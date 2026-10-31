"""Effect measurement (``glass_effect``): the sessions with the ClaudeGlass
mod against the sessions without it, on cost per reply, the context when the
session ended, cold-cache sends, compaction churn and Desktop tip delivery,
shown honestly (both groups, session counts, "too few", "no measurable
difference yet", no causal claim).
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from claudeglass import capture_catalogue as catalogue
from claudeglass import glass_effect, helptext, prompting, recache
from claudeglass.config import Config
from claudeglass.corpus import load_corpus
from claudeglass.glass_effect import SessionFacts
from claudeglass.model import Event, EventKind, TranscriptMeta, TranscriptResult, Turn
from claudeglass.pricing import load_pricing
from claudeglass.render.json_out import render_json
from claudeglass.render.markdown import render_markdown
from claudeglass.report import build_report

from helpers import assert_privacy

TH = recache.RecacheThresholds()


# -- synthetic sessions ----------------------------------------------------------------------


def _turn(sid, index, *, ctx=40_000, creation=0, read=None, gap=60.0, human=True, cc_5m=0) -> Turn:
    return Turn(
        message_id=f"{sid}-{index}", turn_index=index, ts=f"2026-09-18T12:{index:02d}:00.000Z", gap_s=gap, ctx=ctx,
        cache_read_tokens=ctx - creation if read is None else read, cache_creation_tokens=creation, cc_5m=cc_5m,
        preceding_event_kinds=(EventKind.HUMAN_TEXT,) if human else (),
    )


def _transcript(sid, *, replies=4, end_ctx=60_000, cold=0, compactions=0, desktop=False) -> TranscriptResult:
    """A main conversation of ``replies`` replies, ``cold`` of them cold-cache sends of 150,000 tokens (a
    5-minute cache, ten minutes idle), ending at ``end_ctx``."""
    turns = [_turn(sid, 1, ctx=20_000, creation=20_000, read=10_000, cc_5m=1)]
    for i in range(2, replies + 1):
        if i <= cold + 1:
            turns.append(_turn(sid, i, ctx=150_000, creation=148_000, read=2_000, gap=600.0, cc_5m=148_000))
        else:
            turns.append(_turn(sid, i, ctx=150_000, creation=500, cc_5m=500))
    turns[-1].ctx = end_ctx
    events = [Event(kind=EventKind.COMPACT_BOUNDARY, ts="2026-09-18T12:00:30.000Z") for _ in range(compactions)]
    meta = TranscriptMeta(entrypoint="claude-desktop" if desktop else "cli")
    return TranscriptResult(meta=meta, turns=turns, events=events)


def _world(specs):
    """``(prompting sessions, corpus, session_cost, mod_events)`` for ``specs``: a list of dicts with ``id``,
    ``glass`` (bool), ``cost`` and the :func:`_transcript` keywords; ``tips`` is what Claude passed on and
    ``shown`` what the mod showed."""
    sessions, bundles, cost, events = [], [], {}, []
    for spec in specs:
        spec = dict(spec)
        sid = spec.pop("id")
        cost[sid] = spec.pop("cost", 1.0)
        tips, shown = spec.pop("tips", 0), spec.pop("shown", 0)
        glass = spec.pop("glass", False)
        bundles.append(NS(session_id=sid, top=_transcript(sid, **spec)))
        sessions.append(prompting.SessionPrompting(session_id=sid, start=None, tips=Counter({"drip_feed": tips})))
        if glass:
            events.append(_event(sid, "present", surface="desktop" if spec.get("desktop") else "terminal"))
        events += [_event(sid, "note", kind="cold_return", surface="hint", outcome="none")] * shown
    return sessions, NS(sessions=bundles), cost, events


def _event(sid, event, **fields):
    row = {"session_id": sid, "ts": "2026-09-18T12:00:00Z", "event": event, "kind": None, "surface": None,
           "outcome": None, "kept": None, "missing": None, "added": None,
           "instructed": None, "referenced": None, "before": None, "after": None}
    return row | fields


def _table(specs):
    sessions, corpus, cost, events = _world(specs)
    return glass_effect.build_table(sessions, corpus, cost, TH, events)


def _rows(table):
    keys = [c.key for c in table.columns]
    return {row[0]: dict(zip(keys, row)) for row in table.rows}


def _group(prefix, n, *, glass, **spec):
    """``n`` sessions; a numeric ``spec`` value may be a function of the session's number."""
    out = []
    for i in range(n):
        one = {k: v(i) if callable(v) else v for k, v in spec.items()}
        out.append({"id": f"{prefix}{i}", "glass": glass, **one})
    return out


# -- what each figure is -----------------------------------------------------------------------


def test_cold_sends_count_replies_to_you_after_the_cache_expired_with_a_big_context():
    top = _transcript("a", replies=5, cold=2)
    assert glass_effect.cold_sends(top, TH, 100_000) == 2
    # A smaller context is no cold send the hint would warn about.
    assert glass_effect.cold_sends(top, TH, 200_000) == 0
    # Neither is a rebuild that came in the middle of the work, not as a reply to a message of yours.
    for turn in top.turns:
        turn.preceding_event_kinds = ()
    assert glass_effect.cold_sends(top, TH, 100_000) == 0
    # Nor one that no wait caused: the gap was shorter than the cache lifetime.
    quick = _transcript("b", replies=3, cold=1)
    quick.turns[1].gap_s = 120.0
    assert glass_effect.cold_sends(quick, TH, 100_000) == 0


def test_a_session_has_its_cost_replies_end_context_compactions_and_tips():
    sessions, corpus, cost, events = _world([
        {"id": "s", "glass": True, "cost": 2.0, "replies": 4, "end_ctx": 90_000, "cold": 1, "compactions": 2,
         "desktop": True, "tips": 2, "shown": 3},
    ])
    [facts] = glass_effect.facts_of(sessions, corpus, cost, TH, events)
    assert (facts.glass, facts.desktop, facts.cost, facts.replies) == (True, True, 2.0, 4)
    assert (facts.end_context, facts.cold_sends, facts.compactions, facts.tips) == (90_000, 1, 2, 5)
    # A session the report doesn't cover (no prompting session) is left out.
    assert glass_effect.facts_of([], corpus, cost, TH, events) == []


@pytest.mark.parametrize("kind", glass_effect.MOD_EVENTS)
def test_a_session_the_mod_wrote_any_event_for_has_the_mod_whether_or_not_it_logged_its_surface(kind):
    """J10: only a ``present`` event put a session in the 'with' group, and a
    session the mod saw reach a compaction or a cold send, whose surface the
    first ask had missed, was counted among those without it."""
    sessions, corpus, cost, events = _world([{"id": "s", "glass": False}, {"id": "t", "glass": False}])
    assert [f.glass for f in glass_effect.facts_of(sessions, corpus, cost, TH, events)] == [False, False]
    events.append(_event("s", kind))
    by_id = {f.session_id: f for f in glass_effect.facts_of(sessions, corpus, cost, TH, events)}
    assert by_id["s"].glass is True and by_id["t"].glass is False


def test_the_events_that_show_the_mod_ran_are_every_event_but_present():
    assert set(glass_effect.MOD_EVENTS) == set(catalogue.SENSOR_EVENTS) - {"present"}
    assert {"note", "rebuild", "cold_send", "compaction", "tweak"} <= set(glass_effect.MOD_EVENTS)


def test_a_session_with_only_a_non_present_event_does_not_make_it_desktop_but_does_make_it_a_group_member():
    sessions, corpus, cost, events = _world([{"id": f"g{i}", "glass": False} for i in range(10)]
                                            + _group("n", 10, glass=False))
    events += [_event(f"g{i}", "compaction", kind="auto", kept=1, missing=0, added=0, instructed=1) for i in range(10)]
    facts = glass_effect.facts_of(sessions, corpus, cost, TH, events)
    assert sum(f.glass for f in facts) == 10 and not any(f.desktop for f in facts)
    table = glass_effect.build_table(sessions, corpus, cost, TH, events)
    assert table is not None and max(table.rows[0][3:5]) == 10


def test_a_session_is_a_desktop_one_by_its_transcript_or_by_where_the_mod_ran():
    sessions, corpus, cost, events = _world([
        {"id": "a", "desktop": True}, {"id": "b", "glass": True}, {"id": "c"},
    ])
    events.append(_event("b", "present", surface="desktop"))
    by_id = {f.session_id: f for f in glass_effect.facts_of(sessions, corpus, cost, TH, events)}
    assert (by_id["a"].desktop, by_id["b"].desktop, by_id["c"].desktop) == (True, True, False)


def test_the_figures_are_medians_a_pooled_rate_a_share_and_a_mean():
    facts = [
        SessionFacts("a", cost=1.0, replies=2, answered=2, end_context=100_000, cold_sends=1, compactions=2, desktop=True, tips=2),
        SessionFacts("b", cost=6.0, replies=2, answered=2, end_context=200_000, cold_sends=0, compactions=0, desktop=True, tips=0),
        SessionFacts("c", cost=9.0, replies=3, answered=3, end_context=300_000, cold_sends=0, compactions=3, tips=9),
        SessionFacts("d", cost=0.0, replies=0, end_context=None),
    ]
    got = {name: glass_effect.compare(name, facts).without_value for name in glass_effect.FIGURES}
    assert got["cost_per_reply"] == 3.0  # the median of 0.5, 3.0 and 3.0 (the session with no reply has none)
    assert got["end_context"] == 200_000
    assert got["cold_per_100"] == pytest.approx(100 * 1 / 7)  # pooled over the replies, not the mean of the rates
    # Replies in the middle of the work are not in the base: the sends are counted among replies to you.
    mid = SessionFacts("e", cost=1.0, replies=10, answered=2, cold_sends=1)
    assert glass_effect.compare("cold_per_100", [mid]).without_value == 50.0
    assert got["compaction_churn"] == 50.0  # two of four sessions have two or more
    assert got["tips_desktop"] == 1.0  # Desktop sessions only


# -- the honest comparison ---------------------------------------------------------------------


def test_difference_words_say_how_much_higher_or_lower_in_plain_words():
    words = glass_effect.difference_words
    assert words(88.0, 100.0) == "about 12% lower"
    assert words(150.0, 100.0) == "about 50% higher"
    assert words(100.2, 100.0) == "about the same"
    assert words(0.0, 0.0) == "about the same" and words(3.0, 0.0) == "higher, from none"
    assert words(None, 5.0) is None and words(5.0, None) is None


def test_too_few_sessions_with_glass_gives_no_difference():
    table = _table(_group("g", 9, glass=True, cost=1.0) + _group("n", 30, glass=False, cost=2.0))
    row = _rows(table)["cost_per_reply"]
    assert row["difference"] == "Too few sessions with ClaudeGlass yet to compare" and row["verdict"] is None
    # Both groups' values and their session counts are still shown.
    assert (row["with_sessions"], row["without_sessions"]) == (9, 30)
    assert row["with_glass"] == pytest.approx(0.25) and row["without_glass"] == pytest.approx(0.5)
    # The other side being short says so as well.
    other = _rows(_table(_group("g", 12, glass=True) + _group("n", 4, glass=False)))["end_context"]
    assert other["difference"] == "Too few sessions without ClaudeGlass yet to compare" and other["verdict"] is None


def test_a_clear_difference_is_called_measurable_and_stated_in_words():
    table = _table(
        _group("g", 15, glass=True, cost=lambda i: 1.0 + 0.01 * i, end_ctx=lambda i: 40_000 + 100 * i)
        + _group("n", 30, glass=False, cost=lambda i: 2.0 + 0.01 * i, end_ctx=lambda i: 90_000 + 100 * i)
    )
    rows = _rows(table)
    cost = rows["cost_per_reply"]
    assert cost["verdict"] == "Measurable" and cost["difference"] == "about 50% lower"
    assert (cost["with_sessions"], cost["without_sessions"]) == (15, 30)
    assert rows["end_context"]["verdict"] == "Measurable" and rows["end_context"]["difference"].endswith("lower")


def test_no_difference_reads_no_measurable_difference_yet():
    same = lambda i: 1.0 + 0.1 * (i % 5)  # noqa: E731
    row = _rows(_table(_group("g", 10, glass=True, cost=same) + _group("n", 20, glass=False, cost=same)))["cost_per_reply"]
    assert row["verdict"] == "No measurable difference yet" and row["difference"] == "about the same"
    # A gap in the middle values that the spread of the sessions explains is stated, but not called measurable.
    noisy = _rows(_table(_group("g", 12, glass=True, cost=same) + _group("n", 40, glass=False, cost=same)))["cost_per_reply"]
    assert noisy["verdict"] == "No measurable difference yet" and noisy["difference"].endswith("% lower")


def test_cold_sends_and_compaction_churn_compare_across_the_groups():
    table = _table(
        _group("g", 12, glass=True, replies=10, cold=0, compactions=0)
        + _group("n", 40, glass=False, replies=10, cold=lambda i: 2 if i % 2 else 0, compactions=lambda i: 2 if i % 2 else 0)
    )
    rows = _rows(table)
    assert rows["cold_per_100"]["with_glass"] == 0.0 and rows["cold_per_100"]["without_glass"] == pytest.approx(10.0)
    assert rows["cold_per_100"]["verdict"] == "Measurable" and rows["cold_per_100"]["difference"] == "about 100% lower"
    assert rows["compaction_churn"]["with_glass"] == 0.0 and rows["compaction_churn"]["without_glass"] == 50.0
    assert rows["compaction_churn"]["verdict"] == "Measurable"


def test_desktop_tip_delivery_adds_the_mods_tips_to_the_ones_claude_passed_on():
    table = _table(
        _group("g", 10, glass=True, desktop=True, tips=1, shown=3) + _group("n", 12, glass=False, desktop=True, tips=1)
        # Terminal sessions are left out of this row.
        + _group("t", 20, glass=False, tips=7)
    )
    row = _rows(table)["tips_desktop"]
    assert (row["with_glass"], row["without_glass"]) == (4.0, 1.0)
    assert (row["with_sessions"], row["without_sessions"]) == (10, 12)
    assert row["difference"] == "about 300% higher" and row["verdict"] == "Measurable"


def test_the_output_is_deterministic_and_does_not_depend_on_the_order_of_sessions():
    specs = _group("g", 11, glass=True, cost=lambda i: 1.0 + 0.3 * (i % 4)) + _group(
        "n", 25, glass=False, cost=lambda i: 1.2 + 0.4 * (i % 5)
    )
    first, again = _table(specs), _table(specs)
    assert first.rows == again.rows
    assert _table(list(reversed(specs))).rows == first.rows


def test_non_interactive_runs_are_left_out_of_both_groups():
    sessions, corpus, cost, events = _world(_group("g", 10, glass=True) + _group("n", 10, glass=False))
    for i, bundle in enumerate(corpus.sessions):
        if i % 5 == 0:  # four sessions, in both groups
            bundle.top.meta.entrypoint = ("sdk-cli", "sdk-ts", "sdk-py", "mcp")[i // 5]
    ids = {f.session_id for f in glass_effect.facts_of(sessions, corpus, cost, TH, events)}
    assert ids == {b.session_id for i, b in enumerate(corpus.sessions) if i % 5}
    assert len(ids) == 16
    for word in ("sdk", "sdk-cli", "sdk-ts", "sdk-py", "SDK-PY", "mcp"):
        assert glass_effect.is_headless(word)
    for word in ("cli", "claude-desktop", "claude-vscode", "", None):
        assert not glass_effect.is_headless(word)
    table = glass_effect.build_table(sessions, corpus, cost, TH, events)
    assert max(table.rows[0][3:5]) == 8
    assert any("before and after" in n for n in table.notes)


def test_a_session_is_not_called_measurable_by_chance_across_five_figures():
    """Five rows at 95% would show a "Measurable" row by chance about 1 report in 4. At 99% each, about 1 in 20 at most."""
    assert glass_effect.INTERVAL == (0.005, 0.995)
    # With no real difference, many different samples of sessions seldom give any "Measurable" row.
    import random

    rng = random.Random(7)
    hits = 0
    for _ in range(40):
        specs = _group("g", 10, glass=True, cost=lambda i: rng.uniform(0.5, 2.0), end_ctx=lambda i: rng.randint(40_000, 90_000)) + _group(
            "n", 10, glass=False, cost=lambda i: rng.uniform(0.5, 2.0), end_ctx=lambda i: rng.randint(40_000, 90_000)
        )
        table = _table(specs)
        hits += any(row[6] == "Measurable" for row in table.rows)
    assert hits <= 4


def test_the_table_carries_the_caveat_and_what_the_mod_recorded_about_cold_sends():
    specs = _group("g", 10, glass=True, cost=1.0) + _group("n", 10, glass=False, cost=1.0)
    sessions, corpus, cost, events = _world(specs)
    events += [_event("g0", "cold_send", outcome="warned"), _event("g1", "cold_send", outcome="unwarned"),
               _event("g2", "cold_send", outcome="warned"), _event("elsewhere", "cold_send", outcome="warned")]
    table = glass_effect.build_table(sessions, corpus, cost, TH, events)
    notes = " ".join(table.notes)
    assert "does not show that ClaudeGlass caused it" in notes
    # Most sessions without the mod are from before it was added, and no note sentence is over 25 words.
    assert "mostly before and after" in notes and "99%" in notes
    assert all(len(sentence.split()) <= 25 for n in table.notes for sentence in n.replace("? ", ". ").split(". "))
    # Only the report's own sessions count, and only the mod can have warned (a cold send is the
    # mod's event, so a session that has one is a session with the mod).
    assert "recorded 3 cold-cache sends" in notes and "warned before 2 of them" in notes
    assert not any("recorded" in n for n in glass_effect.build_table(sessions, corpus, cost, TH, events[:-4]).notes)


def test_the_table_says_a_claude_p_run_inside_a_desktop_session_counts_as_a_desktop_session():
    """J41: such a run inherits the desktop app's entrypoint, and nothing in
    its transcript tells the two apart."""
    table = _table(_group("g", 10, glass=True) + _group("n", 10, glass=False))
    [note] = [n for n in table.notes if "claude -p" in n]
    assert "Desktop session" in note and "without ClaudeGlass" in note
    assert all(len(sentence.split()) <= 25 for sentence in note.replace("? ", ". ").split(". "))
    assert "claude -p" in (glass_effect.__doc__ or "")


def test_without_a_session_that_has_the_mod_there_is_no_table():
    specs = _group("n", 12, glass=False)
    sessions, corpus, cost, events = _world(specs)
    for mod_events in (None, [], events, [_event("elsewhere", "present", surface="desktop")]):
        assert glass_effect.build_table(sessions, corpus, cost, TH, mod_events) is None


def test_the_table_holds_only_counts_and_figures(tmp_path):
    table = _table(_group("g", 10, glass=True) + _group("n", 10, glass=False))
    assert_privacy(table)
    assert [c.key for c in table.columns] == [
        "figure", "with_glass", "without_glass", "with_sessions", "without_sessions", "difference", "verdict",
    ]
    assert [row[0] for row in table.rows] == list(glass_effect.FIGURES)
    assert set(table.row_kinds) == set(glass_effect.FIGURES) == set(table.value_labels)


# -- the tweaks table ---------------------------------------------------------------------------


def _tweaks(events, ids=("g0", "g1", "g2")):
    sessions = [prompting.SessionPrompting(session_id=sid, start=None) for sid in ids]
    return glass_effect.build_tweaks_table(sessions, events)


def _tweak_rows(table):
    keys = [c.key for c in table.columns]
    return {row[0]: dict(zip(keys, row)) for row in table.rows}


def test_each_kind_of_tweak_is_counted_by_how_often_it_was_pressed():
    events = [
        _event("g0", "tweak", kind="summarise", before=9, after=7),
        _event("g0", "tweak", kind="summarise", before=9, after=9),
        _event("g1", "tweak", kind="readd", before=3),
        _event("g1", "tweak", kind="check", before=2, after=1),
        _event("g2", "tweak", kind="summarise", before=5),
        _event("elsewhere", "tweak", kind="hold", before=1, after=0),
    ]
    table = _tweaks(events)
    assert table.name == "prompting_tweaks" and table.title == "Tweaks"
    assert [c.key for c in table.columns] == ["tweak", "pressed", "kept_with", "kept_without", "referenced"]
    assert [row[0] for row in table.rows] == ["summarise", "readd", "check", "hold"]
    assert tuple(row[0] for row in table.rows) == catalogue.SENSOR_TWEAK_KINDS
    pressed = {kind: row["pressed"] for kind, row in _tweak_rows(table).items()}
    # A session outside the report is not counted.
    assert pressed == {"summarise": 3, "readd": 1, "check": 1, "hold": 0}
    assert set(table.value_labels) == set(glass_effect.TWEAK_LABELS) == set(catalogue.SENSOR_TWEAK_KINDS)


def test_the_kept_rate_is_pooled_for_summaries_with_the_keep_list_and_those_without():
    events = [
        _event("g0", "compaction", kind="plugin", kept=9, missing=1, added=0, instructed=1),
        _event("g1", "compaction", kind="auto", kept=6, missing=4, added=0, instructed=1),
        _event("g1", "compaction", kind="auto", kept=3, missing=7, added=0, instructed=0),
        _event("g2", "compaction", kind="manual", kept=0, missing=0, added=0, instructed=0),
        # An older mod left the count out: it is in neither rate.
        _event("g2", "compaction", kind="auto", kept=10, missing=0, added=0),
        _event("g2", "tweak", kind="summarise", before=9, after=9),
    ]
    rows = _tweak_rows(_tweaks(events))
    assert rows["summarise"]["pressed"] == 1
    assert rows["summarise"]["kept_with"] == pytest.approx(75.0)  # 15 of 20
    assert rows["summarise"]["kept_without"] == pytest.approx(30.0)  # 3 of 10
    # Only the summarise row carries the rates.
    assert all(r["kept_with"] is None and r["kept_without"] is None for k, r in rows.items() if k != "summarise")


def test_a_rate_with_nothing_to_divide_is_blank_not_zero():
    events = [_event("g0", "compaction", kind="auto", kept=0, missing=0, added=0, instructed=1),
              _event("g0", "tweak", kind="check", before=1)]
    row = _tweak_rows(_tweaks(events))["summarise"]
    assert row["kept_with"] is None and row["kept_without"] is None


def test_the_re_added_items_later_replies_used_are_summed_over_the_presses_not_the_summaries():
    """J23: a summary's ``referenced`` is a running count of the review's
    receipts settled so far, so every later summary repeats the earlier
    re-adds, and adding them up counted one item several times. The row
    adds up what each ``readd`` press says it got back (its ``after``)."""
    events = [
        _event("g0", "compaction", kind="plugin", kept=6, missing=3, added=3, instructed=1, referenced=2),
        _event("g0", "compaction", kind="auto", kept=7, missing=2, added=2, instructed=1, referenced=2),
        _event("g1", "compaction", kind="auto", kept=7, missing=2, added=2, instructed=1, referenced=3),
        _event("g0", "tweak", kind="readd", before=3, after=2),
        _event("g1", "tweak", kind="readd", before=2, after=1),
        # Not settled yet: the mod has no count for this press.
        _event("g2", "tweak", kind="readd", before=4),
    ]
    rows = _tweak_rows(_tweaks(events))
    assert rows["readd"]["referenced"] == 3 and rows["readd"]["pressed"] == 3
    assert rows["summarise"]["referenced"] is None
    # A zero from the mod is a zero; nothing settled at all is blank, whatever the summaries count.
    zero = [_event("g1", "tweak", kind="readd", before=2, after=0)]
    assert _tweak_rows(_tweaks(zero))["readd"]["referenced"] == 0
    unsettled = events[:3] + [_event("g0", "tweak", kind="readd", before=3)]
    assert _tweak_rows(_tweaks(unsettled))["readd"]["referenced"] is None
    # A press in a session outside the report adds nothing.
    outside = zero + [_event("elsewhere", "tweak", kind="readd", before=2, after=9)]
    assert _tweak_rows(_tweaks(outside))["readd"]["referenced"] == 0


def test_with_nothing_pressed_and_no_instructed_summary_the_table_has_no_rows_to_draw():
    table = _tweaks([_event("g0", "present", surface="desktop"),
                     _event("g0", "compaction", kind="auto", kept=4, missing=1, added=0)])
    assert table is not None and table.rows == []
    # The dashboard draws an empty table as its own note.
    grid = (Path(__file__).resolve().parents[1] / "src/claudeglass/service/static/grid.js").read_text(encoding="utf-8")
    assert 'prompting_tweaks: ["No tweaks pressed yet.",' in grid


def test_a_report_on_a_machine_without_the_mod_has_no_tweaks_table():
    for events in (None, [], [_event("elsewhere", "present", surface="desktop")],
                   [_event("g0", "note", kind="cold_return", surface="hint", outcome="none")]):
        assert _tweaks(events) is None


def test_the_tweaks_table_holds_only_counts_and_its_words_keep_to_the_help_rules():
    events = [_event("g0", "tweak", kind="summarise", before=9, after=7),
              _event("g0", "compaction", kind="auto", kept=4, missing=1, added=0, instructed=1)]
    table = _tweaks(events)
    assert_privacy(table)
    words = [*table.notes, *glass_effect.TWEAK_LABELS.values(), *(c.label for c in table.columns)]
    sentences = [x for text in words for x in text.replace("? ", ". ").split(". ")]
    assert all(len(x.split()) <= 25 for x in sentences)
    blob = " ".join([table.title, *words])
    assert "~" not in blob and "Apply" not in blob and "(est.)" not in blob
    assert "_" not in blob  # no internal names


def test_the_tweaks_table_has_its_words_in_the_help_and_the_placement():
    copy = helptext.TABLE_COPY["prompting_tweaks"]
    assert helptext.placement_for("prompting_tweaks") == "keep"
    assert set(copy.columns) == {"tweak", "pressed", "kept_with", "kept_without", "referenced"}
    assert "does not show that a tweak caused it" in copy.help.read


# -- in the report, and on the dashboard --------------------------------------------------------


def _write_top(project_dir: Path, session_id: str, replies: int = 3) -> None:
    from helpers import turn_line, user_str_line, write_jsonl

    lines = []
    for i in range(replies):
        stamp = f"2026-09-18T12:{i * 2:02d}:00.000Z"
        lines.append(user_str_line(f"task {i}", timestamp=stamp, origin={"kind": "human"}, sessionId=session_id))
        lines.append(turn_line(timestamp=f"2026-09-18T12:{i * 2 + 1:02d}:00.000Z", cache_read_input_tokens=40_000,
                               sessionId=session_id))
    write_jsonl(project_dir / f"{session_id}.jsonl", lines)


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    project_dir = tmp_path_factory.mktemp("effect") / "proj"
    project_dir.mkdir()
    for i in range(3):
        _write_top(project_dir, f"session-{i:03d}")
    return load_corpus([project_dir])


def test_the_report_puts_the_table_beside_the_sessions_with_the_mod_table(corpus):
    ids = [b.session_id for b in corpus.sessions]
    assert len(ids) == 3
    events = [_event(ids[0], "present", surface="desktop")]
    report = build_report(corpus, load_pricing(), Config(), projects=("proj",), window="w", mod_events=events)
    section = next(s for s in report.sections if s.key == "prompting")
    names = [t.name for t in section.tables]
    assert names[-3:] == ["prompting_glass", "prompting_effect", "prompting_tweaks"]
    table = section.tables[-2]
    # Annotated like every table: its words, its place on the page and its help on every column.
    assert table.title == "Sessions with and without ClaudeGlass" and table.dashboard == "keep"
    assert table.help and table.help.shows and all(c.help for c in table.columns)
    # Three sessions: one with the mod, so every row says there are too few.
    assert {row[5] for row in table.rows} == {"Too few sessions with ClaudeGlass yet to compare"}
    assert_privacy(table)
    # The JSON carries the raw rows and the row kinds the dashboard formats them by.
    data = json.loads(render_json(report))["report"]
    shown = next(t for s in data["sections"] if s["key"] == "prompting" for t in s["tables"] if t["name"] == "prompting_effect")
    assert shown["row_kinds"]["cost_per_reply"] == "money" and shown["rows"][0][0] == "cost_per_reply"
    # And the Markdown report reads it in words.
    text = render_markdown(report)
    assert "Sessions with and without ClaudeGlass" in text and "Cost per reply" in text


def test_a_report_without_the_mod_has_no_effect_table(corpus):
    for events in (None, []):
        report = build_report(corpus, load_pricing(), Config(), projects=("proj",), window="w", mod_events=events)
        section = next(s for s in report.sections if s.key == "prompting")
        assert "prompting_effect" not in [t.name for t in section.tables]
        assert "prompting_tweaks" not in [t.name for t in section.tables]


def test_the_report_carries_the_tweaks_table_with_its_help(corpus):
    ids = [b.session_id for b in corpus.sessions]
    events = [_event(ids[0], "present", surface="desktop"),
              _event(ids[0], "tweak", kind="summarise", before=9, after=7)]
    report = build_report(corpus, load_pricing(), Config(), projects=("proj",), window="w", mod_events=events)
    section = next(s for s in report.sections if s.key == "prompting")
    table = next(t for t in section.tables if t.name == "prompting_tweaks")
    assert table.title == "Tweaks" and table.dashboard == "keep"
    assert table.help and table.help.shows and all(c.help for c in table.columns)
    assert _tweak_rows(table)["summarise"]["pressed"] == 1
    assert "Tweaks" in render_markdown(report)


def test_the_table_has_its_words_in_the_help_and_the_placement():
    copy = helptext.TABLE_COPY["prompting_effect"]
    assert helptext.placement_for("prompting_effect") == "keep"
    assert set(copy.columns) == {"figure", "with_glass", "without_glass", "with_sessions", "without_sessions",
                                 "difference", "verdict"}
    assert "10" in copy.help.read and "does not show the mod caused it" in copy.help.read
    assert "unlikely to be chance alone" in copy.help.read and "five figures together" in copy.help.read
    assert "larger than chance alone would likely give" not in copy.help.read


def test_the_dashboard_draws_every_table_of_the_prompting_section_but_the_habit_cards():
    """The Work habits page hands the prompting section's other tables to the grid, which formats a figure's
    cells by the table's ``row_kinds`` (money, tokens, a rate, a share) and labels its rows by ``value_labels``."""
    page = (Path(__file__).resolve().parents[1] / "src/claudeglass/service/static/page-habits.js").read_text(encoding="utf-8")
    start = page.index("function renderPromptingSection(")
    body = page[start : page.index("\nfunction promptingCard(", start)]
    assert 'return table.name !== "prompting_habits";' in body
    assert 'renderPlacedTables(block, rest, state.currency, "habits");' in body
    grid = (Path(__file__).resolve().parents[1] / "src/claudeglass/service/static/grid.js").read_text(encoding="utf-8")
    assert 'kind === "str" && typeof value === "number") kind = rowKind;' in grid

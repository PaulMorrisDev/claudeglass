"""Rework after delivery (``rework.py``): the headline and its words, the
causes and where each came from, Claude's admitted mistakes, the weekly bars
and the rework per request by level.

Pieces are drawn with ``pieces.pieces_of`` from ``Turn`` objects, so each fact
a rule reads is set by name; one test goes through ``habits.collect`` on a
parsed transcript. Amounts use ``tests/fixtures/pricing_min.toml``'s
``claude-widget-9``: a reply costs ``REPLY`` USD.
"""

from __future__ import annotations

from datetime import timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from claudeglass import capture_catalogue as catalogue, habits, helptext, parse, pieces, rework
from claudeglass.model import CaptureTag, Feedback, Section, Table
from claudeglass.pricing import load_pricing
from claudeglass.units import Units

from helpers import assert_privacy
from test_habits import _corrected_session, _cycle, _gave
from test_pieces import BASE, REPLY, _aside, _build, _fix, _msg, _only, _tag

FIXTURES = Path(__file__).resolve().parent / "fixtures"
WINDOW = "last 30 days"
PERIOD = "over the last 30 days"


@pytest.fixture(autouse=True)
def _salt():
    parse.set_salt(b"r" * 32)


@pytest.fixture()
def pricing():
    return load_pricing(path=FIXTURES / "pricing_min.toml")


def _h(*work, **kw) -> habits.Habits:
    kw.setdefault("window", WINDOW)
    kw.setdefault("tz", "UTC")
    return habits.Habits(work_pieces=list(work), **kw)


def _rows(table: Table) -> list[dict]:
    return [dict(zip((c.key for c in table.columns), row)) for row in table.rows]


def _table(section: Section, name: str) -> Table:
    return next(t for t in section.tables if t.name == name)


def _pieces(*, fixes=(), start=100, **kw) -> list[pieces.WorkPiece]:
    """One session of two pieces, so both have their start found: the first
    delivers and is then fixed ``fixes`` times (each a dict of what the fix
    says), the second delivers and is left alone. ``kw`` goes to ``pieces_of``."""
    cycles = [_build(0)]
    cycles += [_fix(10 * (n + 1), **fix) for n, fix in enumerate(fixes)]
    cycles.append(_msg(start, files=("x", "y"), tag=_tag(shift="new"), human_prompt_chars=400))
    return pieces.pieces_of(cycles, **kw)


def _week_piece(week: int, *, reworked: bool, tag=None, base=None) -> pieces.WorkPiece:
    """A delivered piece, found because a new one starts after it, in the
    week ``week`` weeks after ``BASE``'s; ``tag`` is what its first message
    carries."""
    base = base or BASE + timedelta(weeks=week)
    cycles = [_msg(0, files=("a", "b"), human_prompt_chars=400, base=base, tag=tag)]
    if reworked:
        cycles.append(_msg(10, files=("a",), human_prompt_chars=40, base=base, human_correction=True))
    cycles.append(_msg(60, files=("z",), human_prompt_chars=400, base=base, tag=_tag(shift="new")))
    return pieces.pieces_of(cycles)[0]


# -- the headline ----------------------------------------------------------------------


def test_the_headline_is_the_plans_words_with_the_amount_and_its_period(pricing):
    work = _pieces(fixes=[{"tag": _tag(shift="fix", why="left_out")}], rates=pricing)
    section = rework.build_section(_h(*work))
    units = Units()
    amount = units.money(REPLY, period=PERIOD).phrase()
    assert PERIOD in amount
    (row,) = _rows(_table(section, "rework_headline"))
    assert row["item"] == "pieces"
    assert row["text"] == (
        f"1 of your 2 pieces of work needed changes after Claude delivered them. That rework cost {amount}. "
        "100% came from requests that left something out, 0% from Claude's mistakes, 0% from changes of mind."
    )
    assert (row["count"], row["total"], row["share"], row["period"]) == (1, 2, 50.0, PERIOD)
    assert row["cost"] == pytest.approx(REPLY) and row["tokens"] == 150


def test_the_headline_names_the_share_from_other_causes_when_there_is_some():
    rate = pieces.ReworkRate(pieces=4, segmented=4, reworked=4, substantive=8, rework=4)
    rows = [
        rework.CauseRow("tools", "inferred", 2, 2, 0.0, 0),
        rework.CauseRow("left_out", "feedback", 1, 1, 0.0, 0),
        rework.CauseRow("not_reported", "inferred", 1, 1, 0.0, 0),
    ]
    text = rework.headline_text(rework.Rework(rate=rate, piece_causes=rows), Units(), PERIOD)
    assert text.endswith("0% from changes of mind. 50% came from failed tools, plan gaps or a mix of causes.")
    plain = rework.headline_text(rework.Rework(rate=rate, piece_causes=rows[1:]), Units(), PERIOD)
    assert plain.endswith("from changes of mind.")


def test_the_headline_leaves_the_shares_out_when_no_rework_had_a_cause():
    # "0% ... 0% ... 0%" says nothing the unknown line under it doesn't.
    work = _pieces(fixes=[{"human_correction": True}])
    rows = {r["item"]: r for r in _rows(_table(rework.build_section(_h(*work)), "rework_headline"))}
    assert rows["pieces"]["text"] == (
        "1 of your 2 pieces of work needed changes after Claude delivered them. That rework was not priced."
    )
    assert rows["unknown"]["text"] == "We couldn't tell why for 100%: run /cg-feedback after a piece of work to say."


def test_the_title_names_the_window_and_the_section_has_the_plans_title_and_intro():
    section = rework.build_section(_h(window="last 7 days"))
    assert (section.key, section.title) == ("rework", "Rework after delivery")
    assert section.intro == (
        "How often Claude had to change work it had already delivered, why, and what to change in how you ask."
    )
    assert _table(section, "rework_headline").title == "Rework after delivery (last 7 days)"
    assert rework.period_phrase("all time") == "over all time"
    assert rework.period_phrase("since your last change") == "since your last change"
    assert rework.period_phrase("") == ""


def test_rework_that_cost_nothing_we_could_price_says_so_never_a_bare_zero():
    work = _pieces(fixes=[{"human_correction": True}])
    row = _rows(_table(rework.build_section(_h(*work)), "rework_headline"))[0]
    assert row["item"] == "pieces" and "That rework was not priced." in row["text"]
    assert "0.00" not in row["text"] and "$" not in row["text"]


def test_a_subscription_amount_carries_the_period_the_same_way(pricing):
    work = _pieces(fixes=[{"human_correction": True}], rates=pricing)
    section = rework.build_section(_h(*work), Units(billing_mode="subscription"))
    row = _rows(_table(section, "rework_headline"))[0]
    assert f"list-price equivalent {PERIOD}" in row["text"]


def test_no_rework_says_none_and_one_piece_reads_as_one():
    clean = rework.build_section(_h(*_pieces()))
    (row,) = _rows(_table(clean, "rework_headline"))
    assert row["text"] == "None of your 2 pieces of work needed changes after Claude delivered them."
    assert clean.notes == ["None of the work delivered in this window needed changes afterwards."]
    single = pieces.pieces_of([_build(0), _fix(10, human_correction=True), _msg(50, tag=_tag(shift="new"))])[0]
    text = rework.headline_text(rework.collect(_h(single)), Units(), PERIOD)
    assert text.startswith("1 of your 1 piece of work needed changes after Claude delivered it. ")


def test_a_short_message_re_changing_the_files_with_no_flag_or_tag_is_no_rework_in_the_section():
    # Only a flag or a tag makes a follow-up rework; the size of the message and the files it changes do not.
    plain = rework.build_section(_h(*_pieces(fixes=[{}, {}])))
    (row,) = _rows(_table(plain, "rework_headline"))
    assert row["text"] == "None of your 2 pieces of work needed changes after Claude delivered them."
    assert _rows(_table(plain, "rework_causes")) == []
    flagged = rework.build_section(_h(*_pieces(fixes=[{}, {"human_correction": True}])))
    causes = _rows(_table(flagged, "rework_causes"))
    assert [(r["cause"], r["source"], r["cycles"]) for r in causes] == [("not_reported", "inferred", 1)]


def test_work_nothing_delivered_has_no_rework_and_says_why():
    section = rework.build_section(_h(_only([_msg(0), _msg(10)])))
    assert _table(section, "rework_headline").rows == []
    assert section.notes[0].startswith("No piece of work has changed files in this window yet")


def test_the_share_we_could_not_tell_why_says_to_run_feedback():
    work = _pieces(fixes=[{"tag": _tag(shift="fix", why="left_out")}, {"human_correction": True}])
    rows = {r["item"]: r for r in _rows(_table(rework.build_section(_h(*work)), "rework_headline"))}
    assert rows["unknown"]["text"] == "We couldn't tell why for 50%: run /cg-feedback after a piece of work to say."
    assert (rows["unknown"]["count"], rows["unknown"]["total"]) == (1, 2)
    # Nothing unknown, nothing said.
    known = _pieces(fixes=[{"tag": _tag(shift="fix", why="missed")}])
    assert [r["item"] for r in _rows(_table(rework.build_section(_h(*known)), "rework_headline"))] == ["pieces"]


def test_sessions_we_could_not_cut_into_pieces_are_counted_by_their_requests(pricing):
    cycles = [_build(0), _fix(10, human_correction=True), _fix(20, human_correction=True), _msg(30)]
    work = pieces.pieces_of(cycles, rates=pricing)
    assert [p.unsegmented for p in work] == [True]
    section = rework.build_section(_h(*work))
    row = {r["item"]: r for r in _rows(_table(section, "rework_headline"))}["requests"]
    amount = Units().money(2 * REPLY, period=PERIOD).phrase()
    assert row["item"] == "requests"
    assert row["text"] == (
        "2 of 4 requests in 1 session we couldn't split into pieces of work needed changes after Claude "
        f"delivered. That rework cost {amount}."
    )
    # Beside segmented pieces they say nothing when they needed no changes.
    quiet = pieces.pieces_in([NS(session_id="a", cycles=[_build(0), _msg(10, tag=_tag(shift="new"))], project="p", feedback=None),
                              NS(session_id="b", cycles=[_build(0), _msg(10)], project="p", feedback=None)])
    assert {p.unsegmented for p in quiet} == {False, True}
    assert [r["item"] for r in _rows(_table(rework.build_section(_h(*quiet)), "rework_headline"))] == ["pieces"]


def test_the_headline_figures_are_the_pieces_own_and_sessions_we_could_not_split_keep_theirs(pricing):
    segmented = _pieces(fixes=[{"tag": _tag(shift="fix", why="missed")}], rates=pricing)
    loose = pieces.pieces_of([_build(0), _fix(10, human_correction=True), _fix(20, human_correction=True)], rates=pricing)
    section = rework.build_section(_h(*segmented, *loose))
    rows = {r["item"]: r for r in _rows(_table(section, "rework_headline"))}
    # The sentence's n of m, cost and shares are the pieces': the loose session's two follow-ups are not in them.
    assert (rows["pieces"]["count"], rows["pieces"]["total"], rows["pieces"]["cost"]) == (1, 2, pytest.approx(REPLY))
    assert rows["pieces"]["tokens"] == 150
    assert rows["pieces"]["text"].endswith(
        "0% came from requests that left something out, 100% from Claude's mistakes, 0% from changes of mind."
    )
    # Beside them, the sessions have their own sentence, cost and tokens.
    assert (rows["requests"]["count"], rows["requests"]["cost"], rows["requests"]["tokens"]) == (2, pytest.approx(2 * REPLY), 300)
    # The share we could not tell why for reads beside the shares above it: the pieces had none.
    assert "unknown" not in rows
    # The cause cards still count every rework cycle.
    cards = {(r["cause"], r["source"]): r for r in _rows(_table(section, "rework_causes"))}
    assert cards[("missed", "Claude tag")]["share"] == pytest.approx(100 / 3)
    assert cards[("not_reported", "inferred")]["cycles"] == 2
    r = rework.collect(_h(*segmented, *loose))
    assert (r.rate.reworked, r.rate.segmented, r.cycles) == (1, 2, 3)


def test_with_no_rework_in_a_piece_the_unknown_share_is_every_sessions():
    quiet = _pieces()
    loose = pieces.pieces_of([_build(0), _fix(10, human_correction=True), _fix(20, human_correction=True)])
    rows = {r["item"]: r for r in _rows(_table(rework.build_section(_h(*quiet, *loose)), "rework_headline"))}
    assert rows["unknown"]["text"] == "We couldn't tell why for 100%: run /cg-feedback after a piece of work to say."
    assert (rows["unknown"]["count"], rows["unknown"]["total"]) == (2, 2)


def _aside_pieces(asides: int = 2, **kw) -> list[pieces.WorkPiece]:
    """One session of two pieces: the first delivers and is sent ``asides``
    messages while work runs, the second delivers and is left alone."""
    cycles = [_build(0)] + [_aside(5 + n, tag=_tag(shift="fix")) for n in range(asides)]
    cycles.append(_msg(100, files=("x", "y"), tag=_tag(shift="new"), human_prompt_chars=400))
    return pieces.pieces_of(cycles, **kw)


def test_the_messages_sent_while_background_work_ran_are_a_line_under_the_headline(pricing):
    work = _aside_pieces(2, rates=pricing)
    section = rework.build_section(_h(*work))
    rows = {r["item"]: r for r in _rows(_table(section, "rework_headline"))}
    assert list(rows) == ["pieces", "asides"]
    amount = Units().money(2 * REPLY, period=PERIOD).primary
    assert PERIOD in amount
    assert rows["asides"]["text"] == (
        f"Not counted as rework: 2 messages you sent while background work ran ({amount})."
    )
    assert (rows["asides"]["count"], rows["asides"]["total"], rows["asides"]["share"]) == (2, None, None)
    assert rows["asides"]["cost"] == pytest.approx(2 * REPLY) and rows["asides"]["period"] == PERIOD
    # They are not the rework the headline counts, and the pieces' own row is as it was.
    assert rows["pieces"]["text"] == "None of your 2 pieces of work needed changes after Claude delivered them."
    r = rework.collect(_h(*work))
    assert (r.asides, r.aside_cost, r.rate.reworked, r.cycles) == (2, pytest.approx(2 * REPLY), 0, 0)


def test_one_message_reads_as_one_and_an_unpriced_one_has_no_amount():
    section = rework.build_section(_h(*_aside_pieces(1)))
    row = {r["item"]: r for r in _rows(_table(section, "rework_headline"))}["asides"]
    assert row["text"] == "Not counted as rework: 1 message you sent while background work ran."
    assert row["cost"] == 0.0 and row["count"] == 1


def test_a_subscription_amount_in_the_background_work_line_is_not_nested(pricing):
    section = rework.build_section(_h(*_aside_pieces(2, rates=pricing)), Units(billing_mode="subscription"))
    row = {r["item"]: r for r in _rows(_table(section, "rework_headline"))}["asides"]
    assert row["text"].startswith("Not counted as rework: 2 messages you sent while background work ran (")
    assert f"list-price equivalent {PERIOD}" in row["text"]
    assert row["text"].count("(") == 1 and row["text"].endswith(").")


def test_the_background_work_line_is_there_only_when_a_delivered_piece_has_some(pricing):
    # None sent: no line, whatever else the headline says.
    plain = rework.build_section(_h(*_pieces(fixes=[{"human_correction": True}], rates=pricing)))
    assert [r["item"] for r in _rows(_table(plain, "rework_headline"))] == ["pieces", "unknown"]
    assert rework.collect(_h(*_pieces())).asides == 0
    # A piece that changed nothing has no rework to leave them out of.
    quiet = pieces.pieces_of([_msg(0), _aside(10)])
    assert quiet[0].aside_cycles == 1 and not quiet[0].delivered
    assert rework.collect(_h(*quiet)).asides == 0
    assert _table(rework.build_section(_h(*quiet)), "rework_headline").rows == []
    # The same pieces with a message sent while work ran among them do.
    both = rework.build_section(_h(*_pieces(), *_aside_pieces(3, rates=pricing)))
    rows = {r["item"]: r for r in _rows(_table(both, "rework_headline"))}
    assert rows["asides"]["count"] == 3 and list(rows)[-1] == "asides"


def test_a_message_sent_during_background_work_that_says_fix_is_no_rework_in_the_headline_or_the_causes(pricing):
    work = _aside_pieces(2, rates=pricing)
    section = rework.build_section(_h(*work))
    assert _table(section, "rework_causes").rows == []
    assert _table(section, "rework_by_level").rows[0][1:4] == [2, 0, 0.0]


# -- the causes ------------------------------------------------------------------------


def test_causes_rank_your_feedback_first_and_say_where_each_came_from():
    fixes = [
        {"human_correction": True},
        {"tag": _tag(shift="fix", why="changed")},
        {"tag": _tag(shift="fix", why="tools")},
        {"tag": _tag(shift="fix", why="tools")},
    ]
    work = _pieces(fixes=fixes, feedback=Feedback(source="answers", why=("left_out",)))
    # Your answer names the cause of every rework cycle in the piece it covers.
    rows = _rows(_table(rework.build_section(_h(*work)), "rework_causes"))
    assert [(r["cause"], r["source"], r["cycles"]) for r in rows] == [("left_out", "feedback", 4)]
    mixed = _pieces(fixes=fixes) + _pieces(
        fixes=[{"human_correction": True, "queued_adjust": True}], feedback=Feedback(source="answers", why=("missed",))
    )
    ranked = [(r["cause"], r["source"]) for r in _rows(_table(rework.build_section(_h(*mixed)), "rework_causes"))]
    assert ranked == [
        ("missed", "feedback"),
        ("tools", "Claude tag"),
        ("changed", "Claude tag"),
        ("not_reported", "inferred"),
    ]
    # The words the dashboard shows are the closed ones, not the stored ones.
    assert {r["cause"] for r in rows} <= set(pieces.CAUSES) | {rework.PLAN_FIXES}


def test_a_card_counts_pieces_and_the_sessions_we_could_not_split_apart():
    """The headline counts pieces whose start was found; a card also counts
    the rework in whole sessions, so it says which of the two it is counting
    rather than calling a session a piece."""
    segmented = _pieces(fixes=[{"human_correction": True}])
    loose = pieces.pieces_of([_build(0), _fix(10, human_correction=True), _fix(20, human_correction=True)])
    (row,) = _rows(_table(rework.build_section(_h(*segmented, *loose)), "rework_causes"))
    assert (row["pieces"], row["sessions"], row["cycles"]) == (1, 1, 3)
    assert row["detail"] == "3 follow-ups in 1 piece and 1 session we couldn't split into pieces."
    (row,) = _rows(_table(rework.build_section(_h(*loose)), "rework_causes"))
    assert (row["pieces"], row["sessions"]) == (0, 1)
    assert row["detail"] == "2 follow-ups in 1 session we couldn't split into pieces."
    (row,) = _rows(_table(rework.build_section(_h(*segmented)), "rework_causes"))
    assert row["detail"] == "1 follow-up in 1 piece."


def test_a_tag_haiku_wrote_and_one_inferred_from_the_message_are_labelled_so():
    haiku = _pieces(fixes=[{"tag": CaptureTag(has_tl=True, judged=True, shift="fix", why="missed")}])
    inferred = _pieces(fixes=[{"human_adjust": True, "files": ("a",), "human_prompt_chars": 900}])
    sources = {
        r["source"]
        for r in _rows(_table(rework.build_section(_h(*haiku, *inferred)), "rework_causes"))
    }
    assert sources == {"Haiku tag", "inferred"}


def test_each_cause_has_a_try_line_and_the_plans_line_to_copy():
    fixes = [
        {"tag": _tag(shift="fix", why=why)} for why in ("left_out", "missed", "changed", "tools")
    ]
    rows = {r["cause"]: r for r in _rows(_table(rework.build_section(_h(*_pieces(fixes=fixes))), "rework_causes"))}
    assert "/cg-brief" not in rows["left_out"]["try"] + rows["left_out"]["paste"]
    assert rows["left_out"]["paste"].startswith("Here is what I want, the files it involves")
    assert rows["missed"]["paste"] == (
        "Before you finish, re-read my request, check each point is done, and run the tests for what you changed."
    )
    assert rows["changed"]["paste"] == "Plan this first and wait for my go-ahead before changing any files."
    assert rows["tools"]["paste"] == "" and "{{page:actions/checks}}" in rows["tools"]["try"]
    assert all(r["try"] for r in rows.values())


def test_the_brief_skill_is_offered_for_left_out_asks_only_while_the_brief_card_shows():
    fixes = [{"tag": _tag(shift="fix", why="left_out")}]
    work = _pieces(fixes=fixes)
    clear, vague = CaptureTag(task="bugfix", brief="clear"), CaptureTag(task="bugfix", brief="vague")
    costly = [*(_cycle(tag=clear) for _ in range(5)), *(_cycle(tag=vague, cost=3.0) for _ in range(5))]
    cheap = [*(_cycle(tag=clear) for _ in range(5)), *(_cycle(tag=vague) for _ in range(5))]
    for cycles, offered in ((costly, True), (cheap, False), ([], False)):
        h = _h(*work, cycles=cycles)
        assert habits.brief_card_shown(h) is offered
        (row,) = _rows(_table(rework.build_section(h), "rework_causes"))
        assert ("/cg-brief" in row["try"], row["paste"] == "/cg-brief") == (offered, offered)


def test_where_you_said_claude_missed_it_picks_the_try_line():
    for word, line in catalogue.MISSED_IN_LINES.items():
        feedback = Feedback(source="answers", why=("missed",), missed_in=word or None)
        work = _pieces(fixes=[{"human_correction": True}], feedback=feedback)
        (row,) = _rows(_table(rework.build_section(_h(*work)), "rework_causes"))
        assert (row["cause"], row["try"]) == ("missed", line)
        assert row["paste"].startswith("Before you finish, re-read my request")
        if word:
            assert habits._MISSED_PLACES[word] in row["detail"]
    # What the tags alone say gives no place.
    tagged = _pieces(fixes=[{"tag": _tag(shift="fix", why="missed")}])
    (row,) = _rows(_table(rework.build_section(_h(*tagged)), "rework_causes"))
    assert row["try"] == catalogue.MISSED_IN_LINES[""] and "Most were missed in" not in row["detail"]


def test_a_correction_nothing_explains_reads_cause_not_reported_never_claude_got_it_wrong():
    work = _pieces(fixes=[{"human_correction": True}, {"queued_correction": True, "human_go": True, "human_prompt_chars": 3}])
    section = rework.build_section(_h(*work))
    (row,) = _rows(_table(section, "rework_causes"))
    assert (row["cause"], row["source"], row["cycles"]) == ("not_reported", "inferred", 2)
    assert row["try"] == "Run /cg-feedback after a piece of work to say why it needed changes."
    assert row["paste"] == "/cg-feedback"
    labels = helptext.TABLE_COPY["rework_causes"].value_labels
    assert labels["not_reported"] == "Cause not reported"
    assert not any("got it wrong" in str(cell).lower() for r in section.tables[1].rows for cell in r)


def test_the_left_out_card_cites_the_follow_ups_your_answers_called_things_left_out():
    work = _pieces(fixes=[{"tag": _tag(shift="fix", why="left_out")}])
    answered = [
        _gave(why=("left_out",), followups=4 if n < 2 else 2) for n in range(habits.MIN_FEEDBACK_ANSWERS)
    ]
    cited = _rows(_table(rework.build_section(_h(*work, pieces=answered)), "rework_causes"))
    assert cited[0]["detail"].endswith(
        f"{sum(p.followups for p in answered)} of {sum(p.followups for p in answered)} follow-ups were things "
        "your request left out."
    )
    # Fewer answers than the habits' own floor cite nothing.
    quiet = _rows(_table(rework.build_section(_h(*work, pieces=answered[:-1])), "rework_causes"))
    assert quiet[0]["detail"] == "1 follow-up in 1 piece."


def test_fixes_after_a_plan_show_as_a_card_only_once_enough_plans_were_approved():
    work = _pieces(fixes=[{"human_correction": True}])
    plans = [habits.PlanFix(shape="plan_build", typed=3, cost=0.5) for _ in range(habits.MIN_GROUP)]
    section = rework.build_section(_h(*work, plan_fixes=plans))
    row = _rows(_table(section, "rework_causes"))[-1]
    assert (row["cause"], row["source"], row["pieces"], row["cycles"]) == (rework.PLAN_FIXES, "inferred", 5, 15)
    assert row["detail"] == "5 of 5 plans you approved needed 3 or more fixes after approval."
    assert row["try"].startswith("Add details like these to the plan before approving")
    assert row["cost"] == pytest.approx(2.5)
    few = rework.build_section(_h(*work, plan_fixes=plans[:-1]))
    assert rework.PLAN_FIXES not in [r["cause"] for r in _rows(_table(few, "rework_causes"))]
    unfixed = [habits.PlanFix(shape="plan_build", typed=1) for _ in range(habits.MIN_GROUP)]
    assert rework.PLAN_FIXES not in [
        r["cause"] for r in _rows(_table(rework.build_section(_h(*work, plan_fixes=unfixed)), "rework_causes"))
    ]


def test_the_causes_cost_what_their_cycles_cost_and_carry_their_tokens(pricing):
    work = _pieces(
        fixes=[{"tag": _tag(shift="fix", why="tools")}, {"human_correction": True}, {"human_correction": True}],
        rates=pricing,
    )
    rows = {r["cause"]: r for r in _rows(_table(rework.build_section(_h(*work)), "rework_causes"))}
    assert rows["tools"]["cost"] == pytest.approx(REPLY) and rows["tools"]["tokens"] == 150
    assert rows["not_reported"]["cost"] == pytest.approx(2 * REPLY) and rows["not_reported"]["tokens"] == 300
    assert sum(r["share"] for r in rows.values()) == pytest.approx(100.0)


# -- Claude's admitted mistakes ----------------------------------------------------------


def _admitting(**kw):
    return {"tag": _tag(shift="fix", admit=kw.pop("word", "claim")), "admit_candidate": True, **kw}


def test_admitted_mistakes_read_as_the_plans_sentence(pricing):
    cycles = [
        _build(0),
        _fix(10, human_correction=True, admit_caught="user", **_admitting(word="instruction")),
        _fix(20, human_correction=True, admit_caught="user", **_admitting()),
        _fix(30, admit_caught="self", **_admitting()),
    ]
    work = pieces.pieces_of(cycles, rates=pricing)
    section = rework.build_section(_h(*work))
    (row,) = _rows(_table(section, "rework_admitted"))
    amount = Units().money(3 * REPLY, period=PERIOD).phrase()
    assert row["item"] == "admitted"
    assert row["text"] == (
        "Claude admitted 3 mistakes in 1 piece: you caught 2, it caught 1 itself. 1 was an instruction it had "
        f"been given. The rework after the ones you caught cost {amount}."
    )
    assert (row["count"], row["pieces"], row["user"], row["itself"], row["instruction"]) == (3, 1, 2, 1, 1)
    assert row["cost"] == pytest.approx(3 * REPLY)
    assert row["fix"] == catalogue.MISSED_IN_LINES[""] and row["period"] == PERIOD


def test_the_admissions_sentence_drops_what_has_nothing_to_say():
    cycles = [_build(0), _msg(10, files=("c",), human_prompt_chars=400, tag=_tag(admit="claim"), admit_candidate=True)]
    (row,) = _rows(_table(rework.build_section(_h(*pieces.pieces_of(cycles))), "rework_admitted"))
    assert row["text"] == "Claude admitted 1 mistake in 1 piece: you caught 0, it caught 1 itself."
    unpriced = pieces.pieces_of(
        [_build(0), _fix(10, human_correction=True, admit_caught="user", **_admitting())]
    )
    (row,) = _rows(_table(rework.build_section(_h(*unpriced)), "rework_admitted"))
    assert row["text"].endswith(" The rework after the ones you caught was not priced.")


def test_where_you_said_claude_missed_it_picks_the_admissions_fix_line():
    for word in ("message", "plan", "standing", "earlier"):
        work = pieces.pieces_of(
            [_build(0), _fix(10, **_admitting(admit_caught="user"))],
            feedback=Feedback(source="answers", why=("missed",), missed_in=word),
        )
        (row,) = _rows(_table(rework.build_section(_h(*work)), "rework_admitted"))
        assert row["fix"] == catalogue.MISSED_IN_LINES[word]


def test_a_possible_admission_is_said_apart_and_never_in_a_total():
    possible = _msg(10, files=("c",), human_prompt_chars=400)
    possible.facts = {"admit_candidate": True}
    work = pieces.pieces_of([_build(0), possible, _msg(20, files=("d",), human_prompt_chars=400, tag=_tag(admit="claim"))])
    rows = {r["item"]: r for r in _rows(_table(rework.build_section(_h(*work)), "rework_admitted"))}
    assert rows["possible"]["text"] == (
        "1 more reply reads like an admission that nothing confirmed. It is not counted above."
    )
    assert rows["admitted"]["count"] == 1 and rows["admitted"]["text"].startswith("Claude admitted 1 mistake in 1 piece")
    only_possible = pieces.pieces_of([_build(0), possible])
    rows = _rows(_table(rework.build_section(_h(*only_possible)), "rework_admitted"))
    # With nothing confirmed above it, it does not say "more" or point above.
    assert [(r["item"], r["text"]) for r in rows] == [
        ("possible", "1 reply reads like an admission that nothing confirmed. It is not counted.")
    ]
    assert rework.possible_text(3).startswith("3 more replies read like an admission")
    assert rework.possible_text(3, counted=False) == (
        "3 replies read like an admission that nothing confirmed. They are not counted."
    )


def test_no_admission_and_nothing_possible_leaves_the_table_empty():
    assert _table(rework.build_section(_h(*_pieces(fixes=[{"human_correction": True}]))), "rework_admitted").rows == []


# -- by week ---------------------------------------------------------------------------


def test_a_week_gets_a_share_only_with_five_pieces_that_needed_changes():
    week = [_week_piece(0, reworked=True) for _ in range(rework.MIN_WEEK_REWORKED)]
    week += [_week_piece(0, reworked=False) for _ in range(5)]
    week += [_week_piece(1, reworked=True) for _ in range(rework.MIN_WEEK_REWORKED - 1)]
    rows = _rows(_table(rework.build_section(_h(*week)), "rework_by_week"))
    assert [r["week"] for r in rows] == ["2026-09-14", "2026-09-21"]
    assert [(r["pieces"], r["reworked"], r["share"]) for r in rows] == [(10, 5, 50.0), (4, 4, None)]
    assert rows[0]["cost"] == 0.0


def test_a_week_with_no_piece_between_two_with_one_keeps_its_place_as_a_dash():
    work = [_week_piece(0, reworked=False), _week_piece(2, reworked=False)]
    rows = _rows(_table(rework.build_section(_h(*work)), "rework_by_week"))
    assert [(r["week"], r["pieces"], r["share"], r["cost"]) for r in rows] == [
        ("2026-09-14", 1, None, 0.0),
        ("2026-09-21", 0, None, None),
        ("2026-09-28", 1, None, 0.0),
    ]


def test_weeks_are_the_local_monday_of_the_zone_you_set():
    # Monday 21 September 00:30 UTC is still Sunday afternoon at UTC-10.
    late = _week_piece(0, reworked=False, base=BASE + timedelta(days=2, hours=15, minutes=30))
    assert late.end_ts.startswith("2026-09-21T0")
    honolulu = _rows(_table(rework.build_section(_h(late, tz=timezone(timedelta(hours=-10)))), "rework_by_week"))
    utc = _rows(_table(rework.build_section(_h(late, tz="UTC")), "rework_by_week"))
    assert [r["week"] for r in honolulu] == ["2026-09-14"]
    assert [r["week"] for r in utc] == ["2026-09-21"]


def test_mistakes_you_caught_per_piece_need_five_tagged_pieces_and_never_read_zero_before_capture():
    tag = _tag(task="feature", level="normal")
    week = [_week_piece(0, reworked=False, tag=tag) for _ in range(rework.MIN_WEEK_PIECES - 1)]
    caught = pieces.pieces_of([
        _msg(0, files=("a", "b"), human_prompt_chars=400, tag=tag),
        _fix(10, human_correction=True, admit_caught="user", **_admitting()),
        _msg(60, files=("z",), human_prompt_chars=400, tag=_tag(shift="new")),
    ])[0]
    # The week before capture was on has pieces, but no tag, so no figure.
    before = [_week_piece(-1, reworked=False) for _ in range(6)]
    rows = _rows(_table(rework.build_section(_h(*before, *week, caught)), "rework_by_week"))
    assert [(r["week"], r["caught"], r["caught_per_piece"]) for r in rows] == [
        ("2026-09-07", None, None),
        ("2026-09-14", 1, pytest.approx(1 / 5)),
    ]
    short = rework.build_section(_h(*week))
    assert _rows(_table(short, "rework_by_week"))[0]["caught_per_piece"] is None
    assert _rows(_table(short, "rework_by_week"))[0]["caught"] == 0


def test_the_mistakes_you_caught_by_week_count_every_piece_as_the_admitted_line_does():
    loose = pieces.WorkPiece(
        delivered=True, unsegmented=True, task="feature", admitted=1, admitted_user=1,
        start_ts="2026-09-21T09:00:00.000Z", end_ts="2026-09-21T10:00:00.000Z",
    )
    found = rework.collect(_h(loose))
    (week,) = found.weeks
    assert (week.pieces, week.tagged, week.caught) == (0, 1, 1)
    assert found.admitted.user == sum(w.caught for w in found.weeks)


# -- by level --------------------------------------------------------------------------


def test_rework_by_level_is_per_request_with_the_words_in_the_catalogues_order():
    hard = _only([_msg(0, files=("a", "b"), human_prompt_chars=400, tag=_tag(level="hard")), _fix(10, human_correction=True, tag=_tag(level="hard", shift="fix")), _msg(20, files=("c",), human_prompt_chars=400, tag=_tag(level="hard"))])
    easy = _only([_msg(0, files=("a", "b"), human_prompt_chars=400, tag=_tag(level="easy"))])
    untagged = _only([_build(0), _fix(10, human_correction=True)])
    rows = _rows(_table(rework.build_section(_h(untagged, hard, easy)), "rework_by_level"))
    assert [r["level"] for r in rows] == ["easy", "hard", "unknown"]
    by = {r["level"]: r for r in rows}
    assert (by["hard"]["requests"], by["hard"]["rework"]) == (3, 1)
    assert "pieces" not in by["hard"]
    assert by["hard"]["rate"] == pytest.approx(100 / 3) and by["easy"]["rate"] == 0.0
    assert by["unknown"]["rate"] == pytest.approx(50.0)
    assert set(by) <= {*catalogue.TAG_VOCAB["level"], pieces.UNKNOWN}


# -- the section -----------------------------------------------------------------------


def test_an_empty_corpus_keeps_the_sections_shape():
    section = rework.build_section(habits.Habits())
    assert [t.name for t in section.tables] == [
        "rework_headline", "rework_causes", "rework_admitted", "rework_by_week", "rework_by_level",
    ]
    assert all(t.rows == [] for t in section.tables)
    assert section.notes and _table(section, "rework_headline").title == "Rework after delivery"


def test_a_transcript_goes_through_habits_into_the_section(tmp_path, pricing):
    h = habits.collect(_corrected_session(tmp_path), pricing, window=WINDOW)
    assert len(h.work_pieces) == 1 and h.work_pieces[0].rework == 3
    section = rework.build_section(h)
    rows = {r["item"]: r for r in _rows(_table(section, "rework_headline"))}
    assert rows["requests"]["count"] == 3 and rows["requests"]["total"] == 4
    assert rows["unknown"]["share"] == 100.0
    assert rows["requests"]["cost"] == pytest.approx(h.work_pieces[0].rework_cost) and rows["requests"]["cost"] > 0
    assert _rows(_table(section, "rework_causes"))[0]["cause"] == "not_reported"
    assert_privacy(section)


def test_a_section_holds_counts_closed_words_and_amounts_only():
    work = _pieces(fixes=[{"tag": _tag(shift="fix", why="missed", admit="claim"), "admit_candidate": True}])
    section = rework.build_section(_h(*work))
    allowed = {
        *pieces.CAUSES, *pieces.SOURCES, *catalogue.TAG_VOCAB["level"], rework.PLAN_FIXES, pieces.UNKNOWN,
        "pieces", "unknown", "requests", "admitted", "possible", PERIOD, "",
    }
    free_text = {"text", "detail", "try", "paste", "fix", "week"}
    for table in section.tables:
        keys = [c.key for c in table.columns]
        for row in table.rows:
            for key, cell in zip(keys, row):
                assert cell is None or isinstance(cell, (int, float)) or key in free_text or cell in allowed, (key, cell)
    assert not any(isinstance(cell, str) and ("src/" in cell or "\\" in cell) for t in section.tables for r in t.rows for cell in r)

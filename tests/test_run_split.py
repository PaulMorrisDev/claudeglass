"""Splitting long subagent runs (``run_split.py``): what long runs would
have cost as several shorter runs, each starting fresh from a short note,
and the split interval that saves most per agent type.

Hand-built turns use the packaged Sonnet 5 rates (output 10, cache write
2.5 and cache read 0.2 per million tokens), as ``test_handoff.py`` does.
"""

from __future__ import annotations

import re

import pytest

from claudeglass import model
from claudeglass.model import EventKind, ReportModel, TranscriptMeta, TranscriptResult
from claudeglass.pricing import load_pricing
from claudeglass.run_split import RULES, RunSplitThresholds, build_section, compute_run_split

PRICING = load_pricing()

#: One interval, so each test prices a single split.
TEN = RunSplitThresholds(intervals=(10,))


def _turn(i: int, **overrides) -> model.Turn:
    fields = dict(message_id=f"msg_{i}", request_id=f"req_{i}", turn_index=i, model="claude-sonnet-5")
    fields.update(overrides)
    return model.Turn(**fields)


def _run(
    *,
    replies=15,
    ctx=100_000,
    growth=0,
    agent_type="claude-implementer",
    agent_id="agent-a1",
    session_id="s1",
    tool_use_id=None,
    compaction_at=None,
    kind="subagent",
    workflow_run_id=None,
) -> TranscriptResult:
    """A subagent run that starts at 10,000 tokens, then reads ``ctx``
    tokens from the cache on its second reply, ``growth`` more on each
    after."""
    turns = [_turn(1, ctx=10_000, cache_read_tokens=10_000)]
    for i in range(2, replies + 1):
        kinds = (EventKind.COMPACT_BOUNDARY,) if compaction_at == i else ()
        read = ctx + growth * (i - 2)
        turns.append(_turn(i, ctx=read, cache_read_tokens=read, preceding_event_kinds=kinds))
    meta = TranscriptMeta(
        session_id=session_id,
        kind=kind,
        agent_type=agent_type,
        agent_id=agent_id,
        tool_use_id=tool_use_id,
        workflow_run_id=workflow_run_id,
    )
    return TranscriptResult(meta=meta, turns=turns)


def _parent(later=5, session_id="s1") -> TranscriptResult:
    """A main session whose second reply starts the subagent (``toolu_1``)
    and whose ``later`` replies after it read 50,000 tokens each."""
    turns = [
        _turn(1, ctx=20_000, cache_read_tokens=20_000),
        _turn(2, ctx=30_000, cache_read_tokens=30_000, tool_use_ids=("toolu_1",)),
    ]
    turns += [_turn(3 + n, ctx=50_000, cache_read_tokens=50_000) for n in range(later)]
    return TranscriptResult(meta=TranscriptMeta(session_id=session_id, kind="top-level"), turns=turns)


# One split at the 11th reply: the fresh start is 10,000 + 2,000 tokens, so
# 88,000 dropped from each of the last 5 replies. Less the note written
# (2,000 output tokens), one cache write of the fresh start in place of a
# read, and the allowance.
_GROSS = 5 * 88_000 * 0.2e-6
_NOTE = 2_000 * 10e-6
_FRESH = 12_000 * (2.5 - 0.2) * 1e-6


def test_a_split_drops_the_context_before_it_less_what_it_adds_back():
    stats = compute_run_split([_run()], PRICING, TEN, rediscovery_allowance_usd=0.01)
    row = stats.agents["claude-implementer"]
    at = row.by_interval[10]
    assert (at.long_runs, at.splits, at.dropped, at.replies) == (1, 1, [88_000], [15])
    assert at.saving_usd == pytest.approx(_GROSS - _NOTE - _FRESH - 0.01)
    assert row.runs == 1 and row.longest_run == 15
    assert stats.no_parent_runs == 1


def test_the_parent_pays_for_writing_the_note_and_keeping_it():
    alone = compute_run_split([_run(tool_use_id="toolu_1")], PRICING, TEN)
    stats = compute_run_split([_parent(), _run(tool_use_id="toolu_1")], PRICING, TEN)
    # The parent writes 2,000 more output tokens, then keeps the note and
    # the brief (4,000 tokens) through its 5 later replies.
    parent_share = 2_000 * 10e-6 + 4_000 * 5 * 0.2e-6
    saving = stats.agents["claude-implementer"].by_interval[10].saving_usd
    assert saving == pytest.approx(alone.agents["claude-implementer"].by_interval[10].saving_usd - parent_share)
    assert stats.no_parent_runs == 0


def test_a_short_run_is_counted_but_never_split():
    stats = compute_run_split([_run(replies=10)], PRICING, TEN)
    row = stats.agents["claude-implementer"]
    assert row.runs == 1 and row.by_interval == {}
    assert row.agent_usd > 0


def test_a_conversation_summary_starts_the_count_again():
    stats = compute_run_split([_run(replies=15, compaction_at=6)], PRICING, TEN)
    # The count restarts at the 6th reply, so no split falls in the run.
    assert stats.agents["claude-implementer"].by_interval == {}


def test_a_split_that_drops_little_does_not_count():
    stats = compute_run_split([_run(ctx=25_000)], PRICING, TEN)
    # 25,000 - 12,000 = 13,000 dropped, under the 20,000 minimum.
    assert stats.agents["claude-implementer"].by_interval == {}


def test_workflow_agents_and_main_sessions_are_not_split():
    # Discovery gives an agent under a workflow run's folder its own kind,
    # and keeps the agent type it was started as.
    workflow = _run(kind="workflow-agent", workflow_run_id="wf-1")
    stats = compute_run_split([workflow, _parent()], PRICING, TEN)
    assert stats.agents == {}


def test_the_best_interval_saves_most_among_those_with_enough_runs():
    runs = [_run(replies=40, growth=10_000, agent_id=f"agent-{n}") for n in range(3)]
    th = RunSplitThresholds(intervals=(5, 10, 30))
    stats = compute_run_split(runs, PRICING, th, rediscovery_allowance_usd=0.3)
    row = stats.agents["claude-implementer"]
    # Every 5 replies pays the $0.30 allowance 7 times a run; every 30
    # splits once, late. Every 10 saves most.
    assert row.by_interval[5].saving_usd < 0
    best = row.best(th.min_runs)
    assert best is not None and best.every_n == 10
    assert best.saving_usd == max(s.saving_usd for s in row.by_interval.values())
    assert row.best(4) is None


def test_the_section_reports_each_agent_types_best_interval_and_the_sweep():
    runs = [_run(replies=40, growth=10_000, agent_id=f"agent-{n}") for n in range(3)]
    runs.append(_run(replies=8, agent_type="Explore", agent_id="agent-x"))
    th = RunSplitThresholds(intervals=(5, 10, 30))
    stats = compute_run_split(runs, PRICING, th, rediscovery_allowance_usd=0.3)
    section = build_section(stats, th)
    tables = {t.name: t for t in section.tables}
    assert list(tables) == ["run_split_summary", "run_split_by_agent", "run_split_sweep"]

    def rows(name):
        table = tables[name]
        return [{c.key: v for c, v in zip(table.columns, row)} for row in table.rows]

    [summary] = rows("run_split_summary")
    assert summary["runs"] == 4 and summary["paying_agents"] == 1 and summary["long_runs"] == 3
    implementer, explore = rows("run_split_by_agent")
    assert implementer["agent_type"] == "claude-implementer" and implementer["every_n"] == 10
    assert implementer["saving_usd"] == pytest.approx(summary["saving_usd"])
    assert explore["every_n"] is None and explore["saving_usd"] == 0.0 and explore["long_runs"] == 0
    sweep = rows("run_split_sweep")
    assert [r["interval"] for r in sweep] == ["every 5 replies", "every 10 replies", "every 30 replies"]
    assert sweep[0]["net_usd"] < 0
    assert [r["best_for"] for r in sweep] == [0, 1, 0]


def _report(stats, th) -> ReportModel:
    return ReportModel(meta=None, sections=[build_section(stats, th)])


def test_the_rule_suggests_splitting_at_the_best_interval():
    th = RunSplitThresholds(intervals=(10,))
    runs = [_run(replies=40, agent_id=f"agent-{n}") for n in range(3)]
    stats = compute_run_split(runs, PRICING, th)
    [rec] = RULES[0](_report(stats, th), th)
    assert rec.id == "run-split" and rec.severity == "advice" and rec.agent_type == "claude-implementer"
    assert rec.lever is None and rec.changes == []
    assert "every 10 replies" in rec.why and "about 10 replies' worth" in rec.action
    assert rec.saving_usd == pytest.approx(stats.agents["claude-implementer"].by_interval[10].saving_usd)
    assert {e[2] for e in rec.evidence} == {"run_split.run_split_by_agent"}


def test_the_rule_needs_enough_runs_and_a_large_enough_share():
    runs = [_run(replies=40, agent_id=f"agent-{n}") for n in range(2)]
    stats = compute_run_split(runs, PRICING, TEN)
    assert RULES[0](_report(stats, TEN), TEN) == []
    runs.append(_run(replies=40, agent_id="agent-3"))
    stats = compute_run_split(runs, PRICING, TEN)
    strict = RunSplitThresholds(intervals=(10,), min_saving_share_pct=99.0)
    assert RULES[0](_report(stats, strict), strict) == []


def test_thresholds_come_from_config():
    th = RunSplitThresholds.from_config(
        {
            "thresholds": {
                "run_split_intervals": [200, 40, 40, 1, "x"],
                "run_split_note_tokens": 500,
                "run_split_min_runs": "5",
            }
        }
    )
    assert th.intervals == (40, 200) and th.note_tokens == 500 and th.min_runs == 5
    assert RunSplitThresholds.from_config({"run_split_intervals": []}).intervals == RunSplitThresholds().intervals
    assert any("every 40, 200 replies" in line for line in th.describe())


# -- current settings only -----------------------------------------------------

#: The auto-compact window in force; a run whose context grew past it ran
#: under an older, larger setting.
WINDOW = 300_000


def test_a_run_that_peaked_above_the_current_window_is_left_out():
    older = _run(replies=40, growth=10_000, agent_id="older")  # peaks at 480,000
    stats = compute_run_split([older, _run(replies=40, agent_id="now")], PRICING, TEN, current_window=WINDOW)
    row = stats.agents["claude-implementer"]
    assert row.runs == 1 and row.longest_run == 40
    assert row.by_interval[10].long_runs == 1 and row.by_interval[10].replies == [40]
    assert (stats.older_runs, stats.windows) == (1, {WINDOW})
    kept = compute_run_split([_run(replies=40, agent_id="now")], PRICING, TEN, current_window=WINDOW)
    assert row.agent_usd == pytest.approx(kept.agents["claude-implementer"].agent_usd)
    alone = compute_run_split([older], PRICING, TEN)
    assert stats.older_usd == pytest.approx(alone.agents["claude-implementer"].agent_usd)


def test_a_run_that_peaked_at_the_window_still_counts():
    at_the_window = _run(replies=40, ctx=WINDOW, agent_id="edge")
    stats = compute_run_split([at_the_window], PRICING, TEN, current_window=WINDOW)
    assert stats.older_runs == 0 and stats.agents["claude-implementer"].runs == 1
    over = _run(replies=40, ctx=WINDOW + 1, agent_id="over")
    stats = compute_run_split([over], PRICING, TEN, current_window=WINDOW)
    assert stats.older_runs == 1 and stats.agents == {}


def test_with_no_window_known_every_run_counts():
    runs = [_run(replies=40, growth=10_000, agent_id=f"agent-{n}") for n in range(3)]
    for unknown in (None, lambda tr: None):
        stats = compute_run_split(runs, PRICING, TEN, current_window=unknown)
        assert stats.older_runs == 0 and stats.agents["claude-implementer"].runs == 3


def test_the_window_can_differ_from_one_run_to_the_next():
    small = _run(replies=40, ctx=150_000, agent_id="small-window")
    large = _run(replies=40, ctx=150_000, agent_id="large-window")
    stats = compute_run_split(
        [small, large], PRICING, TEN,
        current_window=lambda tr: 100_000 if tr.meta.agent_id == "small-window" else 400_000,
    )
    assert stats.older_runs == 1 and stats.windows == {100_000}
    assert stats.agents["claude-implementer"].runs == 1


def test_the_card_goes_quiet_when_every_long_run_predates_the_window_and_returns_for_one_under_it():
    th = RunSplitThresholds(intervals=(10,))
    old_runs = [_run(replies=40, growth=10_000, agent_id=f"old-{n}") for n in range(3)]
    # Before: they earn the card.
    assert len(RULES[0](_report(compute_run_split(old_runs, PRICING, th), th), th)) == 1
    # Under the window now in force they were never possible, so it goes quiet.
    stats = compute_run_split(old_runs, PRICING, th, current_window=WINDOW)
    assert stats.agents == {} and stats.older_runs == 3
    assert RULES[0](_report(stats, th), th) == []
    # Three long runs that fit the window still earn it.
    now_runs = [_run(replies=40, agent_id=f"now-{n}") for n in range(3)]
    stats = compute_run_split(old_runs + now_runs, PRICING, th, current_window=WINDOW)
    [rec] = RULES[0](_report(stats, th), th)
    assert rec.agent_type == "claude-implementer"
    assert rec.saving_usd == pytest.approx(compute_run_split(now_runs, PRICING, th).agents["claude-implementer"].by_interval[10].saving_usd)


def test_the_section_says_how_many_runs_ran_under_an_older_setting():
    runs = [_run(replies=40, growth=10_000, agent_id="older"), _run(replies=40, agent_id="now")]
    stats = compute_run_split(runs, PRICING, TEN, current_window=WINDOW)
    section = build_section(stats, TEN)
    summary = next(t for t in section.tables if t.name == "run_split_summary")
    row = {c.key: v for c, v in zip(summary.columns, summary.rows[0])}
    assert row["runs"] == 1 and row["older_runs"] == 1 and row["older_usd"] == pytest.approx(stats.older_usd)
    assert any(
        n.startswith("1 run grew past the auto-compact window") and "300,000" in n and "older setting" in n
        for n in section.notes
    )
    assert not any("$" in n for n in section.notes if "grew past" in n)
    clean = build_section(compute_run_split(runs, PRICING, TEN), TEN)
    assert not any(re.match(r"\d+ runs? grew past", n) for n in clean.notes)

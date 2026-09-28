"""Work habits (``habits.py``): the facts worked out per message and per
agent run, the playbook of habits worth trying, how each habit's trend
and confidence are judged, and the tables the Work habits tab shows.

Amounts in the transcript tests use ``tests/fixtures/pricing_min.toml``'s
``claude-widget-9``; the playbook tests build the facts directly, so
each saving can be worked out by hand.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from claudeglass import capture as capture_mod, capture_catalogue as catalogue, habits, parse
from claudeglass.habits import AgentFact, CycleFact, Habits, Item, Piece
from claudeglass.model import CaptureTag, Recommendation, TranscriptMeta
from claudeglass.parse import parse_transcript
from claudeglass.pricing import load_pricing

from helpers import (
    old_agent_note_text,
    assert_privacy,
    attachment_line,
    tool_result_block,
    tool_use_block,
    turn_line,
    user_block_line,
    user_str_line,
    write_jsonl,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MODEL = "claude-widget-9"
WEEKS = ["2026-08-03", "2026-08-10", "2026-08-17", "2026-08-24", "2026-08-31"]


@pytest.fixture(autouse=True)
def _salt():
    parse.set_salt(b"h" * 32)


@pytest.fixture()
def pricing():
    return load_pricing(path=FIXTURES / "pricing_min.toml")


def _cycle(week: str = WEEKS[0], cost: float = 1.0, tag: CaptureTag | None = None, session_id: str = "s1",
           **kw) -> CycleFact:
    ts = datetime.fromisoformat(week).replace(tzinfo=timezone.utc) if week else None
    return CycleFact(session_id=session_id, ts=ts, week=week, cost=cost, turns=1, tag=tag, **kw)


def _agent(**kw) -> AgentFact:
    return AgentFact(**{"session_id": "s1", "agent_type": "general-purpose", "week": WEEKS[0], "cost": 1.0, **kw})


def _by_key(items) -> dict[str, Item]:
    return {item.key: item for item in items}


def _rows(table) -> list[dict]:
    return [{c.key: v for c, v in zip(table.columns, row)} for row in table.rows]


def _table(section, name):
    return next(t for t in section.tables if t.name == name)


# -- trend and confidence ---------------------------------------------------------


def _weekly(waste_per_message: list[float | None]) -> tuple[Habits, Item]:
    """Three messages a week (one where the rate is ``None``, so the week
    is too thin to count), and an item addressing ``rate`` USD of each."""
    h = Habits()
    waste = {}
    for week, rate in zip(WEEKS, waste_per_message):
        h.cycles.extend(_cycle(week) for _ in range(habits.TREND_MIN_CYCLES if rate is not None else 1))
        waste[week] = 3 * (rate or 0.0)
    return h, Item("tool_loops", 1.0, 1, ("inferred",), "", waste=waste)


def test_a_steady_fall_is_improving_and_prices_what_the_habit_already_saves():
    h, item = _weekly([1.0, 1.0, 0.1, 0.1])
    word, weeks, adopted = habits.trend(h, item)
    assert (word, weeks) == ("falling", "100 100 10 10")
    # (1.0 - 0.1) a message, over the three messages a recent week holds.
    assert adopted == pytest.approx(0.9 * 3)


def test_a_fall_seen_over_too_few_weeks_prices_nothing_yet():
    h, item = _weekly([1.0, 0.1, 0.1])
    assert habits.TREND_MIN_ADOPTED == 4
    assert habits.trend(h, item) == ("falling", "100 10 10", 0.0)


@pytest.mark.parametrize(
    "rates, word",
    [
        ([0.1, 0.1, 1.0, 1.0], "rising"),
        ([0.5, 0.5, 0.5, 0.5], "steady"),
        # The latest week back above the earlier level isn't a fall.
        ([1.0, 1.0, 0.1, 0.1, 1.2], "steady"),
        ([1.0, 0.1], "new"),
    ],
)
def test_trend_words(rates, word):
    h, item = _weekly(rates)
    assert habits.trend(h, item)[0] == word


def test_a_thin_week_is_a_dash_and_does_not_count():
    h, item = _weekly([1.0, None, 1.0, 0.1, 0.1])
    word, weeks, _ = habits.trend(h, item)
    assert weeks == "100 - 100 10 10" and word == "falling"


@pytest.mark.parametrize(
    "n, sources, level",
    [
        (20, ("reported",), "high"),
        (20, ("inferred",), "medium"),
        (20, ("reported", "inferred"), "high"),
        (8, ("your feedback",), "medium"),
        (7, ("reported",), "low"),
    ],
)
def test_confidence_rises_with_evidence_and_inference_alone_never_reaches_high(n, sources, level):
    assert habits.confidence(Item("tool_loops", None, n, sources, "")) == level


# -- the playbook ---------------------------------------------------------------------


def test_the_playbook_puts_the_largest_saving_first_and_unpriced_habits_last():
    easy = CaptureTag(level="easy")
    h = Habits(cycles=[
        # 5 messages (the shared effort threshold, UX-3) with thinking well
        # over the shared 30% share gate (0.2 of 0.3 output = 66.7%).
        *(_cycle(tag=easy, effort="high", thinking_cost=0.2, output_cost=0.3) for _ in range(5)),
        _cycle(loops=1, loop_cost=1.0),
        *(_cycle(tag=CaptureTag(skill="unneeded"), skill_calls=[("lint", False, 0, 0.0)]) for _ in range(2)),
    ])
    items = habits.playbook(h)
    assert [i.key for i in items] == ["tool_loops", "effort_fit", "skill_unneeded"]
    loops, effort, skill = items
    assert loops.saving == pytest.approx(1.0) and loops.sources == ("inferred",)
    # Half the thinking on each easy ask at high effort.
    assert effort.saving == pytest.approx(5 * 0.1) and effort.sources == ("reported",)
    assert skill.saving is None and "(lint)" in skill.evidence


def test_effort_fit_uses_the_same_message_count_and_share_gate_as_effort_mismatch():
    """UX-3: ``effort_fit`` is gated the same way as the ``effort-mismatch``
    rule it's ``COVERED_BY`` -- ``_EFFORT_MIN_MESSAGES`` messages and more
    than the configured thinking-share percent, not the old bare
    ``len(easy) < 3`` count with no share check at all."""
    easy = CaptureTag(level="easy")

    def _easy_cycles(n, thinking_cost=0.2, output_cost=0.3):
        return [_cycle(tag=easy, effort="high", thinking_cost=thinking_cost, output_cost=output_cost) for _ in range(n)]

    # Below the shared message-count floor, even with a high share.
    below_count = Habits(cycles=_easy_cycles(habits._EFFORT_MIN_MESSAGES - 1))
    assert "effort_fit" not in _by_key(habits.playbook(below_count))

    # At the message-count floor but the thinking share doesn't clear the
    # gate (0.1 of 1.0 output = 10%, under the default 30%).
    below_share = Habits(cycles=_easy_cycles(habits._EFFORT_MIN_MESSAGES, thinking_cost=0.1, output_cost=1.0))
    assert "effort_fit" not in _by_key(habits.playbook(below_share))

    # Both gates cleared: fires, at the class default 30% threshold.
    fires = Habits(cycles=_easy_cycles(habits._EFFORT_MIN_MESSAGES))
    assert "effort_fit" in _by_key(habits.playbook(fires))

    # A configured (non-default) threshold, resolved via
    # ``Habits.effort_share_threshold_pct``, is honoured too: a share that
    # clears 30% but not a stricter 70% configured gate doesn't fire.
    stricter = Habits(cycles=_easy_cycles(habits._EFFORT_MIN_MESSAGES), effort_share_threshold_pct=70.0)
    assert "effort_fit" not in _by_key(habits.playbook(stricter))


def test_large_asks_count_whether_reported_or_seen_and_say_which():
    h = Habits(cycles=[
        _cycle(tag=CaptureTag(size="xl"), cost=5.0, growth_cost=2.0),
        _cycle(cost=4.0, growth_cost=1.0, compactions=1, redone=True),
        _cycle(tag=CaptureTag(size="s"), cost=1.0, growth_cost=3.0),
    ])
    item = _by_key(habits.playbook(h))["split_large"]
    assert item.n == 2 and item.saving == pytest.approx(0.5 * 2.0 + 0.5 * 1.0)
    assert item.sources == ("reported", "inferred") and item.source == "reported + inferred"
    assert "1 were compacted part-way" in item.evidence and "1 had to be redone" in item.evidence


def test_a_new_task_on_old_context_is_worth_a_clear_unless_the_work_built_on_it():
    stale = habits.STALE_TOKENS
    h = Habits(cycles=[
        _cycle(tag=CaptureTag(shift="new"), stale_tokens=stale, stale_cost=0.4),
        _cycle(stale_tokens=stale, stale_cost=0.2, stale_rewrite=0.2, gap_s=habits.LONG_BREAK_S),
        _cycle(tag=CaptureTag(shift="build"), stale_tokens=stale, stale_cost=9.0, gap_s=habits.LONG_BREAK_S),
        _cycle(tag=CaptureTag(shift="new"), stale_tokens=stale - 1, stale_cost=9.0),
    ])
    item = _by_key(habits.playbook(h))["clear_between"]
    assert item.saving == pytest.approx(0.4 + 0.5 * (0.2 + 0.2))
    assert item.sources == ("reported", "inferred")


@pytest.mark.parametrize("shift", ["build", "grew", "redo", "fix"])
def test_work_that_carries_on_after_a_long_break_is_not_worth_a_clear(shift):
    stale = habits.STALE_TOKENS
    h = Habits(cycles=[
        _cycle(tag=CaptureTag(shift=shift), stale_tokens=stale, stale_cost=9.0, gap_s=habits.LONG_BREAK_S),
        _cycle(stale_tokens=stale, stale_cost=0.2, stale_rewrite=0.2, gap_s=habits.LONG_BREAK_S),
    ])
    item = _by_key(habits.playbook(h))["clear_between"]
    assert item.n == 1 and item.saving == pytest.approx(0.5 * (0.2 + 0.2))


def test_clear_between_names_how_often_you_cleared_explicitly():
    """SIG-2: end_reasons' "clear" count is session-level evidence, added
    to the evidence text alongside the message-level stale-context count
    -- it never changes the saving figure or n."""
    stale = habits.STALE_TOKENS
    h = Habits(
        cycles=[_cycle(tag=CaptureTag(shift="new"), stale_tokens=stale, stale_cost=0.4)],
        end_reasons=Counter({"clear": 2, "quit": 1}),
    )
    item = _by_key(habits.playbook(h))["clear_between"]
    assert item.saving == pytest.approx(0.4) and item.n == 1
    assert "you cleared explicitly 2 times" in item.evidence


def test_clear_between_says_nothing_about_clears_when_none_were_logged():
    stale = habits.STALE_TOKENS
    h = Habits(cycles=[_cycle(tag=CaptureTag(shift="new"), stale_tokens=stale, stale_cost=0.4)])
    item = _by_key(habits.playbook(h))["clear_between"]
    assert "cleared explicitly" not in item.evidence


def test_vague_asks_are_compared_with_clear_ones_of_the_same_kind():
    clear = CaptureTag(task="bugfix", brief="clear")
    vague = CaptureTag(task="bugfix", brief="vague", missing=("repro", "files"))
    h = Habits(cycles=[*(_cycle(tag=clear) for _ in range(3)), *(_cycle(tag=vague, cost=3.0) for _ in range(5))])
    item = _by_key(habits.playbook(h))["brief_clearly"]
    assert item.saving == pytest.approx(5 * 0.5 * (3.0 - 1.0)) and item.n == 5
    assert "costing 3.0x a clear ask of the same kind" in item.evidence
    assert "most often missing: reproduce, files" in item.evidence
    # The example is the line your asks most often lacked.
    assert item.example == catalogue.BRIEF_LINES["repro"][1]


def test_too_few_vague_asks_suggest_nothing():
    vague = CaptureTag(task="bugfix", brief="vague")
    h = Habits(cycles=[_cycle(tag=vague) for _ in range(habits.MIN_GROUP - 1)])
    assert "brief_clearly" not in _by_key(habits.playbook(h))


def test_long_agent_reports_that_were_not_capped_are_priced_down_to_a_short_one():
    h = Habits(agents=[
        _agent(report_tokens=4_000, report_carry=1.0),
        _agent(report_tokens=4_000, report_carry=1.0, capped=True),
        _agent(report_tokens=500, report_carry=0.1),
    ])
    item = _by_key(habits.playbook(h))["short_reports"]
    assert item.saving == pytest.approx(1.0 * (4_000 - habits.SHORT_REPORT_TOKENS) / 4_000)
    assert item.n == 1 and "33% of briefs asked for a short one" in item.evidence


def test_misses_you_reported_name_the_kind_of_work_and_what_slowed_it():
    h = Habits(pieces=[
        Piece("missed", 4.0, 2, "refactor", ("rework",), (), "your feedback"),
        Piece("stopped", 2.0, 1, "refactor", ("rework", "unclear"), (), "dashboard rating"),
        Piece("met", 1.0, 1, "bugfix", (), (), "your feedback"),
    ])
    item = _by_key(habits.playbook(h))["outcome_misses"]
    # P4 leftover: a missed/stopped piece has a real cost (Piece.cost), so
    # this gets a conservative floor -- half the cost of the misses,
    # not "not priced" -- unlike skill_unneeded, which has no per-call
    # cost figure to floor at all.
    assert item.sources == ("your feedback",) and item.saving == pytest.approx(3.0) and item.n == 2
    assert item.evidence == (
        "2 pieces of work missed their goal or were stopped, costing 3.0x one that met it; mostly refactor work. "
        "Slowed most by: wrong approach or rework."
    )


# -- tables -----------------------------------------------------------------------------


def test_every_table_is_there_even_with_nothing_to_show():
    section = habits.section_from(Habits())
    assert section.key == "habits" and section.notes == []
    assert [t.name for t in section.tables] == [
        "habits_digest",
        "habits_playbook",
        "habits_by_task",
        "habits_briefs",
        "habits_brief_templates",
        "habits_agents",
        "habits_effort_fit",
        "habits_setups",
        "habits_agents_by_task",
        "habits_outcomes",
        "habits_by_shape",
        "habits_self_report",
        "habits_prompt_flags",
        "habits_skills",
        "habits_tool_output",
    ]
    assert _table(section, "habits_playbook").notes


def test_untagged_unrated_work_says_how_to_get_more():
    notes = habits.section_from(Habits(cycles=[_cycle()])).notes
    assert any("turn on metrics capture" in n.lower() for n in notes)
    assert any("run /cg-feedback" in n for n in notes)


def test_span_weeks_does_not_stretch_a_single_day_into_a_fake_weekly_rate():
    """F3/UX-4/7: a corpus that spans under 7 days no longer divides by a
    fraction of a week (the old ``max(1, days) / 7`` multiplied a single
    day's total by about 7x); ``span_weeks`` is 1.0 until there's a full
    week, so a total divided by it is the raw total, not an extrapolation."""
    h = Habits(cycles=[_cycle()])  # one cycle, no ``ts`` spread at all
    assert h.span_days == 1.0
    assert h.span_weeks == 1.0

    two_days = Habits(cycles=[_cycle(WEEKS[0]), _cycle("2026-08-04")])
    assert two_days.span_days == pytest.approx(1.0)  # a day apart, under the 7-day floor
    assert two_days.span_weeks == 1.0

    full_week = Habits(cycles=[_cycle(WEEKS[0]), _cycle(WEEKS[1])])
    assert full_week.span_days == pytest.approx(7.0)
    assert full_week.span_weeks == pytest.approx(1.0)

    two_weeks = Habits(cycles=[_cycle(WEEKS[0]), _cycle(WEEKS[2])])
    assert two_weeks.span_days == pytest.approx(14.0)
    assert two_weeks.span_weeks == pytest.approx(2.0)


def test_the_digest_is_titled_with_the_actual_day_span():
    """UX-4/7: "Weekly pace (last N days)", not a bare "This week" that
    implies a calendar week regardless of how much history there is."""
    one_day = habits.digest_table(Habits(cycles=[_cycle(loops=1, loop_cost=1.0)]))
    assert one_day.title == "Weekly pace (last 1 day)"

    a_week = habits.digest_table(Habits(cycles=[
        _cycle(WEEKS[0], loops=1, loop_cost=1.0), _cycle(WEEKS[1], loops=1, loop_cost=1.0),
    ]))
    assert a_week.title == "Weekly pace (last 7 days)"


def test_the_digest_leads_with_the_habits_worth_most_then_what_met_goals_cost():
    h = Habits(
        cycles=[_cycle(loops=1, loop_cost=2.0), _cycle(WEEKS[1], loops=1, loop_cost=0.0)],
        agents=[_agent(report_tokens=4_000, report_carry=1.0)],
        pieces=[Piece("met", 3.0, 1, None, (), (), "your feedback"), Piece("missed", 1.0, 1, None, (), (), "x")],
    )
    rows = _rows(habits.digest_table(h))
    # P4 leftover: outcome_misses is now priced (a floor on the misses'
    # real Piece.cost), so it joins the top-3 ranking alongside tool_loops
    # and short_reports instead of sitting out as unpriced.
    assert [r["item"] for r in rows] == ["top_1", "top_2", "top_3", "cost_per_met"]
    assert rows[0]["what"] == habits.ITEMS["tool_loops"][1]
    # Savings are spread over the weeks the messages cover.
    assert rows[0]["value"] == pytest.approx(2.0 / h.span_weeks)
    assert rows[1]["what"] == habits.ITEMS["short_reports"][1]
    assert rows[2]["what"] == habits.ITEMS["outcome_misses"][1]
    assert rows[2]["value"] == pytest.approx(0.5 / h.span_weeks)
    assert rows[3]["value"] == 3.0 and rows[3]["detail"] == "1 of 2 pieces you gave feedback on"


def test_the_playbook_table_carries_the_example_the_basis_and_the_trend():
    h = Habits(cycles=[_cycle(loops=1, loop_cost=1.0)])
    row = _rows(habits.playbook_table(h, habits.playbook(h)))[0]
    assert row["habit"] == "tool_loops" and row["theme"] == "verification"
    assert row["example"] == habits.EXAMPLES["tool_loops"] and row["basis"] == habits.BASES["tool_loops"]
    assert (row["source"], row["confidence"], row["trend"]) == ("inferred", "low", "new")
    # UX-8: a where/trade-off/undo entry, same three-part shape as
    # fixes.py's explainer for a Recommendation.
    assert row["where"] == habits.WHERE["tool_loops"]
    assert row["trade_off"] == habits.TRADE_OFFS["tool_loops"]
    assert row["how_to_undo"] == habits.UNDO["tool_loops"]


def test_every_playbook_item_has_a_where_trade_off_and_undo_entry():
    """UX-8 (rest): a where/trade-off/undo entry for every habit item --
    ``WHERE``, ``TRADE_OFFS`` and ``UNDO`` must cover exactly the keys
    ``ITEMS`` does, or ``playbook_table`` raises a ``KeyError`` for
    whichever item is missing."""
    assert set(habits.WHERE) == set(habits.ITEMS)
    assert set(habits.TRADE_OFFS) == set(habits.ITEMS)
    assert set(habits.UNDO) == set(habits.ITEMS)
    for key in habits.ITEMS:
        assert habits.WHERE[key].strip()
        assert habits.TRADE_OFFS[key].strip()
        assert habits.UNDO[key].strip()


def test_apply_covered_by_drops_the_saving_and_names_the_rule_when_it_fired():
    """UX-3: effort_fit is covered by the effort-mismatch rule
    (``habits.COVERED_BY``). When that rule actually fired in this
    report, the playbook's own effort_fit saving is dropped -- shown
    once, by the rule, not twice."""
    from claudeglass.model import ReportModel, Recommendation, Section

    h = Habits(cycles=[_cycle(loops=1, loop_cost=1.0)])
    table = habits.playbook_table(h, habits.playbook(h))
    # Graft an effort_fit row on, with a saving, so this test doesn't
    # depend on the specific facts _item_effort_fit needs to fire.
    key_idx = [c.key for c in table.columns].index("habit")
    saving_idx = [c.key for c in table.columns].index("saving")
    covered_idx = [c.key for c in table.columns].index("covered_by")
    covered_rule_idx = [c.key for c in table.columns].index("covered_by_rule")
    row = list(table.rows[0])
    row[key_idx] = "effort_fit"
    row[saving_idx] = 3.5
    table.rows.append(row)

    section = Section(key="habits", tables=[table])
    rec = Recommendation(id="effort-mismatch", title="High effort is being spent on easy work")
    report = ReportModel(sections=[section], recommendations=[rec])

    habits.apply_covered_by(report)

    covered_row = next(r for r in table.rows if r[key_idx] == "effort_fit")
    assert covered_row[saving_idx] is None
    assert covered_row[covered_idx] == "High effort is being spent on easy work"
    # Additive: the rule id itself, alongside its title, so a caller can
    # link straight to the recommendation.
    assert covered_row[covered_rule_idx] == "effort-mismatch"
    # A row for an item not in COVERED_BY, or whose rule didn't fire, is
    # untouched.
    uncovered_row = next(r for r in table.rows if r[key_idx] == "tool_loops")
    assert uncovered_row[covered_idx] == ""
    assert uncovered_row[covered_rule_idx] == ""


def test_apply_covered_by_leaves_the_saving_alone_when_the_rule_did_not_fire():
    """The same habit, but its covering rule never fired in this report
    (e.g. below threshold) -- its own saving is the only estimate there
    is, so it must not be dropped."""
    from claudeglass.model import ReportModel, Section

    h = Habits(cycles=[_cycle(loops=1, loop_cost=1.0)])
    table = habits.playbook_table(h, habits.playbook(h))
    key_idx = [c.key for c in table.columns].index("habit")
    saving_idx = [c.key for c in table.columns].index("saving")
    covered_idx = [c.key for c in table.columns].index("covered_by")
    covered_rule_idx = [c.key for c in table.columns].index("covered_by_rule")
    row = list(table.rows[0])
    row[key_idx] = "effort_fit"
    row[saving_idx] = 3.5
    table.rows.append(row)

    section = Section(key="habits", tables=[table])
    report = ReportModel(sections=[section], recommendations=[])

    habits.apply_covered_by(report)

    covered_row = next(r for r in table.rows if r[key_idx] == "effort_fit")
    assert covered_row[saving_idx] == 3.5
    assert covered_row[covered_idx] == ""
    assert covered_row[covered_rule_idx] == ""


def test_allow_routine_states_its_security_trade_off_and_a_permissions_undo():
    """UX-8's own callout: allow_routine (a permission allow-rule) needs
    to say plainly that it's a security trade-off, not just a
    convenience one, and that /permissions is how to undo it."""
    assert "security" in habits.TRADE_OFFS["allow_routine"].lower()
    assert "/permissions" in habits.UNDO["allow_routine"]


def test_allow_routine_names_idle_waits_as_evidence_without_pricing_them():
    """SIG-2: waits["idle"] rides along as evidence text -- it's lost
    time, not spend, so it never enters the saving figure (which stays
    keyed only to blocked_cost, same as before this signal existed)."""
    h = Habits(permission_prompts=Counter({"Bash": 5, "Edit": 1}), waits=Counter({"idle": 4, "quota": 2}))
    item = _by_key(habits.playbook(h))["allow_routine"]
    assert item.saving is None  # no blocked cycles -> nothing priced
    assert "Claude sat waiting for you 4 times" in item.evidence
    assert "Claude asked for permission 6 times" in item.evidence


def test_allow_routine_says_nothing_about_idle_waits_when_none_were_logged():
    h = Habits(permission_prompts=Counter({"Bash": 5}))
    item = _by_key(habits.playbook(h))["allow_routine"]
    assert "waiting for you" not in item.evidence


def test_brief_templates_start_from_the_checklist_and_put_what_you_leave_out_first():
    keys, why = habits.template_lines("bugfix")
    assert tuple(keys) == catalogue.BRIEF_CHECKLISTS["bugfix"] and why.startswith("A starting point")
    tag = CaptureTag(task="bugfix", missing=("constraints",))
    keys, why = habits.template_lines("bugfix", [_cycle(tag=tag) for _ in range(3)])
    assert keys == ["constraints", "repro", "files", "done"]
    assert why == "Constraints was missing in 3 of 3 bug fix asks."
    # Phase 10 review: the kind of task reads by its plain name, as the
    # card's title does ("Debugging", not "debug").
    cycles = [_cycle(tag=CaptureTag(task="debug", missing=("repro",))), _cycle(tag=CaptureTag(task="debug"))]
    assert habits.template_lines("debug", cycles)[1] == "Reproduce was missing in 1 of 2 debugging asks."
    # With no tagged work, the common kinds of task get a template each.
    rows = _rows(_table(habits.section_from(Habits()), "habits_brief_templates"))
    assert [r["task"] for r in rows] == ["bugfix", "feature", "refactor", "research"]
    research = rows[-1]
    assert research["checklist"] == "Goal / Files / Report"
    assert research["template"].splitlines()[-1] == catalogue.BRIEF_LINES["report"][1]


def test_the_checklists_are_the_ones_the_brief_skill_holds():
    assert habits.DEFAULT_CHECKLISTS is catalogue.BRIEF_CHECKLISTS
    assert habits.MISSING_LINES == {k: v for k, v in catalogue.BRIEF_LINES.items() if k != "report"}


@pytest.mark.parametrize(
    "row, reason",
    [
        ({"agent_type": "a", "fit_larger": 2, "fit_smaller": 1}, "Claude said 2 of its runs needed a larger model"),
        ({"agent_type": "a", "fit_larger": 1, "fit_smaller": 3, "hard_pct": 60.0}, "60% of its work was reported hard"),
        ({"agent_type": "a", "retried_model": 1}, "a run was retried because the model wasn't enough"),
        ({"agent_type": "a", "fit_smaller": 3, "hard_pct": 10.0}, None),
        ({"agent_type": "", "fit_larger": 5}, None),
    ],
)
def test_unfit_agents_hold_a_cheaper_model_back_with_the_reason(row, reason):
    assert habits.unfit_agents([row]).get(row["agent_type"]) == reason


# -- from transcripts ---------------------------------------------------------------------


def _ts(second: int) -> str:
    return f"2026-09-18T12:{second // 60:02d}:{second % 60:02d}.000Z"


def _parse(tmp_path, name, lines, **meta):
    path = tmp_path / name
    write_jsonl(path, lines)
    return parse_transcript(path, TranscriptMeta(path=str(path), **meta))


def _reply(second: int, *blocks, text: str = "ok") -> dict:
    content = list(blocks) or [{"type": "text", "text": text}]
    return turn_line(content=content, model=MODEL, timestamp=_ts(second))


def _note(second: int, ids, *, hook: str = "SessionStart", agent_type: str = "") -> dict:
    text = catalogue.note_text(ids, "main") if hook == "SessionStart" else old_agent_note_text(ids, agent_type)
    wrapped = f"<system-reminder>\n{hook} hook additional context: {text}\n</system-reminder>"
    line = attachment_line("hook_additional_context", rendered=wrapped, content=[text], hookName=hook,
                           hookEvent=hook, toolUseID=hook)
    line["timestamp"] = _ts(second)
    return line


def _tagged_session(tmp_path, *, captured: bool = True, shift: str = "redo"):
    """``captured=False`` leaves out the capture notes, for the tests
    that want a session capture never started in (SEC-P2 drops an
    uninvited tag's content either way, so the tag lines below are
    identical -- only whether they're trusted differs). ``shift`` is
    the word the second message's tag reports."""
    top_lines = [
        user_str_line("fix the login bug", origin={"kind": "human"}, timestamp=_ts(0)),
        _reply(1, tool_use_block("Agent", "toolu_A", {"prompt": "find where the cookie is set"}),
               {"type": "text", "text": "Looking.\n[cg: task=bugfix brief=vague level=hard]"}),
        user_block_line([tool_result_block("toolu_A", "src/auth.py")], timestamp=_ts(5)),
        _reply(6, text="Fixed.\n[cg: task=bugfix brief=vague level=hard]"),
        user_str_line("do it again properly", origin={"kind": "human"}, timestamp=_ts(10)),
        _reply(11, text=f"Redone.\n[cg: task=bugfix shift={shift}]"),
    ]
    if captured:
        top_lines.insert(0, _note(0, ["task", "brief", "level", "shift"]))
    top = _parse(tmp_path, "top.jsonl", top_lines, kind="top-level")
    sub_lines = [
        user_str_line("find where the cookie is set", timestamp=_ts(2)),
        _reply(3, text="src/auth.py\n[result: done fit=larger rules=unused]"),
    ]
    if captured:
        # Explore is never asked about rules (see test_capture_catalogue's
        # test_setup_agents_get_no_note_and_explore_is_not_asked_about_rules),
        # so "rules" is left off the note codes here too: SEC-P2 must drop
        # the reply's own "rules=unused" the same as the real hook would
        # never have asked for it.
        sub_lines.insert(0, _note(2, ["result", "fit"], hook="SubagentStart", agent_type="Explore"))
    sub = _parse(tmp_path, "agent-a1.jsonl", sub_lines, kind="subagent", agent_id="agent-a1",
                agent_type="Explore", tool_use_id="toolu_A")
    return NS(sessions=[NS(top=top, subs=[sub], session_id="s1", project_dir="p")])


def test_collect_turns_tags_ratings_and_agent_reports_into_facts(tmp_path, pricing):
    rating = {"outcome": "partly", "slow": ["rework"], "worth": "fair", "helped": ["none"]}
    h = habits.collect(_tagged_session(tmp_path), pricing, ratings={"s1": rating})
    first, second = h.cycles
    assert (first.tag.task, first.tag.brief, first.tag.level) == ("bugfix", "vague", "hard")
    assert first.redone and first.redo_cost == pytest.approx(second.cost) and not second.redone
    assert first.explore_agents == 1 and first.cost > 0
    assert (first.outcome, first.outcome_source) == ("partly", "rating")
    (piece,) = h.pieces
    assert (piece.outcome, piece.source, piece.task, piece.slow, piece.helped) == (
        "partly", "dashboard rating", "bugfix", ("rework",), ()
    )
    (agent,) = h.agents
    # rules stays None: Explore is never asked about it, so its own
    # "rules=unused" isn't trusted (SEC-P2).
    assert (agent.agent_type, agent.result, agent.fit, agent.rules, agent.level, agent.task) == (
        "Explore", "done", "larger", None, "hard", "bugfix"
    )


def _asked(questions) -> list[dict]:
    return [
        {
            "question": q.question,
            "header": q.header,
            "multiSelect": q.multi,
            "options": [{"label": label, "description": text} for _word, label, text in q.options],
        }
        for q in questions
    ]


def _plan_session(tmp_path, name: str, *, edit: bool = True, handoff: str | None = "Yes", worth: str = "Too costly"):
    """A main session that explores, has a plan approved, then (with
    ``edit``) edits a file; then a /cg-feedback run answering both calls."""
    lines = [
        user_str_line("plan the login change", origin={"kind": "human"}, timestamp=_ts(0)),
        turn_line(model=MODEL, timestamp=_ts(1), cache_read_input_tokens=10_000, input_tokens=10),
        turn_line(
            content=[tool_use_block("ExitPlanMode", "tu_p", {"plan": "1. Edit a\n2. Edit b\n" + "x" * 4_000})],
            model=MODEL, timestamp=_ts(2), cache_read_input_tokens=90_000, input_tokens=10,
        ),
        user_block_line([tool_result_block("tu_p", "User has approved your plan.")], timestamp=_ts(3)),
    ]
    if edit:
        lines.append(turn_line(
            content=[tool_use_block("Edit", "tu_e", {"file_path": "src/app.py", "old_string": "a", "new_string": "b"})],
            model=MODEL, timestamp=_ts(4), cache_read_input_tokens=92_000, input_tokens=10,
        ))
        lines.append(user_block_line([tool_result_block("tu_e", "ok")], timestamp=_ts(5)))
    lines.append(_reply(6, text="Done."))
    core = _asked(catalogue.FEEDBACK_QUESTIONS)
    answers = {catalogue.FEEDBACK_QUESTIONS[0].question: "Yes", catalogue.FEEDBACK_QUESTIONS[2].question: worth}
    lines += [
        user_str_line("<command-message>cg-feedback</command-message>\n<command-name>/cg-feedback</command-name>",
                      timestamp=_ts(10)),
        user_block_line([{"type": "text", "text": catalogue.feedback_skill_text()}], isMeta=True, timestamp=_ts(10)),
        _reply(11, tool_use_block("AskUserQuestion", "tu_q", {"questions": core})),
        user_block_line([tool_result_block("tu_q", "User has answered your questions.")],
                        toolUseResult={"questions": core, "answers": answers}, timestamp=_ts(12)),
    ]
    if handoff is not None:
        asked = _asked([catalogue.HANDOFF_QUESTION])
        lines += [
            _reply(13, tool_use_block("AskUserQuestion", "tu_h", {"questions": asked})),
            user_block_line([tool_result_block("tu_h", "User has answered your questions.")],
                            toolUseResult={"questions": asked,
                                           "answers": {catalogue.HANDOFF_QUESTION.question: handoff}},
                            timestamp=_ts(14)),
        ]
    lines.append(_reply(15, text="Thanks: ClaudeGlass will use this for your savings tips."))
    top = _parse(tmp_path, f"{name}.jsonl", lines, kind="top-level")
    return NS(top=top, subs=[], session_id=name, project_dir="p", slug="p")


def test_sessions_that_plan_then_build_are_told_apart(tmp_path, pricing):
    corpus = NS(sessions=[
        _plan_session(tmp_path, "s1"),
        _plan_session(tmp_path, "s2", handoff="No", worth="Worth it"),
        _plan_session(tmp_path, "s3", edit=False, handoff=None),
    ] + [bundle for bundle in _tagged_session(tmp_path).sessions])
    h = habits.collect(corpus, pricing)
    assert [s.shape for s in h.shapes] == ["plan_build", "plan_build", "plan_only", "no_plan"]
    # Starts at 10,005 tokens (the first reply less your message); the plan
    # adds 1,005 (4,020 characters / 4).
    assert h.shapes[0].carried == 90_010 - 10_005 - 1_005
    assert h.shapes[3].carried is None
    first = h.pieces[0]
    assert (first.shape, first.worth, first.handoff) == ("plan_build", "no", "yes")

    rows = {r["shape"]: r for r in _rows(_table(habits.section_from(h), "habits_by_shape"))}
    assert list(rows) == ["plan_build", "plan_only", "no_plan"]
    built = rows["plan_build"]
    assert (built["sessions"], built["share"], built["pieces"]) == (2, 50.0, 2)
    assert (built["met_pct"], built["worth_pct"], built["costly_pct"]) == (100.0, 50.0, 50.0)
    assert (built["handoff_yes"], built["handoff_partly"], built["handoff_no"]) == (1, 0, 1)
    assert built["carried_median"] == 79_000
    assert rows["no_plan"]["carried_median"] is None and rows["no_plan"]["pieces"] == 0


@pytest.mark.parametrize("shift, redone", [("redo", True), ("fix", True), ("build", False)])
def test_a_fix_next_counts_as_rework_exactly_like_a_redo(tmp_path, pricing, shift, redone):
    """``shift=fix`` (the next message fixed a fault in this work) marks
    the work redone and prices it just as ``shift=redo`` does; building
    on it doesn't."""
    corpus = _tagged_session(tmp_path, shift=shift)
    first, second = habits.collect(corpus, pricing).cycles
    assert second.tag.shift == shift and not second.redone
    assert first.redone is redone
    assert first.redo_cost == (pytest.approx(second.cost) if redone else 0.0)
    by_task = {r["task"]: r for r in _rows(_table(habits.build_section(corpus, pricing), "habits_by_task"))}
    assert by_task["bugfix"]["redo_pct"] == pytest.approx(50.0 if redone else 0.0)


def test_collect_folds_in_the_free_signals_by_session():
    """SIG-2: end_reasons/waits/permission_prompts are summed across every
    session's ``SessionSignals`` -- one end_reason count per session that
    logged one, waits and permission prompts added up across all of
    them."""
    from claudeglass import signals

    session_signals = {
        "s1": signals.SessionSignals(
            end_reason="clear", waits={"idle": 2, "quota": 1}, permission_prompts={"Bash": 3},
        ),
        "s2": signals.SessionSignals(end_reason="clear", waits={"idle": 1}),
        "s3": signals.SessionSignals(),  # no SessionEnd logged -> not counted
    }
    h = habits.collect(NS(sessions=[]), None, signals=session_signals)
    assert h.end_reasons == {"clear": 2}
    assert h.waits == {"idle": 3, "quota": 1}
    assert h.permission_prompts == {"Bash": 3}


def test_the_agents_table_feeds_the_model_veto(tmp_path, pricing):
    section = habits.build_section(_tagged_session(tmp_path), pricing)
    rows = {r["agent_type"]: r for r in _rows(_table(section, "habits_agents"))}
    assert rows["Explore"]["fit_larger"] == 1 and rows["Explore"]["rules_unused"] == 0
    assert habits.unfit_agents(list(rows.values()))["Explore"] == "Claude said 1 of its runs needed a larger model"
    by_task = {r["task"]: r for r in _rows(_table(section, "habits_by_task"))}
    assert by_task["all"]["cycles"] == 2 and by_task["bugfix"]["redo_pct"] == pytest.approx(50.0)


# -- agents by kind of task --------------------------------------------------------------


def _swap(alt, pct, state="cheaper_available"):
    return NS(tier_verdict=NS(state=state, alt_model=alt, saving_pct=pct))


def test_agents_are_joined_to_the_task_of_the_message_that_spawned_them(tmp_path, pricing):
    h = habits.collect(_tagged_session(tmp_path), pricing)
    (agent,) = h.agents
    assert agent.task == "bugfix"


def test_agents_without_a_task_are_left_out_of_the_by_task_table():
    h = Habits(agents=[_agent(agent_type="reviewer", task=None, cost=1.0)])
    assert _table(habits.section_from(h), "habits_agents_by_task").rows == []


def test_a_cheaper_model_is_named_only_with_enough_runs_no_veto_and_a_real_saving():
    unvetoed = [_agent(agent_type="reviewer", task="bugfix", cost=2.0, result="done") for _ in range(habits.MIN_GROUP)]
    vetoed = [
        _agent(agent_type="Explore", task="bugfix", cost=1.0, result="done", fit="larger"),
        *[_agent(agent_type="Explore", task="bugfix", cost=1.0, result="done") for _ in range(habits.MIN_GROUP - 1)],
    ]
    h = Habits(agents=[*unvetoed, *vetoed])
    model_swap = NS(by_key={
        "reviewer": _swap("claude-sonnet-4-5", 30.0),
        "Explore": _swap("claude-haiku-4-5-20251001", 40.0),
    })
    rows = {r["agent_type"]: r for r in _rows(_table(habits.section_from(h, model_swap=model_swap), "habits_agents_by_task"))}
    assert rows["reviewer"]["task"] == "bugfix" and rows["reviewer"]["runs"] == habits.MIN_GROUP
    assert (rows["reviewer"]["cheaper_model"], rows["reviewer"]["cheaper_saving_pct"]) == ("sonnet", 30.0)
    # Explore's runs said one of them needed a larger model: unfit_agents holds it back.
    assert rows["Explore"]["fit_larger"] == 1 and rows["Explore"]["cheaper_model"] is None


def test_too_few_runs_or_too_small_a_saving_name_no_cheaper_model():
    few = Habits(agents=[_agent(agent_type="reviewer", task="bugfix", cost=1.0) for _ in range(habits.MIN_GROUP - 1)])
    small = Habits(agents=[_agent(agent_type="reviewer", task="bugfix", cost=1.0) for _ in range(habits.MIN_GROUP)])
    swap = NS(by_key={"reviewer": _swap("claude-sonnet-4-5", habits.CHEAPER_MODEL_MIN_PCT - 1)})
    assert _rows(_table(habits.section_from(few, model_swap=NS(by_key={"reviewer": _swap("claude-sonnet-4-5", 30.0)})),
                         "habits_agents_by_task"))[0]["cheaper_model"] is None
    assert _rows(_table(habits.section_from(small, model_swap=swap), "habits_agents_by_task"))[0]["cheaper_model"] is None


def test_without_model_swap_data_no_cheaper_model_is_named():
    h = Habits(agents=[_agent(agent_type="reviewer", task="bugfix", cost=1.0) for _ in range(habits.MIN_GROUP)])
    assert _rows(_table(habits.section_from(h), "habits_agents_by_task"))[0]["cheaper_model"] is None


def test_the_capture_section_says_what_capture_cost_and_since_when(tmp_path, pricing):
    config = NS(level="standard", enabled_at="2026-09-01T08:00:00+00:00")
    section = habits.capture_section(_tagged_session(tmp_path), pricing, config)
    rows = dict(_table(section, "capture_usage").rows)
    assert rows["level"] == catalogue.LEVEL_TITLES["standard"] and rows["since"] == "2026-09-01"
    assert section.key == "capture"
    off = dict(habits.capture_section(NS(sessions=[]), pricing, NS(level="off", enabled_at="")).tables[0].rows)
    assert off["level"] == catalogue.LEVEL_TITLES["off"] and off["since"] == ""


def test_the_capture_section_reports_sessions_with_notes_and_after_compact_cost(tmp_path, pricing):
    """SURV-3/8: the capture_usage table passes ``capture.usage``'s
    ``sessions``/``after_compact_notes``/``after_compact_cost`` straight
    through -- ``_tagged_session`` has one captured main session and no
    compact boundary, so there's nothing after one yet."""
    config = NS(level="standard", enabled_at="2026-09-01T08:00:00+00:00")
    section = habits.capture_section(_tagged_session(tmp_path), pricing, config)
    rows = dict(_table(section, "capture_usage").rows)
    assert rows["sessions_with_notes"] == 1
    assert rows["after_compact_notes"] == 0
    assert rows["after_compact_cost"] == 0.0


def _tagged_session_with_attempted_leaks(tmp_path):
    """Same shape as ``_tagged_session``, plus a prompt naming a real
    path/secret and a tag trying to smuggle a skill name the transcript
    never listed, an unknown vocabulary word and a path through
    ``missing=``/``skill=``. Everything here should be dropped or
    reduced to a closed-vocabulary word well before it could reach a
    playbook evidence sentence or a skills-table row.
    """
    top = _parse(tmp_path, "top.jsonl", [
        user_str_line(
            "fix C:\\Users\\paulm\\secret-project\\auth.py, token=sk-testonly1234567890",
            origin={"kind": "human"}, timestamp=_ts(0),
        ),
        _reply(
            1,
            text="Looking.\n[cg: task=bugfix brief=vague level=hard "
            "missing=files,/etc/passwd skill=would-help:not-a-real-skill]",
        ),
        user_str_line("do it again properly", origin={"kind": "human"}, timestamp=_ts(10)),
        _reply(11, text="Redone.\n[cg: task=bugfix shift=redo]"),
    ], kind="top-level")
    sub = _parse(tmp_path, "agent-a1.jsonl", [
        user_str_line("find where the cookie is set", timestamp=_ts(2)),
        _reply(3, text="src/auth.py\n[result: done fit=larger rules=unused]"),
    ], kind="subagent", agent_id="agent-a1", agent_type="Explore", tool_use_id="toolu_A")
    return NS(sessions=[NS(top=top, subs=[sub], session_id="s1", project_dir="p")])


def test_habits_and_capture_sections_pass_the_privacy_scan(tmp_path, pricing):
    """Coverage gap fix (phase 10 privacy sweep): the parser-level tests
    in test_capture_parse.py already prove a raw path/secret/unknown
    skill name never survives into a ``Turn``'s ``CaptureTag``, but
    nothing ran ``helpers.assert_privacy`` over the rendered ``habits``/
    ``capture`` report sections themselves -- the tables actually shown
    on the Work habits and Capture tabs, built from those tags plus
    template evidence/example sentences. This closes that gap, with a
    fixture that also tries to smuggle a path/secret/unknown skill name
    through, matching test_capture_parse.py's own attempted-leak shape.
    """
    corpus = _tagged_session_with_attempted_leaks(tmp_path)
    section = habits.build_section(corpus, pricing, ratings={"s1": {
        "outcome": "partly", "slow": ["rework"], "worth": "fair", "helped": ["none"],
    }})
    assert_privacy(section)
    capture = habits.capture_section(corpus, pricing, NS(level="standard", enabled_at="2026-09-01T08:00:00+00:00"))
    assert_privacy(capture)
    # The attempted leaks never even reach a table cell.
    skills_rows = _rows(_table(section, "habits_skills"))
    assert "not-a-real-skill" not in {r["skill"] for r in skills_rows}
    briefs_rows = _rows(_table(section, "habits_briefs"))
    assert all("/etc/passwd" not in (r.get("missing") or "") for r in briefs_rows)


def test_the_capture_section_prices_its_weekly_cost_against_what_depends_on_it(tmp_path, pricing):
    config = NS(level="standard", enabled_at="2026-09-01T08:00:00+00:00")
    table = _table(habits.capture_section(_tagged_session(tmp_path, captured=False), pricing, config), "capture_usage")
    rows = dict(table.rows)
    # "since" is far enough in the past for a weekly rate to be worked
    # out (0, since this fixture has no injected capture note to price);
    # nothing in it is worth enough (or reported/rated) to build a habit
    # whose evidence needs capture or feedback, so it says so instead of
    # a zero value.
    assert rows["weekly_cost"] == pytest.approx(0.0)
    assert rows["habit_value"] is None
    assert table.notes and "nothing" in table.notes[0].lower()


def test_the_capture_section_prices_nothing_when_capture_never_started(pricing):
    off = habits.capture_section(NS(sessions=[]), pricing, NS(level="off", enabled_at=""))
    rows = dict(_table(off, "capture_usage").rows)
    assert rows["weekly_cost"] is None and rows["habit_value"] is None
    assert rows["held_back"] == 0


def test_the_capture_section_defaults_the_step_down_row_when_theres_no_suggestion(tmp_path, pricing):
    config = NS(level="standard", enabled_at="2026-09-01T08:00:00+00:00")
    rows = dict(_table(habits.capture_section(_tagged_session(tmp_path), pricing, config), "capture_usage").rows)
    assert rows["step_down_target"] == ""
    assert rows["step_down_tokens_saved"] == 0
    assert rows["step_down_weekly_saving"] == 0


def test_the_capture_section_surfaces_a_step_down_suggestion_when_ready_and_stable(monkeypatch, pricing):
    """CAP-7: ``capture_section`` reuses ``capture_step_down_suggestion``
    rather than recomputing it -- fed here by monkeypatching
    ``capture.usage`` (a full transcript replay isn't needed to prove
    the wiring) and passing an already-built, stable ``h``."""
    h = Habits(cycles=[*_level_tracking_half(WEEKS[0]), *_level_tracking_half(WEEKS[4])])
    dropped = _step_down_dropped("deep", "standard")
    use = capture_mod.CaptureUsage(
        since="2026-08-01T00:00:00+00:00",
        answers={i: capture_mod.enough_target(i) for i in dropped},
        by_metric={i: 2.0 for i in dropped},
    )
    monkeypatch.setattr(capture_mod, "usage", lambda *a, **kw: use)
    config = NS(level="deep", enabled_at="2026-08-01T00:00:00+00:00")
    section = habits.capture_section(NS(sessions=[]), pricing, config, h=h)
    assert_privacy(section)
    table = _table(section, "capture_usage")
    rows = dict(table.rows)
    assert rows["step_down_target"] == "standard"
    assert rows["step_down_tokens_saved"] > 0
    assert any("claudeglass capture level standard --dry-run" in n for n in table.notes)
    assert any("claudeglass capture level deep" in n for n in table.notes)
    # What changes, where, the trade-off and the undo (no apply button).
    note = next(n for n in table.notes if "--dry-run" in n)
    # The dropped metrics by their Capture page names, never their ids.
    assert habits.metric_list(dropped) in note
    assert "stops collecting them" in note and "writes nothing" in note
    assert "config.toml" in note and "settings.json" in note
    assert "per session start" in note and "per subagent start" in note


# -- CAP-5: a derived fallback for check -------------------------------------


def test_targeted_checks_prices_unchecked_redone_work_from_self_reports():
    h = Habits(cycles=[
        *(_cycle(tag=CaptureTag(check="none"), redone=True, redo_cost=2.0) for _ in range(3)),
        *(_cycle(tag=CaptureTag(check="targeted")) for _ in range(2)),
    ])
    item = habits._item_targeted_checks(h)
    assert item is not None
    assert item.n == 5
    assert item.saving == pytest.approx(3 * 0.5 * 2.0)
    assert item.sources == ("reported",)


def test_targeted_checks_counts_a_test_command_as_derived_evidence_without_a_tag():
    h = Habits(cycles=[
        *(_cycle(tag=CaptureTag(check="none"), redone=True, redo_cost=2.0) for _ in range(3)),
        *(_cycle(checked_by_tool=True) for _ in range(2)),  # no tag at all
    ])
    item = habits._item_targeted_checks(h)
    assert item is not None
    # The derived-only cycles widen the evidence base (n) even though
    # they can't be "unchecked" (there's no deriving that from a missing
    # command), so the priced saving is unchanged.
    assert item.n == 5
    assert item.sources == ("reported", "inferred")
    assert item.saving == pytest.approx(3 * 0.5 * 2.0)


def test_targeted_checks_does_not_blame_a_report_a_test_command_contradicts():
    h = Habits(cycles=[
        # Said "none" but a test command ran anyway -- CAP-6-adjacent
        # contradiction, and not really unchecked, so no waste to price.
        *(_cycle(tag=CaptureTag(check="none"), redone=True, redo_cost=2.0, checked_by_tool=True) for _ in range(5)),
    ])
    assert habits._item_targeted_checks(h) is None


# -- EST-P7 + CAP-3: what capture buys in recommend() -----------------------


def _rec(id: str, saving_usd: float | None, agent_type: str | None = None, lever: str | None = None) -> Recommendation:
    return Recommendation(id=id, agent_type=agent_type, lever=lever, saving_usd=saving_usd)


def test_capture_recommend_delta_counts_what_appears_or_grows_and_vetoes_the_rest():
    without = [
        _rec("effort-mismatch", 5.0, agent_type="reviewer"),
        _rec("only-without-habits", 2.0),
    ]
    with_ = [
        _rec("effort-mismatch", 8.0, agent_type="reviewer"),  # grows: +3
        _rec("only-with-habits", 3.0),  # appears: +3
    ]
    grown, vetoed = habits.capture_recommend_delta(with_, without)
    assert grown[("effort-mismatch", "reviewer", None)] == pytest.approx(3.0)
    assert grown[("only-with-habits", None, None)] == pytest.approx(3.0)
    assert [r.id for r in vetoed] == ["only-without-habits"]


def test_capture_recommend_delta_ignores_a_recommendation_that_shrinks():
    without = [_rec("effort-mismatch", 8.0)]
    with_ = [_rec("effort-mismatch", 5.0)]  # capture's evidence made it look smaller, not bigger
    grown, vetoed = habits.capture_recommend_delta(with_, without)
    assert grown == {}
    assert vetoed == []


def test_capture_value_breakdown_dedups_through_the_closed_map_taking_the_larger_figure():
    # The playbook item's own saving (10) beats what recommend() grows by
    # (4) for the SAME waste (effort_fit <-> effort-mismatch, CAP-3) --
    # taking the larger figure, never the sum (10 + 4).
    items = [Item(key="effort_fit", saving=10.0, n=5, sources=("reported",), evidence="e")]
    with_ = [_rec("effort-mismatch", 4.0)]
    breakdown = habits.capture_value_breakdown(items, with_, without_habits=[])
    assert breakdown["usd"] == pytest.approx(10.0)
    assert breakdown["held_back"] == 0

    # The other way around: recommend()'s grown figure (40) beats the
    # item's own saving (10).
    with_bigger = [_rec("effort-mismatch", 40.0)]
    breakdown_bigger = habits.capture_value_breakdown(items, with_bigger, without_habits=[])
    assert breakdown_bigger["usd"] == pytest.approx(40.0)


def test_capture_value_breakdown_adds_unclaimed_recommend_value_in_full():
    # effort_fit dedups against effort-mismatch (max(2*0.5, 1.0) == 1.0);
    # ttl-switch has no playbook counterpart in the closed map, so it's
    # added in full rather than dropped or weighted.
    items = [Item(key="effort_fit", saving=2.0, n=5, sources=("reported",), evidence="e", reported_share=0.5)]
    with_ = [_rec("effort-mismatch", 1.0), _rec("ttl-switch", 6.0, agent_type="reviewer", lever="ttl")]
    breakdown = habits.capture_value_breakdown(items, with_, without_habits=[])
    assert breakdown["usd"] == pytest.approx(7.0)


def test_capture_value_breakdown_holds_back_vetoes_without_pricing_them():
    items = []
    without = [_rec("only-without-habits", 9.0)]
    breakdown = habits.capture_value_breakdown(items, with_habits=[], without_habits=without)
    assert breakdown["usd"] == pytest.approx(0.0)
    assert breakdown["held_back"] == 1


def test_capture_value_breakdown_skips_items_with_no_reported_share():
    # Finding A3: an item whose saving is entirely inferred (reported_share
    # 0.0) shouldn't be counted as something capture is buying.
    items = [Item(key="skill_early", saving=5.0, n=5, sources=("inferred",), evidence="e", reported_share=0.0)]
    breakdown = habits.capture_value_breakdown(items, with_habits=[], without_habits=[])
    assert breakdown["usd"] == pytest.approx(0.0)


def test_capture_dependent_value_normalises_per_week_since_enabled(pricing):
    h = Habits(cycles=[_cycle(week=w) for w in WEEKS])
    items = [Item(key="effort_fit", saving=14.0, n=5, sources=("reported",), evidence="e")]
    since = "2026-08-10T00:00:00+00:00"
    value = habits.capture_dependent_value(h, items, with_habits=[], without_habits=[], since=since)
    # weeks_since uses the real wall clock by default, so assert
    # consistency with the same helper rather than a hand-picked number --
    # the fallback to h.span_weeks is covered by the "no since" test below.
    expected_weeks = capture_mod.weeks_since(since) or h.span_weeks
    assert value == pytest.approx(14.0 / expected_weeks)


def test_capture_dependent_value_falls_back_to_span_weeks_without_since():
    h = Habits(cycles=[_cycle(week=w) for w in WEEKS])
    items = [Item(key="effort_fit", saving=14.0, n=5, sources=("reported",), evidence="e")]
    value = habits.capture_dependent_value(h, items, with_habits=[], without_habits=[], since="")
    assert value == pytest.approx(14.0 / h.span_weeks)


def test_capture_dependent_value_none_when_nothing_depends_on_capture_or_recommend():
    h = Habits(cycles=[_cycle(week=w) for w in WEEKS])
    items = [Item(key="effort_fit", saving=14.0, n=5, sources=("inferred",), evidence="e", reported_share=0.0)]
    assert habits.capture_dependent_value(h, items, with_habits=[], without_habits=[]) is None


def test_patch_capture_recommend_value_folds_the_diff_into_the_table(pricing):
    config = NS(level="standard", enabled_at="")
    corpus = NS(sessions=[])
    section = habits.capture_section(corpus, pricing, config)
    without = [_rec("only-without-habits", 5.0)]
    with_ = [_rec("only-with-habits", 12.0)]
    patched = habits.patch_capture_recommend_value(
        section, corpus, pricing, config, with_habits=with_, without_habits=without,
    )
    table = _table(patched, "capture_usage")
    rows = dict(table.rows)
    assert rows["held_back"] == 1
    # No cycles in this corpus, so Habits.span_weeks falls back to 1.0.
    assert rows["habit_value"] == pytest.approx(12.0)
    assert any("held back 1 recommendation" in note for note in table.notes)
    assert "Nothing measured yet" not in " ".join(table.notes)


# -- Claude's reports against your feedback --------------------------------------


def test_self_report_calibration_flags_easy_work_that_misses_more_than_normal():
    easy, normal = CaptureTag(level="easy"), CaptureTag(level="normal")
    h = Habits(cycles=[
        *(_cycle(tag=easy, outcome="missed") for _ in range(3)),
        *(_cycle(tag=easy, outcome="met") for _ in range(2)),
        *(_cycle(tag=normal, outcome="missed") for _ in range(1)),
        *(_cycle(tag=normal, outcome="met") for _ in range(4)),
    ])
    calibration = habits._self_report_calibration(h)
    assert calibration["contradicts"] is True
    assert calibration["easy_missed_pct"] == pytest.approx(60.0)
    assert calibration["normal_missed_pct"] == pytest.approx(20.0)


def test_self_report_calibration_ignores_outcomes_sourced_from_claudes_own_tag():
    # SEC-P1: a `[cg-fb: ...]` tag is Claude's own report of the
    # outcome, not yours, so it must not feed the calibration that
    # checks Claude's reports against your feedback -- only "answers"
    # (/cg-feedback's question) and "rating" (the dashboard) count.
    easy, normal = CaptureTag(level="easy"), CaptureTag(level="normal")

    def _cycles(source: str) -> list:
        return [
            *(_cycle(tag=easy, outcome="missed", outcome_source=source) for _ in range(3)),
            *(_cycle(tag=easy, outcome="met", outcome_source=source) for _ in range(2)),
            *(_cycle(tag=normal, outcome="missed", outcome_source=source) for _ in range(1)),
            *(_cycle(tag=normal, outcome="met", outcome_source=source) for _ in range(4)),
        ]

    assert habits._self_report_calibration(Habits(cycles=_cycles("tag"))) is None
    calibration = habits._self_report_calibration(Habits(cycles=_cycles("answers")))
    assert calibration["contradicts"] is True
    assert calibration["easy_missed_pct"] == pytest.approx(60.0)


def test_too_little_feedback_leaves_self_report_calibration_unknown():
    h = Habits(cycles=[_cycle(tag=CaptureTag(level="easy")) for _ in range(8)])
    assert habits._self_report_calibration(h) is None
    assert _table(habits.section_from(h), "habits_self_report").notes == []


def test_the_self_report_table_only_lists_words_that_were_tagged():
    h = Habits(cycles=[
        _cycle(tag=CaptureTag(level="hard"), redone=True),
        _cycle(tag=CaptureTag(level="hard")),
        _cycle(tag=CaptureTag(brief="vague"), outcome="met"),
    ])
    rows = _rows(_table(habits.section_from(h), "habits_self_report"))
    assert {r["signal"] for r in rows} == {"level:hard", "brief:vague"}
    hard = next(r for r in rows if r["signal"] == "level:hard")
    assert hard["cycles"] == 2 and hard["rated"] == 0 and hard["redone_pct"] == pytest.approx(50.0)
    vague = next(r for r in rows if r["signal"] == "brief:vague")
    assert vague["rated"] == 1 and vague["met_pct"] == pytest.approx(100.0) and vague["missed_pct"] == 0.0


def test_feedback_that_contradicts_easy_reports_lowers_effort_fits_confidence():
    easy, normal = CaptureTag(level="easy"), CaptureTag(level="normal")
    h = Habits(cycles=[
        # thinking_cost/output_cost keep the combined thinking share well
        # over the shared 30% gate (0.2 of 0.3 output = 66.7%, UX-3).
        *(_cycle(tag=easy, effort="high", thinking_cost=0.2, output_cost=0.3, outcome="missed") for _ in range(5)),
        *(_cycle(tag=easy, effort="high", thinking_cost=0.2, output_cost=0.3, outcome="met") for _ in range(3)),
        *(_cycle(tag=normal, outcome="missed") for _ in range(1)),
        *(_cycle(tag=normal, outcome="met") for _ in range(4)),
    ])
    items = habits.playbook(h)
    effort = _by_key(items)["effort_fit"]
    assert effort.n == 8  # medium confidence by count alone (n >= 8)
    assert "low confidence" in effort.evidence
    row = next(r for r in _rows(habits.playbook_table(h, items)) if r["habit"] == "effort_fit")
    assert row["confidence"] == "low"
    note = _table(habits.section_from(h), "habits_self_report").notes[0]
    assert "62%" in note or "63%" in note  # 5/8 missed


def test_confidence_ignores_self_report_calibration_for_other_habits():
    assert habits.confidence(Item("tool_loops", None, 20, ("inferred",), ""), self_report_ok=False) == "medium"


# -- CAP-6: a consistency score for self-reports -----------------------------


def test_auc_reads_perfect_backwards_and_undefined_discrimination():
    assert habits._auc([0, 0, 1, 1], [False, False, True, True]) == pytest.approx(1.0)
    assert habits._auc([1, 1, 0, 0], [False, False, True, True]) == pytest.approx(0.0)
    assert habits._auc([0, 1, 0, 1], [False, True, True, False]) == pytest.approx(0.5)
    assert habits._auc([1, 1], [True, True]) is None  # no negative class to discriminate from


def test_d_level_is_positive_when_harder_self_reports_track_more_misses():
    h = Habits(cycles=[
        *(_cycle(tag=CaptureTag(level="easy"), outcome="met") for _ in range(4)),
        _cycle(tag=CaptureTag(level="easy"), outcome="missed"),
        *(_cycle(tag=CaptureTag(level="hard"), outcome="missed") for _ in range(4)),
        _cycle(tag=CaptureTag(level="hard"), outcome="met"),
    ])
    assert habits.d_level(h) > 0


def test_d_level_is_negative_when_self_reports_run_backwards():
    h = Habits(cycles=[
        *(_cycle(tag=CaptureTag(level="easy"), outcome="missed") for _ in range(4)),
        _cycle(tag=CaptureTag(level="easy"), outcome="met"),
        *(_cycle(tag=CaptureTag(level="hard"), outcome="met") for _ in range(4)),
        _cycle(tag=CaptureTag(level="hard"), outcome="missed"),
    ])
    assert habits.d_level(h) < 0


def test_d_level_none_under_min_group():
    h = Habits(cycles=[_cycle(tag=CaptureTag(level="easy"), outcome="missed") for _ in range(4)])
    assert habits.d_level(h) is None


# -- CAP-7: whether d_level has settled, and a level step-down suggestion ---


def _level_tracking_half(week: str) -> list[CycleFact]:
    """CAP-7 test fixture: one ``MIN_GROUP``-clearing half where harder
    self-reports genuinely track more misses (the same shape as
    ``test_d_level_is_positive_when_harder_self_reports_track_more_misses``),
    all dated ``week`` so :func:`habits.d_level_stability` can be handed
    two of these (different weeks) as a stable pair."""
    return [
        *(_cycle(week=week, tag=CaptureTag(level="easy"), outcome="met") for _ in range(4)),
        _cycle(week=week, tag=CaptureTag(level="easy"), outcome="missed"),
        *(_cycle(week=week, tag=CaptureTag(level="hard"), outcome="missed") for _ in range(4)),
        _cycle(week=week, tag=CaptureTag(level="hard"), outcome="met"),
    ]


def _level_backwards_half(week: str) -> list[CycleFact]:
    """The reverse of :func:`_level_tracking_half` (self-reports run
    backwards), same shape as
    ``test_d_level_is_negative_when_self_reports_run_backwards``."""
    return [
        *(_cycle(week=week, tag=CaptureTag(level="easy"), outcome="missed") for _ in range(4)),
        _cycle(week=week, tag=CaptureTag(level="easy"), outcome="met"),
        *(_cycle(week=week, tag=CaptureTag(level="hard"), outcome="met") for _ in range(4)),
        _cycle(week=week, tag=CaptureTag(level="hard"), outcome="missed"),
    ]


def test_d_level_stability_is_stable_when_both_halves_agree():
    h = Habits(cycles=[*_level_tracking_half(WEEKS[0]), *_level_tracking_half(WEEKS[4])])
    stability = habits.d_level_stability(h)
    assert stability is not None
    assert stability["first"] == pytest.approx(stability["second"])
    assert stability["stable"] is True


def test_d_level_stability_is_false_when_the_signal_reverses_over_time():
    h = Habits(cycles=[*_level_tracking_half(WEEKS[0]), *_level_backwards_half(WEEKS[4])])
    stability = habits.d_level_stability(h)
    assert stability is not None
    assert stability["first"] > 0 > stability["second"]
    assert stability["stable"] is False


def test_d_level_stability_none_under_twice_min_group():
    # 9 rated cycles: enough for habits.d_level (>= MIN_GROUP) but one
    # short of 2 * MIN_GROUP, so neither half can be judged on its own.
    h = Habits(cycles=[
        *(_cycle(tag=CaptureTag(level="easy"), outcome="met") for _ in range(4)),
        _cycle(tag=CaptureTag(level="easy"), outcome="missed"),
        *(_cycle(tag=CaptureTag(level="hard"), outcome="missed") for _ in range(4)),
    ])
    assert habits.d_level(h) is not None
    assert habits.d_level_stability(h) is None


def _step_down_dropped(level: str, target: str) -> list[str]:
    return [
        i for i in catalogue.level_metrics(level)
        if i not in catalogue.level_metrics(target) and catalogue.asks_claude(i)
    ]


def test_capture_step_down_suggestion_when_ready_and_stable():
    h = Habits(cycles=[*_level_tracking_half(WEEKS[0]), *_level_tracking_half(WEEKS[4])])
    dropped = _step_down_dropped("deep", "standard")
    assert dropped  # sanity: deep really does drop something Claude is asked for
    use = capture_mod.CaptureUsage(
        answers={i: capture_mod.enough_target(i) for i in dropped},
        by_metric={i: 2.0 for i in dropped},
    )
    config = NS(level="deep", enabled_at="2026-08-01T00:00:00+00:00")
    suggestion = habits.capture_step_down_suggestion(h, config, use)
    assert suggestion is not None
    assert suggestion["current"] == "deep" and suggestion["target"] == "standard"
    assert suggestion["dropped_metrics"] == len(dropped)
    assert suggestion["session_note_tokens_saved"] >= 0 and suggestion["subagent_note_tokens_saved"] >= 0
    weeks = capture_mod.weeks_since(config.enabled_at)
    assert suggestion["weekly_usd_saved"] == pytest.approx(2.0 * len(dropped) / weeks)
    assert suggestion["command"] == "claudeglass capture level standard --dry-run"
    assert suggestion["undo_command"] == "claudeglass capture level deep"


def test_capture_step_down_suggestion_none_when_a_dropped_metric_lacks_answers():
    h = Habits(cycles=[*_level_tracking_half(WEEKS[0]), *_level_tracking_half(WEEKS[4])])
    dropped = _step_down_dropped("deep", "standard")
    use = capture_mod.CaptureUsage(
        answers={i: capture_mod.enough_target(i) for i in dropped[:-1]},  # the last one is short
        by_metric={i: 2.0 for i in dropped},
    )
    config = NS(level="deep", enabled_at="2026-08-01T00:00:00+00:00")
    assert habits.capture_step_down_suggestion(h, config, use) is None


def test_capture_step_down_suggestion_none_when_d_level_is_not_stable():
    h = Habits(cycles=[*_level_tracking_half(WEEKS[0]), *_level_backwards_half(WEEKS[4])])
    dropped = _step_down_dropped("deep", "standard")
    use = capture_mod.CaptureUsage(
        answers={i: capture_mod.enough_target(i) for i in dropped},
        by_metric={i: 2.0 for i in dropped},
    )
    config = NS(level="deep", enabled_at="2026-08-01T00:00:00+00:00")
    assert habits.capture_step_down_suggestion(h, config, use) is None


def test_capture_step_down_suggestion_none_off_the_step_ladder_or_without_usage():
    h = Habits(cycles=[*_level_tracking_half(WEEKS[0]), *_level_tracking_half(WEEKS[4])])
    use = capture_mod.CaptureUsage()
    for level in ("off", "free", "essentials", "custom"):
        assert habits.capture_step_down_suggestion(h, NS(level=level, enabled_at=""), use) is None
    assert habits.capture_step_down_suggestion(h, NS(level="deep", enabled_at=""), None) is None


def test_brief_clarity_index_is_positive_when_vaguer_briefs_track_more_misses():
    h = Habits(cycles=[
        *(_cycle(tag=CaptureTag(brief="clear"), outcome="met") for _ in range(4)),
        _cycle(tag=CaptureTag(brief="clear"), outcome="missed"),
        *(_cycle(tag=CaptureTag(brief="vague"), outcome="missed") for _ in range(4)),
        _cycle(tag=CaptureTag(brief="vague"), outcome="met"),
    ])
    assert habits.brief_clarity_index(h) > 0


def test_effort_percentiles_rank_within_reported_task_only():
    a1 = _cycle(tag=CaptureTag(task="bugfix"), cost=1.0)
    a2 = _cycle(tag=CaptureTag(task="bugfix"), cost=2.0)
    a3 = _cycle(tag=CaptureTag(task="bugfix"), cost=3.0)
    solo = _cycle(tag=CaptureTag(task="docs"), cost=5.0)  # the only "docs" cycle -- nothing to rank it against
    h = Habits(cycles=[a1, a2, a3, solo])
    pcts = habits._effort_percentiles(h)
    assert pcts[id(a1)] == 0.0 and pcts[id(a3)] == 1.0 and pcts[id(a2)] == pytest.approx(0.5)
    assert id(solo) not in pcts


def test_contradiction_flags_counts_checked_and_effort_contradictions():
    h = Habits(cycles=[
        *(_cycle(tag=CaptureTag(check="none"), checked_by_tool=True) for _ in range(3)),
        _cycle(tag=CaptureTag(task="bugfix", level="easy"), cost=10.0),
        *(_cycle(tag=CaptureTag(task="bugfix", level="normal"), cost=1.0) for _ in range(3)),
    ])
    flags = habits.contradiction_flags(h)
    assert flags["checked_contradicted"] == 3
    assert flags["easy_high_effort"] == 1  # the easy-tagged message is by far the priciest of its task


def test_self_report_calibration_carries_the_cap_6_fields_and_flips_on_a_frequent_contradiction():
    # Neither easy-vs-normal nor d_level alone would flag this corpus --
    # only a frequent, concrete contradiction does (>= MIN_GROUP times).
    checked = [
        _cycle(tag=CaptureTag(level="normal", check="none"), checked_by_tool=True, outcome="met")
        for _ in range(5)
    ]
    easy = [_cycle(tag=CaptureTag(level="easy"), outcome="met") for _ in range(5)]
    h = Habits(cycles=[*checked, *easy])
    calibration = habits._self_report_calibration(h)
    assert calibration is not None
    assert calibration["contradiction_flags"]["checked_contradicted"] == 5
    assert calibration["contradicts"] is True
    # Nobody missed a goal in this fixture, so there's no positive class
    # for d_level's AUC to discriminate -- None, not a division error.
    assert calibration["d_level"] is None
    note = habits._self_report_note(h)
    assert "5 times it said a change was unchecked but a test command ran anyway" in note


# -- the best setup per kind of task -----------------------------------------------


def _setup_cycles(n, *, model, effort, cost, task="bugfix", level="normal", redone=0, outcome=None, session=None,
                   speed=None, main_cost=None):
    """``n`` cycles of one setup, as one session each -- so, per PROF-04,
    exactly one of them (the last) is left out of the went-well/redone
    rate. ``session`` defaults to a group-specific id so different
    setups in the same test don't share a session and cross-exclude
    each other's cycles."""
    tag = CaptureTag(task=task, level=level)
    session = session or f"{model}-{effort}-{level}-{task}"
    return [
        _cycle(tag=tag, cost=cost, model=model, effort=effort, redone=i < redone, outcome=outcome,
               session_id=session, speed=speed, main_cost=cost if main_cost is None else main_cost)
        for i in range(n)
    ]


@pytest.mark.parametrize(
    "model_id, name",
    [("claude-haiku-4-5-20251001", "haiku"), ("claude-sonnet-5", "sonnet"), ("claude-opus-5-5", "opus"),
     ("claude-fable-5-1", "fable"), ("claude-widget-9", "claude-widget-9"), (None, "unknown")],
)
def test_family_names_the_model_family(model_id, name):
    assert habits.family(model_id) == name


def test_feedback_decides_went_well_before_the_next_message_does():
    assert habits.went_well(_cycle(outcome="met", redone=True))
    assert not habits.went_well(_cycle(outcome="missed"))
    assert habits.went_well(_cycle()) and not habits.went_well(_cycle(redone=True))


def test_the_cheapest_setup_that_went_as_well_as_your_usual_one_is_named():
    h = Habits(cycles=[
        *_setup_cycles(25, model="claude-opus-5-5", effort="high", cost=2.0, redone=2),
        *_setup_cycles(6, model="claude-sonnet-5", effort="medium", cost=0.5, redone=1),
        # Cheaper still, but redone far more often -- big enough on both
        # sides (n=25, n=20) to clear quality.MIN_DENOMINATOR and let
        # the ratio test actually catch it.
        *_setup_cycles(20, model="claude-haiku-4-5-20251001", effort="low", cost=0.1, redone=14),
    ])
    rows = [r for r in _rows(_table(habits.section_from(h), "habits_setups")) if r["level"] == "all"]
    by_setup = {(r["model"], r["effort"]): r for r in rows}
    usual = by_setup[("claude-opus-5-5", "high")]
    cheaper = by_setup[("claude-sonnet-5", "medium")]
    # One cycle per setup (the last of its session) is left out of the
    # went-well rate (PROF-04): 24 of 25 opus messages are rated, 2 redone.
    assert usual["verdict"] == "usual" and usual["cycles"] == 25 and usual["ok_pct"] == pytest.approx(2200 / 24)
    assert cheaper["verdict"] == "cheaper" and cheaper["saving_pct"] == pytest.approx(75.0)
    haiku = by_setup[("claude-haiku-4-5-20251001", "low")]
    assert haiku["verdict"] == "" and haiku["saving_pct"] is None
    # The levels Claude reported get rows of their own, after all of them.
    assert {r["level"] for r in _rows(_table(habits.section_from(h), "habits_setups"))} == {"all", "normal"}


def test_a_cheaper_setup_with_no_clear_quality_difference_is_named():
    h = Habits(cycles=[
        *_setup_cycles(22, model="claude-opus-5-5", effort="high", cost=2.0),
        *_setup_cycles(20, model="claude-sonnet-5", effort="high", cost=1.0, redone=1),
    ])
    rows = {r["model"]: r for r in _rows(_table(habits.section_from(h), "habits_setups")) if r["level"] == "all"}
    # 19 of 20 sonnet messages are rated (the last is excluded), 1 redone.
    assert rows["claude-sonnet-5"]["ok_pct"] == pytest.approx(1800 / 19)
    assert rows["claude-sonnet-5"]["verdict"] == "cheaper"


def test_a_cheaper_setup_that_was_retried_far_more_often_is_not_named():
    h = Habits(cycles=[
        *_setup_cycles(22, model="claude-opus-5-5", effort="high", cost=2.0, redone=2),
        # Much cheaper by raw cost, but a statistically clear jump in the
        # redo rate (2/21 vs 14/19 rated) -- the ratio test with Holm
        # correction catches this even though the old tolerance-based
        # check (a flat 5-point band) would have too, by luck.
        *_setup_cycles(20, model="claude-haiku-4-5-20251001", effort="high", cost=0.2, redone=14),
    ])
    rows = {r["model"]: r for r in _rows(_table(habits.section_from(h), "habits_setups")) if r["level"] == "all"}
    assert rows["claude-haiku-4-5-20251001"]["verdict"] not in ("cheaper",)


def test_too_few_messages_on_either_side_name_no_cheaper_setup():
    few_usual = Habits(cycles=[
        *_setup_cycles(habits.MIN_GROUP - 1, model="claude-opus-5-5", effort="high", cost=2.0),
        *_setup_cycles(habits.MIN_GROUP - 2, model="claude-sonnet-5", effort="high", cost=1.0),
    ])
    few_cheaper = Habits(cycles=[
        *_setup_cycles(10, model="claude-opus-5-5", effort="high", cost=2.0),
        *_setup_cycles(habits.MIN_GROUP - 1, model="claude-sonnet-5", effort="high", cost=1.0),
    ])
    for h in (few_usual, few_cheaper):
        verdicts = [r["verdict"] for r in _rows(_table(habits.section_from(h), "habits_setups"))]
        assert "cheaper" not in verdicts and "usual" in verdicts


def test_untagged_messages_have_no_setup_rows():
    h = Habits(cycles=[_cycle(model="claude-opus-5-5", effort="high") for _ in range(10)])
    assert _table(habits.section_from(h), "habits_setups").rows == []


def test_a_setup_thats_mostly_hard_work_is_not_named_cheaper_at_the_all_level():
    h = Habits(cycles=[
        *_setup_cycles(25, model="claude-opus-5-5", effort="high", cost=2.0, level="normal"),
        # Cheap and never redone, but every message was hard work --
        # it looks like a good deal only because of what it was used
        # for. The hard-work veto (PROF-04) keeps it out of the mixed
        # "all" row even though nothing here would trip the ratio test.
        *_setup_cycles(20, model="claude-sonnet-5", effort="high", cost=0.5, level="hard"),
    ])
    rows = {r["model"]: r for r in _rows(_table(habits.section_from(h), "habits_setups")) if r["level"] == "all"}
    assert rows["claude-sonnet-5"]["verdict"] == "" and rows["claude-sonnet-5"]["saving_pct"] is None


def test_a_setup_with_only_some_hard_work_can_still_be_named_cheaper():
    h = Habits(cycles=[
        *_setup_cycles(25, model="claude-opus-5-5", effort="high", cost=2.0, level="normal"),
        # Same cheap setup, but only a quarter of its messages were
        # hard -- below the veto's 50% floor, so the ratio test (which
        # sees nothing wrong here) decides instead.
        *_setup_cycles(15, model="claude-sonnet-5", effort="high", cost=0.5, level="normal"),
        *_setup_cycles(5, model="claude-sonnet-5", effort="high", cost=0.5, level="hard"),
    ])
    rows = {r["model"]: r for r in _rows(_table(habits.section_from(h), "habits_setups")) if r["level"] == "all"}
    assert rows["claude-sonnet-5"]["verdict"] == "cheaper"
    assert rows["claude-sonnet-5"]["saving_pct"] == pytest.approx(75.0)

"""The "start building in a fresh session" tip (``handoff.py``): what the
replies after an approved plan would have cost had the build started
from the plan alone.

Hand-built turns use the packaged Sonnet 5 rates (cache_write_5m 2.5 and
cache_read 0.2 per million tokens), as ``test_carry.py`` does.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from claudeglass import fixes as fixes_mod, handoff, model
from claudeglass.handoff import (
    RULES,
    HandoffThresholds,
    build_section,
    compute_handoff,
    starting_context,
)
from claudeglass import habits
from claudeglass.habits import Habits, Piece, SessionShape
from claudeglass.model import EventKind, PlanStats, ReportModel, TranscriptMeta, TranscriptResult
from claudeglass.parse import parse_transcript
from claudeglass.pricing import load_pricing

from helpers import assert_privacy, tool_result_block, tool_use_block, turn_line, user_block_line, user_str_line, write_jsonl

PRICING = load_pricing()


def _turn(i: int, **overrides) -> model.Turn:
    fields = dict(message_id=f"msg_{i}", request_id=f"req_{i}", turn_index=i, model="claude-sonnet-5")
    fields.update(overrides)
    return model.Turn(**fields)


def _session(
    *,
    session_id="s1",
    later=10,
    plan_ctx=100_000,
    plan_chars=4_000,
    outcome="approved",
    compaction_at=None,
    kind="top-level",
    model_id="claude-sonnet-5",
) -> TranscriptResult:
    """A session that starts at 10,000 tokens, plans up to ``plan_ctx``,
    has the plan approved, then runs ``later`` replies that each read
    ``plan_ctx`` tokens from the cache."""
    turns = [_turn(1, ctx=10_000, cache_read_tokens=10_000, human_prompt_chars=0, model=model_id)]
    turns.append(
        _turn(
            2,
            ctx=plan_ctx,
            cache_read_tokens=plan_ctx,
            plan_stats=PlanStats(steps=3, chars=plan_chars, outcome=outcome),
            model=model_id,
        )
    )
    for n in range(later):
        i = 3 + n
        kinds = (EventKind.COMPACT_BOUNDARY,) if compaction_at == i else ()
        turns.append(_turn(i, ctx=plan_ctx, cache_read_tokens=plan_ctx, preceding_event_kinds=kinds, model=model_id))
    meta = TranscriptMeta(session_id=session_id, kind=kind, agent_type=None if kind == "top-level" else "Explore")
    return TranscriptResult(meta=meta, turns=turns)


def test_the_saving_takes_off_the_fresh_cache_write_and_the_allowance():
    stats = compute_handoff([_session()], PRICING, rediscovery_allowance_usd=0.01)
    [plan] = stats.plans
    # Fresh start: 10,000 + 4,000 / 4 = 11,000 tokens, so 89,000 dropped.
    assert plan.tokens_carried == 89_000
    assert plan.later_turns == 10
    assert plan.qualifies
    # 10 replies each read 89,000 fewer tokens at $0.20 per million, less
    # one cache write of the 11,000-token fresh start ($2.50 - $0.20 per
    # million) and the $0.01 allowance.
    expected = 10 * 89_000 * 0.2e-6 - 11_000 * 2.3e-6 - 0.01
    assert plan.saving_usd == pytest.approx(expected)
    [row] = stats.sessions
    assert row.saving_usd == pytest.approx(expected)
    assert row.qualifying_plans == 1


def test_a_rejected_plan_counts_for_nothing():
    stats = compute_handoff([_session(outcome="rejected")], PRICING)
    assert stats.plans == [] and stats.sessions == []
    assert stats.main_sessions == 1


def test_a_plan_approved_by_a_message_counts_like_one_approved_in_the_dialog():
    by_dialog = compute_handoff([_session()], PRICING)
    by_message = compute_handoff([_session(outcome="approved_by_message")], PRICING)
    [plan] = by_message.plans
    assert plan.qualifies and plan.later_turns == 10
    assert plan.saving_usd == pytest.approx(by_dialog.plans[0].saving_usd)


def _sent_back_session(tmp_path: Path, after: list[dict]) -> TranscriptResult:
    """A long exploration, a plan the dialog sent back with feedback, what
    you did next (``after``), then twelve build replies."""
    lines = [user_str_line("Plan the change", origin={"kind": "human"})]
    lines.append(turn_line(cache_read_input_tokens=15_000, input_tokens=10))
    for n in range(8):
        lines.append(turn_line(cache_read_input_tokens=15_000 + 12_000 * (n + 1), input_tokens=10))
    lines.append(
        turn_line(
            cache_read_input_tokens=120_000,
            input_tokens=10,
            content=[tool_use_block("ExitPlanMode", "tu_plan", {"plan": "1. Edit a\n2. Edit b\n" + "x" * 3_000})],
        )
    )
    lines.append(
        user_block_line(
            [tool_result_block(
                "tu_plan",
                "The user doesn't want to proceed with this tool use. To tell you how to proceed, the user said:\n"
                "keep the old name for the helper",
                is_error=True,
            )],
            toolDenialKind="user-rejected",
        )
    )
    lines.extend(after)
    for _ in range(12):
        lines.append(turn_line(cache_read_input_tokens=122_000, input_tokens=10))
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    return parse_transcript(path, TranscriptMeta(path=str(path), session_id="parsed"))


def test_a_go_ahead_you_type_after_the_dialog_sent_the_plan_back_makes_it_an_approved_plan(tmp_path: Path):
    stats = compute_handoff([_sent_back_session(tmp_path, [user_str_line("go ahead", origin={"kind": "human"})])], PRICING)
    [plan] = stats.plans
    assert plan.qualifies and plan.later_turns == 12 and plan.tokens_carried > 100_000


def test_a_plan_the_dialog_sent_back_and_you_never_approved_counts_for_nothing(tmp_path: Path):
    stats = compute_handoff([_sent_back_session(tmp_path, [])], PRICING)
    assert stats.plans == [] and stats.sessions == []


def test_the_window_ends_at_a_conversation_summary():
    stats = compute_handoff([_session(later=14, compaction_at=9)], PRICING)
    # Replies 3 to 8 only: the summary before reply 9 already dropped it.
    assert stats.plans[0].later_turns == 6
    assert not stats.plans[0].qualifies
    assert stats.sessions[0].saving_usd == 0.0


def test_small_plans_and_short_builds_do_not_count():
    assert not compute_handoff([_session(later=9)], PRICING).plans[0].qualifies
    small = compute_handoff([_session(plan_ctx=45_000)], PRICING).plans[0]
    assert small.tokens_carried == 34_000 and not small.qualifies
    loose = HandoffThresholds(min_dropped_tokens=30_000, min_later_turns=5)
    assert compute_handoff([_session(plan_ctx=45_000)], PRICING, loose).plans[0].qualifies


def test_subagents_are_left_out():
    stats = compute_handoff([_session(kind="subagent")], PRICING)
    assert stats.main_sessions == 0 and stats.plans == []


def test_the_build_is_priced_at_sonnet_too():
    stats = compute_handoff([_session(model_id="claude-opus-5")], PRICING)
    [row] = stats.sessions
    assert row.build_turns == 10
    assert row.build_usd_sonnet is not None
    assert 0 < row.build_usd_sonnet < row.build_usd


def test_starting_context_leaves_out_the_first_message():
    turns = [_turn(1, ctx=20_000, human_prompt_chars=4_000)]
    assert starting_context(turns) == 19_000
    assert starting_context([]) == 0


def test_the_section_tables_and_privacy():
    stats = compute_handoff([_session(session_id=f"s{i}") for i in range(3)], PRICING)
    section = build_section(stats)
    assert section.key == "plan_handoff"
    assert [t.name for t in section.tables] == ["plan_handoff_summary", "plan_handoff_by_session"]
    summary = {c.key: v for c, v in zip(section.tables[0].columns, section.tables[0].rows[0])}
    assert summary["qualifying_sessions"] == 3
    assert summary["tokens_carried_median"] == 89_000
    assert summary["saving_pct"] > 0
    assert len(section.tables[1].rows) == 3
    assert_privacy(section)


def _report(sessions: int, *, answers=(), costly=0, plans=(), checks=None, **kw) -> ReportModel:
    """``answers``: /cg-feedback handoff words on planned-and-built
    pieces; ``costly``: how many of them said too costly; ``plans``: the
    plan question's words on pieces of their own; ``checks``: plan checks
    by ``(shape, word)``."""
    stats = compute_handoff([_session(session_id=f"s{i}", **kw) for i in range(sessions)], PRICING)
    sections = [build_section(stats)]
    if answers or plans or checks:
        pieces = [
            Piece(outcome="met", cost=1.0, cycles=1, task=None, slow=(), helped=(), source="your feedback",
                  shape="plan_build", worth="no" if n < costly else "yes", handoff=word)
            for n, word in enumerate(answers)
        ] + [
            Piece(outcome="met", cost=1.0, cycles=1, task=None, slow=(), helped=(), source="your feedback",
                  shape="plan_build", plan=word)
            for word in plans
        ]
        h = Habits(
            pieces=pieces, shapes=[SessionShape("plan_build", 1.0, 80_000) for _ in range(max(len(pieces), 1))],
            plan_checks=Counter(checks or {}),
        )
        sections.append(habits.section_from(h))
    return ReportModel(sections=sections)


def test_the_rule_fires_on_three_sessions_and_says_clear_not_fork():
    [rec] = RULES[0](_report(3), HandoffThresholds())
    assert rec.id == "plan-handoff"
    assert rec.lever is None and rec.changes == []
    assert rec.saving_usd > 0
    assert "/clear" in rec.action and "/branch" in rec.action
    assert "89,000 tokens" in rec.why
    for _label, _value, source, row_key in rec.evidence:
        assert source == "plan_handoff.plan_handoff_summary" and row_key == "main sessions"


def test_feedback_that_the_build_needed_the_discussion_asks_for_fuller_plans():
    [rec] = RULES[0](_report(3, answers=("no", "no", "partly")), HandoffThresholds())
    assert rec.title == "Write fuller plans, then build in a fresh session"
    assert "You said 2 of 3 builds relied on the earlier discussion" in rec.why
    assert "decisions, file paths and constraints" in rec.action and "/branch" in rec.action
    assert ("Builds you said needed the discussion", 2, "habits.habits_by_shape", "plan_build") in rec.evidence


def test_feedback_that_the_plan_was_enough_is_cited():
    [rec] = RULES[0](_report(3, answers=("yes", "yes", "yes", "no")), HandoffThresholds())
    assert rec.title.startswith("Start building in a fresh session")
    assert "You said 3 of 4 builds could have started from the plan." in rec.why


def test_too_few_answers_leave_the_card_as_it_was():
    plain = RULES[0](_report(3), HandoffThresholds())[0]
    [rec] = RULES[0](_report(3, answers=("no", "no")), HandoffThresholds())
    assert (rec.title, rec.why, rec.action) == (plain.title, plain.why, plain.action)


def test_planned_builds_you_said_were_too_costly_are_cited():
    [rec] = RULES[0](_report(3, answers=("yes", "partly", "yes"), costly=2), HandoffThresholds())
    assert "67% of the planned builds you rated cost too many tokens" in rec.why


def test_the_feedback_counts_are_read_from_the_plan_build_row():
    fb = handoff._feedback_on_plans(
        _report(3, answers=("yes", "no"), plans=("gap", "gap", "new"), checks={("plan_build", "covered"): 2,
                                                                              ("no_plan", "gap"): 9})
    )
    assert (fb["yes"], fb["partly"], fb["no"], fb["answers"]) == (1, 0, 1, 2)
    # The plan question and the plan check count together; a session with no plan has none to count.
    assert (fb["covered"], fb["gap"], fb["new"], fb["plan_answers"]) == (2, 2, 1, 5)
    empty = handoff._feedback_on_plans(ReportModel())
    assert empty["plan_answers"] == 0 and empty["answers"] == 0 and empty["pieces"] == 0


def test_fixes_the_plan_left_out_ask_for_fuller_plans():
    [rec] = RULES[0](_report(3, plans=("gap", "gap", "covered")), HandoffThresholds())
    assert rec.title == "Write fuller plans, then build in a fresh session" and rec.variant == "fuller_plans"
    assert "You said 2 of 3 fixes after a plan were things it left out" in rec.why
    assert "relied on the earlier discussion" not in rec.why
    assert "decisions, file paths and constraints" in rec.action
    assert ("Fixes after a plan that it left out", 2, "habits.habits_by_shape", "plan_build") in rec.evidence
    assert ("Fixes after a plan you answered for", 3, "habits.habits_by_shape", "plan_build") in rec.evidence
    # What you tell Claude to do is the fuller-plans prompt, not the plain reminder.
    (fix,) = fixes_mod.build_fixes(rec)
    assert "decisions, file paths and constraints" in fix["prompt"] and "done-when" in fix["prompt"]
    plain = fixes_mod.build_fixes(RULES[0](_report(3), HandoffThresholds())[0])[0]
    assert "decisions, file paths and constraints" not in plain["prompt"]


def test_the_plan_check_counts_as_much_as_the_plan_question_in_telling_a_thin_plan():
    [rec] = RULES[0](_report(3, checks={("plan_build", "gap"): 3}), HandoffThresholds())
    assert rec.variant == "fuller_plans" and "3 of 3 fixes after a plan were things it left out" in rec.why
    [mixed] = RULES[0](_report(3, plans=("gap",), checks={("plan_build", "gap"): 2, ("plan_build", "covered"): 1}),
                       HandoffThresholds())
    assert "3 of 4 fixes after a plan were things it left out" in mixed.why


@pytest.mark.parametrize(
    "plans",
    [
        # Half is not more than half.
        ("gap", "gap", "covered", "new"),
        ("covered", "covered", "new"),
        # Fewer than three answers.
        ("gap", "gap"),
    ],
)
def test_a_plan_that_left_things_out_only_sometimes_leaves_the_card_as_it_was(plans):
    plain = RULES[0](_report(3), HandoffThresholds())[0]
    [rec] = RULES[0](_report(3, plans=plans), HandoffThresholds())
    assert (rec.title, rec.why, rec.action, rec.variant) == (plain.title, plain.why, plain.action, "")
    assert not any(label.startswith("Fixes after a plan") for label, *_ in rec.evidence)


def test_builds_that_needed_the_discussion_and_a_thin_plan_say_so_once_each():
    [rec] = RULES[0](_report(3, answers=("no", "no", "no"), plans=("gap", "gap", "gap")), HandoffThresholds())
    assert rec.variant == "fuller_plans" and rec.title == "Write fuller plans, then build in a fresh session"
    assert rec.why.count("You said") == 2
    assert "3 of 3 builds relied on the earlier discussion" in rec.why
    assert "3 of 3 fixes after a plan were things it left out" in rec.why
    # The older answers alone keep the card's variant too.
    [discussed] = RULES[0](_report(3, answers=("no", "no", "partly")), HandoffThresholds())
    assert discussed.variant == "fuller_plans"


def test_a_plan_that_was_enough_says_to_clear_when_the_plan_is_approved():
    [rec] = RULES[0](_report(3, answers=("yes", "yes", "yes", "no")), HandoffThresholds())
    plain = RULES[0](_report(3), HandoffThresholds())[0]
    assert rec.variant == "" and rec.action != plain.action
    assert rec.action.startswith("Approve the plan, then run /clear")
    assert "plan file" in rec.action and "build one phase per session" in rec.action
    # A plan that did not need the discussion, with too few answers, keeps the plain advice.
    [few] = RULES[0](_report(3, answers=("yes", "yes")), HandoffThresholds())
    assert few.action == plain.action


def test_the_rule_needs_three_sessions_and_a_saving_share():
    assert RULES[0](_report(2), HandoffThresholds()) == []
    assert RULES[0](_report(3), HandoffThresholds(min_saving_share_pct=99.0)) == []
    assert RULES[0](ReportModel(), HandoffThresholds()) == []


def test_thresholds_read_their_prefixed_keys():
    th = HandoffThresholds.from_config({"plan_handoff_min_sessions": 5, "plan_handoff_min_dropped_tokens": "x", "top_n": 3})
    assert th.min_sessions == 5
    assert th.min_dropped_tokens == 40_000.0
    assert th.top_n == 20


def test_a_parsed_session_with_an_approved_plan(tmp_path: Path):
    """End to end from a transcript: a long exploration, an approved
    ExitPlanMode, then twelve build replies."""
    lines = [user_str_line("Plan the change", origin={"kind": "human"})]
    lines.append(turn_line(cache_read_input_tokens=15_000, input_tokens=10))
    for n in range(8):
        lines.append(turn_line(cache_read_input_tokens=15_000 + 12_000 * (n + 1), input_tokens=10))
    lines.append(
        turn_line(
            cache_read_input_tokens=120_000,
            input_tokens=10,
            content=[tool_use_block("ExitPlanMode", "tu_plan", {"plan": "1. Edit a\n2. Edit b\n" + "x" * 3_000})],
        )
    )
    lines.append(user_block_line([tool_result_block("tu_plan", "User has approved your plan.")]))
    for _ in range(12):
        lines.append(turn_line(cache_read_input_tokens=122_000, input_tokens=10))
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path), session_id="parsed"))

    stats = compute_handoff([result], PRICING)
    [plan] = stats.plans
    assert plan.qualifies
    assert plan.later_turns == 12
    assert plan.tokens_carried > 100_000
    assert stats.sessions[0].saving_usd > 0

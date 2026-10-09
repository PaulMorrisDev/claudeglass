"""The "start building in a fresh session" tip (``handoff.py``): what the
replies after an approved plan would have cost had the build started
from the plan alone.

Hand-built turns use the packaged Sonnet 5 rates (cache_write_5m 2.5 and
cache_read 0.2 per million tokens), as ``test_carry.py`` does.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
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
from claudeglass.model import Event, EventKind, PlanStats, ReportModel, TranscriptMeta, TranscriptResult
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
    assert [t.name for t in section.tables] == [
        "plan_handoff_summary", "plan_handoff_by_session", "plan_handoff_approvals"
    ]
    summary = {c.key: v for c, v in zip(section.tables[0].columns, section.tables[0].rows[0])}
    assert summary["qualifying_sessions"] == 3
    assert summary["tokens_carried_median"] == 89_000
    assert summary["saving_pct"] > 0
    assert len(section.tables[1].rows) == 3
    assert_privacy(section)


# -- plan rounds, and how the build began -------------------------------------------

_T0 = datetime(2026, 9, 18, 10, 0, 0, tzinfo=timezone.utc)


def _at(seconds: float) -> str:
    return (_T0 + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _reply(i: int, at: float, **fields) -> model.Turn:
    fields.setdefault("ctx", 100_000)
    fields.setdefault("cache_read_tokens", fields["ctx"])
    return _turn(i, ts=_at(at), **fields)


def _plan_turn(i: int, outcome: str | None, *, at: float = 0.0, approved: float | None = None, **plan) -> model.Turn:
    """A reply that put up a plan; ``approved`` is when the line that approved
    it was written, in seconds from the start of the day's tests."""
    fields = dict(steps=3, files=2, chars=4_000, outcome=outcome, rejected=outcome == "rejected")
    fields.update(plan)
    stats = PlanStats(approved_ts=_at(approved) if approved is not None else "", **fields)
    return _reply(i, at, plan_stats=stats)


def _result(turns, *, session_id="s1", project="proj", events=()) -> TranscriptResult:
    meta = TranscriptMeta(session_id=session_id, kind="top-level", project_slug=project)
    return TranscriptResult(meta=meta, turns=list(turns), events=list(events))


def _clear(at: float) -> Event:
    return Event(kind=EventKind.SLASH_COMMAND, ts=_at(at), detail={"command": "clear"})


def _planned(session_id="s1", *, approved=600.0, outcome="approved", later=4, project="proj", events=()):
    """A session that starts at 10,000 tokens, puts up a 4,000-character plan
    at 100,000, has it approved at ``approved`` seconds, then runs ``later``
    build replies."""
    turns = [
        _reply(1, approved - 100, ctx=10_000),
        _plan_turn(2, outcome, at=approved - 10, approved=approved),
        *(_reply(3 + n, approved + 5 + 10 * n) for n in range(later)),
    ]
    return _result(turns, session_id=session_id, project=project, events=events)


def _next(session_id="s2", opened=0.0, *, project="proj", handoff_first=False, events=(), replies=5):
    """A session that starts at 11,000 tokens, as a build from a plan does."""
    turns = [
        _reply(1, opened, ctx=11_000, human_plan_handoff=handoff_first),
        *(_reply(2 + n, opened + 10 * (n + 1), ctx=11_000) for n in range(replies - 1)),
    ]
    return _result(turns, session_id=session_id, project=project, events=events)


def test_a_plan_sent_back_twice_is_three_versions_of_one_ask():
    turns = [
        _reply(1, 0, ctx=10_000),
        _plan_turn(2, "rejected", at=10, feedback_class="question"),
        _reply(3, 20),
        _plan_turn(4, "rejected", at=30, feedback_class="critique", chars=5_000),
        _reply(5, 40),
        _plan_turn(6, "approved", at=50, approved=55, chars=6_000, steps=5, files=4),
        _reply(7, 60),
    ]
    [group] = handoff.plan_groups(turns)
    assert (group.versions, group.sent_back, group.approval, group.typed) == (3, 2, 5, False)
    assert group.calls == (1, 3, 5)
    assert group.feedback == ("question", "critique")
    assert (group.steps, group.files) == (5, 4)
    # What the rounds cost: the replies after the first plan, through the approval.
    assert list(group.between) == [2, 3, 4, 5]


def test_a_plan_approved_when_first_put_up_has_no_rounds():
    [group] = handoff.plan_groups([_reply(1, 0), _plan_turn(2, "approved", approved=5), _reply(3, 10)])
    assert (group.versions, group.sent_back, group.approval) == (1, 0, 1)
    assert list(group.between) == []


def test_a_plan_that_was_never_approved_is_a_group_with_no_approval():
    [group] = handoff.plan_groups([_reply(1, 0), _plan_turn(2, "rejected"), _reply(3, 10), _plan_turn(4, "rejected")])
    assert (group.versions, group.sent_back, group.approval, group.typed) == (2, 2, None, False)
    assert group.end == 3


def test_a_decline_followed_by_a_typed_go_ahead_is_an_approval_not_a_plan_sent_back():
    # The dialog declined it, then "implement the plan" approved it: the parser keeps ``rejected``.
    turns = [_reply(1, 0), _plan_turn(2, "approved_by_message", rejected=True, approved=30), _reply(3, 40)]
    [group] = handoff.plan_groups(turns)
    assert (group.versions, group.sent_back, group.approval, group.typed) == (1, 0, 1, True)


def test_the_feedback_on_a_decline_that_a_go_ahead_followed_is_no_round_either():
    turns = [
        _reply(1, 0),
        _plan_turn(2, "rejected", at=10, feedback_class="question"),
        _reply(3, 20),
        _plan_turn(4, "approved_by_message", at=30, approved=60, rejected=True, feedback_class="critique", chars=5_000),
        _reply(5, 70),
    ]
    [group] = handoff.plan_groups(turns)
    # One plan sent back, with a question. The second was approved by typing, so its decline is no round.
    assert (group.versions, group.sent_back, group.feedback) == (2, 1, ("question",))


def test_a_plan_put_up_again_unchanged_after_a_typed_approval_is_the_same_plan():
    turns = [
        _reply(1, 0),
        _plan_turn(2, "approved_by_message", approved=30),
        _reply(3, 40),
        _plan_turn(4, "approved", at=50, approved=55),
        _reply(5, 60),
    ]
    [group] = handoff.plan_groups(turns)
    assert (group.versions, group.sent_back, group.approval, group.typed) == (1, 0, 1, True)
    assert group.calls == (1, 3)
    # A changed plan after the approval is another ask.
    changed = [*turns[:3], _plan_turn(4, "approved", at=50, approved=55, chars=9_000), _reply(5, 60)]
    assert len(handoff.plan_groups(changed)) == 2


def test_an_approval_ends_the_ask_and_the_next_plan_starts_another():
    turns = [
        _reply(1, 0),
        _plan_turn(2, "rejected"),
        _plan_turn(3, "approved", approved=20, chars=5_000),
        _reply(4, 30),
        _plan_turn(5, "approved", at=40, approved=45, chars=7_000),
    ]
    first, second = handoff.plan_groups(turns)
    assert (first.versions, first.sent_back, first.approval) == (2, 1, 2)
    assert (second.versions, second.sent_back, second.approval) == (1, 0, 4)


def test_edits_between_plans_start_another_ask_only_once_a_build_began():
    def edited(count: int) -> list[model.Turn]:
        return [
            _reply(1, 0),
            _plan_turn(2, "rejected"),
            *(_reply(3 + n, 10 * (n + 1), edit_kind="real") for n in range(count)),
            _plan_turn(3 + count, "approved", at=500, approved=505),
        ]

    # Claude edits its plan file between rounds: a few replies are one ask.
    assert len(handoff.plan_groups(edited(handoff.PLAN_BUILD_REPLIES))) == 1
    # More than that, and it built something without an approval: the next plan is a new ask.
    count = handoff.PLAN_BUILD_REPLIES + 1
    unapproved, approved = handoff.plan_groups(edited(count))
    assert (unapproved.approval, approved.approval, approved.sent_back) == (None, 2 + count, 0)


def test_a_session_without_a_plan_has_no_groups():
    assert handoff.plan_groups([_reply(1, 0), _reply(2, 10)]) == []


def test_a_typed_approval_counts_with_one_you_clicked():
    stats = compute_handoff([_planned("a"), _planned("b", outcome="approved_by_message")], PRICING)
    assert [(a.session_id, a.typed, a.start) for a in stats.approvals] == [("a", False, "kept"), ("b", True, "kept")]
    # An approval that stays in its session carries the planning context and keeps its replies.
    first = stats.approvals[0]
    assert first.tokens_carried == 89_000
    assert (first.build_turns, first.build_context) == (4, 400_000)
    assert first.build_usd > 0


def test_a_decline_followed_by_implement_the_plan_is_an_approval(tmp_path: Path):
    session = _sent_back_session(tmp_path, [user_str_line("implement the plan", origin={"kind": "human"})])
    [plan] = [t.plan_stats for t in session.turns if t.plan_stats is not None]
    assert (plan.outcome, plan.rejected) == ("approved_by_message", True)
    stats = compute_handoff([session], PRICING)
    [approval] = stats.approvals
    assert approval.typed and approval.start == "kept" and approval.build_turns == 12
    [group] = handoff.plan_groups([t for t in session.turns if t.turn_index > 0])
    assert (group.versions, group.sent_back, group.typed) == (1, 0, True)
    # The same decline with no go-ahead is a plan sent back, and nothing was approved.
    declined = _sent_back_session(tmp_path, [])
    [group] = handoff.plan_groups([t for t in declined.turns if t.turn_index > 0])
    assert (group.sent_back, group.approval) == (1, None)
    assert compute_handoff([declined], PRICING).approvals == []


def test_a_clear_within_a_minute_of_the_approval_starts_the_build_fresh():
    quick = compute_handoff([_planned(events=[_clear(630)])], PRICING)
    [approval] = quick.approvals
    assert approval.start == "cleared" and approval.tokens_carried == 0
    # A minute and a second later it is something else you cleared for.
    late = compute_handoff([_planned(events=[_clear(661)])], PRICING)
    assert [(a.start, a.tokens_carried) for a in late.approvals] == [("kept", 89_000)]
    # A clear before the approval is no part of it either.
    before = compute_handoff([_planned(events=[_clear(500)])], PRICING)
    assert before.approvals[0].start == "kept"
    exact = compute_handoff([_planned(events=[_clear(600 + handoff.FRESH_CLEAR_S)])], PRICING)
    assert exact.approvals[0].start == "cleared"


def test_an_approval_without_a_time_cannot_be_matched_to_a_clear():
    session = _planned(events=[_clear(610)])
    session.turns[1].plan_stats.approved_ts = ""
    assert compute_handoff([session], PRICING).approvals[0].start == "kept"


def test_a_session_that_opens_with_a_clear_is_the_fresh_build_of_the_plan_before_it():
    planning = _planned("planning", approved=600, later=1)
    build = _next("build", 625, events=[_clear(620)], replies=5)
    stats = compute_handoff([planning, build], PRICING)
    [approval] = stats.approvals
    assert (approval.start, approval.tokens_carried) == ("cleared", 0)
    # Its build is the next session's replies, not the one reply left in the planning session.
    assert (approval.build_turns, approval.build_context) == (5, 55_000)
    # A clear that came more than a minute after the approval is another job.
    other = compute_handoff([planning, _next("build", 725, events=[_clear(720)])], PRICING)
    assert other.approvals[0].start == "kept" and other.approvals[0].build_turns == 1


def test_a_session_that_opens_with_the_plan_is_the_fresh_build_of_the_plan_before_it():
    planning = _planned("planning", approved=600, later=1)
    stats = compute_handoff([planning, _next("build", 660, handoff_first=True, replies=3)], PRICING)
    [approval] = stats.approvals
    assert (approval.start, approval.tokens_carried, approval.build_turns) == ("handoff", 0, 3)
    # Within the hour, not after it, and not in another project.
    hour = compute_handoff([planning, _next("build", 600 + handoff.HANDOFF_LINK_S, handoff_first=True)], PRICING)
    assert hour.approvals[0].start == "handoff"
    late = compute_handoff([planning, _next("build", 601 + handoff.HANDOFF_LINK_S, handoff_first=True)], PRICING)
    assert late.approvals[0].start == "kept"
    elsewhere = compute_handoff([planning, _next("build", 660, project="other", handoff_first=True)], PRICING)
    assert elsewhere.approvals[0].start == "kept"
    unnamed = compute_handoff([_planned("planning", project="", later=1), _next("build", 660, project="", handoff_first=True)], PRICING)
    assert unnamed.approvals[0].start == "kept"
    # A session that does not open with the plan is not a handoff.
    assert compute_handoff([planning, _next("build", 660)], PRICING).approvals[0].start == "kept"


def test_a_fresh_session_is_the_build_of_one_approval_and_an_approval_has_one_build():
    first, second = _planned("a", approved=600, later=1), _planned("b", approved=700, later=1)
    builds = [_next("x", 720, handoff_first=True, replies=2), _next("y", 740, handoff_first=True, replies=6)]
    stats = compute_handoff([first, second, *builds], PRICING)
    by_session = {a.session_id: a for a in stats.approvals}
    # The nearer approval takes the first build; the other takes the second.
    assert by_session["b"].build_turns == 2 and by_session["a"].build_turns == 6
    assert {a.start for a in stats.approvals} == {"handoff"}
    # One approval and two sessions that open with the plan: the second finds nothing left.
    only = compute_handoff([_planned("a", approved=600, later=1), *builds], PRICING)
    assert [(a.start, a.build_turns) for a in only.approvals] == [("handoff", 2)]


def test_the_approvals_table_compares_how_the_builds_began():
    kept = [_planned(f"k{n}", approved=1_000 * n + 600, later=4) for n in range(3)]
    cleared = _planned("c", approved=20_000, later=1, events=[_clear(20_030)])
    cleared.turns[2:] = [_reply(3, 20_040, ctx=11_000), _reply(4, 20_050, ctx=11_000)]
    stats = compute_handoff([*kept, cleared], PRICING)
    table = build_section(stats).tables[2]
    assert table.name == "plan_handoff_approvals"
    rows = {row[0]: dict(zip((c.key for c in table.columns), row)) for row in table.rows}
    assert list(rows) == ["kept", "cleared"]
    assert (rows["kept"]["approvals"], rows["kept"]["typed"], rows["kept"]["tokens_carried"]) == (3, 0, 89_000)
    assert (rows["kept"]["build_turns"], rows["kept"]["avg_context"]) == (12, 100_000)
    assert (rows["cleared"]["approvals"], rows["cleared"]["tokens_carried"]) == (1, 0)
    assert (rows["cleared"]["build_turns"], rows["cleared"]["avg_context"]) == (2, 11_000)
    # The fresh build reads far less on each reply, and costs less for it.
    assert rows["cleared"]["usd_per_reply"] < rows["kept"]["usd_per_reply"]
    assert rows["kept"]["build_usd"] == pytest.approx(rows["kept"]["usd_per_reply"] * 12)
    assert_privacy(build_section(stats))


def test_the_approvals_table_has_no_rows_without_an_approved_plan():
    stats = compute_handoff([_planned(outcome="rejected")], PRICING)
    assert stats.approvals == [] and build_section(stats).tables[2].rows == []


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

"""Tests for ``waste.py``: wasted-turn cause detection (tool-error,
interrupt, tool-denial, max-turns, api-error-retry count-only,
limit-pause exclusion), the ``waste`` report section's accumulation/
rendering, and the ``wasted-turns`` recommendation rule.

Fixtures are built with ``turn_line``/``user_str_line``/
``user_block_line``/``tool_use_block``/``tool_result_block``/
``system_line`` (never real transcript text — see tests/helpers.py and
SECURITY.md).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claudeglass import waste
from claudeglass.model import Column, Recommendation, ReportModel, Section, Table, TranscriptMeta
from claudeglass.parse import parse_transcript
from claudeglass.pricing import ModelRates, Pricing
from claudeglass.units import Units

from helpers import (
    assert_privacy,
    elasticity_with_slope,
    system_line,
    tool_result_block,
    tool_use_block,
    turn_line,
    user_block_line,
    user_str_line,
    write_jsonl,
)


# -- fixtures --------------------------------------------------------------


def _pricing() -> Pricing:
    """A minimal rate card with every component priced at $1 per million
    tokens, so a turn's cost is simply its own token total / 1e6 -- easy
    to hand-compute."""
    rates = ModelRates(
        canonical_id="claude-widget-9",
        input=1.0,
        output=1.0,
        cache_write_5m=1.0,
        cache_write_1h=1.0,
        cache_read=1.0,
    )
    return Pricing(
        path="<test>",
        version="test-2026",
        currency="USD",
        source_url=None,
        retrieved=None,
        notes=None,
        sha256="0" * 64,
        models={"claude-widget-9": rates},
    )


_MODEL = "claude-widget-9"


def _parse(tmp_path: Path, lines: list[dict], name: str = "session.jsonl", **meta_kwargs) -> object:
    path = tmp_path / name
    write_jsonl(path, lines)
    meta_kwargs.setdefault("session_id", "sess1")
    return parse_transcript(path, TranscriptMeta(path=str(path), **meta_kwargs))


# -- tool-error --------------------------------------------------------------


def test_tool_error_cause_detected_and_priced(tmp_path: Path):
    lines = [
        turn_line(
            message_id="msg_1",
            model=_MODEL,
            input_tokens=1_000_000,
            output_tokens=0,
            content=[tool_use_block("Bash", "tu_a", {"command": "ls /nope"})],
        ),
        user_block_line([tool_result_block("tu_a", "no such directory", is_error=True)]),
    ]
    result = _parse(tmp_path, lines)

    stats = waste.WasteStats(config_dir=tmp_path / "cfg")
    stats.add(result, _pricing())

    assert stats._by_cause["tool-error"].turns == 1
    assert stats._by_cause["tool-error"].cost_usd == pytest.approx(1.0)  # 1M input tokens @ $1/M
    assert stats._by_cause["tool-error"].tokens == 1_000_000
    assert stats.wasted_turns == 1
    assert stats.wasted_cost_usd == pytest.approx(1.0)


def test_tool_error_absent_when_no_is_error_result(tmp_path: Path):
    lines = [
        turn_line(
            message_id="msg_1",
            model=_MODEL,
            content=[tool_use_block("Bash", "tu_a", {"command": "ls"})],
        ),
        user_block_line([tool_result_block("tu_a", "file1\nfile2")]),
    ]
    result = _parse(tmp_path, lines)

    stats = waste.WasteStats(config_dir=tmp_path / "cfg")
    stats.add(result, _pricing())

    assert stats.wasted_turns == 0
    assert stats.wasted_cost_usd == 0.0


def test_tool_error_kinds_come_from_the_start_of_the_error_text():
    from claudeglass.parse import _tool_error_kind

    assert _tool_error_kind("PreToolUse:Read hook error: [pwsh guard.ps1]: first Read must use the index") == "blocked"
    assert _tool_error_kind("<tool_use_error>Blocked: sleep 90 followed by: cat out.txt</tool_use_error>") == "blocked"
    assert _tool_error_kind("The user doesn't want to proceed with this tool use.") == "denied"
    assert _tool_error_kind("Permission to use Bash with command git push has been denied.") == "denied"
    assert _tool_error_kind("Exit code 1\nFAILED tests/test_api.py::test_login - AssertionError") == "failed"
    assert _tool_error_kind([{"type": "text", "text": "Exit code 143\nCommand timed out after 2m 0s"}]) == "failed"
    assert _tool_error_kind("Exit code 2\n/usr/bin/bash: -c: line 3: unexpected EOF while looking for `'") == "misfire"
    assert _tool_error_kind("Exit code 1\n  File \"<stdin>\", line 5\nKeyError: 'x'") == "misfire"
    assert _tool_error_kind("<tool_use_error>String to replace not found in file.</tool_use_error>") == "misfire"


def _one_error_turn(tmp_path: Path, text: str):
    lines = [
        turn_line(message_id="msg_1", model=_MODEL, input_tokens=1_000_000, output_tokens=0,
                  content=[tool_use_block("Bash", "tu_a", {"command": "pytest"})]),
        user_block_line([tool_result_block("tu_a", text, is_error=True)]),
    ]
    stats = waste.WasteStats(config_dir=tmp_path / "cfg")
    stats.add(_parse(tmp_path, lines), _pricing())
    return stats


def test_a_failing_test_run_is_work_not_waste(tmp_path: Path):
    stats = _one_error_turn(tmp_path, "Exit code 1\n3 failed, 40 passed")
    assert stats.wasted_turns == 0 and stats.failed_command_turns == 1
    summary = _table(waste.build_section(stats), "waste_summary")
    assert summary.rows[0][[c.key for c in summary.columns].index("failed_command_turns")] == 1


def test_a_hook_block_is_its_own_cause(tmp_path: Path):
    stats = _one_error_turn(tmp_path, "PreToolUse:Bash hook error: [guard.ps1]: BLOCKED: run the eval first")
    assert stats._by_cause["blocked"].turns == 1 and stats._by_cause["tool-error"].turns == 0
    assert "hook enforces" in waste.LEVERS["blocked"]


# -- redirected (known token savers) ------------------------------------------

#: A real tokensave v7.13.0 redirect message (Bash grep), reworded from a
#: scratch project's own transcript -- see docs/waste.md and
#: known_savers.py's own module docstring.
_TOKENSAVE_GREP_BLOCK = (
    "PreToolUse:Bash hook error: STOP: This Bash grep targets a code file in a tokensave-indexed project and "
    "the pattern `build_fixes` looks like a symbol name. Use tokensave_search (definition) or "
    "tokensave_callers_for (usages) instead -- symbol-indexed lookups are faster and more accurate than text "
    "grep. To override for this one call, set TOKENSAVE_DISABLE_GREP_HOOK=1 in the shell."
)


def test_redirect_cause_when_every_blocked_call_is_a_known_savers_redirect(tmp_path: Path):
    stats = _one_error_turn(tmp_path, _TOKENSAVE_GREP_BLOCK)
    assert stats._by_cause["blocked"].turns == 0
    assert stats.redirected_turns == 1
    assert stats.redirected_cost_usd == pytest.approx(1.0)
    # Never counted as wasted.
    assert stats.wasted_turns == 0
    assert stats.wasted_cost_usd == 0.0


def test_a_mix_of_redirect_and_a_real_block_in_one_turn_stays_blocked(tmp_path: Path):
    hook_text = "PreToolUse:Bash hook error: [guard.ps1]: BLOCKED: run the eval first"
    lines = [
        turn_line(
            message_id="msg_1", model=_MODEL, input_tokens=1_000_000, output_tokens=0,
            content=[tool_use_block("Bash", "tu_a", {"command": "grep build_fixes ."}),
                     tool_use_block("Bash", "tu_b", {"command": "run something"})],
        ),
        user_block_line([
            tool_result_block("tu_a", _TOKENSAVE_GREP_BLOCK, is_error=True),
            tool_result_block("tu_b", hook_text, is_error=True),
        ]),
    ]
    result = _parse(tmp_path, lines)
    stats = waste.WasteStats(config_dir=tmp_path / "cfg")
    stats.add(result, _pricing())
    assert stats._by_cause["blocked"].turns == 1
    assert stats.redirected_turns == 0


def test_redirected_is_priced_and_shown_but_never_wasted(tmp_path: Path):
    stats = _one_error_turn(tmp_path, _TOKENSAVE_GREP_BLOCK)
    section = waste.build_section(stats)

    by_cause = _table(section, "waste_by_cause")
    row = dict(zip([c.key for c in by_cause.columns], next(r for r in by_cause.rows if r[0] == "redirected")))
    assert row["turns"] == 1 and row["cost_usd"] == pytest.approx(1.0)
    assert "Not waste" in row["lever"]
    assert "redirected" not in waste.CAUSES

    summary = _table(section, "waste_summary")
    srow = dict(zip([c.key for c in summary.columns], summary.rows[0]))
    assert srow["wasted_turns"] == 0 and srow["wasted_cost_usd"] == 0.0
    assert srow["redirected_turns"] == 1 and srow["redirected_cost_usd"] == pytest.approx(1.0)


def test_rule_wasted_turns_ignores_redirected_cost(tmp_path: Path):
    """A window whose only blocked replies are redirects must not read
    as a problem: the wasted-turns rule never fires from redirected cost
    alone, however large a share of the window it is."""
    lines = [
        turn_line(
            message_id="msg_1", model=_MODEL, input_tokens=9_000_000, output_tokens=0,
            content=[tool_use_block("Bash", "tu_a", {"command": "grep build_fixes ."})],
        ),
        user_block_line([tool_result_block("tu_a", _TOKENSAVE_GREP_BLOCK, is_error=True)]),
        turn_line(message_id="msg_2", model=_MODEL, input_tokens=1_000_000, output_tokens=0),
    ]
    result = _parse(tmp_path, lines)
    stats = waste.WasteStats(config_dir=tmp_path / "cfg")
    stats.add(result, _pricing())
    section = waste.build_section(stats)
    report = _report_with_waste_section(section, sessions=10, priced_turns=500)

    th = waste.WasteThresholds(share_pct=10.0, min_sessions=5, min_turns=200)
    assert waste.RULES[0](report, th) == []


# -- waste_blocked_by ----------------------------------------------------------

_WORKTREE_BLOCK = (
    "This agent is isolated in the worktree /repo/.worktrees/impl-1, but this command names git in a form too "
    "complex to verify that it stays inside the worktree. Refusing to run it -- a worktree-isolated agent's "
    "git operations must target its own worktree. Split it into plain, separate commands and run them one at "
    "a time."
)
_SLEEP_CHAIN_BLOCK = (
    "<tool_use_error>Blocked: sleep 45 followed by: gh pr checks 674. To wait for a condition, use Monitor "
    "with an until-loop (e.g. `until <check>; do sleep 2; done`). To wait for a command you started, use "
    "run_in_background: true. Do not chain shorter sleeps to work around this block.</tool_use_error>"
)
_NAMED_HOOK_BLOCK = (
    "PreToolUse:Read hook error: [powershell -File .claude/hooks/index-first-guard.ps1]: index-first-guard: "
    "first Read of an indexed file must use the index."
)
_UNNAMED_HOOK_BLOCK = "PreToolUse:Bash hook error: nope"


def _blocked_turn(tmp_path: Path, text: str, *, kind="subagent", agent_type="claude-implementer") -> object:
    lines = [
        turn_line(message_id="msg_1", model=_MODEL, input_tokens=1_000_000, output_tokens=0,
                  content=[tool_use_block("Bash", "tu_a", {"command": "x"})]),
        user_block_line([tool_result_block("tu_a", text, is_error=True)]),
    ]
    result = _parse(tmp_path, lines, kind=kind, agent_type=agent_type)
    stats = waste.WasteStats(config_dir=tmp_path / "cfg")
    stats.add(result, _pricing())
    return waste.build_section(stats)


def _blocked_by_row(section, blocker: str) -> dict:
    table = _table(section, "waste_blocked_by")
    keys = [c.key for c in table.columns]
    return dict(zip(keys, next(r for r in table.rows if r[0] == blocker)))


def test_blocked_by_names_the_worktree_guard_and_its_lever(tmp_path: Path):
    section = _blocked_turn(tmp_path, _WORKTREE_BLOCK)
    row = _blocked_by_row(section, "Claude Code's worktree guard")
    assert (row["kind"], row["agent_type"], row["turns"]) == ("guard", "claude-implementer", 1)
    assert "run git as plain, separate commands" in row["lever"]


def test_blocked_by_names_the_sleep_chain_guard_and_its_lever(tmp_path: Path):
    section = _blocked_turn(tmp_path, _SLEEP_CHAIN_BLOCK)
    row = _blocked_by_row(section, "a Claude Code guard")
    assert row["kind"] == "guard"
    assert "Monitor" in row["lever"] and "run_in_background" in row["lever"]


def test_blocked_by_names_a_hook_by_its_script(tmp_path: Path):
    section = _blocked_turn(tmp_path, _NAMED_HOOK_BLOCK)
    row = _blocked_by_row(section, "index-first-guard.ps1")
    assert row["kind"] == "hook"
    assert "Put what index-first-guard.ps1 enforces into" in row["lever"]


def test_blocked_by_names_an_unnamed_hook(tmp_path: Path):
    section = _blocked_turn(tmp_path, _UNNAMED_HOOK_BLOCK)
    row = _blocked_by_row(section, "a hook that doesn't give its name")
    assert row["kind"] == "guard"
    assert "Check your hooks" in row["lever"]


def test_blocked_by_names_the_saver_with_a_not_waste_lever(tmp_path: Path):
    section = _blocked_turn(tmp_path, _TOKENSAVE_GREP_BLOCK, kind="top-level", agent_type=None)
    row = _blocked_by_row(section, "tokensave")
    assert (row["kind"], row["agent_type"]) == ("saver", "top-level")
    assert "Not waste: tokensave" in row["lever"] and "What does tokensave save you?" in row["lever"]


def test_blocked_by_leaves_the_saver_out_of_the_comparison_for_a_blocked_turn(tmp_path: Path):
    """A turn with a redirect mixed into a real block still counts as
    "blocked" (see test_a_mix_of_redirect_and_a_real_block_in_one_turn_
    stays_blocked), and its own blocked-by row is the hook or guard,
    never the saver mixed in alongside it."""
    hook_text = "PreToolUse:Bash hook error: [guard.ps1]: BLOCKED: run the eval first"
    lines = [
        turn_line(
            message_id="msg_1", model=_MODEL, input_tokens=1_000_000, output_tokens=0,
            content=[tool_use_block("Bash", "tu_a", {"command": "grep build_fixes ."}),
                     tool_use_block("Bash", "tu_b", {"command": "run something"})],
        ),
        user_block_line([
            tool_result_block("tu_a", _TOKENSAVE_GREP_BLOCK, is_error=True),
            tool_result_block("tu_b", hook_text, is_error=True),
        ]),
    ]
    result = _parse(tmp_path, lines)
    stats = waste.WasteStats(config_dir=tmp_path / "cfg")
    stats.add(result, _pricing())
    section = waste.build_section(stats)
    table = _table(section, "waste_blocked_by")
    assert [row[0] for row in table.rows] == ["guard.ps1"]


def test_blocked_by_sums_to_the_blocked_and_redirected_replies(tmp_path: Path):
    lines = [
        turn_line(message_id="msg_1", model=_MODEL, input_tokens=1_000_000, output_tokens=0,
                  content=[tool_use_block("Bash", "tu_a", {"command": "x"})]),
        user_block_line([tool_result_block("tu_a", _WORKTREE_BLOCK, is_error=True)]),
        turn_line(message_id="msg_2", model=_MODEL, input_tokens=1_000_000, output_tokens=0,
                  content=[tool_use_block("Bash", "tu_b", {"command": "grep build_fixes ."})]),
        user_block_line([tool_result_block("tu_b", _TOKENSAVE_GREP_BLOCK, is_error=True)]),
    ]
    result = _parse(tmp_path, lines, kind="subagent", agent_type="claude-implementer")
    stats = waste.WasteStats(config_dir=tmp_path / "cfg")
    stats.add(result, _pricing())
    section = waste.build_section(stats)
    table = _table(section, "waste_blocked_by")
    total = sum(row[_col(table, "turns")] for row in table.rows)
    assert total == stats._by_cause["blocked"].turns + stats.redirected_turns == 2


def test_blocked_by_sorted_by_cost_descending(tmp_path: Path):
    lines = [
        turn_line(message_id="msg_1", model=_MODEL, input_tokens=1_000_000, output_tokens=0,
                  content=[tool_use_block("Bash", "tu_a", {"command": "grep build_fixes ."})]),
        user_block_line([tool_result_block("tu_a", _TOKENSAVE_GREP_BLOCK, is_error=True)]),
        turn_line(message_id="msg_2", model=_MODEL, input_tokens=5_000_000, output_tokens=0,
                  content=[tool_use_block("Bash", "tu_b", {"command": "x"})]),
        user_block_line([tool_result_block("tu_b", _WORKTREE_BLOCK, is_error=True)]),
    ]
    result = _parse(tmp_path, lines, kind="subagent", agent_type="claude-implementer")
    stats = waste.WasteStats(config_dir=tmp_path / "cfg")
    stats.add(result, _pricing())
    section = waste.build_section(stats)
    table = _table(section, "waste_blocked_by")
    costs = [row[_col(table, "cost_usd")] for row in table.rows]
    assert costs == sorted(costs, reverse=True)
    assert table.rows[0][0] == "Claude Code's worktree guard"


def test_waste_blocked_by_passes_privacy_scan(tmp_path: Path):
    section = _blocked_turn(tmp_path, _NAMED_HOOK_BLOCK)
    assert_privacy(section)


# -- interrupt ---------------------------------------------------------------


def test_interrupt_cause_detected_on_the_turn_before_the_interrupt(tmp_path: Path):
    lines = [
        turn_line(message_id="msg_1", model=_MODEL, input_tokens=2_000_000, output_tokens=0),
        user_str_line("[Request interrupted by user]"),
        turn_line(message_id="msg_2", model=_MODEL, input_tokens=500_000, output_tokens=0),
    ]
    result = _parse(tmp_path, lines)

    stats = waste.WasteStats(config_dir=tmp_path / "cfg")
    stats.add(result, _pricing())

    # msg_1 (the turn the user cut off) is wasted, not msg_2 (the turn
    # that merely reports the interruption).
    assert stats._by_cause["interrupt"].turns == 1
    assert stats._by_cause["interrupt"].cost_usd == pytest.approx(2.0)
    assert stats._by_cause["tool-denial"].turns == 0


def test_interrupt_not_flagged_when_turn_has_no_next_turn(tmp_path: Path):
    lines = [turn_line(message_id="msg_1", model=_MODEL, input_tokens=1_000_000, output_tokens=0)]
    result = _parse(tmp_path, lines)

    stats = waste.WasteStats(config_dir=tmp_path / "cfg")
    stats.add(result, _pricing())

    assert stats.wasted_turns == 0


# -- tool-denial ---------------------------------------------------------------


def test_tool_denial_cause_detected_on_the_turn_before_the_denial(tmp_path: Path):
    lines = [
        turn_line(message_id="msg_1", model=_MODEL, input_tokens=3_000_000, output_tokens=0),
        user_str_line("(denied)", toolDenialKind="user-rejected"),
        turn_line(message_id="msg_2", model=_MODEL, input_tokens=100_000, output_tokens=0),
    ]
    result = _parse(tmp_path, lines)

    stats = waste.WasteStats(config_dir=tmp_path / "cfg")
    stats.add(result, _pricing())

    assert stats._by_cause["tool-denial"].turns == 1
    assert stats._by_cause["tool-denial"].cost_usd == pytest.approx(3.0)
    assert stats._by_cause["interrupt"].turns == 0


_SENT_BACK = (
    "The user doesn't want to proceed with this tool use. The tool use was rejected (eg. if it was a file edit, "
    "the new_string was NOT written to the file). To tell you how to proceed, the user said:\n"
)


def _answered_call(tmp_path: Path, tool: str, answer: str, kind: str, *, then: str | None = None) -> waste.WasteStats:
    """A 3M-token turn whose call came back turned away, an optional line
    after it (an interrupt), then the reply that follows."""
    given = {"plan": "1. do it"} if tool == "ExitPlanMode" else {"command": "ls"}
    lines = [
        turn_line(message_id="msg_1", model=_MODEL, input_tokens=3_000_000, output_tokens=0,
                  content=[tool_use_block(tool, "tu_1", given)]),
        user_block_line([tool_result_block("tu_1", answer, is_error=True)], toolDenialKind=kind),
        *([user_str_line(then)] if then else []),
        turn_line(message_id="msg_2", model=_MODEL, input_tokens=100_000, output_tokens=0),
    ]
    stats = waste.WasteStats(config_dir=tmp_path / "cfg")
    stats.add(_parse(tmp_path, lines), _pricing())
    return stats


def test_a_call_you_or_a_deny_rule_turned_down_is_a_tool_denial(tmp_path: Path):
    for tool, answer, kind in [
        ("Bash", "Permission to use Bash has been denied.", "permission-rule"),
        ("Edit", _SENT_BACK + "not that file", "user-rejected"),
    ]:
        stats = _answered_call(tmp_path, tool, answer, kind)
        assert stats._by_cause["tool-denial"].turns == 1, (tool, kind)
        assert stats._by_cause["tool-denial"].cost_usd == pytest.approx(3.0)


@pytest.mark.parametrize("tool, answer, kind", [
    ("ExitPlanMode", _SENT_BACK + "make step two smaller", "user-rejected"),
    ("ExitPlanMode", _SENT_BACK, "user-rejected"),
    ("AskUserQuestion", _SENT_BACK, "user-rejected"),
    ("Bash", "PreToolUse:Bash hook error: [guard.sh] STOP: no rm", "permission-rule"),
    ("Bash", "Blocked by the auto mode classifier", "automode-blocked"),
    ("Bash", "The server-side auto mode classifier gave no verdict", "automode-unavailable"),
    ("Bash", "Permission to use Bash has been denied.", "interrupted"),
])
def test_a_plan_or_question_answer_or_a_block_is_not_a_tool_denial(tmp_path: Path, tool, answer, kind):
    stats = _answered_call(tmp_path, tool, answer, kind)
    assert stats._by_cause["tool-denial"].turns == 0
    assert stats._by_cause["interrupt"].turns == 0


def test_a_plan_with_feedback_is_not_a_tool_denial_even_beside_a_refused_call(tmp_path: Path):
    # The feedback event outranks the denial, so the plan's answer is what
    # the next reply followed.
    lines = [
        turn_line(message_id="msg_1", model=_MODEL, input_tokens=3_000_000, output_tokens=0,
                  content=[tool_use_block("ExitPlanMode", "tu_p", {"plan": "1. do it"})]),
        user_block_line([tool_result_block("tu_p", _SENT_BACK + "why not reuse the cache?", is_error=True)],
                        toolDenialKind="user-rejected"),
        turn_line(message_id="msg_2", model=_MODEL, input_tokens=100_000, output_tokens=0),
    ]
    stats = waste.WasteStats(config_dir=tmp_path / "cfg")
    stats.add(_parse(tmp_path, lines), _pricing())
    assert stats._by_cause["tool-denial"].turns == 0


def test_a_tool_use_interrupt_after_a_refused_call_or_a_closed_dialog_is_still_an_interrupt(tmp_path: Path):
    for tool, answer, kind in [
        ("Bash", "Permission to use Bash has been denied.", "permission-rule"),
        ("Bash", "Permission to use Bash has been denied.", "interrupted"),
    ]:
        stats = _answered_call(tmp_path, tool, answer, kind, then="[Request interrupted by user for tool use]")
        assert stats._by_cause["interrupt"].turns == 1, kind
        assert stats._by_cause["interrupt"].cost_usd == pytest.approx(3.0)


@pytest.mark.parametrize("tool, answer, kind", [
    ("ExitPlanMode", _SENT_BACK + "make step two smaller", "user-rejected"),
    ("AskUserQuestion", _SENT_BACK, "user-rejected"),
    ("Bash", "Blocked by the auto mode classifier", "automode-blocked"),
])
def test_the_interrupt_line_after_a_plan_or_question_answer_is_not_you_stopping_claude(tmp_path: Path, tool, answer, kind):
    stats = _answered_call(tmp_path, tool, answer, kind, then="[Request interrupted by user for tool use]")
    assert stats._by_cause["interrupt"].turns == 0
    assert stats._by_cause["tool-denial"].turns == 0


# -- max-turns / stopped_by_user ----------------------------------------------


def test_max_turns_wastes_every_priced_turn_in_a_stopped_by_user_subagent(tmp_path: Path):
    lines = [
        turn_line(message_id="msg_1", model=_MODEL, input_tokens=1_000_000, output_tokens=0),
        turn_line(message_id="msg_2", model=_MODEL, input_tokens=500_000, output_tokens=0),
    ]
    result = _parse(tmp_path, lines, kind="subagent", agent_type="claude-implementer", stopped_by_user=True)

    stats = waste.WasteStats(config_dir=tmp_path / "cfg")
    stats.add(result, _pricing())

    assert stats._by_cause["max-turns"].turns == 2
    assert stats._by_cause["max-turns"].cost_usd == pytest.approx(1.5)
    assert stats._by_agent_type["claude-implementer"].turns == 2


def test_max_turns_not_applied_to_a_stopped_by_user_top_level_transcript(tmp_path: Path):
    """topology.py's own convention only ever reads stopped_by_user off
    subagent transcripts -- a top-level transcript's own stopped_by_user
    is not a meaningful signal (see the module docstring's ASSUMPTIONS
    entry)."""
    lines = [turn_line(message_id="msg_1", model=_MODEL, input_tokens=1_000_000, output_tokens=0)]
    result = _parse(tmp_path, lines, kind="top-level", stopped_by_user=True)

    stats = waste.WasteStats(config_dir=tmp_path / "cfg")
    stats.add(result, _pricing())

    assert stats.wasted_turns == 0


def test_max_turns_overrides_tool_error_on_the_same_turn(tmp_path: Path):
    """A transcript-level override: even a turn that also has its own
    tool error is attributed to max-turns, not tool-error, once the
    whole transcript was killed."""
    lines = [
        turn_line(
            message_id="msg_1",
            model=_MODEL,
            input_tokens=1_000_000,
            output_tokens=0,
            content=[tool_use_block("Bash", "tu_a", {"command": "ls /nope"})],
        ),
        user_block_line([tool_result_block("tu_a", "no such directory", is_error=True)]),
    ]
    result = _parse(tmp_path, lines, kind="subagent", stopped_by_user=True)

    stats = waste.WasteStats(config_dir=tmp_path / "cfg")
    stats.add(result, _pricing())

    assert stats._by_cause["max-turns"].turns == 1
    assert stats._by_cause["tool-error"].turns == 0


# -- limit-pause exclusion -----------------------------------------------------


def _limit_pause_fixture(tmp_path: Path) -> object:
    lines = [
        turn_line(message_id="msg_1", model=_MODEL, input_tokens=30_000, timestamp="2026-09-18T12:00:00.000Z"),
        turn_line(
            message_id="msg_synth",
            model="<synthetic>",
            isApiErrorMessage=True,
            input_tokens=0,
            output_tokens=0,
            content=[{"type": "text", "text": "You've hit your session limit · resets 3pm (Europe/London)"}],
            timestamp="2026-09-18T12:00:05.000Z",
        ),
        user_str_line(
            "I hit my usage limit while you were working, but it has reset now.",
            promptSource="sdk",
            origin={"kind": "human"},
            timestamp="2026-09-18T15:00:00.000Z",
        ),
        # The post-pause turn also has its own tool error -- proving the
        # limit-pause exclusion outranks tool-error detection too.
        turn_line(
            message_id="msg_2",
            model=_MODEL,
            input_tokens=1_000_000,
            output_tokens=0,
            timestamp="2026-09-18T15:00:10.000Z",
            content=[tool_use_block("Bash", "tu_a", {"command": "ls /nope"})],
        ),
        user_block_line(
            [tool_result_block("tu_a", "no such directory", is_error=True)],
            timestamp="2026-09-18T15:00:11.000Z",
        ),
    ]
    return _parse(tmp_path, lines)


def test_limit_pause_turn_excluded_from_every_cause(tmp_path: Path):
    result = _limit_pause_fixture(tmp_path)
    assert any(t.gap_cause == "limit" for t in result.turns)  # sanity: fixture actually produces one

    stats = waste.WasteStats(config_dir=tmp_path / "cfg")
    stats.add(result, _pricing())

    assert stats.wasted_turns == 0
    assert stats.limit_pause_excluded_turns == 1


# -- api-error-retry (count only) ---------------------------------------------


def test_api_error_retry_counted_but_not_costed(tmp_path: Path):
    lines = [
        turn_line(message_id="msg_1", model=_MODEL, input_tokens=1_000_000, output_tokens=0),
        system_line("api_error", error={"status": 529}),
        turn_line(message_id="msg_2", model=_MODEL, input_tokens=1_000_000, output_tokens=0),
    ]
    result = _parse(tmp_path, lines)

    stats = waste.WasteStats(config_dir=tmp_path / "cfg")
    stats.add(result, _pricing())

    # msg_2 is "preceded by" the API_ERROR -- it is the one counted.
    assert stats.api_error_retry_turns == 1
    assert stats.wasted_turns == 0
    assert stats.wasted_cost_usd == 0.0


# -- corpus totals / share_pct -------------------------------------------------


def test_total_priced_turns_and_cost_include_non_wasted_turns(tmp_path: Path):
    lines = [
        turn_line(message_id="msg_1", model=_MODEL, input_tokens=1_000_000, output_tokens=0),  # ordinary turn
        turn_line(
            message_id="msg_2",
            model=_MODEL,
            input_tokens=1_000_000,
            output_tokens=0,
            content=[tool_use_block("Bash", "tu_a", {"command": "ls /nope"})],
        ),
        user_block_line([tool_result_block("tu_a", "no such directory", is_error=True)]),
    ]
    result = _parse(tmp_path, lines)

    stats = waste.WasteStats(config_dir=tmp_path / "cfg")
    stats.add(result, _pricing())

    assert stats.total_priced_turns == 2
    assert stats.total_priced_cost_usd == pytest.approx(2.0)
    assert stats.wasted_turns == 1
    assert stats.wasted_cost_usd == pytest.approx(1.0)


def test_by_cause_share_pct_sums_to_summary_share_pct(tmp_path: Path):
    lines = [
        turn_line(message_id="msg_1", model=_MODEL, input_tokens=1_000_000, output_tokens=0),
        turn_line(
            message_id="msg_2",
            model=_MODEL,
            input_tokens=1_000_000,
            output_tokens=0,
            content=[tool_use_block("Bash", "tu_a", {"command": "ls /nope"})],
        ),
        user_block_line([tool_result_block("tu_a", "no such directory", is_error=True)]),
        turn_line(message_id="msg_3", model=_MODEL, input_tokens=1_000_000, output_tokens=0),
        user_str_line("[Request interrupted by user]"),
        turn_line(message_id="msg_4", model=_MODEL, input_tokens=1_000_000, output_tokens=0),
    ]
    result = _parse(tmp_path, lines)

    stats = waste.compute_waste([result], _pricing(), config_dir=tmp_path / "cfg")
    section = waste.build_section(stats)

    summary = _table(section, "waste_summary")
    by_cause = _table(section, "waste_by_cause")

    summary_turns_share = summary.rows[0][_col(summary, "wasted_turns_share_pct")]
    summary_cost_share = summary.rows[0][_col(summary, "wasted_cost_share_pct")]

    turns_idx = _col(by_cause, "share_of_turns_pct")
    cost_idx = _col(by_cause, "share_of_cost_pct")
    costed_causes = set(waste.CAUSES)
    turns_sum = sum(row[turns_idx] for row in by_cause.rows if row[0] in costed_causes)
    cost_sum = sum(row[cost_idx] for row in by_cause.rows if row[0] in costed_causes)

    assert turns_sum == pytest.approx(summary_turns_share)
    assert cost_sum == pytest.approx(summary_cost_share)


# -- waste_top_sessions / session hashing --------------------------------------


def test_top_sessions_hashes_session_id_never_raw(tmp_path: Path):
    lines = [
        turn_line(
            message_id="msg_1",
            model=_MODEL,
            input_tokens=1_000_000,
            output_tokens=0,
            content=[tool_use_block("Bash", "tu_a", {"command": "ls /nope"})],
        ),
        user_block_line([tool_result_block("tu_a", "no such directory", is_error=True)]),
    ]
    result = _parse(tmp_path, lines, session_id="a-very-identifiable-session-id")

    stats = waste.WasteStats(config_dir=tmp_path / "cfg")
    stats.add(result, _pricing())
    section = waste.build_section(stats)

    top_sessions = _table(section, "waste_top_sessions")
    assert len(top_sessions.rows) == 1
    session_hash = top_sessions.rows[0][0]
    assert session_hash != "a-very-identifiable-session-id"
    assert "a-very-identifiable-session-id" not in session_hash
    assert len(session_hash) == 12
    int(session_hash, 16)  # must be valid hex


def test_top_sessions_hash_stable_for_same_session_and_config_dir(tmp_path: Path):
    lines = [
        turn_line(
            message_id="msg_1",
            model=_MODEL,
            input_tokens=1_000_000,
            output_tokens=0,
            content=[tool_use_block("Bash", "tu_a", {"command": "ls /nope"})],
        ),
        user_block_line([tool_result_block("tu_a", "no such directory", is_error=True)]),
    ]
    cfg = tmp_path / "cfg"
    result_a = _parse(tmp_path, lines, name="a.jsonl", session_id="sess-x")
    stats_a = waste.WasteStats(config_dir=cfg)
    stats_a.add(result_a, _pricing())
    hash_a = _table(waste.build_section(stats_a), "waste_top_sessions").rows[0][0]

    result_b = _parse(tmp_path, lines, name="b.jsonl", session_id="sess-x")
    stats_b = waste.WasteStats(config_dir=cfg)
    stats_b.add(result_b, _pricing())
    hash_b = _table(waste.build_section(stats_b), "waste_top_sessions").rows[0][0]

    assert hash_a == hash_b


def test_top_sessions_cause_mix_and_sorted_by_cost_descending(tmp_path: Path):
    big = [
        turn_line(
            message_id="msg_1",
            model=_MODEL,
            input_tokens=5_000_000,
            output_tokens=0,
            content=[tool_use_block("Bash", "tu_a", {"command": "ls /nope"})],
        ),
        user_block_line([tool_result_block("tu_a", "no such directory", is_error=True)]),
    ]
    small = [
        turn_line(
            message_id="msg_1",
            model=_MODEL,
            input_tokens=1_000_000,
            output_tokens=0,
            content=[tool_use_block("Bash", "tu_a", {"command": "ls /nope"})],
        ),
        user_block_line([tool_result_block("tu_a", "no such directory", is_error=True)]),
    ]
    result_big = _parse(tmp_path, big, name="big.jsonl", session_id="sess-big")
    result_small = _parse(tmp_path, small, name="small.jsonl", session_id="sess-small")

    stats = waste.WasteStats(config_dir=tmp_path / "cfg")
    stats.add(result_big, _pricing())
    stats.add(result_small, _pricing())
    top_sessions = _table(waste.build_section(stats), "waste_top_sessions")

    assert len(top_sessions.rows) == 2
    assert top_sessions.rows[0][2] > top_sessions.rows[1][2]  # cost_usd descending
    assert "tool-error:1" in top_sessions.rows[0][4]


# -- privacy -------------------------------------------------------------------


def test_waste_section_passes_privacy_scan(tmp_path: Path):
    lines = [
        turn_line(
            message_id="msg_1",
            model=_MODEL,
            input_tokens=1_000_000,
            output_tokens=0,
            content=[tool_use_block("Bash", "tu_a", {"command": "cat /c/Users/paulm/secret.txt"})],
        ),
        user_block_line(
            [tool_result_block("tu_a", "cat: /c/Users/paulm/secret.txt: No such file or directory", is_error=True)]
        ),
    ]
    result = _parse(tmp_path, lines, session_id="a-very-identifiable-session-id")

    stats = waste.WasteStats(config_dir=tmp_path / "cfg")
    stats.add(result, _pricing())
    section = waste.build_section(stats)

    assert_privacy(section)


# -- compute_waste convenience entry point -------------------------------------


def test_compute_waste_equals_incremental_add(tmp_path: Path):
    lines = [
        turn_line(
            message_id="msg_1",
            model=_MODEL,
            input_tokens=1_000_000,
            output_tokens=0,
            content=[tool_use_block("Bash", "tu_a", {"command": "ls /nope"})],
        ),
        user_block_line([tool_result_block("tu_a", "no such directory", is_error=True)]),
    ]
    result = _parse(tmp_path, lines)

    one_shot = waste.compute_waste([result], _pricing(), config_dir=tmp_path / "cfg1")

    incremental = waste.WasteStats(config_dir=tmp_path / "cfg2")
    incremental.add(result, _pricing())

    assert one_shot.wasted_turns == incremental.wasted_turns
    assert one_shot.wasted_cost_usd == pytest.approx(incremental.wasted_cost_usd)


# -- rule: wasted-turns --------------------------------------------------------


def _table(section: Section, name: str) -> Table:
    return next(t for t in section.tables if t.name == name)


def _col(table: Table, key: str) -> int:
    return next(i for i, c in enumerate(table.columns) if c.key == key)


def _overview_section(sessions: int, priced_turns: int) -> Section:
    return Section(
        key="overview",
        title="Overview",
        tables=[
            Table(
                name="totals",
                title="Overview totals",
                columns=[Column(key="metric", label="Metric", kind="str"), Column(key="value", label="Value", kind="str")],
                rows=[["sessions", sessions], ["priced_turns", priced_turns]],
            )
        ],
    )


def _report_with_waste_section(
    waste_section: Section, sessions: int = 10, priced_turns: int = 500, units=None
) -> ReportModel:
    report = ReportModel(sections=[_overview_section(sessions, priced_turns), waste_section])
    report.units = units
    return report


def _built_section_for_high_waste_share(tmp_path: Path) -> Section:
    lines = [
        turn_line(
            message_id="msg_1",
            model=_MODEL,
            input_tokens=9_000_000,
            output_tokens=0,
            content=[tool_use_block("Bash", "tu_a", {"command": "ls /nope"})],
        ),
        user_block_line([tool_result_block("tu_a", "no such directory", is_error=True)]),
        turn_line(message_id="msg_2", model=_MODEL, input_tokens=1_000_000, output_tokens=0),
    ]
    result = _parse(tmp_path, lines)
    stats = waste.WasteStats(config_dir=tmp_path / "cfg")
    stats.add(result, _pricing())
    return waste.build_section(stats)


def test_rule_fires_when_share_exceeds_threshold_and_min_sample_met(tmp_path: Path):
    section = _built_section_for_high_waste_share(tmp_path)  # 90% wasted cost share
    report = _report_with_waste_section(section, sessions=10, priced_turns=500)

    th = waste.WasteThresholds(share_pct=10.0, min_sessions=5, min_turns=200)
    recs = waste.RULES[0](report, th)

    assert len(recs) == 1
    rec = recs[0]
    assert rec.id == "wasted-turns"
    assert isinstance(rec, Recommendation)
    assert "tool-error" in rec.action


def test_rule_action_has_no_bare_dollar_under_a_subscription(tmp_path: Path):
    """UX-2 / finding F1-F2: a subscription's Recommendation.action must
    route through Units, never a raw f"${...:,.2f}"."""
    section = _built_section_for_high_waste_share(tmp_path)  # 90% wasted cost share
    units = Units(billing_mode="subscription", currency="USD", elasticity=elasticity_with_slope())
    report = _report_with_waste_section(section, sessions=10, priced_turns=500, units=units)

    th = waste.WasteThresholds(share_pct=10.0, min_sessions=5, min_turns=200)
    [rec] = waste.RULES[0](report, th)
    assert "$" not in rec.action
    assert "about about" not in rec.action.lower()


def test_rule_does_not_fire_below_threshold(tmp_path: Path):
    lines = [
        turn_line(
            message_id="msg_1",
            model=_MODEL,
            input_tokens=10_000,
            output_tokens=0,
            content=[tool_use_block("Bash", "tu_a", {"command": "ls /nope"})],
        ),
        user_block_line([tool_result_block("tu_a", "no such directory", is_error=True)]),
        turn_line(message_id="msg_2", model=_MODEL, input_tokens=9_990_000, output_tokens=0),
    ]
    result = _parse(tmp_path, lines)
    stats = waste.WasteStats(config_dir=tmp_path / "cfg")
    stats.add(result, _pricing())
    section = waste.build_section(stats)
    report = _report_with_waste_section(section, sessions=10, priced_turns=500)

    th = waste.WasteThresholds(share_pct=10.0, min_sessions=5, min_turns=200)
    recs = waste.RULES[0](report, th)

    assert recs == []


def test_rule_does_not_fire_below_minimum_sample(tmp_path: Path):
    section = _built_section_for_high_waste_share(tmp_path)
    report = _report_with_waste_section(section, sessions=1, priced_turns=2)

    th = waste.WasteThresholds(share_pct=10.0, min_sessions=5, min_turns=200)
    recs = waste.RULES[0](report, th)

    assert recs == []


def test_rule_returns_empty_when_waste_section_absent():
    report = ReportModel(sections=[_overview_section(10, 500)])
    th = waste.WasteThresholds()
    assert waste.RULES[0](report, th) == []


def test_rule_evidence_cites_real_table_cells(tmp_path: Path):
    section = _built_section_for_high_waste_share(tmp_path)
    report = _report_with_waste_section(section, sessions=10, priced_turns=500)

    th = waste.WasteThresholds(share_pct=10.0, min_sessions=5, min_turns=200)
    recs = waste.RULES[0](report, th)
    assert len(recs) == 1

    for label, value, source_table, row_key in recs[0].evidence:
        section_key, table_name = source_table.split(".", 1)
        cited = _table(next(s for s in report.sections if s.key == section_key), table_name)
        row = next(r for r in cited.rows if r[0] == row_key)
        assert value in row, f"{label}: {value!r} not found in row {row!r} for {source_table}/{row_key}"


def test_untyped_subagent_is_not_filed_under_top_level():
    from claudeglass.model import TranscriptMeta, TranscriptResult, agent_type_label

    assert agent_type_label(TranscriptResult(meta=TranscriptMeta(kind="subagent"))) == "unknown"
    assert agent_type_label(TranscriptResult(meta=TranscriptMeta(kind="top-level"))) == "top-level"



def test_wasted_turns_cites_redone_messages_and_missed_goals_when_there_are_any(tmp_path: Path):
    from claudeglass import habits
    from claudeglass.habits import CycleFact, Piece

    report = _report_with_waste_section(_built_section_for_high_waste_share(tmp_path))
    th = waste.WasteThresholds(share_pct=10.0, min_sessions=5, min_turns=200)
    (plain,) = waste.RULES[0](report, th)
    assert plain.why == ""
    h = habits.Habits(
        cycles=[CycleFact(session_id="s", ts=None, week="", cost=1.0, turns=1, redone=n < 1) for n in range(4)],
        pieces=[Piece("missed", 2.0, 1, None, (), (), "your feedback")] * 2,
    )
    report.sections.append(habits.section_from(h))
    (rec,) = waste.RULES[0](report, th)
    assert rec.evidence[-2:] == [
        ("Messages redone, fixed or corrected next (%)", 25.0, "habits.habits_by_task", "all"),
        ("Pieces of work you said missed their goal", 2, "habits.habits_outcomes", "missed"),
    ]
    assert rec.why == (
        "These replies cost money but produced nothing you kept. 25% of your messages were redone, fixed or "
        "corrected by the next one. You said 2 pieces of work missed their goal."
    )

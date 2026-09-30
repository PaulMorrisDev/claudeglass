"""Your own hooks (``hook_costs.py``, and what ``parse.py`` keeps for it):
whether each hook works, and what it costs in kept context, blocked calls
and waiting.

Hand-built turns use the packaged Sonnet 5 rates (cache_read 0.2 per
million tokens), as ``test_carry.py`` does.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claudeglass import model
from claudeglass.capture_catalogue import HOOK_SCRIPT
from claudeglass.hook_costs import RULES, HookThresholds, build_section, compute_hook_costs
from claudeglass.model import EventKind, ReportModel, TranscriptMeta, TranscriptResult
from claudeglass.parse import _hook_label, _hook_script_is_relative, parse_transcript
from claudeglass.pricing import load_pricing

from helpers import (
    assert_privacy,
    attachment_line,
    tool_result_block,
    tool_use_block,
    turn_line,
    user_block_line,
    write_jsonl,
)

PRICING = load_pricing()

RELATIVE = "powershell -NoProfile -ExecutionPolicy Bypass -File .claude/hooks/index-first-guard.ps1"
NOT_FOUND = (
    "Failed with non-blocking status code: The argument '.claude/hooks/index-first-guard.ps1' to the -File "
    "parameter does not exist."
)


def _parse(tmp_path: Path, lines: list[dict]) -> TranscriptResult:
    path = tmp_path / "s.jsonl"
    write_jsonl(path, lines)
    return parse_transcript(path, TranscriptMeta(path=str(path), session_id="s1"))


def _hook_events(result: TranscriptResult) -> list[model.Event]:
    return [e for e in result.events if e.kind == EventKind.HOOK_OUTPUT]


# -- labels ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "label"),
    [
        (RELATIVE, "index-first-guard.ps1"),
        ('py -3 "C:\\Users\\someone\\.claude\\claudeglass\\hooks\\snapshot-config.py"', "snapshot-config.py"),
        ('node "${CLAUDE_PLUGIN_ROOT}/hooks/check.js" --fast', "check.js"),
        ("Checking UI changes", "Checking UI changes"),
        ("jq -r .tool_input.command /home/someone/log.json", "jq -r .tool_input.command <path>"),
        ("", None),
        (None, None),
    ],
)
def test_a_hook_is_labelled_by_its_script_or_a_redacted_command_prefix(command, label):
    assert _hook_label(command) == label


@pytest.mark.parametrize(
    ("command", "relative"),
    [
        (RELATIVE, True),
        ("bash ./hooks/guard.sh", True),
        ('powershell -File "${CLAUDE_PROJECT_DIR}/.claude/hooks/guard.ps1"', False),
        ('bash "$CLAUDE_PROJECT_DIR"/.claude/hooks/guard.sh', False),
        ('py -3 "C:\\hooks\\guard.py"', False),
        ("bash ~/hooks/guard.sh", False),
        # A settings.json "\t" read as a tab stays inside the quoted path.
        ('py -3 "%USERPROFILE%\\.claude\\claudeglass\\hooks\tools\\snapshot-config.py"', False),
        ('bash "hooks/my\tdir/guard.sh"', True),
        ("guard.sh", False),
        ("Checking UI changes", False),
    ],
)
def test_a_relative_script_path_is_flagged(command, relative):
    assert _hook_script_is_relative(command) is relative


# -- parse ----------------------------------------------------------------------


def test_a_failed_run_keeps_its_label_cause_and_relative_flag_never_the_command(tmp_path: Path):
    result = _parse(
        tmp_path,
        [
            attachment_line(
                "hook_non_blocking_error",
                hookName="PreToolUse:Read",
                hookEvent="PreToolUse",
                toolUseID="toolu_1",
                command=RELATIVE,
                stderr=NOT_FOUND,
                exitCode=1,
                durationMs=250,
            ),
            turn_line(),
        ],
    )
    [event] = _hook_events(result)
    assert event.detail == {
        "hookName": "PreToolUse",
        "durationMs": 250,
        "script": "index-first-guard.ps1",
        "relative": True,
        "cause": "not-found",
        "tool": "Read",
    }
    assert_privacy(result)


@pytest.mark.parametrize(
    ("hook_name", "tool"),
    [("PreToolUse:Bash", "Bash"), ("PostToolUse:mcp__acme-crm__lookup", "mcp"), ("Stop", None), ("PreToolUse", None)],
)
def test_a_failed_tool_hook_keeps_a_built_in_tool_never_an_mcp_tools_name(tmp_path: Path, hook_name, tool):
    result = _parse(
        tmp_path,
        [
            attachment_line(
                "hook_non_blocking_error", hookName=hook_name, command=RELATIVE, stderr=NOT_FOUND, exitCode=1
            ),
            turn_line(),
        ],
    )
    [event] = _hook_events(result)
    assert event.detail.get("tool") == tool
    assert "acme" not in repr(result)


def test_a_windows_variable_in_the_command_is_flagged_not_relative(tmp_path: Path):
    command = 'py -3 "%USERPROFILE%\\.claude\\hooks\\snapshot-config.py"'
    result = _parse(
        tmp_path,
        [
            attachment_line(
                "hook_non_blocking_error",
                hookName="SessionStart:startup",
                hookEvent="SessionStart",
                command=command,
                stderr="python.exe: can't open file 'C:\\Dev\\x\\%USERPROFILE%\\.claude'",
                exitCode=2,
            ),
            turn_line(),
        ],
    )
    [event] = _hook_events(result)
    assert event.detail["unexpanded"] is True and "relative" not in event.detail
    assert event.detail["cause"] == "not-found"


@pytest.mark.parametrize(
    ("stderr", "exit_code", "cause"),
    [
        ("", 127, "not-found"),
        ("python.exe: can't open file 'x.py': [Errno 2] No such file or directory", 2, "not-found"),
        ("Hook timed out after 60s", 1, "timeout"),
        ("Traceback: KeyError", 1, "failed"),
    ],
)
def test_a_failed_run_says_why(tmp_path: Path, stderr, exit_code, cause):
    result = _parse(
        tmp_path,
        [
            attachment_line(
                "hook_non_blocking_error", hookName="Stop", command="bash guard.sh", stderr=stderr, exitCode=exit_code
            ),
            turn_line(),
        ],
    )
    [event] = _hook_events(result)
    assert event.detail["cause"] == cause


def test_a_stop_hook_block_is_labelled_from_its_nested_command(tmp_path: Path):
    result = _parse(
        tmp_path,
        [
            attachment_line(
                "hook_blocking_error",
                hookName="SubagentStop",
                blockingError={"blockingError": "Keep going.", "command": "powershell -File completion-guard.ps1"},
            ),
            turn_line(),
        ],
    )
    [event] = _hook_events(result)
    assert event.detail["script"] == "completion-guard.ps1"
    assert "cause" not in event.detail


def test_context_a_tool_hook_adds_is_paired_with_its_run(tmp_path: Path):
    result = _parse(
        tmp_path,
        [
            turn_line(content=[tool_use_block("Edit", "toolu_9", {"file_path": "a.py"})]),
            user_block_line([tool_result_block("toolu_9", "ok")]),
            attachment_line(
                "hook_success",
                hookName="PostToolUse:Edit",
                hookEvent="PostToolUse",
                toolUseID="toolu_9",
                command="Checking UI changes",
                stdout='{"hookSpecificOutput":{"additionalContext":"3 issues"}}',
                durationMs=100,
            ),
            attachment_line(
                "hook_additional_context",
                rendered="x" * 400,
                hookName="PostToolUse:Edit",
                hookEvent="PostToolUse",
                toolUseID="toolu_9",
                content=["3 issues"],
            ),
            attachment_line(
                "hook_additional_context",
                rendered="y" * 80,
                hookName="PostToolUse:Edit",
                hookEvent="PostToolUse",
                toolUseID="toolu_9",
                content=["A preview server is running."],
            ),
            turn_line(),
        ],
    )
    assert result.turns[1].hook_context_chars == {"Checking UI changes": 400, "built-in": 80}


def test_session_start_context_pairs_across_different_ids_and_names(tmp_path: Path):
    result = _parse(
        tmp_path,
        [
            attachment_line(
                "hook_success",
                hookName="SessionStart:clear",
                hookEvent="SessionStart",
                toolUseID="0b1e6c3a-run-id",
                command="powershell -File session-digest.ps1",
                stdout="Yesterday you worked on the parser.",
            ),
            attachment_line(
                "hook_additional_context",
                rendered="z" * 200,
                hookName="SessionStart",
                hookEvent="SessionStart",
                toolUseID="SessionStart",
                content=["Yesterday you worked on the parser."],
            ),
            turn_line(),
        ],
    )
    assert result.turns[0].hook_context_chars == {"session-digest.ps1": 200}


def test_a_hook_block_and_its_unchanged_resend_are_counted(tmp_path: Path):
    blocked = f"PreToolUse:Read hook error: [{RELATIVE}]: index-first-guard: first Read of an indexed file."
    read = {"file_path": "src/big.py"}
    result = _parse(
        tmp_path,
        [
            turn_line(content=[tool_use_block("Read", "toolu_1", read), tool_use_block("Grep", "toolu_2", {"pattern": "x"})]),
            user_block_line(
                [tool_result_block("toolu_1", blocked, is_error=True), tool_result_block("toolu_2", "found")]
            ),
            turn_line(content=[tool_use_block("Read", "toolu_3", read)]),
            user_block_line([tool_result_block("toolu_3", "file text")]),
            turn_line(content=[tool_use_block("Read", "toolu_4", read)]),
            user_block_line([tool_result_block("toolu_4", "file text")]),
            turn_line(),
        ],
    )
    assert result.turns[0].hook_blocks == {"index-first-guard.ps1": 1}
    assert result.turns[0].tool_errors_by_kind == {"blocked": 1}
    # Only the first re-send answers the block.
    assert result.turns[1].hook_resends == {"index-first-guard.ps1": 1}
    assert result.turns[2].hook_resends == {}
    assert_privacy(result)


def test_a_changed_call_after_a_block_is_not_a_resend(tmp_path: Path):
    blocked = f"PreToolUse:Read hook error: [{RELATIVE}]: read a range instead."
    result = _parse(
        tmp_path,
        [
            turn_line(content=[tool_use_block("Read", "toolu_1", {"file_path": "src/big.py"})]),
            user_block_line([tool_result_block("toolu_1", blocked, is_error=True)]),
            turn_line(content=[tool_use_block("Read", "toolu_2", {"file_path": "src/big.py", "offset": 1, "limit": 50})]),
            turn_line(),
        ],
    )
    assert result.turns[0].hook_blocks == {"index-first-guard.ps1": 1}
    assert all(not t.hook_resends for t in result.turns)


def test_a_claude_code_guard_block_is_not_one_of_your_hooks(tmp_path: Path):
    result = _parse(
        tmp_path,
        [
            turn_line(content=[tool_use_block("Bash", "toolu_1", {"command": "sleep 45"})]),
            user_block_line(
                [tool_result_block("toolu_1", "<tool_use_error>Blocked: sleep 45 followed by: ls", is_error=True)]
            ),
            turn_line(),
        ],
    )
    assert result.turns[0].tool_errors_by_kind == {"blocked": 1}
    assert result.turns[0].hook_blocks == {}


# -- savers/guards (v4-redirected: parse._block_source, _tool_error_kind) -------

#: Real tokensave v7.13.0 redirect messages, reworded from a scratch
#: project's own transcript (see docs/waste.md and known_savers.py's own
#: module docstring) -- never copied from a real user transcript.
_TOKENSAVE_GREP_BLOCK = (
    "PreToolUse:Bash hook error: STOP: This Bash grep targets a code file in a tokensave-indexed project and "
    "the pattern `build_fixes` looks like a symbol name. Use tokensave_search (definition) or "
    "tokensave_callers_for (usages) instead -- symbol-indexed lookups are faster and more accurate than text "
    "grep. To override for this one call, set TOKENSAVE_DISABLE_GREP_HOOK=1 in the shell."
)
_TOKENSAVE_GLOB_BLOCK = (
    "PreToolUse:Glob hook error: STOP: This Glob searches a tokensave-indexed project for files matching "
    "`**/*.py`. Use tokensave_files(pattern=\"**/*.py\") instead -- symbol-indexed lookups are faster and more "
    "accurate than a glob. To override for this one call, set TOKENSAVE_DISABLE_GREP_HOOK=1 in the shell."
)
_TOKENSAVE_EXPLORE_BLOCK = (
    "PreToolUse:Agent hook error: STOP: Use tokensave MCP tools (tokensave_context, tokensave_search, "
    "tokensave_callees, tokensave_callers, tokensave_impact, tokensave_files, tokensave_affected) instead of "
    "agents for code research. Set TOKENSAVE_DISABLE_GREP_HOOK=1 to override for this one call."
)
_WORKTREE_GUARD_BLOCK = (
    "This agent is isolated in the worktree /repo/.worktrees/impl-1, but this command names git in a form too "
    "complex to verify that it stays inside the worktree. Refusing to run it -- a worktree-isolated agent's "
    "git operations must target its own worktree. Split it into plain, separate commands and run them one at "
    "a time."
)
_UNNAMED_JSON_DENY = "PreToolUse:Bash hook error: nope"
_CLASSIFIER_OUTAGE = (
    "The server-side auto mode classifier gave no verdict (error): timed out waiting for a response."
)


@pytest.mark.parametrize(
    ("text", "tool"),
    [
        (_TOKENSAVE_GREP_BLOCK, "Bash"),
        (_TOKENSAVE_GLOB_BLOCK, "Glob"),
        (_TOKENSAVE_EXPLORE_BLOCK, "Agent"),
    ],
)
def test_a_tokensave_redirect_is_counted_as_a_saver_not_a_hook(tmp_path: Path, text, tool):
    result = _parse(
        tmp_path,
        [
            turn_line(content=[tool_use_block(tool, "toolu_1", {})]),
            user_block_line([tool_result_block("toolu_1", text, is_error=True)]),
            turn_line(),
        ],
    )
    turn = result.turns[0]
    assert turn.tool_errors_by_kind == {"blocked": 1}
    assert turn.saver_redirects == {"tokensave": 1}
    assert turn.hook_blocks == {} and turn.guard_blocks == {}
    assert_privacy(result)


def test_a_worktree_guard_block_is_its_own_guard_label(tmp_path: Path):
    result = _parse(
        tmp_path,
        [
            turn_line(content=[tool_use_block("Bash", "toolu_1", {"command": "git status && git add ."})]),
            user_block_line([tool_result_block("toolu_1", _WORKTREE_GUARD_BLOCK, is_error=True)]),
            turn_line(),
        ],
    )
    turn = result.turns[0]
    assert turn.tool_errors_by_kind == {"blocked": 1}
    assert turn.guard_blocks == {"Claude Code's worktree guard": 1}
    assert turn.hook_blocks == {} and turn.saver_redirects == {}
    assert_privacy(result)


def test_a_sleep_chain_block_is_a_claude_code_guard(tmp_path: Path):
    result = _parse(
        tmp_path,
        [
            turn_line(content=[tool_use_block("Bash", "toolu_1", {"command": "sleep 45"})]),
            user_block_line(
                [tool_result_block("toolu_1", "<tool_use_error>Blocked: sleep 45 followed by: ls", is_error=True)]
            ),
            turn_line(),
        ],
    )
    assert result.turns[0].guard_blocks == {"a Claude Code guard": 1}


def test_a_named_hook_block_is_counted_by_its_script(tmp_path: Path):
    text = f"PreToolUse:Read hook error: [{RELATIVE}]: index-first-guard: first Read of an indexed file."
    result = _parse(
        tmp_path,
        [
            turn_line(content=[tool_use_block("Read", "toolu_1", {"file_path": "src/big.py"})]),
            user_block_line([tool_result_block("toolu_1", text, is_error=True)]),
            turn_line(),
        ],
    )
    turn = result.turns[0]
    assert turn.hook_blocks == {"index-first-guard.ps1": 1}
    assert turn.guard_blocks == {} and turn.saver_redirects == {}


def test_a_hook_that_denies_with_json_and_no_command_is_an_unnamed_guard(tmp_path: Path):
    result = _parse(
        tmp_path,
        [
            turn_line(content=[tool_use_block("Bash", "toolu_1", {"command": "rm -rf /"})]),
            user_block_line([tool_result_block("toolu_1", _UNNAMED_JSON_DENY, is_error=True)]),
            turn_line(),
        ],
    )
    turn = result.turns[0]
    assert turn.tool_errors_by_kind == {"blocked": 1}
    assert turn.guard_blocks == {"a hook that doesn't give its name": 1}
    assert turn.hook_blocks == {} and turn.saver_redirects == {}


def test_the_auto_mode_classifier_outage_is_denied_not_blocked(tmp_path: Path):
    result = _parse(
        tmp_path,
        [
            turn_line(content=[tool_use_block("Bash", "toolu_1", {"command": "ls"})]),
            user_block_line([tool_result_block("toolu_1", _CLASSIFIER_OUTAGE, is_error=True)]),
            turn_line(),
        ],
    )
    turn = result.turns[0]
    assert turn.tool_errors_by_kind == {"denied": 1}
    assert turn.hook_blocks == {} and turn.guard_blocks == {} and turn.saver_redirects == {}


# -- compute and section ----------------------------------------------------------


def _turn(i: int, **overrides) -> model.Turn:
    fields = dict(message_id=f"msg_{i}", request_id=f"req_{i}", turn_index=i, model="claude-sonnet-5")
    fields.update(overrides)
    return model.Turn(**fields)


def _failed(
    label="guard.ps1",
    *,
    relative=True,
    unexpanded=False,
    ts="2026-09-20T10:00:00Z",
    duration=300,
    hook="PreToolUse",
    tool=None,
) -> model.Event:
    detail = {"hookName": hook, "durationMs": duration, "script": label, "cause": "not-found"}
    if relative:
        detail["relative"] = True
    if unexpanded:
        detail["unexpanded"] = True
    if tool:
        detail["tool"] = tool
    return model.Event(kind=EventKind.HOOK_OUTPUT, subkind="hook_non_blocking_error", ts=ts, detail=detail)


def _result(turns, events=(), session_id="s1", *, project="", kind="top-level") -> TranscriptResult:
    return TranscriptResult(
        meta=TranscriptMeta(session_id=session_id, project_slug=project, kind=kind),
        turns=list(turns),
        events=list(events),
    )


def _bash_failures(n: int, label="guard.ps1") -> tuple[list, list]:
    """``n`` Bash calls, each failing ``label``, from 2026-09-20 10:01."""
    turns = [_turn(i, ts=f"2026-09-20T10:{i:02d}:00Z", tool_calls_by_tool={"Bash": 1}) for i in range(1, n + 1)]
    events = [_failed(label, ts=f"2026-09-20T10:{i:02d}:01Z", tool="Bash") for i in range(1, n + 1)]
    return turns, events


def _bash_calls_later(n: int, start=100) -> list:
    """``n`` Bash calls on 2026-09-21, with Reads that don't count."""
    return [
        _turn(start + i, ts=f"2026-09-21T10:{i:02d}:00Z", tool_calls_by_tool={"Bash": 1, "Read": 3})
        for i in range(1, n + 1)
    ]


def test_context_is_priced_from_the_reply_it_lands_before_until_a_summary():
    turns = [_turn(1, ctx=1_000, cache_read_tokens=1_000)]
    turns.append(_turn(2, cache_read_tokens=1_000_000, hook_context_chars={"notes.py": 4_000}))
    turns.append(_turn(3, cache_read_tokens=1_000_000))
    turns.append(_turn(4, cache_read_tokens=1_000_000, preceding_event_kinds=(EventKind.COMPACT_BOUNDARY,)))
    stats = compute_hook_costs([_result(turns)], PRICING)
    row = stats.hooks["notes.py"]
    assert row.contexts == 1
    assert row.context_tokens == 1_000
    # 1,000 tokens read on replies 2 and 3 at $0.20 per million; the
    # summary before reply 4 drops it.
    assert row.carry_usd == pytest.approx(2 * 1_000 * 0.2 / 1_000_000)


def test_built_in_context_is_left_out_and_the_capture_note_joins_its_hook():
    turns = [
        _turn(1, cache_read_tokens=1_000, hook_context_chars={"built-in": 400}, cap_note_chars=800),
        _turn(2, cache_read_tokens=1_000),
    ]
    stats = compute_hook_costs([_result(turns)], PRICING)
    assert "built-in" not in stats.hooks
    assert stats.built_in_tokens == 100
    assert stats.hooks[HOOK_SCRIPT].context_tokens == 200


def test_a_block_costs_its_share_of_the_next_reply():
    turns = [
        _turn(1, cache_read_tokens=1_000, hook_blocks={"guard.ps1": 1}, tool_calls_by_tool={"Read": 1, "Grep": 1}),
        _turn(2, cache_read_tokens=1_000_000, hook_resends={"guard.ps1": 1}),
    ]
    stats = compute_hook_costs([_result(turns)], PRICING)
    row = stats.hooks["guard.ps1"]
    assert (row.blocks, row.resent, row.worked) == (1, 1, 1)
    # Half of reply 2's $0.20 cache read: it answered two calls.
    assert row.block_usd == pytest.approx(0.2 / 2)
    assert row.resent_usd == pytest.approx(row.block_usd)


def test_failed_runs_count_sessions_last_failure_and_waiting():
    events_a = [_failed(ts="2026-09-18T10:00:00Z"), _failed(ts="2026-09-20T10:00:00Z")]
    events_b = [_failed(ts="2026-09-19T10:00:00Z", duration=200)]
    stats = compute_hook_costs(
        [_result([_turn(1)], events_a, "s1"), _result([_turn(1)], events_b, "s2")], PRICING
    )
    row = stats.hooks["guard.ps1"]
    assert (row.failed, len(row.failed_sessions), row.last_failed) == (3, 2, "2026-09-20 10:00 UTC")
    assert row.failed_wait_ms == 800
    section = build_section(stats)
    summary, by_script = section.tables
    assert summary.rows[0][4] == pytest.approx(0.8)
    hook_row = dict(zip([c.key for c in by_script.columns], by_script.rows[0]))
    assert hook_row["cause"] == "script not found (relative path)"
    assert hook_row["failed_sessions"] == 2
    assert_privacy(section)


# -- rules ----------------------------------------------------------------------


def _report(results) -> ReportModel:
    return ReportModel(sections=[build_section(compute_hook_costs(results, PRICING))])


def _rules(report: ReportModel, th: HookThresholds | None = None) -> list[model.Recommendation]:
    th = th or HookThresholds()
    return [rec for rule in RULES for rec in rule(report, th)]


def test_failing_hooks_get_one_card_naming_the_relative_path_fix():
    events = [_failed("guard.ps1") for _ in range(12)] + [_failed("other.ps1") for _ in range(10)]
    [rec] = _rules(_report([_result([_turn(1)], events)]))
    assert rec.id == "hook-failures"
    assert rec.severity == "action"
    assert rec.title == "2 of your hooks failed 22 times: guard.ps1 and other.ps1"
    assert "${CLAUDE_PROJECT_DIR}" in rec.action
    assert [e[0] for e in rec.evidence] == ["guard.ps1: failed runs", "other.ps1: failed runs"]


def test_a_script_behind_a_windows_variable_gets_the_home_fix():
    events = [_failed("snapshot-config.py", relative=False, unexpanded=True) for _ in range(10)]
    report = _report([_result([_turn(1)], events)])
    by_script = report.sections[0].tables[1]
    hook_row = dict(zip([c.key for c in by_script.columns], by_script.rows[0]))
    assert hook_row["cause"] == "script not found (%VAR% not expanded)"
    [rec] = _rules(report)
    assert '"$HOME/.claude/hooks/snapshot-config.py"' in rec.action
    assert "CLAUDE_PROJECT_DIR" not in rec.action


def test_a_few_failures_are_not_a_card():
    events = [_failed() for _ in range(9)]
    assert _rules(_report([_result([_turn(1)], events)])) == []


def test_blocks_mostly_sent_again_get_a_card_with_their_cost():
    turns = []
    for i in range(1, 25, 2):
        turns.append(_turn(i, cache_read_tokens=1_000, hook_blocks={"guard.ps1": 1}, tool_calls_by_tool={"Read": 1}))
        turns.append(_turn(i + 1, cache_read_tokens=1_000_000, hook_resends={"guard.ps1": 1}))
    [rec] = _rules(_report([_result(turns)]))
    assert rec.id == "hook-block-resent"
    assert rec.title == "Hook guard.ps1 blocks calls that Claude then sends again unchanged"
    assert "additionalContext" in rec.action
    assert rec.saving_usd == pytest.approx(12 * 0.2)


def test_blocks_that_change_the_call_are_not_a_card():
    turns = []
    for i in range(1, 25, 2):
        turns.append(_turn(i, cache_read_tokens=1_000, hook_blocks={"guard.ps1": 1}, tool_calls_by_tool={"Read": 1}))
        turns.append(_turn(i + 1, cache_read_tokens=1_000_000))
    assert _rules(_report([_result(turns)])) == []


def test_costly_hook_context_gets_a_card_but_the_capture_hook_never_does():
    turns = [
        _turn(i, cache_read_tokens=5_000_000, hook_context_chars={"notes.py": 100_000}, cap_note_chars=100_000)
        for i in range(1, 31)
    ]
    [rec] = _rules(_report([_result(turns)]))
    assert rec.id == "hook-context-carry"
    assert rec.title == "Context from hook notes.py is costly to keep"
    assert rec.saving_usd >= HookThresholds().min_context_usd


def test_thresholds_read_their_prefixed_config_keys():
    th = HookThresholds.from_config({"hooks_min_failures": "3", "hooks_resend_share_pct": 80, "min_failures": 99})
    assert (th.min_failures, th.resend_share_pct) == (3, 80.0)
    assert RULES[0](ReportModel(), th) == []


# -- stopped failing ------------------------------------------------------------


def _hook_row(report: ReportModel, label="guard.ps1") -> dict:
    by_script = report.sections[0].tables[1]
    rows = [dict(zip([c.key for c in by_script.columns], row)) for row in by_script.rows]
    return next(r for r in rows if r["hook"] == label)


def test_a_tool_hook_that_stopped_failing_is_not_a_card():
    # It failed on every Bash call, then 5 more ran clean: at its old
    # rate 5 should have failed.
    turns, events = _bash_failures(20)
    report = _report([_result(turns + _bash_calls_later(5), events)])
    row = _hook_row(report)
    assert (row["last_failed"], row["stopped"]) == ("2026-09-20 10:20 UTC", "yes")
    assert _rules(report) == []


def test_a_hook_that_has_not_run_enough_since_is_still_a_card_saying_when_it_last_failed():
    turns, events = _bash_failures(20)
    report = _report([_result(turns + _bash_calls_later(4), events)])
    assert _hook_row(report)["stopped"] == "no"
    [rec] = _rules(report)
    assert rec.why == (
        "A hook that fails doesn't do its job. guard.ps1 failed in 1 sessions, most recently at 2026-09-20 10:20 UTC: "
        "script not found (relative path). If you've fixed it since, this goes once it has run enough times "
        "without failing."
    )


def test_failures_read_before_tools_were_kept_do_not_hide_that_it_stopped():
    # A transcript read before PARSER_VERSION 32, whose file has gone,
    # keeps failures that name no tool.
    turns, events = _bash_failures(20)
    old = _result([], [_failed(ts="2026-09-19T10:00:00Z"), _failed(ts="2026-09-19T10:01:00Z")], "s0")
    row = compute_hook_costs([old, _result(turns + _bash_calls_later(5), events)], PRICING).hooks["guard.ps1"]
    assert not row.tool_less
    assert (row.failed, row.chances_before, row.chances_since) == (22, 20, 5)
    assert row.stopped()


def test_a_session_start_hook_counts_the_sessions_and_summaries_since():
    failing = [
        _result([_turn(1, ts=f"2026-09-20T0{i}:00:05Z")], [_failed(ts=f"2026-09-20T0{i}:00:00Z", hook="SessionStart")],
                f"old{i}")
        for i in range(1, 4)
    ]
    summary = model.Event(kind=EventKind.COMPACT_BOUNDARY, ts="2026-09-21T09:30:00Z")
    later = [_result([_turn(1, ts=f"2026-09-21T0{i}:00:05Z")], [summary] if i == 1 else [], f"new{i}")
             for i in range(1, 5)]
    stats = compute_hook_costs(failing + later, PRICING)
    row = stats.hooks["guard.ps1"]
    # 3 starts, each failing; since, 4 starts and 1 summary ran clean.
    assert (row.chances_before, row.chances_since) == (3, 5)
    assert row.stopped()


def test_a_subagent_counts_in_its_sessions_project_and_other_projects_do_not():
    turns, events = _bash_failures(20)
    results = [
        _result([], session_id="s1", project="shop"),
        _result(turns, events, "s1", kind="subagent"),
        _result(_bash_calls_later(3), session_id="s2", project="shop"),
        _result(_bash_calls_later(9), session_id="s3", project="blog"),
    ]
    row = compute_hook_costs(results, PRICING).hooks["guard.ps1"]
    assert row.failed_projects == {"shop"}
    assert (row.chances_before, row.chances_since) == (20, 3)
    assert not row.stopped()
    assert row.stopped(HookThresholds(quiet_failures=3))


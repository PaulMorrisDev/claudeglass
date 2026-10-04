"""Quality signals (``quality``): what the parser and event classifier
record for them, how one transcript becomes a run, and how two sets of
runs are compared."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest

from claudeglass import events, parse, quality
from claudeglass.model import EventKind, TranscriptMeta
from claudeglass.parse import parse_transcript
from claudeglass.pricing import load_pricing
from claudeglass.units import Units

from helpers import (
    old_agent_note_text,
    attachment_line,
    elasticity_with_slope,
    queue_operation_line,
    tool_result_block,
    tool_use_block,
    turn_line,
    user_block_line,
    user_str_line,
    write_jsonl,
)


def _ts(second: int) -> str:
    return f"2026-09-18T12:{second // 60:02d}:{second % 60:02d}.000Z"


def _reply(second: int, *blocks, stop_reason: str | None = None, **kw) -> dict:
    line = turn_line(content=list(blocks) or [{"type": "text", "text": "done"}], timestamp=_ts(second), **kw)
    line["message"]["stop_reason"] = stop_reason
    return line


def _result(second: int, tool_use_id: str, *, error: bool = False) -> dict:
    return user_block_line([tool_result_block(tool_use_id, "out", is_error=error)], timestamp=_ts(second))


def _parse(tmp_path, lines, name="t.jsonl", **meta):
    path = tmp_path / name
    write_jsonl(path, lines)
    return parse_transcript(path, TranscriptMeta(path=str(path), **meta))


def _agent(tmp_path, lines, agent_id="abc", agent_type="Explore"):
    return _parse(tmp_path, lines, f"agent-{agent_id}.jsonl", kind="subagent", agent_id=f"agent-{agent_id}",
                  agent_type=agent_type)


def _note(second: int, ids, *, agent_type: str = "") -> dict:
    """A SubagentStart note (SEC-P2): a ``[result: ...]`` only counts
    when its own transcript has seen one asking for it."""
    text = old_agent_note_text(ids, agent_type)
    wrapped = f"<system-reminder>\nSubagentStart hook additional context: {text}\n</system-reminder>"
    line = attachment_line("hook_additional_context", rendered=wrapped, content=[text], hookName="SubagentStart",
                           hookEvent="SubagentStart", toolUseID="SubagentStart")
    line["timestamp"] = _ts(second)
    return line


# -- what the parser keeps ----------------------------------------------------------


@pytest.fixture(autouse=True)
def _salt():
    parse.set_salt(b"q" * 32)


def test_turns_keep_tool_calls_errors_edits_and_stop_reason(tmp_path):
    result = _parse(tmp_path, [
        user_str_line("fix the build", timestamp=_ts(0)),
        _reply(1, tool_use_block("Bash", "t1"), tool_use_block("Edit", "t2", {"file_path": "C:/src/a.py"}),
               stop_reason="tool_use"),
        user_block_line([tool_result_block("t1", "boom", is_error=True), tool_result_block("t2", "ok")],
                        timestamp=_ts(2)),
        _reply(3, stop_reason="end_turn"),
    ])
    first = next(t for t in result.turns if t.tool_use_ids)
    assert first.stop_reason == "tool_use"
    assert first.tool_calls_by_tool == {"Bash": 1, "Edit": 1}
    assert first.tool_errors_by_tool == {"Bash": 1}
    assert len(first.edit_target_hashes) == 1
    assert "a.py" not in repr(first.edit_target_hashes)
    assert result.turns[-1].stop_reason == "end_turn"


def _edits(tmp_path, blocks, results, cwd="C:\\Dev\\app"):
    result = _parse(tmp_path, [
        user_str_line("go", timestamp=_ts(0)),
        _reply(1, *blocks, stop_reason="tool_use", cwd=cwd),
        user_block_line(results, timestamp=_ts(2)),
        _reply(3, stop_reason="end_turn"),
    ])
    return next(t for t in result.turns if t.tool_use_ids)


def test_a_file_written_by_a_shell_command_counts_as_the_same_edit_as_edit(tmp_path):
    turn = _edits(tmp_path, [
        tool_use_block("Edit", "t1", {"file_path": "C:\\Dev\\App\\src\\a.py"}),
        tool_use_block("Bash", "t2", {"command": "cat > src/a.py <<'EOF'\nx = 1\nEOF"}),
        tool_use_block("Bash", "t3", {"command": "sed -i 's/1/2/' /c/Dev/app/src/./a.py"}),
        tool_use_block("PowerShell", "t4", {"command": "Set-Content -Path ..\\app\\src\\a.py -Value 'y'"}),
        tool_use_block("MultiEdit", "t5", {"file_path": "C:/Dev/app/src/a.py", "edits": []}),
        tool_use_block("Bash", "t6", {"command": "npm test > test.log"}),
    ], [tool_result_block(f"t{i}", "ok") for i in range(1, 7)])
    assert len(turn.edit_target_hashes) == 5
    assert len(set(turn.edit_target_hashes)) == 1
    assert "a.py" not in repr((turn.edit_target_hashes, turn.read_target_hashes))


def test_an_edit_that_failed_is_not_an_edit(tmp_path):
    turn = _edits(tmp_path, [
        tool_use_block("Edit", "t1", {"file_path": "C:/Dev/app/a.py"}),
        tool_use_block("Write", "t2", {"file_path": "C:/Dev/app/b.py"}),
        tool_use_block("Bash", "t3", {"command": "echo x > c.txt"}),
        tool_use_block("Bash", "t4", {"command": "echo x > d.txt && false"}),
    ], [
        tool_result_block("t1", "<tool_use_error>String to replace not found in file.</tool_use_error>",
                          is_error=True),
        tool_result_block("t2", "ok"),
        tool_result_block("t3", "The user doesn't want to proceed with this tool use.", is_error=True),
        # The command ran; its file was written before it failed.
        tool_result_block("t4", "Exit code 1", is_error=True),
    ])
    assert len(turn.edit_target_hashes) == 2
    assert turn.read_target_hashes == ()  # an edit is not a read


def test_a_relative_shell_write_with_no_working_directory_is_not_recorded(tmp_path):
    turn = _edits(tmp_path, [tool_use_block("Bash", "t1", {"command": "echo x > c.txt"})],
                  [tool_result_block("t1", "ok")], cwd=None)
    assert turn.edit_target_hashes == ()


@pytest.mark.parametrize("text, expected", [
    ("that is wrong, the test still fails", True),
    ("It still doesnt work", True),
    ("why did you delete the config?", True),
    ("please undo that change", True),
    ("No, go ahead", False),
    ("looks good, ship it", False),
    ("x" * 300 + " that's wrong", False),  # only the start of a message is read
])
def test_correction_phrases(text, expected):
    assert events._looks_like_correction([text]) is expected


def test_a_correction_is_a_flag_on_the_next_turn_and_the_text_is_not_kept(tmp_path):
    result = _parse(tmp_path, [
        user_str_line("add a test", timestamp=_ts(0)),
        _reply(1),
        user_str_line("that's wrong, it still fails", timestamp=_ts(2)),
        _reply(3),
    ])
    assert [t.human_correction for t in result.turns if t.turn_index > 0] == [False, True]
    human = [e for e in result.events if e.kind == EventKind.HUMAN_TEXT]
    assert human[-1].detail["correction"] is True
    assert "wrong" not in repr(result)


NOTE = "<task-notification><task-id>abc123</task-id><status>failed</status><summary>x</summary></task-notification>"


def test_task_notifications_carry_id_and_status_in_every_shape():
    as_user = events.classify_line(user_str_line(NOTE))
    queued = events.classify_line(queue_operation_line("enqueue", content=NOTE))
    attached = events.classify_line(attachment_line("queued_command", prompt=NOTE))
    for event in (as_user, queued, attached):
        assert event.detail["task_id"] == "abc123" and event.detail["status"] == "failed"
    assert events._task_notification_detail("no id here") == {}


def test_a_synchronous_agent_result_carries_its_id_and_status_but_a_launch_does_not():
    done = user_block_line([tool_result_block("t1", "ok")], toolUseResult={"agentId": "abc", "status": "completed"})
    launched = user_block_line([tool_result_block("t1", "ok")],
                               toolUseResult={"agentId": "abc", "status": "async_launched", "isAsync": True})
    assert events.classify_line(done).detail == {"agents": [["abc", "completed"]]}
    assert not events.classify_line(launched).detail


# -- one transcript as a run ---------------------------------------------------------


def test_an_agent_that_answers_is_not_cut_off(tmp_path):
    run = quality.run_facts(_agent(tmp_path, [
        user_str_line("brief", timestamp=_ts(0)),
        _reply(1, tool_use_block("Read", "t1")),
        _result(2, "t1"),
        _reply(3),
    ]), None)
    assert run.cut_off is False and not run.turn_limit
    assert (run.replies, run.tool_calls, run.tool_errors) == (2, 1, 0)


@pytest.mark.parametrize("tool", ["StructuredOutput", "SubagentHandback"])
def test_ending_on_an_answer_tool_is_an_answer(tmp_path, tool):
    run = quality.run_facts(_agent(tmp_path, [
        user_str_line("brief", timestamp=_ts(0)),
        _reply(1, tool_use_block(tool, "t1")),
    ]), None)
    assert run.cut_off is False and not run.turn_limit


def test_an_answer_tool_beside_another_call_is_not_an_answer(tmp_path):
    run = quality.run_facts(_agent(tmp_path, [
        user_str_line("brief", timestamp=_ts(0)),
        _reply(1, tool_use_block("SubagentHandback", "t1"), tool_use_block("Bash", "t2")),
    ]), None)
    assert run.cut_off is True


def test_ending_on_a_tool_call_is_cut_off_and_after_its_result_likely_out_of_turns(tmp_path):
    waiting = quality.run_facts(_agent(tmp_path, [
        user_str_line("brief", timestamp=_ts(0)),
        _reply(1, tool_use_block("Bash", "t1")),
    ], agent_id="a1"), None)
    assert waiting.cut_off and not waiting.turn_limit
    out_of_turns = quality.run_facts(_agent(tmp_path, [
        user_str_line("brief", timestamp=_ts(0)),
        _reply(1, tool_use_block("Bash", "t1")),
        _result(2, "t1"),
    ], agent_id="a2"), None)
    assert out_of_turns.cut_off and out_of_turns.turn_limit


def test_an_agent_that_never_replied_is_cut_off(tmp_path):
    run = quality.run_facts(_agent(tmp_path, [user_str_line("brief", timestamp=_ts(0))]), None)
    assert run.cut_off is True and run.replies == 0


def test_rework_counts_edits_to_a_file_changed_before_your_last_message(tmp_path):
    edit = lambda sec, tid: _reply(sec, tool_use_block("Edit", tid, {"file_path": "C:/src/a.py"}))  # noqa: E731
    run = quality.run_facts(_parse(tmp_path, [
        user_str_line("change a", timestamp=_ts(0)),
        edit(1, "t1"), _result(2, "t1"), edit(3, "t2"), _result(4, "t2"), _reply(5),
        user_str_line("that's wrong", timestamp=_ts(6)),
        edit(7, "t3"), _result(8, "t3"), _reply(9),
    ]), None)
    assert (run.edits, run.files_edited, run.rework_edits) == (3, 1, 1)
    assert (run.human_messages, run.corrections) == (2, 1)
    assert run.cut_off is None  # the main session is never "cut off"


def test_a_token_savers_redirect_is_not_a_denial(tmp_path):
    grep = lambda sec, tid: _reply(sec, tool_use_block("Grep", tid, {"pattern": "build_fix"}))  # noqa: E731
    redirect = "PreToolUse:Grep hook error: STOP: This Grep targets a code file in a tokensave-indexed project."
    run = quality.run_facts(_parse(tmp_path, [
        user_str_line("find build_fix", timestamp=_ts(0)),
        grep(1, "t1"),
        user_block_line([tool_result_block("t1", redirect, is_error=True)], timestamp=_ts(2),
                        toolDenialKind="permission-rule"),
        grep(3, "t2"),
        user_block_line([tool_result_block("t2", "Permission to use Grep has been denied.", is_error=True)],
                        timestamp=_ts(4), toolDenialKind="permission-rule"),
        _reply(5),
    ]), None)
    assert run.denials == 1


_SENT_BACK = (
    "The user doesn't want to proceed with this tool use. The tool use was rejected (eg. if it was a file edit, "
    "the new_string was NOT written to the file). To tell you how to proceed, the user said:\n"
)


def _turned_away(second: int, tool_use_id: str, text: str, kind: str) -> dict:
    return user_block_line([tool_result_block(tool_use_id, text, is_error=True)], timestamp=_ts(second),
                           toolDenialKind=kind)


def test_only_calls_you_or_a_deny_rule_turned_down_are_denials(tmp_path):
    run = quality.run_facts(_parse(tmp_path, [
        user_str_line("add a retry to the fetcher", timestamp=_ts(0)),
        _reply(1, tool_use_block("ExitPlanMode", "p1", {"plan": "1. a"})),
        _turned_away(2, "p1", _SENT_BACK + "make step two smaller", "user-rejected"),
        _reply(3, tool_use_block("AskUserQuestion", "q1", {"questions": []})),
        _turned_away(4, "q1", _SENT_BACK, "user-rejected"),
        _reply(5, tool_use_block("Bash", "b1", {"command": "make"})),
        _turned_away(6, "b1", "PreToolUse:Bash hook error: [guard.sh] STOP: no make here", "permission-rule"),
        _reply(7, tool_use_block("Bash", "b2", {"command": "make"})),
        _turned_away(8, "b2", "Blocked by the auto mode classifier", "automode-blocked"),
        _reply(9, tool_use_block("Bash", "b3", {"command": "make"})),
        _turned_away(10, "b3", "Permission to use Bash has been denied.", "interrupted"),
        _reply(11, tool_use_block("Bash", "b4", {"command": "rm -rf build"})),
        _turned_away(12, "b4", "Permission to use Bash has been denied.", "permission-rule"),
        _reply(13, tool_use_block("Edit", "e1", {"file_path": "a.py"})),
        _turned_away(14, "e1", _SENT_BACK + "not that file", "user-rejected"),
        _reply(15),
    ]), None)
    assert run.denials == 2


def test_a_plan_you_sent_back_is_not_a_tool_error_but_a_refused_call_still_is(tmp_path):
    run = quality.run_facts(_parse(tmp_path, [
        user_str_line("add a retry to the fetcher", timestamp=_ts(0)),
        _reply(1, tool_use_block("ExitPlanMode", "p1", {"plan": "1. a"})),
        _turned_away(2, "p1", _SENT_BACK + "smaller", "user-rejected"),
        _reply(3, tool_use_block("Bash", "b1", {"command": "make"})),
        _turned_away(4, "b1", "Permission to use Bash has been denied.", "permission-rule"),
        _reply(5),
    ]), None)
    assert (run.denials, run.tool_errors) == (1, 1)


def test_only_you_stopping_a_reply_is_a_stop(tmp_path):
    def stops(*middle) -> int:
        run = quality.run_facts(_parse(tmp_path, [
            user_str_line("add a retry to the fetcher", timestamp=_ts(0)),
            _reply(1, tool_use_block("Bash", "b1", {"command": "make"})),
            *middle,
            _reply(9),
        ]), None)
        return run.interrupts

    plain = user_str_line("[Request interrupted by user]", timestamp=_ts(3))
    tail = user_str_line("[Request interrupted by user for tool use]", timestamp=_ts(3))
    assert stops(plain) == 1
    # A call you turned down, or a dialog you closed, then the tool-use line.
    assert stops(_turned_away(2, "b1", "Permission to use Bash has been denied.", "permission-rule"), tail) == 1
    assert stops(_turned_away(2, "b1", "Permission to use Bash has been denied.", "interrupted"), tail) == 1
    assert stops(_turned_away(2, "b1", _SENT_BACK + "not now", "user-rejected"), tail) == 1
    # The same line after a hook's or the classifier's block is only how the turn ended.
    assert stops(_turned_away(2, "b1", "PreToolUse:Bash hook error: [guard.sh] no", "permission-rule"), tail) == 0
    assert stops(_turned_away(2, "b1", "Blocked by the auto mode classifier", "automode-blocked"), tail) == 0
    # The session's end isn't you stopping anything.
    assert stops(user_str_line("[Request interrupted: app quit]", timestamp=_ts(3))) == 0


def test_the_tool_use_line_after_a_plan_or_question_answer_is_not_a_stop(tmp_path):
    tail = user_str_line("[Request interrupted by user for tool use]", timestamp=_ts(3))
    plan = quality.run_facts(_parse(tmp_path, [
        user_str_line("add a retry to the fetcher", timestamp=_ts(0)),
        _reply(1, tool_use_block("ExitPlanMode", "p1", {"plan": "1. a"})),
        _turned_away(2, "p1", _SENT_BACK + "smaller", "user-rejected"), tail, _reply(9),
    ]), None)
    question = quality.run_facts(_parse(tmp_path, [
        user_str_line("add a retry to the fetcher", timestamp=_ts(0)),
        _reply(1, tool_use_block("AskUserQuestion", "q1", {"questions": []})),
        _turned_away(2, "q1", _SENT_BACK, "user-rejected"), tail, _reply(9),
    ]), None)
    assert (plan.interrupts, question.interrupts) == (0, 0)
    assert (plan.denials, question.denials) == (0, 0)


def test_session_runs_join_notification_outcomes_to_agent_transcripts(tmp_path):
    top = _parse(tmp_path, [
        user_str_line("go", timestamp=_ts(0)),
        _reply(1),
        queue_operation_line("enqueue", content=NOTE, timestamp=_ts(5)),
    ], "top.jsonl")
    agent = _agent(tmp_path, [user_str_line("brief", timestamp=_ts(0)), _reply(1)], agent_id="abc123")
    runs = quality.session_runs(NS(top=top, subs=[agent], session_id="s1"), None)
    assert [r.group for r in runs] == [quality.MAIN, "Explore"]
    assert runs[1].outcome == "failed"
    assert quality.SIGNAL_BY_KEY["unfinished"].num(runs[1]) == 1.0


# -- comparing ----------------------------------------------------------------------


def _runs(n: int, calls: int, errors: int, group="Explore", **kw) -> list[quality.Run]:
    return [quality.Run(group=group, kind="subagent", tool_calls=calls, tool_errors=errors, replies=10,
                        cut_off=False, **kw) for _ in range(n)]


def _row(rows, key):
    return next(r for r in rows if r["key"] == key)


def test_a_clear_rise_in_failures_is_worse_with_its_counts():
    rows = quality.compare_runs(_runs(20, 50, 1), _runs(20, 50, 8), [quality.SIGNAL_BY_KEY["tool_errors"]])
    row = _row(rows, "tool_errors")
    assert row["label_key"] == "worse" and row["verdict"] == "Worse"
    assert row["before_counts"] == "20 of 1,000 tool calls" and row["after_text"] == "16%"
    assert quality.verdict(rows) == "Quality looks worse: tool calls that failed rose from 2.0% to 16%."


def test_too_few_runs_is_too_little_data():
    rows = quality.compare_runs(_runs(3, 50, 1), _runs(20, 50, 8), [quality.SIGNAL_BY_KEY["tool_errors"]])
    assert _row(rows, "tool_errors")["label_key"] == "too_little_data"
    assert quality.verdict([_row(rows, "tool_errors")]) == (
        "Too few runs to judge quality yet: 3 before and 20 after, and at least 5 are needed on each side."
    )


def test_a_real_but_tiny_move_in_a_share_is_no_clear_change():
    before = _runs(200, 1000, 0)
    after = _runs(200, 1000, 0)
    for run in after[:100]:
        run.tool_errors = 3  # 0.15%: consistent across runs, so "significant", but tiny
    row = _row(quality.compare_runs(before, after, [quality.SIGNAL_BY_KEY["tool_errors"]]), "tool_errors")
    assert row["p"] < quality.ALPHA
    assert row["label_key"] == "no_clear_change"


def test_one_bad_run_counts_as_one_run_not_as_many_failures():
    before = _runs(10, 50, 1)
    after = _runs(9, 50, 1) + _runs(1, 50, 40)
    row = _row(quality.compare_runs(before, after, [quality.SIGNAL_BY_KEY["tool_errors"]]), "tool_errors")
    assert row["label_key"] in ("no_clear_change", "possibly_worse")


@pytest.mark.parametrize("p_values, labels", [
    ((0.02, 0.5, 0.5), ["possibly_worse", "no_clear_change", "no_clear_change"]),  # 0.02 > 0.05 / 3
    ((0.001, 0.02, 0.5), ["worse", "worse", "no_clear_change"]),  # 0.02 < 0.05 / 2 once the first holds
])
def test_holm_correction_across_the_signals_compared(monkeypatch, p_values, labels):
    signals = [quality.SIGNAL_BY_KEY[k] for k in ("tool_errors", "shell_errors", "denials")]
    before = _runs(12, 40, 1, shell_calls=20, shell_errors=1, denials=1)
    after = _runs(12, 40, 6, shell_calls=20, shell_errors=4, denials=4)
    for run in after[::2]:
        run.tool_errors, run.shell_errors, run.denials = 2, 2, 2
    queue = iter(p_values)
    monkeypatch.setattr(quality, "_p_value", lambda z: next(queue))
    rows = quality.compare_runs(before, after, signals)
    assert [r["label_key"] for r in rows] == labels


def test_neutral_measures_are_higher_or_lower_not_better_or_worse():
    before = [quality.Run(kind="subagent", replies=10 + i % 3) for i in range(20)]
    after = [quality.Run(kind="subagent", replies=30 + i % 3) for i in range(20)]
    assert _row(quality.compare_runs(before, after, [quality.SIGNAL_BY_KEY["replies"]]), "replies")["label_key"] == "higher"


@pytest.mark.parametrize("value, text", [(0.0, "0%"), (0.0004, "under 0.1%"), (0.012, "1.2%"), (0.25, "25%")])
def test_share_text(value, text):
    assert quality.value_text(quality.SIGNAL_BY_KEY["tool_errors"], value) == text


def test_value_text_money_unit_has_no_bare_dollar_under_a_subscription():
    """UX-2: no packaged ``Signal`` currently has ``unit="money"``, but
    ``value_text``'s money branch is still wired through ``build_section``/
    ``setup_rows``'s ``money`` callable for defense-in-depth -- this
    exercises that branch directly against a synthetic money-unit signal,
    since no real one reaches it today."""
    money_signal = quality.Signal(
        key="synthetic_cost", label="Synthetic cost", num=lambda r: 0.0, den=lambda r: 1.0,
        kind="per_run", worse="higher", scope="all", of="cost", unit="money",
    )
    units = Units(billing_mode="subscription", currency="USD", elasticity=elasticity_with_slope())
    text = quality.value_text(money_signal, 1.23, money=units.money_text)
    assert "$" not in text
    assert "about about" not in text.lower()


def test_main_only_signals_are_not_compared_for_agents():
    keys = {s.key for s in quality.signals_for("Explore")}
    assert "corrections" not in keys and "unfinished" in keys
    assert "unfinished" not in {s.key for s in quality.signals_for(quality.MAIN)}


# -- the report section ----------------------------------------------------------------


def _setup_runs(model: str, n: int, errors: int, agent="Explore") -> list[quality.Run]:
    return [quality.Run(group=agent, kind="subagent", model=model, effort="high", replies=10, tool_calls=50,
                        tool_errors=errors, cut_off=False) for _ in range(n)]


def test_setups_are_compared_with_the_most_used_one():
    runs = _setup_runs("claude-sonnet-5", 30, 1) + _setup_runs("claude-haiku-4-5", 20, 10) + _setup_runs(
        "claude-opus-5", 2, 1
    ) + [quality.Run(group="Explore", kind="subagent", cut_off=True)]  # never replied: left out
    rows = {r["model"]: r for r in quality.setup_rows(runs)}
    assert set(rows) == {"claude-sonnet-5", "claude-haiku-4-5", "claude-opus-5"}
    assert rows["claude-sonnet-5"]["setup_verdict"] == "baseline"
    assert rows["claude-haiku-4-5"]["setup_verdict"] == "worse"
    assert rows["claude-haiku-4-5"]["compared_model"] == "claude-sonnet-5"
    assert "Worse: tool calls that failed" in rows["claude-haiku-4-5"]["difference"]
    assert rows["claude-opus-5"]["setup_verdict"] == "too_little_data"


WATCHDOG = '<scheduled-task name="heartbeat-watchdog">Check the heartbeat file and stop.</scheduled-task>'


@pytest.mark.parametrize("lines, scheduled", [
    # Started by a scheduled task and left to itself.
    ([user_str_line(WATCHDOG, timestamp=_ts(0)), _reply(1)], True),
    # Started by a scheduled task, then steered by you: your work.
    ([user_str_line(WATCHDOG, timestamp=_ts(0)), _reply(1), user_str_line("now fix it", timestamp=_ts(2)),
      _reply(3)], False),
    ([user_str_line("fix the build", timestamp=_ts(0)), _reply(1)], False),
])
def test_a_main_session_a_scheduled_task_started_with_no_message_of_yours_is_scheduled(tmp_path, lines, scheduled):
    run = quality.run_facts(_parse(tmp_path, lines), None)
    assert run.scheduled is scheduled


def test_a_subagent_run_is_never_scheduled(tmp_path):
    run = quality.run_facts(_agent(tmp_path, [user_str_line(WATCHDOG, timestamp=_ts(0)), _reply(1)]), None)
    assert run.scheduled is False


def _main_runs(effort: str, n: int, replies: int, errors: int, **kw) -> list[quality.Run]:
    return [quality.Run(model="claude-opus-5-5", effort=effort, replies=replies, tool_calls=5 * replies,
                        tool_errors=errors, human_messages=0 if kw.get("scheduled") else 3, **kw) for _ in range(n)]


def test_scheduled_runs_are_not_the_baseline_a_setup_is_judged_against():
    """22 two-reply scheduled checks at medium effort were the main
    session's most-used setup, so eight real sessions at xhigh were
    judged worse against checks that never failed a tool call."""
    runs = _main_runs("medium", 22, 2, 0, scheduled=True) + _main_runs("xhigh", 8, 500, 50)
    rows = quality.setup_rows(runs)
    assert [(r["effort"], r["runs"], r["setup_verdict"]) for r in rows] == [("xhigh", 8, "only")]
    section = quality.build_section(runs)
    assert any("(22 in this window)" in note for note in section.notes)


def test_a_setup_whose_runs_are_far_larger_or_smaller_is_not_comparable():
    runs = _main_runs("medium", 22, 2, 0) + _main_runs("xhigh", 8, 500, 50)
    rows = {r["effort"]: r for r in quality.setup_rows(runs)}
    assert rows["medium"]["setup_verdict"] == "baseline"
    xhigh = rows["xhigh"]
    assert xhigh["setup_verdict"] == "not_comparable"
    assert xhigh["comparison"] == []
    assert (xhigh["compared_model"], xhigh["compared_effort"]) == ("claude-opus-5-5", "medium")
    assert xhigh["difference"].startswith("Not compared: its runs averaged 500.0 replies against 2.0")
    assert "not_comparable" in quality.SETUP_VERDICTS
    # Nothing clearly worse, so the models check doesn't hold xhigh back.
    assert quality.worse_models(rows.values()) == {}


def test_setups_up_to_the_size_limit_apart_are_still_compared():
    ratio = int(quality.COMPARABLE_SIZE)
    runs = _main_runs("medium", 20, 10, 0) + _main_runs("xhigh", 10, 10 * ratio, 100)
    rows = {r["effort"]: r for r in quality.setup_rows(runs)}
    assert rows["xhigh"]["setup_verdict"] == "worse"


def test_clearly_worse_on_one_signal_and_clearly_better_on_another_is_mixed():
    def row(label_key):
        return {"label_key": label_key, "worse_when": "higher"}

    assert quality.setup_verdict([row("worse"), row("better")]) == "mixed"
    assert quality.setup_verdict([row("worse"), row("possibly_better")]) == "worse"
    assert quality.setup_verdict([row("possibly_worse"), row("better")]) == "possibly_worse"
    # A signal with no direction (replies per run) never makes it mixed.
    assert quality.setup_verdict([row("worse"), {"label_key": "better", "worse_when": None}]) == "worse"


def test_worse_models_names_each_agent_and_model_family_that_did_worse():
    rows = [
        {"agent_type": "claude-implementer", "model": "claude-haiku-4-5-20251001", "setup_verdict": "worse",
         "compared_model": "claude-sonnet-5"},
        {"agent_type": quality.MAIN, "model": "claude-sonnet-5", "setup_verdict": "worse",
         "compared_model": "claude-opus-5"},
        # The same model at another effort: about effort, not the model.
        {"agent_type": "Explore", "model": "claude-haiku-4-5", "setup_verdict": "worse",
         "compared_model": "claude-haiku-4-5-20251001"},
        # Worse on some signals is no reason to switch to it either.
        {"agent_type": "Plan", "model": "claude-sonnet-5", "setup_verdict": "mixed", "compared_model": "claude-opus-5"},
        {"agent_type": "Explore", "model": "claude-sonnet-5", "setup_verdict": "possibly_worse",
         "compared_model": "claude-opus-5"},
    ]
    assert set(quality.worse_models(rows)) == {
        ("claude-implementer", "haiku"), ("top-level", "sonnet"), ("Plan", "sonnet"),
    }


def test_section_tables_and_columns():
    runs = _setup_runs("claude-sonnet-5", 6, 2) + [quality.Run(group=quality.MAIN, replies=5, human_messages=2)]
    runs[0].tool_errors_by_tool = {"Bash": 2}
    runs[0].turn_limit = True
    section = quality.build_section(runs)
    assert section.key == "quality"
    tables = {t.name: t for t in section.tables}
    assert set(tables) == {
        "quality_by_agent", "quality_by_setup", "quality_retried", "quality_retry_reasons", "quality_failing_tools",
        "quality_counts", "quality_markers",
    }
    by_agent = tables["quality_by_agent"]
    keys = [c.key for c in by_agent.columns]
    assert {"unfinished_pct", "turn_limit_pct", "tool_errors_pct", "replies_per_run"} <= set(keys)
    groups = [row[0] for row in by_agent.rows]
    assert groups == [quality.MAIN, "Explore"]  # the pooled row only with two agent types or more
    explore = by_agent.rows[groups.index("Explore")]
    assert explore[keys.index("turn_limit_pct")] == pytest.approx(16.7)
    assert tables["quality_failing_tools"].rows[0][:3] == ["Explore", "Bash", 2]
    counts = tables["quality_counts"]
    assert "turn_limit" in [c.key for c in counts.columns]


@pytest.mark.parametrize("state, outcome", [("done", "completed"), ("error", "failed"), ("progress", "stopped")])
def test_a_workflow_agents_run_file_state_is_its_outcome(tmp_path, state, outcome):
    result = _agent(tmp_path, [user_str_line("brief", timestamp=_ts(0)), _reply(1)], agent_type="workflow-subagent")
    result.meta.workflow_agent_state = state
    assert quality.run_facts(result, None).outcome == outcome


def test_a_notification_outcome_wins_over_the_run_file(tmp_path):
    result = _agent(tmp_path, [user_str_line("brief", timestamp=_ts(0)), _reply(1)], agent_id="abc123")
    result.meta.workflow_agent_state = "done"
    assert quality.run_facts(result, None, outcomes={"abc123": "failed"}).outcome == "failed"


# -- retried on a larger model -------------------------------------------------------

_T0 = datetime(2026, 9, 23, 19, 0, tzinfo=timezone.utc)


def _edit_run(model: str, start_min: int, end_min: int, files, group="claude-implementer", kind="subagent",
              edit_min=None) -> quality.Run:
    at = _T0 + timedelta(minutes=end_min if edit_min is None else edit_min)
    return quality.Run(group=group, kind=kind, model=model, start=_T0 + timedelta(minutes=start_min),
                       end=_T0 + timedelta(minutes=end_min), edits=len(files), files_edited=len(files),
                       edit_log=[(at, f) for f in files])


def test_the_same_agent_run_again_on_a_larger_model_on_the_same_files_is_a_retry():
    cheap = _edit_run("claude-haiku-4-5-20251001", 0, 10, ["a", "b", "c"])
    retry = _edit_run("claude-sonnet-5", 20, 30, ["a", "b", "z"])
    quality._mark_retried([cheap, retry])
    assert (cheap.retried, cheap.retried_files, cheap.retried_on) == (True, 2, "claude-sonnet-5")
    assert not retry.retried
    assert quality.SIGNAL_BY_KEY["retried"].num(cheap) == 1.0


@pytest.mark.parametrize("other", [
    # A different agent type afterwards: a reviewer after a writer is often the plan.
    _edit_run("claude-sonnet-5", 20, 30, ["a"], group="code-reviewer"),
    # The main session editing the file afterwards.
    _edit_run("claude-opus-5", 0, 60, ["a"], group=quality.MAIN, kind="top-level", edit_min=20),
    # Working alongside: started before the cheaper run ended.
    _edit_run("claude-sonnet-5", 5, 30, ["a"]),
    # Not a larger model.
    _edit_run("claude-haiku-4-5-20251001", 20, 30, ["a"]),
    # Too long after.
    _edit_run("claude-sonnet-5", 200, 210, ["a"]),
    # Other files.
    _edit_run("claude-sonnet-5", 20, 30, ["z"]),
])
def test_what_is_not_a_retry(other):
    cheap = _edit_run("claude-haiku-4-5-20251001", 0, 10, ["a"])
    quality._mark_retried([other, cheap])
    assert not cheap.retried


def test_session_runs_mark_a_retry_from_the_transcripts(tmp_path):
    def edit(sec, tid, model):
        return _reply(sec, tool_use_block("Edit", tid, {"file_path": "C:/Dev/app/wf.js"}), model=model)

    cheap = _parse(tmp_path, [user_str_line("write it", timestamp=_ts(0)), edit(1, "t1", "claude-haiku-4-5"),
                              _result(2, "t1"), _reply(3, model="claude-haiku-4-5")], "agent-a1.jsonl",
                   kind="subagent", agent_id="agent-a1", agent_type="claude-implementer")
    retry = _parse(tmp_path, [user_str_line("fix it", timestamp=_ts(60)), edit(61, "t2", "claude-sonnet-5"),
                              _result(62, "t2"), _reply(63, model="claude-sonnet-5")], "agent-a2.jsonl",
                   kind="subagent", agent_id="agent-a2", agent_type="claude-implementer")
    runs = quality.session_runs(NS(top=None, subs=[cheap, retry], session_id="s1"), None)
    assert [(r.model, r.retried_files) for r in runs] == [("claude-haiku-4-5", 1), ("claude-sonnet-5", 0)]
    assert "wf.js" not in repr(runs[0].edit_log)


def test_retried_rows_and_the_share_that_keeps_a_model_from_being_suggested():
    runs = []
    for i in range(10):
        cheap = _edit_run("claude-haiku-4-5-20251001", 100 * i, 100 * i + 10, [f"f{i}"])
        runs.append(cheap)
        if i < 2:
            runs.append(_edit_run("claude-sonnet-5", 100 * i + 20, 100 * i + 30, [f"f{i}"]))
    for i in range(20):
        runs.append(_edit_run("claude-haiku-4-5-20251001", 5000 + 100 * i, 5010 + 100 * i, [f"e{i}"], group="Explore"))
    runs.append(_edit_run("claude-sonnet-5", 5020, 5030, ["e0"], group="Explore"))
    quality._mark_retried(runs)
    rows = quality.retried_rows(runs)
    assert [(r["agent_type"], r["runs"], r["retried"], r["retried_pct"]) for r in rows] == [
        ("claude-implementer", 10, 2, 20.0),
        ("Explore", 20, 1, 5.0),
    ]
    assert rows[0]["retried_on"] == "claude-sonnet-5" and rows[0]["files_edited_again"] == 2
    flagged = quality.retried_models(rows)
    assert set(flagged) == {("claude-implementer", "haiku")}  # 1 in 20 is under the share
    assert flagged[("claude-implementer", "haiku")]["reason"] == (
        "2 of its 10 runs on haiku that edited files were run again on sonnet, which edited the same files soon after"
    )


def test_retries_are_shown_per_setup_but_left_out_of_the_comparison():
    runs = _setup_runs("claude-sonnet-5", 30, 1) + _setup_runs("claude-haiku-4-5", 20, 1)
    for run in runs[30:]:
        run.edits, run.retried = 1, True
    for run in runs[:30]:
        run.edits = 1
    rows = {r["model"]: r for r in quality.setup_rows(runs)}
    assert rows["claude-haiku-4-5"]["values"]["retried"] == 100.0
    assert "retried" not in {r["key"] for r in rows["claude-haiku-4-5"]["comparison"]}


# -- markers Claude writes -----------------------------------------------------------


@pytest.mark.parametrize("text, reason", [
    ("[retry: brief] Fix the parser in C:/secret/app.py", "brief"),
    ("  `[Retry: MODEL]` the last run broke the tests", "model"),
    ("Fix the parser; last time it was [retry: brief]", None),  # not at the start
    ("[retry: because] fix it", None),  # not one of the reasons
])
def test_a_brief_starting_with_a_retry_marker_keeps_only_the_reason(tmp_path, text, reason):
    result = _agent(tmp_path, [user_str_line(text, timestamp=_ts(0)), _reply(1)])
    assert result.turns[0].retry_marker == reason
    assert "secret" not in repr(result.turns)


@pytest.mark.parametrize("texts, word", [
    (["Fixed both files.\n\n[result: done]"], "done"),
    (["Two of three tests pass. **[result: partial]**\n"], "partial"),
    (["I was asked to end with [result: done], so here is the rest of the work."], None),  # quoted, not at the end
    (["[result: blocked]", "Actually I got it working."], None),  # the last text block decides
])
def test_a_reply_ending_with_a_result_marker_keeps_only_the_word(tmp_path, texts, word):
    blocks = [{"type": "text", "text": t} for t in texts]
    result = _agent(tmp_path, [_note(0, ["result"]), user_str_line("brief", timestamp=_ts(0)), _reply(1, *blocks)])
    assert result.turns[-1].result_marker == word


def test_run_facts_take_the_markers_and_a_blocked_run_did_not_finish(tmp_path):
    result = _agent(tmp_path, [
        _note(0, ["result"]),
        user_str_line("[retry: tools] run the migration", timestamp=_ts(0)),
        _reply(1, {"type": "text", "text": "Checked the schema."}),
        user_str_line("go on", timestamp=_ts(2)),
        _reply(3, {"type": "text", "text": "No database access.\n[result: blocked]"}),
    ])
    run = quality.run_facts(result, None)
    assert (run.retry_marker, run.result_marker) == ("tools", "blocked")
    assert quality._unfinished(run) == quality._finish_known(run) == 1.0
    done = quality.Run(group="Explore", kind="subagent", result_marker="done")
    assert (quality._unfinished(done), quality._finish_known(done)) == (0.0, 1.0)


def _declared(run: quality.Run, reason: str) -> quality.Run:
    run.retry_marker = reason
    return run


def test_a_retry_that_says_the_brief_was_the_problem_never_counts_against_the_model():
    cheap = _edit_run("claude-haiku-4-5-20251001", 0, 10, ["a", "b"])
    retry = _declared(_edit_run("claude-sonnet-5", 20, 30, ["a", "b"]), "brief")
    quality._mark_retried([cheap, retry])
    assert not cheap.retried and cheap.retried_for == "brief"


def test_a_retry_that_says_the_model_counts_even_for_another_agent_and_other_files():
    cheap = _edit_run("claude-haiku-4-5-20251001", 0, 10, ["a"])
    retry = _declared(_edit_run("claude-sonnet-5", 20, 30, ["z"], group="code-fixer"), "model")
    quality._mark_retried([cheap, retry])
    assert (cheap.retried, cheap.retried_files, cheap.retried_on, cheap.retried_for) == (
        True, 0, "claude-sonnet-5", "model")


def test_a_model_retry_on_the_same_model_records_the_reason_but_is_not_a_larger_model_retry():
    cheap = _edit_run("claude-sonnet-5", 0, 10, ["a"])
    retry = _declared(_edit_run("claude-sonnet-5", 20, 30, ["a"]), "model")
    quality._mark_retried([cheap, retry])
    assert not cheap.retried and cheap.retried_for == "model"


def test_a_declared_retry_is_matched_to_the_run_whose_files_it_edits_then_the_latest():
    edited = _edit_run("claude-haiku-4-5-20251001", 0, 10, ["a"], group="writer")
    later = _edit_run("claude-haiku-4-5-20251001", 5, 15, ["q"])
    retry = _declared(_edit_run("claude-sonnet-5", 20, 30, ["a"]), "other")
    quality._mark_retried([edited, later, retry])
    assert (edited.retried_for, later.retried_for) == ("other", "")
    # No files in common: the same agent type, then the one that ended last.
    other_type = _edit_run("claude-haiku-4-5-20251001", 0, 18, ["x"], group="writer")
    earlier = _edit_run("claude-haiku-4-5-20251001", 0, 10, ["x"])
    latest = _edit_run("claude-haiku-4-5-20251001", 5, 15, ["y"])
    retry = _declared(_edit_run("claude-sonnet-5", 20, 30, ["z"]), "other")
    quality._mark_retried([other_type, earlier, latest, retry])
    assert [r.retried_for for r in (other_type, earlier, latest)] == ["", "", "other"]


def test_retry_reason_rows_and_the_retried_reason_text():
    runs = []
    for i, reason in enumerate(["model", "model", "brief", "brief", "brief"]):
        runs.append(_edit_run("claude-haiku-4-5-20251001", 100 * i, 100 * i + 10, [f"f{i}"]))
        runs.append(_declared(_edit_run("claude-sonnet-5", 100 * i + 20, 100 * i + 30, [f"f{i}"]), reason))
    quality._mark_retried(runs)
    [row] = quality.retry_reason_rows(runs)
    assert (row["agent_type"], row["retries"], row["said_model"], row["said_brief"], row["said_tools"]) == (
        "claude-implementer", 5, 2, 3, 0)
    [retried] = quality.retried_rows(runs)
    assert (retried["runs"], retried["retried"], retried["said_model"]) == (5, 2, 2)
    reason = quality.retried_models([retried])[("claude-implementer", "haiku")]["reason"]
    assert reason == "2 of its 5 runs on haiku that edited files were run again on sonnet, and 2 of those retries " \
                     "said haiku wasn't enough"


def test_markers_table_counts_who_could_have_written_them_and_what_they_cost(tmp_path):
    pricing = load_pricing()

    def agent(name, agent_type, first, last, model="claude-haiku-4-5"):
        return _parse(tmp_path, [_note(0, ["result"], agent_type=agent_type), user_str_line(first, timestamp=_ts(0)),
                                 _reply(1, {"type": "text", "text": last}, model=model)],
                      f"agent-{name}.jsonl", kind="subagent", agent_id=f"agent-{name}", agent_type=agent_type)

    subs = [
        agent("a1", "claude-implementer", "write it", "Done.\n[result: done]"),
        agent("a2", "claude-implementer", "[retry: brief] write it, tests in tests/", "Half.\n[result: partial]"),
        agent("a3", "Explore", "find it", "Found."),
    ]
    top = _parse(tmp_path, [user_str_line("hi", timestamp=_ts(0)), _reply(1, model="claude-opus-5-5")], "top.jsonl",
                 kind="top-level")
    runs = quality.session_runs(NS(top=top, subs=subs, session_id="s1"), pricing)
    table = next(t for t in quality.build_section(runs).tables if t.name == "quality_markers")
    keys = [c.key for c in table.columns]
    by_marker = {row[0]: dict(zip(keys, row)) for row in table.rows}
    retry, result = by_marker["[retry: ...]"], by_marker["[result: ...]"]
    assert (retry["runs"], retry["of_runs"], retry["breakdown"]) == (1, 3, "brief 1")
    assert (result["runs"], result["of_runs"], result["breakdown"]) == (2, 2, "done 1, partial 1")  # Explore left out
    assert result["tokens"] == 2 * quality.MARKER_TOKENS
    haiku = pricing.resolve_model("claude-haiku-4-5").rates.output
    opus = pricing.resolve_model("claude-opus-5-5").rates.output
    assert result["cost"] == pytest.approx(round(2 * quality.MARKER_TOKENS * haiku / 1e6, 4))
    assert retry["cost"] == pytest.approx(round(quality.MARKER_TOKENS * opus / 1e6, 4))  # the main session wrote it
    counts = next(t for t in quality.build_section(runs).tables if t.name == "quality_counts")
    row = dict(zip([c.key for c in counts.columns], counts.rows[[r[0] for r in counts.rows].index("claude-implementer")]))
    assert (row["said_done"], row["said_partial"], row["said_blocked"]) == (1, 1, 0)



def test_marker_cost_is_priced_at_the_writing_turn_fast_mode_included(tmp_path):
    pricing = load_pricing()
    top = _parse(tmp_path, [
        user_str_line("hi", timestamp=_ts(0)),
        _reply(1, tool_use_block("Agent", "toolu_A", {"prompt": "[retry: brief] write it"}), model="claude-opus-5-5",
               speed="fast"),
        _result(2, "toolu_A"),
        _reply(3, model="claude-opus-5-5"),
    ], "top.jsonl", kind="top-level")
    sub = _parse(tmp_path, [
        _note(1, ["result"], agent_type="claude-implementer"),
        user_str_line("[retry: brief] write it", timestamp=_ts(1)),
        _reply(2, {"type": "text", "text": "Done.\n[result: done]"}, model="claude-opus-5-5", speed="fast"),
    ], "agent-a1.jsonl", kind="subagent", agent_id="agent-a1", agent_type="claude-implementer", tool_use_id="toolu_A")
    [_, run] = quality.session_runs(NS(top=top, subs=[sub], session_id="s1"), pricing)
    fast_output = 2 * pricing.resolve_model("claude-opus-5-5").rates.output
    # The retry word was written by the fast turn that started the agent,
    # not the main session's last (standard) turn.
    assert run.retry_marker_cost == pytest.approx(quality.MARKER_TOKENS * fast_output / 1e6)
    assert run.result_marker_cost == pytest.approx(quality.MARKER_TOKENS * fast_output / 1e6)

def test_marker_lines_stay_short():
    assert quality.MARKER_LINES.startswith(quality.MARKER_HEADING + "\n")
    assert len(quality.MARKER_LINES) / 4 < 120
    for word in quality.RETRY_REASONS:
        assert f"[retry: {word}]" in quality.MARKER_LINES
    for word in quality.RESULT_WORDS:
        assert f"[result: {word}]" in quality.MARKER_LINES

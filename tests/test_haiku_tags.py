"""Claude Haiku writes the reply tags (``[capture] tagger = "haiku"``): the
note that then asks Claude for none, the Stop hook's excerpt and the
worker that asks Haiku, the tag file it writes (``haiku_tags``), and how
the tags reach the turns they belong to and what they cost.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from importlib import resources
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from claudeglass import capture, capture_catalogue as cat, cli, haiku_tags, hook_health
from claudeglass.config import CaptureConfig, ConfigError, load_config, set_capture
from claudeglass.model import TranscriptMeta
from claudeglass.parse import parse_transcript
from claudeglass.pricing import load_pricing

from helpers import attachment_line, tool_use_block, turn_line, user_str_line, write_jsonl

SCRIPT = Path(str(resources.files("claudeglass") / "hooks" / cat.HOOK_SCRIPT))
MODULE = SCRIPT.with_name(cat.HOOK_MODULE)


def _load_hook():
    spec = importlib.util.spec_from_file_location("_haiku_hook_under_test", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HOOK = _load_hook()
CATALOGUE = HOOK.load_catalogue()
STANDARD = cat.level_includes("standard")
HAIKU = {"capture": {"level": "standard", "tagger": "haiku"}}


def _at(second: int) -> str:
    return (datetime(2026, 9, 27, 10, 0, tzinfo=timezone.utc) + timedelta(seconds=second)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )


def _transcript(tmp_path, lines, name="s.jsonl") -> Path:
    path = tmp_path / name
    write_jsonl(path, lines)
    return path


def _plan_result(second: int, *, is_error: bool = False, text: str = "ok", **line) -> dict:
    """The answer to an earlier plan's ``ExitPlanMode`` call ("tp")."""
    return {"type": "user", "timestamp": _at(second), **line, "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "tp", "content": text, "is_error": is_error}]}}


def _turn(tmp_path, *, plan=False, skill="", earlier_plan=False, plan_answer=None, between=()) -> Path:
    """One earlier exchange, then a message Claude answered with a test
    run, an edit and a final reply. An earlier plan is approved in the
    dialog unless ``plan_answer`` says otherwise; ``between`` lines come
    before your last message."""
    first = [tool_use_block("ExitPlanMode", "tp", {"plan": "1. do it"})] if earlier_plan else []
    answer = [plan_answer or _plan_result(2)] if earlier_plan else []
    now = [tool_use_block("Bash", "t1", {"command": "pytest -q tests/test_calc.py\necho done"}),
           tool_use_block("Edit", "t2", {"file_path": "/w/calc.py"})]
    if plan:
        now.append(tool_use_block("ExitPlanMode", "t3", {"plan": "1. fix"}))
    if skill:
        now.append(tool_use_block("Skill", "t4", {"skill": skill}))
    return _transcript(tmp_path, [
        user_str_line("Add a calculator module", timestamp=_at(0)),
        turn_line(timestamp=_at(1), content=[*first, {"type": "text", "text": "Added."}], output_tokens=50),
        *answer,
        *between,
        user_str_line("add() returns the wrong sum, fix it", timestamp=_at(10)),
        turn_line(timestamp=_at(11), content=now, output_tokens=300),
        {"type": "user", "timestamp": _at(12), "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "1 failed", "is_error": True}]}},
        turn_line(timestamp=_at(13), content=[{"type": "text", "text": "Fixed add(); the tests pass now."}],
                  output_tokens=40),
    ])


def _stop(path: Path, **extra) -> dict:
    return {"hook_event_name": "Stop", "session_id": "s1", "cwd": "/w", "transcript_path": str(path),
            "last_assistant_message": "Fixed add(); the tests pass now.", **extra}


def _answer(result="[cg: task=bugfix brief=clear level=easy]", usd=0.0014):
    return {"result": result, "total_cost_usd": usd, "usage": {"input_tokens": 1200, "output_tokens": 30},
            "modelUsage": {"claude-haiku-4-5-20251001": {}}}


# -- the note and the hook entries -----------------------------------------------


@pytest.mark.parametrize("level", ["essentials", "standard", "deep"])
def test_the_note_asks_claude_for_no_tag_while_haiku_writes_them(level):
    ids = cat.level_includes(level)
    note = cat.note_text(ids, "main", tagger="haiku")
    assert "[cg:" not in note and cat.MAIN_TAG_INTRO not in note
    # Nothing left to ask at the start but the reminder, where it's on.
    if level == "deep":
        assert note.splitlines()[0] == f"{cat.NOTE_MARKER}{cat.NOTE_VERSION} feedback_reminder"
    else:
        assert note == ""
    # The hook builds the same text, and a subagent is asked for nothing.
    assert HOOK.build_note(CATALOGUE, ids, "main", tagger="haiku") == note
    assert cat.note_text(ids, "subagent", tagger="haiku") == cat.note_text(ids, "subagent") == ""
    # What Haiku is told: a line for each key the note would have asked
    # for, spelled out where the plain line read differently.
    judge = cat.judge_text(ids)
    assert HOOK.build_judge_prompt(CATALOGUE, ids) == judge
    for key in cat.tagged_keys(ids):
        assert cat.JUDGE_LINES.get(key, cat.METRICS_BY_ID[key].main_line) in judge
    assert set(cat.JUDGE_LINES) <= set(cat.tagged_keys(cat.level_includes("deep")))
    for key, line in cat.JUDGE_LINES.items():
        assert line.startswith(f"{key}: {'|'.join(cat.TAG_VOCAB[key])}")


def test_the_reminder_line_says_end_your_reply_when_there_is_no_tag():
    note = cat.note_text(("task", "feedback_reminder"), "main", tagger="haiku")
    assert "end your reply with this" in note and "before your tag" not in note
    assert cat.REMINDER_LABEL in note


def test_haiku_needs_a_foreground_stop_entry():
    specs = cat.hook_specs(cat.level_metrics("essentials") + (cat.HAIKU_TAGGER_HOOK,))
    stops = [spec for spec in specs if spec[1] == "Stop"]
    # One entry, shared with turn_signals, in the foreground: claude -p
    # exits without waiting for a background hook.
    assert stops == [(cat.HOOK_SCRIPT, "Stop", "", False)]
    alone = cat.hook_specs(("task", cat.HAIKU_TAGGER_HOOK))
    assert (cat.HOOK_SCRIPT, "Stop", "", False) in alone
    assert not any(spec[1] == "SessionStart" for spec in alone)  # nothing left to say at start
    # Without a key to ask for, there is nothing for Haiku to do.
    assert not any(spec[1] == "Stop" for spec in cat.hook_specs(("session_end", cat.HAIKU_TAGGER_HOOK)))


def test_the_config_knows_who_writes_the_tags(tmp_path):
    assert CaptureConfig().tagger == "claude"
    set_capture(tmp_path, level="essentials", tagger="haiku")
    capture_config = load_config(config_dir=tmp_path).capture
    assert capture_config.haiku_tags and cat.HAIKU_TAGGER_HOOK in capture_config.hook_metrics()
    assert any(spec.event == "Stop" and not spec.async_ for spec in hook_health.capture_specs(capture_config.hook_metrics()))
    with pytest.raises(ConfigError):
        set_capture(tmp_path, tagger="gpt")
    (tmp_path / "config.toml").write_text('[capture]\nlevel = "free"\ntagger = 3\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(config_dir=tmp_path)


def test_the_large_result_note_goes_only_to_a_main_session_claude_tags():
    config = {"capture": {"level": "deep", "tagger": "haiku"}}
    big = {"hook_event_name": "PostToolUse", "session_id": "s1", "cwd": "/w", "tool_name": "Read"}
    raw = cat.BIG_OUTPUT_TOKENS * 4
    assert HOOK.note_for(big, config, CATALOGUE, result_len=raw) == ""
    assert HOOK.note_for(big, {"capture": {"level": "deep"}}, CATALOGUE, result_len=raw)
    # A subagent's report carries no tag for the word, whoever tags.
    for tagger in ("claude", "haiku"):
        assert HOOK.note_for({**big, "agent_id": "a1"}, {"capture": {"level": "deep", "tagger": tagger}}, CATALOGUE,
                             result_len=raw) == ""


# -- agent runs -------------------------------------------------------------------


def test_agent_runs_are_judged_whoever_writes_the_tags():
    for ids in (cat.level_metrics("essentials"), STANDARD, STANDARD + (cat.HAIKU_TAGGER_HOOK,)):
        specs = cat.hook_specs(ids)
        # In the foreground, as Stop is for Haiku, and never at agent start.
        assert (cat.HOOK_SCRIPT, "SubagentStop", "", False) in specs
        assert not any(spec[1] == "SubagentStart" for spec in specs)
    assert not any(spec[1] == "SubagentStop" for spec in cat.hook_specs(("task", "session_end")))
    text = cat.agent_judge_text(STANDARD)
    assert text == HOOK.build_agent_judge_prompt(CATALOGUE, STANDARD)
    assert text.startswith(cat.AGENT_JUDGE_INTRO) and text.endswith(cat.AGENT_JUDGE_RULE)
    for metric_id in ("result", "retry", "agent_brief"):
        assert cat.METRICS_BY_ID[metric_id].sub_line in text
    # Haiku's model fit verdict is no longer asked for.
    assert "fit:" not in text and "fit" not in cat.AGENT_JUDGE_VOCAB
    # Whether an agent used your rules can't be told from outside it.
    assert "rules" in cat.RETIRED_METRIC_IDS and "rules" not in cat.METRICS_BY_ID
    assert cat.agent_judge_text(("task",)) == ""


def _agent_run(tmp_path, *, report="Added docstrings to mod1.py and mod2.py; mod3.py is still to do.") -> tuple[Path, Path]:
    """A session that ran one agent to the end and one in the background,
    then started ``agent-a3``, which edited two files, ran a test that
    failed, and reported."""
    folder = tmp_path / "sess" / "subagents"
    folder.mkdir(parents=True)
    agent = _transcript(folder, [
        user_str_line("Add a one-line docstring to every function in mod1.py, mod2.py and mod3.py.", timestamp=_at(20)),
        turn_line(timestamp=_at(21), model="claude-sonnet-5", message_id="msg_a1", output_tokens=80, content=[
            tool_use_block("Edit", "e1", {"file_path": "/w/mod1.py"}),
            tool_use_block("Edit", "e2", {"file_path": "/w/mod2.py"}),
            tool_use_block("Bash", "b1", {"command": "pytest -q tests/test_mod.py\necho ok"})]),
        {"type": "user", "timestamp": _at(22), "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "b1", "content": "1 failed", "is_error": True}]}},
        turn_line(timestamp=_at(23), model="claude-sonnet-5", message_id="msg_a2", output_tokens=40,
                  content=[{"type": "text", "text": report}]),
    ], "agent-a3.jsonl")
    brief = "Add a one-line docstring to every function in mod1.py, mod2.py and mod3.py."
    session = _transcript(tmp_path, [
        user_str_line("Document the mod files", timestamp=_at(0)),
        turn_line(timestamp=_at(1), message_id="m1", content=[
            tool_use_block("Agent", "tA", {"prompt": "Add docstrings to mod1.py, mod2.py and mod3.py"})]),
        {"type": "user", "timestamp": _at(5), "toolUseResult": {
            "status": "completed", "agentId": "a1", "agentType": "general-purpose",
            "prompt": "Add docstrings to mod1.py, mod2.py and mod3.py", "content": [
                {"type": "text", "text": "I couldn't finish: mod2.py needs a decision on the API."}]},
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tA", "content": "x"}]}},
        turn_line(timestamp=_at(6), message_id="m2", content=[
            tool_use_block("Agent", "tB", {"subagent_type": "Explore", "prompt": "Find the mod files"})]),
        {"type": "user", "timestamp": _at(6), "toolUseResult": {
            "status": "async_launched", "agentId": "a2", "prompt": "Find the mod files"},
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tB", "content": "x"}]}},
        user_str_line("<task-notification>\n<task-id>a2</task-id>\n<status>completed</status>\n<result>mod1.py, "
                      "mod2.py and mod3.py</result>\n</task-notification>", timestamp=_at(8)),
        # Claude says why, then starts this run and, beside it, another.
        turn_line(timestamp=_at(18), message_id="m3", content=[
            {"type": "text", "text": "The first run stopped at mod2.py; running it again with the API decided."},
            tool_use_block("Agent", "tC", {"prompt": brief}),
            tool_use_block("Agent", "tD", {"subagent_type": "Plan", "prompt": "Plan the README section"})]),
        {"type": "user", "timestamp": _at(19), "toolUseResult": {
            "status": "async_launched", "agentId": "a3", "prompt": brief},
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tC", "content": "x"}]}},
        # A run started after it is never "earlier".
        turn_line(timestamp=_at(30), message_id="m4", content=[tool_use_block("Agent", "tE", {"prompt": "Later work"})]),
    ], "sess.jsonl")
    return session, agent


def _subagent_stop(session: Path, agent: Path, **extra) -> dict:
    return {"hook_event_name": "SubagentStop", "session_id": "s1", "cwd": "/w", "transcript_path": str(session),
            "agent_id": "a3", "agent_type": "general-purpose", "agent_transcript_path": str(agent),
            "stop_hook_active": False, **extra}


class _Clock:
    """Time that passes only while the worker sleeps, so a wait of seconds
    costs none; ``on_sleep(n)`` runs on the n-th sleep (the agent's file
    being written, say)."""

    def __init__(self, on_sleep=None):
        self.now = 0.0
        self.sleeps = 0
        self._on_sleep = on_sleep

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.sleeps += 1
        if self._on_sleep:
            self._on_sleep(self.sleeps)


def _agent_job(session: Path, agent: Path, **extra) -> dict:
    """What the SubagentStop hook hands the worker."""
    return HOOK.agent_judge_job(_subagent_stop(session, agent, **extra), {"capture": {"level": "standard"}}, CATALOGUE)


def _prepared(session: Path, agent: Path, clock: _Clock | None = None, **extra) -> dict:
    """That job, read the way the worker reads it once the agent's
    transcript has settled."""
    clock = clock or _Clock()
    return HOOK.prepare_agent_job(_agent_job(session, agent, **extra), CATALOGUE, sleep=clock.sleep, clock=clock)


def _append(path: Path, *lines) -> None:
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        for line in lines:
            handle.write(json.dumps(line) + "\n")


_RELAYED = (
    "[Workflow harness \u2014 user request] The line below is what the user typed to start this workflow, "
    "relayed as it was typed:\n  Run the review workflow on the payments module and tell me what is wrong."
)
_COMPUTED = (
    "[Workflow harness \u2014 computed task] The task text below was computed at runtime by a workflow script. "
    "It was not typed by this session's user. The computed task text follows:\n  Review refunds in "
    "payments.py.\n  Return verdict and findings."
)


def _workflow_agent(tmp_path, *after, relayed=_RELAYED, name="agent-w1.jsonl") -> Path:
    """A workflow agent: the harness hands it the line the workflow was
    started with, then the task the script computed; it reads a file, and
    ``after`` is what comes next."""
    return _transcript(tmp_path, [
        user_str_line(relayed, timestamp=_at(0)),
        user_str_line(_COMPUTED, timestamp=_at(0)),
        turn_line(timestamp=_at(1), model="claude-sonnet-5", message_id="msg_w1", output_tokens=60, content=[
            tool_use_block("Read", "r1", {"file_path": "/w/payments.py"})]),
        {"type": "user", "timestamp": _at(2), "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "r1", "content": "def pay(): ..."}]}},
        *after,
    ], name)


def _handback(call_id: str, name: str, given: dict, *, second: int = 3, message_id: str = "msg_w2", text: str = "") -> dict:
    content = ([{"type": "text", "text": text}] if text else []) + [tool_use_block(name, call_id, given)]
    return turn_line(timestamp=_at(second), model="claude-sonnet-5", message_id=message_id, output_tokens=900,
                     content=content)


def _workflow_session(tmp_path) -> Path:
    return _transcript(tmp_path, [user_str_line("Run the review workflow", timestamp=_at(0))], "sess.jsonl")


def _workflow_excerpt(tmp_path, agent: Path) -> str:
    return _prepared(_workflow_session(tmp_path), agent, agent_id="w1", agent_type="workflow-subagent")["excerpt"]


def test_the_agent_excerpt_says_what_the_run_did_and_what_came_before(tmp_path):
    session, agent = _agent_run(tmp_path)
    job = _agent_job(session, agent)
    assert job["kind"] == "agent" and job["system"] == cat.agent_judge_text(STANDARD)
    assert job["keys"] == ["brief", "missing", "result", "retry"]
    prepared = _prepared(session, agent)
    assert prepared["reply"] == "msg_a2"
    excerpt = prepared["excerpt"]
    assert "Agent type: general-purpose, on claude-sonnet-5." in excerpt
    assert 'Its brief: "Add a one-line docstring to every function in mod1.py, mod2.py and mod3.py."' in excerpt
    assert "files changed: 2 (mod1.py, mod2.py)" in excerpt and "tool errors: 1" in excerpt
    assert "`pytest -q tests/test_mod.py`" in excerpt and "echo ok" not in excerpt
    assert excerpt.count("mod3.py is still to do") == 1
    # Why the session started it, in Claude's words.
    assert 'What the session said as it started this run: "The first run stopped at mod2.py; running it again ' \
        'with the API decided."' in excerpt
    # The earlier runs, oldest first, the background one's report from its
    # notification; never the run being judged, one started beside it, or
    # one started after it.
    assert '1. general-purpose. Brief: "Add docstrings to mod1.py, mod2.py and mod3.py" Report ended: ' \
        '"I couldn\'t finish: mod2.py needs a decision on the API."' in excerpt
    assert '2. Explore. Brief: "Find the mod files" Report ended: "mod1.py, mod2.py and mod3.py"' in excerpt
    assert "\n3. " not in excerpt and "README" not in excerpt and "Later work" not in excerpt
    # Tool output is never in it.
    assert "1 failed" not in excerpt


def test_a_workflow_agents_brief_is_the_task_its_script_computed_and_the_relayed_line_is_context(tmp_path):
    agent = _workflow_agent(tmp_path, _handback("so1", "StructuredOutput", {"verdict": "sound"}))
    excerpt = _workflow_excerpt(tmp_path, agent)
    # The computed task is the brief; the harness line before each is dropped.
    assert 'Its brief: "Review refunds in payments.py. Return verdict and findings."' in excerpt
    assert 'The line the workflow was started with (context, not the brief): "Run the review workflow on the ' \
        'payments module and tell me what is wrong."' in excerpt
    assert "Workflow harness" not in excerpt and "computed at runtime" not in excerpt
    # The relayed line is cut at its own, shorter limit.
    long = _workflow_agent(
        tmp_path, relayed=_RELAYED.replace("tell me what is wrong", "tell me " + "what is wrong " * 40),
        name="agent-w2.jsonl",
    )
    relay = next(line for line in _workflow_excerpt(tmp_path, long).splitlines() if line.startswith("The line the"))
    quoted = relay.split(': "', 1)[1]
    assert "[...]" in quoted and len(quoted) <= CATALOGUE["judge"]["agent"]["limits"]["relay"] + 10
    # A workflow agent without a relayed line has the computed task alone; any other agent keeps its first
    # prompt as it is.
    alone = _transcript(tmp_path, [
        user_str_line(_COMPUTED, timestamp=_at(0)),
        turn_line(timestamp=_at(1), message_id="msg_w1", content=[_said("Done.")]),
    ], "agent-w3.jsonl")
    text = _workflow_excerpt(tmp_path, alone)
    assert 'Its brief: "Review refunds in payments.py. Return verdict and findings."' in text
    assert "context, not the brief" not in text
    brief, relay_text, workflow = HOOK._agent_brief(HOOK._tail(str(alone)))
    assert workflow and relay_text == ""
    plain = _agent_run(tmp_path)[1]
    brief, relay_text, workflow = HOOK._agent_brief(HOOK._tail(str(plain)))
    assert brief.startswith("Add a one-line docstring") and relay_text == "" and not workflow


def test_a_workflow_agents_answer_is_shown_field_by_field_each_value_cut_on_its_own(tmp_path):
    findings = [f"finding {n}: " + "refunds skip the audit log " * 3 for n in range(12)]
    agent = _workflow_agent(tmp_path, _handback(
        "so1", "StructuredOutput", {"verdict": "sound", "findings": findings, "summary": "x" * 400},
        text="Now let me compile my findings into the structured format.",
    ))
    facts: dict = {}
    excerpt, reply = HOOK.agent_excerpt(
        HOOK._tail(str(agent)), {"cwd": "/w", "agent_type": "workflow-subagent"}, CATALOGUE, [], "", facts)
    assert reply == "msg_w2" and facts["answered"] and facts["workflow"]
    lines = excerpt.splitlines()
    start = lines.index("Its answer, handed back as structured output, field by field:")
    assert lines[start + 1] == "  verdict: sound"
    shown = lines[start + 2]
    limit = CATALOGUE["judge"]["agent"]["limits"]["answer_field"]
    # A long list shows its start and its end, not just its tail.
    assert shown.startswith('  findings: ["finding 0: refunds') and shown.endswith('log "]')
    assert "[...]" in shown and len(shown) <= len("  findings: ") + limit + 10
    assert lines[start + 3].startswith("  summary: xxx") and "[...]" in lines[start + 3]
    # What it said before the call is no report.
    assert "compile my findings" not in excerpt and "The end of its report" not in excerpt


def test_a_workflow_agent_with_many_fields_names_the_first_and_counts_the_rest(tmp_path):
    fields = {f"field{n}": n for n in range(14)}
    agent = _workflow_agent(tmp_path, _handback("so1", "StructuredOutput", fields))
    limits = CATALOGUE["judge"]["agent"]["limits"]
    excerpt = _workflow_excerpt(tmp_path, agent)
    assert f"  field{limits['answer_fields'] - 1}: {limits['answer_fields'] - 1}" in excerpt
    assert f"  field{limits['answer_fields']}:" not in excerpt
    assert f"and {14 - limits['answer_fields']} more fields" in excerpt


@pytest.mark.parametrize("given, shown", [
    ({"message": "Reviewed refunds; two paths skip the audit log."},
     'Its report, handed back through a handback tool: "Reviewed refunds; two paths skip the audit log."'),
    # A message with more beside it is an answer in fields.
    ({"message": "Reviewed refunds.", "status": "ok"}, "  message: Reviewed refunds."),
])
def test_a_subagent_handback_is_the_agents_report_and_a_short_closing_remark_keeps_it_so(tmp_path, given, shown):
    agent = _workflow_agent(tmp_path, _handback("sh1", "SubagentHandback", given))
    facts: dict = {}
    excerpt, _reply = HOOK.agent_excerpt(HOOK._tail(str(agent)), {"agent_type": "x"}, CATALOGUE, [], "", facts)
    assert shown in excerpt and facts["answered"]
    assert "The end of its report" not in excerpt
    remark = _workflow_agent(
        tmp_path, _handback("sh1", "SubagentHandback", given),
        turn_line(timestamp=_at(5), message_id="msg_w3", content=[_said("Handed back.")]), name="agent-w4.jsonl")
    facts = {}
    text, reply = HOOK.agent_excerpt(HOOK._tail(str(remark)), {"agent_type": "x"}, CATALOGUE, [], "", facts)
    # Still the answer, with the remark noted beside it; the reply it ended on is the remark's.
    assert shown in text and 'After the answer it added: "Handed back."' in text and facts["answered"]
    assert "The end of its report" not in text and reply == "msg_w3"


def test_words_that_run_on_after_an_answer_are_the_report_again(tmp_path):
    long = "Having handed that back, here is more detail on every one of the findings. " * 8
    assert len(long) > CATALOGUE["judge"]["agent"]["limits"]["closing"]
    agent = _workflow_agent(
        tmp_path, _handback("so1", "StructuredOutput", {"summary": "draft"}),
        turn_line(timestamp=_at(5), message_id="msg_w3", content=[_said(long)]))
    facts: dict = {}
    excerpt, _reply = HOOK.agent_excerpt(HOOK._tail(str(agent)), {"agent_type": "x"}, CATALOGUE, [], "", facts)
    assert "The end of its report:" in excerpt and "structured output" not in excerpt and not facts["answered"]
    # An agent that wrote words and called no answer tool has a report.
    plain = _workflow_agent(tmp_path, turn_line(timestamp=_at(5), message_id="msg_w3", content=[_said("Done.")]),
                            name="agent-w5.jsonl")
    facts = {}
    HOOK.agent_excerpt(HOOK._tail(str(plain)), {"agent_type": "x"}, CATALOGUE, [], "", facts)
    assert not facts["answered"] and facts["workflow"]


def test_the_quality_check_and_the_hook_name_the_same_answer_tools():
    from claudeglass import quality

    assert set(CATALOGUE["judge"]["agent"]["answer_tools"]) == set(cat.AGENT_ANSWER_TOOLS) == set(quality._ANSWER_TOOLS)
    assert {"StructuredOutput", "SubagentHandback"} <= set(cat.AGENT_ANSWER_TOOLS)


# -- the worker waits for the agent's last lines -----------------------------------


def test_the_stop_hook_returns_at_once_and_leaves_the_reading_to_the_worker(tmp_path):
    session, agent = _agent_run(tmp_path)
    job = _agent_job(session, agent)
    assert job["kind"] == "agent" and job["reply"] == "" and "excerpt" not in job and "facts" not in job
    assert set(job["payload"]) == {"agent_type", "agent_id", "cwd", "transcript_path", "agent_transcript_path",
                                   "last_assistant_message"}
    # The hook never reads the agent's transcript: a path that isn't a file is no job, one that is is a job
    # even before the agent has written its report.
    assert _agent_job(session, tmp_path / "nowhere.jsonl") is None


def test_the_worker_waits_for_an_answer_that_is_written_late(tmp_path):
    agent = _workflow_agent(tmp_path)  # the agent has read its file and nothing more

    def write_answer(sleeps: int) -> None:
        if sleeps == 3:
            _append(agent, _handback("so1", "StructuredOutput", {"verdict": "sound"}))

    clock = _Clock(write_answer)
    prepared = _prepared(_workflow_session(tmp_path), agent, clock, agent_id="w1")
    assert "verdict: sound" in prepared["excerpt"] and prepared["facts"]["answered"] and "err" not in prepared
    # Not the quiet seconds, not the cap: the answer ended the wait.
    assert clock.sleeps == 3 and clock.now < CATALOGUE["judge"]["agent"]["wait"]["quiet_s"]


def test_a_trailing_tool_result_is_not_an_answer_so_the_worker_waits_until_the_file_goes_quiet(tmp_path):
    # The file ends on a tool result, as a run that is still working does and a finished one does too.
    agent = _workflow_agent(tmp_path)
    wait = CATALOGUE["judge"]["agent"]["wait"]
    clock = _Clock()
    prepared = _prepared(_workflow_session(tmp_path), agent, clock, agent_id="w1")
    assert clock.now >= wait["quiet_s"] and clock.now < wait["cap_s"]
    assert "err" not in prepared and not prepared["facts"]["answered"]
    assert "The end of its report" in prepared["excerpt"]


def test_the_wait_starts_again_while_the_file_is_still_growing(tmp_path):
    agent = _workflow_agent(tmp_path)
    wait = CATALOGUE["judge"]["agent"]["wait"]

    def work(sleeps: int) -> None:
        if sleeps <= 8:  # a line every poll for four seconds, longer than the quiet seconds
            _append(agent, turn_line(timestamp=_at(10 + sleeps), message_id=f"msg_g{sleeps}", content=[_said("Working.")]))

    clock = _Clock(work)
    prepared = _prepared(_workflow_session(tmp_path), agent, clock, agent_id="w1")
    assert "err" not in prepared
    # The quiet seconds counted from the last line, not from the start, and still well short of the cap.
    assert 8 * wait["poll_s"] + wait["quiet_s"] - wait["poll_s"] <= clock.now < wait["cap_s"]


def test_a_file_that_never_stops_growing_is_given_up_on_at_the_cap_and_no_haiku_call_is_made(tmp_path):
    agent = _workflow_agent(tmp_path)
    wait = CATALOGUE["judge"]["agent"]["wait"]
    assert wait["cap_s"] < cat.JUDGE_TIMEOUT_S  # well inside the worker's own budget

    def work(sleeps: int) -> None:
        _append(agent, turn_line(timestamp=_at(10 + sleeps), message_id=f"msg_g{sleeps}", content=[_said("Still going.")]))

    clock = _Clock(work)
    job = _agent_job(_workflow_session(tmp_path), agent, agent_id="w1")
    asked = []
    record = HOOK.run_judge(tmp_path / "cg", CATALOGUE, job, ask=lambda *a: asked.append(a), sleep=clock.sleep, clock=clock)
    assert not asked
    assert clock.now >= wait["cap_s"] and clock.now <= wait["cap_s"] + wait["poll_s"]
    assert record["err"] == "no_answer" and record["agent"] == "" and "usd" not in record
    [judged] = haiku_tags.load(tmp_path / "cg")
    assert (judged.kind, judged.tag, judged.error) == ("agent", None, "no_answer")
    # No call was made, so none is charged for or counted among the calls that cost.
    summary = haiku_tags.summary(tmp_path / "cg", kind="agent")
    assert summary.errors == {"no_answer": 1} and summary.usd_per_call is None


def test_a_subagent_stop_reaches_the_worker_with_the_lean_job_and_a_scripted_run_gets_none(tmp_path, monkeypatch):
    config_dir = tmp_path / "cg"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text('[capture]\nlevel = "essentials"\n', encoding="utf-8")
    session, agent = _agent_run(tmp_path)
    spawned: list[dict] = []
    monkeypatch.setattr(HOOK, "spawn_judge", lambda folder, job: spawned.append(job))
    monkeypatch.setattr(HOOK, "_tail", lambda *a, **k: pytest.fail("the hook read a transcript"))
    payload = json.dumps(_subagent_stop(session, agent)).encode("utf-8")

    def stop(entrypoint: str) -> None:
        monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", entrypoint)
        monkeypatch.setattr(sys, "stdin", NS(buffer=io.BytesIO(payload)))
        HOOK._run(["--config-dir", str(config_dir)])

    stop("claude-desktop")
    [job] = spawned
    assert job["kind"] == "agent" and job["reply"] == "" and "excerpt" not in job
    assert job["payload"]["agent_transcript_path"] == str(agent)
    spawned.clear()
    stop("sdk-cli")  # nobody at the screen
    assert spawned == []


def test_a_run_whose_transcript_is_gone_writes_no_line(tmp_path):
    session, agent = _agent_run(tmp_path)
    job = _agent_job(session, agent)
    agent.unlink()
    clock = _Clock()
    assert HOOK.run_judge(tmp_path / "cg", CATALOGUE, job, ask=lambda *a: pytest.fail("asked"),
                          sleep=clock.sleep, clock=clock) == {}
    assert not (tmp_path / "cg" / cat.JUDGE_DIR).exists()


# -- what the judge is asked and what the hook puts right ------------------------


def test_the_brief_is_asked_before_the_missing_list_and_both_before_the_result():
    text = cat.agent_judge_text(STANDARD)
    brief, missing, result, retry = (text.index(word) for word in ("\nbrief: ", "; missing: ", "\nresult: ", "\nretry: "))
    assert brief < missing < result < retry
    assert text == HOOK.build_agent_judge_prompt(CATALOGUE, STANDARD)
    assert list(cat.AGENT_JUDGE_KEYS) == ["agent_brief", "result", "retry"]
    assert cat.agent_metric_ids(STANDARD) == ("agent_brief", "result", "retry")


def test_the_result_wording_counts_findings_and_empty_lists_as_done_and_blocked_as_unable():
    sub_line = cat.METRICS_BY_ID["result"].sub_line
    assert sub_line == next(m["sub_line"] for m in CATALOGUE["metrics"] if m["id"] == "result")
    for phrase in ("findings", "refuted claims", "an empty list", "count as done", "could not do its own work"):
        assert phrase in sub_line


def test_done_is_taken_out_of_missing_when_the_agent_handed_its_answer_back_through_a_tool():
    tag = "brief=clear missing=done,files result=done"
    assert HOOK.agent_grounded(tag, {"answered": True}) == "brief=clear missing=files result=done"
    assert HOOK.agent_grounded("missing=done result=done", {"answered": True}) == "missing=none result=done"
    # An agent that reported in words keeps what Haiku said, and so does a run with no facts.
    assert HOOK.agent_grounded(tag, {"answered": False}) == tag
    assert HOOK.agent_grounded(tag, None) == tag
    assert HOOK.agent_grounded("brief=vague missing=files,goal", {"answered": True}) == "brief=vague missing=files,goal"


def test_the_worker_logs_a_workflow_agents_verdict_with_done_dropped_and_its_retry_word_left_out(tmp_path):
    agent = _workflow_agent(tmp_path, _handback("so1", "StructuredOutput", {"verdict": "sound"}))
    job = _agent_job(_workflow_session(tmp_path), agent, agent_id="w1")
    clock = _Clock()
    record = HOOK.run_judge(
        tmp_path / "cg", CATALOGUE, job, sleep=clock.sleep, clock=clock,
        ask=lambda *_: _answer("[cg: result=done retry=brief brief=clear missing=done,scope]"))
    assert record["agent"] == "result=done brief=clear missing=scope"
    [judged] = haiku_tags.load(tmp_path / "cg")
    assert judged.tag.missing == ("scope",) and not judged.retry


def test_the_job_asks_for_the_brief_before_the_result_and_the_tag_keeps_the_answers_order(tmp_path):
    session, agent = _agent_run(tmp_path)
    job = _agent_job(session, agent)
    assert job["keys"] == ["brief", "missing", "result", "retry"]
    clock = _Clock()
    asked = []

    def ask(prepared, *_args):
        asked.append(prepared)
        return _answer("[cg: result=partial brief=clear missing=none retry=none]")

    record = HOOK.run_judge(tmp_path / "cg", CATALOGUE, job, ask=ask, sleep=clock.sleep, clock=clock)
    assert record["agent"] == "result=partial brief=clear missing=none"
    assert asked[0]["excerpt"].startswith("Agent type: general-purpose")


# -- more than one stop, and a retry on a higher tier ----------------------------


def test_a_run_judged_again_after_a_stop_hook_asked_it_to_carry_on_has_its_newest_verdict(tmp_path):
    session, agent = _agent_run(tmp_path)
    # The stop that follows a stop hook's request to carry on is judged, not skipped.
    again = _agent_job(session, agent, stop_hook_active=True)
    assert again is not None and again["kind"] == "agent"
    _append(agent, turn_line(timestamp=_at(40), model="claude-sonnet-5", message_id="msg_a3", output_tokens=30,
                             content=[{"type": "text", "text": "Added the docstring to mod3.py as well."}]))
    config_dir = tmp_path / "cg"
    _tag_file(
        config_dir,
        {"ts": "2026-09-27T10:00:30Z", "reply": "msg_a2", "agent": "result=partial retry=brief brief=vague", "usd": 0.002},
        {"ts": "2026-09-27T10:00:50Z", "reply": "msg_a3", "agent": "result=done brief=clear", "usd": 0.001},
    )
    top = parse_transcript(session, TranscriptMeta(path=str(session)))
    sub = parse_transcript(agent, TranscriptMeta(path=str(agent), kind="subagent", agent_type="general-purpose"))
    corpus = NS(sessions=[NS(top=top, subs=[sub], session_id="s")])
    assert haiku_tags.apply(corpus, config_dir) == 1
    first, middle, last = sub.turns[0], sub.turns[-2], sub.turns[-1]
    assert last.message_id == "msg_a3" and last.result_marker == "done" and last.cap.brief == "clear"
    # The earlier stop's verdict is not applied beside it; its cost is added to the run's.
    assert middle.result_marker is None and (middle.cap is None or not middle.cap.judged)
    assert last.cap.judge_usd == pytest.approx(0.003)
    # The newest verdict is the whole verdict: a retry word only the earlier one gave is not the run's.
    assert first.retry_marker is None
    assert haiku_tags.summary(config_dir, kind="agent").calls == 2
    # The order the rows were written in makes no difference.
    reverse = tmp_path / "cg2"
    _tag_file(
        reverse,
        {"ts": "2026-09-27T10:00:50Z", "reply": "msg_a3", "agent": "result=done brief=clear", "usd": 0.001},
        {"ts": "2026-09-27T10:00:30Z", "reply": "msg_a2", "agent": "result=partial retry=brief brief=vague", "usd": 0.002},
    )
    sub2 = parse_transcript(agent, TranscriptMeta(path=str(agent), kind="subagent", agent_type="general-purpose"))
    assert haiku_tags.apply(NS(sessions=[NS(top=top, subs=[sub2], session_id="s")]), reverse) == 1
    assert sub2.turns[-1].result_marker == "done"


def _long_session(tmp_path, model_first: str, model_again: str, *, same_brief: bool = True) -> tuple[Path, Path]:
    """A session that ran an agent early, then more than the hook's tail
    of other work, then ran an agent again on ``model_again`` (the same
    brief when ``same_brief``); and the second run's own transcript."""
    first_brief = "Check every refund path in payments.py for a missing audit log entry and list them."
    second_brief = first_brief if same_brief else "Summarise the README"
    filler = [turn_line(timestamp=_at(100 + n), message_id=f"f{n}", content=[_said("w" * 1500)]) for n in range(400)]
    session = _transcript(tmp_path, [
        user_str_line("Audit the refunds", timestamp=_at(0)),
        turn_line(timestamp=_at(1), message_id="m1", content=[
            tool_use_block("Agent", "tA", {"subagent_type": "general-purpose", "prompt": first_brief})]),
        {"type": "user", "timestamp": _at(5), "toolUseResult": {
            "status": "completed", "agentId": "a1", "agentType": "general-purpose", "resolvedModel": model_first,
            "prompt": first_brief, "content": [{"type": "text", "text": "Only found one path, not sure about the rest."}]},
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tA", "content": "x"}]}},
        *filler,
        user_str_line("That missed some, run it again", timestamp=_at(1000)),
        turn_line(timestamp=_at(1001), message_id="m9", content=[
            tool_use_block("Agent", "tB", {"subagent_type": "general-purpose", "prompt": second_brief})]),
        {"type": "user", "timestamp": _at(1005), "toolUseResult": {
            "status": "async_launched", "agentId": "a2", "resolvedModel": model_again, "prompt": second_brief},
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tB", "content": "x"}]}},
    ], "long-sess.jsonl")
    assert session.stat().st_size > HOOK._COACH_TAIL_BYTES * 2
    agent = _transcript(tmp_path, [
        user_str_line(second_brief, timestamp=_at(1002)),
        turn_line(timestamp=_at(1003), model=model_again, message_id="msg_b1", content=[
            tool_use_block("Read", "r1", {"file_path": "/w/payments.py"})]),
        turn_line(timestamp=_at(1004), model=model_again, message_id="msg_b2", content=[_said("Found three paths.")]),
    ], "agent-a2.jsonl")
    return session, agent


def test_a_rerun_of_a_brief_on_a_higher_tier_is_a_retry_for_the_model_even_far_back_in_the_session(tmp_path):
    session, agent = _long_session(tmp_path, "claude-haiku-4-5", "claude-sonnet-5")
    # The first run lies before the end of the transcript the old read covered: the Agent call and its result are
    # still found, by the lines that hold them.
    prepared = _prepared(session, agent, agent_id="a2")
    assert prepared["facts"]["retry"] == "model" and prepared["facts"]["model"] == "claude-sonnet-5"
    assert '1. general-purpose on claude-haiku-4-5. Brief: "Check every refund path' in prepared["excerpt"]
    assert 'Report ended: "Only found one path, not sure about the rest."' in prepared["excerpt"]
    # Written by the hook whatever Haiku said, and Haiku's own retry word is replaced by it.
    job = _agent_job(session, agent, agent_id="a2")
    clock = _Clock()
    for said in ("[cg: result=done retry=none brief=clear]", "[cg: result=done retry=brief brief=clear]"):
        record = HOOK.run_judge(tmp_path / "cg", CATALOGUE, job, ask=lambda *_: _answer(said),
                                sleep=clock.sleep, clock=clock)
        assert record["agent"].split().count("retry=model") == 1 and "retry=brief" not in record["agent"]


@pytest.mark.parametrize("first, again, same_brief", [
    ("claude-sonnet-5", "claude-sonnet-5", True),   # the same tier again is not a step up
    ("claude-opus-4-7", "claude-sonnet-5", True),   # a lower tier is not either
    ("claude-haiku-4-5", "claude-sonnet-5", False),  # another brief is no rerun
    ("claude-haiku-4-5", "", True),                   # an unknown model is no step up
])
def test_a_rerun_that_is_not_a_step_up_in_tier_is_left_to_haikus_own_word(tmp_path, first, again, same_brief):
    session, agent = _long_session(tmp_path, first, again, same_brief=same_brief)
    prepared = _prepared(session, agent, agent_id="a2")
    facts = prepared["facts"]
    assert "retry" not in facts
    if again:
        assert facts["model"] == "claude-sonnet-5"


def test_workflow_siblings_are_not_retries_of_each_other(tmp_path):
    answer = _handback("so1", "StructuredOutput", {"verdict": "sound"})
    agent = _workflow_agent(tmp_path, answer)
    brief = "Review refunds in payments.py.\n  Return verdict and findings."
    # An earlier run of the same brief on a lower tier sits in the main transcript, as a sibling would.
    session = _transcript(tmp_path, [
        user_str_line("Run the review workflow", timestamp=_at(0)),
        turn_line(timestamp=_at(1), message_id="m1", content=[
            tool_use_block("Agent", "tA", {"subagent_type": "general-purpose", "prompt": brief})]),
        {"type": "user", "timestamp": _at(5), "toolUseResult": {
            "status": "completed", "agentId": "a1", "resolvedModel": "claude-haiku-4-5", "prompt": brief,
            "content": [{"type": "text", "text": "Found nothing."}]},
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tA", "content": "x"}]}},
    ], "sess2.jsonl")
    prepared = _prepared(session, agent, agent_id="w1", agent_type="workflow-subagent")
    assert "retry" not in prepared["facts"] and prepared["facts"]["workflow"]
    assert "Earlier agent runs in this session: none." in prepared["excerpt"]
    # Haiku's own retry word is dropped for a workflow agent too.
    assert HOOK.agent_grounded("result=done retry=brief", prepared["facts"]) == "result=done"


@pytest.mark.parametrize("config, extra, env", [
    ({"capture": {"level": "off"}}, {}, {}),
    ({"capture": {"level": "free"}}, {}, {}),
    ({"capture": {"level": "standard"}}, {"agent_type": "statusline-setup"}, {}),
    ({"capture": {"level": "standard"}}, {"hook_event_name": "Stop"}, {}),
    ({"capture": {"level": "standard"}}, {}, {cat.JUDGE_ENV: "1"}),
])
def test_no_agent_job_when_there_is_nothing_to_judge(tmp_path, monkeypatch, config, extra, env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    session, agent = _agent_run(tmp_path)
    assert HOOK.agent_judge_job(_subagent_stop(session, agent, **extra), config, CATALOGUE) is None


def test_the_worker_logs_an_agent_runs_words_under_their_own_key(tmp_path):
    session, agent = _agent_run(tmp_path)
    job = _agent_job(session, agent)
    clock = _Clock()

    def ask(*_args):
        return _answer("[cg: result=partial retry=brief fit=right brief=vague missing=files,constraints rules=used "
                       "task=docs]")

    # "none" only says the run was no retry: it isn't kept.
    none = HOOK.run_judge(tmp_path / "other", CATALOGUE, job, sleep=clock.sleep, clock=clock,
                          ask=lambda *_: _answer("[cg: result=done retry=none]"))
    assert none["agent"] == "result=done"

    record = HOOK.run_judge(tmp_path / "cg", CATALOGUE, job, ask=ask, sleep=clock.sleep, clock=clock)
    # Only the agent's own keys and words, in the order Haiku gave them: no fit (retired), no rules, no
    # main-session key, and the main session's list words it doesn't take.
    assert record["agent"] == "result=partial retry=brief brief=vague missing=files" and "tl" not in record
    loaded = haiku_tags.load(tmp_path / "cg")
    assert [(j.kind, j.result, j.retry, j.tag.fit, j.tag.brief, j.tag.missing) for j in loaded] == [
        ("agent", "partial", "brief", None, "vague", ("files",))
    ]


def test_an_agent_runs_words_land_on_the_run_and_never_over_its_own(tmp_path):
    session, agent = _agent_run(tmp_path)
    top = parse_transcript(session, TranscriptMeta(path=str(session)))
    sub = parse_transcript(agent, TranscriptMeta(path=str(agent), kind="subagent", agent_type="general-purpose"))
    # An agent that tagged its own report, as agents did before 0.11.0.
    old_note = "cg-cap v1 result\n" + cat.NOTE_INTRO + "\nEnd your final report with one line, [result: done|partial|blocked]."
    note = attachment_line(
        "hook_additional_context", content=[old_note], hookName="SubagentStart", hookEvent="SubagentStart",
        rendered=f"<system-reminder>\nSubagentStart hook additional context: {old_note}\n</system-reminder>",
    )
    note["timestamp"] = _at(30)
    own_path = _transcript(tmp_path, [
        note,
        user_str_line("Check it", timestamp=_at(30)),
        turn_line(timestamp=_at(31), message_id="msg_o1", content=[{"type": "text", "text": "Checked.\n[result: done]"}]),
    ], "agent-old.jsonl")
    own = parse_transcript(own_path, TranscriptMeta(path=str(own_path), kind="subagent"))
    config_dir = tmp_path / "cg"
    _tag_file(
        config_dir,
        {"ts": "2026-09-27T10:00:30Z", "reply": "msg_a2", "agent": "result=partial retry=brief fit=right brief=vague "
         "missing=files", "usd": 0.0021},
        {"ts": "2026-09-27T10:00:40Z", "reply": "msg_o1", "agent": "result=blocked"},
    )
    corpus = NS(sessions=[NS(top=top, subs=[sub, own], session_id="s")])
    assert haiku_tags.apply(corpus, config_dir) == 1
    last, first = sub.turns[-1], sub.turns[0]
    assert last.result_marker == "partial" and last.cap.judged and last.cap.judge_usd == 0.0021
    assert (last.cap.fit, last.cap.brief, last.cap.missing) == ("right", "vague", ("files",))
    assert first.retry_marker == "brief"
    assert own.turns[-1].result_marker == "done" and not (own.turns[-1].cap and own.turns[-1].cap.judged)
    # Capture counts the run, prices Haiku's call and nothing Claude wrote.
    use = capture.usage(corpus, load_pricing())
    assert use.subagents == 2 and use.reports == 2 and use.tagged_reports == 2
    assert use.scopes["haiku"].tag_cost == pytest.approx(0.0021)
    # fit is read from the old line (above) but no longer a metric to count.
    assert use.answers["result"] == 2 and use.answers["retry"] == 1 and "fit" not in use.answers
    assert "brief" not in use.scopes  # the retry word was never written into a brief


def test_estimates_add_a_haiku_call_per_agent_run():
    past = capture.History(days=7, sessions=2, cycles=40, subagents=10, main_notes=2, main_note=1e-6, reply_tag=1e-6)
    main_only = capture.estimate(past, ("task",))
    with_agents = capture.estimate(past, ("task", "result"))
    assert with_agents.cost == pytest.approx(main_only.cost + 10 * cat.JUDGE_USD_PER_CALL)
    # Nothing asked of the agent: no note and no report tag.
    assert with_agents.note_tokens == main_only.note_tokens and with_agents.tag_tokens == main_only.tag_tokens


@pytest.mark.skipif(os.name == "nt", reason="a fake claude command is a shell script")
def test_the_subagent_stop_hook_hands_the_run_to_a_worker(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text(
        "#!/bin/sh\ncat > /dev/null\n"
        "echo '{\"result\": \"[cg: result=done brief=clear missing=none]\", \"total_cost_usd\": 0.002}'\n",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    config_dir = tmp_path / "cg"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text('[capture]\nlevel = "standard"\n', encoding="utf-8")
    session, agent = _agent_run(tmp_path)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    env.pop(cat.JUDGE_ENV, None)
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "--config-dir", str(config_dir)],
        input=json.dumps(_subagent_stop(session, agent)).encode("utf-8"), capture_output=True, timeout=30, env=env,
    )
    # Nothing printed: the agent's report reaches the session as it was.
    assert done.returncode == 0 and done.stdout == b"" and done.stderr == b""
    tags = config_dir / cat.JUDGE_DIR
    # The worker waits for the agent's file to go quiet before it reads it.
    for _ in range(200):
        if tags.is_dir() and any(tags.iterdir()):
            break
        time.sleep(0.1)
    line = json.loads(next(tags.iterdir()).read_text(encoding="utf-8"))
    assert line["reply"] == "msg_a2" and line["agent"] == "brief=clear missing=none result=done"


# -- the excerpt -----------------------------------------------------------------


def test_the_excerpt_says_what_happened_in_the_turn(tmp_path):
    job = HOOK.judge_job(_stop(_turn(tmp_path), cwd="/w"), HAIKU, CATALOGUE)
    excerpt = job["excerpt"]
    assert 'The user\'s message before this one: "Add a calculator module"' in excerpt
    assert 'The end of Claude\'s reply to it: "Added."' in excerpt
    assert 'The user\'s message: "add() returns the wrong sum, fix it"' in excerpt
    assert "Messages the user sent before it in this session: 1." in excerpt
    # Changed files are named from the project folder.
    assert "2 model calls; 340 output tokens; tools: Bash 1, Edit 1; files changed: 1 (calc.py); tool errors: 1." in excerpt
    # Only a command's first line; none of its output.
    assert "Shell commands: `pytest -q tests/test_calc.py`." in excerpt and "echo done" not in excerpt
    assert "1 failed" not in excerpt
    assert "Skills run: none." in excerpt and "Tests run: chosen tests." in excerpt
    assert "Plan mode: not used in this session." in excerpt
    assert excerpt.endswith('The end of Claude\'s final reply: "Fixed add(); the tests pass now."')
    # The tag belongs to Claude's last reply; the keys are the level's.
    assert job["reply"] == json.loads((tmp_path / "s.jsonl").read_text().splitlines()[-1])["message"]["id"]
    assert job["keys"] == list(cat.tag_keys(STANDARD)) and job["system"] == cat.judge_text(STANDARD)
    assert job["keys"][3:6] == ["shift", "why", "admit"]
    assert job["facts"] == {"plan_now": False, "plan_before": False, "skills": 0, "files": 1, "commands": 1,
                            "earlier": 1, "tests": "targeted", "docs_only": False, "agent_files": 0,
                            "shell_changes": 0, "correction": False, "adjust": False, "same_files": False,
                            "tool_errors": 1, "admit_candidate": False, "judged": True}


def test_the_excerpt_cuts_long_messages_and_notes_plans_and_skills(tmp_path):
    long = "word " * 2_000
    path = _transcript(tmp_path, [
        user_str_line(long, timestamp=_at(0)),
        turn_line(timestamp=_at(1), content=[tool_use_block("ExitPlanMode", "p", {"plan": "x"}),
                                             tool_use_block("Skill", "k", {"skill": "cg-brief"}),
                                             {"type": "text", "text": "Plan ready."}]),
    ])
    job = HOOK.judge_job(_stop(path, last_assistant_message=""), HAIKU, CATALOGUE)
    message = next(line for line in job["excerpt"].splitlines() if line.startswith("The user's message:"))
    assert len(message) < cat.JUDGE_LIMITS["prompt"] + 60 and "[...]" in message
    assert "Skills run: cg-brief." in job["excerpt"]
    assert "Plan mode: Claude wrote a plan in this turn." in job["excerpt"]
    assert job["excerpt"].endswith('"Plan ready."')
    assert job["facts"]["plan_now"] and job["facts"]["skills"] == 1 and job["facts"]["earlier"] == 0
    later = _turn(tmp_path, earlier_plan=True)
    assert "Plan mode: the user approved a plan earlier" in HOOK.judge_job(_stop(later), HAIKU, CATALOGUE)["excerpt"]


def _earlier_plan_text(path) -> str:
    return next(
        line for line in HOOK.judge_job(_stop(path), HAIKU, CATALOGUE)["excerpt"].splitlines()
        if line.startswith("Plan mode:")
    )


def test_a_plan_you_sent_back_is_not_a_plan_approved_earlier(tmp_path):
    sent_back = _plan_result(2, is_error=True, text="The user doesn't want to proceed with this tool use.")
    path = _turn(tmp_path, earlier_plan=True, plan_answer=sent_back)
    # It was proposed, so "not used" would be wrong.
    assert _earlier_plan_text(path) == "Plan mode: a plan was proposed 1 time in this session and not approved."
    assert HOOK.judge_job(_stop(path), HAIKU, CATALOGUE)["facts"]["plan_before"] is False


def test_a_go_ahead_you_typed_approves_a_plan_the_dialog_sent_back(tmp_path):
    sent_back = _plan_result(2, is_error=True, text="The user doesn't want to proceed with this tool use.")
    path = _turn(tmp_path, earlier_plan=True, plan_answer=sent_back, between=[
        user_str_line("go ahead", timestamp=_at(5), origin={"kind": "human"})])
    assert _earlier_plan_text(path) == "Plan mode: the user approved a plan earlier in this session."


def test_leaving_plan_mode_approves_a_plan_the_dialog_sent_back(tmp_path):
    sent_back = _plan_result(2, is_error=True, text="The user doesn't want to proceed with this tool use.",
                             permissionMode="plan")
    path = _turn(tmp_path, earlier_plan=True, plan_answer=sent_back, between=[
        user_str_line("Looks fine, build it with the second option", timestamp=_at(5), permissionMode="acceptEdits")])
    assert _earlier_plan_text(path) == "Plan mode: the user approved a plan earlier in this session."


def test_a_go_ahead_after_the_next_plan_call_does_not_approve_the_earlier_plan(tmp_path):
    sent_back = _plan_result(2, is_error=True, text="The user doesn't want to proceed with this tool use.")
    replanned = turn_line(timestamp=_at(3), content=[tool_use_block("ExitPlanMode", "tq", {"plan": "1. again"})])
    path = _turn(tmp_path, earlier_plan=True, plan_answer=sent_back, between=[
        replanned,
        {"type": "user", "timestamp": _at(4), "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tq", "content": "no", "is_error": True}]}},
    ])
    assert _earlier_plan_text(path) == "Plan mode: a plan was proposed 2 times in this session and not approved."


def test_plan_modes_plan_file_counts_as_a_plan_and_isnt_a_changed_file(tmp_path):
    # Headless plan mode has no ExitPlanMode: the plan goes to a file.
    path = _transcript(tmp_path, [
        user_str_line("Plan the JSON export", timestamp=_at(0), permissionMode="plan"),
        turn_line(timestamp=_at(1), content=[
            tool_use_block("Write", "w", {"file_path": "/home/me/.claude/plans/json-export.md", "content": "1."}),
            {"type": "text", "text": "The plan is in the plan file."}]),
    ])
    job = HOOK.judge_job(_stop(path, permission_mode="plan"), HAIKU, CATALOGUE)
    assert job["facts"]["plan_now"] and job["facts"]["files"] == 0
    assert "Plan mode: Claude wrote a plan in this turn." in job["excerpt"] and "files changed: 0;" in job["excerpt"]


def test_a_change_to_documentation_only_is_docs_work(tmp_path):
    path = _transcript(tmp_path, [
        user_str_line("Fix the typo in README.md", timestamp=_at(0)),
        turn_line(timestamp=_at(1), content=[tool_use_block("Edit", "e", {"file_path": "/w/README.md"}),
                                             {"type": "text", "text": "Fixed."}]),
    ])
    job = HOOK.judge_job(_stop(path, cwd="/w"), HAIKU, CATALOGUE)
    assert job["facts"]["docs_only"] and "files changed: 1 (README.md)" in job["excerpt"]
    assert HOOK.grounded("task=bugfix check=run", job["facts"]) == "task=docs check=run"


@pytest.mark.parametrize(
    "command, scope",
    [
        ("python -m pytest -q", "full"),
        ("pytest tests/test_store.py -q", "targeted"),
        ("python -m pytest -q -k remove", "targeted"),
        ("cd /w && python -m pytest -q 2>&1 | tail -5", "full"),
        ("FOO=1 timeout 60 uv run pytest -x", "full"),
        ("npm test", "full"),
        ("npm run test -- --testPathPattern=cart", "targeted"),
        ("npx jest src/cart.spec.js", "targeted"),
        ("go test ./...", "full"),
        ("go test ./pkg/cart", "targeted"),
        ("cargo test parse_", "targeted"),
        ("cargo test --release", "full"),
        ("python3 -m unittest tests.test_store", "targeted"),
        ("make test", "full"),
        # Not a test run: the runner's name elsewhere in a command.
        ("grep -r pytest .", ""),
        ("echo pytest", ""),
        ("git commit -m 'run pytest'", ""),
        ("python -m inventory.cli list stock.csv", ""),
    ],
)
def test_which_commands_run_tests(command, scope):
    assert HOOK._test_scope(command) == scope


@pytest.mark.parametrize(
    "config, extra, env",
    [
        ({"capture": {"level": "standard", "tagger": "claude"}}, {"last_assistant_message": "Done. [cg: task=bugfix]"}, False),
        ({"capture": {"level": "off", "tagger": "haiku"}}, {}, False),
        ({"capture": {"level": "free", "tagger": "haiku"}}, {}, False),  # no key to ask for
        (HAIKU, {"stop_hook_active": True}, False),
        (HAIKU, {"agent_id": "a1"}, False),
        (HAIKU, {}, True),  # the call Haiku itself runs in
    ],
)
def test_no_job_when_haiku_has_nothing_to_do(tmp_path, monkeypatch, config, extra, env):
    if env:
        monkeypatch.setenv(cat.JUDGE_ENV, "1")
    assert HOOK.judge_job(_stop(_turn(tmp_path), **extra), config, CATALOGUE) is None


def test_a_turn_waits_for_its_background_agents(tmp_path):
    # Their reports come back as the next message, and the turn that
    # answers them is judged with the whole piece of work: one call, not
    # one per turn in between.
    path = _turn(tmp_path)
    agent = {"id": "a1", "type": "subagent", "status": "running", "description": "Find the files"}
    assert HOOK.judge_job(_stop(path, background_tasks=[agent]), HAIKU, CATALOGUE) is None
    assert HOOK.judge_job(_stop(path, background_tasks=[{**agent, "status": "completed"}]), HAIKU, CATALOGUE)
    # A background shell, such as a dev server, never holds it up.
    server = {"id": "b1", "type": "shell", "status": "running", "description": "npm run dev"}
    assert HOOK.judge_job(_stop(path, background_tasks=[server]), HAIKU, CATALOGUE)


def test_no_job_without_a_message_of_yours(tmp_path):
    path = _transcript(tmp_path, [turn_line(timestamp=_at(1), content=[{"type": "text", "text": "Hi."}])])
    assert HOOK.judge_job(_stop(path), HAIKU, CATALOGUE) is None


# -- Haiku's answer --------------------------------------------------------------


def test_only_known_keys_and_words_are_kept():
    keys = cat.tagged_keys(STANDARD)
    judge = CATALOGUE["judge"]
    text = (
        "Sure! [cg: task=bugfix brief=CLEAR level=impossible missing=files,goal,nonsense skill=would-help:secret "
        "check=full note=hello task=docs]"
    )
    assert HOOK.judge_tag(text, keys, judge) == "task=bugfix brief=clear missing=files,goal skill=would-help"
    assert HOOK.judge_tag("no tag here", keys, judge) == ""
    assert HOOK.judge_tag(None, keys, judge) == ""


def test_why_and_admit_are_keys_haiku_may_answer_and_the_hook_lists_the_same_as_the_catalogue():
    for level in cat.LEVELS:
        ids = cat.level_includes(level)
        assert HOOK.tag_keys(CATALOGUE, ids) == list(cat.tag_keys(ids)), level
    keys = cat.tag_keys(STANDARD)
    judge = CATALOGUE["judge"]
    text = "[cg: task=bugfix shift=redo why=left_out admit=claim size=s found=yes fit=right why=nonsense]"
    # An unknown word is dropped and a retired key is never kept.
    assert HOOK.judge_tag(text, keys, judge) == "task=bugfix shift=redo why=left_out admit=claim size=s"
    # Without shift switched on, why and admit are not asked for, so not kept.
    only = cat.tag_keys(("task", "size"))
    assert HOOK.judge_tag(text, only, judge) == "task=bugfix size=s"


def test_the_transcript_holds_why_and_admit_to_what_it_can_show():
    later = {"plan_now": False, "plan_before": False, "skills": 0, "files": 1, "commands": 1, "earlier": 2,
             "tests": "full", "docs_only": False, "tool_errors": 1, "admit_candidate": True, "judged": True}
    # A redo with a reason, and a candidate in the reply: kept as written.
    assert HOOK.grounded("shift=redo why=left_out admit=claim", later) == "shift=redo why=left_out admit=claim"
    assert HOOK.grounded("shift=fix why=tools", later) == "shift=fix why=tools"
    # Why needs a redo or a fix, and tools need an error.
    assert HOOK.grounded("shift=build why=left_out", later) == "shift=build"
    assert HOOK.grounded("why=missed", later) == ""
    assert HOOK.grounded("shift=fix why=tools", {**later, "tool_errors": 0}) == "shift=fix"
    # Haiku's admit needs the reply to say it got something wrong.
    assert HOOK.grounded("shift=fix admit=claim", {**later, "admit_candidate": False}) == "shift=fix"
    # A correction turns building on the last work into fixing it, and
    # so lets a why stand; so does an adjustment to the same files.
    assert HOOK.grounded("shift=build why=missed", {**later, "correction": True}) == "shift=fix why=missed"
    assert HOOK.grounded("shift=grew", {**later, "adjust": True, "same_files": True}) == "shift=fix"
    assert HOOK.grounded("shift=grew", {**later, "adjust": True}) == "shift=grew"
    assert HOOK.grounded("shift=new", {**later, "correction": True}) == "shift=new"
    # A first message has neither.
    assert HOOK.grounded("shift=fix why=missed", {**later, "earlier": 0}) == ""


def _facts_of(tmp_path, lines, **stop):
    path = _transcript(tmp_path, lines)
    job = HOOK.judge_job(_stop(path, **stop), HAIKU, CATALOGUE)
    return job["facts"], job["excerpt"]


def test_what_the_reply_changed_is_every_edit_of_yours_and_every_command_that_moves_files(tmp_path):
    facts, _ = _facts_of(tmp_path, [
        user_str_line("Move the notes", timestamp=_at(0)),
        turn_line(timestamp=_at(1), content=[
            tool_use_block("Edit", "m", {"file_path": "/home/me/.claude/projects/x/memory/notes.md"}),
            tool_use_block("Bash", "c", {"command": "git commit -m 'merge the notes' && git push 2>&1 > /dev/null"}),
            {"type": "text", "text": "Done."}]),
    ])
    # A commit, a push and a redirection to nowhere change nothing, and the memory file isn't your work.
    assert (facts["files"], facts["shell_changes"], facts["agent_files"]) == (0, 0, 0)
    assert HOOK.grounded("check=manual", facts) == "check=none"
    for command in ("git merge main", "git -C /w rebase main", "mv a.py b.py", "rm -rf build", "echo hi > out.txt",
                    "Remove-Item old.py"):
        facts, _ = _facts_of(tmp_path, [
            user_str_line("Tidy up", timestamp=_at(0)),
            turn_line(timestamp=_at(1), content=[tool_use_block("Bash", "c", {"command": command}),
                                                 {"type": "text", "text": "Done."}]),
        ])
        assert facts["shell_changes"] == 1, command
        assert HOOK.grounded("check=manual", facts) == "check=manual", command


def test_a_subagents_edits_count_as_changes(tmp_path):
    facts, _ = _facts_of(tmp_path, [
        user_str_line("Have an agent fix it", timestamp=_at(0)),
        turn_line(timestamp=_at(1), content=[tool_use_block("Agent", "a", {"prompt": "fix it"}),
                                             {"type": "text", "text": "Working."}]),
        {"type": "user", "timestamp": _at(2), "toolUseResult": {"toolStats": {"editFileCount": 2}},
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": "ok"}]}},
        turn_line(timestamp=_at(3), content=[{"type": "text", "text": "Fixed."}]),
    ])
    assert (facts["files"], facts["agent_files"]) == (0, 2)
    assert HOOK.grounded("task=bugfix check=manual", facts) == "task=bugfix check=manual"
    # Documentation edits don't make the work docs once an agent changed other files.
    assert HOOK.grounded("task=bugfix", {**facts, "files": 1, "docs_only": True}) == "task=bugfix"
    assert HOOK.grounded("task=bugfix", {**facts, "agent_files": 0, "files": 1, "docs_only": True}) == "task=docs"


def test_a_whole_suite_outranks_chosen_tests_in_one_turn(tmp_path):
    facts, excerpt = _facts_of(tmp_path, [
        user_str_line("Run the tests", timestamp=_at(0)),
        turn_line(timestamp=_at(1), content=[
            tool_use_block("Bash", "a", {"command": "pytest tests/test_one.py"}),
            tool_use_block("Bash", "b", {"command": "python -m pytest -q"}),
            tool_use_block("Bash", "c", {"command": "pytest tests/test_two.py"}),
            {"type": "text", "text": "Both pass."}]),
    ])
    assert facts["tests"] == "full" and "Tests run: the whole test suite." in excerpt
    # Tests that ran with no edit are still a check.
    assert HOOK.grounded("check=none", facts) == "check=full"


def test_the_message_and_the_reply_before_it_tell_a_correction_an_adjustment_and_an_admission(tmp_path):
    lines = [
        user_str_line("Add a button", timestamp=_at(0)),
        turn_line(timestamp=_at(1), content=[tool_use_block("Edit", "e1", {"file_path": "/w/ui/button.py"}),
                                             {"type": "text", "text": "Added."}]),
        user_str_line("That's wrong, it's still broken", timestamp=_at(10)),
        turn_line(timestamp=_at(11), content=[
            tool_use_block("Edit", "e2", {"file_path": "/w/ui/button.py"}),
            {"type": "text", "text": "You're right, I got it wrong. I changed the handler."}]),
    ]
    facts, _ = _facts_of(tmp_path, lines, cwd="/w")
    assert facts["correction"] and facts["admit_candidate"] and facts["same_files"] and not facts["adjust"]
    lines[2] = user_str_line("Make it a bit smaller", timestamp=_at(10))
    lines[3] = turn_line(timestamp=_at(11), content=[tool_use_block("Edit", "e2", {"file_path": "/w/ui/button.py"}),
                                                     {"type": "text", "text": "Made it smaller."}])
    facts, _ = _facts_of(tmp_path, lines, cwd="/w")
    assert facts["adjust"] and facts["same_files"] and not facts["correction"] and not facts["admit_candidate"]
    assert HOOK.grounded("shift=build", facts) == "shift=fix"
    # Another file: building on the last work, not fixing it.
    lines[3] = turn_line(timestamp=_at(11), content=[tool_use_block("Edit", "e2", {"file_path": "/w/ui/panel.py"}),
                                                     {"type": "text", "text": "Done."}])
    facts, _ = _facts_of(tmp_path, lines, cwd="/w")
    assert facts["adjust"] and not facts["same_files"]
    assert HOOK.grounded("shift=build", facts) == "shift=build"
    # A mistake owned inside code or a quote block doesn't count.
    lines[3] = turn_line(timestamp=_at(11), content=[{"type": "text", "text": "Use `my mistake` here.\n```\nI was wrong\n```"}])
    facts, _ = _facts_of(tmp_path, lines, cwd="/w")
    assert not facts["admit_candidate"]


def test_a_window_that_starts_inside_the_session_never_reads_as_its_first_message(tmp_path, monkeypatch):
    # The window holds one message of yours, but the file is longer than it.
    monkeypatch.setattr(HOOK, "_JUDGE_TAIL_BYTES", 600)
    lines = [user_str_line("Old message " + "x" * 400, timestamp=_at(0)),
             turn_line(timestamp=_at(1), content=[{"type": "text", "text": "Old reply."}]),
             user_str_line("Fix it", timestamp=_at(10)),
             turn_line(timestamp=_at(11), content=[{"type": "text", "text": "Fixed."}])]
    facts, excerpt = _facts_of(tmp_path, lines)
    assert facts["earlier"] == 1 and "Messages the user sent before it in this session: 0 or more." in excerpt
    assert HOOK.grounded("shift=fix prior=needed", facts) == "shift=fix prior=needed"


def test_hooks_reading_of_shell_commands_and_admissions_matches_the_parsers(tmp_path):
    from claudeglass import prompt_shape, shell_writes
    from claudeglass import testrun

    commands = [
        "git merge main", "git commit -m 'merge it' && git push", "git -C ../x rebase main", "git stash pop",
        "git status && git log --oneline", "mv a b", "cp -r src dst", "rm -rf build", "mkdir -p out",
        "pytest -q 2>&1 | tail -3", "npm test > /dev/null", "echo hi > out.txt", "cat <<'E'\nrm -rf x\nE",
        "sed -i 's/a/b/' f.py", "FOO=1 timeout 60 git pull", "ls -la", "python -m pytest tests/test_a.py",
        "git commit --amend --no-edit", "cd x && git reset --hard HEAD~1", "touch new.txt",
    ]
    for command in commands:
        assert HOOK._shell_changes(command) == (
            shell_writes.changes_files(command, powershell=False)
            or bool(shell_writes.write_targets(command, powershell=False, cwd="/w"))
        ), command
        assert HOOK._command_parts(command) == testrun.command_parts(command), command
    for text in ("I was wrong about that.", "You're right, my mistake.", "Here is `I was wrong` in code.",
                 "I should have checked first.", "All done.", "```\nI was wrong\n```\nDone.", "Good catch’s fine."):
        assert HOOK._admits_mistake(text) == prompt_shape.admits_mistake(text), text
    assert HOOK._DOC_SUFFIXES == cat.DOC_SUFFIXES


def test_the_stored_words_regex_takes_why_and_admit_and_old_agent_lines_keep_fit():
    assert haiku_tags._WORDS_RE.fullmatch("task=bugfix shift=redo why=left_out admit=instruction")
    assert not haiku_tags._WORDS_RE.fullmatch("task=bugfix why=left out")
    # Rows written while Haiku was asked for fit are still read.
    tag, result, retry = haiku_tags._agent_tag("result=partial retry=brief fit=right brief=vague missing=files")
    assert (result, retry) == ("partial", "brief")
    assert tag is not None and (tag.fit, tag.brief, tag.missing) == ("right", "vague", ("files",))


def test_what_the_transcript_settles_beats_haikus_guess():
    first = {"plan_now": False, "plan_before": False, "skills": 0, "files": 1, "commands": 1, "earlier": 0,
             "tests": "full", "docs_only": False}
    assert HOOK.grounded("task=bugfix plan=following skill=helped check=run shift=fix prior=needed", first) == (
        "task=bugfix plan=following skill=none check=full prior=none"
    )
    assert HOOK.grounded("shift=new plan=none", first) == "shift=new plan=none"
    assert HOOK.grounded("plan=none", {**first, "plan_now": True}) == "plan=made"
    after = {**first, "plan_before": True, "skills": 1, "earlier": 2, "tests": ""}
    assert HOOK.grounded("plan=made skill=helped check=run shift=build", after) == (
        "plan=following skill=helped check=run shift=build"
    )
    assert HOOK.grounded("plan=deviated", after) == "plan=deviated" and HOOK.grounded("plan=none", after) == "plan=none"
    # Nothing changed: nothing to check.
    assert HOOK.grounded("check=manual", {**after, "files": 0}) == "check=none"


def test_the_worker_logs_the_words_and_the_cost_never_the_excerpt(tmp_path):
    job = HOOK.judge_job(_stop(_turn(tmp_path)), HAIKU, CATALOGUE)
    asked = []

    def ask(job_, judge, cwd):
        asked.append((job_["excerpt"], judge["model"], cwd))
        return _answer("[cg: task=bugfix brief=clear plan=made skill=helped]")

    record = HOOK.run_judge(tmp_path / "cg", CATALOGUE, job, ask=ask)
    assert asked[0][1] == "haiku" and asked[0][2] == tmp_path / "cg"
    # What grounding put right is noted as closed words, never the excerpt.
    assert record == {
        "ts": job["ts"], "reply": job["reply"], "tl": "task=bugfix brief=clear plan=made skill=none",
        "g": "skill:helped>none", "usd": 0.0014, "in": 1200, "out": 30, "model": "claude-haiku-4-5-20251001",
    }
    text = (tmp_path / "cg" / cat.JUDGE_DIR / f"{job['ts'][:7]}.jsonl").read_text(encoding="utf-8")
    assert json.loads(text) == record
    assert "wrong sum" not in text and "pytest" not in text


def test_the_row_notes_each_word_grounding_changed_or_dropped_and_nothing_else(tmp_path):
    job = HOOK.judge_job(_stop(_turn(tmp_path)), HAIKU, CATALOGUE)
    words = "[cg: task=bugfix shift=fix why=missed admit=claim skill=helped level=hard]"
    record = HOOK.run_judge(tmp_path / "cg", CATALOGUE, job, ask=lambda *_: _answer(words))
    # No admission in the reply and no skill run: those words go, the rest stays.
    assert record["tl"] == "task=bugfix shift=fix why=missed skill=none level=hard"
    assert record["g"] == "admit:claim> skill:helped>none"
    assert haiku_tags._GROUNDED_RE.fullmatch(record["g"])
    # No change, no field; and no tag, no field.
    quiet = HOOK.run_judge(tmp_path / "cg", CATALOGUE, job, ask=lambda *_: _answer("[cg: task=bugfix]"))
    assert "g" not in quiet
    none = HOOK.run_judge(tmp_path / "cg", CATALOGUE, job, ask=lambda *_: _answer("[cg: admit=claim]"))
    assert none["err"] == "no_tag" and "g" not in none and "tl" not in none


def test_the_twin_grounding_rules_note_the_same_changes():
    from claudeglass import capture_tags

    before, after = "task=bugfix shift=build why=missed check=none", "task=bugfix shift=fix why=missed check=full"
    assert HOOK.grounding_changes(before, after) == list(capture_tags.grounding_changes(before, after))
    assert HOOK.grounding_changes(before, after) == ["shift:build>fix", "check:none>full"]
    assert HOOK.grounding_changes("why=missed admit=claim", "") == ["why:missed>", "admit:claim>"]
    # Only the words grounding can change are ever noted.
    assert HOOK.grounding_changes("level=easy size=s", "level=hard") == []
    assert capture_tags.grounding_changes("level=easy size=s", "level=hard") == ()


@pytest.mark.parametrize(
    "error, word",
    [
        (FileNotFoundError("claude"), "no_cli"),
        (HOOK.NoLogin(), "no_login"),
        (subprocess.TimeoutExpired("claude", 60), "timeout"),
        (ValueError("failed"), "failed"),
        (None, "no_tag"),
    ],
)
def test_a_turn_without_a_tag_says_why(tmp_path, error, word):
    job = HOOK.judge_job(_stop(_turn(tmp_path)), HAIKU, CATALOGUE)

    def ask(*_args):
        if error is not None:
            raise error
        return _answer("I can't tell.")

    record = HOOK.run_judge(tmp_path, CATALOGUE, job, ask=ask)
    assert record["err"] == word and "tl" not in record


def test_an_agent_run_that_got_no_words_is_still_an_agent_run(tmp_path):
    """It was logged with no ``agent`` key and read back as a main
    session's turn, so ``capture status`` counted it among the turns."""
    job = {"kind": "agent", "ts": "2026-09-27T10:00:00Z", "reply": "msg_1", "keys": ["result"], "excerpt": "x"}

    def ask(*_args):
        raise HOOK.NoLogin()

    record = HOOK.run_judge(tmp_path, CATALOGUE, job, ask=ask)
    assert record == {"ts": "2026-09-27T10:00:00Z", "reply": "msg_1", "agent": "", "err": "no_login"}
    [judged] = haiku_tags.load(tmp_path)
    assert (judged.kind, judged.tag, judged.error) == ("agent", None, "no_login")


@pytest.mark.parametrize(
    "result, raised",
    [
        # What claude -p answers with no working login (2.1.280 on Windows,
        # and the older wording), and an error that is something else.
        ("Failed to authenticate: OAuth session expired and could not be refreshed", "NoLogin"),
        ("Not logged in · Please run /login", "NoLogin"),
        ("API Error: 529 Overloaded", "ValueError"),
    ],
)
def test_a_claude_command_with_no_login_is_told_apart(tmp_path, monkeypatch, result, raised):
    """The desktop app keeps its own login, so the ``claude`` command the
    hook finds can have none; that is worth saying, not "the call failed"."""
    answer = json.dumps({"type": "result", "is_error": True, "result": result, "total_cost_usd": 0})
    monkeypatch.setattr(HOOK, "_claude_command", lambda: "claude")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: NS(returncode=1, stdout=answer.encode("utf-8")))
    job = HOOK.judge_job(_stop(_turn(tmp_path)), HAIKU, CATALOGUE)
    with pytest.raises(HOOK.NoLogin if raised == "NoLogin" else ValueError):
        HOOK.ask_haiku(job, CATALOGUE["judge"], tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="a fake claude command is a shell script")
def test_the_stop_hook_hands_the_turn_to_a_worker_that_asks_claude(tmp_path):
    """The whole path, as Claude Code runs it: the hook returns at once,
    and its worker runs ``claude -p`` (a fake one here) with the excerpt
    on stdin and writes the tag."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    seen = tmp_path / "seen"
    fake = bin_dir / "claude"
    fake.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" > "{seen}.args"\n'
        f'for a in "$@"; do [ -n "$want" ] && cp "$a" "{seen}.prompt"; want=""; '
        '[ "$a" = "--system-prompt-file" ] && want=1; done\n'
        f'cat > "{seen}.stdin"\n'
        f'env | grep -c "^{cat.JUDGE_ENV}=1" > "{seen}.env"\n'
        "echo '{\"result\": \"[cg: task=bugfix brief=clear]\", \"total_cost_usd\": 0.0013, "
        "\"usage\": {\"input_tokens\": 900, \"output_tokens\": 12}}'\n",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    config_dir = tmp_path / "cg"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text('[capture]\nlevel = "essentials"\ntagger = "haiku"\n', encoding="utf-8")
    payload = _stop(_turn(tmp_path))
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    env.pop(cat.JUDGE_ENV, None)
    started = time.monotonic()
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "--config-dir", str(config_dir)],
        input=json.dumps(payload).encode("utf-8"), capture_output=True, timeout=30, env=env,
    )
    assert done.returncode == 0 and done.stdout == b"" and done.stderr == b""
    assert time.monotonic() - started < 10
    tags = config_dir / cat.JUDGE_DIR
    for _ in range(100):
        if tags.is_dir() and any(tags.iterdir()):
            break
        time.sleep(0.1)
    line = json.loads(next(tags.iterdir()).read_text(encoding="utf-8"))
    assert line["tl"] == "task=bugfix brief=clear" and line["usd"] == 0.0013
    args = Path(f"{seen}.args").read_text(encoding="utf-8")
    assert "-p --model haiku --tools  --setting-sources  --strict-mcp-config --no-session-persistence" in args
    # The excerpt went in on stdin, never on the command line.
    assert "wrong sum" in Path(f"{seen}.stdin").read_text(encoding="utf-8") and "wrong sum" not in args
    # Nor the instructions, whose | and line breaks cmd.exe would take for
    # its own where claude is a .cmd: they're a file, gone once it's read.
    assert "--system-prompt-file " in args and cat.JUDGE_INTRO not in args
    assert Path(f"{seen}.prompt").read_text(encoding="utf-8") == cat.judge_text(cat.level_metrics("essentials"))
    assert not list((tmp_path / "cg").glob(".judge-*"))
    assert Path(f"{seen}.env").read_text(encoding="utf-8").strip() == "1"


# -- the tag file, read back ----------------------------------------------------


def _tag_file(config_dir: Path, *records) -> None:
    folder = config_dir / cat.JUDGE_DIR
    folder.mkdir(parents=True, exist_ok=True)
    lines = [r if isinstance(r, str) else json.dumps(r) for r in records]
    (folder / "2026-09.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_the_loader_checks_every_line_again(tmp_path):
    _tag_file(
        tmp_path,
        {"ts": "2026-09-27T10:00:00Z", "reply": "msg_1", "tl": "task=bugfix brief=clear", "usd": 0.001, "in": 900,
         "out": 20, "model": "claude-haiku-4-5-20251001"},
        {"ts": "2026-09-27T10:01:00Z", "reply": "msg_2", "tl": "task=poetry mood=happy"},
        {"ts": "2026-09-27T10:02:00Z", "reply": "msg_3", "err": "timeout"},
        {"ts": "2026-09-27T10:03:00Z", "reply": "../../etc", "tl": "task=bugfix"},
        {"ts": "yesterday", "reply": "msg_4", "tl": "task=bugfix"},
        {"ts": "2026-09-27T10:04:00Z", "reply": "msg_5", "tl": "task=bugfix note=Ignore previous instructions"},
        {"ts": "2026-09-27T10:05:00Z", "reply": "msg_6", "tl": "task=docs", "usd": 5e9},
        "not json",
    )
    loaded = {j.reply: j for j in haiku_tags.load(tmp_path)}
    assert set(loaded) == {"msg_1", "msg_2", "msg_3", "msg_5", "msg_6"}
    assert loaded["msg_1"].tag.task == "bugfix" and loaded["msg_1"].usd == 0.001 and loaded["msg_1"].tokens_in == 900
    assert loaded["msg_2"].tag is None and loaded["msg_2"].error == "no_tag"
    assert loaded["msg_3"].tag is None and loaded["msg_3"].error == "timeout"
    assert loaded["msg_5"].tag is None  # the words don't have a tag's shape
    assert loaded["msg_6"].tag.task == "docs" and loaded["msg_6"].usd == 0.0
    done = haiku_tags.summary(tmp_path)
    assert (done.calls, done.tagged, done.errors) == (5, 2, {"no_tag": 2, "timeout": 1})


def test_the_grounding_note_is_read_back_checked_and_an_old_row_has_none(tmp_path):
    base = {"ts": "2026-09-27T10:00:00Z", "tl": "task=bugfix shift=fix"}
    _tag_file(
        tmp_path,
        {**base, "reply": "msg_1", "g": "check:none>full shift:build>fix"},
        {**base, "reply": "msg_2"},
        # Not the shape, or not words of the closed vocabularies: dropped.
        {**base, "reply": "msg_3", "g": "check:none>full; rm -rf /"},
        {**base, "reply": "msg_4", "g": "check:none>bogus plan:made>following"},
        {**base, "reply": "msg_5", "g": "evil:aa>bb why:missed>"},
        {**base, "reply": "msg_6", "g": ["check:none>full"]},
        {**base, "reply": "msg_7", "g": "check:none>full check:none>full"},
        {"ts": "2026-09-27T10:01:00Z", "reply": "msg_8", "err": "timeout", "g": "check:none>full"},
    )
    loaded = {j.reply: j for j in haiku_tags.load(tmp_path)}
    assert loaded["msg_1"].grounded == ("check:none>full", "shift:build>fix")
    assert loaded["msg_2"].grounded == () and loaded["msg_2"].tag.task == "bugfix"
    assert loaded["msg_3"].grounded == () and loaded["msg_3"].tag is not None
    assert loaded["msg_4"].grounded == ("plan:made>following",)
    assert loaded["msg_5"].grounded == ("why:missed>",)
    assert loaded["msg_6"].grounded == ()
    assert loaded["msg_7"].grounded == ("check:none>full",)
    assert loaded["msg_8"].tag is None and loaded["msg_8"].grounded == ()


def test_the_grounding_note_goes_on_the_tag_and_survives_the_cycle_merge(tmp_path):
    path = _turn(tmp_path)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    config_dir = tmp_path / "cg"
    _tag_file(
        config_dir,
        {"ts": "2026-09-27T10:00:00Z", "reply": result.turns[-1].message_id, "tl": "task=bugfix check=none",
         "g": "check:manual>none"},
    )
    haiku_tags.apply(NS(sessions=[NS(top=result, subs=[])]), config_dir)
    assert result.turns[-1].cap.grounded == ("check:manual>none",)
    cycle = capture.prompt_cycles(result)[-1]
    assert cycle.tag.grounded == ("check:manual>none",)
    # What the transcript's own facts change is added after it: this cycle
    # ran chosen tests, so its "none" reads "targeted".
    assert cycle.settled.check == "targeted"
    assert cycle.settled.grounded == ("check:manual>none", "check:none>targeted")


def test_tags_land_on_their_replies_and_never_over_claudes_own(tmp_path):
    path = _turn(tmp_path)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    last, first = result.turns[-1], result.turns[0]
    text = cat.note_text(("task",), "main")
    note = attachment_line(
        "hook_additional_context", content=[text], hookName="SessionStart", hookEvent="SessionStart",
        rendered=f"<system-reminder>\nSessionStart hook additional context: {text}\n</system-reminder>",
    )
    note["timestamp"] = _at(0)
    tagged = _transcript(tmp_path, [
        note,
        user_str_line("hi", timestamp=_at(0)),
        turn_line(timestamp=_at(1), content=[{"type": "text", "text": "Hello.\n[cg: task=chat]"}]),
    ], "t.jsonl")
    own = parse_transcript(tagged, TranscriptMeta(path=str(tagged)))
    assert own.turns[-1].cap.task == "chat"
    config_dir = tmp_path / "cg"
    _tag_file(
        config_dir,
        {"ts": "2026-09-27T10:00:00Z", "reply": last.message_id, "tl": "task=bugfix brief=clear", "usd": 0.0012},
        {"ts": "2026-09-27T10:00:00Z", "reply": own.turns[-1].message_id, "tl": "task=docs"},
    )
    corpus = NS(sessions=[NS(top=result, subs=[]), NS(top=own, subs=[])])
    assert haiku_tags.apply(corpus, config_dir) == 1
    assert last.cap.task == "bugfix" and last.cap.judged and last.cap.judge_usd == 0.0012 and last.cap.chars == 0
    assert first.cap is None
    assert own.turns[-1].cap.task == "chat" and not own.turns[-1].cap.judged
    # No folder, no change.
    assert haiku_tags.apply(corpus, tmp_path / "nowhere") == 0


def test_capture_counts_haikus_tags_and_prices_its_calls(tmp_path):
    path = _turn(tmp_path)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    config_dir = tmp_path / "cg"
    _tag_file(
        config_dir,
        {"ts": "2026-09-27T10:00:00Z", "reply": result.turns[0].message_id, "tl": "task=feature brief=clear", "usd": 0.001},
        {"ts": "2026-09-27T10:00:00Z", "reply": result.turns[-1].message_id, "tl": "task=bugfix", "usd": 0.002},
    )
    bundle = NS(top=result, subs=[], session_id="s")
    corpus = NS(sessions=[bundle])
    haiku_tags.apply(corpus, config_dir)
    use = capture.usage(corpus, load_pricing())
    # The session had no capture note at all, and still counts.
    assert use.sessions == 1 and use.cycles == 2 and use.tagged_cycles == 2 and use.judged == 2
    assert use.scopes["haiku"].tag_cost == pytest.approx(0.003) and use.tag_tokens == 0
    assert use.answers["task"] == 2 and use.answers["brief"] == 1
    assert use.by_metric["task"] + use.by_metric["brief"] == pytest.approx(0.003)
    cycle = capture.prompt_cycles(result)[1]
    assert cycle.tag.judged and cycle.tag.judge_usd == 0.002


def test_estimates_swap_the_reply_tag_for_a_haiku_call():
    past = capture.History(days=7, sessions=2, cycles=40, main_notes=2, main_note=1e-6, reply_tag=1e-6)
    claude = capture.estimate(past, STANDARD)
    haiku = capture.estimate(past, STANDARD, tagger="haiku")
    assert haiku.tag_tokens < claude.tag_tokens and haiku.note_tokens < claude.note_tokens
    assert haiku.cost == pytest.approx(
        claude.cost
        - (capture._note_chars(STANDARD, "main") - capture._note_chars(STANDARD, "main", tagger="haiku")) * 1e-6
        - (sum(cat.METRICS_BY_ID[i].out_chars for i in cat.tagged_keys(STANDARD)) + capture._TAG_FRAME_CHARS) * 1e-6
        + 40 * cat.JUDGE_USD_PER_CALL
    )
    rough = cat.rough_tokens(STANDARD, "haiku")
    assert rough["reply_tag"] == 0 and rough["session_note"] < cat.rough_tokens(STANDARD)["session_note"]


def test_prune_deletes_old_months(tmp_path):
    folder = tmp_path / cat.JUDGE_DIR
    folder.mkdir()
    for name in ("2026-01.jsonl", "2026-09.jsonl", "notes.txt"):
        (folder / name).write_text("", encoding="utf-8")
    assert haiku_tags.prune(tmp_path, 90, now=datetime(2026, 9, 27, tzinfo=timezone.utc)) == 1
    assert sorted(p.name for p in folder.iterdir()) == ["2026-09.jsonl", "notes.txt"]


# -- the command -----------------------------------------------------------------


def _capture(config_dir, *argv, stdin=""):
    args = cli._make_parser().parse_args(["capture", *argv, "--config-dir", str(config_dir)])
    out = io.StringIO()
    rc = cli._cmd_capture(args, stdin=io.StringIO(stdin), stdout=out)
    return rc, out.getvalue()


@pytest.fixture
def claude_dir(tmp_path, monkeypatch):
    claude = tmp_path / "claude"
    (claude / "claudeglass").mkdir(parents=True)
    (claude / "settings.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    from claudeglass import installer

    monkeypatch.setattr(installer, "is_registered", lambda *a, **k: False)
    return claude / "claudeglass"


def test_capture_tagger_asks_first_and_says_what_it_sends(claude_dir):
    set_capture(claude_dir, level="essentials")
    rc, out = _capture(claude_dir, "tagger", "haiku", stdin="n\n")
    assert rc == 1 and "Left unchanged." in out and cat.JUDGE_MODEL
    assert "your own Claude Code login" in out and "Only the tag's words are kept" in out
    assert load_config(config_dir=claude_dir).capture.tagger == "claude"
    rc, out = _capture(claude_dir, "tagger", "haiku", "--yes", "--dry-run")
    assert rc == 0 and load_config(config_dir=claude_dir).capture.tagger == "claude"
    rc, out = _capture(claude_dir, "tagger", "haiku", stdin="y\ny\n")
    assert rc == 0 and load_config(config_dir=claude_dir).capture.tagger == "haiku"
    rc, out = _capture(claude_dir, "tagger", "gpt")
    assert rc == 2 and "claude, haiku" in out


def test_capture_status_says_what_haiku_did(claude_dir):
    set_capture(claude_dir, level="essentials", tagger="haiku", now=datetime(2026, 9, 1, tzinfo=timezone.utc))
    rc, out = _capture(claude_dir, "status")
    assert "tags by Haiku" in out and "hasn't tagged a turn yet" in out
    assert f"a Claude Haiku call of about ${cat.JUDGE_USD_PER_CALL:.4f} after each of your messages" in out
    _tag_file(
        claude_dir,
        {"ts": "2026-09-27T10:00:00Z", "reply": "msg_1", "tl": "task=bugfix", "usd": 0.0014, "in": 1200},
        {"ts": "2026-09-27T10:01:00Z", "reply": "msg_2", "err": "no_cli"},
        # From before a switch to the haiku tagger: the fallback's call, not the tagger's.
        {"ts": "2026-09-27T10:02:00Z", "reply": "msg_3", "tl": "task=docs", "usd": 0.5, "w": "haiku-fallback"},
    )
    rc, out = _capture(claude_dir, "status")
    assert "Claude Haiku tagged 1 of the 2 turns it was asked about: $0.0014 ($0.0014 a call)" in out
    assert "No tag for 1 (no claude command on the hook's path)" in out


def test_capture_status_says_how_agent_runs_were_judged_whoever_writes_the_tags(claude_dir):
    """Agent runs are Haiku's to judge while Claude writes the main tags
    too, so their calls, cost and failures show either way."""
    set_capture(claude_dir, level="essentials", now=datetime(2026, 9, 1, tzinfo=timezone.utc))
    rc, out = _capture(claude_dir, "status")
    assert "hasn't judged an agent run yet" in out and "hasn't tagged a turn yet" not in out
    _tag_file(
        claude_dir,
        {"ts": "2026-09-27T10:00:00Z", "reply": "msg_1", "agent": "result=done", "usd": 0.0015, "in": 1500},
        {"ts": "2026-09-27T10:01:00Z", "reply": "msg_2", "agent": "", "err": "no_login"},
        {"ts": "2026-09-27T10:02:00Z", "reply": "msg_3", "agent": "", "err": "no_login"},
    )
    rc, out = _capture(claude_dir, "status")
    assert "Claude Haiku judged 1 of the 3 agent runs it was asked about: $0.0015 ($0.0015 a call)" in out
    assert "No verdict for 2 (the claude command isn't signed in: run 'claude auth login')" in out
    assert "turns it was asked about" not in out


def test_your_changes_names_a_tagger_change():
    from claudeglass import change_points

    record = {"level": "standard", "changed": {"tagger": {"from": "claude", "to": "haiku"}}}
    assert change_points._capture_label(record) == "Claude Haiku writes the tags"
    back = {"level": "standard", "changed": {"tagger": {"from": "haiku", "to": "claude"}}}
    assert change_points._capture_label(back) == "Claude writes the tags again"


def _ran(tmp_path, tool: str, command: str) -> dict:
    path = _transcript(tmp_path, [
        user_str_line("run the checks", timestamp=_at(0)),
        turn_line(timestamp=_at(1), content=[tool_use_block(tool, "c1", {"command": command}),
                                             {"type": "text", "text": "Ran them."}]),
    ])
    return HOOK.judge_job(_stop(path, last_assistant_message=""), HAIKU, CATALOGUE)


def test_the_excerpt_reads_a_whole_command_for_the_tests_it_runs(tmp_path):
    # A heredoc's body is a message, not a run; a run on a later line counts.
    message = "git commit -m \"$(cat <<'EOF'\nRun pytest tests/test_a.py before merging\nEOF\n)\""
    assert _ran(tmp_path, "Bash", message)["facts"]["tests"] == ""
    assert _ran(tmp_path, "Bash", "cd /w\npytest tests/test_a.py -q")["facts"]["tests"] == "targeted"
    assert _ran(tmp_path, "Bash", "cd /w\npytest tests/test_a.py -q\npytest -q")["facts"]["tests"] == "full"


def test_the_excerpt_counts_a_powershell_test_run_and_lists_its_command(tmp_path):
    job = _ran(tmp_path, "PowerShell", "& \"C:\\Python311\\python.exe\" -m pytest tests\\test_a.py")
    assert job["facts"]["tests"] == "targeted" and "Tests run: chosen tests." in job["excerpt"]
    assert job["facts"]["commands"] == 1 and "Shell commands: `& " in job["excerpt"]


# -- what else the excerpt says ----------------------------------------------------


def _job(tmp_path, lines, config=HAIKU, **stop):
    return HOOK.judge_job(_stop(_transcript(tmp_path, lines), **stop), config, CATALOGUE)


def _excerpt_lines(tmp_path, lines, **stop) -> list[str]:
    return _job(tmp_path, lines, **stop)["excerpt"].splitlines()


def _line_starting(tmp_path, lines, *starts: str, **stop) -> str:
    return next((line for line in _excerpt_lines(tmp_path, lines, **stop) if line.startswith(starts)), "")


def _edits(*names: str) -> list[dict]:
    return [tool_use_block("Edit", f"e{n}", {"file_path": f"/w/{name}"}) for n, name in enumerate(names)]


def _said(text: str) -> dict:
    return {"type": "text", "text": text}


def test_the_excerpt_says_how_long_the_user_was_away_and_what_the_reply_before_it_changed(tmp_path):
    lines = [
        user_str_line("Add a button", timestamp=_at(0)),
        turn_line(timestamp=_at(1), content=[*_edits("ui/button.py", "ui/panel.py"), _said("Added.")]),
        user_str_line("Make it smaller", timestamp=_at(181)),
        turn_line(timestamp=_at(182), content=[*_edits("ui/button.py"), _said("Done.")]),
    ]
    excerpt = _job(tmp_path, lines, cwd="/w")["excerpt"]
    gap = "Minutes from Claude's previous reply to this message: 3."
    again = "Files Claude changed in its previous reply: 2; 1 of them changed again in this reply."
    assert gap in excerpt and again in excerpt
    # After the count of earlier messages, before what Claude did.
    assert excerpt.index("Messages the user sent before it") < excerpt.index(gap) < excerpt.index(again)
    assert excerpt.index(again) < excerpt.index("What Claude did:")
    # A reply that changed nothing says so, and no file changed again.
    quiet = [lines[0], turn_line(timestamp=_at(1), content=[_said("Added.")]),
             user_str_line("Make it smaller", timestamp=_at(20)), lines[3]]
    excerpt = _job(tmp_path, quiet, cwd="/w")["excerpt"]
    assert "Minutes from Claude's previous reply to this message: 0." in excerpt
    assert "Files Claude changed in its previous reply: 0.\n" in excerpt and "changed again" not in excerpt
    # Plan files and Claude's own files are no work of yours, there as in "files changed".
    config = [lines[0], turn_line(timestamp=_at(1), content=[
        tool_use_block("Write", "w1", {"file_path": "/home/me/.claude/plans/p.md", "content": "1."}),
        tool_use_block("Edit", "w2", {"file_path": "/home/me/.claude/projects/x/memory/m.md"}),
        *_edits("a.py"), _said("Planned.")]),
        user_str_line("Go on", timestamp=_at(30)), turn_line(timestamp=_at(31), content=[*_edits("a.py"), _said("Done.")])]
    excerpt = _job(tmp_path, config, cwd="/w")["excerpt"]
    assert "Files Claude changed in its previous reply: 1; 1 of them changed again in this reply." in excerpt
    # Neither says anything about a session's first message.
    first = _job(tmp_path, lines[:2], cwd="/w")["excerpt"]
    assert "Minutes from Claude's previous reply" not in first and "Files Claude changed in its previous" not in first


def test_the_excerpt_counts_the_short_follow_ups_in_a_row(tmp_path):
    limit = cat.JUDGE_LIMITS["short"]
    head = f"Short follow-ups the user has sent in a row, this one included (up to {limit} characters each): "

    def counted(*messages: str) -> str:
        lines: list[dict] = []
        for n, text in enumerate(messages):
            lines += [user_str_line(text, timestamp=_at(n * 10)),
                      turn_line(timestamp=_at(n * 10 + 1), content=[_said("Done.")])]
        return _line_starting(tmp_path, lines, "Short follow-ups")

    assert limit == 300
    long = "x" * 400
    assert counted(long, "ok", "go on") == head + "2."
    assert counted(long, "ok", "go on", "and sort it") == head + "3."
    # The session's first message follows nothing, so it is no follow-up.
    assert counted("hi", "ok") == head + "1."
    assert counted("hi") == ""
    # A long message ends the run, and so does a long one right now.
    assert counted("ok", long, "go on") == head + "1."
    assert counted("hi", "ok", long) == ""
    # 300 characters is short; 301 is not.
    assert counted("hi", "ok", "x" * 300) == head + "2."
    assert counted("hi", "ok", "x" * 301) == ""


def test_the_short_follow_ups_of_a_window_that_starts_inside_the_session_are_at_least_that_many(tmp_path, monkeypatch):
    monkeypatch.setattr(HOOK, "_JUDGE_TAIL_BYTES", 600)
    lines = [user_str_line("Old message " + "x" * 400, timestamp=_at(0)),
             turn_line(timestamp=_at(1), content=[_said("Old reply.")]),
             user_str_line("Fix it", timestamp=_at(10)),
             turn_line(timestamp=_at(11), content=[_said("Fixed.")])]
    line = _line_starting(tmp_path, lines, "Short follow-ups")
    assert line.endswith("each): 1 or more.")
    # The first message in the window may not be the session's first.
    assert "Messages the user sent before it in this session: 0 or more." in _excerpt_lines(tmp_path, lines)


def _queued(text, second, mode="prompt", **extra) -> dict:
    line = attachment_line("queued_command", prompt=text, commandMode=mode, **extra)
    line["timestamp"] = _at(second)
    return line


def test_the_excerpt_quotes_the_first_messages_the_user_sent_while_claude_worked(tmp_path):
    notification = "<task-notification>\n<task-id>a2</task-id>\n<status>completed</status>\n</task-notification>"
    lines = [
        user_str_line("Start on the parser", timestamp=_at(0)),
        _queued("Left over from the first piece of work", 1),
        turn_line(timestamp=_at(2), content=[_said("Started.")]),
        user_str_line("Refactor the parser", timestamp=_at(10)),
        turn_line(timestamp=_at(11), content=[tool_use_block("Read", "r1", {"file_path": "/w/p.py"})]),
        _queued("also keep the old API", 12),
        _queued([{"type": "text", "text": "and"}, {"type": "text", "text": "the tests"}], 13),
        # Not messages of the user's: a report, another session, a note, a shell command.
        _queued(notification, 14),
        _queued("from another session", 15, origin={"kind": "peer"}),
        _queued("a reminder", 16, isMeta=True),
        _queued("ls -la", 17, mode="bash"),
        turn_line(timestamp=_at(20), content=[_said("Refactored.")]),
    ]
    job = _job(tmp_path, lines)
    queued = [line for line in job["excerpt"].splitlines() if line.startswith("The user sent")]
    assert queued == ['The user sent 2 more messages while Claude worked: "also keep the old API"; "and the tests".']
    for left_out in ("Left over", "task-id", "another session", "a reminder", "ls -la"):
        assert left_out not in job["excerpt"]
    # It sits among the lines about the message, before what Claude did.
    assert job["excerpt"].index("Messages the user sent before it") < job["excerpt"].index("The user sent 2") \
        < job["excerpt"].index("What Claude did:")
    # One is "one more message", and none is no line.
    one = _line_starting(tmp_path, [*lines[:5], _queued("just one", 12), lines[-1]], "The user sent")
    assert one == 'The user sent 1 more message while Claude worked: "just one".'
    assert _line_starting(tmp_path, [*lines[:5], lines[-1]], "The user sent") == ""
    # Only the first few are quoted, each cut to its limit.
    count, size = cat.JUDGE_LIMITS["queued_count"], cat.JUDGE_LIMITS["queued"]
    many = [_queued(f"message {n}", 12 + n) for n in range(count + 2)]
    line = _line_starting(tmp_path, [*lines[:5], *many, lines[-1]], "The user sent")
    assert line == (f"The user sent {count + 2} more messages while Claude worked (the first {count} below): "
                    + "; ".join(f'"message {n}"' for n in range(count)) + ".")
    long = _line_starting(tmp_path, [*lines[:5], _queued("word " * 200, 12), lines[-1]], "The user sent")
    assert "[...]" in long and len(long) < len('The user sent 1 more message while Claude worked: "".') + size + 8


def test_what_the_user_sent_while_claude_worked_goes_to_haiku_and_is_never_kept(tmp_path):
    lines = [
        user_str_line("Refactor the parser", timestamp=_at(0)),
        _queued("also keep the old API", 2),
        turn_line(timestamp=_at(3), content=[_said("Refactored.")]),
    ]
    job = _job(tmp_path, lines, CLAUDE_STOP)
    assert "also keep the old API" in job["excerpt"]
    assert "also keep the old API" not in json.dumps(job["facts"])
    record = HOOK.run_judge(tmp_path / "cg", CATALOGUE, job, ask=lambda *_: _answer("[cg: task=refactor]"))
    text = (tmp_path / "cg" / cat.JUDGE_DIR / f"{job['ts'][:7]}.jsonl").read_text(encoding="utf-8")
    assert json.loads(text) == record
    assert "also keep" not in text and "Refactor" not in text


def _feedback(text: str) -> str:
    return "The user doesn't want to proceed with this tool use. To tell you how to proceed, the user said:\n" + text


def test_the_excerpt_counts_how_often_a_plan_was_sent_back_with_words(tmp_path):
    said = _plan_result(2, is_error=True, text=_feedback("Use the second option"))
    path = _turn(tmp_path, earlier_plan=True, plan_answer=said)
    assert _earlier_plan_text(path) == (
        "Plan mode: a plan was proposed 1 time in this session and not approved; "
        "1 round of the user's feedback on plans."
    )
    # In plan mode now, and a plan written in this turn, with one sent back before it.
    assert "Plan mode: on, a plan was proposed 1 time" in HOOK.judge_job(
        _stop(path, permission_mode="plan"), HAIKU, CATALOGUE)["excerpt"]
    again = _turn(tmp_path, plan=True, earlier_plan=True, plan_answer=said)
    assert _earlier_plan_text(again) == (
        "Plan mode: Claude wrote a plan in this turn; 1 plan proposed before it, none approved; "
        "1 round of the user's feedback on plans."
    )
    # Words after "the user said:" are what make a round: a bare "no" is not one.
    for text in ("The user doesn't want to proceed with this tool use.", _feedback("   ")):
        bare = _turn(tmp_path, earlier_plan=True, plan_answer=_plan_result(2, is_error=True, text=text))
        assert "round" not in _earlier_plan_text(bare)
    # A plan approved after one was sent back with words still says so.
    lines = [
        user_str_line("Plan the export", timestamp=_at(0)),
        turn_line(timestamp=_at(1), content=[tool_use_block("ExitPlanMode", "tp", {"plan": "1. a"})]),
        said,
        turn_line(timestamp=_at(3), content=[tool_use_block("ExitPlanMode", "tq", {"plan": "1. b"})]),
        {"type": "user", "timestamp": _at(4), "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tq", "content": "ok"}]}},
        user_str_line("Now build it", timestamp=_at(10)),
        turn_line(timestamp=_at(11), content=[_said("Built.")]),
    ]
    assert _line_starting(tmp_path, lines, "Plan mode") == (
        "Plan mode: the user approved a plan earlier in this session; 1 round of the user's feedback on plans."
    )


def test_the_excerpt_counts_plans_sent_back_within_this_turn(tmp_path):
    def answer(second, tool_id, text, error):
        return {"type": "user", "timestamp": _at(second), "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tool_id, "content": text, "is_error": error}]}}
    lines = [
        user_str_line("Add a calc module", timestamp=_at(0)),
        turn_line(timestamp=_at(1), content=[tool_use_block("ExitPlanMode", "p1", {"plan": "1. a"})]),
        answer(2, "p1", _feedback("Use ints"), True),
        turn_line(timestamp=_at(3), content=[tool_use_block("ExitPlanMode", "p2", {"plan": "1. b"})]),
        answer(4, "p2", _feedback("Add tests"), True),
        turn_line(timestamp=_at(5), content=[tool_use_block("ExitPlanMode", "p3", {"plan": "1. c"})]),
        answer(6, "p3", "ok", False),
        turn_line(timestamp=_at(7), content=[_said("Built.")]),
    ]
    assert _line_starting(tmp_path, lines, "Plan mode") == (
        "Plan mode: Claude wrote a plan in this turn, proposed 3 times; 2 rounds of the user's feedback on plans."
    )
    # Three proposals and none approved says so; a single one says nothing of a count.
    unanswered = [*lines[:6], answer(6, "p3", _feedback("Again"), True), lines[7]]
    assert _line_starting(tmp_path, unanswered, "Plan mode") == (
        "Plan mode: Claude wrote a plan in this turn, proposed 3 times, none approved; "
        "3 rounds of the user's feedback on plans."
    )
    single = _line_starting(tmp_path, [*lines[:3], lines[7]], "Plan mode")
    assert single.startswith("Plan mode: Claude wrote a plan in this turn;") and "proposed" not in single


def test_a_plan_call_outside_the_window_read_is_not_a_plan_never_used(tmp_path, monkeypatch):
    monkeypatch.setattr(HOOK, "_JUDGE_TAIL_BYTES", 600)
    lines = [user_str_line("Old message " + "x" * 400, timestamp=_at(0)),
             turn_line(timestamp=_at(1), content=[_said("Old reply.")]),
             user_str_line("Fix it", timestamp=_at(10)),
             turn_line(timestamp=_at(11), content=[_said("Fixed.")])]
    unknown = "Plan mode: unknown, the excerpt covers only the end of a long session."
    assert _line_starting(tmp_path, lines, "Plan mode") == unknown
    assert _line_starting(tmp_path, lines, "Plan mode", permission_mode="plan") == "Plan mode: on, no plan written yet."
    # The whole transcript, with none in it, is where "not used" is true.
    monkeypatch.undo()
    assert _line_starting(tmp_path, lines, "Plan mode") == "Plan mode: not used in this session."


def test_the_excerpt_opens_with_the_request_the_work_began_with(tmp_path):
    limit, floor = cat.JUDGE_LIMITS["origin"], cat.JUDGE_LIMITS["origin_min"]
    assert (limit, floor) == (600, 300)
    brief = "Build an inventory report. " * 30
    start = 'The request this work began with, the user\'s latest long message: "'

    def talk(*messages: str) -> list[dict]:
        lines: list[dict] = []
        for n, text in enumerate(messages):
            lines += [user_str_line(text, timestamp=_at(n * 10)),
                      turn_line(timestamp=_at(n * 10 + 1), content=[_said("Done.")])]
        return lines

    # A short follow-up is judged against the long request before it, cut at its limit.
    excerpt = _job(tmp_path, talk(brief, "ok", "and sort it"))["excerpt"]
    flat = " ".join(brief.split())
    assert excerpt.splitlines()[0] == f'{start}{flat[:limit]} [...]"'
    # The latest long one, not an older one; none when this message is the long one; 300 is not long.
    later = _job(tmp_path, talk("alpha " * 70, "beta " * 70, "ok"))["excerpt"].splitlines()[0]
    assert later.startswith(start + "beta beta") and "alpha" not in later
    assert _line_starting(tmp_path, talk("hi", "ok", brief), "The request this work", "The plan the user") == ""
    assert _line_starting(tmp_path, talk(brief, "ok", "z " * 200), "The request this work") == ""
    assert _line_starting(tmp_path, talk("x" * floor, "ok"), "The request this work") == ""
    assert _line_starting(tmp_path, talk("x" * (floor + 1), "ok"), "The request this work").startswith(start)
    # The plan the user approved takes its place, its first 600 characters.
    plan = "step " * 200
    lines = [
        user_str_line(brief, timestamp=_at(0)),
        turn_line(timestamp=_at(1), content=[tool_use_block("ExitPlanMode", "tp", {"plan": plan})]),
        _plan_result(2),
        user_str_line("go", timestamp=_at(10)),
        turn_line(timestamp=_at(11), content=[_said("Built.")]),
    ]
    first = _excerpt_lines(tmp_path, lines)[0]
    assert first.startswith('The plan the user approved, which this work carries out: "step step')
    assert ("step " * 120).strip() in first and ("step " * 121).strip() not in first and "inventory" not in first
    # A plan sent back is no plan to carry out: the long message stands.
    sent_back = [lines[0], lines[1], _plan_result(2, is_error=True, text="The user doesn't want to proceed."),
                 user_str_line("Make the title bigger", timestamp=_at(10)), lines[4]]
    assert _excerpt_lines(tmp_path, sent_back)[0].startswith(start)


def test_a_replayed_session_gives_the_excerpt_it_would_have_without_the_replay(tmp_path):
    first = user_str_line("Add a calculator module", timestamp=_at(0), uuid="u0")
    plan = turn_line(timestamp=_at(1), uuid="a0", message_id="m0", content=[
        tool_use_block("ExitPlanMode", "tp", {"plan": "1. do it"}), _said("Planned.")])
    answer = _plan_result(2, uuid="r0")
    second = user_str_line("add() returns the wrong sum, fix it", timestamp=_at(10), uuid="u1")
    fix = turn_line(timestamp=_at(11), uuid="a1", message_id="m1", content=[*_edits("calc.py"), _said("Fixed.")])
    clean = [first, plan, answer, second, fix]
    expected = _job(tmp_path, clean, cwd="/w")
    assert expected["reply"] == "m1" and "Messages the user sent before it in this session: 1." in expected["excerpt"]
    assert "the user approved a plan earlier" in expected["excerpt"]
    # The app writes the start again when it resumes: the same lines, and an old reply under a new id.
    for replay in ([first, plan, answer], [turn_line(timestamp=_at(1), message_id="m9", content=[_said("Planned.")])]):
        job = _job(tmp_path, [*clean, *replay], cwd="/w")
        assert job["excerpt"] == expected["excerpt"] and job["reply"] == "m1" and job["facts"] == expected["facts"]


def test_the_agent_excerpt_lists_a_powershell_command_as_it_does_a_bash_one(tmp_path):
    session, _ = _agent_run(tmp_path)
    agent = _transcript(tmp_path, [
        user_str_line("Run the tests for mod1.py", timestamp=_at(20)),
        turn_line(timestamp=_at(21), model="claude-sonnet-5", message_id="msg_p1", content=[
            tool_use_block("PowerShell", "p1", {"command": "pytest -q tests\\test_mod.py\nWrite-Output ok"})]),
        turn_line(timestamp=_at(23), model="claude-sonnet-5", message_id="msg_p2", content=[_said("Ran them.")]),
    ], "agent-p3.jsonl")
    excerpt = _prepared(session, agent, agent_id="p3")["excerpt"]
    assert "Shell commands: `pytest -q tests\\test_mod.py`." in excerpt and "Write-Output" not in excerpt
    assert "tools: PowerShell 1" in excerpt


def test_the_hook_and_the_parser_read_a_plans_feedback_the_same_way(tmp_path):
    from claudeglass import parse

    assert cat.PLAN_SAID_PATTERN == parse._USER_SAID_RE.pattern and parse._USER_SAID_RE.flags & 2  # IGNORECASE
    assert CATALOGUE["coaching"]["plan_said_pattern"] == cat.PLAN_SAID_PATTERN
    assert CATALOGUE["coaching"]["scheduled_task_prefix"] == cat.SCHEDULED_TASK_PREFIX
    assert CATALOGUE["judge"]["limits"] == cat.JUDGE_LIMITS and CATALOGUE["judge"]["fallback_writer"] == cat.JUDGE_FALLBACK_WRITER
    assert cat.JUDGE_WRITERS == (cat.JUDGE_WRITER, cat.JUDGE_FALLBACK_WRITER)
    # The same plan, sent back with words, is a round to both.
    path = _turn(tmp_path, earlier_plan=True, plan_answer=_plan_result(2, is_error=True, text=_feedback("Use B")))
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    plans = [turn.plan_stats for turn in result.turns if turn.plan_stats is not None]
    assert [plan.feedback_chars > 0 for plan in plans] == [True]
    assert "1 round of the user's feedback" in _earlier_plan_text(path)


# -- Haiku fills in a tag Claude left out -------------------------------------------

CLAUDE_STOP = {"capture": {"level": "standard", "tagger": "claude"}}
NOW = datetime(2026, 9, 27, 10, 30, tzinfo=timezone.utc)


def test_the_fallback_asks_haiku_for_an_untagged_reply_that_ends_a_piece_of_work(tmp_path):
    path = _turn(tmp_path)
    asked = HOOK.judge_job(_stop(path), HAIKU, CATALOGUE, now=NOW)
    fallback = HOOK.judge_job(_stop(path), CLAUDE_STOP, CATALOGUE, now=NOW)
    # The same call as when Haiku tags every turn, and marked as the fallback's.
    assert "writer" not in asked
    assert fallback == {**asked, "writer": "haiku-fallback"}
    assert fallback["writer"] == cat.JUDGE_FALLBACK_WRITER == CATALOGUE["judge"]["fallback_writer"]
    # What the transcript settles still grounds its words.
    assert fallback["facts"]["judged"] and fallback["facts"]["files"] == 1
    # The reply text may be missing from the payload: the transcript has it.
    assert HOOK.judge_job(_stop(path, last_assistant_message=None), CLAUDE_STOP, CATALOGUE, now=NOW) == fallback
    for level in ("essentials", "deep"):
        config = {"capture": {"level": level, "tagger": "claude"}}
        assert HOOK.judge_job(_stop(path), config, CATALOGUE, now=NOW)["writer"] == "haiku-fallback", level
    # No tagger line at all is Claude's: the fallback applies there too.
    assert HOOK.judge_job(_stop(path), {"capture": {"level": "standard"}}, CATALOGUE, now=NOW) == fallback


def _work(tmp_path, *texts: str, first="Fix the sum, it is wrong", before=()) -> Path:
    """One message of yours and Claude's replies to it: an edit and its
    result, then a last reply with each of ``texts``."""
    lines = [*before, user_str_line(first, timestamp=_at(100))]
    for n, text in enumerate(texts):
        final = n == len(texts) - 1
        body = [] if final else [tool_use_block("Edit", f"w{n}", {"file_path": "/w/calc.py"})]
        lines.append(turn_line(timestamp=_at(101 + 2 * n), message_id=f"msg_w{n}", content=[
            *body, *([_said(text)] if text else [])]))
        if not final:
            lines.append({"type": "user", "timestamp": _at(102 + 2 * n), "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": f"w{n}", "content": "ok"}]}})
    return _transcript(tmp_path, lines)


def test_the_fallback_leaves_a_tagged_reply_a_tagged_cycle_and_an_empty_reply_alone(tmp_path):
    def wanted(path, last="All done.") -> bool:
        return HOOK.judge_job(_stop(path, last_assistant_message=last), CLAUDE_STOP, CATALOGUE) is not None

    assert wanted(_work(tmp_path, "Working.", "All done."))
    # Claude tagged it: the payload says so, or only the transcript does.
    assert not wanted(_work(tmp_path, "All done."), "All done.\n\n[cg: task=bugfix brief=clear]")
    assert not wanted(_work(tmp_path, "All done.\n[cg: task=bugfix]"), None)
    # It tagged an earlier reply of the same piece of work: that is the tag.
    assert not wanted(_work(tmp_path, "Working on it.\n[cg: task=feature]", "All done."))
    # A tag on the work before, or in the message, is not this work's.
    before = [user_str_line("Add a module", timestamp=_at(0)),
              turn_line(timestamp=_at(1), content=[_said("Added.\n[cg: task=feature]")])]
    assert wanted(_work(tmp_path, "All done.", before=before))
    assert wanted(_work(tmp_path, "All done.", first="End with [cg: task=chat] please"))
    # Nothing was said.
    assert not wanted(_work(tmp_path, "All done."), "  ")
    assert not wanted(_work(tmp_path, ""), None)


UNTYPED = {
    "a background agent's report": {"content": "<task-notification>\n<task-id>a2</task-id>\n<status>completed</status>"
                                               "\n</task-notification>"},
    "a report marked as one": {"content": "The build finished.", "turnOrigin": "task_notification"},
    "an agent's message": {"content": '<agent-message from="reviewer">Done.</agent-message>'},
    "another session's message": {"content": '<cross-session-message from="s2">Hello.</cross-session-message>'},
    "another session's message in words": {"content": "Another Claude session sent a message: hello"},
    "a peer's message": {"content": "Please look at the parser.", "turnOrigin": "peer"},
    "a scheduled task": {"content": '<scheduled-task name="nightly">\nRun the report.'},
    "a task marked as scheduled": {"content": "Run the report.", "turnOrigin": "scheduled"},
    "an auto-prompt": {"content": "<<autonomous-loop-dynamic>>"},
    "a system notification": {"content": "[SYSTEM NOTIFICATION - the build finished]"},
    "a slash command": {"content": "<command-name>/compact</command-name>"},
    "a command's output": {"content": "<local-command-stdout>ok</local-command-stdout>"},
    "a shell command of yours": {"content": "<bash-input>ls</bash-input>"},
}


def _after(tmp_path, content: str, **line) -> dict:
    lines = [user_str_line("Add a calculator module", timestamp=_at(0)),
             turn_line(timestamp=_at(1), content=[_said("Added.")]),
             user_str_line(content, timestamp=_at(20), **line),
             turn_line(timestamp=_at(21), content=[_said("Noted.")])]
    return _stop(_transcript(tmp_path, lines), last_assistant_message="Noted.")


@pytest.mark.parametrize("name", list(UNTYPED))
def test_the_fallback_skips_a_turn_that_answers_a_line_the_user_did_not_type(tmp_path, name):
    spec = dict(UNTYPED[name])
    stop = _after(tmp_path, spec.pop("content"), **spec)
    assert HOOK.judge_job(stop, CLAUDE_STOP, CATALOGUE) is None


def test_the_fallback_answers_a_message_the_user_typed_and_the_apps_resume_ping(tmp_path):
    assert HOOK.judge_job(_after(tmp_path, "Now add a subtract function"), CLAUDE_STOP, CATALOGUE)
    assert HOOK.judge_job(_after(tmp_path, "Now add a subtract function", turnOrigin="human"), CLAUDE_STOP, CATALOGUE)
    # The app's ping carries on the work of your last message: its end is that piece of work's.
    for ping in (cat.LIMIT_RESUME_PREFIX + ", so carry on.", cat.APP_QUIT_PREFIX + ", so carry on."):
        assert HOOK.judge_job(_after(tmp_path, ping), CLAUDE_STOP, CATALOGUE)["writer"] == "haiku-fallback", ping
    # A report that came before your last message is behind it.
    lines = [user_str_line("Add a module", timestamp=_at(0)),
             user_str_line(UNTYPED["a background agent's report"]["content"], timestamp=_at(5)),
             turn_line(timestamp=_at(6), content=[_said("Noted.")]),
             user_str_line("Now fix it", timestamp=_at(10)),
             turn_line(timestamp=_at(11), content=[_said("Fixed.")])]
    stop = _stop(_transcript(tmp_path, lines), last_assistant_message="Fixed.")
    assert HOOK.judge_job(stop, CLAUDE_STOP, CATALOGUE)["writer"] == "haiku-fallback"


def test_the_fallback_waits_for_agents_and_never_runs_in_a_call_haiku_makes_or_a_subagent(tmp_path, monkeypatch):
    path = _turn(tmp_path)
    agent = {"id": "a1", "type": "subagent", "status": "running", "description": "Find the files"}
    assert HOOK.judge_job(_stop(path, background_tasks=[agent]), CLAUDE_STOP, CATALOGUE) is None
    assert HOOK.judge_job(_stop(path, background_tasks=[{**agent, "status": "completed"}]), CLAUDE_STOP, CATALOGUE)
    server = {"id": "b1", "type": "shell", "status": "running", "description": "npm run dev"}
    assert HOOK.judge_job(_stop(path, background_tasks=[server]), CLAUDE_STOP, CATALOGUE)
    assert HOOK.judge_job(_stop(path, stop_hook_active=True), CLAUDE_STOP, CATALOGUE) is None
    assert HOOK.judge_job(_stop(path, agent_id="a1"), CLAUDE_STOP, CATALOGUE) is None
    assert HOOK.judge_job({**_stop(path), "hook_event_name": "SubagentStop"}, CLAUDE_STOP, CATALOGUE) is None
    # Capture off or past its end, a session sampled out, and a level with no tag to ask for.
    for capture in ({"level": "off"}, {"level": "free"}, {"level": "standard", "sample": 0},
                    {"level": "standard", "until": "2020-01-01T00:00:00Z"}):
        assert HOOK.judge_job(_stop(path), {"capture": {"tagger": "claude", **capture}}, CATALOGUE) is None, capture
    # The call Haiku itself runs in.
    monkeypatch.setenv(cat.JUDGE_ENV, "1")
    assert HOOK.judge_job(_stop(path), CLAUDE_STOP, CATALOGUE) is None


def _launched(tmp_path, tool: str, result: dict) -> dict:
    """Your message, Claude starting ``tool`` with ``result`` as its tool
    result, and its untagged reply saying so."""
    lines = [user_str_line("Build the export feature", timestamp=_at(0)),
             turn_line(timestamp=_at(1), message_id="msg_l1", content=[tool_use_block(tool, "l1", {"description": "Build it"})]),
             {"type": "user", "timestamp": _at(2), "toolUseResult": result,
              "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "l1", "content": "Launched."}]}},
             turn_line(timestamp=_at(3), message_id="msg_l2", content=[_said("Started it in the background.")])]
    return _stop(_transcript(tmp_path, lines), last_assistant_message="Started it in the background.")


LAUNCHES = {"Workflow": {"status": "async_launched", "taskId": "w1"},
            "Agent": {"isAsync": True, "status": "async_launched", "agentId": "a1"}}


@pytest.mark.parametrize("tool", list(LAUNCHES))
def test_the_fallback_leaves_a_turn_that_started_background_work_to_its_report(tmp_path, tool):
    assert HOOK.judge_job(_launched(tmp_path, tool, LAUNCHES[tool]), CLAUDE_STOP, CATALOGUE) is None
    # One that ran in the foreground and finished is the turn's own work.
    done = _launched(tmp_path, tool, {"status": "completed", "toolStats": {"editFileCount": 0}})
    assert HOOK.judge_job(done, CLAUDE_STOP, CATALOGUE)["writer"] == "haiku-fallback"


def _shell_in_background(tmp_path, before=()) -> dict:
    """Your message, Claude sending a test run to the background, and its
    untagged reply while the run is still out."""
    lines = [*before, user_str_line("Run the tests", timestamp=_at(10)),
             turn_line(timestamp=_at(11), message_id="msg_s1", content=[
                 tool_use_block("Bash", "s1", {"command": "pytest -q", "run_in_background": True})]),
             {"type": "user", "timestamp": _at(12), "toolUseResult": {}, "message": {"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": "s1", "content": "Command running in background with ID: b1"}]}},
             turn_line(timestamp=_at(13), message_id="msg_s2", content=[_said("The tests are running.")])]
    return _stop(_transcript(tmp_path, lines), last_assistant_message="The tests are running.")


def test_the_fallback_waits_for_a_command_sent_to_the_background(tmp_path):
    # Its notification wakes Claude, which tags the reply after it.
    assert HOOK.judge_job(_shell_in_background(tmp_path), CLAUDE_STOP, CATALOGUE) is None
    # A command sent to the background for earlier work doesn't hold this one up.
    earlier = _launched(tmp_path, "Workflow", {"status": "completed"})
    path = Path(earlier["transcript_path"])
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    lines[1]["message"]["content"] = [tool_use_block("Bash", "l1", {"command": "npm run dev", "run_in_background": True})]
    lines[2]["message"]["content"][0]["content"] = "Command running in background with ID: b0"
    lines.append(user_str_line("Now fix the header", timestamp=_at(5)))
    lines.append(turn_line(timestamp=_at(6), message_id="msg_l3", content=[_said("Fixed the header.")]))
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    payload = _stop(path, last_assistant_message="Fixed the header.")
    assert HOOK.judge_job(payload, CLAUDE_STOP, CATALOGUE)["writer"] == "haiku-fallback"


@pytest.mark.parametrize("tool", list(LAUNCHES))
def test_a_background_launch_counts_as_a_change_the_hook_cannot_see(tmp_path, tool):
    facts = HOOK.judge_job(_launched(tmp_path, tool, LAUNCHES[tool]), HAIKU, CATALOGUE)["facts"]
    assert facts["agent_files"] >= 1
    assert HOOK.grounded("check=targeted task=feature", facts) == "check=targeted task=feature"


def test_the_fallback_skips_a_session_a_scheduled_task_started(tmp_path):
    task = '<scheduled-task name="nightly">\nRun the report.'
    queued = {"type": "queue-operation", "operation": "enqueue", "timestamp": _at(0), "content": task}
    rest = [turn_line(timestamp=_at(1), content=[_said("Report ready.")]),
            user_str_line("Now email it to me", timestamp=_at(30)),
            turn_line(timestamp=_at(31), content=[_said("Emailed.")])]

    def stop(*lines) -> dict:
        return _stop(_transcript(tmp_path, [*lines, *rest]), last_assistant_message="Emailed.")

    # The task is queued first in a current transcript, the first user line in an older one.
    for opening in ([queued, user_str_line(task, timestamp=_at(0), turnOrigin="scheduled")],
                    [user_str_line(task, timestamp=_at(0))]):
        assert HOOK.judge_job(stop(*opening), CLAUDE_STOP, CATALOGUE) is None
        # Where Haiku writes every tag, a reply to your follow-up there is tagged as ever.
        assert HOOK.judge_job(stop(*opening), HAIKU, CATALOGUE)
    # A task mentioned later in a session you began is no scheduled session.
    ordinary = [user_str_line("Add a module", timestamp=_at(0)), queued]
    assert HOOK.judge_job(stop(*ordinary), CLAUDE_STOP, CATALOGUE)["writer"] == "haiku-fallback"


def test_the_worker_marks_a_fallback_line_with_its_writer(tmp_path):
    path = _turn(tmp_path)
    fallback = HOOK.judge_job(_stop(path), CLAUDE_STOP, CATALOGUE)
    record = HOOK.run_judge(tmp_path / "cg", CATALOGUE, fallback, ask=lambda *_: _answer("[cg: task=bugfix brief=clear]"))
    assert record["w"] == "haiku-fallback" and record["tl"] == "task=bugfix brief=clear"
    text = (tmp_path / "cg" / cat.JUDGE_DIR / f"{fallback['ts'][:7]}.jsonl").read_text(encoding="utf-8")
    assert json.loads(text) == record and "wrong sum" not in text

    def no_login(*_args):
        raise HOOK.NoLogin()

    # Marked before the call, so a call that failed is still the fallback's cost.
    failed = HOOK.run_judge(tmp_path / "cg2", CATALOGUE, fallback, ask=no_login)
    assert failed == {"ts": fallback["ts"], "reply": fallback["reply"], "w": "haiku-fallback", "err": "no_login"}
    # The Haiku tagger's lines carry no writer.
    asked = HOOK.judge_job(_stop(path), HAIKU, CATALOGUE)
    assert "w" not in HOOK.run_judge(tmp_path / "cg3", CATALOGUE, asked, ask=lambda *_: _answer())
    assert "w" not in HOOK.run_judge(tmp_path / "cg4", CATALOGUE, asked, ask=no_login)


def test_the_loader_reads_who_asked_and_anything_else_reads_as_the_tagger(tmp_path):
    base = {"ts": "2026-09-27T10:00:00Z", "tl": "task=bugfix"}
    _tag_file(
        tmp_path,
        {**base, "reply": "msg_1", "w": "haiku-fallback", "usd": 0.001},
        {**base, "reply": "msg_2"},
        {**base, "reply": "msg_3", "w": "gpt"},
        {**base, "reply": "msg_4", "w": ["haiku-fallback"]},
        {"ts": "2026-09-27T10:01:00Z", "reply": "msg_5", "w": "haiku-fallback", "err": "timeout"},
        {"ts": "2026-09-27T10:02:00Z", "reply": "msg_6", "agent": "result=done", "w": "haiku-fallback"},
        {**base, "reply": "msg_7", "w": "haiku"},
    )
    loaded = {j.reply: j.writer for j in haiku_tags.load(tmp_path)}
    assert loaded == {"msg_1": "haiku-fallback", "msg_2": "haiku", "msg_3": "haiku", "msg_4": "haiku",
                      "msg_5": "haiku-fallback", "msg_6": "haiku", "msg_7": "haiku"}
    only = haiku_tags.summary(tmp_path, kind="main", writer="haiku-fallback")
    assert (only.calls, only.tagged, only.errors) == (2, 1, {"timeout": 1}) and only.usd == pytest.approx(0.001)
    rest = haiku_tags.summary(tmp_path, kind="main", writer="haiku")
    assert (rest.calls, rest.tagged) == (4, 4)
    assert haiku_tags.summary(tmp_path, kind="main").calls == 6 and haiku_tags.summary(tmp_path).calls == 7


def test_a_fallback_tag_lands_on_the_untagged_reply_and_never_over_claudes_own(tmp_path):
    path = _turn(tmp_path)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    text = cat.note_text(("task",), "main")
    note = attachment_line(
        "hook_additional_context", content=[text], hookName="SessionStart", hookEvent="SessionStart",
        rendered=f"<system-reminder>\nSessionStart hook additional context: {text}\n</system-reminder>",
    )
    note["timestamp"] = _at(0)
    tagged = _transcript(tmp_path, [
        note, user_str_line("hi", timestamp=_at(0)),
        turn_line(timestamp=_at(1), content=[_said("Hello.\n[cg: task=chat]")]),
    ], "t.jsonl")
    own = parse_transcript(tagged, TranscriptMeta(path=str(tagged)))
    config_dir = tmp_path / "cg"
    _tag_file(
        config_dir,
        {"ts": "2026-09-27T10:00:00Z", "reply": result.turns[-1].message_id, "tl": "task=bugfix", "usd": 0.0012,
         "w": "haiku-fallback"},
        {"ts": "2026-09-27T10:00:00Z", "reply": own.turns[-1].message_id, "tl": "task=docs", "usd": 0.0012,
         "w": "haiku-fallback"},
    )
    corpus = NS(sessions=[NS(top=result, subs=[], session_id="a"), NS(top=own, subs=[], session_id="b")])
    assert haiku_tags.apply(corpus, config_dir) == 1
    assert result.turns[-1].cap.task == "bugfix" and result.turns[-1].cap.judged
    assert own.turns[-1].cap.task == "chat" and not own.turns[-1].cap.judged
    # Priced as Haiku's call, as the tagger's would be.
    use = capture.usage(corpus, load_pricing())
    assert use.scopes["haiku"].tag_cost == pytest.approx(0.0012)
    # Counted as tagged, and told apart from the cycle Claude tagged itself.
    assert (use.tagged_cycles, use.filled_cycles) == (2, 1)


def test_capture_status_says_what_haiku_filled_in_while_claude_writes_the_tags(claude_dir):
    set_capture(claude_dir, level="essentials", now=datetime(2026, 9, 1, tzinfo=timezone.utc))
    rc, out = _capture(claude_dir, "status")
    assert "Claude Haiku hasn't had to fill in a tag yet: it does when Claude ends a reply without one." in out
    assert "tags by Haiku" not in out
    # The cost it may add is said, per call, as the tagger's is.
    cost = f"a Claude Haiku call of about ${cat.JUDGE_USD_PER_CALL:.4f} after a reply Claude ends without its tag"
    assert cost in out and "after each of your messages" not in out
    _tag_file(
        claude_dir,
        {"ts": "2026-09-27T10:00:00Z", "reply": "msg_1", "tl": "task=bugfix", "usd": 0.0014, "in": 1200,
         "w": "haiku-fallback"},
        {"ts": "2026-09-27T10:01:00Z", "reply": "msg_2", "err": "timeout", "w": "haiku-fallback"},
        # Not the fallback's: an agent run, and a tag from when Haiku wrote them all.
        {"ts": "2026-09-27T10:02:00Z", "reply": "msg_3", "agent": "result=done", "usd": 0.5, "w": "haiku-fallback"},
        {"ts": "2026-09-27T10:03:00Z", "reply": "msg_4", "tl": "task=docs", "usd": 0.5},
    )
    rc, out = _capture(claude_dir, "status")
    assert "Claude Haiku filled in 1 of the 2 missing tags it was asked about: $0.0014 ($0.0014 a call)" in out
    assert "No tag for 1 (Haiku took too long)" in out and "hasn't had to fill in" not in out
    # Nothing to ask Haiku for at the free level, and the tagger needs no fallback.
    set_capture(claude_dir, level="free")
    out = _capture(claude_dir, "status")[1]
    assert "fill in" not in out and "without its tag" not in out
    set_capture(claude_dir, level="essentials", tagger="haiku")
    assert "fill in" not in _capture(claude_dir, "status")[1]


def test_the_stop_hook_hands_a_fallback_to_the_worker_unless_a_script_is_running_it(tmp_path, monkeypatch):
    config_dir = tmp_path / "cg"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text('[capture]\nlevel = "essentials"\n', encoding="utf-8")
    spawned: list[dict] = []
    monkeypatch.setattr(HOOK, "spawn_judge", lambda folder, job: spawned.append(job))
    untagged = json.dumps(_stop(_turn(tmp_path))).encode("utf-8")
    tagged = json.dumps(_stop(_turn(tmp_path), last_assistant_message="Fixed. [cg: task=bugfix]")).encode("utf-8")

    def stop(entrypoint: str, payload: bytes) -> None:
        monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", entrypoint)
        monkeypatch.setattr(sys, "stdin", NS(buffer=io.BytesIO(payload)))
        HOOK._run(["--config-dir", str(config_dir)])

    stop("claude-desktop", untagged)
    assert [job["writer"] for job in spawned] == ["haiku-fallback"]
    spawned.clear()
    # Claude tagged it, or nobody is at the screen: no call.
    stop("claude-desktop", tagged)
    stop("sdk-cli", untagged)
    assert spawned == []

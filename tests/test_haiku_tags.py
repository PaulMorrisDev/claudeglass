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


def _load_hook():
    spec = importlib.util.spec_from_file_location("_haiku_hook_under_test", SCRIPT)
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


def _turn(tmp_path, *, plan=False, skill="", earlier_plan=False) -> Path:
    """One earlier exchange, then a message Claude answered with a test
    run, an edit and a final reply."""
    first = [tool_use_block("ExitPlanMode", "tp", {"plan": "1. do it"})] if earlier_plan else []
    now = [tool_use_block("Bash", "t1", {"command": "pytest -q tests/test_calc.py\necho done"}),
           tool_use_block("Edit", "t2", {"file_path": "/w/calc.py"})]
    if plan:
        now.append(tool_use_block("ExitPlanMode", "t3", {"plan": "1. fix"}))
    if skill:
        now.append(tool_use_block("Skill", "t4", {"skill": skill}))
    return _transcript(tmp_path, [
        user_str_line("Add a calculator module", timestamp=_at(0)),
        turn_line(timestamp=_at(1), content=[*first, {"type": "text", "text": "Added."}], output_tokens=50),
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
    big = {"hook_event_name": "PostToolUse", "session_id": "s1", "cwd": "/w", "tool_name": "Bash"}
    raw = cat.BIG_OUTPUT_TOKENS * 4
    assert HOOK.note_for(big, config, CATALOGUE, raw_len=raw) == ""
    assert HOOK.note_for(big, {"capture": {"level": "deep"}}, CATALOGUE, raw_len=raw)
    # A subagent's report carries no tag for the word, whoever tags.
    for tagger in ("claude", "haiku"):
        assert HOOK.note_for({**big, "agent_id": "a1"}, {"capture": {"level": "deep", "tagger": tagger}}, CATALOGUE,
                             raw_len=raw) == ""


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
    for metric_id in ("result", "retry", "fit", "agent_brief"):
        assert cat.METRICS_BY_ID[metric_id].sub_line in text
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


def test_the_agent_excerpt_says_what_the_run_did_and_what_came_before(tmp_path):
    session, agent = _agent_run(tmp_path)
    job = HOOK.agent_judge_job(_subagent_stop(session, agent), {"capture": {"level": "standard"}}, CATALOGUE)
    assert job["kind"] == "agent" and job["reply"] == "msg_a2"
    assert job["keys"] == ["result", "retry", "fit", "brief", "missing"]
    assert job["system"] == cat.agent_judge_text(STANDARD)
    excerpt = job["excerpt"]
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


def test_a_workflow_agents_answer_is_its_structured_output_not_its_last_words(tmp_path):
    # A workflow script's agent: the harness frames the brief it computed,
    # and the agent hands its answer back through StructuredOutput after
    # saying what it is about to do.
    framed = ("[Workflow harness — computed task] The task text below was computed at runtime by a workflow "
              "script. It was not typed by this session's user. The computed task text follows:\n"
              "  Review the payments module.\n  Return verdict and findings.")
    agent = _transcript(tmp_path, [
        user_str_line(framed, timestamp=_at(0)),
        turn_line(timestamp=_at(1), model="claude-sonnet-5", message_id="msg_w1", output_tokens=60, content=[
            tool_use_block("Read", "r1", {"file_path": "/w/payments.py"})]),
        {"type": "user", "timestamp": _at(2), "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "r1", "content": "def pay(): ..."}]}},
        turn_line(timestamp=_at(3), model="claude-sonnet-5", message_id="msg_w2", output_tokens=900, content=[
            {"type": "text", "text": "Now let me compile my findings into the structured format."},
            tool_use_block("StructuredOutput", "so1", {"verdict": "sound", "findings": ["refunds skip the audit log"]})]),
        {"type": "user", "timestamp": _at(4), "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "so1", "content": "Structured output provided successfully"}]}},
    ], "agent-w1.jsonl")
    session = _transcript(tmp_path, [user_str_line("Run the review workflow", timestamp=_at(0))], "sess.jsonl")
    payload = _subagent_stop(session, agent, agent_id="w1", agent_type="workflow-subagent",
                             last_assistant_message="Now let me compile my findings into the structured format.")
    excerpt = HOOK.agent_judge_job(payload, {"capture": {"level": "standard"}}, CATALOGUE)["excerpt"]
    assert 'Its brief: "Review the payments module. Return verdict and findings."' in excerpt
    assert "Workflow harness" not in excerpt
    assert 'Its answer, handed back as structured output: "{"verdict": "sound", "findings": ["refunds skip the ' \
        'audit log"]}"' in excerpt
    assert "compile my findings" not in excerpt and "The end of its report" not in excerpt
    # Words after the answer are its report again.
    words = _transcript(tmp_path, [
        user_str_line("Summarise the module", timestamp=_at(0)),
        turn_line(timestamp=_at(1), message_id="msg_x1", content=[
            tool_use_block("StructuredOutput", "so1", {"summary": "draft"})]),
        turn_line(timestamp=_at(2), message_id="msg_x2", content=[{"type": "text", "text": "Done: summary sent."}]),
    ], "agent-x1.jsonl")
    later = HOOK.agent_judge_job(_subagent_stop(session, words, agent_id="x1"), {"capture": {"level": "standard"}},
                                 CATALOGUE)["excerpt"]
    assert 'The end of its report: "Done: summary sent."' in later and "structured output" not in later


@pytest.mark.parametrize("config, extra, env", [
    ({"capture": {"level": "off"}}, {}, {}),
    ({"capture": {"level": "free"}}, {}, {}),
    ({"capture": {"level": "standard"}}, {"stop_hook_active": True}, {}),
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
    job = HOOK.agent_judge_job(_subagent_stop(session, agent), {"capture": {"level": "standard"}}, CATALOGUE)

    def ask(*_args):
        return _answer("[cg: result=partial retry=brief fit=right brief=vague missing=files,constraints rules=used "
                       "task=docs]")

    # "none" only says the run was no retry: it isn't kept.
    none = HOOK.run_judge(tmp_path / "other", CATALOGUE, job, ask=lambda *_: _answer("[cg: result=done retry=none]"))
    assert none["agent"] == "result=done"

    record = HOOK.run_judge(tmp_path / "cg", CATALOGUE, job, ask=ask)
    # Only the agent's own keys and words: no rules, no main-session key,
    # and the main session's list words it doesn't take.
    assert record["agent"] == "result=partial retry=brief fit=right brief=vague missing=files" and "tl" not in record
    loaded = haiku_tags.load(tmp_path / "cg")
    assert [(j.kind, j.result, j.retry, j.tag.fit, j.tag.brief, j.tag.missing) for j in loaded] == [
        ("agent", "partial", "brief", "right", "vague", ("files",))
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
    assert use.answers["result"] == 2 and use.answers["retry"] == 1 and use.answers["fit"] == 1
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
        "echo '{\"result\": \"[cg: result=done fit=smaller brief=clear missing=none]\", \"total_cost_usd\": 0.002}'\n",
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
    for _ in range(100):
        if tags.is_dir() and any(tags.iterdir()):
            break
        time.sleep(0.1)
    line = json.loads(next(tags.iterdir()).read_text(encoding="utf-8"))
    assert line["reply"] == "msg_a2" and line["agent"] == "result=done fit=smaller brief=clear missing=none"


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
    assert job["keys"] == list(cat.tagged_keys(STANDARD)) and job["system"] == cat.judge_text(STANDARD)
    assert job["facts"] == {"plan_now": False, "plan_before": False, "skills": 0, "files": 1, "commands": 1,
                            "earlier": 1, "tests": "targeted", "docs_only": False}


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
        ({"capture": {"level": "standard"}}, {}, False),  # Claude writes the tags
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
    assert record == {
        "ts": job["ts"], "reply": job["reply"], "tl": "task=bugfix brief=clear plan=made skill=none",
        "usd": 0.0014, "in": 1200, "out": 30, "model": "claude-haiku-4-5-20251001",
    }
    text = (tmp_path / "cg" / cat.JUDGE_DIR / f"{job['ts'][:7]}.jsonl").read_text(encoding="utf-8")
    assert json.loads(text) == record
    assert "wrong sum" not in text and "pytest" not in text


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
    job = {"kind": "agent", "ts": "2026-09-27T10:00:00Z", "reply": "msg_1", "keys": ["result"]}

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

"""``hooks/capture-hook.py``, the metrics-capture hook, run the way Claude
Code runs it: a subprocess fed the hook payload on stdin. It must add
exactly the note ``capture_catalogue.note_text`` builds, add nothing
when capture is off, sampled out, past its end, in a skipped project or
on a resume, and never fail or print on bad input.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from importlib import resources
from pathlib import Path

import pytest

from claudeglass import capture_catalogue as cat
from claudeglass import hook_health
from claudeglass.config import CaptureConfig

SCRIPT = Path(str(resources.files("claudeglass") / "hooks" / cat.HOOK_SCRIPT))
MODULE = SCRIPT.with_name(cat.HOOK_MODULE)


def _load_hook_module():
    spec = importlib.util.spec_from_file_location("_capture_note_under_test", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HOOK = _load_hook_module()
CATALOGUE = HOOK.load_catalogue()


def _config(config_dir: Path, body: str) -> Path:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.toml").write_text(body, encoding="utf-8")
    return config_dir


def _run(config_dir: Path, payload, *, script: Path = SCRIPT, env: dict | None = None) -> tuple[int, str, str]:
    data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    done = subprocess.run(
        [sys.executable, str(script), "--config-dir", str(config_dir)],
        input=data,
        capture_output=True,
        timeout=30,
        env=env,
    )
    return done.returncode, done.stdout.decode("utf-8"), done.stderr.decode("utf-8")


def _note(config_dir: Path, payload) -> str:
    rc, out, err = _run(config_dir, payload)
    assert rc == 0 and err == "", err
    if not out:
        return ""
    output = json.loads(out)["hookSpecificOutput"]
    assert output["hookEventName"] == payload["hook_event_name"]
    return output["additionalContext"]


def _start(session_id="s1", **extra) -> dict:
    return {"session_id": session_id, "hook_event_name": "SessionStart", "source": "startup", "cwd": "/work/app", **extra}


def _subagent(session_id="s1", agent_type="general-purpose") -> dict:
    return {"session_id": session_id, "hook_event_name": "SubagentStart", "agent_id": "a1", "agent_type": agent_type}


def _session_id(sample: int, *, inside: bool) -> str:
    for n in range(1000):
        sid = f"session-{n}"
        bucket = int(hashlib.sha256(sid.encode("utf-8")).hexdigest()[:8], 16) % 100
        if (bucket < sample) == inside:
            return sid
    raise AssertionError("no session id found")


# -- the note text matches the catalogue -----------------------------------


@pytest.mark.parametrize("level", cat.LEVELS)
@pytest.mark.parametrize("scope, agent_type", [
    ("main", ""), ("subagent", ""), ("subagent", "Explore"), ("subagent", "Plan"), ("subagent", "statusline-setup"),
])
def test_the_hook_builds_the_same_note_as_the_catalogue(level, scope, agent_type):
    ids = cat.level_metrics(level) + cat.FEEDBACK_IDS
    assert HOOK.build_note(CATALOGUE, ids, scope, agent_type) == cat.note_text(ids, scope, agent_type)


@pytest.mark.parametrize("metric_id", ["big_output", "web"])
def test_the_hook_builds_the_same_tool_note_as_the_catalogue(metric_id):
    assert HOOK.build_tool_note(CATALOGUE, metric_id) == cat.tool_note_text(metric_id)


@pytest.mark.parametrize("capture", [
    CaptureConfig(level="essentials"),
    CaptureConfig(level="deep", feedback=["feedback_note", "feedback_reminder"]),
    CaptureConfig(level="custom", metrics=["task", "result", "fit"]),
    CaptureConfig(level="free", feedback=["feedback_reminder"]),
])
def test_the_hook_switches_on_the_same_metrics_as_the_config(capture):
    table = {"level": capture.level, "metrics": capture.metrics, "feedback": capture.feedback}
    assert HOOK.active_ids(CATALOGUE, table) == capture.active_metrics()


def test_the_slug_matches_discovery():
    from claudeglass import discovery

    for cwd in ("/work/app", r"C:\Dev\claudeglass", "/x/" + "deep/" * 60):
        assert HOOK.slug_for(cwd) == discovery.slug_for(cwd)


# -- when it adds a note ---------------------------------------------------


def test_nothing_is_added_while_capture_is_off(tmp_path):
    assert _note(tmp_path / "none", _start()) == ""
    assert _note(_config(tmp_path / "off", '[capture]\nlevel = "off"\n'), _start()) == ""


def test_a_session_start_gets_the_main_note_and_a_resume_gets_none(tmp_path):
    config_dir = _config(tmp_path, '[capture]\nlevel = "essentials"\n')
    for source in ("startup", "clear", "compact"):
        assert _note(config_dir, _start(source=source)) == cat.note_text(cat.level_metrics("essentials"), "main")
    assert _note(config_dir, _start(source="resume")) == ""


def test_a_subagent_is_asked_for_nothing_at_any_depth(tmp_path):
    # Haiku judges agent runs afterwards: a subagent asked to end its report
    # with a tag broke a JSON-only answer with it.
    config_dir = _config(tmp_path, '[capture]\nlevel = "deep"\n')
    for agent_type in ("general-purpose", "Explore", "Plan", "statusline-setup"):
        assert _note(config_dir, _subagent(agent_type=agent_type)) == ""
    big = {**_subagent(), "hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_response": "x" * 40_000}
    assert _note(config_dir, big) == ""


def test_a_subagent_that_compacts_gets_nothing_and_the_main_session_its_note(tmp_path):
    config_dir = _config(tmp_path, '[capture]\nlevel = "essentials"\n')
    assert _note(config_dir, _start(source="compact", agent_id="a1", agent_type="general-purpose")) == ""
    # Claude Code sends no agent fields for a subagent's compaction; its
    # transcript path gives it away, at any depth (SURV-2: a workflow's own
    # agents sit a level deeper, under the run's id), or its file name alone.
    for path in (
        "/home/u/.claude/projects/p/s1/subagents/agent-a1.jsonl",
        r"C:\Users\u\.claude\projects\p\s1\subagents\agent-a1.jsonl",
        "/home/u/.claude/projects/p/s1/subagents/workflows/wf_1/agent-a1.jsonl",
        r"C:\Users\u\.claude\projects\p\s1\subagents\workflows\wf_1\agent-a1.jsonl",
        "/home/u/.claude/projects/p/s1/somewhere/agent-a1.jsonl",
    ):
        assert _note(config_dir, _start(source="compact", transcript_path=path)) == "", path
    main = _start(source="compact", transcript_path="/home/u/.claude/projects/p/s1.jsonl")
    assert _note(config_dir, main) == cat.note_text(cat.level_metrics("essentials"), "main")


def _session_file(tmp_path: Path, records: list[dict], name: str = "s1.jsonl") -> Path:
    path = tmp_path / "projects" / "p" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def _enqueued(content, **extra) -> dict:
    return {"type": "queue-operation", "operation": "enqueue", "timestamp": "2026-09-27T10:00:00.000Z",
            "sessionId": "s1", "content": content, **extra}


def _user_line(content, **extra) -> dict:
    return {"type": "user", "timestamp": "2026-09-27T10:00:01.000Z", "message": {"role": "user", "content": content},
            **extra}


SCHEDULED = '<scheduled-task name="nightly" file="/home/u/.claude/scheduled-tasks/nightly/SKILL.md">\nRun the report.'


def test_a_scheduled_task_session_gets_no_note(tmp_path):
    """No message of yours opens a cycle there, so every tag Claude wrote
    would be thrown away: the note would cost tokens for nothing."""
    config_dir = _config(tmp_path / "cg", '[capture]\nlevel = "essentials"\n')
    for name, records in {
        # Claude Code queues the task before its first hook runs.
        "queued": [_enqueued(SCHEDULED), {"type": "attachment", "attachment": {"type": "hook_success"}}],
        "queued, leading space": [_enqueued("  " + SCHEDULED)],
        # Older transcripts hold it as the first user line.
        "user line": [_user_line(SCHEDULED)],
        "user blocks": [_user_line([{"type": "text", "text": SCHEDULED}])],
        # A line that isn't a message comes first.
        "after other lines": [{"type": "summary", "summary": "x"}, _user_line("meta", isMeta=True), _enqueued(SCHEDULED)],
    }.items():
        path = _session_file(tmp_path, records)
        for source in ("startup", "clear", "compact"):
            assert _note(config_dir, _start(source=source, transcript_path=str(path))) == "", (name, source)


def test_any_other_session_gets_its_note_whatever_its_transcript_holds(tmp_path):
    config_dir = _config(tmp_path / "cg", '[capture]\nlevel = "essentials"\n')
    note = cat.note_text(cat.level_metrics("essentials"), "main")
    sessions = {
        "typed": [_enqueued("Fix the login bug"), _user_line("Fix the login bug")],
        "a task named later": [_user_line("Fix the login bug"), _enqueued(SCHEDULED)],
        "a task mentioned": [_user_line("What does <scheduled-task> mean?")],
        "empty": [],
    }
    for name, records in sessions.items():
        path = _session_file(tmp_path, records)
        assert _note(config_dir, _start(transcript_path=str(path))) == note, name
    # No transcript to look in, an unreadable one, or no path at all.
    assert _note(config_dir, _start()) == note
    assert _note(config_dir, _start(transcript_path=str(tmp_path / "nowhere.jsonl"))) == note
    assert _note(config_dir, _start(transcript_path=str(tmp_path))) == note
    assert _note(config_dir, _start(transcript_path=7)) == note


def test_the_scheduled_task_check_reads_only_the_first_message_and_keeps_nothing(tmp_path):
    prefix = CATALOGUE["coaching"]["scheduled_task_prefix"]
    assert prefix == cat.SCHEDULED_TASK_PREFIX and prefix in cat.NOT_TYPED_PREFIXES
    path = _session_file(tmp_path, [_enqueued(SCHEDULED)])
    assert HOOK._scheduled_session(str(path), prefix) is True
    assert HOOK._scheduled_session(str(path), "<other") is False
    assert HOOK._scheduled_session(None, prefix) is False and HOOK._scheduled_session("", prefix) is False
    # A first line a dequeue or another kind of line can't stand in for.
    path = _session_file(tmp_path, [{"type": "queue-operation", "operation": "dequeue"}, _user_line("Hi")], "t.jsonl")
    assert HOOK._scheduled_session(str(path), prefix) is False
    # The note hook leaves no file behind that holds the task's words.
    _note(_config(tmp_path / "cg", '[capture]\nlevel = "essentials"\n'),
          _start(transcript_path=str(_session_file(tmp_path, [_enqueued(SCHEDULED)], "u.jsonl"))))
    assert not any("Run the report" in f.read_text(encoding="utf-8", errors="ignore")
                   for f in (tmp_path / "cg").rglob("*") if f.is_file())


# -- whose compaction a SessionStart is ------------------------------------------


def _stamp(ago_s: float = 0.4) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=ago_s)).isoformat().replace("+00:00", "Z")


def _boundary(ago_s: float = 0.4, **extra) -> dict:
    return {"type": "system", "subtype": "compact_boundary", "timestamp": _stamp(ago_s), **extra}


def _compaction(tmp_path: Path) -> tuple[dict, Path]:
    """A SessionStart:compact as Claude Code sends it for a subagent's
    compaction: it names the main session's transcript, with no agent
    field. Returns it and the session's own folder."""
    transcript = tmp_path / "projects" / "p" / "s1.jsonl"
    return _start(source="compact", transcript_path=str(transcript)), transcript.with_suffix("")


def _agent_file(session: Path, records: list[dict], *, name: str = "agent-a1.jsonl", nested: bool = False,
                age_s: float = 0.0) -> Path:
    folder = session / "subagents" / ("workflows/wf_1" if nested else "")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    then = time.time() - age_s
    os.utime(path, (then, then))
    return path


MAIN_NOTE = cat.note_text(cat.level_metrics("essentials"), "main")


@pytest.mark.parametrize("nested", [False, True])
def test_a_compaction_is_a_subagents_when_a_subagent_file_just_recorded_one(tmp_path, nested):
    # The audit: the subagent's boundary is written 0.1 to 0.6 s before the
    # SessionStart; the main session's own is usually not written yet.
    config_dir = _config(tmp_path / "cg", '[capture]\nlevel = "essentials"\n')
    payload, session = _compaction(tmp_path)
    _agent_file(session, [{"type": "assistant", "timestamp": _stamp(30)}, _boundary(0.4)], nested=nested)
    assert HOOK.session_scope(payload, datetime.now(timezone.utc)) == "subagent"
    assert _note(config_dir, payload) == ""


@pytest.mark.parametrize("case", [
    "no subagent folder", "old boundary", "no boundary", "no timestamp", "another record", "from the future",
    "another session", "stale file",
])
def test_any_other_compaction_is_the_main_sessions_and_gets_its_note(tmp_path, case):
    config_dir = _config(tmp_path / "cg", '[capture]\nlevel = "essentials"\n')
    payload, session = _compaction(tmp_path)
    records, where, age_s = {
        "no subagent folder": (None, session, 0),
        "old boundary": ([_boundary(60)], session, 0),
        "no boundary": ([{"type": "assistant", "timestamp": _stamp(0.2)}], session, 0),
        "no timestamp": ([{"type": "system", "subtype": "compact_boundary"}], session, 0),
        "another record": ([{"type": "system", "subtype": "turn_duration", "timestamp": _stamp(0.2)}], session, 0),
        "from the future": ([_boundary(-60)], session, 0),
        "another session": ([_boundary(0.4)], session.with_name("s2"), 0),
        "stale file": ([_boundary(0.4)], session, 60),
    }[case]
    if records is not None:
        _agent_file(where, records, age_s=age_s)
    assert HOOK.session_scope(payload, datetime.now(timezone.utc)) == "main"
    assert _note(config_dir, payload) == MAIN_NOTE


def test_the_main_sessions_own_boundary_neither_decides_nor_is_needed(tmp_path):
    config_dir = _config(tmp_path / "cg", '[capture]\nlevel = "essentials"\n')
    payload, session = _compaction(tmp_path)
    # Not written yet, as in 84 of 93 audited cases: still the main session's.
    assert not Path(payload["transcript_path"]).exists()
    assert _note(config_dir, payload) == MAIN_NOTE
    # Written already: no subagent file recorded one, so it is still the main session's.
    Path(payload["transcript_path"]).parent.mkdir(parents=True)
    Path(payload["transcript_path"]).write_text(json.dumps(_boundary(0.3)) + "\n", encoding="utf-8")
    assert _note(config_dir, payload) == MAIN_NOTE


def test_only_a_compaction_is_checked_for_a_subagent_boundary(tmp_path):
    config_dir = _config(tmp_path / "cg", '[capture]\nlevel = "essentials"\n')
    payload, session = _compaction(tmp_path)
    _agent_file(session, [_boundary(0.4)])
    for source in ("startup", "clear"):
        assert _note(config_dir, {**payload, "source": source}) == MAIN_NOTE


def test_a_subagent_boundary_is_found_past_the_summary_written_after_it(tmp_path):
    payload, session = _compaction(tmp_path)
    summary = {"type": "user", "timestamp": _stamp(0.3), "message": {"role": "user", "content": "x" * 120_000}}
    _agent_file(session, [_boundary(0.4), summary])
    assert HOOK.session_scope(payload, datetime.now(timezone.utc)) == "subagent"


def test_a_subagent_compaction_check_never_fails_on_odd_input(tmp_path):
    now = datetime.now(timezone.utc)
    for payload in ({"source": "compact"}, {"source": "compact", "transcript_path": ""},
                    {"source": "compact", "transcript_path": 7}, {"source": "compact", "transcript_path": "s.jsonl"}):
        assert HOOK.session_scope(payload, now) == "main"
    payload, session = _compaction(tmp_path)
    path = _agent_file(session, [])
    path.write_bytes(b"not json\n\xff\xfe\n[1]\n")
    assert HOOK.session_scope(payload, now) == "main"


def test_the_main_note_tells_a_subagent_to_ignore_it():
    sentence = "If you are a subagent, ignore this note."
    assert sentence in cat.NOTE_INTRO and cat.NOTE_INTRO.endswith(sentence)
    for level in ("essentials", "standard", "deep"):
        ids = cat.level_metrics(level)
        note = cat.note_text(ids, "main")
        assert note.splitlines()[1] == cat.NOTE_INTRO
        assert HOOK.build_note(CATALOGUE, ids, "main") == note


def test_the_session_start_key_names_are_logged_once_and_never_a_value(tmp_path):
    config_dir = _config(tmp_path / "cg", '[capture]\nlevel = "essentials"\n')
    log = config_dir / HOOK.PAYLOAD_KEYS_FILE
    payload = _start(
        transcript_path="/home/u/secret-project/s1.jsonl", model="claude-x",
        **{"a key/with a path": "/home/u/secret-project", "k" * 80: 1},
    )
    _note(config_dir, payload)
    assert json.loads(log.read_text(encoding="utf-8")) == {
        "startup": ["cwd", "hook_event_name", "model", "session_id", "source", "transcript_path"]
    }
    text = log.read_text(encoding="utf-8")
    assert not any(leak in text for leak in ("secret-project", "/work/app", "claude-x", "s1", "with a path", "kkkk"))
    # Once: a later SessionStart of the same kind adds nothing, whatever it carries.
    _note(config_dir, {**payload, "extra_key": 1})
    assert log.read_text(encoding="utf-8") == text
    # Each source and scope is logged on its own.
    _note(config_dir, {**payload, "source": "compact"})
    _note(config_dir, {**payload, "source": "compact", "agent_id": "a1"})
    _note(config_dir, {**payload, "source": "bogus"})
    seen = json.loads(log.read_text(encoding="utf-8"))
    assert set(seen) == {"startup", "compact", "compact:subagent", "other"}
    assert "agent_id" in seen["compact:subagent"] and "agent_id" not in seen["compact"]


def test_the_key_names_of_a_subagents_compaction_are_logged_under_its_own_scope(tmp_path):
    config_dir = _config(tmp_path / "cg", '[capture]\nlevel = "essentials"\n')
    payload, session = _compaction(tmp_path)
    _agent_file(session, [_boundary(0.4)])
    assert _note(config_dir, payload) == ""
    assert set(json.loads((config_dir / HOOK.PAYLOAD_KEYS_FILE).read_text(encoding="utf-8"))) == {"compact:subagent"}


def test_no_key_names_are_logged_where_capture_adds_nothing(tmp_path):
    cases = {
        "off": ('[capture]\nlevel = "off"\ncoaching = ["coaching_notes"]\n', _start()),
        "past its end": ('[capture]\nlevel = "essentials"\nuntil = "2020-01-01T00:00:00Z"\n', _start()),
        "skipped project": ('[capture]\nlevel = "essentials"\nprojects = ["!app"]\n', _start()),
        "sampled out": ('[capture]\nlevel = "essentials"\nsample = 10\n', _start(_session_id(10, inside=False))),
    }
    for name, (body, payload) in cases.items():
        config_dir = _config(tmp_path / name, body)
        _note(config_dir, payload)
        assert not (config_dir / HOOK.PAYLOAD_KEYS_FILE).exists(), name
    # Only a SessionStart is logged.
    config_dir = _config(tmp_path / "other", '[capture]\nlevel = "essentials"\n')
    _note(config_dir, _subagent())
    assert not (config_dir / HOOK.PAYLOAD_KEYS_FILE).exists()


def test_a_hook_that_can_return_output_is_never_a_background_signal():
    # An async hook's output only arrives on the next turn; the signal events
    # are the ones registered async, so none of them may print a note.
    assert not set(cat.OUTPUT_EVENTS) & set(CATALOGUE["signal_events"])
    assert set(cat.OUTPUT_EVENTS) >= {"SessionStart", "UserPromptSubmit", "PostToolUse"}


def test_sampling_keeps_a_session_in_or_out_for_its_whole_length(tmp_path):
    config_dir = _config(tmp_path, '[capture]\nlevel = "essentials"\nsample = 10\n')
    inside, outside = _session_id(10, inside=True), _session_id(10, inside=False)
    assert _note(config_dir, _start(inside))
    assert _note(config_dir, _start(outside)) == ""
    # Its agent runs follow it: judged only inside the sample.
    config = {"capture": {"level": "essentials", "sample": 10}}
    assert HOOK._capture_for({**_subagent(inside), "cwd": "/w"}, config, datetime.now(timezone.utc)) is not None
    assert HOOK._capture_for({**_subagent(outside), "cwd": "/w"}, config, datetime.now(timezone.utc)) is None


def test_a_skipped_project_gets_nothing(tmp_path):
    only = _config(tmp_path / "only", '[capture]\nlevel = "essentials"\nprojects = ["client-a"]\n')
    assert _note(only, _start(cwd="/work/client-a/api"))
    assert _note(only, _start(cwd="/work/personal")) == ""
    skip = _config(tmp_path / "skip", '[capture]\nlevel = "essentials"\nprojects = ["!secret"]\n')
    assert _note(skip, _start(cwd="/work/Secret-Thing")) == ""
    assert _note(skip, _start(cwd="/work/app"))
    left_out = _config(tmp_path / "left", 'exclude_projects = ["scratch"]\n\n[capture]\nlevel = "essentials"\n')
    assert _note(left_out, _start(cwd="/tmp/scratch")) == ""


def test_the_project_is_the_folder_the_session_started_in(monkeypatch):
    """The payload's ``cwd`` follows the shell: a session that ran ``cd``
    into a worktree or another folder is still its project's session."""
    config = {"capture": {"level": "essentials", "projects": ["client-a"]}}
    now = datetime.now(timezone.utc)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/work/client-a")
    assert HOOK._capture_for(_start(cwd="/tmp/elsewhere"), config, now) is not None
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/work/personal")
    assert HOOK._capture_for(_start(cwd="/work/client-a/.claude/worktrees/w1"), config, now) is None
    monkeypatch.delenv("CLAUDE_PROJECT_DIR")
    assert HOOK._capture_for(_start(cwd="/work/client-a/api"), config, now) is not None


def test_the_project_keeps_the_payloads_spelling_inside_it(monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", os.path.normcase("/work/App"))
    assert HOOK.project_dir({"cwd": "/work/App/sub"}) == "/work/App"
    assert HOOK.project_dir({"cwd": "/work/Apple"}) == os.path.normcase("/work/App")
    assert HOOK.project_dir({}) == os.path.normcase("/work/App")


def test_a_bad_pattern_is_skipped_and_the_rest_still_apply(tmp_path):
    """SEC-P5: ``config.toml`` isn't only ever written by ClaudeGlass's own
    validated ``write_config_values`` -- it can be hand-edited, or come
    from an older version -- so the hook must not let one unparseable
    regex (an unbalanced paren, say) take the whole call down with it
    (``main`` catches everything and exits 0 regardless, but that used to
    mean no note for the *whole* session, not just a miss on the one bad
    pattern). A good ``exclude_projects`` pattern alongside the bad one
    must still exclude its project, and a project the bad pattern doesn't
    (and can't validly) match must still get its note.
    """
    config_dir = _config(
        tmp_path,
        'exclude_projects = ["scratch", "(unbalanced"]\n\n[capture]\nlevel = "essentials"\n',
    )
    assert _note(config_dir, _start(cwd="/tmp/scratch")) == ""
    assert _note(config_dir, _start(cwd="/work/app"))


def test_project_allowed_treats_a_bad_pattern_as_a_non_match():
    bad = "(unbalanced"
    # A bad exclude pattern never excludes (fails open on that one entry,
    # not closed) but a good one alongside it still does.
    assert HOOK.project_allowed("app", [], ["scratch", bad]) is True
    assert HOOK.project_allowed("scratch", [], ["scratch", bad]) is False
    # Same for a bad !-exclude inside ``projects``.
    assert HOOK.project_allowed("secret", [f"!{bad}"], []) is True
    # And for a bad plain include: it just never matches, same as any
    # other pattern that doesn't -- an unrelated good include still works.
    assert HOOK.project_allowed("client-a", [bad, "client-a"], []) is True
    assert HOOK.project_allowed("client-b", [bad], []) is False


def test_capture_past_its_end_adds_nothing(tmp_path):
    past = _config(tmp_path / "past", '[capture]\nlevel = "essentials"\nuntil = "2020-01-01T00:00:00+00:00"\n')
    assert _note(past, _start()) == ""
    future = _config(tmp_path / "future", '[capture]\nlevel = "essentials"\nuntil = "2999-01-01"\n')
    assert _note(future, _start())


def test_a_large_tool_result_gets_a_one_line_note_at_deep(tmp_path):
    deep = _config(tmp_path / "deep", '[capture]\nlevel = "deep"\n')
    threshold = cat.BIG_OUTPUT_TOKENS * 4
    small = {"session_id": "s1", "hook_event_name": "PostToolUse", "tool_name": "Read",
             "tool_response": {"type": "text", "file": {"content": "ok"}}}
    big = {**small, "tool_response": {"type": "text", "file": {"content": "x" * threshold}}}
    assert _note(deep, small) == ""
    assert _note(deep, big) == cat.tool_note_text("big_output")
    web = {**small, "tool_name": "WebFetch"}
    assert _note(deep, web) == cat.tool_note_text("web")
    standard = _config(tmp_path / "standard", '[capture]\nlevel = "standard"\n')
    assert _note(standard, big) == ""


def test_a_picture_counts_for_its_tokens_not_its_encoding_in_the_big_output_note(tmp_path):
    """A screenshot's payload is mostly its encoding, not words Claude read:
    it counts for at most its tokens, and the words beside it still count."""
    deep = _config(tmp_path / "deep", '[capture]\nlevel = "deep"\n')
    threshold = cat.BIG_OUTPUT_TOKENS * 4
    base = {"session_id": "s1", "hook_event_name": "PostToolUse", "tool_name": "Read"}
    sized = {"base64": "A" * threshold, "dimensions": {"displayWidth": 1568, "displayHeight": 1000}}
    picture = {"type": "image", "source": {"type": "base64", "data": "A" * threshold}}
    for response in (
        {"type": "image", "file": sized},
        [{"type": "text", "text": "shot"}, picture],
        {"content": [{"type": "text", "text": "x"}, picture]},
    ):
        assert _note(deep, {**base, "tool_response": response}) == "", response
    # Words just under the threshold are pushed over it by the picture.
    words = [{"type": "text", "text": "x" * (threshold - 100)}]
    assert _note(deep, {**base, "tool_response": words}) == ""
    assert _note(deep, {**base, "tool_response": [*words, picture]}) == cat.tool_note_text("big_output")


def test_a_web_search_counts_its_hits_and_summary_but_not_its_query_or_timing():
    """A WebSearch result lists its hits under ``results`` (each a titled
    link), with a summary string beside them. The query and the time it
    took are for Claude Code's display, not words Claude read."""
    response = {
        "query": "a long search query that is not read back",
        "results": [{"tool_use_id": "x", "content": [{"title": "tt", "url": "uu"}]}, "summary"],
        "durationSeconds": 1.5,
    }
    payload = {"hook_event_name": "PostToolUse", "tool_name": "WebSearch", "tool_response": response}
    assert HOOK.result_chars(payload, CATALOGUE) == len("tt") + len("uu") + len("summary") == 11
    other = {**response, "query": "q", "durationSeconds": 99.0}
    assert HOOK.result_chars({**payload, "tool_response": other}, CATALOGUE) == 11


def test_a_large_web_search_gets_the_big_output_note(tmp_path):
    deep = _config(tmp_path / "deep", '[capture]\nlevel = "deep"\n')
    threshold = cat.BIG_OUTPUT_TOKENS * 4
    hit = {"title": "t" * threshold, "url": "https://example.com"}
    call = {"session_id": "s1", "hook_event_name": "PostToolUse", "tool_name": "WebSearch"}
    big = {**call, "tool_response": {"query": "q", "results": [{"tool_use_id": "x", "content": [hit]}], "durationSeconds": 1}}
    small = {**call, "tool_response": {"query": "q", "results": ["summary"], "durationSeconds": 1}}
    assert _note(deep, big) == cat.tool_note_text("big_output")
    assert _note(deep, small) == ""


def test_a_large_result_saved_to_a_file_gets_no_big_output_note(tmp_path):
    deep = _config(tmp_path / "deep", '[capture]\nlevel = "deep"\n')
    threshold = cat.BIG_OUTPUT_TOKENS * 4
    base = {"session_id": "s1", "hook_event_name": "PostToolUse"}
    kept = {**base, "tool_name": "WebFetch", "tool_response": {"result": "r" * (threshold + 100)}}
    saved = {**base, "tool_name": "WebFetch", "tool_response": {"result": "r" * (cat.RESULT_PERSIST_CHARS["WebFetch"] + 1)}}
    assert _note(deep, kept) == cat.tool_note_text("big_output")
    assert _note(deep, saved) == ""


def test_the_shell_and_mcp_tools_get_nothing_whatever_the_matcher_says(tmp_path):
    """A settings.json written before they were dropped from the matcher
    still runs the hook after them. Nothing is added, and nothing is kept."""
    config_dir = _config(tmp_path / "cg", '[capture]\nlevel = "deep"\ncoaching = ["coaching_notes"]\n')
    big = {"session_id": "s1", "cwd": "/w", "hook_event_name": "PostToolUse"}
    shell = {**big, "tool_name": "Bash", "tool_response": {"stdout": "x" * 40_000, "stderr": ""}}
    mcp = {**big, "tool_name": "mcp__docs__search", "tool_response": [{"type": "text", "text": "x" * 40_000}]}
    for payload in (shell, mcp, {**shell, "tool_name": "PowerShell"}):
        assert _run(config_dir, payload) == (0, "", ""), payload["tool_name"]
    assert not (config_dir / cat.COACH_STATE_FILE).exists()
    watched = {**big, "tool_name": "Read", "tool_response": {"type": "text", "file": {"content": "x" * 40_000}}}
    rc, out, _ = _run(config_dir, watched)
    assert rc == 0 and cat.tool_note_text("big_output") in json.loads(out)["hookSpecificOutput"]["additionalContext"]


def test_a_call_for_an_unwatched_tool_is_known_before_the_config_is_read():
    unwatched = lambda payload: HOOK._unwatched(json.dumps(payload))
    call = {"hook_event_name": "PostToolUse", "session_id": "s1"}
    assert unwatched({**call, "tool_name": "Bash"}) and unwatched({**call, "tool_name": "mcp__a__b"})
    assert unwatched(call)
    for tool in cat.COACHING_TOOLS:
        assert not unwatched({**call, "tool_name": tool}), tool
    # Another event is never skipped, whatever it says, nor anything it can't read.
    assert not unwatched({"hook_event_name": "UserPromptSubmit", "prompt": "PostToolUse", "tool_name": "Bash"})
    assert not unwatched({"hook_event_name": "SessionStart", "source": "PostToolUse"})
    for raw in ("", "PostToolUse", "{PostToolUse", "[1, 2]", '"PostToolUse"'):
        assert not HOOK._unwatched(raw), raw


def test_the_size_is_checked_before_anything_else_is_worked_out():
    """Under the threshold a PostToolUse note can't apply, so the sampling
    hash, the project check and the metric list aren't run."""
    def boom(*args, **kwargs):
        raise AssertionError("worked out for a small result")

    config = {"capture": {"level": "deep"}}
    call = {"hook_event_name": "PostToolUse", "session_id": "s1", "tool_name": "Read",
            "tool_response": {"type": "text", "file": {"content": "ok"}}}
    original = HOOK._capture_for
    HOOK._capture_for = boom
    try:
        assert HOOK.note_for(call, config, CATALOGUE) == ""
        assert HOOK.note_for(call, config, CATALOGUE, result_len=10) == ""
    finally:
        HOOK._capture_for = original
    assert HOOK.note_for(call, config, CATALOGUE, result_len=cat.BIG_OUTPUT_TOKENS * 4) == cat.tool_note_text("big_output")


@pytest.mark.parametrize("argv, parsed", [
    ([], (None, False)),
    (["--config-dir", "/x/cg"], ("/x/cg", False)),
    (["--config-dir=/x/cg"], ("/x/cg", False)),
    (["--judge", "--config-dir", "/x/cg"], ("/x/cg", True)),
    (["--config-dir"], None),
    (["--config-dir", "/x", "--nope"], None),
    (["stray"], None),
])
def test_the_command_line_is_read_without_argparse(argv, parsed):
    assert HOOK._parse_args(argv) == parsed


def test_the_hook_imports_nothing_a_call_may_not_need():
    """``argparse``, ``hashlib``, ``hmac``, ``sqlite3``, ``subprocess`` and
    ``tomllib`` cost 4 to 17 ms each to import, on every call: they are
    imported where they are used."""
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); import capture_hook; "
        "print(sorted(m for m in ('argparse', 'hashlib', 'hmac', 'sqlite3', 'subprocess', 'tomllib') if m in sys.modules))"
    )
    done = subprocess.run([sys.executable, "-I", "-S", "-c", code, str(MODULE.parent)], capture_output=True, timeout=30)
    assert done.stdout.decode("utf-8").strip() == "[]", done.stderr.decode("utf-8")


def test_the_feedback_reminder_is_not_in_the_session_start_note(tmp_path):
    # It comes later, once a piece of work is big enough (the next message's note), not with every session.
    free = _config(tmp_path / "free", '[capture]\nlevel = "free"\nfeedback = ["feedback_reminder"]\n')
    note = _note(free, _start())
    assert "/cg-feedback" not in note and "feedback_reminder" not in note
    off = _config(tmp_path / "off", '[capture]\nlevel = "off"\nfeedback = ["feedback_reminder"]\n')
    assert _note(off, _start()) == ""


def test_a_run_with_nobody_at_the_screen_gets_no_note_but_still_logs_signals(tmp_path):
    # claude -p and the Agent SDK: a script reads what the run prints, so
    # nothing may be added to it. The free signal lines still count.
    config_dir = _config(tmp_path / "cg", '[capture]\nlevel = "deep"\ncoaching = ["coaching_notes"]\n')
    (config_dir / "salt").write_bytes(b"s" * 32)
    for entrypoint in ("sdk-cli", "sdk-ts", "sdk-py"):
        env = {**os.environ, "CLAUDE_CODE_ENTRYPOINT": entrypoint}
        for payload in (_start(), _subagent(), {**_start(), "hook_event_name": "UserPromptSubmit", "prompt": "hi"},
                        {**_start(), "hook_event_name": "PostToolUse", "tool_name": "Read",
                         "tool_response": {"type": "text", "file": {"content": "x" * 40_000}}}):
            assert _run(config_dir, payload, env=env) == (0, "", ""), (entrypoint, payload["hook_event_name"])
        rc, out, _ = _run(config_dir, {**_start(), "hook_event_name": "SessionEnd", "reason": "other"}, env=env)
        assert rc == 0 and out == ""
    assert len(list((config_dir / "signals").glob("*.jsonl"))) == 1
    # The terminal and the desktop app still get the note.
    for entrypoint in ("cli", "claude-desktop"):
        env = {**os.environ, "CLAUDE_CODE_ENTRYPOINT": entrypoint}
        rc, out, _ = _run(config_dir, _start(), env=env)
        assert rc == 0 and json.loads(out)["hookSpecificOutput"]["additionalContext"].startswith(cat.NOTE_MARKER)


def _stopped_session(tmp_path: Path) -> dict:
    """A ``Stop`` payload whose transcript ends on a reply that left 150k tokens."""
    reply = {"type": "assistant", "timestamp": "2026-09-25T10:00:00.000Z", "message": {
        "id": "m1", "content": [{"type": "text", "text": "ok"}],
        "usage": {"input_tokens": 10, "cache_read_input_tokens": 149_990, "cache_creation_input_tokens": 0,
                  "output_tokens": 0}}}
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(json.dumps(reply) + "\n", encoding="utf-8")
    return {**_start(), "hook_event_name": "Stop", "transcript_path": str(transcript)}


def test_a_stop_keeps_the_newest_reply_and_prints_nothing(tmp_path):
    # The cold-return receipt needs the real last reply's time: a replay can move the transcript's end.
    config_dir = _config(tmp_path / "cg", '[capture]\nlevel = "deep"\ncoaching = ["coaching_notes"]\n')
    (config_dir / "salt").write_bytes(b"s" * 32)
    env = {key: value for key, value in os.environ.items() if key != "CLAUDE_CODE_ENTRYPOINT"}
    assert _run(config_dir, _stopped_session(tmp_path), env=env) == (0, "", "")
    state = json.loads((config_dir / cat.COACH_STATE_FILE).read_text(encoding="utf-8"))
    (row,) = state["sessions"].values()
    assert row["last"] == {"at": datetime(2026, 9, 25, 10, tzinfo=timezone.utc).timestamp(), "ctx": 150_000, "ttl": 300}


def test_a_stop_keeps_nothing_without_coaching_notes_or_with_nobody_at_the_screen(tmp_path):
    stop = _stopped_session(tmp_path)
    plain = _config(tmp_path / "plain", '[capture]\nlevel = "deep"\n')
    assert _run(plain, stop) == (0, "", "")
    assert not (plain / cat.COACH_STATE_FILE).exists()
    headless = _config(tmp_path / "headless", '[capture]\nlevel = "deep"\ncoaching = ["coaching_notes"]\n')
    assert _run(headless, stop, env={**os.environ, "CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}) == (0, "", "")
    assert not (headless / cat.COACH_STATE_FILE).exists()


@pytest.mark.parametrize("payload", [b"", b"not json", b"[1, 2]", b"\xff\xfe"])
def test_bad_input_prints_nothing_and_exits_zero(tmp_path, payload):
    config_dir = _config(tmp_path, '[capture]\nlevel = "essentials"\n')
    assert _run(config_dir, payload) == (0, "", "")


def test_a_half_written_config_reads_as_off(tmp_path):
    config_dir = _config(tmp_path, '[capture]\nlevel = "essen')
    assert _run(config_dir, json.dumps(_start()).encode("utf-8")) == (0, "", "")


def test_an_installed_copy_runs_from_the_data_folder(tmp_path):
    config_dir = _config(tmp_path / "claudeglass", '[capture]\nlevel = "essentials"\n')
    written = hook_health.install_hook_files(config_dir, hook_health.CAPTURE_FILES[cat.HOOK_SCRIPT])
    # The launcher goes in last: it is the file that starts using the others.
    assert [p.name for p in written] == [cat.CATALOGUE_FILE, cat.HOOK_MODULE, cat.HOOK_SCRIPT]
    rc, out, _ = _run(config_dir, _start(), script=written[-1])
    assert rc == 0 and json.loads(out)["hookSpecificOutput"]["additionalContext"].startswith(cat.NOTE_MARKER)


def _isolated(config_dir: Path, script: Path, payload: dict) -> tuple[int, str, str]:
    """Run the hook the way Claude Code does: ``python -I -S script``."""
    done = subprocess.run(
        [sys.executable, "-I", "-S", str(script), "--config-dir", str(config_dir)],
        input=json.dumps(payload).encode("utf-8"),
        capture_output=True,
        timeout=30,
    )
    return done.returncode, done.stdout.decode("utf-8"), done.stderr.decode("utf-8")


def test_the_launcher_imports_its_module_under_isolated_python_and_the_bytecode_is_kept(tmp_path):
    # -I leaves the script's folder off the import path: the launcher puts it back.
    config_dir = _config(tmp_path / "claudeglass", '[capture]\nlevel = "essentials"\n')
    launcher = hook_health.install_hook_files(config_dir, hook_health.CAPTURE_FILES[cat.HOOK_SCRIPT])[-1]
    cache = launcher.parent / "__pycache__"
    assert not cache.exists()
    rc, out, err = _isolated(config_dir, launcher, _start())
    assert (rc, err) == (0, "") and json.loads(out)["hookSpecificOutput"]["additionalContext"].startswith(cat.NOTE_MARKER)
    # Python keeps no bytecode for a script it is started on, only for a module it imports.
    assert [p.name.split(".")[0] for p in cache.glob("*.pyc")] == ["capture_hook"]
    assert _isolated(config_dir, launcher, _start()) == (rc, out, err)


def test_a_launcher_with_no_module_or_a_broken_one_does_nothing_and_exits_zero(tmp_path):
    config_dir = _config(tmp_path / "claudeglass", '[capture]\nlevel = "essentials"\n')
    launcher = hook_health.install_hook_files(config_dir, hook_health.CAPTURE_FILES[cat.HOOK_SCRIPT])[-1]
    module = launcher.with_name(cat.HOOK_MODULE)
    module.unlink()
    assert _isolated(config_dir, launcher, _start()) == (0, "", "")
    module.write_text("def main(:\n", encoding="utf-8")
    assert _isolated(config_dir, launcher, _start()) == (0, "", "")
    module.write_text("raise RuntimeError('broken')\n", encoding="utf-8")
    assert _isolated(config_dir, launcher, _start()) == (0, "", "")


def test_a_real_session_start_note_is_read_back():
    """Lines as Claude Code 2.1.280 wrote them in a headless run with the
    Essentials hook (paths and ids replaced; the reply stands in for the
    real one): ``rendered`` is a top-level list, and the hook_success
    line's copy of the note in ``stdout`` is not counted again."""
    from claudeglass.model import TranscriptMeta
    from claudeglass.parse import parse_transcript

    path = Path(__file__).parent / "fixtures" / "capture" / "session-start-note.jsonl"
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rendered = next(line["rendered"][0]["content"] for line in lines if "rendered" in line)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    assert (result.meta.cap_version, result.meta.cap_injections) == (cat.NOTE_VERSION, 1)
    assert result.meta.cap_metrics == ("task", "brief", "level", "shift", "retry")
    assert [turn.cap_note_chars for turn in result.turns] == [len(rendered)]
    assert (result.turns[0].cap.task, result.turns[0].cap.level) == ("research", "easy")
    # Recorded before Essentials carried size, before 0.9.0 renamed the
    # tool, before 0.11.0 moved retry from the brief to Haiku, before the
    # note told a subagent to ignore it, and before shift carried why and
    # admit and the note ended on the sentences about plans and agent
    # reports: today's note for the metrics it names, plus the retry line
    # it had then.
    old = rendered.replace("Token Lens", "ClaudeGlass").replace("shift,retry", "shift")
    old = old.replace("where their tokens go.", "where their tokens go. If you are a subagent, ignore this note.")
    recorded_shift = (
        "shift: new|build|grew|redo|fix, only if it applies (a new unrelated task; building on the last one; "
        "the scope grew; redoing earlier work; fixing a fault in it)"
    )
    assert recorded_shift in old and "Leave out a key you can't judge.\n" in old
    old = old.replace(recorded_shift, cat.METRICS_BY_ID["shift"].main_line)
    old = old.replace("Leave out a key you can't judge.", cat.SKIP_KEY_LINE)
    old = old.splitlines()
    retry_line = "When you start an agent again because its last run fell short, begin the brief with [retry: model|brief|tools|scope|other]."
    assert old.pop(-2) == retry_line
    now = f"<system-reminder>\nSessionStart hook additional context: {cat.note_text(result.meta.cap_metrics, 'main')}\n</system-reminder>"
    assert old == now.splitlines()


# -- the /cg-feedback survey: facts line, plan check, rating reminder ----------------

@pytest.fixture(autouse=True)
def _in_a_terminal(monkeypatch):
    """Tips count as shown by their notes, which the desktop app would not do (``CLAUDE_CODE_ENTRYPOINT``)."""
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)


FB_NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
FB_ALL = {"capture": {"level": "off", "feedback": list(cat.FEEDBACK_MESSAGE_IDS)}}
FB_MARKER = CATALOGUE["coaching"]["feedback"]["facts_marker"]
REMINDER_TEXT = f"{cat.REMINDER_LABEL} {cat.FEEDBACK_REMINDER_LINE}"
_fb_ids = iter(range(1_000_000))


def _fb_iso(ago_s: float, now: datetime = FB_NOW) -> str:
    return (now - timedelta(seconds=ago_s)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _timeline(*records: dict, gap: float = 20.0, now: datetime = FB_NOW) -> list[dict]:
    """``records`` (not yet in a timeline) one after another, ``gap``
    seconds apart, the last 50 seconds before ``now``."""
    count = len(records)
    out = []
    for i, record in enumerate(records):
        line = dict(record)
        line.setdefault("timestamp", _fb_iso(gap * (count - 1 - i) + 50, now))
        line.setdefault("uuid", f"fb-{next(_fb_ids)}")
        out.append(line)
    return out


def _says(text: str = "ok", *, tokens: int = 1000, content=None) -> dict:
    return {
        "type": "assistant",
        "message": {
            "id": f"msg_{next(_fb_ids)}",
            "usage": {"input_tokens": tokens, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "output_tokens": 0},
            "content": content or [{"type": "text", "text": text}],
        },
    }


def _types(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}}


def _calls(name: str, call_id: str, tool_input: dict, *, tokens: int = 1000) -> dict:
    return _says(tokens=tokens, content=[{"type": "tool_use", "id": call_id, "name": name, "input": tool_input}])


def _comes_back(call_id: str, *, error: bool = False, text: str = "ok", result=None) -> dict:
    block = {"type": "tool_result", "tool_use_id": call_id, "content": text, **({"is_error": True} if error else {})}
    return {"type": "user", "toolUseResult": result if result is not None else {}, "message": {"role": "user", "content": [block]}}


def _plans(plan_id: str, *, tokens: int = 1000) -> dict:
    return _calls("ExitPlanMode", plan_id, {"plan": "Step one. Step two. Step three."}, tokens=tokens)


def _approved(plan_id: str) -> dict:
    return _comes_back(plan_id)


def _edits(call_id: str = "e1", *, tokens: int = 1000) -> list[dict]:
    return [_calls("Edit", call_id, {"file_path": "src/app.py", "old_string": "a", "new_string": "b"}, tokens=tokens),
            _comes_back(call_id)]


def _noted(hint: str) -> dict:
    return {
        "type": "attachment",
        "attachment": {
            "type": "hook_additional_context", "hookEvent": "UserPromptSubmit",
            "content": [f"{cat.COACH_MARKER}{cat.COACH_VERSION} {hint}\nsome note"],
        },
    }


def _queued_line(text: str) -> dict:
    return {
        "type": "attachment",
        "attachment": {"type": "queued_command", "commandMode": "prompt", "prompt": text, "origin": {"kind": "human"}},
    }


def _ran_feedback() -> dict:
    return _types("<command-message>cg-feedback</command-message>\n<command-name>/cg-feedback</command-name>")


def _plan_check_asked(call_id: str, outcome: str) -> list[dict]:
    """The plan check's question and how it came back: ``declined``, ``answered`` or ``other``."""
    question = {"question": cat.PLAN_CHECK_QUESTION, "header": cat.PLAN_CHECK_HEADER}
    ask = _calls("AskUserQuestion", call_id, {"questions": [question]})
    if outcome == "declined":
        return [ask, _comes_back(call_id, error=True, text="User rejected tool use")]
    answer = cat.PLAN_CHECK_OPTIONS[1][1] if outcome == "answered" else "my own words"
    result = {"questions": [question], "answers": {cat.PLAN_CHECK_QUESTION: answer}}
    return [ask, _comes_back(call_id, result=result)]


def _prompt_for(tmp_path: Path, records: list[dict], prompt: str, *, session: str = "s1") -> dict:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / f"{session}.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return {
        "hook_event_name": "UserPromptSubmit", "session_id": session, "cwd": "/work/app",
        "transcript_path": str(path), "prompt": prompt,
    }


def _fb(tmp_path: Path, records: list[dict], prompt: str, *, config=FB_ALL, now=FB_NOW, tipped=False, session="s1") -> str:
    config_dir = tmp_path / "claudeglass"
    config_dir.mkdir(parents=True, exist_ok=True)
    payload = _prompt_for(tmp_path, records, prompt, session=session)
    return HOOK.feedback_note_for(payload, config, CATALOGUE, config_dir, now=now, tipped=tipped)


def _facts(note: str) -> dict:
    assert note.startswith(FB_MARKER + " "), note
    pairs = [part.split("=", 1) for part in note[len(FB_MARKER):].split()]
    return {key: int(value) if value.isdigit() else value for key, value in pairs}


def _hint(note: str) -> str:
    assert note.startswith(cat.COACH_MARKER + str(cat.COACH_VERSION) + " "), note
    return note.split("\n", 1)[0].rsplit(" ", 1)[1]


FIX = "That's wrong, it still fails on empty files."
WORK = [_types("Plan the importer"), _says("Here is the plan."), _plans("p1"), _approved("p1"), *_edits(), _says("Built it.")]


def test_the_hook_holds_the_same_feedback_ids_as_the_catalogue():
    assert HOOK._FEEDBACK_MESSAGE_IDS == cat.FEEDBACK_MESSAGE_IDS
    assert tuple(CATALOGUE["coaching"]["feedback"]["message_ids"]) == cat.FEEDBACK_MESSAGE_IDS
    assert HOOK.feedback_ids({"capture": {"feedback": ["plan_check", "feedback_note", "feedback_skill"]}}) == (
        "feedback_skill", "plan_check",
    )
    for config in ({}, {"capture": {}}, {"capture": {"feedback": "plan_check"}}, {"capture": []}):
        assert HOOK.feedback_ids(config) == ()


def test_a_feedback_run_starts_with_the_facts_line_of_its_piece(tmp_path):
    records = _timeline(
        _types("Plan the importer"),
        _says("Here is the plan.", tokens=1000),
        _plans("p1", tokens=2000),
        _approved("p1"),
        *_edits(tokens=3000),
        _types("rename the loader to reader"),
        _noted("drip_feed"),
        _says("Done.\n> **ClaudeGlass tip:** send bigger asks", tokens=4000),
        _queued_line("also tweak the docs"),
        _types("go ahead"),
        _types("how is it going?"),
        _types(FIX),
        _says("You're right, my mistake.", tokens=5000),
    )
    note = _fb(tmp_path, records, "/cg-feedback")
    assert note == (
        f"{FB_MARKER} tokens=15000 typical=0 followups=3 queued=1 plan=approved plan_followups=3 plan_asked=0 "
        "build=same tips=drip_feed:1 tip=drip_feed admits=1"
    )
    assert list(_facts(note)) == list(CATALOGUE["coaching"]["feedback"]["fact_keys"])
    # With arguments too, and not for another command.
    assert _fb(tmp_path, records, "/cg-feedback now").startswith(FB_MARKER)
    assert _fb(tmp_path, records, "/cg-feedbacks") == ""
    assert _fb(tmp_path, records, "cg-feedback") == ""


def test_the_facts_line_without_a_plan_or_a_tip_says_none(tmp_path):
    records = _timeline(_types("Add a flag"), _says(tokens=500), *_edits(tokens=700), _says(tokens=300))
    assert _facts(_fb(tmp_path, records, "/cg-feedback")) == {
        "tokens": 1500, "typical": 0, "followups": 0, "queued": 0, "plan": "none", "plan_followups": 0,
        "plan_asked": 0, "build": "none", "tips": "none", "tip": "none", "admits": 0,
    }


def test_the_facts_line_calls_a_plan_pending_until_it_is_approved(tmp_path):
    rejected = _comes_back("p1", error=True, text="The user doesn't want to proceed with this tool use.")
    records = _timeline(_types("Plan it"), _plans("p1"), rejected, _says("Plan sent back."))
    facts = _facts(_fb(tmp_path, records, "/cg-feedback"))
    assert (facts["plan"], facts["build"], facts["plan_followups"]) == ("pending", "none", 0)


def test_a_failed_edit_is_no_build(tmp_path):
    failed = [_calls("Edit", "e9", {"file_path": "src/app.py"}), _comes_back("e9", error=True, text="no match")]
    records = _timeline(_types("Plan it"), _plans("p1"), _approved("p1"), *failed, _says("It failed."))
    assert _facts(_fb(tmp_path, records, "/cg-feedback"))["build"] == "none"


def test_a_go_ahead_that_approves_a_plan_counts_as_the_approval(tmp_path):
    rejected = _comes_back("p1", error=True, text="The user doesn't want to proceed with this tool use.")
    records = _timeline(
        _types("Plan it"), _plans("p1"), rejected, _says("Waiting."), _types("go ahead"), *_edits(), _types("and tidy up"),
        _says("Done."),
    )
    facts = _facts(_fb(tmp_path, records, "/cg-feedback"))
    # The go-ahead is no follow-up, "and tidy up" is one, after the approval.
    assert (facts["plan"], facts["build"], facts["followups"], facts["plan_followups"]) == ("approved", "same", 1, 1)


def test_the_plan_question_counts_as_asked_once_the_plan_check_was_answered_after_the_approval(tmp_path):
    base = [_types("Plan it"), _plans("p1"), _approved("p1"), *_edits()]
    asked = _plan_check_asked("q1", "answered")
    declined = _plan_check_asked("q2", "declined")
    assert _facts(_fb(tmp_path, _timeline(*base, *asked, _says()), "/cg-feedback"))["plan_asked"] == 1
    assert _facts(_fb(tmp_path, _timeline(*base, *declined, _says()), "/cg-feedback"))["plan_asked"] == 0
    # Asked before the approval, it says nothing about the plan that was built.
    before = [_types("Plan it"), *asked, _plans("p1"), _approved("p1"), *_edits()]
    assert _facts(_fb(tmp_path, _timeline(*before, _says()), "/cg-feedback"))["plan_asked"] == 0
    # The survey's own plan question, answered by an earlier run, leaves it at 0:
    # a rerun asks it again, and its answers replace the earlier run's.
    question = {"question": "Was the plan enough?", "header": CATALOGUE["coaching"]["feedback"]["plan_headers"][0]}
    survey = [_calls("AskUserQuestion", "q3", {"questions": [question]}),
              _comes_back("q3", result={"questions": [question], "answers": {"Was the plan enough?": "It was in the plan"}})]
    assert _facts(_fb(tmp_path, _timeline(*base, _ran_feedback(), *survey, _says()), "/cg-feedback"))["plan_asked"] == 0


def test_the_facts_line_names_the_tip_shown_most_and_latest(tmp_path):
    records = _timeline(
        _types("one"), _noted("drip_feed"), _says("a\n> **ClaudeGlass tip:** x"),
        _types("two"), _noted("big_paste"), _says("b\n> **ClaudeGlass tip:** y"),
        _types("three"), _noted("drip_feed"), _says("c\n> **ClaudeGlass tip:** z"),
        _types("four"), _noted("plan_fresh"), _says("d\n> **ClaudeGlass tip:** w"),
    )
    facts = _facts(_fb(tmp_path, records, "/cg-feedback"))
    # tip_hints order, not the order they came in.
    assert facts["tips"] == "plan_fresh:1,drip_feed:2,big_paste:1"
    assert facts["tip"] == "drip_feed"
    tie = _timeline(_types("one"), _noted("drip_feed"), _says("a"), _types("two"), _noted("big_paste"), _says("b"))
    assert _facts(_fb(tmp_path, tie, "/cg-feedback"))["tip"] == "big_paste"


def test_a_tip_counts_on_the_desktop_only_when_the_reply_carried_it(tmp_path, monkeypatch):
    records = _timeline(
        _types("one"), _noted("drip_feed"), _says("no tip here"),
        _types("two"), _noted("big_paste"), _says("ok\n> **ClaudeGlass tip:** y"),
        _types("three"), _noted("status_poll"), _says("done"),
    )
    assert _facts(_fb(tmp_path, records, "/cg-feedback"))["tips"] == "drip_feed:1,big_paste:1,status_poll:1"
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "claude-desktop")
    assert _facts(_fb(tmp_path, records, "/cg-feedback"))["tips"] == "big_paste:1"


def test_a_piece_starts_where_claude_tagged_shift_new_and_tokens_count_from_there(tmp_path):
    records = _timeline(
        _types("first job"), _says("done [cg: task=feature size=m]", tokens=9000),
        _types("a different job"), _says("on it [cg: task=bugfix shift=new]", tokens=2000),
        _types("tweak it"), _says("done", tokens=1000),
    )
    facts = _facts(_fb(tmp_path, records, "/cg-feedback"))
    assert (facts["tokens"], facts["followups"]) == (3000, 1)
    # A tag on a go-ahead or a status check starts nothing.
    go = _timeline(
        _types("first job"), _says("done", tokens=9000), _types("go ahead"), _says("[cg: shift=new]", tokens=2000),
        _types("how is it going?"), _says("[cg: shift=new]", tokens=1000),
    )
    assert _facts(_fb(tmp_path, go, "/cg-feedback"))["tokens"] == 12000
    # Nor does a tag in a reply to a line you didn't type.
    notified = _timeline(
        _types("first job"), _says("done", tokens=9000),
        _types("<task-notification><task-id>a1</task-id></task-notification>"), _says("[cg: shift=new]", tokens=2000),
    )
    assert _facts(_fb(tmp_path, notified, "/cg-feedback"))["tokens"] == 11000


def test_a_run_after_a_feedback_run_rates_the_work_since_it(tmp_path):
    first = [_types("first job"), _says("done", tokens=9000), _ran_feedback(), _says("Recorded.", tokens=500)]
    # Nothing since the run: the same piece again, without the survey's own reply.
    again = _facts(_fb(tmp_path, _timeline(*first), "/cg-feedback"))
    assert (again["tokens"], again["followups"]) == (9000, 0)
    more = _timeline(*first, _types("second job"), _says("done", tokens=2000), _types("tweak"), _says("ok", tokens=700))
    facts = _facts(_fb(tmp_path, more, "/cg-feedback"))
    assert (facts["tokens"], facts["followups"]) == (2700, 1)


def test_the_facts_line_comes_from_the_typical_piece_in_coaching_json(tmp_path):
    config_dir = tmp_path / "claudeglass"
    config_dir.mkdir()
    (config_dir / CATALOGUE["coaching"]["file"]).write_text(json.dumps({"typical_piece_tokens": 420_000}), encoding="utf-8")
    records = _timeline(_types("job"), _says(tokens=1_300_000))
    facts = _facts(_fb(tmp_path, records, "/cg-feedback"))
    assert (facts["tokens"], facts["typical"]) == (1_300_000, 420_000)
    (config_dir / CATALOGUE["coaching"]["file"]).write_text(json.dumps({"typical_piece_tokens": "many"}), encoding="utf-8")
    assert _facts(_fb(tmp_path, records, "/cg-feedback"))["typical"] == 0


def test_the_facts_line_needs_the_skill_toggle_and_the_project_filter(tmp_path):
    records = _timeline(_types("job"), _says())
    only_check = {"capture": {"level": "off", "feedback": ["plan_check"]}}
    assert _fb(tmp_path, records, "/cg-feedback", config=only_check) == ""
    assert _fb(tmp_path, records, "/cg-feedback", config={"capture": {"level": "off"}}) == ""
    assert _fb(tmp_path, records, "/cg-feedback", config={**FB_ALL, "exclude_projects": ["-work-app"]}) == ""
    assert _fb(tmp_path, records, "/cg-feedback", config=FB_ALL).startswith(FB_MARKER)
    # A subagent and a session with no transcript get nothing.
    payload = _prompt_for(tmp_path, records, "/cg-feedback")
    config_dir = tmp_path / "claudeglass"
    assert HOOK.feedback_note_for({**payload, "agent_id": "a1"}, FB_ALL, CATALOGUE, config_dir, now=FB_NOW) == ""
    assert HOOK.feedback_note_for({**payload, "transcript_path": ""}, FB_ALL, CATALOGUE, config_dir, now=FB_NOW) == ""
    assert HOOK.feedback_note_for({**payload, "hook_event_name": "PostToolUse"}, FB_ALL, CATALOGUE, config_dir) == ""


def test_the_facts_line_reads_a_big_transcript_quickly_and_only_its_end(tmp_path):
    import time

    records = [_types("the first job")]
    for n in range(2600):
        records += [_says("x" * 1200, tokens=100), _calls("Read", f"r{n}", {"file_path": "a.py"}, tokens=100),
                    _comes_back(f"r{n}", text="y" * 900)]
    records += [_types("one more"), _says("done", tokens=700)]
    lines = _timeline(*records, gap=1.0)
    payload = _prompt_for(tmp_path, lines, "/cg-feedback")
    assert Path(payload["transcript_path"]).stat().st_size > 5_000_000
    config_dir = tmp_path / "claudeglass"
    config_dir.mkdir()
    started = time.perf_counter()
    note = HOOK.feedback_note_for(payload, FB_ALL, CATALOGUE, config_dir, now=FB_NOW)
    elapsed = time.perf_counter() - started
    facts = _facts(note)
    # Under the hook's 5 s timeout by a wide margin. The tail starts after the first message, so "one
    # more" is the first it holds, and the tokens are those of the end only (2600 * 200 in all).
    assert elapsed < 2.5, elapsed
    assert facts["followups"] == 0 and 700 < facts["tokens"] < 2600 * 200


def test_the_facts_line_holds_only_counts_and_words_of_a_closed_list(tmp_path):
    secret = "zebra-passphrase-4821"
    records = _timeline(
        _types(f"Plan the {secret} importer"), _says(f"here is {secret}"), _plans("p1"), _approved("p1"),
        *_edits(), _queued_line(f"also {secret}"), _types(f"{FIX} {secret}"), _says(f"You're right, {secret}", content=None),
    )
    note = _fb(tmp_path, records, "/cg-feedback")
    assert secret not in note and re.fullmatch(rf"{re.escape(FB_MARKER)}( [a-z_]+=[a-z0-9_:,]+)+", note), note
    assert not list((tmp_path / "claudeglass").glob("*"))


# -- the plan check ---------------------------------------------------------------


def test_a_fix_after_an_approved_plan_and_a_build_gets_the_plan_check(tmp_path):
    note = _fb(tmp_path, _timeline(*WORK), FIX)
    assert _hint(note) == "plan_check"
    assert note == f"{cat.COACH_MARKER}{cat.COACH_VERSION} plan_check\n{cat.FEEDBACK_NOTE_TEXT['plan_check']}"
    assert cat.PLAN_CHECK_HEADER in note and note.count("AskUserQuestion") == 1
    # An adjustment that isn't a question counts as one too; a question doesn't.
    assert _hint(_fb(tmp_path, _timeline(*WORK), "Rename the importer to loader instead.", session="s2")) == "plan_check"
    assert _fb(tmp_path, _timeline(*WORK), "Why not rename the importer to loader instead?", session="s3") == ""


def test_a_plan_approved_by_a_typed_go_ahead_gets_the_plan_check_too(tmp_path):
    rejected = _comes_back("p1", error=True, text="The user doesn't want to proceed with this tool use.")
    records = _timeline(_types("Plan it"), _plans("p1"), rejected, _says("Waiting."), _types("go ahead"), *_edits(), _says("Built."))
    assert _hint(_fb(tmp_path, records, FIX)) == "plan_check"


@pytest.mark.parametrize("records", [
    pytest.param([_types("Plan it"), _plans("p1"), _approved("p1"), _says("Nothing to change.")], id="no edit since"),
    pytest.param([_types("Add it"), *_edits(), _says("Built.")], id="no plan"),
    pytest.param(
        [_types("Plan it"), _plans("p1"), _comes_back("p1", error=True, text="rejected"), *_edits(), _says("x")],
        id="plan never approved",
    ),
    pytest.param(
        [_types("Plan it"), _plans("p1"), _approved("p1"), _calls("Edit", "e2", {"file_path": "a.py"}), _comes_back("e2", error=True), _says("x")],
        id="the only edit failed",
    ),
    pytest.param(
        [_types("Plan it"), _plans("p1"), _approved("p1"), _calls("Edit", "e3", {"file_path": "/home/u/.claude/plans/p.md"}),
         _comes_back("e3"), _says("x")],
        id="only a plan file changed",
    ),
])
def test_no_plan_check_without_an_approved_plan_and_a_build(tmp_path, records):
    assert _fb(tmp_path, _timeline(*records), FIX) == ""


@pytest.mark.parametrize("prompt", [
    "go ahead", "continue", "thanks", "thank you!", "how is it going?", "looks good", "Add a test for the loader",
    "Why does it still fail?",
])
def test_a_go_ahead_a_thank_you_a_status_check_or_a_plain_request_gets_no_plan_check(tmp_path, prompt):
    assert _fb(tmp_path, _timeline(*WORK), prompt) == ""


def test_a_message_typed_while_claude_worked_gets_no_plan_check(tmp_path):
    working = _timeline(*WORK[:-1], _calls("Read", "r1", {"file_path": "a.py"}))
    assert _fb(tmp_path, working, FIX) == ""
    assert _hint(_fb(tmp_path, _timeline(*WORK), FIX, session="s2")) == "plan_check"


def test_feedback_you_typed_into_the_plan_dialog_means_no_plan_check(tmp_path):
    said = _comes_back("p0", error=True, text="The user doesn't want to proceed. the user said: use a queue instead")
    silent = _comes_back("p0", error=True, text="The user doesn't want to proceed. the user said: ")
    base = [_types("Plan it"), _plans("p0"), said, _plans("p1"), _approved("p1"), *_edits(), _says("Built.")]
    assert _fb(tmp_path, _timeline(*base), FIX) == ""
    quiet = [_types("Plan it"), _plans("p0"), silent, _plans("p1"), _approved("p1"), *_edits(), _says("Built.")]
    assert _hint(_fb(tmp_path, _timeline(*quiet), FIX, session="s2")) == "plan_check"
    # A round from before this piece of work says nothing about this plan.
    older = [_types("Old job"), _plans("p0"), said, _says("ok"), _types("New job"), _says("go [cg: task=feature shift=new]"),
             _plans("p1"), _approved("p1"), *_edits(), _says("Built.")]
    assert _hint(_fb(tmp_path, _timeline(*older), FIX, session="s3")) == "plan_check"


def test_a_plan_question_already_asked_since_the_approval_means_no_plan_check(tmp_path):
    asked = _plan_check_asked("q1", "answered")
    assert _fb(tmp_path, _timeline(*WORK, *asked, _says("ok")), FIX) == ""
    survey = {"question": "Was the plan enough?", "header": CATALOGUE["coaching"]["feedback"]["plan_headers"][0]}
    ask = [_calls("AskUserQuestion", "q2", {"questions": [survey]}), _comes_back("q2", error=True)]
    assert _fb(tmp_path, _timeline(*WORK, *ask, _says("ok")), FIX, session="s2") == ""
    # One asked before the approval is another plan's.
    before = [_types("Plan it"), *_plan_check_asked("q0", "answered"), _plans("p1"), _approved("p1"), *_edits(), _says("ok")]
    assert _hint(_fb(tmp_path, _timeline(*before), FIX, session="s3")) == "plan_check"


def test_the_plan_check_comes_once_per_plan(tmp_path):
    records = _timeline(*WORK)
    assert _hint(_fb(tmp_path, records, FIX)) == "plan_check"
    # Claude ignored it (no question in the transcript): not again for this plan.
    assert _fb(tmp_path, records, FIX) == ""
    assert _fb(tmp_path, records, "Rename it to x instead.") == ""
    # Another plan gets its own.
    second = _timeline(*WORK, _types("Plan the next bit"), _plans("p2"), _approved("p2"), *_edits("e5"), _says("Built."))
    assert _hint(_fb(tmp_path, second, FIX)) == "plan_check"
    # Another session is its own.
    assert _hint(_fb(tmp_path, records, FIX, session="s2")) == "plan_check"


def test_the_plan_check_is_off_unless_its_toggle_is_on(tmp_path):
    from claudeglass.config import FEEDBACK_ON

    assert "plan_check" not in FEEDBACK_ON and "plan_check" in cat.DEEP_FEEDBACK_IDS
    records = _timeline(*WORK)
    for feedback in (["feedback_skill", "feedback_reminder"], [], ["feedback_note"]):
        assert _fb(tmp_path, records, FIX, config={"capture": {"level": "off", "feedback": feedback}}) == ""
    assert _hint(_fb(tmp_path, records, FIX, config={"capture": {"level": "off", "feedback": ["plan_check"]}})) == "plan_check"


def test_a_coaching_tip_or_a_message_you_did_not_type_gets_no_plan_check(tmp_path):
    records = _timeline(*WORK)
    assert _fb(tmp_path, records, FIX, tipped=True) == ""
    assert _fb(tmp_path, records, "<command-name>/fix</command-name> " + FIX) == ""
    assert _fb(tmp_path, records, FIX, config={**FB_ALL, "exclude_projects": ["-work-app"]}) == ""


def _backoff_transcript(*outcomes: str) -> list[dict]:
    """A plan, a build and a plan check for each of ``outcomes``, then one more plan and build."""
    records = [_types("Plan it")]
    for n, outcome in enumerate(outcomes):
        records += [_plans(f"p{n}"), _approved(f"p{n}"), *_edits(f"e{n}"), *_plan_check_asked(f"q{n}", outcome)]
    return _timeline(*records, _plans("pz"), _approved("pz"), *_edits("ez"), _says("Built."))


@pytest.mark.parametrize("outcomes, rests", [
    (("declined", "declined"), True),
    (("other", "declined"), True),
    (("other", "other"), True),
    (("declined",), False),
    (("declined", "answered", "declined"), False),
    (("answered", "answered"), False),
])
def test_two_misses_in_a_row_rest_the_plan_check_for_two_weeks(tmp_path, outcomes, rests):
    records = _backoff_transcript(*outcomes)
    note = _fb(tmp_path, records, FIX)
    assert (note == "") is rests
    if not rests:
        return
    state = json.loads((tmp_path / "claudeglass" / CATALOGUE["coaching"]["state_file"]).read_text(encoding="utf-8"))
    check = state["feedback"]["plan_check"]
    assert check["off_until"] == pytest.approx(FB_NOW.timestamp() + 14 * 86400) and check["misses"] == 0
    # Still resting a day before the two weeks are up, on again after them.
    assert _fb(tmp_path, records, FIX, now=FB_NOW + timedelta(days=13, hours=23), session="s2") == ""
    assert _hint(_fb(tmp_path, records, FIX, now=FB_NOW + timedelta(days=14, minutes=1), session="s3")) == "plan_check"


def test_the_back_off_counts_each_question_once(tmp_path):
    records = _backoff_transcript("declined")
    for session in ("s1", "s2", "s3"):
        # One miss, however many messages and sessions see it.
        assert _fb(tmp_path, records, "go ahead", session=session) == ""
    state = json.loads((tmp_path / "claudeglass" / CATALOGUE["coaching"]["state_file"]).read_text(encoding="utf-8"))
    assert state["feedback"]["plan_check"]["misses"] == 1 and "off_until" not in state["feedback"]["plan_check"]
    assert len(state["feedback"]["plan_check"]["seen"]) == 1


def test_the_back_off_keeps_only_a_short_salted_hash_of_a_question_s_call_id(tmp_path):
    records = _backoff_transcript("declined")
    # A salt is in place once capture has run: the hash is not the one anyone could work out from the id.
    config_dir = tmp_path / "claudeglass"
    config_dir.mkdir(parents=True)
    (config_dir / HOOK.SALT_FILE).write_bytes(bytes(range(32)))
    assert _fb(tmp_path, records, "go ahead") == ""
    text = (config_dir / CATALOGUE["coaching"]["state_file"]).read_text(encoding="utf-8")
    state = json.loads(text)
    (seen,) = state["feedback"]["plan_check"]["seen"]
    assert len(seen) == 8 and "q0" not in text
    assert seen != HOOK._sha256_hex("q0")[:8]
    # Another session that carries a copy of the same transcript counts the question once.
    assert _fb(tmp_path, records, "go ahead", session="s2") == ""
    again = json.loads((config_dir / CATALOGUE["coaching"]["state_file"]).read_text(encoding="utf-8"))
    assert again["feedback"]["plan_check"]["seen"] == [seen] and again["feedback"]["plan_check"]["misses"] == 1


def test_the_back_off_survives_a_session_row_being_dropped(tmp_path):
    records = _backoff_transcript("declined", "declined")
    assert _fb(tmp_path, records, FIX) == ""
    # A day and a half on, the session's own row is gone, the rest is not.
    later = FB_NOW + timedelta(hours=40)
    assert _fb(tmp_path, records, FIX, now=later) == ""
    state = json.loads((tmp_path / "claudeglass" / CATALOGUE["coaching"]["state_file"]).read_text(encoding="utf-8"))
    assert state["feedback"]["plan_check"]["off_until"] == pytest.approx(FB_NOW.timestamp() + 14 * 86400)


# -- the rating reminder -----------------------------------------------------------


def _big(tokens: int, *, prompt: str = "Add the importer", tail: tuple = ()) -> list[dict]:
    return _timeline(_types(prompt), _says("done", tokens=tokens), *tail)


NEXT = "now the exporter please"


def test_a_big_piece_that_was_not_rated_gets_the_reminder_note(tmp_path):
    note = _fb(tmp_path, _big(1_300_000), NEXT)
    assert _hint(note) == "rating_reminder"
    text = note.split("\n", 1)[1]
    assert "1.3M tokens" in text and "{tokens}" not in text
    assert text.splitlines()[-1] == REMINDER_TEXT
    assert "hasn't been rated" in text
    lowered = {**FB_ALL, "thresholds": {"coaching_rating_min_tokens": 100_000}}
    assert "420k tokens" in _fb(tmp_path / "other", _big(420_000), NEXT, config=lowered)


@pytest.mark.parametrize("tokens, typical, reminded", [
    (999_999, 0, False),
    (1_000_000, 0, True),
    (1_300_000, 800_000, False),
    (1_600_000, 800_000, True),
    (1_300_000, 100_000, True),
    (5_000_000, 2_600_000, False),
])
def test_the_reminder_waits_for_a_million_tokens_and_twice_your_typical_piece(tmp_path, tokens, typical, reminded):
    config_dir = tmp_path / "claudeglass"
    config_dir.mkdir()
    (config_dir / CATALOGUE["coaching"]["file"]).write_text(json.dumps({"typical_piece_tokens": typical}), encoding="utf-8")
    note = _fb(tmp_path, _big(tokens), NEXT)
    assert (note != "") is reminded
    if reminded:
        assert _hint(note) == "rating_reminder"


def test_a_piece_you_rated_gets_no_reminder(tmp_path):
    rated = [_types("Add the importer"), _says("done", tokens=1_500_000), _ran_feedback(), _says("Recorded.", tokens=300)]
    assert _fb(tmp_path, _timeline(*rated), NEXT) == ""
    # Work after the rating is a piece of its own: not big enough yet, then big enough.
    small = _timeline(*rated, _types("More"), _says("done", tokens=900_000))
    assert _fb(tmp_path / "small", small, NEXT) == ""
    big = _timeline(*rated, _types("More"), _says("done", tokens=1_100_000))
    note = _fb(tmp_path / "big", big, NEXT)
    assert _hint(note) == "rating_reminder" and "1.1M tokens" in note


def _rated_on_dashboard(tmp_path: Path, session: str, at: datetime) -> None:
    """A dashboard store holding your rating of ``session``, set at ``at``."""
    import sqlite3

    config_dir = tmp_path / "claudeglass"
    config_dir.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(config_dir / HOOK._STORE_FILE)
    try:
        with db:
            db.execute("CREATE TABLE session_feedback (session_id TEXT PRIMARY KEY, set_at TEXT NOT NULL)")
            db.execute("INSERT INTO session_feedback VALUES (?, ?)", (session, at.strftime("%Y-%m-%dT%H:%M:%SZ")))
    finally:
        db.close()


def test_a_rating_on_the_dashboard_since_the_piece_started_stops_the_reminder(tmp_path):
    from claudeglass.service import serve

    assert HOOK._STORE_FILE == serve.STORE_FILENAME
    _rated_on_dashboard(tmp_path, "s1", FB_NOW - timedelta(minutes=10))
    assert _fb(tmp_path, _big(1_300_000), NEXT) == ""
    assert _hint(_fb(tmp_path, _big(1_300_000), NEXT, session="s2")) == "rating_reminder"
    # A rating from before the piece started is the earlier piece's.
    work = _timeline(_types("Add the importer"), _says("done"), _types("A different job"),
                     _says("[cg: task=feature shift=new]", tokens=1_400_000))
    _rated_on_dashboard(tmp_path / "before", "s1", FB_NOW - timedelta(minutes=10))
    assert _hint(_fb(tmp_path / "before", work, NEXT)) == "rating_reminder"
    _rated_on_dashboard(tmp_path / "since", "s1", FB_NOW - timedelta(minutes=1))
    assert _fb(tmp_path / "since", work, NEXT) == ""
    # A store it can't read changes nothing.
    broken = tmp_path / "broken" / "claudeglass"
    broken.mkdir(parents=True)
    (broken / HOOK._STORE_FILE).write_bytes(b"not a database")
    assert _hint(_fb(tmp_path / "broken", _big(1_300_000), NEXT)) == "rating_reminder"


def test_the_reminder_comes_once_per_piece_and_once_in_three_days(tmp_path):
    records = _big(1_300_000)
    assert _hint(_fb(tmp_path, records, NEXT)) == "rating_reminder"
    # Once for this piece, whatever the time.
    assert _fb(tmp_path, records, NEXT) == ""
    assert _fb(tmp_path, records, NEXT, now=FB_NOW + timedelta(days=10)) == ""
    # A new piece in another session, Claude marking it new after a message of yours.
    other = _timeline(_types("A different job"), _says("[cg: task=feature shift=new]", tokens=1_400_000))
    assert _fb(tmp_path, other, NEXT, now=FB_NOW + timedelta(days=2, hours=23), session="s2") == ""
    assert _hint(_fb(tmp_path, other, NEXT, now=FB_NOW + timedelta(days=3, minutes=1), session="s2")) == "rating_reminder"
    # And that piece, reminded, is not reminded again by the next piece's rest.
    assert _fb(tmp_path, other, NEXT, now=FB_NOW + timedelta(days=30), session="s2") == ""


def test_a_new_piece_in_the_same_session_can_be_reminded_after_the_rest(tmp_path):
    first = [_types("job one"), _says("done", tokens=1_300_000)]
    assert _hint(_fb(tmp_path, _timeline(*first), NEXT)) == "rating_reminder"
    second = _timeline(
        *first, _types("job two"), _says("[cg: task=feature shift=new]", tokens=500_000), _types("more"), _says("ok", tokens=700_000)
    )
    # 1.2M in the new piece, but the three-day rest isn't over.
    assert _fb(tmp_path, second, NEXT, now=FB_NOW + timedelta(days=1)) == ""
    note = _fb(tmp_path, second, NEXT, now=FB_NOW + timedelta(days=4))
    assert _hint(note) == "rating_reminder" and "1.2M tokens" in note


def test_the_reminders_piece_starts_at_a_message_claude_tagged_shift_new(tmp_path):
    records = _timeline(
        _types("job one"), _says("done", tokens=1_500_000),
        _types("job two"), _says("started [cg: task=docs shift=new]", tokens=200_000),
    )
    assert _fb(tmp_path, records, NEXT) == ""
    # Not when the tag is on a go-ahead or a status check, or on a different shift.
    for n, (text, tag) in enumerate((("go ahead", "shift=new"), ("how is it going?", "shift=new"), ("job two", "shift=build"))):
        kept = _timeline(_types("job one"), _says("done", tokens=1_500_000), _types(text), _says(f"[cg: {tag}]", tokens=200_000))
        assert _hint(_fb(tmp_path / f"kept{n}", kept, NEXT)) == "rating_reminder", text


def test_the_reminders_piece_counts_only_the_end_of_the_transcript_the_hook_reads(tmp_path):
    # 1.2M tokens over 6 MB: the 4 MB the hook reads hold about 0.75M, a lower bound, so no reminder yet.
    records = [_types("The job")] + [_says("x" * 1800, tokens=400) for _ in range(3000)]
    path = tmp_path / "s1.jsonl"
    assert _fb(tmp_path, _timeline(*records, gap=1.0), NEXT) == ""
    assert path.stat().st_size > HOOK._COACH_PROMPT_TAIL_BYTES


def test_the_plan_check_comes_before_the_reminder(tmp_path):
    records = _timeline(_types("Plan it"), _plans("p1", tokens=1_500_000), _approved("p1"), *_edits(), _says("Built."))
    assert _hint(_fb(tmp_path, records, FIX)) == "plan_check"
    # The plan check went out for this plan, so the reminder gets its turn.
    assert _hint(_fb(tmp_path, records, FIX)) == "rating_reminder"
    assert _fb(tmp_path, records, FIX) == ""


def test_a_coaching_tip_in_the_same_call_leaves_the_reminder_for_the_next_message(tmp_path):
    records = _big(1_300_000)
    assert _fb(tmp_path, records, NEXT, tipped=True) == ""
    assert _hint(_fb(tmp_path, records, NEXT)) == "rating_reminder"


def test_the_reminder_waits_for_a_finished_reply_and_a_message_you_typed(tmp_path):
    working = _big(1_300_000, tail=(_calls("Read", "r1", {"file_path": "a.py"}),))
    assert _fb(tmp_path, working, NEXT) == ""
    assert _fb(tmp_path, _big(1_300_000), "<command-name>/clear</command-name>", session="s2") == ""
    assert _fb(tmp_path, _big(1_300_000), NEXT, config={"capture": {"level": "off", "feedback": ["feedback_skill", "plan_check"]}}, session="s3") == ""
    assert _fb(tmp_path, _big(1_300_000), NEXT, config={**FB_ALL, "exclude_projects": ["-work-app"]}, session="s4") == ""
    assert _hint(_fb(tmp_path, _big(1_300_000), NEXT, session="s5")) == "rating_reminder"


def test_the_reminder_uses_the_threshold_from_config_toml_and_coaching_json(tmp_path):
    config_dir = tmp_path / "claudeglass"
    config_dir.mkdir()
    (config_dir / CATALOGUE["coaching"]["file"]).write_text(
        json.dumps({"thresholds": {"rating_min_tokens": 2_000_000}}), encoding="utf-8"
    )
    assert _fb(tmp_path, _big(1_300_000), NEXT) == ""
    assert _hint(_fb(tmp_path, _big(2_100_000), NEXT, session="s2")) == "rating_reminder"
    lowered = {**FB_ALL, "thresholds": {"coaching_rating_min_tokens": 1_000}}
    assert _hint(_fb(tmp_path / "other", _big(1_300_000), NEXT, config=lowered)) == "rating_reminder"


def test_the_reminders_note_names_the_tokens_as_a_person_would():
    assert HOOK._tokens_text(1_300_000) == "1.3M"
    assert HOOK._tokens_text(1_000_000) == "1M"
    assert HOOK._tokens_text(2_049_999) == "2M"
    assert HOOK._tokens_text(420_000) == "420k"
    assert HOOK._tokens_text(999_400) == "999k"


def test_the_feedback_notes_leave_nothing_of_your_words_in_the_note_or_the_state(tmp_path):
    secret = "zebra-passphrase-4821"
    records = _timeline(
        _types(f"Plan the {secret} importer"), _plans("p1", tokens=1_500_000), _approved("p1"), *_edits(),
        _says(f"Built {secret}."),
    )
    seen = [_fb(tmp_path, records, f"{FIX} {secret}"), _fb(tmp_path, records, f"{FIX} {secret}")]
    assert [_hint(n) for n in seen] == ["plan_check", "rating_reminder"]
    assert secret not in "".join(seen)
    for path in (tmp_path / "claudeglass").rglob("*"):
        if path.is_file():
            assert secret not in path.read_text(encoding="utf-8")


# -- the whole hook ---------------------------------------------------------------------

# These run the hook as a process, on the real clock: the transcript must be recent, or a coaching tip
# about a cold cache comes first.


def _feedback_config(tmp_path: Path, feedback: str, extra: str = "") -> Path:
    return _config(tmp_path / "cg", f'[capture]\nlevel = "off"\nfeedback = [{feedback}]\n{extra}')


def test_a_feedback_run_gets_its_facts_line_and_no_other_note_at_any_capture_level(tmp_path):
    records = _timeline(_types("Add the importer"), _says("done", tokens=1_300_000), now=datetime.now(timezone.utc))
    payload = _prompt_for(tmp_path, records, "/cg-feedback")
    # Coaching notes on too: its tip would only be one more thing for Claude to write.
    config_dir = _feedback_config(tmp_path, '"feedback_skill", "feedback_reminder"', 'coaching = ["coaching_notes"]\n')
    note = _note(config_dir, payload)
    assert note.startswith(FB_MARKER) and "\n" not in note and "tokens=1300000" in note
    # A message that is not the command gets the reminder, then the same message gets nothing more.
    reminder = _note(config_dir, {**payload, "prompt": NEXT})
    assert _hint(reminder) == "rating_reminder" and reminder.splitlines()[-1] == REMINDER_TEXT
    assert _note(config_dir, {**payload, "prompt": NEXT}) == ""


def test_a_coaching_tip_goes_last_and_the_reminder_makes_way_for_it(tmp_path):
    records = _timeline(_types("Add the importer"), _says("done", tokens=1_300_000), now=datetime.now(timezone.utc))
    big = "word " * 60_000
    config_dir = _feedback_config(tmp_path, '"feedback_reminder"', 'coaching = ["coaching_notes"]\n')
    note = _note(config_dir, _prompt_for(tmp_path, records, big))
    assert _hint(note) == "big_paste" and REMINDER_TEXT not in note
    assert note.splitlines()[-1].startswith(cat.TIP_LABEL)


def test_the_survey_items_run_with_nothing_else_on_and_not_with_nothing_asked(tmp_path):
    records = _timeline(_types("Add the importer"), _says("done", tokens=1_300_000), now=datetime.now(timezone.utc))
    payload = _prompt_for(tmp_path, records, "/cg-feedback")
    assert _note(_feedback_config(tmp_path, '"feedback_skill"'), payload).startswith(FB_MARKER)
    for body in ('', '"feedback_note"'):
        assert _note(_config(tmp_path / f"c{len(body)}", f'[capture]\nlevel = "off"\nfeedback = [{body}]\n'), payload) == ""
    # A subagent's prompt never carries a facts line.
    assert _note(_feedback_config(tmp_path, '"feedback_skill"'), {**payload, "agent_id": "a1"}) == ""


def test_a_run_with_nobody_at_the_screen_gets_no_facts_line_or_note(tmp_path):
    records = _timeline(_types("Add the importer"), _says("done", tokens=1_300_000), now=datetime.now(timezone.utc))
    config_dir = _feedback_config(tmp_path, '"feedback_skill", "feedback_reminder", "plan_check"')
    for prompt in ("/cg-feedback", NEXT):
        env = {**os.environ, "CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}
        assert _run(config_dir, _prompt_for(tmp_path, records, prompt), env=env) == (0, "", "")


def test_a_broken_transcript_gives_no_note_and_no_error(tmp_path):
    config_dir = _feedback_config(tmp_path, '"feedback_skill", "feedback_reminder", "plan_check"')
    path = tmp_path / "broken.jsonl"
    path.write_bytes(b'{"type": "user"\n\x00\xff garbage\n[1, 2]\n"text"\n')
    payload = {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "cwd": "/work/app", "transcript_path": str(path)}
    for prompt in ("/cg-feedback", FIX):
        rc, out, err = _run(config_dir, {**payload, "prompt": prompt})
        assert rc == 0 and err == ""
        assert out == "" or json.loads(out)["hookSpecificOutput"]["additionalContext"].startswith(FB_MARKER)
    missing = {**payload, "transcript_path": str(tmp_path / "nothing.jsonl"), "prompt": "/cg-feedback"}
    assert _run(config_dir, missing)[0] == 0

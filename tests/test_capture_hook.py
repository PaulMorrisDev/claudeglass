"""``hooks/capture-hook.py``, the metrics-capture hook, run the way Claude
Code runs it: a subprocess fed the hook payload on stdin. It must add
exactly the note ``capture_catalogue.note_text`` builds, add nothing
when capture is off, sampled out, past its end, in a skipped project or
on a resume, and never fail or print on bad input.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
import importlib.util
import json
import os
import subprocess
import sys
from importlib import resources
from pathlib import Path

import pytest

from claudeglass import capture_catalogue as cat
from claudeglass import hook_health
from claudeglass.config import CaptureConfig

SCRIPT = Path(str(resources.files("claudeglass") / "hooks" / cat.HOOK_SCRIPT))


def _load_hook_module():
    spec = importlib.util.spec_from_file_location("_capture_note_under_test", SCRIPT)
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
    small = {"session_id": "s1", "hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_response": {"stdout": "ok"}}
    big = {**small, "tool_response": {"stdout": "x" * threshold}}
    assert _note(deep, small) == ""
    assert _note(deep, big) == cat.tool_note_text("big_output")
    web = {**small, "tool_name": "WebFetch"}
    assert _note(deep, web) == cat.tool_note_text("web")
    standard = _config(tmp_path / "standard", '[capture]\nlevel = "standard"\n')
    assert _note(standard, big) == ""


def test_the_feedback_reminder_needs_capture_on(tmp_path):
    free = _config(tmp_path / "free", '[capture]\nlevel = "free"\nfeedback = ["feedback_reminder"]\n')
    assert "/tl-feedback" in _note(free, _start())
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
                        {**_start(), "hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_response": "x" * 40_000}):
            assert _run(config_dir, payload, env=env) == (0, "", ""), (entrypoint, payload["hook_event_name"])
        rc, out, _ = _run(config_dir, {**_start(), "hook_event_name": "SessionEnd", "reason": "other"}, env=env)
        assert rc == 0 and out == ""
    assert len(list((config_dir / "signals").glob("*.jsonl"))) == 1
    # The terminal and the desktop app still get the note.
    for entrypoint in ("cli", "claude-desktop"):
        env = {**os.environ, "CLAUDE_CODE_ENTRYPOINT": entrypoint}
        rc, out, _ = _run(config_dir, _start(), env=env)
        assert rc == 0 and json.loads(out)["hookSpecificOutput"]["additionalContext"].startswith(cat.NOTE_MARKER)


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
    assert [p.name for p in written] == [cat.HOOK_SCRIPT, cat.CATALOGUE_FILE]
    rc, out, _ = _run(config_dir, _start(), script=written[0])
    assert rc == 0 and json.loads(out)["hookSpecificOutput"]["additionalContext"].startswith(cat.NOTE_MARKER)


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
    # tool, and before 0.11.0 moved retry from the brief to Haiku: today's
    # note for the metrics it names, plus the retry line it had then.
    old = rendered.replace("Token Lens", "ClaudeGlass").replace("shift,retry", "shift").splitlines()
    retry_line = "When you start an agent again because its last run fell short, begin the brief with [retry: model|brief|tools|scope|other]."
    assert old.pop(-2) == retry_line
    now = f"<system-reminder>\nSessionStart hook additional context: {cat.note_text(result.meta.cap_metrics, 'main')}\n</system-reminder>"
    assert old == now.splitlines()

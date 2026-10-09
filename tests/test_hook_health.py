"""Tests for ``src/claudeglass/hook_health.py``: finding the
SessionStart snapshot hook in ``settings.json``, spotting a Windows path
broken by JSON escaping, and repairing only that command after a backup
(also through ``init --repair-hook``)."""

from __future__ import annotations

import dataclasses
import io
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from claudeglass import cli, helptext, hook_health, setup_flow
from claudeglass.model import Diagnostics, Event, EventKind, TranscriptMeta, TranscriptResult, Turn
from claudeglass.render import markdown

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)

windows_only = pytest.mark.skipif(os.name != "nt", reason="the escaping bug needs Windows backslash paths")


@pytest.fixture(autouse=True)
def _claude_folder(tmp_path, monkeypatch):
    """Claude Code's folder is ``$CLAUDE_CONFIG_DIR`` (``discovery.claude_root``),
    never worked out from the data folder: point it at the ``claude``
    folder these tests build ``settings.json`` in."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))


def _claude_dir(tmp_path, command=None, *, script=True):
    """``<tmp>/claude`` with ``claudeglass/`` as the config dir and a
    ``settings.json`` whose one SessionStart hook runs ``command``
    (``None`` means a good command pointing at the real script)."""
    claude = tmp_path / "claude"
    config_dir = claude / "claudeglass"
    script_path = config_dir / "hooks" / "snapshot-config.py"
    script_path.parent.mkdir(parents=True)
    if script:
        script_path.write_text("# hook\n", encoding="utf-8")
    good = f'"{sys.executable}" "{script_path}"'
    settings = {
        "model": "opus",
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": command or good}]}]},
    }
    (claude / "settings.json").write_text(json.dumps(settings, indent=2), encoding="utf-8")
    return config_dir, good


def _broken(good: str) -> str:
    # Backslash-t decodes to a tab. pytest names each test's tmp_path
    # after the test ("test_..."), so that folder becomes TAB + "est_...".
    broken = good.replace("\\test_", "\test_")
    assert broken != good
    return broken


def _write_snapshot(config_dir, ts: str) -> None:
    snaps = config_dir / "snapshots"
    snaps.mkdir(parents=True, exist_ok=True)
    (snaps / "s.json").write_text(json.dumps({"ts": ts, "effective": {}}), encoding="utf-8")


def test_an_apply_stamp_is_not_a_hook_snapshot(tmp_path):
    # apply marks the active profile with a config-free stamp; it says
    # nothing about whether the hook still runs.
    config_dir, _good = _claude_dir(tmp_path)
    _write_snapshot(config_dir, "2026-09-10T12:00:00Z")
    (config_dir / "snapshots" / "stamp.json").write_text(
        json.dumps({"ts": "2026-09-22T11:00:00Z", "schema_version": 2, "profile_id": "lean"}), encoding="utf-8"
    )
    assert hook_health.check(config_dir, now=NOW).last_snapshot_days == pytest.approx(12.0)


def test_good_hook_is_ok(tmp_path):
    config_dir, good = _claude_dir(tmp_path)
    _write_snapshot(config_dir, "2026-09-20T12:00:00Z")
    health = hook_health.check(config_dir, now=NOW)
    assert health.ok
    assert health.command == good
    assert health.fixed_command is None
    assert health.last_snapshot_days == pytest.approx(2.0)
    assert health.summary() == "Last config snapshot: 2 days ago. The SessionStart hook is set up."


@windows_only
def test_mis_escaped_path_is_found_and_a_fix_worked_out(tmp_path):
    config_dir, good = _claude_dir(tmp_path)
    broken = _broken(good)
    settings_path = config_dir.parent / "settings.json"
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    data["hooks"]["SessionStart"][0]["hooks"][0]["command"] = broken
    settings_path.write_text(json.dumps(data), encoding="utf-8")

    health = hook_health.check(config_dir, now=NOW)
    assert not health.ok
    assert health.mis_escaped
    assert not health.script_exists
    assert health.fixed_command == good
    assert "init --repair-hook" in health.summary()
    assert health.summary().startswith("No automatic config snapshot has been taken yet.")


def test_missing_script_has_no_fix(tmp_path):
    config_dir, _ = _claude_dir(tmp_path, script=False)
    health = hook_health.check(config_dir, now=NOW)
    assert not health.ok
    assert not health.mis_escaped
    assert health.fixed_command is None
    assert "does not exist" in health.summary()


def test_no_hook_and_no_settings(tmp_path):
    config_dir = tmp_path / "claude" / "claudeglass"
    config_dir.mkdir(parents=True)
    health = hook_health.check(config_dir, now=NOW)
    assert health.command is None
    assert not health.ok
    assert "No SessionStart hook runs snapshot-config.py" in health.summary()

    (config_dir.parent / "settings.json").write_text("{not json", encoding="utf-8")
    assert hook_health.check(config_dir, now=NOW).command is None


@windows_only
def test_repair_backs_up_and_changes_only_the_command(tmp_path):
    config_dir, good = _claude_dir(tmp_path)
    settings_path = config_dir.parent / "settings.json"
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    data["hooks"]["SessionStart"][0]["hooks"][0]["command"] = _broken(good)
    before = json.dumps(data)
    settings_path.write_text(before, encoding="utf-8")

    backup = hook_health.repair(hook_health.check(config_dir, now=NOW), now=NOW)

    assert backup.name == "settings.json.bak-20260922T120000Z"
    assert backup.read_text(encoding="utf-8") == before
    after = json.loads(settings_path.read_text(encoding="utf-8"))
    assert after["hooks"]["SessionStart"][0]["hooks"][0]["command"] == good
    assert after["model"] == "opus"
    assert hook_health.check(config_dir, now=NOW).ok


def test_repair_refuses_when_there_is_nothing_to_fix(tmp_path):
    config_dir, _ = _claude_dir(tmp_path)
    with pytest.raises(ValueError):
        hook_health.repair(hook_health.check(config_dir, now=NOW), now=NOW)


def test_diagnostics_table_leads_with_the_hook_row(tmp_path):
    config_dir, _ = _claude_dir(tmp_path)
    table = helptext.diagnostics_table(Diagnostics(), hook=hook_health.check(config_dir, now=NOW))
    assert table.rows[0][0] == "snapshot_hook"
    assert table.rows[0][1] == "working"
    assert table.value_labels["snapshot_hook"] == "Config snapshot hook"


def test_diagnostics_table_heads_the_setup_rows_apart_from_the_window_counters(tmp_path):
    """The hook and statusline rows describe your setup and every session
    (not the window or project picked); the counters below them are the
    window's. Only the first setup row heads its group."""
    config_dir, _ = _claude_dir(tmp_path)
    hook = hook_health.check(config_dir, now=NOW)
    statusline = (True, "Your statusline is on.")
    setup = "Your setup and every session, every project"
    first = dataclasses.fields(Diagnostics)[0].name
    counters = {first: "Read in this window"}

    both = helptext.diagnostics_table(Diagnostics(), hook=hook, statusline=statusline)
    assert [row[0] for row in both.rows[:3]] == ["snapshot_hook", "statusline", first]
    assert both.row_groups == {"snapshot_hook": setup, **counters}
    only_hook = helptext.diagnostics_table(Diagnostics(), hook=hook)
    assert only_hook.row_groups == {"snapshot_hook": setup, **counters}
    only_statusline = helptext.diagnostics_table(Diagnostics(), statusline=statusline)
    assert only_statusline.row_groups == {"statusline": setup, **counters}
    # No setup row: one list of counters, no headings.
    neither = helptext.diagnostics_table(Diagnostics())
    assert neither.row_groups == {}
    assert neither.rows[0][0] == first

    # The headings read in the markdown report, each once, in order.
    lines = markdown._render_table(both, "USD")
    headings = [line for line in lines if line.startswith("| **")]
    assert [h.split("**")[1] for h in headings] == [setup, "Read in this window"]
    assert lines.index(headings[0]) < next(i for i, line in enumerate(lines) if "Config snapshot hook" in line)
    assert lines.index(headings[1]) < next(i for i, line in enumerate(lines) if "Lines read" in line)


def _run_init(config_dir, tmp_path, *, repair_hook=False):
    args = cli._make_parser().parse_args(
        [
            "init", "--config-dir", str(config_dir), "--claude-root", str(config_dir.parent),
            "--projects-root", str(tmp_path / "projects"), "--non-interactive", "--no-install", "--no-service",
            *(["--repair-hook"] if repair_hook else []),
        ]
    )
    stdout = io.StringIO()
    rc = setup_flow.run(*cli._setup_flow_inputs(args), stdin=io.StringIO(""), stdout=stdout, now=NOW)
    assert rc == 0
    return stdout.getvalue()


@windows_only
def test_init_reports_but_does_not_repair_without_the_flag(tmp_path):
    config_dir, good = _claude_dir(tmp_path)
    settings_path = config_dir.parent / "settings.json"
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    data["hooks"]["SessionStart"][0]["hooks"][0]["command"] = _broken(good)
    settings_path.write_text(json.dumps(data), encoding="utf-8")

    out = _run_init(config_dir, tmp_path)
    assert "The snapshot hook in Claude Code's settings.json" in out and "can't run." in out
    assert "init --repair-hook" in out
    assert not list(settings_path.parent.glob("settings.json.bak-*"))


@windows_only
def test_init_repair_hook_fixes_it(tmp_path):
    config_dir, good = _claude_dir(tmp_path)
    settings_path = config_dir.parent / "settings.json"
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    data["hooks"]["SessionStart"][0]["hooks"][0]["command"] = _broken(good)
    settings_path.write_text(json.dumps(data), encoding="utf-8")

    out = _run_init(config_dir, tmp_path, repair_hook=True)
    assert "Fixed. The previous settings.json is at" in out
    assert json.loads(settings_path.read_text(encoding="utf-8"))["hooks"]["SessionStart"][0]["hooks"][0]["command"] == good


def test_missing_interpreter_is_found_and_fixed_with_full_paths(tmp_path):
    # "py -3" with no Python launcher installed: the script exists, yet
    # the hook never runs.
    config_dir, _ = _claude_dir(tmp_path)
    script = config_dir / "hooks" / "snapshot-config.py"
    settings_path = config_dir.parent / "settings.json"
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    data["hooks"]["SessionStart"][0]["hooks"][0]["command"] = f'no-such-python-launcher -3 "{script}"'
    settings_path.write_text(json.dumps(data), encoding="utf-8")

    health = hook_health.check(config_dir, now=NOW, python="/usr/bin/python3")
    assert health.script_exists
    assert not health.interpreter_found
    assert not health.ok
    assert "not installed or not on your PATH" in health.summary()
    assert health.fixed_command == f'"/usr/bin/python3" -I -S "{script.resolve()}"'


def test_percent_variable_is_written_out_keeping_the_users_interpreter(tmp_path, monkeypatch):
    # Git Bash, which Claude Code uses on Windows, passes %VAR% through
    # unexpanded.
    config_dir, _ = _claude_dir(tmp_path)
    monkeypatch.setenv("TL_TEST_ROOT", str(config_dir))
    settings_path = config_dir.parent / "settings.json"
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    data["hooks"]["SessionStart"][0]["hooks"][0]["command"] = (
        f'"{sys.executable}" "%TL_TEST_ROOT%{os.sep}hooks{os.sep}snapshot-config.py"'
    )
    settings_path.write_text(json.dumps(data), encoding="utf-8")

    health = hook_health.check(config_dir, now=NOW)
    assert health.percent_vars
    assert not health.ok
    assert "%VARIABLE%" in health.summary()
    script = config_dir / "hooks" / "snapshot-config.py"
    assert health.fixed_command == f'"{sys.executable}" "{script}"'
    hook_health.repair(health, now=NOW)
    assert hook_health.check(config_dir, now=NOW).ok


def test_percent_variable_with_a_missing_interpreter_names_a_python_by_full_path(tmp_path, monkeypatch):
    config_dir, _ = _claude_dir(tmp_path)
    monkeypatch.setenv("TL_TEST_ROOT", str(config_dir))
    settings_path = config_dir.parent / "settings.json"
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    data["hooks"]["SessionStart"][0]["hooks"][0]["command"] = (
        f'no-such-python-launcher -3 "%TL_TEST_ROOT%{os.sep}hooks{os.sep}snapshot-config.py"'
    )
    settings_path.write_text(json.dumps(data), encoding="utf-8")

    health = hook_health.check(config_dir, now=NOW)
    script = (config_dir / "hooks" / "snapshot-config.py").resolve()
    assert health.fixed_command == f'"{hook_health.stable_python()}" -I -S "{script}"'


def test_a_hook_command_prefers_the_base_interpreter_over_a_venv(monkeypatch, tmp_path):
    base = tmp_path / "python.exe"
    base.write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "_base_executable", str(base), raising=False)
    assert hook_health.stable_python() == str(base)
    monkeypatch.setattr(sys, "_base_executable", str(tmp_path / "gone.exe"), raising=False)
    assert hook_health.stable_python() == sys.executable


def test_hook_command_runs_python_isolated_and_without_site():
    script = Path("C:/claudeglass/hooks/capture-hook.py")
    command = hook_health.hook_command(script, python="C:/Python311/python.exe")
    assert command == f'"C:/Python311/python.exe" -I -S "{script}"'


@pytest.mark.parametrize("bad_python", ['C:/weird"quote/python.exe', "C:/weird$var/python.exe", "C:/weird`tick/python.exe"])
def test_hook_command_refuses_a_python_path_with_an_unsafe_character(bad_python):
    assert hook_health.hook_command(Path("C:/claudeglass/hooks/capture-hook.py"), python=bad_python) is None


@pytest.mark.parametrize("bad_script", ['C:/weird"quote/capture-hook.py', "C:/weird$var/capture-hook.py", "C:/weird`tick/capture-hook.py"])
def test_hook_command_refuses_a_script_path_with_an_unsafe_character(bad_script):
    assert hook_health.hook_command(Path(bad_script), python="C:/Python311/python.exe") is None


def test_hook_command_refuses_a_unc_path():
    assert hook_health.hook_command(
        Path(r"\\server\share\claudeglass\hooks\capture-hook.py"), python="C:/Python311/python.exe"
    ) is None


def test_hook_command_doubles_a_trailing_backslash_so_the_quote_still_closes():
    # A folder path ending in a bare backslash would otherwise escape the
    # closing quote in both Windows argv parsing and a POSIX
    # double-quoted string (what Git Bash reads the command as).
    command = hook_health.hook_command(Path("capture-hook.py"), python="C:/oddly\\")
    assert command == '"C:/oddly\\\\" -I -S "capture-hook.py"'


def _with_statusline(tmp_path, rows=()):
    config_dir, _ = _claude_dir(tmp_path)
    settings_path = config_dir.parent / "settings.json"
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    data["statusLine"] = {"type": "command", "command": "python -m claudeglass.statusline"}
    settings_path.write_text(json.dumps(data), encoding="utf-8")
    lines = ["logged_at,session_id,window,used_percentage,resets_at,source"] + list(rows)
    (config_dir / "usage-log.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return config_dir


def test_statusline_check_explains_desktop_only_use(tmp_path):
    config_dir = _with_statusline(tmp_path)
    working, sentence = hook_health.statusline_check(config_dir, {"claude-desktop": {"count": 5, "last_ts": "2026-09-22"}})
    assert not working
    assert "All 5 of your sessions ran outside a terminal" in sentence


def test_statusline_check_working_and_stale(tmp_path):
    config_dir = _with_statusline(tmp_path, ["2026-09-20T10:00:00Z,s1,five_hour,12,,statusline"])
    working, sentence = hook_health.statusline_check(config_dir, {"cli": {"count": 2, "last_ts": "2026-09-20T09:00:00Z"}})
    assert working and "2026-09-20 10:00" in sentence
    working, sentence = hook_health.statusline_check(config_dir, {"cli": {"count": 2, "last_ts": "2026-09-22T09:00:00Z"}})
    assert not working and "terminal sessions ran later" in sentence


def test_statusline_check_not_set_up(tmp_path):
    config_dir, _ = _claude_dir(tmp_path)
    working, sentence = hook_health.statusline_check(config_dir, {})
    assert not working and "init --connect" in sentence


def test_repair_keeps_arguments_after_the_script(tmp_path, monkeypatch):
    # A hook command for a non-default data folder carries --config-dir;
    # rebuilding it around a working Python must keep that.
    claude = tmp_path / "claude"
    config_dir = claude / "claudeglass"
    script = config_dir / "hooks" / "snapshot-config.py"
    script.parent.mkdir(parents=True)
    script.write_text("", encoding="utf-8")
    command = f'nosuchpython-xyz "{script}" --config-dir "{config_dir}"'
    settings = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": command}]}]}}
    (claude / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    health = hook_health.check(config_dir, now=NOW, python=sys.executable)
    assert not health.interpreter_found
    assert health.fixed_command == f'"{sys.executable}" -I -S "{script.resolve()}" --config-dir "{config_dir}"'


# -- SURV-HE: count_hook_errors / HookErrorHealth ----------------------------


def _hook_event(subkind: str, hook_name: str | None = "PreToolUse", ts: str | None = None) -> Event:
    detail = {} if hook_name is None else {"hookName": hook_name}
    return Event(kind=EventKind.HOOK_OUTPUT, subkind=subkind, ts=ts, detail=detail)


def _result(*events: Event) -> TranscriptResult:
    return TranscriptResult(events=list(events))


def test_count_hook_errors_tallies_calls_and_errors_by_hook_name():
    results = [
        _result(
            _hook_event("hook_success", "PreToolUse"),
            _hook_event("hook_success", "PreToolUse"),
            _hook_event("hook_non_blocking_error", "PreToolUse"),
        ),
        _result(_hook_event("hook_success", "PostToolUse")),
    ]
    health = hook_health.count_hook_errors(results)
    by_name = {s.hook_name: s for s in health.stats}
    assert by_name["PreToolUse"].calls == 3
    assert by_name["PreToolUse"].errors == 1
    assert by_name["PreToolUse"].error_rate == pytest.approx(1 / 3)
    assert by_name["PostToolUse"].calls == 1
    assert by_name["PostToolUse"].errors == 0


def test_count_hook_errors_ignores_non_call_subkinds():
    # A blocking error is often a hook doing exactly what it's for; the
    # rest aren't a pass/fail outcome of the hook at all. None of these
    # should move a hook's calls or errors.
    results = [
        _result(
            _hook_event("hook_blocking_error", "PreToolUse"),
            _hook_event("hook_cancelled", "PreToolUse"),
            _hook_event("hook_system_message", "PreToolUse"),
            _hook_event("capture_note", "SessionStart"),
            Event(kind=EventKind.HOOK_OUTPUT, subkind="stop_hook_summary"),
        )
    ]
    assert hook_health.count_hook_errors(results).stats == ()


def test_count_hook_errors_ignores_non_hook_output_events():
    results = [_result(Event(kind=EventKind.REMINDER, subkind="hook_success", detail={"hookName": "PreToolUse"}))]
    assert hook_health.count_hook_errors(results).stats == ()


def test_count_hook_errors_skips_a_call_with_no_hook_name():
    results = [_result(_hook_event("hook_success", hook_name=None))]
    assert hook_health.count_hook_errors(results).stats == ()


def test_count_hook_errors_stats_are_sorted_by_hook_name():
    results = [_result(_hook_event("hook_success", "SessionStart"), _hook_event("hook_success", "PreToolUse"))]
    names = [s.hook_name for s in hook_health.count_hook_errors(results).stats]
    assert names == ["PreToolUse", "SessionStart"]


def test_worst_ignores_a_hook_below_the_minimum_call_count():
    # 100% errors, but only 3 calls -- too little to act on.
    stats = (hook_health.HookErrorStat("Flaky", calls=3, errors=3),)
    assert hook_health.HookErrorHealth(stats=stats).worst() is None


def test_worst_picks_the_highest_error_rate_among_hooks_with_enough_calls():
    stats = (
        hook_health.HookErrorStat("Mild", calls=100, errors=10),
        hook_health.HookErrorStat("Severe", calls=20, errors=15),
        hook_health.HookErrorStat("TooFewCalls", calls=5, errors=5),
    )
    assert hook_health.HookErrorHealth(stats=stats).worst().hook_name == "Severe"


def test_recommendation_is_none_under_the_failure_threshold():
    stats = (hook_health.HookErrorStat("PreToolUse", calls=100, errors=49),)
    assert hook_health.HookErrorHealth(stats=stats).recommendation() is None


def test_recommendation_is_none_with_no_stats_at_all():
    assert hook_health.HookErrorHealth().recommendation() is None


def test_recommendation_names_the_hook_and_failure_share_over_the_threshold():
    stats = (hook_health.HookErrorStat("PreToolUse", calls=100, errors=60),)
    text = hook_health.HookErrorHealth(stats=stats).recommendation()
    assert text is not None
    assert "PreToolUse" in text
    assert "60 times" in text
    assert "60%" in text
    assert "100 runs Claude Code recorded" in text
    # The hook can live in any settings layer or a plugin, not just the
    # user settings.json (a real case: a project's own .claude/settings.json).
    assert "~/.claude/settings.json" in text
    assert "project's .claude/settings.json and .claude/settings.local.json" in text
    assert "plugins" in text
    assert "last failure" not in text


def test_recommendation_says_when_the_hook_last_failed():
    # A hook fixed mid-window still fails "100%" until its old failures
    # age out; the time of the last one shows whether it has stopped.
    results = [
        _result(*[_hook_event("hook_non_blocking_error", ts=f"2026-09-28T07:{minute:02d}:27.460Z") for minute in range(20, 40)]),
        _result(_hook_event("hook_non_blocking_error", ts="2026-09-27T23:59:00.000Z")),
    ]
    health = hook_health.count_hook_errors(results)

    assert health.stats[0].last_error_ts == "2026-09-28T07:39:27.460Z"
    assert "The last failure was at 2026-09-28 07:39 UTC" in health.recommendation()


def test_a_hook_that_stopped_failing_leaves_the_tally():
    fixed = _hook_event("hook_non_blocking_error")
    fixed.detail["script"] = "guard.ps1"
    broken = _hook_event("hook_non_blocking_error")
    broken.detail["script"] = "other.ps1"
    results = [_result(*[fixed] * 30, broken, _hook_event("hook_success"))]
    [stat] = hook_health.count_hook_errors(results, stopped=["guard.ps1"]).stats
    assert (stat.calls, stat.errors) == (2, 1)


# -- Phase 7: measure_hook_overhead / HookOverhead --------------------------


def _call_event(hook_name: str, duration_ms, *, capture: bool = True, subkind: str = "hook_success") -> Event:
    detail: dict = {"hookName": hook_name}
    if duration_ms is not None:
        detail["durationMs"] = duration_ms
    if capture:
        detail["capture"] = True
    return Event(kind=EventKind.HOOK_OUTPUT, subkind=subkind, detail=detail)


def _turn(tools: dict | None = None, *, errors: dict | None = None, stop: str | None = "tool_use", synthetic: bool = False) -> Turn:
    return Turn(
        tool_calls_by_tool=dict(tools or {}),
        tool_errors_by_tool=dict(errors or {}),
        stop_reason=stop,
        is_synthetic=synthetic,
    )


def _main(*, turns=(), events=()) -> TranscriptResult:
    return TranscriptResult(meta=TranscriptMeta(kind="top-level"), turns=list(turns), events=list(events))


def _agent(agent_type: str = "Explore", *, turns=(), events=(), kind: str = "subagent") -> TranscriptResult:
    return TranscriptResult(meta=TranscriptMeta(kind=kind, agent_type=agent_type), turns=list(turns), events=list(events))


def _spec(event: str, matcher: str = "") -> hook_health.HookSpec:
    return hook_health.HookSpec("capture-hook.py", event, matcher)


def _row(overhead: hook_health.HookOverhead, event: str) -> hook_health.HookOverheadRow:
    [row] = [row for row in overhead.rows if row.event == event]
    return row


def test_overhead_counts_runs_from_matched_tool_calls_when_few_durations_are_recorded():
    # 120 matched calls (main and agent), but Claude Code wrote a time for 3.
    results = [
        _main(
            turns=[_turn({"Read": 60, "Grep": 40})],
            events=[_call_event("PostToolUse", 40), _call_event("PostToolUse", 50), _call_event("PostToolUse", 60)],
        ),
        _agent(turns=[_turn({"Read": 20})]),
    ]
    row = _row(hook_health.measure_hook_overhead(results, [_spec("PostToolUse", "Read|Grep")]), "PostToolUse")
    assert (row.runs, row.recorded, row.median_ms) == (120, 3, 50.0)
    assert row.counted
    assert row.summed_s == pytest.approx(6.0)


def test_overhead_does_not_count_tools_the_matcher_leaves_out():
    results = [_main(turns=[_turn({"Read": 5, "Bash": 30, "ReadMcp": 7})])]
    row = _row(hook_health.measure_hook_overhead(results, [_spec("PostToolUse", "Read|Grep")]), "PostToolUse")
    # Bash is not in the list, and a plain list names tools exactly, so
    # ReadMcp is not Read.
    assert row.runs == 5


@pytest.mark.parametrize(
    ("matcher", "expected"),
    [("", 7), ("*", 7), ("Read", 1), ("Read|Bash", 4), ("Web.*", 2), ("^mcp__", 1), ("(unclosed", 0)],
)
def test_overhead_reads_a_matcher_as_claude_code_does(matcher, expected):
    # Empty or "*" selects everything; letters, digits, "_" and "|" name
    # tools exactly; anything else is a regular expression, and one that
    # doesn't compile selects nothing.
    results = [_main(turns=[_turn({"Read": 1, "Bash": 3, "WebFetch": 1, "WebSearch": 1, "mcp__x__y": 1})])]
    row = _row(hook_health.measure_hook_overhead(results, [_spec("PostToolUse", matcher)]), "PostToolUse")
    assert row.runs == expected


def test_overhead_leaves_out_calls_that_failed_because_they_run_a_different_event():
    results = [_main(turns=[_turn({"Read": 10, "Grep": 4}, errors={"Read": 3, "Grep": 9})])]
    row = _row(hook_health.measure_hook_overhead(results, [_spec("PostToolUse", "Read|Grep")]), "PostToolUse")
    # PostToolUseFailure ran for the 3 failed Reads; a count never goes
    # under zero.
    assert row.runs == 7


def test_overhead_adds_the_runs_of_two_entries_on_one_event():
    results = [_main(turns=[_turn({"Read": 5, "Grep": 2})])]
    row = _row(
        hook_health.measure_hook_overhead(results, [_spec("PostToolUse", "Read"), _spec("PostToolUse", "Grep")]),
        "PostToolUse",
    )
    assert row.runs == 7


def test_overhead_median_comes_from_recorded_durations_of_its_own_hook_only():
    results = [
        _main(
            turns=[_turn({"Read": 50})],
            events=[
                _call_event("PostToolUse", 10),
                _call_event("PostToolUse", 20),
                _call_event("PostToolUse", 600, subkind="hook_non_blocking_error"),  # waited for either way
                _call_event("PostToolUse", 9000, capture=False),  # someone else's hook
                _call_event("SessionStart", 9000),  # another event
                _call_event("PostToolUse", None),  # no duration recorded
                _call_event("PostToolUse", "slow"),
                _call_event("PostToolUse", True),
                Event(kind=EventKind.REMINDER, subkind="hook_success", detail={"hookName": "PostToolUse", "durationMs": 7777, "capture": True}),
            ],
        )
    ]
    row = _row(hook_health.measure_hook_overhead(results, [_spec("PostToolUse", "Read")]), "PostToolUse")
    assert (row.runs, row.recorded, row.median_ms) == (50, 3, 20.0)


def test_an_event_with_no_recorded_duration_has_unknown_time_not_zero():
    results = [_main(turns=[_turn(stop="end_turn"), _turn(stop="end_turn")])]
    overhead = hook_health.measure_hook_overhead(results, [_spec("Stop")])
    row = _row(overhead, "Stop")
    assert (row.runs, row.recorded) == (2, 0)
    assert row.median_ms is None
    assert row.summed_s is None
    assert overhead.median_ms is None
    assert overhead.timed == ()
    assert overhead.untimed == (row,)


def test_overhead_never_counts_fewer_runs_than_were_recorded():
    results = [_main(events=[_call_event("PostToolUse", 30), _call_event("PostToolUse", 50)])]
    row = _row(hook_health.measure_hook_overhead(results, [_spec("PostToolUse", "Read")]), "PostToolUse")
    assert (row.runs, row.recorded, row.median_ms) == (2, 2, 40.0)


def test_overhead_counts_user_prompts_in_main_sessions_only():
    prompts = [
        Event(kind=EventKind.HUMAN_TEXT),
        Event(kind=EventKind.SLASH_COMMAND),
        Event(kind=EventKind.TASK_NOTIFICATION),
        Event(kind=EventKind.AGENT_TERMINATED),
        Event(kind=EventKind.SCHEDULED_TASK),
        Event(kind=EventKind.LIMIT_RESUME),
        Event(kind=EventKind.PEER_MESSAGE),
        Event(kind=EventKind.QUEUE_OPERATION, subkind="queued_command"),
        Event(kind=EventKind.META, subkind="resume"),
    ]
    not_prompts = [
        Event(kind=EventKind.QUEUE_OPERATION, subkind="enqueue"),
        Event(kind=EventKind.META, subkind="other"),
        Event(kind=EventKind.REMINDER),
    ]
    results = [_main(events=[*prompts, *not_prompts]), _agent(events=[Event(kind=EventKind.HUMAN_TEXT)] * 4)]
    row = _row(hook_health.measure_hook_overhead(results, [_spec("UserPromptSubmit")]), "UserPromptSubmit")
    assert row.runs == len(prompts)


def test_overhead_counts_the_main_turns_that_ended_for_stop():
    results = [
        _main(
            turns=[
                _turn(stop="end_turn"),
                _turn(stop="max_tokens"),
                _turn(stop="tool_use"),  # carried on to a tool, no stop
                _turn(stop=None),  # cut off
                _turn(stop="end_turn", synthetic=True),  # not a model reply
            ]
        ),
        _agent(turns=[_turn(stop="end_turn")]),  # an agent ends with SubagentStop
    ]
    row = _row(hook_health.measure_hook_overhead(results, [_spec("Stop")]), "Stop")
    assert row.runs == 2


def test_overhead_counts_starts_and_compactions_for_session_start():
    compact = Event(kind=EventKind.COMPACT_BOUNDARY)
    results = [_main(events=[compact, compact]), _main(), _agent(events=[compact])]
    overhead = lambda matcher: _row(hook_health.measure_hook_overhead(results, [_spec("SessionStart", matcher)]), "SessionStart")
    assert overhead("").runs == 2 + 3
    assert overhead("startup|clear|compact").runs == 2 + 3
    assert overhead("startup|clear").runs == 2
    assert overhead("compact").runs == 3
    assert overhead("resume").runs == 0


def test_overhead_counts_sessions_for_session_end_and_agent_runs_for_subagent_events():
    results = [_main(), _main(), _agent("Explore"), _agent("general-purpose", kind="workflow-agent"), _agent("Plan")]
    overhead = hook_health.measure_hook_overhead(
        results,
        [_spec("SessionEnd"), _spec("SubagentStart"), _spec("SubagentStop", "Explore|Plan")],
    )
    assert _row(overhead, "SessionEnd").runs == 2
    assert _row(overhead, "SubagentStart").runs == 3
    assert _row(overhead, "SubagentStop").runs == 2


def test_overhead_keeps_an_event_it_cannot_count_with_its_recorded_runs_only():
    results = [_main(events=[_call_event("Notification", 80), _call_event("Notification", 120)])]
    overhead = hook_health.measure_hook_overhead(results, [_spec("Notification")])
    row = _row(overhead, "Notification")
    assert (row.counted, row.runs, row.recorded, row.median_ms) == (False, 2, 2, 100.0)
    # An uncounted event has no honest run total, so the sentence leaves it out.
    assert overhead.timed == () and overhead.untimed == ()
    assert overhead.summary() is None


def test_overhead_counts_only_the_runs_from_since_on():
    early = Turn(tool_calls_by_tool={"Bash": 1}, stop_reason="tool_use", ts="2026-09-18T09:00:00Z")
    late = Turn(tool_calls_by_tool={"Bash": 1}, stop_reason="tool_use", ts="2026-09-18T11:00:00Z")
    timed = Event(
        kind=EventKind.HOOK_OUTPUT, subkind="hook_success", ts="2026-09-18T08:00:00Z",
        detail={"hookName": "PostToolUse", "durationMs": 80, "capture": True},
    )  # fmt: skip
    results = [_main(turns=[early, late], events=[timed])]
    whole = _row(hook_health.measure_hook_overhead(results, [_spec("PostToolUse")]), "PostToolUse")
    cut = _row(
        hook_health.measure_hook_overhead(results, [_spec("PostToolUse")], since="2026-09-18T10:00:00+00:00"),
        "PostToolUse",
    )
    assert (whole.runs, whole.recorded) == (2, 1)
    assert (cut.runs, cut.recorded, cut.median_ms) == (1, 0, None)
    # A transcript with nothing from then on is left out, so it isn't a start either.
    later = hook_health.measure_hook_overhead(results, [_spec("SessionStart")], since="2026-09-19T00:00:00Z")
    assert _row(later, "SessionStart").runs == 0


def test_overhead_with_no_specs_or_no_transcripts_is_empty():
    assert hook_health.measure_hook_overhead([_main(turns=[_turn({"Read": 3})])], []).rows == ()
    assert hook_health.measure_hook_overhead([], []).summary() is None
    row = _row(hook_health.measure_hook_overhead([], [_spec("PostToolUse", "Read")]), "PostToolUse")
    assert (row.runs, row.recorded, row.median_ms) == (0, 0, None)
    assert hook_health.measure_hook_overhead([], [_spec("PostToolUse", "Read")]).summary() is None


def test_overhead_summary_wording():
    results = [
        _main(
            turns=[_turn({"Read": 2000})],
            events=[_call_event("PostToolUse", 50)] * 5,
        )
    ]
    text = hook_health.measure_hook_overhead(results, [_spec("PostToolUse", "Read")]).summary()
    # 2,000 runs at 50 ms is 100 s: 1.7 min.
    assert text == "ClaudeGlass's hooks ran about 2,000 times, about 50 ms each, about 1.7 min summed (calls overlap)."


def test_overhead_summary_rounds_large_counts_and_long_spans():
    rows = (hook_health.HookOverheadRow("PostToolUse", runs=26_602, recorded=484, median_ms=88.4),)
    # 26,602 x 88.4 ms = 2,352 s = 39 min.
    assert hook_health.HookOverhead(rows).summary() == (
        "ClaudeGlass's hooks ran about 26,600 times, about 88 ms each, about 39 min summed (calls overlap)."
    )


def test_overhead_summary_reads_small_amounts_plainly():
    one = (hook_health.HookOverheadRow("Stop", runs=1, recorded=1, median_ms=0.2),)
    assert hook_health.HookOverhead(one).summary() == (
        "ClaudeGlass's hooks ran once, under 1 ms each, under 1 s summed (calls overlap)."
    )
    few = (hook_health.HookOverheadRow("Stop", runs=12, recorded=3, median_ms=250.0),)
    assert hook_health.HookOverhead(few).summary() == (
        "ClaudeGlass's hooks ran about 12 times, about 250 ms each, about 3 s summed (calls overlap)."
    )


def test_overhead_summary_leaves_out_an_event_with_no_time_and_says_so():
    rows = (
        hook_health.HookOverheadRow("PostToolUse", runs=1000, recorded=10, median_ms=60.0),
        hook_health.HookOverheadRow("Stop", runs=40, recorded=0, median_ms=None),
        hook_health.HookOverheadRow("UserPromptSubmit", runs=55, recorded=0, median_ms=None),
        hook_health.HookOverheadRow("Notification", runs=2, recorded=2, median_ms=100.0, counted=False),
    )
    text = hook_health.HookOverhead(rows).summary()
    # Only the timed event is in the figures; the sentence names the rest
    # that ran without a recorded time (Notification can't be counted at all).
    assert text == (
        "ClaudeGlass's hooks ran about 1,000 times, about 60 ms each, about 1 min summed (calls overlap). "
        "Left out, with no run time recorded: Stop, UserPromptSubmit."
    )


def test_overhead_summary_does_not_mention_left_out_events_when_all_have_a_time():
    rows = (
        hook_health.HookOverheadRow("PostToolUse", runs=100, recorded=10, median_ms=60.0),
        hook_health.HookOverheadRow("Stop", runs=40, recorded=4, median_ms=100.0),
    )
    assert "Left out" not in hook_health.HookOverhead(rows).summary()


def test_overhead_summary_when_no_event_has_a_time():
    rows = (hook_health.HookOverheadRow("Stop", runs=40, recorded=0, median_ms=None),)
    assert hook_health.HookOverhead(rows).summary() == (
        "ClaudeGlass's hooks ran about 40 times. Claude Code recorded no run time for them."
    )
    once = (hook_health.HookOverheadRow("Stop", runs=1, recorded=0, median_ms=None),)
    assert hook_health.HookOverhead(once).summary() == "ClaudeGlass's hooks ran once. Claude Code recorded no run time for them."


def test_overhead_median_is_weighted_by_runs_per_event():
    def overhead(fast_runs: int, slow_runs: int) -> hook_health.HookOverhead:
        return hook_health.HookOverhead(
            (
                hook_health.HookOverheadRow("PostToolUse", runs=fast_runs, recorded=5, median_ms=40.0),
                hook_health.HookOverheadRow("UserPromptSubmit", runs=slow_runs, recorded=5, median_ms=900.0),
            )
        )

    assert overhead(90, 10).median_ms == 40.0
    assert overhead(10, 90).median_ms == 900.0
    assert overhead(90, 10).runs == 100
    assert overhead(90, 10).summed_s == pytest.approx(90 * 0.04 + 10 * 0.9)


def test_installed_specs_reads_the_capture_entries_in_settings(tmp_path):
    claude = tmp_path / "claude"
    claude.mkdir()
    wanted = (
        hook_health.HookSpec("capture-hook.py", "PostToolUse", "Read|Grep"),
        hook_health.HookSpec("capture-hook.py", "Stop"),
    )
    commands = {"capture-hook.py": '"python" -I -S "C:\\cg\\hooks\\capture-hook.py" --config-dir "C:\\cg"'}
    settings = {
        "hooks": {
            "PreToolUse": [{"hooks": [{"type": "command", "command": "other-tool"}]}],  # not ours
        }
    }
    (claude / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    hook_health.connect(hook_health.plan_capture(wanted, commands, claude_root=claude), now=NOW)
    assert set(hook_health.installed_specs(claude)) == set(wanted)


def test_installed_specs_is_empty_without_a_readable_settings_file(tmp_path):
    claude = tmp_path / "claude"
    claude.mkdir()
    assert hook_health.installed_specs(claude) == ()
    (claude / "settings.json").write_text("{not json", encoding="utf-8")
    assert hook_health.installed_specs(claude) == ()
    (claude / "settings.json").write_text("[]", encoding="utf-8")
    assert hook_health.installed_specs(claude) == ()


# -- settings policies that stop the user's own hooks running at all -------


def _policy_roots(tmp_path, *, user=None, managed=None, drop_in=None):
    claude_root = tmp_path / "claude"
    claude_root.mkdir()
    if user is not None:
        (claude_root / "settings.json").write_text(json.dumps(user), encoding="utf-8")
    managed_dir = tmp_path / "managed"
    managed_dir.mkdir()
    if managed is not None:
        (managed_dir / "managed-settings.json").write_text(json.dumps(managed), encoding="utf-8")
    if drop_in is not None:
        (managed_dir / "managed-settings.d").mkdir()
        (managed_dir / "managed-settings.d" / "10-hooks.json").write_text(json.dumps(drop_in), encoding="utf-8")
    return claude_root, managed_dir


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({}, None),
        ({"user": {"hooks": {}}}, None),
        ({"managed": {"allowManagedHooksOnly": True}}, hook_health.POLICY_MANAGED_ONLY),
        ({"managed": {"allowManagedHooksOnly": False}}, None),
        ({"drop_in": {"allowManagedHooksOnly": True}}, hook_health.POLICY_MANAGED_ONLY),
        ({"managed": {"disableAllHooks": True, "allowManagedHooksOnly": True}}, hook_health.POLICY_ALL_OFF_MANAGED),
        ({"user": {"disableAllHooks": True}}, hook_health.POLICY_ALL_OFF),
        ({"user": {"disableAllHooks": False}}, None),
    ],
)
def test_hook_policy_reads_managed_and_user_settings(tmp_path, kwargs, expected):
    claude_root, managed_dir = _policy_roots(tmp_path, **kwargs)
    assert hook_health.hook_policy(claude_root, managed_dir) == expected


def test_hook_policy_treats_an_unreadable_file_as_no_policy(tmp_path):
    claude_root, managed_dir = _policy_roots(tmp_path)
    (managed_dir / "managed-settings.json").write_text("{not json", encoding="utf-8")
    (claude_root / "settings.json").write_text("[1, 2]", encoding="utf-8")
    assert hook_health.hook_policy(claude_root, managed_dir) is None


def test_hook_policy_defaults_to_the_platform_managed_dir(tmp_path, monkeypatch):
    claude_root, managed_dir = _policy_roots(tmp_path, managed={"allowManagedHooksOnly": True})
    monkeypatch.setattr(hook_health, "managed_settings_dir", lambda: managed_dir)
    assert hook_health.hook_policy(claude_root) == hook_health.POLICY_MANAGED_ONLY


def test_capture_health_under_a_policy_is_not_ok_and_says_why(tmp_path):
    claude_root, managed_dir = _policy_roots(tmp_path, managed={"allowManagedHooksOnly": True})
    wanted = hook_health.capture_specs(["task"])
    health = hook_health.check_capture(wanted, claude_root=claude_root, managed_dir=managed_dir)
    assert health.blocked_by == hook_health.POLICY_MANAGED_ONLY
    assert not health.ok
    assert health.summary() == hook_health.POLICY_TEXT[hook_health.POLICY_MANAGED_ONLY]
    assert "capture connect" not in health.summary()


def test_capture_health_with_nothing_needed_ignores_the_policy(tmp_path):
    claude_root, managed_dir = _policy_roots(tmp_path, user={"disableAllHooks": True})
    health = hook_health.check_capture((), claude_root=claude_root, managed_dir=managed_dir)
    assert health.summary() == "No capture hooks are needed or installed."


def test_snapshot_hook_health_under_a_policy_is_not_ok_and_says_why(tmp_path):
    script = tmp_path / "hooks" / hook_health.HOOK_SCRIPT_NAME
    script.parent.mkdir()
    script.write_text("# hook", encoding="utf-8")
    command = f'"{sys.executable}" "{script}"'
    user = {"disableAllHooks": True, "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": command}]}]}}
    claude_root, managed_dir = _policy_roots(tmp_path, user=user)
    health = hook_health.check(tmp_path, claude_root=claude_root, managed_dir=managed_dir)
    assert health.command == command
    assert not health.ok
    assert health.fixed_command is None
    assert hook_health.POLICY_TEXT[hook_health.POLICY_ALL_OFF] in health.summary()


def test_hooks_block_under_a_policy_offers_no_connect_command(tmp_path):
    from claudeglass import capture_view

    claude_root, managed_dir = _policy_roots(tmp_path, managed={"disableAllHooks": True})
    health = hook_health.check_capture(hook_health.capture_specs(["task"]), claude_root=claude_root, managed_dir=managed_dir)
    block = capture_view.hooks_block(health)
    assert block["ok"] is False
    assert block["blocked_by"] == hook_health.POLICY_ALL_OFF_MANAGED
    assert block["summary"] == hook_health.POLICY_TEXT[hook_health.POLICY_ALL_OFF_MANAGED]
    assert capture_view.hooks_block(None)["blocked_by"] is None


# -- Phase 7: sessions by entrypoint, the corpus flattened ------------------


def test_terminal_sessions_counts_terminal_and_all_sessions():
    entrypoints = {
        "claude-desktop": {"count": 204, "last_ts": "2026-09-22"},
        "cli": {"count": 1, "last_ts": "2026-09-20"},
        "": {"count": 3, "last_ts": "2026-09-21"},
    }
    # A session whose entrypoint is unknown counts in the total only.
    assert hook_health.terminal_sessions(entrypoints) == (1, 208)
    assert hook_health.terminal_sessions({"claude-desktop": {"count": 5, "last_ts": "x"}}) == (0, 5)


def test_terminal_sessions_with_no_readings_is_zero_of_zero():
    assert hook_health.terminal_sessions({}) == (0, 0)
    assert hook_health.terminal_sessions(None) == (0, 0)


def test_corpus_results_flattens_each_session_and_skips_a_bundle_with_no_top():
    top, sub = _main(), _agent()
    other_top = _main()
    corpus = SimpleNamespace(
        sessions=[
            SimpleNamespace(top=top, subs=[sub]),
            SimpleNamespace(top=None, subs=[_agent()]),
            SimpleNamespace(top=other_top, subs=[]),
        ]
    )
    assert hook_health.corpus_results(corpus) == [top, sub, other_top]
    assert hook_health.corpus_results(SimpleNamespace(sessions=[])) == []

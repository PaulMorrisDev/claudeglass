"""Tests for ``src/claudeglass/hook_health.py``: finding the
SessionStart snapshot hook in ``settings.json``, spotting a Windows path
broken by JSON escaping, and repairing only that command after a backup
(also through ``init --repair-hook``)."""

from __future__ import annotations

import io
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from claudeglass import cli, helptext, hook_health, setup_flow
from claudeglass.model import Diagnostics, Event, EventKind, TranscriptResult

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


# -- CAP-9/F10: measure_deep_wait / DeepWaitStats ----------------------------


def _call_event(hook_name: str, duration_ms, *, capture: bool = True, subkind: str = "hook_success") -> Event:
    detail: dict = {"hookName": hook_name}
    if duration_ms is not None:
        detail["durationMs"] = duration_ms
    if capture:
        detail["capture"] = True
    return Event(kind=EventKind.HOOK_OUTPUT, subkind=subkind, detail=detail)


def test_measure_deep_wait_computes_median_and_p90():
    # 8 quick calls at 100ms, 2 slow ones at 900ms.
    durations = [100] * 8 + [900] * 2
    results = [_result(*[_call_event("PostToolUse", ms) for ms in durations])]
    stats = hook_health.measure_deep_wait(results)
    assert stats.calls == 10
    assert stats.median_ms == 100.0
    assert stats.p90_ms == 900.0


def test_measure_deep_wait_ignores_a_post_tool_use_call_without_the_capture_flag():
    # A third-party PostToolUse hook: no "capture" key at all.
    results = [_result(_call_event("PostToolUse", 500, capture=False))]
    assert hook_health.measure_deep_wait(results) == hook_health.DeepWaitStats()


def test_measure_deep_wait_ignores_own_hook_calls_under_a_different_event():
    # ClaudeGlass's own SessionStart/SubagentStart calls aren't Deep's wait.
    results = [_result(_call_event("SessionStart", 140))]
    assert hook_health.measure_deep_wait(results) == hook_health.DeepWaitStats()


def test_measure_deep_wait_counts_a_failed_call_too():
    # Claude Code waited for the hook to finish whether or not it errored.
    results = [_result(_call_event("PostToolUse", 300, subkind="hook_non_blocking_error"))]
    stats = hook_health.measure_deep_wait(results)
    assert stats.calls == 1
    assert stats.median_ms == 300.0


def test_measure_deep_wait_with_nothing_to_measure_returns_empty_stats():
    stats = hook_health.measure_deep_wait([_result()])
    assert stats == hook_health.DeepWaitStats()
    assert stats.summary() is None


def test_deep_wait_summary_wording():
    stats = hook_health.DeepWaitStats(calls=10, median_ms=100.0, p90_ms=900.0)
    text = stats.summary()
    assert text == (
        "Deep's large-output/web hook waited ≈0.1s (median, p90 ≈0.9s) over 10 calls this week."
    )


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

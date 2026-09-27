"""Switching metrics capture on and off: the ``capture`` command, the
settings.json entries it syncs (``hook_health.plan_capture``,
``check_capture``), and how ``changes`` and ``uninstall`` see them
(``footprint``). Every settings.json change is shown first, asked about,
and backed up; ``--dry-run`` writes nothing.
"""

from __future__ import annotations

import io
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from claudeglass import capture_catalogue as cat
from claudeglass import cli, footprint, hook_health, installer, setup_flow, signals
from claudeglass.config import CAPTURE_LOG_NAME, SIGNAL_RETENTION_DEFAULT_DAYS, CaptureConfig, load_capture_log, load_config

NOW = datetime(2026, 9, 24, 6, 0, tzinfo=timezone.utc)
ESSENTIALS = hook_health.capture_specs(cat.level_metrics("essentials"))
DEEP = hook_health.capture_specs(cat.level_metrics("deep"))


@pytest.fixture(autouse=True)
def _claude_folder(tmp_path, monkeypatch):
    """settings.json lives in ``<tmp>/claude`` (``$CLAUDE_CONFIG_DIR``),
    never in the real ~/.claude."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setattr(installer, "is_registered", lambda *a, **k: False)


def _claude(tmp_path, settings=None):
    claude = tmp_path / "claude"
    config_dir = claude / "claudeglass"
    config_dir.mkdir(parents=True)
    if settings is not None:
        (claude / "settings.json").write_text(json.dumps(settings, indent=2), encoding="utf-8")
    return config_dir


def _settings(config_dir) -> dict:
    return json.loads((config_dir.parent / "settings.json").read_text(encoding="utf-8"))


def _commands(config_dir) -> dict[str, str]:
    return cli._capture_hook_commands(config_dir)


def _capture(config_dir, *argv, stdin="", now=NOW):
    """Run ``capture`` through the real parser; returns (rc, output)."""
    args = cli._make_parser().parse_args(["capture", *argv, "--config-dir", str(config_dir)])
    out = io.StringIO()
    rc = cli._cmd_capture(args, stdin=io.StringIO(stdin), stdout=out, now=now)
    return rc, out.getvalue()


def _entries(settings: dict) -> list[tuple[str, str, dict]]:
    found = []
    for event, groups in settings.get("hooks", {}).items():
        for group in groups:
            for entry in group["hooks"]:
                found.append((event, group.get("matcher", ""), entry))
    return found


# -- hook_health: syncing the capture entries ------------------------------


def test_plan_capture_adds_the_entries_a_level_needs_and_writes_nothing(tmp_path):
    config_dir = _claude(tmp_path, {"model": "opus"})
    before = (config_dir.parent / "settings.json").read_text(encoding="utf-8")
    plan = hook_health.plan_capture(DEEP, _commands(config_dir))
    assert (config_dir.parent / "settings.json").read_text(encoding="utf-8") == before
    after = json.loads(plan.new_text)
    entries = _entries(after)
    assert [(event, matcher) for event, matcher, _ in entries] == [
        ("SessionStart", "startup|clear|compact"),
        ("SubagentStop", ""),
        ("PostToolUse", "Bash|Read|Grep|Glob|WebFetch|WebSearch|mcp__.*"),
        ("SessionEnd", ""),
        ("Notification", ""),
        ("PermissionRequest", ""),
        ("Stop", ""),
        ("StopFailure", ""),
    ]
    assert all(entry["timeout"] == 5 for _, _, entry in entries)
    # An async hook's additionalContext reaches Claude only on the next
    # turn (V6b), so every entry that adds a note stays foreground. SIG-3's
    # Stop/StopFailure signal lines add no note, so they run in the
    # background like the other free signals.
    assert [entry.get("async", False) for _, _, entry in entries] == [
        False, False, False, False, True, True, True, True,
    ]
    assert after["model"] == "opus"
    assert len(plan.changes) == 8 and all(line.startswith("Add the capture hook") for line in plan.changes)
    assert "Add the capture hook that runs capture-hook.py when Claude waits for you, in the background." in plan.changes


def test_plan_capture_is_a_no_op_once_connected(tmp_path):
    config_dir = _claude(tmp_path, {})
    hook_health.install_hook_files(config_dir, hook_health.CAPTURE_FILES[cat.HOOK_SCRIPT])
    hook_health.connect(hook_health.plan_capture(ESSENTIALS, _commands(config_dir)), now=NOW)
    again = hook_health.plan_capture(ESSENTIALS, _commands(config_dir))
    assert again.new_text is None and again.changes == []
    assert hook_health.check_capture(ESSENTIALS).ok


def test_a_changed_command_is_updated_in_place_not_duplicated(tmp_path):
    config_dir = _claude(tmp_path, {})
    old = {cat.HOOK_SCRIPT: '"/old/python" "/old/hooks/capture-hook.py"'}
    hook_health.connect(hook_health.plan_capture(ESSENTIALS, old), now=NOW)
    plan = hook_health.plan_capture(ESSENTIALS, _commands(config_dir))
    assert all(line.startswith("Update the capture hook") for line in plan.changes)
    entries = _entries(json.loads(plan.new_text))
    assert len(entries) == len(ESSENTIALS) and all("/old/python" not in entry["command"] for _, _, entry in entries)


def test_lowering_the_level_takes_out_entries_no_metric_needs(tmp_path):
    config_dir = _claude(tmp_path, {})
    hook_health.connect(hook_health.plan_capture(DEEP, _commands(config_dir)), now=NOW)
    plan = hook_health.plan_capture(ESSENTIALS, _commands(config_dir))
    assert plan.changes == ["Remove the capture hook that runs capture-hook.py after shell, read, search, web and MCP results."]
    assert "PostToolUse" not in json.loads(plan.new_text)["hooks"]


def test_other_hooks_are_never_touched(tmp_path):
    mine = {"type": "command", "command": "echo mine"}
    config_dir = _claude(tmp_path, {"hooks": {"SessionStart": [{"matcher": "startup", "hooks": [mine]}], "Stop": [{"hooks": [mine]}]}})
    hook_health.connect(hook_health.plan_capture(DEEP, _commands(config_dir)), now=NOW)
    removal = hook_health.plan_capture((), {})
    after = json.loads(removal.new_text)
    assert after == {"hooks": {"SessionStart": [{"matcher": "startup", "hooks": [mine]}], "Stop": [{"hooks": [mine]}]}}


def test_a_capture_entry_sharing_a_group_leaves_the_rest_of_the_group(tmp_path):
    mine = {"type": "command", "command": "echo mine"}
    config_dir = _claude(tmp_path, {})
    capture_entry = {"type": "command", "command": _commands(config_dir)[cat.HOOK_SCRIPT], "timeout": 5}
    (config_dir.parent / "settings.json").write_text(
        json.dumps({"hooks": {"SubagentStart": [{"hooks": [mine, capture_entry]}]}}), encoding="utf-8"
    )
    assert hook_health.check_capture(ESSENTIALS).missing == tuple(s for s in ESSENTIALS if s.event != "SubagentStart")
    after = json.loads(hook_health.plan_capture((), {}).new_text)
    assert after == {"hooks": {"SubagentStart": [{"hooks": [mine]}]}}


def test_a_settings_file_of_an_odd_shape_is_left_alone(tmp_path):
    config_dir = _claude(tmp_path, {"hooks": ["not", "a", "table"]})
    plan = hook_health.plan_capture(ESSENTIALS, _commands(config_dir))
    assert plan.new_text is None
    (config_dir.parent / "settings.json").write_text("{broken", encoding="utf-8")
    assert "could not be read" in hook_health.plan_capture(ESSENTIALS, _commands(config_dir)).changes[0]


def test_check_capture_names_missing_extra_and_broken_entries(tmp_path):
    config_dir = _claude(tmp_path, {})
    health = hook_health.check_capture(ESSENTIALS)
    assert health.missing == ESSENTIALS and not health.ok
    assert "capture connect" in health.summary()
    hook_health.connect(hook_health.plan_capture(DEEP, _commands(config_dir)), now=NOW)
    health = hook_health.check_capture(ESSENTIALS)
    assert not health.missing and health.extra == tuple(spec for spec in DEEP if spec not in ESSENTIALS)
    # The script isn't installed yet, so the entries point at nothing.
    assert any("does not exist" in problem for problem in health.problems)
    hook_health.install_hook_files(config_dir, hook_health.CAPTURE_FILES[cat.HOOK_SCRIPT])
    health = hook_health.check_capture(ESSENTIALS)
    assert health.ok and "add nothing" in health.summary()


def test_check_capture_spots_a_percent_variable(tmp_path):
    config_dir = _claude(tmp_path, {})
    hook_health.install_hook_files(config_dir, hook_health.CAPTURE_FILES[cat.HOOK_SCRIPT])
    commands = {cat.HOOK_SCRIPT: _commands(config_dir)[cat.HOOK_SCRIPT] + " --x %USERPROFILE%"}
    hook_health.connect(hook_health.plan_capture(ESSENTIALS, commands), now=NOW)
    assert any("%VARIABLE%" in problem for problem in hook_health.check_capture(ESSENTIALS).problems)


# -- hook_health: SEC-P7/ROB-P7 hash-stamping -------------------------------


def _packaged(name: str) -> bytes:
    from importlib import resources

    return (resources.files("claudeglass") / "hooks" / name).read_bytes()


def _connect_essentials(config_dir):
    hook_health.install_hook_files(config_dir, hook_health.CAPTURE_FILES[cat.HOOK_SCRIPT])
    hook_health.connect(hook_health.plan_capture(ESSENTIALS, _commands(config_dir)), now=NOW)


def test_check_capture_with_config_dir_is_ok_right_after_install(tmp_path):
    config_dir = _claude(tmp_path, {})
    _connect_essentials(config_dir)
    health = hook_health.check_capture(ESSENTIALS, config_dir=config_dir)
    assert health.ok and not health.outdated and not health.modified


def test_check_capture_flags_a_hand_edited_hook_file_as_modified(tmp_path):
    config_dir = _claude(tmp_path, {})
    _connect_essentials(config_dir)
    (config_dir / "hooks" / cat.HOOK_SCRIPT).write_text("# someone edited this by hand\n", encoding="utf-8")
    health = hook_health.check_capture(ESSENTIALS, config_dir=config_dir)
    # Every entry runs the one shared script file, so all of them are affected.
    assert health.modified == ESSENTIALS
    assert "edited by hand" in health.summary()


def test_check_capture_flags_its_own_older_copy_as_outdated(tmp_path):
    import hashlib

    config_dir = _claude(tmp_path, {})
    _connect_essentials(config_dir)
    script_path = config_dir / "hooks" / cat.HOOK_SCRIPT
    old = b"# an older copy this tool itself wrote\n"
    script_path.write_bytes(old)
    manifest_path = config_dir / "hooks" / ".manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest[cat.HOOK_SCRIPT] = hashlib.sha256(old).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    health = hook_health.check_capture(ESSENTIALS, config_dir=config_dir)
    assert health.outdated == ESSENTIALS
    assert "older copy" in health.summary()


def test_check_capture_reports_a_missing_catalogue(tmp_path):
    config_dir = _claude(tmp_path, {})
    _connect_essentials(config_dir)
    (config_dir / "hooks" / cat.CATALOGUE_FILE).unlink()
    health = hook_health.check_capture(ESSENTIALS, config_dir=config_dir)
    assert any("catalogue" in p and "missing" in p for p in health.problems)


def test_check_capture_python_version_is_off_by_default_and_bounded_when_asked(tmp_path, monkeypatch):
    config_dir = _claude(tmp_path, {})
    _connect_essentials(config_dir)
    calls = []

    def _fake(program):
        calls.append(program)
        return (3, 9)

    monkeypatch.setattr(hook_health, "_interpreter_version", _fake)
    assert hook_health.check_capture(ESSENTIALS).ok and calls == []  # not spawned unless asked
    health = hook_health.check_capture(ESSENTIALS, check_python=True)
    assert any("older than 3.11" in p for p in health.problems)
    assert calls == [hook_health.stable_python()]  # one interpreter shared by every entry: checked once


def test_install_hook_files_skips_a_file_whose_replace_fails(tmp_path, monkeypatch):
    config_dir = _claude(tmp_path, {})
    real_replace = os.replace

    def _boom(src, dst):
        if Path(dst).name == cat.CATALOGUE_FILE:
            raise OSError("locked")
        return real_replace(src, dst)

    monkeypatch.setattr(hook_health.os, "replace", _boom)
    written = hook_health.install_hook_files(config_dir, hook_health.CAPTURE_FILES[cat.HOOK_SCRIPT])
    assert [p.name for p in written] == [cat.HOOK_SCRIPT]
    assert not (config_dir / "hooks" / cat.CATALOGUE_FILE).exists()
    assert not list((config_dir / "hooks").glob("*.tmp"))


def test_refresh_hook_files_with_nothing_installed_is_a_no_op(tmp_path):
    config_dir = _claude(tmp_path, {})
    assert hook_health.refresh_hook_files(config_dir) == []


def test_refresh_hook_files_fixes_outdated_but_leaves_modified(tmp_path):
    import hashlib

    config_dir = _claude(tmp_path, {})
    _connect_essentials(config_dir)
    script_path = config_dir / "hooks" / cat.HOOK_SCRIPT
    old = b"# an older copy\n"
    script_path.write_bytes(old)
    manifest_path = config_dir / "hooks" / ".manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest[cat.HOOK_SCRIPT] = hashlib.sha256(old).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    catalogue_path = config_dir / "hooks" / cat.CATALOGUE_FILE
    catalogue_path.write_bytes(b"tampered")

    refreshed = hook_health.refresh_hook_files(config_dir)
    assert [p.name for p in refreshed] == [cat.HOOK_SCRIPT]
    assert script_path.read_bytes() == _packaged(cat.HOOK_SCRIPT)
    assert catalogue_path.read_bytes() == b"tampered"


def test_refresh_hook_files_self_heals_a_manifest_that_predates_it(tmp_path):
    config_dir = _claude(tmp_path, {})
    hooks_dir = config_dir / "hooks"
    hooks_dir.mkdir(parents=True)
    for name in hook_health.CAPTURE_FILES[cat.HOOK_SCRIPT]:
        (hooks_dir / name).write_bytes(_packaged(name))
    assert not (hooks_dir / ".manifest.json").exists()

    assert hook_health.refresh_hook_files(config_dir) == []  # already current: nothing to rewrite
    manifest = json.loads((hooks_dir / ".manifest.json").read_text(encoding="utf-8"))
    assert all(name in manifest for name in hook_health.CAPTURE_FILES[cat.HOOK_SCRIPT])


def test_capture_settings_step_refuses_an_unsafe_hook_command(tmp_path, monkeypatch):
    # ROB-P9: hook_health.hook_command returning None (an unsafe Python
    # or config-dir path) must never reach settings.json as a broken
    # "command": null entry.
    config_dir = _claude(tmp_path, {})
    monkeypatch.setattr(hook_health, "hook_command", lambda *a, **k: None)
    out = io.StringIO()
    done = cli._capture_settings_step(
        ESSENTIALS,
        config_dir=config_dir,
        claude_root=config_dir.parent,
        dry_run=False,
        assume_yes=True,
        stdin=io.StringIO(""),
        stdout=out,
    )
    assert done is False
    assert "Could not build a safe capture hook command" in out.getvalue()
    assert not _settings(config_dir).get("hooks")


def test_refresh_hook_files_treats_a_pre_manifest_file_as_outdated_not_modified(tmp_path):
    config_dir = _claude(tmp_path, {})
    hooks_dir = config_dir / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / cat.HOOK_SCRIPT).write_bytes(b"# installed before the manifest existed\n")
    (hooks_dir / cat.CATALOGUE_FILE).write_bytes(_packaged(cat.CATALOGUE_FILE))

    refreshed = hook_health.refresh_hook_files(config_dir)
    assert [p.name for p in refreshed] == [cat.HOOK_SCRIPT]
    assert (hooks_dir / cat.HOOK_SCRIPT).read_bytes() == _packaged(cat.HOOK_SCRIPT)


def test_backups_made_in_the_same_second_never_overwrite_each_other(tmp_path):
    config_dir = _claude(tmp_path, {"model": "opus"})
    first = hook_health.connect(hook_health.plan_capture(ESSENTIALS, _commands(config_dir)), now=NOW)
    second = hook_health.connect(hook_health.plan_capture((), {}), now=NOW)
    assert first.name == "settings.json.bak-20260924T060000Z"
    assert second.name == "settings.json.bak-20260924T060000Z-2"
    assert json.loads(first.read_text(encoding="utf-8")) == {"model": "opus"}


def test_hook_files_are_copied_from_the_package(tmp_path):
    from importlib import resources

    written = hook_health.install_hook_files(tmp_path, [cat.HOOK_SCRIPT, cat.CATALOGUE_FILE, hook_health.HOOK_SCRIPT_NAME])
    for path in written:
        packaged = resources.files("claudeglass") / "hooks" / path.name
        assert path.read_bytes() == packaged.read_bytes()
    # SEC-P7/ROB-P7: a SHA-256 manifest of what was just written sits alongside.
    installed = {p.name for p in (tmp_path / "hooks").iterdir()} - {".manifest.json"}
    assert sorted(installed) == sorted(p.name for p in written)
    manifest = json.loads((tmp_path / "hooks" / ".manifest.json").read_text(encoding="utf-8"))
    assert set(manifest) == {p.name for p in written}


def test_the_snapshot_hook_is_installed_from_the_package_too(tmp_path):
    hook = cli._load_snapshot_hook_module()
    dest = hook.install_hook(tmp_path)
    assert dest == tmp_path / "hooks" / hook_health.HOOK_SCRIPT_NAME and dest.is_file()


# -- the capture command ---------------------------------------------------


def test_status_when_off(tmp_path):
    config_dir = _claude(tmp_path)
    rc, out = _capture(config_dir)
    assert rc == 0 and out.startswith("Metrics capture: Off")
    assert "capture level off|free|essentials|standard|deep" in out


def test_on_dry_run_shows_the_cost_and_the_diff_and_writes_nothing(tmp_path):
    config_dir = _claude(tmp_path, {"model": "opus"})
    before = (config_dir.parent / "settings.json").read_text(encoding="utf-8")
    rc, out = _capture(config_dir, "on", "--dry-run", stdin="y\ny\n")
    assert rc == 0
    assert "Off -> Essentials" in out
    assert "This makes Claude use more of your tokens" in out
    assert f"about {cat.rough_tokens(cat.level_metrics('essentials'))['session_note']} tokens of note" in out
    assert "Dry run: config.toml left unchanged." in out and "Dry run: settings.json left unchanged." in out
    assert "+" in out and "capture-hook.py" in out
    assert (config_dir.parent / "settings.json").read_text(encoding="utf-8") == before
    assert not (config_dir / "config.toml").exists() and not (config_dir / "hooks").exists()


def test_a_no_to_the_cost_question_changes_nothing(tmp_path):
    config_dir = _claude(tmp_path, {})
    rc, out = _capture(config_dir, "on", stdin="n\n")
    assert rc == 1 and "Left unchanged." in out
    assert not (config_dir / "config.toml").exists()


def test_a_no_to_the_settings_change_keeps_the_level_and_says_how_to_connect(tmp_path):
    config_dir = _claude(tmp_path, {})
    rc, out = _capture(config_dir, "on", "--level", "standard", stdin="y\nn\n")
    assert rc == 0
    assert load_config(config_dir=config_dir).capture.level == "standard"
    assert _settings(config_dir) == {}
    assert "claudeglass capture connect" in out and "can't be captured" in out


def test_on_yes_connects_everything_and_a_second_run_changes_nothing(tmp_path):
    config_dir = _claude(tmp_path, {"model": "opus"})
    rc, out = _capture(config_dir, "on", "--yes", "--sample", "25")
    assert rc == 0 and "Restart Claude Code" in out
    capture = load_config(config_dir=config_dir).capture
    assert (capture.level, capture.sample, capture.enabled_at) == ("essentials", 25, "2026-09-24T06:00:00+00:00")
    assert (config_dir / "hooks" / cat.HOOK_SCRIPT).is_file() and (config_dir / "hooks" / cat.CATALOGUE_FILE).is_file()
    assert len((config_dir / "salt").read_bytes()) == 32  # the free signals hash session ids with it
    assert hook_health.check_capture(ESSENTIALS).ok
    rc, out = _capture(config_dir, "on", "--yes")
    assert "already Essentials" in out and "already runs the capture hooks" in out


def test_levels_up_and_down_sync_the_entries(tmp_path):
    config_dir = _claude(tmp_path, {})
    _capture(config_dir, "on", "--yes")
    _capture(config_dir, "level", "deep", "--yes")
    assert hook_health.check_capture(DEEP).ok
    rc, out = _capture(config_dir, "level", "essentials", "--yes")
    assert "This makes Claude use more" not in out  # lowering asks nothing about cost
    assert [e for e, _, _ in _entries(_settings(config_dir))] == [spec.event for spec in ESSENTIALS]


def test_disabling_a_metric_takes_what_needs_it_along(tmp_path):
    config_dir = _claude(tmp_path, {})
    _capture(config_dir, "level", "standard", "--yes")
    rc, out = _capture(config_dir, "disable", "result", "--yes")
    assert rc == 0 and "retry, fit, agent_brief need result, so they go too." in out
    capture = load_config(config_dir=config_dir).capture
    assert capture.level == "custom" and not {"result", "retry", "fit", "agent_brief"} & set(capture.metrics)


def test_enabling_one_metric_brings_what_it_needs(tmp_path):
    config_dir = _claude(tmp_path, {})
    rc, out = _capture(config_dir, "enable", "fit", "--yes")
    assert rc == 0 and "Off -> Custom" in out
    assert load_config(config_dir=config_dir).capture.metrics == ["result", "fit"]


def test_feedback_toggles_are_switched_on_their_own_list(tmp_path):
    config_dir = _claude(tmp_path, {})
    _capture(config_dir, "enable", "feedback_skill", "feedback_note", "--yes")
    capture = load_config(config_dir=config_dir).capture
    assert capture.feedback == ["feedback_skill", "feedback_note"] and capture.level == "off"
    _capture(config_dir, "disable", "feedback_note", "--yes")
    assert load_config(config_dir=config_dir).capture.feedback == ["feedback_skill"]


def test_level_deep_turns_on_the_feedback_survey_and_writes_its_skill(tmp_path):
    config_dir = _claude(tmp_path, {})
    rc, out = _capture(config_dir, "level", "deep", "--yes")
    assert rc == 0 and "Deep also turns on the /tl-feedback survey" in out
    assert "feedback_reminder" in out  # priced with the rest of what Deep adds
    assert _skill(config_dir).read_text(encoding="utf-8") == cat.feedback_skill_text()
    assert load_config(config_dir=config_dir).capture.feedback == list(cat.DEEP_FEEDBACK_IDS)
    _capture(config_dir, "feedback", "off", "--yes")
    assert not _skill(config_dir).exists()
    rc, out = _capture(config_dir, "level", "deep", "--yes")
    assert "Metrics capture is already Deep" in out and not _skill(config_dir).exists()


def test_level_deep_dry_run_shows_the_skill_and_writes_nothing(tmp_path):
    config_dir = _claude(tmp_path, {})
    rc, out = _capture(config_dir, "on", "--level", "deep", "--dry-run")
    assert "Deep also turns on the /tl-feedback survey" in out
    assert "Dry run: config.toml left unchanged." in out and "Dry run: the skill is left as it is." in out
    assert not _skill(config_dir).exists() and not (config_dir / "config.toml").exists()


@pytest.mark.parametrize("argv, message", [
    (["enable", "mood"], "unknown metric mood"),
    (["enable"], "needs one or more metric ids"),
    (["enable", "prompt_features"], "always measured"),
    (["level", "max"], "'capture level' needs one of"),
    (["on", "--for", "soon"], "--for 'soon'"),
    (["on", "--for", "0d"], "--for '0d'"),
    (["on", "--for", "7d", "--until", "2026-10-01"], "not more than one"),
    (["on", "--until", "next week", "--yes"], "'capture.until'"),
    (["on", "--for", "7d", "--no-limit"], "not more than one"),
    (["on", "--until", "2026-10-01", "--no-limit"], "not more than one"),
])
def test_bad_arguments_are_named_and_change_nothing(tmp_path, argv, message):
    config_dir = _claude(tmp_path, {})
    rc, out = _capture(config_dir, *argv)
    assert rc == 2 and message in out
    assert not (config_dir / "config.toml").exists()


def test_for_sets_the_end_time(tmp_path):
    config_dir = _claude(tmp_path, {})
    _capture(config_dir, "on", "--for", "2w", "--yes")
    capture = load_config(config_dir=config_dir).capture
    assert capture.until == (NOW + timedelta(weeks=2)).isoformat(timespec="seconds")
    rc, out = _capture(config_dir)
    assert "until 2026-10-08 06:00" in out


def test_a_fresh_on_with_neither_flag_gets_the_default_time_box(tmp_path):
    # CAP-8: 'capture on' with none of --for/--until/--no-limit gets the
    # same default time-box as init, so it can't run forever unnoticed.
    config_dir = _claude(tmp_path, {})
    _capture(config_dir, "on", "--yes")
    capture = load_config(config_dir=config_dir).capture
    assert capture.until == "2026-10-08T06:00:00+00:00"


def test_no_limit_switches_on_with_no_time_box(tmp_path):
    config_dir = _claude(tmp_path, {})
    _capture(config_dir, "on", "--no-limit", "--yes")
    assert load_config(config_dir=config_dir).capture.until == ""


def test_changing_level_while_already_on_does_not_impose_a_surprise_time_box(tmp_path):
    # CAP-8: the default only applies to a fresh off -> on switch --
    # bumping the level of capture that's already on and unlimited must
    # not silently grow a new end date it never had.
    config_dir = _claude(tmp_path, {})
    _capture(config_dir, "on", "--no-limit", "--yes")
    _capture(config_dir, "level", "deep", "--yes")
    assert load_config(config_dir=config_dir).capture.until == ""


def test_off_keeps_the_entries_and_remove_takes_them_out(tmp_path):
    mine = {"type": "command", "command": "echo mine"}
    config_dir = _claude(tmp_path, {"hooks": {"Stop": [{"hooks": [mine]}]}})
    _capture(config_dir, "on", "--yes")
    rc, out = _capture(config_dir, "off")
    assert rc == 0 and "add nothing while capture is off" in out and "capture remove" in out
    assert load_config(config_dir=config_dir).capture == CaptureConfig(level="off")
    assert len(_entries(_settings(config_dir))) == len(ESSENTIALS) + 1  # and the Stop entry of their own
    rc, out = _capture(config_dir, "remove", "--dry-run")
    assert "Dry run: settings.json left unchanged. Run 'claudeglass capture remove'" in out
    rc, out = _capture(config_dir, "remove", "--yes")
    assert _settings(config_dir) == {"hooks": {"Stop": [{"hooks": [mine]}]}}


def test_connect_while_off_says_nothing_is_needed(tmp_path):
    config_dir = _claude(tmp_path, {})
    rc, out = _capture(config_dir, "connect")
    assert rc == 0 and "no hook entries are needed" in out and _settings(config_dir) == {}


def test_status_reports_hooks_that_are_missing(tmp_path):
    config_dir = _claude(tmp_path, {})
    _capture(config_dir, "on", stdin="y\nn\n")
    rc, out = _capture(config_dir, "status")
    # CAP-8: a fresh switch-on with no --for/--until/--no-limit gets the
    # default time-box.
    assert "Metrics capture: Essentials (since 2026-09-24, until 2026-10-08 06:00)" in out
    assert "Hooks: settings.json does not run capture-hook.py" in out


def test_a_bad_config_is_reported_not_overwritten(tmp_path):
    config_dir = _claude(tmp_path, {})
    (config_dir / "config.toml").write_text('[capture]\nlevel = "max"\n', encoding="utf-8")
    rc, out = _capture(config_dir, "on", "--yes")
    assert rc == 2 and "config.toml has a problem" in out
    assert (config_dir / "config.toml").read_text(encoding="utf-8") == '[capture]\nlevel = "max"\n'


def test_a_data_folder_elsewhere_is_passed_to_the_hook(tmp_path):
    _claude(tmp_path, {})
    elsewhere = tmp_path / "data"
    elsewhere.mkdir()
    _capture(elsewhere, "on", "--yes")
    commands = [entry["command"] for _, _, entry in _entries(json.loads((tmp_path / "claude" / "settings.json").read_text(encoding="utf-8")))]
    assert commands and all(f'--config-dir "{elsewhere}"' in command for command in commands)


# -- changes and uninstall -------------------------------------------------


def test_expectations_say_capture_uses_tokens_only_while_it_does():
    assert footprint.expectations(None) == footprint.EXPECTATIONS
    assert footprint.expectations(CaptureConfig()) == footprint.EXPECTATIONS
    on = footprint.expectations(CaptureConfig(level="standard"))
    assert on[0][0] == "It uses a few of your Claude tokens while capture is on" and "(Standard)" in on[0][1]
    assert on[1:] == footprint.EXPECTATIONS[1:]
    free = footprint.expectations(CaptureConfig(level="free"))
    assert "adds no tokens" in free[0][1] and "(Free)" in free[0][1]


def test_inventory_lists_the_capture_hooks_only_when_there_are_any(tmp_path):
    config_dir = _claude(tmp_path, {})
    assert "capture_hooks" not in {item.key for item in footprint.inventory(config_dir, service_registered=False)}
    _capture(config_dir, "on", "--yes")
    items = {item.key: item for item in footprint.inventory(config_dir, service_registered=False)}
    item = items["capture_hooks"]
    assert item.status == "installed" and item.title == "Metrics capture hooks (7 entries)"
    assert "Essentials" in item.token_cost and "capture off" in item.undo
    _capture(config_dir, "level", "free", "--yes")
    item = {item.key: item for item in footprint.inventory(config_dir, service_registered=False)}["capture_hooks"]
    # SIG-3: turn_signals is a "free" group metric like the other three
    # signals, so its Stop/StopFailure entries are included here too.
    assert item.title == "Metrics capture hooks (5 entries)" and item.token_cost.startswith("None at Free")


def test_uninstall_takes_out_the_capture_entries(tmp_path):
    config_dir = _claude(tmp_path, {})
    _capture(config_dir, "on", "--yes")
    plan = footprint.plan_uninstall(config_dir)
    assert plan.settings_changes == [
        "Remove the capture hook that runs capture-hook.py when a session starts, is cleared or compacts.",
        "Remove the capture hook that runs capture-hook.py when a subagent finishes.",
        "Remove the capture hook that runs capture-hook.py when a session ends.",
        "Remove the capture hook that runs capture-hook.py when Claude waits for you, in the background.",
        "Remove the capture hook that runs capture-hook.py when Claude asks for permission, in the background.",
        "Remove the capture hook that runs capture-hook.py when a turn ends.",
        "Remove the capture hook that runs capture-hook.py when a turn ends in an API error.",
    ]
    assert json.loads(plan.new_settings_text) == {}


def test_changes_prints_the_capture_expectation(tmp_path, capsys):
    config_dir = _claude(tmp_path, {})
    _capture(config_dir, "on", "--yes")
    rc = cli.main(["changes", "--config-dir", str(config_dir)])
    out = capsys.readouterr().out
    assert rc == 0 and "Metrics capture hooks (7 entries): installed" in out
    assert "It uses a few of your Claude tokens while capture is on" in out


# -- what it costs: capture status, and the init question -------------------------


def _session(config_dir, *, captured: bool = False, days_ago: float = 1, name: str = "s1"):
    """One recent session under ``<tmp>/claude/projects`` (the projects
    root while ``$CLAUDE_CONFIG_DIR`` points there): a message and a
    tagged reply, with the Essentials note first when ``captured``."""
    from helpers import attachment_line, turn_line, user_str_line, write_jsonl

    start = datetime.now(timezone.utc) - timedelta(days=days_ago)

    def ts(second):
        return (start + timedelta(seconds=second)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    lines = []
    if captured:
        text = cat.note_text(cat.level_metrics("essentials"), "main")
        note = attachment_line(
            "hook_additional_context",
            rendered=f"<system-reminder>\nSessionStart hook additional context: {text}\n</system-reminder>",
            content=[text], hookName="SessionStart", hookEvent="SessionStart", toolUseID="SessionStart",
        )
        note["timestamp"] = ts(0)
        lines.append(note)
    lines += [
        user_str_line("fix the failing test", origin={"kind": "human"}, timestamp=ts(1)),
        turn_line(content=[{"type": "text", "text": "Fixed.\n[tl: task=bugfix brief=clear]"}], timestamp=ts(2),
                  input_tokens=2000, output_tokens=400),
    ]
    project = config_dir.parent / "projects" / "C--work-app"
    project.mkdir(parents=True, exist_ok=True)
    write_jsonl(project / f"{name}.jsonl", lines)
    return start


def _api_billing(config_dir):
    (config_dir / "config.toml").write_text('billing = "api"\n', encoding="utf-8")


def test_status_while_off_estimates_each_level_from_your_sessions(tmp_path):
    config_dir = _claude(tmp_path, {})
    _api_billing(config_dir)
    _session(config_dir)
    rc, out = _capture(config_dir, "status")
    assert rc == 0
    assert "What each level would have cost over your last 14 days (1 session, 0 subagents):" in out
    assert "Free        nothing: it only logs a few events to a local file" in out
    for title in ("Essentials", "Standard", "Deep"):
        line = next(line for line in out.splitlines() if line.strip().startswith(title))
        assert "tokens and" in line and "a week" in line and "of what you spent" in line


def test_status_while_on_shows_what_it_measured(tmp_path):
    config_dir = _claude(tmp_path, {})
    _api_billing(config_dir)
    start = _session(config_dir, captured=True)
    from claudeglass.config import set_capture

    set_capture(config_dir, level="essentials", now=start - timedelta(hours=1))
    rc, out = _capture(config_dir, "status")
    assert f"Measured since {(start - timedelta(hours=1)).date().isoformat()}: 1 session and 0 subagents captured" in out
    assert "tokens of note and" in out and "of what those sessions cost" in out
    assert "Claude tagged 100.0% of your messages" in out


def test_status_while_on_before_any_captured_session_says_so(tmp_path):
    config_dir = _claude(tmp_path, {})
    _session(config_dir, days_ago=2)
    _capture(config_dir, "on", "--yes")
    rc, out = _capture(config_dir, "status")
    assert "No captured sessions yet" in out


# -- SURV-HE: a failing hook flagged in capture status -----------------------


def _hook_call_lines(hook_name: str, *, success: int, errors: int, start) -> list[dict]:
    """``success`` hook_success + ``errors`` hook_non_blocking_error
    attachment lines for ``hook_name``, a second apart from ``start``."""
    from helpers import attachment_line

    lines = []
    for i in range(success):
        line = attachment_line("hook_success", hookName=hook_name)
        line["timestamp"] = (start + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        lines.append(line)
    for i in range(errors):
        line = attachment_line("hook_non_blocking_error", hookName=hook_name)
        line["timestamp"] = (start + timedelta(seconds=success + i)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        lines.append(line)
    return lines


def _session_with_hook_calls(config_dir, *, hook_name: str, success: int, errors: int, days_ago: float = 1):
    """A normal one-message session (so the corpus counts it as a real
    session) whose transcript also carries ``success`` + ``errors`` hook
    attachment lines for ``hook_name``."""
    from helpers import turn_line, user_str_line, write_jsonl

    start = datetime.now(timezone.utc) - timedelta(days=days_ago)

    def ts(second):
        return (start + timedelta(seconds=second)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    lines = [
        user_str_line("fix the failing test", origin={"kind": "human"}, timestamp=ts(0)),
        turn_line(content=[{"type": "text", "text": "Fixed."}], timestamp=ts(1), input_tokens=2000, output_tokens=400),
    ]
    lines += _hook_call_lines(hook_name, success=success, errors=errors, start=start + timedelta(seconds=2))
    project = config_dir.parent / "projects" / "C--work-app"
    project.mkdir(parents=True, exist_ok=True)
    write_jsonl(project / "s1.jsonl", lines)


def test_status_flags_a_hook_failing_on_most_of_its_calls(tmp_path):
    config_dir = _claude(tmp_path, {})
    _session_with_hook_calls(config_dir, hook_name="PreToolUse", success=8, errors=12)
    rc, out = _capture(config_dir, "status")
    assert rc == 0
    assert "PreToolUse" in out
    assert "60%" in out
    assert "12 times" in out
    assert "20 runs Claude Code recorded" in out
    assert "settings.json" in out


def test_status_says_nothing_when_too_few_calls_to_judge(tmp_path):
    config_dir = _claude(tmp_path, {})
    # 3 calls, all errors: a 100% failure rate, but far too few to act on.
    _session_with_hook_calls(config_dir, hook_name="PreToolUse", success=0, errors=3)
    rc, out = _capture(config_dir, "status")
    assert rc == 0
    assert "PreToolUse" not in out


def test_status_says_nothing_when_the_error_rate_is_under_the_threshold(tmp_path):
    config_dir = _claude(tmp_path, {})
    _session_with_hook_calls(config_dir, hook_name="PreToolUse", success=15, errors=5)
    rc, out = _capture(config_dir, "status")
    assert rc == 0
    assert "PreToolUse" not in out


def test_status_never_prints_a_raw_matcher_or_tool_name(tmp_path):
    # events._hook_name_bucket keeps only the closed hook-event name; an
    # MCP tool or bash-matcher suffix must never reach this output.
    config_dir = _claude(tmp_path, {})
    from helpers import attachment_line, turn_line, user_str_line, write_jsonl

    start = datetime.now(timezone.utc) - timedelta(days=1)

    def ts(second):
        return (start + timedelta(seconds=second)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    lines = [
        user_str_line("fix the failing test", origin={"kind": "human"}, timestamp=ts(0)),
        turn_line(content=[{"type": "text", "text": "Fixed."}], timestamp=ts(1), input_tokens=2000, output_tokens=400),
    ]
    for i in range(20):
        line = attachment_line("hook_non_blocking_error", hookName="PreToolUse:mcp__secret-server__do_thing")
        line["timestamp"] = ts(2 + i)
        lines.append(line)
    project = config_dir.parent / "projects" / "C--work-app"
    project.mkdir(parents=True, exist_ok=True)
    write_jsonl(project / "s1.jsonl", lines)

    rc, out = _capture(config_dir, "status")
    assert rc == 0
    assert "secret-server" not in out
    assert "do_thing" not in out
    assert "mcp__" not in out
    assert "PreToolUse" in out


# -- CAP-9/F10: Deep's measured wait in capture status -----------------------


def test_status_shows_deeps_measured_wait_when_its_tool_note_metrics_are_on(tmp_path):
    config_dir = _claude(tmp_path, {})
    from claudeglass.config import set_capture
    from helpers import attachment_line, turn_line, user_str_line, write_jsonl

    set_capture(config_dir, level="deep", now=datetime.now(timezone.utc) - timedelta(days=2))
    start = datetime.now(timezone.utc) - timedelta(days=1)

    def ts(second):
        return (start + timedelta(seconds=second)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    command = (
        '"python.exe" -I -S "C:\\Users\\me\\scratch\\tl\\hooks\\capture-hook.py" '
        '--config-dir "C:\\Users\\me\\scratch\\tl"'
    )
    lines = [
        user_str_line("fix the failing test", origin={"kind": "human"}, timestamp=ts(0)),
        turn_line(content=[{"type": "text", "text": "Fixed."}], timestamp=ts(1), input_tokens=2000, output_tokens=400),
    ]
    for i, ms in enumerate([100] * 8 + [900] * 2):
        line = attachment_line("hook_success", hookName="PostToolUse:Bash", durationMs=ms, command=command)
        line["timestamp"] = ts(2 + i)
        lines.append(line)
    project = config_dir.parent / "projects" / "C--work-app"
    project.mkdir(parents=True, exist_ok=True)
    write_jsonl(project / "s1.jsonl", lines)

    rc, out = _capture(config_dir, "status")
    assert rc == 0
    assert "Deep's large-output/web hook waited \u22480.1s (median, p90 \u22480.9s) over 10 calls this week." in out


def test_status_hides_deep_wait_when_its_tool_note_metrics_are_off(tmp_path):
    config_dir = _claude(tmp_path, {})
    from claudeglass.config import set_capture

    # Standard doesn't turn on big_output/web, so there's nothing to show
    # even though there's a session in the window.
    set_capture(config_dir, level="standard", now=datetime.now(timezone.utc) - timedelta(days=2))
    _session(config_dir)
    rc, out = _capture(config_dir, "status")
    assert rc == 0
    assert "hook waited" not in out


def _init_args(config_dir, *argv):
    return cli._make_parser().parse_args(["init", "--config-dir", str(config_dir), *argv])


#: Every init question but the capture and feedback ones, answered.
_OTHER_ANSWERS = {
    "billing": "api",
    "exclude_projects": [],
    "launch_overlays": False,
    "shared_project_config": False,
    "tz": "",
    "apply_scope": "user",
    "capture_window": 14,
}


def _init(config_dir, *argv, stdin="", answers=None):
    """Run ``init --advanced --no-service`` (the full capture and feedback
    questions) through the real parser, every other question answered by
    an answers file; returns the output. Without ``--connect`` or
    ``--no-install``, the first line of ``stdin`` answers 'Connect?', and
    the end of ``stdin`` says yes to 'Go ahead?'."""
    path = config_dir.parent.parent / "answers.json"
    path.write_text(json.dumps({**_OTHER_ANSWERS, **(answers or {})}), encoding="utf-8")
    args = _init_args(config_dir, "--advanced", "--no-service", "--answers", str(path), *argv)
    out = io.StringIO()
    setup_flow.run(*cli._setup_flow_inputs(args), stdin=io.StringIO(stdin), stdout=out, now=NOW)
    return out.getvalue()


def _capture_entries(config_dir) -> list:
    commands = set(_commands(config_dir).values())
    return [entry for entry in _entries(_settings(config_dir)) if entry[2].get("command") in commands]


def test_init_warns_shows_estimates_and_connects_after_a_yes(tmp_path):
    config_dir = _claude(tmp_path, {})
    _api_billing(config_dir)
    _session(config_dir)
    # Connect: yes; Standard; keep the time limit; no feedback; go ahead.
    out = _init(config_dir, stdin="y\nstandard\n\n\ny\n")
    assert "This uses your tokens" in out and "[tl: task=bugfix brief=clear], which you will see" in out
    assert "What each level would have cost over your last 14 days" in out
    assert "Metrics capture level: off, free, essentials, standard, deep [off]:" in out
    assert "Metrics capture will switch itself off on 2026-10-08 06:00 UTC (14 days from now)" in out
    assert "claudeglass capture on --for 30d" in out
    assert "Turn off that time limit (capture then runs until you switch it off) (y/n) [n]:" in out
    assert "Saved to config.toml: metrics capture Standard (since 2026-09-24, until 2026-10-08 06:00)." in out
    assert load_config(config_dir).capture.level == "standard"
    assert load_config(config_dir).capture.until == "2026-10-08T06:00:00+00:00"
    assert len(_capture_entries(config_dir)) == len(hook_health.capture_specs(cat.level_metrics("standard")))


def test_init_time_box_question_yes_turns_the_limit_off(tmp_path):
    config_dir = _claude(tmp_path, {})
    out = _init(config_dir, stdin="n\nessentials\ny\n")
    assert "Turn off that time limit" in out
    assert load_config(config_dir).capture.level == "essentials"
    assert load_config(config_dir).capture.until == ""


def test_init_capture_no_limit_flag_skips_the_question(tmp_path):
    config_dir = _claude(tmp_path, {})
    out = _init(config_dir, "--capture-no-limit", stdin="n\nessentials\n")
    assert "Turn off that time limit" not in out
    assert load_config(config_dir).capture.level == "essentials"
    assert load_config(config_dir).capture.until == ""


def test_init_capture_no_limit_answers_file(tmp_path):
    config_dir = _claude(tmp_path, {})
    out = _init(
        config_dir, "--non-interactive", "--no-install", answers={"capture_level": "essentials", "capture_no_limit": True}
    )
    assert "Turn off that time limit" not in out
    assert load_config(config_dir).capture.level == "essentials"
    assert load_config(config_dir).capture.until == ""


def test_non_interactive_init_with_explicit_level_gets_the_default_time_box(tmp_path):
    # CAP-8: reverses the tool's earlier assumption here (a --non-
    # interactive run naming a level explicitly kept no time-box unless
    # asked) -- a scripted/unattended init is exactly the case the
    # default most needs to reach, so it now follows the same "no answer
    # -> the derived default" rule as every other onboarding question.
    # --capture-no-limit (or the answers file's capture_no_limit key)
    # still opts out explicitly.
    config_dir = _claude(tmp_path, {})
    out = _init(config_dir, "--non-interactive", "--no-install", "--capture-level", "essentials")
    assert "(derived) capture_no_limit: not given in --answers; used default 'n'" in out
    assert load_config(config_dir).capture.level == "essentials"
    assert load_config(config_dir).capture.until == "2026-10-08T06:00:00+00:00"


def test_non_interactive_init_capture_for_sets_a_specific_time_box(tmp_path):
    # CAP-8: --capture-for answers the time-box question without asking,
    # parallel to 'capture on --for'.
    config_dir = _claude(tmp_path, {})
    out = _init(config_dir, "--non-interactive", "--no-install", "--capture-level", "essentials", "--capture-for", "30d")
    assert "capture_no_limit" not in out
    assert load_config(config_dir).capture.until == (NOW + timedelta(days=30)).isoformat(timespec="seconds")


def test_capture_for_and_capture_no_limit_together_is_rejected(tmp_path):
    config_dir = _claude(tmp_path, {})
    out = _init(
        config_dir, "--non-interactive", "--no-install", "--capture-level", "essentials",
        "--capture-for", "30d", "--capture-no-limit",
    )
    assert "--capture-for and --capture-no-limit can't both be given." in out
    assert not (config_dir / "config.toml").exists()


def test_capture_for_rejects_a_bad_duration(tmp_path):
    config_dir = _claude(tmp_path, {})
    out = _init(config_dir, "--non-interactive", "--no-install", "--capture-level", "essentials", "--capture-for", "soon")
    assert "--capture-for 'soon': use a number and h, d or w" in out
    assert not (config_dir / "config.toml").exists()


@pytest.mark.parametrize("typed, level", [("", "off"), ("n", "off"), ("yes", "essentials"), ("Deep", "deep")])
def test_init_reads_yes_no_and_level_names(tmp_path, typed, level):
    config_dir = _claude(tmp_path, {})
    _init(config_dir, stdin=f"n\n{typed}\nn\n")
    assert load_config(config_dir).capture.level == level


def test_init_does_not_turn_it_on_for_a_word_it_does_not_know(tmp_path):
    config_dir = _claude(tmp_path, {})
    out = _init(config_dir, stdin="n\nmax\n")
    assert "'max' isn't a level" in out and load_config(config_dir).capture.level == "off"


def test_non_interactive_init_leaves_it_off_and_says_so(tmp_path):
    config_dir = _claude(tmp_path, {})
    out = _init(config_dir, "--non-interactive")
    assert "(derived) capture_level: not given in --answers; metrics capture left off" in out
    assert "This uses your tokens" not in out
    assert load_config(config_dir).capture.level == "off"


def test_init_capture_level_flag_without_connecting_prints_the_command(tmp_path):
    config_dir = _claude(tmp_path, {})
    out = _init(config_dir, "--non-interactive", "--no-install", "--capture-level", "essentials")
    assert "This uses your tokens" in out
    assert "add the hooks it needs later with 'claudeglass capture connect'" in out
    assert load_config(config_dir).capture.level == "essentials"
    assert _settings(config_dir) == {}


def test_init_answers_file_level_with_connect_writes_without_asking(tmp_path):
    config_dir = _claude(tmp_path, {})
    out = _init(config_dir, "--non-interactive", "--connect", answers={"capture_level": "free"})
    assert load_config(config_dir).capture.level == "free"
    assert len(_capture_entries(config_dir)) == len(hook_health.capture_specs(cat.level_metrics("free")))
    assert "Go ahead?" not in out


def test_init_leaves_capture_that_is_already_on_alone(tmp_path):
    config_dir = _claude(tmp_path, {})
    _capture(config_dir, "on", "--level", "standard", "--yes")
    # Connect: no; then "off" would answer the capture question if it were
    # asked.
    out = _init(config_dir, stdin="n\noff\n")
    # CAP-8: the earlier "on" call got the default time-box too.
    assert "Metrics capture is Standard (since 2026-09-24, until 2026-10-08 06:00)." in out
    assert load_config(config_dir).capture.level == "standard"
    out = _init(config_dir, "--capture-level", "off", stdin="n\n")
    assert "Metrics capture switched off." in out and load_config(config_dir).capture.level == "off"


def test_init_asks_about_capture_before_writing_anything(tmp_path, monkeypatch, capsys):
    config_dir = _claude(tmp_path, {})
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["init", "--non-interactive", "--no-install", "--no-service", "--config-dir", str(config_dir),
                   "--capture-level", "essentials"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.rindex("Metrics capture (optional)") < out.index("Ready to set up:") < out.index("Wrote config.toml")
    assert load_config(config_dir).capture.level == "essentials"


# -- feedback: the /tl-feedback skill --------------------------------------------


def _skill(config_dir):
    return config_dir.parent / "skills" / "tl-feedback" / "SKILL.md"


def test_feedback_on_shows_the_skill_and_writes_it_after_a_yes(tmp_path):
    config_dir = _claude(tmp_path, {})
    rc, out = _capture(config_dir, "feedback", "on", stdin="y\n")
    assert rc == 0 and "Nothing is added to Claude's context until you run the skill." in out
    assert f"This adds the /tl-feedback skill, {_skill(config_dir)}:" in out
    assert "    name: tl-feedback" in out and "Add it? (y/n) [n]:" in out
    assert _skill(config_dir).read_text(encoding="utf-8") == cat.feedback_skill_text()
    capture = load_config(config_dir=config_dir).capture
    assert capture.feedback == ["feedback_skill", "feedback_note"] and capture.level == "off"
    # settings.json is never touched: the skill needs no hook.
    assert _settings(config_dir) == {}
    rc, out = _capture(config_dir, "feedback", "on")
    assert "Feedback is already on." in out and "The /tl-feedback skill is in place" in out


def test_feedback_on_dry_run_and_a_no_write_no_skill(tmp_path):
    config_dir = _claude(tmp_path, {})
    rc, out = _capture(config_dir, "feedback", "on", "--dry-run")
    assert "Dry run: config.toml left unchanged." in out and "Dry run: the skill is left as it is." in out
    assert not _skill(config_dir).exists() and not (config_dir / "config.toml").exists()
    rc, out = _capture(config_dir, "feedback", "on", stdin="n\n")
    assert "Left as it is. Run 'claudeglass capture feedback on'" in out
    assert not _skill(config_dir).exists()
    rc, out = _capture(config_dir, "status")
    assert "The /tl-feedback skill isn't installed: claudeglass capture feedback on" in out


def test_an_old_skill_is_shown_as_a_diff_and_someone_elses_is_left_alone(tmp_path):
    config_dir = _claude(tmp_path, {})
    _capture(config_dir, "feedback", "on", "--yes")
    skill = _skill(config_dir)
    skill.write_text(cat.feedback_skill_text().replace("four quick", "three quick"), encoding="utf-8")
    assert "out of date" in _capture(config_dir, "status")[1]
    rc, out = _capture(config_dir, "feedback", "on", "--yes")
    assert "This updates the /tl-feedback skill" in out and "-description:" in out
    assert skill.read_text(encoding="utf-8") == cat.feedback_skill_text()
    skill.write_text("---\nname: tl-feedback\n---\nmine\n", encoding="utf-8")
    rc, out = _capture(config_dir, "feedback", "on", "--yes")
    assert "holds a skill this tool didn't write, so it is left alone" in out
    rc, out = _capture(config_dir, "feedback", "off", "--yes")
    assert skill.read_text(encoding="utf-8") == "---\nname: tl-feedback\n---\nmine\n"


def test_feedback_off_removes_the_skill_and_its_reminders_but_not_the_rating(tmp_path):
    config_dir = _claude(tmp_path, {})
    _capture(config_dir, "enable", "dashboard_rating", "feedback_reminder", "--yes")
    _capture(config_dir, "feedback", "on", "--yes")
    rc, out = _capture(config_dir, "feedback", "off", stdin="y\n")
    assert "This removes the /tl-feedback skill" in out and "Removed." in out
    assert not _skill(config_dir).exists() and not _skill(config_dir).parent.exists()
    assert load_config(config_dir=config_dir).capture.feedback == ["dashboard_rating"]


def test_enabling_or_disabling_the_skill_metric_installs_or_removes_it(tmp_path):
    config_dir = _claude(tmp_path, {})
    _capture(config_dir, "enable", "feedback_skill", "--yes")
    assert _skill(config_dir).is_file()
    _capture(config_dir, "disable", "feedback_skill", "--yes")
    assert not _skill(config_dir).exists()


def test_remove_keeps_the_skill_and_says_how_to_take_it_out(tmp_path):
    config_dir = _claude(tmp_path, {})
    _capture(config_dir, "on", "--yes")
    _capture(config_dir, "feedback", "on", "--yes")
    rc, out = _capture(config_dir, "remove", "--yes")
    assert "The /tl-feedback skill stays: it works with capture off." in out
    assert _skill(config_dir).is_file()


@pytest.mark.parametrize("argv", [["feedback"], ["feedback", "maybe"], ["feedback", "on", "off"]])
def test_feedback_needs_on_or_off(tmp_path, argv):
    config_dir = _claude(tmp_path, {})
    rc, out = _capture(config_dir, *argv)
    assert rc == 2 and "'capture feedback' needs on or off" in out


def test_the_skill_is_listed_and_taken_out_by_uninstall(tmp_path, monkeypatch, capsys):
    config_dir = _claude(tmp_path, {})
    assert "feedback_skill" not in {i.key for i in footprint.inventory(config_dir, service_registered=False)}
    _capture(config_dir, "feedback", "on", "--yes")
    item = {i.key: i for i in footprint.inventory(config_dir, service_registered=False)}["feedback_skill"]
    assert item.status == "installed" and item.undo == "claudeglass capture feedback off"
    assert "None until you run it" in item.token_cost
    plan = footprint.plan_uninstall(config_dir)
    assert plan.feedback_skill == _skill(config_dir)
    rc = cli.main(["uninstall", "--yes", "--config-dir", str(config_dir)])
    out = capsys.readouterr().out
    assert "The /tl-feedback skill:" in out and "Removed." in out
    assert not _skill(config_dir).exists()


# -- brief templates: the /tl-brief skill ----------------------------------------


def _brief_skill(config_dir):
    return config_dir.parent / "skills" / "tl-brief" / "SKILL.md"


def test_brief_on_dry_run_writes_nothing_and_a_yes_writes_the_skill(tmp_path):
    config_dir = _claude(tmp_path, {})
    rc, out = _capture(config_dir, "brief", "on", "--dry-run")
    assert rc == 0 and "Brief templates: the /tl-brief skill on." in out
    assert "Dry run: config.toml left unchanged." in out and "Dry run: the skill is left as it is." in out
    assert "    name: tl-brief" in out
    assert not _brief_skill(config_dir).exists() and not (config_dir / "config.toml").exists()
    rc, out = _capture(config_dir, "brief", "on", stdin="y\n")
    assert f"This adds the /tl-brief skill, {_brief_skill(config_dir)}:" in out
    assert "Run /tl-brief in Claude Code followed by your request" in out
    assert _brief_skill(config_dir).read_text(encoding="utf-8") == cat.brief_skill_text()
    capture = load_config(config_dir=config_dir).capture
    assert capture.coaching == ["brief_templates"] and capture.level == "off" and capture.feedback == []
    assert _settings(config_dir) == {}
    rc, out = _capture(config_dir, "brief", "on")
    assert "Brief templates are already on." in out and "The /tl-brief skill is in place" in out


def test_brief_off_removes_only_the_brief_skill(tmp_path):
    config_dir = _claude(tmp_path, {})
    _capture(config_dir, "feedback", "on", "--yes")
    _capture(config_dir, "brief", "on", "--yes")
    rc, out = _capture(config_dir, "brief", "off", stdin="n\n")
    assert "Left as it is. Run 'claudeglass capture brief off'" in out and _brief_skill(config_dir).is_file()
    rc, out = _capture(config_dir, "brief", "off", "--yes")
    assert "This removes the /tl-brief skill" in out and not _brief_skill(config_dir).parent.exists()
    assert _skill(config_dir).is_file()
    capture = load_config(config_dir=config_dir).capture
    assert capture.coaching == [] and capture.feedback == ["feedback_skill", "feedback_note"]


def test_enabling_brief_templates_installs_the_skill_and_status_says_when_it_is_missing(tmp_path):
    config_dir = _claude(tmp_path, {})
    _capture(config_dir, "enable", "brief_templates", "--yes")
    assert _brief_skill(config_dir).is_file()
    _brief_skill(config_dir).unlink()
    assert "The /tl-brief skill isn't installed: claudeglass capture brief on" in _capture(config_dir, "status")[1]
    _capture(config_dir, "connect", "--yes")
    assert _brief_skill(config_dir).is_file()
    _capture(config_dir, "disable", "brief_templates", "--yes")
    assert not _brief_skill(config_dir).exists()


def test_someone_elses_tl_brief_is_left_alone(tmp_path):
    config_dir = _claude(tmp_path, {})
    skill = _brief_skill(config_dir)
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: tl-brief\n---\nmine\n", encoding="utf-8")
    rc, out = _capture(config_dir, "brief", "on", "--yes")
    assert "holds a skill this tool didn't write, so it is left alone" in out
    _capture(config_dir, "brief", "off", "--yes")
    assert skill.read_text(encoding="utf-8") == "---\nname: tl-brief\n---\nmine\n"
    assert footprint.plan_uninstall(config_dir).brief_skill is None


@pytest.mark.parametrize("argv", [["brief"], ["brief", "maybe"]])
def test_brief_needs_on_or_off(tmp_path, argv):
    config_dir = _claude(tmp_path, {})
    rc, out = _capture(config_dir, *argv)
    assert rc == 2 and "'capture brief' needs on or off" in out


def test_the_brief_skill_is_listed_and_taken_out_by_uninstall(tmp_path, monkeypatch, capsys):
    config_dir = _claude(tmp_path, {})
    assert "brief_skill" not in {i.key for i in footprint.inventory(config_dir, service_registered=False)}
    _capture(config_dir, "brief", "on", "--yes")
    item = {i.key: i for i in footprint.inventory(config_dir, service_registered=False)}["brief_skill"]
    assert item.status == "installed" and item.undo == "claudeglass capture brief off"
    assert footprint.plan_uninstall(config_dir).brief_skill == _brief_skill(config_dir)
    cli.main(["uninstall", "--yes", "--config-dir", str(config_dir)])
    out = capsys.readouterr().out
    assert "The /tl-brief skill:" in out and not _brief_skill(config_dir).exists()

def test_init_at_deep_skips_the_feedback_question_and_adds_the_skill(tmp_path):
    config_dir = _claude(tmp_path, {})
    out = _init(config_dir, "--non-interactive", "--no-install", "--capture-level", "deep")
    assert load_config(config_dir).capture.feedback == list(cat.DEEP_FEEDBACK_IDS)
    assert "Feedback after a piece of work" not in out
    assert "The /tl-feedback skill: add it with 'claudeglass capture feedback on'." in out
    assert not _skill(config_dir).exists()
    out = _init(config_dir, "--non-interactive", "--connect")
    assert "The /tl-feedback survey is on, as part of Deep." in out
    assert _skill(config_dir).read_text(encoding="utf-8") == cat.feedback_skill_text()
    assert "The /tl-feedback skill is on." in _init(config_dir, "--non-interactive")


def test_init_offers_the_skill_and_writes_it_after_a_yes(tmp_path):
    config_dir = _claude(tmp_path, {})
    # Connect: no; capture left off; feedback: yes; go ahead.
    out = _init(config_dir, stdin="n\n\ny\n")
    assert "Feedback after a piece of work (optional)" in out and "It works at any capture level" in out
    assert "Add the /tl-feedback skill? (y/n) [n]:" in out and "Add it?" not in out
    assert "Sharper tips (metrics capture): add the /tl-feedback skill." in out
    assert _skill(config_dir).is_file()
    assert load_config(config_dir).capture.feedback == ["feedback_skill", "feedback_note"]
    out = _init(config_dir, stdin="n\n\n")
    assert "The /tl-feedback skill is on." in out and _skill(config_dir).is_file()


def test_init_feedback_no_and_non_interactive_leave_it_off(tmp_path):
    config_dir = _claude(tmp_path, {})
    assert "Sharper tips (metrics capture): off." in _init(config_dir, stdin="n\n\n\n")
    out = _init(config_dir, "--non-interactive")
    assert "(derived) feedback: not given in --answers; left off" in out
    assert "Feedback after a piece of work" not in out
    assert not _skill(config_dir).exists() and load_config(config_dir).capture.feedback == []


def test_init_feedback_flag_without_connecting_prints_the_command(tmp_path):
    config_dir = _claude(tmp_path, {})
    out = _init(config_dir, "--non-interactive", "--no-install", "--feedback", "on")
    assert "The /tl-feedback skill: add it with 'claudeglass capture feedback on'." in out
    assert not _skill(config_dir).exists()
    assert load_config(config_dir).capture.feedback == ["feedback_skill", "feedback_note"]


def test_init_feedback_answers_file_with_connect_writes_without_asking(tmp_path):
    config_dir = _claude(tmp_path, {})
    out = _init(config_dir, "--non-interactive", "--connect", answers={"feedback": True})
    assert _skill(config_dir).is_file() and "Add the /tl-feedback skill?" not in out
    out = _init(config_dir, "--non-interactive", "--connect", "--feedback", "off")
    assert "Saved to config.toml: feedback off." in out and not _skill(config_dir).exists()


def test_init_asks_about_feedback_after_capture(tmp_path, monkeypatch, capsys):
    config_dir = _claude(tmp_path, {})
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["init", "--non-interactive", "--no-install", "--no-service", "--config-dir", str(config_dir),
                   "--capture-level", "off", "--feedback", "on"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.index("Metrics capture (optional)") < out.index("Ready to set up:")
    assert out.index("The /tl-feedback skill: add it with 'claudeglass capture feedback on'.") > out.index(
        "Ready to set up:"
    )


def test_status_says_when_the_status_line_is_someone_elses(tmp_path):
    config_dir = _claude(tmp_path, {"statusLine": {"type": "command", "command": "my-own-line"}})
    _capture(config_dir, "feedback", "on", "--yes")
    rc, out = _capture(config_dir, "status")
    assert "Your status line isn't ClaudeGlass's, so this second line won't show there" in out
    (config_dir.parent / "settings.json").write_text(
        json.dumps({"statusLine": {"type": "command", "command": "claudeglass statusline"}}), encoding="utf-8"
    )
    assert "Your status line isn't" not in _capture(config_dir, "status")[1]


# -- capture prune (SEC-P8/G7: signal files and capture-log.jsonl were --
# -- never pruned; now this is the same housekeeping serve's watcher    --
# -- does on every tick, offered as a standalone command)               --


def _write_signal_month_file(config_dir: Path, year: int, month: int) -> Path:
    signals.signals_dir(config_dir).mkdir(parents=True, exist_ok=True)
    path = signals.signals_dir(config_dir) / f"{year:04d}-{month:02d}.jsonl"
    path.write_text("", encoding="utf-8")
    return path


def _write_capture_log_lines(config_dir: Path, *timestamps: datetime) -> None:
    lines = [
        json.dumps({"ts": ts.isoformat(timespec="seconds"), "level": "essentials", "changed": {}}, sort_keys=True)
        for ts in timestamps
    ]
    (config_dir / CAPTURE_LOG_NAME).write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_prune_dry_run_changes_nothing(tmp_path):
    config_dir = _claude(tmp_path, {})
    old_signal = _write_signal_month_file(config_dir, 2020, 1)
    _write_capture_log_lines(config_dir, NOW - timedelta(days=200))

    rc, out = _capture(config_dir, "prune", "--dry-run")

    assert rc == 0
    assert "Dry run: nothing pruned." in out
    assert old_signal.exists()
    assert len(load_capture_log(config_dir)) == 1


def test_prune_removes_old_signal_files_and_capture_log_records(tmp_path):
    config_dir = _claude(tmp_path, {})
    old_signal = _write_signal_month_file(config_dir, 2020, 1)
    recent_signal = _write_signal_month_file(config_dir, NOW.year, NOW.month)
    _write_capture_log_lines(config_dir, NOW - timedelta(days=200), NOW - timedelta(days=5))

    rc, out = _capture(config_dir, "prune")

    assert rc == 0
    assert f"older than {SIGNAL_RETENTION_DEFAULT_DAYS} days" in out
    assert not old_signal.exists()
    assert recent_signal.exists()
    log = load_capture_log(config_dir)
    assert len(log) == 1
    assert log[0]["ts"] == (NOW - timedelta(days=5)).isoformat(timespec="seconds")


def test_prune_uses_retention_days_from_config_when_set(tmp_path):
    config_dir = _claude(tmp_path, {})
    (config_dir / "config.toml").write_text("retention_days = 30\n", encoding="utf-8")
    old_signal = _write_signal_month_file(config_dir, 2020, 1)
    _write_capture_log_lines(config_dir, NOW - timedelta(days=60), NOW - timedelta(days=1))

    rc, out = _capture(config_dir, "prune")

    assert rc == 0
    assert "older than 30 days" in out
    assert not old_signal.exists()
    log = load_capture_log(config_dir)
    assert len(log) == 1
    assert log[0]["ts"] == (NOW - timedelta(days=1)).isoformat(timespec="seconds")


def test_prune_removes_old_usage_log_rows_too(tmp_path):
    # SIG-5: usage-log.csv is written unconditionally, capture on or off,
    # so `capture prune` must sweep it alongside signal files and
    # capture-log.jsonl.
    from claudeglass.tools import log_usage

    config_dir = _claude(tmp_path, {})
    csv_path = log_usage.default_usage_log_path(config_dir)
    log_usage.append_rows(
        csv_path, [{"session_id": "old", "window": "five_hour", "resets_at": "r"}], now=NOW - timedelta(days=200)
    )
    log_usage.append_rows(
        csv_path, [{"session_id": "recent", "window": "five_hour", "resets_at": "r"}], now=NOW - timedelta(days=5)
    )

    rc, out = _capture(config_dir, "prune")

    assert rc == 0
    assert "usage-log row(s)" in out
    rows = log_usage.load_usage_log(csv_path)
    assert len(rows) == 1
    assert rows[0]["session_id"] == "recent"


def test_on_warns_when_a_settings_policy_stops_hooks_running(tmp_path):
    # disableAllHooks (or a managed allowManagedHooksOnly) means Claude
    # Code won't run the entries at all; say so before showing the diff.
    config_dir = _claude(tmp_path, {"disableAllHooks": True})
    rc, out = _capture(config_dir, "on", "--dry-run", stdin="y\ny\n")
    assert rc == 0
    assert hook_health.POLICY_TEXT[hook_health.POLICY_ALL_OFF] in out
    assert "won't run them while that holds" in out
    assert out.index("won't run them") < out.index("This changes")

"""Tests for ``src/claudeglass/setup_status.py`` and the ``status``
command: each part of the setup reads as done, waiting, off or needing
attention, and ``status`` exits 1 only for an essential problem."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from claudeglass import capture_catalogue, cli, hook_health, installer, setup_status
from claudeglass.config import load_config, set_capture

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _claude_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))


def _dirs(tmp_path) -> tuple[Path, Path]:
    claude = tmp_path / "claude"
    config_dir = claude / "claudeglass"
    config_dir.mkdir(parents=True)
    return config_dir, claude


def _connect(config_dir: Path, claude: Path) -> None:
    script = config_dir / "hooks" / "snapshot-config.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("# hook\n", encoding="utf-8")
    settings = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": f'"{sys.executable}" "{script}"'}]}]}}
    (claude / "settings.json").write_text(json.dumps(settings), encoding="utf-8")


def _snapshot(config_dir: Path, ts: str) -> None:
    snaps = config_dir / "snapshots"
    snaps.mkdir(parents=True, exist_ok=True)
    (snaps / "s.json").write_text(json.dumps({"ts": ts, "effective": {}}), encoding="utf-8")


def _billing(config_dir: Path, value: str) -> None:
    (config_dir / "config.toml").write_text(f'billing = "{value}"\n', encoding="utf-8")


def _items(config_dir, claude, *, registered=True, running=True) -> dict[str, setup_status.SetupItem]:
    items = setup_status.check_setup(
        config_dir, claude, now=NOW, is_registered=lambda: registered, running=running
    )
    return {item.key: item for item in items}


def test_billing_never_chosen_is_an_essential_problem(tmp_path):
    config_dir, claude = _dirs(tmp_path)
    item = _items(config_dir, claude)["billing"]
    assert (item.state, item.essential, item.fix) == ("problem", True, setup_status.INIT_COMMAND)


@pytest.mark.parametrize("value", ["subscription", "api"])
def test_billing_chosen_is_done(tmp_path, value):
    config_dir, claude = _dirs(tmp_path)
    _billing(config_dir, value)
    assert _items(config_dir, claude)["billing"].state == "ok"


def test_automatic_billing_waits_for_usage_limit_readings(tmp_path):
    config_dir, claude = _dirs(tmp_path)
    _billing(config_dir, "auto")
    item = _items(config_dir, claude)["billing"]
    assert (item.state, item.essential) == ("waiting", False)
    assert "desktop app" in item.detail and "1 or 2" in item.detail

    (config_dir / "usage-log.csv").write_text(
        "logged_at,session_id,window,used_percentage\n2026-09-20T10:00:00Z,s1,five_hour,12\n", encoding="utf-8"
    )
    assert _items(config_dir, claude)["billing"].state == "ok"


def test_hook_not_connected_waiting_then_done(tmp_path):
    config_dir, claude = _dirs(tmp_path)
    item = _items(config_dir, claude)["hook"]
    assert (item.state, item.essential) == ("problem", True)

    _connect(config_dir, claude)
    item = _items(config_dir, claude)["hook"]
    assert item.state == "waiting"
    assert item.detail == "No Claude Code session has started since you connected."

    _snapshot(config_dir, "2026-09-20T12:00:00Z")
    item = _items(config_dir, claude)["hook"]
    assert item.state == "ok"
    assert "2 days ago" in item.detail


@pytest.mark.parametrize(
    ("registered", "running", "state", "essential"),
    [
        (True, True, "ok", True),
        (True, False, "problem", True),
        (False, True, "off", True),
        (False, False, "off", True),
        (None, True, "ok", True),
        (None, False, "off", False),
    ],
)
def test_service_states(tmp_path, registered, running, state, essential):
    config_dir, claude = _dirs(tmp_path)
    item = _items(config_dir, claude, registered=registered, running=running)["service"]
    assert (item.state, item.essential) == (state, essential)
    if state != "ok":
        assert "cleanupPeriodDays" in item.detail
        assert item.fix == setup_status.SERVICE_COMMAND


def test_an_unknown_registration_is_never_a_problem(tmp_path):
    config_dir, claude = _dirs(tmp_path)
    for running in (True, False):
        assert _items(config_dir, claude, registered=None, running=running)["service"].state != "problem"


def test_running_is_asked_of_the_health_check_when_not_given(tmp_path):
    config_dir, claude = _dirs(tmp_path)
    asked = []
    items = setup_status.check_setup(
        config_dir, claude, now=NOW, is_registered=lambda: True, health_check=lambda url: asked.append(url) or False
    )
    assert asked == [installer.DEFAULT_URL]
    assert {i.key: i for i in items}["service"].state == "problem"


def test_capture_off_and_missing_hooks(tmp_path):
    config_dir, claude = _dirs(tmp_path)
    assert _items(config_dir, claude)["capture"].state == "off"

    set_capture(config_dir, level="essentials", until="", now=NOW)
    item = _items(config_dir, claude)["capture"]
    assert (item.state, item.essential) == ("problem", True)
    assert item.detail.startswith("Essentials")
    assert item.fix == "claudeglass capture connect"


def test_capture_past_its_time_box_is_off(tmp_path):
    config_dir, claude = _dirs(tmp_path)
    set_capture(config_dir, level="essentials", until="2026-09-01T00:00:00+00:00", now=NOW)
    item = _items(config_dir, claude)["capture"]
    assert item.state == "off"
    assert "2026-09-01" in item.detail


def test_skill_missing_is_a_problem_only_when_feedback_is_on(tmp_path):
    config_dir, claude = _dirs(tmp_path)
    assert _items(config_dir, claude)["skill"].state == "off"
    set_capture(config_dir, level="essentials", until="", feedback=["feedback_skill"], now=NOW)
    item = _items(config_dir, claude)["skill"]
    assert (item.state, item.essential) == ("problem", False)


def test_no_absolute_home_path_in_the_output(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    config_dir, claude = _dirs(tmp_path)
    (claude / "settings.json").write_text("{not json", encoding="utf-8")
    (config_dir / "config.toml").write_text("billing = 3\n", encoding="utf-8")
    for item in setup_status.check_setup(config_dir, claude, now=NOW, is_registered=lambda: False, running=False):
        assert str(tmp_path) not in item.detail, item


def test_verdict_counts_problems():
    ok = setup_status.SetupItem("a", "A", "ok", "")
    waiting = setup_status.SetupItem("b", "B", "waiting", "", essential=True)
    problem = setup_status.SetupItem("c", "C", "problem", "")
    essential = setup_status.SetupItem("d", "D", "problem", "", essential=True)
    assert setup_status.verdict([ok, waiting]) == "Everything's set up."
    assert setup_status.verdict([ok, problem]) == "1 thing needs attention."
    assert setup_status.verdict([problem, essential]) == "2 things need attention."
    assert not setup_status.essential_problem([ok, waiting, problem])
    assert setup_status.essential_problem([essential])


def test_lines_show_words_and_fixes_not_ticks():
    items = [
        setup_status.SetupItem("a", "Short", "ok", "Fine.", "never shown"),
        setup_status.SetupItem("b", "A longer label", "problem", "Broken.", "claudeglass init"),
    ]
    assert setup_status.lines(items) == [
        "  Short           Done: Fine.",
        "  A longer label  Needs attention: Broken.",
        "                  Fix: claudeglass init",
    ]


def _run_status(config_dir, claude, capsys) -> tuple[int, str]:
    rc = cli.main(["status", "--config-dir", str(config_dir), "--claude-root", str(claude)])
    return rc, capsys.readouterr().out


def test_status_exits_1_for_an_essential_problem(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(installer, "is_registered", lambda *a, **k: True)
    monkeypatch.setattr(installer, "http_health_ok", lambda url: True)
    config_dir, claude = _dirs(tmp_path)
    rc, out = _run_status(config_dir, claude, capsys)
    assert rc == 1
    assert out.startswith("ClaudeGlass setup")
    assert "2 things need attention." in out


def test_status_exits_0_while_only_waiting_or_off(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(installer, "is_registered", lambda *a, **k: False)
    monkeypatch.setattr(installer, "http_health_ok", lambda url: False)
    config_dir, claude = _dirs(tmp_path)
    _billing(config_dir, "auto")
    _connect(config_dir, claude)
    rc, out = _run_status(config_dir, claude, capsys)
    assert rc == 0
    assert "No Claude Code session has started since you connected." in out
    assert "Everything's set up." in out


# -- M6: coaching notes need hook entries after capture's time-box, and at level off -----------------------------------

MESSAGE_ENTRY = hook_health.HookSpec(capture_catalogue.HOOK_SCRIPT, "UserPromptSubmit")
#: What still runs once capture's time-box is over: the coaching entries only.
COACHING_SPECS = hook_health.capture_specs(("coaching_notes",))


def _hook_entries(config_dir: Path, claude: Path, wanted, *, leaving_out=None) -> None:
    """Install the capture hook files and write the settings entries ``wanted`` needs, less one."""
    hook_health.install_hook_files(config_dir, hook_health.CAPTURE_FILES[capture_catalogue.HOOK_SCRIPT])
    specs = tuple(spec for spec in wanted if spec != leaving_out)
    commands = cli._capture_hook_commands(config_dir)
    hook_health.connect(hook_health.plan_capture(specs, commands, claude_root=claude), now=NOW)


def _coaching_after_the_time_box(config_dir: Path) -> None:
    set_capture(
        config_dir, level="essentials", until="2026-09-01T00:00:00+00:00", coaching=["coaching_notes"], now=NOW
    )


def test_coaching_notes_with_no_message_entry_after_the_time_box_is_a_problem_that_names_the_entry(tmp_path):
    config_dir, claude = _dirs(tmp_path)
    _coaching_after_the_time_box(config_dir)
    assert MESSAGE_ENTRY in COACHING_SPECS
    _hook_entries(config_dir, claude, COACHING_SPECS, leaving_out=MESSAGE_ENTRY)
    item = _items(config_dir, claude)["capture"]
    assert (item.state, item.essential) == ("problem", True)
    assert "settings.json does not run capture-hook.py when you send a message." in item.detail
    # Only the entry that is missing is named, and the fix is the command that adds it.
    assert item.detail.count("settings.json does not run") == 1
    assert item.fix == "claudeglass capture connect"
    assert "2026-09-01" in item.detail


def test_status_does_not_say_everything_is_set_up_while_the_message_entry_is_missing(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(installer, "is_registered", lambda *a, **k: True)
    monkeypatch.setattr(installer, "http_health_ok", lambda url: True)
    config_dir, claude = _dirs(tmp_path)
    _billing(config_dir, "auto")
    _connect(config_dir, claude)
    _snapshot(config_dir, "2026-09-22T11:00:00+00:00")
    _coaching_after_the_time_box(config_dir)
    _hook_entries(config_dir, claude, COACHING_SPECS, leaving_out=MESSAGE_ENTRY)
    rc, out = _run_status(config_dir, claude, capsys)
    assert rc == 1
    assert "capture-hook.py when you send a message" in out
    assert "claudeglass capture connect" in out
    assert "Everything's set up." not in out
    assert "1 thing needs attention." in out


@pytest.mark.parametrize("gone", COACHING_SPECS, ids=lambda spec: spec.event)
def test_each_missing_coaching_entry_is_named_after_the_time_box(tmp_path, gone):
    config_dir, claude = _dirs(tmp_path)
    _coaching_after_the_time_box(config_dir)
    _hook_entries(config_dir, claude, COACHING_SPECS, leaving_out=gone)
    item = _items(config_dir, claude)["capture"]
    assert item.state == "problem"
    assert f"settings.json does not run {gone.describe()}." in item.detail
    assert item.fix == "claudeglass capture connect"


def test_coaching_notes_with_every_entry_in_place_after_the_time_box_stay_off_and_clean(tmp_path):
    # The level's own entries (a session note, tool notes) stop with the time-box, so their absence is no problem.
    config_dir, claude = _dirs(tmp_path)
    _coaching_after_the_time_box(config_dir)
    _hook_entries(config_dir, claude, COACHING_SPECS)
    item = _items(config_dir, claude)["capture"]
    assert (item.state, item.essential) == ("off", False)
    assert "2026-09-01" in item.detail


def test_coaching_notes_with_capture_off_and_no_entries_is_a_problem(tmp_path):
    config_dir, claude = _dirs(tmp_path)
    set_capture(config_dir, level="off", coaching=["coaching_notes"], now=NOW)
    hook_health.install_hook_files(config_dir, hook_health.CAPTURE_FILES[capture_catalogue.HOOK_SCRIPT])
    item = _items(config_dir, claude)["capture"]
    assert item.state == "problem"
    assert "settings.json does not run capture-hook.py when you send a message." in item.detail
    assert item.fix == "claudeglass capture connect"


@pytest.mark.parametrize("item", ["plan_check", "feedback_reminder"])
def test_a_message_feedback_item_with_no_message_entry_is_a_problem_at_capture_off(tmp_path, item):
    config_dir, claude = _dirs(tmp_path)
    set_capture(config_dir, level="off", feedback=[item], now=NOW)
    hook_health.install_hook_files(config_dir, hook_health.CAPTURE_FILES[capture_catalogue.HOOK_SCRIPT])
    item = _items(config_dir, claude)["capture"]
    assert item.state == "problem"
    assert "capture-hook.py when you send a message" in item.detail


def test_capture_off_with_no_coaching_or_survey_item_is_still_just_off(tmp_path):
    config_dir, claude = _dirs(tmp_path)
    set_capture(config_dir, level="off", now=NOW)
    item = _items(config_dir, claude)["capture"]
    assert (item.state, item.fix) == ("off", "claudeglass capture on")


def test_the_feedback_skill_alone_with_no_message_entry_is_not_a_problem(tmp_path):
    # The skill works without the hook (it only loses the facts line the entry adds), so nothing is said.
    config_dir, claude = _dirs(tmp_path)
    set_capture(config_dir, level="off", feedback=["feedback_skill"], now=NOW)
    hook_health.install_hook_files(config_dir, hook_health.CAPTURE_FILES[capture_catalogue.HOOK_SCRIPT])
    item = _items(config_dir, claude)["capture"]
    assert (item.state, item.essential) == ("off", False)
    assert "still on" not in item.detail and "settings.json does not run" not in item.detail


def test_the_feedback_skill_does_not_hide_a_missing_entry_that_the_plan_check_needs(tmp_path):
    config_dir, claude = _dirs(tmp_path)
    set_capture(config_dir, level="off", feedback=["feedback_skill", "plan_check"], now=NOW)
    hook_health.install_hook_files(config_dir, hook_health.CAPTURE_FILES[capture_catalogue.HOOK_SCRIPT])
    assert _items(config_dir, claude)["capture"].state == "problem"


def test_status_after_leaving_deep_with_remove_reads_plain_off(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(installer, "is_registered", lambda *a, **k: True)
    monkeypatch.setattr(installer, "http_health_ok", lambda url: True)
    config_dir, claude = _dirs(tmp_path)
    _billing(config_dir, "auto")
    _connect(config_dir, claude)
    _snapshot(config_dir, "2026-09-22T11:00:00+00:00")
    for argv in (["level", "deep"], ["remove"]):
        assert cli.main(["capture", *argv, "--yes", "--config-dir", str(config_dir)]) == 0
        capsys.readouterr()
    feedback = load_config(config_dir).capture.feedback
    assert "feedback_skill" in feedback
    assert "plan_check" not in feedback and "feedback_reminder" not in feedback
    item = _items(config_dir, claude)["capture"]
    assert (item.state, item.essential) == ("off", False)
    rc, out = _run_status(config_dir, claude, capsys)
    assert rc == 0
    assert "Everything's set up." in out

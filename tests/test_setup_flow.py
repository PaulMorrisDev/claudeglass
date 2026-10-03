"""``init`` (``setup_flow``): four questions by default, one review of
every change before anything is written, the changes in order, then a
summary of what works. Runs through the real parser and ``cli``'s
tools; the logon task is never registered for real (``conftest``).
"""

from __future__ import annotations

import io
import json
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from claudeglass import capture_catalogue as cat
from claudeglass import cli, discovery, hook_health, installer, setup_flow
from claudeglass.config import ConfigError, check_config_values, feedback_ids, load_config, saved_billing, set_capture
from claudeglass.fixes import RESTART_NOTE

from helpers import assert_privacy_deep

NOW = datetime(2026, 9, 25, 9, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _claude_folder(tmp_path, monkeypatch):
    """settings.json lives in ``<tmp>/claude`` (``$CLAUDE_CONFIG_DIR``),
    and the current folder is a project with no history."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    work = tmp_path / "work" / "my-proj"
    work.mkdir(parents=True)
    monkeypatch.chdir(work)


def _claude(tmp_path, settings=None):
    claude = tmp_path / "claude"
    config_dir = claude / "claudeglass"
    config_dir.mkdir(parents=True)
    if settings is not None:
        (claude / "settings.json").write_text(json.dumps(settings, indent=2), encoding="utf-8")
    return config_dir


def _settings(config_dir) -> dict:
    return json.loads((config_dir.parent / "settings.json").read_text(encoding="utf-8"))


def _skill(config_dir):
    return config_dir.parent / "skills" / "cg-feedback" / "SKILL.md"


def _run(config_dir, *argv, stdin="", sleep=None, clock=None) -> tuple[int, str]:
    args = cli._make_parser().parse_args(["init", "--config-dir", str(config_dir), *argv])
    out = io.StringIO()
    extra = {k: v for k, v in (("sleep", sleep), ("clock", clock)) if v is not None}
    rc = setup_flow.run(*cli._setup_flow_inputs(args), stdin=io.StringIO(stdin), stdout=out, now=NOW, **extra)
    return rc, out.getvalue()


def _in_order(out: str, *texts: str) -> None:
    positions = [out.index(text) for text in texts]
    assert positions == sorted(positions), texts


# -- the default path -----------------------------------------------------------


def test_the_default_path_asks_four_questions_then_sets_everything_up(tmp_path):
    config_dir = _claude(tmp_path, {})
    # A plan; connect; not at logon; sharper tips; go ahead.
    rc, out = _run(config_dir, stdin="1\ny\nn\ny\n\n")
    assert rc == 0, out
    _in_order(
        out,
        "Everything stays on this computer",
        "Choose 1 or 2:",
        "Connect? [Y/n]:",
        "Start it at logon? [Y/n]:",
        "Turn on sharper tips? [y/N]:",
        "Ready to set up:",
        "Go ahead? (d shows the exact changes) [Y/n/d]:",
        "Wrote config.toml",
        "Your setup",
        "Check your setup any time: claudeglass status",
    )
    # Nothing --advanced asks.
    assert "Time zone" not in out and "Metrics capture level:" not in out
    assert "How you pay: a plan (Pro, Max, Team or Enterprise)" in out
    assert "Sharper tips (metrics capture): turn on Essentials until 2026-10-09, and add the /cg-feedback skill." in out
    assert re.search(r"Add \d+ capture hook entries: Claude Code runs capture-hook.py", out)

    config = load_config(config_dir)
    assert config.billing == "subscription"
    assert config.capture.level == "essentials"
    assert config.capture.until == (NOW + timedelta(days=cat.DEFAULT_CAPTURE_TIMEBOX_DAYS)).isoformat(timespec="seconds")
    assert config.capture.feedback == ["feedback_skill", "feedback_note"]
    assert _skill(config_dir).read_text(encoding="utf-8") == cat.feedback_skill_text()
    assert hook_health.check(config_dir, claude_root=config_dir.parent).command is not None
    assert (config_dir / "hooks" / "snapshot-config.py").is_file()
    assert "After a piece of work, run /cg-feedback in Claude Code." in out
    assert RESTART_NOTE in out
    assert not (config_dir / "projects").exists()


def test_the_tips_question_shows_the_catalogue_cost(tmp_path):
    config_dir = _claude(tmp_path, {})
    _rc, out = _run(config_dir, stdin="1\ny\nn\nn\n\n")
    cost = cat.rough_tokens(cat.level_includes("essentials"))
    assert f"about {cost['session_note']} tokens when a session starts and {cost['reply_tag']} per reply" in out
    assert "Sharper tips (metrics capture): off." in out
    assert load_config(config_dir).capture.level == "off"


def test_billing_asks_again_on_a_blank_or_unknown_answer(tmp_path):
    config_dir = _claude(tmp_path, {})
    rc, out = _run(config_dir, "--no-install", "--no-service", stdin="\nmaybe\n2\n\n")
    assert rc == 0
    assert out.count("Choose 1 or 2:") == 3
    assert "Type 1 for a plan or 2 for an API key." in out
    assert load_config(config_dir).billing == "api"


def test_a_saved_auto_is_kept_on_enter(tmp_path):
    config_dir = _claude(tmp_path, {})
    (config_dir / "config.toml").write_text('billing = "auto"\n', encoding="utf-8")
    rc, out = _run(config_dir, "--no-install", "--no-service", stdin="\n\n")
    assert rc == 0
    assert "Choose 1 or 2 [auto]:" in out
    assert saved_billing(config_dir) == "auto"


def test_d_shows_the_exact_changes_and_n_writes_nothing(tmp_path):
    config_dir = _claude(tmp_path, {"model": "opus"})
    before = (config_dir.parent / "settings.json").read_text(encoding="utf-8")
    # An API key; connect; not at logon; no tips; d, then n.
    rc, out = _run(config_dir, stdin="2\ny\nn\nn\nd\nn\n")
    assert rc == 0
    assert "+++" in out and "SessionStart" in out
    assert out.count("Go ahead?") == 2
    assert "Nothing was changed. Run 'claudeglass init' again when you're ready." in out
    assert (config_dir.parent / "settings.json").read_text(encoding="utf-8") == before
    assert list(config_dir.iterdir()) == []


def test_dry_run_shows_everything_and_writes_nothing(tmp_path):
    config_dir = _claude(tmp_path, {"model": "opus"})
    before = (config_dir.parent / "settings.json").read_text(encoding="utf-8")
    rc, out = _run(
        config_dir, "--dry-run", "--non-interactive", "--connect", "--install-service", "--capture-level", "essentials",
        "--feedback", "on",
    )
    assert rc == 0, out
    assert "Dry run: nothing was written." in out
    assert "SessionStart" in out and "install-service:" in out
    assert "The /cg-feedback skill: writes" in out
    # The real snapshot hook installer never ran: no hooks folder, no
    # config.toml, no salt, no digest cache.
    assert list(config_dir.iterdir()) == []
    assert (config_dir.parent / "settings.json").read_text(encoding="utf-8") == before
    assert not _skill(config_dir).exists()


def test_declining_to_connect_leaves_the_tips_question_out(tmp_path):
    config_dir = _claude(tmp_path, {})
    rc, out = _run(config_dir, stdin="1\nn\nn\n\n")
    assert rc == 0
    assert "Turn on sharper tips?" not in out
    assert "Sharper tips need the Claude Code connection, so they stay off." in out
    assert "Connect to Claude Code: not now. Run 'claudeglass init --connect' to connect later." in out
    assert _settings(config_dir) == {}


def test_a_rerun_keeps_deep_and_adds_its_hooks_when_connecting(tmp_path):
    config_dir = _claude(tmp_path, {})
    set_capture(config_dir, level="deep", now=NOW)
    rc, out = _run(config_dir, stdin="1\ny\nn\n\n")
    assert rc == 0, out
    assert "Turn on sharper tips?" not in out
    assert "Sharper tips (metrics capture): Deep" in out and ", unchanged." in out
    assert load_config(config_dir).capture.level == "deep"
    commands = set(cli._capture_hook_commands(config_dir).values())
    entries = [e for groups in _settings(config_dir)["hooks"].values() for g in groups for e in g["hooks"]]
    assert len([e for e in entries if e["command"] in commands]) == len(
        hook_health.capture_specs(cat.level_metrics("deep"))
    )
    # Deep turned the survey on, so its skill comes too.
    assert "The /cg-feedback skill: add it." in out and _skill(config_dir).is_file()


def test_connecting_adds_the_stop_entry_and_the_plan_matcher_when_coaching_notes_are_on(tmp_path):
    config_dir = _claude(tmp_path, {})
    set_capture(config_dir, coaching=["coaching_notes"], now=NOW)
    assert load_config(config_dir).capture.level == "off"
    # A plan; connect; not at logon; go ahead. Capture is off, so there is no tips question.
    rc, out = _run(config_dir, stdin="1\ny\nn\n\n")
    assert rc == 0, out
    commands = set(cli._capture_hook_commands(config_dir).values())
    hooked = [
        (event, group.get("matcher", ""), bool(entry.get("async")))
        for event, groups in _settings(config_dir)["hooks"].items()
        for group in groups
        for entry in group["hooks"]
        if entry["command"] in commands
    ]
    assert sorted(hooked) == sorted([
        ("UserPromptSubmit", "", False),
        ("PostToolUse", "|".join(cat.COACHING_TOOLS), False),
        ("Stop", "", True),
    ])
    assert "ExitPlanMode" in dict((event, matcher) for event, matcher, _ in hooked)["PostToolUse"]
    assert hook_health.check_capture(
        hook_health.capture_specs(load_config(config_dir).capture.hook_metrics()),
        claude_root=config_dir.parent,
        config_dir=config_dir,
    ).ok


def test_a_rerun_skips_what_is_done_and_no_to_tips_turns_essentials_off(tmp_path):
    config_dir = _claude(tmp_path, {})
    _run(config_dir, stdin="1\ny\nn\ny\n\n")
    assert load_config(config_dir).capture.level == "essentials"
    rc, out = _run(config_dir, stdin="\nn\nn\n\n")
    assert rc == 0, out
    assert "Choose 1 or 2 [1]:" in out
    assert "Connect? [Y/n]:" not in out and "Connect to Claude Code: already connected." in out
    assert "Turn on sharper tips? [Y/n]:" in out
    assert "Sharper tips (metrics capture): turn off, and take their hooks out of settings.json." in out
    assert load_config(config_dir).capture.level == "off"
    commands = set(cli._capture_hook_commands(config_dir).values())
    entries = [e for groups in _settings(config_dir).get("hooks", {}).values() for g in groups for e in g["hooks"]]
    assert not [e for e in entries if e["command"] in commands]


def test_advanced_asks_every_question_and_writes_the_project_file(tmp_path):
    config_dir = _claude(tmp_path, {})
    # A plan; six settings; no connection; capture off; no feedback; go.
    rc, out = _run(config_dir, "--advanced", "--no-service", stdin="1\n" + "\n" * 6 + "n\n\n\n\n")
    assert rc == 0, out
    _in_order(out, "Choose 1 or 2:", "More settings (--advanced)", "Time zone", "Connect? [Y/n]:",
              "Metrics capture level:", "Add the /cg-feedback skill?", "Ready to set up:")
    slug = discovery.slug_for(str(tmp_path / "work" / "my-proj"))
    assert (config_dir / "projects" / f"{slug}.toml").is_file()


def test_a_bad_capture_for_stops_init_before_anything_is_written(tmp_path):
    config_dir = _claude(tmp_path, {})
    rc, out = _run(
        config_dir, "--non-interactive", "--no-install", "--capture-level", "essentials", "--capture-for", "soon"
    )
    assert rc == 2
    assert "claudeglass init: --capture-for 'soon'" in out
    assert list(config_dir.iterdir()) == []


# -- the logon task ---------------------------------------------------------------


def _fake_install(monkeypatch, *, answers_after: int):
    """``installer.install`` recording its calls, and a dashboard that
    answers on the ``answers_after``-th check after it (never, for -1)."""
    calls, checks = [], []

    def health(url):
        if not calls:
            return False
        checks.append(url)
        return answers_after >= 0 and len(checks) > answers_after

    monkeypatch.setattr(installer, "install", lambda plan, **k: calls.append(k) or 0)
    monkeypatch.setattr(installer, "http_health_ok", health)
    return calls


class _Clock:
    def __init__(self):
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def test_the_logon_task_is_installed_quietly_and_polled_until_it_answers(tmp_path, monkeypatch):
    config_dir = _claude(tmp_path, {})
    calls = _fake_install(monkeypatch, answers_after=3)
    clock = _Clock()
    rc, out = _run(config_dir, "--no-install", stdin="1\ny\n\n", sleep=clock.sleep, clock=clock)
    assert rc == 0, out
    assert calls == [{"quiet": True}]
    assert "Starting the dashboard... running." in out
    assert clock.slept == [setup_flow._HEALTH_STEP_S] * 3
    assert f"Next: open {installer.DEFAULT_URL} to see where your tokens go." in out
    # install-service's own lines stay out of init.
    assert "will run:" not in out and "Service install complete" not in out


def test_a_dashboard_that_never_answers_is_waited_for_then_reported(tmp_path, monkeypatch):
    config_dir = _claude(tmp_path, {})
    _fake_install(monkeypatch, answers_after=-1)
    clock = _Clock()
    rc, out = _run(config_dir, "--no-install", stdin="1\ny\n\n", sleep=clock.sleep, clock=clock)
    assert rc == 0
    assert "Starting the dashboard... not answering yet: it can take a few seconds to start." in out
    assert sum(clock.slept) == pytest.approx(setup_flow.HEALTH_WAIT_S)
    assert "Next: run 'claudeglass serve'" in out


def test_a_failed_install_is_reported_and_exits_2(tmp_path, monkeypatch):
    config_dir = _claude(tmp_path, {})

    def fail(plan, **k):
        raise installer.InstallerError("command failed (1): schtasks")

    monkeypatch.setattr(installer, "install", fail)
    rc, out = _run(config_dir, "--no-install", stdin="1\ny\n\n")
    assert rc == 2
    assert "Starting the dashboard... it couldn't be set up:\ncommand failed (1): schtasks" in out
    # The quick writes before it still happened.
    assert load_config(config_dir).billing == "subscription"


def test_install_quiet_prints_nothing(tmp_path, capsys):
    plan = installer.plan_service_install("python", tmp_path / "projects", tmp_path / "cfg", platform="linux")
    runner = SimpleNamespace(calls=[])

    def run(command, **kwargs):
        runner.calls.append(command)
        return SimpleNamespace(returncode=0)

    assert installer.install(plan, runner=run, quiet=True) == 0
    assert capsys.readouterr().out == ""
    assert runner.calls == plan.commands


# -- the parts --------------------------------------------------------------------


def test_capture_hook_entries_are_counted_not_listed():
    changes = [
        "Add the config snapshot hook",
        "Add the capture hook that runs a.py on SessionStart",
        "Add the capture hook that runs a.py on Stop",
        "Add the capture hook that runs a.py on PostToolUse",
        "Remove the capture hook that runs b.py on Stop",
    ]
    grouped = setup_flow._grouped(changes)
    assert grouped[0] == "Add the config snapshot hook"
    assert grouped[1].startswith("Add 3 capture hook entries:")
    # One of a kind stays as it is.
    assert grouped[2] == "Remove the capture hook that runs b.py on Stop"


def test_check_config_values_raises_and_writes_nothing(tmp_path):
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    with pytest.raises(ConfigError):
        check_config_values(config_dir, {"billing": "not-a-mode"})
    check_config_values(config_dir, {"billing": "api"})
    assert list(config_dir.iterdir()) == []


def test_feedback_ids_switch_the_skill_and_its_notes():
    assert feedback_ids([], True) == ["feedback_skill", "feedback_note"]
    assert feedback_ids(["dashboard_rating", "feedback_skill"], True) == [
        "dashboard_rating", "feedback_skill", "feedback_note",
    ]
    assert feedback_ids(["dashboard_rating", "feedback_skill", "feedback_note", "feedback_reminder"], False) == [
        "dashboard_rating"
    ]


def test_the_summary_says_what_needs_attention(tmp_path):
    config_dir = _claude(tmp_path, {})
    rc, out = _run(config_dir, "--non-interactive", "--no-install", "--no-service")
    assert rc == 0
    summary = out[out.index("Your setup"):]
    assert "Connected to Claude Code" in summary and "Needs attention" in summary
    assert "1 thing needs attention." in summary
    assert "{{" not in summary
    # The dashboard's own address is the one URL init prints.
    assert_privacy_deep({"stdout": out.replace(installer.DEFAULT_URL, "")})

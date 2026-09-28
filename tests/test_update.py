"""``update``: install with pip, then hand over to the new version's
``update --finish``, which restarts the dashboard, brings settings.json
up to date and looks for copies installed for other Pythons
(``upgrade.py``, ``installer.registered_python``,
``hook_health.plan_statusline_python``)."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from claudeglass import __version__, capture_catalogue, cli, hook_health, installer, upgrade

_writable = cli._writable


@pytest.fixture(autouse=True)
def _folders_writable(monkeypatch):
    """The test run's own Python may have a Scripts folder only an
    administrator can change; ``update`` must not stop over that here."""
    monkeypatch.setattr(cli, "_writable", lambda folder: True)


def _args(*extra):
    return cli._make_parser().parse_args(["update", *extra])


# -- update: install, then hand over ----------------------------------------------


class _Runner:
    def __init__(self, *, pip_rc=0, version="9.9.9", finish_rc=0):
        self.calls: list[list[str]] = []
        self.pip_rc, self.version, self.finish_rc = pip_rc, version, finish_rc

    def __call__(self, command, **kwargs):
        self.calls.append(list(command))
        if "pip" in command:
            return subprocess.CompletedProcess(command, self.pip_rc)
        if "-c" in command:
            return subprocess.CompletedProcess(command, 0, stdout=self.version + "\n")
        return subprocess.CompletedProcess(command, self.finish_rc)


def test_update_dry_run_prints_both_steps_and_runs_nothing(capsys):
    assert cli.main(["update", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "--force-reinstall" in out
    assert "update --finish" in out
    assert "Dry run" in out


def test_update_installs_then_hands_over_to_the_new_version(capsys):
    runner = _Runner()
    assert cli._cmd_update(_args(), runner=runner) == 0
    assert runner.calls[0][1:4] == ["-m", "pip", "install"]
    assert runner.calls[0][-1] == cli.UPDATE_SOURCE
    # The finishing steps run in a new process, so they are the new version's.
    assert runner.calls[-1][:5] == [sys.executable, "-m", "claudeglass", "update", "--finish"]
    assert "Installed version 9.9.9" in capsys.readouterr().out


def test_update_returns_what_finish_returns(capsys):
    assert cli._cmd_update(_args(), runner=_Runner(finish_rc=1)) == 1


def test_update_passes_its_flags_on_to_finish(tmp_path, capsys):
    runner = _Runner()
    args = _args(
        "--config-dir", str(tmp_path), "--port", "8799", "--claude-root", str(tmp_path / "claude"),
        "--no-service", "--yes",
    )
    cli._cmd_update(args, runner=runner)
    finish = runner.calls[-1]
    assert finish[finish.index("--config-dir") + 1] == str(cli._resolve_config_dir(str(tmp_path)))
    assert finish[finish.index("--port") + 1] == "8799"
    assert finish[finish.index("--claude-root") + 1] == str(cli._resolve_claude_root(str(tmp_path / "claude")))
    assert "--no-service" in finish and "--yes" in finish
    assert "--projects-root" in finish
    # The flags parse as an update --finish command line.
    assert cli._make_parser().parse_args(finish[3:]).finish


def test_update_stops_when_pip_fails(capsys, monkeypatch):
    runner = _Runner(pip_rc=1)
    assert cli._cmd_update(_args(), runner=runner) == 1
    assert len(runner.calls) == 1
    err = capsys.readouterr().err
    assert "hook entries were left as they were" in err and "install records" not in err
    # pip's rollback can leave the old record beside the new one.
    monkeypatch.setattr(cli, "_install_records", lambda: ["0.10.0", "0.11.0"])
    assert cli._cmd_update(_args(), runner=_Runner(pip_rc=1)) == 1
    assert "It left 2 install records (0.10.0, 0.11.0)" in capsys.readouterr().err


def test_update_stops_before_pip_when_it_could_only_fail_part_way(capsys, monkeypatch, tmp_path):
    """A Python whose packages folder can be written but whose Scripts
    folder only an administrator can change: pip removed the old copy,
    failed on claudeglass.exe and rolled back only part of it."""
    packages, scripts = tmp_path / "site-packages", tmp_path / "Scripts"
    packages.mkdir()
    scripts.mkdir()
    import sysconfig

    monkeypatch.setattr(sysconfig, "get_path", lambda name: str({"purelib": packages, "scripts": scripts}[name]))
    monkeypatch.setattr(cli, "_writable", lambda folder: folder == str(packages))
    runner = _Runner()
    assert cli._cmd_update(_args(), runner=runner) == 1
    assert runner.calls == []
    err = capsys.readouterr().err
    assert str(scripts) in err and "administrator" in err and "Nothing was changed" in err
    # Neither writable: pip installs for the user instead, so it goes ahead.
    monkeypatch.setattr(cli, "_writable", lambda folder: False)
    assert cli._cmd_update(_args(), runner=runner) == 0
    assert runner.calls


def test_writable_tries_one_file_and_leaves_nothing(tmp_path):
    assert _writable(str(tmp_path)) and list(tmp_path.iterdir()) == []
    assert not _writable(str(tmp_path / "missing"))


def test_update_from_a_local_folder(capsys):
    runner = _Runner()
    cli._cmd_update(_args("--from", "."), runner=runner)
    assert runner.calls[0][-1] == "."


def test_update_dispatches_finish(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "_cmd_update_finish", lambda args: seen.append("finish") or 0)
    monkeypatch.setattr(cli, "_cmd_update", lambda args: seen.append("update") or 0)
    cli.main(["update", "--finish"])
    cli.main(["update"])
    assert seen == ["finish", "update"]


# -- update --finish -----------------------------------------------------------


class _Finish:
    """Runs ``update --finish`` with every outside call faked."""

    def __init__(
        self,
        tmp_path,
        *,
        registered=True,
        answering=(__version__,),
        holder=None,
        copies=(),
        ran_from=None,
        install_rc=0,
        stop_rc=0,
        uninstall_rc=0,
    ):
        self.tmp_path = tmp_path
        self.registered = registered
        self.answering = list(answering)
        self.holder = holder
        self.copies = list(copies)
        self.ran_from = ran_from
        self.install_rc, self.stop_rc, self.uninstall_rc = install_rc, stop_rc, uninstall_rc
        self.installs = 0
        self.commands: list[list[str]] = []
        self.copies_asked_with = None
        (tmp_path / "claude").mkdir(exist_ok=True)

    def runner(self, command, **kwargs):
        self.commands.append(list(command))
        if command[0] == "taskkill":
            return subprocess.CompletedProcess(command, self.stop_rc)
        if "uninstall" in command:
            return subprocess.CompletedProcess(command, self.uninstall_rc)
        return subprocess.CompletedProcess(command, 0)

    def install_service(self, args):
        self.installs += 1
        return self.install_rc

    def health(self, url):
        return self.answering.pop(0) if len(self.answering) > 1 else self.answering[0]

    def find_copies(self, also):
        self.copies_asked_with = also
        return self.copies

    def run(self, *flags, answers=""):
        args = _args(
            "--finish", "--config-dir", str(self.tmp_path / "cfg"), "--claude-root", str(self.tmp_path / "claude"), *flags
        )
        out = io.StringIO()
        rc = cli._cmd_update_finish(
            args,
            stdin=io.StringIO(answers),
            stdout=out,
            runner=self.runner,
            is_registered_fn=lambda: self.registered,
            install_service_fn=self.install_service,
            health_version_fn=self.health,
            registered_python_fn=lambda: self.ran_from,
            copies_fn=self.find_copies,
            port_holder_fn=lambda port: self.holder,
        )
        return rc, out.getvalue()


def _old_copy(tmp_path) -> upgrade.Copy:
    return upgrade.Copy(python=str(tmp_path / "old" / "python.exe"), version="0.5.0", prefix=str(tmp_path / "old"))


def _statusline(tmp_path, python: str) -> Path:
    settings = tmp_path / "claude" / "settings.json"
    settings.parent.mkdir(exist_ok=True)
    command = f'"{python}" -m claudeglass.statusline --config-dir "{tmp_path / "cfg"}"'
    settings.write_text(json.dumps({"statusLine": {"type": "command", "command": command}}), encoding="utf-8")
    return settings


def test_finish_restarts_a_registered_dashboard_and_reports_done(tmp_path):
    finish = _Finish(tmp_path)
    rc, out = finish.run()
    assert rc == 0
    assert finish.installs == 1
    assert "Up to date" in out
    assert f"done. Version {__version__}" in out
    assert "runs it" in out


def test_finish_leaves_an_unregistered_dashboard_alone(tmp_path):
    finish = _Finish(tmp_path, registered=False, answering=(None,))
    rc, out = finish.run()
    assert rc == 0
    assert finish.installs == 0
    assert "nothing to restart" in out


def test_finish_no_service_leaves_the_dashboard_alone(tmp_path):
    finish = _Finish(tmp_path, answering=(None,))
    rc, out = finish.run("--no-service")
    assert (rc, finish.installs) == (0, 0)
    assert "--no-service" in out


def test_finish_reports_a_failed_registration(tmp_path):
    rc, _out = _Finish(tmp_path, install_rc=2).run()
    assert rc == 2


def test_finish_dry_run_changes_nothing_and_asks_nothing(tmp_path):
    settings = _statusline(tmp_path, str(tmp_path / "old" / "python.exe"))
    before = settings.read_text(encoding="utf-8")
    finish = _Finish(tmp_path, copies=[_old_copy(tmp_path)])
    rc, out = finish.run("--dry-run")
    assert rc == 0
    assert settings.read_text(encoding="utf-8") == before
    assert not finish.commands  # nothing stopped or uninstalled
    assert "(y/n)" not in out
    assert "Dry run" in out


def test_finish_offers_to_stop_an_old_dashboard_holding_the_port(tmp_path):
    finish = _Finish(tmp_path, answering=("0.5.0", __version__), holder=(4242, r"C:\Old\pythonw.exe"))
    rc, out = finish.run(answers="y\n")
    assert rc == 0
    assert "process 4242" in out
    assert ["taskkill", "/PID", "4242", "/F"] in finish.commands
    assert finish.installs == 2  # restarted after the old one stopped


def test_finish_keeps_an_old_dashboard_on_no_and_says_so(tmp_path):
    finish = _Finish(tmp_path, answering=("0.5.0",), holder=(4242, r"C:\Old\pythonw.exe"))
    rc, out = finish.run(answers="n\n")
    assert rc == 1
    assert not any(c[0] == "taskkill" for c in finish.commands)
    assert "still answers with version 0.5.0" in out


def test_finish_never_stops_a_port_holder_that_is_not_python(tmp_path):
    finish = _Finish(tmp_path, answering=("older than 0.4.1",), holder=(77, r"C:\Docker\com.docker.backend.exe"))
    rc, out = finish.run("--yes")
    assert rc == 1
    assert not any(c[0] == "taskkill" for c in finish.commands)
    assert "isn't a Python" in out


def test_finish_repoints_a_statusline_that_runs_another_python(tmp_path):
    settings = _statusline(tmp_path, str(tmp_path / "old" / "python.exe"))
    rc, out = _Finish(tmp_path).run(answers="y\n")
    assert rc == 0
    command = json.loads(settings.read_text(encoding="utf-8"))["statusLine"]["command"]
    assert command.startswith(f'"{sys.executable}" -m claudeglass.statusline --config-dir')
    assert "The previous settings.json is at" in out


@pytest.mark.parametrize("name", ["tl-feedback", "cl-feedback"])
def test_finish_renames_a_skill_under_an_earlier_name_and_refreshes_an_old_one(tmp_path, name):
    skills = tmp_path / "claude" / "skills"
    old = skills / name / "SKILL.md"
    old.parent.mkdir(parents=True)
    old_text = capture_catalogue.feedback_skill_text().replace("name: cg-feedback\n", f"name: {name}\n")
    old.write_text(old_text, encoding="utf-8")
    brief = skills / "cg-brief" / "SKILL.md"
    brief.parent.mkdir(parents=True)
    brief.write_text(capture_catalogue.brief_skill_text() + "An earlier version's line.\n", encoding="utf-8")
    rc, out = _Finish(tmp_path).run("--dry-run")
    assert f"/{name} is now called /cg-feedback" in out and "This updates the /cg-brief skill" in out
    assert old.read_text(encoding="utf-8") == old_text and not (skills / "cg-feedback").exists()
    rc, out = _Finish(tmp_path).run("--yes")
    assert rc == 0 and "Up to date" not in out
    assert (skills / "cg-feedback" / "SKILL.md").read_text(encoding="utf-8") == capture_catalogue.feedback_skill_text()
    assert not old.parent.exists()
    assert brief.read_text(encoding="utf-8") == capture_catalogue.brief_skill_text()
    rc, out = _Finish(tmp_path).run()
    assert "Up to date: the hooks, statusline and skills" in out


def test_finish_leaves_a_skill_it_did_not_write_alone(tmp_path):
    mine = tmp_path / "claude" / "skills" / "tl-feedback" / "SKILL.md"
    mine.parent.mkdir(parents=True)
    mine.write_text("---\nname: tl-feedback\n---\nmine\n", encoding="utf-8")
    rc, out = _Finish(tmp_path).run("--yes")
    assert "Up to date" in out and mine.exists() and not (tmp_path / "claude" / "skills" / "cg-feedback").exists()


def test_finish_leaves_the_statusline_on_no(tmp_path):
    settings = _statusline(tmp_path, str(tmp_path / "old" / "python.exe"))
    before = settings.read_text(encoding="utf-8")
    _rc, out = _Finish(tmp_path).run(answers="n\n")
    assert settings.read_text(encoding="utf-8") == before
    assert "Left unchanged" in out


def test_finish_removes_a_copy_nothing_uses_after_a_yes(tmp_path):
    copy = _old_copy(tmp_path)
    finish = _Finish(tmp_path, copies=[copy], ran_from=copy.python)
    rc, out = finish.run(answers="y\n")
    assert rc == 0
    assert finish.copies_asked_with == [copy.python]
    assert [copy.python, "-m", "pip", "uninstall", "-y", "claudeglass"] in finish.commands
    assert "version 0.5.0" in out and "Removed." in out


def test_finish_yes_answers_every_question(tmp_path):
    settings = _statusline(tmp_path, str(tmp_path / "old" / "python.exe"))
    copy = _old_copy(tmp_path)
    finish = _Finish(tmp_path, copies=[copy])
    rc, out = finish.run("--yes")
    assert rc == 0
    assert sys.executable in settings.read_text(encoding="utf-8").replace("\\\\", "\\")
    assert any("uninstall" in c for c in finish.commands)
    assert "(y/n)" not in out


def test_finish_keeps_a_copy_the_statusline_still_runs(tmp_path):
    copy = _old_copy(tmp_path)
    _statusline(tmp_path, copy.python)
    finish = _Finish(tmp_path, copies=[copy])
    _rc, out = finish.run(answers="n\n")  # no to the statusline change
    assert not any("uninstall" in c for c in finish.commands)
    assert "The statusline still runs it" in out
    assert "-m pip uninstall claudeglass" in out


def test_finish_keeps_the_copy_the_logon_service_still_starts(tmp_path):
    copy = _old_copy(tmp_path)
    ran_from = str(tmp_path / "old" / "pythonw.exe")
    for flags, install_rc in ((("--no-service", "--yes"), 0), (("--yes",), 2)):
        finish = _Finish(tmp_path, answering=(None,), copies=[copy], ran_from=ran_from, install_rc=install_rc)
        _rc, out = finish.run(*flags)
        assert not any("uninstall" in c for c in finish.commands)
        assert "still starts from it at logon" in out


def test_finish_keeps_every_copy_while_an_old_dashboard_answers(tmp_path):
    finish = _Finish(tmp_path, answering=("0.5.0",), copies=[_old_copy(tmp_path)])
    rc, out = finish.run("--yes")
    assert rc == 1
    assert not any("uninstall" in c for c in finish.commands)
    assert "An older dashboard still runs" in out


def test_finish_says_when_pip_cannot_remove_a_copy(tmp_path):
    finish = _Finish(tmp_path, copies=[_old_copy(tmp_path)], uninstall_rc=1)
    _rc, out = finish.run("--yes")
    assert "pip could not remove it" in out


def test_finish_refreshes_outdated_hook_files(tmp_path, monkeypatch):
    monkeypatch.setattr(hook_health, "refresh_hook_files", lambda config_dir: [Path("a"), Path("b")])
    _rc, out = _Finish(tmp_path).run()
    assert "Refreshed 2 hook files this version changed." in out


def test_finish_offers_the_snapshot_hook_repair(tmp_path, monkeypatch):
    offered = []

    def offer(health, *, repair_hook, non_interactive, **kwargs):
        offered.append((repair_hook, non_interactive))

    monkeypatch.setattr(
        hook_health, "check", lambda config_dir, claude_root=None: hook_health.HookHealth(
            settings_path=tmp_path / "claude" / "settings.json", command="old", fixed_command="new"
        )
    )
    monkeypatch.setattr(cli.onboarding, "offer_hook_repair", offer)
    _Finish(tmp_path).run("--yes")
    assert offered == [(True, False)]


# -- upgrade.py ---------------------------------------------------------------------


def _completed(stdout="", rc=0):
    return lambda command, **kwargs: subprocess.CompletedProcess(command, rc, stdout=stdout)


def test_candidate_pythons_reads_the_py_launcher_and_path(tmp_path):
    on_path = tmp_path / "bin"
    on_path.mkdir()
    (on_path / "python.exe").write_text("", encoding="utf-8")
    listing = " -V:3.14 *        C:\\Py314\\python.exe\n -V:3.11          C:\\Python311\\python.exe\n"
    found = upgrade.candidate_pythons(
        also=[r"C:\Py314\python.exe", None], runner=_completed(listing), windows=True, path=str(on_path)
    )
    assert found == [r"C:\Py314\python.exe", r"C:\Python311\python.exe", str(on_path / "python.exe")]


def test_candidate_pythons_on_posix_skips_the_launcher(tmp_path):
    (tmp_path / "python3").write_text("", encoding="utf-8")
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="")

    found = upgrade.candidate_pythons(also=[], runner=runner, windows=False, path=str(tmp_path))
    assert found == [str(tmp_path / "python3")]
    assert calls == []


def test_candidate_pythons_survives_no_py_launcher(tmp_path):
    def runner(command, **kwargs):
        raise FileNotFoundError(command[0])

    assert upgrade.candidate_pythons(also=["/usr/bin/python3"], runner=runner, windows=True, path="") == [
        "/usr/bin/python3"
    ]


def test_other_copies_skips_this_install_and_duplicates(tmp_path):
    this, other = tmp_path / "this", tmp_path / "other"
    this.mkdir()
    other.mkdir()
    answers = {
        "a": f"{this}\n0.6.1\n",  # this Python's own install
        "b": f"{other}\n0.5.0\n",
        "c": f"{other}\n0.5.0\n",  # the same install by another path
    }

    def runner(command, **kwargs):
        python = command[0]
        if python == "missing":
            return subprocess.CompletedProcess(command, 1, stdout="")
        return subprocess.CompletedProcess(command, 0, stdout=answers[python])

    copies = upgrade.other_copies(["a", "b", "c", "missing"], runner=runner, this_prefix=str(this))
    assert copies == [upgrade.Copy(python="b", version="0.5.0", prefix=str(other))]


def test_other_copies_passes_the_name_as_an_argument():
    seen = []

    def runner(command, **kwargs):
        seen.append(command)
        return subprocess.CompletedProcess(command, 1)

    upgrade.other_copies(["py"], runner=runner, this_prefix="x")
    assert seen[0][1] == "-c" and seen[0][-1] == "claudeglass"
    assert "claudeglass" not in seen[0][2]


def test_port_holder_parses_the_process():
    assert upgrade.port_holder(8765, runner=_completed("4242|C:\\Py\\pythonw.exe\r\n")) == (4242, r"C:\Py\pythonw.exe")
    assert upgrade.port_holder(8765, runner=_completed("")) is None
    assert upgrade.port_holder(8765, runner=_completed("x|y")) is None
    assert upgrade.port_holder(8765, runner=_completed("1|p", rc=1)) is None


def test_is_python():
    assert upgrade.is_python(r"C:\Py\pythonw.exe")
    assert upgrade.is_python("/usr/bin/python3")
    assert not upgrade.is_python(r"C:\Docker\com.docker.backend.exe")
    assert not upgrade.is_python("")


def test_stop_process_and_remove_copy_report_failure():
    assert upgrade.stop_process(1, runner=_completed(rc=0))
    assert not upgrade.stop_process(1, runner=_completed(rc=128))

    def broken(command, **kwargs):
        raise OSError("no pip")

    assert not upgrade.remove_copy(upgrade.Copy("p", "0.5.0", "x"), runner=broken)


# -- installer.registered_python ---------------------------------------------------


def test_registered_python_reads_the_scheduled_task():
    assert installer.registered_python("windows", runner=_completed('"C:\\Py\\pythonw.exe"\r\n')) == r"C:\Py\pythonw.exe"
    assert installer.registered_python("windows", runner=_completed("", rc=1)) is None


def test_registered_python_reads_the_systemd_unit(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert installer.registered_python("linux") is None
    unit = tmp_path / ".config" / "systemd" / "user" / installer.SYSTEMD_UNIT_NAME
    unit.parent.mkdir(parents=True)
    unit.write_text("[Service]\nExecStart=/opt/py/bin/python3 -m claudeglass serve\n", encoding="utf-8")
    assert installer.registered_python("linux") == "/opt/py/bin/python3"


def test_registered_python_reads_the_launch_agent(tmp_path, monkeypatch):
    import plistlib

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    plist = tmp_path / "Library" / "LaunchAgents" / f"{installer.LAUNCHD_LABEL}.plist"
    plist.parent.mkdir(parents=True)
    plist.write_bytes(plistlib.dumps({"ProgramArguments": ["/usr/local/bin/python3", "-m", "claudeglass"]}))
    assert installer.registered_python("macos") == "/usr/local/bin/python3"


# -- hook_health: the statusline's Python ------------------------------------------------


def test_plan_statusline_python_changes_only_the_interpreter(tmp_path):
    _statusline(tmp_path, "/old/python3")
    plan = hook_health.plan_statusline_python("/new/python3", claude_root=tmp_path / "claude")
    assert plan.new_text is not None
    command = json.loads(plan.new_text)["statusLine"]["command"]
    assert command == f'"/new/python3" -m claudeglass.statusline --config-dir "{tmp_path / "cfg"}"'
    assert hook_health.statusline_python(tmp_path / "claude") == "/old/python3"


def test_plan_statusline_python_leaves_a_matching_or_foreign_statusline(tmp_path):
    _statusline(tmp_path, "/new/python3")
    assert hook_health.plan_statusline_python("/new/python3", claude_root=tmp_path / "claude").new_text is None
    settings = tmp_path / "claude" / "settings.json"
    settings.write_text(json.dumps({"statusLine": {"type": "command", "command": "my-own-statusline"}}), encoding="utf-8")
    assert hook_health.plan_statusline_python("/new/python3", claude_root=tmp_path / "claude").new_text is None
    assert hook_health.statusline_python(tmp_path / "claude") is None


def test_plan_statusline_python_refuses_an_unreadable_settings_file(tmp_path):
    (tmp_path / "claude").mkdir()
    (tmp_path / "claude" / "settings.json").write_text("{not json", encoding="utf-8")
    plan = hook_health.plan_statusline_python("/new/python3", claude_root=tmp_path / "claude")
    assert plan.new_text is None and plan.changes


def test_update_help_names_finish_for_old_versions(capsys):
    try:
        cli.main(["update", "--help"])
    except SystemExit:
        pass
    assert "0.6.0 or older" in " ".join(capsys.readouterr().out.split())

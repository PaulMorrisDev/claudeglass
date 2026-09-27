"""Shared pytest configuration.

Makes ``import claudeglass`` work even when the package hasn't been
pip-installed (editable or otherwise) — e.g. running ``python -m pytest``
directly against a checkout with ``PYTHONPATH`` unset. If the package is
already importable (installed, or PYTHONPATH=src is already set), this is
a no-op.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.fixture(autouse=True)
def _isolated_claude_config_dir(tmp_path_factory, monkeypatch):
    """Point every test at a per-test throwaway config dir instead of the
    real ``~/.claude``. ``CLAUDE_CONFIG_DIR`` is what ``pricing.py`` and
    ``hooks/snapshot-config.py`` consult directly; ``HOME``/``USERPROFILE``
    cover anything that falls back to ``Path.home()``. Individual tests can
    still override any of these with their own ``monkeypatch.setenv`` calls.

    Uses ``tmp_path_factory`` (its own base temp dir) rather than the
    test's own ``tmp_path`` fixture, so this fixture never leaks an extra
    "home" entry into a test that lists/asserts on the full contents of
    its own ``tmp_path``.
    """
    fake_home = tmp_path_factory.mktemp("claude_home")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(fake_home / ".claude"))
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    # init looks for WSL distros through wsl.exe; a developer machine
    # with Ubuntu installed would otherwise get an extra question.
    from claudeglass import discovery, hook_health

    monkeypatch.setattr(discovery, "find_wsl_projects_roots", lambda run=None: [])
    # hook_health.hook_policy reads the machine's own managed-settings.json;
    # a machine under an organisation's hook policy would otherwise change
    # what every hook-health test sees.
    # The real function stays reachable for the one test that checks it.
    managed = fake_home / "managed-settings"
    monkeypatch.setattr(hook_health, "_real_managed_settings_dir", hook_health.managed_settings_dir, raising=False)
    monkeypatch.setattr(hook_health, "managed_settings_dir", lambda: managed)
    # The service writes each command in the form that runs on this
    # install (invocation.py), which depends on this machine's PATH; the
    # short form keeps every other test's expected text the same here and
    # on CI. tests/test_invocation.py checks the detection itself.
    monkeypatch.setenv("CLAUDEGLASS_COMMAND", "claudeglass")
    # The capture hook adds nothing to a ``claude -p`` run (its entrypoint
    # starts with "sdk"), and a hook run as a subprocess inherits this
    # process's environment: tests run from inside such a run would
    # otherwise see no notes at all.
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)


@pytest.fixture(autouse=True)
def _no_real_logon_task(monkeypatch):
    """``init`` asks whether the dashboard's logon task is registered and
    answering, and registers it after a yes. On a developer machine with
    the real task set up, a test would otherwise see it, or replace it
    with one pointing at the test's own throwaway config. Here nothing is
    registered and nothing answers at the default address, and
    registering without a ``runner`` fails the test. A call that brings
    its own ``runner``, or asks another address, still reaches the real
    function (tests/test_installer.py); a test that wants a registered or
    running dashboard monkeypatches these itself.
    """
    from claudeglass import installer

    real_registered, real_health, real_install = installer.is_registered, installer.http_health_ok, installer.install

    def is_registered(platform=None, *, runner=None):
        return False if runner is None else real_registered(platform, runner=runner)

    def http_health_ok(url):
        return False if url == installer.DEFAULT_URL else real_health(url)

    def install(plan, *, runner=None, dry_run=False, quiet=False):
        if runner is None and not dry_run:
            raise AssertionError("a test tried to register the real logon task: monkeypatch installer.install")
        return real_install(plan, runner=runner, dry_run=dry_run, quiet=quiet) if runner else real_install(
            plan, dry_run=dry_run, quiet=quiet
        )

    monkeypatch.setattr(installer, "is_registered", is_registered)
    monkeypatch.setattr(installer, "http_health_ok", http_health_ok)
    monkeypatch.setattr(installer, "install", install)


@pytest.fixture(autouse=True)
def _reset_parse_salt():
    """Capture-improvements addition (A3): ``parse._SALT`` is deliberately
    process-wide state (see ``parse.set_salt``'s docstring on why it's a
    module global rather than a ``parse_transcript`` parameter) -- reset
    it after every test so one test's ``set_salt`` call can't leak a salt
    into an unrelated later test. Lazily imports ``parse`` so tests that
    never touch it pay nothing extra.
    """
    yield
    from claudeglass import parse as parse_mod

    parse_mod._SALT = None

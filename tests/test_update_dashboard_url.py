"""``update --finish`` and the dashboard address in the mod's settings file.

The address ``update`` is given (``--port`` and ``--bind``) is where it has
just registered the dashboard, so it wins over an older one in the file; a
dashboard ``update`` leaves alone (``--no-service``) may be anywhere, so an
address the service wrote is kept (``mod_settings.build``, ``cli._write_mod_settings``).
"""

from __future__ import annotations

import pytest

from claudeglass import cli

from test_update import _Finish, _as_the_service_wrote_it, _settings, _write_config


@pytest.fixture(autouse=True)
def _folders_writable(monkeypatch):
    """The test run's own Python may have a Scripts folder only an
    administrator can change; ``update`` must not stop over that here."""
    monkeypatch.setattr(cli, "_writable", lambda folder: True)


def test_finish_points_the_mod_at_the_address_it_just_registered_the_dashboard_on(tmp_path):
    """J32: the address in the file won, so a stale one (a dev port the
    dashboard had since left) stayed until the service wrote the file
    again with a report."""
    _write_config(tmp_path, 'billing = "subscription"\n')
    written = _as_the_service_wrote_it(tmp_path)
    assert written["dashboard_url"] == "http://127.0.0.1:8787/"
    finish = _Finish(tmp_path)
    rc, _ = finish.run()
    assert rc == 0 and finish.installs == 1
    data = _settings(tmp_path)
    assert data["dashboard_url"] == "http://127.0.0.1:8765/"
    # What only a report knows is still carried forward.
    assert data["billing"] == written["billing"] and data["calibration"] == written["calibration"]
    # And a port named on the command line is the one written.
    rc, _ = _Finish(tmp_path).run("--port", "8792", "--bind", "0.0.0.0")
    assert rc == 0 and _settings(tmp_path)["dashboard_url"] == "http://127.0.0.1:8792/"


def test_finish_leaves_the_address_the_service_wrote_when_it_did_not_touch_the_dashboard(tmp_path):
    _write_config(tmp_path, 'billing = "subscription"\n')
    _as_the_service_wrote_it(tmp_path)
    finish = _Finish(tmp_path, answering=(None,))
    rc, _ = finish.run("--no-service")
    assert (rc, finish.installs) == (0, 0)
    # --port's default is only a default here, not where the dashboard is.
    assert _settings(tmp_path)["dashboard_url"] == "http://127.0.0.1:8787/"


def test_finish_with_no_service_and_no_file_yet_writes_the_default_address(tmp_path):
    _write_config(tmp_path, 'billing = "subscription"\n')
    rc, _ = _Finish(tmp_path, answering=(None,)).run("--no-service")
    assert rc == 0 and _settings(tmp_path)["dashboard_url"] == "http://127.0.0.1:8765/"

"""The mod's settings file: its shape, how it is written, and when the
dashboard's service writes it."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from helpers import assert_privacy_deep
from test_service_api import _start_server

from claudeglass import __version__, calibration, capture_catalogue, mod_settings
from claudeglass.config import ConfigError
from claudeglass.units import Units

NOW = datetime(2026, 9, 18, 12, 30, 5, tzinfo=timezone.utc)

TOP_KEYS = {
    "schema",
    "written_at",
    "version",
    "billing",
    "currency",
    "pricing",
    "cache",
    "calibration",
    "coaching",
    "usual",
    "dashboard_url",
}


class FakeStore:
    def __init__(self, cost=None, context=None, error=None):
        self.cost, self.context, self.error = cost, context, error

    def median_session_cost(self):
        if self.error:
            raise self.error
        return self.cost

    def median_end_context(self):
        if self.error:
            raise self.error
        return self.context


def _report(units=None, calibration_dict=None):
    return SimpleNamespace(meta=SimpleNamespace(units=units or {}, calibration=calibration_dict or {}))


def _config(tmp_path, text=""):
    folder = tmp_path / "config"
    folder.mkdir(exist_ok=True)
    (folder / "config.toml").write_text(text, encoding="utf-8")
    return folder


# -- build ----------------------------------------------------------------------


def test_the_file_lives_in_a_mod_folder_under_the_config_dir(tmp_path):
    assert mod_settings.settings_path(tmp_path) == tmp_path / "mod" / "settings.json"


def test_a_default_config_builds_the_documented_shape(tmp_path):
    data = mod_settings.build(_config(tmp_path, 'billing = "api"\n'), now=NOW)
    assert set(data) == TOP_KEYS
    assert data["schema"] == mod_settings.SCHEMA == 1
    assert data["written_at"] == "2026-09-18T12:30:05Z"
    assert data["version"] == __version__
    assert data["billing"] == {
        "mode": "api",
        "share_per_usd": None,
        "period_label": "weekly usage limit",
        "basis": Units(billing_mode="api").basis(),
    }
    assert isinstance(data["currency"], str) and data["currency"]
    assert data["cache"] == {"ttl_seconds": 300}
    assert data["calibration"] == calibration.DEFAULT.to_dict()
    assert data["usual"] == {"session_cost_usd": None, "end_context_tokens": None}
    assert data["dashboard_url"] is None


def test_the_rate_card_gives_each_model_its_aliases_five_rates_and_window(tmp_path):
    pricing = mod_settings.build(_config(tmp_path), now=NOW)["pricing"]
    assert isinstance(pricing["version"], str) and pricing["models"]
    for model_id, entry in pricing["models"].items():
        assert set(entry) == {
            "aliases",
            "input",
            "output",
            "cache_write_5m",
            "cache_write_1h",
            "cache_read",
            "context_window_tokens",
        }, model_id
        assert isinstance(entry["aliases"], list) and entry["aliases"] == sorted(entry["aliases"])
        for rate in ("input", "output", "cache_write_5m", "cache_write_1h", "cache_read"):
            assert isinstance(entry[rate], float) and entry[rate] >= 0, (model_id, rate)
        assert isinstance(entry["context_window_tokens"], int) and entry["context_window_tokens"] > 0


def test_a_subscription_config_with_a_report_carries_the_share_and_the_measured_calibration(tmp_path):
    units = {"share_per_usd": 0.0125, "basis": "Amounts are shares of your weekly usage limit."}
    measured = {"tool": {"opus": 3.2}, "text": {"opus": 3.9}, "default_family": "opus"}
    data = mod_settings.build(_config(tmp_path, 'billing = "subscription"\n'), report=_report(units, measured), now=NOW)
    assert data["billing"]["mode"] == "subscription"
    assert data["billing"]["share_per_usd"] == 0.0125
    assert isinstance(data["billing"]["share_per_usd"], float)
    assert data["billing"]["basis"] == units["basis"]
    assert data["calibration"] == measured


def test_a_subscription_config_without_a_report_leaves_the_share_unknown_and_says_what_amounts_mean(tmp_path):
    data = mod_settings.build(_config(tmp_path, 'billing = "subscription"\n'), now=NOW)
    assert data["billing"]["mode"] == "subscription"
    assert data["billing"]["share_per_usd"] is None
    assert data["billing"]["basis"].startswith("Amounts are list-price equivalents")


def test_a_report_without_calibration_falls_back_to_the_defaults(tmp_path):
    data = mod_settings.build(_config(tmp_path), report=_report({"share_per_usd": None}), now=NOW)
    assert data["calibration"] == calibration.DEFAULT.to_dict()
    assert data["billing"]["share_per_usd"] is None


def test_the_cache_lifetime_follows_the_config(tmp_path):
    data = mod_settings.build(_config(tmp_path, 'default_ttl = "1h"\n'), now=NOW)
    assert data["cache"]["ttl_seconds"] == 3600


def test_your_usual_comes_from_the_store_as_plain_numbers(tmp_path):
    data = mod_settings.build(_config(tmp_path), store=FakeStore(cost=11.82, context=160616), now=NOW)
    assert data["usual"] == {"session_cost_usd": 11.82, "end_context_tokens": 160616}
    assert isinstance(data["usual"]["end_context_tokens"], int)


def test_a_store_that_cannot_answer_leaves_your_usual_unknown(tmp_path):
    data = mod_settings.build(_config(tmp_path), store=FakeStore(error=sqlite3.OperationalError("locked")), now=NOW)
    assert data["usual"] == {"session_cost_usd": None, "end_context_tokens": None}


def test_the_coaching_block_is_the_catalogues_until_you_change_it(tmp_path):
    folder = _config(tmp_path)
    assert mod_settings.build(folder, now=NOW)["coaching"] == {
        "thresholds": capture_catalogue.COACHING_THRESHOLDS,
        "muted": [],
        "once": [],
    }
    (folder / "coaching.json").write_text(
        json.dumps({"thresholds": {"cold_min_tokens": 50_000}, "muted": ["b", "a", 3], "once": ["c"]}),
        encoding="utf-8",
    )
    coaching = mod_settings.build(folder, now=NOW)["coaching"]
    assert coaching["thresholds"]["cold_min_tokens"] == 50_000
    assert coaching["thresholds"]["cold_rest_hours"] == capture_catalogue.COACHING_THRESHOLDS["cold_rest_hours"]
    assert coaching["muted"] == ["a", "b"]
    assert coaching["once"] == ["c"]


def test_a_threshold_in_config_toml_reaches_the_file(tmp_path):
    folder = _config(tmp_path, "[thresholds]\ncoaching_cold_rest_hours = 3\n")
    assert mod_settings.build(folder, now=NOW)["coaching"]["thresholds"]["cold_rest_hours"] == 3


def test_a_config_that_cannot_be_read_raises_config_error(tmp_path):
    with pytest.raises(ConfigError):
        mod_settings.build(_config(tmp_path, 'billing = "neither"\n'), now=NOW)


@pytest.mark.parametrize(
    "bind,port,expected",
    [
        ("127.0.0.1", 8787, "http://127.0.0.1:8787/"),
        ("0.0.0.0", 8787, "http://127.0.0.1:8787/"),
        ("::", 8787, "http://127.0.0.1:8787/"),
        ("::1", 8787, "http://[::1]:8787/"),
        ("127.0.0.1", 0, None),
    ],
)
def test_the_dashboard_url_uses_loopback_for_a_wildcard_bind(bind, port, expected):
    assert mod_settings.dashboard_url(bind, port) == expected


def test_the_file_holds_no_path_prompt_or_address(tmp_path):
    """Numbers, the rate card's model ids and closed words: nothing that
    names a folder, a person or a place on the network. The dashboard's
    own address is left out, since that is what the privacy scan flags."""
    folder = _config(tmp_path, 'billing = "subscription"\n')
    data = mod_settings.build(
        folder,
        report=_report({"share_per_usd": 0.01}, calibration.DEFAULT.to_dict()),
        store=FakeStore(cost=4.5, context=90_000),
        now=NOW,
    )
    assert_privacy_deep(json.loads(json.dumps(data)))
    assert str(tmp_path) not in json.dumps(data)


# -- write ----------------------------------------------------------------------


def test_writing_creates_the_mod_folder_and_the_file_reads_back(tmp_path):
    folder = tmp_path / "fresh"
    data = mod_settings.build(_config(tmp_path), now=NOW)
    assert mod_settings.write(folder, data) is True
    path = mod_settings.settings_path(folder)
    assert path.is_file()
    assert json.loads(path.read_text(encoding="utf-8")) == data
    assert path.read_text(encoding="utf-8").endswith("\n")


def test_no_temporary_file_is_left_behind(tmp_path):
    folder = _config(tmp_path)
    mod_settings.write(folder, mod_settings.build(folder, now=NOW))
    assert [p.name for p in (folder / "mod").iterdir()] == ["settings.json"]


def test_writing_what_is_already_there_apart_from_the_time_leaves_the_file_alone(tmp_path):
    folder = _config(tmp_path)
    first = mod_settings.build(folder, now=NOW)
    assert mod_settings.write(folder, first) is True
    path = mod_settings.settings_path(folder)
    before = path.read_bytes()
    mtime = path.stat().st_mtime_ns
    later = mod_settings.build(folder, now=datetime(2026, 9, 18, 13, 30, 5, tzinfo=timezone.utc))
    assert later["written_at"] != first["written_at"]
    assert mod_settings.write(folder, later) is False
    assert path.read_bytes() == before and path.stat().st_mtime_ns == mtime


def test_a_changed_value_is_written(tmp_path):
    folder = _config(tmp_path)
    mod_settings.write(folder, mod_settings.build(folder, now=NOW))
    changed = mod_settings.build(folder, store=FakeStore(cost=2.0, context=50_000), now=NOW)
    assert mod_settings.write(folder, changed) is True
    written = json.loads(mod_settings.settings_path(folder).read_text(encoding="utf-8"))
    assert written["usual"]["end_context_tokens"] == 50_000


def test_a_damaged_file_is_replaced(tmp_path):
    folder = _config(tmp_path)
    path = mod_settings.settings_path(folder)
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    data = mod_settings.build(folder, now=NOW)
    assert mod_settings.write(folder, data) is True
    assert json.loads(path.read_text(encoding="utf-8")) == data


def test_a_failed_rename_keeps_the_old_file_whole_and_cleans_up(tmp_path, monkeypatch):
    folder = _config(tmp_path)
    mod_settings.write(folder, mod_settings.build(folder, now=NOW))
    path = mod_settings.settings_path(folder)
    before = path.read_bytes()

    def refuse(src, dst):
        raise OSError("in use")

    monkeypatch.setattr(mod_settings.os, "replace", refuse)
    changed = mod_settings.build(folder, store=FakeStore(cost=9.0, context=1), now=NOW)
    assert mod_settings.write(folder, changed) is False
    assert path.read_bytes() == before
    assert [p.name for p in path.parent.iterdir()] == ["settings.json"]


def test_a_folder_that_cannot_be_made_gives_false_not_an_error(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the config folder should be", encoding="utf-8")
    assert mod_settings.write(blocker, {"schema": 1, "written_at": "x"}) is False


# -- the service ----------------------------------------------------------------


def _join_mod_threads():
    """Wait for a report that is rebuilding behind a request, then for the
    settings write it starts."""
    for name in ("claudeglass-report", "claudeglass-mod-settings"):
        for thread in [t for t in threading.enumerate() if t.name == name]:
            thread.join(timeout=10)


def _make_the_kept_report_stale(served):
    """A newer config.toml: the service builds the report again on the
    next request."""
    path = served.options.config_dir / "config.toml"
    path.write_text('billing = "api"\n', encoding="utf-8")
    later = time.time_ns() + 10_000_000_000
    os.utime(path, ns=(later, later))


@pytest.fixture
def report_builds(served, monkeypatch):
    """How many times the service has built a report from the corpus."""
    import sys

    fake = sys.modules["claudeglass.service.rebuild"]
    real = fake.corpus_from_store
    builds = []

    def counting(*args, **kwargs):
        builds.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(fake, "corpus_from_store", counting)
    return builds


@pytest.fixture
def served(tmp_path, monkeypatch):
    handle = _start_server(tmp_path, monkeypatch)
    try:
        yield handle
    finally:
        handle.close()
        _join_mod_threads()
        handle.store.close()


def test_the_first_default_report_writes_the_file_with_your_usual_and_the_dashboards_address(served):
    path = mod_settings.settings_path(served.options.config_dir)
    assert not path.exists()
    resp, _ = served.request("GET", "/api/report.json")
    assert resp.status == 200
    _join_mod_threads()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data) == TOP_KEYS
    assert data["version"] == __version__
    assert data["calibration"]
    assert data["dashboard_url"] == mod_settings.dashboard_url(served.options.bind, served.options.port)


def test_a_report_for_another_window_does_not_write_the_file(served):
    path = mod_settings.settings_path(served.options.config_dir)
    for query in ("window_days=7", "since=2026-08-01T00:00:00%2B00:00"):
        resp, _ = served.request("GET", f"/api/report.json?{query}")
        assert resp.status == 200
    _join_mod_threads()
    assert not path.exists()


def test_the_file_is_written_at_most_once_an_hour(served, report_builds, monkeypatch):
    calls = []
    real = mod_settings.write

    def counting(config_dir, data):
        calls.append(data["written_at"])
        return real(config_dir, data)

    monkeypatch.setattr(mod_settings, "write", counting)
    served.request("GET", "/api/report.json")
    _join_mod_threads()
    assert len(calls) == 1 and len(report_builds) == 1
    # A newer config makes the next default report build again.
    _make_the_kept_report_stale(served)
    served.request("GET", "/api/report.json")
    _join_mod_threads()
    assert len(report_builds) == 2, "the report should have been built again"
    assert len(calls) == 1, "a second build within the hour must not write again"


def test_the_file_is_written_again_once_the_hour_has_passed(served, report_builds, monkeypatch):
    calls = []
    monkeypatch.setattr(mod_settings, "write", lambda config_dir, data: calls.append(1) or True)
    served.request("GET", "/api/report.json")
    _join_mod_threads()
    assert len(calls) == 1 and len(report_builds) == 1
    real_monotonic = time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: real_monotonic() + 3601)
    _make_the_kept_report_stale(served)
    served.request("GET", "/api/report.json")
    _join_mod_threads()
    assert len(report_builds) == 2
    assert len(calls) == 2


def test_a_failure_to_write_never_reaches_the_request(served, monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(mod_settings, "build", boom)
    resp, raw = served.request("GET", "/api/report.json")
    _join_mod_threads()
    assert resp.status == 200 and json.loads(raw)
    assert "the mod's settings were not written: disk on fire" in capsys.readouterr().err
    assert not mod_settings.settings_path(served.options.config_dir).exists()

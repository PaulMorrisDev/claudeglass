"""The mod's settings file: its shape, how it is written, and when the
dashboard's service writes it."""

from __future__ import annotations

import dataclasses
import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from helpers import assert_privacy_deep
from test_service_api import _start_server

from claudeglass import __version__, calibration, capture_catalogue, mod_settings, recache
from claudeglass.hooks import capture_hook
from claudeglass.config import ConfigError
from claudeglass.pricing import LongContextRule, ModelRates, Pricing, load_pricing
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
    "rebuild",
    "calibration",
    "coaching",
    "usual",
    "dashboard_url",
}


class FakeStore:
    def __init__(self, cost=None, context=None, error=None, writes=(0, 0)):
        self.cost, self.context, self.error, self.writes = cost, context, error, writes
        self.asked = None

    def cache_write_split(self, days, now):
        self.asked = (days, now)
        if self.error:
            raise self.error
        return self.writes

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
    assert data["cache"] == {"ttl_seconds": 300, "source": "default"}
    assert data["rebuild"] == {**dataclasses.asdict(recache.RecacheThresholds()), "session_read_margin": 3000}
    assert data["calibration"] == calibration.DEFAULT.to_dict()
    assert data["usual"] == {"session_cost_usd": None, "end_context_tokens": None}
    assert data["dashboard_url"] is None


def test_the_rate_card_gives_each_model_its_aliases_five_rates_and_window(tmp_path):
    pricing = mod_settings.build(_config(tmp_path), now=NOW)["pricing"]
    assert isinstance(pricing["version"], str) and pricing["models"]
    for model_id, entry in pricing["models"].items():
        # A model with a long-context rule (Haiku 5.5's) carries it as a sixth key.
        assert set(entry) - {"long_context"} == {
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


def test_a_long_context_rule_reaches_the_rate_card_and_a_model_without_one_has_none(tmp_path):
    packaged = load_pricing()
    models = mod_settings.pricing_block(packaged)["models"]
    # In the packaged card too: a rule is in the block exactly when the model has one.
    assert models and all(
        ("long_context" in entry) == (packaged.models[model_id].long_context is not None)
        for model_id, entry in models.items()
    )
    rule_models = {
        "by-multiplier": ModelRates("by-multiplier", 3.0, 15.0, 3.75, 6.0, 0.3, long_context=LongContextRule(200_000, multiplier=2.0)),
        "by-rates": ModelRates(
            "by-rates", 3.0, 15.0, 3.75, 6.0, 0.3, long_context=LongContextRule(100_000, overrides={"input": 6, "cache_read": 0.6})
        ),
        "bare": ModelRates("bare", 3.0, 15.0, 3.75, 6.0, 0.3, long_context=LongContextRule(50_000)),
        "none": ModelRates("none", 3.0, 15.0, 3.75, 6.0, 0.3),
    }
    pricing = Pricing("x", "v", "USD", None, None, None, "", models=rule_models)
    block = mod_settings.pricing_block(pricing)["models"]
    assert block["by-multiplier"]["long_context"] == {"threshold_tokens": 200_000, "multiplier": 2.0}
    assert block["by-rates"]["long_context"] == {"threshold_tokens": 100_000, "overrides": {"input": 6.0, "cache_read": 0.6}}
    assert block["bare"]["long_context"] == {"threshold_tokens": 50_000}
    assert "long_context" not in block["none"]
    json.dumps(block)


def _assert_every_alias_resolves_under_its_model(pricing):
    block = mod_settings.pricing_block(pricing)["models"]
    listed = [alias for entry in block.values() for alias in entry["aliases"]]
    assert len(listed) == len(set(listed)), "an alias is listed under two models"
    for model_id, entry in block.items():
        for alias in entry["aliases"]:
            resolved = pricing.resolve_model(alias)
            assert resolved is not None and resolved.canonical_id == model_id, (alias, model_id)
    return block


def test_every_alias_in_the_rate_card_resolves_in_python_to_the_model_it_sits_under():
    block = _assert_every_alias_resolves_under_its_model(load_pricing())
    assert any(entry["aliases"] for entry in block.values())


def test_an_alias_two_models_claim_is_listed_once_under_the_one_python_resolves_to(tmp_path):
    rates = 'input = 1\noutput = 2\ncache_write_5m = 1.25\ncache_write_1h = 2\ncache_read = 0.1\n'
    path = tmp_path / "pricing.toml"
    path.write_text(
        'version = "t"\n'
        f'[models."zeta"]\n{rates}aliases = ["shared", "z"]\n'
        f'[models."alpha"]\n{rates}aliases = ["shared", "a", "zeta"]\n',
        encoding="utf-8",
    )
    pricing = load_pricing(path=path)
    assert pricing.resolve_model("shared").canonical_id == "alpha"  # the last to name it
    block = _assert_every_alias_resolves_under_its_model(pricing)
    assert block["alpha"]["aliases"] == ["a", "shared"]
    assert block["zeta"]["aliases"] == ["z"]  # "zeta" is a model's own id: not an alias of alpha


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
    assert data["cache"] == {"ttl_seconds": 3600, "source": "config"}


def test_the_config_wins_over_what_your_history_says(tmp_path):
    store = FakeStore(writes=(0, 90_000))
    data = mod_settings.build(_config(tmp_path, 'default_ttl = "5m"\n'), store=store, now=NOW)
    assert data["cache"] == {"ttl_seconds": 300, "source": "config"}
    assert store.asked is None


def test_mostly_hour_long_writes_make_the_cache_last_an_hour(tmp_path):
    store = FakeStore(writes=(2_000, 90_000))
    data = mod_settings.build(_config(tmp_path), store=store, now=NOW)
    assert data["cache"] == {"ttl_seconds": 3600, "source": "history"}
    assert store.asked == (14, NOW)


@pytest.mark.parametrize("writes", [(90_000, 2_000), (5_000, 5_000)])
def test_otherwise_your_history_says_five_minutes(tmp_path, writes):
    data = mod_settings.build(_config(tmp_path), store=FakeStore(writes=writes), now=NOW)
    assert data["cache"] == {"ttl_seconds": 300, "source": "history"}


def test_no_writes_in_the_window_or_no_answer_leaves_the_default(tmp_path):
    for store in (None, FakeStore(writes=(0, 0)), FakeStore(error=sqlite3.OperationalError("locked"))):
        data = mod_settings.build(_config(tmp_path), store=store, now=NOW)
        assert data["cache"] == {"ttl_seconds": 300, "source": "default"}


def test_the_cache_lifetime_reads_the_stores_real_writes(tmp_path):
    from claudeglass.service.store import Store

    store = Store(tmp_path / "store.sqlite")
    store.open()
    try:
        row = {
            "day": "2026-09-18", "bucket": "2026-09-18T10:00:00Z", "model": "claude-sonnet-5", "turns": 1,
            "input_tokens": 1, "cache_creation_tokens": 800, "cache_read_tokens": 0, "output_tokens": 1,
            "thinking_tokens": 0, "cc_5m": 100, "cc_1h": 700, "cost": 1.0,
        }
        store.upsert_session(session_id="s", project_slug="p", slug="p", last_ts="2026-09-18T10:00:00Z")
        store.upsert_transcript(session_id="s", path="/x/s.jsonl", kind="top-level", digest_json="{}", turns_agg=[row])
        assert mod_settings.build(_config(tmp_path), store=store, now=NOW)["cache"] == {"ttl_seconds": 3600, "source": "history"}
        # Weeks later the same writes are out of the window.
        later = NOW + timedelta(days=30)
        assert mod_settings.build(_config(tmp_path), store=store, now=later)["cache"] == {"ttl_seconds": 300, "source": "default"}
    finally:
        store.close()


def test_the_rebuild_thresholds_follow_the_config_and_are_named_as_the_mod_names_them(tmp_path):
    data = mod_settings.build(_config(tmp_path, "[thresholds]\nctx_floor = 50000\ncr_ratio = 0.35\n"), now=NOW)
    assert data["rebuild"]["ctx_floor"] == 50_000 and data["rebuild"]["cr_ratio"] == 0.35
    js = (Path(__file__).resolve().parent.parent / "plugin" / "hooks" / "lib" / "rebuild.js").read_text(encoding="utf-8")
    block = re.search(r"REBUILD_DEFAULTS = \{(.*?)\};", js, flags=re.S).group(1)
    assert set(re.findall(r"^\s*(\w+):", block, flags=re.M)) == set(data["rebuild"])


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
        "enabled": False,
        "projects": [],
        "exclude_projects": [],
        "plan_fresh": True,
        "thresholds": capture_catalogue.COACHING_THRESHOLDS,
        "muted": [],
        "once": [],
    }
    (folder / "coaching.json").write_text(
        json.dumps({"thresholds": {"cold_min_tokens": 50_000}, "muted": ["b", "a", 3], "once": ["c"], "typical_piece_tokens": 2_500_000}),
        encoding="utf-8",
    )
    coaching = mod_settings.build(folder, now=NOW)["coaching"]
    assert coaching["thresholds"]["cold_min_tokens"] == 50_000
    assert coaching["thresholds"]["cold_rest_hours"] == capture_catalogue.COACHING_THRESHOLDS["cold_rest_hours"]
    assert coaching["muted"] == ["a", "b"]
    assert coaching["once"] == ["c"]
    # The rating reminder is the capture hook's own, so its piece size is not a setting of the mod.
    assert "typical_piece_tokens" not in coaching


def test_coaching_is_off_until_coaching_notes_is_in_capture_coaching(tmp_path):
    assert mod_settings.build(_config(tmp_path), now=NOW)["coaching"]["enabled"] is False
    assert mod_settings.build(_config(tmp_path, '[capture]\ncoaching = ["coaching_line"]\n'), now=NOW)["coaching"]["enabled"] is False
    on = _config(tmp_path, '[capture]\ncoaching = ["coaching_notes"]\n')
    assert mod_settings.build(on, now=NOW)["coaching"]["enabled"] is True


def test_the_project_filter_is_mirrored_as_written_and_the_feedback_opt_in_is_not(tmp_path):
    folder = _config(
        tmp_path,
        'exclude_projects = ["scratch"]\n[capture]\ncoaching = ["coaching_notes"]\n'
        'projects = ["work-", "!private"]\nfeedback = ["feedback_reminder", "plan_check"]\n',
    )
    block = mod_settings.build(folder, now=NOW)["coaching"]
    assert block["projects"] == ["work-", "!private"]
    assert block["exclude_projects"] == ["scratch"]
    # J33: nothing in the mod reads whether the reminder is opted in.
    assert "feedback_reminder" not in block
    # The hook's own answer for the same config agrees with what was mirrored.
    config = capture_hook.load_config(folder)
    assert capture_hook.feedback_ids(config) == ("plan_check", "feedback_reminder")
    assert capture_hook.project_allowed("work-api", block["projects"], block["exclude_projects"]) is True
    assert capture_hook.project_allowed("scratch-work-", block["projects"], block["exclude_projects"]) is False


def test_the_plan_tip_opt_out_reaches_the_file(tmp_path):
    folder = _config(tmp_path)
    (folder / "coaching.json").write_text(json.dumps({"plan_fresh": False}), encoding="utf-8")
    assert mod_settings.build(folder, now=NOW)["coaching"]["plan_fresh"] is False


def test_the_rating_reminders_state_and_the_lately_rated_sessions_stay_out_of_the_file(tmp_path):
    """J33: the reminder runs in the capture hook, not the mod, so when it last
    ran, the typical piece of work and the ids of rated sessions were
    written every hour for nothing to read."""
    folder = _config(tmp_path)
    (folder / "coach-state.json").write_text(json.dumps({"feedback": {"reminded_at": 1_780_000_000.5}}), encoding="utf-8")
    store = sqlite3.connect(folder / "service.db")
    store.execute("CREATE TABLE session_feedback (session_id TEXT, set_at TEXT)")
    recent, old = NOW - timedelta(days=2), NOW - timedelta(days=40)
    store.executemany(
        "INSERT INTO session_feedback VALUES (?, ?)",
        [("s-recent", recent.strftime("%Y-%m-%dT%H:%M:%SZ")), ("s-old", old.strftime("%Y-%m-%dT%H:%M:%SZ"))],
    )
    store.commit()
    store.close()
    data = mod_settings.build(folder, now=NOW)
    block = data["coaching"]
    for key in ("reminded_at", "rated", "typical_piece_tokens", "feedback_reminder"):
        assert key not in block, key
    assert set(block) == {"enabled", "projects", "exclude_projects", "plan_fresh", "thresholds", "muted", "once"}
    assert "s-recent" not in json.dumps(data) and not hasattr(mod_settings, "RATED_WINDOW_S")


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
    # ``coaching.projects`` and ``exclude_projects`` mirror the user's own slug patterns as written (a local
    # config mirror, not an export), so a pattern of theirs isn't scanned; the default config has none.
    assert data["coaching"]["projects"] == [] and data["coaching"]["exclude_projects"] == []
    assert_privacy_deep(json.loads(json.dumps(data)))
    assert str(tmp_path) not in json.dumps(data)


def _service_written(folder):
    """What the dashboard's service would have written: a report's share,
    basis, measured calibration and the address it really answers on."""
    units = {"share_per_usd": 0.0125, "basis": "Amounts are shares of your weekly usage limit."}
    measured = {"tool": {"opus": 3.2}, "text": {"opus": 3.9}, "default_family": "opus"}
    return mod_settings.build(folder, report=_report(units, measured), dashboard_url="http://127.0.0.1:8787/", now=NOW)


def test_without_a_report_the_previous_files_figures_carry_forward_in_the_same_billing_mode(tmp_path):
    folder = _config(tmp_path, 'billing = "subscription"\n')
    previous = _service_written(folder)
    data = mod_settings.build(folder, dashboard_url="http://127.0.0.1:8765/", previous=previous, now=NOW)
    assert data["billing"] == previous["billing"]
    assert data["billing"]["share_per_usd"] == 0.0125
    assert data["calibration"] == previous["calibration"]


def test_the_address_update_finish_passes_wins_over_the_one_the_previous_file_holds(tmp_path):
    """J32: the file's own address won, so a dashboard that had moved to
    another port kept being offered at the old one until the service wrote
    the file again."""
    folder = _config(tmp_path, 'billing = "subscription"\n')
    previous = _service_written(folder)
    assert previous["dashboard_url"] == "http://127.0.0.1:8787/"
    data = mod_settings.build(folder, dashboard_url="http://127.0.0.1:8765/", previous=previous, now=NOW)
    assert data["dashboard_url"] == "http://127.0.0.1:8765/"
    # With no address given, the one the service wrote is kept, as before.
    assert mod_settings.build(folder, previous=previous, now=NOW)["dashboard_url"] == "http://127.0.0.1:8787/"
    assert mod_settings.build(folder, dashboard_url="", previous=previous, now=NOW)["dashboard_url"] == (
        "http://127.0.0.1:8787/"
    )
    assert mod_settings.build(folder, now=NOW)["dashboard_url"] is None


def test_a_change_of_billing_mode_carries_no_share_or_basis_forward(tmp_path):
    previous = _service_written(_config(tmp_path, 'billing = "subscription"\n'))
    data = mod_settings.build(_config(tmp_path, 'billing = "api"\n'), previous=previous, now=NOW)
    assert data["billing"]["mode"] == "api"
    assert data["billing"]["share_per_usd"] is None
    assert data["billing"]["basis"] != previous["billing"]["basis"]
    # What does not depend on the mode still carries.
    assert data["calibration"] == previous["calibration"]


def test_a_report_wins_over_the_previous_file(tmp_path):
    folder = _config(tmp_path, 'billing = "subscription"\n')
    report = _report({"share_per_usd": 0.5, "basis": "new"}, {"default_family": "sonnet"})
    data = mod_settings.build(folder, report=report, previous=_service_written(folder), now=NOW)
    assert data["billing"]["share_per_usd"] == 0.5 and data["billing"]["basis"] == "new"
    assert data["calibration"] == {"default_family": "sonnet"}


@pytest.mark.parametrize("previous", [None, [], "text", {}, {"billing": 3, "calibration": [], "dashboard_url": 7}, {"billing": {"mode": "api", "share_per_usd": True}}])
def test_a_previous_file_that_is_not_a_settings_file_changes_nothing(tmp_path, previous):
    folder = _config(tmp_path, 'billing = "api"\n')
    assert mod_settings.build(folder, previous=previous, now=NOW) == mod_settings.build(folder, now=NOW)


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


def test_the_same_content_is_written_again_once_its_stamp_is_over_a_day_old(tmp_path):
    folder = _config(tmp_path)
    assert mod_settings.write(folder, mod_settings.build(folder, now=NOW)) is True
    path = mod_settings.settings_path(folder)
    day = timedelta(hours=24)
    assert mod_settings.write(folder, mod_settings.build(folder, now=NOW + day)) is False
    again = mod_settings.build(folder, now=NOW + day + timedelta(seconds=1))
    assert mod_settings.write(folder, again) is True
    assert json.loads(path.read_text(encoding="utf-8"))["written_at"] == again["written_at"]
    # The new stamp is the one later builds are measured from.
    assert mod_settings.write(folder, mod_settings.build(folder, now=NOW + day + timedelta(hours=2))) is False


@pytest.mark.parametrize("stamp", [None, "", "yesterday", 12])
def test_a_stamp_that_cannot_be_read_is_rewritten(tmp_path, stamp):
    folder = _config(tmp_path)
    data = mod_settings.build(folder, now=NOW)
    mod_settings.write(folder, {**data, "written_at": stamp})
    assert mod_settings.write(folder, data) is True
    assert json.loads(mod_settings.settings_path(folder).read_text(encoding="utf-8"))["written_at"] == data["written_at"]


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


# -- refresh_coaching: a capture change reaches the mod at once ---------------


def test_refresh_coaching_moves_only_the_coaching_block(tmp_path):
    path = mod_settings.settings_path(tmp_path)
    data = mod_settings.build(tmp_path, now=NOW)
    data["usual"] = {"session_cost_usd": 1.5, "end_context_tokens": 90000}
    assert mod_settings.write(tmp_path, data)
    (tmp_path / "config.toml").write_text('[capture]\ncoaching = ["coaching_notes"]\nprojects = ["work-"]\n', encoding="utf-8")
    assert mod_settings.refresh_coaching(tmp_path, now=NOW + timedelta(minutes=5))
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["coaching"]["enabled"] is True and written["coaching"]["projects"] == ["work-"]
    assert written["usual"] == {"session_cost_usd": 1.5, "end_context_tokens": 90000}
    assert written["written_at"] == "2026-09-18T12:35:05Z"
    # Nothing changed since: the file is left alone.
    assert mod_settings.refresh_coaching(tmp_path, now=NOW + timedelta(minutes=6)) is False


@pytest.mark.parametrize("content", [None, "", "not json", "[]", '{"schema": 99}'])
def test_refresh_coaching_never_raises_and_writes_nothing_without_a_readable_file(tmp_path, content):
    path = mod_settings.settings_path(tmp_path)
    if content is not None:
        path.parent.mkdir(parents=True)
        path.write_text(content, encoding="utf-8")
    assert mod_settings.refresh_coaching(tmp_path) is False
    assert (path.read_text(encoding="utf-8") if content is not None else None) == content


# -- two writers: the hourly report and a capture setting's refresh ---------------------------------


def test_a_capture_setting_changed_while_the_report_builds_survives_the_hourly_write(tmp_path):
    folder = _config(tmp_path)
    assert mod_settings.write(folder, mod_settings.build(folder, now=NOW))
    path = mod_settings.settings_path(folder)

    class SlowStore(FakeStore):
        """Takes its time over "your usual": coaching is switched on and reaches the file meanwhile."""

        def median_session_cost(self):
            (folder / "config.toml").write_text('[capture]\ncoaching = ["coaching_notes"]\nprojects = ["work-"]\n', encoding="utf-8")
            assert mod_settings.refresh_coaching(folder, now=NOW + timedelta(minutes=2))
            return super().median_session_cost()

    data = mod_settings.build(folder, store=SlowStore(cost=2.0, context=90000), now=NOW)
    assert data["coaching"]["enabled"] is False  # the block the build worked out before the change
    assert json.loads(path.read_text(encoding="utf-8"))["coaching"]["enabled"] is True

    assert mod_settings.write(folder, data)
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["coaching"]["enabled"] is True and written["coaching"]["projects"] == ["work-"]  # not written over with the older block
    assert written["usual"]["session_cost_usd"] == 2.0  # the rest of the report's file is what it built


def test_the_writers_take_turns_and_each_has_a_temporary_file_of_its_own(tmp_path, monkeypatch):
    folder = _config(tmp_path)
    inside = {"now": 0, "most": 0}
    names: list[tuple[int, str]] = []
    real_replace = os.replace
    guard = threading.Lock()

    def replace(source, target):
        with guard:
            inside["now"] += 1
            inside["most"] = max(inside["most"], inside["now"])
            names.append((threading.get_ident(), Path(source).name))
        time.sleep(0.01)  # long enough for a second writer to arrive, were nothing holding it back
        try:
            return real_replace(source, target)
        finally:
            with guard:
                inside["now"] -= 1

    monkeypatch.setattr(mod_settings.os, "replace", replace)
    failures: list[BaseException] = []

    def hourly(index):
        try:
            for turn in range(6):
                data = mod_settings.build(folder, store=FakeStore(cost=float(index * 10 + turn), context=1), now=NOW + timedelta(minutes=turn))
                mod_settings.write(folder, data)
        except BaseException as exc:  # noqa: BLE001
            failures.append(exc)

    def rating():
        try:
            for turn in range(6):
                mod_settings.refresh_coaching(folder, now=NOW + timedelta(minutes=turn, seconds=30))
        except BaseException as exc:  # noqa: BLE001
            failures.append(exc)

    threads = [threading.Thread(target=hourly, args=(1,)), threading.Thread(target=hourly, args=(2,)), threading.Thread(target=rating)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert failures == []
    assert inside["most"] == 1
    assert len({thread for thread, _ in names}) >= 2 and len({name for _, name in names}) >= 2
    assert json.loads(mod_settings.settings_path(folder).read_text(encoding="utf-8"))["schema"] == 1
    assert list(mod_settings.settings_path(folder).parent.glob("*.tmp")) == []


def test_write_never_raises_when_the_coaching_block_cannot_be_worked_out(tmp_path, monkeypatch):
    folder = _config(tmp_path)
    data = mod_settings.build(folder, now=NOW)

    def broken(config_dir):
        raise RuntimeError("config on fire")

    monkeypatch.setattr(mod_settings, "_coaching_block", broken)
    assert mod_settings.write(folder, data)
    assert json.loads(mod_settings.settings_path(folder).read_text(encoding="utf-8"))["coaching"] == data["coaching"]

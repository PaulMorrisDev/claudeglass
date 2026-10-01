"""Tests for WP5: ``config.toml``/``sessions.toml`` loading and
per-session override round-tripping (``src/claudeglass/config.py``).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from claudeglass.config import (
    Config,
    ConfigError,
    append_prediction_log,
    load_config,
    load_prediction_log,
    load_session_overrides,
    save_session_override,
    saved_pricing_path,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "config"


# --------------------------------------------------------------------
# load_config: defaults
# --------------------------------------------------------------------


def test_missing_config_file_returns_defaults(tmp_path):
    config = load_config(config_dir=tmp_path / "does-not-exist")
    # A missing file means billing = "auto", resolved to "api" here (no
    # usage log); everything else is the dataclass default.
    assert config == Config(billing_source=config.billing_source)
    assert config.billing == "api"
    assert config.billing_source.startswith("automatic")
    assert config.tz is None
    assert config.thresholds == {}
    assert config.recache == {}
    assert config.min_sessions == 5
    assert config.min_turns == 200
    assert config.allow_titles is False
    assert config.pricing_path is None


def test_default_config_dir_honours_claude_config_dir_env_var(monkeypatch, tmp_path):
    # The autouse fixture in conftest.py already points CLAUDE_CONFIG_DIR
    # at a throwaway home; point it somewhere else explicitly here to
    # prove load_config (with no config_dir argument) actually reads it.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    claudeglass_dir = tmp_path / "claudeglass"
    claudeglass_dir.mkdir()
    (claudeglass_dir / "config.toml").write_text('billing = "subscription"\n', encoding="utf-8")

    config = load_config()
    assert config.billing == "subscription"


# --------------------------------------------------------------------
# load_config: full parse
# --------------------------------------------------------------------


def test_valid_config_parses_every_field(tmp_path):
    claudeglass_dir = tmp_path / "claudeglass"
    claudeglass_dir.mkdir()
    (claudeglass_dir / "config.toml").write_text(
        (FIXTURES / "config_valid.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )

    config = load_config(config_dir=claudeglass_dir)
    assert config.billing == "subscription"
    assert config.tz == "Europe/London"
    assert config.min_sessions == 8
    assert config.min_turns == 150
    assert config.allow_titles is True
    assert config.pricing_path == "/custom/pricing.toml"
    assert config.thresholds == {"ctx_floor": 20000, "cr_ratio": 0.2}
    assert config.recache == {"full_expiry_cr": 2000, "huge_ctx": 200000}
    assert config.exclude_projects == ["^scratch-", "throwaway$"]
    assert config.retention_days == 90
    assert config.provider == "bedrock"


def test_partial_config_falls_back_to_defaults_for_missing_fields(tmp_path):
    claudeglass_dir = tmp_path / "claudeglass"
    claudeglass_dir.mkdir()
    (claudeglass_dir / "config.toml").write_text('min_sessions = 12\n', encoding="utf-8")

    config = load_config(config_dir=claudeglass_dir)
    assert config.min_sessions == 12
    # Every other field keeps its documented default.
    assert config.billing == "api"
    assert config.tz is None
    assert config.min_turns == 200
    assert config.allow_titles is False
    assert config.exclude_projects == []
    assert config.retention_days is None
    assert config.provider is None


# --------------------------------------------------------------------
# load_config: exclude_projects / retention_days / provider (fix 6)
# --------------------------------------------------------------------


def test_exclude_projects_defaults_to_empty_list():
    assert Config().exclude_projects == []


def test_exclude_projects_parses_list_of_regex_strings(tmp_path):
    claudeglass_dir = tmp_path / "claudeglass"
    claudeglass_dir.mkdir()
    (claudeglass_dir / "config.toml").write_text(
        'exclude_projects = ["^scratch-", "throwaway$"]\n', encoding="utf-8"
    )
    config = load_config(config_dir=claudeglass_dir)
    assert config.exclude_projects == ["^scratch-", "throwaway$"]


def test_exclude_projects_wrong_type_raises_config_error(tmp_path):
    claudeglass_dir = tmp_path / "claudeglass"
    claudeglass_dir.mkdir()
    (claudeglass_dir / "config.toml").write_text('exclude_projects = "not-a-list"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="exclude_projects"):
        load_config(config_dir=claudeglass_dir)


def test_exclude_projects_non_string_item_raises_config_error(tmp_path):
    claudeglass_dir = tmp_path / "claudeglass"
    claudeglass_dir.mkdir()
    (claudeglass_dir / "config.toml").write_text('exclude_projects = ["ok", 5]\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="exclude_projects"):
        load_config(config_dir=claudeglass_dir)


def test_exclude_projects_bad_regex_raises_config_error_at_load(tmp_path):
    """SEC-P5: compiled at load, same as ``capture.projects`` -- a typo'd
    regex is a ConfigError the user sees right away, not something that
    silently degrades later, once per call, deep inside discovery/corpus.
    """
    claudeglass_dir = tmp_path / "claudeglass"
    claudeglass_dir.mkdir()
    (claudeglass_dir / "config.toml").write_text('exclude_projects = ["ok", "(unbalanced"]\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="exclude_projects"):
        load_config(config_dir=claudeglass_dir)


# --------------------------------------------------------------------
# load_config: [savers] table (v4-saver-roi)
# --------------------------------------------------------------------


def test_savers_defaults_to_empty_list():
    assert Config().savers == []


def test_savers_parses_names_list(tmp_path):
    claudeglass_dir = tmp_path / "claudeglass"
    claudeglass_dir.mkdir()
    (claudeglass_dir / "config.toml").write_text(
        '[savers]\nnames = ["my-mcp-server", "acme-optimizer"]\n', encoding="utf-8"
    )
    config = load_config(config_dir=claudeglass_dir)
    assert config.savers == ["my-mcp-server", "acme-optimizer"]


def test_savers_missing_table_defaults_to_empty_list(tmp_path):
    claudeglass_dir = tmp_path / "claudeglass"
    claudeglass_dir.mkdir()
    (claudeglass_dir / "config.toml").write_text('min_sessions = 12\n', encoding="utf-8")
    config = load_config(config_dir=claudeglass_dir)
    assert config.savers == []


def test_savers_wrong_type_raises_config_error(tmp_path):
    claudeglass_dir = tmp_path / "claudeglass"
    claudeglass_dir.mkdir()
    (claudeglass_dir / "config.toml").write_text('savers = "not-a-table"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="savers"):
        load_config(config_dir=claudeglass_dir)


def test_savers_names_non_string_item_raises_config_error(tmp_path):
    claudeglass_dir = tmp_path / "claudeglass"
    claudeglass_dir.mkdir()
    (claudeglass_dir / "config.toml").write_text('[savers]\nnames = ["ok", 5]\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="savers.names"):
        load_config(config_dir=claudeglass_dir)


def test_describe_shows_savers_when_set():
    config = Config(savers=["my-mcp-server"])
    assert any("savers" in line and "my-mcp-server" in line for line in config.describe())


def test_retention_days_defaults_to_none():
    assert Config().retention_days is None


def test_retention_days_parses_integer(tmp_path):
    claudeglass_dir = tmp_path / "claudeglass"
    claudeglass_dir.mkdir()
    (claudeglass_dir / "config.toml").write_text("retention_days = 30\n", encoding="utf-8")
    config = load_config(config_dir=claudeglass_dir)
    assert config.retention_days == 30


def test_retention_days_wrong_type_raises_config_error(tmp_path):
    claudeglass_dir = tmp_path / "claudeglass"
    claudeglass_dir.mkdir()
    (claudeglass_dir / "config.toml").write_text('retention_days = "thirty"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="retention_days"):
        load_config(config_dir=claudeglass_dir)


def test_retention_days_bool_rejected_as_not_an_integer(tmp_path):
    # bool is a subclass of int in Python; the validator must special-case
    # it out so `retention_days = true` doesn't silently become 1.
    claudeglass_dir = tmp_path / "claudeglass"
    claudeglass_dir.mkdir()
    (claudeglass_dir / "config.toml").write_text("retention_days = true\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="retention_days"):
        load_config(config_dir=claudeglass_dir)


@pytest.mark.parametrize("bad", [0, -1, 36501, -36500])
def test_retention_days_out_of_bounds_raises_config_error(tmp_path, bad):
    """SEC-P5: 1-36500. 0 or negative would prune everything (including
    the session in progress) on the very next poll tick; an absurdly
    large value is almost certainly a typo (days entered as hours, an
    extra digit) rather than a real "keep forever" choice."""
    claudeglass_dir = tmp_path / "claudeglass"
    claudeglass_dir.mkdir()
    (claudeglass_dir / "config.toml").write_text(f"retention_days = {bad}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="retention_days"):
        load_config(config_dir=claudeglass_dir)


@pytest.mark.parametrize("edge", [1, 36500])
def test_retention_days_at_the_boundary_is_accepted(tmp_path, edge):
    claudeglass_dir = tmp_path / "claudeglass"
    claudeglass_dir.mkdir()
    (claudeglass_dir / "config.toml").write_text(f"retention_days = {edge}\n", encoding="utf-8")
    config = load_config(config_dir=claudeglass_dir)
    assert config.retention_days == edge


def test_provider_defaults_to_none():
    assert Config().provider is None


def test_provider_accepts_each_allowed_value(tmp_path):
    for value in ("anthropic", "bedrock", "vertex"):
        claudeglass_dir = tmp_path / f"claudeglass-{value}"
        claudeglass_dir.mkdir()
        (claudeglass_dir / "config.toml").write_text(f'provider = "{value}"\n', encoding="utf-8")
        config = load_config(config_dir=claudeglass_dir)
        assert config.provider == value


def test_provider_invalid_value_raises_config_error(tmp_path):
    claudeglass_dir = tmp_path / "claudeglass"
    claudeglass_dir.mkdir()
    (claudeglass_dir / "config.toml").write_text('provider = "openai"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="provider"):
        load_config(config_dir=claudeglass_dir)


def test_describe_shows_exclude_projects_retention_and_provider_when_set():
    config = Config(exclude_projects=["^scratch-"], retention_days=45, provider="vertex")
    lines = config.describe()
    assert any("exclude_projects" in line and "scratch-" in line for line in lines)
    assert any("retention_days: 45" in line for line in lines)
    assert any("provider: vertex" in line for line in lines)


def test_describe_omits_exclude_projects_retention_and_provider_when_unset():
    lines = Config().describe()
    assert not any("exclude_projects" in line for line in lines)
    assert not any("retention_days" in line for line in lines)
    assert not any(line.strip().startswith("provider:") for line in lines)


# --------------------------------------------------------------------
# load_config: malformed -> ConfigError
# --------------------------------------------------------------------


def test_malformed_toml_syntax_raises_config_error(tmp_path):
    claudeglass_dir = tmp_path / "claudeglass"
    claudeglass_dir.mkdir()
    (claudeglass_dir / "config.toml").write_text(
        (FIXTURES / "config_bad.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    with pytest.raises(ConfigError):
        load_config(config_dir=claudeglass_dir)


def test_invalid_billing_value_raises_config_error(tmp_path):
    claudeglass_dir = tmp_path / "claudeglass"
    claudeglass_dir.mkdir()
    (claudeglass_dir / "config.toml").write_text('billing = "invoice"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="billing"):
        load_config(config_dir=claudeglass_dir)


def test_wrong_type_field_raises_config_error(tmp_path):
    claudeglass_dir = tmp_path / "claudeglass"
    claudeglass_dir.mkdir()
    (claudeglass_dir / "config.toml").write_text('min_sessions = "five"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="min_sessions"):
        load_config(config_dir=claudeglass_dir)


def test_thresholds_must_be_a_table(tmp_path):
    claudeglass_dir = tmp_path / "claudeglass"
    claudeglass_dir.mkdir()
    (claudeglass_dir / "config.toml").write_text('thresholds = "nope"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="thresholds"):
        load_config(config_dir=claudeglass_dir)


def test_thresholds_classify_subdict_round_trips_as_a_nested_table(tmp_path):
    """Fix 5: classify.py reads its own overrides out of
    Config.thresholds["classify"]["mode"/"purpose"] (see
    classify.mode_and_purpose_thresholds_from_config) -- thresholds
    itself stays this module's plain free-form dict, so a nested TOML
    table under it just round-trips as a nested dict with no special
    parsing needed here.
    """
    claudeglass_dir = tmp_path / "claudeglass"
    claudeglass_dir.mkdir()
    (claudeglass_dir / "config.toml").write_text(
        "[thresholds.classify.mode]\n"
        "overnight_night_turn_share = 0.15\n"
        "\n"
        "[thresholds.classify.purpose]\n"
        "local_llm_min_hits = 1\n",
        encoding="utf-8",
    )

    config = load_config(config_dir=claudeglass_dir)
    assert config.thresholds == {
        "classify": {
            "mode": {"overnight_night_turn_share": 0.15},
            "purpose": {"local_llm_min_hits": 1},
        }
    }

    from claudeglass.classify import mode_and_purpose_thresholds_from_config

    mode_t, purpose_t = mode_and_purpose_thresholds_from_config(config.thresholds)
    assert mode_t == {"overnight_night_turn_share": 0.15}
    assert purpose_t == {"local_llm_min_hits": 1}

    assert any("classify" in line for line in config.describe())


# --------------------------------------------------------------------
# Config.describe()
# --------------------------------------------------------------------


def test_describe_lists_every_field_as_readable_lines():
    config = Config(billing="subscription", tz="Europe/London", min_sessions=3)
    lines = config.describe()
    assert any("billing: subscription" in line for line in lines)
    assert any("Europe/London" in line for line in lines)
    assert any("min_sessions: 3" in line for line in lines)


def test_describe_shows_local_when_tz_unset():
    lines = Config().describe()
    assert any("local (machine)" in line for line in lines)


# --------------------------------------------------------------------
# load_session_overrides
# --------------------------------------------------------------------


def test_missing_sessions_file_returns_empty_dict(tmp_path):
    assert load_session_overrides(config_dir=tmp_path / "does-not-exist") == {}


def test_load_session_overrides_parses_mode_purpose_and_tags(tmp_path):
    claudeglass_dir = tmp_path / "claudeglass"
    claudeglass_dir.mkdir()
    (claudeglass_dir / "sessions.toml").write_text(
        (FIXTURES / "sessions_seed.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )

    overrides = load_session_overrides(config_dir=claudeglass_dir)
    assert overrides["session-existing-1"] == {"mode": "interactive", "purpose": "general-dev"}
    assert overrides["session-existing-2"] == {
        "mode": "overnight",
        "purpose": "refactor",
        "tags": ["billing", "urgent"],
    }


def test_malformed_sessions_toml_raises_config_error(tmp_path):
    claudeglass_dir = tmp_path / "claudeglass"
    claudeglass_dir.mkdir()
    (claudeglass_dir / "sessions.toml").write_text('[sessions."x"\nmode = "interactive"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_session_overrides(config_dir=claudeglass_dir)


# --------------------------------------------------------------------
# save_session_override
# --------------------------------------------------------------------


def test_save_session_override_creates_file_when_absent(tmp_path):
    claudeglass_dir = tmp_path / "claudeglass"
    save_session_override(claudeglass_dir, "session-new", mode="interactive", purpose="review")

    overrides = load_session_overrides(config_dir=claudeglass_dir)
    assert overrides == {"session-new": {"mode": "interactive", "purpose": "review"}}


def test_save_session_override_preserves_other_entries(tmp_path):
    claudeglass_dir = tmp_path / "claudeglass"
    claudeglass_dir.mkdir()
    (claudeglass_dir / "sessions.toml").write_text(
        (FIXTURES / "sessions_seed.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )

    save_session_override(claudeglass_dir, "session-existing-1", mode="overnight")

    overrides = load_session_overrides(config_dir=claudeglass_dir)
    # The touched entry only has "mode" overwritten; "purpose" stays.
    assert overrides["session-existing-1"] == {"mode": "overnight", "purpose": "general-dev"}
    # The untouched entry is byte-for-byte preserved, including "tags".
    assert overrides["session-existing-2"] == {
        "mode": "overnight",
        "purpose": "refactor",
        "tags": ["billing", "urgent"],
    }


def test_save_session_override_updates_only_the_given_fields(tmp_path):
    claudeglass_dir = tmp_path / "claudeglass"
    save_session_override(claudeglass_dir, "session-x", mode="interactive", purpose="planning")
    save_session_override(claudeglass_dir, "session-x", purpose="test-triage")

    overrides = load_session_overrides(config_dir=claudeglass_dir)
    # mode was left as None on the second call, so it must be untouched.
    assert overrides["session-x"] == {"mode": "interactive", "purpose": "test-triage"}


def test_save_session_override_round_trips_special_characters(tmp_path):
    claudeglass_dir = tmp_path / "claudeglass"
    session_id = 'weird "id" with\\backslash'
    save_session_override(claudeglass_dir, session_id, mode="mixed")

    overrides = load_session_overrides(config_dir=claudeglass_dir)
    assert overrides[session_id] == {"mode": "mixed"}


def _write_usage_log(config_dir, rows):
    config_dir.mkdir(parents=True, exist_ok=True)
    lines = ["logged_at,session_id,window,used_percentage,resets_at,source"]
    lines.extend(rows)
    (config_dir / "usage-log.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_auto_billing_is_subscription_once_usage_limit_readings_exist(tmp_path):
    _write_usage_log(tmp_path, ["2026-09-01T00:00:00Z,s1,five_hour,12.5,2026-09-01T05:00:00Z,statusline"])
    config = load_config(config_dir=tmp_path)
    assert config.billing == "subscription"
    assert "usage-limit readings were found" in config.billing_source


def test_auto_billing_ignores_context_window_rows(tmp_path):
    _write_usage_log(tmp_path, ["2026-09-01T00:00:00Z,s1,context_window,,,statusline"])
    config = load_config(config_dir=tmp_path)
    assert config.billing == "api"


def test_explicit_billing_wins_over_usage_log(tmp_path):
    _write_usage_log(tmp_path, ["2026-09-01T00:00:00Z,s1,seven_day,40,,statusline"])
    (tmp_path / "config.toml").write_text('billing = "api"\n', encoding="utf-8")
    config = load_config(config_dir=tmp_path)
    assert config.billing == "api"
    assert config.billing_source == "set in config.toml"
    (tmp_path / "config.toml").write_text('billing = "auto"\n', encoding="utf-8")
    assert load_config(config_dir=tmp_path).billing == "subscription"


# --------------------------------------------------------------------
# saved_pricing_path: the one key the service's scanner reads each tick
# --------------------------------------------------------------------


def test_saved_pricing_path_reads_the_key_or_none(tmp_path):
    assert saved_pricing_path(tmp_path) is None
    (tmp_path / "config.toml").write_text('billing = "api"\n', encoding="utf-8")
    assert saved_pricing_path(tmp_path) is None
    (tmp_path / "config.toml").write_text('pricing_path = "/custom/pricing.toml"\n', encoding="utf-8")
    assert saved_pricing_path(tmp_path) == "/custom/pricing.toml"
    (tmp_path / "config.toml").write_text("pricing_path = 3\n", encoding="utf-8")
    assert saved_pricing_path(tmp_path) is None


def test_saved_pricing_path_matches_load_config(tmp_path):
    (tmp_path / "config.toml").write_text(
        (FIXTURES / "config_valid.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    assert saved_pricing_path(tmp_path) == load_config(config_dir=tmp_path).pricing_path == "/custom/pricing.toml"


def test_saved_pricing_path_on_malformed_toml_raises_config_error(tmp_path):
    (tmp_path / "config.toml").write_text("not = = toml\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        saved_pricing_path(tmp_path)


# --------------------------------------------------------------------
# EST-P5: prediction-log.jsonl
# --------------------------------------------------------------------


def test_append_prediction_log_writes_a_record_and_returns_its_id(tmp_path):
    prediction_id = append_prediction_log(
        tmp_path,
        source="whatif",
        measure_key="model",
        agent=None,
        predicted_usd=1.23,
        predicted_pct=None,
        fidelity="ceiling",
        now=datetime(2026, 9, 20, 9, tzinfo=timezone.utc),
    )
    assert len(prediction_id) == 16
    [record] = load_prediction_log(tmp_path)
    assert record == {
        "id": prediction_id,
        "ts": "2026-09-20T09:00:00+00:00",
        "source": "whatif",
        "measure_key": "model",
        "agent": None,
        "predicted_usd": 1.23,
        "predicted_pct": None,
        "fidelity": "ceiling",
    }


def test_append_prediction_log_generates_a_fresh_id_each_call(tmp_path):
    first = append_prediction_log(
        tmp_path, source="whatif", measure_key="model", agent=None,
        predicted_usd=1.0, predicted_pct=None, fidelity="estimated",
    )
    second = append_prediction_log(
        tmp_path, source="whatif", measure_key="model", agent=None,
        predicted_usd=2.0, predicted_pct=None, fidelity="estimated",
    )
    assert first != second
    assert [record["id"] for record in load_prediction_log(tmp_path)] == [first, second]


def test_append_prediction_log_records_an_agent_scoped_prediction(tmp_path):
    append_prediction_log(
        tmp_path, source="whatif", measure_key="rebuild_share", agent="reviewer",
        predicted_usd=None, predicted_pct=-15.0, fidelity="simulated",
    )
    [record] = load_prediction_log(tmp_path)
    assert record["agent"] == "reviewer"
    assert record["predicted_pct"] == -15.0
    assert record["predicted_usd"] is None


def test_load_prediction_log_on_a_missing_file_is_empty(tmp_path):
    assert load_prediction_log(tmp_path / "does-not-exist") == []


def test_load_prediction_log_skips_unparseable_or_incomplete_lines(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "prediction-log.jsonl").write_text(
        "not json\n"
        '{"id": "abc", "source": "whatif"}\n'  # missing ts
        '{"ts": "2026-09-20T09:00:00+00:00", "source": "whatif"}\n'  # missing id
        '{"id": "def", "ts": "2026-09-20T09:00:00+00:00", "source": "whatif", '
        '"measure_key": "model", "agent": null, "predicted_usd": 1.0, '
        '"predicted_pct": null, "fidelity": "ceiling"}\n',
        encoding="utf-8",
    )
    [record] = load_prediction_log(tmp_path)
    assert record["id"] == "def"

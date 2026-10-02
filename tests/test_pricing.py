"""Tests for WP2: rate-card loading, model-id resolution and per-turn
pricing (``src/claudeglass/pricing.py``).

Turns are built directly as ``model.Turn`` instances (there is no parser
yet — that is WP1) via the small ``_turn`` helper below rather than
``tests/helpers.py``'s JSONL builders, which build raw transcript lines.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claudeglass import model
from claudeglass.pricing import (
    FastRule,
    ModelRates,
    Pricing,
    PricingCoverage,
    PricingError,
    ResolvedRates,
    cache_read_savings_usd,
    effective_rates,
    load_pricing,
    newer_version_id,
    newer_version_of,
    price_turn,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"

#: Every model id the WP2 brief requires in the packaged pricing.toml.
REQUIRED_PACKAGED_MODEL_IDS = {
    "claude-fable-5-1",
    "claude-fable-5",
    "claude-opus-5-5",
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-opus-4-5",
    "claude-opus-4-1",
    "claude-opus-4",
    "claude-sonnet-5-5",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
    "claude-sonnet-4-5",
    "claude-sonnet-4",
    "claude-haiku-4-5-20251001",
    "claude-3-5-haiku-20241022",
    "claude-3-5-sonnet-20241022",
    "claude-3-7-sonnet-20250219",
    "claude-3-haiku-20240307",
    "claude-3-opus-20240229",
}


def _turn(**overrides) -> model.Turn:
    """Build a ``Turn`` with sane zero defaults, overridable per field."""
    fields = dict(
        message_id="msg_1",
        request_id="req_1",
        turn_index=1,
        ts="2026-09-18T12:00:00.000Z",
        model="claude-sonnet-5",
        input_tokens=0,
        cache_creation_tokens=0,
        cache_read_tokens=0,
        output_tokens=0,
        cc_5m=0,
        cc_1h=0,
        ctx=0,
    )
    fields.update(overrides)
    return model.Turn(**fields)


# --------------------------------------------------------------------
# Packaged default
# --------------------------------------------------------------------


def test_packaged_default_loads():
    pricing = load_pricing()
    assert pricing.version == "2026-10-01"
    assert pricing.currency == "USD"
    assert pricing.sha256
    assert len(pricing.sha8) == 8


def test_packaged_default_covers_every_required_model_id():
    pricing = load_pricing()
    assert REQUIRED_PACKAGED_MODEL_IDS <= set(pricing.models)
    for model_id in REQUIRED_PACKAGED_MODEL_IDS:
        resolved = pricing.resolve_model(model_id)
        assert resolved is not None, model_id
        assert resolved.canonical_id == model_id
        assert resolved.matched_via == "exact"


def test_packaged_default_currency_and_version_surfaced():
    pricing = load_pricing()
    assert pricing.currency == "USD"
    assert pricing.version == "2026-10-01"
    assert pricing.source_url


def test_rates_meta_matches_pricing_toml():
    """``Pricing.rates_meta()`` (``report.json``'s additive ``meta.rates``)
    reports every priced model's own rates and ratios straight from the
    loaded rate card -- no independent re-derivation that could drift
    from ``pricing.toml`` itself."""
    pricing = load_pricing()
    meta = pricing.rates_meta()
    assert set(meta) == set(pricing.models)
    for model_id, rates in pricing.models.items():
        entry = meta[model_id]
        assert entry["input"] == rates.input
        assert entry["output"] == rates.output
        assert entry["cache_write_5m"] == rates.cache_write_5m
        assert entry["cache_write_1h"] == rates.cache_write_1h
        assert entry["cache_read"] == rates.cache_read
        assert entry["cache_read_ratio"] == pytest.approx(rates.cache_read / rates.input)
        assert entry["cache_write_5m_ratio"] == pytest.approx(rates.cache_write_5m / rates.input)
        assert entry["cache_write_1h_ratio"] == pytest.approx(rates.cache_write_1h / rates.input)
        assert model_id not in entry["input_ratio_to"]
        for other_id, other_rates in pricing.models.items():
            if other_id != model_id:
                assert entry["input_ratio_to"][other_id] == pytest.approx(rates.input / other_rates.input)


# --------------------------------------------------------------------
# cache_read_savings_usd (/api/summary's additive "cache_saved")
# --------------------------------------------------------------------

_TWO_MODEL_TOML = """
version = "test"
currency = "USD"

[models."model-a"]
aliases = []
input = 3.0
output = 15.0
cache_write_5m = 3.75
cache_write_1h = 6.0
cache_read = 0.3

[models."model-b"]
aliases = []
input = 1.0
output = 5.0
cache_write_5m = 1.25
cache_write_1h = 2.0
cache_read = 0.1
"""


def test_cache_read_savings_usd_hand_computed(tmp_path):
    path = tmp_path / "two_models.toml"
    path.write_text(_TWO_MODEL_TOML, encoding="utf-8")
    pricing = load_pricing(path=path)
    rows = [
        {"model": "model-a", "cache_read_tokens": 2_000_000},
        {"model": "model-b", "cache_read_tokens": 500_000},
    ]
    # model-a: 2,000,000 * (3.0 - 0.3) / 1e6 = 5.4
    # model-b:   500,000 * (1.0 - 0.1) / 1e6 = 0.45
    assert cache_read_savings_usd(rows, pricing) == pytest.approx(5.85)


def test_cache_read_savings_usd_skips_unresolved_models(tmp_path):
    path = tmp_path / "two_models.toml"
    path.write_text(_TWO_MODEL_TOML, encoding="utf-8")
    pricing = load_pricing(path=path)
    rows = [
        {"model": "not-a-real-model", "cache_read_tokens": 999_999},
        {"model": "model-a", "cache_read_tokens": 1_000_000},
    ]
    # The unresolved row is left out entirely, not priced at zero.
    assert cache_read_savings_usd(rows, pricing) == pytest.approx(2.7)
    assert cache_read_savings_usd([], pricing) == 0.0


# --------------------------------------------------------------------
# Path resolution order
# --------------------------------------------------------------------


def test_explicit_path_overrides_everything(tmp_path):
    explicit = tmp_path / "explicit.toml"
    explicit.write_text((FIXTURES / "pricing_min.toml").read_text(encoding="utf-8"), encoding="utf-8")
    pricing = load_pricing(path=explicit, config_dir=tmp_path / "unused")
    assert pricing.path == str(explicit)
    assert "claude-widget-9" in pricing.models


def test_config_dir_used_when_present(tmp_path):
    claudeglass_dir = tmp_path / "claudeglass"
    claudeglass_dir.mkdir()
    (claudeglass_dir / "pricing.toml").write_text(
        (FIXTURES / "pricing_min.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    pricing = load_pricing(config_dir=claudeglass_dir)
    assert "claude-widget-9" in pricing.models
    assert pricing.path == str(claudeglass_dir / "pricing.toml")


def test_falls_back_to_packaged_default_when_config_dir_empty(tmp_path):
    pricing = load_pricing(config_dir=tmp_path / "claudeglass")
    assert "claude-sonnet-5" in pricing.models
    assert "packaged default" in pricing.path


def test_claude_config_dir_env_var_moves_the_lookup(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    claudeglass_dir = tmp_path / "claudeglass"
    claudeglass_dir.mkdir()
    (claudeglass_dir / "pricing.toml").write_text(
        (FIXTURES / "pricing_min.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    pricing = load_pricing()
    assert "claude-widget-9" in pricing.models


# --------------------------------------------------------------------
# Malformed files -> PricingError
# --------------------------------------------------------------------


def test_malformed_toml_syntax_raises_pricing_error():
    with pytest.raises(PricingError):
        load_pricing(path=FIXTURES / "pricing_bad.toml")


def test_missing_required_field_raises_pricing_error():
    with pytest.raises(PricingError, match="cache_read"):
        load_pricing(path=FIXTURES / "pricing_missing_field.toml")


def test_missing_file_raises_pricing_error(tmp_path):
    with pytest.raises(PricingError):
        load_pricing(path=tmp_path / "does-not-exist.toml")


# --------------------------------------------------------------------
# Model resolution
# --------------------------------------------------------------------


@pytest.fixture()
def min_pricing() -> Pricing:
    return load_pricing(path=FIXTURES / "pricing_min.toml")


def test_alias_resolution(min_pricing):
    resolved = min_pricing.resolve_model("widget")
    assert resolved is not None
    assert resolved.canonical_id == "claude-widget-9"
    assert resolved.matched_via == "alias"


def test_alias_resolution_with_1m_suffix_in_alias_table(min_pricing):
    # "widget[1m]" is registered directly as an alias, so it should
    # resolve as an ordinary alias hit, not via the [1m]-stripping step.
    resolved = min_pricing.resolve_model("widget[1m]")
    assert resolved is not None
    assert resolved.canonical_id == "claude-widget-9"
    assert resolved.matched_via == "alias"


def test_packaged_fable_1m_alias():
    pricing = load_pricing()
    resolved = pricing.resolve_model("fable[1m]")
    assert resolved is not None
    assert resolved.canonical_id == "claude-fable-5-1"
    assert resolved.matched_via == "alias"


@pytest.mark.parametrize("alias", ["opus", "opus[1m]"])
def test_packaged_opus_alias_resolves_to_opus_5_5(alias):
    # D1: "opus"/"opus[1m]" used to resolve to the older claude-opus-5;
    # the docs (V23) put them on the current claude-opus-5-5.
    pricing = load_pricing()
    resolved = pricing.resolve_model(alias)
    assert resolved is not None
    assert resolved.canonical_id == "claude-opus-5-5"
    assert resolved.matched_via == "alias"


def test_packaged_claude_opus_5_has_no_alias_of_its_own():
    # The older model is still resolvable by its own id; it just no
    # longer carries the "opus"/"opus[1m]" aliases, which now point at
    # claude-opus-5-5 (checked above).
    pricing = load_pricing()
    assert "claude-opus-5" not in pricing.aliases.values()
    resolved = pricing.resolve_model("claude-opus-5")
    assert resolved is not None
    assert resolved.canonical_id == "claude-opus-5"
    assert resolved.matched_via == "exact"


@pytest.mark.parametrize("alias", ["sonnet", "sonnet[1m]"])
def test_packaged_sonnet_alias_resolves_to_sonnet_5_5(alias):
    # Sonnet 5.5 shipped on 2026-09-28; the aliases moved to it.
    pricing = load_pricing()
    resolved = pricing.resolve_model(alias)
    assert resolved is not None
    assert resolved.canonical_id == "claude-sonnet-5-5"
    assert resolved.matched_via == "alias"


def test_packaged_claude_sonnet_5_has_no_alias_of_its_own():
    pricing = load_pricing()
    assert "claude-sonnet-5" not in pricing.aliases.values()
    resolved = pricing.resolve_model("claude-sonnet-5")
    assert resolved is not None
    assert resolved.canonical_id == "claude-sonnet-5"
    assert resolved.matched_via == "exact"


def test_packaged_sonnet_5_5_rates_match_sonnet_5():
    # Same list price, 1M window and data-residency uplift as Sonnet 5,
    # and no fast mode (documented only for Opus 5.5, Opus 5, Opus 4.8).
    pricing = load_pricing()
    rates = pricing.models["claude-sonnet-5-5"]
    assert (rates.input, rates.output, rates.cache_write_5m, rates.cache_write_1h, rates.cache_read) == (
        2.0, 10.0, 2.5, 4.0, 0.2,
    )
    assert rates.geo_multipliers == {"us": 1.1}
    assert rates.fast is None
    assert rates.context_window_tokens == 1_000_000


def test_packaged_aliases_are_unique():
    # _build_pricing lets the last row naming an alias win without a
    # word, so an alias copied onto two rows would silently pick one.
    import tomllib

    from importlib.resources import files

    data = tomllib.loads(files("claudeglass").joinpath("pricing.toml").read_text(encoding="utf-8"))
    seen: dict[str, str] = {}
    for model_id, entry in data["models"].items():
        for alias in entry.get("aliases", []):
            assert alias not in seen, f"{alias!r} is on both {seen[alias]} and {model_id}"
            seen[alias] = model_id


def test_strip_1m_suffix_for_an_unregistered_alias(min_pricing):
    # "claude-gadget-2" has no "[1m]" alias registered, so resolution
    # must fall through to the strip-1m step against its exact id.
    resolved = min_pricing.resolve_model("claude-gadget-2[1m]")
    assert resolved is not None
    assert resolved.canonical_id == "claude-gadget-2"
    assert resolved.matched_via == "strip_1m"


def test_bedrock_prefix_and_suffix_strip():
    pricing = load_pricing()
    resolved = pricing.resolve_model("us.anthropic.claude-opus-4-8")
    assert resolved is not None
    assert resolved.canonical_id == "claude-opus-4-8"
    assert resolved.matched_via == "cloud_strip"


def test_bedrock_versioned_suffix_strip(min_pricing):
    resolved = min_pricing.resolve_model("anthropic.claude-widget-9-v1:0")
    assert resolved is not None
    assert resolved.canonical_id == "claude-widget-9"
    assert resolved.matched_via == "cloud_strip"


def test_vertex_date_suffix_strip():
    pricing = load_pricing()
    resolved = pricing.resolve_model("claude-sonnet-4-5@20250929")
    assert resolved is not None
    assert resolved.canonical_id == "claude-sonnet-4-5"
    assert resolved.matched_via == "cloud_strip"


def test_longest_prefix_match(min_pricing):
    resolved = min_pricing.resolve_model("claude-widget-9-preview-2026")
    assert resolved is not None
    assert resolved.canonical_id == "claude-widget-9"
    assert resolved.matched_via == "prefix"


#: Two models sharing a numeric prefix, used to isolate the token-
#: boundary rule from the packaged pricing.toml's own model set (which
#: happens to give "claude-opus-4" and "claude-opus-4-1" identical
#: rates, making a wrong match invisible in the numbers).
_BOUNDARY_FIXTURE_TOML = """
version = "test"
currency = "USD"

[models."claude-foo-4-1"]
aliases = []
input = 1.0
output = 2.0
cache_write_5m = 0.5
cache_write_1h = 0.8
cache_read = 0.1
"""

_BOUNDARY_FIXTURE_WITH_SHORTER_TOML = (
    _BOUNDARY_FIXTURE_TOML
    + """
[models."claude-foo-4"]
aliases = []
input = 9.0
output = 9.0
cache_write_5m = 9.0
cache_write_1h = 9.0
cache_read = 9.0
"""
)


def test_prefix_match_requires_token_boundary_no_shorter_fallback(tmp_path):
    path = tmp_path / "boundary.toml"
    path.write_text(_BOUNDARY_FIXTURE_TOML, encoding="utf-8")
    pricing = load_pricing(path=path)
    # "claude-foo-4-10-x" merely shares the leading digits of the
    # registered "claude-foo-4-1" id — the character right after the
    # would-be match is "0", not "-"/"@"/end — and there's no shorter
    # registered id to fall back to, so it must resolve to None.
    assert pricing.resolve_model("claude-foo-4-10-x") is None


def test_prefix_match_boundary_rejects_longer_falls_back_to_shorter(tmp_path):
    path = tmp_path / "boundary2.toml"
    path.write_text(_BOUNDARY_FIXTURE_WITH_SHORTER_TOML, encoding="utf-8")
    pricing = load_pricing(path=path)
    resolved = pricing.resolve_model("claude-foo-4-10-x")
    assert resolved is not None
    # Must NOT resolve to "claude-foo-4-1" (fails the boundary check) —
    # the shorter "claude-foo-4" is a genuinely valid boundary match.
    assert resolved.canonical_id == "claude-foo-4"
    assert resolved.matched_via == "prefix"


def test_prefix_match_boundary_dash_still_resolves():
    pricing = load_pricing()
    resolved = pricing.resolve_model("claude-opus-4-1-preview")
    assert resolved is not None
    assert resolved.canonical_id == "claude-opus-4-1"
    assert resolved.matched_via == "prefix"


def test_cloud_strip_matched_via_survives_a_subsequent_prefix_step(min_pricing):
    # "claude-widget-9" has no exact/alias hit for the dated variant
    # below, so after the Bedrock prefix is stripped, resolution only
    # succeeds via the prefix-match step — matched_via must still report
    # "cloud_strip", not "prefix", since the cloud wrapping is the
    # substantive fact.
    resolved = min_pricing.resolve_model("us.anthropic.claude-widget-9-20260101")
    assert resolved is not None
    assert resolved.canonical_id == "claude-widget-9"
    assert resolved.matched_via == "cloud_strip"


def test_unknown_model_resolves_to_none(min_pricing):
    assert min_pricing.resolve_model("claude-totally-unheard-of") is None


# --------------------------------------------------------------------
# ResolvedRates.approximate (fix 2): true only once resolution bottoms
# out at a prefix match (with or without a preceding cloud strip), never
# for exact/alias/strip_1m, and never for a cloud_strip that itself
# landed on an exact/alias id — matched_via alone can't tell the two
# "cloud_strip" cases apart, which is why this is a separate field.
# --------------------------------------------------------------------


def test_approximate_false_for_exact_match(min_pricing):
    resolved = min_pricing.resolve_model("claude-widget-9")
    assert resolved.matched_via == "exact"
    assert resolved.approximate is False


def test_approximate_false_for_alias_match(min_pricing):
    resolved = min_pricing.resolve_model("widget")
    assert resolved.matched_via == "alias"
    assert resolved.approximate is False


def test_approximate_false_for_strip_1m_match(min_pricing):
    resolved = min_pricing.resolve_model("claude-gadget-2[1m]")
    assert resolved.matched_via == "strip_1m"
    assert resolved.approximate is False


def test_approximate_false_for_cloud_strip_to_exact(min_pricing):
    resolved = min_pricing.resolve_model("anthropic.claude-widget-9-v1:0")
    assert resolved.matched_via == "cloud_strip"
    assert resolved.approximate is False


def test_approximate_true_for_plain_prefix_match(min_pricing):
    resolved = min_pricing.resolve_model("claude-widget-9-preview-2026")
    assert resolved.matched_via == "prefix"
    assert resolved.approximate is True


def test_approximate_true_for_cloud_strip_then_prefix(min_pricing):
    # matched_via still reads "cloud_strip" (unchanged -- see the test
    # above this module's docstring points at), but the resolution only
    # succeeded via the prefix step underneath it, so this is still only
    # an approximation of claude-widget-9's real price.
    resolved = min_pricing.resolve_model("us.anthropic.claude-widget-9-20260101")
    assert resolved.matched_via == "cloud_strip"
    assert resolved.approximate is True


# --------------------------------------------------------------------
# Newer versions the rate card has no row for yet: ResolvedRates.
# newer_version and newer_version_of. A prefix match whose leftover is a
# bare minor version is a newer release of the matched model; a dated,
# "-preview" or "-0" leftover is not.
# --------------------------------------------------------------------


@pytest.mark.parametrize(
    "model_id",
    [
        "claude-widget-9-1",
        "claude-widget-9-12",
        "claude-widget-9-1-20261001",
        "claude-widget-9-1[1m]",
        "us.anthropic.claude-widget-9-1-v1:0",
        "claude-widget-9-1@20261001",
    ],
)
def test_newer_version_is_flagged_on_a_minor_version_leftover(min_pricing, model_id):
    resolved = min_pricing.resolve_model(model_id)
    assert resolved.canonical_id == "claude-widget-9"
    assert resolved.approximate is True
    assert resolved.newer_version is True
    assert newer_version_of(model_id, "claude-widget-9") is True


@pytest.mark.parametrize(
    "model_id",
    ["claude-widget-9-20261001", "claude-widget-9-preview-2026", "claude-widget-9-0", "claude-widget-9-01"],
)
def test_newer_version_is_not_flagged_on_a_date_preview_or_zero(min_pricing, model_id):
    resolved = min_pricing.resolve_model(model_id)
    assert resolved.canonical_id == "claude-widget-9"
    assert resolved.approximate is True
    assert resolved.newer_version is False
    assert newer_version_of(model_id, "claude-widget-9") is False


@pytest.mark.parametrize("model_id", ["claude-widget-9", "widget", "widget[1m]", "anthropic.claude-widget-9-v1:0"])
def test_newer_version_is_false_for_an_exact_or_alias_hit(min_pricing, model_id):
    resolved = min_pricing.resolve_model(model_id)
    assert resolved.approximate is False
    assert resolved.newer_version is False
    assert newer_version_of(model_id, resolved.canonical_id) is False


def test_newer_version_of_on_the_packaged_card():
    pricing = load_pricing()
    # claude-opus-4-0 is a real alias of claude-opus-4, not a newer one.
    assert pricing.resolve_model("claude-opus-4-0").newer_version is False
    assert newer_version_of("claude-opus-4-0", "claude-opus-4") is False
    assert newer_version_of("claude-opus-4-1-preview", "claude-opus-4-1") is False
    assert newer_version_of("claude-sonnet-5-20261001", "claude-sonnet-5") is False
    # Two-digit minors, which the token boundary keeps off claude-opus-4-1.
    resolved = pricing.resolve_model("claude-opus-4-10")
    assert resolved.canonical_id == "claude-opus-4"
    assert resolved.newer_version is True
    assert newer_version_of("claude-opus-4-10-x", "claude-opus-4") is True
    # Only a prefix at a token boundary counts.
    assert newer_version_of("claude-opus-4-10", "claude-opus-4-1") is False
    assert newer_version_of("claude-sonnet-5-5", "claude-opus-5") is False
    assert newer_version_of("claude-sonnet-5-5", "") is False


@pytest.mark.parametrize(
    "model_id,expected",
    [
        ("claude-widget-9-1", "claude-widget-9-1"),
        ("claude-widget-9-12", "claude-widget-9-12"),
        ("claude-widget-9-1-20261001", "claude-widget-9-1"),
        ("claude-widget-9-1[1m]", "claude-widget-9-1"),
        ("us.anthropic.claude-widget-9-1-v1:0", "claude-widget-9-1"),
        ("claude-widget-9-1@20261001", "claude-widget-9-1"),
        ("claude-widget-9", None),
        ("claude-widget-9-20261001", None),
        ("claude-widget-9-0", None),
        ("claude-widget-9-preview-2026", None),
    ],
)
def test_newer_version_id_names_the_release_without_its_wrapping(model_id, expected):
    """The newer release itself, so two forms of it compare equal and it
    never compares equal to the older model it is priced as."""
    assert newer_version_id(model_id, "claude-widget-9") == expected
    assert newer_version_of(model_id, "claude-widget-9") is (expected is not None)


# --------------------------------------------------------------------
# Pricing.model_ids_meta (report.json's additive meta.model_ids)
# --------------------------------------------------------------------


def test_model_ids_meta_maps_every_alias_to_its_canonical_id():
    pricing = load_pricing()
    meta = pricing.model_ids_meta()
    assert meta == pricing.aliases
    assert meta["sonnet"] == "claude-sonnet-5-5"
    assert meta["sonnet[1m]"] == "claude-sonnet-5-5"
    assert meta["opus"] == "claude-opus-5-5"
    assert meta["best"] == "claude-fable-5-1"
    assert meta["claude-haiku-4-5"] == "claude-haiku-4-5-20251001"


def test_model_ids_meta_adds_observed_ids_priced_as_another_id(min_pricing):
    observed = [
        "claude-widget-9",  # canonical: already a meta.rates key
        "claude-widget-9-20261001",  # dated, by prefix
        "claude-widget-9-1",  # newer version, by prefix
        "us.anthropic.claude-widget-9-v1:0",  # Bedrock
        "claude-gadget-2[1m]",  # strip_1m
        "widget",  # alias, already there
        "<unknown>",  # no model on the turn
        "claude-mystery-model",  # unpriced
        "",
        None,
    ]
    assert min_pricing.model_ids_meta(observed) == {
        "widget": "claude-widget-9",
        "widget[1m]": "claude-widget-9",
        "claude-widget-9-20261001": "claude-widget-9",
        "claude-widget-9-1": "claude-widget-9",
        "us.anthropic.claude-widget-9-v1:0": "claude-widget-9",
        "claude-gadget-2[1m]": "claude-gadget-2",
    }
    # The rate card's own alias table is never changed by it.
    assert min_pricing.aliases == {"widget": "claude-widget-9", "widget[1m]": "claude-widget-9"}


# --------------------------------------------------------------------
# Legacy dated ids (independent-review fix 5)
# --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        ("claude-3-5-haiku-20241022", (0.8, 4.0, 1.0, 1.6, 0.08)),
        ("claude-3-5-sonnet-20241022", (3.0, 15.0, 3.75, 6.0, 0.3)),
        ("claude-3-7-sonnet-20250219", (3.0, 15.0, 3.75, 6.0, 0.3)),
        ("claude-3-haiku-20240307", (0.25, 1.25, 0.3, 0.5, 0.03)),
        ("claude-3-opus-20240229", (15.0, 75.0, 18.75, 30.0, 1.5)),
    ],
)
def test_packaged_legacy_ids_resolve_exactly_with_documented_rates(model_id, expected):
    pricing = load_pricing()
    resolved = pricing.resolve_model(model_id)
    assert resolved is not None
    assert resolved.canonical_id == model_id
    assert resolved.matched_via == "exact"
    rates = resolved.rates
    assert (
        rates.input,
        rates.output,
        rates.cache_write_5m,
        rates.cache_write_1h,
        rates.cache_read,
    ) == expected


def test_packaged_legacy_ids_have_no_geo_multiplier():
    # The documented "us" uplift only applies to 4.6-generation and
    # later models; every legacy dated id must carry none.
    pricing = load_pricing()
    for model_id in [
        "claude-3-5-haiku-20241022",
        "claude-3-5-sonnet-20241022",
        "claude-3-7-sonnet-20250219",
        "claude-3-haiku-20240307",
        "claude-3-opus-20240229",
    ]:
        assert pricing.models[model_id].geo_multipliers == {}


@pytest.mark.parametrize(
    "model_id",
    [
        "claude-fable-5-1",
        "claude-fable-5",
        "claude-opus-5-5",
        "claude-opus-5",
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-opus-4-6",
        "claude-sonnet-5-5",
        "claude-sonnet-5",
        "claude-sonnet-4-6",
    ],
)
def test_packaged_1m_context_models(model_id):
    # D2/D4/COV-12/V24: Fable 5.1, Fable 5, Sonnet 5.5, Sonnet 5, Sonnet
    # 4.6 and Opus 4.6+ have a native 1M token context window.
    pricing = load_pricing()
    assert pricing.models[model_id].context_window_tokens == 1_000_000


@pytest.mark.parametrize(
    "model_id",
    [
        "claude-opus-4-5",
        "claude-opus-4-1",
        "claude-opus-4",
        "claude-sonnet-4-5",
        "claude-sonnet-4",
        "claude-haiku-4-5-20251001",
        "claude-3-5-haiku-20241022",
        "claude-3-opus-20240229",
    ],
)
def test_packaged_200k_context_models_default(model_id):
    pricing = load_pricing()
    assert pricing.models[model_id].context_window_tokens == 200_000


def test_context_window_tokens_defaults_when_toml_omits_it(min_pricing):
    # pricing_min.toml sets no context_window_tokens on any model.
    assert min_pricing.models["claude-widget-9"].context_window_tokens == 200_000


def test_context_window_tokens_parsed_from_toml(tmp_path):
    # Insert the key into claude-widget-9's own table (not the file's
    # end), so it lands in the right TOML table.
    text = (FIXTURES / "pricing_min.toml").read_text(encoding="utf-8").replace(
        '[models."claude-widget-9"]\naliases = ["widget", "widget[1m]"]',
        '[models."claude-widget-9"]\naliases = ["widget", "widget[1m]"]\ncontext_window_tokens = 1000000',
    )
    custom = tmp_path / "custom.toml"
    custom.write_text(text, encoding="utf-8")
    pricing = load_pricing(path=custom)
    assert pricing.models["claude-widget-9"].context_window_tokens == 1_000_000
    assert pricing.models["claude-gadget-2"].context_window_tokens == 200_000


def test_packaged_web_search_priced_at_documented_rate():
    # D1/V27: the pricing page documents web search at $10 per 1,000
    # searches; it used to sit at 0.0 (counted but not priced).
    pricing = load_pricing()
    assert pricing.server_tools["web_search_per_1000"] == 10.0


def test_server_tools_absent_prices_web_search_at_zero(min_pricing):
    # pricing_min.toml has no [server_tools] table at all.
    assert min_pricing.server_tools == {}
    resolved = min_pricing.resolve_model("claude-widget-9")
    assert resolved.web_search_per_1000 == 0.0


@pytest.mark.parametrize("model_id", [None, "", "<synthetic>"])
def test_synthetic_and_empty_resolve_to_none_without_error(min_pricing, model_id):
    assert min_pricing.resolve_model(model_id) is None


# --------------------------------------------------------------------
# price_turn
# --------------------------------------------------------------------


def test_hand_computed_money_sonnet_5():
    pricing = load_pricing()
    resolved = pricing.resolve_model("claude-sonnet-5")
    turn = _turn(model="claude-sonnet-5", input_tokens=1_000_000, cache_read_tokens=2_000_000)
    breakdown = price_turn(turn, resolved)
    assert breakdown.model_known is True
    assert breakdown.input_cost == pytest.approx(2.00)
    assert breakdown.cache_read_cost == pytest.approx(0.40)
    assert breakdown.output_cost == pytest.approx(0.0)
    assert breakdown.cache_write_cost == pytest.approx(0.0)
    assert breakdown.total == pytest.approx(2.40)


def test_web_search_cost_appears_in_the_total():
    # D1: web search used to price at $0 regardless of usage. It is now
    # folded into CostBreakdown.total via ResolvedRates.web_search_per_1000
    # (see resolve_model / price_turn).
    pricing = load_pricing()
    resolved = pricing.resolve_model("claude-sonnet-5")
    turn = _turn(model="claude-sonnet-5", web_search_requests=2_500)
    breakdown = price_turn(turn, resolved)
    assert breakdown.server_tool_cost == pytest.approx(25.0)  # 2,500 / 1,000 * $10
    assert breakdown.total == pytest.approx(25.0)


def test_web_search_cost_zero_by_default_and_stacks_with_token_costs(min_pricing):
    import dataclasses

    priced = dataclasses.replace(min_pricing, server_tools={"web_search_per_1000": 8.0})
    resolved = priced.resolve_model("claude-widget-9")
    turn = _turn(model="claude-widget-9", input_tokens=1_000_000, web_search_requests=500)
    breakdown = price_turn(turn, resolved)
    assert breakdown.input_cost == pytest.approx(1.0)  # 1,000,000 / 1e6 * 1.0
    assert breakdown.server_tool_cost == pytest.approx(4.0)  # 500 / 1,000 * $8
    assert breakdown.total == pytest.approx(5.0)

    # No web search on this turn: no server-tool cost, even at a nonzero rate.
    quiet_turn = _turn(model="claude-widget-9", input_tokens=1_000_000)
    assert price_turn(quiet_turn, resolved).server_tool_cost == 0.0


def test_web_fetch_requests_never_priced():
    # There is no documented per-request rate for web_fetch, unlike
    # web_search; pricing.toml's [server_tools] comment says so.
    pricing = load_pricing()
    resolved = pricing.resolve_model("claude-sonnet-5")
    turn = _turn(model="claude-sonnet-5", web_fetch_requests=1_000)
    breakdown = price_turn(turn, resolved)
    assert breakdown.server_tool_cost == 0.0
    assert breakdown.total == 0.0


def test_price_turn_accepts_bare_model_rates_too():
    pricing = load_pricing()
    resolved = pricing.resolve_model("claude-sonnet-5")
    turn = _turn(input_tokens=1_000_000)
    via_resolved = price_turn(turn, resolved)
    via_bare_rates = price_turn(turn, resolved.rates)
    assert via_resolved == via_bare_rates


def test_unknown_model_prices_at_zero(min_pricing):
    turn = _turn(model="claude-totally-unheard-of", input_tokens=1_000_000)
    resolved = min_pricing.resolve_model(turn.model)
    breakdown = price_turn(turn, resolved)
    assert breakdown.model_known is False
    assert breakdown.total == 0.0
    assert breakdown.input_cost == 0.0


def test_default_write_split_uses_turn_cc_5m_and_cc_1h(min_pricing):
    resolved = min_pricing.resolve_model("claude-widget-9")
    turn = _turn(model="claude-widget-9", cc_5m=1_000_000, cc_1h=500_000)
    breakdown = price_turn(turn, resolved)
    # 1,000,000 * 0.5 (cache_write_5m) + 500,000 * 0.8 (cache_write_1h), /1e6
    assert breakdown.cache_write_cost == pytest.approx(1_000_000 / 1e6 * 0.5 + 500_000 / 1e6 * 0.8)


def test_simulation_path_equals_default_path_for_observed_split(min_pricing):
    """The invariant the TTL package (WP4) relies on: passing a turn's own
    observed split explicitly through the simulation arguments must equal
    the default (implicit) path exactly.
    """
    resolved = min_pricing.resolve_model("claude-widget-9")
    turn = _turn(
        model="claude-widget-9",
        input_tokens=12_345,
        output_tokens=6_789,
        cc_5m=222_000,
        cc_1h=111_000,
        cache_read_tokens=444_000,
    )
    default_breakdown = price_turn(turn, resolved)
    simulated_breakdown = price_turn(
        turn,
        resolved,
        write_split={"5m": turn.cc_5m, "1h": turn.cc_1h},
        read_tokens=turn.cache_read_tokens,
    )
    assert simulated_breakdown == default_breakdown


def test_write_split_simulation_overrides_observed_values(min_pricing):
    resolved = min_pricing.resolve_model("claude-widget-9")
    # Turn observed an all-5m split; simulate what an all-1h policy
    # would have cost for the same cacheable volume instead.
    turn = _turn(model="claude-widget-9", cc_5m=1_000_000, cc_1h=0, cache_read_tokens=0)
    simulated_1h = price_turn(turn, resolved, write_split={"1h": 1_000_000}, read_tokens=0)
    assert simulated_1h.cache_write_cost == pytest.approx(1_000_000 / 1e6 * 0.8)
    assert simulated_1h.cache_write_cost != price_turn(turn, resolved).cache_write_cost


# --------------------------------------------------------------------
# write_split key normalisation (independent-review fix 4; plan
# Appendix A4's TTL simulation calls price_turn with write_split={T:
# write} where T is 300/3600, not "5m"/"1h").
# --------------------------------------------------------------------


def test_write_split_accepts_seconds_int_key_for_5m(min_pricing):
    resolved = min_pricing.resolve_model("claude-widget-9")
    turn = _turn(model="claude-widget-9")
    breakdown = price_turn(turn, resolved, write_split={300: 1_000_000}, read_tokens=0)
    assert breakdown.cache_write_cost == pytest.approx(1_000_000 / 1e6 * 0.5)  # cache_write_5m rate


def test_write_split_accepts_seconds_int_key_for_1h(min_pricing):
    resolved = min_pricing.resolve_model("claude-widget-9")
    turn = _turn(model="claude-widget-9")
    breakdown = price_turn(turn, resolved, write_split={3600: 1_000_000}, read_tokens=0)
    assert breakdown.cache_write_cost == pytest.approx(1_000_000 / 1e6 * 0.8)  # cache_write_1h rate


def test_write_split_accepts_stringified_seconds_key(min_pricing):
    resolved = min_pricing.resolve_model("claude-widget-9")
    turn = _turn(model="claude-widget-9")
    via_int = price_turn(turn, resolved, write_split={300: 1_000_000}, read_tokens=0)
    via_str = price_turn(turn, resolved, write_split={"300": 1_000_000}, read_tokens=0)
    assert via_int == via_str


def test_write_split_unknown_key_raises_value_error(min_pricing):
    resolved = min_pricing.resolve_model("claude-widget-9")
    turn = _turn(model="claude-widget-9")
    with pytest.raises(ValueError):
        price_turn(turn, resolved, write_split={"90m": 1_000_000}, read_tokens=0)


def test_geo_multiplier_applied_to_all_four_components(min_pricing):
    resolved = min_pricing.resolve_model("claude-widget-9")
    turn = _turn(
        model="claude-widget-9",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cc_5m=1_000_000,
        cache_read_tokens=1_000_000,
    )
    base = price_turn(turn, resolved)
    with_geo = price_turn(turn, resolved, geo="us")
    assert with_geo.input_cost == pytest.approx(base.input_cost * 1.2)
    assert with_geo.output_cost == pytest.approx(base.output_cost * 1.2)
    assert with_geo.cache_write_cost == pytest.approx(base.cache_write_cost * 1.2)
    assert with_geo.cache_read_cost == pytest.approx(base.cache_read_cost * 1.2)
    assert with_geo.total == pytest.approx(base.total * 1.2)


def test_geo_multiplier_no_effect_when_geo_unmatched(min_pricing):
    resolved = min_pricing.resolve_model("claude-widget-9")
    turn = _turn(model="claude-widget-9", input_tokens=1_000_000)
    base = price_turn(turn, resolved)
    other_geo = price_turn(turn, resolved, geo="eu")
    assert other_geo == base


# --------------------------------------------------------------------
# geo sentinel default (independent-review fix 4): omitting `geo` means
# "use turn.inference_geo"; an explicit geo=None disables it even when
# the turn observed one.
# --------------------------------------------------------------------


def test_geo_omitted_uses_turns_own_inference_geo(min_pricing):
    resolved = min_pricing.resolve_model("claude-widget-9")
    turn = _turn(model="claude-widget-9", input_tokens=1_000_000, inference_geo="us")
    breakdown = price_turn(turn, resolved)  # geo not passed at all
    assert breakdown.input_cost == pytest.approx(1_000_000 / 1e6 * 1.0 * 1.2)


def test_geo_explicit_none_overrides_turns_inference_geo(min_pricing):
    resolved = min_pricing.resolve_model("claude-widget-9")
    turn = _turn(model="claude-widget-9", input_tokens=1_000_000, inference_geo="us")
    breakdown = price_turn(turn, resolved, geo=None)  # explicit override disables it
    assert breakdown.input_cost == pytest.approx(1_000_000 / 1e6 * 1.0)


def test_geo_not_available_string_applies_no_multiplier(min_pricing):
    resolved = min_pricing.resolve_model("claude-widget-9")
    turn = _turn(model="claude-widget-9", input_tokens=1_000_000, inference_geo="not_available")
    breakdown = price_turn(turn, resolved)
    assert breakdown.input_cost == pytest.approx(1_000_000 / 1e6 * 1.0)


def test_long_context_multiplier_applies_above_threshold(min_pricing):
    resolved = min_pricing.resolve_model("claude-widget-9")  # multiplier=2.0 at >=100000
    below = _turn(model="claude-widget-9", input_tokens=1_000_000, ctx=99_999)
    at_threshold = _turn(model="claude-widget-9", input_tokens=1_000_000, ctx=100_000)

    below_breakdown = price_turn(below, resolved)
    at_breakdown = price_turn(at_threshold, resolved)

    assert below_breakdown.long_context_applied is False
    assert at_breakdown.long_context_applied is True
    assert at_breakdown.input_cost == pytest.approx(below_breakdown.input_cost * 2.0)


def test_long_context_explicit_overrides_win_over_multiplier(min_pricing):
    resolved = min_pricing.resolve_model("claude-gadget-2")
    # long_context on claude-gadget-2 carries BOTH multiplier=3.0 and
    # explicit input/cache_read overrides; overrides must win for the
    # fields they cover.
    turn = _turn(
        model="claude-gadget-2",
        input_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        ctx=50_000,
    )
    breakdown = price_turn(turn, resolved)
    assert breakdown.long_context_applied is True
    # explicit override: input rate becomes 9.0/million, not 4.0*3=12.0
    assert breakdown.input_cost == pytest.approx(9.0)
    # explicit override: cache_read rate becomes 0.9/million, not 0.4*3=1.2
    assert breakdown.cache_read_cost == pytest.approx(0.9)


# --------------------------------------------------------------------
# Fast mode (fix 3): FastRule parsing and price_turn's rate multiplier.
# "claude-widget-9" carries a [.fast] table (multiplier=2.0);
# "claude-gadget-2" deliberately does not, for the standard-rate
# fallback case.
# --------------------------------------------------------------------


def test_fast_rule_parsed_from_pricing_toml(min_pricing):
    rates = min_pricing.models["claude-widget-9"]
    assert isinstance(rates.fast, FastRule)
    assert rates.fast.multiplier == pytest.approx(2.0)


def test_model_without_fast_table_has_none(min_pricing):
    assert min_pricing.models["claude-gadget-2"].fast is None


def test_fast_speed_doubles_every_rate_when_model_has_fast_table(min_pricing):
    resolved = min_pricing.resolve_model("claude-widget-9")
    kwargs = dict(
        model="claude-widget-9",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cc_5m=1_000_000,
        cache_read_tokens=1_000_000,
    )
    standard = price_turn(_turn(**kwargs), resolved)
    fast = price_turn(_turn(speed="fast", **kwargs), resolved)
    assert fast.fast_applied is True
    assert fast.input_cost == pytest.approx(standard.input_cost * 2.0)
    assert fast.output_cost == pytest.approx(standard.output_cost * 2.0)
    assert fast.cache_write_cost == pytest.approx(standard.cache_write_cost * 2.0)
    assert fast.cache_read_cost == pytest.approx(standard.cache_read_cost * 2.0)
    assert fast.total == pytest.approx(standard.total * 2.0)


def test_fast_speed_on_model_without_fast_table_prices_at_standard_rate(min_pricing):
    resolved = min_pricing.resolve_model("claude-gadget-2")
    standard = price_turn(_turn(model="claude-gadget-2", input_tokens=1_000_000), resolved)
    fast = price_turn(_turn(model="claude-gadget-2", input_tokens=1_000_000, speed="fast"), resolved)
    assert fast.fast_applied is False
    assert fast == standard


def test_standard_speed_never_applies_fast_multiplier(min_pricing):
    resolved = min_pricing.resolve_model("claude-widget-9")
    turn = _turn(model="claude-widget-9", input_tokens=1_000_000, speed="standard")
    assert price_turn(turn, resolved).fast_applied is False


def test_unset_speed_never_applies_fast_multiplier(min_pricing):
    resolved = min_pricing.resolve_model("claude-widget-9")
    turn = _turn(model="claude-widget-9", input_tokens=1_000_000)  # speed defaults to None
    assert price_turn(turn, resolved).fast_applied is False


def test_fast_multiplier_stacks_with_geo_multiplier(min_pricing):
    resolved = min_pricing.resolve_model("claude-widget-9")
    standard = price_turn(_turn(model="claude-widget-9", input_tokens=1_000_000), resolved, geo=None)
    fast_only = price_turn(
        _turn(model="claude-widget-9", input_tokens=1_000_000, speed="fast"), resolved, geo=None
    )
    fast_and_geo = price_turn(
        _turn(model="claude-widget-9", input_tokens=1_000_000, speed="fast"), resolved, geo="us"
    )
    # Fast (2x) and the "us" geo multiplier (1.2x) stack multiplicatively,
    # not replace one another: 2.0 * 1.2 = 2.4x standard.
    assert fast_only.input_cost == pytest.approx(standard.input_cost * 2.0)
    assert fast_and_geo.input_cost == pytest.approx(standard.input_cost * 2.0 * 1.2)


# --------------------------------------------------------------------
# PricingCoverage
# --------------------------------------------------------------------


def test_coverage_tracks_unknown_models_and_computes_percentage(min_pricing):
    coverage = PricingCoverage()

    known_turn = _turn(model="claude-widget-9", input_tokens=1_000_000)
    known_resolved = min_pricing.resolve_model(known_turn.model)
    coverage.add(known_turn, price_turn(known_turn, known_resolved))

    unknown_turn = _turn(model="claude-mystery-model", input_tokens=1_000_000)
    unknown_resolved = min_pricing.resolve_model(unknown_turn.model)
    coverage.add(unknown_turn, price_turn(unknown_turn, unknown_resolved))

    assert coverage.total_turns == 2
    assert coverage.priced_turns == 1
    assert "claude-mystery-model" in coverage.unknown
    assert coverage.unknown["claude-mystery-model"]["turns"] == 1
    assert coverage.unknown["claude-mystery-model"]["tokens"] == 1_000_000
    assert coverage.coverage_pct == pytest.approx(50.0)

    table = coverage.as_table()
    assert table.rows == [["claude-mystery-model", 1, 1_000_000]]


def test_coverage_pct_is_100_when_nothing_recorded():
    coverage = PricingCoverage()
    assert coverage.coverage_pct == 100.0


# -- closest-match and fast-priced-as-standard tracking (fixes 2/3) ------


def test_coverage_tracks_closest_match_turns(min_pricing):
    coverage = PricingCoverage()
    turn = _turn(model="claude-widget-9-preview-2026", input_tokens=1_000_000)
    resolved = min_pricing.resolve_model(turn.model)
    assert resolved.approximate is True
    coverage.add(turn, price_turn(turn, resolved), resolved)

    assert coverage.closest_match_turns == 1
    assert coverage.closest_matches["claude-widget-9-preview-2026"] == {
        "priced_as": "claude-widget-9",
        "turns": 1,
        "tokens": 1_000_000,
    }
    table = coverage.as_closest_match_table()
    assert table.rows == [["claude-widget-9-preview-2026", "claude-widget-9", 1, 1_000_000]]
    assert table.notes
    # A preview build isn't a newer version: only the generic note.
    assert len(table.notes) == 1


def test_closest_match_table_notes_a_newer_version_without_changing_its_shape(min_pricing):
    coverage = PricingCoverage()
    for model_id in ("claude-widget-9-1", "claude-widget-9-preview-2026"):
        turn = _turn(model=model_id, input_tokens=1_000)
        resolved = min_pricing.resolve_model(model_id)
        coverage.add(turn, price_turn(turn, resolved), resolved)

    table = coverage.as_closest_match_table()
    assert [c.key for c in table.columns] == ["model_id", "priced_as", "turns", "tokens"]
    assert coverage.closest_matches["claude-widget-9-1"] == {
        "priced_as": "claude-widget-9",
        "turns": 1,
        "tokens": 1_000,
    }
    assert len(table.notes) == 2
    assert table.notes[1] == (
        "claude-widget-9-1 looks like a newer version of claude-widget-9. It is priced at"
        " claude-widget-9's rate until pricing.toml has a row of its own for it."
    )


def test_coverage_does_not_record_closest_match_for_exact_hit(min_pricing):
    coverage = PricingCoverage()
    turn = _turn(model="claude-widget-9", input_tokens=1_000_000)
    resolved = min_pricing.resolve_model(turn.model)
    coverage.add(turn, price_turn(turn, resolved), resolved)
    assert coverage.closest_matches == {}
    assert coverage.closest_match_turns == 0
    assert coverage.as_closest_match_table().rows == []


def test_coverage_add_without_resolved_argument_still_works(min_pricing):
    # Existing callers that only need unknown-model/coverage_pct
    # accounting (report.py's other PricingCoverage.add call sites, and
    # every pre-existing test above) may omit `resolved` entirely.
    coverage = PricingCoverage()
    turn = _turn(model="claude-widget-9", input_tokens=1_000_000)
    resolved = min_pricing.resolve_model(turn.model)
    coverage.add(turn, price_turn(turn, resolved))
    assert coverage.priced_turns == 1
    assert coverage.closest_matches == {}
    assert coverage.fast_priced_as_standard == {}


def test_coverage_tracks_fast_priced_as_standard(min_pricing):
    coverage = PricingCoverage()
    turn = _turn(model="claude-gadget-2", input_tokens=1_000_000, speed="fast")
    resolved = min_pricing.resolve_model(turn.model)
    breakdown = price_turn(turn, resolved)
    assert breakdown.fast_applied is False
    coverage.add(turn, breakdown, resolved)

    assert coverage.fast_priced_as_standard_turns == 1
    assert coverage.fast_priced_as_standard["claude-gadget-2"] == {"turns": 1, "tokens": 1_000_000}
    table = coverage.as_fast_priced_as_standard_table()
    assert table.rows == [["claude-gadget-2", 1, 1_000_000]]
    assert table.notes


def test_coverage_does_not_record_fast_priced_as_standard_when_fast_applied(min_pricing):
    coverage = PricingCoverage()
    turn = _turn(model="claude-widget-9", input_tokens=1_000_000, speed="fast")
    resolved = min_pricing.resolve_model(turn.model)
    breakdown = price_turn(turn, resolved)
    assert breakdown.fast_applied is True
    coverage.add(turn, breakdown, resolved)
    assert coverage.fast_priced_as_standard == {}
    assert coverage.fast_priced_as_standard_turns == 0


def test_coverage_does_not_record_fast_priced_as_standard_for_standard_speed(min_pricing):
    coverage = PricingCoverage()
    turn = _turn(model="claude-gadget-2", input_tokens=1_000_000)  # speed is None
    resolved = min_pricing.resolve_model(turn.model)
    coverage.add(turn, price_turn(turn, resolved), resolved)
    assert coverage.fast_priced_as_standard == {}


# -- fast-applied tracking (PROF-08): the mirror image of the block above --


def test_coverage_tracks_fast_applied(min_pricing):
    coverage = PricingCoverage()
    turn = _turn(model="claude-widget-9", input_tokens=1_000_000, speed="fast")
    resolved = min_pricing.resolve_model(turn.model)
    breakdown = price_turn(turn, resolved)
    assert breakdown.fast_applied is True
    coverage.add(turn, breakdown, resolved)

    standard = price_turn(_turn(model="claude-widget-9", input_tokens=1_000_000), resolved)
    assert coverage.fast_applied_turns == 1
    entry = coverage.fast_applied["claude-widget-9"]
    assert entry["turns"] == 1 and entry["tokens"] == 1_000_000
    assert entry["cost"] == pytest.approx(breakdown.total)
    # The fast rate is exactly 2x standard on every rate component here,
    # so the standard-rate equivalent recovers the un-fast-priced total.
    assert entry["standard_cost"] == pytest.approx(standard.total)
    assert entry["cost"] == pytest.approx(entry["standard_cost"] * 2.0)

    table = coverage.as_fast_applied_table()
    assert table.rows == [["claude-widget-9", 1, 1_000_000, pytest.approx(breakdown.total), pytest.approx(standard.total)]]
    assert table.notes


def test_resolve_model_is_cached_per_rate_card(min_pricing):
    import dataclasses

    first = min_pricing.resolve_model("claude-widget-9")
    assert min_pricing.resolve_model("claude-widget-9") is first
    assert min_pricing.resolve_model("no-such-model") is None
    assert min_pricing.resolve_model(None) is None

    # A replaced rate card starts its own cache: its server-tool rate shows.
    priced = dataclasses.replace(min_pricing, server_tools={"web_search_per_1000": 8.0})
    assert priced.resolve_model("claude-widget-9").web_search_per_1000 == 8.0
    assert min_pricing.resolve_model("claude-widget-9").web_search_per_1000 == first.web_search_per_1000
    assert dataclasses.replace(min_pricing) == min_pricing  # a filled cache never counts in equality


def test_fast_applied_standard_cost_leaves_the_server_tool_fee_unscaled(min_pricing):
    import dataclasses

    priced = dataclasses.replace(min_pricing, server_tools={"web_search_per_1000": 8.0})
    resolved = priced.resolve_model("claude-widget-9")
    turn = _turn(model="claude-widget-9", input_tokens=1_000_000, speed="fast", web_search_requests=500)
    breakdown = price_turn(turn, resolved)
    assert breakdown.server_tool_cost == pytest.approx(4.0)  # 500 / 1,000 * $8, never doubled by fast

    coverage = PricingCoverage()
    coverage.add(turn, breakdown, resolved)
    entry = coverage.fast_applied["claude-widget-9"]
    # Only the token-rate portion is halved back to standard; the flat
    # per-request server-tool fee is added back unscaled on both sides.
    standard_token_cost = (breakdown.total - breakdown.server_tool_cost) / 2.0
    assert entry["standard_cost"] == pytest.approx(standard_token_cost + breakdown.server_tool_cost)


def test_coverage_does_not_record_fast_applied_when_no_fast_table(min_pricing):
    coverage = PricingCoverage()
    turn = _turn(model="claude-gadget-2", input_tokens=1_000_000, speed="fast")
    resolved = min_pricing.resolve_model(turn.model)
    breakdown = price_turn(turn, resolved)
    assert breakdown.fast_applied is False
    coverage.add(turn, breakdown, resolved)
    assert coverage.fast_applied == {}
    assert coverage.fast_applied_turns == 0
    assert coverage.as_fast_applied_table().rows == []


def test_coverage_does_not_record_fast_applied_for_standard_speed(min_pricing):
    coverage = PricingCoverage()
    turn = _turn(model="claude-widget-9", input_tokens=1_000_000)  # speed is None
    resolved = min_pricing.resolve_model(turn.model)
    coverage.add(turn, price_turn(turn, resolved), resolved)
    assert coverage.fast_applied == {}


def test_coverage_accumulates_fast_applied_across_turns(min_pricing):
    coverage = PricingCoverage()
    resolved = min_pricing.resolve_model("claude-widget-9")
    for _ in range(3):
        turn = _turn(model="claude-widget-9", input_tokens=1_000_000, speed="fast")
        coverage.add(turn, price_turn(turn, resolved), resolved)
    assert coverage.fast_applied_turns == 3
    assert coverage.fast_applied["claude-widget-9"]["turns"] == 3
    assert coverage.fast_applied["claude-widget-9"]["tokens"] == 3_000_000


# --------------------------------------------------------------------
# Pricing.describe()
# --------------------------------------------------------------------


def test_describe_lists_every_model_with_its_aliases(min_pricing):
    table = min_pricing.describe()
    ids = [row[0] for row in table.rows]
    assert ids == sorted(ids)
    assert "claude-widget-9" in ids
    widget_row = table.rows[ids.index("claude-widget-9")]
    aliases_cell = widget_row[1]
    assert "widget" in aliases_cell
    assert "widget[1m]" in aliases_cell


def test_describe_packaged_default_lists_every_required_model():
    pricing = load_pricing()
    table = pricing.describe()
    ids = {row[0] for row in table.rows}
    assert REQUIRED_PACKAGED_MODEL_IDS <= ids


# --------------------------------------------------------------------
# Dataclass sanity (matches the module's own contract, not model.py's)
# --------------------------------------------------------------------


def test_resolved_rates_wraps_model_rates(min_pricing):
    resolved = min_pricing.resolve_model("claude-widget-9")
    assert isinstance(resolved, ResolvedRates)
    assert isinstance(resolved.rates, ModelRates)
    assert resolved.rates.canonical_id == "claude-widget-9"


# --------------------------------------------------------------------
# effective_rates: the per-token rates price_turn charges a turn at
# --------------------------------------------------------------------


def _component_rates(turn_fields: dict, resolved, geo=...) -> tuple[float, ...]:
    """What price_turn charges one million tokens of each component."""
    kwargs = {} if geo is ... else {"geo": geo}
    per_million = []
    for tokens in (
        {"input_tokens": 1_000_000},
        {"output_tokens": 1_000_000},
        {"cache_creation_tokens": 1_000_000, "cc_5m": 1_000_000},
        {"cache_creation_tokens": 1_000_000, "cc_1h": 1_000_000},
        {"cache_read_tokens": 1_000_000},
    ):
        breakdown = price_turn(_turn(**turn_fields, **tokens), resolved, **kwargs)
        per_million.append(breakdown.total)
    return tuple(per_million)


@pytest.mark.parametrize(
    "turn_fields, geo",
    [
        ({"model": "claude-widget-9"}, ...),
        ({"model": "claude-widget-9", "speed": "fast"}, ...),
        ({"model": "claude-widget-9", "ctx": 150_000}, ...),
        ({"model": "claude-widget-9", "speed": "fast", "ctx": 150_000, "inference_geo": "us"}, ...),
        ({"model": "claude-widget-9", "inference_geo": "us"}, None),
        ({"model": "claude-widget-9"}, "us"),
        ({"model": "claude-gadget-2", "ctx": 60_000}, ...),
        ({"model": "claude-gadget-2", "speed": "fast"}, ...),
    ],
)
def test_effective_rates_match_what_price_turn_charges(min_pricing, turn_fields, geo):
    resolved = min_pricing.resolve_model(turn_fields["model"])
    kwargs = {} if geo is ... else {"geo": geo}
    rates = effective_rates(_turn(**turn_fields), resolved, **kwargs)
    expected = _component_rates(turn_fields, resolved, geo)
    got = (rates.input, rates.output, rates.cache_write_5m, rates.cache_write_1h, rates.cache_read)
    assert got == pytest.approx(expected)


def test_effective_rates_accept_bare_model_rates(min_pricing):
    resolved = min_pricing.resolve_model("claude-widget-9")
    turn = _turn(model="claude-widget-9", speed="fast")
    assert effective_rates(turn, resolved.rates) == effective_rates(turn, resolved)
    assert effective_rates(turn, resolved).output == pytest.approx(4.0)


def test_effective_rates_none_for_an_unresolved_model():
    assert effective_rates(_turn(), None) is None


def test_model_name_reads_an_id_as_people_say_it():
    from claudeglass.pricing import model_name, model_names_in

    assert model_name("claude-opus-5-5") == "Opus 5.5"
    assert model_name("claude-haiku-4-5-20251001") == "Haiku 4.5"
    assert model_name("claude-3-5-haiku-20241022") == "Haiku 3.5"
    assert model_name("claude-opus-5[1m]") == "Opus 5 [1m]"
    assert model_name("gpt-4") == "gpt-4"
    assert model_names_in("claude-sonnet-5 (+2 more), not claudeglass") == "Sonnet 5 (+2 more), not claudeglass"

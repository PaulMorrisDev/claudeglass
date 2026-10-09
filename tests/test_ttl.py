"""Tests for WP4: TTL break-even simulation (``src/claudeglass/ttl.py``).

Most branches are exercised on hand-built ``model.Turn`` instances (the
``_turn`` helper below, matching ``test_pricing.py``'s convention) with
hand-computed costs at the packaged Sonnet 5 rates (input 2.0, output
10.0, cache_write_5m 2.5, cache_write_1h 4.0, cache_read 0.2 per million
tokens). A couple of integration tests go through the real parser
(``parse_transcript`` + ``tests/helpers.py``'s JSONL builders) to also
exercise ``helpers.assert_privacy`` and ``TtlStats.add``'s agent-type
keying on a genuine ``TranscriptResult``.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from claudeglass import model, recache
from claudeglass.model import TranscriptMeta, TranscriptResult
from claudeglass.parse import parse_transcript
from claudeglass.pricing import load_pricing, price_turn
from claudeglass.ttl import (
    ASSUMPTIONS,
    POLICY_1H,
    POLICY_5M,
    TtlStats,
    TtlThresholds,
    TtlTypeStats,
    build_section,
    cache_economy,
    dominant_ttl,
    fidelity,
    normalize_ttl_split,
    observed,
    simulate,
)

from helpers import assert_privacy, turn_line, write_jsonl

PRICING = load_pricing()
SONNET_RATES = PRICING.resolve_model("claude-sonnet-5")
HAIKU_RATES = PRICING.resolve_model("claude-haiku-4-5-20251001")


def _turn(**overrides) -> model.Turn:
    """Build a priced ``Turn`` with sane zero defaults, overridable per
    field. ``turn_index`` defaults to 1 (priced); pass ``turn_index=0``
    to build a synthetic/usage-less turn for the skip tests."""
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
        gap_s=None,
    )
    fields.update(overrides)
    return model.Turn(**fields)


def _stats(
    cost_observed: float,
    cost_all_5m: float,
    cost_all_1h: float,
    key: str = "claude-implementer",
    observed_5m_pct: float = 100.0,
    observed_1h_pct: float = 0.0,
    fidelity_pct: float | None = 0.0,
) -> TtlTypeStats:
    """A ``TtlTypeStats`` with only the cost fields the recommendation
    threshold tests care about set to something meaningful.
    ``observed_5m_pct``/``observed_1h_pct``/``fidelity_pct`` default to
    values that never trip R2's dominant-policy or fidelity-gate
    suppression (100% observed 5m, 0% fidelity), so existing tests that
    don't care about those checks keep working unchanged; pass explicit
    values to exercise them."""
    return TtlTypeStats(
        key=key,
        spawns=1,
        priced_turns=1,
        observed_5m_pct=observed_5m_pct,
        observed_1h_pct=observed_1h_pct,
        gaps_over_5m=0,
        gaps_over_1h=0,
        gap_p50_s=None,
        gap_p90_s=None,
        cost_observed=cost_observed,
        cost_all_5m=cost_all_5m,
        cost_all_1h=cost_all_1h,
        unsimulatable=0,
        fidelity_pct=fidelity_pct,
        gap_buckets={},
    )


# --------------------------------------------------------------------
# Assumptions
# --------------------------------------------------------------------


def test_assumptions_include_gap_definition_sentence():
    assert "gap is measured from the start of one request to the start of the next" in ASSUMPTIONS


def test_assumptions_cover_every_a4_rule():
    joined = " ".join(ASSUMPTIONS)
    for fragment in (
        "TTL-invariant",
        "cache_read_i + cache_creation_i",
        "refreshes TTL",
        "prefix-invalidated",
        "cache_read rate",
        "clamps write at 0",
    ):
        assert fragment in joined


def test_assumptions_keep_a_read_only_expiry_s_split_only_under_a_policy_as_long_as_its_wait():
    """``simulate`` keeps that split while ``gap_s <= policy_s``; a shorter
    policy lets the clock expire it."""
    (line,) = (a for a in ASSUMPTIONS if "shared start" in a)
    assert line.endswith("keeps it under any policy at least as long as its wait")
    assert "no cache lifetime would have prevented it" not in line


def test_the_concepts_doc_prints_the_assumptions_as_they_are():
    doc = (Path(__file__).parent.parent / "docs" / "concepts.md").read_text(encoding="utf-8")
    text = " ".join(doc.replace("`", "").split())
    for line in ASSUMPTIONS:
        assert " ".join(line.split()) in text, line


# --------------------------------------------------------------------
# simulate(): each Appendix A4 branch
# --------------------------------------------------------------------


def test_first_turn_always_writes_full_prefix_no_read():
    t = _turn(cache_creation_tokens=1000, cc_5m=1000, cache_read_tokens=0, gap_s=None)
    result = simulate([t], SONNET_RATES, POLICY_5M)
    assert result.write_tokens == 1000
    assert result.read_tokens == 0
    assert result.turns == 1
    assert result.unsimulatable == 0
    assert result.cost == pytest.approx(1000 / 1_000_000 * 2.5)


def test_gap_within_policy_reads_min_c_writes_remainder():
    t1 = _turn(cache_creation_tokens=1000, cc_5m=1000, cache_read_tokens=0)
    # C2 = cache_read(0) + cache_creation(1500) = 1500; prev_C = 1000
    # -> read = min(1500, 1000) = 1000, write = 1500 - 1000 = 500
    t2 = _turn(turn_index=2, message_id="msg_2", cache_creation_tokens=1500, cc_5m=1500, cache_read_tokens=0, gap_s=100)
    result = simulate([t1, t2], SONNET_RATES, POLICY_5M)
    assert result.write_tokens == 1000 + 500
    assert result.read_tokens == 0 + 1000
    expected_cost = (1000 / 1e6 * 2.5) + (500 / 1e6 * 2.5 + 1000 / 1e6 * 0.2)
    assert result.cost == pytest.approx(expected_cost)


def test_gap_beyond_policy_forces_full_rewrite():
    t1 = _turn(cache_creation_tokens=1000, cc_5m=1000)
    t2 = _turn(turn_index=2, message_id="msg_2", cache_creation_tokens=2000, cc_5m=2000, cache_read_tokens=0, gap_s=400)
    result = simulate([t1, t2], SONNET_RATES, POLICY_5M)  # 400 s > 300 s policy
    assert result.write_tokens == 1000 + 2000
    assert result.read_tokens == 0


def test_prefix_invalidated_turn_keeps_observed_split_regardless_of_gap():
    t1 = _turn(cache_creation_tokens=1000, cc_5m=1000)
    # gap_s=50 is well inside the 5m policy, so without the signature this
    # would take the "read = min(C, prev_C)" branch instead.
    t2 = _turn(turn_index=2, message_id="msg_2", cache_creation_tokens=300, cc_5m=300, cache_read_tokens=700, gap_s=50)
    t2 = dataclasses.replace(t2, recache_signature="prefix-invalidated")
    result = simulate([t1, t2], SONNET_RATES, POLICY_5M)
    assert result.write_tokens == 1000 + 300
    assert result.read_tokens == 0 + 700


def test_prefix_invalidated_falls_back_to_minimal_rule_with_and_without_apply():
    """Fix item 1: a prefix-invalidated turn (ctx 100k, cache_read 5k,
    gap 10s) keeps its observed split under both the 5m and 1h policies,
    whether or not ``recache.apply`` has run first — the fallback
    classification inside ``simulate`` (see ``_recache_classification``)
    makes the branch reachable even when nothing upstream ever called
    the RE-CACHE detector, and ``apply``'s real signature agrees with it.
    """
    t1 = _turn(cache_creation_tokens=50_000, cc_5m=50_000, cache_read_tokens=0)
    # C2 = 5_000 + 95_000 = 100_000. ctx (100_000) > ctx_floor (20_000);
    # cache_read (5_000) < cr_ratio * ctx (20_000); cache_read (5_000) >=
    # full_expiry_cr (2_000) -> "prefix-invalidated". gap_s=10 is well
    # inside both the 5m and 1h policy windows, so without the fallback
    # this would take the "read = min(C, prev_C)" gap branch instead.
    t2 = _turn(
        turn_index=2,
        message_id="msg_2",
        ctx=100_000,
        cache_read_tokens=5_000,
        cache_creation_tokens=95_000,
        cc_5m=95_000,
        gap_s=10,
    )
    assert t2.recache_signature is None  # never classified by anything upstream

    for policy in (POLICY_5M, POLICY_1H):
        result = simulate([t1, t2], SONNET_RATES, policy)
        assert result.write_tokens == 50_000 + 95_000
        assert result.read_tokens == 0 + 5_000

    # Same outcome once recache.apply has actually stamped the signature
    # onto the turn (the ordinary pipeline path).
    transcript = TranscriptResult(meta=TranscriptMeta(), turns=[t1, t2])
    updated = recache.apply(transcript, recache.RecacheThresholds())
    assert updated.turns[1].recache_signature == "prefix-invalidated"
    for policy in (POLICY_5M, POLICY_1H):
        result = simulate(updated.turns, SONNET_RATES, policy)
        assert result.write_tokens == 50_000 + 95_000
        assert result.read_tokens == 0 + 5_000


def _cold_start_pair(gap_s: float, first_write: str = "cc_5m", **second):
    """A first call that read a 15k shared start (``cr0``) and wrote 85k
    more, then a call ``gap_s`` later that read only the 15k again and
    wrote 85k: the session part was lost. ``first_write`` picks the TTL the
    first call wrote at, which sets how long its entry lasted."""
    t1 = _turn(
        ctx=100_000,
        cache_read_tokens=15_000,
        cache_creation_tokens=85_000,
        **{first_write: 85_000},
    )
    fields = dict(
        turn_index=2,
        message_id="msg_2",
        ctx=100_000,
        cache_read_tokens=15_000,
        cache_creation_tokens=85_000,
        cc_5m=85_000,
        gap_s=gap_s,
    )
    fields.update(second)
    return t1, _turn(**fields)


def _best_cost(turns: list[model.Turn]) -> float:
    return min(simulate(turns, SONNET_RATES, p).cost for p in (POLICY_5M, POLICY_1H))


def _applied(turns: list[model.Turn]) -> list[model.Turn]:
    transcript = TranscriptResult(meta=TranscriptMeta(), turns=turns)
    return recache.apply(transcript, recache.RecacheThresholds()).turns


def test_read_only_expiry_costs_the_same_raw_or_through_recache_apply():
    """A reply 60 s later that read only the shared start is labelled
    "full-expiry" by ``recache.detect`` (rule 4: its read alone), though the
    5-minute entry was still alive. No cache lifetime could have saved it,
    so the simulation must price it as it does the same turn unlabelled:
    keeping its observed split, with no phantom saving."""
    raw = list(_cold_start_pair(60))
    applied = _applied(raw)
    assert applied[1].recache_signature == "full-expiry"
    assert raw[1].recache_signature is None

    for policy in (POLICY_5M, POLICY_1H):
        sim_raw = simulate(raw, SONNET_RATES, policy)
        sim_applied = simulate(applied, SONNET_RATES, policy)
        assert sim_applied.cost == pytest.approx(sim_raw.cost)
        assert sim_applied.write_tokens == 100_000 + 85_000
        assert sim_applied.read_tokens == 15_000
    delta_raw = observed(raw, SONNET_RATES).cost - _best_cost(raw)
    delta_applied = observed(applied, SONNET_RATES).cost - _best_cost(applied)
    assert delta_applied == pytest.approx(delta_raw)


def test_read_only_expiry_is_content_addressable_in_the_ttl_table():
    raw = list(_cold_start_pair(60))
    rows = {}
    for name, turns in (("raw", raw), ("applied", _applied(raw))):
        row = _row_for(turns)
        rows[name] = row
        assert row.addressable_full_expiry_tokens == 0
        assert row.addressable_full_expiry_usd == 0.0
        assert row.addressable_prefix_invalidated_tokens == 85_000
    assert rows["applied"].cost_observed == pytest.approx(rows["raw"].cost_observed)
    for field in ("cost_all_5m", "cost_all_1h"):
        assert getattr(rows["applied"], field) == pytest.approx(getattr(rows["raw"], field))
    saving = lambda r: r.cost_observed - min(r.cost_all_5m, r.cost_all_1h)  # noqa: E731
    assert saving(rows["applied"]) == pytest.approx(saving(rows["raw"]))


def test_timed_expiry_still_simulates_as_a_lifetime_miss():
    """The control: the same lost session part after a 400 s wait outlasted
    the 5-minute entry, so a 1-hour lifetime would have kept it."""
    applied = _applied(list(_cold_start_pair(400)))
    assert applied[1].recache_signature == "full-expiry"

    sim_5m = simulate(applied, SONNET_RATES, POLICY_5M)
    sim_1h = simulate(applied, SONNET_RATES, POLICY_1H)
    assert sim_5m.write_tokens == 100_000 + 100_000  # expired under 5m: whole prefix again
    assert sim_1h.write_tokens == 100_000 + 0  # alive under 1h: all read
    assert sim_1h.cost < sim_5m.cost

    row = _row_for(applied)
    assert row.addressable_full_expiry_tokens == 85_000
    assert row.addressable_prefix_invalidated_tokens == 0


def test_read_only_expiry_inside_a_one_hour_entry_still_expires_under_a_five_minute_policy():
    """The first call wrote at 1 hour, so a 400 s wait is inside its
    lifetime and the loss is not the clock's. Under a 5-minute policy the
    entry would have expired by then, so that policy takes the full
    rewrite; the 1-hour policy keeps the observed split."""
    applied = _applied(list(_cold_start_pair(400, first_write="cc_1h")))
    assert applied[1].recache_signature == "full-expiry"

    assert simulate(applied, SONNET_RATES, POLICY_1H).write_tokens == 100_000 + 85_000
    assert simulate(applied, SONNET_RATES, POLICY_5M).write_tokens == 100_000 + 100_000

    row = _row_for(applied)
    assert row.addressable_full_expiry_tokens == 0
    assert row.addressable_prefix_invalidated_tokens == 85_000


def test_expiry_with_the_lifetime_unknown_is_not_called_read_only():
    """When the wait is not recorded nothing shows the clock did not do it:
    the turn keeps its observed split as before (an unknown gap), and is
    still counted as expired."""
    t1, t2 = _cold_start_pair(None)
    t2 = dataclasses.replace(t2, recache_signature="full-expiry")
    result = simulate([t1, t2], SONNET_RATES, POLICY_5M)
    assert result.unsimulatable == 1
    assert _row_for([t1, t2]).addressable_full_expiry_tokens == 85_000


@pytest.mark.parametrize("gap_s", [60, 4000])
def test_post_compaction_rewrite_costs_the_same_raw_or_through_recache_apply(gap_s):
    """The summary has to be written under any lifetime, so the first reply
    after a compaction keeps its observed split whatever the wait: without
    that the simulation prices the rewrite of a shrunk context as a read."""
    t1 = _turn(ctx=200_000, cache_read_tokens=15_000, cache_creation_tokens=185_000, cc_5m=185_000)
    t2 = _turn(
        turn_index=2,
        message_id="msg_2",
        ctx=100_000,
        cache_read_tokens=15_000,
        cache_creation_tokens=85_000,
        cc_5m=85_000,
        gap_s=gap_s,
        preceding_event_kinds=(model.EventKind.COMPACT_BOUNDARY,),
    )
    raw = [t1, t2]
    applied = _applied(raw)
    assert applied[1].recache_signature == "post-compaction"

    for policy in (POLICY_5M, POLICY_1H):
        sim_raw = simulate(raw, SONNET_RATES, policy)
        sim_applied = simulate(applied, SONNET_RATES, policy)
        assert sim_applied.cost == pytest.approx(sim_raw.cost)
        assert sim_applied.write_tokens == 200_000 + 85_000
        assert sim_applied.read_tokens == 15_000
    delta_raw = observed(raw, SONNET_RATES).cost - _best_cost(raw)
    delta_applied = observed(applied, SONNET_RATES).cost - _best_cost(applied)
    assert delta_applied == pytest.approx(delta_raw)
    assert _row_for(applied).addressable_full_expiry_tokens == 0


def test_unknown_gap_carries_observed_split_and_counts_unsimulatable():
    t1 = _turn(cache_creation_tokens=1000, cc_5m=1000)
    t2 = _turn(turn_index=2, message_id="msg_2", cache_creation_tokens=400, cc_5m=400, cache_read_tokens=600, gap_s=None)
    result = simulate([t1, t2], SONNET_RATES, POLICY_5M)
    assert result.unsimulatable == 1
    assert result.write_tokens == 1000 + 400
    assert result.read_tokens == 0 + 600


def test_compaction_shrink_clamps_write_at_zero():
    t1 = _turn(cache_creation_tokens=200_000, cc_1h=200_000, gap_s=None)
    # C2 = cache_read(50_000) + cache_creation(0) = 50_000; prev_C = 200_000
    # -> read = min(50_000, 200_000) = 50_000, write = max(0, 50_000 - 50_000) = 0
    t2 = _turn(
        turn_index=2,
        message_id="msg_2",
        cache_creation_tokens=0,
        cc_5m=0,
        cc_1h=0,
        cache_read_tokens=50_000,
        gap_s=100,
    )
    result = simulate([t1, t2], SONNET_RATES, POLICY_1H)
    assert result.write_tokens == 200_000 + 0
    assert result.read_tokens == 0 + 50_000


def test_synthetic_and_usageless_turns_are_skipped_in_simulate_and_observed():
    priced = _turn(cache_creation_tokens=1000, cc_5m=1000)
    synthetic = _turn(turn_index=0, is_synthetic=True, message_id="msg_synth")
    usageless = _turn(turn_index=0, message_id="msg_missing")
    turns = [priced, synthetic, usageless]

    sim_result = simulate(turns, SONNET_RATES, POLICY_5M)
    assert sim_result.turns == 1
    assert sim_result.write_tokens == 1000

    obs_result = observed(turns, SONNET_RATES)
    assert obs_result.turns == 1
    assert obs_result.write_tokens == 1000


# --------------------------------------------------------------------
# observed() == price_turn's default path
# --------------------------------------------------------------------


def test_observed_equals_price_turn_default_path_for_three_turns():
    turns = [
        _turn(input_tokens=100, output_tokens=20, cache_creation_tokens=1000, cc_5m=1000, cache_read_tokens=0),
        _turn(
            turn_index=2,
            message_id="msg_2",
            input_tokens=80,
            output_tokens=15,
            cache_creation_tokens=500,
            cc_5m=500,
            cache_read_tokens=1000,
            gap_s=90,
        ),
        _turn(
            turn_index=3,
            message_id="msg_3",
            input_tokens=60,
            output_tokens=10,
            cache_creation_tokens=0,
            cache_read_tokens=1500,
            gap_s=90,
        ),
    ]
    result = observed(turns, SONNET_RATES)
    expected_total = sum(price_turn(t, SONNET_RATES).total for t in turns)
    assert result.cost == pytest.approx(expected_total)
    assert result.write_tokens == 1000 + 500 + 0
    assert result.read_tokens == 0 + 1000 + 1500


# --------------------------------------------------------------------
# Fix item 2: per-turn rates lookup (mixed-model transcripts)
# --------------------------------------------------------------------


def _two_model_turns() -> list[model.Turn]:
    """One Sonnet-5 turn and one Haiku-4.5 turn, hand-computable at
    each model's own packaged rates (Sonnet: input 2.0, output 10.0,
    cache_write_5m 2.5, cache_read 0.2; Haiku: input 1.0, output 5.0,
    cache_write_5m 1.25, cache_read 0.1 — all per million tokens)."""
    return [
        _turn(
            model="claude-sonnet-5",
            input_tokens=100,
            output_tokens=50,
            cache_creation_tokens=1000,
            cc_5m=1000,
            cache_read_tokens=0,
        ),
        _turn(
            turn_index=2,
            message_id="msg_2",
            model="claude-haiku-4-5-20251001",
            input_tokens=200,
            output_tokens=80,
            cache_creation_tokens=2000,
            cc_5m=2000,
            cache_read_tokens=500,
            gap_s=90,
        ),
    ]


def test_observed_prices_each_turn_at_its_own_models_rate():
    turns = _two_model_turns()
    result = observed(turns, PRICING.resolve_model)

    sonnet_cost = 100 / 1e6 * 2.0 + 50 / 1e6 * 10.0 + 1000 / 1e6 * 2.5 + 0 / 1e6 * 0.2
    haiku_cost = 200 / 1e6 * 1.0 + 80 / 1e6 * 5.0 + 2000 / 1e6 * 1.25 + 500 / 1e6 * 0.1
    expected = sonnet_cost + haiku_cost
    assert result.cost == pytest.approx(expected)
    assert result.unpriced_turns == 0

    # Mispricing every turn at a single model's rate gives a different
    # (wrong) total — this is exactly the bug the per-turn lookup fixes.
    single_model_total = sum(price_turn(t, SONNET_RATES).total for t in turns)
    assert result.cost != pytest.approx(single_model_total)


def test_observed_accepts_single_rate_compatibility_path():
    """A bare ResolvedRates (not a callable) is still accepted, applied
    to every turn regardless of its own model — the pre-item-2 shape
    every other test in this module still uses."""
    turns = _two_model_turns()
    result = observed(turns, SONNET_RATES)
    expected = sum(price_turn(t, SONNET_RATES).total for t in turns)
    assert result.cost == pytest.approx(expected)


def test_observed_counts_unpriced_turns_for_unresolvable_model():
    turns = _two_model_turns()
    turns[1] = dataclasses.replace(turns[1], model="claude-unreleased-9000")
    result = observed(turns, PRICING.resolve_model)
    assert result.unpriced_turns == 1
    sonnet_cost = 100 / 1e6 * 2.0 + 50 / 1e6 * 10.0 + 1000 / 1e6 * 2.5
    assert result.cost == pytest.approx(sonnet_cost)


def test_ttl_stats_two_model_transcript_matches_hand_computed_observed_cost():
    turns = _two_model_turns()
    result = TranscriptResult(meta=TranscriptMeta(kind="top-level"), turns=turns)
    stats = TtlStats()
    stats.add(result, PRICING.resolve_model)
    row = stats.by_key()["top-level"]

    sonnet_cost = 100 / 1e6 * 2.0 + 50 / 1e6 * 10.0 + 1000 / 1e6 * 2.5
    haiku_cost = 200 / 1e6 * 1.0 + 80 / 1e6 * 5.0 + 2000 / 1e6 * 1.25 + 500 / 1e6 * 0.1
    assert row.cost_observed == pytest.approx(sonnet_cost + haiku_cost)
    assert row.unpriced_turns == 0
    # Dominant model by total token volume is Haiku (turn 2 carries more
    # tokens), so premium_ratio must be Haiku's, not Sonnet's.
    expected_premium_ratio = (HAIKU_RATES.rates.cache_write_1h - HAIKU_RATES.rates.cache_write_5m) / HAIKU_RATES.rates.cache_write_5m
    assert row.premium_ratio == pytest.approx(expected_premium_ratio)


# --------------------------------------------------------------------
# dominant_ttl()
# --------------------------------------------------------------------


def test_dominant_ttl_all_5m():
    assert dominant_ttl([_turn(cc_5m=1000, cc_1h=0)]) == "5m"


def test_dominant_ttl_all_1h():
    assert dominant_ttl([_turn(cc_5m=0, cc_1h=1000)]) == "1h"


def test_dominant_ttl_boundary_ninety_percent_counts_as_dominant():
    assert dominant_ttl([_turn(cc_5m=900, cc_1h=100)]) == "5m"


def test_dominant_ttl_mixed_below_threshold():
    assert dominant_ttl([_turn(cc_5m=600, cc_1h=400)]) == "mixed"


def test_dominant_ttl_none_when_no_cache_write_tokens():
    assert dominant_ttl([_turn(cc_5m=0, cc_1h=0)]) == "none"


# --------------------------------------------------------------------
# normalize_ttl_split() (coordinator follow-up, WP12a diversity fixtures)
# --------------------------------------------------------------------


def test_normalize_ttl_split_attributes_pre_split_turn_to_observed_dominant():
    # t1 has a real, observed 1h-dominant split; t2 is pre-split (no
    # nested cache_creation object was ever seen for it) with a nonzero
    # flat cache_creation_tokens - it must be attributed to the
    # transcript's own dominant TTL ("1h" here), not silently zeroed.
    t1 = _turn(message_id="m1", cache_creation_tokens=9_000, cc_5m=0, cc_1h=9_000)
    t2 = _turn(
        message_id="m2",
        turn_index=2,
        cache_creation_tokens=1_000,
        cc_5m=0,
        cc_1h=0,
        ttl_split_unknown=True,
    )
    normalized = normalize_ttl_split([t1, t2])
    assert normalized[0] is t1  # already-split turn passes through unchanged
    assert normalized[1].cc_5m == 0
    assert normalized[1].cc_1h == 1_000
    assert normalized[1].cache_creation_tokens == 1_000
    # Purity: the input turn itself is never mutated.
    assert t2.cc_1h == 0


def test_normalize_ttl_split_falls_back_to_5m_when_no_dominant():
    # Only pre-split turns in this transcript: dominant_ttl sees zero
    # real cc_5m/cc_1h tokens anywhere (both are 0 on every turn), so it
    # returns "none" - normalize_ttl_split must fall back to 5m rather
    # than leaving the write unattributed.
    t1 = _turn(message_id="m1", cache_creation_tokens=2_000, cc_5m=0, cc_1h=0, ttl_split_unknown=True)
    normalized = normalize_ttl_split([t1])
    assert normalized[0].cc_5m == 2_000
    assert normalized[0].cc_1h == 0


def test_normalize_ttl_split_leaves_zero_cache_creation_turn_alone():
    t1 = _turn(message_id="m1", cache_creation_tokens=0, cc_5m=0, cc_1h=0, ttl_split_unknown=True)
    normalized = normalize_ttl_split([t1])
    assert normalized[0] is t1


def test_observed_prices_pre_split_turn_at_the_dominant_ttl_rate():
    # Without normalization, observed()'s default price_turn path would
    # price this pre-split write at zero (cc_5m == cc_1h == 0) despite a
    # real, nonzero cache_creation_tokens - undercounting real spend.
    t1 = _turn(message_id="m1", cache_creation_tokens=8_000, cc_5m=8_000, cc_1h=0)
    t2 = _turn(
        message_id="m2",
        turn_index=2,
        cache_creation_tokens=2_000,
        cc_5m=0,
        cc_1h=0,
        ttl_split_unknown=True,
    )
    result = observed([t1, t2], SONNET_RATES)
    # write_tokens must include t2's cache_creation_tokens once
    # attributed to the dominant (5m) side, not silently drop it.
    assert result.write_tokens == 8_000 + 2_000
    expected_cost = price_turn(t1, SONNET_RATES).total + price_turn(
        dataclasses.replace(t2, cc_5m=2_000), SONNET_RATES
    ).total
    assert result.cost == pytest.approx(expected_cost)


# --------------------------------------------------------------------
# fidelity()
# --------------------------------------------------------------------


def test_fidelity_near_zero_when_observed_matches_ideal_5m_policy():
    # Every turn's actual observed split is exactly what a 5m-policy
    # simulation would have produced, so sim(5m) == observed exactly.
    t1 = _turn(cache_creation_tokens=1000, cc_5m=1000, cache_read_tokens=0, gap_s=None)
    t2 = _turn(turn_index=2, message_id="msg_2", cache_creation_tokens=500, cc_5m=500, cache_read_tokens=1000, gap_s=100)
    t3 = _turn(turn_index=3, message_id="msg_3", cache_creation_tokens=500, cc_5m=500, cache_read_tokens=1500, gap_s=100)
    turns = [t1, t2, t3]

    assert dominant_ttl(turns) == "5m"
    fid = fidelity(turns, SONNET_RATES)
    assert fid is not None
    assert fid < 0.01


def test_fidelity_none_when_dominant_is_mixed():
    turns = [_turn(cc_5m=600, cc_1h=400)]
    assert dominant_ttl(turns) == "mixed"
    assert fidelity(turns, SONNET_RATES) is None


def test_fidelity_none_when_observed_cost_is_zero():
    turns = [_turn(cc_5m=0, cc_1h=0, input_tokens=0, output_tokens=0, cache_read_tokens=0)]
    # No cache-write tokens at all: dominant_ttl is "none".
    assert fidelity(turns, SONNET_RATES) is None


# --------------------------------------------------------------------
# Recommendation thresholds (both independently blocking)
# --------------------------------------------------------------------


def test_recommendation_blocked_by_usd_threshold_despite_large_pct_saving():
    # 9.40 vs 10.00: 6% cheaper (passes the 5% bar) but only $0.60 saved.
    s = _stats(cost_observed=10.0, cost_all_5m=10.0, cost_all_1h=9.40)
    assert s.recommendation() == "no material difference"


def test_recommendation_blocked_by_pct_threshold_despite_large_usd_saving():
    # 960 vs 1000: $40 saved but only 4% cheaper (fails the 5% bar).
    s = _stats(cost_observed=1000.0, cost_all_5m=1000.0, cost_all_1h=960.0)
    assert s.recommendation() == "no material difference"


def test_recommendation_switches_to_1h_when_both_thresholds_clear():
    s = _stats(cost_observed=100.0, cost_all_5m=100.0, cost_all_1h=90.0)
    assert s.recommendation() == "switch to 1h"


def test_recommendation_switches_to_5m_symmetrically():
    # Observed traffic is dominated by 1h (not 5m, the _stats default),
    # so switching to the cheaper 5m policy is a genuine cross-policy
    # switch rather than "keep 1h (already dominant)".
    s = _stats(
        cost_observed=100.0, cost_all_5m=90.0, cost_all_1h=100.0, observed_5m_pct=0.0, observed_1h_pct=100.0
    )
    assert s.recommendation() == "switch to 5m"


def test_recommendation_no_material_difference_when_observed_cost_zero():
    s = _stats(cost_observed=0.0, cost_all_5m=0.0, cost_all_1h=0.0)
    assert s.recommendation() == "no material difference"


# -- R1: recommendation must pick the cheaper policy, not whichever is
# checked first -----------------------------------------------------------


def test_recommendation_picks_cheaper_policy_not_first_checked_order():
    # cost_all_1h (91.0) clears both switch thresholds on its own
    # (cheaper than 95% of observed, saves > $1), but cost_all_5m (80.0)
    # is cheaper still. The old implementation checked 1h before 5m and
    # returned on the first policy to clear the bar, so it would wrongly
    # recommend "switch to 1h" here instead of the actually-cheaper 5m.
    s = _stats(
        cost_observed=100.0,
        cost_all_5m=80.0,
        cost_all_1h=91.0,
        observed_5m_pct=0.0,
        observed_1h_pct=100.0,
    )
    assert s.best_policy == "5m"
    assert s.recommendation() == "switch to 5m"


# -- R2: never advise switching to the policy already dominant, and gate
# on simulation fidelity ---------------------------------------------------


def test_recommendation_suppressed_when_best_policy_already_dominant():
    # Best policy is 5m, and observed traffic is already ~100% 5m: no
    # genuine switch is available, so this reads back as "keep", not a
    # switch recommendation.
    s = _stats(
        cost_observed=100.0,
        cost_all_5m=90.0,
        cost_all_1h=200.0,
        observed_5m_pct=100.0,
        observed_1h_pct=0.0,
    )
    assert s.recommendation() == "keep 5m (already dominant)"


def test_recommendation_suppressed_when_fidelity_exceeds_max_for_advice():
    # Best policy (1h) differs from the dominant observed policy (5m),
    # both switch thresholds clear, but the simulation's own fidelity is
    # too poor to trust: nothing should be advised.
    s = _stats(
        cost_observed=100.0,
        cost_all_5m=100.0,
        cost_all_1h=90.0,
        observed_5m_pct=100.0,
        observed_1h_pct=0.0,
        fidelity_pct=50.0,
    )
    rec = s.recommendation()
    assert rec.startswith("no material difference (suppressed:")
    assert "fidelity" in rec


def test_recommendation_suppressed_when_saving_does_not_clear_fidelity_margin():
    # A looser switch_pct lets a small (2%) saving clear the switch
    # gate, but that saving doesn't exceed this row's own 3% simulation
    # fidelity -- too close to the simulation's own noise to act on.
    th = TtlThresholds(switch_pct=0.99, switch_usd=0.5)
    s = _stats(
        cost_observed=100.0,
        cost_all_5m=100.0,
        cost_all_1h=98.0,
        observed_5m_pct=100.0,
        observed_1h_pct=0.0,
        fidelity_pct=3.0,
    )
    assert s.delta_pct == pytest.approx(2.0)
    rec = s.recommendation(th)
    assert rec == "no material difference (suppressed: 2.0% saving does not exceed 3.0% simulation fidelity)"


def test_lever_text_top_level_vs_subagent():
    top = _stats(10.0, 10.0, 9.0, key="top-level")
    sub = _stats(10.0, 10.0, 9.0, key="claude-implementer")
    assert top.lever == "promptCacheTtl"
    assert sub.lever == "experimental.cacheTtl in claude-implementer.md (or subagentPromptCacheTtl for all subagents)"


def test_lever_text_for_subagents_with_no_recorded_type_is_the_setting():
    """A subagent whose type wasn't recorded has no agent file to edit,
    so the only lever that reaches it is subagentPromptCacheTtl."""
    assert _stats(10.0, 10.0, 9.0, key="unknown").lever == "subagentPromptCacheTtl"


# --------------------------------------------------------------------
# TtlStats: agent-type keying, gap buckets, p50/p90
# --------------------------------------------------------------------


def test_ttl_stats_gap_buckets_and_percentiles():
    turns = [
        _turn(cache_creation_tokens=100, cc_5m=100, gap_s=None),
        _turn(turn_index=2, message_id="m2", cache_creation_tokens=100, cc_5m=100, gap_s=30),  # <1m
        _turn(turn_index=3, message_id="m3", cache_creation_tokens=100, cc_5m=100, gap_s=120),  # 1-5m
        _turn(turn_index=4, message_id="m4", cache_creation_tokens=100, cc_5m=100, gap_s=600),  # 5-15m
        _turn(turn_index=5, message_id="m5", cache_creation_tokens=100, cc_5m=100, gap_s=1800),  # 15-60m
        _turn(turn_index=6, message_id="m6", cache_creation_tokens=100, cc_5m=100, gap_s=5000),  # >60m
    ]
    stats = TtlStats()
    stats.add(TranscriptResult(meta=TranscriptMeta(kind="top-level"), turns=turns), SONNET_RATES)

    row = stats.by_key()["top-level"]
    assert row.spawns == 1
    assert row.priced_turns == 6
    assert row.gap_buckets == {"lt_1m": 1, "1_5m": 1, "5_15m": 1, "15_60m": 1, "gt_60m": 1}
    assert row.gaps_over_5m == 3  # 600, 1800, 5000
    assert row.gaps_over_1h == 1  # 5000
    assert row.gap_p50_s == 600
    assert row.gap_p90_s == 5000


def test_ttl_stats_keys_by_top_level_vs_agent_type():
    top_turns = [_turn(cache_creation_tokens=1000, cc_5m=1000)]
    sub_turns = [_turn(cache_creation_tokens=1000, cc_1h=1000)]
    stats = TtlStats()
    stats.add(TranscriptResult(meta=TranscriptMeta(kind="top-level"), turns=top_turns), SONNET_RATES)
    stats.add(
        TranscriptResult(meta=TranscriptMeta(kind="subagent", agent_type="claude-implementer"), turns=sub_turns),
        SONNET_RATES,
    )
    stats.add(
        TranscriptResult(meta=TranscriptMeta(kind="subagent", agent_type=None), turns=sub_turns),
        SONNET_RATES,
    )

    by_key = stats.by_key()
    assert set(by_key) == {"top-level", "claude-implementer", "unknown"}
    assert by_key["top-level"].spawns == 1
    assert by_key["claude-implementer"].spawns == 1


def test_ttl_stats_accumulates_spawns_and_priced_turns_across_multiple_adds():
    turns = [_turn(cache_creation_tokens=1000, cc_5m=1000)]
    stats = TtlStats()
    for i in range(3):
        stats.add(
            TranscriptResult(meta=TranscriptMeta(kind="subagent", agent_type="verification-runner"), turns=turns),
            SONNET_RATES,
        )
    row = stats.by_key()["verification-runner"]
    assert row.spawns == 3
    assert row.priced_turns == 3


# --------------------------------------------------------------------
# build_section(): table shape, fidelity warning, subscription suppression
# --------------------------------------------------------------------


def _rewrite_every_time_turns(n: int = 5, c: int = 1_000_000, gap: float = 1000.0) -> list[model.Turn]:
    """``n`` turns, each a full rewrite of the same-sized prefix ``c``
    with a fixed gap between them. Actual observed billing behaves as if
    the TTL were always 5m (every gap invalidates the write), but the
    gap is short enough (< 3600 s) that a real 1h TTL would have kept
    hitting — engineered so cost_all_1h is unambiguously cheaper than
    cost_observed/cost_all_5m by more than both switch thresholds."""
    turns = []
    for i in range(n):
        turns.append(
            _turn(
                turn_index=i + 1,
                message_id=f"msg_{i}",
                cache_creation_tokens=c,
                cc_5m=c,
                cc_1h=0,
                cache_read_tokens=0,
                gap_s=None if i == 0 else gap,
            )
        )
    return turns


def test_build_section_table_shape_and_columns():
    stats = TtlStats()
    stats.add(TranscriptResult(meta=TranscriptMeta(kind="top-level"), turns=_rewrite_every_time_turns()), SONNET_RATES)
    section = build_section(stats, billing_mode="api")

    assert section.key == "ttl"
    assert section.title == "Cache TTL break-even"
    assert len(section.tables) == 8
    (
        by_agent_type,
        gap_distribution,
        wasted_writes,
        premium_waste,
        break_even_share,
        near_miss,
        addressable_share,
        cache_economy_table,
    ) = section.tables
    assert by_agent_type.name == "ttl_by_agent_type"
    assert gap_distribution.name == "ttl_gap_distribution"
    assert wasted_writes.name == "ttl_wasted_writes"
    assert premium_waste.name == "ttl_premium_waste"
    assert break_even_share.name == "ttl_break_even_share"
    assert near_miss.name == "ttl_near_miss"
    assert addressable_share.name == "ttl_addressable_share"
    assert cache_economy_table.name == "ttl_cache_economy"
    assert [c.key for c in by_agent_type.columns] == [
        "agent_type",
        "spawns",
        "priced_turns",
        "observed_5m_pct",
        "observed_1h_pct",
        "gaps_over_5m",
        "gaps_over_1h",
        "limit_gaps",
        "gap_p50_s",
        "gap_p90_s",
        "cost_observed",
        "cost_all_5m",
        "cost_all_1h",
        "best_policy",
        "delta_usd",
        "delta_pct",
        "saving_usd",
        "fidelity_pct",
        "unsimulatable",
        "unpriced_turns",
        "recommendation",
        "lever",
    ]
    money_columns = {c.key for c in by_agent_type.columns if c.kind == "money"}
    assert money_columns == {"cost_observed", "cost_all_5m", "cost_all_1h", "delta_usd", "saving_usd"}
    pct_columns = {c.key for c in by_agent_type.columns if c.kind == "pct"}
    assert pct_columns == {"observed_5m_pct", "observed_1h_pct", "delta_pct", "fidelity_pct"}


def test_build_section_recommends_switch_to_1h_in_api_mode():
    stats = TtlStats()
    stats.add(TranscriptResult(meta=TranscriptMeta(kind="top-level"), turns=_rewrite_every_time_turns()), SONNET_RATES)
    section = build_section(stats, billing_mode="api")

    table = section.tables[0]
    rec_idx = [c.key for c in table.columns].index("recommendation")
    row = next(r for r in table.rows if r[0] == "top-level")
    assert row[rec_idx] == "switch to 1h"


def test_build_section_gives_subagents_switch_advice_in_subscription_mode():
    """Within plan usage a subscription's subagents honour a 1h lifetime
    (Claude Code's prompt-caching docs); only an agent file's 1h is
    ignored while the plan draws on usage credits. So subscription mode
    keeps every row's advice and states that caveat in a note, instead
    of suppressing subagent rows as if every subscription were on
    usage credits."""
    turns = _rewrite_every_time_turns()
    stats = TtlStats()
    stats.add(TranscriptResult(meta=TranscriptMeta(kind="top-level"), turns=turns), SONNET_RATES)
    stats.add(
        TranscriptResult(meta=TranscriptMeta(kind="subagent", agent_type="claude-implementer"), turns=turns),
        SONNET_RATES,
    )

    section = build_section(stats, billing_mode="subscription")
    table = section.tables[0]
    rec_idx = [c.key for c in table.columns].index("recommendation")
    by_key = {r[0]: r for r in table.rows}

    assert by_key["top-level"][rec_idx] == "switch to 1h"
    assert by_key["claude-implementer"][rec_idx] == "switch to 1h"
    note = next(note for note in section.notes if "usage credits" in note)
    assert "agent file is ignored" in note
    assert "subagentPromptCacheTtl" in note


def test_build_section_api_mode_has_no_usage_credits_note():
    stats = TtlStats()
    stats.add(TranscriptResult(meta=TranscriptMeta(kind="top-level"), turns=_rewrite_every_time_turns()), SONNET_RATES)
    section = build_section(stats, billing_mode="api")
    assert not any("usage credits" in note for note in section.notes)


_REAL_SESSION_A = Path(__file__).parent / "fixtures" / "real" / "session-a"


@pytest.mark.skipif(
    not _REAL_SESSION_A.exists() or not any(_REAL_SESSION_A.glob("*.jsonl")),
    reason="tests/fixtures/real/session-a/ not present (real fixture not checked out)",
)
def test_real_fixture_no_row_recommends_its_own_current_policy():
    """R2 regression: on a real corpus, ``build_section``'s recommendation
    must never tell an agent type to switch to the TTL policy that
    already dominates that same row's own observed traffic (previously
    possible since ``recommendation()`` didn't compare against the
    dominant *observed* split at all)."""
    from claudeglass import discovery

    top_paths = list(_REAL_SESSION_A.glob("*.jsonl"))
    assert len(top_paths) == 1, f"expected exactly one top-level jsonl, found {top_paths}"
    top_path = top_paths[0]
    session_id = top_path.stem
    top_meta = TranscriptMeta(path=str(top_path), kind="top-level", session_id=session_id)

    stats = TtlStats()
    stats.add(parse_transcript(top_path, top_meta), PRICING.resolve_model)
    for jsonl_path, _meta_dict in discovery.find_subagents(_REAL_SESSION_A, session_id):
        meta_path = jsonl_path.with_name(jsonl_path.stem + ".meta.json")
        meta = discovery.load_meta(meta_path)
        stats.add(parse_transcript(jsonl_path, meta), PRICING.resolve_model)

    section = build_section(stats, billing_mode="api")
    table = section.tables[0]
    idx = {c.key: i for i, c in enumerate(table.columns)}
    for row in table.rows:
        recommendation = row[idx["recommendation"]]
        observed_5m_pct = row[idx["observed_5m_pct"]]
        observed_1h_pct = row[idx["observed_1h_pct"]]
        if observed_5m_pct >= 90.0:
            assert recommendation != "switch to 5m", row
        if observed_1h_pct >= 90.0:
            assert recommendation != "switch to 1h", row


def test_build_section_fidelity_warning_lists_offending_agent_types():
    # turn2 grows far beyond what a naive 5m-policy replay would predict
    # from turn1, so sim(5m) diverges noticeably from observed cost even
    # though the dominant observed TTL is unambiguously "5m".
    t1 = _turn(cache_creation_tokens=1000, cc_5m=1000, cache_read_tokens=0, gap_s=None)
    t2 = _turn(turn_index=2, message_id="msg_2", cache_creation_tokens=5000, cc_5m=5000, cache_read_tokens=0, gap_s=50)
    turns = [t1, t2]

    stats = TtlStats()
    result = TranscriptResult(meta=TranscriptMeta(kind="subagent", agent_type="claude-implementer"), turns=turns)
    stats.add(result, SONNET_RATES)

    row = stats.by_key()["claude-implementer"]
    assert row.fidelity_pct is not None
    assert row.fidelity_pct > 10.0

    section = build_section(stats, billing_mode="api")
    assert any("claude-implementer" in note for note in section.notes)


# --------------------------------------------------------------------
# Real parser integration + privacy
# --------------------------------------------------------------------


def test_real_parsed_transcript_feeds_ttl_stats_and_passes_privacy(tmp_path: Path):
    lines = [
        turn_line(
            message_id="m1",
            model="claude-sonnet-5",
            input_tokens=10,
            cache_creation_input_tokens=1000,
            ephemeral_5m_input_tokens=1000,
            output_tokens=5,
            timestamp="2026-09-18T12:00:00.000Z",
        ),
        turn_line(
            message_id="m2",
            model="claude-sonnet-5",
            input_tokens=10,
            cache_creation_input_tokens=500,
            ephemeral_5m_input_tokens=500,
            cache_read_input_tokens=1000,
            output_tokens=5,
            timestamp="2026-09-18T12:01:30.000Z",
        ),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path), kind="top-level"))
    assert_privacy(result)

    stats = TtlStats()
    stats.add(result, SONNET_RATES)
    row = stats.by_key()["top-level"]
    assert row.priced_turns == 2
    assert row.spawns == 1

    section = build_section(stats, billing_mode="api")
    assert section.key == "ttl"
    assert len(section.tables) == 8


# --------------------------------------------------------------------
# Cache utilisation monitoring follow-up (items 1-6): wasted writes,
# 1h premium waste vs 5m expiry loss, break-even share, near-miss
# histogram, TTL-addressable share, cache economy.
# --------------------------------------------------------------------


def _row_for(turns: list[model.Turn], key: str = "top-level") -> TtlTypeStats:
    """Feed ``turns`` as a single top-level transcript into a fresh
    ``TtlStats`` and return its rolled-up row, at ``SONNET_RATES``."""
    stats = TtlStats()
    stats.add(TranscriptResult(meta=TranscriptMeta(kind="top-level"), turns=turns), SONNET_RATES)
    return stats.by_key()[key]


# -- Item 1: wasted writes --------------------------------------------


def test_wasted_writes_hand_computed_sonnet_5():
    """Four turns, all 5m writes (Sonnet 5 cache_write_5m = 2.5/M):

    - t1 writes C1=1000 (cc_5m=1000). t2's gap is 100s (<=300) and reads
      back 1000 (>= C1) -> t1's write is USED.
    - t2 writes 200 more (cc_5m=200, C2=1200). t3's gap is 400s (>300)
      -> the chain breaks before t3 even gets checked -> t2's write is
      NEVER READ AGAIN (wasted). USD = 200/1e6 * 2.5 = 0.0005.
    - t3 writes C3=1200 (cc_5m=1200, full rewrite since t2's entry
      expired). t4's gap is 100s (<=300) and reads back 1200 (>= C3)
      -> t3's write is USED.
    - t4 writes 300 more (cc_5m=300) and is the transcript's LAST turn,
      so it is a terminal write: USD = 300/1e6 * 2.5 = 0.00075. It is
      not a candidate for "used"/"wasted" at all (nothing can come
      after it) and must not appear in writes/wasted_writes/tokens
      written/tokens wasted/share.

    Non-terminal totals: writes=3 (t1, t2, t3), wasted_writes=1 (t2),
    tokens_written=1000+200+1200=2400, tokens_wasted=200,
    share=200/2400=8.333...%, usd_wasted=0.0005.
    """
    t1 = _turn(cache_creation_tokens=1000, cc_5m=1000, cache_read_tokens=0, gap_s=None)
    t2 = _turn(
        turn_index=2, message_id="msg_2", cache_creation_tokens=200, cc_5m=200, cache_read_tokens=1000, gap_s=100
    )
    t3 = _turn(
        turn_index=3, message_id="msg_3", cache_creation_tokens=1200, cc_5m=1200, cache_read_tokens=0, gap_s=400
    )
    t4 = _turn(
        turn_index=4, message_id="msg_4", cache_creation_tokens=300, cc_5m=300, cache_read_tokens=1200, gap_s=100
    )
    row = _row_for([t1, t2, t3, t4])

    assert row.waste_writes == 3
    assert row.waste_wasted_writes == 1
    assert row.waste_tokens_written == 2400
    assert row.waste_tokens_wasted == 200
    assert row.waste_share_pct == pytest.approx(100.0 * 200 / 2400)
    assert row.waste_usd_wasted == pytest.approx(200 / 1_000_000 * 2.5)
    assert row.waste_terminal_writes == 1
    assert row.waste_terminal_tokens == 300
    assert row.waste_terminal_usd == pytest.approx(300 / 1_000_000 * 2.5)


def test_terminal_write_excluded_from_waste_share():
    """t1 writes 1000 (cc_5m), used by t2 (gap 100s <= 300, reads back
    1000). t2 is the transcript's LAST turn and itself writes 500 more
    (cc_5m=500): that write is terminal — unavoidable, since nothing can
    come after the last turn — and must not inflate tokens_written or
    tokens_wasted, so waste_share_pct stays 0.0 rather than counting the
    terminal write as "wasted"."""
    t1 = _turn(cache_creation_tokens=1000, cc_5m=1000, cache_read_tokens=0, gap_s=None)
    t2 = _turn(
        turn_index=2, message_id="msg_2", cache_creation_tokens=500, cc_5m=500, cache_read_tokens=1000, gap_s=100
    )
    row = _row_for([t1, t2])

    assert row.waste_writes == 1
    assert row.waste_wasted_writes == 0
    assert row.waste_tokens_written == 1000
    assert row.waste_tokens_wasted == 0
    assert row.waste_share_pct == 0.0
    assert row.waste_terminal_writes == 1
    assert row.waste_terminal_tokens == 500
    assert row.waste_terminal_usd == pytest.approx(500 / 1_000_000 * 2.5)


def test_mixed_write_counted_as_two_separate_writes():
    """t1 is a mixed write: cc_5m=1000 (TTL 300s) and cc_1h=2000 (TTL
    3600s) in the same turn, C1 = 0 + 3000 = 3000. t2's gap is 500s and
    it reads back 3000 (>= C1):

    - The cc_5m portion's chain check uses TTL=300: 500 > 300, so the
      chain breaks before t2 is even considered -> NEVER READ AGAIN.
    - The cc_1h portion's chain check uses TTL=3600: 500 <= 3600, so t2
      is considered, and its read (3000) >= C1 -> USED.

    A single write couldn't have two different outcomes from the same
    next turn; getting a "wasted" 5m portion and a "used" 1h portion out
    of the same t1 proves they're tracked as two independent writes.
    t3 is a filler last turn with no cache_creation of its own (keeps
    this fixture focused; also confirms it contributes nothing to the
    waste table).

    USD wasted = the 5m portion only = 1000/1e6 * 2.5 = 0.0025.
    """
    t1 = _turn(cache_creation_tokens=3000, cc_5m=1000, cc_1h=2000, cache_read_tokens=0, gap_s=None)
    t2 = _turn(
        turn_index=2, message_id="msg_2", cache_creation_tokens=0, cache_read_tokens=3000, gap_s=500
    )
    t3 = _turn(turn_index=3, message_id="msg_3", cache_creation_tokens=0, cache_read_tokens=0, gap_s=200)
    row = _row_for([t1, t2, t3])

    assert row.waste_writes == 2
    assert row.waste_wasted_writes == 1
    assert row.waste_tokens_written == 1000 + 2000
    assert row.waste_tokens_wasted == 1000
    assert row.waste_usd_wasted == pytest.approx(1000 / 1_000_000 * 2.5)
    assert row.waste_terminal_writes == 0


# -- Item 2: 1h premium waste vs 5m expiry loss ------------------------


def test_premium_waste_vs_expiry_loss_hand_computed_sonnet_5():
    """Four turns (Sonnet 5: cache_write_5m=2.5/M, cache_write_1h=4.0/M).
    Independent-review item 5: every priced write W_i (=
    cache_creation_tokens, regardless of its own observed cc_5m/cc_1h
    split) is bucketed, unconditionally, into BOTH the 1h-premium and
    the 5m-expiry-loss counterfactuals by the gap to the *next* turn
    (t2.gap_s is "t1's next gap", etc.) -- these are two independent
    "what if every write used this TTL" simulations over the same W_i,
    not a report on whichever TTL the write actually happened to use.

    - t1: W=1300, next gap (t2.gap_s) = 200 (<=300) ->
      1h "not needed" (USD = 1300/1e6*(4.0-2.5) = 0.00195) AND
      5m "fine" (no USD).
    - t2: W=1200, next gap (t3.gap_s) = 1000 (300 < 1000 <= 3600) ->
      1h "earned" AND 5m "expiry loss", both priced at the *next*
      turn's (t3's) own prefix C3 = cache_read(0) + cache_creation(1100)
      = 1100, at the flat 5m rate: 1100/1e6*2.5 = 0.00275.
    - t3: W=1100, next gap (t4.gap_s) = 4000 (>3600) ->
      1h "expired anyway" (USD = 1100/1e6*(4.0-2.5) = 0.00165) AND
      5m "would have expired under 1h too" (no USD).
    - t4: W=0 (cache_creation_tokens=0) -> contributes nothing on
      either side, and there is no turn after it to receive a gap.

    Because t2's "5m loss" is priced off t3's own C at the 5m rate --
    exactly expiry_loss_all_5m's own basis for t3's in-window gap --
    summing premium_5m_loss_usd across this transcript reproduces
    expiry_loss_all_5m exactly (see the dedicated invariant test below).
    """
    t1 = _turn(cache_creation_tokens=1300, cc_5m=300, cc_1h=1000, cache_read_tokens=0, gap_s=None)
    t2 = _turn(
        turn_index=2,
        message_id="msg_2",
        cache_creation_tokens=1200,
        cc_5m=400,
        cc_1h=800,
        cache_read_tokens=0,
        gap_s=200,
    )
    t3 = _turn(
        turn_index=3,
        message_id="msg_3",
        cache_creation_tokens=1100,
        cc_5m=600,
        cc_1h=500,
        cache_read_tokens=0,
        gap_s=1000,
    )
    t4 = _turn(
        turn_index=4,
        message_id="msg_4",
        cache_creation_tokens=0,
        cc_5m=0,
        cc_1h=0,
        cache_read_tokens=0,
        gap_s=4000,
    )
    row = _row_for([t1, t2, t3, t4])

    assert row.premium_1h_not_needed_tokens == 1300
    assert row.premium_1h_not_needed_usd == pytest.approx(1300 / 1_000_000 * (4.0 - 2.5))
    assert row.premium_1h_earned_tokens == 1200
    assert row.premium_1h_earned_usd == pytest.approx(1100 / 1_000_000 * 2.5)
    assert row.premium_1h_expired_tokens == 1100
    assert row.premium_1h_expired_usd == pytest.approx(1100 / 1_000_000 * (4.0 - 2.5))

    assert row.premium_5m_fine_tokens == 1300
    assert row.premium_5m_loss_tokens == 1200
    assert row.premium_5m_loss_usd == pytest.approx(1100 / 1_000_000 * 2.5)
    assert row.premium_5m_would_expire_tokens == 1100


def test_premium_5m_loss_usd_sums_to_expiry_loss_all_5m():
    """Independent-review item 5's required invariant: for a
    well-formed fixture where every non-terminal turn actually writes
    something (W_i > 0), summing premium_5m_loss_usd across the
    transcript reproduces expiry_loss_all_5m exactly, because both are
    the same "receiving turn's own C, priced at the flat 5m rate"
    computation -- just attributed from opposite ends of the same gap
    (the writer's "loss bucket" vs. the receiver's "expiry loss").
    Reuses the same four-turn fixture as the hand-computed test above.
    """
    t1 = _turn(cache_creation_tokens=1300, cc_5m=300, cc_1h=1000, cache_read_tokens=0, gap_s=None)
    t2 = _turn(
        turn_index=2,
        message_id="msg_2",
        cache_creation_tokens=1200,
        cc_5m=400,
        cc_1h=800,
        cache_read_tokens=0,
        gap_s=200,
    )
    t3 = _turn(
        turn_index=3,
        message_id="msg_3",
        cache_creation_tokens=1100,
        cc_5m=600,
        cc_1h=500,
        cache_read_tokens=0,
        gap_s=1000,
    )
    t4 = _turn(
        turn_index=4,
        message_id="msg_4",
        cache_creation_tokens=0,
        cc_5m=0,
        cc_1h=0,
        cache_read_tokens=0,
        gap_s=4000,
    )
    row = _row_for([t1, t2, t3, t4])

    assert row.premium_5m_loss_usd == pytest.approx(row.expiry_loss_all_5m)


# -- Item 3: break-even share -------------------------------------------


def test_break_even_premium_ratio_is_0_6_for_sonnet_5():
    """premium_ratio = (write_1h - write_5m) / write_5m, computed from
    the resolved rate card, never hard-coded: for Sonnet 5,
    (4.0 - 2.5) / 2.5 = 0.6 exactly."""
    row = _row_for([_turn(cache_creation_tokens=100, cc_5m=100)])
    assert row.premium_ratio == pytest.approx(0.6)


def test_break_even_share_hand_computed_sonnet_5():
    """Three turns (Sonnet 5: cache_write_5m=2.5/M, cache_write_1h=4.0/M,
    cache_read=0.2/M). t1 is the first write (its gap is excluded from
    the weighted share, per simulate's own i==0 convention). t2's gap is
    500s (in the (300, 3600] window) with prefix C2 = cache_read(1000) +
    cache_creation(2000) = 3000. t3's gap is 100s (NOT in the window,
    <=300) with prefix C3 = cache_read(500) + cache_creation(500) =
    1000.

    premium_all_1h = Sigma_i W_i * (write_1h - write_5m) over EVERY
    write (t1, t2, t3 all count, not gated on a gap): W = 500+2000+500 =
    3000 tokens * 1.5/1e6 = 0.0045.

    expiry_loss_all_5m = Sigma over in-window gaps of C_j * write_5m:
    only t2's gap (500s) lands in (300, 3600] -> C2(3000) * 2.5/1e6 =
    0.0075. (t3's gap is 100s, <=300, excluded.)

    margin = 0.0075 - 0.0045 = 0.003 -> positive (1h direction), but
    both the 5%-of-larger-side (0.000375) and the $1.00 floor put this
    well inside "marginal" — small hand-computed dollar amounts like
    this one are exactly what the $1.00 floor exists to catch.

    Fix item 6: the break-even denominator (Sigma_{all gaps} C_j) now
    also includes turn 0's own prefix C1 = cache_read(0) +
    cache_creation(500) = 500 -- not just the turns with a gap in front
    of them -- so the denominator is C1+C2+C3 = 500+3000+1000 = 4500,
    not just C2+C3 = 4000. Both share fields are now percentages
    (0-100):

    in_window_pct = 100 * C2 / (C1+C2+C3) = 100 * 3000/4500 = 66.667%.
    break_even_pct = 100 * premium_ratio * (Sigma W_i / (C1+C2+C3))
    = 100 * 0.6 * (3000/4500) = 40.0%.

    margin > 0 (1h direction) and in_window_pct (66.667) >
    break_even_pct (40.0) — the verdict identity holds.
    """
    t1 = _turn(cache_creation_tokens=500, cc_5m=500, cache_read_tokens=0, gap_s=None)
    t2 = _turn(
        turn_index=2, message_id="msg_2", cache_creation_tokens=2000, cc_5m=2000, cache_read_tokens=1000, gap_s=500
    )
    t3 = _turn(
        turn_index=3, message_id="msg_3", cache_creation_tokens=500, cc_5m=500, cache_read_tokens=500, gap_s=100
    )
    row = _row_for([t1, t2, t3])

    assert row.premium_ratio == pytest.approx(0.6)
    assert row.in_window_pct == pytest.approx(100 * 3000 / 4500)
    assert row.premium_all_1h == pytest.approx(0.0045)
    assert row.expiry_loss_all_5m == pytest.approx(0.0075)
    assert row.margin == pytest.approx(0.003)
    assert row.break_even_pct == pytest.approx(40.0)
    assert row.verdict == "marginal"
    assert (row.margin > 0) == (row.in_window_pct > row.break_even_pct)


def test_break_even_pct_identity_matches_margin_sign_in_5m_pays_direction():
    """Fix item 6's required check, in the opposite direction from the
    hand-computed test above: two writes, both followed by a short
    (<=300s) gap, so nothing ever lands in the (300, 3600] window.
    expiry_loss_all_5m is then 0 while premium_all_1h is positive (every
    write still counts, unconditionally) -> margin < 0 (5m pays).

    in_window_pct = 0 (the numerator, in-window weight, is 0).
    break_even_pct = 100 * premium_ratio * (total_w / total_weight) =
    100 * 0.6 * (2000/2000) = 60.0 (turn 2's own gap -- 100s, not
    itself in-window -- still contributes its C to the *denominator*,
    same as turn 0's C via the item-6 fix, since the denominator is
    "every gap", not only the in-window ones).

    margin < 0 and in_window_pct (0.0) < break_even_pct (60.0) -- the
    identity holds in this direction too.
    """
    t1 = _turn(cache_creation_tokens=1000, cc_5m=1000, cache_read_tokens=0, gap_s=None)
    t2 = _turn(
        turn_index=2, message_id="msg_2", cache_creation_tokens=1000, cc_5m=1000, cache_read_tokens=0, gap_s=100
    )
    row = _row_for([t1, t2])

    assert row.premium_ratio == pytest.approx(0.6)
    assert row.expiry_loss_all_5m == pytest.approx(0.0)
    assert row.premium_all_1h == pytest.approx(0.003)
    assert row.margin == pytest.approx(-0.003)
    assert row.in_window_pct == pytest.approx(0.0)
    assert row.break_even_pct == pytest.approx(60.0)
    assert (row.margin > 0) == (row.in_window_pct > row.break_even_pct)


# -- Fix item 6: config-driven TtlThresholds ----------------------------


def test_ttl_thresholds_defaults_match_the_pre_item_6_hardcoded_values():
    """The consolidated dataclass's defaults must reproduce every one
    of the five module constants it replaces, so a caller that never
    passes ``thresholds`` sees unchanged behaviour."""
    th = TtlThresholds()
    assert th.dominance == pytest.approx(0.9)
    assert th.fidelity_warn_pct == pytest.approx(10.0)
    assert th.switch_pct == pytest.approx(0.95)
    assert th.switch_usd == pytest.approx(1.00)
    assert th.near_miss_window_s == pytest.approx(60.0)
    assert th.ctx_floor == 20_000
    assert th.cr_ratio == pytest.approx(0.2)
    assert th.full_expiry_cr == 2_000


def test_ttl_thresholds_from_config_reads_flat_dict():
    th = TtlThresholds.from_config(
        {
            "dominance": 0.8,
            "fidelity_warn_pct": 5.0,
            "switch_pct": 0.9,
            "switch_usd": 2.0,
            "near_miss_window_s": 30.0,
            "ctx_floor": 10_000,
            "cr_ratio": 0.1,
            "full_expiry_cr": 500,
        }
    )
    assert th.dominance == pytest.approx(0.8)
    assert th.fidelity_warn_pct == pytest.approx(5.0)
    assert th.switch_pct == pytest.approx(0.9)
    assert th.switch_usd == pytest.approx(2.0)
    assert th.near_miss_window_s == pytest.approx(30.0)
    assert th.ctx_floor == 10_000
    assert th.cr_ratio == pytest.approx(0.1)
    assert th.full_expiry_cr == 500


def test_ttl_thresholds_from_config_reads_nested_thresholds_table():
    config = {"thresholds": {"dominance": 0.75, "ctx_floor": 5_000}}
    th = TtlThresholds.from_config(config)
    assert th.dominance == pytest.approx(0.75)
    assert th.ctx_floor == 5_000
    # Untouched keys keep this class's own defaults.
    assert th.switch_pct == pytest.approx(0.95)


def test_ttl_thresholds_recache_trio_delegates_to_recache_thresholds():
    """The RE-CACHE trio must resolve identically to
    ``recache.RecacheThresholds.from_config`` on the same config, so the
    two modules can never drift apart on these three numbers."""
    config = {"thresholds": {"ctx_floor": 12_345, "cr_ratio": 0.33, "full_expiry_cr": 999}}
    ttl_th = TtlThresholds.from_config(config)
    recache_th = recache.RecacheThresholds.from_config(config)
    assert ttl_th.ctx_floor == recache_th.ctx_floor == 12_345
    assert ttl_th.cr_ratio == pytest.approx(recache_th.cr_ratio) == pytest.approx(0.33)
    assert ttl_th.full_expiry_cr == recache_th.full_expiry_cr == 999


def test_ttl_thresholds_from_config_none_gives_defaults():
    assert TtlThresholds.from_config(None) == TtlThresholds()


def test_ttl_thresholds_describe_mentions_every_field():
    lines = " ".join(TtlThresholds().describe())
    # One value per threshold, in plain words rather than its config key.
    for needle in ("90%", "10.0%", "95%", "$1.00", "5.0%", "60 seconds", "20,000", "20%", "2,000"):
        assert needle in lines


def test_recommendation_honors_custom_switch_thresholds():
    """The same 9.40-vs-10.00 case ``test_recommendation_blocked_by_usd_threshold_
    despite_large_pct_saving`` shows blocked at the default $1.00 floor
    (only $0.60 saved) now switches once a caller relaxes ``switch_usd``
    below the actual saving."""
    s = _stats(cost_observed=10.0, cost_all_5m=10.0, cost_all_1h=9.40)
    assert s.recommendation() == "no material difference"
    assert s.recommendation(TtlThresholds(switch_usd=0.5)) == "switch to 1h"


def test_dominant_ttl_honors_custom_dominance_threshold():
    """80/20 split: "mixed" at the default 90% dominance cutoff, "5m"
    once a caller relaxes it to 75%."""
    turns = [_turn(cc_5m=80, cc_1h=20)]
    assert dominant_ttl(turns) == "mixed"
    assert dominant_ttl(turns, TtlThresholds(dominance=0.75)) == "5m"


def test_near_miss_window_s_customizes_the_boundary():
    """A 390s gap falls outside the default 60s near-5m-miss window
    ((300, 360]) but inside a 120s one ((300, 420])."""
    t1 = _turn(cache_creation_tokens=0, cache_read_tokens=0, gap_s=None)
    t2 = _turn(turn_index=2, message_id="msg_2", cache_creation_tokens=200, cc_5m=200, gap_s=390.0)

    stats_default = TtlStats()
    stats_default.add(TranscriptResult(meta=TranscriptMeta(kind="top-level"), turns=[t1, t2]), SONNET_RATES)
    row_default = stats_default.by_key()["top-level"]
    assert row_default.near_5m_miss == 0

    stats_custom = TtlStats()
    stats_custom.add(
        TranscriptResult(meta=TranscriptMeta(kind="top-level"), turns=[t1, t2]),
        SONNET_RATES,
        TtlThresholds(near_miss_window_s=120.0),
    )
    row_custom = stats_custom.by_key()["top-level"]
    assert row_custom.near_5m_miss == 1
    assert row_custom.near_5m_miss_tokens == 200


def test_recache_classification_fallback_honors_custom_ctx_floor():
    """``ctx=15_000`` never qualifies under the default ``ctx_floor``
    (20_000), so it never reaches the addressable-share tally; lowering
    ``ctx_floor`` to 10_000 (via the same TtlThresholds passed to
    ``TtlStats.add``) makes it qualify as "full-expiry" (cache_read=100
    is below both the 20%-of-ctx bar and the 2_000-token full-expiry
    bar)."""
    t1 = _turn(cache_creation_tokens=1000, cc_5m=1000, cache_read_tokens=0, gap_s=None)
    t2 = _turn(
        turn_index=2,
        message_id="msg_2",
        ctx=15_000,
        cache_read_tokens=100,
        cache_creation_tokens=500,
        cc_5m=500,
        gap_s=400,
    )

    stats_default = TtlStats()
    stats_default.add(TranscriptResult(meta=TranscriptMeta(kind="top-level"), turns=[t1, t2]), SONNET_RATES)
    row_default = stats_default.by_key()["top-level"]
    assert row_default.addressable_full_expiry_tokens == 0

    stats_custom = TtlStats()
    stats_custom.add(
        TranscriptResult(meta=TranscriptMeta(kind="top-level"), turns=[t1, t2]),
        SONNET_RATES,
        TtlThresholds(ctx_floor=10_000),
    )
    row_custom = stats_custom.by_key()["top-level"]
    assert row_custom.addressable_full_expiry_tokens == 500


def test_build_section_fidelity_warning_uses_configured_threshold():
    """Two turns engineered for a small, nonzero fidelity mismatch
    (~2.59%: t2 reads back the whole of t1's prefix but its 50-token
    incremental write is observed at the 1h rate even though the 5m
    policy the transcript is dominant under would only ever write it at
    the 5m rate — a real anomaly no fixed-policy simulation predicts).
    A ``TtlThresholds`` set just above that value never warns; one set
    just below it does, and names the configured percentage rather than
    a hardcoded "10%"."""
    t1 = _turn(cache_creation_tokens=1000, cc_5m=1000, cache_read_tokens=0, gap_s=None)
    t2 = _turn(
        turn_index=2, message_id="msg_2", cache_read_tokens=1000, cache_creation_tokens=50, cc_1h=50, gap_s=100.0
    )
    stats = TtlStats()
    stats.add(TranscriptResult(meta=TranscriptMeta(kind="top-level"), turns=[t1, t2]), SONNET_RATES)
    row = stats.by_key()["top-level"]
    assert row.fidelity_pct == pytest.approx(2.5862068965517233)

    strict = TtlThresholds(fidelity_warn_pct=row.fidelity_pct + 1.0)
    section_default = build_section(stats, billing_mode="api", thresholds=strict)
    assert not any("Simulation fidelity exceeds" in note for note in section_default.notes)

    relaxed = TtlThresholds(fidelity_warn_pct=row.fidelity_pct - 1.0)
    section_flagged = build_section(stats, billing_mode="api", thresholds=relaxed)
    assert any(f"exceeds {relaxed.fidelity_warn_pct:.0f}%" in note for note in section_flagged.notes)


def test_build_section_notes_include_thresholds_line():
    stats = TtlStats()
    stats.add(TranscriptResult(meta=TranscriptMeta(kind="top-level"), turns=_rewrite_every_time_turns()), SONNET_RATES)
    section = build_section(stats, billing_mode="api")
    assert any(note.startswith("Thresholds:") for note in section.notes)


def _break_even_stats(premium_all_1h: float, expiry_loss_all_5m: float) -> TtlTypeStats:
    """A ``TtlTypeStats`` with only the item-3 USD fields the
    marginal/verdict threshold tests care about set to something
    meaningful."""
    return TtlTypeStats(
        key="top-level",
        spawns=1,
        priced_turns=1,
        observed_5m_pct=100.0,
        observed_1h_pct=0.0,
        gaps_over_5m=0,
        gaps_over_1h=0,
        gap_p50_s=None,
        gap_p90_s=None,
        cost_observed=1.0,
        cost_all_5m=1.0,
        cost_all_1h=1.0,
        unsimulatable=0,
        fidelity_pct=0.0,
        gap_buckets={},
        premium_all_1h=premium_all_1h,
        expiry_loss_all_5m=expiry_loss_all_5m,
    )


def test_break_even_verdict_marginal_within_five_percent_of_larger_side():
    """margin = 104.0 - 100.0 = 4.0; larger side = 104.0, 5% of it =
    5.2. |margin| (4.0) < 5.2 -> "marginal", even though |margin| is
    well above the $1.00 floor on its own (this case is decided purely
    by the relative condition)."""
    s = _break_even_stats(premium_all_1h=100.0, expiry_loss_all_5m=104.0)
    assert s.margin == pytest.approx(4.0)
    assert s.verdict == "marginal"


def test_break_even_verdict_marginal_within_one_dollar():
    """margin = 1.9 - 1.0 = 0.9; larger side = 1.9, 5% of it = 0.095, so
    the relative condition alone would NOT call this marginal (0.9 >
    0.095) -- it's the $1.00 absolute floor that decides it here (0.9 <
    1.00), proving the two conditions are independent ORs."""
    s = _break_even_stats(premium_all_1h=1.0, expiry_loss_all_5m=1.9)
    assert s.margin == pytest.approx(0.9)
    assert s.verdict == "marginal"


def test_break_even_verdict_decisive_1h_and_5m_pays():
    """Neither threshold trips: margin=10.0 against a larger side of
    20.0 (5% = 1.0) clears both the relative and the $1.00 floor, so the
    sign of margin alone decides the verdict."""
    pays_1h = _break_even_stats(premium_all_1h=10.0, expiry_loss_all_5m=20.0)
    assert pays_1h.margin == pytest.approx(10.0)
    assert pays_1h.verdict == "1h pays"

    pays_5m = _break_even_stats(premium_all_1h=20.0, expiry_loss_all_5m=5.0)
    assert pays_5m.margin == pytest.approx(-15.0)
    assert pays_5m.verdict == "5m pays"


def test_break_even_verdict_agrees_with_best_policy_direction_large_prefix():
    """Regression test for the item-3 formula bug the corpus run
    surfaced: the old percentage-point formula could disagree in
    *direction* with the precise ``simulate()``-based ``best_policy``.
    This fixture is the "large prefix, small increments" shape that
    exposes it — a big first write, then three small per-turn writes
    onto a steadily growing context, two of whose gaps (500s, 600s) land
    in the (300, 3600] in-window bucket where a 5m TTL would force a
    full 500k+-token prefix rewrite but a 1h TTL would not.

    c_i (= cache_read_i + cache_creation_i) grows 500,000 -> 505,000 ->
    510,000 -> 515,000 across the four turns; simulate()'s own
    read/write derivation (min(c_i, prev_c) when the gap survives the
    policy) gives cost_all_5m=3.902 and cost_all_1h=2.363 by hand, i.e.
    best_policy="1h" -- and the new verdict must agree.

    premium_all_1h = Sigma W_i * 1.5/1e6 over ALL FOUR writes (500000 +
    5000 + 5000 + 5000 = 515000 tokens) = 0.7725. expiry_loss_all_5m =
    C2(505000)*2.5/1e6 + C3(510000)*2.5/1e6 = 1.2625 + 1.275 = 2.5375
    (t4's gap is 200s, <=300, so it's excluded). margin = 2.5375 -
    0.7725 = 1.765, comfortably clear of both the 5%-of-2.5375 (~0.127)
    and $1.00 thresholds -> "1h pays", matching best_policy.
    """
    t1 = _turn(cache_creation_tokens=500_000, cc_5m=500_000, cache_read_tokens=0, gap_s=None)
    t2 = _turn(
        turn_index=2,
        message_id="msg_2",
        cache_creation_tokens=5_000,
        cc_5m=5_000,
        cache_read_tokens=500_000,
        gap_s=500,
    )
    t3 = _turn(
        turn_index=3,
        message_id="msg_3",
        cache_creation_tokens=5_000,
        cc_5m=5_000,
        cache_read_tokens=505_000,
        gap_s=600,
    )
    t4 = _turn(
        turn_index=4,
        message_id="msg_4",
        cache_creation_tokens=5_000,
        cc_5m=5_000,
        cache_read_tokens=510_000,
        gap_s=200,
    )
    turns = [t1, t2, t3, t4]
    row = _row_for(turns)

    assert row.premium_all_1h == pytest.approx(0.7725)
    assert row.expiry_loss_all_5m == pytest.approx(2.5375)
    assert row.margin == pytest.approx(1.765)
    assert row.verdict == "1h pays"

    sim_5m = simulate(turns, SONNET_RATES, POLICY_5M)
    sim_1h = simulate(turns, SONNET_RATES, POLICY_1H)
    assert sim_5m.cost == pytest.approx(3.902)
    assert sim_1h.cost == pytest.approx(2.363)
    assert row.best_policy == "1h"
    assert (row.verdict == "1h pays") == (row.best_policy == "1h")


# -- Item 4: near-miss histogram -----------------------------------------


def test_near_miss_histogram_boundaries_hand_computed_sonnet_5():
    """Boundaries are asymmetric on purpose: [240, 300] and [3540, 3600]
    (inclusive both ends) still hit the cache; (300, 360] and
    (3600, 3660] (exclusive lower, inclusive upper) just missed it and
    forced a full rewrite; anything past 360/3660 is neither bucket.

    5m side: gaps 240 and 300 -> hits (2). Gaps 301 and 360 -> misses
    (2), rewriting 100 and 200 tokens respectively (tokens=300).
    Gap 361 -> neither.

    1h side: gaps 3540 and 3600 -> hits (2). Gaps 3601 and 3660 ->
    misses (2), rewriting 300 and 400 tokens respectively (tokens=700).
    Gap 3661 -> neither.

    USD (independent-review item 5): both sides price the miss at the
    flat 5m write rate against the turn's own C_i = cache_read +
    cache_creation (here cache_read=0, so C_i is just the tokens
    written), never the turn's real observed rate -- so the 1h-side
    misses are NOT priced at the 1h premium they actually paid.
    5m side: (100+200)/1e6*2.5 = 0.00075. 1h side: (300+400)/1e6*2.5
    = 0.00175.
    """
    turns = [_turn(cache_creation_tokens=0, cache_read_tokens=0, gap_s=None)]
    gap_values = [240, 300, 301, 360, 361, 3540, 3600, 3601, 3660, 3661]
    # tokens rewritten only matter for the two "just missed" gaps at
    # each boundary (301 -> 100 tokens cc_5m, 360 -> 200 tokens cc_5m,
    # 3601 -> 300 tokens cc_1h, 3660 -> 400 tokens cc_1h); every other
    # gap carries no cache-write tokens (irrelevant to the histogram).
    rewrite_tokens = {301: ("cc_5m", 100), 360: ("cc_5m", 200), 3601: ("cc_1h", 300), 3660: ("cc_1h", 400)}
    for idx, gap in enumerate(gap_values, start=2):
        kwargs = dict(turn_index=idx, message_id=f"msg_{idx}", gap_s=float(gap))
        if gap in rewrite_tokens:
            field_name, tokens = rewrite_tokens[gap]
            kwargs["cache_creation_tokens"] = tokens
            kwargs[field_name] = tokens
        turns.append(_turn(**kwargs))

    row = _row_for(turns)

    assert row.near_5m_hit == 2
    assert row.near_5m_miss == 2
    assert row.near_5m_miss_tokens == 300
    assert row.near_5m_miss_usd == pytest.approx(300 / 1_000_000 * 2.5)
    assert row.near_1h_hit == 2
    assert row.near_1h_miss == 2
    assert row.near_1h_miss_tokens == 700
    assert row.near_1h_miss_usd == pytest.approx((300 + 400) / 1_000_000 * 2.5)


# -- Item 5: TTL-addressable share ---------------------------------------


def test_addressable_share_falls_back_to_minimal_rule_when_signatures_none():
    """Every turn's ``recache_signature`` is ``None`` (the default —
    nothing in this test ran ``recache.detect``/``recache.apply`` on
    these turns), so classification falls back to ``recache.py``'s own
    minimal rule turn by turn: ``turn_index > 1``, ``ctx > 20_000``,
    ``cache_read < 0.2 * ctx``.

    - t1 (turn_index=1) never qualifies regardless of its ctx/cache_read
      (turn_index > 1 required) -> excluded.
    - t2: ctx=25_000, cache_read=500 (500 < 20% of 25_000=5_000, and
      500 < 2_000) -> "full-expiry". cache_creation_tokens=3000 ->
      USD = 3000/1e6*2.5 = 0.0075.
    - t3: ctx=30_000, cache_read=3_000 (3_000 < 20% of 30_000=6_000, but
      3_000 >= 2_000) -> "prefix-invalidated". cache_creation_tokens=2000
      -> USD = 2000/1e6*2.5 = 0.005.
    - t4: ctx=10_000 (not > 20_000) -> not a re-cache turn at all,
      excluded from both buckets.
    """
    t1 = _turn(cache_creation_tokens=1000, cc_5m=1000, cache_read_tokens=0, ctx=1000, gap_s=None)
    t2 = _turn(
        turn_index=2,
        message_id="msg_2",
        cache_creation_tokens=3000,
        cc_5m=3000,
        cache_read_tokens=500,
        ctx=25_000,
        gap_s=400,
    )
    t3 = _turn(
        turn_index=3,
        message_id="msg_3",
        cache_creation_tokens=2000,
        cc_5m=2000,
        cache_read_tokens=3000,
        ctx=30_000,
        gap_s=100,
    )
    t4 = _turn(
        turn_index=4,
        message_id="msg_4",
        cache_creation_tokens=500,
        cc_5m=500,
        cache_read_tokens=200,
        ctx=10_000,
        gap_s=100,
    )
    for t in (t1, t2, t3, t4):
        assert t.recache_signature is None
    row = _row_for([t1, t2, t3, t4])

    assert row.addressable_full_expiry_tokens == 3000
    assert row.addressable_full_expiry_usd == pytest.approx(3000 / 1_000_000 * 2.5)
    assert row.addressable_prefix_invalidated_tokens == 2000
    assert row.addressable_prefix_invalidated_usd == pytest.approx(2000 / 1_000_000 * 2.5)


def test_addressable_share_uses_real_signature_when_set():
    # ctx=0 would NOT satisfy the minimal fallback rule at all (ctx not >
    # 20_000), yet the real signature still classifies this turn as
    # full-expiry -- proving the signature is preferred over the fallback
    # whenever it is actually set. gap_s=400 is past the 5m write's
    # lifetime, so the clock expired it (a shorter wait would not count).
    t1 = _turn(cache_creation_tokens=1000, cc_5m=1000, cache_read_tokens=0, gap_s=None)
    t2 = _turn(turn_index=2, message_id="msg_2", cache_creation_tokens=400, cc_5m=400, cache_read_tokens=100, gap_s=400)
    t2 = dataclasses.replace(t2, recache_signature="full-expiry")
    row = _row_for([t1, t2])

    assert row.addressable_full_expiry_tokens == 400
    assert row.addressable_full_expiry_usd == pytest.approx(400 / 1_000_000 * 2.5)
    assert row.addressable_prefix_invalidated_tokens == 0


# -- Item 6: cache economy -------------------------------------------------


def test_cache_economy_hand_computed_sonnet_5():
    """Four turns (Sonnet 5: input=2.0, output=10.0, write_5m=2.5,
    write_1h=4.0, read=0.2, all per million tokens):

    - t1: input=1000, output=200, cc_5m=5000 (write), read=0.
      cost = 1000/1e6*2 + 200/1e6*10 + 5000/1e6*2.5 + 0
           = 0.002 + 0.002 + 0.0125 = 0.0165.
      uncached-equivalent (cache tokens at input rate) = 5000/1e6*2 = 0.01.
    - t2: input=800, output=150, read=5000, no write.
      cost = 0.0016 + 0.0015 + 0 + 5000/1e6*0.2(=0.001) = 0.0041.
      uncached-equivalent = 5000/1e6*2 = 0.01.
    - t3: input=600, output=100, cc_1h=2000 (write), read=3000.
      cost = 0.0012 + 0.001 + 2000/1e6*4.0(=0.008) + 3000/1e6*0.2(=0.0006)
           = 0.0108.
      uncached-equivalent = (3000+2000)/1e6*2 = 0.01.
    - t4: input=400, output=50, read=5000, no write.
      cost = 0.0008 + 0.0005 + 0 + 0.001 = 0.0023.
      uncached-equivalent = 5000/1e6*2 = 0.01.

    Totals: tokens_written=5000+0+2000+0=7000,
    tokens_read=0+5000+3000+5000=13000,
    write_usd=0.0125+0+0.008+0=0.0205,
    read_usd=0+0.001+0.0006+0.001=0.0026,
    uncached_equivalent_usd=0.01*4=0.04,
    net_saving_usd=0.04-(0.0205+0.0026)=0.0169,
    cache_roi=0.0169/0.0205.
    """
    t1 = _turn(input_tokens=1000, output_tokens=200, cache_creation_tokens=5000, cc_5m=5000, cache_read_tokens=0)
    t2 = _turn(
        turn_index=2, message_id="msg_2", input_tokens=800, output_tokens=150, cache_read_tokens=5000, gap_s=90
    )
    t3 = _turn(
        turn_index=3,
        message_id="msg_3",
        input_tokens=600,
        output_tokens=100,
        cache_creation_tokens=2000,
        cc_1h=2000,
        cache_read_tokens=3000,
        gap_s=90,
    )
    t4 = _turn(
        turn_index=4, message_id="msg_4", input_tokens=400, output_tokens=50, cache_read_tokens=5000, gap_s=90
    )
    turns = [t1, t2, t3, t4]

    write_usd = 0.0125 + 0 + 0.008 + 0
    read_usd = 0 + 0.001 + 0.0006 + 0.001
    uncached_equivalent_usd = 0.01 * 4
    net_saving_usd = uncached_equivalent_usd - (write_usd + read_usd)

    row = _row_for(turns)
    assert row.economy_tokens_written == 7000
    assert row.economy_tokens_read == 13000
    assert row.economy_write_usd == pytest.approx(write_usd)
    assert row.economy_read_usd == pytest.approx(read_usd)
    assert row.economy_uncached_equivalent_usd == pytest.approx(uncached_equivalent_usd)
    assert row.net_saving_usd == pytest.approx(net_saving_usd)
    assert row.cache_roi == pytest.approx(net_saving_usd / write_usd)

    # The standalone helper (for WP10's independent "overall" line) must
    # agree with TtlStats's own roll-up exactly.
    standalone = cache_economy(turns, SONNET_RATES)
    assert standalone["tokens_written"] == row.economy_tokens_written
    assert standalone["tokens_read"] == row.economy_tokens_read
    assert standalone["write_usd"] == pytest.approx(row.economy_write_usd)
    assert standalone["read_usd"] == pytest.approx(row.economy_read_usd)
    assert standalone["uncached_equivalent_usd"] == pytest.approx(row.economy_uncached_equivalent_usd)
    assert standalone["net_saving_usd"] == pytest.approx(row.net_saving_usd)
    assert standalone["cache_roi"] == pytest.approx(row.cache_roi)


def test_cache_economy_zero_write_usd_gives_zero_roi():
    turns = [_turn(input_tokens=100, output_tokens=10, cache_read_tokens=500, cache_creation_tokens=0)]
    result = cache_economy(turns, SONNET_RATES)
    assert result["write_usd"] == 0.0
    assert result["cache_roi"] == 0.0


def test_cache_tokens_at_input_rate_folds_in_fast_long_context_and_geo():
    """P10a perf rewrite: ``_cache_tokens_at_input_rate`` used to price a
    synthetic turn (cache tokens folded into ``input_tokens``) a second
    time via ``price_turn`` and subtract the real ``input_cost`` back
    out; it now reads the effective input rate straight from
    ``pricing.effective_rates``. Cross-checks that rewrite against a
    model whose fast, long-context and geo-uplift rules are all active
    at once -- the multipliers ``effective_rates`` folds in, in the same
    order ``price_turn`` applies them -- so a mistake in that folding
    (e.g. dropping the geo uplift) would show up here even though it
    wouldn't in the plain-``SONNET_RATES`` tests above.
    """
    from claudeglass.pricing import FastRule, LongContextRule, ModelRates, ResolvedRates

    model_rates = ModelRates(
        canonical_id="test-model",
        input=2.0,
        output=10.0,
        cache_write_5m=2.5,
        cache_write_1h=4.0,
        cache_read=0.2,
        geo_multipliers={"us": 1.25},
        long_context=LongContextRule(threshold_tokens=1_000, multiplier=2.0),
        fast=FastRule(multiplier=1.5),
    )
    rates = ResolvedRates(canonical_id="test-model", rates=model_rates)
    turn = _turn(
        input_tokens=1_000,
        output_tokens=100,
        cache_read_tokens=500,
        cache_creation_tokens=300,
        cc_5m=300,
        ctx=2_000,  # >= long_context.threshold_tokens
        speed="fast",
        inference_geo="us",
    )
    # input rate: 2.0 (base) * 1.5 (fast) * 2.0 (long_context) * 1.25 (geo) = 7.5.
    expected_rate = 2.0 * 1.5 * 2.0 * 1.25
    expected = (500 + 300) / 1_000_000 * expected_rate

    result = cache_economy([turn], rates)
    assert result["uncached_equivalent_usd"] == pytest.approx(expected, abs=1e-12, rel=1e-12)

    # Cross-check against the old two-price_turn-calls-and-subtract shape
    # directly (not just the hand-computed rate above), so a change to
    # price_turn's own multiplier order would also be caught here.
    real = price_turn(turn, rates)
    synthetic = dataclasses.replace(
        turn,
        input_tokens=turn.input_tokens + turn.cache_read_tokens + turn.cache_creation_tokens,
        cache_creation_tokens=0,
        cache_read_tokens=0,
        cc_5m=0,
        cc_1h=0,
    )
    inflated = price_turn(synthetic, rates)
    assert result["uncached_equivalent_usd"] == pytest.approx(
        inflated.input_cost - real.input_cost, abs=1e-12, rel=1e-12
    )


# -- build_section: new tables, notes, window_start caveat ----------------


def test_build_section_new_tables_have_expected_notes():
    stats = TtlStats()
    stats.add(TranscriptResult(meta=TranscriptMeta(kind="top-level"), turns=_rewrite_every_time_turns()), SONNET_RATES)
    section = build_section(stats, billing_mode="api")
    tables_by_name = {t.name: t for t in section.tables}

    assert (
        "The 1-hour premium is paid only on new cache writes. An expiry rewrites the whole cached context."
        " So the break-even share is the premium ratio, scaled by how big new writes are next to the whole"
        " context."
        in " ".join(tables_by_name["ttl_break_even_share"].notes)
    )
    assert "statusline countdown" in " ".join(tables_by_name["ttl_near_miss"].notes)


def test_the_addressable_share_note_keeps_each_sentence_short():
    stats = TtlStats()
    stats.add(TranscriptResult(meta=TranscriptMeta(kind="top-level"), turns=_rewrite_every_time_turns()), SONNET_RATES)
    table = next(t for t in build_section(stats, billing_mode="api").tables if t.name == "ttl_addressable_share")
    note = " ".join(table.notes)
    assert "counts as broken. The clock didn't expire it." in note
    for sentence in note.replace(". ", ".\n").splitlines():
        assert len(sentence.split()) <= 25, sentence


def test_build_section_window_start_caveat_note_only_when_subagent_predates_window():
    import datetime as dt

    turns = [_turn(cache_creation_tokens=100, cc_5m=100)]

    # No caveat: no window_start given at all.
    stats = TtlStats()
    stats.add(
        TranscriptResult(
            meta=TranscriptMeta(kind="subagent", agent_type="claude-implementer", mtime_ns=0), turns=turns
        ),
        SONNET_RATES,
    )
    section = build_section(stats, billing_mode="api")
    assert not any("started before the window did" in note for note in section.notes)

    # No caveat: window_start given, but the subagent's mtime is inside it.
    recent_ns = int(dt.datetime(2026, 9, 15, tzinfo=dt.timezone.utc).timestamp() * 1_000_000_000)
    stats2 = TtlStats()
    stats2.add(
        TranscriptResult(
            meta=TranscriptMeta(kind="subagent", agent_type="claude-implementer", mtime_ns=recent_ns),
            turns=turns,
        ),
        SONNET_RATES,
    )
    section2 = build_section(stats2, billing_mode="api", window_start=dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc))
    assert not any("started before the window did" in note for note in section2.notes)

    # Caveat present: window_start given, subagent mtime predates it.
    old_ns = int(dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc).timestamp() * 1_000_000_000)
    stats3 = TtlStats()
    stats3.add(
        TranscriptResult(
            meta=TranscriptMeta(kind="subagent", agent_type="claude-implementer", mtime_ns=old_ns), turns=turns
        ),
        SONNET_RATES,
    )
    section3 = build_section(stats3, billing_mode="api", window_start=dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc))
    assert any("started before the window did" in note for note in section3.notes)

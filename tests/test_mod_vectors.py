"""The mod's pure logic (``plugin/hooks/lib/*.js``) gives the same answers
as the Python it mirrors.

The mod runs inside Claude Code and can't import Python, so
``pricing.js``, ``cache.js``, ``rebuild.js``, ``units.js`` and ``rest.js``
are ports. This builds vectors by calling the real Python functions
(``Pricing.resolve_model``, ``price_turn``, the status line's cache
countdown, ``recache.detect``, ``Units``, ``capture_hook._gate`` and
friends) on a spread of cases, writes ``{fn, args, expect}`` for each to a
file, and runs ``tests/js/mod_vectors.mjs`` over it under Node, which
prints every mismatch. ``tests/js/context.test.mjs`` covers the logic with
no Python twin (``context.js``, ``text.js``).

Both skip when Node isn't installed; CI installs it (``ci.yml``).
"""

from __future__ import annotations

import json
import math
import random
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from claudeglass import mod_settings, recache, statusline
from claudeglass.capture_catalogue import COACHING_THRESHOLDS
from claudeglass.hooks import capture_hook
from claudeglass.model import EventKind, Turn
from claudeglass.pricing import LongContextRule, ModelRates, Pricing, load_pricing, price_turn
from claudeglass.units import Units

from helpers import elasticity_with_slope

ROOT = Path(__file__).resolve().parent.parent
VECTORS_MJS = ROOT / "tests" / "js" / "mod_vectors.mjs"
CONTEXT_TEST_MJS = ROOT / "tests" / "js" / "context.test.mjs"
LIB = ROOT / "plugin" / "hooks" / "lib"

PRICING = load_pricing()


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node isn't installed")
    return node


def vec(fn: str, args: list, expect, note: str = "") -> dict:
    out = {"fn": fn, "args": args, "expect": expect}
    if note:
        out["note"] = note
    return out


# -- pricing --------------------------------------------------------------------


def _models_json(pricing: Pricing) -> dict:
    """The settings file's ``pricing.models``, as the Python writes it: the
    JS reads what ``mod_settings.pricing_block`` makes, not a copy built
    here that could hide what it leaves out."""
    return mod_settings.pricing_block(pricing)["models"]


#: Rate cards the packaged one doesn't have: a long-context rule by
#: multiplier and by explicit rates, written through the same block.
SYNTHETIC = Pricing(
    path="synthetic",
    version="test",
    currency="USD",
    source_url=None,
    retrieved=None,
    notes=None,
    sha256="",
    models={
        "multiplier": ModelRates(
            "multiplier", 3.0, 15.0, 3.75, 6.0, 0.3, long_context=LongContextRule(threshold_tokens=200_000, multiplier=2.0)
        ),
        "overrides": ModelRates(
            "overrides", 3.0, 15.0, 3.75, 6.0, 0.3,
            long_context=LongContextRule(threshold_tokens=100_000, overrides={"input": 6.0, "cache_write_5m": 7.5, "cache_read": 0.6}),
        ),
    },
)


def _resolve_ids(pricing: Pricing) -> list:
    ids: list = []
    for model_id in pricing.models:
        ids += [
            model_id,
            model_id + "[1m]",
            "us.anthropic." + model_id + "-v1:0",
            "eu.anthropic." + model_id,
            "anthropic." + model_id + "-v1:0",
            "us.anthropic." + model_id + "-v1:0[1m]",
            model_id + "@20261001",
            model_id + "-20261001",
            model_id + "-preview",
            model_id + "-5",
            model_id + "-10",
            model_id + "x",
            model_id.upper(),
        ]
    for alias in pricing.aliases:
        ids += [alias, alias + "[1m]", "anthropic." + alias]
    ids += [
        "claude-opus-4-10",
        "claude-opus-4-10[1m]",
        "claude-opus-4-1-20250805",
        "claude-opus-4-0",
        "claude-haiku-5-5",
        "claude-haiku-5-5[1m]",
        "claude-sonnet-5-5-20261001",
        "claude-sonnet-4-5@20250929",
        "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "<synthetic>",
        "",
        None,
        "unknown",
        "gpt-4",
        "claude",
        "[1m]",
        "anthropic.",
        "us.anthropic.",
        "-v1:0",
        "@20260101",
        "constructor",
        "__proto__",
    ]
    return ids


def _price_vectors() -> list[dict]:
    vectors: list[dict] = []
    models = _models_json(PRICING)
    for model_id in _resolve_ids(PRICING):
        resolved = PRICING.resolve_model(model_id)
        vectors.append(vec("resolveModel", [models, model_id], resolved.canonical_id if resolved else None))

    usages = [
        {"input_tokens": 1200, "output_tokens": 800, "cache_read_input_tokens": 90000, "cache_creation_input_tokens": 3000},
        {"input_tokens": 3, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 150000},
        {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        {"input_tokens": 500000, "output_tokens": 64000, "cache_read_input_tokens": 700000, "cache_creation_input_tokens": 120000},
        {"input_tokens": 7},
    ]
    rate_cards = [
        (rates, _models_json(pricing)[model_id]) for pricing in (PRICING, SYNTHETIC) for model_id, rates in pricing.models.items()
    ]
    # The rule reaches the file, or the JS would price without it.
    assert sum("long_context" in card for _, card in rate_cards) >= 2
    for rates, rates_json in rate_cards:
        for usage in usages:
            for ttl in (300, 900, 3600, 7200):
                write = usage.get("cache_creation_input_tokens", 0)
                ctx = usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0) + write
                turn = Turn(
                    input_tokens=usage.get("input_tokens", 0),
                    output_tokens=usage.get("output_tokens", 0),
                    cache_read_tokens=usage.get("cache_read_input_tokens", 0),
                    cache_creation_tokens=write,
                    cc_5m=write if ttl < 3600 else 0,
                    cc_1h=write if ttl >= 3600 else 0,
                    ctx=ctx,
                )
                vectors.append(vec("priceUsage", [rates_json, usage, ttl], price_turn(turn, rates).total))
        # What it costs to send the next message over a context.
        for context in (0, 1, 42_000, 150_000, 999_999):
            for ttl in (300, 3600):
                warm = Turn(cache_read_tokens=context, ctx=context)
                cold = Turn(
                    cache_creation_tokens=context,
                    cc_5m=context if ttl < 3600 else 0,
                    cc_1h=context if ttl >= 3600 else 0,
                    ctx=context,
                )
                vectors.append(vec("sendCost", [rates_json, context, True, ttl], price_turn(warm, rates).total))
                vectors.append(vec("sendCost", [rates_json, context, False, ttl], price_turn(cold, rates).total))
    return vectors


# -- cache countdown -----------------------------------------------------------------


_LAST_TS = datetime(2026, 10, 9, 12, 0, 0, 123000, tzinfo=timezone.utc)
_LAST_MS = int(_LAST_TS.timestamp() * 1000)


def _assistant_line(ttl: int) -> dict:
    cache_creation = {"ephemeral_1h_input_tokens": 100 if ttl == 3600 else 0, "ephemeral_5m_input_tokens": 0 if ttl == 3600 else 100}
    return {
        "type": "assistant",
        "timestamp": _LAST_TS.isoformat().replace("+00:00", "Z"),
        "message": {"usage": {"cache_creation": cache_creation}},
    }


def _cache_vectors(tmp_path: Path) -> list[dict]:
    vectors: list[dict] = []
    # Seconds after the last reply; halves test the status line's round().
    elapsed = [
        0, 0.001, 0.5, 1, 1.5, 2.5, 12.5, 59, 59.5, 60, 61, 100.25, 287.5, 298.5, 299, 299.4, 299.5, 299.6, 299.999,
        300, 300.001, 301, 600, -30, -400, 1800.5, 3000, 3540.5, 3599, 3599.5, 3600, 3601, 7200,
    ]
    for ttl in (300, 3600):
        transcript = tmp_path / f"transcript-{ttl}.jsonl"
        transcript.write_text(json.dumps(_assistant_line(ttl)) + "\n", encoding="utf-8")
        for seconds in elapsed:
            now_ms = _LAST_MS + round(seconds * 1000)
            now = _LAST_TS + timedelta(milliseconds=round(seconds * 1000))
            line = statusline._fmt_cache_estimate({"transcript_path": str(transcript)}, now, 300)
            vectors.append(vec("cacheEstimate", [_LAST_MS, now_ms, ttl], line, f"{seconds}s after, {ttl}s lifetime"))
            remaining = statusline._cache_remaining_s({}, _assistant_line(ttl), now)
            assert remaining is not None
            state = {
                "isWarm": remaining > 0,
                # The status line works in float epoch seconds, good to about a
                # ten-thousandth of a millisecond: rounded past that noise.
                "remainingMs": round(max(0.0, remaining * 1000), 3),
                "fractionLeft": round(max(0.0, remaining) / ttl, 7),
            }
            vectors.append(vec("cacheState", [_LAST_MS, now_ms, ttl], state))
    for seconds in [0, 0.2, 0.4, 0.5, 0.6, 1, 1.5, 2.5, 3.5, 59.4, 59.5, 59.6, 60, 61, 287.5, 299.9, 300, 600, 3599.5, 3600, 3725, 7325, -5, -0.2]:
        vectors.append(vec("formatClock", [seconds * 1000], statusline._format_mmss(seconds), f"{seconds}s"))

    for value in [300, 3600, "5m", "1h", "300", "3600s", " 1H ", 90, "90", "abc", True, False, None, 12.7, "12.7", -5, "", 0, "0"]:
        vectors.append(vec("parseTtl", [value], statusline._parse_ttl_value(value), repr(value)))
    payloads = [
        {},
        {"prompt_cache": {"cache_ttl": "1h"}},
        {"prompt_cache": {"cache_ttl": "5m"}, "cache_ttl": "1h"},
        {"prompt_cache": {"cache_ttl": "nonsense"}, "cache_ttl": 3600},
        {"prompt_cache": "x", "cache_ttl": "1h"},
        {"cache_ttl": 600},
        {"cache_ttl": None},
        None,
    ]
    for payload in payloads:
        vectors.append(vec("resolveTtl", [payload, None], statusline.resolve_effective_ttl(payload or {}), json.dumps(payload)))
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text('default_ttl = "1h"\n', encoding="utf-8")
    for payload in payloads[:3]:
        vectors.append(vec("resolveTtl", [payload, "1h"], statusline.resolve_effective_ttl(payload, config_dir), json.dumps(payload)))
    return vectors


# -- cache rebuilds ----------------------------------------------------------------


_SIGNATURE_CAUSE = {
    "limit-expiry": "limit",
    "post-compaction": "compaction",
    "full-expiry": "expiry",
    "prefix-invalidated": "prefix",
}

_T0_MS = 1_800_000_000_000


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _request(turn: Turn, ms: int, after_compaction: bool, after_limit: bool, first: bool = False) -> dict:
    out = {
        "ts": ms,
        "ctx": turn.ctx,
        "cacheRead": turn.cache_read_tokens,
        "cacheWrite": turn.cache_creation_tokens,
        "afterCompaction": after_compaction,
        "afterLimitWait": after_limit,
        "model": turn.model,
    }
    if first:
        out["first"] = True
    return out


def _sequence(rng: random.Random) -> list[tuple[Turn, int, bool, bool]]:
    """One synthetic session: (turn, ms, after_compaction, after_limit)
    for each reply, the kinds of reply a rebuild can follow."""
    length = rng.randint(2, 8)
    ms = _T0_MS
    ctx = rng.choice([8_000, 30_000, 60_000, 120_000])
    out: list[tuple[Turn, int, bool, bool]] = []
    first_read = 0
    prev: Turn | None = None
    for index in range(length):
        flags = (False, False)
        gap_s: float | None = None
        if index:
            gap_s = rng.choice([5, 20, 90, 250, 299, 300, 301, 400, 1200, 3599, 3600, 3601, 5000])
            ms += gap_s * 1000
            flags = (rng.random() < 0.15, rng.random() < 0.1)
        inp = rng.randint(1, 500)
        if index == 0:
            read = rng.choice([0, 14_000, 40_000])
        else:
            assert prev is not None
            ctx = int(rng.choice([ctx + rng.randint(0, 8_000), ctx * 2, ctx + 500, ctx // 3, ctx]))
            mode = rng.choice(["warm", "cold", "shared", "partial", "warm"])
            if mode == "warm":
                read = prev.cache_read_tokens + prev.cache_creation_tokens
            elif mode == "cold":
                read = rng.choice([0, 500, 1_999, 2_500])
            elif mode == "shared":
                read = first_read + rng.choice([0, 1_000, 3_000, 3_500])
            else:
                read = int(ctx * rng.choice([0.1, 0.19, 0.2, 0.21, 0.5]))
        ctx = max(ctx, inp + 1)
        read = max(0, min(read, ctx - inp))
        write = ctx - inp - read
        one_hour = rng.random() < 0.3
        turn = Turn(
            message_id=f"m{index}",
            turn_index=index + 1,
            ts=_iso(ms),
            gap_s=gap_s,
            model="claude-sonnet-5-5",
            input_tokens=inp,
            cache_read_tokens=read,
            cache_creation_tokens=write,
            cc_5m=0 if one_hour else write,
            cc_1h=write if one_hour else 0,
            ctx=ctx,
            gap_cause="limit" if flags[1] else None,
            preceding_event_kinds=(EventKind.COMPACT_BOUNDARY,) if flags[0] else (),
        )
        if index == 0:
            first_read = read
        out.append((turn, ms, flags[0], flags[1]))
        prev = turn
    return out


def _rebuild_vectors() -> list[dict]:
    rng = random.Random(20261009)
    vectors: list[dict] = []
    variants = [
        (recache.RecacheThresholds(), {}),
        (recache.RecacheThresholds(ctx_floor=10_000, cr_ratio=0.3, full_expiry_cr=5_000), {"ctx_floor": 10_000, "cr_ratio": 0.3, "full_expiry_cr": 5_000}),
    ]
    for count in range(500):
        th, th_json = variants[count % 2]
        # From the first reply (the shared start is known), or from the
        # second, as when the mod starts mid-session (it isn't).
        start = 0 if count % 4 < 2 else 1
        steps = _sequence(rng)[start:]
        detected = {t.message_id: t.recache_signature for t in recache.detect([t for t, *_ in steps], th)}
        carried: float | None = None
        shared = steps[0][0].cache_read_tokens if start == 0 else None
        for position, (turn, ms, compacted, limited) in enumerate(steps):
            signature = detected.get(turn.message_id)
            expect = None
            if signature is not None:
                cause = _SIGNATURE_CAUSE[signature]
                expect = {"cause": cause, "tokens": turn.cache_creation_tokens, "expected": cause in ("limit", "compaction")}
            if position:
                prev_turn, prev_ms, prev_compacted, prev_limited = steps[position - 1]
                prev_request = _request(prev_turn, prev_ms, prev_compacted, prev_limited)
                vectors.append(vec("detectRebuild", [prev_request, _request(turn, ms, compacted, limited), th_json, carried, shared], expect))
            else:
                # No previous request: the session's first reply (never a
                # rebuild), or the first the mod saw after a resume, which
                # the read-ratio test still judges.
                first = start == 0
                vectors.append(
                    vec("detectRebuild", [None, _request(turn, ms, compacted, limited, first), th_json, None, shared], expect, f"no previous, first={first}")
                )
            # The lifetime the previous reply's cache had, carried forward.
            carried = recache.write_ttl_s(turn) or carried
            ttl = recache.write_ttl_s(turn)
            vectors.append(vec("writeTtlSeconds", [{"cacheWrite": turn.cache_creation_tokens}, 300 if turn.cc_1h <= turn.cc_5m else 3600], ttl))
    for gap in (None, 0, 299.9, 300, 3600, 7200):
        for ttl in (None, 300, 3600):
            vectors.append(vec("expiredByClock", [gap, ttl], recache.expired_by_clock(gap, ttl)))
    return vectors


# -- amounts -------------------------------------------------------------------------


_AMOUNTS = [
    0.0, -1.0, -0.001, 0.0001, 0.004, 0.005, 0.01, 0.125, 0.25, 0.375, 0.38, 0.625, 1.0, 1.125, 1.5, 2.0, 2.5, 9.995,
    12.345, 45.67, 99.99, 100.0, 199.0, 248.7, 1000.0, 1234567.891, 987654321.125,
]


def _billings() -> list[tuple[Units, dict]]:
    out: list[tuple[Units, dict]] = [
        (Units(billing_mode="api"), {"mode": "api"}),
        (Units(billing_mode="api"), {"mode": "api", "share_per_usd": 0.5}),
        (Units(billing_mode="subscription"), {"mode": "subscription", "share_per_usd": None}),
        (Units(billing_mode="subscription"), {"mode": "subscription"}),
        (Units(billing_mode="api", currency="EUR"), {"mode": "api", "currency": "EUR"}),
    ]
    for slope in (0.5, 0.01, 1.0, 0.0003, 2.0, 8.0):
        out.append(
            (
                Units(billing_mode="subscription", elasticity=elasticity_with_slope(slope)),
                {"mode": "subscription", "share_per_usd": slope},
            )
        )
    return out


def _units_vectors() -> list[dict]:
    vectors: list[dict] = []
    for units, billing in _billings():
        for usd in _AMOUNTS:
            note = f"{usd} {billing}"
            money = units.money(usd)
            vectors.append(vec("formatAmount", [usd, billing, None], money.primary if money else units.money_text(usd), note))
            periodic = units.money(usd, period="over 30 days")
            if periodic is not None:
                vectors.append(vec("formatAmount", [usd, billing, {"period": "over 30 days"}], periodic.primary, note))
                vectors.append(vec("amountParts", [usd, billing, {"period": "over 30 days"}], {"primary": periodic.primary, "secondary": periodic.secondary}, note))
            else:
                vectors.append(vec("amountParts", [usd, billing, None], None, note))
            vectors.append(vec("amountText", [usd, billing, None], units.money_text(usd), note))
            vectors.append(vec("formatAmountCell", [usd, billing, None], units.money_cell(usd), note))
    return vectors


# -- rest rules ----------------------------------------------------------------------


_REST_KINDS = ["cold_return", "plan_fresh", "plan_fresh_early", "split_run:abc", "big_paste", "drip_feed"]
_REST_STAKES = [0, 1, 1_000, 50_000, 80_000, 120_000, 200_000, 400_000]
_SESSION = "s"


def _rows(state: dict) -> dict:
    hints = state.get("sessions", {}).get(_SESSION, {}).get("hints", {})
    return {kind: dict(row) for kind, row in hints.items()}


def _shown(state: dict, kind: str, stake: float, now_ts: float, th: dict, once: bool) -> None:
    """What ``capture_hook.coaching_for`` does when a hint shows, line for
    line (the gate, the stamp, the others at rest)."""
    hint = kind.split(":", 1)[0]
    if hint == "cold_return":
        capture_hook._stamp(state, _SESSION, kind, 0, now_ts, th, rest=th["cold_rest_hours"] * 3600, once=once)
    else:
        capture_hook._stamp(state, _SESSION, kind, stake, now_ts, th, once=once)
    if hint in capture_hook._FRESH_START_HINTS:
        for other in capture_hook._FRESH_START_HINTS:
            if other != hint:
                capture_hook._rest_beside(state, _SESSION, other, now_ts, th)


def _rest_vectors() -> list[dict]:
    rng = random.Random(1009)
    vectors: list[dict] = []
    variants = [dict(COACHING_THRESHOLDS), {**COACHING_THRESHOLDS, "cooldown_minutes": 10, "rearm_factor": 2, "max_backoff": 2, "cold_rest_hours": 1}]
    for count in range(120):
        th = variants[count % 2]
        state: dict = {}
        now = 1_800_000_000.0
        steps: list[dict] = []
        expects: list[dict] = []
        for _ in range(rng.randint(3, 14)):
            now += rng.choice([0, 30, 600, 1_799, 1_800, 1_801, 3_600, 5_400])
            kind = rng.choice(_REST_KINDS)
            stake = rng.choice(_REST_STAKES)
            op = rng.choice(["may", "stamp", "beside", "show", "show", "show"])
            once = rng.random() < 0.15
            step: dict = {"op": op, "kind": kind, "stake": stake, "now": now}
            expect: dict = {}
            if op == "may":
                expect["may"] = capture_hook._gate(state, _SESSION, kind, stake, now, th)
            elif op == "stamp":
                rest = rng.choice([None, None, 900, 43_200])
                step.update({"rest": rest, "once": once})
                capture_hook._stamp(state, _SESSION, kind, stake, now, th, rest=rest, once=once)
            elif op == "beside":
                capture_hook._rest_beside(state, _SESSION, kind, now, th)
            else:
                step["once"] = once
                shown = capture_hook._gate(state, _SESSION, kind, stake, now, th)
                expect["shown"] = shown
                if shown:
                    _shown(state, kind, stake, now, th, once)
            expect["hints"] = _rows(state)
            steps.append(step)
            expects.append(expect)
        vectors.append(vec("restSequence", [th, steps], expects))
    return vectors


# -- the run -------------------------------------------------------------------------


@pytest.fixture(scope="module")
def vector_run(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """Build every vector, run them under Node once, and keep what came back."""
    node = _node()
    tmp_path = tmp_path_factory.mktemp("mod_vectors")
    groups = {
        "pricing": _price_vectors(),
        "cache": _cache_vectors(tmp_path),
        "rebuild": _rebuild_vectors(),
        "units": _units_vectors(),
        "rest": _rest_vectors(),
    }
    vectors = [v for group in groups.values() for v in group]
    path = tmp_path / "vectors.json"
    # No NaN or infinity in any vector: JSON can't carry them.
    for v in vectors:
        assert not _has_non_finite(v), v
    path.write_text(json.dumps(vectors), encoding="utf-8")
    # A cold Node on a busy Windows runner has taken over 30 s to start.
    done = subprocess.run(
        [node, str(VECTORS_MJS), str(path)],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180, cwd=ROOT,
    )
    lines = done.stdout.strip().splitlines()
    summary = json.loads(lines[-1].removeprefix("SUMMARY ")) if lines and lines[-1].startswith("SUMMARY ") else {"counts": {}, "failed": {}}
    return {"done": done, "summary": summary, "groups": groups, "vectors": vectors}


def _has_non_finite(value) -> bool:
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, dict):
        return any(_has_non_finite(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_has_non_finite(v) for v in value)
    return False


def _group_functions(run: dict, group: str) -> set[str]:
    return {v["fn"] for v in run["groups"][group]}


def _assert_group_agrees(run: dict, group: str, at_least: int) -> None:
    done = run["done"]
    counts, failed = run["summary"]["counts"], run["summary"]["failed"]
    fns = _group_functions(run, group)
    ran = sum(counts.get(fn, 0) for fn in fns)
    assert ran == len(run["groups"][group]), f"Node ran {ran} of {len(run['groups'][group])} {group} vectors\n{done.stdout}{done.stderr}"
    assert ran >= at_least
    mismatched = {fn: failed[fn] for fn in fns if failed.get(fn)}
    assert not mismatched, f"{mismatched}\n{done.stdout}{done.stderr}"


def test_node_ran_every_vector(vector_run: dict) -> None:
    done = vector_run["done"]
    assert done.returncode in (0, 1), done.stderr
    assert "SUMMARY" in done.stdout, done.stdout + done.stderr
    assert sum(vector_run["summary"]["counts"].values()) == len(vector_run["vectors"])


def test_pricing_js_agrees_with_pricing_py(vector_run: dict) -> None:
    _assert_group_agrees(vector_run, "pricing", 1000)


def test_cache_js_agrees_with_the_status_line(vector_run: dict) -> None:
    _assert_group_agrees(vector_run, "cache", 150)


def test_rebuild_js_agrees_with_recache_detect(vector_run: dict) -> None:
    _assert_group_agrees(vector_run, "rebuild", 1000)
    # The vectors reach every outcome, or they prove less than they seem to.
    outcomes = {
        (v["expect"] or {}).get("cause")
        for v in vector_run["groups"]["rebuild"]
        if v["fn"] == "detectRebuild"
    }
    assert outcomes == {None, "limit", "compaction", "expiry", "prefix"}
    # So do the replies with no known previous request: the first (never a
    # rebuild) and the first seen after a resume (judged by the read ratio).
    unknown_prev = [v for v in vector_run["groups"]["rebuild"] if v["fn"] == "detectRebuild" and v["args"][0] is None]
    resumed = [v["expect"] for v in unknown_prev if not v["args"][1].get("first")]
    assert any(resumed) and not all(resumed)
    assert all(v["expect"] is None for v in unknown_prev if v["args"][1].get("first"))


def test_units_js_agrees_with_units_py(vector_run: dict) -> None:
    _assert_group_agrees(vector_run, "units", 1000)


def test_rest_js_agrees_with_the_capture_hook_gate(vector_run: dict) -> None:
    _assert_group_agrees(vector_run, "rest", 100)
    # Some steps hold a hint back and some let it show, or the gate went untested.
    results = [
        step.get("shown", step.get("may"))
        for v in vector_run["groups"]["rest"]
        for step in v["expect"]
    ]
    assert True in results and False in results


def test_js_logic_with_no_python_twin_passes() -> None:
    """``context.js``, ``text.js`` and the rest of lib's own cases
    (``tests/js/context.test.mjs``, Node's test runner)."""
    node = _node()
    done = subprocess.run(
        [node, "--test", str(CONTEXT_TEST_MJS)],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180, cwd=ROOT,
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_every_lib_module_is_plain_and_self_contained() -> None:
    """No imports outside ``plugin/hooks/lib/``, no dynamic import, no JSON
    import, no Node or DOM globals: the mod's engine loads only script
    files and the lib runs in whatever it compiles them for."""
    import re

    forbidden = re.compile(
        r"\b(require|process|window|document|localStorage|fetch|setTimeout|setInterval|Buffer|__dirname|__filename)\b\s*[\.(]"
        r"|\bimport\s*\(|\bDate\.now\b|\bnew Date\b|\bMath\.random\b|\bperformance\.now\b"
    )
    modules = sorted(LIB.glob("*.js"))
    assert modules, "no lib modules"
    for module in modules:
        text = module.read_text(encoding="utf-8")
        for match in re.finditer(r"""(?:import|export)\s[^;]*?from\s+["']([^"']+)["']""", text):
            target = match.group(1)
            assert target.startswith("./") and target.endswith(".js") and (LIB / target[2:]).exists(), (module.name, target)
        assert not re.search(r"""import\s+["']""", text), module.name
        # Comments may say what the module doesn't do; look at code only.
        code = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        code = re.sub(r"(?m)^\s*//.*$", "", code)
        found = forbidden.search(code)
        assert not found, (module.name, found.group(0))

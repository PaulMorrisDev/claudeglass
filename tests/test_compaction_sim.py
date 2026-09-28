"""Tests for the ``autoCompactWindow`` sweep
(``src/claudeglass/compaction_sim.py``).

Replay arithmetic is exercised on hand-built ``model.Turn`` instances
(the ``_turn`` helper below, matching ``test_ttl.py``'s convention) with
hand-computed costs at the packaged Sonnet 5 rates (input 2.0, output
10.0, cache_write_5m 2.5, cache_write_1h 4.0, cache_read 0.2 per million
tokens; see ``_synthetic_20_turn_transcript``'s docstring for the by-hand
derivation). The fidelity and rule tests build a small hand-crafted
``TranscriptResult`` with a real ``COMPACT_BOUNDARY`` event rather than
going through the real parser, since only the event/turn timestamp
correlation and cost arithmetic are under test here.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from claudeglass import model
from claudeglass.compaction_sim import (
    ASSUMPTIONS,
    CANDIDATE_WINDOWS,
    RULES,
    CompactionSimThresholds,
    _rediscovery_allowance_usd_used,
    _replay_transcript,
    _shrunk_cost,
    _summary_request_cost,
    _Shape,
    build_section,
    simulate_compaction_windows,
)
from claudeglass.model import Event, EventKind, ReportModel, ReportMeta, TranscriptMeta, TranscriptResult
from claudeglass.pricing import load_pricing, price_turn
from claudeglass.units import Units

from helpers import assert_privacy, elasticity_with_slope

PRICING = load_pricing()
SONNET_RATES = PRICING.resolve_model("claude-sonnet-5")


def _turn(**overrides) -> model.Turn:
    """Build a priced ``Turn`` with sane zero defaults, overridable per
    field -- same convention as ``test_ttl.py``'s ``_turn``."""
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


def _top_level_transcript(session_id: str, turns: list[model.Turn], events: list[Event] | None = None) -> TranscriptResult:
    return TranscriptResult(
        meta=TranscriptMeta(path=f"{session_id}.jsonl", kind="top-level", session_id=session_id),
        turns=turns,
        events=events or [],
    )


def _ts(minute: int) -> str:
    return f"2026-09-18T12:{minute:02d}:00.000Z"


def _synthetic_20_turn_transcript() -> list[model.Turn]:
    """20 priced turns with linearly growing ctx and NO real compaction
    event: turn i (1-indexed) carries K=20,000 new cache-creation
    tokens, reads back everything written by every earlier turn
    (cache_read_i = (i-1)*K), so ctx_i = i*K, up to ctx_20 = 400,000.
    input/output tokens are 0 throughout so every dollar is cache-write
    or cache-read, keeping the by-hand arithmetic in this file's tests
    tractable. All cc_5m = cache_creation_tokens, cc_1h = 0 (every write
    priced at the 5-minute rate).
    """
    turns = []
    k = 20_000
    for i in range(1, 21):
        ctx = i * k
        cache_read = (i - 1) * k
        turns.append(
            _turn(
                message_id=f"msg_{i}",
                request_id=f"req_{i}",
                turn_index=i,
                ts=_ts(i),
                cache_creation_tokens=k,
                cache_read_tokens=cache_read,
                cc_5m=k,
                cc_1h=0,
                ctx=ctx,
            )
        )
    return turns


def _plateau_transcript() -> list[model.Turn]:
    """42 priced turns that reach a large context early and stay there:
    turn 1 writes an 80,000-token starting context, turn 2 writes
    200,000 more (ctx 280,000), and turns 3-42 read those 280,000 back
    and add nothing. Every write is at the 5-minute rate; input and
    output are 0.

    Observed, by hand (write 2.5/1e6, read 0.2/1e6 per token):
      turn 1: 80,000 written                   -> 0.2
      turn 2: 200,000 written + 80,000 read    -> 0.516
      turns 3-42: 280,000 read, 40 times       -> 2.24
      total = 2.956
    """
    turns = [
        _turn(message_id="msg_1", turn_index=1, ts=_ts(0), ctx=80_000, cache_creation_tokens=80_000, cc_5m=80_000),
        _turn(
            message_id="msg_2",
            turn_index=2,
            ts=_ts(1),
            ctx=280_000,
            cache_creation_tokens=200_000,
            cache_read_tokens=80_000,
            cc_5m=200_000,
        ),
    ]
    for i in range(3, 43):
        turns.append(
            _turn(message_id=f"msg_{i}", turn_index=i, ts=_ts(i), ctx=280_000, cache_read_tokens=280_000)
        )
    return turns


# -- replay arithmetic ------------------------------------------------------


def test_replay_transcript_caches_model_resolution_by_string():
    """SURV-9: ``lookup(turn.model)`` used to run uncached on every turn
    of every window replayed, even though a transcript only ever uses a
    handful of distinct model strings. It's now cached by model string,
    local to one ``_replay_transcript`` call (the same pattern as
    ``habits._Rates._resolve``) -- so a 10-turn, 2-model transcript
    resolves each model once, not ten times, and the cache must not
    change what gets priced: the result with a call-counting ``lookup``
    matches one built straight from ``Pricing.resolve_model`` with no
    wrapper at all."""
    models = ["claude-sonnet-5", "claude-opus-5-5"]
    turns = [
        _turn(
            message_id=f"msg_{i}", turn_index=i, ts=_ts(i), model=models[i % 2],
            ctx=i * 10_000, cache_creation_tokens=10_000, cache_read_tokens=(i - 1) * 10_000, cc_5m=10_000,
        )
        for i in range(1, 11)
    ]
    calls: list[str] = []

    def counting_lookup(model_id: str):
        calls.append(model_id)
        return PRICING.resolve_model(model_id)

    result = _replay_transcript(turns, counting_lookup, None, _Shape(), {})
    reference = _replay_transcript(turns, PRICING.resolve_model, None, _Shape(), {})

    assert sorted(set(calls)) == sorted(models)
    assert len(calls) == 2  # one resolve per distinct model, not per turn
    assert result == reference
    assert result.cost > 0


@pytest.mark.parametrize("model_id", sorted(PRICING.models))
@pytest.mark.parametrize("dropped", [0.0, 50_000.0, 250_000.0])
def test_shrunk_turns_price_the_same_as_a_replaced_turn(model_id, dropped):
    """The replay prices changed turns through a light stand-in rather
    than ``dataclasses.replace``. Fast mode, the long-context rule, a
    data-residency geo and web searches must all price exactly as on a
    real ``Turn`` with the same changes."""
    rates = PRICING.resolve_model(model_id)
    turn = _turn(
        model=model_id, speed="fast", inference_geo="us", web_search_requests=3,
        input_tokens=900, output_tokens=4_000, thinking_tokens=1_000,
        ctx=400_000, cache_read_tokens=200_000, cache_creation_tokens=199_100, cc_5m=150_000, cc_1h=49_100,
    )
    ctx = max(0, int(round(turn.ctx - dropped)))
    read = max(0, int(round(turn.cache_read_tokens - dropped)))
    keep = max(0.0, (turn.cache_creation_tokens - max(0.0, dropped - turn.cache_read_tokens)) / turn.cache_creation_tokens)
    shrunk = replace(
        turn, ctx=ctx, cache_read_tokens=read, cc_5m=int(round(turn.cc_5m * keep)), cc_1h=int(round(turn.cc_1h * keep))
    )

    assert _shrunk_cost(turn, rates, dropped) == price_turn(shrunk, rates).total
    assert _summary_request_cost(turn, rates, dropped, 20_000.4) == price_turn(
        replace(shrunk, output_tokens=20_000, thinking_tokens=0), rates
    ).total


def test_window_none_has_zero_synthetic_compactions_and_matches_true_observed_cost():
    """window=None never opens the synthetic-compaction guard, so its
    row's cost must equal the sum of pricing every turn's own real,
    unmodified values -- the module docstring's "no candidate window"
    identity. By hand: sum_{i=1..20} [K*2.5 + (i-1)*K*0.2] / 1e6
    = 20*0.05 + 0.004*sum_{k=0..19}k = 1.0 + 0.004*190 = 1.76.
    """
    turns = _synthetic_20_turn_transcript()
    tr = _top_level_transcript("sess-synthetic", turns)
    stats = simulate_compaction_windows([tr], SONNET_RATES, {})
    rows = {r.window: r for r in stats.by_window("top-level")}

    assert rows[None].compactions == 0
    assert rows[None].cost == pytest.approx(1.76)
    # The observed cost baseline every other row's delta is measured
    # against must be this same true observed cost.
    assert rows[None].observed_cost == pytest.approx(1.76)
    assert rows[None].delta_usd == pytest.approx(0.0)


def test_small_window_hand_computed_compaction_count_and_cost():
    """window=100,000 on the 20-turn transcript (turn i has
    ctx = i*20,000: a 20,000-token write plus the rest read) triggers
    four synthetic compactions. With no real compact_boundary event
    anywhere in this corpus, every measured shape falls back to its
    default: a 20,000-token summary, no trigger reserve, nothing of the
    starting context still cached. Each summary resets the context to
    the starting context (turn 1's 20,000) plus the summary: 40,000.

    A compaction charges the summary request (the triggering turn as it
    was, with the summary as output) and the reply after it writing its
    whole new context. By hand (write 2.5/1e6, read 0.2/1e6, output
    10/1e6 per token):
      turns 1-5 (as observed):            0.05 .. 0.066          -> 0.29
      turn 6 (ctx 120,000 > 100,000):
        summary request: 20,000 written + 100,000 read + 20,000 out -> 0.27
        reply: 40,000 written                                     -> 0.1
      turns 7-9: 20,000 written + 40,000/60,000/80,000 read      -> 0.186
      turns 10, 14, 18: the same compaction at 120,000            -> 0.37 each
      turns 11-13, 15-17: as turns 7-9                            -> 0.186 each
      turns 19-20: as turns 7-8                                   -> 0.12
      total = 0.29 + 4*0.37 + 3*0.186 + 0.12 = 2.448

    More than the observed 1.76: on a context this small, each summary's
    own output costs more than the reads it saves.
    """
    turns = _synthetic_20_turn_transcript()
    tr = _top_level_transcript("sess-synthetic", turns)
    stats = simulate_compaction_windows([tr], SONNET_RATES, {})
    rows = {r.window: r for r in stats.by_window("top-level")}

    row = rows[100_000]
    assert row.compactions == 4
    assert row.cost == pytest.approx(2.448)
    assert row.observed_cost == pytest.approx(1.76)
    # Sign convention: candidate - observed, negative = cheaper.
    assert row.delta_usd == pytest.approx(2.448 - 1.76)
    assert row.saving_usd == 0.0


def test_a_summary_keeps_the_starting_context():
    """The plateau transcript at window=100,000: one compaction at turn 2
    (280,000 > 100,000), after which every reply carries the 80,000-token
    starting context plus the 20,000-token summary, not 20,000 alone.

    By hand: turn 1 0.2; turn 2's summary request 0.516 + 20,000 output
    (0.2) = 0.716; the reply writing 100,000 = 0.25; turns 3-42 each
    read 100,000 = 0.02, 40 times = 0.8. Total 1.966, 0.99 below the
    observed 2.956. The context stays at 100,000, never above the
    window, so no second summary fires.
    """
    tr = _top_level_transcript("sess-plateau", _plateau_transcript())
    stats = simulate_compaction_windows([tr], SONNET_RATES, {})
    rows = {r.window: r for r in stats.by_window("top-level")}

    assert rows[None].cost == pytest.approx(2.956)
    assert rows[100_000].compactions == 1
    assert rows[100_000].cost == pytest.approx(1.966)
    assert rows[100_000].mean_ctx == pytest.approx((80_000 + 100_000 + 40 * 100_000) / 42)
    # Windows at or above the plateau never summarise.
    assert rows[300_000].compactions == 0
    assert rows[300_000].cost == pytest.approx(2.956)


def test_a_summary_that_would_not_shrink_the_context_is_skipped():
    """A 150,000-token starting context plus a 20,000-token summary is
    larger than the 160,000 the session reaches, so no window summarises."""
    turns = [
        _turn(turn_index=1, ts=_ts(0), ctx=150_000, cache_creation_tokens=150_000, cc_5m=150_000),
        _turn(turn_index=2, ts=_ts(1), ctx=160_000, cache_creation_tokens=10_000, cache_read_tokens=150_000, cc_5m=10_000),
    ]
    stats = simulate_compaction_windows([_top_level_transcript("sess-big-start", turns)], SONNET_RATES, {})
    rows = {r.window: r for r in stats.by_window("top-level")}
    assert rows[100_000].compactions == 0
    assert rows[100_000].cost == pytest.approx(rows[None].cost)


def test_the_trigger_reserve_is_measured_from_real_auto_compactions():
    """A real auto compaction at 267,000 in a session configured for
    300,000 puts the trigger 33,000 below each window, so the plateau
    session (280,000) summarises under a 300,000 window."""
    measured = _top_level_transcript(
        "sess-measured",
        [_turn(turn_index=1, ts=_ts(0), ctx=10_000, cache_creation_tokens=10_000, cc_5m=10_000)],
        # After its only turn: measured, but not a compaction this replay prices.
        [Event(kind=EventKind.COMPACT_BOUNDARY, ts=_ts(30), pre_tokens=267_000, post_tokens=20_000, trigger="auto")],
    )
    plateau = _top_level_transcript("sess-plateau", _plateau_transcript())
    stats = simulate_compaction_windows([measured, plateau], SONNET_RATES, {"sess-measured": 300_000})
    assert stats.shape.trigger_reserve == 33_000
    assert "trigger_reserve" not in stats.defaults
    rows = {r.window: r for r in stats.by_window("top-level")}
    assert rows[300_000].compactions == 1
    assert rows[400_000].compactions == 0

    no_window = simulate_compaction_windows([measured, plateau], SONNET_RATES, {})
    assert "trigger_reserve" in no_window.defaults
    assert {r.window: r for r in no_window.by_window("top-level")}[300_000].compactions == 0


def test_the_cached_share_of_the_starting_context_is_measured_from_real_compactions():
    """The reply after the fidelity transcript's real compaction reads
    25,000 of its 50,000-token starting context from cache: a share of
    0.5."""
    tr = _fidelity_transcript()
    tr.turns[3] = replace(tr.turns[3], cache_read_tokens=25_000)
    stats = simulate_compaction_windows([tr], SONNET_RATES, {})
    assert stats.shape.cached_prefix_share == pytest.approx(0.5)
    assert stats.shape.summary_tokens == 30_000
    assert not {"summary_tokens", "cached_prefix_share"} & stats.defaults


def test_a_simulated_summary_request_outputs_what_the_real_ones_did():
    """The parser estimated the real summary request at 12,000 output
    tokens (twice the summary it kept), under the 30,000 tokens the
    summary left behind: a simulated request outputs 12,000 too."""
    tr = _fidelity_transcript()
    estimate = _turn(turn_index=4, ts="2026-09-18T12:02:00.000Z", ctx=195_000, cache_read_tokens=190_000,
                     input_tokens=5_000, output_tokens=12_000, is_synthetic=True, estimated="compaction")
    tr.turns[3:3] = [estimate]
    stats = simulate_compaction_windows([tr], SONNET_RATES, {})
    assert stats.shape.summary_tokens == 30_000
    assert stats.shape.summary_output_tokens == 12_000
    assert "summary_output_tokens" not in stats.defaults

    without = simulate_compaction_windows([_fidelity_transcript()], SONNET_RATES, {})
    assert without.shape.summary_output_tokens == without.shape.summary_tokens
    assert "summary_output_tokens" in without.defaults


def test_each_simulated_summary_request_is_priced_at_the_summary_output():
    turns = _plateau_transcript()
    real = _replay_transcript(turns, PRICING.resolve_model, 200_000, _Shape(summary_output_tokens=5_000), {})
    summary_sized = _replay_transcript(turns, PRICING.resolve_model, 200_000, _Shape(), {})

    assert real.compactions == summary_sized.compactions == 1
    assert summary_sized.cost - real.cost == pytest.approx(15_000 * SONNET_RATES.rates.output / 1e6)


def test_every_candidate_window_present_and_ordered():
    turns = _synthetic_20_turn_transcript()
    tr = _top_level_transcript("sess-synthetic", turns)
    stats = simulate_compaction_windows([tr], SONNET_RATES, {})
    rows = stats.by_window("top-level")
    assert [r.window for r in rows] == list(CANDIDATE_WINDOWS)


# -- fidelity self-check ------------------------------------------------


def _fidelity_transcript() -> TranscriptResult:
    """A top-level transcript with one real, auto-triggered
    COMPACT_BOUNDARY event correlated to turn 4, and ctx that never
    again approaches the configured window afterwards -- so simulating
    at that same configured window should reproduce the exact observed
    cost (only the one real compaction ever fires, under both
    ``window=None`` and ``window=200_000``)."""
    turns = [
        _turn(turn_index=1, ts=_ts(0), ctx=50_000, cache_creation_tokens=50_000, cache_read_tokens=0, cc_5m=50_000),
        _turn(turn_index=2, ts=_ts(1), ctx=100_000, cache_creation_tokens=50_000, cache_read_tokens=50_000, cc_5m=50_000),
        _turn(turn_index=3, ts=_ts(2), ctx=190_000, cache_creation_tokens=90_000, cache_read_tokens=100_000, cc_5m=90_000),
        # Real compaction correlates here (event ts 12:02:30, this turn's
        # ts 12:03:00 -- a 30s join, well inside the default 900s gate).
        _turn(turn_index=4, ts="2026-09-18T12:03:00.000Z", ctx=30_000, cache_creation_tokens=30_000, cache_read_tokens=0, cc_5m=30_000),
        _turn(turn_index=5, ts=_ts(4), ctx=60_000, cache_creation_tokens=30_000, cache_read_tokens=30_000, cc_5m=30_000),
        _turn(turn_index=6, ts=_ts(5), ctx=90_000, cache_creation_tokens=30_000, cache_read_tokens=60_000, cc_5m=30_000),
    ]
    events = [
        Event(
            kind=EventKind.COMPACT_BOUNDARY,
            ts="2026-09-18T12:02:30.000Z",
            pre_tokens=195_000,
            post_tokens=30_000,
            trigger="auto",
        )
    ]
    return _top_level_transcript("sess-fidelity", turns, events)


def test_fidelity_under_one_percent_when_simulating_the_observed_window():
    tr = _fidelity_transcript()
    stats = simulate_compaction_windows([tr], SONNET_RATES, {"sess-fidelity": 200_000})

    assert len(stats.fidelity_rows) == 1
    row = stats.fidelity_rows[0]
    assert row.session_id == "sess-fidelity"
    assert row.window == 200_000
    assert row.fidelity_pct is not None
    assert row.fidelity_pct < 1.0

    # The real compaction is kept under every candidate window, including
    # the one matching the session's own configured setting.
    by_window = {r.window: r for r in stats.by_window("top-level")}
    assert by_window[200_000].compactions == 1
    assert by_window[None].compactions == 1
    assert by_window[200_000].cost == pytest.approx(by_window[None].cost)


def test_no_fidelity_row_when_snapshot_window_unknown():
    tr = _fidelity_transcript()
    stats = simulate_compaction_windows([tr], SONNET_RATES, {})
    assert stats.fidelity_rows == []


# -- build_section --------------------------------------------------------


def test_build_section_tables_and_notes():
    turns = _synthetic_20_turn_transcript()
    tr = _top_level_transcript("sess-synthetic", turns)
    stats = simulate_compaction_windows([tr], SONNET_RATES, {"sess-synthetic": 100_000})
    section = build_section(stats)

    assert section.key == "compaction_sim"
    names = [t.name for t in section.tables]
    assert names == [
        "compaction_sim_by_window",
        "compaction_sim_by_agent_type",
        "compaction_sim_by_task",
        "compaction_sim_fidelity",
    ]

    by_window = next(t for t in section.tables if t.name == "compaction_sim_by_window")
    assert [row[0] for row in by_window.rows] == [
        "100,000", "150,000", "200,000", "250,000", "300,000", "400,000", "500,000",
        "600,000", "700,000", "800,000", "900,000", "967,000", "none",
    ]

    by_type = next(t for t in section.tables if t.name == "compaction_sim_by_agent_type")
    assert [row[0] for row in by_type.rows] == ["top-level"]

    for note in ASSUMPTIONS:
        assert note in section.notes

    assert_privacy(section)


def test_only_the_main_session_row_says_to_set_the_window(monkeypatch):
    """The window is one setting for the whole session: a subagent type
    whose own replay is cheapest at a smaller window says so without
    telling you to set it, so it never contradicts the main session's
    row or the compaction-window rule."""
    from claudeglass.compaction_sim import CompactionSimTypeStats

    stats = simulate_compaction_windows([], SONNET_RATES, {})
    monkeypatch.setattr(stats, "by_key", lambda: {
        "top-level": CompactionSimTypeStats("top-level", 30, 300.0, 250_000, 250.0),
        "general-purpose": CompactionSimTypeStats("general-purpose", 25, 400.0, 200_000, 327.1),
    })
    by_type = next(t for t in build_section(stats).tables if t.name == "compaction_sim_by_agent_type")
    col = [c.key for c in by_type.columns].index("recommendation")
    advice = {row[0]: row[col] for row in by_type.rows}
    assert advice["top-level"] == "Set the auto-compact window to 250,000 tokens (saves $50.00)"
    assert advice["general-purpose"] == (
        "Cheapest at 200,000 tokens (saves $72.90), but the window is one setting for the whole session: "
        "choose it from the main session row"
    )


def test_a_scheduled_main_session_is_not_replayed():
    """Scheduled checks never summarise, so replaying them would lower the
    simulated compactions per session the compaction-window rule gates on."""
    real = _top_level_transcript("sess-real", _synthetic_20_turn_transcript())
    check = _top_level_transcript(
        "sess-check", [_turn()], events=[Event(kind=EventKind.SCHEDULED_TASK, subkind=None, ts=_ts(0))]
    )
    alone = simulate_compaction_windows([real], SONNET_RATES, {})
    stats = simulate_compaction_windows([real] + [check] * 3, SONNET_RATES, {})
    assert stats.scheduled_sessions == 3
    assert stats.by_key()["top-level"].sessions == 1
    assert [r.compactions_per_session for r in stats.by_window("top-level")] == [
        r.compactions_per_session for r in alone.by_window("top-level")
    ]
    notes = build_section(stats).notes
    assert any(note.startswith("3 main sessions a scheduled task started") for note in notes)
    assert any("raising the window can't be tested" in note for note in notes)


def test_build_section_notes_flag_every_default():
    turns = _synthetic_20_turn_transcript()
    tr = _top_level_transcript("sess-synthetic", turns)
    stats = simulate_compaction_windows([tr], SONNET_RATES, {})
    section = build_section(stats)
    joined = " ".join(section.notes)
    assert "Summary size used: 20,000 tokens (default, as no real conversation summary was found)" in joined
    assert "Trigger reserve used: 0 tokens below the window (default" in joined
    assert "Starting context still cached after a summary: 0% (default" in joined
    assert "no real cache rebuild after a summary was found" in joined


def test_build_section_empty_corpus_notes_instead_of_crashing():
    stats = simulate_compaction_windows([], SONNET_RATES, {})
    section = build_section(stats)
    by_window = next(t for t in section.tables if t.name == "compaction_sim_by_window")
    assert by_window.rows == []
    assert by_window.notes


# -- EST-P8: per-task compaction aggregate -----------------------------------


def _tag_task(turns: list[model.Turn], task: str) -> list[model.Turn]:
    """Give every turn the same self-reported ``task=`` tag: guarantees
    ``_reported_task``'s majority-of-tagged-turns gate regardless of how
    many turns a transcript has."""
    return [replace(t, cap=model.CaptureTag(task=task, has_tl=True)) for t in turns]


def test_by_task_needs_min_task_sessions_before_it_reports_a_task():
    """A task reported by fewer than MIN_TASK_SESSIONS main sessions is
    tallied but never surfaced -- same "small group" gate as
    habits.MIN_GROUP (kept local to this module; see MIN_TASK_SESSIONS's
    docstring)."""
    from claudeglass.compaction_sim import MIN_TASK_SESSIONS

    results = [
        _top_level_transcript(f"sess-{i}", _tag_task(_synthetic_20_turn_transcript(), "review"))
        for i in range(MIN_TASK_SESSIONS - 1)
    ]
    stats = simulate_compaction_windows(results, SONNET_RATES, {})
    assert stats.by_task() == {}


def test_by_task_reports_once_min_task_sessions_is_reached():
    from claudeglass.compaction_sim import MIN_TASK_SESSIONS

    results = [
        _top_level_transcript(f"sess-{i}", _tag_task(_synthetic_20_turn_transcript(), "review"))
        for i in range(MIN_TASK_SESSIONS)
    ]
    stats = simulate_compaction_windows(results, SONNET_RATES, {})
    by_task = stats.by_task()
    assert set(by_task) == {"review"}
    row = by_task["review"]
    assert row.sessions == MIN_TASK_SESSIONS
    # Every session here is a main session, all tagged "review", so the
    # per-task roll-up must land on the same cheapest window and costs as
    # the equivalent by_key() roll-up for "top-level".
    by_key = stats.by_key()
    assert row.best_window == by_key["top-level"].best_window
    assert row.observed_cost == pytest.approx(by_key["top-level"].observed_cost)
    assert row.best_cost == pytest.approx(by_key["top-level"].best_cost)


def test_by_task_ignores_subagent_transcripts():
    """A subagent run's ``cap.task`` is never tallied: task aggregation is
    restricted to main sessions in ``add_transcript`` (a subagent has no
    self-reported "kind of task" of its own)."""
    from claudeglass.compaction_sim import MIN_TASK_SESSIONS

    results = [
        TranscriptResult(
            meta=TranscriptMeta(kind="subagent", agent_type="Explore"),
            turns=_tag_task(_synthetic_20_turn_transcript(), "review"),
        )
        for _ in range(MIN_TASK_SESSIONS)
    ]
    stats = simulate_compaction_windows(results, SONNET_RATES, {})
    assert stats.by_task() == {}


def test_by_task_ignores_a_session_with_fewer_than_two_tagged_turns():
    """A single tagged turn never counts toward any task -- mirrors
    ``classify.reported_task``'s own ``len(tasks) < 2`` gate exactly."""
    from claudeglass.compaction_sim import MIN_TASK_SESSIONS

    results = []
    for i in range(MIN_TASK_SESSIONS):
        turns = _synthetic_20_turn_transcript()
        turns[0] = replace(turns[0], cap=model.CaptureTag(task="review", has_tl=True))
        results.append(_top_level_transcript(f"sess-{i}", turns))
    stats = simulate_compaction_windows(results, SONNET_RATES, {})
    assert stats.by_task() == {}


def test_by_task_ignores_a_session_with_no_majority_task():
    """Three tagged turns split three ways never reaches "at least half,
    twice or more" for any one task -- mirrors ``classify.reported_task``'s
    own majority gate exactly."""
    from claudeglass.compaction_sim import MIN_TASK_SESSIONS

    results = []
    for i in range(MIN_TASK_SESSIONS):
        turns = _synthetic_20_turn_transcript()
        turns[0] = replace(turns[0], cap=model.CaptureTag(task="review", has_tl=True))
        turns[1] = replace(turns[1], cap=model.CaptureTag(task="test-triage", has_tl=True))
        turns[2] = replace(turns[2], cap=model.CaptureTag(task="planning", has_tl=True))
        results.append(_top_level_transcript(f"sess-{i}", turns))
    stats = simulate_compaction_windows(results, SONNET_RATES, {})
    assert stats.by_task() == {}


def test_build_section_by_task_table_renders_rows_and_recommendation():
    from claudeglass.compaction_sim import MIN_TASK_SESSIONS

    results = [
        _top_level_transcript(f"sess-{i}", _tag_task(_synthetic_20_turn_transcript(), "review"))
        for i in range(MIN_TASK_SESSIONS)
    ]
    stats = simulate_compaction_windows(results, SONNET_RATES, {})
    section = build_section(stats)
    by_task = next(t for t in section.tables if t.name == "compaction_sim_by_task")
    assert [row[0] for row in by_task.rows] == ["review"]
    row = dict(zip([c.key for c in by_task.columns], by_task.rows[0]))
    assert row["sessions"] == MIN_TASK_SESSIONS
    assert row["recommendation"]
    assert_privacy(section)


def test_build_section_by_task_table_notes_when_no_task_clears_the_gate():
    turns = _synthetic_20_turn_transcript()
    tr = _top_level_transcript("sess-synthetic", turns)
    stats = simulate_compaction_windows([tr], SONNET_RATES, {})
    section = build_section(stats)
    by_task = next(t for t in section.tables if t.name == "compaction_sim_by_task")
    assert by_task.rows == []
    assert by_task.notes


# -- thresholds -------------------------------------------------------------


def test_thresholds_from_config_flat_dict():
    th = CompactionSimThresholds.from_config({"switch_usd": 2.5, "fidelity_warn_pct": 20.0})
    assert th.switch_usd == 2.5
    assert th.fidelity_warn_pct == 20.0
    assert th.switch_pct == CompactionSimThresholds().switch_pct


def test_thresholds_from_config_nested_dict():
    th = CompactionSimThresholds.from_config({"thresholds": {"default_summary_tokens": 15_000}})
    assert th.default_summary_tokens == 15_000


def test_thresholds_from_config_none():
    assert CompactionSimThresholds.from_config(None) == CompactionSimThresholds()


def test_thresholds_describe_nonempty():
    assert CompactionSimThresholds().describe()


# -- rule: compaction-window -----------------------------------------------


def _base_report(sections: list[model.Section], units=None) -> ReportModel:
    report = ReportModel(meta=ReportMeta(), sections=sections, recommendations=[])
    report.units = units
    return report


#: The plateau transcript saves 0.99 USD at best, so the rule tests
#: lower the default 1 USD bar; nothing else changes.
_SMALL_FIXTURE_TH = CompactionSimThresholds(switch_usd=0.1)


def _plateau_report(th: CompactionSimThresholds | None = None, units=None) -> ReportModel:
    tr = _top_level_transcript("sess-plateau", _plateau_transcript())
    stats = simulate_compaction_windows([tr], SONNET_RATES, {}, th)
    return _base_report([build_section(stats, th, units=units)], units=units)


def test_rule_fires_when_saving_clears_both_thresholds():
    report = _plateau_report()

    recs = RULES[0](report, _SMALL_FIXTURE_TH, None)
    assert len(recs) == 1
    rec = recs[0]
    assert rec.id == "compaction-window"
    assert rec.category == "settings"
    assert rec.lever == "autoCompactWindow"
    assert rec.scope == "user"
    # One summary a session at every window below the plateau: the
    # smallest is the floor.
    assert "at least 100,000" in rec.action
    assert_privacy(rec)


def test_rule_names_the_env_variable_while_it_overrides_the_setting():
    from claudeglass.snapshots import Snapshot

    snap = Snapshot(
        path=None,
        ts="2026-09-20T00:00:00Z",
        data={
            "effective": {"autoCompactWindow": 500_000},
            "env_names": ["CLAUDE_CODE_AUTO_COMPACT_WINDOW"],
            "effective_env_provenance": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "project_local"},
        },
    )
    [rec] = RULES[0](_plateau_report(), _SMALL_FIXTURE_TH, snap)
    assert rec.title == "Set CLAUDE_CODE_AUTO_COMPACT_WINDOW to at least 100,000"
    assert rec.action.startswith(
        "Set CLAUDE_CODE_AUTO_COMPACT_WINDOW to at least 100,000 in the env block of "
        "<project>/.claude/settings.local.json. It overrides the autoCompactWindow setting. "
    )
    assert rec.scope == "project" and rec.lever == "autoCompactWindow"


def test_rule_action_has_no_bare_dollar_under_a_subscription():
    """UX-2 / finding F1-F2: a subscription's Recommendation.action must
    route through Units, never a raw f"${...:.2f}"."""
    units = Units(billing_mode="subscription", currency="USD", elasticity=elasticity_with_slope())
    report = _plateau_report(units=units)

    [rec] = RULES[0](report, _SMALL_FIXTURE_TH, None)
    assert "$" not in rec.action
    assert "about about" not in rec.action.lower()


def test_rule_does_not_fire_when_no_transcripts():
    stats = simulate_compaction_windows([], SONNET_RATES, {})
    section = build_section(stats)
    report = _base_report([section])
    recs = RULES[0](report, CompactionSimThresholds(), None)
    assert recs == []


def test_rule_does_not_fire_below_switch_usd_threshold():
    """A single, tiny transcript where even the best window's saving
    never clears a high switch_usd bar must not fire."""
    turns = _synthetic_20_turn_transcript()[:3]  # a few small turns only
    tr = _top_level_transcript("sess-small", turns)
    stats = simulate_compaction_windows([tr], SONNET_RATES, {})
    section = build_section(stats)
    report = _base_report([section])

    th = CompactionSimThresholds(switch_usd=1_000_000.0)
    recs = RULES[0](report, th, None)
    assert recs == []


def test_rule_evidence_resolves_against_the_report():
    report = _plateau_report()

    recs = RULES[0](report, _SMALL_FIXTURE_TH, None)
    assert recs, "expected the rule to fire on this fixture"
    for rec in recs:
        for label, value, source_table, row_key in rec.evidence:
            section_key, table_name = source_table.split(".", 1)
            found_section = next((s for s in report.sections if s.key == section_key), None)
            assert found_section is not None, f"no section {section_key!r} for evidence {label!r}"
            table = next((t for t in found_section.tables if t.name == table_name), None)
            assert table is not None, f"no table {table_name!r} for evidence {label!r}"
            row = next((r for r in table.rows if r and r[0] == row_key), None)
            assert row is not None, f"no row {row_key!r} in {source_table} for evidence {label!r}"


def test_rule_recommends_a_range_floor_not_a_single_best_window():
    """"compaction-window" now names a floor ("at least W"), not one
    "best" point -- and the action text says so explicitly."""
    recs = RULES[0](_plateau_report(), _SMALL_FIXTURE_TH, None)
    assert len(recs) == 1
    rec = recs[0]
    assert rec.title == "Set autoCompactWindow to at least 100,000"
    assert "at least 100,000" in rec.action
    assert "modelled, not observed" in rec.action


def test_rule_gates_out_a_candidate_with_more_than_two_compactions_per_session():
    """A window whose modelled compactions/session exceeds 2 must never
    be the recommended floor, even if its raw saving alone would have
    cleared both switch thresholds -- built directly against the
    ``compaction_sim_by_window`` table rather than a real sweep, since
    driving the synthetic fixture's own ctx growth past 2 compactions
    at 100,000 would also change its saving arithmetic."""
    by_window = model.Table(
        name="compaction_sim_by_window",
        title="t",
        columns=[
            model.Column(key="window", label="w", kind="str"),
            model.Column(key="compactions_per_session", label="c", kind="float"),
            model.Column(key="mean_ctx", label="m", kind="tokens"),
            model.Column(key="cost", label="cost", kind="money"),
            model.Column(key="delta_usd", label="d", kind="money"),
            model.Column(key="delta_pct", label="dp", kind="pct"),
        ],
        rows=[
            ["100,000", 3.0, 50_000, 0.20, -5.0, -71.0],
            ["150,000", 1.0, 80_000, 3.50, -3.5, -50.0],
            ["none", 0.0, 200_000, 7.0, 0.0, 0.0],
        ],
    )
    section = model.Section(key="compaction_sim", title="t", tables=[by_window])
    report = _base_report([section])

    recs = RULES[0](report, CompactionSimThresholds(), None)
    assert len(recs) == 1
    assert recs[0].title == "Set autoCompactWindow to at least 150,000"


def test_rule_rediscovery_correction_suppresses_a_saving_that_only_clears_the_bar_before_it():
    """With no real compaction in the corpus, the rediscovery allowance
    falls back to ``default_rediscovery_allowance_usd``; this report
    carries no ``topology_redundant_reads`` table, so the rule takes one
    allowance off per simulated summary. The sweep itself never charges
    it.

    By hand, on ``_plateau_transcript`` (see
    ``test_a_summary_keeps_the_starting_context``): every window from
    100,000 to 250,000 summarises once and saves 0.99. With
    ``switch_usd=0.9`` that fires at 100,000; with a 0.15 allowance the
    corrected saving is 0.99 - 0.15 = 0.84, below 0.9, so nothing fires.
    """
    bar = CompactionSimThresholds(switch_usd=0.9)
    assert [r.title for r in RULES[0](_plateau_report(bar), bar, None)] == [
        "Set autoCompactWindow to at least 100,000"
    ]

    th = CompactionSimThresholds(switch_usd=0.9, default_rediscovery_allowance_usd=0.15)
    report = _plateau_report(th)
    by_window = {r[0]: r for r in report.sections[0].tables[0].rows}
    assert by_window["100,000"][3] == pytest.approx(1.966)  # the sweep's cost is unchanged
    assert RULES[0](report, th, None) == []


def test_rediscovery_allowance_used_note_round_trips_through_its_list_price_format():
    """UX-2 regression: ``build_section``'s "Allowance for re-reading files:
    ..." note never prints a bare "$" (it reads "$X.XXXX at list price",
    see the module's own UX-2 comment there) --
    ``_rediscovery_allowance_usd_used`` must still read the number back
    out of that note rather than silently falling through to the
    *reading* call's own ``default_rediscovery_allowance_usd`` (which
    would happen if it choked on the "$")."""
    build_th = CompactionSimThresholds(switch_usd=0.9, default_rediscovery_allowance_usd=0.1234)
    report = _plateau_report(build_th)  # note baked in at 0.1234 (this fixture's own default branch)
    notes = [n for s in report.sections for n in s.notes if n.startswith("Allowance for re-reading files:")]
    assert notes and notes[0].startswith("Allowance for re-reading files: $0.1234 at list price")

    # A different thresholds object, with a distinct default, at read time:
    # correct parsing returns the note's 0.1234, not this object's 0.9999.
    read_th = CompactionSimThresholds(default_rediscovery_allowance_usd=0.9999)
    assert _rediscovery_allowance_usd_used(report, read_th) == pytest.approx(0.1234)

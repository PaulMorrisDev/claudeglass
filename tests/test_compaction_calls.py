"""Compaction calls: Claude Code bills the request that writes a
compaction's summary but logs only the ``compact_boundary`` line, never
the request as a reply. ``parse.py`` adds one estimated turn for it --
see that module's docstring. The shapes and numbers below are from a real
Claude Code 2.1.283 session, where Claude Code's own ``cost-state``
billed the compaction on Opus 5.5.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claudeglass import compaction, recache
from claudeglass.model import TranscriptMeta
from claudeglass.parse import parse_transcript
from claudeglass.pricing import load_pricing, price_turn

from helpers import system_line, turn_line, user_str_line, write_jsonl

MODEL = "claude-opus-5-5"


def _reply(message_id: str, timestamp: str, **usage) -> dict:
    return turn_line(message_id=message_id, model=MODEL, timestamp=timestamp, input_tokens=2, **usage)


def _boundary(timestamp: str, **metadata) -> dict:
    meta = {"trigger": "auto", "preTokens": 783870, "postTokens": 8577, "durationMs": 68425}
    meta.update(metadata)
    return system_line("compact_boundary", timestamp=timestamp, compactMetadata={k: v for k, v in meta.items() if v is not None})


def _parse(tmp_path: Path, lines: list[dict]):
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    return parse_transcript(path, TranscriptMeta(path=str(path), kind="top-level", session_id="session"))


def _session(before_ts: str = "2026-09-26T13:27:23.053Z", boundary_ts: str = "2026-09-26T13:28:40.321Z", **boundary) -> list[dict]:
    """A reply that read 780,762 tokens from the cache and wrote 1,511
    (one-hour lifetime), an automatic compaction, then the next reply."""
    return [
        user_str_line("start", timestamp="2026-09-26T13:20:00.000Z"),
        _reply("msg_before", before_ts, cache_read_input_tokens=780762, cache_creation_input_tokens=1511,
               ephemeral_1h_input_tokens=1511, output_tokens=1042),
        user_str_line("next", timestamp="2026-09-26T13:27:31.815Z"),
        _boundary(boundary_ts, **boundary),
        _reply("msg_after", "2026-09-26T13:28:42.010Z", cache_read_input_tokens=26692, cache_creation_input_tokens=37767,
               ephemeral_1h_input_tokens=37767, output_tokens=194),
    ]


def test_the_summary_request_is_added_after_the_reply_before_it(tmp_path: Path):
    result = _parse(tmp_path, _session())

    assert [turn.message_id for turn in result.turns] == ["msg_before", "compaction-2026-09-26T13:27:31.896Z", "msg_after"]
    assert [turn.turn_index for turn in result.turns] == [1, 2, 3]
    estimate = result.turns[1]
    assert estimate.estimated == "compaction"
    assert estimate.is_synthetic
    assert estimate.model == MODEL
    assert result.diagnostics.compaction_calls == 1
    assert result.diagnostics.compaction_calls_unsized == 0
    assert result.diagnostics.distinct_turns == 2


def test_a_warm_cache_is_read_and_the_summary_is_the_output(tmp_path: Path):
    estimate = _parse(tmp_path, _session()).turns[1]

    # The reply before it cached 780,762 + 1,511 tokens; Claude Code's own
    # cost-state showed the compaction read exactly 782,273 from the cache.
    assert estimate.cache_read_tokens == 782273
    assert estimate.cache_creation_tokens == 0
    assert estimate.input_tokens == 783870 - 782273
    assert estimate.output_tokens == 8577
    assert estimate.ctx == 783870


def test_the_estimate_is_priced_at_the_model_rates(tmp_path: Path):
    pricing = load_pricing()
    estimate = _parse(tmp_path, _session()).turns[1]

    cost = price_turn(estimate, pricing.resolve_model(estimate.model)).total

    # Opus 5.5: $4 input, $20 output, $0.20 cache read per million tokens.
    assert cost == pytest.approx(782273 * 0.2e-6 + 1597 * 4e-6 + 8577 * 20e-6)
    # Claude Code billed this compaction about $0.328.
    assert cost == pytest.approx(0.328, rel=0.02)


def test_the_next_reply_keeps_its_own_gap_and_compaction_record(tmp_path: Path):
    result = _parse(tmp_path, _session())
    after = result.turns[2]

    assert after.gap_s == pytest.approx(78.957)
    assert result.turns[1].gap_s is None
    records = compaction.compaction_records_for_transcript(result, load_pricing().resolve_model(MODEL))
    assert len(records) == 1
    assert records[0].next_turn_cache_creation == 37767


def test_an_expired_cache_is_written_again_at_the_same_lifetime(tmp_path: Path):
    lines = _session(before_ts="2026-09-26T12:20:00.000Z", boundary_ts="2026-09-26T13:28:40.321Z")

    estimate = _parse(tmp_path, lines).turns[1]

    assert estimate.cache_read_tokens == 0
    assert estimate.cache_creation_tokens == 782273
    assert estimate.cc_1h == 782273
    assert estimate.cc_5m == 0


def test_a_five_minute_cache_expires_after_five_minutes(tmp_path: Path):
    lines = [
        _reply("msg_before", "2026-09-26T10:00:00.000Z", cache_read_input_tokens=90000,
               cache_creation_input_tokens=10000, ephemeral_5m_input_tokens=10000),
        _boundary("2026-09-26T10:07:00.000Z", preTokens=101000, postTokens=6000, durationMs=30000),
    ]

    estimate = _parse(tmp_path, lines).turns[1]

    assert (estimate.cache_read_tokens, estimate.cache_creation_tokens) == (0, 100000)
    assert (estimate.cc_5m, estimate.cc_1h) == (100000, 0)
    assert estimate.input_tokens == 1000


def test_a_compaction_with_no_reply_before_it_is_left_out_and_counted(tmp_path: Path):
    lines = [
        _boundary("2026-09-26T10:00:00.000Z"),
        _reply("msg_after", "2026-09-26T10:01:00.000Z", output_tokens=10),
    ]

    result = _parse(tmp_path, lines)

    assert [turn.message_id for turn in result.turns] == ["msg_after"]
    assert result.diagnostics.compaction_calls == 0
    assert result.diagnostics.compaction_calls_unsized == 1


def test_a_compaction_with_no_summary_size_is_left_out_and_counted(tmp_path: Path):
    result = _parse(tmp_path, _session(postTokens=None))

    assert [turn.estimated for turn in result.turns] == [None, None]
    assert result.diagnostics.compaction_calls_unsized == 1


def test_the_estimate_is_never_read_as_a_re_cache(tmp_path: Path):
    lines = _session(before_ts="2026-09-26T12:20:00.000Z")
    result = _parse(tmp_path, lines)

    recache.apply(result, recache.RecacheThresholds())

    assert not result.turns[1].is_recache


def _project(tmp_path: Path) -> Path:
    folder = tmp_path / "-home-user-shop"
    folder.mkdir()
    write_jsonl(folder / "7e5fd47d-1816-569f-9241-e02958cf9196.jsonl", _session())
    return folder


def test_spend_totals_count_the_estimate_and_reply_counts_do_not(tmp_path: Path):
    from claudeglass.config import Config
    from claudeglass.corpus import load_corpus
    from claudeglass.report import build_report

    pricing = load_pricing()
    corpus = load_corpus([_project(tmp_path)])
    turns = corpus.sessions[0].top.turns
    every_turn = sum(price_turn(t, pricing.resolve_model(t.model)).total for t in turns)

    model = build_report(corpus, pricing, Config(), projects=(), window="all time")
    overview = next(s for s in model.sections if s.key == "overview")
    totals = dict(next(t for t in overview.tables if t.name == "totals").rows)

    assert totals["priced_turns"] == 2
    assert totals["total_cost_usd"] == pytest.approx(every_turn)
    assert totals["output_tokens"] == 1042 + 8577 + 194
    assert model.diagnostics.compaction_calls == 1


def test_the_dashboard_store_counts_replies_without_the_estimate(tmp_path: Path):
    from claudeglass.service.watcher import _build_turns_agg

    pricing = load_pricing()
    result = _parse(tmp_path, _session())

    rows = _build_turns_agg(result, pricing)

    assert [(row["model"], row["turns"]) for row in rows] == [(MODEL, 2)]
    assert rows[0]["output_tokens"] == 1042 + 8577 + 194
    assert rows[0]["cost"] == pytest.approx(
        sum(price_turn(t, pricing.resolve_model(t.model)).total for t in result.turns)
    )


def test_the_compaction_section_shows_what_the_summaries_cost(tmp_path: Path):
    pricing = load_pricing()
    result = _parse(tmp_path, _session())

    stats = compaction.CompactionStats.build([(result, pricing.resolve_model(MODEL))])
    rows = dict(compaction.build_section(stats).tables[0].rows)

    assert stats.summary_requests == 1
    assert rows["Summary requests (estimated, USD)"] == pytest.approx(
        price_turn(result.turns[1], pricing.resolve_model(MODEL)).total
    )

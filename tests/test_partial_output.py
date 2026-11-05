"""Replies Claude Code logged only while they were still being written.

Claude Code writes one transcript line per content block, each with a
snapshot of the message's usage. A subagent's reply is often never
finished in the log: every line has ``stop_reason`` null and a partial
``output_tokens`` (3 to 17 tokens), with no ``output_tokens_details``,
though the input and cache counts are right and Claude Code bills the
whole reply. ``parse.py`` marks such a reply ``Turn.output_partial`` and,
when a priced reply follows it, estimates the output from how much the
next request's context grew (``Turn.output_tokens_est``); ``pricing``
prices that, and ``reconcile`` shows its share on its own.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from claudeglass.cache import encode_result, result_from_jsonable
from claudeglass.model import Diagnostics, TranscriptMeta
from claudeglass.parse import parse_transcript
from claudeglass.pricing import load_pricing, price_turn
from claudeglass.report import _merge_diagnostics

from helpers import tool_result_block, tool_use_block, turn_line, user_block_line, user_str_line, write_jsonl

MODEL = "claude-sonnet-5"


def _reply(message_id: str, timestamp: str, stop_reason: str | None = "end_turn", details: bool = False, **fields) -> dict:
    """One reply line. ``stop_reason`` null and no ``details`` make a
    snapshot of a reply still being written."""
    line = turn_line(message_id=message_id, model=MODEL, timestamp=timestamp, **fields)
    if stop_reason is not None:
        line["message"]["stop_reason"] = stop_reason
    if details:
        line["message"]["usage"]["output_tokens_details"] = {"thinking_tokens": 0}
    return line


def _results(tool_use_id: str, chars: int, timestamp: str) -> dict:
    return user_block_line([tool_result_block(tool_use_id, "x" * chars)], timestamp=timestamp)


def _parse(tmp_path: Path, lines: list[dict]):
    path = tmp_path / "agent-x.jsonl"
    write_jsonl(path, lines)
    return parse_transcript(path, TranscriptMeta(path=str(path), kind="subagent", session_id="session"))


def _subagent(tmp_path: Path):
    """A finished first reply, a reply logged twice at output 7 that
    called a tool (no ``stop_reason``), its 4,000-character tool result,
    a finished reply whose context grew 2,000 tokens, and a last reply
    logged mid-stream with nothing after it."""
    read = [tool_use_block("Read", "toolu_2", {"file_path": "a.py"})]
    return _parse(
        tmp_path,
        [
            user_str_line("go", timestamp="2026-10-20T10:00:00.000Z"),
            _reply("msg_1", "2026-10-20T10:00:05.000Z", input_tokens=3000, output_tokens=40),
            _reply("msg_2", "2026-10-20T10:00:10.000Z", stop_reason=None, input_tokens=5000, output_tokens=7,
                   content=[{"type": "text", "text": "reading"}]),
            _reply("msg_2", "2026-10-20T10:00:10.500Z", stop_reason=None, input_tokens=5000, output_tokens=7,
                   content=read),
            _results("toolu_2", 4000, "2026-10-20T10:00:12.000Z"),
            _reply("msg_3", "2026-10-20T10:00:20.000Z", input_tokens=7000, output_tokens=300),
            _reply("msg_4", "2026-10-20T10:00:30.000Z", stop_reason=None, input_tokens=7400, output_tokens=5),
        ],
    )


def _by_id(result):
    return {turn.message_id: turn for turn in result.turns}


def test_a_reply_logged_only_mid_stream_gets_an_output_estimate_from_the_next_requests_growth(tmp_path: Path):
    turns = _by_id(_subagent(tmp_path))

    partial = turns["msg_2"]
    assert partial.output_partial is True
    assert partial.output_tokens == 7
    # The next request holds 2,000 more tokens, 1,000 of them the 4,000
    # characters of tool result: the other 1,000 were this reply's output.
    assert partial.output_tokens_est == 1000


def test_a_finished_reply_is_not_partial_and_has_no_estimate(tmp_path: Path):
    turns = _by_id(_subagent(tmp_path))

    for message_id in ("msg_1", "msg_3"):
        assert turns[message_id].output_partial is False
        assert turns[message_id].output_tokens_est is None


def test_the_last_reply_has_nothing_after_it_so_it_keeps_the_logged_count(tmp_path: Path):
    last = _by_id(_subagent(tmp_path))["msg_4"]

    assert last.output_partial is True
    assert last.output_tokens == 5
    assert last.output_tokens_est is None


def test_output_is_priced_at_the_estimate(tmp_path: Path):
    pricing = load_pricing()
    turns = _by_id(_subagent(tmp_path))

    cost = price_turn(turns["msg_2"], pricing.resolve_model(MODEL))

    # Sonnet 5: $10 per million output tokens.
    assert cost.output_cost == pytest.approx(1000 * 10e-6)
    last = price_turn(turns["msg_4"], pricing.resolve_model(MODEL))
    assert last.output_cost == pytest.approx(5 * 10e-6)


def test_the_data_quality_counts_say_how_many_replies_and_how_many_tokens_were_estimated(tmp_path: Path):
    diagnostics = _subagent(tmp_path).diagnostics

    assert diagnostics.partial_output_turns == 2
    assert diagnostics.partial_output_est_tokens == 1000 - 7


def test_a_reply_with_output_token_details_is_finished_even_without_a_stop_reason(tmp_path: Path):
    lines = [
        _reply("msg_1", "2026-10-20T10:00:05.000Z", input_tokens=3000, output_tokens=40),
        _reply("msg_2", "2026-10-20T10:00:10.000Z", stop_reason=None, details=True, input_tokens=5000, output_tokens=90),
        _reply("msg_3", "2026-10-20T10:00:20.000Z", input_tokens=7000, output_tokens=300),
    ]

    turns = _by_id(_parse(tmp_path, lines))

    assert turns["msg_2"].output_partial is False
    assert turns["msg_2"].output_tokens_est is None


def test_a_later_snapshot_with_a_stop_reason_makes_the_reply_finished(tmp_path: Path):
    lines = [
        _reply("msg_1", "2026-10-20T10:00:05.000Z", input_tokens=3000, output_tokens=40),
        _reply("msg_2", "2026-10-20T10:00:10.000Z", stop_reason=None, input_tokens=5000, output_tokens=7),
        _reply("msg_2", "2026-10-20T10:00:11.000Z", stop_reason="end_turn", details=True, input_tokens=5000,
               output_tokens=120),
        _reply("msg_3", "2026-10-20T10:00:20.000Z", input_tokens=7000, output_tokens=300),
    ]

    turns = _by_id(_parse(tmp_path, lines))

    assert turns["msg_2"].output_partial is False
    assert turns["msg_2"].output_tokens == 120


def test_nothing_is_partial_when_no_reply_records_a_stop_reason(tmp_path: Path):
    """An older Claude Code, or a hand-built log, records no stop reason on
    any reply: a partial one can't be told from a complete one."""
    lines = [
        _reply("msg_1", "2026-10-20T10:00:05.000Z", stop_reason=None, input_tokens=3000, output_tokens=40),
        _reply("msg_2", "2026-10-20T10:00:10.000Z", stop_reason=None, input_tokens=5000, output_tokens=7),
        _reply("msg_3", "2026-10-20T10:00:20.000Z", stop_reason=None, input_tokens=7000, output_tokens=300),
    ]

    result = _parse(tmp_path, lines)

    assert all(not turn.output_partial and turn.output_tokens_est is None for turn in result.turns)
    assert result.diagnostics.partial_output_turns == 0


def test_the_estimate_is_never_below_the_logged_count(tmp_path: Path):
    """A context that shrank (a compaction, a cleared tool result) would
    make the growth negative."""
    lines = [
        _reply("msg_1", "2026-10-20T10:00:05.000Z", input_tokens=9000, output_tokens=40),
        _reply("msg_2", "2026-10-20T10:00:10.000Z", stop_reason=None, input_tokens=9500, output_tokens=7),
        _reply("msg_3", "2026-10-20T10:00:20.000Z", input_tokens=2000, output_tokens=300),
    ]

    partial = _by_id(_parse(tmp_path, lines))["msg_2"]

    assert partial.output_partial is True
    assert partial.output_tokens_est == 7


def test_a_synthetic_placeholder_reply_is_never_partial(tmp_path: Path):
    placeholder = _reply("msg_s", "2026-10-20T10:00:08.000Z", stop_reason=None, input_tokens=0, output_tokens=0)
    placeholder["message"]["model"] = "<synthetic>"
    lines = [
        _reply("msg_1", "2026-10-20T10:00:05.000Z", input_tokens=3000, output_tokens=40),
        placeholder,
        _reply("msg_3", "2026-10-20T10:00:20.000Z", input_tokens=7000, output_tokens=300),
    ]

    result = _parse(tmp_path, lines)

    assert all(not turn.output_partial for turn in result.turns)
    assert result.diagnostics.partial_output_turns == 0


def test_the_new_fields_survive_the_digest_cache(tmp_path: Path):
    result = _subagent(tmp_path)

    decoded = result_from_jsonable(json.loads(json.dumps(encode_result(result))))

    before = _by_id(result)["msg_2"]
    after = _by_id(decoded)["msg_2"]
    assert (after.output_partial, after.output_tokens_est) == (True, 1000)
    assert after == before
    assert decoded.diagnostics.partial_output_turns == 2
    assert decoded.diagnostics.partial_output_est_tokens == 993


def test_the_corpus_diagnostics_add_up_every_counter_a_transcript_records():
    """A counter added to ``Diagnostics`` must also be added up by
    ``_merge_diagnostics``, or the report's Data quality page shows 0 for
    it however many transcripts counted it (the mid-stream counters were
    once left out). Only the two ``pricing_*`` totals are set later, once,
    by ``build_report``."""
    set_later = {"pricing_closest_match_turns", "pricing_fast_priced_as_standard_turns"}
    one = Diagnostics()
    for field in dataclasses.fields(Diagnostics):
        current = getattr(one, field.name)
        if isinstance(current, bool):
            setattr(one, field.name, True)
        elif isinstance(current, int):
            setattr(one, field.name, 1)
        elif isinstance(current, dict):
            setattr(one, field.name, {"x": 1})

    total = Diagnostics()
    _merge_diagnostics(total, one)

    for field in dataclasses.fields(Diagnostics):
        if field.name in set_later:
            assert getattr(total, field.name) == 0
        else:
            assert getattr(total, field.name) == getattr(one, field.name), field.name
    assert total.partial_output_turns == 1
    assert total.partial_output_est_tokens == 1

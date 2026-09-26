"""Claude Code's own cost record (``cost-state``) against ClaudeGlass's
pricing -- ``parse.py`` keeps when the total was written and its cost by
model, ``reconcile.claude_code_reported_costs`` compares the same span,
and the ``cost-record`` check reports it. Shapes are from real Claude Code
2.1.283 sessions, where a ``cost-state`` line is a running total written
now and then (see ``model.py``'s module docstring).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from claudeglass import quick_actions as qa
from claudeglass import reconcile
from claudeglass.corpus import load_corpus
from claudeglass.model import TranscriptMeta, Turn
from claudeglass.parse import parse_transcript
from claudeglass.pricing import load_pricing, price_turn
from claudeglass.units import Units

from helpers import turn_line, user_str_line, write_jsonl

SESSION = "7e5fd47d-1816-569f-9241-e02958cf9196"
SONNET = "claude-sonnet-5"
HAIKU = "claude-haiku-4-5-20251001"


def _reply(message_id: str, timestamp: str, stop_reason: str | None = "end_turn", **usage) -> dict:
    line = turn_line(message_id=message_id, model=SONNET, timestamp=timestamp, **usage)
    line["message"]["stop_reason"] = stop_reason
    return line


def _cost_state(total: float, by_model: dict[str, float]) -> dict:
    return {
        "type": "cost-state",
        "sessionId": SESSION,
        "totalCostUSD": total,
        "startTime": 1790415397937,
        "modelUsage": {model: {"inputTokens": 1, "outputTokens": 1, "costUSD": cost} for model, cost in by_model.items()},
        "hasUnknownModelCost": False,
    }


def _sonnet_cost(**usage) -> float:
    """What a Sonnet 5 reply with ``usage`` costs."""
    return price_turn(Turn(model=SONNET, turn_index=1, **usage), load_pricing().resolve_model(SONNET)).total


def _session_lines(*, stopped: bool = True, after: bool = True) -> list[dict]:
    """Two replies, one maybe stopped mid-stream, then Claude Code's total
    (which counts neither the stopped reply nor the reply after it, but
    does count a title request on Haiku), then one more reply."""
    first = _sonnet_cost(input_tokens=1000, output_tokens=500)
    haiku = 0.001
    lines = [
        user_str_line("start", timestamp="2026-09-26T10:00:00.000Z", sessionId=SESSION),
        _reply("msg_1", "2026-09-26T10:00:05.000Z", input_tokens=1000, output_tokens=500, sessionId=SESSION),
    ]
    if stopped:
        lines.append(_reply("msg_2", "2026-09-26T10:01:00.000Z", stop_reason=None, input_tokens=2000,
                            output_tokens=9, sessionId=SESSION))
    lines.append(user_str_line("[Request interrupted by user]", timestamp="2026-09-26T10:01:01.000Z",
                               sessionId=SESSION))
    lines.append(_cost_state(first + haiku, {SONNET: first, HAIKU: haiku, "bad id/../x": 5.0}))
    if after:
        lines.append(_reply("msg_3", "2026-09-26T11:00:00.000Z", input_tokens=4000, output_tokens=800,
                            sessionId=SESSION))
    return lines


def _write(tmp_path: Path, lines: list[dict]) -> Path:
    folder = tmp_path / "-home-user-shop"
    folder.mkdir(parents=True, exist_ok=True)
    write_jsonl(folder / f"{SESSION}.jsonl", lines)
    return folder


def test_the_parser_keeps_when_the_total_was_written_and_its_models(tmp_path: Path):
    path = _write(tmp_path, _session_lines()) / f"{SESSION}.jsonl"

    meta = parse_transcript(path, TranscriptMeta(path=str(path), kind="top-level", session_id=SESSION)).meta

    assert meta.cc_cost_as_of == "2026-09-26T10:01:01.000Z"
    assert set(meta.cc_cost_by_model) == {SONNET, HAIKU}


def test_the_comparison_stops_where_claude_codes_total_stops(tmp_path: Path):
    corpus = load_corpus([_write(tmp_path, _session_lines(stopped=False))])

    [cost] = reconcile.claude_code_reported_costs(corpus, load_pricing())

    first = _sonnet_cost(input_tokens=1000, output_tokens=500)
    assert cost.as_of == "2026-09-26T10:01:01.000Z"
    assert cost.local_cost_usd == pytest.approx(first)
    assert cost.local_by_model == {SONNET: pytest.approx(first)}


def test_a_stopped_reply_and_an_unlogged_request_explain_the_difference(tmp_path: Path):
    corpus = load_corpus([_write(tmp_path, _session_lines())])

    [cost] = reconcile.claude_code_reported_costs(corpus, load_pricing())

    assert cost.stopped_usd == pytest.approx(_sonnet_cost(input_tokens=2000, output_tokens=9))
    assert cost.unlogged_usd == pytest.approx(0.001)
    assert cost.local_cost_usd - cost.cc_cost_usd == pytest.approx(cost.stopped_usd - cost.unlogged_usd)
    assert cost.unexplained_usd == pytest.approx(0.0, abs=1e-12)


def test_no_reply_counts_as_stopped_when_the_log_records_no_stop_reasons(tmp_path: Path):
    lines = [
        _reply("msg_1", "2026-09-26T10:00:05.000Z", stop_reason=None, sessionId=SESSION),
        _reply("msg_2", "2026-09-26T10:00:10.000Z", stop_reason=None, sessionId=SESSION),
        _cost_state(0.01, {SONNET: 0.01}),
    ]
    corpus = load_corpus([_write(tmp_path, lines)])

    [cost] = reconcile.claude_code_reported_costs(corpus, load_pricing())

    assert cost.stopped_usd == 0.0


def _check(tmp_path: Path, costs) -> dict:
    section = reconcile.build_cost_record_section(costs)
    config_dir = tmp_path / ".claude" / "claudeglass"
    config_dir.mkdir(parents=True, exist_ok=True)
    ctx = qa.Context(
        model=NS(sections=[section], recommendations=[]),
        units=Units(billing_mode="api", currency="USD"),
        period="over the last 14 days",
        config_dir=config_dir,
        effective={},
        effective_agents={},
    )
    return qa.run("cost-record", ctx)


def test_the_check_is_ok_when_the_known_reasons_explain_the_difference(tmp_path: Path):
    costs = reconcile.claude_code_reported_costs(load_corpus([_write(tmp_path, _session_lines())]), load_pricing())

    result = _check(tmp_path, costs)

    assert result["status"] == "ok"
    assert "0.0% is left" in result["summary"]
    assert result["table"]["rows"][0][:2] == [SESSION[:8], "2026-09-26 10:01 UTC"]


def test_the_check_asks_for_a_report_when_much_is_left_unexplained(tmp_path: Path):
    costs = [reconcile.ClaudeCodeCost(session_id=SESSION, cc_cost_usd=10.0, local_cost_usd=11.0, as_of="x")]

    result = _check(tmp_path, costs)

    assert result["status"] == "act"
    assert "+10.0%" in result["summary"]


def test_the_check_has_nothing_to_say_without_a_record(tmp_path: Path):
    result = _check(tmp_path, [])

    assert result["status"] == "no_data"


def test_the_section_adds_up_the_sessions_and_their_reasons():
    costs = [
        reconcile.ClaudeCodeCost(session_id="a", cc_cost_usd=10.0, local_cost_usd=10.5, as_of="2026-09-26T10:00:00Z",
                                 stopped_usd=0.5),
        reconcile.ClaudeCodeCost(session_id="b", cc_cost_usd=5.0, local_cost_usd=4.9, as_of="2026-09-26T11:00:00Z",
                                 unlogged_usd=0.1),
    ]

    section = reconcile.build_cost_record_section(costs)
    summary, sessions = section.tables
    row = dict(zip([c.key for c in summary.columns], summary.rows[0]))

    assert row["sessions"] == 2
    assert row["difference_pct"] == pytest.approx(100 * 0.4 / 15)
    assert row["unexplained_pct"] == pytest.approx(0.0, abs=1e-9)
    assert [r[0] for r in sessions.rows] == ["b", "a"]

"""What the parser keeps of an assistant line's ``usage`` and
``diagnostics`` beyond the plain counts: the API's own cache-miss reason
(``message.diagnostics.cache_miss_reason``) and the cache split of the
iteration that answered when a model refusal fell back to another model
(``usage.iterations``). Shapes are from real Claude Code 2.1.30x logs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claudeglass.model import TranscriptMeta
from claudeglass.parse import parse_transcript

from helpers import turn_line, user_str_line, write_jsonl

REFUSED = "claude-fable-5-1"
ANSWERED = "claude-opus-5-5"


def _parse(tmp_path: Path, lines: list[dict]):
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    return parse_transcript(path, TranscriptMeta(path=str(path), kind="top-level", session_id="session"))


def _with_reason(line: dict, reason) -> dict:
    line["message"]["diagnostics"] = {"cache_miss_reason": reason}
    return line


# -- the API's own cache-miss reason -----------------------------------------


@pytest.mark.parametrize(
    "kind",
    ["messages_changed", "system_changed", "tools_changed", "model_changed", "previous_message_not_found", "unavailable"],
)
def test_a_known_cache_miss_reason_is_kept_with_the_tokens_it_missed(tmp_path: Path, kind: str):
    line = _with_reason(turn_line(message_id="msg_1"), {"type": kind, "cache_missed_input_tokens": 48786})

    [turn] = _parse(tmp_path, [user_str_line("hi"), line]).turns

    assert turn.cache_miss_reason == kind
    assert turn.cache_missed_tokens == 48786


def test_a_reason_the_parser_does_not_know_becomes_other(tmp_path: Path):
    line = _with_reason(turn_line(message_id="msg_1"), {"type": "a-reason-with /private/path in it"})

    [turn] = _parse(tmp_path, [user_str_line("hi"), line]).turns

    assert turn.cache_miss_reason == "other"
    assert turn.cache_missed_tokens is None
    assert "private" not in repr(turn)


@pytest.mark.parametrize("missed", [-5, "12", True, 1.5, None])
def test_a_missed_token_count_that_is_not_a_count_is_dropped(tmp_path: Path, missed):
    line = _with_reason(turn_line(message_id="msg_1"), {"type": "tools_changed", "cache_missed_input_tokens": missed})

    [turn] = _parse(tmp_path, [user_str_line("hi"), line]).turns

    assert turn.cache_miss_reason == "tools_changed"
    assert turn.cache_missed_tokens is None


def test_a_reply_with_no_diagnostics_has_no_reason(tmp_path: Path):
    [turn] = _parse(tmp_path, [user_str_line("hi"), turn_line(message_id="msg_1")]).turns

    assert turn.cache_miss_reason is None
    assert turn.cache_missed_tokens is None


def test_a_diagnostics_value_that_is_not_an_object_is_ignored(tmp_path: Path):
    line = turn_line(message_id="msg_1")
    line["message"]["diagnostics"] = {"cache_miss_reason": "tools_changed"}

    [turn] = _parse(tmp_path, [user_str_line("hi"), line]).turns

    assert turn.cache_miss_reason is None


def test_the_reason_on_a_later_snapshot_of_the_same_reply_is_kept(tmp_path: Path):
    first = turn_line(message_id="msg_1", timestamp="2026-10-20T10:00:00.000Z")
    later = _with_reason(
        turn_line(message_id="msg_1", timestamp="2026-10-20T10:00:01.000Z"),
        {"type": "system_changed", "cache_missed_input_tokens": 900},
    )

    [turn] = _parse(tmp_path, [user_str_line("hi"), first, later]).turns

    assert (turn.cache_miss_reason, turn.cache_missed_tokens) == ("system_changed", 900)


# -- a model-refusal fallback -------------------------------------------------


def _fallback_line() -> dict:
    """A reply that was refused on the first model and answered by the
    second. The top-level counts are the answering model's; the nested
    ``cache_creation`` split still holds the refused attempt's 80,000-token
    one-hour write. ``iterations`` lists both attempts."""
    line = turn_line(
        message_id="msg_1", model=ANSWERED, input_tokens=3, output_tokens=100, cache_read_input_tokens=0,
        cache_creation_input_tokens=5000, ephemeral_5m_input_tokens=0, ephemeral_1h_input_tokens=80000,
    )
    line["message"]["usage"]["iterations"] = [
        {
            "type": "message", "model": REFUSED, "input_tokens": 10, "output_tokens": 0,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 80000,
            "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 80000},
        },
        {
            "type": "fallback_message", "model": ANSWERED, "input_tokens": 3, "output_tokens": 100,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 5000,
            "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 5000},
        },
    ]
    return line


def test_the_cache_split_comes_from_the_iteration_that_answered(tmp_path: Path):
    result = _parse(tmp_path, [user_str_line("hi"), _fallback_line()])
    [turn] = result.turns

    assert turn.model == ANSWERED
    assert turn.cache_creation_tokens == 5000
    assert (turn.cc_5m, turn.cc_1h) == (0, 5000)
    # The split now adds up to the write, so it is not a mismatch (a
    # Diagnostics count).
    assert result.diagnostics.ttl_sum_mismatch == 0
    assert turn.ttl_split_unknown is False


def test_the_refused_attempts_write_is_not_priced_at_the_answering_models_rate(tmp_path: Path):
    from claudeglass.pricing import load_pricing, price_turn

    [turn] = _parse(tmp_path, [user_str_line("hi"), _fallback_line()]).turns

    cost = price_turn(turn, load_pricing().resolve_model(ANSWERED))

    # Opus 5.5: $8 per million for a one-hour write; 5,000 tokens, not 85,000.
    assert cost.cache_write_cost == pytest.approx(5000 * 8e-6)


def test_a_reply_with_one_iteration_keeps_the_top_level_split(tmp_path: Path):
    line = turn_line(message_id="msg_1", cache_creation_input_tokens=700, ephemeral_5m_input_tokens=200,
                     ephemeral_1h_input_tokens=500)
    line["message"]["usage"]["iterations"] = [
        {"type": "message", "input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 0,
         "cache_creation_input_tokens": 700,
         "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 700}},
    ]

    [turn] = _parse(tmp_path, [user_str_line("hi"), line]).turns

    assert (turn.cc_5m, turn.cc_1h) == (200, 500)


def test_two_iterations_none_of_which_match_leave_the_top_level_split(tmp_path: Path):
    line = turn_line(message_id="msg_1", input_tokens=100, output_tokens=50, cache_creation_input_tokens=700,
                     ephemeral_5m_input_tokens=200, ephemeral_1h_input_tokens=500)
    line["message"]["usage"]["iterations"] = [
        {"type": "message", "input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": 0,
         "cache_creation_input_tokens": 9, "cache_creation": {"ephemeral_1h_input_tokens": 9}},
        {"type": "message", "input_tokens": 2, "output_tokens": 2, "cache_read_input_tokens": 0,
         "cache_creation_input_tokens": 8, "cache_creation": {"ephemeral_1h_input_tokens": 8}},
    ]

    [turn] = _parse(tmp_path, [user_str_line("hi"), line]).turns

    assert (turn.cc_5m, turn.cc_1h) == (200, 500)

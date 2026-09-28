"""Parser-signals batch (SURV-4/5/6/7, ``PARSER_VERSION`` 19, see
model.py's and events.py's own module docstrings): ``thinking_drop``
joining the ``CACHE_SIGNAL`` family, ``task_status``/``structured_output``
getting their own ``EventKind``s, ``cost-state`` lines feeding
``TranscriptMeta.cc_cost_usd``/``cc_cost_has_unknown_model``, image/
document content-block sizing (``events.image_token_estimate``,
``events.content_block_size``), and the ``unknown_line_types``/
``unsized_blocks`` counters on ``TranscriptResult.parser_notes``.

Privacy-specific fixtures for these same additions live in
tests/test_privacy.py, matching that file's own convention -- this file
covers behaviour/shape, not privacy.
"""

from __future__ import annotations

import base64
from pathlib import Path

from claudeglass import events
from claudeglass.model import EventKind, TranscriptMeta
from claudeglass.parse import parse_transcript

from helpers import (
    attachment_line,
    ignorable_line,
    tool_result_block,
    tool_use_block,
    turn_line,
    user_block_line,
    user_str_line,
    write_jsonl,
)


# -- thinking_drop (SURV-4): joins the CACHE_SIGNAL family ----------------


def test_thinking_drop_is_a_cache_signal():
    line = attachment_line(
        "thinking_drop", newlyDropped={"reason": "prefix_mismatch", "blockCount": 2, "turnCount": 1}
    )
    event = events.classify_line(line)
    assert event.kind == EventKind.CACHE_SIGNAL
    assert event.subkind == "thinking_drop"
    assert event.detail == {"reason": "prefix_mismatch", "blockCount": 2, "turnCount": 1}


def test_thinking_drop_missing_newly_dropped_is_empty_detail():
    line = attachment_line("thinking_drop")
    event = events.classify_line(line)
    assert event.kind == EventKind.CACHE_SIGNAL
    assert event.detail == {}


# -- task_status / structured_output (SURV-4): their own EventKinds ------


def test_task_status_event_kind_and_closed_words():
    line = attachment_line("task_status", status="running", taskType="local_agent")
    event = events.classify_line(line)
    assert event.kind == EventKind.TASK_STATUS
    assert event.subkind == "task_status"
    assert event.detail == {"status": "running", "task_type": "local_agent"}


def test_task_status_partial_fields():
    line = attachment_line("task_status", status="failed")
    event = events.classify_line(line)
    assert event.detail == {"status": "failed"}


def test_structured_output_event_kind_sizes_data_only():
    line = attachment_line("structured_output", data={"a": 1, "b": [1, 2, 3]})
    event = events.classify_line(line)
    assert event.kind == EventKind.STRUCTURED_OUTPUT
    assert event.subkind == "structured_output"
    assert event.detail == {}
    assert event.size_chars == len('{"a":1,"b":[1,2,3]}')


def test_structured_output_missing_data_is_unsized():
    line = attachment_line("structured_output")
    event = events.classify_line(line)
    assert event.kind == EventKind.STRUCTURED_OUTPUT
    assert event.size_chars is None


def test_task_status_and_structured_output_rank_with_queue_operation(tmp_path: Path):
    # PRECEDENCE addition: both new kinds sit in the same harness-plumbing
    # band as QUEUE_OPERATION/HOOK_OUTPUT, ahead of ordinary attachments,
    # so a turn preceded only by one of them still resolves it as
    # preceding_primary rather than falling through to None.
    lines = [
        turn_line(message_id="msg_1", input_tokens=100, output_tokens=10),
        attachment_line("task_status", status="completed"),
        turn_line(message_id="msg_2", input_tokens=50, output_tokens=5),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    assert result.turns[-1].preceding_primary == EventKind.TASK_STATUS


# -- cost-state (SURV-5) --------------------------------------------------


def test_cost_state_populates_meta_fields(tmp_path: Path):
    lines = [
        turn_line(message_id="msg_1"),
        ignorable_line("cost-state", totalCostUSD=3.14, hasUnknownModelCost=True),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    assert result.meta.cc_cost_usd == 3.14
    assert result.meta.cc_cost_has_unknown_model is True


def test_a_blank_cost_state_is_not_claude_codes_cost(tmp_path: Path):
    """Claude Code writes ``totalCostUSD: 0`` with an empty ``modelUsage``
    on some sessions: that is no record, not a cost of nothing, and it
    doesn't replace a real total seen before it."""
    blank = ignorable_line("cost-state", totalCostUSD=0, totalAPIDuration=0, modelUsage={}, hasUnknownModelCost=False)
    path = tmp_path / "session.jsonl"
    write_jsonl(path, [turn_line(message_id="msg_1"), blank])
    assert parse_transcript(path, TranscriptMeta(path=str(path))).meta.cc_cost_usd is None

    real = ignorable_line("cost-state", totalCostUSD=2.5, modelUsage={"claude-opus-5-5": {"costUSD": 2.5}})
    write_jsonl(path, [turn_line(message_id="msg_1"), real, turn_line(message_id="msg_2"), blank])
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    assert result.meta.cc_cost_usd == 2.5
    assert result.meta.cc_cost_by_model == {"claude-opus-5-5": 2.5}


def test_cost_state_defaults_to_none_when_absent(tmp_path: Path):
    lines = [turn_line(message_id="msg_1")]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    assert result.meta.cc_cost_usd is None
    assert result.meta.cc_cost_has_unknown_model is False


def test_cost_state_is_counted_in_ignored_line_types(tmp_path: Path):
    lines = [ignorable_line("cost-state", totalCostUSD=1.0, hasUnknownModelCost=False)]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    assert result.diagnostics.ignored_line_types.get("cost-state") == 1
    # Never double-counted as an unknown type too.
    assert "cost-state" not in result.parser_notes.get("unknown_line_types", {})


# -- unknown_line_types (SURV-6) ------------------------------------------


def test_sanitize_line_type_accepts_a_well_formed_type():
    assert events.sanitize_line_type("bridge-session-v2") == "bridge-session-v2"


def test_sanitize_line_type_rejects_oversized_or_malformed():
    assert events.sanitize_line_type("x" * 100) == "other"
    assert events.sanitize_line_type("has spaces") == "other"
    assert events.sanitize_line_type("<script>") == "other"
    assert events.sanitize_line_type("1starts-with-digit") == "other"
    assert events.sanitize_line_type(None) == "other"
    assert events.sanitize_line_type(123) == "other"
    assert events.sanitize_line_type("") == "other"


def test_unknown_line_types_counted_separately_from_ignored(tmp_path: Path):
    lines = [
        ignorable_line("totally-new-line-type"),
        ignorable_line("totally-new-line-type"),
        ignorable_line("another-unknown"),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    assert result.parser_notes["unknown_line_types"] == {"totally-new-line-type": 2, "another-unknown": 1}
    # A separate, additive counter -- not a replacement for the
    # long-standing ignored-type bucket, which an EventKind.UNKNOWN type
    # (one classify_line has no rule for at all) still also feeds, same
    # as before this phase.
    assert result.diagnostics.ignored_line_types["totally-new-line-type"] == 2


def test_parser_notes_absent_when_nothing_to_report(tmp_path: Path):
    lines = [turn_line(message_id="msg_1")]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    assert result.parser_notes == {}


# -- image/document block sizing (SURV-7) ---------------------------------

# A real 1x1 PNG (IHDR: width=1, height=1) -- minimal valid header bytes.
_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _image_block(media_type: str, data: bytes | str) -> dict:
    raw = data if isinstance(data, str) else _b64(data)
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": raw}}


def _document_block(media_type: str = "application/pdf") -> dict:
    return {"type": "document", "source": {"type": "base64", "media_type": media_type, "data": "ZmFrZQ=="}}


def test_image_token_estimate_is_ceil_of_patch_grid():
    # tokens = ceil(width/28) * ceil(height/28) -- Anthropic's documented
    # Standard-tier image-token rule (see events.py's image_token_estimate
    # docstring; verified against Anthropic's own vision docs and against
    # real Pillow-generated fixtures during this phase's implementation).
    assert events.image_token_estimate(28, 28) == 1
    assert events.image_token_estimate(29, 28) == 2
    assert events.image_token_estimate(1, 1) == 1
    assert events.image_token_estimate(1092, 1092) == 1521
    assert events.image_token_estimate(1568, 100) == 224


def test_image_token_estimate_none_outside_standard_tier():
    # Never guesses at the High-resolution tier's own downscaling: a long
    # edge over 1568px, or a patch-grid token count that would exceed
    # 1568 tokens even within that edge cap, is flagged unsized (None)
    # rather than estimated.
    assert events.image_token_estimate(8000, 8000) is None
    assert events.image_token_estimate(1569, 100) is None
    # A long edge exactly at the 1568px cap can still overflow the
    # 1568-token cap once squared into a patch grid.
    assert events.image_token_estimate(1568, 1568) is None


def test_image_token_estimate_none_for_non_positive_dimensions():
    assert events.image_token_estimate(0, 100) is None
    assert events.image_token_estimate(100, 0) is None
    assert events.image_token_estimate(-1, 100) is None


def test_content_block_size_sizes_a_real_png():
    tokens = events.image_token_estimate(1, 1)
    chars, block_type = events.content_block_size(_image_block("image/png", _TINY_PNG))
    assert block_type is None or block_type == "image"
    assert chars == tokens * events._CHARS_PER_TOKEN_APPROX


def test_content_block_size_document_is_always_unsized():
    chars, block_type = events.content_block_size(_document_block())
    assert chars is None
    assert block_type == "document"


def test_content_block_size_malformed_base64_is_unsized_not_a_crash():
    chars, block_type = events.content_block_size(_image_block("image/png", "not-valid-base64!!!"))
    assert chars is None
    assert block_type == "image"


def test_content_block_size_unrecognised_block_is_ignored():
    chars, block_type = events.content_block_size({"type": "text", "text": "hello"})
    assert chars is None
    assert block_type is None


def test_unsized_blocks_from_tool_result_image_and_document(tmp_path: Path):
    lines = [
        turn_line(
            message_id="msg_1",
            content=[tool_use_block("Read", "tu1", {"file_path": "x"})],
        ),
        user_block_line(
            [
                tool_result_block(
                    "tu1",
                    [
                        _document_block(),
                        _image_block("image/png", "not-valid-base64!!!"),
                    ],
                )
            ]
        ),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    assert result.parser_notes["unsized_blocks"] == {"document": 1, "image": 1}


def test_unsized_blocks_from_human_prompt_image(tmp_path: Path):
    lines = [
        user_str_line(
            "look",
            message={
                "role": "user",
                "content": [
                    {"type": "text", "text": "look"},
                    _image_block("image/png", "not-valid-base64!!!"),
                ],
            },
        ),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    assert result.parser_notes.get("unsized_blocks", {}).get("image") == 1


def test_sized_image_in_tool_result_is_not_unsized(tmp_path: Path):
    lines = [
        turn_line(
            message_id="msg_1",
            content=[tool_use_block("Read", "tu1", {"file_path": "x"})],
        ),
        user_block_line([tool_result_block("tu1", [_image_block("image/png", _TINY_PNG)])]),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    assert "unsized_blocks" not in result.parser_notes
    tokens = events.image_token_estimate(1, 1)
    assert result.tool_result_chars["Read"] == tokens * events._CHARS_PER_TOKEN_APPROX

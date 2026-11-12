"""Whether a coaching hint could have fired (``hint_occasions``): the parser's
``Turn.background_waiting``, which the status-check moment reads, and the
count of moments by the hook's own triggers.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from claudeglass import capture_catalogue as cat
from claudeglass.hooks import capture_hook
from claudeglass.model import TranscriptMeta
from claudeglass.parse import parse_transcript

from helpers import (
    tool_result_block,
    tool_use_block,
    turn_line,
    user_block_line,
    user_str_line,
    write_jsonl,
)


def _at(minute: float) -> str:
    whole = int(minute)
    return f"2026-09-18T{12 + whole // 60:02d}:{whole % 60:02d}:{int((minute - whole) * 60):02d}.000Z"


def _said(text: str, minute: float, **extra) -> dict:
    return user_str_line(text, timestamp=_at(minute), origin={"kind": "human"}, **extra)


def _reply(minute: float, *, say: str = "Done.", ctx: int = 40_000, one_hour: int = 0, content=None) -> dict:
    content = content if content is not None else [{"type": "text", "text": say}]
    return turn_line(
        timestamp=_at(minute), content=content, cache_read_input_tokens=ctx, output_tokens=200,
        ephemeral_1h_input_tokens=one_hour,
    )


LAUNCH_SHELL = "Command running in background with ID: b1. Output is being written to a file."
LAUNCH_AGENT = "Async agent launched successfully. It works in the background."


def _finished(call_id: str | None, minute: float) -> dict:
    """A background task's finishing message as a queue operation, naming the
    call that started it (or none)."""
    named = f"<tool-use-id>{call_id}</tool-use-id>\n" if call_id else ""
    return {
        "type": "queue-operation", "operation": "enqueue", "timestamp": _at(minute),
        "content": f"<task-notification>\n<task-id>b1</task-id>\n{named}<status>completed</status>\n</task-notification>",
    }


def _started(result: str, minute: float = 1, *, call_id: str = "toolu_bg", sidechain: bool = False, **reply) -> list[dict]:
    """A message, a reply that starts work, the tool result saying where it
    went and a short reply after it."""
    back = user_block_line([tool_result_block(call_id, result)], timestamp=_at(minute + 0.5))
    if sidechain:
        back["isSidechain"] = True
    return [
        _said("run the build", minute - 1),
        _reply(minute, content=[tool_use_block("Bash", call_id, {"command": "make"})], **reply),
        back,
        _reply(minute + 1, say="Started it.", **reply),
    ]


def _parse(tmp_path, lines, name="s.jsonl"):
    path = tmp_path / name
    write_jsonl(path, lines)
    return parse_transcript(path, TranscriptMeta(path=str(path)))


# -- what the parser keeps ------------------------------------------------------------


def _scenarios() -> dict[str, list[dict]]:
    ask = _said("how is it going?", 4)
    answer = _reply(5)
    return {
        "shell launch": [*_started(LAUNCH_SHELL), ask, answer],
        "agent launch": [*_started(LAUNCH_AGENT), ask, answer],
        "finished, named": [*_started(LAUNCH_SHELL), _finished("toolu_bg", 3), ask, answer],
        "finished, another call": [*_started(LAUNCH_SHELL), _finished("toolu_other", 3), ask, answer],
        "finished, names no call": [*_started(LAUNCH_SHELL), _finished(None, 3), ask, answer],
        "a subagent's launch": [*_started(LAUNCH_SHELL, sidechain=True), ask, answer],
        "ran to the end": [*_started("Command finished with exit code 0."), ask, answer],
        "launch past the first 400 characters": [*_started("x" * 500 + LAUNCH_SHELL), ask, answer],
        "launched again after a finish": [
            *_started(LAUNCH_SHELL), _finished("toolu_bg", 3),
            *_started(LAUNCH_AGENT, 6, call_id="toolu_two"), _said("any updates?", 9), _reply(10),
        ],
        "nothing started": [_said("hello there", 0), _reply(1), ask, answer],
    }


def test_the_parser_flags_background_waiting_as_the_hook_reads_it(tmp_path):
    coaching = capture_hook.load_catalogue()["coaching"]
    seen = {True: 0, False: 0}
    for index, (name, lines) in enumerate(_scenarios().items()):
        result = _parse(tmp_path, lines, name=f"s{index}.jsonl")
        for n, line in enumerate(lines):
            if line.get("origin") != {"kind": "human"}:
                continue
            asked = datetime.fromisoformat(line["timestamp"].replace("Z", "+00:00")).astimezone(timezone.utc)
            opened = next(t for t in result.turns if datetime.fromisoformat(t.ts.replace("Z", "+00:00")) > asked)
            expected = capture_hook._background_pending(lines[:n], coaching)
            assert opened.background_waiting is expected, (name, n)
            seen[expected] += 1
    # Both answers came up, so the comparison is not vacuous.
    assert seen[True] >= 3 and seen[False] >= 8


def test_a_typed_line_with_background_work_out_keeps_a_flag_and_no_words(tmp_path):
    result = _parse(tmp_path, [*_started(LAUNCH_SHELL), _said("how is it going?", 4), _reply(5)])
    typed = [e for e in result.events if e.kind.name == "HUMAN_TEXT"]
    assert [e.detail.get("waiting") for e in typed] == [None, True]
    assert [t.background_waiting for t in result.turns] == [False, False, True]
    assert cat.BACKGROUND_LAUNCH_PATTERN and LAUNCH_SHELL not in repr(result.events)

"""Q1 part D: the checks that read the answers capture collects
(``capture_checks``): the summary question ``lost`` and its check. The
evidence reads the salted read and edit hashes of a turn, which the
dashboard gets from the store, not from the transcript files.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from claudeglass import parse
from claudeglass.parse import parse_transcript
from claudeglass.model import TranscriptMeta
from claudeglass.service.rebuild import corpus_from_store

from helpers import (
    store_with_older_sessions,
    tool_result_block,
    tool_use_block,
    turn_line,
    user_block_line,
    user_str_line,
    write_jsonl,
)

_COMMON = {"cwd": "C:/work/proj"}


def _stamper(when: datetime):
    def stamp(seconds: int) -> str:
        return (when + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    return stamp


def _read_and_edit_session(session_id: str, when: datetime) -> list[dict]:
    """A message, a reply that reads ``a.py`` and one that edits ``b.py``."""
    stamp = _stamper(when)
    common = {"sessionId": session_id, **_COMMON}
    return [
        user_str_line("look at a and change b", timestamp=stamp(0), **common),
        turn_line(
            content=[tool_use_block("Read", "tu_read", {"file_path": "C:/work/proj/a.py"})],
            timestamp=stamp(1),
            **common,
        ),
        user_block_line([tool_result_block("tu_read", "contents")], timestamp=stamp(2), **common),
        turn_line(
            content=[
                tool_use_block(
                    "Edit", "tu_edit", {"file_path": "C:/work/proj/b.py", "old_string": "x", "new_string": "y"}
                )
            ],
            timestamp=stamp(3),
            **common,
        ),
        user_block_line([tool_result_block("tu_edit", "edited")], timestamp=stamp(4), **common),
        turn_line(content=[{"type": "text", "text": "done"}], timestamp=stamp(5), **common),
    ]


def test_the_read_and_edit_hashes_survive_the_store(tmp_path):
    # Part D's evidence (a file read again after a summary) needs both hashes on the dashboard's path,
    # which is corpus_from_store: a store that dropped them would leave only corrections as evidence.
    parse.set_salt(b"s" * 32)
    when = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=2)
    lines = _read_and_edit_session("sess-a", when)
    path = tmp_path / "direct.jsonl"
    write_jsonl(path, lines)
    direct = parse_transcript(path, TranscriptMeta(path=str(path), kind="top-level", session_id="sess-a"))
    wanted = [(t.read_target_hashes, t.edit_target_hashes) for t in direct.turns]
    assert any(reads for reads, _ in wanted) and any(edits for _, edits in wanted)

    store, _ = store_with_older_sessions(tmp_path / "store", {"sess-a": lines}, older=set())
    [bundle] = corpus_from_store(store).sessions
    assert [(t.read_target_hashes, t.edit_target_hashes) for t in bundle.top.turns] == wanted

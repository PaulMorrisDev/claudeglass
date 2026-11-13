"""Streaming line-level reader for Claude Code transcript JSONL files.

Every other module in this package reads a transcript through
``iter_lines`` so line handling — encoding tolerance, blank-line
skipping, unparsable-line and truncated-final-line counting, oversized-line
skipping, and Windows long-path handling — lives in exactly one place.

Streaming: ``iter_lines`` never reads the whole file into memory. It keeps
at most one line of lookahead (to tell whether the current line is the
file's last non-blank line, for truncated-final-line detection), so peak
memory stays proportional to the longest single line, not file size.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

#: Lines larger than this (measured as UTF-8 encoded bytes) are skipped
#: without being parsed, and counted in ``LineStats.oversized_lines``.
MAX_LINE_BYTES = 8 * 1024 * 1024

#: Windows MAX_PATH is 260; a path at or beyond this length needs the
#: ``\\?\`` extended-length prefix to open reliably. Matches the plan's
#: "Windows long paths: prefix \\?\ when len > 255" instruction.
_LONG_PATH_THRESHOLD = 255


@dataclass(slots=True)
class LineStats:
    """Parse-quality counters accumulated by one ``iter_lines`` call.

    Callers fold these into ``model.Diagnostics``: ``lines``,
    ``unparsable_lines``, ``truncated_final_line``, ``oversized_lines``
    (a WP1 addition to ``Diagnostics`` — see model.py's module docstring).

    ``bytes_read`` is how far into the file the lines handled so far reach
    and ``line_start`` where the latest one began: each line's text in UTF-8
    plus the white space around it (one byte a character), blank, oversized
    and unparsable lines included. It is the file's size to the byte for
    ASCII white space and valid UTF-8, which is all the parser uses it for
    (how far back the capture hook's 4 MB tail reaches).
    """

    lines: int = 0
    unparsable_lines: int = 0
    truncated_final_line: bool = False
    oversized_lines: int = 0
    bytes_read: int = 0
    line_start: int = 0


def _windows_long_path(path: Path) -> str:
    """Return a path string safe to pass to ``open`` when ``path`` is at
    or beyond the long-path threshold, by prefixing the ``\\?\\``
    extended-length marker (a UNC path gets ``\\?\\UNC\\`` instead).

    A no-op on non-Windows platforms and on paths under the threshold.
    """
    text = str(path)
    if os.name != "nt" or len(text) < _LONG_PATH_THRESHOLD:
        return text
    resolved = str(Path(path).resolve())
    if resolved.startswith("\\\\?\\"):
        return resolved
    if resolved.startswith("\\\\"):
        return "\\\\?\\UNC\\" + resolved.lstrip("\\")
    return "\\\\?\\" + resolved


def iter_lines(path: str | Path, stats: LineStats | None = None) -> Iterator[tuple[int, dict]]:
    """Stream ``(line_number, dict)`` pairs from a transcript JSONL file.

    ``line_number`` is 1-based, counting every physical line (including
    blank ones) so it matches up with ``stats.lines`` at the end.

    - Blank lines are skipped silently (not counted as unparsable).
    - A line whose UTF-8 encoded size exceeds ``MAX_LINE_BYTES`` is
      skipped without being parsed, and counted in
      ``stats.oversized_lines``.
    - A line that fails to parse as a JSON object is skipped and counted
      in ``stats.unparsable_lines`` — unless it is the last non-blank
      line in the file, in which case it is treated as a truncated write
      in progress (Claude Code appends to a live session file) and
      counted in ``stats.truncated_final_line`` instead.

    Opens with ``encoding="utf-8", errors="replace", newline=""`` per the
    project plan, and applies the Windows ``\\?\\`` long-path prefix when
    the path is long enough to need it.

    If ``stats`` is omitted, a private ``LineStats`` is used and
    discarded — pass one in to inspect the counts afterwards.
    """
    if stats is None:
        stats = LineStats()
    open_path = _windows_long_path(Path(path))
    line_no = 0
    with open(open_path, encoding="utf-8", errors="replace", newline="") as fh:
        pending: tuple[int, str] | None = None
        for raw in fh:
            line_no += 1
            if pending is not None:
                result = _process_line(pending[0], pending[1], stats, is_last=False)
                if result is not None:
                    yield result
            pending = (line_no, raw)
        if pending is not None:
            result = _process_line(pending[0], pending[1], stats, is_last=True)
            if result is not None:
                yield result
    stats.lines = line_no


def _process_line(
    line_no: int, raw: str, stats: LineStats, *, is_last: bool
) -> tuple[int, dict] | None:
    text = raw.strip()
    stats.line_start = stats.bytes_read
    if not text:
        stats.bytes_read += len(raw)
        return None
    size = len(text.encode("utf-8"))
    stats.bytes_read += size + len(raw) - len(text)
    if size > MAX_LINE_BYTES:
        stats.oversized_lines += 1
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
        ok = False
    else:
        ok = isinstance(parsed, dict)
    if not ok:
        if is_last:
            stats.truncated_final_line = True
        else:
            stats.unparsable_lines += 1
        return None
    return line_no, parsed


__all__ = ["iter_lines", "LineStats", "MAX_LINE_BYTES"]

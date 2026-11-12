"""Which figures a digest measured.

A digest Claude Code's cleanup left without its transcript can never be
read again by a newer parser, so the store keeps what an older parser
wrote. A field a parser version adds or redefines is not measured in such
a digest: every reader leaves that transcript out of that figure, never
reading it as 0 or in its older meaning. One table says which version each
field's meaning dates from; ``TranscriptResult.parser_version`` says which
parser wrote the digest.
"""

from __future__ import annotations

from collections.abc import Iterable

from .model import TranscriptResult

#: Field (on ``Turn`` or ``TranscriptResult``) -> the ``PARSER_VERSION``
#: whose meaning it has now: the version that added it or last redefined
#: it. A bump that adds or redefines a field a figure reads adds it here.
FIELD_VERSIONS: dict[str, int] = {
    "shell_read_count": 46,
    "shell_code_write_count": 50,
    "edit_code_count": 50,
    "coach_tip": 50,
    "coach_tips": 50,
    "coach_disowned": 49,
}
#: The version a digest must be from to measure every field above.
NEWEST = max(FIELD_VERSIONS.values())
#: ``TranscriptResult.parser_version`` of a digest written before the
#: stamp existed (parser 49 or earlier): ``cache.result_from_jsonable``
#: sets it where the key is missing.
BEFORE_STAMP = 0
#: The sets the readers ask about together.
TIP_FIELDS = ("coach_tip", "coach_tips", "coach_disowned")
CODE_WRITE_FIELDS = ("shell_code_write_count", "edit_code_count")


def measures(result: TranscriptResult, *fields: str) -> bool:
    """Whether ``result``'s digest measured every one of ``fields`` with
    today's meaning. A result built in memory (``parser_version`` None: a
    test's, never a digest's) reads as current."""
    version = result.parser_version
    return version is None or all(version >= FIELD_VERSIONS[name] for name in fields)


def is_older(result: TranscriptResult) -> bool:
    """Whether some figure leaves ``result`` out: its digest misses at least
    one field of :data:`FIELD_VERSIONS`."""
    version = result.parser_version
    return version is not None and version < NEWEST


def older_sessions(sessions: Iterable) -> tuple[int, int]:
    """``(n, m)``: of the ``m`` sessions with a top-level transcript, the
    ``n`` that hold at least one older digest (:func:`is_older`, top or
    agent). ``sessions`` are ``corpus.SessionBundle``s."""
    tops = [bundle for bundle in sessions if bundle.top is not None]
    return sum(1 for b in tops if any(is_older(r) for r in (b.top, *b.subs))), len(tops)


def older_sentence(n: int, m: int) -> str:
    """The Data quality line for :func:`older_sessions`; "" when ``n`` is 0."""
    if not n:
        return ""
    noun = "session" if m == 1 else "sessions"
    if n == 1:
        return (
            f"1 of {m:,} {noun} in this window was read by an older version and its transcripts are gone, "
            "so the newer figures leave it out."
        )
    return (
        f"{n:,} of {m:,} {noun} in this window were read by an older version and their transcripts are gone, "
        "so the newer figures leave them out."
    )


__all__ = [
    "BEFORE_STAMP",
    "CODE_WRITE_FIELDS",
    "FIELD_VERSIONS",
    "NEWEST",
    "TIP_FIELDS",
    "is_older",
    "measures",
    "older_sentence",
    "older_sessions",
]

"""What an export or a CLI report covers.

The dashboard reads its store, which keeps a digest of each session after
Claude Code deletes the transcript (``cleanupPeriodDays``, 30 days unless
set). ``tuning export``, ``export`` and the CLI report read transcripts on
disk. When the store knows of sessions in the window whose transcripts are
gone, they say so in one line: how many of the window's sessions the
figures cover and how long Claude Code keeps transcripts here
(``cli._store_coverage``). ``tuning export`` reads those sessions from the
store's digests instead and says how many it read that way.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

#: Claude Code's own ``cleanupPeriodDays`` when no setting gives one.
DEFAULT_KEPT_DAYS = 30


@dataclass(frozen=True, slots=True)
class Coverage:
    """One window's sessions: ``sessions`` known to the transcripts on disk
    or to the dashboard's store, ``read`` of them from transcripts now,
    ``stored`` from the store's digests because their transcripts are gone,
    and ``kept_days``, how long Claude Code keeps transcripts here."""

    sessions: int
    read: int
    stored: int = 0
    kept_days: int = DEFAULT_KEPT_DAYS


def kept_days(snapshots: Iterable) -> int:
    """``cleanupPeriodDays`` as the newest snapshot that records settings (an
    ``effective`` block) has it, else Claude Code's default. ``snapshots``
    are ``snapshots.Snapshot``s, oldest first (``snapshots.load_snapshots``)."""
    for snap in reversed(list(snapshots)):
        effective = snap.data.get("effective")
        if isinstance(effective, dict):
            value = effective.get("cleanupPeriodDays")
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value
            return DEFAULT_KEPT_DAYS
    return DEFAULT_KEPT_DAYS


def _days(n: int) -> str:
    return f"{n:,} day" if n == 1 else f"{n:,} days"


def gone_sentence(covered: int, sessions: int, days: int) -> str:
    """The line for figures that cover ``covered`` of the window's
    ``sessions``, the rest kept only by the dashboard; "" when they cover all."""
    if covered >= sessions:
        return ""
    noun = "session" if sessions == 1 else "sessions"
    return (
        f"These figures cover {covered:,} of {sessions:,} {noun} in this window. "
        f"Claude Code keeps transcripts for {_days(days)} here, and the dashboard has the other {sessions - covered:,}."
    )


def stored_sentence(stored: int, sessions: int, days: int) -> str:
    """The line for the ``stored`` of the window's ``sessions`` read from the
    dashboard's store; "" when there are none."""
    if not stored:
        return ""
    noun = "session" if sessions == 1 else "sessions"
    verb = "is" if stored == 1 else "are"
    return (
        f"{stored:,} of {sessions:,} {noun} in this window {verb} read from what the dashboard kept, "
        f"because Claude Code keeps transcripts for {_days(days)} here."
    )


__all__ = ["DEFAULT_KEPT_DAYS", "Coverage", "gone_sentence", "kept_days", "stored_sentence"]

"""What an export or a CLI report covers.

The dashboard reads its store, which keeps a digest of each session after
its transcript is gone from disk (Claude Code's own cleanup, a moved or
deleted project folder, or a projects folder the command was not given).
``tuning export``, ``export`` and the CLI report read transcripts on disk.
When the store knows of sessions in the window whose transcripts were not
found, they say so in one line: how many of the window's sessions the figures
cover (``cli._store_coverage``). ``tuning export`` reads those sessions from
the store's digests instead and says how many it read that way.

No sentence gives a reason for a missing transcript: nothing here can tell
which of those it was. A ``tuning summary`` line is read on the machine of
whoever was sent the file, so it names the machine that made it
(``of_file``).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Coverage:
    """One window's sessions: ``sessions`` known to the transcripts on disk
    or to the dashboard's store, ``read`` of them from transcripts now, and
    ``stored`` from the store's digests because their transcripts were not
    found."""

    sessions: int
    read: int
    stored: int = 0


def gone_sentence(covered: int, sessions: int, *, of_file: bool = False) -> str:
    """The line for figures that cover ``covered`` of the window's
    ``sessions``, the rest kept only by the dashboard; "" when they cover all.
    ``of_file`` words it for a reader of a file made on another machine."""
    if covered >= sessions:
        return ""
    noun = "session" if sessions == 1 else "sessions"
    left = sessions - covered
    if left == 1:
        rest = "The transcript of the other session was not found"
        tail = " on the machine that made this file." if of_file else ", and the dashboard still has that session."
    else:
        rest = f"The transcripts of the other {left:,} were not found"
        tail = " on the machine that made this file." if of_file else ", and the dashboard still has those sessions."
    return f"These figures cover {covered:,} of {sessions:,} {noun} in this window. {rest}{tail}"


def stored_sentence(stored: int, sessions: int, *, of_file: bool = False) -> str:
    """The line for the ``stored`` of the window's ``sessions`` read from the
    dashboard's store; "" when there are none. ``of_file`` words it for a
    reader of a file made on another machine."""
    if not stored:
        return ""
    noun = "session" if sessions == 1 else "sessions"
    if of_file:
        verb, found = ("was", "Its transcript was") if stored == 1 else ("were", "Their transcripts were")
        return (
            f"{stored:,} of {sessions:,} {noun} in this window {verb} read from what the dashboard kept "
            f"on the machine that made this file. {found} not found there."
        )
    verb = "is" if stored == 1 else "are"
    return (
        f"{stored:,} of {sessions:,} {noun} in this window {verb} read from what the dashboard kept, "
        "because their transcripts were not found."
    )


__all__ = ["Coverage", "gone_sentence", "stored_sentence"]

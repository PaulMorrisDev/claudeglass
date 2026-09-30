"""Token savers ClaudeGlass recognises by name: third-party tools whose
hook turns a tool call away and points Claude at the tool's own tools
instead. A turned-away call is a redirect, not a mistake, so the waste
section counts it apart (``waste.REDIRECT_CAUSE``) and the "What does
tokensave save you?" check weighs what the saver says it saved against
what it cost.

Today the registry holds tokensave (github.com/aovestdipaperino/tokensave).
Its strings were checked against v7.13.0, installed and run in a scratch
project (see ``docs/savers.md``):

- Its PreToolUse hook denies with JSON (``permissionDecision: "deny"``),
  which Claude Code writes into the transcript as ``PreToolUse:<Tool>
  hook error: <reason>``, with no ``[command]:`` part naming the hook.
  So the reason's own words are the only way to tell whose block it was:
  :data:`KNOWN_SAVERS` holds a phrase from each of its redirect messages.
- It records every call's estimate in ``~/.tokensave/global.db``, table
  ``savings_ledger`` (``ts``, ``project_path``, ``tool_name``,
  ``before_tokens``, ``after_tokens``), whether or not its
  ``report_savings`` setting adds a ``tokensave_metrics:`` line to the
  tool result (off by default in v7.13.0). :func:`read_ledger` reads
  that table, read-only, and keeps the numbers only.

Nothing here imports the rest of ClaudeGlass, so ``parse.py`` can use it.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class KnownSaver:
    name: str
    #: The prefix of its MCP tools' names.
    tool_prefix: str
    #: Phrases found only in its hook's redirect messages.
    markers: tuple[str, ...]
    #: The folder in a project that holds its index, when it keeps one.
    index_dir: str = ""
    #: Its own tools to name in advice, most useful first.
    search_tools: tuple[str, ...] = ()


TOKENSAVE = KnownSaver(
    name="tokensave",
    tool_prefix="mcp__tokensave__",
    markers=(
        # Grep, Bash grep/rg: "This Grep targets a code file in a
        # tokensave-indexed project and the pattern ... looks like a
        # symbol name." Glob and find: "This Glob searches a
        # tokensave-indexed project for files matching ...".
        "tokensave-indexed project",
        # An Explore agent: "STOP: Use tokensave MCP tools (...) instead
        # of agents for code research."
        "Use tokensave MCP tools",
        # The override every search redirect names.
        "TOKENSAVE_DISABLE_GREP_HOOK",
    ),
    index_dir=".tokensave",
    search_tools=("tokensave_context", "tokensave_search", "tokensave_files", "tokensave_read"),
)

KNOWN_SAVERS: tuple[KnownSaver, ...] = (TOKENSAVE,)

#: The ``subkind`` ``events.py`` gives a tool-denial event whose denial
#: was a saver's redirect. Claude Code tags a JSON hook deny
#: ``toolDenialKind: "permission-rule"``, the same as a deny rule you
#: wrote (checked against tokensave v7.13.0), so without this a redirect
#: would count as a request you turned down.
REDIRECT_DENIAL_KIND = "saver-redirect"


def saver_for_text(text: str) -> str | None:
    """The saver whose redirect message ``text`` is, or ``None``."""
    if not text:
        return None
    for saver in KNOWN_SAVERS:
        if any(marker in text for marker in saver.markers):
            return saver.name
    return None


def saver_for_tool(tool_name: str) -> str | None:
    """The saver whose MCP tool ``tool_name`` is, or ``None``."""
    for saver in KNOWN_SAVERS:
        if str(tool_name).startswith(saver.tool_prefix):
            return saver.name
    return None


def indexed(folder: str | Path | None, saver: KnownSaver = TOKENSAVE) -> bool:
    """Whether ``folder`` holds ``saver``'s index, so its hook turns
    searches and Explore agents there away."""
    if not folder or not saver.index_dir:
        return False
    try:
        return (Path(folder) / saver.index_dir).is_dir()
    except OSError:
        return False


def _rows(report, section_key: str, table_name: str) -> list[dict]:
    for section in getattr(report, "sections", None) or ():
        if section.key != section_key:
            continue
        for table in section.tables:
            if table.name == table_name:
                keys = [column.key for column in table.columns]
                return [dict(zip(keys, row)) for row in table.rows]
    return []


#: The least share of a window's tool calls that a saver's own calls and
#: redirects must make up before advice is worded for it
#: (``min_share``). Below this it was tried, not relied on: one scratch
#: session among a month's work shouldn't turn every "find code" tip
#: into "use tokensave". The "What does tokensave save you?" check
#: itself shows on any use.
ADVICE_MIN_SHARE = 0.02


def _count(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _meets(own: float, total: float, min_share: float) -> bool:
    if own <= 0:
        return False
    return min_share <= 0 or not total or own / total >= min_share


def active_in_report(report, saver: KnownSaver = TOKENSAVE, *, min_share: float = 0.0) -> bool:
    """Whether ``saver`` was at work in the report's window: Claude called
    one of its tools (``carry_by_tool``) or its hook turned a call away
    (``waste_blocked_by``). With ``min_share`` (:data:`ADVICE_MIN_SHARE`),
    only when those calls and redirected replies were at least that share
    of the window's tool results."""
    tool_rows = _rows(report, "carry", "carry_by_tool")
    own = sum(_count(row.get("result_count")) or 1 for row in tool_rows if str(row.get("key") or "").startswith(saver.tool_prefix))
    own += sum(
        _count(row.get("turns")) or 1
        for row in _rows(report, "waste", "waste_blocked_by")
        if row.get("kind") == "saver" and row.get("blocker") == saver.name
    )
    return _meets(own, sum(_count(row.get("result_count")) for row in tool_rows), min_share)


def active_in_turns(turns, saver: KnownSaver = TOKENSAVE, *, min_share: float = 0.0) -> bool:
    """The same, from parsed turns rather than a finished report."""
    own, total = calls_in_turns(turns, saver)
    return _meets(own, total, min_share)


def calls_in_turns(turns, saver: KnownSaver = TOKENSAVE) -> tuple[int, int]:
    """``(saver's own calls and redirects, every tool call)`` in
    ``turns``, for a caller that adds several sessions up before judging
    them against :data:`ADVICE_MIN_SHARE`."""
    own = total = 0
    for turn in turns:
        calls = getattr(turn, "tool_calls_by_tool", None) or {}
        total += sum(calls.values())
        own += sum(n for name, n in calls.items() if str(name).startswith(saver.tool_prefix))
        own += (getattr(turn, "saver_redirects", None) or {}).get(saver.name, 0)
    return own, total


def ledger_path(home: str | Path | None = None) -> Path:
    return Path(home or Path.home()) / ".tokensave" / "global.db"


@dataclass(slots=True)
class LedgerTotals:
    """What tokensave's ledger recorded for a window: its estimate of
    the tokens its answers stood in for (``before``) and what its
    answers cost (``after``, its schema and request overhead included)."""

    calls: int = 0
    before: int = 0
    after: int = 0
    #: Calls whose answer cost more than it stood in for.
    losing_calls: int = 0

    @property
    def saved(self) -> int:
        return self.before - self.after


def _normalized_path(value) -> str:
    """A path for comparison, case- and symlink-insensitive (the same
    normalisation ``discovery.py`` uses for a project folder): tokensave
    records its own ``os.getcwd()`` verbatim, which can differ in case or
    trailing separators from the ``Path`` this report already resolved
    for the same project."""
    text = str(value or "")
    try:
        return os.path.normcase(os.path.realpath(text))
    except OSError:
        return os.path.normcase(text)


def read_ledger(
    since_ts: float | None,
    until_ts: float | None,
    *,
    home: str | Path | None = None,
    project: str | Path | None = None,
) -> LedgerTotals | None:
    """tokensave's ledger totals for calls at or after ``since_ts`` and
    before ``until_ts`` (Unix seconds; ``None`` leaves that end open), or
    ``None`` when there is no ledger to read. Read-only; numbers only.

    ``project``, when given, keeps only rows whose own ``project_path``
    matches it (:func:`_normalized_path`) -- a report already limited to
    one project should not fold in another project's calls, the same way
    :func:`active_in_report` is read off a single report's own tables.
    Omit it for a report that spans every project, so its figures aren't
    quietly narrowed to whichever project happened to be read first."""
    path = ledger_path(home)
    if not path.is_file():
        return None
    where, args = [], []
    if since_ts is not None:
        where.append("ts >= ?")
        args.append(int(since_ts))
    if until_ts is not None:
        where.append("ts < ?")
        args.append(int(until_ts))
    sql = (
        "SELECT project_path, before_tokens, after_tokens FROM savings_ledger"
        + (" WHERE " + " AND ".join(where) if where else "")
    )
    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=2) as conn:
            rows = conn.execute(sql, args).fetchall()
    except (sqlite3.Error, OSError, ValueError):
        return None
    wanted = _normalized_path(project) if project is not None else None
    calls = before = after = losing = 0
    for project_path, before_tokens, after_tokens in rows:
        if wanted is not None and _normalized_path(project_path) != wanted:
            continue
        before_tokens, after_tokens = int(before_tokens or 0), int(after_tokens or 0)
        calls += 1
        before += before_tokens
        after += after_tokens
        if after_tokens > before_tokens:
            losing += 1
    return LedgerTotals(calls=calls, before=before, after=after, losing_calls=losing)


__all__ = [
    "KNOWN_SAVERS",
    "KnownSaver",
    "ADVICE_MIN_SHARE",
    "LedgerTotals",
    "REDIRECT_DENIAL_KIND",
    "TOKENSAVE",
    "active_in_report",
    "active_in_turns",
    "calls_in_turns",
    "indexed",
    "ledger_path",
    "read_ledger",
    "saver_for_text",
    "saver_for_tool",
]

"""``Store``: the only code in this codebase that talks to the v0.2
service's SQLite file.

Every write goes through an ``upsert_*`` method (idempotent: re-running
the watcher over an unchanged file must not create a duplicate row) and
every read goes through a named query method (``summary``, ``sessions``,
``session``, ``daily_usage``, ``recache``, ``compactions``, ``snapshots``,
``tags``) that returns plain ``dict``/``list[dict]`` data — never a
``sqlite3.Row``, never a dataclass, never a raw local path (see
``service/schema.py``'s and ``service/__init__.py``'s privacy-rule
docstrings; ``tests/test_service_store.py`` enforces the path part of
that by construction).

One :class:`Store` may be shared across threads (the watcher thread and
the API server's request-handling threads both hold the same instance),
but a ``sqlite3.Connection`` may only be used from the thread that
created it. :class:`Store` works around this with one connection per
thread (``threading.local``), all pointed at the same on-disk file, each
opened in WAL journal mode (``PRAGMA journal_mode=WAL``) so a writer
(the watcher) and readers (API requests) don't block each other.

``migrate()`` is idempotent: every statement in ``schema.ALL_STATEMENTS``
is ``CREATE TABLE IF NOT EXISTS``/``CREATE INDEX IF NOT EXISTS``, so
calling it against an already-migrated database at the current
``schema.SCHEMA_VERSION`` is a no-op beyond recording
``meta['schema_version']`` again.

When the store's own recorded ``schema_version`` is *older* than the
running code's ``schema.SCHEMA_VERSION``, ``migrate()`` walks the
additive ``MIGRATIONS`` ladder (review B2) -- one ``ALTER TABLE``/
``CREATE INDEX`` step per version, run inside a single transaction that
stamps the new version last -- so an upgrade never loses a row. This
matters because the store is the one artefact documented to outlive
Claude Code's own ``cleanupPeriodDays`` transcript cleanup: dropping it
on every version bump would silently erase history nothing else can
re-derive once the source transcripts are gone. Drop-and-rebuild
remains the fallback for the two cases a ladder genuinely can't serve --
a recorded version *newer* than the code's own (e.g. a downgraded
install pointed at a store a later version already migrated), or a
recorded version with no registered ladder step (a version this codebase
never actually shipped, or one from further back than the ladder
reaches) -- and in either case the on-disk file is first copied aside to
a timestamped ``<path>.bak-<version>-<timestamp>``, never overwriting an
earlier backup (ROB-P6), and a warning printed, so a drop-and-rebuild
still never *silently* discards data. The store is otherwise a derived
cache over transcripts still on disk, never the source of truth, and
the next watcher tick repopulates a rebuilt store because
``known_files()`` is empty again -- except ``session_tags`` and
``session_feedback``, ``session_plan_feedback`` and ``tip_feedback`` (your
own tags and ratings from the dashboard), which nothing else can
re-derive: a drop-and-rebuild reads them before dropping and writes them
straight back once the tables are recreated
(``_export_marks``/``_reimport_marks``), so they survive even the two
cases above that the additive ladder can't serve.

A transcript whose file disappears from disk (review finding 3: "the
store must outlive Claude Code's own ``cleanupPeriodDays``") is never
deleted by the watcher's own poll tick -- ``remove_missing`` only marks
its ``missing_since`` timestamp (clearing it again if the file
reappears with the same path). Every read query that returns
transcripts (``session``) includes a missing-but-not-yet-pruned
transcript by default, same as one still on disk, so its stored
``digest_blob`` keeps serving reports/rebuild until the row is actually
removed by ``retention_prune`` or ``claudeglass serve --purge``.
``count_missing_transcripts`` is the one query that reports the current
total, for ``/api/health``.

``GLOBAL_PROJECT_SLUG`` is the synthetic project slug the watcher files
every config snapshot under (``hooks/snapshot-config.py`` writes them all
to one ``<config_dir>/snapshots/`` folder). ``Store.snapshots()`` maps
this slug back to a ``None`` ``project_slug`` in its own read query, so
an API/UI consumer sees an honest "no project" rather than a fabricated
one (S1-integration fix 1.c). A schema-2 snapshot's own
``project_slug`` field, kept in ``digest_json``, still names the project
it was taken in; ``api.py`` reads that one.
"""

from __future__ import annotations

import contextlib
import json
import re
import shutil
import sqlite3
import sys
import threading
import time
import zlib
from collections.abc import Callable
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path

from . import schema
from .. import PARSER_VERSION
from ..cache import result_from_jsonable
from ..discovery import _resolve_window, _zone, local_day, redact_slug, source_label, ts_in_window, utc_stamp
from ..limits import limit_markers as _limit_markers
from ..model import EventKind

#: ``meta`` key recording the schema version the store's tables were
#: created under. Compared against ``schema.SCHEMA_VERSION`` by callers
#: that want to detect a stale store (see module docstring).
_SCHEMA_VERSION_KEY = "schema_version"

#: See module docstring's "GLOBAL_PROJECT_SLUG" paragraph.
GLOBAL_PROJECT_SLUG = "__global__"

#: How long a connection waits for another's write lock before SQLite
#: gives up with "database is locked" (its own default is 5 s).
_BUSY_TIMEOUT_S = 30.0

#: EST-P5: a still-unjudged prediction (never matched to a real change
#: point, or matched but not yet judged) is dropped by
#: ``Store.prune_predictions`` after this many days -- it was likely
#: never applied, or nothing traced it back to a change.
PREDICTIONS_UNSEEN_EXPIRY_DAYS = 90
#: A *judged* prediction is kept this much longer, so EST-P6's
#: calibration has a real history of predicted-vs-measured pairs to
#: learn from before ``Store.prune_predictions`` starts dropping them.
PREDICTIONS_JUDGED_EXPIRY_DAYS = 400

#: Matches every ``CREATE TABLE IF NOT EXISTS <name>`` statement in
#: ``schema.ALL_STATEMENTS``, so :meth:`Store.migrate` can derive the
#: exact set of tables to drop (in reverse -- child-before-parent --
#: order) from the same single source of truth as table creation,
#: rather than hand-maintaining a second list that could drift.
_CREATE_TABLE_RE = re.compile(r"CREATE TABLE IF NOT EXISTS\s+(\w+)")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def encode_digest_blob(digest_json: str) -> bytes:
    """Compress a ``cache.encode_result``-shaped JSON string for storage
    in ``transcripts.digest_blob`` (S1-perf item 4). A per-transcript
    digest is mostly repeated key names across thousands of ``Turn``/
    ``Event`` entries, which zlib compresses well; this is the single
    largest column in the store on a real corpus, so this is the
    largest single contributor to S1-perf's store-size target."""
    return zlib.compress(digest_json.encode("utf-8"))


def decode_digest_blob(blob: bytes) -> str:
    """Inverse of :func:`encode_digest_blob` — every reader of
    ``transcripts.digest_blob`` (``Store.turns_for_session``,
    ``watcher.py``'s ``_load_existing``, ``rebuild.py``) goes through
    this rather than calling ``zlib.decompress`` directly, so the one
    compression format is defined in one place."""
    return zlib.decompress(blob).decode("utf-8")


def bucket_start(dt: datetime) -> str:
    """The ``turns_agg.bucket`` value for ``dt``: the UTC quarter hour it
    falls in, ``%Y-%m-%dT%H:%M:00Z`` (minute 00, 15, 30 or 45; a time
    with no offset is read as UTC). The shape of a minute-rounded
    ``since``, so a bucket and a bound compare as strings, in time order.
    The watcher writes buckets with this and ``Store.daily_usage`` rounds
    a window's start down with it."""
    dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    return utc_stamp(dt.replace(minute=dt.minute - dt.minute % 15))


@contextlib.contextmanager
def _transaction(conn: sqlite3.Connection):
    """A real, explicit transaction for a connection opened with
    ``isolation_level=None`` (autocommit mode -- see :meth:`Store.
    _connection`). In that mode ``with conn:`` is a silent no-op: Python's
    ``sqlite3`` module only wraps a ``with`` block in an implicit
    transaction when ``isolation_level`` is *not* None, so every writer
    touching more than one table/statement was previously running with
    no atomicity at all -- a failure partway through left whatever had
    already executed committed (review finding 2/5). This issues an
    explicit ``BEGIN IMMEDIATE`` (taking the write lock up front, rather
    than deferring it to the first write statement and risking a
    SQLITE_BUSY upgrade later) and commits on success or rolls back on
    any exception, re-raising it either way.

    Not used around ``Store.migrate``'s own ``executescript`` calls:
    ``executescript`` issues its own implicit ``COMMIT`` of any pending
    transaction before running, which would silently end this one early
    -- and every statement it runs there is an idempotent ``CREATE TABLE
    IF NOT EXISTS``/``CREATE INDEX IF NOT EXISTS`` anyway, so partial
    application on failure is harmless (the next ``migrate()`` call
    finishes the job). It *is* used around the ``MIGRATIONS`` ladder
    steps below (plain ``conn.execute`` calls, never ``executescript``),
    so an upgrade's ``ALTER TABLE``/``CREATE INDEX`` statements and the
    version stamp that follows them either all land or none do.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _demote_parsed(conn: sqlite3.Connection) -> int:
    """Record every transcript parsed by this build or an older one as
    parsed by version 0, so the watcher's stale-parser check
    (``watcher.FileWatcher._resolve``) parses it again, and prices it
    again, the next time it finds the file on disk. Rows a newer build
    wrote are left alone, as the watcher leaves them. Takes the caller's
    connection, so a ``MIGRATIONS`` step can run it inside the ladder's
    own transaction; :meth:`Store.demote_parsed` runs it in one of its
    own. Returns the number of rows marked."""
    cursor = conn.execute(
        "UPDATE transcripts SET parser_version = 0 WHERE parser_version <= ?", (PARSER_VERSION,)
    )
    return cursor.rowcount


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


#: ``session_feedback``'s columns in the order the marks export reads them,
#: and what a store from before the column existed reads as.
_FEEDBACK_COLUMNS = (
    "session_id", "outcome", "slow", "worth", "helped", "set_at", "why", "missed_in", "tip", "tip_hint",
)
_FEEDBACK_ABSENT = {"why": "''", "missed_in": "NULL", "tip": "NULL", "tip_hint": "NULL"}


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, ddl_type: str) -> None:
    """``ALTER TABLE ... ADD COLUMN`` is not itself idempotent (it errors
    if the column is already there), so every ladder step in
    ``MIGRATIONS`` goes through this rather than a bare ``ALTER TABLE``
    -- a migration step that only half-applied (process killed
    mid-``migrate()``, before the version stamp landed) is safely
    re-run in full on the next ``open()``."""
    if column not in _table_columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")


def _migrate_4_to_5(conn: sqlite3.Connection) -> None:
    """v4 -> v5 (``schema.py``'s "Version 5" paragraph, review B2):
    baseline/profile content-hash dedupe columns, plus the baseline
    ``record_id`` natural key. SQLite cannot add a ``UNIQUE`` column via
    ``ALTER TABLE``, so that constraint moves to a separate unique index
    here -- ``CREATE_BASELINES`` also declares ``record_id TEXT UNIQUE``
    directly for a table created fresh at v5, so both paths end up with
    the same constraint."""
    _add_column_if_missing(conn, "profiles", "content_hash", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(conn, "baselines", "record_id", "TEXT")
    _add_column_if_missing(conn, "baselines", "content_hash", "TEXT NOT NULL DEFAULT ''")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_baselines_record_id ON baselines(record_id)")


def _migrate_5_to_6(conn: sqlite3.Connection) -> None:
    """v5 -> v6 (``schema.py``'s "Version 6" paragraph): the
    ``session_feedback`` table. :meth:`Store.migrate` has already run
    every ``CREATE ... IF NOT EXISTS`` before this step; creating it here
    too keeps the step whole on its own."""
    conn.execute(schema.CREATE_SESSION_FEEDBACK)


def _migrate_6_to_7(conn: sqlite3.Connection) -> None:
    """v6 -> v7 (``schema.py``'s "Version 7" paragraph, EST-P5): the
    ``predictions`` table. Not one of ``_export_marks``/``_reimport_marks``'s
    two protected tables -- unlike a tag or a rating, a prediction is
    always re-ingestible from ``prediction-log.jsonl`` on the next
    watcher tick, so a downgrade-then-drop-and-rebuild losing it and
    getting it back that way is fine (see ``Store.migrate``'s own
    docstring for why only ``session_tags``/``session_feedback`` get
    that extra protection). ``CREATE_PREDICTIONS`` is two statements (the
    table and its index), so unlike ``_migrate_5_to_6``'s single-table
    one-liner this runs each separately -- ``conn.execute`` (unlike
    ``executescript``, not usable here: see ``_transaction``'s own
    docstring) only ever takes one statement at a time."""
    for statement in schema.CREATE_PREDICTIONS.strip().split(";"):
        statement = statement.strip()
        if statement:
            conn.execute(statement)


def _migrate_7_to_8(conn: sqlite3.Connection) -> None:
    """v7 -> v8 (``schema.py``'s "Version 8" paragraph): the
    ``turns_agg.bucket`` column, NULL on every row already there. The
    rows are rebuilt with their buckets by the watcher's next parse of
    each transcript, so every stored transcript is marked for one
    (:func:`_demote_parsed`, the same step a rate-card change takes).
    A half-applied upgrade is retried in full, which both halves allow."""
    _add_column_if_missing(conn, "turns_agg", "bucket", "TEXT")
    _demote_parsed(conn)


def _migrate_8_to_9(conn: sqlite3.Connection) -> None:
    """v8 -> v9 (``schema.py``'s "Version 9" paragraph): the redesigned
    rating's columns on ``session_feedback`` and its two new tables. Each
    ``ALTER`` goes through :func:`_add_column_if_missing`, so a half-applied
    upgrade is retried in full."""
    _add_column_if_missing(conn, "session_feedback", "why", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(conn, "session_feedback", "missed_in", "TEXT")
    _add_column_if_missing(conn, "session_feedback", "tip", "TEXT")
    _add_column_if_missing(conn, "session_feedback", "tip_hint", "TEXT")
    conn.execute(schema.CREATE_SESSION_PLAN_FEEDBACK)
    conn.execute(schema.CREATE_TIP_FEEDBACK)


#: Additive migration ladder for :meth:`Store.migrate`, keyed by the
#: *recorded* version being migrated away from -- ``MIGRATIONS[4]`` takes
#: a v4 store to v5. Each step may only add columns/indexes/tables, never
#: drop or rewrite existing data (review B2: the store outlives Claude
#: Code's own transcript cleanup, so an upgrade must never lose a row).
#: A recorded version with no entry here -- older than anything this
#: ladder reaches -- falls back to backup-then-drop-and-rebuild, same as
#: a recorded version newer than the running code's own.
MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    4: _migrate_4_to_5,
    5: _migrate_5_to_6,
    6: _migrate_6_to_7,
    7: _migrate_7_to_8,
    8: _migrate_8_to_9,
}



def window_column(window_by: str) -> str:
    """The ``sessions`` column a window reads: ``last_ts`` for
    ``"last-reply"`` (a session counts when it was last active in the
    window, the default everywhere) and ``first_ts`` for
    ``"first-reply"`` (when it started, the "since my last change"
    window's rule)."""
    if window_by == "last-reply":
        return "last_ts"
    if window_by == "first-reply":
        return "first_ts"
    raise ValueError(f"unknown window_by: {window_by!r}")


#: The ``turns_agg`` columns ``Store.daily_usage`` adds up.
_USAGE_SUM_COLUMNS = (
    "turns", "input_tokens", "cache_creation_tokens", "cache_read_tokens",
    "output_tokens", "thinking_tokens", "cc_5m", "cc_1h", "cost",
)


def _turns_agg_window(
    since_dt: datetime | None, until_dt: datetime | None, zone: tzinfo | None
) -> tuple[str, list]:
    """The ``WHERE`` condition, and its parameters, that keeps the
    ``turns_agg`` rows a window holds, for :meth:`Store.daily_usage`.

    A row with a ``bucket`` is in when that quarter hour starts at or after
    the window's start rounded down to a quarter hour, and strictly before
    its end (so an ``until`` on a quarter-hour edge doesn't pull in the
    quarter hour after it). A window can take in up to 15 minutes more
    than asked at either end; a local midnight is always on a bucket edge,
    so a calendar window is exact.

    A row with no bucket (written before schema 8, kept for a transcript
    that can't be parsed again, or a turn with no timestamp) can only be
    placed by its UTC ``day``, and is shown under that day. It is kept
    when that day lies within both the window's UTC days and its local
    days in ``zone`` (``None`` is the machine's own zone), so it never adds
    a day outside the window's local ones. With no ``until`` the window
    ends today, which also leaves out a turn with no timestamp (day
    ``unknown``) as soon as any bound is set.

    A row's ``day`` is its bucket's first ten characters (the watcher
    writes both from one timestamp), so the window's UTC days also
    bound ``day`` for the rows of both kinds. That plain range, outside
    the ``OR``, is what lets SQLite read ``idx_turns_agg_day`` instead of
    every row, now that there is a row per quarter hour."""
    bucket = ["a.bucket IS NOT NULL"]
    legacy = ["a.bucket IS NULL"]
    day_range: list[str] = []
    day_params: list = []
    bucket_params: list = []
    legacy_params: list = []
    if since_dt is not None:
        since_utc = since_dt.astimezone(timezone.utc)
        since_day = since_utc.date().isoformat()
        day_range.append("a.day >= ?")
        day_params.append(since_day)
        bucket.append("a.bucket >= ?")
        bucket_params.append(bucket_start(since_utc))
        legacy.append("a.day >= ?")
        legacy_params.append(max(since_day, local_day(since_utc, zone)))
    if until_dt is not None:
        until_utc = until_dt.astimezone(timezone.utc)
        until_day = until_utc.date().isoformat()
        day_range.append("a.day <= ?")
        day_params.append(until_day)
        bucket.append("a.bucket < ?")
        bucket_params.append(utc_stamp(until_utc, seconds=True))
        last_day = min(until_day, local_day(until_utc, zone))
    else:
        last_day = local_day(datetime.now(timezone.utc), zone)
    legacy.append("a.day <= ?")
    legacy_params.append(last_day)
    kept = f"(({' AND '.join(bucket)}) OR ({' AND '.join(legacy)}))"
    condition = " AND ".join([*day_range, kept])
    return f"({condition})", day_params + bucket_params + legacy_params


def _by_local_day(rows: list[sqlite3.Row], zone: tzinfo | None, *, split_agent: bool) -> list[dict]:
    """Add up ``daily_usage``'s per-bucket rows into one row per local day
    in ``zone`` (``None`` is the machine's own zone), per model (and per
    agent with ``split_agent``), sorted by day. A row with no bucket keeps
    its stored UTC day. Each bucket's local day is worked out once."""
    days: dict[str, str] = {}
    merged: dict[tuple, dict] = {}
    for row in rows:
        bucket = row["bucket"]
        if bucket is None:
            day = row["day"]
        else:
            day = days.get(bucket)
            if day is None:
                day = days[bucket] = bucket[:10] if zone is timezone.utc else local_day(bucket, zone)
        key = (day, row["agent"], row["model"]) if split_agent else (day, row["model"])
        out = merged.get(key)
        if out is None:
            out = {"day": day}
            if split_agent:
                out["agent"] = row["agent"]
            out["model"] = row["model"]
            for column in _USAGE_SUM_COLUMNS:
                out[column] = row[column]
            merged[key] = out
        else:
            for column in _USAGE_SUM_COLUMNS:
                out[column] += row[column]
    return [merged[key] for key in sorted(merged)]


class Store:
    """One SQLite-backed store, rooted at ``path``.

    ``path`` may be ``":memory:"`` for tests; every real (file-backed)
    store additionally gets WAL journal mode so concurrent readers don't
    block the watcher's writes.
    """

    def __init__(self, path: str | Path, config_dir: str | Path | None = None) -> None:
        self.path = str(path)
        #: The data folder, for what lives beside the store (the tags
        #: Claude Haiku wrote, ``haiku_tags``); ``None`` in tests.
        self.config_dir = Path(config_dir) if config_dir is not None else None
        self._local = threading.local()

    # -- connection lifecycle ------------------------------------------

    def open(self) -> None:
        """Open (or reuse) this thread's connection and ensure the
        schema exists. Safe to call more than once per thread."""
        self._connection()
        self.migrate()

    def close(self) -> None:
        """Close this thread's connection, if one is open. Other
        threads' connections (if any) are unaffected — each thread must
        call ``close()`` itself, typically at thread exit."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def _connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            # A generous busy timeout: a scan's write transaction or a WAL
            # checkpoint can hold the write lock for seconds on a big
            # corpus, and waiting out a slow neighbour beats failing.
            conn = sqlite3.connect(self.path, isolation_level=None, timeout=_BUSY_TIMEOUT_S)
            try:
                self._configure(conn)
            except BaseException:
                conn.close()
                raise
            self._local.conn = conn
        return conn

    def _configure(self, conn: sqlite3.Connection) -> None:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            conn.execute("PRAGMA journal_mode = WAL")
            # S1-perf item 3: NORMAL still fsyncs at every checkpoint
            # (durable against an application crash) but no longer at
            # every transaction commit as FULL does -- WAL mode's own
            # documented safety guarantee ("consistent after a crash,
            # perhaps missing the last few committed transactions")
            # is an acceptable trade for a store that's a rebuildable
            # cache over transcripts still on disk (module docstring),
            # never the source of truth, in exchange for a large cut
            # in per-transaction write latency. temp_store=MEMORY
            # keeps SQLite's own internal temp b-trees (e.g. for a
            # multi-column ON CONFLICT upsert) off disk entirely.
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA temp_store = MEMORY")

    def _table_names_in_creation_order(self) -> list[str]:
        names: list[str] = []
        for statement in schema.ALL_STATEMENTS:
            names.extend(_CREATE_TABLE_RE.findall(statement))
        return names

    def _drop_all_tables(self, conn: sqlite3.Connection) -> None:
        """Drop every table this schema creates, child-before-parent (the
        reverse of ``schema.ALL_STATEMENTS``'s own dependency order), so
        a foreign key never blocks a drop. Used only for the two cases
        the ``MIGRATIONS`` ladder can't serve -- a recorded version newer
        than the running code's, or older with no registered ladder step
        (see :meth:`migrate`)."""
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            for table in reversed(self._table_names_in_creation_order()):
                conn.execute(f"DROP TABLE IF EXISTS {table}")
        finally:
            conn.execute("PRAGMA foreign_keys = ON")

    def _backup_before_rebuild(self, version: int) -> None:
        """Copy the on-disk store file aside as ``<path>.bak-<version>-
        <timestamp>`` (ROB-P6: timestamped, and never overwritten -- see
        below) before a drop-and-rebuild that the ``MIGRATIONS`` ladder
        can't serve (review B2), and print a warning naming where it
        went -- so a version this build can't migrate additively is
        never *silently* discarded. A no-op for an in-memory store
        (nothing on disk to copy)."""
        if self.path == ":memory:":
            return
        source = Path(self.path)
        if not source.exists():
            return
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        # ROB-P6: a timestamp (colon-free -- Windows paths can't hold one)
        # so a second rebuild of the same recorded version, later, gets
        # its own backup rather than silently overwriting the first --
        # and a numeric suffix on top of that, in the unlikely case two
        # rebuilds land in the same second, so this copy is genuinely
        # never overwritten.
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        backup = source.with_name(source.name + f".bak-{version}-{stamp}")
        suffix = 2
        while backup.exists():
            backup = source.with_name(source.name + f".bak-{version}-{stamp}-{suffix}")
            suffix += 1
        shutil.copy2(source, backup)
        print(
            f"claudeglass: store at {source} is schema version {version}, which "
            f"this build cannot migrate additively -- backed up to {backup} before "
            "rebuilding it from scratch",
            file=sys.stderr,
        )

    def _export_marks(
        self, conn: sqlite3.Connection
    ) -> tuple[list[tuple], list[tuple], list[tuple], list[tuple]]:
        """``(tag_rows, feedback_rows, plan_rows, tip_rows)`` currently in
        ``session_tags``/``session_feedback``/``session_plan_feedback``/
        ``tip_feedback``, or ``[]`` for a table that doesn't
        exist yet (an older store, or a fresh one); a ``session_feedback``
        from before v9 reads its newer columns as empty. ROB-P6: read before
        :meth:`_drop_all_tables` runs, so :meth:`_reimport_marks` can put
        them back once the tables are recreated -- unlike the rest of the
        store, a rating or a tag is never re-derivable from the
        transcripts on disk, so a drop-and-rebuild must not silently
        erase it the way it safely can everything else."""
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        tags = (
            conn.execute("SELECT session_id, key, value, set_at FROM session_tags").fetchall()
            if "session_tags" in tables
            else []
        )
        feedback = []
        if "session_feedback" in tables:
            held = _table_columns(conn, "session_feedback")
            select = ", ".join(
                column if column in held else f"{_FEEDBACK_ABSENT[column]} AS {column}"
                for column in _FEEDBACK_COLUMNS
            )
            feedback = conn.execute(f"SELECT {select} FROM session_feedback").fetchall()
        plans = (
            conn.execute("SELECT session_id, build, plan, handoff, set_at FROM session_plan_feedback").fetchall()
            if "session_plan_feedback" in tables
            else []
        )
        tips = (
            conn.execute("SELECT kind, item, answer, set_at FROM tip_feedback").fetchall()
            if "tip_feedback" in tables
            else []
        )
        return (
            [tuple(row) for row in tags],
            [tuple(row) for row in feedback],
            [tuple(row) for row in plans],
            [tuple(row) for row in tips],
        )

    def _reimport_marks(
        self,
        conn: sqlite3.Connection,
        tags: list[tuple],
        feedback: list[tuple],
        plans: list[tuple] = (),
        tips: list[tuple] = (),
    ) -> None:
        """Put rows :meth:`_export_marks` read back into the just-recreated
        ``session_tags``/``session_feedback``/``session_plan_feedback``/
        ``tip_feedback`` tables. The ``sessions`` row
        each one's ``session_id`` foreign key names doesn't exist again
        yet -- the next watcher tick repopulates it (same as every other
        table here) -- so this runs with foreign keys off, the same way
        :meth:`_drop_all_tables` already does for the drop itself."""
        if not (tags or feedback or plans or tips):
            return
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            conn.executemany(
                "INSERT INTO session_tags (session_id, key, value, set_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(session_id, key) DO UPDATE SET value = excluded.value, set_at = excluded.set_at",
                tags,
            )
            conn.executemany(
                "INSERT INTO session_feedback (session_id, outcome, slow, worth, helped, set_at, why, missed_in, "
                "tip, tip_hint) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
                "outcome = excluded.outcome, slow = excluded.slow, worth = excluded.worth, "
                "helped = excluded.helped, set_at = excluded.set_at, why = excluded.why, "
                "missed_in = excluded.missed_in, tip = excluded.tip, tip_hint = excluded.tip_hint",
                feedback,
            )
            conn.executemany(
                "INSERT INTO session_plan_feedback (session_id, build, plan, handoff, set_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(session_id, build) DO UPDATE SET plan = excluded.plan, "
                "handoff = excluded.handoff, set_at = excluded.set_at",
                plans,
            )
            conn.executemany(
                "INSERT INTO tip_feedback (kind, item, answer, set_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(kind, item) DO UPDATE SET answer = excluded.answer, set_at = excluded.set_at",
                tips,
            )
        finally:
            conn.execute("PRAGMA foreign_keys = ON")

    def migrate(self) -> None:
        """Create every table/index in ``schema.ALL_STATEMENTS`` if
        missing, and record ``schema.SCHEMA_VERSION`` in ``meta``.
        Idempotent when the store is already current.

        When the store's recorded version is *older* than
        ``schema.SCHEMA_VERSION``, every intervening version's
        ``MIGRATIONS`` step is run -- additive ``ALTER TABLE``/
        ``CREATE INDEX`` only, inside one transaction that stamps the
        new version last -- so existing rows survive the upgrade (review
        B2). Drop-and-rebuild (with a backup copy first, see
        :meth:`_backup_before_rebuild`) is used only for the two cases a
        ladder can't serve: a recorded version *newer* than the running
        code's own (e.g. a downgraded install pointed at a store a later
        version already migrated), or an older recorded version with no
        registered ladder step (nit 24: the original ``<``-only check
        left a newer-than-code store's stale shape in place instead of
        rebuilding it -- still handled here, just via backup-then-drop
        rather than a silent drop). Either way, ``session_tags``,
        ``session_feedback``, ``session_plan_feedback`` and ``tip_feedback``
        -- genuine user data, not a re-derivable
        cache over transcripts like the rest of the store -- are read
        before the drop and put back once the tables are recreated
        (ROB-P6, :meth:`_export_marks`/:meth:`_reimport_marks`)."""
        conn = self._connection()
        current = self.schema_version()
        marks: tuple[list[tuple], list[tuple], list[tuple], list[tuple]] | None = None

        if current is not None and current > schema.SCHEMA_VERSION:
            marks = self._export_marks(conn)
            self._backup_before_rebuild(current)
            self._drop_all_tables(conn)
        elif current is not None and current < schema.SCHEMA_VERSION:
            steps: list[Callable[[sqlite3.Connection], None]] = []
            version = current
            while version < schema.SCHEMA_VERSION:
                step = MIGRATIONS.get(version)
                if step is None:
                    marks = self._export_marks(conn)
                    self._backup_before_rebuild(current)
                    self._drop_all_tables(conn)
                    steps = []
                    break
                steps.append(step)
                version += 1
            if steps:
                # Ensure any wholly new table exists (a harmless re-run
                # of CREATE TABLE/INDEX IF NOT EXISTS against tables the
                # ladder steps below don't touch), then run every
                # version step and stamp the new version together so an
                # interrupted upgrade is safely retried in full.
                for statement in schema.ALL_STATEMENTS:
                    conn.executescript(statement)
                with _transaction(conn):
                    for step in steps:
                        step(conn)
                    conn.execute(
                        "INSERT INTO meta (key, value) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (_SCHEMA_VERSION_KEY, str(schema.SCHEMA_VERSION)),
                    )
                return

        with conn:
            for statement in schema.ALL_STATEMENTS:
                conn.executescript(statement)
            if marks is not None:
                self._reimport_marks(conn, *marks)
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (_SCHEMA_VERSION_KEY, str(schema.SCHEMA_VERSION)),
            )

    def schema_version(self) -> int | None:
        """The schema version recorded in ``meta``, or ``None`` if this
        store has never been migrated (including the very first call
        ever made against a brand new database, before ``meta`` itself
        exists)."""
        value = self.get_meta(_SCHEMA_VERSION_KEY)
        return int(value) if value is not None else None

    def get_meta(self, key: str) -> str | None:
        """One value from the ``meta`` key/value table (the schema
        version, and the watcher's own bookkeeping such as the rate card
        the stored costs were priced with), or ``None`` when the key is
        missing or ``meta`` itself doesn't exist yet."""
        try:
            row = self._connection().execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        except sqlite3.OperationalError:
            return None
        return row["value"] if row is not None else None

    def set_meta(self, key: str, value: str) -> None:
        """Write one value into the ``meta`` table, replacing any value
        already there."""
        conn = self._connection()
        with _transaction(conn):
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # -- writers ---------------------------------------------------------

    def _upsert_project(self, conn: sqlite3.Connection, slug: str, root_path: str) -> int:
        now = _now()
        conn.execute(
            "INSERT INTO projects (slug, root_path, first_seen, last_seen) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(slug) DO UPDATE SET root_path = excluded.root_path, last_seen = excluded.last_seen",
            (slug, root_path, now, now),
        )
        row = conn.execute("SELECT id FROM projects WHERE slug = ?", (slug,)).fetchone()
        return int(row["id"])

    def ensure_session(self, *, session_id: str, project_slug: str, project_root_path: str = "", slug: str = "") -> None:
        """Create a bare session row if none exists yet, leaving an
        existing row (and its folded totals) untouched. The watcher calls
        this before writing a session's transcript rows (a foreign key
        needs the session), so a dashboard read mid-scan never sees the
        session's cost and tokens reset to zero."""
        conn = self._connection()
        with _transaction(conn):
            project_id = self._upsert_project(conn, project_slug, project_root_path)
            conn.execute(
                "INSERT INTO sessions (id, project_id, slug, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(id) DO NOTHING",
                (session_id, project_id, slug or project_slug, _now()),
            )

    def upsert_session(
        self,
        *,
        session_id: str,
        project_slug: str,
        project_root_path: str = "",
        slug: str = "",
        first_ts: str | None = None,
        last_ts: str | None = None,
        span_s: float = 0.0,
        archetype: str | None = None,
        mode: str | None = None,
        mode_source: str | None = None,
        purpose: str | None = None,
        purpose_source: str | None = None,
        entrypoint: str | None = None,
        billing_mode: str | None = None,
        snapshot_id: int | None = None,
        profile_id: str | None = None,
        total_cost: float = 0.0,
        total_tokens: int = 0,
    ) -> None:
        """Insert or update one top-level session row (``SessionRecord``
        plus the cost/token totals folded from its transcripts)."""
        conn = self._connection()
        with _transaction(conn):
            project_id = self._upsert_project(conn, project_slug, project_root_path)
            conn.execute(
                """
                INSERT INTO sessions (
                    id, project_id, slug, first_ts, last_ts, span_s, archetype,
                    mode, mode_source, purpose, purpose_source, entrypoint,
                    billing_mode, snapshot_id, profile_id, total_cost,
                    total_tokens, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    project_id = excluded.project_id,
                    slug = excluded.slug,
                    first_ts = excluded.first_ts,
                    last_ts = excluded.last_ts,
                    span_s = excluded.span_s,
                    archetype = excluded.archetype,
                    mode = excluded.mode,
                    mode_source = excluded.mode_source,
                    purpose = excluded.purpose,
                    purpose_source = excluded.purpose_source,
                    entrypoint = excluded.entrypoint,
                    billing_mode = excluded.billing_mode,
                    snapshot_id = excluded.snapshot_id,
                    profile_id = excluded.profile_id,
                    total_cost = excluded.total_cost,
                    total_tokens = excluded.total_tokens,
                    updated_at = excluded.updated_at
                """,
                (
                    session_id, project_id, slug or project_slug, first_ts, last_ts,
                    span_s, archetype, mode, mode_source, purpose, purpose_source,
                    entrypoint, billing_mode, snapshot_id, profile_id, total_cost,
                    total_tokens, _now(),
                ),
            )

    def upsert_transcript(
        self,
        *,
        session_id: str,
        path: str,
        kind: str,
        agent_id: str | None = None,
        agent_type: str | None = None,
        spawn_depth: int = 0,
        parent_agent_id: str | None = None,
        mtime_ns: int = 0,
        size_bytes: int = 0,
        parser_version: int = 0,
        digest_json: str,
        turns_agg: list[dict] | None = None,
        recache_turns: list[dict] | None = None,
        events: list[dict] | None = None,
        compactions: list[dict] | None = None,
    ) -> int:
        """Insert or update one transcript row, replacing its
        ``turns_agg``/``recache_turns``/``events_agg``/``compactions``
        child rows wholesale (a re-parse always supersedes the previous
        breakdown for that file). Returns the transcript's row id. A
        ``turns_agg`` row's ``bucket`` (its UTC quarter hour, see
        :func:`bucket_start`) is stored as given, or NULL when it has none.

        S1-perf item 3: every child row's value tuple (minus the
        ``transcript_id`` it's keyed on, not known until the parent
        ``INSERT ... ON CONFLICT`` above runs) is built here, before the
        write transaction opens -- the ``.get()``/default-filling work
        for a transcript with thousands of turns is pure Python, not
        I/O, and doing it while the write lock (``BEGIN IMMEDIATE``) is
        held only extends how long every other connection blocks on it
        for no benefit. The transaction itself then does only the
        parent upsert, the four child-table deletes, and one
        ``executemany`` per child table -- a single prepared statement
        executed once per row via the C sqlite3 module, rather than
        ``execute()`` (a fresh Python-level call, parameter binding and
        round trip) per row.
        """
        turns_agg_rows = [
            (
                row["day"], row["model"], row.get("turns", 0),
                row.get("input_tokens", 0), row.get("cache_creation_tokens", 0),
                row.get("cache_read_tokens", 0), row.get("output_tokens", 0),
                row.get("thinking_tokens", 0), row.get("cc_5m", 0),
                row.get("cc_1h", 0), row.get("cost", 0.0), row.get("bucket"),
            )
            for row in turns_agg or []
        ]
        recache_turns_rows = [
            (
                row["turn_index"], row["signature"],
                row.get("cache_creation_tokens", 0),
                row.get("preceding_primary"), row.get("gap_s"),
            )
            for row in recache_turns or []
        ]
        events_agg_rows = [
            (row["kind"], row.get("subkind"), row["count"], row["dropped_tokens_sum"], row["duration_ms_sum"])
            for row in events or []
        ]
        compactions_rows = [
            (
                row["ts"], row.get("pre_tokens"),
                row.get("post_tokens"), row.get("dropped_tokens"),
                row.get("trigger"), row.get("join_delta_s"),
            )
            for row in compactions or []
        ]
        # S1-perf item 4: compressed here, outside the write transaction,
        # for the same reason the child-row tuples above are -- zlib is
        # pure CPU work with no need for the write lock held while it runs.
        digest_blob = encode_digest_blob(digest_json)

        conn = self._connection()
        with _transaction(conn):
            conn.execute(
                """
                INSERT INTO transcripts (
                    session_id, path, kind, agent_id, agent_type, spawn_depth,
                    parent_agent_id, mtime_ns, size_bytes, parser_version,
                    digest_blob, missing_since, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                ON CONFLICT(path) DO UPDATE SET
                    session_id = excluded.session_id,
                    kind = excluded.kind,
                    agent_id = excluded.agent_id,
                    agent_type = excluded.agent_type,
                    spawn_depth = excluded.spawn_depth,
                    parent_agent_id = excluded.parent_agent_id,
                    mtime_ns = excluded.mtime_ns,
                    size_bytes = excluded.size_bytes,
                    parser_version = excluded.parser_version,
                    digest_blob = excluded.digest_blob,
                    missing_since = NULL,
                    updated_at = excluded.updated_at
                """,
                (
                    session_id, path, kind, agent_id, agent_type, spawn_depth,
                    parent_agent_id, mtime_ns, size_bytes, parser_version,
                    digest_blob, _now(),
                ),
            )
            transcript_id = int(
                conn.execute("SELECT id FROM transcripts WHERE path = ?", (path,)).fetchone()["id"]
            )
            conn.execute("DELETE FROM turns_agg WHERE transcript_id = ?", (transcript_id,))
            conn.execute("DELETE FROM recache_turns WHERE transcript_id = ?", (transcript_id,))
            conn.execute("DELETE FROM events_agg WHERE transcript_id = ?", (transcript_id,))
            conn.execute("DELETE FROM compactions WHERE transcript_id = ?", (transcript_id,))
            if turns_agg_rows:
                conn.executemany(
                    """
                    INSERT INTO turns_agg (
                        transcript_id, day, model, turns, input_tokens,
                        cache_creation_tokens, cache_read_tokens, output_tokens,
                        thinking_tokens, cc_5m, cc_1h, cost, bucket
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [(transcript_id, *row) for row in turns_agg_rows],
                )
            if recache_turns_rows:
                conn.executemany(
                    """
                    INSERT INTO recache_turns (
                        transcript_id, turn_index, signature,
                        cache_creation_tokens, preceding_primary, gap_s
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [(transcript_id, *row) for row in recache_turns_rows],
                )
            if events_agg_rows:
                conn.executemany(
                    """
                    INSERT INTO events_agg (
                        transcript_id, kind, subkind, count, dropped_tokens_sum, duration_ms_sum
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [(transcript_id, *row) for row in events_agg_rows],
                )
            if compactions_rows:
                conn.executemany(
                    """
                    INSERT INTO compactions (
                        transcript_id, ts, pre_tokens, post_tokens, dropped_tokens,
                        trigger, join_delta_s
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [(transcript_id, *row) for row in compactions_rows],
                )
        return transcript_id

    def upsert_snapshot(
        self, *, project_slug: str, project_root_path: str = "", ts: str,
        schema_version: int, digest_json: str,
    ) -> int:
        """Insert or update one config-snapshot row (``snapshots.py``'s
        ``Snapshot``, already flattened/redacted), deduped by its natural
        key ``(project_id, ts, schema_version)`` (schema v2) so
        re-ingesting the same on-disk snapshot file on a later watcher
        tick updates the existing row instead of growing a duplicate one
        -- the same idempotent posture every other ``upsert_*`` method
        already has. Returns the snapshot's row id, so a caller can pass
        it as ``upsert_session``'s ``snapshot_id``."""
        conn = self._connection()
        with _transaction(conn):
            project_id = self._upsert_project(conn, project_slug, project_root_path)
            conn.execute(
                """
                INSERT INTO snapshots (project_id, ts, schema_version, digest_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(project_id, ts, schema_version) DO UPDATE SET
                    digest_json = excluded.digest_json
                """,
                (project_id, ts, schema_version, digest_json),
            )
            row = conn.execute(
                "SELECT id FROM snapshots WHERE project_id = ? AND ts = ? AND schema_version = ?",
                (project_id, ts, schema_version),
            ).fetchone()
            return int(row["id"])

    def upsert_workflow_run(
        self,
        *,
        session_id: str,
        run_id: str,
        agent_count: int = 0,
        phase_titles: list[str] | None = None,
        started: str | None = None,
        finished: str | None = None,
        cost: float = 0.0,
        status: str | None = None,
    ) -> int:
        """Insert or update one ``<session>/workflows/wf_*.json`` run row
        (``workflows.parse_workflow_file``/``link_workflow_agents``'s
        ``WorkflowRun``, already cost-linked by the caller), deduped by
        ``(session_id, run_id)``. ``phase_titles`` is stored as a JSON
        array of names only -- never ``detail``, which carries workflow
        source/prompt text (see ``workflows.py``'s module docstring).

        A row whose values are all unchanged is left alone, ``updated_at``
        included: :meth:`change_token` reads that column, so touching it
        on every re-read would tell every kept report the store changed
        when nothing did."""
        conn = self._connection()
        with _transaction(conn):
            conn.execute(
                """
                INSERT INTO workflow_runs (
                    session_id, run_id, agent_count, phases, started,
                    finished, cost, status, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, run_id) DO UPDATE SET
                    agent_count = excluded.agent_count,
                    phases = excluded.phases,
                    started = excluded.started,
                    finished = excluded.finished,
                    cost = excluded.cost,
                    status = excluded.status,
                    updated_at = excluded.updated_at
                WHERE agent_count IS NOT excluded.agent_count
                    OR phases IS NOT excluded.phases
                    OR started IS NOT excluded.started
                    OR finished IS NOT excluded.finished
                    OR cost IS NOT excluded.cost
                    OR status IS NOT excluded.status
                """,
                (
                    session_id, run_id, agent_count,
                    json.dumps(list(phase_titles or [])),
                    started, finished, cost, status, _now(),
                ),
            )
            row = conn.execute(
                "SELECT id FROM workflow_runs WHERE session_id = ? AND run_id = ?",
                (session_id, run_id),
            ).fetchone()
            return int(row["id"])

    def upsert_profile(
        self, *, profile_id: str, name: str, toml_path: str, content_hash: str | None = None
    ) -> None:
        """Insert or update one *user* profile's index row (v0.3's
        ``<config_dir>/profiles/<id>.toml`` -- never a catalogue id, see
        ``schema.CREATE_PROFILES``'s docstring).

        ``content_hash`` (v5), when given, makes a repeat call a true
        no-op (no write at all, so ``updated_at`` doesn't churn) when it
        matches the row already on file -- ``watcher._scan_profiles``'s
        own dedup, so re-ingesting an unchanged profile file on every
        poll tick never touches the database. ``None`` (the default,
        also every pre-v0.3 caller/test fixture) always writes, matching
        this method's original always-upsert behaviour exactly.
        """
        conn = self._connection()
        if content_hash is not None:
            existing = conn.execute(
                "SELECT content_hash FROM profiles WHERE id = ?", (profile_id,)
            ).fetchone()
            if existing is not None and existing["content_hash"] == content_hash:
                return
        # A single statement is already atomic under autocommit -- no
        # explicit transaction wrapper needed (see _transaction's own
        # docstring; this isn't one of the multi-statement writers finding
        # 2/5 is about).
        conn.execute(
            "INSERT INTO profiles (id, name, toml_path, content_hash, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name = excluded.name, toml_path = excluded.toml_path, "
            "content_hash = excluded.content_hash, updated_at = excluded.updated_at",
            (profile_id, name, toml_path, content_hash or "", _now()),
        )

    def record_baseline(
        self, *, project_slug: str, project_root_path: str = "", window_start: str,
        window_end: str, archetype: str | None, digest_json: str,
        record_id: str | None = None, content_hash: str | None = None,
    ) -> int:
        """Insert one baseline-capture row. Returns the baseline's row id.

        ``record_id``/``content_hash`` (v5), when both given, dedupe the
        same way :meth:`upsert_profile` does: ``watcher._scan_baselines``
        passes the baseline JSON record's own ``id`` field as
        ``record_id`` and a hash of the record's own content as
        ``content_hash`` -- re-ingesting the same (immutable-once-written)
        baseline file on a later tick with an unchanged hash is a no-op
        (the existing row's id is returned, nothing is written); a
        changed hash for the same ``record_id`` updates the existing row
        in place rather than growing a duplicate. Omitting either (every
        pre-v0.3 caller/test fixture) always inserts a new row, matching
        this method's original behaviour exactly.
        """
        conn = self._connection()
        with _transaction(conn):
            project_id = self._upsert_project(conn, project_slug, project_root_path)
            if record_id is not None:
                existing = conn.execute(
                    "SELECT id, content_hash FROM baselines WHERE record_id = ?", (record_id,)
                ).fetchone()
                if existing is not None:
                    if existing["content_hash"] == (content_hash or ""):
                        return int(existing["id"])
                    conn.execute(
                        "UPDATE baselines SET project_id = ?, window_start = ?, window_end = ?, "
                        "archetype = ?, digest_json = ?, content_hash = ? WHERE id = ?",
                        (
                            project_id, window_start, window_end, archetype, digest_json,
                            content_hash or "", existing["id"],
                        ),
                    )
                    return int(existing["id"])
            cursor = conn.execute(
                "INSERT INTO baselines (project_id, window_start, window_end, archetype, digest_json, "
                "record_id, content_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    project_id, window_start, window_end, archetype, digest_json,
                    record_id, content_hash or "", _now(),
                ),
            )
            return int(cursor.lastrowid)

    def known_files(self) -> dict[str, tuple[int, int, int]]:
        """``{path: (mtime_ns, size_bytes, parser_version)}`` for every
        transcript currently stored — the watcher's own incremental-diff
        basis, so it never has to re-stat/re-parse an unchanged file.
        ``parser_version`` is included (not just the file identity pair)
        so the watcher can also detect a transcript that hasn't changed
        on disk at all but was parsed under an older ``PARSER_VERSION``
        than the one now running -- see ``watcher.FileWatcher._resolve``.
        Local-only: never exposed through a read query or the API."""
        rows = self._connection().execute(
            "SELECT path, mtime_ns, size_bytes, parser_version FROM transcripts"
        ).fetchall()
        return {row["path"]: (row["mtime_ns"], row["size_bytes"], row["parser_version"]) for row in rows}

    def demote_parsed(self) -> int:
        """Mark every stored transcript this build (or an older one)
        parsed for a fresh parse (see :func:`_demote_parsed`). The
        watcher calls this when the rate card changes, so every
        transcript still on disk is priced again at the new rates, over
        as many ticks as it takes: the mark is in the store, so a project
        folder out of reach this tick, or a restart, doesn't lose it.
        Leaves ``updated_at`` alone, so :meth:`change_token` only moves
        once each row is actually parsed again. Returns the number of
        rows marked."""
        conn = self._connection()
        with _transaction(conn):
            return _demote_parsed(conn)

    def remove_missing(self, known_paths: set[str]) -> int:
        """Mark every transcript row whose ``path`` is not in
        ``known_paths`` (a file the watcher can no longer find on disk --
        deleted, or already past Claude Code's own ``cleanupPeriodDays``
        retention) with a ``missing_since`` timestamp, instead of
        deleting it outright (review finding 3: "the store must outlive
        ``cleanupPeriodDays``"). A missing transcript's stored
        ``digest_json`` is still enough to serve it in a report or
        ``rebuild.corpus_from_store`` -- only :meth:`retention_prune` or
        ``claudeglass serve --purge`` actually delete a transcript
        row. A transcript whose file has reappeared (``path`` is back in
        ``known_paths``) has its ``missing_since`` cleared again. Returns
        the number of transcripts *newly* marked missing on this call --
        see :meth:`count_missing_transcripts` for the running total."""
        conn = self._connection()
        with _transaction(conn):
            rows = conn.execute("SELECT id, path, missing_since FROM transcripts").fetchall()
            now = _now()
            newly_missing = 0
            for row in rows:
                is_known = row["path"] in known_paths
                if not is_known and row["missing_since"] is None:
                    conn.execute(
                        "UPDATE transcripts SET missing_since = ?, updated_at = ? WHERE id = ?",
                        (now, now, row["id"]),
                    )
                    newly_missing += 1
                elif is_known and row["missing_since"] is not None:
                    conn.execute(
                        "UPDATE transcripts SET missing_since = NULL, updated_at = ? WHERE id = ?",
                        (now, row["id"]),
                    )
        return newly_missing

    def count_missing_transcripts(self) -> int:
        """The running total of transcripts currently marked missing
        (``missing_since`` is set) -- what ``/api/health`` reports as
        ``transcripts_missing``, distinct from :meth:`remove_missing`'s
        own per-tick delta return value."""
        row = self._connection().execute(
            "SELECT COUNT(*) AS n FROM transcripts WHERE missing_since IS NOT NULL"
        ).fetchone()
        return int(row["n"])

    def retention_prune(self, retention_days: int) -> int:
        """Delete every session (and its transcripts/workflow runs/tags/
        child rows) last active more than ``retention_days`` ago. Returns
        the number of sessions removed. A transcript's file may still
        exist on disk (or have already been cleaned up by Claude Code's
        own ``cleanupPeriodDays``, or be marked missing via
        :meth:`remove_missing`) -- either way the store no longer needs
        rows for it once its session ages out of the configured
        retention window (plan "Locked-down installs" / "Retention and
        portability"). This is one of only two ways a session/transcript
        row is ever actually deleted (the other being
        ``claudeglass serve --purge``, which drops the whole
        store).

        The whole prune runs inside one explicit transaction (review
        finding 2/5): every child table with a foreign key into
        ``sessions``/``transcripts`` -- including ``workflow_runs``,
        which the original implementation omitted and which would
        otherwise raise ``sqlite3.IntegrityError`` on the ``sessions``
        delete for any session with a recorded workflow run -- is deleted
        before its parent, and a failure partway through rolls back the
        entire prune rather than leaving it half-applied."""
        cutoff = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - retention_days * 86400)
        )
        conn = self._connection()
        with _transaction(conn):
            rows = conn.execute(
                "SELECT id FROM sessions WHERE last_ts IS NOT NULL AND last_ts < ?", (cutoff,)
            ).fetchall()
            session_ids = [row["id"] for row in rows]
            for session_id in session_ids:
                transcript_rows = conn.execute(
                    "SELECT id FROM transcripts WHERE session_id = ?", (session_id,)
                ).fetchall()
                for trow in transcript_rows:
                    transcript_id = trow["id"]
                    conn.execute("DELETE FROM turns_agg WHERE transcript_id = ?", (transcript_id,))
                    conn.execute("DELETE FROM recache_turns WHERE transcript_id = ?", (transcript_id,))
                    conn.execute("DELETE FROM events_agg WHERE transcript_id = ?", (transcript_id,))
                    conn.execute("DELETE FROM compactions WHERE transcript_id = ?", (transcript_id,))
                conn.execute("DELETE FROM transcripts WHERE session_id = ?", (session_id,))
                conn.execute("DELETE FROM workflow_runs WHERE session_id = ?", (session_id,))
                conn.execute("DELETE FROM session_tags WHERE session_id = ?", (session_id,))
                conn.execute("DELETE FROM session_feedback WHERE session_id = ?", (session_id,))
                conn.execute("DELETE FROM session_plan_feedback WHERE session_id = ?", (session_id,))
                conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        return len(session_ids)

    def log_usage(self, *, ts: str, window_start: str | None, window_end: str | None,
                  utilization_pct: float | None, raw: dict) -> None:
        """Append one ``get_usage`` snapshot (see ``usage.py``'s
        ``log-usage``)."""
        conn = self._connection()
        # Single statement -- see upsert_profile's comment above.
        conn.execute(
            "INSERT INTO usage_log (ts, window_start, window_end, utilization_pct, raw_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (ts, window_start, window_end, utilization_pct, json.dumps(raw, sort_keys=True)),
        )

    # -- read queries (API-facing: never a local path) --------------------

    def change_token(self) -> str:
        """A cheap fingerprint of the store's current content -- changes
        whenever a transcript, snapshot, workflow run or session tag is
        added, removed, re-parsed or set, and only then. Combines each of
        ``transcripts``, ``snapshots``, ``workflow_runs`` and
        ``session_tags``' own row count with its own "latest touched"
        marker (``updated_at`` for transcripts/workflow_runs; ``ts``, the
        closest analogue, for snapshots, which have no ``updated_at``
        column; ``set_at`` for session_tags). ``workflow_runs``/
        ``session_tags`` were added under review finding 8 -- without
        them, a tag write or a freshly-linked workflow run left the
        report-model cache (``api.py``'s ``_get_report_model``) serving a
        stale report until some unrelated transcript/snapshot change
        happened to also invalidate it; ``session_feedback`` (v6) and, for
        the same reason, ``session_plan_feedback`` and ``tip_feedback`` (v9).
        Used by ``api.py``'s report-model cache to know when a cached report
        needs rebuilding, without exposing anything about *what* changed."""
        conn = self._connection()
        transcripts_row = conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(updated_at), '') FROM transcripts"
        ).fetchone()
        snapshots_row = conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(ts), '') FROM snapshots"
        ).fetchone()
        workflow_runs_row = conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(updated_at), '') FROM workflow_runs"
        ).fetchone()
        session_tags_row = conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(set_at), '') FROM session_tags"
        ).fetchone()
        feedback_row = conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(set_at), '') FROM session_feedback"
        ).fetchone()
        plan_feedback_row = conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(set_at), '') FROM session_plan_feedback"
        ).fetchone()
        tip_feedback_row = conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(set_at), '') FROM tip_feedback"
        ).fetchone()
        return (
            f"{transcripts_row[0]}:{transcripts_row[1]}:"
            f"{snapshots_row[0]}:{snapshots_row[1]}:"
            f"{workflow_runs_row[0]}:{workflow_runs_row[1]}:"
            f"{session_tags_row[0]}:{session_tags_row[1]}:"
            f"{feedback_row[0]}:{feedback_row[1]}:"
            f"{plan_feedback_row[0]}:{plan_feedback_row[1]}:"
            f"{tip_feedback_row[0]}:{tip_feedback_row[1]}"
        )

    #: Review finding 11: an extreme-length session's turn_series could
    #: otherwise ship tens of thousands of points to the browser (and,
    #: pre-finding-10-fix, feed a huge array into `Math.max.apply` there).
    #: Above this many priced turns, turns_for_session() downsamples.
    MAX_TURN_SERIES_POINTS = 5000

    def session_parts(self, session_id: str) -> dict:
        """What one session's cost is made of, for
        ``GET /api/session/<id>/explain``: token and cost totals per
        (transcript kind, agent type) and per model, cache rebuilds per
        signature, and the number of conversation summaries. Aggregates
        only -- no paths, no content."""
        conn = self._connection()
        by_agent = conn.execute(
            """
            SELECT t.kind AS kind, t.agent_type AS agent_type,
                   COUNT(DISTINCT t.id) AS runs,
                   COALESCE(SUM(a.turns), 0) AS turns,
                   COALESCE(SUM(a.input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(a.cache_creation_tokens), 0) AS cache_creation_tokens,
                   COALESCE(SUM(a.cache_read_tokens), 0) AS cache_read_tokens,
                   COALESCE(SUM(a.output_tokens), 0) AS output_tokens,
                   COALESCE(SUM(a.cost), 0) AS cost
            FROM transcripts t LEFT JOIN turns_agg a ON a.transcript_id = t.id
            WHERE t.session_id = ?
            GROUP BY t.kind, t.agent_type
            ORDER BY cost DESC
            """,
            (session_id,),
        ).fetchall()
        by_model = conn.execute(
            """
            SELECT a.model AS model,
                   SUM(a.turns) AS turns,
                   SUM(a.input_tokens) AS input_tokens,
                   SUM(a.cache_creation_tokens) AS cache_creation_tokens,
                   SUM(a.cc_5m) AS cc_5m,
                   SUM(a.cc_1h) AS cc_1h,
                   SUM(a.cache_read_tokens) AS cache_read_tokens,
                   SUM(a.output_tokens) AS output_tokens,
                   SUM(a.cost) AS cost
            FROM turns_agg a JOIN transcripts t ON a.transcript_id = t.id
            WHERE t.session_id = ?
            GROUP BY a.model
            ORDER BY cost DESC
            """,
            (session_id,),
        ).fetchall()
        rebuilds = conn.execute(
            """
            SELECT r.signature AS signature, COUNT(*) AS turns,
                   COALESCE(SUM(r.cache_creation_tokens), 0) AS tokens
            FROM recache_turns r JOIN transcripts t ON r.transcript_id = t.id
            WHERE t.session_id = ?
            GROUP BY r.signature
            ORDER BY tokens DESC
            """,
            (session_id,),
        ).fetchall()
        compactions = conn.execute(
            "SELECT COUNT(*) FROM compactions c JOIN transcripts t ON c.transcript_id = t.id WHERE t.session_id = ?",
            (session_id,),
        ).fetchone()[0]
        return {
            "by_agent": [dict(row) for row in by_agent],
            "by_model": [dict(row) for row in by_model],
            "rebuilds": [dict(row) for row in rebuilds],
            "compactions": int(compactions or 0),
        }

    def median_session_cost(self) -> float | None:
        """The median ``total_cost`` over sessions with any cost, or
        ``None`` when there are none."""
        costs = [
            row[0]
            for row in self._connection().execute(
                "SELECT total_cost FROM sessions WHERE total_cost > 0 ORDER BY total_cost"
            ).fetchall()
        ]
        if not costs:
            return None
        mid = len(costs) // 2
        return costs[mid] if len(costs) % 2 else (costs[mid - 1] + costs[mid]) / 2

    def turns_for_session(self, session_id: str) -> dict | None:
        """Per-turn ``ctx``/cache/marker series for one session's
        top-level transcript, decoded from its stored ``digest_blob``
        (the same lossless ``cache.result_from_jsonable`` decode
        ``service/rebuild.py`` uses) -- never re-parses a file, never
        exposes ``path`` or any other store-internal column. ``None``
        when the session has no stored top-level transcript.

        ``turn_series``: one ``[turn_index, ctx, cache_creation_tokens,
        is_recache, preceding_primary]`` row per priced turn
        (``turn_index > 0``), ``preceding_primary`` rendered as its
        enum's ``.value`` string (or ``None``) -- downsampled to at most
        :data:`MAX_TURN_SERIES_POINTS` rows for a very long session
        (review finding 11), keeping every marked turn (see
        ``markers`` below) and evenly striding through the remainder to
        fill the rest of the budget, so the shape of the series survives
        even when most of its raw points are dropped.

        ``markers``: ``{"compactions": [...], "spawns": [...], "human":
        [...]}`` -- the turn indices whose ``preceding_primary`` is
        ``compact_boundary``, whose ``agent_brief_chars`` is set (an
        Agent/Task tool call was made from that turn), or whose
        ``human_prompt_chars`` is set (a human message preceded that
        turn), respectively. Always computed from the *full* turn list,
        never from the downsampled ``turn_series``.

        ``truncated``: ``True`` when ``turn_series`` was downsampled --
        the UI uses this to say so rather than silently showing a
        thinned-out chart as if it were the complete picture.

        ``limit_markers`` (v3-limits wiring): every ``LIMIT_HIT``/
        ``LIMIT_RESUME``/``AGENT_TERMINATED`` event on this session's
        top-level transcript, as ``{"ts", "kind", "detail"}`` dicts --
        ``limits.limit_markers(result)``'s own ``(ts, kind, detail)``
        triples reshaped into JSON objects. Never downsampled (there are
        at most a handful of these per session, nothing like
        ``turn_series``'s volume).
        """
        row = self._connection().execute(
            "SELECT digest_blob FROM transcripts WHERE session_id = ? AND kind = 'top-level'",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            result = result_from_jsonable(json.loads(decode_digest_blob(row["digest_blob"])))
        except (KeyError, TypeError, ValueError, zlib.error):
            return None

        turn_series: list[list] = []
        compactions: list[int] = []
        spawns: list[int] = []
        human: list[int] = []
        for turn in result.turns:
            if turn.turn_index <= 0:
                continue
            primary = turn.preceding_primary.value if turn.preceding_primary is not None else None
            turn_series.append(
                [turn.turn_index, turn.ctx, turn.cache_creation_tokens, turn.is_recache, primary]
            )
            if primary == EventKind.COMPACT_BOUNDARY.value:
                compactions.append(turn.turn_index)
            if turn.agent_brief_chars is not None:
                spawns.append(turn.turn_index)
            if turn.human_prompt_chars is not None:
                human.append(turn.turn_index)

        truncated = False
        total_points = len(turn_series)
        if total_points > self.MAX_TURN_SERIES_POINTS:
            marker_turns = set(compactions) | set(spawns) | set(human)
            keep = {i for i, row_ in enumerate(turn_series) if row_[0] in marker_turns}
            budget = self.MAX_TURN_SERIES_POINTS - len(keep)
            if budget > 0:
                # Evenly spaced indices across the *full* range, computed
                # with a float step rather than an integer stride -- an
                # integer `total_points // budget` floors to 1 whenever
                # budget is more than half of total_points, which would
                # select every single index and then have the later
                # `[:MAX_TURN_SERIES_POINTS]` truncation cut off
                # everything past the cap, silently dropping any marker
                # turn that happens to sit later in the series (the bug
                # this comment replaces).
                step = total_points / budget
                for k in range(budget):
                    idx = min(int(k * step), total_points - 1)
                    keep.add(idx)
            kept_indices = sorted(keep)
            if len(kept_indices) > self.MAX_TURN_SERIES_POINTS:
                # Pathological case: marker turns alone already exceed
                # the cap. Truncate rather than silently exceed it --
                # there is no marker-preserving way to shrink further.
                kept_indices = kept_indices[: self.MAX_TURN_SERIES_POINTS]
            turn_series = [turn_series[i] for i in kept_indices]
            truncated = True

        return {
            "turn_series": turn_series,
            "markers": {"compactions": compactions, "spawns": spawns, "human": human},
            "truncated": truncated,
            "limit_markers": [
                {"ts": ts, "kind": kind, "detail": detail} for ts, kind, detail in _limit_markers(result)
            ],
        }

    def summary(
        self,
        *,
        window_days: int | None = None,
        since: str | None = None,
        until: str | None = None,
        project_slugs: list[str] | None = None,
        window_by: str = "last-reply",
    ) -> dict:
        """Corpus-wide totals: session/transcript counts and cost/token
        sums, optionally restricted to a ``window_days``/``since``/
        ``until`` window (the same three params ``sessions``/
        ``compactions`` accept). ``window_by="first-reply"`` counts the
        sessions whose first reply falls in the window instead (see
        :meth:`_session_ids_in_window`).

        The windowed branch counts exactly the sessions/transcripts a
        report over the same window would (``report.py``'s "overview"
        section, built from ``service.rebuild.corpus_from_store``/
        ``corpus.load_corpus`` with their shared ``window_by="last-reply"``
        default): a session qualifies when its last reply (the row's
        ``last_ts``) falls in the window, and every transcript belonging to
        it (top-level and every subagent) then counts, matching
        ``corpus_from_store``'s own ``total_files``.

        ``project_slugs`` (additive, project-filter work): raw
        ``sessions.slug`` values (already resolved from the client's
        redacted ``project`` query param by ``api.py``'s
        ``_project_query`` -- see ``resolve_project_slug`` below) to
        restrict to; ``None`` (the default) keeps every project, matching
        every existing caller exactly.
        """
        conn = self._connection()
        if window_days is None and since is None and until is None and project_slugs is None:
            row = conn.execute(
                "SELECT COUNT(*) AS sessions, COALESCE(SUM(total_cost), 0) AS total_cost, "
                "COALESCE(SUM(total_tokens), 0) AS total_tokens FROM sessions"
            ).fetchone()
            transcripts = conn.execute("SELECT COUNT(*) AS n FROM transcripts").fetchone()["n"]
            return {
                "window_days": None,
                "sessions": row["sessions"],
                "transcripts": transcripts,
                "total_cost": row["total_cost"],
                "total_tokens": row["total_tokens"],
            }

        since_dt, until_dt = _resolve_window(window_days, since, until)
        session_ids = self._session_ids_in_window(since_dt, until_dt, project_slugs=project_slugs, window_by=window_by)
        if not session_ids:
            return {
                "window_days": window_days,
                "sessions": 0,
                "transcripts": 0,
                "total_cost": 0.0,
                "total_tokens": 0,
            }
        placeholders = ",".join("?" * len(session_ids))
        row = conn.execute(
            f"SELECT COUNT(*) AS sessions, COALESCE(SUM(total_cost), 0) AS total_cost, "
            f"COALESCE(SUM(total_tokens), 0) AS total_tokens FROM sessions WHERE id IN ({placeholders})",
            session_ids,
        ).fetchone()
        transcripts = conn.execute(
            f"SELECT COUNT(*) AS n FROM transcripts WHERE session_id IN ({placeholders})",
            session_ids,
        ).fetchone()["n"]
        return {
            "window_days": window_days,
            "sessions": row["sessions"],
            "transcripts": transcripts,
            "total_cost": row["total_cost"],
            "total_tokens": row["total_tokens"],
        }

    def entrypoint_counts(self) -> dict[str, dict]:
        """Sessions per entrypoint (``cli``, ``claude-desktop``, ...),
        each with its count and newest ``last_ts``: where you run Claude
        Code, which decides whether a statusline can run at all."""
        rows = self._connection().execute(
            "SELECT COALESCE(entrypoint, '') AS entrypoint, COUNT(*) AS n, MAX(last_ts) AS last_ts "
            "FROM sessions GROUP BY 1"
        ).fetchall()
        return {row["entrypoint"]: {"count": row["n"], "last_ts": row["last_ts"]} for row in rows}

    def _session_ids_in_window(
        self,
        since_dt: datetime | None,
        until_dt: datetime | None,
        *,
        project_slugs: list[str] | None = None,
        window_by: str = "last-reply",
    ) -> list[str]:
        """Sessions whose last reply falls in the window: the rule
        ``service.rebuild.corpus_from_store`` and ``corpus.load_corpus``
        use to decide what a windowed report counts. With
        ``window_by="first-reply"``, sessions whose first reply does: the
        "since my last change" window, whose sessions all started on the
        new settings. ``project_slugs`` (additive), when given, further
        restricts to sessions whose raw ``slug`` is one of them."""
        column = window_column(window_by)
        rows = self._connection().execute("SELECT id, slug, first_ts, last_ts FROM sessions").fetchall()
        if project_slugs is not None:
            allowed = set(project_slugs)
            rows = [row for row in rows if row["slug"] in allowed]
        return [row["id"] for row in rows if ts_in_window(row[column], since_dt, until_dt)]

    def resolve_project_slug(self, redacted: str) -> list[str] | None:
        """The raw ``sessions.slug`` value(s) that redact
        (``discovery.redact_slug``) to ``redacted`` -- resolves a
        client-supplied project slug, which is always already redacted
        (the API never hands out a raw one -- see "Privacy" in
        ``docs/api.md``), back to what a ``project`` filter must actually
        match in SQL/``Corpus`` filtering. Two raw slugs can share one
        redacted form (each under its own ``Users-<name>`` segment), so
        every match is returned, sorted; ``None`` when ``redacted``
        matches no session's slug at all -- ``api.py``'s ``_project_query``
        treats that as an unknown project (``400 bad_request``)."""
        matches = [slug for slug in self.project_slugs() if redact_slug(slug) == redacted]
        return matches or None

    def project_slugs(self) -> list[str]:
        """Every raw ``sessions.slug`` this store has recorded a session
        for, sorted. Never handed out by the API (see
        :meth:`resolve_project_slug`): the report reads it only to fold a
        project's two snapshot keys into one (``build_report``'s
        ``known_slugs``)."""
        rows = self._connection().execute("SELECT DISTINCT slug FROM sessions WHERE slug IS NOT NULL").fetchall()
        return sorted({row["slug"] for row in rows})

    def sessions(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        window_days: int | None = None,
        since: str | None = None,
        until: str | None = None,
        project_slugs: list[str] | None = None,
        window_by: str = "last-reply",
    ) -> list[dict]:
        """The most recent ``limit`` sessions (by ``first_ts`` descending),
        one summary dict each — no transcript paths. ``source`` says
        where it ran ("This computer" or "WSL: <distro>", see
        ``discovery.source_label``). ``window_days``/``since``/``until``
        keep only the sessions a report over that window counts (last
        reply in the window, or first with ``window_by="first-reply"``).
        ``project_slugs`` (additive), when given,
        further restricts to raw slugs in that list -- see
        ``resolve_project_slug``."""
        where_sql = ""
        where_params: list = []
        if project_slugs:
            placeholders = ",".join("?" * len(project_slugs))
            where_sql = f" WHERE s.slug IN ({placeholders})"
            where_params = list(project_slugs)
        query = f"""
            SELECT s.id, s.slug, s.first_ts, s.last_ts, s.span_s, s.archetype,
                   s.mode, s.mode_source, s.purpose, s.purpose_source, s.entrypoint,
                   s.billing_mode, s.profile_id, s.total_cost, s.total_tokens,
                   (SELECT t.path FROM transcripts t WHERE t.session_id = s.id LIMIT 1) AS source_path
            FROM sessions s
            {where_sql}
            ORDER BY s.first_ts DESC
            """
        since_dt, until_dt = _resolve_window(window_days, since, until)
        if since_dt is None and until_dt is None:
            rows = self._connection().execute(
                query + " LIMIT ? OFFSET ?", (*where_params, limit, offset)
            ).fetchall()
        else:
            rows = [
                row
                for row in self._connection().execute(query, where_params).fetchall()
                if ts_in_window(row[window_column(window_by)], since_dt, until_dt)
            ][offset : offset + limit]
        result = [dict(row) for row in rows]
        for item in result:
            item["slug"] = redact_slug(item["slug"])
            item["source"] = source_label(item.pop("source_path"))
        return result

    def session(self, session_id: str) -> dict | None:
        """One session's full detail: its own summary fields plus its
        transcripts (kind/agent_type/spawn_depth only — no ``path``) and
        any tags. ``None`` if ``session_id`` is unknown."""
        conn = self._connection()
        row = conn.execute(
            """
            SELECT id, slug, first_ts, last_ts, span_s, archetype, mode,
                   mode_source, purpose, purpose_source, entrypoint,
                   billing_mode, profile_id, total_cost, total_tokens
            FROM sessions WHERE id = ?
            """,
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["slug"] = redact_slug(result["slug"])
        source_row = conn.execute(
            "SELECT path FROM transcripts WHERE session_id = ? LIMIT 1", (session_id,)
        ).fetchone()
        result["source"] = source_label(source_row["path"] if source_row else None)
        transcript_rows = conn.execute(
            "SELECT id, kind, agent_id, agent_type, spawn_depth, parent_agent_id "
            "FROM transcripts WHERE session_id = ?",
            (session_id,),
        ).fetchall()
        result["transcripts"] = [dict(trow) for trow in transcript_rows]
        result["tags"] = self.tags(session_id)
        result["feedback"] = self.feedback(session_id)
        return result

    def daily_usage(
        self,
        *,
        days: int | None = 30,
        since: str | None = None,
        until: str | None = None,
        split: str | None = None,
        project_slugs: list[str] | None = None,
        window_by: str = "last-reply",
        tz: str | tzinfo | None = "UTC",
    ) -> list[dict]:
        """Per-day, per-model token/cost rollups, joined from
        ``turns_agg`` (no per-transcript or path detail).

        ``tz`` is the zone a day is counted in: an IANA name, a
        ``tzinfo``, or ``None`` for the machine's own zone (a name that
        can't be resolved, such as one on a machine with no ``tzdata``,
        falls back to the machine's zone too). The default, ``"UTC"``,
        keeps a direct caller on UTC days; the dashboard passes
        ``config.tz``. The store knows no zone, so it adds up the
        quarter-hour ``bucket`` rows into local days here, on the way out
        (:func:`_by_local_day`). A row with no bucket (see
        ``schema.py``'s "Version 8") keeps its stored UTC day.

        A window reads whole quarter-hour buckets (see
        :func:`_turns_agg_window`): from the one holding ``since`` up to,
        not including, the one starting at ``until``. A window that starts
        at a local midnight, as a calendar window does, is exact. A turn
        with no timestamp is only in a window with no bound at all.

        ``window_by="first-reply"`` keeps only the replies of the
        sessions whose first reply falls in the window (see
        :meth:`_session_ids_in_window`), so the days add up to
        :meth:`summary`'s total for the same window rather than taking in
        every session on the window's first day.

        ``days`` keeps its original meaning for existing callers -- a
        rolling window back from now -- but ``since``/``until`` (ISO 8601)
        take precedence when given, the same ``_resolve_window``
        precedence ``summary``/``sessions``/``compactions`` already use
        (the dashboard resolves its calendar days to a ``since`` before it
        gets here); pass ``days=None`` for no lower bound at all (paired
        with ``since``/``until`` already resolving to "no window", as
        ``route_daily_usage`` does for ``?window=all``).

        ``split="agent"`` additionally breaks each day/model row into the
        main session and every subagent (``transcripts.kind`` joined in
        from ``turns_agg.transcript_id`` -- ``"top-level"`` is
        ``"main"``, ``"subagent"``/``"workflow-agent"`` are
        ``"subagent"``), adding an ``"agent"`` key. ``split="model"`` or
        omitted keeps the original, unsplit shape -- the default, so
        existing callers see no change.

        ``project_slugs`` (additive), when given, further restricts to
        raw slugs in that list -- see ``resolve_project_slug``. Reaching
        a project from ``turns_agg`` needs two joins it otherwise
        skips (``turns_agg.transcript_id -> transcripts.session_id ->
        sessions.slug``), added only when filtering is requested so an
        unfiltered call plans identically to before.
        """
        since_dt, until_dt = _resolve_window(days, since, until)
        zone = _zone(tz)
        conditions = []
        params: list = []
        if since_dt is not None or until_dt is not None:
            window_sql, window_params = _turns_agg_window(since_dt, until_dt, zone)
            conditions.append(window_sql)
            params.extend(window_params)
        if project_slugs:
            placeholders = ",".join("?" * len(project_slugs))
            conditions.append(f"s2.slug IN ({placeholders})")
            params.extend(project_slugs)
        if window_by != "last-reply" and (since_dt is not None or until_dt is not None):
            session_ids = self._session_ids_in_window(since_dt, until_dt, project_slugs=project_slugs, window_by=window_by)
            if not session_ids:
                return []
            placeholders = ",".join("?" * len(session_ids))
            conditions.append(f"a.transcript_id IN (SELECT id FROM transcripts WHERE session_id IN ({placeholders}))")
            params.extend(session_ids)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        if split == "agent":
            project_join = " JOIN sessions s2 ON s2.id = t.session_id" if project_slugs else ""
            rows = self._connection().execute(
                f"""
                SELECT a.bucket AS bucket, a.day AS day,
                       CASE WHEN t.kind = 'top-level' THEN 'main' ELSE 'subagent' END AS agent,
                       a.model AS model,
                       SUM(a.turns) AS turns,
                       SUM(a.input_tokens) AS input_tokens,
                       SUM(a.cache_creation_tokens) AS cache_creation_tokens,
                       SUM(a.cache_read_tokens) AS cache_read_tokens,
                       SUM(a.output_tokens) AS output_tokens,
                       SUM(a.thinking_tokens) AS thinking_tokens,
                       SUM(a.cc_5m) AS cc_5m,
                       SUM(a.cc_1h) AS cc_1h,
                       SUM(a.cost) AS cost
                FROM turns_agg a JOIN transcripts t ON t.id = a.transcript_id{project_join}
                {where}
                GROUP BY a.bucket, a.day, agent, a.model
                ORDER BY a.bucket, a.day, agent, a.model
                """,
                params,
            ).fetchall()
            return _by_local_day(rows, zone, split_agent=True)
        project_join = " JOIN transcripts t ON t.id = a.transcript_id JOIN sessions s2 ON s2.id = t.session_id" if project_slugs else ""
        rows = self._connection().execute(
            f"""
            SELECT a.bucket AS bucket, a.day AS day, a.model AS model,
                   SUM(a.turns) AS turns,
                   SUM(a.input_tokens) AS input_tokens,
                   SUM(a.cache_creation_tokens) AS cache_creation_tokens,
                   SUM(a.cache_read_tokens) AS cache_read_tokens,
                   SUM(a.output_tokens) AS output_tokens,
                   SUM(a.thinking_tokens) AS thinking_tokens,
                   SUM(a.cc_5m) AS cc_5m,
                   SUM(a.cc_1h) AS cc_1h,
                   SUM(a.cost) AS cost
            FROM turns_agg a{project_join}
            {where}
            GROUP BY a.bucket, a.day, a.model
            ORDER BY a.bucket, a.day, a.model
            """,
            params,
        ).fetchall()
        return _by_local_day(rows, zone, split_agent=False)

    def cache_read_tokens_by_model(
        self,
        *,
        days: int | None = 30,
        since: str | None = None,
        until: str | None = None,
        project_slugs: list[str] | None = None,
        window_by: str = "last-reply",
    ) -> dict[str, int]:
        """``cache_read_tokens`` summed per model over the sessions a
        window counts, for ``/api/summary``'s additive ``cache_saved``
        figure. It uses the same rule as :meth:`summary`'s ``sessions``
        and ``total_cost``: a session counts when its last reply falls in
        the window, and then every turn of it (main and subagents) does.
        Day buckets would pull in other sessions' reads whenever a bound
        falls mid-day (a 1-hour window, or "the same hours yesterday").
        With no window and no project, every turn counts.
        ``project_slugs`` (additive) and ``window_by``: see
        :meth:`summary`'s own parameters of the same names."""
        since_dt, until_dt = _resolve_window(days, since, until)
        conn = self._connection()
        if since_dt is None and until_dt is None and project_slugs is None:
            rows = conn.execute(
                "SELECT model, COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens FROM turns_agg GROUP BY model"
            ).fetchall()
            return {row["model"]: row["cache_read_tokens"] for row in rows}
        session_ids = self._session_ids_in_window(since_dt, until_dt, project_slugs=project_slugs, window_by=window_by)
        if not session_ids:
            return {}
        placeholders = ",".join("?" * len(session_ids))
        rows = conn.execute(
            f"SELECT ta.model AS model, COALESCE(SUM(ta.cache_read_tokens), 0) AS cache_read_tokens "
            f"FROM turns_agg ta JOIN transcripts t ON t.id = ta.transcript_id "
            f"WHERE t.session_id IN ({placeholders}) GROUP BY ta.model",
            session_ids,
        ).fetchall()
        return {row["model"]: row["cache_read_tokens"] for row in rows}

    def recache(self) -> dict:
        """Aggregate RE-CACHE turn counts by signature, corpus-wide."""
        rows = self._connection().execute(
            """
            SELECT signature, COUNT(*) AS turns, COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens
            FROM recache_turns
            GROUP BY signature
            """
        ).fetchall()
        by_signature = {row["signature"]: {"turns": row["turns"], "cache_creation_tokens": row["cache_creation_tokens"]} for row in rows}
        return {"by_signature": by_signature}

    def compactions(
        self,
        *,
        window_days: int | None = None,
        since: str | None = None,
        until: str | None = None,
        project_slugs: list[str] | None = None,
        window_by: str = "last-reply",
    ) -> list[dict]:
        """Every recorded compaction event (no transcript path — only
        the opaque, store-local ``transcript_id``), oldest first. With a
        window, every compaction of the sessions it counts, the same
        sessions :meth:`summary` counts (a session whose last reply falls
        in it, or whose first does with ``window_by="first-reply"``; see
        :meth:`_session_ids_in_window`), whenever in the session it
        happened: so the list is as long as the report's own count, and a
        session that began before the window still lists all its
        compactions. ``project_slugs`` (additive), when given, further
        restricts to raw slugs in that list -- see
        ``resolve_project_slug``; reaching a project needs a join
        ``compactions`` otherwise skips (``transcripts.session_id ->
        sessions.slug``), added only when filtering is requested."""
        since_dt, until_dt = _resolve_window(window_days, since, until)
        counted: set | None = None
        if since_dt is not None or until_dt is not None:
            counted = set(
                self._session_ids_in_window(since_dt, until_dt, project_slugs=project_slugs, window_by=window_by)
            )
            if not counted:
                return []
        if project_slugs:
            placeholders = ",".join("?" * len(project_slugs))
            rows = self._connection().execute(
                f"""
                SELECT c.transcript_id AS transcript_id, c.ts AS ts, c.pre_tokens AS pre_tokens,
                       c.post_tokens AS post_tokens, c.dropped_tokens AS dropped_tokens,
                       c.trigger AS trigger, c.join_delta_s AS join_delta_s,
                       t.session_id AS session_id
                FROM compactions c
                JOIN transcripts t ON t.id = c.transcript_id
                JOIN sessions s ON s.id = t.session_id
                WHERE s.slug IN ({placeholders})
                ORDER BY c.ts
                """,
                list(project_slugs),
            ).fetchall()
        else:
            rows = self._connection().execute(
                """
                SELECT c.transcript_id AS transcript_id, c.ts AS ts, c.pre_tokens AS pre_tokens,
                       c.post_tokens AS post_tokens, c.dropped_tokens AS dropped_tokens,
                       c.trigger AS trigger, c.join_delta_s AS join_delta_s,
                       t.session_id AS session_id
                FROM compactions c
                LEFT JOIN transcripts t ON t.id = c.transcript_id
                ORDER BY c.ts
                """
            ).fetchall()
        listed = []
        for row in rows:
            item = dict(row)
            session_id = item.pop("session_id")
            if counted is None or session_id in counted:
                listed.append(item)
        return listed

    def snapshots(self) -> list[dict]:
        """Every captured config snapshot's identity and digest (already
        flattened/redacted before storage — see ``schema.py``), plus its
        owning project's ``slug`` as ``project_slug``. A snapshot
        attributed to :data:`GLOBAL_PROJECT_SLUG` (the watcher's
        synthetic attribution for a machine-wide, not-per-project
        snapshot file — see module docstring) reports ``project_slug`` as
        ``None`` instead of that internal sentinel, so a caller sees an
        honest "no project" rather than a fabricated one (S1-integration
        fix 1.c)."""
        rows = self._connection().execute(
            """
            SELECT sn.id, sn.project_id, p.slug AS project_slug, sn.ts,
                   sn.schema_version, sn.digest_json
            FROM snapshots sn
            LEFT JOIN projects p ON p.id = sn.project_id
            ORDER BY sn.ts
            """
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            if item.get("project_slug") == GLOBAL_PROJECT_SLUG:
                item["project_slug"] = None
            elif item.get("project_slug") is not None:
                item["project_slug"] = redact_slug(item["project_slug"])
            result.append(item)
        return result

    def profiles(self) -> list[dict]:
        """Every indexed profile's id/name (no ``toml_path`` — local
        filesystem location, never API-returned)."""
        rows = self._connection().execute(
            "SELECT id, name, updated_at FROM profiles ORDER BY name"
        ).fetchall()
        return [dict(row) for row in rows]

    def baselines(self) -> list[dict]:
        """Every recorded baseline capture, plus its owning project's
        (redacted) ``slug`` -- joined in (nit 27) so a caller can label a
        baseline row by project name without a second round trip through
        ``sessions()``/a raw ``project_id``."""
        rows = self._connection().execute(
            """
            SELECT b.id, b.project_id, p.slug AS project_slug, b.window_start,
                   b.window_end, b.archetype, b.digest_json, b.created_at
            FROM baselines b
            LEFT JOIN projects p ON p.id = b.project_id
            ORDER BY b.created_at
            """
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            if item.get("project_slug") is not None:
                item["project_slug"] = redact_slug(item["project_slug"])
            result.append(item)
        return result

    def tags(self, session_id: str) -> dict:
        """``{key: value}`` of every tag set on ``session_id`` (empty
        dict if none)."""
        rows = self._connection().execute(
            "SELECT key, value FROM session_tags WHERE session_id = ?", (session_id,)
        ).fetchall()
        return {row["key"]: row["value"] for row in rows}

    def all_tags(self) -> dict[str, dict[str, str]]:
        """Every session's tags, grouped by ``session_id`` -- the
        whole-store counterpart to :meth:`tags` (one session at a time).
        Used by ``api.py``'s report building to merge ``POST
        /api/sessions/<id>/tags`` writes into the same
        ``session_overrides`` mechanism ``config.load_session_overrides``
        feeds ``classify.classify_session`` (review finding 7: a tag
        write must actually change the built report, not just sit in the
        store inertly)."""
        rows = self._connection().execute("SELECT session_id, key, value FROM session_tags").fetchall()
        result: dict[str, dict[str, str]] = {}
        for row in rows:
            result.setdefault(row["session_id"], {})[row["key"]] = row["value"]
        return result

    def set_tag(self, session_id: str, key: str, value: str) -> None:
        """Set (or overwrite) one ``session_tags`` entry — the only
        mutation the v0.2 API exposes (``POST /api/sessions/<id>/tags``,
        per ``docs/api.md``)."""
        conn = self._connection()
        # Single statement -- see upsert_profile's comment above.
        conn.execute(
            "INSERT INTO session_tags (session_id, key, value, set_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(session_id, key) DO UPDATE SET value = excluded.value, set_at = excluded.set_at",
            (session_id, key, value, _now()),
        )

    @staticmethod
    def _feedback_row(row, builds=()) -> dict:
        """One rating as the API shows it. ``row`` may come from a store
        that predates version 9, which has none of the newer columns: they
        read as empty. ``builds`` are the plan-build rows
        (:meth:`_build_rows`); ``plan`` and ``handoff`` repeat the first
        build's answers, so a session with one plan reads as it always did."""
        held = row.keys()
        first = next((b for b in builds if b["build"] == 1), None)
        return {
            "outcome": row["outcome"],
            "slow": [w for w in row["slow"].split(",") if w],
            "worth": row["worth"],
            "helped": [w for w in row["helped"].split(",") if w],
            "why": [w for w in row["why"].split(",") if w] if "why" in held else [],
            "missed_in": row["missed_in"] if "missed_in" in held else None,
            "plan": first["plan"] if first else None,
            "handoff": first["handoff"] if first else None,
            "tip": row["tip"] if "tip" in held else None,
            "tip_hint": row["tip_hint"] if "tip_hint" in held else None,
            "builds": [dict(b) for b in builds],
            "set_at": row["set_at"],
        }

    @staticmethod
    def _build_rows(rows) -> dict[str, list[dict]]:
        """``session_plan_feedback`` rows as ``{session_id: [{build, plan,
        handoff}, ...]}``, in build order."""
        out: dict[str, list[dict]] = {}
        for row in sorted(rows, key=lambda r: (r["session_id"], r["build"])):
            out.setdefault(row["session_id"], []).append(
                {"build": row["build"], "plan": row["plan"], "handoff": row["handoff"]}
            )
        return out

    def feedback(self, session_id: str) -> dict | None:
        """Your rating of ``session_id`` from the Sessions tab
        (``{outcome, slow, worth, helped, why, missed_in, plan, handoff,
        tip, tip_hint, builds, set_at}``), or ``None``."""
        conn = self._connection()
        row = conn.execute("SELECT * FROM session_feedback WHERE session_id = ?", (session_id,)).fetchone()
        if row is None:
            return None
        plans = conn.execute(
            "SELECT session_id, build, plan, handoff FROM session_plan_feedback WHERE session_id = ?", (session_id,)
        ).fetchall()
        return self._feedback_row(row, self._build_rows(plans).get(session_id, ()))

    def all_feedback(self) -> dict[str, dict]:
        """Every rating, by ``session_id``."""
        conn = self._connection()
        rows = conn.execute("SELECT * FROM session_feedback").fetchall()
        builds = self._build_rows(
            conn.execute("SELECT session_id, build, plan, handoff FROM session_plan_feedback").fetchall()
        )
        return {row["session_id"]: self._feedback_row(row, builds.get(row["session_id"], ())) for row in rows}

    def feedback_count(self) -> int:
        return self._connection().execute("SELECT COUNT(*) FROM session_feedback").fetchone()[0]

    def unrated_sessions(self, *, min_tokens: int, since: str) -> list[dict]:
        """Sessions with a last reply at or after ``since`` that used at
        least ``min_tokens`` (their stored total, subagents included, so
        an upper bound on the main transcript's own) and that you have not
        rated on the dashboard, newest first: ``{id, slug, last_ts,
        total_tokens, stamp}``, the slug redacted. ``stamp`` is the latest
        parse of its transcripts, so a caller can tell which sessions
        changed. The banner's candidates (``ratings.unrated_piece`` has the
        last word)."""
        since_dt, _until = _resolve_window(None, since, None)
        rows = self._connection().execute(
            "SELECT s.id, s.slug, s.last_ts, s.total_tokens, "
            "(SELECT MAX(t.updated_at) FROM transcripts t WHERE t.session_id = s.id) AS stamp FROM sessions s "
            "WHERE s.total_tokens >= ? "
            "AND NOT EXISTS (SELECT 1 FROM session_feedback f WHERE f.session_id = s.id) "
            "ORDER BY s.last_ts DESC",
            (min_tokens,),
        ).fetchall()
        return [
            {**dict(row), "slug": redact_slug(row["slug"])}
            for row in rows
            if ts_in_window(row["last_ts"], since_dt, None)
        ]

    def set_feedback(
        self,
        session_id: str,
        *,
        outcome: str | None = None,
        slow=(),
        worth: str | None = None,
        helped=(),
        why=(),
        missed_in: str | None = None,
        plan: str | None = None,
        handoff: str | None = None,
        tip: str | None = None,
        tip_hint: str | None = None,
        builds=(),
    ) -> None:
        """Set (or replace) your rating of ``session_id``; a rating with
        nothing ticked clears it, plan builds and all. ``builds`` is a list
        of ``{"build": n, "plan": word, "handoff": word}`` (``n`` from 1);
        ``plan`` and ``handoff`` given on their own are build 1's, unless
        ``builds`` names it. A tip answer keeps its ``tip_hint`` only with
        the answer. The caller checks the words (``api.py``'s
        ``route_set_feedback``)."""
        conn = self._connection()
        answers: dict[int, tuple[str | None, str | None]] = {}
        if plan or handoff:
            answers[1] = (plan, handoff)
        for item in builds:
            if item.get("plan") or item.get("handoff"):
                answers[int(item["build"])] = (item.get("plan"), item.get("handoff"))
        ticked = outcome or slow or worth or helped or why or missed_in or tip or answers
        now = _now()
        with _transaction(conn):
            conn.execute("DELETE FROM session_plan_feedback WHERE session_id = ?", (session_id,))
            if not ticked:
                conn.execute("DELETE FROM session_feedback WHERE session_id = ?", (session_id,))
                return
            conn.execute(
                "INSERT INTO session_feedback (session_id, outcome, slow, worth, helped, set_at, why, missed_in, "
                "tip, tip_hint) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
                "outcome = excluded.outcome, slow = excluded.slow, worth = excluded.worth, "
                "helped = excluded.helped, set_at = excluded.set_at, why = excluded.why, "
                "missed_in = excluded.missed_in, tip = excluded.tip, tip_hint = excluded.tip_hint",
                (
                    session_id, outcome, ",".join(slow), worth, ",".join(helped), now, ",".join(why), missed_in,
                    tip, tip_hint if tip else None,
                ),
            )
            conn.executemany(
                "INSERT INTO session_plan_feedback (session_id, build, plan, handoff, set_at) "
                "VALUES (?, ?, ?, ?, ?)",
                [(session_id, build, words[0], words[1], now) for build, words in sorted(answers.items())],
            )

    # -- what you said about a tip, habit or recommendation card (v9) ----

    def tip_feedback(self) -> dict[tuple[str, str], dict]:
        """Every card answer, by ``(kind, item)``: ``{answer, set_at}``."""
        rows = self._connection().execute("SELECT kind, item, answer, set_at FROM tip_feedback").fetchall()
        return {(row["kind"], row["item"]): {"answer": row["answer"], "set_at": row["set_at"]} for row in rows}

    def tip_feedback_count(self) -> int:
        return self._connection().execute("SELECT COUNT(*) FROM tip_feedback").fetchone()[0]

    def set_tip_feedback(self, kind: str, item: str, answer: str | None) -> dict | None:
        """Set (or replace) what you said about one card; ``None`` takes
        the answer back. Returns the row as saved, or ``None`` when there
        is none. The caller checks the words (``api.py``'s
        ``route_set_tip_feedback``)."""
        conn = self._connection()
        if answer is None:
            conn.execute("DELETE FROM tip_feedback WHERE kind = ? AND item = ?", (kind, item))
            return None
        stamp = _now()
        # Single statement -- see upsert_profile's comment above.
        conn.execute(
            "INSERT INTO tip_feedback (kind, item, answer, set_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(kind, item) DO UPDATE SET answer = excluded.answer, set_at = excluded.set_at",
            (kind, item, answer, stamp),
        )
        return {"answer": answer, "set_at": stamp}

    # -- EST-P5: predictions and back-testing ---------------------------

    def upsert_prediction(
        self, *, prediction_id: str, ts: str, source: str, measure_key: str, agent: str | None,
        predicted_usd: float | None, predicted_pct: float | None, fidelity: str,
    ) -> bool:
        """Ingest one ``prediction-log.jsonl`` record
        (``watcher._scan_predictions``). A prediction is immutable once
        logged, so this is ``INSERT OR IGNORE`` keyed on ``prediction_id``
        -- a repeat tick over an already-ingested line is a no-op.
        Returns whether a new row was actually inserted."""
        conn = self._connection()
        cursor = conn.execute(
            "INSERT OR IGNORE INTO predictions "
            "(id, ts, source, measure_key, agent, predicted_usd, predicted_pct, fidelity, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (prediction_id, ts, source, measure_key, agent, predicted_usd, predicted_pct, fidelity, _now()),
        )
        return cursor.rowcount > 0

    def mark_prediction_seen(self, prediction_id: str) -> bool:
        """``POST /api/predictions/seen``: record that the dashboard has
        shown you this prediction. Returns whether a row matched (and
        hadn't already been marked seen)."""
        conn = self._connection()
        cursor = conn.execute(
            "UPDATE predictions SET seen_at = ? WHERE id = ? AND seen_at IS NULL", (_now(), prediction_id)
        )
        return cursor.rowcount > 0

    def judge_prediction(
        self, prediction_id: str, *, change_ts: str, verdict: str,
        measured_usd: float | None, measured_pct: float | None,
    ) -> None:
        """Persist ``backtest.py``'s verdict for one prediction, so a
        later call (or EST-P6's calibration) doesn't rework out the same
        match from scratch -- the same before-computed-then-reused
        posture ``impact_cache``/``route_impact`` already give the
        impact comparison (``service/api.py``)."""
        conn = self._connection()
        conn.execute(
            "UPDATE predictions SET change_ts = ?, judged_at = ?, verdict = ?, measured_usd = ?, "
            "measured_pct = ? WHERE id = ?",
            (change_ts, _now(), verdict, measured_usd, measured_pct, prediction_id),
        )

    def reopen_predictions(self, verdict: str) -> int:
        """Put every prediction judged ``verdict`` back to unjudged, so
        ``backtest.py`` matches and judges it again from scratch. Returns
        the number of predictions reopened."""
        cursor = self._connection().execute(
            "UPDATE predictions SET change_ts = NULL, judged_at = NULL, verdict = NULL, "
            "measured_usd = NULL, measured_pct = NULL WHERE verdict = ?",
            (verdict,),
        )
        return cursor.rowcount

    @staticmethod
    def _prediction_row(row) -> dict:
        return {
            "id": row["id"],
            "ts": row["ts"],
            "source": row["source"],
            "measure_key": row["measure_key"],
            "agent": row["agent"],
            "predicted_usd": row["predicted_usd"],
            "predicted_pct": row["predicted_pct"],
            "fidelity": row["fidelity"],
            "seen_at": row["seen_at"],
            "change_ts": row["change_ts"],
            "judged_at": row["judged_at"],
            "verdict": row["verdict"],
            "measured_usd": row["measured_usd"],
            "measured_pct": row["measured_pct"],
        }

    def predictions(self, *, judged: bool | None = None) -> list[dict]:
        """Every prediction, newest first. ``judged=True``/``False``
        filters to only-judged/only-unjudged predictions; ``None`` (the
        default) returns all of them."""
        query = "SELECT * FROM predictions"
        if judged is True:
            query += " WHERE judged_at IS NOT NULL"
        elif judged is False:
            query += " WHERE judged_at IS NULL"
        query += " ORDER BY ts DESC"
        rows = self._connection().execute(query).fetchall()
        return [self._prediction_row(row) for row in rows]

    def prune_predictions(self, *, now: str | None = None) -> int:
        """Delete a still-unjudged prediction older than
        :data:`PREDICTIONS_UNSEEN_EXPIRY_DAYS` and a judged one older
        than :data:`PREDICTIONS_JUDGED_EXPIRY_DAYS` (EST-P5's 90/400 day
        windows). Returns the number of rows removed."""
        try:
            now_dt = datetime.strptime(now or _now(), "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            return 0
        unseen_cutoff = (now_dt - timedelta(days=PREDICTIONS_UNSEEN_EXPIRY_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        judged_cutoff = (now_dt - timedelta(days=PREDICTIONS_JUDGED_EXPIRY_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn = self._connection()
        with _transaction(conn):
            cursor = conn.execute(
                "DELETE FROM predictions WHERE (judged_at IS NULL AND ts < ?) OR "
                "(judged_at IS NOT NULL AND judged_at < ?)",
                (unseen_cutoff, judged_cutoff),
            )
            return cursor.rowcount


def read_session_marks(path: str | Path) -> tuple[dict[str, dict[str, str]], dict[str, dict]]:
    """``(tags, ratings)`` set on the dashboard's Sessions tab, read from
    the store at ``path`` without writing to it, for the CLI's own
    reports: the same shapes as :meth:`Store.all_tags` and
    :meth:`Store.all_feedback`. Both empty when there is no store, it
    predates a table, or it can't be read (a lock held too long). A store
    from before version 9 gives ratings without the newer answers."""
    path = Path(path)
    if not path.is_file():
        return {}, {}
    tags: dict[str, dict[str, str]] = {}
    ratings: dict[str, dict] = {}
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return {}, {}
    conn.row_factory = sqlite3.Row
    try:
        with contextlib.closing(conn):
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            if "session_tags" in tables:
                for row in conn.execute("SELECT session_id, key, value FROM session_tags"):
                    tags.setdefault(row["session_id"], {})[row["key"]] = row["value"]
            if "session_feedback" in tables:
                builds: dict[str, list[dict]] = {}
                if "session_plan_feedback" in tables:
                    builds = Store._build_rows(
                        conn.execute("SELECT session_id, build, plan, handoff FROM session_plan_feedback").fetchall()
                    )
                for row in conn.execute("SELECT * FROM session_feedback"):
                    ratings[row["session_id"]] = Store._feedback_row(row, builds.get(row["session_id"], ()))
    except sqlite3.Error:
        return {}, {}
    return tags, ratings


def read_tip_feedback(path: str | Path) -> dict[tuple[str, str], dict]:
    """:meth:`Store.tip_feedback`, read from the store at ``path`` without
    writing to it, for the CLI's own reports. Empty when there is no
    store, it predates the table, or it can't be read."""
    path = Path(path)
    if not path.is_file():
        return {}
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return {}
    conn.row_factory = sqlite3.Row
    try:
        with contextlib.closing(conn):
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            if "tip_feedback" not in tables:
                return {}
            return {
                (row["kind"], row["item"]): {"answer": row["answer"], "set_at": row["set_at"]}
                for row in conn.execute("SELECT kind, item, answer, set_at FROM tip_feedback")
            }
    except sqlite3.Error:
        return {}


def read_predictions(path: str | Path) -> list[dict]:
    """Every row in the ``predictions`` table, newest first, read from
    the store at ``path`` without writing to it (EST-P5/P4) -- the
    ``backtest`` CLI command's own read-only counterpart to
    :func:`read_session_marks`, since the CLI never opens a writable
    connection to the dashboard's own store. Empty when there is no
    store, it predates the table, or it can't be read (a lock held too
    long)."""
    path = Path(path)
    if not path.is_file():
        return []
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return []
    conn.row_factory = sqlite3.Row
    rows: list[dict] = []
    try:
        with contextlib.closing(conn):
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            if "predictions" in tables:
                for row in conn.execute("SELECT * FROM predictions ORDER BY ts DESC"):
                    rows.append(Store._prediction_row(row))
    except sqlite3.Error:
        return []
    return rows


def read_entrypoint_counts(path: str | Path) -> dict[str, dict]:
    """:meth:`Store.entrypoint_counts`, read from the store at ``path``
    without writing to it, for ``claudeglass status``. Empty when
    there is no store or it can't be read (a lock held too long)."""
    path = Path(path)
    if not path.is_file():
        return {}
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return {}
    conn.row_factory = sqlite3.Row
    try:
        with contextlib.closing(conn):
            rows = conn.execute(
                "SELECT COALESCE(entrypoint, '') AS entrypoint, COUNT(*) AS n, MAX(last_ts) AS last_ts "
                "FROM sessions GROUP BY 1"
            ).fetchall()
    except sqlite3.Error:
        return {}
    return {row["entrypoint"]: {"count": row["n"], "last_ts": row["last_ts"]} for row in rows}


__all__ = [
    "Store",
    "encode_digest_blob",
    "decode_digest_blob",
    "read_session_marks",
    "read_tip_feedback",
    "read_predictions",
    "read_entrypoint_counts",
]

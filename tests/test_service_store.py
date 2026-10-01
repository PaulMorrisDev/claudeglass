"""Round-trip and privacy tests for ``service.store.Store`` against a
synthetic corpus (two sessions, a top-level + a subagent transcript
each, turns_agg/recache_turns/compactions/events rows, a snapshot, a
profile and a baseline).

The path-leak guard (``test_no_local_path_leaks_from_any_read_query``)
is the sharpest test in this file: it upserts a transcript whose
``path`` is a deliberately distinctive, real-looking Windows path, then
walks the JSON-serialised output of every read query and asserts that
exact string never appears anywhere in it — the concrete regression
``service/schema.py``'s and ``service/__init__.py``'s privacy-rule
docstrings warn against.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from claudeglass.service.store import Store
from helpers import assert_privacy

#: A deliberately distinctive fake local path -- if this string (or the
#: username segment alone) ever surfaces in a read-query result, the
#: store has leaked a local filesystem path into API-facing data.
_FAKE_PATH = r"C:\Users\definitely-not-a-real-person\.claude\projects\proj-a\session-a.jsonl"
_FAKE_SUB_PATH = r"C:\Users\definitely-not-a-real-person\.claude\projects\proj-a\session-a\subagents\agent-1.jsonl"
_FAKE_ROOT = r"C:\Users\definitely-not-a-real-person\.claude\projects\proj-a"
_FAKE_PROFILE_PATH = r"C:\Users\definitely-not-a-real-person\.claude\claudeglass\profiles\p1.toml"


@pytest.fixture
def store() -> Store:
    s = Store(":memory:")
    s.open()
    return s


def _seed(store: Store) -> None:
    snapshot_id = store.upsert_snapshot(
        project_slug="proj-a",
        project_root_path=_FAKE_ROOT,
        ts="2026-09-18T12:00:00Z",
        schema_version=2,
        digest_json=json.dumps({"agents": {"claude-implementer": True}}),
    )
    store.upsert_session(
        session_id="session-a",
        project_slug="proj-a",
        project_root_path=_FAKE_ROOT,
        slug="proj-a",
        first_ts="2026-09-18T12:00:00Z",
        last_ts="2026-09-18T13:00:00Z",
        span_s=3600.0,
        archetype="plan-high-implement-low",
        mode="agentic",
        mode_source="tool-signature",
        purpose="refactor",
        purpose_source="intent-signature",
        entrypoint="cli",
        billing_mode="subscription",
        snapshot_id=snapshot_id,
        profile_id="p1",
        total_cost=1.23,
        total_tokens=45000,
    )
    store.upsert_transcript(
        session_id="session-a",
        path=_FAKE_PATH,
        kind="top-level",
        agent_id=None,
        agent_type=None,
        spawn_depth=0,
        parent_agent_id=None,
        mtime_ns=123,
        size_bytes=456,
        parser_version=3,
        digest_json=json.dumps({"turns": 10}),
        turns_agg=[
            {
                "day": "2026-09-18",
                "model": "claude-sonnet-5",
                "turns": 10,
                "input_tokens": 1000,
                "cache_creation_tokens": 500,
                "cache_read_tokens": 2000,
                "output_tokens": 300,
                "thinking_tokens": 50,
                "cc_5m": 0,
                "cc_1h": 500,
                "cost": 1.0,
            }
        ],
        recache_turns=[
            {
                "turn_index": 3,
                "signature": "full-expiry",
                "cache_creation_tokens": 500,
                "preceding_primary": "HUMAN_TEXT",
                "gap_s": 400.0,
            }
        ],
        events=[{"kind": "compact_boundary", "subkind": None, "count": 1, "dropped_tokens_sum": 0, "duration_ms_sum": 0}],
        compactions=[
            {
                "ts": "2026-09-18T12:30:00Z",
                "pre_tokens": 180000,
                "post_tokens": 40000,
                "dropped_tokens": 140000,
                "trigger": "auto",
                "join_delta_s": 5.0,
            }
        ],
    )
    store.upsert_transcript(
        session_id="session-a",
        path=_FAKE_SUB_PATH,
        kind="subagent",
        agent_id="agent-1",
        agent_type="claude-implementer",
        spawn_depth=1,
        parent_agent_id=None,
        mtime_ns=789,
        size_bytes=1011,
        parser_version=3,
        digest_json=json.dumps({"turns": 5}),
        turns_agg=[
            {
                "day": "2026-09-18",
                "model": "claude-sonnet-5",
                "turns": 5,
                "input_tokens": 200,
                "cache_creation_tokens": 100,
                "cache_read_tokens": 400,
                "output_tokens": 60,
                "thinking_tokens": 0,
                "cc_5m": 100,
                "cc_1h": 0,
                "cost": 0.23,
            }
        ],
    )
    store.upsert_profile(profile_id="p1", name="implementation-heavy", toml_path=_FAKE_PROFILE_PATH)
    store.record_baseline(
        project_slug="proj-a",
        project_root_path=_FAKE_ROOT,
        window_start="2026-09-11T00:00:00Z",
        window_end="2026-09-18T00:00:00Z",
        archetype="plan-high-implement-low",
        digest_json=json.dumps({"sessions": 12}),
    )
    store.set_tag("session-a", "purpose", "refactor-override")


def _seed_second_project(store: Store) -> None:
    """A second project's session (``proj-b``), alongside ``_seed``'s own
    ``proj-a`` -- two distinct ``sessions.slug`` values for the
    project-filter tests below to filter between."""
    store.upsert_session(
        session_id="session-b",
        project_slug="proj-b",
        slug="proj-b",
        first_ts="2026-09-19T12:00:00Z",
        last_ts="2026-09-19T13:00:00Z",
        total_cost=5.0,
        total_tokens=9000,
    )
    store.upsert_transcript(
        session_id="session-b",
        path=r"C:\Users\definitely-not-a-real-person\.claude\projects\proj-b\session-b.jsonl",
        kind="top-level",
        digest_json=json.dumps({"turns": 4}),
        turns_agg=[
            {
                "day": "2026-09-19",
                "model": "claude-sonnet-5",
                "turns": 4,
                "input_tokens": 400,
                "cache_creation_tokens": 0,
                "cache_read_tokens": 100,
                "output_tokens": 80,
                "thinking_tokens": 0,
                "cc_5m": 0,
                "cc_1h": 0,
                "cost": 5.0,
            }
        ],
        compactions=[
            {
                "ts": "2026-09-19T12:30:00Z",
                "pre_tokens": 2000,
                "post_tokens": 500,
                "dropped_tokens": 1500,
                "trigger": "auto",
                "join_delta_s": 3.0,
            }
        ],
    )


# -- migrate / schema --------------------------------------------------

#: The exact v0.2.0 (schema version 4) DDL, taken verbatim from
#: ``git show v0.2.0:src/claudeglass/service/schema.py`` --
#: ``profiles``/``baselines`` are one version *before* v5's
#: ``content_hash``/``record_id`` columns. Used only by
#: :func:`test_migrate_upgrades_a_v4_store_without_losing_rows` (review
#: B2) to build a store shaped exactly like a real upgrade would find
#: one, without depending on git tag history being available at test
#: time.
_V4_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS projects (
        id         INTEGER PRIMARY KEY,
        slug       TEXT NOT NULL UNIQUE,
        root_path  TEXT NOT NULL,
        first_seen TEXT NOT NULL,
        last_seen  TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS snapshots (
        id             INTEGER PRIMARY KEY,
        project_id     INTEGER REFERENCES projects(id),
        ts             TEXT NOT NULL,
        schema_version INTEGER NOT NULL,
        digest_json    TEXT NOT NULL,
        UNIQUE (project_id, ts, schema_version)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id              TEXT PRIMARY KEY,
        project_id      INTEGER NOT NULL REFERENCES projects(id),
        slug            TEXT NOT NULL,
        first_ts        TEXT,
        last_ts         TEXT,
        span_s          REAL NOT NULL DEFAULT 0,
        archetype       TEXT,
        mode            TEXT,
        mode_source     TEXT,
        purpose         TEXT,
        purpose_source  TEXT,
        entrypoint      TEXT,
        billing_mode    TEXT,
        snapshot_id     INTEGER REFERENCES snapshots(id),
        profile_id      TEXT,
        total_cost      REAL NOT NULL DEFAULT 0,
        total_tokens    INTEGER NOT NULL DEFAULT 0,
        updated_at      TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_sessions_slug_first_ts ON sessions(slug, first_ts);
    """,
    """
    CREATE TABLE IF NOT EXISTS transcripts (
        id              INTEGER PRIMARY KEY,
        session_id      TEXT NOT NULL REFERENCES sessions(id),
        path            TEXT NOT NULL UNIQUE,
        kind            TEXT NOT NULL,
        agent_id        TEXT,
        agent_type      TEXT,
        spawn_depth     INTEGER NOT NULL DEFAULT 0,
        parent_agent_id TEXT,
        mtime_ns        INTEGER NOT NULL,
        size_bytes      INTEGER NOT NULL,
        parser_version  INTEGER NOT NULL,
        digest_blob     BLOB NOT NULL,
        missing_since   TEXT,
        updated_at      TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_transcripts_session_id ON transcripts(session_id);
    """,
    """
    CREATE TABLE IF NOT EXISTS turns_agg (
        id                     INTEGER PRIMARY KEY,
        transcript_id          INTEGER NOT NULL REFERENCES transcripts(id),
        day                    TEXT NOT NULL,
        model                  TEXT NOT NULL,
        turns                  INTEGER NOT NULL DEFAULT 0,
        input_tokens           INTEGER NOT NULL DEFAULT 0,
        cache_creation_tokens  INTEGER NOT NULL DEFAULT 0,
        cache_read_tokens      INTEGER NOT NULL DEFAULT 0,
        output_tokens          INTEGER NOT NULL DEFAULT 0,
        thinking_tokens        INTEGER NOT NULL DEFAULT 0,
        cc_5m                  INTEGER NOT NULL DEFAULT 0,
        cc_1h                  INTEGER NOT NULL DEFAULT 0,
        cost                   REAL NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_turns_agg_day ON turns_agg(day);
    """,
    """
    CREATE TABLE IF NOT EXISTS recache_turns (
        id                     INTEGER PRIMARY KEY,
        transcript_id          INTEGER NOT NULL REFERENCES transcripts(id),
        turn_index             INTEGER NOT NULL,
        signature              TEXT NOT NULL,
        cache_creation_tokens  INTEGER NOT NULL DEFAULT 0,
        preceding_primary      TEXT,
        gap_s                  REAL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS events_agg (
        id                 INTEGER PRIMARY KEY,
        transcript_id      INTEGER NOT NULL REFERENCES transcripts(id),
        kind               TEXT NOT NULL,
        subkind            TEXT,
        count              INTEGER NOT NULL DEFAULT 0,
        dropped_tokens_sum INTEGER NOT NULL DEFAULT 0,
        duration_ms_sum    INTEGER NOT NULL DEFAULT 0,
        UNIQUE (transcript_id, kind, subkind)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS compactions (
        id             INTEGER PRIMARY KEY,
        transcript_id  INTEGER NOT NULL REFERENCES transcripts(id),
        ts             TEXT NOT NULL,
        pre_tokens     INTEGER,
        post_tokens    INTEGER,
        dropped_tokens INTEGER,
        trigger        TEXT,
        join_delta_s   REAL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS session_tags (
        session_id TEXT NOT NULL REFERENCES sessions(id),
        key        TEXT NOT NULL,
        value      TEXT NOT NULL,
        set_at     TEXT NOT NULL,
        PRIMARY KEY (session_id, key)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS profiles (
        id         TEXT PRIMARY KEY,
        name       TEXT NOT NULL,
        toml_path  TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS baselines (
        id           INTEGER PRIMARY KEY,
        project_id   INTEGER REFERENCES projects(id),
        window_start TEXT NOT NULL,
        window_end   TEXT NOT NULL,
        archetype    TEXT,
        digest_json  TEXT NOT NULL,
        created_at   TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS workflow_runs (
        id            INTEGER PRIMARY KEY,
        session_id    TEXT NOT NULL REFERENCES sessions(id),
        run_id        TEXT NOT NULL,
        agent_count   INTEGER NOT NULL DEFAULT 0,
        phases        TEXT NOT NULL DEFAULT '[]',
        started       TEXT,
        finished      TEXT,
        cost          REAL NOT NULL DEFAULT 0,
        status        TEXT,
        updated_at    TEXT NOT NULL,
        UNIQUE (session_id, run_id)
    );
    CREATE INDEX IF NOT EXISTS idx_workflow_runs_session_id ON workflow_runs(session_id);
    """,
    """
    CREATE TABLE IF NOT EXISTS usage_log (
        id              INTEGER PRIMARY KEY,
        ts              TEXT NOT NULL,
        window_start    TEXT,
        window_end      TEXT,
        utilization_pct REAL,
        raw_json        TEXT NOT NULL
    );
    """,
)


def _build_v4_store(path: str) -> None:
    """Create a SQLite file at ``path`` shaped exactly like a v0.2.0
    store (schema version 4), with one row in every table."""
    conn = sqlite3.connect(path)
    try:
        for statement in _V4_STATEMENTS:
            conn.executescript(statement)
        conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '4')")
        conn.execute(
            "INSERT INTO projects (id, slug, root_path, first_seen, last_seen) "
            "VALUES (1, 'proj-a', '/root/proj-a', 't', 't')"
        )
        conn.execute(
            "INSERT INTO snapshots (id, project_id, ts, schema_version, digest_json) "
            "VALUES (1, 1, 't', 1, '{}')"
        )
        conn.execute(
            "INSERT INTO sessions (id, project_id, slug, updated_at) "
            "VALUES ('session-a', 1, 'proj-a', 't')"
        )
        conn.execute(
            "INSERT INTO transcripts "
            "(id, session_id, path, kind, mtime_ns, size_bytes, parser_version, digest_blob, updated_at) "
            "VALUES (1, 'session-a', '/root/proj-a/session-a.jsonl', 'top', 1, 1, 1, x'', 't')"
        )
        conn.execute(
            "INSERT INTO turns_agg (id, transcript_id, day, model) VALUES (1, 1, '2026-01-01', 'm')"
        )
        conn.execute(
            "INSERT INTO recache_turns (id, transcript_id, turn_index, signature) "
            "VALUES (1, 1, 0, 'sig')"
        )
        conn.execute("INSERT INTO events_agg (id, transcript_id, kind) VALUES (1, 1, 'k')")
        conn.execute("INSERT INTO compactions (id, transcript_id, ts) VALUES (1, 1, 't')")
        conn.execute(
            "INSERT INTO session_tags (session_id, key, value, set_at) "
            "VALUES ('session-a', 'mode', 'agentic', 't')"
        )
        conn.execute(
            "INSERT INTO profiles (id, name, toml_path, updated_at) VALUES ('p1', 'P1', '/x/p1.toml', 't')"
        )
        conn.execute(
            "INSERT INTO baselines (id, project_id, window_start, window_end, digest_json, created_at) "
            "VALUES (1, 1, 't', 't', '{}', 't')"
        )
        conn.execute(
            "INSERT INTO workflow_runs (id, session_id, run_id, updated_at) "
            "VALUES (1, 'session-a', 'wf-1', 't')"
        )
        conn.execute(
            "INSERT INTO usage_log (id, ts, raw_json) VALUES (1, 't', '{}')"
        )
        conn.commit()
    finally:
        conn.close()


def test_migrate_upgrades_a_v4_store_without_losing_rows(tmp_path) -> None:
    """Review B2: opening a v0.2.0 (schema version 4) store under the
    current code must migrate additively, not drop every table. Every
    row inserted under the old schema must still be there afterwards,
    and the two new v5 columns must exist."""
    from claudeglass.service import schema

    db_path = tmp_path / "v4.db"
    _build_v4_store(str(db_path))

    store = Store(str(db_path))
    store.open()
    try:
        assert store.schema_version() == schema.SCHEMA_VERSION

        conn = store._connection()
        for table in (
            "projects",
            "snapshots",
            "sessions",
            "transcripts",
            "turns_agg",
            "recache_turns",
            "events_agg",
            "compactions",
            "session_tags",
            "profiles",
            "baselines",
            "workflow_runs",
            "usage_log",
        ):
            count = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            assert count == 1, f"{table} lost its row(s) across the v4 -> v5 migration"

        profile_columns = {row["name"] for row in conn.execute("PRAGMA table_info(profiles)")}
        assert "content_hash" in profile_columns
        baseline_columns = {row["name"] for row in conn.execute("PRAGMA table_info(baselines)")}
        assert {"record_id", "content_hash"} <= baseline_columns

        # The migrated row's new columns take the documented default,
        # never NULL/missing.
        profile_row = conn.execute("SELECT content_hash FROM profiles WHERE id = 'p1'").fetchone()
        assert profile_row["content_hash"] == ""
        baseline_row = conn.execute("SELECT record_id, content_hash FROM baselines WHERE id = 1").fetchone()
        assert baseline_row["record_id"] is None
        assert baseline_row["content_hash"] == ""

        # The record_id uniqueness that CREATE_BASELINES declares
        # directly for a fresh table is present via the ladder's index
        # too -- a second NULL is fine (SQLite never treats NULLs as
        # conflicting), but a duplicate non-NULL value is rejected.
        conn.execute(
            "INSERT INTO baselines (id, project_id, window_start, window_end, digest_json, "
            "record_id, content_hash, created_at) VALUES (2, 1, 't', 't', '{}', 'rid-1', '', 't')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO baselines (id, project_id, window_start, window_end, digest_json, "
                "record_id, content_hash, created_at) VALUES (3, 1, 't', 't', '{}', 'rid-1', '', 't')"
            )
    finally:
        store.close()


def test_migrate_then_watcher_upgrades_a_stale_parser_version(tmp_path) -> None:
    """Integration of two independent version ladders that must not mask
    each other: ``Store.migrate()`` upgrading a v4 (schema.SCHEMA_VERSION
    4) store additively, preserving its one transcript row (review B2,
    see :func:`test_migrate_upgrades_a_v4_store_without_losing_rows`
    above), and the watcher's own stale-``parser_version`` re-parse (the
    confirmed watcher bug this fix addresses) then reaching that
    preserved row on the very next tick, even though its file never
    changed. The ladder's v7 -> v8 step marks every transcript as parsed
    by version 0 (so each gets its ``turns_agg.bucket``), which is the
    stale-parser check's own trigger, whatever version it was at."""
    import time

    from claudeglass import PARSER_VERSION
    from claudeglass.service import schema
    from claudeglass.service.contracts import ServeOptions
    from claudeglass.service.watcher import FileWatcher
    from helpers import turn_line, write_jsonl

    # A real, on-disk transcript the watcher can actually discover --
    # session id ("session-a") and project slug ("proj-a") match the v4
    # fixture's own session/transcript rows below, well outside the
    # live-file window.
    projects_root = tmp_path / "projects"
    session_path = projects_root / "proj-a" / "session-a.jsonl"
    session_path.parent.mkdir(parents=True, exist_ok=True)
    write_jsonl(session_path, [turn_line(timestamp="2026-09-18T12:00:00.000Z")])
    stable_mtime = time.time() - 3600
    os.utime(session_path, (stable_mtime, stable_mtime))
    file_stat = session_path.stat()

    db_path = tmp_path / "v4.db"
    _build_v4_store(str(db_path))
    # Point the v4 fixture's transcript row at the real file above, with
    # its real (mtime_ns, size_bytes) and an old parser_version -- from
    # the watcher's point of view this is a file that hasn't changed
    # since the last tick, but was parsed under a since-superseded
    # PARSER_VERSION.
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "UPDATE transcripts SET path = ?, mtime_ns = ?, size_bytes = ?, parser_version = 1 WHERE id = 1",
        (str(session_path), file_stat.st_mtime_ns, file_stat.st_size),
    )
    conn.commit()
    conn.close()

    store = Store(str(db_path))
    store.open()  # Store.migrate(): v4 -> current schema, additively
    try:
        assert store.schema_version() == schema.SCHEMA_VERSION
        transcript_count = store._connection().execute(
            "SELECT COUNT(*) AS n FROM transcripts"
        ).fetchone()["n"]
        assert transcript_count == 1, "the migration lost the v4 store's transcript row"
        assert store.known_files()[str(session_path)] == (file_stat.st_mtime_ns, file_stat.st_size, 0)

        options = ServeOptions(projects_root=projects_root, config_dir=tmp_path / "config")
        watcher = FileWatcher(store, options)
        stats = watcher.run_once()

        assert stats.errors == 0
        assert stats.files_reparsed_stale_parser == 1
        assert store.known_files()[str(session_path)][2] == PARSER_VERSION
    finally:
        store.close()


def test_migrate_backs_up_and_rebuilds_a_newer_than_code_store(tmp_path) -> None:
    """Review B2: a recorded schema_version newer than the running
    code's own is the one case (besides "no ladder step") a migration
    genuinely can't serve -- but the old file must be copied aside
    first, never just silently dropped. ROB-P6: a rating and a tag
    aren't re-derivable from transcripts the way the rest of the store
    is, so they must survive the rebuild itself, not just the backup."""
    from claudeglass.service import schema

    db_path = tmp_path / "newer.db"
    store = Store(str(db_path))
    store.open()
    _seed(store)
    store.set_feedback("session-a", outcome="delivered", slow=(), worth="yes", helped=())
    store.set_tag("session-a", "purpose", "refactor")
    assert store.summary()["sessions"] == 1
    store.close()

    newer_version = schema.SCHEMA_VERSION + 1
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(newer_version),),
    )
    conn.commit()
    conn.close()

    reopened = Store(str(db_path))
    reopened.open()
    try:
        assert reopened.schema_version() == schema.SCHEMA_VERSION
        assert reopened.summary()["sessions"] == 0

        # ROB-P6: a timestamped, never-overwritten backup, not the bare
        # "<path>.bak-<version>" of before.
        backups = list(tmp_path.glob(f"newer.db.bak-{newer_version}-*"))
        assert len(backups) == 1, "no (or more than one) backup was made before the newer-than-code store was rebuilt"

        # The rating and the tag survive the drop-and-rebuild, even though
        # "session-a" doesn't exist in the freshly recreated sessions table
        # yet (the next watcher tick repopulates it).
        assert reopened.feedback("session-a") == {
            "outcome": "delivered", "slow": [], "worth": "yes", "helped": [], "set_at": reopened.feedback("session-a")["set_at"],
        }
        assert reopened.all_tags().get("session-a") == {"purpose": "refactor"}
    finally:
        reopened.close()


def test_a_second_rebuild_of_the_same_version_gets_its_own_backup(tmp_path) -> None:
    """ROB-P6: the backup must never be overwritten -- two drop-and-
    rebuilds of a store recorded at the same unmigratable version (e.g.
    two 'serve' starts against a downgraded install) each get their own
    file, not one silently clobbering the other's data."""
    from claudeglass.service import schema

    db_path = tmp_path / "store.db"
    Store(str(db_path)).open()
    newer_version = schema.SCHEMA_VERSION + 1

    def _mark_newer() -> None:
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(newer_version),),
        )
        conn.commit()
        conn.close()

    _mark_newer()
    Store(str(db_path)).open()
    _mark_newer()
    Store(str(db_path)).open()

    backups = list(tmp_path.glob(f"store.db.bak-{newer_version}-*"))
    assert len(backups) == 2, f"expected two distinct backups, found {[p.name for p in backups]}"


def test_a_v8_to_v7_to_v8_round_trip_keeps_the_ratings(tmp_path, monkeypatch) -> None:
    """ROB-P6: session_feedback and session_tags are exported before a
    drop-and-rebuild and re-imported after, so a store that briefly looks
    older or newer than this build's own SCHEMA_VERSION -- e.g. an
    install downgraded and then upgraded again -- doesn't lose your
    ratings and tags along the way, even though the rest of the store
    (freely re-derivable from transcripts, or -- for a prediction, EST-P5
    -- from prediction-log.jsonl) is dropped and starts empty."""
    from claudeglass.service import schema

    db_path = tmp_path / "roundtrip.db"
    store = Store(str(db_path))
    store.open()  # built fresh at the real, current SCHEMA_VERSION (8)
    _seed(store)
    store.set_feedback("session-a", outcome="delivered", slow=("scope",), worth="yes", helped=("clearer-brief",))
    store.set_tag("session-a", "purpose", "refactor")
    store.upsert_prediction(
        prediction_id="pred-1", ts="2026-09-20T09:00:00Z", source="whatif", measure_key="model",
        agent=None, predicted_usd=1.5, predicted_pct=None, fidelity="ceiling",
    )
    store.close()

    # "Downgrade": a build that only knows up to v7 opens this v8 store.
    # v8 is newer than that build's own SCHEMA_VERSION, so migrate() takes
    # the backup-then-drop-and-rebuild path (same branch as the test
    # above), stamping the store back down to "7".
    monkeypatch.setattr(schema, "SCHEMA_VERSION", schema.SCHEMA_VERSION - 1)
    downgraded = Store(str(db_path))
    downgraded.open()
    assert downgraded.schema_version() == 7
    downgraded.close()

    # Upgrade back to the real, current build: v7 -> v8 walks the
    # additive MIGRATIONS ladder (it never drops a table, and finds the
    # bucket column the rebuilt tables already carry), so this step
    # alone was never the risk -- the ratings must already have survived
    # the downgrade step above to still be here now.
    monkeypatch.undo()
    upgraded = Store(str(db_path))
    upgraded.open()
    try:
        assert upgraded.schema_version() == schema.SCHEMA_VERSION == 8
        assert upgraded.feedback("session-a") == {
            "outcome": "delivered", "slow": ["scope"], "worth": "yes", "helped": ["clearer-brief"],
            "set_at": upgraded.feedback("session-a")["set_at"],
        }
        assert upgraded.all_tags().get("session-a") == {"purpose": "refactor"}
        # Predictions aren't ROB-P6-protected (unlike ratings/tags, a
        # prediction is re-ingestible from prediction-log.jsonl on the
        # next watcher tick) -- the round trip's drop-and-rebuild loses
        # it, and that's fine by design.
        assert upgraded.predictions() == []
    finally:
        upgraded.close()


def test_migrate_is_idempotent(store: Store) -> None:
    from claudeglass.service import schema

    assert store.schema_version() == schema.SCHEMA_VERSION
    store.migrate()
    store.migrate()
    assert store.schema_version() == schema.SCHEMA_VERSION


def test_migrate_drops_and_rebuilds_a_stale_store(store: Store) -> None:
    """A store whose recorded schema_version is older than the running
    code's is dropped and recreated from scratch on the next open() --
    the store is a derived cache, so this is safe, and the next watcher
    tick repopulates it (S1-integration fix 1.b)."""
    from claudeglass.service import schema

    _seed(store)
    assert store.summary()["sessions"] == 1

    conn = store._connection()
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', '0') "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
    )
    assert store.schema_version() == 0

    store.migrate()

    assert store.schema_version() == schema.SCHEMA_VERSION
    # The old session/transcript rows are gone -- a fresh, empty store.
    assert store.summary()["sessions"] == 0
    assert store.summary()["transcripts"] == 0
    assert store.known_files() == {}


# -- writer round trips --------------------------------------------------


def test_upsert_session_round_trips(store: Store) -> None:
    _seed(store)
    result = store.session("session-a")
    assert result is not None
    assert result["archetype"] == "plan-high-implement-low"
    assert result["total_cost"] == pytest.approx(1.23)
    assert result["total_tokens"] == 45000
    assert result["profile_id"] == "p1"


def test_ensure_session_keeps_existing_totals(store: Store) -> None:
    # The watcher's placeholder write runs before a session's subagents
    # are re-parsed; it must not zero the folded totals meanwhile.
    _seed(store)
    store.ensure_session(session_id="session-a", project_slug="proj-a", slug="proj-a")
    result = store.session("session-a")
    assert result["total_cost"] == pytest.approx(1.23)
    assert result["total_tokens"] == 45000
    store.ensure_session(session_id="session-new", project_slug="proj-a", slug="proj-a")
    assert store.session("session-new")["total_cost"] == 0


def test_upsert_transcript_is_idempotent_on_path(store: Store) -> None:
    _seed(store)
    # Re-upserting the same path (a re-parse after the file changed)
    # must update in place, not create a second transcript row.
    store.upsert_transcript(
        session_id="session-a",
        path=_FAKE_PATH,
        kind="top-level",
        mtime_ns=999,
        size_bytes=999,
        parser_version=3,
        digest_json=json.dumps({"turns": 11}),
    )
    detail = store.session("session-a")
    assert len(detail["transcripts"]) == 2  # top-level + subagent, not 3


def test_upsert_transcript_replaces_child_rows_wholesale(store: Store) -> None:
    _seed(store)
    # A re-parse with a different recache_turns set must replace, not
    # accumulate alongside, the previous set.
    store.upsert_transcript(
        session_id="session-a",
        path=_FAKE_PATH,
        kind="top-level",
        digest_json=json.dumps({"turns": 10}),
        recache_turns=[
            {"turn_index": 7, "signature": "prefix-invalidated", "cache_creation_tokens": 10},
        ],
    )
    recache = store.recache()
    assert "full-expiry" not in recache["by_signature"]
    assert recache["by_signature"]["prefix-invalidated"]["turns"] == 1


def test_known_files_reports_every_transcript(store: Store) -> None:
    _seed(store)
    files = store.known_files()
    assert files[_FAKE_PATH] == (123, 456, 3)
    assert files[_FAKE_SUB_PATH] == (789, 1011, 3)


def test_demote_parsed_marks_this_builds_rows_and_leaves_a_newer_builds(store: Store) -> None:
    from claudeglass import PARSER_VERSION

    _seed(store)
    store.upsert_transcript(
        session_id="session-a", path=_FAKE_PATH, kind="top-level",
        mtime_ns=123, size_bytes=456, parser_version=PARSER_VERSION, digest_json="{}",
    )
    newer = r"C:\Users\definitely-not-a-real-person\.claude\projects\proj-a\session-newer.jsonl"
    store.upsert_transcript(
        session_id="session-a", path=newer, kind="top-level",
        mtime_ns=1, size_bytes=2, parser_version=PARSER_VERSION + 1, digest_json="{}",
    )
    token = store.change_token()

    assert store.demote_parsed() == 2

    files = store.known_files()
    assert files[_FAKE_PATH] == (123, 456, 0)
    assert files[_FAKE_SUB_PATH] == (789, 1011, 0)
    assert files[newer] == (1, 2, PARSER_VERSION + 1)
    # Nothing a reader sees has changed until each row is parsed again.
    assert store.change_token() == token


def test_meta_values_round_trip(store: Store) -> None:
    assert store.get_meta("turns_priced_with") is None
    store.set_meta("turns_priced_with", "abc")
    assert store.get_meta("turns_priced_with") == "abc"
    store.set_meta("turns_priced_with", "def")
    assert store.get_meta("turns_priced_with") == "def"
    # The schema version lives in the same table, untouched.
    from claudeglass.service import schema

    assert store.schema_version() == schema.SCHEMA_VERSION


def test_get_meta_before_the_meta_table_exists_is_none() -> None:
    unopened = Store(":memory:")
    try:
        assert unopened.get_meta("turns_priced_with") is None
        assert unopened.schema_version() is None
    finally:
        unopened.close()


def test_remove_missing_marks_transcripts_not_in_known_set(store: Store) -> None:
    # Review finding 3: the store must outlive `cleanupPeriodDays` --
    # `remove_missing` only marks a vanished transcript's `missing_since`,
    # it never deletes the row. Only `retention_prune`/`--purge` do that.
    _seed(store)
    newly_missing = store.remove_missing({_FAKE_PATH})  # subagent path dropped
    assert newly_missing == 1
    assert store.count_missing_transcripts() == 1
    detail = store.session("session-a")
    # Both transcripts are still present -- a reader must include a
    # missing-but-not-yet-pruned transcript by default.
    assert len(detail["transcripts"]) == 2
    kinds = {trow["kind"] for trow in detail["transcripts"]}
    assert kinds == {"top-level", "subagent"}
    # Calling it again with the same known set is a no-op: already-missing
    # rows don't get re-marked or double-counted.
    assert store.remove_missing({_FAKE_PATH}) == 0
    assert store.count_missing_transcripts() == 1


def test_missing_transcript_survives_until_retention_prune_deletes_it(store: Store) -> None:
    """Regression test for review finding 3 (blocking): the store must
    outlive Claude Code's own ``cleanupPeriodDays`` retention. A
    transcript whose file has vanished is marked (``missing_since``), not
    deleted -- it keeps serving reports/rebuild regardless of how long
    ago it went missing, until its *session* actually ages past
    ``--retention-days``/``--purge``. Fails against a pre-fix
    ``remove_missing`` that deleted the row outright.
    """
    _seed(store)
    store.remove_missing({_FAKE_PATH})  # subagent path dropped -> marked missing
    assert store.count_missing_transcripts() == 1
    assert store.session("session-a") is not None

    # A generous retention window leaves a recently active session
    # (missing transcript or not) untouched.
    removed = store.retention_prune(retention_days=3650)
    assert removed == 0
    assert store.session("session-a") is not None
    assert store.count_missing_transcripts() == 1

    # Only once the session itself ages past the retention window does
    # the row -- and its missing transcript -- actually get deleted.
    removed = store.retention_prune(retention_days=0)
    assert removed == 1
    assert store.session("session-a") is None
    assert store.count_missing_transcripts() == 0


def test_retention_prune_removes_old_sessions(store: Store) -> None:
    _seed(store)
    store.upsert_session(
        session_id="session-old",
        project_slug="proj-a",
        slug="proj-a",
        first_ts="2000-01-01T00:00:00Z",
        last_ts="2000-01-01T01:00:00Z",
    )
    removed = store.retention_prune(retention_days=30)
    assert removed == 1
    assert store.session("session-old") is None
    assert store.session("session-a") is not None


# -- read queries --------------------------------------------------------


def test_summary_totals(store: Store) -> None:
    _seed(store)
    summary = store.summary()
    assert summary["sessions"] == 1
    assert summary["transcripts"] == 2
    assert summary["total_cost"] == pytest.approx(1.23)
    assert summary["total_tokens"] == 45000


def test_sessions_listing_has_no_transcripts_key(store: Store) -> None:
    _seed(store)
    rows = store.sessions()
    assert len(rows) == 1
    assert rows[0]["id"] == "session-a"
    assert "transcripts" not in rows[0]


def test_daily_usage_aggregates_across_transcripts(store: Store) -> None:
    _seed(store)
    rows = store.daily_usage(days=30)
    assert len(rows) == 1
    row = rows[0]
    assert row["day"] == "2026-09-18"
    assert row["turns"] == 15  # 10 top-level + 5 subagent
    assert row["input_tokens"] == 1200


def test_daily_usage_accepts_since_until_like_summary(store: Store) -> None:
    _seed(store)  # session-a's only turns_agg day is 2026-09-18
    assert store.daily_usage(since="2026-09-19T00:00:00Z") == []
    assert store.daily_usage(until="2026-09-17T00:00:00Z") == []
    assert len(store.daily_usage(since="2026-09-18T00:00:00Z", until="2026-09-18T23:59:00Z")) == 1
    # since=None/until=None/days=None means no bound at all -- as
    # ``route_daily_usage`` passes for ``?window=all``.
    assert len(store.daily_usage(days=None)) == 1


def test_daily_usage_split_agent_separates_main_from_subagents(store: Store) -> None:
    _seed(store)  # one top-level transcript (10 turns), one subagent (5 turns)
    rows = {(r["agent"], r["model"]): r for r in store.daily_usage(split="agent")}
    assert set(rows) == {("main", "claude-sonnet-5"), ("subagent", "claude-sonnet-5")}
    assert rows[("main", "claude-sonnet-5")]["turns"] == 10
    assert rows[("subagent", "claude-sonnet-5")]["turns"] == 5
    # split="model" (and the default) keep the original, unsplit shape.
    assert store.daily_usage(split="model") == store.daily_usage()
    assert "agent" not in store.daily_usage()[0]


def test_cache_read_tokens_by_model(store: Store) -> None:
    _seed(store)  # top-level cache_read_tokens=2000, subagent=400, same model
    assert store.cache_read_tokens_by_model() == {"claude-sonnet-5": 2400}
    assert store.cache_read_tokens_by_model(since="2026-09-19T00:00:00Z") == {}


def test_cache_read_tokens_by_model_counts_the_windows_sessions_whole(store: Store) -> None:
    """A bound inside the seed's day doesn't pull its reads in when the
    session's last reply (13:00) is outside the window, and a window that
    holds that last reply counts the whole session, as summary() does."""
    _seed(store)  # one session, last reply 2026-09-18T13:00:00Z
    assert store.cache_read_tokens_by_model(since="2026-09-18T14:00:00Z", until="2026-09-18T23:00:00Z") == {}
    assert store.cache_read_tokens_by_model(since="2026-09-18T12:30:00Z", until="2026-09-18T13:30:00Z") == {"claude-sonnet-5": 2400}
    assert store.summary(since="2026-09-18T14:00:00Z", until="2026-09-18T23:00:00Z")["sessions"] == 0


def test_compactions_listing(store: Store) -> None:
    _seed(store)
    rows = store.compactions()
    assert len(rows) == 1
    assert rows[0]["dropped_tokens"] == 140000


def test_compactions_listing_keeps_only_those_of_the_sessions_in_the_window(store: Store) -> None:
    _seed(store)  # session-a, 12:00 to 13:00, one compaction at 12:30 on 2026-09-18
    assert len(store.compactions(since="2026-09-18T12:00:00Z")) == 1
    # The window opens after the compaction but before the session's last
    # reply: the session counts, so its compaction does too.
    assert len(store.compactions(since="2026-09-18T12:45:00Z")) == 1
    assert store.compactions(since="2026-09-18T13:30:00Z") == []
    # The window closes before the session's last reply: it isn't counted,
    # whenever its compaction happened.
    assert store.compactions(until="2026-09-18T12:45:00Z") == []
    assert store.compactions(until="2026-09-18T12:00:00Z") == []


def _compacting_session(store: Store, session_id: str, slug: str, first_ts: str, last_ts: str, compacted_at: list[str]) -> None:
    store.upsert_session(
        session_id=session_id, project_slug=slug, slug=slug, first_ts=first_ts, last_ts=last_ts, total_cost=1.0, total_tokens=100
    )
    store.upsert_transcript(
        session_id=session_id,
        path=rf"C:\Users\definitely-not-a-real-person\.claude\projects\{slug}\{session_id}.jsonl",
        kind="top-level",
        digest_json=json.dumps({"turns": 1}),
        compactions=[
            {"ts": ts, "pre_tokens": 1000, "post_tokens": 200, "dropped_tokens": 800, "trigger": "auto", "join_delta_s": 1.0}
            for ts in compacted_at
        ],
    )


def test_compactions_are_counted_and_listed_by_the_sessions_the_window_counts(store: Store) -> None:
    """A window counts a session whole, so the list holds every compaction
    of the sessions the tiles count, wherever in the session it fell: one
    that began before the window opened lists the compactions from before
    it too, and one that ran on past the window's end lists none."""
    _compacting_session(store, "straddles", "proj-a", "2026-09-19T22:00:00Z", "2026-09-20T02:00:00Z",
                        ["2026-09-19T23:00:00Z", "2026-09-20T01:00:00Z"])
    _compacting_session(store, "earlier", "proj-a", "2026-09-18T09:00:00Z", "2026-09-18T10:00:00Z", ["2026-09-18T09:30:00Z"])
    _compacting_session(store, "later", "proj-b", "2026-09-21T09:00:00Z", "2026-09-21T10:00:00Z", ["2026-09-21T09:30:00Z"])
    _compacting_session(store, "runs-on", "proj-b", "2026-09-20T10:00:00Z", "2026-09-22T10:00:00Z", ["2026-09-20T11:00:00Z"])
    compactions = {"straddles": 2, "earlier": 1, "later": 1, "runs-on": 1}
    opens, closes = "2026-09-20T00:00:00Z", "2026-09-21T12:00:00Z"

    def listed(**window) -> list[str]:
        rows = store.compactions(**window)
        # The list is as long as the compactions of the sessions the window counts.
        counted = [s["id"] for s in store.sessions(**window)]
        assert len(rows) == sum(compactions[session_id] for session_id in counted), window
        return [row["ts"] for row in rows]

    # Last replies on the 20th, 21st and 22nd: three sessions, and all the
    # compactions of each, the one before the window's start among them.
    assert store.summary(since=opens)["sessions"] == 3
    assert listed(since=opens) == [
        "2026-09-19T23:00:00Z", "2026-09-20T01:00:00Z", "2026-09-20T11:00:00Z", "2026-09-21T09:30:00Z",
    ]
    # The window closes while "runs-on" is still going: it, and the
    # compaction inside the window, are out.
    assert store.summary(since=opens, until=closes)["sessions"] == 2
    assert listed(since=opens, until=closes) == ["2026-09-19T23:00:00Z", "2026-09-20T01:00:00Z", "2026-09-21T09:30:00Z"]
    assert listed(project_slugs=["proj-b"], since=opens) == ["2026-09-20T11:00:00Z", "2026-09-21T09:30:00Z"]
    # "Since my last change" counts by first reply: "straddles" began before it.
    assert store.summary(since=opens, window_by="first-reply")["sessions"] == 2
    assert listed(since=opens, window_by="first-reply") == ["2026-09-20T11:00:00Z", "2026-09-21T09:30:00Z"]
    # With no window, every compaction.
    assert len(store.compactions()) == sum(compactions.values())


def test_the_compactions_list_is_as_long_as_the_reports_own_count(tmp_path) -> None:
    """End to end: real transcripts through the watcher, then the report the
    dashboard builds over the same window. Its Compactions section counts the
    compactions of the sessions the window counts, so the list has as many
    rows, the one a straddling session had before the window opened included."""
    from claudeglass.config import Config
    from claudeglass.pricing import load_pricing
    from claudeglass.report import build_report
    from claudeglass.service.contracts import ServeOptions
    from claudeglass.service.rebuild import corpus_from_store
    from claudeglass.service.watcher import FileWatcher
    from helpers import system_line, turn_line, write_jsonl

    def compaction(timestamp: str) -> dict:
        return system_line(
            "compact_boundary",
            timestamp=timestamp,
            compactMetadata={"trigger": "auto", "preTokens": 100000, "postTokens": 20000, "cumulativeDroppedTokens": 80000},
        )

    def session(slug: str, name: str, first: str, last: str, compacted_at: list[str]) -> None:
        lines = [turn_line(timestamp=first)] + [compaction(ts) for ts in compacted_at] + [turn_line(timestamp=last)]
        path = tmp_path / "projects" / slug / f"{name}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        write_jsonl(path, lines)
        os.utime(path, (1_700_000_000, 1_700_000_000))  # long settled, so the watcher takes it

    session("proj-a", "straddles", "2026-09-19T22:00:00.000Z", "2026-09-20T02:00:00.000Z",
            ["2026-09-19T23:00:00.000Z", "2026-09-20T01:00:00.000Z"])
    session("proj-a", "earlier", "2026-09-18T09:00:00.000Z", "2026-09-18T10:00:00.000Z", ["2026-09-18T09:30:00.000Z"])
    session("proj-b", "later", "2026-09-21T09:00:00.000Z", "2026-09-21T10:00:00.000Z", ["2026-09-21T09:30:00.000Z"])

    store = Store(":memory:")
    store.open()
    try:
        stats = FileWatcher(store, ServeOptions(projects_root=tmp_path / "projects", config_dir=tmp_path / "config")).run_once()
        assert stats.errors == 0

        def reported(**window) -> int:
            corpus = corpus_from_store(store, **window)
            model = build_report(corpus, load_pricing(), Config(), projects=("proj-a", "proj-b"), window="w", include={"compactions"})
            section = next(item for item in model.sections if item.key == "compactions")
            mix = next(table for table in section.tables if table.name == "compactions_trigger_mix")
            return sum(row[1] for row in mix.rows)

        for window in (
            {"since": "2026-09-20T00:00:00Z"},
            {"since": "2026-09-20T00:00:00Z", "until": "2026-09-21T00:00:00Z"},
            {"since": "2026-09-20T03:00:00Z"},
            {"since": "2026-09-18T00:00:00Z"},
        ):
            assert len(store.compactions(**window)) == reported(**window), window
        # The straddling session counts whole: three compactions, not two.
        assert len(store.compactions(since="2026-09-20T00:00:00Z")) == 3
        assert len(store.compactions(since="2026-09-20T03:00:00Z")) == 1
    finally:
        store.close()


def test_sessions_listing_keeps_only_sessions_with_a_reply_in_the_window(store: Store) -> None:
    _seed(store)  # session-a, replies from 12:00 to 13:00 on 2026-09-18
    assert [s["id"] for s in store.sessions(since="2026-09-18T12:30:00Z")] == ["session-a"]
    assert store.sessions(since="2026-09-18T13:30:00Z") == []
    assert store.sessions(since="2026-09-17T00:00:00Z", until="2026-09-18T12:30:00Z") == []
    assert store.summary(since="2026-09-18T13:30:00Z")["sessions"] == 0


def test_first_reply_windows_count_only_the_sessions_started_in_them(store: Store) -> None:
    """The "since my last change" rule: session-a (12:00 to 13:00 on
    2026-09-18) was already running at 12:30, so it is left out whole,
    and only session-b's rows are counted, on every read."""
    _seed(store)
    _seed_second_project(store)  # session-b, 12:00 to 13:00 on 2026-09-19
    since = "2026-09-18T12:30:00Z"
    assert store.summary(since=since)["sessions"] == 2
    started = store.summary(since=since, window_by="first-reply")
    assert started["sessions"] == 1
    assert started["total_cost"] == pytest.approx(5.0)
    assert [s["id"] for s in store.sessions(since=since, window_by="first-reply")] == ["session-b"]
    assert {row["day"] for row in store.daily_usage(since=since)} == {"2026-09-18", "2026-09-19"}
    days = store.daily_usage(since=since, window_by="first-reply")
    assert {row["day"] for row in days} == {"2026-09-19"}
    assert sum(row["cost"] for row in days) == pytest.approx(started["total_cost"])
    assert store.cache_read_tokens_by_model(since=since, window_by="first-reply") == {"claude-sonnet-5": 100}
    assert len(store.compactions(since=since)) == 2
    assert [row["ts"] for row in store.compactions(since=since, window_by="first-reply")] == ["2026-09-19T12:30:00Z"]
    # Nothing started in the window: every read is empty.
    later = "2026-09-20T00:00:00Z"
    assert store.summary(since=later, window_by="first-reply")["sessions"] == 0
    assert store.daily_usage(since=later, window_by="first-reply") == []
    assert store.compactions(since=later, window_by="first-reply") == []
    # With no bound there is nothing to window by.
    assert store.daily_usage(days=None, window_by="first-reply") == store.daily_usage(days=None)
    with pytest.raises(ValueError):
        store.summary(since=since, window_by="mtime")


def test_summary_accepts_an_until_bound(store: Store) -> None:
    _seed(store)  # session-a's last reply is 2026-09-18T13:00:00Z
    assert store.summary(since="2026-09-17T00:00:00Z", until="2026-09-18T12:30:00Z")["sessions"] == 0
    assert store.summary(since="2026-09-17T00:00:00Z", until="2026-09-18T13:30:00Z")["sessions"] == 1
    # An until bound alone (no since, no window_days) also windows it.
    assert store.summary(until="2026-09-18T11:00:00Z")["sessions"] == 0
    assert store.summary(until="2026-09-18T13:30:00Z")["sessions"] == 1


def test_resolve_project_slug_finds_the_raw_slug_or_none(store: Store) -> None:
    _seed(store)
    _seed_second_project(store)
    assert store.resolve_project_slug("proj-a") == ["proj-a"]
    assert store.resolve_project_slug("proj-b") == ["proj-b"]
    assert store.resolve_project_slug("no-such-project") is None


def test_project_slugs_lists_every_project_with_a_session(store: Store) -> None:
    assert store.project_slugs() == []
    _seed(store)
    _seed_second_project(store)
    assert store.project_slugs() == ["proj-a", "proj-b"]


def test_summary_filters_by_project_slugs(store: Store) -> None:
    _seed(store)
    _seed_second_project(store)
    # The fully-unfiltered fast path still counts every project.
    assert store.summary()["sessions"] == 2
    only_a = store.summary(project_slugs=["proj-a"])
    assert only_a["sessions"] == 1
    assert only_a["transcripts"] == 2  # session-a's top-level + subagent
    assert only_a["total_cost"] == pytest.approx(1.23)
    only_b = store.summary(project_slugs=["proj-b"])
    assert only_b["sessions"] == 1
    assert only_b["transcripts"] == 1
    assert only_b["total_cost"] == pytest.approx(5.0)


def test_sessions_filters_by_project_slugs(store: Store) -> None:
    _seed(store)
    _seed_second_project(store)
    assert [s["id"] for s in store.sessions(project_slugs=["proj-b"])] == ["session-b"]
    assert {s["id"] for s in store.sessions(project_slugs=["proj-a"])} == {"session-a"}
    assert {s["id"] for s in store.sessions()} == {"session-a", "session-b"}


def test_daily_usage_filters_by_project_slugs(store: Store) -> None:
    _seed(store)
    _seed_second_project(store)
    rows = store.daily_usage(days=None, project_slugs=["proj-b"])
    assert len(rows) == 1
    assert rows[0]["day"] == "2026-09-19"
    assert rows[0]["turns"] == 4
    rows_a = store.daily_usage(days=None, project_slugs=["proj-a"])
    assert sum(r["turns"] for r in rows_a) == 15  # session-a's 10 + 5


def test_daily_usage_split_agent_filters_by_project_slugs(store: Store) -> None:
    _seed(store)
    _seed_second_project(store)
    rows = store.daily_usage(days=None, split="agent", project_slugs=["proj-a"])
    assert {r["agent"] for r in rows} == {"main", "subagent"}
    assert sum(r["turns"] for r in rows) == 15
    rows_b = store.daily_usage(days=None, split="agent", project_slugs=["proj-b"])
    assert {r["agent"] for r in rows_b} == {"main"}
    assert sum(r["turns"] for r in rows_b) == 4


def test_cache_read_tokens_by_model_filters_by_project_slugs(store: Store) -> None:
    _seed(store)
    _seed_second_project(store)
    assert store.cache_read_tokens_by_model(days=None, project_slugs=["proj-a"]) == {"claude-sonnet-5": 2400}
    assert store.cache_read_tokens_by_model(days=None, project_slugs=["proj-b"]) == {"claude-sonnet-5": 100}
    assert store.cache_read_tokens_by_model(days=None) == {"claude-sonnet-5": 2500}


def test_compactions_filters_by_project_slugs(store: Store) -> None:
    _seed(store)
    _seed_second_project(store)
    assert len(store.compactions()) == 2
    only_a = store.compactions(project_slugs=["proj-a"])
    assert len(only_a) == 1
    assert only_a[0]["dropped_tokens"] == 140000
    only_b = store.compactions(project_slugs=["proj-b"])
    assert len(only_b) == 1
    assert only_b[0]["dropped_tokens"] == 1500


def test_snapshots_listing(store: Store) -> None:
    _seed(store)
    rows = store.snapshots()
    assert len(rows) == 1
    assert rows[0]["schema_version"] == 2
    assert rows[0]["project_slug"] == "proj-a"


def test_snapshots_reports_global_attribution_as_null_project_slug(store: Store) -> None:
    """A snapshot attributed to Store.GLOBAL_PROJECT_SLUG (the watcher's
    synthetic attribution for a machine-wide capture with no real
    per-project identity) is exposed honestly as project_slug=None, never
    as the internal sentinel string (S1-integration fix 1.c)."""
    from claudeglass.service.store import GLOBAL_PROJECT_SLUG

    assert GLOBAL_PROJECT_SLUG == "__global__"
    store.upsert_snapshot(
        project_slug=GLOBAL_PROJECT_SLUG,
        ts="2026-09-19T00:00:00Z",
        schema_version=2,
        digest_json=json.dumps({}),
    )
    rows = store.snapshots()
    assert len(rows) == 1
    assert rows[0]["project_slug"] is None


def test_upsert_snapshot_dedupes_by_natural_key(store: Store) -> None:
    """Re-ingesting the same (project, ts, schema_version) snapshot
    updates the existing row instead of creating a duplicate -- the
    ON CONFLICT dedupe that lets the watcher drop its own pre-check
    workaround (S1-integration fix 1.b)."""
    first_id = store.upsert_snapshot(
        project_slug="proj-a",
        ts="2026-09-18T12:00:00Z",
        schema_version=2,
        digest_json=json.dumps({"a": 1}),
    )
    second_id = store.upsert_snapshot(
        project_slug="proj-a",
        ts="2026-09-18T12:00:00Z",
        schema_version=2,
        digest_json=json.dumps({"a": 2}),
    )
    assert first_id == second_id
    rows = store.snapshots()
    assert len(rows) == 1
    assert json.loads(rows[0]["digest_json"]) == {"a": 2}

    # A different schema_version for the same (project, ts) is a distinct
    # natural key -- a second row, not an update of the first.
    store.upsert_snapshot(
        project_slug="proj-a",
        ts="2026-09-18T12:00:00Z",
        schema_version=1,
        digest_json=json.dumps({"a": 1}),
    )
    assert len(store.snapshots()) == 2


def test_profiles_and_baselines_listing(store: Store) -> None:
    _seed(store)
    profiles = store.profiles()
    assert profiles == [{"id": "p1", "name": "implementation-heavy", "updated_at": profiles[0]["updated_at"]}]
    baselines = store.baselines()
    assert len(baselines) == 1
    assert baselines[0]["archetype"] == "plan-high-implement-low"


def test_tags_round_trip(store: Store) -> None:
    _seed(store)
    assert store.tags("session-a") == {"purpose": "refactor-override"}
    store.set_tag("session-a", "purpose", "docs")
    assert store.tags("session-a") == {"purpose": "docs"}


# -- change_token ------------------------------------------------------


def test_change_token_changes_when_a_transcript_is_added_or_reparsed(store: Store) -> None:
    before = store.change_token()
    _seed(store)
    after_seed = store.change_token()
    assert after_seed != before

    # A brand-new transcript (distinct natural key) changes the row count,
    # which the token always reflects regardless of timestamp resolution.
    store.upsert_transcript(
        session_id="session-a",
        path=_FAKE_PATH + ".extra",
        kind="subagent",
        digest_json=json.dumps({"turns": 999}),
    )
    after_new_transcript = store.change_token()
    assert after_new_transcript != after_seed


def test_change_token_stable_when_nothing_changed(store: Store) -> None:
    _seed(store)
    assert store.change_token() == store.change_token()


def test_rewriting_an_unchanged_workflow_run_keeps_the_change_token(store: Store, monkeypatch) -> None:
    """The watcher re-reads every workflow file each tick. Writing the
    same values again must not touch the row, or the token would move
    every tick and every kept report would be rebuilt for nothing."""
    from claudeglass.service import store as store_mod

    _seed(store)
    run = dict(session_id="session-a", run_id="wf_1", agent_count=2, phase_titles=["Build"], cost=1.5, status="done")
    monkeypatch.setattr(store_mod, "_now", lambda: "2026-09-18T10:00:00Z")
    store.upsert_workflow_run(**run)
    before = store.change_token()

    monkeypatch.setattr(store_mod, "_now", lambda: "2026-09-18T10:05:00Z")
    store.upsert_workflow_run(**run)
    assert store.change_token() == before

    store.upsert_workflow_run(**{**run, "status": "failed"})
    assert store.change_token() != before
    row = store._connection().execute("SELECT status, updated_at FROM workflow_runs").fetchone()
    assert (row["status"], row["updated_at"]) == ("failed", "2026-09-18T10:05:00Z")


# -- turns_for_session ---------------------------------------------------


def test_turns_for_session_returns_none_without_a_top_level_transcript(store: Store) -> None:
    store.upsert_session(session_id="ghost", project_slug="proj-a", slug="proj-a")
    assert store.turns_for_session("ghost") is None
    assert store.turns_for_session("does-not-exist") is None


def test_turns_for_session_builds_series_and_markers(store: Store) -> None:
    from claudeglass.cache import encode_result
    from claudeglass.model import EventKind, Turn, TranscriptMeta, TranscriptResult

    result = TranscriptResult(
        meta=TranscriptMeta(path=_FAKE_PATH, kind="top-level", session_id="session-b"),
        turns=[
            Turn(turn_index=0, ctx=0),  # synthetic -- excluded
            Turn(turn_index=1, ctx=1000, cache_creation_tokens=500, is_recache=False,
                 preceding_primary=EventKind.HUMAN_TEXT, human_prompt_chars=42),
            Turn(turn_index=2, ctx=1500, cache_creation_tokens=0, is_recache=True,
                 preceding_primary=EventKind.COMPACT_BOUNDARY),
            Turn(turn_index=3, ctx=2000, cache_creation_tokens=300, is_recache=False,
                 preceding_primary=EventKind.TOOL_RESULT, agent_brief_chars=120),
        ],
    )
    store.upsert_session(session_id="session-b", project_slug="proj-a", slug="proj-a")
    store.upsert_transcript(
        session_id="session-b",
        path=_FAKE_PATH + ".b",
        kind="top-level",
        digest_json=json.dumps(encode_result(result)),
    )

    data = store.turns_for_session("session-b")
    assert data is not None
    assert data["turn_series"] == [
        [1, 1000, 500, False, "human_text"],
        [2, 1500, 0, True, "compact_boundary"],
        [3, 2000, 300, False, "tool_result"],
    ]
    assert data["markers"] == {"compactions": [2], "spawns": [3], "human": [1]}
    assert data["truncated"] is False
    assert_privacy(data)


def test_turns_for_session_downsamples_above_the_point_cap(store: Store) -> None:
    """Regression test for review finding 11 (should-fix): a very long
    session's turn_series must be capped at
    ``Store.MAX_TURN_SERIES_POINTS`` rather than shipping every single
    turn to the browser (the original unbounded list is also what fed
    app.js's ``Math.max.apply`` -- finding 10). Every marker turn must
    still survive the downsampling.
    """
    from claudeglass.cache import encode_result
    from claudeglass.model import EventKind, Turn, TranscriptMeta, TranscriptResult

    total_turns = Store.MAX_TURN_SERIES_POINTS + 500
    marker_turn_index = total_turns - 1  # deliberately outside any stride sample
    turns = []
    for i in range(1, total_turns + 1):
        is_marker = i == marker_turn_index
        turns.append(
            Turn(
                turn_index=i,
                ctx=i * 10,
                preceding_primary=EventKind.COMPACT_BOUNDARY if is_marker else None,
            )
        )
    result = TranscriptResult(
        meta=TranscriptMeta(path=_FAKE_PATH, kind="top-level", session_id="session-huge"),
        turns=turns,
    )
    store.upsert_session(session_id="session-huge", project_slug="proj-a", slug="proj-a")
    store.upsert_transcript(
        session_id="session-huge",
        path=_FAKE_PATH + ".huge",
        kind="top-level",
        digest_json=json.dumps(encode_result(result)),
    )

    data = store.turns_for_session("session-huge")
    assert data is not None
    assert data["truncated"] is True
    assert len(data["turn_series"]) <= Store.MAX_TURN_SERIES_POINTS
    # markers are always computed from the full turn list, never thinned.
    assert data["markers"]["compactions"] == [marker_turn_index]
    # the marker turn itself must survive into the downsampled series.
    kept_turn_indices = {row[0] for row in data["turn_series"]}
    assert marker_turn_index in kept_turn_indices


# -- privacy guard ---------------------------------------------------------


def test_slug_username_segment_is_redacted_from_every_read_query(store: Store) -> None:
    """Regression test for review finding 6 (should-fix): a project slug
    is derived from Claude Code's own project-directory naming, which
    embeds the caller's OS username -- a ``C:\\Users\\someone\\repo``
    project directory becomes the slug ``"C--Users-someone-repo"``. Every
    read query returning a slug/``project_slug`` must redact that
    username segment to ``"<user>"`` before it leaves the store layer.
    Fails against a pre-fix store that returned the raw slug unchanged.
    """
    raw_slug = "C--Users-someone-repo"
    store.upsert_session(
        session_id="session-user",
        project_slug=raw_slug,
        project_root_path=_FAKE_ROOT,
        slug=raw_slug,
        first_ts="2026-09-18T12:00:00Z",
        last_ts="2026-09-18T13:00:00Z",
    )
    store.upsert_snapshot(
        project_slug=raw_slug,
        project_root_path=_FAKE_ROOT,
        ts="2026-09-18T12:30:00Z",
        schema_version=2,
        digest_json=json.dumps({}),
    )
    store.record_baseline(
        project_slug=raw_slug,
        project_root_path=_FAKE_ROOT,
        window_start="2026-09-11T00:00:00Z",
        window_end="2026-09-18T00:00:00Z",
        archetype="plan-high-implement-low",
        digest_json=json.dumps({"sessions": 1}),
    )

    outputs = {
        "sessions": store.sessions(),
        "session": store.session("session-user"),
        "snapshots": store.snapshots(),
        "baselines": store.baselines(),
    }
    blob = json.dumps(outputs, default=str)
    assert "someone" not in blob, f"raw username segment leaked into a read-query result: {blob}"
    assert raw_slug not in blob, f"unredacted slug leaked into a read-query result: {blob}"
    assert "<user>" in blob, "redact_slug should have substituted the <user> placeholder"


def test_no_local_path_leaks_from_any_read_query(store: Store) -> None:
    _seed(store)

    outputs = {
        "summary": store.summary(),
        "sessions": store.sessions(),
        "session": store.session("session-a"),
        "daily_usage": store.daily_usage(),
        "daily_usage_split_agent": store.daily_usage(split="agent"),
        "cache_read_tokens_by_model": store.cache_read_tokens_by_model(),
        "recache": store.recache(),
        "compactions": store.compactions(),
        "snapshots": store.snapshots(),
        "profiles": store.profiles(),
        "baselines": store.baselines(),
        "tags": store.tags("session-a"),
    }
    blob = json.dumps(outputs, default=str)
    for needle in (_FAKE_PATH, _FAKE_SUB_PATH, _FAKE_ROOT, _FAKE_PROFILE_PATH, "definitely-not-a-real-person"):
        assert needle not in blob, f"{needle!r} leaked into read-query output"

    # known_files() is explicitly local-only -- confirm it DOES carry the
    # path (proving the guard above isn't vacuously passing because no
    # method ever stored the path at all).
    assert _FAKE_PATH in store.known_files()


__all__: list[str] = []


def test_sessions_say_where_they_ran_without_the_path():
    store = Store(":memory:")
    store.open()
    for session_id, path in (
        ("s-local", r"C:\Users\alice\.claude\projects\proj\s-local.jsonl"),
        ("s-wsl", r"\\wsl.localhost\Ubuntu\home\alice\.claude\projects\-home-alice-repo\s-wsl.jsonl"),
    ):
        store.ensure_session(session_id=session_id, project_slug="proj")
        store.upsert_transcript(
            session_id=session_id,
            path=path,
            kind="top-level",
            mtime_ns=1,
            size_bytes=1,
            parser_version=1,
            digest_json="{}",
        )
    sources = {row["id"]: row["source"] for row in store.sessions()}
    assert sources == {"s-local": "This computer", "s-wsl": "WSL: Ubuntu"}
    detail = store.session("s-wsl")
    assert detail["source"] == "WSL: Ubuntu"
    assert "wsl.localhost" not in json.dumps(detail)


# -- session_feedback (v6) -------------------------------------------------


def test_feedback_round_trips_and_nothing_ticked_clears_it(store: Store) -> None:
    _seed(store)
    assert store.feedback("session-a") is None and store.feedback_count() == 0
    store.set_feedback("session-a", outcome="met", slow=["unclear", "tools"], worth="yes", helped=[])
    saved = store.feedback("session-a")
    assert saved["outcome"] == "met" and saved["slow"] == ["unclear", "tools"] and saved["helped"] == []
    assert saved["set_at"]
    assert store.session("session-a")["feedback"] == saved
    assert store.all_feedback() == {"session-a": saved}
    store.set_feedback("session-a", outcome=None, slow=["tools"], worth=None, helped=["context"])
    assert store.feedback("session-a")["outcome"] is None and store.feedback_count() == 1
    store.set_feedback("session-a", outcome=None, worth=None)
    assert store.feedback("session-a") is None and store.session("session-a")["feedback"] is None


def test_change_token_changes_when_a_rating_is_set(store: Store) -> None:
    _seed(store)
    before = store.change_token()
    store.set_feedback("session-a", outcome="partly", worth="no")
    assert store.change_token() != before


def test_retention_prune_removes_old_ratings(store: Store) -> None:
    _seed(store)
    store.upsert_session(
        session_id="session-old", project_slug="proj-a", slug="proj-a",
        first_ts="2000-01-01T00:00:00Z", last_ts="2000-01-01T01:00:00Z",
    )
    store.set_feedback("session-old", outcome="missed", worth="no")
    assert store.retention_prune(retention_days=30) == 1
    assert store.all_feedback() == {}


def test_upsert_prediction_is_idempotent_on_its_id(store: Store) -> None:
    inserted = store.upsert_prediction(
        prediction_id="pred-1", ts="2026-09-20T09:00:00Z", source="whatif", measure_key="model",
        agent=None, predicted_usd=1.5, predicted_pct=None, fidelity="ceiling",
    )
    again = store.upsert_prediction(
        prediction_id="pred-1", ts="2026-09-20T09:00:00Z", source="whatif", measure_key="model",
        agent=None, predicted_usd=9.9, predicted_pct=None, fidelity="ceiling",
    )
    assert inserted is True and again is False
    [row] = store.predictions()
    assert row["predicted_usd"] == 1.5  # the second (differing) write was ignored


def test_mark_prediction_seen_and_judge_prediction(store: Store) -> None:
    store.upsert_prediction(
        prediction_id="pred-1", ts="2026-09-20T09:00:00Z", source="whatif", measure_key="agent_cost",
        agent="Explore", predicted_usd=2.0, predicted_pct=None, fidelity="ceiling",
    )
    assert store.mark_prediction_seen("pred-1") is True
    assert store.mark_prediction_seen("pred-1") is False  # already seen
    assert store.mark_prediction_seen("no-such-id") is False
    assert store.predictions(judged=False)[0]["seen_at"]
    assert store.predictions(judged=True) == []

    store.judge_prediction(
        "pred-1", change_ts="2026-09-21T09:00:00Z", verdict="as_estimated", measured_usd=1.8, measured_pct=None
    )
    [judged] = store.predictions(judged=True)
    assert judged["verdict"] == "as_estimated" and judged["measured_usd"] == 1.8 and judged["judged_at"]
    assert store.predictions(judged=False) == []


def test_reopen_predictions_unjudges_only_the_verdict_named(store: Store) -> None:
    for prediction_id, verdict in (("short", "too_little_data"), ("kept", "as_estimated")):
        store.upsert_prediction(
            prediction_id=prediction_id, ts="2026-09-20T09:00:00Z", source="whatif", measure_key="model",
            agent=None, predicted_usd=2.0, predicted_pct=None, fidelity="ceiling",
        )
        store.judge_prediction(
            prediction_id, change_ts="2026-09-21T09:00:00Z", verdict=verdict, measured_usd=1.8, measured_pct=None
        )
    assert store.reopen_predictions("too_little_data") == 1
    [reopened] = store.predictions(judged=False)
    assert reopened["id"] == "short"
    assert [reopened[k] for k in ("change_ts", "judged_at", "verdict", "measured_usd", "measured_pct")] == [None] * 5
    assert [row["id"] for row in store.predictions(judged=True)] == ["kept"]
    assert store.reopen_predictions("too_little_data") == 0


def test_prune_predictions_drops_stale_unjudged_and_old_judged_rows(store: Store) -> None:
    store.upsert_prediction(
        prediction_id="fresh", ts="2026-09-20T09:00:00Z", source="whatif", measure_key="model",
        agent=None, predicted_usd=1.0, predicted_pct=None, fidelity="ceiling",
    )
    store.upsert_prediction(
        prediction_id="stale-unjudged", ts="2026-01-01T09:00:00Z", source="whatif", measure_key="model",
        agent=None, predicted_usd=1.0, predicted_pct=None, fidelity="ceiling",
    )
    store.upsert_prediction(
        prediction_id="old-judged", ts="2025-01-01T09:00:00Z", source="whatif", measure_key="model",
        agent=None, predicted_usd=1.0, predicted_pct=None, fidelity="ceiling",
    )
    store.judge_prediction(
        "old-judged", change_ts="2025-01-02T09:00:00Z", verdict="smaller", measured_usd=0.1, measured_pct=None
    )
    conn = store._connection()
    conn.execute("UPDATE predictions SET judged_at = '2025-01-02T09:00:00Z' WHERE id = 'old-judged'")

    removed = store.prune_predictions(now="2026-09-24T09:00:00Z")
    assert removed == 2
    remaining = {row["id"] for row in store.predictions()}
    assert remaining == {"fresh"}


def test_migrate_upgrades_a_v5_store_with_the_feedback_table(tmp_path) -> None:
    from claudeglass.service import schema
    from claudeglass.service import store as store_mod

    db_path = tmp_path / "v5.db"
    store = Store(str(db_path))
    store.open()
    _seed(store)
    conn = store._connection()
    conn.execute("DROP TABLE session_feedback")
    conn.execute("UPDATE meta SET value = '5' WHERE key = ?", (store_mod._SCHEMA_VERSION_KEY,))
    conn.commit()
    store.close()

    store = Store(str(db_path))
    store.open()
    try:
        assert store.schema_version() == schema.SCHEMA_VERSION == 8
        assert store.session("session-a") is not None
        assert store.tags("session-a") == {"purpose": "refactor-override"}
        store.set_feedback("session-a", outcome="met", worth="yes")
        assert store.feedback_count() == 1
    finally:
        store.close()



def test_read_session_marks_reads_tags_and_ratings_without_writing(tmp_path) -> None:
    from claudeglass.service.store import read_session_marks

    db_path = tmp_path / "service.db"
    assert read_session_marks(db_path) == ({}, {})
    store = Store(str(db_path))
    store.open()
    _seed(store)
    store.set_feedback("session-a", outcome="met", slow=["tools"], worth="yes", helped=[])
    expected_ratings = store.all_feedback()
    store.close()
    before = db_path.read_bytes()
    tags, ratings = read_session_marks(db_path)
    assert tags == {"session-a": {"purpose": "refactor-override"}}
    assert ratings == expected_ratings and ratings["session-a"]["slow"] == ["tools"]
    assert db_path.read_bytes() == before


def test_read_session_marks_of_a_store_without_the_ratings_table_reads_the_tags(tmp_path) -> None:
    from claudeglass.service.store import read_session_marks

    db_path = tmp_path / "service.db"
    store = Store(str(db_path))
    store.open()
    _seed(store)
    conn = store._connection()
    conn.execute("DROP TABLE session_feedback")
    conn.commit()
    store.close()
    assert read_session_marks(db_path) == ({"session-a": {"purpose": "refactor-override"}}, {})
    (tmp_path / "junk.db").write_bytes(b"not a database")
    assert read_session_marks(tmp_path / "junk.db") == ({}, {})


# -- turns_agg.bucket (schema 8) and daily_usage's local days ------------------

_IST = timezone(timedelta(hours=5, minutes=30))
_PST = timezone(timedelta(hours=-8))
_MODEL = "claude-sonnet-5"
_COLUMNS = [
    "turns", "input_tokens", "cache_creation_tokens", "cache_read_tokens",
    "output_tokens", "thinking_tokens", "cc_5m", "cc_1h", "cost",
]


def _tzdata_has(name: str) -> bool:
    """Same helper as ``tests/test_classify.py``: a bare Windows install
    has no ``tzdata`` for ``zoneinfo`` to read."""
    try:
        ZoneInfo(name)
        return True
    except ZoneInfoNotFoundError:
        return False


def _agg(bucket: str | None, *, day: str | None = None, model: str = _MODEL, turns: int = 1, cost: float = 1.0) -> dict:
    """One ``turns_agg`` row. ``bucket`` is the UTC quarter hour
    (``2026-09-18T23:45:00Z``), and its date is the row's ``day``; a row
    written before schema 8 has no bucket and says its ``day``."""
    return {
        "day": day or bucket[:10], "bucket": bucket, "model": model, "turns": turns,
        "input_tokens": 10 * turns, "cache_creation_tokens": 0, "cache_read_tokens": turns,
        "output_tokens": turns, "thinking_tokens": 0, "cc_5m": 0, "cc_1h": 0, "cost": cost,
    }


def _seed_agg(
    store: Store,
    *rows: dict,
    session_id: str = "session-bk",
    slug: str = "proj-bk",
    kind: str = "top-level",
    path: str | None = None,
    first_ts: str = "2026-09-01T00:00:00Z",
    last_ts: str = "2026-09-30T00:00:00Z",
    parser_version: int = 0,
) -> None:
    """A session (wholly inside any window the tests ask about, unless a
    test says otherwise) and one transcript holding ``rows``."""
    store.upsert_session(session_id=session_id, project_slug=slug, slug=slug, first_ts=first_ts, last_ts=last_ts)
    store.upsert_transcript(
        session_id=session_id,
        path=path or f"/x/{session_id}-{kind}.jsonl",
        kind=kind,
        parser_version=parser_version,
        digest_json="{}",
        turns_agg=list(rows),
    )


def _by_day(rows: list[dict]) -> list[tuple[str, int]]:
    return [(row["day"], row["turns"]) for row in rows]


def test_daily_usage_groups_buckets_into_local_days(store: Store) -> None:
    _seed_agg(store, _agg("2026-09-18T23:45:00Z", turns=2), _agg("2026-09-19T00:15:00Z", turns=3))
    utc = store.daily_usage(days=None)
    assert _by_day(utc) == [("2026-09-18", 2), ("2026-09-19", 3)]
    assert store.daily_usage(days=None, tz="UTC") == utc
    # In India both buckets are on the 19th (05:15 and 05:45), in
    # California both are on the 18th (15:45 and 16:15).
    assert _by_day(store.daily_usage(days=None, tz=_IST)) == [("2026-09-19", 5)]
    assert _by_day(store.daily_usage(days=None, tz=_PST)) == [("2026-09-18", 5)]


def test_daily_usage_adds_up_every_column_across_a_local_day(store: Store) -> None:
    _seed_agg(
        store,
        _agg("2026-09-18T20:00:00Z", turns=2, cost=0.25),
        _agg("2026-09-18T22:30:00Z", turns=3, cost=0.5),
        _agg("2026-09-19T01:00:00Z", turns=4, cost=1.5),
    )
    [row] = store.daily_usage(days=None, tz=_IST)  # 01:30, 04:00 and 06:30 on the 19th
    assert row["day"] == "2026-09-19" and row["model"] == _MODEL
    assert row["turns"] == 9
    assert row["input_tokens"] == 90 and row["cache_read_tokens"] == 9 and row["output_tokens"] == 9
    assert row["cost"] == pytest.approx(2.25)


def test_daily_usage_window_starts_in_the_bucket_holding_since(store: Store) -> None:
    _seed_agg(
        store,
        _agg("2026-09-18T11:45:00Z", turns=1),
        _agg("2026-09-18T12:00:00Z", turns=2),
        _agg("2026-09-18T12:15:00Z", turns=4),
    )

    def turns(**window) -> int:
        return sum(row["turns"] for row in store.daily_usage(days=None, **window))

    assert turns(since="2026-09-18T12:07:00Z") == 6  # keeps 12:00, drops 11:45
    assert turns(since="2026-09-18T12:00:00Z") == 6
    assert turns(since="2026-09-18T12:15:00Z") == 4
    assert turns(since="2026-09-18T11:59:59Z") == 7  # the 11:45 bucket still holds it
    assert turns(since="2026-09-18T12:16:00Z") == 4
    assert turns(since="2026-09-18T12:30:00Z") == 0


def test_daily_usage_window_ends_before_the_bucket_starting_at_until(store: Store) -> None:
    _seed_agg(
        store,
        _agg("2026-09-18T11:45:00Z", turns=1),
        _agg("2026-09-18T12:00:00Z", turns=2),
        _agg("2026-09-18T12:15:00Z", turns=4),
    )

    def turns(until: str) -> int:
        return sum(row["turns"] for row in store.daily_usage(days=None, until=until))

    assert turns("2026-09-18T12:15:00Z") == 3  # strict: 12:15 itself isn't in
    assert turns("2026-09-18T12:15:30Z") == 7
    assert turns("2026-09-18T12:00:00Z") == 1
    assert turns("2026-09-18T11:45:00Z") == 0
    assert turns("2026-09-18T12:10:00Z") == 3


def test_daily_usage_calendar_window_in_a_half_hour_zone_is_exact(store: Store) -> None:
    # 18 September, 00:00 to 24:00 in India is 17 September 18:30 UTC to
    # 18 September 18:30 UTC, and a local midnight is always on a bucket
    # edge.
    _seed_agg(
        store,
        _agg("2026-09-17T18:15:00Z", turns=1),  # 23:45 on the 17th
        _agg("2026-09-17T18:30:00Z", turns=2),  # 00:00 on the 18th
        _agg("2026-09-18T18:15:00Z", turns=4),  # 23:45 on the 18th
        _agg("2026-09-18T18:30:00Z", turns=8),  # 00:00 on the 19th
    )
    window = {"since": "2026-09-17T18:30:00Z", "until": "2026-09-18T18:30:00Z"}
    assert _by_day(store.daily_usage(days=None, tz=_IST, **window)) == [("2026-09-18", 6)]
    # The same window grouped by UTC day is two days.
    assert _by_day(store.daily_usage(days=None, tz="UTC", **window)) == [("2026-09-17", 2), ("2026-09-18", 4)]


def test_daily_usage_legacy_rows_never_add_a_day_before_the_first_local_day(store: Store) -> None:
    # Rows with no bucket can only say their UTC day. East of UTC the
    # window's first local day starts on the previous UTC day, and a row for
    # that UTC day would be a column the chart has no room for.
    _seed_agg(store, _agg(None, day="2026-09-17", turns=1), _agg(None, day="2026-09-18", turns=2))
    since = "2026-09-17T18:30:00Z"  # 00:00 on the 18th in India
    assert _by_day(store.daily_usage(days=None, since=since, tz=_IST)) == [("2026-09-18", 2)]
    assert _by_day(store.daily_usage(days=None, since=since, tz="UTC")) == [("2026-09-17", 1), ("2026-09-18", 2)]
    shown = store.daily_usage(days=None, since=since, tz=_IST)
    assert min(row["day"] for row in shown) >= "2026-09-18"


def test_daily_usage_legacy_rows_never_add_a_day_after_the_last_local_day(store: Store) -> None:
    _seed_agg(store, _agg(None, day="2026-09-18", turns=1), _agg(None, day="2026-09-19", turns=2))
    until = "2026-09-19T05:00:00Z"  # 21:00 on the 18th in California
    assert _by_day(store.daily_usage(days=None, until=until, tz=_PST)) == [("2026-09-18", 1)]
    assert _by_day(store.daily_usage(days=None, until=until, tz="UTC")) == [("2026-09-18", 1), ("2026-09-19", 2)]


def test_daily_usage_legacy_rows_are_kept_whole_inside_the_window(store: Store) -> None:
    _seed_agg(store, _agg(None, day="2026-09-18", turns=3))
    assert _by_day(store.daily_usage(days=None, since="2026-09-18T00:00:00Z", until="2026-09-18T23:59:00Z")) == [
        ("2026-09-18", 3)
    ]
    assert _by_day(store.daily_usage(days=None, since="2026-09-18T12:00:00Z")) == [("2026-09-18", 3)]
    assert store.daily_usage(days=None, since="2026-09-19T00:00:00Z") == []
    assert store.daily_usage(days=None, until="2026-09-17T23:59:00Z") == []


def test_daily_usage_leaves_out_a_turn_with_no_timestamp_unless_the_window_is_open(store: Store) -> None:
    _seed_agg(store, _agg(None, day="unknown", turns=1), _agg("2026-09-18T12:00:00Z", turns=2))
    assert _by_day(store.daily_usage(days=None)) == [("2026-09-18", 2), ("unknown", 1)]
    assert _by_day(store.daily_usage(days=None, since="2026-09-01T00:00:00Z")) == [("2026-09-18", 2)]
    assert _by_day(store.daily_usage(days=None, until="2026-09-30T00:00:00Z")) == [("2026-09-18", 2)]
    assert _by_day(store.daily_usage(days=None, since="2026-09-01T00:00:00Z", until="2026-09-30T00:00:00Z")) == [
        ("2026-09-18", 2)
    ]


def test_daily_usage_legacy_rows_after_today_are_left_out_of_a_window(store: Store) -> None:
    _seed_agg(store, _agg(None, day="2026-09-18", turns=1), _agg(None, day="2099-01-01", turns=2))
    assert _by_day(store.daily_usage(days=None, since="2026-09-01T00:00:00Z")) == [("2026-09-18", 1)]
    assert _by_day(store.daily_usage(days=None)) == [("2026-09-18", 1), ("2099-01-01", 2)]


def test_daily_usage_puts_bucket_and_legacy_rows_of_one_day_in_one_row(store: Store) -> None:
    _seed_agg(store, _agg(None, day="2026-09-18", turns=1), session_id="old")
    _seed_agg(store, _agg("2026-09-18T10:00:00Z", turns=2), _agg("2026-09-18T10:15:00Z", turns=4), session_id="new")
    [row] = store.daily_usage(days=None)
    assert row["day"] == "2026-09-18" and row["turns"] == 7


def test_daily_usage_row_shape_and_order_are_unchanged(store: Store) -> None:
    other = "claude-opus-5"
    _seed_agg(
        store,
        _agg("2026-09-19T00:15:00Z", model=other, turns=1, cost=0.1),
        _agg("2026-09-18T23:45:00Z", model=_MODEL, turns=2, cost=0.2),
        _agg("2026-09-18T23:30:00Z", model=_MODEL, turns=3, cost=0.4),
        _agg("2026-09-18T12:00:00Z", model=other, turns=4, cost=0.8),
    )
    _seed_agg(
        store,
        _agg("2026-09-18T23:45:00Z", model=_MODEL, turns=5, cost=1.6),
        session_id="session-bk",
        kind="subagent",
        path="/x/session-bk-sub.jsonl",
    )
    plain = store.daily_usage(days=None)
    assert [list(row) for row in plain] == [["day", "model", *_COLUMNS]] * len(plain)
    assert [(row["day"], row["model"]) for row in plain] == [
        ("2026-09-18", other), ("2026-09-18", _MODEL), ("2026-09-19", other),
    ]
    assert plain[1]["turns"] == 10 and plain[1]["cost"] == pytest.approx(2.2)
    assert isinstance(plain[1]["turns"], int) and isinstance(plain[1]["cost"], float)
    assert store.daily_usage(days=None, split="model") == plain

    by_agent = store.daily_usage(days=None, split="agent")
    assert [list(row) for row in by_agent] == [["day", "agent", "model", *_COLUMNS]] * len(by_agent)
    assert [(row["day"], row["agent"], row["model"]) for row in by_agent] == [
        ("2026-09-18", "main", other),
        ("2026-09-18", "main", _MODEL),
        ("2026-09-18", "subagent", _MODEL),
        ("2026-09-19", "main", other),
    ]
    assert sum(row["turns"] for row in by_agent) == sum(row["turns"] for row in plain)
    assert sum(row["cost"] for row in by_agent) == pytest.approx(sum(row["cost"] for row in plain))


def test_daily_usage_split_by_agent_regroups_to_local_days(store: Store) -> None:
    _seed_agg(store, _agg("2026-09-18T20:00:00Z", turns=1), _agg("2026-09-19T01:00:00Z", turns=2))
    _seed_agg(
        store,
        _agg("2026-09-18T23:45:00Z", turns=4),
        session_id="session-bk",
        kind="subagent",
        path="/x/session-bk-sub.jsonl",
    )
    rows = store.daily_usage(days=None, split="agent", tz=_IST)
    assert [(row["day"], row["agent"], row["turns"]) for row in rows] == [
        ("2026-09-19", "main", 3),
        ("2026-09-19", "subagent", 4),
    ]


def test_daily_usage_first_reply_window_still_counts_only_the_sessions_started_in_it(store: Store) -> None:
    _seed_agg(
        store, _agg("2026-09-18T12:00:00Z", turns=5), session_id="old",
        first_ts="2026-09-10T00:00:00Z", last_ts="2026-09-19T00:00:00Z",
    )
    _seed_agg(
        store, _agg("2026-09-18T12:00:00Z", turns=2), session_id="new",
        first_ts="2026-09-18T11:00:00Z", last_ts="2026-09-18T13:00:00Z",
    )
    since = "2026-09-18T00:00:00Z"
    assert _by_day(store.daily_usage(days=None, since=since)) == [("2026-09-18", 7)]
    assert _by_day(store.daily_usage(days=None, since=since, window_by="first-reply")) == [("2026-09-18", 2)]
    assert _by_day(store.daily_usage(days=None, since=since, window_by="first-reply", tz=_IST)) == [("2026-09-18", 2)]


def test_daily_usage_regroups_by_local_day_for_a_project(store: Store) -> None:
    _seed_agg(store, _agg("2026-09-18T20:00:00Z", turns=1), _agg("2026-09-19T01:00:00Z", turns=2), slug="proj-a", session_id="a")
    _seed_agg(store, _agg("2026-09-18T20:00:00Z", turns=8), slug="proj-b", session_id="b")
    assert _by_day(store.daily_usage(days=None, project_slugs=["proj-a"], tz=_IST)) == [("2026-09-19", 3)]
    assert _by_day(store.daily_usage(days=None, project_slugs=["proj-b"], tz=_IST)) == [("2026-09-19", 8)]
    assert _by_day(store.daily_usage(days=None, project_slugs=["proj-a"])) == [("2026-09-18", 1), ("2026-09-19", 2)]
    assert _by_day(store.daily_usage(days=None, split="agent", project_slugs=["proj-a"], tz=_IST)) == [("2026-09-19", 3)]


def test_daily_usage_zone_that_cannot_be_resolved_is_the_machine_zone(store: Store) -> None:
    _seed_agg(store, _agg("2026-09-18T23:45:00Z", turns=2), _agg("2026-09-19T00:15:00Z", turns=3))
    assert store.daily_usage(days=None, tz="Mars/Olympus") == store.daily_usage(days=None, tz=None)
    assert store.daily_usage(days=None, tz="") == store.daily_usage(days=None, tz=None)


@pytest.mark.skipif(not _tzdata_has("Asia/Kolkata"), reason="no tzdata for Asia/Kolkata on this machine")
def test_daily_usage_takes_an_iana_zone_name(store: Store) -> None:
    _seed_agg(store, _agg("2026-09-18T23:45:00Z", turns=2), _agg("2026-09-19T00:15:00Z", turns=3))
    assert store.daily_usage(days=None, tz="Asia/Kolkata") == store.daily_usage(days=None, tz=_IST)


def test_a_new_store_has_the_bucket_column_and_no_index_on_it(store: Store) -> None:
    conn = store._connection()
    assert "bucket" in {row["name"] for row in conn.execute("PRAGMA table_info(turns_agg)")}
    indexed = {row["name"] for row in conn.execute("PRAGMA index_list(turns_agg)")}
    assert "idx_turns_agg_day" in indexed and not any("bucket" in name for name in indexed)


def test_upsert_transcript_stores_the_bucket_or_null(store: Store) -> None:
    _seed_agg(store, _agg("2026-09-18T12:15:00Z", turns=1), _agg(None, day="2026-09-18", turns=2, model="other"))
    no_key = _agg(None, day="2026-09-18", turns=3, model="third")
    del no_key["bucket"]  # as every row in ``_seed`` is written
    store.upsert_transcript(
        session_id="session-bk", path="/x/other.jsonl", kind="top-level", digest_json="{}", turns_agg=[no_key]
    )
    rows = store._connection().execute("SELECT day, bucket, model FROM turns_agg ORDER BY id").fetchall()
    assert [(row["day"], row["bucket"], row["model"]) for row in rows] == [
        ("2026-09-18", "2026-09-18T12:15:00Z", _MODEL),
        ("2026-09-18", None, "other"),
        ("2026-09-18", None, "third"),
    ]
    # A re-parse replaces the rows wholesale, buckets included.
    _seed_agg(store, _agg("2026-09-18T12:30:00Z", turns=1))
    buckets = [
        row["bucket"]
        for row in store._connection().execute(
            "SELECT bucket FROM turns_agg a JOIN transcripts t ON t.id = a.transcript_id WHERE t.path = '/x/session-bk-top-level.jsonl'"
        )
    ]
    assert buckets == ["2026-09-18T12:30:00Z"]


def test_seeded_rows_without_a_bucket_read_as_before(store: Store) -> None:
    _seed(store)
    assert [row["bucket"] for row in store._connection().execute("SELECT bucket FROM turns_agg")] == [None, None]
    [row] = store.daily_usage(days=None)
    assert row["day"] == "2026-09-18" and row["turns"] == 15


def test_bucket_start_floors_to_the_utc_quarter_hour() -> None:
    from claudeglass.service.store import bucket_start

    assert bucket_start(datetime(2026, 9, 18, 12, 7, 59, tzinfo=timezone.utc)) == "2026-09-18T12:00:00Z"
    assert bucket_start(datetime(2026, 9, 18, 12, 15, tzinfo=timezone.utc)) == "2026-09-18T12:15:00Z"
    assert bucket_start(datetime(2026, 9, 18, 12, 59, 59, 999999, tzinfo=timezone.utc)) == "2026-09-18T12:45:00Z"
    assert bucket_start(datetime(2026, 9, 18, 0, 0, tzinfo=timezone.utc)) == "2026-09-18T00:00:00Z"
    # An offset is converted, and a time with none is read as UTC.
    assert bucket_start(datetime(2026, 9, 19, 5, 20, tzinfo=_IST)) == "2026-09-18T23:45:00Z"
    assert bucket_start(datetime(2026, 9, 18, 23, 50)) == "2026-09-18T23:45:00Z"


# -- the v7 -> v8 migration ----------------------------------------------------

#: ``turns_agg`` as schema 7 declared it: no ``bucket``.
_V7_TURNS_AGG = """
CREATE TABLE turns_agg (
    id                     INTEGER PRIMARY KEY,
    transcript_id          INTEGER NOT NULL REFERENCES transcripts(id),
    day                    TEXT NOT NULL,
    model                  TEXT NOT NULL,
    turns                  INTEGER NOT NULL DEFAULT 0,
    input_tokens           INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens      INTEGER NOT NULL DEFAULT 0,
    output_tokens          INTEGER NOT NULL DEFAULT 0,
    thinking_tokens        INTEGER NOT NULL DEFAULT 0,
    cc_5m                  INTEGER NOT NULL DEFAULT 0,
    cc_1h                  INTEGER NOT NULL DEFAULT 0,
    cost                   REAL NOT NULL DEFAULT 0
)
"""
_V7_COLUMNS = "id, transcript_id, day, model, turns, input_tokens, cache_creation_tokens, cache_read_tokens, output_tokens, thinking_tokens, cc_5m, cc_1h, cost"


def _make_v7(store: Store) -> None:
    """Turn an open store into a schema-7 one: ``turns_agg`` as it was
    before ``bucket`` (every row kept), stamped version 7."""
    conn = store._connection()
    conn.execute("ALTER TABLE turns_agg RENAME TO turns_agg_v8")
    conn.execute(_V7_TURNS_AGG)
    conn.execute(f"INSERT INTO turns_agg ({_V7_COLUMNS}) SELECT {_V7_COLUMNS} FROM turns_agg_v8")
    conn.execute("DROP TABLE turns_agg_v8")
    conn.execute("CREATE INDEX idx_turns_agg_day ON turns_agg(day)")
    conn.execute("UPDATE meta SET value = '7' WHERE key = 'schema_version'")


def _agg_rows(store: Store) -> list[tuple]:
    return [
        tuple(row)
        for row in store._connection().execute("SELECT id, transcript_id, day, model, turns, cost FROM turns_agg ORDER BY id")
    ]


def _parser_versions(store: Store) -> dict[str, int]:
    return {path: version for path, (_mtime, _size, version) in store.known_files().items()}


def test_migrate_upgrades_a_v7_store_and_marks_every_transcript_for_a_re_parse(tmp_path) -> None:
    from claudeglass import PARSER_VERSION
    from claudeglass.service import schema

    db_path = tmp_path / "v7.db"
    store = Store(str(db_path))
    store.open()
    _seed(store)  # two transcripts at parser version 3, rows with no bucket
    _seed_agg(store, _agg("2026-09-18T12:00:00Z", turns=2), session_id="now", path="/x/now.jsonl", parser_version=PARSER_VERSION)
    _seed_agg(store, _agg("2026-09-18T12:15:00Z", turns=4), session_id="later", path="/x/later.jsonl", parser_version=PARSER_VERSION + 1)
    rows_before = _agg_rows(store)
    assert len(rows_before) == 4
    _make_v7(store)
    assert "bucket" not in {row["name"] for row in store._connection().execute("PRAGMA table_info(turns_agg)")}
    store.close()

    upgraded = Store(str(db_path))
    upgraded.open()
    try:
        assert upgraded.schema_version() == schema.SCHEMA_VERSION == 8
        assert "bucket" in {row["name"] for row in upgraded._connection().execute("PRAGMA table_info(turns_agg)")}
        # Every row is kept, with no bucket until its transcript is parsed again.
        assert _agg_rows(upgraded) == rows_before
        assert [row["bucket"] for row in upgraded._connection().execute("SELECT bucket FROM turns_agg")] == [None] * 4
        # Every transcript a build this old or this new parsed is marked to be
        # parsed again; one a newer build wrote is left alone.
        assert _parser_versions(upgraded) == {
            _FAKE_PATH: 0,
            _FAKE_SUB_PATH: 0,
            "/x/now.jsonl": 0,
            "/x/later.jsonl": PARSER_VERSION + 1,
        }
        # Until then every read still works, by the rows' UTC days.
        assert _by_day(upgraded.daily_usage(days=None)) == [("2026-09-18", 21)]
    finally:
        upgraded.close()


def test_migrate_7_to_8_is_a_no_op_when_run_again(store: Store) -> None:
    from claudeglass import PARSER_VERSION
    from claudeglass.service import store as store_mod

    _seed(store)
    _seed_agg(store, _agg("2026-09-18T12:15:00Z"), session_id="later", path="/x/later.jsonl", parser_version=PARSER_VERSION + 1)
    conn = store._connection()
    store_mod._migrate_7_to_8(conn)
    columns = [row["name"] for row in conn.execute("PRAGMA table_info(turns_agg)")]
    versions, rows = _parser_versions(store), _agg_rows(store)
    assert versions[_FAKE_PATH] == 0 and versions["/x/later.jsonl"] == PARSER_VERSION + 1

    store_mod._migrate_7_to_8(conn)  # the column is there already, nothing is left to mark
    assert [row["name"] for row in conn.execute("PRAGMA table_info(turns_agg)")] == columns
    assert _parser_versions(store) == versions
    assert _agg_rows(store) == rows


def test_migrating_a_store_that_is_already_current_marks_nothing(store: Store) -> None:
    _seed(store)
    store.migrate()
    store.migrate()
    assert set(_parser_versions(store).values()) == {3}


def test_the_migration_ladder_reaches_the_current_version() -> None:
    from claudeglass.service import schema
    from claudeglass.service import store as store_mod

    assert set(store_mod.MIGRATIONS) == set(range(4, schema.SCHEMA_VERSION))
    assert store_mod.MIGRATIONS[7] is store_mod._migrate_7_to_8


def test_a_windowed_read_of_turns_agg_uses_the_day_index(store: Store) -> None:
    from claudeglass.service.store import _turns_agg_window

    conn = store._connection()
    for since, until in [
        (datetime(2026, 9, 18, 18, 30, tzinfo=timezone.utc), datetime(2026, 9, 19, 18, 30, tzinfo=timezone.utc)),
        (datetime(2026, 9, 18, 18, 30, tzinfo=timezone.utc), None),
        (None, datetime(2026, 9, 19, 18, 30, tzinfo=timezone.utc)),
    ]:
        condition, params = _turns_agg_window(since, until, _IST)
        plan = " ".join(
            row["detail"] for row in conn.execute(f"EXPLAIN QUERY PLAN SELECT a.id FROM turns_agg a WHERE {condition}", params)
        )
        assert "idx_turns_agg_day" in plan, plan

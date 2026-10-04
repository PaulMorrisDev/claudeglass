"""SQLite schema for the v0.2 local read-only service (``serve``).

This module is pure data: SQL ``CREATE TABLE``/``CREATE INDEX`` strings
plus :data:`SCHEMA_VERSION`. It has no behaviour of its own —
``service/store.py``'s :class:`~claudeglass.service.store.Store`
is the only thing that executes these statements. Keeping the schema
separate from the code that runs it lets a migration (a future
``SCHEMA_VERSION`` bump) diff cleanly against this file's history.

:data:`SCHEMA_VERSION` is independent of the top-level
``claudeglass.SCHEMA_VERSION`` (the frozen ``model.py`` dataclass
contract that gates the on-disk digest *cache*) and of ``PARSER_VERSION``
(``parse.py``'s parsing-logic version). This one versions the SQLite
*store*'s own table shapes. A mismatch between the version recorded in
the ``meta`` table and this module's :data:`SCHEMA_VERSION` means the
store's tables are stale relative to the code that opened them;
``Store.migrate`` (``service/store.py``) walks an additive
``MIGRATIONS`` ladder -- ``ALTER TABLE``/``CREATE INDEX`` only, existing
rows kept -- for a recorded version it has a registered step for, and
falls back to "drop and rebuild" (backed up first, see
``Store._backup_before_rebuild``) only for the two cases a ladder step
can't serve: a recorded version newer than the running code's own, or
an older one with no registered step. Most of the store is a derived
cache over transcripts on disk, never the source of truth, so losing it
to a rebuild is safe -- the next watcher tick repopulates it. The two
tables that aren't re-derivable this way, ``session_tags``,
``session_feedback``, ``session_plan_feedback`` and ``tip_feedback`` (your
own tags and ratings from the dashboard), are read before a
drop-and-rebuild and written straight back once the tables are recreated
(ROB-P6, ``Store._export_marks``/``_reimport_marks``), so a rebuild never
silently erases them either.

Privacy rule (binding on every table below, restated from ``model.py``'s
own module docstring and enforced here for the store specifically): no
column may ever hold message text, a path *found inside a transcript*
(a file a tool read/wrote, a working directory mentioned in output), or
a shell command. The one narrow exception is ``transcripts.path`` (and
``projects.root_path``, ``profiles.toml_path``) — the local filesystem
location of a file *this machine's own store* needs to reopen it (to
re-parse on change, to know a project's scan root, to load a profile
overlay). Those columns exist for the watcher's own bookkeeping only.
The API layer (``service/contracts.py``'s ``ApiHandler``, and every
``/api/*`` route documented in ``docs/api.md``) must never select or
forward them — ``Store``'s own read queries (``summary``, ``sessions``,
``session``, ``daily_usage``, ``recache``, ``compactions``,
``snapshots``, ``tags``) are written to leave those columns out of their
result dicts entirely, and ``tests/test_service_store.py`` asserts by
construction (a distinctive fake path round-tripped through every read
query) that none of them ever surface it.

``digest_json``/``digest_blob`` columns (``transcripts.digest_blob``,
``snapshots.digest_json``, ``baselines.digest_json``) hold the output of
``cache.encode_result`` (or the equivalent flattened/redacted encoding
for snapshots/baselines) — numeric digests and short enum-like strings
only, already subject to ``model.py``'s own no-message-text contract
before it ever reaches this schema. ``transcripts.digest_blob`` (v4)
holds that same JSON zlib-compressed rather than as plain text; see the
"Version 4" paragraph below.

Version 2 (S1-integration): ``snapshots`` gains a ``(project_id, ts,
schema_version)`` unique key so ``Store.upsert_snapshot`` can dedupe via
``ON CONFLICT`` the same way every other ``upsert_*`` method already
does, retiring the watcher's own pre-check-and-skip workaround; and a new
``workflow_runs`` table stores ``<session>/workflows/wf_*.json`` run data
(``workflows.py``'s ``WorkflowRun``, minus ``phase_titles``' sibling
``detail`` text) so ``service/rebuild.py`` can read it back into
``SessionBundle.workflows`` instead of always reporting an empty list.

Version 3 (review finding 3, "the store must outlive Claude Code's own
``cleanupPeriodDays``"): ``transcripts`` gains a nullable
``missing_since`` timestamp column. A transcript whose file the watcher
can no longer find on disk is no longer deleted on the spot — it is
marked with ``missing_since`` instead (cleared again if the file
reappears), so its stored ``digest_json`` keeps serving reports and
``service/rebuild.py`` round trips until the row is actually removed by
``Store.retention_prune`` or ``claudeglass serve --purge``. See
``Store.remove_missing``'s own docstring for the exact mechanics.

A store opened against an older ``schema_version`` is dropped and
rebuilt from scratch (see ``Store.migrate``) — the next watcher tick
repopulates it, since ``known_files()`` is empty again.

Version 4 (S1-perf): a real corpus's ``events`` table dwarfed every
other table combined (231k+ rows for under 2k transcripts, one row per
structural event with no per-row reader anywhere in this codebase --
``store.py`` has no ``events()`` read method and neither ``api.py`` nor
``rebuild.py`` ever queries it) purely to let ``Store`` insert then
immediately discard per-event detail no consumer wanted. Renamed to
``events_agg`` and collapsed to one row per ``(transcript_id, kind,
subkind)`` with ``count``/``dropped_tokens_sum``/``duration_ms_sum``
instead of one row per event -- ``recache_turns`` is untouched because
``Store.recache`` genuinely reads it per-row. Separately,
``transcripts.digest_json`` (a full per-transcript JSON blob needed
verbatim by ``service/rebuild.py``'s round trip, and by far the
store's largest single column) is now stored zlib-compressed as
``transcripts.digest_blob`` -- ``snapshots.digest_json`` and
``baselines.digest_json`` are untouched (both empty in every real
corpus observed; compressing a column nothing populates buys nothing).
See ``Store.encode_digest_blob``/``decode_digest_blob``.

Version 5 (v0.3 baseline/profile ingestion): the watcher now ingests
``<config_dir>/baselines/*.json`` and ``<config_dir>/profiles/*.toml``
into ``baselines``/``profiles`` on every tick (see ``watcher.py``'s
``_scan_baselines``/``_scan_profiles``), content-hash deduped so a
repeat tick over an unchanged file is a no-op write. ``profiles`` gains
``content_hash`` (the ingested file's own text, hashed); ``baselines``
gains ``record_id`` (the baseline JSON record's own ``id`` field --
its natural, stable identity, distinct from this table's unrelated
autoincrement ``id`` primary key) and ``content_hash``. Both default to
``''``/``NULL`` for a row from before this version, which the
drop-and-rebuild-on-version-mismatch policy above makes moot in
practice: a store opened under an older recorded ``schema_version`` is
dropped and recreated from scratch before any row like that could
exist.

Version 6 (metrics capture feedback): a new ``session_feedback`` table
holds the ratings you give a session on the dashboard's Sessions tab
(``POST /api/sessions/<id>/feedback``): the four questions the
``/cg-feedback`` skill asked until its redesign, as words from
``capture_catalogue.RATING_VOCAB`` (``slow`` and ``helped`` comma-joined),
never free text. A v5 store gains the table in place
(``store.MIGRATIONS[5]``); Version 9 below adds the rest.

Version 7 (EST-P5: predictions and back-testing): a new ``predictions``
table holds a whatif estimate worth checking against what actually
happened -- ``id``/``source``/``measure_key``/``agent``/``fidelity``/
``verdict`` are short enum-like strings (never free text), the rest
numbers or timestamps, ingested from ``<config_dir>/prediction-log.jsonl``
(``watcher._scan_predictions``, mirroring how ``baselines``/``profiles``
ingest their own on-disk files) the same way ``change_points.py`` reads
``capture-log.jsonl``. ``seen_at`` is set by ``POST
/api/predictions/seen`` (the dashboard has shown it to you once);
``judged_at``/``verdict``/``measured_usd``/``measured_pct``/``change_ts``
are filled in once ``backtest.py`` (via ``GET /api/backtest``) matches
the prediction to a real change point with enough sessions on both
sides to compare, and persisted so the match/verdict isn't reworked out
from scratch on every call -- the same before-computed-then-cached
posture ``Store.judge_prediction`` shares with ``impact_cache`` in
``service/api.py``. A v6 store gains the table in place
(``store.MIGRATIONS[6]``); ``Store.prune_predictions`` (EST-P5's 90/400
day windows) deletes a still-unmatched prediction after 90 days (it was
never applied, or nothing traced it) and a judged one after 400 days
(kept longer so EST-P6's calibration has a real history to learn from).

Version 8 (local calendar-day windows): ``turns_agg`` gains a nullable
``bucket`` column, the UTC quarter hour a turn's reply falls in
(``%Y-%m-%dT%H:%M:00Z``, minute 00, 15, 30 or 45), and a row is now one
per ``(transcript, day, bucket, model)``. ``day`` stays the turn's UTC
date, which is always the first ten characters of its ``bucket``. The
store knows no time zone: ``Store.daily_usage`` groups buckets into local
days when it reads. A quarter hour is the width that keeps every local
midnight on a bucket edge, since every time zone's offset is a whole
number of quarter hours (``+05:30``, ``+05:45``, ``+08:45``, ``-03:30``).
``bucket`` is NULL for a row written before v8 and for a turn with no
timestamp (its ``day`` is ``unknown``). A v7 store gains the column in
place (``store.MIGRATIONS[7]``) and marks every transcript for a re-parse
(``Store.demote_parsed``), which fills it in; until then
``daily_usage`` reads a NULL-bucket row by its UTC ``day``, and a
transcript the watcher can't re-parse (its file is gone) keeps it. There
is no index on ``bucket``: ``ALL_STATEMENTS`` runs before the ladder, so
one declared here would fail against a v7 table, and the table is small
enough (``idx_turns_agg_day`` narrows a windowed read first).

Version 9 (the /cg-feedback redesign on the dashboard): the rating takes
the same questions the skill asks (``capture_catalogue.RATING_QUESTIONS``).
``session_feedback`` gains ``why`` (a comma list, like ``helped``),
``missed_in``, ``tip`` and ``tip_hint``, all words from
``capture_catalogue.RATING_VOCAB``. A new ``session_plan_feedback`` table
holds the plan and handoff answers once for each approved plan in a session
(``build`` counts them from 1), because a session with two plans has two
answers. A new ``tip_feedback`` table holds what you said about a tip,
habit or recommendation card (``kind`` and ``item`` name the card from a
closed list, ``answer`` is a word from ``capture_catalogue.TIP_CARD_VOCAB``),
one row per card. Still words and ids only, never free text. A v8 store
gains the columns and tables in place (``store.MIGRATIONS[8]``).
"""

from __future__ import annotations

#: Bump when a table or index below changes shape. See module docstring.
SCHEMA_VERSION = 9

CREATE_META = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

#: One row per discovered project directory (``~/.claude/projects/<slug>``).
#: ``root_path`` is the local scan root the watcher polls — never
#: API-returned (see module docstring).
CREATE_PROJECTS = """
CREATE TABLE IF NOT EXISTS projects (
    id         INTEGER PRIMARY KEY,
    slug       TEXT NOT NULL UNIQUE,
    root_path  TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL
);
"""

#: One row per top-level session (``SessionRecord``). Cost/token totals
#: are folded in at upsert time from the session's own transcripts so
#: reads never have to re-aggregate ``turns_agg`` for a session listing.
CREATE_SESSIONS = """
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
"""

#: One row per parsed transcript file (top-level, subagent or
#: workflow-agent). ``path`` is local-store-only (see module docstring).
#: ``missing_since`` (v3) is NULL while the watcher can still find the
#: file on disk; set to the timestamp the watcher first noticed it gone,
#: cleared again if it reappears. A missing transcript's row (and its
#: stored ``digest_blob``) is kept, not deleted -- see
#: ``Store.remove_missing``. ``digest_blob`` (v4, renamed from
#: ``digest_json``) is the same JSON payload zlib-compressed -- see
#: ``Store.encode_digest_blob``/``decode_digest_blob``.
CREATE_TRANSCRIPTS = """
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
"""

#: Per-transcript, per-quarter-hour, per-model rollups (``day`` is the UTC
#: date, ``bucket`` the UTC quarter hour, see the module docstring's
#: "Version 8") — the basis for ``Store.daily_usage`` and the Usage report
#: section without re-reading every turn on each request.
CREATE_TURNS_AGG = """
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
    cost                   REAL NOT NULL DEFAULT 0,
    bucket                 TEXT
);
CREATE INDEX IF NOT EXISTS idx_turns_agg_day ON turns_agg(day);
"""

#: One row per detected RE-CACHE turn (``Turn.is_recache``), feeding
#: ``Store.recache``.
CREATE_RECACHE_TURNS = """
CREATE TABLE IF NOT EXISTS recache_turns (
    id                     INTEGER PRIMARY KEY,
    transcript_id          INTEGER NOT NULL REFERENCES transcripts(id),
    turn_index             INTEGER NOT NULL,
    signature              TEXT NOT NULL,
    cache_creation_tokens  INTEGER NOT NULL DEFAULT 0,
    preceding_primary      TEXT,
    gap_s                  REAL
);
"""

#: Non-priced structural events (``Event``), content-free (kind/subkind
#: only — never the attachment/tool content itself). v4 (S1-perf):
#: aggregated to one row per ``(transcript_id, kind, subkind)`` rather
#: than one row per event -- see the module docstring's "Version 4"
#: paragraph for why (no reader anywhere used per-row event data).
#: ``count`` is the number of events folded into this row;
#: ``dropped_tokens_sum``/``duration_ms_sum`` are the sum of each
#: event's own (possibly ``NULL``, treated as 0) field. The ``UNIQUE``
#: constraint is informational -- ``Store.upsert_transcript`` builds
#: already-distinct aggregates in Python before inserting, so it's
#: never relied on via ``ON CONFLICT``.
CREATE_EVENTS = """
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
"""

#: One row per ``COMPACT_BOUNDARY`` event, feeding ``Store.compactions``.
CREATE_COMPACTIONS = """
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
"""

#: One row per captured config snapshot: the ``Snapshot``'s own document
#: (``Snapshot.data``, already redacted by the hook that wrote it). The
#: natural key is ``(project_id, ts, schema_version)`` (v2) -- the same
#: snapshot file re-ingested on a later watcher tick updates its own row
#: via ``Store.upsert_snapshot``'s ``ON CONFLICT`` rather than growing a
#: new one each tick.
CREATE_SNAPSHOTS = """
CREATE TABLE IF NOT EXISTS snapshots (
    id             INTEGER PRIMARY KEY,
    project_id     INTEGER REFERENCES projects(id),
    ts             TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    digest_json    TEXT NOT NULL,
    UNIQUE (project_id, ts, schema_version)
);
"""

#: User-set ``mode``/``purpose`` overrides (mirrors ``config.
#: sessions.toml``, but keyed for fast API reads/writes instead of a
#: TOML round trip on every request).
CREATE_SESSION_TAGS = """
CREATE TABLE IF NOT EXISTS session_tags (
    session_id TEXT NOT NULL REFERENCES sessions(id),
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    set_at     TEXT NOT NULL,
    PRIMARY KEY (session_id, key)
);
"""

#: Your rating of a session from the Sessions tab (v6): one row per
#: session, words only (see module docstring). ``why``, ``missed_in``,
#: ``tip`` and ``tip_hint`` came with v9.
CREATE_SESSION_FEEDBACK = """
CREATE TABLE IF NOT EXISTS session_feedback (
    session_id TEXT PRIMARY KEY REFERENCES sessions(id),
    outcome    TEXT,
    slow       TEXT NOT NULL DEFAULT '',
    worth      TEXT,
    helped     TEXT NOT NULL DEFAULT '',
    set_at     TEXT NOT NULL,
    why        TEXT NOT NULL DEFAULT '',
    missed_in  TEXT,
    tip        TEXT,
    tip_hint   TEXT
);
"""

#: What you said about each plan build of a session (v9): the plan and
#: handoff answers, one row per approved plan, counted from 1 in the order
#: they were approved. Words only.
CREATE_SESSION_PLAN_FEEDBACK = """
CREATE TABLE IF NOT EXISTS session_plan_feedback (
    session_id TEXT NOT NULL REFERENCES sessions(id),
    build      INTEGER NOT NULL,
    plan       TEXT,
    handoff    TEXT,
    set_at     TEXT NOT NULL,
    PRIMARY KEY (session_id, build)
);
"""

#: What you said about a tip, habit or recommendation card (v9): one row
#: per card. ``kind`` is one of ``capture_catalogue.TIP_CARD_KINDS``,
#: ``item`` the card's id (a habit or hint id, or a recommendation rule
#: id) and ``answer`` one of ``capture_catalogue.TIP_CARD_VOCAB``. No
#: session id: it is about the card, not the work.
CREATE_TIP_FEEDBACK = """
CREATE TABLE IF NOT EXISTS tip_feedback (
    kind   TEXT NOT NULL,
    item   TEXT NOT NULL,
    answer TEXT NOT NULL,
    set_at TEXT NOT NULL,
    PRIMARY KEY (kind, item)
);
"""

#: Index of *user* profile files on disk (v0.3's ``<config_dir>/
#: profiles/<id>.toml``, tracked here from v0.2 so the service can
#: list/diff them). ``toml_path`` is local-store-only (see module
#: docstring). Catalogue profiles (``profiles.catalogue.CATALOGUE_IDS``)
#: are never rows here -- they are shipped, static package data with no
#: on-disk mtime/content of the user's own to track, so ``/api/profiles``
#: (``service/api.py``) merges them in at query time instead, tagging
#: each with its own ``source``. ``content_hash`` (v5) is the ingested
#: file's own text, hashed -- ``watcher._scan_profiles`` compares it
#: against the stored value before writing, so a repeat tick over an
#: unchanged file is a no-op (no ``updated_at`` churn).
CREATE_PROFILES = """
CREATE TABLE IF NOT EXISTS profiles (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    toml_path     TEXT NOT NULL,
    content_hash  TEXT NOT NULL DEFAULT '',
    updated_at    TEXT NOT NULL
);
"""

#: One row per captured baseline window (``claudeglass baseline``,
#: ingested from ``<config_dir>/baselines/*.json`` by
#: ``watcher._scan_baselines``). ``record_id`` (v5) is the baseline JSON
#: record's own ``id`` field (``baseline.py``'s ``uuid.uuid4().hex[:12]``)
#: -- a stable natural key distinct from this table's own unrelated
#: autoincrement ``id`` -- so re-ingesting the same (immutable) baseline
#: file on a later tick updates its existing row via
#: ``Store.record_baseline``'s own ``content_hash`` comparison rather
#: than growing a duplicate one. ``NULL``/non-unique for a hand-inserted
#: test row that predates ingestion (see module docstring's "Version 5"
#: paragraph) -- SQLite's ``UNIQUE`` never treats two ``NULL``s as
#: conflicting, so that stays safe.
CREATE_BASELINES = """
CREATE TABLE IF NOT EXISTS baselines (
    id           INTEGER PRIMARY KEY,
    project_id   INTEGER REFERENCES projects(id),
    window_start TEXT NOT NULL,
    window_end   TEXT NOT NULL,
    archetype    TEXT,
    digest_json  TEXT NOT NULL,
    record_id    TEXT UNIQUE,
    content_hash TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);
"""

#: One row per ``<session>/workflows/wf_*.json`` run (v2, S1-integration),
#: written by the watcher via ``workflows.parse_workflow_file``/
#: ``link_workflow_agents`` and read back by ``service/rebuild.py`` into
#: ``SessionBundle.workflows``. ``phases`` is a JSON array of phase
#: *names only* (``WorkflowRun.phase_titles`` -- never ``detail``, which
#: carries workflow source/prompt text, see ``workflows.py``'s module
#: docstring); a rebuilt ``WorkflowRun.phases`` count is therefore
#: ``len(phase_titles)``, which can undercount a fresh parse's own
#: ``phases`` if any phase entry in the original file lacked a ``title``
#: (documented deviation, not fixed here -- see ``service/rebuild.py``).
#: ``status`` is stored even though it isn't named in the work package's
#: column list, because without it a rebuilt ``WorkflowRun`` can't match
#: a fresh parse's ``build_section`` status-mix table byte-for-byte.
CREATE_WORKFLOW_RUNS = """
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
"""

#: Raw ``get_usage`` snapshots logged by ``log-usage`` (5-hour/weekly
#: window utilisation), kept verbatim as JSON for the Usage tab's
#: window-overlay view.
CREATE_USAGE_LOG = """
CREATE TABLE IF NOT EXISTS usage_log (
    id              INTEGER PRIMARY KEY,
    ts              TEXT NOT NULL,
    window_start    TEXT,
    window_end      TEXT,
    utilization_pct REAL,
    raw_json        TEXT NOT NULL
);
"""

#: A whatif estimate worth checking against what actually happened
#: (v7, EST-P5). ``id`` is the prediction-log record's own id (stable,
#: from ``config.append_prediction_log``) -- re-ingesting the same log
#: line on a later watcher tick is a no-op via ``INSERT OR IGNORE``,
#: since a prediction is immutable once logged. ``measure_key`` is the
#: raw settings/agent-lever key the whatif estimate was for (``"model"``,
#: ``"promptCacheTtl"``, ...), not yet translated to an ``impact.py``
#: measure -- ``backtest.py`` does that translation once, against a real
#: change point's own keys, the same way ``impact.measures_for`` already
#: does for a ``ChangePoint``. See the module docstring's "Version 7"
#: paragraph for the read-then-judge lifecycle the other columns carry.
CREATE_PREDICTIONS = """
CREATE TABLE IF NOT EXISTS predictions (
    id             TEXT PRIMARY KEY,
    ts             TEXT NOT NULL,
    source         TEXT NOT NULL,
    measure_key    TEXT NOT NULL,
    agent          TEXT,
    predicted_usd  REAL,
    predicted_pct  REAL,
    fidelity       TEXT NOT NULL DEFAULT 'estimated',
    seen_at        TEXT,
    change_ts      TEXT,
    judged_at      TEXT,
    verdict        TEXT,
    measured_usd   REAL,
    measured_pct   REAL,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_predictions_judged_at ON predictions(judged_at);
"""

#: Every ``CREATE TABLE``/``CREATE INDEX`` statement, in dependency
#: order (a table referencing another via ``REFERENCES`` is listed
#: after it) — ``Store.migrate`` executes these in this order.
ALL_STATEMENTS: tuple[str, ...] = (
    CREATE_META,
    CREATE_PROJECTS,
    CREATE_SNAPSHOTS,
    CREATE_SESSIONS,
    CREATE_TRANSCRIPTS,
    CREATE_TURNS_AGG,
    CREATE_RECACHE_TURNS,
    CREATE_EVENTS,
    CREATE_COMPACTIONS,
    CREATE_SESSION_TAGS,
    CREATE_SESSION_FEEDBACK,
    CREATE_SESSION_PLAN_FEEDBACK,
    CREATE_TIP_FEEDBACK,
    CREATE_PROFILES,
    CREATE_BASELINES,
    CREATE_WORKFLOW_RUNS,
    CREATE_USAGE_LOG,
    CREATE_PREDICTIONS,
)

__all__ = [
    "SCHEMA_VERSION",
    "CREATE_META",
    "CREATE_PROJECTS",
    "CREATE_SESSIONS",
    "CREATE_TRANSCRIPTS",
    "CREATE_TURNS_AGG",
    "CREATE_RECACHE_TURNS",
    "CREATE_EVENTS",
    "CREATE_COMPACTIONS",
    "CREATE_SNAPSHOTS",
    "CREATE_SESSION_TAGS",
    "CREATE_SESSION_FEEDBACK",
    "CREATE_SESSION_PLAN_FEEDBACK",
    "CREATE_TIP_FEEDBACK",
    "CREATE_PROFILES",
    "CREATE_BASELINES",
    "CREATE_WORKFLOW_RUNS",
    "CREATE_USAGE_LOG",
    "CREATE_PREDICTIONS",
    "ALL_STATEMENTS",
]

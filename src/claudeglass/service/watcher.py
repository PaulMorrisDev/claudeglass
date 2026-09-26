"""``FileWatcher``: the polling loop that keeps the v0.2 service's
:class:`~claudeglass.service.store.Store` up to date with whatever
is currently under ``ServeOptions.projects_root`` (and its
``extra_projects_roots``), implementing
``service.contracts.Watcher``.

Each :meth:`FileWatcher.run_once` tick:

1. Discovers every project directory under ``options.projects_root``
   and each of ``options.extra_projects_roots``
   (``discovery.resolve_project_dirs(..., all_projects=True,
   exclude_projects=options.exclude_projects)``), then every top-level
   session file, subagent transcript (ordinary and workflow-nested — see
   ``discovery.find_subagents``'s own docstring) and workflow run file
   under each, honouring the same four shapes ``discovery.py`` documents.
2. Diffs the discovered ``(path, mtime_ns, size_bytes)`` triples, plus
   each stored transcript's own ``parser_version``, against
   ``Store.known_files()`` to decide, per file, whether to re-parse it
   this tick (see :meth:`FileWatcher._resolve`'s docstring for the exact
   new/changed/live/stale-parser decision table).
3. Folds every parsed (or previously-stored, for an unchanged file)
   transcript into the store via ``Store.upsert_transcript``, and every
   session's classification/cost totals via ``Store.upsert_session``. A
   session with nothing changed since the tick that last folded it is
   skipped (see ``FileWatcher._session_fingerprint``).
4. Removes rows for files no longer on disk (``Store.remove_missing``),
   prunes old sessions when ``options.retention_days`` is set, and always
   prunes old capture signal files (``signals.prune``), old
   ``capture-log.jsonl`` records (``config.prune_capture_log``) and old
   ``usage-log.csv`` rows (SIG-5: ``log_usage.prune_usage_log``) -- at
   ``options.retention_days`` when set, else
   ``config.SIGNAL_RETENTION_DEFAULT_DAYS`` (SEC-P8/G7: this telemetry
   must never grow forever just because nobody set a retention window,
   unlike session rows, which are visible report data and are only ever
   pruned on an explicit opt-in) -- and ingests any new config-snapshot
   file under ``options.config_dir/snapshots/`` (see
   :meth:`_scan_snapshots`).

Never raises out of :meth:`run_once` for a single bad file or session —
each is wrapped in its own ``try``/``except`` and recorded in
``WatcherStats.error_messages`` as a short, path-free message (privacy
rule restated from ``service/__init__.py``'s module docstring: no stat,
log line or exception message that escapes this module may ever contain
a path or transcript text).

S1-integration closed every contract gap this module originally
documented here (``ServeOptions.billing_mode``, ``Store.upsert_snapshot``
de-duplication, ``WorkflowRun`` persistence) — see ``service/contracts.py``,
``service/schema.py`` and ``service/store.py`` for the resulting shapes.
One attribution choice remains, carried over unchanged:

- The snapshot-config hook (``hooks/snapshot-config.py``) writes every
  snapshot to one ``<config_dir>/snapshots/`` folder, whichever project
  it ran in — but ``Store.upsert_snapshot`` requires a store project.
  Every snapshot row is filed under the synthetic project slug
  ``store.GLOBAL_PROJECT_SLUG``; ``Store.snapshots()`` maps that sentinel
  back to a ``None`` ``project_slug`` for any reader. The project a
  schema-2 snapshot was taken in stays in its own ``project_slug`` field
  (the hook's redacted ``slug:<hash>``), which ``api.py`` reads.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import sqlite3
import threading
import time
import zlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from .. import PARSER_VERSION, classify, discovery, recache, workflows as workflows_mod, workstyle
from .. import baseline as baseline_mod
from .. import config as config_mod
from ..cache import DigestCache, encode_result, result_from_jsonable
from ..compaction import compaction_records_for_transcript
from ..corpus import _parse_worker
from ..model import TranscriptMeta, TranscriptResult
from .. import parse as parse_mod
from ..parse import parse_transcript
from ..pricing import Pricing, PricingError, load_pricing, price_turn
from ..profiles import catalogue as profile_catalogue, schema as profile_schema
from ..report import _dominant_transcript_model, _extract_workstyle_features
from .. import signals as signals_mod
from .. import snapshots as snapshots_mod
from ..tools import log_usage as log_usage_mod
from .contracts import ServeOptions, WatcherState, WatcherStats
from .store import GLOBAL_PROJECT_SLUG, Store, decode_digest_blob

#: A file whose mtime is under this many seconds old is assumed to still
#: be an active Claude Code session (same convention/value as
#: ``cache.LIVE_FILE_WINDOW_S``, duplicated here rather than imported so
#: this module never has to import ``cache.DigestCache`` just for the
#: constant).
LIVE_FILE_WINDOW_S = 60.0

#: A live file that keeps changing is parsed again once this many seconds
#: have passed since its last parse, live or not. Waiting for it to go
#: quiet froze a session written to more than once a minute for as long
#: as it ran (3.5 hours, $15 behind, on a real cloud session). A 44 MB
#: transcript parses in under half a second.
LIVE_REPARSE_S = 60.0

#: S1-perf item 2: a tick whose :meth:`FileWatcher._collect_parse_candidates`
#: pool is larger than this is worth the ``ProcessPoolExecutor`` start-up
#: cost; a smaller tick (the common case once the corpus is warm -- most
#: ticks touch only the handful of sessions actively being written)
#: parses sequentially exactly as before, at whatever single-file latency
#: that already has.
_PARALLEL_PARSE_THRESHOLD = 50

#: Same cap ``cli.py``'s own ``--jobs`` default guidance and
#: ``corpus.load_corpus`` use elsewhere in this project -- more workers
#: than CPUs just adds context-switch overhead for CPU-bound JSONL
#: parsing, and a huge machine gains nothing past 4 for a tick-sized
#: (not whole-corpus) batch.
_MAX_PARSE_WORKERS = 4


def _is_dir(path: Path) -> bool:
    """``Path.is_dir`` that treats an unreadable network path (a WSL
    distro that is shutting down) as missing instead of raising."""
    try:
        return Path(path).is_dir()
    except OSError:
        return False


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _error_summary(exc: BaseException) -> str:
    """An exception's type name for ``WatcherStats.error_messages``,
    plus SQLite's own first line for a database error ("database is
    locked"), which names no path or transcript text and is the part
    that says what went wrong. Any other exception's message may carry
    a path (an ``OSError``'s always does), so only its type is kept.
    """
    name = type(exc).__name__
    if isinstance(exc, sqlite3.Error):
        lines = str(exc).strip().splitlines()
        if lines:
            return f"{name}: {lines[0][:80]}"
    return name


def _default_pricing() -> Pricing:
    """The packaged default rate card, used to price every turn folded
    into ``turns_agg``/``sessions.total_cost``. Mirrors ``corpus.py``'s
    own ``_default_rates`` (duplicated rather than imported — that
    function is module-private to ``corpus.py`` and this module has no
    other reason to import from it): a corrupted/missing packaged
    ``pricing.toml`` degrades to an all-unknown rate card (every
    ``price_turn`` call prices at zero, ``model_known=False``) rather
    than failing every watcher tick outright.
    """
    try:
        return load_pricing()
    except PricingError:
        return Pricing(
            path="none",
            version="none",
            currency="USD",
            source_url=None,
            retrieved=None,
            notes=None,
            sha256="",
        )


def _build_top_meta(top_path: Path, session_id: str, project_slug: str) -> TranscriptMeta:
    """Same construction as ``corpus.py``'s own (private)
    ``_build_top_meta`` — duplicated rather than imported, per this
    project's established convention for small cross-module helpers.
    """
    meta = TranscriptMeta(path=str(top_path), kind="top-level", session_id=session_id, project_slug=project_slug)
    try:
        stat = top_path.stat()
    except OSError:
        return meta
    meta.mtime_ns = stat.st_mtime_ns
    meta.size_bytes = stat.st_size
    return meta


def _priced_turns(result: TranscriptResult):
    return [t for t in result.turns if t.turn_index > 0]


def _parse_ts(ts: str | None):
    from datetime import datetime

    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _turn_day(turn) -> str:
    """The turn's own UTC calendar day, e.g. ``"2026-09-18"``. Unlike
    ``usage.py``'s ``_day_key`` (which buckets by ``config.tz``'s local
    day for the report's own Usage section), the watcher has no
    ``Config``/timezone to read (``ServeOptions`` carries none) — UTC is
    the only zone available without one, so ``turns_agg.day`` is a UTC
    calendar day, not a local one. ``Store.daily_usage`` inherits this.
    """
    dt = _parse_ts(turn.ts)
    if dt is None:
        return "unknown"
    if dt.tzinfo is None:
        return dt.strftime("%Y-%m-%d")
    from datetime import timezone

    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


def _build_turns_agg(result: TranscriptResult, pricing: Pricing) -> list[dict]:
    """Per-day, per-model rollups (``turns_agg`` rows) for one
    transcript's priced turns, cost via ``pricing.price_turn`` at default
    pricing (the packaged rate card — no per-project/CLI override is
    plumbed through ``ServeOptions``)."""
    buckets: dict[tuple[str, str], dict] = {}
    for turn in _priced_turns(result):
        model = turn.model or "<unknown>"
        key = (_turn_day(turn), model)
        bucket = buckets.setdefault(
            key,
            {
                "day": key[0],
                "model": model,
                "turns": 0,
                "input_tokens": 0,
                "cache_creation_tokens": 0,
                "cache_read_tokens": 0,
                "output_tokens": 0,
                "thinking_tokens": 0,
                "cc_5m": 0,
                "cc_1h": 0,
                "cost": 0.0,
            },
        )
        resolved = pricing.resolve_model(turn.model)
        breakdown = price_turn(turn, resolved)
        # An estimated compaction call is spend, not a reply.
        bucket["turns"] += not turn.is_synthetic
        bucket["input_tokens"] += turn.input_tokens
        bucket["cache_creation_tokens"] += turn.cache_creation_tokens
        bucket["cache_read_tokens"] += turn.cache_read_tokens
        bucket["output_tokens"] += turn.output_tokens
        bucket["thinking_tokens"] += turn.thinking_tokens
        bucket["cc_5m"] += turn.cc_5m
        bucket["cc_1h"] += turn.cc_1h
        bucket["cost"] += breakdown.total
    return list(buckets.values())


def _build_recache_turns(result: TranscriptResult, thresholds: recache.RecacheThresholds) -> list[dict]:
    """``recache_turns`` rows for one transcript, via ``recache.detect``
    at default thresholds (``ServeOptions`` carries no threshold
    overrides — same posture as pricing above)."""
    flagged = recache.detect(result.turns, thresholds)
    return [
        {
            "turn_index": turn.turn_index,
            "signature": turn.recache_signature or "",
            "cache_creation_tokens": turn.cache_creation_tokens,
            "preceding_primary": turn.preceding_primary.value if turn.preceding_primary is not None else None,
            "gap_s": turn.gap_s,
        }
        for turn in flagged
    ]


def _build_events(result: TranscriptResult) -> list[dict]:
    """One aggregate row per ``(kind, subkind)`` (S1-perf item 4) --
    ``events_agg.count``/``dropped_tokens_sum``/``duration_ms_sum``,
    rather than one row per raw ``Event`` -- no reader anywhere in this
    codebase used per-event detail (``store.py`` has no ``events()``
    read method, and neither ``api.py`` nor ``rebuild.py`` ever queries
    the table), so this is the exact same information any caller could
    ever get back out, at a small fraction of the row count. ``ts`` is
    intentionally dropped: aggregating necessarily collapses it (an
    aggregate row spans every occurrence's own timestamp), and nothing
    read it back either.
    """
    aggregates: dict[tuple[str, str | None], dict[str, int]] = {}
    for event in result.events:
        key = (event.kind.value, event.subkind)
        agg = aggregates.setdefault(key, {"count": 0, "dropped_tokens_sum": 0, "duration_ms_sum": 0})
        agg["count"] += 1
        agg["dropped_tokens_sum"] += event.dropped_tokens or 0
        agg["duration_ms_sum"] += event.duration_ms or 0
    return [
        {"kind": kind, "subkind": subkind, **agg}
        for (kind, subkind), agg in aggregates.items()
    ]


def _build_compactions(
    result: TranscriptResult, pricing: Pricing, thresholds: recache.RecacheThresholds
) -> list[dict]:
    dominant_model = _dominant_transcript_model(result)
    rates = pricing.resolve_model(dominant_model) if dominant_model else None
    records = compaction_records_for_transcript(result, rates, thresholds)
    return [
        {
            "ts": record.ts,
            "pre_tokens": record.pre_tokens,
            "post_tokens": record.post_tokens,
            "dropped_tokens": record.dropped_tokens,
            "trigger": record.trigger,
            "join_delta_s": record.join_delta_s,
        }
        for record in records
        if record.ts is not None  # schema's compactions.ts is NOT NULL
    ]


class FileWatcher:
    """``contracts.Watcher`` implementation over one
    :class:`~claudeglass.service.store.Store`. See the module
    docstring for the per-tick algorithm.
    """

    def __init__(
        self,
        store: Store,
        options: ServeOptions,
        *,
        cache: DigestCache | None = None,
        now=None,
        salt: bytes | None = None,
        after_tick: Callable[[], object] | None = None,
    ) -> None:
        self.store = store
        self.options = options
        self.cache = cache
        #: Called on the background thread after each tick (``serve.py``
        #: checks whether the package's code changed on disk). Not by
        #: :meth:`run_once` itself, so ``serve --once`` and tests that
        #: tick by hand never run it.
        self._after_tick = after_tick
        #: Handed to each prewarm worker process, which (under Windows'
        #: ``spawn``) does not inherit this process's ``parse.set_salt``.
        self._salt = salt
        self._now = now or time.time
        self._pricing = _default_pricing()
        self._recache_thresholds = recache.RecacheThresholds()

        #: Paths force-parsed once while still "live" (see :meth:`_resolve`)
        #: that must be re-parsed on a later tick once stable, even if
        #: their ``(mtime_ns, size_bytes)`` never changes again in the
        #: meantime. In-memory only — a watcher restart loses this, which
        #: only means a file that finished writing in the exact window
        #: between two process lifetimes keeps its first (possibly
        #: incomplete) parse until it's next touched; a strictly better
        #: outcome than the alternative of never getting a first parse at
        #: all until the file is touched again.
        self._pending_stabilize: set[str] = set()
        #: ``{str(path): now}`` as of this process's last parse of each
        #: transcript, so a live one that keeps changing is parsed again
        #: every :data:`LIVE_REPARSE_S` (see :meth:`_live_due`). In-memory
        #: only: after a restart a live file is due at once.
        self._parsed_at: dict[str, float] = {}

        self._loaded_snapshots: list[snapshots_mod.Snapshot] = []
        self._snapshot_ids_by_ts: dict[str, int] = {}
        #: Which profile was active when, re-read each tick by
        #: :meth:`_scan_snapshots` for :meth:`_fold_session`'s ``profile_id``.
        self._profile_marks: list[snapshots_mod.ProfileMark] = []
        #: ``{str(path): mtime_ns}`` as of the last tick that actually
        #: upserted that snapshot file (nit 29) -- lets _scan_snapshots
        #: skip re-flattening/re-upserting a snapshot file that hasn't
        #: changed since the previous tick instead of doing so on every
        #: single poll regardless.
        self._snapshot_file_mtimes: dict[str, int] = {}

        #: ``{session_id: fingerprint}`` for each session as of the tick
        #: that last folded it (see :meth:`_session_fingerprint`). A
        #: session whose fingerprint still matches, with every one of its
        #: files unchanged in the store, is skipped outright: decoding
        #: every stored digest and re-folding every session each tick took
        #: seconds of CPU every 30 seconds with nothing new to read.
        #: In-memory only, so a restart folds every session once again.
        self._folded: dict[str, tuple] = {}
        #: What :meth:`_fold_session` reads besides the session's own
        #: files and tags (snapshot ids, profile marks), refreshed each
        #: tick by :meth:`_scan_snapshots`.
        self._fold_context: tuple = ()
        #: ``{(project_dir, session_id): [(subagent path, meta or None)]}``
        #: as :meth:`_collect_parse_candidates` listed them this tick, so
        #: :meth:`_scan_session` doesn't list and read them all again.
        #: A ``None`` meta failed to load and is read again there, where
        #: its error is recorded.
        self._listed_subagents: dict[tuple[str, str], list[tuple[str, TranscriptMeta | None]]] = {}

        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lifecycle_lock = threading.Lock()

        #: ``contracts.Watcher.last_stats`` -- the most recent
        #: :meth:`run_once` tick's stats, kept up to date so
        #: ``serve.run`` never has to guess (S1-integration fix 1.e).
        self.last_stats: WatcherStats | None = None

        #: What :meth:`state` reports, written by the ticking thread and
        #: read by ``/api/health`` request threads under ``_state_lock``.
        self._state_lock = threading.Lock()
        self._scan_started_at: str | None = None
        self._last_success_at: str | None = None
        self._last_tick_failed = False
        self._phase: str | None = None
        self._done = 0
        self._total = 0

    # -- contracts.Watcher ------------------------------------------------

    def state(self) -> WatcherState:
        thread = self._thread
        with self._state_lock:
            return WatcherState(
                running=thread is not None and thread.is_alive(),
                scanning=self._scan_started_at is not None,
                scan_started_at=self._scan_started_at,
                last_success_at=self._last_success_at,
                last_tick_failed=self._last_tick_failed,
                phase=self._phase,
                done=self._done,
                total=self._total,
            )

    def _set_progress(self, phase: str | None, total: int = 0) -> None:
        with self._state_lock:
            self._phase, self._done, self._total = phase, 0, total

    def _advance_progress(self) -> None:
        with self._state_lock:
            self._done += 1

    def run_once(self) -> WatcherStats:
        stats = WatcherStats(started_at=_now_iso())
        with self._state_lock:
            self._scan_started_at = stats.started_at
        t0 = time.monotonic()
        failed = False
        try:
            # Inside the try: a store another process holds locked must
            # fail this one tick, not kill the thread that runs them all.
            self.store.open()  # idempotent; ensures this thread's own connection has the schema
            self._run_once(stats)
        except Exception as exc:  # never let one bad tick raise out of run_once
            failed = True
            stats.errors += 1
            stats.error_messages = stats.error_messages + (f"tick failed: {_error_summary(exc)}",)
        stats.duration_s = time.monotonic() - t0
        stats.finished_at = _now_iso()
        with self._state_lock:
            self.last_stats = stats
            self._last_tick_failed = failed
            if not failed:
                self._last_success_at = stats.finished_at
            self._scan_started_at = None
            self._phase, self._done, self._total = None, 0, 0
        return stats

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            thread = threading.Thread(target=self._loop, name="claudeglass-watcher", daemon=True)
            self._thread = thread
            thread.start()

    def stop(self) -> None:
        with self._lifecycle_lock:
            self._stop_event.set()
            thread = self._thread
        if thread is not None:
            thread.join()
        with self._lifecycle_lock:
            self._thread = None

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except Exception:  # run_once catches its own; this thread must outlive anything
                pass
            if self._after_tick is not None:
                try:
                    self._after_tick()
                except Exception:  # nor may the hook stop the scans
                    pass
            self._stop_event.wait(self.options.poll_interval_s)

    # -- per-tick timing helpers (S1-perf item 5) ---------------------------

    def _time_discovery(self, stats: WatcherStats, fn, *args, **kwargs):
        """Call ``fn(*args, **kwargs)``, adding the elapsed wall-clock
        time to ``stats.discovery_s`` regardless of whether ``fn`` raises
        (the caller's own try/except still sees the exception -- this
        never swallows one, it only makes sure the time already spent is
        recorded before it propagates)."""
        t0 = time.monotonic()
        try:
            return fn(*args, **kwargs)
        finally:
            stats.discovery_s += time.monotonic() - t0

    def _time_store(self, stats: WatcherStats, fn, *args, **kwargs):
        """``Store`` counterpart to :meth:`_time_discovery` -- every
        ``Store`` reader/writer call this module makes goes through this
        (or :meth:`_parse`'s own ``parse_s`` timing) so ``stats.store_s``
        covers the whole tick's database time, not just its writes."""
        t0 = time.monotonic()
        try:
            return fn(*args, **kwargs)
        finally:
            stats.store_s += time.monotonic() - t0

    # -- per-tick algorithm -------------------------------------------------

    def _run_once(self, stats: WatcherStats) -> None:
        self._scan_snapshots(stats)
        self._scan_baselines(stats)
        self._scan_profiles(stats)
        self._scan_predictions(stats)

        known = self._time_store(stats, self.store.known_files)
        seen_paths: set[str] = set()

        project_dirs = self._time_discovery(
            stats,
            discovery.resolve_project_dirs,
            [self.options.projects_root, *self.options.extra_projects_roots],
            all_projects=True,
            exclude_projects=list(self.options.exclude_projects),
        )

        # S1-perf item 2: bulk-parse this tick's pending transcripts in a
        # process pool ahead of the per-session loop below, so that
        # loop's own sequential _resolve/_parse calls resolve as on-disk
        # cache hits instead of each doing its own single-file parse. A
        # no-op (falls through to the loop's existing sequential
        # behaviour) whenever there's no cache configured or too little
        # pending work to justify a pool.
        self._listed_subagents = {}
        self._prewarm_cache(project_dirs, known, stats)

        # Finding 7/S1-perf item 6: real-time tag overrides -- a POST
        # /api/sessions/<id>/tags write must change sessions.mode/purpose
        # on the very next tick, not only the next report rebuild (see
        # _fold_session's own docstring note below). Fetched once per
        # tick, not once per session, since classify.classify_session's
        # own overrides.get(session_id, {}) already does the per-session
        # lookup into this whole-store dict.
        all_tags = self._time_store(stats, self.store.all_tags)

        sessions_by_dir = [
            (project_dir, self._time_discovery(stats, discovery.find_sessions, project_dir))
            for project_dir in project_dirs
        ]
        self._set_progress("storing", total=sum(len(top_paths) for _, top_paths in sessions_by_dir))
        for project_dir, top_paths in sessions_by_dir:
            slug = project_dir.name
            for top_path in top_paths:
                self._scan_session(project_dir, slug, top_path, known, seen_paths, stats, all_tags)
                self._advance_progress()
        self._set_progress(None)
        self._listed_subagents = {}
        seen_sessions = {top_path.stem for _, top_paths in sessions_by_dir for top_path in top_paths}
        self._folded = {sid: fingerprint for sid, fingerprint in self._folded.items() if sid in seen_sessions}

        if not project_dirs:
            # Finding 3 (second failure mode): an empty project_dirs list
            # is ambiguous between "genuinely no projects yet" and
            # "--projects-root is misconfigured/unmounted this tick" --
            # calling remove_missing(set()) here would mark every single
            # known transcript missing on the strength of that ambiguity
            # alone. Skip the missing-marking step entirely and record it,
            # so a transient/misconfigured root never mass-marks a whole
            # corpus missing; a real "no projects" installation is still
            # visible as an explicit, non-error note rather than silence.
            stats.error_messages = stats.error_messages + (
                "projects root returned no projects; skipped missing check",
            )
        elif unreachable := [
            root for root in (self.options.projects_root, *self.options.extra_projects_roots) if not _is_dir(root)
        ]:
            # One folder of several is out of reach (a WSL distro that
            # was shut down): its sessions were not seen this tick, but
            # are not gone. Same reasoning as the empty case above.
            stats.error_messages = stats.error_messages + (
                f"{len(unreachable)} projects folder(s) not reachable; skipped missing check",
            )
        else:
            stats.files_removed = self._time_store(stats, self.store.remove_missing, seen_paths)

        stats.transcripts_missing = self.store.count_missing_transcripts()

        if self.options.retention_days is not None:
            self._time_store(stats, self.store.retention_prune, self.options.retention_days)

        # SEC-P8/G7: signal files and the capture-change log are Token
        # Lens's own background telemetry, not visible report data --
        # unlike the store-row pruning above (which only ever runs when
        # the user opts in with an explicit retention_days, since that
        # deletes what a report shows), these must never be left to grow
        # forever just because nobody configured a retention window.
        # `or` (not `is not None`) so a 0 -- which config.py's own
        # RETENTION_DAYS_MIN already forbids on the way in, but a caller
        # could still construct ServeOptions directly with one -- falls
        # back to the safe default rather than pruning everything.
        signal_retention = self.options.retention_days or config_mod.SIGNAL_RETENTION_DEFAULT_DAYS
        signals_mod.prune(self.options.config_dir, signal_retention)
        config_mod.prune_capture_log(self.options.config_dir, signal_retention)

        # SIG-5: usage-log.csv is written unconditionally on every
        # statusline refresh, capture on or off -- same "never left to
        # grow forever" reasoning as the two lines above, so it's pruned
        # on the same schedule rather than needing its own opt-in.
        usage_log_path = log_usage_mod.default_usage_log_path(self.options.config_dir)
        log_usage_mod.prune_usage_log(usage_log_path, signal_retention)

        # EST-P5: predictions.jsonl's own 90/400-day expiry (unseen vs.
        # judged) is fixed, unlike the rest of this module's retention --
        # it runs every tick regardless of --retention-days.
        self._time_store(stats, self.store.prune_predictions)

    # -- S1-perf item 2: bulk parallel prewarm -------------------------------

    def _needs_parse_this_tick(
        self, path_str: str, meta: TranscriptMeta, known: dict[str, tuple[int, int, int]]
    ) -> bool:
        """A read-only predicate mirroring :meth:`_resolve`'s own new/
        changed/live/stale-parser decision table (see that method's
        docstring) -- used only by :meth:`_collect_parse_candidates` to
        decide which paths are worth bulk-parsing in parallel ahead of
        the main per-session loop. Never mutates ``_pending_stabilize``
        or any ``stats`` counter -- :meth:`_resolve` remains the sole
        authority on what actually gets parsed and recorded this tick; a
        mismatch between the two here only costs efficiency (a file
        prewarmed that ``_resolve`` decides not to re-parse after all,
        or vice versa), never correctness.
        """
        prior = known.get(path_str)
        never_seen = prior is None
        prior_key = prior[:2] if prior is not None else None
        changed = never_seen or (meta.mtime_ns, meta.size_bytes) != prior_key
        forced = path_str in self._pending_stabilize
        live = self._is_live(meta.mtime_ns)
        #: The file itself is unchanged, but the digest stored for it was
        #: produced under an older ``PARSER_VERSION`` than the one now
        #: running -- see :meth:`_resolve`'s docstring.
        parser_stale = (not never_seen) and (prior[2] or 0) < PARSER_VERSION

        if not changed and not forced:
            return parser_stale
        if live and not never_seen and not forced and not self._live_due(path_str):
            return False
        return True

    def _collect_parse_candidates(
        self, project_dirs: list[Path], known: dict[str, tuple[int, int, int]]
    ) -> list[tuple[str, TranscriptMeta]]:
        """Every top-level/subagent transcript path
        :meth:`_needs_parse_this_tick` says needs a fresh parse this
        tick, across every ``project_dirs`` entry -- the candidate pool
        :meth:`_prewarm_cache` bulk-parses in a process pool when it's
        large enough to be worth the pool start-up cost.

        Walks the same ``discovery.find_sessions``/``find_subagent_paths``
        shapes the main per-session loop walks afterwards, keeping each
        session's subagent listing in ``_listed_subagents`` for that loop
        to reuse rather than list and read every ``.meta.json`` again.
        Never raises -- an ``OSError``/other failure walking one project
        directory's sessions or subagents is silently skipped here (the
        main loop's own try/except around the identical calls records it
        properly-scoped).
        """
        candidates: list[tuple[str, TranscriptMeta]] = []
        for project_dir in project_dirs:
            slug = project_dir.name
            try:
                top_paths = discovery.find_sessions(project_dir)
            except OSError:
                continue
            for top_path in top_paths:
                self._advance_progress()
                session_id = top_path.stem
                top_meta = _build_top_meta(top_path, session_id, slug)
                if self._needs_parse_this_tick(str(top_path), top_meta, known):
                    candidates.append((str(top_path), top_meta))
                try:
                    subagent_paths = discovery.find_subagent_paths(project_dir, session_id)
                except OSError:
                    continue
                listed: list[tuple[str, TranscriptMeta | None]] = []
                for jsonl_path in subagent_paths:
                    self._advance_progress()
                    sub_path_str = str(jsonl_path)
                    try:
                        sub_meta = discovery.load_meta(jsonl_path.with_name(jsonl_path.stem + ".meta.json"))
                    except Exception:
                        listed.append((sub_path_str, None))
                        continue
                    listed.append((sub_path_str, sub_meta))
                    if self._needs_parse_this_tick(sub_path_str, sub_meta, known):
                        candidates.append((sub_path_str, sub_meta))
                self._listed_subagents[(str(project_dir), session_id)] = listed
        return candidates

    def _prewarm_cache(
        self, project_dirs: list[Path], known: dict[str, tuple[int, int, int]], stats: WatcherStats
    ) -> None:
        """Parse this tick's pending transcripts in a
        ``ProcessPoolExecutor`` and prime ``self.cache`` with the
        results, so the per-session loop below's own sequential
        ``_resolve``/``_parse`` calls resolve as on-disk cache hits
        instead of each doing its own single-file parse (S1-perf item 2:
        a cold first tick over a large corpus is otherwise entirely
        single-threaded). A no-op when this watcher has no
        ``DigestCache`` configured (``serve.run`` always passes one --
        see ``service/serve.py`` -- but tests that construct
        ``FileWatcher(store, options)`` directly, without a cache,
        deliberately keep the fully sequential path), when the pending
        pool is at or below :data:`_PARALLEL_PARSE_THRESHOLD`, or on a
        single-CPU machine (a pool would only add overhead there).

        Never raises: a worker's parse failure is silently skipped here
        (never cached) -- the per-session loop's own ``_resolve``/
        ``_parse`` call for that same path then hits a cache miss and
        re-parses it in-process, surfacing the exact same, correctly
        path-free, properly-scoped error message
        (top-level/subagent/workflow) the loop has always produced.
        Re-parsing a handful of bad files twice is a non-issue; silently
        losing per-file error attribution would not be.
        """
        if self.cache is None:
            return

        t_discover = time.monotonic()
        self._set_progress("finding")
        candidates = self._collect_parse_candidates(project_dirs, known)
        stats.discovery_s += time.monotonic() - t_discover

        if len(candidates) <= _PARALLEL_PARSE_THRESHOLD:
            return

        t_parse = time.monotonic()
        # One count over every candidate: a cache hit is read at once, a
        # miss once its worker below has parsed it.
        self._set_progress("reading", total=len(candidates))
        try:
            pending: list[tuple[str, TranscriptMeta]] = []
            for path_str, meta in candidates:
                try:
                    hit = self.cache.get(path_str, meta)
                except Exception:
                    hit = None
                if hit is None:
                    pending.append((path_str, meta))
                else:
                    self._advance_progress()

            if pending:
                workers = min(_MAX_PARSE_WORKERS, os.cpu_count() or 1)
                if workers > 1:
                    pool_kwargs: dict = {}
                    if self._salt is not None:
                        pool_kwargs["initializer"] = parse_mod.set_salt
                        pool_kwargs["initargs"] = (self._salt,)
                    with concurrent.futures.ProcessPoolExecutor(max_workers=workers, **pool_kwargs) as executor:
                        future_map = {
                            executor.submit(_parse_worker, path, meta): (path, meta) for path, meta in pending
                        }
                        for future in concurrent.futures.as_completed(future_map):
                            self._advance_progress()
                            path, meta = future_map[future]
                            try:
                                result = future.result()
                            except Exception:
                                continue
                            try:
                                self.cache.put(path, meta, result)
                            except OSError:
                                continue
        except Exception:
            pass
        finally:
            stats.parse_s += time.monotonic() - t_parse

    def _scan_session(
        self,
        project_dir: Path,
        slug: str,
        top_path: Path,
        known: dict[str, tuple[int, int, int]],
        seen_paths: set[str],
        stats: WatcherStats,
        all_tags: dict[str, dict[str, str]],
    ) -> None:
        session_id = top_path.stem
        stats.files_scanned += 1
        top_path_str = str(top_path)
        seen_paths.add(top_path_str)

        try:
            top_meta = _build_top_meta(top_path, session_id, slug)
        except Exception as exc:
            stats.errors += 1
            stats.error_messages = stats.error_messages + (f"top-level parse error: {_error_summary(exc)}",)
            return

        subagent_error: BaseException | None = None
        sub_metas = self._listed_subagents.pop((str(project_dir), session_id), None)
        try:
            # nit 26: find_subagent_paths/find_workflows are generators that
            # walk the filesystem lazily -- an OSError raised mid-walk
            # (a directory removed/permission-denied between discovery
            # and this iteration) previously escaped the surrounding
            # try/except entirely, because the generator itself, not the
            # loop body, is where the exception would actually surface.
            # Materializing the listing up front brings that failure
            # under the same per-session error handling as everything
            # else in this method.
            if sub_metas is None:
                sub_metas = []
                for jsonl_path in self._time_discovery(
                    stats, lambda: discovery.find_subagent_paths(project_dir, session_id)
                ):
                    try:
                        sub_meta = discovery.load_meta(jsonl_path.with_name(jsonl_path.stem + ".meta.json"))
                    except Exception:
                        sub_meta = None  # read again below, where its error is recorded
                    sub_metas.append((str(jsonl_path), sub_meta))
        except OSError as exc:
            subagent_error = exc
            sub_metas = []
        workflow_error: BaseException | None = None
        try:
            workflow_paths = self._time_discovery(
                stats, lambda: list(discovery.find_workflows(project_dir, session_id))
            )
        except OSError as exc:
            workflow_error = exc
            workflow_paths = []

        fingerprint = None
        if subagent_error is None and workflow_error is None:
            fingerprint = self._session_fingerprint(
                session_id, top_path_str, top_meta, sub_metas, workflow_paths, known, all_tags
            )
        if fingerprint is not None and self._folded.get(session_id) == fingerprint:
            # Nothing this session is folded from has changed since the
            # tick that last folded it.
            stats.files_scanned += len(sub_metas) + len(workflow_paths)
            seen_paths.update(path_str for path_str, _meta in sub_metas)
            return
        self._folded.pop(session_id, None)

        try:
            top_result, top_was_parsed = self._resolve(top_path_str, top_meta, known, stats)
        except Exception as exc:
            stats.errors += 1
            stats.error_messages = stats.error_messages + (f"top-level parse error: {_error_summary(exc)}",)
            return

        # ``transcripts.session_id`` is a foreign key onto ``sessions.id``
        # (enforced -- ``Store`` runs with ``PRAGMA foreign_keys = ON``),
        # so a session row must exist before any transcript row naming it
        # can be written. This placeholder (bare session/project identity
        # only) is deliberately minimal -- ``_fold_session`` below always
        # overwrites it with the fully computed record once classification
        # and cost are known. It is insert-only (``ensure_session``): an
        # existing row keeps its totals while this session's subagents
        # are re-parsed, so the dashboard's cost never dips mid-scan.
        # It's only reached once the top-level file itself resolved
        # successfully, so a session whose one-and-only top-level file
        # never parses still never gets a row at all (nothing to fold).
        self._time_store(
            stats,
            self.store.ensure_session,
            session_id=session_id, project_slug=slug, project_root_path=str(project_dir), slug=slug,
        )

        if top_was_parsed:
            self._upsert_transcript_row(session_id, top_path_str, top_meta, top_result, stats)

        subs: list[TranscriptResult] = []
        if subagent_error is not None:
            stats.errors += 1
            stats.error_messages = stats.error_messages + (
                f"subagent discovery error: {_error_summary(subagent_error)}",
            )
        for sub_path_str, sub_meta in sub_metas:
            stats.files_scanned += 1
            seen_paths.add(sub_path_str)
            try:
                if sub_meta is None:
                    sub_meta = discovery.load_meta(Path(sub_path_str).with_name(Path(sub_path_str).stem + ".meta.json"))
                sub_result, sub_was_parsed = self._resolve(sub_path_str, sub_meta, known, stats)
                if sub_was_parsed:
                    self._upsert_transcript_row(session_id, sub_path_str, sub_meta, sub_result, stats)
                subs.append(sub_result)
            except Exception as exc:
                stats.errors += 1
                stats.error_messages = stats.error_messages + (f"subagent parse error: {_error_summary(exc)}",)
                continue

        # Workflow run files (S1-integration fix 1.d): parsed via the
        # same workflows.py functions corpus.load_corpus uses, linked to
        # this session's already-collected subagent transcripts (subs
        # already includes any workflow-nested agents -- see
        # discovery.find_subagents's own docstring), and persisted so
        # service/rebuild.py can read them back into
        # SessionBundle.workflows.
        if workflow_error is not None:
            stats.errors += 1
            stats.error_messages = stats.error_messages + (
                f"workflow discovery error: {_error_summary(workflow_error)}",
            )
        for workflow_path in workflow_paths:
            stats.files_scanned += 1
            try:
                run = workflows_mod.parse_workflow_file(workflow_path)
                workflows_mod.link_workflow_agents(run, subs, self._pricing)
                self._time_store(
                    stats,
                    self.store.upsert_workflow_run,
                    session_id=session_id,
                    run_id=run.run_id,
                    agent_count=run.agent_count,
                    phase_titles=list(run.phase_titles),
                    started=run.started,
                    finished=run.finished,
                    cost=run.cost,
                    status=run.status,
                )
            except Exception as exc:
                stats.errors += 1
                stats.error_messages = stats.error_messages + (f"workflow parse error: {_error_summary(exc)}",)

        try:
            self._fold_session(session_id, slug, project_dir, top_result, subs, stats, all_tags)
        except Exception as exc:
            stats.errors += 1
            stats.error_messages = stats.error_messages + (f"session fold error: {_error_summary(exc)}",)
            return
        if fingerprint is not None:
            self._folded[session_id] = fingerprint

    def _session_fingerprint(
        self,
        session_id: str,
        top_path_str: str,
        top_meta: TranscriptMeta,
        sub_metas: list[tuple[str, TranscriptMeta | None]],
        workflow_paths: list[Path],
        known: dict[str, tuple[int, int, int]],
        all_tags: dict[str, dict[str, str]],
    ) -> tuple | None:
        """Everything :meth:`_fold_session` reads for one session: each
        transcript's and workflow file's ``(path, mtime_ns, size_bytes)``,
        the session's tags and :attr:`_fold_context`. ``None`` when any
        transcript isn't already stored as it is on disk (new, changed,
        live, awaiting its stable re-parse, from an older parser, or its
        meta unreadable) -- such a session is always processed in full."""
        files = []
        for path_str, meta in [(top_path_str, top_meta), *sub_metas]:
            if meta is None:
                return None
            prior = known.get(path_str)
            if (
                prior is None
                or (meta.mtime_ns, meta.size_bytes) != prior[:2]
                or (prior[2] or 0) < PARSER_VERSION
                or path_str in self._pending_stabilize
            ):
                return None
            files.append((path_str, meta.mtime_ns, meta.size_bytes))
        for workflow_path in workflow_paths:
            try:
                stat = workflow_path.stat()
            except OSError:
                return None
            files.append((str(workflow_path), stat.st_mtime_ns, stat.st_size))
        tags = tuple(sorted(all_tags.get(session_id, {}).items()))
        return (tuple(files), tags, self._fold_context)

    def _resolve(
        self,
        path_str: str,
        meta: TranscriptMeta,
        known: dict[str, tuple[int, int, int]],
        stats: WatcherStats,
    ) -> tuple[TranscriptResult, bool]:
        """Decide whether ``path_str`` needs (re-)parsing this tick, and
        do it if so. Returns ``(result, was_parsed)`` — ``was_parsed``
        tells the caller whether to write a fresh ``transcripts`` row
        (``True``) or reuse the one already there (``False``, for a file
        this tick chose not to touch).

        Decision table (see the module docstring's algorithm summary):

        - Unchanged since the last tick (same ``(mtime_ns, size_bytes)``
          as ``known``), not pending a forced re-parse, and its stored
          digest was produced under the ``PARSER_VERSION`` still
          running: reuse the store's existing digest, no re-parse.
        - Unchanged since the last tick, not pending a forced re-parse,
          but its stored digest predates the current ``PARSER_VERSION``
          (bumped 6): re-parse anyway (``files_reparsed_stale_parser``)
          — the file on disk hasn't changed, but the parsing logic that
          produced its stored fields has, so a digest computed under an
          older parser must not be silently reused forever (this is the
          only path that ever revisits an untouched file; see the digest
          cache's own ``header["parser_version"]`` check in ``cache.py``
          for the matching on-disk-cache half of this).
        - New-or-changed, but live (mtime under
          :data:`LIVE_FILE_WINDOW_S`), already known from a previous
          tick, and parsed less than :data:`LIVE_REPARSE_S` ago: skip
          parsing this tick (``files_skipped_live``), reuse whatever is
          already stored — it will be seen as changed again next tick
          (``known`` wasn't updated) and re-examined then. Once that long
          has passed it is parsed even though still live, so a session
          that never goes quiet doesn't freeze.
        - New-or-changed and either not live, or live but never seen
          before: parse now. A live-and-never-seen file is also added to
          :attr:`_pending_stabilize` so a later tick re-parses it even if
          its ``(mtime_ns, size_bytes)`` doesn't change again before it
          goes stable.
        """
        prior = known.get(path_str)
        never_seen = prior is None
        prior_key = prior[:2] if prior is not None else None
        current_key = (meta.mtime_ns, meta.size_bytes)
        changed = never_seen or current_key != prior_key
        forced = path_str in self._pending_stabilize
        live = self._is_live(meta.mtime_ns)
        #: The file itself is unchanged, but the digest stored for it was
        #: produced under an older ``PARSER_VERSION`` than the one now
        #: running.
        parser_stale = (not never_seen) and (prior[2] or 0) < PARSER_VERSION

        if not changed and not forced and not parser_stale:
            existing = self._load_existing(path_str, stats)
            if existing is not None:
                return existing, False
            # No prior digest despite a known_files entry -- shouldn't
            # normally happen, but parse rather than return nothing.

        elif not changed and not forced and parser_stale:
            stats.files_reparsed_stale_parser += 1
            # Fall through to parse: the file hasn't changed, but its
            # stored digest predates the current PARSER_VERSION.

        elif live and not never_seen and not forced and not self._live_due(path_str):
            stats.files_skipped_live += 1
            existing = self._load_existing(path_str, stats)
            if existing is not None:
                return existing, False
            # Fall through to parse: known_files() said we'd seen this
            # path before, but there's no digest to reuse.

        result = self._parse(path_str, meta, stats)
        self._parsed_at[path_str] = self._now()
        if live:
            self._pending_stabilize.add(path_str)
        else:
            self._pending_stabilize.discard(path_str)
        stats.files_parsed += 1
        return result, True

    def _live_due(self, path_str: str) -> bool:
        """Whether a live file this process last parsed
        :data:`LIVE_REPARSE_S` or more ago (or never) is due a parse."""
        parsed_at = self._parsed_at.get(path_str)
        return parsed_at is None or self._now() - parsed_at >= LIVE_REPARSE_S

    def _is_live(self, mtime_ns: int) -> bool:
        age_s = self._now() - (mtime_ns / 1_000_000_000)
        return age_s < LIVE_FILE_WINDOW_S

    def _parse(self, path_str: str, meta: TranscriptMeta, stats: WatcherStats) -> TranscriptResult:
        t0 = time.monotonic()
        try:
            if self.cache is not None:
                hit = self.cache.get(path_str, meta)
                if hit is not None:
                    return hit
            result = parse_transcript(path_str, meta)
            if self.cache is not None:
                self.cache.put(path_str, meta, result)
            return result
        finally:
            stats.parse_s += time.monotonic() - t0

    def _load_existing(self, path_str: str, stats: WatcherStats) -> TranscriptResult | None:
        """The already-stored ``TranscriptResult`` for ``path_str``, decoded
        from its ``transcripts.digest_blob`` column (zlib-compressed, S1-perf
        item 4 -- see :func:`~claudeglass.service.store.decode_digest_blob`)
        — the same encoding ``cache.py`` uses (see
        :func:`~claudeglass.cache.encode_result`), so this is a
        lossless round trip, not a re-parse."""
        row = self._time_store(
            stats,
            lambda: self.store._connection().execute(
                "SELECT digest_blob FROM transcripts WHERE path = ?", (path_str,)
            ).fetchone(),
        )
        if row is None:
            return None
        try:
            return result_from_jsonable(json.loads(decode_digest_blob(row["digest_blob"])))
        except (KeyError, TypeError, ValueError, zlib.error):
            return None

    def _upsert_transcript_row(
        self, session_id: str, path_str: str, meta: TranscriptMeta, result: TranscriptResult, stats: WatcherStats
    ) -> None:
        digest_json = json.dumps(encode_result(result))
        self._time_store(
            stats,
            self.store.upsert_transcript,
            session_id=session_id,
            path=path_str,
            kind=meta.kind,
            agent_id=meta.agent_id,
            agent_type=meta.agent_type,
            spawn_depth=meta.spawn_depth,
            parent_agent_id=meta.parent_agent_id,
            mtime_ns=meta.mtime_ns,
            size_bytes=meta.size_bytes,
            parser_version=PARSER_VERSION,
            digest_json=digest_json,
            turns_agg=_build_turns_agg(result, self._pricing),
            recache_turns=_build_recache_turns(result, self._recache_thresholds),
            events=_build_events(result),
            compactions=_build_compactions(result, self._pricing, self._recache_thresholds),
        )

    def _fold_session(
        self,
        session_id: str,
        slug: str,
        project_dir: Path,
        top: TranscriptResult,
        subs: list[TranscriptResult],
        stats: WatcherStats,
        all_tags: dict[str, dict[str, str]],
    ) -> None:
        # S1-perf item 6: apply real-time ``POST /api/sessions/<id>/tags``
        # overrides on the watcher path too, not only when a report is
        # built (``api.py``'s ``_build_report_model`` already merged
        # ``store.all_tags()`` into its own overrides -- this was the gap
        # that left ``/api/sessions`` showing the pre-override
        # mode/purpose until a full rebuild). Config-file
        # ``sessions.toml`` overrides are deliberately out of scope here,
        # exactly as before this fix (the watcher has never consulted
        # them) -- only the store-tag gap is closed.
        classification = classify.classify_session(
            top, subs, all_tags, None, workflows=0, entrypoint=top.meta.entrypoint
        )
        record = classify.build_session_record(top, subs, [], classification, slug)
        features = _extract_workstyle_features(top, subs, [])
        archetype, _evidence = workstyle.detect_archetype(features)

        total_cost = 0.0
        total_tokens = 0
        for transcript in [top, *subs]:
            for turn in _priced_turns(transcript):
                resolved = self._pricing.resolve_model(turn.model)
                breakdown = price_turn(turn, resolved)
                total_cost += breakdown.total
                total_tokens += (
                    turn.input_tokens + turn.cache_creation_tokens + turn.cache_read_tokens + turn.output_tokens
                )

        snapshot_id: int | None = None
        if record.first_ts and self._loaded_snapshots:
            snap = snapshots_mod.snapshot_for(record.first_ts, self._loaded_snapshots)
            if snap is not None:
                snapshot_id = self._snapshot_ids_by_ts.get(snap.ts)
        profile_id = snapshots_mod.profile_for(record.first_ts, self._profile_marks, session_id)

        self._time_store(
            stats,
            self.store.upsert_session,
            session_id=session_id,
            project_slug=slug,
            project_root_path=str(project_dir),
            slug=slug,
            first_ts=record.first_ts,
            last_ts=record.last_ts,
            span_s=record.span_s,
            archetype=archetype,
            mode=classification.mode,
            mode_source=classification.mode_source,
            purpose=classification.purpose,
            purpose_source=classification.purpose_source,
            entrypoint=record.entrypoint,
            billing_mode=self.options.billing_mode,
            snapshot_id=snapshot_id,
            profile_id=profile_id,
            total_cost=total_cost,
            total_tokens=total_tokens,
        )
        stats.sessions_upserted += 1

    def _scan_snapshots(self, stats: WatcherStats) -> None:
        """Ingest every ``options.config_dir/snapshots/*.json`` file this
        tick, unconditionally -- ``Store.upsert_snapshot`` now dedupes by
        its own natural key ``(project_id, ts, schema_version)`` (schema
        v2's ``ON CONFLICT``), so re-ingesting an already-known snapshot
        on a later tick just updates its existing row rather than growing
        a duplicate one (S1-integration fix 1.b; this replaces the
        previous pre-check-and-skip workaround against
        ``Store.snapshots()``). Populates
        :attr:`_loaded_snapshots`/:attr:`_snapshot_ids_by_ts` for
        :meth:`_fold_session`'s ``snapshot_id`` lookup, and
        :attr:`_profile_marks` for its ``profile_id``.
        """
        loaded = snapshots_mod.load_snapshots(self.options.config_dir)
        self._loaded_snapshots = loaded

        ids_by_ts: dict[str, int] = {}
        fresh_mtimes: dict[str, int] = {}
        for snap in loaded:
            path_key = str(snap.path)
            try:
                mtime_ns = snap.path.stat().st_mtime_ns
            except OSError:
                mtime_ns = None

            # nit 29: a snapshot file only actually changes once per
            # Claude Code session start, but this method previously
            # re-flattened and re-upserted every loaded snapshot on every
            # single poll tick (every 30s by default) regardless. Reuse
            # the id already on record when the file's own mtime hasn't
            # moved since the tick that last upserted it.
            if (
                mtime_ns is not None
                and self._snapshot_file_mtimes.get(path_key) == mtime_ns
                and snap.ts in self._snapshot_ids_by_ts
            ):
                ids_by_ts[snap.ts] = self._snapshot_ids_by_ts[snap.ts]
                fresh_mtimes[path_key] = mtime_ns
                continue

            try:
                # The snapshot's own (hook-redacted) document, not its
                # flattened sections: api.py rebuilds a Snapshot from this,
                # and effective_config/managed_keys/effective_agents read
                # top-level fields that flattening dropped.
                digest_json = json.dumps(snap.data, sort_keys=True, default=str)
                schema_version = int(snap.data.get("schema", 1)) if isinstance(snap.data, dict) else 1
                new_id = self._time_store(
                    stats,
                    self.store.upsert_snapshot,
                    project_slug=GLOBAL_PROJECT_SLUG,
                    project_root_path="",
                    ts=snap.ts,
                    schema_version=schema_version,
                    digest_json=digest_json,
                )
                ids_by_ts[snap.ts] = new_id
                if mtime_ns is not None:
                    fresh_mtimes[path_key] = mtime_ns
            except Exception as exc:
                stats.errors += 1
                stats.error_messages = stats.error_messages + (f"snapshot ingest error: {_error_summary(exc)}",)
        self._snapshot_ids_by_ts = ids_by_ts
        self._snapshot_file_mtimes = fresh_mtimes
        self._profile_marks = snapshots_mod.load_profile_marks(self.options.config_dir)
        self._fold_context = (
            tuple(sorted(ids_by_ts.items())),
            tuple((mark.ts.isoformat(), mark.profile_id, mark.session_id) for mark in self._profile_marks),
        )

    # -- v0.3 baseline / profile ingestion -----------------------------------

    def _scan_baselines(self, stats: WatcherStats) -> None:
        """Ingest every ``<config_dir>/baselines/*.json`` baseline record
        (``claudeglass baseline`` -- ``baseline.list_baselines``)
        into the ``baselines`` table, content-hash deduped
        (``Store.record_baseline``'s own ``record_id``/``content_hash``
        check) so a repeat tick over an unchanged file is a no-op.

        A baseline record's own ``projects`` field (already redacted --
        ``baseline.py``'s own privacy guarantee, restated in this
        module's docstring) attributes the row to its first named
        project, or the synthetic global slug when the record named
        none (e.g. the "no sessions in this window yet" minimal record)
        -- the same attribution posture :meth:`_scan_snapshots` uses for
        a snapshot with no per-project identity of its own.
        ``window_start``/``window_end`` are derived from the record's
        own ``created_at``/``window_days`` fields -- there is no
        separate start/end timestamp in a baseline record
        (``baseline.build_baseline``) -- a computation from the record's
        own already-computed fields, never a fabrication.
        """
        try:
            records = baseline_mod.list_baselines(self.options.config_dir)
        except OSError as exc:
            stats.errors += 1
            stats.error_messages = stats.error_messages + (f"baseline scan error: {_error_summary(exc)}",)
            return

        for record in records:
            try:
                record_id = record.get("id")
                if not record_id:
                    continue
                digest_json = json.dumps(record, sort_keys=True)
                content_hash = hashlib.sha256(digest_json.encode("utf-8")).hexdigest()
                projects = record.get("projects") or []
                project_slug = projects[0] if projects else GLOBAL_PROJECT_SLUG

                created_at = record.get("created_at") or _now_iso()
                window_days = record.get("window_days")
                window_end = created_at
                window_start = created_at
                if window_days:
                    try:
                        created_dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
                        window_start = (created_dt - timedelta(days=window_days)).isoformat()
                    except ValueError:
                        pass

                self._time_store(
                    stats,
                    self.store.record_baseline,
                    project_slug=project_slug,
                    project_root_path="",
                    window_start=window_start,
                    window_end=window_end,
                    archetype=record.get("archetype"),
                    digest_json=digest_json,
                    record_id=record_id,
                    content_hash=content_hash,
                )
            except Exception as exc:
                stats.errors += 1
                stats.error_messages = stats.error_messages + (f"baseline ingest error: {_error_summary(exc)}",)

    def _scan_profiles(self, stats: WatcherStats) -> None:
        """Ingest every ``<config_dir>/profiles/*.toml`` *user* profile
        file into the ``profiles`` table (``Store.upsert_profile``),
        content-hash deduped on the file's own raw text so a repeat tick
        over an unchanged file is a no-op.

        Catalogue profiles (``profiles.catalogue.CATALOGUE_IDS``) are
        never ingested here -- they are shipped, static package data
        with no on-disk file of the user's own under ``config_dir`` to
        track; ``/api/profiles`` (``service/api.py``) merges them in at
        query time instead. A user file that happens to name a catalogue
        id is skipped rather than upserted, so it can never shadow the
        shipped one. A malformed profile file (fails
        ``profiles.schema.load_profile``) is skipped and recorded in
        ``stats.error_messages`` rather than aborting the whole tick --
        the same "never let one bad file break the tick" posture every
        other per-file step in this module already follows.
        """
        profiles_dir = Path(self.options.config_dir) / "profiles"
        if not profiles_dir.is_dir():
            return

        for path in sorted(profiles_dir.glob("*.toml")):
            try:
                text = path.read_text(encoding="utf-8")
                profile = profile_schema.load_profile(path)
                if profile.id in profile_catalogue.CATALOGUE_IDS:
                    continue
                content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
                self._time_store(
                    stats,
                    self.store.upsert_profile,
                    profile_id=profile.id,
                    name=profile.name or profile.id,
                    toml_path=str(path),
                    content_hash=content_hash,
                )
            except Exception as exc:
                stats.errors += 1
                stats.error_messages = stats.error_messages + (f"profile ingest error: {_error_summary(exc)}",)

    def _scan_predictions(self, stats: WatcherStats) -> None:
        """Ingest every record in ``<config_dir>/prediction-log.jsonl``
        (``config.append_prediction_log`` -- written by ``route_whatif``
        when asked to log an estimate, EST-P5) into the ``predictions``
        table (``Store.upsert_prediction``), id-deduped so a repeat tick
        over an already-ingested line is a no-op -- the same posture
        :meth:`_scan_profiles` gives its own content-hash dedup, just
        keyed on the record's own id instead of a hash of its file,
        since a prediction log line is itself immutable once written.
        """
        try:
            records = config_mod.load_prediction_log(self.options.config_dir)
        except OSError as exc:
            stats.errors += 1
            stats.error_messages = stats.error_messages + (f"prediction scan error: {_error_summary(exc)}",)
            return

        for record in records:
            try:
                prediction_id = record.get("id")
                ts = record.get("ts")
                source = record.get("source")
                measure_key = record.get("measure_key")
                fidelity = record.get("fidelity")
                if not all(isinstance(value, str) and value for value in (prediction_id, ts, source, measure_key, fidelity)):
                    continue
                self._time_store(
                    stats,
                    self.store.upsert_prediction,
                    prediction_id=prediction_id,
                    ts=ts,
                    source=source,
                    measure_key=measure_key,
                    agent=record.get("agent"),
                    predicted_usd=record.get("predicted_usd"),
                    predicted_pct=record.get("predicted_pct"),
                    fidelity=fidelity,
                )
            except Exception as exc:
                stats.errors += 1
                stats.error_messages = stats.error_messages + (f"prediction ingest error: {_error_summary(exc)}",)


__all__ = ["FileWatcher", "LIVE_FILE_WINDOW_S", "LIVE_REPARSE_S"]

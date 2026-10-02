"""Frozen v0.2 contracts for the watcher thread and the JSON API, so
``watcher.py``, ``api.py`` and their tests can be built in parallel
against this one file rather than against each other.

Three dataclasses describe data that crosses the watcher/API/CLI
boundary:

- :class:`ServeOptions` — everything ``claudeglass serve`` parses
  from its CLI flags, passed to both the watcher and the API factory.
- :class:`WatcherStats` — what one watcher poll tick did, for the
  ``/api/health``/diagnostics surface and for tests.
- :class:`ApiError` — the ``{"ok": false, "error": {...}}`` envelope
  shape every failed ``/api/*`` response uses (see ``docs/api.md``).

Two ``Protocol``s describe behaviour without importing an implementation:

- :class:`Watcher` — the polling loop ``watcher.py`` implements.
- :class:`ApiHandler` — the callable shape one ``/api/*`` route
  implements; :data:`MakeHandler` is the factory-function shape
  ``api.py``'s own ``make_handler`` follows to bind a set of routes to
  one :class:`~claudeglass.service.store.Store` and
  :class:`ServeOptions` pair as an ``http.server.BaseHTTPRequestHandler``
  subclass.

Nothing here imports ``store.py``, ``watcher.py`` or ``api.py`` at
runtime (only under ``TYPE_CHECKING``, for the ``Store`` type hints)
precisely so this module has no implementation to keep in sync — it is
the thing implementations are checked against, not the other way round.
"""

from __future__ import annotations

import typing
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

if typing.TYPE_CHECKING:
    from http.server import BaseHTTPRequestHandler

    from .store import Store


@dataclass(slots=True)
class ServeOptions:
    """Everything ``claudeglass serve --projects-root ... --config-dir
    ... --port ... --bind ...`` resolves from its CLI flags (plan
    Milestone v0.2's first bullet). Passed to both the watcher and the
    API handler factory so the two never have to agree on flag parsing
    independently.
    """

    projects_root: Path
    config_dir: Path
    #: More folders of project folders read alongside ``projects_root``
    #: (more ``--projects-root`` flags, then ``config.toml``'s
    #: ``extra_projects_roots``, such as a WSL distro's).
    extra_projects_roots: tuple[Path, ...] = ()
    port: int = 8765
    #: Localhost by default (plan: "port bound to localhost only").
    #: ``serve`` may accept a different bind for an explicit opt-in
    #: (e.g. a container's own loopback), never a wildcard by default.
    bind: str = "127.0.0.1"
    #: Watcher poll interval in seconds (plan: "interval 30 s").
    poll_interval_s: float = 30.0
    #: ``None`` means "keep forever"; otherwise ``Store.retention_prune``
    #: is run once per poll tick.
    retention_days: int | None = None
    #: Directories under ``projects_root`` to never scan (plan
    #: "Enterprise use": confidential repositories).
    exclude_projects: tuple[str, ...] = ()
    #: ``"api"`` (pay-per-token) or ``"subscription"`` (flat-rate plan) --
    #: stamped onto every ``sessions.billing_mode`` row the watcher
    #: upserts (S1-integration fix 1.a). ``cli.py``'s ``serve`` subcommand
    #: defaults this from ``<config_dir>/config.toml``'s own
    #: ``Config.billing`` when no explicit ``--billing-mode`` flag is
    #: given, falling back to this field's own default otherwise.
    billing_mode: str = "api"
    #: ``serve --monthly-report DIR``: while ``serve`` runs,
    #: ``monthly_job.MonthlyReportJob`` writes the previous calendar
    #: month's report (the same files ``monthly-report --out DIR``
    #: writes) into this directory when they are missing, checking at
    #: startup and hourly. ``None`` means no report is written. Any
    #: billing mode.
    monthly_report_dir: Path | None = None
    #: Extra host names the ``Host`` header may carry (``serve
    #: --allowed-host``), on top of the loopback names and a specific
    #: ``bind`` address that are always allowed. Any other ``Host`` is
    #: refused before routing, so a DNS-rebinding page can't read the API.
    allowed_hosts: tuple[str, ...] = ()
    #: ``serve --store PATH``: the SQLite database file. ``None`` means
    #: ``<config_dir>/service.db``. A second ``serve`` (a dev copy
    #: beside the logon service) points this elsewhere so the two never
    #: share, and lock, one database.
    store_path: Path | None = None
    #: ``serve --exit-on-code-change``: once this package's own files
    #: change on disk (``codewatch.CodeWatch``), exit with
    #: ``serve.EXIT_CODE_CHANGED`` so the registered service starts
    #: ``serve`` again on the new code. ``install-service`` registers it;
    #: without it, ``serve`` only reports the change.
    exit_on_code_change: bool = False


@dataclass(slots=True)
class WatcherStats:
    """What one :meth:`Watcher.run_once` tick did — returned to the
    caller and folded into the ``/api/health`` response so a stuck or
    erroring watcher is externally visible rather than silent.
    """

    files_scanned: int = 0
    files_parsed: int = 0
    files_skipped_live: int = 0
    #: Despite the name (kept for API stability), this tick's count of
    #: transcripts newly marked missing (``Store.remove_missing``'s own
    #: return value) -- not necessarily deleted. A transcript is only
    #: ever actually deleted by ``Store.retention_prune`` or
    #: ``claudeglass serve --purge`` (review finding 3). Always 0
    #: on a tick where the missing check itself was skipped (see
    #: ``error_messages``'s "projects root returned no projects" note).
    files_removed: int = 0
    #: This tick's count of transcripts re-parsed even though their
    #: ``(mtime_ns, size_bytes)`` hadn't changed, because their stored
    #: digest was written under an older ``PARSER_VERSION`` than the one
    #: now running (``FileWatcher._resolve``) -- lets an operator see a
    #: parser-version bump's corpus-wide rebuild actually happening,
    #: rather than it silently never reaching untouched files. Also
    #: counts the one-time re-parse after the rate card (``pricing.toml``,
    #: or ``config.toml``'s ``pricing_path``) changes, since a card change
    #: marks every stored transcript stale through the same path
    #: (``FileWatcher._check_rate_card``).
    files_reparsed_stale_parser: int = 0
    #: Running total of transcripts currently marked missing
    #: (``Store.count_missing_transcripts``) as of this tick -- a
    #: transcript whose file the watcher can no longer find is marked,
    #: not deleted (review finding 3), so this is the *current* total,
    #: not this tick's own delta.
    transcripts_missing: int = 0
    #: Sessions folded and written this tick. A session with nothing
    #: changed since the tick that last folded it is skipped and not
    #: counted, so an idle tick counts 0.
    sessions_upserted: int = 0
    errors: int = 0
    started_at: str | None = None
    finished_at: str | None = None
    duration_s: float = 0.0
    #: Short, non-path error summaries (e.g. ``"json decode error"``),
    #: never a traceback or a file path — see ``service/__init__.py``'s
    #: privacy-rule docstring.
    error_messages: tuple[str, ...] = ()
    #: S1-perf timing breakdown of this tick's ``duration_s``, so a slow
    #: tick's dominant cost is externally visible rather than only the
    #: single total (``/api/health``, ``docs/api.md``). The three don't
    #: exactly sum to ``duration_s`` -- session/workflow folding and the
    #: fixed per-tick overhead (snapshot scanning, missing-file bookkeeping)
    #: are counted in none of them -- but each is a real, non-overlapping
    #: wall-clock measurement of its own named phase.
    #:
    #: ``discovery_s``: time spent walking the filesystem to find project
    #: dirs/sessions/subagents (``discovery.py``) and diffing them against
    #: ``Store.known_files()``, including the S1-perf bulk-parse
    #: candidate scan (see ``watcher.py``'s ``_prewarm_cache``).
    #: ``parse_s``: time spent inside ``FileWatcher._parse`` (cache
    #: lookups and, on a miss, ``parse.parse_transcript``) plus the
    #: parallel prewarm pool's own wall-clock time when it runs.
    #: ``store_s``: time spent inside ``Store`` writer calls
    #: (``upsert_session``/``upsert_transcript``/``upsert_workflow_run``/
    #: ``upsert_snapshot``/``remove_missing``/``retention_prune``).
    discovery_s: float = 0.0
    parse_s: float = 0.0
    store_s: float = 0.0


@dataclass(slots=True)
class WatcherState:
    """Where the watcher is right now, as opposed to what its last
    finished tick did (:class:`WatcherStats`). ``/api/health`` reads it
    to tell a first scan still running from a scanner that has stopped.
    """

    #: The background thread is alive (``False`` before :meth:`Watcher.start`,
    #: after :meth:`Watcher.stop`, and if the thread ever dies).
    running: bool = False
    #: A tick is in progress; ``scan_started_at`` is when it began.
    scanning: bool = False
    scan_started_at: str | None = None
    #: When the last tick that did not fail outright finished. ``None``
    #: until one has.
    last_success_at: str | None = None
    #: The most recent finished tick failed outright (the store could not
    #: be opened or read, say), rather than just skipping a bad file.
    last_tick_failed: bool = False
    #: The in-progress tick's phase, with ``done`` of ``total`` items:
    #: ``"finding"`` (walking the projects folders; ``done`` files seen,
    #: ``total`` 0 as the count isn't known up front), ``"reading"``
    #: (parsing changed files, from the parse cache or in parallel) and
    #: ``"storing"`` (folding sessions into the store). The first two
    #: happen only on a tick with many changed files. ``None``/``0``
    #: when idle.
    phase: str | None = None
    done: int = 0
    total: int = 0


@dataclass(slots=True)
class CodeState:
    """Whether this package's files on disk still match the ones the
    running ``serve`` loaded (``codewatch.CodeWatch``). ``/api/health``
    reports it as ``code`` and turns ``status`` to ``"outdated"`` once
    they differ.
    """

    #: The files have differed from the ones loaded at start. Stays
    #: ``True`` once set, since a lazy import may already have loaded
    #: new code.
    changed: bool = False
    #: When the difference was first seen.
    changed_at: str | None = None
    #: ``__version__`` in the package's ``__init__.py`` on disk (``None``
    #: when it can't be read).
    version_on_disk: str | None = None
    #: A short fingerprint of the code this process loaded: a different
    #: ``id`` after a restart means the service now runs other code.
    id: str = ""


@dataclass(slots=True)
class ApiError:
    """The shape of a failed ``/api/*`` response's ``error`` object
    (plan/``docs/api.md``: every response is either ``{"ok": true,
    "data": ...}`` or ``{"ok": false, "error": {...}}``).
    """

    status: int
    code: str
    message: str

    def to_envelope(self) -> dict:
        """The full JSON-ready ``{"ok": false, "error": {...}}`` body a
        handler writes for this error (``status`` is the HTTP status
        code, sent separately as the response header)."""
        return {"ok": False, "error": {"code": self.code, "message": self.message}}


class Watcher(Protocol):
    """The polling loop a ``watcher.py`` implementation must provide.
    ``serve`` constructs one ``Watcher``, calls :meth:`start` once, and
    :meth:`stop` on shutdown; tests call :meth:`run_once` directly
    without a background thread.
    """

    #: The most recent :meth:`run_once` tick's stats, or ``None`` before
    #: the first tick has ever run. A concrete ``Watcher`` must keep this
    #: up to date so ``/api/health`` (via ``serve.run``'s
    #: ``watcher_stats`` callable) always has a real answer once the
    #: background poll thread is running, without ``serve.py`` having to
    #: guess at an attribute a ``Watcher`` implementation might or might
    #: not happen to expose (S1-integration fix 1.e).
    last_stats: "WatcherStats | None"

    def state(self) -> "WatcherState":
        """A consistent snapshot of the watcher's current
        :class:`WatcherState`. Safe to call from any thread."""
        ...

    def run_once(self) -> "WatcherStats":
        """Scan ``ServeOptions.projects_root`` (and ``extra_projects_roots``)
        once: find new/changed
        transcript files since the last tick (via ``Store.known_files``),
        plus any unchanged file whose stored digest predates the running
        ``PARSER_VERSION``, re-parse each in full with ``parse_transcript``
        (nit 25: there is no incremental "resume from the last byte
        offset" path -- a changed file is re-read from the start), fold
        the result into the store (``Store.upsert_transcript``/``upsert_session``),
        remove rows for files no longer present (``Store.remove_missing``),
        and run ``Store.retention_prune`` if configured. Returns stats
        for this one tick. Must never raise for a single bad file —
        record it in ``WatcherStats.error_messages`` and continue.
        """
        ...

    def start(self) -> None:
        """Begin polling every ``ServeOptions.poll_interval_s`` seconds
        on a background thread. Idempotent — calling it twice must not
        start a second thread."""
        ...

    def stop(self) -> None:
        """Stop the background thread and wait for the in-flight tick
        (if any) to finish. Idempotent."""
        ...


class ApiHandler(Protocol):
    """The callable shape one ``/api/*`` route implements: given the
    open :class:`~claudeglass.service.store.Store` and the
    request's parsed query-string mapping, return ``(http_status,
    json_ready_body)``. ``json_ready_body`` is always either
    ``{"ok": True, "data": ...}`` or an :class:`ApiError`'s
    :meth:`~ApiError.to_envelope`.

    A ``POST`` route (``/api/sessions/<id>/tags``, ``/api/profiles``)
    additionally receives the parsed JSON request body as ``body``;
    a ``GET`` route ignores it (``body`` is ``None``).
    """

    def __call__(
        self, store: "Store", query: dict[str, str], body: dict | None
    ) -> tuple[int, dict]:
        ...


class MakeHandler(Protocol):
    """The shape of ``api.py``'s own ``make_handler`` factory: given the
    open store and the resolved serve options, build an
    ``http.server.BaseHTTPRequestHandler`` subclass with every
    ``/api/*`` route (each implementing :class:`ApiHandler`) bound to
    them via closure, ready to pass to
    ``http.server.HTTPServer((options.bind, options.port), make_handler(store, options))``.
    """

    def __call__(self, store: "Store", options: "ServeOptions") -> type["BaseHTTPRequestHandler"]:
        ...


__all__ = [
    "ServeOptions",
    "WatcherStats",
    "ApiError",
    "Watcher",
    "ApiHandler",
    "MakeHandler",
]

"""``claudeglass serve``'s runtime entry point: lock and open the
SQLite store, bind the port, start the watcher's background poll thread
(its first tick included, so a first scan of a large history never keeps
the dashboard from answering), then serve the JSON API
(``service/api.py``'s ``make_handler``) until interrupted.

:func:`run` is the whole module's surface -- ``cli.py``'s ``serve``
subcommand calls it directly with the ``ServeOptions`` it builds from
argv.

Contract notes:

- The SQLite file is ``ServeOptions.store_path`` (``--store``), else
  ``<config_dir>/service.db`` (a sibling of ``config.toml``/``profiles/``/
  ``snapshots/`` already living under the same directory per
  ``config.py``/``snapshots.py``) -- see :func:`store_path_for`.
- One ``serve`` per store: :func:`run` takes ``storelock.StoreLock`` on
  the store before opening it and holds it until it exits, so a second
  ``serve`` (or ``--once``) on the same store is refused with the
  holder's process id and address rather than fighting it for SQLite's
  write lock.
- ``allow_remote`` is a keyword-only addition beyond ``ServeOptions``'s
  own fields (mirroring ``api.py``'s ``watcher_stats`` addition to
  ``MakeHandler``) -- the plan's "port bound to localhost only" default
  posture needs an explicit opt-in for anything else, and
  ``ServeOptions`` itself carries no such flag.
- ``contracts.Watcher.last_stats`` (S1-integration fix 1.e) is now a
  documented Protocol attribute a concrete ``Watcher`` must keep current,
  so ``/api/health`` always has a real answer once the watcher is polling
  on its own thread: :func:`run` passes ``make_handler`` a
  ``watcher_stats`` callable that simply reads ``watcher.last_stats``,
  and a ``watcher_state`` callable (``watcher.state``) that says whether
  the first scan is still running and how far it has got --
  ``last_stats`` stays ``None`` until that first tick finishes.
- ``ServeOptions.monthly_report_dir`` (``--monthly-report DIR``) starts
  ``monthly_job.MonthlyReportJob``: last month's report is written into
  ``DIR`` when missing, checked at startup and hourly on a background
  thread (once, in line, under ``--once``).
- ``coaching_job.CoachingJob`` rewrites ``coaching.json`` (your split
  points and plan habit, for the capture hook's coaching notes) once a
  day while coaching notes are on, from a report built from the store
  once the first scan is done (in line, under ``--once``).
- A running ``serve`` fingerprints this package's files at start
  (``codewatch.CodeWatch``) and checks them after every watcher tick;
  ``/api/health`` turns ``"outdated"`` once they change on disk. With
  ``ServeOptions.exit_on_code_change`` (``--exit-on-code-change``, which
  ``install-service`` registers) it also exits with
  :data:`EXIT_CODE_CHANGED` once the change has settled, so the service
  starts again on the new code (``installer.relaunch_after_exit``); where
  that can't be arranged it stays up.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

from .. import __version__
from .. import hook_health
from .. import parse as parse_mod
from ..cache import DigestCache
from ..installer import relaunch_after_exit
from .codewatch import CodeWatch
from .contracts import ServeOptions
from .store import Store
from .storelock import StoreLock, StoreLockedError

#: See module docstring's first contract note. Public (S1-integration
#: fix 2.e) so ``cli.py``'s ``serve --purge`` can name the exact file
#: (and its WAL/SHM sidecars) it is about to delete without duplicating
#: the filename as a second literal.
STORE_FILENAME = "service.db"

_LOOPBACK_ADDRESSES = {"127.0.0.1", "::1", "localhost"}

#: How long a starting ``serve`` waits for another holder of its store's
#: lock to let go before refusing: long enough for ``install-service``'s
#: stop-then-start to hand over, short enough to fail visibly otherwise.
_LOCK_WAIT_S = 10.0

#: ``serve --exit-on-code-change``'s exit status once the package's code
#: changed on disk. Non-zero, so systemd's ``Restart=on-failure`` starts
#: it again (launchd's ``KeepAlive`` does whatever the status); Task
#: Scheduler restarts nothing by exit status, so on Windows
#: ``installer.relaunch_after_exit`` starts the task again instead.
EXIT_CODE_CHANGED = 3


def _is_loopback(bind: str) -> bool:
    return bind in _LOOPBACK_ADDRESSES


def store_path_for(options: ServeOptions) -> Path:
    """The SQLite file ``serve`` uses for ``options``: ``--store`` if
    given, else ``<config_dir>/service.db``."""
    if options.store_path is not None:
        return Path(options.store_path)
    return options.config_dir / STORE_FILENAME


def _url(bind: str, port: int) -> str:
    host = bind if ":" not in bind else f"[{bind}]"
    return f"http://{host}:{port}"


def _lock_info(options: ServeOptions, *, port: int | None, once: bool) -> dict:
    return {
        "pid": os.getpid(),
        "bind": options.bind,
        "port": port,
        "once": once,
        "version": __version__,
        "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def locked_message(store_path: Path, holder: dict, *, command: str = "serve") -> str:
    """What ``serve`` (or ``serve --purge``) prints when another process
    holds ``store_path``'s lock, naming that process from the details it
    recorded (``storelock.holder``)."""
    who = f"process {holder['pid']}" if isinstance(holder.get("pid"), int) else "another process"
    if holder.get("once"):
        who += ", a serve --once scan"
    elif isinstance(holder.get("port"), int) and isinstance(holder.get("bind"), str):
        who += f", serving {_url(holder['bind'], holder['port'])}"
    advice = "Stop it first."
    if command == "serve":
        advice = "Stop it first, or give this one its own database with --store PATH."
    return f"claudeglass {command}: {store_path} is in use by another serve ({who}).\n{advice}"


def _format_stats_line(stats) -> str:
    """Render a ``WatcherStats`` as the single line ``serve --once``
    prints on exit -- same fields ``/api/health`` exposes (``docs/api.md``),
    formatted for a terminal/cron log rather than as JSON, matching this
    module's other ``print()`` lines (``"claudeglass serve: ..."``).
    """
    line = (
        "claudeglass serve --once: "
        f"duration_s={stats.duration_s:.3f} "
        f"(discovery_s={stats.discovery_s:.3f} parse_s={stats.parse_s:.3f} store_s={stats.store_s:.3f}) "
        f"files_scanned={stats.files_scanned} files_parsed={stats.files_parsed} "
        f"files_skipped_live={stats.files_skipped_live} files_removed={stats.files_removed} "
        f"transcripts_missing={stats.transcripts_missing} sessions_upserted={stats.sessions_upserted} "
        f"errors={stats.errors}"
    )
    if stats.error_messages:
        line += f" error_messages={list(stats.error_messages)!r}"
    return line


def run(options: ServeOptions, *, once: bool = False, allow_remote: bool = False) -> int:
    """Run the v0.2 service until ``SIGINT``/``KeyboardInterrupt``
    (or, with ``once=True``, run a single watcher tick and return).
    Returns the process exit code: ``0`` on a clean stop, ``1`` if the
    store is in use by another ``serve`` or the port can't be bound,
    ``2`` if binding a non-loopback ``options.bind`` was refused,
    :data:`EXIT_CODE_CHANGED` when ``options.exit_on_code_change`` stopped
    it because the package's code changed on disk.
    """
    if not once and not allow_remote and not _is_loopback(options.bind):
        print(
            f"claudeglass serve: refusing to bind non-loopback address "
            f"{options.bind!r} without --allow-remote",
            file=sys.stderr,
        )
        return 2

    options.config_dir.mkdir(parents=True, exist_ok=True)
    store_path = store_path_for(options)
    lock = StoreLock(store_path)
    try:
        lock.acquire(_lock_info(options, port=None, once=once), wait_s=0.0 if once else _LOCK_WAIT_S)
    except StoreLockedError as exc:
        print(locked_message(store_path, exc.holder), file=sys.stderr)
        return 1
    try:
        return _run_locked(options, store_path, lock, once=once)
    finally:
        lock.release()


def _run_locked(options: ServeOptions, store_path: Path, lock: StoreLock, *, once: bool) -> int:
    # First, so the fingerprint is of the code this process has loaded
    # (see codewatch.py). --once exits before an update could matter.
    code_watch = None if once else CodeWatch()

    # ROB-P7: pick up a hook file this tool itself changed since it was
    # last installed (a pip upgrade that ran without --no-service, or one
    # 'update' couldn't reach) -- cheap (a few small files hashed, no
    # transcript read), and a no-op when metrics capture was never
    # connected (no <config_dir>/hooks folder yet).
    hook_health.refresh_hook_files(options.config_dir)

    store = Store(store_path, config_dir=options.config_dir)
    store.open()

    # Local import: service.watcher is a sibling work package's module
    # (S1-watcher) -- see api.py's module docstring for the identical
    # constraint applied to service.rebuild. Importing at call time
    # (rather than at this module's top level) means this module still
    # imports cleanly in a checkout where watcher.py hasn't landed yet.
    from .watcher import FileWatcher

    # S1-perf item 2: share the on-disk digest cache with the CLI's own
    # ``load_corpus``/``--jobs`` path (``<config_dir>/cache``, the same
    # directory ``cache.DigestCache`` always resolves to for a given
    # ``config_dir``) so a transcript parsed by one is a cache hit for
    # the other, and so the watcher's own bulk-prewarm pool
    # (``FileWatcher._prewarm_cache``) has somewhere to persist parsed
    # results across ticks.
    # Salt the path and skill-name hashes the same way the CLI does, so a
    # digest cached by either carries the same hashes (CLAUDE.md and
    # skills review join on them). Loaded before the cache below
    # (SEC-P8) so it can be threaded into DigestCache itself too --
    # otherwise a cache entry from a since-rotated salt would keep
    # matching on mtime/size/fingerprint alone and get served with
    # hashes that no longer agree with anything parsed fresh.
    salt = parse_mod.load_or_create_salt(options.config_dir)
    parse_mod.set_salt(salt)
    cache = DigestCache(options.config_dir, salt=salt)
    # ROB-P4/P5: once per serve process (not once per poll tick -- a
    # stale PARSER_VERSION folder only ever appears after a code deploy,
    # never mid-run), the same "sweep it where the cache is first opened
    # for real work" placement _load_corpus_for_args uses for the CLI.
    cache.prune_stale_versions()

    # After every tick: has the package's code changed on disk? With
    # --exit-on-code-change, stop once the change has settled and the
    # service is sure to start again (else stay up: /api/health says to
    # restart), from this (the watcher's) thread, which may call
    # server.shutdown(). Everything this needs was imported at start: a
    # lazy import now would load the changed code.
    exit_code = 0
    stay_up = False

    def _after_tick() -> None:
        nonlocal exit_code, stay_up
        code_watch.check()
        if not options.exit_on_code_change or exit_code or stay_up or not code_watch.settled():
            return
        if not relaunch_after_exit(os.getpid()):
            stay_up = True
            return
        exit_code = EXIT_CODE_CHANGED
        print(
            "claudeglass serve: this package's code changed on disk; exiting "
            f"(status {EXIT_CODE_CHANGED}) so the service starts again on the new code.",
            file=sys.stderr,
        )
        server.shutdown()

    watcher = FileWatcher(store, options, cache=cache, salt=salt, after_tick=None if once else _after_tick)

    # serve --monthly-report DIR: write last month's report into DIR when
    # it is missing (service/monthly_job.py). --once checks once, in
    # line; a running service checks at startup and hourly on its own
    # thread, so a report being built never holds up a request.
    monthly_job = None
    if options.monthly_report_dir is not None:
        from .monthly_job import MonthlyReportJob

        monthly_job = MonthlyReportJob(options)

    # Coaching notes' own numbers (service/coaching_job.py), from a
    # 30-day report of every project built from the store.
    from .coaching_job import CoachingJob

    # The job builds the report and then the typical piece of work from one
    # load of the store: the corpus is kept between the two calls.
    coaching_corpus: dict = {}

    def _coaching_report(days: int):
        from ..config import load_config
        from ..pricing import load_pricing
        from ..report import build_report
        from . import rebuild

        config = load_config(options.config_dir)
        rates = load_pricing(path=config.pricing_path, config_dir=options.config_dir)
        corpus = coaching_corpus["corpus"] = rebuild.corpus_from_store(store, days=days)
        return build_report(
            corpus,
            rates,
            config,
            projects=tuple(sorted({bundle.slug for bundle in corpus.sessions if bundle.slug})),
            window=f"last {days} days",
            ratings=store.all_feedback(),
            tip_feedback=store.tip_feedback(),
            config_dir=options.config_dir,
        )

    def _coaching_typical(days: int) -> int:
        from .. import coaching
        from . import rebuild

        corpus = coaching_corpus.pop("corpus", None) or rebuild.corpus_from_store(store, days=days)
        return coaching.typical_piece_tokens(corpus)

    coaching_job = CoachingJob(
        options, _coaching_report, build_typical=_coaching_typical, ready=lambda: watcher.last_stats is not None
    )

    if once:
        stats = watcher.run_once()
        # Print the tick's WatcherStats before exiting -- --once is the
        # one-shot/cron/verification entry point, and until this was
        # added it discarded every number docs/api.md documents as a
        # first-class diagnostic (discovery_s/parse_s/store_s, errors,
        # ...), leaving an operator running --once with no way to see
        # what the tick actually did short of reading the store or
        # starting the full server just to hit /api/health once.
        print(_format_stats_line(stats))
        if monthly_job is not None:
            monthly_job.run_once()
        coaching_job.run_once()
        store.close()
        return 0

    # Local import: service.api depends on this work package's own
    # api.py, which in turn lazily imports service.rebuild -- kept as a
    # call-time import here purely for symmetry with the watcher import
    # above, not out of necessity (api.py is this package's own module).
    from .api import make_handler

    # v3: wire the real logon/boot registration probe into /api/health
    # here (not inside api.py itself, per that module's own docstring on
    # why it never imports installer.py) -- installer.is_registered's
    # own default runner is subprocess.run, and api.py's make_handler
    # caches whatever this callable returns for
    # api._SERVICE_REGISTERED_CACHE_TTL_S, so this only actually shells
    # out to schtasks/systemctl/launchctl at most once every 10 minutes
    # of live traffic, not on every request.
    from ..installer import is_registered as _probe_service_registered

    handler_cls = make_handler(
        store,
        options,
        watcher_stats=lambda: watcher.last_stats,
        watcher_state=watcher.state,
        service_registered=_probe_service_registered,
        code_watch=code_watch,
        restarts_itself=lambda: options.exit_on_code_change and not stay_up,
    )
    # Bind before the first scan: the dashboard answers (and shows the
    # scan's progress) straight away instead of refusing connections for
    # as long as a first read of the whole history takes.
    try:
        server = ThreadingHTTPServer((options.bind, options.port), handler_cls)
    except OSError as exc:
        print(
            f"claudeglass serve: can't listen on {_url(options.bind, options.port)} "
            f"({exc.strerror or type(exc).__name__}). Is something else using port {options.port}? "
            "Pick another with --port N.",
            file=sys.stderr,
        )
        store.close()
        return 1
    # nit 30: ThreadingHTTPServer defaults daemon_threads to True, so
    # a KeyboardInterrupt/shutdown() can tear the process down mid-
    # request, abandoning a request thread (and its still-open
    # per-thread Store connection, see api.py's Handler._dispatch)
    # without ever running its own cleanup. Non-daemon request
    # threads are joined properly on interpreter/thread-pool
    # teardown instead.
    server.daemon_threads = False
    lock.update(_lock_info(options, port=server.server_port, once=False))

    watcher.start()
    if monthly_job is not None:
        monthly_job.start()
    coaching_job.start()
    try:
        print(f"claudeglass serve: listening on {_url(options.bind, server.server_port)}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.shutdown()
            server.server_close()
    finally:
        if monthly_job is not None:
            monthly_job.stop()
        coaching_job.stop()
        watcher.stop()
        store.close()

    return exit_code


__all__ = ["run", "EXIT_CODE_CHANGED", "STORE_FILENAME", "locked_message", "store_path_for"]

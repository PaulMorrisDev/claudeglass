"""``http.server`` JSON API (v0.2), built against ``service/contracts.py``'s
``ApiHandler``/``MakeHandler`` shapes and the frozen contract in
``docs/api.md``.

:func:`make_handler` is the one entry point this module exports: given an
open :class:`~claudeglass.service.store.Store` and the resolved
:class:`~claudeglass.service.contracts.ServeOptions`, it returns an
``http.server.BaseHTTPRequestHandler`` subclass with every ``/api/*``
route (plus the static-file routes ``docs/ui.md`` describes) bound to
them via closure. Every route function has the exact ``(store, query,
body) -> (status, json_ready_body)`` shape ``contracts.ApiHandler``
declares -- report-backed routes additionally close over ``options`` and
a small in-process report cache rather than taking them as parameters,
since ``ApiHandler`` itself only threads ``store``/``query``/``body``
through.

Two kinds of route:

- **Store-backed** (``/api/health``, ``/api/summary``, ``/api/sessions``,
  ``/api/session/<id>``, ``/api/recache``, ``/api/compactions``,
  ``/api/profiles``, ``/api/baseline``, ``/api/daily-usage``, the two
  ``POST`` routes): read straight from ``store``'s own read queries --
  cheap, always fresh.
- **Report-backed** (``/api/ttl``, ``/api/config-diff``,
  ``/api/recommendations``, ``/api/report.{md,html,json}``): rebuild a
  :class:`~claudeglass.corpus.Corpus` from the store's own
  ``transcripts.digest_blob`` rows via ``service.rebuild.corpus_from_store``
  (a sibling package's module -- imported lazily, inside the function
  that needs it, per this work package's brief, so this module still
  imports cleanly before that module exists) and run it through the same
  :func:`~claudeglass.report.build_report`/
  :func:`~claudeglass.recommend.recommend` pipeline the CLI's
  ``report`` subcommand uses, then a renderer. Rebuilt once per
  ``(window_days, since, until, window_by, project, store-change-token)`` key and cached in-process (see
  ``_ReportCache``) so switching UI tabs (``docs/ui.md``) never re-parses
  the whole store for the same window.

Contract notes / deviations (reported here rather than silently, per this
project's convention -- see e.g. ``report.py``'s own module docstring):

- ``GET /api/session/<id>`` returns ``Store.session()``'s dict verbatim,
  which holds every field ``docs/api.md`` lists for ``/api/sessions``,
  plus (S1-integration fix 1.g)
  ``turn_series``/``markers``/``truncated``, and (v3-limits wiring)
  ``limit_markers``, all from ``Store.turns_for_session``. This is a
  superset, not a contradiction -- ``docs/api.md`` describes it as "the
  session-summary fields above, plus transcripts ... and tags", not an
  exact field count, and dropping fields ``Store`` already computes for
  no privacy reason would only lose information a client might want.
- ``GET /api/profiles/<id>/diff`` renders the real
  ``profiles/diff.py`` computation (v0.3) against the store's own
  *latest* recorded config snapshot (``snapshots.effective_config`` and
  friends) -- not a per-project selection: it takes no ``project``
  filter, unlike config-diff, recommendations and the quick actions'
  "in force now" settings, which follow a picked project. A store with no
  snapshot at all diffs against an empty effective config (nothing
  currently set, nothing managed) and adds a note saying so, rather
  than erroring.
- ``POST /api/profiles`` validates the body via
  ``profiles.schema.load_dict`` (v0.3), writes
  ``<config_dir>/profiles/<id>.toml`` atomically (temp file +
  ``os.replace``, this module's own convention -- see ``cache.py``'s
  ``DigestCache.put``), and re-ingests it into the store immediately
  (rather than waiting for the watcher's next tick) so the response's
  own ``GET /api/profiles`` reflects the write straight away. A
  catalogue id can never be created or overwritten this way -- ``409``
  regardless of ``?replace=1``.
- ``GET /api/report.json``/``.md``/``.html`` are **not** wrapped in the
  ``{"ok": ..., "data": ...}`` envelope on success -- their body is the
  renderer's own native output (``render_json``/``render_markdown``/
  ``render_html``), so ``/api/report.json`` is byte-equivalent to the
  CLI's ``report --json`` for the same window (the parity ``docs/api.md``
  requires). A request error on one of these three routes (a bad
  ``window_days``, or an unexpected exception) still falls back to the
  normal JSON error envelope -- only the success path is raw.
- ``GET /api/config-diff`` reuses the already-assembled report's
  ``"config"`` section (``report.py``'s own ``_build_config_section``,
  capped at 20 changed keys) rather than recomputing
  ``snapshots.build_config_diff_table`` a second time with a
  service-specific session-metrics rebuild -- the CLI's own
  ``config-diff`` subcommand computes session metrics itself only
  because it has no ``build_report`` call to reuse in that code path;
  the service always builds a full report for the same window anyway
  (``/api/ttl``/``/api/recommendations`` need to), so reusing that
  report's own "config" section tables is strictly less duplicated work
  for the identical numbers. A ``key`` naming a config key that did not
  change in this window returns ``{"ok": true, "data": []}`` (an empty,
  valid diff), not an error.
"""

from __future__ import annotations

import hashlib
import html
import importlib.resources
import ipaddress
import json
import mimetypes
import os
import re
import sys
import tempfile
import threading
import time
import urllib.parse
from collections import OrderedDict
from concurrent.futures import Future
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .. import __version__ as _TOOL_VERSION
from .. import baseline as baseline_mod
from .. import capture_catalogue, classify, coaching, discovery, helptext, hook_health, ignores, invocation
from .. import ratings as ratings_mod
from .. import snapshots as snapshots_mod
from ..config import (
    CAPTURE_SAMPLES,
    ConfigError,
    append_habit_log,
    load_config,
    load_session_overrides,
    set_capture,
)
from ..pricing import PricingError, cache_read_savings_usd, load_pricing
from ..profiles import catalogue as profile_catalogue
from ..profiles import diff as profile_diff_mod
from ..profiles import schema as profile_schema
from ..render.html import render_html
from ..render.json_out import render_json, to_jsonable
from ..render.markdown import render_markdown
from ..report import build_report
from ..snapshots import Snapshot
from .contracts import ApiError, CodeState, ServeOptions, WatcherState, WatcherStats

if TYPE_CHECKING:
    from .codewatch import CodeWatch
    from .store import Store

#: Every security header ``docs/api.md`` requires on every response,
#: regardless of route or outcome.
_SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    ("Cache-Control", "no-store"),
    ("X-Content-Type-Options", "nosniff"),
    (
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'",
    ),
)

#: The static folders whose files are pinned by sha256 in
#: ``static/THIRD_PARTY.sha256`` (the vendored d3 and fonts). Their bytes
#: only change when the pin does, so the browser may keep them; every
#: other response stays ``no-store`` (docs/api.md's Security headers).
_PINNED_STATIC_DIRS = frozenset({"vendor", "fonts"})
_PINNED_CACHE_CONTROL = "public, max-age=31536000, immutable"

#: Default report window (calendar days: today and the days before it, see
#: ``_window_query``) for the report-backed routes when ``window_days``
#: isn't given -- matches ``docs/api.md``'s "Accept window_days (default 30)
#: on these routes".
_DEFAULT_WINDOW_DAYS = 30

#: v3: how long a ``service_registered`` probe result is reused before
#: ``/api/health`` runs the platform's own query command again --
#: registration status essentially never changes between requests, and
#: the probe itself shells out to ``schtasks``/``systemctl``/
#: ``launchctl`` (``installer.is_registered``), so this keeps a busy UI
#: polling ``/api/health`` from spawning that process on every refresh.
_SERVICE_REGISTERED_CACHE_TTL_S = 600.0

#: ``/api/health`` calls the scanner stale once no scan has finished for
#: this long (or ten poll intervals, if longer).
_STALE_AFTER_S = 600.0

#: ... and calls one scan stuck once it has run this long (a first read
#: of a large history takes minutes, not hours).
_STUCK_SCAN_S = 3600.0

#: How many built reports (one per window) the service keeps.
_REPORT_CACHE_SIZE = 8

#: A kept report older than this is rebuilt before it is served, rather
#: than served while a rebuild runs (a tab reopened after a long idle
#: shouldn't show figures from hours ago, even briefly).
_STALE_REPORT_MAX_AGE_S = 600.0
#: How long the "since my last change" window's newest change is kept
#: while sessions keep arriving (each would otherwise re-read them).
_LATEST_CHANGE_MAX_AGE_S = 120.0
#: How many projects' newest change are kept (one more per project picked).
_LATEST_CHANGE_CACHE_SIZE = 8
#: How many scopes of the change cards (a project, and how far back the
#: window reaches: ``_impact_reach``) the service keeps.
_IMPACT_CACHE_SIZE = 6
#: How far back "All time" reads for the change cards: the widest window the
#: dashboard's picker offers (core.js WINDOW_OPTIONS), so it lists every
#: change any window does.
_ALL_TIME_REACH_DAYS = 90
#: The most project files ``/api/project-files`` returns (the dearest, prose first).
PROJECT_FILES_SHOWN = 100

_RESTART_ADVICE = "Restart the dashboard: claudeglass install-service, or stop and start serve."

#: How long the Capture tab's estimates (a replay of your last two
#: weeks of sessions) are kept before they are worked out again, in the
#: background: they move slowly, and each is a whole-corpus read.
_CAPTURE_HISTORY_TTL_S = 1800.0
#: How long kept estimates may be served while newer ones are built.
_CAPTURE_HISTORY_MAX_AGE_S = 6 * 3600.0

#: What each free signal's hook logs, by the metric it belongs to
#: (``signals.Signal.event``).
_SIGNAL_METRICS = {"end": "session_end", "wait": "waits", "perm": "permissions"}

#: G5: the largest POST body this server will read off the socket, on
#: any route. Every current POST body (a profile, a tag list, a feedback
#: payload) is small hand-typed or hand-picked JSON -- 64 KB is generous
#: headroom over that, while still bounding the memory and json.loads
#: cost of a body from an untrusted local process (F5/SEC-P6: no auth
#: token gates these routes, only Origin/Sec-Fetch-Site and Host).
_MAX_POST_BODY_BYTES = 64 * 1024


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _clock(ts: str | None) -> str:
    parsed = _parse_utc(ts)
    return parsed.strftime("%H:%M UTC") if parsed is not None else "an unknown time"


def _scan_progress_message(state: WatcherState) -> str:
    if state.phase == "finding" and state.done:
        return (
            f"Scanning your history: found {state.done:,} transcript files so far. "
            "Figures may be incomplete until it finishes."
        )
    if state.phase == "reading" and state.total:
        return (
            f"Scanning your history: read {state.done:,} of {state.total:,} changed files. "
            "Figures may be incomplete until it finishes."
        )
    if state.phase == "storing" and state.total:
        return (
            f"Scanning your history: stored {state.done:,} of {state.total:,} sessions. "
            "Figures may be incomplete until it finishes."
        )
    return "Scanning your history. Figures may be incomplete until it finishes."


def _outdated_message(code: CodeState, *, restarts_itself: bool) -> str:
    versions = ""
    if code.version_on_disk and code.version_on_disk != _TOOL_VERSION:
        versions = f" ({_TOOL_VERSION} is running, {code.version_on_disk} is on disk)"
    message = (
        f"ClaudeGlass's code changed on disk at {_clock(code.changed_at)}{versions}, "
        "so parts of the dashboard may fail until it restarts. "
    )
    if restarts_itself:
        return message + "It restarts by itself within a few minutes; if it doesn't, run claudeglass install-service."
    return message + _RESTART_ADVICE


def _health_status(
    stats: WatcherStats | None,
    state: WatcherState | None,
    *,
    poll_interval_s: float,
    now: datetime | None = None,
    code: CodeState | None = None,
    restarts_itself: bool = False,
) -> tuple[str, str | None]:
    """``/api/health``'s ``(status, message)``: ``"ok"`` (message
    ``None``); ``"outdated"`` once the package's code changed on disk
    (``code``), ahead of everything else, since only a restart helps
    then; ``"starting"`` while the scanner has yet to finish its first
    scan; ``"degraded"`` when its last scan failed outright; ``"stale"``
    when it has stopped, or has not finished a scan for a long while.
    With no ``state`` (no watcher wired in) it is otherwise ``"ok"``."""
    if code is not None and code.changed:
        return "outdated", _outdated_message(code, restarts_itself=restarts_itself)
    if state is None:
        return "ok", None
    now = now or datetime.now(timezone.utc)
    failure = None
    if stats is not None:
        failure = next((m for m in reversed(stats.error_messages) if m.startswith("tick failed: ")), None)
    reason = failure[len("tick failed: "):] if failure else "an error"
    every = f"{poll_interval_s:g} s"

    if not state.running:
        if state.last_success_at is None:
            return "stale", f"The background scanner is not running, so nothing has been read yet. {_RESTART_ADVICE}"
        return (
            "stale",
            f"The background scanner has stopped; figures are as of {_clock(state.last_success_at)}. "
            f"{_RESTART_ADVICE}",
        )
    if state.last_success_at is None:
        if state.last_tick_failed and not state.scanning:
            return "degraded", f"The first scan failed ({reason}). Retrying every {every}."
        return "starting", _scan_progress_message(state)
    if state.last_tick_failed:
        return (
            "degraded",
            f"The last scan failed ({reason}); figures are as of {_clock(state.last_success_at)}. "
            f"Retrying every {every}.",
        )
    stale_after = max(_STALE_AFTER_S, 10 * poll_interval_s)
    last_success = _parse_utc(state.last_success_at)
    if state.scanning:
        started = _parse_utc(state.scan_started_at)
        if started is not None and (now - started).total_seconds() > max(_STUCK_SCAN_S, stale_after):
            return (
                "stale",
                f"A scan has been running since {_clock(state.scan_started_at)} without finishing; "
                f"figures are as of {_clock(state.last_success_at)}. {_RESTART_ADVICE}",
            )
        return "ok", None
    if last_success is not None and (now - last_success).total_seconds() > stale_after:
        return (
            "stale",
            f"No scan has finished since {_clock(state.last_success_at)}, so figures may be out of date. "
            f"{_RESTART_ADVICE}",
        )
    return "ok", None

#: Content types pinned for the dashboard's static files, checked before
#: ``mimetypes`` (which reads the Windows registry, so its answer varies
#: by machine and has no entry for ``.woff2`` on some). A module script
#: served with the wrong type fails to load under ``nosniff``.
_STATIC_CONTENT_TYPES = {
    ".js": "text/javascript",
    ".css": "text/css; charset=utf-8",
    ".woff2": "font/woff2",
    ".svg": "image/svg+xml",
}


def _default_static_dir() -> Traversable:
    """The dashboard's own ``static/`` folder. Read through
    ``importlib.resources``, so it is found inside a zip (the single-file
    ``.pyz``) as well as in a folder: a path built from ``__file__``
    points inside the archive and opens nothing."""
    return importlib.resources.files("claudeglass.service").joinpath("static")


def _static_file(root: Traversable, name: str) -> tuple[Traversable, tuple[str, ...]] | None:
    """The file that ``name`` (the part of the URL after ``/static/``,
    already unquoted) names under ``root``, with its parts, or ``None``
    when it is not one file of the UI: a missing file, a folder, a
    dot-file or dot-folder (an editor's or a tool's own cache, which may
    hold local paths), or a name that steps out of ``root``. ``root`` is
    a folder or a place inside a zip, so a name is checked by its parts
    and not resolved as a path; in a folder a link pointing outside it
    is refused too."""
    parts = tuple(part for part in name.split("/") if part)
    if not parts or any(part.startswith(".") or "\\" in part or ":" in part or "\x00" in part for part in parts):
        return None
    node = root
    for part in parts:
        node = node.joinpath(part)
    if isinstance(root, Path):
        try:
            if root.resolve() not in node.resolve().parents:
                return None
        except (OSError, ValueError, RuntimeError):
            return None
    return (node, parts) if node.is_file() else None


#: index.html's placeholder for the command that runs claudeglass
#: on this install (invocation.py), filled in as the page is served so
#: the dashboard's own commands read right before any API call returns.
_COMMAND_META = b'<meta name="cg-command" content="claudeglass">'

_PLACEHOLDER_INDEX_HTML = (
    "<!DOCTYPE html><html><head><meta charset=\"utf-8\"><title>claudeglass</title>"
    "</head><body>UI not built yet.</body></html>"
)

def _command_meta() -> bytes:
    prefix = html.escape(invocation.command_prefix(), quote=True)
    return b'<meta name="cg-command" content="' + prefix.encode("utf-8") + b'">'


def _report_json_commands(text: str) -> str:
    """/api/report.json with its commands in this install's form. It is
    rendered text (render_json), so it is parsed and re-rendered the same
    way (sorted keys, 2-space indent), and only when the form differs."""
    return invocation.rewrite_rendered(text, "json")


_SESSION_ID_RE = re.compile(r"^/api/session/([^/]+)$")
_SESSION_TAGS_RE = re.compile(r"^/api/sessions/([^/]+)/tags$")
_SESSION_FEEDBACK_RE = re.compile(r"^/api/sessions/([^/]+)/feedback$")
_PROFILE_DIFF_RE = re.compile(r"^/api/profiles/([^/]+)/diff$")
_PROFILE_RE = re.compile(r"^/api/profiles/([^/]+)$")
_SESSION_EXPLAIN_RE = re.compile(r"^/api/session/([^/]+)/explain$")
_CLAUDE_MD_RE = re.compile(r"^/api/claude-md/([0-9a-f]{16})$")
_QUICK_ACTION_RE = re.compile(r"^/api/quick-actions/([a-z0-9-]+)$")
#: A recommendation's key (``Recommendation.key``), as the ignore route
#: accepts it.
_REC_KEY_RE = re.compile(r"^[a-z0-9._:-]{1,200}$")
#: At most this many keys in one ignore request (a rule for many agent
#: types is one item on the dashboard, and one request).
_MAX_IGNORE_KEYS = 100
#: The most plan builds a rating may hold answers for, and the most
#: recommendation cards that may hold an answer at once.
_MAX_PLAN_BUILDS = 32
_MAX_TIP_FEEDBACK = 500

#: ``profiles.diff``'s own ``_VALID_SCOPES`` -- duplicated rather than
#: imported (that name is private) so a scope query param can be
#: validated with a clear 400 before ever reaching ``diff.py``/
#: ``apply_command``, which both raise ``ValueError`` on an unknown one.
_VALID_PROFILE_SCOPES = ("user", "project-local", "repo")


# -- envelope helpers ---------------------------------------------------


def _ok(data) -> tuple[int, dict]:
    return 200, {"ok": True, "data": data}


def _error(status: int, code: str, message: str) -> tuple[int, dict]:
    return status, ApiError(status=status, code=code, message=message).to_envelope()


def _not_found(message: str = "not found") -> tuple[int, dict]:
    return _error(404, "not_found", message)


def _bad_request(message: str) -> tuple[int, dict]:
    return _error(400, "bad_request", message)


def _forbidden(message: str) -> tuple[int, dict]:
    return _error(403, "forbidden", message)


def _internal_error(message: str) -> tuple[int, dict]:
    return _error(500, "internal_error", message)


def _restart_needed(exc: ImportError, code: CodeState | None) -> tuple[int, dict]:
    """A route's ``ImportError``: nearly always a module imported on
    first use that no longer matches the ones loaded at start (see
    ``codewatch.py``). Names the exception's type only; its message
    can carry a path."""
    name = type(exc).__name__
    if code is not None and code.changed:
        message = (
            f"ClaudeGlass's code changed on disk since the dashboard started, so this can't load ({name}). "
            + _RESTART_ADVICE
        )
    else:
        message = (
            f"Part of ClaudeGlass's code couldn't be loaded ({name}), usually because it was updated on disk "
            f"since the dashboard started. {_RESTART_ADVICE} If it keeps happening after a restart, reinstall ClaudeGlass."
        )
    return _error(503, "restart_needed", message)


def _not_implemented(message: str) -> tuple[int, dict]:
    return _error(501, "not_implemented", message)


def _payload_too_large(message: str) -> tuple[int, dict]:
    return _error(413, "payload_too_large", message)


def _int_query(
    query: dict[str, str], key: str, default: int | None, *, minimum: int | None = None
) -> tuple[int | None, tuple[int, dict] | None]:
    """Parse ``query[key]`` as an int, or return ``default`` when absent
    (or an empty string -- an HTML form/query-string convention for
    "unset"). Returns ``(value, None)`` on success, ``(None, error)`` --
    an already-built ``400 bad_request`` response -- otherwise.
    """
    raw = query.get(key)
    if raw is None or raw == "":
        return default, None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None, _bad_request(f"{key!r} must be an integer")
    if minimum is not None and value < minimum:
        return None, _bad_request(f"{key!r} must be >= {minimum}")
    return value, None


def _str_query(query: dict[str, str], key: str) -> str | None:
    """``query[key]`` as a string, or ``None`` when absent/empty -- the
    same "empty string means unset" convention ``_int_query`` uses.
    """
    raw = query.get(key)
    return raw if raw else None


def _parse_iso8601(value: str) -> bool:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _round_iso_to_minute(value: str) -> str:
    """An ISO 8601 timestamp, rounded down to the minute -- the same
    ``%Y-%m-%dT%H:%M:00Z`` form :func:`_named_window_since` returns for a
    named window's own ``since``, so an explicit ``since``/``until`` bound
    a caller passes gets the same minute-level granularity the report
    cache key already gives a named window (see that function and the
    report cache key built around ``_get_report_model``)."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return discovery.utc_stamp(dt.astimezone(timezone.utc))


#: Short windows the dashboard offers by name (``?window=``), each as the
#: phrase that follows an amount.
WINDOW_NAMES = {
    "1h": "in the last hour",
    "today": "today",
    "24h": "in the last 24 hours",
    "change": "since your last change",
    "all": "over all time",
}

#: The named windows whose start slides with the clock: each keeps one
#: report-cache slot under its name (``_slot_for``), so a start that has
#: moved a minute on serves the last report while a new one is built.
#: "today" and "change" start at a fixed moment, so they key on it.
_ROLLING_WINDOWS = ("1h", "24h")

#: ``_config_tz``'s answers: ``config.toml``'s path -> ((mtime, size), tz).
_CONFIG_TZ_KEPT: dict[str, tuple[tuple[int, int], str | None]] = {}


def _config_tz(config_dir: Path | str | None) -> str | None:
    """The ``tz`` name in ``config.toml``, ``None`` (the machine's own
    zone) when it sets none or the file can't be read. A calendar window is
    worked out on every request, so the answer is kept until the file
    changes rather than parsed again each time."""
    if config_dir is None:
        return None
    path = Path(config_dir) / "config.toml"
    try:
        stat = path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        stamp = (0, 0)
    kept = _CONFIG_TZ_KEPT.get(str(path))
    if kept is not None and kept[0] == stamp:
        return kept[1]
    try:
        tz = load_config(config_dir).tz or None
    except Exception:  # noqa: BLE001 -- a config that can't be read means the machine's zone
        tz = None
    _CONFIG_TZ_KEPT[str(path)] = (stamp, tz)
    return tz


def _local_day_or_none(value: str | None, tz) -> str | None:
    """``value`` (an ISO timestamp) as a local ``YYYY-MM-DD`` day in ``tz``,
    ``None`` when it is missing or can't be read."""
    if not value:
        return None
    try:
        return discovery.local_day(value, tz)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _wall_clock_back(now: datetime, days: int, tz) -> str:
    """``now``'s local time of day, ``days`` calendar days earlier, as an
    ISO timestamp in UTC rounded down to the minute: "this time a week
    ago", with a clock change in between counted in."""
    local = discovery.to_local(now, tz)
    wall = datetime.combine(local.date() - timedelta(days=days), local.time())
    zone = discovery._zone(tz)
    moment = wall.replace(tzinfo=zone) if zone is not None else wall.astimezone()
    return discovery.utc_stamp(moment.astimezone(timezone.utc))


def _previous_period(
    query: dict[str, str], window_days: int | None, since: str | None, until: str | None, tz, now: datetime
) -> tuple[str, str] | None:
    """``(since, until)`` of the period of the same length just before the
    window a request names, or ``None`` when it has none: "all time",
    "since my last change" and a ``since``/``until`` the caller picked have
    nothing the same length before them.

    - ``window_days`` N: the same hours N days back. The window is today so
      far and N-1 whole days, so the N days before it end at this time of
      day, N days ago (a whole earlier N days would compare a part-day with
      full ones and show a drop every morning).
    - "today": the same hours yesterday.
    - "1h", "24h": the hour or the 24 hours just before.
    """
    name = _str_query(query, "window")
    if name in _ROLLING_WINDOWS:
        start = _parse_utc(since)
        if start is None:
            return None
        length = timedelta(hours=1 if name == "1h" else 24)
        return discovery.utc_stamp(start - length), since
    if name == "today":
        return discovery.window_start_iso(2, tz, now=now), _wall_clock_back(now, 1, tz)
    if name is None and window_days and until is None and "since" not in query and "until" not in query:
        return discovery.window_start_iso(2 * window_days, tz, now=now), _wall_clock_back(now, window_days, tz)
    return None


def _summary_period(window_days: int | None, since: str | None, until: str | None, tz, now: datetime) -> dict:
    """``/api/summary``'s ``period``: the window's bounds and the local
    calendar days it covers, so the dashboard draws and compares the days
    the figures count and does no zone arithmetic of its own. ``tz`` is the
    IANA name the days were worked out in (``null`` for the machine's own
    zone, or one that can't be found); ``today`` is the day it is now.
    ``window_days`` windows (today and the days before it) cover exactly
    that many days, counted in calendar days so a midnight passing between
    two requests can't add one."""
    first_day = _local_day_or_none(since, tz)
    if window_days and until is None and first_day is not None:
        last_day = (date.fromisoformat(first_day) + timedelta(days=window_days - 1)).isoformat()
    else:
        last_day = _local_day_or_none(until, tz) or discovery.local_day(now, tz)
    return {
        "since": since,
        "until": until,
        "tz": discovery.zone_name(tz),
        "first_day": first_day,
        "last_day": last_day,
        "today": discovery.local_day(now, tz),
    }


def _named_window_since(
    name: str, config_dir: Path | None, now: datetime | None = None, *, latest=None, project: bool = False
) -> tuple[str | None, str]:
    """``(since, "")`` for a named window as an ISO timestamp, rounded
    down to the minute so repeat requests share one cached report, or
    ``(None, reason)`` when it can't be worked out. ``latest``, when
    given, returns the newest change point for the "change" window (the
    service passes one that counts changes only your sessions show).
    ``project`` says a project is picked: with no change that applies
    there, the reason says the project has none (others may)."""
    now = now or datetime.now(timezone.utc)
    if name == "1h":
        start = now - timedelta(hours=1)
    elif name == "24h":
        start = now - timedelta(hours=24)
    elif name == "today":
        start = discovery.window_start(1, _config_tz(config_dir), now=now)
    elif name == "change":
        from .. import change_points

        if latest is not None:
            point = latest()
        else:
            point = change_points.latest(config_dir) if config_dir is not None else None
        if point is None:
            where = " for this project" if project else ""
            return None, (
                f"No change recorded{where} yet. This window starts at your latest `apply` (a profile or a "
                "one-off change), its undo, a settings change the config hook saw, a change to metrics "
                "capture, or a model, effort or CLAUDE.md size change your sessions show."
            )
        start = point.ts
    else:
        return None, f"'window' must be one of {', '.join(WINDOW_NAMES)}"
    return discovery.utc_stamp(start.astimezone(timezone.utc)), ""


def _window_query(
    query: dict[str, str],
    *,
    config_dir: Path | None = None,
    latest=None,
    project: bool = False,
) -> tuple[tuple[int | None, str | None, str | None, str], tuple[int, dict] | None]:
    """Parse the report-backed routes' windowing query params: ``window``
    (a named short window, :data:`WINDOW_NAMES`, resolved to ``since``), or
    ``since``/``until`` (ISO 8601, matching the CLI's own ``report
    --since``/``--until``, ``discovery._resolve_window``'s resolution)
    or ``window_days`` -- never both defaulted at once, mirroring the
    CLI's ``--days``/``--since`` mutually-exclusive argparse group so a
    ``since``/``until`` request isn't silently also clamped to the
    routes' usual 30-day default (docs/api.md's "byte-equivalent to the
    CLI" parity requirement for ``/api/report.*``).

    ``window_days`` is calendar days: today and the days before it, from
    local midnight (``discovery.window_start``, in ``config.toml``'s
    ``tz``, else the machine's zone). It is resolved to ``since`` here, so
    a window's cache key and every figure behind it start at the same
    moment, and a ``since`` the caller gave wins over it (``window_days``
    then comes back ``None``). A returned ``window_days`` always has its
    resolved ``since`` beside it.

    Returns ``((window_days, since, until, window_by), None)`` on
    success, or ``(None, error)`` -- an already-built ``400 bad_request``
    response. ``window_by`` is ``"first-reply"`` for "since your last
    change", whose sessions are the ones that started on the new
    settings, and ``"last-reply"`` otherwise. ``project`` (whether a
    project is picked) goes to :func:`_named_window_since` for its "no
    change" reason.
    """
    name = _str_query(query, "window")
    if name == "all":
        return (None, None, None, "last-reply"), None
    if name is not None:
        since, reason = _named_window_since(name, config_dir, latest=latest, project=project)
        if since is None:
            return None, _bad_request(reason)
        return (None, since, None, "first-reply" if name == "change" else "last-reply"), None
    since = _str_query(query, "since")
    until = _str_query(query, "until")
    for label, value in (("since", since), ("until", until)):
        if value is not None and not _parse_iso8601(value):
            return None, _bad_request(f"{label!r} must be an ISO 8601 timestamp")
    has_since_until = since is not None or until is not None
    default_days = None if has_since_until else _DEFAULT_WINDOW_DAYS
    window_days, err = _int_query(query, "window_days", default_days, minimum=1)
    if err is not None:
        return None, err
    if since is not None:
        window_days = None
    elif window_days is not None:
        try:
            since = discovery.window_start_iso(window_days, _config_tz(config_dir))
        except (OverflowError, OSError, ValueError):
            return None, _bad_request("'window_days' is too large")
    return (window_days, since, until, "last-reply"), None


def _period_text(
    window_days: int | None, since: str | None, until: str | None, window_by: str = "last-reply", *, name: str | None = None
) -> str:
    """The window as a phrase that follows an amount: "over the last 30
    days", "in the last hour", "since 2026-09-20T10:00:00Z", "over all
    time"."""
    if name in WINDOW_NAMES:
        return WINDOW_NAMES[name]
    if window_days and until is None:
        return f"over the last {window_days} days"
    if since or until:
        return _window_label(window_days, since, until)
    return "over all time"


def _window_label(window_days: int | None, since: str | None, until: str | None) -> str:
    """Matches ``cli.py``'s own ``_window_description`` exactly, so
    ``report.meta.window`` in an API-served report is byte-identical to
    the CLI's for the same window (see this module's docstring). A
    ``window_days`` window carries its resolved ``since`` (calendar days
    from local midnight), which the label leaves out: it reads "last 30
    days" as it always has.
    """
    if window_days and until is None:
        return f"last {window_days} days"
    if since or until:
        start = f"since {since}" if since else "since the beginning"
        end = f"until {until}" if until else "until now"
        return f"{start} {end}"
    return "all time"


def _find_section(model, key: str):
    for section in model.sections:
        if section.key == key:
            return section
    return None


def _min_sessions_gate(before: int, after: int, need: int) -> dict | None:
    """P4 leftover: a structured ``{reason, have, need}`` object for a
    minimum-sample-size gate, ``None`` once ``need`` is met on both
    sides -- the emptyState() counterpart to a route's own prose verdict
    (e.g. ``impact.compare``'s ``verdict``/``enough``), which stays as
    is; this is additive, read only from data the caller already built,
    never a substitute for it."""
    have = min(before, after)
    if have >= need:
        return None
    return {"reason": "min_sessions", "have": have, "need": need}


def _impact_reach(
    oldest_recorded: datetime | None, since_dt: datetime | None, now: datetime
) -> tuple[datetime, datetime, str | None]:
    """``(earliest, base, reach_key)``: where the sessions the change cards
    are worked out from start, and the oldest change they list.

    ``base`` is this service's default window back from ``now``, or the
    oldest recorded change when that is older, so the changes only your
    sessions show (``change_points``' EST-P9) are found over at least that.
    A window that starts before ``base`` (``since_dt``) moves it back to the
    start of its day, and ``reach_key`` is that day (``None`` when the
    default reach covers the window): rounding to the day keeps a window
    that slides with the clock from opening a new kept answer every second.
    ``earliest`` is ``base`` less impact's lookback, for the sessions before
    the first change. A change older than ``base`` has its before side cut
    short by ``earliest``, so the cards never list one."""
    from .. import impact as impact_mod

    base = now - timedelta(days=_DEFAULT_WINDOW_DAYS)
    if oldest_recorded is not None and oldest_recorded < base:
        base = oldest_recorded
    reach_key = None
    if since_dt is not None and since_dt < base:
        base = since_dt.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        reach_key = base.isoformat()
    return base - timedelta(days=impact_mod.LOOKBACK_DAYS), base, reach_key


def _change_corpus_since(points) -> str:
    """Where the sessions that change points are worked out from start, as
    the ``since`` ``corpus_from_store`` takes: the default reach of the
    change cards (:func:`_impact_reach`) over the recorded ``points``. The
    impact cards and the "since my last change" window both read from here,
    so the window starts where the newest card's change does."""
    earliest, _base, _key = _impact_reach(min((p.ts for p in points), default=None), None, datetime.now(timezone.utc))
    return discovery.utc_stamp(earliest, seconds=True)


def _change_id(point) -> tuple:
    """What names one change point across two builds of the change list
    from the same store: ``ChangePoint`` isn't hashable, and each build
    makes new objects."""
    return (point.iso(), point.source, point.backup_ts, point.project, point.label, tuple(point.keys))


def _changes_listed(points, base: datetime, since_dt: datetime | None, until_dt: datetime | None) -> list:
    """The change ``points`` (oldest first) a window lists: those made from
    ``base`` on (``_impact_reach``) and inside ``[since_dt, until_dt]``, by
    the second the dashboard shows. A change older than ``base`` is never
    listed, whatever the window says: its before side would be cut short."""
    return [p for p in points if p.ts >= base and discovery.ts_in_window(p.iso(), since_dt, until_dt)]


def _impact_answer(rows: dict, listed: list) -> dict:
    """``/api/impact``'s data: each of ``listed`` (change points, oldest
    first) as its compared row in ``rows``, newest first. The list is new
    every time, so what a request does with it never changes the rows kept."""
    from .. import impact as impact_mod

    return {
        "changes": [rows[_change_id(p)] for p in reversed(listed)],
        "caveat": impact_mod.CAVEAT,
        "min_sessions": impact_mod.MIN_SESSIONS,
        "lookback_days": impact_mod.LOOKBACK_DAYS,
    }


def _backtest_listing(data: dict, since_dt: datetime | None, until_dt: datetime | None) -> dict:
    """``/api/backtest``'s data with only the predictions a window lists: a
    judged one is dated by the change it was matched to, one still waiting
    by when it was logged. ``data`` itself, which is kept, is left as it is."""

    def dated(row: dict) -> str | None:
        return (row.get("change_ts") if row.get("judged_at") else None) or row.get("ts")

    return dict(
        data, predictions=[r for r in data["predictions"] if discovery.ts_in_window(dated(r), since_dt, until_dt)]
    )


# -- make_handler ---------------------------------------------------------


#: Host names every request may carry in its ``Host`` header.
def _is_loopback_address(address: str | None) -> bool:
    """Whether a client address is this machine (an IPv4-mapped IPv6
    loopback counts). Changing metrics capture changes what Claude
    writes, so only this machine may, even when ``serve`` listens on
    more than loopback."""
    if not address:
        return False
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    return ip.is_loopback or (mapped is not None and mapped.is_loopback)


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
#: Wildcard binds: never a name a browser should be sending as ``Host``.
_WILDCARD_BINDS = frozenset({"0.0.0.0", "::", ""})


def allowed_host_names(options: ServeOptions) -> frozenset[str]:
    """The ``Host`` names this server answers to: loopback, the bind
    address when it's a specific one, and ``options.allowed_hosts``.
    Lower-cased, without port or IPv6 brackets."""
    names = set(_LOOPBACK_HOSTS)
    if options.bind not in _WILDCARD_BINDS:
        names.add(options.bind.lower())
    names.update(h.strip().lower().strip("[]") for h in options.allowed_hosts if h.strip())
    return frozenset(names)


def _host_name(header: str) -> str:
    """``Host`` header value -> host name, without port or brackets."""
    value = header.strip().lower()
    if value.startswith("["):
        return value[1 : value.find("]")] if "]" in value else value[1:]
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def make_handler(
    store: "Store",
    options: ServeOptions,
    *,
    watcher_stats: Callable[[], WatcherStats] | None = None,
    static_dir: Traversable | None = None,
    service_registered: Callable[[], bool | None] | None = None,
    watcher_state: Callable[[], WatcherState] | None = None,
    code_watch: "CodeWatch | None" = None,
    restarts_itself: Callable[[], bool] | None = None,
) -> type[BaseHTTPRequestHandler]:
    """Build an ``http.server.BaseHTTPRequestHandler`` subclass with every
    ``/api/*`` route from ``docs/api.md`` bound to ``store``/``options``,
    ready for ``http.server.HTTPServer((options.bind, options.port),
    make_handler(store, options))`` (``contracts.MakeHandler``'s shape).

    ``watcher_stats``, when given, is called on every ``/api/health``
    request for the current :class:`WatcherStats` snapshot -- this is an
    additional keyword-only parameter beyond ``contracts.MakeHandler``'s
    bare ``(store, options)`` signature (a Protocol callable is satisfied
    by an implementation that accepts *extra* optional parameters, so
    this remains a valid ``MakeHandler``); omitted, ``/api/health``
    reports an all-zero :class:`WatcherStats`. ``watcher_state``, when
    given, is called likewise for the watcher's current
    :class:`WatcherState`, from which ``/api/health`` works out its
    ``status``/``message`` (see :func:`_health_status`); omitted, the
    status is always ``"ok"`` and ``scan`` is ``null``.

    ``service_registered``, when given, is called (at most once every
    ``_SERVICE_REGISTERED_CACHE_TTL_S``, per module docstring above) for
    ``/api/health``'s own ``service_registered`` field -- ``True``/
    ``False``/``None`` exactly as it returns them. Omitted (the default,
    and always the case in this module's own tests -- see
    ``installer.py``'s "never touch the machine from a test" posture),
    ``/api/health`` reports ``service_registered: null``, the same
    "unknown, not false" meaning ``installer.is_registered`` itself
    documents. ``service.serve.run`` wires the real
    ``installer.is_registered`` probe in here for an actual ``serve``
    process; this module never imports ``installer.py`` itself, so a
    checkout with only this module's own tests never shells out to
    ``schtasks``/``systemctl``/``launchctl``.

    ``code_watch``, when given, is the ``codewatch.CodeWatch`` that
    ``serve.run`` checks after every watcher tick: ``/api/health``
    reports its :meth:`~CodeWatch.state` as ``code`` (``null`` when
    omitted) and turns ``"outdated"`` once the package's code changed on
    disk, and a route's ``ImportError`` checks it again at once before
    answering ``503 restart_needed``. ``restarts_itself``, when given,
    says whether this process will exit and be started again on the new
    code, so the ``"outdated"`` message can say so; ``serve.run`` turns
    it false when ``--exit-on-code-change`` finds no way to be started
    again. Omitted, ``options.exit_on_code_change`` answers.

    ``static_dir``, when given, overrides the directory the ``/`` and
    ``/static/*`` routes serve from (default: this package's own
    ``service/static/`` -- the UI package's build output, per
    ``docs/ui.md`` -- read through ``importlib.resources``, so it is
    served from inside the single-file ``.pyz`` too). This is a second
    additional keyword-only parameter, added purely so tests can point
    it at a ``tmp_path`` fixture with a real ``index.html``/asset
    without writing anything into the source tree -- the package's own
    ``static/`` is empty at S1-api's own delivery time (a sibling work
    package ships its contents), so this module's own tests exercise
    only the placeholder-index and traversal-protection paths against
    the real default directory.
    """

    static_dir = static_dir if static_dir is not None else _default_static_dir()

    report_lock = threading.Lock()
    #: slot -> the latest report built for it: {"key", "token", "model",
    #: "started" (monotonic), "as_of" (ISO)}, least recently used first.
    #: A slot is the window as asked for: a rolling window ("1h", "24h")
    #: by its name (its resolved start moves every minute), anything else
    #: by its key (a calendar window's start moves at local midnight, "since
    #: my last change" at a new change: each a fresh build).
    report_cache: OrderedDict = OrderedDict()
    #: cache key -> the build in progress for it, which later requests
    #: for the same window wait on (see _get_report_model).
    report_building: dict = {}
    #: rolling window name (``_ROLLING_WINDOWS``) -> the start it last
    #: resolved to, so its key maps back to its slot.
    named_window_starts: dict[str, str] = {}
    #: One background rebuild at a time: each is a whole report build,
    #: and they would only slow each other (and requests) down.
    background_builds = threading.Semaphore(1)
    #: What this request's figures are as of, for the X-Figures-As-Of
    #: header (see Handler._write_headers).
    request_ctx = threading.local()

    #: Every change point, counting the ones only sessions show, as of
    #: one store and one list of recorded changes: {"key", "points", "at"
    #: (monotonic)}. Not tied to a project: each project reads its own
    #: newest change from it.
    change_list_cache: dict = {"key": None, "points": [], "at": 0.0}
    #: The newest change in a project (its raw slugs, or ``None`` for every
    #: project), for the "since my last change" window: project ->
    #: {"key", "point", "at" (monotonic)}, least recently used first.
    latest_change_cache: OrderedDict = OrderedDict()

    def _change_kept(entry: dict, token, point_key, now: float) -> bool:
        """Whether a kept change answer still holds: the recorded changes
        are the same and the store is too, or only sessions have arrived
        and it is under ``_LATEST_CHANGE_MAX_AGE_S`` old. Call with
        ``report_lock`` held."""
        kept_key = entry["key"]
        return kept_key is not None and kept_key[1] == point_key and (
            kept_key[0] == token or now - entry["at"] <= _LATEST_CHANGE_MAX_AGE_S
        )

    def _latest_change(project: tuple[str, ...] | None = None):
        """The newest change point that applies in ``project`` (the raw
        slugs of the picked project, ``None`` for every one), counting a
        model, effort or CLAUDE.md size change only your sessions show
        (``change_points``' EST-P9), so the "since my last change" window
        starts where the impact card's newest change does. A change for one
        project doesn't start the window in another; a change for every
        project starts it everywhere. Reads the sessions from the same
        place the impact cards do (``_change_corpus_since``). While
        sessions keep arriving, the answer is kept for
        ``_LATEST_CHANGE_MAX_AGE_S`` rather than worked out per request."""
        from .. import change_points
        from . import rebuild

        points = change_points.change_points(options.config_dir)
        point_key = tuple((p.iso(), p.source, p.backup_ts) for p in points)
        token = store.change_token()
        now = time.monotonic()
        with report_lock:
            kept = latest_change_cache.get(project)
            if kept is not None and _change_kept(kept, token, point_key, now):
                latest_change_cache.move_to_end(project)
                return kept["point"]
            # A list kept from an earlier store lends its own key and age to
            # the project's answer, so the answer expires when the list would.
            found, list_key, list_at = None, (token, point_key), now
            if _change_kept(change_list_cache, token, point_key, now):
                found, list_key, list_at = change_list_cache["points"], change_list_cache["key"], change_list_cache["at"]
        if found is None:
            corpus = rebuild.corpus_from_store(store, since=_change_corpus_since(points))
            found = change_points.change_points(options.config_dir, corpus)
            with report_lock:
                change_list_cache.update(key=list_key, points=found, at=list_at)
        if project is not None:
            keys = {key for slug in project for key in snapshots_mod.snapshot_project_keys(slug)}
            found = [p for p in found if change_points.applies_to(p, keys)]
        point = found[-1] if found else None
        with report_lock:
            latest_change_cache[project] = {"key": list_key, "point": point, "at": list_at}
            latest_change_cache.move_to_end(project)
            while len(latest_change_cache) > _LATEST_CHANGE_CACHE_SIZE:
                latest_change_cache.popitem(last=False)
        return point

    def _window_query(query, _parse=globals()["_window_query"]):
        # Named windows ("since your last change", "today") need this
        # service's config dir: its apply backups, snapshots and tz. The
        # last change is the picked project's own.
        name = _str_query(query, "window")
        latest, picked = None, False
        if name == "change":
            project, _err = _project_query(query)  # an unknown project is the route's own 400
            latest = lambda: _latest_change(project)  # noqa: E731
            picked = project is not None
        window, err = _parse(query, config_dir=options.config_dir, latest=latest, project=picked)
        if err is None and name in _ROLLING_WINDOWS and window[1] is not None:
            with report_lock:
                named_window_starts[name] = window[1]
        return window, err

    def _default_window():
        """The default window (``_window_query`` with nothing asked for) as
        ``_get_report_model`` takes it, for the routes that explain or price
        a figure on it whatever window is picked: they share the build the
        picker's "30" asks for rather than make their own."""
        return _window_query({})[0]

    def _project_query(query):
        """Parse the additive ``project`` query param (see docs/api.md's
        "Filtering by project"): the redacted slug a caller names,
        resolved to the raw ``sessions.slug`` value(s) it stands for.

        Returns ``(project_slugs, None)`` -- a sorted tuple, for use as
        part of a report cache key -- or ``(None, None)`` when the
        request names no ``project`` at all (no filter); or ``(None,
        error)``, an already-built ``400 bad_request``, when the given
        slug matches no project this store has ever recorded a session
        for. The message never echoes the value back, matching every
        other malformed query param's convention above.
        """
        redacted = _str_query(query, "project")
        if redacted is None:
            return None, None
        raw_slugs = store.resolve_project_slug(redacted)
        if not raw_slugs:
            return None, _bad_request("'project' does not match a known project")
        return tuple(raw_slugs), None

    service_registered_lock = threading.Lock()
    service_registered_cache: dict = {"checked_at": None, "value": None}

    def _cached_service_registered() -> bool | None:
        if service_registered is None:
            return None
        now = time.monotonic()
        with service_registered_lock:
            checked_at = service_registered_cache["checked_at"]
            if checked_at is not None and (now - checked_at) < _SERVICE_REGISTERED_CACHE_TTL_S:
                return service_registered_cache["value"]
        # Deliberately called outside the lock: the probe itself may
        # spawn a process (installer.is_registered's own subprocess
        # call) and take real wall-clock time -- holding the lock across
        # it would serialise every concurrent /api/health request behind
        # one slow probe instead of just letting a rare double-probe
        # happen right at cache expiry.
        value = service_registered()
        with service_registered_lock:
            service_registered_cache["checked_at"] = time.monotonic()
            service_registered_cache["value"] = value
        return value

    # -- report building / caching --------------------------------------

    def _config_mtime_ns() -> int:
        try:
            return (Path(options.config_dir) / "config.toml").stat().st_mtime_ns
        except OSError:
            return 0

    def _cache_token():
        """What a kept report is checked against: the store's content and
        when config.toml last changed (billing, thresholds, capture)."""
        return (store.change_token(), _config_mtime_ns())

    def _snapshots_from_store() -> list[Snapshot]:
        out: list[Snapshot] = []
        for row in store.snapshots():
            try:
                data = json.loads(row["digest_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            # A schema-2 snapshot names its own project (the hook's
            # "slug:<hash>" of its cwd), the key snapshots.snapshot_for
            # joins sessions on, as the CLI reads it from disk. The
            # store's attribution (None for the watcher's machine-wide
            # row) only fills in for a schema-1 snapshot without one;
            # overwriting a real one put every project under
            # "(unknown project)" and left their settings unmatched.
            if not snapshots_mod.records_config(data):
                continue  # apply's active-profile stamp: no config in it
            if not data.get("project_slug"):
                data["project_slug"] = row.get("project_slug")
            out.append(Snapshot(path=Path(""), ts=row["ts"], data=data))
        out.sort(key=lambda s: s.ts)
        return out

    def _latest_config_snapshot() -> Snapshot | None:
        """The newest snapshot that records settings. ``apply`` also
        writes a ``{ts, schema_version, profile_id}`` stamp into the
        snapshots folder to mark the active profile; that stamp carries
        no config, so reading "now" from it would show every key unset."""
        snaps = _snapshots_from_store()
        for snapshot in reversed(snaps):
            if isinstance(snapshot.data.get("effective"), dict):
                return snapshot
        return snaps[-1] if snaps else None

    def _config_snapshot_with_every_project_agents(project=None) -> Snapshot | None:
        """:func:`_latest_config_snapshot` with its agents widened to
        every project's latest snapshot (see
        ``snapshots.with_every_project_agents``), for the "now" value of
        an agent recorded in another project. With a ``project`` filter
        (its raw slugs), only that project's snapshots count, as the
        report's own advice reads them (``report._settings_snapshots``)."""
        snaps = _snapshots_from_store()
        if project:
            snaps = snapshots_mod.snapshots_for_projects(snaps, project)
        canonical = snapshots_mod.canonical_project_keys(store.project_slugs())
        return snapshots_mod.with_every_project_agents(snaps, canonical)

    def _build_report_model(
        window_days: int | None,
        since: str | None = None,
        until: str | None = None,
        window_by: str = "last-reply",
        project: tuple[str, ...] | None = None,
    ):
        # Local import: service.rebuild is a sibling work package's
        # module (S1-watcher), not yet present in every checkout this
        # module is imported from -- see this module's docstring.
        from . import rebuild

        config = load_config(options.config_dir)
        rates = load_pricing(path=config.pricing_path, config_dir=options.config_dir)
        corpus = rebuild.corpus_from_store(
            store,
            days=window_days,
            since=since,
            until=until,
            window_by=window_by,
            project_slugs=list(project) if project else None,
        )
        snaps = _snapshots_from_store()
        # The picked project counts even with no session in the window, so
        # its settings still show (report._settings_snapshots) and the
        # report's meta.projects names it, as the CLI's does for a selected
        # folder.
        projects =tuple(sorted({bundle.slug for bundle in corpus.sessions if bundle.slug} | set(project or ())))
        window = _window_label(window_days, since, until)
        try:
            overrides = load_session_overrides(options.config_dir)
        except ConfigError:
            overrides = {}
        # Finding 7: POST /api/sessions/<id>/tags writes to the store's
        # own session_tags table, but classify.classify_session only ever
        # reads session_overrides (config.toml's sessions.toml). Without
        # this merge a tag write was accepted and stored, yet never
        # changed a single report/UI figure -- merge it into the same
        # overrides dict classify_session already consumes, with a
        # store-set tag (the more recently made edit) taking precedence
        # over a config-file override for the same key.
        overrides = {sid: dict(entry) for sid, entry in overrides.items()}
        for session_id, tags in store.all_tags().items():
            merged = overrides.get(session_id, {})
            merged.update(tags)
            overrides[session_id] = merged
        # The statusline's usage log (usage limits, Claude Code's own
        # cache-miss causes), scoped to this window's sessions exactly as
        # the CLI's report does.
        from .. import statusline as statusline_mod
        from ..discovery import _resolve_window

        since_dt, until_dt = _resolve_window(window_days, since, until)
        usage_log_rows = statusline_mod.scoped_usage_log_rows(
            Path(options.config_dir) / "usage-log.csv",
            {bundle.session_id for bundle in corpus.sessions},
            since_dt,
            until_dt,
        )
        return build_report(
            corpus,
            rates,
            config,
            projects=projects,
            window=window,
            snapshots=snaps or None,
            session_overrides=overrides,
            usage_log_rows=usage_log_rows,
            # Your Sessions-tab ratings, for the Work habits tab.
            ratings=store.all_feedback(),
            tip_feedback=store.tip_feedback(),
            # No project picked: every project's sessions, so an MCP
            # server every project loads can be judged unused.
            all_projects=not project,
            # Every project the store knows, so a project's two
            # drive-letter snapshot keys fold into one even when it has
            # no session in the window.
            known_slugs=tuple(store.project_slugs()),
            # Spend > Usage shows cost by phase. The CLI keeps it behind
            # --phases; here it costs about 1% of the build.
            phases=True,
            # v4 wiring round: without this, waste.WasteStats's salted
            # session-id hash would fall back to report.py's own
            # temp-directory default (see _default_waste_config_dir) --
            # harmless, but this service already has a real, legitimate
            # config_dir of its own, so its salt should live there
            # alongside its other state rather than in the OS temp dir.
            config_dir=options.config_dir,
        )

    def _slot_for(cache_key):
        """The report cache slot for a key (see ``report_cache``). Call
        with ``report_lock`` held."""
        window_days, since, until, window_by, project = cache_key
        # Only a last-reply key: a first-reply one ("since my last change")
        # can start at the very minute "1h" does, and must not be served
        # that window's report.
        if window_days is None and until is None and since is not None and window_by == "last-reply":
            for name, start in named_window_starts.items():
                if start == since:
                    # `project` rides along in the named slot too (rather
                    # than being dropped), so "1h" unfiltered and "1h" for
                    # one project never collide into the same cache entry
                    # even though they'd resolve to the same `since` a
                    # minute apart.
                    return ("named", name, project)
        return cache_key

    def _keep_report(cache_key, token, model, started: float, as_of: str) -> None:
        """Keep a finished build, unless the slot already holds one that
        started later. Call with ``report_lock`` held."""
        slot = _slot_for(cache_key)
        current = report_cache.get(slot)
        if current is not None and current["started"] > started:
            return
        report_cache[slot] = {"key": cache_key, "token": token, "model": model, "started": started, "as_of": as_of}
        report_cache.move_to_end(slot)
        while len(report_cache) > _REPORT_CACHE_SIZE:
            report_cache.popitem(last=False)

    def _note_as_of(as_of: str, refreshing: bool) -> None:
        """Record this request's figures' age for its response headers
        (the oldest, when one request reads several reports)."""
        current = getattr(request_ctx, "as_of", None)
        if current is None or as_of < current[0]:
            request_ctx.as_of = (as_of, refreshing)
        elif refreshing:
            request_ctx.as_of = (current[0], True)

    def _build_and_keep(cache_key, building: Future):
        token = _cache_token()
        started = time.monotonic()
        as_of = _now_utc_iso()
        try:
            model = _build_report_model(*cache_key)
        except BaseException as exc:
            with report_lock:
                report_building.pop(cache_key, None)
            building.set_exception(exc)
            raise
        with report_lock:
            report_building.pop(cache_key, None)
            _keep_report(cache_key, token, model, started, as_of)
        building.set_result(model)
        return model, as_of

    def _rebuild_in_background(cache_key, building: Future) -> None:
        def run():
            try:
                with background_builds:
                    _build_and_keep(cache_key, building)
            except BaseException as exc:  # noqa: BLE001 -- the next request retries; waiters see it via the Future
                print(f"claudeglass serve: your report was not refreshed: {exc}", file=sys.stderr, flush=True)
            finally:
                store.close()  # this thread's own connection

        threading.Thread(target=run, name="claudeglass-report", daemon=True).start()

    def _get_report_model(
        window_days: int | None,
        since: str | None = None,
        until: str | None = None,
        window_by: str = "last-reply",
        project: tuple[str, ...] | None = None,
    ):
        """The report for a window, built at most once per store change.

        Stale-while-revalidate: when the store has changed since the
        window's report was built (a live session writes every few
        seconds), the kept report is served at once and a rebuild starts
        in the background, so a tab never waits on a whole report build
        just because a transcript grew. Only a window with nothing kept
        (or a report older than ``_STALE_REPORT_MAX_AGE_S``) is built
        while the request waits, and requests for a window already being
        built wait on that one build rather than starting their own.

        ``project`` (additive, project-filter work): the resolved raw
        project slug(s) a ``project`` query param named, or ``None`` for
        no filter -- part of the cache key (below) so two different
        ``project`` values for the same window never share a report.
        """
        # Cache key widened from a bare window_days to the full
        # (window_days, since, until, window_by, project) tuple so a
        # since/until or project-filtered request never collides with (or
        # is served from) an unfiltered entry for the same store
        # change_token.
        cache_key = (window_days, since, until, window_by, project)
        token = _cache_token()
        now = time.monotonic()
        with report_lock:
            slot = _slot_for(cache_key)
            kept = report_cache.get(slot)
            if kept is not None:
                report_cache.move_to_end(slot)
                if kept["key"] == cache_key and kept["token"] == token:
                    _note_as_of(kept["as_of"], False)
                    return kept["model"]
                if now - kept["started"] > _STALE_REPORT_MAX_AGE_S:
                    kept = None
            building = report_building.get(cache_key)
            owner = building is None
            if owner:
                building = report_building[cache_key] = Future()
        if kept is not None:
            # Serve what is kept; refresh it behind the scenes.
            if owner:
                _rebuild_in_background(cache_key, building)
            _note_as_of(kept["as_of"], True)
            return kept["model"]
        if not owner:
            model = building.result()
            with report_lock:
                kept = report_cache.get(_slot_for(cache_key))
            _note_as_of(kept["as_of"] if kept is not None else _now_utc_iso(), False)
            return model
        model, as_of = _build_and_keep(cache_key, building)
        _note_as_of(as_of, False)
        return model

    # -- store-backed routes ---------------------------------------------

    def route_health(store, query, body):
        last_stats = watcher_stats() if watcher_stats is not None else None
        state = watcher_state() if watcher_state is not None else None
        stats = last_stats or WatcherStats()
        code = code_watch.state() if code_watch is not None else None
        status, message = _health_status(
            last_stats,
            state,
            poll_interval_s=options.poll_interval_s,
            code=code,
            restarts_itself=restarts_itself() if restarts_itself is not None else options.exit_on_code_change,
        )
        try:
            capture = _capture_health()
        except Exception:  # noqa: BLE001 -- health answers even when this part can't (code changed on disk, say)
            capture = None
        data = {
            # "ok", "outdated" (the code changed on disk), "starting"
            # (first scan still running), "degraded" (the last scan
            # failed) or "stale" (the scanner stopped, or nothing has
            # finished for a long while); ``message`` says what that
            # means in plain words, null when ok.
            "status": status,
            "message": message,
            # Where the scanner is right now (its progress through a
            # scan, when the last one finished); ``watcher`` below is
            # what its last finished scan did.
            "scan": to_jsonable(state) if state is not None else None,
            # The running code's version, so "is the dashboard still on
            # the old version after an update?" has a one-look answer.
            "version": _TOOL_VERSION,
            # Whether the package's files on disk still match the code
            # this process loaded (codewatch.py): an update that landed
            # without a restart shows here, and as status "outdated".
            "code": to_jsonable(code) if code is not None else None,
            "schema_version": store.schema_version() or 0,
            "watcher": to_jsonable(stats),
            # Finding 3: a transcript whose file has gone missing (past
            # Claude Code's own cleanupPeriodDays, or simply deleted) is
            # marked rather than removed -- surfacing the running total
            # here lets an operator notice a projects-root misconfiguration
            # (everything suddenly "missing") without it being silent.
            "transcripts_missing": store.count_missing_transcripts(),
            # v3: whether `serve` is registered to start at logon/boot
            # (installer.py) -- true/false when the platform's own query
            # command gave a clear answer, null when it couldn't be run
            # at all (no probe wired up, an unsupported platform, or the
            # query tool itself missing). Never a raw path -- a plain
            # boolean, per this route's existing privacy posture.
            "service_registered": _cached_service_registered(),
            # Metrics capture as set in config.toml, and whether
            # settings.json runs the hooks it needs, for the banner on
            # every tab. Cheap: no transcript is read here (the costs
            # are /api/capture's). null when config.toml can't be read.
            "capture": capture,
        }
        return _ok(data)

    # -- metrics capture ---------------------------------------------------

    def _capture_health() -> dict | None:
        from .. import capture_view

        try:
            capture = load_config(options.config_dir).capture
        except (ConfigError, OSError, ValueError):
            return None
        block = capture_view.config_block(capture)
        # config_dir=: cheap (a few small hook files hashed, no subprocess,
        # no transcript read) but still catches an outdated or hand-edited
        # hook file (SEC-P7/ROB-P7), not just a missing settings.json entry.
        hooks = hook_health.check_capture(hook_health.capture_specs(capture.hook_metrics()), config_dir=options.config_dir)
        block["hooks_ok"] = hooks.ok
        return block

    #: name -> {"key", "soft", "data", "started", "as_of", "building"}:
    #: the parts of /api/capture that read transcripts, kept like the
    #: report (see _capture_part).
    capture_parts: dict = {}

    #: (session id, its transcripts' latest parse, threshold) -> the pieces
    #: of work in that session that are due a rating, none for an empty
    #: list (ratings.unrated_pieces), so the banner reads again only the
    #: sessions that changed since it was last built.
    unrated_memo: dict = {}

    def _keep_capture_part(name, key, soft, build):
        started = time.monotonic()
        as_of = _now_utc_iso()
        data = build()
        with report_lock:
            kept = capture_parts.get(name)
            if kept is None or started >= kept["started"]:
                capture_parts[name] = {
                    "key": key, "soft": soft, "data": data, "started": started, "as_of": as_of, "building": False,
                }
        return data, as_of

    def _capture_part(name: str, key, soft, build, max_age: float):
        """``build()``'s result, kept like the report (see
        _get_report_model): the kept one when its ``key`` still matches;
        when only ``soft`` does (the same window, a newer store) and it
        is under ``max_age`` seconds old, the kept one while a new one is
        built in the background; otherwise one built now."""
        now = time.monotonic()
        refresh = False
        with report_lock:
            kept = capture_parts.get(name)
            if kept is not None and kept["key"] == key:
                _note_as_of(kept["as_of"], False)
                return kept["data"]
            if kept is not None and kept["soft"] == soft and now - kept["started"] <= max_age:
                refresh = not kept["building"]
                if refresh:
                    kept["building"] = True
                _note_as_of(kept["as_of"], True)
                data = kept["data"]
            else:
                kept = None
        if kept is not None:
            if refresh:

                def run():
                    try:
                        with background_builds:
                            _keep_capture_part(name, key, soft, build)
                    except BaseException as exc:  # noqa: BLE001 -- the next request retries
                        print(
                            f"claudeglass serve: your capture figures were not refreshed: {exc}",
                            file=sys.stderr,
                            flush=True,
                        )
                    finally:
                        with report_lock:
                            current = capture_parts.get(name)
                            if current is not None:
                                current["building"] = False
                        store.close()

                threading.Thread(target=run, name=f"claudeglass-capture-{name}", daemon=True).start()
            return data
        data, as_of = _keep_capture_part(name, key, soft, build)
        _note_as_of(as_of, False)
        return data

    def _capture_rates(config):
        try:
            return load_pricing(path=config.pricing_path, config_dir=options.config_dir)
        except (PricingError, OSError, ValueError):
            return None

    def _capture_history(config):
        """``(history, units)`` from your last two weeks of sessions, in
        every project the store holds; ``(None, units)`` when the rate
        card can't be read."""
        from .. import capture as capture_mod
        from ..report import _report_units
        from ..units import Units
        from . import rebuild

        rates = _capture_rates(config)
        if rates is None:
            return None, Units(billing_mode=config.billing)
        corpus = rebuild.corpus_from_store(store, days=capture_mod.HISTORY_DAYS)
        past = capture_mod.history(corpus, rates, days=capture_mod.HISTORY_DAYS)
        return past, _report_units(corpus, rates, config, options.config_dir)

    def _capture_usage(config, enabled_at: str):
        """``(usage, sessions started since, signal sessions by metric,
        cost a week, what depends on capture or your feedback a week)``
        from ``enabled_at`` on."""
        from .. import capture as capture_mod
        from .. import habits as habits_mod
        from .. import signals as signals_mod
        from ..corpus import _session_first_ts
        from ..discovery import _parse_bound
        from ..report import _capture_signals
        from . import rebuild

        rates = _capture_rates(config)
        corpus = rebuild.corpus_from_store(store, since=enabled_at)
        use = capture_mod.usage(corpus, rates, since=enabled_at)
        weekly_cost = capture_mod.weekly_cost(use)
        dependent_value = habits_mod.capture_dependent_value(
            habits_mod.collect(
                corpus, rates, ratings=store.all_feedback(), signals=_capture_signals(corpus, options.config_dir),
                tz=config.tz,
            )
        )
        start = _parse_bound(enabled_at)
        started = 0
        for bundle in corpus.sessions:
            first = _session_first_ts(bundle)
            try:
                if first and _parse_bound(first) >= start:
                    started += 1
            except ValueError:
                continue
        seen: dict[str, set] = {}
        for signal in signals_mod.load(options.config_dir, since=start):
            metric_id = _SIGNAL_METRICS.get(signal.event)
            if metric_id is not None:
                seen.setdefault(metric_id, set()).add(signal.session_hash)
        return (
            use, started, {metric_id: len(hashes) for metric_id, hashes in seen.items()}, weekly_cost,
            dependent_value,
        )

    def _capture_feedback(config):
        """``capture.feedback_usage`` over the replayed days: your
        /cg-feedback runs, whatever the capture level."""
        from .. import capture as capture_mod
        from . import rebuild

        corpus = rebuild.corpus_from_store(store, days=capture_mod.HISTORY_DAYS)
        return capture_mod.feedback_usage(corpus, _capture_rates(config))

    def _capture_coaching(config):
        """``capture.coaching_usage`` over the replayed days: the coaching
        notes the capture hook added, whatever the capture level."""
        from .. import capture as capture_mod
        from . import rebuild

        since = (datetime.now(timezone.utc) - timedelta(days=capture_mod.HISTORY_DAYS)).isoformat(timespec="seconds")
        corpus = rebuild.corpus_from_store(store, days=capture_mod.HISTORY_DAYS)
        return capture_mod.coaching_usage(corpus, _capture_rates(config), since=since)

    def _capture_overhead(config, specs, window):
        """``capture_view.build_overhead`` over ``window``'s sessions: how
        often ClaudeGlass's hooks ran, and what capture's notes and the
        coaching notes cost, in the same stretch."""
        from .. import capture_view
        from ..report import _report_units
        from ..units import Units
        from . import rebuild

        rates = _capture_rates(config)
        corpus = rebuild.corpus_from_store(store, **window["corpus"])
        units = (
            _report_units(corpus, rates, config, options.config_dir)
            if rates is not None
            else Units(billing_mode=config.billing)
        )
        return capture_view.build_overhead(corpus, specs, rates, units, window)

    def _reminder_threshold(config) -> int:
        """How many tokens a piece of work needs for the rating reminder,
        from the same thresholds and typical piece the hook reads."""
        return ratings_mod.reminder_tokens(
            ratings_mod.coaching_thresholds(config.thresholds, coaching.read(options.config_dir)), _typical_piece()
        )

    def _capture_unrated(threshold: int) -> dict:
        """The pieces of work, in sessions of the last ``coaching.DAYS``
        days, of at least ``threshold`` tokens (the rating reminder's size)
        that you have not rated, on the dashboard or with a /cg-feedback
        run: newest first, at most ``ratings.BANNER_LIMIT``, with the count
        of them all. A session holding several pieces lists each one, and a
        piece is the stretch of a session that ``pieces.pieces_of`` draws:
        one that carries on from an earlier session counts only what ran in
        this one. The stored total prefilters (it counts subagents too);
        the main transcript's own tokens decide."""
        from . import rebuild

        since = (datetime.now(timezone.utc) - timedelta(days=coaching.DAYS)).isoformat(timespec="seconds")
        candidates = {row["id"]: row for row in store.unrated_sessions(min_tokens=threshold, since=since)}

        def memo_key(session_id):
            return (session_id, candidates[session_id]["stamp"], threshold)

        for key in list(unrated_memo):
            if key[0] not in candidates or key != memo_key(key[0]):
                unrated_memo.pop(key, None)
        fresh = [session_id for session_id in candidates if memo_key(session_id) not in unrated_memo]
        if fresh:
            found = {
                bundle.session_id: ratings_mod.unrated_pieces(bundle, threshold)
                for bundle in rebuild.corpus_from_store(store, session_ids=fresh).sessions
            }
            for session_id in fresh:
                unrated_memo[memo_key(session_id)] = found.get(session_id, [])
        pieces = []
        for session_id, row in candidates.items():
            for piece in unrated_memo.get(memo_key(session_id)) or []:
                pieces.append(
                    {
                        "session_id": session_id,
                        "slug": row["slug"],
                        "last_ts": piece["end_ts"] or row["last_ts"],
                        "tokens": piece["tokens"],
                        "tokens_text": ratings_mod.tokens_text(piece["tokens"]),
                        "part": piece["part"],
                        "label": piece["label"],
                    }
                )
        pieces.sort(key=lambda piece: (piece["last_ts"] or "", piece["part"]), reverse=True)
        return {
            "threshold": threshold,
            "threshold_text": ratings_mod.tokens_text(threshold),
            "total": len(pieces),
            "pieces": pieces[: ratings_mod.BANNER_LIMIT],
        }

    def _capture_view(config) -> dict:
        from .. import capture_view

        capture = config.capture
        soft = (config.billing, str(config.pricing_path))
        bucket = int(time.time() // _CAPTURE_HISTORY_TTL_S)
        # A replay taken while the first scan was still filling the store
        # is short of sessions: replay again once that scan has finished.
        state = watcher_state() if watcher_state is not None else None
        scanned = state is None or state.last_success_at is not None
        past, units = _capture_part(
            "history",
            (bucket, scanned, *soft),
            (scanned, *soft),
            lambda: _capture_history(config),
            _CAPTURE_HISTORY_MAX_AGE_S,
        )
        use, started, signal_sessions, weekly_cost, dependent_value = None, 0, {}, None, None
        if capture.is_on and capture.enabled_at:
            use, started, signal_sessions, weekly_cost, dependent_value = _capture_part(
                "usage",
                (store.change_token(), capture.enabled_at, *soft),
                (capture.enabled_at, *soft),
                lambda: _capture_usage(config, capture.enabled_at),
                _STALE_REPORT_MAX_AGE_S,
            )
        feedback_use = skill = ratings = None
        if "feedback_skill" in capture.feedback:
            from .. import footprint

            token = store.change_token()
            feedback_use = _capture_part(
                "feedback", (token, *soft), soft, lambda: _capture_feedback(config), _STALE_REPORT_MAX_AGE_S
            )
            skill = footprint.feedback_skill_state()
        coaching_use = None
        if capture.coaching_notes_on:
            token = store.change_token()
            coaching_use = _capture_part(
                "coaching", (token, *soft), soft, lambda: _capture_coaching(config), _STALE_REPORT_MAX_AGE_S
            )
        brief_skill = None
        if "brief_templates" in capture.coaching:
            from .. import footprint

            brief_skill = footprint.skill_state(capture_catalogue.BRIEF_SKILL)
        if "dashboard_rating" in capture.feedback:
            ratings = store.feedback_count()
        unrated = None
        if {"dashboard_rating", "feedback_reminder"} & set(capture.feedback):
            # What the rating reminder would say needs a rating: the
            # banner lists those sessions, so a rating given there or in
            # /cg-feedback takes them off it. The key holds the threshold
            # and the store's change token. It is also the soft key, so a
            # kept list is never served for a newer store: a session you
            # have just rated must not stay on it while a new one is built.
            # The half-hour bucket lets a session that ages out of the window
            # leave the list even when nothing new is stored.
            threshold = _reminder_threshold(config)
            unrated_key = (store.change_token(), threshold, bucket, *soft)
            unrated = _capture_part(
                "unrated", unrated_key, unrated_key, lambda: _capture_unrated(threshold), _STALE_REPORT_MAX_AGE_S
            )
        statusline = None
        if "feedback_note" in capture.feedback or "coaching_line" in capture.coaching:
            from .. import footprint

            try:
                settings = json.loads(hook_health.settings_path().read_text(encoding="utf-8"))
            except (OSError, ValueError):
                settings = None
            statusline = footprint.is_own_statusline(settings if isinstance(settings, dict) else None)
        # Where your sessions ran decides whether a status line runs at all.
        entrypoints = store.entrypoint_counts() if statusline is not None else None
        overhead = None
        specs = hook_health.installed_specs()
        if specs:
            window = capture_view.overhead_window(capture)
            where = tuple(sorted(window["corpus"].items()))
            overhead = _capture_part(
                "overhead",
                (store.change_token(), bucket, where, specs, *soft),
                (where, specs, *soft),
                lambda: _capture_overhead(config, specs, window),
                _STALE_REPORT_MAX_AGE_S,
            )
        hooks = hook_health.check_capture(
            hook_health.capture_specs(capture.hook_metrics()), config_dir=options.config_dir
        )
        return capture_view.view(
            capture, past=past, units=units, use=use, hooks=hooks, signal_sessions=signal_sessions,
            started_since=started, feedback_use=feedback_use, skill=skill, brief_skill=brief_skill, ratings=ratings,
            statusline=statusline, weekly_cost=weekly_cost, dependent_value=dependent_value,
            coaching_use=coaching_use, unrated=unrated, entrypoints=entrypoints, overhead=overhead,
        )

    def _capture_conflict(message: str, commands: list[str]) -> tuple[int, dict]:
        """409 with the commands that make the change from a terminal."""
        return 409, {"ok": False, "error": {"code": "conflict", "message": message, "commands": commands}}

    def route_capture(store, query, body):
        """The Capture tab and the banner: every metric with what it
        captures and why, what each level and metric would cost you, and
        what capture has cost since it was turned on."""
        try:
            config = load_config(options.config_dir)
        except ConfigError:
            return _capture_conflict(
                "config.toml can't be read, so metrics capture can't be shown. "
                "'claudeglass capture status' says what is wrong with it.",
                ["claudeglass capture status"],
            )
        return _ok(_capture_view(config))

    def _capture_changes(body) -> tuple[dict | None, str | None]:
        """``set_capture`` arguments from a POST body, or a reason it's
        refused."""
        if not isinstance(body, dict) or not body:
            return None, (
                "request body must be a JSON object with one or more of: level, metrics, sample, until, feedback, "
                "coaching, tagger"
            )
        allowed = ("level", "metrics", "sample", "until", "feedback", "coaching", "tagger")
        unknown = sorted(set(body) - set(allowed))
        if unknown:
            return None, f"unknown key {', '.join(unknown)}; allowed: {', '.join(allowed)}"
        if "level" in body and "metrics" in body:
            return None, "give 'level' (a preset) or 'metrics' (your own pick), not both"
        changes: dict = {}
        if "level" in body:
            if body["level"] not in capture_catalogue.LEVELS:
                return None, f"'level' must be one of: {', '.join(capture_catalogue.LEVELS)}"
            changes["level"] = body["level"]
        for key, known in (
            ("metrics", capture_catalogue.LEVEL_METRIC_IDS),
            ("feedback", capture_catalogue.FEEDBACK_IDS),
            ("coaching", capture_catalogue.COACHING_IDS),
        ):
            if key not in body:
                continue
            value = body[key]
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                return None, f"'{key}' must be a list of metric ids"
            bad = [v for v in value if v not in known]
            if bad:
                return None, f"'{key}' has unknown id {', '.join(bad)}; known: {', '.join(known)}"
            changes[key] = list(dict.fromkeys(value))
        if "sample" in body:
            sample = body["sample"]
            if isinstance(sample, bool) or sample not in CAPTURE_SAMPLES:
                return None, f"'sample' must be one of: {', '.join(str(s) for s in CAPTURE_SAMPLES)}"
            changes["sample"] = sample
        if "until" in body:
            until = body["until"]
            if not isinstance(until, str):
                return None, "'until' must be an ISO-8601 time, or \"\" for no end"
            if until:
                from ..discovery import _parse_bound

                try:
                    stop = _parse_bound(until)
                except ValueError:
                    return None, "'until' must be an ISO-8601 time, or \"\" for no end"
                if stop <= datetime.now(timezone.utc):
                    return None, "'until' is in the past"
            changes["until"] = until
        if "tagger" in body:
            if body["tagger"] not in capture_catalogue.TAGGERS:
                return None, f"'tagger' must be one of: {', '.join(capture_catalogue.TAGGERS)}"
            changes["tagger"] = body["tagger"]
        return changes, None

    def route_capture_post(store, query, body):
        """Change ``[capture]`` in this tool's own config.toml. Never
        touches Claude Code's settings: hook entries a new metric needs
        are added by 'claudeglass capture connect', which the
        response names."""
        from .. import capture_view

        if not _is_loopback_address(getattr(request_ctx, "client", None)):
            return _forbidden(
                "metrics capture can only be changed from this machine; run 'claudeglass capture' there"
            )
        changes, reason = _capture_changes(body)
        if reason is not None:
            return _bad_request(reason)
        try:
            config = load_config(options.config_dir)
        except ConfigError:
            return _capture_conflict(
                "config.toml can't be read, so the change wasn't saved. "
                "'claudeglass capture status' says what is wrong with it.",
                ["claudeglass capture status"],
            )
        before = config.capture
        try:
            after = set_capture(options.config_dir, **changes)
        except (ConfigError, OSError):
            return _capture_conflict(
                "The change couldn't be saved to config.toml here. Make it from a terminal instead.",
                capture_view.change_commands(before, changes),
            )
        try:
            config = load_config(options.config_dir)
        except ConfigError:
            pass
        data = _capture_view(config)
        data["changed"] = after != before
        return _ok(data)

    def route_summary(store, query, body):
        # v0.4 windowing (dashboard delta support): the same window
        # params every report-backed route accepts, plus since/until --
        # but, unlike those routes, no params at all still means "all
        # time" (docs/api.md's original contract), matching how
        # /api/sessions and /api/compactions already treat "nothing
        # given" via _listing_window rather than _window_query's own
        # 30-day default.
        window, err = _listing_window(query)
        if err is not None:
            return err
        window_days, since, until, window_by = window
        project, err = _project_query(query)
        if err is not None:
            return err
        previous, err = _int_query(query, "previous", 0, minimum=0)
        if err is None and previous not in (0, 1):
            err = _bad_request("'previous' must be 0 or 1")
        if err is not None:
            return err
        tz = _config_tz(options.config_dir)
        now = datetime.now(timezone.utc)
        # Round explicit bounds to the minute the same way a named
        # window's own `since` already is (_named_window_since) -- so two
        # requests for "the same" explicit period a few seconds apart
        # agree exactly. (The dashboard's previous period is `previous=1`,
        # resolved below by _previous_period, not a second since/until call.)
        if since is not None:
            since = _round_iso_to_minute(since)
        if until is not None:
            until = _round_iso_to_minute(until)
        if previous:
            # The period of the same length just before this window, with
            # its own `period`: the dashboard's deltas read both.
            try:
                bounds = _previous_period(query, window_days, since, until, tz, now)
            except (OverflowError, OSError, ValueError):
                return _bad_request("'window_days' is too large")
            if bounds is None:
                return _bad_request("This window has no earlier period of the same length")
            window_days, window_by = None, "last-reply"
            since, until = bounds
        result = store.summary(
            window_days=window_days, since=since, until=until, project_slugs=project, window_by=window_by
        )
        # Additive: what cache reads saved against sending the same
        # tokens fresh as input, from turns_agg in the same window.
        config = load_config(options.config_dir)
        rates = _capture_rates(config)
        by_model = store.cache_read_tokens_by_model(
            days=window_days, since=since, until=until, project_slugs=project, window_by=window_by
        )
        result["cache_read_tokens"] = sum(by_model.values())
        result["cache_saved"] = (
            cache_read_savings_usd(
                [{"model": model_id, "cache_read_tokens": tokens} for model_id, tokens in by_model.items()],
                rates,
            )
            if rates is not None
            else 0.0
        )
        # Additive: the calendar days this window covers, as the server's
        # zone reads them, so a chart draws the same days the figures
        # count (see _summary_period).
        result["period"] = _summary_period(window_days, since, until, tz, now)
        return _ok(result)

    def _listing_window(query):
        """The window a store listing is limited to: none unless the
        request names one (``window``, ``window_days``, ``since`` or
        ``until``), then the same one the report uses."""
        if not any(key in query for key in ("window", "window_days", "since", "until")):
            return (None, None, None, "last-reply"), None
        return _window_query(query)

    def _low_confidence(row, tags=None) -> bool:
        """Whether a session's mode or purpose is the catch-all a rule fell
        back to, so the dashboard can show a chip on it (additive). A label
        you set on the dashboard (``tags``) is yours from then on, even
        before the next scan writes it to the session."""
        tags = tags or {}
        return classify.is_low_confidence(
            row.get("mode") or "",
            "override" if tags.get("mode") else row.get("mode_source") or "",
            row.get("purpose") or "",
            "override" if tags.get("purpose") else row.get("purpose_source") or "",
        )

    def _typical_piece() -> int:
        """The median piece of work in ``coaching.json``, or 0 before it
        is known."""
        value = coaching.read(options.config_dir).get("typical_piece_tokens")
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0

    def _rating_facts(store, session_id):
        """``(facts, builds)`` for a session's rating questions, worked out
        from its stored transcripts (``ratings.session_facts``); ``(None,
        [])`` when no top-level transcript is stored."""
        from . import rebuild

        corpus = rebuild.corpus_from_store(store, session_ids=[session_id])
        bundle = next((b for b in corpus.sessions if b.top is not None), None)
        if bundle is None:
            return None, []
        return ratings_mod.session_facts(bundle, _typical_piece()), ratings_mod.session_builds(bundle)

    def route_sessions(store, query, body):
        limit, err = _int_query(query, "limit", 50, minimum=0)
        if err is not None:
            return err
        offset, err = _int_query(query, "offset", 0, minimum=0)
        if err is not None:
            return err
        window, err = _listing_window(query)
        if err is not None:
            return err
        window_days, since, until, window_by = window
        project, err = _project_query(query)
        if err is not None:
            return err
        rows = store.sessions(
            limit=limit,
            offset=offset,
            window_days=window_days,
            since=since,
            until=until,
            project_slugs=project,
            window_by=window_by,
        )
        # Additive: the local days a session's first and last replies fall
        # on, so the day a chart column names filters to the same sessions
        # (null when the session has no timestamp).
        zone = discovery._zone(_config_tz(options.config_dir))
        tags = store.all_tags()
        for row in rows:
            row["first_day"] = _local_day_or_none(row.get("first_ts"), zone)
            row["last_day"] = _local_day_or_none(row.get("last_ts"), zone)
            row["low_confidence"] = _low_confidence(row, tags.get(row["id"]))
        return _ok(rows)

    def route_session(store, query, body):
        session_id = query.get("id", "")
        result = store.session(session_id)
        if result is None:
            return _not_found("session not found")
        # S1-integration fix 1.g: per-turn context/cache series for the
        # session-timeline chart, sourced from the top-level transcript's
        # stored digest -- see Store.turns_for_session's own docstring
        # for the exact shape. None (no stored top-level transcript --
        # shouldn't normally happen for a session store.session() found)
        # simply omits both keys rather than sending an empty shape.
        turns = store.turns_for_session(session_id)
        if turns is not None:
            result["turn_series"] = turns["turn_series"]
            result["markers"] = turns["markers"]
            # Finding 11: a very long session's turn_series is
            # downsampled server-side; tell the UI so it can say so
            # rather than silently rendering a thinned-out chart.
            result["truncated"] = turns["truncated"]
            # v3-limits wiring: usage-cap pause/resume/agent-terminated
            # markers for the session-timeline chart, alongside the
            # existing compactions/spawns/human markers above.
            result["limit_markers"] = turns["limit_markers"]
        # Metrics-capture feedback: the questions to rate it with, while
        # the dashboard rating is switched on (the Capture tab).
        try:
            rating_on = "dashboard_rating" in load_config(options.config_dir).capture.feedback
        except ConfigError:
            rating_on = False
        result["low_confidence"] = _low_confidence(result, result["tags"])
        if rating_on:
            # The same questions /cg-feedback asks, filled and left out by
            # the same facts (ratings.question_rows); counts and ids only.
            facts, builds = _rating_facts(store, session_id)
            result["feedback_facts"] = facts
            result["feedback_questions"] = ratings_mod.question_rows(facts, builds)
        return _ok(result)

    def route_recache(store, query, body):
        return _ok(store.recache())

    def route_daily_usage(store, query, body):
        # The original `days` param stays exactly as it was for existing
        # callers; the shared window params (window/window_days/since/
        # until) are additive and, when any is given, take precedence --
        # the same "new params win when present" rule route_summary uses.
        # Either way the days are calendar days in this service's zone
        # (config.toml's tz, else the machine's), and so are the `day`
        # keys of the rows.
        tz = _config_tz(options.config_dir)
        if any(key in query for key in ("window", "window_days", "since", "until")):
            window, err = _window_query(query)
            if err is not None:
                return err
            window_days, since, until, window_by = window
        else:
            window_days, err = _int_query(query, "days", 30, minimum=1)
            if err is not None:
                return err
            try:
                since = discovery.window_start_iso(window_days, tz)
            except (OverflowError, OSError, ValueError):
                return _bad_request("'days' is too large")
            until = None
            window_by = "last-reply"
        split = _str_query(query, "split")
        if split not in (None, "agent", "model"):
            return _bad_request("'split' must be 'agent' or 'model'")
        project, err = _project_query(query)
        if err is not None:
            return err
        return _ok(
            store.daily_usage(
                days=window_days,
                since=since,
                until=until,
                split=split,
                project_slugs=project,
                window_by=window_by,
                tz=tz,
            )
        )

    def route_compactions(store, query, body):
        """Every compaction of the sessions the window counts, as the
        tiles count them (``Store.compactions``), so the list is as long
        as the Spend page's own count."""
        window, err = _listing_window(query)
        if err is not None:
            return err
        window_days, since, until, window_by = window
        project, err = _project_query(query)
        if err is not None:
            return err
        return _ok(
            store.compactions(
                window_days=window_days, since=since, until=until, project_slugs=project, window_by=window_by
            )
        )

    def _latest_baseline_row(store) -> dict | None:
        rows = store.baselines()
        if not rows:
            return None
        # Store.baselines() is already ordered by created_at ascending
        # (its own ``ORDER BY b.created_at``) -- the last row is the
        # most recent capture across every project, matching "the latest
        # stored baseline digest" (singular) this route now returns.
        return rows[-1]

    def route_profiles(store, query, body):
        # v0.3: catalogue profiles are shipped package data, never rows
        # in the store (service/watcher.py's _scan_profiles never
        # ingests a catalogue id) -- merged in here at query time instead,
        # each tagged with which of the two it came from.
        catalogue_entries = [
            {
                "id": p.id,
                "name": p.name or p.id,
                "source": "catalogue",
                "archetype": p.archetype,
                "for": list(p.for_),
                "tasks": list(profile_catalogue.tasks_for(p)),
                "updated_at": None,
            }
            for p in profile_catalogue.list_profiles()
        ]
        user_entries = [
            {
                "id": row["id"],
                "name": row["name"],
                "source": "user",
                "archetype": None,
                "for": [],
                "tasks": [],
                "updated_at": row["updated_at"],
            }
            for row in store.profiles()
        ]

        suggested_profile_id = None
        latest_baseline = _latest_baseline_row(store)
        if latest_baseline is not None:
            try:
                record = json.loads(latest_baseline["digest_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                record = {}
            if isinstance(record, dict):
                suggested_profile_id = record.get("suggested_profile")

        # The profile `apply <profile>` last marked active: ignored
        # recommendations are kept per profile.
        active_id = ignores.active_profile(options.config_dir)
        active_name = None
        if active_id != ignores.NO_PROFILE:
            names = {entry["id"]: entry["name"] for entry in catalogue_entries + user_entries}
            active_name = names.get(active_id, active_id)
        return _ok(
            {
                "profiles": catalogue_entries + user_entries,
                "suggested_profile_id": suggested_profile_id,
                "active_profile_id": None if active_id == ignores.NO_PROFILE else active_id,
                "active_profile_name": active_name,
            }
        )

    def route_baseline(store, query, body):
        # v0.3: pair the latest capture with the onboarding capture
        # window's own status (baseline.capture_status) so the UI can
        # mark a recommendation/diff built from it as provisional --
        # config.toml is read the same way _build_report_model already
        # does for every report-backed route (never guarded there
        # either: an unreadable config.toml is a genuine 500, not
        # something this route should mask).
        config = load_config(options.config_dir)
        status = baseline_mod.capture_status(config)

        latest_row = _latest_baseline_row(store)
        latest: dict | None = None
        if latest_row is not None:
            try:
                record = json.loads(latest_row["digest_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                record = None
            latest = {
                "id": latest_row["id"],
                "project_slug": latest_row["project_slug"],
                "window_start": latest_row["window_start"],
                "window_end": latest_row["window_end"],
                "archetype": latest_row["archetype"],
                "created_at": latest_row["created_at"],
                "record": record if isinstance(record, dict) else None,
            }

        history = [
            {
                "id": row["id"],
                "project_slug": row["project_slug"],
                "window_start": row["window_start"],
                "window_end": row["window_end"],
                "archetype": row["archetype"],
                "created_at": row["created_at"],
            }
            for row in reversed(store.baselines())
        ]

        return _ok(
            {
                "baseline": latest,
                "history": history,
                "capture_status": {
                    "started": status.started,
                    "window_days": status.window_days,
                    "elapsed_days": status.elapsed_days,
                    "remaining_days": status.remaining_days,
                    "complete": status.complete,
                    "summary": baseline_mod.format_capture_status(status),
                },
            }
        )

    def route_set_tag(store, query, body):
        session_id = query.get("id", "")
        if store.session(session_id) is None:
            return _not_found("session not found")
        if not isinstance(body, dict):
            return _bad_request("request body must be a JSON object")
        key = body.get("key")
        value = body.get("value")
        if key not in ("mode", "purpose"):
            return _bad_request("'key' must be 'mode' or 'purpose'")
        if not isinstance(value, str):
            return _bad_request("'value' must be a string")
        store.set_tag(session_id, key, value)
        return _ok({"session_id": session_id, "tags": store.tags(session_id)})

    def route_set_feedback(store, query, body):
        """Your rating of a session (the /cg-feedback questions as
        checkboxes): words from ``capture_catalogue.RATING_VOCAB`` only,
        and ``builds``, the plan and handoff answers for each plan build of
        a session with two or more. Nothing ticked clears it."""
        session_id = query.get("id", "")
        if store.session(session_id) is None:
            return _not_found("session not found")
        if not isinstance(body, dict):
            return _bad_request("request body must be a JSON object")
        known = (*capture_catalogue.RATING_VOCAB, "builds")
        unknown = sorted(set(body) - set(known))
        if unknown:
            return _bad_request(f"unknown field {', '.join(unknown)}; known: {', '.join(known)}")
        values: dict = {}
        for key, words in capture_catalogue.RATING_VOCAB.items():
            value = body.get(key)
            if key in capture_catalogue.FEEDBACK_LIST_KEYS:
                value = [] if value is None else value
                if not isinstance(value, list) or any(w not in words for w in value):
                    return _bad_request(f"'{key}' must be a list of: {', '.join(words)}")
                values[key] = list(dict.fromkeys(value))
            else:
                if value is not None and value not in words:
                    return _bad_request(f"'{key}' must be one of: {', '.join(words)}, or null")
                values[key] = value
        builds, reason = _plan_builds(body.get("builds"))
        if reason is not None:
            return _bad_request(reason)
        store.set_feedback(session_id, builds=builds, **values)
        return _ok({"session_id": session_id, "feedback": store.feedback(session_id)})

    def _plan_builds(value) -> tuple[list[dict], str | None]:
        """The ``builds`` of a rating, checked: ``[{"build": 1, "plan":
        word, "handoff": word}, ...]`` with each build once, from 1, and
        the words those of the plan and handoff questions."""
        if value is None:
            return [], None
        if not isinstance(value, list) or len(value) > _MAX_PLAN_BUILDS:
            return [], f"'builds' must be a list of at most {_MAX_PLAN_BUILDS} plan builds"
        out: list[dict] = []
        for item in value:
            if not isinstance(item, dict) or set(item) - {"build", "plan", "handoff"}:
                return [], "each of 'builds' must be an object with build, plan and handoff"
            number = item.get("build")
            if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= _MAX_PLAN_BUILDS:
                return [], f"'build' must be a whole number from 1 to {_MAX_PLAN_BUILDS}"
            if any(number == held["build"] for held in out):
                return [], f"plan build {number} is given twice"
            clean = {"build": number}
            for key in capture_catalogue.PER_BUILD_KEYS:
                word = item.get(key)
                if word is not None and word not in capture_catalogue.RATING_VOCAB[key]:
                    return [], f"'{key}' must be one of: {', '.join(capture_catalogue.RATING_VOCAB[key])}, or null"
                clean[key] = word
            out.append(clean)
        return out, None

    # -- what you say about a tip, habit or recommendation card ----------------

    def _tip_card_known(kind: str, item: str) -> bool:
        """Whether ``item`` is a card of that kind: a tip hint or a
        prompting habit, a playbook item, or a recommendation key (which
        only needs its shape: the report it came from may be another
        window's)."""
        from .. import habits as habits_mod
        from .. import prompting

        if kind == "tip":
            return item in capture_catalogue.TIP_HINT_TITLES or item in prompting.HABITS
        if kind == "habit":
            return item in habits_mod.ITEMS
        return bool(_REC_KEY_RE.match(item))

    def route_tip_feedback(store, query, body):
        """What you said about each card so far, and the answers a card can
        take."""
        answers = [
            {"kind": kind, "item": item, **row} for (kind, item), row in sorted(store.tip_feedback().items())
        ]
        options = [
            {"word": word, "label": label, "description": text}
            for word, label, text in capture_catalogue.TIP_CARD_OPTIONS
        ]
        return _ok({"answers": answers, "options": options})

    def route_set_tip_feedback(store, query, body):
        """Rate a tip, habit or recommendation card: ``{"kind", "item",
        "answer"}`` with the answer one of ``capture_catalogue.
        TIP_CARD_VOCAB``, or ``null`` to take it back. "Trying it" also
        logs the day you started (``habit-log.jsonl``), which is a change
        point for that habit. It is something you say: nothing in Claude
        Code's own settings changes."""
        if not isinstance(body, dict):
            return _bad_request("request body must be a JSON object")
        unknown = sorted(set(body) - {"kind", "item", "answer"})
        if unknown:
            return _bad_request(f"unknown field {', '.join(unknown)}; known: kind, item, answer")
        kind, item, answer = body.get("kind"), body.get("item"), body.get("answer")
        if kind not in capture_catalogue.TIP_CARD_KINDS:
            return _bad_request(f"'kind' must be one of: {', '.join(capture_catalogue.TIP_CARD_KINDS)}")
        if not isinstance(item, str) or not _REC_KEY_RE.match(item) or not _tip_card_known(kind, item):
            return _bad_request(f"'item' must be the id of a {kind} card")
        if answer is not None and answer not in capture_catalogue.TIP_CARD_VOCAB:
            return _bad_request(f"'answer' must be one of: {', '.join(capture_catalogue.TIP_CARD_VOCAB)}, or null")
        held = store.tip_feedback()
        before = held.get((kind, item))
        if answer is not None and before is None and len(held) >= _MAX_TIP_FEEDBACK:
            return _bad_request(f"at most {_MAX_TIP_FEEDBACK} cards can hold an answer")
        saved = store.set_tip_feedback(kind, item, answer)
        if answer == "trying" and (before is None or before["answer"] != "trying"):
            append_habit_log(options.config_dir, kind=kind, item=item, state="trying")
        return _ok({"kind": kind, "item": item, "answer": answer, "set_at": saved["set_at"] if saved else None})

    # -- v0.3 profile routes -----------------------------------------------

    def _load_profile_by_id(profile_id: str):
        """``Profile`` for ``profile_id`` -- a catalogue id first (shipped
        package data, cheap to check), then a user profile written under
        ``<config_dir>/profiles/<id>.toml`` (the one path
        ``route_profiles_post``/``_scan_profiles`` ever write a user
        profile to -- see that route's own docstring). ``None`` if
        neither exists, or the on-disk file no longer parses (never lets
        a malformed file 500 the route -- this project's usual "skip,
        don't crash" posture for a foreign/edited-by-hand file).

        SEC-P4/F4: ``profile_id`` reaches here straight from the URL
        path, percent-decoded (see the route dispatcher's ``unquote``),
        so ``..%2F..%2Fetc%2Fpasswd`` or ``C:%5CWindows%5C...`` would
        otherwise interpolate real ``/``/``\\`` separators into the path
        built below and read a file outside ``<config_dir>/profiles/``.
        Checked against the same closed id shape a profile must already
        satisfy to be saved (``profile_schema._ID_RE``,
        ``^[a-z0-9-]{1,40}$``) before it ever touches the filesystem;
        every catalogue id already matches it too.
        """
        if not profile_schema._ID_RE.match(profile_id):
            return None
        if profile_id in profile_catalogue.CATALOGUE_IDS:
            return profile_catalogue.get(profile_id)
        path = Path(options.config_dir) / "profiles" / f"{profile_id}.toml"
        if not path.is_file():
            return None
        try:
            return profile_schema.load_profile(path)
        except (OSError, profile_schema.ProfileError, ValueError):
            return None

    def route_profile_diff(store, query, body):
        profile_id = query.get("id", "")
        profile = _load_profile_by_id(profile_id)
        if profile is None:
            return _not_found(f"unknown profile: {profile_id!r}")

        scope = query.get("scope") or "user"
        if scope not in _VALID_PROFILE_SCOPES:
            return _bad_request(f"'scope' must be one of {_VALID_PROFILE_SCOPES}")

        notes: list[str] = []
        snapshot = _config_snapshot_with_every_project_agents()
        if snapshot is not None:
            effective = snapshots_mod.effective_config(snapshot)
            provenance = snapshots_mod.effective_provenance(snapshot)
            managed_keys = set(snapshots_mod.managed_keys(snapshot))
            # Deviation (mirrors profiles/apply.py's own, documented
            # deviation note): snapshots.py has no effective_agents()
            # accessor, so the schema-2 field is read straight off the
            # snapshot's own data dict.
            raw_effective_agents = snapshot.data.get("effective_agents")
            effective_agents = dict(raw_effective_agents) if isinstance(raw_effective_agents, dict) else {}
        else:
            effective, provenance, managed_keys, effective_agents = {}, {}, set(), {}
            notes.append("no config snapshot recorded yet; diff computed against an empty effective config")

        profile_diff = profile_diff_mod.diff_against_effective(
            profile, effective, effective_agents, provenance, managed_keys
        )
        diff_text = profile_diff_mod.render_unified_diff(profile_diff, scope=scope)
        # project_path is deliberately never accepted from the client here
        # (unlike diff.py's own apply_command signature) -- this route's
        # response is API/UI output, and this project's privacy rule
        # forbids a raw filesystem path in any of it; a project-scoped
        # apply command names the folder it is run from (--project-dir .),
        # as apply_command's own docstring describes for "project_path
        # omitted", and a note says where to run it.
        apply_cmd, launch_cmd = profile_diff_mod.apply_command(profile.id, scope).split("\n", 1)
        if scope != "user":
            notes.append("Run the command in the project's own folder: --project-dir . means the folder you run it from.")

        from ..fixes import LEVER_LABELS, SETTING_TEXT, profile_change_where, profile_prompt

        def _row(row) -> dict:
            parts = row.key.split(".")
            # agents.<agent>.<key>; the key itself may be dotted (experimental.cacheTtl).
            name = ".".join(parts[2:]) if row.key.startswith("agents.") else ".".join(parts[1:])
            return {
                "key": row.key,
                "setting": name,
                "agent": parts[1] if row.key.startswith("agents.") else None,
                "label": LEVER_LABELS.get(name, name),
                "description": SETTING_TEXT.get(name, ("", "", ""))[0],
                "where": profile_change_where(row.key, scope),
                "current_value": row.current_value,
                "current_provenance": row.current_provenance,
                "proposed_value": row.proposed_value,
                "target_file": row.target_file,
                "managed": row.managed,
            }

        settings_rows = [_row(r) for r in profile_diff.rows if r.key.startswith("settings.")]
        agent_rows = [_row(r) for r in profile_diff.rows if r.key.startswith("agents.")]
        env_rows = [_row(r) for r in profile_diff.rows if r.key.startswith("env.")]

        return _ok(
            {
                "profile_id": profile.id,
                "scope": scope,
                "diff": diff_text,
                "settings": settings_rows,
                "agents": agent_rows,
                "env": env_rows,
                "apply_command": apply_cmd,
                "dry_run_command": f"{apply_cmd} --dry-run",
                "launch_command": launch_cmd,
                "prompt": profile_prompt(profile.name or profile.id, settings_rows + agent_rows + env_rows, scope),
                "notes": notes,
            }
        )

    def route_profile_schema(store, query, body):
        """Every key a profile may set, with its type, allowed values and
        plain-English text, for the dashboard's profile form."""
        from ..fixes import LEVER_LABELS, SETTING_TEXT

        def _lever(key, spec) -> dict:
            what, tradeoff, caveat = SETTING_TEXT.get(key, ("", "", ""))
            return {
                "key": key,
                "label": LEVER_LABELS.get(key, key),
                "kind": spec.kind,
                "values": list(spec.values) if spec.values else None,
                "min": spec.min,
                "max": spec.max,
                "description": what,
                "tradeoff": " ".join(t for t in (tradeoff, caveat) if t),
            }

        return _ok(
            {
                "settings": [_lever(k, v) for k, v in profile_schema.SETTINGS_ALLOWLIST.items()],
                "agents": [_lever(k, v) for k, v in profile_schema.AGENT_ALLOWLIST.items()],
                "env": sorted(profile_schema.ENV_ALLOWLIST),
                "archetypes": list(profile_schema.ARCHETYPES),
                "scopes": [
                    {"key": "user", "label": "Your user settings, every project"},
                    {"key": "project-local", "label": "This project, on your machine only"},
                    {"key": "repo", "label": "This project, shared with everyone who works in it"},
                ],
            }
        )

    def route_profile(store, query, body):
        profile_id = query.get("id", "")
        profile = _load_profile_by_id(profile_id)
        if profile is None:
            return _not_found(f"unknown profile: {profile_id!r}")
        setting_count = len(profile.settings) + sum(len(v) for v in profile.agents.values()) + len(profile.env)
        return _ok(
            {
                "id": profile.id,
                "name": profile.name or profile.id,
                "source": "catalogue" if profile_id in profile_catalogue.CATALOGUE_IDS else "user",
                "archetype": profile.archetype,
                "for": list(profile.for_),
                "tasks": list(profile_catalogue.tasks_for(profile)),
                "notes": profile.notes,
                "settings": dict(profile.settings),
                "agents": {name: dict(keys) for name, keys in profile.agents.items()},
                "env": dict(profile.env),
                "setting_count": setting_count,
            }
        )

    def route_session_explain(store, query, body):
        from .explain import explain_session

        session_id = query.get("id", "")
        detail = store.session(session_id)
        if detail is None:
            return _not_found("session not found")
        rates = load_pricing(path=load_config(options.config_dir).pricing_path, config_dir=options.config_dir)
        # UX-1: the same units.Units a full report would carry (with a
        # real elasticity fit under a subscription, from this machine's
        # own statusline usage-limit readings) rather than a bare
        # Units(billing_mode, currency) that always fell back to
        # "list-price equivalent" -- same idiom _compute_impact uses.
        units = _report_units(_get_report_model(*_default_window()))
        explained = explain_session(
            detail, store.session_parts(session_id), rates, units, store.median_session_cost()
        )
        return _ok({"session_id": session_id, **explained})

    def route_profiles_from_current(store, query, body):
        """Save the latest snapshot's effective config as a user
        profile: allowlisted keys only, managed keys left out and listed
        so the UI can say so. Writes only this tool's own profile store,
        never Claude Code's config."""
        body = body if isinstance(body, dict) else {}
        snapshot = _latest_config_snapshot()
        if snapshot is None or not isinstance(snapshot.data.get("effective"), dict):
            return _error(409, "conflict", "no config snapshot recorded yet; run claudeglass snapshot-config")
        effective = snapshots_mod.effective_config(snapshot)
        managed = set(snapshots_mod.managed_keys(snapshot))
        raw_agents = snapshot.data.get("effective_agents")
        effective_agents = raw_agents if isinstance(raw_agents, dict) else {}

        def _valid(doc: dict) -> bool:
            # One key at a time, so one out-of-range value drops only itself.
            return profile_schema.validate({"id": "x", **doc}) == []

        settings = {
            key: effective[key]
            for key in profile_schema.SETTINGS_ALLOWLIST
            if key in effective and key not in managed and effective[key] is not None
            and _valid({"settings": {key: effective[key]}})
        }
        agents: dict = {}
        for name, fields in effective_agents.items():
            if not isinstance(fields, dict):
                continue
            kept = {
                key: fields[key]
                for key in profile_schema.AGENT_ALLOWLIST
                if fields.get(key) is not None and _valid({"agents": {str(name): {key: fields[key]}}})
            }
            if kept:
                agents[str(name)] = kept
        doc = {
            "id": body.get("id") or "my-current-settings",
            "name": body.get("name") or "My current settings",
            "settings": settings,
            "agents": agents,
            "notes": f"Saved from the config snapshot taken {snapshot.ts}.",
        }
        status, payload = _save_user_profile(store, doc, replace=query.get("replace") == "1")
        if status == 201:
            payload["data"]["skipped_managed"] = sorted(k for k in profile_schema.SETTINGS_ALLOWLIST if k in managed)
        return status, payload

    def route_profiles_post(store, query, body):
        if not isinstance(body, dict):
            return _bad_request("request body must be a JSON object")
        return _save_user_profile(store, body, replace=query.get("replace") == "1")

    def _save_user_profile(store, body: dict, *, replace: bool):
        try:
            profile = profile_schema.load_dict(body)
        except profile_schema.ProfileError as exc:
            return _bad_request("; ".join(exc.problems))

        if profile.id in profile_catalogue.CATALOGUE_IDS:
            return _error(409, "conflict", f"{profile.id!r} is a reserved catalogue profile id")

        profiles_dir = Path(options.config_dir) / "profiles"
        target_path = profiles_dir / f"{profile.id}.toml"
        if target_path.is_file() and not replace:
            return _error(
                409, "conflict", f"profile {profile.id!r} already exists (pass ?replace=1 to overwrite)"
            )

        profiles_dir.mkdir(parents=True, exist_ok=True)
        text = profile_schema.dump_profile(profile)
        # Atomic write: temp file in the same directory + os.replace,
        # this module's own convention for "never leave a half-written
        # file behind" (mirrors cache.py's DigestCache.put).
        fd, tmp_name = tempfile.mkstemp(dir=str(profiles_dir), prefix=f".{profile.id}-", suffix=".toml.tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp_name, target_path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        store.upsert_profile(
            profile_id=profile.id,
            name=profile.name or profile.id,
            toml_path=str(target_path),
            content_hash=content_hash,
        )

        stored = next((row for row in store.profiles() if row["id"] == profile.id), None)
        data = {
            "id": profile.id,
            "name": profile.name or profile.id,
            "source": "user",
            "updated_at": stored["updated_at"] if stored is not None else None,
        }
        return 201, {"ok": True, "data": data}

    # -- report-backed routes ---------------------------------------------

    def route_ttl(store, query, body):
        window, err = _window_query(query)
        if err is not None:
            return err
        project, err = _project_query(query)
        if err is not None:
            return err
        model = _get_report_model(*window, project)
        section = _find_section(model, "ttl")
        return _ok(to_jsonable(section) if section is not None else None)

    def route_carry(store, query, body):
        window, err = _window_query(query)
        if err is not None:
            return err
        project, err = _project_query(query)
        if err is not None:
            return err
        model = _get_report_model(*window, project)
        section = _find_section(model, "carry")
        return _ok(to_jsonable(section) if section is not None else None)

    def route_plan_handoff(store, query, body):
        window, err = _window_query(query)
        if err is not None:
            return err
        project, err = _project_query(query)
        if err is not None:
            return err
        model = _get_report_model(*window, project)
        section = _find_section(model, "plan_handoff")
        return _ok(to_jsonable(section) if section is not None else None)

    def route_compaction_sim(store, query, body):
        window, err = _window_query(query)
        if err is not None:
            return err
        project, err = _project_query(query)
        if err is not None:
            return err
        model = _get_report_model(*window, project)
        section = _find_section(model, "compaction_sim")
        return _ok(to_jsonable(section) if section is not None else None)

    def route_model_swap(store, query, body):
        window, err = _window_query(query)
        if err is not None:
            return err
        project, err = _project_query(query)
        if err is not None:
            return err
        model = _get_report_model(*window, project)
        section = _find_section(model, "model_swap")
        return _ok(to_jsonable(section) if section is not None else None)

    def route_waste(store, query, body):
        window, err = _window_query(query)
        if err is not None:
            return err
        project, err = _project_query(query)
        if err is not None:
            return err
        model = _get_report_model(*window, project)
        section = _find_section(model, "waste")
        return _ok(to_jsonable(section) if section is not None else None)

    def route_config_diff(store, query, body):
        window, err = _window_query(query)
        if err is not None:
            return err
        project, err = _project_query(query)
        if err is not None:
            return err
        key = query.get("key")
        auto_keys = query.get("auto_keys") == "1"
        if not key and not auto_keys:
            return _bad_request("provide 'key' or 'auto_keys=1'")
        model = _get_report_model(*window, project)
        section = _find_section(model, "config")
        tables = section.tables if section is not None else []
        if auto_keys:
            data = [to_jsonable(table) for table in tables]
            # The page shows only the tables, so the section's own notes (no
            # setting changed between two snapshots of the same project, or
            # only the first 20 changed settings shown) go under the first.
            if data and section is not None and section.notes:
                data[0]["notes"] = [*section.notes, *(data[0].get("notes") or [])]
            return _ok(data)
        table = next((t for t in tables if t.name == f"config-diff-{key}"), None)
        return _ok(to_jsonable(table) if table is not None else [])

    def route_diagnostics(store, query, body):
        window, err = _window_query(query)
        if err is not None:
            return err
        project, err = _project_query(query)
        if err is not None:
            return err
        model = _get_report_model(*window, project)
        hook = hook_health.check(options.config_dir)
        statusline = hook_health.statusline_check(options.config_dir, store.entrypoint_counts())
        return _ok(
            to_jsonable(
                helptext.diagnostics_table(
                    model.diagnostics, hook=hook, statusline=statusline, parser_notes=model.parser_notes
                )
            )
        )

    def _report_units(model):
        from ..units import Units

        if model.units is not None:
            return model.units
        config = load_config(options.config_dir)
        return Units(billing_mode=config.billing, currency=model.meta.pricing.currency)

    def _claude_md_review(window, query, project=None):
        from .. import claude_md_review

        model = _get_report_model(*window, project)
        # With a project picked, only its folders' files (and your user
        # files, which every project reads), as Skills reads them.
        folders = _project_folders(project)
        review = claude_md_review.build_review(
            options.config_dir, model.context_files or {}, projects=None if folders is None else list(folders)
        )
        return claude_md_review, review, _report_units(model), _period_text(*window, name=query.get("window"))

    def route_claude_md(store, query, body):
        """Every CLAUDE.md-family file on disk (your own, and every
        project's, or only the picked project's), with how often it was
        sent in the window and what it cost. File text is read now and
        never stored."""
        window, err = _window_query(query)
        if err is not None:
            return err
        project, err = _project_query(query)
        if err is not None:
            return err
        module, review, units, period = _claude_md_review(window, query, project)
        return _ok(
            {
                "period": period,
                "transcripts": review.transcripts,
                "files": [module.file_summary(item, units, period) for item in review.files],
            }
        )

    def route_claude_md_file(store, query, body):
        window, err = _window_query(query)
        if err is not None:
            return err
        project, err = _project_query(query)
        if err is not None:
            return err
        module, review, units, period = _claude_md_review(window, query, project)
        file_id = query.get("id", "")
        item = next((entry for entry in review.files if entry.id == file_id), None)
        if item is None:
            return _not_found("unknown CLAUDE.md file")
        return _ok({"period": period, **module.file_detail(item, units, period)})

    def route_project_files(store, query, body):
        """The project files your agents consume: the CLAUDE.md-family
        files Claude Code loads, the files they import, and the files agents
        read by habit, each with its size now, its change over about 30
        days, who reads it and what it costs a month. Names are worked
        out now from the project folders on disk and are never stored.
        Text files come first; only they can be flagged (``reasons``), as
        the Overview check covers the documents agents take in."""
        from .. import claude_md_review, context_files

        window, err = _window_query(query)
        if err is not None:
            return err
        project, err = _project_query(query)
        if err is not None:
            return err
        model = _get_report_model(*window, project)
        data = model.context_files or {}
        folders = _project_folders(project)
        rows, local = claude_md_review.project_file_rows(
            options.config_dir, data, projects=None if folders is None else list(folders)
        )
        flagged = {row["hash"]: row["reasons"] for row in context_files.check_rows(rows)}
        units = _report_units(model)
        shown = context_files.dearest(rows, PROJECT_FILES_SHOWN)
        return _ok(
            {
                "period": _period_text(*window, name=query.get("window")),
                "window_days": data.get("window_days") or context_files.MIN_WINDOW_DAYS,
                "transcripts": data.get("transcripts") or {},
                "total": len(rows),
                "named": sum(1 for row in rows if row["name"]),
                "truncated": local.truncated,
                "files": [
                    {
                        **row,
                        "reasons": flagged.get(row["hash"], []),
                        "fixes": claude_md_review.project_file_fixes(row, units)
                        if row["hash"] in flagged
                        else [],
                    }
                    for row in shown
                ],
            }
        )

    def route_skills(store, query, body):
        """Every skill Claude Code listed in the window: what it is (its
        description, read now from the newest listing and never stored),
        where it comes from, how often it was listed and used, and how
        to hide the ones Claude never uses."""
        from .. import skills_review

        window, err = _window_query(query)
        if err is not None:
            return err
        project, err = _project_query(query)
        if err is not None:
            return err
        model = _get_report_model(*window, project)
        return _ok(
            skills_review.review(
                options.config_dir,
                model.context_files or {},
                _report_units(model),
                _period_text(*window, name=query.get("window")),
                only=_project_folders(project),
            )
        )

    def _project_folders(project):
        """The folders a ``project`` filter stands for (``skills_review``),
        or ``None`` with no filter."""
        from .. import skills_review

        return skills_review.project_folders_for(Path(options.config_dir).parent, project) if project else None

    def _current_settings(project=None) -> tuple[dict, dict, bool]:
        """The latest snapshot's effective settings as in force, every project's agent
        fields, and (PROF-03) whether ``CLAUDE_CODE_EFFORT_LEVEL`` is set
        -- content_layers' own flag, never a value that could be
        anything else -- or empty/``False`` when no snapshot is recorded
        yet. With a ``project`` filter, that project's own settings and
        agents (:func:`_config_snapshot_with_every_project_agents`)."""
        snapshot = _config_snapshot_with_every_project_agents(project)
        if snapshot is None:
            return {}, {}, False
        agents = snapshot.data.get("effective_agents")
        content_layers = snapshot.data.get("content_layers")
        env_set = bool(isinstance(content_layers, dict) and content_layers.get("effort_level_env_set"))
        # In force, not as written: CLAUDE_CODE_AUTO_COMPACT_WINDOW beats
        # autoCompactWindow while it's set.
        return snapshots_mod.effective_config_in_force(snapshot), agents if isinstance(agents, dict) else {}, env_set

    def route_profile_goals(store, query, body):
        """Without ``goal``: the goals a profile can start from. With it:
        that goal's candidate changes, each with the value in effect now,
        the evidence, the trade-off and a what-if estimate."""
        from ..profiles import goals

        goal = query.get("goal")
        if not goal:
            return _ok({"goals": goals.goals_list()})
        if goal not in goals.GOAL_IDS:
            return _bad_request(f"unknown goal {goal!r}; expected one of: {', '.join(goals.GOAL_IDS)}")
        window, err = _window_query(query)
        if err is not None:
            return err
        task = query.get("task") or None
        if task is not None and task not in capture_catalogue.TAG_VOCAB["task"]:
            return _bad_request(
                f"unknown task {task!r}; expected one of: {', '.join(capture_catalogue.TAG_VOCAB['task'])}"
            )
        project, err = _project_query(query)
        if err is not None:
            return err
        model = _get_report_model(*window, project)
        effective, effective_agents, effort_level_env_set = _current_settings(project)
        return _ok(
            goals.draft(
                goal,
                model,
                _report_units(model),
                effective=effective,
                effective_agents=effective_agents,
                period=_period_text(*window, name=query.get("window")),
                task=task,
                effort_level_env_set=effort_level_env_set,
                skip_keys=_ignored_keys(model, query),
            )
        )

    def route_whatif(store, query, body):
        """The estimated effect of ``{settings, agents}`` on the window,
        looked up in the report's own tables. Reads only; nothing is
        saved or applied -- unless the body also carries ``"log": true``
        (EST-P5), in which case every row whose ``saving_usd`` could be
        estimated is appended to ``prediction-log.jsonl``
        (``config.append_prediction_log``) so ``backtest.py`` can later
        check it against what actually happened. The log is opt-in
        because most ``/api/whatif`` calls are the dashboard exploring
        "what if" interactively as you drag a slider -- only a change
        you actually mean to track is worth a prediction row. EST-P6:
        once at least 3 of your own past predictions for a given kind of
        change have been judged, its estimate here is calibrated by how
        that change actually turned out for you before
        (``backtest.calibration_multipliers``) -- logging always records
        the *uncalibrated* estimate (``row["uncalibrated_usd"]`` when
        present), so calibrating an already-calibrated number never
        compounds. PROF-01: a ``?task=`` query param scales every row
        down to that kind of task's own share of the window, the same
        way the tasks goal's own draft does
        (``profiles.goals._scale_whatif``) -- for the tasks goal's live
        total as you tick candidates, and for a saved profile whose
        ``for`` names a task."""
        from .. import backtest as backtest_mod
        from .. import config as config_mod
        from .. import whatif
        from ..profiles import goals

        if not isinstance(body, dict):
            return _bad_request("request body must be a JSON object")
        settings = body.get("settings") or {}
        agents = body.get("agents") or {}
        if not isinstance(settings, dict) or not isinstance(agents, dict):
            return _bad_request("settings and agents must be JSON objects")
        problems = profile_schema.validate({"id": "whatif", "settings": settings, "agents": agents})
        if problems:
            return _bad_request("; ".join(problems))
        # One task, or several comma-separated (a catalogue profile's
        # ``for`` covers more than one; their shares add).
        tasks = tuple(dict.fromkeys(t for t in (query.get("task") or "").split(",") if t))
        unknown = [t for t in tasks if t not in capture_catalogue.TAG_VOCAB["task"]]
        if unknown:
            return _bad_request(
                f"unknown task {unknown[0]!r}; expected one of: {', '.join(capture_catalogue.TAG_VOCAB['task'])}"
            )
        task = tasks or None
        window, err = _window_query(query)
        if err is not None:
            return err
        project, err = _project_query(query)
        if err is not None:
            return err
        model = _get_report_model(*window, project)
        effective, _agents, _env_set = _current_settings(project)
        units = _report_units(model)
        period = _period_text(*window, name=query.get("window"))
        result = whatif.estimate(
            settings,
            agents,
            model,
            units,
            period=period,
            current=effective,
            calibration=backtest_mod.calibration_multipliers(store),
        )
        if task is not None:
            result = goals._scale_whatif(result, whatif._Tables(model), task, units, period)
        if body.get("log") is True:
            for row in result["rows"]:
                predicted_usd = row["uncalibrated_usd"] if row["uncalibrated_usd"] is not None else row["saving_usd"]
                fidelity = row["uncalibrated_fidelity"] or row["fidelity"]
                if predicted_usd is None:
                    continue
                config_mod.append_prediction_log(
                    options.config_dir,
                    source="whatif",
                    measure_key=row["key"],
                    agent=row["agent"],
                    predicted_usd=predicted_usd,
                    predicted_pct=None,
                    fidelity=fidelity,
                )
        return _ok(result)

    def route_predictions_seen(store, query, body):
        """EST-P5: record that the dashboard has actually shown you a
        prediction (``Store.mark_prediction_seen``), by its
        ``prediction-log.jsonl``/``predictions`` row id."""
        if not isinstance(body, dict):
            return _bad_request("request body must be a JSON object")
        prediction_id = body.get("id")
        if not isinstance(prediction_id, str) or not prediction_id:
            return _bad_request("'id' must be a non-empty string")
        seen = store.mark_prediction_seen(prediction_id)
        return _ok({"id": prediction_id, "seen": seen})

    def _quick_context(window, query, project=None):
        from ..discovery import _resolve_window
        from .. import quick_actions

        model = _get_report_model(*window, project)
        effective, effective_agents, _env_set = _current_settings(project)
        window_days, since, until, _window_by = window
        since_dt, until_dt = _resolve_window(window_days, since, until)
        return quick_actions, quick_actions.Context(
            model=model,
            units=_report_units(model),
            period=_period_text(*window, name=query.get("window")),
            config_dir=Path(options.config_dir),
            effective=effective,
            effective_agents=effective_agents,
            skip_keys=_ignored_keys(model, query),
            only=_project_folders(project),
            since_ts=since_dt.timestamp() if since_dt else None,
            until_ts=until_dt.timestamp() if until_dt else None,
        )

    def route_quick_actions(store, query, body):
        """Every quick-action check's status and one-line answer for the
        window."""
        window, err = _window_query(query)
        if err is not None:
            return err
        project, err = _project_query(query)
        if err is not None:
            return err
        module, ctx = _quick_context(window, query, project)
        return _ok({"period": ctx.period, "checks": module.run_all(ctx)})

    def route_quick_action(store, query, body):
        """One check in full: its evidence table, fixes and tips."""
        window, err = _window_query(query)
        if err is not None:
            return err
        project, err = _project_query(query)
        if err is not None:
            return err
        module, ctx = _quick_context(window, query, project)
        if query.get("id") not in module.CHECK_IDS:
            return _not_found("unknown quick action")
        return _ok(module.run(query["id"], ctx))

    def route_setup(store, query, body):
        """What this tool has installed and changed on this machine, what
        each costs in tokens and how to undo it, plus what to expect."""
        from .. import footprint

        items = footprint.inventory(options.config_dir, service_registered=_cached_service_registered())
        return _ok(
            {
                "items": [item.as_dict() for item in items],
                "expectations": [
                    {"title": title, "text": text}
                    for title, text in footprint.expectations(footprint.capture_setting(options.config_dir))
                ],
                "uninstall_command": footprint.UNINSTALL_COMMAND,
            }
        )

    def route_setup_status(store, query, body):
        """Whether each part of the setup works (``setup_status.py``), for
        the Overview's Setup card and Data quality's checklist. This
        dashboard answering is proof it runs, so only whether it starts
        at logon is asked, through the same cached probe as
        ``/api/health``."""
        from .. import setup_status

        items = setup_status.check_setup(
            options.config_dir,
            entrypoints=store.entrypoint_counts(),
            is_registered=_cached_service_registered,
            running=True,
            url=None,
        )
        return _ok(
            {
                "items": [setup_status.to_jsonable(item) for item in items],
                "done": setup_status.done(items),
                "needs_attention": len(setup_status.needs_attention(items)),
                "verdict": setup_status.verdict(items),
            }
        )

    #: scope -> the change cards worked out for it, least recently used
    #: first. A scope is the project picked (its raw slugs, ``()`` for every
    #: one) and how far back a window reaches (``_impact_reach``'s key), so
    #: the windows inside one reach share a slot. A slot: {"key" (the store's
    #: change token and the recorded changes), "points" (every change that
    #: applies in it, oldest first), "rows" (``_change_id`` -> that change
    #: compared, worked out only for the changes a window listed),
    #: "earliest" and "base" (``_impact_reach``), "started" (monotonic),
    #: "as_of" (ISO), "building"}.
    impact_cache: OrderedDict = OrderedDict()

    def _change_scope(query):
        """The window and project the change cards (``/api/impact``,
        ``/api/backtest``) are asked about: ``((since_dt, until_dt),
        project_slugs, None)``, or ``(None, None, error)``. With no window
        param at all it is all time, as for every listing (``_listing_window``);
        a ``window_days`` window is the local calendar days the dashboard's
        other figures count, already resolved to its ``since``."""
        window, err = _listing_window(query)
        if err is not None:
            return None, None, err
        project, err = _project_query(query)
        if err is not None:
            return None, None, err
        return discovery._resolve_window(*window[:3]), project, None

    def route_impact(store, query, body):
        """Each change you made (an apply, its undo, a settings change the
        config hook saw, or a metrics capture change), and each model,
        effort or CLAUDE.md size change your sessions show, with the
        sessions before it against those after it, on the measures that
        change should move.

        The window (``window``, ``window_days``, ``since``/``until``; none
        means all time) lists every change made inside it, newest first, and
        never clips one: a change is compared on its whole before and after
        whichever window lists it. All time (no start) reads back as far as
        the widest window the picker offers (``_ALL_TIME_REACH_DAYS``), so it
        lists every change another window does; a change older than the
        sessions this reads from (``_impact_reach``) is not listed.
        ``project`` lists the changes that apply there (those for every
        project and that project's own), compared on that project's
        sessions alone.

        Cached like the report (see _get_report_model), one slot per
        project and reach (``impact_cache``): a store change serves the kept
        answer and refreshes it in the background, while a new recorded
        change point (the list itself changing) or a changed config.toml
        (its tz sets each change's day) is worked out at once. Inside a slot
        each change is compared once, and a window works out only the
        changes it lists that the slot lacks, so switching windows doesn't
        compare anything again."""
        from .. import change_points

        span, project, err = _change_scope(query)
        if err is not None:
            return err
        since_dt, until_dt = span
        recorded = change_points.change_points(options.config_dir)
        # The changes themselves, and config.toml (its tz decides each
        # change's `day`): either moving is worked out at once, not served
        # stale while it refreshes.
        point_key = (tuple((p.iso(), p.source, p.backup_ts) for p in recorded), _config_mtime_ns())
        key = (store.change_token(), point_key)
        now_utc = datetime.now(timezone.utc)
        # All time (no start) reads as far back as the widest window, so it
        # lists every change another window can.
        reach_since = since_dt or discovery.window_start(
            _ALL_TIME_REACH_DAYS, _config_tz(options.config_dir), now=now_utc
        )
        earliest, base, reach_key = _impact_reach(recorded[0].ts if recorded else None, reach_since, now_utc)
        scope = (project or (), reach_key)
        now = time.monotonic()
        answer, refresh, carry, keep = None, False, frozenset(), None
        with report_lock:
            slot = impact_cache.get(scope)
            if slot is not None:
                impact_cache.move_to_end(scope)
                listed = _changes_listed(slot["points"], slot["base"], since_dt, until_dt)
                complete = all(_change_id(p) in slot["rows"] for p in listed)
                if complete and slot["key"] == key:
                    _note_as_of(slot["as_of"], False)
                    return _ok(_impact_answer(slot["rows"], listed))
                if complete and slot["key"][1] == point_key and now - slot["started"] <= _STALE_REPORT_MAX_AGE_S:
                    refresh = not slot["building"]
                    if refresh:
                        slot["building"] = True
                        carry = frozenset(slot["rows"])
                    _note_as_of(slot["as_of"], True)
                    answer = _impact_answer(slot["rows"], listed)
                elif slot["key"] == key:
                    # The answer kept is this one's, but lacks changes this
                    # window lists: add them to it, on the reach it was made on.
                    earliest, base, keep = slot["earliest"], slot["base"], dict(slot["rows"])
        if answer is not None:
            if refresh:

                def run():
                    try:
                        with background_builds:
                            _compute_impact(
                                scope, key, project, earliest, base, since_dt, until_dt, carry=carry, background=True
                            )
                    except BaseException as exc:  # noqa: BLE001 -- the next request retries
                        print(f"claudeglass serve: your changes were not refreshed: {exc}", file=sys.stderr, flush=True)
                    finally:
                        with report_lock:
                            current = impact_cache.get(scope)
                            if current is not None:
                                current["building"] = False
                        store.close()

                threading.Thread(target=run, name="claudeglass-impact", daemon=True).start()
            return _ok(answer)
        answer, as_of = _compute_impact(scope, key, project, earliest, base, since_dt, until_dt, keep=keep)
        _note_as_of(as_of, False)
        return _ok(answer)

    def _compute_impact(
        scope, key, project, earliest, base, since_dt, until_dt, *, keep=None, carry=frozenset(), background=False
    ):
        """Compare the changes the window lists in ``scope``'s slot, and
        return ``(answer, as_of)`` for the window. ``keep`` (the slot's rows
        for this same ``key``) is added to rather than started again;
        ``carry`` names changes to compare again besides the window's (a
        refresh keeps what was already shown). A ``background`` build that
        finds its slot gone (evicted while it ran) drops its result rather
        than bring the slot back."""
        from .. import change_points, counterfactual
        from .. import impact as impact_mod
        from ..discovery import redact_slug
        from ..snapshots import snapshot_project_keys
        from . import rebuild

        started = time.monotonic()
        as_of = _now_utc_iso()
        # The sessions the changes are worked out from reach back from
        # _impact_reach's earliest: the older of this service's default
        # window and the oldest recorded change (or the start of a wider
        # window's day, All time's included), less impact's lookback.
        # Transcript changes (EST-P9) can only be found once they are read.
        # A project reads its own sessions alone.
        read = {"project_slugs": list(project)} if project else {}
        corpus = rebuild.corpus_from_store(store, since=discovery.utc_stamp(earliest, seconds=True), **read)
        points = change_points.change_points(options.config_dir, corpus)
        if project:
            # The changes that apply there: every project's, and its own
            # (named by either spelling of its drive letter).
            keys = {k for slug in project for k in snapshot_project_keys(slug)}
            points = [p for p in points if not p.project or p.project in keys]
        listed = _changes_listed(points, base, since_dt, until_dt)
        rows = dict(keep) if keep is not None else {}
        # A refresh compares again what it kept, as far as the reach goes.
        in_reach = {_change_id(p) for p in points if p.ts >= base}
        todo = ({_change_id(p) for p in listed} | (carry & in_reach)) - set(rows)
        if todo:
            config = load_config(options.config_dir)
            zone = discovery._zone(_config_tz(options.config_dir))
            rates = load_pricing(path=config.pricing_path, config_dir=options.config_dir)
            sessions = impact_mod.session_facts(corpus, rates)
            units = _report_units(_get_report_model(*_default_window()))
            # Every change still bounds its neighbours: only the ones to be
            # compared are.
            compared = impact_mod.impact(
                points,
                sessions,
                units,
                limit=None,
                without=counterfactual.for_impact(corpus, rates, units),
                listed=lambda p: _change_id(p) in todo,
            )
            # A project change names its project as the dashboard's
            # project filter does (redacted), never by its snapshot key,
            # and by either spelling of its drive letter.
            names = {
                k: redact_slug(b.slug) for b in corpus.sessions if b.slug for k in snapshot_project_keys(b.slug)
            }
            for point, change in zip((p for p in reversed(points) if _change_id(p) in todo), compared):
                project_key = change["change"]["project"]
                change["change"]["project_name"] = names.get(project_key, "") if project_key else ""
                # The local day the change falls on, so a chart marks the
                # column the daily figures put it in.
                change["change"]["day"] = _local_day_or_none(change["change"].get("ts"), zone)
                # P4 leftover: a structured gate the dashboard's
                # emptyState() can key off, alongside the existing prose
                # verdict -- same "enough" predicate impact.compare
                # already computed (min_sessions on both sides).
                change["gate"] = _min_sessions_gate(
                    change["before_sessions"], change["after_sessions"], impact_mod.MIN_SESSIONS
                )
                rows[_change_id(point)] = change
        with report_lock:
            slot = impact_cache.get(scope)
            if keep is not None:
                if slot is not None and slot["key"] == key:
                    slot["rows"].update(rows)
            elif slot is not None and started >= slot["started"]:
                slot.update(
                    key=key, points=points, rows=rows, earliest=earliest, base=base, started=started, as_of=as_of
                )
                impact_cache.move_to_end(scope)
            elif slot is None and not background:
                impact_cache[scope] = {
                    "key": key, "points": points, "rows": rows, "earliest": earliest, "base": base,
                    "started": started, "as_of": as_of, "building": False,
                }
                while len(impact_cache) > _IMPACT_CACHE_SIZE:
                    impact_cache.popitem(last=False)
        return _impact_answer(rows, listed), as_of

    backtest_cache: dict = {"key": None, "data": None, "started": 0.0, "as_of": None, "building": False}

    def _backtest_key(store, recorded, predictions):
        point_key = tuple((p.iso(), p.source, p.backup_ts) for p in recorded)
        predictions_key = tuple(sorted((p["id"], p["judged_at"]) for p in predictions))
        return (store.change_token(), point_key, predictions_key)

    def route_backtest(store, query, body):
        """EST-P4: every logged prediction (``POST /api/whatif`` with
        ``"log": true``) matched to the change point it turned into and
        judged against the sessions before and after (``backtest.py``),
        plus whatever is still waiting on more data or a match. Cached
        like ``/api/impact`` -- a store or prediction-log change serves
        the kept answer and judges any newly-eligible predictions in the
        background, while a genuinely new set of change points is worked
        out at once.

        The window (as for ``/api/impact``; none means all time) lists the
        predictions that belong to it: a judged one by the change it was
        matched to (``change_ts``), one still waiting by when it was
        logged (``ts``). The sessions are read from ``/api/impact``'s default
        reach (``_impact_reach``, the one its 30-day window reads), stretched
        back to the oldest prediction still waiting (the too_little_data ones
        the first check reopens included): the change it turns into is at or
        after it, and is judged on its whole before side. The window never
        moves that reach, since a verdict is kept once it is reached.
        ``project`` is checked and then ignored: a prediction names no
        project, so the estimates, and where "since my last change" starts
        for them (the newest change in any project), stay every project's."""
        from .. import backtest as backtest_mod
        from .. import change_points

        _project, err = _project_query(query)
        if err is not None:
            return err
        # The estimates are every project's, so the window is too: "since my
        # last change" starts at the newest change in any project.
        span, _every, err = _change_scope({k: v for k, v in query.items() if k != "project"})
        if err is not None:
            return err
        since_dt, until_dt = span
        recorded = change_points.change_points(options.config_dir)
        # The first check on a store judges the old too_little_data verdicts
        # again: reopen them before the reach is worked out, so it reaches
        # back to them too.
        backtest_mod._reopen_too_little_data(store)
        predictions = store.predictions()
        key = _backtest_key(store, recorded, predictions)
        waiting = [change_points._parse_iso(p.get("ts")) for p in predictions if not p.get("judged_at")]
        earliest = _impact_reach(
            recorded[0].ts if recorded else None,
            min((ts for ts in waiting if ts is not None), default=None),
            datetime.now(timezone.utc),
        )[0]
        now = time.monotonic()
        refresh = False
        with report_lock:
            kept = backtest_cache["data"]
            kept_key = backtest_cache["key"]
            if kept is not None and kept_key == key:
                _note_as_of(backtest_cache["as_of"], False)
                return _ok(_backtest_listing(kept, since_dt, until_dt))
            if (
                kept is not None
                and kept_key[1] == key[1]
                and now - backtest_cache["started"] <= _STALE_REPORT_MAX_AGE_S
            ):
                refresh = not backtest_cache["building"]
                if refresh:
                    backtest_cache["building"] = True
                _note_as_of(backtest_cache["as_of"], True)
            else:
                kept = None
        if kept is not None:
            if refresh:

                def run():
                    try:
                        with background_builds:
                            _compute_backtest(key, earliest)
                    except BaseException as exc:  # noqa: BLE001 -- the next request retries
                        print(f"claudeglass serve: past estimates were not checked: {exc}", file=sys.stderr, flush=True)
                    finally:
                        with report_lock:
                            backtest_cache["building"] = False
                        store.close()

                threading.Thread(target=run, name="claudeglass-backtest", daemon=True).start()
            return _ok(_backtest_listing(kept, since_dt, until_dt))
        data = _compute_backtest(key, earliest)
        _note_as_of(backtest_cache["as_of"] or _now_utc_iso(), False)
        return _ok(_backtest_listing(data, since_dt, until_dt))

    def _compute_backtest(key, earliest):
        from .. import backtest as backtest_mod
        from . import rebuild

        started = time.monotonic()
        as_of = _now_utc_iso()
        config = load_config(options.config_dir)
        rates = load_pricing(path=config.pricing_path, config_dir=options.config_dir)
        # The sessions from the impact cards' default reach, back to the
        # oldest prediction still waiting (EST-P9's transcript-derived points
        # need a corpus before they can even be listed, the same
        # chicken-and-egg change_points.py's own docstring notes), so a change
        # one lists is the change the other judges on.
        corpus = rebuild.corpus_from_store(store, since=discovery.utc_stamp(earliest, seconds=True))
        units = _report_units(_get_report_model(*_default_window()))
        judged = backtest_mod.judge_predictions(store, corpus, rates, units, options.config_dir)
        predictions = store.predictions()
        data = {
            "predictions": backtest_mod.present(predictions, units),
            "judged_just_now": judged,
            "verdicts": list(backtest_mod.VERDICTS),
        }
        with report_lock:
            if started >= backtest_cache["started"]:
                backtest_cache.update(key=key, data=data, started=started, as_of=as_of)
        return data

    def _ignored_keys(model, query) -> frozenset[str]:
        """The keys of this report's recommendations ignored in the
        project asked about (as the dashboard names it), under the active
        profile."""
        return ignores.skip_keys(options.config_dir, model.recommendations, _str_query(query, "project"))

    def route_recommendations(store, query, body):
        window, err = _window_query(query)
        if err is not None:
            return err
        project, err = _project_query(query)
        if err is not None:
            return err
        model = _get_report_model(*window, project)
        rows = [to_jsonable(rec) for rec in model.recommendations]
        # Each row says whether it's ignored here (ignores.py); the cached
        # report itself is never changed, so /api/report.json and the CLI
        # reports stay complete.
        marks = ignores.annotate(
            model.recommendations,
            ignores.load(options.config_dir),
            ignores.active_profile(options.config_dir),
            _str_query(query, "project"),
        )
        for row, mark in zip(rows, marks):
            row.update(mark)
        return _ok(rows)

    def route_recommendations_ignore(store, query, body):
        """Ignore recommendations, or stop ignoring them: ``{"keys":
        [...], "ignored": true|false}``. Each key must be a recommendation
        in this window and project's report; the fingerprint that decides
        whether it has changed since is worked out here, never taken from
        the request."""
        if not isinstance(body, dict):
            return _bad_request("request body must be a JSON object")
        keys = body.get("keys")
        if not isinstance(keys, list) or not keys or len(keys) > _MAX_IGNORE_KEYS:
            return _bad_request(f"'keys' must be a list of 1 to {_MAX_IGNORE_KEYS} recommendation keys")
        if not all(isinstance(key, str) and _REC_KEY_RE.match(key) for key in keys):
            return _bad_request("each key must be a recommendation key: a-z, 0-9, '.', '_', ':' or '-'")
        ignored = body.get("ignored")
        if not isinstance(ignored, bool):
            return _bad_request("'ignored' must be true or false")
        window, err = _window_query(query)
        if err is not None:
            return err
        project, err = _project_query(query)
        if err is not None:
            return err
        model = _get_report_model(*window, project)
        by_key = {rec.key: rec for rec in model.recommendations}
        missing = [key for key in keys if key not in by_key]
        if missing:
            return _not_found("no recommendation with that key in this window")
        recs = [by_key[key] for key in dict.fromkeys(keys)]
        profile = ignores.set_ignored(options.config_dir, recs, ignored=ignored, project=_str_query(query, "project"))
        return _ok(
            {
                "keys": [rec.key for rec in recs],
                "ignored": ignored,
                "active_profile_id": None if profile == ignores.NO_PROFILE else profile,
            }
        )

    def _render_report(content_type: str, render: Callable[[object], str]):
        def _route(store, query, body):
            window, err = _window_query(query)
            if err is not None:
                return err
            project, err = _project_query(query)
            if err is not None:
                return err
            model = _get_report_model(*window, project)
            return ("raw", content_type, render(model))

        return _route

    host_names = allowed_host_names(options)

    # -- routing tables -----------------------------------------------------

    get_routes: dict[str, Callable] = {
        "/api/health": route_health,
        "/api/summary": route_summary,
        "/api/sessions": route_sessions,
        "/api/recache": route_recache,
        "/api/compactions": route_compactions,
        "/api/profiles": route_profiles,
        "/api/baseline": route_baseline,
        "/api/daily-usage": route_daily_usage,
        "/api/ttl": route_ttl,
        "/api/carry": route_carry,
        "/api/compaction-sim": route_compaction_sim,
        "/api/plan-handoff": route_plan_handoff,
        "/api/model-swap": route_model_swap,
        "/api/waste": route_waste,
        "/api/config-diff": route_config_diff,
        "/api/recommendations": route_recommendations,
        "/api/diagnostics": route_diagnostics,
        "/api/profile-schema": route_profile_schema,
        "/api/claude-md": route_claude_md,
        "/api/project-files": route_project_files,
        "/api/skills": route_skills,
        "/api/impact": route_impact,
        "/api/backtest": route_backtest,
        "/api/profile-goals": route_profile_goals,
        "/api/quick-actions": route_quick_actions,
        "/api/setup": route_setup,
        "/api/setup/status": route_setup_status,
        "/api/capture": route_capture,
        "/api/tip-feedback": route_tip_feedback,
        "/api/report.json": _render_report("application/json", lambda model: render_json(model)),
        # Finding 22: charset was missing on the two text-ish renderers
        # (application/json has no encoding ambiguity, but text/markdown
        # and text/html do -- a client/browser guessing the wrong one on
        # a non-ASCII report is exactly the failure mode this closes).
        "/api/report.md": _render_report("text/markdown; charset=utf-8", render_markdown),
        "/api/report.html": _render_report("text/html; charset=utf-8", render_html),
    }
    get_patterns: tuple[tuple[re.Pattern, Callable], ...] = (
        (_SESSION_ID_RE, route_session),
        (_SESSION_EXPLAIN_RE, route_session_explain),
        (_PROFILE_DIFF_RE, route_profile_diff),
        (_PROFILE_RE, route_profile),
        (_CLAUDE_MD_RE, route_claude_md_file),
        (_QUICK_ACTION_RE, route_quick_action),
    )
    post_routes: dict[str, Callable] = {
        "/api/capture": route_capture_post,
        "/api/profiles": route_profiles_post,
        "/api/profiles/from-current": route_profiles_from_current,
        "/api/whatif": route_whatif,
        "/api/predictions/seen": route_predictions_seen,
        "/api/recommendations/ignore": route_recommendations_ignore,
        "/api/tip-feedback": route_set_tip_feedback,
    }
    post_patterns: tuple[tuple[re.Pattern, Callable], ...] = (
        (_SESSION_TAGS_RE, route_set_tag),
        (_SESSION_FEEDBACK_RE, route_set_feedback),
    )

    class Handler(BaseHTTPRequestHandler):
        # Review S5: derived from __version__ (major.minor, matching the
        # stdlib http.server convention of a two-part version on this
        # header) rather than a literal that goes stale on every release.
        server_version = f"claudeglass/{'.'.join(_TOOL_VERSION.split('.')[:2])}"
        protocol_version = "HTTP/1.1"

        # -- quiet by default: never log a request path to stdout/stderr
        # (docs/api.md's privacy rule is about response bodies, but a
        # request path can carry a session/profile id too -- keep both
        # off any shared log by default).
        def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
            pass

        # -- low-level writers ------------------------------------------

        def _write_headers(
            self, status: int, content_type: str, length: int, *, cache_control: str | None = None
        ) -> None:
            self.send_response(status)
            for name, value in _SECURITY_HEADERS:
                if name == "Cache-Control" and cache_control is not None:
                    value = cache_control
                self.send_header(name, value)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            # When this request read a built report: when that report's
            # figures were read from the store, and whether a newer one
            # is being built (see _get_report_model).
            as_of = getattr(request_ctx, "as_of", None)
            if as_of is not None:
                self.send_header("X-Figures-As-Of", as_of[0])
                if as_of[1]:
                    self.send_header("X-Figures-Refreshing", "1")
            self.end_headers()

        def _write_json(self, status: int, payload: dict, *, head_only: bool = False) -> None:
            body = json.dumps(invocation.rewrite_payload(payload, invocation.command_prefix())).encode("utf-8")
            self._write_headers(status, "application/json", len(body))
            if not head_only:
                self.wfile.write(body)

        def _write_text(self, status: int, content_type: str, text: str, *, head_only: bool = False) -> None:
            body = text.encode("utf-8")
            self._write_headers(status, content_type, len(body))
            if not head_only:
                self.wfile.write(body)

        def _write_bytes(
            self,
            status: int,
            content_type: str,
            data: bytes,
            *,
            head_only: bool = False,
            cache_control: str | None = None,
        ) -> None:
            self._write_headers(status, content_type, len(data), cache_control=cache_control)
            if not head_only:
                self.wfile.write(data)

        # -- static files --------------------------------------------------

        def _serve_index(self, *, head_only: bool = False) -> None:
            index_path = static_dir.joinpath("index.html")
            if static_dir.is_dir() and index_path.is_file():
                try:
                    page = index_path.read_bytes().replace(_COMMAND_META, _command_meta())
                    self._write_bytes(200, "text/html", page, head_only=head_only)
                    return
                except (OSError, KeyError):
                    pass
            self._write_text(200, "text/html", _PLACEHOLDER_INDEX_HTML, head_only=head_only)

        def _serve_static(self, raw_name: str, *, head_only: bool = False) -> None:
            found = _static_file(static_dir, urllib.parse.unquote(raw_name))
            if found is None:
                self._write_json(*_not_found(), head_only=head_only)
                return
            candidate, parts = found
            content_type = _STATIC_CONTENT_TYPES.get(os.path.splitext(parts[-1])[1].lower())
            if content_type is None:
                content_type, _encoding = mimetypes.guess_type(parts[-1])
            pinned = len(parts) > 1 and parts[0] in _PINNED_STATIC_DIRS
            self._write_bytes(
                200,
                content_type or "application/octet-stream",
                candidate.read_bytes(),
                head_only=head_only,
                cache_control=_PINNED_CACHE_CONTROL if pinned else None,
            )

        # -- dispatch --------------------------------------------------------

        def _host_allowed(self) -> bool:
            """DNS-rebinding guard: a page on attacker.example that
            re-resolves its own name to 127.0.0.1 reaches this server as
            same-origin, but its requests still carry ``Host:
            attacker.example``. Only names in :func:`allowed_host_names`
            are answered. A request without ``Host`` (HTTP/1.0, not a
            browser) is allowed."""
            host = self.headers.get("Host")
            return host is None or _host_name(host) in host_names

        def _dispatch(self, body: dict | None, *, head_only: bool = False) -> None:
            request_ctx.as_of = None
            request_ctx.client = self.client_address[0] if self.client_address else None
            if not self._host_allowed():
                self._write_json(
                    *_forbidden("this Host is not allowed; start serve with --allowed-host NAME to add it"),
                    head_only=head_only,
                )
                store.close()
                return
            try:
                split = urllib.parse.urlsplit(self.path)
                path = split.path
                query = {
                    k: v[0]
                    for k, v in urllib.parse.parse_qs(split.query, keep_blank_values=True).items()
                }

                is_get_like = self.command in ("GET", "HEAD")

                if is_get_like and path == "/":
                    self._serve_index(head_only=head_only)
                    return
                if is_get_like and path.startswith("/static/"):
                    self._serve_static(path[len("/static/") :], head_only=head_only)
                    return

                handler = None
                if is_get_like:
                    handler = get_routes.get(path)
                    patterns = get_patterns
                elif self.command == "POST":
                    handler = post_routes.get(path)
                    patterns = post_patterns
                else:
                    patterns = ()

                if handler is None:
                    for pattern, candidate in patterns:
                        match = pattern.match(path)
                        if match:
                            handler = candidate
                            query = {**query, "id": urllib.parse.unquote(match.group(1))}
                            break

                if handler is None:
                    self._write_json(*_not_found("route not found"), head_only=head_only)
                    return

                result = handler(store, query, body)
                if isinstance(result, tuple) and len(result) == 3 and result[0] == "raw":
                    _tag, content_type, text = result
                    if content_type == "application/json":
                        text = _report_json_commands(text)
                    elif content_type.startswith("text/html"):
                        text = invocation.rewrite_rendered(text, "html")
                    elif content_type.startswith("text/markdown"):
                        text = invocation.rewrite_rendered(text, "markdown")
                    self._write_text(200, content_type, text, head_only=head_only)
                    return
                status, payload = result
                self._write_json(status, payload, head_only=head_only)
            except ImportError as exc:
                # A module this route imports on first use no longer
                # matches the ones loaded at start: look at the code on
                # disk now, rather than at the next tick, and say what
                # fixes it (docs/api.md, "Envelope").
                code = code_watch.check() if code_watch is not None else None
                self._write_json(*_restart_needed(exc, code), head_only=head_only)
            except Exception as exc:  # noqa: BLE001 - last-resort 500, see docs/api.md
                self._write_json(
                    *_internal_error(f"unexpected error ({type(exc).__name__})"), head_only=head_only
                )
            finally:
                # nit 30: ThreadingHTTPServer hands each request its own
                # thread, and Store keeps one sqlite3 connection per
                # thread (threading.local) -- without this, that
                # connection is only ever reclaimed when the thread
                # object itself is garbage collected, letting open
                # connections/file descriptors pile up under sustained
                # traffic instead of being released as soon as the
                # request that opened them finishes.
                store.close()

        def _method_not_allowed(self) -> None:
            # Finding 9: PUT/DELETE/PATCH/OPTIONS previously fell through
            # to BaseHTTPRequestHandler's own default 501 handler, which
            # never runs through _write_json -- so it carried none of
            # this API's security headers or {"ok": false, ...} envelope.
            self._write_json(*_error(405, "method_not_allowed", f"{self.command} is not supported on this route"))

        def do_GET(self) -> None:  # noqa: N802 - stdlib method name
            self._dispatch(None)

        def do_HEAD(self) -> None:  # noqa: N802 - stdlib method name
            self._dispatch(None, head_only=True)

        def do_PUT(self) -> None:  # noqa: N802 - stdlib method name
            self._method_not_allowed()

        def do_DELETE(self) -> None:  # noqa: N802 - stdlib method name
            self._method_not_allowed()

        def do_PATCH(self) -> None:  # noqa: N802 - stdlib method name
            self._method_not_allowed()

        def do_OPTIONS(self) -> None:  # noqa: N802 - stdlib method name
            self._method_not_allowed()

        def _reject_cross_site_post(self) -> str | None:
            """Same-origin guard for every mutating route (review S3):
            both POST routes this server exposes (``/api/profiles``,
            ``/api/sessions/<id>/tags``) are, without this check, a
            preflight-free "simple" request a cross-site page can issue
            blind -- the response is opaque to it (no CORS headers are
            ever sent), but a written profile is exactly what ``apply``
            later reads back. Returns ``None`` to allow the request, or a
            human-readable reason for the 403 otherwise.

            ``Origin`` is present on every fetch/XHR POST a browser
            issues (cross-site or not) and is compared against this
            server's own ``Host`` header -- itself always
            ``options.bind:options.port`` since nothing here handles TLS,
            so a straight ``http://`` comparison is exact. Older browsers
            that omit ``Origin`` on a same-origin POST are still covered
            by the ``Sec-Fetch-Site`` check below (sent by every current
            browser); a request with neither header (e.g. a same-machine
            CLI tool) is allowed, matching this API's existing no-auth,
            localhost-only posture (``docs/api.md``).
            """
            origin = self.headers.get("Origin")
            if origin is not None:
                host = self.headers.get("Host")
                if host is None or origin != f"http://{host}":
                    return "cross-origin requests are not allowed on this route"
            sec_fetch_site = self.headers.get("Sec-Fetch-Site")
            if sec_fetch_site is not None and sec_fetch_site not in ("same-origin", "none"):
                return "cross-site requests are not allowed on this route"
            return None

        def _drain_body(self, length: int) -> None:
            """Read and discard exactly ``length`` bytes from the socket,
            in bounded chunks (G5). Used for a body over
            ``_MAX_POST_BODY_BYTES``: it still must be consumed off the
            wire -- an HTTP/1.1 keep-alive connection with it left unread
            would corrupt the next request on the same connection, same
            as the plain read below -- but ``length`` itself is
            attacker-controlled (``Content-Length``) and exactly what the
            cap exists to bound, so this never allocates a buffer sized
            to it the way a single ``self.rfile.read(length)`` would."""
            remaining = length
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    return  # client closed early; nothing left to drain
                remaining -= len(chunk)

        def do_POST(self) -> None:  # noqa: N802 - stdlib method name
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length > _MAX_POST_BODY_BYTES:
                self._drain_body(length)
                self._write_json(
                    *_payload_too_large(f"request body must be <= {_MAX_POST_BODY_BYTES} bytes")
                )
                return
            # Read (and discard, on rejection) the body unconditionally,
            # before any check that might return early -- this is an
            # HTTP/1.1 keep-alive connection, and leaving unread bytes in
            # the socket would corrupt the next request on the same
            # connection.
            raw = self.rfile.read(length) if length > 0 else b""

            reason = self._reject_cross_site_post()
            if reason is None and not self._host_allowed():
                reason = "this Host is not allowed; start serve with --allowed-host NAME to add it"
            if reason is not None:
                self._write_json(*_forbidden(reason))
                return

            content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                self._write_json(*_bad_request("Content-Type must be application/json"))
                return

            body: dict | None = None
            if raw:
                try:
                    body = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self._write_json(*_bad_request("request body must be valid JSON"))
                    return
            self._dispatch(body)

    return Handler


__all__ = ["make_handler"]

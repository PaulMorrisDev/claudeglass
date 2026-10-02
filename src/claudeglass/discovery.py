"""Filesystem discovery: where transcripts live and which ones match a
query, without parsing any of them.

Layout assumed throughout (plan "Verified facts"): ``<projects_root>/
<slug>/<session_id>.jsonl`` for top-level sessions, ``<projects_root>/
<slug>/<session_id>/subagents/agent-<hex>.jsonl`` (+ sibling
``.meta.json``) for subagent transcripts, ``<projects_root>/<slug>/
<session_id>/workflows/wf_*.json`` for workflow runs, and (fix 3
addition) ``<projects_root>/<slug>/<session_id>/subagents/workflows/
<run_id>/agent-<hex>.jsonl`` for a workflow run's own agents — see
``workflows.py``'s module docstring for why this nested shape can't be
linked back to an invoking turn the way an ordinary subagent can.

Deviation from the plan, proposed here rather than silently made: the
plan states project slugs are "truncated to 200 chars plus a hash when
longer" without naming the hash algorithm or how it's appended — that
exact scheme isn't published. ``slug_for`` here uses the first 8 hex
characters of a SHA-256 digest of the untruncated slug text, joined with
a ``-``, which is deterministic and collision-resistant but is this
module's own choice, not a restatement of a documented format.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone, tzinfo
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import agent_roles, jsonl
from .model import TranscriptMeta
from .parse import detect_provider

_NON_ALNUM_RE = re.compile(r"[^A-Za-z0-9]")
_SLUG_MAX_CHARS = 200
_SLUG_HASH_HEX_CHARS = 8

#: Matches the ``Users-<name>``, ``home-<name>`` or ``c-Users-<name>``
#: shape ``slug_for`` produces from a real home-directory path (every
#: non-alphanumeric character, including ``:``/``\\``/``/``, becomes
#: ``-``, so ``C:\Users\alice\repo`` -> ``C--Users-alice-repo`` and
#: ``/home/alice/repo`` -> ``-home-alice-repo``) -- see ``redact_slug``.
_HOME_SEGMENT_RE = re.compile(r"(?i)(c-users|users|home)-([^-]+)")


def redact_slug(slug: str) -> str:
    """Replace the path segment immediately following a ``Users-``,
    ``home-`` or ``c-Users-`` marker (case-insensitive) with the literal
    ``<user>``, so a project slug built from a real filesystem path never
    carries a real username through the API or a rendered report (review
    finding 6: "raw slugs with usernames"). Applied at every API
    boundary (``service/store.py``'s ``sessions``/``session``/
    ``baselines`` read queries) and inside ``report.build_report`` itself
    (so the CLI and the service redact identically), never only at one
    of the two. A slug that matches none of those markers -- the common
    case, since most slugs are already a short project name -- is
    returned unchanged."""
    if not slug:
        return slug
    return _HOME_SEGMENT_RE.sub(lambda m: f"{m.group(1)}-<user>", slug)


def claude_root(explicit: str | Path | None = None) -> Path:
    """Claude Code's own folder: the one holding ``settings.json``,
    ``agents/`` and ``projects/``. ``explicit`` (a ``--claude-root``
    flag) wins; else ``$CLAUDE_CONFIG_DIR``; else ``~/.claude`` -- the
    same lookup Claude Code itself makes.

    The one place every command finds ``settings.json``: ``init``'s
    connect step and ``--repair-hook``, ``apply``, ``uninstall``,
    ``changes``, the dashboard's hook and statusline checks and the
    snapshot hook's own copy of this rule. It is never derived from
    ``--config-dir``: that flag moves this tool's own folder, which can
    sit anywhere, and its parent is then unrelated to Claude Code.
    """
    if explicit:
        return Path(explicit)
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(base) if base else (Path.home() / ".claude")


def projects_root() -> Path:
    """The root directory holding every ``<slug>/`` project directory.

    Honours ``CLAUDE_CONFIG_DIR`` (whole-tree override); falls back to
    ``~/.claude/projects``.
    """
    return claude_root() / "projects"


#: ``\\wsl.localhost\<distro>\...`` or ``\\wsl$\<distro>\...`` (either
#: slash direction): the Windows path to a folder inside a WSL distro.
_WSL_PATH_RE = re.compile(r"^[\\/]{2}(?:wsl\.localhost|wsl\$)[\\/]([^\\/]+)", re.IGNORECASE)

#: WSL distros that belong to Docker Desktop, never a place people run
#: Claude Code.
_WSL_SKIP_DISTROS = ("docker-desktop", "docker-desktop-data")


def projects_roots(explicit: Iterable[str | Path] | None = None, extra: Iterable[str | Path] = ()) -> list[Path]:
    """Every folder of project folders to read: ``explicit`` (the
    ``--projects-root`` flags) or else :func:`projects_root`, then
    ``extra`` (``config.toml``'s ``extra_projects_roots``, such as a WSL
    distro's ``~/.claude/projects``). A folder named twice is kept once.
    """
    roots = [Path(p) for p in explicit or ()] or [projects_root()]
    roots.extend(Path(p) for p in extra)
    seen: set[str] = set()
    result: list[Path] = []
    for root in roots:
        key = os.path.normcase(str(root)).rstrip("\\/")
        if key in seen:
            continue
        seen.add(key)
        result.append(root)
    return result


def source_label(path: str | Path | None) -> str:
    r"""Where a transcript came from, for display: ``"WSL: <distro>"``
    for a path inside a WSL distro (``\\wsl.localhost\Ubuntu\...``),
    otherwise ``"This computer"``. Never includes the rest of the path.
    """
    match = _WSL_PATH_RE.match(str(path or ""))
    return f"WSL: {match.group(1)}" if match else "This computer"


def find_wsl_projects_roots(run=subprocess.run) -> list[Path]:
    r"""Claude Code project folders inside this Windows computer's WSL
    distros: ``\\wsl.localhost\<distro>\home\<user>\.claude\projects``
    (and ``root``'s own). Distro names come from ``wsl.exe -l -q``.
    Returns ``[]`` off Windows, without WSL, or on any error. Looking
    starts a stopped distro, so only ``init`` calls this, never the
    dashboard's polling.
    """
    if sys.platform != "win32":
        return []
    try:
        completed = run(["wsl.exe", "-l", "-q"], capture_output=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0:
        return []
    raw = completed.stdout or b""
    # wsl.exe writes UTF-16 unless WSL_UTF8=1 is set.
    text = raw.decode("utf-16-le", errors="ignore") if b"\x00" in raw else raw.decode("utf-8", errors="ignore")
    found: list[Path] = []
    for line in text.splitlines():
        distro = line.strip().lstrip("\ufeff").strip()
        if not distro or distro.lower() in _WSL_SKIP_DISTROS:
            continue
        base = Path(rf"\\wsl.localhost\{distro}")
        candidates: list[Path] = []
        try:
            home = base / "home"
            if home.is_dir():
                candidates.extend(sorted(p / ".claude" / "projects" for p in home.iterdir() if p.is_dir()))
            candidates.append(base / "root" / ".claude" / "projects")
            found.extend(c for c in candidates if c.is_dir())
        except OSError:
            continue
    return found


def slug_for(cwd: str | Path) -> str:
    """The project slug for a working directory: non-alphanumeric
    characters become ``-``, truncated to 200 characters plus an 8-hex
    hash when longer (see the module docstring's deviation note).

    Honours ``CLAUDE_CODE_PROJECT_DIR_NAME`` whenever it's set, whether or
    not ``CLAUDE_CONFIG_DIR`` also moves the whole config tree — the two
    env vars are independent documented overrides, not a package deal.
    """
    project_dir_name = os.environ.get("CLAUDE_CODE_PROJECT_DIR_NAME")
    if project_dir_name:
        return project_dir_name

    raw = str(cwd)
    slug = _NON_ALNUM_RE.sub("-", raw)
    if len(slug) <= _SLUG_MAX_CHARS:
        return slug
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:_SLUG_HASH_HEX_CHARS]
    return f"{slug[:_SLUG_MAX_CHARS]}-{digest}"


def resolve_project_dirs(
    root: str | Path | Iterable[str | Path],
    slugs: list[str] | None = None,
    all_projects: bool = False,
    family_regex: str | None = None,
    exclude_projects: list[str] | None = None,
) -> list[Path]:
    """Resolve which project directories under ``root`` a query selects.

    Matching is case-insensitive (``slugs`` compares lower-cased names;
    ``family_regex`` is searched with ``re.IGNORECASE``). Results are
    de-duplicated by ``os.path.normcase(os.path.realpath(...))`` so
    Windows slug case variants of the same real directory, or a worktree
    symlinked back to its parent, only appear once.

    ``root`` may be one folder or several (see :func:`projects_roots`);
    a folder that is missing or can't be read is skipped.

    Precedence when more than one selector is given: ``all_projects``,
    then ``family_regex``, then ``slugs``. Returns ``[]`` if none of the
    three select anything (including when ``root`` doesn't exist).

    ``exclude_projects`` (fix 6, ``Config.exclude_projects``) is a list of
    slug regexes (``re.search``, case-insensitive — same convention as
    ``family_regex``) applied AFTER the selector above, dropping any
    candidate whose slug matches one of them even when it was explicitly
    named by ``slugs`` or matched by ``family_regex``/``all_projects`` —
    a standing "never touch this project" list, not a narrower selector.
    A malformed regex in the list is skipped rather than raising (same
    "never crash on a foreign shape" posture ``config.py`` documents for
    its own optional structure), since one bad entry in a user's
    exclude list shouldn't take discovery down entirely.
    """
    roots = [Path(root)] if isinstance(root, (str, Path)) else [Path(r) for r in root]
    candidates: list[Path] = []
    for one_root in roots:
        # A WSL distro's folder can vanish (WSL shut down, distro
        # removed): skip that folder rather than fail the whole scan.
        try:
            if one_root.is_dir():
                candidates.extend(sorted(p for p in one_root.iterdir() if p.is_dir()))
        except OSError:
            continue

    if all_projects:
        selected = candidates
    elif family_regex:
        pattern = re.compile(family_regex, re.IGNORECASE)
        selected = [p for p in candidates if pattern.search(p.name)]
    elif slugs:
        wanted = {s.lower() for s in slugs}
        selected = [p for p in candidates if p.name.lower() in wanted]
    else:
        selected = []

    if exclude_projects:
        exclude_patterns = []
        for raw_pattern in exclude_projects:
            try:
                exclude_patterns.append(re.compile(raw_pattern, re.IGNORECASE))
            except re.error:
                continue
        if exclude_patterns:
            selected = [
                p for p in selected if not any(pattern.search(p.name) for pattern in exclude_patterns)
            ]

    seen: set[str] = set()
    result: list[Path] = []
    for p in selected:
        key = os.path.normcase(os.path.realpath(p))
        if key in seen:
            continue
        seen.add(key)
        result.append(p)
    return result


def _resolve_window(
    days: int | None, since: str | None, until: str | None
) -> tuple[datetime | None, datetime | None]:
    """``(since, until)`` as aware datetimes. ``since`` wins over ``days``,
    and ``days`` here is rolling: now minus ``days`` x 24 hours, for the
    replays that divide by their own length (capture, the baseline, the
    reconcile and coaching passes). The dashboard and the report commands
    count calendar days instead: they resolve :func:`window_start` at
    their entry point and pass it in as ``since``."""
    since_dt: datetime | None = None
    until_dt: datetime | None = None
    if since is not None:
        since_dt = _parse_bound(since)
    elif days is not None:
        since_dt = datetime.now(timezone.utc) - timedelta(days=days)
    if until is not None:
        until_dt = _parse_bound(until)
    return since_dt, until_dt


def _parse_bound(value: str) -> datetime:
    """A ``--since``/``--until`` value as an aware datetime. A bare date or
    a time with no offset is read as UTC, as :func:`ts_in_window` reads
    transcript times, so it compares with them."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _zone(tz: str | tzinfo | None) -> tzinfo | None:
    """The zone ``tz`` names, or ``None`` for the machine's own zone.
    ``tz`` is an IANA name (``config.toml``'s ``tz``), a ``tzinfo`` (passed
    straight through) or empty. ``UTC`` and ``Etc/UTC`` resolve without
    ``tzdata``, which a bare Windows install lacks. A name that can't be
    resolved, whatever the reason (no ``tzdata``, a typo, a folder where a
    zone file should be), also gives ``None``, the same fallback
    ``usage._to_local`` and ``classify._to_local`` make."""
    if tz is None or tz == "":
        return None
    if isinstance(tz, tzinfo):
        return tz
    if tz in ("UTC", "Etc/UTC"):
        return timezone.utc
    try:
        return ZoneInfo(tz)
    except Exception:
        return None


def to_local(dt: datetime, tz: str | tzinfo | None = None) -> datetime:
    """``dt`` (aware) in ``tz``, or in the machine's own zone when ``tz``
    is empty or can't be resolved (see :func:`_zone`)."""
    zone = _zone(tz)
    return dt.astimezone(zone) if zone is not None else dt.astimezone()


def local_day(value: str | datetime, tz: str | tzinfo | None = None) -> str:
    """The calendar day, ``YYYY-MM-DD``, that ``value`` falls on in ``tz``
    (the machine's own zone when ``tz`` is empty or can't be resolved).
    ``value`` is an aware datetime or an ISO 8601 string; a time with no
    offset is read as UTC, as :func:`ts_in_window` reads transcript times.
    Raises ``ValueError`` for a string that isn't a time."""
    dt = _parse_bound(value) if isinstance(value, str) else value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    # isoformat, not strftime("%Y"): see utc_stamp.
    return to_local(dt, tz).date().isoformat()


def zone_name(tz: str | tzinfo | None) -> str | None:
    """The IANA name of the zone days are counted in, or ``None`` when
    that is the machine's own zone (``tz`` empty or unresolvable) or a
    zone with no name (a fixed offset), for the dashboard to show which
    zone its days follow."""
    zone = _zone(tz)
    if zone is None:
        return None
    if isinstance(tz, str):
        return tz
    if zone is timezone.utc:
        return "UTC"
    key = getattr(zone, "key", None)
    return key if isinstance(key, str) else None


def local_midnight(day: date, tz: str | tzinfo | None = None) -> datetime:
    """The first instant of ``day`` in ``tz`` (the machine's own zone when
    ``tz`` is empty or can't be resolved), as an aware UTC datetime. Right
    across a clock change: when midnight doesn't exist (the clocks jump
    over it) this is the first moment that does, and when it happens twice
    (the clocks go back across it) the earlier one."""
    zone = _zone(tz)
    if zone is not None:
        local = datetime.combine(day, dtime.min, tzinfo=zone)
    else:
        local = datetime.combine(day, dtime.min).astimezone()
    return local.astimezone(timezone.utc)


def window_start(days: int, tz: str | tzinfo | None = None, *, now: datetime | None = None) -> datetime:
    """Where a calendar window of ``days`` days starts: local midnight of
    today minus ``days - 1`` days, as an aware UTC datetime. "Last 7 days"
    is today and the six days before it, whole days in ``tz`` (the
    machine's own zone when ``tz`` is empty or can't be resolved), not
    7 x 24 hours back from this minute. ``now`` (aware) stands in for the
    current time. Raises ``ValueError`` for fewer than one day."""
    if days < 1:
        raise ValueError(f"days must be at least 1, got {days}")
    if now is None:
        now = datetime.now(timezone.utc)
    return local_midnight(to_local(now, tz).date() - timedelta(days=days - 1), tz)


def utc_stamp(moment: datetime, *, seconds: bool = False) -> str:
    """``moment``'s clock reading (already in UTC) as the string a
    ``since`` takes: rounded down to the minute, e.g.
    ``2026-09-25T00:00:00Z``, or to the second with ``seconds``. The year
    is always four digits: ``strftime("%Y")`` leaves a year before 1000
    unpadded on Linux ("931-03-01"), which ``fromisoformat`` rejects and
    which sorts after "2026" as a string. A window of hundreds of
    thousands of days reaches such a year."""
    second = moment.second if seconds else 0
    return (
        f"{moment.year:04d}-{moment.month:02d}-{moment.day:02d}"
        f"T{moment.hour:02d}:{moment.minute:02d}:{second:02d}Z"
    )


def window_start_iso(days: int, tz: str | tzinfo | None = None, *, now: datetime | None = None) -> str:
    """:func:`window_start` as the minute-shaped UTC string a ``since``
    takes, e.g. ``2026-09-25T00:00:00Z``."""
    return utc_stamp(window_start(days, tz, now=now))


#: How a session is matched to a window. ``last-reply`` (the default): the
#: session's last reply, from its top-level or any subagent transcript,
#: falls in the window. ``mtime``: the top-level file's modification time,
#: which Claude Code moves forward when it appends titles and other
#: metadata to an old transcript, so a session with no replies in the
#: window can count. ``timestamp``: the first reply's time.
WINDOW_BY = ("last-reply", "mtime", "timestamp")


def ts_in_window(ts: str | None, since_dt: datetime | None, until_dt: datetime | None) -> bool:
    """Whether ``ts`` (ISO 8601, e.g. a session's last reply) falls in
    ``[since_dt, until_dt]``. With no window everything is in; with one, a
    missing or unreadable time is out."""
    if since_dt is None and until_dt is None:
        return True
    if not ts:
        return False
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if since_dt is not None and dt < since_dt:
        return False
    if until_dt is not None and dt > until_dt:
        return False
    return True


def _session_window_ts(path: Path, window_by: str) -> datetime | None:
    if window_by == "mtime":
        try:
            return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            return None
    if window_by == "timestamp":
        try:
            for _line_no, d in jsonl.iter_lines(path):
                if d.get("type") not in ("user", "assistant"):
                    continue
                ts_raw = d.get("timestamp")
                if not isinstance(ts_raw, str) or not ts_raw:
                    continue
                try:
                    return datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                except ValueError:
                    continue
        except OSError:
            return None
        return None
    raise ValueError(f"unknown window_by: {window_by!r}")


def find_sessions(
    project_dir: str | Path,
    days: int | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int | None = None,
    window_by: str = "last-reply",
) -> list[Path]:
    """List top-level session transcripts (``<project_dir>/*.jsonl``)
    matching a window, newest first.

    ``window_by="mtime"`` uses the file's modification time;
    ``window_by="timestamp"`` reads the first ``user``/``assistant``
    line's ``timestamp`` field instead — slower (a partial parse per file)
    but immune to a file being touched without new content (e.g. a
    filesystem backup). ``window_by="last-reply"`` (default) needs the
    parsed replies, so here it keeps every file written since the window
    opened (a file's last reply is never later than its mtime) and
    :func:`~claudeglass.corpus.load_corpus` drops the sessions whose
    last reply falls outside the window.

    A file whose window key can't be determined (e.g. no user/assistant
    line, stat failure) is included only when no ``days``/``since``/
    ``until`` filter is active, and sorts last.
    """
    project_dir = Path(project_dir)
    if not project_dir.exists():
        return []
    since_dt, until_dt = _resolve_window(days, since, until)
    if window_by == "last-reply":
        window_by, until_dt = "mtime", None
    has_window_filter = since_dt is not None or until_dt is not None

    dated: list[tuple[Path, datetime | None]] = []
    for candidate in sorted(project_dir.glob("*.jsonl")):
        ts = _session_window_ts(candidate, window_by)
        if ts is None:
            if has_window_filter:
                continue
            dated.append((candidate, None))
            continue
        if since_dt is not None and ts < since_dt:
            continue
        if until_dt is not None and ts > until_dt:
            continue
        dated.append((candidate, ts))

    dated.sort(key=lambda pair: pair[1] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    paths = [p for p, _ts in dated]
    if limit is not None:
        paths = paths[:limit]
    return paths


def _read_meta_dict(jsonl_path: Path) -> dict:
    """The raw parsed ``.meta.json`` content sibling to ``jsonl_path``
    (``{}`` if missing or unparsable) — shared by both glob shapes
    :func:`find_subagents` reads.
    """
    meta_path = jsonl_path.with_name(jsonl_path.stem + ".meta.json")
    if not meta_path.exists():
        return {}
    try:
        loaded = json.loads(meta_path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def filter_subagents_by_window(
    pairs: list[tuple[Path, dict]], since: str | None, until: str | None
) -> list[tuple[Path, dict]]:
    """Filter ``find_subagents`` pairs to those whose own transcript's
    first ``user``/``assistant`` line timestamp falls inside
    ``[since, until]`` (ISO 8601, same convention as ``find_sessions``).
    This is the ``subagent_window="own"`` behaviour on ``find_subagents``,
    also usable standalone by a caller that already has a pair list from
    elsewhere.

    A pair whose window key can't be determined (no user/assistant line,
    stat failure) is kept only when neither ``since`` nor ``until`` is
    given, matching ``find_sessions``'s own tolerant-inclusion rule.
    """
    since_dt, until_dt = _resolve_window(None, since, until)
    has_window_filter = since_dt is not None or until_dt is not None
    kept: list[tuple[Path, dict]] = []
    for jsonl_path, meta_dict in pairs:
        ts = _session_window_ts(jsonl_path, "timestamp")
        if ts is None:
            if not has_window_filter:
                kept.append((jsonl_path, meta_dict))
            continue
        if since_dt is not None and ts < since_dt:
            continue
        if until_dt is not None and ts > until_dt:
            continue
        kept.append((jsonl_path, meta_dict))
    return kept


def find_subagents(
    project_dir: str | Path,
    session_id: str,
    since: str | None = None,
    until: str | None = None,
    subagent_window: str = "parent",
) -> list[tuple[Path, dict]]:
    """List ``(jsonl_path, meta_dict)`` pairs for a session's subagent
    transcripts. ``meta_dict`` is the raw parsed ``.meta.json`` content
    (``{}`` if missing or unparsable) — callers needing a
    ``TranscriptMeta`` should pass it through ``load_meta`` instead, this
    is the raw form for callers that want individual keys.

    Globs both the ordinary ``<session_id>/subagents/agent-*.jsonl``
    shape and the workflow-nested ``<session_id>/subagents/workflows/
    <run_id>/agent-*.jsonl`` shape (see ``workflows.py``'s module
    docstring) — a workflow-nested subagent's ``.meta.json`` was observed
    to carry no ``toolUseId``, so it can't be joined to an invoking turn
    the way an ordinary subagent is (``topology.index_tool_use_ids``);
    ``workflows.link_workflow_agents`` links these by path instead.
    ``load_meta`` derives ``TranscriptMeta.workflow_run_id`` for these
    from the same path shape.

    ``subagent_window`` selects how ``since``/``until`` apply (deviation
    note: the plan named this parameter on ``find_sessions``, but only
    this function has subagent transcripts to filter against a window —
    ``find_sessions`` only ever lists top-level session files). ``"parent"``
    (default, matches the seed script's own behaviour) returns every
    subagent of this session regardless of its own timestamp — a
    subagent inherits inclusion from its already-windowed parent session,
    and ``since``/``until`` are ignored. ``"own"`` additionally requires
    the subagent transcript's own first line timestamp to fall inside
    ``[since, until]`` (via :func:`filter_subagents_by_window`).
    """
    paths = find_subagent_paths(project_dir, session_id, since, until, subagent_window)
    return [(jsonl_path, _read_meta_dict(jsonl_path)) for jsonl_path in paths]


def find_subagent_paths(
    project_dir: str | Path,
    session_id: str,
    since: str | None = None,
    until: str | None = None,
    subagent_window: str = "parent",
) -> list[Path]:
    """:func:`find_subagents`'s paths, same arguments and order, without
    reading any ``.meta.json``: for a caller that loads each one through
    :func:`load_meta` anyway, where the raw dict would be a second read
    of every file.
    """
    if subagent_window not in ("parent", "own"):
        raise ValueError(f"unknown subagent_window: {subagent_window!r} (expected 'parent' or 'own')")

    subagents_dir = Path(project_dir) / session_id / "subagents"
    if not subagents_dir.exists():
        return []
    paths = sorted(subagents_dir.glob("agent-*.jsonl"))
    workflows_dir = subagents_dir / "workflows"
    if workflows_dir.exists():
        paths.extend(sorted(workflows_dir.glob("*/agent-*.jsonl")))

    if subagent_window == "own":
        paths = [path for path, _ in filter_subagents_by_window([(path, {}) for path in paths], since, until)]
    return paths


def find_workflows(project_dir: str | Path, session_id: str) -> list[Path]:
    """List a session's ``workflows/wf_*.json`` run files. Not parsed
    here — the plan defers ``WorkflowRun`` parsing to WP8.
    """
    workflows_dir = Path(project_dir) / session_id / "workflows"
    if not workflows_dir.exists():
        return []
    return sorted(workflows_dir.glob("wf_*.json"))


#: meta.json key -> TranscriptMeta attribute, for the fields that copy
#: straight across with a type check and no transformation.
_META_DIRECT_FIELDS: tuple[tuple[str, str, type], ...] = (
    ("agentType", "agent_type", str),
    ("spawnDepth", "spawn_depth", int),
    ("parentAgentId", "parent_agent_id", str),
    ("requestShape", "request_shape", str),
)


def load_meta(path: str | Path) -> TranscriptMeta:
    """Load one subagent ``.meta.json`` file into a ``TranscriptMeta``.

    Maps ``agentType``, ``description`` (length only, never the text),
    ``workflowPhase`` and ``description`` (-> ``role_word``, the one
    canonical word ``agent_roles.role_word`` finds in the phase, then the
    type, then the description's first words; never the text, so a phase
    or label that carries a path leaves nothing behind), the presence of
    either of those two keys (-> ``model_recorded``: the newer meta shape,
    where an absent ``model`` means the call set none), ``spawnDepth``,
    ``parentAgentId``, ``model`` (-> ``agent_model_alias``
    — ``model`` on ``TranscriptMeta`` isn't a field; the transcript's own
    turns carry the real per-turn ``model`` — and, from the same value,
    -> ``provider`` via ``parse.detect_provider``, a best guess before any
    turn is parsed that ``parse_transcript`` recomputes and takes
    precedence over once a turn exists), ``requestShape``,
    ``worktreeBranch`` (-> ``worktree_branch_present``, a bool: never the
    branch name itself), ``stoppedByUser`` and ``toolUseId`` (->
    ``tool_use_id``, linking the subagent back to the parent turn that
    spawned it). Returns a ``kind="subagent"`` ``TranscriptMeta`` (or
    ``kind="workflow-agent"`` for the workflow-nested layout below) with
    defaults for anything missing or the file being absent/unparsable —
    this never raises.

    ``agent_id`` and ``session_id`` are derived from ``path`` itself
    rather than the file's content, per the documented layout
    ``<projects_root>/<slug>/<session_id>/subagents/agent-<hex>.jsonl``
    (paired with ``agent-<hex>.meta.json``): ``agent_id`` is the filename
    stem with a trailing ``.meta.json``/``.json`` stripped, and
    ``session_id`` is the grandparent directory's name (``path``'s
    parent is ``subagents/``, its parent is ``<session_id>/``) — except
    for the workflow-nested layout ``<session_id>/subagents/workflows/
    <run_id>/agent-<hex>.jsonl`` (three levels under ``subagents/``, not
    one), detected by the immediate parent's parent being named
    ``workflows``: there, ``kind`` is ``"workflow-agent"``, ``session_id``
    is the great-grandparent and ``workflow_run_id`` (fix 3 addition) is
    set to the run id directory name. ``agent_type`` still comes from the
    ``.meta.json``: a named agent a workflow starts keeps its type, an
    unnamed one is ``"workflow-subagent"``. A ``path`` that isn't
    actually under either shape (e.g. a test fixture that hands
    ``load_meta`` a bare file) still derives *some* value for each —
    never raises — it just won't be meaningful.

    ``path`` (fix 3 addition, on the returned ``TranscriptMeta`` itself)
    is set to the sibling transcript file (``.meta.json`` -> ``.jsonl``
    in the same directory), and ``mtime_ns``/``size_bytes`` are that
    transcript file's own ``stat()`` — 0 for either when the transcript
    file doesn't exist (e.g. a ``.meta.json`` written before its
    ``.jsonl``, or a bare test fixture), never raising.
    """
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        raw = None
    if not isinstance(raw, dict):
        raw = {}

    meta = TranscriptMeta(kind="subagent")
    for json_key, attr, expected_type in _META_DIRECT_FIELDS:
        value = raw.get(json_key)
        if isinstance(value, expected_type) and not isinstance(value, bool):
            setattr(meta, attr, value)

    description = raw.get("description")
    if isinstance(description, str):
        meta.description_len = len(description)

    # Agent-roles addition (see model.py's TranscriptMeta.role_word
    # docstring): the one canonical word, then the phase, type and
    # description text is dropped. model_recorded is key presence only.
    meta.role_word = agent_roles.role_word(raw.get("workflowPhase"), raw.get("agentType"), description)
    meta.model_recorded = "description" in raw or "workflowPhase" in raw

    model = raw.get("model")
    if isinstance(model, str):
        meta.agent_model_alias = model
        # Batch C addition (see model.py's TranscriptMeta.provider
        # docstring): best guess before any turn is parsed.
        # parse_transcript recomputes this from the transcript's own
        # turns once one exists, which takes precedence.
        meta.provider = detect_provider(model)

    meta.worktree_branch_present = bool(raw.get("worktreeBranch"))

    stopped_by_user = raw.get("stoppedByUser")
    if isinstance(stopped_by_user, bool):
        meta.stopped_by_user = stopped_by_user

    tool_use_id = raw.get("toolUseId")
    if isinstance(tool_use_id, str):
        meta.tool_use_id = tool_use_id

    name = path.name
    if name.endswith(".meta.json"):
        meta.agent_id = name[: -len(".meta.json")]
    else:
        meta.agent_id = path.stem

    # Workflow-nested subagent: <session_id>/subagents/workflows/<run_id>/
    # agent-<hex>.meta.json — one directory level deeper than the
    # ordinary <session_id>/subagents/agent-<hex>.meta.json shape (see
    # workflows.py's module docstring).
    if path.parent.parent.name == "workflows":
        meta.kind = "workflow-agent"
        meta.workflow_run_id = path.parent.name
        meta.session_id = path.parent.parent.parent.parent.name
        run_file = path.parent.parent.parent.parent / "workflows" / f"{meta.workflow_run_id}.json"
        meta.workflow_agent_state = _workflow_agent_states(str(run_file)).get(meta.agent_id.removeprefix("agent-"))
    else:
        meta.session_id = path.parent.parent.name

    sibling_jsonl = path.parent / f"{meta.agent_id}.jsonl"
    meta.path = str(sibling_jsonl)
    try:
        stat = sibling_jsonl.stat()
    except OSError:
        pass
    else:
        meta.mtime_ns = stat.st_mtime_ns
        meta.size_bytes = stat.st_size

    return meta


_WORKFLOW_STATES_CACHE: dict[str, tuple[int, dict[str, str]]] = {}


def _workflow_agent_states(run_file: str) -> dict[str, str]:
    """Agent id -> end state (``done``/``error``/``progress``) from a
    finished workflow run file's ``workflowProgress``; ``{}`` while the
    run is still going (a state could still change) or when the file
    can't be read. Cached per file by mtime: a run file lists every one
    of its agents, and each agent's meta asks for it."""
    try:
        mtime = os.stat(run_file).st_mtime_ns
    except OSError:
        return {}
    cached = _WORKFLOW_STATES_CACHE.get(run_file)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    states: dict[str, str] = {}
    try:
        raw = json.loads(Path(run_file).read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        raw = None
    status = raw.get("status") if isinstance(raw, dict) else None
    if isinstance(status, str) and status not in ("running", "pending"):
        for entry in raw.get("workflowProgress") or ():
            if not isinstance(entry, dict) or entry.get("type") != "workflow_agent":
                continue
            agent_id, state = entry.get("agentId"), entry.get("state")
            if isinstance(agent_id, str) and isinstance(state, str):
                states[agent_id.removeprefix("agent-")] = state[:24]
    _WORKFLOW_STATES_CACHE[run_file] = (mtime, states)
    return states


__all__ = [
    "claude_root",
    "projects_root",
    "slug_for",
    "redact_slug",
    "resolve_project_dirs",
    "find_sessions",
    "ts_in_window",
    "to_local",
    "local_day",
    "zone_name",
    "local_midnight",
    "window_start",
    "window_start_iso",
    "WINDOW_BY",
    "find_subagents",
    "find_subagent_paths",
    "filter_subagents_by_window",
    "find_workflows",
    "load_meta",
]

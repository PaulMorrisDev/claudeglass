"""User-editable configuration (WP5): ``config.toml`` and per-session
overrides (``sessions.toml``).

Two files live under ``<config_dir>/`` (default ``~/.claude/claudeglass``,
or ``$CLAUDE_CONFIG_DIR/claudeglass`` when that env var moves the whole
config tree — the same resolution ``pricing.py`` uses for
``pricing.toml``, deliberately kept independent here rather than
importing ``pricing.CLAUDEGLASS_DIRNAME`` so this module has no
dependency beyond ``model.py``/the standard library):

- ``config.toml``: :func:`load_config` -> :class:`Config`. A missing
  file is not an error — every field just takes its documented default.
  Malformed TOML raises :class:`ConfigError`.
- ``sessions.toml``: per-session ``mode``/``purpose`` overrides that win
  ahead of ``classify.classify_mode``/``classify_purpose``'s rules (see
  ``classify.classify_session``). :func:`load_session_overrides` reads
  every entry; :func:`save_session_override` rewrites one entry in
  place, preserving every other session's entry untouched.

``config.toml``'s ``[capture]`` table (:class:`CaptureConfig`) switches
metrics capture on and off; :func:`set_capture` is the one writer, and
logs every change to ``capture-log.jsonl``. Its metric ids come from
``capture_catalogue``, a data-only module, so this module still needs
nothing from the package beyond it.

``tomllib`` (stdlib, read-only) has no counterpart writer, so
:func:`save_session_override` serialises TOML by hand — see
``_write_sessions_toml``. The format it writes back is deliberately the
same shape ``load_session_overrides`` reads, so a round trip through
both functions is lossless for the keys this module understands
(``mode``, ``purpose``, ``tags``); an entry section with unrecognised
extra keys is preserved as opaque scalars/lists (not dropped), but a
value shaped as a nested table under a session id is out of scope (the
plan's ``[sessions."<id>"]`` shape is flat) and is dropped with the rest
of that key silently — the same "never crash on a foreign shape"
posture ``pricing.py``/``snapshots.py`` take for optional structure.
"""

from __future__ import annotations

import csv
import json
import os
import re
import tomllib
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import capture_catalogue

#: Directory name under the resolved Claude config root holding
#: claudeglass's own files. Mirrors ``pricing.CLAUDEGLASS_DIRNAME``.
CLAUDEGLASS_DIRNAME = "claudeglass"

_ALLOWED_BILLING = frozenset({"api", "subscription", "auto"})
#: Same three values ``parse.detect_provider`` can return (fix 6).
_ALLOWED_PROVIDER = frozenset({"anthropic", "bedrock", "vertex"})
#: v0.3 addition (``init``/``apply`` milestone): where a profile/settings
#: change is written by default -- see ``profiles/diff.py``'s
#: ``render_unified_diff(scope=...)``, which this string is passed
#: straight through to.
_ALLOWED_APPLY_SCOPE = frozenset({"user", "project-local", "repo"})
#: v0.3 addition: whether a project directory is work or personal --
#: ``exclude_projects`` already excludes a slug from discovery entirely,
#: this is a softer per-project label ``init`` records when the user says
#: a project is personal without necessarily wanting it excluded outright.
_ALLOWED_PROJECT_KIND = frozenset({"work", "personal"})


#: Share of sessions capture may run in (``[capture] sample``): a session
#: is in or out by a hash of its id, and its subagents follow it.
CAPTURE_SAMPLES = (100, 50, 25, 10)

#: SEC-P5: bounds on ``retention_days``. 1 (never 0 or negative, which
#: would prune everything, including the session in progress) through
#: 36500 (100 years -- large enough that no real "keep forever" user
#: needs more, small enough to catch a typo like an extra zero or a
#: value pasted in milliseconds/hours by mistake).
RETENTION_DAYS_MIN = 1
RETENTION_DAYS_MAX = 36500

#: SEC-P8/G7: how long a capture signal file (``signals.prune``) or a
#: ``capture-log.jsonl`` record (:func:`prune_capture_log`) is kept when
#: the user hasn't set an explicit ``retention_days`` of their own --
#: unlike :attr:`Config.retention_days` (which only takes effect when the
#: user opts in, since it prunes visible report data), this is Token
#: Lens's own background telemetry and is never allowed to grow forever
#: just because nobody configured a retention window (previously: G7
#: found capture-log.jsonl was never pruned at all, under any setting).
#: An explicit ``retention_days`` still overrides this default in both
#: directions -- see ``service/watcher.py``'s per-tick prune call.
SIGNAL_RETENTION_DEFAULT_DAYS = 180

#: Every change to ``[capture]`` is appended here, one JSON object per
#: line, so a change can be lined up against the costs around it.
CAPTURE_LOG_NAME = "capture-log.jsonl"

#: EST-P5: every whatif estimate worth checking against what actually
#: happened is appended here, one JSON object per line -- see
#: :func:`append_prediction_log`. Ingested into the store's
#: ``predictions`` table by ``service.watcher._scan_predictions``, the
#: same way ``CAPTURE_LOG_NAME`` feeds ``change_points._capture_points``.
PREDICTION_LOG_NAME = "prediction-log.jsonl"


class ConfigError(Exception):
    """A config or session-overrides file could not be read or is not
    valid TOML/structure. Written to stand alone as user-facing output,
    same convention as ``pricing.PricingError``.
    """


@dataclass(slots=True)
class ProjectConfig:
    """Parsed ``<config_dir>/projects/<slug>.toml`` (v0.3 ``init``
    milestone): per-project answers to the "Asked, not guessed" questions
    the plan's "Configuration layers" section names, for a project whose
    answer differs from the global ``config.toml`` default. Every field is
    ``None`` when not set in the file, meaning "fall back to the global
    ``Config`` field of the same name".
    """

    #: "work" | "personal" | None.
    kind: str | None = None
    #: Whether this project's agents/skills are shared with colleagues
    #: (drives the default ``apply_scope`` for this project specifically).
    shared_project_config: bool | None = None
    #: Whether the user launches Claude Code in this project with
    #: ``--settings``/``CLAUDE_CONFIG_DIR`` overlays rather than the
    #: project's own settings files.
    launch_overlays: bool | None = None
    #: "user" | "project-local" | "repo" | None.
    apply_scope: str | None = None


@dataclass(slots=True)
class CaptureConfig:
    """``config.toml``'s ``[capture]`` table: whether metrics capture is on,
    and how much of it. See ``capture_catalogue`` for the metrics and
    levels, and ``hooks/capture-hook.py`` for the hook that reads this.
    """

    #: One of ``capture_catalogue.LEVELS``, or ``"custom"``.
    level: str = "off"
    #: The metric ids switched on when ``level`` is ``"custom"``; empty
    #: otherwise (a preset level decides its own).
    metrics: list[str] = field(default_factory=list)
    #: Percent of sessions captured, one of :data:`CAPTURE_SAMPLES`.
    sample: int = 100
    #: ISO-8601 time capture stops by itself, or ``""`` for never.
    until: str = ""
    #: Project slug regexes (``re.search``, case-insensitive) capture runs
    #: in; one starting ``!`` leaves matching projects out. Empty means
    #: every project.
    projects: list[str] = field(default_factory=list)
    #: Feedback toggles switched on (``capture_catalogue.FEEDBACK_IDS``).
    feedback: list[str] = field(default_factory=list)
    #: Live coaching toggles switched on
    #: (``capture_catalogue.COACHING_IDS``).
    coaching: list[str] = field(default_factory=list)
    #: Who writes the main session's ``[cg: ...]`` tags, one of
    #: ``capture_catalogue.TAGGERS``: Claude, at the end of its replies, or
    #: Claude Haiku, asked by the hook after each turn.
    tagger: str = capture_catalogue.DEFAULT_TAGGER
    #: ISO-8601 time capture was last switched on, or ``""`` while off.
    enabled_at: str = ""

    @property
    def is_on(self) -> bool:
        return self.level != "off"

    def active_metrics(self) -> tuple[str, ...]:
        """Every metric switched on, level and feedback toggles together
        (``capture_catalogue.active_metrics``)."""
        return capture_catalogue.active_metrics(self.level, self.metrics, self.feedback)

    def hook_metrics(self) -> tuple[str, ...]:
        """What the capture hook's settings.json entries are for: the
        metrics on, plus the coaching toggles that run through the hook
        (``coaching_notes``), whatever the level, and
        ``capture_catalogue.HAIKU_TAGGER_HOOK`` while Haiku writes the
        tags."""
        hooked = tuple(
            i for i in capture_catalogue.COACHING_IDS if i in self.coaching and capture_catalogue.METRICS_BY_ID[i].hooks
        )
        haiku = (capture_catalogue.HAIKU_TAGGER_HOOK,) if self.haiku_tags else ()
        return self.active_metrics() + hooked + haiku

    @property
    def haiku_tags(self) -> bool:
        """Whether Haiku writes the main session's tags."""
        return self.tagger == "haiku"

    @property
    def coaching_notes_on(self) -> bool:
        return "coaching_notes" in self.coaching

    def expired(self, now: datetime | None = None) -> bool:
        """Whether ``until`` has passed (``False`` when unset)."""
        stop = _parse_iso(self.until)
        if stop is None:
            return False
        return (now or datetime.now(timezone.utc)) >= stop


def _parse_iso(value: str) -> datetime | None:
    """An ISO-8601 date or time as an aware UTC ``datetime`` (a bare date
    is its midnight UTC; a naive time is taken as UTC), or ``None``."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(slots=True)
class Config:
    """Parsed ``config.toml`` (or all-defaults when the file is absent)."""

    #: "api" | "subscription" once loaded: ``load_config`` resolves
    #: config.toml's "auto" (also the default when the key is absent) to
    #: one of the two, see :func:`resolve_billing`.
    billing: str = "api"
    #: Why ``billing`` has its value, in plain words, for the report
    #: header and the dashboard. Empty on a directly constructed Config.
    billing_source: str = ""
    #: IANA zone name, or None for the machine's own local zone.
    tz: str | None = None
    #: Free-form, passed to other packages' ``from_config`` (plan
    #: "Classification"/"RE-CACHE" sections) — this module doesn't
    #: interpret its contents, only loads/validates its shape.
    thresholds: dict = field(default_factory=dict)
    recache: dict = field(default_factory=dict)
    min_sessions: int = 5
    min_turns: int = 200
    allow_titles: bool = False
    pricing_path: str | None = None
    #: Slug regexes (``re.search``, case-insensitive — same convention as
    #: ``discovery.resolve_project_dirs``'s own ``family_regex``) for
    #: project directories to leave out of every discovery call. A slug
    #: matching any one of these is excluded even when explicitly named
    #: by ``--project`` or matched by ``--project-family``/
    #: ``--all-projects`` — this is a standing "never touch this project"
    #: list (e.g. a work project on a personal machine), not a narrower
    #: selector. Fix 6 addition.
    exclude_projects: list[str] = field(default_factory=list)
    #: More folders of project folders to read besides this computer's
    #: own ``~/.claude/projects``, such as the one inside a WSL distro
    #: (reached from Windows through ``wsl.localhost``). ``init`` offers
    #: the ones it finds; every command and the dashboard read them
    #: (see ``discovery.projects_roots``).
    extra_projects_roots: list[str] = field(default_factory=list)
    #: Sessions older than this many days (by the same ``mtime``/
    #: ``timestamp`` window key ``discovery.find_sessions`` already
    #: understands) are outside the tool's normal window — not enforced
    #: by this module, just carried through for a future caller (report
    #: assembly/CLI) to apply as its own default ``--days`` when neither
    #: ``--days`` nor ``--since``/``--until`` is given explicitly on the
    #: command line. ``None`` means no retention window. Fix 6 addition.
    retention_days: int | None = None
    #: Force every session's ``TranscriptMeta.provider`` to this value
    #: (``"anthropic"``/``"bedrock"``/``"vertex"``) rather than deriving
    #: it per-session from the model id (see ``parse.detect_provider``) —
    #: for an account that's always on one provider and wants the report
    #: header to say so unambiguously even for a session with no model
    #: line at all. ``None`` (the default) leaves per-session detection
    #: alone. Fix 6 addition.
    provider: str | None = None
    #: v0.3 ``init``/``baseline`` addition: the length in days of the
    #: "onboarding capture window" started by ``init`` (plan "Running on
    #: other people's machines" / "Asked, not guessed" sections) — the
    #: minimum corpus age ``baseline`` treats as non-provisional. ``None``
    #: means no capture window has been started (``init`` has not run, or
    #: this config predates v0.3).
    capture_window: int | None = None
    #: ISO-8601 UTC timestamp (``datetime.isoformat()``) of the moment
    #: ``init`` started the capture window, or ``None`` if not started.
    #: A plain string (not parsed to ``datetime``) so a malformed value
    #: never fails config loading — ``baseline.capture_status`` is the
    #: one place that parses it, and reports rather than raises on a bad
    #: value, the same "never crash on a foreign shape" posture the
    #: module docstring already describes for ``sessions.toml``.
    capture_started: str | None = None
    #: Whether the user launches Claude Code with ``--settings``/
    #: ``CLAUDE_CONFIG_DIR`` overlays rather than each project's own
    #: settings files, as a global default (overridable per project via
    #: :class:`ProjectConfig.launch_overlays`).
    launch_overlays: bool = False
    #: Whether project-level agents/skills are shared with colleagues
    #: (e.g. committed to a shared repo), as a global default
    #: (overridable per project via
    #: :class:`ProjectConfig.shared_project_config`).
    shared_project_config: bool = False
    #: Default scope a profile ``apply`` writes to when neither the CLI
    #: nor a project's own :class:`ProjectConfig.apply_scope` says
    #: otherwise. One of ``_ALLOWED_APPLY_SCOPE``.
    apply_scope: str = "user"
    #: Per-project answers, keyed by project slug (``discovery.slug_for``)
    #: — see :class:`ProjectConfig`. Loaded from
    #: ``<config_dir>/projects/<slug>.toml`` by :func:`load_config`.
    projects: dict[str, ProjectConfig] = field(default_factory=dict)
    #: v4-saver-roi addition: explicit MCP server / plugin / skill names
    #: from ``config.toml``'s ``[savers]`` table (``names = [...]``) that
    #: ``savers.detect_savers`` should always treat as a candidate
    #: token-saver tool, regardless of whether its name matches the
    #: auto-detection regex — for a saver whose name gives no lexical
    #: hint at all (a codename, an acronym).
    savers: list[str] = field(default_factory=list)
    #: Metrics capture (``[capture]``). Off unless the user opts in.
    capture: CaptureConfig = field(default_factory=CaptureConfig)

    def describe(self) -> list[str]:
        """Lines for the report header (plan "Renderers and CLI"
        section)."""
        lines = [
            f"billing: {self.billing}" + (f" ({self.billing_source})" if self.billing_source else ""),
            f"timezone: {self.tz or 'local (machine)'}",
            f"min_sessions: {self.min_sessions}",
            f"min_turns: {self.min_turns}",
            f"allow_titles: {self.allow_titles}",
            f"pricing_path: {self.pricing_path or 'default (packaged/config-dir)'}",
        ]
        if self.thresholds:
            lines.append(f"thresholds: {self.thresholds}")
        if self.recache:
            lines.append(f"recache: {self.recache}")
        if self.exclude_projects:
            lines.append(f"exclude_projects: {self.exclude_projects}")
        if self.extra_projects_roots:
            lines.append(f"extra_projects_roots: {len(self.extra_projects_roots)} folder(s)")
        if self.retention_days is not None:
            lines.append(f"retention_days: {self.retention_days}")
        if self.provider is not None:
            lines.append(f"provider: {self.provider}")
        if self.capture_window is not None:
            lines.append(f"capture_window: {self.capture_window}")
        if self.capture_started is not None:
            lines.append(f"capture_started: {self.capture_started}")
        if self.launch_overlays:
            lines.append(f"launch_overlays: {self.launch_overlays}")
        if self.shared_project_config:
            lines.append(f"shared_project_config: {self.shared_project_config}")
        if self.apply_scope != "user":
            lines.append(f"apply_scope: {self.apply_scope}")
        if self.projects:
            lines.append(f"projects: {sorted(self.projects)}")
        if self.savers:
            lines.append(f"savers: {self.savers}")
        if self.capture.is_on:
            sample = f", {self.capture.sample}% of sessions" if self.capture.sample != 100 else ""
            lines.append(f"capture: {self.capture.level}{sample}")
        return lines


def _default_config_dir() -> Path:
    """``~/.claude/claudeglass``, or ``$CLAUDE_CONFIG_DIR/claudeglass`` when
    ``CLAUDE_CONFIG_DIR`` moves the whole config tree elsewhere. Mirrors
    ``pricing._default_claudeglass_dir()`` (see the module docstring for
    why this isn't a shared import).
    """
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    root = Path(base) if base else (Path.home() / ".claude")
    return root / CLAUDEGLASS_DIRNAME


def _resolve_config_dir(config_dir: str | Path | None) -> Path:
    return Path(config_dir) if config_dir is not None else _default_config_dir()


def _read_toml(path: Path, *, what: str) -> dict | None:
    """Read and parse one TOML file. Returns ``None`` if it doesn't
    exist; raises :class:`ConfigError` if it can't be read or isn't
    valid TOML.
    """
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {what}: {path} ({exc})") from exc
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"malformed {what} ({path}): {exc}") from exc


# -- config.toml --------------------------------------------------------


def _build_config(data: dict, path: Path) -> Config:
    config = Config()

    billing = data.get("billing", "auto")
    if not isinstance(billing, str) or billing not in _ALLOWED_BILLING:
        raise ConfigError(
            f"config file {path}: 'billing' must be one of {sorted(_ALLOWED_BILLING)}, got {billing!r}"
        )
    config.billing = billing

    tz = data.get("tz")
    if tz is not None and not isinstance(tz, str):
        raise ConfigError(f"config file {path}: 'tz' must be a string")
    config.tz = tz

    thresholds = data.get("thresholds", {})
    if not isinstance(thresholds, dict):
        raise ConfigError(f"config file {path}: 'thresholds' must be a table")
    config.thresholds = dict(thresholds)

    recache = data.get("recache", {})
    if not isinstance(recache, dict):
        raise ConfigError(f"config file {path}: 'recache' must be a table")
    config.recache = dict(recache)

    min_sessions = data.get("min_sessions", config.min_sessions)
    if not isinstance(min_sessions, int) or isinstance(min_sessions, bool):
        raise ConfigError(f"config file {path}: 'min_sessions' must be an integer")
    config.min_sessions = min_sessions

    min_turns = data.get("min_turns", config.min_turns)
    if not isinstance(min_turns, int) or isinstance(min_turns, bool):
        raise ConfigError(f"config file {path}: 'min_turns' must be an integer")
    config.min_turns = min_turns

    allow_titles = data.get("allow_titles", config.allow_titles)
    if not isinstance(allow_titles, bool):
        raise ConfigError(f"config file {path}: 'allow_titles' must be a boolean")
    config.allow_titles = allow_titles

    pricing_path = data.get("pricing_path")
    if pricing_path is not None and not isinstance(pricing_path, str):
        raise ConfigError(f"config file {path}: 'pricing_path' must be a string")
    config.pricing_path = pricing_path

    exclude_projects = data.get("exclude_projects", [])
    if not isinstance(exclude_projects, list) or not all(
        isinstance(item, str) for item in exclude_projects
    ):
        raise ConfigError(f"config file {path}: 'exclude_projects' must be a list of strings")
    # SEC-P5: compiled here, at load, the same as 'capture.projects' just
    # above -- so a typo'd regex is a load-time ConfigError the user sees
    # right away, not a pattern that silently stops excluding anything
    # once it reaches discovery.resolve_project_dirs/corpus's own
    # skip-and-carry-on compilation (still needed there as defense in
    # depth for a caller that builds the list itself, e.g. ``serve
    # --exclude-project``, without going through this loader).
    for pattern in exclude_projects:
        try:
            re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise ConfigError(f"config file {path}: 'exclude_projects' has a bad pattern {pattern!r} ({exc})") from exc
    config.exclude_projects = list(exclude_projects)

    extra_projects_roots = data.get("extra_projects_roots", [])
    if not isinstance(extra_projects_roots, list) or not all(
        isinstance(item, str) for item in extra_projects_roots
    ):
        raise ConfigError(f"config file {path}: 'extra_projects_roots' must be a list of strings")
    config.extra_projects_roots = list(extra_projects_roots)

    retention_days = data.get("retention_days")
    if retention_days is not None and (
        not isinstance(retention_days, int) or isinstance(retention_days, bool)
    ):
        raise ConfigError(f"config file {path}: 'retention_days' must be an integer")
    if retention_days is not None and not (RETENTION_DAYS_MIN <= retention_days <= RETENTION_DAYS_MAX):
        raise ConfigError(
            f"config file {path}: 'retention_days' must be between {RETENTION_DAYS_MIN} and "
            f"{RETENTION_DAYS_MAX}, got {retention_days}"
        )
    config.retention_days = retention_days

    provider = data.get("provider")
    if provider is not None and (not isinstance(provider, str) or provider not in _ALLOWED_PROVIDER):
        raise ConfigError(
            f"config file {path}: 'provider' must be one of {sorted(_ALLOWED_PROVIDER)}, got {provider!r}"
        )
    config.provider = provider

    capture_window = data.get("capture_window")
    if capture_window is not None and (
        not isinstance(capture_window, int) or isinstance(capture_window, bool)
    ):
        raise ConfigError(f"config file {path}: 'capture_window' must be an integer")
    config.capture_window = capture_window

    capture_started = data.get("capture_started")
    if capture_started is not None and not isinstance(capture_started, str):
        raise ConfigError(f"config file {path}: 'capture_started' must be a string")
    config.capture_started = capture_started

    launch_overlays = data.get("launch_overlays", config.launch_overlays)
    if not isinstance(launch_overlays, bool):
        raise ConfigError(f"config file {path}: 'launch_overlays' must be a boolean")
    config.launch_overlays = launch_overlays

    shared_project_config = data.get("shared_project_config", config.shared_project_config)
    if not isinstance(shared_project_config, bool):
        raise ConfigError(f"config file {path}: 'shared_project_config' must be a boolean")
    config.shared_project_config = shared_project_config

    apply_scope = data.get("apply_scope", config.apply_scope)
    if not isinstance(apply_scope, str) or apply_scope not in _ALLOWED_APPLY_SCOPE:
        raise ConfigError(
            f"config file {path}: 'apply_scope' must be one of {sorted(_ALLOWED_APPLY_SCOPE)}, got {apply_scope!r}"
        )
    config.apply_scope = apply_scope

    savers = data.get("savers", {})
    if not isinstance(savers, dict):
        raise ConfigError(f"config file {path}: 'savers' must be a table, e.g. [savers]\\nnames = [...]")
    saver_names = savers.get("names", [])
    if not isinstance(saver_names, list) or not all(isinstance(item, str) for item in saver_names):
        raise ConfigError(f"config file {path}: 'savers.names' must be a list of strings")
    config.savers = list(saver_names)

    capture = data.get("capture", {})
    if not isinstance(capture, dict):
        raise ConfigError(f"config file {path}: 'capture' must be a table, e.g. [capture]\nlevel = \"essentials\"")
    config.capture = _build_capture_config(capture, path)

    return config


def _capture_list(table: dict, key: str, path: Path, allowed=None) -> list[str]:
    value = table.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"config file {path}: 'capture.{key}' must be a list of strings")
    if allowed is not None:
        unknown = [item for item in value if item not in allowed]
        if unknown:
            raise ConfigError(
                f"config file {path}: 'capture.{key}' has unknown {', '.join(repr(u) for u in unknown)}; "
                f"known: {', '.join(allowed)}"
            )
    return list(value)


def _build_capture_config(table: dict, path: Path) -> CaptureConfig:
    capture = CaptureConfig()
    levels = (*capture_catalogue.LEVELS, capture_catalogue.CUSTOM_LEVEL)
    level = table.get("level", capture.level)
    if not isinstance(level, str) or level not in levels:
        raise ConfigError(f"config file {path}: 'capture.level' must be one of {list(levels)}, got {level!r}")
    capture.level = level
    # CAP-5: a config.toml written before a metric's retirement may still
    # list it (``capture_catalogue.RETIRED_METRIC_IDS``) -- accepted here
    # so the file keeps loading; ``with_requirements``/``active_metrics``
    # drop it from what's actually asked or shown.
    capture.metrics = _capture_list(
        table, "metrics", path, capture_catalogue.LEVEL_METRIC_IDS + capture_catalogue.RETIRED_METRIC_IDS
    )
    sample = table.get("sample", capture.sample)
    if isinstance(sample, bool) or sample not in CAPTURE_SAMPLES:
        raise ConfigError(
            f"config file {path}: 'capture.sample' must be one of {list(CAPTURE_SAMPLES)}, got {sample!r}"
        )
    capture.sample = sample
    for key in ("until", "enabled_at"):
        value = table.get(key, "")
        if not isinstance(value, str) or (value and _parse_iso(value) is None):
            raise ConfigError(f"config file {path}: 'capture.{key}' must be an ISO-8601 date or time, got {value!r}")
        setattr(capture, key, value)
    capture.projects = _capture_list(table, "projects", path)
    for pattern in capture.projects:
        try:
            re.compile(pattern[1:] if pattern.startswith("!") else pattern)
        except re.error as exc:
            raise ConfigError(f"config file {path}: 'capture.projects' has a bad pattern {pattern!r} ({exc})") from exc
    capture.feedback = _capture_list(table, "feedback", path, capture_catalogue.FEEDBACK_IDS)
    capture.coaching = _capture_list(table, "coaching", path, capture_catalogue.COACHING_IDS)
    tagger = table.get("tagger", capture.tagger)
    if not isinstance(tagger, str) or tagger not in capture_catalogue.TAGGERS:
        raise ConfigError(
            f"config file {path}: 'capture.tagger' must be one of {list(capture_catalogue.TAGGERS)}, got {tagger!r}"
        )
    capture.tagger = tagger
    return capture


def _build_project_config(data: dict, path: Path) -> ProjectConfig:
    project = ProjectConfig()

    kind = data.get("kind")
    if kind is not None and (not isinstance(kind, str) or kind not in _ALLOWED_PROJECT_KIND):
        raise ConfigError(
            f"project config {path}: 'kind' must be one of {sorted(_ALLOWED_PROJECT_KIND)}, got {kind!r}"
        )
    project.kind = kind

    shared_project_config = data.get("shared_project_config")
    if shared_project_config is not None and not isinstance(shared_project_config, bool):
        raise ConfigError(f"project config {path}: 'shared_project_config' must be a boolean")
    project.shared_project_config = shared_project_config

    launch_overlays = data.get("launch_overlays")
    if launch_overlays is not None and not isinstance(launch_overlays, bool):
        raise ConfigError(f"project config {path}: 'launch_overlays' must be a boolean")
    project.launch_overlays = launch_overlays

    apply_scope = data.get("apply_scope")
    if apply_scope is not None and (
        not isinstance(apply_scope, str) or apply_scope not in _ALLOWED_APPLY_SCOPE
    ):
        raise ConfigError(
            f"project config {path}: 'apply_scope' must be one of {sorted(_ALLOWED_APPLY_SCOPE)}, got {apply_scope!r}"
        )
    project.apply_scope = apply_scope

    return project


def load_project_configs(config_dir: str | Path | None = None) -> dict[str, ProjectConfig]:
    """Load every ``<config_dir>/projects/<slug>.toml`` file into
    ``slug -> ProjectConfig`` (keyed by filename stem). A missing
    ``projects/`` directory returns ``{}``. Raises :class:`ConfigError`
    for a file that can't be read, isn't valid TOML, or has a field of
    the wrong shape/value — same posture as :func:`load_config`.
    """
    resolved_dir = _resolve_config_dir(config_dir)
    projects_dir = resolved_dir / "projects"
    if not projects_dir.is_dir():
        return {}

    result: dict[str, ProjectConfig] = {}
    for toml_path in sorted(projects_dir.glob("*.toml")):
        data = _read_toml(toml_path, what="project config file")
        if data is None:
            continue
        result[toml_path.stem] = _build_project_config(data, toml_path)
    return result


def _usage_log_has_rate_limits(config_dir: Path) -> bool:
    """Whether ``<config_dir>/usage-log.csv`` holds any usage-limit
    reading (a ``five_hour``/``seven_day`` row with a used percentage).
    Claude Code only reports usage limits to Pro and Max plans, so one
    such row means a subscription."""
    path = config_dir / "usage-log.csv"
    if not path.exists():
        return False
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            reader = csv.reader(fh)
            next(reader, None)
            for raw in reader:
                # log_usage.CSV_FIELDS: logged_at, session_id, window, used_percentage, ...
                if len(raw) >= 4 and raw[2] and raw[2] != "context_window" and raw[3].strip():
                    return True
    except (OSError, csv.Error, UnicodeDecodeError):
        return False
    return False


def resolve_billing(config: Config, config_dir: Path) -> None:
    """Resolve ``config.billing == "auto"`` in place and record why in
    ``config.billing_source``."""
    if config.billing != "auto":
        config.billing_source = "set in config.toml"
    elif _usage_log_has_rate_limits(config_dir):
        config.billing = "subscription"
        config.billing_source = "automatic: usage-limit readings were found, so this is a Pro or Max plan"
    else:
        config.billing = "api"
        config.billing_source = (
            "automatic: no usage-limit readings found, so amounts are pay-per-token. "
            "Set billing in config.toml if you have a Pro or Max plan"
        )


def saved_billing(config_dir: str | Path | None = None) -> str | None:
    """``billing`` as written in ``config.toml`` (``api``,
    ``subscription`` or ``auto``, before :func:`resolve_billing` works
    ``auto`` out), or ``None`` when the file or the key is missing: the
    "how do you pay" question has never been answered. Raises
    :class:`ConfigError` as :func:`load_config` does."""
    data = _read_toml(_resolve_config_dir(config_dir) / "config.toml", what="config file")
    value = (data or {}).get("billing")
    return value if isinstance(value, str) and value in _ALLOWED_BILLING else None


def saved_pricing_path(config_dir: str | Path | None = None) -> str | None:
    """``pricing_path`` as written in ``config.toml``, or ``None`` when
    the file or the key is missing. Reads only that one file, so it is
    cheap enough for the service's scanner to call on every tick, unlike
    :func:`load_config` (which also works out billing from the usage log
    and loads every project's config). Raises :class:`ConfigError` as
    :func:`load_config` does."""
    data = _read_toml(_resolve_config_dir(config_dir) / "config.toml", what="config file")
    value = (data or {}).get("pricing_path")
    return value if isinstance(value, str) else None


def load_config(config_dir: str | Path | None = None) -> Config:
    """Load ``<config_dir>/config.toml`` (``config_dir`` defaults to
    ``~/.claude/claudeglass``, honouring ``CLAUDE_CONFIG_DIR``). A missing
    file returns every-field-default :class:`Config`. Raises
    :class:`ConfigError` for a file that can't be read, isn't valid
    TOML, or has a field of the wrong shape/value.
    """
    resolved_dir = _resolve_config_dir(config_dir)
    path = resolved_dir / "config.toml"
    data = _read_toml(path, what="config file")
    if data is None:
        config = Config()
        config.billing = "auto"
    else:
        config = _build_config(data, path)
    resolve_billing(config, resolved_dir)
    config.projects = load_project_configs(resolved_dir)
    return config


def save_project_config(
    config_dir: str | Path | None, slug: str, project_config: ProjectConfig
) -> Path:
    """Write ``<config_dir>/projects/<slug>.toml`` from
    ``project_config``, overwriting any existing file for that slug
    whole (unlike :func:`save_session_override`, a project's answers are
    a handful of scalars set once by ``init``, not an incrementally
    merged table, so there is no "preserve other keys" concern here).
    Only fields that are not ``None`` are written. Returns the path
    written.
    """
    resolved_dir = _resolve_config_dir(config_dir)
    projects_dir = resolved_dir / "projects"
    projects_dir.mkdir(parents=True, exist_ok=True)
    path = projects_dir / f"{slug}.toml"

    lines: list[str] = []
    if project_config.kind is not None:
        lines.append(f"kind = {_toml_format_value(project_config.kind)}")
    if project_config.shared_project_config is not None:
        lines.append(f"shared_project_config = {_toml_format_value(project_config.shared_project_config)}")
    if project_config.launch_overlays is not None:
        lines.append(f"launch_overlays = {_toml_format_value(project_config.launch_overlays)}")
    if project_config.apply_scope is not None:
        lines.append(f"apply_scope = {_toml_format_value(project_config.apply_scope)}")

    text = "\n".join(lines) + "\n" if lines else ""
    path.write_text(text, encoding="utf-8")
    return path


# -- sessions.toml --------------------------------------------------------


def _parse_session_entry(entry: dict) -> dict:
    override: dict = {}
    mode = entry.get("mode")
    if isinstance(mode, str):
        override["mode"] = mode
    purpose = entry.get("purpose")
    if isinstance(purpose, str):
        override["purpose"] = purpose
    tags = entry.get("tags")
    if isinstance(tags, list) and all(isinstance(t, str) for t in tags):
        override["tags"] = list(tags)
    return override


def load_session_overrides(config_dir: str | Path | None = None) -> dict[str, dict]:
    """Load ``<config_dir>/sessions.toml``: ``session_id -> {mode?,
    purpose?, tags?}``. A missing file returns ``{}``. Raises
    :class:`ConfigError` for a file that can't be read or isn't valid
    TOML — same posture as :func:`load_config`, so a corrupted overrides
    file is surfaced rather than silently ignored (which would silently
    drop a user's classification corrections).
    """
    resolved_dir = _resolve_config_dir(config_dir)
    path = resolved_dir / "sessions.toml"
    data = _read_toml(path, what="session overrides file")
    if data is None:
        return {}

    sessions_raw = data.get("sessions")
    if not isinstance(sessions_raw, dict):
        return {}

    result: dict[str, dict] = {}
    for session_id, entry in sessions_raw.items():
        if not isinstance(entry, dict):
            continue
        override = _parse_session_entry(entry)
        if override:
            result[str(session_id)] = override
    return result


#: TOML basic-string escapes with their own short form (SEC-P5); every
#: other C0 control character or DEL falls back to \\uXXXX below, so a
#: stray control byte in a value (a pasted purpose, a slug, an exclude
#: pattern) can never produce a literal control character inside the
#: written ``"..."`` string -- which would be invalid TOML and, read
#: back by the capture hook's own ``tomllib.loads``, would silently
#: blank the whole config rather than just that one field.
_TOML_SHORT_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


def _toml_escape_string(value: str) -> str:
    out = []
    for ch in value:
        short = _TOML_SHORT_ESCAPES.get(ch)
        if short is not None:
            out.append(short)
        elif ch == "\x7f" or ord(ch) < 0x20:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return "".join(out)


def _toml_string(value: str) -> str:
    return f'"{_toml_escape_string(value)}"'


def _toml_format_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_format_value(item) for item in value) + "]"
    raise ConfigError(f"cannot serialise value of type {type(value).__name__} to TOML: {value!r}")


def _write_sessions_toml(path: Path, sessions_table: dict[str, dict]) -> None:
    lines: list[str] = []
    for session_id, entry in sessions_table.items():
        if not isinstance(entry, dict) or not entry:
            continue
        lines.append(f"[sessions.{_toml_string(session_id)}]")
        for key, value in entry.items():
            lines.append(f"{key} = {_toml_format_value(value)}")
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines).rstrip("\n") + "\n" if lines else ""
    # SEC-P5: same atomic-write-plus-reparse posture as the config.toml
    # writer below, for the same reason -- a bad escape here would
    # otherwise corrupt sessions.toml, which (unlike config.toml) has no
    # ".new fallback" to catch it.
    _write_atomic(path, text, verify_toml=True)


def save_session_override(
    config_dir: str | Path | None,
    session_id: str,
    mode: str | None = None,
    purpose: str | None = None,
) -> None:
    """Set ``mode``/``purpose`` for ``session_id`` in
    ``<config_dir>/sessions.toml``, rewriting the whole file but
    preserving every other session's entry (and any other key already
    present on this session's own entry, e.g. ``tags``) untouched.
    Passing ``mode=None``/``purpose=None`` (the default) leaves that
    field as it already was — it does not clear it.
    """
    resolved_dir = _resolve_config_dir(config_dir)
    path = resolved_dir / "sessions.toml"

    sessions_table: dict[str, dict] = {}
    data = _read_toml(path, what="session overrides file")
    if data is not None:
        raw_sessions = data.get("sessions")
        if isinstance(raw_sessions, dict):
            for sid, entry in raw_sessions.items():
                if isinstance(entry, dict):
                    sessions_table[str(sid)] = dict(entry)

    entry = dict(sessions_table.get(session_id, {}))
    if mode is not None:
        entry["mode"] = mode
    if purpose is not None:
        entry["purpose"] = purpose
    sessions_table[session_id] = entry

    _write_sessions_toml(path, sessions_table)


# -- config.toml writer (v0.3 ``init``) ------------------------------------
#
# ``tomllib`` has no writer (see module docstring), and unlike
# ``sessions.toml`` (a flat table of per-session tables, handled above by
# ``_write_sessions_toml``), ``config.toml`` is a flat document of
# top-level scalars/lists plus at most one level of nested table
# (``[thresholds]``/``[recache]``) -- so it needs its own small dumper
# rather than reusing ``_write_sessions_toml``'s shape.


def _dump_toml_table(data: dict) -> str:
    """Serialise a dict of TOML-safe values to ``config.toml`` text.
    Top-level scalars/lists are written first, then one ``[section]``
    block per top-level dict value (``thresholds``/``recache``'s shape).
    Raises :class:`ConfigError` for a table nested inside a table (no
    ``config.toml`` field needs that) -- the caller uses this as the
    signal to fall back to a ``.toml.new`` file rather than overwrite the
    real one with a lossy dump.
    """
    top_lines: list[str] = []
    table_blocks: list[str] = []
    for key, value in data.items():
        if isinstance(value, dict):
            block = [f"[{key}]"]
            for sub_key, sub_value in value.items():
                if isinstance(sub_value, dict):
                    raise ConfigError(
                        f"cannot serialise nested table under '{key}.{sub_key}' to TOML "
                        "(only one level of table nesting is supported)"
                    )
                block.append(f"{sub_key} = {_toml_format_value(sub_value)}")
            table_blocks.append("\n".join(block))
        else:
            top_lines.append(f"{key} = {_toml_format_value(value)}")
    parts = [part for part in ("\n".join(top_lines), *table_blocks) if part]
    return ("\n\n".join(parts) + "\n") if parts else ""


def write_config_values(config_dir: str | Path | None, updates: dict) -> Path:
    """Merge ``updates`` into ``<config_dir>/config.toml`` (a top-level
    dict value in ``updates`` is merged key-by-key into the existing
    table of the same name, e.g. updating one ``thresholds`` key leaves
    the others alone; anything else replaces the existing top-level key
    outright). The merged result is validated the same way
    :func:`load_config` validates a file read from disk (via
    :func:`_build_config`) before anything is written, so a bad update
    never corrupts the file. If the merged config's shape can't be
    round-tripped by :func:`_dump_toml_table` (a table nested inside a
    table), the existing ``config.toml`` is left untouched and the full
    merged config is written to ``config.toml.new`` instead, for the
    user to reconcile by hand -- the ".new fallback" this module's docs
    describe. Returns the path actually written.
    """
    resolved_dir = _resolve_config_dir(config_dir)
    # Validate before writing anything -- raises ConfigError on a bad
    # value/shape, same as loading a hand-edited file would.
    merged = _merged_values(resolved_dir, updates)

    resolved_dir.mkdir(parents=True, exist_ok=True)
    path = resolved_dir / "config.toml"
    try:
        text = _dump_toml_table(merged)
    except ConfigError:
        new_path = resolved_dir / "config.toml.new"
        _write_atomic(new_path, _dump_toml_table(_flat_part(merged)), verify_toml=True)
        return new_path
    _write_atomic(path, text, verify_toml=True)
    return path


def check_config_values(config_dir: str | Path | None, updates: dict) -> None:
    """Raise :class:`ConfigError` when :func:`write_config_values` would
    refuse ``updates``; write nothing. ``init`` checks its answers before
    it shows them for review."""
    _merged_values(_resolve_config_dir(config_dir), updates)


def _merged_values(resolved_dir: Path, updates: dict) -> dict:
    """``config.toml`` with ``updates`` merged in (see
    :func:`write_config_values`), validated."""
    path = resolved_dir / "config.toml"
    merged = dict(_read_toml(path, what="config file") or {})
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged_sub = dict(merged[key])
            merged_sub.update(value)
            merged[key] = merged_sub
        else:
            merged[key] = value
    _build_config(merged, path)
    return merged


def _flat_part(data: dict) -> dict:
    """What :func:`_dump_toml_table` can write of ``data``: every
    top-level value and every one-level table (``[savers]``,
    ``[capture]``, ...), less only the tables nested inside a table."""
    return {
        key: ({k: v for k, v in value.items() if not isinstance(v, dict)} if isinstance(value, dict) else value)
        for key, value in data.items()
    }


def _write_atomic(path: Path, text: str, *, verify_toml: bool = False) -> None:
    """Write ``text`` to ``path`` through a temporary file and a rename,
    so a reader (the capture hook, say) never sees half a file.

    ``verify_toml`` (SEC-P5) re-parses ``text`` with ``tomllib`` first and
    raises :class:`ConfigError` without touching ``path`` at all if it
    doesn't come back as valid TOML -- a belt-and-braces check that a
    writer bug (a value ``_toml_format_value`` didn't escape correctly,
    say) can never replace a good ``config.toml`` with one that Token
    Lens, or the capture hook's own ``tomllib.loads``, can't read back.
    """
    if verify_toml:
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"internal error: generated TOML for {path} does not parse back: {exc}") from exc
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


# -- [capture] writer --------------------------------------------------------


def _capture_table(capture: CaptureConfig) -> dict:
    return {
        "level": capture.level,
        "metrics": list(capture.metrics),
        "sample": capture.sample,
        "until": capture.until,
        "projects": list(capture.projects),
        "feedback": list(capture.feedback),
        "coaching": list(capture.coaching),
        "tagger": capture.tagger,
        "enabled_at": capture.enabled_at,
    }


def _in_catalogue_order(chosen: list[str], known: tuple[str, ...]) -> list[str]:
    """``chosen`` with known ids in catalogue order first; unknown ids are
    kept, last, for validation to name."""
    wanted = set(chosen)
    return [i for i in known if i in wanted] + [i for i in dict.fromkeys(chosen) if i not in known]


#: What turning the ``/cg-feedback`` survey on turns on (``capture
#: feedback on``, ``init``), and what turning it off turns off: the skill
#: and the reminders to run it. The dashboard rating stays as set.
FEEDBACK_ON = ("feedback_skill", "feedback_note")
FEEDBACK_OFF = ("feedback_skill", "feedback_note", "feedback_reminder")


def feedback_ids(current: list[str], on: bool) -> list[str]:
    """``[capture] feedback`` with the ``/cg-feedback`` skill and its
    notes switched on or off (:data:`FEEDBACK_ON`/:data:`FEEDBACK_OFF`)."""
    if on:
        return list(current) + [i for i in FEEDBACK_ON if i not in current]
    return [i for i in current if i not in FEEDBACK_OFF]


def set_capture(
    config_dir: str | Path | None,
    *,
    level: str | None = None,
    metrics: list[str] | None = None,
    sample: int | None = None,
    until: str | None = None,
    projects: list[str] | None = None,
    feedback: list[str] | None = None,
    coaching: list[str] | None = None,
    tagger: str | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
) -> CaptureConfig:
    """Change ``config.toml``'s ``[capture]`` table and return the result.

    Only the arguments given change. ``level`` picks a preset (clearing
    ``metrics``); ``metrics`` picks metrics one by one, and the level
    becomes the preset they match, else ``"custom"``. Switching from off
    to on stamps ``enabled_at``; switching off clears it and ``until``.
    A switch from off to on that leaves ``until`` unsaid (``None``) gets
    :data:`~claudeglass.capture_catalogue.DEFAULT_CAPTURE_TIMEBOX_DAYS`
    days by default (CAP-8), so capture can't run forever unnoticed --
    pass ``until=""`` for a deliberate "no limit" instead. A switch into
    Deep (by ``level`` or by ``metrics`` that add up to it) also turns on
    :data:`~claudeglass.capture_catalogue.DEEP_FEEDBACK_IDS`, unless
    the same call passes ``feedback``; leaving Deep keeps them. The skill
    file itself is the caller's to write. The new table
    is validated before anything is written, the write is
    atomic, and every change is appended to ``capture-log.jsonl``. Raises
    :class:`ConfigError` for a bad value, or when ``config.toml`` can't be
    rewritten in place (the change then sits in ``config.toml.new``).
    With ``dry_run`` it validates and returns the result without writing.
    """
    resolved_dir = _resolve_config_dir(config_dir)
    path = resolved_dir / "config.toml"
    current = _build_config(_read_toml(path, what="config file") or {}, path).capture
    table = _capture_table(current)
    if level is not None:
        table["level"] = level
        table["metrics"] = []
    if metrics is not None:
        unknown = [m for m in metrics if m not in capture_catalogue.LEVEL_METRIC_IDS]
        if unknown:
            raise ConfigError(
                f"unknown capture metric {', '.join(repr(u) for u in unknown)}; "
                f"known: {', '.join(capture_catalogue.LEVEL_METRIC_IDS)}"
            )
        chosen = list(capture_catalogue.with_requirements(metrics))
        table["level"] = capture_catalogue.level_of(chosen)
        table["metrics"] = chosen if table["level"] == capture_catalogue.CUSTOM_LEVEL else []
    if sample is not None:
        table["sample"] = sample
    if until is not None:
        table["until"] = until
    if projects is not None:
        table["projects"] = list(projects)
    if feedback is not None:
        table["feedback"] = _in_catalogue_order(feedback, capture_catalogue.FEEDBACK_IDS)
    elif table["level"] == "deep" and current.level != "deep":
        table["feedback"] = _in_catalogue_order(
            table["feedback"] + list(capture_catalogue.DEEP_FEEDBACK_IDS), capture_catalogue.FEEDBACK_IDS
        )
    if coaching is not None:
        table["coaching"] = _in_catalogue_order(coaching, capture_catalogue.COACHING_IDS)
    if tagger is not None:
        table["tagger"] = tagger
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    stamp = moment.isoformat(timespec="seconds")
    if table["level"] == "off":
        table["enabled_at"] = ""
        table["until"] = ""
    elif not current.is_on:
        table["enabled_at"] = stamp
        # CAP-8: a fresh "off" -> "on" switch gets a default time-box when
        # nothing says otherwise, so capture can't run forever unnoticed
        # just because nobody set one. An explicit --until/--for/"" (a
        # deliberate "no limit") already set table["until"] above, and an
        # *existing* until can't reach this branch at all -- it's cleared
        # to "" whenever level is "off", which is the only way to get here
        # -- so this is exactly the "skipped when --capture-no-limit/--for/
        # --until is given or an until exists" case the audit calls for.
        # Every path that can turn capture on (non-interactive init,
        # 'capture on'/'level', POST /api/capture) funnels through this one
        # place, so none of them need to duplicate the default themselves.
        if until is None:
            table["until"] = (
                moment + timedelta(days=capture_catalogue.DEFAULT_CAPTURE_TIMEBOX_DAYS)
            ).isoformat(timespec="seconds")

    if table == _capture_table(current):
        return current
    if dry_run:
        return _build_capture_config(table, path)
    written = write_config_values(resolved_dir, {"capture": table})
    if written.name != "config.toml":
        raise ConfigError(
            f"could not update {path} in place; the change was written to {written} for you to merge by hand"
        )
    updated = _build_capture_config(table, path)
    _append_capture_log(resolved_dir, stamp, current, updated)
    return updated


def _append_capture_log(config_dir: Path, stamp: str, before: CaptureConfig, after: CaptureConfig) -> None:
    old, new = asdict(before), asdict(after)
    changed = {key: {"from": old[key], "to": new[key]} for key in new if old[key] != new[key] and key != "enabled_at"}
    record = {"ts": stamp, "level": after.level, "changed": changed}
    with open(config_dir / CAPTURE_LOG_NAME, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def load_capture_log(config_dir: str | Path | None = None) -> list[dict]:
    """Every ``[capture]`` change recorded in ``capture-log.jsonl``, oldest
    first; lines that aren't a change record are skipped."""
    path = _resolve_config_dir(config_dir) / CAPTURE_LOG_NAME
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and isinstance(record.get("ts"), str):
            records.append(record)
    return records


def prune_capture_log(
    config_dir: str | Path | None = None,
    retention_days: int = SIGNAL_RETENTION_DEFAULT_DAYS,
    now: datetime | None = None,
) -> int:
    """Drop every ``capture-log.jsonl`` record older than
    ``retention_days`` (G7: this file was never pruned at all -- the
    same retention idea :func:`~claudeglass.signals.prune` already
    applies to capture signal files). A record whose line can't be
    parsed back as ``{"ts": <str>, ...}`` (never written by
    :func:`_append_capture_log` itself, but see :func:`load_capture_log`'s
    own tolerant-read posture) is dropped along with the rest -- a prune
    pass is also a chance to repair the file, not just trim it, and such
    a line was already invisible to :func:`load_capture_log`'s own reader
    either way. Rewritten atomically via :func:`_write_atomic`. Returns
    how many lines were removed; a missing file, or one with nothing to
    remove, is a no-op returning 0.
    """
    path = _resolve_config_dir(config_dir) / CAPTURE_LOG_NAME
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    if not lines:
        return 0

    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=retention_days)
    kept = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or not isinstance(record.get("ts"), str):
            continue
        try:
            ts = datetime.fromisoformat(record["ts"])
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts >= cutoff:
            kept.append(line)

    removed = len(lines) - len(kept)
    if removed == 0:
        return 0
    text = "".join(f"{line}\n" for line in kept)
    _write_atomic(path, text)
    return removed


def append_prediction_log(
    config_dir: str | Path,
    *,
    source: str,
    measure_key: str,
    agent: str | None,
    predicted_usd: float | None,
    predicted_pct: float | None,
    fidelity: str,
    now: datetime | None = None,
) -> str:
    """Log one whatif estimate worth checking against what actually
    happened (EST-P5), so it can later be matched to a real change point
    and judged (``backtest.py``). ``source``/``measure_key``/``fidelity``
    are short enum-like strings (never free text -- see
    ``service/schema.py``'s "Version 7" paragraph); ``agent`` is an agent
    type or ``None`` for a main-session/global change. Returns the
    prediction's own generated id (a stable key so re-ingesting the same
    log line twice, e.g. on a later watcher tick, is a no-op)."""
    resolved_dir = _resolve_config_dir(config_dir)
    resolved_dir.mkdir(parents=True, exist_ok=True)
    prediction_id = uuid.uuid4().hex[:16]
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")
    record = {
        "id": prediction_id,
        "ts": stamp,
        "source": source,
        "measure_key": measure_key,
        "agent": agent,
        "predicted_usd": predicted_usd,
        "predicted_pct": predicted_pct,
        "fidelity": fidelity,
    }
    with open(resolved_dir / PREDICTION_LOG_NAME, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    return prediction_id


def load_prediction_log(config_dir: str | Path | None = None) -> list[dict]:
    """Every prediction recorded in ``prediction-log.jsonl``, oldest
    first; lines that aren't a prediction record are skipped."""
    path = _resolve_config_dir(config_dir) / PREDICTION_LOG_NAME
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and isinstance(record.get("id"), str) and isinstance(record.get("ts"), str):
            records.append(record)
    return records


__all__ = [
    "CLAUDEGLASS_DIRNAME",
    "CAPTURE_LOG_NAME",
    "PREDICTION_LOG_NAME",
    "CAPTURE_SAMPLES",
    "SIGNAL_RETENTION_DEFAULT_DAYS",
    "CaptureConfig",
    "ConfigError",
    "Config",
    "ProjectConfig",
    "load_config",
    "saved_billing",
    "saved_pricing_path",
    "check_config_values",
    "FEEDBACK_ON",
    "FEEDBACK_OFF",
    "feedback_ids",
    "load_project_configs",
    "save_project_config",
    "write_config_values",
    "set_capture",
    "load_capture_log",
    "prune_capture_log",
    "append_prediction_log",
    "load_prediction_log",
    "load_session_overrides",
    "save_session_override",
]

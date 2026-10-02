#!/usr/bin/env python3
"""SessionStart config-capture hook for claudeglass (WP7).

Standalone stdlib script. It intentionally imports nothing from the
``claudeglass`` package (or any third-party library) so it keeps
working when copied on its own onto a locked-down machine that only has a
plain Python 3.11+ interpreter (see the plan's "Running on other people's
machines" section and Appendix A6). The dependency runs the other way: the
``snapshot-config`` CLI subcommand loads *this* file by path.

Contract (Appendix A6, refined by the WP7 brief):

- Reads the SessionStart hook JSON from stdin: ``session_id``, ``cwd``,
  ``transcript_path``, ``source``. Stdin may be empty or malformed; both are
  tolerated (treated as ``{}``). The project read is ``CLAUDE_PROJECT_DIR``
  when Claude Code sets it, not ``cwd`` (see :func:`_project_dir`).
- Resolves the config (claudeglass) directory: ``--config-dir`` wins and
  IS that directory directly; else ``<CLAUDE_CONFIG_DIR or ~/.claude>/
  claudeglass`` (see :func:`resolve_config_dir`; ``settings.json``/
  ``agents/`` are read from its parent, the ``~/.claude`` root).
- Writes ``<config_dir>/snapshots/<UTC compact ts>.json`` (e.g.
  ``20260918T191200Z.json``).
- Never lets a broken Python block a session: always exits 0. On success it
  prints nothing; on any exception it writes one line to stderr and still
  exits 0.
- Idempotent: ``--min-interval SECONDS`` (default 300) skips the write when
  the newest existing snapshot is both younger than that interval and has
  the same ``content_hash`` as the snapshot that would be written now.

Redaction rule applied throughout (settings and agent frontmatter alike):
a value is kept as-is only if its key is in a small safe allowlist, or the
value itself is a ``bool``/``int`` (harmless scalars — booleans and small
integers reveal a toggle or a limit, never content). Every other value is
replaced by a shape-only marker: ``dict(n)``, ``list(n)`` or ``str(len)``.
This is deliberately generic (not just settings.json) so a new, unknown key
in a future Claude Code version degrades to a safe shape marker instead of
leaking its value.

Managed (enterprise-policy) settings (fix 7, plan "Enterprise use" section):
the platform-wide ``managed-settings.json`` a system administrator can drop
outside any user's control (macOS
``/Library/Application Support/ClaudeCode/managed-settings.json``, Linux
``/etc/claude-code/managed-settings.json``, Windows
``%ProgramFiles%\\ClaudeCode\\managed-settings.json`` — see
:func:`default_managed_settings_dir`; the legacy ``%ProgramData%`` path is
no longer read by Claude Code) is read the same way as
user/project settings.json and redacted with the exact same
``redact_settings`` rule into ``snapshot["managed_settings"]``. Its raw
top-level key names (never values) are additionally recorded verbatim into
``snapshot["managed_keys"]``, since the plan calls for reports to be able to
say "this lever is managed by policy" for any recommendation whose key
appears there — the key name alone ("permissions", "model", ...) carries no
content to redact. ``--managed-path`` overrides the platform default, for
tests and for the rare machine whose policy file lives somewhere else.

Schema 2 (owner request, plan "Configuration layers" section, additive over
schema 1 — :func:`~claudeglass.snapshots.load_snapshots` still loads
schema-1 files unchanged) adds, on top of every schema-1 field above:

- ``project_slug`` — the same slug algorithm as
  ``discovery.slug_for`` (duplicated here rather than imported, since this
  script stays standalone stdlib), used to locate this project's auto-memory
  directory and as a human-readable (not path-shaped) project identifier.
- ``settings_layers`` — one entry per settings layer (``managed``,
  ``project_local`` i.e. ``.claude/settings.local.json``, ``project_shared``
  i.e. ``.claude/settings.json``, ``user`` i.e. ``~/.claude/settings.json``),
  precedence high to low in that order, each with ``present``,
  ``source_path_hash``, ``content_hash``, the existing allowlist-redacted
  settings, and purpose-built summaries a report needs directly:
  ``env_names`` (the settings ``env`` block, names only), ``permissions``
  (allow/deny/ask *counts* plus ``default_mode``), ``hooks`` (event name ->
  entry count), ``enabled_plugins``, and the named safe scalars
  (``model``, ``effort_level``, ``max_effort_level``, ``model_settings``,
  ``always_thinking_enabled``, ``auto_compact_window``,
  ``prompt_cache_ttl``, ``subagent_prompt_cache_ttl``,
  ``cleanup_period_days``, ``output_style``, ``statusline_present``).
  PROF-03: ``max_effort_level`` (a hard cap) and ``model_settings`` (each
  named model id's own ``effortLevel``, when set) both rank above the
  plain ``effortLevel`` scalar for the model they apply to -- a
  recommendation that ticks ``effortLevel`` for a model either of these
  names should say so, since applying the change to settings.json alone
  won't take effect there (see ``profiles.goals``).
- ``effective`` / ``effective_provenance`` — every :data:`SETTINGS_SUMMARY_KEYS`
  key's value (the same allowlist/redaction :func:`redact_settings_value`
  already applies — ``statusLine``, ``modelPricing`` and, since COV-09,
  ``attribution`` included, each reduced to their own safe summary shape
  rather than a raw value; ``includeCoAuthoredBy`` — the Boolean
  ``attribution`` replaces, docs/en/settings-reference.md — rides the
  plain scalar allowlist) merged across the four settings layers in
  precedence order, with ``effective_provenance[key]`` naming which layer
  supplied it.
- ``effective_env_names`` / ``effective_env_provenance``,
  ``effective_permissions``, ``effective_hooks``,
  ``effective_enabled_plugins``, ``effective_mcpjson_servers`` (COV-03,
  fixing D6) — the ``env``/``permissions``/``hooks``/``enabledPlugins``/
  ``enabledMcpjsonServers``/``disabledMcpjsonServers`` settings keys,
  deep-merged across all four layers with the merge rule each actually
  has (per-name precedence for ``env`` and ``enabledPlugins``, additive
  union for ``permissions``/``hooks``/the mcpjson lists) rather than the
  single-layer read schema 1's ``mcp_servers``/``enabled_plugins`` and
  each layer's own ``permissions``/``hooks``/``env_names`` give alone --
  see :func:`build_effective_env_names` and its neighbours for the exact
  doc citation behind each rule.
- ``effective_agents`` — every agent name -> ``{source, experimental_cache_ttl,
  model, effort, max_turns}``, derived from ``agents`` (which schema 2 also
  extends with a ``source`` ("user"/"project") and, on a name clash, a
  ``shadowed_by_project`` flag on the project entry that won the merge).
- ``claude_json`` — a redacted read of ``~/.claude.json`` (the CLI's own
  per-machine state file, not a Claude Code settings file): the entry
  matching this session's project directory, found by comparing
  ``os.path.normcase(os.path.realpath(...))`` on both sides so the same
  physical directory is recognised under any of the drive-letter-case /
  slash-style key spellings ``~/.claude.json`` is observed to use — the raw
  matching key itself is never stored. Records MCP server/plugin *names*,
  small counts, and (if present) the numeric ``last*`` per-project session
  totals — a cross-check against this tool's own accounting for the same
  session, never message text.
- ``content_layers`` — sizes, counts and names only (never content) for the
  CLAUDE.md family (user, project root/local, and a bounded walk of nested
  ``CLAUDE.md`` files, plus each file's ``@import`` reference *count* —
  COV-10), ``.claude/rules/*.md``, ``.claude/commands/**/*.md``, project
  and user skills (``.claude/skills/*/SKILL.md`` — names + bytes, plus
  each skill's closed-vocabulary frontmatter summary: ``model``,
  ``effort``, ``context``, ``paths_count`` — COV-10/PROF-09), a rollup of
  the ``agents`` dict's own source/shadow flags (agent directories are now
  scanned recursively — COV-10), the project's ``.mcp.json`` server names,
  ``managed-mcp.json`` presence and server names (now looked up in the
  system managed-settings directory, not ``claude_root`` — COV-05b),
  installed plugins' own skill names/agent counts (``plugin_content`` —
  COV-10, best-effort default-layout scan), output style names, this
  project's auto-memory byte/file count, installed plugin names and
  marketplace count, whether ``CLAUDE_CONFIG_DIR`` is set, and (PROF-03)
  whether ``CLAUDE_CODE_EFFORT_LEVEL`` is set -- never its value, since
  the env var beats every settings.json effort lever including
  ``--effort``'s own saved form, so a recommendation only needs to know
  it's pinned at all, not to what.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

#: Settings keys whose values are always safe to record verbatim: they are
#: either short enum-like strings/enums or the value type check below
#: (bool/int) already keeps them. Kept as its own list per the WP7 brief
#: rather than folded into "any scalar", because a handful of *string*
#: settings keys are explicitly named as safe and must be exempted from the
#: generic ``str(len)`` redaction that other string settings get.
SAFE_SETTINGS_KEYS = frozenset(
    {
        "model",
        "effortLevel",
        "outputStyle",
        "autoCompactWindow",
        "autoCompactEnabled",
        "promptCacheTtl",
        "subagentPromptCacheTtl",
        "cleanupPeriodDays",
        "desktopSessionCleanupPeriodDays",
        "autoUpdatesChannel",
        "alwaysThinkingEnabled",
        # COV-09: a plain Boolean (docs/en/settings-reference.md), so the
        # generic bool passthrough in _redact_generic already kept it
        # verbatim even before this -- adding it here only pulls it into
        # SETTINGS_SUMMARY_KEYS so effective/effective_provenance (and the
        # "effective-config" report table) carry it too.
        "includeCoAuthoredBy",
        # PROF-03: a hard cap on effort level, same short enum-like string
        # posture as effortLevel itself -- see the module docstring's
        # "Configuration layers" note on why a ticked effortLevel can be
        # overridden.
        "maxEffortLevel",
        # PROF-08: a plain Boolean (docs/en/settings-reference.md), same
        # posture as includeCoAuthoredBy/autoCompactEnabled above -- lets
        # a profile's "turn fastMode off" candidate (schema.py's
        # SETTINGS_ALLOWLIST) skip itself when it's already off.
        "fastMode",
    }
)

#: Settings keys that need their own summary shape rather than either a
#: verbatim pass-through or the fully-generic ``dict(n)``/``str(len)``
#: marker: ``statusLine`` reduces to a present/absent boolean (a report only
#: ever needs "is a statusline configured", never the command it runs), and
#: ``modelPricing`` reduces to a present flag plus the model ids it overrides
#: (never the overridden numbers, which are exactly the sort of "silently
#: adopt whatever the file says" figure the report's own pricing.toml
#: exists to keep user-editable and out of code). Handled in
#: :func:`redact_settings_value` ahead of the plain allowlist check.
_STATUS_LINE_KEY = "statusLine"
_MODEL_PRICING_KEY = "modelPricing"
#: COV-09: ``attribution`` (docs/en/settings-reference.md) replaces the
#: deprecated ``includeCoAuthoredBy`` and lets a layer customise the git
#: commit trailer / PR text Claude Code adds -- an object with ``commit``/
#: ``pr`` free-text strings and a ``sessionUrl`` Boolean, so it needs its
#: own summary shape (below) rather than either the verbatim allowlist
#: (the strings could be anything) or the generic ``dict(n)`` marker
#: (which would hide even the safe-to-keep ``sessionUrl`` Boolean).
_ATTRIBUTION_KEY = "attribution"
#: PROF-03: ``modelSettings`` gives per-model overrides (an effort level
#: that beats the top-level ``effortLevel`` scalar for that model alone --
#: see ``docs/en/settings-reference.md``), so like ``statusLine``/
#: ``modelPricing``/``attribution`` it needs its own summary shape rather
#: than either the verbatim allowlist (arbitrary model ids as keys) or the
#: generic ``dict(n)`` marker (which would hide the one sub-value a
#: recommendation actually needs to check: whether this model's effort is
#: pinned).
_MODEL_SETTINGS_KEY = "modelSettings"

#: Every settings key with special handling, allowlisted or summarised
#: (never the generic ``dict(n)``/``str(len)`` shape marker) -- used to
#: build ``effective``/``effective_provenance`` (schema 2), which merges
#: exactly these keys across the settings layers.
SETTINGS_SUMMARY_KEYS = SAFE_SETTINGS_KEYS | {
    _STATUS_LINE_KEY, _MODEL_PRICING_KEY, _ATTRIBUTION_KEY, _MODEL_SETTINGS_KEY,
}

#: Agent frontmatter keys kept verbatim (everything under "experimental."
#: is also kept — see ``redact_agent_frontmatter``). ``description`` is
#: handled separately (always ``str(len)``), never in this set.
AGENT_KEEP_KEYS = frozenset(
    {
        "name",
        "model",
        "effort",
        "maxTurns",
        "tools",
        "disallowedTools",
        "permissionMode",
        "memory",
        "omitClaudeMd",
        "skills",
        "isolation",
        "background",
    }
)

#: Schema 2 is additive over schema 1 (every schema-1 field keeps the same
#: name and shape); see the module docstring's "Schema 2" section for the
#: new top-level keys. ``snapshots.load_snapshots`` loads either.
SCHEMA_VERSION = 2

_TS_FORMAT = "%Y%m%dT%H%M%SZ"

#: discovery.slug_for's own algorithm, duplicated (not imported -- this
#: script stays standalone stdlib, see the module docstring).
_SLUG_NON_ALNUM_RE = re.compile(r"[^A-Za-z0-9]")
_SLUG_MAX_CHARS = 200
_SLUG_HASH_HEX_CHARS = 8

#: Directory names skipped by the bounded nested-CLAUDE.md walk (never
#: worth descending into: VCS metadata, dependency/venv trees, build output).
_CLAUDE_MD_WALK_SKIP_DIRS = frozenset(
    {
        ".git", "node_modules", ".venv", "bin", "obj",
        # Fix #18: common build/vendor directory names the original list
        # missed -- an unpruned one of these can hold tens of thousands
        # of files.
        "dist", "build", "target", "__pycache__", ".next", "vendor", "Pods", "packages",
    }
)
_CLAUDE_MD_WALK_MAX_DEPTH = 6
_CLAUDE_MD_WALK_MAX_DIRS = 5000
#: Fix #18: wall-clock budget for the whole walk, on top of the existing
#: depth/breadth bounds -- those bound the number of *directories*
#: visited, not the per-directory cost (iterdir + a stat per entry), so a
#: single huge directory could still make a SessionStart hang.
_CLAUDE_MD_WALK_MAX_SECONDS = 1.0

#: Env var name prefixes captured (names only, values never recorded) --
#: widened from ANTHROPIC_*/CLAUDE_* to also cover OpenTelemetry config
#: (plan "Enterprise use" section's OTel-compatible export already reads
#: OTEL_* by name; the snapshot recording OTEL_* names lets a report note
#: telemetry is configured at all).
_ENV_NAME_PREFIXES = ("ANTHROPIC_", "CLAUDE_", "OTEL_")

#: Individual env var names captured that don't share one of the prefixes
#: above (documented Claude Code levers with irregular names). COV-09 adds
#: the five ``DISABLE_PROMPT_CACHING*`` levers (docs/en/env-vars.md: one
#: global switch plus a per-family override for Sonnet/Opus/Haiku/Fable)
#: and ``ENABLE_TOOL_SEARCH`` (same doc: controls MCP tool search) so a
#: recommend.py rule can see whether either is set -- both already
#: names-only, matching every other lever in this set.
_ENV_EXTRA_NAMES = frozenset(
    {
        "MAX_THINKING_TOKENS",
        "DISABLE_NON_ESSENTIAL_MODEL_CALLS",
        "MAX_MCP_OUTPUT_TOKENS",
        "BASH_MAX_OUTPUT_LENGTH",
        "DISABLE_PROMPT_CACHING",
        "DISABLE_PROMPT_CACHING_SONNET",
        "DISABLE_PROMPT_CACHING_OPUS",
        "DISABLE_PROMPT_CACHING_HAIKU",
        "DISABLE_PROMPT_CACHING_FABLE",
        "ENABLE_TOOL_SEARCH",
    }
)

#: Of the names above (plus CLAUDE_AUTOCOMPACT_PCT_OVERRIDE and
#: CLAUDE_CODE_MAX_OUTPUT_TOKENS, both already covered by the CLAUDE_
#: prefix), these are numeric *caps* rather than secrets or content, so
#: the integer value itself is recorded alongside the name -- everything
#: else stays names-only. CLAUDE_CODE_MAX_OUTPUT_TOKENS added for COV-09:
#: docs/en/env-vars.md notes raising it "reduces the effective context
#: window available before auto-compaction triggers", which a rule needs
#: the actual value to reason about, not just presence.
#: CLAUDE_CODE_AUTO_COMPACT_WINDOW sets the auto-compact window in tokens
#: and overrides the autoCompactWindow setting (same doc), so a report
#: needs its value to know the window a session ran at.
_ENV_NUMERIC_CAP_NAMES = frozenset(
    {
        "MAX_THINKING_TOKENS",
        "MAX_MCP_OUTPUT_TOKENS",
        "BASH_MAX_OUTPUT_LENGTH",
        "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE",
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
    }
)

#: Settings layers, precedence high to low (plan "Configuration layers"
#: section). Matches the ``scope`` vocabulary CLAUDE.md/A6 use elsewhere
#: (user/project-local/repo/managed) with "repo" spelled ``project_shared``
#: here since that's the file's own name (``settings.json``, checked in and
#: shared with colleagues) rather than the apply-time scope label.
SETTINGS_LAYER_ORDER: tuple[str, ...] = (
    "managed",
    "project_local",
    "project_shared",
    "user",
)


# -- config dir / small file reads --------------------------------------


def resolve_config_dir(cli_arg: str | None = None) -> Path:
    """``--config-dir`` wins -- and IS the claudeglass directory itself,
    directly containing ``snapshots/``, ``hooks/`` and ``active-profile``
    -- else ``<CLAUDE_CONFIG_DIR or ~/.claude>/claudeglass``.

    Fix config-dir: this used to treat an explicit ``--config-dir`` as
    the ``~/.claude`` root itself (appending ``claudeglass/`` for every
    subpath internally), which disagreed with ``cli.py``'s
    ``_resolve_config_dir`` and ``snapshots.load_snapshots`` -- both of
    which have always treated an explicit ``--config-dir`` as the
    claudeglass directory directly. A user pointing the same
    ``--config-dir`` value at both this hook and the CLI got snapshots
    written one directory level away from where the CLI looked for
    them. Claude Code's own folder (``settings.json``/``agents/``) is
    :func:`resolve_claude_root`, never this path's parent.
    """
    if cli_arg:
        return Path(cli_arg)
    return resolve_claude_root() / "claudeglass"


def resolve_claude_root() -> Path:
    """Claude Code's own folder, holding ``settings.json``, ``agents/``
    and ``CLAUDE.md``: ``$CLAUDE_CONFIG_DIR``, else ``~/.claude``. A
    copy of ``discovery.claude_root`` (this script runs on its own,
    without the package). Not derived from ``--config-dir``: ``init``
    adds that flag to the hook command when this tool's data folder is
    somewhere else, and its parent is then unrelated to Claude Code.
    """
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env) if env else (Path.home() / ".claude")


def default_managed_settings_dir() -> Path:
    """The platform's system managed-settings directory (fix 7, COV-05):
    holds both ``managed-settings.json`` and ``managed-mcp.json`` (docs/en/
    managed-settings.md: "managed-settings.json, an optional
    managed-settings.d/ directory, and managed-mcp.json in the system
    directory: /Library/Application Support/ClaudeCode/ on macOS,
    /etc/claude-code/ on Linux and WSL, and C:\\Program Files\\ClaudeCode\\
    on Windows"). Windows previously used %ProgramData%\\ClaudeCode, the
    legacy path the same doc says Claude Code no longer reads ("Claude
    Code doesn't read the legacy Windows path
    C:\\ProgramData\\ClaudeCode\\managed-settings.json") — a doc/code
    conflict fixed here (COV-05). ``ProgramFiles`` stays env-overridable
    (mirroring the old ``ProgramData`` pattern) so tests don't need a real
    Program Files directory.
    """
    if sys.platform == "win32":
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        return Path(program_files) / "ClaudeCode"
    if sys.platform == "darwin":
        return Path("/Library/Application Support/ClaudeCode")
    return Path("/etc/claude-code")


def default_managed_settings_path() -> Path:
    """The platform's system-wide ``managed-settings.json`` path (fix 7).
    This file is written by IT/policy tooling, not by the current user, so
    unlike ``resolve_config_dir`` there is no per-user env var to prefer —
    only ``--managed-path`` (handled by the caller) overrides it.
    """
    return default_managed_settings_dir() / "managed-settings.json"


def resolve_dot_claude_json_path() -> Path:
    """``~/.claude.json`` (the CLI's own per-machine state file — MCP
    servers, per-project trust, OAuth account), relocated by
    ``CLAUDE_CONFIG_DIR`` exactly like ``~/.claude`` itself (COV-05,
    D8): docs/en/devcontainer.md: "It stores your OAuth account, personal
    MCP servers, and per-project trust in ~/.claude.json, a separate file
    outside that directory... Mount a named volume at ~/.claude and set
    CLAUDE_CONFIG_DIR to the same path so Claude Code writes .claude.json
    inside the volume." So the file sits *beside* the config dir, not
    inside it: ``<CLAUDE_CONFIG_DIR>/.claude.json`` when set, else
    ``~/.claude.json``.
    """
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(env) if env else Path.home()
    return base / ".claude.json"


def _read_json_dict(path: Path) -> dict | None:
    """Read ``path`` as a JSON object. Returns ``None`` on any I/O error,
    parse error, or if the top-level value isn't an object — the hook must
    tolerate a missing or malformed settings file, never crash on one.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _read_active_profile(config_dir: Path) -> str | None:
    path = config_dir / "active-profile"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    text = text.strip()
    return text or None


# -- redaction ------------------------------------------------------------


def _redact_generic(value):
    """Shape-only redaction used for any key with no special handling:
    bool/int kept (a toggle or a limit, not content), dict/list/str reduced
    to a length marker, everything else (``None``, float) kept as-is.
    """
    if isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, dict):
        return f"dict({len(value)})"
    if isinstance(value, list):
        return f"list({len(value)})"
    if isinstance(value, str):
        return f"str({len(value)})"
    return value


def _redact_model_pricing(value) -> dict:
    """``modelPricing`` reduces to whether it's set at all and which model
    ids it overrides -- never the overridden numbers themselves (see
    ``_MODEL_PRICING_KEY``'s note above ``SETTINGS_SUMMARY_KEYS``).
    """
    if not isinstance(value, dict) or not value:
        return {"present": False, "model_ids": []}
    return {"present": True, "model_ids": sorted(str(k) for k in value)}


def _redact_attribution(value) -> dict:
    """``attribution`` (COV-09) reduces to whether ``commit``/``pr`` are
    customised at all -- never the free-text trailer/PR body itself --
    plus ``sessionUrl`` verbatim, since that sub-key is a plain Boolean
    per docs/en/settings-reference.md, not free text.
    """
    if not isinstance(value, dict):
        return {"commit_set": False, "pr_set": False, "session_url": None}
    commit = value.get("commit")
    pr = value.get("pr")
    session_url = value.get("sessionUrl")
    return {
        "commit_set": isinstance(commit, str) and bool(commit),
        "pr_set": isinstance(pr, str) and bool(pr),
        "session_url": session_url if isinstance(session_url, bool) else None,
    }


def _redact_model_settings(value) -> dict:
    """``modelSettings``: for each model id it names, only its
    ``effortLevel`` override (a short safe enum-like string, same posture
    as the top-level ``effortLevel`` scalar) -- never any other sub-key a
    future Claude Code version might add per model, which is left out
    entirely rather than guessed at (same minimal posture as
    ``_redact_attribution``). Model ids are clipped like any other
    user-supplied name (``_clip_name``), not treated as free text."""
    if not isinstance(value, dict):
        return {}
    out: dict = {}
    for model_id, entry in value.items():
        if not isinstance(entry, dict):
            continue
        effort = entry.get("effortLevel")
        out[_clip_name(model_id)] = {"effortLevel": effort if isinstance(effort, str) else None}
    return out


def redact_settings_value(key: str, value):
    if key == _STATUS_LINE_KEY:
        return bool(value)
    if key == _MODEL_PRICING_KEY:
        return _redact_model_pricing(value)
    if key == _ATTRIBUTION_KEY:
        return _redact_attribution(value)
    if key == _MODEL_SETTINGS_KEY:
        return _redact_model_settings(value)
    if key in SAFE_SETTINGS_KEYS:
        return value
    return _redact_generic(value)


def redact_settings(settings: dict) -> dict:
    """Redact a whole settings object (user settings.json or a project
    settings*.json), key by key, using the same allowlist for both.
    """
    if not isinstance(settings, dict):
        return {}
    return {key: redact_settings_value(key, value) for key, value in settings.items()}


def redact_agent_frontmatter(parsed: dict) -> dict:
    """Redact one agent's already-flattened frontmatter dict (see
    ``parse_frontmatter``). ``description`` always becomes ``str(len)``;
    the keep-list (and anything under ``experimental.``) is kept verbatim;
    everything else falls back to the generic shape-only redaction so an
    unrecognised frontmatter field never leaks its value.
    """
    out: dict = {}
    for key, value in parsed.items():
        if key == "description":
            out[key] = f"str({len(value)})" if isinstance(value, str) else "str(0)"
            continue
        if key in AGENT_KEEP_KEYS or key.startswith("experimental."):
            # Fix #5: AGENT_KEEP_KEYS entries (e.g. "tools", "name") are
            # user-authored names/lists of names, not the shape-neutral
            # scalars the allowlist elsewhere is built for -- clip/mask
            # each one the same way every other recorded name now is,
            # rather than keeping it fully verbatim and uncapped.
            if isinstance(value, str):
                out[key] = _clip_name(value)
            elif isinstance(value, list):
                out[key] = [_clip_name(item) for item in value]
            else:
                out[key] = value
            continue
        out[key] = _redact_generic(value)
    return out


# -- tiny YAML-ish frontmatter parser (no PyYAML) --------------------------


def _parse_scalar(raw: str):
    """Parse one YAML-ish scalar: quoted strings, ``[a, b]`` inline lists,
    booleans, null, int, float, else the bare string. Good enough for the
    handful of shapes Claude Code agent frontmatter actually uses — this is
    not a general YAML parser.
    """
    raw = raw.strip()
    if raw == "":
        return None
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if not inner:
            return []
        return [item.strip().strip("'\"") for item in inner.split(",")]
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    low = raw.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("null", "~"):
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def parse_frontmatter(text: str) -> dict:
    """Parse the ``---``-delimited frontmatter block at the top of an agent
    or skill markdown file into a flat dict. Top-level ``key: value``
    scalars are kept as-is; a top-level ``key:`` with no value starts
    either a nested map, whose two-space-indented ``child: value`` lines
    are flattened to ``key.child`` (the shape the plan's
    ``experimental.cacheTtl`` example needs), or a block sequence, whose
    ``- item`` lines collect into an actual list at ``result[key]`` (COV-10:
    a skill's ``paths:`` frontmatter field is conventionally written this
    way, not as an inline ``[a, b]`` list -- see :func:`_parse_scalar` for
    that shorter form). Returns ``{}`` if the file has no frontmatter block
    at all.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}

    result: dict = {}
    last_top_key: str | None = None
    for raw_line in lines[1:end]:
        if not raw_line.strip():
            continue
        if raw_line[:1] in (" ", "\t") and last_top_key is not None:
            stripped = raw_line.strip()
            if stripped.startswith("- "):
                item = _parse_scalar(stripped[2:])
                existing = result.get(last_top_key)
                if isinstance(existing, list):
                    existing.append(item)
                else:
                    result[last_top_key] = [item]
                continue
            if ":" not in stripped:
                continue
            child_key, _, rest = stripped.partition(":")
            result[f"{last_top_key}.{child_key.strip()}"] = _parse_scalar(rest)
            continue
        stripped = raw_line.strip()
        if ":" not in stripped:
            last_top_key = None
            continue
        key, _, rest = stripped.partition(":")
        key = key.strip()
        rest = rest.strip()
        if rest == "":
            last_top_key = key
            continue
        result[key] = _parse_scalar(rest)
        last_top_key = None
    return result


def _load_agents(agents_dir: Path, source: str) -> dict:
    """Every ``*.md`` under ``agents_dir``, scanned recursively (COV-10,
    D9: "Claude Code scans .claude/agents/ and ~/.claude/agents/
    recursively, so you can organize definitions into subfolders" --
    docs/en/sub-agents.md; a subfolder doesn't change identity for
    project/user agents, only for plugin agents), keyed by its
    frontmatter ``name`` (falling back to the filename stem), redacted
    per-field, each tagged with ``source`` (schema 2: "user" or
    "project" -- which of the two agent directories it came from, before
    any project-wins merge).
    """
    result: dict = {}
    if not agents_dir.is_dir():
        return result
    for md_path in sorted(agents_dir.rglob("*.md")):
        try:
            text = md_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        parsed = parse_frontmatter(text)
        if not parsed:
            continue
        name = _clip_name(parsed.get("name") or md_path.stem)
        entry = redact_agent_frontmatter(parsed)
        entry["source"] = source
        result[name] = entry
    return result


def _merge_agents(user_agents: dict, project_agents: dict) -> dict:
    """Project agents win on a name clash (matching Claude Code's own
    resolution order), same as schema 1's plain ``dict.update``. Schema 2
    additionally flags the winning entry ``shadowed_by_project`` so a
    report can say "N agents are shadowed" without re-deriving it from two
    separate directory listings.
    """
    merged = dict(user_agents)
    for name, entry in project_agents.items():
        if name in merged:
            entry = dict(entry)
            entry["shadowed_by_project"] = True
        merged[name] = entry
    return merged


# -- hashing / timestamps ---------------------------------------------------


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_prefixed(text: str) -> str:
    return f"sha256:{_sha256_hex(text)}"


def _now_ts(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime(_TS_FORMAT)


def _content_hash(snapshot: dict) -> str:
    """sha256 of the snapshot with ``ts``/``session_id``/
    ``transcript_path_hash``/``source`` excluded, so an idempotent re-run
    (same config, new session, new timestamp) compares equal.
    """
    payload = {
        key: value
        for key, value in snapshot.items()
        if key not in ("ts", "session_id", "transcript_path_hash", "source")
    }
    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


# -- schema 2: project slug --------------------------------------------------


def _project_dir(cwd: str | None) -> str | None:
    """The folder the session started in: ``CLAUDE_PROJECT_DIR``, which
    Claude Code sets for every hook. Stdin's ``cwd`` is the shell's folder
    now, and a session that ran ``cd`` into a worktree or a subfolder
    before compacting would record that folder's agents and settings as
    another project's (a worktree's committed agent files read as the
    project's own). Inside the project folder, ``cwd``'s own spelling of
    it is kept (Windows paths differ in case), so the project slug stays
    the one earlier snapshots used. ``cwd`` when the variable isn't set."""
    project = (os.environ.get("CLAUDE_PROJECT_DIR") or "").rstrip("\\/")
    if not project:
        return cwd
    if cwd:
        folded, root = os.path.normcase(cwd), os.path.normcase(project)
        if folded == root or folded.startswith(root.rstrip(os.sep) + os.sep):
            return cwd[: len(project)]
    return project


def _project_slug(cwd: str) -> str:
    """``discovery.slug_for``'s own algorithm (non-alphanumeric -> ``-``,
    truncated to 200 chars plus an 8-hex hash when longer), duplicated
    rather than imported -- see the module docstring. Honours
    ``CLAUDE_CODE_PROJECT_DIR_NAME`` the same way ``slug_for`` does. The
    slug is deliberately not treated as a raw path needing a hash: it's
    already the on-disk directory name every transcript under
    ``~/.claude/projects/<slug>/`` uses, and its non-alnum substitution
    means it no longer contains a drive-letter-colon or path separator
    (the project's privacy scan's own allowance -- see the WP7 brief).
    """
    project_dir_name = os.environ.get("CLAUDE_CODE_PROJECT_DIR_NAME")
    if project_dir_name:
        return project_dir_name
    raw = str(cwd)
    slug = _SLUG_NON_ALNUM_RE.sub("-", raw)
    if len(slug) <= _SLUG_MAX_CHARS:
        return slug
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:_SLUG_HASH_HEX_CHARS]
    return f"{slug[:_SLUG_MAX_CHARS]}-{digest}"


#: Review finding #2: ``_project_slug`` only swaps separators for ``-``,
#: it does not remove the *content* -- the result still carries the
#: username and full directory structure (e.g.
#: ``C--Users-alice-work-acme-client``) and was being stored verbatim in
#: the snapshot and printed in report tables / ``probe-config`` Markdown.
#: This reduces the slug to an opaque join key before it is ever stored:
#: same shape as ``cwd_hash``/``source_path_hash`` elsewhere in this file.
#: Duplicates the (future) ``discovery.redact_slug`` algorithm rather than
#: importing it -- this script stays standalone stdlib (module docstring)
#: and ``discovery.redact_slug`` is landing on a sibling branch; once it
#: exists, package-level callers (``snapshots.py``, ``cli.py``) can prefer
#: it directly since both read the *stored* (already-redacted) slug back
#: off the snapshot rather than recomputing it.
_SLUG_HASH_PREFIX = "slug"
_SLUG_HASH_HEX_CHARS_REDACTED = 12


#: A Windows slug starts with its drive letter (``C--Dev-x``), and Claude
#: Code writes the letter in whichever case the folder was opened with, so
#: one project can have two slugs. The key uses the upper-case spelling.
#: Duplicates ``snapshots._DRIVE_SLUG_RE`` (this script imports nothing from
#: the package); ``snapshots.snapshot_project_keys()[0]`` must give the same
#: key, which ``test_hook`` checks.
_DRIVE_SLUG_RE = re.compile(r"^[A-Za-z]--")


def _redact_slug(slug: str) -> str:
    if _DRIVE_SLUG_RE.match(slug):
        slug = slug[0].upper() + slug[1:]
    digest = hashlib.sha256(slug.encode("utf-8")).hexdigest()[:_SLUG_HASH_HEX_CHARS_REDACTED]
    return f"{_SLUG_HASH_PREFIX}:{digest}"


#: Review finding #5: every *value* this hook records goes through some
#: redaction, but names (plugin/MCP-server/agent/skill names, and the
#: ``tools`` list an ``AGENT_KEEP_KEYS`` entry keeps verbatim) were
#: recorded with no shape check and no length cap -- unlike ``probe.py``'s
#: own ``_clip``/``MAX_VALUE_CHARS`` for exactly this reason. Mirrors that
#: helper: caps length, and additionally masks a name shaped like an
#: absolute path or a URL (a locally sourced plugin marketplace, an MCP
#: server named after its endpoint, an agent whose frontmatter ``name`` is
#: a path) rather than recording it verbatim.
_MAX_NAME_CHARS = 64
_NAME_LOOKS_LIKE_PATH_OR_URL_RE = re.compile(
    r"^[A-Za-z]:[\\/]|^[\\/]{1,2}|^[a-zA-Z][a-zA-Z0-9+.-]*://"
)


def _clip_name(value: object) -> str:
    text = str(value)
    if _NAME_LOOKS_LIKE_PATH_OR_URL_RE.search(text):
        return f"<redacted path/url, str({len(text)})>"
    if len(text) <= _MAX_NAME_CHARS:
        return text
    return text[: _MAX_NAME_CHARS - 3] + "..."


# -- schema 2: settings layers ------------------------------------------------


def _extract_enabled_plugins(raw_settings: dict) -> list[str]:
    raw = raw_settings.get("enabledPlugins")
    if isinstance(raw, dict):
        return sorted(_clip_name(k) for k in raw)
    if isinstance(raw, list):
        return sorted(_clip_name(x) for x in raw)
    return []


def _permissions_summary(raw_settings: dict) -> dict:
    """Rule *counts* (never the rules themselves -- a Bash allowlist entry
    is exactly the kind of content this hook must not record) plus
    ``defaultMode``, which is a small enum-like string already safe to
    keep verbatim (same posture as the settings allowlist).
    """
    perms = raw_settings.get("permissions")
    if not isinstance(perms, dict):
        return {"allow_count": 0, "deny_count": 0, "ask_count": 0, "default_mode": None}

    def _count(key: str) -> int:
        value = perms.get(key)
        return len(value) if isinstance(value, list) else 0

    default_mode = perms.get("defaultMode")
    return {
        "allow_count": _count("allow"),
        "deny_count": _count("deny"),
        "ask_count": _count("ask"),
        "default_mode": default_mode if isinstance(default_mode, str) else None,
    }


def _hooks_summary(raw_settings: dict) -> dict:
    """Hook *counts* per event name -- never the commands a hook runs."""
    hooks = raw_settings.get("hooks")
    if not isinstance(hooks, dict):
        return {}
    out: dict = {}
    for event_name, entries in hooks.items():
        if isinstance(entries, list):
            out[str(event_name)] = len(entries)
    return out


def summarize_settings_layer(raw_settings: dict | None, path: Path, present: bool) -> dict:
    """One :data:`SETTINGS_LAYER_ORDER` entry: presence, hashed source path
    (never the raw path), a content hash (so two layers/machines can be
    compared for equality without diffing raw JSON), the existing
    allowlist-redacted settings (``redacted``), and the purpose-built
    summaries a report needs directly without re-deriving them from
    ``redacted`` (env names, permission/hook counts, plugin names, and the
    named safe scalars).
    """
    raw_settings = raw_settings or {}
    content_hash = None
    if present:
        encoded = json.dumps(raw_settings, sort_keys=True, default=str)
        content_hash = _sha256_prefixed(encoded)

    env_block = raw_settings.get("env")
    env_names = sorted(str(k) for k in env_block) if isinstance(env_block, dict) else []

    return {
        "present": present,
        "source_path_hash": _sha256_prefixed(str(path)),
        "content_hash": content_hash,
        "redacted": redact_settings(raw_settings),
        "env_names": env_names,
        "permissions": _permissions_summary(raw_settings),
        "hooks": _hooks_summary(raw_settings),
        "enabled_plugins": _extract_enabled_plugins(raw_settings),
        "model": raw_settings.get("model"),
        "effort_level": raw_settings.get("effortLevel"),
        "max_effort_level": raw_settings.get("maxEffortLevel"),
        "model_settings": _redact_model_settings(raw_settings.get("modelSettings")),
        "always_thinking_enabled": raw_settings.get("alwaysThinkingEnabled"),
        "auto_compact_window": raw_settings.get("autoCompactWindow"),
        "prompt_cache_ttl": raw_settings.get("promptCacheTtl"),
        "subagent_prompt_cache_ttl": raw_settings.get("subagentPromptCacheTtl"),
        "cleanup_period_days": raw_settings.get("cleanupPeriodDays"),
        "output_style": raw_settings.get("outputStyle"),
        "statusline_present": bool(raw_settings.get("statusLine")),
    }


def _settings_layer_path(layer: str, cwd_path: Path, claude_root: Path, managed_path: Path) -> Path:
    return {
        "managed": managed_path,
        "project_local": cwd_path / ".claude" / "settings.local.json",
        "project_shared": cwd_path / ".claude" / "settings.json",
        "user": claude_root / "settings.json",
    }[layer]


def build_settings_layers(
    cwd_path: Path, claude_root: Path, managed_path: Path, raw_settings_by_layer: dict[str, dict]
) -> dict:
    """Every :data:`SETTINGS_LAYER_ORDER` entry, keyed by layer name.
    ``raw_settings_by_layer`` supplies the already-read raw dict for any
    layer the caller has read for its own purposes (schema 1's
    ``user_settings_raw``/``managed_settings_raw``), so the file is never
    read from disk twice.
    """
    layers: dict = {}
    for layer in SETTINGS_LAYER_ORDER:
        path = _settings_layer_path(layer, cwd_path, claude_root, managed_path)
        raw = raw_settings_by_layer.get(layer)
        layers[layer] = summarize_settings_layer(raw, path, present=raw is not None)
    return layers


def build_effective_settings(raw_settings_by_layer: dict[str, dict]) -> tuple[dict, dict]:
    """Merge :data:`SETTINGS_SUMMARY_KEYS` across the settings layers in
    :data:`SETTINGS_LAYER_ORDER` precedence (high to low): the first layer
    (in that order) that defines a key wins. Returns ``(effective,
    provenance)`` where ``provenance[key]`` names the winning layer.
    Values go through :func:`redact_settings_value` exactly as the
    per-layer ``redacted`` dict does, so ``statusLine``/``modelPricing``
    still resolve to their safe summary shape here too.
    """
    effective: dict = {}
    provenance: dict = {}
    for key in sorted(SETTINGS_SUMMARY_KEYS):
        for layer in SETTINGS_LAYER_ORDER:
            raw = raw_settings_by_layer.get(layer)
            if raw and key in raw:
                effective[key] = redact_settings_value(key, raw[key])
                provenance[key] = layer
                break
    return effective, provenance


def build_effective_env_names(raw_settings_by_layer: dict[str, dict]) -> tuple[list[str], dict]:
    """Every name set in any layer's own ``env`` block, deep-merged the way
    Claude Code actually resolves it (COV-03, D6): "If more than one
    settings file sets a variable... the highest-precedence file's value
    is used" (docs/en/settings.md). A per-layer ``env_names`` list alone
    (see :func:`summarize_settings_layer`) only shows what one file sets,
    not what a lower-precedence file contributes that no higher layer
    overrides -- the two differ whenever the layers set *different* names.
    Returns ``(names, provenance)``; ``provenance[name]`` is the
    highest-precedence layer that sets it -- the one actually in effect.
    Names only, never values, matching the per-layer posture.
    """
    names: set[str] = set()
    provenance: dict[str, str] = {}
    for layer in SETTINGS_LAYER_ORDER:
        raw = raw_settings_by_layer.get(layer)
        env_block = raw.get("env") if isinstance(raw, dict) else None
        if not isinstance(env_block, dict):
            continue
        for name in env_block:
            name = str(name)
            names.add(name)
            provenance.setdefault(name, layer)
    return sorted(names), provenance


def build_effective_permissions(raw_settings_by_layer: dict[str, dict]) -> dict:
    """``permissions.allow``/``deny``/``ask`` deep-merged across every
    layer (COV-03, D6): "Lists merge instead of overriding... permission
    rules from different settings files are combined" (docs/en/
    settings.md), so a per-layer count alone understates what's actually
    in effect whenever more than one layer contributes rules. Counts the
    *unique* rule strings across all layers -- the rule text is used only
    in-process to dedupe and is never itself recorded, matching
    :func:`_permissions_summary`'s existing counts-only posture.
    ``default_mode`` does not merge (docs don't describe a combine rule
    for it); it's the highest-precedence layer's own value.
    """
    allow: set[str] = set()
    deny: set[str] = set()
    ask: set[str] = set()
    default_mode = None
    for layer in SETTINGS_LAYER_ORDER:
        raw = raw_settings_by_layer.get(layer)
        perms = raw.get("permissions") if isinstance(raw, dict) else None
        if not isinstance(perms, dict):
            continue
        for bucket, target in (("allow", allow), ("deny", deny), ("ask", ask)):
            value = perms.get(bucket)
            if isinstance(value, list):
                target.update(str(v) for v in value)
        if default_mode is None:
            mode = perms.get("defaultMode")
            if isinstance(mode, str):
                default_mode = mode
    return {
        "allow_count": len(allow),
        "deny_count": len(deny),
        "ask_count": len(ask),
        "default_mode": default_mode,
    }


def build_effective_hooks(raw_settings_by_layer: dict[str, dict]) -> dict:
    """Hook entry counts per event, summed across every layer (COV-03,
    D6): "Hook entries merge across settings levels rather than replacing
    each other: user, project, and local settings add their own hooks
    without removing managed ones" (docs/en/hooks.md) -- additive, unlike
    the scalar settings keys :func:`build_effective_settings` merges.
    """
    totals: dict[str, int] = {}
    for layer in SETTINGS_LAYER_ORDER:
        raw = raw_settings_by_layer.get(layer)
        hooks = raw.get("hooks") if isinstance(raw, dict) else None
        if not isinstance(hooks, dict):
            continue
        for event_name, entries in hooks.items():
            if isinstance(entries, list):
                key = str(event_name)
                totals[key] = totals.get(key, 0) + len(entries)
    return totals


def build_effective_enabled_plugins(raw_settings_by_layer: dict[str, dict]) -> list[str]:
    """``enabledPlugins`` resolved per plugin name across every layer
    (COV-03, D6): each plugin name is its own key, decided by the
    highest-precedence layer that names it -- not a union of "enabled
    anywhere" (docs/en/settings-reference.md's ``enabledPlugins`` entry:
    "Project settings take precedence over user settings, so setting a
    plugin to false in ~/.claude/settings.json doesn't disable a plugin
    that the project's .claude/settings.json enables"). Per-layer
    ``enabled_plugins`` (see :func:`_extract_enabled_plugins`) only lists
    one file's own keys, plugin previously read from the user layer only
    (D6) -- this is what's actually enabled. Names only.
    """
    resolved: dict[str, bool] = {}
    for layer in SETTINGS_LAYER_ORDER:
        raw = raw_settings_by_layer.get(layer)
        entry = raw.get("enabledPlugins") if isinstance(raw, dict) else None
        if not isinstance(entry, dict):
            continue
        for name, value in entry.items():
            name = str(name)
            if name not in resolved and isinstance(value, bool):
                resolved[name] = value
    return sorted(_clip_name(name) for name, enabled in resolved.items() if enabled)


def build_effective_mcpjson_servers(raw_settings_by_layer: dict[str, dict]) -> dict:
    """``enabledMcpjsonServers``/``disabledMcpjsonServers`` deep-merged
    across every layer (COV-03): both are "Lists merge instead of
    overriding" arrays (docs/en/settings.md), and a rejection anywhere
    wins ("Rejection takes precedence over enabledMcpjsonServers and
    enableAllProjectMcpServers" -- docs/en/settings-reference.md's
    ``disabledMcpjsonServers`` entry) -- previously read from the user
    layer only (D6), missing e.g. a project-local rejection.
    """
    enabled: set[str] = set()
    disabled: set[str] = set()
    for layer in SETTINGS_LAYER_ORDER:
        raw = raw_settings_by_layer.get(layer)
        if not isinstance(raw, dict):
            continue
        for key, target in (("enabledMcpjsonServers", enabled), ("disabledMcpjsonServers", disabled)):
            value = raw.get(key)
            if isinstance(value, list):
                target.update(str(v) for v in value)
    enabled -= disabled
    return {
        "enabled": sorted(_clip_name(name) for name in enabled),
        "disabled": sorted(_clip_name(name) for name in disabled),
    }


def build_effective_agents(agents: dict) -> dict:
    """``agents`` (already source/shadow-tagged by :func:`_merge_agents`)
    reduced to the handful of fields a TTL/model/effort recommendation
    actually keys off, one entry per agent name.
    """
    return {
        name: {
            "source": entry.get("source"),
            "experimental_cache_ttl": entry.get("experimental.cacheTtl"),
            "model": entry.get("model"),
            "effort": entry.get("effort"),
            "max_turns": entry.get("maxTurns"),
        }
        for name, entry in agents.items()
    }


# -- schema 2: ~/.claude.json cross-check ------------------------------------

#: The per-project ``last*`` session-statistics keys this hook records if
#: present (checked against a real ~/.claude.json on this machine -- see
#: the module docstring). Every value here is a number or (``lastSessionId``)
#: an opaque id, never text.
_CLAUDE_JSON_LAST_SESSION_KEYS: tuple[str, ...] = (
    "lastCost",
    "lastDuration",
    "lastAPIDuration",
    "lastTotalInputTokens",
    "lastTotalOutputTokens",
    "lastTotalCacheCreationInputTokens",
    "lastTotalCacheReadInputTokens",
    "lastSessionId",
    "lastLinesAdded",
    "lastLinesRemoved",
)


def _normcase_realpath(path_str: str) -> str:
    """Never raises: a ``~/.claude.json`` project key or a live ``cwd`` can
    both be arbitrary strings, and this only ever feeds an equality check.
    """
    try:
        return os.path.normcase(os.path.realpath(path_str))
    except (OSError, ValueError):
        return os.path.normcase(path_str)


def _find_claude_json_project_entry(dot_claude_json: dict, cwd_path: Path) -> dict | None:
    """The ``projects`` entry matching ``cwd_path``, found by comparing
    ``normcase(realpath(...))`` on every key -- ``~/.claude.json`` is
    observed to hold the same directory under several spellings at once
    (forward slashes, backslashes, drive-letter case). The raw matching key
    is deliberately never returned or stored (fix: raw paths never
    recorded) -- only the entry's own values.
    """
    projects = dot_claude_json.get("projects")
    if not isinstance(projects, dict):
        return None
    target = _normcase_realpath(str(cwd_path))
    best: dict | None = None
    for raw_key, entry in projects.items():
        if not isinstance(entry, dict):
            continue
        if _normcase_realpath(str(raw_key)) != target:
            continue
        if best is None or entry.get("lastSessionId"):
            best = entry
    return best


def build_claude_json_section(cwd_path: Path, dot_claude_json: dict | None) -> dict:
    """A redacted view of an already-read ``~/.claude.json`` (the CLI's
    own per-machine state file -- distinct from any Claude Code *settings*
    file): whether this project has an entry at all, its MCP server/plugin
    names and small counts, and its ``last*`` session totals if present --
    a cross-check against this tool's own accounting for the same session
    (joined later by ``lastSessionId``). Degrades to ``{"matched": False}``
    on a missing, unreadable or malformed file, or one with no matching
    project entry -- never raises.

    Fix #19: ``dot_claude_json`` is now read once by the caller
    (``build_snapshot``) and passed in here, rather than this function
    re-reading and re-parsing the same (potentially multi-MB) file that
    ``build_snapshot``'s own MCP-name gathering also reads --
    ``build_settings_layers``'s docstring already establishes "read once"
    as this file's convention; this closes the one place that didn't.
    """
    if dot_claude_json is None:
        return {"matched": False}

    entry = _find_claude_json_project_entry(dot_claude_json, cwd_path)

    result: dict = {"matched": entry is not None}
    if entry is not None:
        mcp_servers = entry.get("mcpServers")
        result["mcp_servers"] = (
            sorted(_clip_name(k) for k in mcp_servers) if isinstance(mcp_servers, dict) else []
        )
        enabled = entry.get("enabledMcpjsonServers")
        result["enabled_mcpjson_servers"] = (
            sorted(_clip_name(x) for x in enabled) if isinstance(enabled, list) else []
        )
        disabled = entry.get("disabledMcpjsonServers")
        result["disabled_mcpjson_servers"] = (
            sorted(_clip_name(x) for x in disabled) if isinstance(disabled, list) else []
        )
        allowed_tools = entry.get("allowedTools")
        result["allowed_tools_count"] = len(allowed_tools) if isinstance(allowed_tools, list) else 0
        result["has_trust_dialog_accepted"] = bool(entry.get("hasTrustDialogAccepted"))

        last_session: dict = {}
        for key in _CLAUDE_JSON_LAST_SESSION_KEYS:
            if key not in entry:
                continue
            value = entry[key]
            if key == "lastSessionId":
                if isinstance(value, str):
                    last_session[key] = value
            elif isinstance(value, bool):
                continue  # not one of the documented numeric fields
            elif isinstance(value, (int, float)):
                last_session[key] = value
        if last_session:
            result["last_session"] = last_session

    projects = dot_claude_json.get("projects")
    top_level_scalars: dict = {}
    for key, value in dot_claude_json.items():
        if key == "projects":
            continue
        if isinstance(value, bool) or isinstance(value, (int, float)):
            top_level_scalars[key] = value
        elif isinstance(value, str):
            top_level_scalars[key] = f"str({len(value)})"
        # dict/list top-level fields (oauthAccount, tipsHistory, ...) are
        # skipped entirely: unpredictable shape that can hold identity data,
        # unlike the small fixed set of per-project fields handled above.
    result["top_level"] = {
        "num_projects": len(projects) if isinstance(projects, dict) else 0,
        "scalars": top_level_scalars,
    }
    return result


# -- schema 2: content layers -------------------------------------------------


def _file_bytes(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def _count_bytes_for_glob(base: Path, pattern: str) -> tuple[int, int]:
    if not base.is_dir():
        return 0, 0
    count = 0
    total = 0
    try:
        paths = list(base.glob(pattern))
    except OSError:
        return 0, 0
    for candidate in paths:
        try:
            if candidate.is_file():
                count += 1
                total += candidate.stat().st_size
        except OSError:
            continue
    return count, total


def _walk_nested_claude_md(root: Path) -> tuple[int, int]:
    """Bounded walk for nested ``CLAUDE.md`` files below (not including)
    ``root`` itself -- the project root's own ``CLAUDE.md``/``CLAUDE.local.md``
    are recorded separately. Depth-limited to
    :data:`_CLAUDE_MD_WALK_MAX_DEPTH` and :data:`_CLAUDE_MD_WALK_MAX_DIRS`
    directories visited, skipping :data:`_CLAUDE_MD_WALK_SKIP_DIRS`, and
    now also wall-clock-bounded to :data:`_CLAUDE_MD_WALK_MAX_SECONDS`
    (fix #18) -- the depth/breadth bounds cap the number of directories
    visited, not the per-directory cost, so a single huge directory could
    otherwise still make a SessionStart hang. Uses ``os.scandir`` with
    ``entry.is_dir(follow_symlinks=False)`` rather than
    ``Path.iterdir()``/``Path.is_dir()``/``Path.is_file()``, which each
    issue their own ``stat`` call -- ``DirEntry`` caches that information
    from the original directory read on most platforms.
    """
    count = 0
    total_bytes = 0
    visited_dirs = 0
    deadline = time.monotonic() + _CLAUDE_MD_WALK_MAX_SECONDS
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        if visited_dirs >= _CLAUDE_MD_WALK_MAX_DIRS or time.monotonic() >= deadline:
            break
        current, depth = stack.pop()
        visited_dirs += 1
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name in _CLAUDE_MD_WALK_SKIP_DIRS:
                        continue
                    if depth < _CLAUDE_MD_WALK_MAX_DEPTH:
                        stack.append((Path(entry.path), depth + 1))
                elif depth > 0 and entry.name == "CLAUDE.md" and entry.is_file(follow_symlinks=False):
                    count += 1
                    total_bytes += entry.stat().st_size
            except OSError:
                continue
    return count, total_bytes


#: Skill frontmatter fields kept as short scalars (COV-10/PROF-09, V18):
#: ``model``/``effort``/``context`` are closed-vocabulary-ish, enum-like
#: strings (a model id, an effort level, "fork") -- same posture as
#: ``SAFE_SETTINGS_KEYS``/``AGENT_KEEP_KEYS``. ``paths`` (the path-scoping
#: glob list) is deliberately NOT kept verbatim: PROF-09's privacy rule is
#: "store only closed values and counts... never free-text descriptions",
#: and a glob pattern can embed a real project-specific path, so only its
#: length (``paths_count``) is recorded.
_SKILL_FRONTMATTER_SCALAR_KEYS = ("model", "effort", "context")


def _skill_frontmatter_summary(skill_md: Path) -> dict:
    """``model``/``effort``/``context``/``paths_count`` off one skill's
    frontmatter (COV-10/PROF-09), so a recommendation can suggest a
    per-skill ``model:``/``effort:`` (noting V14: a skill ``model`` forces
    a full cache miss) without ever seeing the skill's body text or its
    free-text ``description``/``when_to_use`` fields, which this
    deliberately never reads.
    """
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"model": None, "effort": None, "context": None, "paths_count": 0}
    parsed = parse_frontmatter(text)
    summary: dict = {
        key: (_clip_name(parsed[key]) if isinstance(parsed.get(key), str) else None)
        for key in _SKILL_FRONTMATTER_SCALAR_KEYS
    }
    paths = parsed.get("paths")
    summary["paths_count"] = len(paths) if isinstance(paths, list) else 0
    return summary


def _skills_summary(skills_dir: Path) -> dict:
    """Skill *names* (the directory name under ``skills/``, already an
    identifier rather than content -- same posture as an agent name), the
    total bytes of their ``SKILL.md`` files, and (COV-10/PROF-09) each
    skill's closed-vocabulary frontmatter summary keyed by that same name.
    A skill directory without a ``SKILL.md`` is not a skill Claude Code
    will load, so it's excluded.
    """
    names: list[str] = []
    total_bytes = 0
    config: dict = {}
    if not skills_dir.is_dir():
        return {"names": names, "total_bytes": total_bytes, "config": config}
    try:
        entries = sorted(skills_dir.iterdir())
    except OSError:
        entries = []
    for entry in entries:
        try:
            if not entry.is_dir():
                continue
            skill_md = entry / "SKILL.md"
            if skill_md.is_file():
                name = _clip_name(entry.name)
                names.append(name)
                total_bytes += skill_md.stat().st_size
                config[name] = _skill_frontmatter_summary(skill_md)
        except OSError:
            continue
    return {"names": names, "total_bytes": total_bytes, "config": config}


def _plugin_content_summary(plugins_dir: Path, plugin_names: list[str]) -> dict:
    """Best-effort skill/agent counts for each installed plugin (COV-10:
    "scan content layers: ... plugin skills and agents"), using each
    plugin's *default* layout only: skills live in a ``skills/``
    directory (one subfolder per skill) or a single root ``SKILL.md``;
    agents live in ``agents/``, scanned recursively (docs/en/
    plugins-reference.md). A plugin manifest (``plugin.json``) can remap
    any of these to custom paths ("Component path fields") -- unlike the
    default layout, that remapping isn't replicated here, so a plugin
    using it undercounts; this mirrors :func:`_plugins_summary`'s own
    directory-listing approach, which is equally manifest-unaware.
    Skill names are recorded (matching project/user skills' posture);
    agents are a count only, since a plugin agent's *scoped* identifier
    joins the plugin name, every subfolder and the filename with colons
    (e.g. ``my-plugin:review:security``), which is more path-shaped
    detail than this hook's name allowlist is meant to carry.
    """
    result: dict = {}
    for name in plugin_names:
        plugin_dir = plugins_dir / name
        skills_names: list[str] = []
        skills_dir = plugin_dir / "skills"
        if skills_dir.is_dir():
            try:
                sub_entries = sorted(skills_dir.iterdir())
            except OSError:
                sub_entries = []
            for entry in sub_entries:
                try:
                    if entry.is_dir() and (entry / "SKILL.md").is_file():
                        skills_names.append(_clip_name(entry.name))
                except OSError:
                    continue
        else:
            try:
                has_root_skill = (plugin_dir / "SKILL.md").is_file()
            except OSError:
                has_root_skill = False
            if has_root_skill:
                skills_names.append(_clip_name(name))

        agents_count = 0
        agents_dir = plugin_dir / "agents"
        if agents_dir.is_dir():
            try:
                agents_count = sum(1 for p in agents_dir.rglob("*.md") if p.is_file())
            except OSError:
                agents_count = 0

        if skills_names or agents_count:
            result[_clip_name(name)] = {"skills": sorted(skills_names), "agents_count": agents_count}
    return result


#: Import parsing skips fenced code blocks and inline code spans (COV-10,
#: D9, docs/en/memory.md: "Import parsing skips Markdown code spans and
#: fenced code blocks... writing `@README` keeps the text literal") --
#: approximated (not a full Markdown parser) by stripping both before
#: counting ``@token`` occurrences.
_CLAUDE_MD_CODE_FENCE_RE = re.compile(r"```.*?```|~~~.*?~~~", re.DOTALL)
_CLAUDE_MD_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_CLAUDE_MD_IMPORT_TOKEN_RE = re.compile(r"@[^\s`]+")


def _count_claude_md_imports(path: Path) -> int:
    """Count of ``@path/to/import`` references in one CLAUDE.md-family
    file (COV-10, D9: "CLAUDE.md files can import additional files using
    @path/to/import syntax" -- docs/en/memory.md). A count only -- never
    the import paths themselves, which are exactly the kind of
    local-filesystem detail this hook must not record.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    text = _CLAUDE_MD_CODE_FENCE_RE.sub("", text)
    text = _CLAUDE_MD_INLINE_CODE_RE.sub("", text)
    return len(_CLAUDE_MD_IMPORT_TOKEN_RE.findall(text))


def _plugins_summary(claude_root: Path) -> dict:
    plugins_dir = claude_root / "plugins"
    names: list[str] = []
    marketplaces = 0
    if plugins_dir.is_dir():
        try:
            names = sorted(entry.name for entry in plugins_dir.iterdir() if entry.is_dir())
        except OSError:
            names = []
        marketplaces_dir = plugins_dir / "marketplaces"
        if marketplaces_dir.is_dir():
            try:
                marketplaces = sum(1 for entry in marketplaces_dir.iterdir() if entry.is_dir())
            except OSError:
                marketplaces = 0
    # "marketplaces" is itself a plugin-manager bookkeeping directory, not
    # an installed plugin -- excluded from the installed-plugin name list.
    names = [name for name in names if name != "marketplaces"]
    return {"names": names, "marketplaces": marketplaces}


def _memory_summary(claude_root: Path, project_slug: str) -> dict:
    """This project's auto-memory directory (``MEMORY.md`` plus the
    individual per-topic leaf files it indexes) -- byte/file count only,
    never content.
    """
    memory_dir = claude_root / "projects" / project_slug / "memory"
    if not memory_dir.is_dir():
        return {"present": False, "files": 0, "bytes": 0}
    files = 0
    total_bytes = 0
    try:
        candidates = list(memory_dir.rglob("*.md"))
    except OSError:
        candidates = []
    for path in candidates:
        try:
            if path.is_file():
                files += 1
                total_bytes += path.stat().st_size
        except OSError:
            continue
    return {"present": files > 0, "files": files, "bytes": total_bytes}


def build_content_layers(
    cwd_path: Path, claude_root: Path, project_slug: str, agents: dict, managed_dir: Path | None = None
) -> dict:
    """Sizes, counts and names only (never content) for every content
    layer the plan's "Configuration layers" section lists: the CLAUDE.md
    family (plus, COV-10, each file's ``@import`` reference count), rules,
    commands, skills (plus, COV-10/PROF-09, each skill's closed-vocabulary
    frontmatter summary), an agents source/shadow rollup, the project's
    own ``.mcp.json``, ``managed-mcp.json`` (COV-05: looked up in the same
    system managed-settings directory as ``managed-settings.json``, not
    ``claude_root``), plugin skills/agents (COV-10), output style names,
    this project's auto-memory footprint, installed plugin
    names/marketplace count, whether ``CLAUDE_CONFIG_DIR`` is set at
    all (never its value, which is a path), and (PROF-03) whether
    ``CLAUDE_CODE_EFFORT_LEVEL`` is set (never its value).

    ``managed_dir`` is the system managed-settings directory (see
    :func:`default_managed_settings_dir`); defaults to the platform
    default when omitted, same posture as ``build_snapshot``'s own
    ``managed_path`` default.
    """
    nested_count, nested_bytes = _walk_nested_claude_md(cwd_path)
    rules_count, rules_bytes = _count_bytes_for_glob(cwd_path / ".claude" / "rules", "*.md")
    commands_count, commands_bytes = _count_bytes_for_glob(cwd_path / ".claude" / "commands", "**/*.md")

    user_claude_md = claude_root / "CLAUDE.md"
    project_root_claude_md = cwd_path / "CLAUDE.md"
    project_local_claude_md = cwd_path / "CLAUDE.local.md"

    mcp_json = _read_json_dict(cwd_path / ".mcp.json")
    mcp_json_names: list[str] = []
    if mcp_json is not None:
        servers = mcp_json.get("mcpServers")
        if isinstance(servers, dict):
            mcp_json_names = sorted(_clip_name(k) for k in servers)

    managed_dir = managed_dir if managed_dir is not None else default_managed_settings_dir()
    managed_mcp_path = managed_dir / "managed-mcp.json"
    managed_mcp_json = _read_json_dict(managed_mcp_path)
    managed_mcp_names: list[str] = []
    if managed_mcp_json is not None:
        servers = managed_mcp_json.get("mcpServers")
        if isinstance(servers, dict):
            managed_mcp_names = sorted(_clip_name(k) for k in servers)

    output_styles_dir = claude_root / "output-styles"
    output_style_names: list[str] = []
    if output_styles_dir.is_dir():
        try:
            output_style_names = sorted(p.stem for p in output_styles_dir.glob("*.md"))
        except OSError:
            output_style_names = []

    plugin_names = _plugins_summary(claude_root)["names"]
    plugin_content = _plugin_content_summary(claude_root / "plugins", plugin_names)

    return {
        "claude_md": {
            "user_bytes": _file_bytes(user_claude_md),
            "project_root_bytes": _file_bytes(project_root_claude_md),
            "project_local_bytes": _file_bytes(project_local_claude_md),
            "nested_count": nested_count,
            "nested_bytes": nested_bytes,
            "user_imports": _count_claude_md_imports(user_claude_md),
            "project_root_imports": _count_claude_md_imports(project_root_claude_md),
            "project_local_imports": _count_claude_md_imports(project_local_claude_md),
        },
        "rules": {"count": rules_count, "bytes": rules_bytes},
        "commands": {"count": commands_count, "bytes": commands_bytes},
        "skills": {
            "project": _skills_summary(cwd_path / ".claude" / "skills"),
            "user": _skills_summary(claude_root / "skills"),
        },
        "agents_summary": {
            "count": len(agents),
            "user_count": sum(1 for a in agents.values() if a.get("source") == "user"),
            "project_count": sum(1 for a in agents.values() if a.get("source") == "project"),
            "shadowed_count": sum(1 for a in agents.values() if a.get("shadowed_by_project")),
        },
        "mcp_json": {"present": mcp_json is not None, "names": mcp_json_names},
        "managed_mcp_present": managed_mcp_path.is_file(),
        "managed_mcp": {"present": managed_mcp_json is not None, "names": managed_mcp_names},
        "plugin_content": plugin_content,
        "output_styles": output_style_names,
        "memory": _memory_summary(claude_root, project_slug),
        "plugins": _plugins_summary(claude_root),
        "claude_config_dir_set": bool(os.environ.get("CLAUDE_CONFIG_DIR")),
        "effort_level_env_set": bool(os.environ.get("CLAUDE_CODE_EFFORT_LEVEL")),
    }


# -- snapshot assembly -------------------------------------------------------


def _load_stdin_json() -> dict:
    """Read and parse the hook's stdin payload. Empty, unreadable or
    malformed stdin all tolerate down to ``{}`` — the hook must never fail
    a session start over a stdin problem.
    """
    try:
        raw = sys.stdin.read()
    except Exception:
        return {}
    raw = raw.strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def build_snapshot(
    stdin_data: dict,
    cwd_override: str | None,
    config_dir: Path,
    managed_path: Path | str | None = None,
) -> dict:
    """Build the full A6-shaped snapshot dict (unwritten) for the given
    hook stdin payload, cwd override and resolved config directory.

    ``config_dir`` is the claudeglass directory (see
    :func:`resolve_config_dir`); ``settings.json``/``agents/`` live in
    Claude Code's own folder (:func:`resolve_claude_root`).

    ``managed_path`` overrides the platform default from
    :func:`default_managed_settings_path` (used by tests and by the
    ``--managed-path`` CLI flag); it is read even if missing, since most
    machines have no managed-settings file at all and that must degrade to
    ``{}``/``[]`` rather than an error.
    """
    session_id = stdin_data.get("session_id")
    transcript_path = stdin_data.get("transcript_path")
    source = stdin_data.get("source")
    cwd = cwd_override or _project_dir(stdin_data.get("cwd")) or os.getcwd()
    cwd_path = Path(cwd)
    claude_root = resolve_claude_root()

    claude_version = os.environ.get("CLAUDE_CODE_VERSION") or None
    profile_id = _read_active_profile(config_dir)

    # Kept as the raw ``dict | None`` (never defaulted to ``{}``) alongside
    # the schema-1 ``or {}`` convenience variable below it, since schema 2's
    # settings-layer presence flag needs to distinguish "file exists and is
    # an empty object" from "file doesn't exist" -- something ``or {}``
    # collapses into the same value.
    user_settings_raw_or_none = _read_json_dict(claude_root / "settings.json")
    user_settings_raw = user_settings_raw_or_none or {}
    user_settings = redact_settings(user_settings_raw)

    resolved_managed_path = Path(managed_path) if managed_path else default_managed_settings_path()
    managed_settings_raw_or_none = _read_json_dict(resolved_managed_path)
    managed_settings_raw = managed_settings_raw_or_none or {}
    managed_settings = redact_settings(managed_settings_raw)
    managed_keys = sorted(managed_settings_raw.keys())

    project_settings: dict = {}
    project_settings_raw: dict[str, dict | None] = {}
    for name in ("settings.json", "settings.local.json"):
        path = cwd_path / ".claude" / name
        data = _read_json_dict(path)
        project_settings_raw[name] = data
        if data is not None:
            project_settings[_sha256_hex(str(path))] = redact_settings(data)

    mcp_names: set[str] = set()
    # COV-03/D6 fix: ``mcpServers`` is not a valid settings.json key at any
    # scope (docs/en/settings-reference.md's key index has no such entry;
    # personal servers live in ``~/.claude.json``, project ones in
    # ``.mcp.json``) -- reading it off ``user_settings_raw`` here could
    # never fire. The one settings-layer key that legitimately carries MCP
    # servers is ``managedMcpServers``, "Managed"-scope only
    # (settings-reference.md: "object keyed by server name... Claude Code
    # drops the key... in user, project, and local settings").
    raw_managed_mcp_servers = managed_settings_raw.get("managedMcpServers")
    if isinstance(raw_managed_mcp_servers, dict):
        mcp_names.update(str(k) for k in raw_managed_mcp_servers)
    mcp_json = _read_json_dict(cwd_path / ".mcp.json")
    if mcp_json is not None:
        project_mcp_servers = mcp_json.get("mcpServers")
        if isinstance(project_mcp_servers, dict):
            mcp_names.update(str(k) for k in project_mcp_servers)
    # Fix #19: read once, reused below for build_claude_json_section too.
    # COV-05a fix: honours CLAUDE_CONFIG_DIR the same way ~/.claude itself
    # does (see resolve_dot_claude_json_path's docstring for the doc
    # citation) instead of always reading Path.home().
    dot_claude_json = _read_json_dict(resolve_dot_claude_json_path())
    if dot_claude_json is not None:
        global_mcp_servers = dot_claude_json.get("mcpServers")
        if isinstance(global_mcp_servers, dict):
            mcp_names.update(str(k) for k in global_mcp_servers)

    # COV-03/D6 fix: these two were read from the user layer only. They're
    # now deep-merged across every layer by build_effective_mcpjson_servers
    # below (schema 2's effective_mcpjson_servers); the schema-1 fields
    # here keep their original user-layer-only meaning unchanged for
    # backward compatibility with anything already reading them.
    enabled_mcpjson = user_settings_raw.get("enabledMcpjsonServers")
    disabled_mcpjson = user_settings_raw.get("disabledMcpjsonServers")

    mcp_servers = {
        "names": sorted(_clip_name(name) for name in mcp_names),
        "enabled_mcpjson_servers": sorted(_clip_name(x) for x in enabled_mcpjson)
        if isinstance(enabled_mcpjson, list)
        else [],
        "disabled_mcpjson_servers": sorted(_clip_name(x) for x in disabled_mcpjson)
        if isinstance(disabled_mcpjson, list)
        else [],
    }

    # COV-03/D6 fix: previously the user layer's own enabledPlugins keys,
    # regardless of layer precedence or true/false value (schema 1,
    # unchanged below for backward compatibility). effective_enabled_plugins
    # (schema 2) is the deep-merged, true-only, per-layer-precedence result.
    enabled_plugins = _extract_enabled_plugins(user_settings_raw)

    user_agents = _load_agents(claude_root / "agents", source="user")
    project_agents = _load_agents(cwd_path / ".claude" / "agents", source="project")
    agents = _merge_agents(user_agents, project_agents)

    env_names = sorted(
        name
        for name in os.environ
        if name.startswith(_ENV_NAME_PREFIXES) or name in _ENV_EXTRA_NAMES
    )
    env_numeric_caps: dict = {}
    for name in sorted(_ENV_NUMERIC_CAP_NAMES):
        raw_value = os.environ.get(name)
        if raw_value is None:
            continue
        try:
            env_numeric_caps[name] = int(raw_value)
        except ValueError:
            continue

    # Schema 2: settings layers / effective config / effective agents.
    raw_settings_by_layer = {
        "managed": managed_settings_raw_or_none,
        "project_local": project_settings_raw.get("settings.local.json"),
        "project_shared": project_settings_raw.get("settings.json"),
        "user": user_settings_raw_or_none,
    }
    settings_layers = build_settings_layers(cwd_path, claude_root, resolved_managed_path, raw_settings_by_layer)
    effective, effective_provenance = build_effective_settings(raw_settings_by_layer)
    effective_agents = build_effective_agents(agents)
    # COV-03: env/permissions/hooks/enabledPlugins/enabledMcpjsonServers
    # deep-merged across every settings layer (D6) -- see each function's
    # own docstring for the doc citation behind its merge rule.
    effective_env_names, effective_env_provenance = build_effective_env_names(raw_settings_by_layer)
    effective_permissions = build_effective_permissions(raw_settings_by_layer)
    effective_hooks = build_effective_hooks(raw_settings_by_layer)
    effective_enabled_plugins = build_effective_enabled_plugins(raw_settings_by_layer)
    effective_mcpjson_servers = build_effective_mcpjson_servers(raw_settings_by_layer)

    # Fix #2: the raw slug (still the real on-disk `~/.claude/projects/`
    # directory name) is kept ONLY for internal lookups that need the real
    # directory -- e.g. _memory_summary below -- and is never itself
    # stored in the snapshot or returned to a caller. Everything the
    # snapshot records or a report prints uses the redacted form.
    raw_project_slug = _project_slug(cwd)
    project_slug = _redact_slug(raw_project_slug)
    claude_json_section = build_claude_json_section(cwd_path, dot_claude_json)
    content_layers = build_content_layers(
        cwd_path, claude_root, raw_project_slug, agents, managed_dir=resolved_managed_path.parent
    )

    snapshot = {
        "schema": SCHEMA_VERSION,
        "ts": _now_ts(),
        "session_id": session_id,
        # Fix #1: the raw transcript_path is an absolute path
        # (C:\Users\<username>\.claude\projects\<slug>\<uuid>.jsonl) --
        # hashed the same way cwd_hash/source_path_hash already are,
        # rather than stored verbatim.
        "transcript_path_hash": _sha256_prefixed(transcript_path) if transcript_path else None,
        "source": source,
        "cwd_hash": _sha256_prefixed(cwd),
        "claude_version": claude_version,
        "profile_id": profile_id,
        "user_settings": user_settings,
        "managed_settings": managed_settings,
        "managed_keys": managed_keys,
        "project_settings": project_settings,
        "mcp_servers": mcp_servers,
        "enabled_plugins": enabled_plugins,
        "agents": agents,
        "env_names": env_names,
        "env_numeric_caps": env_numeric_caps,
        # -- schema 2 additions (additive; see module docstring) --
        "project_slug": project_slug,
        "settings_layers": settings_layers,
        "effective": effective,
        "effective_provenance": effective_provenance,
        "effective_agents": effective_agents,
        "effective_env_names": effective_env_names,
        "effective_env_provenance": effective_env_provenance,
        "effective_permissions": effective_permissions,
        "effective_hooks": effective_hooks,
        "effective_enabled_plugins": effective_enabled_plugins,
        "effective_mcpjson_servers": effective_mcpjson_servers,
        "claude_json": claude_json_section,
        "content_layers": content_layers,
    }
    snapshot["content_hash"] = _content_hash(snapshot)
    return snapshot


# -- idempotent write --------------------------------------------------------


def _snapshots_dir(config_dir: Path) -> Path:
    return config_dir / "snapshots"


#: How many of the newest snapshot files ``_should_skip`` will scan
#: looking for one matching ``project_slug`` (fix #12) -- bounded so a
#: user with a long snapshot history doesn't turn every SessionStart into
#: an unbounded directory scan.
_LATEST_SNAPSHOT_FOR_PROJECT_SCAN_LIMIT = 50


def _find_latest_snapshot(snapshots_dir: Path) -> Path | None:
    if not snapshots_dir.is_dir():
        return None
    files = sorted(p for p in snapshots_dir.glob("*.json") if p.is_file())
    return files[-1] if files else None


def _find_latest_snapshot_for_project(snapshots_dir: Path, project_slug: str) -> Path | None:
    """Fix #12: the newest existing snapshot whose own ``project_slug``
    matches ``project_slug`` -- scanned newest-first, stopping at the
    first match, bounded to the last
    :data:`_LATEST_SNAPSHOT_FOR_PROJECT_SCAN_LIMIT` files. ``_should_skip``
    used to compare against :func:`_find_latest_snapshot` (the globally
    newest file regardless of project), which made ``--min-interval``
    inoperative for anyone alternating between projects -- two different
    projects never hash equal, so the "same content_hash" half of the
    check never passed.
    """
    if not snapshots_dir.is_dir():
        return None
    files = sorted((p for p in snapshots_dir.glob("*.json") if p.is_file()), reverse=True)
    for path in files[:_LATEST_SNAPSHOT_FOR_PROJECT_SCAN_LIMIT]:
        existing = _read_json_dict(path)
        if existing is not None and existing.get("project_slug") == project_slug:
            return path
    return None


def _snapshot_age_seconds(ts: str, now: datetime | None = None) -> float | None:
    now = now or datetime.now(timezone.utc)
    try:
        parsed = datetime.strptime(ts, _TS_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (now - parsed).total_seconds()


def _should_skip(new_snapshot: dict, snapshots_dir: Path, min_interval: int) -> bool:
    # Fix #12: compare against the newest snapshot for THIS project, not
    # the globally newest file in snapshots/ -- see
    # _find_latest_snapshot_for_project's docstring.
    latest_path = _find_latest_snapshot_for_project(snapshots_dir, new_snapshot.get("project_slug"))
    if latest_path is None:
        return False
    existing = _read_json_dict(latest_path)
    if existing is None:
        return False
    age = _snapshot_age_seconds(str(existing.get("ts", "")))
    if age is None or age >= min_interval:
        return False
    return existing.get("content_hash") == new_snapshot.get("content_hash")


def snapshot_and_get_path(
    config_dir: Path,
    cwd: str | None,
    min_interval: int = 300,
    stdin_data: dict | None = None,
    managed_path: Path | str | None = None,
) -> tuple[Path | None, bool]:
    """Build a snapshot for ``cwd`` and write it unless idempotency skips
    it. Returns ``(path, written)``: ``path`` is the snapshot that now
    represents current config — either the file just written, or (when
    skipped) the most recent existing one; ``written`` says which. ``path``
    is ``None`` only when the write was skipped and no snapshot exists yet
    (shouldn't happen, since a skip requires a latest snapshot to compare
    against — kept for defensiveness).
    """
    snapshot = build_snapshot(stdin_data or {}, cwd, config_dir, managed_path=managed_path)
    snapshots_dir = _snapshots_dir(config_dir)
    if _should_skip(snapshot, snapshots_dir, min_interval):
        return _find_latest_snapshot_for_project(snapshots_dir, snapshot.get("project_slug")), False

    snapshots_dir.mkdir(parents=True, exist_ok=True)
    # Fix #11: _TS_FORMAT has one-second granularity, so two snapshots
    # written in the same second (concurrent SessionStart hooks, several
    # sessions opened at once) used to collide on the same filename and
    # the later write silently destroyed the earlier one. The
    # content_hash's own first 8 hex characters make the name
    # collision-proof while staying lexicographically sortable after the
    # timestamp (_find_latest_snapshot still just needs the newest name).
    # The write itself is now atomic (temp file + os.replace) so a reader
    # can never observe a torn/partial file mid-write.
    content_hash = snapshot.get("content_hash", "")
    hash_suffix = content_hash.split(":", 1)[-1][:8] if content_hash else "00000000"
    out_path = snapshots_dir / f"{snapshot['ts']}-{hash_suffix}.json"
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(snapshot, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp_path, out_path)
    return out_path, True


# -- install / print-hook (used by the CLI subcommand, not the hook itself) -


def install_hook(config_dir: Path) -> Path:
    """Copy this script into ``<config_dir>/hooks/`` (``config_dir`` is
    the claudeglass directory -- see :func:`resolve_config_dir`). Never
    touches settings.json — pair with ``hook_fragment_text()`` for the
    fragment the user pastes in themselves.
    """
    dest_dir = config_dir / "hooks"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "snapshot-config.py"
    shutil.copy2(Path(__file__).resolve(), dest)
    return dest


#: ROB-P9: a character a double-quoted JSON command string and Claude
#: Code's shell on Windows (Git Bash) cannot both carry safely -- see
#: ``hook_health._quote_for_command``, which this mirrors: this script
#: must stay standalone stdlib, so the two copies can't share code.
_UNSAFE_COMMAND_CHARS = ('"', "$", "`")


def _quote_for_command(path: str) -> str | None:
    """``path`` double-quoted for a hook command, or ``None`` when
    quoting alone can't make it safe: a quote/``$``/backtick, or a UNC
    path (``\\\\server\\share\\...`` would silently collapse to one
    backslash inside a POSIX double-quoted string). A single trailing
    backslash is doubled, which both Windows's argv parsing and a POSIX
    double-quoted string treat as an escaped backslash rather than one
    that escapes the closing quote."""
    if any(ch in path for ch in _UNSAFE_COMMAND_CHARS):
        return None
    if path.startswith("\\\\") or path.startswith("//"):
        return None
    n = len(path) - len(path.rstrip("\\"))
    if n:
        path = path + "\\" * n
    return f'"{path}"'


def hook_command(python: str | None = None, script: Path | None = None, extra_args: str = "") -> str | None:
    """The SessionStart command for this machine: ``python`` (default:
    this interpreter) and ``script`` (default: the installed copy under
    ``<config dir>/hooks``) by their full paths, run with ``-I -S``
    (ROB-P8: isolated mode, no ``site`` import) so this stdlib-only
    script never picks up a ``PYTHON*`` environment variable or a
    ``sitecustomize.py``/``.pth`` file from whatever happens to be on
    this machine. A ``py -3`` or ``%USERPROFILE%`` command fails
    silently when the Python launcher is missing, or when Claude Code
    runs the hook through Git Bash, which does not expand ``%VAR%``.
    The default is the base interpreter when this one runs in a virtual
    environment: this script needs only the standard library, and a venv
    can be deleted or rebuilt. ``extra_args`` (for example
    ``--config-dir "<path>"`` when this tool's data folder is not the
    default) is appended as written. ``None`` (ROB-P9) when the Python
    or the script's own path can't be safely written into a command
    string (:func:`_quote_for_command`) -- the caller's job to refuse
    the hook entry rather than write a broken or unsafe one."""
    base = getattr(sys, "_base_executable", "") or ""
    python = python or (base if base and Path(base).is_file() else sys.executable)
    script = script or (resolve_config_dir(None) / "hooks" / "snapshot-config.py")
    quoted_python = _quote_for_command(python)
    quoted_script = _quote_for_command(str(script))
    if quoted_python is None or quoted_script is None:
        return None
    return f"{quoted_python} -I -S {quoted_script}{extra_args}"


def hook_fragment_text(python: str | None = None, script: Path | None = None, extra_args: str = "") -> str:
    """The settings.json ``hooks`` fragment to paste in, for Windows and
    POSIX, using the commands named in the plan's "Running on other
    people's machines" section (both must exit 0 and print nothing on
    error so a broken Python never blocks a session or blanks the status
    line — this script already guarantees that). The block for the
    platform this runs on is :func:`hook_command`; the other shows the
    general shape. When this machine's own Python or script path can't
    be safely written into a command string (ROB-P9, :func:`hook_command`
    returning ``None``), explains that instead of the fragment.
    """
    native = hook_command(python, script, extra_args)
    if native is None:
        return (
            "Could not build a safe hook command: the Python interpreter's path or "
            f"{script or '(this script)'} holds a quote, $, backtick, or is a UNC path (\\\\server\\share\\...), "
            "none of which can be written into settings.json safely. Move claudeglass's data folder (or "
            "this Python) somewhere with a plain path, then try again."
        )
    if os.name == "nt":
        windows_command = native
        posix_command = 'python3 "$HOME/.claude/claudeglass/hooks/snapshot-config.py"'
    else:
        windows_command = r'"C:\path\to\python.exe" "C:\Users\<you>\.claude\claudeglass\hooks\snapshot-config.py"'
        posix_command = native

    def _fragment(command: str) -> str:
        return json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {"hooks": [{"type": "command", "command": command, "async": True}]}
                    ]
                }
            },
            indent=2,
        )

    return (
        "Merge this into the \"hooks\" key of ~/.claude/settings.json\n"
        "(SessionStart may already have other entries — add to the list,\n"
        "don't replace it). Install the script first with --install-hook,\n"
        "or point the command at wherever you copied hooks/snapshot-config.py.\n"
        "\n"
        "Windows:\n"
        f"{_fragment(windows_command)}\n"
        "\n"
        "POSIX (Linux/macOS):\n"
        f"{_fragment(posix_command)}"
    )


# -- CLI entry point ----------------------------------------------------------


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="snapshot-config.py")
    parser.add_argument("--config-dir", default=None)
    parser.add_argument("--cwd", default=None, help="override stdin's cwd")
    parser.add_argument(
        "--print", action="store_true", dest="print_only",
        help="print the snapshot JSON instead of writing it",
    )
    parser.add_argument("--min-interval", type=int, default=300)
    parser.add_argument(
        "--managed-path", default=None,
        help="override the platform managed-settings.json path (testing, or a "
        "non-standard policy location)",
    )
    return parser.parse_args(argv)


def _run(args: argparse.Namespace) -> None:
    stdin_data = _load_stdin_json()
    config_dir = resolve_config_dir(args.config_dir)

    if args.print_only:
        snapshot = build_snapshot(stdin_data, args.cwd, config_dir, managed_path=args.managed_path)
        print(json.dumps(snapshot, indent=2, sort_keys=True))
        return

    snapshot_and_get_path(
        config_dir,
        args.cwd or _project_dir(stdin_data.get("cwd")),
        min_interval=args.min_interval,
        stdin_data=stdin_data,
        managed_path=args.managed_path,
    )


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    # Fix #17: this file's own module docstring promises a SessionStart
    # hook "always exits 0", but an unrecognised flag used to let
    # argparse's SystemExit(2) straight through, contradicting it -- a
    # hook fragment is a string in settings.json that Claude Code (or a
    # typo) could one day invoke with an extra argument. Running
    # interactively (stdin is a TTY, e.g. `--help` or a genuine usage
    # typo at a terminal) keeps argparse's normal exit-code behaviour;
    # only the non-interactive (hook) path is coerced to 0.
    interactive = sys.stdin.isatty()
    try:
        args = _parse_args(argv)
    except SystemExit as exc:
        if interactive or exc.code in (0, None):
            raise
        return 0
    # Everything below this point is a session-start failure mode and
    # must never propagate as a non-zero exit.
    try:
        _run(args)
    except Exception as exc:  # noqa: BLE001 - must never fail a session start
        print(f"snapshot-config: {exc}", file=sys.stderr)
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())

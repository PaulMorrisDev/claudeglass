"""Applying a profile to a real project (plan Milestone v0.3's ``apply``
bullet): "host-side only. ``--dry-run`` prints the exact diff. Real apply
backs up every touched file to ``~/.claude/claudeglass/backups/<ts>/``,
writes the settings overlay to ``~/.claude/settings.json`` (or
``.claude/settings.local.json`` with ``--project``), patches agent
frontmatter in place, writes a snapshot stamped with ``profile_id``, and
prints how to revert (``apply --revert <ts>``)."

This module is the one place in the package that actually touches a
project's or a user's real files -- ``schema.py``/``diff.py``/
``catalogue.py`` are all pure/filesystem-free by design (see their own
module docstrings); this is deliberately the exception, since applying a
profile is inherently a filesystem-writing operation.

Fix B4: the dry-run text used to be ``diff.py``'s ``diff_against_effective``/
``render_unified_diff`` -- a snapshot-vs-profile computation, independent
of whatever :func:`execute` would actually merge into the *current*
target file. With no snapshot (a supported configuration -- see
:func:`plan_apply`'s own docstring), or merely a stale one, that
rendering showed ``(unset)``/a superseded value for a key the real file
already held, so the one preview a user has before writing was wrong
about what would be overwritten. :func:`render_plan_diff` now renders
the dry-run text directly from :func:`plan_apply`'s own ``actions`` --
each one already carries the real ``old_bytes`` :func:`execute` read
from the target file and the exact ``new_bytes`` it would write -- so
the preview and the write are provably the same bytes, not two
independently-maintained computations that could drift apart. ``diff.py``'s
functions remain the right tool for the service's ``GET
/api/profiles/<id>/diff`` (which has no target file to read, only a
snapshot's effective config), and are unchanged here.

Enterprise-use safety (plan "Enterprise use" section, Risks item 5): a
project-scoped write (``project-local`` or ``repo``) is refused when its
target file is already tracked by git, unless ``allow_tracked=True`` is
given -- this covers both a project's ``.claude/settings.local.json``/
``.claude/settings.json`` *and* every ``.claude/agents/<name>.md`` file a
profile would patch, since an agent frontmatter file is exactly as likely
to be shared with colleagues as ``settings.json`` is (the plan's own risk
text names both). A managed-settings key is never written regardless of
scope or flags -- :func:`plan_apply` drops it from the write plan
entirely (the same exclusion :mod:`diff` already renders as a "managed by
policy" note).

Deviations from the plan/brief, reported rather than made silently (see
``schema.py``'s module docstring for this project's convention on this):

- **``snapshots.py`` has no ``effective_agents()`` accessor**, even
  though ``diff.py``'s own docstring refers to "``snapshots.effective_agents``'
  per-agent summary" as if one exists. Both ``schema.py`` and ``diff.py``
  are outside this work package's writable paths, so rather than add the
  missing function there, :func:`plan_apply` reads the schema-2
  ``effective_agents`` field directly off the caller's
  ``snapshots.Snapshot.data`` dict (exactly the shape
  ``hooks/snapshot-config.py`` writes it in, per
  ``docs/config-layers.md``'s own "``effective_agents``" section) --
  ``snapshot.data.get("effective_agents", {})``.
- **The snapshot :func:`execute` stamps is a minimal marker, not a full
  schema-2 capture.** Running the real ``hooks/snapshot-config.py`` hook
  from inside this module would mean either shelling out to a script this
  package only ever *loads by path* from the CLI's own snapshot-config
  command (``cli._load_snapshot_hook_module``) or duplicating hundreds of
  lines of redaction logic that script deliberately keeps standalone.
  Instead, :func:`execute` writes a small ``{"ts", "schema_version":
  2, "profile_id"}`` document to ``<config_dir>/snapshots/<ts>.json`` --
  a real, loadable schema-2 snapshot file (every accessor in
  ``snapshots.py`` degrades an absent field to ``{}``/``[]`` rather than
  raising, so this loads cleanly), just a narrower one than the hook's
  own next run will produce. The next real hook run (which already reads
  ``<config_dir>/active-profile``, written by this same :func:`execute`)
  naturally supersedes it with the full capture. Readers that need
  config skip the stamp (``snapshots.records_config``).
- **``--force`` recreates a missing agent frontmatter file from
  scratch.** Patching requires existing text to parse (frontmatter.py's
  own "refuse rather than guess" contract has nothing to patch without a
  file), so by default a profile agent key with no corresponding
  ``<name>.md`` file at the resolved scope is a blocked/refused apply
  (see "project-local agent refusal" in the work package's test list).
  ``force=True`` is the documented escape hatch: it writes a brand-new
  ``---\\n<key>: value\\n...\\n---\\n`` frontmatter block (no body) instead
  of refusing. This is the one behaviour ``--force`` controls in this
  module; it has no effect on the git-tracked-file refusal (that is
  ``--allow-tracked``'s job specifically) or on anything else.
- **COV-07/COV-11 supersedes the earlier "an env var is never written to
  any file" reading of plan Appendix A7.** ``profile.env`` used to be
  print-only guidance (``env_lines_for_profile``: "the caller prints
  these lines and the user exports them in their own shell"), on the
  reasoning that ``apply``'s own Milestone v0.3 quote above only names a
  "settings overlay" and "agent frontmatter" as things it writes.
  ``docs/config-layers.md``'s "P7b note" resolves this explicitly for
  the five ``env-*`` recommendation rules -- "it needs ``apply.py``
  support for ``--set env.NAME=value`` (or an equivalent)" -- and the
  same env block is what ``hooks/snapshot-config.py``'s
  ``effective_env_provenance`` already reads its layers from, so
  settings.json's ``"env"`` object was always the real, persisted
  mechanism; the print-only path just hadn't caught up. A profile's
  ``env`` entries are now merged into the target settings file's own
  ``"env"`` object exactly like any other settings key -- same backup,
  revert, git-tracked-file refusal and managed-key exclusion as
  everything else this module writes.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import subprocess
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .. import snapshots as snapshots_mod
from .frontmatter import FrontmatterError, parse_frontmatter, patch_frontmatter
from .schema import SETTINGS_ALLOWLIST, Profile

__all__ = [
    "ApplyError",
    "FileAction",
    "ApplyPlan",
    "ApplyResult",
    "RevertResult",
    "BackupInfo",
    "plan_apply",
    "execute",
    "revert",
    "list_backups",
    "write_launch_overlay",
    "render_plan_diff",
    "action_changes",
    "explain_plan",
]

_VALID_SCOPES = ("user", "project-local", "repo")

#: Compact UTC timestamp, matching ``hooks/snapshot-config.py``'s own
#: ``_TS_FORMAT`` / ``snapshots._HOOK_TS_FORMAT`` so a stamp this module
#: writes sorts and parses exactly like a real hook snapshot.
_TS_FORMAT = "%Y%m%dT%H%M%SZ"

_ACTIVE_PROFILE_FILENAME = "active-profile"
#: Written into a backup folder by :func:`revert`, so the change reads as undone.
REVERTED_FILENAME = "reverted.json"

#: COV-04: each ``_VALID_SCOPES`` value's ``snapshots.SETTINGS_LAYER_NAMES``
#: counterpart -- "managed" has no scope here (a managed key is never
#: written by this module regardless of ``scope``, see ``plan_apply``'s
#: ``skipped_managed`` exclusion above).
_SCOPE_LAYER = {"user": "user", "project-local": "project_local", "repo": "project_shared"}

#: Readable label for a ``snapshots.SETTINGS_LAYER_NAMES`` layer, for the
#: "already overridden" warning below -- same file/who-it-affects text as
#: ``fixes._SETTINGS_WHERE``/``PROFILE_SCOPE_WHERE``, duplicated rather
#: than imported (this module is host-side/CLI machinery, ``fixes.py`` is
#: the plain-language dashboard layer -- see this module's own docstring
#: on why it doesn't reach into another module's presentation text).
_LAYER_LABEL = {
    "managed": "your organisation's managed settings",
    "project_local": "this project's local settings (.claude/settings.local.json)",
    "project_shared": "this project's shared settings (.claude/settings.json)",
    "user": "your user settings (~/.claude/settings.json)",
}


class ApplyError(Exception):
    """An apply/revert operation was refused rather than attempted --
    e.g. a git-tracked target file without ``--allow-tracked``, a missing
    project agent file without ``--force``, or an unknown ``--revert``
    timestamp. ``reasons`` carries every refusal message at once (same
    convention as ``schema.ProfileError.problems``)."""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = list(reasons)
        super().__init__("; ".join(self.reasons) if self.reasons else "apply refused")


@dataclass(frozen=True, slots=True)
class FileAction:
    """One file :func:`execute` will back up (if it already exists) and
    then overwrite. ``old_bytes`` is ``None`` when the file does not
    exist yet -- :func:`revert` deletes it in that case rather than
    restoring empty content."""

    kind: str  # "settings" | "agent_frontmatter" | "active_profile"
    path: Path
    old_bytes: bytes | None
    new_bytes: bytes
    tracked: bool
    agent_name: str | None = None


@dataclass(frozen=True, slots=True)
class ApplyPlan:
    """Everything :func:`plan_apply` resolved before touching disk:
    which files would be written and with what content, which rows were
    dropped because a managed key governs them, the dry-run diff text,
    and any reason the plan would be refused (:func:`execute` raises
    :class:`ApplyError` with exactly these reasons rather than writing
    anything when ``blocked`` is non-empty). A profile's ``env`` entries
    are folded into ``actions`` like any other settings key (COV-07/
    COV-11, see the module docstring's deviation note) -- there is no
    separate env-only field here any more.

    COV-04: ``overridden`` names every settings key this plan would
    write whose *effective* value today is already supplied by a layer
    with higher precedence than ``scope`` (``snapshots.effective_provenance``,
    via ``snapshots.SETTINGS_LAYER_NAMES``'s precedence order) -- writing
    it would change the file on disk but not what Claude Code actually
    uses, since the higher layer keeps winning. Never blocks the apply
    (the write still happens; it may be exactly what a user wants ready
    for when the higher layer's override is later removed) -- just a
    warning :func:`explain_plan`/``cli._cmd_apply`` surface alongside the
    diff. Empty when ``snapshot`` was ``None`` (nothing to compare
    against) or the plan writes no settings keys with known provenance."""

    profile_id: str
    scope: str
    config_dir: Path
    actions: tuple[FileAction, ...]
    skipped_managed: tuple[str, ...]
    diff_text: str
    blocked: tuple[str, ...] = ()
    overridden: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ApplyResult:
    """What :func:`execute` actually did."""

    profile_id: str
    scope: str
    ts: str
    config_dir: Path
    written: tuple[Path, ...]
    backup_dir: Path | None
    snapshot_path: Path
    active_profile_path: Path


@dataclass(frozen=True, slots=True)
class RevertResult:
    ts: str
    config_dir: Path
    restored: tuple[Path, ...]
    deleted: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class BackupInfo:
    """One row of :func:`list_backups`."""

    ts: str
    profile_id: str
    scope: str
    file_count: int
    #: When :func:`revert` undid it (UTC), or None while it is still in place.
    reverted_at: str | None = None


# -- small filesystem helpers -------------------------------------------


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` via a temp file + ``os.replace`` in the
    same directory (same pattern as ``cache.DigestCache.put``), so a
    crash mid-write never leaves a half-written target for the next
    reader. Creates ``path``'s parent directories first."""
    # A symlinked settings.json (kept in a dotfiles folder, say) is
    # written through, never replaced by a plain file, and a file keeps
    # its permissions.
    path = path.resolve() if path.is_symlink() else path
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=path.suffix or ".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        if path.exists():
            shutil.copymode(path, tmp_name)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _read_bytes_or_none(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _is_git_tracked(path: Path) -> bool:
    """Whether ``path`` is currently tracked by a git repository rooted
    anywhere above it. Returns ``False`` (never raises) when ``path``'s
    directory isn't inside a git repo at all, or when ``git`` itself
    isn't installed -- both degrade to "not tracked" rather than
    blocking an apply that has no way to actually check (a documented
    limitation, not a silent guess: an environment with no git available
    at all is outside what this safety net can cover)."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", str(path)],
            cwd=str(path.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    return result.returncode == 0


def _relative_label(path: Path, base: Path | None) -> str:
    """``path`` rendered relative to ``base`` when possible (``base`` is
    always a path the caller explicitly supplied -- ``project_path`` or
    ``claude_root`` -- so printing it back is not a privacy leak, matching
    this project's "paths explicitly given by the caller print verbatim"
    convention). Falls back to ``path``'s own name when it isn't under
    ``base``."""
    if base is not None:
        try:
            return str(path.relative_to(base))
        except ValueError:
            pass
    return path.name


# -- managed-key exclusion (mirrors diff.py's private helper) ------------
#
# diff.py's _is_agent_key_managed is not exported (this module must not
# modify diff.py -- see the module docstring's constraints), so the same
# small rule is duplicated here rather than imported, matching this
# project's established convention for a helper this short (e.g.
# cli.py's _priced_turns).


def _is_agent_key_managed(key: str, managed_keys: set[str]) -> bool:
    if key in managed_keys or "agents" in managed_keys:
        return True
    if key == "experimental.cacheTtl" and "subagentPromptCacheTtl" in managed_keys:
        return True
    return False


# -- scope resolution ------------------------------------------------------


def _resolve_settings_path(scope: str, project_path: Path | None, claude_root: Path) -> Path:
    if scope == "user":
        return claude_root / "settings.json"
    if project_path is None:
        raise ValueError(f"scope={scope!r} requires project_path")
    if scope == "project-local":
        return project_path / ".claude" / "settings.local.json"
    return project_path / ".claude" / "settings.json"  # "repo"


def _resolve_agents_dir(scope: str, project_path: Path | None, claude_root: Path) -> Path:
    if scope == "user":
        return claude_root / "agents"
    if project_path is None:
        raise ValueError(f"scope={scope!r} requires project_path")
    return project_path / ".claude" / "agents"


# -- planning ---------------------------------------------------------------


def _detect_json_style(existing_bytes: bytes | None) -> tuple[int, str]:
    """The settings file's own indentation width (spaces) and line
    ending, sniffed from its current bytes so rewriting it doesn't
    reformat lines nobody touched (fix N1: every write previously used
    a hardcoded ``indent=2``/``\\n`` regardless of the file's own style,
    so a 4-space or CRLF settings file -- e.g. one shared through a
    Windows-authored dotfiles repo, which S7 now allows apply to write
    to with ``--allow-tracked`` -- turned a one-key change into a
    whole-file reformat diff). Defaults to ``(2, "\\n")`` -- this
    project's own convention, and the previous hardcoded behaviour --
    when there is no existing file, or nothing in it reveals an indent
    (e.g. ``{}``)."""
    if not existing_bytes:
        return 2, "\n"
    line_ending = "\r\n" if b"\r\n" in existing_bytes else "\n"
    indent = 2
    for raw_line in existing_bytes.split(b"\n"):
        stripped_line = raw_line.rstrip(b"\r")
        content = stripped_line.lstrip(b" ")
        leading = len(stripped_line) - len(content)
        if leading > 0 and content:
            indent = leading
            break
    return indent, line_ending


def _render_settings_json(*, existing_bytes: bytes | None, merged: dict) -> bytes:
    """``merged`` serialised to match ``existing_bytes``'s own
    indentation/line-ending style (see :func:`_detect_json_style`, fix
    N1), so a single-key change doesn't reformat the rest of a shared or
    git-tracked settings file."""
    indent, line_ending = _detect_json_style(existing_bytes)
    text = json.dumps(merged, indent=indent) + "\n"
    if line_ending != "\n":
        text = text.replace("\n", line_ending)
    return text.encode("utf-8")


def render_plan_diff(actions: tuple[FileAction, ...], *, base: Path | None) -> str:
    """A true unified diff of every non-``active_profile`` action's
    ``old_bytes`` -> ``new_bytes`` (fix B4) -- the exact bytes
    :func:`execute` would write, decoded as UTF-8 text (best-effort: an
    undecodable byte is substituted rather than raising, since this is
    display-only) and compared line by line with
    :func:`difflib.unified_diff`. See the module docstring's B4 note for
    why this replaced a snapshot-based rendering. The ``active_profile``
    marker is internal bookkeeping (see :func:`plan_apply`), not a file
    a user asked to change, so it never appears here -- matching the
    previous diff text's own scope.

    A file with no ``old_bytes`` (:func:`execute` will create it, e.g.
    ``--force`` on a missing agent file) diffs against an empty "does
    not exist yet" baseline rather than being skipped, so the preview
    still shows what it will contain. ``base``, when given, is used the
    same way :func:`_relative_label` already uses it elsewhere in this
    module -- to print a path relative to a directory the caller
    explicitly supplied, per this project's "paths given by the caller
    print verbatim" convention -- rather than an absolute path.

    ``difflib.unified_diff``'s own ``@@ -a,b +c,d @@`` hunk-position
    header lines are dropped: this is a preview, never fed back in as a
    patch, so the position info has no use here, and this project's own
    privacy convention (``tests/helpers.py``'s ``assert_privacy``) flags
    any bare ``@`` as email-shaped -- keeping the header would make
    every non-trivial dry-run diff fail that check for a false reason.
    """
    blocks: list[str] = []
    for action in actions:
        if action.kind == "active_profile":
            continue
        label = _relative_label(action.path, base)
        from_label = f"{label} (does not exist yet)" if action.old_bytes is None else label
        old_text = (action.old_bytes or b"").decode("utf-8", errors="replace")
        new_text = action.new_bytes.decode("utf-8", errors="replace")
        diff_lines = [
            line
            for line in difflib.unified_diff(
                old_text.splitlines(), new_text.splitlines(), fromfile=from_label, tofile=label, lineterm=""
            )
            if not line.startswith("@@")
        ]
        if diff_lines:
            blocks.append("\n".join(diff_lines))
    return ("\n\n".join(blocks) + "\n") if blocks else ""


def plan_apply(
    profile: Profile,
    *,
    scope: str,
    project_path: str | Path | None,
    config_dir: str | Path,
    claude_root: str | Path,
    snapshot: "snapshots_mod.Snapshot | None" = None,
    allow_tracked: bool = False,
    force: bool = False,
    mark_active: bool = True,
) -> ApplyPlan:
    """Resolve every file :func:`execute` would touch for applying
    ``profile`` at ``scope``, without writing anything.

    ``scope`` is one of ``"user"`` (``<claude_root>/settings.json`` +
    ``<claude_root>/agents/``), ``"project-local"``
    (``<project_path>/.claude/settings.local.json`` +
    ``<project_path>/.claude/agents/``), or ``"repo"``
    (``<project_path>/.claude/settings.json`` + the same agents dir --
    an agent's frontmatter file is not itself scope-specific, see
    ``diff.py``'s own note that "a per-agent row always renders against
    ``.claude/agents/<name>.md`` regardless of scope"). ``project_path``
    is required for ``"project-local"``/``"repo"`` and ignored for
    ``"user"``.

    ``claude_root`` is the directory that directly holds ``settings.json``
    and ``agents/`` -- ``~/.claude``, or ``$CLAUDE_CONFIG_DIR`` when that
    env var moves the whole tree -- **not** ``config_dir`` and not
    necessarily an ancestor of it: ``config_dir`` (this tool's own
    ``claudeglass`` directory) can be pointed anywhere via
    ``--config-dir``/``config.toml``, independently of where the real
    Claude Code config lives (fix B3). The caller resolves this
    explicitly (``cli._resolve_claude_root``) rather than this module
    deriving one from the other.

    ``snapshot``, when given, supplies the "current" side of the dry-run
    diff and the managed-key exclusion (via ``snapshots.effective_config``/
    ``effective_provenance``/``managed_keys``, plus this module's own read
    of the schema-2 ``effective_agents`` field -- see the module
    docstring's deviation note). ``None`` computes the diff against an
    empty "nothing currently set" baseline and excludes no keys as
    managed -- a real ``apply`` invocation always has a snapshot; tests
    exercising a narrower scenario do not have to construct one.

    Every settings/agent-frontmatter key a managed-settings layer
    governs is silently dropped from the write plan (never blocked --
    there's nothing wrong with the rest of the apply) and named in
    ``skipped_managed`` instead. A ``FileAction`` whose target is
    already git-tracked -- at any scope, user included: a ``~/.claude``
    kept in a dotfiles repository is exactly as real as a project's own
    tracked ``.claude/`` (fix S7) -- is kept in ``actions`` (so a caller
    can still show what *would* be written) but also names itself in
    ``blocked`` unless ``allow_tracked=True`` -- :func:`execute` refuses
    to write anything at all when ``blocked`` is non-empty. Likewise, a
    profile agent key whose target ``<name>.md`` file does not exist is
    blocked unless ``force=True`` (see the module docstring).

    ``diff_text`` (fix B4) is rendered from the plan's own ``actions`` --
    a true unified diff of each touched file's real current bytes versus
    what :func:`execute` would write -- never from ``snapshot``, which
    can be absent or stale; see :func:`render_plan_diff`.
    """
    if scope not in _VALID_SCOPES:
        raise ValueError(f"unknown scope: {scope!r} (expected one of {_VALID_SCOPES})")

    config_dir = Path(config_dir)
    claude_root = Path(claude_root)
    project_path = Path(project_path) if project_path is not None else None
    if scope != "user" and project_path is None:
        raise ValueError(f"scope={scope!r} requires project_path")

    if snapshot is not None:
        effective = snapshots_mod.effective_config(snapshot)
        provenance = snapshots_mod.effective_provenance(snapshot)
        managed_keys = set(snapshots_mod.managed_keys(snapshot))
        # Deviation (see module docstring): snapshots.py has no
        # effective_agents() accessor, so this reads the schema-2 field
        # straight off the snapshot's own data dict.
        raw_effective_agents = snapshot.data.get("effective_agents")
        effective_agents = dict(raw_effective_agents) if isinstance(raw_effective_agents, dict) else {}
    else:
        effective, provenance, managed_keys, effective_agents = {}, {}, set(), {}

    settings_path = _resolve_settings_path(scope, project_path, claude_root)
    agents_dir = _resolve_agents_dir(scope, project_path, claude_root)

    actions: list[FileAction] = []
    skipped_managed: list[str] = []
    blocked: list[str] = []
    claude_root_or_project = project_path if project_path is not None else claude_root

    # -- settings overlay --
    settings_changes = {k: v for k, v in profile.settings.items() if k not in managed_keys}
    skipped_managed += [f"settings.{k}" for k in profile.settings if k in managed_keys]
    # COV-07/COV-11: a profile's env entries are just another settings.json
    # key ("env" itself) as far as writing goes -- merged by name into
    # whatever the target file's own "env" object already holds, same as
    # any other map-kind settings key below. Managed-settings.json mirrors
    # settings.json's own shape, so "is env managed" is a whole-key
    # question here (managed_keys is top-level key names only -- there is
    # no per-env-var-name managed signal at this layer, matching how a
    # single settings key like "model" is all-or-nothing too).
    env_managed = "env" in managed_keys
    env_changes = {} if env_managed else dict(profile.env)
    if env_managed:
        skipped_managed += [f"env.{name}" for name in profile.env]
    if settings_changes or env_changes:
        old_bytes = _read_bytes_or_none(settings_path)
        try:
            existing = json.loads(old_bytes.decode("utf-8")) if old_bytes else {}
            if not isinstance(existing, dict):
                raise ValueError("top-level value is not an object")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ApplyError([f"{settings_path}: cannot parse as JSON ({exc})"]) from None
        merged = dict(existing)
        for key, value in settings_changes.items():
            spec = SETTINGS_ALLOWLIST.get(key)
            if spec is not None and spec.kind.startswith("map[") and isinstance(existing.get(key), dict):
                # Merge by name: change only the entries the profile names.
                merged[key] = {**existing[key], **value}
            else:
                merged[key] = value
        if env_changes:
            existing_env = existing.get("env")
            merged["env"] = {**(existing_env if isinstance(existing_env, dict) else {}), **env_changes}
        new_bytes = _render_settings_json(existing_bytes=old_bytes, merged=merged)
        if new_bytes != (old_bytes or b""):
            # Fix S7: checked regardless of scope -- a user-scope
            # ~/.claude can be a dotfiles repo just as easily as a
            # project's .claude/ can be shared, and _is_git_tracked
            # never raises (it degrades to "not tracked" outside any
            # git repo, or when git itself isn't installed).
            tracked = _is_git_tracked(settings_path)
            if tracked and not allow_tracked:
                blocked.append(
                    f"{_relative_label(settings_path, claude_root_or_project)} is tracked by git; "
                    "pass --allow-tracked to write it anyway"
                )
            actions.append(
                FileAction(kind="settings", path=settings_path, old_bytes=old_bytes, new_bytes=new_bytes, tracked=tracked)
            )

    # -- per-agent frontmatter --
    for agent_name in sorted(profile.agents):
        agent_settings = profile.agents[agent_name]
        changes = {k: v for k, v in agent_settings.items() if not _is_agent_key_managed(k, managed_keys)}
        skipped_managed += [
            f"agents.{agent_name}.{k}" for k in agent_settings if _is_agent_key_managed(k, managed_keys)
        ]
        if not changes:
            continue

        agent_path = agents_dir / f"{agent_name}.md"
        old_bytes = _read_bytes_or_none(agent_path)
        if old_bytes is None:
            if not force:
                blocked.append(
                    f"no agent file found for {agent_name!r} at "
                    f"{_relative_label(agent_path, claude_root_or_project)}; apply does not create a "
                    "new agent file (pass --force to create one from scratch)"
                )
                continue
            new_text = _new_frontmatter_text(changes)
            new_bytes = new_text.encode("utf-8")
        else:
            try:
                old_text = old_bytes.decode("utf-8")
                new_text = patch_frontmatter(old_text, changes)
            except (UnicodeDecodeError, FrontmatterError) as exc:
                raise ApplyError([f"{agent_path}: {exc}"]) from None
            new_bytes = new_text.encode("utf-8")

        if new_bytes == (old_bytes or b""):
            continue
        # Fix S7: see the settings-overlay check above -- same
        # every-scope rule applies to agent frontmatter files.
        tracked = _is_git_tracked(agent_path)
        if tracked and not allow_tracked:
            blocked.append(
                f"{_relative_label(agent_path, claude_root_or_project)} is tracked by git; "
                "pass --allow-tracked to write it anyway"
            )
        actions.append(
            FileAction(
                kind="agent_frontmatter",
                path=agent_path,
                old_bytes=old_bytes,
                new_bytes=new_bytes,
                tracked=tracked,
                agent_name=agent_name,
            )
        )

    # -- active-profile marker (config_dir is this tool's own directory,
    # never git-tracked in practice, so no tracked-file check applies) --
    active_path = config_dir / _ACTIVE_PROFILE_FILENAME
    old_active = _read_bytes_or_none(active_path)
    new_active = (profile.id + "\n").encode("utf-8")
    # A one-off ``apply --set`` change isn't a profile, so it leaves the
    # active-profile marker alone (mark_active=False).
    if mark_active and new_active != (old_active or b""):
        actions.append(
            FileAction(kind="active_profile", path=active_path, old_bytes=old_active, new_bytes=new_active, tracked=False)
        )

    # COV-04: a key this plan would write whose effective value already
    # comes from a layer with higher precedence than where we're about
    # to write it -- the write still happens (it isn't wrong, and may be
    # exactly what someone wants staged for when the higher layer's
    # override goes away), but it would have no visible effect today,
    # which is worth saying up front rather than leaving a user to
    # wonder why nothing changed after a real apply. Covers env entries
    # too now that they're a real write (COV-07/COV-11) -- their
    # provenance lives in a different snapshot field
    # (``effective_env_provenance``, not ``effective_provenance``, see
    # ``recommend.py``'s ``_env_lever_scope``), so it's checked
    # separately from ``settings_changes``.
    overridden: list[str] = []
    target_layer = _SCOPE_LAYER.get(scope)
    if target_layer is not None:
        precedence = {name: i for i, name in enumerate(snapshots_mod.SETTINGS_LAYER_NAMES)}
        target_rank = precedence[target_layer]
        for key in sorted(settings_changes):
            layer = provenance.get(key)
            if layer is None or layer == target_layer or layer not in precedence:
                continue
            if precedence[layer] < target_rank:
                overridden.append(f"{key}: already set by {_LAYER_LABEL.get(layer, layer)}, which takes precedence")
        env_provenance = snapshot.data.get("effective_env_provenance") if snapshot is not None else None
        if env_changes and isinstance(env_provenance, dict):
            for name in sorted(env_changes):
                layer = env_provenance.get(name)
                if layer is None or layer == target_layer or layer not in precedence:
                    continue
                if precedence[layer] < target_rank:
                    overridden.append(
                        f"env.{name}: already set by {_LAYER_LABEL.get(layer, layer)}, which takes precedence"
                    )

    diff_text = render_plan_diff(tuple(actions), base=claude_root_or_project)

    return ApplyPlan(
        profile_id=profile.id,
        scope=scope,
        config_dir=config_dir,
        actions=tuple(actions),
        skipped_managed=tuple(sorted(set(skipped_managed))),
        diff_text=diff_text,
        blocked=tuple(blocked),
        overridden=tuple(overridden),
    )


def _new_frontmatter_text(changes: dict) -> str:
    """A brand-new ``---``-fenced frontmatter document holding exactly
    ``changes`` (``--force``'s "create a missing agent file" path -- see
    the module docstring). Dotted ``"experimental.cacheTtl"`` keys are
    grouped into nested form, matching ``patch_frontmatter``'s own
    default when a file has no existing form to follow."""
    top: dict[str, object] = {}
    nested: dict[str, dict[str, object]] = {}
    for key, value in changes.items():
        if "." in key:
            parent, _, child = key.partition(".")
            nested.setdefault(parent, {})[child] = value
        else:
            top[key] = value

    lines = ["---"]
    for key, value in top.items():
        if isinstance(value, list):
            rendered = "[" + ", ".join(_frontmatter_scalar(v) for v in value) + "]"
        else:
            rendered = _frontmatter_scalar(value)
        lines.append(f"{key}: {rendered}")
    for parent, children in nested.items():
        lines.append(f"{parent}:")
        for child, value in children.items():
            lines.append(f"  {child}: {_frontmatter_scalar(value)}")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def _frontmatter_scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if not text or any(ch in text for ch in ':#"\'[]{}') or text != text.strip():
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


# -- what changes, in words -------------------------------------------------


def _flatten(value: dict, prefix: str = "") -> dict:
    """``{"a": {"b": 1}}`` -> ``{"a.b": 1}``: settings keys as the
    allowlist and the frontmatter parser name them."""
    out: dict = {}
    for key, item in value.items():
        name = f"{prefix}{key}"
        if isinstance(item, dict) and item:
            out.update(_flatten(item, name + "."))
        else:
            out[name] = item
    return out


def _parsed(action: FileAction, data: bytes | None) -> dict:
    if data is None:
        return {}
    text = data.decode("utf-8", errors="replace")
    try:
        if action.kind == "settings":
            loaded = json.loads(text) if text.strip() else {}
            return _flatten(loaded) if isinstance(loaded, dict) else {}
        if action.kind == "agent_frontmatter":
            return parse_frontmatter(text)
    except (json.JSONDecodeError, FrontmatterError):
        return {}
    return {}


def action_changes(action: FileAction) -> list[dict]:
    """The keys ``action`` changes, each as ``{"key", "agent", "old",
    "new"}`` (``None``: not set). Recorded in the backup manifest and
    used by :func:`explain_plan`; the active-profile marker has none."""
    if action.kind not in ("settings", "agent_frontmatter"):
        return []
    before = _parsed(action, action.old_bytes)
    after = _parsed(action, action.new_bytes)
    changes = []
    for key in sorted(set(before) | set(after)):
        if before.get(key) != after.get(key):
            changes.append({"key": key, "agent": action.agent_name, "old": before.get(key), "new": after.get(key)})
    return changes


#: ``scope`` in plain words, for :func:`explain_plan`.
_SCOPE_WORDS = {
    "user": "your user settings, used in every project",
    "project-local": "this project, on your machine only",
    "repo": "this project's shared settings, used by everyone who works in it",
}


def _words(value) -> str:
    if value is None:
        return "not set"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value) if value else "(empty list)"
    return str(value)


def explain_plan(plan: ApplyPlan) -> list[str]:
    """What applying ``plan`` changes, in plain words: per key, what it
    controls, now and after, where and who it affects, the trade-off,
    and how to undo it. ``apply`` prints this before its diff (dry run)
    and before writing."""
    from ..fixes import SETTING_TEXT

    lines: list[str] = []
    for action in plan.actions:
        changes = action_changes(action)
        if not changes:
            continue
        if action.kind == "agent_frontmatter":
            where = f"{action.path} (the {action.agent_name} agent's file)"
        else:
            where = f"{action.path} ({_SCOPE_WORDS.get(plan.scope, plan.scope)})"
        for change in changes:
            subject = f"{change['key']} for {change['agent']}" if change["agent"] else change["key"]
            what, tradeoff, caveat = SETTING_TEXT.get(change["key"], ("", "", ""))
            lines.append(f"Change: {subject}")
            if what:
                lines.append(f"  What it controls: {what}")
            lines.append(f"  Now: {_words(change['old'])}. After: {_words(change['new'])}.")
            lines.append(f"  Where: {where}")
            notes = " ".join(t for t in (tradeoff, caveat) if t)
            if notes:
                lines.append(f"  Trade-off: {notes}")
            lines.append("  Undo: run the apply --revert command printed after the change is made.")
    return lines


def _sha256(data: bytes | None) -> str | None:
    return hashlib.sha256(data).hexdigest() if data is not None else None


# -- executing / reverting ---------------------------------------------------


def execute(plan: ApplyPlan, *, config_dir: str | Path) -> ApplyResult:
    """Write every action in ``plan``, after first backing up whatever
    each target already held. Raises :class:`ApplyError` (writing
    nothing) when ``plan.blocked`` is non-empty -- callers should check
    ``plan.blocked`` themselves before deciding to call this at all, but
    :func:`execute` re-checks so a plan built with ``allow_tracked=False``
    can never be executed through some other path that forgot to check.

    Every touched file's pre-image is written verbatim (byte for byte)
    under ``<config_dir>/backups/<ts>/files/`` before the new content is
    written, with a ``manifest.json`` recording which backup file (or
    ``null``, meaning "did not exist") corresponds to which target --
    :func:`revert` replays this manifest to restore the exact prior
    state. A file whose pre-image is ``None`` is removed by
    :func:`revert` rather than emptied. Every write is atomic (temp file
    + ``os.replace``, see :func:`_atomic_write_bytes`) so a crash
    mid-apply can never leave a target file half-written.
    """
    config_dir = Path(config_dir)
    if plan.blocked:
        raise ApplyError(list(plan.blocked))
    # Asked before writing: a file changed while the question waited
    # (Claude Code writes settings.json too) is never overwritten.
    changed = [str(action.path) for action in plan.actions if _read_bytes_or_none(action.path) != action.old_bytes]
    if changed:
        raise ApplyError([
            f"{path} changed after this change was worked out, so nothing was written; run the command again"
            for path in changed
        ])

    # _TS_FORMAT only has second resolution, so two applies within the
    # same UTC second (a real risk for scripted/back-to-back applies,
    # not just a test artefact) would otherwise collide on the same
    # backup directory -- the second apply's manifest and backup files
    # would silently overwrite the first's, corrupting that first
    # apply's revert. A numeric suffix disambiguates rather than
    # guessing the collision away: "<ts>-2", "<ts>-3", ... -- each
    # still sorts immediately after its bare "<ts>" (a longer string
    # with that exact prefix always compares greater), so
    # list_backups' ascending-by-ts order is unaffected.
    base_ts = datetime.now(timezone.utc).strftime(_TS_FORMAT)
    ts = base_ts
    backup_dir = config_dir / "backups" / ts
    suffix = 2
    while backup_dir.exists():
        ts = f"{base_ts}-{suffix}"
        backup_dir = config_dir / "backups" / ts
        suffix += 1
    files_dir = backup_dir / "files"

    manifest_entries = []
    written: list[Path] = []
    for i, action in enumerate(plan.actions):
        backup_rel = None
        if action.old_bytes is not None:
            backup_rel = f"{i:04d}.bak"
            _atomic_write_bytes(files_dir / backup_rel, action.old_bytes)
        _atomic_write_bytes(action.path, action.new_bytes)
        written.append(action.path)
        manifest_entries.append(
            {
                "kind": action.kind,
                "path": str(action.path),
                "backup": backup_rel,
                "agent_name": action.agent_name,
                # What changed, and the written file's hash, so revert
                # can tell when the file was edited afterwards.
                "changes": action_changes(action),
                "new_sha256": _sha256(action.new_bytes),
            }
        )

    manifest = {
        "ts": ts,
        "profile_id": plan.profile_id,
        "scope": plan.scope,
        "entries": manifest_entries,
    }
    manifest_path = backup_dir / "manifest.json"
    _atomic_write_bytes(manifest_path, (json.dumps(manifest, indent=2) + "\n").encode("utf-8"))

    snapshot_path = config_dir / "snapshots" / f"{ts}.json"
    stamp = {"ts": ts, "schema_version": 2, "profile_id": plan.profile_id}
    _atomic_write_bytes(snapshot_path, (json.dumps(stamp, indent=2) + "\n").encode("utf-8"))

    active_profile_path = config_dir / _ACTIVE_PROFILE_FILENAME

    return ApplyResult(
        profile_id=plan.profile_id,
        scope=plan.scope,
        ts=ts,
        config_dir=config_dir,
        written=tuple(written),
        backup_dir=backup_dir if manifest_entries else None,
        snapshot_path=snapshot_path,
        active_profile_path=active_profile_path,
    )


def revert(ts: str, *, config_dir: str | Path, ignore_changes: bool = False) -> RevertResult:
    """Undo exactly the writes :func:`execute` made for backup ``ts``:
    restore each entry's pre-image byte for byte, or delete the target
    when its pre-image was ``None`` (it did not exist before that
    apply). Raises :class:`ApplyError` if no backup manifest exists for
    ``ts``. Never touches the snapshot stamp :func:`execute` wrote
    (snapshots accumulate as a history, the same convention every other
    snapshot in this project follows -- reverting a settings change
    doesn't erase the historical record that it happened).

    A target whose content no longer matches the hash recorded when it
    was written (edited since, by you or Claude Code) is refused with
    :class:`ApplyError`, restoring nothing, unless ``ignore_changes``
    is given: restoring the backup would silently discard those edits.
    Manifests written before hashes were recorded skip this check."""
    config_dir = Path(config_dir)
    manifest_path = config_dir / "backups" / ts / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except OSError:
        raise ApplyError([f"no backup found for {ts!r} (looked for {manifest_path})"]) from None
    except json.JSONDecodeError as exc:
        raise ApplyError([f"{manifest_path}: cannot parse manifest ({exc})"]) from None

    files_dir = config_dir / "backups" / ts / "files"
    if not ignore_changes:
        edited = [
            entry["path"]
            for entry in manifest.get("entries", [])
            if "new_sha256" in entry and _sha256(_read_bytes_or_none(Path(entry["path"]))) != entry["new_sha256"]
        ]
        if edited:
            raise ApplyError(
                [f"{path} changed after this apply; reverting would discard those edits" for path in edited]
                + ["run again with --ignore-changes to restore the backup anyway"]
            )
    restored: list[Path] = []
    deleted: list[Path] = []
    for entry in manifest.get("entries", []):
        target = Path(entry["path"])
        backup_rel = entry.get("backup")
        if backup_rel is None:
            try:
                target.unlink()
                deleted.append(target)
            except FileNotFoundError:
                pass
        else:
            data = (files_dir / backup_rel).read_bytes()
            _atomic_write_bytes(target, data)
            restored.append(target)

    # Marks the backup as undone, so ``changes``/``uninstall`` stop
    # listing it as a change still in place. The backup itself stays.
    reverted_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _atomic_write_bytes(config_dir / "backups" / ts / REVERTED_FILENAME, (json.dumps({"reverted_at": reverted_at}) + "\n").encode("utf-8"))

    return RevertResult(ts=ts, config_dir=config_dir, restored=tuple(restored), deleted=tuple(deleted))


def list_backups(config_dir: str | Path) -> list[BackupInfo]:
    """Every backup manifest under ``<config_dir>/backups/``, oldest
    first (matching ``snapshots.load_snapshots``'s own ascending-by-ts
    convention). A manifest that can't be read/parsed is skipped rather
    than raising -- same "degrade around one corrupt file" posture as
    ``snapshots.load_snapshots``."""
    backups_dir = Path(config_dir) / "backups"
    if not backups_dir.is_dir():
        return []
    result: list[BackupInfo] = []
    for ts_dir in sorted(backups_dir.iterdir()):
        manifest_path = ts_dir / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        try:
            reverted_at = json.loads((ts_dir / REVERTED_FILENAME).read_text(encoding="utf-8")).get("reverted_at")
        except (OSError, json.JSONDecodeError, AttributeError):
            reverted_at = None
        result.append(
            BackupInfo(
                ts=str(manifest.get("ts", ts_dir.name)),
                profile_id=str(manifest.get("profile_id", "")),
                scope=str(manifest.get("scope", "")),
                file_count=len(manifest.get("entries", [])),
                reverted_at=str(reverted_at) if reverted_at else None,
            )
        )
    result.sort(key=lambda b: b.ts)
    return result


# -- --launch: a one-session settings overlay, nothing else ------------------


def write_launch_overlay(profile: Profile, *, config_dir: str | Path, managed_keys: set[str] | None = None) -> Path:
    """Write ``<config_dir>/profiles/<id>.settings.json``: a plain
    ``settings.json``-shaped JSON object holding exactly ``profile``'s
    non-managed settings keys, plus its non-managed ``env`` entries
    folded into an ``"env"`` sub-object (never agent-frontmatter --
    ``claude --settings <file>`` only ever accepts top-level settings
    keys, and there is no per-session way to patch an agent's frontmatter
    file). COV-07/COV-11: env used to be excluded here on the reasoning
    that env vars are never written anywhere (see ``plan_apply``'s own
    deviation note in this module's docstring for why that no longer
    holds) -- ``"env"`` is itself a top-level settings key like any
    other, so a launch overlay carries it the same way. This is the
    *only* file this function writes -- no backup, no manifest, no
    active-profile marker, no existing file read or merged -- matching
    the plan's "``--launch`` instead prints a ``claude --settings
    <profile-settings.json>`` command" description of a one-off,
    session-scoped overlay rather than a persisted apply."""
    managed_keys = managed_keys or set()
    settings = {k: v for k, v in profile.settings.items() if k not in managed_keys}
    if "env" not in managed_keys and profile.env:
        settings["env"] = dict(profile.env)
    path = Path(config_dir) / "profiles" / f"{profile.id}.settings.json"
    _atomic_write_bytes(path, (json.dumps(settings, indent=2) + "\n").encode("utf-8"))
    return path

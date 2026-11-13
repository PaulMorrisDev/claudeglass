"""Profile diff and apply-command rendering (plan Milestone v0.3's
``apply``/``--dry-run`` and ``docs/api.md``'s ``GET /api/profiles/<id>/diff``
-- both currently ``501`` stubs waiting on this module, per that route's
own note: "swapping the final ``_not_implemented(...)`` for the real
read/write is the only change needed once that module lands").

Every function here is a pure function of its arguments -- no filesystem
access, no import of ``cli.py``/``service/``/``snapshots.py`` -- matching
this project's established convention for exactly this situation
(``snapshots.py``'s module docstring: "this module takes ``snapshots``
as an explicit ... parameter ... rather than reaching out to load them
itself, so every function here stays a pure function of its
arguments"). The caller (a later work package's ``apply --dry-run`` CLI
path, or the service's diff route) is responsible for loading the
profile, the store's latest ``effective``/``effective_provenance``/
``effective_agents``/``managed_keys`` for the relevant project, and
handing them in as plain data -- exactly the shape ``snapshots.py``
already exposes (see ``docs/config-layers.md``).

Design choices made here, reported per this project's "report deviations
rather than making them silently" convention (``model.py``'s module
docstring):

- **``DiffRow.target_file`` (set by :func:`diff_against_effective`) names
  where a settings key is *currently* set** (derived from its
  ``provenance`` entry: ``"managed-settings.json"``,
  ``".claude/settings.local.json"``, ``".claude/settings.json"`` (repo,
  shared), or the ``"user settings"`` default when the key is not
  currently set anywhere or its provenance layer is ``"user"``/unknown).
  **:func:`render_unified_diff`'s ``scope`` argument is a separate,
  render-time choice of where to *write* the change** -- the caller's
  intended apply target, independent of where the value happens to live
  today (a profile might currently be user-scoped but the caller wants
  to render it as a project-local overlay instead). Only settings-kind
  rows are affected by ``scope``; a per-agent row always renders against
  ``.claude/agents/<name>.md`` regardless of scope (frontmatter is not a
  settings-layer concept), and an env row always renders as a name-only
  note (the plan's own A7 comment: "names only here; values supplied at
  apply time").
- **Managed rows are rendered as a trailing note block, not inside any
  ``---``/``+++`` hunk** ("excluded from the diff body" per this work
  package's brief) -- a caller applying the *rendered text* as if it
  were a real patch will never accidentally act on a line describing a
  key it has no power to change.
- **Rows where the current and proposed value already agree are kept in
  :class:`ProfileDiff` but never rendered** by :func:`render_unified_diff`
  (nothing to act on) -- a caller wanting the full current-vs-proposed
  picture (e.g. an API response) still gets every row from
  :func:`diff_against_effective`, only the *rendered* diff text is
  filtered to genuine changes, matching a real unified diff's own
  behaviour of never printing an unchanged line as a hunk.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .schema import AGENT_ALLOWLIST, MODEL_SETTINGS_ALLOWLIST, Profile

__all__ = [
    "DiffRow",
    "ProfileDiff",
    "diff_against_effective",
    "render_unified_diff",
    "apply_command",
]

_VALID_SCOPES = ("user", "project-local", "repo")

#: Settings-layer provenance name (``snapshots.SETTINGS_LAYER_NAMES``,
#: duplicated here rather than imported -- this module must not depend
#: on ``snapshots.py``, see the module docstring) -> the file that layer
#: represents, for :func:`diff_against_effective`'s "currently set in"
#: target_file. Not present in this map (``None``/unknown provenance, or
#: a key with no current value at all) falls back to "user settings".
_PROVENANCE_TARGET_FILE = {
    "managed": "managed-settings.json",
    "project_local": ".claude/settings.local.json",
    "project_shared": ".claude/settings.json",
    "user": "user settings",
}

#: :func:`render_unified_diff`'s ``scope`` -> the settings file header it
#: writes the (non-agent, non-env) hunk against.
_SCOPE_SETTINGS_FILE = {
    "user": "user settings",
    "project-local": ".claude/settings.local.json",
    "repo": ".claude/settings.json",
}

_ENV_TARGET_FILE = "environment (names only; set the value yourself)"


@dataclass(frozen=True, slots=True)
class DiffRow:
    """One profile key's current-vs-proposed comparison. ``key`` is a
    dotted path: ``"settings.<name>"``, ``"settings.modelSettings.<model
    id>.<name>"`` (one row per model and sub-key; a model id has no dot),
    ``"agents.<agent>.<name>"``, or ``"env.<NAME>"``."""

    key: str
    current_value: object = None
    #: The settings layer (``snapshots.SETTINGS_LAYER_NAMES`` values) the
    #: current value came from, or ``None`` if the key has no current
    #: value anywhere / this row is not a settings key.
    current_provenance: str | None = None
    proposed_value: object = None
    #: Where this key is currently set (see module docstring) --
    #: informational only; :func:`render_unified_diff`'s ``scope``
    #: decides where the *change* is actually rendered against.
    target_file: str = ""
    managed: bool = False
    #: ``False`` for a per-model row whose model has an entry in the
    #: snapshot that lacks this sub-key: the hook keeps only a model's
    #: ``effortLevel``, so its ``autoCompactWindow`` is not known, and
    #: ``current_value`` ``None`` there does not mean "not set".
    recorded: bool = True


@dataclass(frozen=True, slots=True)
class ProfileDiff:
    """Every key ``profile`` sets, each compared against the caller's
    already-computed effective config. See :func:`diff_against_effective`."""

    profile_id: str = ""
    rows: tuple[DiffRow, ...] = field(default_factory=tuple)


#: ``effective_agents[name]``'s own field names (docs/config-layers.md's
#: "effective_agents" section: "reduces this to exactly the fields a
#: TTL/model/effort recommendation keys off: {source,
#: experimental_cache_ttl, model, effort, max_turns}") -> the allowlisted
#: agent key each one answers for. A profile key with no entry here
#: (``memory``, ``tools``, ``disallowedTools``, ``skills``,
#: ``omitClaudeMd``) has no known "current" value -- the snapshot hook
#: does not summarise those fields per plan/docs/config-layers.md, so
#: this module reports them as currently unset rather than guessing.
_EFFECTIVE_AGENT_FIELD = {
    "model": "model",
    "effort": "effort",
    "maxTurns": "max_turns",
    "experimental.cacheTtl": "experimental_cache_ttl",
}


def _is_agent_key_managed(key: str, managed_keys: set) -> bool:
    """A per-agent lever is managed if the administrator locked either
    the specific dotted key, or the blanket ``"agents"``/
    ``"subagentPromptCacheTtl"`` keys ``recommend._lever_managed_keys``
    already treats as governing every agent's TTL lever -- duplicated
    here rather than imported (this module must not depend on
    ``recommend.py``, see the module docstring)."""
    if key in managed_keys or "agents" in managed_keys:
        return True
    if key == "experimental.cacheTtl" and "subagentPromptCacheTtl" in managed_keys:
        return True
    return False


def _model_settings_rows(
    proposed: dict, effective: dict, provenance: dict, managed_keys: set
) -> list[DiffRow]:
    """One row per (model id, sub-key) the profile's ``modelSettings``
    names, sorted by id then in :data:`MODEL_SETTINGS_ALLOWLIST` order.
    ``current_value`` is ``effective["modelSettings"][id][sub-key]``;
    provenance and ``target_file`` are the whole ``modelSettings`` key's."""
    current_models = effective.get("modelSettings")
    if not isinstance(current_models, dict):
        current_models = {}
    has_current = "modelSettings" in effective
    prov = provenance.get("modelSettings") if has_current else None
    target_file = _PROVENANCE_TARGET_FILE.get(prov, "user settings") if has_current else "user settings"
    rows: list[DiffRow] = []
    for model_id in sorted(proposed):
        entry = current_models.get(model_id)
        entry = entry if isinstance(entry, dict) else None
        for sub_key in MODEL_SETTINGS_ALLOWLIST:
            if sub_key not in proposed[model_id]:
                continue
            has_value = entry is not None and sub_key in entry
            rows.append(
                DiffRow(
                    key=f"settings.modelSettings.{model_id}.{sub_key}",
                    current_value=entry[sub_key] if has_value else None,
                    current_provenance=prov if has_value else None,
                    proposed_value=proposed[model_id][sub_key],
                    target_file=target_file if has_value else "user settings",
                    managed="modelSettings" in managed_keys,
                    recorded=entry is None or has_value,
                )
            )
    return rows


def diff_against_effective(
    profile: Profile,
    effective: dict,
    effective_agents: dict,
    provenance: dict,
    managed_keys: set,
) -> ProfileDiff:
    """Every key ``profile`` sets, compared against ``effective``
    (``snapshots.effective_config``'s merged settings),
    ``effective_agents`` (``snapshots.effective_agents``' per-agent
    summary), ``provenance`` (``snapshots.effective_provenance``'s
    ``{key: layer}`` map) and ``managed_keys``
    (``snapshots.managed_keys``'s list, as a ``set``). Pure -- no
    filesystem access; the caller loads all four from a real snapshot
    (or hands in synthetic data for a test)."""
    rows: list[DiffRow] = []

    for key, proposed in profile.settings.items():
        if key == "modelSettings":
            rows.extend(_model_settings_rows(proposed, effective, provenance, managed_keys))
            continue
        has_current = key in effective
        current = effective.get(key)
        prov = provenance.get(key)
        target_file = _PROVENANCE_TARGET_FILE.get(prov, "user settings") if has_current else "user settings"
        rows.append(
            DiffRow(
                key=f"settings.{key}",
                current_value=current if has_current else None,
                current_provenance=prov if has_current else None,
                proposed_value=proposed,
                target_file=target_file,
                managed=key in managed_keys,
            )
        )

    for agent_name in sorted(profile.agents):
        agent_settings = profile.agents[agent_name]
        agent_effective = effective_agents.get(agent_name, {})
        for key in AGENT_ALLOWLIST:
            if key not in agent_settings:
                continue
            proposed = agent_settings[key]
            effective_field = _EFFECTIVE_AGENT_FIELD.get(key)
            has_current = effective_field is not None and effective_field in agent_effective
            current = agent_effective.get(effective_field) if has_current else None
            current_prov = agent_effective.get("source") if has_current else None
            rows.append(
                DiffRow(
                    key=f"agents.{agent_name}.{key}",
                    current_value=current,
                    current_provenance=current_prov,
                    proposed_value=proposed,
                    target_file=f".claude/agents/{agent_name}.md",
                    managed=_is_agent_key_managed(key, managed_keys),
                )
            )

    for name in sorted(profile.env):
        rows.append(
            DiffRow(
                key=f"env.{name}",
                current_value=None,
                current_provenance=None,
                proposed_value=profile.env[name],
                target_file=_ENV_TARGET_FILE,
                managed=name in managed_keys,
            )
        )

    return ProfileDiff(profile_id=profile.id, rows=tuple(rows))


def _fmt(value: object) -> str:
    if value is None:
        return "(unset)"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(str(item) for item in value) + "]"
    return str(value)


def _bare_key(row_key: str, prefix_parts: int) -> str:
    return row_key.split(".", prefix_parts)[prefix_parts]


def render_unified_diff(profile_diff: ProfileDiff, *, scope: str) -> str:
    """A unified-diff-style text of ``profile_diff``'s changed,
    non-managed rows: one ``---``/``+++`` hunk for the settings file
    ``scope`` resolves to, one per named agent's
    ``.claude/agents/<name>.md``, and one for environment variable
    names -- sorted for byte-stable ordering, matching
    ``recommend.render_patch_set``'s own convention. Managed rows never
    appear inside a hunk (see module docstring); each contributes one
    line to a trailing note block instead. A row whose current and
    proposed value already agree is silently dropped (nothing to
    render)."""
    if scope not in _VALID_SCOPES:
        raise ValueError(f"unknown scope: {scope!r} (expected one of {_VALID_SCOPES})")

    settings_rows: list[DiffRow] = []
    agent_rows: dict[str, list[DiffRow]] = {}
    env_rows: list[DiffRow] = []
    for row in profile_diff.rows:
        if row.key.startswith("settings."):
            settings_rows.append(row)
        elif row.key.startswith("agents."):
            agent_name = row.key.split(".", 2)[1]
            agent_rows.setdefault(agent_name, []).append(row)
        elif row.key.startswith("env."):
            env_rows.append(row)

    lines: list[str] = []
    managed_notes: list[str] = []

    def changed(rows: list[DiffRow]) -> list[DiffRow]:
        return [r for r in rows if r.current_value != r.proposed_value]

    def emit_settings_style(path: str, rows: list[DiffRow], prefix_parts: int) -> None:
        rows_changed = changed(rows)
        active = sorted((r for r in rows_changed if not r.managed), key=lambda r: r.key)
        for r in sorted((r for r in rows_changed if r.managed), key=lambda r: r.key):
            managed_notes.append(f"{r.key}: managed by policy, raise with your administrator")
        if not active:
            return
        lines.append(f"--- {path}")
        lines.append(f"+++ {path}")
        for r in active:
            bare = _bare_key(r.key, prefix_parts)
            lines.append(f"-{bare}: {_fmt(r.current_value) if r.recorded else 'not recorded'}")
            lines.append(f"+{bare}: {_fmt(r.proposed_value)}")
        lines.append("")

    emit_settings_style(_SCOPE_SETTINGS_FILE[scope], settings_rows, prefix_parts=1)
    for agent_name in sorted(agent_rows):
        emit_settings_style(f".claude/agents/{agent_name}.md", agent_rows[agent_name], prefix_parts=2)

    env_changed = changed(env_rows)
    env_active = sorted((r for r in env_changed if not r.managed), key=lambda r: r.key)
    for r in sorted((r for r in env_changed if r.managed), key=lambda r: r.key):
        managed_notes.append(f"{r.key}: managed by policy, raise with your administrator")
    if env_active:
        lines.append(f"--- {_ENV_TARGET_FILE}")
        lines.append(f"+++ {_ENV_TARGET_FILE}")
        for r in env_active:
            name = _bare_key(r.key, 1)
            lines.append(f"+{name}={_fmt(r.proposed_value)}")
        lines.append("")

    if managed_notes:
        lines.append("# managed by policy, raise with your administrator:")
        for note in managed_notes:
            lines.append(f"#   {note}")
        lines.append("")

    return "\n".join(lines).rstrip("\n") + ("\n" if lines else "")


def apply_command(profile_id: str, scope: str, project_path: str | None = None) -> str:
    """Two commands, one per line: the exact host-side ``apply``
    invocation for ``profile_id``/``scope``, and the ``apply --launch``
    one-session-overlay alternative (which writes the overlay and prints
    the ``claude --settings <profile-settings.json>`` command). Pure
    string formatting -- no filesystem access (this module never checks
    that ``profile_id`` actually exists), matching every other function
    here.

    Privacy: when ``project_path`` is omitted, nothing in the returned
    text is an absolute path -- a ``project-local``/``repo`` scope with
    no ``project_path`` writes ``--project-dir .``, the folder the
    command is run from (``apply`` refuses a project scope without
    ``--project-dir``, so leaving it out made a command that couldn't
    run). When ``project_path`` is given it is printed exactly as given,
    and nowhere else in the output.

    The flag is ``--project-dir``, not ``--project``: the CLI's common
    ``--project`` flag (every subcommand) already means "a repeatable
    project slug to filter a report by", so ``apply`` names its own
    project-directory argument ``--project-dir`` instead
    (``cli.py``'s ``_add_apply_args`` docstring) -- this function must
    mirror that exactly, since its output is printed verbatim as the
    command a user or the service UI would actually run."""
    if scope not in _VALID_SCOPES:
        raise ValueError(f"unknown scope: {scope!r} (expected one of {_VALID_SCOPES})")

    args = ["claudeglass", "apply", profile_id]
    # ``apply`` defaults to user scope without --project-dir and to
    # project-local with it, so any other scope is named explicitly.
    if scope != "user":
        args += ["--scope", scope]
    if scope in ("project-local", "repo"):
        args += ["--project-dir", project_path or "."]
    if scope == "repo":
        args.append("--allow-tracked")
    apply_cmd = " ".join(args)

    # ``apply --launch`` writes the overlay file and prints the
    # ``claude --settings <file>`` command with its real path: the file
    # doesn't exist until it runs.
    launch_cmd = f"claudeglass apply {profile_id} --launch"

    return f"{apply_cmd}\n{launch_cmd}"

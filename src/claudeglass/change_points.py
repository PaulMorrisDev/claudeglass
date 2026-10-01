"""When your Claude Code setup changed: each ``apply`` (a profile or a
one-off ``--set``), each revert, and each change the config hook's
snapshots show between one session start and the next (a change you or
Claude made by hand, or with a prompt from the dashboard). One edit to
your user settings shows in every project's snapshots, and is one change.

Each change to metrics capture (``capture-log.jsonl``, written by
``config.set_capture``) is one too: it changes what Claude writes and
what it costs. Turning capture on, changing what it measures or removing
it also rewrites its hook entries in your settings, and that settings
change isn't a second point.

EST-P9: when a corpus is on hand (``change_points(config_dir, corpus)``),
a session's transcript can show a change nothing else caught: a
CLAUDE.md or memory size change of :data:`CLAUDE_MD_CHANGE_PCT` percent
or more, or the dominant model or effort level shifting. These are
``source "transcript"`` points. A new value counts once it holds for
:data:`SUSTAINED_SESSIONS` sessions in a row in the same project, and
the point is timestamped at the first of them. Switching back and forth,
one odd session, a CLAUDE.md that grows a little at a time, a CLAUDE.md
that is missing from a session, and an effort level the transcript
doesn't record are all left out. A setting an apply, undo or settings
change recorded between the two sessions isn't one either: the sessions
showing it is that change seen again, and a second point would cut the
first one's after sessions short.

Each point names the project it applies to (``project``), as the
config hook's snapshot key (``snapshots.snapshot_project_key``, with the
drive letter upper-cased), or ``""`` for a change that applies in every
project: an apply to your user settings, or a settings change outside a
project's own files. A config point records each changed setting's old
and new value where both are short plain values.

Used for the "Since my last change" window and for the before-and-after
comparison in :mod:`impact`. Reads ``<config_dir>/backups/*/manifest.json``,
``<config_dir>/snapshots/`` and ``<config_dir>/capture-log.jsonl``;
writes nothing.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable, Collection
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import capture_catalogue
from . import config as config_mod
from . import discovery
from . import snapshots as snapshots_mod
from .model import EventKind
from .profiles import apply as apply_mod
from .profiles.frontmatter import parse_frontmatter

_TS_FORMAT = "%Y%m%dT%H%M%SZ"
#: A CLAUDE.md/memory size change at least this big (either way, from the
#: session before in the same project) is a change (EST-P9).
CLAUDE_MD_CHANGE_PCT = 10.0

#: A transcript change is a change point once the new value has held for
#: this many sessions in a row in one project (EST-P9). It matches the
#: sessions each side of a change card needs (``impact.MIN_SESSIONS``), so
#: a point that appears has the after sessions to judge it with.
SUSTAINED_SESSIONS = 3

#: Snapshot sections whose change is a change to how Claude Code runs.
#: ``env_names`` (which variables are set, not their values) and
#: provenance fields are left out: they change without changing behaviour.
_BEHAVIOUR_PREFIXES = ("effective.", "agents.", "user_settings.", "project_settings.", "mcp_servers", "enabled_plugins")

#: The settings layers a project's own files hold (``snapshots.
#: effective_provenance``): a change that only came from these applies in
#: that project alone.
_PROJECT_LAYERS = ("project_local", "project_shared")

#: A config point records a value only when it is a plain value no longer
#: than this: settings values, never file contents or long commands.
_MAX_VALUE_CHARS = 80


@dataclass(slots=True)
class ChangePoint:
    ts: datetime
    #: "apply", "revert", "config", "capture" or "transcript".
    source: str
    label: str
    #: Settings keys that changed, as ``key`` or ``agent: key``, when known.
    keys: list[str] = field(default_factory=list)
    #: Each changed key's old and new value, when the apply manifest has them.
    changes: list[dict] = field(default_factory=list)
    #: Backup timestamp, for an apply (so the undo command can name it).
    backup_ts: str = ""
    reverted: bool = False
    #: The project it applies to, as ``snapshots.snapshot_project_key``
    #: names it; ``""`` for every project.
    project: str = ""

    def iso(self) -> str:
        return self.ts.strftime("%Y-%m-%dT%H:%M:%SZ")

    def to_dict(self) -> dict:
        return {
            "ts": self.iso(),
            "source": self.source,
            "label": self.label,
            "keys": list(self.keys),
            "changes": list(self.changes),
            "backup_ts": self.backup_ts,
            "reverted": self.reverted,
            "project": self.project,
            "summary": summary(self),
        }


def _words(value) -> str:
    if value is None:
        return "not set"
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, (list, tuple)):
        # A capture list ([capture] coaching, feedback): its metrics' names.
        names = [
            capture_catalogue.METRICS_BY_ID[v].title if v in capture_catalogue.METRICS_BY_ID else str(v) for v in value
        ]
        return ", ".join(names) if names else "none"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def summary(point: ChangePoint) -> str:
    """What changed, in one line: each change with both values known as
    "key: old → new", then any other key by name."""
    parts: list[str] = []
    named: set[str] = set()
    for change in point.changes:
        if "old" not in change or "new" not in change:
            continue
        label = _key_label(change)
        named.add(label)
        parts.append(f"{label}: {_words(change['old'])} → {_words(change['new'])}")
    parts.extend(k for k in point.keys if k not in named and _plain_key(k) not in named)
    return "; ".join(parts)


def _plain_key(label: str) -> str:
    """A config key label as a change names it: ``effective.model`` is
    ``model``, ``agents.reviewer.model`` is ``reviewer: model``."""
    if label.startswith("effective."):
        return label[len("effective."):]
    if label.startswith("agents."):
        parts = label.split(".")
        if len(parts) >= 3:
            return f"{parts[1]}: {parts[-1]}"
    return label


def _parse_backup_ts(ts: str) -> datetime | None:
    try:
        return datetime.strptime(ts.split("-")[0], _TS_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return _parse_backup_ts(ts)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _manifest_changes(config_dir: Path, backup_ts: str) -> list[dict]:
    folder = config_dir / "backups" / backup_ts
    try:
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for entry in manifest.get("entries") or ():
        if not isinstance(entry, dict):
            continue
        if "changes" not in entry:
            out.extend(_backup_diff(folder, entry))
        for change in entry.get("changes") or ():
            if isinstance(change, dict) and change.get("key"):
                out.append(change)
    return out


def _read_keys(path: Path, kind: str) -> dict | None:
    """A settings file's top-level keys or an agent file's frontmatter;
    ``{}`` for a missing file, ``None`` for one that can't be read."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError:
        return None
    try:
        data = json.loads(text) if kind == "settings" else parse_frontmatter(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _backup_diff(folder: Path, entry: dict) -> list[dict]:
    """The keys an apply changed, for a manifest written before applies
    recorded them: the backed-up file against the file now. Later edits
    to the same keys show too, so this names keys but not values."""
    kind = entry.get("kind")
    if kind not in ("settings", "agent_frontmatter") or not entry.get("path"):
        return []
    old = _read_keys(folder / "files" / entry["backup"], kind) if entry.get("backup") else {}
    new = _read_keys(Path(entry["path"]), kind)
    if old is None or new is None:
        return []
    agent = entry.get("agent_name") if kind == "agent_frontmatter" else None
    return [
        {"key": key, "agent": agent}
        for key in sorted(set(old) | set(new))
        if json.dumps(old.get(key), sort_keys=True, default=str) != json.dumps(new.get(key), sort_keys=True, default=str)
    ]


def _key_label(change: dict) -> str:
    return f"{change['agent']}: {change['key']}" if change.get("agent") else str(change["key"])


def project_key(project_path: str | Path) -> str:
    """The snapshot project key (``snapshots.snapshot_project_key``, so the
    drive letter is upper-cased) for a project folder."""
    return snapshots_mod.snapshot_project_key(discovery.slug_for(str(project_path)))


def _manifest_project(config_dir: Path, backup) -> str:
    """The project an apply wrote to, from its manifest's settings or
    agent file path; ``""`` for an apply to your user settings."""
    if backup.scope in ("", "user"):
        return ""
    try:
        manifest = json.loads((config_dir / "backups" / backup.ts / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    for entry in manifest.get("entries") or ():
        if not isinstance(entry, dict) or entry.get("kind") not in ("settings", "agent_frontmatter"):
            continue
        if not entry.get("path"):
            continue
        path = Path(entry["path"])
        # <project>/.claude/settings(.local).json or <project>/.claude/agents/<name>.md
        claude_dir = path.parent if entry["kind"] == "settings" else path.parent.parent
        if claude_dir.name == ".claude":
            return project_key(claude_dir.parent)
    return ""


def _apply_points(config_dir: Path) -> list[ChangePoint]:
    points: list[ChangePoint] = []
    for backup in apply_mod.list_backups(config_dir):
        when = _parse_backup_ts(backup.ts)
        if when is None:
            continue
        changes = _manifest_changes(config_dir, backup.ts)
        project = _manifest_project(config_dir, backup)
        profile = backup.profile_id
        label = "Applied a one-off change" if profile in ("one-off", "") else f"Applied profile {profile}"
        points.append(
            ChangePoint(
                ts=when,
                source="apply",
                label=label,
                keys=[_key_label(c) for c in changes],
                changes=changes,
                backup_ts=backup.ts,
                reverted=backup.reverted_at is not None,
                project=project,
            )
        )
        reverted = _parse_iso(backup.reverted_at)
        if reverted is not None:
            points.append(
                ChangePoint(
                    ts=reverted,
                    source="revert",
                    label=f"Undid {label[0].lower()}{label[1:]}",
                    keys=[_key_label(c) for c in changes],
                    backup_ts=backup.ts,
                    project=project,
                )
            )
    return points


def _flat(snap: snapshots_mod.Snapshot) -> dict:
    flat = {k: v for k, v in snapshots_mod.flatten_snapshot(snap).items() if k.startswith(_BEHAVIOUR_PREFIXES)}
    for key, value in snapshots_mod.effective_config(snap).items():
        flat[f"effective.{key}"] = value
    return flat


def _changed_keys(before: snapshots_mod.Snapshot, after: snapshots_mod.Snapshot) -> list[str]:
    """Keys whose value differs. When both snapshots carry the merged
    ``effective`` settings, those stand for the per-layer settings keys
    (the same change would otherwise be listed twice)."""
    old, new = _flat(before), _flat(after)
    changed = sorted(
        k for k in set(old) | set(new)
        if json.dumps(old.get(k), sort_keys=True, default=str) != json.dumps(new.get(k), sort_keys=True, default=str)
    )
    if any(k.startswith("effective.") for k in changed):
        changed = [k for k in changed if not k.startswith(("user_settings.", "project_settings."))]
    return changed


def _plain_value(value) -> bool:
    if isinstance(value, str):
        return len(value) <= _MAX_VALUE_CHARS
    return value is None or isinstance(value, (bool, int, float))


def _config_changes(before: snapshots_mod.Snapshot, after: snapshots_mod.Snapshot, keys: list[str]) -> list[dict]:
    """Each changed settings lever (``effective.*``) or agent model with
    its old and new value, when both are plain values."""
    old, new = _flat(before), _flat(after)
    out = []
    for key in keys:
        if key.startswith("effective."):
            agent, name = None, key[len("effective."):]
        elif key.startswith("agents.") and key.count(".") == 2 and key.endswith(".model"):
            agent, name = key.split(".")[1], "model"
        else:
            continue
        if _plain_value(old.get(key)) and _plain_value(new.get(key)):
            out.append({"key": name, "agent": agent, "old": old.get(key), "new": new.get(key)})
    return out


#: The one snapshot key a capture change moves: turning capture on,
#: changing what it measures or removing it rewrites its hook entries in
#: ``~/.claude/settings.json`` (``capture off`` leaves them), which the
#: snapshot records as ``dict(N)``, the number of hook events.
_CAPTURE_HOOK_KEYS = frozenset({"user_settings.hooks"})


def _dump(value) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _names(value) -> list[str]:
    return [str(v) for v in value] if isinstance(value, list) else []


def _mcp_names(snap: snapshots_mod.Snapshot) -> list[str]:
    servers = snap.data.get("mcp_servers")
    return _names(servers.get("names")) if isinstance(servers, dict) else []


def _mcp_json_names(snap: snapshots_mod.Snapshot) -> list[str]:
    """The servers the project's own ``.mcp.json`` names."""
    layers = snap.data.get("content_layers")
    mcp_json = layers.get("mcp_json") if isinstance(layers, dict) else None
    return _names(mcp_json.get("names")) if isinstance(mcp_json, dict) else []


def _agent_name(key: str, before: snapshots_mod.Snapshot, after: snapshots_mod.Snapshot) -> str:
    """The agent an ``agents.<name>.<field>`` key belongs to (a name can
    have dots in it)."""
    rest = key[len("agents."):]
    known = (
        str(name)
        for snap in (before, after)
        if isinstance(snap.data.get("agents"), dict)
        for name in snap.data["agents"]
        if rest.startswith(f"{name}.")
    )
    return max(known, key=len, default=rest.split(".")[0])


def _agent_source(snap: snapshots_mod.Snapshot, name: str):
    agents = snap.data.get("agents")
    entry = agents.get(name) if isinstance(agents, dict) else None
    return entry.get("source") if isinstance(entry, dict) else None


def _origin(
    key: str, before: snapshots_mod.Snapshot, after: snapshots_mod.Snapshot, after_flat: dict
) -> tuple[bool, str | None]:
    """Where a changed key's change came from: whether a project's own
    files changed it, and the new value (as JSON) of the part your user
    settings changed, when one did. Every project's snapshots show one
    user-level edit with the same new value, which is how they are told
    to be one change; a project's own change shows in its snapshots
    alone."""
    if key.startswith("project_settings."):
        return True, None
    if key.startswith("effective."):
        name = key[len("effective."):]
        if any(snapshots_mod.effective_provenance(snap).get(name) in _PROJECT_LAYERS for snap in (before, after)):
            return True, None
    elif key.startswith("agents."):
        name = _agent_name(key, before, after)
        if any(_agent_source(snap, name) == "project" for snap in (before, after)):
            return True, None
    elif key == "mcp_servers.names":
        # Every server the project can reach, its own .mcp.json's among
        # them: split by the snapshot's record of what that file names.
        own = set(_mcp_json_names(before)) | set(_mcp_json_names(after))
        old, new = _mcp_names(before), _mcp_names(after)
        old_rest, new_rest = [n for n in old if n not in own], [n for n in new if n not in own]
        project = [n for n in old if n in own] != [n for n in new if n in own]
        return project, _dump(new_rest) if old_rest != new_rest else None
    return False, _dump(after_flat.get(key))


def _reported_name(key: str, before: snapshots_mod.Snapshot, after: snapshots_mod.Snapshot) -> str:
    """The name :func:`_config_points` notes a user-level change under. A
    settings lever your user settings set shows as ``effective.<name>``
    in a project that doesn't override it, and as ``user_settings.<name>``
    in one whose own files do, so both note it as the second."""
    if key.startswith("effective."):
        name = key[len("effective."):]
        if any(snapshots_mod.effective_provenance(snap).get(name) == "user" for snap in (before, after)):
            return f"user_settings.{name}"
    return key


def _user_changes(before: snapshots_mod.Snapshot, after_flat: dict) -> dict[str, str]:
    """Each user-settings key a difference changed, with its new value as
    JSON, those :func:`_changed_keys` leaves out for the settings lever
    they set included."""
    old = _flat(before)
    return {
        key: _dump(after_flat.get(key))
        for key in set(old) | set(after_flat)
        if key.startswith("user_settings.") and _dump(old.get(key)) != _dump(after_flat.get(key))
    }


def _config_project(after: snapshots_mod.Snapshot, everywhere: bool) -> str:
    """The snapshot's project (as stored) for a point that changes only
    that project's own files (:func:`_origin`): a settings lever its
    settings file set, a project setting, one of its agents or its
    ``.mcp.json``. ``""`` when the point reports a change every project
    sees (``everywhere``)."""
    if everywhere:
        return ""
    slug = after.data.get("project_slug")
    return str(slug) if isinstance(slug, str) and slug else ""


def _capture_log_cutoff(config_dir: Path, now: datetime | None) -> datetime:
    """When ``capture-log.jsonl`` starts: ``serve`` and ``capture prune``
    drop what is older than ``retention_days`` (``config.toml``, else
    :data:`config.SIGNAL_RETENTION_DEFAULT_DAYS`;
    :func:`config.prune_capture_log`). ``serve --retention-days`` prunes
    by its own number, which this doesn't see."""
    try:
        days = config_mod.saved_retention_days(config_dir)
    except config_mod.ConfigError:
        days = None
    return (now or datetime.now(timezone.utc)) - timedelta(days=days or config_mod.SIGNAL_RETENTION_DEFAULT_DAYS)


def _config_diffs(
    config_dir: Path,
) -> list[tuple[datetime | None, ChangePoint, snapshots_mod.Snapshot, snapshots_mod.Snapshot]]:
    """Wherever one project's snapshot differs from its previous one in a
    key that changes how Claude Code runs: the previous snapshot's time, a
    point with every key that changed, and the two snapshots. Oldest
    first."""
    diffs = []
    previous: dict[str, snapshots_mod.Snapshot] = {}
    for snap in snapshots_mod.load_snapshots(config_dir):
        project = str(snap.data.get("project_slug") or "")
        before = previous.get(project)
        previous[project] = snap
        if before is None:
            continue
        keys = _changed_keys(before, snap)
        when = _parse_backup_ts(snap.ts) or _parse_iso(snap.ts)
        if not keys or when is None:
            continue
        since = _parse_backup_ts(before.ts) or _parse_iso(before.ts)
        point = ChangePoint(ts=when, source="config", label="Your settings changed", keys=keys)
        diffs.append((since, point, before, snap))
    diffs.sort(key=lambda diff: diff[1].ts)
    return diffs


def _config_points(
    config_dir: Path, applied: list[ChangePoint], captures: list[ChangePoint], now: datetime | None = None
) -> list[ChangePoint]:
    """A change point for each settings change the snapshots show, once.

    Each project's snapshots are compared with its own previous one, so
    an edit to your user settings shows in all of them. The first to see
    it is the point: a key a later snapshot shows with the same new value
    is dropped, and a point left with no keys goes. A lever your user
    settings set is one key whether a snapshot shows it as the lever or,
    in a project whose own files override it, as the user setting
    (:func:`_reported_name`), and a point that changes every project
    notes every user setting its difference changed
    (:func:`_user_changes`). A key a project's own files changed
    (:func:`_origin`) is never dropped that way, and a point left with
    only such keys is that project's (:func:`_config_project`).

    A difference that spans an apply or revert is that change seen again.
    Turning capture on, changing what it measures or removing it rewrites
    its hook entries in your settings, so ``user_settings.hooks`` is
    dropped from a difference a capture change falls inside, and so is a
    hooks-only difference too old for ``capture-log.jsonl`` to still hold
    the change that made it. Either way its new value counts as reported,
    so another project's later snapshot of the same hooks goes too."""
    cutoff = _capture_log_cutoff(config_dir, now)
    reported: dict[str, str] = {}
    points: list[ChangePoint] = []
    for since, point, before, snap in _config_diffs(config_dir):
        if any((since is None or since <= other.ts) and other.ts <= point.ts for other in applied):
            continue
        after_flat = _flat(snap)
        captured = any((since is None or since <= other.ts) and other.ts <= point.ts for other in captures)
        kept: list[str] = []
        # Whether a key is kept for a user-level change no earlier point
        # reported; a point with none changes only the project's own files.
        everywhere = False
        # What this point reports, noted once the point is settled: the
        # new value of each user-level key it keeps, and of the hooks key
        # a capture change explains or that is too old for the capture log
        # to still say (the rule below), so a later snapshot of the same
        # edit (taken after the capture was logged) is dropped too.
        seen: dict[str, str] = {}
        for key in point.keys:
            if key in _CAPTURE_HOOK_KEYS and captured:
                seen[key] = _dump(after_flat.get(key))
                continue
            project, value = _origin(key, before, snap, after_flat)
            name = _reported_name(key, before, snap)
            fresh = value is not None and value != reported.get(name)
            if project or fresh:
                kept.append(key)
                everywhere = everywhere or fresh
                if value is not None:
                    seen[name] = value
        if kept and _CAPTURE_HOOK_KEYS.issuperset(kept) and point.ts < cutoff:
            kept = []
        if kept and everywhere:
            # The rest of the same edit: the user-settings keys behind the
            # levers it shows, so a project that overrides one of those
            # levers, and so shows the edit as these keys, isn't a second.
            seen = {**_user_changes(before, after_flat), **seen}
        reported.update(seen)
        if not kept:
            continue
        points.append(
            replace(
                point,
                keys=kept,
                changes=_config_changes(before, snap, kept),
                project=_config_project(snap, everywhere),
            )
        )
    return points


def _capture_label(record: dict) -> str:
    changed = record.get("changed") if isinstance(record.get("changed"), dict) else {}
    coaching = changed.get("coaching") if isinstance(changed.get("coaching"), dict) else None
    if coaching is not None and set(changed) == {"coaching"}:
        old = coaching.get("from") if isinstance(coaching.get("from"), list) else []
        new = coaching.get("to") if isinstance(coaching.get("to"), list) else []
        if "coaching_notes" in new and "coaching_notes" not in old:
            return "Turned coaching notes on"
        if "coaching_notes" in old and "coaching_notes" not in new:
            return "Turned coaching notes off"
        return "Changed live coaching"
    tagger = changed.get("tagger") if isinstance(changed.get("tagger"), dict) else None
    if tagger is not None and set(changed) == {"tagger"}:
        return "Claude Haiku writes the tags" if tagger.get("to") == "haiku" else "Claude writes the tags again"
    # The label follows what changed: the log's own ``level`` is where
    # capture stands after the change, so it says "off" for any change made
    # while capture is off (which projects it runs in, say).
    level = changed.get("level") if isinstance(changed.get("level"), dict) else None
    if level is not None:
        new = str(level.get("to") or record.get("level") or "off")
        title = capture_catalogue.LEVEL_TITLES.get(new, new)
        if new == "off":
            return "Turned metrics capture off"
        if level.get("from") == "off":
            return f"Turned metrics capture on: {title}"
        return f"Metrics capture level: {title}"
    if set(changed) <= {"projects"}:
        return "Changed which projects capture and coaching run in"
    return "Changed metrics capture"


#: Records or snapshots this close together are one command seen more
#: than once: ``capture on`` logs which projects and then the level, and
#: a settings edit can reach two project chains seconds apart.
ONE_EDIT = timedelta(seconds=60)


def _capture_records(config_dir: Path) -> list[tuple[datetime, dict]]:
    """``capture-log.jsonl``'s changes, oldest first, with the records one
    command wrote within :data:`ONE_EDIT` of each other as one: each key
    from its first ``from`` to its last ``to``, at the first record's time."""
    merged: list[tuple[datetime, dict]] = []
    last: datetime | None = None
    for record in config_mod.load_capture_log(config_dir):
        when = _parse_iso(record.get("ts"))
        changed = record.get("changed")
        if when is None or not isinstance(changed, dict) or not changed:
            continue
        if merged and last is not None and timedelta(0) <= when - last <= ONE_EDIT:
            _first, record_so_far = merged[-1]
            joined = dict(record_so_far["changed"])
            for key, value in changed.items():
                earlier = joined.get(key)
                if isinstance(earlier, dict) and isinstance(value, dict):
                    joined[key] = {**value, "from": earlier.get("from")}
                else:
                    joined[key] = value
            merged[-1] = (_first, {**record, "changed": joined})
        else:
            merged.append((when, record))
        last = when
    return merged


def _capture_points(config_dir: Path) -> list[ChangePoint]:
    """A change point for each ``[capture]`` change in ``capture-log.jsonl``
    (one per command, :func:`_capture_records`)."""
    points = []
    for when, record in _capture_records(config_dir):
        changed = record["changed"]
        changes = [
            {"key": f"capture.{key}", "agent": None, "old": value.get("from"), "new": value.get("to")}
            for key, value in sorted(changed.items())
            if isinstance(value, dict)
        ]
        points.append(
            ChangePoint(
                ts=when,
                source="capture",
                label=_capture_label(record),
                keys=[c["key"] for c in changes],
                changes=changes,
            )
        )
    return points


# -- EST-P9: change points a transcript itself shows -----------------------


def _dominant(values) -> str:
    """The most common non-empty value (mirrors quality.py's own
    ``_dominant``, duplicated here since a session's dominant model/effort
    isn't otherwise available without a full ``quality.Run``)."""
    counts = Counter(v for v in values if v)
    return counts.most_common(1)[0][0] if counts else ""


def _priced_turns(top) -> list:
    return [turn for turn in top.turns if turn.turn_index > 0]


def _claude_md_chars(top) -> int:
    """CLAUDE.md/memory characters injected before this session's first
    priced turn (CONTEXT_INJECT events, subkind "instructions" or
    "nested_memory") -- the same events context_budget.py's startup
    accounting counts, duplicated minimally here since EST-P9 only needs
    the total, not the per-source breakdown."""
    turns = _priced_turns(top)
    first_ts = _parse_iso(turns[0].ts) if turns else None
    total = 0
    for event in top.events:
        if event.kind != EventKind.CONTEXT_INJECT or event.subkind not in ("instructions", "nested_memory"):
            continue
        if first_ts is not None:
            event_ts = _parse_iso(event.ts)
            if event_ts is not None and event_ts >= first_ts:
                continue
        total += event.size_chars or 0
    return total


@dataclass(slots=True)
class _SessionSignature:
    start: datetime
    project: str
    claude_md_chars: int
    model: str
    #: ``"default"`` when no reply records one.
    effort: str
    #: The project as ``snapshots.snapshot_project_key`` names it: the
    #: canonical key, so ``c--X`` and ``C--X`` folders are one project.
    key: str = ""


def _session_signature(bundle) -> _SessionSignature | None:
    top = bundle.top
    if top is None:
        return None
    turns = _priced_turns(top)
    start = next((t for t in (_parse_iso(turn.ts) for turn in turns) if t is not None), None)
    if start is None:
        return None
    return _SessionSignature(
        start=start,
        # The dashboard's corpus comes from the store, which keeps no
        # project folder: the slug tells its projects apart instead.
        project=bundle.project_dir or bundle.slug,
        claude_md_chars=_claude_md_chars(top),
        model=_dominant(turn.model for turn in turns),
        effort=_dominant(turn.effort or "" for turn in turns) or "default",
        key=snapshots_mod.snapshot_project_key(bundle.slug) if bundle.slug else "",
    )


_TRANSCRIPT_KEY_LABELS = {"model": "Model", "effortLevel": "Effort level", "claude_md_chars": "CLAUDE.md size"}


def _join(bits: list[str]) -> str:
    if len(bits) <= 1:
        return bits[0] if bits else ""
    if len(bits) == 2:
        return f"{bits[0]} and {bits[1]}"
    return f"{', '.join(bits[:-1])} and {bits[-1]}"


def _transcript_label(keys: list[str]) -> str:
    bits = [_TRANSCRIPT_KEY_LABELS[k] for k in ("model", "effortLevel", "claude_md_chars") if k in keys]
    return f"{_join(bits)} changed" if bits else "Your setup changed"  # pragma: no cover


def _same_value(old, new) -> bool:
    return old == new


def _same_size(old: int, new: int) -> bool:
    """Whether a CLAUDE.md size is within :data:`CLAUDE_MD_CHANGE_PCT`
    percent of ``old`` (never 0: a session without one isn't tracked)."""
    return abs(new - old) / old * 100.0 < CLAUDE_MD_CHANGE_PCT


#: What a transcript can show a change in: the point's key, how two values
#: are told apart, and each session's value. A session with a falsy one (no
#: model, no CLAUDE.md, an effort level the transcript doesn't record) says
#: nothing about it and is passed over.
_TRACKED: tuple[tuple[str, Callable, Callable], ...] = (
    ("model", _same_value, lambda sig: sig.model),
    ("effortLevel", _same_value, lambda sig: "" if sig.effort == "default" else sig.effort),
    ("claude_md_chars", _same_size, lambda sig: sig.claude_md_chars),
)


@dataclass(slots=True)
class _Tracker:
    """One project's model, effort level or CLAUDE.md size, session by
    session, until the value changes and the change holds.

    ``settled`` is the value in force. A session with another value starts
    a run, and the run's new value counts as a change once it has
    :data:`SUSTAINED_SESSIONS` sessions in a row (each one the same as the
    run's first), at which point it is the value in force. A session with
    the value in force ends the run, so switching back and forth, or one
    odd session, changes nothing. A CLAUDE.md size follows each session
    that is within :data:`CLAUDE_MD_CHANGE_PCT` percent of it, so growing
    a little at a time never adds up to a change.

    The value in force has to be known, and it is once it has held for
    :data:`SUSTAINED_SESSIONS` sessions in a row (``confirmed``). Until
    then a run only counts if every session before it held that one
    value (``clean``): a project's first sessions (one on its old model,
    then the new one for good) show a switch, but a project that has
    shown several values just adopts the one that sticks."""

    same: Callable
    settled: object = None
    held: int = 0
    confirmed: bool = False
    clean: bool = True
    previous: _SessionSignature | None = None
    run: list = field(default_factory=list)
    run_before: _SessionSignature | None = None

    def _end_run(self) -> None:
        if self.run and not self.confirmed:
            self.clean = False
        self.run = []

    def feed(self, sig: _SessionSignature, value) -> tuple[datetime, datetime, object, object] | None:
        """Take the project's next session. When that makes a change hold:
        the session before it, the first session with the new value, and
        the old and new values."""
        change = None
        if self.settled is None:
            self.settled, self.held = value, 1
        elif self.same(self.settled, value):
            self._end_run()
            self.settled, self.held = value, self.held + 1
            self.confirmed = self.confirmed or self.held >= SUSTAINED_SESSIONS
        else:
            self.held = 0
            if self.run and self.same(self.run[0][1], value):
                self.run.append((sig, value))
            else:
                self._end_run()
                self.run, self.run_before = [(sig, value)], self.previous
            if len(self.run) == SUSTAINED_SESSIONS:
                (first, new), (_last, latest) = self.run[0], self.run[-1]
                if self.confirmed or self.clean:
                    change = (self.run_before.start, first.start, self.settled, new)
                self.settled, self.held, self.confirmed, self.run = latest, len(self.run), True, []
        self.previous = sig
        return change


def _transcript_points(corpus) -> list[tuple[datetime, ChangePoint]]:
    """A change point wherever one project's sessions show a new value for
    the dominant model, the dominant effort level or the CLAUDE.md or
    memory size (a change of :data:`CLAUDE_MD_CHANGE_PCT` percent or more)
    that then holds for :data:`SUSTAINED_SESSIONS` sessions in a row
    (EST-P9, see :class:`_Tracker`). Each comes with the start of the
    session before the new value's first: the change happened between the
    two, and the point is the first. Values whose runs start at the same
    session are one point."""
    projects: dict[str, list[_SessionSignature]] = {}
    for bundle in corpus.sessions:
        sig = _session_signature(bundle)
        if sig is not None:
            projects.setdefault(sig.key or sig.project, []).append(sig)
    points: list[tuple[datetime, ChangePoint]] = []
    for signatures in projects.values():
        signatures.sort(key=lambda s: s.start)
        trackers = {key: _Tracker(same) for key, same, _ in _TRACKED}
        found: dict[datetime, dict[str, tuple[datetime, object, object]]] = {}
        for sig in signatures:
            for key, _, value_of in _TRACKED:
                value = value_of(sig)
                change = trackers[key].feed(sig, value) if value else None
                if change is not None:
                    since, start, old, new = change
                    found.setdefault(start, {})[key] = (since, old, new)
        for start, changed in sorted(found.items()):
            keys = [key for key, _, _ in _TRACKED if key in changed]
            points.append(
                (
                    min(since for since, _old, _new in changed.values()),
                    ChangePoint(
                        ts=start,
                        source="transcript",
                        label=_transcript_label(keys),
                        keys=keys,
                        changes=[
                            {"key": key, "agent": None, "old": changed[key][1], "new": changed[key][2]} for key in keys
                        ],
                        project=signatures[0].key,
                    ),
                )
            )
    return points


def _explained(since: datetime, point: ChangePoint, recorded: list[ChangePoint]) -> ChangePoint | None:
    """``point`` without the keys a recorded change (an apply, undo or
    settings change) made between the last session before the new value
    (``since``) and the first with it, in its project: the sessions
    showing that change is the same change seen again. ``None`` when
    every key is explained."""
    made = {
        label.rpartition(": ")[2].split(".")[-1]
        for other in recorded
        if since <= other.ts <= point.ts and applies_to(other, point.project)
        for label in other.keys
        if not label.startswith("agents.")
    }
    keys = [key for key in point.keys if key not in made]
    if len(keys) == len(point.keys):
        return point
    if not keys:
        return None
    changes = [c for c in point.changes if c.get("key") in keys]
    return replace(point, label=_transcript_label(keys), keys=keys, changes=changes)


def _canonical_projects(corpus) -> dict[str, str]:
    """Every key a project of ``corpus`` goes by, to its canonical one
    (``snapshots.snapshot_project_keys``). A snapshot taken before the
    config hook upper-cased the drive letter is filed under the other."""
    slugs = {bundle.slug for bundle in corpus.sessions if bundle.slug}
    return {key: keys[0] for keys in map(snapshots_mod.snapshot_project_keys, slugs) for key in keys}


def _one_edit(points: list[ChangePoint]) -> list[ChangePoint]:
    """Config points for one project within :data:`ONE_EDIT` of each other
    as one: the same edit seen by two project chains, or saved twice. Each
    key keeps its first old value and its last new one."""
    out: list[ChangePoint] = []
    for point in sorted(points, key=lambda p: p.ts):
        prior = next(
            (
                p
                for p in reversed(out)
                if p.source == "config" and p.project == point.project and point.ts - p.ts <= ONE_EDIT
            ),
            None,
        )
        if point.source != "config" or prior is None:
            out.append(point)
            continue
        olds = {c.get("key"): c for c in prior.changes}
        changes = [
            {**c, "old": olds[c.get("key")].get("old")} if c.get("key") in olds else c for c in point.changes
        ]
        changes += [c for c in prior.changes if c.get("key") not in {n.get("key") for n in point.changes}]
        keys = list(dict.fromkeys([*prior.keys, *point.keys]))
        out[out.index(prior)] = replace(prior, keys=keys, changes=changes)
    return out


def change_points(config_dir: Path | str, corpus=None, *, now: datetime | None = None) -> list[ChangePoint]:
    """Every change point, oldest first. A snapshot difference that spans
    an apply or revert is that change seen again, not a second one, and
    so is a transcript change a recorded one explains (:func:`_explained`).
    ``corpus``, when given, adds transcript-derived points too (EST-P9,
    see the module docstring) -- opt-in, since building a corpus is more
    than ``config_dir`` alone can do, and most callers (the "since my
    last change" window) build one only when asked for that window. It
    also names a config point's project by its canonical key, where the
    project is one of the corpus's. ``now`` is where ``capture-log.jsonl``'s
    retention is counted back from (:func:`_config_points`)."""
    config_dir = Path(config_dir)
    applied = _apply_points(config_dir)
    captures = _capture_points(config_dir)
    points = [*applied, *_one_edit(_config_points(config_dir, applied, captures, now)), *captures]
    if corpus is not None:
        canonical = _canonical_projects(corpus)
        points = [replace(p, project=canonical.get(p.project, p.project)) if p.project else p for p in points]
        recorded = list(points)
        for since, point in _transcript_points(corpus):
            point = _explained(since, point, recorded)
            if point is not None:
                points.append(point)
    points.sort(key=lambda p: p.ts)
    return points


def latest(config_dir: Path | str, corpus=None) -> ChangePoint | None:
    points = change_points(config_dir, corpus)
    return points[-1] if points else None


def applies_to(point: ChangePoint, project: str | Collection[str]) -> bool:
    """Whether ``point`` applies in ``project``: a snapshot project key, or
    every key one project goes by (``snapshots.snapshot_project_keys``).
    A change for every project applies everywhere."""
    if not point.project:
        return True
    if isinstance(project, str):
        return point.project == project
    return point.project in project


__all__ = ["ChangePoint", "SUSTAINED_SESSIONS", "applies_to", "change_points", "latest", "project_key", "summary"]

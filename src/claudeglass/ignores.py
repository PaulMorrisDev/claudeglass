"""Recommendations you chose to ignore, kept per profile and per project.

The dashboard's **Ignore this recommendation** button stores one entry in
``<config_dir>/ignored-recommendations.json``::

    {"version": 1,
     "profiles": {"<profile id, or 'none'>": {
         "<project, or '*' for every project>": {
             "<recommendation key>": {"fingerprint", "ignored_at", "title", "changes"}}}}}

- **Per profile.** The profile is the one ``apply <profile>`` last marked
  active (``<config_dir>/active-profile``); ``none`` when no profile has
  been applied. A recommendation ignored under one profile shows again
  under another, where it may well apply.
- **Per project.** Ignored while the dashboard shows one project, it's
  ignored in that project only (keyed by the project as the dashboard
  names it, the redacted slug). Ignored in the every-project view, it's
  stored under ``*`` and ignored in every project. A project's own entry
  is checked first, then ``*``.
- **It comes back when it changes.** An entry holds the recommendation's
  :func:`fingerprint`: its rule and the changes it suggests (target,
  agent, key, value and scope), never its saving, evidence or wording,
  so moving numbers don't bring it back but a new value does. The
  entry's ``changes`` keep what was ignored, for "shown again because it
  now suggests ...".

Only the dashboard's list reads this (``service/api.py``'s
``/api/recommendations`` annotates each row, and the profile goals skip
what's ignored), and ``coaching.py``, which leaves an ignored run-split
or plan-handoff tip out of the coaching notes. ``/api/report.json`` and
the CLI reports stay complete.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from .config import _write_atomic

FILENAME = "ignored-recommendations.json"
#: The profile bucket while no profile has been applied.
NO_PROFILE = "none"
#: The project bucket for an ignore made in the every-project view.
ALL_PROJECTS = "*"
_VERSION = 1

# The dashboard is served by a threaded server: one read-modify-write
# at a time.
_LOCK = threading.Lock()


def active_profile(config_dir: str | Path) -> str:
    """The id ``apply <profile>`` last marked active, or ``none``."""
    try:
        text = (Path(config_dir) / "active-profile").read_text(encoding="utf-8").strip()
    except OSError:
        return NO_PROFILE
    return text or NO_PROFILE


def _changes(rec) -> list[dict]:
    return [
        {
            "target": getattr(change, "target", ""),
            "agent": getattr(change, "agent", None),
            "key": getattr(change, "key", ""),
            "value": getattr(change, "value", None),
            "scope": getattr(change, "scope", "") or getattr(rec, "scope", ""),
        }
        for change in getattr(rec, "changes", ()) or ()
    ]


def fingerprint(rec) -> str:
    """What makes this recommendation the same one: its rule and the
    changes it suggests, or for one without changes its rule, agent type,
    lever and subject (left out when empty, so a recommendation without
    one keeps the fingerprint it always had). Savings, evidence and
    wording are left out."""
    changes = sorted(json.dumps(change, sort_keys=True, default=str) for change in _changes(rec))
    if changes:
        payload = {"id": rec.id, "changes": changes}
    else:
        payload = {"id": rec.id, "agent_type": rec.agent_type, "lever": rec.lever}
        if getattr(rec, "subject", ""):
            payload["subject"] = rec.subject
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _path(config_dir: str | Path) -> Path:
    return Path(config_dir) / FILENAME


def load(config_dir: str | Path) -> dict:
    """Every stored ignore, as ``{profile: {project: {key: entry}}}``.
    Empty when the file is missing or unreadable: an ignore is a
    convenience, never a reason to fail."""
    try:
        data = json.loads(_path(config_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    profiles = data.get("profiles") if isinstance(data, dict) else None
    if not isinstance(profiles, dict):
        return {}
    return {
        str(profile): {
            str(project): {str(key): entry for key, entry in entries.items() if isinstance(entry, dict)}
            for project, entries in projects.items()
            if isinstance(entries, dict)
        }
        for profile, projects in profiles.items()
        if isinstance(projects, dict)
    }


def _save(config_dir: str | Path, profiles: dict) -> None:
    Path(config_dir).mkdir(parents=True, exist_ok=True)
    text = json.dumps({"version": _VERSION, "profiles": profiles}, indent=2, sort_keys=True, default=str) + "\n"
    _write_atomic(_path(config_dir), text)


def _match(projects: dict, key: str, project: str | None) -> tuple[str, dict] | None:
    """The entry that applies to ``key`` in ``project``: the project's own
    first, then every project's."""
    for bucket in ([project] if project else []) + [ALL_PROJECTS]:
        entry = projects.get(bucket, {}).get(key)
        if entry is not None:
            return bucket, entry
    return None


def annotate(recs, stored: dict, profile: str, project: str | None) -> list[dict]:
    """For each of ``recs``, the fields the dashboard adds to its row:

    - ``ignored``: ignored here, and it still suggests what was ignored;
    - ``ignored_at``: when (ISO-8601, UTC), else ``None``;
    - ``ignored_in``: ``"project"`` or ``"all"`` (every project);
    - ``ignored_before``: ``{"ignored_at", "changes"}`` when it was
      ignored here but now suggests something else, so it shows again.
    """
    projects = stored.get(profile, {})
    marks = []
    for rec in recs:
        found = _match(projects, rec.key, project)
        mark = {"ignored": False, "ignored_at": None, "ignored_in": None, "ignored_before": None}
        if found is not None:
            bucket, entry = found
            if entry.get("fingerprint") == fingerprint(rec):
                mark.update(
                    ignored=True,
                    ignored_at=entry.get("ignored_at"),
                    ignored_in="all" if bucket == ALL_PROJECTS else "project",
                )
            else:
                mark["ignored_before"] = {"ignored_at": entry.get("ignored_at"), "changes": entry.get("changes") or []}
        marks.append(mark)
    return marks


def skip_keys(config_dir: str | Path, recs, project: str | None) -> frozenset[str]:
    """The keys of ``recs`` ignored here that still suggest what was
    ignored: the profile goals leave these out."""
    marks = annotate(recs, load(config_dir), active_profile(config_dir), project)
    return frozenset(rec.key for rec, mark in zip(recs, marks) if mark["ignored"])


def set_ignored(
    config_dir: str | Path,
    recs,
    *,
    ignored: bool,
    project: str | None,
    now: datetime | None = None,
) -> str:
    """Ignore each of ``recs`` in ``project`` (every project when
    ``None``) under the active profile, or stop ignoring them. Stopping
    removes whichever entry applied, so stopping an every-project ignore
    from one project's view stops it everywhere. Returns the profile id."""
    profile = active_profile(config_dir)
    stamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    with _LOCK:
        stored = load(config_dir)
        projects = stored.setdefault(profile, {})
        for rec in recs:
            if ignored:
                projects.setdefault(project or ALL_PROJECTS, {})[rec.key] = {
                    "fingerprint": fingerprint(rec),
                    "ignored_at": stamp,
                    "title": rec.title,
                    "changes": [
                        {"agent": change["agent"], "key": change["key"], "value": change["value"]}
                        for change in _changes(rec)
                    ],
                }
                continue
            found = _match(projects, rec.key, project)
            if found is not None:
                del projects[found[0]][rec.key]
        # Nothing left under a project or profile: drop it.
        for bucket in [name for name, entries in projects.items() if not entries]:
            del projects[bucket]
        if not projects:
            del stored[profile]
        _save(config_dir, stored)
    return profile


__all__ = [
    "ALL_PROJECTS",
    "FILENAME",
    "NO_PROFILE",
    "active_profile",
    "annotate",
    "fingerprint",
    "load",
    "set_ignored",
    "skip_keys",
]

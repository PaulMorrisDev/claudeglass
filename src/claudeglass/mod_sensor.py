"""Reads the files the ClaudeGlass mod writes about what it showed:
``<config_dir>/mod/sensor/<session id>.json``.

The mod runs inside Claude Code and can't append to a file, so it rewrites
a session's whole file at each turn's end, with its newest
``capture_catalogue.MOD_SENSOR_MAX_EVENTS`` events::

    {"schema": 1, "session_id": "...", "version": "...", "events": [...]}

Each event is ``{"ts": <epoch seconds>, "event": <word>, ...}`` plus the
closed words and counts its event takes (``capture_catalogue.SENSOR_VOCAB``;
the events are described there). The file lives outside the mod's reach of
Claude and is read by the dashboard's watcher (``service/watcher.py``,
``_scan_mod_sensor``) into the ``mod_events`` table.

Privacy: the file is the mod's, not ours, so nothing in it is trusted.
:func:`clean` keeps an event only when its event word, its every word of
``kind``, ``surface`` and ``outcome`` and its counts are exactly what the
vocabulary lists, and keeps nothing else it carries; the session id must be
the file's own name and shaped like a session id. Anything else is dropped
whole, so a stray message, path or summary can never reach the store.

:func:`load` reads one file, :func:`files` lists them, :func:`prune`
removes the old ones. The mod's heartbeat files
(``<config_dir>/mod/present/<session id>.json``, read by the capture hook)
are not read here; :func:`prune_present` removes the ones left behind.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from .capture_catalogue import (
    MOD_DIR,
    MOD_PRESENT_DIR,
    MOD_PRESENT_PRUNE_S,
    MOD_SENSOR_DIR,
    MOD_SENSOR_MAX_EVENTS,
    SENSOR_COUNT_FIELDS,
    SENSOR_VOCAB,
)

#: The only ``schema`` a reader understands; another is ignored whole.
SCHEMA = 1

#: What a session id looks like: Claude Code's UUIDs, and any id made of
#: letters, digits, ``-`` and ``_`` up to a length no real one passes.
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")

#: The span of times an event may claim (UTC, seconds since the epoch): from
#: 2020 to 2100. A time outside it is no time of ours.
_TS_MIN = 1_577_836_800
_TS_MAX = 4_102_444_800

#: The largest count a compaction event may carry (messages, files or
#: constraints a summary kept or lost: no real review passes it).
_COUNT_MAX = 1_000_000

#: The most bytes of a sensor file that is read: 500 events of a few dozen
#: bytes each are about 50 KB.
_MAX_BYTES = 1_000_000


def sensor_dir(config_dir: str | Path) -> Path:
    """``<config_dir>/mod/sensor``."""
    return Path(config_dir) / MOD_DIR / MOD_SENSOR_DIR


def files(config_dir: str | Path) -> list[Path]:
    """The sensor files in the folder, oldest name first; none when there
    is no folder."""
    try:
        return sorted(p for p in sensor_dir(config_dir).glob("*.json") if p.is_file())
    except OSError:
        return []


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _count(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _COUNT_MAX:
        return None
    return value


def clean_event(raw) -> dict | None:
    """One event as the store keeps it, or ``None`` when it is not exactly
    what the vocabulary allows: ``{"ts": "<UTC ISO time>", "event", "kind",
    "surface", "outcome", "kept", "missing", "added"}`` with ``None`` for
    each field the event doesn't take. Every field the event takes must be
    there and in its list, and a field it doesn't take is left out, not
    kept."""
    if not isinstance(raw, dict):
        return None
    vocab = SENSOR_VOCAB.get(raw.get("event")) if isinstance(raw.get("event"), str) else None
    ts = _number(raw.get("ts"))
    if vocab is None or ts is None or not _TS_MIN <= ts <= _TS_MAX:
        return None
    out: dict = {
        "ts": datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "event": raw["event"],
        "kind": None,
        "surface": None,
        "outcome": None,
        "kept": None,
        "missing": None,
        "added": None,
    }
    for field, words in vocab.items():
        word = raw.get(field)
        if not isinstance(word, str) or word not in words:
            return None
        out[field] = word
    for field in SENSOR_COUNT_FIELDS.get(raw["event"], ()):
        value = _count(raw.get(field))
        if value is None:
            return None
        out[field] = value
    return out


def clean(raw_events) -> list[dict]:
    """The events of a file that :func:`clean_event` keeps, in file order,
    at most the newest :data:`MOD_SENSOR_MAX_EVENTS` of them."""
    if not isinstance(raw_events, list):
        return []
    kept = [event for event in map(clean_event, raw_events[-MOD_SENSOR_MAX_EVENTS:]) if event is not None]
    return kept


def load(path: str | Path) -> tuple[str, list[dict]] | None:
    """``(session id, cleaned events)`` from one sensor file, or ``None``
    for a file that is missing, too big, not JSON, of another schema, or
    whose ``session_id`` is not shaped like one or is not its own name."""
    path = Path(path)
    try:
        if path.stat().st_size > _MAX_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        return None
    session_id = data.get("session_id")
    if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id) or session_id != path.stem:
        return None
    return session_id, clean(data.get("events"))


def prune(config_dir: str | Path, retention_days: int, now: float | None = None) -> int:
    """Removes the sensor files not written for ``retention_days`` days: a
    session that old has nothing more to say, and what they held is already
    in the store. Returns how many went. Like the other telemetry files
    (``signals.prune``), it runs whether or not a retention was set."""
    cutoff = (time.time() if now is None else now) - retention_days * 86400
    removed = 0
    for path in files(config_dir):
        try:
            if path.stat().st_mtime < cutoff:
                os.unlink(path)
                removed += 1
        except OSError:
            continue
    return removed


def prune_present(config_dir: str | Path, now: float | None = None) -> int:
    """Removes the heartbeat files in ``<config_dir>/mod/present`` not
    written for ``capture_catalogue.MOD_PRESENT_PRUNE_S`` (24 hours): the
    mod refreshes one every minute and, as the engine cannot delete a file,
    writes it a last time with ``seen`` at 0 when its session ends, so one
    that old is a session long over (the dashboard clears them away).
    Returns how many went."""
    cutoff = (time.time() if now is None else now) - MOD_PRESENT_PRUNE_S
    removed = 0
    try:
        found = sorted(p for p in (Path(config_dir) / MOD_DIR / MOD_PRESENT_DIR).glob("*.json") if p.is_file())
    except OSError:
        return 0
    for path in found:
        try:
            if path.stat().st_mtime < cutoff:
                os.unlink(path)
                removed += 1
        except OSError:
            continue
    return removed

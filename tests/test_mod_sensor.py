"""``mod_sensor``: reading the ClaudeGlass mod's sensor files.

The file is the mod's, so every test here is about what is kept from it
and what is not: only the closed words and counts of
``capture_catalogue.SENSOR_VOCAB``, for a session whose id is the file's own
name.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from claudeglass import capture_catalogue as cat
from claudeglass import mod_sensor

from helpers import assert_privacy

TS = 1_790_000_000  # 2026-09-21T14:13:20Z


def _event(event: str, **fields) -> dict:
    return {"ts": TS, "event": event, **fields}


PRESENT = _event("present", surface="desktop")
NOTE = _event("note", kind="cold_return", surface="hint", outcome="none")
REBUILD = _event("rebuild", kind="expiry", outcome="expected")
COLD = _event("cold_send", outcome="warned")
COMPACTION = _event("compaction", kind="auto", kept=12, missing=2, added=1)


def _write(tmp_path: Path, session_id: str, events, *, schema=1, name=None, **extra) -> Path:
    folder = tmp_path / cat.MOD_DIR / cat.MOD_SENSOR_DIR
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name or session_id}.json"
    path.write_text(
        json.dumps({"schema": schema, "session_id": session_id, "version": "1.0.0", "events": events, **extra}),
        encoding="utf-8",
    )
    return path


def test_the_vocabulary_is_the_catalogues_and_names_every_event():
    assert tuple(cat.SENSOR_VOCAB) == cat.SENSOR_EVENTS
    assert set(cat.SENSOR_COUNT_FIELDS) <= set(cat.SENSOR_VOCAB)
    # A note can be about any coaching hint, any rating reminder, or one of the mod's own.
    assert set(cat.MOD_NOTE_KINDS) <= set(cat.SENSOR_NOTE_KINDS)
    assert set(cat.COACHING_HINTS) <= set(cat.SENSOR_NOTE_KINDS)


@pytest.mark.parametrize("raw", [PRESENT, NOTE, REBUILD, COLD, COMPACTION])
def test_an_event_of_every_kind_is_kept_with_a_utc_time(raw):
    kept = mod_sensor.clean_event(raw)
    assert kept is not None
    assert kept["ts"] == "2026-09-21T14:13:20Z" and kept["event"] == raw["event"]
    for field in ("kind", "surface", "outcome", "kept", "missing", "added"):
        assert kept[field] == raw.get(field)


@pytest.mark.parametrize("kind", ["auto", "manual", "plugin"])
def test_a_compaction_of_every_trigger_that_is_reviewed_is_kept(kind):
    # A summary a plugin starts is reviewed like the others; one made ahead (precompute) never reaches the sensor.
    kept = mod_sensor.clean_event(_event("compaction", kind=kind, kept=3, missing=1, added=0))
    assert kept is not None and kept["kind"] == kind
    assert mod_sensor.clean_event(_event("compaction", kind="precompute", kept=3, missing=1, added=0)) is None


def test_a_note_of_every_hint_and_every_mod_kind_is_kept():
    for kind in cat.SENSOR_NOTE_KINDS:
        assert mod_sensor.clean_event(_event("note", kind=kind, surface="note", outcome="sent")) is not None, kind


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "note",
        [],
        {},
        _event("surprise"),
        _event("present", surface="toaster"),
        _event("present"),
        _event("note", kind="runway", surface="hint", outcome="maybe"),
        _event("note", kind="a message from Paul", surface="hint", outcome="sent"),
        _event("note", kind="runway", surface="C:\\Users\\paul", outcome="sent"),
        _event("note", kind=["runway"], surface="hint", outcome="sent"),
        _event("rebuild", kind="expiry", outcome="surprising"),
        _event("cold_send", outcome="warned-ish"),
        _event("compaction", kind="auto", kept=-1, missing=0, added=0),
        _event("compaction", kind="auto", kept=1.5, missing=0, added=0),
        _event("compaction", kind="auto", kept=True, missing=0, added=0),
        _event("compaction", kind="auto", kept="12", missing=0, added=0),
        _event("compaction", kind="auto", kept=10**9, missing=0, added=0),
        _event("compaction", kind="auto", kept=1, missing=0),
        _event("compaction", kind="summary text", kept=1, missing=0, added=0),
        {"event": "present", "surface": "desktop"},
        {"ts": "now", "event": "present", "surface": "desktop"},
        {"ts": True, "event": "present", "surface": "desktop"},
        {"ts": float("nan"), "event": "present", "surface": "desktop"},
        {"ts": 5, "event": "present", "surface": "desktop"},
        {"ts": 10**12, "event": "present", "surface": "desktop"},
    ],
    ids=lambda raw: repr(raw)[:60],
)
def test_an_event_outside_the_vocabulary_is_dropped_whole(raw):
    assert mod_sensor.clean_event(raw) is None


def test_extra_fields_never_reach_the_kept_event():
    raw = _event("note", kind="runway", surface="hint", outcome="sent", text="please fix my login", path="C:\\Users\\paul")
    kept = mod_sensor.clean_event(raw)
    assert kept is not None and set(kept) == {"ts", "event", "kind", "surface", "outcome", "kept", "missing", "added"}
    # A field the event doesn't take is dropped even when it is a valid word of another event.
    assert mod_sensor.clean_event(_event("present", surface="desktop", outcome="sent"))["outcome"] is None
    assert_privacy(kept)


def test_clean_keeps_the_good_events_in_order_and_the_newest_500():
    good = [_event("present", surface="terminal"), "junk", _event("cold_send", outcome="unwarned")]
    assert [e["event"] for e in mod_sensor.clean(good)] == ["present", "cold_send"]
    assert mod_sensor.clean("nope") == [] and mod_sensor.clean(None) == []
    many = [_event("compaction", kind="manual", kept=n, missing=0, added=0) for n in range(cat.MOD_SENSOR_MAX_EVENTS + 40)]
    kept = mod_sensor.clean(many)
    assert len(kept) == cat.MOD_SENSOR_MAX_EVENTS and kept[0]["kept"] == 40


def test_load_returns_the_session_and_its_cleaned_events(tmp_path):
    path = _write(tmp_path, "sess-1", [PRESENT, "junk", NOTE])
    session_id, events = mod_sensor.load(path)
    assert session_id == "sess-1" and [e["event"] for e in events] == ["present", "note"]
    assert mod_sensor.files(tmp_path) == [path]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda tmp: _write(tmp, "sess-1", [PRESENT], schema=2),
        lambda tmp: _write(tmp, "sess-1", [PRESENT], schema="1"),
        # The session id has to be the file's own name: a file can't speak for another session.
        lambda tmp: _write(tmp, "sess-2", [PRESENT], name="sess-1"),
        lambda tmp: _write(tmp, "../escape", [PRESENT], name="sess-1"),
        lambda tmp: _write(tmp, "", [PRESENT], name="sess-1"),
        lambda tmp: _write(tmp, "s" * 80, [PRESENT], name="s" * 80),
        lambda tmp: _write(tmp, "sess 1", [PRESENT], name="sess 1"),
    ],
)
def test_load_refuses_a_file_that_is_not_what_the_mod_writes(tmp_path, mutate):
    path = mutate(tmp_path)
    assert mod_sensor.load(path) is None


def test_load_refuses_a_file_that_is_missing_big_or_not_json(tmp_path):
    folder = tmp_path / cat.MOD_DIR / cat.MOD_SENSOR_DIR
    folder.mkdir(parents=True)
    assert mod_sensor.load(folder / "gone.json") is None
    (folder / "bad.json").write_text("{not json", encoding="utf-8")
    assert mod_sensor.load(folder / "bad.json") is None
    (folder / "list.json").write_text("[1, 2]", encoding="utf-8")
    assert mod_sensor.load(folder / "list.json") is None
    (folder / "big.json").write_text(" " * (mod_sensor._MAX_BYTES + 1), encoding="utf-8")
    assert mod_sensor.load(folder / "big.json") is None
    # A file with no events list is a file with nothing to say, not a bad one.
    (folder / "empty.json").write_text(json.dumps({"schema": 1, "session_id": "empty"}), encoding="utf-8")
    assert mod_sensor.load(folder / "empty.json") == ("empty", [])


def test_files_is_empty_without_a_folder(tmp_path):
    assert mod_sensor.files(tmp_path) == []


def test_prune_present_removes_only_the_heartbeat_files_older_than_a_day(tmp_path):
    folder = tmp_path / cat.MOD_DIR / cat.MOD_PRESENT_DIR
    folder.mkdir(parents=True)
    now = time.time()
    made = {}
    for name, age in (("old", 25 * 3600), ("new", 23 * 3600), ("fresh", 0)):
        made[name] = folder / f"{name}.json"
        made[name].write_text(json.dumps({"schema": 1, "session_id": name, "seen": now - age}), encoding="utf-8")
        os.utime(made[name], (now - age, now - age))
    other = folder / "notes.txt"
    other.write_text("not a heartbeat", encoding="utf-8")
    os.utime(other, (now - 40 * 3600, now - 40 * 3600))
    assert mod_sensor.prune_present(tmp_path, now) == 1
    assert not made["old"].exists() and made["new"].exists() and made["fresh"].exists() and other.exists()
    assert mod_sensor.prune_present(tmp_path, now) == 0


def test_prune_present_without_a_folder_removes_nothing(tmp_path):
    assert mod_sensor.prune_present(tmp_path) == 0
    # The sensor files are not heartbeats: they go by the retention, not after a day.
    old = _write(tmp_path, "old", [PRESENT])
    ago = time.time() - 3 * 86400
    os.utime(old, (ago, ago))
    assert mod_sensor.prune_present(tmp_path) == 0 and old.exists()


def test_prune_removes_only_the_files_not_written_for_the_retention(tmp_path):
    old = _write(tmp_path, "old", [PRESENT])
    new = _write(tmp_path, "new", [PRESENT])
    ago = time.time() - 40 * 86400
    os.utime(old, (ago, ago))
    assert mod_sensor.prune(tmp_path, 30) == 1
    assert not old.exists() and new.exists()
    assert mod_sensor.prune(tmp_path, 30) == 0

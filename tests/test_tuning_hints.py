"""The ``hint_occasions`` block of the tuning export (``tuning._hint_occasions``):
whether a live coaching hint could have fired, and how many of those moments
got a tip. The moments themselves are tried in ``tests/test_hint_occasions.py``;
this holds the block's wiring in ``build``, its place in the spec and its
lines in the summary. It reuses the world on disk of ``tests/test_tuning.py``.
"""

from __future__ import annotations

import copy
import os
import shutil
from datetime import timedelta

import pytest

from claudeglass import hint_occasions as hint_occasions_mod
from claudeglass import tuning
from claudeglass.config import CaptureConfig, Config
from claudeglass.corpus import load_corpus

from helpers import assert_privacy_deep
from test_tuning import BASE, _build, _plain, _put, _salt, _sample, world  # noqa: F401 (fixtures)


def _notes_on() -> Config:
    return Config(tz="UTC", capture=CaptureConfig(level="standard", coaching=["coaching_line", "coaching_notes"]))


def _setup_before_the_sessions(world) -> None:
    """The world's settings.json is as old as the sessions, not as new as this run."""
    stamp = (BASE - timedelta(days=2)).timestamp()
    os.utime(world.claude_root / "settings.json", (stamp, stamp))


def _zeros() -> dict:
    return dict.fromkeys(("occasions", "sessions", "noted", "notes", "glass", "mod_sessions", "older"), 0)


def test_the_hint_occasions_block_is_left_out_while_coaching_notes_is_off(world):
    assert "hint_occasions" not in _build(world)
    doc = _build(world, config=_notes_on())
    block = doc["hint_occasions"]
    assert set(block) == {"on_hours", "hints", "muted", "silent"}
    assert list(block["hints"]) == list(hint_occasions_mod.HINTS)
    assert all(set(row) == set(_zeros()) for row in block["hints"].values())
    names = list(doc)
    assert names.index("compactions") < names.index("hint_occasions")
    assert tuning.validate(doc) == []
    assert tuning.loads(tuning.dumps(doc)) == doc


def test_the_hint_occasions_block_counts_the_worlds_drip_run(world):
    _setup_before_the_sessions(world)
    doc = _build(world, config=_notes_on())
    block = doc["hint_occasions"]
    # Four small edits two minutes apart: the first has no message of yours before it to be quick after, so
    # only the fourth makes three in a row.
    assert block["hints"]["drip_feed"] == {**_zeros(), "occasions": 1, "sessions": 1}
    assert block["hints"]["big_paste"] == _zeros() and block["hints"]["status_poll"] == _zeros()
    assert block["muted"] == []
    # One session is not yet a missing tip.
    assert block["silent"] == []
    # The after-the-fact count of the same messages is still one run.
    assert doc["pieces"]["drip_runs"]["by_length"] == {"three": 1}
    assert block["on_hours"] >= 24 * 30
    assert tuning.validate(doc) == []
    assert_privacy_deep(doc)


def test_the_hint_occasions_block_names_a_hint_that_two_sessions_had_a_moment_for_and_got_no_tip(world, tmp_path):
    _setup_before_the_sessions(world)
    beta = world.root / "projects" / "proj-beta"
    shutil.copy(beta / "s4.jsonl", beta / "s4-again.jsonl")
    corpus = load_corpus([world.root / "projects" / "proj-alpha", beta])
    doc = _build(world, corpus=corpus, config=_notes_on())
    block = doc["hint_occasions"]
    assert block["hints"]["drip_feed"] == {**_zeros(), "occasions": 2, "sessions": 2}
    assert block["silent"] == ["drip_feed"]
    assert tuning.validate(doc) == []
    assert_privacy_deep(doc)
    # The mod's own note for it, in the window, is a tip given.
    sessions = {b.session_id for b in corpus.sessions}
    assert {"s4", "s4-again"} <= sessions
    shown = [{"session_id": "s4", "ts": int(BASE.timestamp()) + 20000, "event": "note", "kind": "drip_feed",
              "surface": "hint", "outcome": "sent"}]
    doc = _build(world, corpus=corpus, config=_notes_on(), mod_events=shown)
    assert doc["hint_occasions"]["hints"]["drip_feed"]["glass"] == 1
    assert doc["hint_occasions"]["silent"] == []


def test_a_file_without_the_hint_occasions_block_still_passes_and_a_bad_one_fails():
    doc = _sample()
    assert "hint_occasions" in doc
    older = {name: value for name, value in doc.items() if name != "hint_occasions"}
    assert tuning.validate(older) == []
    assert tuning.summary_text(older)
    assert "Coaching moment" not in tuning.summary_text(older)
    for path, value, check in (
        ("hint_occasions.hints", {**doc["hint_occasions"]["hints"], "cold_return": _zeros()}, "hint_occasions.hints: has a key"),
        ("hint_occasions.hints.big_paste.sessions", -1, "sessions: must be 0 or more"),
        ("hint_occasions.hints.big_paste.noted", None, "noted: is missing"),
        ("hint_occasions.silent", ["cold_return"], "hint_occasions.silent[0]: must be one of the words"),
        ("hint_occasions.muted", ["not_a_hint"], "hint_occasions.muted[0]: must be one of the words"),
        ("hint_occasions.hints", [], "hint_occasions.hints: must be an object"),
        ("hint_occasions.on_hours", 1.5, "on_hours: must be a whole number"),
    ):
        bad = copy.deepcopy(doc)
        if value is None:
            del bad["hint_occasions"]["hints"]["big_paste"]["noted"]
        else:
            _put(bad, path, value)
        assert any(check in problem for problem in tuning.validate(bad)), (path, tuning.validate(bad))
    # Every hint is there in a document, zeros included.
    assert doc["hint_occasions"]["hints"].keys() == set(hint_occasions_mod.HINTS)


def test_the_summary_says_the_coaching_moments_in_plain_lines():
    doc = _sample()
    block = {
        "on_hours": 720,
        "hints": {
            "drip_feed": {**_zeros(), "occasions": 5, "sessions": 3, "noted": 2, "notes": 2},
            "big_paste": _zeros(),
            "status_poll": {**_zeros(), "occasions": 2, "sessions": 1, "mod_sessions": 2, "older": 1},
        },
        "muted": [],
        "silent": ["status_poll"],
    }
    doc["hint_occasions"] = block
    lines = tuning.summary_text(doc).splitlines()
    assert "Coaching moment, small requests: 3 sessions had one, and 2 of them got a tip." in lines
    assert "Coaching moment, a status check: 1 session had one, and 0 of them got a tip." in lines
    assert not any(line.startswith("Coaching moment, a big paste") for line in lines)
    assert "2 more sessions had the mod running, which may show these tips itself." in lines
    assert "1 session left out of status checks: an older version read them and their transcripts are gone." in lines
    assert "The Capture page would say no tip came for a status check." in lines
    none = copy.deepcopy(doc)
    none["hint_occasions"] = {"on_hours": 5, "hints": {h: _zeros() for h in hint_occasions_mod.HINTS}, "muted": [], "silent": []}
    quiet = tuning.summary_text(none).splitlines()
    assert "Coaching moments: none in the window." in quiet
    assert not any("mod running" in line or "left out of status" in line or "Capture page would" in line for line in quiet)
    two = copy.deepcopy(doc)
    two["hint_occasions"]["silent"] = ["drip_feed", "big_paste", "status_poll"]
    assert (
        "The Capture page would say no tip came for small requests and a big paste and a status check."
        in tuning.summary_text(two).splitlines()
    )
    for text in (tuning.summary_text(doc), tuning.summary_text(none), tuning.summary_text(two)):
        for line in text.splitlines():
            _plain(line)


def test_the_hint_occasions_hints_are_the_catalogues_live_hints():
    spec = tuning.spec().fields["hint_occasions"]
    assert set(spec.fields["hints"].keys.words) == set(hint_occasions_mod.HINTS)
    assert set(spec.fields["muted"].item.words) == set(spec.fields["silent"].item.words) == set(hint_occasions_mod.HINTS)
    assert spec.required == ("on_hours", "hints", "muted", "silent")
    assert tuning._BLOCKS["hint_occasions"][2] is False

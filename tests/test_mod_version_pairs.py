"""The mod and the capture hook share two files: the mod's settings (``mod/settings.json``, which the package
writes) and the mod's present file (``mod/present/<session>.json``, which the mod writes and the hook reads
through ``capture_hook._moved``). Either side can be older than the other, so each pairing is a test.

The mod's side of a pair is the plugin's own tests (``plugin/tests/files.test.tsx``: the present file's
``shows`` follows whether coaching applies; ``plugin/tests/coach.test.tsx``: no hint is drawn with coaching
off, and a muted kind is not drawn). The hook's side is ``_moved`` reading a present file, written here in
the shape each copy of the mod writes it.

| Mod | Package | Expected |
|---|---|---|
| new | new | the mod lists a surface only while coaching applies, and the hook keeps every hint the mod does not list |
| 0.16.0 or older | new | a file without ``shows`` moves the whole set; ``muted`` carries the off hints |
| new | 0.16.0 | with coaching on the mod shows the hints; with it off neither side does |
| 0.16.0 or older | 0.16.0 | unchanged |
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest

from claudeglass import capture_catalogue, mod_settings
from claudeglass.hooks import capture_hook

NOW = datetime(2026, 11, 12, 12, 0, 0, tzinfo=timezone.utc)

#: The hints the hook leaves to the mod when it moves them (0.16.0 moved all five while the mod's file was fresh).
THE_WHOLE_SET = frozenset({"cold_return", "status_poll", "drip_feed", "big_paste", "plan_fresh"})
HINT_LINE_HINTS = frozenset({"cold_return", "status_poll", "drip_feed", "big_paste"})

_WRITES = [0]


def _config(tmp_path, toml: str = "") -> object:
    folder = tmp_path / "claudeglass"
    folder.mkdir(parents=True)
    if toml:
        (folder / "config.toml").write_text(toml, encoding="utf-8")
    return folder


def _present(folder, **body) -> None:
    """The mod's present file for session ``s1``, seen just now, with ``body`` as the keys the mod wrote."""
    directory = folder / capture_catalogue.MOD_DIR / capture_catalogue.MOD_PRESENT_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "s1.json"
    path.write_text(json.dumps({"schema": 1, "session_id": "s1", "seen": NOW.timestamp(), **body}), encoding="utf-8")
    # The hook remembers a file by its time and size: give each write its own time.
    _WRITES[0] += 1
    os.utime(path, (1_700_000_000 + _WRITES[0], 1_700_000_000 + _WRITES[0]))


def _moved(folder) -> frozenset:
    return capture_hook._moved(folder, capture_hook.load_catalogue(), "s1", NOW.timestamp())


# -- new mod, new package ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "shows, left_to_the_mod",
    [
        ([], frozenset()),
        (["hint"], HINT_LINE_HINTS),
        (["status"], frozenset({"plan_fresh"})),
        (["hint", "status"], THE_WHOLE_SET),
    ],
)
def test_the_hook_keeps_every_hint_a_new_mod_does_not_list(tmp_path, shows, left_to_the_mod):
    folder = _config(tmp_path)
    _present(folder, shows=shows, active=0)
    assert _moved(folder) == left_to_the_mod


def test_a_new_mod_with_coaching_off_writes_an_empty_list_and_the_hook_keeps_all_five(tmp_path):
    # What the mod's settings say decides the list (plugin/tests/files.test.tsx); here, that the package says
    # coaching is off until coaching notes are on, and that an empty list leaves every hint with the hook.
    off = _config(tmp_path / "off")
    assert mod_settings.build(off, now=NOW)["coaching"]["enabled"] is False
    _present(off, shows=[])
    assert _moved(off) == frozenset()
    on = _config(tmp_path / "on", '[capture]\ncoaching = ["coaching_notes"]\n')
    assert mod_settings.build(on, now=NOW)["coaching"]["enabled"] is True
    _present(on, shows=["hint", "status"])
    assert _moved(on) == THE_WHOLE_SET


# -- an older mod, new package -----------------------------------------------------------------------------------


def test_a_file_without_shows_moves_the_whole_set(tmp_path):
    folder = _config(tmp_path)
    _present(folder)
    assert "shows" not in json.loads(
        (folder / capture_catalogue.MOD_DIR / capture_catalogue.MOD_PRESENT_DIR / "s1.json").read_text(encoding="utf-8")
    )
    assert _moved(folder) == THE_WHOLE_SET == frozenset(capture_catalogue.MOVED_TO_MOD)


def test_muted_carries_the_off_hints_so_an_older_mod_stays_silent_for_them(tmp_path):
    muted = mod_settings.build(_config(tmp_path), now=NOW)["coaching"]["muted"]
    assert set(capture_catalogue.LIVE_OFF_HINTS) <= set(muted)
    # The two the older mod drew on its own, and the package turned off live.
    assert {"cold_return", "plan_fresh"} <= set(muted)


# -- a new mod, a 0.16.0 package ---------------------------------------------------------------------------------


def test_what_a_new_mod_reads_is_the_same_settings_a_0_16_0_package_writes(tmp_path):
    # The block's keys a new mod reads (`enabled`, `projects`, `exclude_projects`, `muted`, `once`,
    # `plan_fresh`, `thresholds`) are the ones 0.16.0 wrote, so a new mod with that package lists the hints
    # while coaching is on and nothing while it is off: with it off neither side shows a hint, no worse than
    # 0.16.0 alone.
    block = mod_settings.build(_config(tmp_path), now=NOW)["coaching"]
    assert {"enabled", "projects", "exclude_projects", "plan_fresh", "thresholds", "muted", "once"} <= set(block)


# -- an older mod, a 0.16.0 package ------------------------------------------------------------------------------


def test_an_older_mods_file_moves_the_set_0_16_0_moved(tmp_path):
    # Unchanged: the hook given a file with no ``shows`` moves the five hints 0.16.0 moved.
    folder = _config(tmp_path)
    _present(folder)
    assert _moved(folder) == THE_WHOLE_SET

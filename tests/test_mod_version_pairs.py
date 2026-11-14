"""The mod and the capture hook share two files: the mod's settings (``mod/settings.json``, which the package
writes) and the mod's present file (``mod/present/<session>.json``, which the mod writes and the hook reads
through ``capture_hook._moved``). Either side can be older than the other, so each pairing is a test.

The mod's side of a pair is the plugin's own tests (``plugin/tests/files.test.tsx``: the present file's
``shows`` follows whether coaching applies; ``plugin/tests/coach.test.tsx``: no hint is drawn with coaching
off, and a muted kind is not drawn). The hook's side is ``_moved`` reading a present file, written here in
the shape each copy of the mod writes it.

The rows that name the 0.16.0 package run that release's own hook and ``mod_settings``, taken from the
``v0.16.0`` tag (they are skipped where git or the tag is not available). The other rows run this build's code.

| Mod | Package | Expected |
|---|---|---|
| new | new | the mod lists a surface only while coaching applies, and the hook keeps every hint the mod does not list |
| 0.16.0 or older | new | a file without ``shows`` moves the whole set; ``muted`` carries the off hints |
| new | 0.16.0 | with coaching on the mod shows the hints; with it off neither side does |
| 0.16.0 or older | 0.16.0 | unchanged |
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

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
# The 0.16.0 side is that release's own code: its hook module and its mod_settings, taken from the v0.16.0 tag
# with `git archive` and run in a child Python of their own, so these rows fail if either side of the pair moves.

REPO = Path(__file__).resolve().parents[1]
_OLD_RUN = """
import json, sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from datetime import datetime, timezone
folder, now = Path(sys.argv[2]), float(sys.argv[3])
what = sys.argv[4]
if what == "moved":
    from claudeglass.hooks import capture_hook
    print(json.dumps(sorted(capture_hook._moved(folder, capture_hook.load_catalogue(), "s1", now))))
else:
    from claudeglass import mod_settings
    data = mod_settings.build(folder, now=datetime.fromtimestamp(now, timezone.utc))
    print(json.dumps(data["coaching"]))
"""


@pytest.fixture(scope="module")
def v016(tmp_path_factory):
    """The ``src`` folder of v0.16.0, or a skip when git or the tag is not there."""
    try:
        subprocess.run(["git", "-C", str(REPO), "rev-parse", "--verify", "v0.16.0^{commit}"], check=True, capture_output=True)
        archive = subprocess.run(
            ["git", "-C", str(REPO), "archive", "--format=tar", "v0.16.0", "src/claudeglass"], check=True, capture_output=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("the v0.16.0 tag, or git, is not available here")
    folder = tmp_path_factory.mktemp("v016")
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(folder)
    return folder / "src"


def _old(src, folder, what: str):
    done = subprocess.run(
        [sys.executable, "-I", "-c", _OLD_RUN, str(src), str(folder), str(NOW.timestamp()), what],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return json.loads(done.stdout)


@pytest.mark.parametrize("shows", [[], ["hint"], ["hint", "status"]])
def test_a_0_16_0_hook_moves_the_whole_set_whatever_a_new_mod_lists(v016, tmp_path, shows):
    # So with coaching off, where a new mod lists nothing and shows no hint, neither side shows one: no worse
    # than 0.16.0 alone. With coaching on the new mod shows them and the 0.16.0 hook leaves them to it.
    folder = _config(tmp_path)
    _present(folder, shows=shows, active=0)
    assert _old(v016, folder, "moved") == sorted(THE_WHOLE_SET)


def test_0_16_0_writes_every_coaching_key_a_new_mod_reads_and_enabled_follows_coaching_notes(v016, tmp_path):
    needed = {"enabled", "projects", "exclude_projects", "plan_fresh", "thresholds", "muted", "once"}
    off = _old(v016, _config(tmp_path / "off"), "settings")
    on = _old(v016, _config(tmp_path / "on", '[capture]\ncoaching = ["coaching_notes"]\n'), "settings")
    assert needed <= set(off) and needed <= set(on)
    assert (off["enabled"], on["enabled"]) == (False, True)


# -- an older mod, a 0.16.0 package ------------------------------------------------------------------------------


def test_a_0_16_0_hook_given_an_older_mods_file_moves_the_whole_set_while_it_is_fresh(v016, tmp_path):
    # Unchanged by this build: a file without ``shows`` moves the five hints, and a stale one moves nothing.
    folder = _config(tmp_path)
    _present(folder)
    assert _old(v016, folder, "moved") == sorted(THE_WHOLE_SET)
    _present(folder, seen=NOW.timestamp() - 3600)
    assert _old(v016, folder, "moved") == []

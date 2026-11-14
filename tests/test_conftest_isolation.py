"""The suite never reaches the real ``~/.claude``, even from a test that
undoes its own monkeypatching (conftest's session floor)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from claudeglass import discovery, hook_health


def _in_session_home(path: Path) -> bool:
    return any(part.name.startswith("claudeglass-test-home-") for part in (path, *path.parents))


def test_undo_falls_back_to_the_session_throwaway_home(monkeypatch):
    # undo() also undoes the autouse fixture's per-test home: what is left is
    # the floor conftest set before collection, never the real home.
    monkeypatch.undo()
    assert _in_session_home(Path.home())
    assert _in_session_home(discovery.claude_root())
    assert _in_session_home(hook_health.settings_path())


@pytest.mark.skipif(os.name != "nt", reason="HOMEDRIVE/HOMEPATH are Path.home()'s Windows fallback")
def test_removing_userprofile_still_lands_in_the_session_home(monkeypatch):
    monkeypatch.undo()
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert _in_session_home(Path.home())
    assert _in_session_home(hook_health.settings_path())

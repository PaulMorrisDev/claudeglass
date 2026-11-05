"""The mod's draft checks and /glass tighten's words and diff
(``plugin/hooks/lib/draft.js``, ``plugin/hooks/lib/tighten.js``) behave as
their Node test says.

The logic is plain ES modules so Node can import the same files the mod
loads; this runs ``tests/js/draft.test.mjs`` from pytest, the way
``test_mod_band.py`` does, and holds the stale-context size to the
dashboard's. It skips when Node is missing.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from claudeglass import habits

ROOT = Path(__file__).resolve().parent.parent


def test_node_tests_pass() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node isn't installed")
    done = subprocess.run(
        [node, "--test", str(ROOT / "tests" / "js" / "draft.test.mjs")],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180, cwd=ROOT,
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_stale_context_is_the_dashboards() -> None:
    source = (ROOT / "plugin" / "hooks" / "lib" / "draft.js").read_text(encoding="utf-8")
    match = re.search(r"export var STALE_TOKENS = (\d+);", source)
    assert match is not None and int(match.group(1)) == habits.STALE_TOKENS

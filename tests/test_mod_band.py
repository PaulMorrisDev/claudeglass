"""How the mod's band narrows (``plugin/hooks/lib/band.js``) behaves as its
Node test says.

The logic is a plain ES module so Node can import the same file the mod
loads; this runs ``tests/js/band.test.mjs`` from pytest, the way
``test_mod_keeplist.py`` does. It skips when Node is missing.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_node_tests_pass() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node isn't installed")
    done = subprocess.run(
        [node, "--test", str(ROOT / "tests" / "js" / "band.test.mjs")],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180, cwd=ROOT,
    )
    assert done.returncode == 0, done.stdout + done.stderr

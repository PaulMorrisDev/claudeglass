"""The Instructions page's pure logic (``plugin/hooks/lib/instructions.js``)
behaves as its Node tests say.

The mod runs inside Claude Code and keeps its logic in plain ES modules so
Node can import the same files. This runs ``tests/js/instructions.test.mjs``
from pytest, the way ``test_mod_keeplist.py`` does. It skips when Node is
missing. ``test_mod_vectors.py`` already holds every lib module to the
no-Node, no-clock, no-disk rule, this one included.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "tests" / "js"


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node isn't installed")
    return node


def test_node_tests_pass() -> None:
    done = subprocess.run(
        [_node(), "--test", str(JS / "instructions.test.mjs")],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180, cwd=ROOT,
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_lib_imports_nothing() -> None:
    text = (ROOT / "plugin" / "hooks" / "lib" / "instructions.js").read_text(encoding="utf-8")
    for line in text.splitlines():
        assert not line.startswith("import "), line

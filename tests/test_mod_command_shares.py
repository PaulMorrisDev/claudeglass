"""The mod's readable-command and whole-percent helpers
(``plugin/hooks/lib/command.js`` and ``shares.js``) behave as their Node tests say.

The mod runs inside Claude Code and keeps its logic in plain ES modules so
Node can import the same files. These run the Node tests
(``tests/js/command.test.mjs``, ``tests/js/shares.test.mjs``) from pytest, the
way ``test_mod_keeplist.py`` does, so CI runs them too. They skip when Node is
missing.
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


@pytest.mark.parametrize("name", ["command.test.mjs", "shares.test.mjs"])
def test_node_tests_pass(name: str) -> None:
    done = subprocess.run(
        [_node(), "--test", str(JS / name)],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180, cwd=ROOT,
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_every_node_test_file_has_a_pytest_wrapper() -> None:
    """A ``tests/js/*.test.mjs`` that no pytest file names never runs in CI (the gap this file closes)."""
    wrappers = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "tests").glob("test_*.py"))
    unwrapped = [path.name for path in sorted(JS.glob("*.test.mjs")) if path.name not in wrappers]
    assert unwrapped == []

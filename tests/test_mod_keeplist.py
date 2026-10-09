"""The compaction review's pure logic (``plugin/hooks/lib/keeplist.js`` and
``coverage.js``) behaves as its Node tests say.

The mod runs inside Claude Code and keeps its logic in plain ES modules so
Node can import the same files. These run the Node tests
(``tests/js/keeplist.test.mjs``, ``tests/js/coverage.test.mjs``; the planted
conversation they share is ``tests/js/planted.mjs``) from pytest, the way
``test_mod_vectors.py`` runs the shared vectors. They skip when Node is
missing. ``test_mod_vectors.py`` already holds every lib module to the
no-Node, no-clock, no-disk rule, these two included.
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


@pytest.mark.parametrize("name", ["keeplist.test.mjs", "coverage.test.mjs"])
def test_node_tests_pass(name: str) -> None:
    done = subprocess.run(
        [_node(), "--test", str(JS / name)],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180, cwd=ROOT,
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_libs_import_only_each_other() -> None:
    for name in ("keeplist.js", "coverage.js"):
        text = (ROOT / "plugin" / "hooks" / "lib" / name).read_text(encoding="utf-8")
        for line in text.splitlines():
            # A JSDoc type may name a sibling (`{import("./keeplist.js").KeepItem}`); code may not load anything at run time.
            if not line.lstrip().startswith(("*", "/*", "//")):
                assert "import(" not in line, line
            if line.startswith("import "):
                assert line.rstrip(";").endswith('"./keeplist.js"') or line.rstrip(";").endswith('"./coverage.js"'), line

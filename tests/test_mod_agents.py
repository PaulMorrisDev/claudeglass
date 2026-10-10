"""The Agents feature's pure logic (``plugin/hooks/lib/agents.js``) behaves
as its Node tests say, and its skip list is the catalogue's.

The mod runs inside Claude Code and keeps its logic in plain ES modules so
Node can import the same files. This runs ``tests/js/agents.test.mjs`` under
Node. It skips when Node isn't installed; CI installs it.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from claudeglass import capture_catalogue

ROOT = Path(__file__).resolve().parent.parent
AGENTS_TEST_MJS = ROOT / "tests" / "js" / "agents.test.mjs"
AGENTS_LIB = ROOT / "plugin" / "hooks" / "lib" / "agents.js"
TYPES = ROOT / "plugin" / "types" / "index.d.ts"


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node isn't installed")
    return node


def test_node_tests_pass() -> None:
    done = subprocess.run(
        [_node(), "--test", str(AGENTS_TEST_MJS)],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180, cwd=ROOT,
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_skip_list_is_the_catalogue_s() -> None:
    """lib/agents.js keeps its own copy of the types never coached on, so a
    change to the catalogue's list must change it too."""
    text = AGENTS_LIB.read_text(encoding="utf-8")
    start = text.index("export var SKIP_AGENT_TYPES = ") + len("export var SKIP_AGENT_TYPES = ")
    listed = json.loads(text[start : text.index(";", start)])
    assert listed == list(capture_catalogue.SKIP_AGENT_TYPES)


def test_an_agent_row_never_holds_the_task_prompt() -> None:
    """The snapshot lives in the host's state; the task prompt is kept in the
    module's memory only, so the row's type has no field for it."""
    text = TYPES.read_text(encoding="utf-8")
    start = text.index("export type GlassAgent")
    block = text[start : text.index("\n}", start)]
    fields = re.findall(r"^  (\w+)\??:", block, flags=re.MULTILINE)
    assert "toolUseId" in fields
    assert not [name for name in fields if "prompt" in name.lower() or name in ("brief", "text")]

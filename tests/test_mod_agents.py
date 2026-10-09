"""The Agents feature's pure logic (``plugin/hooks/lib/agents.js``) behaves
as its Node tests say, and its two coaching notes give the answers of the
Python they port.

The mod runs inside Claude Code and keeps its logic in plain ES modules so
Node can import the same files. This builds cases by calling the real
``capture_hook._report_hint`` and ``_split_hint`` (``report_reread`` and
``split_run``) on a spread of contexts, thresholds and counts, writes
``{fn, args, expect}`` for each to a file and runs
``tests/js/agents.test.mjs`` over it under Node, which prints every
mismatch. It skips when Node isn't installed; CI installs it.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from claudeglass import capture_catalogue
from claudeglass.capture_catalogue import COACHING_THRESHOLDS
from claudeglass.hooks import capture_hook

ROOT = Path(__file__).resolve().parent.parent
AGENTS_TEST_MJS = ROOT / "tests" / "js" / "agents.test.mjs"
AGENTS_LIB = ROOT / "plugin" / "hooks" / "lib" / "agents.js"
TYPES = ROOT / "plugin" / "types" / "index.d.ts"

SESSION = "s1"
COACHING = capture_catalogue.export_json()["coaching"]


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node isn't installed")
    return node


def _thresholds(report: float, brief: float) -> dict:
    return {**COACHING_THRESHOLDS, "report_reread_tokens": report, "split_brief_chars": brief}


def _report_cases() -> list[dict]:
    cases = []
    contexts = [0, 1, 99_999, 149_999, 150_000, 150_001, 150_499, 150_500, 151_500, 152_500, 152_501, 158_123, 200_000, 999_500, 1_234_567]
    for report in (150_000.0, 80_000.0, 400_000.0):
        th = _thresholds(report, 6000)
        for ctx in contexts:
            for background in (True, False):
                payload = {"tool_input": {"run_in_background": True} if background else {}}
                state = {"sessions": {SESSION: {"last": {"at": 1.0, "ctx": ctx, "ttl": 300}}}}
                found = capture_hook._report_hint(payload, state, SESSION, COACHING, th)
                expect = None if found is None else {"kind": found[0], "stake": found[1], "fields": found[2]}
                cases.append({"fn": "reportReread", "args": {"background": background, "ctx": ctx, "th": {"report_reread_tokens": report}}, "expect": expect})
    return cases


def _split_cases() -> list[dict]:
    cases = []
    for brief in (6000.0, 12_500.0, 1_234_567.0):
        th = _thresholds(150_000, brief)
        for compaction in (0, 1, 3):
            for count in (0, 1, 2, 7):
                state = {"sessions": {SESSION: {"splits": {"compaction": compaction, "brief": count}}}}
                found = capture_hook._split_hint(state, SESSION, COACHING, th)
                expect = None if found is None else {"kind": found[0], "stake": found[1], "fields": found[2]}
                args = {"compaction": compaction, "brief": count, "th": {"split_brief_chars": brief}, "why": COACHING["split_why"]}
                cases.append({"fn": "splitRun", "args": args, "expect": expect})
    return cases


def test_node_tests_pass_and_the_notes_match_the_python(tmp_path: Path) -> None:
    cases = _report_cases() + _split_cases()
    assert any(case["expect"] is not None for case in cases)
    assert any(case["expect"] is None for case in cases)
    path = tmp_path / "vectors.json"
    path.write_text(json.dumps(cases), encoding="utf-8")
    env = {**__import__("os").environ, "AGENT_VECTORS": str(path)}
    done = subprocess.run(
        [_node(), "--test", str(AGENTS_TEST_MJS)],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180, cwd=ROOT, env=env,
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

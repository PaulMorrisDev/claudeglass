"""The mod's coaching before a message is sent (``plugin/hooks/lib/coach.js``)
makes the decisions of the capture hook it ports.

The capture hook decides ``cold_return``, ``status_poll``, ``drip_feed``,
``big_paste`` and ``plan_fresh`` from a message after
it is sent. When the mod runs, it decides the same things from the draft
and the session so far. This builds cases by calling the real functions
(``_is_status``, ``_is_change_request``, ``_ends_on_question``,
``_changes_file``, ``_drip_count``, ``_paste_hint``, ``_cold_hint``,
``_background_pending`` and ``_fresh_hint``) on a spread
of inputs, writes ``{fn, args, expect}`` for each to a file and runs
``tests/js/coach.test.mjs`` over it under Node, which prints every
mismatch. It skips when Node isn't installed; CI installs it.
"""

from __future__ import annotations

import json
import os
import random
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from claudeglass import capture_catalogue
from claudeglass.capture_catalogue import COACHING_THRESHOLDS
from claudeglass.hooks import capture_hook

ROOT = Path(__file__).resolve().parent.parent
COACH_TEST_MJS = ROOT / "tests" / "js" / "coach.test.mjs"
COACH_LIB = ROOT / "plugin" / "hooks" / "lib" / "coach.js"

COACHING = capture_hook.load_catalogue()["coaching"]
TH = dict(COACHING_THRESHOLDS)
EDIT_TOOLS = COACHING["edit_tools"]

PROMPTS = [
    "how is it going?", "how's it going", "status", "any updates?", "is it done yet", "are you still working?", "what's the status",
    "progress please", "where are we at", "hey, how are things looking now?", "how long is left", "is everything finished?",
    "continue", "go ahead", "yes, go ahead and merge it", "ok proceed", "lgtm, ship it", "implement the plan", "yes",
    "thanks", "great, thank you!", "ok", "perfect", "no", "nice work, thanks",
    "rename foo to bar", "Fix the typo in x.py", "add a --verbose flag", "now remove the old helper", "also update the docs",
    "can you please change the title to Hello", "let's make the button blue", "we should move this into utils",
    "and then delete the unused imports.", "I want you to rewrite the parser", "please add tests for the parser",
    "what does this function do?", "why is the build slow", "which file defines the router", "how do I run the tests",
    "explain the cache logic", "review the diff", "can you look at the failing test", "summarise the changes",
    "show me the config", "check that the migration is safe", "tell me what changed",
    "fix it", "make it faster?", "add a test and then fix the typo", "ok. now rename the module", "so, add a retry",
    "the build is red", "this is broken", "I think the cache is wrong", "", "   ", "x" * 700, "fix " + "word " * 130,
    "Can we rename the variable? Also fix the typo.", "update the changelog\nand bump the version",
]

REPLIES = [
    "All done. Want me to also fix the tests?",
    "All done.",
    "I changed the file.\n\nShould I commit it?",
    "Here is the plan:\n- first step\n- second step\n- Would you like me to start with the second?",
    "Done. Run `foo?` to check.",
    "I looked at https://example.com/what?x=1 and found nothing.",
    "```\nwhat?\n```\nThat is the output.",
    "Which of these do you want, A or B?",
    "I fixed it.\n\n> quoted question?\n\nThe tests pass.",
    "Finished! Anything else?\n\n[cg: task=feature size=s]",
    "Two options:\n1. Keep it\n2. Drop it\nWhich one?",
    "Ok.",
    "",
    "Is that right ? ",
    "Done.\n- Is this OK?\n- And this?",
]

CALLS = [
    ("Edit", {"file_path": "src/a.py"}), ("Write", {"file_path": "README.md"}), ("MultiEdit", {"file_path": "/home/u/.claude/settings.json"}),
    ("NotebookEdit", {"notebook_path": "n.ipynb"}), ("Edit", {}), ("Read", {"file_path": "a.py"}),
    ("Bash", {"command": "echo hi > out.txt"}), ("Bash", {"command": "ls -la"}), ("Bash", {"command": "cat a >> b.log"}),
    ("Bash", {"command": "echo x > ~/.claude/notes.md"}), ("PowerShell", {"command": "Set-Content a.txt 'x'"}),
    ("Bash", {"command": "git commit -m x"}), ("Bash", {}), ("Grep", {"pattern": "x"}), ("Bash", {"command": "sed -i s/a/b/ f.txt"}),
    ("Bash", {"command": "tee out.log < in.log"}), ("Edit", {"file_path": ".claude/x.md"}),
]


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node isn't installed")
    return node


def vec(fn: str, args, expect) -> dict:
    return {"fn": fn, "args": args, "expect": expect}


def _found(found) -> dict | None:
    """A hint as ``(kind, stake, fields)`` to what the JS returns of it, less the fields' words."""
    return None if found is None else {"kind": found[0], "stake": found[1]}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


# -- what a message is ----------------------------------------------------------------


def _shape_vectors() -> list[dict]:
    out = []
    for text in PROMPTS:
        out.append(vec("isStatus", [text], capture_hook._is_status(text, COACHING)))
        out.append(vec("isGo", [text], capture_hook._is_go(text, COACHING)))
        out.append(vec("isQuestion", [text], capture_hook._is_question(text, COACHING)))
        out.append(vec("isChangeRequest", [text], bool(capture_hook._is_change_request(text, COACHING))))
    for text in REPLIES:
        out.append(vec("endsOnQuestion", [text], capture_hook._ends_on_question(text)))
    for name, tool_input in CALLS:
        block = {"type": "tool_use", "name": name, "input": tool_input}
        out.append(vec("changesFile", [name, tool_input], capture_hook._changes_file(block, EDIT_TOOLS)))
    return out


# -- drip_feed -------------------------------------------------------------------------


def _drip_vectors() -> list[dict]:
    rng = random.Random(7)
    changes = [t for t in PROMPTS if t.strip() and capture_hook._is_change_request(t, COACHING) and len(t.strip()) <= TH["drip_chars"]]
    others = [t for t in PROMPTS if t.strip()]
    out = []
    for case in range(300):
        th = dict(TH) if case % 3 else {**TH, "drip_chars": 40, "drip_window_minutes": 5}
        earlier = []
        for _ in range(rng.randint(0, 6)):
            text = rng.choice(changes if rng.random() < 0.75 else others)
            earlier.append({
                "text": text,
                "answer": rng.random() < 0.15,
                "answered": rng.random() < 0.9,
                "edited": rng.random() < 0.85,
                "since": rng.choice([None, 30.0, 200.0, 900.0, 1500.0, 90.0]),
            })
        prompt = rng.choice(changes if rng.random() < 0.8 else others)
        since = rng.choice([None, 10.0, 120.0, 400.0, 1199.0, 1200.0, 1201.0, 5000.0])
        answer = rng.random() < 0.15
        count = capture_hook._drip_count(earlier, prompt, since, answer, COACHING, th)
        out.append(vec("dripCount", [{"earlier": earlier, "prompt": prompt, "since": since, "answer": answer, "th": th}], count))
    assert any(v["expect"] >= 3 for v in out) and any(v["expect"] == 0 for v in out)
    return out


# -- big_paste -------------------------------------------------------------------------


def _paste_vectors() -> list[dict]:
    out = []
    for th in (dict(TH), {**TH, "big_paste_tokens": 500}):
        edge = int(th["big_paste_tokens"] * capture_hook._CHARS_PER_TOKEN)
        for chars in (0, 1, edge // 2, edge - 1, edge, edge + 1, edge * 3, 4_000_000):
            found = capture_hook._paste_hint("x" * chars, th)
            out.append(vec("pasteHint", [{"chars": chars, "th": th}], _found(found)))
    assert any(v["expect"] for v in out) and any(v["expect"] is None for v in out)
    return out


# -- cold_return -----------------------------------------------------------------------


def _cold_vectors() -> list[dict]:
    rng = random.Random(11)
    out = []
    for case in range(260):
        th = dict(TH) if case % 2 else {**TH, "cold_min_tokens": 50_000, "warm_prefix_tokens": 10_000}
        at = 1_700_000_000.0 + rng.randint(0, 1000)
        last = {"at": at, "ctx": rng.choice([0, 49_999, 50_000, 99_999, 100_000, 100_001, 180_000, 950_000]), "ttl": rng.choice([300, 3600])}
        idle = rng.choice([0, 100, 299, 300, 301, 3599, 3600, 3601, 7200, 90_000])
        now = at + idle
        records = []
        compacted = rng.random() < 0.25
        if compacted:
            records.append({"type": "system", "subtype": "compact_boundary", "timestamp": _iso(at + rng.choice([0, 5, 60]) if rng.random() < 0.7 else at - 30)})
        stopped = rng.random() < 0.3
        reset = None
        if stopped:
            reset = at + rng.choice([-60, 10, 200, 600, 4000, 90_000]) if rng.random() < 0.7 else None
            line = {
                "type": "assistant", "isApiErrorMessage": True, "timestamp": _iso(at + 1),
                "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "You've hit your limit"}]},
            }
            if reset is not None:
                line["quotaLimits"] = {"resetsAt": reset}
            records.append(line)
        prefixes = ("You've hit your",)
        found = capture_hook._cold_hint("", records, False, last, True, th, now, prefixes)
        stopped, reset_at = capture_hook._limit_stop(records, last["at"], prefixes)
        out.append(vec("coldHint", [{
            "last": last, "nowS": now, "compactedSince": capture_hook._compacted_since(records, last["at"]),
            "limit": {"stopped": stopped, "reset": reset_at}, "th": th,
        }], _found(found)))
    kinds = {bool(v["expect"]) for v in out}
    assert kinds == {True, False}
    return out


# -- status_poll's background work --------------------------------------------------------

LAUNCHES = [
    "Command running in background with ID: bx1. Output is being written to a file",
    "Async agent launched successfully. agentId: a1",
    "Workflow launched in background. Task ID: w1",
    "Command was moved to the background (ID: bx2)",
    "The command finished with exit code 0",
    "Plain output",
]


def _background_vectors() -> list[dict]:
    rng = random.Random(5)
    prefix = COACHING["task_notification_prefix"]
    out = []
    for _ in range(200):
        steps = []
        records = []
        for index in range(rng.randint(0, 6)):
            if rng.random() < 0.65:
                call = f"t{rng.randint(1, 4)}"
                text = rng.choice(LAUNCHES)
                steps.append({"step": "result", "id": call, "text": text})
                records.append({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": call, "content": text}]}})
            else:
                names = rng.choice([[], ["t1"], ["t2"], ["t1", "t3"], ["t9"]])
                text = prefix + ">" + "".join(f"<tool-use-id>{n}</tool-use-id>" for n in names) + "</task-notification>"
                steps.append({"step": "notify", "text": text})
                records.append({"type": "user", "message": {"role": "user", "content": text}})
        out.append(vec("backgroundSteps", [steps], capture_hook._background_pending(records, COACHING)))
    assert any(v["expect"] for v in out) and any(not v["expect"] for v in out)
    return out


# -- plan_fresh -------------------------------------------------------------------------


def _reply(ctx_parts: tuple[int, int, int], index: int) -> dict:
    input_tokens, cache_read, cache_write = ctx_parts
    return {
        "type": "assistant", "timestamp": _iso(1_700_000_000 + index),
        "message": {"id": f"m{index}", "role": "assistant", "model": "claude-sonnet-4-6", "content": [{"type": "text", "text": "ok"}],
                    "usage": {"input_tokens": input_tokens, "cache_read_input_tokens": cache_read, "cache_creation_input_tokens": cache_write, "output_tokens": 50}},
    }


def _fresh_vectors(tmp_path: Path) -> list[dict]:
    rng = random.Random(3)
    out = []
    for case in range(120):
        th = dict(TH) if case % 2 else {**TH, "plan_fresh_tokens": 10_000}
        prompt_chars = rng.choice([5, 400, 5000, 60_000])
        first = (rng.randint(1, 2000), rng.choice([0, 15_000]), rng.choice([0, 20_000, 30_000]))
        last = (rng.randint(1, 5000), rng.choice([20_000, 60_000, 120_000, 300_000]), rng.choice([0, 4000]))
        plan_chars = rng.choice([0, 800, 12_000, 90_000])
        compacted = rng.random() < 0.2
        records = [{"type": "user", "timestamp": _iso(1_699_999_999), "message": {"role": "user", "content": "p" * prompt_chars}}, _reply(first, 1)]
        if rng.random() < 0.9:
            records.append(_reply((rng.randint(1, 100), 40_000, 1000), 2))
        records.append(_reply(last, 3))
        if compacted:
            records.insert(3, {"type": "system", "subtype": "compact_boundary", "timestamp": _iso(1_700_000_002)})
        path = tmp_path / f"fresh{case}.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
        found = capture_hook._fresh_hint(str(path), plan_chars, th)
        ctx_of = lambda p: p[0] + p[1] + p[2]  # noqa: E731
        out.append(vec("freshHint", [{
            "ctx": ctx_of(last), "firstCtx": ctx_of(first), "promptChars": prompt_chars, "planChars": plan_chars,
            "compacted": compacted, "th": th,
        }], _found(found)))
    assert {bool(v["expect"]) for v in out} == {True, False}
    return out


# -- who coaching is for -----------------------------------------------------------------------


def _project_vectors() -> list[dict]:
    """``capture_hook._project_applies`` for a spread of folders and filters, with the project-naming
    variables unset (the mod learns the folder from the session, and ``CLAUDE_CODE_PROJECT_DIR_NAME``
    from the environment, and passes it on as ``name``)."""
    folders = [
        "C:\\Dev\\claude-token-lens", "C:\\Dev\\RevIXO", "/home/pat/work/api", "/home/pat/scratch/play", "/srv/Private Things/app",
        "D:\\Work\\Client A\\site", "/a/" + "very-long-folder-name/" * 14 + "tail",
    ]
    filters = [
        ([], []), (["claude"], []), (["!RevIXO"], []), (["work", "!api"], []), ([], ["scratch"]), (["Dev"], ["token"]),
        (["PRIVATE"], []), (["(unclosed"], []), ([], ["(unclosed"]), (["!(unclosed", "work"], []), (["^C-Dev-"], ["lens$"]),
        (["very-long"], []), (["^-home"], ["play"]),
    ]
    out = []
    saved = {key: os.environ.pop(key, None) for key in ("CLAUDE_PROJECT_DIR", "CLAUDE_CODE_PROJECT_DIR_NAME")}
    try:
        for folder in folders:
            for projects, excluded in filters:
                config = {"capture": {"projects": projects}, "exclude_projects": excluded}
                out.append(vec("projectApplies", [{"cwd": folder, "projects": projects, "excluded": excluded}], capture_hook._project_applies({"cwd": folder}, config)))
        os.environ["CLAUDE_CODE_PROJECT_DIR_NAME"] = "named-project"
        for projects, excluded in filters:
            config = {"capture": {"projects": projects}, "exclude_projects": excluded}
            args = {"cwd": "/anywhere", "projects": projects, "excluded": excluded, "name": "named-project"}
            out.append(vec("projectApplies", [args], capture_hook._project_applies({"cwd": "/anywhere"}, config)))
    finally:
        os.environ.pop("CLAUDE_CODE_PROJECT_DIR_NAME", None)
        for key, value in saved.items():
            if value is not None:
                os.environ[key] = value
    assert {v["expect"] for v in out} == {True, False}
    return out


# -- the run --------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def vectors(tmp_path_factory) -> dict[str, list[dict]]:
    tmp = tmp_path_factory.mktemp("coach")
    (tmp / "cfg").mkdir()
    return {
        "shape": _shape_vectors(),
        "drip": _drip_vectors(),
        "paste": _paste_vectors(),
        "cold": _cold_vectors(),
        "background": _background_vectors(),
        "fresh": _fresh_vectors(tmp),
        "project": _project_vectors(),
    }


def test_the_corpus_has_every_kind_of_message(vectors) -> None:
    shape = {(v["fn"], str(v["expect"])) for v in vectors["shape"]}
    for fn in ("isStatus", "isGo", "isQuestion", "isChangeRequest", "endsOnQuestion", "changesFile"):
        assert (fn, "True") in shape and (fn, "False") in shape, fn


def test_node_tests_pass_and_the_decisions_match_the_python(vectors, tmp_path: Path) -> None:
    flat = [v for group in vectors.values() for v in group]
    path = tmp_path / "vectors.json"
    path.write_text(json.dumps(flat), encoding="utf-8")
    done = subprocess.run(
        [_node(), "--test", str(COACH_TEST_MJS)],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=240, cwd=ROOT, env={**os.environ, "COACH_VECTORS": str(path)},
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_hints_the_mod_decides_are_the_ones_the_hook_leaves_to_it() -> None:
    assert {"cold_return", "status_poll", "drip_feed", "big_paste", "plan_fresh"} <= set(capture_catalogue.MOVED_TO_MOD)
    text = COACH_LIB.read_text(encoding="utf-8")
    match = re.search(r'HINT_ORDER = (\[[^\]]*\])', text)
    assert match
    assert json.loads(match.group(1)) == ["drip_feed", "big_paste", "status_poll", "cold_return"]


# -- the hook and the mod share what each knows ---------------------------------------------------


def _mod_folder(config_dir: Path) -> Path:
    folder = config_dir / capture_catalogue.MOD_DIR
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def test_a_heartbeat_rewritten_with_seen_zero_at_the_end_of_a_session_reads_as_absent(tmp_path: Path) -> None:
    catalogue = capture_hook.load_catalogue()
    present = _mod_folder(tmp_path) / capture_catalogue.MOD_PRESENT_DIR
    present.mkdir()
    now = 1_700_000_000.0
    (present / "s1.json").write_text(json.dumps({"schema": 1, "session_id": "s1", "seen": now}), encoding="utf-8")
    assert capture_hook._mod_present(tmp_path, catalogue, "s1", now) is True
    # The engine cannot delete the file: at the end the mod writes it again with `seen` at 0.
    (present / "s1.json").write_text(json.dumps({"schema": 1, "session_id": "s1", "seen": 0}), encoding="utf-8")
    os.utime(present / "s1.json", (now + 5, now + 5))
    assert capture_hook._mod_present(tmp_path, catalogue, "s1", now) is False
    assert capture_hook._moved(tmp_path, catalogue, "s1", now) == frozenset()


def test_the_mods_reminder_rests_the_hooks_and_the_later_of_the_two_counts(tmp_path: Path) -> None:
    catalogue = capture_hook.load_catalogue()
    now = 1_700_000_000.0
    scan = {"replies": {"m": {"i": 4, "tokens": 3_000_000, "admit": False}}, "piece": 0, "piece_key": "k1", "piece_at": None}

    def due(state: dict) -> str:
        return capture_hook._reminder_due(scan, {}, dict(TH), COACHING, state, "s1", now, tmp_path, "s1", catalogue)

    assert capture_hook._mod_reminded_at(tmp_path, catalogue) is None
    assert due({}) != ""
    coach_file = _mod_folder(tmp_path) / "coach.json"

    def write(text: str) -> None:
        # Two rewrites of one size inside the clock's tick look unchanged to
        # the hook's cache (time and size), so start each read afresh.
        coach_file.write_text(text, encoding="utf-8")
        capture_hook._MOD_COACH.clear()

    write(json.dumps({"schema": 1, "reminded_at": now - 3600}))
    assert capture_hook._mod_reminded_at(tmp_path, catalogue) == now - 3600
    assert due({}) == ""
    # An old reminder of the mod's, and a newer one of the hook's: the hook's counts.
    write(json.dumps({"schema": 1, "reminded_at": now - 30 * 86400}))
    assert due({}) != ""
    assert due({"feedback": {"reminded_at": now - 3600}}) == ""
    # A file that says nothing usable changes nothing.
    for text in ("not json", json.dumps({"schema": 2, "reminded_at": now}), json.dumps({"schema": 1, "reminded_at": "x"})):
        write(text)
        assert capture_hook._mod_reminded_at(tmp_path, catalogue) is None
    coach_file.unlink()
    coach_file.mkdir()
    assert capture_hook._mod_reminded_at(tmp_path, catalogue) is None

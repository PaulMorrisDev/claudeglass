#!/usr/bin/env python3
"""How well Claude Haiku judges a finished agent run: known-answer runs,
each judged several times, scored key by key.

The capture hook's ``SubagentStop`` entry hands Haiku an excerpt of each
finished agent run (``agent_excerpt`` in ``hooks/capture-hook.py``) and
keeps the words it answers with: ``result``, ``retry``, ``fit``,
``brief`` and ``missing``. This builds each case's excerpt with the hook's
own ``agent_excerpt``, so the text Haiku reads is the text it reads in a
session, asks ``claude -p --model haiku`` exactly as the hook does, and
prints, for each case and key, what Haiku said against the right answer.

Every call costs about $0.0015 on your own Claude Code login:

    python scripts/eval-agent-judge.py              # every case, 3 runs each
    python scripts/eval-agent-judge.py --repeats 1

``docs/tagger-eval.md`` has the results this was run for.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from claudeglass import capture_catalogue as cat  # noqa: E402

_SPEC = importlib.util.spec_from_file_location("_capture_hook", ROOT / "src" / "claudeglass" / "hooks" / cat.HOOK_SCRIPT)
HOOK = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(HOOK)
CATALOGUE = HOOK.load_catalogue()


def _run(brief: str, tools: list[tuple[str, dict]], report: str, *, errors: int = 0, model: str = "claude-sonnet-5"):
    """An agent's transcript: its brief, one reply calling ``tools``, a
    tool error for each of ``errors``, and its report."""
    uses = [{"type": "tool_use", "id": f"t{n}", "name": name, "input": given} for n, (name, given) in enumerate(tools)]
    records = [
        {"type": "user", "message": {"role": "user", "content": brief}},
        {"type": "assistant", "message": {"id": "msg_1", "model": model, "usage": {"output_tokens": 400}, "content": uses}},
    ]
    for n in range(errors):
        records.append({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"t{n}", "content": "error", "is_error": True}]}})
    records.append({"type": "assistant", "message": {"id": "msg_2", "model": model, "usage": {"output_tokens": 120},
                                                     "content": [{"type": "text", "text": report}]}})
    return records


#: Each case: (agent type, the run, what the session said as it started
#: it, the earlier runs, the right words). A key left out of the right
#: words isn't scored; ``retry: None`` means no retry.
CASES = {
    "done": ("general-purpose", _run(
        "Add a one-line docstring to every function in mod1.py and mod2.py.",
        [("Read", {"file_path": "/w/mod1.py"}), ("Edit", {"file_path": "/w/mod1.py"}),
         ("Read", {"file_path": "/w/mod2.py"}), ("Edit", {"file_path": "/w/mod2.py"})],
        "Added docstrings to all four functions in mod1.py and mod2.py."),
        "", [], {"result": "done", "retry": None, "brief": "clear"}),
    "partial": ("general-purpose", _run(
        "Add input validation to every handler in api/handlers.py and a test for each.",
        [("Read", {"file_path": "/w/api/handlers.py"}), ("Edit", {"file_path": "/w/api/handlers.py"})],
        "Added validation to create_user and update_user. delete_user and list_users still need it, and no tests "
        "were written yet."),
        "", [], {"result": "partial", "retry": None}),
    "blocked": ("general-purpose", _run(
        "Read config/settings.yaml and list every key it defines, with its value.",
        [("Read", {"file_path": "/w/config/settings.yaml"}), ("Glob", {"pattern": "**/*.yaml"})],
        "config/settings.yaml doesn't exist, and there are no YAML files in the project, so there were no keys to "
        "list.", errors=1),
        "", [], {"result": "blocked", "retry": None}),
    "vague brief": ("Explore", _run(
        "Look at the code.",
        [("Glob", {"pattern": "**/*"}), ("Read", {"file_path": "/w/app.py"})],
        "The project has one module, app.py, with two functions. Tell me what you're looking for and I can dig "
        "further."),
        "", [], {"brief": "vague", "retry": None}),
    "retry: model": ("general-purpose", _run(
        "Fix the failing test test_parse_dates in tests/test_dates.py without changing the test.",
        [("Read", {"file_path": "/w/src/dates.py"}), ("Edit", {"file_path": "/w/src/dates.py"}),
         ("Bash", {"command": "pytest -q tests/test_dates.py"})],
        "Fixed: parse_date dropped the UTC offset. tests/test_dates.py passes now.", model="claude-opus-5-5"),
        "The Haiku agent couldn't find the cause. Running it again on Opus, which should handle the timezone logic.",
        [{"type": "general-purpose", "brief": "Fix the failing test test_parse_dates in tests/test_dates.py without "
          "changing the test.", "report": "I could not find why the test fails; the dates look right to me."}],
        {"result": "done", "retry": "model"}),
    "retry: tools": ("general-purpose", _run(
        "Run the database migrations with `make migrate` and report any errors.",
        [("Bash", {"command": "make migrate"})], "Migrations ran: 3 applied, no errors."),
        "The Explore agent can't run commands, so starting a general-purpose agent to run the migrations.",
        [{"type": "Explore", "brief": "Run the database migrations with `make migrate` and report any errors.",
          "report": "I can't run shell commands with my tools, so I couldn't run the migrations."}],
        {"result": "done", "retry": "tools"}),
    "next task, not a retry": ("general-purpose", _run(
        "Add a timeout option to load() in config.py, defaulting to 30 seconds, with a test.",
        [("Edit", {"file_path": "/w/config.py"}), ("Edit", {"file_path": "/w/tests/test_config.py"}),
         ("Bash", {"command": "pytest -q tests/test_config.py"})],
        "Added timeout=30 to load(); tests pass."),
        "Found where the config is loaded. Now implementing the timeout.",
        [{"type": "Explore", "brief": "Find where the config file is loaded.",
          "report": "It is loaded in config.py by load(), called from main.py."}],
        {"result": "done", "retry": None}),
    "second task, not a retry": ("general-purpose", _run(
        "Write the release notes for 0.11.0 from CHANGELOG.md into docs/release.md.",
        [("Read", {"file_path": "/w/CHANGELOG.md"}), ("Write", {"file_path": "/w/docs/release.md"})],
        "Wrote docs/release.md."),
        "Tests are done. Next, the release notes.",
        [{"type": "general-purpose", "brief": "Add tests for the parser's new flags.", "report": "Added 6 tests; all pass."}],
        {"result": "done", "retry": None}),
}


def _ask(name: str) -> dict:
    agent_type, records, said, earlier, _right = CASES[name]
    payload = {"agent_type": agent_type, "cwd": "/w"}
    excerpt, _reply = HOOK.agent_excerpt(records, payload, CATALOGUE, earlier, said)
    job = {"system": cat.agent_judge_text(cat.level_includes("standard")), "excerpt": excerpt}
    answer = HOOK.ask_haiku(job, CATALOGUE["judge"], Path.cwd())
    agent = CATALOGUE["judge"]["agent"]
    keys = [key for metric_keys in agent["keys"].values() for key in metric_keys]
    words = HOOK.judge_tag(answer.get("result"), keys, CATALOGUE["judge"], agent["vocab"])
    got = dict(word.split("=", 1) for word in words.split())
    if got.get("retry") == "none":
        del got["retry"]
    return got


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args(argv)
    runs = [(name, n) for name in CASES for n in range(args.repeats)]
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        answers = dict(zip(runs, pool.map(lambda run: _ask(run[0]), runs)))
    right = total = 0
    for name, (*_, want) in CASES.items():
        got = [answers[(name, n)] for n in range(args.repeats)]
        cells = []
        for key, value in want.items():
            said = [g.get(key) for g in got]
            ok = sum(s == value for s in said)
            right += ok
            total += len(said)
            cells.append(f"{key}={value or '(none)'}: {ok}/{len(said)}" + ("" if ok == len(said) else f" {said}"))
        print(f"{name:26} " + "; ".join(cells))
    print(f"\n{right} of {total} right ({100 * right / total:.0f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

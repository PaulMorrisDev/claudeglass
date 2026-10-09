#!/usr/bin/env python3
"""How well Claude Haiku judges a finished agent run: known-answer runs,
each judged several times, scored key by key and answer shape by answer
shape.

The capture hook's ``SubagentStop`` entry hands Haiku an excerpt of each
finished agent run (``agent_excerpt`` in ``hooks/capture_hook.py``) and
keeps the words it answers with: ``brief``, ``missing``, ``result`` and
``retry``, asked in that order (it was also asked for ``fit``, the model
fit, until that verdict said "right" every time). This builds each case's
excerpt with the hook's own ``agent_excerpt``, so the text Haiku reads is
the text it reads in a session, asks ``claude -p --model haiku`` exactly
as the hook does, and prints, for each case and key, what Haiku said
against the right answer. What the hook puts right itself, a
``retry=model`` for the same brief rerun on a higher model tier and the
``done`` it takes out of ``missing`` for an agent that handed back its
answer, is not scored here: only Haiku's own words are.

Each case also has an answer shape, one word of ``SHAPES``: how the run
handed its work back (a written report, findings, an empty list, verdicts
on claims, an answer that lists concerns or risks, a handback, or a run
that could not do its work, which outranks the others). The scores print
per key and per shape, because a miss tends to follow the shape of the
answer, not the key: a list of concerns read as unfinished work, or a
refuted claim read as a blocked run.

Every call costs about $0.0015 on your own Claude Code login:

    python scripts/eval-agent-judge.py              # every case, 3 runs each
    python scripts/eval-agent-judge.py --repeats 1

A replay set judges real runs instead. ``--replay FILE`` reads a labels
file you made by hand: a JSON list of ``{"transcript": <path of an agent's
transcript>, "agent_reply": <the id of its last reply>, "shape": <a word
of SHAPES>, "right": {"result": ..., "brief": ...}}``. It builds each
excerpt from that transcript the way the hook's worker does
(``prepare_agent_job``, which uses ``agent_excerpt``) and asks Haiku as the
cases do. ``--stored CONFIG_DIR`` with ``--replay`` spends nothing: it
scores the verdicts the hook already wrote to ``CONFIG_DIR/tags/*.jsonl``
(a line's ``agent`` words, the newest line for a reply winning; a run's
verdict is the one on its newest reply that has one, as the dashboard
reads it) against the labels, and reports how often the stored
verdict says ``result=done`` and how many labelled runs have none:

    python scripts/eval-agent-judge.py --replay ~/agent-judge-labels.json
    python scripts/eval-agent-judge.py --replay ~/agent-judge-labels.json --stored ~/.claude/claudeglass

The labels file never goes in the repository. It holds paths, reply ids
and closed words only, no text of any run, and the loader refuses a file
that holds anything else (``load_labels``). Keep it outside the checkout:
the loader refuses one inside it.

``docs/tagger-eval.md`` has the results this was run for.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import importlib.util
import itertools
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from claudeglass import capture_catalogue as cat  # noqa: E402
from claudeglass import haiku_tags  # noqa: E402

_SPEC = importlib.util.spec_from_file_location("_capture_hook", ROOT / "src" / "claudeglass" / "hooks" / cat.HOOK_MODULE)
HOOK = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(HOOK)
CATALOGUE = HOOK.load_catalogue()

#: How a run handed its work back, one word per case and per labelled
#: run. ``blocked`` is a run that says it could not do its own work, by
#: whatever means it says so (a handback that says it too).
SHAPES = ("report", "findings", "empty", "verdicts", "concerns", "risks", "handback", "blocked")

#: The keys a labels file gives right words for.
LABEL_KEYS = ("result", "brief")


def _run(
    brief: str, tools: list[tuple[str, dict]], report: str, *, errors: int = 0, model: str = "claude-sonnet-5",
    relay: str = "", answer: tuple[str, dict] | None = None, after: str = "",
    rejected: tuple[str, str] | None = None, then: list[tuple[str, dict]] = (),
):
    """An agent's transcript: its brief, one reply calling ``tools``, a
    tool error for each of ``errors``, and its report. A workflow agent
    (``relay``: the line the workflow was started with) is handed that line
    and then its computed task by the harness, and hands its ``answer``
    back through an answer tool, as ``(tool name, input)``, in place of a
    report; ``after`` is a short remark it makes once that tool has
    answered. A run a stop hook sent back (``rejected``: its first report
    and what the hook said) carries on with the ``then`` tools before its
    final report or answer."""
    prompts = [brief]
    if relay:
        prompts = [
            f"[Workflow harness — user request] The user typed this to start the workflow:\n  {relay}",
            f"[Workflow harness — computed task] The task text below was computed at runtime by a workflow "
            f"script. It was not typed by this session's user. The computed task text follows:\n  {brief}",
        ]
    records = [{"type": "user", "message": {"role": "user", "content": prompt}} for prompt in prompts]
    replies = 0

    def reply(content: list, output: int) -> None:
        nonlocal replies
        replies += 1
        records.append({"type": "assistant", "message": {
            "id": f"msg_{replies}", "model": model, "usage": {"output_tokens": output}, "content": content}})

    def calls(given: list[tuple[str, dict]], first: int) -> list[dict]:
        return [{"type": "tool_use", "id": f"t{n}", "name": name, "input": args}
                for n, (name, args) in enumerate(given, first)]

    reply(calls(tools, 0), 400)
    for n in range(errors):
        records.append({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"t{n}", "content": "error", "is_error": True}]}})
    if rejected:
        reply([{"type": "text", "text": rejected[0]}], 60)
        records.append({"type": "user", "isMeta": True,
                        "message": {"role": "user", "content": f"Stop hook feedback:\n{rejected[1]}"}})
        if then:
            reply(calls(list(then), len(tools)), 150)
    reply(
        [{"type": "tool_use", "id": "t_answer", "name": answer[0], "input": answer[1]}]
        if answer else [{"type": "text", "text": report}],
        120,
    )
    if answer and after:
        records.append({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t_answer", "content": "ok"}]}})
        reply([{"type": "text", "text": after}], 10)
    return records


class Case(NamedTuple):
    """A known-answer run: the agent's type, its transcript, what the
    session said as it started it, the earlier runs, the right words (a key
    left out isn't scored; ``retry: None`` means no retry) and its answer
    shape (one word of :data:`SHAPES`). ``late`` is how many of the
    transcript's last records are written only after the hook fired, so
    that the worker has to wait for them; ``restop`` marks a stop that
    follows a stop hook's request to carry on."""

    agent_type: str
    records: list
    said: str
    earlier: list
    right: dict
    shape: str
    late: int = 0
    restop: bool = False


#: One brief, given to two agents, one of which could do the work and one
#: of which could not: the brief reads the same either way.
_SAME_BRIEF = (
    "Update the pinned version of requests in requirements.txt to the latest 2.x release, then run pip check "
    "and report whether it passes."
)

CASES = {
    "done": Case("general-purpose", _run(
        "Add a one-line docstring to every function in mod1.py and mod2.py.",
        [("Read", {"file_path": "/w/mod1.py"}), ("Edit", {"file_path": "/w/mod1.py"}),
         ("Read", {"file_path": "/w/mod2.py"}), ("Edit", {"file_path": "/w/mod2.py"})],
        "Added docstrings to all four functions in mod1.py and mod2.py."),
        "", [], {"result": "done", "retry": None, "brief": "clear"}, "report"),
    "partial": Case("general-purpose", _run(
        "Add input validation to every handler in api/handlers.py and a test for each.",
        [("Read", {"file_path": "/w/api/handlers.py"}), ("Edit", {"file_path": "/w/api/handlers.py"})],
        "Added validation to create_user and update_user. delete_user and list_users still need it, and no tests "
        "were written yet."),
        "", [], {"result": "partial", "retry": None}, "report"),
    "blocked": Case("general-purpose", _run(
        "Read config/settings.yaml and list every key it defines, with its value.",
        [("Read", {"file_path": "/w/config/settings.yaml"}), ("Glob", {"pattern": "**/*.yaml"})],
        "config/settings.yaml doesn't exist, and there are no YAML files in the project, so there were no keys to "
        "list.", errors=1),
        "", [], {"result": "blocked", "retry": None}, "blocked"),
    "vague brief": Case("Explore", _run(
        "Look at the code.",
        [("Glob", {"pattern": "**/*"}), ("Read", {"file_path": "/w/app.py"})],
        "The project has one module, app.py, with two functions. Tell me what you're looking for and I can dig "
        "further."),
        "", [], {"brief": "vague", "retry": None}, "report"),
    "retry: model": Case("general-purpose", _run(
        "Fix the failing test test_parse_dates in tests/test_dates.py without changing the test.",
        [("Read", {"file_path": "/w/src/dates.py"}), ("Edit", {"file_path": "/w/src/dates.py"}),
         ("Bash", {"command": "pytest -q tests/test_dates.py"})],
        "Fixed: parse_date dropped the UTC offset. tests/test_dates.py passes now.", model="claude-opus-5-5"),
        "The Haiku agent couldn't find the cause. Running it again on Opus, which should handle the timezone logic.",
        [{"type": "general-purpose", "brief": "Fix the failing test test_parse_dates in tests/test_dates.py without "
          "changing the test.", "model": "claude-haiku-4-5",
          "report": "I could not find why the test fails; the dates look right to me."}],
        {"result": "done", "retry": "model"}, "report"),
    "retry: tools": Case("general-purpose", _run(
        "Run the database migrations with `make migrate` and report any errors.",
        [("Bash", {"command": "make migrate"})], "Migrations ran: 3 applied, no errors."),
        "The Explore agent can't run commands, so starting a general-purpose agent to run the migrations.",
        [{"type": "Explore", "brief": "Run the database migrations with `make migrate` and report any errors.",
          "model": "claude-haiku-4-5",
          "report": "I can't run shell commands with my tools, so I couldn't run the migrations."}],
        {"result": "done", "retry": "tools"}, "report"),
    "next task, not a retry": Case("general-purpose", _run(
        "Add a timeout option to load() in config.py, defaulting to 30 seconds, with a test.",
        [("Edit", {"file_path": "/w/config.py"}), ("Edit", {"file_path": "/w/tests/test_config.py"}),
         ("Bash", {"command": "pytest -q tests/test_config.py"})],
        "Added timeout=30 to load(); tests pass."),
        "Found where the config is loaded. Now implementing the timeout.",
        [{"type": "Explore", "brief": "Find where the config file is loaded.",
          "report": "It is loaded in config.py by load(), called from main.py."}],
        {"result": "done", "retry": None}, "report"),
    "second task, not a retry": Case("general-purpose", _run(
        "Write the release notes for 0.11.0 from CHANGELOG.md into docs/release.md.",
        [("Read", {"file_path": "/w/CHANGELOG.md"}), ("Write", {"file_path": "/w/docs/release.md"})],
        "Wrote docs/release.md."),
        "Tests are done. Next, the release notes.",
        [{"type": "general-purpose", "brief": "Add tests for the parser's new flags.", "report": "Added 6 tests; all pass."}],
        {"result": "done", "retry": None}, "report"),
    # A refuted claim and an empty list are findings: the agent did its own work.
    "answer: refuted claim": Case("workflow-subagent", _run(
        "Check whether the claim that refunds skip the audit log in payments.py is true. Return a verdict and "
        "your evidence.",
        [("Read", {"file_path": "/w/payments.py"}), ("Grep", {"pattern": "audit_log"})], "",
        relay="Run the review workflow on the payments module.",
        answer=("StructuredOutput", {"verdict": "refuted", "evidence": ["refund() writes the audit log on line 41"]})),
        "", [], {"result": "done", "brief": "clear"}, "verdicts"),
    "answer: empty list": Case("workflow-subagent", _run(
        "List every caller of legacy_pay() in the repo. Return the list of call sites.",
        [("Grep", {"pattern": r"legacy_pay\("})], "",
        relay="Find what still uses the old payment call.",
        answer=("StructuredOutput", {"call_sites": []})),
        "", [], {"result": "done"}, "empty"),
    "handback: could not work": Case("general-purpose", _run(
        "Run the integration tests in tests/integration and report the failures.",
        [("Bash", {"command": "pytest tests/integration"})], "", errors=1,
        answer=("SubagentHandback", {"message": "I could not run them: pytest is not installed and I have no "
                                               "permission to install it, so there are no results."})),
        "", [], {"result": "blocked"}, "blocked"),
    # A workflow started with "Continue" says nothing of the task: the computed task is the brief.
    "two-frame: Continue relay": Case("workflow-subagent", _run(
        "Add a retry limit of 3 to fetch_page() in crawler.py and a test that checks the limit. Return the files "
        "you changed and the test result.",
        [("Read", {"file_path": "/w/crawler.py"}), ("Edit", {"file_path": "/w/crawler.py"}),
         ("Edit", {"file_path": "/w/tests/test_crawler.py"}),
         ("Bash", {"command": "pytest -q tests/test_crawler.py"})], "",
        relay="Continue",
        answer=("StructuredOutput", {"files_changed": ["crawler.py", "tests/test_crawler.py"],
                                     "test_result": "tests/test_crawler.py: 7 passed"})),
        "", [], {"result": "done", "brief": "clear"}, "report"),
    "answer: three findings": Case("workflow-subagent", _run(
        "List every place in the repo that opens a file without a with block. Return each as the file, the line "
        "and the call.",
        [("Grep", {"pattern": r"= open\("}), ("Read", {"file_path": "/w/export.py"}),
         ("Read", {"file_path": "/w/load.py"})], "",
        relay="Tidy up the file handling.",
        answer=("StructuredOutput", {"findings": [
            {"file": "export.py", "line": 18, "call": "out = open(path, 'w')"},
            {"file": "load.py", "line": 7, "call": "src = open(path)"},
            {"file": "load.py", "line": 52, "call": "log = open(log_path, 'a')"}]})),
        "", [], {"result": "done"}, "findings"),
    "answer: confirmed and refuted": Case("workflow-subagent", _run(
        "Check each claim against the code and return a verdict, confirmed or refuted, with evidence for each. "
        "Claim 1: sessions expire after 30 minutes. Claim 2: expired sessions are deleted from the database.",
        [("Read", {"file_path": "/w/sessions.py"}), ("Grep", {"pattern": "expire|delete"})], "",
        relay="Verify the session claims in the design doc.",
        answer=("StructuredOutput", {"claims": [
            {"claim": "Sessions expire after 30 minutes", "verdict": "confirmed",
             "evidence": "SESSION_TTL = 30 * 60 in sessions.py line 9"},
            {"claim": "Expired sessions are deleted from the database", "verdict": "refuted",
             "evidence": "expire() only sets active = False; nothing calls DELETE"}]})),
        "", [], {"result": "done"}, "verdicts"),
    # An implementer that did the work and lists what worries it has still finished.
    "answer: done, with concerns": Case("workflow-subagent", _run(
        "Replace the dict in cache.py with an LRU cache of 500 entries and keep the tests passing. Return a "
        "summary, the files you changed and any concerns.",
        [("Read", {"file_path": "/w/cache.py"}), ("Edit", {"file_path": "/w/cache.py"}),
         ("Edit", {"file_path": "/w/tests/test_cache.py"}), ("Bash", {"command": "pytest -q tests/test_cache.py"})], "",
        relay="Make the session cache bounded.",
        answer=("StructuredOutput", {
            "summary": "Replaced the session dict with an LRU cache of 500 entries; the 12 cache tests pass.",
            "files_changed": ["cache.py", "tests/test_cache.py"],
            "concerns": ["Entries are now evicted, so a caller that expects a key to stay will miss it.",
                         "The size of 500 is a guess; nothing in the repo says how many sessions are live.",
                         "invalidate_all() still walks the whole cache and was not changed."]})),
        "", [], {"result": "done"}, "concerns"),
    "answer: research with risks": Case("workflow-subagent", _run(
        "Find out whether the upload parser can switch from the csv module to pandas. Return the answer and the "
        "risks.",
        [("Read", {"file_path": "/w/uploads.py"}), ("Grep", {"pattern": "import csv"}),
         ("Read", {"file_path": "/w/tests/data/sample1.csv"})], "",
        relay="Look into replacing csv with pandas.",
        answer=("StructuredOutput", {
            "answer": "Yes. pandas.read_csv reads every sample file the current parser accepts, and the change "
                      "is about 20 lines in uploads.py.",
            "risks": ["pandas adds about 60 MB to the install.",
                      "read_csv guesses column types, so an id such as 007 becomes 7 unless dtype=str is set.",
                      "Quoted line breaks parse differently from csv in 2 of the 14 sample files."]})),
        "", [], {"result": "done"}, "risks"),
    # The handback is the answer; the short remark after it doesn't make it a report again.
    "handback, then Report delivered.": Case("general-purpose", _run(
        "Add a timeout argument to the get() helper in http_client.py and report what you changed.",
        [("Read", {"file_path": "/w/http_client.py"}), ("Edit", {"file_path": "/w/http_client.py"}),
         ("Bash", {"command": "pytest -q tests/test_http_client.py"})], "",
        answer=("SubagentHandback", {"message": "Added timeout=10 to get() in http_client.py and passed it to the "
                                               "request. tests/test_http_client.py passes (9 tests)."}),
        after="Report delivered."),
        "", [], {"result": "done"}, "handback"),
    # A stop hook's guard sent the first report back: the newest verdict is the run's.
    "re-stop after a guard rejection": Case("general-purpose", _run(
        "Fix the off-by-one in paginate() in utils/paging.py and run tests/test_paging.py.",
        [("Read", {"file_path": "/w/utils/paging.py"}), ("Edit", {"file_path": "/w/utils/paging.py"})],
        "Fixed the off-by-one in paginate(): the last page started one item late. tests/test_paging.py passes "
        "(11 tests).",
        rejected=("Changed paginate() so the last page starts at the right item. I have not run the tests yet.",
                  "- [guard]: run the tests before you stop."),
        then=[("Bash", {"command": "pytest -q tests/test_paging.py"})]),
        "", [], {"result": "done", "brief": "clear"}, "report", restop=True),
    # The hook fires before the agent's last lines are written: the worker waits for the answer.
    "answer written after the hook fired": Case("workflow-subagent", _run(
        "Count the public functions in api/handlers.py that have no docstring and name them. Return the count "
        "and the names.",
        [("Read", {"file_path": "/w/api/handlers.py"}), ("Grep", {"pattern": "def [a-z]"})], "",
        relay="Check the api docs.",
        answer=("StructuredOutput", {"count": 2, "names": ["delete_user", "list_users"]})),
        "", [], {"result": "done"}, "findings", late=1),
    "same brief: done run": Case("general-purpose", _run(
        _SAME_BRIEF,
        [("Read", {"file_path": "/w/requirements.txt"}), ("Edit", {"file_path": "/w/requirements.txt"}),
         ("Bash", {"command": "pip check"})],
        "requirements.txt now pins requests==2.32.3, and pip check reports no broken requirements."),
        "", [], {"result": "done", "brief": "clear"}, "report"),
    "same brief: blocked run": Case("general-purpose", _run(
        _SAME_BRIEF,
        [("Edit", {"file_path": "/w/requirements.txt"}), ("Bash", {"command": "pip check"}),
         ("Read", {"file_path": "/w/requirements.txt"})],
        "I could not do this: the edit to requirements.txt was refused for lack of write permission, and pip check "
        "would not run. Nothing was changed.", errors=2),
        "", [], {"result": "blocked", "brief": "clear"}, "blocked"),
}


# -- building the excerpt Haiku reads -------------------------------------------

def _worker_excerpt(
    payload: dict, path: Path, transcript_path: str = "", written_later: list[dict] | None = None
) -> tuple[str, str]:
    """The excerpt the hook's worker builds for the agent transcript at
    ``path`` (``prepare_agent_job``), as ``(excerpt, reply id)``; ``("", "")``
    when there is nothing to judge. The worker's wait runs on a clock that
    needs no real seconds. With ``written_later``, those records are appended
    to the file the first time the worker waits, as an agent's last lines are
    written after its ``SubagentStop`` hook fires."""
    ticks = itertools.count()
    pending = list(written_later or [])

    def sleep(_seconds: float) -> None:
        if pending:
            with open(path, "a", encoding="utf-8", newline="\n") as handle:
                for record in pending:
                    handle.write(json.dumps(record) + "\n")
            pending.clear()

    job = {
        "ts": "2026-01-01T00:00:00Z", "kind": "agent", "reply": "", "keys": [], "system": "",
        "payload": {**payload, "transcript_path": transcript_path, "agent_transcript_path": str(path),
                    "last_assistant_message": ""},
    }
    prepared = HOOK.prepare_agent_job(job, CATALOGUE, sleep, lambda: float(next(ticks)))
    return (prepared["excerpt"], prepared["reply"]) if prepared else ("", "")


def case_excerpt(case: Case) -> tuple[str, str]:
    """What Haiku reads about ``case``, and its last reply's id: the hook's
    own ``agent_excerpt`` of the transcript, or, for a run whose last lines
    arrive after the hook fired (``late``), what the worker reads once it has
    waited for them. Such a run is a stand-alone one: nothing was said as it
    started and no run came before it."""
    payload = {"agent_type": case.agent_type, "cwd": "/w"}
    if case.restop:
        payload["stop_hook_active"] = True
    if not case.late:
        return HOOK.agent_excerpt(case.records, payload, CATALOGUE, case.earlier, case.said)
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "agent-a1.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in case.records[:-case.late]), encoding="utf-8")
        return _worker_excerpt({**payload, "agent_id": "a1"}, path, written_later=case.records[-case.late:])


def _session_file(path: Path) -> str:
    """The main transcript of the session that ran the agent whose transcript
    is ``path`` (``<session>/subagents/agent-X.jsonl``, or a level deeper for
    a workflow agent); ``""`` when it isn't there."""
    for parent in path.parents:
        if parent.name == "subagents":
            session = parent.parent.with_suffix(".jsonl")
            return str(session) if session.is_file() else ""
    return ""


def _first_cwd(path: Path) -> str:
    """The working folder the agent's transcript names (``""`` without)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in itertools.islice(handle, 20):
                try:
                    cwd = json.loads(line).get("cwd")
                except (ValueError, AttributeError):
                    continue
                if isinstance(cwd, str):
                    return cwd
    except OSError:
        pass
    return ""


def _agent_type(path: Path) -> str:
    """The agent's type, from the ``.meta.json`` beside its transcript."""
    try:
        meta = json.loads(path.with_name(path.stem + ".meta.json").read_text(encoding="utf-8"))
        given = meta.get("agentType")
    except (OSError, ValueError, AttributeError):
        return "general-purpose"
    return given if isinstance(given, str) and given else "general-purpose"


def _run_replies(path: Path) -> list[str]:
    """The ids of the replies in the agent transcript at ``path``, oldest
    first, each once (``[]`` when it can't be read)."""
    try:
        records = HOOK._records(path.read_bytes())
    except OSError:
        return []
    replies = (
        record["message"].get("id")
        for record in records
        if record.get("type") == "assistant" and isinstance(record.get("message"), dict)
    )
    return list(dict.fromkeys(reply for reply in replies if isinstance(reply, str) and reply))


def replay_excerpt(label: Label) -> tuple[str, str, str]:
    """``(excerpt, reply id, why not)`` for a labelled run: the excerpt the
    worker would build from its transcript. ``why not`` is ``""`` when it
    built one, else ``"transcript missing"``, ``"nothing to judge"`` or
    ``"last reply differs"`` (the transcript has moved on since it was
    labelled, so the right words are about another end of the run)."""
    path = Path(label.transcript).expanduser()
    if not path.is_file():
        return "", "", "transcript missing"
    payload = {
        "agent_type": _agent_type(path), "agent_id": path.stem.removeprefix("agent-"), "cwd": _first_cwd(path),
    }
    excerpt, reply = _worker_excerpt(payload, path, _session_file(path))
    if not excerpt:
        return "", "", "nothing to judge"
    if reply != label.reply:
        return "", "", "last reply differs"
    return excerpt, reply, ""


# -- the labels file --------------------------------------------------------------

class Label(NamedTuple):
    """A hand-checked run: the path of its transcript, the id of its last
    reply, its answer shape and the right words."""

    transcript: str
    reply: str
    shape: str
    right: dict


LABEL_FIELDS = ("transcript", "agent_reply", "shape", "right")
#: An absolute (or home) path to an agent's transcript, ending as Claude
#: Code names one (``subagents/[workflows/<run>/]agent-<id>.jsonl``), and a
#: reply id as the parser keeps it (``haiku_tags``): a sentence in place of
#: either is refused.
_PATH_RE = re.compile(
    r"(?:[A-Za-z]:[\\/]|[\\/]|~[\\/])[\w .:\\/~()+@-]{0,400}[\\/]subagents[\\/](?:[\w.-]{1,80}[\\/]){0,2}"
    r"agent-[A-Za-z0-9_-]{1,64}\.jsonl"
)
_REPLY_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")


def labels_from(data) -> list[Label]:
    """The labels in ``data`` (a parsed labels file), or a ``ValueError``
    that names the entry and the field, never the value, when the file holds
    anything outside the closed spec: a field other than
    :data:`LABEL_FIELDS`, a ``right`` key other than :data:`LABEL_KEYS`, a
    word the key doesn't take, a shape outside :data:`SHAPES`, a path or a
    reply id of another form, free text anywhere, or one reply twice."""
    if not isinstance(data, list):
        raise ValueError("the labels file must hold a JSON list")
    vocab = CATALOGUE["judge"]["agent"]["vocab"]
    labels: list[Label] = []
    seen: set[str] = set()
    for n, entry in enumerate(data):
        where = f"entry {n}"
        if not isinstance(entry, dict) or set(entry) != set(LABEL_FIELDS):
            raise ValueError(f"{where}: must be an object with exactly the fields {', '.join(LABEL_FIELDS)}")
        transcript, reply, shape, right = (entry[field] for field in LABEL_FIELDS)
        if not isinstance(transcript, str) or not _PATH_RE.fullmatch(transcript):
            raise ValueError(
                f"{where}: transcript must be the absolute path of an agent's transcript "
                "(.../subagents/.../agent-<id>.jsonl)"
            )
        if not isinstance(reply, str) or not _REPLY_RE.fullmatch(reply):
            raise ValueError(f"{where}: agent_reply must be a reply id (letters, digits, _ and -)")
        if shape not in SHAPES:
            raise ValueError(f"{where}: shape must be one of {', '.join(SHAPES)}")
        if not isinstance(right, dict) or not right or not set(right) <= set(LABEL_KEYS):
            raise ValueError(f"{where}: right must be an object with some of the keys {', '.join(LABEL_KEYS)}")
        for key, word in right.items():
            if word not in vocab[key]:
                raise ValueError(f"{where}: right.{key} must be one of {', '.join(vocab[key])}")
        if reply in seen:
            raise ValueError(f"{where}: agent_reply appears in more than one entry")
        seen.add(reply)
        labels.append(Label(transcript, reply, shape, dict(right)))
    return labels


def load_labels(path) -> list[Label]:
    """:func:`labels_from` of the JSON file at ``path``, which must lie
    outside the repository: it names your own runs."""
    path = Path(path).expanduser()
    if path.resolve().is_relative_to(ROOT):
        raise ValueError("keep the labels file outside the repository")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as err:
        raise ValueError(f"can't read the labels file ({err.strerror})") from None
    except ValueError as err:
        raise ValueError(f"the labels file is not JSON (error at character {getattr(err, 'pos', 0)})") from None
    return labels_from(data)


# -- the verdicts the hook already stored --------------------------------------------

def stored_verdicts(config_dir) -> tuple[dict[str, dict], set[str]]:
    """``(verdicts, failed)`` from the agent lines in ``config_dir``'s tag
    files (``haiku_tags.load``): each reply's words (``result``, ``brief``),
    the newest line for a reply winning, and the replies whose only lines are
    calls that failed (an error line is no verdict and never replaces one
    that has words)."""
    verdicts: dict[str, dict] = {}
    failed: set[str] = set()
    for judged in haiku_tags.load(config_dir):
        if judged.kind != "agent":
            continue
        if judged.tag is None:
            failed.add(judged.reply)
            continue
        words = {"result": judged.result, "brief": judged.tag.brief}
        verdicts[judged.reply] = {key: word for key, word in words.items() if word}
    return verdicts, failed - set(verdicts)


class Row(NamedTuple):
    """One word Haiku (or the hook, in a stored verdict) said against the
    right one."""

    name: str
    shape: str
    key: str
    want: str | None
    said: str | None

    @property
    def ok(self) -> bool:
        return self.said == self.want


def score_stored(labels: list[Label], verdicts: dict[str, dict], failed: set[str]) -> tuple[list[Row], int, int]:
    """``(rows, without, failed_only)``: a row for each right word of each
    labelled run that has a stored verdict, how many labelled runs have
    none, and how many of those have only a failed call. A run's verdict is
    the one on its newest reply that has one, up to the labelled reply, as
    the dashboard reads it (``haiku_tags._apply_run``): a run judged before
    its last lines were written, or at an earlier stop, has it on an earlier
    reply than the labelled one. Without its transcript, only the labelled
    reply is looked up."""
    rows: list[Row] = []
    without = failed_only = 0
    for n, label in enumerate(labels):
        replies = _run_replies(Path(label.transcript).expanduser())
        replies = replies[: replies.index(label.reply) + 1] if label.reply in replies else [label.reply]
        verdict = next((verdicts[reply] for reply in reversed(replies) if reply in verdicts), None)
        if verdict is None:
            without += 1
            failed_only += any(reply in failed for reply in replies)
            continue
        rows += [Row(f"label {n}", label.shape, key, want, verdict.get(key)) for key, want in label.right.items()]
    return rows, without, failed_only


# -- asking Haiku and the report ---------------------------------------------------------

def _ask(excerpt: str) -> dict:
    job = {"system": cat.agent_judge_text(cat.level_includes("standard")), "excerpt": excerpt}
    answer = HOOK.ask_haiku(job, CATALOGUE["judge"], Path.cwd())
    agent = CATALOGUE["judge"]["agent"]
    keys = [key for metric_keys in agent["keys"].values() for key in metric_keys]
    words = HOOK.judge_tag(answer.get("result"), keys, CATALOGUE["judge"], agent["vocab"])
    got = dict(word.split("=", 1) for word in words.split())
    if got.get("retry") == "none":
        del got["retry"]
    return got


def judge(items: list[tuple[str, str, str, dict]], repeats: int) -> list[Row]:
    """Ask Haiku ``repeats`` times about each ``(name, shape, excerpt,
    right words)`` and score every right word of every answer."""
    runs = [(item, n) for item in items for n in range(repeats)]
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        answers = list(pool.map(lambda run: _ask(run[0][2]), runs))
    return [
        Row(name, shape, key, want, got.get(key))
        for ((name, shape, _excerpt, right), _n), got in zip(runs, answers)
        for key, want in right.items()
    ]


def _share(right: int, total: int) -> str:
    return f"{right}/{total} ({100 * right / total:.0f}%)" if total else "0/0"


def tally(rows: list[Row], by: str) -> dict[str, list[int]]:
    """``{key or shape: [right, total]}`` over ``rows``, in first-seen order
    (``by``: ``"key"`` or ``"shape"``)."""
    out: dict[str, list[int]] = {}
    for row in rows:
        counts = out.setdefault(getattr(row, by), [0, 0])
        counts[0] += row.ok
        counts[1] += 1
    return out


def print_runs(rows: list[Row]) -> None:
    """One line for each run: its right words, how often each came out
    right and what was said instead when not."""
    names = dict.fromkeys(row.name for row in rows)
    for name in names:
        cells = []
        by_key: dict[str, list[Row]] = {}
        for row in rows:
            if row.name == name:
                by_key.setdefault(row.key, []).append(row)
        for key, group in by_key.items():
            ok = sum(row.ok for row in group)
            said = [row.said for row in group]
            cell = f"{key}={group[0].want or '(none)'}: {ok}/{len(group)}"
            cells.append(cell + ("" if ok == len(group) else f" {said}"))
        print(f"{name:36} " + "; ".join(cells))


def print_scores(rows: list[Row]) -> None:
    """The scores per key and per answer shape, then overall."""
    right = sum(row.ok for row in rows)
    print("\nBy key")
    for key, (ok, total) in tally(rows, "key").items():
        print(f"  {key:10} {_share(ok, total)}")
    print("By answer shape")
    for shape, (ok, total) in tally(rows, "shape").items():
        print(f"  {shape:10} {_share(ok, total)}")
    print(f"\n{right} of {len(rows)} right ({100 * right / len(rows):.0f}%)" if rows else "\nNothing was scored.")


def print_done_share(rows: list[Row]) -> None:
    """How often ``result=done`` was said, against how often it is right."""
    results = [row for row in rows if row.key == "result"]
    if results:
        said = sum(row.said == "done" for row in results)
        right = sum(row.want == "done" for row in results)
        print(f"result=done: said for {_share(said, len(results))}, right for {_share(right, len(results))}")


def print_misses(rows: list[Row]) -> None:
    for row in rows:
        if not row.ok:
            print(f"  {row.name} ({row.shape}): {row.key} said {row.said or '(nothing)'}, right {row.want}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repeats", type=int, default=3, help="how many times Haiku judges each run")
    parser.add_argument(
        "--replay", metavar="FILE",
        help="judge the real runs a hand-made labels file names (its form is in this script's docstring)",
    )
    parser.add_argument(
        "--stored", metavar="CONFIG_DIR",
        help="with --replay: score the verdicts the hook already stored in CONFIG_DIR/tags, spending nothing",
    )
    args = parser.parse_args(argv)
    if args.stored and not args.replay:
        parser.error("--stored needs --replay FILE")
    labels: list[Label] = []
    if args.replay:
        try:
            labels = load_labels(args.replay)
        except ValueError as err:
            print(f"{args.replay}: {err}", file=sys.stderr)
            return 2
    if args.stored:
        config_dir = Path(args.stored).expanduser()
        if not haiku_tags.tags_dir(config_dir).is_dir():
            print(f"{args.stored}: no tags folder there, so no stored verdicts to score", file=sys.stderr)
            return 2
        verdicts, failed = stored_verdicts(config_dir)
        rows, without, failed_only = score_stored(labels, verdicts, failed)
        print(f"{len(labels) - without} of {len(labels)} labelled runs have a stored verdict; {without} have none"
              + (f" ({failed_only} of them only a failed call)" if failed_only else ""))
        print_done_share(rows)
        print_scores(rows)
        if any(not row.ok for row in rows):
            print("\nMisses")
            print_misses(rows)
        return 0
    if args.replay:
        items, skipped = [], {}
        for n, label in enumerate(labels):
            excerpt, _reply, why = replay_excerpt(label)
            if why:
                skipped[why] = skipped.get(why, 0) + 1
            else:
                items.append((f"label {n}", label.shape, excerpt, label.right))
        if skipped:
            why = ", ".join(f"{count} {reason}" for reason, count in skipped.items())
            print(f"skipped {sum(skipped.values())} of {len(labels)}: {why}")
    else:
        items = [(name, case.shape, case_excerpt(case)[0], case.right) for name, case in CASES.items()]
    try:
        rows = judge(items, args.repeats)
    except HOOK.NoLogin:
        print("claude is not signed in: run `claude` once and log in, then try again.", file=sys.stderr)
        return 1
    except FileNotFoundError:
        print("the claude command was not found.", file=sys.stderr)
        return 1
    print_runs(rows)
    if args.replay:
        print_done_share(rows)
    print_scores(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())

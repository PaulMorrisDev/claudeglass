"""``scripts/eval-agent-judge.py``: the known-answer cases, the labels file
and the scoring, which spend nothing. Asking Haiku is stubbed here: a real
call needs a ``claude`` login and never runs in the suite.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "eval-agent-judge.py"


def _load():
    spec = importlib.util.spec_from_file_location("_eval_agent_judge_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EVAL = _load()
VOCAB = EVAL.CATALOGUE["judge"]["agent"]["vocab"]
LIMITS = EVAL.CATALOGUE["judge"]["agent"]["limits"]
ANSWER_TOOLS = set(EVAL.CATALOGUE["judge"]["agent"]["answer_tools"])


def _answer_call(case):
    """The input of the last answer-tool call in the case's transcript, as
    ``(tool name, input)``, or ``None``."""
    found = None
    for record in case.records:
        if record.get("type") != "assistant":
            continue
        for block in record["message"]["content"]:
            if block.get("type") == "tool_use" and block["name"] in ANSWER_TOOLS:
                found = (block["name"], block["input"])
    return found


def _two_frame(case) -> bool:
    first = case.records[0]["message"]["content"]
    return first.startswith("[Workflow harness — user request]")


def _quoted(excerpt: str, start: str) -> str:
    """The text between the quotes of the excerpt line that begins ``start``."""
    line = next(line for line in excerpt.splitlines() if line.startswith(start))
    return line[line.index('"') + 1 : line.rindex('"')]


# -- the cases ----------------------------------------------------------------------

def test_every_case_has_a_shape_from_the_closed_list():
    assert all(case.shape in EVAL.SHAPES for case in EVAL.CASES.values())
    # and each word of the list is something a case measures
    assert {case.shape for case in EVAL.CASES.values()} == set(EVAL.SHAPES)


def test_every_right_word_is_a_word_the_judge_takes():
    for name, case in EVAL.CASES.items():
        assert case.right, name
        for key, word in case.right.items():
            assert key in VOCAB, (name, key)
            assert word is None or word in VOCAB[key], (name, key, word)


@pytest.mark.parametrize("name", list(EVAL.CASES))
def test_every_cases_excerpt_builds_with_the_hooks_agent_excerpt(name):
    case = EVAL.CASES[name]
    excerpt, reply = EVAL.case_excerpt(case)
    assert excerpt.startswith(f"Agent type: {case.agent_type}")
    last = [r for r in case.records if r["type"] == "assistant"][-1]["message"]["id"]
    assert reply == last
    assert "Workflow harness" not in excerpt  # the frame lines are stripped
    if not case.late:
        # the same text the hook builds from the same records
        expected = EVAL.HOOK.agent_excerpt(
            case.records, {"agent_type": case.agent_type, "cwd": "/w"}, EVAL.CATALOGUE, case.earlier, case.said
        )
        assert (excerpt, reply) == expected


def test_the_new_cases_are_there():
    assert {
        "two-frame: Continue relay", "answer: three findings", "answer: confirmed and refuted",
        "answer: done, with concerns", "answer: research with risks", "handback, then Report delivered.",
        "re-stop after a guard rejection", "answer written after the hook fired",
        "same brief: done run", "same brief: blocked run",
    } <= set(EVAL.CASES)


def test_two_frame_cases_put_the_computed_task_in_the_brief_and_the_relay_beside_it():
    two_frame = {name: case for name, case in EVAL.CASES.items() if _two_frame(case)}
    assert "two-frame: Continue relay" in two_frame and len(two_frame) >= 5
    for name, case in two_frame.items():
        excerpt, _ = EVAL.case_excerpt(case)
        computed = case.records[1]["message"]["content"].split("computed task text follows:\n  ", 1)[1]
        relay = case.records[0]["message"]["content"].split("start the workflow:\n  ", 1)[1]
        assert _quoted(excerpt, "Its brief:") == computed, name
        relay_line = _quoted(excerpt, "The line the workflow was started with (context, not the brief):")
        assert relay_line == relay and len(relay_line) <= LIMITS["relay"], name
        assert relay not in _quoted(excerpt, "Its brief:"), name


def test_the_continue_relay_is_the_line_and_the_task_is_the_brief():
    excerpt, _ = EVAL.case_excerpt(EVAL.CASES["two-frame: Continue relay"])
    assert 'started with (context, not the brief): "Continue"' in excerpt
    assert _quoted(excerpt, "Its brief:").startswith("Add a retry limit of 3 to fetch_page()")
    assert EVAL.CASES["two-frame: Continue relay"].right == {"result": "done", "brief": "clear"}


@pytest.mark.parametrize(
    "name", [n for n, c in EVAL.CASES.items() if _answer_call(c) and _answer_call(c)[0] == "StructuredOutput"]
)
def test_answer_tool_cases_render_the_answer_field_by_field(name):
    case = EVAL.CASES[name]
    excerpt, _ = EVAL.case_excerpt(case)
    _tool, given = _answer_call(case)
    assert "Its answer, handed back as structured output, field by field:" in excerpt
    lines = excerpt.splitlines()
    start = lines.index("Its answer, handed back as structured output, field by field:") + 1
    for offset, key in enumerate(given):
        assert lines[start + offset].startswith(f"  {key}: "), (name, key)
    assert "The end of its report" not in excerpt


def test_an_answer_that_lists_concerns_shows_its_summary_before_them():
    excerpt, _ = EVAL.case_excerpt(EVAL.CASES["answer: done, with concerns"])
    assert excerpt.index("  summary: ") < excerpt.index("  files_changed: ") < excerpt.index("  concerns: ")
    assert EVAL.CASES["answer: done, with concerns"].right["result"] == "done"
    assert EVAL.CASES["answer: research with risks"].right["result"] == "done"


def test_findings_and_a_validators_verdicts_are_done():
    for name in (
        "answer: three findings", "answer: confirmed and refuted", "answer: empty list", "answer: refuted claim"
    ):
        assert EVAL.CASES[name].right["result"] == "done", name
    three = _answer_call(EVAL.CASES["answer: three findings"])[1]
    assert len(three["findings"]) == 3
    claims = _answer_call(EVAL.CASES["answer: confirmed and refuted"])[1]["claims"]
    assert {claim["verdict"] for claim in claims} == {"confirmed", "refuted"}


def test_a_handback_stays_the_answer_after_a_short_closing_remark():
    case = EVAL.CASES["handback, then Report delivered."]
    excerpt, reply = EVAL.case_excerpt(case)
    assert _answer_call(case)[0] == "SubagentHandback"
    assert "handed back through a handback tool" in excerpt
    assert 'After the answer it added: "Report delivered."' in excerpt
    assert "The end of its report" not in excerpt
    assert reply == "msg_3"  # the closing remark's reply is the run's last
    assert case.right == {"result": "done"} and case.shape == "handback"


def test_a_re_stop_is_scored_on_the_final_answer():
    case = EVAL.CASES["re-stop after a guard rejection"]
    excerpt, reply = EVAL.case_excerpt(case)
    first_report = next(
        block["text"] for r in case.records if r["type"] == "assistant"
        for block in r["message"]["content"] if block.get("type") == "text"
    )
    assert "have not run the tests yet" in first_report
    assert "have not run the tests" not in excerpt  # the report the guard rejected
    assert "tests/test_paging.py passes (11 tests)" in excerpt
    assert "Shell commands: `pytest -q tests/test_paging.py`." in excerpt  # the work done after the rejection
    assert reply == "msg_4" and case.restop
    # the hook's feedback line is not read as a message to the agent
    assert _quoted(excerpt, "Its brief:").startswith("Fix the off-by-one in paginate()")
    assert case.right["result"] == "done"


def test_a_run_whose_answer_is_written_after_the_hook_fired_is_read_after_waiting(tmp_path):
    case = EVAL.CASES["answer written after the hook fired"]
    assert case.late == 1
    # what is on disk when the hook fires has no answer yet
    path = tmp_path / "agent-a1.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in case.records[:-1]), encoding="utf-8")
    assert not EVAL.HOOK._answer_called(str(path), ANSWER_TOOLS)
    payload = {"agent_type": case.agent_type, "cwd": "/w"}
    early = EVAL.HOOK.agent_excerpt(case.records[:-1], payload, EVAL.CATALOGUE, [], "")[0]
    assert "handed back as structured output" not in early
    # what the worker reads once it has waited does
    excerpt, reply = EVAL.case_excerpt(case)
    assert "Its answer, handed back as structured output, field by field:" in excerpt
    assert "  count: 2" in excerpt and "delete_user" in excerpt
    assert reply == "msg_2" and case.right == {"result": "done"}


def test_the_same_brief_is_judged_on_a_done_run_and_on_a_blocked_run():
    done, blocked = EVAL.CASES["same brief: done run"], EVAL.CASES["same brief: blocked run"]
    done_excerpt, _ = EVAL.case_excerpt(done)
    blocked_excerpt, _ = EVAL.case_excerpt(blocked)
    assert _quoted(done_excerpt, "Its brief:") == _quoted(blocked_excerpt, "Its brief:")
    assert (done.right, blocked.right) == (
        {"result": "done", "brief": "clear"}, {"result": "blocked", "brief": "clear"}
    )
    assert (done.shape, blocked.shape) == ("report", "blocked")
    assert "tool errors: 2" in blocked_excerpt and "tool errors: 0" in done_excerpt


def test_a_blocked_handback_is_the_blocked_shape():
    case = EVAL.CASES["handback: could not work"]
    assert _answer_call(case)[0] == "SubagentHandback" and case.shape == "blocked"


# -- the labels file -------------------------------------------------------------------

def _label(**changes):
    entry = {
        "transcript": "C:\\Users\\me\\.claude\\projects\\p\\s1\\subagents\\agent-a1.jsonl",
        "agent_reply": "msg_01AbC", "shape": "report", "right": {"result": "done", "brief": "clear"},
    }
    entry.update(changes)
    return entry


def test_labels_load_from_a_file(tmp_path):
    other = _label(agent_reply="msg_2", transcript="/home/me/.claude/projects/p/s2/subagents/agent-a2.jsonl",
                   shape="blocked", right={"result": "blocked"})
    path = tmp_path / "labels.json"
    path.write_text(json.dumps([_label(), other]), encoding="utf-8")
    labels = EVAL.load_labels(path)
    assert [(l.reply, l.shape, l.right) for l in labels] == [
        ("msg_01AbC", "report", {"result": "done", "brief": "clear"}), ("msg_2", "blocked", {"result": "blocked"}),
    ]


@pytest.mark.parametrize(
    "entry",
    [
        _label(note="this agent did well"),  # a field outside the spec
        {k: v for k, v in _label().items() if k != "shape"},  # a field left out
        _label(right={"result": "done", "comment": "good"}),  # a key outside the spec
        _label(right={"result": "done", "retry": "model"}),  # a real key, but not one labels give
        _label(right={"result": "Finished all of the work."}),  # free text for a word
        _label(right={"result": "complete"}),  # an unknown word
        _label(right={"brief": "good"}),
        _label(right={}),
        _label(right="done"),
        _label(shape="a long findings list"),  # a shape outside the list
        _label(shape=None),
        _label(agent_reply="the agent's last reply"),  # not an id
        _label(agent_reply=""),
        _label(agent_reply=7),
        _label(transcript="the transcript where it forgot the tests"),  # not a path
        _label(transcript="C:\\work\\notes.txt"),  # not a transcript
        _label(transcript="relative/agent-a1.jsonl"),
        _label(transcript="/x/agent, it forgot the tests.jsonl"),
        _label(transcript="/x/the agent forgot the tests.jsonl"),  # a sentence, not an agent's transcript
        _label(transcript="C:\\Users\\me\\.claude\\projects\\p\\s1.jsonl"),  # a session's own transcript
        _label(transcript=["/x/agent-a1.jsonl"]),
        "msg_1",
    ],
)
def test_the_labels_loader_rejects_anything_outside_the_closed_spec(entry):
    with pytest.raises(ValueError):
        EVAL.labels_from([entry])


def test_the_labels_loader_rejects_a_file_that_is_not_a_list_of_labels(tmp_path):
    for data in ({"a": 1}, "labels", None, 3):
        with pytest.raises(ValueError):
            EVAL.labels_from(data)
    path = tmp_path / "labels.json"
    path.write_text("[{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        EVAL.load_labels(path)
    with pytest.raises(ValueError):
        EVAL.load_labels(tmp_path / "missing.json")


def test_the_labels_loader_rejects_one_reply_twice():
    with pytest.raises(ValueError, match="more than one"):
        EVAL.labels_from([_label(), _label(shape="findings")])


def test_a_labels_file_inside_the_repository_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(EVAL, "ROOT", tmp_path.resolve())
    path = tmp_path / "labels.json"
    path.write_text(json.dumps([_label()]), encoding="utf-8")
    with pytest.raises(ValueError, match="outside the repository"):
        EVAL.load_labels(path)


def test_a_rejection_never_repeats_the_text_it_rejected():
    secret = "the agent leaked SECRET-TOKEN-123 here"
    for entry in (_label(note=secret), _label(right={"result": secret}), _label(shape=secret),
                  _label(agent_reply=secret), _label(transcript=secret), _label(right={secret: "done"})):
        with pytest.raises(ValueError) as err:
            EVAL.labels_from([entry])
        assert "SECRET-TOKEN-123" not in str(err.value)


def test_a_real_looking_path_with_spaces_and_a_drive_is_a_path():
    path = "C:\\Users\\Some One\\.claude\\projects\\C--Dev-x\\4af5-b8\\subagents\\workflows\\run_1\\agent-a1b2.jsonl"
    assert EVAL.labels_from([_label(transcript=path)])[0].transcript == path


# -- scoring the verdicts the hook already stored -------------------------------------------

def _tag_file(config_dir: Path, lines: list[dict], month: str = "2026-09") -> None:
    folder = config_dir / "tags"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{month}.jsonl").write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")


def _line(reply, words=None, ts="2026-09-27T10:00:00Z", **extra):
    record = {"ts": ts, "reply": reply, "agent": words if words is not None else ""}
    record.update(extra)
    return record


def test_stored_verdicts_keep_the_newest_line_for_a_reply(tmp_path):
    _tag_file(tmp_path, [
        _line("msg_a", "brief=vague missing=goal result=partial", ts="2026-09-27T10:00:00Z"),
        _line("msg_a", "brief=clear missing=none result=done", ts="2026-09-27T10:05:00Z"),
        _line("msg_b", "brief=clear missing=none result=blocked", ts="2026-09-27T11:00:00Z"),
        _line("msg_b", ts="2026-09-27T11:05:00Z", err="timeout"),  # a failed call is no verdict
        _line("msg_c", ts="2026-09-27T12:00:00Z", err="no_answer"),
        {"ts": "2026-09-27T12:30:00Z", "reply": "msg_d", "tl": "task=bugfix brief=clear"},  # the main session's
    ])
    verdicts, failed = EVAL.stored_verdicts(tmp_path)
    assert verdicts == {
        "msg_a": {"result": "done", "brief": "clear"}, "msg_b": {"result": "blocked", "brief": "clear"},
    }
    assert failed == {"msg_c"}


def test_stored_verdicts_are_empty_without_tag_files(tmp_path):
    assert EVAL.stored_verdicts(tmp_path / "nothing") == ({}, set())


def test_scoring_stored_verdicts_joins_on_the_reply_and_counts_the_unmatched():
    labels = [
        EVAL.Label("/x/a.jsonl", "msg_a", "report", {"result": "done", "brief": "clear"}),
        EVAL.Label("/x/b.jsonl", "msg_b", "findings", {"result": "done"}),
        EVAL.Label("/x/c.jsonl", "msg_c", "blocked", {"result": "blocked"}),
        EVAL.Label("/x/d.jsonl", "msg_d", "report", {"result": "done"}),
        EVAL.Label("/x/e.jsonl", "msg_e", "report", {"result": "done", "brief": "vague"}),
    ]
    verdicts = {
        "msg_a": {"result": "done", "brief": "clear"},
        "msg_b": {"result": "partial", "brief": "clear"},
        "msg_e": {"brief": "vague"},  # no result word stored
        "msg_zzz": {"result": "done"},  # a run nobody labelled
    }
    rows, without, failed_only = EVAL.score_stored(labels, verdicts, {"msg_c"})
    assert (without, failed_only) == (2, 1)  # msg_c (a failed call) and msg_d (nothing stored)
    assert [(r.name, r.key, r.want, r.said, r.ok) for r in rows] == [
        ("label 0", "result", "done", "done", True),
        ("label 0", "brief", "clear", "clear", True),
        ("label 1", "result", "done", "partial", False),
        ("label 4", "result", "done", None, False),
        ("label 4", "brief", "vague", "vague", True),
    ]
    assert EVAL.tally(rows, "key") == {"result": [1, 3], "brief": [2, 2]}
    assert EVAL.tally(rows, "shape") == {"report": [3, 4], "findings": [0, 1]}


def test_a_stored_verdict_on_an_earlier_reply_of_the_run_is_the_runs(tmp_path):
    path = _write_run(tmp_path, "a1", EVAL.CASES["done"].records)  # replies msg_1a1, msg_2a1
    label = EVAL.Label(str(path), "msg_2a1", "report", {"result": "done"})
    rows, without, failed_only = EVAL.score_stored([label], {"msg_1a1": {"result": "partial"}}, set())
    assert (without, failed_only) == (0, 0)
    assert [(r.key, r.want, r.said) for r in rows] == [("result", "done", "partial")]
    rows, _, _ = EVAL.score_stored([label], {"msg_1a1": {"result": "partial"}, "msg_2a1": {"result": "done"}}, set())
    assert [r.said for r in rows] == ["done"]
    assert EVAL.score_stored([label], {}, {"msg_1a1"})[1:] == (1, 1)
    # a reply after the labelled one is another end of the run, and is not read
    rows, without, _ = EVAL.score_stored([label._replace(reply="msg_1a1")], {"msg_2a1": {"result": "done"}}, set())
    assert (rows, without) == ([], 1)

    assert EVAL._run_replies(path) == ["msg_1a1", "msg_2a1"]
    assert EVAL._run_replies(tmp_path / "gone.jsonl") == []


def test_stored_scoring_through_main_spends_nothing(tmp_path, capsys, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a stored score must not ask Haiku")

    monkeypatch.setattr(EVAL.HOOK, "ask_haiku", refuse)
    config = tmp_path / "config"
    _tag_file(config, [
        _line("msg_a", "brief=clear missing=none result=done"),
        _line("msg_b", "brief=clear missing=none result=done"),
        _line("msg_c", "brief=vague missing=goal result=partial"),
        _line("msg_x", ts="2026-09-27T12:00:00Z", err="timeout"),
    ])
    labels = tmp_path / "labels.json"
    labels.write_text(json.dumps([
        _label(agent_reply="msg_a", shape="report", right={"result": "done", "brief": "clear"}),
        _label(agent_reply="msg_b", shape="concerns", right={"result": "done"}),
        _label(agent_reply="msg_c", shape="blocked", right={"result": "blocked"}),
        _label(agent_reply="msg_x", shape="findings", right={"result": "done"}),
        _label(agent_reply="msg_y", shape="findings", right={"result": "done"}),
    ]), encoding="utf-8")
    assert EVAL.main(["--replay", str(labels), "--stored", str(config)]) == 0
    out = capsys.readouterr().out
    assert "3 of 5 labelled runs have a stored verdict; 2 have none (1 of them only a failed call)" in out
    assert "result=done: said for 2/3 (67%), right for 2/3 (67%)" in out
    for line in (r"result\s+2/3 \(67%\)", r"brief\s+1/1 \(100%\)", r"report\s+2/2 \(100%\)",
                 r"blocked\s+0/1 \(0%\)", r"concerns\s+1/1 \(100%\)"):
        assert re.search(rf"^  {line}$", out, re.M), line
    assert "label 2 (blocked): result said partial, right blocked" in out
    assert "3 of 4 right (75%)" in out


def test_stored_needs_a_replay_file(capsys):
    with pytest.raises(SystemExit):
        EVAL.main(["--stored", "somewhere"])
    assert "--replay" in capsys.readouterr().err


def test_stored_needs_a_folder_with_tags(tmp_path, capsys):
    labels = tmp_path / "labels.json"
    labels.write_text(json.dumps([_label()]), encoding="utf-8")
    assert EVAL.main(["--replay", str(labels), "--stored", str(tmp_path / "nowhere")]) == 2
    assert "no tags folder" in capsys.readouterr().err


def test_a_bad_labels_file_stops_the_replay_before_anything_is_asked(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(EVAL.HOOK, "ask_haiku", lambda *a, **k: pytest.fail("asked Haiku"))
    labels = tmp_path / "labels.json"
    labels.write_text(json.dumps([_label(note="free text")]), encoding="utf-8")
    assert EVAL.main(["--replay", str(labels)]) == 2
    assert "exactly the fields" in capsys.readouterr().err
    assert EVAL.main(["--replay", str(labels), "--stored", str(tmp_path)]) == 2


# -- replaying real runs against Haiku (stubbed) ---------------------------------------------

def _write_run(folder: Path, name: str, records: list[dict], agent_type: str = "Explore") -> Path:
    """The run's transcript under a session folder, as Claude Code lays it
    out, with each reply id ending in the agent's name (``msg_2a1``)."""
    path = folder / "sess1" / "subagents" / f"agent-{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for record in records:
        record = {**record, "cwd": "/w"}
        if record["type"] == "assistant":
            record["message"] = {**record["message"], "id": record["message"]["id"] + name}
        lines.append(json.dumps(record) + "\n")
    path.write_text("".join(lines), encoding="utf-8")
    path.with_name(f"agent-{name}.meta.json").write_text(json.dumps({"agentType": agent_type}), encoding="utf-8")
    session = {"type": "user", "message": {"content": "hello"}}
    (folder / "sess1.jsonl").write_text(json.dumps(session) + "\n", encoding="utf-8")
    return path


def test_replay_builds_each_excerpt_from_the_transcript_and_asks_haiku(tmp_path, capsys, monkeypatch):
    done = _write_run(tmp_path, "a1", EVAL.CASES["done"].records)
    answer = _write_run(tmp_path, "a2", EVAL.CASES["answer: three findings"].records, agent_type="workflow-subagent")
    stale = _write_run(tmp_path, "a3", EVAL.CASES["done"].records)
    asked = []

    def haiku(job, judge, cwd=None):
        asked.append(job["excerpt"])
        return {"result": "[cg: brief=clear missing=none result=done retry=none]"}

    monkeypatch.setattr(EVAL.HOOK, "ask_haiku", haiku)
    labels = tmp_path / "labels.json"
    labels.write_text(json.dumps([
        _label(transcript=str(done), agent_reply="msg_2a1", shape="report", right={"result": "done", "brief": "clear"}),
        _label(transcript=str(answer), agent_reply="msg_2a2", shape="findings", right={"result": "partial"}),
        _label(transcript=str(stale), agent_reply="msg_77", shape="report", right={"result": "done"}),
        _label(transcript=str(tmp_path / "sess1" / "subagents" / "agent-gone.jsonl"), agent_reply="msg_9",
               shape="report", right={"result": "done"}),
    ]), encoding="utf-8")
    assert EVAL.main(["--replay", str(labels), "--repeats", "2"]) == 0
    out = capsys.readouterr().out
    assert "skipped 2 of 4: 1 last reply differs, 1 transcript missing" in out
    assert len(asked) == 4  # two runs, twice each
    assert sum(e.startswith("Agent type: Explore, on claude-sonnet-5.") for e in asked) == 2
    assert sum(e.startswith("Agent type: workflow-subagent") and "field by field" in e for e in asked) == 2
    assert "label 0" in out and "result=done: 2/2" in out and "brief=clear: 2/2" in out
    assert "label 1" in out and "result=partial: 0/2" in out
    assert "result=done: said for 4/4 (100%), right for 2/4 (50%)" in out
    # the shape table counts words, not runs: label 0 has two right words, each asked twice
    assert re.search(r"^  report\s+4/4 \(100%\)$", out, re.M)
    assert re.search(r"^  findings\s+0/2 \(0%\)$", out, re.M)


def test_replay_excerpt_is_the_one_the_hooks_worker_builds(tmp_path):
    path = _write_run(tmp_path, "a1", EVAL.CASES["done"].records)
    label = EVAL.Label(str(path), "msg_2a1", "report", {"result": "done"})
    excerpt, reply, why = EVAL.replay_excerpt(label)
    assert (reply, why) == ("msg_2a1", "")
    assert excerpt.startswith("Agent type: Explore, on claude-sonnet-5.")
    assert 'The end of its report: "Added docstrings to all four functions in mod1.py and mod2.py."' in excerpt
    assert EVAL.replay_excerpt(label._replace(reply="msg_1"))[2] == "last reply differs"
    assert EVAL.replay_excerpt(label._replace(transcript=str(tmp_path / "gone.jsonl")))[2] == "transcript missing"


def test_a_signed_out_claude_stops_a_run_with_a_message(capsys, monkeypatch):
    def signed_out(*args, **kwargs):
        raise EVAL.HOOK.NoLogin()

    monkeypatch.setattr(EVAL.HOOK, "ask_haiku", signed_out)
    assert EVAL.main(["--repeats", "1"]) == 1
    assert "not signed in" in capsys.readouterr().err


# -- the cases through main (stubbed) -------------------------------------------------------------

def test_the_cases_print_accuracy_per_key_and_per_shape(capsys, monkeypatch):
    def stub(job, judge, cwd=None):
        excerpt = job["excerpt"]
        result = "blocked" if "could not" in excerpt or "doesn't exist" in excerpt else "done"
        return {"result": f"[cg: brief=clear missing=none result={result}]"}

    monkeypatch.setattr(EVAL.HOOK, "ask_haiku", stub)
    assert EVAL.main(["--repeats", "1"]) == 0
    out = capsys.readouterr().out
    assert "By key" in out and "By answer shape" in out
    for shape in EVAL.SHAPES:
        assert re.search(rf"^  {shape}\s+\d+/\d+ \(\d+%\)$", out, re.M), shape
    for key in ("result", "brief", "retry"):
        assert re.search(rf"^  {key}\s+\d+/\d+ \(\d+%\)$", out, re.M), key
    assert "two-frame: Continue relay" in out
    assert re.search(r"^\d+ of \d+ right \(\d+%\)$", out, re.M)


def test_tally_counts_each_row_once_per_key_and_per_shape():
    rows = [
        EVAL.Row("a", "report", "result", "done", "done"),
        EVAL.Row("a", "report", "retry", None, None),
        EVAL.Row("b", "findings", "result", "done", "blocked"),
    ]
    assert EVAL.tally(rows, "key") == {"result": [1, 2], "retry": [1, 1]}
    assert EVAL.tally(rows, "shape") == {"report": [2, 2], "findings": [0, 1]}

"""``scripts/eval-tagger.py``: the tag scoring and the transcript trimming,
which spend nothing. Recording and judging call ``claude`` and never run
here.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "eval-tagger.py"


def _load():
    spec = importlib.util.spec_from_file_location("_eval_tagger_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EVAL = _load()


@pytest.mark.parametrize(
    "expected, got, ok",
    [
        (["bugfix"], "bugfix", True),
        (["bugfix", "debug"], "debug", True),
        (["bugfix"], "feature", False),
        ([None], None, True),
        ([None], "yes", False),
        (["none", None], None, True),
        (["~files|repro"], "files,goal", True),
        (["~files|repro"], "goal", False),
        (["~files|repro"], "none", False),
        (["~files|repro"], None, False),
    ],
)
def test_accepts(expected, got, ok):
    assert EVAL.accepts(expected, got) is ok


def test_the_keys_scored_carry_why_and_admit_with_shift_and_not_the_dropped_ones():
    assert EVAL.KEYS[EVAL.KEYS.index("shift"):][:3] == ["shift", "why", "admit"]
    # why and admit are keys, not metrics to switch on; found is dropped.
    assert "why" not in EVAL.METRIC_IDS and "admit" not in EVAL.METRIC_IDS
    assert "found" not in EVAL.KEYS and "fit" not in EVAL.KEYS


def test_scenarios_only_expect_known_keys_and_words():
    vocab = EVAL.CATALOGUE["judge"]["vocab"]
    for scenario in EVAL._scenarios():
        assert scenario["turns"] and scenario["id"]
        for key, options in scenario["expect"].items():
            assert key in EVAL.KEYS, (scenario["id"], key)
            if options == "auto":
                continue
            for option in options:
                if option is None:
                    continue
                for word in option.lstrip("~").split("|"):
                    assert word in vocab[key], (scenario["id"], key, word)


def test_a_skill_is_scored_by_whether_one_ran():
    scenario = {"expect": {"skill": "auto"}}
    assert EVAL.expected_for(scenario, {"skill_ran": True})["skill"] == ["helped", "unneeded"]
    assert None in EVAL.expected_for(scenario, {"skill_ran": False})["skill"]


def test_the_trimmed_transcript_keeps_what_the_excerpt_reads_and_no_tool_output():
    record = {
        "type": "assistant", "uuid": "u", "timestamp": "t", "cwd": "/secret", "gitBranch": "main",
        "message": {"id": "msg_1", "role": "assistant", "usage": {"output_tokens": 5, "service_tier": "x"},
                    "content": [{"type": "thinking", "thinking": "hmm"},
                                {"type": "tool_use", "id": "t", "name": "Bash",
                                 "input": {"command": "pytest", "description": "run", "timeout": 5}},
                                {"type": "text", "text": "Done."}]},
    }
    trimmed = EVAL._trim(record)
    assert "cwd" not in trimmed and "gitBranch" not in trimmed
    assert trimmed["message"]["usage"] == {"output_tokens": 5}
    assert trimmed["message"]["content"] == [
        {"type": "tool_use", "id": "t", "name": "Bash", "input": {"command": "pytest"}},
        {"type": "text", "text": "Done."},
    ]
    result = {"type": "user", "toolUseResult": {"stdout": "lots"}, "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t", "content": "secret output", "is_error": True}]}}
    trimmed = EVAL._trim(result)
    assert trimmed["toolUseResult"] is True
    assert trimmed["message"]["content"] == [{"type": "tool_result", "tool_use_id": "t", "is_error": True, "content": ""}]
    assert EVAL._trim({"type": "attachment", "attachment": {}}) is None


def test_haiku_never_sees_claudes_own_tag():
    records = [{"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Fixed it.\n\n`[cg: task=bugfix brief=clear]`"}]}}]
    assert EVAL._untagged(records)[0]["message"]["content"][0]["text"] == "Fixed it."
    assert records[0]["message"]["content"][0]["text"].endswith("]`")


def test_the_report_scores_each_judge_and_claudes_own_tags():
    scenarios = {
        "a": {"id": "a", "expect": {"task": ["bugfix"], "prior": [None]}},
        "b": {"id": "b", "expect": {"task": ["research"], "prior": ["needed"]}},
    }

    def run(tag, raw=None):
        return {"tag": tag, "raw": raw or tag, "usd": 0.002, "seconds": 2.0, "thinking": 0}

    results = {
        "when": "2026-09-27T00:00:00+00:00",
        "repeats": 2,
        "configs": {"haiku": {}},
        "scenarios": {
            "a": {"claude": "task=bugfix prior=needed", "skill_ran": False,
                  "runs": {"haiku": [run("task=bugfix"), run("task=bugfix", "task=bugfix prior=needed")]}},
            "b": {"claude": "task=research prior=needed", "skill_ran": False,
                  "runs": {"haiku": [run("task=research prior=needed"), run("task=docs prior=needed")]}},
        },
    }
    report = EVAL.score(results, scenarios)
    # 7 of 8 scored answers right; 6 of 8 before corrections; task differs between b's runs.
    assert "| haiku | 88% | 75% |" in report
    assert "| Claude's own tags (the session's model, one run) | 75% |" in report
    assert "a.prior: said needed, right is (left out)" in report
    assert json.dumps(results)  # stays JSON-safe


# -- the sessions written by hand ---------------------------------------------------

#: The scenarios whose sessions are written by hand, never recorded.
HAND = (
    "plan_build_rename", "plan_build_three_fixes", "drip_three_small", "claude_wrong", "claude_admits",
    "queued_correction", "ho_post_plan_tweak", "ho_queued_scope", "desk_plan_rounds", "desk_interpreter_tests",
    "desk_powershell_tests", "desk_replay_block", "desk_screenshot", "desk_opus_workflow",
)
HELD_OUT = ("ho_post_plan_tweak", "ho_queued_scope")
DESKTOP = tuple(name for name in HAND if name.startswith("desk_"))
#: The sessions with a message typed while Claude worked, and what it says.
QUEUED = {
    "queued_correction": "no wait, it has to be `remove NAME QUANTITY FILE` and write the file back, don't just print",
    "ho_queued_scope": "and show that count at the bottom of the report in report.py, with a test for it",
}


def _hand() -> dict[str, dict]:
    return {s["id"]: s for s in EVAL._scenarios() if s.get("source") == "hand"}


def _session(name: str) -> list[dict]:
    return EVAL._records(EVAL.SESSIONS / f"{name}.jsonl")


def _excerpt(name: str) -> str:
    return EVAL._job(name)[0]["excerpt"]


def _queued(prompt, **attachment) -> dict:
    return {"type": "attachment", "uuid": "q", "parentUuid": "p", "isSidechain": False, "timestamp": "t",
            "cwd": "/secret", "entrypoint": "cli",
            "attachment": {"type": "queued_command", "prompt": prompt, "commandMode": "prompt", **attachment}}


def test_scenarios_use_only_known_fields_and_values():
    for scenario in EVAL._scenarios():
        assert set(scenario) <= {"id", "set", "source", "entrypoint", "about", "turns", "expect", "mode"}, scenario["id"]
        assert scenario.get("set") in (None, "holdout"), scenario["id"]
        assert scenario.get("source") in (None, "hand"), scenario["id"]
        assert scenario.get("entrypoint") in (None, "claude-desktop"), scenario["id"]
        if scenario.get("source") == "hand":
            assert scenario.get("about"), scenario["id"]


def test_the_hand_written_scenarios_are_marked_as_such_held_out_or_desktop():
    hand = _hand()
    assert set(hand) == set(HAND)
    for name in HAND:
        assert hand[name]["about"], name
        assert (hand[name].get("set") == "holdout") is (name in HELD_OUT), name
        assert (hand[name].get("entrypoint") == "claude-desktop") is (name in DESKTOP), name
    # Only the hand-written ones are desktop sessions, and their lines say so.
    assert {s["id"] for s in EVAL._scenarios() if s.get("entrypoint")} == set(DESKTOP)
    for name in HAND:
        assert all(r.get("entrypoint") == "claude-desktop" for r in _session(name)) is (name in DESKTOP), name
        assert any(r.get("entrypoint") for r in _session(name)) is (name in DESKTOP), name


def test_each_hand_session_is_there_parses_and_has_one_typed_message_for_each_turn():
    prefix = EVAL.CATALOGUE["coaching"]["interrupt_prefix"]
    for name, scenario in _hand().items():
        path = EVAL.SESSIONS / f"{name}.jsonl"
        assert path.is_file(), name
        records = EVAL._records(path)
        assert {r["type"] for r in records} <= {"user", "assistant", "attachment"}, name
        typed = [r for r in EVAL.HOOK._ordered(records)[0] if EVAL.HOOK._typed(r, prefix)]
        assert len(typed) == len(scenario["turns"]), name
        assert EVAL._final_text(records), name
        # In the order the transcript is written: nothing runs backwards, bar a replay.
        assert all(isinstance(r.get("timestamp"), str) and r.get("uuid") for r in records), name


def test_record_skips_hand_written_scenarios_and_says_so(tmp_path, monkeypatch, capsys):
    def never(*args, **kwargs):
        raise AssertionError("a hand-written scenario was recorded")

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(EVAL.subprocess, "run", never)
    monkeypatch.setattr(EVAL, "_record_one", never)
    before = {name: (EVAL.SESSIONS / f"{name}.jsonl").read_bytes() for name in HAND}
    assert EVAL.cmd_record(argparse.Namespace(only=list(HAND), model="sonnet", jobs=1)) == 0
    out = capsys.readouterr().out
    assert f"skipped {len(HAND)} written by hand, never recorded: " in out
    assert all(name in out for name in HAND)
    assert not [line for line in out.splitlines() if line.startswith(("recorded", "FAILED"))]
    assert before == {name: (EVAL.SESSIONS / f"{name}.jsonl").read_bytes() for name in HAND}


def test_record_goes_on_with_the_scenarios_that_are_not_by_hand(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    asked = []
    monkeypatch.setattr(EVAL, "_record_one", lambda scenario, model, python: asked.append(scenario["id"]) or scenario["id"])
    only = ["bugfix_clear", "queued_correction", "ho_queued_scope"]
    assert EVAL.cmd_record(argparse.Namespace(only=only, model="sonnet", jobs=1)) == 0
    assert asked == ["bugfix_clear"]
    out = capsys.readouterr().out
    assert "skipped 2 written by hand, never recorded: queued_correction, ho_queued_scope" in out
    assert "recorded bugfix_clear" in out


def test_a_hand_written_scenario_cannot_be_recorded_even_when_asked_directly():
    with pytest.raises(ValueError, match="written by hand"):
        EVAL._record_one(_hand()["claude_wrong"], "sonnet", "python")


def test_the_trimmed_transcript_keeps_a_queued_message_as_its_text_and_mode_only():
    trimmed = EVAL._trim(_queued("no wait, use CSV", source_uuid="s", origin={"kind": "human"}, junk="x"))
    assert trimmed == {
        "type": "attachment", "uuid": "q", "parentUuid": "p", "isSidechain": False, "timestamp": "t",
        "attachment": {"type": "queued_command", "prompt": "no wait, use CSV", "commandMode": "prompt"},
    }
    assert EVAL.HOOK._queued_text(trimmed) == "no wait, use CSV"
    # A message with a picture keeps the words and not the picture.
    blocks = EVAL._trim(_queued([{"type": "text", "text": "and the tests"},
                                 {"type": "image", "source": {"data": "bytes"}}]))
    assert blocks["attachment"]["prompt"] == [{"type": "text", "text": "and the tests"}]
    assert EVAL.HOOK._queued_text(blocks) == "and the tests"


def test_the_trimmed_transcript_drops_every_other_attachment_and_queued_lines_that_are_not_your_message():
    others = [
        {"type": "attachment", "attachment": {"type": "hook_success", "content": "secret"}},
        {"type": "attachment", "attachment": {"type": "todo_reminder", "content": [{"subject": "secret"}]}},
        {"type": "attachment", "attachment": {"type": "queued_command", "prompt": 7, "commandMode": "prompt"}},
        _queued("<task-notification>done</task-notification>"),
        _queued("ls -la", commandMode="bash"),
        _queued("from a task", origin={"kind": "task-notification"}),
        _queued("a meta line", isMeta=True),
        _queued("   "),
    ]
    assert [EVAL._trim(record) for record in others] == [None] * len(others)


@pytest.mark.parametrize("name", HAND)
def test_the_judge_excerpt_of_each_hand_scenarios_last_turn_builds(name):
    job, own = EVAL._job(name)
    turn = " ".join(_hand()[name]["turns"][-1].split())
    assert job["facts"]["judged"] is True
    assert f'The user\'s message: "{turn}"' in job["excerpt"]
    assert "What Claude did: " in job["excerpt"] and "The end of Claude's final reply: " in job["excerpt"]
    assert job["reply"].startswith("msg_")
    # Only the queued sessions show a message typed while Claude worked.
    assert ("more message while Claude worked" in job["excerpt"]) is (name in QUEUED)
    # Nobody tagged these replies, and nothing here is for Claude's own score.
    assert own == {"claude": "", "skill_ran": False}


@pytest.mark.parametrize("name, text", QUEUED.items())
def test_a_message_typed_while_claude_worked_is_in_the_session_and_the_excerpt(name, text):
    queued = [r["attachment"] for r in _session(name) if r["type"] == "attachment"]
    assert queued == [{"type": "queued_command", "prompt": text, "commandMode": "prompt"}]
    assert f'The user sent 1 more message while Claude worked: "{text}".' in _excerpt(name)


def test_a_test_run_through_the_interpreters_full_path_or_the_powershell_tool_counts_as_a_test():
    job = EVAL._job("desk_interpreter_tests")[0]
    assert job["facts"]["tests"] == "targeted"
    assert "Shell commands: `C:/Python311/python.exe -m pytest tests/test_store.py -q`." in job["excerpt"]
    assert "Tests run: chosen tests." in job["excerpt"]

    job = EVAL._job("desk_powershell_tests")[0]
    assert job["facts"]["tests"] == "full"
    assert "tools: PowerShell 2" in job["excerpt"]
    assert "`& C:\\Python311\\python.exe -m pytest -q`" in job["excerpt"]
    assert "Tests run: the whole test suite." in job["excerpt"]


def test_the_powershell_session_reads_back_a_persisted_output():
    records = _session("desk_powershell_tests")
    results = [b for r in records if r["type"] == "user" and isinstance(r["message"]["content"], list)
               for b in r["message"]["content"] if b.get("type") == "tool_result"]
    assert any(b["content"].startswith("<persisted-output>") and b["is_error"] for b in results)
    reads = [b["input"]["file_path"] for r in records if r["type"] == "assistant"
             for b in r["message"]["content"] if b.get("type") == "tool_use" and b["name"] == "Read"]
    assert reads and all("/tool-results/" in path for path in reads)


def test_plan_rounds_and_a_typed_go_ahead_show_in_the_excerpt():
    records = _session("desk_plan_rounds")
    rejections = [b["content"] for r in records if r["type"] == "user" and isinstance(r["message"]["content"], list)
                  for b in r["message"]["content"] if b.get("type") == "tool_result" and b.get("is_error")]
    assert len(rejections) == 2
    assert "the user said:\nAdd a schema version field" in rejections[0] and "the user said:" not in rejections[1]
    # The go-ahead is typed in the plan's own mode, not clicked in the dialog.
    assert records[0]["permissionMode"] == "plan"
    assert [r.get("permissionMode") for r in records if r["type"] == "user" and isinstance(r["message"]["content"], str)] == ["plan", None]

    job = EVAL._job("desk_plan_rounds")[0]
    assert job["facts"]["plan_before"] is True and job["facts"]["plan_now"] is False
    assert 'The plan the user approved, which this work carries out: "1. Add Store.save(path): write {' in job["excerpt"]
    assert "the user approved a plan earlier in this session; 1 round of the user's feedback on plans." in job["excerpt"]
    assert 'The user\'s message: "implement the plan"' in job["excerpt"]


def test_a_plan_approved_and_built_shows_as_followed_for_the_turns_after_it():
    for name in ("plan_build_rename", "plan_build_three_fixes", "ho_post_plan_tweak"):
        job = EVAL._job(name)[0]
        assert job["facts"]["plan_before"] is True, name
        assert "Plan mode: the user approved a plan earlier in this session." in job["excerpt"], name
        assert job["excerpt"].startswith("The plan the user approved, which this work carries out: "), name


def test_the_desktop_replay_block_does_not_put_an_old_reply_last():
    records = _session("desk_replay_block")
    uuids = [r["uuid"] for r in records]
    assert len(set(uuids)) < len(uuids)
    assert records[-1]["message"]["role"] == "assistant"
    assert "Store.clear()" in json.dumps(records[-1])
    job = EVAL._job("desk_replay_block")[0]
    assert "files changed: 1 (README.md)" in job["excerpt"]
    assert "The end of Claude's final reply: \"Fixed the typo in `README.md`.\"" in job["excerpt"]
    assert job["facts"]["docs_only"] is True
    assert job["excerpt"].count("The user's message: ") == 1


def test_a_hook_without_the_replay_filter_still_reads_a_session_as_it_is(monkeypatch):
    real = EVAL.HOOK

    class Older:
        """The hook as ``judge --hook`` may load it from before ``_ordered``."""

        def __getattr__(self, name):
            if name == "_ordered":
                raise AttributeError(name)
            return getattr(real, name)

    expected = EVAL._job("bugfix_clear")
    monkeypatch.setattr(EVAL, "HOOK", Older())
    assert EVAL._job("bugfix_clear") == expected


def test_the_screenshot_request_is_an_image_block_with_no_placeholder():
    content = _session("desk_screenshot")[0]["message"]["content"]
    assert [block["type"] for block in content] == ["image", "text"]
    assert "[image]" not in content[1]["text"]
    assert 'The user\'s message: "[image] This is the stock report' in _excerpt("desk_screenshot")


def test_the_opus_session_starts_a_workflow_that_is_still_working_when_the_turn_ends():
    records = _session("desk_opus_workflow")
    assert {r["message"]["model"] for r in records if r["type"] == "assistant"} == {"claude-opus-5"}
    assert [r["toolUseResult"]["status"] for r in records if isinstance(r.get("toolUseResult"), dict)] == ["async_launched"]
    job = EVAL._job("desk_opus_workflow")[0]
    assert "Workflow 1" in job["excerpt"] and job["facts"]["agent_files"] == 1
    assert job["facts"]["files"] == 0


def test_the_facts_of_the_hand_written_sessions_settle_what_the_judge_cannot_get_wrong():
    def settled(name, words):
        return EVAL.HOOK.grounded(words, EVAL._job(name)[0]["facts"])

    # A rename or a tweak of what was just built, and a correction, are fixes; an approved plan is followed.
    assert settled("plan_build_rename", "shift=build plan=made check=none") == "shift=fix plan=following check=full"
    assert settled("ho_post_plan_tweak", "shift=grew why=left_out") == "shift=fix why=left_out"
    assert settled("plan_build_three_fixes", "shift=build why=missed") == "shift=fix why=missed"
    assert settled("claude_wrong", "shift=grew") == "shift=fix"
    # Three small requests in a row are not corrections.
    assert settled("drip_three_small", "shift=grew why=missed") == "shift=grew"
    # Only a reply that owns a mistake can admit one.
    assert settled("claude_admits", "shift=fix admit=change") == "shift=fix admit=change"
    assert settled("claude_wrong", "shift=fix admit=change") == "shift=fix"
    # A first message has no shift but new and no prior but none.
    assert settled("ho_queued_scope", "shift=build prior=needed") == "prior=none"


def test_judging_a_hand_written_session_asks_for_no_tag_of_claudes_and_scoring_leaves_it_out(tmp_path, monkeypatch):
    def ask(job, config):
        return {"tag": "task=bugfix", "raw": "task=bugfix", "usd": 0.001, "seconds": 1.0, "thinking": 0}

    monkeypatch.setattr(EVAL, "ROOT", tmp_path)
    monkeypatch.setattr(EVAL, "RESULTS", tmp_path / "results")
    monkeypatch.setattr(EVAL, "_ask", ask)
    only = ["bugfix_clear", "claude_wrong"]
    args = argparse.Namespace(configs="haiku", repeats=1, jobs=1, only=only, set=None, hook=None)
    assert EVAL.cmd_judge(args) == 0
    (path,) = (tmp_path / "results").glob("*.json")
    results = json.loads(path.read_text(encoding="utf-8"))
    assert results["scenarios"]["claude_wrong"]["claude"] is None
    assert results["scenarios"]["bugfix_clear"]["claude"]

    scenarios = {s["id"]: s for s in EVAL._scenarios(only)}
    report = EVAL.score(results, scenarios)
    assert "Sessions written by hand (1) hold no tag of Claude's" in report
    own = next(line for line in report.splitlines() if line.startswith("| Claude's own tags"))
    # Scored over bugfix_clear's keys alone: the same figure as without the hand-written session.
    results["scenarios"].pop("claude_wrong")
    alone = EVAL.score(results, {"bugfix_clear": scenarios["bugfix_clear"]})
    assert own in alone.splitlines()

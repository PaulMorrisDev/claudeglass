"""``scripts/eval-tagger.py``: the tag scoring and the transcript trimming,
which spend nothing. Recording and judging call ``claude`` and never run
here.
"""

from __future__ import annotations

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

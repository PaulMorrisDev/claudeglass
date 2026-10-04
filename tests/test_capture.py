"""What metrics capture costs (``capture.py``): prompt cycles and the
subagents they started, what capture cost while it ran (measured from the
notes and tags in the transcripts), and what each level or metric would
cost, replayed from your own history.

Amounts use ``tests/fixtures/pricing_min.toml``'s ``claude-widget-9``
(per million tokens: input 1.0, output 2.0, 5-minute cache write 0.5,
cache read 0.1; fast mode doubles every rate), so each expected value can
be worked out by hand.
"""

from __future__ import annotations

import importlib.util
import json
import random
from dataclasses import replace
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from claudeglass import cache, capture, capture_catalogue as catalogue, capture_tags, parse
from claudeglass.model import CaptureTag, TranscriptMeta, WorkflowRun
from claudeglass.parse import parse_transcript
from claudeglass.pricing import load_pricing, price_turn

from helpers import (
    old_agent_note_text,
    attachment_line,
    system_line,
    tool_result_block,
    tool_use_block,
    turn_line,
    user_block_line,
    user_str_line,
    write_jsonl,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MODEL = "claude-widget-9"
#: USD per character, at 4 characters a token.
OUT = 2.0 / 4 / 1e6
WRITE = 0.5 / 4 / 1e6
READ = 0.1 / 4 / 1e6


@pytest.fixture(autouse=True)
def _salt():
    parse.set_salt(b"c" * 32)


@pytest.fixture()
def pricing():
    return load_pricing(path=FIXTURES / "pricing_min.toml")


def _ts(second: int) -> str:
    return f"2026-09-18T12:{second // 60:02d}:{second % 60:02d}.000Z"


def _ask(second: int, text: str = "do it") -> dict:
    return user_str_line(text, origin={"kind": "human"}, timestamp=_ts(second))


def _reply(second: int, *blocks, text: str = "ok", **kw) -> dict:
    content = list(blocks) or [{"type": "text", "text": text}]
    return turn_line(content=content, model=MODEL, timestamp=_ts(second), **kw)


def _note(second: int, ids, *, hook: str = "SessionStart", agent_type: str = "") -> dict:
    # An agent's note is one from before 0.11.0: older transcripts hold it,
    # and are still priced.
    text = catalogue.note_text(ids, "main") if hook == "SessionStart" else old_agent_note_text(ids, agent_type)
    wrapped = f"<system-reminder>\n{hook} hook additional context: {text}\n</system-reminder>"
    line = attachment_line("hook_additional_context", rendered=wrapped, content=[text], hookName=hook,
                           hookEvent=hook, toolUseID=hook)
    line["timestamp"] = _ts(second)
    return line


def _chars(note_line: dict) -> int:
    return len(note_line["rendered"][0]["content"])


def _parse(tmp_path, name: str, lines, **meta):
    path = tmp_path / name
    write_jsonl(path, lines)
    return parse_transcript(path, TranscriptMeta(path=str(path), **meta))


def _top(tmp_path, lines):
    return _parse(tmp_path, "top.jsonl", lines, kind="top-level")


def _sub(tmp_path, agent: str, lines, *, tool_use_id: str, agent_type: str = "general-purpose", parent: str = ""):
    return _parse(tmp_path, f"agent-{agent}.jsonl", lines, kind="subagent", agent_id=f"agent-{agent}",
                  agent_type=agent_type, tool_use_id=tool_use_id, parent_agent_id=parent or None)


def _corpus(top, *subs):
    return NS(sessions=[NS(top=top, subs=list(subs), session_id="s1")])


# -- prompt cycles ---------------------------------------------------------------


def test_cycles_run_from_each_message_to_the_next_and_collect_nested_agents(tmp_path):
    top = _top(tmp_path, [
        _reply(0, text="left over from a resumed session"),
        _ask(1, "first"),
        _reply(2, tool_use_block("Agent", "toolu_A", {"prompt": "look"})),
        user_block_line([tool_result_block("toolu_A", "found")], timestamp=_ts(8)),
        _reply(9),
        _ask(10, "second"),
        _reply(11, tool_use_block("Agent", "toolu_C", {"prompt": "fix"})),
    ])
    a = _sub(tmp_path, "a1", [
        user_str_line("look", timestamp=_ts(3)),
        _reply(4, tool_use_block("Agent", "toolu_B", {"prompt": "deeper"})),
    ], tool_use_id="toolu_A")
    # A nested agent's parentAgentId is the bare id, without "agent-".
    b = _sub(tmp_path, "b2", [user_str_line("deeper", timestamp=_ts(5)), _reply(6)], tool_use_id="toolu_B",
             parent="a1")
    c = _sub(tmp_path, "c3", [user_str_line("fix", timestamp=_ts(12)), _reply(13)], tool_use_id="toolu_C")
    stray = _sub(tmp_path, "d4", [user_str_line("?", timestamp=_ts(14)), _reply(15)], tool_use_id="toolu_X")

    cycles = capture.prompt_cycles(top, [a, b, c, stray])
    assert [len(cycle.turns) for cycle in cycles] == [2, 1]
    assert [cycle.subs for cycle in cycles] == [[a, b], [c]]


def test_the_last_tag_in_a_cycle_is_the_one_that_counts(tmp_path):
    top = _top(tmp_path, [
        _note(0, ["task"]),
        _ask(1),
        _reply(2, text="Looking.\n[cg: task=debug]"),
        _reply(3, text="Found it.\n[cg: task=bugfix]"),
        _ask(4),
        _reply(5, text="untagged"),
    ])
    first, second = capture.prompt_cycles(top)
    assert first.tag.task == "bugfix"
    assert second.tag is None


def test_a_cycles_tags_merge_key_by_key(tmp_path):
    # CAP-10: a retry's tag winning "task" doesn't erase a key only the
    # earlier tag answered -- they merge key by key, not tag-for-tag.
    top = _top(tmp_path, [
        _note(0, ["task", "level", "shift"]),
        _ask(1),
        _reply(2, text="Looking.\n[cg: task=debug level=hard]"),
        _reply(3, text="Found it.\n[cg: task=bugfix shift=redo]"),
    ])
    [cycle] = capture.prompt_cycles(top)
    turn2, turn3 = top.turns[0], top.turns[1]
    tag = cycle.tag
    assert (tag.task, tag.level, tag.shift) == ("bugfix", "hard", "redo")
    assert tag.has_tl is True
    assert tag.chars == turn2.cap.chars + turn3.cap.chars


# -- what the transcript settles about a cycle's tag ----------------------------------

TAG_IDS = ["task", "level", "size", "shift", "plan", "skill", "prior", "check"]


def _edit(tool_use_id: str, path: str) -> dict:
    return tool_use_block("Edit", tool_use_id, {"file_path": path})


def _ok(second: int, *ids: str) -> dict:
    return user_block_line([tool_result_block(tool_use_id, "ok") for tool_use_id in ids], timestamp=_ts(second))


def test_a_cycles_level_and_size_are_the_highest_any_of_its_tags_gave(tmp_path):
    top = _top(tmp_path, [
        _note(0, TAG_IDS),
        _ask(1),
        _reply(2, text="Looking.\n[cg: task=debug level=hard size=l]"),
        _reply(3, text="Narrowing it.\n[cg: task=debug level=easy size=xs]"),
        _reply(4, text="Fixed.\n[cg: task=bugfix level=normal]"),
    ])
    [cycle] = capture.prompt_cycles(top)
    assert (cycle.tag.level, cycle.tag.size) == ("hard", "l")
    # Every other key still goes to the last tag that answered it.
    assert cycle.tag.task == "bugfix"


def test_claudes_own_tag_outranks_one_haiku_filled_in_for_an_earlier_reply(tmp_path):
    top = _top(tmp_path, [
        _note(0, TAG_IDS),
        _ask(1),
        _reply(2, text="Tests are running.\n[cg: task=test level=hard size=xl check=full]"),
        _reply(3, text="All passed.\n[cg: task=bugfix level=easy size=s]"),
    ])
    [cycle] = capture.prompt_cycles(top)
    # Haiku's fill-in for the reply before the background run finished.
    first = cycle.turns[0]
    first.cap = replace(first.cap, judged=True, chars=0, judge_usd=0.002)
    tag = cycle.tag
    assert (tag.task, tag.level, tag.size, tag.check) == ("bugfix", "easy", "s", None)
    # Its call is still paid for, and the cycle still counts as judged.
    assert tag.judged and tag.judge_usd == pytest.approx(0.002)
    # With no tag of Claude's in the cycle, Haiku's words are the cycle's.
    second = cycle.turns[1]
    second.cap = replace(second.cap, judged=True, chars=0)
    assert (cycle.tag.task, cycle.tag.level, cycle.tag.size) == ("bugfix", "hard", "xl")


@pytest.mark.parametrize("first, second, level, size", [
    ("level=easy size=s", "level=hard size=xl", "hard", "xl"),
    ("level=normal size=m", "size=s", "normal", "m"),
    ("size=l", "level=easy", "easy", "l"),
    ("task=debug", "task=bugfix", None, None),
])
def test_the_highest_word_may_come_from_either_tag_and_none_stays_none(tmp_path, first, second, level, size):
    top = _top(tmp_path, [
        _note(0, TAG_IDS),
        _ask(1),
        _reply(2, text=f"One.\n[cg: {first}]"),
        _reply(3, text=f"Two.\n[cg: {second}]"),
    ])
    [cycle] = capture.prompt_cycles(top)
    assert (cycle.tag.level, cycle.tag.size) == (level, size)


def test_a_cycles_facts_add_up_what_its_replies_did(tmp_path):
    top = _top(tmp_path, [
        _note(0, TAG_IDS),
        _ask(1, "fix it"),
        _reply(2, _edit("e1", "C:/Dev/repo/a.py"), _edit("e2", "C:/Dev/repo/README.md"),
               tool_use_block("Bash", "b1", {"command": "git merge main && pytest tests/test_a.py"}),
               tool_use_block("Bash", "b2", {"command": "echo x > /home/me/out.txt"})),
        user_block_line([
            tool_result_block("e1", "ok"), tool_result_block("e2", "ok"), tool_result_block("b1", "ok"),
            tool_result_block("b2", "Exit code 1", is_error=True),
        ], timestamp=_ts(3)),
        _reply(4, text="Fixed.\n[cg: task=bugfix check=none]"),
    ])
    [cycle] = capture.prompt_cycles(top)
    facts = cycle.facts
    assert (facts["files"], facts["docs_only"]) == (2, False)
    # One command moved files and one wrote a file, and the failed one still did.
    assert (facts["shell_changes"], facts["tests"], facts["tool_errors"]) == (2, "targeted", 1)
    assert (facts["earlier"], facts["plan_now"], facts["plan_before"], facts["skills"]) == (0, False, False, 0)
    assert not (facts["correction"] or facts["adjust"] or facts["same_files"] or facts["admit_candidate"])
    assert facts["agent_files"] == 0


def test_a_whole_suite_outranks_chosen_tests_in_a_cycle(tmp_path):
    top = _top(tmp_path, [
        _note(0, TAG_IDS),
        _ask(1),
        _reply(2, tool_use_block("Bash", "b1", {"command": "pytest -q"}),
               tool_use_block("Bash", "b2", {"command": "pytest tests/test_a.py"})),
        _ok(3, "b1", "b2"),
        _reply(4, text="Done."),
        _ask(5),
        _reply(6, tool_use_block("Bash", "b3", {"command": "pytest tests/test_a.py"})),
        _ok(7, "b3"),
        _reply(8, tool_use_block("Bash", "b4", {"command": "pytest -q"})),
        _ok(9, "b4"),
        _reply(10, text="Done."),
    ])
    first, second = capture.prompt_cycles(top)
    assert (first.facts["tests"], second.facts["tests"]) == ("full", "full")


def test_a_cycle_that_only_edited_documentation_says_so(tmp_path):
    top = _top(tmp_path, [
        _note(0, TAG_IDS),
        _ask(1),
        _reply(2, _edit("e1", "C:/Dev/repo/README.md"), _edit("e2", "C:/Dev/repo/docs/a.rst")),
        _ok(3, "e1", "e2"),
        _reply(4, text="Done."),
        _ask(5),
        _reply(6, _edit("e3", "C:/Dev/repo/README.md"), _edit("e4", "C:/Dev/repo/src/a.py")),
        _ok(7, "e3", "e4"),
        _reply(8, text="Done."),
        _ask(9),
        _reply(10, text="Nothing to change."),
    ])
    facts = [cycle.facts for cycle in capture.prompt_cycles(top)]
    assert [(f["files"], f["docs_only"]) for f in facts] == [(2, True), (2, False), (0, False)]


def test_edits_to_a_claude_folder_are_not_changes_of_yours(tmp_path):
    top = _top(tmp_path, [
        _note(0, TAG_IDS),
        _ask(1),
        _reply(2, _edit("e1", "C:/Users/me/.claude/plans/p.md")),
        _ok(3, "e1"),
        _reply(4, text="Done."),
    ])
    [cycle] = capture.prompt_cycles(top)
    assert (cycle.facts["files"], cycle.facts["docs_only"]) == (0, False)


def test_what_a_subagent_or_a_workflow_agent_changed_counts_for_the_cycle_that_started_it(tmp_path):
    top = _top(tmp_path, [
        _note(0, TAG_IDS),
        _ask(1, "first"),
        _reply(2, tool_use_block("Agent", "toolu_A", {"prompt": "edit"})),
        user_block_line([tool_result_block("toolu_A", "done")], timestamp=_ts(8)),
        _reply(9, text="Done.\n[cg: task=bugfix]"),
        _ask(10, "second"),
        _wf_call(11, "tu_w"),
        _wf_launched(12, "tu_w", "wf_a", "t_a"),
        _reply(13, text="Started.\n[cg: task=ops]"),
        _ask(20, "third"),
        _reply(21, text="Nothing.\n[cg: task=chat]"),
    ])
    sub = _sub(tmp_path, "a1", [
        user_str_line("edit", timestamp=_ts(3)),
        _reply(4, _edit("x1", "C:/Dev/repo/a.py"), _edit("x2", "C:/Dev/repo/b.py"),
               tool_use_block("Bash", "x3", {"command": "git merge main"})),
        _ok(5, "x1", "x2", "x3"),
        _reply(6, text="Edited."),
    ], tool_use_id="toolu_A")
    agent = _wf_agent(tmp_path, "w1", "wf_a", [
        user_str_line("go", timestamp=_ts(14)),
        _reply(15, tool_use_block("Bash", "w1", {"command": "rm -rf build"})),
        _ok(16, "w1"),
        _reply(17, text="Removed."),
    ])
    first, second, third = capture.prompt_cycles(top, [sub, agent])
    assert [c.facts["files"] for c in (first, second, third)] == [0, 0, 0]
    assert [c.facts["agent_files"] for c in (first, second, third)] == [3, 1, 0]


def test_what_came_before_a_message_shows_in_its_facts(tmp_path):
    top = _top(tmp_path, [
        _note(0, TAG_IDS),
        _ask(1, "plan the change"),
        _reply(2, tool_use_block("ExitPlanMode", "tu_p", {"plan": "1. Edit a.py"})),
        _ok(3, "tu_p"),
        _reply(4, _edit("e1", "C:/Dev/repo/a.py")),
        _ok(5, "e1"),
        _reply(6, text="Done.\n[cg: task=feature plan=made]"),
        _ask(10, "that's wrong, the title is missing"),
        _reply(11, _edit("e2", "C:/Dev/repo/a.py")),
        _ok(12, "e2"),
        _reply(13, text="Sorry, I got that wrong.\n[cg: task=bugfix shift=build]"),
        _ask(20, "actually make the title blue instead"),
        _reply(21, _edit("e3", "C:/Dev/repo/a.py")),
        _ok(22, "e3"),
        _reply(23, text="Done.\n[cg: shift=build]"),
        _ask(30, "now something unrelated"),
        _reply(31, _edit("e4", "C:/Dev/repo/b.py")),
        _ok(32, "e4"),
        _reply(33, text="Done.\n[cg: task=chat]"),
    ])
    cycles = capture.prompt_cycles(top)
    facts = [cycle.facts for cycle in cycles]
    assert [f["earlier"] for f in facts] == [0, 1, 2, 3]
    assert [f["plan_now"] for f in facts] == [True, False, False, False]
    assert [f["plan_before"] for f in facts] == [False, True, True, True]
    assert [f["correction"] for f in facts] == [False, True, False, False]
    assert [f["adjust"] for f in facts] == [False, False, True, False]
    assert [f["same_files"] for f in facts] == [False, True, True, False]
    assert [f["admit_candidate"] for f in facts] == [False, True, False, False]


def test_a_plan_you_sent_back_is_not_one_that_was_approved(tmp_path):
    top = _top(tmp_path, [
        _note(0, TAG_IDS),
        _ask(1, "plan it"),
        _reply(2, tool_use_block("ExitPlanMode", "tu_p", {"plan": "1. Edit a.py"})),
        user_block_line([tool_result_block("tu_p", "no", is_error=True)], timestamp=_ts(3),
                        toolDenialKind="user-rejected"),
        _reply(4, text="Rethinking.\n[cg: task=plan plan=made]"),
        _ask(10, "do something else"),
        _reply(11, text="Ok.\n[cg: task=chat]"),
    ])
    first, second = capture.prompt_cycles(top)
    assert (first.facts["plan_now"], second.facts["plan_before"]) == (True, False)


def test_a_transcript_that_starts_inside_a_session_has_no_first_message(tmp_path):
    top = _top(tmp_path, [
        _reply(0, text="left over from a resumed session"),
        _note(1, TAG_IDS),
        _ask(2, "first I can see"),
        _reply(3, text="Done.\n[cg: task=chat]"),
    ])
    [cycle] = capture.prompt_cycles(top)
    assert cycle.facts["earlier"] == 1


def test_the_replies_that_answered_an_agents_report_count_for_the_cycle_that_launched_it(tmp_path):
    top = _top(tmp_path, [
        _tagged(),
        _ask(1, "research this"),
        _background(2, "toolu_A"),
        _launched(3, "toolu_A"),
        _reply(4, text="Launched.\n[cg: task=research]"),
        _ask(10, "something else"),
        _reply(11, text="Done.\n[cg: task=docs]"),
        _report(20, "a1"),
        _reply(21, {"type": "text", "text": "Merged.\n[cg: task=ops]"}, _edit("e1", "C:/Dev/repo/a.py")),
        _ok(22, "e1"),
    ])
    agent = _agent_that_reports(tmp_path, "a1", "toolu_A", 6)
    first, second = capture.prompt_cycles(top, [agent])
    assert first.late_turns and second.handed_off
    assert (first.facts["files"], second.facts["files"]) == (1, 0)


def test_a_cycles_settled_tag_has_the_facts_applied_and_its_tag_stays_as_written(tmp_path):
    top = _top(tmp_path, [
        _note(0, TAG_IDS),
        _ask(1, "document it"),
        _reply(2, _edit("e1", "C:/Dev/repo/README.md")),
        _ok(3, "e1"),
        _reply(4, text="Done.\n[cg: task=feature shift=grew skill=helped check=manual prior=some level=hard]"),
    ])
    [cycle] = capture.prompt_cycles(top)
    raw, settled = cycle.tag, cycle.settled
    assert (raw.task, raw.shift, raw.skill, raw.check, raw.prior) == ("feature", "grew", "helped", "manual", "some")
    assert raw.grounded == ()
    # Only documentation changed, it is the first message, and no skill ran.
    assert (settled.task, settled.shift, settled.skill, settled.check, settled.prior) == (
        "docs", None, "none", "manual", "none"
    )
    assert settled.level == "hard"
    assert settled.grounded == ("task:feature>docs", "shift:grew>", "skill:helped>none", "prior:some>none")


def test_a_cycles_settled_tag_is_its_tag_when_nothing_is_known_or_nothing_changes(tmp_path):
    top = _top(tmp_path, [
        _note(0, TAG_IDS),
        _ask(1),
        _reply(2, tool_use_block("Bash", "b1", {"command": "pytest -q"})),
        _ok(3, "b1"),
        _reply(4, text="Done.\n[cg: task=test check=full]"),
        _ask(5),
        _reply(6, text="Done."),
    ])
    first, second = capture.prompt_cycles(top)
    assert first.settled == first.tag and first.settled.grounded == ()
    assert second.tag is None and second.settled is None
    first.facts = {}
    assert first.settled == first.tag


def test_a_judged_admission_needs_a_reply_that_owns_a_mistake(tmp_path):
    top = _top(tmp_path, [
        _note(0, TAG_IDS),
        _ask(1, "go"),
        _reply(2, text="Done.\n[cg: task=chat]"),
        _ask(3, "go on"),
        _reply(4, text="Sorry, I got that wrong.\n[cg: task=bugfix shift=fix]"),
        _ask(5, "once more"),
        _reply(6, text="Done.\n[cg: task=bugfix shift=fix]"),
    ])
    first, owned, plain = capture.prompt_cycles(top)
    for cycle in (owned, plain):
        turn = cycle.turns[-1]
        turn.cap = replace(turn.cap, admit="claim", judged=True)
    assert owned.settled.admit == "claim" and owned.admit_possible is False
    assert plain.settled.admit is None and plain.admit_possible is False
    assert plain.settled.grounded == ("admit:claim>",)
    # What Claude wrote itself stands, whether or not a pattern finds it.
    plain.turns[-1].cap = replace(plain.turns[-1].cap, judged=False)
    assert plain.settled.admit == "claim"


def test_a_reply_that_owns_a_mistake_nobody_tagged_is_only_possible(tmp_path):
    top = _top(tmp_path, [
        _note(0, TAG_IDS),
        _ask(1, "go"),
        _reply(2, text="Done.\n[cg: task=chat]"),
        _ask(3, "that's wrong"),
        _reply(4, text="Sorry, I got that wrong.\n[cg: task=bugfix shift=fix]"),
        _ask(5, "still wrong"),
        _reply(6, text="My mistake.\n[cg: task=bugfix shift=fix]"),
        _ask(7, "ok"),
        _reply(8, text="Fine.\n[cg: task=chat]"),
    ])
    quiet, possible, claimed, none = capture.prompt_cycles(top)
    claimed.turns[-1].cap = replace(claimed.turns[-1].cap, admit="claim")
    assert [c.admit_possible for c in (quiet, possible, claimed, none)] == [False, True, False, False]
    assert claimed.settled.admit == "claim"
    assert possible.settled.admit is None


# -- the twin rule sets ------------------------------------------------------------------

HOOK_FILE = Path(str(resources.files("claudeglass") / "hooks" / catalogue.HOOK_SCRIPT)).with_name(
    catalogue.HOOK_MODULE
)


def _load_hook():
    spec = importlib.util.spec_from_file_location("_capture_twin_hook_under_test", HOOK_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HOOK = _load_hook()

#: (words, facts, what both rule sets make of them). ``capture_hook.grounded``
#: puts Haiku's words right from the live transcript and
#: ``capture_tags.settle`` Claude's from the parsed one: the hook can't import
#: the package, so the rules are written twice and this table holds them to
#: one another and to what each rule says.
LATER = {"earlier": 2}
GROUNDING = [
    # plan
    ("plan=none", {"plan_now": True}, "plan=made"),
    ("plan=following", {"plan_now": True}, "plan=made"),
    ("plan=made", {"plan_before": True}, "plan=following"),
    ("plan=made", {"plan_now": True, "plan_before": True}, "plan=made"),
    ("plan=deviated", {"plan_before": True}, "plan=deviated"),
    ("plan=made", {"plan_now": False, "plan_before": False, **LATER}, "plan=made"),
    # skill
    ("skill=helped", LATER, "skill=none"),
    ("skill=unneeded", {"skills": 0, **LATER}, "skill=none"),
    ("skill=helped", {"skills": 1, **LATER}, "skill=helped"),
    ("skill=would-help", LATER, "skill=would-help"),
    ("skill=none", LATER, "skill=none"),
    # check: tests that ran, then nothing changed
    ("check=manual", {"tests": "full", "files": 1, **LATER}, "check=full"),
    ("check=none", {"tests": "targeted", **LATER}, "check=targeted"),
    ("check=full", {"tests": "targeted", **LATER}, "check=targeted"),
    ("check=manual", {"files": 0, **LATER}, "check=none"),
    ("check=build", {"tests": "", **LATER}, "check=none"),
    ("check=manual", {"agent_files": 2, **LATER}, "check=manual"),
    ("check=manual", {"shell_changes": 1, **LATER}, "check=manual"),
    ("check=manual", {"files": 3, **LATER}, "check=manual"),
    ("check=none", {"files": 1, **LATER}, "check=none"),
    # task
    ("task=bugfix", {"files": 1, "docs_only": True, **LATER}, "task=docs"),
    ("task=feature", {"files": 1, "docs_only": True, **LATER}, "task=docs"),
    ("task=refactor", {"files": 1, "docs_only": True, **LATER}, "task=docs"),
    ("task=feature", {"files": 1, "docs_only": True, "agent_files": 1, **LATER}, "task=feature"),
    ("task=refactor", {"files": 1, "docs_only": True, "shell_changes": 1, **LATER}, "task=refactor"),
    ("task=debug", {"files": 1, "docs_only": True, **LATER}, "task=debug"),
    ("task=bugfix", {"files": 1, "docs_only": False, **LATER}, "task=bugfix"),
    # a first message
    ("shift=build prior=some", {"earlier": 0}, "prior=none"),
    ("shift=new prior=none", {"earlier": 0}, "shift=new prior=none"),
    ("shift=redo", {"earlier": 0}, ""),
    ("shift=build", {"earlier": 1}, "shift=build"),
    ("prior=needed", {"earlier": 4}, "prior=needed"),
    ("prior=needed", {"plan_now": False}, "prior=needed"),
    # a correction, or an adjustment to the same files
    ("shift=build", {"correction": True, **LATER}, "shift=fix"),
    ("shift=grew", {"adjust": True, "same_files": True, **LATER}, "shift=fix"),
    ("shift=grew", {"adjust": True, **LATER}, "shift=grew"),
    ("shift=grew", {"same_files": True, **LATER}, "shift=grew"),
    ("shift=new", {"correction": True, **LATER}, "shift=new"),
    ("shift=redo", {"correction": True, **LATER}, "shift=redo"),
    # admit
    ("shift=fix admit=claim", {"judged": True, **LATER}, "shift=fix"),
    ("shift=fix admit=claim", {"judged": True, "admit_candidate": True, **LATER}, "shift=fix admit=claim"),
    ("shift=fix admit=claim", {"admit_candidate": False, **LATER}, "shift=fix admit=claim"),
    # why: only behind a redo or a fix, and tools only with a tool error
    ("shift=redo why=left_out", LATER, "shift=redo why=left_out"),
    ("shift=fix why=missed", LATER, "shift=fix why=missed"),
    ("shift=build why=missed", LATER, "shift=build"),
    ("shift=new why=missed", LATER, "shift=new"),
    ("why=missed", LATER, ""),
    ("shift=fix why=tools", LATER, "shift=fix"),
    ("shift=fix why=tools", {"tool_errors": 2, **LATER}, "shift=fix why=tools"),
    ("shift=build why=missed", {"correction": True, **LATER}, "shift=fix why=missed"),
    ("why=missed shift=build", {"correction": True, **LATER}, "why=missed shift=fix"),
    ("shift=fix why=missed", {"earlier": 0}, ""),
    # words nothing settles
    ("level=hard size=xl missing=goal brief=clear", {"earlier": 0}, "level=hard size=xl missing=goal brief=clear"),
    ("task=chat", {"files": 4, "earlier": 0}, "task=chat"),
    # several at once
    (
        "task=bugfix shift=build why=tools admit=claim plan=made skill=helped check=manual prior=some level=hard",
        {"files": 1, "docs_only": True, "correction": True, "judged": True, "earlier": 3, "plan_before": True},
        "task=docs shift=fix plan=following skill=none check=manual prior=some level=hard",
    ),
    # no facts, nothing to put right
    ("task=bugfix shift=build why=missed", None, "task=bugfix shift=build why=missed"),
    ("task=bugfix shift=build why=missed", {}, "task=bugfix shift=build why=missed"),
    ("", {"earlier": 0}, ""),
]


@pytest.mark.parametrize("words, facts, expected", GROUNDING)
def test_the_twin_rule_sets_make_the_same_of_the_same_words_and_facts(words, facts, expected):
    assert HOOK.grounded(words, facts) == expected
    assert capture_tags.settle(words, facts) == expected


@pytest.mark.parametrize("words, facts, expected", GROUNDING)
def test_the_twins_note_the_same_changes(words, facts, expected):
    assert HOOK.grounding_changes(words, expected) == list(capture_tags.grounding_changes(words, expected))


def test_the_twin_rule_sets_agree_on_every_kind_of_input():
    """The same random words (in any order, some left out) and facts through both."""
    rng = random.Random(38)
    keys = list(capture_tags.GROUNDED_KEYS) + ["level", "size", "brief"]
    for _ in range(4000):
        chosen = [key for key in keys if rng.random() < 0.6]
        rng.shuffle(chosen)
        words = " ".join(f"{key}={rng.choice(catalogue.TAG_VOCAB[key])}" for key in chosen)
        facts = {
            "plan_now": rng.random() < 0.3, "plan_before": rng.random() < 0.3, "skills": rng.choice([0, 0, 1, 2]),
            "files": rng.choice([0, 0, 1, 3]), "agent_files": rng.choice([0, 0, 0, 2]),
            "shell_changes": rng.choice([0, 0, 0, 1]), "tests": rng.choice(["", "", "targeted", "full"]),
            "docs_only": rng.random() < 0.4, "earlier": rng.choice([0, 1, 5]), "correction": rng.random() < 0.3,
            "adjust": rng.random() < 0.3, "same_files": rng.random() < 0.5, "tool_errors": rng.choice([0, 0, 2]),
            "admit_candidate": rng.random() < 0.4, "judged": rng.random() < 0.5,
        }
        settled = capture_tags.settle(words, facts)
        assert HOOK.grounded(words, facts) == settled, (words, facts)
        assert HOOK.grounding_changes(words, settled) == list(capture_tags.grounding_changes(words, settled))
        # Settling twice changes nothing more.
        assert capture_tags.settle(settled, facts) == settled, (words, facts)


def test_each_twin_names_the_other():
    assert "capture_tags.settle" in HOOK.grounded.__doc__
    assert "capture_hook.py" in capture_tags.settle.__doc__ and "grounded" in capture_tags.settle.__doc__
    assert "tests/test_capture.py" in HOOK.grounded.__doc__ and "tests/test_capture.py" in capture_tags.settle.__doc__


# -- workflow agents -----------------------------------------------------------------


def _wf_call(second: int, tool_use_id: str) -> dict:
    return _reply(second, tool_use_block("Workflow", tool_use_id, {"script": "return 1"}))


def _wf_launched(second: int, tool_use_id: str, run_id: str, task_id: str) -> dict:
    body = {"status": "async_launched", "taskType": "local_workflow", "runId": run_id, "taskId": task_id}
    return user_block_line(
        [tool_result_block(tool_use_id, "Workflow launched in background.")],
        timestamp=_ts(second),
        toolUseResult=body,
    )


def _wf_agent(tmp_path, name: str, run_id: str, lines, *, agent_type: str = "workflow-subagent",
              kind: str = "workflow-agent", parent: str = ""):
    # Its .meta.json has no toolUseId and no parentAgentId: only the run.
    return _parse(tmp_path, f"wf-{name}.jsonl", lines, kind=kind, agent_id=f"agent-{name}",
                  agent_type=agent_type, workflow_run_id=run_id, parent_agent_id=parent or None)


def _wf_steps(agent_second: int):
    return [user_str_line("go", timestamp=_ts(agent_second)), _reply(agent_second + 1)]


def _two_messages_with_a_workflow_in_the_second(tmp_path):
    return _top(tmp_path, [
        _ask(1, "first"),
        _reply(2),
        _ask(10, "second"),
        _wf_call(11, "tu_w"),
        _wf_launched(12, "tu_w", "wf_a", "t_a"),
        _reply(13),
    ])


def test_a_workflow_agent_joins_the_cycle_whose_reply_started_its_run(tmp_path):
    top = _two_messages_with_a_workflow_in_the_second(tmp_path)
    agent = _wf_agent(tmp_path, "w1", "wf_a", _wf_steps(14))
    first, second = capture.prompt_cycles(top, [agent])
    assert (first.subs, second.subs) == ([], [agent])


def test_a_run_with_no_run_file_still_joins_by_its_run_id(tmp_path):
    top = _two_messages_with_a_workflow_in_the_second(tmp_path)
    agent = _wf_agent(tmp_path, "w1", "wf_a", _wf_steps(14))
    other = WorkflowRun(run_id="wf_unrelated", started=_ts(1))
    for runs in ((), (other,)):
        first, second = capture.prompt_cycles(top, [agent], runs)
        assert (first.subs, second.subs) == ([], [agent])


def test_a_workflow_agent_is_known_by_its_kind_not_its_agent_type(tmp_path):
    top = _two_messages_with_a_workflow_in_the_second(tmp_path)
    named = _wf_agent(tmp_path, "w1", "wf_a", _wf_steps(14), agent_type="Explore")
    # The same run id and the type workflow agents usually carry, but not
    # one of the run's own transcripts.
    impostor = _wf_agent(tmp_path, "w2", "wf_a", _wf_steps(16), kind="subagent")
    first, second = capture.prompt_cycles(top, [named, impostor])
    assert (first.subs, second.subs) == ([], [named])


def test_a_resumed_run_splits_its_agents_across_the_cycles_of_its_calls(tmp_path):
    top = _top(tmp_path, [
        _ask(1, "first"),
        _wf_call(2, "tu_w1"),
        _wf_launched(3, "tu_w1", "wf_a", "t_one"),
        _reply(4),
        _ask(20, "second"),
        _wf_call(21, "tu_w2"),
        _wf_launched(22, "tu_w2", "wf_a", "t_two"),
        _reply(23),
    ])
    early = _wf_agent(tmp_path, "w1", "wf_a", _wf_steps(5))
    # Spoke after the first call and before the resume: still the first's.
    between = _wf_agent(tmp_path, "w2", "wf_a", _wf_steps(19))
    late = _wf_agent(tmp_path, "w3", "wf_a", _wf_steps(25))
    first, second = capture.prompt_cycles(top, [early, between, late])
    assert (first.subs, second.subs) == ([early, between], [late])


def test_a_workflow_agent_that_started_before_every_call_is_left_to_the_run_file(tmp_path):
    top = _two_messages_with_a_workflow_in_the_second(tmp_path)
    early = _wf_agent(tmp_path, "w1", "wf_a", _wf_steps(5))
    assert [c.subs for c in capture.prompt_cycles(top, [early])] == [[], []]
    run = WorkflowRun(run_id="wf_a", started=_ts(11))
    # The reply before the run's start time is the second message's.
    assert [c.subs for c in capture.prompt_cycles(top, [early], [run])] == [[], [early]]


def test_a_run_with_no_logged_call_falls_back_to_the_cycle_its_run_file_started_in(tmp_path):
    top = _top(tmp_path, [_ask(1, "first"), _reply(2), _ask(10, "second"), _reply(11), _reply(13)])
    agent = _wf_agent(tmp_path, "w1", "wf_old", _wf_steps(14))
    run = WorkflowRun(run_id="wf_old", started=_ts(12))
    assert [c.subs for c in capture.prompt_cycles(top, [agent], [run])] == [[], [agent]]
    # No run file, or one that began before the first message: no cycle.
    assert [c.subs for c in capture.prompt_cycles(top, [agent])] == [[], []]
    before = WorkflowRun(run_id="wf_old", started=_ts(0))
    assert [c.subs for c in capture.prompt_cycles(top, [agent], [before])] == [[], []]
    # A run file with no start time says nothing.
    assert [c.subs for c in capture.prompt_cycles(top, [agent], [WorkflowRun(run_id="wf_old")])] == [[], []]


def test_an_agent_a_workflow_agent_spawned_follows_it_to_its_cycle(tmp_path):
    top = _two_messages_with_a_workflow_in_the_second(tmp_path)
    parent = _wf_agent(tmp_path, "w1", "wf_a", _wf_steps(14))
    child = _sub(tmp_path, "c2", _wf_steps(16), tool_use_id="toolu_inner", parent="w1")
    first, second = capture.prompt_cycles(top, [parent, child])
    assert (first.subs, second.subs) == ([], [parent, child])


def test_a_cycle_costs_the_workflow_agents_of_its_run_too(tmp_path, pricing):
    top = _two_messages_with_a_workflow_in_the_second(tmp_path)
    agent = _wf_agent(tmp_path, "w1", "wf_a", _wf_steps(14))
    _first, second = capture.prompt_cycles(top, [agent])
    alone = capture._cycle_cost(capture.prompt_cycles(top)[1], pricing)
    expected = sum(price_turn(t, pricing.resolve_model(t.model)).total for t in capture._priced(agent))
    assert expected > 0
    assert capture._cycle_cost(second, pricing) == pytest.approx(alone + expected)


def test_a_workflow_call_in_a_digest_reaches_the_launch_lookup_as_tuples(tmp_path):
    top = _two_messages_with_a_workflow_in_the_second(tmp_path)
    decoded = cache.result_from_jsonable(json.loads(json.dumps(cache.encode_result(top))))
    agent = _wf_agent(tmp_path, "w1", "wf_a", _wf_steps(14))
    assert capture.WorkflowLaunches(decoded).call_for(agent) == capture.WorkflowLaunches(top).call_for(agent)
    assert capture.WorkflowLaunches(decoded).call_for(agent) is not None


def _workflow_run_on_disk(tmp_path, *, run_file: bool):
    """A project directory as Claude Code writes one for a session that ran
    a workflow: the main transcript, the run's directory of agent
    transcripts (their ``.meta.json`` carries no ``toolUseId``), and, when
    ``run_file``, the ``workflows/wf_*.json`` run summary."""
    project = tmp_path / "proj"
    run_dir = project / "session-wf" / "subagents" / "workflows" / "wf_a"
    run_dir.mkdir(parents=True)
    write_jsonl(project / "session-wf.jsonl", [
        _ask(1, "first"),
        _reply(2),
        _ask(10, "second"),
        _wf_call(11, "tu_w"),
        _wf_launched(12, "tu_w", "wf_a", "t_a"),
        _reply(13),
    ])
    write_jsonl(run_dir / "agent-w1.jsonl", _wf_steps(14))
    (run_dir / "agent-w1.meta.json").write_text(json.dumps({"agentType": "workflow-subagent"}), encoding="utf-8")
    if run_file:
        (project / "session-wf" / "workflows").mkdir()
        (project / "session-wf" / "workflows" / "wf_a.json").write_text(
            json.dumps({"runId": "wf_a", "taskId": "t_a", "agentCount": 1, "phases": []}), encoding="utf-8"
        )
    return project


@pytest.mark.parametrize("run_file", [False, True])
def test_a_workflow_run_on_disk_joins_its_agents_to_the_cycle_whether_or_not_it_has_a_run_file(tmp_path, run_file):
    from claudeglass.corpus import load_corpus

    (bundle,) = load_corpus([_workflow_run_on_disk(tmp_path, run_file=run_file)]).sessions
    assert len(bundle.workflows) == int(run_file)
    (agent,) = bundle.subs
    assert (agent.meta.kind, agent.meta.workflow_run_id, agent.meta.tool_use_id) == ("workflow-agent", "wf_a", None)
    first, second = capture.prompt_cycles(bundle.top, bundle.subs, bundle.workflows)
    assert (first.subs, second.subs) == ([], [agent])


# -- tags written in reply to an agent's report --------------------------------------


def _tagged() -> dict:
    return _note(0, ["task", "brief", "level", "size"])


def _report(second: int, task_id: str) -> dict:
    text = f"<task-notification><task-id>{task_id}</task-id><status>completed</status><result>found it</result></task-notification>"
    return user_str_line(text, origin={"kind": "task-notification"}, timestamp=_ts(second))


def _background(second: int, tool_use_id: str, text: str = "Launched.") -> dict:
    return _reply(second, {"type": "text", "text": text},
                  tool_use_block("Agent", tool_use_id, {"prompt": "look", "run_in_background": True}))


def _launched(second: int, tool_use_id: str) -> dict:
    return user_block_line([tool_result_block(tool_use_id, "Async agent launched")], timestamp=_ts(second),
                           toolUseResult={"status": "async_launched", "agentId": "a1"})


def _agent_that_reports(tmp_path, agent: str, tool_use_id: str, second: int):
    return _sub(tmp_path, agent, _wf_steps(second), tool_use_id=tool_use_id)


def test_a_tag_in_reply_to_a_background_agents_report_counts_for_the_cycle_that_launched_it(tmp_path):
    top = _top(tmp_path, [
        _tagged(),
        _ask(1, "research this"),
        _background(2, "toolu_A"),
        _launched(3, "toolu_A"),
        _reply(4, text="Launched.\n[cg: task=research brief=clear]"),
        _ask(10, "something else"),
        _reply(11, text="Done.\n[cg: task=docs]"),
        _report(20, "a1"),
        _reply(21, text="It found the cause.\n[cg: task=research level=hard size=m]"),
    ])
    agent = _agent_that_reports(tmp_path, "a1", "toolu_A", 6)
    first, second = capture.prompt_cycles(top, [agent])
    assert (first.tag.task, first.tag.brief, first.tag.level, first.tag.size) == ("research", "clear", "hard", "m")
    # The second message keeps the tag it wrote itself and not the report's.
    assert (second.tag.task, second.tag.level, second.tag.size) == ("docs", None, None)
    # The turns, and so the cost, stay where they ran.
    assert (len(first.turns), len(second.turns)) == (2, 2)
    assert first.late_turns == [second.turns[1]] and second.handed_off == {1}


def test_a_cycle_that_only_answered_a_report_has_no_tag_of_its_own(tmp_path):
    top = _top(tmp_path, [
        _tagged(),
        _ask(1, "research this"),
        _background(2, "toolu_A", text="Launched.\n[cg: task=research]"),
        _ask(10, "thanks"),
        _reply(11, text="Welcome."),
        _report(20, "a1"),
        _reply(21, text="Found it.\n[cg: task=research size=m]"),
    ])
    agent = _agent_that_reports(tmp_path, "a1", "toolu_A", 6)
    first, second = capture.prompt_cycles(top, [agent])
    assert first.tag.size == "m" and second.tag is None


def test_a_workflows_report_hands_its_reply_to_the_call_that_launched_it(tmp_path):
    top = _top(tmp_path, [
        _tagged(),
        _ask(1, "run it"),
        _wf_call(2, "tu_w"),
        _wf_launched(3, "tu_w", "wf_a", "t_a"),
        _reply(4, text="Running.\n[cg: task=research]"),
        _ask(10, "meanwhile"),
        _reply(11, text="Sure.\n[cg: task=docs]"),
        _report(20, "t_a"),
        _reply(21, text="It finished.\n[cg: task=research size=l]"),
    ])
    first, second = capture.prompt_cycles(top)
    assert first.tag.size == "l" and second.tag.size is None and second.tag.task == "docs"


def test_a_resumed_runs_report_goes_to_the_call_that_resumed_it(tmp_path):
    top = _top(tmp_path, [
        _tagged(),
        _ask(1, "run it"),
        _wf_call(2, "tu_w1"),
        _wf_launched(3, "tu_w1", "wf_a", "t_one"),
        _reply(4, text="Running.\n[cg: task=research]"),
        _ask(10, "again"),
        _wf_call(11, "tu_w2"),
        _wf_launched(12, "tu_w2", "wf_a", "t_two"),
        _reply(13, text="Resumed.\n[cg: task=research]"),
        _ask(20, "wait"),
        _reply(21, text="Waiting.\n[cg: task=chat]"),
        _report(30, "t_two"),
        _reply(31, text="Done.\n[cg: task=research size=m]"),
    ])
    first, second, third = capture.prompt_cycles(top)
    assert (first.tag.size, second.tag.size, third.tag.size) == (None, "m", None)


def test_a_message_of_yours_between_the_report_and_the_reply_hands_nothing_off(tmp_path):
    top = _top(tmp_path, [
        _tagged(),
        _ask(1, "research this"),
        _background(2, "toolu_A", text="Launched.\n[cg: task=research]"),
        _ask(10, "something else"),
        _reply(11, text="Done.\n[cg: task=docs]"),
        _report(20, "a1"),
        _ask(21, "and the agent?"),
        _reply(22, text="Here.\n[cg: task=research size=m]"),
    ])
    agent = _agent_that_reports(tmp_path, "a1", "toolu_A", 6)
    first, second, third = capture.prompt_cycles(top, [agent])
    assert first.tag.size is None and third.tag.size == "m"
    assert first.late_turns == [] and not (first.handed_off | second.handed_off | third.handed_off)


def test_a_report_answered_in_the_cycle_that_launched_the_agent_hands_nothing_off(tmp_path):
    top = _top(tmp_path, [
        _tagged(),
        _ask(1, "research this"),
        _background(2, "toolu_A", text="Launched.\n[cg: task=research]"),
        _report(8, "a1"),
        _reply(9, text="Found it.\n[cg: task=research size=m]"),
        _ask(10, "next"),
        _reply(11, text="Ok.\n[cg: task=docs]"),
    ])
    agent = _agent_that_reports(tmp_path, "a1", "toolu_A", 6)
    first, second = capture.prompt_cycles(top, [agent])
    assert first.late_turns == [] and first.handed_off == set() and second.handed_off == set()
    assert (first.tag.size, second.tag.size) == ("m", None)


def test_a_report_for_an_agent_nobody_launched_hands_nothing_off(tmp_path):
    top = _top(tmp_path, [
        _tagged(),
        _ask(1, "one"),
        _reply(2, text="Ok.\n[cg: task=chat]"),
        _ask(10, "two"),
        _reply(11, text="Ok.\n[cg: task=docs]"),
        _report(20, "zz9"),
        _reply(21, text="A report.\n[cg: task=research]"),
    ])
    first, second = capture.prompt_cycles(top)
    assert (first.tag.task, second.tag.task) == ("chat", "research")


def test_a_reply_to_two_reports_goes_to_the_cycle_of_the_first(tmp_path):
    top = _top(tmp_path, [
        _tagged(),
        _ask(1, "one"),
        _background(2, "toolu_A", text="Launched.\n[cg: task=research]"),
        _ask(10, "two"),
        _background(11, "toolu_B", text="Launched.\n[cg: task=research]"),
        _ask(20, "three"),
        _reply(21, text="Waiting.\n[cg: task=chat]"),
        _report(30, "a1"),
        _report(31, "b2"),
        _reply(32, text="Both done.\n[cg: task=research size=m]"),
    ])
    a = _agent_that_reports(tmp_path, "a1", "toolu_A", 6)
    b = _agent_that_reports(tmp_path, "b2", "toolu_B", 15)
    first, second, third = capture.prompt_cycles(top, [a, b])
    assert (first.tag.size, second.tag.size, third.tag.size) == ("m", None, None)


def test_a_reply_whose_first_report_is_its_own_cycles_keeps_its_tag(tmp_path):
    top = _top(tmp_path, [
        _tagged(),
        _ask(1, "one"),
        _background(2, "toolu_A", text="Launched.\n[cg: task=research]"),
        _ask(10, "two"),
        _background(11, "toolu_B", text="Launched.\n[cg: task=research]"),
        _report(30, "b2"),
        _report(31, "a1"),
        _reply(32, text="Both done.\n[cg: task=research size=m]"),
    ])
    a = _agent_that_reports(tmp_path, "a1", "toolu_A", 6)
    b = _agent_that_reports(tmp_path, "b2", "toolu_B", 15)
    first, second = capture.prompt_cycles(top, [a, b])
    assert (first.tag.size, second.tag.size) == (None, "m")


def test_coverage_counts_a_cycle_tagged_only_by_a_late_reply(tmp_path, pricing):
    top = _top(tmp_path, [
        _tagged(),
        _ask(1, "research this"),
        _background(2, "toolu_A"),
        _ask(10, "something else"),
        _reply(11, text="Done."),
        _report(20, "a1"),
        _reply(21, text="Found it.\n[cg: task=research]"),
    ])
    agent = _agent_that_reports(tmp_path, "a1", "toolu_A", 6)
    use = capture.usage(_corpus(top, agent), pricing)
    assert (use.cycles, use.tagged_cycles) == (2, 1)


# -- measured use ------------------------------------------------------------------


def _captured_session(tmp_path, *, speed=None):
    """A captured session: an Essentials note, a tagged reply that starts
    an agent, an untagged reply, a compaction, and one more message."""
    ids = catalogue.level_metrics("essentials")
    note = _note(0, ids)
    top = _top(tmp_path, [
        note,
        _ask(1, "hi"),
        _reply(2, tool_use_block("Agent", "toolu_A", {"prompt": "[spawn: isolate] check it"}),
               {"type": "text", "text": "Started.\n[cg: task=bugfix brief=clear]"}, speed=speed),
        user_block_line([tool_result_block("toolu_A", "Done.")], timestamp=_ts(6)),
        _ask(7, "more"),
        _reply(8),
        system_line("compact_boundary", timestamp=_ts(9)),
        _ask(10, "again"),
        _reply(11),
    ])
    sub_note = _note(3, ids, hook="SubagentStart", agent_type="general-purpose")
    sub = _sub(tmp_path, "a1", [
        sub_note,
        user_str_line("[spawn: isolate] check it", timestamp=_ts(3)),
        _reply(4, text="Checked.\n[result: done]"),
    ], tool_use_id="toolu_A")
    return top, sub, _chars(note), _chars(sub_note)


def test_usage_prices_notes_until_the_compaction_and_tags_at_the_writer_rate(tmp_path, pricing):
    top, sub, note_chars, sub_note_chars = _captured_session(tmp_path)
    use = capture.usage(_corpus(top, sub), pricing)

    # The session note is written with the first turn, read by the next,
    # and dropped by the compaction before the third.
    main = use.scopes["main"]
    assert main.note_cost == pytest.approx(note_chars * (WRITE + READ))
    assert main.note_tokens == round(note_chars / 4)
    tag = len("[cg: task=bugfix brief=clear]") + 1
    # CAP-2: the tag is Claude's own output on turn 1 (OUT), then sits in
    # context and is cache-written once more into turn 2's prompt (WRITE);
    # the compaction before turn 3 drops it before it is ever read back.
    assert main.tag_cost == pytest.approx(tag * (OUT + WRITE))
    # The agent's note is written once; its report tag is output.
    agent = use.scopes["subagent"]
    assert agent.note_cost == pytest.approx(sub_note_chars * WRITE)
    assert agent.tag_cost == pytest.approx((len("[result: done]") + 1) * OUT)
    # The spawn word opens the brief, so the main session wrote it.
    assert use.scopes["brief"].tag_cost == pytest.approx((len("[spawn: isolate]") + 1) * OUT)

    assert (use.sessions, use.subagents, use.notes) == (1, 1, 2)
    assert (use.cycles, use.tagged_cycles, use.reports, use.tagged_reports) == (3, 1, 1, 1)
    assert use.coverage == pytest.approx(100 / 3)
    assert use.report_coverage == 100.0
    assert use.cost == pytest.approx(sum(s.cost for s in use.scopes.values()))
    assert sum(use.by_metric.values()) == pytest.approx(use.cost)
    assert sum(use.daily.values()) == pytest.approx(use.cost)
    assert list(use.daily) == ["2026-09-18"]
    assert 0 < use.share < 100
    assert {k: use.answers[k] for k in ("task", "brief", "result", "spawn")} == {
        "task": 1, "brief": 1, "result": 1, "spawn": 1}
    assert "level" not in use.answers


def test_a_note_after_a_compact_boundary_is_priced_and_counted_separately(tmp_path, pricing):
    """SURV-3: a note whose own turn lands at or after a real
    compact_boundary is tallied under after_compact_notes/
    after_compact_cost in addition to its ordinary scope -- the carried
    prefix a compaction would otherwise have discounted it against is
    gone by then."""
    before = _note(0, ["task"])
    after = _note(4, ["task"])
    top = _top(tmp_path, [
        before,
        _ask(1),
        _reply(2, text="Looking.\n[cg: task=debug]"),
        system_line("compact_boundary", timestamp=_ts(3)),
        after,
        _ask(5),
        _reply(6, text="Done.\n[cg: task=bugfix]"),
    ])
    use = capture.usage(_corpus(top), pricing)
    assert use.notes == 2
    assert use.after_compact_notes == 1
    assert use.after_compact_cost == pytest.approx(_chars(after) * WRITE)
    # Both notes individually cost the same write-only amount here, so the
    # scope total (both notes) is strictly more than the after-compact
    # share (one of them).
    assert use.after_compact_cost < use.scopes["main"].note_cost


def test_no_after_compact_notes_without_a_real_compaction(tmp_path, pricing):
    top, sub, _, _ = _captured_session(tmp_path)
    use = capture.usage(_corpus(top, sub), pricing)
    # _captured_session's only note (t=0) precedes its one compaction, so
    # nothing here lands after a boundary.
    assert use.after_compact_notes == 0
    assert use.after_compact_cost == 0.0


def test_fast_mode_doubles_what_the_fast_turn_wrote_and_carried(tmp_path, pricing):
    top, sub, note_chars, _ = _captured_session(tmp_path, speed="fast")
    use = capture.usage(_corpus(top, sub), pricing)
    # The first turn ran fast: its cache write, its tag and the spawn word
    # it wrote cost double; the next turn's read does not.
    assert use.scopes["main"].note_cost == pytest.approx(note_chars * (2 * WRITE + READ))
    # The tag's own output doubles (turn 1 ran fast), but turn 2 -- the
    # turn that carries it forward into its prompt -- did not, so that
    # carry-write portion stays at the normal rate.
    assert use.scopes["main"].tag_cost == pytest.approx((len("[cg: task=bugfix brief=clear]") + 1) * (2 * OUT + WRITE))
    assert use.scopes["brief"].tag_cost == pytest.approx((len("[spawn: isolate]") + 1) * 2 * OUT)


def test_usage_since_leaves_out_what_came_before(tmp_path, pricing):
    top, sub, _, _ = _captured_session(tmp_path)
    use = capture.usage(_corpus(top, sub), pricing, since="2026-09-18T12:00:07Z")
    assert (use.notes, use.cycles, use.tagged_cycles, use.reports) == (0, 2, 0, 0)
    assert use.cost == 0.0
    assert use.spend > 0


def test_sessions_without_a_capture_note_are_not_counted(tmp_path, pricing):
    top = _top(tmp_path, [_ask(0), _reply(1, text="Done.\n[cg: task=bugfix]")])
    use = capture.usage(_corpus(top), pricing)
    assert (use.sessions, use.cycles, use.cost, use.coverage, use.share) == (0, 0, 0.0, None, None)


def test_a_feedback_run_cycle_is_left_out_of_the_coverage_denominator(tmp_path, pricing):
    # CAP-10: a /cg-feedback run answers /cg-feedback's own question, not
    # the one an ordinary reply reports on -- it shouldn't count against
    # coverage just because it never wrote a [cg: ...] task tag either.
    top = _top(tmp_path, [
        _note(0, ["task"]),
        user_str_line(
            "<command-message>cg-feedback</command-message>\n<command-name>/cg-feedback</command-name>",
            timestamp=_ts(1),
        ),
        _reply(2, text="Thanks: ClaudeGlass will use this for your savings tips."),
        _ask(3),
        _reply(4, text="Done.\n[cg: task=bugfix]"),
    ])
    use = capture.usage(_corpus(top), pricing)
    assert (use.cycles, use.tagged_cycles) == (1, 1)
    assert use.coverage == 100.0


def test_a_max_tokens_cycle_is_left_out_of_the_coverage_denominator(tmp_path, pricing):
    # CAP-10: a reply cut off by max_tokens never got to write its tag.
    cut_off = _reply(2, text="Still working")
    cut_off["message"]["stop_reason"] = "max_tokens"
    top = _top(tmp_path, [
        _note(0, ["task"]),
        _ask(1),
        cut_off,
        _ask(3),
        _reply(4, text="Done.\n[cg: task=bugfix]"),
    ])
    use = capture.usage(_corpus(top), pricing)
    assert (use.cycles, use.tagged_cycles) == (1, 1)
    assert use.coverage == 100.0


def test_an_interrupted_cycle_is_left_out_of_the_coverage_denominator(tmp_path, pricing):
    # CAP-10: the user cut Claude off before it could write its tag.
    top = _top(tmp_path, [
        _note(0, ["task"]),
        _ask(1),
        _reply(2, text="cut off mid-thought"),
        user_str_line("[Request interrupted by user]", timestamp=_ts(3)),
        _ask(4, "try again"),
        _reply(5, text="Done.\n[cg: task=bugfix]"),
    ])
    use = capture.usage(_corpus(top), pricing)
    assert (use.cycles, use.tagged_cycles) == (1, 1)
    assert use.coverage == 100.0


def test_a_tool_use_interrupt_cycle_is_left_out_of_the_coverage_denominator_too(tmp_path, pricing):
    # The "for tool use" line is an interrupt of the same kind, now with a
    # subkind: a call you turned down cut the cycle off before the tag.
    top = _top(tmp_path, [
        _note(0, ["task"]),
        _ask(1),
        _reply(2, tool_use_block("Bash", "toolu_d", {"command": "make"})),
        user_block_line([tool_result_block("toolu_d", "Permission to use Bash has been denied.", is_error=True)],
                        timestamp=_ts(3), toolDenialKind="permission-rule"),
        user_str_line("[Request interrupted by user for tool use]", timestamp=_ts(3)),
        _ask(4, "try again"),
        _reply(5, text="Done.\n[cg: task=bugfix]"),
    ])
    use = capture.usage(_corpus(top), pricing)
    assert (use.cycles, use.tagged_cycles) == (1, 1)
    assert use.coverage == 100.0


def test_a_tool_note_counts_in_the_tool_scope(tmp_path, pricing):
    ids = catalogue.level_metrics("deep")
    text = catalogue.tool_note_text("big_output")
    tool_note = attachment_line("hook_additional_context", rendered=f"<system-reminder>\nPostToolUse hook "
                                f"additional context: {text}\n</system-reminder>", content=[text],
                                hookName="PostToolUse", hookEvent="PostToolUse", toolUseID="toolu_1")
    tool_note["timestamp"] = _ts(3)
    top = _top(tmp_path, [
        _note(0, ids),
        _ask(1),
        _reply(2, tool_use_block("Bash", "toolu_1", {"command": "make"})),
        user_block_line([tool_result_block("toolu_1", "x" * 40_000)], timestamp=_ts(3)),
        tool_note,
        _reply(4, text="Built.\n[cg: task=ops out=part]"),
    ])
    use = capture.usage(_corpus(top), pricing)
    assert use.scopes["tool"].note_cost == pytest.approx(_chars(tool_note) * WRITE)
    assert use.by_metric["big_output"] > 0
    assert use.answers["big_output"] == 1


# -- estimates from your own history ------------------------------------------------


def _history_corpus(tmp_path):
    top = _top(tmp_path, [
        _ask(0),
        _reply(1, tool_use_block("Agent", "toolu_A", {"prompt": "a"}), tool_use_block("Agent", "toolu_E", {"prompt": "e"})),
        user_block_line([tool_result_block("toolu_A", "a"), tool_result_block("toolu_E", "e")], timestamp=_ts(6)),
        _reply(7, tool_use_block("Read", "toolu_B", {"file_path": "/w/big.log"})),
        user_block_line([tool_result_block("toolu_B", "x" * 40_000)], timestamp=_ts(8)),
        _reply(9),
        _ask(10),
        _reply(11),
    ])
    general = _sub(tmp_path, "a1", [user_str_line("a", timestamp=_ts(2)), _reply(3), _reply(4)], tool_use_id="toolu_A")
    explore = _sub(tmp_path, "e1", [user_str_line("e", timestamp=_ts(2)), _reply(3)], tool_use_id="toolu_E",
                   agent_type="Explore")
    setup = _sub(tmp_path, "s1", [user_str_line("s", timestamp=_ts(2)), _reply(3)], tool_use_id="toolu_S",
                 agent_type="statusline-setup")
    return _corpus(top, general, explore, setup)


def test_history_prices_one_character_in_each_place(tmp_path, pricing):
    past = capture.history(_history_corpus(tmp_path), pricing, days=7)
    assert (past.days, past.sessions, past.cycles, past.subagents) == (7, 1, 2, 2)
    assert (past.main_notes, past.sub_notes) == (1, 2)
    # Main: written with the first of 4 turns, read by the other 3.
    assert past.main_note == pytest.approx(WRITE + 3 * READ)
    assert past.sub_note == pytest.approx(WRITE + READ)
    assert past.sub_note_no_rules == pytest.approx(WRITE)
    # CAP-2: each cycle's reply tag prices its own output plus, when a
    # later turn follows it, one cache-write carry into that turn. The
    # first cycle's tag (turn 3 of 4) carries into the 4th; the second
    # cycle's tag is the corpus's last turn, so it has nothing to carry
    # into.
    assert past.reply_tag == pytest.approx(2 * OUT + WRITE)
    # Both subagents' report tags are their transcript's own last turn,
    # so neither has a later turn to carry into -- unchanged from output
    # cost alone.
    assert past.report_tag == pytest.approx(2 * OUT)
    # The brief marker isn't a [cg:]/[result:] tag -- it stays priced at
    # output cost alone here too, same as the real usage() path's own
    # _add_brief_markers.
    assert past.brief_tag == pytest.approx(2 * OUT)
    # The 40k-character result arrives with the third turn, carried to the end.
    assert past.big_outputs == 1
    assert past.big_output_note == pytest.approx(WRITE + READ)
    # CAP-2: the big-output tag is written with the 3rd (of 4) turns and
    # carried into the 4th.
    assert past.big_output_tag == pytest.approx(OUT + WRITE)
    assert past.spend > 0


def test_big_output_notes_are_estimated_per_call_not_per_turn(tmp_path, pricing):
    # CAP-10: two big results from the same tool in one turn cost two
    # notes, not one -- Claude Code fires PostToolUse per call, not once
    # per turn regardless of how many of its calls crossed the threshold.
    big = "x" * 35_000
    top = _top(tmp_path, [
        _ask(0),
        _reply(1, tool_use_block("Read", "toolu_B", {"file_path": "/w/a.log"}),
               tool_use_block("Read", "toolu_C", {"file_path": "/w/b.log"})),
        user_block_line([tool_result_block("toolu_B", big), tool_result_block("toolu_C", big)], timestamp=_ts(2)),
        _reply(3),
    ])
    past = capture.history(_corpus(top), pricing, days=7)
    assert past.big_outputs == 2


def test_a_big_shell_or_mcp_result_is_not_estimated_a_note(tmp_path, pricing):
    # The hook isn't run after the shell and MCP tools, so a big result from
    # one of them never costs a note.
    big = "x" * 35_000
    top = _top(tmp_path, [
        _ask(0),
        _reply(1, tool_use_block("Bash", "toolu_B", {"command": "make"}),
               tool_use_block("mcp__docs__search", "toolu_C", {"query": "q"})),
        user_block_line([tool_result_block("toolu_B", big), tool_result_block("toolu_C", big)], timestamp=_ts(2)),
        _reply(3),
    ])
    assert capture.history(_corpus(top), pricing, days=7).big_outputs == 0


def test_estimates_rise_with_the_level_and_scale_with_sampling(tmp_path, pricing):
    past = capture.history(_history_corpus(tmp_path), pricing, days=7)
    levels = capture.level_estimates(past)
    assert list(levels) == ["free", "essentials", "standard", "deep"]
    assert levels["free"].cost == 0.0 and levels["free"].note_tokens == 0
    assert 0 < levels["essentials"].cost < levels["standard"].cost < levels["deep"].cost
    half = capture.level_estimates(past, sample=50)["standard"]
    assert half.cost == pytest.approx(levels["standard"].cost / 2)
    assert levels["standard"].per_week == pytest.approx(levels["standard"].cost)
    assert levels["standard"].share == pytest.approx(100 * levels["standard"].cost / past.spend)
    assert capture.estimate(past, ()).cost == 0.0


def test_an_estimate_is_the_note_and_tag_sizes_times_the_unit_costs(tmp_path, pricing):
    past = capture.history(_history_corpus(tmp_path), pricing, days=7)
    ids = ("task",)
    main = len(catalogue.note_text(ids, "main")) + catalogue.NOTE_WRAP_CHARS + len("SessionStart")
    reply = catalogue.METRICS_BY_ID["task"].out_chars + 7
    assert capture.estimate(past, ids).cost == pytest.approx(main * past.main_note + reply * past.reply_tag)


def test_metric_estimates_price_what_each_metric_adds(tmp_path, pricing):
    past = capture.history(_history_corpus(tmp_path), pricing, days=7)
    ids = catalogue.level_metrics("standard")
    parts = capture.metric_estimates(past, ids)
    assert set(parts) == set(ids)
    assert parts["session_end"] == 0.0
    assert parts["task"] > 0
    # Agent runs cost a Haiku call each, whichever agent metrics are on:
    # leaving out result leaves out the rest that need it, and the call;
    # leaving out one of the rest saves nothing.
    needs_result = [i for i in ids if "result" in catalogue.METRICS_BY_ID[i].requires]
    assert needs_result and all(parts[i] == 0.0 for i in needs_result)
    assert past.subagents and parts["result"] == pytest.approx(past.subagents * catalogue.JUDGE_USD_PER_CALL)


def test_why_and_admit_are_paid_for_with_shift_and_counted_once():
    """They ride on the shift switch, so a tag answering all three costs the
    one metric; a retired found, with no catalogue entry, weighs nothing."""
    shift = catalogue.METRICS_BY_ID["shift"]
    tag = CaptureTag(task="bugfix", shift="redo", why="left_out", admit="claim", found="yes")
    weights = capture._tag_weights(NS(cap=tag, result_marker=None), False)
    assert weights == {"task": catalogue.METRICS_BY_ID["task"].out_chars, "shift": shift.out_chars}
    # The note's share for shift is the length of all three of its lines.
    note = capture._note_weights(["shift"], "main")
    assert note == {"shift": len(shift.main_line)} and shift.main_line.count("\n") == 2


def test_enough_data_counts_answers_against_each_target():
    use = capture.CaptureUsage(answers={"task": 12})
    assert capture.enough_data(use, "task") == (12, capture.ENOUGH["main"])
    assert capture.enough_data(use, "agent_brief") == (0, capture.ENOUGH["subagent"])
    # Judged per agent run, as the rest of an agent's words are.
    assert capture.enough_data(use, "retry") == (0, capture.ENOUGH["subagent"])
    assert capture.enough_data(use, "big_output") == (0, capture.ENOUGH["tool"])
    assert capture.enough_data(use, "waits", signal_sessions=5) == (5, capture.ENOUGH["signal"])
    assert capture.enough_target("no-such-metric") == 0


def _message_note_chars(hint: str) -> int:
    """What a feedback note adds to the context: its text and the wrapper around a hook note on a message."""
    return len(catalogue.FEEDBACK_NOTE_TEXT[hint]) + catalogue.NOTE_WRAP_CHARS + len("UserPromptSubmit")


def test_estimate_prices_the_rating_reminder_at_most_once_per_session_and_once_in_the_rest_period():
    past = capture.History(days=14, sessions=2, cycles=10, subagents=3, main_notes=2, main_note=1e-6, reply_tag=2e-6,
                           brief_tag=5e-6)
    est = capture.estimate(past, ("feedback_reminder",))
    note = _message_note_chars("rating_reminder")
    out = catalogue.METRICS_BY_ID["feedback_reminder"].out_chars
    # 2 sessions in 14 days (the 3-day rest allows 4): 2 reminders at most. Each is a note, carried at the
    # average price of a note (1e-6 over 2 notes), and a line at the average price of a reply's words.
    assert est.cost == pytest.approx(2 * (note * 1e-6 / 2 + out * 2e-6 / 10))
    assert est.tag_tokens == round(out * 2 / 4) and est.note_tokens == round(note * 2 / 4)
    # Nothing at the session start: it comes on a message of yours.
    assert catalogue.note_text(("feedback_reminder",), "main") == ""
    rough = catalogue.rough_tokens(("task", "feedback_reminder"))
    assert rough["reminder"] == round(out / 4) and rough["reply_tag"] == round((13 + 6) / 4)
    assert rough["message_note"] == round(note / 4)
    # Many short sessions are held to the rest period; none means none.
    busy = capture.History(days=9, sessions=30, cycles=300, main_notes=30, main_note=3e-5, reply_tag=6e-5)
    assert capture.estimate(busy, ("feedback_reminder",)).cost == pytest.approx(3 * (note * 1e-6 + out * 2e-7))
    assert capture.estimate(capture.History(days=14), ("feedback_reminder",)).cost == 0


def test_estimate_prices_the_plan_check_once_per_approved_plan():
    past = capture.History(days=14, sessions=5, cycles=50, main_notes=5, main_note=5e-6, reply_tag=1e-5,
                           plans_approved=3)
    est = capture.estimate(past, ("plan_check",))
    note = _message_note_chars("plan_check")
    out = catalogue.METRICS_BY_ID["plan_check"].out_chars
    assert est.cost == pytest.approx(3 * (note * 1e-6 + out * 2e-7))
    assert est.tag_tokens == round(out * 3 / 4) and est.note_tokens == round(note * 3 / 4)
    assert capture.estimate(capture.History(days=14, sessions=5, cycles=50), ("plan_check",)).cost == 0
    # Both together, and a capture level beside them, add up.
    both = capture.estimate(past, ("plan_check", "feedback_reminder"))
    assert both.cost == pytest.approx(
        est.cost + capture.estimate(past, ("feedback_reminder",)).cost
    )
    rough = catalogue.rough_tokens(("plan_check",))
    assert rough["plan_check"] == round(out / 4) and rough["reminder"] == 0
    assert rough["message_note"] == round(note / 4)


def test_plans_approved_counts_the_approved_plans(tmp_path):
    plan = {"plan": "1. a\n2. b"}
    sessions = []
    for name, outcome_error in (("with", False), ("rejected", True), ("none", None)):
        lines = [user_str_line("do it", origin={"kind": "human"})]
        if outcome_error is not None:
            lines += [
                turn_line(content=[tool_use_block("ExitPlanMode", "tu_p", plan)]),
                user_block_line([tool_result_block("tu_p", "ok", is_error=outcome_error)]),
            ]
        lines.append(turn_line(content=[{"type": "text", "text": "done"}]))
        path = tmp_path / f"{name}.jsonl"
        write_jsonl(path, lines)
        top = parse_transcript(path, TranscriptMeta(path=str(path), kind="top-level"))
        sessions.append(NS(top=top, subs=[], session_id=name))
    past = capture.history(NS(sessions=sessions), None, days=7)
    assert past.sessions == 3 and past.plans_approved == 1


# -- weekly_cost --------------------------------------------------------------------


def _priced_use(since: str, note_cost: float = 0.0, tag_cost: float = 0.0) -> capture.CaptureUsage:
    use = capture.CaptureUsage(since=since)
    use.scopes["main"] = capture.ScopeUse(note_cost=note_cost, tag_cost=tag_cost)
    return use


def test_weekly_cost_spreads_what_was_measured_over_the_weeks_since_it_began():
    use = _priced_use("2026-09-01T00:00:00+00:00", note_cost=1.0, tag_cost=0.4)
    now = datetime(2026, 9, 15, tzinfo=timezone.utc)
    assert capture.weekly_cost(use, now=now) == pytest.approx(1.4 / 2)


def test_weekly_cost_is_none_without_a_start_time():
    assert capture.weekly_cost(capture.CaptureUsage(since="")) is None


def test_weekly_cost_is_none_less_than_a_day_after_it_began():
    use = _priced_use("2026-09-24T00:00:00+00:00", note_cost=1.0)
    now = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
    assert capture.weekly_cost(use, now=now) is None

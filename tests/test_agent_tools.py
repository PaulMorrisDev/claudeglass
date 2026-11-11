"""The one reader of an agent's ``tools:`` line (``recommend.read_agent_tools``):
what the config snapshot stores of it, which tools it offers, and how a
suggested line writes a kept tool."""

from __future__ import annotations

import pytest

from claudeglass.recommend import known_tools_line, read_agent_tools

#: What the snapshot hook stored before it kept a line whole: the string cut at
#: 64 characters (61 and "..."), as ``_clip_name`` does.
_CUT = "Read, Grep, Glob, LS, Skill, NotebookEdit, WebFetch, WebSearch, Bash, Write, Edit"[:61] + "..."


def test_the_cut_string_is_exactly_what_the_hook_leaves():
    assert len(_CUT) == 64 and _CUT.endswith("...")


@pytest.mark.parametrize(
    "stored",
    [
        _CUT,
        "<redacted path/url, str(120)>",
        # An entry the hook masked or cut before any parenthesis is a tool nobody can read.
        ["Read", "<redacted path/url, str(90)>"],
        ["Read", ("mcp__" + "x" * 100)[:61] + "..."],
    ],
)
def test_a_line_the_hook_cut_off_is_not_known_so_every_tool_is_offered(stored):
    line = read_agent_tools(stored)
    assert line.allow is None and known_tools_line(stored) is None
    assert line.offered("Bash") and line.offered("mcp__github__*")


def test_an_agent_entry_cut_inside_its_parentheses_keeps_its_base_name():
    cut = "Agent(worker-one, worker-two, worker-three, worker-four, worker-fi"[:61] + "..."
    assert len(cut) == 64
    line = read_agent_tools([cut, "Read"])
    assert line.allow == ("Agent", "Read")
    assert line.offered("Agent") and not line.offered("Bash")


def test_a_list_is_a_whole_line_however_long_and_a_string_under_the_cap_is_too():
    entries = [f"Tool{n}" for n in range(40)]
    assert read_agent_tools(entries).allow == tuple(entries)
    assert read_agent_tools("Read, Grep, Agent(a, b)").allow == ("Read", "Grep", "Agent(a, b)")
    # A string that merely ends in dots, short of the cap, is not a cut.
    assert read_agent_tools("Read, Grep...").allow == ("Read", "Grep...")


@pytest.mark.parametrize("stored", [None, "", [], "unknown (no config snapshot yet)", 7])
def test_no_line_is_every_tool(stored):
    line = read_agent_tools(stored)
    assert line.allow is None and line.offered("Anything")


def test_task_is_the_old_name_of_agent():
    for stored in ("Task, Read", "Task(a, b), Read", ["Agent", "Read"], ["Agent(a, b)", "Read"]):
        line = read_agent_tools(stored)
        assert line.offered("Agent") and line.offered("Task"), stored
        assert not line.offered("Bash")


def test_a_kept_tool_is_written_as_the_lines_own_entry():
    line = read_agent_tools("Task(a, b), Read, mcp__github__*")
    assert line.entries_for("Agent") == ["Task(a, b)"]
    assert line.entries_for("Read") == ["Read"]
    assert line.entries_for("mcp__github__*") == ["mcp__github__*"]
    assert line.entries_for("Bash") == []
    # With no known line, the name as measured.
    assert read_agent_tools(None).entries_for("Agent") == ["Agent"]
    assert read_agent_tools(None).entries_for("mcp__github__*") == ["mcp__github__*"]


def test_the_reader_carries_the_deny_entries_a_string_or_a_list_holds():
    assert read_agent_tools("Read", "Write, Edit").deny == ("Write", "Edit")
    assert read_agent_tools("Read", ["Write", "mcp__github"]).deny == ("Write", "mcp__github")
    assert read_agent_tools("Read").deny == ()
    # What a cut-off deny string still shows was denied.
    cut = "Write, Edit, NotebookEdit, MultiEdit, Bash, PowerShell, WebFetch, WebSearch"[:61] + "..."
    assert len(cut) == 64
    # The entry the cut falls in may be short, so it is not counted.
    assert read_agent_tools(None, cut).deny == ("Write", "Edit", "NotebookEdit", "MultiEdit", "Bash", "PowerShell")

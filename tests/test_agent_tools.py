"""The one reader of an agent's ``tools:`` line (``recommend.read_agent_tools``):
what the config snapshot stores of it, which tools it offers, and how a
suggested line writes a kept tool."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claudeglass import change_points, snapshots
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


#: A permission-bearing entry the hook cut inside its parentheses.
_CUT_AGENT = ("Agent(" + "worker-one, " * 8)[:61] + "..."
_CUT_BASH = ("Bash(" + "rm -rf /tmp/build-cache && " * 4)[:61] + "..."


def test_the_reader_reports_which_entries_were_cut_inside_their_parentheses():
    assert len(_CUT_AGENT) == len(_CUT_BASH) == 64
    line = read_agent_tools([_CUT_AGENT, "Read", "Bash"])
    assert line.allow == ("Agent", "Read", "Bash")
    assert line.cut == ("Agent",)
    assert read_agent_tools(["Agent(a, b)", "Read"]).cut == ()
    assert read_agent_tools(None).cut == ()


def test_a_cut_deny_entry_counts_as_its_base_tool_and_the_line_is_still_whole():
    # An entry with a specifier still removes the whole tool, so the cut costs nothing.
    line = read_agent_tools(None, [_CUT_BASH, "Write"])
    assert line.deny == ("Bash", "Write") and line.deny_whole is True
    assert not line.offered("Bash") and not line.offered("Write")
    whole = read_agent_tools(None, ["Bash(rm -rf /tmp/x)", "Write"])
    assert whole.deny == ("Bash(rm -rf /tmp/x)", "Write") and whole.deny_whole is True
    assert read_agent_tools("Read").deny_whole is True
    # A comma line the hook cut off, and an entry it masked, are partly read too.
    cut_line = "Write, Edit, NotebookEdit, MultiEdit, Bash, PowerShell, WebFetch, WebSearch"[:61] + "..."
    assert read_agent_tools(None, cut_line).deny_whole is False
    assert read_agent_tools(None, ["Write", "<redacted path/url, str(40)>"]).deny_whole is False


# -- the first snapshot after the upgrade: a line stored cut, then whole --------

_LONG_LINE = [
    "Read", "Grep", "Glob", "Bash", "Edit", "Write", "WebFetch", "WebSearch", "NotebookEdit", "TodoWrite", "Agent(a, b)",
]
#: The string an older hook stored for the line above.
_STORED_CUT = ", ".join(_LONG_LINE)[:61] + "..."


def _write_snapshot(config_dir, ts, tools, *, slug="slug:abc", agent="overseer"):
    folder = config_dir / "snapshots"
    folder.mkdir(parents=True, exist_ok=True)
    doc = {"ts": ts, "schema": 2, "project_slug": slug, "agents": {agent: {"source": "user", "tools": tools}}}
    (folder / f"{ts}.json").write_text(json.dumps(doc), encoding="utf-8")


def test_a_line_stored_cut_then_stored_whole_is_not_an_edit_you_made(tmp_path):
    assert len(_STORED_CUT) == 64
    _write_snapshot(tmp_path, "20260901T100000Z", _STORED_CUT)
    _write_snapshot(tmp_path, "20260902T100000Z", _LONG_LINE)
    assert change_points.change_points(tmp_path) == []
    # The comparison forgives the cut; the record itself is not rewritten from a later file.
    first, second = snapshots.load_snapshots(tmp_path)
    assert first.data["agents"]["overseer"]["tools"] == _STORED_CUT
    assert second.data["agents"]["overseer"]["tools"] == _LONG_LINE
    assert snapshots.changed_keys([first, second]) == []
    assert snapshots.co_changed_keys(first, second) == []
    assert snapshots.diff_keys([first, second]) == {}


def _watch(tmp_path):
    from claudeglass.service.contracts import ServeOptions
    from claudeglass.service.store import Store
    from claudeglass.service.watcher import FileWatcher

    config_dir = tmp_path / "cfg"
    (tmp_path / "projects").mkdir()
    store = Store(":memory:")
    store.open()
    watcher = FileWatcher(store, ServeOptions(projects_root=tmp_path / "projects", config_dir=config_dir))
    return config_dir, store, watcher


def _store_snapshots(store):
    out = []
    for row in store.snapshots():
        data = json.loads(row["digest_json"])
        if snapshots.records_config(data):
            data.setdefault("project_slug", row.get("project_slug"))
            out.append(snapshots.Snapshot(path=Path(""), ts=row["ts"], data=data))
    return sorted(out, key=lambda snap: snap.ts)


def test_the_store_and_the_disk_agree_that_a_cut_line_then_the_whole_list_is_no_change(tmp_path):
    """The watcher ingests the cut snapshot while it is the newest and never
    reads it again, so what the store holds can't depend on a later file."""
    config_dir, store, watcher = _watch(tmp_path)
    _write_snapshot(config_dir, "20260901T100000Z", _STORED_CUT)
    watcher.run_once()
    _write_snapshot(config_dir, "20260902T100000Z", _LONG_LINE)
    watcher.run_once()
    from_store, from_disk = _store_snapshots(store), snapshots.load_snapshots(config_dir)
    assert snapshots.changed_keys(from_store) == snapshots.changed_keys(from_disk) == []
    assert change_points.change_points(config_dir) == []
    assert [snap.data["agents"]["overseer"]["tools"] for snap in from_store] == [snap.data["agents"]["overseer"]["tools"] for snap in from_disk]


def test_a_real_edit_inside_the_first_61_characters_is_reported_from_the_store_and_from_disk(tmp_path):
    config_dir, store, watcher = _watch(tmp_path)
    edited = ["Read", "Grep", "Glob", "Bash", "Edit", "Write", "WebFetch", "MultiEdit"] + _LONG_LINE[8:]
    assert ", ".join(edited)[:61] != ", ".join(_LONG_LINE)[:61]
    _write_snapshot(config_dir, "20260901T100000Z", _STORED_CUT)
    watcher.run_once()
    _write_snapshot(config_dir, "20260902T100000Z", edited)
    watcher.run_once()
    assert snapshots.changed_keys(_store_snapshots(store)) == ["agents.overseer.tools"]
    assert snapshots.changed_keys(snapshots.load_snapshots(config_dir)) == ["agents.overseer.tools"]
    assert [p.keys for p in change_points.change_points(config_dir)] == [["agents.overseer.tools"]]


def test_a_short_comma_line_then_the_same_list_is_not_an_edit_either(tmp_path):
    _write_snapshot(tmp_path, "20260901T100000Z", "Read, Grep")
    _write_snapshot(tmp_path, "20260902T100000Z", ["Read", "Grep"])
    assert change_points.change_points(tmp_path) == []


def test_a_cut_line_then_a_different_whole_line_is_still_an_edit(tmp_path):
    changed = ["Read", "Grep", "Glob", "Bash", "Edit", "Write", "WebFetch", "Agent(c)"]
    _write_snapshot(tmp_path, "20260901T100000Z", _STORED_CUT)
    _write_snapshot(tmp_path, "20260902T100000Z", changed)
    [point] = change_points.change_points(tmp_path)
    assert point.keys == ["agents.overseer.tools"]
    assert snapshots.load_snapshots(tmp_path)[0].data["agents"]["overseer"]["tools"] == _STORED_CUT


def test_a_line_that_changes_is_an_edit_whether_stored_as_a_string_or_a_list(tmp_path):
    _write_snapshot(tmp_path, "20260901T100000Z", ["Read", "Grep"])
    _write_snapshot(tmp_path, "20260902T100000Z", ["Read", "Grep", "Bash"])
    [point] = change_points.change_points(tmp_path)
    assert point.keys == ["agents.overseer.tools"]


# -- deny first: a tool on disallowedTools is not offered, whatever tools says -


@pytest.mark.parametrize(
    "deny, name, offered",
    [
        ("Skill", "Skill", False),
        ("Skill", "Read", True),
        (["Write", "Edit"], "Edit", False),
        # Task is Agent under its old name, on either line.
        ("Task", "Agent", False),
        ("Agent", "Task", False),
        # mcp__X and mcp__X__* withhold server X, and each of its tools.
        ("mcp__github", "mcp__github__*", False),
        ("mcp__github__*", "mcp__github__*", False),
        ("mcp__github", "mcp__github__create_issue", False),
        ("mcp__github", "mcp__gitlab__*", True),
        ("mcp__git", "mcp__github__*", True),
        # mcp__* withholds every MCP tool and no other.
        ("mcp__*", "mcp__github__*", False),
        ("mcp__*", "mcp__github__create_issue", False),
        ("mcp__*", "Read", True),
        # mcp__X__tool withholds that tool only: the server stays offered.
        ("mcp__github__create_issue", "mcp__github__create_issue", False),
        ("mcp__github__create_issue", "mcp__github__*", True),
        ("mcp__github__create_issue", "mcp__github__list_issues", True),
        # An entry with a specifier still removes the whole tool (the sub-agents page).
        ("Bash(rm *)", "Bash", False),
        ("Bash(git push *)", "Bash", False),
        ("Bash(rm *)", "Read", True),
        ("Agent(Explore)", "Agent", False),
        ("Task(Explore)", "Agent", False),
        ("Agent(Explore)", "Bash", True),
        (["Read", ("Bash(" + "rm -rf /tmp/a-very-long-path-" * 3)[:61] + "..."], "Bash", False),
    ],
)
def test_a_denied_tool_is_not_offered_with_no_tools_line(deny, name, offered):
    assert read_agent_tools(None, deny).offered(name) is offered


@pytest.mark.parametrize("tools", ["Read, Skill, mcp__github__*", ["Read", "Skill", "mcp__github__*"]])
def test_a_tool_on_both_lines_is_removed(tools):
    line = read_agent_tools(tools, "Skill, mcp__github")
    assert line.offered("Read")
    assert not line.offered("Skill") and not line.offered("mcp__github__*")
    # A tool the tools line lacks stays absent whatever the deny line says.
    assert not read_agent_tools("Read", "Write").offered("Bash")


def test_a_deny_line_the_hook_cut_off_still_denies_what_it_shows():
    cut = "Write, Edit, NotebookEdit, MultiEdit, Bash, PowerShell, WebFetch, WebSearch"[:61] + "..."
    line = read_agent_tools(None, cut)
    assert not line.offered("Bash") and line.offered("Read")

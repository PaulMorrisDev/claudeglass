"""``cost_centres.py`` (Phase 8a): the spend matrix, its parts, the launch
split behind the cost-per-spawn table, and the model-choice rows.

The scenario (``_scenario``) is one main session and three agents:

- main: a first call that writes a 40k starting prompt at the 1-hour price
  (and starts a background agent and a foreground agent), a warm reply, a
  cold reply weeks later (a rebuild), then a conversation summary and the
  reply after it;
- ``reviewer``: the foreground agent (``tu_fg``), two replies;
- ``Explore``: the background agent (``tu_bg``), two replies;
- ``worker``: a workflow agent, two replies.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claudeglass import cost_centres, quick_actions, tool_search, topology
from claudeglass.calibration import Calibration
from claudeglass.config import Config
from claudeglass.context_budget import ContextBudgetStats, StartupSizes, startup_sizes
from claudeglass.corpus import load_corpus
from claudeglass.model import ReportModel, Section, TranscriptMeta
from claudeglass.parse import parse_transcript
from claudeglass.pricing import load_pricing, price_turn
from claudeglass.recache import RecacheThresholds
from claudeglass.report import build_report
from claudeglass.snapshots import Snapshot, snapshot_project_key

from helpers import (
    attachment_line,
    system_line,
    tool_result_block,
    tool_use_block,
    turn_line,
    user_block_line,
    user_str_line,
    write_jsonl,
)


def _stamp(line: dict, ts: str) -> dict:
    line["timestamp"] = ts
    return line


def _tool(name: str, size: int = 300) -> dict:
    return {"name": name, "description": "d" * size, "input_schema": {"type": "object"}}


def _parse(tmp_path: Path, name: str, lines: list[dict], **meta):
    path = tmp_path / f"{name}.jsonl"
    write_jsonl(path, lines)
    meta.setdefault("session_id", "sess-1")
    return parse_transcript(path, TranscriptMeta(path=str(path), **meta))


def _agent_lines(prefix: str, *, first_write: int = 5_000, day: str = "2026-09-18") -> list[dict]:
    return [
        _stamp(
            turn_line(
                message_id=f"{prefix}_1",
                input_tokens=50,
                cache_creation_input_tokens=first_write,
                ephemeral_5m_input_tokens=first_write,
                cache_read_input_tokens=30_000,
                output_tokens=120,
            ),
            f"{day}T12:10:00.000Z",
        ),
        _stamp(
            turn_line(
                message_id=f"{prefix}_2",
                input_tokens=10,
                cache_creation_input_tokens=800,
                ephemeral_5m_input_tokens=800,
                cache_read_input_tokens=30_000 + first_write,
                output_tokens=90,
            ),
            f"{day}T12:10:30.000Z",
        ),
    ]


def _scenario(tmp_path: Path):
    top_lines = [
        _stamp(user_str_line("please do the thing"), "2026-08-01T11:59:00.000Z"),
        _stamp(
            attachment_line("prompt_snapshot", systemPrompt=["s" * 4_000], tools=[_tool("Read"), _tool("Bash", 900)]),
            "2026-08-01T11:59:30.000Z",
        ),
        _stamp(
            turn_line(
                message_id="top_1",
                input_tokens=100,
                cache_creation_input_tokens=35_000,
                ephemeral_1h_input_tokens=35_000,
                cache_read_input_tokens=5_000,
                output_tokens=200,
                content=[
                    tool_use_block("Agent", "tu_bg", {"subagent_type": "Explore", "prompt": "look", "run_in_background": True}),
                    tool_use_block("Agent", "tu_fg", {"subagent_type": "reviewer", "prompt": "review"}),
                ],
            ),
            "2026-08-01T12:00:00.000Z",
        ),
        _stamp(user_block_line([tool_result_block("tu_fg", "x" * 200)]), "2026-08-01T12:00:20.000Z"),
        _stamp(
            turn_line(
                message_id="top_2",
                input_tokens=20,
                cache_creation_input_tokens=1_000,
                ephemeral_5m_input_tokens=1_000,
                cache_read_input_tokens=40_000,
                output_tokens=100,
            ),
            "2026-08-01T12:01:00.000Z",
        ),
        # Weeks later, a cold start of the same conversation: a rebuild.
        _stamp(
            turn_line(
                message_id="top_3",
                input_tokens=20,
                cache_creation_input_tokens=41_000,
                ephemeral_5m_input_tokens=41_000,
                cache_read_input_tokens=0,
                output_tokens=100,
            ),
            "2026-09-10T12:00:00.000Z",
        ),
        _stamp(
            system_line(
                "compact_boundary",
                compactMetadata={
                    "preTokens": 41_000,
                    "postTokens": 8_000,
                    "cumulativeDroppedTokens": 33_000,
                    "durationMs": 500,
                    "trigger": "auto",
                },
            ),
            "2026-09-18T11:59:00.000Z",
        ),
        _stamp(
            turn_line(
                message_id="top_4",
                input_tokens=20,
                cache_creation_input_tokens=6_000,
                ephemeral_5m_input_tokens=6_000,
                cache_read_input_tokens=40_000,
                output_tokens=100,
            ),
            "2026-09-18T12:00:00.000Z",
        ),
        _stamp(
            turn_line(
                message_id="top_5",
                input_tokens=20,
                cache_creation_input_tokens=500,
                ephemeral_5m_input_tokens=500,
                cache_read_input_tokens=46_000,
                output_tokens=100,
            ),
            "2026-09-18T12:01:00.000Z",
        ),
    ]
    top = _parse(tmp_path, "top", top_lines, kind="top-level")
    reviewer = _parse(
        tmp_path, "agent-fg", _agent_lines("fg"), kind="subagent", agent_id="agent-fg", agent_type="reviewer",
        tool_use_id="tu_fg", spawn_depth=1, model_recorded=True,
    )
    explore = _parse(
        tmp_path, "agent-bg", _agent_lines("bg"), kind="subagent", agent_id="agent-bg", agent_type="Explore",
        tool_use_id="tu_bg", spawn_depth=1, model_recorded=True, agent_model_alias="opus",
    )
    worker = _parse(
        tmp_path, "agent-wf", _agent_lines("wf"), kind="workflow-agent", agent_id="agent-wf", agent_type="worker",
        tool_use_id="tu_wf", spawn_depth=1, model_recorded=True,
    )
    return top, [reviewer, explore, worker]


def _compute(top, subs, **kwargs):
    return cost_centres.compute([(top, subs)], load_pricing(), RecacheThresholds(), **kwargs)


def _total(top, subs) -> float:
    pricing = load_pricing()
    return sum(
        price_turn(turn, pricing.resolve_model(turn.model)).total
        for result in (top, *subs)
        for turn in result.turns
        if turn.turn_index > 0
    )


# -- the matrix --------------------------------------------------------------------------------


def test_rows_sum_to_the_total_window_spend(tmp_path):
    top, subs = _scenario(tmp_path)
    cc = _compute(top, subs)
    assert cc.total() == pytest.approx(_total(top, subs))
    table = cost_centres.build_matrix_table(cc)
    total_idx = [c.key for c in table.columns].index("total")
    assert sum(row[total_idx] for row in table.rows) == pytest.approx(_total(top, subs))
    # Every row's own total is its cells' sum.
    for row in table.rows:
        assert row[total_idx] == pytest.approx(sum(row[1:total_idx]))


def test_rows_sum_to_the_total_with_the_base_split_into_parts(tmp_path):
    top, subs = _scenario(tmp_path)
    cc = _compute(top, subs, calibration=Calibration(), context_stats=ContextBudgetStats())
    assert cc.total() == pytest.approx(_total(top, subs))
    for centre in cost_centres.CENTRES:
        for cell in ("base_read", "rewrite", "post_compaction"):
            parts = sum(cost for (c, k, _p), cost in cc.parts.items() if c == centre and k == cell)
            assert parts == pytest.approx(cc.cell(centre, cell))


def test_a_main_sessions_first_call_is_the_session_start_row(tmp_path):
    top, subs = _scenario(tmp_path)
    cc = _compute(top, subs)
    pricing = load_pricing()
    first = next(t for t in top.turns if t.turn_index == 1)
    breakdown = price_turn(first, pricing.resolve_model(first.model))
    assert cc.centre_total("start") == pytest.approx(breakdown.total)
    # The 1-hour write is the whole of its growth cell.
    assert cc.cell("start", "growth_write") == pytest.approx(breakdown.cache_write_cost + breakdown.input_cost)


def test_agents_split_by_what_started_them(tmp_path):
    top, subs = _scenario(tmp_path)
    cc = _compute(top, subs)
    pricing = load_pricing()

    def spend(result):
        return sum(
            price_turn(t, pricing.resolve_model(t.model)).total for t in result.turns if t.turn_index > 0
        )

    reviewer, explore, worker = subs
    assert cc.centre_total("direct") == pytest.approx(spend(reviewer) + spend(explore))
    assert cc.centre_total("workflow") == pytest.approx(spend(worker))


def test_base_read_is_capped_at_the_base_and_the_rest_reads_above_it(tmp_path):
    top, subs = _scenario(tmp_path)
    cc = _compute(top, subs)
    pricing = load_pricing()
    worker = subs[2]
    turns = [t for t in worker.turns if t.turn_index > 0]
    base = turns[0].cache_creation_tokens + turns[0].cache_read_tokens
    expected_base = expected_above = 0.0
    for turn in turns:
        cost = price_turn(turn, pricing.resolve_model(turn.model)).cache_read_cost
        share = min(base, turn.cache_read_tokens) / turn.cache_read_tokens
        expected_base += cost * share
        expected_above += cost * (1 - share)
    assert cc.cell("workflow", "base_read") == pytest.approx(expected_base)
    assert cc.cell("workflow", "above_read") == pytest.approx(expected_above)


def test_a_rebuild_and_the_reply_after_a_summary_have_their_own_cells(tmp_path):
    top, subs = _scenario(tmp_path)
    cc = _compute(top, subs)
    pricing = load_pricing()
    by_id = {t.message_id: t for t in top.turns}
    rebuild = price_turn(by_id["top_3"], pricing.resolve_model(by_id["top_3"].model))
    after = price_turn(by_id["top_4"], pricing.resolve_model(by_id["top_4"].model))
    assert cc.cell("main", "rewrite") == pytest.approx(rebuild.cache_write_cost)
    assert cc.cell("main", "post_compaction") >= after.cache_write_cost - 1e-12
    assert cc.cell("main", "growth_write") > 0


def test_a_rewrite_splits_into_the_prefix_it_wrote_again_and_the_conversation(tmp_path):
    top, subs = _scenario(tmp_path)
    cc = _compute(top, subs, calibration=Calibration(), context_stats=ContextBudgetStats())
    base = 40_000
    rewrite = cc.cell("main", "rewrite")
    prefix = cc.parts[("main", "rewrite", "prefix")]
    conversation = cc.parts[("main", "rewrite", "conversation")]
    # The cold reply wrote 41,000 tokens, 40,000 of them the starting prompt.
    assert prefix == pytest.approx(rewrite * base / 41_000)
    assert prefix + conversation == pytest.approx(rewrite)


def test_the_newest_days_are_a_subset_of_the_window(tmp_path):
    top, subs = _scenario(tmp_path)
    cc = _compute(top, subs)
    window, thirty, seven = (cc.total(view) for view in cost_centres.VIEWS)
    assert 0 < seven <= thirty < window
    # The first calls are weeks before the newest reply.
    assert cc.centre_total("start", "30d") == 0.0
    assert cc.centre_total("main", "7d") > 0.0


def test_a_cell_without_spend_is_left_out_and_an_empty_corpus_has_no_rows():
    cc = cost_centres.compute([], load_pricing(), RecacheThresholds())
    assert cc.total() == 0.0
    assert cost_centres.build_matrix_table(cc).rows == []
    assert cost_centres.build_advice_table(cc).rows == []
    assert cost_centres.matrix_usd(cc) == {}


# -- the base, in parts ---------------------------------------------------------------------------


def _sizes(**kwargs) -> StartupSizes:
    base = dict(
        system=3_000.0,
        builtin_tools={"Read": 500.0, "Artifact": 13_000.0, "PowerShell": 4_500.0, "WebFetch": 800.0},
        server_definitions={"srv": 2_000.0},
        server_deferred={"srv": 300.0},
        server_instructions={"srv": 700.0},
        skills=1_500.0,
        claude_md=2_500.0,
        memory=400.0,
        hooks=200.0,
    )
    base.update(kwargs)
    return StartupSizes(**base)


def test_the_base_parts_add_up_to_the_base():
    parts = cost_centres.compose_base(_sizes(), 40_000.0, allowlist=["WebFetch"])
    assert sum(parts.values()) == pytest.approx(40_000.0)
    # What nothing measured covers is left in one part of its own.
    measured = 3_000 + 500 + 13_000 + 4_500 + 800 + 3_000 + 1_500 + 2_500 + 400 + 200
    assert parts["other"] == pytest.approx(40_000 - measured)


def test_overlapping_levers_count_a_token_once():
    """An allowlist can drop an MCP server's definitions and so can switching
    the connector off: the tokens are the connector's, and never also the
    allowlist's."""
    parts = cost_centres.compose_base(_sizes(), 40_000.0, allowlist=["WebFetch", "mcp__srv__*", "NotATool"])
    assert parts["connector"] == pytest.approx(2_000 + 300 + 700)
    assert parts["allowlist"] == pytest.approx(800)
    assert "tool:WebFetch" not in parts
    assert parts["claude_md"] == pytest.approx(2_500)
    assert parts["memory"] == pytest.approx(400)
    assert sum(parts.values()) == pytest.approx(40_000.0)


def test_the_desktop_apps_own_servers_are_fixed_in_a_main_session():
    sizes = _sizes(
        server_definitions={"srv": 2_000.0, "computer-use": 5_000.0},
        server_deferred={"srv": 300.0, "ccd_session": 400.0},
        server_instructions={"srv": 700.0, "computer-use": 600.0},
    )
    main = cost_centres.compose_base(sizes, 60_000.0, main=True)
    assert main["connector"] == pytest.approx(3_000)
    assert main["desktop_servers"] == pytest.approx(6_000)
    assert cost_centres.part_lever("desktop_servers") == cost_centres.FIXED
    agent = cost_centres.compose_base(sizes, 60_000.0)
    assert agent["connector"] == pytest.approx(9_000) and "desktop_servers" not in agent


def test_the_desktop_apps_servers_are_the_apps_own_only_where_the_caller_names_them():
    """``built_in`` is the tool search table's answer: a server of yours that
    shares a name from the desktop app's list stays the connector's."""
    sizes = _sizes(server_definitions={"terminal": 5_000.0, "srv": 2_000.0}, server_deferred={}, server_instructions={})
    by_name = cost_centres.compose_base(sizes, 60_000.0, main=True)
    assert by_name["desktop_servers"] == pytest.approx(5_000) and by_name["connector"] == pytest.approx(2_000)
    yours = cost_centres.compose_base(sizes, 60_000.0, main=True, built_in=frozenset())
    assert "desktop_servers" not in yours and yours["connector"] == pytest.approx(7_000)
    theirs = cost_centres.compose_base(sizes, 60_000.0, main=True, built_in=frozenset({"terminal"}))
    assert theirs["desktop_servers"] == pytest.approx(5_000)
    # Not a main session: the answer never matters.
    assert "desktop_servers" not in cost_centres.compose_base(sizes, 60_000.0, built_in=frozenset({"terminal"}))


def _lone_session(tmp_path, name, **meta):
    """A main session whose first call writes and reads a 35k base."""
    return _parse(tmp_path, name, _agent_lines(name), kind="top-level", session_id=name, **meta)


def _parts_of_the_base(top, monkeypatch, **kwargs) -> set[str]:
    sizes = _sizes(server_definitions={"terminal": 5_000.0}, server_deferred={}, server_instructions={})
    monkeypatch.setattr(cost_centres, "startup_sizes", lambda result, calibration: sizes)
    cc = _compute(top, [], calibration=Calibration(), **kwargs)
    return {part for (_centre, cell, part) in cc.parts if cell == "base_read"}


def test_the_desktop_servers_part_is_only_for_a_main_session_that_ran_in_the_desktop_app(tmp_path, monkeypatch):
    desktop = _parts_of_the_base(_lone_session(tmp_path, "a", entrypoint="claude-desktop"), monkeypatch)
    assert "desktop_servers" in desktop and "connector" not in desktop
    # A command-line session has no servers built into the app: a server of yours that shares a name is the connector's.
    for entrypoint in ("cli", None):
        parts = _parts_of_the_base(_lone_session(tmp_path, f"b-{entrypoint}", entrypoint=entrypoint), monkeypatch)
        assert "connector" in parts and "desktop_servers" not in parts


def test_the_tool_search_rows_decide_which_servers_the_desktop_app_brought(tmp_path, monkeypatch):
    top = _lone_session(tmp_path, "a", entrypoint="claude-desktop")
    yours = [tool_search.McpServerRow(server="terminal", kind=tool_search.KIND_USER)]
    assert "desktop_servers" not in _parts_of_the_base(top, monkeypatch, mcp_servers=yours)
    theirs = [tool_search.McpServerRow(server="id-1", aliases=("terminal",), kind=tool_search.KIND_DESKTOP_BUILTIN)]
    assert "desktop_servers" in _parts_of_the_base(top, monkeypatch, mcp_servers=theirs)
    # No rows to go on: the name on the list decides, in a desktop session.
    assert "desktop_servers" in _parts_of_the_base(top, monkeypatch, mcp_servers=[])


def _desktop_report_parts(tmp_path, snapshots) -> set[str]:
    """The base's parts the report files a desktop session under, where the
    session was offered a server named ``terminal`` (a name on the app's
    list) and ``snapshots`` is what the report read of the config."""
    project_dir = tmp_path / "proj-desk"
    project_dir.mkdir(parents=True)
    lines = [
        _stamp(user_str_line("please do the thing", entrypoint="claude-desktop"), "2026-09-18T11:59:00.000Z"),
        _stamp(
            attachment_line(
                "mcp_instructions_delta", addedNames=["terminal"], addedBlocks=["Run a command. " * 400], removedNames=[]
            ),
            "2026-09-18T11:59:10.000Z",
        ),
        _stamp(
            turn_line(
                message_id="d_1",
                input_tokens=100,
                cache_creation_input_tokens=35_000,
                ephemeral_1h_input_tokens=35_000,
                cache_read_input_tokens=5_000,
                output_tokens=200,
            ),
            "2026-09-18T12:00:00.000Z",
        ),
    ]
    write_jsonl(project_dir / "session-1.jsonl", lines)
    report = build_report(
        load_corpus([project_dir]), load_pricing(), Config(), projects=("proj-desk",), window="w", snapshots=snapshots
    )
    agents = next(section for section in report.sections if section.key == "agents")
    table = next(t for t in agents.tables if t.name == cost_centres.PARTS_TABLE)
    keys = [column.key for column in table.columns]
    rows = [dict(zip(keys, row)) for row in table.rows]
    return {row["part"] for row in rows if row["cell"] == "base_read"}


def test_the_report_files_a_server_of_yours_as_the_connector_even_when_the_app_has_one_by_that_name(tmp_path):
    """The report hands the cost centres the tool search table's rows, so a
    server your config lists is not counted as one the desktop app brought."""
    app_label = cost_centres.part_label("desktop_servers")
    connector_label = cost_centres.part_label("connector")
    assert app_label in _desktop_report_parts(tmp_path / "unlisted", None)
    listed = Snapshot(
        path=None,
        ts="2026-09-30T00:00:00Z",
        data={"project_slug": snapshot_project_key("proj-desk"), "mcp_servers": {"names": ["terminal"]}},
    )
    parts = _desktop_report_parts(tmp_path / "listed", [listed])
    assert connector_label in parts and app_label not in parts


def test_the_agent_list_is_a_controllable_part_of_its_own():
    parts = cost_centres.compose_base(_sizes(agent_roster=1_200.0), 40_000.0)
    assert parts["roster"] == pytest.approx(1_200)
    measured = 3_000 + 500 + 13_000 + 4_500 + 800 + 3_000 + 1_500 + 2_500 + 400 + 200 + 1_200
    assert parts["other"] == pytest.approx(40_000 - measured)
    assert sum(parts.values()) == pytest.approx(40_000.0)
    assert "roster" not in cost_centres.compose_base(_sizes(), 40_000.0)
    assert cost_centres.part_lever("roster") == cost_centres.CONTROLLABLE
    assert cost_centres.part_label("roster") == "Agent list"
    for centre in ("main", "start", "direct", "workflow"):
        assert cost_centres.part_card("roster", centre) in quick_actions.CHECK_IDS


def test_what_nothing_measured_covers_is_not_claimed_to_be_out_of_reach():
    """The brief an agent is given and a session's first prompt are in "not
    itemised": marked not measured, with neither a check nor "no setting known"."""
    assert cost_centres.part_lever("other") == cost_centres.UNMEASURED
    assert cost_centres.LEVER_LABELS[cost_centres.UNMEASURED] == "Not measured"
    assert set(cost_centres.LEVER_LABELS) == {cost_centres.FIXED, cost_centres.CONTROLLABLE, cost_centres.UNMEASURED}
    cc = cost_centres.CostCentres(sessions=1)
    cc.matrix["window"] = {("direct", "base_read"): 10.0}
    parts = cost_centres.compose_base(_sizes(agent_roster=1_200.0), 40_000.0)
    cc.parts = {("direct", "base_read", part): 10.0 * tokens / 40_000.0 for part, tokens in parts.items()}
    table = cost_centres.build_parts_table(cc)
    keys = [c.key for c in table.columns]
    by_part = {row[keys.index("part")]: dict(zip(keys, row)) for row in table.rows}
    other = by_part["Not itemised"]
    assert other["lever"] == "unmeasured" and other["card"] == "" and other["advice"] == ""
    assert table.value_labels["unmeasured"] == "Not measured"
    # The agent list is one the user can change, and links its check.
    assert by_part["Agent list"]["lever"] == "controllable" and by_part["Agent list"]["card"] == "tools"
    assert by_part["Agent list"]["advice"] == ""
    # Only the harness-fixed parts say "no setting known".
    assert {name for name, row in by_part.items() if row["advice"] == cost_centres.NO_SETTING} == {
        name for name, row in by_part.items() if row["lever"] == "fixed"
    }


def test_a_measured_total_over_the_base_is_scaled_to_it():
    parts = cost_centres.compose_base(_sizes(), 10_000.0)
    assert sum(parts.values()) == pytest.approx(10_000.0)
    assert "other" not in parts


def test_harness_fixed_parts_say_no_setting_is_known_and_controllable_ones_link_a_check():
    cc = cost_centres.CostCentres(sessions=1)
    cc.matrix["window"] = {("main", "base_read"): 10.0}
    parts = cost_centres.compose_base(_sizes(), 40_000.0, allowlist=["WebFetch"])
    cc.parts = {("main", "base_read", part): 10.0 * tokens / 40_000.0 for part, tokens in parts.items()}
    table = cost_centres.build_parts_table(cc)
    keys = [c.key for c in table.columns]
    rows = [dict(zip(keys, row)) for row in table.rows]
    artifact = next(r for r in rows if r["part"] == "Artifact tool definition")
    assert artifact["lever"] == "fixed" and artifact["advice"] == "no setting known" and artifact["card"] == ""
    assert next(r for r in rows if r["part"] == "PowerShell tool definition")["advice"] == "no setting known"
    by_part = {r["part"]: r for r in rows}
    assert by_part["CLAUDE.md files"]["card"] == "claude-md"
    assert by_part["Auto memory"]["card"] == "claude-md"
    assert by_part["Skills list"]["card"] == "skills"
    assert by_part["Built-in tools this agent type rarely uses"]["card"] == "tools"
    assert by_part["MCP servers: tools, names and instructions"]["card"] == "tool-search"
    assert by_part["System prompt"]["advice"] == "no setting known"
    assert abs(sum(r["share"] for r in rows) - 100.0) < 1e-6
    # Only the largest built-in tools are named, and the Artifact tool and PowerShell always are.
    assert len([r for r in rows if r["part"].endswith(" tool definition")]) <= cost_centres.NAMED_TOOLS + len(
        cost_centres.ALWAYS_NAMED_TOOLS
    )


def test_the_artifact_tool_and_powershell_are_named_even_when_small_and_the_rest_fold_together():
    cc = cost_centres.CostCentres(sessions=1)
    cc.matrix["window"] = {("main", "base_read"): 10.0}
    cc.parts = {
        ("main", "base_read", "tool:Read"): 3.0,
        ("main", "base_read", "tool:Bash"): 2.5,
        ("main", "base_read", "tool:Edit"): 2.0,
        ("main", "base_read", "tool:Grep"): 1.5,
        ("main", "base_read", "tool:Artifact"): 0.6,
        ("main", "base_read", "tool:PowerShell"): 0.4,
    }
    table = cost_centres.build_parts_table(cc)
    names = [row[2] for row in table.rows]
    assert "Artifact tool definition" in names and "PowerShell tool definition" in names
    assert "Grep tool definition" not in names and "Other built-in tool definitions" in names
    folded = next(row for row in table.rows if row[2] == "Other built-in tool definitions")
    assert folded[4] == pytest.approx(1.5)


def test_an_agents_connector_part_links_the_tools_check():
    cc = cost_centres.CostCentres(sessions=1)
    cc.matrix["window"] = {("direct", "base_read"): 4.0}
    cc.parts = {("direct", "base_read", "connector"): 4.0}
    table = cost_centres.build_parts_table(cc)
    assert table.rows[0][[c.key for c in table.columns].index("card")] == "tools"


def test_startup_sizes_reads_the_first_call_of_an_agent_whose_tools_come_after_it(tmp_path):
    lines = [
        _stamp(user_str_line("investigate the parser " * 4), "2026-09-18T12:00:00.000Z"),
        _stamp(attachment_line("prompt_snapshot", systemPrompt=["s" * 2_000]), "2026-09-18T12:00:01.000Z"),
        _stamp(
            turn_line(
                message_id="s_1",
                input_tokens=100,
                cache_creation_input_tokens=3_000,
                cache_read_input_tokens=60_000,
                mcp_servers=None,
            ),
            "2026-09-18T12:00:02.000Z",
        ),
        _stamp(
            attachment_line(
                "prompt_snapshot",
                systemPrompt=["s" * 2_000],
                tools=[_tool("Read"), _tool("Bash", 900), _tool("mcp__figma__a"), _tool("mcp__figma__b", 500)],
            ),
            "2026-09-18T12:00:03.000Z",
        ),
        _stamp(attachment_line("agent_listing_delta", rendered="a" * 400), "2026-09-18T12:00:03.500Z"),
        _stamp(
            turn_line(message_id="s_2", cache_read_input_tokens=63_000, content=[tool_use_block("Read", "tu_1", {})]),
            "2026-09-18T12:00:10.000Z",
        ),
    ]
    sub = _parse(tmp_path, "agent-s", lines, kind="subagent", agent_type="Explore", agent_id="agent-s")
    sizes = startup_sizes(sub, Calibration())
    assert sizes is not None and sizes.saw_tools
    assert set(sizes.builtin_tools) == {"Read", "Bash"}
    assert set(sizes.server_definitions) == {"figma"}
    assert sizes.builtin_tools["Bash"] > sizes.builtin_tools["Read"]
    assert sizes.system == pytest.approx(2_000 / 4.0)
    assert sizes.agent_roster == pytest.approx(400 / 4.0)
    assert startup_sizes(_parse(tmp_path, "empty", [], kind="subagent"), Calibration()) is None


# -- advice ----------------------------------------------------------------------------------------


def test_every_hint_names_a_real_check_and_the_start_read_has_no_advice():
    hinted = {
        cost_centres.hint_for(centre, cell) for centre in cost_centres.CENTRES for cell in cost_centres.CELLS
    } - {None}
    assert hinted <= set(quick_actions.CHECK_IDS)
    assert hinted <= set(cost_centres.CHECK_LABELS)
    assert cost_centres.hint_for("start", "base_read") is None


def test_the_advice_table_names_the_check_or_says_there_is_no_advice(tmp_path):
    top, subs = _scenario(tmp_path)
    cc = _compute(top, subs)
    table = cost_centres.build_advice_table(cc)
    keys = [c.key for c in table.columns]
    rows = [dict(zip(keys, row)) for row in table.rows]
    start_read = next(r for r in rows if r["centre"] == "start" and r["cell"] == "base_read")
    assert start_read["hint"] == cost_centres.NO_ADVICE
    assert table.value_labels[cost_centres.NO_ADVICE] == "No advice"
    rewrite = next(r for r in rows if r["cell"] == "rewrite")
    assert rewrite["hint"] == "cache"
    assert all(r["cost_7d"] <= r["cost_30d"] <= r["cost"] + 1e-12 for r in rows)


def test_the_largest_centre_is_read_back_from_the_table(tmp_path):
    top, subs = _scenario(tmp_path)
    cc = _compute(top, subs)
    report = ReportModel(sections=[Section(key="agents", tables=cost_centres.build_tables(cc))])
    found = cost_centres.largest_centre(report)
    assert found is not None
    centre, cell, total, share = found
    assert total == pytest.approx(max(cc.centre_total(c) for c in cost_centres.CENTRES))
    assert cell in cost_centres.CELLS and 0 < share <= 100
    assert cost_centres.largest_centre(ReportModel(sections=[])) is None


# -- model choice -------------------------------------------------------------------------------------


def test_model_choice_rows_split_direct_and_workflow_by_tier_and_who_chose(tmp_path):
    top, subs = _scenario(tmp_path)
    cc = _compute(top, subs, calibration=Calibration(), agent_files={"reviewer": "sonnet"})
    keys = {key: row for key, row in cc.models.items()}
    assert ("direct", "reviewer", "sonnet", "file") in keys
    assert ("direct", "Explore", "sonnet", "call") in keys  # named in the call (alias "opus"), ran on Sonnet
    assert ("workflow", "worker", "sonnet", "inherited") in keys
    assert all(row.runs == 1 for row in keys.values())
    # Nothing above Sonnet here, so there is no ceiling.
    table = cost_centres.build_models_table(cc)
    ceiling = [c.key for c in table.columns].index("ceiling")
    assert all(row[ceiling] is None for row in table.rows)
    assert [row[0] for row in table.rows] == ["direct", "direct", "workflow"]


def test_an_opus_run_has_a_sonnet_ceiling(tmp_path):
    lines = [
        _stamp(
            turn_line(message_id="o_1", model="claude-opus-4-1", input_tokens=1_000, output_tokens=2_000),
            "2026-09-18T12:00:00.000Z",
        )
    ]
    top = _parse(tmp_path, "top", [_stamp(turn_line(message_id="t_1"), "2026-09-18T11:00:00.000Z")], kind="top-level")
    sub = _parse(
        tmp_path, "agent-o", lines, kind="subagent", agent_type="Plan2", agent_id="agent-o", model_recorded=True
    )
    cc = _compute(top, [sub], calibration=Calibration())
    row = cc.models[("direct", "Plan2", "opus", "inherited")]
    assert row.cost > row.cost_on_sonnet > 0
    table = cost_centres.build_models_table(cc)
    ceiling = table.rows[0][[c.key for c in table.columns].index("ceiling")]
    assert ceiling == pytest.approx(row.cost - row.cost_on_sonnet)


# -- the launch split (topology_cost_per_spawn) --------------------------------------------------------


def test_launches_are_read_from_the_parent_call(tmp_path):
    top, subs = _scenario(tmp_path)
    launches = topology.launches_by_tool_use([top, *subs])
    assert launches == {"tu_bg": "background", "tu_fg": "foreground"}
    reviewer, explore, worker = subs
    assert topology.launch_of(explore, launches) == "background"
    assert topology.launch_of(reviewer, launches) == "foreground"


def test_a_workflow_agent_is_never_counted_as_foreground(tmp_path):
    top, subs = _scenario(tmp_path)
    worker = subs[2]
    # Even when its tool_use id is joined to a foreground call.
    assert topology.launch_of(worker, {"tu_wf": "foreground"}) == "workflow"
    assert topology.launch_of(worker, {}) == "workflow"
    stats = topology.TopologyStats()
    stats.add_session("sess-1", top, subs, load_pricing())
    assert ("worker", "workflow") in stats.cost_by_launch
    assert ("worker", "foreground") not in stats.cost_by_launch
    assert set(stats.cost_by_launch) == {
        ("reviewer", "foreground"),
        ("Explore", "background"),
        ("worker", "workflow"),
    }


def test_the_cost_per_spawn_table_has_runs_total_and_the_launch(tmp_path):
    top, subs = _scenario(tmp_path)
    stats = topology.TopologyStats()
    stats.add_session("sess-1", top, subs, load_pricing())
    table = next(t for t in topology.build_section(stats).tables if t.name == "topology_cost_per_spawn")
    keys = [c.key for c in table.columns]
    assert keys == ["agent_type", "launch", "runs", "total_cost", "mean_cost", "median_cost", "mean_tool_wait"]
    rows = {(row[0], row[1]): dict(zip(keys, row)) for row in table.rows}
    assert rows[("Explore", "background")]["runs"] == 1
    assert rows[("Explore", "background")]["total_cost"] == pytest.approx(rows[("Explore", "background")]["mean_cost"])
    assert table.value_labels["background"] == "Background agent"
    assert table.value_labels["workflow"] == "Workflow agent"


def test_the_spawns_note_gives_the_median_among_sessions_that_spawn_and_those_that_spawn_none(tmp_path):
    top, subs = _scenario(tmp_path)
    lonely = _parse(tmp_path, "lonely", [turn_line(message_id="l_1")], kind="top-level", session_id="sess-2")
    stats = topology.TopologyStats()
    pricing = load_pricing()
    stats.add_session("sess-1", top, subs, pricing)
    stats.add_session("sess-2", lonely, [], pricing)
    stats.add_session("sess-3", lonely, [], pricing)
    note = next(t for t in topology.build_section(stats).tables if t.name == "topology_spawn_depth").notes[0]
    assert "Median spawns per session that spawns any: 3" in note
    assert "Sessions that spawn none: 2." in note
    assert "Sessions seen: 3" in note


# -- reading the launch word out of the transcript -------------------------------------------------------


def test_the_launch_word_comes_from_the_call_and_an_async_result_overrides_it(tmp_path):
    lines = [
        turn_line(
            message_id="p_1",
            content=[
                tool_use_block("Agent", "tu_plain", {"subagent_type": "Explore", "prompt": "go"}),
                tool_use_block("Agent", "tu_flag", {"subagent_type": "Explore", "prompt": "go", "run_in_background": True}),
                tool_use_block("Agent", "tu_async", {"subagent_type": "Explore", "prompt": "go"}),
                tool_use_block("Agent", "tu_false", {"subagent_type": "Explore", "prompt": "go", "run_in_background": False}),
            ],
        ),
        user_block_line(
            [tool_result_block("tu_async", "Async agent launched successfully.")],
            toolUseResult={"isAsync": True, "agentId": "abc123", "status": "async_launched"},
        ),
    ]
    result = _parse(tmp_path, "p", lines, kind="top-level")
    assert topology.launches_by_tool_use([result]) == {
        "tu_plain": "foreground",
        "tu_flag": "background",
        "tu_async": "background",
        "tu_false": "foreground",
    }
    assert dict(next(t for t in result.turns if t.turn_index == 1).agent_launches) == topology.launches_by_tool_use([result])


def test_nothing_in_the_tables_carries_a_server_name_or_text(tmp_path):
    top, subs = _scenario(tmp_path)
    cc = _compute(top, subs, calibration=Calibration(), context_stats=ContextBudgetStats())
    text = json.dumps(
        [
            [t.name, t.title, t.columns and [c.label for c in t.columns], t.rows, t.notes]
            for t in cost_centres.build_tables(cc)
        ],
        default=str,
    )
    assert "please do the thing" not in text and "figma" not in text

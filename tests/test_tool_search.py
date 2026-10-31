"""What MCP tool search saves (``tool_search``), from what ``parse.py``
keeps of Claude Code's deferred-tool records."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from claudeglass import advice, ignores, known_savers
from claudeglass import fixes as fixes_mod
from claudeglass import quick_actions as qa
from claudeglass import tool_search
from claudeglass.cache import encode_result, result_from_jsonable
from claudeglass.calibration import Calibration
from claudeglass.model import (
    Diagnostics,
    PricingMeta,
    Recommendation,
    ReportMeta,
    ReportModel,
    TranscriptMeta,
    TranscriptResult,
    Turn,
)
from claudeglass.parse import mcp_name, parse_transcript, tool_server
from claudeglass.pricing import load_pricing, price_turn
from claudeglass.snapshots import Snapshot, snapshot_project_key, snapshot_project_keys
from claudeglass.units import Units

from helpers import attachment_line, ignorable_line, system_line, tool_use_block, turn_line, write_jsonl

PRICING = load_pricing()
SONNET = PRICING.resolve_model("claude-sonnet-5").rates

LONG_DESCRIPTION = "Lists the pull requests of a repository, with their state and reviewers. " * 4


def _entry(name: str, description: str = "Does one thing.") -> dict:
    return {
        "name": name,
        "description": description,
        "input_schema": {"type": "object", "properties": {"owner": {"type": "string"}}},
        "defer_loading": True,
    }


def _definition_chars(entry: dict) -> int:
    definition = {key: entry.get(key) for key in ("name", "description", "input_schema")}
    return len(json.dumps(definition, ensure_ascii=False, separators=(",", ":")))


# -- parsing ------------------------------------------------------------------


def test_tool_server_names_the_mcp_server_or_built_in():
    assert tool_server("mcp__github__list_issues") == "github"
    assert tool_server("mcp__Claude_Code_Remote__send_later") == "Claude_Code_Remote"
    assert tool_server("WebFetch") == "built-in"
    assert tool_server("mcp__broken") == "built-in"


def test_each_reply_keeps_its_deferred_tools_by_server(tmp_path: Path):
    names = ["mcp__github__list_prs", "mcp__github__merge_pr", "mcp__jira__search", "WebFetch"]
    loaded = _entry("mcp__github__list_prs", LONG_DESCRIPTION)
    lines = [
        attachment_line("deferred_tools_delta", addedNames=names, addedLines=names, removedNames=[]),
        turn_line(message_id="msg_1"),
        turn_line(message_id="msg_2", content=[tool_use_block("ToolSearch", "toolu_1", {"query": "prs"})]),
        attachment_line("deferred_tools_record", entries=[loaded]),
        turn_line(message_id="msg_3"),
        attachment_line("deferred_tools_delta", addedNames=[], removedNames=["mcp__jira__search"]),
        turn_line(message_id="msg_4"),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)

    result = parse_transcript(path, TranscriptMeta(path=str(path)))

    by_turn = [turn.deferred_tools_by_server for turn in result.turns]
    assert by_turn == [
        {"github": 2, "jira": 1, "built-in": 1},
        {"github": 2, "jira": 1, "built-in": 1},
        {"github": 1, "jira": 1, "built-in": 1},
        {"github": 1, "built-in": 1},
    ]
    assert result.turns[0].deferred_list_chars == sum(len(n) + 1 for n in names)
    assert result.turns[3].deferred_list_chars == sum(len(n) + 1 for n in names if n != "mcp__jira__search")
    assert result.tool_definition_chars == {"mcp__github__list_prs": _definition_chars(loaded)}


def test_a_transcript_without_tool_search_keeps_nothing(tmp_path: Path):
    path = tmp_path / "session.jsonl"
    write_jsonl(path, [turn_line(message_id="msg_1")])

    result = parse_transcript(path, TranscriptMeta(path=str(path)))

    assert result.turns[0].deferred_tools_by_server == {}
    assert result.turns[0].deferred_list_chars == 0
    assert result.tool_definition_chars == {}


def test_only_names_and_sizes_are_kept_never_a_description_or_an_odd_name(tmp_path: Path):
    odd = ["../../etc/passwd", "name with spaces", "x" * 200]
    lines = [
        attachment_line("deferred_tools_delta", addedNames=["mcp__github__list_prs", *odd], removedNames=[]),
        attachment_line(
            "deferred_tools_record",
            entries=[_entry("mcp__github__list_prs", LONG_DESCRIPTION), _entry("../../etc/passwd"), "not an entry"],
        ),
        turn_line(message_id="msg_1"),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)

    result = parse_transcript(path, TranscriptMeta(path=str(path)))

    assert set(result.tool_definition_chars) == {"mcp__github__list_prs"}
    assert result.turns[0].deferred_tools_by_server == {}
    assert "pull requests of a repository" not in repr(result)


# -- the saving -----------------------------------------------------------------


def _turn(index: int, deferred: dict[str, int], *, read: int = 100_000, write: int = 0, list_chars: int = 0,
          tools: tuple[str, ...] = ()) -> Turn:
    return Turn(
        turn_index=index,
        model="claude-sonnet-5",
        input_tokens=10,
        cache_read_tokens=read,
        cache_creation_tokens=write,
        cc_5m=write,
        output_tokens=100,
        tool_names=tools,
        deferred_tools_by_server=deferred,
        deferred_list_chars=list_chars,
    )


def _result(*turns: Turn, definitions: dict[str, int] | None = None) -> TranscriptResult:
    return TranscriptResult(turns=list(turns), tool_definition_chars=definitions or {})


def test_deferred_tools_are_sized_by_their_own_server_or_a_typical_mcp_tool():
    # github's loaded definitions average 400 tokens; jira has none of its
    # own, and only two MCP tools were loaded, so it takes the typical 550.
    definitions = {"mcp__github__a": 1200, "mcp__github__b": 2000}
    stats = tool_search.compute_tool_search(
        [_result(_turn(1, {"github": 10, "jira": 5}), definitions=definitions)], PRICING
    )

    github, jira = stats.servers["github"], stats.servers["jira"]
    assert github.definition_tokens == pytest.approx(400) and github.own_sizes and github.measured == 2
    assert jira.definition_tokens == 550.0 and not jira.own_sizes and jira.measured == 0
    assert (github.sized_from, jira.sized_from) == ("its own tools", "typical MCP tool")
    assert stats.kept_tokens == pytest.approx(10 * 400 + 5 * 550)
    assert github.saving_usd == pytest.approx(10 * 400 * SONNET.cache_read / 1e6)
    assert stats.most_deferred == 15 and stats.most_deferred_mcp == 15


def test_the_typical_mcp_tool_is_the_mean_claude_code_counted_and_needs_no_calibration():
    found = Calibration(tool={"claude-sonnet-5": 2.0}, default_family="claude-sonnet-5")
    result = _upfront_result({"jira": 1}, loaded={"mcp__github__a": 1200})
    jira = tool_search.compute_tool_search([result], PRICING, calibration=found).servers["jira"]
    assert tool_search.DEFAULT_MCP_TOOL_TOKENS == 550.0 and tool_search.MIN_MCP_SAMPLE == 10
    assert jira.definition_tokens == 550.0 and jira.sized_from == "typical MCP tool"


def _upfront_result(deferred: dict[str, int], *, loaded=None, upfront=None, requested=()) -> TranscriptResult:
    return TranscriptResult(
        turns=[_turn(1, deferred)], tool_definition_chars=loaded or {}, upfront_tool_chars=upfront or {},
        upfront_request_tools=list(requested),
    )


def test_a_server_with_no_loaded_tool_is_sized_from_its_own_tools_sent_in_full_not_claude_codes_own():
    """Claude Code's own tools are far larger than an MCP tool's: one mixed
    into the average put the saving about 28% too high."""
    result = _upfront_result(
        {"docs": 5, "other": 3, "built-in": 2},
        loaded={"WebFetch": 4000},
        upfront={"mcp__docs__a": 400, "mcp__docs__b": 600},
    )

    stats = tool_search.compute_tool_search([result], PRICING)

    docs, other, built_in = stats.servers["docs"], stats.servers["other"], stats.servers["built-in"]
    assert (docs.measured, docs.definition_tokens, docs.sized_from) == (2, 125.0, "its own tools")
    assert docs.own_sizes
    assert (other.measured, other.definition_tokens, other.sized_from) == (0, 550.0, "typical MCP tool")
    assert not other.own_sizes
    assert (built_in.definition_tokens, built_in.sized_from) == (1000.0, "its own tools")
    # Every sample counts towards the window's figures, Claude Code's own too.
    assert stats.definitions_measured == 3
    assert stats.mean_definition_tokens == pytest.approx((400 + 600 + 4000) / 3 / 4)
    assert stats.kept_tokens == pytest.approx(5 * 125 + 3 * 550 + 2 * 1000)


def test_a_loaded_size_wins_over_the_size_it_was_sent_in_full_at():
    result = _upfront_result(
        {"docs": 1}, loaded={"mcp__docs__a": 800}, upfront={"mcp__docs__a": 400, "mcp__docs__b": 600}
    )
    docs = tool_search.compute_tool_search([result], PRICING).servers["docs"]
    assert docs.measured == 2 and docs.definition_tokens == pytest.approx((800 + 600) / 2 / 4)


def test_a_size_from_the_request_wins_over_a_larger_snapshot_size_of_the_same_tool_in_another_session():
    """A window holds sessions from before Claude Code logged the request:
    their snapshot sizes run about 1.5 times a request's, and the largest of
    both would have mixed the two scales into one average."""
    old = _upfront_result({"docs": 1}, upfront={"mcp__docs__a": 600, "mcp__other__b": 900})
    new = _upfront_result({"docs": 1}, upfront={"mcp__docs__a": 400}, requested=["mcp__docs__a"])

    sizes = tool_search._upfront_sizes([old, new])

    # docs a: the request's 400, not the snapshot's 600; other b: only the
    # snapshot has seen it, so its size stands.
    assert sizes == {"mcp__docs__a": 400, "mcp__other__b": 900}


def test_among_sizes_from_the_request_the_largest_wins_and_among_snapshot_sizes_too():
    requests = [
        _upfront_result({"docs": 1}, upfront={"mcp__docs__a": 380}, requested=["mcp__docs__a"]),
        _upfront_result({"docs": 1}, upfront={"mcp__docs__a": 420}, requested=["mcp__docs__a"]),
    ]
    snapshots = [_upfront_result({"docs": 1}, upfront={"mcp__docs__b": size}) for size in (500, 700, 600)]

    assert tool_search._upfront_sizes([*requests, *snapshots]) == {"mcp__docs__a": 420, "mcp__docs__b": 700}


def test_a_cached_digest_from_before_the_field_existed_keeps_the_largest_size_as_before():
    encoded = encode_result(TranscriptResult(upfront_tool_chars={"mcp__docs__a": 600}))
    del encoded["upfront_request_tools"]
    older = result_from_jsonable(encoded)

    assert older.upfront_request_tools == []
    assert tool_search._upfront_sizes([older, _upfront_result({"docs": 1}, upfront={"mcp__docs__a": 500})]) == {
        "mcp__docs__a": 600
    }


def test_a_servers_own_mean_is_at_the_request_scale_across_sessions():
    """A tool sent in full takes the snapshot's size only when no session
    sized it from the request, so the server's mean stays on one scale."""
    old = _upfront_result({"docs": 1}, upfront={"mcp__docs__a": 600, "mcp__docs__b": 600})
    new = _upfront_result(
        {"docs": 1, "other": 2}, upfront={"mcp__docs__a": 400, "mcp__docs__b": 400},
        requested=["mcp__docs__a", "mcp__docs__b"],
    )

    stats = tool_search.compute_tool_search([old, new], PRICING)

    assert stats.servers["docs"].definition_tokens == pytest.approx(100.0)
    assert stats.servers["docs"].sized_from == "its own tools"


def test_an_unmeasured_server_is_sized_at_the_typical_mcp_tool_not_another_servers_tools_sent_in_full():
    """Tools sent in full are a server's small always-load tools (terminal
    270 tokens against a real MCP tool's 550), so they don't stand for a
    server with none measured."""
    result = _upfront_result({"other": 3}, upfront={"mcp__docs__a": 400, "mcp__docs__b": 600})

    other = tool_search.compute_tool_search([result], PRICING).servers["other"]

    assert other.definition_tokens == 550.0 and other.sized_from == "typical MCP tool"
    assert not other.own_sizes and other.measured == 0


def test_ten_loaded_mcp_tools_on_other_servers_size_an_unmeasured_server():
    loaded = {f"mcp__docs__t{i}": 1200 for i in range(9)} | {"mcp__docs__big": 3000}
    result = _upfront_result(
        {"docs": 1, "other": 3}, loaded=loaded, upfront={"mcp__small__a": 40, "mcp__small__b": 40}
    )

    other = tool_search.compute_tool_search([result], PRICING).servers["other"]

    # Ten loaded tools (the sent-in-full ones don't count): (9 * 1200 + 3000) / 10 / 4.
    assert other.definition_tokens == pytest.approx((9 * 1200 + 3000) / 10 / 4)
    assert other.sized_from == "MCP tools loaded in this window" and not other.own_sizes


def test_nine_loaded_mcp_tools_are_too_few_and_claude_codes_own_never_count():
    loaded = {f"mcp__docs__t{i}": 1200 for i in range(9)} | {"WebFetch": 4000}
    other = tool_search.compute_tool_search([_upfront_result({"other": 3}, loaded=loaded)], PRICING).servers["other"]
    assert (other.definition_tokens, other.sized_from) == (550.0, "typical MCP tool")


def test_a_servers_own_tools_sent_in_full_still_size_it_when_others_are_loaded():
    loaded = {f"mcp__docs__t{i}": 1200 for i in range(10)}
    result = _upfront_result(
        {"small": 2, "other": 1}, loaded=loaded, upfront={"mcp__small__a": 400, "mcp__small__b": 600}
    )

    stats = tool_search.compute_tool_search([result], PRICING)

    small = stats.servers["small"]
    assert (small.measured, small.definition_tokens, small.sized_from) == (2, 125.0, "its own tools")
    assert stats.servers["other"].sized_from == "MCP tools loaded in this window"


def test_with_only_claude_codes_tools_measured_an_mcp_server_takes_the_typical_tool():
    result = _upfront_result({"docs": 2, "built-in": 1}, loaded={"WebFetch": 4000, "Bash": 2000})
    stats = tool_search.compute_tool_search([result], PRICING)
    assert stats.servers["docs"].definition_tokens == 550.0
    assert stats.servers["docs"].sized_from == "typical MCP tool"
    assert stats.servers["built-in"].sized_from == "its own tools"


def test_claude_codes_own_tools_with_none_measured_take_the_average_of_every_tool():
    result = _upfront_result({"built-in": 2}, upfront={"mcp__docs__a": 400, "mcp__docs__b": 600})
    row = tool_search.compute_tool_search([result], PRICING).servers["built-in"]
    assert row.definition_tokens == pytest.approx(125) and row.sized_from == "all servers"


def test_tools_sent_in_full_alone_are_enough_to_size_the_saving():
    result = _upfront_result({"docs": 4}, upfront={"mcp__docs__a": 400})
    stats = tool_search.compute_tool_search([result], PRICING)
    assert stats.measurable and stats.kept_tokens == pytest.approx(4 * 100)
    by_server = next(t for t in tool_search.build_section(stats).tables if t.name == "tool_search_by_server")
    column = [c.key for c in by_server.columns].index("sized_from")
    assert [row[column] for row in by_server.rows] == ["its own tools"]


def test_nothing_is_sized_from_when_nothing_was_measured():
    stats = tool_search.compute_tool_search([_upfront_result({"docs": 4})], PRICING)
    assert not stats.measurable and stats.servers["docs"].sized_from == ""
    by_server = next(t for t in tool_search.build_section(stats).tables if t.name == "tool_search_by_server")
    column = [c.key for c in by_server.columns].index("sized_from")
    assert [row[column] for row in by_server.rows] == [""]


def test_definitions_and_name_lists_use_the_calibrated_characters_per_token():
    """A definition is sized at the model's tool ratio and the name list at its
    text ratio; without a calibration both are 4.0."""
    found = Calibration(tool={"claude-sonnet-5": 5.0}, text={"claude-sonnet-5": 2.0}, default_family="claude-sonnet-5")
    definitions = {"mcp__github__a": 1200, "mcp__github__b": 2000}
    turn = _turn(1, {"github": 10}, list_chars=400)
    stats = tool_search.compute_tool_search([_result(turn, definitions=definitions)], PRICING, calibration=found)

    assert stats.mean_definition_tokens == pytest.approx(1600 / 5.0)
    assert stats.servers["github"].definition_tokens == pytest.approx(1600 / 5.0)
    assert stats.list_tokens == pytest.approx(400 / 2.0)
    assert stats.kept_tokens == pytest.approx(10 * 1600 / 5.0)

    plain = tool_search.compute_tool_search([_result(turn, definitions=definitions)], PRICING)
    assert plain.mean_definition_tokens == pytest.approx(1600 / 4.0)
    assert plain.list_tokens == pytest.approx(400 / 4.0)


def test_a_reply_that_rebuilt_the_cache_prices_them_as_a_cache_write():
    definitions = {"mcp__github__a": 4000}
    stats = tool_search.compute_tool_search(
        [_result(_turn(1, {"github": 1}, read=0, write=50_000), definitions=definitions)], PRICING
    )
    assert stats.gross_usd == pytest.approx(1000 * SONNET.cache_write_5m / 1e6)


def test_the_name_list_and_search_only_replies_are_taken_off():
    definitions = {"mcp__github__a": 4000}
    search = _turn(2, {"github": 1}, tools=("ToolSearch",))
    both = _turn(3, {"github": 1}, tools=("ToolSearch", "Bash"))
    stats = tool_search.compute_tool_search(
        [_result(_turn(1, {"github": 1}, list_chars=400), search, both, definitions=definitions)], PRICING
    )

    search_cost = price_turn(search, PRICING.resolve_model(search.model)).total
    assert stats.search_replies == 1
    assert stats.search_usd == pytest.approx(search_cost)
    assert stats.list_usd == pytest.approx(100 * SONNET.cache_read / 1e6)
    assert stats.net_usd == pytest.approx(stats.gross_usd - stats.list_usd - stats.search_usd)
    assert stats.replies == 3 and stats.all_replies == 3


def test_nothing_is_priced_when_no_definition_was_loaded():
    stats = tool_search.compute_tool_search([_result(_turn(1, {"github": 40}))], PRICING)
    assert not stats.measurable
    assert stats.gross_usd == 0 and stats.kept_tokens == 0
    section = tool_search.build_section(stats)
    summary = {c.key: v for c, v in zip(section.tables[0].columns, section.tables[0].rows[0])}
    assert summary["replies"] == 1 and summary["most_deferred"] == 40
    assert summary["net_usd"] is None and summary["kept_per_reply"] is None
    assert any("isn't worked out" in note for note in section.notes)


def test_section_lists_servers_by_saving():
    definitions = {"mcp__github__a": 4000, "mcp__jira__b": 400}
    stats = tool_search.compute_tool_search(
        [_result(_turn(1, {"github": 2, "jira": 2, "built-in": 1}), definitions=definitions)], PRICING
    )
    section = tool_search.build_section(stats)
    assert section.key == "tool_search"
    names = [t.name for t in section.tables]
    assert names == ["tool_search_summary", "tool_search_by_server", "tool_search_servers"]
    servers = [row[0] for row in section.tables[1].rows]
    assert servers[0] == "github"
    sized_from = {row[0]: row[4] for row in section.tables[1].rows}
    assert sized_from == {"github": "its own tools", "jira": "its own tools", "built-in": "all servers"}


# -- the check ----------------------------------------------------------------


def _table(name: str, rows: list[dict]):
    keys = list(rows[0]) if rows else []
    return NS(name=name, columns=[NS(key=k) for k in keys], rows=[[row[k] for k in keys] for row in rows])


def _ctx(tmp_path, sections):
    return qa.Context(
        model=NS(sections=sections, recommendations=[]),
        units=Units(billing_mode="api", currency="USD"),
        period="over the last 14 days",
        config_dir=tmp_path,
        effective={},
        effective_agents={},
    )


def _summary(**values):
    row = {"scope": "all replies", "replies": 0, "most_deferred": 0, "most_deferred_mcp": 0, "kept_per_reply": None,
           "net_usd": None}
    row.update(values)
    return _table("tool_search_summary", [row])


def test_check_reports_the_saving_by_server(tmp_path):
    servers = _table("tool_search_by_server", [
        {"server": "github", "most_deferred": 56, "kept_per_reply": 18_800.0, "saving_usd": 2.2},
        {"server": "built-in", "most_deferred": 32, "kept_per_reply": 15_500.0, "saving_usd": 1.8},
    ])
    section = NS(key="tool_search", tables=[
        _summary(replies=500, most_deferred=154, most_deferred_mcp=122, kept_per_reply=57_500.0, net_usd=5.58),
        servers,
    ])

    result = qa.run("tool-search", _ctx(tmp_path, [section]))

    assert result["status"] == "ok"
    assert "57,500 tokens" in result["summary"] and "122 of them MCP tools" in result["summary"]
    assert [row[0] for row in result["table"]["rows"]] == ["github", "Claude Code's own tools"]


def test_check_has_no_data_without_tool_search_or_loaded_definitions(tmp_path):
    none = qa.run("tool-search", _ctx(tmp_path, []))
    assert none["status"] == "no_data"
    unsized = qa.run("tool-search", _ctx(tmp_path, [NS(key="tool_search", tables=[_summary(replies=3,
                                                                                         most_deferred=40)])]))
    assert unsized["status"] == "no_data" and "40 tools" in unsized["summary"]


# -- each MCP server: parsing ---------------------------------------------------


def test_mcp_name_puts_every_raw_form_under_its_tools_prefix():
    assert mcp_name("Claude Browser") == "Claude_Browser"
    assert mcp_name("plugin:playwright:playwright") == "plugin_playwright_playwright"
    assert mcp_name("claude.ai Claude Docs") == "claude_ai_Claude_Docs"
    assert mcp_name("github") == "github"
    assert len(mcp_name("x" * 100)) == 64
    assert tool_server(f"mcp__{mcp_name('Claude Browser')}__open") == "Claude_Browser"


def test_each_reply_keeps_what_each_mcp_server_sends_by_length_only(tmp_path: Path):
    names = ["mcp__github__list_prs", "mcp__github__merge_pr", "mcp__jira__search", "WebFetch"]
    long_tool = "mcp__srv__" + "t" * 100
    docs_text = "Use the docs server to look things up. " * 20
    upfront = {"name": "mcp__Claude_Browser__open", "description": "Opens a page.", "schema": {"type": "object"}}
    lines = [
        attachment_line(
            "deferred_tools_delta", addedNames=[*names, long_tool], addedLines=[*names, long_tool], removedNames=[],
            surfacedNames=["mcp__jira__search"], needsAuthMcpServers=["plugin:engineering:slack"],
            failedMcpServers=["broken server"], pendingMcpServers=["slow one"],
        ),
        attachment_line(
            "mcp_instructions_delta", addedNames=["claude.ai Claude Docs", "plugin:playwright:playwright"],
            addedBlocks=[docs_text, "Drive the browser."], removedNames=[],
        ),
        attachment_line("prompt_snapshot", tools=[upfront, {"name": "Bash", "description": "Runs.", "schema": {}}]),
        turn_line(message_id="msg_1", content=[
            tool_use_block("ReadMcpResourceTool", "toolu_1", {"server": "claude.ai Claude Docs", "uri": "docs://a"}),
        ]),
        attachment_line("mcp_instructions_delta", addedNames=[], addedBlocks=[],
                        removedNames=["plugin:playwright:playwright"]),
        turn_line(message_id="msg_2"),
        system_line("compact_boundary"),
        attachment_line("mcp_instructions_delta", addedNames=["plugin:playwright:playwright"],
                        addedBlocks=["Drive it again."], removedNames=[]),
        turn_line(message_id="msg_3"),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)

    result = parse_transcript(path, TranscriptMeta(path=str(path), kind="top-level", session_id="s"))

    first, second, third = result.turns
    # A surfaced tool is sent in full: on the name list, not deferred.
    assert first.deferred_tools_by_server == {"github": 2, "srv": 1, "built-in": 1}
    assert first.deferred_list_chars_by_server == {
        "github": len(names[0]) + 1 + len(names[1]) + 1,
        "jira": len(names[2]) + 1,
        "built-in": len(names[3]) + 1,
        "srv": len(long_tool) + 1,
    }
    assert first.deferred_list_chars == sum(first.deferred_list_chars_by_server.values())
    assert first.mcp_instruction_chars_by_server == {
        "claude_ai_Claude_Docs": len(docs_text), "plugin_playwright_playwright": len("Drive the browser."),
    }
    assert first.mcp_resource_servers == {"claude_ai_Claude_Docs": 1}
    assert second.mcp_instruction_chars_by_server == {"claude_ai_Claude_Docs": len(docs_text)}
    assert third.mcp_instruction_chars_by_server == {
        "claude_ai_Claude_Docs": len(docs_text), "plugin_playwright_playwright": len("Drive it again."),
    }
    assert result.upfront_definition_chars_by_server == {
        "Claude_Browser": len(json.dumps(upfront, ensure_ascii=False, separators=(",", ":"))),
    }
    assert result.mcp_connection_status == {
        "plugin_engineering_slack": "needs sign-in", "broken_server": "failed to connect", "slow_one": "pending",
    }
    assert result.mcp_tool_suffixes_by_server == {
        "github": ["list_prs", "merge_pr"], "jira": ["search"], "srv": ["t" * 64], "Claude_Browser": ["open"],
    }
    # Lengths, never the text.
    assert "look things up" not in repr(result) and "Drive the browser" not in repr(result)
    assert result_from_jsonable(json.loads(json.dumps(encode_result(result)))) == result


def test_each_mcp_tool_sent_in_full_keeps_its_own_size(tmp_path: Path):
    a = {"name": "mcp__docs__a", "description": "Finds a page.", "schema": {"type": "object"}}
    b = {"name": "mcp__docs__b", "description": "Reads a page. " * 10, "schema": {"type": "object", "x": 1}}
    smaller_b = {"name": "mcp__docs__b", "description": "Reads.", "schema": {}}
    lines = [
        attachment_line("prompt_snapshot", tools=[a, b, {"name": "Bash", "description": "Runs.", "schema": {}}]),
        turn_line(message_id="msg_1"),
        attachment_line("prompt_snapshot", tools=[smaller_b]),
        turn_line(message_id="msg_2"),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)

    result = parse_transcript(path, TranscriptMeta(path=str(path), kind="top-level", session_id="s"))

    def chars(entry: dict) -> int:
        return len(json.dumps(entry, ensure_ascii=False, separators=(",", ":")))

    # The name, description and schema as compact JSON; the largest sighting
    # of a tool wins, and a built-in tool isn't an MCP tool.
    assert result.upfront_tool_chars == {"mcp__docs__a": chars(a), "mcp__docs__b": chars(b)}
    assert result.upfront_definition_chars_by_server == {"docs": chars(a) + chars(b)}
    assert "Finds a page" not in repr(result.upfront_tool_chars)
    # No request was logged, so none of those sizes is a request's.
    assert result.upfront_request_tools == []


def _request_chars(tool: dict) -> int:
    """A definition as the request sent it: name, description and
    ``input_schema``, the keys a ``deferred_tools_record`` is measured on."""
    definition = {key: tool.get(key) for key in ("name", "description", "input_schema")}
    return len(json.dumps(definition, ensure_ascii=False, separators=(",", ":")))


def _snapshot_and_request(tmp_path: Path, snapshot_tools: list[dict], *shapes: list[dict], shape_first: bool = False):
    """A transcript whose ``prompt_snapshot`` lists ``snapshot_tools`` (the
    snapshot runs about 1.6 times the request) and which logged one
    ``api-request-shape`` line per list in ``shapes``."""
    shape_lines = [ignorable_line("api-request-shape", shape={"tools": tools}) for tools in shapes]
    snapshot = attachment_line("prompt_snapshot", tools=snapshot_tools)
    lines = [*shape_lines, snapshot, turn_line(message_id="msg_1")] if shape_first else [
        snapshot, *shape_lines, turn_line(message_id="msg_1"),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    return parse_transcript(path, TranscriptMeta(path=str(path), kind="top-level", session_id="s"))


def test_an_mcp_tool_sent_in_full_is_sized_from_the_request_not_the_larger_snapshot(tmp_path: Path):
    """Claude Code's snapshot entries run about 1.6 times the definitions it
    sends, and a loaded definition is measured at the request's scale: mixing
    the two put one server's average a quarter too high."""
    sent_a = {"name": "mcp__docs__a", "description": "Finds a page.", "input_schema": {"type": "object"}}
    sent_b = {"name": "mcp__docs__b", "description": "Reads a page.", "input_schema": {"type": "object"}}
    snapshot_a = {"name": "mcp__docs__a", "description": "Finds a page. " * 20, "schema": {"type": "object"}}
    snapshot_b = {"name": "mcp__docs__b", "description": "Reads a page. " * 20, "schema": {"type": "object"}}

    result = _snapshot_and_request(tmp_path, [snapshot_a, snapshot_b], [sent_a, sent_b])

    assert result.upfront_tool_chars == {"mcp__docs__a": _request_chars(sent_a), "mcp__docs__b": _request_chars(sent_b)}
    assert result.upfront_definition_chars_by_server == {"docs": _request_chars(sent_a) + _request_chars(sent_b)}
    assert result.upfront_request_tools == ["mcp__docs__a", "mcp__docs__b"]
    snapshot_total = sum(len(json.dumps(t, ensure_ascii=False, separators=(",", ":"))) for t in (snapshot_a, snapshot_b))
    assert snapshot_total > 1.5 * result.upfront_definition_chars_by_server["docs"]
    # Sizes only: no description reaches the result.
    assert "Finds a page" not in repr(result.upfront_tool_chars)
    assert "Finds a page" not in repr(result.upfront_definition_chars_by_server)


@pytest.mark.parametrize("shape_first", [True, False])
def test_the_request_size_wins_whichever_line_comes_first(tmp_path: Path, shape_first: bool):
    sent = {"name": "mcp__docs__a", "description": "Finds a page.", "input_schema": {"type": "object"}}
    snapshot = {"name": "mcp__docs__a", "description": "Finds a page. " * 20, "schema": {"type": "object"}}

    result = _snapshot_and_request(tmp_path, [snapshot], [sent], shape_first=shape_first)

    assert result.upfront_tool_chars == {"mcp__docs__a": _request_chars(sent)}
    assert result.upfront_definition_chars_by_server == {"docs": _request_chars(sent)}


def test_a_tool_the_request_does_not_list_keeps_its_snapshot_size(tmp_path: Path):
    sent = {"name": "mcp__docs__a", "description": "Finds a page.", "input_schema": {"type": "object"}}
    snapshot_a = {"name": "mcp__docs__a", "description": "Finds a page. " * 20, "schema": {"type": "object"}}
    snapshot_b = {"name": "mcp__docs__b", "description": "Reads a page.", "schema": {"type": "object"}}
    snapshot_other = {"name": "mcp__other__c", "description": "Does a thing.", "schema": {"type": "object"}}

    result = _snapshot_and_request(tmp_path, [snapshot_a, snapshot_b, snapshot_other], [sent])

    def snapshot_chars(tool: dict) -> int:
        return len(json.dumps({k: tool.get(k) for k in ("name", "description", "schema")}, separators=(",", ":")))

    assert result.upfront_tool_chars == {
        "mcp__docs__a": _request_chars(sent),
        "mcp__docs__b": snapshot_chars(snapshot_b),
        "mcp__other__c": snapshot_chars(snapshot_other),
    }
    # Only the tool the request listed is marked as sized from it.
    assert result.upfront_request_tools == ["mcp__docs__a"]
    # A server's total is its tools' sizes added up, wherever each came from;
    # one the request never mentions keeps the snapshot's total.
    assert result.upfront_definition_chars_by_server == {
        "docs": _request_chars(sent) + snapshot_chars(snapshot_b),
        "other": snapshot_chars(snapshot_other),
    }


def test_the_largest_request_size_of_a_tool_wins_and_a_deferred_entry_is_not_sent_in_full(tmp_path: Path):
    small = {"name": "mcp__docs__a", "description": "Finds.", "input_schema": {"type": "object"}}
    large = {"name": "mcp__docs__a", "description": "Finds a page. " * 10, "input_schema": {"type": "object"}}
    deferred = {"name": "mcp__docs__b", "description": "Reads a page. " * 30, "input_schema": {}, "defer_loading": True}
    built_in = {"name": "Bash", "description": "Runs.", "input_schema": {}}

    result = _snapshot_and_request(
        tmp_path, [], [small, deferred, built_in, "not a tool", {"name": "../../x"}], [large]
    )

    assert result.upfront_tool_chars == {"mcp__docs__a": _request_chars(large)}
    assert result.upfront_definition_chars_by_server == {"docs": _request_chars(large)}
    assert result.mcp_tool_suffixes_by_server == {"docs": ["a"]}


def test_a_request_line_with_no_snapshot_still_sizes_the_mcp_tools_it_sent(tmp_path: Path):
    sent = {"name": "mcp__docs__a", "description": "Finds a page.", "input_schema": {"type": "object"}}
    path = tmp_path / "session.jsonl"
    write_jsonl(path, [ignorable_line("api-request-shape", shape={"tools": [sent]}), turn_line(message_id="msg_1")])

    result = parse_transcript(path, TranscriptMeta(path=str(path), kind="top-level", session_id="s"))

    assert result.upfront_tool_chars == {"mcp__docs__a": _request_chars(sent)}
    assert result.upfront_definition_chars_by_server == {"docs": _request_chars(sent)}


def test_a_connection_problem_clears_once_the_server_connects(tmp_path: Path):
    lines = [
        attachment_line(
            "deferred_tools_delta", addedNames=[], addedLines=[], removedNames=[],
            pendingMcpServers=["plugin:playwright:playwright"], needsAuthMcpServers=["claude.ai Notes"],
            failedMcpServers=["broken"],
        ),
        turn_line(message_id="msg_1"),
        attachment_line(
            "deferred_tools_delta", addedNames=["mcp__plugin_playwright_playwright__browser_click"],
            addedLines=["mcp__plugin_playwright_playwright__browser_click"], removedNames=[],
        ),
        attachment_line("mcp_instructions_delta", addedNames=["claude.ai Notes"], addedBlocks=["Notes."],
                        removedNames=[]),
        turn_line(message_id="msg_2"),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)

    result = parse_transcript(path, TranscriptMeta(path=str(path), kind="top-level", session_id="s"))

    # Its tools or its instructions arrived, so it connected in the end.
    assert result.mcp_connection_status == {"broken": "failed to connect"}


# -- each MCP server: what it is, whether it's used, what it cost ----------------


END = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
#: What one character at the front of a Sonnet reply that read the cache costs.
PER_CHAR = SONNET.cache_read / 1e6 / 4
#: Instruction characters that cost $0.20 a reply, so ten replies pass every bar.
BIG = int(0.2 / PER_CHAR)
ID = "0a1b2c3d-1111-4222-8333-444455556666"


def _ts(days: float) -> str:
    return (END - timedelta(days=days)).isoformat().replace("+00:00", "Z")


def _reply(days: float = 0.0, *, lists=None, instructions=None, calls=None, attribution=None, commands=(),
           resources=None, redirects=None) -> Turn:
    return Turn(
        turn_index=1,
        ts=_ts(days),
        model="claude-sonnet-5",
        input_tokens=10,
        cache_read_tokens=100_000,
        output_tokens=100,
        deferred_list_chars_by_server=lists or {},
        mcp_instruction_chars_by_server=instructions or {},
        tool_calls_by_tool=calls or {},
        attribution_mcp_server=attribution,
        commands_run=tuple(commands),
        mcp_resource_servers=resources or {},
        saver_redirects=redirects or {},
    )


def _session(session_id: str, *turns: Turn, kind: str = "top-level", slug: str = "proj", tools=None, upfront=None,
             status=None, entrypoint: str | None = None) -> TranscriptResult:
    return TranscriptResult(
        meta=TranscriptMeta(
            path=f"{session_id}.jsonl", kind=kind, session_id=session_id, project_slug=slug, entrypoint=entrypoint
        ),
        turns=list(turns),
        mcp_tool_suffixes_by_server=tools or {},
        upfront_definition_chars_by_server=upfront or {},
        mcp_connection_status=status or {},
    )


def _unused(server: str, sessions: int = 10, *, first: float = 9.0, last: float = 0.0, chars: int = BIG,
            **session_kw) -> list[TranscriptResult]:
    """``sessions`` main sessions offering ``server``'s instructions, from
    ``first`` to ``last`` days before the end, and never using it."""
    step = (first - last) / max(sessions - 1, 1)
    return [
        _session(f"{server}-{i}", _reply(first - i * step, instructions={server: chars}), **session_kw)
        for i in range(sessions)
    ]


#: A reply at the very end of the window, offering nothing.
CLOCK = _session("clock", _reply(0.0))


def _stats(results, *, snapshots=None, all_projects=True, thresholds=None) -> tool_search.ToolSearchStats:
    return tool_search.compute_tool_search(
        [*results, CLOCK], PRICING, thresholds, snapshots=snapshots, all_projects=all_projects
    )


def _rows(results, **kw) -> dict[str, tool_search.McpServerRow]:
    return {row.server: row for row in _stats(results, **kw).mcp_servers}


def _snapshot(slug: str = "proj", **data) -> Snapshot:
    return Snapshot(path=None, ts="2026-09-30T00:00:00Z", data={"project_slug": snapshot_project_key(slug), **data})


def _report(stats: tool_search.ToolSearchStats) -> ReportModel:
    return ReportModel(
        meta=ReportMeta(pricing=PricingMeta(coverage_pct=100.0)),
        sections=[tool_search.build_section(stats)],
        diagnostics=Diagnostics(lines=1000),
    )


def _cards(results, **kw) -> list[Recommendation]:
    return tool_search.RULES[0](_report(_stats(results, **kw)), tool_search.ToolSearchThresholds())


def test_a_server_offered_in_many_main_sessions_and_never_used_is_flagged():
    row = _rows(_unused("claude_ai_Notes"))["claude_ai_Notes"]
    assert row.kind == tool_search.KIND_CONNECTOR and row.status == tool_search.STATUS_REMOVE
    assert row.main_sessions == 10 and row.subagent_runs == 0 and row.uses == 0
    assert row.first_seen_days == pytest.approx(9) and row.last_seen_days == pytest.approx(0)
    assert row.instructions_usd == pytest.approx(10 * BIG * PER_CHAR)


@pytest.mark.parametrize("kw,status", [
    ({"sessions": 10}, tool_search.STATUS_REMOVE),
    ({"sessions": 9}, tool_search.STATUS_UNUSED),
    ({"first": 7.0}, tool_search.STATUS_REMOVE),
    ({"first": 6.5}, tool_search.STATUS_UNUSED),
    ({"first": 12.0, "last": 3.0}, tool_search.STATUS_REMOVE),
    ({"first": 12.0, "last": 3.5}, tool_search.STATUS_UNUSED),
    ({"chars": int(0.0501 / PER_CHAR)}, tool_search.STATUS_REMOVE),
    ({"chars": int(0.049 / PER_CHAR)}, tool_search.STATUS_UNUSED),
])
def test_each_bar_for_flagging_a_server(kw, status):
    assert _rows(_unused("claude_ai_Notes", **kw))["claude_ai_Notes"].status == status


def test_the_card_needs_a_dollar_in_all():
    cheap = int(0.06 / PER_CHAR)
    assert _rows(_unused("claude_ai_A", chars=cheap))["claude_ai_A"].status == tool_search.STATUS_REMOVE
    assert _cards(_unused("claude_ai_A", chars=cheap)) == []
    (card,) = _cards(_unused("claude_ai_A", chars=cheap) + _unused("claude_ai_B", chars=cheap))
    assert card.saving_usd == pytest.approx(20 * cheap * PER_CHAR)


def test_a_server_costing_exactly_the_bar_is_flagged():
    cost = _rows(_unused("claude_ai_Notes"))["claude_ai_Notes"].removable_usd
    at_the_bar = tool_search.ToolSearchThresholds(unused_min_server_usd=cost)
    row = _rows(_unused("claude_ai_Notes"), thresholds=at_the_bar)["claude_ai_Notes"]
    assert row.status == tool_search.STATUS_REMOVE


def test_a_card_totalling_exactly_the_bar_is_shown():
    stats = _stats(_unused("claude_ai_A") + _unused("claude_ai_B"))
    (card,) = tool_search.RULES[0](_report(stats), tool_search.ToolSearchThresholds())
    at_the_bar = tool_search.ToolSearchThresholds(unused_min_total_usd=card.saving_usd)
    assert len(tool_search.RULES[0](_report(stats), at_the_bar)) == 1


def test_one_server_reads_in_the_singular():
    (card,) = _cards(_unused("claude_ai_Notes"))
    assert card.title == "Notes is an MCP server you haven't used in 9 days"
    assert card.why == (
        "Notes was offered in up to 10 main sessions over the last 9 days, and Claude didn't use it. "
        "Every reply still carried its tool names, instructions or tools."
    )
    assert card.action.startswith("Notes: run /mcp and disable it ")


def test_the_card_says_how_far_back_it_looked():
    (card,) = _cards(_unused("claude_ai_Notes", first=13.0))
    assert card.title == "Notes is an MCP server you haven't used in 13 days"
    assert "over the last 13 days" in card.why
    assert "looked back only 13 days" in card.action
    # Before the line about when the saving starts.
    assert card.action.index("looked back only") < card.action.index("The saving starts")


def test_several_servers_say_the_least_that_any_of_them_was_looked_at():
    (card,) = _cards(_unused("claude_ai_A", first=13.0) + _unused("claude_ai_B", first=9.0, chars=BIG * 2))
    assert card.title == "2 MCP servers you haven't used in 9 days are loaded into your sessions"
    assert "over the last 9 days" in card.why and "looked back only 9 days" in card.action


def test_a_window_a_day_deep_reads_in_the_singular():
    thresholds = tool_search.ToolSearchThresholds(unused_min_age_days=0.5)
    (card,) = _cards(_unused("claude_ai_Notes", first=1.2), thresholds=thresholds)
    assert card.title == "Notes is an MCP server you haven't used in 1 day"
    assert "over the last 1 day," in card.why and "looked back only 1 day," in card.action


def _carried(server: str, first: float, last: float) -> TranscriptResult:
    """A session that started ``first`` days before the end and ended
    ``last`` days before it, offering ``server`` throughout. The window took
    it for its last reply and so brought its earlier turns too."""
    return _session("carried", _reply(first, instructions={server: BIG}), _reply(last, instructions={server: BIG}))


def test_a_session_that_started_before_the_window_does_not_stretch_the_look_back():
    """Sessions that ended in the days before the window aren't in the
    corpus, so a use in them can't be seen: 17 days is only how far one
    long session reached back, and the card may claim only what every reply
    was seen for."""
    results = [*_unused("claude_ai_Notes", first=9.0), _carried("claude_ai_Notes", first=17.0, last=8.0)]

    row = _rows(results)["claude_ai_Notes"]
    (card,) = _cards(results)

    assert row.first_seen_days == pytest.approx(9)
    assert card.title == "Notes is an MCP server you haven't used in 9 days"
    assert "over the last 9 days" in card.why
    assert "looked back only 9 days" in card.action


def test_a_server_seen_only_in_a_session_that_started_before_the_window_is_not_old_enough_to_flag():
    """The age bar counts the days every reply was seen, not the days one
    session spans: with five such days, ten sessions in them and one that
    began ten days before the end, a server hasn't been offered for a week."""
    results = [*_unused("claude_ai_Notes", first=5.0), _carried("claude_ai_Notes", first=10.0, last=4.0)]

    row = _rows(results)["claude_ai_Notes"]

    assert row.first_seen_days == pytest.approx(5)
    assert row.status == tool_search.STATUS_UNUSED
    assert _cards(results) == []


def test_a_subagents_later_reply_makes_its_session_end_later():
    """A session is in the window by its last reply across the main
    transcript and its subagents: here the main transcript stopped twelve
    days before the end but a subagent ran until two, so the session says
    nothing about the days between."""
    main = _session("long", _reply(12.0, instructions={"claude_ai_Notes": BIG}))
    later = _session("long", _reply(2.0), kind="subagent")
    results = [*_unused("claude_ai_Notes", first=9.0), main, later]

    row = _rows(results)["claude_ai_Notes"]

    assert row.first_seen_days == pytest.approx(9)


def test_a_window_with_no_main_session_keeps_the_first_offer_as_it_was():
    subagents = [
        _session(f"sub-{i}", _reply(9.0 - i, instructions={"claude_ai_Notes": BIG}), kind="subagent")
        for i in range(3)
    ]

    (row,) = tool_search.compute_tool_search(subagents, PRICING, all_projects=True).mcp_servers

    # Measured from the last of its replies, seven days before END.
    assert row.first_seen_days == pytest.approx(2)


def test_the_thresholds_say_the_window_is_what_was_looked_at():
    text = " ".join(tool_search.ToolSearchThresholds().describe())
    assert "didn't use it in the window" in text and "never used" not in text


def test_every_kind_from_its_own_evidence():
    snap = _snapshot(
        claude_json={"mcp_servers": ["sentry"]},
        content_layers={"mcp_json": {"names": ["linear"]}, "managed_mcp": {"names": ["corp"]}},
        mcp_servers={"names": ["github", "corp", "<redacted:1>"]},
    )
    results = [
        r for server in ("claude_ai_Notes", "plugin_x_y", "sentry", "linear", "github", "corp", "mystery", ID)
        for r in _unused(server)
    ]
    rows = _rows(results, snapshots=[snap])
    kinds = {server: row.kind for server, row in rows.items()}
    assert kinds == {
        "claude_ai_Notes": tool_search.KIND_CONNECTOR,
        "plugin_x_y": tool_search.KIND_PLUGIN,
        "sentry": tool_search.KIND_LOCAL,
        "linear": tool_search.KIND_PROJECT,
        "github": tool_search.KIND_USER,
        "corp": tool_search.KIND_MANAGED,
        "mystery": tool_search.KIND_UNKNOWN,
        ID: tool_search.KIND_UNKNOWN,
    }
    assert rows["sentry"].config_name == "sentry" and rows["sentry"].projects == ("proj",)
    assert rows["corp"].status == tool_search.STATUS_MANAGED
    assert rows["mystery"].status == rows[ID].status == tool_search.STATUS_UNKNOWN
    card_servers = {e[3] for card in _cards(results, snapshots=[snap]) for e in card.evidence}
    assert card_servers == {"claude_ai_Notes", "plugin_x_y", "sentry", "linear", "github"}


def test_with_managed_servers_set_an_unmatched_config_server_may_be_the_organisations():
    snap = _snapshot(mcp_servers={"names": ["github"]}, managed_keys=["managedMcpServers"])
    row = _rows(_unused("github"), snapshots=[snap])["github"]
    assert row.kind == tool_search.KIND_MANAGED and row.status == tool_search.STATUS_MANAGED


def test_a_clipped_config_name_finds_its_server():
    snap = _snapshot(mcp_servers={"names": ["very-long-server-na..."]})
    rows = _rows(_unused("very-long-server-name"), snapshots=[snap])
    assert set(rows) == {"very-long-server-name"}
    assert rows["very-long-server-name"].kind == tool_search.KIND_USER


def test_a_snapshot_filed_under_either_drive_letter_spelling_names_its_project():
    """The config hook filed these under the lower-case drive's hash before
    it upper-cased the letter; the folder is ``c--Dev-x`` either way."""
    canonical, legacy = snapshot_project_keys("c--Dev-x")
    for key in (canonical, legacy):
        snap = Snapshot(
            path=None, ts="2026-09-30T00:00:00Z",
            data={"project_slug": key, "content_layers": {"mcp_json": {"names": ["linear"]}}},
        )
        rows = _rows(_unused("linear", slug="c--Dev-x"), snapshots=[snap])
        assert rows["linear"].kind == tool_search.KIND_PROJECT
        assert rows["linear"].projects == ("c--Dev-x",)


def test_a_project_with_snapshots_under_both_keys_is_read_from_its_newest_one():
    canonical, legacy = snapshot_project_keys("c--Dev-x")
    older = Snapshot(
        path=None, ts="2026-09-20T00:00:00Z",
        data={"project_slug": legacy, "claude_json": {"mcp_servers": ["sentry"]}},
    )
    newer = Snapshot(path=None, ts="2026-09-25T00:00:00Z", data={"project_slug": canonical})
    results = _unused("sentry", slug="c--Dev-x")
    # The newer row no longer configures it, so the older one's name is stale.
    for snaps in ([older, newer], [newer, older]):
        assert _rows(results, snapshots=snaps)["sentry"].kind == tool_search.KIND_UNKNOWN
    assert _rows(results, snapshots=[older])["sentry"].kind == tool_search.KIND_LOCAL
    # Another project's newer snapshot says nothing about this one's.
    other = Snapshot(path=None, ts="2026-09-28T00:00:00Z", data={"project_slug": snapshot_project_key("C--Dev-y")})
    assert _rows(results, snapshots=[older, other])["sentry"].kind == tool_search.KIND_LOCAL


def test_a_snapshot_with_no_project_still_names_its_servers():
    snap = Snapshot(path=None, ts="2026-09-30T00:00:00Z", data={"claude_json": {"mcp_servers": ["sentry"]}})
    assert _rows(_unused("sentry"), snapshots=[snap])["sentry"].kind == tool_search.KIND_LOCAL


def test_an_account_wide_server_is_judged_only_across_every_project():
    results = _unused("claude_ai_Notes") + _unused("sentry")
    snap = _snapshot(claude_json={"mcp_servers": ["sentry"]})
    rows = _rows(results, snapshots=[snap], all_projects=False)
    assert rows["claude_ai_Notes"].status == tool_search.STATUS_ALL_PROJECTS
    assert rows["sentry"].status == tool_search.STATUS_REMOVE


def test_a_desktop_id_without_its_claude_ai_name_is_judged_only_across_every_project():
    """Its claude.ai name, which says it is a connector, may only show up in
    another project's sessions."""
    rows = _rows(_unused(ID) + _unused("other-server"), all_projects=False)
    assert rows[ID].status == tool_search.STATUS_ALL_PROJECTS
    assert rows["other-server"].status == tool_search.STATUS_UNKNOWN
    assert _rows(_unused(ID))[ID].status == tool_search.STATUS_UNKNOWN


# -- the desktop app: connectors by ID, and the servers it brings itself ----------

DESKTOP = "claude-desktop"


def test_an_id_named_server_only_the_desktop_app_ever_named_is_a_desktop_connector():
    row = _rows(_unused(ID, entrypoint=DESKTOP))[ID]
    assert row.kind == tool_search.KIND_DESKTOP_CONNECTOR and row.status == tool_search.STATUS_REMOVE
    assert row.aliases == ()
    (card,) = _cards(_unused(ID, entrypoint=DESKTOP))
    assert "+ > Connectors" in card.action and "claude.ai/customize/connectors" in card.action
    assert card.title == "Connector 0a1b2c3d is an MCP server you haven't used in 9 days"
    assert card.subject == ID


def test_a_desktop_connector_by_id_keeps_the_remove_bars_and_the_all_projects_rule():
    # The same thresholds as a named connector.
    assert _rows(_unused(ID, sessions=9, entrypoint=DESKTOP))[ID].status == tool_search.STATUS_UNUSED
    assert _rows(_unused(ID, first=6.5, entrypoint=DESKTOP))[ID].status == tool_search.STATUS_UNUSED
    # Removing it changes every project, so one project's view can't judge.
    assert _rows(_unused(ID, entrypoint=DESKTOP), all_projects=False)[ID].status == tool_search.STATUS_ALL_PROJECTS
    # A use counts.
    used = _unused(ID, entrypoint=DESKTOP) + [
        _session("u", _reply(calls={f"mcp__{ID}__find": 1}), entrypoint=DESKTOP)
    ]
    assert _rows(used)[ID].status == tool_search.STATUS_USED


def test_an_id_named_server_a_terminal_session_also_named_stays_of_unknown_kind():
    results = _unused(ID, entrypoint=DESKTOP) + [_session("cli", _reply(instructions={ID: BIG}), entrypoint="cli")]
    row = _rows(results)[ID]
    assert row.kind == tool_search.KIND_UNKNOWN and row.status == tool_search.STATUS_UNKNOWN
    # And one the transcripts never gave an entrypoint for is not taken for the desktop app's.
    assert _rows(_unused(ID, entrypoint=None))[ID].kind == tool_search.KIND_UNKNOWN


def test_a_subagent_run_in_the_desktop_app_counts_as_the_desktop_app():
    results = _unused(ID, kind="subagent", entrypoint=DESKTOP)
    row = _rows(results)[ID]
    assert row.kind == tool_search.KIND_DESKTOP_CONNECTOR and row.status == tool_search.STATUS_SUBAGENTS


def test_an_id_named_server_is_a_connector_only_when_nothing_else_names_it():
    snap = _snapshot(mcp_servers={"names": [ID]})
    assert _rows(_unused(ID, entrypoint=DESKTOP), snapshots=[snap])[ID].kind == tool_search.KIND_USER


BUILT_INS = ["computer-use", "visualize", "Claude_Browser", "Claude_Preview", "ccd_session", "ccd_directory",
             "terminal", "scheduled-tasks", "mcp-registry"]


@pytest.mark.parametrize("server", BUILT_INS)
def test_a_server_the_desktop_app_brings_itself_is_shown_with_its_cost_and_never_told_to_remove(server):
    results = _unused(server, entrypoint=DESKTOP)
    row = _rows(results)[server]
    assert row.kind == tool_search.KIND_DESKTOP_BUILTIN and row.status == tool_search.STATUS_BUILT_IN
    assert row.removable_usd == pytest.approx(10 * BIG * PER_CHAR)
    # Even well past every bar, and with every project in view: no card.
    assert _cards(results) == []
    (card,) = _cards(_unused(server, entrypoint=DESKTOP) + _unused("claude_ai_A", entrypoint=DESKTOP))
    assert server not in card.subject and card.subject == "claude_ai_A"


def test_claude_in_chrome_in_the_desktop_app_is_built_in():
    rows = _rows(_unused("claude-in-chrome", entrypoint=DESKTOP))
    row = rows["claude-in-chrome"]
    assert row.kind == tool_search.KIND_DESKTOP_BUILTIN
    assert row.status == tool_search.STATUS_BUILT_IN
    assert "/chrome" in tool_search.server_fix({"kind": row.kind, "server": row.server})
    assert _cards(_unused("claude-in-chrome", entrypoint=DESKTOP)) == []
    # A terminal session's Chrome integration is not taken for the app's.
    assert _rows(_unused("claude-in-chrome", entrypoint="cli"))["claude-in-chrome"].kind == tool_search.KIND_UNKNOWN


def test_a_built_in_is_used_when_claude_used_it():
    results = _unused("terminal", entrypoint=DESKTOP) + [
        _session("u", _reply(calls={"mcp__terminal__run": 1}), entrypoint=DESKTOP)
    ]
    assert _rows(results)["terminal"].status == tool_search.STATUS_USED


def test_a_name_from_the_built_in_list_is_a_built_in_only_in_the_desktop_app_and_when_not_yours():
    # Offered only to a terminal session: some server of yours that shares a name.
    assert _rows(_unused("terminal", entrypoint="cli"))["terminal"].kind == tool_search.KIND_UNKNOWN
    assert _rows(_unused("terminal"))["terminal"].kind == tool_search.KIND_UNKNOWN
    # In your config, it is yours, and as removable as any other of yours.
    snap = _snapshot(mcp_servers={"names": ["terminal"]})
    row = _rows(_unused("terminal", entrypoint=DESKTOP), snapshots=[snap])["terminal"]
    assert row.kind == tool_search.KIND_USER and row.status == tool_search.STATUS_REMOVE
    # A near miss is not on the list.
    assert _rows(_unused("terminals", entrypoint=DESKTOP))["terminals"].kind == tool_search.KIND_UNKNOWN
    assert _rows(_unused("ccd", entrypoint=DESKTOP))["ccd"].kind == tool_search.KIND_UNKNOWN


def test_the_built_in_names_are_the_servers_this_table_calls_built_in():
    """What the pages that price the base are given, so that cost centres and
    the context budget never file a server of yours as the app's: a name on
    the list that the table calls yours, or saw only outside the desktop app,
    is not in it."""
    results = (
        _unused("terminal", entrypoint=DESKTOP)
        + _unused("computer-use", entrypoint=DESKTOP)
        + _unused("visualize", entrypoint="cli")
        + _unused("github", entrypoint=DESKTOP)
    )
    snap = _snapshot(mcp_servers={"names": ["computer-use"]})
    rows = _stats(results, snapshots=[snap]).mcp_servers
    assert tool_search.built_in_names(rows) == frozenset({"terminal"})
    assert tool_search.built_in_names(_stats(results).mcp_servers) == frozenset({"terminal", "computer-use"})
    assert tool_search.built_in_names([]) == frozenset()
    # Every name a built-in row goes by counts.
    row = tool_search.McpServerRow(server="id-1", aliases=("terminal",), kind=tool_search.KIND_DESKTOP_BUILTIN)
    assert tool_search.built_in_names([row]) == frozenset({"id-1", "terminal"})


def test_the_table_says_how_to_turn_a_server_off_only_where_it_is_known():
    results = [
        r for server in ("computer-use", "terminal", "claude_ai_Notes", "mystery")
        for r in _unused(server, entrypoint=DESKTOP)
    ]
    table = next(t for t in tool_search.build_section(_stats(results)).tables if t.name == "tool_search_servers")
    column = [c.key for c in table.columns].index("how_to_turn_off")
    fix = {row[0]: row[column] for row in table.rows}
    assert "not verified" in fix["computer-use"]
    assert fix["terminal"] == "" and fix["mystery"] == ""
    assert "+ > Connectors" not in fix["claude_ai_Notes"] and "run /mcp" in fix["claude_ai_Notes"]


def test_the_desktop_apps_id_for_a_connector_is_the_same_server():
    tools = {ID: ["find", "read"], "claude_ai_Notes": ["find", "read"], "other": ["find"]}
    results = _unused(ID, tools=tools)
    rows = _rows(results)
    row = rows["claude_ai_Notes"]
    assert ID not in rows and row.aliases == (ID,)
    assert row.kind == tool_search.KIND_DESKTOP_CONNECTOR and row.status == tool_search.STATUS_REMOVE
    assert row.samples == ("find", "read")
    (card,) = _cards(results)
    assert "+ > Connectors" in card.action and "claude.ai/customize/connectors" in card.action
    # A use under the other name counts.
    used = results + [_session("u", _reply(calls={"mcp__claude_ai_Notes__find": 1}), tools=tools)]
    assert _rows(used)["claude_ai_Notes"].status == tool_search.STATUS_USED


def test_two_named_servers_with_the_same_tools_stay_apart():
    tools = {"alpha": ["find"], "beta": ["find"]}
    rows = _rows(_unused("alpha", tools=tools) + _unused("beta", tools=tools))
    assert rows["alpha"].aliases == () and rows["beta"].aliases == ()


@pytest.mark.parametrize("use", [
    {"calls": {"mcp__srv__find": 2}},
    {"resources": {"srv": 1}},
    {"commands": ("/mcp__srv__prompt",)},
    {"attribution": "srv"},
])
def test_every_kind_of_use_counts(use):
    rows = _rows(_unused("srv") + [_session("u", _reply(**use))])
    assert rows["srv"].status == tool_search.STATUS_USED and rows["srv"].uses >= 1


def test_a_savers_hook_pointing_claude_at_its_tools_counts_as_use():
    saver = known_savers.KNOWN_SAVERS[0]
    server = tool_server(saver.tool_prefix + "x")
    rows = _rows(_unused(server) + [_session("u", _reply(redirects={saver.name: 3}))])
    assert rows[server].status == tool_search.STATUS_USED and rows[server].uses == 3


def test_a_server_offered_only_to_subagents_is_left_to_the_agent_rules():
    row = _rows(_unused("srv", kind="subagent"))["srv"]
    assert row.status == tool_search.STATUS_SUBAGENTS and row.main_sessions == 0 and row.subagent_runs == 10


def test_servers_never_offered_show_why():
    status = {"plugin_eng_slack": "needs sign-in", "broken": "failed to connect", "slow": "pending"}
    snap = _snapshot(mcp_servers={"names": ["stripe"]})
    rows = _rows([_session("s", _reply(), status=status)], snapshots=[snap])
    assert {s: rows[s].status for s in (*status, "stripe")} == {
        **status, "stripe": tool_search.STATUS_NOT_SEEN,
    }


def test_a_server_loaded_upfront_is_tracked_and_priced():
    results = [_session(f"s{i}", _reply(9.0 - i), upfront={"srv": BIG}) for i in range(10)]
    row = _rows(results)["srv"]
    assert row.main_sessions == 10 and row.list_usd == 0 and row.instructions_usd == 0
    assert row.definitions_usd == pytest.approx(10 * BIG * PER_CHAR)


def test_each_part_is_priced_at_the_replys_front_rate():
    results = [_session("s", _reply(lists={"srv": 400}, instructions={"srv": 1000}), upfront={"srv": 2000})]
    row = _rows(results)["srv"]
    assert row.list_usd == pytest.approx(400 * PER_CHAR)
    assert row.instructions_usd == pytest.approx(1000 * PER_CHAR)
    assert row.definitions_usd == pytest.approx(2000 * PER_CHAR)
    assert row.removable_usd == pytest.approx(3400 * PER_CHAR)


def test_the_servers_table_lists_every_server():
    results = [_session(f"s{i}", _reply(instructions={f"srv{i:02d}": 100})) for i in range(30)]
    section = tool_search.build_section(_stats(results))
    table = next(t for t in section.tables if t.name == "tool_search_servers")
    assert len(table.rows) == 30


def test_the_card_cites_real_cells_and_words_each_fix():
    snap = _snapshot(mcp_servers={"names": ["github"]}, content_layers={"mcp_json": {"names": ["linear"]}})
    tools = {ID: ["find"], "claude_ai_Notes": ["find"]}
    results = _unused("github") + _unused("linear") + _unused(ID, tools=tools) + _unused("claude_ai_Drive")
    stats = _stats(results, snapshots=[snap])
    report = _report(stats)
    (card,) = tool_search.RULES[0](report, tool_search.ToolSearchThresholds())
    assert card.id == "mcp-unused-server" and card.lever is None and not card.changes
    # No settings file changes, so the dashboard shows no scope chip.
    assert card.scope == ""
    assert card.subject == ",".join(sorted(["github", "linear", "claude_ai_Notes", "claude_ai_Drive"]))
    table = next(t for t in report.sections[0].tables if t.name == "tool_search_servers")
    for _label, value, source, row_key in card.evidence:
        assert source == "tool_search.tool_search_servers"
        (row,) = [r for r in table.rows if r[0] == row_key]
        assert value in row
    assert "claude mcp remove github --scope user" in card.action
    assert "disabledMcpjsonServers" in card.action and ".claude/settings.local.json" in card.action
    # One connector is the desktop app's, one isn't: a sentence each.
    assert "Notes: in the desktop app" in card.action and "Drive: run /mcp" in card.action

    (fix,) = fixes_mod.build_fixes(card)
    opening = fixes_mod._FINDING_OPEN.format(title=card.title)
    assert fix["prompt"].startswith(opening) and fix["prompt"].count(opening) == 1
    assert "claude mcp remove github --scope user" in fix["prompt"] and "+ > Connectors" in fix["prompt"]
    assert "{action}" not in fix["prompt"]


def test_servers_that_share_a_fix_share_a_sentence():
    (card,) = _cards(_unused("claude_ai_A") + _unused("claude_ai_B", chars=BIG * 2))
    assert card.action.startswith("B and A: run /mcp and disable them")
    assert card.why.startswith("B and A were offered in up to 10 main sessions over the last 9 days, "
                               "and Claude didn't use them. Every reply still carried their ")
    assert card.action.count("claude.ai/customize/connectors") == 1


def test_ignoring_the_card_holds_until_the_servers_change():
    a = _cards(_unused("claude_ai_A") + _unused("claude_ai_B"))[0]
    same = _cards(_unused("claude_ai_A") + _unused("claude_ai_B", chars=BIG * 2))[0]
    other = _cards(_unused("claude_ai_A") + _unused("claude_ai_C"))[0]
    assert ignores.fingerprint(a) == ignores.fingerprint(same) != ignores.fingerprint(other)
    # A card with no subject keeps the fingerprint it always had.
    plain = Recommendation(id="x", severity="advice", category="workflow", title="t", lever=None)
    legacy = {"id": "x", "agent_type": None, "lever": None}
    assert ignores.fingerprint(plain) == hashlib.sha256(json.dumps(legacy, sort_keys=True).encode("utf-8")).hexdigest()
    assert ignores.fingerprint(plain) != ignores.fingerprint(
        Recommendation(id="x", severity="advice", category="workflow", title="t", lever=None, subject="s")
    )


@pytest.mark.parametrize("mode", ["api", "subscription"])
def test_the_saving_reads_in_the_billing_mode_and_the_fix_list_is_kept(mode):
    (card,) = _cards(_unused("claude_ai_A") + _unused("claude_ai_B"))
    action = card.action
    (out,) = [r for r in advice.finish([card], _report(_stats([])), None, Units(billing_mode=mode))
              if r.id == "mcp-unused-server"]
    assert out.action == action
    assert out.estimated_saving.startswith("At least ")
    # Without a usage-limit reading, a subscription reads it at list price.
    assert ("list-price" in out.estimated_saving) == (mode == "subscription")
    assert "cache read rate" in out.saving_basis


def test_no_server_is_named_in_the_code_that_classifies_them():
    source = Path(tool_search.__file__).read_text(encoding="utf-8")
    for name in ("Microsoft", "Google", "Drive", "Docs", "visualize", "ccd", "codex", "tokensave", "github",
                 "playwright", "computer-use", "Browser", "Superhuman", "sentry", "stripe"):
        assert not re.search(rf"(?<![a-z]){re.escape(name)}(?![a-z])", source, re.I), name


# -- the check: the unused-server card --------------------------------------------


def test_check_offers_the_unused_server_fix_even_with_nothing_deferred(tmp_path):
    (card,) = _cards(_unused("claude_ai_A") + _unused("claude_ai_B"))
    card.fixes = fixes_mod.build_fixes(card)
    ctx = _ctx(tmp_path, [])
    ctx.model.recommendations = [card]
    result = qa.run("tool-search", ctx)
    assert result["status"] == "act" and card.title in result["summary"]
    assert [f["title"] for f in result["fixes"]] == [card.title]

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
from claudeglass.snapshots import Snapshot, snapshot_project_key
from claudeglass.units import Units

from helpers import attachment_line, system_line, tool_use_block, turn_line, write_jsonl

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


def test_deferred_tools_are_sized_by_their_own_server_or_every_server():
    # github's loaded definitions average 400 tokens; jira has none of its
    # own, so it takes the average of every loaded definition (400 too).
    definitions = {"mcp__github__a": 1200, "mcp__github__b": 2000}
    stats = tool_search.compute_tool_search(
        [_result(_turn(1, {"github": 10, "jira": 5}), definitions=definitions)], PRICING
    )

    github, jira = stats.servers["github"], stats.servers["jira"]
    assert github.definition_tokens == pytest.approx(400) and github.own_sizes and github.measured == 2
    assert jira.definition_tokens == pytest.approx(400) and not jira.own_sizes and jira.measured == 0
    assert stats.kept_tokens == pytest.approx(15 * 400)
    assert github.saving_usd == pytest.approx(10 * 400 * SONNET.cache_read / 1e6)
    assert stats.most_deferred == 15 and stats.most_deferred_mcp == 15


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
             status=None) -> TranscriptResult:
    return TranscriptResult(
        meta=TranscriptMeta(path=f"{session_id}.jsonl", kind=kind, session_id=session_id, project_slug=slug),
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
    assert card.title == "Notes is an MCP server you never use"
    assert card.why == (
        "Notes was offered in up to 10 main sessions and Claude never used it, "
        "yet every reply carried its tool names, instructions or tools."
    )
    assert card.action.startswith("Notes: run /mcp and disable it ")


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
    assert card.why.startswith("B and A were offered in up to 10 main sessions and Claude never used them, "
                               "yet every reply carried their ")
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

"""Tests for the per-part subagent startup recommendations
(``recommend._rule_spawn_parts``), the fixes built from them
(``fixes.py``: explainer, command, prompt) and billing-mode amounts
(``units.py``)."""

from __future__ import annotations

import re
import shlex

import pytest

from claudeglass import cli, context_budget, fixes
from claudeglass.config import Config
from claudeglass.elasticity import ElasticityStats, FitResult
from claudeglass.model import (
    Column,
    Diagnostics,
    PricingMeta,
    Recommendation,
    ReportMeta,
    ReportModel,
    Section,
    SettingChange,
    Table,
)
from claudeglass.recommend import recommend
from claudeglass.snapshots import Snapshot
from claudeglass.units import NO_LIMIT_SHARE_HINT, Units

PRICE = 3.75  # USD per million tokens written (a Sonnet-class 5m cache write)


def _acc(agent_type, *, spawns=10, claude_md=3000.0, skills=800.0, task=500.0, tool_lists=1500.0, **counts):
    acc = context_budget._AgentStartupAcc(agent_type=agent_type, spawns=spawns)
    acc.startup_tokens = [20_000] * spawns
    acc.parts = {part: [0.0] * spawns for part in context_budget.STARTUP_PARTS}
    acc.parts["claude_md"] = [claude_md] * spawns
    acc.parts["skills_listing"] = [skills] * spawns
    acc.parts["task_prompt"] = [task] * spawns
    acc.parts["tool_lists"] = [tool_lists] * spawns
    acc.write_prices = [PRICE] * spawns
    acc.skills_listed_spawns = counts.get("skills_listed", spawns)
    acc.skills_used_spawns = counts.get("skills_used", spawns)
    acc.mcp_offered_spawns = counts.get("mcp_offered", 0)
    acc.mcp_used_spawns = counts.get("mcp_used", 0)
    acc.read_only_spawns = counts.get("read_only", 0)
    return acc


READ_PRICE = 0.30  # USD per million tokens read from cache (the same model's)


def _with_tools(
    acc,
    *,
    uses=None,
    chars=None,
    servers=None,
    prefix_written=None,
    later_calls=10,
    roster=0.0,
):
    """Record on ``acc`` what its spawns were offered and called, as the
    parser leaves it: ``uses`` is tool -> spawns that called it, ``chars``
    tool -> characters of its definition per spawn (every spawn is offered
    every tool in ``chars``), ``servers`` server -> (spawns that used it,
    definition tokens, deferred tokens, instruction tokens, all per spawn).
    Everything is under the unnamed model family the helper's other
    fields use."""
    spawns = acc.spawns
    uses = dict(uses or {})
    chars = dict(chars or {})
    servers = dict(servers or {})
    acc.read_prices = [READ_PRICE] * spawns
    acc.later_calls = [later_calls] * spawns
    acc.roster_tokens = [roster] * spawns
    acc.tool_spawns = {"": spawns}
    acc.tools = {
        "": {
            name: context_budget._ToolOffer(offered=spawns, used=uses.get(name, 0), chars=int(size * spawns))
            for name, size in chars.items()
        }
    }
    acc.tool_uses = {"": {name: n for name, n in uses.items() if n}}
    acc.servers = {}
    for server, (used, definitions, deferred, instructions) in servers.items():
        key = context_budget.mcp_tool_key(server)
        acc.tools[""][key] = context_budget._ToolOffer(
            offered=spawns, used=used, chars=int(definitions * spawns * 4)
        )
        acc.servers.setdefault("", {})[server] = context_budget._ServerOffer(
            offered=spawns,
            used=used,
            definitions=definitions * spawns,
            deferred=deferred * spawns,
            instructions=instructions * spawns,
        )
        if used:
            acc.tool_uses[""][key] = used
    acc.prefix_written = {"": spawns if prefix_written is None else prefix_written}
    return acc


def _diet_acc(agent_type, *, spawns=20, **kwargs):
    """An agent type that called Read and Grep every time and Bash twice
    in twenty, and was offered three tools it never called."""
    counts = {name: kwargs.pop(name) for name in ("skills_used", "mcp_offered", "mcp_used", "read_only") if name in kwargs}
    task = kwargs.pop("task", 500.0)
    claude_md = kwargs.pop("claude_md", 0.0)
    acc = _acc(agent_type, spawns=spawns, claude_md=claude_md, skills=1200.0, task=task, **{"skills_used": 0, **counts})
    options = {
        "uses": {"Read": spawns, "Grep": spawns, "Bash": max(1, spawns // 10)},
        "chars": {"Read": 1500, "Grep": 1200, "Bash": 9000, "Skill": 4000, "NotebookEdit": 16000, "WebFetch": 8000},
    }
    options.update(kwargs)
    return _with_tools(acc, **options)


def _report(*accs) -> ReportModel:
    stats = context_budget.ContextBudgetStats()
    for acc in accs:
        stats.agents[acc.agent_type] = acc
    overview = Section(
        key="overview",
        title="Overview",
        tables=[
            Table(
                name="totals",
                columns=[Column(key="metric", label="Metric"), Column(key="value", label="Value")],
                rows=[["sessions", 10], ["priced_turns", 400], ["cache_read_cost_share_pct", 10.0]],
            )
        ],
    )
    return ReportModel(
        meta=ReportMeta(pricing=PricingMeta(coverage_pct=100.0)),
        sections=[overview, context_budget.build_startup_section(stats)],
        diagnostics=Diagnostics(lines=1000),
    )


def _snapshot(agents: dict) -> Snapshot:
    return Snapshot(path=None, ts="2026-09-20T00:00:00Z", data={"agents": agents})


def _recs(report, *, snapshot=None, units=None):
    recs = recommend(report, config=Config(), archetype=None, snapshot=snapshot, units=units)
    fixes.attach_fixes(recs)
    return recs


# -- the rules -----------------------------------------------------------


def test_large_claude_md_on_a_custom_agent_suggests_omit_claude_md():
    snap = _snapshot({"reviewer": {"source": "user"}})
    recs = _recs(_report(_acc("reviewer")), snapshot=snap, units=Units())
    rec = next(r for r in recs if r.id == "spawn-claude-md")
    assert rec.agent_type == "reviewer"
    assert rec.scope == "user"
    change = rec.changes[0]
    assert (change.target, change.key, change.agent, change.value) == ("agent", "omitClaudeMd", "reviewer", True)
    assert change.current is None  # snapshot present, key not set
    # 3,000 tokens x 10 spawns x 3.75 USD per million = 0.1125 USD.
    assert rec.estimated_saving == "0.11 USD across the spawns in this report."
    assert rec.fixes[0]["command"] == (
        "claudeglass apply --set omitClaudeMd=true --agent reviewer --scope user --dry-run"
    )
    # The rules the agent needs are moved into its own prompt before the
    # flag is set; the command, which only sets the flag, says so.
    prompt = rec.fixes[0]["prompt"]
    assert prompt.index("List the rules reviewer needs") < prompt.index("set omitClaudeMd to true")
    assert "~/.claude/agents/reviewer.md" in prompt
    assert "only sets the flag" in rec.fixes[0]["command_warning"]
    # The generic spawn-cost advice is not repeated for a covered agent type.
    assert not any(r.id == "spawn-cost" and r.agent_type == "reviewer" for r in recs)


def test_managed_claude_md_is_excluded_from_the_omit_claude_md_saving():
    # PROF-11/F13: Managed policy CLAUDE.md still loads regardless of
    # omitClaudeMd, so it's excluded from the saving and cited on its own.
    acc = _acc("reviewer", claude_md=5000.0)
    acc.claude_md_by_source = {"Managed": [2000.0] * acc.spawns, "Project": [3000.0] * acc.spawns}
    snap = _snapshot({"reviewer": {"source": "user"}})
    recs = _recs(_report(acc), snapshot=snap, units=Units())
    rec = next(r for r in recs if r.id == "spawn-claude-md")
    # Only the 3,000 non-managed tokens count: 3,000 x 10 x 3.75 / 1e6 = 0.1125 USD.
    assert rec.estimated_saving == "0.11 USD across the spawns in this report."
    assert "Managed policy CLAUDE.md (2,000 tokens) still loads either way." in rec.why
    labels = {label for label, *_ in rec.evidence}
    assert "Managed policy CLAUDE.md per spawn (still loads)" in labels


def test_omit_claude_md_never_offered_when_only_managed_claude_md_is_seen():
    acc = _acc("reviewer", claude_md=5000.0)
    acc.claude_md_by_source = {"Managed": [5000.0] * acc.spawns}
    snap = _snapshot({"reviewer": {"source": "user"}})
    recs = _recs(_report(acc), snapshot=snap, units=Units())
    assert not any(r.id == "spawn-claude-md" for r in recs)


def test_explore_and_plan_never_get_omit_claude_md():
    recs = _recs(_report(_acc("Explore"), _acc("Plan")))
    assert not any(r.id == "spawn-claude-md" for r in recs)


def test_builtin_agent_gets_a_prompt_to_create_an_override_but_no_command():
    recs = _recs(_report(_acc("general-purpose")))
    rec = next(r for r in recs if r.id == "spawn-claude-md")
    assert rec.lever is None
    assert rec.changes[0].new_agent_file
    fix = rec.fixes[0]
    assert fix["command"] is None
    assert "same name" in fix["prompt"]
    assert "unknown (no config snapshot yet)" in dict(map(tuple, fix["explainer"]))["Now and after"]


def test_workflow_subagents_get_no_override_advice():
    recs = _recs(_report(_acc("workflow-subagent", skills_used=0, mcp_offered=10)))
    assert not any(r.agent_type == "workflow-subagent" and r.changes for r in recs)


def test_unused_skills_are_unconfirmed_and_prompt_only_for_an_existing_file():
    snap = _snapshot({"reviewer": {"source": "project", "disallowedTools": ["Bash"]}})
    recs = _recs(_report(_acc("reviewer", claude_md=0.0, skills_used=0)), snapshot=snap, units=Units())
    rec = next(r for r in recs if r.id == "spawn-unused-skills")
    change = rec.changes[0]
    assert change.unconfirmed and change.value is None and change.current == ["Bash"]
    assert rec.scope == "repo"
    assert rec.fixes[0]["command"] is None
    assert "docs don't confirm" in dict(map(tuple, rec.fixes[0]["explainer"]))["Expected effect"]


# -- the tools list --------------------------------------------------------


def _tools_rec(recs, agent_type):
    return next((r for r in recs if r.id == "spawn-tools-list" and r.agent_type == agent_type), None)


def test_tools_list_keeps_the_tools_called_in_at_least_a_tenth_of_the_spawns():
    # 20 spawns: Read and Grep every time, Bash in 2 (exactly a tenth, so
    # kept), Skill, NotebookEdit and WebFetch never.
    offered = ["Read", "Grep", "Bash", "Skill", "NotebookEdit", "WebFetch"]
    snap = _snapshot({"searcher": {"source": "user", "tools": offered}})
    recs = _recs(_report(_diet_acc("searcher")), snapshot=snap, units=Units())
    rec = _tools_rec(recs, "searcher")
    assert rec is not None and rec.id == "spawn-tools-list"
    change = rec.changes[0]
    assert (change.target, change.key, change.agent) == ("agent", "tools", "searcher")
    assert change.value == ["Bash", "Grep", "Read"]
    assert not change.new_agent_file and change.current == offered
    assert "tools: Bash, Grep, Read" in rec.action
    for left_out in ("NotebookEdit", "Skill", "WebFetch"):
        assert left_out in rec.action
    assert rec.scope == "user" and rec.category == "settings" and rec.severity == "advice"
    # The rule retires the read-only card; neither id exists any more.
    assert not any(r.id == "spawn-read-only-tools" for r in recs)


def test_a_tool_called_in_under_a_tenth_of_the_spawns_is_left_off_the_list():
    # Bash in 1 of 20 is 5%: below the tenth, so it is listed as rarely used.
    acc = _diet_acc("searcher", uses={"Read": 20, "Grep": 20, "Bash": 1})
    recs = _recs(_report(acc), snapshot=_snapshot({"searcher": {"source": "user"}}), units=Units())
    rec = _tools_rec(recs, "searcher")
    assert rec.changes[0].value == ["Grep", "Read"]
    assert "Rarely used, so left out" in rec.action and "Bash" in rec.action.split("Rarely used")[1]
    assert "fewer than 10%" in rec.action


def test_the_card_notes_the_tools_claude_code_adds_and_asks_for_a_before_and_after_check():
    recs = _recs(_report(_diet_acc("searcher")), snapshot=_snapshot({"searcher": {"source": "user"}}), units=Units())
    rec = _tools_rec(recs, "searcher")
    assert "StructuredOutput and SubagentHandback" in rec.action
    assert "one spawn before and one after on the same model" in rec.action
    # They are never on the line itself, or in the rarely used list.
    assert "StructuredOutput" not in rec.changes[0].value
    prompt = rec.fixes[0]["prompt"]
    assert "StructuredOutput and SubagentHandback" in prompt


def test_the_card_is_priced_from_the_diet_table_and_is_the_only_one_to_carry_a_saving():
    acc = _diet_acc("searcher")
    report = _report(acc)
    table = next(t for s in report.sections if s.key == "agent_startup" for t in s.tables if t.name == "agent_startup_diet")
    row = dict(zip([c.key for c in table.columns], table.rows[0]))
    # Read and Grep stay; Bash (2 of 20) stays too. Skill, NotebookEdit and
    # WebFetch go: (4000 + 16000 + 8000) chars at the default 4 per token.
    assert row["keep_tools"] == "Bash, Grep, Read"
    assert set(row["rare_tools"].split(", ")) == {"NotebookEdit", "WebFetch", "Skill"}
    recs = _recs(report, snapshot=_snapshot({"searcher": {"source": "user"}}), units=Units())
    rec = _tools_rec(recs, "searcher")
    assert rec.saving_usd == pytest.approx(row["saving_usd"])
    assert any(label == "Saving across these spawns" and value == row["saving_usd"] for label, value, *_ in rec.evidence)
    # The skills card is a part of the same saving, so it carries none.
    skills = next(r for r in recs if r.id == "spawn-unused-skills")
    assert skills.saving_usd is None
    assert "part of the tools list saving, not on top of it" in skills.saving_basis
    assert skills.estimated_saving


def test_the_shared_tool_prefix_is_not_counted_at_the_write_price_by_every_sibling():
    # The same agent type, once where every spawn wrote the tool prefix and
    # once where one in ten did: the second saves less, because the spawns
    # that read it pay the cache-read price for those definitions. The
    # skills list is not part of that prefix, so it prices the same.
    def saving(prefix_written):
        report = _report(_diet_acc("searcher", prefix_written=prefix_written))
        table = next(t for s in report.sections if s.key == "agent_startup" for t in s.tables if t.name == "agent_startup_diet")
        return dict(zip([c.key for c in table.columns], table.rows[0]))

    every, tenth = saving(20), saving(2)
    assert every["prefix_write_share"] == 100.0 and tenth["prefix_write_share"] == 10.0
    assert tenth["saving_usd"] < every["saving_usd"]
    definitions = every["dropped_definitions"]
    gap = definitions * 0.9 * (PRICE - READ_PRICE) * 20 / 1_000_000
    assert every["saving_usd"] - tenth["saving_usd"] == pytest.approx(gap, rel=1e-6)


def test_agents_the_diet_leaves_alone_get_no_tools_list():
    for name in ("Explore", "Plan", "claude-code-guide"):
        recs = _recs(_report(_diet_acc(name)), units=Units())
        assert not any(r.id == "spawn-tools-list" for r in recs), name


def test_no_tools_list_without_a_tool_called_or_below_the_size_worth_a_change():
    # Nothing called: there is nothing to put on the line.
    silent = _diet_acc("searcher", uses={})
    assert _tools_rec(_recs(_report(silent), units=Units()), "searcher") is None
    # A few small tools left out: under the 5,000-token floor.
    small = _diet_acc("searcher", chars={"Read": 1500, "Grep": 1200, "Skill": 400}, uses={"Read": 20, "Grep": 20})
    small.parts["skills_listing"] = [0.0] * small.spawns
    assert _tools_rec(_recs(_report(small), units=Units()), "searcher") is None
    # Too few spawns.
    few = _diet_acc("searcher", spawns=3, uses={"Read": 3, "Grep": 3})
    assert _tools_rec(_recs(_report(few), units=Units()), "searcher") is None


def test_a_tools_list_already_in_the_agent_file_is_not_offered_again():
    # The same tools in another order are the same list.
    snap = _snapshot({"searcher": {"source": "user", "tools": ["Read", "Grep", "Bash"]}})
    recs = _recs(_report(_diet_acc("searcher")), snapshot=snap, units=Units())
    assert _tools_rec(recs, "searcher") is None
    snap = _snapshot({"searcher": {"source": "user", "tools": "Read, Grep, Bash"}})
    assert _tools_rec(_recs(_report(_diet_acc("searcher")), snapshot=snap, units=Units()), "searcher") is None


def test_a_builtin_agent_gets_a_prompt_to_create_a_same_named_file():
    recs = _recs(_report(_diet_acc("statusline-setup")), units=Units())
    rec = _tools_rec(recs, "statusline-setup")
    assert rec.changes[0].new_agent_file and rec.fixes[0]["command"] is None
    assert "tools" in rec.fixes[0]["prompt"]
    # A new file already lists the tools, so the prompt does not also say to
    # keep them the same.
    assert "keep its tools the same" not in rec.fixes[0]["prompt"]


def test_general_purpose_adds_a_tools_list_with_a_warning_that_it_limits_every_adhoc_spawn():
    # Without an override file the prompt creates one, with the warning.
    recs = _recs(_report(_diet_acc("general-purpose")), units=Units())
    rec = _tools_rec(recs, "general-purpose")
    assert rec.changes[0].new_agent_file
    assert "limits every one of those spawns" in rec.action
    assert "limits every one of those spawns" in rec.fixes[0]["prompt"]
    # With an existing ~/.claude/agents/general-purpose.md the change is to
    # that file, and the warning is still there.
    snap = _snapshot({"general-purpose": {"source": "user", "model": "inherit"}})
    rec = _tools_rec(_recs(_report(_diet_acc("general-purpose")), snapshot=snap, units=Units()), "general-purpose")
    assert not rec.changes[0].new_agent_file
    assert "~/.claude/agents/general-purpose.md" in rec.fixes[0]["prompt"]
    assert "limits every one of those spawns" in rec.action
    # No other agent gets the warning.
    other = _tools_rec(_recs(_report(_diet_acc("searcher")), units=Units()), "searcher")
    assert "every one of those spawns" not in other.action


def test_workflow_agents_get_the_workflow_script_variant_and_never_a_setting():
    recs = _recs(_report(_diet_acc("workflow-subagent")), units=Units())
    rec = _tools_rec(recs, "workflow-subagent")
    assert rec is not None
    assert rec.variant == "workflow-script" and rec.category == "workflow" and not rec.changes
    assert "agentType" in rec.action and "tools: Bash, Grep, Read" in rec.action
    [fix] = rec.fixes
    assert fix["key"] is None and fix["command"] is None
    assert "agentType" in fix["prompt"] and "tools: Bash, Grep, Read" in fix["prompt"]
    assert fix["explainer"] and all(text.strip() for _, text in fix["explainer"])
    # Forks and unknown types have no file to give a list, so they get none.
    for name in ("fork", "unknown"):
        assert _tools_rec(_recs(_report(_diet_acc(name)), units=Units()), name) is None


def test_mcp_instructions_stay_under_a_tools_list():
    servers = {"playwright": (0, 8000.0, 400.0, 600.0)}
    recs = _recs(_report(_diet_acc("searcher", servers=servers)), units=Units())
    rec = _tools_rec(recs, "searcher")
    assert "mcp__playwright__*" in rec.action
    assert "leaves MCP server instructions in" in rec.saving_basis
    assert "600 tokens per spawn" in rec.saving_basis
    # Switching the server off removes them; an mcpServers list is unverified.
    assert "Switching the server off drops them" in rec.saving_basis and "isn't verified" in rec.saving_basis


def test_skills_card_offers_the_tools_list_first_and_keeps_disallowed_tools_as_the_narrow_alternative():
    # Skill is on the tools list's rarely used side: the card points to it.
    recs = _recs(_report(_diet_acc("searcher")), snapshot=_snapshot({"searcher": {"source": "user"}}), units=Units())
    rec = next(r for r in recs if r.id == "spawn-unused-skills")
    assert rec.action.startswith("Leave Skill off the tools list for searcher")
    assert "add Skill to its disallowedTools instead" in rec.action
    assert rec.changes[0].key == "disallowedTools"
    # Skill kept on the list (it is called): no tools list takes it out, so the
    # card names the list as one way and does not call itself a part of it.
    no_diet = _acc("plain", claude_md=0.0, skills=1200.0, skills_used=0)
    recs = _recs(_report(no_diet), snapshot=_snapshot({"plain": {"source": "user"}}), units=Units())
    rec = next(r for r in recs if r.id == "spawn-unused-skills")
    assert rec.action.startswith("Give plain a tools list that leaves Skill out")
    assert "not on top of it" not in rec.saving_basis


def test_unused_mcp_names_a_server_only_at_two_percent_of_spawns_and_five_dollars_a_month():
    # A big server: 25,000 definition tokens per spawn, 10 later calls, over
    # a 7-day window, so it is well over 5 USD a month.
    big = (0, 25_000.0, 800.0, 300.0)
    recs = _recs(_report(_diet_acc("searcher", spawns=50, servers={"playwright": big})), units=Units())
    rec = next(r for r in recs if r.id == "spawn-unused-mcp" and r.agent_type == "searcher")
    assert "playwright (used in 0 of 50 spawns)" in rec.action
    assert "mcpServers" in rec.action and rec.changes[0].key == "mcpServers"
    # ... exactly 2% (1 of 50) still counts, 2 of 50 (4%) does not.
    two = (1, 25_000.0, 800.0, 300.0)
    recs = _recs(_report(_diet_acc("searcher", spawns=50, servers={"playwright": two})), units=Units())
    assert any(r.id == "spawn-unused-mcp" for r in recs)
    more = (2, 25_000.0, 800.0, 300.0)
    recs = _recs(_report(_diet_acc("searcher", spawns=50, servers={"playwright": more})), units=Units())
    assert not any(r.id == "spawn-unused-mcp" for r in recs)
    # A small server is under 5 USD over 30 days, so it is left off the card.
    small = (0, 300.0, 40.0, 100.0)
    recs = _recs(_report(_diet_acc("searcher", spawns=50, servers={"playwright": small})), units=Units())
    assert not any(r.id == "spawn-unused-mcp" for r in recs)


def test_unused_mcp_goes_per_server_and_is_a_breakdown_not_an_addition():
    servers = {"playwright": (0, 25_000.0, 800.0, 300.0), "sentry": (0, 20_000.0, 500.0, 200.0), "tiny": (0, 200.0, 20.0, 50.0)}
    recs = _recs(_report(_diet_acc("searcher", spawns=50, servers=servers)), snapshot=_snapshot({"searcher": {"source": "user"}}), units=Units())
    mcp = [r for r in recs if r.id == "spawn-unused-mcp"]
    assert len(mcp) == 1  # one card per agent, not one per server
    rec = mcp[0]
    assert "playwright" in rec.action and "sentry" in rec.action and "tiny" not in rec.action
    assert rec.saving_usd is None and rec.estimated_saving
    assert "isn't added to it" in rec.saving_basis
    assert rec.changes[0].unconfirmed
    assert "instructions stay under a tools list" in rec.saving_basis
    # The tools list card carries the one saving that the dashboard sums.
    assert _tools_rec(recs, "searcher").saving_usd
    # mcpServers is not in the config snapshot, so the change says so.
    note = rec.changes[0].note
    assert "doesn't record" in note and "mcpServers" in rec.changes[0].key
    assert rec.changes[0].value is None and rec.changes[0].suggested


def test_unused_mcp_is_not_offered_to_workflow_agents():
    servers = {"playwright": (0, 25_000.0, 800.0, 300.0)}
    recs = _recs(_report(_diet_acc("workflow-subagent", spawns=50, servers=servers)), units=Units())
    assert not any(r.id == "spawn-unused-mcp" for r in recs)


def test_below_min_spawns_nothing_fires():
    recs = _recs(_report(_acc("reviewer", spawns=3, skills_used=0, mcp_offered=3)))
    assert not any(r.id.startswith("spawn-") and r.id != "spawn-cost" for r in recs)


def test_long_task_prompt_is_workflow_advice():
    recs = _recs(_report(_acc("reviewer", claude_md=0.0, task=6000.0)))
    rec = next(r for r in recs if r.id == "spawn-task-prompt")
    # UX-8: a workflow rule with no SettingChange still gets one fix, built
    # from _WORKFLOW_EXPLAINER/_WORKFLOW_PROMPTS -- a where/trade-off/undo
    # explainer and a self-contained prompt, not a real config change.
    assert rec.category == "workflow" and not rec.changes
    [fix] = rec.fixes
    assert fix["key"] is None and fix["command"] is None and fix["explainer"]
    assert fix["prompt"]


def test_shared_claude_md_comes_with_a_prompt():
    recs = _recs(_report(_acc("a", claude_md=0.0), _acc("b", claude_md=0.0)))
    # No CLAUDE.md by source was recorded, so nothing is shared.
    assert not any(r.id == "spawn-shared-claude-md" for r in recs)

    one, two = _acc("a"), _acc("b")
    for acc in (one, two):
        acc.claude_md_by_source = {"Project": [3000.0] * acc.spawns}
    recs = _recs(_report(one, two))
    rec = next(r for r in recs if r.id == "spawn-shared-claude-md")
    assert rec.fixes and rec.fixes[0]["command"] is None
    assert "move" in rec.fixes[0]["prompt"]


# -- recommendation key --------------------------------------------------


def test_recommendation_keys_are_unique_across_a_realistic_multi_agent_corpus():
    """``key`` (Task Group B, item 5) disambiguates a rule id that fires
    once per agent type: three agent types large enough to each trip
    spawn-claude-md still get one recommendation apiece, and every
    recommendation across the whole run gets its own key."""
    snap = _snapshot({
        "reviewer": {"source": "user"},
        "implementer": {"source": "user"},
        "Report Writer": {"source": "user"},
    })
    recs = _recs(
        _report(_acc("reviewer"), _acc("implementer"), _acc("Report Writer")),
        snapshot=snap,
        units=Units(),
    )
    claude_md_recs = [r for r in recs if r.id == "spawn-claude-md"]
    assert {r.agent_type for r in claude_md_recs} == {"reviewer", "implementer", "Report Writer"}
    keys = [r.key for r in recs]
    assert keys and all(keys)
    assert len(keys) == len(set(keys)), keys
    # Every key stays URL-safe even for an agent type named with a space
    # and capitals.
    assert all(re.fullmatch(r"[a-z0-9._:-]+", key) for key in keys), keys
    by_agent = {r.agent_type: r.key for r in claude_md_recs}
    assert by_agent["reviewer"] == "spawn-claude-md:reviewer"
    assert by_agent["Report Writer"] == "spawn-claude-md:report-writer"


def test_recommendation_keys_are_stable_across_two_runs():
    """The same corpus recommended twice (e.g. two dashboard requests a
    few seconds apart) gets exactly the same keys back."""
    snap = _snapshot({
        "reviewer": {"source": "user"},
        "implementer": {"source": "user"},
    })

    def _run():
        report = _report(_acc("reviewer"), _acc("implementer"))
        return {r.key for r in _recs(report, snapshot=snap, units=Units())}

    assert _run() == _run()


# -- fix contract ------------------------------------------------------------


def _all_fixes():
    snap = _snapshot({"reviewer": {"source": "user"}, "searcher": {"source": "project"}})
    servers = {"playwright": (0, 40_000.0, 800.0, 300.0)}
    report = _report(
        _diet_acc("reviewer", spawns=10, servers=servers, mcp_offered=10, task=6000.0, claude_md=3000.0),
        _diet_acc("searcher", spawns=10),
        _diet_acc("general-purpose", spawns=10),
        _diet_acc("workflow-subagent", spawns=10),
    )
    recs = _recs(report, snapshot=snap, units=Units(billing_mode="subscription"))
    return [(rec, fix) for rec in recs for fix in rec.fixes]


def test_every_setting_fix_has_the_six_part_explainer():
    pairs = [(rec, fix) for rec, fix in _all_fixes() if fix["key"]]
    assert pairs
    for rec, fix in pairs:
        headings = [heading for heading, _ in fix["explainer"]]
        assert headings == [
            "What this setting controls",
            "Now and after",
            "Where and who it affects",
            "Expected effect",
            "Trade-off",
            "How to undo it",
        ], rec.id
        assert all(text.strip() for _, text in fix["explainer"])


def test_every_command_parses_with_the_real_parser():
    commands = [fix["command"] for _, fix in _all_fixes() if fix["command"]]
    assert commands
    parser = cli._make_parser()
    for command in commands:
        argv = shlex.split(command)
        assert argv[0] == "claudeglass"
        args = parser.parse_args(argv[1:])
        assert args.dry_run and args.set_values
        profile, err = cli._one_off_profile(args.set_values, args.agent)
        assert err is None, (command, err)


def test_prompts_are_self_contained_and_carry_no_absolute_paths():
    for rec, fix in _all_fixes():
        prompt = fix["prompt"]
        if not prompt:
            # UX-8: a purely informational workflow card (no SettingChange,
            # no "ask Claude to do it" prompt) -- nothing to check here.
            continue
        assert ":\\" not in prompt and "/Users/" not in prompt and "/home/" not in prompt, rec.id
        if fix["key"]:
            assert fix["key"] in prompt


def test_subscription_without_readings_says_list_price_and_how_to_get_the_share():
    for rule in ("spawn-claude-md", "spawn-tools-list"):
        rec = next(rec for rec, _ in _all_fixes() if rec.id == rule)
        assert "list-price equivalent" in rec.estimated_saving, rule
        assert NO_LIMIT_SHARE_HINT in rec.saving_basis, rule


# -- units -------------------------------------------------------------------


def _fitted(slope: float) -> ElasticityStats:
    stats = ElasticityStats()
    stats.fits = {"seven_day": {"usd": FitResult(window="seven_day", metric="usd", slope=slope, accepted=True)}}
    return stats


def test_units_api_mode_is_dollars():
    amount = Units().money(12.5)
    assert amount.primary == "12.50 USD" and amount.basis == "at list price"


def test_units_subscription_uses_the_weekly_share_when_fitted():
    amount = Units(billing_mode="subscription", elasticity=_fitted(0.4)).money(10.0, period="per week")
    assert amount.primary == "about 4.0% of your weekly usage limit per week"
    assert amount.secondary == "10.00 USD list-price equivalent"
    small = Units(billing_mode="subscription", elasticity=_fitted(0.004)).money(10.0)
    assert small.primary.startswith("about 0.04%")


def test_units_subscription_falls_back_without_an_accepted_fit():
    stats = _fitted(0.4)
    stats.fits["seven_day"]["usd"].accepted = False
    amount = Units(billing_mode="subscription", elasticity=stats).money(3.0)
    assert amount.primary == "3.00 USD list-price equivalent"
    assert amount.basis == NO_LIMIT_SHARE_HINT


@pytest.mark.parametrize("value", [0, -1.0, float("nan"), None])
def test_units_refuses_non_positive_amounts(value):
    assert Units().money(value) is None


def test_fixes_for_purely_informational_workflow_advice_have_an_explainer_but_no_prompt():
    """UX-8: cache-read-dominance has nothing to change (see
    fixes._WORKFLOW_EXPLAINER) -- one fix with a where/trade-off/undo
    explainer, but an empty prompt: there's nothing to ask Claude to do."""
    [fix] = fixes.build_fixes(Recommendation(id="cache-read-dominance"))
    assert fix["explainer"] and fix["prompt"] == ""


def test_fixes_for_a_rule_id_with_no_workflow_entry_at_all_are_empty():
    assert fixes.build_fixes(Recommendation(id="not-a-real-rule-id")) == []


def test_command_for_repo_scope_names_the_project_dir():
    change = SettingChange(target="agent", key="omitClaudeMd", agent="x", value=True)
    assert fixes.command_for(change, "repo").endswith("--scope repo --project-dir . --dry-run")


# -- runs that said whether they used CLAUDE.md (metrics capture) ------------


def _with_rules(report, agent_type: str, used: int, unused: int):
    from claudeglass import habits

    runs = [habits.AgentFact(session_id="s", agent_type=agent_type, week="", cost=1.0, rules=word)
            for word, n in (("used", used), ("unused", unused)) for _ in range(n)]
    report.sections.append(habits.section_from(habits.Habits(agents=runs)))
    return report


def test_omit_claude_md_is_held_back_when_most_runs_said_they_used_it():
    snap = _snapshot({"reviewer": {"source": "user"}})
    recs = _recs(_with_rules(_report(_acc("reviewer")), "reviewer", used=2, unused=1), snapshot=snap)
    assert not any(r.id == "spawn-claude-md" for r in recs)


def test_runs_that_said_they_did_not_use_claude_md_are_cited():
    snap = _snapshot({"reviewer": {"source": "user"}})
    report = _with_rules(_report(_acc("reviewer")), "reviewer", used=1, unused=3)
    rec = next(r for r in _recs(report, snapshot=snap) if r.id == "spawn-claude-md")
    assert ("Runs that said they didn't use CLAUDE.md", 3, "habits.habits_agents", "reviewer") in rec.evidence
    assert "3 of the 4 runs that said, said they didn't use your CLAUDE.md." in rec.why

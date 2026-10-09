"""Tests for WP10b's recommendation engine (recommend.py).

Most rules are exercised with hand-built ``ReportModel``/``Section``/
``Table`` fixtures (``recommend()`` works purely off the rendered report,
never a raw corpus -- see recommend.py's module docstring), which lets
each test cross exactly one rule's threshold without assembling a whole
transcript corpus. The broader evidence-exists check and the real-fixture
check instead go through ``report.build_report()`` on an actual corpus,
so they also prove the wiring in report.py (Deliverable 2) produces
recommendations whose evidence resolves against the report it was built
from.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from claudeglass import fixes as fixes_mod, recommend, report
from claudeglass.config import Config
from claudeglass.corpus import load_corpus
from claudeglass.model import (
    Column,
    Diagnostics,
    PricingMeta,
    ReportMeta,
    ReportModel,
    Section,
    Table,
)
from claudeglass.recommend import (
    RecommendThresholds,
    effective_min_sample,
    recommend as recommend_fn,
    render_patch_set,
)
from claudeglass.snapshots import Snapshot

from helpers import assert_privacy, turn_line, write_jsonl

REAL_SESSION_A = Path(__file__).parent / "fixtures" / "real" / "session-a"


# -- fixture builders ------------------------------------------------------


def _overview_totals(sessions: int = 10, priced_turns: int = 400, cache_read_cost_share_pct: float = 10.0) -> Table:
    return Table(
        name="totals",
        title="Totals",
        columns=[Column(key="metric", label="Metric"), Column(key="value", label="Value")],
        rows=[
            ["sessions", sessions],
            ["priced_turns", priced_turns],
            ["cache_read_cost_share_pct", cache_read_cost_share_pct],
        ],
    )


def _base_report(**overview_kwargs) -> ReportModel:
    """A minimal but min-sample-satisfying report: just the ``overview``
    section every rule's ``_meets_min_sample`` gate reads. Individual
    tests add whatever other section/table a given rule needs.
    """
    overview = Section(key="overview", title="Overview", tables=[_overview_totals(**overview_kwargs)])
    return ReportModel(
        meta=ReportMeta(pricing=PricingMeta(coverage_pct=100.0)),
        sections=[overview],
        recommendations=[],
        diagnostics=Diagnostics(lines=1000, unparsable_lines=0, ttl_sum_mismatch=0),
    )


def _add_section(report_model: ReportModel, section: Section) -> ReportModel:
    report_model.sections.append(section)
    return report_model


def _config(provider: str | None = None) -> Config:
    return Config(provider=provider)


# -- minimum sample gate -----------------------------------------------------


def test_below_minimum_sample_suppresses_everything():
    small_report = _base_report(sessions=2, priced_turns=50)
    # Add a table that would otherwise clearly fire cache-read-dominance.
    small_report.sections[0].tables[0] = _overview_totals(
        sessions=2, priced_turns=50, cache_read_cost_share_pct=90.0
    )
    recs = recommend_fn(small_report, config=_config(), archetype=None)
    assert recs == []


def test_meets_minimum_sample_via_sessions_only():
    r = _base_report(sessions=5, priced_turns=10, cache_read_cost_share_pct=90.0)
    recs = recommend_fn(r, config=_config(), archetype=None)
    assert any(rec.id == "cache-read-dominance" for rec in recs)


def test_meets_minimum_sample_via_priced_turns_only():
    r = _base_report(sessions=1, priced_turns=200, cache_read_cost_share_pct=90.0)
    recs = recommend_fn(r, config=_config(), archetype=None)
    assert any(rec.id == "cache-read-dominance" for rec in recs)


# -- cache-read-dominance (simplest rule, also used above) -------------------


def test_cache_read_dominance_fires_above_threshold():
    r = _base_report(cache_read_cost_share_pct=60.0)
    recs = recommend_fn(r, config=_config(), archetype=None)
    rec = next(rec for rec in recs if rec.id == "cache-read-dominance")
    assert rec.evidence == [
        ("Cache-read share of cost", 60.0, "overview.totals", "cache_read_cost_share_pct"),
    ]


def test_cache_read_dominance_does_not_fire_below_threshold():
    r = _base_report(cache_read_cost_share_pct=40.0)
    recs = recommend_fn(r, config=_config(), archetype=None)
    assert not any(rec.id == "cache-read-dominance" for rec in recs)


def test_a_recommendation_with_no_agent_type_keeps_id_as_its_key():
    """``key`` (Task Group B, item 5) only needs to disambiguate a rule
    id that repeats per agent type; a corpus-wide recommendation like
    cache-read-dominance has no agent_type, so its key is just its id."""
    r = _base_report(cache_read_cost_share_pct=60.0)
    recs = recommend_fn(r, config=_config(), archetype=None)
    rec = next(rec for rec in recs if rec.id == "cache-read-dominance")
    assert rec.agent_type is None
    assert rec.key == "cache-read-dominance"


def test_repeated_keys_get_a_suffix_from_their_first_evidence_row():
    """``spawn-shared-claude-md`` fires once per CLAUDE.md source with no
    agent type, so the ids repeat; every card of the group gets its own
    key, and a lone card keeps its id."""
    from claudeglass.model import Recommendation
    from claudeglass.recommend import _unique_keys

    def rec(row, key="spawn-shared-claude-md"):
        r = Recommendation(
            id="spawn-shared-claude-md",
            severity="advice",
            category="settings",
            title="t",
            evidence=[("Size per spawn", 1, "agent_startup.agent_startup_shared", row)],
        )
        r.key = key
        return r

    recs = [rec("Project CLAUDE.md"), rec("User CLAUDE.md"), rec("Project CLAUDE.md"), rec("x", key="other")]
    _unique_keys(recs)
    assert [r.key for r in recs] == [
        "spawn-shared-claude-md:project-claude.md",
        "spawn-shared-claude-md:user-claude.md",
        "spawn-shared-claude-md:project-claude.md-2",
        "other",
    ]
    lone = [rec("User CLAUDE.md")]
    _unique_keys(lone)
    assert lone[0].key == "spawn-shared-claude-md"


# -- ttl-switch ---------------------------------------------------------


def _ttl_by_agent_type_table(rows: list[list]) -> Table:
    return Table(
        name="ttl_by_agent_type",
        title="TTL by agent type",
        columns=[
            Column(key="agent_type", label="Agent type"),
            Column(key="cost_observed", label="Cost observed", kind="money"),
            Column(key="fidelity_pct", label="Fidelity", kind="pct"),
            Column(key="recommendation", label="Recommendation"),
            Column(key="lever", label="Lever"),
        ],
        rows=rows,
    )


def test_ttl_switch_fires_for_row_recommending_a_switch():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="ttl",
            title="TTL",
            tables=[
                _ttl_by_agent_type_table(
                    [["top-level", 10.0, 0.0, "switch to 1h", "promptCacheTtl"]]
                )
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    rec = next(rec for rec in recs if rec.id == "ttl-switch")
    assert rec.lever == "promptCacheTtl"
    assert "1h" in rec.action
    assert rec.evidence == [
        ("TTL recommendation", "switch to 1h", "ttl.ttl_by_agent_type", "top-level"),
    ]


def test_ttl_switch_does_not_fire_for_no_material_difference():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="ttl",
            title="TTL",
            tables=[
                _ttl_by_agent_type_table(
                    [["top-level", 10.0, 0.0, "no material difference", "promptCacheTtl"]]
                )
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    assert not any(rec.id == "ttl-switch" for rec in recs)


def test_ttl_switch_suppressed_for_non_top_level_row_when_chat_only():
    # Fix R10: chat-only never spawns subagents, so per-agent-type TTL
    # advice for anything other than the session's own top-level row
    # should not fire -- the top-level row itself still can.
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="ttl",
            title="TTL",
            tables=[
                _ttl_by_agent_type_table(
                    [
                        ["top-level", 10.0, 0.0, "switch to 1h", "promptCacheTtl"],
                        ["claude-planner", 5.0, 0.0, "switch to 1h", "experimental.cacheTtl in claude-planner.md (or subagentPromptCacheTtl for all subagents)"],
                    ]
                )
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype="chat-only")
    ttl_recs = [rec for rec in recs if rec.id == "ttl-switch"]
    assert len(ttl_recs) == 1
    assert ttl_recs[0].agent_type == "top-level"


def test_ttl_switch_suppressed_for_non_anthropic_provider():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="ttl",
            title="TTL",
            tables=[
                _ttl_by_agent_type_table(
                    [["top-level", 10.0, 0.0, "switch to 1h", "promptCacheTtl"]]
                )
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(provider="bedrock"), archetype=None)
    assert not any(rec.id == "ttl-switch" for rec in recs)


def test_ttl_switch_allowed_for_anthropic_and_none_provider():
    for provider in (None, "anthropic"):
        r = _base_report()
        r = _add_section(
            r,
            Section(
                key="ttl",
                title="TTL",
                tables=[
                    _ttl_by_agent_type_table(
                        [["top-level", 10.0, 0.0, "switch to 1h", "promptCacheTtl"]]
                    )
                ],
            ),
        )
        recs = recommend_fn(r, config=_config(provider=provider), archetype=None)
        assert any(rec.id == "ttl-switch" for rec in recs), provider


def test_ttl_switch_managed_key_gains_managed_scope_and_action_text():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="ttl",
            title="TTL",
            tables=[
                _ttl_by_agent_type_table(
                    [["top-level", 10.0, 0.0, "switch to 1h", "promptCacheTtl"]]
                )
            ],
        ),
    )
    snapshot = Snapshot(path=Path("snap.json"), ts="20260918T000000Z", data={"managed_keys": ["promptCacheTtl"]})
    recs = recommend_fn(r, config=_config(), archetype=None, snapshot=snapshot)
    rec = next(rec for rec in recs if rec.id == "ttl-switch")
    assert rec.lever == "promptCacheTtl"
    assert rec.scope == "managed"
    assert "administrator" in rec.action


def test_ttl_switch_unmanaged_key_has_user_scope():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="ttl",
            title="TTL",
            tables=[
                _ttl_by_agent_type_table(
                    [["top-level", 10.0, 0.0, "switch to 1h", "promptCacheTtl"]]
                )
            ],
        ),
    )
    snapshot = Snapshot(path=Path("snap.json"), ts="20260918T000000Z", data={"managed_keys": ["model"]})
    recs = recommend_fn(r, config=_config(), archetype=None, snapshot=snapshot)
    rec = next(rec for rec in recs if rec.id == "ttl-switch")
    assert rec.lever == "promptCacheTtl"
    assert rec.scope == "user"
    assert "administrator" not in rec.action


def _subagent_ttl_report(agent_type: str, lever: str):
    r = _base_report()
    return _add_section(
        r,
        Section(
            key="ttl",
            title="TTL",
            tables=[_ttl_by_agent_type_table([[agent_type, 10.0, 0.0, "switch to 1h", lever]])],
        ),
    )


_AGENT_FILE_LEVER = "experimental.cacheTtl in claude-planner.md (or subagentPromptCacheTtl for all subagents)"


def test_ttl_switch_names_the_agent_file_when_subagent_setting_is_unset():
    snapshot = Snapshot(path=Path("snap.json"), ts="20260918T000000Z", data={"effective": {}})
    recs = recommend_fn(
        _subagent_ttl_report("claude-planner", _AGENT_FILE_LEVER), config=_config(), archetype=None, snapshot=snapshot
    )
    rec = next(rec for rec in recs if rec.id == "ttl-switch")
    assert rec.lever == _AGENT_FILE_LEVER
    assert [(c.target, c.key, c.agent, c.value) for c in rec.changes] == [
        ("agent", "experimental.cacheTtl", "claude-planner", "1h")
    ]


def test_ttl_switch_names_the_subagent_setting_when_it_already_outranks_agent_files():
    """Claude Code reads subagentPromptCacheTtl before an agent file's
    cacheTtl, so once it is set an agent-file edit does nothing: the
    card must change the setting and say it reaches every subagent."""
    snapshot = Snapshot(
        path=Path("snap.json"),
        ts="20260918T000000Z",
        data={
            "effective": {"subagentPromptCacheTtl": "5m"},
            "effective_provenance": {"subagentPromptCacheTtl": "project_shared"},
        },
    )
    recs = recommend_fn(
        _subagent_ttl_report("claude-planner", _AGENT_FILE_LEVER), config=_config(), archetype=None, snapshot=snapshot
    )
    rec = next(rec for rec in recs if rec.id == "ttl-switch")
    assert rec.lever == "subagentPromptCacheTtl"
    assert rec.scope == "repo"
    [change] = rec.changes
    assert (change.target, change.key, change.value, change.current) == (
        "settings",
        "subagentPromptCacheTtl",
        "1h",
        "5m",
    )
    assert "every subagent" in change.note
    assert "every subagent" in rec.action


def test_ttl_switch_for_subagents_with_no_recorded_type_changes_the_setting():
    snapshot = Snapshot(path=Path("snap.json"), ts="20260918T000000Z", data={"effective": {}})
    recs = recommend_fn(
        _subagent_ttl_report("unknown", "subagentPromptCacheTtl"), config=_config(), archetype=None, snapshot=snapshot
    )
    rec = next(rec for rec in recs if rec.id == "ttl-switch")
    assert rec.lever == "subagentPromptCacheTtl"
    [change] = rec.changes
    assert (change.target, change.key, change.value, change.current, change.note) == (
        "settings",
        "subagentPromptCacheTtl",
        "1h",
        None,
        "",
    )


def test_ttl_switch_fires_for_subagents_under_subscription_billing():
    """Subscription billing no longer suppresses a subagent's TTL advice
    (see ttl.build_section): within plan usage the 1h lifetime works."""
    config = _config()
    config.billing = "subscription"
    recs = recommend_fn(_subagent_ttl_report("claude-planner", _AGENT_FILE_LEVER), config=config, archetype=None)
    assert any(rec.id == "ttl-switch" and rec.agent_type == "claude-planner" for rec in recs)


# -- R3: per-row minimum-sample gate -----------------------------------


def test_ttl_switch_suppressed_for_low_sample_agent_type_despite_corpus_wide_pass():
    # Corpus-wide gate passes (_base_report's default sessions=10,
    # priced_turns=400), but this specific agent type only has 1 spawn
    # / 3 priced turns of its own -- a claude-planner-like row that
    # should get no per-agent-type advice even though the rest of the
    # corpus is large enough.
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="ttl",
            title="TTL",
            tables=[
                Table(
                    name="ttl_by_agent_type",
                    title="TTL by agent type",
                    columns=[
                        Column(key="agent_type", label="Agent type"),
                        Column(key="cost_observed", label="Cost observed"),
                        Column(key="fidelity_pct", label="Fidelity"),
                        Column(key="recommendation", label="Recommendation"),
                        Column(key="lever", label="Lever"),
                        Column(key="spawns", label="Spawns"),
                        Column(key="priced_turns", label="Priced turns"),
                    ],
                    rows=[
                        [
                            "claude-planner",
                            10.0,
                            0.0,
                            "switch to 1h",
                            "experimental.cacheTtl in claude-planner.md (or subagentPromptCacheTtl for all subagents)",
                            1,
                            3,
                        ]
                    ],
                )
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    assert not any(rec.id == "ttl-switch" for rec in recs)


def test_subagent_volume_suppressed_for_low_sample_agent_type():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="ttl",
            title="TTL",
            tables=[
                Table(
                    name="ttl_by_agent_type",
                    title="TTL by agent type",
                    columns=[
                        Column(key="agent_type", label="Agent type"),
                        Column(key="cost_observed", label="Cost observed"),
                        Column(key="fidelity_pct", label="Fidelity"),
                        Column(key="recommendation", label="Recommendation"),
                        Column(key="lever", label="Lever"),
                        Column(key="spawns", label="Spawns"),
                        Column(key="priced_turns", label="Priced turns"),
                    ],
                    rows=[
                        ["top-level", 40.0, 0.0, "no material difference", "promptCacheTtl", 10, 400],
                        ["claude-planner", 60.0, 0.0, "no material difference", "promptCacheTtl", 1, 3],
                    ],
                )
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    assert not any(rec.id == "subagent-volume" for rec in recs)


def _report_proxy_report(spawns: int, mean_proxy: float = 12_000) -> ReportModel:
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="agents",
            title="Agents",
            tables=[
                Table(
                    name="topology_report_proxy",
                    title="Report proxy",
                    columns=[
                        Column(key="agent_type", label="Agent type"),
                        Column(key="spawns", label="Spawns"),
                        Column(key="mean_proxy", label="Mean proxy"),
                    ],
                    rows=[["claude-planner", spawns, mean_proxy]],
                )
            ],
        ),
    )
    return r


def test_agent_report_size_suppressed_for_low_sample_via_ttl_cross_reference():
    # topology_report_proxy has no priced_turns column of its own; the
    # per-row gate cross-references ttl_by_agent_type's priced_turns
    # for the same agent type -- absent here, so it stays None and the
    # gate falls back to the (too-low) own spawns count.
    r = _report_proxy_report(spawns=1)
    r = _add_section(
        r,
        Section(
            key="ttl",
            title="TTL",
            tables=[_ttl_by_agent_type_table([["claude-planner", 10.0, 0.0, "no material difference", "promptCacheTtl"]])],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    assert not any(rec.id == "agent-report-size" for rec in recs)


def test_agent_report_size_fires_when_cross_referenced_priced_turns_clears_gate():
    # Same low own-spawns count as above, but this time
    # ttl_by_agent_type carries a priced_turns figure for the same
    # agent type that alone clears the minimum-sample bar -- proving
    # the cross-reference lookup (not just the table's own spawns
    # column) is actually consulted.
    r = _report_proxy_report(spawns=1)
    ttl_table = Table(
        name="ttl_by_agent_type",
        title="TTL by agent type",
        columns=[
            Column(key="agent_type", label="Agent type"),
            Column(key="cost_observed", label="Cost observed", kind="money"),
            Column(key="fidelity_pct", label="Fidelity", kind="pct"),
            Column(key="recommendation", label="Recommendation"),
            Column(key="lever", label="Lever"),
            Column(key="priced_turns", label="Priced turns"),
        ],
        rows=[["claude-planner", 10.0, 0.0, "no material difference", "promptCacheTtl", 500]],
    )
    r = _add_section(r, Section(key="ttl", title="TTL", tables=[ttl_table]))
    recs = recommend_fn(r, config=_config(), archetype=None)
    assert any(rec.id == "agent-report-size" for rec in recs)


def test_spawn_cost_suppressed_for_low_sample_via_ttl_cross_reference():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="agents",
            title="Agents",
            tables=[
                Table(
                    name="topology_spawn_write",
                    title="Spawn write",
                    columns=[
                        Column(key="agent_type", label="Agent type"),
                        Column(key="spawns", label="Spawns"),
                        Column(key="mean_first_call", label="Mean first call"),
                    ],
                    rows=[["claude-planner", 1, 110_000]],
                )
            ],
        ),
    )
    r = _add_section(
        r,
        Section(
            key="agent_startup",
            title="Agent startup",
            tables=[
                Table(
                    name="agent_startup_breakdown",
                    title="Startup",
                    columns=[
                        Column(key="agent_type", label="Agent type"),
                        Column(key="removable_tools", label="Tools never used"),
                    ],
                    rows=[["claude-planner", 8_000]],
                )
            ],
        ),
    )
    r = _add_section(
        r,
        Section(
            key="ttl",
            title="TTL",
            tables=[_ttl_by_agent_type_table([["claude-planner", 10.0, 0.0, "no material difference", "promptCacheTtl"]])],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    assert not any(rec.id == "spawn-cost" for rec in recs)


def test_from_config_seeds_min_sample_from_config_when_no_explicit_override():
    cfg = Config(min_sessions=3, min_turns=50)
    th = RecommendThresholds.from_config(None, cfg)
    assert th.min_sessions == 3
    assert th.min_turns == 50


def test_from_config_explicit_override_wins_over_config_default():
    cfg = Config(min_sessions=3, min_turns=50)
    th = RecommendThresholds.from_config({"min_sessions": 7}, cfg)
    assert th.min_sessions == 7
    assert th.min_turns == 50


def test_effective_min_sample_reflects_the_thresholds_actually_used():
    th = RecommendThresholds(min_sessions=9, min_turns=99)
    assert effective_min_sample(th) == (9, 99)


# -- subagent-volume ------------------------------------------------------


def test_subagent_volume_fires_above_threshold():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="ttl",
            title="TTL",
            tables=[
                _ttl_by_agent_type_table(
                    [
                        ["top-level", 40.0, 0.0, "no material difference", "promptCacheTtl"],
                        ["claude-implementer", 60.0, 0.0, "no material difference", "promptCacheTtl"],
                    ]
                )
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    rec = next(rec for rec in recs if rec.id == "subagent-volume")
    # R11: the cited evidence is the real cost_observed cell (60.0 is
    # claude-implementer's own cost_observed value in the fixture below,
    # not a derived share) -- the computed 60% share lives in the
    # action text instead.
    assert rec.evidence == [
        ("Cost (observed)", 60.0, "ttl.ttl_by_agent_type", "claude-implementer"),
    ]
    assert "60.0%" in rec.why


def _cost_centres_table(rows: list[list]) -> Table:
    from claudeglass import cost_centres

    return Table(
        name="cost_centres",
        title="Spend by cost centre",
        columns=[
            Column(key="centre", label="Cost centre"),
            *[Column(key=cell, label=cell, kind="money") for cell in cost_centres.CELLS],
            Column(key="total", label="Total", kind="money"),
        ],
        rows=rows,
    )


def test_subagent_volume_names_the_largest_cost_centre_and_cites_its_row():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="ttl",
            title="TTL",
            tables=[
                _ttl_by_agent_type_table(
                    [
                        ["top-level", 40.0, 0.0, "no material difference", "promptCacheTtl"],
                        ["claude-implementer", 60.0, 0.0, "no material difference", "promptCacheTtl"],
                    ]
                )
            ],
        ),
    )
    r = _add_section(
        r,
        Section(
            key="agents",
            title="Agents",
            tables=[
                _cost_centres_table(
                    [
                        ["main", 10.0, 50.0, 5.0, 0.0, 0.0, 5.0, 70.0],
                        ["direct", 4.0, 6.0, 2.0, 0.0, 0.0, 3.0, 15.0],
                    ]
                )
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    rec = next(rec for rec in recs if rec.id == "subagent-volume")
    assert ("Largest cost centre", 70.0, "agents.cost_centres", "main") in rec.evidence
    assert ("Cost (observed)", 60.0, "ttl.ttl_by_agent_type", "claude-implementer") in rec.evidence
    # 70 of 85 is 82%, mostly above-base read.
    assert "the largest cost centre is main session (82%)" in rec.why
    assert "above-base read" in rec.why


def test_subagent_volume_without_a_cost_centre_table_cites_only_the_agent_type():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="ttl",
            title="TTL",
            tables=[
                _ttl_by_agent_type_table(
                    [
                        ["top-level", 40.0, 0.0, "no material difference", "promptCacheTtl"],
                        ["claude-implementer", 60.0, 0.0, "no material difference", "promptCacheTtl"],
                    ]
                )
            ],
        ),
    )
    rec = next(rec for rec in recommend_fn(r, config=_config(), archetype=None) if rec.id == "subagent-volume")
    assert len(rec.evidence) == 1 and "cost centre" not in rec.why


def test_subagent_volume_does_not_fire_below_threshold():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="ttl",
            title="TTL",
            tables=[
                _ttl_by_agent_type_table(
                    [
                        ["top-level", 70.0, 0.0, "no material difference", "promptCacheTtl"],
                        ["claude-implementer", 30.0, 0.0, "no material difference", "promptCacheTtl"],
                    ]
                )
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    assert not any(rec.id == "subagent-volume" for rec in recs)


def test_subagent_volume_suppressed_for_overseer_fanout():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="ttl",
            title="TTL",
            tables=[
                _ttl_by_agent_type_table(
                    [
                        ["top-level", 40.0, 0.0, "no material difference", "promptCacheTtl"],
                        ["claude-implementer", 60.0, 0.0, "no material difference", "promptCacheTtl"],
                    ]
                )
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype="overseer-fanout")
    assert not any(rec.id == "subagent-volume" for rec in recs)


def _workstyle_section(rows: list[list]) -> Section:
    """The workstyle section, with each row ``[archetype, sessions, spend]``."""
    return Section(
        key="workstyle",
        title="Workstyle",
        tables=[
            Table(
                name="workstyle_archetypes",
                title="Workstyle archetypes",
                columns=[
                    Column(key="archetype", label="Archetype"),
                    Column(key="sessions", label="Sessions", kind="int"),
                    Column(key="pct", label="Share", kind="pct"),
                    Column(key="spend", label="Spend", kind="money"),
                    Column(key="description", label="Description"),
                ],
                rows=[[name, sessions, None, spend, ""] for name, sessions, spend in rows],
            )
        ],
    )


def _volume_report(workstyle_rows: list[list] | None) -> ReportModel:
    """A report in which claude-implementer is 60% of the spend, so
    subagent-volume fires unless the fan-out gate holds it back, with a
    workstyle section made of ``workstyle_rows`` (none when ``None``)."""
    r = _add_section(
        _base_report(),
        Section(
            key="ttl",
            title="TTL",
            tables=[
                _ttl_by_agent_type_table(
                    [
                        ["top-level", 40.0, 0.0, "no material difference", "promptCacheTtl"],
                        ["claude-implementer", 60.0, 0.0, "no material difference", "promptCacheTtl"],
                    ]
                )
            ],
        ),
    )
    if workstyle_rows is not None:
        _add_section(r, _workstyle_section(workstyle_rows))
    return r


def _fires(r: ReportModel, archetype: str | None = None, config: Config | None = None) -> bool:
    recs = recommend_fn(r, config=config or _config(), archetype=archetype)
    return any(rec.id == "subagent-volume" for rec in recs)


def test_subagent_volume_is_held_back_when_fanout_sessions_are_30_percent_of_the_spend():
    # 30 of 100 dollars, in 1 session of 11: not the corpus's biggest
    # archetype by sessions or even by spend, but a third of its money.
    rows = [["single-model", 6, 50.0], ["overseer-fanout", 1, 30.0], ["chat-only", 4, 20.0]]
    assert not _fires(_volume_report(rows))
    assert not _fires(_volume_report(rows), archetype="single-model")


def test_subagent_volume_fires_when_fanout_sessions_are_under_30_percent_of_the_spend():
    rows = [["single-model", 6, 70.1], ["overseer-fanout", 4, 29.9]]
    assert _fires(_volume_report(rows))


def test_the_spend_share_beats_the_corpus_archetype_when_there_is_a_workstyle_table():
    rows = [["single-model", 6, 90.0], ["overseer-fanout", 4, 10.0]]
    # The caller says fan-out; the money says otherwise.
    assert _fires(_volume_report(rows), archetype="overseer-fanout")


def test_subagent_volume_falls_back_to_the_archetype_without_a_workstyle_table():
    assert not _fires(_volume_report(None), archetype="overseer-fanout")
    assert _fires(_volume_report(None), archetype="single-model")
    assert _fires(_volume_report(None), archetype=None)


def test_subagent_volume_falls_back_to_the_archetype_when_the_workstyle_table_has_no_spend():
    rows = [["single-model", 6, None], ["overseer-fanout", 4, None]]
    assert not _fires(_volume_report(rows), archetype="overseer-fanout")
    assert _fires(_volume_report(rows), archetype="single-model")
    free = [["single-model", 6, 0.0], ["overseer-fanout", 4, 0.0]]
    assert not _fires(_volume_report(free), archetype="overseer-fanout")


def test_the_fanout_share_threshold_is_30_by_default_and_configurable():
    assert RecommendThresholds().fanout_spend_share_pct == 30.0
    rows = [["single-model", 6, 60.0], ["overseer-fanout", 4, 40.0]]
    assert not _fires(_volume_report(rows))
    config = Config(thresholds={"recommend": {"fanout_spend_share_pct": 50}})
    assert _fires(_volume_report(rows), config=config)


def test_fanout_spend_share_pct_reads_the_spend_column():
    rows = [["single-model", 6, 75.0], ["overseer-fanout", 4, 25.0]]
    assert recommend._fanout_spend_share_pct(_volume_report(rows)) == pytest.approx(25.0)
    assert recommend._fanout_spend_share_pct(_volume_report([])) is None
    assert recommend._fanout_spend_share_pct(_volume_report(None)) is None


# -- compaction-churn ------------------------------------------------------


def _compactions_summary_table(rows: dict[str, float]) -> Table:
    return Table(
        name="compactions_summary",
        title="Compactions summary",
        columns=[Column(key="metric", label="Metric"), Column(key="value", label="Value")],
        rows=[[k, v] for k, v in rows.items()],
    )


def test_compaction_churn_fires_on_mean_per_session():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="compactions",
            title="Compactions",
            tables=[
                _compactions_summary_table(
                    {
                        "Compactions per session (mean)": 3.0,
                        "Dropped tokens (share of new_tokens: input+cache_creation)": 5.0,
                    }
                )
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    rec = next(rec for rec in recs if rec.id == "compaction-churn")
    assert rec.lever == "autoCompactWindow"
    assert ("Compactions per session (mean)", 3.0, "compactions.compactions_summary", "Compactions per session (mean)") in rec.evidence


def test_compaction_churn_fires_on_dropped_share():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="compactions",
            title="Compactions",
            tables=[
                _compactions_summary_table(
                    {
                        "Compactions per session (mean)": 0.5,
                        "Dropped tokens (share of new_tokens: input+cache_creation)": 45.0,
                    }
                )
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    assert any(rec.id == "compaction-churn" for rec in recs)


def test_compaction_churn_does_not_fire_below_both_thresholds():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="compactions",
            title="Compactions",
            tables=[
                _compactions_summary_table(
                    {
                        "Compactions per session (mean)": 0.5,
                        "Dropped tokens (share of new_tokens: input+cache_creation)": 5.0,
                    }
                )
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    assert not any(rec.id == "compaction-churn" for rec in recs)


def test_compaction_churn_managed_lever():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="compactions",
            title="Compactions",
            tables=[_compactions_summary_table({"Compactions per session (mean)": 3.0})],
        ),
    )
    snapshot = Snapshot(path=Path("s.json"), ts="20260918T000000Z", data={"managed_keys": ["autoCompactWindow"]})
    recs = recommend_fn(r, config=_config(), archetype=None, snapshot=snapshot)
    rec = next(rec for rec in recs if rec.id == "compaction-churn")
    assert rec.lever == "autoCompactWindow"
    assert rec.scope == "managed"
    assert "administrator" in rec.action


# -- long-context-share ---------------------------------------------------


def test_long_context_share_fires_on_huge_context_share():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="recache",
            title="Recache",
            tables=[
                Table(
                    name="recache_huge_context",
                    title="Huge context",
                    columns=[
                        Column(key="metric", label="Metric"),
                        Column(key="count", label="Count"),
                        Column(key="share_pct", label="Share", kind="pct"),
                    ],
                    rows=[["all", 3, 25.0]],
                )
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    rec = next(rec for rec in recs if rec.id == "long-context-share")
    assert ("Cache-read volume share from huge-context turns", 25.0, "recache.recache_huge_context", "all") in rec.evidence


def test_long_context_share_fires_on_p90_ctx():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="scorecard",
            title="Scorecard",
            tables=[
                Table(
                    name="dimensions",
                    title="Dimensions",
                    columns=[
                        Column(key="dimension", label="Dimension"),
                        Column(key="level", label="Level"),
                        Column(key="label", label="Label"),
                        Column(key="metric", label="Metric"),
                        Column(key="value", label="Value"),
                        Column(key="threshold", label="Threshold"),
                    ],
                    rows=[["context_hygiene", "warn", "Context hygiene", "p90_top_level_ctx", 200_000, 150_000]],
                )
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    rec = next(rec for rec in recs if rec.id == "long-context-share")
    assert (
        "Context size 9 in 10 main-session replies stay under",
        200_000,
        "scorecard.dimensions",
        "context_hygiene",
    ) in rec.evidence


def test_long_context_share_does_not_fire_below_both():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="recache",
            title="Recache",
            tables=[
                Table(
                    name="recache_huge_context",
                    title="Huge context",
                    columns=[
                        Column(key="metric", label="Metric"),
                        Column(key="count", label="Count"),
                        Column(key="share_pct", label="Share", kind="pct"),
                    ],
                    rows=[["all", 0, 5.0]],
                )
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    assert not any(rec.id == "long-context-share" for rec in recs)


# -- baseline-bloat ---------------------------------------------------------


def _baseline_report(r, *, controllable=35_000, first_call=95_000, project="proj"):
    """``r`` with the context-budget baseline rows baseline-bloat reads: a
    project row (and the "all" row built with no config snapshot, so no
    memory files) sizing the part of the first call a setting can change
    next to the whole first call, which is mostly Claude Code's own tools."""
    columns = [
        Column(key="project", label="Project"),
        Column(key="sessions", label="Sessions"),
        Column(key="mean_baseline", label="Mean first call"),
        Column(key="human_prompt_est", label="Human prompt"),
        Column(key="skills_listing_est", label="Skills listing"),
        Column(key="memory_files_est", label="Memory files"),
        Column(key="mcp_removable_tokens", label="MCP tools you can turn off"),
        Column(key="controllable_est", label="What you can change"),
        Column(key="system_prompt_and_tools_est", label="System prompt and tools"),
    ]
    split = controllable / 5
    rows = [
        ["all", 10, first_call, 300, split, None, split, split * 2, first_call - controllable],
        [project, 10, first_call, 300, split, split, split * 2, controllable, first_call - controllable - 300],
    ]
    return _add_section(
        r,
        Section(
            key="context_budget",
            title="Context budget",
            tables=[Table(name="context_budget_baseline", title="Baseline", columns=columns, rows=rows)],
        ),
    )



def test_baseline_bloat_fires_with_snapshot_evidence():
    r = _base_report()
    r = _baseline_report(r)
    # Fix #20: mcp_servers is the fixed three-key dict the hook actually
    # emits (hooks/snapshot-config.py) -- server names live under
    # "names", not as top-level dict keys -- and enabled_plugins is a
    # plain list, not a dict.
    snapshot = Snapshot(
        path=Path("s.json"),
        ts="20260918T000000Z",
        data={
            "mcp_servers": {"names": ["a", "b", "c"], "enabled_mcpjson_servers": [], "disabled_mcpjson_servers": []},
            "enabled_plugins": ["x", "y"],
        },
    )
    recs = recommend_fn(r, config=_config(), archetype=None, snapshot=snapshot)
    rec = next(rec for rec in recs if rec.id == "baseline-bloat")
    # No lever: MCP servers aren't a settings key, so --patch-set has
    # nothing to write for it, and no one settings file for a scope chip.
    assert rec.lever is None and rec.scope == ""
    table = "context_budget.context_budget_baseline"
    assert rec.evidence == [
        ("What you can change at the start of a session (est)", 35_000, table, "proj"),
        ("Estimated human prompt (est)", 300, table, "proj"),
        ("Estimated skills listing (est)", 7_000, table, "proj"),
        ("Estimated memory files (est)", 7_000, table, "proj"),
        ("Estimated MCP tools you can turn off (est)", 14_000, table, "proj"),
        ("Mean first call (measured)", 95_000, table, "proj"),
        ("Estimated system prompt and tools (est)", 59_700, table, "proj"),
    ]
    # Only what a setting can change competes for "largest".
    raw = recommend._rule_baseline_bloat(r, RecommendThresholds(), snapshot, None)[0]
    assert "largest estimated share of that baseline is MCP tools" in raw.action


def test_baseline_bloat_fires_without_snapshot():
    r = _base_report()
    r = _baseline_report(r)
    recs = recommend_fn(r, config=_config(), archetype=None, snapshot=None)
    assert any(rec.id == "baseline-bloat" for rec in recs)


def _baseline_with_custom_agents(custom_agents):
    r = _baseline_report(_base_report())
    table = r.sections[-1].tables[0]
    table.columns.insert(5, Column(key="custom_agents_est", label="Custom agents (est)"))
    for row in table.rows:
        row.insert(5, custom_agents)
    return r


def test_baseline_bloat_suggests_trimming_agent_descriptions_only_when_they_are_a_real_share():
    snapshot = Snapshot(
        path=Path("s.json"),
        ts="20260918T000000Z",
        data={
            "mcp_servers": {"names": ["a", "b", "c"], "enabled_mcpjson_servers": [], "disabled_mcpjson_servers": []},
            "enabled_plugins": ["x", "y"],
        },
    )
    big = recommend_fn(_baseline_with_custom_agents(1_800.0), config=_config(), archetype=None, snapshot=snapshot)
    action = next(rec for rec in big if rec.id == "baseline-bloat").action
    assert "Your own agents' descriptions add about 1,800 tokens to every session" in action
    assert "Shortening the description line in their agent files trims that" in action
    small = recommend_fn(_baseline_with_custom_agents(400.0), config=_config(), archetype=None, snapshot=snapshot)
    action = next(rec for rec in small if rec.id == "baseline-bloat").action
    assert "descriptions" not in action
    # Exactly the threshold counts.
    edge = recommend_fn(_baseline_with_custom_agents(1_000.0), config=_config(), archetype=None, snapshot=snapshot)
    assert "descriptions add about 1,000 tokens" in next(rec for rec in edge if rec.id == "baseline-bloat").action


def test_baseline_bloat_ignores_config_counts():
    r = _base_report()
    r = _baseline_report(r)
    snapshot = Snapshot(
        path=Path("s.json"), ts="20260918T000000Z", data={"mcp_servers": {"names": ["a"]}}
    )
    recs = recommend_fn(r, config=_config(), archetype=None, snapshot=snapshot)
    assert any(rec.id == "baseline-bloat" for rec in recs)


def test_baseline_bloat_suppressed_for_chat_only():
    r = _base_report()
    r = _baseline_report(r)
    snapshot = Snapshot(
        path=Path("s.json"),
        ts="20260918T000000Z",
        data={"mcp_servers": {"names": ["a", "b", "c", "d", "e"]}},
    )
    recs = recommend_fn(r, config=_config(), archetype="chat-only", snapshot=snapshot)
    assert not any(rec.id == "baseline-bloat" for rec in recs)


# -- COV-09: env-var / deprecated-setting lever rules ------------------------


def test_env_disable_prompt_caching_fires_on_any_variant():
    r = _base_report()
    snapshot = Snapshot(
        path=Path("s.json"),
        ts="20260918T000000Z",
        data={"env_names": ["DISABLE_PROMPT_CACHING_SONNET"], "effective_env_provenance": {}},
    )
    recs = recommend_fn(r, config=_config(), archetype=None, snapshot=snapshot)
    rec = next(rec for rec in recs if rec.id == "env-disable-prompt-caching")
    assert rec.severity == "action"
    assert rec.lever == "env:DISABLE_PROMPT_CACHING_SONNET"
    assert rec.scope == "user"
    assert rec.evidence == [
        ("DISABLE_PROMPT_CACHING_SONNET", True, "config.env-levers", "DISABLE_PROMPT_CACHING_SONNET"),
    ]
    # COV-07/COV-11: a real (unset-suggesting) SettingChange, not just prose.
    assert len(rec.changes) == 1
    change = rec.changes[0]
    assert change.target == "settings" and change.key == "env.DISABLE_PROMPT_CACHING_SONNET"
    assert change.value is None and change.suggested and change.scope == "user"


def test_env_disable_prompt_caching_managed_scope_and_action_note():
    r = _base_report()
    snapshot = Snapshot(
        path=Path("s.json"),
        ts="20260918T000000Z",
        data={
            "env_names": ["DISABLE_PROMPT_CACHING"],
            "effective_env_provenance": {"DISABLE_PROMPT_CACHING": "managed"},
        },
    )
    recs = recommend_fn(r, config=_config(), archetype=None, snapshot=snapshot)
    rec = next(rec for rec in recs if rec.id == "env-disable-prompt-caching")
    assert rec.scope == "managed"
    assert "administrator" in rec.action
    # fixes.command_for suppresses a command for scope="managed" regardless,
    # so the change can carry the managed scope through without a
    # separate branch here.
    assert rec.changes[0].scope == "managed"


def test_env_disable_prompt_caching_does_not_fire_when_unset():
    r = _base_report()
    snapshot = Snapshot(path=Path("s.json"), ts="20260918T000000Z", data={"env_names": []})
    recs = recommend_fn(r, config=_config(), archetype=None, snapshot=snapshot)
    assert not any(rec.id == "env-disable-prompt-caching" for rec in recs)


def test_env_tool_search_fires_on_base_url_without_tool_search():
    r = _base_report()
    snapshot = Snapshot(
        path=Path("s.json"),
        ts="20260918T000000Z",
        data={
            "env_names": ["ANTHROPIC_BASE_URL"],
            # baseline_bloat_min_mcp_or_plugins defaults to 5 -- this rule
            # reuses that same threshold (see its own docstring).
            "mcp_servers": {"names": ["a", "b", "c", "d", "e"]},
        },
    )
    recs = recommend_fn(r, config=_config(), archetype=None, snapshot=snapshot)
    rec = next(rec for rec in recs if rec.id == "env-tool-search")
    assert rec.lever == "env:ENABLE_TOOL_SEARCH"
    assert rec.evidence == [("ENABLE_TOOL_SEARCH", False, "config.env-levers", "ENABLE_TOOL_SEARCH")]
    # COV-07/COV-11: the one COV-09 env rule with a concrete proposed
    # value, so it's the one that gets a real apply --dry-run command.
    assert len(rec.changes) == 1
    change = rec.changes[0]
    assert change.target == "settings" and change.key == "env.ENABLE_TOOL_SEARCH"
    assert change.value == "true" and change.scope == "user"


def test_env_tool_search_does_not_fire_when_already_set():
    r = _base_report()
    snapshot = Snapshot(
        path=Path("s.json"),
        ts="20260918T000000Z",
        data={
            "env_names": ["ANTHROPIC_BASE_URL", "ENABLE_TOOL_SEARCH"],
            "mcp_servers": {"names": ["a", "b", "c", "d", "e"]},
        },
    )
    recs = recommend_fn(r, config=_config(), archetype=None, snapshot=snapshot)
    assert not any(rec.id == "env-tool-search" for rec in recs)


def test_env_tool_search_does_not_fire_with_too_few_mcp_servers():
    r = _base_report()
    snapshot = Snapshot(
        path=Path("s.json"),
        ts="20260918T000000Z",
        data={"env_names": ["ANTHROPIC_BASE_URL"], "mcp_servers": {"names": ["a"]}},
    )
    recs = recommend_fn(r, config=_config(), archetype=None, snapshot=snapshot)
    assert not any(rec.id == "env-tool-search" for rec in recs)


def test_env_max_output_tokens_fires_with_value_and_optional_compaction_evidence():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="compactions",
            title="Compactions",
            tables=[
                Table(
                    name="compactions_summary",
                    title="Compactions summary",
                    columns=[Column(key="metric", label="Metric"), Column(key="value", label="Value")],
                    rows=[["Compactions per session (mean)", 2.5]],
                )
            ],
        ),
    )
    snapshot = Snapshot(
        path=Path("s.json"),
        ts="20260918T000000Z",
        data={"env_numeric_caps": {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": 64000}},
    )
    recs = recommend_fn(r, config=_config(), archetype=None, snapshot=snapshot)
    rec = next(rec for rec in recs if rec.id == "env-max-output-tokens")
    assert "64,000" in rec.action
    assert "2.5" in rec.action
    assert (
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
        "64000",
        "config.env-levers",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
    ) in rec.evidence
    assert (
        "Compactions per session (mean)",
        2.5,
        "compactions.compactions_summary",
        "Compactions per session (mean)",
    ) in rec.evidence
    # COV-07/COV-11: no concrete proposed value (apply has no "unset"
    # primitive), so this is a SettingChange with value=None/suggested,
    # not a real apply command -- but it does carry a change (unlike
    # env-subagent-model/env-attribution-deprecated below).
    assert len(rec.changes) == 1
    change = rec.changes[0]
    assert change.target == "settings" and change.key == "env.CLAUDE_CODE_MAX_OUTPUT_TOKENS"
    assert change.value is None and change.suggested


def test_env_max_output_tokens_does_not_fire_when_unset():
    r = _base_report()
    snapshot = Snapshot(path=Path("s.json"), ts="20260918T000000Z", data={"env_numeric_caps": {}})
    recs = recommend_fn(r, config=_config(), archetype=None, snapshot=snapshot)
    assert not any(rec.id == "env-max-output-tokens" for rec in recs)


def test_env_subagent_model_fires_and_names_the_order():
    r = _base_report()
    snapshot = Snapshot(
        path=Path("s.json"), ts="20260918T000000Z", data={"env_names": ["CLAUDE_CODE_SUBAGENT_MODEL"]}
    )
    recs = recommend_fn(r, config=_config(), archetype=None, snapshot=snapshot)
    rec = next(rec for rec in recs if rec.id == "env-subagent-model")
    assert "Explore" in rec.action and "Plan" in rec.action
    assert rec.severity == "info"
    # COV-07/COV-11: purely informational -- no proposed value at all --
    # so it stays prose-only via fixes._WORKFLOW_EXPLAINER, no SettingChange.
    assert rec.changes == []


def test_env_subagent_model_suppressed_for_chat_only():
    r = _base_report()
    snapshot = Snapshot(
        path=Path("s.json"), ts="20260918T000000Z", data={"env_names": ["CLAUDE_CODE_SUBAGENT_MODEL"]}
    )
    recs = recommend_fn(r, config=_config(), archetype="chat-only", snapshot=snapshot)
    assert not any(rec.id == "env-subagent-model" for rec in recs)


def test_attribution_deprecated_fires_when_only_include_co_authored_by_set():
    r = _base_report()
    snapshot = Snapshot(
        path=Path("s.json"), ts="20260918T000000Z", data={"effective": {"includeCoAuthoredBy": False}}
    )
    recs = recommend_fn(r, config=_config(), archetype=None, snapshot=snapshot)
    rec = next(rec for rec in recs if rec.id == "env-attribution-deprecated")
    assert rec.lever == "includeCoAuthoredBy"
    assert rec.evidence == [
        ("includeCoAuthoredBy", "False", "config.env-levers", "includeCoAuthoredBy"),
    ]
    # COV-07/COV-11: the real target, attribution.commit, isn't on
    # SETTINGS_ALLOWLIST, so this stays prose-only via
    # fixes._WORKFLOW_PROMPTS/_WORKFLOW_EXPLAINER, no SettingChange.
    assert rec.changes == []


def test_attribution_deprecated_does_not_fire_once_attribution_is_set():
    r = _base_report()
    snapshot = Snapshot(
        path=Path("s.json"),
        ts="20260918T000000Z",
        data={"effective": {"includeCoAuthoredBy": False, "attribution": {"commit_set": True}}},
    )
    recs = recommend_fn(r, config=_config(), archetype=None, snapshot=snapshot)
    assert not any(rec.id == "env-attribution-deprecated" for rec in recs)


def test_attribution_deprecated_does_not_fire_when_neither_set():
    r = _base_report()
    snapshot = Snapshot(path=Path("s.json"), ts="20260918T000000Z", data={"effective": {}})
    recs = recommend_fn(r, config=_config(), archetype=None, snapshot=snapshot)
    assert not any(rec.id == "env-attribution-deprecated" for rec in recs)


def test_baseline_bloat_does_not_fire_on_the_harness_floor_alone():
    """A 95k first call with 5k of it a setting can change is Claude Code's
    own tool JSON: the raw size used to fire this card, and says nothing
    about the user's configuration."""
    r = _baseline_report(_base_report(), controllable=5_000, first_call=95_000)
    snapshot = Snapshot(
        path=Path("s.json"),
        ts="20260918T000000Z",
        data={"mcp_servers": {"names": ["a", "b", "c", "d", "e"]}},
    )
    recs = recommend_fn(r, config=_config(), archetype=None, snapshot=snapshot)
    assert not any(rec.id == "baseline-bloat" for rec in recs)


def test_baseline_bloat_fires_on_the_controllable_part_whatever_the_first_call():
    r = _baseline_report(_base_report(), controllable=30_000, first_call=40_000)
    snapshot = Snapshot(
        path=Path("s.json"),
        ts="20260918T000000Z",
        data={"mcp_servers": {"names": ["a", "b", "c", "d", "e"]}},
    )
    recs = recommend_fn(r, config=_config(), archetype=None, snapshot=snapshot)
    assert any(rec.id == "baseline-bloat" for rec in recs)


def test_baseline_bloat_does_not_fire_without_the_context_budget_table():
    r = _base_report()
    snapshot = Snapshot(
        path=Path("s.json"),
        ts="20260918T000000Z",
        data={"mcp_servers": {"names": ["a", "b", "c", "d", "e"]}},
    )
    recs = recommend_fn(r, config=_config(), archetype=None, snapshot=snapshot)
    assert not any(rec.id == "baseline-bloat" for rec in recs)


# -- agent-report-size ------------------------------------------------------


def test_agent_report_size_fires_per_agent_type():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="agents",
            title="Agents",
            tables=[
                Table(
                    name="topology_report_proxy",
                    title="Report proxy",
                    columns=[Column(key="agent_type", label="Agent type"), Column(key="mean_proxy", label="Mean proxy")],
                    rows=[["claude-implementer", 12_000], ["haiku-sweeper", 2_000]],
                )
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    fired = [rec for rec in recs if rec.id == "agent-report-size"]
    assert len(fired) == 1
    assert fired[0].evidence == [
        ("Mean report size (tokens)", 12_000, "agents.topology_report_proxy", "claude-implementer"),
    ]


def test_agent_report_size_suppressed_for_chat_only():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="agents",
            title="Agents",
            tables=[
                Table(
                    name="topology_report_proxy",
                    title="Report proxy",
                    columns=[Column(key="agent_type", label="Agent type"), Column(key="mean_proxy", label="Mean proxy")],
                    rows=[["claude-implementer", 12_000]],
                )
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype="chat-only")
    assert not any(rec.id == "agent-report-size" for rec in recs)


# -- agent-batch-probes ------------------------------------------------------


def _probes_section(*rows) -> Section:
    """The Work habits section's ``habits_probes`` table, each row
    ``(where, replies, single read-only calls, of them by shell, runs, cost of the replies after the first)``."""
    return Section(
        key="habits",
        title="Work habits",
        tables=[
            Table(
                name="habits_probes",
                title="Single lookups, one call per reply",
                columns=[
                    Column(key="agent_type", label="Where"),
                    Column(key="calls", label="Replies"),
                    Column(key="probes", label="Single read-only calls"),
                    Column(key="shell", label="Of them by shell command"),
                    Column(key="runs", label="Runs of two or more"),
                    Column(key="batch_cost", label="Replies a batch would spare", kind="money"),
                ],
                rows=[list(row) for row in rows],
            )
        ],
    )


def _batch_probe_recs(*rows, archetype=None):
    r = _add_section(_base_report(), _probes_section(*rows))
    return [rec for rec in recommend_fn(r, config=_config(), archetype=archetype) if rec.id == "agent-batch-probes"]


def test_agent_batch_probes_fires_per_agent_type_with_half_the_tables_upper_bound():
    fired = _batch_probe_recs(
        ["top-level", 900, 500, 50, 120, 90.0],
        ["Explore", 400, 190, 30, 55, 12.0],
        ["claude-implementer", 300, 100, 0, 20, 4.0],
        ["haiku-sweeper", 400, 20, 0, 3, 40.0],
    )
    # The main session's row is yours to prompt; a type with few lookups is left alone.
    assert [(rec.agent_type, rec.severity, rec.category, rec.lever) for rec in fired] == [
        ("Explore", "advice", "workflow", None),
        ("claude-implementer", "advice", "workflow", None),
    ]
    explore = fired[0]
    assert explore.saving_usd == pytest.approx(6.0)
    assert explore.evidence == [
        ("Replies it made", 400, "habits.habits_probes", "Explore"),
        ("Single read-only calls", 190, "habits.habits_probes", "Explore"),
        ("Of them by shell command", 30, "habits.habits_probes", "Explore"),
        ("Replies a batch would spare", 12.0, "habits.habits_probes", "Explore"),
    ]
    # The action is the line to paste, word for word.
    assert "Batch independent Read/Grep/Glob calls into a single message" in explore.action
    assert fired[1].saving_usd == pytest.approx(2.0)


@pytest.mark.parametrize(
    "row, why",
    [
        (["Explore", 99, 90, 0, 20, 50.0], "under 100 replies"),
        (["Explore", 400, 99, 0, 20, 50.0], "under a quarter of the replies"),
        (["Explore", 400, 200, 0, 20, 1.9], "under $1 once halved"),
        (["Explore", 400, 200, 0, 20, "n/a"], "no figure for the cost"),
        (["Explore", None, 200, 0, 20, 50.0], "no count of replies"),
    ],
)
def test_agent_batch_probes_stays_quiet_below_its_thresholds_or_without_numbers(row, why):
    assert _batch_probe_recs(row) == [], why


def test_agent_batch_probes_fires_at_exactly_its_thresholds():
    assert len(_batch_probe_recs(["Explore", 100, 25, 0, 4, 2.0])) == 1


def test_agent_batch_probes_needs_the_probes_table_and_its_columns():
    r = _base_report()
    assert not any(rec.id == "agent-batch-probes" for rec in recommend_fn(r, config=_config(), archetype=None))
    section = _probes_section(["Explore", 400, 200, 0, 20, 50.0])
    del section.tables[0].columns[-1]
    r = _add_section(_base_report(), section)
    assert not any(rec.id == "agent-batch-probes" for rec in recommend_fn(r, config=_config(), archetype=None))


def test_agent_batch_probes_is_suppressed_for_chat_only():
    assert _batch_probe_recs(["Explore", 400, 200, 0, 20, 50.0], archetype="chat-only") == []


def test_agent_batch_probes_thresholds_come_from_the_config():
    th = RecommendThresholds.from_config(
        {"agent_batch_probes_min_replies": 20, "agent_batch_probes_share_pct": 10, "agent_batch_probes_saving_factor": 1}
    )
    assert (th.agent_batch_probes_min_replies, th.agent_batch_probes_share_pct) == (20, 10.0)
    r = _add_section(_base_report(), _probes_section(["Explore", 30, 5, 0, 1, 1.5]))
    config = Config(thresholds={"recommend": {"agent_batch_probes_min_replies": 20, "agent_batch_probes_share_pct": 10,
                                              "agent_batch_probes_saving_factor": 1}})
    recs = recommend_fn(r, config=config, archetype=None)
    (rec,) = [rec for rec in recs if rec.id == "agent-batch-probes"]
    assert rec.saving_usd == pytest.approx(1.5)


# -- plan-rounds -------------------------------------------------------------


def _rounds_section(*rows) -> Section:
    """The Work habits section's ``habits_plan_rounds`` table, each row
    ``(kind, plans, typed, rounds, asked, cost)``."""
    return Section(
        key="habits",
        title="Work habits",
        tables=[
            Table(
                name="habits_plan_rounds",
                title="Plans sent back",
                columns=[
                    Column(key="kind", label="Which plans"),
                    Column(key="plans", label="Plans"),
                    Column(key="typed", label="Approved by typing"),
                    Column(key="rounds", label="Plans sent back"),
                    Column(key="asked", label="Sent back with a question or critique"),
                    Column(key="cost", label="Cost between the first plan and approval", kind="money"),
                ],
                rows=[list(row) for row in rows],
            )
        ],
    )


def _plan_round_recs(*rows, config=None):
    r = _add_section(_base_report(), _rounds_section(*rows))
    return [rec for rec in recommend_fn(r, config=config or _config(), archetype=None) if rec.id == "plan-rounds"]


#: Ten approved plans, six sent back at least once (twelve rounds, six a
#: question or critique), 40.00 spent on the rounds.
_ROUNDS_ALL = ["all", 10, 3, 12, 6, 40.0]
_ROUNDS_NONE = ["none", 4, 1, 0, 0, 0.0]


def test_plan_rounds_fires_with_a_quarter_of_the_rounds_cost_scaled_to_the_questions_and_critiques():
    (rec,) = _plan_round_recs(_ROUNDS_ALL, _ROUNDS_NONE)
    assert (rec.severity, rec.category, rec.lever, rec.agent_type) == ("advice", "workflow", None, None)
    assert rec.title == "Plans keep being sent back"
    # 40.00 of rounds, half of them a question or a critique, a quarter of that.
    assert rec.saving_usd == pytest.approx(5.0)
    assert rec.evidence == [
        ("Plans you approved", 10, "habits.habits_plan_rounds", "all"),
        ("Never sent back", 4, "habits.habits_plan_rounds", "none"),
        ("Times plans were sent back", 12, "habits.habits_plan_rounds", "all"),
        ("Sent back with a question or critique", 6, "habits.habits_plan_rounds", "all"),
        ("Replies between the first plan and approval", 40.0, "habits.habits_plan_rounds", "all"),
    ]
    # The action is the line to paste, word for word.
    assert fixes_mod.CRITIQUE_PLAN_LINE in rec.action


def test_plan_rounds_counts_every_plan_as_sent_back_when_none_was_approved_at_once():
    (rec,) = _plan_round_recs(_ROUNDS_ALL)
    assert rec.evidence[1] == ("Never sent back", 0, "habits.habits_plan_rounds", "none")


@pytest.mark.parametrize(
    "all_row, none_row, why",
    [
        (["all", 4, 1, 12, 6, 40.0], ["none", 1, 0, 0, 0, 0.0], "under five approved plans"),
        (["all", 10, 3, 12, 6, 40.0], ["none", 8, 2, 0, 0, 0.0], "under 30% of the plans were sent back"),
        (["all", 10, 3, 12, 3, 40.0], ["none", 4, 1, 0, 0, 0.0], "under 30% of the rounds were a question or critique"),
        (["all", 10, 3, 12, 6, 3.0], ["none", 4, 1, 0, 0, 0.0], "under $1 once scaled and cut"),
        (["all", 10, 3, 0, 0, 40.0], ["none", 10, 3, 0, 0, 0.0], "no plan was sent back"),
        (["all", 10, 3, 12, 6, "n/a"], ["none", 4, 1, 0, 0, 0.0], "no figure for the cost"),
        (["all", None, 3, 12, 6, 40.0], ["none", 4, 1, 0, 0, 0.0], "no count of plans"),
    ],
)
def test_plan_rounds_stays_quiet_below_its_thresholds_or_without_numbers(all_row, none_row, why):
    assert _plan_round_recs(all_row, none_row) == [], why


def test_plan_rounds_fires_at_exactly_its_thresholds():
    # Ten plans with seven approved at once is exactly 30% sent back; five plans is the fewest that count.
    assert len(_plan_round_recs(["all", 10, 0, 10, 3, 14.0], ["none", 7, 0, 0, 0, 0.0])) == 1
    assert len(_plan_round_recs(["all", 5, 0, 10, 4, 25.0], ["none", 3, 0, 0, 0, 0.0])) == 1


def test_plan_rounds_needs_the_table_its_all_row_and_its_columns():
    r = _base_report()
    assert not any(rec.id == "plan-rounds" for rec in recommend_fn(r, config=_config(), archetype=None))
    # No approved plan at all: no "all" row.
    assert _plan_round_recs(["dropped", 3, 0, 4, 2, 9.0]) == []
    section = _rounds_section(_ROUNDS_ALL, _ROUNDS_NONE)
    del section.tables[0].columns[-1]
    r = _add_section(_base_report(), section)
    assert not any(rec.id == "plan-rounds" for rec in recommend_fn(r, config=_config(), archetype=None))
    # A row shorter than its columns is skipped, not a crash.
    assert _plan_round_recs(["all", 10]) == []


def test_plan_rounds_thresholds_come_from_the_config():
    th = RecommendThresholds.from_config(
        {"plan_rounds_min_plans": 2, "plan_rounds_min_share_pct": 10, "plan_rounds_min_asked_pct": 10,
         "plan_rounds_min_saving_usd": 0.1, "plan_rounds_saving_factor": 1}
    )
    assert (th.plan_rounds_min_plans, th.plan_rounds_min_share_pct, th.plan_rounds_saving_factor) == (2, 10.0, 1.0)
    config = Config(thresholds={"recommend": {
        "plan_rounds_min_plans": 2, "plan_rounds_min_share_pct": 10, "plan_rounds_min_asked_pct": 10,
        "plan_rounds_min_saving_usd": 0.1, "plan_rounds_saving_factor": 1,
    }})
    (rec,) = _plan_round_recs(["all", 3, 0, 2, 1, 4.0], ["none", 2, 0, 0, 0, 0.0], config=config)
    assert rec.saving_usd == pytest.approx(2.0)
    # The defaults would have left it alone.
    assert _plan_round_recs(["all", 3, 0, 2, 1, 4.0], ["none", 2, 0, 0, 0, 0.0]) == []


# -- spawn-cost --------------------------------------------------------------


def _spawn_cost_report(r, *rows):
    """``r`` with a first call and an unused-tools size per ``(agent type,
    mean first call, tools never used)`` row: what spawn-cost reads."""
    r = _add_section(
        r,
        Section(
            key="agents",
            title="Agents",
            tables=[
                Table(
                    name="topology_spawn_write",
                    title="Spawn write",
                    columns=[
                        Column(key="agent_type", label="Agent type"),
                        Column(key="mean_first_call", label="Mean first call"),
                    ],
                    rows=[[agent, first_call] for agent, first_call, _removable in rows],
                )
            ],
        ),
    )
    return _add_section(
        r,
        Section(
            key="agent_startup",
            title="Agent startup",
            tables=[
                Table(
                    name="agent_startup_breakdown",
                    title="Startup",
                    columns=[
                        Column(key="agent_type", label="Agent type"),
                        Column(key="removable_tools", label="Tools never used"),
                    ],
                    rows=[[agent, removable] for agent, _first_call, removable in rows],
                )
            ],
        ),
    )


def test_spawn_cost_fires_per_agent_type():
    r = _base_report()
    r = _spawn_cost_report(r, ("claude-implementer", 110_000, 8_000))
    recs = recommend_fn(r, config=_config(), archetype=None)
    rec = next(rec for rec in recs if rec.id == "spawn-cost")
    assert rec.lever == "omitClaudeMd"
    assert rec.evidence == [
        ("Mean first call", 110_000, "agents.topology_spawn_write", "claude-implementer"),
        (
            "Tool definitions it rarely or never uses",
            8_000,
            "agent_startup.agent_startup_breakdown",
            "claude-implementer",
        ),
    ]


def test_spawn_cost_not_suppressed_for_overseer_fanout():
    """spawn-cost's advice ('trim the briefing') is different from
    subagent-volume's ('stop spawning so much') -- an overseer-fanout
    session should still be told to trim an expensive spawn briefing."""
    r = _base_report()
    r = _spawn_cost_report(r, ("claude-implementer", 110_000, 8_000))
    recs = recommend_fn(r, config=_config(), archetype="overseer-fanout")
    assert any(rec.id == "spawn-cost" for rec in recs)


def test_spawn_cost_suppressed_for_chat_only():
    r = _base_report()
    r = _spawn_cost_report(r, ("claude-implementer", 110_000, 8_000))
    recs = recommend_fn(r, config=_config(), archetype="chat-only")
    assert not any(rec.id == "spawn-cost" for rec in recs)


def test_spawn_cost_emits_workflow_advice_with_no_lever_for_builtin_agent_type():
    # Fix A1: "general-purpose" is one of Claude Code's own bundled agent
    # types (_BUILTIN_AGENT_TYPES) -- it has no .claude/agents/*.md file
    # for omitClaudeMd to patch, so without a snapshot to say otherwise
    # this must fall back to workflow advice with no lever.
    r = _base_report()
    r = _spawn_cost_report(r, ("general-purpose", 110_000, 8_000))
    recs = recommend_fn(r, config=_config(), archetype=None)
    rec = next(rec for rec in recs if rec.id == "spawn-cost")
    assert rec.lever is None
    assert rec.category == "workflow"
    # A shorter task prompt does not change a built-in agent's start, so the
    # card says so and points at a same-named file with a tools list.
    assert "a shorter task prompt does not change" in rec.action
    assert "same-named agent file with a tools list" in rec.action
    assert "Shorten" not in rec.action and "briefing" not in rec.action
    text = render_patch_set([rec])
    assert text == ""


def test_spawn_cost_is_not_given_to_the_read_only_helpers():
    """Explore, Plan and claude-code-guide start with a small tool set of
    their own, which the tools-list card leaves alone: a built-in one has
    no file of the user's to put a tools list in, so spawn-cost has no
    lever for it (it used to fire for Plan, with tools-list advice and no
    amount)."""
    for agent in ("Explore", "Plan", "claude-code-guide"):
        r = _spawn_cost_report(_base_report(), (agent, 110_000, 8_000))
        recs = recommend_fn(r, config=_config(), archetype=None)
        assert not any(rec.id == "spawn-cost" for rec in recs), agent


def test_spawn_cost_still_names_a_read_only_helper_the_user_gave_a_file():
    # A Plan.md of the user's is a file omitClaudeMd and a tools list can go in.
    r = _spawn_cost_report(_base_report(), ("Plan", 110_000, 8_000), ("general-purpose", 110_000, 8_000))
    snapshot = Snapshot(path=Path("s.json"), ts="20260918T000000Z", data={"agents": {"Plan": {"model": "sonnet"}}})
    recs = recommend_fn(r, config=_config(), archetype=None, snapshot=snapshot)
    spawn_recs = {rec.agent_type: rec for rec in recs if rec.id == "spawn-cost"}
    assert spawn_recs["Plan"].lever == "omitClaudeMd"
    # The built-in general-purpose still gets its card beside it.
    assert spawn_recs["general-purpose"].lever is None


def test_spawn_cost_snapshot_agents_map_overrides_builtin_fallback():
    # Fix A1: when a snapshot is available, its own "agents" map (real
    # frontmatter files found on disk) is authoritative -- even for an
    # agent type not on the built-in list, absence from that map means
    # no lever; presence means a lever, regardless of the built-in guess.
    r = _base_report()
    r = _spawn_cost_report(r, ("claude-implementer", 110_000, 8_000), ("general-purpose", 110_000, 8_000))
    snapshot = Snapshot(
        path=Path("s.json"),
        ts="20260918T000000Z",
        data={"agents": {"claude-implementer": {"model": "sonnet"}}},
    )
    recs = recommend_fn(r, config=_config(), archetype=None, snapshot=snapshot)
    spawn_recs = {rec.agent_type: rec for rec in recs if rec.id == "spawn-cost"}
    assert spawn_recs["claude-implementer"].lever == "omitClaudeMd"
    assert spawn_recs["claude-implementer"].scope == "repo"
    assert spawn_recs["general-purpose"].lever is None
    assert spawn_recs["general-purpose"].category == "workflow"


def test_spawn_cost_does_not_fire_below_threshold():
    r = _base_report()
    r = _spawn_cost_report(r, ("claude-implementer", 30_000, 8_000))
    recs = recommend_fn(r, config=_config(), archetype=None)
    assert not any(rec.id == "spawn-cost" for rec in recs)


def test_spawn_cost_does_not_fire_when_the_first_call_is_mostly_claude_codes_own_tools():
    """A first call of 110k with nothing a tools list could take out is the
    harness floor: no setting of the user's changes it, so no card."""
    r = _spawn_cost_report(_base_report(), ("claude-implementer", 110_000, 500))
    recs = recommend_fn(r, config=_config(), archetype=None)
    assert not any(rec.id == "spawn-cost" for rec in recs)


def test_spawn_cost_does_not_fire_without_the_startup_breakdown():
    r = _spawn_cost_report(_base_report(), ("claude-implementer", 110_000, 8_000))
    r = dataclasses.replace(r, sections=[sec for sec in r.sections if sec.key != "agent_startup"])
    recs = recommend_fn(r, config=_config(), archetype=None)
    assert not any(rec.id == "spawn-cost" for rec in recs)


def test_spawn_cost_ignores_the_cache_write_it_used_to_gate_on():
    """A big cache write (a long briefing) on a small first call is the
    spawn-task-prompt rule's, not spawn-cost's."""
    r = _spawn_cost_report(_base_report(), ("claude-implementer", 30_000, 8_000))
    recs = recommend_fn(r, config=_config(), archetype=None)
    assert not any(rec.id == "spawn-cost" for rec in recs)



# -- effort-mismatch ---------------------------------------------------------


def test_effort_mismatch_fires_with_evidence_per_purpose_row():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="sessions",
            title="Sessions",
            tables=[
                Table(
                    name="sessions_by_purpose",
                    title="Sessions by purpose",
                    columns=[Column(key="purpose", label="Purpose"), Column(key="sessions", label="Sessions")],
                    rows=[["docs-or-light-edit", 4], ["general-dev", 3], ["review", 2]],
                )
            ],
        ),
    )
    r = _add_section(
        r,
        Section(
            key="agents",
            title="Agents",
            tables=[
                Table(
                    name="topology_effort_tokens",
                    title="Effort tokens",
                    columns=[
                        Column(key="effort", label="Effort"),
                        Column(key="turns", label="Turns"),
                        Column(key="output_tokens", label="Output tokens"),
                        Column(key="thinking_tokens", label="Thinking tokens"),
                        Column(key="thinking_share", label="Thinking share", kind="pct"),
                    ],
                    rows=[["high", 100, 10_000, 4_000, 40.0]],
                )
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    rec = next(rec for rec in recs if rec.id == "effort-mismatch")
    assert rec.lever == "effortLevel"
    # one evidence tuple for the thinking-share condition plus one per
    # contributing purpose row -- each citing its own exact cell (the
    # bug this module's docstring documents fixing).
    assert ("High-effort thinking share of output", 40.0, "agents.topology_effort_tokens", "high") in rec.evidence
    assert ("docs-or-light-edit sessions in corpus", 4, "sessions.sessions_by_purpose", "docs-or-light-edit") in rec.evidence
    assert ("general-dev sessions in corpus", 3, "sessions.sessions_by_purpose", "general-dev") in rec.evidence
    assert len(rec.evidence) == 3
    # R22: this rule can't join the thinking-share group-by to the
    # purpose group-by by session (no report table carries both), so
    # the approximation is disclosed in the action text rather than
    # presented as a genuine per-session join.
    assert "not only the light ones" in rec.changes[0].note
    # No snapshot given -- _lever_scope has nothing to consult, so this
    # defaults to "user" rather than raising.
    assert rec.scope == "user"


def test_effort_mismatch_scope_follows_effort_levels_own_provenance():
    # COV-01: effort-mismatch used to hardcode "user"/"managed" (a bare
    # string prefix on the title); it now asks _lever_scope, which reads
    # effortLevel's actual provenance off the snapshot -- including the
    # project-local layer _lever_scope didn't used to know existed.
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="sessions",
            title="Sessions",
            tables=[
                Table(
                    name="sessions_by_purpose",
                    title="Sessions by purpose",
                    columns=[Column(key="purpose", label="Purpose"), Column(key="sessions", label="Sessions")],
                    rows=[["docs-or-light-edit", 4], ["general-dev", 3]],
                )
            ],
        ),
    )
    r = _add_section(
        r,
        Section(
            key="agents",
            title="Agents",
            tables=[
                Table(
                    name="topology_effort_tokens",
                    title="Effort tokens",
                    columns=[
                        Column(key="effort", label="Effort"),
                        Column(key="turns", label="Turns"),
                        Column(key="output_tokens", label="Output tokens"),
                        Column(key="thinking_tokens", label="Thinking tokens"),
                        Column(key="thinking_share", label="Thinking share", kind="pct"),
                    ],
                    rows=[["high", 100, 10_000, 4_000, 40.0]],
                )
            ],
        ),
    )
    snapshot = Snapshot(
        path=Path("s.json"),
        ts="20260918T000000Z",
        data={"effective_provenance": {"effortLevel": "project_local"}},
    )
    recs = recommend_fn(r, config=_config(), archetype=None, snapshot=snapshot)
    rec = next(rec for rec in recs if rec.id == "effort-mismatch")
    assert rec.scope == "project-local"
    assert rec.changes[0].scope == "project-local"


def test_effort_mismatch_does_not_fire_without_docs_purposes():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="sessions",
            title="Sessions",
            tables=[
                Table(
                    name="sessions_by_purpose",
                    title="Sessions by purpose",
                    columns=[Column(key="purpose", label="Purpose"), Column(key="sessions", label="Sessions")],
                    rows=[["review", 5]],
                )
            ],
        ),
    )
    r = _add_section(
        r,
        Section(
            key="agents",
            title="Agents",
            tables=[
                Table(
                    name="topology_effort_tokens",
                    title="Effort tokens",
                    columns=[
                        Column(key="effort", label="Effort"),
                        Column(key="thinking_share", label="Thinking share", kind="pct"),
                    ],
                    rows=[["high", 40.0]],
                )
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    assert not any(rec.id == "effort-mismatch" for rec in recs)


def test_effort_mismatch_does_not_fire_below_thinking_share():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="sessions",
            title="Sessions",
            tables=[
                Table(
                    name="sessions_by_purpose",
                    title="Sessions by purpose",
                    columns=[Column(key="purpose", label="Purpose"), Column(key="sessions", label="Sessions")],
                    rows=[["general-dev", 4]],
                )
            ],
        ),
    )
    r = _add_section(
        r,
        Section(
            key="agents",
            title="Agents",
            tables=[
                Table(
                    name="topology_effort_tokens",
                    title="Effort tokens",
                    columns=[
                        Column(key="effort", label="Effort"),
                        Column(key="thinking_share", label="Thinking share", kind="pct"),
                    ],
                    rows=[["high", 10.0]],
                )
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    assert not any(rec.id == "effort-mismatch" for rec in recs)


# -- discovery-share ----------------------------------------------------


def _phases_summary_table(discovery_share: float) -> Table:
    return Table(
        name="phases_summary",
        title="Phases",
        columns=[Column(key="phase", label="Phase"), Column(key="cost_share_pct", label="Cost share", kind="pct")],
        rows=[["discovery", discovery_share], ["implementation", 100 - discovery_share]],
    )


def test_discovery_share_fires_when_phases_section_present_and_above_threshold():
    r = _base_report()
    r = _add_section(r, Section(key="phases", title="Phases", tables=[_phases_summary_table(50.0)]))
    recs = recommend_fn(r, config=_config(), archetype=None)
    rec = next(rec for rec in recs if rec.id == "discovery-share")
    assert rec.evidence == [("Share of cost", 50.0, "phases.phases_summary", "discovery")]
    # The dashboard shows phases without --phases, so the card never names the flag.
    assert rec.title == "Much of the work is finding your way around"
    assert rec.why == "Searching and reading the code took 50% of the cost."
    assert "--phases" not in rec.action


def test_discovery_share_does_not_fire_without_phases_section():
    r = _base_report()  # no "phases" section at all -- the CLI without --phases
    recs = recommend_fn(r, config=_config(), archetype=None)
    assert not any(rec.id == "discovery-share" for rec in recs)


def test_discovery_share_does_not_fire_below_threshold():
    r = _base_report()
    r = _add_section(r, Section(key="phases", title="Phases", tables=[_phases_summary_table(20.0)]))
    recs = recommend_fn(r, config=_config(), archetype=None)
    assert not any(rec.id == "discovery-share" for rec in recs)


# -- pricing-coverage ---------------------------------------------------


def _scorecard_dimensions_table(rows: list[list]) -> Table:
    return Table(
        name="dimensions",
        title="Dimensions",
        columns=[
            Column(key="dimension", label="Dimension"),
            Column(key="level", label="Level"),
            Column(key="label", label="Label"),
            Column(key="metric", label="Metric"),
            Column(key="value", label="Value"),
            Column(key="threshold", label="Threshold"),
        ],
        rows=rows,
    )


def test_pricing_coverage_fires_when_below_full_coverage():
    r = _base_report()
    r.meta.pricing.coverage_pct = 95.0
    r = _add_section(
        r,
        Section(
            key="scorecard",
            title="Scorecard",
            tables=[_scorecard_dimensions_table([["data_quality", "warn", "Data quality", "pricing_coverage_pct", 95.0, 100.0]])],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    rec = next(rec for rec in recs if rec.id == "pricing-coverage")
    assert rec.evidence == [
        ("Pricing coverage (data-quality dimension)", 95.0, "scorecard.dimensions", "data_quality"),
    ]


def test_pricing_coverage_does_not_fire_at_full_coverage():
    r = _base_report()
    r.meta.pricing.coverage_pct = 100.0
    r = _add_section(
        r,
        Section(
            key="scorecard",
            title="Scorecard",
            tables=[_scorecard_dimensions_table([["data_quality", "ok", "Data quality", "pricing_coverage_pct", 100.0, 100.0]])],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    assert not any(rec.id == "pricing-coverage" for rec in recs)


def test_pricing_coverage_fires_from_coverage_pct_alone_with_no_unknown_models_table():
    # R12: a report built without the usage section has no
    # usage.pricing_unknown_models table -- the rule must still fire off
    # report.meta.pricing.coverage_pct alone.
    r = _base_report()
    r.meta.pricing.coverage_pct = 42.0
    r = _add_section(
        r,
        Section(
            key="scorecard",
            title="Scorecard",
            tables=[_scorecard_dimensions_table([["data_quality", "warn", "Data quality", "pricing_coverage_pct", 42.0, 100.0]])],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    assert any(rec.id == "pricing-coverage" for rec in recs)


def test_pricing_coverage_action_names_unknown_model_ids_when_table_present():
    r = _base_report()
    r.meta.pricing.coverage_pct = 90.0
    r = _add_section(
        r,
        Section(
            key="scorecard",
            title="Scorecard",
            tables=[_scorecard_dimensions_table([["data_quality", "warn", "Data quality", "pricing_coverage_pct", 90.0, 100.0]])],
        ),
    )
    r = _add_section(
        r,
        Section(
            key="usage",
            title="Usage",
            tables=[
                Table(
                    name="pricing_unknown_models",
                    title="Unpriced models",
                    columns=[
                        Column(key="model_id", label="Model"),
                        Column(key="turns", label="Turns"),
                        Column(key="tokens", label="Tokens"),
                    ],
                    rows=[["claude-mystery-9", 3, 1000]],
                )
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    rec = next(rec for rec in recs if rec.id == "pricing-coverage")
    assert "claude-mystery-9" in rec.action


def test_pricing_coverage_fires_at_full_coverage_when_closest_match_table_present():
    # Fix 2: coverage_pct == 100.0 means "no tokens went unpriced", not
    # "every model has its own rate" -- a pricing_closest_match table
    # (turns priced by prefix match, not their own pricing.toml row)
    # must still fire the rule even though nothing is missing from the
    # total.
    r = _base_report()
    r.meta.pricing.coverage_pct = 100.0
    r = _add_section(
        r,
        Section(
            key="scorecard",
            title="Scorecard",
            tables=[_scorecard_dimensions_table([["data_quality", "warn", "Data quality", "pricing_coverage_pct", 100.0, 100.0]])],
        ),
    )
    r = _add_section(
        r,
        Section(
            key="usage",
            title="Usage",
            tables=[
                Table(
                    name="pricing_closest_match",
                    title="Priced by closest match",
                    columns=[
                        Column(key="model_id", label="Model"),
                        Column(key="priced_as", label="Priced as"),
                        Column(key="turns", label="Turns"),
                        Column(key="tokens", label="Tokens"),
                    ],
                    rows=[["claude-widget-9-preview", "claude-widget-9", 4, 2000]],
                )
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    rec = next(rec for rec in recs if rec.id == "pricing-coverage")
    assert "claude-widget-9-preview" in rec.action
    assert (
        "claude-widget-9-preview was priced by closest match, not its own rate; give it a pricing.toml row of "
        "its own for an exact cost."
    ) in rec.action


def _closest_match_report(rows: list[list]):
    r = _base_report()
    r.meta.pricing.coverage_pct = 100.0
    r = _add_section(
        r,
        Section(
            key="scorecard",
            title="Scorecard",
            tables=[_scorecard_dimensions_table([["data_quality", "warn", "Data quality", "pricing_coverage_pct", 100.0, 100.0]])],
        ),
    )
    return _add_section(
        r,
        Section(
            key="usage",
            title="Usage",
            tables=[
                Table(
                    name="pricing_closest_match",
                    title="Priced by closest match",
                    columns=[
                        Column(key="model_id", label="Model"),
                        Column(key="priced_as", label="Priced as"),
                        Column(key="turns", label="Turns"),
                        Column(key="tokens", label="Tokens"),
                    ],
                    rows=rows,
                )
            ],
        ),
    )


def test_pricing_coverage_names_a_newer_version_as_one_with_no_rate_yet():
    r = _closest_match_report([["claude-widget-9-1", "claude-widget-9", 4, 2000]])
    rec = next(rec for rec in recommend_fn(r, config=_config(), archetype=None) if rec.id == "pricing-coverage")
    assert rec.action == (
        "There is no rate of its own for claude-widget-9-1 yet, so it was priced as claude-widget-9; update "
        'ClaudeGlass or add a models."claude-widget-9-1" row to pricing.toml for its exact cost.'
    )
    assert "closest match" not in rec.action


def test_pricing_coverage_words_newer_versions_and_other_closest_matches_apart():
    r = _closest_match_report([
        ["claude-widget-9-1", "claude-widget-9", 4, 2000],
        ["claude-widget-9-preview", "claude-widget-9", 2, 1000],
        ["claude-gadget-2-20260101", "claude-gadget-2", 1, 500],
    ])
    rec = next(rec for rec in recommend_fn(r, config=_config(), archetype=None) if rec.id == "pricing-coverage")
    # Two plain closest matches read in the plural.
    assert (
        "claude-widget-9-preview, claude-gadget-2-20260101 were priced by closest match, not their own rates; "
        "give them pricing.toml rows of their own for an exact cost."
    ) in rec.action
    assert "There is no rate of its own for claude-widget-9-1 yet, so it was priced as claude-widget-9;" in rec.action
    assert "claude-widget-9-1," not in rec.action


# -- data-quality ---------------------------------------------------------


def test_data_quality_fires_on_ttl_fidelity():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="ttl",
            title="TTL",
            tables=[_ttl_by_agent_type_table([["top-level", 10.0, 25.0, "no material difference", "promptCacheTtl"]])],
        ),
    )
    r = _add_section(
        r,
        Section(
            key="scorecard",
            title="Scorecard",
            tables=[_scorecard_dimensions_table([["data_quality", "warn", "Data quality", "pricing_coverage_pct", 100.0, 100.0]])],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    rec = next(rec for rec in recs if rec.id == "data-quality")
    assert ("Pricing coverage (data-quality dimension)", 100.0, "scorecard.dimensions", "data_quality") in rec.evidence
    assert ("TTL simulation fidelity", 25.0, "ttl.ttl_by_agent_type", "top-level") in rec.evidence


def test_data_quality_fires_on_unparsable_lines():
    r = _base_report()
    r.diagnostics = Diagnostics(lines=1000, unparsable_lines=5, ttl_sum_mismatch=0)  # 0.5% > 0.1%
    r = _add_section(
        r,
        Section(
            key="scorecard",
            title="Scorecard",
            tables=[_scorecard_dimensions_table([["data_quality", "ok", "Data quality", "pricing_coverage_pct", 100.0, 100.0]])],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    rec = next(rec for rec in recs if rec.id == "data-quality")
    assert "could not be read" in rec.why


def test_data_quality_fires_on_ttl_mismatch():
    r = _base_report()
    r.diagnostics = Diagnostics(lines=1000, unparsable_lines=0, ttl_sum_mismatch=2)
    r = _add_section(
        r,
        Section(
            key="scorecard",
            title="Scorecard",
            tables=[_scorecard_dimensions_table([["data_quality", "ok", "Data quality", "pricing_coverage_pct", 100.0, 100.0]])],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    assert any(rec.id == "data-quality" for rec in recs)


def test_data_quality_does_not_fire_when_all_clean():
    r = _base_report()
    r.diagnostics = Diagnostics(lines=1000, unparsable_lines=0, ttl_sum_mismatch=0)
    r = _add_section(
        r,
        Section(
            key="ttl",
            title="TTL",
            tables=[_ttl_by_agent_type_table([["top-level", 10.0, 1.0, "no material difference", "promptCacheTtl"]])],
        ),
    )
    r = _add_section(
        r,
        Section(
            key="scorecard",
            title="Scorecard",
            tables=[_scorecard_dimensions_table([["data_quality", "ok", "Data quality", "pricing_coverage_pct", 100.0, 100.0]])],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    assert not any(rec.id == "data-quality" for rec in recs)


# -- limit-pressure (v3-limits addition) ------------------------------------


def _limits_summary_table(
    five_hour: int = 0,
    weekly: int = 0,
    weekly_stopped_work: int = 0,
    cut_off: int = 0,
    days: int = 30,
    sessions_affected: int = 1,
) -> Table:
    return Table(
        name="limits_summary",
        title="Usage-limits summary",
        columns=[
            Column(key="metric", label="Metric"),
            Column(key="five_hour_stops", label="5-hour limit stops"),
            Column(key="weekly_stops", label="Weekly limit stops"),
            Column(key="weekly_stops_stopped_work", label="Weekly stops that stopped work"),
            Column(key="window_days", label="Days covered"),
            Column(key="sessions_affected", label="Sessions affected"),
            Column(key="agents_cut_off", label="Agents cut off"),
        ],
        rows=[["all", five_hour, weekly, weekly_stopped_work, days, sessions_affected, cut_off]],
    )


def _limit_pressure_recs(**counts):
    r = _add_section(
        _base_report(),
        Section(key="limits", title="Usage limits", tables=[_limits_summary_table(**counts)]),
    )
    return [rec for rec in recommend_fn(r, config=_config(), archetype=None) if rec.id == "limit-pressure"]


def test_limit_pressure_fires_on_two_five_hour_stops_a_week():
    # 9 stops in 30 days is 2.1 a week.
    (rec,) = _limit_pressure_recs(five_hour=9, days=30)
    assert ("5-hour limit stops", 9, "limits.limits_summary", "all") in rec.evidence
    assert ("Days covered", 30, "limits.limits_summary", "all") in rec.evidence
    assert ("Sessions affected", 1, "limits.limits_summary", "all") in rec.evidence


def test_limit_pressure_counts_a_rate_not_a_total():
    # 8 stops in 30 days is 1.9 a week: below the threshold however long the window.
    assert not _limit_pressure_recs(five_hour=8, days=30)
    assert _limit_pressure_recs(five_hour=2, days=7)


def test_limit_pressure_reads_a_short_window_as_a_week():
    # The window floors at 7 days: two stops in a day read as two a week,
    # and a single stop never does.
    assert _limit_pressure_recs(five_hour=2, days=1)
    assert not _limit_pressure_recs(five_hour=1, days=1)


def test_limit_pressure_fires_on_a_weekly_stop_that_stopped_work():
    (rec,) = _limit_pressure_recs(weekly=1, weekly_stopped_work=1)
    assert ("Weekly stops that stopped work", 1, "limits.limits_summary", "all") in rec.evidence


def test_limit_pressure_ignores_a_weekly_stop_you_worked_through():
    assert not _limit_pressure_recs(weekly=1, weekly_stopped_work=0)


def test_limit_pressure_fires_on_a_single_cut_off_agent():
    (rec,) = _limit_pressure_recs(cut_off=1)
    assert ("Agents cut off by a limit", 1, "limits.limits_summary", "all") in rec.evidence


def test_limit_pressure_does_not_fire_on_one_five_hour_stop():
    assert not _limit_pressure_recs(five_hour=1, days=30)


def test_limit_pressure_absent_without_limits_section():
    r = _base_report()
    recs = recommend_fn(r, config=_config(), archetype=None)
    assert not any(rec.id == "limit-pressure" for rec in recs)


def test_limit_pressure_threshold_is_overridable():
    r = _add_section(
        _base_report(),
        Section(key="limits", title="Usage limits", tables=[_limits_summary_table(five_hour=1, days=7)]),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    assert not any(rec.id == "limit-pressure" for rec in recs)

    recs = recommend_fn(
        r,
        config=_config(),
        archetype=None,
        thresholds=RecommendThresholds(limit_pressure_min_episodes=1),
    )
    assert any(rec.id == "limit-pressure" for rec in recs)


def test_limit_pressure_old_message_threshold_is_read_but_ignored():
    thresholds = RecommendThresholds.from_config({"limit_pressure_min_hits": 1, "limit_pressure_min_episodes": 4})
    assert thresholds.limit_pressure_min_episodes == 4
    assert not hasattr(thresholds, "limit_pressure_min_hits")


# -- long-tool-waits / notification-invalidation / batch-instructions -------
# (recache-derived rules whose conditions are approximated across two
# tables each -- see recommend.py's module docstring)


def test_long_tool_waits_fires_above_both_thresholds():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="recache",
            title="Recache",
            tables=[
                Table(
                    name="recache_summary",
                    title="Recache summary",
                    columns=[Column(key="metric", label="Metric"), Column(key="recache_cc_tokens", label="Recache cc tokens")],
                    rows=[["all", 100_000]],
                ),
                Table(
                    name="recache_signature_split",
                    title="Signature split",
                    columns=[Column(key="signature", label="Signature"), Column(key="cc_tokens", label="CC tokens")],
                    rows=[["full-expiry", 30_000], ["prefix-invalidated", 70_000]],
                ),
                Table(
                    name="recache_preceding_tool",
                    title="Preceding tool",
                    columns=[Column(key="tool", label="Tool"), Column(key="share_pct_turns", label="Share")],
                    rows=[["Bash", 40.0], ["PowerShell", 30.0]],
                ),
                Table(
                    name="recache_gap_buckets",
                    title="Gap buckets",
                    columns=[Column(key="bucket", label="Bucket"), Column(key="share_pct_turns", label="Share")],
                    rows=[[">60m", 30.0], ["15-60m", 25.0], ["5-15m", 20.0], ["1-5m", 15.0], ["<1m", 10.0]],
                ),
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    rec = next(rec for rec in recs if rec.id == "long-tool-waits")
    assert ("Full-expiry cache-creation tokens", 30_000, "recache.recache_signature_split", "full-expiry") in rec.evidence
    # R14: no table exposes the true joint count of turns preceded by
    # Bash/PowerShell AND following a long gap, so each of the two
    # independent shares that stand in for it must be cited as its own
    # evidence entry (previously the long-gap-bucket shares weren't
    # cited at all, only used to compute a fake "combined" number).
    for bucket, expected in ((">60m", 30.0), ("15-60m", 25.0), ("5-15m", 20.0)):
        assert (
            f"{bucket} gap-bucket re-cache turn share",
            expected,
            "recache.recache_gap_buckets",
            bucket,
        ) in rec.evidence


def test_long_tool_waits_requires_both_shares_independently_above_threshold():
    # Bash/PowerShell share is well above threshold (90%), but the
    # long-gap share is well below it (10%) -- the two independent
    # turn populations plainly don't overlap enough to justify firing,
    # even though a naive min() of two *different* metrics could be
    # fooled by a badly-chosen pair of inputs. Here both the old and
    # new logic agree the rule should not fire; this pins that a low
    # long-gap share alone is enough to suppress it regardless of how
    # high the tool share runs.
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="recache",
            title="Recache",
            tables=[
                Table(
                    name="recache_summary",
                    title="Recache summary",
                    columns=[Column(key="metric", label="Metric"), Column(key="recache_cc_tokens", label="Recache cc tokens")],
                    rows=[["all", 100_000]],
                ),
                Table(
                    name="recache_signature_split",
                    title="Signature split",
                    columns=[Column(key="signature", label="Signature"), Column(key="cc_tokens", label="CC tokens")],
                    rows=[["full-expiry", 30_000]],
                ),
                Table(
                    name="recache_preceding_tool",
                    title="Preceding tool",
                    columns=[Column(key="tool", label="Tool"), Column(key="share_pct_turns", label="Share")],
                    rows=[["Bash", 60.0], ["PowerShell", 30.0]],
                ),
                Table(
                    name="recache_gap_buckets",
                    title="Gap buckets",
                    columns=[Column(key="bucket", label="Bucket"), Column(key="share_pct_turns", label="Share")],
                    rows=[[">60m", 5.0], ["15-60m", 3.0], ["5-15m", 2.0]],
                ),
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    assert not any(rec.id == "long-tool-waits" for rec in recs)


def test_long_tool_waits_does_not_fire_below_full_expiry_share():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="recache",
            title="Recache",
            tables=[
                Table(
                    name="recache_summary",
                    title="Recache summary",
                    columns=[Column(key="metric", label="Metric"), Column(key="recache_cc_tokens", label="Recache cc tokens")],
                    rows=[["all", 100_000]],
                ),
                Table(
                    name="recache_signature_split",
                    title="Signature split",
                    columns=[Column(key="signature", label="Signature"), Column(key="cc_tokens", label="CC tokens")],
                    rows=[["full-expiry", 5_000]],
                ),
                Table(
                    name="recache_preceding_tool",
                    title="Preceding tool",
                    columns=[Column(key="tool", label="Tool"), Column(key="share_pct_turns", label="Share")],
                    rows=[["Bash", 40.0]],
                ),
                Table(
                    name="recache_gap_buckets",
                    title="Gap buckets",
                    columns=[Column(key="bucket", label="Bucket"), Column(key="share_pct_turns", label="Share")],
                    rows=[[">60m", 30.0]],
                ),
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    assert not any(rec.id == "long-tool-waits" for rec in recs)


def test_notification_invalidation_fires_above_both_thresholds():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="recache",
            title="Recache",
            tables=[
                Table(
                    name="recache_primary_cause_prefix_invalidated",
                    title="Primary cause (prefix-invalidated)",
                    columns=[
                        Column(key="primary", label="Primary"),
                        Column(key="cc_share_pct", label="CC share"),
                        Column(key="control_cc_share_pct", label="Control share"),
                        Column(key="over_representation_points_tokens", label="Over-rep points"),
                    ],
                    rows=[["task_notification", 40.0, 20.0, 20.0]],
                )
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    rec = next(rec for rec in recs if rec.id == "notification-invalidation")
    assert (
        "TASK_NOTIFICATION share of prefix-invalidated cc",
        40.0,
        "recache.recache_primary_cause_prefix_invalidated",
        "task_notification",
    ) in rec.evidence


def test_notification_invalidation_does_not_fire_below_thresholds():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="recache",
            title="Recache",
            tables=[
                Table(
                    name="recache_primary_cause_prefix_invalidated",
                    title="Primary cause (prefix-invalidated)",
                    columns=[
                        Column(key="primary", label="Primary"),
                        Column(key="cc_share_pct", label="CC share"),
                        Column(key="over_representation_points_tokens", label="Over-rep points"),
                    ],
                    rows=[["task_notification", 10.0, 2.0]],
                )
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    assert not any(rec.id == "notification-invalidation" for rec in recs)


def test_batch_instructions_fires_above_overrep_threshold():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="recache",
            title="Recache",
            tables=[
                Table(
                    name="recache_primary_cause",
                    title="Primary cause",
                    columns=[
                        Column(key="primary", label="Primary"),
                        Column(key="cc_share_pct", label="CC share"),
                        Column(key="over_representation_points_tokens", label="Over-rep points"),
                    ],
                    rows=[["queue_operation", 30.0, 15.0]],
                )
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    rec = next(rec for rec in recs if rec.id == "batch-instructions")
    assert ("Over-representation vs all-turns control", 15.0, "recache.recache_primary_cause", "queue_operation") in rec.evidence


def test_batch_instructions_does_not_fire_below_overrep_threshold():
    r = _base_report()
    r = _add_section(
        r,
        Section(
            key="recache",
            title="Recache",
            tables=[
                Table(
                    name="recache_primary_cause",
                    title="Primary cause",
                    columns=[
                        Column(key="primary", label="Primary"),
                        Column(key="cc_share_pct", label="CC share"),
                        Column(key="over_representation_points_tokens", label="Over-rep points"),
                    ],
                    rows=[["queue_operation", 30.0, 2.0]],
                )
            ],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    assert not any(rec.id == "batch-instructions" for rec in recs)


# -- RecommendThresholds.from_config -----------------------------------


def test_from_config_none_returns_defaults():
    th = RecommendThresholds.from_config(None)
    assert th == RecommendThresholds()


def test_from_config_overrides_known_field():
    th = RecommendThresholds.from_config({"cache_read_dominance_pct": 75.0})
    assert th.cache_read_dominance_pct == 75.0
    assert th.subagent_volume_cost_share_pct == RecommendThresholds().subagent_volume_cost_share_pct


def test_from_config_ignores_unknown_and_malformed_keys():
    th = RecommendThresholds.from_config({"not_a_real_field": 1.0, "cache_read_dominance_pct": "not-a-number"})
    assert th == RecommendThresholds()


def test_recommend_honours_explicit_thresholds_override():
    r = _base_report(cache_read_cost_share_pct=60.0)
    default_recs = recommend_fn(r, config=_config(), archetype=None)
    assert any(rec.id == "cache-read-dominance" for rec in default_recs)

    strict = RecommendThresholds(cache_read_dominance_pct=90.0)
    strict_recs = recommend_fn(r, config=_config(), archetype=None, thresholds=strict)
    assert not any(rec.id == "cache-read-dominance" for rec in strict_recs)


# -- render_patch_set ------------------------------------------------------


def test_render_patch_set_per_agent_ttl_lever():
    rec = dataclasses.replace(
        _make_recommendation(),
        lever="experimental.cacheTtl in claude-implementer.md (or subagentPromptCacheTtl for all subagents)",
        action="Switch claude-implementer's prompt cache TTL to 1h.",
    )
    text = render_patch_set([rec])
    assert "--- .claude/agents/claude-implementer.md" in text
    assert "+experimental.cacheTtl: 1h" in text


def test_render_patch_set_top_level_prompt_cache_ttl_lever():
    rec = dataclasses.replace(
        _make_recommendation(),
        lever="promptCacheTtl",
        action="Switch top-level's prompt cache TTL to 5m.",
    )
    text = render_patch_set([rec])
    assert "--- settings (user)" in text
    assert "+promptCacheTtl: 5m" in text
    # No path other than .claude/agents/<agent_type>.md anywhere in output.
    assert ".claude/agents/" not in text


def test_render_patch_set_subagent_setting_lever_is_a_settings_stanza():
    """A per-agent-type row whose lever is subagentPromptCacheTtl (the
    setting outranks the agent file) renders as a settings key, not as a
    key inside that agent's frontmatter."""
    rec = dataclasses.replace(
        _make_recommendation(),
        lever="subagentPromptCacheTtl",
        agent_type="claude-implementer",
        action="Switch claude-implementer's prompt cache TTL to 1h.",
    )
    text = render_patch_set([rec])
    assert "--- settings (user)" in text
    assert "+subagentPromptCacheTtl: 1h" in text
    assert ".claude/agents/" not in text


def test_render_patch_set_managed_lever_gets_reference_only_comment():
    rec = dataclasses.replace(_make_recommendation(), lever="autoCompactWindow", scope="managed", action="Raise it.")
    text = render_patch_set([rec])
    assert "# managed by policy -- shown for reference only" in text
    assert "autoCompactWindow" in text


def test_render_patch_set_deduplicates_same_lever():
    rec_a = dataclasses.replace(_make_recommendation(id="a"), lever="autoCompactWindow", action="Raise it.")
    rec_b = dataclasses.replace(_make_recommendation(id="b"), lever="autoCompactWindow", action="Raise it more.")
    text = render_patch_set([rec_a, rec_b])
    assert text.count("--- settings (user)") == 1


def test_render_patch_set_skips_recommendations_with_no_lever():
    rec = dataclasses.replace(_make_recommendation(), lever=None)
    text = render_patch_set([rec])
    assert text == ""


def test_render_patch_set_routes_omit_claude_md_to_the_agent_file_not_settings():
    # Fix R13: omitClaudeMd (spawn-cost's lever) is per-agent
    # frontmatter, not a top-level settings key -- it must not fall
    # into the generic "settings (user)" stanza.
    rec = dataclasses.replace(
        _make_recommendation(),
        lever="omitClaudeMd",
        agent_type="claude-implementer",
        action="Trim claude-implementer's briefing.",
    )
    text = render_patch_set([rec])
    assert "--- .claude/agents/claude-implementer.md" in text
    assert "+omitClaudeMd: true" in text
    assert "settings (user)" not in text


def test_render_patch_set_merges_multiple_levers_for_the_same_agent_into_one_stanza():
    # Fix R13: a TTL switch and an omitClaudeMd recommendation for the
    # *same* agent type must produce one merged diff for that agent's
    # file, not two separate "--- .claude/agents/..." stanzas.
    ttl_rec = dataclasses.replace(
        _make_recommendation(id="ttl"),
        lever="experimental.cacheTtl in claude-implementer.md (or subagentPromptCacheTtl for all subagents)",
        agent_type="claude-implementer",
        action="Switch claude-implementer's prompt cache TTL to 1h.",
    )
    briefing_rec = dataclasses.replace(
        _make_recommendation(id="spawn"),
        lever="omitClaudeMd",
        agent_type="claude-implementer",
        action="Trim claude-implementer's briefing.",
    )
    text = render_patch_set([ttl_rec, briefing_rec])
    assert text.count("--- .claude/agents/claude-implementer.md") == 1
    assert "+experimental.cacheTtl: 1h" in text
    assert "+omitClaudeMd: true" in text


def test_render_patch_set_top_level_agent_type_stays_a_settings_key_not_a_file():
    # agent_type="top-level" is the main session, not a subagent --
    # it must still render as the bare settings-key stanza.
    rec = dataclasses.replace(
        _make_recommendation(),
        lever="promptCacheTtl",
        agent_type="top-level",
        action="Switch top-level's prompt cache TTL to 5m.",
    )
    text = render_patch_set([rec])
    assert "--- settings (user)" in text
    assert ".claude/agents/" not in text


def test_render_patch_set_skips_the_agent_model_cards():
    """Their fix is a prompt: no agent-file stanza, least of all for
    workflow-subagent, which has no agent file."""
    from claudeglass import agent_models

    recs = [
        dataclasses.replace(_make_recommendation(id=rule_id), category="workflow", lever="model", agent_type=agent_type)
        for rule_id, agent_type in zip(agent_models.RULES, ("workflow-subagent", "claude-implementer", "general-purpose"))
    ]
    assert render_patch_set(recs) == ""


def _make_recommendation(**overrides):
    from claudeglass.model import Recommendation

    fields = dict(
        id="x",
        severity="advice",
        category="settings",
        archetypes=(),
        title="Title",
        action="Action.",
        lever=None,
        evidence=[],
    )
    fields.update(overrides)
    return Recommendation(**fields)


# -- privacy --------------------------------------------------------------


def test_recommend_output_has_no_privacy_leaks():
    r = _base_report(cache_read_cost_share_pct=90.0)
    r = _add_section(
        r,
        Section(
            key="ttl",
            title="TTL",
            tables=[_ttl_by_agent_type_table([["claude-implementer", 10.0, 0.0, "switch to 1h", "promptCacheTtl"]])],
        ),
    )
    recs = recommend_fn(r, config=_config(), archetype=None)
    assert recs, "expected at least one recommendation to scan"
    for rec in recs:
        assert_privacy(rec)
    patch_text = render_patch_set(recs)
    assert_privacy({"patch_set": patch_text})


# -- evidence-exists integration test (real assembled ReportModel) ---------


def test_every_recommendation_evidence_resolves_against_the_report(tmp_path: Path):
    """Build a real corpus through report.build_report() (exercising the
    report.py wiring from Deliverable 2) and confirm every recommendation
    it produces cites evidence that actually exists in the assembled
    report's own sections, with the exact cell value quoted.
    """
    project_dir = tmp_path / "proj-recommend"
    project_dir.mkdir()
    # 220 priced turns in one session clears the "200 priced turns" half of
    # the minimum-sample gate; heavy cache_read relative to input/output
    # tokens is enough to trip cache-read-dominance, so the evidence walk
    # below isn't vacuous.
    lines = [
        turn_line(
            timestamp=f"2026-09-{10 + (i % 15):02d}T12:00:00.000Z",
            input_tokens=100,
            output_tokens=50,
            ephemeral_5m_input_tokens=1000,
            cache_read_input_tokens=5000,
        )
        for i in range(220)
    ]
    write_jsonl(project_dir / "session-recommend.jsonl", lines)

    corpus = load_corpus([project_dir])
    from claudeglass.pricing import load_pricing

    pricing = load_pricing()
    model = report.build_report(corpus, pricing, Config(), projects=("proj-recommend",), window="test", phases=True)

    assert model.recommendations, "expected at least one recommendation from this cache-read-heavy corpus"
    for rec in model.recommendations:
        for label, value, source_table, row_key in rec.evidence:
            section_key, table_name = source_table.split(".", 1)
            section = next((s for s in model.sections if s.key == section_key), None)
            assert section is not None, f"{rec.id}: no section {section_key!r} for evidence {label!r}"
            table = next((t for t in section.tables if t.name == table_name), None)
            assert table is not None, f"{rec.id}: no table {table_name!r} for evidence {label!r}"
            row = next((row for row in table.rows if row and row[0] == row_key), None)
            assert row is not None, f"{rec.id}: no row {row_key!r} in {source_table} for evidence {label!r}"


@pytest.mark.skipif(not REAL_SESSION_A.exists(), reason="tests/fixtures/real/session-a not present")
def test_real_fixture_recommendation_evidence_resolves(tmp_path: Path):
    """Same evidence-exists walk as above, against the real anonymised
    fixture corpus when it's present locally (see test_real_fixture.py
    for the same skipif convention).
    """
    corpus = load_corpus([REAL_SESSION_A])
    from claudeglass.pricing import load_pricing

    pricing = load_pricing()
    model = report.build_report(corpus, pricing, Config(), projects=("session-a",), window="real fixture")

    for rec in model.recommendations:
        for label, value, source_table, row_key in rec.evidence:
            section_key, table_name = source_table.split(".", 1)
            section = next((s for s in model.sections if s.key == section_key), None)
            assert section is not None, f"{rec.id}: no section {section_key!r} for evidence {label!r}"
            table = next((t for t in section.tables if t.name == table_name), None)
            assert table is not None, f"{rec.id}: no table {table_name!r} for evidence {label!r}"
            row = next((row for row in table.rows if row and row[0] == row_key), None)
            assert row is not None, f"{rec.id}: no row {row_key!r} in {source_table} for evidence {label!r}"


# -- v4 wiring round: carry/compaction_sim/model_swap/waste rules -----------


def test_v4_module_rules_fire_via_recommend_and_evidence_resolves(tmp_path: Path):
    """Extends the evidence-exists walk above to the four v4 analytics
    modules' own rules (carry/compaction_sim/model_swap/waste -- each
    carries its own RULES, folded into recommend.recommend() via the
    recs.extend(...) calls added in this wiring round). A hand-built
    ReportModel carries each module's real build_section() output, built
    from small fixtures engineered so every one of the four rules
    actually fires (each mirrors that module's own test file's own
    "rule fires" fixture -- test_carry.py/test_compaction_sim.py/
    test_model_swap.py/test_waste.py), then the same generic
    evidence-resolves walk as the two tests above runs across every
    recommendation produced.
    """
    from claudeglass import carry, compaction_sim, model_swap, waste
    from claudeglass.model import Turn, TranscriptMeta, TranscriptResult
    from claudeglass.parse import parse_transcript
    from claudeglass.pricing import load_pricing

    from helpers import tool_result_block, tool_use_block, user_block_line

    pricing = load_pricing()

    def _turn(**overrides) -> Turn:
        fields = dict(
            message_id="msg_1",
            request_id="req_1",
            turn_index=1,
            ts="2026-09-18T12:00:00.000Z",
            model="claude-sonnet-5",
            input_tokens=0,
            cache_creation_tokens=0,
            cache_read_tokens=0,
            output_tokens=0,
            cc_5m=0,
            cc_1h=0,
            ctx=0,
            gap_s=None,
        )
        fields.update(overrides)
        return Turn(**fields)

    # -- carry: several transcripts each carrying one huge Read result
    # and several later cache-read-only turns, so Read's carry cost
    # dominates the corpus's cache volume (test_carry.py's own
    # _heavy_read_corpus fixture).
    carry_transcripts = []
    for s in range(6):
        turns = []
        for i in range(1, 9):
            kwargs = dict(turn_index=i, message_id=f"msg_c{s}_{i}")
            if i == 1:
                kwargs["tool_result_chars_by_tool"] = {"Read": 80_000}
            else:
                kwargs["cache_read_tokens"] = 200
            turns.append(_turn(**kwargs))
        carry_transcripts.append(
            TranscriptResult(
                meta=TranscriptMeta(path=f"c{s}.jsonl", kind="top-level", session_id=f"carry-sess-{s}"),
                turns=turns,
            )
        )
    carry_section = carry.build_section(carry.compute_carry(carry_transcripts, pricing.resolve_model))

    # -- compaction_sim: the module's own plateau fixture (an 80,000-token
    # start, a jump to 280,000, then 40 replies that add nothing; no real
    # compact_boundary event; docs/compaction-sim.md's worked examples).
    cs_turns = [
        _turn(message_id="msg_cs_1", request_id="req_cs_1", turn_index=1, ts="2026-09-18T12:00:00.000Z",
              cache_creation_tokens=80_000, cc_5m=80_000, ctx=80_000),
        _turn(message_id="msg_cs_2", request_id="req_cs_2", turn_index=2, ts="2026-09-18T12:01:00.000Z",
              cache_creation_tokens=200_000, cache_read_tokens=80_000, cc_5m=200_000, ctx=280_000),
    ] + [
        _turn(
            message_id=f"msg_cs_{i}",
            request_id=f"req_cs_{i}",
            turn_index=i,
            ts=f"2026-09-18T12:{i:02d}:00.000Z",
            cache_creation_tokens=0,
            cache_read_tokens=280_000,
            cc_5m=0,
            cc_1h=0,
            ctx=280_000,
        )
        for i in range(3, 43)
    ]
    # Five copies, so the modelled saving at 100,000 (0.99 USD per
    # session) clears the default 1 USD bar.
    cs_transcripts = [
        TranscriptResult(
            meta=TranscriptMeta(path=f"cs{n}.jsonl", kind="top-level", session_id=f"cs-sess-{n}"), turns=cs_turns
        )
        for n in range(5)
    ]
    compaction_sim_section = compaction_sim.build_section(
        compaction_sim.simulate_compaction_windows(cs_transcripts, pricing.resolve_model, {})
    )

    # -- model_swap: a top-level session run entirely on Fable, cheap to
    # swap down a tier (test_model_swap.py's own top-level fixture).
    ms_transcript = TranscriptResult(
        meta=TranscriptMeta(path="ms.jsonl", kind="top-level", session_id="ms-sess"),
        turns=[_turn(model="claude-fable-5-1", input_tokens=1_000_000, output_tokens=1_000_000)],
    )
    model_swap_section = model_swap.build_section(model_swap.compute_model_swap([ms_transcript], pricing))

    # -- waste: one tool-error turn dwarfing a tiny useful turn
    # (test_waste.py's own _built_section_for_high_waste_share fixture).
    waste_lines = [
        turn_line(
            message_id="msg_w1",
            model="claude-sonnet-5",
            input_tokens=9_000_000,
            output_tokens=0,
            content=[tool_use_block("Bash", "tu_a", {"command": "ls /nope"})],
        ),
        user_block_line([tool_result_block("tu_a", "no such directory", is_error=True)]),
        turn_line(message_id="msg_w2", model="claude-sonnet-5", input_tokens=1_000_000, output_tokens=0),
    ]
    waste_path = tmp_path / "waste-session.jsonl"
    write_jsonl(waste_path, waste_lines)
    waste_result = parse_transcript(waste_path, TranscriptMeta(path=str(waste_path), session_id="waste-sess"))
    ws = waste.WasteStats(config_dir=tmp_path / "cfg")
    ws.add(waste_result, pricing)
    waste_section = waste.build_section(ws)

    report_model = _base_report(sessions=10, priced_turns=500)
    for section in (carry_section, compaction_sim_section, model_swap_section, waste_section):
        _add_section(report_model, section)

    config = Config(
        thresholds={
            "carry_share_pct": 1.0,
            "min_sample_results": 3,
            "model_swap": {"saving_pct_min": 10.0, "saving_usd_min": 1.0, "min_sessions": 1, "min_turns": 1},
            "waste": {"share_pct": 1.0, "min_sessions": 1, "min_turns": 1},
        }
    )
    recs = recommend_fn(report_model, config=config, archetype=None)

    found_ids = {rec.id for rec in recs}
    # The fixture's model swap is the main session's, which advice gives
    # a card of its own.
    for expected_id in ("tool-output-carry", "compaction-window", "model-tier-main", "wasted-turns"):
        assert expected_id in found_ids, f"expected {expected_id!r} to fire; got {sorted(found_ids)}"

    for rec in recs:
        for label, value, source_table, row_key in rec.evidence:
            section_key, table_name = source_table.split(".", 1)
            section = next((s for s in report_model.sections if s.key == section_key), None)
            assert section is not None, f"{rec.id}: no section {section_key!r} for evidence {label!r}"
            table = next((t for t in section.tables if t.name == table_name), None)
            assert table is not None, f"{rec.id}: no table {table_name!r} for evidence {label!r}"
            row = next((r for r in table.rows if r and r[0] == row_key), None)
            assert row is not None, f"{rec.id}: no row {row_key!r} in {source_table} for evidence {label!r}"


def test_render_patch_set_prefers_setting_changes_with_now_and_after():
    from claudeglass.model import SettingChange

    rec = dataclasses.replace(
        _make_recommendation(),
        lever="omitClaudeMd",
        changes=[
            SettingChange(target="agent", key="omitClaudeMd", agent="reviewer", value=True, current=False),
            SettingChange(target="agent", key="tools", agent="reviewer", suggested="only the tools it uses"),
            SettingChange(key="autoCompactWindow", value=120000),
        ],
    )
    text = render_patch_set([rec])
    assert "--- .claude/agents/reviewer.md" in text
    assert "-omitClaudeMd: false" in text
    assert "+omitClaudeMd: true" in text
    assert "+tools: (your choice: only the tools it uses)" in text
    assert "-autoCompactWindow: (unset)" in text
    assert "+autoCompactWindow: 120000" in text


def test_pricing_coverage_names_unknown_models_from_a_built_report(tmp_path):
    # End to end: build_report attaches usage.pricing_unknown_models, and
    # the recommendation names the unpriced model from it.
    from claudeglass.corpus import load_corpus
    from claudeglass.pricing import load_pricing
    from claudeglass.report import build_report

    from helpers import turn_line, write_jsonl

    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    for i in range(6):
        write_jsonl(
            project_dir / f"s{i}.jsonl",
            [turn_line(timestamp=f"2026-09-1{i}T10:00:00.000Z", model="claude-mystery-9")]
            + [turn_line(timestamp=f"2026-09-1{i}T10:0{j}:00.000Z") for j in range(1, 3)],
        )
    model = build_report(load_corpus([project_dir]), load_pricing(), Config(), projects=("proj",), window="w")

    usage = next(s for s in model.sections if s.key == "usage")
    unknown = next(t for t in usage.tables if t.name == "pricing_unknown_models")
    assert [row[0] for row in unknown.rows] == ["claude-mystery-9"]
    assert unknown.dashboard == "advanced" and unknown.help and unknown.help.shows
    rec = next(r for r in model.recommendations if r.id == "pricing-coverage")
    assert "claude-mystery-9" in rec.action


def test_a_newer_version_is_named_end_to_end_and_its_id_reaches_meta_model_ids(tmp_path):
    # claude-sonnet-5-7 has no row: it is priced as claude-sonnet-5 by
    # prefix. The rule says so in its own words, and meta.model_ids tells
    # the dashboard which meta.rates entry the observed ids use.
    import json

    from claudeglass.pricing import load_pricing
    from claudeglass.render.json_out import render_json
    from claudeglass.report import build_report

    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    for i in range(6):
        write_jsonl(
            project_dir / f"s{i}.jsonl",
            [turn_line(timestamp=f"2026-09-1{i}T10:00:00.000Z", model="claude-sonnet-5-7")]
            + [turn_line(timestamp=f"2026-09-1{i}T10:01:00.000Z", model="us.anthropic.claude-opus-5-5-v1:0")]
            + [turn_line(timestamp=f"2026-09-1{i}T10:02:00.000Z")],
        )
    pricing = load_pricing()
    model = build_report(load_corpus([project_dir]), pricing, Config(), projects=("proj",), window="w")

    rec = next(r for r in model.recommendations if r.id == "pricing-coverage")
    assert "There is no rate of its own for claude-sonnet-5-7 yet, so it was priced as claude-sonnet-5;" in rec.action
    assert rec.title == "A newer model is priced at an older model's rate"

    model_ids = model.meta.model_ids
    assert model_ids["claude-sonnet-5-7"] == "claude-sonnet-5"
    assert model_ids["us.anthropic.claude-opus-5-5-v1:0"] == "claude-opus-5-5"
    assert model_ids["sonnet"] == "claude-sonnet-5-5"
    assert "claude-sonnet-5" not in model_ids  # already a meta.rates key
    assert set(model_ids.values()) <= set(model.meta.rates)
    meta = json.loads(render_json(model))["report"]["meta"]
    assert meta["model_ids"] == model_ids


# -- effort-mismatch from work Claude reported easy (metrics capture) --------


def _habits_section(cycles):
    from claudeglass import habits

    return habits.section_from(habits.Habits(cycles=cycles))


def _easy_cycles(n: int, effort: str = "high", thinking: float = 0.6, output: float = 1.0):
    from claudeglass.habits import CycleFact
    from claudeglass.model import CaptureTag

    return [
        CycleFact(session_id="s", ts=None, week="", cost=1.0, turns=1, tag=CaptureTag(level="easy"), effort=effort,
                  output_cost=output, thinking_cost=thinking)
        for _ in range(n)
    ]


def test_effort_mismatch_reads_easy_work_at_high_effort_directly():
    r = _add_section(_base_report(), _habits_section(_easy_cycles(5) + _easy_cycles(2, effort="max")))
    rec = next(rec for rec in recommend_fn(r, config=_config(), archetype=None) if rec.id == "effort-mismatch")
    assert rec.lever == "effortLevel"
    # Only the effort level with enough easy messages is cited, each fact
    # from its own habits_effort_fit cell.
    assert rec.evidence == [
        ("Easy messages at high effort", 5, "habits.habits_effort_fit", "easy:high"),
        ("Easy work at high effort, thinking share of output", 60.0, "habits.habits_effort_fit", "easy:high"),
    ]
    # Half the thinking on each easy message.
    assert rec.saving_usd == pytest.approx(5 * 0.3)
    assert rec.title == "High effort is being spent on easy work"


@pytest.mark.parametrize("cycles", [
    _easy_cycles(4),
    _easy_cycles(5, thinking=0.2),
    _easy_cycles(5, effort="medium"),
])
def test_effort_mismatch_direct_path_needs_enough_easy_high_effort_thinking(cycles):
    r = _add_section(_base_report(), _habits_section(cycles))
    assert not any(rec.id == "effort-mismatch" for rec in recommend_fn(r, config=_config(), archetype=None))

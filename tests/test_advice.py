"""Tests for ``advice.finish``: the plain-language pass over
``recommend()``'s rules (consolidation, wording, ordering)."""

from __future__ import annotations

from claudeglass import advice, fixes, known_savers
from claudeglass.model import (
    Column,
    Diagnostics,
    PricingMeta,
    Recommendation,
    ReportMeta,
    ReportModel,
    Section,
    Table,
)
from claudeglass.snapshots import Snapshot
from claudeglass.units import Units


def _model_swap_report(rows) -> ReportModel:
    table = Table(
        name="model_swap_by_agent_type",
        columns=[
            Column(key="agent_type", label="Agent type"),
            Column(key="observed_model", label="Observed model"),
            Column(key="best_cheaper_alternative_model", label="Alternative"),
            Column(key="saving_usd", label="Saving", kind="money"),
        ],
        rows=rows,
    )
    return ReportModel(
        meta=ReportMeta(pricing=PricingMeta(coverage_pct=100.0)),
        sections=[Section(key="model_swap", title="Model swap", tables=[table])],
        diagnostics=Diagnostics(lines=1000),
    )


def _tier(agent_type: str, scope: str = "user") -> Recommendation:
    return Recommendation(
        id="model-tier",
        severity="advice",
        category="settings",
        title=f"{agent_type} could run a cheaper model tier",
        lever="model",
        scope=scope,
        agent_type=agent_type,
        evidence=[("Ceiling saving (%)", 40.0, "model_swap.model_swap_by_agent_type", agent_type)],
    )


def test_model_tier_cards_merge_into_one_with_a_change_per_agent_type():
    report = _model_swap_report(
        [
            ["top-level", "claude-opus-5-5", "claude-sonnet-5", 30.0],
            ["reviewer", "claude-sonnet-5", "claude-haiku-4-5-20251001", 50.0],
            ["general-purpose", "claude-sonnet-5", "claude-haiku-4-5-20251001", 10.0],
            ["workflow-subagent", "claude-opus-5-5", "claude-sonnet-5", 99.0],
        ]
    )
    snap = Snapshot(path=None, ts="2026-09-20T00:00:00Z", data={"agents": {"reviewer": {"source": "project"}}})
    recs = [_tier(a) for a in ("top-level", "reviewer", "general-purpose", "workflow-subagent")]
    out = advice.finish(recs, report, snap, Units())
    tier = [r for r in out if r.id == "model-tier"]
    assert len(tier) == 1
    changes = tier[0].changes
    # Largest saving first; workflow subagents can't be changed; the main
    # session gets a card of its own.
    assert [(c.agent, c.value) for c in changes] == [
        ("reviewer", "haiku"),
        ("general-purpose", "haiku"),
    ]
    reviewer, builtin = changes
    assert reviewer.target == "agent" and reviewer.scope == "repo" and not reviewer.new_agent_file
    assert builtin.new_agent_file
    # PROF-02: an agent-level change has no session-only path (unlike a
    # settings change, which --launch can scope to one session), so it's
    # labelled persistent and given the plain saving figure -- no "At
    # most" session-ceiling framing, unlike the top-level change.
    assert reviewer.note == (
        "Persistent: applies to every later run of this agent that isn't given a model when it starts, not only "
        "one session."
    )
    assert reviewer.saving and not reviewer.saving.startswith("At most")
    assert "aren't counted" not in tier[0].why
    assert tier[0].saving_usd == 60.0
    assert tier[0].estimated_saving.startswith("At most 60.00 USD")
    fixes.attach_fixes(tier)
    assert fixes.command_for(reviewer, tier[0].scope).startswith("claudeglass apply --set model=haiku")
    assert tier[0].fixes[1]["command"] is None  # a built-in needs a new agent file

    (main_card,) = [r for r in out if r.id == "model-tier-main"]
    (main,) = main_card.changes
    assert main.target == "settings" and main.key == "model" and main.value == "sonnet"
    assert main.note == "This changes the model for your main session in every project."
    assert main.saving.startswith("At most")
    assert main_card.saving_usd == 30.0
    assert main_card.title == "Your main session could run on Sonnet"
    assert "yours to make" in main_card.why


def test_the_model_tier_card_names_runs_an_agent_file_does_not_decide():
    """A workflow script or a model named at spawn sets some runs' model:
    the card says how many and where that model is really set, and names
    the model the file's own runs used."""
    table = Table(
        name="model_swap_by_agent_type",
        columns=[
            Column(key="agent_type", label="Agent type"),
            Column(key="observed_model", label="Observed model"),
            Column(key="best_cheaper_alternative_model", label="Alternative"),
            Column(key="saving_usd", label="Saving", kind="money"),
            Column(key="lever_model", label="Model on those runs"),
            Column(key="workflow_runs", label="Workflow runs", kind="int"),
            Column(key="spawn_model_runs", label="Spawn model runs", kind="int"),
        ],
        rows=[["reviewer", "claude-sonnet-5 (+1 more)", "claude-sonnet-5", 20.0, "claude-opus-5", 62, 1]],
    )
    report = ReportModel(
        meta=ReportMeta(pricing=PricingMeta(coverage_pct=100.0)),
        sections=[Section(key="model_swap", title="Model swap", tables=[table])],
        diagnostics=Diagnostics(lines=1000),
    )
    snap = Snapshot(path=None, ts="2026-09-20T00:00:00Z", data={"agents": {"reviewer": {"source": "project"}}})
    (card,) = [r for r in advice.finish([_tier("reviewer")], report, snap, Units()) if r.id == "model-tier"]
    (change,) = card.changes
    assert change.value == "sonnet"
    assert change.current == "not set (used claude-opus-5)"
    assert "62 runs a workflow script started (set the model in the script's agent() call)" in change.note
    assert "1 run given a model when it started" in change.note
    assert "Opus 5" in card.why and "aren't counted" in card.why


def test_the_main_session_model_card_ranks_after_the_other_advice():
    """A quality trade on your own model comes after the tips that keep
    it, however large its saving."""
    report = _model_swap_report([["top-level", "claude-opus-5-5", "claude-sonnet-5", 500.0]])
    snap = Snapshot(path=None, ts="2026-09-20T00:00:00Z", data={"agents": {}})
    compaction = Recommendation(
        id="compaction-window",
        severity="advice",
        category="settings",
        title="Summarise at 100,000 tokens",
        saving_usd=20.0,
    )
    out = advice.finish([_tier("top-level"), compaction], report, snap, Units())
    assert [r.id for r in out] == ["compaction-window", "model-tier-main"]


def test_model_tier_leaves_out_agents_already_on_the_cheaper_model():
    """The period's saving still counts runs from before the change, so an
    agent already moved must not be offered it again."""
    report = _model_swap_report(
        [
            ["top-level", "claude-opus-5-5", "claude-sonnet-5", 30.0],
            ["reviewer", "claude-sonnet-5", "claude-haiku-4-5-20251001", 50.0],
            ["implementer", "claude-opus-5-5", "claude-sonnet-5", 20.0],
        ]
    )
    snap = Snapshot(
        path=None,
        ts="2026-09-20T00:00:00Z",
        data={
            "effective": {"model": "claude-sonnet-5"},
            "agents": {"reviewer": {"source": "project", "model": "haiku"}, "implementer": {"source": "project"}},
        },
    )
    recs = [_tier(a) for a in ("top-level", "reviewer", "implementer")]
    (tier,) = [r for r in advice.finish(recs, report, snap, Units()) if r.id == "model-tier"]
    assert [(c.agent, c.value) for c in tier.changes] == [("implementer", "sonnet")]
    assert tier.saving_usd == 20.0


def test_model_tier_leaves_out_an_agent_that_did_worse_on_the_cheaper_model():
    report = _model_swap_report(
        [
            ["reviewer", "claude-sonnet-5", "claude-haiku-4-5-20251001", 50.0],
            ["implementer", "claude-opus-5-5", "claude-sonnet-5", 20.0],
        ]
    )
    report.sections.append(
        Section(
            key="quality",
            title="Quality",
            tables=[
                Table(
                    name="quality_by_setup",
                    columns=[Column(key=k, label=k) for k in ("agent_type", "model", "setup_verdict", "compared_model")],
                    rows=[["reviewer", "claude-haiku-4-5-20251001", "worse", "claude-sonnet-5"]],
                )
            ],
        )
    )
    snap = Snapshot(path=None, ts="2026-09-20T00:00:00Z", data={"agents": {}})
    recs = [_tier("reviewer"), _tier("implementer")]
    (tier,) = [r for r in advice.finish(recs, report, snap, Units()) if r.id == "model-tier"]
    assert [c.agent for c in tier.changes] == ["implementer"]
    assert "Some agents were left out because a cheaper model may not be enough." in tier.why
    assert "Did worse on a cheaper model: reviewer (Haiku)." in tier.why


def test_model_tier_is_dropped_when_every_agent_is_already_moved():
    report = _model_swap_report([["reviewer", "claude-sonnet-5", "claude-haiku-4-5-20251001", 50.0]])
    snap = Snapshot(
        path=None,
        ts="2026-09-20T00:00:00Z",
        data={"agents": {"reviewer": {"source": "user", "model": "claude-haiku-4-5-20251001"}}},
    )
    out = advice.finish([_tier("reviewer")], report, snap, Units())
    assert not any(r.id == "model-tier" for r in out)


def test_a_custom_agent_missing_from_the_snapshot_is_not_called_built_in():
    """The hook records only the agents of the project a session started
    in, so an absent custom agent may still have a file."""
    report = _model_swap_report([["implementer", "claude-opus-5-5", "claude-sonnet-5", 20.0]])
    snap = Snapshot(path=None, ts="2026-09-20T00:00:00Z", data={"agents": {}})
    (tier,) = [r for r in advice.finish([_tier("implementer")], report, snap, Units()) if r.id == "model-tier"]
    assert not tier.changes[0].new_agent_file


def test_cache_ttl_and_effort_cards_are_dropped_when_already_set():
    report = _model_swap_report([])
    snap = Snapshot(
        path=None,
        ts="2026-09-20T00:00:00Z",
        data={
            "effective": {"effortLevel": "Medium"},
            "agents": {"verification-runner": {"source": "user", "experimental.cacheTtl": "1h"}},
        },
    )
    recs = [
        Recommendation(
            id="ttl-switch",
            severity="advice",
            agent_type="verification-runner",
            evidence=[("TTL recommendation", "switch to 1h", "ttl", "verification-runner")],
        ),
        Recommendation(id="effort-mismatch", severity="advice"),
    ]
    assert advice.finish(recs, report, snap, Units()) == []


def test_already_set_matches_model_aliases_and_maps():
    assert fixes.already_set("model", "sonnet", "claude-sonnet-5")
    assert not fixes.already_set("model", "haiku", "claude-sonnet-5")
    assert not fixes.already_set("model", "sonnet", None)
    assert fixes.already_set("skillOverrides", {"pdf": "off"}, {"pdf": "off", "xlsx": "on"})
    assert not fixes.already_set("skillOverrides", {"pdf": "off", "xlsx": "off"}, {"pdf": "off"})
    assert fixes.already_set("omitClaudeMd", True, True)
    assert not fixes.already_set("maxTurns", 1, True)


def _compaction(id_, **kw) -> Recommendation:
    return Recommendation(id=id_, severity="advice", category="settings", lever="autoCompactWindow", **kw)


def test_compaction_window_wins_over_churn_and_takes_the_setting_from_long_context():
    report = _model_swap_report([])
    recs = [
        _compaction(
            "compaction-window",
            title="Set autoCompactWindow to at least 250,000",
            evidence=[("Candidate 250,000: modelled saving after rediscovery correction", 12.0, "x", "250,000")],
        ),
        _compaction("compaction-churn", title="churn"),
        _compaction("long-context-share", title="long"),
    ]
    out = advice.finish(recs, report, None, Units())
    ids = [r.id for r in out]
    assert "compaction-churn" not in ids
    window = next(r for r in out if r.id == "compaction-window")
    assert window.changes[0].value == 250_000
    assert window.saving_usd == 12.0
    long_ctx = next(r for r in out if r.id == "long-context-share")
    assert long_ctx.lever is None and long_ctx.changes == [] and long_ctx.category == "workflow"
    # Sorted by saving within a severity.
    assert ids[0] == "compaction-window"


def test_compaction_window_is_dropped_when_already_at_or_below_the_floor():
    report = _model_swap_report([])
    snap = Snapshot(path=None, ts="2026-09-20T00:00:00Z", data={"effective": {"autoCompactWindow": 200_000}})
    recs = [_compaction("compaction-window", title="Set autoCompactWindow to at least 250,000")]
    out = advice.finish(recs, report, snap, Units())
    assert not any(r.id == "compaction-window" for r in out)


def _env_window_snapshot(value: int, setting: int = 500_000) -> Snapshot:
    """CLAUDE_CODE_AUTO_COMPACT_WINDOW set, from the user's settings env
    block, over an autoCompactWindow setting it overrides."""
    return Snapshot(
        path=None,
        ts="2026-09-20T00:00:00Z",
        data={
            "effective": {"autoCompactWindow": setting},
            "env_names": ["CLAUDE_CODE_AUTO_COMPACT_WINDOW"],
            "env_numeric_caps": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": value},
            "effective_env_provenance": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "user"},
        },
    )


def test_compaction_window_changes_the_env_variable_while_it_overrides_the_setting():
    report = _model_swap_report([])
    recs = [_compaction("compaction-window", title="Set CLAUDE_CODE_AUTO_COMPACT_WINDOW to at least 250,000")]
    out = advice.finish(recs, report, _env_window_snapshot(400_000), Units())
    [change] = next(r for r in out if r.id == "compaction-window").changes
    assert change.key == "env.CLAUDE_CODE_AUTO_COMPACT_WINDOW"
    assert (change.value, change.current, change.scope) == ("250000", "400000", "user")


def test_compaction_window_is_dropped_when_the_env_variable_is_already_below_the_floor():
    # The setting (500,000) is above the floor, but the variable wins.
    report = _model_swap_report([])
    recs = [_compaction("compaction-window", title="Set CLAUDE_CODE_AUTO_COMPACT_WINDOW to at least 250,000")]
    out = advice.finish(recs, report, _env_window_snapshot(200_000), Units())
    assert not any(r.id == "compaction-window" for r in out)


def _replayed_report() -> ReportModel:
    """A report whose compaction replay priced the main sessions and found
    no window worth setting (the live case: 300,000 within 0.2% of the
    cheapest point, so ``compaction-window`` didn't fire)."""
    report = _model_swap_report([])
    table = Table(
        name="compaction_sim_by_window",
        columns=[Column(key="window", label="Window"), Column(key="compactions_per_session", label="Per session"),
                 Column(key="cost", label="Cost", kind="money"), Column(key="delta_usd", label="Delta", kind="money")],
        rows=[["200,000", 4.57, 322.6, 14.8], ["300,000", 1.6, 308.4, 0.55], ["none", 1.57, 307.88, 0.0]],
    )
    report.sections.append(Section(key="compaction_sim", title="Compaction-window sweep", tables=[table]))
    return report


def test_with_a_replay_verdict_churn_and_long_context_leave_the_window_to_it():
    snap = Snapshot(path=None, ts="2026-09-25T00:00:00Z", data={"effective": {"autoCompactWindow": 300_000}})
    recs = [
        _compaction("compaction-churn", title="churn",
                    evidence=[("Compactions per session (mean)", 4.2, "compactions.compactions_summary", "x")]),
        _compaction("long-context-share", title="long"),
    ]
    out = advice.finish(recs, _replayed_report(), snap, Units())
    assert [r.id for r in out] == ["long-context-share"]
    (long_ctx,) = out
    assert long_ctx.lever is None and long_ctx.changes == [] and long_ctx.category == "workflow"
    (fix,) = fixes.build_fixes(long_ctx)
    words = (fix["prompt"] + " ".join(text for _label, text in fix["explainer"])).lower()
    assert "subagent" in fix["prompt"]
    assert "lower" not in words and "sooner" not in words and "autocompactwindow" not in words


def test_without_a_replay_churn_and_long_context_never_point_the_window_opposite_ways():
    recs = [_compaction("compaction-churn", title="churn"), _compaction("long-context-share", title="long")]
    out = {r.id: r for r in advice.finish(recs, _model_swap_report([]), None, Units())}
    (raise_it,) = out["compaction-churn"].changes
    assert raise_it.key == "autoCompactWindow" and "larger" in raise_it.suggested
    assert out["long-context-share"].changes == [] and out["long-context-share"].lever is None
    # On its own, long-context-share still owns the setting.
    (alone,) = advice.finish([_compaction("long-context-share", title="long")], _model_swap_report([]), None, Units())
    assert alone.lever == "autoCompactWindow" and "smaller" in alone.changes[0].suggested
    # ...unless the replay has a verdict.
    (deferred,) = advice.finish([_compaction("long-context-share", title="long")], _replayed_report(), None, Units())
    assert deferred.lever is None and deferred.changes == []


def test_spawn_cost_is_dropped_for_agents_no_file_can_change():
    report = _model_swap_report([])
    recs = [
        Recommendation(id="spawn-cost", severity="advice", agent_type="workflow-subagent"),
        Recommendation(id="spawn-cost", severity="advice", agent_type="reviewer"),
    ]
    out = advice.finish(recs, report, None, None)
    assert [r.agent_type for r in out] == ["reviewer"]


def test_severity_orders_before_saving():
    report = _model_swap_report([])
    recs = [
        Recommendation(id="a", severity="info", saving_usd=500.0),
        Recommendation(id="b", severity="advice", saving_usd=1.0),
        Recommendation(id="c", severity="advice", saving_usd=10.0),
        Recommendation(id="d", severity="action"),
    ]
    assert [r.id for r in advice.finish(recs, report, None, None)] == ["d", "c", "b", "a"]


# -- pricing-coverage (fix 2): title/why depend on which of the two usage
# tables the rule cites have rows -----------------------------------------


def _usage_report(*tables: Table) -> ReportModel:
    return ReportModel(
        meta=ReportMeta(pricing=PricingMeta(coverage_pct=90.0)),
        sections=[Section(key="usage", title="Usage", tables=list(tables))],
        diagnostics=Diagnostics(lines=1000),
    )


def _unknown_models_table(rows) -> Table:
    return Table(
        name="pricing_unknown_models",
        columns=[
            Column(key="model_id", label="Model"),
            Column(key="turns", label="Turns"),
            Column(key="tokens", label="Tokens"),
        ],
        rows=rows,
    )


def _closest_match_table(rows) -> Table:
    return Table(
        name="pricing_closest_match",
        columns=[
            Column(key="model_id", label="Model"),
            Column(key="priced_as", label="Priced as"),
            Column(key="turns", label="Turns"),
            Column(key="tokens", label="Tokens"),
        ],
        rows=rows,
    )


def _pricing_coverage_rec() -> Recommendation:
    return Recommendation(id="pricing-coverage", severity="info", category="data", action="...")


def test_pricing_coverage_wording_when_only_unknown_models():
    report = _usage_report(_unknown_models_table([["claude-mystery-9", 3, 1000]]))
    out = advice.finish([_pricing_coverage_rec()], report, None, None)
    rec = out[0]
    assert rec.title == "Some usage has no price"
    assert "left out of every cost" in rec.why


def test_pricing_coverage_wording_when_only_closest_match():
    report = _usage_report(_closest_match_table([["claude-widget-9-preview", "claude-widget-9", 3, 1000]]))
    out = advice.finish([_pricing_coverage_rec()], report, None, None)
    rec = out[0]
    assert rec.title == "Some usage is priced by closest match, not its own rate"
    assert "estimated from the closest registered model" in rec.why


def test_pricing_coverage_wording_when_both_tables_present():
    report = _usage_report(
        _unknown_models_table([["claude-mystery-9", 3, 1000]]),
        _closest_match_table([["claude-widget-9-preview", "claude-widget-9", 3, 1000]]),
    )
    out = advice.finish([_pricing_coverage_rec()], report, None, None)
    rec = out[0]
    assert rec.title == "Some usage has no price, some is only an estimate"
    assert "left out of every cost" in rec.why
    assert "a different model's rate" in rec.why


def test_pricing_coverage_wording_falls_back_when_neither_table_present():
    # A report built with `include` leaving out `usage` (or one with no
    # rows in either table) still gets the plain "no price" wording.
    report = _usage_report()
    out = advice.finish([_pricing_coverage_rec()], report, None, None)
    rec = out[0]
    assert rec.title == "Some usage has no price"


def test_model_tier_leaves_out_an_agent_often_retried_on_a_larger_model():
    report = _model_swap_report([["reviewer", "claude-sonnet-5", "claude-haiku-4-5-20251001", 50.0]])
    report.sections.append(
        Section(
            key="quality",
            title="Quality",
            tables=[
                Table(
                    name="quality_retried",
                    columns=[Column(key=k, label=k) for k in ("agent_type", "model", "runs", "retried", "retried_on")],
                    rows=[["reviewer", "claude-haiku-4-5-20251001", 5, 2, "claude-sonnet-5"]],
                )
            ],
        )
    )
    snap = Snapshot(path=None, ts="2026-09-20T00:00:00Z", data={"agents": {}})
    assert not any(r.id == "model-tier" for r in advice.finish([_tier("reviewer")], report, snap, Units()))



def test_model_tier_leaves_out_an_agent_whose_runs_said_they_needed_a_larger_model():
    from claudeglass import habits

    report = _model_swap_report(
        [
            ["reviewer", "claude-sonnet-5", "claude-haiku-4-5-20251001", 50.0],
            ["implementer", "claude-opus-5-5", "claude-sonnet-5", 20.0],
        ]
    )
    runs = [habits.AgentFact(session_id="s", agent_type="reviewer", week="", cost=1.0, fit=fit)
            for fit in ("larger", "larger", "larger", "smaller", "smaller")]
    report.sections.append(habits.section_from(habits.Habits(agents=runs)))
    snap = Snapshot(path=None, ts="2026-09-20T00:00:00Z", data={"agents": {}})
    (tier,) = [r for r in advice.finish([_tier("reviewer"), _tier("implementer")], report, snap, Units())
               if r.id == "model-tier"]
    assert [c.agent for c in tier.changes] == ["implementer"]
    assert "Claude said a larger model was needed: reviewer (3 runs)." in tier.why


def test_model_tier_left_out_note_groups_agents_by_reason_and_names_only_a_few():
    """However many agents are left out, the note stays a few short
    sentences: one per reason, naming at most three agents each."""
    note = advice._left_out_note(
        [("hard", f"agent-{i} (90%)") for i in range(5)] + [("retried", "implementer (4 of 31 Haiku runs)")]
    )
    assert note == (
        " Some agents were left out because a cheaper model may not be enough."
        " Edits often redone on a larger model: implementer (4 of 31 Haiku runs)."
        " Much of the work reported hard: agent-0 (90%), agent-1 (90%), agent-2 (90%) and 2 more."
    )
    assert all(len(sentence.split()) <= 25 for sentence in note.split(". "))
    assert advice._left_out_note([]) == ""


def test_model_tier_why_names_models_as_people_say_them():
    assert advice._model_prose("claude-opus-5 (+1 more)") == "Opus 5 and 1 other model"
    assert advice._model_prose("claude-haiku-4-5-20251001 (+2 more)") == "Haiku 4.5 and 2 other models"
    assert advice._model_prose("claude-sonnet-5") == "Sonnet 5"


def test_effort_mismatch_from_reported_work_is_explained_as_measured():
    rec = Recommendation(
        id="effort-mismatch",
        severity="advice",
        category="settings",
        title="x",
        lever="effortLevel",
        saving_usd=1.5,
        evidence=[
            ("Easy messages at high effort", 6, "habits.habits_effort_fit", "easy:high"),
            ("Easy work at high effort, thinking share of output", 55.0, "habits.habits_effort_fit", "easy:high"),
        ],
    )
    report = ReportModel(meta=ReportMeta(pricing=PricingMeta(coverage_pct=100.0)), sections=[],
                         diagnostics=Diagnostics(lines=1000))
    snap = Snapshot(path=None, ts="2026-09-20T00:00:00Z", data={"effective": {}})
    (out,) = [r for r in advice.finish([rec], report, snap, Units()) if r.id == "effort-mismatch"]
    assert out.title == "High effort is being spent on easy work"
    assert out.why == (
        "Claude reported 6 of your messages as easy work, yet they ran at high effort or above, and up to 55% of "
        "their output was thinking."
    )
    assert out.changes[0].key == "effortLevel" and out.changes[0].value == "medium"
    assert out.estimated_saving.startswith("About ")


def test_effort_mismatch_change_keeps_the_recommendations_own_scope():
    # COV-01: every _EXPLAIN entry used to flatten anything but "managed"
    # down to "user" (`scope="managed" if rec.scope == "managed" else
    # "user"`), silently dropping a "project-local"/"repo" scope
    # recommend.py had already worked out. _advice_scope should carry it
    # through unchanged (aside from the compaction_sim.py "project" ->
    # "repo" alias, exercised elsewhere).
    rec = Recommendation(
        id="effort-mismatch",
        severity="advice",
        category="settings",
        title="x",
        lever="effortLevel",
        scope="project-local",
        evidence=[
            ("Easy messages at high effort", 6, "habits.habits_effort_fit", "easy:high"),
        ],
    )
    report = ReportModel(meta=ReportMeta(pricing=PricingMeta(coverage_pct=100.0)), sections=[],
                         diagnostics=Diagnostics(lines=1000))
    snap = Snapshot(path=None, ts="2026-09-20T00:00:00Z", data={"effective": {}})
    (out,) = [r for r in advice.finish([rec], report, snap, Units()) if r.id == "effort-mismatch"]
    assert out.changes[0].scope == "project-local"


# -- long-context-share: evidence lookup, title, tokensave variant --------


def _tokensave_report() -> ReportModel:
    """A report where tokensave (github.com/aovestdipaperino/tokensave)
    was at work in the window: ``known_savers.active_in_report`` reads
    this the same way it reads a real one, via a ``carry_by_tool`` row
    whose key is one of tokensave's MCP tools."""
    table = Table(
        name="carry_by_tool",
        columns=[Column(key="key", label="Tool"), Column(key="count", label="Count")],
        rows=[["mcp__tokensave__tokensave_search", 5]],
    )
    return ReportModel(
        meta=ReportMeta(pricing=PricingMeta(coverage_pct=100.0)),
        sections=[Section(key="carry", title="Carry", tables=[table])],
        diagnostics=Diagnostics(lines=1000),
    )


def _long_context_share_rec(*, p90=None, share_pct=None) -> Recommendation:
    evidence = []
    if share_pct is not None:
        evidence.append(("Cache-read volume share from huge-context turns", share_pct, "recache.recache_huge_context", "all"))
    if p90 is not None:
        evidence.append(("Context size 9 in 10 main-session replies stay under", p90, "scorecard.dimensions", "context_hygiene"))
    return Recommendation(id="long-context-share", severity="advice", category="workflow", lever=None, evidence=evidence)


def test_long_context_share_why_names_the_p90_number_and_title_says_main_session():
    # recommend._rule_long_context_share labels this evidence row
    # "Context size 9 in 10 main-session replies stay under" -- the old
    # advice.py lookup searched for "p90", a substring that label never
    # had, so the whole clause silently dropped out of rec.why.
    (out,) = advice.finish([_long_context_share_rec(p90=236_196)], _model_swap_report([]), None, Units())
    assert "236,196" in out.why
    assert "One main-session reply in ten read more than" in out.why
    assert "Your main session" in out.title


def test_long_context_share_title_does_not_claim_main_session_when_only_share_fired():
    # The share clause counts every reply, subagents included -- only the
    # p90 clause is about the main session specifically.
    (out,) = advice.finish([_long_context_share_rec(share_pct=25.0)], _model_swap_report([]), None, Units())
    assert "main session" not in out.title.lower()


def test_long_context_share_tokensave_variant_avoids_subagent_and_explore_wording():
    rec = _long_context_share_rec(p90=236_196)
    (out,) = advice.finish([rec], _tokensave_report(), None, Units())
    assert out.variant == "tokensave"
    (fix,) = fixes.build_fixes(out)
    text = (fix["prompt"] + " " + out.action + " " + " ".join(t for _h, t in fix["explainer"])).lower()
    assert "tokensave" in text
    assert "subagent" not in text
    assert "explore" not in text


def test_long_context_share_ignores_tokensave_tried_once_in_a_busy_window():
    # One scratch session's tokensave calls among a month of work
    # shouldn't reword every fix for it (known_savers.ADVICE_MIN_SHARE).
    table = Table(
        name="carry_by_tool",
        columns=[Column(key="key", label="Tool"), Column(key="result_count", label="Results")],
        rows=[["Read", 480], ["Bash", 500], ["mcp__tokensave__tokensave_search", 2]],
    )
    report = ReportModel(
        meta=ReportMeta(pricing=PricingMeta(coverage_pct=100.0)),
        sections=[Section(key="carry", title="Carry", tables=[table])],
        diagnostics=Diagnostics(lines=1000),
    )
    (out,) = advice.finish([_long_context_share_rec(p90=236_196)], report, None, Units())
    assert out.variant == ""
    assert known_savers.active_in_report(report)
    assert not known_savers.active_in_report(report, min_share=known_savers.ADVICE_MIN_SHARE)


def test_long_context_share_without_tokensave_still_mentions_a_subagent():
    rec = _long_context_share_rec(p90=236_196)
    (out,) = advice.finish([rec], _model_swap_report([]), None, Units())
    assert out.variant == ""
    (fix,) = fixes.build_fixes(out)
    assert "subagent" in fix["prompt"].lower()

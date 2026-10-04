"""Tests for v4-model-swap: the model-swap counterfactual
(``src/claudeglass/model_swap.py``).

Most branches are exercised on hand-built ``model.Turn``/
``model.TranscriptResult`` instances (the ``_turn``/``_transcript``
helpers below, matching ``test_ttl.py``'s convention) with hand-computed
costs at the packaged rate card (``load_pricing()`` -- the real
``pricing.toml``, never a fake one, so a rate-card edit that changes
these numbers is caught here too). One test goes through the real
parser (``parse_transcript`` + ``tests/helpers.py``'s JSONL builders) to
exercise ``helpers.assert_privacy`` on a genuine ``TranscriptResult``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claudeglass import model, model_swap
from claudeglass.model import (
    Diagnostics,
    PricingMeta,
    ReportMeta,
    ReportModel,
    Section,
    TranscriptMeta,
    TranscriptResult,
)
from claudeglass.parse import parse_transcript
from claudeglass.pricing import load_pricing
from claudeglass.render.tables import format_cell
from claudeglass.units import Units

from helpers import assert_privacy, elasticity_with_slope, turn_line, write_jsonl

PRICING = load_pricing()

#: An observed Sonnet id. The cheaper tier a row names is whatever the
#: rate card's "sonnet" alias points at, PRICING.aliases["sonnet"].
SONNET = "claude-sonnet-5"
HAIKU = "claude-haiku-4-5-20251001"
OPUS = "claude-opus-5"
FABLE = "claude-fable-5-1"


def _turn(**overrides) -> model.Turn:
    """Build a priced ``Turn`` with sane zero defaults, overridable per
    field -- same convention as ``test_ttl.py``'s own ``_turn`` helper."""
    fields = dict(
        message_id="msg_1",
        request_id="req_1",
        turn_index=1,
        ts="2026-09-18T12:00:00.000Z",
        model=SONNET,
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
    return model.Turn(**fields)


def _transcript(
    turns: list[model.Turn],
    kind: str = "top-level",
    agent_type: str | None = None,
    agent_model_alias: str | None = None,
) -> TranscriptResult:
    meta = TranscriptMeta(
        path="synthetic.jsonl",
        kind=kind,
        session_id="sess1",
        agent_type=agent_type,
        agent_model_alias=agent_model_alias,
    )
    return TranscriptResult(meta=meta, turns=turns)


def _cost(model_id: str, **fields) -> float:
    """Hand-computed cost of one turn's ``fields`` at ``model_id``'s
    packaged rates, using the exact same formula as
    ``pricing.price_turn`` (input/output/cache_write_5m/cache_write_1h/
    cache_read, each per-million-token) -- an independent restatement,
    not a call into the code under test."""
    rates = PRICING.models[model_id]
    input_tokens = fields.get("input_tokens", 0)
    output_tokens = fields.get("output_tokens", 0)
    cc_5m = fields.get("cc_5m", 0)
    cc_1h = fields.get("cc_1h", 0)
    cache_read_tokens = fields.get("cache_read_tokens", 0)
    return (
        input_tokens / 1_000_000 * rates.input
        + output_tokens / 1_000_000 * rates.output
        + cc_5m / 1_000_000 * rates.cache_write_5m
        + cc_1h / 1_000_000 * rates.cache_write_1h
        + cache_read_tokens / 1_000_000 * rates.cache_read
    )


# -- repricing arithmetic ------------------------------------------------------


def test_reprice_arithmetic_matches_hand_computation_at_two_models():
    fields = dict(input_tokens=100_000, output_tokens=50_000, cc_5m=20_000, cache_read_tokens=10_000)
    tr = _transcript([_turn(model=SONNET, **fields)])

    stats = model_swap.compute_model_swap([tr], PRICING)
    row = stats.by_key["top-level"]

    expected_observed = _cost(SONNET, **fields)
    expected_haiku = _cost(HAIKU, **fields)
    expected_sonnet_alt = _cost(SONNET, **fields)

    assert row.observed_cost == pytest.approx(expected_observed)
    assert row.cost_by_model[HAIKU] == pytest.approx(expected_haiku)
    assert row.cost_by_model[SONNET] == pytest.approx(expected_sonnet_alt)
    # Repricing at the turn's own observed model must equal the observed
    # cost exactly -- both paths call price_turn with the same rates and
    # the same (unmodified) token volumes/write split.
    assert row.cost_by_model[SONNET] == pytest.approx(row.observed_cost)


def test_reprice_holds_token_volumes_and_write_split_constant():
    """A mixed 5m/1h write, repriced at another model, must scale each
    bucket by that model's own 5m/1h rate independently -- never a flat
    blend -- proving the observed write split (not just the totals) is
    preserved across the counterfactual."""
    fields = dict(input_tokens=0, output_tokens=0, cc_5m=40_000, cc_1h=10_000, cache_read_tokens=0)
    tr = _transcript([_turn(model=SONNET, **fields)])

    stats = model_swap.compute_model_swap([tr], PRICING)
    row = stats.by_key["top-level"]

    expected_fable = _cost(FABLE, **fields)
    assert row.cost_by_model[FABLE] == pytest.approx(expected_fable)
    # Sanity: the two write buckets really do carry different rates at
    # Fable (12.5 vs 20.0), so this test would fail if the code under
    # test collapsed cc_5m/cc_1h into one flat bucket.
    fable_rates = PRICING.models[FABLE]
    assert fable_rates.cache_write_5m != fable_rates.cache_write_1h


# -- alias handling -------------------------------------------------------------


def test_alias_handling_bare_alias_resolves_same_as_full_id():
    fields = dict(input_tokens=200_000, output_tokens=100_000)
    tr_alias = _transcript([_turn(model="sonnet", **fields)], kind="subagent", agent_type="claude-implementer")
    tr_full = _transcript([_turn(model=SONNET, **fields)], kind="subagent", agent_type="claude-implementer")

    stats_alias = model_swap.compute_model_swap([tr_alias], PRICING)
    stats_full = model_swap.compute_model_swap([tr_full], PRICING)

    row_alias = stats_alias.by_key["claude-implementer"]
    row_full = stats_full.by_key["claude-implementer"]
    # The bare alias "sonnet" resolves (via Pricing.resolve_model) to the
    # exact same rate as the full canonical id -- observed cost agrees.
    assert row_alias.observed_cost == pytest.approx(row_full.observed_cost)
    # The tier verdict (which needs a family match) also agrees, since
    # workstyle.model_tier substring-matches "sonnet" either way.
    assert row_alias.tier_verdict.state == row_full.tier_verdict.state == "cheaper_available"
    assert row_alias.tier_verdict.alt_model == row_full.tier_verdict.alt_model == HAIKU


# -- which runs the agent file's model line decides ------------------------------


def _run(model_id: str, agent_type: str, *, kind: str = "subagent", spawn_model: str | None = None) -> TranscriptResult:
    return _transcript(
        [_turn(model=model_id, input_tokens=1_000_000, output_tokens=1_000_000)],
        kind=kind,
        agent_type=agent_type,
        agent_model_alias=spawn_model,
    )


def test_model_set_by_follows_claude_codes_order():
    """.meta.json's ``model`` is the model the spawn asked for (absent when
    it asked for none) and wins over the agent file; a workflow script
    sets its agents' model whatever their type."""
    assert model_swap.model_set_by(_run(OPUS, "", kind="top-level")) == "settings"
    assert model_swap.model_set_by(_run(OPUS, "reviewer")) == "agent file"
    assert model_swap.model_set_by(_run(SONNET, "reviewer", spawn_model="sonnet")) == "spawn"
    assert model_swap.model_set_by(_run(OPUS, "reviewer", kind="workflow-agent")) == "workflow"
    assert model_swap.model_set_by(_run(SONNET, "reviewer", kind="workflow-agent", spawn_model="sonnet")) == "workflow"
    assert model_swap.model_set_by(_run(OPUS, "workflow-subagent", kind="workflow-agent")) == "workflow"
    assert model_swap.model_set_by(_run(OPUS, "fork")) == "none"


def test_the_assumptions_give_a_workflow_agents_model_order_and_no_workflow_default():
    """A workflow agent's model is its call's, else its agent type's file's,
    else CLAUDE_CODE_SUBAGENT_MODEL, else the main session's: the run file's
    defaultModel only records the main model at launch."""
    text = " ".join(model_swap.ASSUMPTIONS)

    for part in (
        "agent() call, else its agent type's file, else CLAUDE_CODE_SUBAGENT_MODEL, else your main session's model",
        "defaultModel only records the main model at launch",
    ):
        assert part in text
    assert "workflow's default" not in text


def _row_cells(section: Section, agent_type: str, table_name: str = "model_swap_by_agent_type") -> dict:
    table = next(t for t in section.tables if t.name == table_name)
    row = next(r for r in table.rows if r[0] == agent_type)
    return dict(zip([c.key for c in table.columns], row))


def _firing_th() -> model_swap.ModelSwapThresholds:
    return model_swap.ModelSwapThresholds(saving_pct_min=10.0, saving_usd_min=1.0, min_sessions=1, min_turns=1)


def test_a_named_type_with_workflow_runs_prices_its_direct_runs_only():
    direct = [_run(OPUS, "reviewer") for _ in range(2)]
    workflow = [_run(OPUS, "reviewer", kind="workflow-agent") for _ in range(3)]
    stats = model_swap.compute_model_swap(direct + workflow, PRICING)
    alone = model_swap.compute_model_swap(direct, PRICING).by_key["reviewer"]
    row = stats.by_key["reviewer"]

    # The row still counts every run and every dollar.
    assert row.spawns == 5
    assert row.observed_cost == pytest.approx(5 * alone.observed_cost / 2)
    assert row.workflow_runs == 3
    # The saving is the direct runs' alone.
    assert row.tier_verdict.alt_model == PRICING.aliases["sonnet"]
    assert row.tier_verdict.saving_usd == pytest.approx(alone.tier_verdict.saving_usd)
    assert row.tier_verdict.saving_pct == pytest.approx(alone.tier_verdict.saving_pct)
    assert "on the 2 runs its agent file sets" in row.tier_verdict.label

    section = model_swap.build_section(stats)
    cells = _row_cells(section, "reviewer")
    assert (cells["spawns"], cells["lever_runs"], cells["workflow_runs"], cells["spawn_model_runs"]) == (5, 2, 3, 0)
    assert cells["observed_cost"] == pytest.approx(row.observed_cost)
    assert cells["lever_cost"] == pytest.approx(alone.observed_cost)
    assert cells["saving_usd"] == pytest.approx(alone.tier_verdict.saving_usd)
    file_cells = _row_cells(section, "reviewer", "model_swap_agent_file_runs")
    assert file_cells["runs"] == 2
    assert file_cells[f"cost_{SONNET}"] == pytest.approx(alone.cost_by_model[SONNET])

    report = _report_with_section(section)
    (rec,) = model_swap.RULES["model-tier"](report, _firing_th(), archetype=None, snapshot=None)
    assert "on the 2 runs started without a model" in rec.action
    assert "3 runs a workflow script started" in rec.action
    assert "agent() call" in rec.action
    _assert_evidence_resolves(report, rec)


def test_a_type_with_only_workflow_runs_gets_no_agent_file_lever():
    runs = [_run(OPUS, "reviewer", kind="workflow-agent") for _ in range(4)]
    stats = model_swap.compute_model_swap(runs, PRICING)
    row = stats.by_key["reviewer"]

    assert row.spawns == 4 and row.observed_cost > 0
    assert row.tier_verdict.state == "set_elsewhere"
    assert row.tier_verdict.alt_model is None
    assert row.tier_verdict.saving_usd == 0.0
    assert "4 came from workflow scripts" in row.tier_verdict.label

    section = model_swap.build_section(stats)
    assert not next(t for t in section.tables if t.name == "model_swap_agent_file_runs").rows
    summary = next(t for t in section.tables if t.name == "model_swap_summary")
    assert summary.rows[0][1] == 0
    report = _report_with_section(section)
    assert model_swap.RULES["model-tier"](report, _firing_th(), archetype=None, snapshot=None) == []


def test_runs_given_a_model_when_started_are_left_out_of_the_agent_file_saving():
    """Real transcripts show the spawn's model wins: an agent file set to
    Opus whose spawn asked for Sonnet ran on Sonnet. So those runs aren't
    the agent file's to change."""
    direct = [_run(OPUS, "reviewer") for _ in range(2)]
    named = [_run(SONNET, "reviewer", spawn_model="sonnet") for _ in range(3)]
    stats = model_swap.compute_model_swap(direct + named, PRICING)
    alone = model_swap.compute_model_swap(direct, PRICING).by_key["reviewer"]
    row = stats.by_key["reviewer"]

    assert row.spawn_model_runs == 3
    # Most of the row ran on Sonnet, but the runs the file decides ran on
    # Opus, so its cheaper tier is Sonnet, priced on those runs alone.
    assert row.observed_model == SONNET
    assert row.tier_verdict.alt_model == PRICING.aliases["sonnet"]
    assert row.tier_verdict.saving_usd == pytest.approx(alone.tier_verdict.saving_usd)

    section = model_swap.build_section(stats)
    assert _row_cells(section, "reviewer")["lever_model"] == OPUS
    report = _report_with_section(section)
    (rec,) = model_swap.RULES["model-tier"](report, _firing_th(), archetype=None, snapshot=None)
    assert f"currently effectively {OPUS} on the 2 runs started without a model" in rec.action
    assert "3 runs given a model when they started" in rec.action
    assert "workflow" not in rec.action

    only_named = model_swap.compute_model_swap(named, PRICING).by_key["reviewer"]
    assert only_named.tier_verdict.state == "set_elsewhere"


def test_workflow_subagents_have_no_agent_file_lever():
    stats = model_swap.compute_model_swap([_run(OPUS, "workflow-subagent", kind="workflow-agent")], PRICING)
    row = stats.by_key["workflow-subagent"]
    assert row.tier_verdict.state == "no_lever"
    assert row.tier_verdict.saving_usd == 0.0
    cells = _row_cells(model_swap.build_section(stats), "workflow-subagent")
    assert cells["lever"] == "none (the workflow script sets it)"
    assert cells["lever_runs"] == 0 and cells["workflow_runs"] == 1


def test_a_workflow_script_sets_the_model_by_the_runs_kind_and_not_by_the_agent_type_it_is_named_for():
    """Only a run a workflow script started (the transcript's kind) has the script
    as its lever; an agent named ``workflow-subagent`` that Claude Code started is not."""
    script = model_swap.compute_model_swap([_run(OPUS, "workflow-subagent", kind="workflow-agent")], PRICING)
    assert "a workflow script sets its model" in script.by_key["workflow-subagent"].tier_verdict.label
    direct = model_swap.compute_model_swap([_run(OPUS, "workflow-subagent")], PRICING)
    row = direct.by_key["workflow-subagent"]
    assert row.tier_verdict.state == "no_lever" and "Claude Code sets its model" in row.tier_verdict.label
    assert _row_cells(model_swap.build_section(direct), "workflow-subagent")["lever"] == "none (Claude Code picks)"
    mixed = model_swap.compute_model_swap(
        [_run(OPUS, "workflow-subagent", kind="workflow-agent"), _run(OPUS, "workflow-subagent")], PRICING
    )
    assert "Claude Code sets its model" in mixed.by_key["workflow-subagent"].tier_verdict.label
    assert _row_cells(model_swap.build_section(mixed), "workflow-subagent")["lever"] == "none (Claude Code picks)"


def test_a_fork_has_no_lever_and_claude_code_picks_its_model_whatever_runs_beside_it():
    stats = model_swap.compute_model_swap([_run(OPUS, "fork")], PRICING)
    assert "Claude Code sets its model" in stats.by_key["fork"].tier_verdict.label
    assert _row_cells(model_swap.build_section(stats), "fork")["lever"] == "none (Claude Code picks)"


# -- unknown model ---------------------------------------------------------------


def test_unknown_model_excluded_from_observed_cost_with_a_note():
    tr = _transcript([_turn(model="not-a-real-claude-model-id", input_tokens=100_000, output_tokens=50_000)])
    stats = model_swap.compute_model_swap([tr], PRICING)
    row = stats.by_key["top-level"]

    assert row.unpriced_turns == 1
    assert row.observed_cost == 0.0
    # Every alternative is still repriced -- the counterfactual doesn't
    # need the observed model to resolve, only the turn's own tokens.
    assert row.cost_by_model[SONNET] > 0.0

    section = model_swap.build_section(stats)
    joined_notes = " ".join(section.notes)
    assert "unpriced" in joined_notes.lower() or "unknown model" in joined_notes.lower()


# -- already-cheapest state -------------------------------------------------------


def test_already_cheapest_state_reports_no_saving():
    tr = _transcript(
        [_turn(model=HAIKU, input_tokens=100_000, output_tokens=50_000)],
        kind="subagent",
        agent_type="general-purpose",
    )
    stats = model_swap.compute_model_swap([tr], PRICING)
    row = stats.by_key["general-purpose"]

    assert row.tier_verdict.state == "already_cheapest"
    assert row.tier_verdict.alt_model is None
    assert row.tier_verdict.saving_usd == 0.0
    assert row.tier_verdict.saving_pct == 0.0
    assert "already on the cheapest model" in row.tier_verdict.label

    section = model_swap.build_section(stats)
    table = next(t for t in section.tables if t.name == "model_swap_by_agent_type")
    alt_model_idx = [c.key for c in table.columns].index("best_cheaper_alternative_model")
    saving_idx = [c.key for c in table.columns].index("saving_usd")
    row_cells = next(r for r in table.rows if r[0] == "general-purpose")
    assert row_cells[alt_model_idx] is None
    assert row_cells[saving_idx] == 0.0


def test_a_sonnet_main_session_is_never_offered_haiku():
    """The main session does the hard, open-ended work, so Sonnet is its
    floor: no alternative, no saving, and so no card, lever or goal."""
    tr = _transcript([_turn(model=SONNET, input_tokens=1_000_000, output_tokens=500_000)], kind="top-level")
    stats = model_swap.compute_model_swap([tr], PRICING)
    row = stats.by_key["top-level"]

    assert row.tier_verdict.state == "main_floor"
    assert row.tier_verdict.alt_model is None
    assert row.tier_verdict.saving_usd == 0.0
    assert "smallest model suggested for your main session" in row.tier_verdict.label
    # What Haiku would have cost is still shown, as information only.
    assert row.cost_by_model[HAIKU] > 0

    report = ReportModel(
        meta=ReportMeta(pricing=PricingMeta(coverage_pct=100.0)),
        sections=[model_swap.build_section(stats)],
        diagnostics=Diagnostics(lines=1000),
    )
    th = model_swap.ModelSwapThresholds(min_sessions=1, min_turns=1)
    assert model_swap.RULES["model-tier"](report, th, archetype=None, snapshot=None) == []


def test_a_sonnet_subagent_is_still_offered_haiku():
    tr = _transcript(
        [_turn(model=SONNET, input_tokens=1_000_000, output_tokens=500_000)],
        kind="subagent",
        agent_type="reviewer",
    )
    stats = model_swap.compute_model_swap([tr], PRICING)
    assert stats.by_key["reviewer"].tier_verdict.state == "cheaper_available"
    assert stats.by_key["reviewer"].tier_verdict.alt_model == HAIKU


def test_no_priced_turns_reports_no_data_not_a_false_already_cheapest():
    tr = _transcript([], kind="subagent", agent_type="claude-implementer")
    stats = model_swap.compute_model_swap([tr], PRICING)
    row = stats.by_key["claude-implementer"]
    assert row.tier_verdict.state == "no_data"
    assert row.tier_verdict.alt_model is None


# -- corpus-wide summary --------------------------------------------------------


def _agent(model_id: str, agent_type: str, tokens: int = 1_000_000) -> TranscriptResult:
    return _transcript(
        [_turn(model=model_id, input_tokens=tokens, output_tokens=tokens)],
        kind="subagent",
        agent_type=agent_type,
    )


def test_corpus_summary_only_counts_qualifying_fable_or_opus_subagent_rows():
    top = _transcript([_turn(model=FABLE, input_tokens=1_000_000, output_tokens=1_000_000)], kind="top-level")
    fable_agent = _agent(FABLE, "overseer")
    opus_agent = _agent(OPUS, "claude-implementer")
    sonnet_agent = _agent(SONNET, "Explore")
    haiku_agent = _agent(HAIKU, "statusline-setup")

    stats = model_swap.compute_model_swap(
        [top, fable_agent, opus_agent, sonnet_agent, haiku_agent], PRICING
    )
    section = model_swap.build_section(stats)
    summary = next(t for t in section.tables if t.name == "model_swap_summary")
    row = summary.rows[0]
    columns = [c.key for c in summary.columns]

    # Only the Fable (-> Opus) and Opus (-> Sonnet) subagent rows count;
    # top-level is excluded by scope, sonnet/haiku rows aren't Fable/Opus.
    assert row[columns.index("agent_types")] == 2

    expected_saving = (
        stats.by_key["overseer"].tier_verdict.saving_usd
        + stats.by_key["claude-implementer"].tier_verdict.saving_usd
    )
    expected_observed = stats.by_key["overseer"].observed_cost + stats.by_key["claude-implementer"].observed_cost
    assert row[columns.index("saving_usd")] == pytest.approx(expected_saving)
    assert row[columns.index("observed_cost_usd")] == pytest.approx(expected_observed)
    assert row[columns.index("cost_after_tier_down_usd")] == pytest.approx(expected_observed - expected_saving)


# -- RULES / recommendation ------------------------------------------------------


def _report_with_section(section: Section, units: Units | None = None) -> ReportModel:
    report = ReportModel(
        meta=ReportMeta(pricing=PricingMeta(coverage_pct=100.0)),
        sections=[section],
        recommendations=[],
        diagnostics=Diagnostics(),
    )
    report.units = units
    return report


def _assert_evidence_resolves(report: ReportModel, rec) -> None:
    for label, value, source_table, row_key in rec.evidence:
        section_key, table_name = source_table.split(".", 1)
        section = next(s for s in report.sections if s.key == section_key)
        table = next(t for t in section.tables if t.name == table_name)
        row = next(r for r in table.rows if r[0] == row_key)
        # Find a column whose cell equals the cited value (mirrors
        # test_recommend_contract.py's own tolerant match: int/float and
        # str/number crossings are fine, the cited number must be real).
        assert any(cell == value for cell in row), (label, value, row)


def test_rule_fires_when_saving_exceeds_thresholds_and_evidence_resolves():
    tr = _agent(FABLE, "claude-implementer")
    stats = model_swap.compute_model_swap([tr], PRICING)
    th = model_swap.ModelSwapThresholds(saving_pct_min=10.0, saving_usd_min=1.0, min_sessions=1, min_turns=1)
    section = model_swap.build_section(stats, th)
    report = _report_with_section(section)

    recs = model_swap.RULES["model-tier"](report, th, archetype=None, snapshot=None)
    assert len(recs) == 1
    rec = recs[0]
    assert rec.id == "model-tier"
    assert rec.agent_type == "claude-implementer"
    assert rec.lever == "model"
    assert rec.scope == "repo"
    assert ".claude/agents/claude-implementer.md" in rec.action
    assert OPUS in rec.action
    _assert_evidence_resolves(report, rec)


def test_rule_action_and_table_label_have_no_bare_dollar_under_a_subscription():
    """UX-2 / finding F1-F2: a subscription's ``model-tier`` action and
    the section table's "cheaper_available" label must route through
    Units, never a raw f"${...:.2f}"."""
    units = Units(billing_mode="subscription", currency="USD", elasticity=elasticity_with_slope())
    tr = _agent(FABLE, "claude-implementer")
    stats = model_swap.compute_model_swap([tr], PRICING)
    th = model_swap.ModelSwapThresholds(saving_pct_min=10.0, saving_usd_min=1.0, min_sessions=1, min_turns=1)
    section = model_swap.build_section(stats, th, units=units)
    report = _report_with_section(section, units=units)

    recs = model_swap.RULES["model-tier"](report, th, archetype=None, snapshot=None)
    assert len(recs) == 1
    rec = recs[0]
    assert "$" not in rec.action
    assert "about about" not in rec.action.lower()

    table = next(t for t in section.tables if t.name == "model_swap_by_agent_type")
    label_idx = [c.key for c in table.columns].index("best_cheaper_alternative")
    labels = " ".join(str(row[label_idx]) for row in table.rows if row[label_idx])
    assert "$" not in labels
    assert "about about" not in labels.lower()


def test_rule_uses_settings_json_and_user_scope_for_top_level():
    tr = _transcript([_turn(model=FABLE, input_tokens=1_000_000, output_tokens=1_000_000)], kind="top-level")
    stats = model_swap.compute_model_swap([tr], PRICING)
    th = model_swap.ModelSwapThresholds(saving_pct_min=10.0, saving_usd_min=1.0, min_sessions=1, min_turns=1)
    section = model_swap.build_section(stats, th)
    report = _report_with_section(section)

    recs = model_swap.RULES["model-tier"](report, th, archetype=None, snapshot=None)
    assert len(recs) == 1
    rec = recs[0]
    assert rec.agent_type == "top-level"
    assert rec.scope == "user"
    assert "settings.json" in rec.action


def test_rule_does_not_fire_below_default_min_sample():
    tr = _agent(FABLE, "claude-implementer")
    stats = model_swap.compute_model_swap([tr], PRICING)
    th = model_swap.ModelSwapThresholds()  # default min_sessions=5, min_turns=200
    section = model_swap.build_section(stats, th)
    report = _report_with_section(section)

    recs = model_swap.RULES["model-tier"](report, th, archetype=None, snapshot=None)
    assert recs == []


def test_rule_does_not_fire_below_saving_floors():
    tr = _agent(FABLE, "claude-implementer")
    stats = model_swap.compute_model_swap([tr], PRICING)
    th = model_swap.ModelSwapThresholds(saving_pct_min=99.0, saving_usd_min=1_000_000.0, min_sessions=1, min_turns=1)
    section = model_swap.build_section(stats, th)
    report = _report_with_section(section)

    recs = model_swap.RULES["model-tier"](report, th, archetype=None, snapshot=None)
    assert recs == []


def test_rule_never_fires_for_already_cheapest_row():
    tr = _agent(HAIKU, "general-purpose")
    stats = model_swap.compute_model_swap([tr], PRICING)
    th = model_swap.ModelSwapThresholds(saving_pct_min=0.0, saving_usd_min=0.0, min_sessions=1, min_turns=1)
    section = model_swap.build_section(stats, th)
    report = _report_with_section(section)

    recs = model_swap.RULES["model-tier"](report, th, archetype=None, snapshot=None)
    assert recs == []


def test_rule_suppressed_for_chat_only_archetype_on_subagent_row_but_not_top_level():
    top = _transcript([_turn(model=FABLE, input_tokens=1_000_000, output_tokens=1_000_000)], kind="top-level")
    sub = _agent(FABLE, "claude-implementer")
    stats = model_swap.compute_model_swap([top, sub], PRICING)
    th = model_swap.ModelSwapThresholds(saving_pct_min=10.0, saving_usd_min=1.0, min_sessions=1, min_turns=1)
    section = model_swap.build_section(stats, th)
    report = _report_with_section(section)

    recs = model_swap.RULES["model-tier"](report, th, archetype="chat-only", snapshot=None)
    agent_types = {r.agent_type for r in recs}
    assert agent_types == {"top-level"}


# -- rendering sanity -------------------------------------------------------------


def test_every_cell_renders_without_error():
    tr = _agent(FABLE, "claude-implementer")
    stats = model_swap.compute_model_swap([tr], PRICING)
    section = model_swap.build_section(stats)
    for table in section.tables:
        for row in table.rows:
            for column, cell in zip(table.columns, row):
                format_cell(cell, column.kind)


# -- privacy ----------------------------------------------------------------------


def test_privacy(tmp_path: Path):
    top_lines = [
        turn_line(message_id="msg_1", model=SONNET, input_tokens=1000, output_tokens=500),
        turn_line(message_id="msg_2", model=SONNET, input_tokens=2000, output_tokens=800, cache_creation_input_tokens=500),
    ]
    top_path = tmp_path / "top.jsonl"
    write_jsonl(top_path, top_lines)
    top = parse_transcript(top_path, TranscriptMeta(path=str(top_path), kind="top-level", session_id="sess1"))

    sub_lines = [turn_line(message_id="msg_3", model=FABLE, input_tokens=5000, output_tokens=2000)]
    sub_path = tmp_path / "sub.jsonl"
    write_jsonl(sub_path, sub_lines)
    sub = parse_transcript(
        sub_path,
        TranscriptMeta(
            path=str(sub_path),
            kind="subagent",
            session_id="sess1",
            agent_type="claude-implementer",
            agent_model_alias="fable",
        ),
    )

    stats = model_swap.compute_model_swap([top, sub], PRICING)
    th = model_swap.ModelSwapThresholds(saving_pct_min=0.0, saving_usd_min=0.0, min_sessions=1, min_turns=1)
    section = model_swap.build_section(stats, th)
    assert_privacy(section)

    report = _report_with_section(section)
    recs = model_swap.RULES["model-tier"](report, th, archetype=None, snapshot=None)
    for rec in recs:
        assert_privacy(rec)



def test_measured_work_is_cited_but_never_changes_the_saving():
    from claudeglass import habits

    tr = _agent(FABLE, "claude-implementer")
    stats = model_swap.compute_model_swap([tr], PRICING)
    th = model_swap.ModelSwapThresholds(saving_pct_min=10.0, saving_usd_min=1.0, min_sessions=1, min_turns=1)
    report = _report_with_section(model_swap.build_section(stats, th))
    (plain,) = model_swap.RULES["model-tier"](report, th, archetype=None, snapshot=None)
    # Four runs, 20 calls, 8 of them one read-only look; two runs edited, after 2 and after 4 calls.
    runs = [habits.AgentFact(session_id="s", agent_type="claude-implementer", week="", cost=1.0, level=level,
                             calls=calls, probe_calls=probes, calls_before_edit=edit)
            for level, calls, probes, edit in (("easy", 4, 3, 2), ("easy", 6, 3, 4), ("normal", 5, 0, None),
                                               ("easy", 5, 2, None))]
    report.sections.append(habits.section_from(habits.Habits(agents=runs)))
    (rec,) = model_swap.RULES["model-tier"](report, th, archetype=None, snapshot=None)
    assert rec.evidence[: len(plain.evidence)] == plain.evidence
    assert rec.evidence[len(plain.evidence):] == [
        ("Work reported easy (%)", 75.0, "habits.habits_agents", "claude-implementer"),
        ("Calls that were a single read-only look (%)", 40.0, "habits.habits_agents", "claude-implementer"),
        ("Calls before the first edit", 3.0, "habits.habits_agents", "claude-implementer"),
    ]
    # The saving is the same with or without them, and nothing says what Claude thought of its own model.
    assert rec.saving_usd == plain.saving_usd and not any("said" in label for label, *_ in rec.evidence)
    _assert_evidence_resolves(report, rec)
    # Runs whose first call was an edit measured 0, the clearest sign a smaller model fits: it is kept.
    # A run that changed nothing measured nothing, and its row says None.
    def before_edit_evidence(first):
        fresh = _report_with_section(model_swap.build_section(stats, th))
        runs = [habits.AgentFact(session_id="s", agent_type="claude-implementer", week="", cost=1.0, level="normal",
                                 calls=4, probe_calls=0, calls_before_edit=first)] * 2
        fresh.sections.append(habits.section_from(habits.Habits(agents=runs)))
        (fresh_rec,) = model_swap.RULES["model-tier"](fresh, th, archetype=None, snapshot=None)
        _assert_evidence_resolves(fresh, fresh_rec)
        return [item for item in fresh_rec.evidence if item[0] == "Calls before the first edit"]

    assert before_edit_evidence(0) == [
        ("Calls before the first edit", 0.0, "habits.habits_agents", "claude-implementer")
    ]
    assert before_edit_evidence(None) == []

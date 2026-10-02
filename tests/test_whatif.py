"""What if? Estimates looked up in the report's own tables (``whatif``)."""

from __future__ import annotations

from types import SimpleNamespace as NS

from claudeglass import whatif
from claudeglass.units import Units

UNITS = Units(billing_mode="api", currency="USD")
PERIOD = "over the last 14 days"


def _table(name: str, rows: list[dict]):
    keys = list(rows[0]) if rows else []
    return NS(name=name, columns=[NS(key=k) for k in keys], rows=[[row[k] for k in keys] for row in rows])


def _model(**extra):
    sections = [
        NS(key="model_swap", tables=[_table("model_swap_by_agent_type", [
            # observed_model (PROF-11/F14): a model that doesn't gate
            # goals._thinking's effortLevel/effort candidate (V26 --
            # Opus 5.5 and the Fable models are the ones that do).
            {"agent_type": "top-level", "observed_model": "claude-sonnet-5",
             "observed_cost": 100.0, "cost_claude-sonnet-4-5": 60.0, "cost_claude-haiku-4-5": 20.0},
            {"agent_type": "Explore", "observed_model": "claude-haiku-4-5",
             "observed_cost": 10.0, "cost_claude-sonnet-4-5": 6.0, "cost_claude-haiku-4-5": 2.0},
        ])]),
        NS(key="ttl", tables=[_table("ttl_by_agent_type", [
            {"agent_type": "top-level", "cost_observed": 50.0, "cost_all_5m": 55.0, "cost_all_1h": 45.0},
            {"agent_type": "Explore", "cost_observed": 5.0, "cost_all_5m": 4.0, "cost_all_1h": 6.0},
            {"agent_type": "Plan", "cost_observed": 3.0, "cost_all_5m": 2.0, "cost_all_1h": 3.5},
        ])]),
        NS(key="compaction_sim", tables=[_table("compaction_sim_by_window", [
            {"window": "none", "cost": 100.0, "compactions_per_session": 0.2},
            {"window": "200,000", "cost": 80.0, "compactions_per_session": 1.4},
        ])]),
        NS(key="agent_startup", tables=[_table("agent_startup_breakdown", [
            {"agent_type": "Explore", "claude_md": 5000, "spawns": 100, "write_price": 3.75},
        ])]),
        NS(key="agents", tables=[_table("topology_effort_by_agent_type", [
            {"agent_type": "top-level", "thinking_share": 40.0},
        ])]),
        # PROF-08: replies actually billed at a fast-mode rate this
        # window, and what the same replies would have cost standard.
        NS(key="usage", tables=[_table("pricing_fast_applied", [
            {"model_id": "claude-opus-5-5", "turns": 4, "tokens": 40_000, "cost": 20.0, "standard_cost": 10.0},
            {"model_id": "claude-sonnet-5", "turns": 2, "tokens": 10_000, "cost": 5.0, "standard_cost": 3.0},
        ])]),
    ]
    context_files = {"skills": [
        {"name": "dataviz", "listing_cost_usd": 1.0},
        {"name": "engineering:review", "listing_cost_usd": 0.5},
    ]}
    return NS(sections=sections, context_files=extra.get("context_files", context_files))


def _estimate(settings=None, agents=None, **kw):
    return whatif.estimate(settings or {}, agents or {}, _model(), UNITS, period=PERIOD, **kw)


def test_opusplan_reprices_the_builds_after_approved_plans_at_sonnet():
    model = _model()
    model.sections.append(NS(key="plan_handoff", tables=[_table("plan_handoff_summary", [
        {"scope": "main sessions", "build_usd": 30.0, "build_usd_sonnet": 18.0},
    ])]))
    [row] = whatif.estimate({"model": "opusplan"}, {}, model, UNITS, period=PERIOD)["rows"]
    assert (row["saving_usd"], row["fidelity"]) == (12.0, "ceiling")
    assert "without a plan run on Sonnet too" in row["basis"]
    [none] = _estimate({"model": "opusplan"})["rows"]
    assert none["saving_usd"] is None and none["fidelity"] == "none"


def test_a_cheaper_main_model_is_repriced_from_the_model_swap_table():
    [row] = _estimate({"model": "sonnet"})["rows"]
    assert row["saving_usd"] == 40.0
    # E3/EST-P1: a model reprice is a ceiling (same tokens, new rate), not
    # a genuine simulation like autoCompactWindow/cache TTL below -- it
    # can't capture a different model needing more or fewer replies.
    assert row["fidelity"] == "ceiling"
    assert row["effect_text"] == f"Saves 40.00 USD {PERIOD}"


def test_model_ceiling_fidelity_text_is_distinct_from_simulated():
    [row] = _estimate({"model": "sonnet"})["rows"]
    row["fidelity_text"] = whatif.FIDELITY_TEXT.get(row["fidelity"], "")
    assert row["fidelity_text"].startswith("Ceiling:")
    assert row["fidelity_text"] != whatif.FIDELITY_TEXT["simulated"]


def test_an_agent_model_uses_that_agent_row():
    [row] = _estimate(agents={"Explore": {"model": "haiku"}})["rows"]
    assert (row["agent"], row["saving_usd"]) == ("Explore", 8.0)


def test_an_agent_model_prices_only_the_runs_its_agent_file_decides():
    """A workflow script or a model named at spawn sets the other runs'
    model, so the agent file's what-if leaves them out."""
    model = _model()
    model.sections[0].tables.append(_table("model_swap_agent_file_runs", [
        {"agent_type": "Explore", "runs": 2, "observed_cost": 4.0, "cost_claude-sonnet-4-5": 2.5,
         "cost_claude-haiku-4-5": 1.0},
    ]))
    [row] = whatif.estimate({}, {"Explore": {"model": "haiku"}}, model, UNITS, period=PERIOD)["rows"]
    assert (row["saving_usd"], row["fidelity"]) == (3.0, "ceiling")
    assert "started without a model of their own" in row["basis"]
    # The main session is still priced on its whole row.
    [top] = whatif.estimate({"model": "sonnet"}, {}, model, UNITS, period=PERIOD)["rows"]
    assert top["saving_usd"] == 40.0
    # An agent none of whose runs followed its file has nothing to price.
    model.sections[0].tables[-1].rows.clear()
    [none] = whatif.estimate({}, {"Explore": {"model": "haiku"}}, model, UNITS, period=PERIOD)["rows"]
    assert none["saving_usd"] is None and none["fidelity"] == "none"
    assert "workflow script" in none["basis"]


def _swap_model(columns: dict, model_ids: dict | None = None):
    """A report whose main session row carries only ``columns`` (and,
    with ``model_ids``, a ``meta.model_ids`` like a real report's)."""
    row = {"agent_type": "top-level", "observed_model": "claude-fable-5", "observed_cost": 100.0, **columns}
    model = NS(sections=[NS(key="model_swap", tables=[_table("model_swap_by_agent_type", [row])])])
    if model_ids is not None:
        model.meta = NS(model_ids=model_ids)
    return model


def test_an_alias_prices_at_the_column_the_rate_card_names():
    # "best" names no column itself; the rate card's alias table puts it
    # on Fable 5.1, as it does "sonnet[1m]" on Sonnet 5.5.
    columns = {"cost_claude-fable-5-1": 70.0, "cost_claude-sonnet-5-5": 30.0, "cost_claude-sonnet-5": 25.0}
    model = _swap_model(columns, {"best": "claude-fable-5-1", "sonnet[1m]": "claude-sonnet-5-5"})
    [best] = whatif.estimate({"model": "best"}, {}, model, UNITS, period=PERIOD)["rows"]
    assert (best["saving_usd"], best["fidelity"]) == (30.0, "ceiling")
    assert "at claude-fable-5-1" in best["basis"]
    [sonnet] = whatif.estimate({"model": "sonnet[1m]"}, {}, model, UNITS, period=PERIOD)["rows"]
    assert sonnet["saving_usd"] == 70.0
    # Without meta.model_ids, an alias that names no column stays unpriced.
    [none] = whatif.estimate({"model": "best"}, {}, _swap_model(columns), UNITS, period=PERIOD)["rows"]
    assert none["saving_usd"] is None and none["basis"] == "No price for best in the rate card."


def test_an_alias_whose_target_has_no_column_falls_back_to_the_column_names():
    model = _swap_model({"cost_claude-sonnet-5": 25.0}, {"sonnet": "claude-sonnet-5-5"})
    [row] = whatif.estimate({"model": "sonnet"}, {}, model, UNITS, period=PERIOD)["rows"]
    assert row["saving_usd"] == 75.0


def test_the_newest_model_is_picked_by_version_number_not_by_spelling():
    # As text, "claude-opus-4-9" sorts after "claude-opus-4-10".
    columns = {"cost_claude-opus-4-9": 60.0, "cost_claude-opus-4-10": 40.0, "cost_claude-3-opus-20240229": 90.0}
    [row] = whatif.estimate({"model": "opus"}, {}, _swap_model(columns), UNITS, period=PERIOD)["rows"]
    assert row["saving_usd"] == 60.0
    assert "at claude-opus-4-10" in row["basis"]
    # A date is not a version: Sonnet 5.5 beats a dated Sonnet 5.
    columns = {"cost_claude-sonnet-5-20260101": 10.0, "cost_claude-sonnet-5-5": 20.0}
    [row] = whatif.estimate({"model": "sonnet"}, {}, _swap_model(columns), UNITS, period=PERIOD)["rows"]
    assert "at claude-sonnet-5-5" in row["basis"]


def test_subagent_ttl_sums_every_subagent_and_can_cost_more():
    [row] = _estimate({"subagentPromptCacheTtl": "1h"})["rows"]
    assert row["saving_usd"] == -1.5
    assert row["effect_text"] == f"Costs 1.50 USD more {PERIOD}"


def test_compaction_compares_against_the_current_window():
    [row] = _estimate({"autoCompactWindow": 200000})["rows"]
    assert row["saving_usd"] == 20.0
    [unknown] = _estimate({"autoCompactWindow": 123})["rows"]
    assert unknown["fidelity"] == "none" and "200000" in unknown["basis"]


def test_compaction_is_not_estimated_past_the_same_compactions_per_session_floor_the_goal_uses():
    # EST-P2: whatif._compact holds a candidate window to the same
    # CompactionSimThresholds().max_compactions_per_session floor
    # goals._compaction already filters its own candidates by.
    model = _model()
    [table] = next(s for s in model.sections if s.key == "compaction_sim").tables
    table.rows.append(["50,000", 40.0, 2.5])  # window, cost, compactions_per_session
    row = whatif.estimate({"autoCompactWindow": 50000}, {}, model, UNITS, period=PERIOD)["rows"][0]
    assert row["fidelity"] == "none"
    assert "2.5" in row["basis"] and "2" in row["basis"]


def test_omit_claude_md_is_measured_per_spawn():
    [row] = _estimate(agents={"Explore": {"omitClaudeMd": True}})["rows"]
    assert row["fidelity"] == "measured"
    assert row["saving_usd"] == 5000 * 100 * 3.75 / 1_000_000


def test_omit_claude_md_excludes_managed_claude_md_which_still_loads():
    # PROF-11/F13: Managed policy CLAUDE.md loads regardless of
    # omitClaudeMd, so only the remaining 3,000 tokens are priced.
    model = _model()
    [table] = next(s for s in model.sections if s.key == "agent_startup").tables
    table.columns.append(NS(key="claude_md_managed"))
    table.rows[0].append(2000)
    row = whatif.estimate({}, {"Explore": {"omitClaudeMd": True}}, model, UNITS, period=PERIOD)["rows"][0]
    assert row["fidelity"] == "measured"
    assert row["saving_usd"] == 3000 * 100 * 3.75 / 1_000_000
    assert "Managed policy CLAUDE.md (2,000 tokens) still loads either way." in row["basis"]


def test_omit_claude_md_not_estimated_when_only_managed_claude_md_is_seen():
    model = _model()
    [table] = next(s for s in model.sections if s.key == "agent_startup").tables
    table.columns.append(NS(key="claude_md_managed"))
    table.rows[0].append(5000)  # equal to the total: nothing left to omit
    row = whatif.estimate({}, {"Explore": {"omitClaudeMd": True}}, model, UNITS, period=PERIOD)["rows"][0]
    assert row["fidelity"] == "none"


def test_omit_claude_md_prices_the_carry_cost_when_higher_than_the_write_floor():
    # EST-P10: carrying CLAUDE.md across the rest of a spawn's turns
    # (cache reads until it's re-sent) usually costs more than the one
    # write the floor above prices. Managed CLAUDE.md's own carry cost
    # is excluded too (F13 -- it still loads either way).
    context_files = {"files": [
        {"type": "Project", "cost_by_reach": {"Explore": 5.0}},
        {"type": "Managed", "cost_by_reach": {"Explore": 100.0}},
    ]}
    model = _model(context_files=context_files)
    row = whatif.estimate({}, {"Explore": {"omitClaudeMd": True}}, model, UNITS, period=PERIOD)["rows"][0]
    assert row["saving_usd"] == 5.0
    assert row["fidelity"] == "measured"
    assert "Carrying it across the rest of each spawn's turns" in row["basis"]


def test_omit_claude_md_never_prices_below_the_write_floor():
    context_files = {"files": [{"type": "Project", "cost_by_reach": {"Explore": 0.5}}]}
    model = _model(context_files=context_files)
    row = whatif.estimate({}, {"Explore": {"omitClaudeMd": True}}, model, UNITS, period=PERIOD)["rows"][0]
    assert row["saving_usd"] == 5000 * 100 * 3.75 / 1_000_000
    assert "Carrying it across the rest of each spawn's turns" not in row["basis"]


def test_omit_claude_md_ignores_another_agents_carry_cost():
    context_files = {"files": [{"type": "Project", "cost_by_reach": {"reviewer": 50.0}}]}
    model = _model(context_files=context_files)
    row = whatif.estimate({}, {"Explore": {"omitClaudeMd": True}}, model, UNITS, period=PERIOD)["rows"][0]
    assert row["saving_usd"] == 5000 * 100 * 3.75 / 1_000_000


def test_skills_and_plugins_use_the_listing_cost():
    rows = _estimate({"skillOverrides": {"dataviz": "off"}, "enabledPlugins": {"engineering@marketplace": False}})["rows"]
    assert [r["saving_usd"] for r in rows] == [1.0, 0.5]


def test_effort_is_never_estimated_but_says_how_much_was_thinking():
    [row] = _estimate({"effortLevel": "medium"})["rows"]
    assert row["saving_usd"] is None and row["effect_text"] == "Not estimated"
    assert "40%" in row["basis"]


# -- PROF-08: fastMode, repriced from pricing_fast_applied -------------------


def test_fast_mode_off_reprices_fast_applied_turns_at_standard():
    [row] = _estimate({"fastMode": False})["rows"]
    # 20.0 - 10.0 (opus) + 5.0 - 3.0 (sonnet) -- every model row summed.
    assert row["saving_usd"] == 12.0
    assert row["fidelity"] == "simulated"
    assert row["effect_text"] == f"Saves 12.00 USD {PERIOD}"


def test_fast_mode_on_is_not_simulated():
    [row] = _estimate({"fastMode": True})["rows"]
    assert row["saving_usd"] is None and row["fidelity"] == "none"
    assert row["basis"] == "This change isn't simulated."


def test_fast_mode_off_without_fast_applied_data_is_not_estimated():
    model = NS(sections=[])
    [row] = whatif.estimate({"fastMode": False}, {}, model, UNITS)["rows"]
    assert row["saving_usd"] is None and row["fidelity"] == "none"
    assert "No fast-priced replies in this window" in row["basis"]


def test_total_counts_only_estimated_rows_and_notes_overlap():
    out = _estimate({"model": "sonnet", "autoCompactWindow": 200000, "effortLevel": "low"})
    assert out["total_usd"] == 60.0
    assert (out["estimated"], out["not_estimated"]) == (2, 1)
    assert out["total_note"]
    assert _estimate({"model": "sonnet"})["total_note"] == ""


def test_unknown_keys_and_empty_reports_are_not_estimated():
    out = whatif.estimate({"model": "sonnet", "cleanupPeriodDays": 7}, {}, NS(sections=[]), UNITS)
    assert [r["fidelity"] for r in out["rows"]] == ["none", "none"]
    assert out["total_text"] == ""


# -- EST-P6: calibration -----------------------------------------------------


def test_a_row_with_no_calibration_entry_is_untouched():
    [row] = _estimate({"model": "sonnet"}, calibration={(None, "promptCacheTtl"): 2.0})["rows"]
    assert row["saving_usd"] == 40.0
    assert row["fidelity"] == "ceiling"
    assert row["uncalibrated_usd"] is None and row["uncalibrated_fidelity"] is None


def test_a_matching_calibration_scales_the_row_and_changes_its_fidelity():
    [row] = _estimate({"model": "sonnet"}, calibration={(None, "model"): 0.5})["rows"]
    assert row["saving_usd"] == 20.0
    assert row["fidelity"] == "calibrated"
    assert row["fidelity_text"] == whatif.FIDELITY_TEXT["calibrated"]
    # The pre-calibration value survives, for a caller (route_whatif's
    # "log": true) that must log the raw estimate, not a calibrated one.
    assert row["uncalibrated_usd"] == 40.0
    assert row["uncalibrated_fidelity"] == "ceiling"


def test_calibration_keys_on_agent_and_the_raw_settings_key():
    [row] = _estimate(agents={"Explore": {"model": "haiku"}}, calibration={("Explore", "model"): 1.5})["rows"]
    assert row["agent"] == "Explore" and row["key"] == "model"
    assert row["saving_usd"] == row["uncalibrated_usd"] * 1.5
    assert row["fidelity"] == "calibrated"


def test_calibration_never_invents_a_saving_for_a_row_that_had_none():
    [row] = _estimate({"effortLevel": "medium"}, calibration={(None, "effortLevel"): 3.0})["rows"]
    assert row["saving_usd"] is None
    assert row["fidelity"] == "none"
    assert row["uncalibrated_usd"] is None

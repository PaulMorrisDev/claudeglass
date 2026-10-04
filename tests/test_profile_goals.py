"""Create a profile from a goal (``profiles.goals``)."""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from claudeglass.profiles import goals
from claudeglass.units import Units

from test_whatif import _model

UNITS = Units(billing_mode="api", currency="USD")


def _report(recommendations=()):
    model = _model()
    model.recommendations = list(recommendations)
    return model


def _change(key, agent, value):
    return NS(key=key, agent=agent, value=value)


def test_recommendations_goal_ticks_everything_but_the_main_model():
    rec = NS(title="A cheaper model", changes=[
        _change("model", None, "sonnet"),
        _change("model", "reviewer", "sonnet"),
        _change("tools", "reviewer", None),
    ])
    out = goals.draft("recommendations", _report([rec]), UNITS, effective={"model": "opus"})
    ticks = {(c["agent"], c["key"]): c["ticked"] for c in out["candidates"]}
    assert ticks == {(None, "model"): False, ("reviewer", "model"): True}
    main = out["candidates"][0]
    assert main["now"] == "opus" and main["tradeoff"]
    assert out["profile"] == {"settings": {}, "agents": {"reviewer": {"model": "sonnet"}}}


def test_ignored_recommendations_stay_out_of_the_draft():
    kept = NS(key="kept", title="Kept", changes=[_change("model", "reviewer", "sonnet")])
    ignored = NS(key="ignored", title="Ignored", changes=[_change("model", "writer", "sonnet")])
    for goal in ("recommendations", "subagents", "models"):
        out = goals.draft(goal, _report([kept, ignored]), UNITS, skip_keys=frozenset({"ignored"}))
        agents = {c["agent"] for c in out["candidates"] if c.get("evidence", "").startswith("Recommended")}
        assert "writer" not in agents and "reviewer" in agents, goal


def test_models_goal_reads_the_cheapest_alternative_per_agent():
    model = _report()
    [table] = model.sections[0].tables
    table.columns.append(NS(key="best_cheaper_alternative_model"))
    table.columns.append(NS(key="saving_pct"))
    table.rows[0] += ["claude-sonnet-4-5", 40.0]
    table.rows[1] += ["claude-haiku-4-5-20251001", 80.0]
    out = goals.draft("models", model, UNITS)
    rows = {(c["agent"], c["value"], c["ticked"]) for c in out["candidates"]}
    assert rows == {(None, "sonnet", False), ("Explore", "haiku", True)}
    explore = next(c for c in out["candidates"] if c["agent"] == "Explore")
    assert explore["estimate"]["saving_usd"] == 8.0
    assert explore["evidence"] == "Explore's replies in this window would have cost 80% less on claude-haiku-4-5-20251001."


def test_models_goal_evidence_names_the_runs_the_agent_file_decides():
    model = _report()
    [table] = model.sections[0].tables
    for key in ("best_cheaper_alternative_model", "saving_pct", "workflow_runs", "spawn_model_runs"):
        table.columns.append(NS(key=key))
    table.rows[0] += ["claude-sonnet-4-5", 40.0, 0, 0]
    table.rows[1] += ["claude-haiku-4-5-20251001", 80.0, 12, 0]
    out = goals.draft("models", model, UNITS)
    explore = next(c for c in out["candidates"] if c["agent"] == "Explore")
    assert explore["evidence"] == (
        "The replies of Explore's runs started without a model of their own would have cost 80% less on "
        "claude-haiku-4-5-20251001 in this window."
    )


def _with_plans(observed="claude-opus-5-5", build=100.0, sonnet=60.0):
    model = _model()
    model.sections[0].tables[0].rows[0][1] = observed
    model.sections.append(NS(key="plan_handoff", tables=[_table("plan_handoff_summary", [
        {"scope": "main sessions", "build_usd": build, "build_usd_sonnet": sonnet},
    ])]))
    return model


def test_models_goal_offers_opusplan_unticked_when_the_main_session_ran_on_opus():
    result = goals.draft("models", _with_plans(), UNITS, effective={"model": "opus"})
    [main] = [c for c in result["candidates"] if c["agent"] is None]
    assert (main["key"], main["value"], main["ticked"]) == ("model", "opusplan", False)
    assert "40% less on Sonnet" in main["evidence"] and "/model opusplan" in main["evidence"]
    assert main["estimate"]["saving_usd"] == pytest.approx(40.0)
    assert main["estimate"]["fidelity"] == "ceiling"


def test_opusplan_needs_an_opus_main_session_and_a_build_to_reprice():
    for model in (_with_plans(observed="claude-sonnet-5"), _with_plans(build=0.0), _with_plans(sonnet=None)):
        result = goals.draft("models", model, UNITS)
        assert all(c["value"] != "opusplan" for c in result["candidates"])
    # Already on opusplan: nothing to offer, not taken for plain Opus.
    result = goals.draft("models", _with_plans(), UNITS, effective={"model": "opusplan"})
    assert all(c["value"] != "opusplan" for c in result["candidates"])


def test_models_goal_skips_a_model_the_quality_check_found_worse():
    model = _report()
    [table] = model.sections[0].tables
    table.columns.append(NS(key="best_cheaper_alternative_model"))
    table.columns.append(NS(key="saving_pct"))
    table.rows[0] += ["claude-sonnet-4-5", 40.0]
    table.rows[1] += ["claude-haiku-4-5-20251001", 80.0]
    model.sections.append(NS(key="quality", tables=[NS(
        name="quality_by_setup",
        columns=[NS(key=k) for k in ("agent_type", "model", "setup_verdict", "compared_model")],
        rows=[["Explore", "claude-haiku-4-5-20251001", "worse", "claude-sonnet-4-5"]],
    )]))
    out = goals.draft("models", model, UNITS)
    assert [(c["agent"], c["value"]) for c in out["candidates"]] == [(None, "sonnet")]


def test_compaction_goal_picks_the_cheapest_window_over_the_current_one():
    out = goals.draft("compaction", _report(), UNITS, effective={})
    [candidate] = out["candidates"]
    assert candidate["value"] == 200000 and candidate["ticked"]
    assert out["whatif"]["total_usd"] == 20.0


def test_compaction_goal_never_offers_a_window_that_summarises_more_than_twice_a_session():
    model = _report()
    [table] = next(s for s in model.sections if s.key == "compaction_sim").tables
    table.rows.append(["100,000", 50.0, 3.5])  # window, cost, compactions_per_session
    [candidate] = goals.draft("compaction", model, UNITS, effective={})["candidates"]
    assert candidate["value"] == 200000


def test_compaction_goal_warns_and_leaves_the_window_unticked_while_the_env_variable_overrides_it():
    # snapshots.effective_config_in_force's shape while the variable is set.
    effective = {"autoCompactWindow": 400_000, "env.CLAUDE_CODE_AUTO_COMPACT_WINDOW": 400_000}
    [candidate] = goals.draft("compaction", _report(), UNITS, effective=effective)["candidates"]
    assert candidate["value"] == 200000 and not candidate["ticked"]
    assert candidate["evidence"].endswith(
        "Won't apply while CLAUDE_CODE_AUTO_COMPACT_WINDOW is set: it overrides this setting, so change the "
        "variable instead."
    )


def test_thinking_is_offered_unticked_and_settings_already_in_effect_are_skipped():
    out = goals.draft("thinking", _report(), UNITS)
    [candidate] = out["candidates"]
    assert (candidate["key"], candidate["value"], candidate["ticked"]) == ("effortLevel", "medium", False)
    assert goals.draft("thinking", _report(), UNITS, effective={"effortLevel": "medium"})["candidates"] == []


# -- PROF-11/F14: a thinking lever offered on a model where it does nothing --


def _with_observed_model(model_id):
    report = _report()
    [table] = next(s for s in report.sections if s.key == "model_swap").tables
    table.rows[0][table.columns.index(NS(key="observed_model"))] = model_id
    return report


def test_on_opus_5_5_and_fable_lower_effort_is_still_offered_but_the_thinking_toggles_are_ruled_out():
    # V26: thinking can't be switched off there, but effort still works
    # (V25, V13), so the effort candidate stays and says why the toggles don't.
    for model_id in ("claude-opus-5-5", "claude-fable-5-1", "claude-fable-5"):
        out = goals.draft("thinking", _with_observed_model(model_id), UNITS)
        [candidate] = out["candidates"]
        assert candidate["key"] == "effortLevel", model_id
        assert "Thinking can't be switched off on this model" in candidate["evidence"], model_id


def test_thinking_stays_offered_on_a_plain_opus_model_v26_doesnt_name():
    # V26 names Opus 5.5 specifically, not the whole Opus family -- a
    # mixed-model "(+N more)" label still matches by substring.
    out = goals.draft("thinking", _with_observed_model("claude-opus-5 (+1 more)"), UNITS)
    assert [c["key"] for c in out["candidates"]] == ["effortLevel"]


def test_thinking_is_still_offered_when_no_model_swap_data_exists():
    # Fail open: absent evidence isn't grounds to suppress the candidate.
    report = _report()
    [table] = next(s for s in report.sections if s.key == "model_swap").tables
    table.rows.clear()
    out = goals.draft("thinking", report, UNITS)
    assert [c["key"] for c in out["candidates"]] == ["effortLevel"]


def test_agents_claude_code_starts_itself_are_never_proposed():
    rec = NS(title="x", changes=[_change("model", "workflow-subagent", "haiku"), _change("omitClaudeMd", "Explore", True)])
    assert goals.draft("recommendations", _report([rec]), UNITS)["candidates"] == []


def test_current_goal_hands_over_to_save_current_settings_and_unknown_goals_raise():
    out = goals.draft("current", _report(), UNITS)
    assert out["from_current"] and out["candidates"] == []
    with pytest.raises(KeyError):
        goals.draft("everything", _report(), UNITS)


def test_an_agent_setting_already_in_effect_is_skipped_under_its_snapshot_field_name():
    """effective_agents names the cache lifetime experimental_cache_ttl and
    models by id, so the lookup must translate both before comparing."""
    rec = NS(title="x", changes=[
        _change("experimental.cacheTtl", "verification-runner", "1h"),
        _change("model", "verification-runner", "haiku"),
    ])
    agents = {"verification-runner": {"source": "project", "experimental_cache_ttl": "1h",
                                      "model": "claude-haiku-4-5-20251001"}}
    out = goals.draft("recommendations", _report([rec]), UNITS, effective_agents=agents)
    assert out["candidates"] == []


def test_models_goal_skips_a_model_the_agent_was_often_retried_from():
    model = _report()
    [table] = model.sections[0].tables
    table.columns.append(NS(key="best_cheaper_alternative_model"))
    table.columns.append(NS(key="saving_pct"))
    table.rows[0] += ["claude-sonnet-4-5", 40.0]
    table.rows[1] += ["claude-haiku-4-5-20251001", 80.0]
    model.sections.append(NS(key="quality", tables=[NS(
        name="quality_retried",
        columns=[NS(key=k) for k in ("agent_type", "model", "runs", "retried", "retried_on")],
        rows=[["Explore", "claude-haiku-4-5-20251001", 5, 1, "claude-sonnet-5"]],
    )]))
    out = goals.draft("models", model, UNITS)
    assert [(c["agent"], c["value"]) for c in out["candidates"]] == [(None, "sonnet")]


# -- a profile for one kind of task (metrics capture) ------------------------------

from test_whatif import _table  # noqa: E402

_SETUP_KEYS = ("task", "level", "model", "effort", "cycles", "avg_cost", "ok_pct", "rated", "verdict", "saving_pct")


def _with_setups(*rows):
    model = _report()
    model.sections.append(NS(key="habits", tables=[
        NS(name="habits_setups", columns=[NS(key=k) for k in _SETUP_KEYS], rows=[list(r) for r in rows]),
    ]))
    return model


_SETUPS = (
    ("chat", "all", "opus", "high", 9, 1.0, 90.0, 0, "usual", None),
    ("bugfix", "all", "opus", "high", 12, 2.0, 80.0, 3, "usual", None),
    # 20 messages -- enough to clear habits.TICK_MIN_GROUP, so the
    # effortLevel candidate below is ticked (PROF-04).
    ("bugfix", "all", "sonnet", "medium", 20, 0.5, 83.0, 1, "cheaper", 75.0),
    ("bugfix", "normal", "sonnet", "medium", 6, 0.5, 83.0, 1, "cheaper", 75.0),
)


def test_the_tasks_goal_drafts_the_cheaper_setup_for_the_first_task_that_has_one():
    out = goals.draft("tasks", _with_setups(*_SETUPS), UNITS, effective={"model": "opus", "effortLevel": "high"})
    assert out["tasks"] == ["chat", "bugfix"] and out["task"] == "bugfix"
    # Phase 10 review: the picker and the notes use each task's plain name.
    assert out["task_labels"] == {"chat": "Chat", "bugfix": "Bug fix"}
    by_key = {c["key"]: c for c in out["candidates"]}
    # The main session's model stays yours to decide; the effort is ticked.
    assert (by_key["model"]["value"], by_key["model"]["ticked"]) == ("sonnet", False)
    assert (by_key["effortLevel"]["value"], by_key["effortLevel"]["ticked"]) == ("medium", True)
    assert by_key["model"]["evidence"].startswith(
        "For bug fix work, sonnet at medium effort cost 75% less a message than your usual opus at high effort"
    )
    assert "went well 83% of the time against 80% (20 and 12 messages)" in by_key["model"]["evidence"]
    assert out["note"].startswith("Save it, then launch Claude with it when you start bug fix work.")
    assert "implementation-heavy" in out["note"]
    assert out["profile"] == {"settings": {"effortLevel": "medium"}, "agents": {}}


def test_the_catalogue_note_is_dropped_when_it_would_contradict_the_draft():
    # PROF-11/F12: research maps to discovery-scrape (effortLevel=low),
    # but this corpus's own cheaper setup for research is *high* effort
    # -- citing discovery-scrape as "a starting point" right under that
    # candidate would contradict what was just drafted.
    setups = (
        ("research", "all", "opus", "medium", 9, 1.0, 90.0, 0, "usual", None),
        ("research", "all", "opus", "high", 20, 0.5, 83.0, 1, "cheaper", 50.0),
    )
    out = goals.draft("tasks", _with_setups(*setups), UNITS, task="research")
    assert out["candidates"][0]["value"] == "high"
    assert "discovery-scrape" not in out["note"]


def test_the_catalogue_note_still_shows_when_it_agrees_with_the_draft():
    # bugfix maps to implementation-heavy, whose own effortLevel=medium
    # matches _SETUPS' cheaper setup below -- no contradiction to hide.
    out = goals.draft("tasks", _with_setups(*_SETUPS), UNITS, task="bugfix")
    assert "implementation-heavy" in out["note"]


def _with_task_compaction(*rows):
    model = _with_setups(*_SETUPS)
    section = next(s for s in model.sections if s.key == "compaction_sim")
    section.tables.append(_table("compaction_sim_by_task", [
        {"task": task, "sessions": sessions, "best_window": window, "delta_pct": delta}
        for task, sessions, window, delta in rows
    ]))
    return model


def test_the_tasks_goal_drafts_the_tasks_own_compaction_window():
    # EST-P8: the per-task split, not the corpus-wide sweep, and ticked
    # once the task has habits.TICK_MIN_GROUP sessions behind it.
    out = goals.draft("tasks", _with_task_compaction(("bugfix", 20, "200,000", -25.0)), UNITS, task="bugfix")
    [candidate] = [c for c in out["candidates"] if c["key"] == "autoCompactWindow"]
    assert (candidate["value"], candidate["ticked"]) == (200000, True)
    assert candidate["evidence"] == "Your 20 bug fix sessions replayed with summaries at 200,000 tokens cost 25% less."
    few = goals.draft("tasks", _with_task_compaction(("bugfix", 6, "200,000", -25.0)), UNITS, task="bugfix")
    assert [c["ticked"] for c in few["candidates"] if c["key"] == "autoCompactWindow"] == [False]


def test_the_tasks_goal_skips_a_task_window_that_saves_little_or_summarises_too_often():
    for row in (("bugfix", 20, "200,000", -2.0), ("bugfix", 20, "none", 0.0), ("bugfix", 20, "50,000", -40.0)):
        # 50,000 isn't in the corpus-wide sweep at all here, so there's no
        # summary count to hold it to -- not offered.
        out = goals.draft("tasks", _with_task_compaction(row), UNITS, task="bugfix")
        assert not [c for c in out["candidates"] if c["key"] == "autoCompactWindow"], row
    model = _with_task_compaction(("bugfix", 20, "50,000", -40.0))
    [by_window, _] = next(s for s in model.sections if s.key == "compaction_sim").tables
    by_window.rows.append(["50,000", 40.0, 2.5])  # window, cost, compactions_per_session
    out = goals.draft("tasks", model, UNITS, task="bugfix")
    assert not [c for c in out["candidates"] if c["key"] == "autoCompactWindow"]


def test_a_task_with_no_cheaper_setup_says_what_your_usual_one_is():
    out = goals.draft("tasks", _with_setups(*_SETUPS), UNITS, task="chat")
    assert out["task"] == "chat" and out["candidates"] == []
    assert out["note"].startswith("Your usual setup for chat work is opus at high effort. No cheaper setup")
    assert "interactive-chat" in out["note"]


def test_the_tasks_goal_without_capture_says_how_to_get_the_data():
    out = goals.draft("tasks", _report(), UNITS)
    assert out["tasks"] == [] and out["task"] is None and out["candidates"] == []
    assert "metrics capture" in out["note"]
    # Other goals carry no task.
    other = goals.draft("cache", _report(), UNITS)
    assert (other["tasks"], other["task_labels"], other["task"], other["note"]) == ([], {}, None, None)


def test_an_unknown_task_falls_back_to_the_first_with_a_cheaper_setup():
    assert goals.draft("tasks", _with_setups(*_SETUPS), UNITS, task="docs")["task"] == "bugfix"


def test_a_cheaper_effort_below_tick_min_group_is_shown_but_not_ticked():
    # habits.MIN_GROUP (5) is enough for the setups table to show and
    # name a cheaper setup at all; habits.TICK_MIN_GROUP (20) is the
    # higher bar the tasks goal itself applies before ticking the
    # effortLevel candidate for it (PROF-04).
    setups = (
        ("bugfix", "all", "opus", "high", 12, 2.0, 80.0, 3, "usual", None),
        ("bugfix", "all", "sonnet", "medium", 8, 0.5, 83.0, 1, "cheaper", 75.0),
    )
    out = goals.draft("tasks", _with_setups(*setups), UNITS, task="bugfix")
    by_key = {c["key"]: c for c in out["candidates"]}
    assert (by_key["effortLevel"]["value"], by_key["effortLevel"]["ticked"]) == ("medium", False)


# -- PROF-03: an effortLevel candidate warns when it won't apply -------------


def test_an_env_set_effort_level_warns_the_candidate_wont_apply():
    out = goals.draft(
        "tasks", _with_setups(*_SETUPS), UNITS,
        effective={"model": "opus", "effortLevel": "high"}, effort_level_env_set=True,
    )
    by_key = {c["key"]: c for c in out["candidates"]}
    assert "Won't apply to sonnet: CLAUDE_CODE_EFFORT_LEVEL is set for this session; use --effort instead." \
        in by_key["effortLevel"]["evidence"]
    # It's still drafted (a legitimate default for every other model) --
    # only the wording changes, not ticked/value.
    assert (by_key["effortLevel"]["value"], by_key["effortLevel"]["ticked"]) == ("medium", True)


def test_a_models_own_effort_override_warns_the_candidate_wont_apply():
    out = goals.draft(
        "tasks", _with_setups(*_SETUPS), UNITS,
        effective={
            "model": "opus", "effortLevel": "high",
            "modelSettings": {"claude-sonnet-5": {"effortLevel": "low"}},
        },
    )
    by_key = {c["key"]: c for c in out["candidates"]}
    assert "Won't apply to sonnet: it already has its own effort level set; use --effort instead." \
        in by_key["effortLevel"]["evidence"]


def test_an_effort_level_candidate_carries_no_warning_when_neither_applies():
    out = goals.draft("tasks", _with_setups(*_SETUPS), UNITS, effective={"model": "opus", "effortLevel": "high"})
    by_key = {c["key"]: c for c in out["candidates"]}
    assert "Won't apply" not in by_key["effortLevel"]["evidence"]


# -- the tasks goal's estimate is scaled to the task's own share ------------------

_BY_TASK_KEYS = (
    "task", "cycles", "share", "cost", "avg_cost", "main_cost", "clear_pct", "large_pct", "redo_pct", "met_pct",
)
_AGENTS_BY_TASK_KEYS = (
    "task", "agent_type", "runs", "avg_cost", "done_pct", "probe_pct", "before_edit",
    "cheaper_model", "cheaper_saving_pct",
)
_AGENTS_KEYS = (
    "agent_type", "runs", "cost", "report_tokens", "capped_pct", "done_pct", "retried", "retried_model",
    "probe_pct", "before_edit", "rules_used", "rules_unused", "easy_pct", "hard_pct",
    "overlap_reads", "nested",
)


def _row_for(keys, mapping):
    return [mapping.get(k) for k in keys]


def _with_task_data(setups=(), by_task=(), agents_by_task=(), agents=()):
    model = _report()
    tables = [NS(name="habits_setups", columns=[NS(key=k) for k in _SETUP_KEYS], rows=[list(r) for r in setups])]
    if by_task:
        tables.append(NS(name="habits_by_task", columns=[NS(key=k) for k in _BY_TASK_KEYS],
                          rows=[_row_for(_BY_TASK_KEYS, r) for r in by_task]))
    if agents_by_task:
        tables.append(NS(name="habits_agents_by_task", columns=[NS(key=k) for k in _AGENTS_BY_TASK_KEYS],
                          rows=[_row_for(_AGENTS_BY_TASK_KEYS, r) for r in agents_by_task]))
    if agents:
        tables.append(NS(name="habits_agents", columns=[NS(key=k) for k in _AGENTS_KEYS],
                          rows=[_row_for(_AGENTS_KEYS, r) for r in agents]))
    model.sections.append(NS(key="habits", tables=tables))
    return model


def test_the_tasks_goal_scales_its_estimate_to_the_task_cost_share():
    # cost (subagents included) and main_cost (main session alone) are
    # deliberately different shares here -- 75% vs 60% -- so this test
    # would catch a regression to F7 (scaling a main-only reprice by the
    # whole-window share instead of the main-only one).
    model = _with_task_data(_SETUPS, by_task=[
        {"task": "all", "cost": 200.0, "main_cost": 100.0},
        {"task": "bugfix", "cost": 150.0, "main_cost": 60.0},
    ])
    out = goals.draft("tasks", model, UNITS, effective={"model": "opus", "effortLevel": "high"})
    by_key = {c["key"]: c for c in out["candidates"]}
    # model_swap's top-level row reprices the WHOLE window (40.0 raw saving); bugfix is 60% of the main session's own cost.
    assert by_key["model"]["estimate"]["saving_usd"] == pytest.approx(24.0)
    assert by_key["model"]["estimate"]["effect_text"] == "Saves 24.00 USD"
    assert "60%" in by_key["model"]["estimate"]["basis"]
    # PROF-01: the combined "whatif" total (what /api/whatif?task=... also
    # returns for this same draft) is the sum of its own scaled rows, not
    # a separate repricing of the whole window.
    scaled_rows = out["whatif"]["rows"]
    assert out["whatif"]["total_usd"] == pytest.approx(sum(r["saving_usd"] for r in scaled_rows if r["saving_usd"] is not None))


def test_without_a_by_task_cost_the_estimate_is_left_unscaled_and_explained():
    out = goals.draft("tasks", _with_setups(*_SETUPS), UNITS, effective={"model": "opus", "effortLevel": "high"})
    estimate = next(c for c in out["candidates"] if c["key"] == "model")["estimate"]
    assert estimate["saving_usd"] is None and estimate["effect_text"] == "Not estimated"
    assert "no per-task cost" in estimate["basis"]


def test_a_profile_covering_several_tasks_scales_by_their_combined_share():
    # F11: a catalogue profile's `for` covers several tasks (implementation-
    # heavy: feature, bugfix, debug, ...); /api/whatif scales by their
    # shares added up. debug has no row of its own, so it adds nothing.
    from claudeglass import whatif

    model = _with_task_data(by_task=[
        {"task": "all", "cost": 200.0, "main_cost": 100.0},
        {"task": "bugfix", "cost": 150.0, "main_cost": 60.0},
        {"task": "feature", "cost": 30.0, "main_cost": 20.0},
    ])
    result = {"rows": [{"key": "model", "agent": None, "saving_usd": 40.0, "uncalibrated_usd": None,
                        "fidelity": "ceiling", "effect_text": "", "basis": "Repriced."}]}
    out = goals._scale_whatif(result, whatif._Tables(model), ("feature", "bugfix", "debug"), UNITS, "")
    [row] = out["rows"]
    assert row["saving_usd"] == pytest.approx(32.0)
    assert "these tasks' 80% share" in row["basis"]
    assert out["total_usd"] == pytest.approx(32.0)
    # One task keeps its own wording.
    [one] = goals._scale_whatif(result, whatif._Tables(model), "bugfix", UNITS, "")["rows"]
    assert one["saving_usd"] == pytest.approx(24.0) and "this task's 60% share" in one["basis"]


def test_the_tasks_goal_drafts_a_cheaper_model_for_the_agent_that_ran_the_task_most():
    model = _with_task_data(
        _SETUPS,
        agents_by_task=[{"task": "bugfix", "agent_type": "Explore", "runs": 8, "avg_cost": 1.0,
                          "cheaper_model": "haiku", "cheaper_saving_pct": 25.0}],
        agents=[{"agent_type": "Explore", "cost": 10.0}],
    )
    out = goals.draft("tasks", model, UNITS, task="bugfix")
    explore = next(c for c in out["candidates"] if c["agent"] == "Explore")
    assert (explore["key"], explore["value"], explore["ticked"]) == ("model", "haiku", True)
    # F8: the saving percentage is Explore's corpus-wide verdict (every task it ran), not bugfix-only -- said plainly.
    assert explore["evidence"] == "Explore runs 25% cheaper on haiku across every task it did; bug fix work was 8 of its runs in this window."
    # model_swap's Explore row reprices ALL of its work (raw saving 8.0); bugfix is 80% of its cost (8 of 10).
    assert explore["estimate"]["saving_usd"] == pytest.approx(6.4)


def test_agent_candidates_for_a_task_are_vetoed_by_unfit_agents():
    model = _with_task_data(
        _SETUPS,
        agents_by_task=[{"task": "bugfix", "agent_type": "Explore", "runs": 8, "avg_cost": 1.0,
                          "cheaper_model": "haiku", "cheaper_saving_pct": 25.0}],
        agents=[{"agent_type": "Explore", "runs": 8, "cost": 10.0, "hard_pct": 75.0, "retried_model": 0}],
    )
    out = goals.draft("tasks", model, UNITS, task="bugfix")
    assert not [c for c in out["candidates"] if c["agent"] == "Explore"]


def test_agent_candidates_for_a_task_are_vetoed_by_the_quality_check_too():
    model = _with_task_data(
        _SETUPS,
        agents_by_task=[{"task": "bugfix", "agent_type": "reviewer", "runs": 8, "avg_cost": 1.0,
                          "cheaper_model": "sonnet", "cheaper_saving_pct": 30.0}],
        agents=[{"agent_type": "reviewer", "cost": 10.0}],
    )
    model.sections.append(NS(key="quality", tables=[NS(
        name="quality_by_setup",
        columns=[NS(key=k) for k in ("agent_type", "model", "setup_verdict", "compared_model")],
        rows=[["reviewer", "claude-sonnet-4-5", "worse", "claude-opus-5-5"]],
    )]))
    out = goals.draft("tasks", model, UNITS, task="bugfix")
    assert not [c for c in out["candidates"] if c["agent"] == "reviewer"]


def _with_agent_reports(used, unused, *, claude_md=4000, claude_md_managed=0):
    model = _report()
    model.sections = [s for s in model.sections if s.key != "agent_startup"]
    model.sections.append(NS(key="agent_startup", tables=[_table("agent_startup_breakdown", [
        {"agent_type": "reviewer", "claude_md": claude_md, "claude_md_managed": claude_md_managed,
         "spawns": 20, "write_price": 3.75},
    ])]))
    model.sections.append(NS(key="habits", tables=[_table("habits_agents", [
        {"agent_type": "reviewer", "runs": used + unused, "rules_used": used, "rules_unused": unused},
    ])]))
    return model


def test_agents_that_said_they_did_not_use_claude_md_get_it_left_out_ticked():
    [candidate] = [c for c in goals.draft("subagents", _with_agent_reports(1, 3), UNITS)["candidates"]
                   if c["key"] == "omitClaudeMd"]
    assert candidate["agent"] == "reviewer" and candidate["ticked"]
    assert candidate["evidence"].endswith("3 of the 4 runs that said, said they didn't use it.")


def test_agents_that_said_they_used_claude_md_keep_it():
    out = goals.draft("subagents", _with_agent_reports(3, 1), UNITS)
    assert not [c for c in out["candidates"] if c["key"] == "omitClaudeMd"]


def test_without_reports_claude_md_is_offered_unticked():
    [candidate] = [c for c in goals.draft("subagents", _with_agent_reports(0, 0), UNITS)["candidates"]
                   if c["key"] == "omitClaudeMd"]
    assert not candidate["ticked"] and "Not ticked" in candidate["evidence"]


def test_managed_claude_md_is_excluded_and_cited_separately():
    # PROF-11/F13: Managed policy CLAUDE.md still loads regardless of
    # omitClaudeMd, so it never counts towards the saving evidence.
    [candidate] = [c for c in goals.draft("subagents", _with_agent_reports(1, 3, claude_md_managed=1500), UNITS)["candidates"]
                   if c["key"] == "omitClaudeMd"]
    assert "About 2,500 CLAUDE.md tokens at each of 20 spawns." in candidate["evidence"]
    assert "Managed policy CLAUDE.md (1,500 tokens) still loads either way." in candidate["evidence"]


def test_omit_claude_md_not_offered_when_only_managed_claude_md_is_seen():
    out = goals.draft("subagents", _with_agent_reports(1, 3, claude_md=1500, claude_md_managed=1500), UNITS)
    assert not [c for c in out["candidates"] if c["key"] == "omitClaudeMd"]

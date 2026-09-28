"""Quick actions: every check answers, and its fixes keep the fix
contract (``quick_actions``)."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from claudeglass import capture_catalogue, discovery, quality
from claudeglass import quick_actions as qa
from claudeglass.fixes import PROMPT_RESTART
from claudeglass.model import Recommendation
from claudeglass.units import Units

from test_whatif import _model, _table

API_MD = Path(__file__).resolve().parent.parent / "docs" / "api.md"
SRC = Path(__file__).resolve().parent.parent / "src" / "claudeglass"
#: Every module that can build a ``Recommendation`` -- see recommend.py's
#: own ``recommend()`` entry point, which folds each of these in.
_RULE_MODULES = ("recommend.py", "advice.py", "carry.py", "compaction_sim.py", "handoff.py", "hook_costs.py",
                  "run_split.py", "model_swap.py", "waste.py", "elasticity.py")

UNITS = Units(billing_mode="api", currency="USD")
FIX_KEYS = {"key", "agent", "explainer", "command", "command_warning", "prompt", "title"}


def _ctx(tmp_path, model=None, **kw):
    config_dir = tmp_path / ".claude" / "claudeglass"
    config_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / ".claude" / "projects").mkdir(exist_ok=True)
    return qa.Context(
        model=model if model is not None else _full_model(),
        units=UNITS,
        period="over the last 14 days",
        config_dir=config_dir,
        effective=kw.get("effective", {}),
        effective_agents=kw.get("effective_agents", {}),
    )


def _full_model():
    model = _model()
    swap = model.sections[0].tables[0]
    swap.columns += [NS(key="observed_model"), NS(key="best_cheaper_alternative_model"), NS(key="saving_usd"),
                     NS(key="saving_pct")]
    swap.rows[0] += ["claude-opus-4-7", "claude-sonnet-4-5", 40.0, 40.0]
    swap.rows[1] += ["claude-sonnet-4-5", "claude-haiku-4-5-20251001", 8.0, 80.0]
    model.sections[4].tables[0].columns.append(NS(key="output_tokens"))
    model.sections[4].tables[0].rows[0].append(1000)
    model.sections.append(NS(key="carry", tables=[_table("carry_by_tool", [
        {"key": "Bash", "result_count": 10, "tokens_entered": 5000, "mean_turns_carried": 30, "carry_cost_usd": 8.0},
        {"key": "Read", "result_count": 10, "tokens_entered": 5000, "mean_turns_carried": 30, "carry_cost_usd": 2.0},
    ])]))
    model.sections.append(NS(key="waste", tables=[_table("waste_by_cause", [
        {"cause": "tool-error", "turns": 3, "cost_usd": 1.0, "lever": "Check paths first."},
    ])]))
    model.recommendations = [
        Recommendation(id="long-tool-waits", title="Tools often wait on you", action="Pre-approve routine tools."),
    ]
    return model


def test_every_check_answers_with_a_valid_status_and_fix_contract(tmp_path):
    ctx = _ctx(tmp_path)
    for check_id in qa.CHECK_IDS:
        result = qa.run(check_id, ctx)
        assert result["status"] in ("act", "ok", "no_data"), check_id
        assert result["summary"], check_id
        for fix in result["fixes"]:
            assert FIX_KEYS <= set(fix), (check_id, fix)
            assert fix["prompt"] and fix["title"], check_id


def test_check_ids_documented_in_api_md_match_the_code():
    """docs/api.md's ``GET /api/quick-actions`` summary once dropped
    "quality" (added after "habits") from its prose id list. Regression
    test: the sentence must name exactly ``quick_actions.CHECK_IDS``, in
    order."""
    text = API_MD.read_text(encoding="utf-8")
    match = re.search(
        r"One answer per way of saving tokens \(`quick_actions\.CHECKS`\):(.+?)\.\s*Each check always answers",
        text,
        re.S,
    )
    assert match, "docs/api.md's quick-actions summary sentence has changed shape"
    ids_text = " ".join(match.group(1).split())
    ids = [part.strip() for part in re.split(r",| and ", ids_text) if part.strip()]
    assert ids == list(qa.CHECK_IDS)


def _all_recommendation_ids() -> set[str]:
    """Every literal ``id="..."`` a ``Recommendation(...)`` call passes,
    across every module ``recommend.recommend()`` folds in -- the real
    id space a quick-action's ``rule_ids`` can point into."""
    ids: set[str] = set()
    for name in _RULE_MODULES:
        tree = ast.parse((SRC / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Recommendation":
                for kw in node.keywords:
                    if kw.arg == "id" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                        ids.add(kw.value.value)
    return ids


def test_every_checks_rule_id_is_a_real_recommendation_id():
    """Additive ``Check.rule_ids`` (Task Group B, item 6): each id it
    names must be one some rule module can actually produce, so a
    dashboard link from a check to `/api/recommendations?id=...` never
    points at nothing."""
    known = _all_recommendation_ids()
    assert known, "the scan found no recommendation ids"
    for check in qa.CHECKS:
        for rule_id in check.rule_ids:
            assert rule_id in known, (check.id, rule_id)


def test_an_empty_report_is_no_data_everywhere_but_never_fails(tmp_path):
    ctx = _ctx(tmp_path, model=NS(sections=[], context_files={}, recommendations=[]))
    statuses = {row["id"]: row["status"] for row in qa.run_all(ctx)}
    assert set(statuses) == set(qa.CHECK_IDS)
    assert statuses["models"] == statuses["cache"] == statuses["tool-output"] == statuses["quality"] == "no_data"


def test_files_on_disk_without_transcript_records_are_no_data_not_ok(tmp_path):
    """A skill or CLAUDE.md file on disk that no session in the window
    recorded says "not enough data", never "nothing to do"."""
    ctx = _ctx(tmp_path, model=NS(sections=[], context_files={}, recommendations=[]))
    claude_root = ctx.config_dir.parent
    (claude_root / "CLAUDE.md").write_text("# Rules\n\nBe brief.\n", encoding="utf-8")
    skill = claude_root / "skills" / "tidy"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: tidy\ndescription: Tidy things.\n---\n", encoding="utf-8")
    for check_id in ("skills", "claude-md"):
        result = qa.run(check_id, ctx)
        assert result["status"] == "no_data", (check_id, result["summary"])
        assert "over the last 14 days" in result["summary"]


def test_skills_check_keeps_a_skill_a_claude_code_tool_loads_out_of_the_hide_list(tmp_path):
    def usage(name):
        return {"name": name, "listing_tokens": 40, "listed": {"main": 5}, "listing_cost_usd": 0.5}

    skills = [usage("artifact-capabilities"), usage("keybindings-help"), usage("loop")]
    ctx = _ctx(tmp_path, model=NS(sections=[], context_files={"skills": skills}, recommendations=[]))
    result = qa.run("skills", ctx)
    assert result["status"] == "act"
    assert [row[0] for row in result["table"]["rows"]] == ["keybindings-help", "loop"]
    hide_all, *rest = result["fixes"]
    assert "artifact-capabilities" not in hide_all["command"]
    assert rest[-1]["title"] == "artifact-capabilities: list it by name only"
    [tip] = result["tips"]
    assert tip["title"] == "artifact-capabilities: needed by the Artifact tool"


def test_skills_check_of_one_project_hides_skills_there_only(tmp_path):
    skills = [{"name": name, "listing_tokens": 40, "listed": {"main": 5}, "listing_cost_usd": 0.5}
              for name in ("keybindings-help", "loop")]
    ctx = _ctx(tmp_path, model=NS(sections=[], context_files={"skills": skills}, recommendations=[]))
    ctx.only = (tmp_path,)
    result = qa.run("skills", ctx)
    assert "in that project only (.claude/settings.local.json)" in result["summary"]
    assert all("--scope project-local --project-dir ." in fix["command"] for fix in result["fixes"])
    assert not any("--scope user" in fix["command"] for fix in result["fixes"])


def test_models_check_offers_a_fix_per_cheaper_model_with_a_dry_run_command(tmp_path):
    result = qa.run("models", _ctx(tmp_path, effective_agents={"Explore": {}}))
    assert result["status"] == "act"
    titles = [fix["title"] for fix in result["fixes"]]
    assert titles == ["Main session: use sonnet", "Explore: use haiku"]
    explore = result["fixes"][1]
    assert explore["command"].endswith("--dry-run") and "--agent Explore" in explore["command"]
    assert any("Saves 8.00 USD" in text for _h, text in explore["explainer"])
    assert result["table"]["rows"][0][0] == "Main session"


def test_models_check_says_where_each_agents_model_is_set(tmp_path):
    """A workflow script or a model named at spawn sets some runs' model:
    the table says so per agent, and one tip points those runs to where
    their model is really set."""
    model = _full_model()
    swap = model.sections[0].tables[0]
    swap.columns += [NS(key="lever_runs"), NS(key="workflow_runs"), NS(key="spawn_model_runs")]
    swap.rows[0] += [40, 0, 0]
    swap.rows[1] += [5, 12, 3]
    result = qa.run("models", _ctx(tmp_path, model=model, effective_agents={"Explore": {}}))
    keys = [c["key"] for c in result["table"]["columns"]]
    set_by = {row[0]: row[keys.index("set_by")] for row in result["table"]["rows"]}
    assert set_by == {"Main session": "settings", "Explore": "its agent file (5), workflow scripts (12), when started (3)"}
    (tip,) = [t for t in result["tips"] if t["title"] == "Some runs' model isn't set by an agent file"]
    assert "12 runs a workflow script started" in tip["text"] and "3 runs given a model" in tip["text"]


def test_tool_output_offers_the_bash_cap_as_a_prompt_only(tmp_path):
    result = qa.run("tool-output", _ctx(tmp_path))
    [fix] = result["fixes"]
    assert fix["key"] == "BASH_MAX_OUTPUT_LENGTH" and fix["command"] is None
    assert '"env"' in fix["prompt"] and "diff" in fix["prompt"]


def _cap_row(usd_saved, carry_cost_usd):
    return {"setting": "BASH_MAX_OUTPUT_LENGTH", "value": "15000", "cap_tokens": 3750, "results": 10,
            "results_affected": 2, "tokens_saved": 5000, "usd_saved": usd_saved, "carry_cost_usd": carry_cost_usd}


def test_tool_output_prices_the_bash_cap_from_its_own_saving(tmp_path):
    model = _full_model()
    carry = next(s for s in model.sections if s.key == "carry")
    carry.tables.append(_table("carry_output_cap_savings", [_cap_row(2.0, 8.0)]))
    [fix] = qa.run("tool-output", _ctx(tmp_path, model=model))["fixes"]
    effect = dict(fix["explainer"])["Expected effect"]
    assert "would have cut 2 of 10 results" in effect and "saved up to 2.00 USD" in effect


def test_tool_output_explains_a_bash_cap_that_would_save_little(tmp_path):
    model = _full_model()
    carry = next(s for s in model.sections if s.key == "carry")
    carry.tables.append(_table("carry_output_cap_savings", [_cap_row(0.3, 8.0)]))
    result = qa.run("tool-output", _ctx(tmp_path, model=model))
    assert result["fixes"] == []
    tip = next(t for t in result["tips"] if t["title"].startswith("BASH_MAX_OUTPUT_LENGTH"))
    assert "0.30 USD" in tip["text"] and "8.00 USD" in tip["text"]


def test_compaction_says_the_summary_point_is_already_set_when_it_is(tmp_path):
    """The last column is against the sessions as they ran, so the row
    for the window already set can read cheaper than 0%."""
    model = NS(sections=[NS(key="compaction_sim", tables=[_table("compaction_sim_by_window", [
        {"window": 300000, "compactions_per_session": 2.0, "cost": 75.0, "delta_pct": -25.0},
        {"window": "none", "compactions_per_session": 1.0, "cost": 100.0, "delta_pct": 0.0},
    ])])], context_files={}, recommendations=[])
    result = qa.run("compaction", _ctx(tmp_path, model=model, effective={"autoCompactWindow": 300000}))
    assert result["status"] == "ok" and result["fixes"] == []
    assert result["summary"].startswith("You already summarise at 300,000 tokens")
    assert "at most 2 times a session" in result["summary"]
    # The replay keeps real summaries, so it can't say raising helps.
    assert "A larger window can't be tested" in result["summary"]
    assert qa.run("compaction", _ctx(tmp_path, model=model))["status"] == "act"


def test_compaction_says_no_window_is_suggested_when_real_summaries_pass_the_limit(tmp_path):
    """Real summaries stay in every replayed window, so 6 a session as
    they ran means no point can meet the 2-a-session limit: not "within a
    few percent of the cheapest point that summarises at most 2 times"."""
    model = NS(sections=[NS(key="compaction_sim", tables=[_table("compaction_sim_by_window", [
        {"window": "200,000", "compactions_per_session": 18.1, "cost": 104.9, "delta_pct": 4.9},
        {"window": "300,000", "compactions_per_session": 6.4, "cost": 100.2, "delta_pct": 0.2},
        {"window": "none", "compactions_per_session": 6.25, "cost": 100.0, "delta_pct": 0.0},
    ])])], context_files={}, recommendations=[])
    result = qa.run("compaction", _ctx(tmp_path, model=model, effective={"autoCompactWindow": 300000}))
    assert result["status"] == "ok" and result["fixes"] == []
    assert result["summary"].startswith("Your sessions summarised about 6.2 times each as they ran, more than the 2")
    assert "A larger window can't be tested" in result["summary"]


def test_habits_are_tips_not_settings(tmp_path):
    result = qa.run("habits", _ctx(tmp_path))
    assert result["status"] == "act"
    assert result["tips"] == [{"title": "Tools often wait on you", "text": "Pre-approve routine tools."}]


def test_markdown_carries_the_table_tips_and_fixes(tmp_path):
    text = qa.render_markdown(qa.run("models", _ctx(tmp_path)))
    assert text.startswith("## Is each agent on the cheapest model")
    assert "| Agent |" in text and "### Explore: use haiku" in text and "```bash" in text


def test_unknown_check_raises(tmp_path):
    with pytest.raises(KeyError):
        qa.run("nope", _ctx(tmp_path))


def _quality_model(agents: list[dict], setups: list[dict] | None = None, failing: list[dict] | None = None,
                   retried: list[dict] | None = None, reasons: list[dict] | None = None,
                   markers: list[dict] | None = None):
    return NS(sections=[NS(key="quality", tables=[
        _table("quality_by_agent", agents),
        _table("quality_by_setup", setups or []),
        _table("quality_retried", retried or []),
        _table("quality_retry_reasons", reasons or []),
        _table("quality_failing_tools", failing or []),
        _table("quality_markers", markers or []),
    ])], recommendations=[], context_files={})


_AGENT = {"agent_type": "claude-implementer", "runs": 40, "unfinished_pct": 30.0, "turn_limit_pct": 20.0,
          "tool_errors_pct": 2.0, "shell_errors_pct": 3.0, "corrections_pct": None, "max_tokens_pct": 0.0}
_WORSE = {"agent_type": "claude-implementer", "model": "claude-haiku-4-5-20251001", "effort": "high", "runs": 20,
          "setup_verdict": "worse",
          "difference": "Lower: replies per run 14.5 against 91.4; Worse: tool calls that failed 8.4% against 2.2%.",
          "compared_model": "claude-sonnet-5", "compared_effort": "high"}
_MIXED = {**_WORSE, "setup_verdict": "mixed",
          "difference": "Better: agent runs that didn't finish 0% against 14%; Worse: tool calls that failed 8.4% against 2.2%."}


def test_quality_is_ok_when_nothing_stands_out(tmp_path):
    calm = {**_AGENT, "unfinished_pct": 3.0, "turn_limit_pct": 0.0}
    result = qa.run("quality", _ctx(tmp_path, model=_quality_model([calm])))
    assert result["status"] == "ok" and not result["fixes"]


def test_a_setup_that_is_not_comparable_is_not_counted_as_compared(tmp_path):
    calm = {**_AGENT, "unfinished_pct": 3.0, "turn_limit_pct": 0.0}
    far = {**_WORSE, "setup_verdict": "not_comparable",
           "difference": "Not compared: its runs averaged 495.0 replies against 2.2, more than 5 times apart."}
    near = {**_WORSE, "effort": "medium", "setup_verdict": "no_clear_difference", "difference": "No clear difference."}
    result = qa.run("quality", _ctx(tmp_path, model=_quality_model([calm], [far, near])))
    assert result["status"] == "ok" and not result["fixes"]
    assert result["summary"].endswith("(1 setups compared).")


def test_quality_flags_an_agent_that_runs_out_of_turns_with_a_tip(tmp_path):
    result = qa.run("quality", _ctx(tmp_path, model=_quality_model([_AGENT])))
    assert result["status"] == "act"
    assert result["table"]["rows"][0][-1] == "30% of runs didn't finish"
    assert result["summary"] == "1 agent often fails or doesn't finish."
    tip = result["tips"][0]
    assert tip["title"] == "claude-implementer: runs often don't finish"
    assert tip["text"].startswith("20% of its runs most likely ran out of turns")


def test_quality_ignores_agents_with_too_few_runs(tmp_path):
    result = qa.run("quality", _ctx(tmp_path, model=_quality_model([{**_AGENT, "runs": 3}])))
    assert result["status"] == "ok"


def test_a_worse_setup_offers_the_model_it_was_compared_with(tmp_path):
    calm = {**_AGENT, "unfinished_pct": 3.0}
    ctx = _ctx(tmp_path, model=_quality_model([calm], [_WORSE]),
               effective_agents={"claude-implementer": {"model": "haiku"}})
    result = qa.run("quality", ctx)
    assert result["status"] == "act"
    assert result["table"]["rows"][-1][-1] == (
        "On claude-haiku-4-5-20251001, effort high: Worse: tool calls that failed 8.4% against 2.2%."
    )
    fix = result["fixes"][0]
    assert FIX_KEYS <= set(fix)
    assert (fix["key"], fix["agent"], fix["title"]) == ("model", "claude-implementer", "claude-implementer: back to sonnet")
    assert "--dry-run" in fix["command"]
    # Going back up a tier costs more; it doesn't risk more replies.
    tradeoff = dict(fix["explainer"])["Trade-off"]
    assert tradeoff.startswith("A larger model costs more per token")


def test_a_mixed_setup_is_a_tip_not_a_switch(tmp_path):
    ctx = _ctx(tmp_path, model=_quality_model([{**_AGENT, "unfinished_pct": 3.0}], [_MIXED]),
               effective_agents={"claude-implementer": {"model": "haiku"}})
    result = qa.run("quality", ctx)
    assert result["status"] == "ok" and not result["fixes"]
    [tip] = result["tips"]
    assert tip["title"] == "claude-implementer: mixed results on claude-haiku-4-5-20251001, effort high"
    assert "nothing to switch" in tip["text"]


def test_models_check_does_not_suggest_a_model_the_agent_did_worse_on(tmp_path):
    model = _full_model()
    model.sections.append(NS(key="quality", tables=[
        _table("quality_by_setup", [{**_WORSE, "agent_type": "Explore"}]),
    ]))
    result = qa.run("models", _ctx(tmp_path, model=model))
    assert [fix["agent"] for fix in result["fixes"]] == [None]
    [tip] = result["tips"]
    assert tip["title"] == "Explore: haiku not suggested"
    assert "it did worse than on claude-sonnet-5" in tip["text"]


def test_models_check_does_not_suggest_a_model_with_mixed_results(tmp_path):
    """Worse on some signals and better on others is no reason to
    switch: runs on the cheaper model that didn't finish aren't made up
    for by fewer failed tool calls."""
    model = _full_model()
    model.sections.append(NS(key="quality", tables=[
        _table("quality_by_setup", [{**_MIXED, "agent_type": "Explore"}]),
    ]))
    result = qa.run("models", _ctx(tmp_path, model=model))
    assert [fix["agent"] for fix in result["fixes"]] == [None]
    [tip] = result["tips"]
    assert tip["title"] == "Explore: haiku not suggested"
    assert "it did worse on some signals than on claude-sonnet-5" in tip["text"]


def test_a_project_agents_fix_edits_the_projects_agent_file(tmp_path):
    ctx = _ctx(tmp_path, model=_quality_model([{**_AGENT, "unfinished_pct": 3.0}], [_WORSE]),
               effective_agents={"claude-implementer": {"source": "project", "model": "haiku"}})
    fix = qa.run("quality", ctx)["fixes"][0]
    assert "--scope repo --project-dir ." in fix["command"]
    assert "In .claude/agents/claude-implementer.md" in fix["prompt"]


def test_a_worse_setup_no_longer_in_use_is_a_tip_not_a_fix(tmp_path):
    ctx = _ctx(tmp_path, model=_quality_model([{**_AGENT, "unfinished_pct": 3.0}], [_WORSE]),
               effective_agents={"claude-implementer": {"model": "sonnet"}})
    result = qa.run("quality", ctx)
    assert not result["fixes"]
    assert "no longer uses that setup" in result["tips"][0]["text"]


def test_the_main_session_doing_worse_is_a_tip(tmp_path):
    main = {**_WORSE, "agent_type": "(main session)"}
    result = qa.run("quality", _ctx(tmp_path, model=_quality_model([{**_AGENT, "unfinished_pct": 3.0}], [main])))
    assert not result["fixes"] and result["tips"][0]["title"] == "Main session did worse on claude-haiku-4-5-20251001"


_RETRIED = {"agent_type": "claude-implementer", "model": "claude-haiku-4-5-20251001", "runs": 31, "retried": 4,
            "retried_pct": 12.9, "files_edited_again": 10, "files_edited": 19, "retried_on": "claude-sonnet-5",
            "last_retried": "2026-09-23"}


def test_runs_retried_on_a_larger_model_offer_the_move_back_when_the_agent_file_is_on_the_cheaper_one(tmp_path):
    ctx = _ctx(tmp_path, model=_quality_model([{**_AGENT, "unfinished_pct": 3.0}], retried=[_RETRIED]),
               effective_agents={"claude-implementer": {"model": "haiku"}})
    result = qa.run("quality", ctx)
    assert result["status"] == "act"
    assert result["summary"].startswith("1 agent was often run again on a larger model after a cheaper one.")
    [fix] = result["fixes"]
    assert (fix["key"], fix["agent"], fix["title"]) == ("model", "claude-implementer", "claude-implementer: back to sonnet")
    assert result["table"]["rows"][-1][-1] == (
        "On claude-haiku-4-5-20251001: 4 of its 31 runs on haiku that edited files were run again on sonnet, which "
        "edited the same files soon after"
    )


def test_one_retry_is_a_tip_until_it_happens_again(tmp_path):
    ctx = _ctx(tmp_path, model=_quality_model([{**_AGENT, "unfinished_pct": 3.0}],
                                              retried=[{**_RETRIED, "runs": 3, "retried": 1}]),
               effective_agents={"claude-implementer": {"model": "haiku"}})
    result = qa.run("quality", ctx)
    assert not result["fixes"]
    [tip] = result["tips"]
    assert tip["title"] == "claude-implementer: runs on haiku were retried on a larger model"
    assert tip["text"].endswith("One more retry and this check will offer to move it back to sonnet.")


def test_retries_started_on_a_model_the_agent_file_does_not_name_are_a_tip_about_the_dispatcher(tmp_path):
    ctx = _ctx(tmp_path, model=_quality_model([{**_AGENT, "unfinished_pct": 3.0}], retried=[_RETRIED]),
               effective_agents={"claude-implementer": {"model": "sonnet"}})
    result = qa.run("quality", ctx)
    assert not result["fixes"]
    [tip] = result["tips"]
    assert "Its agent file now says sonnet" in tip["text"] and "Don't pick haiku" in tip["text"]


def test_models_check_does_not_suggest_a_model_the_agent_was_often_retried_from(tmp_path):
    model = _full_model()
    model.sections.append(NS(key="quality", tables=[
        _table("quality_by_setup", []),
        _table("quality_retried", [{**_RETRIED, "agent_type": "Explore"}]),
    ]))
    result = qa.run("models", _ctx(tmp_path, model=model))
    assert [fix["agent"] for fix in result["fixes"]] == [None]
    [tip] = result["tips"]
    assert tip["title"] == "Explore: haiku not suggested"
    assert "were run again on sonnet" in tip["text"]


_NO_MARKERS = [{"marker": "[retry: ...]", "runs": 0, "of_runs": 40}, {"marker": "[result: ...]", "runs": 0, "of_runs": 40}]


def _capture_table(level: str) -> NS:
    return NS(key="capture", tables=[_table("capture_usage", [{"metric": "level", "value": level}])])


def _claude_md(text: str) -> None:
    root = discovery.claude_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / "CLAUDE.md").write_text(text, encoding="utf-8")


def test_quality_offers_metrics_capture_when_agents_ran_and_none_wrote_a_marker(tmp_path):
    calm = {**_AGENT, "unfinished_pct": 3.0, "turn_limit_pct": 0.0}
    result = qa.run("quality", _ctx(tmp_path, model=_quality_model([calm], markers=_NO_MARKERS)))
    assert result["status"] == "ok"
    [fix] = result["fixes"]
    assert set(fix) == FIX_KEYS and fix["key"] is None
    assert fix["title"] == "Turn on metrics capture"
    assert fix["command"] == "claudeglass capture on --level essentials --dry-run"
    assert "--dry-run" in fix["prompt"] and "Don't run it without --dry-run" in fix["prompt"]
    explainer = dict(fix["explainer"])
    metrics = capture_catalogue.level_metrics("essentials")
    assert f"about {round(len(capture_catalogue.note_text(metrics, 'main')) / 4)} tokens" in explainer["What it costs"]
    assert "capture off" in explainer["How to undo it"]


@pytest.mark.parametrize("already", ["written", "no_agents", "capture_on"])
def test_quality_does_not_offer_metrics_capture_again(tmp_path, already):
    markers = [{"marker": "[result: ...]", "runs": 3 if already == "written" else 0,
                "of_runs": 0 if already == "no_agents" else 40}]
    model = _quality_model([{**_AGENT, "unfinished_pct": 3.0, "turn_limit_pct": 0.0}], markers=markers)
    if already == "capture_on":
        model.sections.append(_capture_table("Essentials"))
    ctx = _ctx(tmp_path, model=model)
    assert qa.run("quality", ctx)["fixes"] == []


@pytest.mark.parametrize("capture_on", [True, False])
def test_quality_offers_to_remove_the_older_markers_section_whether_capture_is_on_or_not(tmp_path, capture_on):
    # It asks every subagent to tag its report, which broke answers that had
    # to be exact: it goes whatever capture is doing.
    model = _quality_model([{**_AGENT, "unfinished_pct": 3.0, "turn_limit_pct": 0.0}], markers=_NO_MARKERS)
    if capture_on:
        model.sections.append(_capture_table("Standard"))
    _claude_md(f"# Rules\n\n{quality.MARKER_LINES}\n")
    [fix] = qa.run("quality", _ctx(tmp_path, model=model))["fixes"]
    assert fix["title"] == "Remove the older markers section from CLAUDE.md"
    assert "JSON only" in dict(fix["explainer"])["Why"]
    assert set(fix) == FIX_KEYS and fix["command"] is None
    assert quality.MARKER_HEADING in fix["prompt"] and "Show me the diff before saving" in fix["prompt"]
    assert fix["prompt"].endswith(PROMPT_RESTART)
    explainer = dict(fix["explainer"])
    assert f"About {round(len(quality.MARKER_LINES) / 4)} tokens" in explainer["What it saves"]
    assert quality.MARKER_LINES in explainer["How to undo it"]


def test_retries_that_blame_the_brief_or_tools_are_tips(tmp_path):
    reasons = [{"agent_type": "claude-implementer", "model": "claude-haiku-4-5-20251001", "retries": 5,
                "said_model": 1, "said_brief": 3, "said_tools": 2, "said_other": 0, "last_retried": "2026-09-23"}]
    ctx = _ctx(tmp_path, model=_quality_model([{**_AGENT, "unfinished_pct": 3.0, "turn_limit_pct": 0.0}],
                                              reasons=reasons))
    tips = {tip["title"]: tip["text"] for tip in qa.run("quality", ctx)["tips"]}
    assert tips["claude-implementer: retried because the brief was unclear"].startswith(
        "3 of its retries said the instructions it was given were the problem, not haiku.")
    assert tips["claude-implementer: retried because it lacked a tool or permission"].startswith("2 of its retries")



# -- Work habits evidence in the checks -----------------------------------------


def _habits_tables(**tables) -> NS:
    return NS(key="habits", tables=[_table(name, rows) for name, rows in tables.items()])


_PLAYBOOK = [
    {"habit": key, "saving": saving, "evidence": f"{key} evidence.", "example": f"{key} example.", "source": "inferred"}
    for key, saving in (("tool_loops", 2.0), ("short_reports", 1.0), ("name_files", 0.5), ("quiet_output", 0.25))
]


def test_habits_shows_the_top_of_the_playbook_as_tips(tmp_path):
    model = _full_model()
    model.recommendations = []
    model.sections.append(_habits_tables(habits_playbook=_PLAYBOOK))
    result = qa.run("habits", _ctx(tmp_path, model=model))
    assert result["status"] == "act" and "{{page:habits}} has the rest." in result["summary"]
    tips = result["tips"][-qa.PLAYBOOK_TIPS:]
    assert [t["title"] for t in tips] == [
        "Stop retrying a failing command", "Ask agents for short reports", "Name the files you already know",
    ]
    assert tips[0]["text"] == (
        f"tool_loops evidence. Try: tool_loops example. About {qa._money(_ctx(tmp_path), 2.0)} a week (inferred)."
    )


def test_playbook_tips_skip_a_habit_already_covered_by_a_fired_recommendation(tmp_path):
    """UX-3, "quick actions deduped by theme": a habit ``apply_covered_by``
    (habits.py) has already matched to a fired rule is skipped here too --
    it's already said, via ``_HABIT_RECS`` or the Recommendations section,
    so a tip repeating it would say the same thing twice."""
    model = _full_model()
    model.recommendations = []
    rows = [dict(row, covered_by="") for row in _PLAYBOOK]
    rows[0]["covered_by"] = "High effort is being spent on easy work"  # tool_loops, the top saving
    model.sections.append(_habits_tables(habits_playbook=rows))
    result = qa.run("habits", _ctx(tmp_path, model=model))
    tips = result["tips"][-qa.PLAYBOOK_TIPS:]
    # tool_loops is skipped; the next 3 rows take its place.
    assert [t["title"] for t in tips] == [
        "Ask agents for short reports", "Name the files you already know", "Keep tool output small",
    ]


def test_playbook_tips_pick_at_most_one_habit_per_theme(tmp_path):
    """Two rows sharing a theme (both "delegation") only contribute their
    top-saving row; a lower-ranked row with a fresh theme takes the
    second slot instead of being crowded out."""
    model = _full_model()
    model.recommendations = []
    rows = [dict(row) for row in _PLAYBOOK]
    rows[0]["theme"] = "delegation"  # tool_loops, saving 2.0
    rows[1]["theme"] = "delegation"  # short_reports, saving 1.0 -- same theme, skipped
    rows[2]["theme"] = "breakdown"  # name_files, saving 0.5
    rows[3]["theme"] = "information"  # quiet_output, saving 0.25
    model.sections.append(_habits_tables(habits_playbook=rows))
    result = qa.run("habits", _ctx(tmp_path, model=model))
    tips = result["tips"][-qa.PLAYBOOK_TIPS:]
    assert [t["title"] for t in tips] == [
        "Stop retrying a failing command", "Name the files you already know", "Keep tool output small",
    ]


def test_skills_late_or_not_needed_become_tips(tmp_path):
    model = _full_model()
    model.sections.append(_habits_tables(habits_skills=[
        {"skill": "review", "late": 3, "unneeded": 0, "before": 0.4},
        {"skill": "deploy", "late": 1, "unneeded": 2, "before": None},
        {"skill": "lint", "late": 1, "unneeded": 1, "before": None},
    ]))
    tips = qa._skill_timing_tips(_ctx(tmp_path, model=model))
    assert [t["title"] for t in tips] == ["review: run it at the start", "deploy: often not needed"]
    assert "3 times, with about 0.40 USD already spent each time" in tips[0]["text"]
    assert "/review" in tips[0]["text"] and "disable-model-invocation: true" in tips[1]["text"]


def test_tool_output_says_to_stop_a_failing_command_sooner(tmp_path):
    model = _full_model()
    model.sections.append(_habits_tables(habits_tool_output=[{"tool": "loops", "loops": 4, "cost": 1.2}]))
    tips = qa.run("tool-output", _ctx(tmp_path, model=model))["tips"]
    tip = next(t for t in tips if t["title"] == "Stop a failing command sooner")
    assert tip["text"].startswith("4 commands failed three or more times within one message")


def test_quality_tells_you_what_came_before_work_you_said_missed(tmp_path):
    model = _quality_model([{**_AGENT, "unfinished_pct": 3.0, "turn_limit_pct": 0.0}])
    model.sections.append(_habits_tables(habits_outcomes=[
        {"outcome": "missed", "pieces": 2, "cost": 3.0, "slow": "Wrong approach or rework", "helped": "A plan first"},
    ]))
    tips = qa.run("quality", _ctx(tmp_path, model=model))["tips"]
    tip = next(t for t in tips if t["title"] == "Work you said missed its goal")
    assert tip["text"] == (
        "2 pieces of work missed their goal, costing 3.00 USD. Most often slowed by: Wrong approach or rework. "
        "Would have helped most: A plan first."
    )


def test_models_check_leaves_out_an_agent_claude_said_needed_a_larger_model(tmp_path):
    model = _full_model()
    model.sections.append(_habits_tables(habits_agents=[
        {"agent_type": "Explore", "fit_smaller": 0, "fit_larger": 2, "hard_pct": None, "retried_model": 0},
    ]))
    result = qa.run("models", _ctx(tmp_path, model=model))
    assert [fix["agent"] for fix in result["fixes"]] == [None]
    [tip] = result["tips"]
    assert tip["title"] == "Explore: haiku not suggested"
    assert tip["text"].endswith("but Claude said 2 of its runs needed a larger model.")

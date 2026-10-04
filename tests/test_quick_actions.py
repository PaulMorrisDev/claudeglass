"""Quick actions: every check answers, and its fixes keep the fix
contract (``quick_actions``)."""

from __future__ import annotations

import ast
import json
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from claudeglass import agent_models, capture_catalogue, discovery, known_savers, quality, waste
from claudeglass import quick_actions as qa
from claudeglass.fixes import PROMPT_RESTART, SCOPE_NOTE, RESTART_NOTE
from claudeglass.model import Recommendation
from claudeglass.units import Units

from test_whatif import _model, _table

API_MD = Path(__file__).resolve().parent.parent / "docs" / "api.md"
SRC = Path(__file__).resolve().parent.parent / "src" / "claudeglass"
#: Every module that can build a ``Recommendation`` -- see recommend.py's
#: own ``recommend()`` entry point, which folds each of these in.
_RULE_MODULES = ("recommend.py", "advice.py", "carry.py", "compaction_sim.py", "handoff.py", "hook_costs.py",
                  "run_split.py", "model_swap.py", "waste.py", "elasticity.py", "tool_search.py")

UNITS = Units(billing_mode="api", currency="USD")
FIX_KEYS = {"key", "agent", "explainer", "command", "command_warning", "prompt", "title"}


def _ctx(tmp_path, model=None, **kw):
    config_dir = tmp_path / ".claude" / "claudeglass"
    config_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / ".claude" / "projects").mkdir(exist_ok=True)
    return qa.Context(
        model=model if model is not None else _full_model(),
        units=kw.get("units", UNITS),
        period="over the last 14 days",
        config_dir=config_dir,
        effective=kw.get("effective", {}),
        effective_agents=kw.get("effective_agents", {}),
        skip_keys=kw.get("skip_keys", frozenset()),
        since_ts=kw.get("since_ts"),
        until_ts=kw.get("until_ts"),
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
    id space a quick-action's ``rule_ids`` can point into. ``agent_models``
    builds its three through one helper, so its ids are its ``RULES`` keys."""
    ids: set[str] = set(agent_models.RULES)
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


def test_claude_md_check_of_one_project_reads_only_its_files_and_yours(tmp_path):
    ctx = _ctx(tmp_path, model=NS(sections=[], context_files={}, recommendations=[]))
    claude_root = ctx.config_dir.parent
    (claude_root / "CLAUDE.md").write_text("# Rules\n\nBe brief.\n", encoding="utf-8")
    folders = {}
    for name in ("alpha-repo", "beta-repo"):
        folder = tmp_path / "work" / name
        folder.mkdir(parents=True)
        (folder / "CLAUDE.md").write_text(f"# {name}\n\nNotes for {name}.\n", encoding="utf-8")
        (claude_root / "projects" / name).mkdir()
        (claude_root / "projects" / name / "session.jsonl").write_text(json.dumps({"cwd": str(folder)}) + "\n", encoding="utf-8")
        folders[name] = folder

    def listed() -> list[str]:
        return [row[0] for row in qa.run("claude-md", ctx)["table"]["rows"]]

    def projects(paths: list[str]) -> set[str]:
        return {name for name in folders if any(name in path for path in paths)}

    assert ctx.only is None
    assert projects(listed()) == {"alpha-repo", "beta-repo"}
    ctx.only = (folders["alpha-repo"],)
    one = listed()
    assert projects(one) == {"alpha-repo"}
    assert any("-repo" not in path for path in one)  # yours is still read
    # A project whose folder isn't known lists only yours.
    ctx.only = ()
    assert projects(listed()) == set()


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


_AGENT_MODEL_IDS = ("agent-decide-apply", "agent-model-asked", "agent-model-inherited")


def _agent_model_rec(rec_id, severity, title, why="", prompt=None, key=""):
    """An agent-model card as ``agent_models.RULES`` and ``advice`` leave it:
    its title and why, and a fix with a prompt (``fixes.build_fixes``'s
    shape) unless ``prompt`` is empty."""
    fixes = [{"key": None, "agent": None, "explainer": [("Where", "Your notes.")], "command": None,
              "command_warning": None, "prompt": prompt, "title": title}] if prompt else []
    return Recommendation(id=rec_id, severity=severity, category="workflow", title=title, why=why, fixes=fixes,
                          agent_type="workflow-subagent", key=key)


_INHERITED = "5 workflow agents wrote code on Opus 5.5 with no model set"


def test_models_check_acts_on_an_advice_level_agent_model_card_and_offers_its_fix(tmp_path):
    model = _model()
    model.recommendations = [_agent_model_rec(
        "agent-model-inherited", "advice", _INHERITED, why="They ran on your main session's model.",
        prompt="From now on, set the model on every agent you start.",
    )]
    result = qa.run("models", _ctx(tmp_path, model=model))
    assert result["status"] == "act"
    assert result["summary"].startswith(_INHERITED + ".")
    assert "no change to an agent file or setting would save a useful amount" in result["summary"]
    [fix] = result["fixes"]
    assert fix["title"] == _INHERITED and fix["prompt"] == "From now on, set the model on every agent you start."
    assert FIX_KEYS <= set(fix)
    assert result["tips"] == []
    assert result["table"] == qa.run("models", _ctx(tmp_path, model=_model()))["table"]


def test_models_check_adds_the_agent_model_title_after_its_own_model_changes(tmp_path):
    model = _full_model()
    model.recommendations = [_agent_model_rec(
        "agent-model-inherited", "advice", _INHERITED, prompt="From now on, set the model on every agent you start.",
    )]
    result = qa.run("models", _ctx(tmp_path, model=model, effective_agents={"Explore": {}}))
    plain = qa.run("models", _ctx(tmp_path, effective_agents={"Explore": {}}))
    assert result["status"] == plain["status"] == "act"
    assert result["summary"] == plain["summary"] + " " + _INHERITED + "."
    assert [fix["title"] for fix in result["fixes"]] == [fix["title"] for fix in plain["fixes"]] + [_INHERITED]


def test_models_check_names_three_advice_level_cards_then_counts_the_rest(tmp_path):
    model = _model()
    model.recommendations = [
        _agent_model_rec("agent-model-inherited", "advice", f"3 {kind} agents wrote code on Opus 5.5 with no model set")
        for kind in ("workflow", "general-purpose", "claude-implementer", "builder", "fixer")
    ]
    summary = qa.run("models", _ctx(tmp_path, model=model))["summary"]
    assert summary.startswith(
        "3 workflow agents wrote code on Opus 5.5 with no model set. "
        "3 general-purpose agents wrote code on Opus 5.5 with no model set. "
        "3 claude-implementer agents wrote code on Opus 5.5 with no model set. 2 more are flagged too."
    )
    model.recommendations = model.recommendations[:4]
    summary = qa.run("models", _ctx(tmp_path, model=model))["summary"]
    assert summary.endswith(
        "1 more is flagged too. Apart from that, no change to an agent file or setting would save a useful amount."
    )


def test_models_check_offers_info_level_agent_model_cards_as_fixes_not_tips(tmp_path):
    plain = qa.run("models", _ctx(tmp_path, model=_model()))
    model = _model()
    model.recommendations = [
        _agent_model_rec("agent-model-inherited", "info", _INHERITED, why="Your 6 later workflow runs set a model.",
                         prompt="Copy this rule to your CLAUDE.md."),
        _agent_model_rec("agent-model-asked", "info", "46 agents that wrote code were started on Opus 5.5",
                         why="Sonnet would cost less.", prompt="Find where these agents are started."),
        _agent_model_rec("agent-decide-apply", "info", "5 general-purpose agents decided and changed code on Opus 5.5",
                         why="Splitting it lets Opus decide and Sonnet apply.", prompt="Split the deciding from the applying."),
        _agent_model_rec("long-tool-waits", "advice", "Tools often wait on you", why="Not a models finding.",
                         prompt="Pre-approve routine tools."),
    ]
    result = qa.run("models", _ctx(tmp_path, model=model))
    assert result["status"] == plain["status"] == "ok"
    assert result["summary"] == plain["summary"] and result["table"] == plain["table"]
    # Each card's own fix is offered; another rule's is not.
    assert [fix["prompt"] for fix in result["fixes"]] == [
        "Copy this rule to your CLAUDE.md.", "Find where these agents are started.",
        "Split the deciding from the applying.",
    ]
    # A card whose fix carries a prompt isn't said twice as a tip.
    assert result["tips"] == plain["tips"]


def test_models_check_lists_an_info_level_agent_model_card_with_no_prompt_as_a_tip(tmp_path):
    plain = qa.run("models", _ctx(tmp_path, model=_model()))
    model = _model()
    model.recommendations = [
        _agent_model_rec("agent-model-asked", "info", "46 agents that wrote code were started on Opus 5.5",
                         why="Sonnet would cost less.", prompt="Find where these agents are started."),
        _agent_model_rec("agent-model-asked", "info", "3 claude agents that wrote code were started on Opus 5.5",
                         why="Sonnet would cost less here too."),
    ]
    # A fix with nothing to paste or run isn't offered.
    model.recommendations[1].fixes = [{"key": None, "agent": None, "explainer": [], "command": None,
                                       "command_warning": None, "prompt": None, "title": None}]
    result = qa.run("models", _ctx(tmp_path, model=model))
    assert result["status"] == "ok"
    assert [fix["prompt"] for fix in result["fixes"]] == ["Find where these agents are started."]
    # Decided per card: the same id with no prompt is still a tip.
    assert result["tips"] == plain["tips"] + [
        {"title": "3 claude agents that wrote code were started on Opus 5.5", "text": "Sonnet would cost less here too."},
    ]


def test_models_check_leaves_an_ignored_agent_model_card_out(tmp_path):
    model = _model()
    model.recommendations = [
        _agent_model_rec("agent-model-inherited", "advice", _INHERITED, prompt="Set the model.",
                         key="agent-model-inherited:workflow-subagent"),
        _agent_model_rec("agent-model-asked", "info", "46 agents that wrote code were started on Opus 5.5",
                         why="Sonnet would cost less.", prompt="Find where these agents are started.",
                         key="agent-model-asked:workflow-subagent"),
    ]
    ctx = _ctx(tmp_path, model=model)
    ctx.skip_keys = frozenset({"agent-model-inherited:workflow-subagent"})
    result = qa.run("models", ctx)
    assert result["status"] == "ok"
    assert [fix["prompt"] for fix in result["fixes"]] == ["Find where these agents are started."]


def test_models_check_claims_the_three_agent_model_rule_ids(tmp_path):
    result = qa.run("models", _ctx(tmp_path))
    assert set(_AGENT_MODEL_IDS) <= set(result["rule_ids"])
    assert {"model-tier", "model-tier-main"} <= set(result["rule_ids"])
    assert qa._AGENT_MODEL_RECS == set(_AGENT_MODEL_IDS)
    # No other check claims them: ignoring a card never moves it elsewhere.
    for check in qa.CHECKS:
        if check.id != "models":
            assert not set(_AGENT_MODEL_IDS) & set(check.rule_ids), check.id


def test_tool_output_offers_the_bash_cap_as_a_prompt_only(tmp_path):
    result = qa.run("tool-output", _ctx(tmp_path))
    fix = next(f for f in result["fixes"] if f["key"] == "BASH_MAX_OUTPUT_LENGTH")
    assert fix["command"] is None
    assert '"env"' in fix["prompt"] and "diff" in fix["prompt"]


def _cap_row(usd_saved, carry_cost_usd):
    return {"setting": "BASH_MAX_OUTPUT_LENGTH", "value": "15000", "cap_tokens": 3750, "results": 10,
            "results_affected": 2, "tokens_saved": 5000, "usd_saved": usd_saved, "carry_cost_usd": carry_cost_usd}


def test_tool_output_prices_the_bash_cap_from_its_own_saving(tmp_path):
    model = _full_model()
    carry = next(s for s in model.sections if s.key == "carry")
    carry.tables.append(_table("carry_output_cap_savings", [_cap_row(2.0, 8.0)]))
    fix = next(
        f for f in qa.run("tool-output", _ctx(tmp_path, model=model))["fixes"] if f["key"] == "BASH_MAX_OUTPUT_LENGTH"
    )
    effect = dict(fix["explainer"])["Expected effect"]
    assert "would have cut 2 of 10 results" in effect and "saved up to 2.00 USD" in effect


def _no_read_carry_model():
    """``_full_model`` with the ``Read`` row taken out of ``carry_by_tool``,
    so only the output-cap logic is under test -- the "would save little"
    cap sentence moved into the summary, never a fix or a tip."""
    model = _full_model()
    carry = next(s for s in model.sections if s.key == "carry")
    carry.tables[0].rows = [row for row in carry.tables[0].rows if row[0] != "Read"]
    return model


def test_tool_output_explains_a_bash_cap_that_would_save_little_in_the_summary(tmp_path):
    model = _no_read_carry_model()
    carry = next(s for s in model.sections if s.key == "carry")
    carry.tables.append(_table("carry_output_cap_savings", [_cap_row(0.3, 8.0)]))
    result = qa.run("tool-output", _ctx(tmp_path, model=model))
    assert result["status"] == "ok" and result["fixes"] == []
    assert not any(t["title"].startswith("BASH_MAX_OUTPUT_LENGTH") for t in result["tips"])
    assert "wouldn't help much" in result["summary"]
    assert "BASH_MAX_OUTPUT_LENGTH at 15000 would have saved 0.30 USD" in result["summary"]
    assert "because most results are already short" in result["summary"]


def test_tool_output_offers_a_fix_when_read_dominates_the_carried_context(tmp_path):
    """Read's carry cost is >=10% of the total and it has >=5 results:
    a real fix, not just a tip -- and, since it really is the largest
    carry cost here, the fix says so."""
    model = _full_model()
    carry = next(s for s in model.sections if s.key == "carry")
    carry.tables[0] = _table("carry_by_tool", [
        {"key": "Bash", "result_count": 10, "tokens_entered": 3000, "mean_turns_carried": 20, "carry_cost_usd": 2.0},
        {"key": "Read", "result_count": 10, "tokens_entered": 5000, "mean_turns_carried": 30, "carry_cost_usd": 8.0},
    ])
    result = qa.run("tool-output", _ctx(tmp_path, model=model))
    assert result["status"] == "act"
    read_fix = next(f for f in result["fixes"] if f["title"] == "Have Claude read only the part of a file it needs")
    assert read_fix["command"] is None and read_fix["note"] == "scope"
    why = dict(read_fix["explainer"])["Why it's suggested"]
    assert why == "Read carried 5,000 tokens over 10 reads; each stayed about 30 replies. That's the most of any tool."
    assert "offset and limit" in read_fix["prompt"]


def test_tool_output_read_fix_says_largest_only_when_it_really_is(tmp_path):
    """In the default fixture Bash still costs more than Read, so Read's
    own fix doesn't claim to be the largest carry cost -- while its own
    10%-of-total and >=5-results bar is still cleared."""
    result = qa.run("tool-output", _ctx(tmp_path))
    read_fix = next(f for f in result["fixes"] if f["title"] == "Have Claude read only the part of a file it needs")
    why = dict(read_fix["explainer"])["Why it's suggested"]
    assert why == "Read carried 5,000 tokens over 10 reads; each stayed about 30 replies."
    assert "most of any tool" not in why


def test_tool_output_skips_the_read_fix_when_tool_output_carry_already_covers_it(tmp_path):
    model = _full_model()
    model.recommendations = [Recommendation(id="tool-output-carry", title="Tool results fill your context",
                                             why="Carrying results cost a lot.")]
    result = qa.run("tool-output", _ctx(tmp_path, model=model))
    assert not any(f["title"] == "Have Claude read only the part of a file it needs" for f in result["fixes"])
    assert any("Grep over Read" in f["prompt"] for f in result["fixes"])


def test_tool_output_read_fix_points_at_tokensaves_own_tools(tmp_path):
    model = _full_model()
    model.sections.append(NS(key="waste", tables=[_table("waste_blocked_by", [
        {"blocker": "tokensave", "agent_type": "top-level", "kind": "saver", "turns": 1, "cost_usd": 0.1,
         "tokens": 100, "lever": "Use tokensave's own tools."},
    ])]))
    result = qa.run("tool-output", _ctx(tmp_path, model=model))
    read_fix = next(f for f in result["fixes"] if f["title"] == "Have Claude read only the part of a file it needs")
    assert "tokensave_context" in read_fix["prompt"] and "tokensave_read" in read_fix["prompt"]
    assert '"lines" mode' in read_fix["prompt"]


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
    """A habit rec with no prompt fix (an informational-only workflow
    id, see fixes._WORKFLOW_EXPLAINER) surfaces as a plain tip."""
    model = _full_model()
    model.recommendations = [
        Recommendation(id="cache-read-dominance", title="Cache reads dominate cost",
                        action="Batch related work into fewer, longer sessions."),
    ]
    result = qa.run("habits", _ctx(tmp_path, model=model))
    assert result["status"] == "act"
    assert result["tips"] == [
        {"title": "Cache reads dominate cost", "text": "Batch related work into fewer, longer sessions."},
    ]


def test_a_rec_whose_fix_already_has_a_prompt_is_not_also_a_tip(tmp_path):
    """UX: a rec like long-tool-waits already gets a fix card with a prompt
    (fixes._WORKFLOW_PROMPTS) -- listing it as a tip too would say the
    same finding twice."""
    model = _full_model()
    model.recommendations = [
        Recommendation(id="long-tool-waits", title="Tools often wait on you", action="Pre-approve routine tools."),
    ]
    result = qa.run("habits", _ctx(tmp_path, model=model))
    assert result["tips"] == []
    [fix] = result["fixes"]
    assert fix["prompt"]


def test_markdown_carries_the_table_tips_and_fixes(tmp_path):
    text = qa.render_markdown(qa.run("models", _ctx(tmp_path)))
    assert text.startswith("## Is each agent on the cheapest model")
    assert "| Agent |" in text and "### Explore: use haiku" in text and "```bash" in text


def test_markdown_notes_a_fix_by_its_own_note_kind():
    """``render_markdown`` looks up each fix's note via ``fixes.fix_note``
    instead of always appending ``RESTART_NOTE`` -- a "from now on" scope
    prompt gets ``SCOPE_NOTE``, a fix marked ``note="none"`` gets no note
    line at all, and a plain settings fix (no ``note`` key) still gets
    ``RESTART_NOTE``."""
    result = {
        "question": "Q", "summary": "S", "tips": [],
        "fixes": [
            {"title": "Scope it", "explainer": [], "prompt": "Do X.", "command": None, "note": "scope"},
            {"title": "No note", "explainer": [], "prompt": "Do Y.", "command": None, "note": "none"},
            {"title": "Setting", "explainer": [], "prompt": "Do Z.", "command": "SOME_SETTING=1"},
        ],
    }
    text = qa.render_markdown(result)
    scope_section = text.split("### Scope it", 1)[1].split("### No note", 1)[0]
    no_note_section = text.split("### No note", 1)[1].split("### Setting", 1)[0]
    setting_section = text.split("### Setting", 1)[1]
    assert SCOPE_NOTE in scope_section and RESTART_NOTE not in scope_section
    assert SCOPE_NOTE not in no_note_section and RESTART_NOTE not in no_note_section
    assert RESTART_NOTE in setting_section


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


def _waste_tables(**tables) -> NS:
    return NS(key="waste", tables=[_table(name, rows) for name, rows in tables.items()])


def _waste_model(*, recommendations=None, **tables) -> NS:
    model = _model()
    model.recommendations = recommendations or []
    model.sections.append(_waste_tables(**tables))
    return model


_PLAYBOOK = [
    {
        "habit": key, "saving": saving, "saving_total": 2 * saving,
        "evidence": f"{key} evidence.", "example": f"{key} example.", "source": "inferred",
    }
    for key, saving in (("targeted_checks", 2.0), ("short_reports", 1.0), ("name_files", 0.5), ("quiet_output", 0.25))
]


def test_habits_shows_the_top_of_the_playbook_as_tips(tmp_path):
    model = _full_model()
    model.recommendations = []
    model.sections.append(_habits_tables(habits_playbook=_PLAYBOOK))
    result = qa.run("habits", _ctx(tmp_path, model=model))
    assert result["status"] == "act" and "{{page:habits}} has the rest." in result["summary"]
    tips = result["tips"][-qa.PLAYBOOK_TIPS:]
    assert [t["title"] for t in tips] == [
        "Check each change, and run the full suite once", "Ask agents for short reports", "Name the files you already know",
    ]
    assert tips[0]["text"] == (
        f"targeted_checks evidence. Try: targeted_checks example. About {qa._money(_ctx(tmp_path), 2.0)} a week (inferred)."
    )


def test_playbook_tips_skip_a_habit_already_covered_by_a_fired_recommendation(tmp_path):
    """UX-3, "quick actions deduped by theme": a habit ``apply_covered_by``
    (habits.py) has already matched to a fired rule is skipped here too --
    it's already said, via ``_HABIT_RECS`` or the Recommendations section,
    so a tip repeating it would say the same thing twice."""
    model = _full_model()
    model.recommendations = []
    rows = [dict(row, covered_by="") for row in _PLAYBOOK]
    rows[0]["covered_by"] = "High effort is being spent on easy work"  # targeted_checks, the top saving
    model.sections.append(_habits_tables(habits_playbook=rows))
    result = qa.run("habits", _ctx(tmp_path, model=model))
    tips = result["tips"][-qa.PLAYBOOK_TIPS:]
    # targeted_checks is skipped; the next 3 rows take its place.
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
    rows[0]["theme"] = "delegation"  # targeted_checks, saving 2.0
    rows[1]["theme"] = "delegation"  # short_reports, saving 1.0 -- same theme, skipped
    rows[2]["theme"] = "breakdown"  # name_files, saving 0.5
    rows[3]["theme"] = "information"  # quiet_output, saving 0.25
    model.sections.append(_habits_tables(habits_playbook=rows))
    result = qa.run("habits", _ctx(tmp_path, model=model))
    tips = result["tips"][-qa.PLAYBOOK_TIPS:]
    assert [t["title"] for t in tips] == [
        "Check each change, and run the full suite once", "Name the files you already know", "Keep tool output small",
    ]


def test_playbook_tips_use_the_rows_own_title_when_present(tmp_path):
    """A ``habits_playbook`` row can carry its own resolved ``title``
    (``habits.item_title``, additive column) -- the tip shows that
    verbatim instead of looking ``habit`` up in ``habits.ITEMS``."""
    model = _full_model()
    model.recommendations = []
    rows = [dict(row, title="") for row in _PLAYBOOK]
    rows[0]["title"] = "A custom title for targeted_checks"
    model.sections.append(_habits_tables(habits_playbook=rows))
    result = qa.run("habits", _ctx(tmp_path, model=model))
    tips = result["tips"][-qa.PLAYBOOK_TIPS:]
    assert tips[0]["title"] == "A custom title for targeted_checks"


def test_a_playbook_tip_on_a_subscription_says_the_period_on_the_list_price_equivalent(tmp_path):
    """Without a reading of the weekly limit the amount is a list-price
    equivalent, and a week's saving still says it is a week's."""
    model = _full_model()
    model.recommendations = []
    model.sections.append(_habits_tables(habits_playbook=_PLAYBOOK))
    result = qa.run("habits", _ctx(tmp_path, model=model, units=Units(billing_mode="subscription", currency="USD")))
    assert "2.00 USD list-price equivalent a week" in result["tips"][0]["text"]


# -- Habits card: who blocked it, and redirects aren't waste --------------------


_BLOCKED_BY = [
    {"blocker": "Claude Code's worktree guard", "agent_type": "claude-implementer", "kind": "guard",
     "turns": 5, "cost_usd": 3.0, "tokens": 30_000, "lever": "Tell claude-implementer (its agent file) to run git..."},
    {"blocker": "tokensave", "agent_type": "top-level", "kind": "saver",
     "turns": 4, "cost_usd": 1.0, "tokens": 8_000, "lever": "Not waste: tokensave sent these calls..."},
]


_WASTE_BY_CAUSE = [
    {"cause": "tool-error", "turns": 2, "cost_usd": 0.5, "lever": "Check paths first."},
    {"cause": "blocked", "turns": 5, "cost_usd": 3.0, "lever": "Put the rule a hook enforces..."},
    {"cause": "redirected", "turns": 4, "cost_usd": 1.0, "lever": "Not waste: a token saver's own hook..."},
    {"cause": "interrupt", "turns": 3, "cost_usd": 0.75, "lever": "Batch instructions."},
    {"cause": "tool-denial", "turns": 0, "cost_usd": 0.0, "lever": "Allow it."},
]


def test_failed_calls_replaces_the_blocked_row_with_the_blocked_by_breakdown(tmp_path):
    model = _waste_model(waste_by_cause=_WASTE_BY_CAUSE, waste_blocked_by=_BLOCKED_BY)
    result = qa.run("failed-calls", _ctx(tmp_path, model=model))
    labels = [row[0] for row in result["table"]["rows"]]
    assert "tool-error" in labels
    assert "blocked" not in labels and "redirected" not in labels
    assert "blocked: claude-implementer, by Claude Code's worktree guard" in labels
    assert "redirected by tokensave (not waste)" in labels
    # The replies that went nowhere for another reason stay with the habits.
    assert "interrupt" not in labels


def test_the_failed_and_blocked_rows_are_not_in_the_habits_check(tmp_path):
    """The Work habits row on the Overview was built from these rows (the
    tool errors and every blocker's blocked replies); they have a check of
    their own, and the habits check keeps the causes that are about how
    you work."""
    model = _waste_model(waste_by_cause=_WASTE_BY_CAUSE, waste_blocked_by=_BLOCKED_BY)
    result = qa.run("habits", _ctx(tmp_path, model=model))
    assert [row[0] for row in result["table"]["rows"]] == ["interrupt"]
    assert "interrupt" not in [row[0] for row in qa.run("failed-calls", _ctx(tmp_path, model=model))["table"]["rows"]]


def test_a_window_of_only_redirects_reads_as_fine_not_a_problem(tmp_path):
    """A window whose only blocked replies are on-purpose saver redirects
    must not read as a problem: no wasted-turns rec fires (waste.py keeps
    redirected cost out of the wasted total), and the habits card itself
    must say so plainly rather than defaulting to a bland "nothing to
    report"."""
    model = _waste_model(
        waste_by_cause=[{"cause": "redirected", "turns": 4, "cost_usd": 1.0, "lever": "Not waste: ..."}],
        waste_blocked_by=[
            {"blocker": "tokensave", "agent_type": "top-level", "kind": "saver", "turns": 4, "cost_usd": 1.0,
             "tokens": 8_000, "lever": "Not waste: tokensave sent these calls..."},
        ],
    )
    result = qa.run("failed-calls", _ctx(tmp_path, model=model))
    assert result["status"] == "ok"
    assert "not a problem" in result["summary"]
    assert "tokensave" in result["summary"]
    assert qa.run("habits", _ctx(tmp_path, model=model))["status"] == "no_data"


_WASTED_TURNS = Recommendation(
    id="wasted-turns", severity="advice", title="A material share of spend went to turns with no benefit",
    action="10.0% of priced cost went to turns whose output was never used.",
)


def test_the_act_summary_says_the_costliest_cause_found(tmp_path):
    model = _waste_model(
        recommendations=[
            Recommendation(id="cache-read-dominance", title="Cache reads dominate cost", action="Batch work."),
        ],
        waste_by_cause=[
            {"cause": "interrupt", "turns": 2, "cost_usd": 0.5, "lever": "Batch instructions."},
            {"cause": "max-turns", "turns": 5, "cost_usd": 3.0, "lever": "Raise the budget."},
        ],
    )
    result = qa.run("habits", _ctx(tmp_path, model=model))
    assert result["status"] == "act"
    assert "The costliest: max-turns" in result["summary"]


def test_a_problem_that_did_not_clear_the_bar_still_says_its_cost(tmp_path):
    """No rec fired and no playbook tip -- but the table still shows real
    wasted-reply cost, so the "ok" summary should say so instead of a
    blanket "no habit stands out"."""
    model = _waste_model(
        waste_by_cause=[{"cause": "interrupt", "turns": 2, "cost_usd": 0.5, "lever": "Batch instructions."}],
    )
    result = qa.run("habits", _ctx(tmp_path, model=model))
    assert result["status"] == "ok"
    assert "went to replies that went nowhere" in result["summary"]


# -- Failed and blocked tool calls ----------------------------------------------


def test_failed_calls_is_its_own_check_after_habits_and_claims_the_wasted_turns_finding(tmp_path):
    ids = list(qa.CHECK_IDS)
    assert ids.index("failed-calls") == ids.index("habits") + 1
    check = next(c for c in qa.CHECKS if c.id == "failed-calls")
    assert check.rule_ids == ("wasted-turns",)
    # One finding is one check's: the habits check no longer claims it.
    assert "wasted-turns" not in next(c for c in qa.CHECKS if c.id == "habits").rule_ids


def test_failed_calls_says_what_the_failed_and_blocked_calls_cost_and_offers_its_own_fix(tmp_path):
    model = _waste_model(
        recommendations=[_WASTED_TURNS],
        waste_by_cause=_WASTE_BY_CAUSE,
        waste_blocked_by=_BLOCKED_BY,
    )
    result = qa.run("failed-calls", _ctx(tmp_path, model=model))
    assert result["id"] == "failed-calls" and result["rule_ids"] == ["wasted-turns"]
    assert result["status"] == "act"
    # 2 tool errors and the 5 blocked replies; the redirects are on purpose.
    assert result["summary"].startswith("7 replies went to failed or blocked tool calls over the last 14 days")
    assert "The costliest: blocked: claude-implementer, by Claude Code's worktree guard" in result["summary"]
    assert "{{page:spend/savings}}" in result["summary"]
    own, found = result["fixes"]
    assert set(own) >= FIX_KEYS and own["key"] is None and own["note"] == "scope"
    assert own["title"] == waste.CALL_FAILURE_FIX["title"]
    assert own["prompt"].startswith(waste.CALL_FAILURE_FIX["prompt"]) and "AskUserQuestion" in own["prompt"]
    assert [heading for heading, _ in own["explainer"]] == [
        "Why it's suggested", "Where and who it affects", "Trade-off", "How to undo it",
    ]
    # The finding's own fix follows it, and is not also a tip.
    assert "flag it before you start rather than after" in found["prompt"]
    assert result["tips"] == []


def test_failed_calls_is_fine_when_nothing_cleared_the_bar_and_says_so_when_nothing_failed(tmp_path):
    quiet = _waste_model(waste_by_cause=[{"cause": "tool-error", "turns": 2, "cost_usd": 0.5, "lever": "Check paths."}])
    result = qa.run("failed-calls", _ctx(tmp_path, model=quiet))
    assert result["status"] == "ok" and result["fixes"] == []
    assert "went to replies lost to failed or blocked tool calls over the last 14 days, but not enough to flag" in result["summary"]
    none = _waste_model(waste_by_cause=[{"cause": "interrupt", "turns": 2, "cost_usd": 0.5, "lever": "Batch."}])
    result = qa.run("failed-calls", _ctx(tmp_path, model=none))
    assert result["status"] == "ok"
    assert result["summary"] == "No reply was lost to a failed or blocked tool call over the last 14 days."
    # No waste section at all: too little to say.
    empty = NS(sections=[], context_files={}, recommendations=[])
    assert qa.run("failed-calls", _ctx(tmp_path, model=empty))["status"] == "no_data"


def test_failed_calls_does_not_claim_waste_that_was_not_a_failed_call(tmp_path):
    """``wasted-turns`` fires on every wasted reply, interrupts included, so
    when the waste is all interrupts the check must not say a call failed."""
    model = _waste_model(
        recommendations=[_WASTED_TURNS],
        waste_by_cause=[{"cause": "interrupt", "turns": 40, "cost_usd": 30.0, "lever": "Batch instructions."}],
    )
    result = qa.run("failed-calls", _ctx(tmp_path, model=model))
    assert result["status"] == "act"
    assert result["summary"].startswith(
        "Replies that went nowhere cost a material share of spend over the last 14 days. "
        "Most of that was not from failed or blocked tool calls."
    )


# -- The Overview's Work habits row: rework lead, playbook saving, item -------------


def _rework_model(count, total, *, item="pieces"):
    model = _full_model()
    model.recommendations = []
    text = (
        f"{count} of your {total} pieces of work needed changes after Claude delivered them. "
        "That rework cost $1.00 over the last 14 days. 50% came from requests that left something out."
    )
    rows = [{
        "item": item, "text": text, "count": count, "total": total, "share": 100.0 * count / total,
        "cost": 1.0, "tokens": 1000, "period": "over the last 14 days",
    }]
    model.sections.append(NS(key="rework", tables=[_table("rework_headline", rows)]))
    return model


@pytest.mark.parametrize(
    ("count", "total", "leads"),
    [
        (1, 5, True),     # exactly 20% of exactly 5 pieces
        (2, 10, True),
        (4, 19, True),    # 21%
        (3, 4, False),    # 75%, but under 5 pieces
        (1, 6, False),    # 17% of 6
        (4, 21, False),   # 19% of 21
        (0, 5, False),
    ],
)
def test_the_rework_headline_leads_at_a_fifth_of_five_or_more_pieces(tmp_path, count, total, leads):
    result = qa.run("habits", _ctx(tmp_path, model=_rework_model(count, total)))
    if not leads:
        assert result["headline"] is None and result["item"] is None
        assert result["status"] == "no_data"
        return
    sentence = f"{count} of your {total} pieces of work needed changes after Claude delivered them."
    # The first sentence only: the cost and the causes are on the Work habits page.
    assert result["headline"] == sentence
    assert result["item"] == qa.REWORK_ITEM == "rework"
    assert result["status"] == "act"
    assert result["summary"].startswith(sentence + " 1 way of working cost tokens over the last 14 days.")


def test_requests_in_sessions_that_were_not_cut_into_pieces_never_lead(tmp_path):
    result = qa.run("habits", _ctx(tmp_path, model=_rework_model(10, 10, item="requests")))
    assert result["headline"] is None and result["status"] == "no_data"


def test_the_row_keeps_its_current_lead_when_too_little_was_reworked(tmp_path):
    """Under the threshold the check answers as it did: no headline, and
    the item to open is the top habit worth trying."""
    model = _rework_model(1, 6)
    model.sections.append(_habits_tables(habits_playbook=_PLAYBOOK))
    result = qa.run("habits", _ctx(tmp_path, model=model))
    assert result["headline"] is None and result["status"] == "act"
    assert result["item"] == "targeted_checks"
    assert result["summary"].startswith("3 ways of working cost tokens")
    # Over it, the headline leads and the rework section is the item.
    model = _rework_model(2, 6)
    model.sections.append(_habits_tables(habits_playbook=_PLAYBOOK))
    result = qa.run("habits", _ctx(tmp_path, model=model))
    assert result["headline"].startswith("2 of your 6 pieces") and result["item"] == "rework"
    assert result["summary"].startswith("2 of your 6 pieces of work needed changes after Claude delivered them. 4 ways")


def test_playbook_tips_carry_their_habit_for_the_link_to_its_card(tmp_path):
    model = _full_model()
    model.recommendations = []
    model.sections.append(_habits_tables(habits_playbook=_PLAYBOOK))
    result = qa.run("habits", _ctx(tmp_path, model=model))
    assert [tip["habit"] for tip in result["tips"]] == ["targeted_checks", "short_reports", "name_files"]
    assert result["item"] == "targeted_checks"


_HABIT_REC = Recommendation(
    id="long-tool-waits", key="long-tool-waits", severity="advice", title="Tools often wait on you",
    action="Pre-approve routine tools.", saving_usd=4.0,
)


def _saving_model(*, covered=True):
    """One habit recommendation saving 4.00 over the window, and a playbook
    of 2.00, 1.00, 0.50 and 0.25 a week: the top one covered by a fired
    rule, so ``apply_covered_by`` left it with no saving of its own."""
    model = _full_model()
    model.recommendations = [_HABIT_REC]
    rows = [dict(row, covered_by="") for row in _PLAYBOOK]
    if covered:
        rows[0].update(covered_by="Tools often wait on you", saving=None, saving_total=None)
    model.sections.append(_habits_tables(habits_playbook=rows))
    return model


def test_the_habits_saving_includes_the_playbook_saving_over_the_window(tmp_path):
    two_weeks = dict(since_ts=0.0, until_ts=14 * 86400.0)
    ctx = _ctx(tmp_path, model=_saving_model(), **two_weeks)
    result = qa.run("habits", ctx)
    # The recommendation's 4.00, and the habits it doesn't cover: 3.50 over the window.
    assert result["saving_usd"] == pytest.approx(4.0 + 1.75 * 2)
    assert result["saving"] == f"{qa._money(ctx, 7.5, period=True, prefix='About ')} if you change these habits."
    assert result["saving"] == "About 7.50 USD over the last 14 days if you change these habits."
    # A covered habit is in the recommendation's saving already: uncovered, it adds its 2.00 a week.
    both = qa.run("habits", _ctx(tmp_path, model=_saving_model(covered=False), **two_weeks))
    assert both["saving_usd"] == pytest.approx(4.0 + 3.75 * 2)


def test_the_playbook_saving_is_its_own_total_whatever_the_window(tmp_path):
    """Each habit adds what it would have saved over the window
    (``saving_total``), so a window longer or shorter than the sessions it
    holds neither scales it up nor cuts it down."""
    for window in ({}, dict(since_ts=0.0, until_ts=86400.0), dict(since_ts=0.0, until_ts=90 * 86400.0)):
        result = qa.run("habits", _ctx(tmp_path, model=_saving_model(), **window))
        assert result["saving_usd"] == pytest.approx(4.0 + 3.5)


def test_an_ignored_recommendation_adds_nothing_to_the_habits_saving(tmp_path):
    ctx = _ctx(tmp_path, model=_saving_model(), since_ts=0.0, until_ts=7 * 86400.0, skip_keys=frozenset({"long-tool-waits"}))
    assert qa.run("habits", ctx)["saving_usd"] == pytest.approx(3.5)


def test_a_habits_check_with_no_saving_says_none(tmp_path):
    model = _full_model()
    model.recommendations = [Recommendation(id="cache-read-dominance", title="Cache reads dominate cost", action="Batch work.")]
    result = qa.run("habits", _ctx(tmp_path, model=model))
    assert result["status"] == "act" and result["saving_usd"] is None and result["saving"] == ""


def test_the_list_carries_what_the_overview_row_needs(tmp_path):
    rows = {row["id"]: row for row in qa.run_all(_ctx(tmp_path, model=_saving_model(), since_ts=0.0, until_ts=14 * 86400.0))}
    habits = rows["habits"]
    assert habits["saving_usd"] == pytest.approx(7.5) and habits["saving"].startswith("About 7.50 USD over the last 14 days")
    assert habits["headline"] is None and habits["item"] == "short_reports"
    # Every other check answers with the four fields empty.
    for check_id, row in rows.items():
        assert {"headline", "item", "saving_usd", "saving"} <= set(row), check_id
        if check_id != "habits":
            assert (row["headline"], row["item"], row["saving_usd"], row["saving"]) == (None, None, None, ""), check_id


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
    """The count comes from the waste summary and is never priced: no amount is quoted."""
    model = _full_model()
    model.sections.append(_waste_tables(waste_summary=[{"metric": "all", "failed_command_loops": 4}]))
    tips = qa.run("tool-output", _ctx(tmp_path, model=model))["tips"]
    tip = next(t for t in tips if t["title"] == "Stop a failing command sooner")
    assert tip["text"].startswith("4 commands failed three or more times within one message.")
    assert "USD" not in tip["text"] and "$" not in tip["text"]


def test_tool_output_has_no_failing_command_tip_without_repeated_failures(tmp_path):
    model = _full_model()
    model.sections.append(_waste_tables(waste_summary=[{"metric": "all", "failed_command_loops": 0}]))
    tips = qa.run("tool-output", _ctx(tmp_path, model=model))["tips"]
    assert not [t for t in tips if t["title"] == "Stop a failing command sooner"]


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


def test_models_check_leaves_out_an_agent_whose_work_was_mostly_hard(tmp_path):
    model = _full_model()
    model.sections.append(_habits_tables(habits_agents=[
        {"agent_type": "Explore", "runs": 6, "hard_pct": 70.0, "retried_model": 0},
    ]))
    result = qa.run("models", _ctx(tmp_path, model=model))
    assert [fix["agent"] for fix in result["fixes"]] == [None]
    [tip] = result["tips"]
    assert tip["title"] == "Explore: haiku not suggested"
    assert tip["text"].endswith("but 70% of its work was reported hard.")


def _hooks_report(results):
    from claudeglass.hook_costs import RULES, HookThresholds, build_section, compute_hook_costs
    from claudeglass.model import ReportModel
    from claudeglass.pricing import load_pricing

    report = ReportModel(sections=[build_section(compute_hook_costs(results, load_pricing()))])
    report.recommendations = [rec for rule in RULES for rec in rule(report, HookThresholds())]
    return report


def test_hooks_check_leaves_a_hook_that_stopped_failing_out_of_the_fix(tmp_path):
    from test_hook_costs import _bash_calls_later, _bash_failures, _failed, _result

    turns, events = _bash_failures(20)
    events += [_failed("other.ps1", ts="2026-09-21T11:00:00Z", hook="Stop") for _ in range(12)]
    result = qa.run("hooks", _ctx(tmp_path, model=_hooks_report([_result(turns + _bash_calls_later(5), events)])))
    assert result["status"] == "act"
    assert result["summary"] == (
        "1 of your hooks failed 12 times, so they didn't do their job. guard.ps1 stopped failing after "
        "2026-09-20 10:20 UTC."
    )
    cells = {row[0]: row[3] for row in result["table"]["rows"]}
    assert cells == {"guard.ps1": "2026-09-20 10:20 UTC (stopped failing)", "other.ps1": "2026-09-21 11:00 UTC"}
    [fix] = result["fixes"]
    assert "guard.ps1" not in fix["title"]


def test_hooks_check_is_fine_once_every_failing_hook_stopped(tmp_path):
    from test_hook_costs import _bash_calls_later, _bash_failures, _result

    turns, events = _bash_failures(20)
    result = qa.run("hooks", _ctx(tmp_path, model=_hooks_report([_result(turns + _bash_calls_later(5), events)])))
    assert result["status"] == "ok"
    assert result["summary"] == (
        "Your hooks work: guard.ps1, which failed earlier in this window, stopped failing after 2026-09-20 10:20 UTC, "
        "and none costs much in kept context or blocked calls."
    )



# -- "What does tokensave save you?" (known-savers) -----------------------------


def _write_ledger(path: Path, rows: list[tuple]):
    """A minimal ``savings_ledger`` table, as tokensave itself writes it
    (see ``known_savers.py``'s module docstring), holding exactly the
    columns ``known_savers.read_ledger`` selects."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE savings_ledger (ts INTEGER, project_path TEXT, tool_name TEXT, "
        "before_tokens INTEGER, after_tokens INTEGER)"
    )
    conn.executemany(
        "INSERT INTO savings_ledger (ts, project_path, tool_name, before_tokens, after_tokens) VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def _tokensave_carry_row(cost: float, tokens: int, calls: int = 5):
    return {"key": "mcp__tokensave__search", "result_count": calls, "tokens_entered": tokens,
            "mean_turns_carried": 10, "carry_cost_usd": cost}


def _redirect_row(replies: float, cost: float):
    return {"blocker": "tokensave", "agent_type": "top-level", "kind": "saver", "turns": replies,
            "cost_usd": cost, "tokens": 0, "lever": "Use tokensave's own tools."}


def _savers_model(carry_rows=None, redirect_rows=None):
    model = _model()
    model.sections.append(NS(key="carry", tables=[_table("carry_by_tool", carry_rows or [])]))
    model.sections.append(NS(key="waste", tables=[_table("waste_blocked_by", redirect_rows or [])]))
    return model


def _patch_ledger(monkeypatch, tmp_path, rows: list[tuple] | None):
    """Points ``known_savers.ledger_path`` at a scratch file under
    ``tmp_path`` instead of the real ``~/.tokensave`` -- ``rows=None``
    leaves no file at all, so ``read_ledger`` returns ``None`` (the
    "no ledger on this machine" case)."""
    ledger_file = tmp_path / ".tokensave" / "global.db"
    if rows is not None:
        _write_ledger(ledger_file, rows)
    monkeypatch.setattr(known_savers, "ledger_path", lambda home=None: ledger_file)


def test_known_savers_no_data_when_tokensave_was_not_used(tmp_path):
    result = qa.run("known-savers", _ctx(tmp_path))
    assert result["status"] == "no_data"
    assert result["summary"] == "tokensave wasn't used over the last 14 days."


def test_known_savers_ok_when_it_nets_positive(tmp_path, monkeypatch):
    model = _savers_model(carry_rows=[_tokensave_carry_row(cost=1.0, tokens=1000)])
    _patch_ledger(monkeypatch, tmp_path, [(1, "any", "tokensave_search", 3000, 500),
                                           (2, "any", "tokensave_search", 2000, 300)])
    result = qa.run("known-savers", _ctx(tmp_path, model=model))
    assert result["status"] == "ok"
    assert "tokensave says it saved 4,200 tokens in 2 calls over the last 14 days." in result["summary"]
    assert "Net: 4.00 USD" in result["summary"]
    assert "0 of 2 calls cost more than they replaced." in result["summary"]
    assert result["fixes"] == []


def test_known_savers_act_offers_a_scope_fix_when_redirects_drive_the_loss(tmp_path, monkeypatch):
    model = _savers_model(
        carry_rows=[_tokensave_carry_row(cost=1.0, tokens=1000)],
        redirect_rows=[_redirect_row(replies=4, cost=2.0)],
    )
    _patch_ledger(monkeypatch, tmp_path, [(1, "any", "tokensave_search", 1000, 900)])
    result = qa.run("known-savers", _ctx(tmp_path, model=model))
    assert result["status"] == "act"
    assert "Net: -2.00 USD" in result["summary"]
    [fix] = result["fixes"]
    assert fix["title"] == "Go straight to tokensave's own tools"
    assert fix["command"] is None and fix["note"] == "scope"
    assert "tokensave_context" in fix["prompt"] and fix["prompt"].endswith(qa.PROMPT_SCOPE)


def test_known_savers_act_offers_an_uninstall_fix_when_its_own_cost_drives_the_loss(tmp_path, monkeypatch):
    model = _savers_model(carry_rows=[_tokensave_carry_row(cost=10.0, tokens=1000)])
    _patch_ledger(monkeypatch, tmp_path, [(1, "any", "tokensave_search", 500, 100)])
    result = qa.run("known-savers", _ctx(tmp_path, model=model))
    assert result["status"] == "act"
    assert "Net: -5.00 USD" in result["summary"]
    [fix] = result["fixes"]
    assert fix["title"] == "Turn tokensave off"
    assert fix["command"] == "tokensave uninstall --agent claude"
    assert fix["command_warning"]


def test_known_savers_shows_only_known_costs_with_no_ledger_on_this_machine(tmp_path, monkeypatch):
    model = _savers_model(carry_rows=[_tokensave_carry_row(cost=1.0, tokens=1000)])
    _patch_ledger(monkeypatch, tmp_path, None)
    result = qa.run("known-savers", _ctx(tmp_path, model=model))
    assert result["status"] == "ok"
    assert "its ledger isn't on this machine" in result["summary"]
    assert result["fixes"] == []


def test_known_savers_no_ledger_still_acts_on_a_material_redirect_cost(tmp_path, monkeypatch):
    model = _savers_model(
        carry_rows=[_tokensave_carry_row(cost=1.0, tokens=1000)],
        redirect_rows=[_redirect_row(replies=3, cost=0.5)],
    )
    _patch_ledger(monkeypatch, tmp_path, None)
    result = qa.run("known-savers", _ctx(tmp_path, model=model))
    assert result["status"] == "act"
    [fix] = result["fixes"]
    assert fix["title"] == "Go straight to tokensave's own tools"


def test_rec_fixes_drop_an_informational_fix_with_nothing_to_paste_or_run():
    from types import SimpleNamespace

    empty = {"key": None, "prompt": "", "command": None, "explainer": [], "note": "none"}
    prompt = {"key": None, "prompt": "Do this.", "command": None, "explainer": []}
    rec = SimpleNamespace(title="Most of your cost is re-reading the conversation", fixes=[empty, prompt])
    assert [f["prompt"] for f in qa._rec_fixes([rec])] == ["Do this."]


def test_tools_offers_the_baseline_fix_when_no_subagent_started(tmp_path):
    # baseline-bloat is about main sessions: no subagent run mustn't hide it.
    rec = Recommendation(id="baseline-bloat", severity="advice", category="settings", title="Big start",
                         action="Turn off MCP servers.", lever=None)
    empty = NS(sections=[], recommendations=[rec])
    result = qa.run("tools", _ctx(tmp_path, empty))
    assert result["status"] == "act" and "No subagents started" in result["summary"]
    assert result["fixes"] and "~/.claude.json" in result["fixes"][0]["prompt"]
    assert qa.run("tools", _ctx(tmp_path, NS(sections=[], recommendations=[])))["status"] == "no_data"

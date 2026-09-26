"""Quick actions: one question per token lever, each answered from the
report's own tables for the window, with the evidence as a small table
and fixes in the :mod:`fixes` shape (explainer, prompt, and a dry-run
``apply`` command where one applies).

Unlike a recommendation, a check always answers, including "nothing to
do here" (``status`` "ok") and "not enough data" ("no_data"). Checks
reuse the recommendations, the goal drafts (:mod:`profiles.goals`), the
CLAUDE.md and skills reviews and the quality signals (:mod:`quality`)
rather than adding rules of their own, so a check and a recommendation
never disagree. The quality check is the one with thresholds of its own
(:data:`STRUGGLE_PCT`): no recommendation covers how well work went.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import capture_catalogue, carry, discovery, habits, model_gate, model_swap, pages, quality, whatif
from .compaction_sim import CompactionSimThresholds
from .fixes import PROMPT_RESTART, RESTART_NOTE, build_fix, build_fixes
from .model import Recommendation, SettingChange
from .profiles import goals
from .recommend import _BUILTIN_AGENT_TYPES, _NOT_OVERRIDABLE
from .units import Units

TOP = whatif.TOP


@dataclass(slots=True)
class Context:
    model: object
    units: Units
    period: str
    config_dir: Path
    effective: dict
    effective_agents: dict
    #: Recommendations ignored on the dashboard (``ignores.py``): the
    #: drafted fixes leave their changes out.
    skip_keys: frozenset = frozenset()


@dataclass(frozen=True, slots=True)
class Check:
    id: str
    question: str
    why: str
    run: Callable[[Context], dict]
    #: Additive: the recommendation rule ids (recommend.recommend's own
    #: ``Recommendation.id`` values, plus the other rule modules it folds
    #: in) this check draws its fixes or evidence tables from -- empty
    #: for a check with no rule behind it (e.g. "skills", "quality",
    #: which read their own tables directly).
    rule_ids: tuple[str, ...] = ()


# -- helpers ---------------------------------------------------------------


def _money(ctx: Context, usd, *, period: bool = False, prefix: str = "") -> str:
    value = whatif._num(usd)
    amount = ctx.units.money(value, period=ctx.period if period else "") if value else None
    if amount is None:
        return "none"
    # UX-2: Amount.phrase avoids "about about X% of your weekly usage
    # limit" when a caller's own sentence also says "about" -- a
    # subscription's share text already opens with it.
    return amount.phrase(prefix)


def _cell(ctx: Context, usd) -> str:
    """An amount for a table cell: the short form (Units.money_cell), so a
    column doesn't wrap into a tall stack of words."""
    value = whatif._num(usd)
    return ctx.units.money_cell(value) if value else "none"


def _short(text: str, limit: int = 160) -> str:
    """``text`` cut at a word to about ``limit`` characters, for a table
    cell; the full text is on the Context files tab."""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(",;:-") + "…"


def _who(agent) -> str:
    return "Main session" if agent in (None, TOP) else str(agent)


def _pct(value) -> str:
    number = whatif._num(value)
    return f"{number:.0f}%" if number is not None else ""


def _table(columns: list[tuple[str, str]], rows: list[list]) -> dict | None:
    if not rows:
        return None
    return {"columns": [{"key": k, "label": label} for k, label in columns], "rows": rows}


def _result(status: str, summary: str, *, table=None, fixes=None, tips=None) -> dict:
    return {"status": status, "summary": summary, "table": table, "fixes": fixes or [], "tips": tips or []}


def _recommendations(ctx: Context, ids: set[str]) -> list:
    return [rec for rec in getattr(ctx.model, "recommendations", ()) or () if rec.id in ids]


def _rec_fixes(recs) -> list[dict]:
    out = []
    for rec in recs:
        for fix in (getattr(rec, "fixes", None) or build_fixes(rec)):
            out.append({**fix, "title": fix.get("title") or rec.title})
    return out


def _candidate_fix(ctx: Context, candidate: dict, title: str) -> dict:
    """A goal candidate as a fix: the same explainer, prompt and command
    a recommendation's change gets."""
    agent = candidate["agent"]
    estimate = candidate.get("estimate") or {}
    fields = ctx.effective_agents.get(agent) if agent else None
    change = SettingChange(
        target="agent" if agent else "settings",
        key=candidate["key"],
        agent=agent,
        value=candidate["value"],
        current=candidate["now"],
        new_agent_file=bool(agent) and agent in _BUILTIN_AGENT_TYPES and agent not in ctx.effective_agents,
        # A project's own agent file, not one in ~/.claude/agents.
        scope="repo" if isinstance(fields, dict) and fields.get("source") == "project" else "",
    )
    effect = estimate.get("effect_text") or ""
    rec = Recommendation(
        id="quick-action",
        title=title,
        scope="user",
        why=candidate["evidence"],
        estimated_saving=f"{effect}." if effect and estimate.get("saving_usd") is not None else "",
        saving_basis=estimate.get("basis") or "",
        changes=[change],
    )
    return {**build_fix(rec, change), "title": title}


def _merge_fixes(*groups: list[dict]) -> list[dict]:
    """Every fix once: the first for a (key, agent) wins, and fixes with
    no key (prompt-only advice) are kept by their prompt."""
    seen: set = set()
    out = []
    for group in groups:
        for fix in group:
            marker = (fix.get("key"), fix.get("agent")) if fix.get("key") else ("prompt", fix.get("prompt"))
            if marker in seen:
                continue
            seen.add(marker)
            out.append(fix)
    return out


def _goal(ctx: Context, goal_id: str) -> dict:
    return goals.draft(
        goal_id,
        ctx.model,
        ctx.units,
        effective=ctx.effective,
        effective_agents=ctx.effective_agents,
        period=ctx.period,
        skip_keys=ctx.skip_keys,
    )


def _goal_fixes(ctx: Context, draft: dict, title: Callable[[dict], str]) -> list[dict]:
    return [_candidate_fix(ctx, c, title(c)) for c in draft["candidates"]]


# -- checks ------------------------------------------------------------------


def _models(ctx: Context) -> dict:
    tables = whatif._Tables(ctx.model)
    rows = [r for r in tables.rows("model_swap", "model_swap_by_agent_type") if whatif._num(r.get("observed_cost"))]
    if not rows:
        return _result("no_data", "No priced replies in this window.")
    rows.sort(key=lambda r: -(whatif._num(r.get("observed_cost")) or 0))
    table = _table(
        [("agent", "Agent"), ("model", "Model used"), ("cost", "Cost"), ("set_by", "Model set by"),
         ("cheaper", "Cheapest alternative"), ("saving", "Would save")],
        [
            [_who(r.get("agent_type")), r.get("observed_model") or "", _cell(ctx, r.get("observed_cost")),
             _model_set_by(r), r.get("best_cheaper_alternative_model") or "none cheaper",
             f"{_cell(ctx, r.get('saving_usd'))}, {_pct(r.get('saving_pct'))} less" if whatif._num(r.get("saving_usd"))
             else ""]
            for r in rows
        ],
    )
    draft = _goal(ctx, "models")
    fixes = _goal_fixes(ctx, draft, lambda c: f"{_who(c['agent'])}: use {c['value']}")
    left_out = _models_left_out(ctx, rows)
    tips = left_out + _models_set_elsewhere(rows)
    if not fixes:
        return _result(
            "ok",
            "Every agent is already on the cheapest model that priced lower by a useful margin"
            + (", or did worse on it." if left_out else "."),
            table=table,
            tips=tips,
        )
    top = max(draft["candidates"], key=lambda c: (c["estimate"] or {}).get("saving_usd") or 0)
    return _result(
        "act",
        f"{len(fixes)} model change{'s' if len(fixes) != 1 else ''} would have cost less. The largest: {_who(top['agent'])} on "
        f"{top['value']}, {top['estimate']['effect_text'][:1].lower()}{top['estimate']['effect_text'][1:]}. A "
        "cheaper model may need more replies for hard work, so try it on one agent first.",
        table=table,
        fixes=fixes,
        tips=tips,
    )


def _count(value) -> int:
    return int(whatif._num(value) or 0)


def _model_set_by(row: dict) -> str:
    """Where each run's model came from, per model-swap row: the settings
    for the main session; for a subagent, its agent file, workflow scripts
    and a model named when the run started, with how many runs each."""
    if row.get("agent_type") == "top-level":
        return "settings"
    parts = [
        f"{words} ({count:,})"
        for words, count in (
            ("its agent file", _count(row.get("lever_runs"))),
            ("workflow scripts", _count(row.get("workflow_runs"))),
            ("when started", _count(row.get("spawn_model_runs"))),
        )
        if count
    ]
    if parts:
        return ", ".join(parts)
    return "Claude Code" if "lever_runs" in row else ""


def _models_set_elsewhere(rows: list[dict]) -> list[dict]:
    """One tip when a workflow script or a model named at spawn set some
    subagent runs' model: an agent file change doesn't reach those, and
    the savings above leave them out."""
    subagents = [r for r in rows if r.get("agent_type") != "top-level"]
    workflow = sum(_count(r.get("workflow_runs")) for r in subagents)
    spawn = sum(_count(r.get("spawn_model_runs")) for r in subagents)
    sentence = model_swap.set_elsewhere_sentence(workflow, spawn)
    if not sentence:
        return []
    return [{
        "title": "Some runs' model isn't set by an agent file",
        "text": "An agent file's model line only decides the runs started without a model of their own." + sentence
        + " Change those where they start.",
    }]


def _models_left_out(ctx: Context, rows: list[dict]) -> list[dict]:
    """A tip for each cheaper model the models goal skipped because the
    quality check found that agent did worse on it, its runs on it were
    often retried on a larger one, or Claude reported its work needed a
    larger model (metrics capture)."""
    tables = whatif._Tables(ctx.model)
    worse, retried, unfit = model_gate.raw(tables)
    tips = []
    for row in rows:
        agent, best = row.get("agent_type"), row.get("best_cheaper_alternative_model")
        if not best or (whatif._num(row.get("saving_pct")) or 0.0) < goals.MIN_SHARE_PCT:
            continue
        key = (agent, goals._alias(best))
        if key in worse:
            setup = worse[key]
            reason = (
                f"on {setup.get('model')} at effort {setup.get('effort')} it did worse than on "
                f"{setup.get('compared_model')} at effort {setup.get('compared_effort')}: {setup.get('difference')}"
            )
        elif key in retried:
            reason = retried[key]["reason"] + "."
        elif agent in unfit:
            reason = unfit[agent] + "."
        else:
            continue
        tips.append({
            "title": f"{_who(agent)}: {goals._alias(best)} not suggested",
            "text": f"It would price {_pct(row.get('saving_pct'))} lower, but {reason}",
        })
    return tips


def _effort(ctx: Context) -> dict:
    tables = whatif._Tables(ctx.model)
    rows = [r for r in tables.rows("agents", "topology_effort_by_agent_type") if whatif._num(r.get("output_tokens"))]
    if not rows:
        return _result("no_data", "No thinking recorded in this window.")
    rows.sort(key=lambda r: -(whatif._num(r.get("thinking_share")) or 0))
    table = _table(
        [("agent", "Agent"), ("output", "Output tokens"), ("thinking", "Of which thinking")],
        [[_who(r.get("agent_type")), f"{int(whatif._num(r.get('output_tokens')) or 0):,}",
          _pct(r.get("thinking_share"))] for r in rows],
    )
    draft = _goal(ctx, "thinking")
    fixes = _merge_fixes(
        _rec_fixes(_recommendations(ctx, {"effort-mismatch"})),
        _goal_fixes(ctx, draft, lambda c: f"{_who(c['agent'])}: effort {c['value']}"),
    )
    if not fixes:
        return _result(
            "ok", f"No agent spends more than {goals.THINKING_PCT:.0f}% of its output thinking.", table=table
        )
    return _result(
        "act",
        f"{len(fixes)} of your agents or sessions spent more than {goals.THINKING_PCT:.0f}% of their output "
        "thinking. Thinking is billed as output; a lower effort thinks less, but how much less isn't measured, so "
        "check {{page:changes}} after a few sessions.",
        table=table,
        fixes=fixes,
    )


#: The replay keeps every real summary, so it can only test smaller windows.
_LARGER_WINDOW = (
    "A larger window can't be tested: the replay keeps every real summary, so points above yours cost what your "
    "sessions did."
)


def _compaction(ctx: Context) -> dict:
    tables = whatif._Tables(ctx.model)
    rows = tables.rows("compaction_sim", "compaction_sim_by_window")
    if not rows:
        return _result("no_data", "No sessions long enough to replay in this window.")
    table = _table(
        [("window", "Summarise at (tokens)"), ("summaries", "Summaries per session"), ("cost", "Cost"),
         ("change", "Against your sessions as they ran")],
        [[r.get("window"), f"{whatif._num(r.get('compactions_per_session')) or 0:.1f}", _cell(ctx, r.get("cost")),
          _pct(r.get("delta_pct"))] for r in rows],
    )
    draft = _goal(ctx, "compaction")
    fixes = _goal_fixes(ctx, draft, lambda c: f"Summarise at {c['value']:,} tokens")
    limit = CompactionSimThresholds().max_compactions_per_session
    if not fixes and not any(
        str(r.get("window")) != "none" and (whatif._num(r.get("compactions_per_session")) or 0.0) <= limit
        for r in rows
    ):
        # Real summaries are kept under every window, so when they alone
        # pass the limit no window replayed can meet it.
        observed = next((r for r in rows if str(r.get("window")) == "none"), {})
        return _result(
            "ok",
            f"Your sessions summarised about {whatif._num(observed.get('compactions_per_session')) or 0:.1f} times "
            f"each as they ran, more than the {limit:g} a session a suggested point may reach, so no smaller window "
            f"is suggested. {_LARGER_WINDOW}",
            table=table,
        )
    if not fixes:
        current = (ctx.effective or {}).get("autoCompactWindow")
        if current and any(str(r.get("window")).replace(",", "") == str(current) for r in rows):
            # The last column is against the sessions as they ran, most
            # perhaps before this setting, so its own row can read cheaper.
            return _result(
                "ok",
                f"You already summarise at {int(current):,} tokens, within a few percent of the cheapest point "
                f"replayed that summarises at most {limit:g} times a session. The last column compares each point "
                f"with your sessions as they ran, not with that setting. {_LARGER_WINDOW}",
                table=table,
            )
        return _result(
            "ok",
            f"Your current summary point is within a few percent of the cheapest one replayed. {_LARGER_WINDOW}",
            table=table,
        )
    [candidate] = draft["candidates"]
    return _result(
        "act",
        f"Summarising at {candidate['value']:,} tokens {candidate['estimate']['effect_text'][:1].lower()}"
        f"{candidate['estimate']['effect_text'][1:]}. Earlier summaries make each reply cheaper but drop detail.",
        table=table,
        fixes=fixes,
    )


def _cache(ctx: Context) -> dict:
    tables = whatif._Tables(ctx.model)
    rows = [r for r in tables.rows("ttl", "ttl_by_agent_type") if whatif._num(r.get("cost_observed"))]
    if not rows:
        return _result("no_data", "No cache writes in this window.")
    table = _table(
        [("agent", "Agent"), ("gaps", "Pauses over 5 minutes"), ("now", "Cost now"), ("m5", "All 5 minutes"),
         ("h1", "All 1 hour"), ("best", "Cheaper")],
        [[_who(r.get("agent_type")), r.get("gaps_over_5m"), _cell(ctx, r.get("cost_observed")),
          _cell(ctx, r.get("cost_all_5m")), _cell(ctx, r.get("cost_all_1h")), r.get("best_policy") or ""]
         for r in rows],
    )
    draft = _goal(ctx, "cache")
    subagents = _goal(ctx, "subagents")
    per_agent = [c for c in subagents["candidates"] if c["key"] == "experimental.cacheTtl"]
    fixes = _merge_fixes(
        _goal_fixes(ctx, draft, lambda c: f"{goals.LEVER_LABELS.get(c['key'], c['key'])}: {c['value']}"),
        [_candidate_fix(ctx, c, f"{c['agent']}: cache lifetime {c['value']}") for c in per_agent],
        _rec_fixes(_recommendations(ctx, {"ttl-switch"})),
    )
    if not fixes:
        return _result("ok", "Your cache lifetimes are within a few percent of the cheapest replayed.", table=table)
    return _result(
        "act",
        f"{len(fixes)} cache lifetime change{'s' if len(fixes) != 1 else ''} would have cost less. A 1-hour cache "
        "costs more to write but survives longer pauses; a 5-minute one is cheaper when you reply quickly.",
        table=table,
        fixes=fixes,
    )


def _tools(ctx: Context) -> dict:
    tables = whatif._Tables(ctx.model)
    rows = tables.rows("agent_startup", "agent_startup_unused")
    if not rows:
        return _result("no_data", "No subagents started in this window.")
    table = _table(
        [("agent", "Agent"), ("spawns", "Starts"), ("mcp", "Offered MCP / used it"),
         ("skills", "Listed skills / used one")],
        [[_who(r.get("agent_type")), r.get("spawns"),
          f"{r.get('mcp_offered_spawns') or 0} / {r.get('mcp_used_spawns') or 0}",
          f"{r.get('skills_listed_spawns') or 0} / {r.get('skills_used_spawns') or 0}"] for r in rows],
    )
    ids = {"spawn-unused-mcp", "spawn-unused-skills", "spawn-read-only-tools", "baseline-bloat"}
    fixes = _rec_fixes(_recommendations(ctx, ids))
    if not fixes:
        return _result("ok", "Every agent uses the tools, MCP servers and skills it's given, or they cost little.",
                       table=table)
    return _result(
        "act",
        "Some agents start with MCP servers, skills or tools they never use, and each is sent at every start.",
        table=table,
        fixes=fixes,
    )


def _skills(ctx: Context) -> dict:
    from . import skills_review

    data = skills_review.review(
        ctx.config_dir, getattr(ctx.model, "context_files", None) or {}, ctx.units, ctx.period,
    )
    rows = data["skills"]
    if not any(r["status"] != "not listed" for r in rows):
        return _result("no_data", f"No skill listing was recorded {ctx.period}.")
    unused = sorted((r for r in rows if r["status"] == "unused"), key=lambda r: -r["listing_cost_usd"])
    kept = [r for r in rows if r["status"] == "needed by a tool"]
    table = _table(
        [("name", "Skill"), ("source", "From"), ("description", "What it is"), ("cost", "Listing cost")],
        [[r["name"], r["source_label"], _short(r["description"]), _cell(ctx, r["listing_cost_usd"])]
         for r in unused[:20]],
    )
    tips = [
        {
            "title": f"{r['name']}: needed by the {r['needed_by']} tool",
            "text": f"Claude never used it {ctx.period}, but Claude Code's {r['needed_by']} tool tells Claude to "
            "load it, so it isn't offered for hiding. Listing it by name only drops its description and keeps "
            "it loadable.",
        }
        for r in kept
    ]
    tips += _skill_timing_tips(ctx)
    if not unused:
        return _result("ok", f"Claude used every listed skill it can do without {ctx.period}.", tips=tips)
    return _result(
        "act",
        f"{len(unused)} skills were listed to Claude at every session and subagent start but never used "
        f"{ctx.period}. Hiding them from Claude keeps them available to you as /name.",
        table=table,
        fixes=data["fixes"]
        + [fix for r in unused[:5] for fix in r["fixes"]]
        + [{**fix, "title": f"{r['name']}: list it by name only"} for r in kept for fix in r["fixes"]],
        tips=tips,
    )


#: Times a skill must be loaded late, or said not to be needed, for a tip.
MIN_SKILL_TIMING = 2


def _skill_timing_tips(ctx: Context) -> list[dict]:
    """Skills Claude reached for late, or said weren't needed, from the
    Work habits section's ``habits_skills`` (metrics capture)."""
    tips = []
    for row in whatif._Tables(ctx.model).rows("habits", "habits_skills"):
        name = row.get("skill")
        late = int(whatif._num(row.get("late")) or 0)
        unneeded = int(whatif._num(row.get("unneeded")) or 0)
        if late >= MIN_SKILL_TIMING:
            before = (
                _money(ctx, row.get("before"), prefix="about ") if whatif._num(row.get("before")) else ""
            )
            tips.append({
                "title": f"{name}: run it at the start",
                "text": f"Claude loaded it after three or more replies {late} times"
                + (f", with {before} already spent each time" if before else "")
                + f". Start that kind of task with /{name} so the work follows it from the first reply.",
            })
        if unneeded >= MIN_SKILL_TIMING:
            tips.append({
                "title": f"{name}: often not needed",
                "text": f"Claude said it wasn't needed {unneeded} times. Setting disable-model-invocation: true in "
                "its SKILL.md keeps Claude from loading it by itself; you can still run it as /" + str(name) + ".",
            })
    return tips


def _claude_md(ctx: Context) -> dict:
    from . import claude_md_review

    review = claude_md_review.build_review(
        ctx.config_dir, getattr(ctx.model, "context_files", None) or {},
    )
    pairs = sorted(
        ((item, claude_md_review.file_summary(item, ctx.units, ctx.period)) for item in review.files),
        key=lambda pair: -pair[1]["cost_usd"],
    )
    if not pairs:
        return _result("no_data", "No CLAUDE.md files found.")
    summaries = [summary for _item, summary in pairs]
    table = _table(
        [("file", "File"), ("tokens", "Tokens"), ("sent", "Sent to"), ("cost", "Cost"), ("findings", "Findings")],
        [[s["path"], f"{s['tokens']:,}", s["reach_text"], _cell(ctx, s["cost_usd"]), len(s["findings"])]
         for s in summaries[:10]],
    )
    if not any(summary["seen"] for summary in summaries):
        return _result(
            "no_data",
            f"None of your CLAUDE.md files was seen in a session {ctx.period}, so how often each is sent isn't known.",
            table=table,
        )
    detail = claude_md_review.file_detail(pairs[0][0], ctx.units, ctx.period)
    fixes = _merge_fixes(
        detail["fixes"], _rec_fixes(_recommendations(ctx, {"spawn-claude-md", "spawn-shared-claude-md"}))
    )
    if not fixes or not summaries[0]["cost_usd"]:
        return _result("ok", "Your CLAUDE.md files are small or rarely sent.", table=table)
    return _result(
        "act",
        f"{summaries[0]['path']} costs most: {summaries[0]['tokens']:,} tokens sent to {summaries[0]['reach_text']}, "
        f"{summaries[0]['cost_text']}. Context files (or claudeglass review claude-md) shows every file's "
        "sections.",
        table=table,
        fixes=fixes,
    )


#: For each of ``carry.OUTPUT_CAPS``: (Claude Code's default, what it is).
_OUTPUT_CAP_TEXT = {
    "BASH_MAX_OUTPUT_LENGTH": (
        "30,000 characters",
        "The most characters of a shell command's output Claude Code keeps; the middle of longer output is cut.",
    ),
    "MAX_MCP_OUTPUT_TOKENS": ("25,000 tokens", "The most tokens of one MCP tool result Claude Code keeps."),
}
#: A cap is offered only when it would have saved at least this share of
#: what carrying the results it covers cost; below that, most of them are
#: already under the cap and cutting the rest isn't worth the lost output.
CAP_MIN_SAVING_SHARE = 0.10


def _env_fix(name: str, value: str, default: str, what: str, effect: str) -> dict:
    return {
        "key": name,
        "agent": None,
        "title": f"Cap {name} at {value}",
        "explainer": [
            ["What this setting controls", what],
            ["Now and after", f"Now: Claude Code's default ({default}) unless you set it. After: {value}."],
            ["Where and who it affects", "The env block of ~/.claude/settings.json: every session, in every project."],
            ["Expected effect", effect],
            ["Trade-off", "Long output is cut, which can hide an error at the end. Claude then re-runs a narrower "
             "command, which costs a reply."],
            ["How to undo it", f"Remove {name} from the env block (Claude Code shows the change before saving)."],
        ],
        "command": None,
        "command_warning": "",
        "prompt": (
            f"In ~/.claude/settings.json, add \"{name}\": \"{value}\" to the \"env\" object (create it if it's "
            "missing), keeping every other entry. Show me the diff before saving. "
            "Claude Code will ask my permission to edit files under .claude; that is expected. " + PROMPT_RESTART
        ),
    }


def _tool_output(ctx: Context) -> dict:
    tables = whatif._Tables(ctx.model)
    rows = sorted(tables.rows("carry", "carry_by_tool"), key=lambda r: -(whatif._num(r.get("carry_cost_usd")) or 0))
    if not rows:
        return _result("no_data", "No tool results in this window.")
    total = sum(whatif._num(r.get("carry_cost_usd")) or 0 for r in rows)
    table = _table(
        [("tool", "Tool"), ("results", "Results"), ("tokens", "Tokens returned"), ("turns", "Replies each stays for"),
         ("cost", "Cost of carrying them")],
        [[r.get("key"), f"{int(whatif._num(r.get('result_count')) or 0):,}",
          f"{int(whatif._num(r.get('tokens_entered')) or 0):,}", f"{whatif._num(r.get('mean_turns_carried')) or 0:.0f}",
          _cell(ctx, r.get("carry_cost_usd"))] for r in rows[:10]],
    )
    fixes, tips = [], []
    savings = {r.get("setting"): r for r in tables.rows("carry", "carry_output_cap_savings")}
    for cap in carry.OUTPUT_CAPS:
        default, what = _OUTPUT_CAP_TEXT[cap.setting]
        cost = sum(whatif._num(r.get("carry_cost_usd")) or 0 for r in rows if cap.covers(str(r.get("key") or "")))
        if not total or cost / total < 0.05:
            continue
        row = savings.get(cap.setting)
        if row is None:
            # A report from before the saving was worked out.
            fixes.append(_env_fix(
                cap.setting, cap.value, default, what,
                f"Results from these tools stayed in context and cost {_money(ctx, cost, period=True)}. Capping "
                "them cuts that for the largest results; the saving isn't estimated on its own.",
            ))
            continue
        saved = whatif._num(row.get("usd_saved")) or 0.0
        cut = f"{int(whatif._num(row.get('results_affected')) or 0):,} of {int(whatif._num(row.get('results')) or 0):,}"
        if saved >= CAP_MIN_SAVING_SHARE * cost:
            fixes.append(_env_fix(
                cap.setting, cap.value, default, what,
                f"At {cap.value}, it would have cut {cut} results and saved up to "
                f"{_money(ctx, saved, period=True)} of the {_money(ctx, cost, period=True)} they cost to keep in "
                "context. Up to: results from one reply are counted together.",
            ))
        else:
            tips.append({
                "title": f"{cap.setting} at {cap.value} would save little",
                "text": f"It would have cut {cut} results and saved {_money(ctx, saved, period=True)} of the "
                f"{_money(ctx, cost, period=True)} they cost to keep in context: most of that output is already "
                "shorter, and cutting the rest can hide an error Claude then re-runs a command to see.",
            })
    read = next((r for r in rows if r.get("key") == "Read"), None)
    if read and total and (whatif._num(read.get("carry_cost_usd")) or 0) / total >= 0.1:
        tips.append({
            "title": "Point Claude at the part of a file you mean",
            "text": "File reads are the largest thing carried in your context. Naming the function or line range "
                    "(\"read handle_request in api.py\") keeps whole files out of every later reply.",
        })
    loops = next((r for r in tables.rows("habits", "habits_tool_output") if r.get("tool") == "loops"), None)
    if loops and (whatif._num(loops.get("loops")) or 0) >= 1:
        tips.append({
            "title": "Stop a failing command sooner",
            "text": f"{int(whatif._num(loops.get('loops')) or 0)} commands failed three or more times within one "
            f"message, costing {_money(ctx, loops.get('cost'), period=True, prefix='about ')}. Ask Claude to stop after two failed tries "
            "at the same command and tell you what it saw.",
        })
    if not fixes and not tips:
        return _result("ok", "No one tool's output dominates your context.", table=table)
    if not fixes and all(t["title"].endswith("would save little") for t in tips):
        return _result("ok", "No output cap would save much: most results are already short.", table=table,
                       tips=tips)
    return _result(
        "act",
        f"Carrying tool results in context cost {_money(ctx, total, period=True)}. "
        "Every result is re-read on every later reply until a summary drops it.",
        table=table,
        fixes=fixes,
        tips=tips,
    )


_HOOK_RECS = {"hook-failures", "hook-block-resent", "hook-context-carry"}


def _hooks(ctx: Context) -> dict:
    tables = whatif._Tables(ctx.model)
    rows = tables.rows("hooks", "hooks_by_script")
    if not rows:
        return _result("no_data", "No hook of yours left a record in this window.")

    def count(row, key) -> int:
        return int(whatif._num(row.get(key)) or 0)

    table = _table(
        [("hook", "Hook"), ("failed", "Failed runs"), ("cause", "Why it failed"), ("blocks", "Calls blocked"),
         ("resent", "Sent again unchanged"), ("context", "Context added"), ("cost", "Cost of its context and blocks")],
        [[r.get("hook"), f"{count(r, 'failed'):,}", str(r.get("cause") or "")[:1].upper() + str(r.get("cause") or "")[1:],
          f"{count(r, 'blocks'):,}", f"{count(r, 'resent'):,}", f"{count(r, 'context_tokens'):,} tokens",
          _cell(ctx, (whatif._num(r.get("carry_usd")) or 0) + (whatif._num(r.get("block_usd")) or 0))]
         for r in rows[:10]],
    )
    recs = _recommendations(ctx, _HOOK_RECS)
    if not recs:
        return _result("ok", "Your hooks work, and none costs much in kept context or blocked calls.", table=table)
    failing = [r for r in rows if count(r, "failed")]
    if any(rec.id == "hook-failures" for rec in recs):
        summary = (
            f"{len(failing)} of your hooks failed {sum(count(r, 'failed') for r in failing):,} times, so they didn't "
            "do their job."
        )
    else:
        summary = "Some of your hooks cost more than they need to, in kept context or in replies spent on blocks."
    return _result("act", summary, table=table, fixes=_rec_fixes(recs))


def _tool_search(ctx: Context) -> dict:
    tables = whatif._Tables(ctx.model)
    summary = tables.rows("tool_search", "tool_search_summary")
    row = summary[0] if summary else {}

    def count(r, key) -> int:
        return int(round(whatif._num(r.get(key)) or 0))

    replies, most, mcp = count(row, "replies"), count(row, "most_deferred"), count(row, "most_deferred_mcp")
    if not replies:
        return _result("no_data", f"No reply {ctx.period} had tools deferred by tool search.")
    net = whatif._num(row.get("net_usd"))
    if net is None:
        return _result(
            "no_data",
            f"Tool search deferred up to {most:,} tools a reply {ctx.period}, but none was loaded, so their size "
            "isn't known.",
        )
    table = _table(
        [("server", "MCP server"), ("deferred", "Most tools deferred"), ("kept", "Kept out of each reply"),
         ("saving", "Saved")],
        [["Claude Code's own tools" if r.get("server") == "built-in" else r.get("server"),
          f"{count(r, 'most_deferred'):,}", f"{count(r, 'kept_per_reply'):,} tokens", _cell(ctx, r.get("saving_usd"))]
         for r in tables.rows("tool_search", "tool_search_by_server")[:10]],
    )
    kept = count(row, "kept_per_reply")
    if net > 0:
        text = (
            f"Tool search kept about {kept:,} tokens of tool definitions out of each reply, from up to {most:,} "
            f"deferred tools ({mcp:,} of them MCP tools). That saved {_money(ctx, net)} {ctx.period}, after the "
            "replies spent searching for tools."
        )
    else:
        text = (
            f"Tool search saved nothing {ctx.period}: the replies spent searching for tools cost more than keeping "
            f"up to {most:,} tool definitions out of each reply saved."
        )
    return _result("ok", text, table=table)


#: The most of a difference from Claude Code's own cost record, in %,
#: that stopped replies and unlogged requests don't explain, before the
#: cost-record check says ClaudeGlass's figures may be off.
_COST_RECORD_LIMIT_PCT = 5.0


def _cost_record(ctx: Context) -> dict:
    tables = whatif._Tables(ctx.model)
    summary = tables.rows("cost_record", "cost_record_summary")
    row = summary[0] if summary else {}
    sessions = int(round(whatif._num(row.get("sessions")) or 0))
    if not sessions:
        return _result(
            "no_data",
            f"No session {ctx.period} has Claude Code's own cost record to check against. Only some Claude Code "
            "versions write one.",
        )
    difference = whatif._num(row.get("difference_pct")) or 0.0
    unexplained = whatif._num(row.get("unexplained_pct")) or 0.0
    worst = whatif._num(row.get("worst_unexplained_pct")) or 0.0
    table = _table(
        [("session", "Session"), ("recorded", "Recorded"), ("claude_code", "Claude Code's own"),
         ("claudeglass", "ClaudeGlass"), ("left", "Left unexplained")],
        [[str(r.get("session_id") or "")[:8], r.get("as_of"), _cell(ctx, r.get("cc_usd")),
          _cell(ctx, r.get("local_usd")), f"{whatif._num(r.get('unexplained_pct')) or 0.0:+.1f}%"]
         for r in tables.rows("cost_record", "cost_record_sessions")[:10]],
    )
    plural = "s" if sessions != 1 else ""
    if abs(unexplained) > _COST_RECORD_LIMIT_PCT or worst > _COST_RECORD_LIMIT_PCT:
        return _result(
            "act",
            f"ClaudeGlass's cost differs from Claude Code's own record by {unexplained:+.1f}% over {sessions:,} "
            f"session{plural}, beyond what stopped replies and unlogged requests explain. Its figures may be off, "
            "so please report it.",
            table=table,
        )
    return _result(
        "ok",
        f"ClaudeGlass's cost is within {abs(difference):.1f}% of Claude Code's own record over {sessions:,} "
        f"session{plural}. Once stopped replies and unlogged requests are taken out, {abs(unexplained):.1f}% is "
        "left.",
        table=table,
    )


_HABIT_RECS = {
    "batch-instructions", "long-tool-waits", "notification-invalidation", "agent-report-size", "spawn-task-prompt",
    "cache-read-dominance", "limit-pressure", "long-context-share", "subagent-volume", "discovery-share",
}


def _habits(ctx: Context) -> dict:
    tables = whatif._Tables(ctx.model)
    causes = [r for r in tables.rows("waste", "waste_by_cause") if whatif._num(r.get("turns"))]
    table = _table(
        [("cause", "Replies that went nowhere"), ("turns", "Replies"), ("cost", "Cost"), ("lever", "What helps")],
        [[r.get("cause"), r.get("turns"), _cell(ctx, r.get("cost_usd")), r.get("lever") or ""] for r in causes],
    )
    recs = _recommendations(ctx, _HABIT_RECS)
    tips = [{"title": rec.title, "text": rec.action or rec.why} for rec in recs]
    playbook = _playbook_tips(ctx, tables)
    tips += playbook
    fixes = _rec_fixes(recs)
    if not recs and not causes and not playbook:
        return _result("no_data", "Not enough sessions in this window.")
    if not recs and not playbook:
        return _result("ok", "No habit stands out as costing tokens.", table=table)
    ways = len(recs) + len(playbook)
    return _result(
        "act",
        f"{ways} way{'s' if ways != 1 else ''} of working cost tokens {ctx.period}. These are habits, not "
        "settings: nothing changes unless you change how you work. {{page:habits}} has the rest.",
        table=table,
        fixes=fixes,
        tips=tips,
    )


#: Habits from the Work habits playbook shown as tips.
PLAYBOOK_TIPS = 3


def _playbook_tips(ctx: Context, tables) -> list[dict]:
    """The Work habits playbook's top habits (``habits_playbook``, ranked
    by saving), each with its evidence, an example and the saving.

    UX-3, "quick actions deduped by theme": skips a habit already
    ``covered_by`` a fired recommendation (``habits.apply_covered_by``) --
    that finding is already a tip via ``_HABIT_RECS`` or shown in
    Recommendations, so repeating it here would say the same thing twice
    -- and picks at most one habit per ``theme``, so the top few tips
    aren't several variations on the same underlying issue."""
    tips = []
    seen_themes: set = set()
    for row in tables.rows("habits", "habits_playbook"):
        if len(tips) >= PLAYBOOK_TIPS:
            break
        if row.get("covered_by"):
            continue
        theme = row.get("theme")
        if theme and theme in seen_themes:
            continue
        key = row.get("habit")
        title = habits.ITEMS.get(key, ("", key))[1]
        saving = ""
        if whatif._num(row.get("saving")):
            saving = _money(ctx, row.get("saving"), prefix="About ")
            # habits.playbook_table already normalizes ``saving`` to a
            # per-week figure; say so explicitly for API billing, where
            # the phrased amount is just a dollar figure. A subscription's
            # own phrasing already says "of your weekly usage limit", so
            # adding "a week" there would read as "weekly usage limit a
            # week" (the same doubling this prefix already avoids for
            # "about").
            if saving and ctx.units.billing_mode != "subscription":
                saving = f"{saving} a week"
        tips.append({
            "title": title,
            "text": " ".join(part for part in (
                row.get("evidence") or "",
                f"Try: {row.get('example')}" if row.get("example") else "",
                f"{saving} ({row.get('source')})." if saving else "",
            ) if part),
        })
        if theme:
            seen_themes.add(theme)
    return tips


#: An agent is struggling when, over at least ``quality.MIN_RUNS`` runs,
#: one of these shares (percent) is reached.
STRUGGLE_PCT = {
    "unfinished_pct": 25.0,
    "tool_errors_pct": 5.0,
    "shell_errors_pct": 10.0,
    "corrections_pct": 5.0,
    "max_tokens_pct": 2.0,
}
_STRUGGLE_TEXT = {
    "unfinished_pct": "{v} of runs didn't finish",
    "tool_errors_pct": "{v} of tool calls failed",
    "shell_errors_pct": "{v} of shell commands failed",
    "corrections_pct": "{v} of your messages corrected Claude",
    "max_tokens_pct": "{v} of replies hit the output limit",
}


def _worse_part(difference) -> str:
    """Only the worse findings of a setup's difference text (its parts
    are joined by "; " and start with their label)."""
    parts = [p for p in str(difference or "").rstrip(".").split("; ") if p.startswith(("Worse", "Possibly worse"))]
    return "; ".join(parts) + "." if parts else str(difference or "")


#: A declared retry reason is worth a tip once an agent's retries give it
#: this many times.
MIN_DECLARED_RETRIES = 2
_REASON_TIPS = {
    "brief": (
        "{agent}: retried because the brief was unclear",
        "{n} of its retries said the instructions it was given were the problem, not {model}. Say in its task "
        "prompt what done looks like: the files, the test to pass, what not to touch. A larger model won't fix "
        "an unclear brief.",
    ),
    "tools": (
        "{agent}: retried because it lacked a tool or permission",
        "{n} of its retries said it was missing a tool or permission. Give it the tools it needs (its agent file's "
        "tools list) and allow the commands it runs, instead of running it again.",
    ),
}


def _capture_on(tables) -> bool:
    """Whether metrics capture was on when the report was built (the
    capture section's level row; off when the section is missing)."""
    level = next((r.get("value") for r in tables.rows("capture", "capture_usage") if r.get("metric") == "level"), None)
    return level not in (None, "", capture_catalogue.LEVEL_TITLES["off"])


def _capture_fix(ctx: Context, tables) -> dict | None:
    """Metrics capture at Essentials, offered when agents ran in this
    window, none wrote a ``[result: ...]`` marker, capture is off and
    CLAUDE.md doesn't ask for the markers itself. While capture is on, the
    prompt that removes the older :data:`quality.MARKER_HEADING` section
    from CLAUDE.md instead: capture asks for the same markers."""
    claude_md = discovery.claude_root() / "CLAUDE.md"
    try:
        has_section = quality.MARKER_HEADING in claude_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        has_section = False
    if _capture_on(tables):
        return _remove_markers_fix() if has_section else None
    if has_section:
        return None
    markers = {r.get("marker"): r for r in tables.rows("quality", "quality_markers")}
    result = markers.get("[result: ...]") or {}
    if not (whatif._num(result.get("of_runs")) or 0) or (whatif._num(result.get("runs")) or 0):
        return None
    metrics = capture_catalogue.level_metrics("essentials")
    main = round(len(capture_catalogue.note_text(metrics, "main")) / carry._CHARS_PER_TOKEN_APPROX)
    sub = round(len(capture_catalogue.note_text(metrics, "subagent")) / carry._CHARS_PER_TOKEN_APPROX)
    return {
        "key": None,
        "agent": None,
        "title": "Turn on metrics capture",
        "explainer": [
            ["What this adds", "Metrics capture at its Essentials level. A hook adds a short note at each session "
             "and subagent start asking Claude to end its reply to each of your messages with a one-line tag (the "
             "kind of task, how clear the ask was, how hard the work was, whether it changed course), a subagent "
             "to end its report with [result: done], [result: partial] or [result: blocked], and a rerun to say "
             "why it was run again. This tool keeps only those words, never the text around them."],
            ["Why", "Without them this check guesses: a retry on a larger model counts against the cheaper one even "
             "when the brief was the problem, and an agent that stopped half-done looks finished. With them, "
             "retries and unfinished runs are counted from what Claude said, and {{page:habits}} can rank "
             "habits by kind of task."],
            ["What it costs", f"A note of about {main} tokens at each session start and about {sub} at each "
             "subagent start, read from the prompt cache after the first reply, and about 15 output tokens per "
             "message. {{page:setup/capture}} estimates it from your own recent sessions before you turn it on, and the "
             "banner shows what it has cost while it's on."],
            ["Where and who it affects", "~/.claude/settings.json gets the hook entries (the command shows the "
             "change and asks first); this tool's own config.toml holds the level. Every session, in every "
             "project, until you turn it off; {{page:setup/capture}} can sample sessions or set an end date."],
            ["How to undo it", "claudeglass capture off stops the notes at once; claudeglass capture "
             "remove also takes the hook entries out of settings.json."],
        ],
        "command": "claudeglass capture on --level essentials --dry-run",
        "command_warning": "",
        "prompt": (
            "Run claudeglass capture on --level essentials --dry-run and tell me what it would change and what "
            "it would cost. Don't run it without --dry-run: I'll do that myself."
        ),
    }


def _remove_markers_fix() -> dict:
    tokens = round(len(quality.MARKER_LINES) / carry._CHARS_PER_TOKEN_APPROX)
    return {
        "key": None,
        "agent": None,
        "title": "Remove the older markers section from CLAUDE.md",
        "explainer": [
            ["What this changes", "Removes the \"" + quality.MARKER_HEADING + "\" section from ~/.claude/CLAUDE.md, "
             "keeping everything else."],
            ["Why", "Metrics capture asks Claude for the same [retry: ...] and [result: ...] markers, and only while "
             "it's on. With the section in place Claude is asked twice, and still asked after capture is off."],
            ["What it saves", f"About {tokens} tokens of CLAUDE.md on every session and most subagents, read from "
             "the prompt cache after the first reply."],
            ["Where and who it affects", "~/.claude/CLAUDE.md: every session, in every project."],
            ["How to undo it", "Ask Claude to add this section back to the end of ~/.claude/CLAUDE.md:\n\n"
             + quality.MARKER_LINES],
        ],
        "command": None,
        "command_warning": "",
        "prompt": (
            "Remove the \"" + quality.MARKER_HEADING + "\" section from ~/.claude/CLAUDE.md, keeping everything else. "
            "Show me the diff before saving. Claude Code will ask my permission to edit files under .claude; that "
            "is expected. " + PROMPT_RESTART
        ),
    }


def _quality(ctx: Context) -> dict:
    tables = whatif._Tables(ctx.model)
    agents = [r for r in tables.rows("quality", "quality_by_agent") if r.get("agent_type") != quality.ALL_AGENTS]
    setups = tables.rows("quality", "quality_by_setup")
    failing = tables.rows("quality", "quality_failing_tools")
    if not agents:
        return _result("no_data", "No sessions in this window.")
    struggling = []
    for row in agents:
        if (whatif._num(row.get("runs")) or 0) < quality.MIN_RUNS:
            continue
        issues = [
            _STRUGGLE_TEXT[key].format(v=_pct(row.get(key)))
            for key, limit in STRUGGLE_PCT.items()
            if (whatif._num(row.get(key)) or 0) >= limit
        ]
        if issues:
            struggling.append((row, issues))
    worse = [r for r in setups if r.get("setup_verdict") == "worse"]
    mixed = [r for r in setups if r.get("setup_verdict") == "mixed"]
    retried = quality.retried_models(tables.rows("quality", "quality_retried"))
    table = _table(
        [("agent", "Agent"), ("runs", "Runs"), ("unfinished", "Didn't finish"), ("tools", "Failed tool calls"),
         ("shell", "Failed shell commands"), ("stands_out", "What stands out")],
        [[_who(r.get("agent_type") if r.get("agent_type") != quality.MAIN else None), r.get("runs"),
          _pct(r.get("unfinished_pct")), _pct(r.get("tool_errors_pct")), _pct(r.get("shell_errors_pct")),
          "; ".join(issues)] for r, issues in struggling]
        + [[_who(r.get("agent_type") if r.get("agent_type") != quality.MAIN else None), r.get("runs"),
            _pct(r.get("unfinished_pct")), _pct(r.get("tool_errors_pct")), _pct(r.get("shell_errors_pct")),
            f"On {r.get('model')}, effort {r.get('effort')}: {_worse_part(r.get('difference'))}"] for r in worse]
        + [[_who(r.get("agent_type")), r.get("runs"), "", "", "", f"On {r.get('model')}: {r['reason']}"]
           for r in retried.values()],
    )
    fixes = []
    tips = []
    for row in worse:
        agent = row.get("agent_type")
        base_model, base_effort = row.get("compared_model") or "", row.get("compared_effort") or ""
        evidence = (
            f"On {row.get('model')} at effort {row.get('effort')}, {agent} did worse than on {base_model} at effort "
            f"{base_effort}: {row.get('difference')}"
        )
        if agent == quality.MAIN or agent in _NOT_OVERRIDABLE:
            tips.append({"title": f"{_who(None if agent == quality.MAIN else agent)} did worse on "
                                  f"{row.get('model')}", "text": evidence})
            continue
        fields = ctx.effective_agents.get(agent) if isinstance(ctx.effective_agents.get(agent), dict) else {}
        now_model = fields.get("model")
        if goals._alias(row.get("model") or "") != goals._alias(base_model) and (now_model is None or goals._alias(now_model) == goals._alias(
            row.get("model") or ""
        )):
            fixes.append(_candidate_fix(ctx, {"agent": agent, "key": "model", "value": goals._alias(base_model),
                                              "now": now_model, "evidence": evidence},
                                        f"{agent}: back to {goals._alias(base_model)}"))
        now_effort = fields.get("effort")
        if base_effort not in ("", "default") and base_effort != row.get("effort") and now_effort in (
            None, row.get("effort")
        ):
            fixes.append(_candidate_fix(ctx, {"agent": agent, "key": "effort", "value": base_effort,
                                              "now": now_effort, "evidence": evidence},
                                        f"{agent}: back to effort {base_effort}"))
        if not any(fix.get("agent") == agent for fix in fixes):
            tips.append({"title": f"{agent} did worse on {row.get('model')}, effort {row.get('effort')}",
                         "text": evidence + " Its agent file no longer uses that setup, so nothing to change."})
    for (agent, family), row in retried.items():
        evidence = (
            f"{row['reason'][:1].upper()}{row['reason'][1:]}: {row.get('files_edited_again')} of the "
            f"{row.get('files_edited')} files those runs edited, last on {row.get('last_retried')}."
        )
        back_to = goals._alias(row.get("retried_on") or "")
        fields = ctx.effective_agents.get(agent) if isinstance(ctx.effective_agents.get(agent), dict) else {}
        now_model = fields.get("model")
        on_it = now_model is not None and goals._alias(now_model) == family
        if (
            on_it
            and agent not in _NOT_OVERRIDABLE
            and back_to
            and (row.get("retried") or 0) >= quality.MIN_RETRIED_RUNS
            and not any(fix.get("agent") == agent and fix.get("key") == "model" for fix in fixes)
        ):
            fixes.append(_candidate_fix(ctx, {"agent": agent, "key": "model", "value": back_to, "now": now_model,
                                              "evidence": evidence}, f"{agent}: back to {back_to}"))
            continue
        if on_it:
            advice = (f" One more retry and this check will offer to move it back to {back_to}." if back_to
                      and (row.get("retried") or 0) < quality.MIN_RETRIED_RUNS else "")
        else:
            said = f"now says {now_model}" if now_model is not None else "names no model"
            advice = (
                f" Its agent file {said}, so those runs were most likely started on {family} by whatever dispatched "
                "them: a workflow script's model setting, or Claude choosing a model when it started the agent. "
                f"Don't pick {family} for this agent's work."
            )
        tips.append({"title": f"{agent}: runs on {family} were retried on a larger model", "text": evidence + advice})
    for row in tables.rows("quality", "quality_retry_reasons"):
        for reason, (title, text) in _REASON_TIPS.items():
            n = int(whatif._num(row.get(f"said_{reason}")) or 0)
            if n >= MIN_DECLARED_RETRIES:
                agent = _who(row.get("agent_type"))
                tips.append({"title": title.format(agent=agent),
                             "text": text.format(n=n, model=goals._alias(row.get("model") or "") or "the model")})
    missed = next((r for r in tables.rows("habits", "habits_outcomes") if r.get("outcome") == "missed"), None)
    if missed and (whatif._num(missed.get("pieces")) or 0) >= 1:
        slow = missed.get("slow") or ""
        helped = missed.get("helped") or ""
        tips.append({
            "title": "Work you said missed its goal",
            "text": f"{int(whatif._num(missed.get('pieces')) or 0)} pieces of work missed their goal"
            + (f", costing {_money(ctx, missed.get('cost'))}." if whatif._num(missed.get("cost")) else ".")
            + (f" Most often slowed by: {slow}." if slow else "")
            + (f" Would have helped most: {helped}." if helped else ""),
        })
    marker_fix = _capture_fix(ctx, tables)
    if marker_fix is not None:
        fixes.append(marker_fix)
    for row in mixed:
        agent = row.get("agent_type")
        who = _who(None if agent == quality.MAIN else agent)
        tips.append({
            "title": f"{who}: mixed results on {row.get('model')}, effort {row.get('effort')}",
            "text": f"Against {row.get('compared_model')} at effort {row.get('compared_effort')}, some signals were "
            f"clearly better and others clearly worse: {row.get('difference')} Neither setup is clearly better, so "
            "nothing to switch.",
        })
    for row, issues in struggling:
        agent = row.get("agent_type")
        who = _who(None if agent == quality.MAIN else agent)
        tool = next((f for f in failing if f.get("agent_type") == agent), None)
        if (whatif._num(row.get("tool_errors_pct")) or 0) >= STRUGGLE_PCT["tool_errors_pct"] or (
            whatif._num(row.get("shell_errors_pct")) or 0
        ) >= STRUGGLE_PCT["shell_errors_pct"]:
            tips.append({
                "title": f"{who}: tool calls fail often",
                "text": (f"Its {tool.get('tool')} calls failed {tool.get('errors')} times in {tool.get('runs_with_errors')} "
                         "runs. " if tool else "")
                + "Say in its task prompt or agent file which commands and paths to use, and allow the ones it "
                "needs, so it doesn't spend replies recovering.",
            })
        if (whatif._num(row.get("unfinished_pct")) or 0) >= STRUGGLE_PCT["unfinished_pct"]:
            out_of_turns = whatif._num(row.get("turn_limit_pct")) or 0
            tips.append({
                "title": f"{who}: runs often don't finish",
                "text": (f"{out_of_turns:.0f}% of its runs most likely ran out of turns (the agent's maxTurns). "
                         if out_of_turns else "")
                + "Give it a smaller task, or raise maxTurns in its agent file if it keeps stopping mid-task. "
                "Quality signal counts ({{page:agents/quality}}) splits failed, stopped, cut off and out of turns.",
            })
        if (whatif._num(row.get("corrections_pct")) or 0) >= STRUGGLE_PCT["corrections_pct"]:
            tips.append({
                "title": "You correct Claude often",
                "text": "Say what done looks like in your first message (the file, the test to pass, what not to "
                "touch). Put rules you repeat into CLAUDE.md.",
            })
        if (whatif._num(row.get("max_tokens_pct")) or 0) >= STRUGGLE_PCT["max_tokens_pct"]:
            tips.append({
                "title": f"{who}: replies hit the output limit",
                "text": "Ask for the result in parts, or write long output to a file instead of the reply.",
            })
    if not struggling and not worse and not retried:
        tested = [
            r for r in setups if r.get("setup_verdict") not in ("only", "baseline", "too_little_data", "not_comparable")
        ]
        return _result(
            "ok",
            "No agent stands out: none fails often, no model or effort did clearly worse than the one it is "
            "compared with, and no agent was often run again on a larger model"
            + (f" ({len(tested)} setups compared)." if tested else "."),
            fixes=fixes,
            tips=tips,
        )
    parts = []
    if worse:
        parts.append(f"{len(worse)} model or effort setup{'s' if len(worse) != 1 else ''} did clearly worse than the "
                     "one that agent used most")
    if retried:
        parts.append(f"{len(retried)} agent{'s were' if len(retried) != 1 else ' was'} often run again on a larger "
                     "model after a cheaper one")
    if struggling:
        one = len(struggling) == 1
        parts.append(f"{len(struggling)} agent{'' if one else 's'} often fail{'s' if one else ''} or "
                     f"{'doesn' if one else 'don'}'t finish")
    caveat = (
        " Setups ran at different times and maybe on different work, so check {{page:changes}} before you "
        "switch back." if worse else ""
    )
    return _result(
        "act",
        " and ".join(parts) + "." + caveat,
        table=table,
        fixes=_merge_fixes(fixes),
        tips=tips,
    )


CHECKS: tuple[Check, ...] = (
    Check("models", "Is each agent on the cheapest model that does the job?",
          "Every reply is priced by its model; a cheaper model for routine agents is usually the largest saving.",
          _models, ("model-tier", "model-tier-main")),
    Check("effort", "Is anything thinking more than the work needs?",
          "Thinking is billed as output, the most expensive kind of token.", _effort, ("effort-mismatch",)),
    Check("compaction", "When should conversations be summarised?",
          "Every reply re-reads the whole conversation, so the point it's summarised at sets the cost of each reply.",
          _compaction, ("compaction-window", "compaction-churn", "plan-handoff", "run-split")),
    Check("cache", "Which cache lifetime is cheaper for you?",
          "A 5-minute cache is cheaper to write; a 1-hour one survives longer pauses without rebuilding.", _cache,
          ("ttl-switch",)),
    Check("tools", "Do agents carry tools, MCP servers or skills they never use?",
          "Everything an agent is offered is sent each time it starts, used or not.", _tools,
          ("spawn-unused-mcp", "spawn-unused-skills", "spawn-read-only-tools", "baseline-bloat")),
    Check("skills", "Which skills are listed to Claude but never used?",
          "Each skill's name and description is sent at every session and subagent start.", _skills),
    Check("claude-md", "Which CLAUDE.md files cost most?",
          "CLAUDE.md files are sent at the start of every session and most subagents.", _claude_md,
          ("spawn-claude-md", "spawn-shared-claude-md")),
    Check("tool-output", "Do tool results fill your context?",
          "A tool's output stays in the conversation and is re-read on every later reply.", _tool_output,
          ("tool-output-carry",)),
    Check("hooks", "Do your hooks work, and what do they cost?",
          "A failing hook doesn't do its job, and context a hook adds is re-read on every later reply.", _hooks,
          tuple(sorted(_HOOK_RECS))),
    Check("tool-search", "What does MCP tool search save you?",
          "Claude Code lists MCP tools by name and loads a full definition only when Claude needs it, so the rest "
          "aren't re-read on every reply.", _tool_search),
    Check("habits", "Do any habits cost tokens?",
          "Pauses, retries and long reports cost tokens that no setting can save.", _habits, tuple(sorted(_HABIT_RECS))),
    Check("quality", "Is any agent struggling?",
          "A cheaper model or a lower effort only saves money if the work still gets done.", _quality),
    Check("cost-record", "Do ClaudeGlass's figures match Claude Code's own?",
          "Claude Code writes down what it thinks each session cost. Where it does, ClaudeGlass checks its own "
          "figures against it.", _cost_record),
)
CHECK_IDS = tuple(check.id for check in CHECKS)


def run(check_id: str, ctx: Context) -> dict:
    """One check's full answer. Raises ``KeyError`` for an unknown id."""
    check = next((c for c in CHECKS if c.id == check_id), None)
    if check is None:
        raise KeyError(check_id)
    return {
        "id": check.id,
        "question": check.question,
        "why": check.why,
        "period": ctx.period,
        "rule_ids": list(check.rule_ids),
        **check.run(ctx),
    }


def run_all(ctx: Context) -> list[dict]:
    """Every check's status and summary (the list view)."""
    out = []
    for check in CHECKS:
        result = run(check.id, ctx)
        out.append({key: result[key] for key in ("id", "question", "why", "status", "summary", "rule_ids")}
                   | {"fix_count": len(result["fixes"]), "tip_count": len(result["tips"])})
    return out


def render_markdown(result: dict) -> str:
    lines = [f"## {result['question']}", "", pages.plain(result["summary"]), ""]
    table = result.get("table")
    if table:
        lines.append("| " + " | ".join(c["label"] for c in table["columns"]) + " |")
        lines.append("|" + "---|" * len(table["columns"]))
        for row in table["rows"]:
            lines.append("| " + " | ".join(str(cell).replace("|", "/") for cell in row) + " |")
        lines.append("")
    for tip in result.get("tips") or ():
        lines += [f"- **{tip['title']}**: {pages.plain(tip['text'])}"]
    if result.get("tips"):
        lines.append("")
    for fix in result.get("fixes") or ():
        lines += [f"### {fix.get('title') or fix.get('key')}", ""]
        for heading, text in fix.get("explainer") or ():
            lines.append(f"- **{heading}**: {pages.plain(text)}")
        if fix.get("prompt"):
            lines += ["", "Prompt for Claude:", "", "```text", fix["prompt"], "```"]
        if fix.get("command"):
            lines += ["", "Command:", "", "```bash", fix["command"], "```"]
        if fix.get("prompt") or fix.get("command"):
            lines += ["", RESTART_NOTE]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


__all__ = ["CHECKS", "CHECK_IDS", "Context", "render_markdown", "run", "run_all"]

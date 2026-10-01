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

from . import capture_catalogue, carry, discovery, habits, known_savers, model_gate, model_swap, pages, quality, waste, whatif
from .compaction_sim import CompactionSimThresholds
from .fixes import PROMPT_RESTART, PROMPT_SCOPE, _FINDING_OPEN, build_fix, build_fixes, fix_note
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
    #: The project folders the report was limited to, or ``None`` when it
    #: saw every project: a skill Claude never used there is hidden in
    #: that project only (``skills_review``).
    only: tuple[Path, ...] | None = None
    #: The report window's own bounds, as Unix timestamps (``None``: open
    #: on that side) -- for a check that reads outside the report itself,
    #: such as the "savers" check's read of tokensave's own ledger.
    since_ts: float | None = None
    until_ts: float | None = None


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


def _signed_money(ctx: Context, usd, *, period: bool = False) -> str:
    """``_money``, but a negative amount reads as "-$0.42" instead of
    "none" -- ``Units.money`` only phrases a positive amount, and a net
    figure (the "savers" check's own) can go either way."""
    value = whatif._num(usd) or 0.0
    return f"-{_money(ctx, -value, period=period)}" if value < 0 else _money(ctx, value, period=period)


def _signed_cell(ctx: Context, usd) -> str:
    """``_cell``, with the same sign handling as :func:`_signed_money`."""
    value = whatif._num(usd) or 0.0
    return f"-{_cell(ctx, -value)}" if value < 0 else _cell(ctx, value)


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
            # UX: an informational finding's "fix" has nothing to paste or
            # run -- a card under "The fixes" that says so is noise; the
            # finding still shows as a tip and a recommendation.
            if not fix.get("prompt") and not fix.get("command"):
                continue
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
            some = " on some signals" if setup.get("setup_verdict") == "mixed" else ""
            reason = (
                f"on {setup.get('model')} at effort {setup.get('effort')} it did worse{some} than on "
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
    ids = {"spawn-unused-mcp", "spawn-unused-skills", "spawn-read-only-tools", "baseline-bloat"}
    # baseline-bloat is about main sessions, so it can fire when no
    # subagent started: look its fix up first.
    fixes = _rec_fixes(_recommendations(ctx, ids))
    if not rows:
        if fixes:
            return _result("act", "No subagents started in this window; the fix below is for your main sessions.",
                           fixes=fixes)
        return _result("no_data", "No subagents started in this window.")
    table = _table(
        [("agent", "Agent"), ("spawns", "Starts"), ("mcp", "Offered MCP / used it"),
         ("skills", "Listed skills / used one")],
        [[_who(r.get("agent_type")), r.get("spawns"),
          f"{r.get('mcp_offered_spawns') or 0} / {r.get('mcp_used_spawns') or 0}",
          f"{r.get('skills_listed_spawns') or 0} / {r.get('skills_used_spawns') or 0}"] for r in rows],
    )
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
        ctx.config_dir, getattr(ctx.model, "context_files", None) or {}, ctx.units, ctx.period, only=ctx.only,
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
    limited = data["limited_text"]
    return _result(
        "act",
        f"{len(unused)} skills were listed to Claude at every session and subagent start but never used "
        f"{ctx.period}. Hiding them from Claude keeps them available to you as /name."
        + (f" {limited}" if limited else ""),
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


#: The least reads (``carry_by_tool``'s own ``result_count``) before a
#: Read fix is offered -- one or two large reads don't justify a
#: standing habit change.
_READ_FIX_MIN_RESULTS = 5


def _cap_little_text(ctx: Context, cap_little: list[tuple[str, str, float]], *, period: bool = True) -> str:
    """``cap_little``'s entries (setting, value, saved) as one clause for
    the summary sentence: "BASH_MAX_OUTPUT_LENGTH at 15000 would have
    saved $0.01 and MAX_MCP_OUTPUT_TOKENS nothing". ``period=False`` when
    the sentence before it already named the period."""
    parts = []
    for setting, value, saved in cap_little:
        amount = _money(ctx, saved, period=period) if saved else "nothing"
        parts.append(f"{setting} at {value} would have saved {amount}")
    return " and ".join(parts)


def _read_fix(ctx: Context, read: dict, is_largest: bool, share: float) -> dict:
    """A real fix (not a tip) for Read dominating carried context: read
    only the lines Claude needs, worded for tokensave's own tools when
    it's at work (:func:`known_savers.active_in_report`). ``share`` is
    Read's part of the cost of carrying every tool's results."""
    tokens = int(whatif._num(read.get("tokens_entered")) or 0)
    results = int(whatif._num(read.get("result_count")) or 0)
    turns = whatif._num(read.get("mean_turns_carried")) or 0
    why = f"Read carried {tokens:,} tokens over {results:,} reads; each stayed about {turns:.0f} replies."
    if is_largest:
        why += " That's the most of any tool."
    if known_savers.active_in_report(ctx.model, min_share=known_savers.ADVICE_MIN_SHARE):
        saver = known_savers.TOKENSAVE
        context_tool, search_tool, _files_tool, read_tool = saver.search_tools
        body = (
            f"From now on, find the lines first with {context_tool} or {search_tool}, then read just that range "
            f'with {read_tool} in "lines" mode instead of the whole file.'
        )
    else:
        body = (
            "From now on, find the lines first (Grep for the function or text), then read just that range with "
            "Read's offset and limit instead of the whole file."
        )
    opening = _FINDING_OPEN.format(title=f"File reads are {share:.0%} of what my tool results cost to keep in context")
    opening += f" {why}"
    return {
        "key": None,
        "agent": None,
        "title": "Have Claude read only the part of a file it needs",
        "explainer": [["Why it's suggested", why]],
        "command": None,
        "command_warning": "",
        "prompt": f"{opening} {body} " + PROMPT_SCOPE,
        "note": "scope",
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
    cap_little: list[tuple[str, str, float]] = []
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
            # UX: a cap that would save little is a fact for the summary
            # sentence, not a card of its own -- it isn't something to do.
            cap_little.append((cap.setting, cap.value, saved))

    carry_recs = _recommendations(ctx, {"tool-output-carry"})
    read = next((r for r in rows if r.get("key") == "Read"), None)
    if (
        read
        and total
        and (whatif._num(read.get("carry_cost_usd")) or 0) / total >= 0.1
        and (whatif._num(read.get("result_count")) or 0) >= _READ_FIX_MIN_RESULTS
        # UX: tool-output-carry's own workflow prompt already tells
        # Claude to prefer Grep over Read for a large file -- don't say
        # the same thing twice.
        and not carry_recs
    ):
        fixes.append(_read_fix(
            ctx, read, is_largest=rows[0].get("key") == "Read",
            share=(whatif._num(read.get("carry_cost_usd")) or 0) / total,
        ))
    fixes = _merge_fixes(fixes, _rec_fixes(carry_recs))

    loops = next((r for r in tables.rows("habits", "habits_tool_output") if r.get("tool") == "loops"), None)
    if loops and (whatif._num(loops.get("loops")) or 0) >= 1:
        tips.append({
            "title": "Stop a failing command sooner",
            "text": f"{int(whatif._num(loops.get('loops')) or 0)} commands failed three or more times within one "
            f"message, costing {_money(ctx, loops.get('cost'), period=True, prefix='about ')}. Ask Claude to stop after two failed tries "
            "at the same command and tell you what it saw.",
        })

    if not fixes:
        if cap_little:
            return _result(
                "ok",
                f"An output cap wouldn't help much: {_cap_little_text(ctx, cap_little)}, because most results are "
                "already short.",
                table=table,
                tips=tips,
            )
        return _result("ok", "Nothing to change: no one tool's output dominates your context.", table=table, tips=tips)
    summary = (
        f"Carrying tool results in context cost {_money(ctx, total, period=True)}. "
        "Every result is re-read on every later reply until a summary drops it."
    )
    if cap_little:
        summary += (
            f" An output cap wouldn't help much: {_cap_little_text(ctx, cap_little, period=False)}, because most "
            "results are already short."
        )
    return _result("act", summary, table=table, fixes=fixes, tips=tips)


_HOOK_RECS = {"hook-failures", "hook-block-resent", "hook-context-carry"}


def _hooks(ctx: Context) -> dict:
    tables = whatif._Tables(ctx.model)
    rows = tables.rows("hooks", "hooks_by_script")
    if not rows:
        return _result("no_data", "No hook of yours left a record in this window.")

    def count(row, key) -> int:
        return int(whatif._num(row.get(key)) or 0)

    def last_failed(row) -> str:
        last = str(row.get("last_failed") or "")
        return f"{last} (stopped failing)" if last and row.get("stopped") == "yes" else last

    table = _table(
        [("hook", "Hook"), ("failed", "Failed runs"), ("cause", "Why it failed"), ("last_failed", "Last failed"),
         ("blocks", "Calls blocked"), ("resent", "Sent again unchanged"), ("context", "Context added"),
         ("cost", "Cost of its context and blocks")],
        [[r.get("hook"), f"{count(r, 'failed'):,}", str(r.get("cause") or "")[:1].upper() + str(r.get("cause") or "")[1:],
          last_failed(r), f"{count(r, 'blocks'):,}", f"{count(r, 'resent'):,}", f"{count(r, 'context_tokens'):,} tokens",
          _cell(ctx, (whatif._num(r.get("carry_usd")) or 0) + (whatif._num(r.get("block_usd")) or 0))]
         for r in rows[:10]],
    )
    # A hook fixed mid-window keeps its old failures until they age out;
    # hook_costs judges whether it has stopped failing since.
    stopped = [r for r in rows if count(r, "failed") and r.get("stopped") == "yes"]
    failing = [r for r in rows if count(r, "failed") and r.get("stopped") != "yes"]
    latest = max((str(r.get("last_failed") or "") for r in stopped), default="")
    recs = _recommendations(ctx, _HOOK_RECS)
    if not recs:
        if len(stopped) == 1:
            summary = (
                f"Your hooks work: {stopped[0].get('hook')}, which failed earlier in this window, stopped failing "
                f"after {latest}, and none costs much in kept context or blocked calls."
            )
        elif stopped:
            summary = (
                f"Your hooks work: the {len(stopped)} that failed earlier in this window stopped failing after "
                f"{latest}, and none costs much in kept context or blocked calls."
            )
        else:
            summary = "Your hooks work, and none costs much in kept context or blocked calls."
        return _result("ok", summary, table=table)
    if any(rec.id == "hook-failures" for rec in recs):
        summary = (
            f"{len(failing)} of your hooks failed {sum(count(r, 'failed') for r in failing):,} times, so they didn't "
            "do their job."
        )
        if len(stopped) == 1:
            summary += f" {stopped[0].get('hook')} stopped failing after {latest}."
        elif stopped:
            summary += f" Another {len(stopped)} stopped failing after {latest}."
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
    # An MCP server Claude never used costs every reply whether or not
    # tool search deferred anything (one loaded upfront isn't deferred),
    # so its fix is looked up before either "no data" answer.
    unused = _recommendations(ctx, {"mcp-unused-server"})
    fixes = _rec_fixes(unused)
    also = "".join(f" {rec.title}." for rec in unused) if fixes else ""
    status = "act" if fixes else "no_data"
    if not replies:
        return _result(status, f"No reply {ctx.period} had tools deferred by tool search.{also}", fixes=fixes)
    net = whatif._num(row.get("net_usd"))
    if net is None:
        return _result(
            status,
            f"Tool search deferred up to {most:,} tools a reply {ctx.period}, but none was loaded, so their size "
            f"isn't known.{also}",
            fixes=fixes,
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
    return _result("act" if fixes else "ok", text + also, table=table, fixes=fixes)


#: How much of a known saver's own net loss its redirects have to
#: explain before the fix offered is "search past its hook" rather than
#: "turn it off" -- reuses ``CAP_MIN_SAVING_SHARE``'s own "worth acting
#: on" bar (10%) for the same reason: below it, the redirects are a
#: minor part of the story and turning the saver off is the more direct
#: lever.
_SAVER_REDIRECT_SHARE = CAP_MIN_SAVING_SHARE
#: With no ledger to size a loss by (:func:`known_savers.read_ledger`
#: returned ``None``), the flat cost its redirects have to reach on
#: their own before they're worth a fix -- the same $0.01 "material at
#: all" floor ``savers.SaverThresholds.net_saving_usd_min`` uses for the
#: general present/absent saver comparison (docs/savers.md).
_SAVER_REDIRECT_MIN_USD = 0.01


def _saver_scope_fix(ctx: Context, saver: known_savers.KnownSaver, replies: int, cost: float) -> dict:
    """A "from now on" prompt asking Claude to go straight to the
    saver's own search tools, so its hook has nothing left to redirect."""
    context_tool, search_tool = saver.search_tools[0], saver.search_tools[1]
    why = (
        f"{saver.name}'s hook turned away a search or an Explore agent in {replies:,} "
        f"{'reply' if replies == 1 else 'replies'}, which cost {_money(ctx, cost, period=True)} and did nothing "
        "but point Claude at its tools."
    )
    opening = _FINDING_OPEN.format(title=f"{saver.name} keeps turning my searches away") + f" {why}"
    body = (
        f"From now on, for code searches, go straight to {context_tool} or {search_tool} instead of Grep, Glob "
        f"or an Explore agent, so {saver.name}'s hook has nothing to turn away."
    )
    return {
        "key": None,
        "agent": None,
        "title": f"Go straight to {saver.name}'s own tools",
        "explainer": [
            ["Why it's suggested", why],
            [
                "Where and who it affects",
                "Wherever you tell Claude when it asks: this session, a line in this project's CLAUDE.md, or a "
                "line in your ~/.claude/CLAUDE.md.",
            ],
            ["Trade-off", f"{saver.name}'s own tools may miss a match Grep or Glob would have found."],
            ["How to undo it", "Remove the line from that CLAUDE.md; a session-only rule ends with the session."],
        ],
        "command": None,
        "command_warning": "",
        "prompt": f"{opening} {body} " + PROMPT_SCOPE,
        "note": "scope",
    }


def _saver_uninstall_fix(ctx: Context, saver: known_savers.KnownSaver, net: float) -> dict:
    """A command fix that turns the saver off outright: offered once its
    net cost, not just its redirects, is the larger part of the loss."""
    return {
        "key": None,
        "agent": None,
        "title": f"Turn {saver.name} off",
        "explainer": [
            [
                "Why it's suggested",
                f"What {saver.name} says it replaced was worth less than what its own answers and redirects "
                f"cost: net {_signed_money(ctx, net, period=True)}.",
            ],
            [
                "Where and who it affects",
                f"Removes {saver.name}'s MCP server and hook from Claude Code, for every session, in every "
                "project.",
            ],
            [
                "Trade-off",
                f"You lose {saver.name}'s search tools; Grep, Glob and Read go back to being the only way to "
                "look at code. Any real saving it made outside this window is lost too.",
            ],
            ["How to undo it", f"Reinstall {saver.name} the way you did the first time."],
        ],
        "command": f"{saver.name} uninstall --agent claude",
        "command_warning": (
            f"This only removes {saver.name}'s hook and MCP server entry. It doesn't delete the index it built "
            f"under a project's {saver.index_dir} folder, or its own ledger -- remove those by hand if you don't "
            "plan to reinstall it."
        ),
        "prompt": (
            f"Run `{saver.name} uninstall --agent claude` and tell me what it changed before doing anything "
            "further."
        ),
    }


def _saver_table(rows: list[list]) -> dict | None:
    return _table([("row", "What"), ("amount", "Tokens / replies"), ("value", "Cost")], rows)


def _savers(ctx: Context) -> dict:
    saver = known_savers.TOKENSAVE
    if not known_savers.active_in_report(ctx.model, saver):
        return _result("no_data", f"{saver.name} wasn't used {ctx.period}.")

    tables = whatif._Tables(ctx.model)
    tool_rows = [
        r for r in tables.rows("carry", "carry_by_tool") if str(r.get("key") or "").startswith(saver.tool_prefix)
    ]
    own_calls = sum(int(whatif._num(r.get("result_count")) or 0) for r in tool_rows)
    own_tokens = sum(int(whatif._num(r.get("tokens_entered")) or 0) for r in tool_rows)
    own_cost = sum(whatif._num(r.get("carry_cost_usd")) or 0 for r in tool_rows)

    redirect_rows = [
        r for r in tables.rows("waste", "waste_blocked_by") if r.get("kind") == "saver" and r.get("blocker") == saver.name
    ]
    redirect_replies = sum(int(whatif._num(r.get("turns")) or 0) for r in redirect_rows)
    redirect_cost = sum(whatif._num(r.get("cost_usd")) or 0 for r in redirect_rows)

    # A report already limited to one project shouldn't fold in another
    # project's ledger rows; one that spans several (or every) project
    # reads the whole ledger, same as active_in_report reads the whole
    # report.
    project = ctx.only[0] if ctx.only and len(ctx.only) == 1 else None
    ledger = known_savers.read_ledger(ctx.since_ts, ctx.until_ts, project=project)

    if ledger is None:
        table = _saver_table([
            ["Its own answers, carried in context", f"{own_calls:,}", _cell(ctx, own_cost)],
            ["Replies spent on its redirects", f"{redirect_replies:,}", _cell(ctx, redirect_cost)],
        ])
        summary = (
            f"Only {saver.name}'s own costs are known here, not what it replaced: its ledger isn't on this "
            "machine (it may run in Docker, or on another one). "
            f"Its answers cost {_money(ctx, own_cost, period=True)} to carry"
            + (f", and its redirects cost {_money(ctx, redirect_cost)} more" if redirect_cost else "")
            + "."
        )
        if redirect_cost >= _SAVER_REDIRECT_MIN_USD:
            return _result(
                "act", summary, table=table, fixes=[_saver_scope_fix(ctx, saver, redirect_replies, redirect_cost)]
            )
        return _result("ok", summary, table=table)

    rate = own_cost / own_tokens if own_tokens else 0.0
    value = ledger.before * rate
    net = value - own_cost - redirect_cost

    table = _saver_table([
        [f"{saver.name} says its answers replaced", f"{ledger.before:,}", _cell(ctx, value)],
        ["Its own answers, carried in context", f"{own_tokens:,}", _cell(ctx, own_cost)],
        ["Replies spent on its redirects", f"{redirect_replies:,}", _cell(ctx, redirect_cost)],
        ["Net", "", _signed_cell(ctx, net)],
    ])
    summary = (
        f"{saver.name} says it saved {ledger.saved:,} tokens in {ledger.calls:,} calls {ctx.period}. "
        f"ClaudeGlass values what it replaced -- if carried as long as its own answers were -- at "
        f"{_money(ctx, value)}; its answers themselves cost {_money(ctx, own_cost)} to carry"
        + (f", and its redirects cost {_money(ctx, redirect_cost)} more" if redirect_cost else "")
        + f". Net: {_signed_money(ctx, net)}. "
        f"{ledger.losing_calls} of {ledger.calls} calls cost more than they replaced."
    )
    if net >= 0:
        return _result("ok", summary, table=table)
    if redirect_cost and redirect_cost >= _SAVER_REDIRECT_SHARE * abs(net):
        fix = _saver_scope_fix(ctx, saver, redirect_replies, redirect_cost)
    else:
        fix = _saver_uninstall_fix(ctx, saver, net)
    return _result("act", summary, table=table, fixes=[fix])


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
    "wasted-turns",
}


def _blocked_by_label(row: dict) -> str:
    """A ``waste_blocked_by`` row worded for the habits table's own
    "cause" column: "blocked: claude-implementer, by Claude Code's
    worktree guard", or, for a saver's on-purpose redirect, "redirected
    by tokensave (not waste)" -- no agent named, since a redirect is the
    same non-problem whichever agent it happened to."""
    blocker = row.get("blocker") or ""
    if row.get("kind") == "saver":
        return f"redirected by {blocker} (not waste)"
    who = _who(None if row.get("agent_type") == "top-level" else row.get("agent_type"))
    return f"blocked: {who}, by {blocker}"


def _habit_rows(tables) -> list[dict]:
    """The habits card's own "replies that went nowhere" rows:
    ``waste_by_cause`` minus its "blocked"/"redirected" rows (a flat
    total says less than who blocked it), plus one row per
    ``waste_blocked_by`` breakdown row, each already labelled for
    display. Kept as plain dicts, cost_usd still a raw number, so the
    summary can pick out and phrase the costliest one."""
    rows = [
        {
            "label": r.get("cause"), "turns": r.get("turns"), "cost_usd": r.get("cost_usd"),
            "lever": r.get("lever") or "", "redirect": False, "blocker": None,
        }
        for r in tables.rows("waste", "waste_by_cause")
        if whatif._num(r.get("turns")) and r.get("cause") not in ("blocked", waste.REDIRECT_CAUSE)
    ]
    rows += [
        {
            "label": _blocked_by_label(r), "turns": r.get("turns"), "cost_usd": r.get("cost_usd"),
            "lever": r.get("lever") or "", "redirect": r.get("kind") == "saver", "blocker": r.get("blocker"),
        }
        for r in tables.rows("waste", "waste_blocked_by")
        if whatif._num(r.get("turns"))
    ]
    return rows


def _recs_with_prompt_fix(recs) -> set[str]:
    """Recommendation ids among ``recs`` whose own fix (``_rec_fixes``)
    already carries a prompt: these are skipped as tips so the same
    finding isn't said twice, once as a plain tip and once as a fix card
    offering to act on it."""
    return {rec.id for rec in recs if any((fix.get("prompt") or "") for fix in _rec_fixes([rec]))}


def _habits(ctx: Context) -> dict:
    tables = whatif._Tables(ctx.model)
    rows = _habit_rows(tables)
    table = _table(
        [("cause", "Replies that went nowhere"), ("turns", "Replies"), ("cost", "Cost"), ("lever", "What helps")],
        [[r["label"], r["turns"], _cell(ctx, r["cost_usd"]), r["lever"]] for r in rows],
    )
    recs = _recommendations(ctx, _HABIT_RECS)
    skip_as_tip = _recs_with_prompt_fix(recs)
    tips = [{"title": rec.title, "text": rec.action or rec.why} for rec in recs if rec.id not in skip_as_tip]
    playbook = _playbook_tips(ctx, tables)
    tips += playbook
    fixes = _rec_fixes(recs)

    if not recs and not rows and not playbook:
        return _result("no_data", "Not enough sessions in this window.")

    problem_rows = [r for r in rows if not r["redirect"]]
    redirect_rows = [r for r in rows if r["redirect"]]

    if not recs and not playbook:
        if not problem_rows and redirect_rows:
            savers = ", ".join(sorted({r["blocker"] for r in redirect_rows if r["blocker"]}))
            cost = sum(whatif._num(r["cost_usd"]) or 0 for r in redirect_rows)
            return _result(
                "ok",
                f"The only blocked replies {ctx.period} were {_money(ctx, cost, prefix='about ')} of on-purpose "
                f"redirects from {savers} -- not a problem to fix.",
                table=table,
            )
        if problem_rows:
            cost = sum(whatif._num(r["cost_usd"]) or 0 for r in problem_rows)
            return _result(
                "ok",
                f"{_money(ctx, cost, prefix='About ')} went to replies that went nowhere {ctx.period}, but no "
                "single habit crossed the bar to flag.",
                table=table,
            )
        return _result("ok", "No habit stands out as costing tokens.", table=table)

    ways = len(recs) + len(playbook)
    top = max(problem_rows, key=lambda r: whatif._num(r["cost_usd"]) or 0, default=None)
    why = f" The costliest: {top['label']} ({_cell(ctx, top['cost_usd'])})." if top else ""
    return _result(
        "act",
        f"{ways} way{'s' if ways != 1 else ''} of working cost tokens {ctx.period}.{why} These are habits, not "
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
        title = row.get("title") or habits.ITEMS.get(key, ("", key))[1]
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
    """The prompt that removes the older :data:`quality.MARKER_HEADING`
    section from CLAUDE.md, whenever it's there: it asks every subagent to
    tag its own report, which broke answers that had to be exact. Else
    metrics capture at Essentials, offered when agents ran in this window,
    none has a ``[result: ...]`` word and capture is off."""
    claude_md = discovery.claude_root() / "CLAUDE.md"
    try:
        has_section = quality.MARKER_HEADING in claude_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        has_section = False
    if has_section:
        return _remove_markers_fix()
    if _capture_on(tables):
        return None
    markers = {r.get("marker"): r for r in tables.rows("quality", "quality_markers")}
    result = markers.get("[result: ...]") or {}
    if not (whatif._num(result.get("of_runs")) or 0) or (whatif._num(result.get("runs")) or 0):
        return None
    metrics = capture_catalogue.level_metrics("essentials")
    main = round(len(capture_catalogue.note_text(metrics, "main")) / carry._CHARS_PER_TOKEN_APPROX)
    return {
        "key": None,
        "agent": None,
        "title": "Turn on metrics capture",
        "explainer": [
            ["What this adds", "Metrics capture at its Essentials level. A hook adds a short note at each session "
             "start asking Claude to end its reply to each of your messages with a one-line tag (the kind of task, "
             "how clear the ask was, how hard the work was, whether it changed course). A subagent is asked for "
             "nothing: when one finishes, Claude Haiku reads its brief and the end of its report and says whether "
             "it finished and, for a rerun, why it was run again. This tool keeps only those words, never the text "
             "around them."],
            ["Why", "Without them this check guesses: a retry on a larger model counts against the cheaper one even "
             "when the brief was the problem, and an agent that stopped half-done looks finished. With them, "
             "retries and unfinished runs are counted from what Claude said, and {{page:habits}} can rank "
             "habits by kind of task."],
            ["What it costs", f"A note of about {main} tokens at each session start, read from the prompt cache "
             "after the first reply, about 15 output tokens per message, and a Claude Haiku call of about "
             f"${capture_catalogue.JUDGE_USD_PER_CALL:.3f} per subagent run. " "{{page:setup/capture}} estimates it "
             "from your own recent sessions before you turn it on, and the banner shows what it has cost while it's "
             "on."],
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
            ["Why", "It asks every subagent to end its report with [result: ...] and every rerun's brief to start "
             "with [retry: ...]. A subagent asked for JSON only added the marker after it, breaking the answer, "
             "and the session that started it took the line for an injected instruction. Metrics capture now gets "
             "the same answers from Claude Haiku after each run, without asking the agent anything."],
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
          "aren't re-read on every reply.", _tool_search, ("mcp-unused-server",)),
    Check("known-savers", "What does tokensave save you?",
          "A token-saving tool has its own overhead: its answers still sit in context, and its hook can turn "
          "a call away and cost a reply. This weighs what it says it saved against what it cost.", _savers),
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
        note = fix_note(fix)
        if note:
            lines += ["", note]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


__all__ = ["CHECKS", "CHECK_IDS", "Context", "render_markdown", "run", "run_all"]

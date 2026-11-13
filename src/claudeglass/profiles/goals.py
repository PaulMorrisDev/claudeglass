"""Create a profile from a goal: each goal names the settings that move
it, and :func:`draft` proposes values for them from the report's own
tables and recommendations, each with the value in effect now, the
evidence, the trade-off and a what-if estimate (:mod:`whatif`).

A change is ticked only when the data supports it; the rest are offered
unticked, so a goal never quietly makes a quality trade for you (the
main session's model, for one, is never pre-ticked).

The ``tasks`` goal needs metrics capture: it reads the Work habits
section's ``habits_setups`` table (the model and effort each kind of
task Claude reported ran on, and how often it went well) and drafts the
cheapest setup that went about as well as your usual one.
"""

from __future__ import annotations

from dataclasses import dataclass

from .. import habits, model_gate, model_swap, whatif
from ..capture_catalogue import TASK_LABELS, task_words
from ..compaction_sim import CompactionSimThresholds
from ..fixes import LEVER_LABELS, SETTING_TEXT, already_set
from ..recommend import _NOT_OVERRIDABLE, _SKIPS_CLAUDE_MD
from ..snapshots import AUTO_COMPACT_WINDOW_ENV
from ..units import Units
from . import catalogue
from .diff import _EFFECTIVE_AGENT_FIELD
from .schema import _EFFORT_LEVELS, _MODEL_ID_RE

TOP = whatif.TOP

#: A saving smaller than this share of what the change touches isn't
#: worth pre-ticking.
MIN_SHARE_PCT = 5.0
#: Thinking share (of output) above which a lower effort is offered.
THINKING_PCT = 20.0


@dataclass(frozen=True, slots=True)
class Goal:
    id: str
    title: str
    what: str


GOALS: tuple[Goal, ...] = (
    Goal(
        "recommendations",
        "Start from my recommendations",
        "Every setting change your current recommendations suggest, in one profile.",
    ),
    Goal(
        "subagents",
        "Spend less on subagents",
        "A cheaper model, less thinking, no CLAUDE.md or a different cache lifetime for the agents that cost most.",
    ),
    Goal(
        "models",
        "Cheaper models where it's safe",
        "The cheapest model each agent could move to, from its replies repriced at other models.",
    ),
    Goal(
        "cache",
        "Cheaper cache",
        "The cache lifetime (5 minutes or 1 hour) that would have cost least, for the main session and subagents.",
    ),
    Goal(
        "compaction",
        "Shorter conversations",
        "Summarise the conversation at the point that would have cost least, from your sessions replayed.",
    ),
    Goal(
        "thinking",
        "Less thinking where it isn't needed",
        "A lower effort for the main session or agents that spend a large share of their output thinking.",
    ),
    Goal(
        "tasks",
        "A profile for one kind of task",
        "The cheapest model and effort that went about as well as your usual setup, for one kind of task "
        "Claude reported. Needs metrics capture.",
    ),
    Goal(
        "current",
        "Start from my current settings",
        "Save the settings in effect now as a profile, to switch back to later or edit.",
    ),
)
GOAL_IDS = tuple(goal.id for goal in GOALS)


def _alias(model_id: str) -> str:
    """"claude-haiku-4-5-20251001" -> "haiku": the alias follows the
    newest model of that family."""
    for family in ("haiku", "sonnet", "opus", "fable"):
        if family in str(model_id):
            return family
    return str(model_id)


@dataclass(slots=True)
class _Draft:
    goal: Goal
    effective: dict
    effective_agents: dict
    candidates: list
    #: PROF-03/F3: whether CLAUDE_CODE_EFFORT_LEVEL is set for this
    #: session (never its value -- see hooks/snapshot-config.py's
    #: content_layers). It beats every settings.json effort lever, so an
    #: effortLevel candidate warns rather than implying the profile
    #: alone would move it.
    effort_level_env_set: bool = False
    #: Whether ANTHROPIC_MODEL is set: it wins over the model setting from
    #: any file, so no main-session model candidate is drafted.
    model_env_set: bool = False

    def now(self, key: str, agent: str | None):
        if agent is None:
            return self.effective.get(key)
        fields = self.effective_agents.get(agent)
        # effective_agents names its fields its own way (experimental_cache_ttl).
        return fields.get(_EFFECTIVE_AGENT_FIELD.get(key, key)) if isinstance(fields, dict) else None

    def add(self, key: str, agent: str | None, value, *, ticked: bool, evidence: str) -> None:
        if value is None or any(c["key"] == key and c["agent"] == agent for c in self.candidates):
            return
        # Started by Claude Code itself: no agent file can change them.
        if agent in _NOT_OVERRIDABLE or (key == "omitClaudeMd" and agent in _SKIPS_CLAUDE_MD):
            return
        now = self.now(key, agent)
        if now == value or already_set(key, value, now):
            return
        if key == "modelSettings" and isinstance(now, dict):
            # Only the models (and keys) the change names, not every model's entry.
            now = {
                model_id: {sub: now[model_id].get(sub) for sub in sub_values}
                for model_id, sub_values in value.items()
                if isinstance(now.get(model_id), dict)
            }
        what, tradeoff, note = SETTING_TEXT.get(key, ("", "", ""))
        if key == "modelSettings" and all(
            set(sub_values) == {"effortLevel"} for sub_values in value.values() if isinstance(sub_values, dict)
        ):
            # Only per-model effort levels: the flat effort level's trade-off applies.
            tradeoff = SETTING_TEXT["effortLevel"][1]
        self.candidates.append(
            {
                "key": key,
                "agent": agent,
                "label": LEVER_LABELS.get(key, key),
                "now": now,
                "value": value,
                "ticked": ticked,
                "evidence": evidence,
                "what": what,
                "tradeoff": tradeoff,
                "note": note,
            }
        )


def _share(saving, base) -> float:
    saving = whatif._num(saving) or 0.0
    base = whatif._num(base) or 0.0
    return 100.0 * saving / base if base > 0 else 0.0


def _from_recommendations(
    draft: _Draft, recommendations, keys: set[str] | None = None, *, subagents_only=False, skip_keys=frozenset()
):
    for rec in recommendations or ():
        # Ignored on the dashboard: left out of any profile drafted from them.
        if getattr(rec, "key", "") in skip_keys:
            continue
        for change in getattr(rec, "changes", ()) or ():
            if change.value is None or (keys is not None and change.key not in keys):
                continue
            if subagents_only and change.agent is None:
                continue
            draft.add(
                change.key,
                change.agent,
                change.value,
                # The main session's model is a quality trade that's yours to make.
                ticked=not (change.key == "model" and change.agent is None),
                evidence=f"Recommended: {rec.title}",
            )


def _models(draft: _Draft, tables, *, subagents_only: bool) -> None:
    gate = model_gate.build(tables)
    for row in tables.rows("model_swap", "model_swap_by_agent_type"):
        agent = row.get("agent_type")
        best = row.get("best_cheaper_alternative_model")
        pct = whatif._num(row.get("saving_pct")) or 0.0
        if not best or pct < MIN_SHARE_PCT or (subagents_only and agent == TOP):
            continue
        if agent == TOP and draft.model_env_set:
            # ANTHROPIC_MODEL picks the main session's model; the setting would do nothing.
            continue
        if agent == TOP and model_swap.at_main_floor(draft.now("model", None)):
            # A main-session setting at Sonnet or smaller is already there:
            # never offer to raise it.
            continue
        if gate.vetoed(agent, _alias(best)):
            # The quality check found this agent did worse on that model,
            # or its runs on it were often retried on a larger one; or
            # its work was mostly reported hard.
            continue
        who = "the main session" if agent == TOP else agent
        counted, total = whatif._num(row.get("lever_runs")), whatif._num(row.get("spawns"))
        if agent == TOP and counted is not None and total is not None and counted < total:
            evidence = (
                f"The replies of {int(counted):,} of the main session's {int(total):,} sessions in this window "
                f"would have cost {pct:.0f}% less on {best}."
            )
        elif agent == TOP or not _set_elsewhere(row):
            evidence = f"{who}'s replies in this window would have cost {pct:.0f}% less on {best}."
        else:
            evidence = (
                f"The replies of {agent}'s runs started without a model of their own would have cost "
                f"{pct:.0f}% less on {best} in this window."
            )
        draft.add(
            "model",
            None if agent == TOP else agent,
            _alias(best),
            ticked=agent != TOP and pct >= 20.0,
            evidence=evidence,
        )


def _set_elsewhere(row: dict) -> bool:
    """Whether a workflow script or a model named at spawn set the model
    for some of this model-swap row's runs. The row's saving is on the
    rest, the runs its agent file decides."""
    return bool((whatif._num(row.get("workflow_runs")) or 0) + (whatif._num(row.get("spawn_model_runs")) or 0))


def _file_runs(tables, agent: str) -> str:
    """How the task-agents evidence names what runs cheaper: the agent,
    or only its runs started without a model when others weren't."""
    row = tables.row("model_swap", "model_swap_by_agent_type", agent) or {}
    return f"{agent}'s runs started without a model of their own run" if _set_elsewhere(row) else f"{agent} runs"


def _opusplan(draft: _Draft, tables) -> None:
    """``model = "opusplan"`` (Opus while planning, Sonnet otherwise),
    offered unticked when the main session ran on Opus and built after
    approved plans. The model picker in the desktop app overrides the
    settings file, so the evidence says to choose it there."""
    top = tables.row("model_swap", "model_swap_by_agent_type", TOP)
    if top is None or _alias(str(top.get("observed_model") or "")) != "opus":
        return
    # With no main session counted (each already on Sonnet or smaller, or
    # from before their project moved there), the check says nothing is left
    # to price, so it offers no opusplan beside that.
    if "lever_runs" in top and not whatif._num(top.get("lever_runs")) and whatif._num(top.get("spawns")):
        return
    row = next(iter(tables.rows("plan_handoff", "plan_handoff_summary")), None) or {}
    build = row.get("build_usd")
    pct = _share((whatif._num(build) or 0.0) - (whatif._num(row.get("build_usd_sonnet")) or 0.0), build)
    if whatif._num(row.get("build_usd_sonnet")) is None or pct < MIN_SHARE_PCT:
        return
    draft.add(
        "model",
        None,
        "opusplan",
        ticked=False,
        evidence=(
            f"Plan on Opus, build on Sonnet: the replies after your approved plans would have cost {pct:.0f}% less "
            "on Sonnet. Sessions without a plan would run on Sonnet too. The model picker overrides this setting, "
            "so choose opusplan there or run /model opusplan."
        ),
    )


def _cache(draft: _Draft, tables, *, subagents_only: bool) -> None:
    rows = tables.rows("ttl", "ttl_by_agent_type")
    top = next((r for r in rows if r.get("agent_type") == TOP), None)
    if top is not None and not subagents_only:
        _cheapest_ttl(draft, "promptCacheTtl", None, [top], "the main session")
    subs = [r for r in rows if r.get("agent_type") != TOP]
    if subs and not subagents_only:
        _cheapest_ttl(draft, "subagentPromptCacheTtl", None, subs, "your subagents together")
    if subagents_only:
        for row in subs:
            _cheapest_ttl(draft, "experimental.cacheTtl", row.get("agent_type"), [row], row.get("agent_type"))


def _cheapest_ttl(draft: _Draft, key: str, agent: str | None, rows: list[dict], who: str) -> None:
    observed = sum(whatif._num(r.get("cost_observed")) or 0.0 for r in rows)
    costs = {ttl: sum(whatif._num(r.get(f"cost_all_{ttl}")) or 0.0 for r in rows) for ttl in ("5m", "1h")}
    best = min(costs, key=costs.get)
    pct = _share(observed - costs[best], observed)
    if pct < MIN_SHARE_PCT:
        return
    draft.add(
        key,
        agent,
        best,
        ticked=True,
        evidence=f"Every cache write of {who} replayed at {best} would have cost {pct:.0f}% less.",
    )


def _window_overridden(draft: _Draft) -> str:
    """While CLAUDE_CODE_AUTO_COMPACT_WINDOW is set it beats the
    autoCompactWindow setting (``snapshots.effective_config_in_force``
    names it), so a window candidate says so and isn't ticked: applying
    it writes a setting that changes nothing yet."""
    if draft.now(f"env.{AUTO_COMPACT_WINDOW_ENV}", None) is None:
        return ""
    return f" Won't apply while {AUTO_COMPACT_WINDOW_ENV} is set: it overrides this setting, so change the variable instead."


def _compaction(draft: _Draft, tables) -> None:
    rows = [r for r in tables.rows("compaction_sim", "compaction_sim_by_window") if whatif._num(r.get("cost"))]
    if not rows:
        return
    current = draft.now("autoCompactWindow", None)
    base = next((r for r in rows if str(r.get("window")).replace(",", "") == str(current)), None)
    base = base or next((r for r in rows if str(r.get("window")) == "none"), None)
    # Same limit as the compaction-window rule: a window that summarises
    # more often loses too much detail to suggest, however cheap.
    limit = CompactionSimThresholds().max_compactions_per_session
    best = min(
        (
            r
            for r in rows
            if str(r.get("window")) != "none" and (whatif._num(r.get("compactions_per_session")) or 0.0) <= limit
        ),
        key=lambda r: r["cost"],
        default=None,
    )
    if best is None or base is None:
        return
    pct = _share(base["cost"] - best["cost"], base["cost"])
    if pct < MIN_SHARE_PCT:
        return
    overridden = _window_overridden(draft)
    draft.add(
        "autoCompactWindow",
        None,
        int(str(best["window"]).replace(",", "")),
        ticked=not overridden,
        evidence=(
            f"Your sessions replayed with summaries at {best['window']} tokens cost {pct:.0f}% less, with about "
            f"{whatif._num(best.get('compactions_per_session')) or 0:.1f} summaries per session."
        )
        + overridden,
    )


#: PROF-11/F14: "You can't turn thinking off on Opus 5.5 or the Fable
#: models. The session toggle, alwaysThinkingEnabled, and
#: MAX_THINKING_TOKENS=0 have no effect there" (V26, model-config docs).
#: Effort still works there (V25 lists Opus 5.5's levels, and V13 says an
#: effort change keeps the cache on both), so the lower-effort candidate
#: is still drafted; its evidence just says the thinking toggles are no
#: way round it on these models. Substring match against the
#: model_swap table's own "observed model" label (id, plus an optional
#: "(+N more)" suffix when an agent type mixed models) -- "claude-opus-
#: 5-5" is exact (V26 names that generation only, not every Opus), and
#: both Fable ids ("claude-fable-5-1", "claude-fable-5") share the
#: "fable" substring.
_THINKING_ALWAYS_ON = ("claude-opus-5-5", "fable")


def _thinking_always_on(observed_model: str | None) -> bool:
    return bool(observed_model) and any(needle in observed_model for needle in _THINKING_ALWAYS_ON)


def _thinking(draft: _Draft, tables, *, subagents_only: bool) -> None:
    # model_swap_by_agent_type's own "observed model" column, keyed by
    # agent type -- the same join goals.py's model-swap veto (model_gate)
    # relies on report data for, rather than settings.json's configured
    # model, which can be stale or overridden per agent file.
    observed_models = {
        row.get("agent_type"): row.get("observed_model")
        for row in tables.rows("model_swap", "model_swap_by_agent_type")
    }
    for row in tables.rows("agents", "topology_effort_by_agent_type"):
        agent = row.get("agent_type")
        share = whatif._num(row.get("thinking_share")) or 0.0
        if share < THINKING_PCT or (subagents_only and agent == TOP):
            continue
        is_top = agent == TOP
        always_on = (
            " Thinking can't be switched off on this model, so alwaysThinkingEnabled and MAX_THINKING_TOKENS do "
            "nothing here; effort is the lever that still works."
            if _thinking_always_on(observed_models.get(agent)) else ""
        )
        evidence = (
            f"Thinking was {share:.0f}% of {'the main session' if is_top else agent}'s output. How much less "
            "a lower effort thinks isn't measured, so this isn't ticked for you." + always_on
        )
        # The main session's level is set for the model it ran on, the way
        # /effort saves it (v2.1.251 on). That entry works at every scope,
        # where Opus 5.5 and later ignore a user-level effortLevel. A
        # "(+N more)" label names its most-used model first. The entry is
        # keyed by the canonical id (Claude Code matches its dated, [1m],
        # Bedrock and Vertex spellings to it); a name that has none, such as
        # a gateway's own, gets the flat effortLevel, as with no model.
        observed = _main_model_id(observed_models.get(agent)) if is_top else None
        model_id = _canonical_model(observed)
        if model_id and not draft.effort_level_env_set:
            entry = _model_with_own_effort(draft, model_id) or model_id
            draft.add(
                "modelSettings", None, {entry: {"effortLevel": "medium"}}, ticked=False,
                evidence=evidence + f" It is set for {model_id}, the model this ran on, the way /effort saves it.",
            )
            continue
        draft.add(
            "effortLevel" if is_top else "effort",
            None if is_top else agent,
            "medium",
            ticked=False,
            evidence=evidence + _effort_override_note(draft, observed),
        )


def _main_model_id(observed: str | None) -> str | None:
    """The model id at the front of a model_swap "observed model" cell
    ("claude-opus-5 (+1 more)" gives "claude-opus-5"), or ``None``."""
    model_id = str(observed or "").split(" (+")[0].strip()
    return model_id or None


def _canonical_model(model: str | None) -> str | None:
    """``model`` as the canonical id a ``modelSettings`` key is written in:
    without its ``[1m]`` tag and date suffix (``calibration.model_family``)
    and its Bedrock or Vertex wrapping (``pricing._strip_cloud_provider``),
    lower case. ``None`` when that is not a Claude model id the profile
    schema accepts (an alias, a gateway's own name)."""
    from ..calibration import model_family
    from ..pricing import _strip_cloud_provider

    name = model_family(_strip_cloud_provider(model_family(model)))
    return name if name.startswith("claude-") and _MODEL_ID_RE.match(name) else None


def _omit_claude_md(draft: _Draft, tables) -> None:
    # Metrics capture: what each agent type's runs said about CLAUDE.md.
    said = {row.get("agent_type"): row for row in tables.rows("habits", "habits_agents")}
    for row in tables.rows("agent_startup", "agent_startup_breakdown"):
        agent = row.get("agent_type")
        # PROF-11/F13: Managed policy CLAUDE.md still loads regardless of
        # omitClaudeMd, so it never counts towards what this would save.
        managed = whatif._num(row.get("claude_md_managed")) or 0.0
        tokens = max(0.0, (whatif._num(row.get("claude_md")) or 0.0) - managed)
        if tokens < 1000 or agent == TOP:
            continue
        told = said.get(agent) or {}
        used = int(whatif._num(told.get("rules_used")) or 0)
        unused = int(whatif._num(told.get("rules_unused")) or 0)
        if used > unused:
            # Most of its runs that said, said they used it.
            continue
        evidence = f"About {round(tokens):,} CLAUDE.md tokens at each of {int(whatif._num(row.get('spawns')) or 0)} spawns."
        if managed:
            evidence += f" Managed policy CLAUDE.md ({round(managed):,} tokens) still loads either way."
        if unused and used + unused == 1:
            evidence += " The one run that said, said it didn't use it."
        elif unused:
            evidence += f" {unused} of the {used + unused} runs that said, said they didn't use it."
        else:
            evidence += " Not ticked: move the rules it needs into its agent file first."
        draft.add("omitClaudeMd", agent, True, ticked=unused > used, evidence=evidence)


def _setups_by_task(tables) -> dict[str, list[dict]]:
    """``habits_setups``' all-levels rows per kind of task, the most-used
    task first (the table's own order)."""
    by_task: dict[str, list[dict]] = {}
    for row in tables.rows("habits", "habits_setups"):
        if row.get("level") == "all" and row.get("task"):
            by_task.setdefault(str(row["task"]), []).append(row)
    return by_task


def _setup_text(row: dict) -> str:
    effort = row.get("effort")
    return f"{row.get('model')}" + (f" at {effort} effort" if effort and effort != "default" else "")


def _effort_override_note(draft: _Draft, model_id: str | None) -> str:
    """F3/PROF-03: ``CLAUDE_CODE_EFFORT_LEVEL`` being set at all
    (``hooks/snapshot-config.py``'s ``content_layers``) beats the
    top-level ``effortLevel`` scalar a profile's ``effortLevel`` change
    would set, for every model. A candidate for it says so plainly rather
    than implying the profile alone will move it: applying it still
    writes the setting (it's a legitimate default for every other model),
    but this one needs the variable changed: it beats ``--effort`` and
    ``/effort`` too (model-config docs). (A model's own
    ``modelSettings`` effort also beats the scalar, for that one model;
    :func:`_model_with_own_effort` finds it and the draft offers that
    entry instead.)"""
    if not model_id:
        return ""
    if draft.effort_level_env_set:
        model_id = _canonical_model(model_id) or model_id
        return (
            f" Won't apply to {model_id}: CLAUDE_CODE_EFFORT_LEVEL is set and wins over every effort setting. "
            "Set it to medium or remove it."
        )
    return ""


def _model_with_own_effort(draft: _Draft, model_id: str | None) -> str | None:
    """The key, as written in the settings, of the ``modelSettings`` entry
    that gives ``model_id``'s model its own effort level, or ``None``:
    without one, or while ``CLAUDE_CODE_EFFORT_LEVEL`` is set (it beats
    that entry too), the plain ``effortLevel`` is the candidate."""
    if not model_id or draft.effort_level_env_set:
        return None
    model_settings = draft.effective.get("modelSettings")
    if isinstance(model_settings, dict):
        for key, entry in model_settings.items():
            # A key the profile schema refuses could not be saved or applied.
            if (isinstance(entry, dict) and entry.get("effortLevel") and _MODEL_ID_RE.match(str(key))
                    and _same_model(str(key), model_id)):
                return str(key)
    return None


def _same_model(key: str, model_id: str) -> bool:
    """Whether a ``modelSettings`` key names ``model_id``'s model: the same
    canonical id (:func:`_canonical_model`, so a dated or ``[1m]`` spelling
    of either matches), or a bare family alias ("opus"). Another version
    of the family is a different entry."""
    canonical = _canonical_model(key)
    if canonical is not None and canonical == _canonical_model(model_id):
        return True
    key, model_id = key.strip().lower(), model_id.strip().lower()
    return key == _alias(model_id) or model_id == _alias(key)


def _tasks(draft: _Draft, tables, task: str | None) -> tuple[list[str], str | None, str]:
    """The main session's model and effort for ``task`` (or, without
    one, the first kind of task that has a cheaper setup): the kinds of
    task there are, the one drafted, and a note."""
    by_task = _setups_by_task(tables)
    tasks = list(by_task)
    if not tasks:
        return [], None, (
            "No kind of task has been reported yet. Turn on metrics capture at Essentials or above on "
            "{{page:setup/capture}}, then come back after a week or so of work."
        )
    if task not in by_task:
        task = next((t for t in tasks if any(r.get("verdict") == "cheaper" for r in by_task[t])), tasks[0])
    rows = by_task[task]
    usual = next((r for r in rows if r.get("verdict") == "usual"), rows[0])
    cheaper = next((r for r in rows if r.get("verdict") == "cheaper"), None)
    if cheaper is None:
        note = (
            f"Your usual setup for {task_words(task)} work is {_setup_text(usual)}. No cheaper setup went as well "
            f"over at least {habits.MIN_GROUP} messages yet."
        )
    else:
        evidence = (
            f"For {task_words(task)} work, {_setup_text(cheaper)} cost "
            f"{whatif._num(cheaper.get('saving_pct')) or 0:.0f}% less a message than your usual {_setup_text(usual)}, "
            f"and went well {whatif._num(cheaper.get('ok_pct')) or 0:.0f}% of the time against "
            f"{whatif._num(usual.get('ok_pct')) or 0:.0f}% ({int(whatif._num(cheaper.get('cycles')) or 0)} and "
            f"{int(whatif._num(usual.get('cycles')) or 0)} messages), compared level for level. They still ran on "
            "different work, so it's a lead, not proof."
        )
        # habits_setups' "model" column is the full resolved id (split by
        # version -- F6); the draft, like every other model candidate,
        # takes the portable family alias.
        cheaper_model = _alias(cheaper.get("model") or "")
        if cheaper_model != _alias(usual.get("model") or "") and cheaper_model in habits._FAMILIES:
            draft.add("model", None, cheaper_model, ticked=False, evidence=evidence)
        if cheaper.get("effort") != usual.get("effort") and cheaper.get("effort") in _EFFORT_LEVELS:
            # PROF-04: only ticked once the cheaper setup has enough
            # messages behind it to trust, not just enough to show.
            tick = (whatif._num(cheaper.get("cycles")) or 0) >= habits.TICK_MIN_GROUP
            own = _model_with_own_effort(draft, cheaper.get("model"))
            if own is not None:
                # That model's own entry beats the plain effortLevel, so change the entry.
                draft.add("modelSettings", None, {own: {"effortLevel": cheaper["effort"]}}, ticked=tick, evidence=evidence)
            else:
                draft.add(
                    "effortLevel", None, cheaper["effort"], ticked=tick,
                    evidence=evidence + _effort_override_note(draft, cheaper.get("model")),
                )
        note = f"Save it, then launch Claude with it when you start {task_words(task)} work."
    profile_id = catalogue.task_profile(task)
    if profile_id is not None and not _catalogue_conflicts(draft, profile_id):
        note += f" The catalogue profile {profile_id} is also a starting point for this kind of task."
    return tasks, task, note


def _catalogue_conflicts(draft: _Draft, profile_id: str) -> bool:
    """PROF-11/F12: True when ``profile_id``'s own settings disagree with
    a main-session candidate this draft already proposed for the same
    key -- citing it as "a starting point" right under that candidate
    would then contradict the draft above it."""
    profile = catalogue.get(profile_id)
    if profile is None:
        return False
    for candidate in draft.candidates:
        if candidate["agent"] is not None:
            continue
        catalogue_value = profile.settings.get(candidate["key"])
        if catalogue_value is not None and catalogue_value != candidate["value"]:
            return True
    return False


def _task_compaction(draft: _Draft, tables, task: str) -> None:
    """EST-P8: ``task``'s own best ``autoCompactWindow``, from the
    compaction sweep's per-task split (``compaction_sim_by_task``, which
    only lists a task at least ``compaction_sim.MIN_TASK_SESSIONS`` main
    sessions reported). That split keeps no per-task summary count, so
    the corpus-wide ``compaction_sim_by_window`` count for the same
    window stands in for the rule's compactions-per-session limit, as in
    :func:`_compaction`. Ticked only once the task has
    ``habits.TICK_MIN_GROUP`` sessions behind it (PROF-04)."""
    row = next((r for r in tables.rows("compaction_sim", "compaction_sim_by_task") if r.get("task") == task), None)
    if row is None or str(row.get("best_window")) == "none":
        return
    saving_pct = -(whatif._num(row.get("delta_pct")) or 0.0)
    if saving_pct < MIN_SHARE_PCT:
        return
    window = str(row.get("best_window")).replace(",", "")
    overall = next(
        (r for r in tables.rows("compaction_sim", "compaction_sim_by_window")
         if str(r.get("window")).replace(",", "") == window),
        None,
    )
    per_session = whatif._num(overall.get("compactions_per_session")) if overall else None
    if per_session is None or per_session > CompactionSimThresholds().max_compactions_per_session:
        return
    sessions = int(whatif._num(row.get("sessions")) or 0)
    overridden = _window_overridden(draft)
    draft.add(
        "autoCompactWindow",
        None,
        int(window),
        ticked=sessions >= habits.TICK_MIN_GROUP and not overridden,
        evidence=(
            f"Your {sessions} {task_words(task)} sessions replayed with summaries at {row.get('best_window')} tokens cost "
            f"{saving_pct:.0f}% less."
        )
        + overridden,
    )


def _task_agents(draft: _Draft, tables, task: str) -> None:
    """Cheaper-model candidates for the agent types that most often
    answered ``task``'s work (metrics capture's ``habits_agents_by_task``),
    vetoed exactly as ``_models`` vetoes its corpus-wide draft: a setup
    the quality check found worse, one often retried for the model, an
    agent whose runs were mostly hard work or retried for the model
    (``habits.unfit_agents``), or this task's own slice of its runs
    being mostly hard or retried for the model (``model_gate``, all wired through
    ``habits.habits_agents_by_task``'s ``cheaper_model`` column, which
    is already ``None`` when that row's own per-task veto fired -- see
    ``habits._agents_by_task_table``). F8: ``cheaper_model``/
    ``cheaper_saving_pct`` are ``_model_swap_alt``'s corpus-wide verdict for
    the agent type (every task it ran, not just this one) -- only the
    veto and the run count are task-specific -- so the evidence says so,
    rather than implying the saving percentage was measured on this
    task's own runs alone."""
    gate = model_gate.build(tables)
    rows = [r for r in tables.rows("habits", "habits_agents_by_task") if r.get("task") == task]
    for row in sorted(rows, key=lambda r: -(whatif._num(r.get("runs")) or 0.0)):
        agent = row.get("agent_type")
        best = row.get("cheaper_model")
        pct = whatif._num(row.get("cheaper_saving_pct")) or 0.0
        if not agent or not best or pct < MIN_SHARE_PCT:
            continue
        if gate.vetoed(agent, best):
            continue
        runs = int(whatif._num(row.get("runs")) or 0)
        draft.add(
            "model",
            agent,
            best,
            ticked=pct >= 20.0,
            evidence=(
                f"{_file_runs(tables, agent)} {pct:.0f}% cheaper on {best} across every task it did; "
                f"{task_words(task)} work was {runs} of its runs in this window."
            ),
        )


def _task_share(tables, task: str) -> float | None:
    """This task's share (%) of the *main session's* cost in the window,
    from ``habits_by_task``'s ``main_cost`` column against the ``all``
    row's. Used to scale down a main-session estimate (a model or
    effortLevel change to the top-level settings) that reprices the
    whole window (``whatif`` has no notion of a task). ``main_cost``,
    not ``cost``, on purpose (F7): the reprice being scaled only ever
    covers the main session's own share of the work, never the
    subagents ``cost`` also counts, so scaling it by a share of the
    *whole* piece of work (main and subagents together) would be
    scaling a main-only number by the wrong denominator. ``None``
    without the data to compare."""
    by_task = {r.get("task"): r for r in tables.rows("habits", "habits_by_task")}
    task_row, all_row = by_task.get(task), by_task.get("all")
    total = whatif._num(all_row.get("main_cost")) if all_row else None
    if task_row is None or not total:
        return None
    return 100.0 * (whatif._num(task_row.get("main_cost")) or 0.0) / total


def _task_agent_share(tables, task: str, agent: str) -> float | None:
    """This task's share (%) of ``agent``'s total cost, from
    ``habits_agents_by_task`` (runs at this task, times its cost per
    run) against ``habits_agents``' own total. ``None`` without the data
    to compare."""
    row = next(
        (r for r in tables.rows("habits", "habits_agents_by_task") if r.get("task") == task and r.get("agent_type") == agent),
        None,
    )
    total_row = tables.row("habits", "habits_agents", agent)
    total = whatif._num(total_row.get("cost")) if total_row else None
    if row is None or not total:
        return None
    task_cost = (whatif._num(row.get("runs")) or 0.0) * (whatif._num(row.get("avg_cost")) or 0.0)
    return 100.0 * task_cost / total


def _scale_estimate(row: dict, pct: float | None, units: Units, period: str, whose: str = "this task's") -> dict:
    """A ``whatif`` row reprices *all* of a setup's observed work in the
    window, but a task's draft is for that task's share of it alone.
    Scale the saving down to ``pct``; without a clean share to scale by,
    drop the number rather than leave the unscaled (too large) one.
    ``uncalibrated_usd`` (EST-P6, set only when calibration adjusted the
    row) is scaled the same way, so a task-scoped ``/api/whatif`` call
    that also logs a prediction (PROF-01) logs the right raw figure --
    not the whole window's."""
    row = dict(row)
    if row.get("saving_usd") is None:
        return row
    if pct is None:
        row["saving_usd"] = None
        row["fidelity"] = "none"
        row["effect_text"] = "Not estimated"
        row["basis"] = "Not estimated: no per-task cost to scale this window's reprice by."
        return row
    row["saving_usd"] = round(row["saving_usd"] * pct / 100.0, 6)
    # The saving is now a share of a task's cost, not of the figure the row
    # was priced on across the whole window.
    row["priced_on"] = None
    if row.get("uncalibrated_usd") is not None:
        row["uncalibrated_usd"] = round(row["uncalibrated_usd"] * pct / 100.0, 6)
    row["effect_text"] = whatif._effect_text(row["saving_usd"], units, period)
    row["basis"] = row.get("basis", "") + f" Scaled to {whose} {pct:.0f}% share of what was repriced above."
    return row


def _task_share_for(tables, task: str, agent: str | None) -> float | None:
    return _task_share(tables, task) if agent is None else _task_agent_share(tables, task, agent)


def _tasks_share_for(tables, tasks: tuple[str, ...], agent: str | None) -> float | None:
    """The combined share of several tasks (a catalogue profile's ``for``
    covers more than one): each is a share of the same total, so they
    add. A task with no row of its own adds nothing; ``None`` only when
    none of them has a share to give."""
    shares = [share for share in (_task_share_for(tables, task, agent) for task in tasks) if share is not None]
    return sum(shares) if shares else None


def _scale_whatif(result: dict, tables, task: str | tuple[str, ...], units: Units, period: str) -> dict:
    """Scale every row of a combined ``whatif.estimate`` result to
    ``task``'s share (or the combined share of several tasks), and
    recompute the total from the scaled rows."""
    tasks = (task,) if isinstance(task, str) else tuple(task)
    whose = "this task's" if len(tasks) == 1 else "these tasks'"
    rows = [
        _scale_estimate(row, _tasks_share_for(tables, tasks, row.get("agent")), units, period, whose)
        for row in result["rows"]
    ]
    estimated = [row for row in rows if row["saving_usd"] is not None]
    total = sum(row["saving_usd"] for row in estimated)
    out = dict(result)
    out["rows"] = rows
    out["total_usd"] = round(total, 6)
    out["total_text"] = whatif._effect_text(total, units, period) if estimated else ""
    out["estimated"] = len(estimated)
    out["not_estimated"] = len(rows) - len(estimated)
    return out


def draft(
    goal_id: str,
    model,
    units: Units,
    *,
    effective: dict | None = None,
    effective_agents: dict | None = None,
    period: str = "",
    task: str | None = None,
    effort_level_env_set: bool = False,
    skip_keys: frozenset[str] = frozenset(),
    model_env_set: bool = False,
) -> dict:
    """The candidate changes for ``goal_id``, each with its what-if row.
    Raises ``KeyError`` for an unknown goal. ``task``: for the ``tasks``
    goal, the kind of task to draft for (the first with a cheaper setup
    when it's missing or not in the data). ``effort_level_env_set``:
    PROF-03, see ``_Draft``. ``skip_keys``: the keys of recommendations
    ignored on the dashboard (``ignores.skip_keys``), whose changes are
    left out. ``model_env_set``: see ``_Draft``."""
    goal = next(g for g in GOALS if g.id == goal_id) if goal_id in GOAL_IDS else None
    if goal is None:
        raise KeyError(goal_id)
    tables = whatif._Tables(model)
    d = _Draft(goal, dict(effective or {}), dict(effective_agents or {}), [], effort_level_env_set, model_env_set)
    reading = model_swap.main_setting(model)
    if reading is not None:
        # The main session's model setting as the saving reads it, so a
        # candidate never names a current value the saving disagrees with.
        d.effective["model"] = reading[0]
    recommendations = getattr(model, "recommendations", ()) or ()
    tasks: list[str] = []
    note = None
    if goal.id == "tasks":
        tasks, task, note = _tasks(d, tables, task)
        if task is not None:
            _task_compaction(d, tables, task)
            _task_agents(d, tables, task)
    else:
        task = None
    if goal.id == "recommendations":
        _from_recommendations(d, recommendations, skip_keys=skip_keys)
    elif goal.id == "subagents":
        _from_recommendations(d, recommendations, {"model", "omitClaudeMd", "effort", "experimental.cacheTtl"},
                              subagents_only=True, skip_keys=skip_keys)
        _models(d, tables, subagents_only=True)
        _thinking(d, tables, subagents_only=True)
        _cache(d, tables, subagents_only=True)
        _omit_claude_md(d, tables)
    elif goal.id == "models":
        # Before the recommendations: the main session has one model
        # setting, and opusplan keeps Opus for the planning.
        _opusplan(d, tables)
        _from_recommendations(d, recommendations, {"model"}, skip_keys=skip_keys)
        _models(d, tables, subagents_only=False)
    elif goal.id == "cache":
        _cache(d, tables, subagents_only=False)
    elif goal.id == "compaction":
        _compaction(d, tables)
    elif goal.id == "thinking":
        _thinking(d, tables, subagents_only=False)
    for candidate in d.candidates:
        settings, agents = _as_profile([candidate])
        estimate = whatif.estimate(settings, agents, model, units, period=period, current=d.effective)["rows"][0]
        if goal.id == "tasks" and task is not None:
            # whatif reprices all of that setup's work in the window;
            # a task's own draft only covers its share of it.
            estimate = _scale_estimate(estimate, _task_share_for(tables, task, candidate["agent"]), units, period)
        candidate["estimate"] = estimate
    ticked = [c for c in d.candidates if c["ticked"]]
    settings, agents = _as_profile(ticked)
    combined = whatif.estimate(settings, agents, model, units, period=period, current=d.effective)
    if goal.id == "tasks" and task is not None:
        combined = _scale_whatif(combined, tables, task, units, period)
    return {
        "goal": {"id": goal.id, "title": goal.title, "what": goal.what},
        "period": period,
        "from_current": goal.id == "current",
        "tasks": tasks,
        # Each task word's plain name, for the "Kind of task" picker.
        "task_labels": {t: TASK_LABELS.get(t, t) for t in tasks},
        "task": task,
        "note": note,
        "candidates": d.candidates,
        "profile": {"settings": settings, "agents": agents},
        "whatif": combined,
    }


def _as_profile(candidates: list[dict]) -> tuple[dict, dict]:
    settings: dict = {}
    agents: dict = {}
    for c in candidates:
        if c["agent"] is None and c["key"] == "modelSettings":
            # One entry per model, so candidates for different models (or keys) join.
            merged = settings.setdefault("modelSettings", {})
            for model_id, sub_values in c["value"].items():
                merged[model_id] = {**merged.get(model_id, {}), **sub_values}
        elif c["agent"] is None:
            settings[c["key"]] = c["value"]
        else:
            agents.setdefault(c["agent"], {})[c["key"]] = c["value"]
    return settings, agents


def goals_list() -> list[dict]:
    return [{"id": g.id, "title": g.title, "what": g.what} for g in GOALS]


__all__ = ["GOALS", "GOAL_IDS", "draft", "goals_list"]

"""The plain-language layer over :func:`recommend.recommend`'s rules.

The rules decide *whether* something is worth saying and cite the table
cells that prove it. This module decides *how it is said* and what to
change, in one place, so every card reads the same way:

- **Consolidation.** Rules that look at the same setting from different
  angles can disagree. Once the compaction replay has a verdict on
  ``autoCompactWindow`` (``compaction_sim_by_window`` priced your main
  sessions at the candidate windows), that verdict is the one answer on
  the setting, whether it is ``compaction-window`` (a modelled sweep that
  weighs both sides) or no window worth setting: ``compaction-churn``
  ("summaries happen too often", raise it) is dropped and
  ``long-context-share`` ("the context is too large", lower it) keeps
  only its workflow advice. Without a replay, ``compaction-churn`` keeps
  the setting and ``long-context-share`` still gives it up, so the two
  never point it opposite ways.
  ``model-tier`` fires once per agent type; its subagent cards are merged
  into one with a change per agent type, largest saving first. The main
  session's gets a card of its own, ``model-tier-main``, ranked last among
  its severity: its model is a quality trade that's yours to make.
- **Plain words.** Each recommendation gets a plain title, an action, a
  ``why`` sentence and, where a setting is involved, ``changes``
  (:class:`~claudeglass.model.SettingChange`) with the current
  value from the latest config snapshot.
- **Agent models.** ``agent-model-inherited``, ``agent-model-asked`` and
  ``agent-decide-apply`` (``agent_models.RULES``) carry only numbers in
  their evidence; the card's words are written here from it. They have no
  setting to change, so ``NOT_OVERRIDABLE`` and the already-applied pass
  leave them alone: the fix is a prompt (``fixes``).
- **Already applied.** The rules measure the whole period, so a change
  made part-way through it would still be offered at its full saving. A
  change the current config already makes is left out, and a card with
  nothing left to change is dropped.
- **Amounts** follow the billing mode (:class:`~claudeglass.units.Units`).

Titles and actions are rewritten here, not in the rules, so the rules'
own modules (and their unit tests) keep their wording and evidence.
House style: ``docs/writing-help.md``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Callable

from . import cost_centres, known_savers, model_gate, model_swap, whatif
from .fixes import BATCH_PROBES_LINE, CRITIQUE_PLAN_LINE, already_set
from .limits import BURST_AGENTS, SPEND_SOURCES, burst_stands_out
from .model import Recommendation, ReportModel, SettingChange
from .pricing import model_names_in, newer_version_of
from .snapshots import (
    AUTO_COMPACT_WINDOW_ENV,
    Snapshot,
    auto_compact_window,
    auto_compact_window_env_set,
    effective_config,
)
from .units import NO_LIMIT_SHARE_HINT, Units

#: Agent types Claude Code starts itself (workflow scripts, forks): no
#: agent file can change them, so no per-agent change is offered.
NOT_OVERRIDABLE = frozenset({"workflow-subagent", "fork", "unknown", "(unknown)"})

#: The baseline-bloat card suggests shortening the ``description`` lines of
#: your own agents only when their listing is at least this many tokens of
#: a session's start (``context_budget``'s custom agents estimate).
CUSTOM_AGENTS_TRIM_TOKENS = 1_000.0

_PERIOD = "over the period in this report"
_UNKNOWN = "unknown (no config snapshot yet)"


@dataclass(slots=True)
class _Context:
    report: ReportModel
    snapshot: Snapshot | None
    units: Units | None

    def cell(self, section_key: str, table_name: str, row_key, column_key: str):
        for section in self.report.sections:
            if section.key != section_key:
                continue
            for table in section.tables:
                if table.name != table_name:
                    continue
                keys = [c.key for c in table.columns]
                if column_key not in keys:
                    return None
                idx = keys.index(column_key)
                for row in table.rows:
                    if row and row[0] == row_key and idx < len(row):
                        return row[idx]
        return None

    def setting_now(self, key: str):
        if self.snapshot is None:
            return _UNKNOWN
        return effective_config(self.snapshot).get(key)

    def window_now(self):
        """The auto-compact window in force: CLAUDE_CODE_AUTO_COMPACT_WINDOW,
        while it's set, overrides the setting."""
        if self.snapshot is None:
            return _UNKNOWN
        if auto_compact_window_env_set(self.snapshot):
            window = auto_compact_window(self.snapshot)
            return window if window is not None else f"set by {AUTO_COMPACT_WINDOW_ENV} (value not recorded)"
        return self.setting_now("autoCompactWindow")

    def agent_entry(self, agent_type: str) -> dict | None:
        if self.snapshot is None:
            return None
        agents = self.snapshot.data.get("agents")
        entry = agents.get(agent_type) if isinstance(agents, dict) else None
        return entry if isinstance(entry, dict) else None

    def agent_now(self, agent_type: str, key: str):
        if self.snapshot is None:
            return _UNKNOWN
        entry = self.agent_entry(agent_type)
        return entry.get(key) if entry is not None else None

    def agent_scope(self, agent_type: str) -> tuple[str, bool]:
        """``(scope, has_file)`` for an agent-file change."""
        entry = self.agent_entry(agent_type)
        if entry is not None:
            return ("repo" if entry.get("source") == "project" else "user"), True
        from .recommend import _agent_has_frontmatter

        return "user", _agent_has_frontmatter(agent_type, self.snapshot)

    def money(self, usd, *, prefix: str = "") -> str:
        if self.units is None or not isinstance(usd, (int, float)) or usd <= 0:
            return ""
        amount = self.units.money(float(usd), period=_PERIOD)
        if amount is None:
            return ""
        # UX-2: Amount.phrase avoids "About about X% of your weekly
        # usage limit" -- a subscription's own share text already opens
        # with "about" (units.Units.money), so a plain f"{prefix}{...}"
        # concatenation here used to double it (finding F3).
        text = f"{amount.phrase(prefix)}."
        return text[:1].upper() + text[1:]

    def basis(self, text: str) -> str:
        if self.units is None:
            return text
        probe = self.units.money(1.0)
        if probe is not None and probe.basis == NO_LIMIT_SHARE_HINT:
            return f"{text} {probe.basis}"
        return text


def _evidence_value(rec: Recommendation, label_part: str):
    for label, value, _table, _row in rec.evidence:
        if label_part in label:
            return value
    return None


def _tokens(value) -> str:
    return f"{value:,.0f}" if isinstance(value, (int, float)) else str(value)


def _family_alias(model_id: str) -> str:
    """``sonnet``/``haiku``/``opus``/``fable`` for a model id of that
    family, so an agent follows the family's current model; any other id
    as is."""
    for family in ("haiku", "sonnet", "opus", "fable"):
        if family in model_id:
            return family
    return model_id


def _int(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _who(agent_type: str) -> str:
    return "your main session" if agent_type == "top-level" else agent_type


def _percent(_saving, rec: Recommendation) -> str:
    pct = _evidence_value(rec, "Ceiling saving (%)")
    return f"up to {pct:.0f}% less" if isinstance(pct, (int, float)) else "less"


def _model_prose(observed: str) -> str:
    """A model-swap ``observed_model`` cell (``claude-opus-5 (+1 more)``)
    as prose: ``Opus 5 and 1 other model``."""
    text = model_names_in(observed)
    more = re.search(r" \(\+(\d+) more\)$", text)
    if more:
        count = int(more.group(1))
        text = text[: more.start()] + f" and {count} other model{'s' if count != 1 else ''}"
    return text


#: The groups the model-tier card's "left out" note lists, in order:
#: key and the words that introduce the group.
_LEFT_OUT_GROUPS = (
    ("worse", "Did worse on a cheaper model"),
    ("retried", "Edits often redone on a larger model"),
    ("hard", "Much of the work reported hard"),
    ("retried-model", "Run again because the model wasn't enough"),
)

#: At most this many agents are named per group; the rest are counted.
_LEFT_OUT_NAMED = 3


def _left_out_note(left_out: list[tuple[str, str]]) -> str:
    """The model-tier card's note on who was left out and why: one short
    sentence, then one per reason with a few named agents each, so the
    note stays short however many agents it covers. ``left_out`` holds
    ``(group key, "agent (detail)")`` pairs."""
    if not left_out:
        return ""
    parts = [" Some agents were left out because a cheaper model may not be enough."]
    for key, words in _LEFT_OUT_GROUPS:
        names = [who for group, who in left_out if group == key]
        if not names:
            continue
        shown = names[:_LEFT_OUT_NAMED]
        if len(names) > len(shown):
            listed = ", ".join(shown) + f" and {len(names) - len(shown)} more"
        elif len(shown) > 1:
            listed = ", ".join(shown[:-1]) + " and " + shown[-1]
        else:
            listed = shown[0]
        parts.append(f" {words}: {listed}.")
    return "".join(parts)


# -- consolidation -------------------------------------------------------------


def _merge_model_tier(recs: list[Recommendation], ctx: _Context) -> list[Recommendation]:
    tier = [r for r in recs if r.id == "model-tier"]
    if not tier:
        return recs
    rest = [r for r in recs if r.id != "model-tier"]
    tables = whatif._Tables(ctx.report)
    # Metrics capture: agents whose work was mostly reported hard, or that
    # were retried for the model. A veto only: nothing a run reports adds
    # a suggestion.
    worse, retried, _unfit = model_gate.raw(tables)
    unfit = model_gate.unfit_kinds(tables)
    # The main session's model is a quality trade that's yours to make, so
    # it gets a card of its own, ranked after every other advice card
    # (finish's sort); subagents share the merged card.
    left_out: dict[bool, list[tuple[str, str]]] = {True: [], False: []}
    rows = []
    for rec in tier:
        agent = rec.agent_type or "top-level"
        if agent in NOT_OVERRIDABLE:
            continue
        alt = ctx.cell("model_swap", "model_swap_by_agent_type", agent, "best_cheaper_alternative_model")
        saving = ctx.cell("model_swap", "model_swap_by_agent_type", agent, "saving_usd")
        # The model on the runs the setting decides (the saving is theirs).
        observed = ctx.cell("model_swap", "model_swap_by_agent_type", agent, "lever_model") or ctx.cell(
            "model_swap", "model_swap_by_agent_type", agent, "observed_model"
        )
        if not alt:
            continue
        now = ctx.setting_now("model") if agent == "top-level" else ctx.agent_now(agent, "model")
        if already_set("model", _family_alias(alt), now):
            # Already on the cheaper model; the saving is from before the change.
            continue
        family = _family_alias(alt)
        main = agent == "top-level"
        if agent in unfit:
            kind, figure = unfit[agent]
            detail = f"{figure:.0f}%" if kind == "hard" else f"{figure} run{'s' if figure != 1 else ''}"
            left_out[main].append((kind, f"{_who(agent)} ({detail})"))
            continue
        if (agent, family) in worse:
            # The quality section found this agent did worse on that model.
            left_out[main].append(("worse", f"{_who(agent)} ({family.capitalize()})"))
            continue
        if (agent, family) in retried:
            # Its runs on that model were often retried on a larger one.
            row = retried[(agent, family)]
            left_out[main].append(
                ("retried", f"{_who(agent)} ({row.get('retried') or 0} of {row.get('runs') or 0} "
                 f"{family.capitalize()} runs)")
            )
            continue
        rows.append((rec, agent, alt, saving if isinstance(saving, (int, float)) else 0.0, observed))
    rows.sort(key=lambda r: -r[3])
    main_rows = [r for r in rows if r[1] == "top-level"]
    agent_rows = [r for r in rows if r[1] != "top-level"]
    out = list(rest)
    if agent_rows:
        out.append(_agent_tier_card(agent_rows, left_out[False], tier, ctx))
    if main_rows:
        out.append(_main_tier_card(main_rows[0], left_out[True], ctx))
    return out


_TIER_BASIS = (
    "Worked out by pricing the same tokens at the cheaper model's list price. A smaller model may "
    "need more replies or fail some tasks, so this is a ceiling, not a forecast."
)


def _main_tier_card(row: tuple, left_out: list[tuple[str, str]], ctx: _Context) -> Recommendation:
    """The main session's model, one tier down. ``model_swap`` never
    suggests Haiku here (its ``main_floor`` verdict), so this is only ever
    Opus to Sonnet or Fable to Opus."""
    rec, _agent, alt, saving, observed = row
    family = _family_alias(alt).capitalize()
    current = ctx.setting_now("model")
    change = SettingChange(
        target="settings",
        key="model",
        value=_family_alias(alt),
        current=current if current not in (None, _UNKNOWN) else f"not set (used {observed or 'unknown'})",
        note="This changes the model for your main session in every project.",
        scope=_advice_scope(rec.scope),
        saving=ctx.money(saving, prefix="At most "),
    )
    return Recommendation(
        id="model-tier-main",
        severity="advice",
        category="settings",
        title=f"Your main session could run on {family}",
        why=(
            f"Your main session ran on {_model_prose(observed) if observed else 'a larger model'}, and the same "
            f"work priced at {family} would cost {_percent(saving, rec)}. It's a quality trade, so it's yours "
            "to make."
        )
        + _left_out_note(left_out),
        action=(
            f"Try {family} for a session or two of routine work (/model {_family_alias(alt)}) and compare the "
            "results before making it your default."
        ),
        lever="model",
        scope=rec.scope,
        evidence=list(rec.evidence),
        changes=[change],
        estimated_saving=ctx.money(saving, prefix="At most "),
        saving_basis=ctx.basis(_TIER_BASIS),
        saving_usd=saving,
    )


def _agent_tier_card(
    rows: list[tuple], left_out: list[tuple[str, str]], tier: list[Recommendation], ctx: _Context
) -> Recommendation:
    changes = []
    evidence = []
    set_elsewhere = False
    for rec, agent, alt, saving, observed in rows:
        evidence.extend(rec.evidence)
        now = observed or "unknown"
        scope, has_file = ctx.agent_scope(agent)
        current = ctx.agent_now(agent, "model")
        # Runs a workflow script started, or given a model when they
        # started, don't follow the agent file: the saving leaves them out.
        elsewhere = model_swap.set_elsewhere_sentence(
            *(
                _int(ctx.cell("model_swap", "model_swap_by_agent_type", agent, column))
                for column in ("workflow_runs", "spawn_model_runs")
            )
        )
        set_elsewhere = set_elsewhere or bool(elsewhere)
        changes.append(
            SettingChange(
                target="agent",
                key="model",
                agent=agent,
                value=_family_alias(alt),
                current=current if current not in (None, _UNKNOWN) else f"not set (used {now})",
                new_agent_file=not has_file,
                scope="managed" if rec.scope == "managed" else scope,
                # PROF-02: unlike a settings change (which `apply --launch`
                # can scope to one session via a --settings overlay), an
                # agent frontmatter edit has no session-only path -- Claude
                # Code's --agents flag would need the agent's full prompt
                # body inline, which this dashboard never reads or copies
                # (Assumption: nobody wants their agent prompts round-
                # tripped through a savings estimate). So it's written
                # once and then applies to every run of that agent from
                # then on: labelled as such, and given the plain saving
                # figure rather than the "At most" session ceiling used
                # for a change that might only be tried for a session.
                note="Persistent: applies to every later run of this agent that isn't given a model when it "
                "starts, not only one session." + elsewhere,
                saving=ctx.money(saving),
            )
        )
    total = sum(r[3] for r in rows)
    top = rows[0]
    return Recommendation(
        id="model-tier",
        severity="advice",
        category="settings",
        title="A cheaper model could do some of this work",
        why=(
            f"{_who(top[1]).capitalize()} ran on {_model_prose(top[4]) if top[4] else 'a larger model'}, "
            f"and the same work priced at {_family_alias(top[2]).capitalize()} would cost "
            f"{_percent(top[3], top[0])}."
            if len(rows) == 1
            else f"{len(rows)} of your agent types ran on a larger model than their work may need. "
            f"The biggest saving is {_who(top[1])}."
        )
        + (
            " Only runs started without a model follow the agent file. Runs a workflow script started, or given "
            "a model when they started, aren't counted."
            if set_elsewhere
            else ""
        )
        + _left_out_note(left_out),
        action=(
            "Try the cheaper model on a few tasks and compare the results before keeping it."
            if len(rows) == 1
            else "Move one agent type to the cheaper model first, compare its results for a few days, "
            "then decide about the rest."
        ),
        lever="model",
        scope=tier[0].scope,
        evidence=evidence,
        changes=changes,
        estimated_saving=ctx.money(total, prefix="At most "),
        saving_basis=ctx.basis(_TIER_BASIS),
        saving_usd=total,
    )


def _compaction_replayed(ctx: _Context) -> bool:
    """The compaction replay priced the main sessions as they ran, and
    so every candidate window beside them: it has a verdict on
    ``autoCompactWindow``, whether or not ``compaction-window`` fired."""
    observed = ctx.cell("compaction_sim", "compaction_sim_by_window", "none", "cost")
    return isinstance(observed, (int, float)) and observed > 0


def _consolidate_compaction(recs: list[Recommendation], ctx: _Context) -> list[Recommendation]:
    replayed = _compaction_replayed(ctx)
    window = next((r for r in recs if r.id == "compaction-window"), None)
    if window is not None:
        floor = window.title.rsplit(" ", 1)[-1].replace(",", "")
        current = ctx.window_now()
        if floor.isdigit() and isinstance(current, int) and current <= int(floor):
            # Already summarising at or below the modelled window.
            recs = [r for r in recs if r is not window]
            window = None
    if window is not None or replayed:
        # The replay weighed both sides, so its verdict (a window, or none
        # worth setting) stands over "summaries happen too often".
        recs = [r for r in recs if r.id != "compaction-churn"]
    churn = next((r for r in recs if r.id == "compaction-churn"), None)
    for rec in recs:
        if rec.id == "long-context-share" and (replayed or window is not None or churn is not None):
            # The setting belongs to the replay, else to churn: never
            # "raise it" on one card and "lower it" on another.
            rec.lever = None
            rec.category = "workflow"
    return recs


def _drop_applied(recs: list[Recommendation]) -> list[Recommendation]:
    """Leave out changes the current config already makes. The rules
    measure the whole period, so a change made part-way through it still
    shows its full saving; a card whose every change is already in effect
    is dropped."""
    out = []
    for rec in recs:
        if rec.changes:
            pending = [c for c in rec.changes if not already_set(c.key, c.value, c.current)]
            if not pending:
                continue
            rec.changes = pending
        out.append(rec)
    return out


# -- per-rule wording ------------------------------------------------------------

#: ``compaction_sim._scope_and_lever_note`` (compaction_sim.py is outside
#: this work package's file list -- P5 owns it) still returns ``"project"``
#: for a value set at either the project_shared or the project_local
#: layer, one token short of this module's/``fixes.py``'s ``"repo"``/
#: ``"project-local"`` split. Folded to ``"repo"`` here so every
#: ``SettingChange`` this module builds carries a scope ``fixes.py``'s
#: ``_SETTINGS_WHERE``/``command_for`` actually recognise -- COV-01's
#: project-local precision is still lost for ``compaction-window``
#: specifically (it can name the wrong of the two project-scoped files
#: when the value is actually project-local); every other rule's scope
#: (``recommend.py``'s own ``_lever_scope``, fixed for COV-01) already
#: distinguishes the two correctly.
_SCOPE_ALIASES = {"project": "repo"}


def _advice_scope(rec_scope: str) -> str:
    """COV-01: the scope a ``SettingChange`` this module builds should
    carry, translated from whichever vocabulary the rule that produced
    ``rec`` uses into ``fixes.py``'s own user/project-local/repo/managed
    four-way split. Every call site below used to hardcode
    ``"managed" if rec.scope == "managed" else "user"``, silently
    discarding a rule's own already-correct "a higher layer overrides
    this" finding (recommend.py's ``_lever_scope``) whenever it wasn't
    exactly "managed" -- the flattening finding D3/D5 describe."""
    return _SCOPE_ALIASES.get(rec_scope, rec_scope)


def _window_change(rec: Recommendation, ctx: _Context, *, value: int | None = None, suggested: str) -> SettingChange:
    """A change to the auto-compact window. While
    CLAUDE_CODE_AUTO_COMPACT_WINDOW is set it overrides autoCompactWindow,
    so the change goes to the variable, in a settings file's env block
    (whose value replaces the shell's)."""
    now = ctx.window_now()
    if not auto_compact_window_env_set(ctx.snapshot):
        return SettingChange(
            target="settings",
            key="autoCompactWindow",
            value=value,
            current=now,
            suggested=suggested,
            scope=_advice_scope(rec.scope),
        )
    from .recommend import _env_lever_scope

    # Env block values are strings.
    return SettingChange(
        target="settings",
        key=f"env.{AUTO_COMPACT_WINDOW_ENV}",
        value=str(value) if value is not None else None,
        current=str(now) if isinstance(now, int) else now,
        suggested=suggested,
        scope=_env_lever_scope(AUTO_COMPACT_WINDOW_ENV, ctx.snapshot),
    )


def _explain_compaction_window(rec: Recommendation, ctx: _Context) -> None:
    label = rec.title.rsplit(" ", 1)[-1]
    value = int(label.replace(",", "")) if label.replace(",", "").isdigit() else None
    saving = _evidence_value(rec, "after rediscovery correction")
    rec.title = f"Summarising the main session at {label} tokens would cost less"
    rec.why = (
        "Every reply re-reads the whole conversation, so a long main session gets more expensive with each "
        f"reply. A summary at {label} tokens resets that."
    )
    rec.action = (
        f"Set your auto-compact window to {label} tokens. Claude Code then summarises the main session a "
        "little before its context reaches that size."
    )
    rec.changes = [_window_change(rec, ctx, value=value, suggested=f"{label} tokens")]
    rec.estimated_saving = ctx.money(saving, prefix="About ")
    rec.saving_usd = saving if isinstance(saving, (int, float)) else None
    rec.saving_basis = ctx.basis(
        "Modelled by replaying your main sessions with summaries at this size, shaped like your past ones. "
        "It counts the summary itself, the cache rebuild on the reply after it, and an allowance for "
        "re-reading files. Not measured."
    )


def _explain_compaction_churn(rec: Recommendation, ctx: _Context) -> None:
    mean = _evidence_value(rec, "Compactions per session")
    rec.title = "Your main sessions are summarised often"
    rec.why = (
        f"Main sessions were summarised {mean:.1f} times each on average. " if isinstance(mean, (int, float)) else ""
    ) + "Each summary rewrites the cache and can drop detail Claude then has to find again."
    rec.action = (
        "Raise your auto-compact window so summaries happen less often, or start a fresh session between tasks."
    )
    rec.changes = [_window_change(rec, ctx, suggested="a larger window than now, so summaries happen less often")]


def _explain_long_context_share(rec: Recommendation, ctx: _Context) -> None:
    share = _evidence_value(rec, "huge-context")
    # recommend._rule_long_context_share labels this evidence row "Context
    # size 9 in 10 main-session replies stay under" -- "p90" alone isn't a
    # substring of it, so the old lookup here never found it and the p90
    # clause silently dropped out of rec.why.
    p90 = _evidence_value(rec, "9 in 10 main-session replies stay under")
    has_p90 = isinstance(p90, (int, float))
    # The p90 clause is about the main session specifically; the share
    # clause counts every reply, subagents included, so only claim "main
    # session" in the title when p90 is the reason this fired.
    rec.title = "Your main session's context is running large" if has_p90 else "Context is running large across replies"
    parts = []
    if isinstance(share, (int, float)):
        parts.append(f"{share:.0f}% of what replies read back from the cache came from very large contexts.")
    if has_p90:
        parts.append(f"One main-session reply in ten read more than {p90:,.0f} tokens of context.")
    parts.append("Every reply re-reads all of it.")
    rec.why = " ".join(parts)
    # known_savers.active_in_report: tokensave's hook, in a project it has
    # indexed, blocks an Explore-agent spawn and a symbol-shaped search --
    # this card's usual "send searches to a subagent" advice would just be
    # redirected there, so point at tokensave's own tools instead.
    tokensave = known_savers.active_in_report(ctx.report, min_share=known_savers.ADVICE_MIN_SHARE)
    if tokensave:
        rec.variant = "tokensave"
    if rec.lever is None:
        rec.action = (
            (
                "Find code with tokensave's tools, read only the part of a file you need, and start a fresh "
                "session when you switch tasks."
            )
            if tokensave
            else (
                "Send searches and exploration to a subagent, whose context is thrown away when it finishes, "
                "and start a fresh session when you switch tasks."
            )
        )
        rec.changes = []
        return
    rec.action = (
        (
            "Summarise the main session sooner (a smaller auto-compact window), and find code with "
            "tokensave's tools instead of reading whole files."
        )
        if tokensave
        else "Summarise the main session sooner (a smaller auto-compact window), and send exploration to subagents."
    )
    rec.changes = [
        _window_change(rec, ctx, suggested="a smaller window than now, so the main session is summarised sooner")
    ]


def _explain_ttl_switch(rec: Recommendation, ctx: _Context) -> None:
    agent = rec.agent_type or "top-level"
    verdict = _evidence_value(rec, "TTL recommendation")
    target = "1h" if isinstance(verdict, str) and verdict.endswith("1h") else "5m"
    saving = ctx.cell("ttl", "ttl_by_agent_type", agent, "saving_usd")
    who = {"top-level": "your main session", "unknown": "subagents with no recorded type"}.get(agent, agent)
    lifetime = "1 hour" if target == "1h" else "5 minutes"
    rec.title = f"A {'1-hour' if target == '1h' else '5-minute'} cache lifetime would suit {who} better"
    rec.why = (
        f"At the actual pauses between replies from {who}, a {lifetime} cache would have cost less than "
        "the one used."
    )
    rec.action = f"Set {who}'s cache lifetime to {lifetime} ({target})."
    if agent == "top-level":
        change = SettingChange(target="settings", key="promptCacheTtl", value=target, current=ctx.setting_now("promptCacheTtl"))
    elif rec.lever == "subagentPromptCacheTtl":
        # recommend._ttl_row_lever: no agent file to edit ("unknown"),
        # or the setting is already set and outranks every agent file.
        current = ctx.setting_now("subagentPromptCacheTtl")
        change = SettingChange(
            target="settings",
            key="subagentPromptCacheTtl",
            value=target,
            current=current,
            note=(
                "This setting is already set, and Claude Code uses it before any agent file's cacheTtl, so "
                "changing it changes every subagent's cache lifetime, not just this one's."
                if current not in (None, _UNKNOWN)
                else ""
            ),
        )
        rec.action = f"Set every subagent's cache lifetime to {lifetime} ({target})."
    else:
        scope, has_file = ctx.agent_scope(agent)
        change = SettingChange(
            target="agent",
            key="experimental.cacheTtl",
            agent=agent,
            value=target,
            current=ctx.agent_now(agent, "experimental.cacheTtl"),
            new_agent_file=not has_file,
            scope=scope,
        )
    if rec.scope == "managed":
        change.scope = "managed"
    rec.changes = [change]
    rec.estimated_saving = ctx.money(saving, prefix="About ")
    rec.saving_usd = saving if isinstance(saving, (int, float)) else None
    rec.saving_basis = ctx.basis(
        "Replays every reply's cache writes and reads under the other lifetime at list price."
    )


def _explain_effort_mismatch(rec: Recommendation, ctx: _Context) -> None:
    if any(source == "habits.habits_effort_fit" for _label, _value, source, _row in rec.evidence):
        _explain_effort_mismatch_reported(rec, ctx)
        return
    share = _evidence_value(rec, "thinking share")
    rec.title = "High effort is being spent on light work"
    rec.why = (
        (f"At high effort, {share:.0f}% of Claude's output was thinking, " if isinstance(share, (int, float)) else "")
        + "yet many of your sessions are docs or light edits that rarely need it."
    )
    rec.why = rec.why[:1].upper() + rec.why[1:]
    rec.action = "Make medium your default effort, and raise it with /effort for the tasks that need it."
    rec.changes = [
        SettingChange(
            target="settings",
            key="effortLevel",
            value="medium",
            current=ctx.setting_now("effortLevel"),
            note="The high-effort thinking share is measured across all sessions, not only the light ones.",
            scope=_advice_scope(rec.scope),
        )
    ]


def _explain_effort_mismatch_reported(rec: Recommendation, ctx: _Context) -> None:
    """The direct path: messages Claude reported as easy that ran at high
    effort or above (``recommend._effort_mismatch_reported``)."""
    easy = sum(v for label, v, _s, _r in rec.evidence if label.startswith("Easy messages") and isinstance(v, int))
    shares = [v for label, v, _s, _r in rec.evidence if "thinking share" in label and isinstance(v, (int, float))]
    rec.title = "High effort is being spent on easy work"
    rec.why = (
        f"Claude reported {easy} of your messages as easy work, yet they ran at high effort or above"
        + (f", and up to {max(shares):.0f}% of their output was thinking." if shares else ".")
    )
    rec.action = "Make medium your default effort, and raise it with /effort for the tasks that need it."
    rec.changes = [
        SettingChange(
            target="settings",
            key="effortLevel",
            value="medium",
            current=ctx.setting_now("effortLevel"),
            note="Measured on the messages Claude reported as easy (metrics capture).",
            scope=_advice_scope(rec.scope),
        )
    ]
    rec.estimated_saving = ctx.money(rec.saving_usd, prefix="About ")
    rec.saving_basis = ctx.basis("Half the thinking on those messages, at list price. Not measured.")


def _explain_plan_handoff(rec: Recommendation, ctx: _Context) -> None:
    rec.estimated_saving = ctx.money(rec.saving_usd, prefix="At most ")
    rec.saving_basis = ctx.basis(
        "The replies after each big approved plan, priced without the planning context, less one cache write "
        "of the plan and an allowance for re-reading files. At list price."
    )


def _explain_run_split(rec: Recommendation, ctx: _Context) -> None:
    rec.estimated_saving = ctx.money(rec.saving_usd, prefix="At most ")
    rec.saving_basis = ctx.basis(
        "This agent's long runs repriced as fresh runs at the interval that saves most, less each split's note, "
        "cache write and an allowance for re-reading files. Only runs your current auto-compact window allows are "
        "counted. At list price."
    )


def _explain_hook_block_resent(rec: Recommendation, ctx: _Context) -> None:
    rec.estimated_saving = ctx.money(rec.saving_usd, prefix="About ")
    rec.saving_basis = ctx.basis(
        "The replies that read these blocks, each block taking its share of the reply after it. At list price."
    )


def _explain_hook_context_carry(rec: Recommendation, ctx: _Context) -> None:
    rec.estimated_saving = ctx.money(rec.saving_usd, prefix="At most ")
    rec.saving_basis = ctx.basis(
        "What keeping this hook's context cost across the replies after it, at list price. A shorter message "
        "saves part of that."
    )


def _explain_baseline_bloat(rec: Recommendation, ctx: _Context) -> None:
    table = next((t for s in ctx.report.sections if s.key == "agents" for t in s.tables
                  if t.name == "topology_session_baseline"), None)
    row_key = table.rows[0][0] if table is not None and table.rows else None
    baseline = ctx.cell("agents", "topology_session_baseline", row_key, "mean_baseline")
    changeable = _evidence_value(rec, "What you can change at the start of a session (est)")
    rec.title = "Every session starts with a large context"
    if isinstance(changeable, (int, float)):
        rec.why = (
            f"About {_tokens(changeable)} tokens of every session's first call come from settings you control: "
            "the skills list, your memory files, and the MCP servers and plugins you load everywhere."
        )
    elif isinstance(baseline, (int, float)):
        rec.why = (
            f"Each main session's first call reads about {_tokens(baseline)} tokens before your first message, "
            "and MCP servers and plugins you load everywhere add to it."
        )
    else:
        rec.why = "MCP servers and plugins you load everywhere add to every session's startup context."
    rec.action = "Turn off MCP servers and plugins in the projects that don't use them."
    # Your own agents' descriptions are in every session's start. Shortening
    # them is worth saying only when they are a real share of it.
    agents = _evidence_value(rec, "custom agents")
    if isinstance(agents, (int, float)) and agents >= CUSTOM_AGENTS_TRIM_TOKENS:
        rec.action += (
            f" Your own agents' descriptions add about {_tokens(agents)} tokens to every session. Shortening "
            "the description line in their agent files trims that."
        )


def _explain_mcp_unused_server(rec: Recommendation, ctx: _Context) -> None:
    rec.estimated_saving = ctx.money(rec.saving_usd, prefix="At least ")
    rec.saving_basis = ctx.basis(
        "What these servers' tool names, instructions and tools sent in full cost in this window, at each "
        "reply's cache read rate; a reply that rebuilt the cache paid more. It starts with your next new "
        "session, not the ones already running."
    )


def _explain_spawn_cost(rec: Recommendation, ctx: _Context) -> None:
    first_call = _evidence_value(rec, "Mean first call")
    unused = _evidence_value(rec, "Tool definitions it rarely or never uses")
    agent = rec.agent_type or "this agent"
    rec.title = f"Starting {agent} is expensive before it does any work"
    if rec.lever is None:
        # A built-in agent type has no file of its own: most of its start is
        # Claude Code's system prompt and tool definitions, so a shorter task
        # prompt does not change it. A same-named agent file with a tools list
        # does. (The rule gives no card for Explore, Plan or claude-code-guide,
        # which the tools list card leaves alone, so every one that gets here
        # can be promised that list.)
        rec.action = (
            f"Most of what {agent} reads at startup is Claude Code's own system prompt and tool definitions, "
            "which a shorter task prompt does not change. A same-named agent file with a tools list leaves out "
            "the tools it never calls; the tools list card gives that list once enough of its spawns show it."
        )
    else:
        rec.action = (
            f"Check what {agent} is given when it starts: its agent file and tools list, and the CLAUDE.md "
            "files it receives. Trim what it doesn't need."
        )
    if isinstance(first_call, (int, float)) and isinstance(unused, (int, float)):
        rec.why = (
            f"Each {agent} spawn reads about {_tokens(first_call)} tokens on its first reply, and about "
            f"{_tokens(unused)} of those are tool definitions it rarely or never calls."
        )
    elif isinstance(first_call, (int, float)):
        rec.why = f"Each {agent} spawn reads about {_tokens(first_call)} tokens on its first reply."
    else:
        rec.why = f"Each {agent} spawn reads a lot on its first reply."


def _explain_agent_report_size(rec: Recommendation, ctx: _Context) -> None:
    size = _evidence_value(rec, "report proxy")
    agent = rec.agent_type or "this agent"
    rec.title = f"{agent} sends back long reports"
    rec.why = (
        f"{agent}'s final reply averages {_tokens(size)} tokens, and it stays in the main session's context "
        "for the rest of the session."
        if isinstance(size, (int, float))
        else f"{agent}'s final reply stays in the main session's context for the rest of the session."
    )
    rec.action = f"Ask {agent} for a short report: the findings and file paths, not the working."


def _explain_agent_batch_probes(rec: Recommendation, ctx: _Context) -> None:
    calls = _evidence_value(rec, "Replies it made")
    probes = _evidence_value(rec, "Single read-only calls")
    shell = _evidence_value(rec, "by shell command")
    agent = rec.agent_type or "this agent"
    workflow = agent == "workflow-subagent"
    rec.title = f"{agent} looks things up one call at a time"
    if isinstance(calls, (int, float)) and isinstance(probes, (int, float)):
        rec.why = (
            f"{agent} made one read-only call and nothing else in {_tokens(probes)} of its {_tokens(calls)} replies. "
            "Each of those replies read its whole context again."
        )
        if isinstance(shell, (int, float)) and shell >= 1:
            rec.why += f" {_tokens(shell)} of the calls were shell commands such as cat or grep."
    else:
        rec.why = f"{agent} often makes one read-only call per reply, and each reply reads its whole context again."
    where = "the prompt in each workflow script that starts it" if workflow else "its agent definition or the prompt that starts it"
    rec.action = f"Add \"{BATCH_PROBES_LINE}\" to {where}. Lookups that don't depend on each other then share one reply."
    rec.estimated_saving = ctx.money(rec.saving_usd, prefix="At most ")
    rec.saving_basis = ctx.basis(
        "The replies after the first of each run of single lookups, at list price, halved because some lookups "
        "need the answer to the one before."
    )


def _explain_plan_rounds(rec: Recommendation, ctx: _Context) -> None:
    plans = _evidence_value(rec, "Plans you approved")
    rounds = _evidence_value(rec, "Times plans were sent back")
    asked = _evidence_value(rec, "with a question or critique")
    rec.title = "Plans keep being sent back"
    if all(isinstance(v, (int, float)) for v in (plans, rounds, asked)):
        rec.why = (
            f"You sent plans back {_tokens(rounds)} times before approving {_tokens(plans)}. "
            f"{_tokens(asked)} of those rounds were a question, a critique or a doubt. "
            "Each round reads the whole planning conversation again."
        )
    else:
        rec.why = "You often send a plan back before you approve it, and each round reads the whole planning conversation again."
    rec.action = (
        "Put one standing request in your first planning message, in CLAUDE.md or in a plan skill. "
        f"It reads \"{CRITIQUE_PLAN_LINE}.\" Claude then raises those points itself, and you answer them once."
    )
    rec.estimated_saving = ctx.money(rec.saving_usd, prefix="At most ")
    rec.saving_basis = ctx.basis(
        "The replies between the first plan and the approval, at list price. Only rounds that were a question, "
        "a critique or a doubt count, at a quarter of their cost. A critique won't spare every round."
    )


#: The limit-pressure card's evidence label for each ``limits.SPEND_SOURCES``
#: entry's share, the name the card gives it, and the change it points to
#: when that source spent the most before your stops.
_LIMIT_CENTRES: dict[str, tuple[str, str, str]] = {
    "main": (
        "Main session share",
        "main session",
        "Plan the work before you start, and run /clear when the task changes, so each reply carries less.",
    ),
    "direct": ("Direct agents share", "direct agents", "Run fewer agents at once when a limit is close."),
    "workflow": (
        "Workflow agents share",
        "workflow agents",
        "Lower the concurrency in the workflow script, so fewer agents run at once when a limit is close.",
    ),
}
_FEWER_AGENTS = "Run fewer agents at once when a limit is close."


def _limit_centre(rec: Recommendation) -> tuple[str, float] | None:
    """The ``limits.SPEND_SOURCES`` entry with the biggest share of the
    spend before your stops (the earlier one on a tie), and that share;
    ``None`` when the roll-up gave no shares."""
    shares = {
        source: value
        for source in SPEND_SOURCES
        for value in [_evidence_value(rec, _LIMIT_CENTRES[source][0])]
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }
    if not shares:
        return None
    best = max(SPEND_SOURCES, key=lambda source: shares.get(source, -1.0))
    return best, shares[best]


def _explain_limit_pressure(rec: Recommendation, ctx: _Context) -> None:
    five_hour = _evidence_value(rec, "5-hour limit stops")
    weekly = _evidence_value(rec, "Weekly stops that stopped work")
    days = _evidence_value(rec, "Days covered")
    cut_off = _evidence_value(rec, "cut off")
    counted = _evidence_value(rec, "Stops counted")
    burst = _evidence_value(rec, "in these stops")
    overall = _evidence_value(rec, "across all your work")
    rec.title = "You keep hitting your usage limit"

    def times(count) -> str:
        return "1 time" if count == 1 else f"{count:,.0f} times"

    span = f" in {days:,.0f} days" if isinstance(days, (int, float)) and days > 1 else ""
    stops = []
    if isinstance(five_hour, (int, float)) and five_hour:
        stops.append(("Your 5-hour limit stopped you ", times(five_hour)))
    if isinstance(weekly, (int, float)) and weekly:
        stops.append(("your weekly limit ", times(weekly)) if stops else ("Your weekly limit stopped you ", times(weekly)))
    sentences = []
    if stops:
        sentences.append(" and ".join(lead + count for lead, count in stops) + span + ".")
    if isinstance(cut_off, (int, float)) and cut_off:
        sentences.append(f"{cut_off:,.0f} {'subagent was' if cut_off == 1 else 'subagents were'} cut off by a usage limit.")
    centre = _limit_centre(rec)
    if centre is not None:
        source, share = centre
        sentences.append(
            f"Before your stops, your {_LIMIT_CENTRES[source][1]} spent the most: {share:.0f}% of list-price spend. "
            "The limit may weigh models differently."
        )
    if (
        isinstance(counted, (int, float))
        and isinstance(burst, (int, float))
        and isinstance(overall, (int, float))
        and burst_stands_out(burst, overall)
    ):
        sentences.append(
            ("In 1 recent stop, " if counted == 1 else f"In {counted:,.0f} recent stops, ")
            + f"{burst:.0f}% of the spend ran while {BURST_AGENTS} or more agents "
            f"worked at once ({overall:.0f}% across all your work)."
        )
    rec.why = " ".join(sentences) if sentences else "Your usage limit keeps stopping your work."
    action = _FEWER_AGENTS if centre is None else _LIMIT_CENTRES[centre[0]][2]
    if centre is not None and centre[0] == "main" and isinstance(cut_off, (int, float)) and cut_off:
        action += " " + _FEWER_AGENTS
    rec.action = action + " {{page:cache/rebuilds}} shows each stop, its reset time and what it cost."


def _explain_cache_read_dominance(rec: Recommendation, ctx: _Context) -> None:
    share = _evidence_value(rec, "Cache-read share")
    rec.title = "Most of your cost is re-reading the conversation"
    rec.why = (
        (f"Cache reads are {share:.0f}% of your cost. " if isinstance(share, (int, float)) else "")
        + "Each reply reads the whole conversation back from the cache. That is already the cheapest way to "
        "send it, so the saving comes from sending less."
    )
    # See _explain_long_context_share: tokensave's hook redirects the
    # searches/Explore-agent advice this card would otherwise give.
    rec.action = (
        "Keep contexts small: start fresh sessions between tasks, and look up code with tokensave's tools "
        "instead of reading whole files."
        if known_savers.active_in_report(ctx.report, min_share=known_savers.ADVICE_MIN_SHARE)
        else "Keep contexts small: start fresh sessions between tasks, and send exploration to subagents."
    )


def _explain_data_quality(rec: Recommendation, ctx: _Context) -> None:
    rec.title = "A few numbers may be slightly off"
    rec.why = rec.action.split(":", 1)[-1].strip() if ":" in rec.action else rec.action
    rec.action = "See {{page:data}} for what could not be read."


def _explain_pricing_coverage(rec: Recommendation, ctx: _Context) -> None:
    # Fix 2: coverage_pct alone can't tell "no price at all" (cost is
    # left out entirely, totals read too low) from "priced by closest
    # match" (cost is counted, but only an estimate) apart -- read the
    # two usage tables the rule cites back off the report itself, the
    # same way _explain_baseline_bloat above reads a table it needs
    # rows from rather than a single cell.
    unknown_table = next(
        (t for s in ctx.report.sections if s.key == "usage" for t in s.tables if t.name == "pricing_unknown_models"),
        None,
    )
    closest_table = next(
        (t for s in ctx.report.sections if s.key == "usage" for t in s.tables if t.name == "pricing_closest_match"),
        None,
    )
    has_unknown = bool(unknown_table is not None and unknown_table.rows)
    has_closest_match = bool(closest_table is not None and closest_table.rows)
    # A closest match that is only a newer release the rate card doesn't
    # know yet (claude-x-5-5 priced as claude-x-5) isn't a mismatched
    # model; say so when that's all there is.
    keys = [c.key for c in closest_table.columns] if has_closest_match else []
    newer_rows = [
        row
        for row in (closest_table.rows if "priced_as" in keys else [])
        if row and newer_version_of(str(row[0]), str(row[keys.index("priced_as")]))
    ]
    all_newer = has_closest_match and len(newer_rows) == len(closest_table.rows)

    if has_unknown and has_closest_match:
        rec.title = "Some usage has no price, some is only an estimate"
        rec.why = (
            "Replies from models missing from pricing.toml are left out of every cost, so "
            "totals are too low. Others were priced at a different model's rate, so their "
            "cost may be off."
        )
    elif all_newer and len(newer_rows) == 1:
        rec.title = "A newer model is priced at an older model's rate"
        rec.why = (
            "pricing.toml has no row for this newer model yet, so its cost is estimated from the older "
            "model's rate and may be off."
        )
    elif all_newer:
        rec.title = "Newer models are priced at older models' rates"
        rec.why = (
            "pricing.toml has no rows for these newer models yet, so their cost is estimated from the older "
            "models' rates and may be off."
        )
    elif has_closest_match:
        rec.title = "Some usage is priced by closest match, not its own rate"
        rec.why = (
            "These replies' model has no row of its own in pricing.toml. Their cost is "
            "estimated from the closest registered model's rate instead, so it may be off."
        )
    else:
        rec.title = "Some usage has no price"
        rec.why = "Replies from models missing from pricing.toml are left out of every cost, so totals are too low."


def _explain_discovery_share(rec: Recommendation, ctx: _Context) -> None:
    share = _evidence_value(rec, "Share of cost")
    rec.title = "Much of the work is finding your way around"
    rec.why = (
        f"Searching and reading the code took {share:.0f}% of the cost." if isinstance(share, (int, float)) else ""
    )
    rec.action = (
        "Write down what gets rediscovered each time, such as where things live and how to run things. Put it "
        "in CLAUDE.md or a short reference file, so sessions start from it."
    )


def _explain_subagent_volume(rec: Recommendation, ctx: _Context) -> None:
    agent = rec.agent_type or "One agent type"
    share = re.search(r"([\d.]+)%", rec.action)
    rec.title = f"{agent} is most of your subagent cost"
    rec.why = (
        f"{agent} is {share.group(1)}% of what your subagents cost, so it is the best place to look first."
        if share
        else "One agent type drives most subagent spend, so it is the best place to look first."
    )
    rec.action = (
        f"Check whether every {agent} run is needed, whether a cheaper model fits, and how long its task "
        "prompts are."
    )
    # Phase 8a: say where the most spend sits overall, from the cost-centre table.
    centre = cost_centres.largest_centre(ctx.report)
    if centre is not None:
        key, cell, _total, share = centre
        rec.why += (
            f" Across all spend, the largest cost centre is {cost_centres.CENTRE_LABELS.get(key, key).lower()}"
            f" ({share:.0f}%), mostly {cost_centres.CELL_LABELS.get(cell, cell).lower()}."
        )


# -- agent models: code written on a larger model than it needed -----------------


#: What a code-writing agent is called, by the role word ``agent_models``
#: counted it under. ``other`` is a run with no role word.
_WRITER_NOUNS = {
    "implement": ("implementer", "implementers"),
    "fix": ("fixer", "fixers"),
    "apply": ("applier", "appliers"),
    "test": ("test writer", "test writers"),
    "build": ("builder", "builders"),
    "write": ("writer", "writers"),
    "migrate": ("migrator", "migrators"),
    "refactor": ("refactorer", "refactorers"),
    "other": ("other agent that edited code", "other agents that edited code"),
}

#: A deciding role word as the verb "started to ..." takes. A word left out
#: reads as its own verb ("review", "audit").
_DECIDER_VERBS = {"completeness": "check", "adversarial": "challenge", "baseline": "measure"}

#: Most roles named in one sentence; the rest are counted as "N more", so
#: the sentence stays short however many roles a card covers.
_ROLES_NAMED = 3

#: Most verbs the decide-and-apply card lists.
_VERBS_NAMED = 3

_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)

_AGENT_MODEL_BASIS = (
    "Worked out by pricing the same tokens at Sonnet's list price. Sonnet may need more replies, so this is a "
    "ceiling, not a forecast."
)


@dataclass(slots=True)
class _AgentModelFacts:
    """What one ``agent_models`` rule cited, read back from its evidence
    (never from a table cell: a row key repeats across verdicts)."""

    agents: int
    roles: list[tuple[str, int]]
    model: str
    saving: float | None
    edit_turns: int
    workflow_runs: int
    last_seen: str
    later: int
    env_var_set: bool


def _agent_model_facts(rec: Recommendation) -> _AgentModelFacts | None:
    agents = _evidence_value(rec, "Agents")
    if not isinstance(agents, int) or isinstance(agents, bool):
        return None
    roles = _evidence_value(rec, "Roles")
    saving = _evidence_value(rec, "Ceiling saving (USD)")
    return _AgentModelFacts(
        agents=agents,
        roles=[(word, int(count)) for word, count in re.findall(r"([a-z]+) (\d+)", roles)]
        if isinstance(roles, str)
        else [],
        model=str(_evidence_value(rec, "Model") or ""),
        saving=saving if isinstance(saving, (int, float)) else rec.saving_usd,
        edit_turns=_int(_evidence_value(rec, "Edit turns")),
        workflow_runs=_int(_evidence_value(rec, "Workflow runs")),
        last_seen=str(_evidence_value(rec, "Last seen") or ""),
        later=_int(_evidence_value(rec, "Later compliant writers")),
        env_var_set=_evidence_value(rec, "CLAUDE_CODE_SUBAGENT_MODEL set") == "yes",
    )


def _started_by_workflow(facts: _AgentModelFacts) -> bool:
    """Whether a card's agents were started by a workflow script. The table
    counts them as ``Workflow runs`` from the transcript's kind
    (``TranscriptMeta.kind == "workflow-agent"``): a workflow agent's agent
    type is any name its script gave it, so the type never says."""
    return facts.workflow_runs > 0


def _agent_noun(agent_type: str | None, count: int, workflow: bool = False) -> str:
    """``workflow agents`` for the workflow card, else ``<type> agents``."""
    kind = "workflow" if workflow else (agent_type or "")
    return " ".join(part for part in (kind, "agent" if count == 1 else "agents") if part)


def _listed(parts: list[str], joiner: str = "and") -> str:
    """``a``, ``a and b`` or ``a, b and c``."""
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + f" {joiner} " + parts[-1]


def _role_noun(word: str, count: int) -> str:
    one, many = _WRITER_NOUNS.get(word, (f"{word} agent", f"{word} agents"))
    return f"{count} {one if count == 1 else many}"


def _roles_phrase(facts: _AgentModelFacts, noun: str) -> str:
    """``4 implementers and 1 fixer``; the agent count when the card cites
    no roles."""
    roles = facts.roles
    if not roles:
        return f"{facts.agents} {noun}"
    if len(roles) > _ROLES_NAMED:
        rest = sum(count for _word, count in roles[_ROLES_NAMED - 1 :])
        parts = [_role_noun(word, count) for word, count in roles[: _ROLES_NAMED - 1]] + [f"{rest} more"]
    else:
        parts = [_role_noun(word, count) for word, count in roles]
    return _listed(parts)


def _most_recently(day: str, report: ReportModel) -> str:
    """``, most recently on 1 October`` for an ISO date; nothing when it
    isn't one. The year is added only when it isn't the report's own."""
    try:
        seen = date.fromisoformat(day[:10])
    except ValueError:
        return ""
    stamp = report.meta.generated_at[:4]
    year = int(stamp) if stamp.isdigit() else date.today().year
    text = f"{seen.day} {_MONTHS[seen.month - 1]}" + ("" if seen.year == year else f" {seen.year}")
    return f", most recently on {text}"


def _family_name(model: str) -> str:
    """``Opus`` for ``claude-opus-5-5``. The flagged agents ran above
    Sonnet, so a model with no family in its id is called Opus."""
    family = _family_alias(model)
    return family.capitalize() if family in ("haiku", "sonnet", "opus", "fable") else "Opus"


def _agent_model_saving(rec: Recommendation, facts: _AgentModelFacts, ctx: _Context) -> None:
    rec.estimated_saving = ctx.money(facts.saving, prefix="At most ")
    rec.saving_basis = ctx.basis(_AGENT_MODEL_BASIS)


def _explain_agent_model_inherited(rec: Recommendation, ctx: _Context) -> None:
    facts = _agent_model_facts(rec)
    if facts is None:
        return
    count = facts.agents
    workflow = _started_by_workflow(facts)
    noun = _agent_noun(rec.agent_type, count, workflow)
    model = _model_prose(facts.model) if facts.model else "a larger model"
    when = _most_recently(facts.last_seen, ctx.report)
    if workflow:
        runs = max(facts.workflow_runs, 1)
        started = (
            f"{_roles_phrase(facts, noun)} in {runs} workflow run{'s' if runs != 1 else ''} started with no "
            f"model{when}."
        )
    else:
        started = f"{count} {noun} started with no model and wrote code{when}."
    source = "the model CLAUDE_CODE_SUBAGENT_MODEL names" if facts.env_var_set else "your main session's model"
    many = count != 1
    why = [
        started,
        f"So {'each' if many else 'it'} ran on {source}, {model}.",
        f"{'They' if many else 'It'} wrote code to a settled spec, which Sonnet does well.",
        "Agents that decide, such as integrate and review, aren't counted: Opus suits them.",
    ]
    if rec.variant == "fixed":
        why.append(
            f"Since then, {facts.later} agent{'s' if facts.later != 1 else ''} that wrote code ran on Sonnet or "
            "a smaller model, so this looks fixed."
        )
    if not workflow and rec.agent_type is not None and model_gate.build(whatif._Tables(ctx.report)).vetoed(
        rec.agent_type, "sonnet"
    ):
        rec.severity = "info"
        why.append(
            f"Your quality data says {rec.agent_type} may not be enough on Sonnet, so try it on a few tasks first."
        )
    rec.title = f"{count} {noun} wrote code on {model} with no model set"
    rec.why = " ".join(why)
    rec.action = (
        "If this rule lives in one project's notes, copy it to ~/.claude/CLAUDE.md so every project follows it."
        if rec.variant == "fixed"
        else "Paste the prompt below so Claude sets the model on every agent it starts."
    )
    _agent_model_saving(rec, facts, ctx)


def _explain_agent_model_asked(rec: Recommendation, ctx: _Context) -> None:
    facts = _agent_model_facts(rec)
    if facts is None:
        return
    count = facts.agents
    noun = _agent_noun(rec.agent_type, count, _started_by_workflow(facts))
    model = _model_prose(facts.model) if facts.model else "a larger model"
    family = _family_name(facts.model)
    many = count != 1
    rec.title = f"{count} {noun} that wrote code {'were' if many else 'was'} started on {model}"
    rec.why = " ".join(
        [
            f"{_roles_phrase(facts, noun)} {'were' if many else 'was'} started with {model} named in the call"
            f"{_most_recently(facts.last_seen, ctx.report)}.",
            f"{'They' if many else 'It'} wrote code to a settled spec, which Sonnet does well.",
            f"If whatever starts {'them' if many else 'it'} asks for {family} out of habit, Sonnet would cost less.",
        ]
    )
    rec.action = (
        f"Check the prompt, skill or script that starts {'these agents' if many else 'this agent'}. "
        f"Ask for Sonnet where nothing needs {family}."
    )
    _agent_model_saving(rec, facts, ctx)


def _explain_agent_decide_apply(rec: Recommendation, ctx: _Context) -> None:
    facts = _agent_model_facts(rec)
    if facts is None:
        return
    count = facts.agents
    noun = _agent_noun(rec.agent_type, count, _started_by_workflow(facts))
    model = _model_prose(facts.model) if facts.model else "a larger model"
    family = _family_name(facts.model)
    verbs = []
    for word, _count in facts.roles:
        verb = _DECIDER_VERBS.get(word, word)
        if verb not in verbs:
            verbs.append(verb)
    edits = facts.edit_turns
    rec.title = f"{count} {noun} decided and changed code on {model}"
    rec.why = " ".join(
        [
            f"{count} {noun} started to {_listed(verbs[:_VERBS_NAMED] or ['decide'], 'or')} and then edited code "
            f"in {edits} repl{'ies' if edits != 1 else 'y'}{_most_recently(facts.last_seen, ctx.report)}.",
            f"Deciding and applying in one {family} agent spends {family} rates on the edits too.",
            "Splitting it lets Opus decide and Sonnet apply what was decided.",
        ]
    )
    rec.action = (
        "Split this work: an Opus agent that decides and writes the exact changes, then a Sonnet agent that "
        "applies them."
    )
    rec.estimated_saving = ""
    rec.saving_usd = None


def _why_only(text: str, title: str = "") -> Callable[[Recommendation, _Context], None]:
    def explain(rec: Recommendation, ctx: _Context) -> None:
        if title:
            rec.title = title
        if not rec.why:
            rec.why = text

    return explain


_EXPLAIN: dict[str, Callable[[Recommendation, _Context], None]] = {
    "compaction-window": _explain_compaction_window,
    "compaction-churn": _explain_compaction_churn,
    "long-context-share": _explain_long_context_share,
    "ttl-switch": _explain_ttl_switch,
    "effort-mismatch": _explain_effort_mismatch,
    "plan-handoff": _explain_plan_handoff,
    "run-split": _explain_run_split,
    "hook-block-resent": _explain_hook_block_resent,
    "hook-context-carry": _explain_hook_context_carry,
    "baseline-bloat": _explain_baseline_bloat,
    "mcp-unused-server": _explain_mcp_unused_server,
    "spawn-cost": _explain_spawn_cost,
    "agent-report-size": _explain_agent_report_size,
    "agent-batch-probes": _explain_agent_batch_probes,
    "plan-rounds": _explain_plan_rounds,
    "limit-pressure": _explain_limit_pressure,
    "cache-read-dominance": _explain_cache_read_dominance,
    "data-quality": _explain_data_quality,
    "pricing-coverage": _explain_pricing_coverage,
    "discovery-share": _explain_discovery_share,
    "subagent-volume": _explain_subagent_volume,
    "agent-model-inherited": _explain_agent_model_inherited,
    "agent-model-asked": _explain_agent_model_asked,
    "agent-decide-apply": _explain_agent_decide_apply,
    "long-tool-waits": _why_only(
        "When a command runs for more than 5 minutes, the cache expires and the next reply pays to rebuild it.",
        "Long waits for commands let the cache expire",
    ),
    "notification-invalidation": _why_only(
        "A notice that lands mid-conversation changes what the cache holds, so the next reply rebuilds it.",
        "Subagent notices keep breaking the cache",
    ),
    "batch-instructions": _why_only(
        "Each message you queue while Claude works is written to the cache on its own.",
        "Messages sent one at a time rebuild the cache",
    ),
    "tool-output-carry": _why_only(
        "A tool's output stays in the conversation, so every later reply pays to read it again."
    ),
    "wasted-turns": _why_only("These replies cost money but produced nothing you kept."),
}

_SEVERITY_ORDER = {"action": 0, "advice": 1, "info": 2}

#: Sorted after every other card of the same severity.
_LAST_IN_GROUP = frozenset({"model-tier-main"})


def finish(
    recs: list[Recommendation], report: ReportModel, snapshot: Snapshot | None, units: Units | None
) -> list[Recommendation]:
    """Consolidate, reword and order ``recs`` (see the module docstring).
    Returns a new list; the recommendations in it are changed in place."""
    ctx = _Context(report=report, snapshot=snapshot, units=units)
    recs = _merge_model_tier(list(recs), ctx)
    recs = _consolidate_compaction(recs, ctx)
    # Nothing in an agent file can change how Claude Code starts these.
    recs = [r for r in recs if not (r.id == "spawn-cost" and r.agent_type in NOT_OVERRIDABLE)]
    for rec in recs:
        explain = _EXPLAIN.get(rec.id)
        if explain is not None:
            explain(rec, ctx)
        if rec.scope == "managed" and rec.changes and "administrator" not in rec.action:
            rec.action += " Your organisation's managed settings set this, so only your administrator can change it."
    recs = _drop_applied(recs)
    # Most important first: severity, then the largest estimated saving.
    # The main session's model goes last in its group: a quality trade
    # ranks below the tips that keep the same model (compaction, say).
    recs.sort(
        key=lambda r: (
            _SEVERITY_ORDER.get(r.severity, 3),
            r.id in _LAST_IN_GROUP,
            -(r.saving_usd or 0.0),
        )
    )
    return recs


__all__ = ["NOT_OVERRIDABLE", "finish"]

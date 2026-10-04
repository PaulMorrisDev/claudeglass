"""Agents that ran on a larger model than their work needed, and what the
Sonnet counterfactual would have saved.

``model_swap`` answers "what if this agent type ran one tier down". This
module asks a narrower question of each subagent and workflow agent
whose model nobody chose: did it write code to a settled spec on a model
above Sonnet because the call that started it named no model? Three
verdicts, each at its own severity (see :data:`RULES`):

- ``inherited`` -- the call set no model, no agent file chose one, and the
  agent wrote code on Opus or Fable. The one verdict that carries advice;
  it drops to info once later writers of the same kind ran on Sonnet or
  smaller, so a rule that is already followed stops nagging.
- ``asked`` -- the same, but the call named the larger model. Priced as a
  ceiling for information; never added to the ``inherited`` figure.
- ``decide-apply`` -- an agent whose role is to decide (review, audit, ...)
  that also edited code on a larger model. Cost and edit counts only: its
  fix is a split into two agents, not a swap, so there is no saving.

Whether the model was chosen is read, never inferred: a ``.meta.json`` of
the newer shape (``TranscriptMeta.model_recorded``) leaves ``model`` out
exactly when the call set none, so an agent with no ``agent_model_alias``
inherited. The agent's role is its canonical word
(``TranscriptMeta.role_word``, see ``agent_roles``) and what it did is
whether a reply edited a file or wrote to a path outside the temp dir
(``Turn.edit_kind``/``Turn.shell_write_count``). A role word that writes
code, and a write, is a writer; so is an agent with no word that edited
code in several replies. Integrators and deciders are never writers.

Three layers, mirroring ``model_swap.py``'s own shape:

- :func:`compute_agent_models` -- a pure function folding every transcript
  into one :class:`AgentModelGroup` per (group, verdict). The group is
  ``"workflow-subagent"`` for every workflow agent (the lever is the
  script's ``agent()`` call whatever its ``agentType``), else the agent
  type. Each flagged agent's replies are repriced at Sonnet with the same
  ``price_turn`` ``model_swap`` uses: same tokens, same cache-write split,
  only the rate changes.
- :func:`build_table` -- renders the stats as ``model_swap_agent_models``,
  one row per group and verdict. The report appends it to the
  ``model_swap`` section.
- :data:`RULES` -- one rule per verdict, reading only the rendered table
  (as ``model_swap.RULES`` does), so a card never sees raw stats. The
  card's words live in ``advice.py`` and ``fixes.py``.

Privacy: the table and every rule carry only agent types, canonical role
words, counts, model ids, amounts and dates. No prompt, label, description,
phase or path text is read or kept.

Every saving is a price ceiling at today's token counts, the same caveat as
``model_swap`` (see :data:`ASSUMPTIONS`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable, Sequence

from . import agent_roles, workstyle
from .model import Column, Recommendation, ReportModel, Table, TranscriptResult, Turn
from .model_swap import _ALL_ARCHETYPES, _NO_SUBAGENT_ARCHETYPES, _dominant_label
from .pricing import Pricing, model_name, price_turn
from .snapshots import Snapshot

#: The group every workflow agent is filed under: the lever is the
#: script's ``agent()`` call, whatever agent type it asked for.
WORKFLOW_GROUP = "workflow-subagent"

#: Agent types nothing can change the model of, or that name no type.
_SKIPPED_TYPES = frozenset({"fork", "unknown"})

#: Verdicts in the order the table lists them.
VERDICTS: tuple[str, ...] = ("inherited", "asked", "decide-apply")

#: The environment variable that sets every subagent's model at once.
ENV_VAR = "CLAUDE_CODE_SUBAGENT_MODEL"

#: The table's name, and the ``section.table`` its rules cite as evidence.
TABLE_NAME = "model_swap_agent_models"
_SOURCE_SECTION = "model_swap"

#: Model tiers: ``workstyle.model_tier`` ranks fable 3, opus 2, sonnet 1,
#: haiku 0. Above Sonnet is Opus and up.
_ABOVE_SONNET = 2
_SONNET_OR_SMALLER = 1

_UNKNOWN_ROLE = "other"

#: Deviations and caveats, reported per this project's own convention.
ASSUMPTIONS: list[str] = [
    "the saving for agents on a larger model is a ceiling at today's token counts: the same tokens priced at "
    "Sonnet's list rate. Sonnet may need more replies, so it is never a forecast",
    "an agent's role is read from its workflow phase, its agent type or the first words of its description. "
    "Only the role word is kept, never the text it came from",
]


# -- thresholds --------------------------------------------------------------


@dataclass(slots=True)
class AgentModelThresholds:
    """Every tunable number this module's verdicts depend on, in the same
    config-driven shape as ``model_swap.ModelSwapThresholds``."""

    #: An ``inherited`` card drops from advice to info when this many
    #: later writers of the same kind (workflow or Agent tool) ran on
    #: Sonnet or smaller.
    later_compliant_for_info: int = 3
    #: An agent no role word describes counts as a writer when at least
    #: this many of its replies edited code.
    unknown_min_edit_turns: int = 2
    #: A deciding agent counts as also applying changes when at least this
    #: many of its replies edited code.
    decide_apply_min_edit_turns: int = 3

    @classmethod
    def from_config(cls, config: dict | None) -> "AgentModelThresholds":
        """Build thresholds from a config dict, keeping this class's
        defaults for any key that's absent.

        Accepts either a flat dict of this class's field names or a full
        ``config.toml``-shaped ``[thresholds]`` dict with a nested
        ``agent_models`` table, the same dual shape as
        ``ModelSwapThresholds.from_config``. Unknown keys are ignored.
        """
        data = config or {}
        if not isinstance(data, dict):
            data = {}
        nested = data.get("agent_models")
        if isinstance(nested, dict):
            data = nested

        kwargs: dict = {}
        for name in ("later_compliant_for_info", "unknown_min_edit_turns", "decide_apply_min_edit_turns"):
            if name in data:
                kwargs[name] = int(data[name])
        return cls(**kwargs)

    def describe(self) -> list[str]:
        """One sentence per threshold, for the table's notes -- same
        convention as ``ModelSwapThresholds.describe``."""
        return [
            f"An agent no role word describes counts as writing code when at least {self.unknown_min_edit_turns} "
            "of its replies edited code.",
            f"The card drops to For your information once {self.later_compliant_for_info} or more later agents "
            "that wrote code ran on Sonnet or a smaller model.",
            f"An agent that decides counts as also changing code when at least {self.decide_apply_min_edit_turns} "
            "of its replies edited code.",
        ]


_DEFAULT_THRESHOLDS = AgentModelThresholds()


# -- accumulation --------------------------------------------------------------


@dataclass(slots=True)
class AgentModelGroup:
    """Rolled-up stats for one (group, verdict) -- the values behind one
    row of ``model_swap_agent_models``."""

    #: ``WORKFLOW_GROUP`` for every workflow agent, else the agent type.
    key: str
    #: ``"workflow"`` or ``"agent tool"``.
    kind: str
    verdict: str
    runs: int = 0
    #: Role word (``"other"`` when no word) -> agents.
    role_counts: dict[str, int] = field(default_factory=dict)
    #: Each agent's most common model id -> agents.
    model_counts: dict[str, int] = field(default_factory=dict)
    #: The agents' replies priced at their own models.
    cost: float = 0.0
    #: The same replies priced at Sonnet (stays 0.0 for ``decide-apply``,
    #: which is never repriced, and unused when the rate card has no
    #: Sonnet alias).
    cost_on_sonnet: float = 0.0
    #: Replies that edited a file or wrote to a path outside the temp dir.
    write_turns: int = 0
    workflow_run_ids: set[str] = field(default_factory=set)
    #: ``YYYY-MM-DD`` of each agent's first reply; "" until one is seen.
    first_seen: str = ""
    last_seen: str = ""
    #: Writers of the same kind on Sonnet or smaller that started after the
    #: latest agent in this group. Only ever set for ``inherited``.
    later_compliant: int = 0


@dataclass(slots=True)
class AgentModelStats:
    """The whole roll-up: one :class:`AgentModelGroup` per (group, verdict),
    plus the facts every row shares."""

    groups: dict[tuple[str, str], AgentModelGroup] = field(default_factory=dict)
    #: ``CLAUDE_CODE_SUBAGENT_MODEL`` is among the snapshot's env names.
    env_var_set: bool = False
    #: The rate card's current Sonnet model id, or None when it has none
    #: (then no row carries a saving).
    sonnet_model: str | None = None
    #: Flagged agents' replies whose model the rate card couldn't price;
    #: they count as zero, in both the observed and the Sonnet cost.
    unpriced_turns: int = 0
    thresholds: AgentModelThresholds = field(default_factory=AgentModelThresholds)


@dataclass(slots=True)
class _Run:
    """What one agent run tells the verdicts. Private: never leaves
    :func:`compute_agent_models`."""

    group: str
    kind: str
    verdict: str | None
    #: A writer on Sonnet or smaller, by a known tier (counts towards "this looks fixed").
    compliant: bool
    role: str
    model: str
    write_turns: int
    workflow_run_id: str | None
    first_ts: datetime | None
    date: str
    turns: list[Turn]


def _priced_turns(result: TranscriptResult) -> list[Turn]:
    """Turns that actually got a ``turn_index`` -- same convention as
    ``model_swap._priced_turns``, duplicated for the same reason."""
    return [t for t in result.turns if t.turn_index > 0]


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _file_chose(file_model: object, run_tier: int) -> bool:
    """The agent's file names a model that explains the one it ran on: an
    explicit ``inherit``, or a model of the same family. A file naming
    another family doesn't excuse the run."""
    if not isinstance(file_model, str) or not file_model.strip():
        return False
    if file_model.strip().lower() == "inherit":
        return True
    file_tier = workstyle.model_tier(file_model)
    return file_tier != -1 and file_tier == run_tier


def _analyse(
    result: TranscriptResult,
    agent_files: dict,
    th: AgentModelThresholds,
) -> _Run | None:
    """The run's verdict and the facts behind it; None for a run this
    module never judges (a direct run, a fork, an untyped subagent, an
    older meta that can't say whether a model was set, or a run with no
    priced turn)."""
    meta = result.meta
    if meta.kind not in ("subagent", "workflow-agent") or not meta.model_recorded:
        return None
    is_workflow = meta.kind == "workflow-agent"
    run_type = meta.agent_type or (WORKFLOW_GROUP if is_workflow else None)
    if run_type is None or run_type in _SKIPPED_TYPES:
        return None
    priced = _priced_turns(result)
    if not priced:
        return None

    model_counts: dict[str, int] = {}
    for turn in priced:
        if turn.model:
            model_counts[turn.model] = model_counts.get(turn.model, 0) + 1
    model = _dominant_label(model_counts) or ""
    tier = workstyle.model_tier(model) if model else -1

    write_turns = sum(1 for t in result.turns if t.edit_kind == "real" or t.shell_write_count > 0)
    role_class = agent_roles.role_class(meta.role_word)
    writer = (role_class == "writer" and write_turns >= 1) or (
        role_class is None and write_turns >= th.unknown_min_edit_turns
    )

    if meta.agent_model_alias:
        chosen_by = "call"
    elif run_type != WORKFLOW_GROUP and _file_chose(agent_files.get(run_type), tier):
        chosen_by = "file"
    else:
        chosen_by = "inherited"

    verdict = None
    if tier >= _ABOVE_SONNET:
        if writer:
            # A model its agent file chose was chosen on purpose, so it gets
            # no verdict here. model-tier prices such a run only when it is a
            # direct Agent-tool run; a workflow agent's is left alone.
            verdict = {"inherited": "inherited", "call": "asked"}.get(chosen_by)
        elif role_class == "decider" and write_turns >= th.decide_apply_min_edit_turns:
            verdict = "decide-apply"

    return _Run(
        group=WORKFLOW_GROUP if is_workflow else run_type,
        kind="workflow" if is_workflow else "agent tool",
        verdict=verdict,
        compliant=writer and 0 <= tier <= _SONNET_OR_SMALLER,
        role=meta.role_word or _UNKNOWN_ROLE,
        model=model,
        write_turns=write_turns,
        workflow_run_id=meta.workflow_run_id,
        first_ts=_parse_ts(priced[0].ts),
        date=(priced[0].ts or "")[:10],
        turns=priced,
    )


def compute_agent_models(
    results: Sequence[TranscriptResult],
    pricing: Pricing,
    *,
    agent_files: dict | None = None,
    env_names: Iterable[str] | None = None,
    thresholds: AgentModelThresholds | None = None,
) -> AgentModelStats:
    """Fold every subagent and workflow-agent transcript in ``results``
    into one :class:`AgentModelGroup` per (group, verdict).

    ``agent_files`` maps an agent type to the ``model`` its agent file
    names (None when the file names none), from the latest snapshot's
    effective agents. ``env_names`` is the snapshot's environment variable
    names: ``ENV_VAR`` among them sets ``env_var_set``. Both are
    optional: without them no run counts as file-chosen and
    ``env_var_set`` is False.
    """
    th = thresholds or _DEFAULT_THRESHOLDS
    files = agent_files or {}
    names = set(env_names or ())
    sonnet_model = pricing.aliases.get("sonnet")
    sonnet_rates = pricing.models.get(sonnet_model) if sonnet_model else None
    stats = AgentModelStats(
        env_var_set=ENV_VAR in names,
        sonnet_model=sonnet_model if sonnet_rates is not None else None,
        thresholds=th,
    )

    compliant: list[_Run] = []
    latest_flagged: dict[tuple[str, str], datetime] = {}
    for result in results:
        run = _analyse(result, files, th)
        if run is None:
            continue
        if run.compliant:
            compliant.append(run)
        if run.verdict is None:
            continue

        key = (run.group, run.verdict)
        group = stats.groups.get(key)
        if group is None:
            group = stats.groups[key] = AgentModelGroup(key=run.group, kind=run.kind, verdict=run.verdict)
        group.runs += 1
        group.role_counts[run.role] = group.role_counts.get(run.role, 0) + 1
        group.model_counts[run.model] = group.model_counts.get(run.model, 0) + 1
        group.write_turns += run.write_turns
        if run.workflow_run_id:
            group.workflow_run_ids.add(run.workflow_run_id)
        if run.date:
            group.first_seen = min(group.first_seen or run.date, run.date)
            group.last_seen = max(group.last_seen, run.date)
        if run.first_ts is not None and (key not in latest_flagged or run.first_ts > latest_flagged[key]):
            latest_flagged[key] = run.first_ts

        for turn in run.turns:
            observed = price_turn(turn, pricing.resolve_model(turn.model))
            if not observed.model_known:
                stats.unpriced_turns += 1
                continue
            group.cost += observed.total
            if run.verdict != "decide-apply" and sonnet_rates is not None:
                group.cost_on_sonnet += price_turn(turn, sonnet_rates).total

    for (group_key, verdict), group in stats.groups.items():
        latest = latest_flagged.get((group_key, verdict))
        if verdict != "inherited" or latest is None:
            continue
        group.later_compliant = sum(
            1 for run in compliant if run.kind == group.kind and run.first_ts is not None and run.first_ts > latest
        )
    return stats


# -- report table --------------------------------------------------------------


def _roles_text(role_counts: dict[str, int]) -> str:
    """"implement 4, fix 1": each role word and its agents, most first
    (ties in word order); agents no word describes are "other"."""
    ordered = sorted(role_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return ", ".join(f"{word} {count}" for word, count in ordered)


#: Each verdict as the row label's second half ("Workflow agents, no model
#: set"). Display only; ``helptext`` has the verdict column's own labels.
_VERDICT_PHRASES = {
    "inherited": "no model set",
    "asked": "asked for a larger model",
    "decide-apply": "decided and changed code",
}


def build_table(stats: AgentModelStats) -> Table:
    """Render a finished :class:`AgentModelStats` as
    ``model_swap_agent_models``: one row per group and verdict, verdicts in
    :data:`VERDICTS` order, then the biggest saving first. No ids, labels
    or paths.

    The first column, the row key every card's evidence cites, is
    ``"<group>:<verdict>"`` (``"workflow-subagent:inherited"``): an agent
    type can have a row for more than one verdict, so the type alone isn't
    unique. ``value_labels`` shows it as "Workflow agents, no model set";
    the agent type and the verdict keep columns of their own for anything
    that reads the raw values."""
    columns = [
        Column(key="case", label="Agents and finding", kind="str"),
        Column(key="agent_type", label="Agent type", kind="str"),
        Column(key="verdict", label="Verdict", kind="str"),
        Column(key="runs", label="Agents", kind="int"),
        Column(key="roles", label="Roles", kind="str"),
        Column(key="model", label="Model", kind="str"),
        Column(key="cost_usd", label="Cost", kind="money"),
        Column(key="cost_on_sonnet_usd", label="Cost on Sonnet", kind="money"),
        Column(key="saving_usd", label="Most you could save", kind="money"),
        Column(key="saving_pct", label="Ceiling saving (%)", kind="pct"),
        Column(key="write_turns", label="Edit turns", kind="int"),
        Column(key="workflow_runs", label="Workflow runs", kind="int"),
        Column(key="first_seen", label="First seen", kind="str"),
        Column(key="last_seen", label="Last seen", kind="str"),
        Column(key="later_compliant", label="Later writers on Sonnet or smaller", kind="int"),
        Column(key="env_var_set", label="Model environment variable set", kind="str"),
    ]

    def _saving(group: AgentModelGroup) -> float | None:
        if group.verdict == "decide-apply" or stats.sonnet_model is None:
            return None
        return max(group.cost - group.cost_on_sonnet, 0.0)

    def _order(group: AgentModelGroup) -> tuple:
        return (VERDICTS.index(group.verdict), -(_saving(group) or 0.0), -group.cost, group.key)

    rows: list[list] = []
    labels: dict[str, str] = {}
    for group in sorted(stats.groups.values(), key=_order):
        saving = _saving(group)
        saving_pct = None if saving is None else (100.0 * saving / group.cost if group.cost > 0 else 0.0)
        key = f"{group.key}:{group.verdict}"
        who = "Workflow agents" if group.key == WORKFLOW_GROUP else f"{group.key} agents"
        labels[key] = f"{who}, {_VERDICT_PHRASES[group.verdict]}"
        rows.append(
            [
                key,
                group.key,
                group.verdict,
                group.runs,
                _roles_text(group.role_counts),
                _dominant_label(group.model_counts) or "",
                group.cost,
                None if saving is None else group.cost_on_sonnet,
                saving,
                saving_pct,
                group.write_turns,
                # A workflow agent whose run file named no run still came from one.
                max(len(group.workflow_run_ids), 1) if group.kind == "workflow" else 0,
                group.first_seen,
                group.last_seen,
                group.later_compliant,
                "yes" if stats.env_var_set else "no",
            ]
        )

    notes = [f"Thresholds: {' '.join(stats.thresholds.describe())}"]
    if stats.unpriced_turns:
        notes.append(
            "Some replies used a model with no price in your pricing file. They count as zero in both costs."
        )
    return Table(
        name=TABLE_NAME,
        title="Agents that ran on a larger model than their work needed",
        columns=columns,
        rows=rows,
        notes=notes,
        value_labels=labels,
    )


# -- report-lookup helpers --------------------------------------------------
#
# Duplicated from model_swap.py (itself a copy of recommend.py's): small,
# private and stable, so copied rather than imported.


def _table(report: ReportModel, section_key: str, table_name: str) -> Table | None:
    for section in report.sections:
        if section.key == section_key:
            for table in section.tables:
                if table.name == table_name:
                    return table
    return None


def _col_index(table: Table, column_key: str) -> int | None:
    for idx, column in enumerate(table.columns):
        if column.key == column_key:
            return idx
    return None


def _evidence(label: str, value, section_key: str, table_name: str, row_key) -> tuple:
    return (label, value, f"{section_key}.{table_name}", row_key)


# -- rules --------------------------------------------------------------------

#: Evidence label -> the table column it reads, in the order every card
#: cites them.
_EVIDENCE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("Agents", "runs"),
    ("Roles", "roles"),
    ("Model", "model"),
    ("Cost (USD)", "cost_usd"),
    ("Cost on Sonnet (USD)", "cost_on_sonnet_usd"),
    ("Ceiling saving (USD)", "saving_usd"),
    ("Ceiling saving (%)", "saving_pct"),
    ("Edit turns", "write_turns"),
    ("Workflow runs", "workflow_runs"),
    ("First seen", "first_seen"),
    ("Last seen", "last_seen"),
    ("Later compliant writers", "later_compliant"),
    (f"{ENV_VAR} set", "env_var_set"),
)

#: Plain fallback wording per verdict: ``advice.py`` rewrites the title and
#: action, so these only matter for a caller that skips it.
_FALLBACK_TITLE = {
    "inherited": "{who} wrote code on {model} with no model set",
    "asked": "{who} that write code were started on {model}",
    "decide-apply": "{who} decided and changed code on {model}",
}
_FALLBACK_ACTION = {
    "inherited": "Set the model on every agent you start: Sonnet for agents that write code, Opus for agents that decide.",
    "asked": "Check what starts these agents. Ask for Sonnet where nothing needs Opus.",
    "decide-apply": "Split this work: an Opus agent that decides, then a Sonnet agent that applies it.",
}


def _number(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _recommendations(
    report: ReportModel,
    th: AgentModelThresholds,
    archetype: str | None,
    rule_id: str,
    verdict: str,
) -> list[Recommendation]:
    """One recommendation per ``model_swap_agent_models`` row of ``verdict``.
    Suppressed for an archetype that never spawns subagents (same gate as
    ``model-tier``)."""
    if archetype in _NO_SUBAGENT_ARCHETYPES:
        return []
    table = _table(report, _SOURCE_SECTION, TABLE_NAME)
    if table is None:
        return []
    index = {column.key: i for i, column in enumerate(table.columns)}
    if "case" not in index or "agent_type" not in index or "verdict" not in index:
        return []

    def cell(row: list, column_key: str):
        idx = index.get(column_key)
        return row[idx] if idx is not None and idx < len(row) else None

    out: list[Recommendation] = []
    for row in table.rows:
        if cell(row, "verdict") != verdict:
            continue
        agent_type = cell(row, "agent_type")
        later = cell(row, "later_compliant")
        later = later if isinstance(later, int) and not isinstance(later, bool) else 0

        severity = "info"
        variant = ""
        if verdict == "inherited":
            if later >= th.later_compliant_for_info:
                variant = "fixed"
            else:
                severity = "advice"

        who = "Workflow agents" if agent_type == WORKFLOW_GROUP else f"{agent_type} agents"
        # The row's own key: the agent type alone repeats across verdicts.
        row_key = cell(row, "case")
        out.append(
            Recommendation(
                id=rule_id,
                severity=severity,
                category="workflow",
                archetypes=_ALL_ARCHETYPES,
                title=_FALLBACK_TITLE[verdict].format(who=who, model=model_name(str(cell(row, "model") or ""))),
                action=_FALLBACK_ACTION[verdict],
                lever="model",
                scope="user",
                agent_type=agent_type,
                subject=str(cell(row, "last_seen") or ""),
                saving_usd=_number(cell(row, "saving_usd")),
                variant=variant,
                evidence=[
                    _evidence(label, cell(row, column_key), _SOURCE_SECTION, TABLE_NAME, row_key)
                    for label, column_key in _EVIDENCE_COLUMNS
                ],
            )
        )
    return out


def _rule_agent_model_inherited(
    report: ReportModel,
    th: AgentModelThresholds,
    archetype: str | None = None,
    snapshot: Snapshot | None = None,
) -> list[Recommendation]:
    """``agent-model-inherited``: agents that wrote code on a larger model
    because no model was set. Advice, or an info tip with ``variant ==
    "fixed"`` once ``th.later_compliant_for_info`` later writers ran on
    Sonnet or smaller. ``snapshot`` is accepted for the shared rule
    signature; nothing here reads it."""
    return _recommendations(report, th, archetype, "agent-model-inherited", "inherited")


def _rule_agent_model_asked(
    report: ReportModel,
    th: AgentModelThresholds,
    archetype: str | None = None,
    snapshot: Snapshot | None = None,
) -> list[Recommendation]:
    """``agent-model-asked``: agents that wrote code on a larger model
    because the call named it. Info only."""
    return _recommendations(report, th, archetype, "agent-model-asked", "asked")


def _rule_agent_decide_apply(
    report: ReportModel,
    th: AgentModelThresholds,
    archetype: str | None = None,
    snapshot: Snapshot | None = None,
) -> list[Recommendation]:
    """``agent-decide-apply``: agents that decide, and also changed code, on
    a larger model. Info only, with no saving."""
    return _recommendations(report, th, archetype, "agent-decide-apply", "decide-apply")


#: One rule per verdict, keyed by id -- ``recommend.recommend`` runs each as
#: ``RULES[id](report, thresholds, archetype, snapshot)``.
RULES: dict[str, Callable[..., list[Recommendation]]] = {
    "agent-model-inherited": _rule_agent_model_inherited,
    "agent-model-asked": _rule_agent_model_asked,
    "agent-decide-apply": _rule_agent_decide_apply,
}


__all__ = [
    "ASSUMPTIONS",
    "AgentModelGroup",
    "AgentModelStats",
    "AgentModelThresholds",
    "ENV_VAR",
    "RULES",
    "TABLE_NAME",
    "VERDICTS",
    "WORKFLOW_GROUP",
    "build_table",
    "compute_agent_models",
]

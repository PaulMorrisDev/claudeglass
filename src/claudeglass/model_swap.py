"""Model-swap counterfactual (v4-model-swap): for each subagent type and
the top-level conversation, what would today's already-observed token
volumes have cost at every other model this rate card knows, and — the
number that actually leads somewhere — what's the ceiling saving from
moving one tier down (fable -> opus -> sonnet -> haiku)?

This module answers a narrower, more honest question than "switch model
X to model Y and save $Z": it holds every observed token volume and the
observed 5m/1h cache-write split exactly constant (the same
``price_turn`` the rest of the engine uses, called with
``write_split=None`` so it falls back to the turn's own ``cc_5m``/
``cc_1h`` — see ``pricing.price_turn``'s docstring) and only swaps the
per-token rate. A smaller model may need more turns to reach the same
result, or fail the task outright, and neither of those possibilities
has any representation here — every saving figure this module produces
is a **price ceiling at today's usage shape**, never a prediction of
what a real swap would cost. ``build_section``'s notes and every
``model-tier`` recommendation's action text restate this explicitly, per
the project's "every number must lead to a lever, honestly" convention.

Three layers, mirroring ``ttl.py``/``topology.py``'s own shape:

- :func:`compute_model_swap` — a pure function (no ``add``-style
  accumulator; the whole corpus's transcripts are handed in at once) that
  folds every priced turn into a per-agent-type :class:`ModelSwapTypeStats`,
  keyed exactly like ``TtlStats.add`` (``"top-level"`` for a
  ``TranscriptMeta.kind == "top-level"`` transcript, else
  ``TranscriptMeta.agent_type`` or ``"unknown"``): observed cost (each
  turn priced at its own resolved model, so a mixed-model agent type is
  priced correctly), and — for every model this rate card carries, not
  just the four current-generation ones — the same turns repriced flat
  at that model's rate. The tier verdict (see below) is resolved here,
  once, because it needs ``Pricing.aliases`` and ``build_section`` is
  deliberately pricing-free (matching the literal signature the work
  order asked for).
- :func:`build_section` — renders a finished :class:`ModelSwapStats` as
  the report's ``model_swap`` section: ``model_swap_by_agent_type`` (one
  row per agent type, observed cost, cost at every alternative model,
  and the one-tier-down verdict) and ``model_swap_summary`` (the
  corpus-wide ceiling if every subagent type currently on Fable/Opus
  moved one tier down).
- :data:`RULES` — one rule, ``model-tier``, built the same way
  ``recommend.py``'s own rules are (reads back the *rendered*
  ``model_swap_by_agent_type`` table, never the raw stats — see below),
  so a future ``recommend.py`` (off-limits to this work order — see
  deviations) can fold it in by calling ``RULES["model-tier"](report,
  th, archetype, snapshot)``.

Tier order: this module deliberately reuses ``workstyle.model_tier``'s
existing "fable(3) > opus(2) > sonnet(1) > haiku(0)" family-substring
ranking rather than inventing a cost-derived ordering of its own. Each
family's bare alias (``"fable"``/``"opus"``/``"sonnet"``/``"haiku"``)
resolves in ``pricing.toml`` to that family's current model
(``claude-fable-5-1``, ``claude-opus-5-5``, ``claude-sonnet-5-5`` and
``claude-haiku-4-5-20251001`` today — newer than two of the four the
work order names as "at minimum" present, ``claude-opus-5`` and
``claude-sonnet-5``, which stay priced), so "one tier down" is resolved
via ``Pricing.aliases[family]`` — the public alias table, per the work
order's "resolve via pricing.py's public API, do not hardcode" — never
a hardcoded model id. If a future rate card drops a family's bare alias
entirely, that agent type's row reports "unknown tier" rather than
guessing at a specific dated id.

Deviations from the brief, reported rather than made silently (project
convention — see ``model.py``'s own module docstring):

- **Every model in ``pricing.toml`` is priced as an alternative column**,
  not just the four current-generation ones the brief names "at
  minimum" — read literally, "every model in pricing.toml with known
  rates" is the full set (legacy dated ids included), and the four named
  models are a floor on that set, not a ceiling on it.
- **The RULES-firing "one tier down" pick is narrower than "cheapest
  alternative overall"**: it is specifically the immediately next
  cheaper *family*'s current aliased model (via ``Pricing.aliases``),
  matching the rule id ``model-tier`` and the brief's own "one-tier-down
  saving" wording — jumping straight from Fable to Haiku is a bigger,
  differently-risky move than "move down one tier", so this module never
  recommends it as the ``model-tier`` action even though it's visible as
  a column in ``model_swap_by_agent_type``.
- **``recommend.py``, ``model.py``, ``pricing.py`` and ``report.py`` are
  off-limits to this work order** (a wiring agent integrates this module
  afterwards — see the module's own header comment in this repo's task
  brief). Several small private helpers this module needs — the
  report-lookup helpers (``_table``/``_col_index``/``_row``/``_cell``/
  ``_evidence``), the minimum-sample row gate (``_row_meets_min_sample``),
  the archetype-gating constants (``_ALL_ARCHETYPES``/
  ``_NO_SUBAGENT_ARCHETYPES``), and the lever-scope convention
  (``"user"``/``"repo"``/``"managed"``) — already exist in
  ``recommend.py`` in exactly this shape, and ``workstyle.py`` already
  carries the private ``_TIER_FAMILIES`` tuple this module's tier lookup
  needs the family *name* for (``workstyle.model_tier`` returns only the
  rank). Rather than reach into another module's underscore-prefixed
  internals across a file this module can't also keep in lockstep with,
  every one of these is duplicated locally, in the same shape, following
  ``topology.py``'s own documented precedent for ``_transcript_cost``
  ("deliberately duplicated ... rather than imported"). A wiring agent
  editing ``recommend.py`` calls ``model_swap.RULES["model-tier"](...)``
  directly rather than merging this module's copies back in.
- **Lever scope for the per-agent-type ``model`` lever is computed
  locally** (``_scope_for``) instead of via ``recommend.py``'s private
  ``_lever_scope``/``_AGENT_LEVER_RE``: that regex only ever matched the
  TTL-switch lever's specific wording (``"experimental.cacheTtl in
  X.md"``), and since ``Recommendation.agent_type`` is set on every row
  here, ``recommend.py``'s own ``render_patch_set`` already routes by
  ``agent_type`` first (Fix R13), never falling back to the regex, for a
  recommendation from this module — so a text-format match was never
  necessary here. Scope is simply "user" for the top-level row's
  ``settings.json`` lever and "repo" for a subagent's
  ``.claude/agents/<type>.md`` frontmatter lever, upgraded to "managed"
  when ``"model"`` appears in ``snapshots.managed_keys(snapshot)`` — the
  same three-value convention, computed directly from what this module
  already knows about the row rather than sniffed back out of text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Sequence

from . import workstyle
from .model import Column, Recommendation, ReportModel, Section, Table, TranscriptResult, Turn, agent_type_label
from .pricing import Pricing, model_name, price_turn
from .snapshots import Snapshot, managed_keys

if TYPE_CHECKING:
    from .units import Units

#: See module docstring's tier-order deviation note: kept in lock-step
#: with (never imported from) ``workstyle._TIER_FAMILIES`` — the
#: family name at rank ``r`` is ``_TIER_FAMILIES[r]``, and
#: ``workstyle.model_tier`` returns exactly that rank.
_TIER_FAMILIES: tuple[str, ...] = ("haiku", "sonnet", "opus", "fable")

#: The exact phrase every "no cheaper alternative exists" state's label
#: contains, so a caller (a rule, a test, a reader skimming the report)
#: never mistakes "already on the cheapest model" for a real saving.
_ALREADY_CHEAPEST_LABEL = "already on the cheapest model"

#: Families never suggested for the main session (``"top-level"``): a
#: Sonnet main session gets the ``"main_floor"`` verdict instead of Haiku.
_MAIN_SESSION_BELOW_FLOOR = frozenset({"haiku"})
_MAIN_FLOOR_LABEL = "Sonnet is the smallest model suggested for your main session"

#: Agent types no agent file sets the model for: a workflow script
#: starts ``workflow-subagent`` runs, and Claude Code starts forks and
#: untyped subagents itself.
_NO_AGENT_FILE = frozenset({"unknown", "fork", "workflow-subagent"})

#: Archetypes that never spawn subagents of their own -- duplicated from
#: ``recommend.py``'s own constant of the same name (see module
#: docstring's deviation note): per-agent-type model advice makes no
#: sense for a session that never runs a subagent.
_NO_SUBAGENT_ARCHETYPES = frozenset({"chat-only"})

#: Every archetype ``workstyle.detect_archetype``/``corpus_archetype``
#: can return; the empty tuple means "no restriction" -- same convention
#: as ``recommend.py``'s ``_ALL_ARCHETYPES``.
_ALL_ARCHETYPES: tuple[str, ...] = ()

#: Deviations from the brief/plan, reported per this project's own
#: convention -- see module docstring for the full explanation of each.
ASSUMPTIONS: list[str] = [
    "token volumes, turn counts and the observed 5-minute/1-hour cache-write split stay the same when each "
    "alternative model is priced. So every saving figure is a price ceiling at today's usage, never a prediction",
    "a smaller model may need more turns to reach the same result, or fail the task outright. Neither is priced "
    "here. With metrics capture on, the evidence cites how hard Claude reported the work, how many of the agent's "
    "calls were a single read-only look and how many came before its first edit. A run retried because the "
    "model wasn't enough holds the suggestion back. None of this ever changes a figure",
    "alternative columns cover every model in pricing.toml, older dated ids included. But the cheaper-model "
    "advice only ever suggests the next cheaper family's current model, never the cheapest alternative overall",
    "tier order (Fable, then Opus, then Sonnet, then Haiku) follows each model's family name, not its price",
    "a subagent's saving counts only the runs its agent file's model line decides: runs started without a model "
    "of their own. A model named when the run started wins over the agent file. A workflow agent's model comes "
    "from its agent() call, else its agent type's file, else CLAUDE_CODE_SUBAGENT_MODEL, else your main session's "
    "model. A workflow has no default of its own: the run file's defaultModel only records the main model at "
    "launch. Every workflow agent run counts as set by its script, so none is in an agent type's saving",
]


def _priced_turns(result: TranscriptResult) -> list[Turn]:
    """Turns that actually got a ``turn_index`` — same convention as
    ``topology._priced_turns``/``ttl``'s own filtering, duplicated here
    for the same "small private helper, not worth a cross-module
    import" reason given in the module docstring."""
    return [t for t in result.turns if t.turn_index > 0]


def _agent_type_label(result: TranscriptResult) -> str:
    """"top-level" for the main conversation, else the recorded agent
    type (or "unknown") -- exactly ``TtlStats.add``'s own keying, so a
    corpus fed to both modules always agrees on group boundaries."""
    return agent_type_label(result)


def model_set_by(result: TranscriptResult) -> str:
    """What decided this run's model, in Claude Code's order (a model
    passed for that spawn, then the agent file's ``model``, then
    ``CLAUDE_CODE_SUBAGENT_MODEL``, then the main model):

    - ``"settings"`` -- the main session: its ``model`` setting;
    - ``"workflow"`` -- a run a workflow script started. Its model is the
      ``agent()`` call's ``model``, else its ``agentType``'s agent file,
      else ``CLAUDE_CODE_SUBAGENT_MODEL``, else the main session's model:
      a workflow has no default of its own (the run file's
      ``defaultModel`` only records the main model at launch). Every such
      run counts as the script's, whatever ``agentType`` it names, since
      the ``agent()`` call is the lever the script holds;
    - ``"spawn"`` -- a direct run whose spawn named a model. The
      ``.meta.json`` ``model`` (``agent_model_alias``) is the model the
      spawn asked for, absent when it asked for none, and it wins over the
      agent file;
    - ``"agent file"`` -- a direct run started without a model: its agent
      file's ``model`` line decides, or a new file for a built-in type;
    - ``"none"`` -- a fork or untyped subagent, which no agent file sets.
    """
    meta = result.meta
    if meta.kind == "top-level":
        return "settings"
    if meta.kind == "workflow-agent":
        return "workflow"
    if meta.agent_model_alias:
        return "spawn"
    if (meta.agent_type or "unknown") in _NO_AGENT_FILE:
        return "none"
    return "agent file"


#: ``model_set_by`` values the row's lever decides.
_LEVER_SETTERS = frozenset({"settings", "agent file"})


def _dominant_label(counts: dict[str, int]) -> str | None:
    """The most-observed key in ``counts``, ties broken lexicographically
    -- same convention as ``report._dominant_transcript_model``."""
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]


# -- thresholds --------------------------------------------------------------


@dataclass(slots=True)
class ModelSwapThresholds:
    """Every tunable number the ``model-tier`` rule's firing decision
    depends on, in the same config-driven shape as
    ``recache.RecacheThresholds``/``ttl.TtlThresholds``/
    ``limits.LimitThresholds``/``recommend.RecommendThresholds``.
    """

    #: A one-tier-down swap must clear both this percentage floor...
    saving_pct_min: float = 10.0
    #: ...and this absolute USD floor, at today's observed volumes,
    #: before ``model-tier`` fires for a row (both independently
    #: blocking -- same "and", not "or", convention as
    #: ``TtlThresholds.switch_pct``/``switch_usd``).
    saving_usd_min: float = 1.00
    #: Per-row minimum sample -- same convention as
    #: ``RecommendThresholds.min_sessions``/``min_turns`` and
    #: ``recommend._row_meets_min_sample``: a row needs at least this
    #: many spawns OR this many priced turns before its saving is
    #: trusted enough to recommend.
    min_sessions: int = 5
    min_turns: int = 200

    @classmethod
    def from_config(cls, config: dict | None) -> "ModelSwapThresholds":
        """Build thresholds from a config dict, keeping this class's
        defaults for any key that's absent or of the wrong shape.

        Accepts either a flat dict of this class's four field names
        directly, or a full ``config.toml``-shaped ``[thresholds]``
        dict with a nested ``model_swap`` table
        (``{"model_swap": {"saving_pct_min": ...}}``) -- whichever a
        caller happens to have loaded, same dual-shape convention as
        ``RecacheThresholds.from_config``. Unknown keys are ignored.
        """
        data = config or {}
        if not isinstance(data, dict):
            data = {}
        nested = data.get("model_swap")
        if isinstance(nested, dict):
            data = nested

        kwargs: dict = {}
        if "saving_pct_min" in data:
            kwargs["saving_pct_min"] = float(data["saving_pct_min"])
        if "saving_usd_min" in data:
            kwargs["saving_usd_min"] = float(data["saving_usd_min"])
        if "min_sessions" in data:
            kwargs["min_sessions"] = int(data["min_sessions"])
        if "min_turns" in data:
            kwargs["min_turns"] = int(data["min_turns"])
        return cls(**kwargs)

    def describe(self) -> list[str]:
        """One sentence per threshold, for the report's thresholds block
        and this module's own section notes -- same convention as
        ``RecacheThresholds.describe``/``TtlThresholds.describe``."""
        return [
            f"A one-tier-down swap is suggested only when the most it could save at today's "
            f"volumes is over {self.saving_pct_min:.1f}% and over ${self.saving_usd_min:.2f} at "
            "list price. Both must hold.",
            f"An agent type needs at least {self.min_sessions} runs or {self.min_turns} replies "
            "before its saving is trusted enough to suggest.",
        ]


_DEFAULT_THRESHOLDS = ModelSwapThresholds()


# -- tier verdict --------------------------------------------------------------


@dataclass(slots=True)
class TierVerdict:
    """The one-tier-down verdict for one agent type's row: whether a
    cheaper family is available, which model it names (``None`` unless
    ``state == "cheaper_available"``), the ceiling saving, and the
    human-readable label ``build_section`` puts in the table.

    ``state`` is one of ``"cheaper_available"`` / ``"already_cheapest"``
    / ``"main_floor"`` (the main session on Sonnet: never moved to Haiku)
    / ``"unknown_tier"`` / ``"no_data"`` / ``"set_elsewhere"`` (a named
    agent type none of whose runs followed its agent file's model: a
    workflow script or the spawn named it) / ``"no_lever"`` (a workflow
    script, fork or untyped subagent: no agent file sets its model) --
    every state other than ``"cheaper_available"`` carries
    ``saving_usd == saving_pct == 0.0``, so the table never implies a
    saving where none exists. The verdict is worked out on the runs the
    row's lever decides (``ModelSwapTypeStats.lever``) only.
    """

    state: str
    alt_model: str | None
    saving_usd: float
    saving_pct: float
    label: str


def _set_elsewhere_text(stats: "ModelSwapTypeStats") -> str:
    """How many of this row's runs something other than its lever named
    the model for, as a clause: "62 came from workflow scripts and 3 were
    given a model when they started". Empty when none."""
    parts = []
    if stats.workflow_runs:
        parts.append(f"{stats.workflow_runs} came from workflow scripts")
    if stats.spawn_model_runs:
        parts.append(
            f"{stats.spawn_model_runs} {'was' if stats.spawn_model_runs == 1 else 'were'} given a model when "
            f"{'it' if stats.spawn_model_runs == 1 else 'they'} started"
        )
    return " and ".join(parts)


def _reach_suffix(stats: "ModelSwapTypeStats") -> str:
    """", on the 21 runs its agent file sets" when a subagent type's
    saving covers only some of its runs; empty otherwise."""
    lever_runs = stats.lever.spawns if stats.lever is not None else stats.spawns
    if stats.key == "top-level" or lever_runs >= stats.spawns:
        return ""
    return f", on the {lever_runs} run{'s' if lever_runs != 1 else ''} its agent file sets"


def _tier_verdict(row: "ModelSwapTypeStats", pricing: Pricing) -> TierVerdict:
    if row.key in _NO_AGENT_FILE:
        # Every run a workflow script started: its transcript's kind says so,
        # whatever agent type the row is named for.
        setter = "a workflow script" if row.workflow_runs and row.workflow_runs >= row.spawns else "Claude Code"
        return TierVerdict("no_lever", None, 0.0, 0.0, f"{setter} sets its model, so there is no agent file to change")
    stats = row.lever if row.lever is not None else row
    if stats.spawns == 0 and row.spawns > 0:
        return TierVerdict(
            "set_elsewhere", None, 0.0, 0.0,
            f"no run followed {row.key}.md's model: {_set_elsewhere_text(row)}",
        )
    if stats.priced_turns == 0 or stats.observed_cost <= 0:
        return TierVerdict("no_data", None, 0.0, 0.0, "no priced turns")

    rank = workstyle.model_tier(stats.observed_model, stats.observed_model_alias)
    if rank == -1:
        return TierVerdict(
            "unknown_tier", None, 0.0, 0.0,
            "model family not recognised, so no cheaper tier can be named",
        )
    if rank == 0:
        return TierVerdict(
            "already_cheapest", None, 0.0, 0.0,
            f"{_ALREADY_CHEAPEST_LABEL} ({stats.observed_model})",
        )

    family = _TIER_FAMILIES[rank - 1]
    if stats.key == "top-level" and family in _MAIN_SESSION_BELOW_FLOOR:
        # The main session does the hard, open-ended work; it's never
        # moved below Sonnet, so a Sonnet main session has no cheaper tier.
        return TierVerdict("main_floor", None, 0.0, 0.0, _MAIN_FLOOR_LABEL)
    alt_model = pricing.aliases.get(family)
    if alt_model is None or alt_model not in stats.cost_by_model:
        return TierVerdict(
            "unknown_tier", None, 0.0, 0.0,
            f"no {family} rate in this pricing file, so no cheaper tier can be named",
        )

    alt_cost = stats.cost_by_model[alt_model]
    saving_usd = stats.observed_cost - alt_cost
    if saving_usd <= 0:
        return TierVerdict(
            "already_cheapest", None, 0.0, 0.0,
            f"{_ALREADY_CHEAPEST_LABEL} at today's volumes (already cheaper than {model_name(alt_model)})",
        )
    saving_pct = 100.0 * saving_usd / stats.observed_cost
    return TierVerdict(
        "cheaper_available", alt_model, saving_usd, saving_pct,
        f"{model_name(alt_model)} (saves ${saving_usd:,.2f}, {saving_pct:.1f}%, at today's volumes{_reach_suffix(row)})",
    )


# -- accumulation --------------------------------------------------------------


@dataclass(slots=True)
class ModelSwapTypeStats:
    """Rolled-up model-swap stats for one agent type (or
    ``"top-level"``) -- the values behind one row of
    ``build_section``'s ``model_swap_by_agent_type`` table."""

    key: str
    spawns: int = 0
    priced_turns: int = 0
    #: Priced turns whose observed model the rate card couldn't resolve
    #: -- priced at zero rather than silently vanishing from
    #: ``observed_cost`` (same convention as ``ttl.TtlTypeStats.unpriced_turns``
    #: / ``pricing.PricingCoverage``).
    unpriced_turns: int = 0
    #: Sum of every priced turn's own resolved-model cost (a mixed-model
    #: agent type is priced correctly, turn by turn).
    observed_cost: float = 0.0
    #: Turn.model -> turn count, for the dominant-model label and the
    #: tier lookup.
    model_turn_counts: dict[str, int] = field(default_factory=dict)
    #: TranscriptMeta.agent_model_alias -> transcript count (one vote per
    #: transcript, not per turn -- it's transcript-level metadata).
    alias_counts: dict[str, int] = field(default_factory=dict)
    #: canonical model id (every id in the loaded Pricing.models, not
    #: just the observed one(s)) -> this group's turns repriced flat at
    #: that model's rate, same token volumes and write split throughout.
    cost_by_model: dict[str, float] = field(default_factory=dict)
    #: Resolved once, in ``compute_model_swap`` (needs ``Pricing.aliases``
    #: -- see module docstring on why ``build_section`` doesn't take
    #: ``pricing`` and can't compute this itself).
    tier_verdict: TierVerdict = field(
        default_factory=lambda: TierVerdict("no_data", None, 0.0, 0.0, "no priced turns")
    )
    #: The same totals for only the runs this row's lever decides
    #: (:func:`model_set_by`): every run of the main session, and a
    #: subagent type's runs started without a model of their own. The
    #: tier verdict is worked out on these. ``None`` on the lever's own
    #: stats.
    lever: "ModelSwapTypeStats | None" = None
    #: Runs a workflow script started, and direct runs whose spawn named a
    #: model: the agent file's model line decides neither.
    workflow_runs: int = 0
    spawn_model_runs: int = 0

    @property
    def observed_model(self) -> str | None:
        return _dominant_label(self.model_turn_counts)

    @property
    def observed_model_alias(self) -> str | None:
        return _dominant_label(self.alias_counts)

    @property
    def distinct_models(self) -> int:
        return len(self.model_turn_counts)

    @property
    def observed_model_label(self) -> str:
        """The dominant model id, plus a "(+N more)" suffix when this
        group actually mixed models across its turns."""
        dominant = self.observed_model
        if dominant is None:
            return "unknown"
        extra = self.distinct_models - 1
        return dominant if extra <= 0 else f"{dominant} (+{extra} more)"


@dataclass(slots=True)
class ModelSwapStats:
    """The whole model-swap roll-up: one :class:`ModelSwapTypeStats` per
    agent type, plus the full set of alternative model ids every row was
    repriced against (every id the loaded :class:`~claudeglass.pricing.Pricing`
    carries, sorted for deterministic column order)."""

    by_key: dict[str, ModelSwapTypeStats] = field(default_factory=dict)
    alternative_models: tuple[str, ...] = ()


def compute_model_swap(
    results: Sequence[TranscriptResult],
    pricing: Pricing,
    thresholds: ModelSwapThresholds | None = None,
) -> ModelSwapStats:
    """Fold every transcript in ``results`` (top-level and subagent
    alike -- pass the whole corpus's transcripts, one entry per
    transcript, exactly the flat shape ``ttl.TtlStats.add`` consumes one
    at a time) into a per-agent-type :class:`ModelSwapTypeStats`, then
    resolve each row's one-tier-down :class:`TierVerdict`.

    ``thresholds`` isn't used by the arithmetic here (it only gates
    whether ``RULES["model-tier"]`` fires, downstream, off the rendered
    report) -- accepted for symmetry with every other ``compute_*``/
    ``*Stats`` constructor in this codebase and so a future threshold
    that *does* affect accumulation (e.g. a minimum-turn floor before a
    model is even considered as an alternative) has somewhere to land
    without changing this function's signature again.
    """
    del thresholds  # see docstring: accepted for signature symmetry only

    by_key: dict[str, ModelSwapTypeStats] = {}
    for result in results:
        key = _agent_type_label(result)
        stats = by_key.get(key)
        if stats is None:
            stats = by_key[key] = ModelSwapTypeStats(key=key, lever=ModelSwapTypeStats(key=key))
        setter = model_set_by(result)
        if setter == "workflow":
            stats.workflow_runs += 1
        elif setter == "spawn":
            stats.spawn_model_runs += 1
        # A run the lever decides counts on both, each in the same order,
        # so the row's own totals add up exactly as they would alone.
        targets = (stats, stats.lever) if setter in _LEVER_SETTERS else (stats,)

        alias = result.meta.agent_model_alias
        for target in targets:
            target.spawns += 1
            if alias:
                target.alias_counts[alias] = target.alias_counts.get(alias, 0) + 1

        for turn in _priced_turns(result):
            resolved = pricing.resolve_model(turn.model)
            observed_breakdown = price_turn(turn, resolved)
            alt_costs = [(alt_id, price_turn(turn, alt_rates).total) for alt_id, alt_rates in pricing.models.items()]
            for target in targets:
                target.priced_turns += 1
                if turn.model:
                    target.model_turn_counts[turn.model] = target.model_turn_counts.get(turn.model, 0) + 1
                target.observed_cost += observed_breakdown.total
                if not observed_breakdown.model_known:
                    target.unpriced_turns += 1
                for alt_id, alt_cost in alt_costs:
                    target.cost_by_model[alt_id] = target.cost_by_model.get(alt_id, 0.0) + alt_cost

    for stats in by_key.values():
        stats.tier_verdict = _tier_verdict(stats, pricing)

    return ModelSwapStats(by_key=by_key, alternative_models=tuple(sorted(pricing.models)))


# -- report section --------------------------------------------------------------


def _lever_label(key: str, workflow_only: bool = False) -> str:
    """Where this row's model is set. A built-in agent has no file to
    edit, so it takes a new same-named agent file; a workflow script sets
    the model for its unnamed agents (``workflow_only``: every run in the
    row was started by one, by the transcripts' kind), and Claude Code
    picks it for fork and untyped subagents."""
    from .recommend import _BUILTIN_AGENT_TYPES

    if key == "top-level":
        return "model (settings.json)"
    if workflow_only and key in _NO_AGENT_FILE:
        return "none (the workflow script sets it)"
    if key in _NO_AGENT_FILE:
        return "none (Claude Code picks)"
    if key in _BUILTIN_AGENT_TYPES:
        return f"model in a new {key}.md (overrides the built-in)"
    return f"model in {key}.md"


def build_section(
    stats: ModelSwapStats, thresholds: ModelSwapThresholds | None = None, units: "Units | None" = None
) -> Section:
    """Render a finished :class:`ModelSwapStats` as the report's
    ``model_swap`` section: ``model_swap_by_agent_type`` (one row per
    agent type) and ``model_swap_summary`` (the corpus-wide ceiling for
    moving every Fable/Opus subagent type one tier down). ``units``
    (UX-2) rephrases the "best cheaper alternative" label's saving for
    the report's billing mode -- ``compute_model_swap`` builds
    ``TierVerdict.label`` before a billing config is known, so it always
    carries a plain-USD fallback.
    """
    th = thresholds or _DEFAULT_THRESHOLDS

    columns = [
        Column(key="agent_type", label="Agent type", kind="str"),
        Column(key="spawns", label="Spawns", kind="int"),
        Column(key="priced_turns", label="Priced turns", kind="int"),
        Column(key="unpriced_turns", label="Unpriced turns (unknown model)", kind="int"),
        Column(key="observed_model", label="Observed model", kind="str"),
        Column(key="observed_cost", label="Observed cost", kind="money"),
    ]
    for alt_id in stats.alternative_models:
        columns.append(
            Column(
                key=f"cost_{alt_id}",
                label=f"Cost at {alt_id}",
                kind="money",
                help=f"The same tokens at {alt_id}'s list price.",
            )
        )
    columns.extend(
        [
            Column(key="best_cheaper_alternative_model", label="Best cheaper alternative (model id)", kind="str"),
            Column(key="best_cheaper_alternative", label="Best cheaper alternative", kind="str"),
            Column(key="saving_usd", label="Most you could save", kind="money"),
            Column(key="saving_pct", label="Ceiling saving (%, one tier down)", kind="pct"),
            Column(key="lever", label="Lever", kind="str"),
            Column(key="lever_runs", label="Runs the lever decides", kind="int"),
            Column(key="lever_priced_turns", label="Priced turns on those runs", kind="int"),
            Column(key="lever_model", label="Model on those runs", kind="str"),
            Column(key="lever_cost", label="Cost of those runs", kind="money"),
            Column(key="workflow_runs", label="Runs a workflow script started", kind="int"),
            Column(key="spawn_model_runs", label="Runs given a model when started", kind="int"),
        ]
    )

    rows: list[list] = []
    file_rows: list[list] = []
    any_unpriced = False
    for key in sorted(stats.by_key):
        row_stats = stats.by_key[key]
        lever_stats = row_stats.lever if row_stats.lever is not None else row_stats
        verdict = row_stats.tier_verdict
        lever = _lever_label(key, bool(row_stats.workflow_runs) and row_stats.workflow_runs >= row_stats.spawns)
        label = verdict.label
        if units is not None and verdict.state == "cheaper_available":
            saving_text = units.money_text(verdict.saving_usd)
            label = (
                f"{model_name(verdict.alt_model)} (saves {saving_text}, {verdict.saving_pct:.1f}%, at today's "
                f"volumes{_reach_suffix(row_stats)})"
            )
        row = [
            row_stats.key,
            row_stats.spawns,
            row_stats.priced_turns,
            row_stats.unpriced_turns,
            row_stats.observed_model_label,
            row_stats.observed_cost,
        ]
        for alt_id in stats.alternative_models:
            row.append(row_stats.cost_by_model.get(alt_id, 0.0))
        row.extend(
            [
                verdict.alt_model,
                label,
                verdict.saving_usd,
                verdict.saving_pct,
                lever,
                lever_stats.spawns,
                lever_stats.priced_turns,
                lever_stats.observed_model_label if lever_stats.spawns else None,
                lever_stats.observed_cost,
                row_stats.workflow_runs,
                row_stats.spawn_model_runs,
            ]
        )
        rows.append(row)
        if row_stats.unpriced_turns > 0:
            any_unpriced = True
        if key != "top-level" and key not in _NO_AGENT_FILE and lever_stats.spawns:
            file_rows.append(
                [
                    key,
                    lever_stats.spawns,
                    lever_stats.priced_turns,
                    lever_stats.observed_model_label,
                    lever_stats.observed_cost,
                    *(lever_stats.cost_by_model.get(alt_id, 0.0) for alt_id in stats.alternative_models),
                ]
            )

    table = Table(
        name="model_swap_by_agent_type",
        title="Model-swap counterfactual by agent type",
        columns=columns,
        rows=rows,
    )

    # The runs each agent file's model line decides, repriced at every
    # model: what a what-if for that line prices, whatever model it names.
    file_table = Table(
        name="model_swap_agent_file_runs",
        title="Runs each agent file's model decides, at other models",
        columns=[
            Column(key="agent_type", label="Agent type", kind="str"),
            Column(key="runs", label="Runs", kind="int"),
            Column(key="priced_turns", label="Priced turns", kind="int"),
            Column(key="observed_model", label="Observed model", kind="str"),
            Column(key="observed_cost", label="Observed cost", kind="money"),
            *(
                Column(
                    key=f"cost_{alt_id}",
                    label=f"Cost at {alt_id}",
                    kind="money",
                    help=f"The same tokens at {alt_id}'s list price.",
                )
                for alt_id in stats.alternative_models
            ),
        ],
        rows=file_rows,
    )

    # -- corpus-wide summary: every subagent type currently on Fable/Opus,
    # moved one tier down, on the runs its agent file decides.
    def _lever_of(s: ModelSwapTypeStats) -> ModelSwapTypeStats:
        return s.lever if s.lever is not None else s

    qualifying = [
        s
        for key, s in stats.by_key.items()
        if key != "top-level"
        and workstyle.model_tier(_lever_of(s).observed_model, _lever_of(s).observed_model_alias) in (2, 3)
        and s.tier_verdict.state == "cheaper_available"
    ]
    total_observed = sum(_lever_of(s).observed_cost for s in qualifying)
    total_saving = sum(s.tier_verdict.saving_usd for s in qualifying)
    total_after = total_observed - total_saving
    total_saving_pct = 100.0 * total_saving / total_observed if total_observed > 0 else 0.0

    summary_table = Table(
        name="model_swap_summary",
        title="Corpus-wide ceiling: every Fable/Opus subagent type moved one tier down",
        columns=[
            Column(key="scope", label="Scope", kind="str"),
            Column(key="agent_types", label="Agent types (Fable/Opus, cheaper tier available)", kind="int"),
            Column(key="observed_cost_usd", label="Observed cost", kind="money"),
            Column(key="cost_after_tier_down_usd", label="Cost after one-tier-down swap", kind="money"),
            Column(key="saving_usd", label="Ceiling saving", kind="money"),
            Column(key="saving_pct", label="Ceiling saving (%)", kind="pct"),
        ],
        rows=[
            [
                "subagent types currently on Fable/Opus",
                len(qualifying),
                total_observed,
                total_after,
                total_saving,
                total_saving_pct,
            ]
        ],
        notes=[
            "This figure is for subagents only, so it leaves out the main session. It also leaves "
            "out any Fable or Opus agent type that already costs no more than the next tier down "
            "at today's volumes.",
            "It counts only the runs each agent file's model line decides. Runs a workflow script "
            "started, or that were given a model when they started, are left out.",
        ],
    )

    notes = list(ASSUMPTIONS)
    if any_unpriced:
        notes.append(
            "At least one agent type has replies whose model has no price in your pricing file "
            "(see its \"Unpriced turns\" column). Those replies count as zero in the observed cost, "
            "but are still priced at every alternative model. So that agent type's saving is "
            "understated."
        )
    notes.append(f"Thresholds: {' '.join(th.describe())}")

    return Section(
        key="model_swap",
        title="Model-swap counterfactual",
        tables=[table, summary_table, file_table],
        notes=notes,
    )


# -- report-lookup helpers --------------------------------------------------
#
# Duplicated from recommend.py (see module docstring's deviation note):
# recommend.py is off-limits to this work order, so these small,
# private, already-stable helpers are copied rather than imported.


def _section(report: ReportModel, key: str) -> Section | None:
    for section in report.sections:
        if section.key == key:
            return section
    return None


def _table(report: ReportModel, section_key: str, table_name: str) -> Table | None:
    section = _section(report, section_key)
    if section is None:
        return None
    for table in section.tables:
        if table.name == table_name:
            return table
    return None


def _col_index(table: Table, column_key: str) -> int | None:
    for idx, column in enumerate(table.columns):
        if column.key == column_key:
            return idx
    return None


def _row(table: Table, row_key) -> list | None:
    for row in table.rows:
        if row and row[0] == row_key:
            return row
    return None


def _cell(report: ReportModel, section_key: str, table_name: str, row_key, column_key: str):
    table = _table(report, section_key, table_name)
    if table is None:
        return None
    row = _row(table, row_key)
    if row is None:
        return None
    idx = _col_index(table, column_key)
    if idx is None or idx >= len(row):
        return None
    return row[idx]


def _evidence(label: str, value, section_key: str, table_name: str, row_key) -> tuple:
    return (label, value, f"{section_key}.{table_name}", row_key)


def _row_meets_min_sample(th: ModelSwapThresholds, spawns: int | None, priced_turns: int | None) -> bool:
    """Per-row analogue of ``recommend._row_meets_min_sample`` -- a row
    with neither column populated (an older/hand-built table shape)
    can't be evaluated and is treated as passing, same as the original."""
    if spawns is None and priced_turns is None:
        return True
    if spawns is not None and spawns >= th.min_sessions:
        return True
    if priced_turns is not None and priced_turns >= th.min_turns:
        return True
    return False


def _scope_for(agent_type: str, snapshot: Snapshot | None) -> str:
    """"user" for the top-level row's settings.json lever, "repo" for a
    subagent's ``.claude/agents/<type>.md`` frontmatter lever, upgraded
    to "managed" when "model" is a managed-settings key (see module
    docstring's scope deviation note)."""
    if snapshot is not None and "model" in set(managed_keys(snapshot)):
        return "managed"
    return "user" if agent_type == "top-level" else "repo"


def _action_with_scope(action: str, scope: str) -> str:
    if scope == "managed":
        return f"{action} This lever is managed by policy, raise with your administrator."
    return action


# -- rules --------------------------------------------------------------------


def _rule_model_tier(
    report: ReportModel,
    th: ModelSwapThresholds,
    archetype: str | None = None,
    snapshot: Snapshot | None = None,
) -> list[Recommendation]:
    """The ``model-tier`` rule: fires per agent type when the one-tier-
    down saving (already computed by ``build_section``, read back from
    the rendered table -- this module's rules never read raw stats,
    matching ``recommend.py``'s own "rules work only from the rendered
    report" convention) exceeds both of ``th``'s floors and the row's own
    sample clears ``th.min_sessions``/``th.min_turns``.

    Suppressed for a non-top-level row when ``archetype`` is one that
    never spawns subagents of its own (``_NO_SUBAGENT_ARCHETYPES`` --
    "never tell a chat-only user about subagent models"); the top-level
    row is never suppressed, since the main session's own model is a
    real lever regardless of archetype (same asymmetry as
    ``recommend._rule_ttl_switch``).
    """
    table = _table(report, "model_swap", "model_swap_by_agent_type")
    if table is None:
        return []

    agent_idx = _col_index(table, "agent_type")
    # The sample and the model are the lever's own runs' (a table built
    # before those columns existed falls back to the whole row's).
    spawns_idx = _col_index(table, "lever_runs")
    if spawns_idx is None:
        spawns_idx = _col_index(table, "spawns")
    priced_turns_idx = _col_index(table, "lever_priced_turns")
    if priced_turns_idx is None:
        priced_turns_idx = _col_index(table, "priced_turns")
    observed_cost_idx = _col_index(table, "lever_cost")
    if observed_cost_idx is None:
        observed_cost_idx = _col_index(table, "observed_cost")
    observed_model_idx = _col_index(table, "lever_model")
    if observed_model_idx is None:
        observed_model_idx = _col_index(table, "observed_model")
    workflow_idx = _col_index(table, "workflow_runs")
    spawn_model_idx = _col_index(table, "spawn_model_runs")
    alt_model_idx = _col_index(table, "best_cheaper_alternative_model")
    alt_label_idx = _col_index(table, "best_cheaper_alternative")
    saving_usd_idx = _col_index(table, "saving_usd")
    saving_pct_idx = _col_index(table, "saving_pct")
    if agent_idx is None or alt_model_idx is None or saving_usd_idx is None or saving_pct_idx is None:
        return []

    out: list[Recommendation] = []
    for row in table.rows:
        agent_type = row[agent_idx]
        if agent_type != "top-level" and archetype in _NO_SUBAGENT_ARCHETYPES:
            continue

        spawns = row[spawns_idx] if spawns_idx is not None and spawns_idx < len(row) else None
        priced_turns = row[priced_turns_idx] if priced_turns_idx is not None and priced_turns_idx < len(row) else None
        if not _row_meets_min_sample(th, spawns, priced_turns):
            continue

        alt_model = row[alt_model_idx]
        if not alt_model:
            # "already on the cheapest model" / "unknown tier" / "no
            # priced turns" -- every one of these carries saving 0.0 (see
            # TierVerdict), but the explicit None-model check is the
            # unambiguous gate, not the numeric one.
            continue

        saving_usd = row[saving_usd_idx] or 0.0
        saving_pct = row[saving_pct_idx] or 0.0
        if saving_usd <= th.saving_usd_min or saving_pct <= th.saving_pct_min:
            continue

        observed_cost = row[observed_cost_idx] if observed_cost_idx is not None else None
        observed_model = row[observed_model_idx] if observed_model_idx is not None else "the observed model"
        alt_label = row[alt_label_idx] if alt_label_idx is not None else alt_model

        scope = _scope_for(agent_type, snapshot)
        workflow_runs = _count(row, workflow_idx)
        spawn_model_runs = _count(row, spawn_model_idx)
        if agent_type == "top-level":
            frontmatter_note = f'Set "model": "{alt_model}" in settings.json'
            reach = ""
        else:
            frontmatter_note = f"Set `model: {alt_model}` in .claude/agents/{agent_type}.md's frontmatter"
            reach = f" on the {spawns} run{'s' if spawns != 1 else ''} started without a model" if (
                workflow_runs or spawn_model_runs
            ) and isinstance(spawns, int) else ""
        # UX-2: units may be unset (a caller without a billing config) --
        # money_text still gives a plain currency-suffixed number rather
        # than a bare "$" in that case.
        units = report.units
        saving_text = units.money_text(saving_usd) if units is not None else f"${saving_usd:,.2f}"
        action = _action_with_scope(
            f"{frontmatter_note} (currently effectively {observed_model}{reach}). "
            f"Ceiling saving at today's volumes: {saving_text} ({saving_pct:.1f}%) -- token "
            "volumes and turn counts are held constant, so a smaller model may need more turns "
            "or fail tasks outright; verify quality before committing."
            + _set_elsewhere_action(agent_type, workflow_runs, spawn_model_runs),
            scope,
        )

        out.append(
            Recommendation(
                id="model-tier",
                severity="advice",
                category="settings",
                archetypes=_ALL_ARCHETYPES,
                title=f"{agent_type} could run a cheaper model tier",
                action=action,
                lever="model",
                scope=scope,
                agent_type=agent_type,
                evidence=[
                    _evidence("Observed cost", observed_cost, "model_swap", "model_swap_by_agent_type", agent_type),
                    _evidence("Best cheaper alternative", alt_label, "model_swap", "model_swap_by_agent_type", agent_type),
                    _evidence("Ceiling saving (USD)", saving_usd, "model_swap", "model_swap_by_agent_type", agent_type),
                    _evidence("Ceiling saving (%)", saving_pct, "model_swap", "model_swap_by_agent_type", agent_type),
                    *(
                        _evidence(label, count, "model_swap", "model_swap_by_agent_type", agent_type)
                        for label, count in (
                            ("Runs a workflow script started (not counted)", workflow_runs),
                            ("Runs given a model when started (not counted)", spawn_model_runs),
                        )
                        if count
                    ),
                    *_measured_fit_evidence(report, agent_type),
                ],
            )
        )
    return out


def _count(row: list, idx: int | None) -> int:
    value = row[idx] if idx is not None and idx < len(row) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def set_elsewhere_sentence(workflow_runs: int, spawn_model_runs: int) -> str:
    """Where the model of an agent type's other runs is set, as one
    sentence with a leading space, or ``""`` when the agent file decides
    every run: a workflow script's ``agent()`` call, and the model a run
    was given when it started, which comes from whatever prompt, skill or
    command asked for it."""
    parts = []
    if workflow_runs:
        parts.append(
            f"{workflow_runs} run{'s' if workflow_runs != 1 else ''} a workflow script started "
            "(set the model in the script's agent() call)"
        )
    if spawn_model_runs:
        parts.append(
            f"{spawn_model_runs} run{'s' if spawn_model_runs != 1 else ''} given a model when "
            f"{'it' if spawn_model_runs == 1 else 'they'} started (set by the prompt, skill or command that asks "
            "for that model)"
        )
    if not parts:
        return ""
    return f" The agent file doesn't decide the model for {' or '.join(parts)}, so they aren't counted."


def _set_elsewhere_action(agent_type: str, workflow_runs: int, spawn_model_runs: int) -> str:
    if agent_type == "top-level":
        return ""
    return set_elsewhere_sentence(workflow_runs, spawn_model_runs)


def _measured_fit_evidence(report: ReportModel, agent_type: str) -> list[tuple]:
    """Evidence for one agent type from the Work habits section's
    ``habits_agents`` table: the share of its work Claude reported easy
    (metrics capture), and what its runs did, counted from the transcripts:
    the share of its calls that were one read-only look, and how many
    calls came before its first edit. Each is left out when it is empty,
    but not a 0 of the last: runs whose first call was an edit are the
    clearest sign a smaller model fits."""
    table = _table(report, "habits", "habits_agents")
    row = _row(table, agent_type) if table is not None else None
    if row is None:
        return []
    out = []
    for key, label in (
        ("easy_pct", "Work reported easy (%)"),
        ("probe_pct", "Calls that were a single read-only look (%)"),
        ("before_edit", "Calls before the first edit"),
    ):
        idx = _col_index(table, key)
        value = row[idx] if idx is not None and idx < len(row) else None
        if isinstance(value, (int, float)) and not isinstance(value, bool) and (
            value > 0 or (key == "before_edit" and value == 0)
        ):
            out.append(_evidence(label, value, "habits", "habits_agents", agent_type))
    return out


#: One rule, keyed by id -- a wiring agent folds this into recommend.py's
#: own rule list via ``RULES["model-tier"](report, th, archetype,
#: snapshot)`` (see module docstring).
RULES: dict[str, Callable[..., list[Recommendation]]] = {
    "model-tier": _rule_model_tier,
}


__all__ = [
    "ASSUMPTIONS",
    "ModelSwapThresholds",
    "ModelSwapTypeStats",
    "ModelSwapStats",
    "TierVerdict",
    "RULES",
    "compute_model_swap",
    "model_set_by",
    "set_elsewhere_sentence",
    "build_section",
]

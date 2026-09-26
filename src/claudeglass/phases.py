"""Phase classification and cost split (WP12a): how much of a session's
cost went to DISCOVERY (reading/searching), IMPLEMENTATION (editing real
files or running an ordinary shell command), VERIFICATION (running a
test/build tool, or editing a scratch file) versus OTHER (a turn using no
tools at all, or one that doesn't fit any of the above).

Classification rule (:func:`classify_turn_phase`), first match wins, in
this order:

1. **OTHER** if the turn used no tools at all (nothing to classify).
2. **DISCOVERY** if every tool the turn used is one of Read/Grep/Glob/
   WebFetch/WebSearch/ListAgents, and it made no edit (``edit_kind`` is
   ``None``).
3. **IMPLEMENTATION** if ``edit_kind == "real"``, or the turn ran a
   Bash/PowerShell command whose prefix does *not* match one of the
   test/build tool prefixes below (an ordinary shell command in service
   of writing code).
4. **VERIFICATION** if the turn's ``cmd_prefix`` starts with one of the
   recognised test/build tool prefixes (``pytest``, ``python -m pytest``,
   ``dotnet test``, ``dotnet build``, ``npm test``, ``npx vitest``,
   ``npx playwright``, ``go test``, ``cargo test``, ``make``, ``mvn``,
   ``gradle``), or ``edit_kind == "scratch"`` (a throwaway/temp-dir edit,
   the shape of a debug script rather than production code).
5. **OTHER** otherwise (a fallback beyond the "no tools" case above --
   e.g. a turn that only used a subagent-spawning tool).

Deviation, documented rather than made silently: the brief lists these
four buckets in the order DISCOVERY, IMPLEMENTATION, VERIFICATION, OTHER,
which this module follows literally as the first-match-wins precedence
(matching the project's convention elsewhere -- ``classify.classify_purpose``,
``workstyle.detect_archetype`` -- of a textually-ordered rule list). The
one place this matters in practice: a turn that both makes a "real" edit
*and* runs a test/build command in the same assistant turn (both tool
calls in one turn) lands in IMPLEMENTATION, not VERIFICATION, because
DISCOVERY/IMPLEMENTATION are checked first. This is rare in the observed
corpus (a turn usually either edits or verifies, not both at once) and is
called out here rather than silently picked.

``classify.py``'s own ``_TEST_TOOL_PREFIXES`` is a narrower subset of the
prefix list used here (it's missing ``python -m pytest``, ``dotnet
build``, ``npx playwright``, ``make``, ``mvn``, ``gradle``) -- this module
defines its own, fuller list rather than importing that narrower one, so
a future change to ``classify.py``'s purpose-detection needs can't
silently narrow phase VERIFICATION detection too.

:class:`PhaseStats` is an accumulator, fed one transcript at a time via
:meth:`PhaseStats.add_transcript`, mirroring ``compaction.CompactionStats``/
``topology.TopologyStats``'s add-then-summarise shape -- the same object
serves a single-session report or a whole-corpus one. :func:`build_section`
turns a finished accumulator into a ``Section`` (key ``"phases"``).

Per plan Appendix A5, a DISCOVERY cost share above
:data:`DISCOVERY_SHARE_THRESHOLD` (35%) is what triggers the
``discovery-share`` recommendation (category ``workflow``) -- the
recommendation itself is WP10's (report assembly) to emit; this module
only computes the share and exposes :func:`discovery_share_exceeds_threshold`
so that WP10 code has one place to call rather than re-deriving the
threshold check.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from .model import Column, Section, Table, TranscriptResult, Turn, agent_type_label
from .pricing import Pricing, price_turn

PHASE_DISCOVERY = "discovery"
PHASE_IMPLEMENTATION = "implementation"
PHASE_VERIFICATION = "verification"
PHASE_OTHER = "other"

#: Every phase bucket, in the brief's own order -- also the row order
#: ``build_section`` renders the summary table in.
PHASES: tuple[str, ...] = (PHASE_DISCOVERY, PHASE_IMPLEMENTATION, PHASE_VERIFICATION, PHASE_OTHER)

#: A DISCOVERY-only turn may use any of these tools and nothing else.
_DISCOVERY_TOOLS = frozenset({"Read", "Grep", "Glob", "WebFetch", "WebSearch", "ListAgents"})

_SHELL_TOOL_NAMES = frozenset({"Bash", "PowerShell"})

#: Recognised test/build tool command prefixes -- see module docstring's
#: note on why this is a fuller list than ``classify.py``'s own.
_VERIFICATION_CMD_PREFIXES: tuple[str, ...] = (
    "pytest",
    "python -m pytest",
    "dotnet test",
    "dotnet build",
    "npm test",
    "npx vitest",
    "npx playwright",
    "go test",
    "cargo test",
    "make",
    "mvn",
    "gradle",
)

#: Plan Appendix A5: DISCOVERY cost share above this triggers the
#: "discovery-share" recommendation.
DISCOVERY_SHARE_THRESHOLD = 0.35


def _matches_verification_cmd(cmd_prefix: str | None) -> bool:
    if not cmd_prefix:
        return False
    return cmd_prefix.lstrip().startswith(_VERIFICATION_CMD_PREFIXES)


def classify_turn_phase(turn: Turn) -> str:
    """Classify one priced ``Turn`` into a phase bucket. See module
    docstring for the full first-match-wins rule."""
    if not turn.tool_names:
        return PHASE_OTHER

    if set(turn.tool_names) <= _DISCOVERY_TOOLS and turn.edit_kind is None:
        return PHASE_DISCOVERY

    has_shell = any(name in _SHELL_TOOL_NAMES for name in turn.tool_names)
    if turn.edit_kind == "real" or (has_shell and not _matches_verification_cmd(turn.cmd_prefix)):
        return PHASE_IMPLEMENTATION

    if _matches_verification_cmd(turn.cmd_prefix) or turn.edit_kind == "scratch":
        return PHASE_VERIFICATION

    return PHASE_OTHER


def _priced_turns(result: TranscriptResult) -> list[Turn]:
    """Turns that actually got a ``turn_index`` (excludes synthetic and
    missing-usage turns, but keeps an estimated compaction call, which is
    spend -- see ``parse.py``). Deliberately duplicated rather than imported --
    see ``workflows.py``'s docstring note on this same one-line helper
    appearing in several WP8+ modules.
    """
    return [t for t in result.turns if t.turn_index > 0]


@dataclass(slots=True)
class _Bucket:
    """Accumulated metrics for one (phase[, dimension]) cell."""

    turns: int = 0
    new_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0

    def add(self, turn: Turn, cost: float) -> None:
        self.turns += 1
        self.new_tokens += turn.input_tokens + turn.cache_creation_tokens
        self.cache_read_tokens += turn.cache_read_tokens
        self.output_tokens += turn.output_tokens
        self.cost += cost


@dataclass(slots=True)
class PhaseStats:
    """Accumulator: per-phase totals, plus the same totals broken down by
    transcript kind (``TranscriptMeta.kind``: "top-level"/"subagent"/
    "workflow-agent") and by agent type (``TranscriptMeta.agent_type``,
    "unknown" when unset) -- fed one transcript at a time via
    :meth:`add_transcript`.
    """

    overall: dict[str, _Bucket] = field(default_factory=lambda: {p: _Bucket() for p in PHASES})
    by_kind: dict[tuple[str, str], _Bucket] = field(default_factory=dict)
    by_agent_type: dict[tuple[str, str], _Bucket] = field(default_factory=dict)

    def add_transcript(self, result: TranscriptResult, rates_lookup: Pricing) -> None:
        """Classify and cost every priced turn in ``result``, folding it
        into ``overall``, ``by_kind`` (keyed by ``(phase, transcript
        kind)``) and ``by_agent_type`` (keyed by ``(phase, agent type)``).
        """
        kind = result.meta.kind or "top-level"
        agent_type = agent_type_label(result)
        for turn in _priced_turns(result):
            phase = classify_turn_phase(turn)
            resolved = rates_lookup.resolve_model(turn.model)
            cost = price_turn(turn, resolved).total

            self.overall[phase].add(turn, cost)
            self.by_kind.setdefault((phase, kind), _Bucket()).add(turn, cost)
            self.by_agent_type.setdefault((phase, agent_type), _Bucket()).add(turn, cost)

    @property
    def total_cost(self) -> float:
        return sum(bucket.cost for bucket in self.overall.values())

    @property
    def total_turns(self) -> int:
        return sum(bucket.turns for bucket in self.overall.values())

    def cost_share(self, phase: str) -> float | None:
        """This phase's share of total cost, ``None`` when no cost has
        been priced at all yet (nothing to divide by) -- deliberately
        not ``0.0``, which would misleadingly read as "this phase cost
        nothing" rather than "nothing is priced yet" (matches
        ``compaction.CompactionStats.dropped_share_of_cache_creation``'s
        documented convention)."""
        total = self.total_cost
        if not total:
            return None
        return self.overall[phase].cost / total


def discovery_share_exceeds_threshold(stats: PhaseStats) -> bool:
    """Whether DISCOVERY's cost share exceeds :data:`DISCOVERY_SHARE_THRESHOLD`
    -- the plan Appendix A5 condition for the ``discovery-share``
    recommendation (emitted by WP10's report-assembly code, not here).
    ``False`` when nothing has been priced yet."""
    share = stats.cost_share(PHASE_DISCOVERY)
    return share is not None and share > DISCOVERY_SHARE_THRESHOLD


def _sorted_dim_values(pairs: Sequence[tuple[str, str]]) -> list[str]:
    seen: dict[str, None] = {}
    for _phase, dim in pairs:
        seen.setdefault(dim, None)
    return sorted(seen)


def _build_summary_table(stats: PhaseStats) -> Table:
    rows = []
    for phase in PHASES:
        bucket = stats.overall[phase]
        share = stats.cost_share(phase)
        rows.append(
            [
                phase,
                bucket.turns,
                bucket.new_tokens,
                bucket.cache_read_tokens,
                bucket.output_tokens,
                bucket.cost,
                100.0 * share if share is not None else None,
            ]
        )
    return Table(
        name="phases_summary",
        title="Cost by phase",
        columns=[
            Column(key="phase", label="Phase", kind="str"),
            Column(key="turns", label="Turns", kind="int"),
            Column(key="new_tokens", label="New tokens (input and cache writes)", kind="tokens"),
            Column(key="cache_read_tokens", label="Cache read tokens", kind="tokens"),
            Column(key="output_tokens", label="Output tokens", kind="tokens"),
            Column(key="cost", label="Cost", kind="money"),
            Column(key="cost_share_pct", label="Share of cost", kind="pct"),
        ],
        rows=rows,
    )


def _build_dimension_table(name: str, title: str, dim_label: str, cells: dict[tuple[str, str], _Bucket]) -> Table:
    dim_values = _sorted_dim_values(list(cells.keys()))
    rows = []
    for dim_value in dim_values:
        for phase in PHASES:
            bucket = cells.get((phase, dim_value))
            if bucket is None:
                continue
            rows.append([dim_value, phase, bucket.turns, bucket.new_tokens, bucket.cache_read_tokens, bucket.output_tokens, bucket.cost])
    return Table(
        name=name,
        title=title,
        columns=[
            Column(key="dimension", label=dim_label, kind="str"),
            Column(key="phase", label="Phase", kind="str"),
            Column(key="turns", label="Turns", kind="int"),
            Column(key="new_tokens", label="New tokens (input and cache writes)", kind="tokens"),
            Column(key="cache_read_tokens", label="Cache read tokens", kind="tokens"),
            Column(key="output_tokens", label="Output tokens", kind="tokens"),
            Column(key="cost", label="Cost", kind="money"),
        ],
        rows=rows,
    )


def build_section(stats: PhaseStats) -> Section:
    """The "Phases" report section (key ``"phases"``): a phase-summary
    table, a phase-by-transcript-kind table, and a phase-by-agent-type
    table, with notes stating the classification rule and the
    discovery-share recommendation threshold.
    """
    summary_table = _build_summary_table(stats)
    by_kind_table = _build_dimension_table(
        "phases_by_transcript_kind", "Cost by phase and transcript kind", "Transcript kind", stats.by_kind
    )
    by_agent_type_table = _build_dimension_table(
        "phases_by_agent_type", "Cost by phase and agent type", "Agent type", stats.by_agent_type
    )

    # Named as the tables label the phases (helptext's value_labels), since
    # the dashboard and the Markdown/HTML reports show these notes too.
    notes = [
        "Other: the reply called no tools. It is also where a reply lands when nothing below fits.",
        "Exploring: every tool the reply called is one of Read, Grep, Glob, WebFetch, WebSearch or "
        "ListAgents, and it made no edit.",
        "Building: the reply edited a file outside a temp folder, or ran a shell command that isn't a known "
        "test or build tool.",
        "Checking: the reply ran a known test or build tool (pytest, dotnet test or build, npm test, npx "
        "vitest or playwright, go test, cargo test, make, mvn, gradle), or edited a scratch file.",
        f"When exploring is more than {DISCOVERY_SHARE_THRESHOLD:.0%} of cost, a recommendation suggests "
        "writing down what keeps being looked up.",
    ]

    return Section(
        key="phases",
        title="Phases",
        tables=[summary_table, by_kind_table, by_agent_type_table],
        notes=notes,
    )


__all__ = [
    "PHASE_DISCOVERY",
    "PHASE_IMPLEMENTATION",
    "PHASE_VERIFICATION",
    "PHASE_OTHER",
    "PHASES",
    "DISCOVERY_SHARE_THRESHOLD",
    "classify_turn_phase",
    "PhaseStats",
    "discovery_share_exceeds_threshold",
    "build_section",
]

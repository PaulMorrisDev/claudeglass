"""Split long subagent runs (``run_split``): what each long run would have
cost had it been done as several shorter runs, each started fresh from a
short note of what the last one did, and how long a run should get before
splitting pays.

Everything a run reads stays in its context and is read again on every
later reply, so a run's cost grows faster than its length. The model, per
subagent run (workflow agents are left out: their script decides how work
is split), for each interval in ``intervals``:

- **Where it splits.** Every that many replies, counted from the run's
  start or its last conversation summary. A split counts only when it
  would drop at least ``min_dropped_tokens``.
- **What a fresh run starts with.** The run's own first context (its system
  prompt, tools and task) plus the note (``note_tokens``).
- **What it drops.** The context just before the split, less that fresh
  start. Every reply until the next split that counts, the next summary or
  the end is priced with it taken out of its context
  (``compaction_sim._shrunk_cost``, the shrink the compaction replay uses).
- **What each split adds back.** The run writes the note (output at the
  run's rates); the parent writes it into the next run's brief (output at
  the parent's rates) and keeps both in its own context for the rest of
  its transcript (``carry.py``'s rates); the new run's first reply writes
  its fresh start to the cache instead of reading it
  (``handoff._fresh_start_cost``); and it re-reads some files the last run
  had read (compaction_sim's rediscovery allowance, measured from this
  corpus's real summaries). When the parent transcript can't be found, its
  part is left out.

**Current settings only.** The auto-compact window now in force caps how
long a run's context can grow: a run that peaked above it ran under an
older, larger setting (or none), and its length says nothing about what a
run costs now. Such runs are left out of every figure here (their count
and cost are reported apart), so a limit the setting has since removed
never earns a tip. A run's peak context is the largest context of any of
its replies; the window comes from the latest config snapshot of the
run's project (``build_report`` passes it). With no window known, every
run counts.

Each agent type's **best interval** is the one that saves most, among
those with at least ``min_runs`` runs long enough to split. Short
intervals split often and pay the add-backs often; long ones split only
the longest runs. The saving counts every split, including those near a
run's end that cost more than they save, since a run can't know in advance
how long it will be. An upper bound all the same: a thin note can send the
next run back to work the last one had done.

Same shape as ``handoff.py``: :class:`RunSplitThresholds`,
:func:`compute_run_split`, :func:`build_section` and :data:`RULES` (folded
into ``recommend.recommend``).
"""

from __future__ import annotations

import statistics
from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Callable, Union

from .carry import boundary_turn_indices, carry_end_index, carry_rate_prefix
from .compaction_sim import _shrunk_cost
from .handoff import _fresh_start_cost
from .model import Column, EventKind, Recommendation, ReportModel, Section, Table, TranscriptResult, Turn
from .pricing import Pricing, price_turn
from .topology import agent_key

ASSUMPTIONS: tuple[str, ...] = (
    "a run split every so many replies starts again from its own first context plus a short note of what "
    "the last part did",
    "each split adds back the note written twice (by the run, then into the next brief), the parent "
    "keeping both, one cache write of the fresh start and one file re-read allowance",
    "the run-split saving overlaps with the auto-compact saving: both come from carrying less context in "
    "later replies",
    "a run whose context peaked above the auto-compact window now in force for its project ran under an "
    "older setting and is left out, so only runs the current setting allows are counted",
)

#: The split intervals tried, in replies.
DEFAULT_INTERVALS: tuple[int, ...] = (50, 75, 100, 150, 200, 300)


@dataclass(slots=True)
class RunSplitThresholds:
    """Every tunable number the run-split check depends on (same
    ``from_config``/``describe`` convention as ``carry.CarryThresholds``).
    Config keys carry a ``run_split_`` prefix: ``[thresholds]`` is one flat
    table shared by every module."""

    #: The split intervals tried, in replies (``run_split_intervals``, a
    #: list).
    intervals: tuple[int, ...] = DEFAULT_INTERVALS
    #: The handoff note each split writes, in tokens.
    note_tokens: int = 2_000
    #: A split counts only when it drops at least this much context.
    min_dropped_tokens: float = 20_000.0
    #: An interval counts for an agent type only with at least this many
    #: of its runs long enough to split...
    min_runs: int = 3
    #: ...and the tip needs a saving of at least this share (%) of that
    #: agent type's cost.
    min_saving_share_pct: float = 5.0
    #: How many agent types ``run_split_by_agent`` lists.
    top_n: int = 20

    @classmethod
    def from_config(cls, config: dict | None) -> "RunSplitThresholds":
        data = config or {}
        if not isinstance(data, dict):
            data = {}
        nested = data.get("thresholds")
        if isinstance(nested, dict):
            data = nested
        kwargs: dict = {}
        for name, cast in (
            ("note_tokens", int),
            ("min_dropped_tokens", float),
            ("min_runs", int),
            ("min_saving_share_pct", float),
            ("top_n", int),
        ):
            key = f"run_split_{name}"
            if key in data:
                try:
                    kwargs[name] = cast(data[key])
                except (TypeError, ValueError):
                    pass
        intervals = data.get("run_split_intervals")
        if isinstance(intervals, (list, tuple)):
            chosen = sorted({int(n) for n in intervals if isinstance(n, int) and not isinstance(n, bool) and n > 1})
            if chosen:
                kwargs["intervals"] = tuple(chosen)
        return cls(**kwargs)

    def describe(self) -> list[str]:
        every = ", ".join(str(n) for n in self.intervals)
        return [
            f"Runs are split every {every} replies in turn, with a {self.note_tokens:,}-token note.",
            f"A split counts when it drops at least {self.min_dropped_tokens:,.0f} tokens of context.",
            f"An interval counts for an agent type with at least {self.min_runs} runs long enough to split. "
            f"The split-runs tip needs a saving of at least {self.min_saving_share_pct:.1f}% of its cost.",
            f"The agent table lists the top {self.top_n}.",
        ]


_DEFAULT_THRESHOLDS = RunSplitThresholds()


@dataclass(slots=True)
class SplitAt:
    """One agent type's runs split every ``every_n`` replies."""

    every_n: int
    long_runs: int = 0
    replies: list[int] = field(default_factory=list)
    splits: int = 0
    dropped: list[int] = field(default_factory=list)
    long_run_usd: float = 0.0
    #: Less what splitting adds back, so it can be below zero.
    saving_usd: float = 0.0


@dataclass(slots=True)
class AgentRunSplit:
    """One agent type: counts and costs only."""

    agent_type: str
    runs: int = 0
    longest_run: int = 0
    agent_usd: float = 0.0
    by_interval: dict[int, SplitAt] = field(default_factory=dict)

    def at(self, every_n: int) -> SplitAt:
        found = self.by_interval.get(every_n)
        if found is None:
            found = self.by_interval[every_n] = SplitAt(every_n=every_n)
        return found

    def best(self, min_runs: int) -> SplitAt | None:
        """The interval that saves most, among those with at least
        ``min_runs`` runs long enough to split; ``None`` when none saves
        anything."""
        pays = [s for s in self.by_interval.values() if s.long_runs >= min_runs and s.saving_usd > 0]
        return max(pays, key=lambda s: (s.saving_usd, -s.every_n)) if pays else None


@dataclass(slots=True)
class RunSplitStats:
    agents: dict[str, AgentRunSplit] = field(default_factory=dict)
    intervals: tuple[int, ...] = DEFAULT_INTERVALS
    min_runs: int = 3
    rediscovery_allowance_usd: float = 0.0
    #: Runs long enough to split whose parent transcript wasn't found.
    no_parent_runs: int = 0
    #: Runs left out because their peak context was above the window now in
    #: force (they ran under an older setting), their cost, and the windows
    #: that decided it.
    older_runs: int = 0
    older_usd: float = 0.0
    windows: set[int] = field(default_factory=set)

    def agent(self, agent_type: str) -> AgentRunSplit:
        found = self.agents.get(agent_type)
        if found is None:
            found = self.agents[agent_type] = AgentRunSplit(agent_type=agent_type)
        return found

    def ranked(self) -> list[AgentRunSplit]:
        """Agent types, largest saving at their best interval first."""

        def saving(a: AgentRunSplit) -> float:
            best = a.best(self.min_runs)
            return best.saving_usd if best is not None else 0.0

        return sorted(self.agents.values(), key=lambda a: (-saving(a), -a.agent_usd, a.agent_type))


@dataclass(slots=True)
class _Parent:
    """What one parent transcript costs to keep a token in, from each of
    its replies on."""

    priced: list[Turn]
    turn_indices: list[int]
    prefix_rate: list[float]
    boundaries: list[int]
    by_tool_use: dict[str, int]

    def carry_rate(self, position: int) -> float:
        """Per token, across the replies after ``priced[position]`` up to
        the next summary."""
        turn = self.priced[position]
        end_index = carry_end_index(turn.turn_index, self.boundaries, self.priced[-1].turn_index)
        right = bisect_right(self.turn_indices, end_index)
        return self.prefix_rate[right] - self.prefix_rate[position + 1] if right > position + 1 else 0.0


def _parents(results: list[TranscriptResult]) -> tuple[dict, dict]:
    """Every transcript that can start a subagent, by session (top level)
    and by agent id (a subagent that starts others)."""
    top: dict[str, TranscriptResult] = {}
    subs: dict[str, TranscriptResult] = {}
    for tr in results:
        if tr.meta.kind == "top-level":
            top.setdefault(tr.meta.session_id, tr)
        elif tr.meta.agent_id:
            subs[agent_key(tr.meta.agent_id)] = tr
    return top, subs


def _parent_view(tr: TranscriptResult, lookup) -> _Parent | None:
    priced = [t for t in tr.turns if t.turn_index > 0]
    if not priced:
        return None
    by_tool_use = {}
    for position, turn in enumerate(priced):
        for tool_use_id in turn.tool_use_ids:
            by_tool_use[tool_use_id] = position
    return _Parent(
        priced=priced,
        turn_indices=[t.turn_index for t in priced],
        prefix_rate=carry_rate_prefix(priced, lookup),
        boundaries=boundary_turn_indices(priced),
        by_tool_use=by_tool_use,
    )


def _extra_output_cost(turn: Turn, rates, tokens: int) -> float:
    """What ``tokens`` more output would have added to ``turn``."""
    more = _shrunk_cost(turn, rates, 0, output_tokens=turn.output_tokens + tokens)
    return max(0.0, more - price_turn(turn, rates).total)


def _split(
    priced: list[Turn], costs: list[float], lookup, every_n: int, fresh: int, add_back: float, th: RunSplitThresholds
) -> tuple[int, list[int], float]:
    """One run split every ``every_n`` replies: ``(splits, dropped per
    split, saving)``. ``add_back``: each split's parent share and re-read
    allowance."""
    splits = 0
    dropped_each: list[int] = []
    saving = 0.0
    dropped = 0
    since = 0
    for j in range(1, len(priced)):
        turn = priced[j]
        if EventKind.COMPACT_BOUNDARY in turn.preceding_event_kinds:
            # A summary already dropped the old context: count afresh.
            dropped = 0
            since = j
        elif (j - since) % every_n == 0:
            candidate = priced[j - 1].ctx - fresh
            if candidate >= th.min_dropped_tokens:
                dropped = candidate
                splits += 1
                dropped_each.append(int(candidate))
                last = priced[j - 1]
                saving -= _extra_output_cost(last, lookup(last.model), th.note_tokens)
                saving -= _fresh_start_cost(turn, lookup(turn.model), fresh)
                saving -= add_back
        if dropped > 0:
            saving += costs[j] - _shrunk_cost(turn, lookup(turn.model), dropped)
    return splits, dropped_each, saving


def _run(
    tr: TranscriptResult,
    lookup,
    parent: _Parent | None,
    allowance: float,
    th: RunSplitThresholds,
    stats: RunSplitStats,
    window: int | None = None,
) -> None:
    priced = [t for t in tr.turns if t.turn_index > 0]
    if not priced:
        return
    costs = [price_turn(t, lookup(t.model)).total for t in priced]
    run_usd = sum(costs)
    if window is not None and max(t.ctx for t in priced) > window:
        # Longer than the setting now in force lets a context grow: it ran
        # under an older one.
        stats.older_runs += 1
        stats.older_usd += run_usd
        stats.windows.add(window)
        return
    row = stats.agent(tr.meta.agent_type or "unknown")
    n = len(priced)
    row.runs += 1
    row.agent_usd += run_usd
    row.longest_run = max(row.longest_run, n)
    if n <= min(th.intervals):
        return

    fresh = priced[0].ctx + th.note_tokens
    add_back = allowance
    spawn = parent.by_tool_use.get(tr.meta.tool_use_id or "") if parent is not None else None
    if spawn is not None:
        parent_turn = parent.priced[spawn]
        # The parent writes the note into the next brief, then keeps the
        # note it was handed and the brief it wrote.
        add_back += _extra_output_cost(parent_turn, lookup(parent_turn.model), th.note_tokens)
        add_back += 2 * th.note_tokens * parent.carry_rate(spawn)
    split_any = False
    for every_n in th.intervals:
        if n <= every_n:
            continue
        splits, dropped_each, saving = _split(priced, costs, lookup, every_n, fresh, add_back, th)
        if not splits:
            continue
        split_any = True
        at = row.at(every_n)
        at.long_runs += 1
        at.replies.append(n)
        at.splits += splits
        at.dropped.extend(dropped_each)
        at.long_run_usd += run_usd
        at.saving_usd += saving
    if split_any and spawn is None:
        stats.no_parent_runs += 1


#: The auto-compact window now in force, as a number for every run or a
#: function from a run to its own (``None``: not known, so it all counts).
CurrentWindow = Union[int, None, Callable[[TranscriptResult], "int | None"]]


def compute_run_split(
    results: list[TranscriptResult],
    pricing: Pricing,
    thresholds: RunSplitThresholds | None = None,
    rediscovery_allowance_usd: float = 0.0,
    current_window: CurrentWindow = None,
) -> RunSplitStats:
    """The run-split figures over ``results`` (every transcript in the
    window; only subagent runs are split, the rest are read for the parent
    that started each one). ``rediscovery_allowance_usd``: the cost of
    re-reading files after a fresh start, per split; ``build_report``
    passes the one compaction_sim measured from this corpus's real
    summaries. ``current_window``: the auto-compact window now in force
    (for all runs, or per run); a run that peaked above it ran under an
    older setting and is left out of every figure (see the module
    docstring)."""
    th = thresholds or _DEFAULT_THRESHOLDS
    lookup = pricing.resolve_model
    stats = RunSplitStats(
        intervals=th.intervals, min_runs=th.min_runs, rediscovery_allowance_usd=rediscovery_allowance_usd
    )
    top, subs = _parents(results)
    views: dict[int, _Parent | None] = {}
    for tr in results:
        # Subagent runs only: a main session isn't split, and a workflow
        # agent's script, not the agent, decides how its work is split.
        if tr.meta.kind != "subagent":
            continue
        parent_tr = subs.get(agent_key(tr.meta.parent_agent_id)) if tr.meta.parent_agent_id else None
        if parent_tr is None:
            parent_tr = top.get(tr.meta.session_id)
        parent = None
        if parent_tr is not None:
            key = id(parent_tr)
            if key not in views:
                views[key] = _parent_view(parent_tr, lookup)
            parent = views[key]
        window = current_window(tr) if callable(current_window) else current_window
        _run(tr, lookup, parent, rediscovery_allowance_usd, th, stats, window)
    return stats


# -- report section -----------------------------------------------------------


def _median(values: list[int]) -> int | None:
    return int(statistics.median(values)) if values else None


def _share(part: float, whole: float) -> float | None:
    return 100.0 * part / whole if whole > 0 else None


def build_section(stats: RunSplitStats, thresholds: RunSplitThresholds | None = None) -> Section:
    th = thresholds or _DEFAULT_THRESHOLDS
    ranked = stats.ranked()
    bests = {a.agent_type: a.best(stats.min_runs) for a in ranked}
    paying = [b for b in bests.values() if b is not None]
    total_usd = sum(a.agent_usd for a in ranked)
    saving = sum(b.saving_usd for b in paying)
    summary = Table(
        name="run_split_summary",
        title="Splitting long subagent runs",
        columns=[
            Column(key="scope", label="Scope", kind="str"),
            Column(key="runs", label="Subagent runs", kind="int"),
            Column(key="paying_agents", label="Agent types where splitting pays", kind="int"),
            Column(key="long_runs", label="Runs it would split", kind="int"),
            Column(key="splits", label="Splits", kind="int"),
            Column(key="dropped_median", label="Context each split drops (median)", kind="tokens"),
            Column(key="long_run_usd", label="Cost of those runs", kind="money"),
            Column(key="saving_usd", label="Most you could save", kind="money"),
            Column(key="saving_pct", label="Share of subagent cost", kind="pct"),
            Column(key="agent_usd", label="Subagent cost", kind="money"),
            Column(key="older_runs", label="Runs left out (older setting)", kind="int"),
            Column(key="older_usd", label="Cost of those runs", kind="money"),
        ],
        rows=[
            [
                "subagent runs",
                sum(a.runs for a in ranked),
                len(paying),
                sum(b.long_runs for b in paying),
                sum(b.splits for b in paying),
                _median([d for b in paying for d in b.dropped]),
                sum(b.long_run_usd for b in paying),
                saving,
                _share(saving, total_usd),
                total_usd,
                stats.older_runs,
                stats.older_usd,
            ]
        ],
    )
    by_agent_rows = []
    for a in ranked[: th.top_n]:
        best = bests[a.agent_type]
        by_agent_rows.append(
            [
                a.agent_type,
                a.runs,
                a.longest_run,
                best.every_n if best else None,
                best.long_runs if best else 0,
                _median(best.replies) if best else None,
                best.splits if best else 0,
                _median(best.dropped) if best else None,
                best.long_run_usd if best else 0.0,
                best.saving_usd if best else 0.0,
                _share(best.saving_usd, a.agent_usd) if best else None,
                a.agent_usd,
            ]
        )
    by_agent = Table(
        name="run_split_by_agent",
        title="By agent type",
        columns=[
            Column(key="agent_type", label="Agent type", kind="str"),
            Column(key="runs", label="Runs", kind="int"),
            Column(key="longest_run", label="Longest run (replies)", kind="int"),
            Column(key="every_n", label="Split every (replies)", kind="int"),
            Column(key="long_runs", label="Runs it would split", kind="int"),
            Column(key="replies_median", label="Replies in those runs (median)", kind="int"),
            Column(key="splits", label="Splits", kind="int"),
            Column(key="dropped_median", label="Context each split drops (median)", kind="tokens"),
            Column(key="long_run_usd", label="Cost of those runs", kind="money"),
            Column(key="saving_usd", label="Most you could save", kind="money"),
            Column(key="saving_pct", label="Share of its cost", kind="pct"),
            Column(key="agent_usd", label="Cost", kind="money"),
        ],
        rows=by_agent_rows,
    )
    sweep_rows = []
    for every_n in stats.intervals:
        at = [a.by_interval[every_n] for a in ranked if every_n in a.by_interval]
        sweep_rows.append(
            [
                f"every {every_n} replies",
                sum(s.long_runs for s in at),
                sum(s.splits for s in at),
                sum(s.saving_usd for s in at),
                sum(1 for a in ranked if (b := bests[a.agent_type]) is not None and b.every_n == every_n),
            ]
        )
    sweep = Table(
        name="run_split_sweep",
        title="Each split interval",
        columns=[
            Column(key="interval", label="Split interval", kind="str"),
            Column(key="long_runs", label="Runs it would split", kind="int"),
            Column(key="splits", label="Splits", kind="int"),
            Column(key="net_usd", label="Net saving, every agent type", kind="money"),
            Column(key="best_for", label="Best interval for (agent types)", kind="int"),
        ],
        rows=sweep_rows,
    )
    notes = list(ASSUMPTIONS) + [
        f"File re-read allowance per split: ${stats.rediscovery_allowance_usd:.4f} at list price.",
        f"Thresholds: {' '.join(th.describe())}",
    ]
    if stats.no_parent_runs:
        notes.append(
            f"{stats.no_parent_runs} runs long enough to split have no parent transcript in view, so the "
            "parent's part of each split's cost is left out for them."
        )
    if stats.older_runs:
        low, high = min(stats.windows), max(stats.windows)
        window = f"{low:,}" if low == high else f"{low:,} to {high:,}"
        many = stats.older_runs != 1
        notes.append(
            f"{stats.older_runs} run{'s' if many else ''} grew past the auto-compact "
            f"window now in force ({window} tokens), so {'they' if many else 'it'} ran under an older setting. "
            f"{'They are' if many else 'It is'} left out of every figure here."
        )
    if len(ranked) > th.top_n:
        notes.append(f"{len(ranked) - th.top_n} more agent types aren't listed.")
    return Section(
        key="run_split", title="Splitting long subagent runs", tables=[summary, by_agent, sweep], notes=notes
    )


# -- recommendation rule -------------------------------------------------


def _table(report: ReportModel, section_key: str, table_name: str) -> Table | None:
    for section in report.sections:
        if section.key != section_key:
            continue
        for table in section.tables:
            if table.name == table_name:
                return table
    return None


def _evidence(label: str, value, section_key: str, table_name: str, row_key) -> tuple:
    return (label, value, f"{section_key}.{table_name}", row_key)


def _num(value) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _rule_run_split(report: ReportModel, th: RunSplitThresholds) -> list[Recommendation]:
    """``run-split``: an agent type whose runs, split at their best
    interval, would have saved at least ``min_saving_share_pct`` of its
    cost. One card per agent type."""
    table = _table(report, "run_split", "run_split_by_agent")
    if table is None:
        return []
    out: list[Recommendation] = []
    for values in table.rows:
        r = {col.key: value for col, value in zip(table.columns, values)}
        agent = str(r["agent_type"])
        every_n = int(_num(r.get("every_n")))
        long_runs = int(_num(r.get("long_runs")))
        saving = _num(r.get("saving_usd"))
        share = _num(r.get("saving_pct"))
        if not every_n or long_runs < th.min_runs or saving <= 0 or share < th.min_saving_share_pct:
            continue
        replies = int(_num(r.get("replies_median")))
        dropped = int(_num(r.get("dropped_median")))
        out.append(
            Recommendation(
                id="run-split",
                severity="advice",
                category="workflow",
                archetypes=(),
                title=f"Give {agent} smaller tasks: its long runs keep re-reading everything",
                why=(
                    f"{long_runs:,} {agent} runs went past {every_n} replies, {replies:,} at the median. Every later "
                    f"reply reads again all the run has read, and a fresh run every {every_n} replies would carry "
                    f"about {dropped:,} fewer tokens. Overlaps with the compaction tip: together they save less than "
                    "the two figures added up."
                ),
                action=(
                    f"Give {agent} one part of a large task per run, about {every_n} replies' worth. For the next "
                    f"part, start a fresh {agent} with a short note of what's done, what's left and the files "
                    "involved."
                ),
                lever=None,
                agent_type=agent,
                saving_usd=saving,
                evidence=[
                    _evidence("Split every (replies)", every_n, "run_split", "run_split_by_agent", agent),
                    _evidence("Runs it would split", long_runs, "run_split", "run_split_by_agent", agent),
                    _evidence("Replies in those runs (median)", replies, "run_split", "run_split_by_agent", agent),
                    _evidence("Context each split drops (median)", dropped, "run_split", "run_split_by_agent", agent),
                    _evidence("Most you could save", saving, "run_split", "run_split_by_agent", agent),
                    _evidence("Share of its cost", share, "run_split", "run_split_by_agent", agent),
                ],
            )
        )
    return out


RULES: list[Callable[[ReportModel, RunSplitThresholds], list[Recommendation]]] = [_rule_run_split]


__all__ = [
    "ASSUMPTIONS",
    "DEFAULT_INTERVALS",
    "RunSplitThresholds",
    "RunSplitStats",
    "AgentRunSplit",
    "SplitAt",
    "compute_run_split",
    "build_section",
    "RULES",
]

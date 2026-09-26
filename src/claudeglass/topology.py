"""Topology analytics (WP8): how tokens, cost and information flow
between a top-level session and the agents/skills/workflows it spawns.

Implements plan section "Topology and workstyle" (a)-(i) and Feature
expansion items 1 ("context composition per turn") and 2 ("redundant
work metrics"). :class:`TopologyStats` is an accumulator — mirroring
``pricing.PricingCoverage``'s add-then-summarise shape — fed one
session at a time via :meth:`TopologyStats.add_session(session_id, top,
subs, rates_lookup)`, so the same object serves a single-session report
or a whole-corpus one by calling it in a loop. :func:`build_section`
turns a finished accumulator into a ``Section`` of report ``Table``s.

Two things this module reads only lengths/counts/ids of, never content:

- The skill roll-up's tool_use_id join (subagent ``.meta.json``
  ``toolUseId`` -> the parent turn, and that turn's ``attributionSkill``,
  that spawned it) is built in-memory from already-parsed
  ``Turn.tool_use_ids`` (batch C addition — see model.py's module
  docstring) via :func:`_tool_use_index_from_turns`.
  :func:`index_tool_use_ids`, which re-scans a top-level transcript's raw
  JSONL for the same join, is kept only as a fallback for a
  ``TranscriptResult`` parsed before that field existed (``subs``/``top``
  loaded from an on-disk cache written by an older schema version, say) —
  see ``_add_skill_rollup``.
- The skill roll-up's "spawned cost" is the transitive closure of every
  agent reachable from a skill's invoking turns via that tool_use_id join
  (direct spawns) and then ``parent_agent_id`` (further agents those
  spawns themselves started) — so a custom skill that fans out is costed
  as a whole, per the plan.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from . import jsonl
from .model import Column, EventKind, Section, Table, TranscriptResult, Turn
from .pricing import Pricing, price_turn

#: No tokenizer is run over transcript content (privacy rule); tool-result
#: and attachment sizes are only ever known in characters, so this is the
#: approximate chars-per-token ratio used to render them as a token
#: estimate — every table built from it says "approximate" explicitly.
_CHARS_PER_TOKEN_APPROX = 4

#: How many turns after a compact_boundary count as "rediscovery" if they
#: re-Read a file (Feature expansion item 2).
_REDISCOVERY_WINDOW_TURNS = 10


# -- small shared helpers -------------------------------------------------


def _priced_turns(result: TranscriptResult) -> list[Turn]:
    """The replies that actually got a ``turn_index`` (excludes synthetic
    and missing-usage turns, which parse.py leaves at ``turn_index ==
    0``). An estimated compaction call is priced but not a reply, so it
    is left out here and counted only in :func:`_transcript_cost`."""
    return [t for t in result.turns if t.turn_index > 0 and not t.is_synthetic]


def _transcript_cost(result: TranscriptResult, rates_lookup: Pricing) -> float:
    """Sum ``price_turn`` over every priced turn in one transcript,
    including an estimated compaction call (spend, not a reply).

    Deliberately duplicated in ``workflows.py`` rather than imported —
    see that module's docstring note on the same helper.
    """
    total = 0.0
    for turn in (t for t in result.turns if t.turn_index > 0):
        resolved = rates_lookup.resolve_model(turn.model)
        total += price_turn(turn, resolved).total
    return total


def agent_key(agent_id: str | None) -> str:
    """An agent id as ``parent_agent_id`` spells it. A subagent's
    ``meta.agent_id`` is its file stem (``agent-a98...``), while the
    ``parentAgentId`` its children record is the bare id (``a98...``)."""
    return (agent_id or "").removeprefix("agent-")


def _first_priced_turn(result: TranscriptResult) -> Turn | None:
    for turn in result.turns:
        if turn.turn_index == 1:
            return turn
    return None


@dataclass(slots=True)
class _ReportIndex:
    """Where each agent's report was measured, for :func:`_report_tokens`:
    characters by the ``Agent`` tool_use id it answered (a synchronous
    agent's tool_result, ``Turn.agent_result_chars``) and by agent id (a
    background agent's task notification, which carries its report)."""

    by_tool_use: dict[str, int] = field(default_factory=dict)
    by_agent_id: dict[str, int] = field(default_factory=dict)


def _report_index(top: TranscriptResult, subs: Sequence[TranscriptResult]) -> _ReportIndex:
    """Every report size one session measured, from the transcript each
    report was handed back to (the top level for direct spawns, a
    subagent for the agents it started)."""
    index = _ReportIndex()
    for result in (top, *subs):
        for turn in result.turns:
            index.by_tool_use.update(turn.agent_result_chars)
        for event in result.events:
            if event.kind == EventKind.TASK_NOTIFICATION and event.size_chars:
                task_id = event.detail.get("task_id")
                if isinstance(task_id, str) and task_id:
                    index.by_agent_id[agent_key(task_id)] = event.size_chars
    return index


def _report_tokens(result: TranscriptResult, index: _ReportIndex | None = None) -> int:
    """The size of the report one agent handed back to whatever started
    it, in approximate tokens: the parent-side tool_result (or, for a
    background agent, its task notification) when ``index`` has it,
    measured in characters over :data:`_CHARS_PER_TOKEN_APPROX`. Falls
    back to the agent's own last priced turn's output tokens -- a proxy,
    since that turn can also carry tool calls or thinking -- when the
    parent side wasn't found (a digest parsed before ``PARSER_VERSION``
    15, or a parent transcript that is missing)."""
    if index is not None:
        chars = None
        if result.meta.tool_use_id:
            chars = index.by_tool_use.get(result.meta.tool_use_id)
        if chars is None and result.meta.agent_id:
            chars = index.by_agent_id.get(agent_key(result.meta.agent_id))
        if chars is not None:
            return round(chars / _CHARS_PER_TOKEN_APPROX)
    priced = _priced_turns(result)
    return priced[-1].output_tokens if priced else 0


def _agent_type_label(result: TranscriptResult) -> str:
    return result.meta.agent_type or "unknown"


def _mean(values: Sequence[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _median(values: Sequence[float]) -> float | None:
    return statistics.median(values) if values else None


def index_tool_use_ids(path: str | Path) -> dict[str, tuple[str, str | None]]:
    """Re-scan a top-level transcript's raw JSONL for every assistant
    line's ``tool_use`` content blocks, mapping each block's ``id`` to
    ``(message_id, attribution_skill)`` — the two things ``topology``'s
    skill roll-up needs to join a subagent (via its ``.meta.json``
    ``toolUseId``) back to the top-level turn, and the skill that turn
    ran as, that spawned it.

    Stores only ids and the skill name — ``attribution_skill`` is already
    a plain field on ``Turn`` (see model.py), so a skill name is not new
    exposure here, unlike (say) ``invoked_skills`` attachment names,
    which ``events.py`` deliberately reduces to a count for exactly this
    reason.

    Fallback only: ``_add_skill_rollup`` prefers the in-memory join built
    from ``Turn.tool_use_ids`` (:func:`_tool_use_index_from_turns`, batch
    C) and only re-scans the raw file via this function when that index
    comes back empty (see module docstring).
    """
    index: dict[str, tuple[str, str | None]] = {}
    for _line_no, d in jsonl.iter_lines(path):
        if d.get("type") != "assistant":
            continue
        message = d.get("message")
        if not isinstance(message, dict):
            continue
        message_id = message.get("id")
        if not isinstance(message_id, str) or not message_id:
            continue
        attribution_skill = d.get("attributionSkill")
        if not isinstance(attribution_skill, str):
            attribution_skill = None
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            tool_use_id = block.get("id")
            if isinstance(tool_use_id, str) and tool_use_id:
                index[tool_use_id] = (message_id, attribution_skill)
    return index


def _tool_use_index_from_turns(top: TranscriptResult) -> dict[str, tuple[str, str | None]]:
    """Batch C: the in-memory equivalent of :func:`index_tool_use_ids`,
    built from already-parsed ``Turn.tool_use_ids`` instead of re-scanning
    the raw JSONL. Empty when every turn's ``tool_use_ids`` is empty
    (e.g. a ``TranscriptResult`` parsed before that field existed) — the
    caller falls back to :func:`index_tool_use_ids` in that case.
    """
    index: dict[str, tuple[str, str | None]] = {}
    for turn in top.turns:
        for tool_use_id in turn.tool_use_ids:
            index[tool_use_id] = (turn.message_id, turn.attribution_skill)
    return index


def _transitive_closure(
    direct: list[TranscriptResult],
    children_by_parent: dict[str, list[TranscriptResult]],
) -> list[TranscriptResult]:
    """Every transcript reachable from ``direct`` by following
    ``parent_agent_id`` chains, direct spawns included. Dedupes by object
    identity — ``TranscriptResult`` isn't hashable (a plain, non-frozen
    dataclass), and doesn't need to be for this one walk.
    """
    seen: set[int] = set()
    result: list[TranscriptResult] = []
    stack = list(direct)
    while stack:
        sub = stack.pop()
        if id(sub) in seen:
            continue
        seen.add(id(sub))
        result.append(sub)
        agent_id = agent_key(sub.meta.agent_id)
        if agent_id:
            stack.extend(children_by_parent.get(agent_id, []))
    return result


@dataclass(slots=True)
class _SkillAccumulator:
    invocations: int = 0
    direct_cost: float = 0.0
    spawned_cost: float = 0.0
    direct_spawns: int = 0
    report_proxy_values: list[int] = field(default_factory=list)


# -- the accumulator --------------------------------------------------------


@dataclass(slots=True)
class TopologyStats:
    """Accumulates every topology metric in the plan's (a)-(i) list, one
    session at a time via :meth:`add_session`. See module docstring for
    why this is an accumulator rather than a single compute-from-args
    function.
    """

    sessions_seen: int = 0

    # (a) downward: spawn write per agent type, session baseline
    spawn_write_by_agent_type: dict[str, list[int]] = field(default_factory=dict)
    session_baseline_writes: list[int] = field(default_factory=list)
    #: Capture-improvements addition (A7): the spawning top-level turn's
    #: ``agent_brief_chars`` for each direct spawn (joined via
    #: ``sub.meta.tool_use_id`` -> ``_tool_use_index_from_turns``, same
    #: join as the skill roll-up), keyed by agent type. Unlike
    #: ``spawn_write_by_agent_type`` above (which folds in *every* sub
    #: regardless of linkage), a sub reached only transitively via
    #: ``parent_agent_id`` (e.g. a sub-subagent with no ``tool_use_id`` of
    #: its own, or a turn whose Agent/Task tool_use carried no ``prompt``)
    #: has nothing to join to and contributes no value here.
    spawn_brief_chars_by_agent_type: dict[str, list[int]] = field(default_factory=dict)

    # (b) upward: Agent/Workflow tool_result totals, per-agent-type proxy
    agent_tool_result_chars: int = 0
    agent_tool_result_calls: int = 0
    workflow_tool_result_chars: int = 0
    workflow_tool_result_calls: int = 0
    report_proxy_by_agent_type: dict[str, list[int]] = field(default_factory=dict)

    # (c) skill roll-up
    skills: dict[str, _SkillAccumulator] = field(default_factory=dict)

    # (d) chains
    spawn_depth_histogram: dict[int, int] = field(default_factory=dict)
    spawns_per_session: list[int] = field(default_factory=list)
    cost_by_agent_type: dict[str, list[float]] = field(default_factory=dict)
    stopped_by_user_count: int = 0
    total_spawns: int = 0
    #: Capture-improvements addition (A7): every priced turn's
    #: ``tool_wait_s`` (see model.py's ``Turn.tool_wait_s`` docstring),
    #: keyed by agent type -- turns with no tool call (``tool_wait_s is
    #: None``) contribute nothing.
    tool_wait_by_agent_type: dict[str, list[float]] = field(default_factory=dict)

    # (e) reminder/hook pressure, CACHE_SIGNAL histogram
    reminder_rate_by_kind: dict[str, list[float]] = field(default_factory=dict)
    hook_rate_by_kind: dict[str, list[float]] = field(default_factory=dict)
    cache_signal_subkind_histogram: dict[str, int] = field(default_factory=dict)

    # (f) MCP cost
    mcp_cost_by_server: dict[str, float] = field(default_factory=dict)
    mcp_turns_by_server: dict[str, int] = field(default_factory=dict)

    # (g) effort / thinking share
    effort_output: dict[str, int] = field(default_factory=dict)
    effort_thinking: dict[str, int] = field(default_factory=dict)
    effort_turns: dict[str, int] = field(default_factory=dict)
    per_turn_effort_output: dict[str, int] = field(default_factory=dict)
    per_turn_effort_thinking: dict[str, int] = field(default_factory=dict)
    per_turn_effort_turns: dict[str, int] = field(default_factory=dict)
    agent_type_output: dict[str, int] = field(default_factory=dict)
    agent_type_thinking: dict[str, int] = field(default_factory=dict)

    # (h) context composition, grouped by TranscriptMeta.kind
    composition_transcripts: dict[str, int] = field(default_factory=dict)
    composition_baseline: dict[str, list[int]] = field(default_factory=dict)
    composition_tool_result_tokens: dict[str, list[float]] = field(default_factory=dict)
    composition_output_tokens: dict[str, list[int]] = field(default_factory=dict)
    composition_attachment_tokens: dict[str, list[float]] = field(default_factory=dict)
    composition_compactions: dict[str, list[int]] = field(default_factory=dict)

    # (i) redundant work
    redundant_cmd_repeats_per_session: list[int] = field(default_factory=list)
    rediscovery_reads_per_session: list[int] = field(default_factory=list)

    #: Fix #8 consumer: the A3 read-target-hash subsystem
    #: (``parse.set_salt``/``Turn.read_target_hashes``) had no consumer
    #: anywhere in ``src/`` -- see :meth:`_add_redundant_reads`.
    redundant_reads_per_session: list[int] = field(default_factory=list)
    redundant_reads_after_compaction_per_session: list[int] = field(default_factory=list)

    def add_session(
        self,
        session_id: str,
        top: TranscriptResult,
        subs: list[TranscriptResult],
        rates_lookup: Pricing,
    ) -> None:
        """Fold one session's top-level transcript and its subagent
        transcripts into every accumulator above.

        ``session_id`` isn't read by any accumulator below (every metric
        here aggregates across sessions, not per-session) but is taken
        explicitly to match the plan's "fed by (session_id, top, subs,
        rates_lookup)" signature, so a future per-session breakdown can
        be added without changing this call site.
        """
        self.sessions_seen += 1
        self._add_downward(top, subs)
        self._add_upward(top, subs)
        self._add_skill_rollup(top, subs, rates_lookup)
        self._add_chains(subs, rates_lookup)
        self._add_reminder_hook_pressure(top, subs)
        self._add_mcp_cost(top, subs, rates_lookup)
        self._add_effort(top, subs)
        self._add_composition(top, subs)
        self._add_redundant_work(top)
        self._add_redundant_reads(top)

    # -- (a) downward --------------------------------------------------

    def _add_downward(self, top: TranscriptResult, subs: list[TranscriptResult]) -> None:
        baseline_turn = _first_priced_turn(top)
        if baseline_turn is not None:
            self.session_baseline_writes.append(baseline_turn.cache_creation_tokens)
        # A7: join each sub back to its spawning top-level turn (same
        # tool_use_id join the skill roll-up uses) to read that turn's
        # own agent_brief_chars.
        tool_use_index = _tool_use_index_from_turns(top)
        turns_by_message_id = {t.message_id: t for t in top.turns if t.message_id}
        for sub in subs:
            first = _first_priced_turn(sub)
            if first is None:
                continue
            label = _agent_type_label(sub)
            self.spawn_write_by_agent_type.setdefault(label, []).append(first.cache_creation_tokens)
            brief_chars = self._spawn_brief_chars(sub, tool_use_index, turns_by_message_id)
            if brief_chars is not None:
                self.spawn_brief_chars_by_agent_type.setdefault(label, []).append(brief_chars)

    @staticmethod
    def _spawn_brief_chars(
        sub: TranscriptResult,
        tool_use_index: dict[str, tuple[str, str | None]],
        turns_by_message_id: dict[str, Turn],
    ) -> int | None:
        tool_use_id = sub.meta.tool_use_id
        if not tool_use_id:
            return None
        entry = tool_use_index.get(tool_use_id)
        if entry is None:
            return None
        message_id, _skill_at_call_site = entry
        parent_turn = turns_by_message_id.get(message_id)
        if parent_turn is None:
            return None
        return parent_turn.agent_brief_chars

    # -- (b) upward ------------------------------------------------------

    def _add_upward(self, top: TranscriptResult, subs: list[TranscriptResult]) -> None:
        self.agent_tool_result_chars += top.tool_result_chars.get("Agent", 0)
        self.agent_tool_result_calls += top.tool_result_calls.get("Agent", 0)
        self.workflow_tool_result_chars += top.tool_result_chars.get("Workflow", 0)
        self.workflow_tool_result_calls += top.tool_result_calls.get("Workflow", 0)
        index = _report_index(top, subs)
        for sub in subs:
            label = _agent_type_label(sub)
            self.report_proxy_by_agent_type.setdefault(label, []).append(_report_tokens(sub, index))

    # -- (c) skill roll-up -------------------------------------------------

    def _add_skill_rollup(
        self, top: TranscriptResult, subs: list[TranscriptResult], rates_lookup: Pricing
    ) -> None:
        tool_use_index = _tool_use_index_from_turns(top)
        if not tool_use_index:
            # Fallback for a TranscriptResult parsed before Turn.tool_use_ids
            # existed (or a transcript that genuinely made no tool calls at
            # top level, where the raw re-scan is equally empty and cheap).
            if not top.meta.path:
                return
            tool_use_index = index_tool_use_ids(top.meta.path)

        children_by_parent: dict[str, list[TranscriptResult]] = {}
        for sub in subs:
            parent = agent_key(sub.meta.parent_agent_id)
            if parent:
                children_by_parent.setdefault(parent, []).append(sub)

        direct_by_message_id: dict[str, list[TranscriptResult]] = {}
        for sub in subs:
            tool_use_id = sub.meta.tool_use_id
            if not tool_use_id:
                continue
            entry = tool_use_index.get(tool_use_id)
            if entry is None:
                continue
            message_id, _skill_at_call_site = entry
            direct_by_message_id.setdefault(message_id, []).append(sub)

        skill_turns: dict[str, list[Turn]] = {}
        for turn in _priced_turns(top):
            if turn.attribution_skill:
                skill_turns.setdefault(turn.attribution_skill, []).append(turn)
        report_index = _report_index(top, subs) if skill_turns else None

        for skill_name, turns in skill_turns.items():
            acc = self.skills.setdefault(skill_name, _SkillAccumulator())
            acc.invocations += len(turns)
            acc.direct_cost += sum(
                price_turn(t, rates_lookup.resolve_model(t.model)).total for t in turns
            )
            message_ids = {t.message_id for t in turns if t.message_id}
            direct_subs: list[TranscriptResult] = []
            for message_id in message_ids:
                direct_subs.extend(direct_by_message_id.get(message_id, []))
            acc.direct_spawns += len(direct_subs)

            closure = _transitive_closure(direct_subs, children_by_parent)
            acc.spawned_cost += sum(_transcript_cost(sub, rates_lookup) for sub in closure)
            acc.report_proxy_values.extend(_report_tokens(sub, report_index) for sub in closure)

    # -- (d) chains --------------------------------------------------------

    def _add_chains(self, subs: list[TranscriptResult], rates_lookup: Pricing) -> None:
        self.total_spawns += len(subs)
        self.spawns_per_session.append(len(subs))
        for sub in subs:
            depth = sub.meta.spawn_depth
            self.spawn_depth_histogram[depth] = self.spawn_depth_histogram.get(depth, 0) + 1
            label = _agent_type_label(sub)
            self.cost_by_agent_type.setdefault(label, []).append(_transcript_cost(sub, rates_lookup))
            for turn in _priced_turns(sub):
                if turn.tool_wait_s is not None:
                    self.tool_wait_by_agent_type.setdefault(label, []).append(turn.tool_wait_s)
            if sub.meta.stopped_by_user:
                self.stopped_by_user_count += 1

    # -- (e) reminder/hook pressure, CACHE_SIGNAL histogram -----------------

    def _add_reminder_hook_pressure(
        self, top: TranscriptResult, subs: list[TranscriptResult]
    ) -> None:
        for result in (top, *subs):
            kind = result.meta.kind
            turn_count = max(1, len(_priced_turns(result)))
            reminder_count = sum(1 for e in result.events if e.kind == EventKind.REMINDER)
            hook_count = sum(1 for e in result.events if e.kind == EventKind.HOOK_OUTPUT)
            self.reminder_rate_by_kind.setdefault(kind, []).append(reminder_count / turn_count)
            self.hook_rate_by_kind.setdefault(kind, []).append(hook_count / turn_count)
            for e in result.events:
                if e.kind == EventKind.CACHE_SIGNAL and e.subkind:
                    self.cache_signal_subkind_histogram[e.subkind] = (
                        self.cache_signal_subkind_histogram.get(e.subkind, 0) + 1
                    )

    # -- (f) MCP cost --------------------------------------------------------

    def _add_mcp_cost(
        self, top: TranscriptResult, subs: list[TranscriptResult], rates_lookup: Pricing
    ) -> None:
        for result in (top, *subs):
            for turn in _priced_turns(result):
                server = turn.attribution_mcp_server
                if not server:
                    continue
                cost = price_turn(turn, rates_lookup.resolve_model(turn.model)).total
                self.mcp_cost_by_server[server] = self.mcp_cost_by_server.get(server, 0.0) + cost
                self.mcp_turns_by_server[server] = self.mcp_turns_by_server.get(server, 0) + 1

    # -- (g) effort / thinking share -----------------------------------------

    def _add_effort(self, top: TranscriptResult, subs: list[TranscriptResult]) -> None:
        for result in (top, *subs):
            label = "top-level" if result is top else _agent_type_label(result)
            for turn in _priced_turns(result):
                if turn.effort:
                    self.effort_output[turn.effort] = (
                        self.effort_output.get(turn.effort, 0) + turn.output_tokens
                    )
                    self.effort_thinking[turn.effort] = (
                        self.effort_thinking.get(turn.effort, 0) + turn.thinking_tokens
                    )
                    self.effort_turns[turn.effort] = self.effort_turns.get(turn.effort, 0) + 1
                if turn.per_turn_effort:
                    self.per_turn_effort_output[turn.per_turn_effort] = (
                        self.per_turn_effort_output.get(turn.per_turn_effort, 0) + turn.output_tokens
                    )
                    self.per_turn_effort_thinking[turn.per_turn_effort] = (
                        self.per_turn_effort_thinking.get(turn.per_turn_effort, 0)
                        + turn.thinking_tokens
                    )
                    self.per_turn_effort_turns[turn.per_turn_effort] = (
                        self.per_turn_effort_turns.get(turn.per_turn_effort, 0) + 1
                    )
                self.agent_type_output[label] = self.agent_type_output.get(label, 0) + turn.output_tokens
                self.agent_type_thinking[label] = (
                    self.agent_type_thinking.get(label, 0) + turn.thinking_tokens
                )

    # -- (h) context composition ---------------------------------------------

    def _add_composition(self, top: TranscriptResult, subs: list[TranscriptResult]) -> None:
        for result in (top, *subs):
            kind = result.meta.kind
            self.composition_transcripts[kind] = self.composition_transcripts.get(kind, 0) + 1
            first = _first_priced_turn(result)
            self.composition_baseline.setdefault(kind, []).append(
                first.cache_creation_tokens if first is not None else 0
            )
            tool_result_chars_total = sum(result.tool_result_chars.values())
            self.composition_tool_result_tokens.setdefault(kind, []).append(
                tool_result_chars_total / _CHARS_PER_TOKEN_APPROX
            )
            output_total = sum(t.output_tokens for t in _priced_turns(result))
            self.composition_output_tokens.setdefault(kind, []).append(output_total)
            attachment_chars = sum(
                e.size_chars or 0
                for e in result.events
                if e.kind in (EventKind.CACHE_SIGNAL, EventKind.REMINDER, EventKind.CONTEXT_INJECT)
            )
            self.composition_attachment_tokens.setdefault(kind, []).append(
                attachment_chars / _CHARS_PER_TOKEN_APPROX
            )
            compaction_count = sum(1 for e in result.events if e.kind == EventKind.COMPACT_SUMMARY)
            self.composition_compactions.setdefault(kind, []).append(compaction_count)

    # -- (i) redundant work ---------------------------------------------------

    def _add_redundant_work(self, top: TranscriptResult) -> None:
        priced = _priced_turns(top)

        prefix_counts: dict[str, int] = {}
        for turn in priced:
            if turn.cmd_prefix:
                prefix_counts[turn.cmd_prefix] = prefix_counts.get(turn.cmd_prefix, 0) + 1
        repeats = sum(count - 1 for count in prefix_counts.values() if count > 1)
        self.redundant_cmd_repeats_per_session.append(repeats)

        compaction_starts = [
            t.turn_index for t in priced if EventKind.COMPACT_BOUNDARY in t.preceding_event_kinds
        ]
        by_index = {t.turn_index: t for t in priced}
        rediscovery = 0
        for start in compaction_starts:
            for idx in range(start, start + _REDISCOVERY_WINDOW_TURNS):
                turn = by_index.get(idx)
                if turn is not None and "Read" in turn.tool_names:
                    rediscovery += 1
        self.rediscovery_reads_per_session.append(rediscovery)

    def _add_redundant_reads(self, top: TranscriptResult) -> None:
        """Fix #8: the only consumer of ``Turn.read_target_hashes`` (a
        salted hash of each Read tool's target path -- see
        ``parse.set_salt``/``parse.load_or_create_salt``'s own
        docstrings). Counts, per top-level transcript, how many times a
        file already read earlier in the same transcript is read again
        with no edit to it in between (``Turn.edit_target_hashes``: after
        an edit, reading the file again is checking the change, not
        rereading it), and how many of those repeats land within
        :data:`_REDISCOVERY_WINDOW_TURNS` turns of a ``COMPACT_BOUNDARY``
        -- the same window ``_add_redundant_work``'s rediscovery metric
        uses, since this is the same phenomenon (context lost to
        compaction, paid for again) narrowed from "any Read tool call" to
        "a read of a file this session had already read before".

        Requires a salt to have been wired up before parsing (``cli.py``'s
        corpus load does this automatically via
        ``parse.load_or_create_salt`` -- see ``corpus.load_corpus``'s own
        docstring); with no salt set, every ``Turn.read_target_hashes`` is
        empty and both counts are always 0 -- the same "cleanly reports
        nothing" degradation every metric in this module follows for an
        unmet precondition, never a crash.

        Only ever sees a hash, never a path -- consistent with this
        module's privacy rule (see the module docstring).
        """
        priced = _priced_turns(top)
        compaction_starts = [
            t.turn_index for t in priced if EventKind.COMPACT_BOUNDARY in t.preceding_event_kinds
        ]
        seen_hashes: set[str] = set()
        repeats = 0
        repeats_after_compaction = 0
        for turn in priced:
            in_rediscovery_window = any(
                start <= turn.turn_index < start + _REDISCOVERY_WINDOW_TURNS
                for start in compaction_starts
            )
            for target_hash in turn.read_target_hashes:
                if target_hash in seen_hashes:
                    repeats += 1
                    if in_rediscovery_window:
                        repeats_after_compaction += 1
                else:
                    seen_hashes.add(target_hash)
            # A turn's reads come before its edits: an edited file's next
            # read is a first read again.
            seen_hashes.difference_update(turn.edit_target_hashes)
        self.redundant_reads_per_session.append(repeats)
        self.redundant_reads_after_compaction_per_session.append(repeats_after_compaction)


# -- Section/Table assembly --------------------------------------------------


def _build_spawn_write_table(stats: TopologyStats) -> Table:
    columns = [
        Column(key="agent_type", label="Agent type", kind="str"),
        Column(key="spawns", label="Spawns", kind="int"),
        Column(key="mean_write", label="Mean spawn write", kind="tokens"),
        Column(key="median_write", label="Median spawn write", kind="tokens"),
        Column(key="mean_briefing_chars", label="Mean briefing chars", kind="int"),
    ]
    rows = [
        [
            agent_type,
            len(values),
            _mean(values),
            _median(values),
            _mean(stats.spawn_brief_chars_by_agent_type.get(agent_type, [])),
        ]
        for agent_type, values in sorted(stats.spawn_write_by_agent_type.items())
    ]
    return Table(
        name="topology_spawn_write",
        title="Downward: mean/median first-turn cache_creation per agent type (spawn write)",
        columns=columns,
        rows=rows,
        notes=[
            "The startup write is the cache write on each subagent's first"
            " reply. It holds the task prompt, system prompt and any preloaded"
            " skills you pay to write into that agent's cache.",
            "Your MCP servers and plugins aren't matched against this table:"
            " nothing yet links a subagent's startup write to the settings in"
            " effect when it ran.",
            "Average task prompt is the length of the prompt in the call that"
            " started each subagent. It is empty when a subagent can't be"
            " matched to that call, or the call carried no prompt.",
        ],
    )


def _build_session_baseline_table(stats: TopologyStats) -> Table:
    values = stats.session_baseline_writes
    columns = [
        # v0.2.0 fix A2: leading string row-key column -- see
        # recache.py's module docstring for the same fix and rationale;
        # this table's single row used to start with the bare `sessions`
        # int count.
        Column(key="metric", label="Metric", kind="str"),
        Column(key="sessions", label="Sessions", kind="int"),
        Column(key="mean_baseline", label="Mean session baseline", kind="tokens"),
        Column(key="median_baseline", label="Median session baseline", kind="tokens"),
    ]
    rows = [["all", len(values), _mean(values), _median(values)]]
    return Table(
        name="topology_session_baseline",
        title="Session baseline: top-level first-turn cache_creation",
        columns=columns,
        rows=rows,
        notes=[
            "The main session's own first reply: system prompt, CLAUDE.md and"
            " the tool definitions loaded up front. Your MCP servers and"
            " plugins aren't matched against it: nothing yet links the two.",
        ],
    )


def _build_upward_table(stats: TopologyStats) -> Table:
    columns = [
        Column(key="tool", label="Parent-side tool", kind="str"),
        Column(key="calls", label="Calls", kind="int"),
        Column(key="total_chars", label="Total result chars", kind="int"),
        Column(key="mean_chars_per_call", label="Mean chars/call", kind="int"),
    ]
    rows = []
    for label, chars, calls in (
        ("Agent", stats.agent_tool_result_chars, stats.agent_tool_result_calls),
        ("Workflow", stats.workflow_tool_result_chars, stats.workflow_tool_result_calls),
    ):
        mean_chars = chars / calls if calls else None
        rows.append([label, calls, chars, mean_chars])
    return Table(
        name="topology_upward_tool_result",
        title="Upward: parent-side Agent/Workflow tool_result sizes",
        columns=columns,
        rows=rows,
        notes=[
            "From the main session's own totals only, not split by agent type:"
            " the main session doesn't record which subagent produced which"
            " report. The report size table below splits it by agent type.",
        ],
    )


def _build_report_proxy_table(stats: TopologyStats) -> Table:
    columns = [
        Column(key="agent_type", label="Agent type", kind="str"),
        Column(key="spawns", label="Spawns", kind="int"),
        Column(key="mean_proxy", label="Mean report size (tokens)", kind="tokens"),
        Column(key="median_proxy", label="Median report size (tokens)", kind="tokens"),
    ]
    rows = [
        [agent_type, len(values), _mean(values), _median(values)]
        for agent_type, values in sorted(stats.report_proxy_by_agent_type.items())
    ]
    return Table(
        name="topology_report_proxy",
        title="Upward: per-subagent report size, by agent type",
        columns=columns,
        rows=rows,
        notes=[
            "Report size is the report each subagent handed back, in"
            f" characters divided by {_CHARS_PER_TOKEN_APPROX}. It is measured"
            " where the report arrived: the Agent tool's result in the parent,"
            " or a background agent's task notification. When the parent side wasn't found,"
            " it falls back to the output tokens of the subagent's own last"
            " reply, an approximation.",
        ],
    )


def _build_skills_table(stats: TopologyStats) -> Table:
    columns = [
        Column(key="skill", label="Skill", kind="str"),
        Column(key="invocations", label="Invocations", kind="int"),
        Column(key="direct_cost", label="Direct cost", kind="money"),
        Column(key="spawned_cost", label="Spawned cost", kind="money"),
        Column(key="total_cost", label="Total cost", kind="money"),
        Column(key="mean_spawns", label="Mean spawns/invocation", kind="float"),
        Column(key="mean_report_proxy", label="Mean report size (tokens)", kind="tokens"),
    ]
    rows = []
    for name, acc in sorted(stats.skills.items()):
        total_cost = acc.direct_cost + acc.spawned_cost
        mean_spawns = acc.direct_spawns / acc.invocations if acc.invocations else None
        rows.append(
            [
                name,
                acc.invocations,
                acc.direct_cost,
                acc.spawned_cost,
                total_cost,
                mean_spawns,
                _mean(acc.report_proxy_values),
            ]
        )
    return Table(
        name="topology_skills_rollup",
        title="Skill roll-up: cost of a skill's own turns plus everything it spawned",
        columns=columns,
        rows=rows,
        notes=[
            "Subagent cost counts every subagent the skill's replies started,"
            " and every subagent those started in turn. So a skill that fans"
            " out is costed as a whole.",
            "Agents inside a workflow don't record the call that started"
            " them. So they can't be linked to a skill this way, and are left"
            " out of its subagent cost. The workflows section covers them.",
        ],
    )


def _build_spawn_depth_table(stats: TopologyStats) -> Table:
    columns = [
        # v0.2.0 fix A2: row key must be a str (see recache.py's module
        # docstring) -- the depth itself is still a genuine per-row key
        # (unlike recache_summary's bug), just stringified; the column
        # stays kind="int" since format_cell's "int" formatting accepts
        # a numeric string just as well as a real int.
        Column(key="depth", label="Spawn depth", kind="int"),
        Column(key="count", label="Count", kind="int"),
    ]
    rows = [[str(depth), count] for depth, count in sorted(stats.spawn_depth_histogram.items())]
    mean_spawns_per_session = _mean(stats.spawns_per_session)
    note = f"Sessions seen: {stats.sessions_seen}. Total spawns: {stats.total_spawns}"
    if mean_spawns_per_session is not None:
        note += f". Average spawns per session: {mean_spawns_per_session:.2f}."
    else:
        note += "."
    return Table(
        name="topology_spawn_depth",
        title="Chains: spawn depth histogram",
        columns=columns,
        rows=rows,
        notes=[note],
    )


def _build_cost_per_spawn_table(stats: TopologyStats) -> Table:
    columns = [
        Column(key="agent_type", label="Agent type", kind="str"),
        Column(key="spawns", label="Spawns", kind="int"),
        Column(key="mean_cost", label="Mean cost/spawn", kind="money"),
        Column(key="median_cost", label="Median cost/spawn", kind="money"),
        Column(key="mean_tool_wait", label="Mean tool wait", kind="secs"),
    ]
    rows = [
        [
            agent_type,
            len(values),
            _mean(values),
            _median(values),
            _mean(stats.tool_wait_by_agent_type.get(agent_type, [])),
        ]
        for agent_type, values in sorted(stats.cost_by_agent_type.items())
    ]
    return Table(
        name="topology_cost_per_spawn",
        title="Chains: cost per spawn, by agent type",
        columns=columns,
        rows=rows,
        notes=[
            "Average tool wait is how long that agent type's own tool calls"
            " took to answer, averaged over its replies. It is not the"
            " parent's wait on the whole run.",
        ],
    )


def _build_chains_summary_table(stats: TopologyStats) -> Table:
    columns = [
        Column(key="metric", label="Metric", kind="str"),
        Column(key="value", label="Value", kind="int"),
    ]
    rows = [
        ["Total spawns", stats.total_spawns],
        ["Stopped by user", stats.stopped_by_user_count],
    ]
    return Table(
        name="topology_chains_summary",
        title="Chains: truncation signals",
        columns=columns,
        rows=rows,
        notes=[
            "Claude Code doesn't record when a run stopped because it reached"
            " the agent's turn limit. So only runs stopped by you are counted"
            " here. The quality signals on {{page:agents/quality}} infer likely"
            " turn-limit endings from how a run ends.",
        ],
    )


def _build_reminder_hook_table(stats: TopologyStats) -> Table:
    columns = [
        Column(key="transcript_kind", label="Transcript kind", kind="str"),
        Column(key="transcripts", label="Transcripts", kind="int"),
        Column(key="mean_reminders_per_turn", label="Mean REMINDER/turn", kind="float"),
        Column(key="mean_hook_output_per_turn", label="Mean HOOK_OUTPUT/turn", kind="float"),
    ]
    kinds = sorted(set(stats.reminder_rate_by_kind) | set(stats.hook_rate_by_kind))
    rows = []
    for kind in kinds:
        reminder_values = stats.reminder_rate_by_kind.get(kind, [])
        hook_values = stats.hook_rate_by_kind.get(kind, [])
        rows.append([kind, len(reminder_values), _mean(reminder_values), _mean(hook_values)])
    return Table(
        name="topology_reminder_hook_pressure",
        title="Reminder and hook-output pressure per turn, by transcript kind",
        columns=columns,
        rows=rows,
    )


def _build_cache_signal_table(stats: TopologyStats) -> Table:
    columns = [
        Column(key="subkind", label="CACHE_SIGNAL subkind", kind="str"),
        Column(key="count", label="Count", kind="int"),
    ]
    rows = [
        [subkind, count]
        for subkind, count in sorted(
            stats.cache_signal_subkind_histogram.items(), key=lambda kv: (-kv[1], kv[0])
        )
    ]
    return Table(
        name="topology_cache_signal_histogram",
        title="Cache events, by kind",
        columns=columns,
        rows=rows,
    )


def _build_mcp_cost_table(stats: TopologyStats) -> Table:
    columns = [
        Column(key="mcp_server", label="MCP server", kind="str"),
        Column(key="turns", label="Turns", kind="int"),
        Column(key="cost", label="Cost", kind="money"),
    ]
    rows = [
        [server, stats.mcp_turns_by_server.get(server, 0), cost]
        for server, cost in sorted(stats.mcp_cost_by_server.items(), key=lambda kv: -kv[1])
    ]
    return Table(
        name="topology_mcp_cost",
        title="Cost by attribution_mcp_server",
        columns=columns,
        rows=rows,
    )


def _build_effort_table(stats: TopologyStats, *, by: str) -> Table:
    if by == "effort":
        output_map, thinking_map, turns_map = stats.effort_output, stats.effort_thinking, stats.effort_turns
        name, title = "topology_effort_tokens", "Output/thinking tokens by effort"
    else:
        output_map = stats.per_turn_effort_output
        thinking_map = stats.per_turn_effort_thinking
        turns_map = stats.per_turn_effort_turns
        name, title = "topology_per_turn_effort_tokens", "Output/thinking tokens by per_turn_effort"
    columns = [
        Column(key="effort", label="Effort", kind="str"),
        Column(key="turns", label="Turns", kind="int"),
        Column(key="output_tokens", label="Output tokens", kind="tokens"),
        Column(key="thinking_tokens", label="Thinking tokens", kind="tokens"),
        Column(key="thinking_share", label="Thinking share", kind="pct"),
    ]
    rows = []
    for effort, output in sorted(output_map.items()):
        thinking = thinking_map.get(effort, 0)
        share = 100.0 * thinking / output if output else 0.0
        rows.append([effort, turns_map.get(effort, 0), output, thinking, share])
    return Table(name=name, title=title, columns=columns, rows=rows)


def _build_effort_by_agent_type_table(stats: TopologyStats) -> Table:
    columns = [
        Column(key="agent_type", label="Agent type", kind="str"),
        Column(key="output_tokens", label="Output tokens", kind="tokens"),
        Column(key="thinking_tokens", label="Thinking tokens", kind="tokens"),
        Column(key="thinking_share", label="Thinking share", kind="pct"),
    ]
    rows = []
    for label, output in sorted(stats.agent_type_output.items()):
        thinking = stats.agent_type_thinking.get(label, 0)
        share = 100.0 * thinking / output if output else 0.0
        rows.append([label, output, thinking, share])
    return Table(
        name="topology_effort_by_agent_type",
        title='Output/thinking tokens by agent type (top-level counts as "top-level")',
        columns=columns,
        rows=rows,
    )


def _build_composition_table(stats: TopologyStats) -> Table:
    columns = [
        Column(key="transcript_kind", label="Transcript kind", kind="str"),
        Column(key="transcripts", label="Transcripts", kind="int"),
        Column(key="mean_baseline", label="Mean baseline (write)", kind="tokens"),
        Column(key="mean_tool_result_tokens", label="Mean tool result tokens (approx)", kind="tokens"),
        Column(key="mean_output_tokens", label="Mean assistant output tokens", kind="tokens"),
        Column(key="mean_attachment_tokens", label="Mean attachment tokens (approx)", kind="tokens"),
        Column(key="mean_compactions", label="Mean compaction summaries", kind="float"),
    ]
    rows = []
    for kind, transcripts in sorted(stats.composition_transcripts.items()):
        rows.append(
            [
                kind,
                transcripts,
                _mean(stats.composition_baseline.get(kind, [])),
                _mean(stats.composition_tool_result_tokens.get(kind, [])),
                _mean(stats.composition_output_tokens.get(kind, [])),
                _mean(stats.composition_attachment_tokens.get(kind, [])),
                _mean(stats.composition_compactions.get(kind, [])),
            ]
        )
    return Table(
        name="topology_context_composition",
        title="Context composition per turn, averaged per transcript kind",
        columns=columns,
        rows=rows,
        notes=[
            "Tool result and Claude Code note token counts are approximate:"
            f" characters divided by {_CHARS_PER_TOKEN_APPROX}. No tokenizer"
            " reads your transcripts, to keep them private.",
        ],
    )


def _build_redundant_work_table(stats: TopologyStats) -> Table:
    columns = [
        Column(key="metric", label="Metric", kind="str"),
        Column(key="sessions", label="Sessions", kind="int"),
        Column(key="mean_per_session", label="Mean per session", kind="float"),
        Column(key="total", label="Total", kind="int"),
    ]
    rows = [
        [
            "Repeated identical Bash/PowerShell command prefixes",
            len(stats.redundant_cmd_repeats_per_session),
            _mean(stats.redundant_cmd_repeats_per_session),
            sum(stats.redundant_cmd_repeats_per_session),
        ],
        [
            "Rediscovery reads (Read within 10 turns after a compaction)",
            len(stats.rediscovery_reads_per_session),
            _mean(stats.rediscovery_reads_per_session),
            sum(stats.rediscovery_reads_per_session),
        ],
    ]
    return Table(
        name="topology_redundant_work",
        title="Redundant work: repeated commands and post-compaction rediscovery",
        columns=columns,
        rows=rows,
        notes=[
            "Counted in the main session only. Repeated commands and files"
            " read again after a summary are signs of a long conversation,"
            " which a freshly started subagent doesn't have.",
        ],
    )


def _build_redundant_reads_table(stats: TopologyStats) -> Table:
    columns = [
        Column(key="metric", label="Metric", kind="str"),
        Column(key="sessions", label="Sessions", kind="int"),
        Column(key="mean_per_session", label="Mean per session", kind="float"),
        Column(key="total", label="Total", kind="int"),
    ]
    rows = [
        [
            "Repeated reads (same file read more than once in a session)",
            len(stats.redundant_reads_per_session),
            _mean(stats.redundant_reads_per_session),
            sum(stats.redundant_reads_per_session),
        ],
        [
            f"...within {_REDISCOVERY_WINDOW_TURNS} turns of a compaction",
            len(stats.redundant_reads_after_compaction_per_session),
            _mean(stats.redundant_reads_after_compaction_per_session),
            sum(stats.redundant_reads_after_compaction_per_session),
        ],
    ]
    return Table(
        name="topology_redundant_reads",
        title="Redundant reads: the same file read more than once per session",
        columns=columns,
        rows=rows,
        notes=[
            "Counted in the main session only, from a salted hash of each "
            "file the Read tool opened, never the path itself. A read "
            "after an edit to the same file isn't counted. Every count is "
            "0 when no salt was set up for this run (the CLI sets one up "
            "automatically).",
            "The second row is the part of the first whose repeat read "
            f"came within {_REDISCOVERY_WINDOW_TURNS} replies of a "
            "conversation summary. The redundant work table uses the same "
            "window for files re-read after a summary. Both show context "
            "that a summary dropped being paid for again.",
        ],
    )


def build_section(stats: TopologyStats) -> Section:
    """Turn a finished :class:`TopologyStats` accumulator into the
    "Agents and information flow" report section: one table per plan
    item (a)-(i), each with the notes explaining its proxies and
    approximations.
    """
    tables = [
        _build_spawn_write_table(stats),
        _build_session_baseline_table(stats),
        _build_upward_table(stats),
        _build_report_proxy_table(stats),
        _build_skills_table(stats),
        _build_spawn_depth_table(stats),
        _build_cost_per_spawn_table(stats),
        _build_chains_summary_table(stats),
        _build_reminder_hook_table(stats),
        _build_cache_signal_table(stats),
        _build_mcp_cost_table(stats),
        _build_effort_table(stats, by="effort"),
        _build_effort_table(stats, by="per_turn_effort"),
        _build_effort_by_agent_type_table(stats),
        _build_composition_table(stats),
        _build_redundant_work_table(stats),
        _build_redundant_reads_table(stats),
    ]
    return Section(key="agents", title="Agents and information flow", tables=tables, notes=[])


__all__ = ["TopologyStats", "build_section", "index_tool_use_ids"]

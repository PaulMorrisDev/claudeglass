"""Context budget analytics (S1-context-budget): a clearly labelled
*estimate* of how a session's context window is spent before the model
sees any real work, plus whatever ground truth is available.

Motivation (the owner question this package answers): does this tool
track preloaded skills, the system prompt, and the autocompact buffer?
Claude Code's own ``/context`` view breaks the context window into
system prompt, system tools, MCP tools, custom agents, memory files
(the ``CLAUDE.md`` family), skills, messages, free space and the
autocompact buffer. A transcript never carries those sizes directly --
this module reconstructs an approximation from what IS captured (a
first-turn ``cache_creation`` baseline, HUMAN_TEXT/attachment
``size_chars``, a schema-2 config snapshot's ``content_layers``) and
says, in every column label and table note, that it is an estimate, not
Claude Code's own accounting. Where a genuine measurement exists (the
statusline payload's own ``context_window`` object, once
:mod:`statusline` has logged it -- see that module's docstring), this
module surfaces it as a separate, unlabelled-as-"est" table instead.

Three tables (:func:`build_section`, section key ``"context_budget"``):

- ``context_budget_baseline`` -- per project, plus one "all" row summing
  every project: the measured mean/median top-level first-turn
  ``cache_creation`` (:mod:`topology`'s own "session baseline" metric,
  duplicated here rather than read back off that section's table so this
  module works from ``TranscriptResult`` objects directly, matching the
  rest of this package's convention of small per-module accumulators),
  next to estimated buckets in tokens for human prompt, skills listing,
  memory files, custom agents and MCP tools, then a residual
  "system prompt and tools" bucket (the baseline minus every other known
  bucket, floored at 0).
- ``context_budget_autocompact`` -- per project: the configured
  ``autoCompactWindow`` from the latest schema-2 snapshot (or
  ``CLAUDE_CODE_AUTO_COMPACT_WINDOW``, which overrides it), the model's
  context window size (from a statusline ground-truth row when one is
  available, else an assumed 1,000,000/200,000 split on a ``"[1m]"``
  model alias), the *observed* effective autocompact threshold (median
  ``compactMetadata.preTokens`` over ``trigger == "auto"`` compactions --
  :func:`compaction.effective_autocompact_threshold`), the implied
  buffer, how many auto compactions were observed, and whether the
  observed threshold has drifted more than 10% from the configured
  window.
- ``context_budget_statusline`` -- one real, non-estimated line per
  session, present only once at least one usage-log row carries
  ``context_window`` fields (see :mod:`statusline`'s module docstring for
  how those columns get there). When every session in the report ran in
  the desktop app, which runs no status line, its empty table says so
  (``Table.empty_variant``) and points to the baseline table's
  transcript sizes.

Every chars/bytes-to-tokens conversion in this module uses the same
``chars / 4`` approximation the rest of the codebase already documents
(``topology._CHARS_PER_TOKEN_APPROX``, duplicated here as
:data:`_CHARS_PER_TOKEN_APPROX` per this project's established
"small helper constants are duplicated, not imported across modules"
convention -- see e.g. ``compaction.py``'s and ``usage.py``'s own module
docstrings for the same convention stated explicitly). No tokenizer is
ever run over transcript content.

Accumulator pattern: :class:`ContextBudgetStats` is fed one top-level
session at a time via :meth:`ContextBudgetStats.add_session` (mirroring
``topology.TopologyStats``/``compaction.CompactionStats``), keyed by
project slug; :func:`build_section` turns the finished accumulator (plus
the corpus's config snapshots and, optionally, statusline usage-log
rows) into the report ``Section``. Snapshot and usage-log-row lookups are
deferred to :func:`build_section` rather than threaded through
``add_session`` -- a project's own snapshot doesn't vary per session, and
this keeps the call site in ``report.py`` to exactly one line per
session (see that module's own wiring for this section).

Privacy: nothing here retains message/attachment text or a full path --
only counts, byte/char lengths already present on ``Turn``/``Event``,
and the small numeric/flag fields a schema-2 snapshot already exposes
(``content_layers``, ``mcp_servers.names`` length only, never the names
themselves).
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from statistics import fmean, median
from typing import Sequence

from . import compaction
from . import snapshots as snapshots_mod
from .model import Column, Event, EventKind, Section, Table, TranscriptResult, Turn
from .pricing import Pricing
from .snapshots import Snapshot
from .tools import log_usage

#: No tokenizer is run over transcript content (privacy rule) -- see the
#: module docstring. Duplicated from ``topology._CHARS_PER_TOKEN_APPROX``
#: per this project's small-constant-duplication convention.
_CHARS_PER_TOKEN_APPROX = 4

#: The ``entrypoint`` Claude Code records for the desktop app, which never
#: runs a status line (``hook_health.TERMINAL_ENTRYPOINTS`` names the
#: ones that do), so a window of only these sessions has no readings to
#: log. Duplicated per the convention above.
DESKTOP_ENTRYPOINT = "claude-desktop"

#: ``Table.empty_variant`` of an empty statusline table in such a window:
#: the dashboard's ``grid.js`` keys its own empty sentence on it.
DESKTOP_EMPTY_VARIANT = "desktop"
DESKTOP_EMPTY_NOTE = (
    "Every session in this report ran in the desktop app, which doesn't run status lines, so there is nothing to "
    "log. The baseline table's first-call sizes come from the transcripts instead."
)

#: Per-agent-listing token constant for the ``custom_agents_est`` bucket
#: (a snapshot's ``content_layers.agents_summary.count`` names how many
#: agent frontmatter files load, not their rendered size) -- a rough,
#: labelled-as-such stand-in for "one short frontmatter+description
#: listing costs about this many tokens".
_AGENT_LISTING_TOKENS_PER_AGENT = 60

#: A snapshot's ``autoCompactWindow``/observed-threshold pair counts as
#: "drifted" once they disagree by more than this fraction.
_DRIFT_RATIO = 0.10

#: Assumed model context window when no statusline ground truth is
#: available for a project AND the project's model setting can't be
#: resolved against a rate card (no ``pricing`` given, or no model set)
#: -- see the module docstring. D2/D4/COV-12: this used to be the *only*
#: window this module ever assumed, gated on the model alias literally
#: ending in "[1m]" rather than on which model was actually set --
#: wrong for a bare "sonnet"/"opus"/"fable" alias on a Claude 5 model,
#: which is natively 1M (V24) with no "[1m]" suffix needed. Same literal
#: value as ``pricing._DEFAULT_CONTEXT_WINDOW_TOKENS``, duplicated
#: rather than imported (that name is a private module constant of
#: ``pricing.py``, and this project's convention is to duplicate a small
#: constant like this rather than reach across a module's underscore
#: boundary -- see e.g. ``compaction.py``'s own ``RecacheThresholds``
#: import instead of copying ctx_floor/cr_ratio, which is the opposite
#: choice made for a *shared, evolving* threshold pair; this one is a
#: single frozen number).
_ASSUMED_CONTEXT_WINDOW_DEFAULT = 200_000


# -- small shared helpers (duplicated per this project's convention -----
# see e.g. report.py's/topology.py's own module docstrings) -------------


def _first_priced_turn(result: TranscriptResult) -> Turn | None:
    for turn in result.turns:
        if turn.turn_index == 1:
            return turn
    return None


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _mean(values: Sequence[float]) -> float | None:
    return fmean(values) if values else None


def _median(values: Sequence[float]) -> float | None:
    return median(values) if values else None


def _model_alias_from_snapshot(snapshot: Snapshot | None) -> str | None:
    """The model *setting* (an alias like ``"sonnet"``/``"fable[1m]"``) for
    a project's own config, not the full API model id a transcript's
    ``Turn.model`` carries -- see fix #25: the ``"[1m]"`` suffix only ever
    appears in settings, never in an observed transcript model. Prefers
    schema 2's merged :func:`snapshots.effective_config`, falling back to
    schema 1's raw ``user_settings.model`` (which predates the settings-layer
    merge but already carries the same alias shape)."""
    if snapshot is None:
        return None
    effective_model = snapshots_mod.effective_config(snapshot).get("model")
    if isinstance(effective_model, str) and effective_model:
        return effective_model
    user_settings = snapshot.data.get("user_settings")
    if isinstance(user_settings, dict):
        legacy_model = user_settings.get("model")
        if isinstance(legacy_model, str) and legacy_model:
            return legacy_model
    return None


def _events_before_first_turn(top: TranscriptResult, first_turn: Turn | None) -> list[Event]:
    """Every event in ``top.events`` that precedes ``first_turn`` --
    everything the transcript carries before the first priced turn even
    starts (there is no "previous finalised turn" to bound the window
    from the other side, unlike ``Turn.preceding_event_kinds``, which
    only names *kinds*, not the events themselves or their
    ``size_chars``). Falls back to the whole event list when there is no
    first turn, or its timestamp can't be parsed -- a session-start
    estimate degrading to "count everything" rather than "count nothing"
    on a malformed timestamp.
    """
    if first_turn is None:
        return list(top.events)
    target = _parse_ts(first_turn.ts)
    if target is None:
        return list(top.events)
    result: list[Event] = []
    for event in top.events:
        event_dt = _parse_ts(event.ts)
        if event_dt is None or event_dt < target:
            result.append(event)
    return result


def _human_prompt_est_tokens(first_turn: Turn | None, events_before: list[Event]) -> float:
    """The first HUMAN_TEXT event's own ``size_chars`` found among
    ``events_before``, else ``first_turn.human_prompt_chars`` (already
    the summed length of every human-text line preceding that turn --
    see ``model.py``'s module docstring -- used as a fallback for a
    transcript shape where no matching HUMAN_TEXT event was found, e.g.
    the very first prompt arriving via a non-text content shape this
    module doesn't specifically look for)."""
    for event in events_before:
        if event.kind == EventKind.HUMAN_TEXT and event.size_chars is not None:
            return event.size_chars / _CHARS_PER_TOKEN_APPROX
    if first_turn is not None and first_turn.human_prompt_chars is not None:
        return first_turn.human_prompt_chars / _CHARS_PER_TOKEN_APPROX
    return 0.0


def _skills_listing_est_tokens(events_before: list[Event]) -> float:
    total_chars = sum(
        event.size_chars or 0
        for event in events_before
        if event.kind == EventKind.CONTEXT_INJECT and event.subkind == "skill_listing"
    )
    return total_chars / _CHARS_PER_TOKEN_APPROX


def _memory_files_est_tokens(snapshot: Snapshot | None) -> float | None:
    """CLAUDE.md family bytes + rules bytes, divided by four (bytes
    treated the same as chars for this approximation -- see the module
    docstring). ``None`` when there is no snapshot, or the snapshot
    predates schema 2's ``content_layers`` field."""
    if snapshot is None:
        return None
    content = snapshot.data.get("content_layers")
    if not isinstance(content, dict):
        return None
    claude_md = content.get("claude_md") or {}
    claude_md_bytes = sum(
        value
        for value in (
            claude_md.get("user_bytes"),
            claude_md.get("project_root_bytes"),
            claude_md.get("project_local_bytes"),
            claude_md.get("nested_bytes"),
        )
        if isinstance(value, (int, float))
    )
    rules = content.get("rules") or {}
    rules_bytes = rules.get("bytes")
    total_bytes = claude_md_bytes + (rules_bytes if isinstance(rules_bytes, (int, float)) else 0)
    return total_bytes / _CHARS_PER_TOKEN_APPROX


def _custom_agents_est_tokens(snapshot: Snapshot | None) -> float | None:
    """``content_layers.agents_summary.count`` custom agents, each priced
    at :data:`_AGENT_LISTING_TOKENS_PER_AGENT` tokens (a labelled
    constant, not a measurement -- see the module docstring). ``None``
    without a snapshot (or a schema-1 one, which predates
    ``content_layers``)."""
    if snapshot is None:
        return None
    content = snapshot.data.get("content_layers")
    if not isinstance(content, dict):
        return None
    agents_summary = content.get("agents_summary") or {}
    count = agents_summary.get("count", 0)
    if not isinstance(count, (int, float)):
        count = 0
    return count * _AGENT_LISTING_TOKENS_PER_AGENT


def _mcp_tools_flag(snapshot: Snapshot | None) -> str | None:
    """``"present, size unknown"`` when the snapshot names at least one
    MCP server, else ``None`` -- this module has no way to measure an MCP
    server's own tool-schema size, only whether one is configured at
    all (mirrors ``snapshots.build_config_layers_table``'s own
    ``mcp_servers.names`` reading)."""
    if snapshot is None:
        return None
    mcp_servers = snapshot.data.get("mcp_servers")
    names = (mcp_servers or {}).get("names") if isinstance(mcp_servers, dict) else None
    return "present, size unknown" if names else None


# -- accumulator -----------------------------------------------------------


@dataclass(slots=True)
class _ProjectAcc:
    project: str = ""
    sessions: int = 0
    baseline_writes: list[int] = field(default_factory=list)
    human_prompt_est_tokens: list[float] = field(default_factory=list)
    skills_listing_est_tokens: list[float] = field(default_factory=list)
    compaction_records: list[compaction.CompactionRecord] = field(default_factory=list)
    session_ids: list[str] = field(default_factory=list)
    #: The keys this project's config snapshots can be stored under (see
    #: ``snapshots.snapshot_project_keys``); empty when unknown.
    snapshot_keys: tuple[str, ...] = ()


#: Parts of a subagent's startup context, in display order. Each is
#: measured in tokens (chars / 4) from what the transcript records before
#: the subagent's first priced turn; see :func:`_startup_parts`.
STARTUP_PARTS = (
    "task_prompt",
    "claude_md",
    "skills_listing",
    "tool_lists",
    "hook_context",
    "other_attachments",
    "system_prompt",
    "tool_definitions",
)

#: CACHE_SIGNAL attachment types that list what the agent can call or
#: spawn: the deferred-tool list, MCP server instructions, and the
#: sibling-agent roster.
_TOOL_LIST_SUBKINDS = frozenset({"deferred_tools_delta", "mcp_instructions_delta", "agent_listing_delta"})

#: Tools that only search or read. A subagent that used nothing else
#: counts towards :class:`_AgentStartupAcc`'s ``read_only_spawns``.
_READ_ONLY_TOOLS = frozenset({"Read", "Grep", "Glob", "LS", "WebFetch", "WebSearch", "ToolSearch"})

#: A subagent counts as a fork (it inherited the parent's conversation
#: and prompt cache) when its first turn reads at least this share of the
#: parent's context at the spawning turn from cache, and that context is
#: at least :data:`_FORK_MIN_PARENT_CTX` tokens. A fresh spawn only reads
#: its own system prompt and tools from cache, which is far smaller than
#: a parent conversation of this size.
_FORK_READ_RATIO = 0.9
_FORK_MIN_PARENT_CTX = 40_000

#: A startup part counts as shared when at least half of the measured
#: agent types (and at least two) receive it, and each of their means is
#: within this fraction of the largest.
_SHARED_TOLERANCE = 0.15


@dataclass(slots=True)
class _AgentStartupAcc:
    """Per-agent-type running totals for the subagent startup breakdown."""

    agent_type: str = ""
    spawns: int = 0
    fork_spawns: int = 0
    startup_tokens: list[int] = field(default_factory=list)
    parts: dict[str, list[float]] = field(default_factory=dict)
    #: Spawns whose transcript recorded a system-prompt snapshot, so the
    #: system prompt and tool definitions were measured rather than left
    #: in "not recorded".
    snapshot_spawns: int = 0
    claude_md_by_source: dict[str, list[float]] = field(default_factory=dict)
    skills_listed_spawns: int = 0
    skills_used_spawns: int = 0
    mcp_offered_spawns: int = 0
    mcp_used_spawns: int = 0
    claude_md_spawns: int = 0
    read_only_spawns: int = 0
    #: The first turn's model's 5-minute cache-write list price (USD per
    #: million tokens), per measured spawn whose model is on the rate card.
    write_prices: list[float] = field(default_factory=list)


def _startup_parts(events_before: list[Event], first: Turn | None) -> tuple[dict[str, float], dict[str, float], bool]:
    """Tokens per startup part (:data:`STARTUP_PARTS`), CLAUDE.md tokens
    per source (``User``/``Project``/``Local``/``AutoMem``/``Managed``/
    ``Nested``/``Other``), and whether a system-prompt snapshot was seen.
    """
    chars: dict[str, float] = {part: 0.0 for part in STARTUP_PARTS}
    claude_md_by_source: dict[str, float] = {}
    saw_snapshot = False
    chars["task_prompt"] = _human_prompt_est_tokens(first, events_before) * _CHARS_PER_TOKEN_APPROX
    for event in events_before:
        size = event.size_chars or 0
        if event.kind == EventKind.CONTEXT_INJECT and event.subkind == "prompt_snapshot":
            saw_snapshot = True
            chars["system_prompt"] += event.detail.get("system_chars") or 0
            chars["tool_definitions"] += event.detail.get("tools_chars") or 0
        elif event.kind == EventKind.CONTEXT_INJECT and event.subkind == "instructions":
            chars["claude_md"] += size
            for source, source_chars in (event.detail.get("chars_by_type") or {}).items():
                claude_md_by_source[source] = claude_md_by_source.get(source, 0.0) + source_chars
        elif event.kind == EventKind.CONTEXT_INJECT and event.subkind == "nested_memory":
            chars["claude_md"] += size
            claude_md_by_source["Nested"] = claude_md_by_source.get("Nested", 0.0) + size
        elif event.kind == EventKind.CONTEXT_INJECT and event.subkind == "skill_listing":
            chars["skills_listing"] += size
        elif event.kind == EventKind.CACHE_SIGNAL and event.subkind in _TOOL_LIST_SUBKINDS:
            chars["tool_lists"] += size
        elif event.kind == EventKind.HOOK_OUTPUT:
            chars["hook_context"] += size
        elif event.kind in (EventKind.CONTEXT_INJECT, EventKind.REMINDER, EventKind.CACHE_SIGNAL, EventKind.ATTACHMENT):
            chars["other_attachments"] += size
    tokens = {part: value / _CHARS_PER_TOKEN_APPROX for part, value in chars.items()}
    by_source = {source: value / _CHARS_PER_TOKEN_APPROX for source, value in claude_md_by_source.items()}
    return tokens, by_source, saw_snapshot


@dataclass(slots=True)
class ContextBudgetStats:
    """Corpus-wide context-budget accumulator, fed one top-level session
    at a time via :meth:`add_session`, and one subagent transcript at a
    time via :meth:`add_subagent`. See the module docstring."""

    projects: dict[str, _ProjectAcc] = field(default_factory=dict)
    agents: dict[str, _AgentStartupAcc] = field(default_factory=dict)
    #: Where each main session ran (its transcript's ``entrypoint``, ``""``
    #: when none was recorded) -> sessions, for :attr:`desktop_only`.
    entrypoints: dict[str, int] = field(default_factory=dict)

    @property
    def desktop_only(self) -> bool:
        """Whether every main session here ran in the desktop app, which
        never runs a status line. ``False`` with no session, and with any
        session of another kind or of none recorded: only a clear answer
        is worth a table's own empty sentence."""
        return set(self.entrypoints) == {DESKTOP_ENTRYPOINT}

    def add_subagent(
        self, sub: TranscriptResult, parent_ctx_at_spawn: int | None = None, pricing: "Pricing | None" = None
    ) -> None:
        """Fold one subagent transcript into its agent type's startup
        breakdown: what the transcript records before the first priced
        turn (task prompt, CLAUDE.md, skills listing, tool lists, hook
        output, other attachments and -- when a system-prompt snapshot was
        recorded -- the system prompt and tool definitions), next to the
        first turn's full input size.

        ``parent_ctx_at_spawn`` is the parent's context size on the turn
        that spawned this subagent (``None`` when it can't be joined). A
        subagent whose first turn reads nearly all of that from cache is
        counted as a fork and kept out of the means: it inherited the
        parent's conversation, so its startup size isn't comparable to a
        fresh spawn's.

        ``pricing``, when given, records the first turn's model's
        cache-write price, so a recommendation can put a list price on
        each startup part.
        """
        agent_type = sub.meta.agent_type or "(unknown)"
        acc = self.agents.setdefault(agent_type, _AgentStartupAcc(agent_type=agent_type))
        acc.spawns += 1
        first = _first_priced_turn(sub)
        if first is None:
            return
        if agent_type == "fork" or (
            parent_ctx_at_spawn is not None
            and parent_ctx_at_spawn >= _FORK_MIN_PARENT_CTX
            and first.cache_read_tokens >= _FORK_READ_RATIO * parent_ctx_at_spawn
        ):
            acc.fork_spawns += 1
            return

        events_before = _events_before_first_turn(sub, first)
        parts, claude_md_by_source, saw_snapshot = _startup_parts(events_before, first)
        acc.startup_tokens.append(first.input_tokens + first.cache_creation_tokens + first.cache_read_tokens)
        for part, tokens in parts.items():
            acc.parts.setdefault(part, []).append(tokens)
        for source, tokens in claude_md_by_source.items():
            acc.claude_md_by_source.setdefault(source, []).append(tokens)
        if saw_snapshot:
            acc.snapshot_spawns += 1
        resolved = pricing.resolve_model(first.model) if pricing is not None else None
        if resolved is not None:
            acc.write_prices.append(resolved.rates.cache_write_5m)

        tool_names = {name for turn in sub.turns for name in turn.tool_names}
        if parts["skills_listing"] > 0:
            acc.skills_listed_spawns += 1
            if "Skill" in tool_names:
                acc.skills_used_spawns += 1
        mcp_offered = any(
            event.detail.get("mcp_added")
            for event in events_before
            if event.kind == EventKind.CACHE_SIGNAL and event.subkind == "deferred_tools_delta"
        )
        if mcp_offered:
            acc.mcp_offered_spawns += 1
            if any(turn.attribution_mcp_server for turn in sub.turns) or any(
                name.startswith("mcp__") for name in tool_names
            ):
                acc.mcp_used_spawns += 1
        if parts["claude_md"] > 0:
            acc.claude_md_spawns += 1
            if tool_names <= _READ_ONLY_TOOLS:
                acc.read_only_spawns += 1

    def add_session(self, project: str, top: TranscriptResult, raw_slug: str | None = None) -> None:
        """Fold one session's top-level transcript into ``project``'s
        running totals. Only the top-level transcript is read -- the
        context-budget baseline is specifically about what a session
        pays *before any work happens*, which is a top-level-only
        question (a subagent's own baseline is already covered by
        ``topology``'s downward/spawn-write table).
        """
        acc = self.projects.setdefault(project, _ProjectAcc(project=project))
        if raw_slug and not acc.snapshot_keys:
            acc.snapshot_keys = snapshots_mod.snapshot_project_keys(raw_slug)
        acc.sessions += 1
        entrypoint = top.meta.entrypoint or ""
        self.entrypoints[entrypoint] = self.entrypoints.get(entrypoint, 0) + 1
        if top.meta.session_id:
            acc.session_ids.append(top.meta.session_id)

        first = _first_priced_turn(top)
        if first is not None:
            acc.baseline_writes.append(first.cache_creation_tokens)

        events_before = _events_before_first_turn(top, first)
        acc.human_prompt_est_tokens.append(_human_prompt_est_tokens(first, events_before))
        acc.skills_listing_est_tokens.append(_skills_listing_est_tokens(events_before))

        # rates=None: only trigger/pre_tokens/dropped_tokens are read by
        # this module (all come straight from the COMPACT_BOUNDARY event,
        # never from pricing) -- next_turn_write_cost, the only field an
        # unresolved rate affects, is never read here. See
        # compaction_records_for_transcript's own docstring: an
        # unresolved rate simply leaves that field at 0.0.
        acc.compaction_records.extend(compaction.compaction_records_for_transcript(top, None))


# -- usage-log (statusline) row helpers -------------------------------------
#
# ``statusline.py`` appends nine trailing CSV columns after
# ``log_usage.CSV_FIELDS``'s existing six (three ``context_window_*``
# columns this work package added, plus six ``cache_*`` columns a later
# package added -- see that module's own docstring for the write-side
# contract), for 15 columns total. This module only ever reads the three
# ``context_window_*`` ones, positionally rather than by name (see
# :func:`load_context_window_rows`).


#: Same literal ``statusline._CONTEXT_WINDOW_SENTINEL`` value (the
#: ``window`` column statusline writes on its own context-window rows),
#: duplicated per this project's small-constant convention rather than
#: reaching across that module's underscore boundary. Fix #28: identify a
#: context-window row by this sentinel, the same way
#: ``statusline.load_usage_log_ground_truth`` does, rather than by width
#: alone -- width and sentinel agree today (only
#: ``_append_context_window_row`` writes wide rows), but checking the
#: sentinel too means a future wide row shape can't be silently
#: misread as a context-window row.
_CONTEXT_WINDOW_SENTINEL = "context_window"


def _parse_number(text: str | None) -> float | None:
    if not text:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def load_context_window_rows(csv_path: str | Path) -> list[dict]:
    """Every usage-log CSV row carrying a numeric
    ``context_window_used_tokens`` trailing value (written by
    ``statusline.main``'s context-window append -- see that module's
    docstring), as ``{"session_id", "context_window_used_tokens",
    "context_window_size", "context_window_used_percentage",
    "context_window_cache_read_tokens"}`` dicts, in file order.

    Reads the file positionally via ``csv.reader`` rather than
    ``log_usage.load_usage_log``'s ``csv.DictReader`` (a fixed six-column
    ``log_usage.CSV_FIELDS``): an old-format row (six columns, written
    before this work package existed) simply has nothing at the extra
    positions and is skipped -- there is no context_window data to
    report for it -- rather than raising or misreading a later column as
    an earlier one. A row is also required to carry the
    :data:`_CONTEXT_WINDOW_SENTINEL` value in its ``window`` column (fix
    #28), matching how ``statusline.load_usage_log_ground_truth``
    identifies the same rows, rather than relying on width alone. Returns
    ``[]`` when the file doesn't exist, matching
    ``log_usage.load_usage_log``'s own "the log is optional" contract.
    """
    csv_path = Path(csv_path)
    if not csv_path.exists():
        return []
    rows: list[dict] = []
    with open(csv_path, "r", encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        if header is None:
            return []
        for raw_row in reader:
            if len(raw_row) <= len(log_usage.CSV_FIELDS):
                continue
            if len(raw_row) <= 2 or raw_row[2] != _CONTEXT_WINDOW_SENTINEL:
                continue
            used_tokens = _parse_number(raw_row[6]) if len(raw_row) > 6 else None
            if used_tokens is None:
                continue
            rows.append(
                {
                    "session_id": raw_row[1] if len(raw_row) > 1 else "",
                    "context_window_used_tokens": used_tokens,
                    "context_window_size": _parse_number(raw_row[7]) if len(raw_row) > 7 else None,
                    "context_window_used_percentage": _parse_number(raw_row[3]) if len(raw_row) > 3 else None,
                    "context_window_cache_read_tokens": (
                        _parse_number(raw_row[8]) if len(raw_row) > 8 else None
                    ),
                }
            )
    return rows


def _latest_statusline_window_by_project(
    usage_log_rows: list[dict] | None, session_to_project: dict[str, str]
) -> dict[str, float]:
    result: dict[str, float] = {}
    if not usage_log_rows:
        return result
    for row in usage_log_rows:
        project = session_to_project.get(row.get("session_id"))
        if project is None:
            continue
        size = row.get("context_window_size")
        if isinstance(size, (int, float)) and not isinstance(size, bool):
            result[project] = size  # last one wins -- rows are in file order
    return result


# -- Section/Table assembly --------------------------------------------------


def _baseline_row(
    project: str,
    sessions: int,
    baseline_writes: list[int],
    human_list: list[float],
    skills_list: list[float],
    snapshot: Snapshot | None,
) -> list:
    mean_baseline = _mean(baseline_writes)
    median_baseline = _median(baseline_writes)
    human_est = _mean(human_list) or 0.0
    skills_est = _mean(skills_list) or 0.0
    memory_est = _memory_files_est_tokens(snapshot)
    agents_est = _custom_agents_est_tokens(snapshot)
    mcp_flag = _mcp_tools_flag(snapshot)

    known_total = human_est + skills_est
    if isinstance(memory_est, (int, float)):
        known_total += memory_est
    if isinstance(agents_est, (int, float)):
        known_total += agents_est

    # Floored at 0, but the floor is not silently absorbed: when the
    # chars/4 estimates alone already exceed the measured baseline, that
    # is itself informative (the estimates over-shot), so this reports
    # None rather than a misleading 0.0 -- see fix #24 and the table note
    # below.
    if not isinstance(mean_baseline, (int, float)):
        residual = None
    elif known_total > mean_baseline:
        residual = None
    else:
        residual = mean_baseline - known_total

    return [
        project,
        sessions,
        mean_baseline,
        median_baseline,
        human_est,
        skills_est,
        memory_est,
        agents_est,
        mcp_flag,
        residual,
    ]


def _snapshot_for_project(latest_snapshots: dict[str, Snapshot], acc: _ProjectAcc) -> Snapshot | None:
    """The project's latest snapshot: by its hashed snapshot keys (the
    newest of the drive-letter spellings), falling back to the readable
    slug (older snapshots and hand-built tests)."""
    found = snapshots_mod.latest_for_keys(list(latest_snapshots.values()), acc.snapshot_keys)
    return found if found is not None else latest_snapshots.get(acc.project)


def _build_baseline_table(stats: ContextBudgetStats, latest_snapshots: dict[str, Snapshot]) -> Table:
    columns = [
        Column(key="project", label="Project", kind="str"),
        Column(key="sessions", label="Sessions", kind="int"),
        Column(key="mean_baseline", label="Mean baseline (measured)", kind="tokens"),
        Column(key="median_baseline", label="Median baseline (measured)", kind="tokens"),
        Column(key="human_prompt_est", label="Human prompt (est)", kind="tokens"),
        Column(key="skills_listing_est", label="Skills listing (est)", kind="tokens"),
        Column(key="memory_files_est", label="Memory files (est)", kind="tokens"),
        Column(key="custom_agents_est", label="Custom agents (est)", kind="tokens"),
        Column(key="mcp_tools_est", label="MCP tools (est)", kind="str"),
        Column(key="system_prompt_and_tools_est", label="System prompt and tools (est)", kind="tokens"),
    ]

    all_baseline: list[int] = []
    all_human: list[float] = []
    all_skills: list[float] = []
    all_sessions = 0

    rows: list[list] = []
    for project in sorted(stats.projects):
        acc = stats.projects[project]
        all_baseline.extend(acc.baseline_writes)
        all_human.extend(acc.human_prompt_est_tokens)
        all_skills.extend(acc.skills_listing_est_tokens)
        all_sessions += acc.sessions

        snapshot = _snapshot_for_project(latest_snapshots, acc)
        rows.append(
            _baseline_row(project, acc.sessions, acc.baseline_writes, acc.human_prompt_est_tokens,
                          acc.skills_listing_est_tokens, snapshot)
        )

    rows.insert(0, _baseline_row("all", all_sessions, all_baseline, all_human, all_skills, None))

    return Table(
        name="context_budget_baseline",
        title="Context budget: baseline",
        columns=columns,
        rows=rows,
        notes=[
            "Human prompt, skills listing and memory files (est) count "
            f"characters (or bytes) divided by {_CHARS_PER_TOKEN_APPROX} as "
            "tokens: no tokenizer reads your transcripts. Custom agents (est) "
            f"is the number of agents times {_AGENT_LISTING_TOKENS_PER_AGENT} "
            "tokens. MCP tools (est) says only whether MCP tools are there "
            "(\"present, size unknown\"), not their size. This tool can't "
            "measure an MCP server's own tool definitions. Claude Code's own "
            "/context view is the authoritative breakdown of the context "
            "window. Treat every (est) figure here as a rough guide, never as "
            "exact.",
            "The \"all\" row sums every project's own sessions into one "
            "mean and median baseline. Its memory files, custom agents and "
            "MCP tools are left blank. Those figures come from each project's "
            "own config snapshot, which can't be combined across projects.",
            "\"System prompt and tools (est)\" is what is left over: the "
            "mean baseline minus every other known (est) part, and never "
            "below 0. It also takes in any part that couldn't be estimated "
            "at all (for example, no config snapshot for that project). So a "
            "large figure here does not always mean a large system prompt. "
            "It is left empty rather than 0 when the (est) parts alone "
            "already exceed the measured baseline. That means the estimates "
            "overshot, not that the system prompt is free.",
        ],
    )


def _build_autocompact_table(
    stats: ContextBudgetStats,
    latest_snapshots: dict[str, Snapshot],
    usage_log_rows: list[dict] | None,
    pricing: Pricing | None,
) -> Table:
    columns = [
        Column(key="project", label="Project", kind="str"),
        Column(key="configured_window", label="Configured auto-compact window", kind="tokens"),
        Column(key="context_window_size", label="Model context window", kind="tokens"),
        Column(key="context_window_source", label="Context window source", kind="str"),
        Column(key="observed_threshold", label="Observed effective threshold", kind="tokens"),
        Column(key="implied_buffer", label="Implied buffer", kind="tokens"),
        Column(key="auto_compactions", label="Auto compactions", kind="int"),
        Column(key="drift", label="Drift (>10%)", kind="str"),
    ]

    session_to_project = {
        session_id: project for project, acc in stats.projects.items() for session_id in acc.session_ids
    }
    statusline_window_by_project = _latest_statusline_window_by_project(usage_log_rows, session_to_project)

    rows: list[list] = []
    for project in sorted(stats.projects):
        acc = stats.projects[project]
        snapshot = _snapshot_for_project(latest_snapshots, acc)

        # CLAUDE_CODE_AUTO_COMPACT_WINDOW, when set, beats the setting.
        configured_window = snapshots_mod.auto_compact_window(snapshot)

        window_size = statusline_window_by_project.get(project)
        source = "statusline"
        if window_size is None:
            # Fix #25: the model setting only ever appears in a *setting*
            # (project config), never on an observed transcript model --
            # read it from the project's own snapshot rather than from
            # any session's Turn.model. D2/D4/COV-12: resolve that
            # setting (an alias like "sonnet", or "sonnet[1m]") against
            # the rate card's per-model context_window_tokens, rather
            # than assuming 200k unless the alias literally ends in
            # "[1m]" -- a bare "sonnet"/"opus"/"fable" alias is natively
            # 1M on a Claude 5 model (V24) with no such suffix needed.
            model_alias = _model_alias_from_snapshot(snapshot)
            resolved = pricing.resolve_model(model_alias) if pricing is not None and model_alias else None
            window_size = (
                resolved.rates.context_window_tokens
                if resolved is not None
                else _ASSUMED_CONTEXT_WINDOW_DEFAULT
            )
            source = "assumed"

        observed_threshold = compaction.effective_autocompact_threshold(acc.compaction_records)
        implied_buffer = window_size - observed_threshold if observed_threshold is not None else None
        if (
            implied_buffer is not None
            and implied_buffer < 0
            and source == "assumed"
        ):
            # An assumed window size is a guess; a negative buffer here
            # means the guess was wrong (e.g. a real 1M-context session
            # whose "[1m]" alias this project's snapshot didn't carry),
            # not that Claude Code is actually compacting past its own
            # window -- suppress rather than publish a nonsensical
            # negative buffer.
            implied_buffer = None
        # Counts the same sample effective_autocompact_threshold's median
        # is drawn from (trigger == "auto" AND a usable pre_tokens) --
        # fix #23: this used to count every trigger="auto" boundary
        # regardless of pre_tokens, so it could read non-zero next to a
        # null observed threshold.
        auto_compactions = sum(
            1 for record in acc.compaction_records if record.trigger == "auto" and record.pre_tokens is not None
        )

        drift = None
        if (
            isinstance(configured_window, (int, float))
            and configured_window
            and observed_threshold is not None
        ):
            drift = abs(observed_threshold - configured_window) / configured_window > _DRIFT_RATIO

        rows.append(
            [
                project,
                configured_window,
                window_size,
                source,
                observed_threshold,
                implied_buffer,
                auto_compactions,
                drift,
            ]
        )

    return Table(
        name="context_budget_autocompact",
        title="Context budget: autocompact",
        columns=columns,
        rows=rows,
        notes=[
            "\"Summarised at\" is the typical context size at this "
            "project's own automatic summaries; empty when none recorded "
            "a size. \"Automatic summaries\" counts that same sample, not "
            "every automatic summary, so it is never above zero next to an "
            "empty \"Summarised at\".",
            "\"Context window\" comes from the status line's log when it "
            "has one for a session in this project. Otherwise it is taken "
            "as 1,000,000 tokens for a \"[1m]\" model alias, or 200,000. "
            "\"Window from\" says which.",
            "\"Differs from setting\" is yes when \"Summarised at\" is more "
            "than 10% away from your auto-compact window setting. It is "
            "empty when either figure is missing.",
        ],
    )


def _build_statusline_table(usage_log_rows: list[dict] | None, desktop_only: bool = False) -> Table:
    """The statusline table. ``desktop_only`` (every session ran in the
    desktop app, ``ContextBudgetStats.desktop_only``) changes what an empty
    table says: the app runs no status line, so there is nothing to install
    and the baseline table's transcript sizes are the ones to read."""
    columns = [
        Column(key="session", label="Session", kind="str"),
        Column(key="used_tokens", label="Last used tokens", kind="tokens"),
        Column(key="context_window_size", label="Window size", kind="tokens"),
        Column(key="used_percentage", label="Used %", kind="pct"),
    ]

    by_session: dict[str, dict] = {}
    for row in usage_log_rows or []:
        used_tokens = row.get("context_window_used_tokens")
        if not isinstance(used_tokens, (int, float)) or isinstance(used_tokens, bool):
            continue
        session_id = row.get("session_id") or "(unknown)"
        by_session[session_id] = row  # last one wins -- rows are in file order

    rows = [
        [
            session_id,
            row.get("context_window_used_tokens"),
            row.get("context_window_size")
            if isinstance(row.get("context_window_size"), (int, float))
            else None,
            row.get("context_window_used_percentage")
            if isinstance(row.get("context_window_used_percentage"), (int, float))
            else None,
        ]
        for session_id, row in sorted(by_session.items())
    ]

    notes: list[str] = []
    variant = ""
    if not rows and desktop_only:
        variant = DESKTOP_EMPTY_VARIANT
        notes.append(DESKTOP_EMPTY_NOTE)
    elif not rows:
        notes.append(
            "The status line log has no context sizes yet. Install the "
            "status line logger (claudeglass statusline "
            "--print-install-fragment) to fill this table with the sizes "
            "Claude Code itself reports."
        )
    return Table(
        name="context_budget_statusline",
        title="Context budget: statusline ground truth",
        columns=columns,
        rows=rows,
        notes=notes,
        empty_variant=variant,
    )


def build_section(
    stats: ContextBudgetStats,
    *,
    snapshots: list[Snapshot] | None = None,
    usage_log_rows: list[dict] | None = None,
    pricing: Pricing | None = None,
) -> Section:
    """Build the "Context budget" report section (key
    ``"context_budget"``). See the module docstring for the three tables.

    ``snapshots`` is the same corpus-wide snapshot list every other
    section that reads config takes (``report.py``'s own ``snapshots``
    parameter); this function joins each project to its own *latest*
    snapshot via :func:`snapshots.latest_snapshot_per_project` rather
    than requiring a caller to have done that join already. A Windows
    project's snapshots can sit under two keys (the drive letter's case);
    the newest of the two is the project's latest.

    ``usage_log_rows`` is whatever :func:`load_context_window_rows`
    returns (or an equivalent hand-built list of the same dict shape, as
    every test in this work package uses) -- ``None``/empty is tolerated
    throughout; the autocompact table then falls back to an assumed
    context-window size and the statusline table renders empty with an
    explanatory note.

    ``pricing`` (D2/D4/COV-12), when given, resolves a project's model
    setting against the rate card's per-model ``context_window_tokens``
    for the autocompact table's assumed window, instead of the flat
    200,000-token default every project without statusline ground truth
    used to get regardless of which model it actually set -- same
    optional/degrade-gracefully convention as ``limits.build_section``'s
    own ``pricing`` parameter.

    Skips cleanly (no tables, one note) when ``stats`` has never seen a
    top-level transcript at all -- the same "still return a Section,
    never omit it" convention every other section in this codebase
    follows for its own precondition-not-met case.
    """
    if not stats.projects:
        return Section(
            key="context_budget",
            title="Context budget",
            tables=[],
            notes=[
                "No main sessions in this report, so a context budget "
                "cannot be estimated."
            ],
        )

    latest_snapshots = snapshots_mod.latest_snapshot_per_project(snapshots) if snapshots else {}

    tables = [
        _build_baseline_table(stats, latest_snapshots),
        _build_autocompact_table(stats, latest_snapshots, usage_log_rows, pricing),
        _build_statusline_table(usage_log_rows, stats.desktop_only),
    ]

    return Section(key="context_budget", title="Context budget", tables=tables, notes=[])


# -- subagent startup section ------------------------------------------------


def _mean_or_zero(values: list[float] | None) -> float:
    return fmean(values) if values else 0.0


def _build_startup_table(stats: ContextBudgetStats) -> Table:
    columns = [
        Column(key="agent_type", label="Agent type", kind="str"),
        Column(key="spawns", label="Spawns", kind="int"),
        Column(key="fork_spawns", label="Forks (left out)", kind="int"),
        Column(key="startup_tokens", label="Startup size", kind="tokens"),
        Column(key="task_prompt", label="Task prompt", kind="tokens"),
        Column(key="claude_md", label="CLAUDE.md and memory", kind="tokens"),
        Column(key="skills_listing", label="Skills list", kind="tokens"),
        Column(key="tool_lists", label="Tool and agent lists", kind="tokens"),
        Column(key="hook_context", label="Hook output", kind="tokens"),
        Column(key="other_attachments", label="Environment and other notes", kind="tokens"),
        Column(key="system_prompt", label="System prompt", kind="tokens"),
        Column(key="tool_definitions", label="Tool definitions", kind="tokens"),
        Column(key="not_recorded", label="Not recorded", kind="tokens"),
        Column(key="measured_pct", label="Share explained", kind="pct"),
        Column(key="write_price", label="Cache-write price per million tokens", kind="money"),
        # PROF-11/F13: the share of the "claude_md" column above that is
        # Managed policy CLAUDE.md -- it loads regardless of
        # omitClaudeMd, so a caller pricing what omitClaudeMd would save
        # subtracts this out first (goals._omit_claude_md,
        # whatif._omit_claude_md, recommend.py's spawn-claude-md rule).
        Column(
            key="claude_md_managed",
            label="...of which, Managed policy CLAUDE.md (still loads either way)",
            kind="tokens",
        ),
    ]
    rows: list[list] = []
    for agent_type in sorted(stats.agents, key=lambda key: -stats.agents[key].spawns):
        acc = stats.agents[agent_type]
        if not acc.startup_tokens:
            continue
        startup = fmean(acc.startup_tokens)
        parts = {part: _mean_or_zero(acc.parts.get(part)) for part in STARTUP_PARTS}
        known = sum(parts.values())
        not_recorded = max(0.0, startup - known)
        measured_pct = min(100.0, known / startup * 100) if startup else None
        managed_claude_md = _mean_or_zero(acc.claude_md_by_source.get("Managed"))
        rows.append(
            [agent_type, acc.spawns, acc.fork_spawns, startup]
            + [parts[part] for part in STARTUP_PARTS]
            + [not_recorded, measured_pct, fmean(acc.write_prices) if acc.write_prices else None, managed_claude_md]
        )
    return Table(
        name="agent_startup_breakdown",
        title="What each subagent is given at startup",
        columns=columns,
        rows=rows,
        notes=[
            "Startup size is the first turn's whole input (new, cache-write and cache-read tokens). "
            "Every other column is the average per spawn, in tokens, estimated as characters / "
            f"{_CHARS_PER_TOKEN_APPROX} from what the transcript records before that first turn.",
            "The system prompt and tool definitions are only measured when Claude Code recorded a "
            "system-prompt snapshot for the spawn; otherwise they sit in \"Not recorded\".",
            "Forks inherit the parent's conversation and prompt cache, so they are counted but kept "
            "out of the averages.",
            "\"CLAUDE.md and memory\" includes a managed policy CLAUDE.md (also shown on its own in "
            "the last column). An agent's setting that skips CLAUDE.md files skips only the project's "
            "own ones: a policy CLAUDE.md still loads.",
        ],
    )


def _build_unused_table(stats: ContextBudgetStats) -> Table:
    columns = [
        Column(key="agent_type", label="Agent type", kind="str"),
        Column(key="spawns", label="Spawns measured", kind="int"),
        Column(key="skills_listing_tokens", label="Skills list size", kind="tokens"),
        Column(key="skills_listed_spawns", label="Spawns given the skills list", kind="int"),
        Column(key="skills_used_spawns", label="Spawns that used a skill", kind="int"),
        Column(key="mcp_offered_spawns", label="Spawns offered MCP tools", kind="int"),
        Column(key="mcp_used_spawns", label="Spawns that used an MCP tool", kind="int"),
        Column(key="claude_md_tokens", label="CLAUDE.md size", kind="tokens"),
        Column(key="read_only_spawns", label="Spawns that only searched or read", kind="int"),
    ]
    rows: list[list] = []
    for agent_type in sorted(stats.agents, key=lambda key: -stats.agents[key].spawns):
        acc = stats.agents[agent_type]
        if not acc.startup_tokens:
            continue
        rows.append(
            [
                agent_type,
                len(acc.startup_tokens),
                _mean_or_zero(acc.parts.get("skills_listing")),
                acc.skills_listed_spawns,
                acc.skills_used_spawns,
                acc.mcp_offered_spawns,
                acc.mcp_used_spawns,
                _mean_or_zero(acc.parts.get("claude_md")),
                acc.read_only_spawns,
            ]
        )
    return Table(
        name="agent_startup_unused",
        title="Loaded at startup but not used",
        columns=columns,
        rows=rows,
        notes=[
            "A skill counts as used when the subagent called the Skill tool; an MCP tool counts as "
            "used when any of its turns called one.",
            "\"Only searched or read\" means every tool the subagent called was one of "
            + ", ".join(sorted(_READ_ONLY_TOOLS))
            + ".",
        ],
    )


#: ``instructions.files[].type`` (plus ``Nested`` for nested CLAUDE.md
#: files) -> where that text comes from, for the shared-injection table.
CLAUDE_MD_SOURCES = {
    "User": "Your global CLAUDE.md (~/.claude/CLAUDE.md)",
    "Project": "The project's CLAUDE.md",
    "Local": "The project's CLAUDE.local.md",
    "AutoMem": "Auto memory (MEMORY.md)",
    "Managed": "Managed policy CLAUDE.md",
    "Nested": "CLAUDE.md files in subfolders and rules",
    "Other": "Other instruction files",
}

#: Startup part -> where it comes from, for the shared-injection table.
_PART_SOURCES = {
    "skills_listing": "Installed skills and plugins",
    "tool_lists": "MCP servers, deferred tools and the agent roster",
    "hook_context": "A SessionStart or SubagentStart hook",
    "other_attachments": "Claude Code (environment, model and settings notes)",
    "system_prompt": "Claude Code's system prompt plus the agent's own prompt",
    "tool_definitions": "Built-in and MCP tool definitions",
}


def _is_shared(means: list[float], measured_types: int) -> bool:
    if len(means) < 2 or len(means) * 2 < measured_types:
        return False
    top = max(means)
    return top > 0 and all(abs(top - value) <= _SHARED_TOLERANCE * top for value in means)


def _build_shared_table(stats: ContextBudgetStats) -> Table:
    columns = [
        Column(key="part", label="What", kind="str"),
        Column(key="source", label="Where it comes from", kind="str"),
        Column(key="agent_types", label="Agent types given it", kind="str"),
        Column(key="mean_tokens", label="Size per spawn", kind="tokens"),
        Column(key="total_tokens", label="Total across spawns", kind="tokens"),
    ]
    measured = [acc for acc in stats.agents.values() if acc.startup_tokens]
    rows: list[list] = []
    if len(measured) >= 2:
        candidates: list[tuple[str, str, list[list[float]]]] = [
            (f"claude_md:{source}", CLAUDE_MD_SOURCES.get(source, source), [acc.claude_md_by_source.get(source, []) for acc in measured])
            for source in sorted({source for acc in measured for source in acc.claude_md_by_source})
        ]
        candidates += [
            (part, source, [acc.parts.get(part, []) for acc in measured]) for part, source in _PART_SOURCES.items()
        ]
        for key, source, per_agent in candidates:
            receiving = [values for values in per_agent if values and fmean(values) > 0]
            means = [fmean(values) for values in receiving]
            if not _is_shared(means, len(measured)):
                continue
            total = sum(sum(values) for values in receiving)
            rows.append([key, source, f"{len(receiving)} of {len(measured)}", fmean(means), total])
        rows.sort(key=lambda row: -row[4])
    return Table(
        name="agent_startup_shared",
        title="Given to most subagent types",
        columns=columns,
        rows=rows,
        notes=[
            "A part is listed when at least half of the agent types receive it at about the same "
            f"size (within {int(_SHARED_TOLERANCE * 100)}%). That usually means one shared source. "
            "Trimming that source shrinks every one of those spawns.",
        ],
    )


def build_startup_section(stats: ContextBudgetStats) -> Section:
    """Build the "Subagent startup" report section (key
    ``"agent_startup"``): what each agent type is given before its first
    turn, what it was given but never used, and what every agent type
    receives alike."""
    if not any(acc.startup_tokens for acc in stats.agents.values()):
        return Section(
            key="agent_startup",
            title="Subagent startup",
            tables=[],
            notes=["No subagent transcripts with a priced first turn in this window."],
        )
    return Section(
        key="agent_startup",
        title="Subagent startup",
        tables=[_build_startup_table(stats), _build_unused_table(stats), _build_shared_table(stats)],
        notes=[],
    )


__all__ = [
    "CLAUDE_MD_SOURCES",
    "ContextBudgetStats",
    "STARTUP_PARTS",
    "build_section",
    "build_startup_section",
    "load_context_window_rows",
]

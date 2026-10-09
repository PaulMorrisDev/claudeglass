"""Context budget analytics (S1-context-budget): a clearly labelled
*estimate* of how a session's context window is spent before the model
sees any real work, plus whatever ground truth is available.

Motivation (the owner question this package answers): does this tool
track preloaded skills, the system prompt, and the autocompact buffer?
Claude Code's own ``/context`` view breaks the context window into
system prompt, system tools, MCP tools, custom agents, memory files
(the ``CLAUDE.md`` family), skills, messages, free space and the
autocompact buffer. A transcript never carries those sizes directly --
this module reconstructs an approximation from what IS captured (the
first call's size, HUMAN_TEXT/attachment
``size_chars``, a schema-2 config snapshot's ``content_layers``) and
says, in every column label and table note, that it is an estimate, not
Claude Code's own accounting. Where a genuine measurement exists (the
statusline payload's own ``context_window`` object, once
:mod:`statusline` has logged it -- see that module's docstring), this
module surfaces it as a separate, unlabelled-as-"est" table instead.

Four tables (:func:`build_section`, section key ``"context_budget"``):

- ``context_budget_baseline`` -- per project, plus one "all" row summing
  every project: the measured mean/median top-level first call (P0 =
  uncached input + cache write + cache read, :mod:`topology`'s own
  "session baseline" metric, duplicated here rather than read back off
  that section's table so this module works from ``TranscriptResult``
  objects directly, matching the rest of this package's convention of
  small per-module accumulators), split into the shared prefix the call
  read from cache, what the session wrote itself and the first prompt
  that went in uncached, next to estimated buckets in tokens for human
  prompt, skills listing, memory files, custom agents and MCP tools, then
  a residual "system prompt and tools" bucket (P0 minus every other known
  bucket, floored at 0). The part you can change (skills list, memory
  files, MCP tools of servers you can turn off) is summed in its own
  column, since most of P0 is Claude Code's own tool JSON and the desktop
  app's own servers, which no setting removes.
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
- ``context_budget_calibration`` -- the characters per token measured
  for each model (:mod:`calibration`), which every figure above built from
  characters is divided by.

Every chars/bytes-to-tokens conversion in this module goes through a
:class:`calibration.Calibration` (characters per token measured on your
own first calls, by model family, ``chars / 4`` until a family has ten
of them). No tokenizer is ever run over transcript content. A subagent's
first call is compared across agent types on one model only: the same
tool set is 51.5k tokens on Haiku 4.5 and 69.4k on Sonnet 5, so a mean
over both would measure the mix of models, not the agent.

Subagent transcripts write their tools snapshot, agent roster and MCP
instructions after the first call, not before it. The startup breakdown
therefore takes the first snapshot that lists tools wherever it sits,
and counts the roster and MCP instructions that arrive before the second
call (:func:`_startup_events`). It keeps each built-in tool's definition
size and each MCP server's total (never a description) to find what an
agent type is offered and never uses.

:func:`build_startup_section` (section key ``"agent_startup"``) turns that
into six tables. Two of them price a tools list. ``agent_startup_diet`` is
one row per agent type: the tools to keep (those called in at least
:data:`REMOVABLE_USE_SHARE` of the spawns offered them), the ones to leave
out, and what leaving them out takes from a start: tool definitions, the
deferred names of MCP tools, the skills list (it goes with the Skill tool)
and the agent list (with the Agent tool). MCP server instructions stay
under a tools list, so they are shown apart and not counted.
``agent_startup_servers`` splits the MCP part a server at a time: each
server's tool definitions and deferred names are priced as the tools list
table does, and its instructions are shown apart. :func:`diet_usd` puts
a list price on both: tool definitions are the front of a cached prefix
that sibling spawns share, so only the spawns that wrote it pay the write
price for them; every other part is written by each spawn; each is then
read on every later call. The roster the custom-agents bucket is sized from
is the one the first calls carried, split by agent type count.

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
from . import desktop_servers
from . import snapshots as snapshots_mod
from . import tool_search
from .calibration import Calibration, FirstCall, model_family
from .capture_catalogue import AGENT_ANSWER_TOOLS
from .events import BUILT_IN_TOOLS, tool_server
from .model import Column, Event, EventKind, Section, Table, TranscriptResult, Turn
from .pricing import Pricing
from .snapshots import Snapshot
from .tools import log_usage

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


def _second_priced_turn(result: TranscriptResult) -> Turn | None:
    for turn in result.turns:
        if turn.turn_index == 2:
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


def _human_prompt_chars(first_turn: Turn | None, events_before: list[Event]) -> float:
    """The first HUMAN_TEXT event's own ``size_chars`` found among
    ``events_before``, else ``first_turn.human_prompt_chars`` (already
    the summed length of every human-text line preceding that turn --
    see ``model.py``'s module docstring -- used as a fallback for a
    transcript shape where no matching HUMAN_TEXT event was found, e.g.
    the very first prompt arriving via a non-text content shape this
    module doesn't specifically look for)."""
    for event in events_before:
        if event.kind == EventKind.HUMAN_TEXT and event.size_chars is not None:
            return float(event.size_chars)
    if first_turn is not None and first_turn.human_prompt_chars is not None:
        return float(first_turn.human_prompt_chars)
    return 0.0


def _human_prompt_est_tokens(
    first_turn: Turn | None, events_before: list[Event], calibration: Calibration | None = None
) -> float:
    """:func:`_human_prompt_chars` as tokens on the first turn's model."""
    cal = calibration or Calibration()
    return cal.text_tokens(_human_prompt_chars(first_turn, events_before), first_turn.model if first_turn else None)


def _skills_listing_est_tokens(
    events_before: list[Event], calibration: Calibration | None = None, model: str | None = None
) -> float:
    total_chars = sum(
        event.size_chars or 0
        for event in events_before
        if event.kind == EventKind.CONTEXT_INJECT and event.subkind == "skill_listing"
    )
    return (calibration or Calibration()).text_tokens(total_chars, model)


def _memory_files_est_tokens(snapshot: Snapshot | None, calibration: Calibration | None = None) -> float | None:
    """CLAUDE.md family bytes + rules bytes, as tokens on the corpus's
    most common model (bytes treated the same as chars for this
    approximation -- see the module docstring). ``None`` when there is no
    snapshot, or the snapshot predates schema 2's ``content_layers``
    field."""
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
    return (calibration or Calibration()).text_tokens(total_bytes)


def _custom_agents_est_tokens(snapshot: Snapshot | None, roster: tuple[float, float] | None = None) -> float | None:
    """The share of the sibling-agent roster that lists
    ``content_layers.agents_summary.count`` custom agents. With the roster
    the sessions' first calls carried (``roster``: its tokens and the
    number of agent types it listed), that is its custom share by count of
    types: the parser keeps how many types a roster lists, not the size of
    each line. Without one, each custom agent is priced at
    :data:`_AGENT_LISTING_TOKENS_PER_AGENT` tokens (a labelled constant,
    not a measurement -- see the module docstring). ``None`` without a
    snapshot (or a schema-1 one, which predates ``content_layers``)."""
    if snapshot is None:
        return None
    content = snapshot.data.get("content_layers")
    if not isinstance(content, dict):
        return None
    agents_summary = content.get("agents_summary") or {}
    count = agents_summary.get("count", 0)
    if not isinstance(count, (int, float)):
        count = 0
    if roster is not None and roster[0] > 0 and roster[1] > 0:
        return roster[0] * min(count, roster[1]) / roster[1]
    return count * _AGENT_LISTING_TOKENS_PER_AGENT


def _mcp_tools_tokens(
    top: TranscriptResult, first: Turn | None, calibration: Calibration, skip_built_in: bool = False
) -> float | None:
    """Tokens of the MCP servers' tool definitions sent in full, the
    deferred-tool names listed for them and their instructions in
    ``top``'s first call, summed over its servers. ``None`` when no MCP
    server shows up in it at all. With ``skip_built_in``, the servers built
    into the desktop app (:mod:`desktop_servers`) are left out."""
    if first is None:
        return None
    model = first.model
    servers = (
        set(top.upfront_definition_chars_by_server)
        | set(first.deferred_list_chars_by_server)
        | set(first.mcp_instruction_chars_by_server)
    ) - {BUILT_IN_TOOLS}
    if skip_built_in:
        servers = {server for server in servers if not desktop_servers.is_built_in(server)}
    if not servers:
        return None
    return sum(
        calibration.tool_tokens(top.upfront_definition_chars_by_server.get(server, 0), model)
        + calibration.text_tokens(first.deferred_list_chars_by_server.get(server, 0), model)
        + calibration.text_tokens(first.mcp_instruction_chars_by_server.get(server, 0), model)
        for server in servers
    )


def _mcp_tools_flag(snapshot: Snapshot | None) -> str | None:
    """``"present, size unknown"`` when the snapshot names at least one
    MCP server, else ``None``. A config snapshot says only whether a server
    is configured, never what its tools weigh (mirrors
    ``snapshots.build_config_layers_table``'s own ``mcp_servers.names``
    reading); the size the first call carried is the separate
    ``mcp_tools_tokens`` column."""
    if snapshot is None:
        return None
    mcp_servers = snapshot.data.get("mcp_servers")
    names = (mcp_servers or {}).get("names") if isinstance(mcp_servers, dict) else None
    return "present, size unknown" if names else None


def _mcp_servers_summary(
    servers: Sequence[tool_search.McpServerRow] | None, project: str | None
) -> tuple[str | None, float | None]:
    """The MCP servers ``project``'s sessions were offered (``None``: every
    project), as the section on tool search prices them: a count by what
    can be done about them, and what keeping them cost. Counts and kinds
    only, never a name. ``(None, None)`` when none was offered, so the
    caller falls back to what the config snapshot says."""
    removable = built_in = other = 0
    usd = 0.0
    for row in servers or ():
        if project is None:
            cost = sum(row.usd_by_project.values())
            offered = bool(row.usd_by_project)
        else:
            cost = row.usd_by_project.get(project, 0.0)
            offered = project in row.usd_by_project
        if not offered:
            continue
        usd += cost
        if row.kind == tool_search.KIND_DESKTOP_BUILTIN:
            built_in += 1
        elif tool_search.is_removable_kind(row.kind):
            removable += 1
        else:
            other += 1
    total = removable + built_in + other
    if not total:
        return None, None
    parts = [
        f"{count} {label}"
        for count, label in ((removable, "you can turn off"), (built_in, "built into the desktop app"), (other, "other"))
        if count
    ]
    return f"{total} offered: {', '.join(parts)}", usd


# -- accumulator -----------------------------------------------------------


@dataclass(slots=True)
class _ProjectAcc:
    project: str = ""
    sessions: int = 0
    #: The first call's cache write (what the session wrote itself) and,
    #: beside it, the shared prefix it read from cache, the first prompt
    #: that went in uncached and their sum, P0.
    baseline_writes: list[int] = field(default_factory=list)
    shared_prefix_tokens: list[int] = field(default_factory=list)
    first_prompt_tokens: list[int] = field(default_factory=list)
    first_call_tokens: list[int] = field(default_factory=list)
    human_prompt_est_tokens: list[float] = field(default_factory=list)
    skills_listing_est_tokens: list[float] = field(default_factory=list)
    #: Tokens of the MCP servers' tool definitions, instructions and
    #: deferred-tool names the first call carried, per session.
    mcp_tools_tokens: list[float] = field(default_factory=list)
    #: The same without the servers built into the desktop app, which no
    #: setting removes: the MCP part of what you can change.
    mcp_removable_tokens: list[float] = field(default_factory=list)
    #: Per session, the first sibling-agent roster the first calls carried
    #: (tokens) and how many agent types it listed.
    roster_tokens: list[float] = field(default_factory=list)
    roster_types: list[int] = field(default_factory=list)
    compaction_records: list[compaction.CompactionRecord] = field(default_factory=list)
    session_ids: list[str] = field(default_factory=list)
    #: The keys this project's config snapshots can be stored under (see
    #: ``snapshots.snapshot_project_keys``); empty when unknown.
    snapshot_keys: tuple[str, ...] = ()


#: Parts of a subagent's startup context, in display order. Each is
#: measured in tokens (characters over the calibrated characters per
#: token, :mod:`calibration`) from what the transcript records for the
#: first call; see :func:`_startup_events` and :func:`_startup_chars`.
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

#: The ones of those a subagent's transcript writes after its first call
#: (539 of 539 agent rosters and 1,334 of 1,334 MCP instructions in the
#: author's), so they count as startup when seen before the second.
_LATE_STARTUP_SUBKINDS = frozenset({"mcp_instructions_delta", "agent_listing_delta"})

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

#: A tool an agent type is offered stays on its ``tools:`` list when at
#: least this share of the spawns offered it called it; one called in
#: fewer of them is removable.
REMOVABLE_USE_SHARE = 0.10

#: The tools Claude Code adds to an agent whatever its ``tools:`` says
#: (a workflow agent's answer and a handback), so no allowlist removes
#: them.
_ALWAYS_OFFERED = frozenset(AGENT_ANSWER_TOOLS)

#: Rows of ``agent_startup_tools`` kept per agent type, largest first.
_TOOL_ROWS_PER_AGENT = 12

#: Rows of ``agent_startup_servers`` kept per agent type, largest first.
_SERVER_ROWS_PER_AGENT = 12

#: Tools whose absence from a ``tools:`` list takes a listing out of the
#: agent's startup with it: the skills list goes with Skill, the roster of
#: sibling agents with Agent (named Task in older versions of Claude Code).
SKILLS_TOOL = "Skill"
ROSTER_TOOLS = frozenset({"Agent", "Task"})

#: Agent types the diet leaves alone: Claude Code's own read-only helpers,
#: already started with a small tool set of their own.
DIET_EXCLUDED = frozenset({"Explore", "Plan", "claude-code-guide"})

#: A report window shorter than this many days is stretched to it before a
#: saving is scaled to 30 days, so a short window never reads as a rate.
MIN_WINDOW_DAYS = 7


def mcp_tool_key(server: str) -> str:
    """The name an MCP server's tools go under in a tools list or an
    allowlist (``mcp__<server>__*``)."""
    return f"mcp__{server}__*"


def rarely_used(used: int, offered: int) -> bool:
    """Whether a tool called in ``used`` of the ``offered`` spawns that were
    given it is called in fewer than :data:`REMOVABLE_USE_SHARE` of them.
    The small tolerance keeps an exact tenth (3 of 30) on the list."""
    return used < REMOVABLE_USE_SHARE * offered - 1e-9


@dataclass(slots=True)
class _ToolOffer:
    """One tool, or one MCP server's tools, offered to an agent type on one
    model: spawns offered it, spawns that called it, and the characters of
    its definitions across the spawns offered it."""

    offered: int = 0
    used: int = 0
    chars: int = 0


@dataclass(slots=True)
class _ServerOffer:
    """One MCP server offered to an agent type on one model: the spawns
    that were given any of it, the spawns that called one of its tools, and
    its three costs summed over the spawns offered it, in tokens: tool
    definitions sent in full, the names of its deferred tools, and its
    instructions."""

    offered: int = 0
    used: int = 0
    definitions: float = 0.0
    deferred: float = 0.0
    instructions: float = 0.0


@dataclass(slots=True)
class _AgentStartupAcc:
    """Per-agent-type running totals for the subagent startup breakdown.

    ``startup_tokens``, ``parts``, ``claude_md_by_source`` and the other
    lists beside them have one entry per measured spawn, in the same order,
    so a table can keep the spawns on one model. ``models`` names each
    spawn's model family; an accumulator built by hand without it is read
    as one model."""

    agent_type: str = ""
    spawns: int = 0
    fork_spawns: int = 0
    #: The first call's whole input (P0: uncached input, cache write and
    #: cache read), per measured spawn.
    startup_tokens: list[int] = field(default_factory=list)
    #: ... and the three tokens it is made of: the shared prefix it read
    #: from cache, what the spawn wrote itself and the first prompt that
    #: went in uncached.
    shared_prefix_tokens: list[int] = field(default_factory=list)
    written_tokens: list[int] = field(default_factory=list)
    prompt_tokens: list[int] = field(default_factory=list)
    #: Each measured spawn's model family (:func:`calibration.model_family`).
    models: list[str] = field(default_factory=list)
    parts: dict[str, list[float]] = field(default_factory=dict)
    #: Spawns whose transcript recorded a snapshot that lists tools, so the
    #: tool definitions were measured rather than left in "not recorded".
    snapshot_spawns: int = 0
    claude_md_by_source: dict[str, list[float]] = field(default_factory=dict)
    skills_listed_spawns: int = 0
    skills_used_spawns: int = 0
    mcp_offered_spawns: int = 0
    mcp_used_spawns: int = 0
    claude_md_spawns: int = 0
    read_only_spawns: int = 0
    #: The first turn's model's 5-minute cache-write list price (USD per
    #: million tokens), per measured spawn whose model is on the rate card,
    #: with the model family it came from beside it.
    write_prices: list[float] = field(default_factory=list)
    write_price_models: list[str] = field(default_factory=list)
    #: ... and the same model's cache-read price, beside each write price
    #: (so ``write_price_models`` names the family of both).
    read_prices: list[float] = field(default_factory=list)
    #: Model family -> spawns that recorded their tools, and -> each
    #: tool's offers (a built-in tool by name, an MCP server as
    #: :func:`mcp_tool_key`).
    tool_spawns: dict[str, int] = field(default_factory=dict)
    tools: dict[str, dict[str, _ToolOffer]] = field(default_factory=dict)
    #: Model family -> each tool the spawns called (a built-in tool by name,
    #: an MCP server as :func:`mcp_tool_key`) -> spawns that called it,
    #: whether it was sent in full or loaded when asked for.
    tool_uses: dict[str, dict[str, int]] = field(default_factory=dict)
    #: Model family -> each MCP server's offers, as tokens.
    servers: dict[str, dict[str, _ServerOffer]] = field(default_factory=dict)
    #: Model family -> spawns (of those that recorded their tools) whose
    #: first call wrote the tool definitions to the cache instead of
    #: reading them: nothing of the shared prefix came from cache.
    prefix_written: dict[str, int] = field(default_factory=dict)
    #: Per measured spawn, in step with ``startup_tokens``: the calls after
    #: the first, and the sibling-agent roster sent at startup (tokens).
    later_calls: list[int] = field(default_factory=list)
    roster_tokens: list[float] = field(default_factory=list)

    def fixed_model(self) -> tuple[str, list[int]]:
        """The model family most of this agent type's measured spawns ran
        on, and the positions of the spawns on it. ``("", all)`` for an
        accumulator that doesn't name models. Everything compared across
        agent types is taken from these, so a mix of models is never
        averaged into one startup size."""
        count = len(self.startup_tokens)
        if len(self.models) != count or not any(self.models):
            return "", list(range(count))
        tally: dict[str, int] = {}
        for family in self.models:
            tally[family] = tally.get(family, 0) + 1
        family = max(sorted(tally), key=lambda name: tally[name])
        return family, [index for index, name in enumerate(self.models) if name == family]

    def removable_chars(self, family: str) -> tuple[float, list[tuple[str, int, int, float]]]:
        """Characters per spawn of the tools and MCP servers offered on
        ``family`` that fewer than :data:`REMOVABLE_USE_SHARE` of the spawns
        offered them called (never the tools Claude Code adds whatever the
        list says), and each such tool as ``(key, offered, used, chars per
        spawn offered)``, largest first."""
        spawns = self.tool_spawns.get(family, 0)
        if not spawns:
            return 0.0, []
        rows = [
            (key, offer.offered, offer.used, offer.chars / offer.offered)
            for key, offer in self.tools.get(family, {}).items()
            if offer.offered and key not in _ALWAYS_OFFERED and rarely_used(offer.used, offer.offered)
        ]
        rows.sort(key=lambda row: (-row[3], row[0]))
        total = sum(self.tools[family][key].chars for key, _o, _u, _c in rows) / spawns
        return total, rows

    def kept_tools(self, family: str) -> list[str]:
        """The tools and MCP servers (as :func:`mcp_tool_key`) a ``tools:``
        list on this agent type needs: every one that at least
        :data:`REMOVABLE_USE_SHARE` of the spawns given it called, whether
        it was sent in full or loaded when asked for. Never the tools
        Claude Code adds whatever the list says. Built-in tools first."""
        spawns = self.tool_spawns.get(family, 0)
        offers = self.tools.get(family, {})
        servers = self.servers.get(family, {})
        kept: list[str] = []
        for key, called in self.tool_uses.get(family, {}).items():
            if key in _ALWAYS_OFFERED:
                continue
            if key.startswith("mcp__"):
                offer = servers.get(key[len("mcp__") : -len("__*")])
                offered, used = (offer.offered, offer.used) if offer is not None and offer.offered else (spawns, called)
            else:
                tool = offers.get(key)
                offered, used = (tool.offered, tool.used) if tool is not None and tool.offered else (spawns, called)
            if not rarely_used(used, offered):
                kept.append(key)
        return sorted(kept, key=lambda key: (key.startswith("mcp__"), key))

    def rare_servers(self, family: str) -> list[tuple[str, _ServerOffer]]:
        """The MCP servers offered on ``family`` that fewer than
        :data:`REMOVABLE_USE_SHARE` of the spawns given them called,
        largest first by what they cost at startup (definitions, deferred
        names and instructions)."""
        rows = [
            (server, offer)
            for server, offer in self.servers.get(family, {}).items()
            if offer.offered and rarely_used(offer.used, offer.offered)
        ]
        rows.sort(key=lambda row: (-(row[1].definitions + row[1].deferred + row[1].instructions), row[0]))
        return rows


@dataclass(slots=True)
class _Startup:
    """What one spawn's transcript records for its first call (see
    :func:`_startup_chars`): characters per part, never text."""

    chars: dict[str, float]
    claude_md_by_source: dict[str, float]
    #: A snapshot that lists tools was recorded (so tool definitions are
    #: measured, not left unrecorded).
    saw_tools: bool = False
    #: Each built-in tool's definition size by name, and each MCP server's
    #: total, from that snapshot.
    tool_chars: dict[str, int] = field(default_factory=dict)
    server_chars: dict[str, int] = field(default_factory=dict)


def _startup_events(sub: TranscriptResult, first: Turn, second: Turn | None) -> list[Event]:
    """The events that make up ``sub``'s first call. A subagent writes its
    tools snapshot, agent roster and MCP instructions after that call, so
    besides everything before it (:func:`_events_before_first_turn`) this
    takes the roster and MCP instructions that arrive before the second
    call, and the first snapshot that lists tools wherever it sits. Later
    snapshots that list tools are not startup, and a header-only snapshot
    adds nothing."""
    events = _events_before_first_turn(sub, first)
    taken = {id(event) for event in events}
    have_tools = any(
        event.kind == EventKind.CONTEXT_INJECT and event.subkind == "prompt_snapshot" and event.detail.get("tools_chars")
        for event in events
    )
    stop = _parse_ts(second.ts) if second is not None else None
    for event in sub.events:
        if id(event) in taken:
            continue
        if event.kind == EventKind.CONTEXT_INJECT and event.subkind == "prompt_snapshot":
            if event.detail.get("tools_chars") and not have_tools:
                events.append(event)
                have_tools = True
        elif event.kind == EventKind.CACHE_SIGNAL and event.subkind in _LATE_STARTUP_SUBKINDS:
            when = _parse_ts(event.ts)
            if stop is None or (when is not None and when < stop):
                events.append(event)
    return events


def _startup_chars(events: list[Event], first: Turn | None) -> _Startup:
    """Characters per startup part (:data:`STARTUP_PARTS`) and CLAUDE.md
    characters per source (``User``/``Project``/``Local``/``AutoMem``/
    ``Managed``/``Nested``/``Other``) in ``events``, which
    :func:`_startup_events` picked. The system prompt is the snapshots'
    own, counted once (every snapshot repeats it); the tool definitions
    are those of the first snapshot that lists tools."""
    found = _Startup(chars={part: 0.0 for part in STARTUP_PARTS}, claude_md_by_source={})
    chars = found.chars
    claude_md_by_source = found.claude_md_by_source
    saw_system = False
    chars["task_prompt"] = _human_prompt_chars(first, events)
    for event in events:
        size = event.size_chars or 0
        if event.kind == EventKind.CONTEXT_INJECT and event.subkind == "prompt_snapshot":
            if not saw_system and event.detail.get("system_chars"):
                saw_system = True
                chars["system_prompt"] += event.detail["system_chars"]
            if event.detail.get("tools_chars") and not found.saw_tools:
                found.saw_tools = True
                chars["tool_definitions"] += event.detail["tools_chars"]
                found.tool_chars = dict(event.detail.get("tool_chars") or {})
                found.server_chars = dict(event.detail.get("server_chars") or {})
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
    return found


def _startup_tokens(
    startup: _Startup, calibration: Calibration, model: str | None
) -> tuple[dict[str, float], dict[str, float]]:
    """:func:`_startup_chars` as tokens on ``model``: the tool definitions
    at the model's tool-definition rate, everything else at its text rate.
    Returns the tokens per part and the CLAUDE.md tokens per source."""
    tokens = {
        part: (
            calibration.tool_tokens(value, model)
            if part == "tool_definitions"
            else calibration.text_tokens(value, model)
        )
        for part, value in startup.chars.items()
    }
    by_source = {source: calibration.text_tokens(value, model) for source, value in startup.claude_md_by_source.items()}
    return tokens, by_source


def first_call(sub: TranscriptResult) -> FirstCall | None:
    """``sub``'s first call as the numbers :mod:`calibration` needs:
    its model, the characters of its tool definitions and of every other
    recorded startup part, and its cache-read and other input tokens.
    ``None`` for a fork (its cache read is the parent's conversation, not
    a tool prefix) and for a transcript with no priced call."""
    first = _first_priced_turn(sub)
    if first is None or sub.meta.agent_type == "fork":
        return None
    startup = _startup_chars(_startup_events(sub, first, _second_priced_turn(sub)), first)
    return FirstCall(
        model=first.model or "",
        tool_chars=int(startup.chars["tool_definitions"]),
        text_chars=int(sum(value for part, value in startup.chars.items() if part != "tool_definitions")),
        shared_prefix_tokens=first.cache_read_tokens,
        own_tokens=first.input_tokens + first.cache_creation_tokens,
    )


@dataclass(slots=True)
class StartupSizes:
    """What one transcript's first call carried, in approximate tokens, by
    the part a setting or the harness decides (:func:`startup_sizes`).
    Sizes only: no text, no description, and no name beyond the built-in
    tools' and the MCP servers'."""

    model: str | None = None
    family: str = ""
    #: A snapshot that lists tools was recorded, so the tool sizes below
    #: are measured rather than missing.
    saw_tools: bool = False
    system: float = 0.0
    #: Each built-in tool's definition, by name.
    builtin_tools: dict[str, float] = field(default_factory=dict)
    #: Per MCP server: its tool definitions sent in full, the names of its
    #: deferred tools, and its instructions.
    server_definitions: dict[str, float] = field(default_factory=dict)
    server_deferred: dict[str, float] = field(default_factory=dict)
    server_instructions: dict[str, float] = field(default_factory=dict)
    skills: float = 0.0
    #: CLAUDE.md files, rules and nested memory (not auto memory).
    claude_md: float = 0.0
    #: Auto memory (``AutoMem``), a CLAUDE.md source of its own.
    memory: float = 0.0
    hooks: float = 0.0
    #: The sibling-agent roster Claude Code lists.
    agent_roster: float = 0.0

    def total(self) -> float:
        return (
            self.system
            + sum(self.builtin_tools.values())
            + sum(self.server_definitions.values())
            + sum(self.server_deferred.values())
            + sum(self.server_instructions.values())
            + self.skills
            + self.claude_md
            + self.memory
            + self.hooks
            + self.agent_roster
        )


def startup_sizes(result: TranscriptResult, calibration: Calibration) -> StartupSizes | None:
    """The first call's startup parts for any transcript (a main session
    or an agent), in tokens at ``calibration``'s characters per token.
    Built from the same events as the subagent startup breakdown
    (:func:`_startup_events`), so a snapshot or roster written after the
    first call still counts. The MCP deferred names and instructions are
    the larger of the first two calls' sizes: an agent's instructions can
    arrive between them. ``None`` for a transcript with no priced call."""
    first = _first_priced_turn(result)
    if first is None:
        return None
    second = _second_priced_turn(result)
    events = _startup_events(result, first, second)
    return _sizes_of(result, first, second, events, _startup_chars(events, first), calibration)


def _sizes_of(
    result: TranscriptResult,
    first: Turn,
    second: Turn | None,
    events: list[Event],
    startup: _Startup,
    calibration: Calibration,
) -> StartupSizes:
    """:func:`startup_sizes` for a transcript whose startup events and
    characters are already worked out."""
    model = first.model
    parts, by_source = _startup_tokens(startup, calibration, model)
    sizes = StartupSizes(model=model, family=model_family(model), saw_tools=startup.saw_tools)
    sizes.system = parts["system_prompt"]
    if startup.saw_tools:
        sizes.builtin_tools = {name: calibration.tool_tokens(chars, model) for name, chars in startup.tool_chars.items()}
        sizes.server_definitions = {
            server: calibration.tool_tokens(chars, model) for server, chars in startup.server_chars.items()
        }
    else:
        sizes.server_definitions = {
            server: calibration.tool_tokens(chars, model)
            for server, chars in result.upfront_definition_chars_by_server.items()
            if server != BUILT_IN_TOOLS
        }
    for turn in (first, second):
        if turn is None:
            continue
        for server, chars in turn.deferred_list_chars_by_server.items():
            if server != BUILT_IN_TOOLS:
                sizes.server_deferred[server] = max(sizes.server_deferred.get(server, 0.0), calibration.text_tokens(chars, model))
        for server, chars in turn.mcp_instruction_chars_by_server.items():
            if server != BUILT_IN_TOOLS:
                sizes.server_instructions[server] = max(
                    sizes.server_instructions.get(server, 0.0), calibration.text_tokens(chars, model)
                )
    roster_chars = sum(
        event.size_chars or 0
        for event in events
        if event.kind == EventKind.CACHE_SIGNAL and event.subkind == "agent_listing_delta"
    )
    sizes.agent_roster = calibration.text_tokens(roster_chars, model)
    sizes.skills = parts["skills_listing"]
    sizes.hooks = parts["hook_context"]
    sizes.memory = by_source.get("AutoMem", 0.0)
    sizes.claude_md = max(0.0, parts["claude_md"] - sizes.memory)
    return sizes


@dataclass(slots=True)
class ContextBudgetStats:
    """Corpus-wide context-budget accumulator, fed one top-level session
    at a time via :meth:`add_session`, and one subagent transcript at a
    time via :meth:`add_subagent`. See the module docstring."""

    projects: dict[str, _ProjectAcc] = field(default_factory=dict)
    agents: dict[str, _AgentStartupAcc] = field(default_factory=dict)
    #: Characters per token by model family, measured on the corpus's own
    #: first calls (``report.py`` sets it before any session is added).
    calibration: Calibration = field(default_factory=Calibration)
    #: Where each main session ran (its transcript's ``entrypoint``, ``""``
    #: when none was recorded) -> sessions, for :attr:`desktop_only`.
    entrypoints: dict[str, int] = field(default_factory=dict)

    #: Epoch seconds of the oldest and newest first call seen, for
    #: :attr:`window_days`.
    first_when: float | None = None
    last_when: float | None = None

    def _note_when(self, ts: str | None) -> None:
        when = _parse_ts(ts)
        if when is None:
            return
        seconds = when.timestamp()
        self.first_when = seconds if self.first_when is None else min(self.first_when, seconds)
        self.last_when = seconds if self.last_when is None else max(self.last_when, seconds)

    @property
    def window_days(self) -> float:
        """Days from the oldest to the newest first call seen, at least
        :data:`MIN_WINDOW_DAYS`: what an amount over this window is scaled
        by to read as an amount over 30 days."""
        if self.first_when is None or self.last_when is None:
            return float(MIN_WINDOW_DAYS)
        return max(float(MIN_WINDOW_DAYS), (self.last_when - self.first_when) / 86400.0)

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
        breakdown: what the transcript records for the first priced call
        (task prompt, CLAUDE.md, skills listing, tool lists, hook output,
        other attachments, the system prompt and the tool definitions --
        see :func:`_startup_events` for why some of those sit after the
        call), next to the call's full input size (P0) and the three
        parts of it: the shared prefix it read from cache, what the spawn
        wrote itself and the first prompt that went in uncached.

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

        second = _second_priced_turn(sub)
        events = _startup_events(sub, first, second)
        startup = _startup_chars(events, first)
        family = model_family(first.model)
        parts, claude_md_by_source = _startup_tokens(startup, self.calibration, first.model)
        sizes = _sizes_of(sub, first, second, events, startup, self.calibration)
        self._note_when(first.ts)
        measured = len(acc.startup_tokens)
        acc.startup_tokens.append(first.input_tokens + first.cache_creation_tokens + first.cache_read_tokens)
        acc.shared_prefix_tokens.append(first.cache_read_tokens)
        acc.written_tokens.append(first.cache_creation_tokens)
        acc.prompt_tokens.append(first.input_tokens)
        acc.models.append(family)
        acc.later_calls.append(sum(1 for turn in sub.turns if turn.turn_index > 1))
        acc.roster_tokens.append(sizes.agent_roster)
        for part, tokens in parts.items():
            acc.parts.setdefault(part, []).append(tokens)
        # One entry per spawn for every source seen so far, so the lists
        # line up with ``startup_tokens`` (a spawn without a source is 0).
        for source in set(acc.claude_md_by_source) | set(claude_md_by_source):
            values = acc.claude_md_by_source.setdefault(source, [0.0] * measured)
            values.append(claude_md_by_source.get(source, 0.0))
        if startup.saw_tools:
            acc.snapshot_spawns += 1
        resolved = pricing.resolve_model(first.model) if pricing is not None else None
        if resolved is not None:
            acc.write_prices.append(resolved.rates.cache_write_5m)
            acc.read_prices.append(resolved.rates.cache_read)
            acc.write_price_models.append(family)

        tool_names = {name for turn in sub.turns for name in turn.tool_names}
        if startup.saw_tools:
            acc.tool_spawns[family] = acc.tool_spawns.get(family, 0) + 1
            # The tool definitions are the front of the cached prefix: a
            # spawn that read less than half of them from cache wrote them.
            if first.cache_read_tokens < 0.5 * parts["tool_definitions"]:
                acc.prefix_written[family] = acc.prefix_written.get(family, 0) + 1
            offers = acc.tools.setdefault(family, {})
            for name, chars in startup.tool_chars.items():
                offer = offers.setdefault(name, _ToolOffer())
                offer.offered += 1
                offer.chars += chars
                offer.used += name in tool_names
            for server, chars in startup.server_chars.items():
                offer = offers.setdefault(mcp_tool_key(server), _ToolOffer())
                offer.offered += 1
                offer.chars += chars
                offer.used += any(tool_server(name) == server for name in tool_names)
            uses = acc.tool_uses.setdefault(family, {})
            for key in {
                name if tool_server(name) == BUILT_IN_TOOLS else mcp_tool_key(tool_server(name)) for name in tool_names
            }:
                uses[key] = uses.get(key, 0) + 1
            offered_servers = (
                set(sizes.server_definitions) | set(sizes.server_deferred) | set(sizes.server_instructions)
            ) - {BUILT_IN_TOOLS}
            server_offers = acc.servers.setdefault(family, {})
            for server in offered_servers:
                server_offer = server_offers.setdefault(server, _ServerOffer())
                server_offer.offered += 1
                server_offer.definitions += sizes.server_definitions.get(server, 0.0)
                server_offer.deferred += sizes.server_deferred.get(server, 0.0)
                server_offer.instructions += sizes.server_instructions.get(server, 0.0)
                server_offer.used += any(tool_server(name) == server for name in tool_names)
        if parts["skills_listing"] > 0:
            acc.skills_listed_spawns += 1
            if "Skill" in tool_names:
                acc.skills_used_spawns += 1
        mcp_offered = any(
            event.detail.get("mcp_added")
            for event in events
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
            self._note_when(first.ts)
            acc.baseline_writes.append(first.cache_creation_tokens)
            acc.shared_prefix_tokens.append(first.cache_read_tokens)
            acc.first_prompt_tokens.append(first.input_tokens)
            acc.first_call_tokens.append(first.input_tokens + first.cache_creation_tokens + first.cache_read_tokens)

        events_before = _events_before_first_turn(top, first)
        model = first.model if first is not None else None
        acc.human_prompt_est_tokens.append(_human_prompt_est_tokens(first, events_before, self.calibration))
        acc.skills_listing_est_tokens.append(_skills_listing_est_tokens(events_before, self.calibration, model))
        mcp_tokens = _mcp_tools_tokens(top, first, self.calibration)
        if mcp_tokens is not None:
            acc.mcp_tools_tokens.append(mcp_tokens)
            acc.mcp_removable_tokens.append(_mcp_tools_tokens(top, first, self.calibration, skip_built_in=True) or 0.0)
        if first is not None:
            roster = next(
                (
                    event
                    for event in _startup_events(top, first, _second_priced_turn(top))
                    if event.kind == EventKind.CACHE_SIGNAL and event.subkind == "agent_listing_delta"
                ),
                None,
            )
            if roster is not None and (roster.size_chars or 0) > 0:
                acc.roster_tokens.append(self.calibration.text_tokens(roster.size_chars or 0, model))
                acc.roster_types.append(int(roster.detail.get("added") or 0))

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
    acc: _ProjectAcc,
    snapshot: Snapshot | None,
    calibration: Calibration,
    servers: Sequence[tool_search.McpServerRow] | None = None,
) -> list:
    mean_baseline = _mean(acc.first_call_tokens)
    median_baseline = _median(acc.first_call_tokens)
    human_est = _mean(acc.human_prompt_est_tokens) or 0.0
    skills_est = _mean(acc.skills_listing_est_tokens) or 0.0
    memory_est = _memory_files_est_tokens(snapshot, calibration)
    roster = (_mean(acc.roster_tokens) or 0.0, _mean(acc.roster_types) or 0.0) if acc.roster_tokens else None
    agents_est = _custom_agents_est_tokens(snapshot, roster)
    # The servers the sessions were offered, priced as tool search prices
    # them; with none known, whether the config snapshot names any.
    mcp_flag, mcp_usd = _mcp_servers_summary(servers, None if project == "all" else project)
    if mcp_flag is None:
        mcp_flag = _mcp_tools_flag(snapshot)
    mcp_tokens = _mean(acc.mcp_tools_tokens)
    mcp_removable = _mean(acc.mcp_removable_tokens)

    known_total = human_est + skills_est
    if isinstance(memory_est, (int, float)):
        known_total += memory_est
    if isinstance(agents_est, (int, float)):
        known_total += agents_est
    if mcp_tokens is not None:
        known_total += mcp_tokens

    # What you can change: the skills list, the memory files and the MCP
    # tools of servers you can turn off. The rest of the first call is
    # Claude Code's own tool JSON and system prompt and the desktop app's
    # own servers, which no setting removes.
    controllable = skills_est + (memory_est or 0.0) + (mcp_removable or 0.0) if acc.first_call_tokens else None

    # Floored at 0, but the floor is not silently absorbed: when the
    # estimates alone already exceed the measured first call, that is
    # itself informative (the estimates over-shot), so this reports
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
        acc.sessions,
        mean_baseline,
        median_baseline,
        _mean(acc.shared_prefix_tokens),
        _mean(acc.baseline_writes),
        _mean(acc.first_prompt_tokens),
        human_est,
        skills_est,
        memory_est,
        agents_est,
        mcp_flag,
        mcp_tokens,
        mcp_removable,
        mcp_usd,
        controllable,
        residual,
    ]


def _snapshot_for_project(latest_snapshots: dict[str, Snapshot], acc: _ProjectAcc) -> Snapshot | None:
    """The project's latest snapshot: by its hashed snapshot keys (the
    newest of the drive-letter spellings), falling back to the readable
    slug (older snapshots and hand-built tests)."""
    found = snapshots_mod.latest_for_keys(list(latest_snapshots.values()), acc.snapshot_keys)
    return found if found is not None else latest_snapshots.get(acc.project)


def _build_baseline_table(
    stats: ContextBudgetStats,
    latest_snapshots: dict[str, Snapshot],
    calibration: Calibration | None = None,
    servers: Sequence[tool_search.McpServerRow] | None = None,
) -> Table:
    calibration = calibration or stats.calibration
    columns = [
        Column(key="project", label="Project", kind="str"),
        Column(key="sessions", label="Sessions", kind="int"),
        Column(key="mean_baseline", label="Mean first call (measured)", kind="tokens"),
        Column(key="median_baseline", label="Median first call (measured)", kind="tokens"),
        Column(key="shared_prefix", label="Shared prefix (measured)", kind="tokens"),
        Column(key="session_written", label="Written by the session (measured)", kind="tokens"),
        Column(key="first_prompt", label="First prompt (measured)", kind="tokens"),
        Column(key="human_prompt_est", label="Human prompt (est)", kind="tokens"),
        Column(key="skills_listing_est", label="Skills listing (est)", kind="tokens"),
        Column(key="memory_files_est", label="Memory files (est)", kind="tokens"),
        Column(key="custom_agents_est", label="Custom agents (est)", kind="tokens"),
        Column(key="mcp_tools_est", label="MCP tools (est)", kind="str"),
        Column(key="mcp_tools_tokens", label="MCP tools in the first call (est)", kind="tokens"),
        Column(key="mcp_removable_tokens", label="MCP tools you can turn off (est)", kind="tokens"),
        Column(key="mcp_servers_usd", label="Cost of keeping the MCP servers offered (est)", kind="money"),
        Column(key="controllable_est", label="What you can change (est)", kind="tokens"),
        Column(key="system_prompt_and_tools_est", label="System prompt and tools (est)", kind="tokens"),
    ]

    everything = _ProjectAcc(project="all")

    rows: list[list] = []
    for project in sorted(stats.projects):
        acc = stats.projects[project]
        everything.sessions += acc.sessions
        for name in (
            "baseline_writes",
            "shared_prefix_tokens",
            "first_prompt_tokens",
            "first_call_tokens",
            "human_prompt_est_tokens",
            "skills_listing_est_tokens",
            "mcp_tools_tokens",
            "mcp_removable_tokens",
        ):
            getattr(everything, name).extend(getattr(acc, name))

        snapshot = _snapshot_for_project(latest_snapshots, acc)
        rows.append(_baseline_row(project, acc, snapshot, calibration, servers))

    rows.insert(0, _baseline_row("all", everything, None, calibration, servers))

    return Table(
        name="context_budget_baseline",
        title="Context budget: baseline",
        columns=columns,
        rows=rows,
        notes=[
            "The first call is everything the first reply read: new input, cache writes and cache reads. "
            "It splits into the shared prefix read from cache (mostly Claude Code's own tool definitions), "
            "what the session wrote itself, and the first prompt that went in uncached. "
            "The same tools measure differently on each model, so compare projects that ran on one model.",
            "Human prompt, skills listing, memory files and MCP tools (est) count characters (or bytes) "
            f"divided by characters per token, {calibration.basis()}: no tokenizer reads your transcripts. "
            "Custom agents (est) is your custom agents' share of the agent list the first calls carried. "
            f"With no list recorded, it is {_AGENT_LISTING_TOKENS_PER_AGENT} tokens per agent. "
            "\"MCP tools\" counts the MCP servers the sessions were offered, by what can be done "
            "about them, with what keeping them cost, from the same per-server prices as the tool search "
            "section. A server built into the desktop app is shown with its cost and has nothing to remove. "
            "With none offered it says only whether MCP servers are configured. "
            "\"MCP tools in the first call\" sizes the definitions, "
            "deferred-tool names and instructions that call carried. Claude Code's own "
            "/context view is the authoritative breakdown of the context "
            "window. Treat every (est) figure here as a rough guide, never as "
            "exact.",
            "The \"all\" row sums every project's own sessions into one "
            "mean and median. Its memory files and custom agents "
            "are left blank. Those figures come from each project's "
            "own config snapshot, which can't be combined across projects.",
            "\"What you can change\" adds the skills listing, memory files and the MCP tools of servers you can turn off. "
            "The rest is mostly Claude Code's own tool definitions and system prompt and the desktop app's own servers, "
            "which no setting removes.",
            "\"System prompt and tools (est)\" is what is left over: the "
            "mean first call minus every other known (est) part, and never "
            "below 0. It also takes in any part that couldn't be estimated "
            "at all (for example, no config snapshot for that project). So a "
            "large figure here does not always mean a large system prompt. "
            "It is left empty rather than 0 when the (est) parts alone "
            "already exceed the measured first call. That means the estimates "
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
    mcp_servers: Sequence[tool_search.McpServerRow] | None = None,
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

    ``mcp_servers`` is ``tool_search``'s per-server rows
    (``ToolSearchStats.mcp_servers``): the baseline table's MCP column
    reuses their cost and kinds rather than judging servers from config
    names, which the desktop app's own and connector servers never appear
    in.
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
        _build_baseline_table(stats, latest_snapshots, servers=mcp_servers),
        _build_autocompact_table(stats, latest_snapshots, usage_log_rows, pricing),
        _build_statusline_table(usage_log_rows, stats.desktop_only),
        _build_calibration_table(stats.calibration),
    ]

    return Section(key="context_budget", title="Context budget", tables=tables, notes=[])


def _build_calibration_table(calibration: Calibration) -> Table:
    columns = [
        Column(key="model", label="Model", kind="str"),
        Column(key="tool_chars_per_token", label="Tool definitions (characters per token)", kind="float"),
        Column(key="text_chars_per_token", label="Other text (characters per token)", kind="float"),
    ]
    return Table(
        name="context_budget_calibration",
        title="Characters per token",
        columns=columns,
        rows=calibration.rows(),
        notes=[
            f"Every token figure built from characters in this report is {calibration.basis()}. "
            "A model gets its own figure once it has ten first calls to measure; until then 4.0 stands in.",
        ],
    )


# -- subagent startup section ------------------------------------------------


def _mean_or_zero(values: list[float] | None) -> float:
    return fmean(values) if values else 0.0


def _picked(values: list, positions: list[int], count: int) -> list:
    """``values``' entries at ``positions``, when it has one per measured
    spawn (``count``); a list built some other way is used whole."""
    return [values[i] for i in positions] if len(values) == count else values


def _fixed_write_prices(acc: _AgentStartupAcc, family: str) -> list[float]:
    if family and len(acc.write_price_models) == len(acc.write_prices):
        return [price for price, model in zip(acc.write_prices, acc.write_price_models) if model == family]
    return acc.write_prices


def _fixed_read_prices(acc: _AgentStartupAcc, family: str) -> list[float]:
    """The cache-read prices of the spawns on ``family``, in step with
    :func:`_fixed_write_prices` (empty for an accumulator without them)."""
    if len(acc.read_prices) != len(acc.write_prices):
        return []
    if family and len(acc.write_price_models) == len(acc.write_prices):
        return [price for price, model in zip(acc.read_prices, acc.write_price_models) if model == family]
    return acc.read_prices


def diet_usd(
    spawns: float,
    prefix_tokens: float,
    body_tokens: float,
    *,
    write: float | None,
    read: float | None,
    later_calls: float,
    written_share: float,
) -> float:
    """List-price USD of leaving tokens out of the start of ``spawns``
    spawns of one agent type, with prices per million tokens.

    ``prefix_tokens`` are tool definitions, the front of a cached prefix
    that sibling spawns share: only the share ``written_share`` of the
    spawns wrote it, the rest read it, so a first call costs
    ``written_share * write + (1 - written_share) * read`` per token.
    ``body_tokens`` (skills list, agent roster, deferred tool names, MCP
    instructions) sit after that prefix and every spawn writes its own.
    Each token is then read on every later call, ``later_calls`` of them
    on average. Without a read price only the writes are counted."""
    if not spawns or write is None or write <= 0:
        return 0.0
    read_price = read if read is not None and read > 0 else 0.0
    share = min(1.0, max(0.0, written_share))
    first_prefix = share * write + (1.0 - share) * read_price
    per_spawn = prefix_tokens * (first_prefix + later_calls * read_price) + body_tokens * (
        write + later_calls * read_price
    )
    return spawns * per_spawn / 1_000_000


def _price_inputs(acc: _AgentStartupAcc, family: str, positions: list[int]) -> tuple[float | None, float | None, float, float]:
    """The write price, read price, mean later calls and share of spawns
    that wrote the tool prefix for the spawns of ``acc`` on ``family``."""
    count = len(acc.startup_tokens)
    writes = _fixed_write_prices(acc, family)
    reads = _fixed_read_prices(acc, family)
    later = _mean_or_zero(_picked(acc.later_calls, positions, count)) if acc.later_calls else 0.0
    snapped = acc.tool_spawns.get(family, 0)
    written_share = acc.prefix_written.get(family, 0) / snapped if snapped else 1.0
    return (fmean(writes) if writes else None, fmean(reads) if reads else None, later, written_share)


def _build_startup_table(stats: ContextBudgetStats) -> Table:
    columns = [
        Column(key="agent_type", label="Agent type", kind="str"),
        Column(key="spawns", label="Spawns", kind="int"),
        Column(key="fork_spawns", label="Forks (left out)", kind="int"),
        Column(key="model", label="Model measured", kind="str"),
        Column(key="other_model_spawns", label="Other models (left out)", kind="int"),
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
        Column(key="removable_tools", label="Tools never used (can be left out)", kind="tokens"),
        # A part left out of a start is also not read back on the calls
        # after the first, so a saving is priced with these two (the
        # recommendation rules read them; see ``diet_usd``).
        Column(key="read_price", label="Cache-read price per million tokens", kind="money"),
        Column(key="later_calls", label="Calls after the first", kind="float"),
    ]
    rows: list[list] = []
    for agent_type in sorted(stats.agents, key=lambda key: -stats.agents[key].spawns):
        acc = stats.agents[agent_type]
        if not acc.startup_tokens:
            continue
        count = len(acc.startup_tokens)
        family, positions = acc.fixed_model()
        _write, read_price, later_calls, _written = _price_inputs(acc, family, positions)
        startup = fmean(_picked(acc.startup_tokens, positions, count))
        parts = {part: _mean_or_zero(_picked(acc.parts.get(part, []), positions, count)) for part in STARTUP_PARTS}
        known = sum(parts.values())
        not_recorded = max(0.0, startup - known)
        measured_pct = min(100.0, known / startup * 100) if startup else None
        managed_claude_md = _mean_or_zero(_picked(acc.claude_md_by_source.get("Managed", []), positions, count))
        prices = _fixed_write_prices(acc, family)
        removable_chars, _tools = acc.removable_chars(family)
        removable = stats.calibration.tool_tokens(removable_chars, family or None) if _tools else None
        rows.append(
            [agent_type, acc.spawns, acc.fork_spawns, family or None, count - len(positions), startup]
            + [parts[part] for part in STARTUP_PARTS]
            + [not_recorded, measured_pct, fmean(prices) if prices else None, managed_claude_md, removable]
            + [read_price, later_calls if acc.later_calls else None]
        )
    return Table(
        name="agent_startup_breakdown",
        title="What each subagent is given at startup",
        columns=columns,
        rows=rows,
        notes=[
            "Startup size is the first turn's whole input (new, cache-write and cache-read tokens). "
            "Every other column is the average per spawn, in tokens, estimated from what the transcript "
            f"records for that first turn, at characters per token {stats.calibration.basis()}.",
            "Each row averages the spawns on one model, the one most of that agent type's spawns ran on. "
            "The same tools measure differently on each model, as each one counts tokens its own way. "
            "Spawns on any other model are counted apart and left out, so a mix of models never "
            "decides which agent type looks big.",
            "Claude Code writes a subagent's tool definitions after its first call, so they come from the "
            "snapshot recorded just after it. A spawn with no such snapshot leaves its tool definitions in "
            "\"Not recorded\".",
            "Forks inherit the parent's conversation and prompt cache, so they are counted but kept "
            "out of the averages.",
            "\"CLAUDE.md and memory\" includes a managed policy CLAUDE.md (also shown on its own in "
            "the last column). An agent's setting that skips CLAUDE.md files skips only the project's "
            "own ones: a policy CLAUDE.md still loads.",
            "\"Tools never used\" is the size of the tool definitions and MCP servers this agent type was "
            f"offered and called in fewer than {int(REMOVABLE_USE_SHARE * 100)}% of the spawns offered them. "
            "A tools list on the agent would leave them out. Tools Claude Code adds whatever the list says "
            "are not counted.",
            "A part left out of the start is also not read back on the calls after the first, so the last two "
            "columns give what a saving needs: the cache-read price and the average number of those calls.",
        ],
    )


def _build_tools_table(stats: ContextBudgetStats) -> Table:
    columns = [
        Column(key="agent_type", label="Agent type", kind="str"),
        Column(key="model", label="Model measured", kind="str"),
        Column(key="tool", label="Tool", kind="str"),
        Column(key="offered_spawns", label="Spawns offered it", kind="int"),
        Column(key="used_spawns", label="Spawns that used it", kind="int"),
        Column(key="definition_tokens", label="Definition size", kind="tokens"),
    ]
    rows: list[list] = []
    for agent_type in sorted(stats.agents, key=lambda key: -stats.agents[key].spawns):
        acc = stats.agents[agent_type]
        if not acc.startup_tokens:
            continue
        family, _positions = acc.fixed_model()
        _total, tools = acc.removable_chars(family)
        for key, offered, used, chars in tools[:_TOOL_ROWS_PER_AGENT]:
            rows.append(
                [agent_type, family or None, key, offered, used, stats.calibration.tool_tokens(chars, family or None)]
            )
    return Table(
        name="agent_startup_tools",
        title="Tools offered to a subagent and rarely used",
        columns=columns,
        rows=rows,
        notes=[
            "One row per tool or MCP server an agent type was offered and called in fewer than "
            f"{int(REMOVABLE_USE_SHARE * 100)}% of the spawns offered it, largest first, "
            f"up to {_TOOL_ROWS_PER_AGENT} per agent type. An MCP server's tools are one row, "
            "named as an allowlist names them.",
            "Sizes are tool definitions only, read from the first snapshot that lists tools, "
            f"at characters per token {stats.calibration.basis()}. No description is kept.",
        ],
    )


def _build_diet_table(stats: ContextBudgetStats) -> Table:
    columns = [
        Column(key="agent_type", label="Agent type", kind="str"),
        Column(key="model", label="Model measured", kind="str"),
        Column(key="spawns", label="Spawns with tools recorded", kind="int"),
        Column(key="keep_tools", label="Tools to keep", kind="str"),
        Column(key="rare_tools", label="Tools rarely used", kind="str"),
        Column(key="dropped_definitions", label="Tool definitions left out", kind="tokens"),
        Column(key="dropped_deferred", label="Deferred tool names left out", kind="tokens"),
        Column(key="dropped_skills", label="Skills list left out", kind="tokens"),
        Column(key="dropped_roster", label="Agent list left out", kind="tokens"),
        Column(key="kept_instructions", label="MCP instructions that stay", kind="tokens"),
        Column(key="later_calls", label="Calls after the first", kind="float"),
        Column(key="prefix_write_share", label="Spawns that wrote the tool prefix", kind="pct"),
        Column(key="write_price", label="Cache-write price per million tokens", kind="money"),
        Column(key="read_price", label="Cache-read price per million tokens", kind="money"),
        Column(key="saving_usd", label="Saving across these spawns", kind="money"),
        Column(key="window_days", label="Days in the window", kind="float"),
    ]
    rows: list[list] = []
    for agent_type in sorted(stats.agents, key=lambda key: -stats.agents[key].spawns):
        acc = stats.agents[agent_type]
        if not acc.startup_tokens:
            continue
        family, positions = acc.fixed_model()
        snapped = acc.tool_spawns.get(family, 0)
        if not snapped:
            continue
        count = len(acc.startup_tokens)
        _total, rare_rows = acc.removable_chars(family)
        builtin = [row for row in rare_rows if not row[0].startswith("mcp__")]
        servers = acc.rare_servers(family)
        if not builtin and not servers:
            continue
        tools = acc.tools.get(family, {})
        definition_chars = sum(tools[key].chars for key, _o, _u, _c in builtin) / snapped
        definitions = stats.calibration.tool_tokens(definition_chars, family or None) + sum(
            offer.definitions for _name, offer in servers
        ) / snapped
        deferred = sum(offer.deferred for _name, offer in servers) / snapped
        instructions = sum(offer.instructions for _name, offer in servers) / snapped
        dropped = {key for key, _o, _u, _c in builtin}
        skills = (
            _mean_or_zero(_picked(acc.parts.get("skills_listing", []), positions, count))
            if SKILLS_TOOL in dropped
            else 0.0
        )
        roster = _mean_or_zero(_picked(acc.roster_tokens, positions, count)) if dropped & ROSTER_TOOLS else 0.0
        write, read, later, written_share = _price_inputs(acc, family, positions)
        usd = diet_usd(
            snapped,
            definitions,
            deferred + skills + roster,
            write=write,
            read=read,
            later_calls=later,
            written_share=written_share,
        )
        rare_names = [key for key, _o, _u, _c in builtin] + [mcp_tool_key(name) for name, _offer in servers]
        rows.append(
            [
                agent_type,
                family or None,
                snapped,
                ", ".join(acc.kept_tools(family)),
                ", ".join(rare_names),
                definitions,
                deferred,
                skills,
                roster,
                instructions,
                later,
                written_share * 100,
                write,
                read,
                usd or None,
                stats.window_days,
            ]
        )
    return Table(
        name="agent_startup_diet",
        title="What a tools list would take out of each agent type's start",
        columns=columns,
        rows=rows,
        notes=[
            "One row per agent type that was offered tools or MCP servers it called in fewer than "
            f"{int(REMOVABLE_USE_SHARE * 100)}% of the spawns offered them, on the model most of its spawns ran on. "
            "\"Tools to keep\" are the ones called in at least that share of them, including tools loaded when "
            "asked for. Tools Claude Code adds whatever the list says are in neither.",
            "Sizes are per spawn. The skills list goes with the Skill tool and the agent list with the Agent "
            "tool. An MCP server's deferred tool names go with its tools; its instructions stay under a "
            "tools list, so they are not counted in the saving. Deferred built-in tool names are not counted.",
            "The saving prices tool definitions as a prefix that sibling spawns share: only the spawns that wrote it "
            "pay the cache-write price for them, the rest pay the cache-read price. The other parts are written "
            "by each spawn. Every part is then read on each later call. List prices, over the spawns in this window.",
            f"Sizes are at characters per token {stats.calibration.basis()}. No description is kept.",
        ],
    )


def _build_servers_table(stats: ContextBudgetStats) -> Table:
    columns = [
        Column(key="agent_type", label="Agent type", kind="str"),
        Column(key="model", label="Model measured", kind="str"),
        Column(key="server", label="MCP server", kind="str"),
        Column(key="offered_spawns", label="Spawns offered it", kind="int"),
        Column(key="used_spawns", label="Spawns that used it", kind="int"),
        Column(key="definition_tokens", label="Tool definitions", kind="tokens"),
        Column(key="deferred_tokens", label="Deferred tool names", kind="tokens"),
        Column(key="instruction_tokens", label="Instructions", kind="tokens"),
        Column(key="saving_usd", label="Cost across these spawns", kind="money"),
        Column(key="window_days", label="Days in the window", kind="float"),
    ]
    rows: list[list] = []
    for agent_type in sorted(stats.agents, key=lambda key: -stats.agents[key].spawns):
        acc = stats.agents[agent_type]
        if not acc.startup_tokens:
            continue
        family, positions = acc.fixed_model()
        write, read, later, written_share = _price_inputs(acc, family, positions)
        for server, offer in acc.rare_servers(family)[:_SERVER_ROWS_PER_AGENT]:
            usd = diet_usd(
                offer.offered,
                offer.definitions / offer.offered,
                offer.deferred / offer.offered,
                write=write,
                read=read,
                later_calls=later,
                written_share=written_share,
            )
            rows.append(
                [
                    agent_type,
                    family or None,
                    server,
                    offer.offered,
                    offer.used,
                    offer.definitions / offer.offered,
                    offer.deferred / offer.offered,
                    offer.instructions / offer.offered,
                    usd or None,
                    stats.window_days,
                ]
            )
    return Table(
        name="agent_startup_servers",
        title="MCP servers offered to a subagent and rarely used",
        columns=columns,
        rows=rows,
        notes=[
            "One row per MCP server an agent type was offered and called in fewer than "
            f"{int(REMOVABLE_USE_SHARE * 100)}% of the spawns offered it, largest first, "
            f"up to {_SERVER_ROWS_PER_AGENT} per agent type.",
            "Sizes are per spawn offered it: the tool definitions sent in full, the names of its deferred "
            "tools, and its instructions. The cost prices the first two as the tools list table does: a tools "
            "list leaves out those two only. The instructions stay under it and are not in the cost.",
            f"Sizes are at characters per token {stats.calibration.basis()}. No instruction text is kept.",
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


def _reference_family(stats: ContextBudgetStats) -> str:
    """The model most measured spawns ran on, which every agent type is
    compared on in the shared table (``""`` when the accumulators don't
    name models)."""
    tally: dict[str, int] = {}
    for acc in stats.agents.values():
        if len(acc.models) == len(acc.startup_tokens):
            for family in acc.models:
                if family:
                    tally[family] = tally.get(family, 0) + 1
    return max(sorted(tally), key=lambda name: tally[name]) if tally else ""


def _build_shared_table(stats: ContextBudgetStats) -> Table:
    columns = [
        Column(key="part", label="What", kind="str"),
        Column(key="source", label="Where it comes from", kind="str"),
        Column(key="agent_types", label="Agent types given it", kind="str"),
        Column(key="mean_tokens", label="Size per spawn", kind="tokens"),
        Column(key="total_tokens", label="Total across spawns", kind="tokens"),
    ]
    # Agent types are compared on one model: the same tool set is a
    # different size on each, which would otherwise decide what looks shared.
    reference = _reference_family(stats)
    measured: list[tuple[_AgentStartupAcc, list[int]]] = []
    for acc in stats.agents.values():
        if not acc.startup_tokens:
            continue
        count = len(acc.startup_tokens)
        positions = (
            [i for i, family in enumerate(acc.models) if family == reference]
            if reference and len(acc.models) == count
            else list(range(count))
        )
        if positions:
            measured.append((acc, positions))
    rows: list[list] = []
    if len(measured) >= 2:
        sources = sorted({source for acc, _ in measured for source in acc.claude_md_by_source})
        candidates: list[tuple[str, str, list[list[float]]]] = [
            (
                f"claude_md:{source}",
                CLAUDE_MD_SOURCES.get(source, source),
                [_picked(acc.claude_md_by_source.get(source, []), positions, len(acc.startup_tokens)) for acc, positions in measured],
            )
            for source in sources
        ]
        candidates += [
            (
                part,
                source,
                [_picked(acc.parts.get(part, []), positions, len(acc.startup_tokens)) for acc, positions in measured],
            )
            for part, source in _PART_SOURCES.items()
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
            "Only spawns on "
            + (reference or "one model")
            + " are compared, so the model never decides which parts look shared.",
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
        tables=[
            _build_startup_table(stats),
            _build_unused_table(stats),
            _build_shared_table(stats),
            _build_tools_table(stats),
            _build_diet_table(stats),
            _build_servers_table(stats),
        ],
        notes=[],
    )


__all__ = [
    "CLAUDE_MD_SOURCES",
    "ContextBudgetStats",
    "DIET_EXCLUDED",
    "MIN_WINDOW_DAYS",
    "REMOVABLE_USE_SHARE",
    "ROSTER_TOOLS",
    "SKILLS_TOOL",
    "STARTUP_PARTS",
    "StartupSizes",
    "build_section",
    "build_startup_section",
    "diet_usd",
    "first_call",
    "load_context_window_rows",
    "mcp_tool_key",
    "rarely_used",
    "startup_sizes",
]

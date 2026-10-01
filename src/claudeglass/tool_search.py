"""What MCP tool search saves (``tool_search``).

With tool search on (Claude Code's default on the Anthropic API), Claude
Code sends deferred tools by name only and loads a tool's full
definition when Claude searches for it. Without it, every definition
would sit at the front of every request. ``parse.py`` keeps, per reply,
how many listed tools had no definition loaded, by MCP server
(``Turn.deferred_tools_by_server``), and per transcript the size of each
definition it did load (``TranscriptResult.tool_definition_chars``).

The model, per reply requested with tools deferred:

- **Kept out.** Each unloaded tool is sized at the average definition of
  its own MCP server, measured from the definitions loaded anywhere in
  the window, or at the average of every server when none of its own was
  loaded. Nothing is priced when no definition was loaded at all.
- **Priced** at that reply's own rate for the front of the prompt: its
  cache read rate when it read the cache, its cache write rate when it
  wrote the cache from the start, its input rate when it cached nothing
  (each derived from ``price_turn``, so geo and long-context multipliers
  are folded in).
- **Taken off**: the name list sent in the definitions' place, priced the
  same way, and every reply that did nothing but call ``ToolSearch``, in
  full: without tool search Claude would have called the tool directly.

**Each MCP server** (``tool_search_servers``): every server the window's
transcripts or config snapshots name, keyed by its name as its tools
carry it (``parse.mcp_name``), with what keeping it cost and whether
Claude used it. Nothing here names a server: what a server is, and what
removing it takes, comes from generic evidence only.

- **Two names, one server.** The desktop app names a claude.ai connector
  by an ID (``1a59c906-...``) where Claude Code on its own calls it
  ``claude_ai_<name>``. An ID-named server with exactly the same tools as
  a named one is the same server; a use under either name counts.
- **Offered** when a reply's name list, instructions or tools sent in full
  (``prompt_snapshot``) include it; **used** when Claude called one of its
  tools, read one of its resources, ran one of its prompts, a reply is
  attributed to it (``attributionMcpServer``), or a known saver's hook
  pointed Claude at its tools (``Turn.saver_redirects``).
- **Kind**, from positive evidence only: a claude.ai connector (by name,
  and in the desktop app by an ID matched to that name), a plugin's server
  (``plugin_...``), or a server in a config snapshot's local
  (``~/.claude.json``, per project), project (``.mcp.json``), managed or
  user list. Anything else is of unknown kind and never gets a card.
- **Priced** per reply at the front-of-prompt rate (:func:`_front_rate`):
  its share of the name list, its instructions and its tools sent in full.
  A lower bound: a reply that rebuilt the cache paid the write rate.

The ``mcp-unused-server`` rule (:data:`RULES`) lists the servers offered
in many main sessions, recently and for a while, that Claude never used,
each with the fix for its kind.

Same shape as ``hook_costs.py``: :class:`ToolSearchThresholds`,
:func:`compute_tool_search`, :func:`build_section` and :data:`RULES`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from . import known_savers
from . import snapshots as snapshots_mod
from .discovery import redact_slug
from .model import Column, Recommendation, ReportModel, Section, Table, TranscriptResult, Turn
from .parse import BUILT_IN_TOOLS, mcp_name, tool_server
from .pricing import Pricing, price_turn

#: Characters per token, the approximation used throughout this report
#: (duplicated per module by convention, see ``carry.py``).
_CHARS_PER_TOKEN_APPROX = 4

#: The tool Claude calls to load deferred definitions.
SEARCH_TOOL = "ToolSearch"

ASSUMPTIONS: tuple[str, ...] = (
    "a tool definition tool search kept out would have been at the front of every request, so it is priced at "
    "that reply's cache read rate, or its cache write rate when the reply wrote the cache from the start",
    "a definition that was never loaded is sized at the average of the loaded definitions from its own MCP "
    "server, or from every server when none of its own were loaded",
    "a reply that only searched for tools wouldn't have happened without tool search, so its whole cost is "
    "taken off the saving",
)


@dataclass(slots=True)
class ToolSearchThresholds:
    """Every tunable number this section depends on (same
    ``from_config``/``describe`` convention as ``hook_costs.HookThresholds``).
    Config keys carry a ``tool_search_`` prefix."""

    #: How many servers ``tool_search_by_server`` lists.
    top_n: int = 20
    #: ``mcp-unused-server``: main sessions a server must be offered in.
    unused_min_sessions: int = 10
    #: ... how many days before the window's end it was first offered.
    unused_min_age_days: float = 7.0
    #: ... and at most how many days before the end it was last offered,
    #: so the card clears once the server is gone.
    unused_max_idle_days: float = 3.0
    #: ... what keeping it cost, at least.
    unused_min_server_usd: float = 0.50
    #: The card's total, at least.
    unused_min_total_usd: float = 1.0

    @classmethod
    def from_config(cls, config: dict | None) -> "ToolSearchThresholds":
        data = config or {}
        if not isinstance(data, dict):
            data = {}
        nested = data.get("thresholds")
        if isinstance(nested, dict):
            data = nested
        kwargs: dict = {}
        for name, kind in (
            ("top_n", int),
            ("unused_min_sessions", int),
            ("unused_min_age_days", float),
            ("unused_max_idle_days", float),
            ("unused_min_server_usd", float),
            ("unused_min_total_usd", float),
        ):
            key = f"tool_search_{name}"
            if key in data:
                try:
                    kwargs[name] = kind(data[key])
                except (TypeError, ValueError):
                    pass
        return cls(**kwargs)

    def describe(self) -> list[str]:
        return [
            f"\"By MCP server\" lists the top {self.top_n}; \"Each MCP server\" lists every one.",
            f"An MCP server is flagged as unused when it was offered in at least {self.unused_min_sessions} main "
            f"sessions, first at least {self.unused_min_age_days:g} days and last at most "
            f"{self.unused_max_idle_days:g} days before the window's end, Claude never used it, and keeping it "
            f"cost at least ${self.unused_min_server_usd:.2f}; the card needs ${self.unused_min_total_usd:.2f} "
            "in all.",
        ]


_DEFAULT_THRESHOLDS = ToolSearchThresholds()


@dataclass(slots=True)
class ServerRow:
    """One MCP server (or Claude Code's own tools): counts and costs only."""

    server: str
    #: Most of its tools listed by name only in one request.
    most_deferred: int = 0
    #: Its tools whose definitions were loaded somewhere in the window.
    measured: int = 0
    #: Average definition size its unloaded tools were given, in tokens.
    definition_tokens: float = 0.0
    #: Whether that average came from its own loaded tools.
    own_sizes: bool = False
    replies: int = 0
    kept_tokens: float = 0.0
    saving_usd: float = 0.0

    @property
    def kept_per_reply(self) -> float:
        return self.kept_tokens / self.replies if self.replies else 0.0


@dataclass(slots=True)
class ToolSearchStats:
    #: Priced replies in the window, and those requested with tools deferred.
    all_replies: int = 0
    replies: int = 0
    #: Most tools listed by name only in one request, and how many of those
    #: came from MCP servers.
    most_deferred: int = 0
    most_deferred_mcp: int = 0
    #: Distinct definitions loaded in the window, and their average size.
    definitions_measured: int = 0
    mean_definition_tokens: float = 0.0
    kept_tokens: float = 0.0
    gross_usd: float = 0.0
    list_tokens: float = 0.0
    list_usd: float = 0.0
    search_replies: int = 0
    search_usd: float = 0.0
    servers: dict[str, ServerRow] = field(default_factory=dict)
    #: Every MCP server named in the window (``tool_search_servers``).
    mcp_servers: list["McpServerRow"] = field(default_factory=list)

    @property
    def net_usd(self) -> float:
        return self.gross_usd - self.list_usd - self.search_usd

    @property
    def measurable(self) -> bool:
        """Whether any definition was loaded, so sizes can be estimated."""
        return self.definitions_measured > 0

    def ranked(self) -> list[ServerRow]:
        return sorted(self.servers.values(), key=lambda s: (-s.saving_usd, -s.most_deferred, s.server))


#: What an MCP server is (:func:`_kind`). Removing one of the first four
#: changes every project, so a one-project view can't judge it.
KIND_DESKTOP_CONNECTOR = "claude.ai connector (desktop app)"
KIND_CONNECTOR = "claude.ai connector"
KIND_PLUGIN = "plugin"
KIND_USER = "user (every project)"
KIND_LOCAL = "local (one project)"
KIND_PROJECT = "project (.mcp.json)"
KIND_MANAGED = "managed"
KIND_UNKNOWN = "unknown"
_ACCOUNT_WIDE = frozenset({KIND_DESKTOP_CONNECTOR, KIND_CONNECTOR, KIND_PLUGIN, KIND_USER})
_REMOVABLE = _ACCOUNT_WIDE | {KIND_LOCAL, KIND_PROJECT}

#: A server's status, in the order the table lists them. Only ``remove``
#: gets a card.
STATUS_REMOVE = "remove"
STATUS_UNUSED = "unused"
STATUS_ALL_PROJECTS = "all-projects view only"
STATUS_UNKNOWN = "kind unknown"
STATUS_MANAGED = "managed"
STATUS_SUBAGENTS = "subagents only"
STATUS_USED = "used"
STATUS_NOT_SEEN = "configured, not seen"
_STATUS_ORDER = (
    STATUS_REMOVE,
    STATUS_UNUSED,
    STATUS_ALL_PROJECTS,
    STATUS_UNKNOWN,
    STATUS_MANAGED,
    STATUS_SUBAGENTS,
    STATUS_USED,
    "needs sign-in",
    "failed to connect",
    "pending",
    STATUS_NOT_SEEN,
)

#: Claude Code's own prefix for the claude.ai connectors it fetches, and a
#: plugin's for the servers it brings (``plugin:<plugin>:<server>``).
_CONNECTOR_PREFIX = "claude_ai_"
_PLUGIN_PREFIX = "plugin_"
#: How the desktop app names a server it hosts: an ID, not a name.
_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
#: Tools shown per server.
_SAMPLES = 3


@dataclass(slots=True)
class McpServerRow:
    """One MCP server (under all its names): counts, days and costs only."""

    server: str
    #: Its other names, e.g. the desktop app's ID for a connector.
    aliases: tuple[str, ...] = ()
    kind: str = KIND_UNKNOWN
    status: str = STATUS_NOT_SEEN
    main_sessions: int = 0
    subagent_runs: int = 0
    uses: int = 0
    #: Days before the window's end it was first and last offered.
    first_seen_days: float | None = None
    last_seen_days: float | None = None
    samples: tuple[str, ...] = ()
    list_usd: float = 0.0
    instructions_usd: float = 0.0
    definitions_usd: float = 0.0
    #: Its name as the config writes it, for a command (local, project or
    #: user kind), and the projects that config belongs to (local, project).
    config_name: str = ""
    projects: tuple[str, ...] = ()

    @property
    def removable_usd(self) -> float:
        return self.list_usd + self.instructions_usd + self.definitions_usd


@dataclass(slots=True)
class _Seen:
    """What the transcripts show of one server name."""

    main: set = field(default_factory=set)
    runs: set = field(default_factory=set)
    first: datetime | None = None
    last: datetime | None = None
    uses: int = 0
    list_usd: float = 0.0
    instructions_usd: float = 0.0
    definitions_usd: float = 0.0
    tools: set = field(default_factory=set)
    connection: str = ""


@dataclass(slots=True)
class _Config:
    """The MCP servers config snapshots name, by :func:`parse.mcp_name`:
    the raw name, and for local and project lists the projects that have
    it."""

    local: dict[str, tuple[str, set]] = field(default_factory=dict)
    project: dict[str, tuple[str, set]] = field(default_factory=dict)
    managed: dict[str, str] = field(default_factory=dict)
    user: dict[str, str] = field(default_factory=dict)
    #: ``managedMcpServers`` is set, so a server on the merged list that no
    #: other list has may be the organisation's.
    managed_list: bool = False

    def names(self) -> set[str]:
        return set(self.local) | set(self.project) | set(self.managed) | set(self.user)


def _config_names(value) -> list[str]:
    """Raw server names off a snapshot list, less the ones the config
    hook redacted."""
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str) and v and not v.startswith("<redacted")]


def _config_key(raw: str, known: set[str]) -> str:
    """``raw``'s key: its :func:`parse.mcp_name`, or for a name the config
    hook clipped ("...") the one server in ``known`` it begins."""
    if raw.endswith("...") and len(raw) > 3:
        prefix = mcp_name(raw[:-3])
        matches = [name for name in known if name.startswith(prefix)]
        if len(matches) == 1:
            return matches[0]
    return mcp_name(raw)


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _read_config(snapshots, slugs: dict[str, str], known: set[str]) -> _Config:
    """Every project's latest snapshot. ``slugs`` maps a snapshot's
    project key to the project as reports name it."""
    config = _Config()
    for label, snap in snapshots_mod.latest_snapshot_per_project(list(snapshots or ())).items():
        project = slugs.get(label, "")
        layers = _dict(snap.data.get("content_layers"))
        for raw in _config_names(_dict(snap.data.get("claude_json")).get("mcp_servers")):
            config.local.setdefault(_config_key(raw, known), (raw, set()))[1].add(project)
        for raw in _config_names(_dict(layers.get("mcp_json")).get("names")):
            config.project.setdefault(_config_key(raw, known), (raw, set()))[1].add(project)
        for raw in _config_names(_dict(layers.get("managed_mcp")).get("names")):
            config.managed.setdefault(_config_key(raw, known), raw)
        for raw in _config_names(_dict(snap.data.get("mcp_servers")).get("names")):
            config.user.setdefault(_config_key(raw, known), raw)
        if "managedMcpServers" in snapshots_mod.managed_keys(snap):
            config.managed_list = True
    return config


def _kind(names: list[str], config: _Config) -> tuple[str, str, tuple[str, ...]]:
    """What the server under ``names`` is, from positive evidence only:
    its kind, its config name and the projects that config belongs to."""
    if any(name.startswith(_CONNECTOR_PREFIX) for name in names):
        desktop = any(_ID_RE.match(name) for name in names)
        return (KIND_DESKTOP_CONNECTOR if desktop else KIND_CONNECTOR), "", ()
    if any(name.startswith(_PLUGIN_PREFIX) for name in names):
        return KIND_PLUGIN, "", ()
    for name in names:
        if name in config.managed:
            return KIND_MANAGED, config.managed[name], ()
    for table, kind in ((config.local, KIND_LOCAL), (config.project, KIND_PROJECT)):
        for name in names:
            if name in table:
                raw, projects = table[name]
                return kind, raw, tuple(sorted(p for p in projects if p))
    for name in names:
        if name in config.user:
            # The merged list also carries managedMcpServers: with that
            # set, a server no other list has may be the organisation's.
            return (KIND_MANAGED if config.managed_list else KIND_USER), config.user[name], ()
    return KIND_UNKNOWN, "", ()


def _parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None
    except ValueError:
        return None


def _saver_servers() -> dict[str, str]:
    """Known saver -> the MCP server its tools come from."""
    return {saver.name: tool_server(saver.tool_prefix) for saver in known_savers.KNOWN_SAVERS}


def _used_in(turn: Turn, savers: dict[str, str]) -> dict[str, int]:
    """MCP server -> this turn's uses of it (see the module docstring)."""
    used: dict[str, int] = {}

    def add(server: str, n: int = 1) -> None:
        if server and server != BUILT_IN_TOOLS and n > 0:
            used[server] = used.get(server, 0) + n

    for name, n in (turn.tool_calls_by_tool or {}).items():
        if isinstance(n, int) and str(name).startswith("mcp__"):
            add(tool_server(str(name)), n)
    for server, n in (turn.mcp_resource_servers or {}).items():
        if isinstance(n, int):
            add(server, n)
    for command in turn.commands_run or ():
        command = str(command).lstrip("/")
        if command.startswith("mcp__"):
            add(tool_server(command))
    for saver, n in (turn.saver_redirects or {}).items():
        if saver in savers and isinstance(n, int):
            add(savers[saver], n)
    if turn.attribution_mcp_server:
        server = mcp_name(turn.attribution_mcp_server)
        if server not in used:
            add(server)
    return used


def _mcp_servers(
    results: list[TranscriptResult], lookup, snapshots, all_projects: bool, th: "ToolSearchThresholds"
) -> list[McpServerRow]:
    """Every MCP server the window names (see the module docstring)."""
    seen: dict[str, _Seen] = {}
    savers = _saver_servers()
    slugs: dict[str, str] = {}
    end: datetime | None = None

    def get(server: str) -> _Seen:
        entry = seen.get(server)
        if entry is None:
            entry = seen[server] = _Seen()
        return entry

    for tr in results:
        main = tr.meta.kind == "top-level"
        run = tr.meta.session_id if main else tr.meta.path
        slug = tr.meta.project_slug or ""
        if slug:
            slugs.setdefault(snapshots_mod.snapshot_project_key(slug), redact_slug(slug))
        upfront = {
            s: c for s, c in (tr.upfront_definition_chars_by_server or {}).items() if isinstance(c, int) and c > 0
        }
        for server, names in (tr.mcp_tool_suffixes_by_server or {}).items():
            get(server).tools.update(names)
        for server, word in (tr.mcp_connection_status or {}).items():
            get(server).connection = str(word)
        for turn in tr.turns:
            for server, n in _used_in(turn, savers).items():
                get(server).uses += n
            if turn.turn_index <= 0 or turn.is_synthetic:
                continue
            at = _parse_ts(turn.ts)
            if at is not None and (end is None or at > end):
                end = at
            offered: dict[str, list[int]] = {}
            for chars_by_server, slot in (
                (turn.deferred_list_chars_by_server, 0),
                (turn.mcp_instruction_chars_by_server, 1),
                (upfront, 2),
            ):
                for server, chars in (chars_by_server or {}).items():
                    if server != BUILT_IN_TOOLS and isinstance(chars, int) and chars > 0:
                        offered.setdefault(server, [0, 0, 0])[slot] += chars
            if not offered:
                continue
            rate = _front_rate(turn, lookup(turn.model)) / _CHARS_PER_TOKEN_APPROX
            for server, (list_chars, instruction_chars, definition_chars) in offered.items():
                entry = get(server)
                entry.list_usd += list_chars * rate
                entry.instructions_usd += instruction_chars * rate
                entry.definitions_usd += definition_chars * rate
                (entry.main if main else entry.runs).add(run)
                if at is not None:
                    entry.first = at if entry.first is None or at < entry.first else entry.first
                    entry.last = at if entry.last is None or at > entry.last else entry.last

    seen.pop(BUILT_IN_TOOLS, None)
    config = _read_config(snapshots, slugs, set(seen))
    for name in config.names() - set(seen):
        get(name)

    # Two names, one server: an ID-named server with exactly the same
    # tools as a named one.
    by_tools: dict[frozenset, list[str]] = {}
    for name, entry in seen.items():
        if entry.tools:
            by_tools.setdefault(frozenset(entry.tools), []).append(name)
    groups: list[list[str]] = []
    grouped: set[str] = set()
    for names in by_tools.values():
        if len(names) > 1 and any(_ID_RE.match(n) for n in names):
            groups.append(names)
            grouped.update(names)
    groups += [[name] for name in seen if name not in grouped]

    rows: list[McpServerRow] = []
    for names in groups:
        # A readable name first: a claude.ai connector's, then any, then an ID.
        names.sort(key=lambda n: (bool(_ID_RE.match(n)), not n.startswith(_CONNECTOR_PREFIX), n))
        entries = [seen[n] for n in names]
        firsts = [e.first for e in entries if e.first is not None]
        lasts = [e.last for e in entries if e.last is not None]
        kind, config_name, projects = _kind(names, config)
        tools = sorted(set().union(*(e.tools for e in entries)))
        row = McpServerRow(
            server=names[0],
            aliases=tuple(names[1:]),
            kind=kind,
            main_sessions=len(set().union(*(e.main for e in entries))),
            subagent_runs=len(set().union(*(e.runs for e in entries))),
            uses=sum(e.uses for e in entries),
            first_seen_days=(end - min(firsts)).total_seconds() / 86400 if end and firsts else None,
            last_seen_days=(end - max(lasts)).total_seconds() / 86400 if end and lasts else None,
            samples=tuple(tools[:_SAMPLES]),
            list_usd=sum(e.list_usd for e in entries),
            instructions_usd=sum(e.instructions_usd for e in entries),
            definitions_usd=sum(e.definitions_usd for e in entries),
            config_name=config_name,
            projects=projects,
        )
        row.status = _status(row, next((e.connection for e in entries if e.connection), ""), all_projects, th)
        rows.append(row)
    order = {status: i for i, status in enumerate(_STATUS_ORDER)}
    rows.sort(key=lambda r: (order.get(r.status, len(order)), -r.removable_usd, r.server))
    return rows


def _status(row: McpServerRow, connection: str, all_projects: bool, th: "ToolSearchThresholds") -> str:
    if row.uses:
        return STATUS_USED
    if not row.main_sessions and not row.subagent_runs:
        return connection or STATUS_NOT_SEEN
    if not row.main_sessions:
        return STATUS_SUBAGENTS
    if row.kind == KIND_MANAGED:
        return STATUS_MANAGED
    # The desktop app names its connectors by ID; the claude.ai name that
    # tells one apart may only turn up in another project's sessions.
    if row.kind == KIND_UNKNOWN and not all_projects and _ID_RE.match(row.server):
        return STATUS_ALL_PROJECTS
    if row.kind not in _REMOVABLE:
        return STATUS_UNKNOWN
    if row.kind in _ACCOUNT_WIDE and not all_projects:
        return STATUS_ALL_PROJECTS
    if (
        row.main_sessions >= th.unused_min_sessions
        and (row.first_seen_days or 0.0) >= th.unused_min_age_days
        and row.last_seen_days is not None
        and row.last_seen_days <= th.unused_max_idle_days
        and row.removable_usd >= th.unused_min_server_usd
    ):
        return STATUS_REMOVE
    return STATUS_UNUSED


def _front_rate(turn: Turn, rates) -> float:
    """What one more token at the front of this reply's prompt would have
    cost: its cache read rate when it read the cache, its cache write rate
    when it wrote it from the start, else its input rate."""
    breakdown = price_turn(turn, rates)
    if turn.cache_read_tokens > 0:
        return breakdown.cache_read_cost / turn.cache_read_tokens
    if turn.cache_creation_tokens > 0:
        return breakdown.cache_write_cost / turn.cache_creation_tokens
    if turn.input_tokens > 0:
        return breakdown.input_cost / turn.input_tokens
    return 0.0


def _definition_sizes(results: list[TranscriptResult]) -> dict[str, int]:
    """Tool name -> characters of its largest loaded definition."""
    sizes: dict[str, int] = {}
    for tr in results:
        for name, chars in tr.tool_definition_chars.items():
            if isinstance(chars, int) and chars > sizes.get(name, 0):
                sizes[name] = chars
    return sizes


def compute_tool_search(
    results: list[TranscriptResult],
    pricing: Pricing,
    thresholds: ToolSearchThresholds | None = None,
    *,
    snapshots=None,
    all_projects: bool = False,
) -> ToolSearchStats:
    """The tool search figures over ``results`` (every transcript in the
    window: main sessions and agents each get their own deferred list).
    ``snapshots`` (config snapshots: what kind each MCP server is) and
    ``all_projects`` (the window covers every project, so a server every
    project loads can be judged) feed ``tool_search_servers``."""
    th = thresholds or _DEFAULT_THRESHOLDS
    stats = ToolSearchStats()
    stats.mcp_servers = _mcp_servers(results, pricing.resolve_model, snapshots, all_projects, th)
    sizes = _definition_sizes(results)
    by_server: dict[str, list[int]] = {}
    for name, chars in sizes.items():
        by_server.setdefault(tool_server(name), []).append(chars)
    stats.definitions_measured = len(sizes)
    if sizes:
        stats.mean_definition_tokens = sum(sizes.values()) / len(sizes) / _CHARS_PER_TOKEN_APPROX

    def server_row(server: str) -> ServerRow:
        row = stats.servers.get(server)
        if row is None:
            own = by_server.get(server)
            row = stats.servers[server] = ServerRow(
                server=server,
                measured=len(own or ()),
                definition_tokens=(
                    sum(own) / len(own) / _CHARS_PER_TOKEN_APPROX if own else stats.mean_definition_tokens
                ),
                own_sizes=bool(own),
            )
        return row

    lookup = pricing.resolve_model
    for tr in results:
        for turn in tr.turns:
            if turn.turn_index <= 0 or turn.is_synthetic:
                continue
            stats.all_replies += 1
            if set(turn.tool_names) == {SEARCH_TOOL}:
                stats.search_replies += 1
                stats.search_usd += price_turn(turn, lookup(turn.model)).total
            deferred = {s: n for s, n in turn.deferred_tools_by_server.items() if isinstance(n, int) and n > 0}
            if not deferred:
                continue
            stats.replies += 1
            total = sum(deferred.values())
            stats.most_deferred = max(stats.most_deferred, total)
            stats.most_deferred_mcp = max(
                stats.most_deferred_mcp, sum(n for s, n in deferred.items() if s != BUILT_IN_TOOLS)
            )
            rate = _front_rate(turn, lookup(turn.model))
            list_tokens = turn.deferred_list_chars / _CHARS_PER_TOKEN_APPROX
            stats.list_tokens += list_tokens
            stats.list_usd += list_tokens * rate
            for server, count in deferred.items():
                row = server_row(server)
                row.most_deferred = max(row.most_deferred, count)
                row.replies += 1
                if not stats.measurable:
                    continue
                kept = count * row.definition_tokens
                row.kept_tokens += kept
                row.saving_usd += kept * rate
                stats.kept_tokens += kept
                stats.gross_usd += kept * rate
    return stats


# -- report section -----------------------------------------------------------


def build_section(stats: ToolSearchStats, thresholds: ToolSearchThresholds | None = None) -> Section:
    th = thresholds or _DEFAULT_THRESHOLDS
    measurable = stats.measurable
    summary = Table(
        name="tool_search_summary",
        title="What tool search saves",
        columns=[
            Column(key="scope", label="Scope", kind="str"),
            Column(key="replies", label="Replies with tools deferred", kind="int"),
            Column(key="most_deferred", label="Most tools deferred in one reply", kind="int"),
            Column(key="most_deferred_mcp", label="Of them MCP tools", kind="int"),
            Column(key="definitions_measured", label="Definitions measured", kind="int"),
            Column(key="definition_tokens", label="Average definition", kind="tokens"),
            Column(key="kept_per_reply", label="Kept out of each reply", kind="tokens"),
            Column(key="gross_usd", label="Saved by keeping them out", kind="money"),
            Column(key="list_usd", label="Cost of the name list", kind="money"),
            Column(key="search_replies", label="Replies that only searched", kind="int"),
            Column(key="search_usd", label="Cost of those replies", kind="money"),
            Column(key="net_usd", label="Net saving", kind="money"),
        ],
        rows=[
            [
                "all replies",
                stats.replies,
                stats.most_deferred,
                stats.most_deferred_mcp,
                stats.definitions_measured,
                stats.mean_definition_tokens if measurable else None,
                stats.kept_tokens / stats.replies if measurable and stats.replies else None,
                stats.gross_usd if measurable else None,
                stats.list_usd,
                stats.search_replies,
                stats.search_usd,
                stats.net_usd if measurable else None,
            ]
        ],
    )
    ranked = stats.ranked()
    by_server = Table(
        name="tool_search_by_server",
        title="By MCP server",
        columns=[
            Column(key="server", label="MCP server", kind="str"),
            Column(key="most_deferred", label="Most tools deferred", kind="int"),
            Column(key="measured", label="Definitions measured", kind="int"),
            Column(key="definition_tokens", label="Average definition", kind="tokens"),
            Column(key="sized_from", label="Sized from", kind="str"),
            Column(key="replies", label="Replies", kind="int"),
            Column(key="kept_per_reply", label="Kept out of each reply", kind="tokens"),
            Column(key="saving_usd", label="Saved by keeping them out", kind="money"),
        ],
        rows=[
            [
                s.server,
                s.most_deferred,
                s.measured,
                s.definition_tokens if measurable else None,
                ("its own tools" if s.own_sizes else "all servers") if measurable else "",
                s.replies,
                s.kept_per_reply if measurable else None,
                s.saving_usd if measurable else None,
            ]
            for s in ranked[: th.top_n]
        ],
    )
    notes = list(ASSUMPTIONS) + list(SERVER_ASSUMPTIONS) + [f"Thresholds: {' '.join(th.describe())}"]
    if stats.replies and not measurable:
        notes.append(
            "No tool definition was loaded in this window, so there is nothing to size the deferred ones by: "
            "the saving isn't worked out."
        )
    if len(ranked) > th.top_n:
        notes.append(f"{len(ranked) - th.top_n} more servers aren't in \"By MCP server\".")
    return Section(
        key="tool_search",
        title="What tool search saves",
        tables=[summary, by_server, _servers_table(stats)],
        notes=notes,
    )


SERVER_ASSUMPTIONS: tuple[str, ...] = (
    "an MCP server's tool names, instructions and tools sent in full are priced at each reply's cache read "
    "rate, so what keeping it cost is a lower bound: a reply that rebuilt the cache paid its write rate",
    "a server's kind comes from its name (a claude.ai connector, a plugin's server) or from the config "
    "snapshots (local, project, managed or user); a server named only by an ID in the desktop app is of "
    "unknown kind until it also appears under its claude.ai name",
)


def _servers_table(stats: ToolSearchStats) -> Table:
    """Every MCP server named in the window, whatever its status: not
    capped, so none goes missing."""
    return Table(
        name="tool_search_servers",
        title="Each MCP server",
        columns=[
            Column(key="server", label="MCP server", kind="str"),
            Column(key="kind", label="Kind", kind="str"),
            Column(key="status", label="Status", kind="str"),
            Column(key="main_sessions", label="Main sessions offered it", kind="int"),
            Column(key="subagent_runs", label="Subagent runs offered it", kind="int"),
            Column(key="uses", label="Uses", kind="int"),
            Column(key="first_seen_days", label="First offered, days before the end", kind="float"),
            Column(key="last_seen_days", label="Last offered, days before the end", kind="float"),
            Column(key="also_named", label="Also named", kind="str"),
            Column(key="sample_tools", label="Some of its tools", kind="str"),
            Column(key="config_name", label="Name in your config", kind="str"),
            Column(key="projects", label="Projects whose config has it", kind="str"),
            Column(key="list_usd", label="Its share of the name list", kind="money"),
            Column(key="instructions_usd", label="Its instructions", kind="money"),
            Column(key="definitions_usd", label="Its tools sent in full", kind="money"),
            Column(key="removable_usd", label="Cost of keeping it", kind="money"),
        ],
        rows=[
            [
                r.server,
                r.kind,
                r.status,
                r.main_sessions,
                r.subagent_runs,
                r.uses,
                r.first_seen_days,
                r.last_seen_days,
                ", ".join(r.aliases),
                ", ".join(r.samples),
                r.config_name,
                ", ".join(r.projects),
                r.list_usd,
                r.instructions_usd,
                r.definitions_usd,
                r.removable_usd,
            ]
            for r in stats.mcp_servers
        ],
    )


# -- rule -------------------------------------------------------------------


def _server_rows(report: ReportModel) -> list[dict]:
    """``tool_search_servers``'s rows as ``{column key: value}``."""
    for section in report.sections:
        if section.key != "tool_search":
            continue
        for table in section.tables:
            if table.name == "tool_search_servers":
                return [{col.key: value for col, value in zip(table.columns, row)} for row in table.rows]
    return []


def _num(value) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _evidence(label: str, value, section_key: str, table_name: str, row_key) -> tuple:
    return (label, value, f"{section_key}.{table_name}", row_key)


def server_label(server: str) -> str:
    """A server's name for a sentence: a claude.ai connector's own name
    ("Team Notes" for ``claude_ai_Team_Notes``), else its key."""
    if server.startswith(_CONNECTOR_PREFIX) and len(server) > len(_CONNECTOR_PREFIX):
        return server[len(_CONNECTOR_PREFIX):].replace("_", " ")
    return server


def _command_name(name: str) -> str:
    return name if re.fullmatch(r"[A-Za-z0-9_.-]+", name) else f'"{name}"'


def _where(projects: str) -> str:
    return f"in {projects}" if projects else "in the project that has it"


def server_fix(row: dict, many: bool = False) -> str:
    """How to turn the server in ``row`` off, for its kind. ``many``
    words it for several servers of that kind at once (only the kinds
    whose fix doesn't name the server are grouped)."""
    kind = row.get("kind")
    name = _command_name(str(row.get("config_name") or row.get("server") or ""))
    projects = str(row.get("projects") or "")
    it = "them" if many else "it"
    # Anthropic provides some connectors itself, which leaves nothing to
    # disconnect on claude.ai (Claude Code's MCP documentation).
    disconnect = (
        " disconnect any you added yourself at claude.ai/customize/connectors" if many
        else ", if you added it yourself, disconnect it at claude.ai/customize/connectors"
    )
    if kind == KIND_DESKTOP_CONNECTOR:
        return (
            f"in the desktop app, switch {'each' if many else 'it'} off under + > Connectors (new sessions then "
            f"start without {it}), or{disconnect} to remove {it} from claude.ai chat too"
        )
    if kind == KIND_CONNECTOR:
        return (
            f"run /mcp and disable {it} (that turns {it} off in the current project only), or{disconnect} "
            f"to remove {it} everywhere"
        )
    if kind == KIND_PLUGIN:
        return (
            f"run /mcp and disable {it} (that turns {it} off in the current project only), or turn off the "
            f"plugin that brings {it} with /plugin"
        )
    if kind == KIND_USER:
        return f"run `claude mcp remove {name} --scope user`"
    if kind == KIND_LOCAL:
        return f"{_where(projects)}, run `claude mcp remove {name} --scope local`"
    if kind == KIND_PROJECT:
        return (
            f"{_where(projects)}, add {name} to disabledMcpjsonServers in .claude/settings.local.json, which is "
            f"just yours; `claude mcp remove {name} --scope project` edits the shared .mcp.json, so use that "
            "only if your team agrees"
        )
    return ""


#: Kinds whose fix doesn't name the server, so servers of one kind share
#: a sentence on the card.
_GROUPED = frozenset({KIND_DESKTOP_CONNECTOR, KIND_CONNECTOR, KIND_PLUGIN})


def _names(labels: list[str]) -> str:
    if len(labels) <= 2:
        return " and ".join(labels)
    return f"{', '.join(labels[:-1])} and {labels[-1]}"


def _rule_unused_servers(report: ReportModel, th: ToolSearchThresholds) -> list[Recommendation]:
    """``mcp-unused-server``: every server ``compute_tool_search`` marked
    ``remove`` (offered in many main sessions, recently and for a while,
    never used, of a kind you can turn off), on one card, each with the
    fix for its kind. Amounts are left to ``advice.py``, which words them
    for the billing mode."""
    rows = [r for r in _server_rows(report) if r.get("status") == STATUS_REMOVE]
    total = sum(_num(r.get("removable_usd")) for r in rows)
    if not rows or total < th.unused_min_total_usd:
        return []
    rows.sort(key=lambda r: -_num(r.get("removable_usd")))
    labels = [server_label(str(r["server"])) for r in rows]
    # Servers whose fix doesn't name them (connectors, plugins) share
    # one sentence per kind; the rest get one each.
    groups: dict[str, list[int]] = {}
    for i, r in enumerate(rows):
        key = str(r.get("kind")) if r.get("kind") in _GROUPED else f"{r.get('kind')}:{i}"
        groups.setdefault(key, []).append(i)
    fixes = []
    for members in groups.values():
        first = rows[members[0]]
        many = len(members) > 1
        fix = server_fix(first, many=many)
        fixes.append(f"{_names([labels[i] for i in members])}: {fix}.")
    one = len(rows) == 1
    sessions = max(int(_num(r.get("main_sessions"))) for r in rows)
    evidence = []
    for label, r in zip(labels, rows):
        server = r["server"]
        evidence += [
            _evidence(f"{label}: cost of keeping it", r.get("removable_usd"), "tool_search", "tool_search_servers",
                      server),
            _evidence(f"{label}: main sessions that offered it", r.get("main_sessions"), "tool_search",
                      "tool_search_servers", server),
        ]
        if r.get("sample_tools"):
            evidence.append(_evidence(f"{label}: some of its tools", r["sample_tools"], "tool_search",
                                      "tool_search_servers", server))
        if r.get("also_named"):
            evidence.append(_evidence(f"{label}: also named", r["also_named"], "tool_search",
                                      "tool_search_servers", server))
    return [
        Recommendation(
            id="mcp-unused-server",
            severity="advice",
            category="workflow",
            archetypes=(),
            title=(
                f"{labels[0]} is an MCP server you never use" if one
                else f"{len(rows)} MCP servers you never use are loaded into your sessions"
            ),
            why=(
                f"{_names(labels)} {'was' if one else 'were'} offered in up to {sessions:,} main sessions and "
                f"Claude never used {'it' if one else 'them'}, yet every reply carried "
                f"{'its' if one else 'their'} tool names, instructions or tools."
            ),
            action=(
                " ".join(fixes)
                + " The saving starts with your next new session, or once a conversation is summarised."
            ),
            lever=None,
            # No settings file: the fixes are /mcp, the desktop app and
            # claude.ai, so no "Changes your user settings" chip.
            scope="",
            subject=",".join(sorted(str(r["server"]) for r in rows)),
            saving_usd=total,
            evidence=evidence,
        )
    ]


#: One rule function, ``(report, thresholds) -> list[Recommendation]``,
#: which ``recommend.recommend`` folds in (same convention as
#: ``carry.RULES``).
RULES: list[Callable[[ReportModel, ToolSearchThresholds], list[Recommendation]]] = [_rule_unused_servers]


__all__ = [
    "ASSUMPTIONS",
    "McpServerRow",
    "RULES",
    "SEARCH_TOOL",
    "SERVER_ASSUMPTIONS",
    "ServerRow",
    "ToolSearchStats",
    "ToolSearchThresholds",
    "build_section",
    "compute_tool_search",
    "server_fix",
    "server_label",
]

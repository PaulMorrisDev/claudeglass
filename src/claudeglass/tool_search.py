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

Same shape as ``hook_costs.py``: :class:`ToolSearchThresholds`,
:func:`compute_tool_search` and :func:`build_section`. There is no rule:
tool search is already on wherever this has anything to measure.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .model import Column, Section, Table, TranscriptResult, Turn
from .parse import BUILT_IN_TOOLS, tool_server
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

    @classmethod
    def from_config(cls, config: dict | None) -> "ToolSearchThresholds":
        data = config or {}
        if not isinstance(data, dict):
            data = {}
        nested = data.get("thresholds")
        if isinstance(nested, dict):
            data = nested
        kwargs: dict = {}
        if "tool_search_top_n" in data:
            try:
                kwargs["top_n"] = int(data["tool_search_top_n"])
            except (TypeError, ValueError):
                pass
        return cls(**kwargs)

    def describe(self) -> list[str]:
        return [f"The servers table lists the top {self.top_n}."]


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

    @property
    def net_usd(self) -> float:
        return self.gross_usd - self.list_usd - self.search_usd

    @property
    def measurable(self) -> bool:
        """Whether any definition was loaded, so sizes can be estimated."""
        return self.definitions_measured > 0

    def ranked(self) -> list[ServerRow]:
        return sorted(self.servers.values(), key=lambda s: (-s.saving_usd, -s.most_deferred, s.server))


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
    results: list[TranscriptResult], pricing: Pricing, thresholds: ToolSearchThresholds | None = None
) -> ToolSearchStats:
    """The tool search figures over ``results`` (every transcript in the
    window: main sessions and agents each get their own deferred list)."""
    stats = ToolSearchStats()
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
    notes = list(ASSUMPTIONS) + [f"Thresholds: {' '.join(th.describe())}"]
    if stats.replies and not measurable:
        notes.append(
            "No tool definition was loaded in this window, so there is nothing to size the deferred ones by: "
            "the saving isn't worked out."
        )
    if len(ranked) > th.top_n:
        notes.append(f"{len(ranked) - th.top_n} more servers aren't listed.")
    return Section(key="tool_search", title="What tool search saves", tables=[summary, by_server], notes=notes)


__all__ = [
    "ASSUMPTIONS",
    "SEARCH_TOOL",
    "ServerRow",
    "ToolSearchStats",
    "ToolSearchThresholds",
    "build_section",
    "compute_tool_search",
]

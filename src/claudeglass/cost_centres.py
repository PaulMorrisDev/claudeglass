"""Spend by cost centre (Phase 8a): where the money goes, not just who spent it.

Every priced reply is split into the cells of one matrix. Its rows are the
main session, the agents the main session started directly, the agents a
workflow started (``TranscriptMeta.kind == "workflow-agent"``, never the
agent type) and, on its own, each main session's first call -- the
"session start (1-hour write)", which writes the whole starting prompt at
the 1-hour price. Its columns are what the reply paid for:

- ``base_read``: cache read of the starting prompt every call carries (the
  first call's cache write plus its cache read, ``B``). A reply reads at
  most ``B`` tokens of it.
- ``above_read``: cache read above the base, the conversation so far.
- ``growth_write``: new content written to the cache, plus uncached input.
- ``rewrite``: the cache write of a reply ``recache.detect`` calls a
  rebuild, and ``post_compaction`` the write of the reply that follows a
  conversation summary (and of the estimated call that wrote it).
- ``output``: output and thinking, plus a server-tool fee.

Each component of a reply's price (``pricing.price_turn``) goes into exactly
one cell, so the cells add up to the Overview's total to the cent. The
cache write of a rebuild or a post-compaction reply is split into the part
that re-wrote the base (the prefix) and the part that re-wrote the
conversation, and the base-read cell is split into the parts of the
starting prompt that a setting or the harness decides
(:func:`compose_base`). Each part is counted once, in the lever that
removes it: MCP server definitions, deferred names and instructions under
the connector (but in a main session the servers built into the desktop
app apart, since no setting removes them there); built-in tools an agent
type rarely uses under its allowlist; CLAUDE.md and auto memory apart;
skills and hooks apart. What
nothing measured covers stays in a "not itemised" part that no setting is
known to change, as do the system prompt and the built-in tools every
agent is offered (the Artifact tool, PowerShell).

The 30-day and 7-day views count the replies from the newest reply back.
Each cell names the check that covers it (:func:`hint_for`) or says there
is no advice. The model-choice table (information only) lists agent type by
model tier by who chose the model, direct and workflow apart, with runs,
cost and the most a move to Sonnet could save.

Privacy: sizes, counts, amounts and the names the report already shows
(agent types, built-in tool names). No MCP server name is kept: the parts
are lever words.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Sequence

from . import agent_models, desktop_servers, recache, workstyle
from .calibration import Calibration
from .context_budget import ContextBudgetStats, StartupSizes, startup_sizes
from .model import Column, EventKind, ReportModel, Table, TranscriptResult, Turn
from .pricing import Pricing, price_turn

#: The rows, in display order. ``start`` is each main session's first call.
CENTRES = ("main", "direct", "workflow", "start")
CENTRE_LABELS = {
    "main": "Main session",
    "direct": "Direct agents",
    "workflow": "Workflow agents",
    "start": "Session start (1-hour write)",
}
#: The columns, in display order.
CELLS = ("base_read", "above_read", "growth_write", "rewrite", "post_compaction", "output")
CELL_LABELS = {
    "base_read": "Base read",
    "above_read": "Above-base read",
    "growth_write": "Growth write",
    "rewrite": "Rewrite",
    "post_compaction": "Post-compaction",
    "output": "Output and thinking",
}
#: The views: the whole report window, then the newest 30 and 7 days of it.
VIEWS = ("window", "30d", "7d")
VIEW_DAYS = {"30d": 30, "7d": 7}

#: Where a part of the base can be changed.
FIXED = "fixed"
CONTROLLABLE = "controllable"
LEVER_LABELS = {FIXED: "Harness-fixed", CONTROLLABLE: "Controllable"}
NO_SETTING = "no setting known"

#: The parts of the base, by id: (label, lever). ``tool:<name>`` parts
#: (one built-in tool's definition, harness-fixed) are added by name.
PARTS: dict[str, tuple[str, str]] = {
    "system": ("System prompt", FIXED),
    "tools_fixed": ("Other built-in tool definitions", FIXED),
    "allowlist": ("Built-in tools this agent type rarely uses", CONTROLLABLE),
    "connector": ("MCP servers: tools, names and instructions", CONTROLLABLE),
    "desktop_servers": ("MCP servers built into the desktop app", FIXED),
    "claude_md": ("CLAUDE.md files", CONTROLLABLE),
    "memory": ("Auto memory", CONTROLLABLE),
    "skills": ("Skills list", CONTROLLABLE),
    "hooks": ("Hook output at the start", CONTROLLABLE),
    "other": ("Not itemised", FIXED),
}
#: How many built-in tools are named on their own in the base split (the
#: largest by cost); the rest join ``tools_fixed``.
NAMED_TOOLS = 3
#: Two harness tools that are always named when the base holds them: they
#: are the largest definitions, and no setting is known to drop either.
ALWAYS_NAMED_TOOLS = ("tool:Artifact", "tool:PowerShell")
#: The split of a rewritten cache: the base re-written and the conversation.
SPLIT_PARTS = {"prefix": "Starting prompt (prefix)", "conversation": "Conversation"}

#: The checks (``quick_actions.CHECKS``) a part links to.
PART_CARDS = {
    "allowlist": "tools",
    "claude_md": "claude-md",
    "memory": "claude-md",
    "skills": "skills",
    "hooks": "hooks",
}
#: The connector's check differs: a main session switches a server off
#: (tool search), an agent type lists the servers it may use.
CONNECTOR_CARD_MAIN = "tool-search"
CONNECTOR_CARD_AGENT = "tools"

#: The check names, as the Overview words them.
CHECK_LABELS = {
    "tools": "Tools, MCP servers and skills",
    "skills": "Skills",
    "claude-md": "CLAUDE.md files",
    "hooks": "Hooks",
    "tool-search": "MCP tool search",
    "compaction": "Conversation summaries",
    "cache": "Cache lifetime",
    "tool-output": "Tool output",
    "effort": "Thinking effort",
}
NO_ADVICE = "none"

#: Tiers of the model-choice table, by ``workstyle.model_tier`` rank.
TIER_WORDS = {0: "haiku", 1: "sonnet", 2: "opus", 3: "fable"}
TIER_LABELS = {"haiku": "Haiku", "sonnet": "Sonnet", "opus": "Opus", "fable": "Fable", "unknown": "Unknown"}
_ABOVE_SONNET = 2
CHOSEN_LABELS = {
    "call": "Named in the call",
    "file": "Named in the agent file",
    "inherited": "Inherited",
    "not recorded": "Not recorded",
}
#: Types nothing can change the model of, or that name no type.
_SKIPPED_TYPES = frozenset({"fork", "unknown"})

def hint_for(centre: str, cell: str) -> str | None:
    """The check that covers a cell, or ``None`` when no advice does."""
    if cell == "base_read":
        return None if centre == "start" else "tools"
    if cell == "above_read":
        return None if centre == "start" else "compaction"
    if cell == "growth_write":
        return "cache" if centre == "start" else "tool-output"
    if cell == "rewrite":
        return "cache"
    if cell == "post_compaction":
        return "compaction"
    if cell == "output":
        return "effort"
    return None


# -- the result ---------------------------------------------------------------------------


@dataclass(slots=True)
class ModelRow:
    """One row of the model-choice table: runs of one agent type on one
    model tier, whose model was chosen the same way."""

    runs: int = 0
    cost: float = 0.0
    #: The same replies priced at Sonnet (0.0 when the tier is not above
    #: Sonnet or the rate card has no Sonnet).
    cost_on_sonnet: float = 0.0


@dataclass(slots=True)
class CostCentres:
    """The matrix in each view, the parts behind three of its cells and the
    model-choice rows."""

    #: view -> (centre, cell) -> list-price USD.
    matrix: dict[str, dict[tuple[str, str], float]] = field(
        default_factory=lambda: {view: {} for view in VIEWS}
    )
    #: (centre, cell, part id) -> USD, for the report window.
    parts: dict[tuple[str, str, str], float] = field(default_factory=dict)
    #: (centre, agent type, tier word, chosen by) -> row.
    models: dict[tuple[str, str, str, str], ModelRow] = field(default_factory=dict)
    sessions: int = 0
    #: Whether the parts of the base were worked out (they need the
    #: corpus's calibration; the tuning export leaves them out).
    itemised: bool = False

    def total(self, view: str = "window") -> float:
        return sum(self.matrix[view].values())

    def cell(self, centre: str, cell: str, view: str = "window") -> float:
        return self.matrix[view].get((centre, cell), 0.0)

    def centre_total(self, centre: str, view: str = "window") -> float:
        return sum(self.matrix[view].get((centre, cell), 0.0) for cell in CELLS)


# -- the base, split into parts --------------------------------------------------------------


def compose_base(
    sizes: StartupSizes, base_tokens: float, allowlist: Sequence[str] = (), *, main: bool = False
) -> dict[str, float]:
    """The base (``base_tokens``) as tokens per part id, adding up to
    ``base_tokens`` exactly. Every measured token goes in one part only:

    - an MCP server's definitions, deferred names and instructions, whatever
      else could also remove its definitions (an allowlist), are the
      ``connector``;
    - in a main session (``main``), the servers built into the desktop app
      (:mod:`desktop_servers`) are ``desktop_servers``, harness-fixed, since
      no setting removes them there; an agent's tools list can leave their
      tools out, so for an agent they stay the connector's;
    - a built-in tool named in ``allowlist`` (the tools an agent type is
      offered and rarely uses) is the ``allowlist``; any other built-in
      tool is harness-fixed, as ``tool:<name>``;
    - CLAUDE.md files and auto memory are apart; so are skills and hooks;
    - the roster and anything unmeasured are ``other``.

    A measured total above the base is scaled down to it.
    """
    if base_tokens <= 0:
        return {}
    removable = {name for name in allowlist if name in sizes.builtin_tools}
    parts: dict[str, float] = {}

    def add(key: str, tokens: float) -> None:
        if tokens > 0:
            parts[key] = parts.get(key, 0.0) + tokens

    add("system", sizes.system)
    for name, tokens in sizes.builtin_tools.items():
        add("allowlist" if name in removable else f"tool:{name}", tokens)
    for server in sorted({*sizes.server_definitions, *sizes.server_deferred, *sizes.server_instructions}):
        tokens = (
            sizes.server_definitions.get(server, 0.0)
            + sizes.server_deferred.get(server, 0.0)
            + sizes.server_instructions.get(server, 0.0)
        )
        add("desktop_servers" if main and desktop_servers.is_built_in(server) else "connector", tokens)
    add("claude_md", sizes.claude_md)
    add("memory", sizes.memory)
    add("skills", sizes.skills)
    add("hooks", sizes.hooks)
    measured = sum(parts.values())
    if measured > base_tokens:
        scale = base_tokens / measured
        return {key: tokens * scale for key, tokens in parts.items()}
    if base_tokens > measured:
        parts["other"] = base_tokens - measured
    return parts


def part_label(part: str) -> str:
    if part.startswith("tool:"):
        return f"{part[5:]} tool definition"
    if part in PARTS:
        return PARTS[part][0]
    return SPLIT_PARTS.get(part, part)


def part_lever(part: str) -> str:
    if part.startswith("tool:"):
        return FIXED
    return PARTS[part][1] if part in PARTS else ""


def part_card(part: str, centre: str) -> str:
    if part == "connector":
        return CONNECTOR_CARD_MAIN if centre in ("main", "start") else CONNECTOR_CARD_AGENT
    return PART_CARDS.get(part, "")


# -- compute ------------------------------------------------------------------------------------


def _parse_ts(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _priced(result: TranscriptResult) -> list[Turn]:
    """Every turn the Overview prices: replies and the estimated calls that
    wrote conversation summaries."""
    return [t for t in result.turns if t.turn_index > 0]


class _Collector:
    """Adds a cost to the views its reply falls in."""

    def __init__(self, cc: CostCentres, newest: datetime | None):
        self.cc = cc
        self.cuts = {
            view: (newest - timedelta(days=days)) if newest is not None else None for view, days in VIEW_DAYS.items()
        }

    def add(self, when: datetime | None, centre: str, cell: str, cost: float, part: str | None = None) -> None:
        if not cost:
            return
        key = (centre, cell)
        views = ["window"]
        if when is not None:
            views += [view for view, cut in self.cuts.items() if cut is not None and when >= cut]
        for view in views:
            matrix = self.cc.matrix[view]
            matrix[key] = matrix.get(key, 0.0) + cost
        if part is not None:
            pkey = (centre, cell, part)
            self.cc.parts[pkey] = self.cc.parts.get(pkey, 0.0) + cost


def _centre_of(result: TranscriptResult) -> str:
    kind = result.meta.kind
    if kind == "workflow-agent":
        return "workflow"
    return "main" if kind == "top-level" else "direct"


def _allowlist_for(result: TranscriptResult, stats: ContextBudgetStats | None, family: str) -> list[str]:
    """The built-in tools this agent type is offered and rarely uses: the
    ones its allowlist could drop. Empty for a main session."""
    if stats is None or result.meta.kind == "top-level":
        return []
    acc = stats.agents.get(result.meta.agent_type or "(unknown)")
    if acc is None:
        return []
    _chars, rows = acc.removable_chars(family)
    return [key for key, _offered, _used, _chars_each in rows if not key.startswith("mcp__")]


def _walk(
    result: TranscriptResult,
    centre_row: str,
    collector: _Collector,
    th: recache.RecacheThresholds,
    pricing: Pricing,
    composition: dict[str, float] | None,
    base_tokens: int,
) -> None:
    """Split every priced reply of ``result`` into the matrix.

    ``centre_row`` is where its replies go; a main session's first call goes
    to ``start`` instead. ``composition`` is the base's parts in tokens
    (``None`` when they weren't worked out)."""
    turns = _priced(result)
    if not turns:
        return
    rebuilds = {t.turn_index for t in recache.detect(result.turns, th)}
    is_main = result.meta.kind == "top-level"
    for turn in turns:
        breakdown = price_turn(turn, pricing.resolve_model(turn.model))
        when = _parse_ts(turn.ts)
        centre = "start" if is_main and turn.turn_index == 1 else centre_row

        # Cache read: the base first, then the conversation above it.
        reads = turn.cache_read_tokens
        base_read_tokens = min(base_tokens, reads) if reads > 0 else 0
        base_cost = breakdown.cache_read_cost * base_read_tokens / reads if reads > 0 else 0.0
        if composition and base_tokens > 0 and base_cost:
            assigned = 0.0
            for part, tokens in composition.items():
                share = base_cost * tokens / base_tokens
                assigned += share
                collector.add(when, centre, "base_read", share, part)
            # Rounding left over: keep the cell whole.
            collector.add(when, centre, "base_read", base_cost - assigned, None)
        else:
            collector.add(when, centre, "base_read", base_cost)
        collector.add(when, centre, "above_read", breakdown.cache_read_cost - base_cost)

        # Cache write: growth, unless the reply rebuilt the cache or
        # followed a conversation summary.
        write = breakdown.cache_write_cost
        after_summary = turn.estimated == "compaction" or EventKind.COMPACT_BOUNDARY in turn.preceding_event_kinds
        if centre != "start" and turn.turn_index > 1 and after_summary:
            cell = "post_compaction"
        elif centre != "start" and turn.turn_index in rebuilds:
            cell = "rewrite"
        else:
            cell = "growth_write"
        if cell == "growth_write" or turn.cache_creation_tokens <= 0:
            collector.add(when, centre, cell, write)
        else:
            prefix = min(max(base_tokens - reads, 0), turn.cache_creation_tokens)
            prefix_cost = write * prefix / turn.cache_creation_tokens
            collector.add(when, centre, cell, prefix_cost, "prefix")
            collector.add(when, centre, cell, write - prefix_cost, "conversation")

        collector.add(when, centre, "growth_write", breakdown.input_cost)
        collector.add(when, centre, "output", breakdown.output_cost + breakdown.server_tool_cost)


def _model_rows(
    results: Sequence[TranscriptResult], pricing: Pricing, agent_files: dict, cc: CostCentres
) -> None:
    sonnet_model = pricing.aliases.get("sonnet")
    sonnet_rates = pricing.models.get(sonnet_model) if sonnet_model else None
    for result in results:
        meta = result.meta
        if meta.kind not in ("subagent", "workflow-agent"):
            continue
        workflow = meta.kind == "workflow-agent"
        run_type = meta.agent_type or (agent_models.WORKFLOW_GROUP if workflow else None)
        if run_type is None or run_type in _SKIPPED_TYPES:
            continue
        turns = _priced(result)
        if not turns:
            continue
        counts = Counter(t.model for t in turns if t.model)
        model = counts.most_common(1)[0][0] if counts else ""
        tier = workstyle.model_tier(model, meta.agent_model_alias) if model or meta.agent_model_alias else -1
        tier_word = TIER_WORDS.get(tier, "unknown")
        chosen = (
            agent_models.model_chosen_by(meta.agent_model_alias, run_type, tier, agent_files)
            if meta.model_recorded
            else "not recorded"
        )
        row = cc.models.setdefault(("workflow" if workflow else "direct", run_type, tier_word, chosen), ModelRow())
        row.runs += 1
        for turn in turns:
            row.cost += price_turn(turn, pricing.resolve_model(turn.model)).total
            if tier >= _ABOVE_SONNET and sonnet_rates is not None:
                row.cost_on_sonnet += price_turn(turn, sonnet_rates).total


def compute(
    sessions: Sequence[tuple[TranscriptResult, Sequence[TranscriptResult]]],
    pricing: Pricing,
    th: recache.RecacheThresholds,
    *,
    calibration: Calibration | None = None,
    context_stats: ContextBudgetStats | None = None,
    agent_files: dict | None = None,
) -> CostCentres:
    """The cost-centre matrix over ``sessions`` (each a main session and
    its agents). Without a ``calibration`` the base is not split into parts
    and the model-choice rows are left out (the tuning export needs only
    the matrix)."""
    cc = CostCentres(sessions=len(sessions), itemised=calibration is not None)
    newest: datetime | None = None
    for top, subs in sessions:
        for result in (top, *subs):
            for turn in _priced(result):
                when = _parse_ts(turn.ts)
                if when is not None and (newest is None or when > newest):
                    newest = when
    collector = _Collector(cc, newest)
    for top, subs in sessions:
        for result in (top, *subs):
            first = next((t for t in result.turns if t.turn_index == 1), None)
            base_tokens = (first.cache_creation_tokens + first.cache_read_tokens) if first is not None else 0
            composition = None
            if calibration is not None and first is not None and base_tokens > 0:
                sizes = startup_sizes(result, calibration)
                if sizes is not None:
                    composition = compose_base(
                        sizes,
                        base_tokens,
                        _allowlist_for(result, context_stats, sizes.family),
                        main=result.meta.kind == "top-level",
                    )
            _walk(result, _centre_of(result), collector, th, pricing, composition, base_tokens)
        if calibration is not None:
            _model_rows(subs, pricing, agent_files or {}, cc)
    return cc


def model_choice(
    sessions: Sequence[tuple[TranscriptResult, Sequence[TranscriptResult]]],
    pricing: Pricing,
    agent_files: dict | None = None,
) -> dict[tuple[str, str, str, str], ModelRow]:
    """The model-choice rows of ``sessions`` alone, keyed by (started by,
    agent type, model tier word, who chose it): the rows
    :func:`compute` makes with a calibration, without the matrix or the
    parts. The tuning export reads these."""
    cc = CostCentres(sessions=len(sessions))
    for _top, subs in sessions:
        _model_rows(subs, pricing, agent_files or {}, cc)
    return cc.models


def matrix_usd(cc: CostCentres) -> dict[str, dict[str, float]]:
    """The report window's matrix as ``centre -> cell -> list-price USD``,
    rows and cells with spend only: the tuning export's block."""
    out: dict[str, dict[str, float]] = {}
    for centre in CENTRES:
        row = {cell: cc.cell(centre, cell) for cell in CELLS if cc.cell(centre, cell) > 0}
        if row:
            out[centre] = row
    return out


# -- tables -------------------------------------------------------------------------------------

CENTRES_TABLE = "cost_centres"
PARTS_TABLE = "cost_centres_parts"
ADVICE_TABLE = "cost_centres_advice"
MODELS_TABLE = "cost_centres_models"
SECTION = "agents"

_CHECK_VALUE_LABELS = {**CHECK_LABELS, NO_ADVICE: "No advice"}


def build_matrix_table(cc: CostCentres) -> Table:
    columns = [Column(key="centre", label="Cost centre", kind="str")]
    columns += [Column(key=cell, label=CELL_LABELS[cell], kind="money") for cell in CELLS]
    columns.append(Column(key="total", label="Total", kind="money"))
    rows = []
    if cc.total() > 0:
        for centre in CENTRES:
            values = [cc.cell(centre, cell) for cell in CELLS]
            rows.append([centre, *values, sum(values)])
    return Table(
        name=CENTRES_TABLE,
        title="Spend by cost centre",
        columns=columns,
        rows=rows,
        value_labels=dict(CENTRE_LABELS),
        lead_columns=["centre", *CELLS],
        notes=[
            "Every reply is split into these cells, so the rows add up to the total spend for the window.",
            "Base read is the starting prompt every reply reads again. Above-base read is the conversation on top of"
            " it. Growth write includes input that was not cached. Output includes a search fee.",
            "Session start is each main session's first call. It writes the starting prompt once, at the 1-hour"
            " price. An agent's first call is counted in its own row, as growth write.",
            "The parts of the base read, the parts of a rewrite and the advice for each cell are in the tables"
            " below. Fixes are on {{page:actions/checks}}.",
        ],
    )


def build_parts_table(cc: CostCentres) -> Table:
    columns = [
        Column(key="centre", label="Cost centre", kind="str"),
        Column(key="cell", label="Cell", kind="str"),
        Column(key="part", label="Part", kind="str"),
        Column(key="lever", label="Who decides it", kind="str"),
        Column(key="cost", label="Cost", kind="money"),
        Column(key="share", label="Share of cell", kind="pct"),
        Column(key="card", label="Check", kind="str"),
        Column(key="advice", label="Setting", kind="str"),
    ]
    named = _named_tools(cc)
    folded: dict[tuple[str, str, str], float] = {}
    for (centre, cell, part), cost in cc.parts.items():
        if part.startswith("tool:") and part not in named:
            part = "tools_fixed"
        key = (centre, cell, part)
        folded[key] = folded.get(key, 0.0) + cost
    rows = []
    for centre in CENTRES:
        for cell in ("base_read", "rewrite", "post_compaction"):
            entries = sorted(
                ((part, cost) for (c, k, part), cost in folded.items() if c == centre and k == cell and cost > 0),
                key=lambda item: (-item[1], item[0]),
            )
            cell_total = sum(cost for _part, cost in entries)
            for part, cost in entries:
                lever = part_lever(part)
                rows.append(
                    [
                        centre,
                        cell,
                        part_label(part),
                        lever or None,
                        cost,
                        100.0 * cost / cell_total if cell_total else None,
                        part_card(part, centre) if lever == CONTROLLABLE else "",
                        NO_SETTING if lever == FIXED else "",
                    ]
                )
    return Table(
        name=PARTS_TABLE,
        title="What the base read, rewrites and post-compaction writes are made of",
        columns=columns,
        rows=rows,
        value_labels={
            **CENTRE_LABELS,
            **CELL_LABELS,
            **LEVER_LABELS,
            **CHECK_LABELS,
        },
        lead_columns=["centre", "cell", "part", "cost", "share", "card"],
        notes=[
            "Each part is counted once, under the setting that removes it. MCP server tools, names and"
            " instructions are one part, even where an allowlist could also drop the tool definitions.",
            "In the main session, the MCP servers built into the desktop app are a part of their own, since no"
            " setting removes them there.",
            "A rewrite or a post-compaction write is split into the starting prompt it wrote again and the"
            " conversation it wrote again.",
            "Sizes are estimates from characters, at the characters per token measured on your own sessions."
            " A resumed session's first call carries its old conversation, which makes its base look larger.",
        ],
    )


def _named_tools(cc: CostCentres) -> set[str]:
    """The built-in tools shown on their own: the largest by cost."""
    totals: dict[str, float] = {}
    for (_centre, _cell, part), cost in cc.parts.items():
        if part.startswith("tool:"):
            totals[part] = totals.get(part, 0.0) + cost
    ranked = sorted(totals, key=lambda part: (-totals[part], part))
    return set(ranked[:NAMED_TOOLS]) | {part for part in ALWAYS_NAMED_TOOLS if part in totals}


def build_advice_table(cc: CostCentres) -> Table:
    columns = [
        Column(key="centre", label="Cost centre", kind="str"),
        Column(key="cell", label="Cell", kind="str"),
        Column(key="cost", label="Window", kind="money"),
        Column(key="cost_30d", label="Newest 30 days", kind="money"),
        Column(key="cost_7d", label="Newest 7 days", kind="money"),
        Column(key="hint", label="Advice", kind="str"),
    ]
    rows = []
    for centre in CENTRES:
        for cell in CELLS:
            window = cc.cell(centre, cell)
            if window <= 0:
                continue
            rows.append(
                [
                    centre,
                    cell,
                    window,
                    cc.cell(centre, cell, "30d"),
                    cc.cell(centre, cell, "7d"),
                    hint_for(centre, cell) or NO_ADVICE,
                ]
            )
    return Table(
        name=ADVICE_TABLE,
        title="Cost centres in the newest 30 and 7 days, and what advises on each",
        columns=columns,
        rows=rows,
        value_labels={**CENTRE_LABELS, **CELL_LABELS, **_CHECK_VALUE_LABELS},
        lead_columns=["centre", "cell", "cost", "cost_30d", "cost_7d", "hint"],
        notes=[
            "The 30 and 7 days count back from the newest reply in the window. A cell names the check that covers"
            " it, or says there is no advice. A check that names a cell does not mean it found a saving."
            " See {{page:actions/checks}}.",
        ],
    )


def build_models_table(cc: CostCentres) -> Table:
    columns = [
        Column(key="centre", label="Started by", kind="str"),
        Column(key="agent_type", label="Agent type", kind="str"),
        Column(key="tier", label="Model", kind="str"),
        Column(key="chosen", label="Who chose it", kind="str"),
        Column(key="runs", label="Runs", kind="int"),
        Column(key="cost", label="Cost", kind="money"),
        Column(key="ceiling", label="Sonnet ceiling", kind="money"),
    ]
    centre_rank = {"direct": 0, "workflow": 1}
    rows = [
        [
            centre,
            agent_type,
            tier,
            chosen,
            row.runs,
            row.cost,
            max(row.cost - row.cost_on_sonnet, 0.0) if row.cost_on_sonnet > 0 else None,
        ]
        for (centre, agent_type, tier, chosen), row in sorted(
            cc.models.items(), key=lambda item: (centre_rank[item[0][0]], -item[1].cost, item[0][1:])
        )
    ]
    return Table(
        name=MODELS_TABLE,
        title="Model choice by agent type",
        columns=columns,
        rows=rows,
        value_labels={
            "direct": "Main session",
            "workflow": "Workflow",
            **{tier: label for tier, label in TIER_LABELS.items()},
            **CHOSEN_LABELS,
        },
        notes=[
            "For information only: there is no card for this. The Sonnet ceiling is the most that moving a run to"
            " Sonnet could save, with the same tokens at Sonnet's list price. Sonnet may need more replies, so it is"
            " never a forecast.",
            "Who chose it: the call that started the agent, its agent file, or nobody, so it took the session's"
            " model. Older runs do not record this.",
        ],
    )


def build_tables(cc: CostCentres) -> list[Table]:
    return [build_matrix_table(cc), build_parts_table(cc), build_advice_table(cc), build_models_table(cc)]


# -- reading the table back (the advice rules) -------------------------------------------------


def largest_centre(report: ReportModel) -> tuple[str, str, float, float] | None:
    """The cost centre with the most spend in the window, as ``(centre key,
    its largest cell key, its total, its share of all spend as 0-100)``;
    ``None`` when the table is absent or empty. Reads the rendered table,
    as the advice rules do."""
    for section in report.sections:
        if section.key != SECTION:
            continue
        for table in section.tables:
            if table.name != CENTRES_TABLE or not table.rows:
                continue
            keys = [column.key for column in table.columns]
            if "total" not in keys:
                return None
            total_idx = keys.index("total")
            grand = sum(row[total_idx] for row in table.rows if isinstance(row[total_idx], (int, float)))
            best = max(
                (row for row in table.rows if isinstance(row[total_idx], (int, float))),
                key=lambda row: row[total_idx],
                default=None,
            )
            if best is None or grand <= 0:
                return None
            cells = [(best[keys.index(cell)], cell) for cell in CELLS if cell in keys]
            top_cell = max(cells, key=lambda item: item[0])[1]
            return str(best[0]), top_cell, float(best[total_idx]), 100.0 * best[total_idx] / grand
    return None


__all__ = [
    "ADVICE_TABLE",
    "CELLS",
    "CELL_LABELS",
    "CENTRES",
    "CENTRE_LABELS",
    "CENTRES_TABLE",
    "CHECK_LABELS",
    "CostCentres",
    "MODELS_TABLE",
    "PARTS_TABLE",
    "SECTION",
    "build_tables",
    "compose_base",
    "compute",
    "hint_for",
    "largest_centre",
    "matrix_usd",
]

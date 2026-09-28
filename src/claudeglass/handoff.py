"""Start building in a fresh session once a big plan is approved
(``plan_handoff``): what the replies after an approved plan would have
cost had they started from the plan alone, not from everything the
planning read.

The model, per main session and per approved plan (an ``ExitPlanMode``
call whose result came back without an error, ``Turn.plan_stats``):

- **What a fresh session starts with.** The session's own starting
  context (:func:`starting_context`: its first reply's context less your
  first message) plus the plan itself (``plan_stats.chars`` / 4).
- **What it would drop.** The approving reply's context less that fresh
  start. A plan qualifies only when that is at least
  ``min_dropped_tokens`` and at least ``min_later_turns`` replies follow.
- **The saving.** Every later reply, until the session's next
  conversation summary (compaction) or its next approved plan, priced
  with the dropped tokens taken out of its context
  (``compaction_sim._shrunk_cost``, the same shrink the compaction replay
  uses), less two costs a fresh start adds back: the first reply writes
  the fresh context to the cache instead of reading it, and the session
  re-reads some files it had already read (compaction_sim's rediscovery
  allowance, per real summary in this corpus). An upper bound: a fresh
  session may need more than the plan.

``/branch`` and ``claude --continue --fork-session`` copy the whole
conversation, so forking saves none of this; the tip says ``/clear``.

Each approved plan's build (the replies after it, up to the next
``ExitPlanMode`` call) is also priced as it ran and at Sonnet's rates:
``plan_handoff_summary``'s ``build_usd`` / ``build_usd_sonnet``, for the
"plan on Opus, build on Sonnet" estimate (``whatif``), which reads report
tables, never transcripts.

Same shape as ``carry.py``: :class:`HandoffThresholds`,
:func:`compute_handoff`, :func:`build_section` and :data:`RULES` (folded
into ``recommend.recommend``). Never imports ``habits``: ``habits``
imports :func:`starting_context` from here.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Callable

from .compaction_sim import _shrunk_cost
from .model import (
    Column,
    EventKind,
    Recommendation,
    ReportModel,
    Section,
    Table,
    TranscriptResult,
    Turn,
    scheduled_main_session,
)
from .pricing import ModelRates, Pricing, ResolvedRates, price_turn

#: Characters per token, the approximation used throughout this report
#: (duplicated per module by convention, see ``carry.py``).
_CHARS_PER_TOKEN_APPROX = 4

ASSUMPTIONS: tuple[str, ...] = (
    "a build started fresh from an approved plan carries the session's starting context plus the plan "
    "itself, and nothing else the planning read",
    "the plan-handoff saving counts the replies after the plan until the next conversation summary or "
    "the next approved plan, and takes off the first reply's cache write and one file re-read allowance",
    "the plan-handoff saving overlaps with the auto-compact saving: both come from carrying less context "
    "in later replies",
)

RatesArg = ModelRates | ResolvedRates | None


@dataclass(slots=True)
class HandoffThresholds:
    """Every tunable number the plan-handoff check depends on (same
    ``from_config``/``describe`` convention as ``carry.CarryThresholds``).
    Config keys carry a ``plan_handoff_`` prefix: ``[thresholds]`` is one
    flat table shared by every module."""

    #: A plan qualifies only when a fresh start would have dropped at
    #: least this many tokens of context.
    min_dropped_tokens: float = 40_000.0
    #: ...and at least this many replies followed it before the next
    #: summary or plan.
    min_later_turns: int = 10
    #: The tip needs at least this many sessions with a qualifying plan.
    min_sessions: int = 3
    #: ...and a saving of at least this share (%) of main-session cost.
    min_saving_share_pct: float = 1.0
    #: How many sessions ``plan_handoff_by_session`` lists.
    top_n: int = 20

    @classmethod
    def from_config(cls, config: dict | None) -> "HandoffThresholds":
        data = config or {}
        if not isinstance(data, dict):
            data = {}
        nested = data.get("thresholds")
        if isinstance(nested, dict):
            data = nested
        kwargs: dict = {}
        for name, cast in (
            ("min_dropped_tokens", float),
            ("min_later_turns", int),
            ("min_sessions", int),
            ("min_saving_share_pct", float),
            ("top_n", int),
        ):
            key = f"plan_handoff_{name}"
            if key in data:
                try:
                    kwargs[name] = cast(data[key])
                except (TypeError, ValueError):
                    pass
        return cls(**kwargs)

    def describe(self) -> list[str]:
        return [
            f"A plan counts for the fresh-session tip when starting fresh would drop at least "
            f"{self.min_dropped_tokens:,.0f} tokens and at least {self.min_later_turns} replies follow it.",
            f"The fresh-session tip needs at least {self.min_sessions} such sessions and a saving of at least "
            f"{self.min_saving_share_pct:.1f}% of main-session cost.",
            f"The sessions table lists the top {self.top_n}.",
        ]


_DEFAULT_THRESHOLDS = HandoffThresholds()


def starting_context(priced: list[Turn]) -> int:
    """The context a session carries before your first message: its first
    priced reply's context less that message (characters / 4). Claude
    Code's system prompt, tools and CLAUDE.md, which a fresh session
    starts with too. Shared with ``habits``."""
    if not priced:
        return 0
    first = priced[0]
    return max(0, first.ctx - (first.human_prompt_chars or 0) // _CHARS_PER_TOKEN_APPROX)


def _approved(turn: Turn) -> bool:
    return turn.plan_stats is not None and turn.plan_stats.outcome == "approved"


def _fresh(start: int, plan_turn: Turn) -> int:
    """The context a fresh session started from the plan would carry."""
    return start + plan_turn.plan_stats.chars // _CHARS_PER_TOKEN_APPROX


def plan_carried(priced: list[Turn]) -> int | None:
    """The most planning context any approved plan in ``priced`` (a main
    session's replies) kept into the build: what a fresh start from the
    plan would have dropped. ``None`` without an approved plan. Shared
    with ``habits``."""
    approved = [t for t in priced if _approved(t)]
    if not approved:
        return None
    start = starting_context(priced)
    return max(max(0, t.ctx - _fresh(start, t)) for t in approved)


def plan_shape(priced: list[Turn]) -> str:
    """``"plan_build"`` when a plan was approved and files were edited
    after it in ``priced``, ``"plan_only"`` when a plan was approved but
    nothing was edited after it, ``"no_plan"`` otherwise."""
    at = next((i for i, t in enumerate(priced) if _approved(t)), None)
    if at is None:
        return "no_plan"
    return "plan_build" if any(t.edit_kind == "real" for t in priced[at + 1 :]) else "plan_only"


def _fresh_start_cost(turn: Turn, rates: RatesArg, fresh: int) -> float:
    """What the first reply after ``/clear`` adds: it writes the fresh
    context to the cache (5-minute) where the real reply read it."""
    if fresh <= 0:
        return 0.0
    written = price_turn(turn, rates, write_split={"5m": fresh}, read_tokens=0).total
    read = price_turn(turn, rates, write_split={}, read_tokens=fresh).total
    return max(0.0, written - read)


@dataclass(slots=True)
class PlanHandoff:
    """One approved plan in a main session: counts and costs only."""

    session_id: str
    turn_index: int
    tokens_carried: int
    later_turns: int
    saving_usd: float
    qualifies: bool


@dataclass(slots=True)
class SessionHandoff:
    """A main session with at least one approved plan."""

    session_id: str
    plans: int = 0
    #: The most context any of its plans would have dropped.
    tokens_carried: int = 0
    later_turns: int = 0
    qualifying_plans: int = 0
    saving_usd: float = 0.0
    build_turns: int = 0
    build_usd: float = 0.0
    #: The build at Sonnet's rates; ``None`` when the rate card has no
    #: ``sonnet`` alias.
    build_usd_sonnet: float | None = None


@dataclass(slots=True)
class HandoffStats:
    main_sessions: int = 0
    main_session_usd: float = 0.0
    sessions: list[SessionHandoff] = field(default_factory=list)
    plans: list[PlanHandoff] = field(default_factory=list)
    rediscovery_allowance_usd: float = 0.0
    sonnet_available: bool = False


def _session(
    tr: TranscriptResult, lookup, sonnet: RatesArg, allowance: float, th: HandoffThresholds, stats: HandoffStats
) -> None:
    priced = [t for t in tr.turns if t.turn_index > 0]
    if not priced:
        return
    stats.main_sessions += 1
    costs = [price_turn(t, lookup(t.model)).total for t in priced]
    stats.main_session_usd += sum(costs)
    approved = [i for i, t in enumerate(priced) if _approved(t)]
    if not approved:
        return

    start = starting_context(priced)
    plan_calls = [i for i, t in enumerate(priced) if t.plan_stats is not None]
    row = SessionHandoff(session_id=tr.meta.session_id, build_usd_sonnet=0.0 if sonnet is not None else None)
    n = len(priced)
    for i in approved:
        plan_turn = priced[i]
        fresh = _fresh(start, plan_turn)
        dropped = max(0, plan_turn.ctx - fresh)

        # The handoff window: until the next summary or approved plan.
        end = i + 1
        while end < n and EventKind.COMPACT_BOUNDARY not in priced[end].preceding_event_kinds and not _approved(
            priced[end]
        ):
            end += 1
        later = range(i + 1, end)
        saving = 0.0
        if dropped > 0 and len(later) > 0:
            gross = sum(costs[j] - _shrunk_cost(priced[j], lookup(priced[j].model), dropped) for j in later)
            first = priced[i + 1]
            saving = max(0.0, gross - _fresh_start_cost(first, lookup(first.model), fresh) - allowance)
        qualifies = dropped >= th.min_dropped_tokens and len(later) >= th.min_later_turns and saving > 0
        stats.plans.append(
            PlanHandoff(tr.meta.session_id, plan_turn.turn_index, dropped, len(later), saving if qualifies else 0.0, qualifies)
        )

        # The build: until the next plan, approved or not.
        build_end = next((k for k in plan_calls if k > i), n)
        for j in range(i + 1, build_end):
            row.build_turns += 1
            row.build_usd += costs[j]
            if sonnet is not None:
                row.build_usd_sonnet += price_turn(priced[j], sonnet).total

        row.plans += 1
        row.tokens_carried = max(row.tokens_carried, dropped)
        row.later_turns += len(later)
        if qualifies:
            row.qualifying_plans += 1
            row.saving_usd += saving
    stats.sessions.append(row)


def compute_handoff(
    results: list[TranscriptResult],
    pricing: Pricing,
    thresholds: HandoffThresholds | None = None,
    rediscovery_allowance_usd: float = 0.0,
) -> HandoffStats:
    """The plan-handoff figures over ``results`` (every transcript in the
    window; only main sessions are read, and scheduled ones are left
    out). ``rediscovery_allowance_usd``: the cost of re-reading files
    after a fresh start, per plan; ``build_report`` passes the one
    compaction_sim measured from this corpus's real summaries."""
    th = thresholds or _DEFAULT_THRESHOLDS
    lookup = pricing.resolve_model
    sonnet_id = pricing.aliases.get("sonnet")
    sonnet = pricing.resolve_model(sonnet_id) if sonnet_id else None
    stats = HandoffStats(rediscovery_allowance_usd=rediscovery_allowance_usd, sonnet_available=sonnet is not None)
    for tr in results:
        if tr.meta.kind != "top-level" or scheduled_main_session(tr):
            continue
        _session(tr, lookup, sonnet, rediscovery_allowance_usd, th, stats)
    stats.sessions.sort(key=lambda s: (-s.saving_usd, -s.tokens_carried, s.session_id))
    return stats


# -- report section -----------------------------------------------------------


def build_section(stats: HandoffStats, thresholds: HandoffThresholds | None = None) -> Section:
    th = thresholds or _DEFAULT_THRESHOLDS
    qualifying = [s for s in stats.sessions if s.qualifying_plans]
    carried = [p.tokens_carried for p in stats.plans if p.qualifies]
    saving = sum(s.saving_usd for s in stats.sessions)
    summary = Table(
        name="plan_handoff_summary",
        title="Building in a fresh session after a big plan",
        columns=[
            Column(key="scope", label="Scope", kind="str"),
            Column(key="main_sessions", label="Main sessions", kind="int"),
            Column(key="sessions_with_plan", label="Sessions with an approved plan", kind="int"),
            Column(key="qualifying_sessions", label="Sessions where it pays", kind="int"),
            Column(key="tokens_carried_median", label="Planning context kept (median)", kind="tokens"),
            Column(key="saving_usd", label="Most you could save", kind="money"),
            Column(key="saving_pct", label="Share of main-session cost", kind="pct"),
            Column(key="main_session_usd", label="Main-session cost", kind="money"),
            Column(key="build_usd", label="Cost after approved plans", kind="money"),
            Column(key="build_usd_sonnet", label="Same at Sonnet's prices", kind="money"),
        ],
        rows=[
            [
                "main sessions",
                stats.main_sessions,
                len(stats.sessions),
                len(qualifying),
                int(statistics.median(carried)) if carried else None,
                saving,
                (100.0 * saving / stats.main_session_usd) if stats.main_session_usd > 0 else None,
                stats.main_session_usd,
                sum(s.build_usd for s in stats.sessions),
                sum(s.build_usd_sonnet or 0.0 for s in stats.sessions) if stats.sonnet_available else None,
            ]
        ],
    )
    by_session = Table(
        name="plan_handoff_by_session",
        title="Sessions with an approved plan",
        columns=[
            Column(key="session", label="Session", kind="str"),
            Column(key="plans", label="Approved plans", kind="int"),
            Column(key="tokens_carried", label="Planning context kept", kind="tokens"),
            Column(key="later_turns", label="Replies after the plan", kind="int"),
            Column(key="qualifies", label="Worth a fresh session", kind="str"),
            Column(key="saving_usd", label="Most you could save", kind="money"),
            Column(key="build_turns", label="Build replies", kind="int"),
            Column(key="build_usd", label="Build cost", kind="money"),
            Column(key="build_usd_sonnet", label="Build cost at Sonnet's prices", kind="money"),
        ],
        rows=[
            [
                s.session_id,
                s.plans,
                s.tokens_carried,
                s.later_turns,
                "yes" if s.qualifying_plans else "no",
                s.saving_usd,
                s.build_turns,
                s.build_usd,
                s.build_usd_sonnet,
            ]
            for s in stats.sessions[: th.top_n]
        ],
    )
    notes = list(ASSUMPTIONS) + [
        f"File re-read allowance per fresh start: ${stats.rediscovery_allowance_usd:.4f} at list price.",
        f"Thresholds: {' '.join(th.describe())}",
    ]
    if len(stats.sessions) > th.top_n:
        notes.append(f"{len(stats.sessions) - th.top_n} more sessions with an approved plan aren't listed.")
    return Section(key="plan_handoff", title="Building in a fresh session after a big plan", tables=[summary, by_session], notes=notes)


# -- recommendation rule -------------------------------------------------


def _table(report: ReportModel, section_key: str, table_name: str) -> Table | None:
    for section in report.sections:
        if section.key != section_key:
            continue
        for table in section.tables:
            if table.name == table_name:
                return table
    return None


def _col_index(table: Table, column_key: str) -> int | None:
    for i, col in enumerate(table.columns):
        if col.key == column_key:
            return i
    return None


def _evidence(label: str, value, section_key: str, table_name: str, row_key) -> tuple:
    return (label, value, f"{section_key}.{table_name}", row_key)


#: /cl-feedback handoff answers (or rated pieces) needed before your
#: feedback changes the card.
MIN_FEEDBACK_ANSWERS = 3


def _cells(table: Table | None, row_key: str) -> dict:
    """``table``'s row ``row_key`` as ``{column key: value}``, or ``{}``."""
    if table is None:
        return {}
    for row in table.rows:
        if row and row[0] == row_key:
            return {col.key: value for col, value in zip(table.columns, row)}
    return {}


def _feedback_on_plans(report: ReportModel) -> dict:
    """What your /cl-feedback answers said about sessions you planned and
    built in (``habits_by_shape``'s ``plan_build`` row): ``yes``,
    ``partly`` and ``no`` handoff counts, and the rated pieces and the
    share too costly."""
    cells = _cells(_table(report, "habits", "habits_by_shape"), "plan_build")
    counts = {word: cells.get(f"handoff_{word}") for word in ("yes", "partly", "no")}
    counts = {word: n if isinstance(n, int) else 0 for word, n in counts.items()}
    pieces = cells.get("pieces")
    costly = cells.get("costly_pct")
    return {
        **counts,
        "answers": sum(counts.values()),
        "pieces": pieces if isinstance(pieces, int) else 0,
        "costly_pct": costly if isinstance(costly, (int, float)) else None,
    }


def _rule_plan_handoff(report: ReportModel, th: HandoffThresholds) -> list[Recommendation]:
    """``plan-handoff``: in at least ``min_sessions`` main sessions the
    build after an approved plan carried a lot of planning context, and
    starting it fresh would have saved at least ``min_saving_share_pct``
    of main-session cost. ``advice`` words the saving.

    Your /cl-feedback answers change the card once there are at least
    :data:`MIN_FEEDBACK_ANSWERS`: when more than half say the build
    relied on the earlier discussion, it suggests writing fuller plans
    first; when more than half say the plan was enough, it cites them;
    when most rated planned builds were too costly, it says so."""
    table = _table(report, "plan_handoff", "plan_handoff_summary")
    if table is None or not table.rows:
        return []
    row = table.rows[0]

    def cell(key):
        idx = _col_index(table, key)
        return row[idx] if idx is not None and idx < len(row) else None

    sessions = cell("qualifying_sessions")
    saving = cell("saving_usd")
    share = cell("saving_pct")
    carried = cell("tokens_carried_median")
    if not isinstance(sessions, int) or sessions < th.min_sessions:
        return []
    if not isinstance(saving, (int, float)) or saving <= 0:
        return []
    if not isinstance(share, (int, float)) or share < th.min_saving_share_pct:
        return []
    carried_text = f"about {carried:,} tokens" if isinstance(carried, int) else "a lot"
    fb = _feedback_on_plans(report)
    enough_answers = fb["answers"] >= MIN_FEEDBACK_ANSWERS
    needs_discussion = enough_answers and fb["no"] * 2 > fb["answers"]
    plan_enough = enough_answers and fb["yes"] * 2 > fb["answers"]
    too_costly = (
        fb["pieces"] >= MIN_FEEDBACK_ANSWERS and fb["costly_pct"] is not None and fb["costly_pct"] > 50
    )

    title = "Start building in a fresh session once a big plan is approved"
    why = (
        f"In {sessions} sessions you kept {carried_text} of planning in context after approving the plan, "
        "and every later reply paid to read it again."
    )
    action = (
        "When a plan is approved after a lot of exploring, run /clear and ask Claude to carry out the plan "
        "file (Claude Code saves it under ~/.claude/plans), one phase per session."
    )
    if needs_discussion:
        title = "Write fuller plans, then build in a fresh session"
        why += (
            f" You said {fb['no']} of {fb['answers']} builds relied on the earlier discussion, so a fresh start "
            "would have lost what they needed. The saving needs a plan that carries it."
        )
        action = (
            "Before you approve a plan, ask Claude to add the decisions, file paths and constraints the build "
            "needs. Then run /clear and ask Claude to carry out the plan file (saved under ~/.claude/plans)."
        )
    elif plan_enough:
        why += f" You said {fb['yes']} of {fb['answers']} builds could have started from the plan."
    if too_costly:
        why += f" You also said {fb['costly_pct']:.0f}% of the planned builds you rated cost too many tokens."
    why += (
        " Overlaps with the compaction tip: together they save less than the two figures added up. It also "
        "overlaps with the habit of splitting large asks."
    )
    action += " Forking with /branch copies the whole conversation, so it doesn't save anything."

    evidence = [
        _evidence("Sessions where a fresh start pays", sessions, "plan_handoff", "plan_handoff_summary", "main sessions"),
        _evidence("Planning context kept (median tokens)", carried, "plan_handoff", "plan_handoff_summary", "main sessions"),
        _evidence("Most you could save", saving, "plan_handoff", "plan_handoff_summary", "main sessions"),
        _evidence("Share of main-session cost", share, "plan_handoff", "plan_handoff_summary", "main sessions"),
    ]
    if enough_answers:
        evidence += [
            _evidence("Builds you said the plan was enough for", fb["yes"], "habits", "habits_by_shape", "plan_build"),
            _evidence("Builds you said needed the discussion", fb["no"], "habits", "habits_by_shape", "plan_build"),
        ]
    if too_costly:
        evidence.append(
            _evidence("Planned builds you said were too costly", fb["costly_pct"], "habits", "habits_by_shape", "plan_build")
        )
    return [
        Recommendation(
            id="plan-handoff",
            severity="advice",
            category="workflow",
            archetypes=(),
            title=title,
            why=why,
            action=action,
            lever=None,
            saving_usd=float(saving),
            evidence=evidence,
        )
    ]


RULES: list[Callable[[ReportModel, HandoffThresholds], list[Recommendation]]] = [_rule_plan_handoff]


__all__ = [
    "ASSUMPTIONS",
    "HandoffThresholds",
    "HandoffStats",
    "PlanHandoff",
    "SessionHandoff",
    "starting_context",
    "plan_carried",
    "plan_shape",
    "compute_handoff",
    "build_section",
    "RULES",
]

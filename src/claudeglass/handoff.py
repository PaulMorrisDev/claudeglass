"""Start building in a fresh session once a big plan is approved
(``plan_handoff``): what the replies after an approved plan would have
cost had they started from the plan alone, not from everything the
planning read.

The model, per main session and per approved plan (an ``ExitPlanMode``
call whose result came back without an error, or one you approved by
typing a go-ahead or leaving plan mode, ``Turn.plan_stats``):

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

Each approved plan is also compared by how its build began
(:class:`PlanApproval`, ``plan_handoff_approvals``): in the same session, with
the planning context carried on (``kept``); after a /clear within
:data:`FRESH_CLEAR_S` seconds of the approval, in this session or in the next
one you opened (``cleared``); or in a session whose first message is the plan
itself (``handoff``, ``Turn.human_plan_handoff``). A fresh build carries none
of the planning, so its replies read less. Approvals count whether you clicked
them in the dialog or typed a go-ahead; a plan you declined and then told
Claude to carry out is an approval too. :func:`plan_groups` finds them, and
the plans sent back before each one: ``habits`` builds the plan-rounds table
from the same groups.

Same shape as ``carry.py``: :class:`HandoffThresholds`,
:func:`compute_handoff`, :func:`build_section` and :data:`RULES` (folded
into ``recommend.recommend``). Never imports ``habits``: ``habits``
imports :func:`starting_context` from here.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from . import capture as capture_mod
from .compaction_sim import _shrunk_cost
from .model import (
    Column,
    EventKind,
    PlanStats,
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

#: A /clear this many seconds after you approved a plan, or fewer, starts
#: its build fresh.
FRESH_CLEAR_S = 60.0

#: A session whose first message is the plan itself is the build of the
#: latest plan approved before it in the same project, when it began no more
#: than this many seconds after the approval.
HANDOFF_LINK_S = 3600.0

#: More replies than this editing files between two plans, and the second
#: plan is a new ask: a build began without an approval. Claude edits the
#: plan file between rounds, and that counts as an edit to the parser.
PLAN_BUILD_REPLIES = 8

#: How a build began, in the order ``plan_handoff_approvals`` lists them.
START_WORDS = ("kept", "cleared", "handoff")
START_LABELS = {
    "kept": "Carried on in the same session",
    "cleared": "Cleared right after the approval",
    "handoff": "Started from the plan",
}

ASSUMPTIONS: tuple[str, ...] = (
    "a build started fresh from an approved plan carries the session's starting context plus the plan "
    "itself, and nothing else the planning read",
    "the plan-handoff saving counts the replies after the plan until the next conversation summary or "
    "the next approved plan, and takes off the first reply's cache write and one file re-read allowance",
    "the plan-handoff saving overlaps with the auto-compact saving: both come from carrying less context "
    "in later replies",
    "a build started fresh when a /clear came within a minute of the approval, or when a session in the same "
    "project began with the plan itself within an hour of it; an approval from before the parser recorded "
    "its time can't be told from one that carried on",
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
    """Whether this reply's plan was approved: by the dialog, or by a
    go-ahead message or leaving plan mode instead (``approved_by_message``,
    set by ``parse.py``)."""
    return turn.plan_stats is not None and turn.plan_stats.outcome in ("approved", "approved_by_message")


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


@dataclass(slots=True)
class PlanGroup:
    """The plans Claude put up for one ask in a main session, from the
    first to the one you approved (or the last, when none was). A decline
    you answered with a go-ahead is an approval of that plan, not a plan
    sent back, and a plan Claude puts up again unchanged after you approved
    it by typing is the same plan."""

    #: Indices into the session's priced replies of every ``ExitPlanMode``
    #: call, the repeat of an approved plan among them.
    calls: tuple[int, ...]
    #: Different plans put up, the first and the approved one among them.
    versions: int
    #: Plans you sent back in the dialog (a decline followed by a go-ahead
    #: is no such plan).
    sent_back: int
    #: The reply with the approved plan; ``None`` when you approved none.
    approval: int | None
    #: You approved it by typing a go-ahead or leaving plan mode, not in the
    #: dialog.
    typed: bool
    #: Steps and files named in the last plan (``PlanStats``).
    steps: int
    files: int
    #: How the feedback on each plan you sent back reads, in order
    #: (``PlanStats.feedback_class``).
    feedback: tuple[str, ...]

    @property
    def end(self) -> int:
        """The reply with the approved plan, else the last plan put up."""
        return self.approval if self.approval is not None else self.calls[-1]

    @property
    def between(self) -> range:
        """The replies after the first plan, through the approval: what the
        plan rounds cost. Empty when the first plan was the one."""
        return range(self.calls[0] + 1, self.end + 1)


def _same_plan(a: PlanStats, b: PlanStats) -> bool:
    return (a.chars, a.steps, a.files) == (b.chars, b.steps, b.files)


def _sent_back(plan: PlanStats) -> bool:
    """The dialog declined it and a typed go-ahead did not follow."""
    return plan.rejected and plan.outcome != "approved_by_message"


def _group(priced: list[Turn], plans: list[int], repeats: list[int]) -> PlanGroup:
    stats = [priced[i].plan_stats for i in plans]
    last = stats[-1]
    approved = _approved(priced[plans[-1]])
    return PlanGroup(
        calls=tuple(sorted([*plans, *repeats])),
        versions=len(plans),
        sent_back=sum(1 for plan in stats if _sent_back(plan)),
        approval=plans[-1] if approved else None,
        typed=approved and last.outcome == "approved_by_message",
        steps=last.steps,
        files=last.files,
        feedback=tuple(plan.feedback_class for plan in stats if _sent_back(plan) and plan.feedback_class),
    )


def plan_groups(priced: list[Turn]) -> list[PlanGroup]:
    """The plans of a main session's replies (``priced``), grouped by ask:
    a group closes when you approve a plan, or when more than
    :data:`PLAN_BUILD_REPLIES` replies edited files since its last plan
    (the build began without an approval; Claude's edits to the plan file
    between rounds are a few). Shared with ``habits``."""
    groups: list[PlanGroup] = []
    plans: list[int] = []
    repeats: list[int] = []
    for i, turn in enumerate(priced):
        plan = turn.plan_stats
        if plan is None:
            continue
        if plans:
            head = priced[plans[-1]]
            if _approved(head):
                if head.plan_stats.outcome == "approved_by_message" and _same_plan(head.plan_stats, plan):
                    repeats.append(i)
                    continue
                groups.append(_group(priced, plans, repeats))
                plans, repeats = [], []
            elif sum(1 for t in priced[plans[-1] + 1 : i] if t.edit_kind == "real") > PLAN_BUILD_REPLIES:
                groups.append(_group(priced, plans, repeats))
                plans, repeats = [], []
        plans.append(i)
    if plans:
        groups.append(_group(priced, plans, repeats))
    return groups


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
class PlanApproval:
    """One approved plan and how its build began: counts and costs only."""

    session_id: str
    turn_index: int
    #: You approved it by typing a go-ahead or leaving plan mode, not in the
    #: dialog.
    typed: bool
    #: A word of :data:`START_WORDS`.
    start: str = "kept"
    #: The planning context the build carried: what a fresh start would have
    #: dropped (0 once it started fresh).
    tokens_carried: int = 0
    #: The replies of the build, what they cost, and the context they read
    #: in all.
    build_turns: int = 0
    build_usd: float = 0.0
    build_context: int = 0


@dataclass(slots=True)
class HandoffStats:
    main_sessions: int = 0
    main_session_usd: float = 0.0
    sessions: list[SessionHandoff] = field(default_factory=list)
    plans: list[PlanHandoff] = field(default_factory=list)
    approvals: list[PlanApproval] = field(default_factory=list)
    rediscovery_allowance_usd: float = 0.0
    sonnet_available: bool = False


@dataclass(slots=True)
class _Run:
    """A main session read once: its priced replies and what each cost."""

    tr: TranscriptResult
    priced: list[Turn]
    costs: list[float]

    @property
    def project(self) -> str:
        return self.tr.meta.project_slug

    @property
    def began(self) -> datetime:
        return capture_mod._moment(self.priced[0].ts) or capture_mod._FLOOR

    def clears(self) -> list[datetime]:
        """When you ran /clear in it."""
        found = (
            capture_mod._moment(event.ts)
            for event in self.tr.events
            if event.kind == EventKind.SLASH_COMMAND and event.detail.get("command") == "clear"
        )
        return sorted(at for at in found if at is not None)

    def openings(self) -> list[tuple[str, datetime]]:
        """How the session begins when that starts a build fresh, with when:
        a /clear before its first reply, and a first message that is the
        plan itself."""
        out: list[tuple[str, datetime]] = []
        clears = self.clears()
        if clears and clears[0] <= self.began:
            out.append(("cleared", clears[0]))
        if self.priced[0].human_plan_handoff and self.began > capture_mod._FLOOR:
            out.append(("handoff", self.began))
        return out


def _session(
    tr: TranscriptResult, lookup, sonnet: RatesArg, allowance: float, th: HandoffThresholds, stats: HandoffStats
) -> _Run | None:
    priced = [t for t in tr.turns if t.turn_index > 0]
    if not priced:
        return None
    stats.main_sessions += 1
    costs = [price_turn(t, lookup(t.model)).total for t in priced]
    stats.main_session_usd += sum(costs)
    run = _Run(tr, priced, costs)
    approved = [i for i, t in enumerate(priced) if _approved(t)]
    if not approved:
        return run

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
    return run


def _build_of(record: PlanApproval, run: _Run, window: range) -> None:
    """Sets ``record``'s build to the replies of ``run`` in ``window``."""
    record.build_turns = len(window)
    record.build_usd = sum(run.costs[j] for j in window)
    record.build_context = sum(run.priced[j].ctx for j in window)


def _approvals(runs: list[_Run]) -> list[PlanApproval]:
    """Every approved plan in ``runs`` and how its build began. It stays in
    the session (``kept``) unless a /clear came within :data:`FRESH_CLEAR_S`
    seconds of the approval (``cleared``: in the same session, or opening a
    session of the same project), or a session of the same project, begun
    within :data:`HANDOFF_LINK_S` seconds of it, opens with the plan itself
    (``handoff``). A fresh build is the replies of that next session, up to
    its own first plan; each session starts at most one, and each approval
    is claimed once. The time of the approval is the line that approved it
    (``PlanStats.approved_ts``), so a plan without one stays ``kept``."""
    found: list[tuple[PlanApproval, datetime | None, _Run]] = []
    for run in runs:
        priced = run.priced
        start = starting_context(priced)
        plan_calls = [i for i, t in enumerate(priced) if t.plan_stats is not None]
        for group in plan_groups(priced):
            if group.approval is None:
                continue
            turn = priced[group.approval]
            record = PlanApproval(
                session_id=run.tr.meta.session_id,
                turn_index=turn.turn_index,
                typed=group.typed,
                tokens_carried=max(0, turn.ctx - _fresh(start, turn)),
            )
            end = next((k for k in plan_calls if k > group.calls[-1]), len(priced))
            _build_of(record, run, range(group.approval + 1, end))
            found.append((record, capture_mod._moment(turn.plan_stats.approved_ts), run))

    for record, at, run in found:
        if at is not None and any(0 <= (clear - at).total_seconds() <= FRESH_CLEAR_S for clear in run.clears()):
            record.start, record.tokens_carried = "cleared", 0

    claimed: set[int] = set()
    for run in sorted(runs, key=lambda r: r.began):
        if not run.project:
            continue
        for word, opened in run.openings():
            window = FRESH_CLEAR_S if word == "cleared" else HANDOFF_LINK_S
            near = [
                (at, n)
                for n, (record, at, other) in enumerate(found)
                if other is not run
                and other.project == run.project
                and n not in claimed
                and record.start == "kept"
                and at is not None
                and 0 <= (opened - at).total_seconds() <= window
            ]
            if not near:
                continue
            n = max(near)[1]
            claimed.add(n)
            record = found[n][0]
            record.start, record.tokens_carried = word, 0
            first_plan = next((j for j, t in enumerate(run.priced) if t.plan_stats is not None), len(run.priced))
            _build_of(record, run, range(first_plan))
            break
    return [record for record, _, _ in found]


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
    runs: list[_Run] = []
    for tr in results:
        if tr.meta.kind != "top-level" or scheduled_main_session(tr):
            continue
        run = _session(tr, lookup, sonnet, rediscovery_allowance_usd, th, stats)
        if run is not None:
            runs.append(run)
    stats.approvals = _approvals(runs)
    stats.sessions.sort(key=lambda s: (-s.saving_usd, -s.tokens_carried, s.session_id))
    return stats


# -- report section -----------------------------------------------------------


def _approval_row(word: str, rows: list[PlanApproval]) -> list:
    replies = sum(a.build_turns for a in rows)
    usd = sum(a.build_usd for a in rows)
    return [
        word,
        len(rows),
        sum(1 for a in rows if a.typed),
        int(sum(a.tokens_carried for a in rows) / len(rows)),
        replies,
        int(sum(a.build_context for a in rows) / replies) if replies else None,
        usd / replies if replies else None,
        usd,
    ]


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
    approvals = Table(
        name="plan_handoff_approvals",
        title="How the build began after each approved plan",
        columns=[
            Column(key="start", label="How the build began", kind="str"),
            Column(key="approvals", label="Approved plans", kind="int"),
            Column(key="typed", label="Approved by typing", kind="int"),
            Column(key="tokens_carried", label="Planning context carried", kind="tokens"),
            Column(key="build_turns", label="Build replies", kind="int"),
            Column(key="avg_context", label="Context read per build reply", kind="tokens"),
            Column(key="usd_per_reply", label="Cost per build reply", kind="money"),
            Column(key="build_usd", label="Build cost", kind="money"),
        ],
        rows=[
            _approval_row(word, [a for a in stats.approvals if a.start == word])
            for word in START_WORDS
            if any(a.start == word for a in stats.approvals)
        ],
    )
    notes = list(ASSUMPTIONS) + [
        f"File re-read allowance per fresh start: ${stats.rediscovery_allowance_usd:.4f} at list price.",
        f"Thresholds: {' '.join(th.describe())}",
    ]
    if len(stats.sessions) > th.top_n:
        notes.append(f"{len(stats.sessions) - th.top_n} more sessions with an approved plan aren't listed.")
    return Section(key="plan_handoff", title="Building in a fresh session after a big plan", tables=[summary, by_session, approvals], notes=notes)


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


#: /cg-feedback handoff answers (or rated pieces) needed before your
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
    """What your /cg-feedback answers said about sessions you planned and
    built in (``habits_by_shape``'s ``plan_build`` row): ``yes``,
    ``partly`` and ``no`` handoff counts, the rated pieces and the share
    too costly, and what you said about the fixes after a plan: ``covered``
    (the plan said it), ``gap`` (it left it out) and ``new`` (you thought of
    it later), from the plan check and the plan question together."""
    cells = _cells(_table(report, "habits", "habits_by_shape"), "plan_build")
    counts = {word: cells.get(f"handoff_{word}") for word in ("yes", "partly", "no")}
    counts = {word: n if isinstance(n, int) else 0 for word, n in counts.items()}
    plan = {word: cells.get(f"plan_{word}") for word in ("covered", "gap", "new")}
    plan = {word: n if isinstance(n, int) else 0 for word, n in plan.items()}
    pieces = cells.get("pieces")
    costly = cells.get("costly_pct")
    return {
        **counts,
        "answers": sum(counts.values()),
        "pieces": pieces if isinstance(pieces, int) else 0,
        "costly_pct": costly if isinstance(costly, (int, float)) else None,
        **plan,
        "plan_answers": sum(plan.values()),
    }


def _rule_plan_handoff(report: ReportModel, th: HandoffThresholds) -> list[Recommendation]:
    """``plan-handoff``: in at least ``min_sessions`` main sessions the
    build after an approved plan carried a lot of planning context, and
    starting it fresh would have saved at least ``min_saving_share_pct``
    of main-session cost. ``advice`` words the saving.

    Your /cg-feedback answers change the card once there are at least
    :data:`MIN_FEEDBACK_ANSWERS`: when more than half say the build
    relied on the earlier discussion, or more than half of your fixes
    after a plan were things the plan left out (the plan check and the
    plan question), it suggests writing fuller plans first; when more than
    half say the plan was enough, it cites them, and says the build can
    start fresh at the approval; when most rated planned builds were too
    costly, it says so."""
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
    plan_gaps = fb["plan_answers"] >= MIN_FEEDBACK_ANSWERS and fb["gap"] * 2 > fb["plan_answers"]
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
    variant = ""
    if needs_discussion or plan_gaps:
        title = "Write fuller plans, then build in a fresh session"
        variant = "fuller_plans"
        if needs_discussion:
            why += (
                f" You said {fb['no']} of {fb['answers']} builds relied on the earlier discussion, so a fresh start "
                "would have lost what they needed. The saving needs a plan that carries it."
            )
        if plan_gaps:
            why += (
                f" You said {fb['gap']} of {fb['plan_answers']} fixes after a plan were things it left out, so a "
                "thin plan sends you back to fix the build."
            )
        action = (
            "Before you approve a plan, ask Claude to add the decisions, file paths and constraints the build "
            "needs. Then run /clear and ask Claude to carry out the plan file (saved under ~/.claude/plans)."
        )
    elif plan_enough:
        why += f" You said {fb['yes']} of {fb['answers']} builds could have started from the plan."
        action = (
            "Approve the plan, then run /clear and ask Claude to carry out the plan file. Claude Code saves it "
            "under ~/.claude/plans; build one phase per session."
        )
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
    if plan_gaps:
        evidence += [
            _evidence("Fixes after a plan that it left out", fb["gap"], "habits", "habits_by_shape", "plan_build"),
            _evidence("Fixes after a plan you answered for", fb["plan_answers"], "habits", "habits_by_shape", "plan_build"),
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
            variant=variant,
        )
    ]


RULES: list[Callable[[ReportModel, HandoffThresholds], list[Recommendation]]] = [_rule_plan_handoff]


__all__ = [
    "ASSUMPTIONS",
    "HandoffThresholds",
    "HandoffStats",
    "PlanHandoff",
    "PlanApproval",
    "PlanGroup",
    "SessionHandoff",
    "starting_context",
    "plan_carried",
    "plan_shape",
    "plan_groups",
    "FRESH_CLEAR_S",
    "HANDOFF_LINK_S",
    "PLAN_BUILD_REPLIES",
    "START_WORDS",
    "START_LABELS",
    "compute_handoff",
    "build_section",
    "RULES",
]

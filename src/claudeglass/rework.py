"""Rework after delivery: how often Claude had to change work it had already
delivered, why, and what to change in how you ask (the ``rework`` report
section, on the Work habits page).

Built from :attr:`habits.Habits.work_pieces`: every piece of work drawn from
the transcripts (:mod:`pieces`), rated or not, so it needs no /cg-feedback
answer. Your answers, and Claude's and Haiku's tags, only say *why*. Counts,
closed words and amounts only; nothing a transcript said is kept.

**Pieces.** Only pieces that changed files (``WorkPiece.delivered``) can need
changes after a delivery, so only they count. The headline says how many of
those whose start was found inside their session needed changes; a session
whose structure isn't known (``WorkPiece.unsegmented``) is one piece we can't
count, so its requests are counted instead, in a sentence of their own, with
their own cost. The headline's cost and cause shares are those of the first
kind of piece; the cause cards count every rework cycle, those sessions'
too.

**Causes** are :data:`pieces.CAUSES`, each with the word for where it came
from (:data:`pieces.SOURCES`), your feedback answers first. A correction that
nothing explains reads "cause not reported", never "Claude got it wrong".
Each has a Try line and, mostly, something to copy. Fixes after a plan you
approved (:func:`habits.fixes_after_plan`) show as a card of their own, only
once enough plans were approved to say anything.

**Admissions** are the settled ``admit`` tags
(``WorkPiece.admitted``); a reply that reads like one that nothing confirmed
is only a *possible* one and never in a total. Where Claude's mistake was,
from your ``missed_in`` answer, picks the fix line
(:data:`capture_catalogue.MISSED_IN_LINES`).

**By week**: the share of pieces that needed changes, only for a week with
:data:`MIN_WEEK_REWORKED` pieces or more that did, and the mistakes you
caught per piece, only for a week with :data:`MIN_WEEK_PIECES` tagged
pieces. A week with fewer is a dash, never a zero.

**By level**: rework cycles per request (``WorkPiece.substantive``), by how
hard the work was tagged: a hard piece has more requests, so the bare count
would only say it was bigger.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import capture_catalogue as catalogue
from . import habits as habits_mod
from . import pieces as pieces_mod
from .model import Column, Section, Table
from .pieces import CAUSES, SOURCES, UNKNOWN, ReworkRate, WorkPiece
from .units import Units

TITLE = "Rework after delivery"
INTRO = "How often Claude had to change work it had already delivered, why, and what to change in how you ask."

#: The fewest pieces that needed changes in a week for it to get a bar, and
#: the fewest tagged pieces for the mistakes you caught per piece.
MIN_WEEK_REWORKED = 5
MIN_WEEK_PIECES = habits_mod.MIN_GROUP

#: The row for fixes after a plan you approved, in ``rework_causes``: not a
#: cause the pieces record, a measure ``habits.fixes_after_plan`` makes.
PLAN_FIXES = "plan_fixes"

#: What to try for each cause, and what to copy. ``missed`` takes its Try line
#: from where you said Claude missed it (:func:`_try_line`).
TRY: dict[str, str] = {
    "left_out": "Say more in your first message: the files, what you want and what done looks like.",
    "missed": catalogue.MISSED_IN_LINES[""],
    "changed": "Plan first, so a change of mind comes before the work is done, not after.",
    "tools": "See which tool calls failed or were blocked on {{page:actions/checks}}, and fix the cause.",
    "plan_gap": "Before you approve a plan, ask for the files, the decisions and a done-when line.",
    PLAN_FIXES: "Add details like these to the plan before approving: the files, the decisions and a done-when line.",
    "mixed": "You named more than one reason. Run /cg-feedback after one piece of work to say which it was.",
    "not_reported": "Run /cg-feedback after a piece of work to say why it needed changes.",
}
#: ``left_out``'s Try and Copy lines while the Work habits page shows its
#: brief card: the optional /cg-brief skill is offered only then.
BRIEF_TRY = "Say more in your first message: run /cg-brief with your request and it asks for what is missing."
BRIEF_PASTE = "/cg-brief"
PASTE: dict[str, str] = {
    "left_out": "Here is what I want, the files it involves and what done looks like: <say it here>.",
    "missed": (
        "Before you finish, re-read my request, check each point is done, and run the tests for what you changed."
    ),
    "changed": "Plan this first and wait for my go-ahead before changing any files.",
    "tools": "",
    "plan_gap": (
        "Before I approve this plan, list the files you will change, the decisions you made and what done looks like."
    ),
    PLAN_FIXES: (
        "Before I approve this plan, list the files you will change, the decisions you made and what done looks like."
    ),
    "mixed": "/cg-feedback",
    "not_reported": "/cg-feedback",
}


@dataclass(frozen=True, slots=True)
class CauseRow:
    """The rework cycles with one cause from one source."""

    cause: str
    source: str
    #: Pieces with any, and the cycles in them (follow-up messages that were
    #: rework), what those cost and the tokens they used.
    pieces: int
    cycles: int
    cost: float
    tokens: int
    #: Sessions with any whose pieces couldn't be told apart, which are not
    #: counted in ``pieces``.
    sessions: int = 0


@dataclass(slots=True)
class Admitted:
    """Claude's admitted mistakes (``WorkPiece.admitted``) over every piece."""

    mistakes: int = 0
    #: Pieces with any.
    pieces: int = 0
    user: int = 0
    itself: int = 0
    instruction: int = 0
    #: Replies that read like an admission that no tag confirmed.
    possible: int = 0
    #: What the rework after the mistakes you caught cost.
    cost: float = 0.0


@dataclass(slots=True)
class WeekRow:
    """One week (the Monday, ``YYYY-MM-DD``): the pieces that changed files and
    whose start was found, and the mistakes you caught in any piece."""

    week: str
    pieces: int = 0
    reworked: int = 0
    cost: float = 0.0
    #: Every piece with any tag, the ones we couldn't cut included, and the
    #: mistakes you caught in them.
    tagged: int = 0
    caught: int = 0


@dataclass(slots=True)
class Rework:
    """Everything the tables are worked out from."""

    #: The report window (``ReportMeta.window``), to label every figure.
    window: str = ""
    #: Pieces that changed files.
    delivered: int = 0
    #: :func:`pieces.rework_rate` over the ones whose start was found inside
    #: their session: what the headline's "N of M pieces" says, with its
    #: cost and the tokens of its rework.
    rate: ReworkRate = field(default_factory=ReworkRate)
    tokens: int = 0
    #: The same, over the sessions whose pieces couldn't be told apart.
    unsegmented: ReworkRate = field(default_factory=ReworkRate)
    unsegmented_tokens: int = 0
    #: Causes over every piece, those sessions included, and over the
    #: pieces of ``rate`` alone (the headline's shares).
    causes: list[CauseRow] = field(default_factory=list)
    piece_causes: list[CauseRow] = field(default_factory=list)
    #: Where Claude's mistake was, from your answers: ``missed_in`` word ->
    #: cycles.
    missed_in: Counter = field(default_factory=Counter)
    admitted: Admitted = field(default_factory=Admitted)
    weeks: list[WeekRow] = field(default_factory=list)
    levels: dict[str, ReworkRate] = field(default_factory=dict)
    plan_fixes: habits_mod.PlanFixes | None = None
    #: "N of M follow-ups were things your request left out", or ``""``.
    left_out_note: str = ""
    #: Whether Work habits shows its brief card, so /cg-brief is on offer.
    brief_offer: bool = False

    @property
    def cycles(self) -> int:
        """Rework cycles in every piece, all causes."""
        return self.rate.rework + self.unsegmented.rework

    def share(self, cause: str) -> float:
        """The percent of the pieces' rework cycles that had ``cause``."""
        total = sum(row.cycles for row in self.piece_causes)
        return 100.0 * sum(row.cycles for row in self.piece_causes if row.cause == cause) / total if total else 0.0

    def unknown(self) -> tuple[int, int, float, int] | None:
        """``(cycles, of, cost, tokens)`` of the rework with no cause
        reported: among the pieces' rework when they have any, so it sits
        beside the shares above it, else among every session's. ``None``
        when there is none."""
        rows = self.piece_causes if self.rate.rework else self.causes
        total = sum(row.cycles for row in rows)
        mine = [row for row in rows if row.cause == "not_reported"]
        if not total or not mine:
            return None
        return sum(r.cycles for r in mine), total, sum(r.cost for r in mine), sum(r.tokens for r in mine)

    @property
    def top_missed_in(self) -> str:
        """The place you most often said Claude missed it, or ``""``."""
        return self.missed_in.most_common(1)[0][0] if self.missed_in else ""


# -- collecting ------------------------------------------------------------------


def _cause_rows(pieces: list[WorkPiece]) -> list[CauseRow]:
    """The cause rows of ``pieces``, your feedback first, then by cycles."""
    cycles: Counter = Counter()
    spend: dict[tuple[str, str], list] = {}
    touched: Counter = Counter()
    loose: Counter = Counter()
    for piece in pieces:
        for cause, source, n in piece.causes:
            cycles[(cause, source)] += n
            (loose if piece.unsegmented else touched)[(cause, source)] += 1
        for cause, source, cost, tokens in piece.cause_spend:
            row = spend.setdefault((cause, source), [0.0, 0])
            row[0] += cost
            row[1] += tokens
    rows = [
        CauseRow(cause, source, touched[(cause, source)], n, *spend.get((cause, source), (0.0, 0)), loose[(cause, source)])
        for (cause, source), n in cycles.items()
    ]
    rows.sort(key=lambda r: (SOURCES.index(r.source), -r.cycles, CAUSES.index(r.cause)))
    return rows


def _admitted(pieces: list[WorkPiece]) -> Admitted:
    return Admitted(
        mistakes=sum(p.admitted for p in pieces),
        pieces=sum(1 for p in pieces if p.admitted),
        user=sum(p.admitted_user for p in pieces),
        itself=sum(p.admitted_self for p in pieces),
        instruction=sum(p.admitted_instruction for p in pieces),
        possible=sum(p.admit_possible for p in pieces),
        cost=sum(p.admit_user_cost for p in pieces),
    )


def _monday(week: str) -> date | None:
    try:
        return date.fromisoformat(week)
    except ValueError:
        return None


def _weeks(h: habits_mod.Habits, pieces: list[WorkPiece]) -> list[WeekRow]:
    """One row per week from the first piece's to the last's, a week with no
    piece included so the bars keep their spacing. ``pieces``, ``reworked``
    and ``cost`` count ``pieces``; ``tagged`` and ``caught`` count every piece
    of ``h``, as the admitted mistakes do."""
    by_week: dict[str, WeekRow] = {}
    for piece in pieces:
        week = habits_mod._week(habits_mod._moment(piece.end_ts or piece.start_ts), h.tz)
        if not week:
            continue
        row = by_week.setdefault(week, WeekRow(week))
        row.pieces += 1
        row.reworked += int(bool(piece.rework))
        row.cost += piece.rework_cost
    # Mistakes you caught are counted over every piece, as the admitted line is.
    for piece in h.work_pieces:
        if not (piece.task or piece.level or piece.size):
            continue
        week = habits_mod._week(habits_mod._moment(piece.end_ts or piece.start_ts), h.tz)
        if not week:
            continue
        row = by_week.setdefault(week, WeekRow(week))
        row.tagged += 1
        row.caught += piece.admitted_user
    if not by_week:
        return []
    first, last = _monday(min(by_week)), _monday(max(by_week))
    if first is None or last is None:
        return [by_week[w] for w in sorted(by_week)]
    rows = []
    day = first
    while day <= last:
        rows.append(by_week.get(day.isoformat()) or WeekRow(day.isoformat()))
        day += timedelta(days=7)
    return rows


def collect(h: habits_mod.Habits) -> Rework:
    """The rework in ``h``'s pieces of work (``habits.collect`` has drawn
    them)."""
    delivered = [p for p in h.work_pieces if p.delivered]
    segmented = [p for p in delivered if not p.unsegmented]
    loose = [p for p in delivered if p.unsegmented]
    missed_in: Counter = Counter()
    for piece in delivered:
        for word, n in piece.missed_in:
            missed_in[word] += n
    return Rework(
        window=h.window,
        delivered=len(delivered),
        rate=pieces_mod.rework_rate(segmented),
        tokens=sum(p.rework_tokens for p in segmented),
        unsegmented=pieces_mod.rework_rate(loose),
        unsegmented_tokens=sum(p.rework_tokens for p in loose),
        causes=_cause_rows(delivered),
        piece_causes=_cause_rows(segmented),
        missed_in=missed_in,
        admitted=_admitted(h.work_pieces),
        weeks=_weeks(h, segmented),
        levels=pieces_mod.rework_by_level(delivered),
        plan_fixes=_plan_fixes(h),
        left_out_note=habits_mod.left_out_note(h),
        brief_offer=habits_mod.brief_card_shown(h),
    )


def _plan_fixes(h: habits_mod.Habits) -> habits_mod.PlanFixes | None:
    """Fixes after a plan, once enough plans were approved to say anything
    and some needed several."""
    found = habits_mod.fixes_after_plan(h)
    return found if found is not None and found.fixed else None


def period_phrase(window: str) -> str:
    """"last 30 days" -> "over the last 30 days", for amounts; ``""`` when
    the caller didn't say which window this is."""
    if not window:
        return ""
    if window.startswith("since"):
        return window
    return f"over the {window}" if window.startswith("last") else f"over {window}"


# -- words -----------------------------------------------------------------------


def _count(n: int, one: str, many: str = "") -> str:
    return f"{n} {one if n == 1 else many or one + 's'}"


def _pct(share: float) -> str:
    return f"{share:.0f}"


def _cost(units: Units, usd: float, period: str, said: str, unpriced: str = "That rework was not priced.") -> str:
    """``said`` with the amount in it ("That rework cost {amount}."), or
    ``unpriced`` when there is none: "not priced", never a bare zero."""
    amount = units.money(usd, period=period)
    return said.format(amount=amount.phrase()) if amount is not None else unpriced


def headline_text(r: Rework, units: Units, period: str) -> str:
    """"N of your M pieces of work needed changes after Claude delivered
    them. That rework cost X. U% came from requests that left something
    out, C% from Claude's mistakes, X% from changes of mind." When some
    rework had another cause: "O% came from failed tools, plan gaps or a mix
    of causes."
    """
    n, total = r.rate.reworked, r.rate.segmented
    pieces, them = ("piece", "it") if total == 1 else ("pieces", "them")
    if not n:
        return f"None of your {total} {pieces} of work needed changes after Claude delivered {them}."
    other = r.share("tools") + r.share("plan_gap") + r.share("mixed")
    rest = f" {_pct(other)}% came from failed tools, plan gaps or a mix of causes." if other > 0 else ""
    return (
        f"{n} of your {total} {pieces} of work needed changes after Claude delivered {them}. "
        + _cost(units, r.rate.rework_cost, period, "That rework cost {amount}.")
        + f" {_pct(r.share('left_out'))}% came from requests that left something out, "
        f"{_pct(r.share('missed'))}% from Claude's mistakes, {_pct(r.share('changed'))}% from changes of mind."
        + rest
    )


def unknown_text(share: float) -> str:
    return f"We couldn't tell why for {_pct(share)}%: run /cg-feedback after a piece of work to say."


def requests_text(r: Rework, units: Units, period: str) -> str:
    """The sentence for sessions whose pieces couldn't be told apart."""
    u = r.unsegmented
    where = f"in {_count(u.pieces, 'session')} we couldn't split into pieces of work"
    if not u.rework:
        return f"None of {_count(u.substantive, 'request')} {where} needed changes after Claude delivered."
    return f"{u.rework} of {_count(u.substantive, 'request')} {where} needed changes after Claude delivered. " + _cost(
        units, u.rework_cost, period, "That rework cost {amount}."
    )


def admitted_text(a: Admitted, units: Units, period: str) -> str:
    """"Claude admitted N mistakes in M pieces: you caught U, it caught S
    itself. I were instructions it had been given. The rework after the ones
    you caught cost X."""
    text = (
        f"Claude admitted {_count(a.mistakes, 'mistake')} in {_count(a.pieces, 'piece')}: "
        f"you caught {a.user}, it caught {a.itself} itself."
    )
    if a.instruction:
        text += f" {a.instruction} {'was an instruction' if a.instruction == 1 else 'were instructions'} it had been given."
    if a.user:
        text += " " + _cost(
            units, a.cost, period, "The rework after the ones you caught cost {amount}.",
            "The rework after the ones you caught was not priced.",
        )
    return text


def possible_text(count: int, counted: bool = True) -> str:
    """The sentence for replies that only read like an admission. ``counted``
    says there are confirmed admissions above it, which it says apart from."""
    more, where = ("more ", " above") if counted else ("", "")
    if count == 1:
        return f"1 {more}reply reads like an admission that nothing confirmed. It is not counted{where}."
    return f"{count} {more}replies read like an admission that nothing confirmed. They are not counted{where}."


def _try_line(cause: str, r: Rework) -> str:
    if cause == "missed":
        return catalogue.MISSED_IN_LINES.get(r.top_missed_in, catalogue.MISSED_IN_LINES[""])
    if cause == "left_out" and r.brief_offer:
        return BRIEF_TRY
    return TRY[cause]


def _paste_line(cause: str, r: Rework) -> str:
    if cause == "left_out" and r.brief_offer:
        return BRIEF_PASTE
    return PASTE.get(cause, "")


def _detail(row: CauseRow, r: Rework, first_of_cause: bool) -> str:
    """The counts behind a cause card, in words."""
    where = []
    if row.pieces:
        where.append(_count(row.pieces, "piece"))
    if row.sessions:
        where.append(f"{_count(row.sessions, 'session')} we couldn't split into pieces")
    text = f"{_count(row.cycles, 'follow-up')} in {' and '.join(where)}."
    if row.cause == "missed" and row.source == "feedback" and r.top_missed_in in habits_mod._MISSED_PLACES:
        text += f" Most were missed in {habits_mod._MISSED_PLACES[r.top_missed_in]}."
    if row.cause == "left_out" and first_of_cause and r.left_out_note:
        text += f" {r.left_out_note[0].upper()}{r.left_out_note[1:]}."
    return text


# -- tables ----------------------------------------------------------------------


def _headline_table(r: Rework, units: Units, period: str, title: str) -> Table:
    rows: list[list] = []
    if r.rate.segmented:
        rows.append([
            "pieces", headline_text(r, units, period), r.rate.reworked, r.rate.segmented,
            100.0 * r.rate.reworked / r.rate.segmented, r.rate.rework_cost, r.tokens, period,
        ])
    unknown = r.unknown()
    if unknown is not None:
        cycles, of, cost, tokens = unknown
        rows.append(["unknown", unknown_text(100.0 * cycles / of), cycles, of, 100.0 * cycles / of, cost, tokens, period])
    u = r.unsegmented
    if u.substantive and (u.rework or not r.rate.segmented):
        rows.append([
            "requests", requests_text(r, units, period), u.rework, u.substantive, 100.0 * u.rework / u.substantive,
            u.rework_cost, r.unsegmented_tokens, period,
        ])
    return Table(
        name="rework_headline",
        title=title,
        columns=[
            Column(key="item", label="Item", kind="str"),
            Column(key="text", label="What it says", kind="str"),
            Column(key="count", label="Needed changes", kind="int"),
            Column(key="total", label="Out of", kind="int"),
            Column(key="share", label="Share", kind="pct"),
            Column(key="cost", label="What the rework cost", kind="money"),
            Column(key="tokens", label="Tokens", kind="tokens"),
            Column(key="period", label="Period", kind="str"),
        ],
        rows=rows,
    )


def _causes_table(r: Rework) -> Table:
    rows: list[list] = []
    seen: set[str] = set()
    for row in r.causes:
        rows.append([
            row.cause, row.source, row.pieces, row.sessions, row.cycles,
            100.0 * row.cycles / r.cycles if r.cycles else None,
            row.cost, row.tokens, _detail(row, r, row.cause not in seen), _try_line(row.cause, r),
            _paste_line(row.cause, r),
        ])
        seen.add(row.cause)
    fixes = r.plan_fixes
    if fixes is not None:
        rows.append([
            PLAN_FIXES, "inferred", fixes.fixed, None, fixes.fixes, None, fixes.cost, None,
            f"{fixes.fixed} of {_count(fixes.plans, 'plan')} you approved needed {habits_mod.PLAN_FIXES_MIN} or more "
            "fixes after approval.",
            TRY[PLAN_FIXES], PASTE[PLAN_FIXES],
        ])
    return Table(
        name="rework_causes",
        title="Why work needed changes",
        columns=[
            Column(key="cause", label="Cause", kind="str"),
            Column(key="source", label="Where it came from", kind="str"),
            Column(key="pieces", label="Pieces", kind="int"),
            Column(key="sessions", label="Sessions we couldn't split", kind="int"),
            Column(key="cycles", label="Follow-ups", kind="int"),
            Column(key="share", label="Share", kind="pct"),
            Column(key="cost", label="What the rework cost", kind="money"),
            Column(key="tokens", label="Tokens", kind="tokens"),
            Column(key="detail", label="Detail", kind="str"),
            Column(key="try", label="Try", kind="str"),
            Column(key="paste", label="Copy", kind="str"),
        ],
        rows=rows,
    )


def _admitted_table(r: Rework, units: Units, period: str) -> Table:
    a = r.admitted
    fix = catalogue.MISSED_IN_LINES.get(r.top_missed_in, catalogue.MISSED_IN_LINES[""])
    rows: list[list] = []
    if a.mistakes:
        rows.append([
            "admitted", admitted_text(a, units, period), a.mistakes, a.pieces, a.user, a.itself, a.instruction,
            a.cost, fix, PASTE["missed"], period,
        ])
    if a.possible:
        rows.append(["possible", possible_text(a.possible, bool(a.mistakes)), a.possible, None, None, None, None, None, "", "", period])
    return Table(
        name="rework_admitted",
        title="Mistakes Claude admitted",
        columns=[
            Column(key="item", label="Item", kind="str"),
            Column(key="text", label="What it says", kind="str"),
            Column(key="count", label="Replies", kind="int"),
            Column(key="pieces", label="Pieces", kind="int"),
            Column(key="user", label="You caught", kind="int"),
            Column(key="itself", label="Claude caught", kind="int"),
            Column(key="instruction", label="Instructions it had", kind="int"),
            Column(key="cost", label="Rework after yours", kind="money"),
            Column(key="fix", label="What to change", kind="str"),
            Column(key="paste", label="Copy", kind="str"),
            Column(key="period", label="Period", kind="str"),
        ],
        rows=rows,
    )


def _by_week_table(r: Rework) -> Table:
    rows = []
    for w in r.weeks:
        rows.append([
            w.week, w.pieces, w.reworked,
            100.0 * w.reworked / w.pieces if w.reworked >= MIN_WEEK_REWORKED else None,
            w.cost if w.pieces else None,
            w.caught if w.tagged else None,
            w.caught / w.tagged if w.tagged >= MIN_WEEK_PIECES else None,
        ])
    return Table(
        name="rework_by_week",
        title="Rework by week",
        columns=[
            Column(key="week", label="Week of", kind="str"),
            Column(key="pieces", label="Pieces", kind="int"),
            Column(key="reworked", label="Needed changes", kind="int"),
            Column(key="share", label="Share", kind="pct"),
            Column(key="cost", label="What the rework cost", kind="money"),
            Column(key="caught", label="Mistakes you caught", kind="int"),
            Column(key="caught_per_piece", label="Caught per piece", kind="float"),
        ],
        rows=rows,
    )


def _by_level_table(r: Rework) -> Table:
    rows = []
    for word in (*catalogue.TAG_VOCAB["level"], UNKNOWN):
        rate = r.levels.get(word)
        if rate is None:
            continue
        per_cycle = rate.per_cycle
        rows.append([
            word, rate.substantive, rate.rework,
            None if per_cycle is None else 100.0 * per_cycle, rate.rework_cost,
        ])
    return Table(
        name="rework_by_level",
        title="Rework by how hard the work was",
        columns=[
            Column(key="level", label="How hard", kind="str"),
            Column(key="requests", label="Requests", kind="int"),
            Column(key="rework", label="Needed changes", kind="int"),
            Column(key="rate", label="Per request", kind="pct"),
            Column(key="cost", label="What the rework cost", kind="money"),
        ],
        rows=rows,
    )


def _window_title(title: str, window: str) -> str:
    return f"{title} ({window})" if window else title


def build_section(h: habits_mod.Habits, units: Units | None = None) -> Section:
    """The "rework" report section. Every table is always there, empty when
    there's nothing to show, so the report keeps its shape. ``h`` is
    ``habits.collect``'s; ``units`` phrases every amount for the billing mode
    and carries the window's label with it."""
    units = units or Units()
    r = collect(h)
    period = period_phrase(r.window)
    notes = []
    if not r.delivered:
        notes.append(
            "No piece of work has changed files in this window yet, so there is no rework to count. "
            "Pick a longer window to include more sessions."
        )
    elif not r.cycles:
        notes.append("None of the work delivered in this window needed changes afterwards.")
    return Section(
        key="rework",
        title=TITLE,
        intro=INTRO,
        tables=[
            _headline_table(r, units, period, _window_title(TITLE, r.window)),
            _causes_table(r),
            _admitted_table(r, units, period),
            _by_week_table(r),
            _by_level_table(r),
        ],
        notes=notes,
    )


__all__ = [
    "Admitted",
    "CauseRow",
    "INTRO",
    "MIN_WEEK_PIECES",
    "MIN_WEEK_REWORKED",
    "PASTE",
    "PLAN_FIXES",
    "Rework",
    "TITLE",
    "TRY",
    "WeekRow",
    "admitted_text",
    "build_section",
    "collect",
    "headline_text",
    "period_phrase",
    "possible_text",
    "requests_text",
    "unknown_text",
]

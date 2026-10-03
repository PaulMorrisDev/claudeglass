"""How you prompt: the habits the coaching hints warn about as you type,
counted after the fact from your transcripts, with what they cost and
how often they happened week by week (the ``prompting`` section, on the
Work habits page). It works whether or not coaching notes are on.

Each habit uses the live hint's own rule and default threshold
(``capture_catalogue.COACHING_THRESHOLDS``), on what the parser kept
about each message (``Turn.prompt_steps`` and the other prompting-habits
fields; see ``model.py``) -- counts and flags, never your words:

- ``drip_feed``: three or more small messages in a row, each sent within
  ``drip_window_minutes`` of Claude's reply and answered with a file
  change. A reply to Claude's question is skipped; a thank-you, a long
  message, a reply that changed nothing or a longer wait ends the run.
- ``repeat_ask``: much the same request as one sent earlier.
- ``stop_loop``: ``stop_loop_count`` stopped replies within
  ``stop_window_minutes``, counting a message stopped before any reply
  and sent again.
- ``plan_first``: a request for ``plan_steps`` or more separate changes,
  ``plan_min_chars`` or longer, sent outside plan mode before any plan
  was approved in that session.
- ``vague_fix``: a short fix request that names nothing specific.
- ``big_paste``: a message of ``big_paste_tokens`` or more.

What each costs is a rough figure at list price, with its basis stated
(:data:`BASIS`): what batching, planning or pasting less could have
saved at most, not a forecast. A big task without a plan has no figure:
what it led to can't be told apart from the work itself.

For coaching notes, it also counts how often Claude showed the tip a
note asked for (``Turn.coach_tip``), by hint.
"""

from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import capture as capture_mod
from . import capture_catalogue as catalogue
from . import events as events_mod
from .context_files import _parse_ts
from .model import Column, EventKind, Section, Table, Turn
from .pricing import Pricing, effective_rates, price_turn

#: The habits, in the order the section shows them.
HABITS = ("drip_feed", "repeat_ask", "stop_loop", "plan_first", "vague_fix", "big_paste")

#: What each habit is called on the page.
TITLES = {
    "drip_feed": "Small requests sent one at a time",
    "repeat_ask": "The same request again",
    "stop_loop": "Stopping Claude again and again",
    "plan_first": "Big tasks without a plan",
    "vague_fix": "Vague corrections",
    "big_paste": "Huge pastes",
}

#: What to do instead.
TRY = {
    "drip_feed": "Work out everything the work still needs, then send it as one message.",
    "repeat_ask": "Say what was wrong with the last attempt instead of sending the request again.",
    "stop_loop": "Agree the approach first: plan mode (Shift+Tab), or ask for a short plan before any change.",
    "plan_first": "Start a job of several changes in plan mode (Shift+Tab), so the approach is agreed first.",
    "vague_fix": "Say what you saw and what you expected, or paste the error text.",
    "big_paste": "Paste only the part that matters, or save it to a file and give the path.",
}

#: How each habit's cost is worked out.
BASIS = {
    "drip_feed": "re-reading the whole context for each message after the first in a run",
    "repeat_ask": "the reply before the repeat, the attempt that missed",
    "stop_loop": "the replies you stopped",
    "plan_first": "",
    "vague_fix": "the reply that had to ask what was wrong",
    "big_paste": "carrying the pasted text through the rest of the session",
}

_TH = catalogue.COACHING_THRESHOLDS
_EDIT_TOOLS = frozenset(catalogue.EDIT_TOOLS)
#: Coaching hints whose note asks Claude to show you a tip.
TIP_HINTS = tuple(hint for hint in catalogue.COACHING_HINTS if catalogue.TIP_LABEL in catalogue.COACHING_TEXT[hint])
#: How many weeks the trend covers, and the fewest messages a week needs
#: to count towards it (as ``habits.TREND_WEEKS``/``TREND_MIN_CYCLES``).
TREND_WEEKS = 8
TREND_MIN_MESSAGES = 3
_CHARS_PER_TOKEN = 4


def _moment(ts: str | None) -> datetime | None:
    moment = _parse_ts(ts) if ts else None
    if moment is not None and moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def _week(moment: datetime | None) -> str:
    if moment is None:
        return ""
    day = moment.astimezone(timezone.utc).date()
    return (day - timedelta(days=day.weekday())).isoformat()


class _Prices:
    """List-price USD for replies, each model resolved once."""

    def __init__(self, pricing: Pricing | None):
        self.pricing = pricing
        self._resolved: dict = {}

    def _resolve(self, turn: Turn):
        if turn.model not in self._resolved:
            self._resolved[turn.model] = self.pricing.resolve_model(turn.model)
        return self._resolved[turn.model]

    def cost(self, turn: Turn) -> float:
        return 0.0 if self.pricing is None else price_turn(turn, self._resolve(turn)).total

    def reread(self, turn: Turn) -> float:
        """What a reply paid to take in its context: everything but output."""
        if self.pricing is None:
            return 0.0
        priced = price_turn(turn, self._resolve(turn))
        return priced.total - priced.output_cost

    def carry(self, turns: list[Turn], start: int, tokens: float) -> float:
        """Keeping ``tokens`` in context from reply ``start`` on: a cache
        write there, then a cache read by each reply until a summary."""
        if self.pricing is None or tokens <= 0 or start >= len(turns):
            return 0.0
        first = turns[start]
        rates = effective_rates(first, self._resolve(first))
        total = tokens * (rates.cache_write_1h if first.cc_1h > first.cc_5m else rates.cache_write_5m) / 1e6
        for turn in turns[start + 1:]:
            if EventKind.COMPACT_BOUNDARY in turn.preceding_event_kinds:
                break
            total += tokens * effective_rates(turn, self._resolve(turn)).cache_read / 1e6
        return total


@dataclass(slots=True)
class Message:
    """One message of yours and the work that answered it."""

    at: datetime | None
    chars: int
    steps: int = 0
    plan_mode: bool = False
    vague: bool = False
    ack: bool = False
    repeat: bool = False
    #: Claude changed a file in reply.
    edited: bool = False
    #: The reply before it ended on a question, so it answers one.
    answers: bool = False
    #: Seconds since Claude's reply before it; ``None`` for the first.
    gap: float | None = None
    #: A plan was approved earlier in the session.
    planned: bool = False
    #: The whole reply to it, and what its first reply paid to take in
    #: the context.
    cost: float = 0.0
    reread: float = 0.0
    #: What carrying the message itself costs (for a huge paste).
    carry: float = 0.0


@dataclass(slots=True)
class Occurrence:
    habit: str
    at: datetime | None
    #: USD at list price; ``None`` when there's no figure (``plan_first``).
    cost: float | None


@dataclass(slots=True)
class SessionPrompting:
    """One session's messages, the habits in them, and its coaching tips."""

    session_id: str
    start: datetime | None
    messages: list[Message] = field(default_factory=list)
    occurrences: list[Occurrence] = field(default_factory=list)
    #: Coaching notes that asked for a tip, by hint, and of those, the ones
    #: Claude showed a tip for.
    notes: Counter = field(default_factory=Counter)
    tips: Counter = field(default_factory=Counter)

    def counts(self) -> Counter:
        return Counter(o.habit for o in self.occurrences)


def messages_of(top, prices: _Prices) -> list[Message]:
    """Each message of yours in a main transcript, with what the parser
    kept about it and the work that answered it."""
    turns = capture_mod._priced(top)
    cycles = capture_mod.prompt_cycles(top)
    out: list[Message] = []
    replied_at: datetime | None = None
    asked = planned = False
    for cycle in cycles:
        first = cycle.turns[0]
        at = _moment(first.ts)
        message = Message(
            at=at,
            chars=first.human_prompt_chars or 0,
            steps=first.prompt_steps,
            plan_mode=first.prompt_plan_mode,
            vague=first.human_vague,
            ack=first.human_ack,
            repeat=first.human_repeat,
            edited=any(tool in _EDIT_TOOLS for turn in cycle.turns for tool in turn.tool_calls_by_tool),
            answers=asked,
            gap=(at - replied_at).total_seconds() if at is not None and replied_at is not None else None,
            planned=planned,
            cost=sum(prices.cost(turn) for turn in cycle.turns),
            reread=prices.reread(first),
            carry=prices.carry(turns, cycle.start, (first.human_prompt_chars or 0) / _CHARS_PER_TOKEN),
        )
        out.append(message)
        last = cycle.turns[-1]
        replied_at = _moment(last.ts) or replied_at
        asked = last.reply_asked
        planned = planned or any(turn.plan_stats is not None for turn in cycle.turns)
    return out


def _drip_runs(messages: list[Message]) -> list[list[int]]:
    """Runs of small, quick follow-ups each answered with a file change
    (answers to Claude's questions skipped), at least ``drip_count`` long."""
    window = _TH["drip_window_minutes"] * 60
    runs: list[list[int]] = []
    run: list[int] = []
    for i, m in enumerate(messages):
        if m.answers:
            continue
        if (
            m.chars <= _TH["drip_chars"] and not m.ack and m.edited and m.gap is not None and m.gap <= window
        ):
            run.append(i)
            continue
        if len(run) >= _TH["drip_count"]:
            runs.append(run)
        run = []
    if len(run) >= _TH["drip_count"]:
        runs.append(run)
    return runs


def _stop_loops(stops: list[datetime]) -> list[list[datetime]]:
    """Groups of ``stop_loop_count`` or more stopped replies within
    ``stop_window_minutes`` of the first, not overlapping."""
    window = timedelta(minutes=_TH["stop_window_minutes"])
    need = int(_TH["stop_loop_count"])
    loops, i = [], 0
    stops = sorted(stops)
    while i < len(stops):
        group = [s for s in stops[i:] if s - stops[i] <= window]
        if len(group) >= need:
            loops.append(group)
            i += len(group)
        else:
            i += 1
    return loops


def occurrences(messages: list[Message], stops: list[tuple[datetime, float]]) -> list[Occurrence]:
    """Every habit in one session's messages; ``stops`` holds each stopped
    reply's time and what it had cost."""
    out: list[Occurrence] = []
    for run in _drip_runs(messages):
        out.append(Occurrence("drip_feed", messages[run[0]].at, sum(messages[i].reread for i in run[1:])))
    for i, m in enumerate(messages):
        if m.repeat:
            out.append(Occurrence("repeat_ask", m.at, messages[i - 1].cost if i else 0.0))
        if (
            m.steps >= _TH["plan_steps"] and m.chars >= _TH["plan_min_chars"] and not m.plan_mode
            and not m.planned and m.chars / _CHARS_PER_TOKEN < _TH["big_paste_tokens"]
        ):
            out.append(Occurrence("plan_first", m.at, None))
        if m.vague:
            # Only the round trip it caused: a reply that had to ask.
            answered_by_question = i + 1 < len(messages) and messages[i + 1].answers
            out.append(Occurrence("vague_fix", m.at, m.cost if answered_by_question else 0.0))
        if m.chars / _CHARS_PER_TOKEN >= _TH["big_paste_tokens"]:
            out.append(Occurrence("big_paste", m.at, m.carry))
    cost_at = dict(stops)
    for loop in _stop_loops([at for at, _cost in stops]):
        out.append(Occurrence("stop_loop", loop[0], sum(cost_at.get(at, 0.0) for at in loop)))
    out.sort(key=lambda o: (o.at or datetime.min.replace(tzinfo=timezone.utc), HABITS.index(o.habit)))
    return out


def _stops(top, prices: _Prices) -> list[tuple[datetime, float]]:
    """Each stopped reply's time and the cost of the replies it cut short:
    those since your message before it. Stopping before any reply leaves
    no interrupt line, only a message you sent again (``detail
    ["replaced"]``): that counts as a stop that cost nothing. An interrupt
    that only ends a turn after a plan you answered, or a call a hook
    blocked, is not a stop (``events.is_stop``)."""
    turns = capture_mod._priced(top)
    out = []
    for event in top.events:
        if event.kind == EventKind.HUMAN_TEXT and event.detail.get("replaced"):
            at = _moment(event.ts)
            if at is not None:
                out.append((at, 0.0))
            continue
        if not events_mod.is_stop(event):
            continue
        at = _moment(event.ts)
        if at is None:
            continue
        cut = []
        for turn in reversed(turns):
            when = _moment(turn.ts)
            if when is None or when > at:
                continue
            cut.append(turn)
            if turn.human_prompt_chars is not None:
                break
        out.append((at, sum(prices.cost(turn) for turn in cut)))
    return out


def _tips(top) -> tuple[Counter, Counter]:
    """Coaching notes that asked for a tip, by hint, and of those, the ones
    whose message Claude answered with a tip."""
    notes: Counter = Counter()
    tips: Counter = Counter()
    turns = capture_mod._priced(top)
    for event in top.events:
        kind = None
        if event.subkind == "coaching_note":
            kind = event.detail.get("kind")
        elif event.subkind == "capture_note":
            kind = event.detail.get("coach")
        if kind not in TIP_HINTS:
            continue
        notes[kind] += 1
        at = _moment(event.ts)
        # The replies to the message the note came with: up to the next one.
        after = [t for t in turns if at is not None and (_moment(t.ts) or at) >= at]
        cycle = []
        for n, turn in enumerate(after):
            if n and turn.human_prompt_chars is not None:
                break
            cycle.append(turn)
        if any(turn.coach_tip for turn in cycle):
            tips[kind] += 1
    return notes, tips


def session_prompting(bundle, prices: _Prices) -> SessionPrompting | None:
    top = bundle.top
    if top is None:
        return None
    messages = messages_of(top, prices)
    if not messages:
        return None
    notes, tips = _tips(top)
    return SessionPrompting(
        session_id=bundle.session_id,
        start=messages[0].at,
        messages=messages,
        occurrences=occurrences(messages, _stops(top, prices)),
        notes=notes,
        tips=tips,
    )


def collect(corpus, pricing: Pricing | None) -> list[SessionPrompting]:
    """Every session with a message of yours, oldest first."""
    prices = _Prices(pricing)
    out = [s for s in (session_prompting(bundle, prices) for bundle in corpus.sessions) if s is not None]
    out.sort(key=lambda s: s.start or datetime.min.replace(tzinfo=timezone.utc))
    return out


def trend(sessions: list[SessionPrompting], habit: str) -> tuple[str, str]:
    """``(word, weeks)``: whether ``habit`` per message is falling, rising
    or steady over the last weeks (``new`` with fewer than three weeks to
    go on), and each week's rate scaled to 0-100, ``-`` for a week with
    too few messages (as ``habits.trend``)."""
    per_week: Counter = Counter()
    hits: Counter = Counter()
    for s in sessions:
        for m in s.messages:
            per_week[_week(m.at)] += 1
        for o in s.occurrences:
            if o.habit == habit:
                hits[_week(o.at)] += 1
    weeks = sorted(w for w in per_week if w)[-TREND_WEEKS:]
    rates = [hits[w] / per_week[w] if per_week[w] >= TREND_MIN_MESSAGES else None for w in weeks]
    top = max((r for r in rates if r is not None), default=0.0)
    spark = " ".join("-" if r is None else str(round(100 * r / top)) if top else "0" for r in rates)
    known = [r for r in rates if r is not None]
    if len(known) < 3:
        return "new", spark
    half = len(known) // 2
    before, after = statistics.median(known[:half]), statistics.median(known[-half:])
    if before and after <= 0.8 * before and known[-1] <= before:
        return "falling", spark
    if after >= 1.2 * before and after > 0:
        return "rising", spark
    return "steady", spark


_TREND_LABELS = {"new": "Too early to say", "falling": "Improving", "rising": "Getting worse", "steady": "Steady"}


def build_section(sessions: list[SessionPrompting]) -> Section:
    """The ``prompting`` section: a row per habit seen, most costly first
    (a big task without a plan, which has no figure, by how often), and,
    once there are coaching notes, a row per hint that asked Claude for a
    tip. No habit rows without a message of yours in the window."""
    messages = sum(len(s.messages) for s in sessions)
    counts: Counter = Counter()
    costs: dict[str, float] = {}
    for s in sessions:
        for o in s.occurrences:
            counts[o.habit] += 1
            if o.cost is not None:
                costs[o.habit] = costs.get(o.habit, 0.0) + o.cost
    rows = []
    for habit in HABITS:
        if not counts[habit]:
            continue
        word, spark = trend(sessions, habit)
        rows.append([
            habit, counts[habit], 100.0 * counts[habit] / max(messages, 1), costs.get(habit), BASIS[habit] or None,
            word, spark, TRY[habit],
        ])
    rows.sort(key=lambda r: (-(r[3] or 0.0), -r[1]))
    habits_table = Table(
        name="prompting_habits",
        title="How you prompt",
        columns=[
            Column(key="habit", label="Habit", kind="str"),
            Column(key="times", label="Times", kind="int"),
            Column(key="per_100", label="Per 100 messages", kind="float"),
            Column(key="cost", label="What it cost", kind="money"),
            Column(key="basis", label="Worked out from", kind="str"),
            Column(key="trend", label="Trend", kind="str"),
            Column(key="weeks", label="By week", kind="str"),
            Column(key="try", label="Try instead", kind="str"),
        ],
        rows=rows,
        value_labels={**TITLES, **_TREND_LABELS},
    )
    notes: Counter = Counter()
    tips: Counter = Counter()
    for s in sessions:
        notes.update(s.notes)
        tips.update(s.tips)
    tips_table = Table(
        name="prompting_tips",
        title="Tips Claude showed",
        columns=[
            Column(key="hint", label="Hint", kind="str"),
            Column(key="notes", label="Notes", kind="int"),
            Column(key="shown", label="Tip shown", kind="int"),
            Column(key="shown_pct", label="Shown", kind="pct"),
        ],
        rows=[
            [hint, notes[hint], tips[hint], 100.0 * tips[hint] / notes[hint]]
            for hint in TIP_HINTS if notes[hint]
        ],
    )
    tables = [habits_table] + ([tips_table] if tips_table.rows else [])
    return Section(key="prompting", title="How you prompt", tables=tables)


def habit_rates(session: SessionPrompting) -> tuple[int, int, int]:
    """``(habits, drip messages, messages)`` for one session, for the
    before-and-after comparison of turning coaching notes on (``impact``)."""
    drip = sum(len(run) for run in _drip_runs(session.messages))
    return len(session.occurrences), drip, len(session.messages)

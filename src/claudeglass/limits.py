"""Usage-limit tracking (v3-limits): turn a 5-hour/weekly usage-cap pause,
the harness's forced early termination of a subagent, and the desktop
app's automatic resume ping into first-class, attributable facts instead
of behavioural noise.

Motivation (see ``model.py``'s "Usage-limits batch" module docstring
section for the full field-level contract this module reads): when the
account hits its usage cap, the harness pauses and, if the stop outlasts
the cache's hour, the first reply after it writes the conversation to
the cache again. Left unattributed, that pause looks exactly like a very
long idle gap, inflating "gaps > 5 min" counts in
``recache.py``/``ttl.py``, making that re-cache look like ordinary TTL
churn, driving ``recommend.py``'s long-tool-wait
heuristic, and occasionally making ``classify.py`` read a session as
"overnight" (Claude working on its own at night while you were away)
purely because the pause — not real overnight work — made the silence
long enough. The pause is no work and no night activity, so it can't.

``events.py``/``parse.py`` already do the actual detection (see their
own module docstrings): a synthetic assistant line's text becomes
``EventKind.LIMIT_HIT`` (subkind ``session_limit``/``weekly_limit``), the
desktop app's resume ping becomes ``EventKind.LIMIT_RESUME``, and a
harness-killed subagent's task notification becomes
``EventKind.AGENT_TERMINATED`` (subkind ``rate_limit``/``other``). Every
turn whose gap to the previous one spanned one of these events carries
``Turn.gap_cause == "limit"``. This module is purely a *reader* of that
already-parsed state — it detects nothing new — and exists to:

- Give ``classify.py`` and ``pieces.py`` a per-session list of usage-cap
  pause intervals (:func:`limit_pause_intervals`) and the overlap of a
  silence with them (:func:`pause_overlap_s`), so their gap and away-time
  figures can leave the pauses out (see classify.py's own module
  docstring for the places that use them).
- Give a session-timeline API a flat, ready-to-render list of markers
  (:func:`limit_markers`).
- Accumulate corpus-wide facts (:class:`LimitStats`) and render them as
  the report's own ``limits`` :class:`~claudeglass.model.Section`
  (:func:`build_section`): limit stops (:class:`Episode`), message,
  resume and termination counts, pause durations, a reset-hour-of-day
  histogram, a by-agent-type roll-up, and the cache-write cost of the
  turn immediately following each pause.
- Show each recent stop (:func:`_stops_table`): when it reset, how long
  before the reset it began, the list-price spend over the 5 hours (the
  weekly limit: 7 days) before it split by who spent it, and how much of
  that ran while three or more agents worked at once; and roll those up
  since :attr:`LimitThresholds.current_since` (:func:`_rollup_table`).
- Cross-check the transcript-derived stop count against
  ``usage-log.csv`` (``tools/log_usage.py``'s own ground-truth log, when
  one exists) via :func:`csv_cross_check` — a sanity check, not a second
  detector.

A limit *stop* is one episode, not one line. A single stop writes a
storm of limit lines (one per retry, one per cut-off agent's notice, 58
in the longest one seen), so counting lines would read a single stop as
dozens. :meth:`LimitStats.episodes` keys every line by (event subkind,
reset floored to the minute), merges same-subkind keys whose resets fall
within two hours, and joins a line with no reset to the same-subkind stop
whose reset lies up to five hours (weekly: seven days) after it. The
parser has already dropped replayed lines by (file, uuid), so a replay
never adds to a stop. Line counts stay as "limit messages".

Privacy: every field this module produces (counts, percentages, USD
totals, token counts, an hour-of-day integer, an enum-like subkind
string) is already privacy-clean per ``model.py``'s contract; this
module never reads or stores message text, a path, or a full command.

Deviation from the plan, reported rather than made silently (project
convention, see ``model.py``'s module docstring): the original brief's
"pauses (hit ts -> next priced turn or resume marker; duration)" is
computed here from ``(turn.ts - turn.gap_s, turn.ts)`` for every turn with
``gap_cause == "limit"`` -- i.e. read directly off ``parse.py``'s own
gap computation -- rather than re-deriving the interval by walking raw
``LIMIT_HIT``/``LIMIT_RESUME`` events and matching them up by hand. The
two are equivalent by construction (``parse.py``'s two-buffer scheme
guarantees the limit event(s) precede exactly the turn that carries
``gap_cause == "limit"``, and ``gap_s`` is that turn's own gap to the
previous finalised turn), and reading it off ``Turn`` is far simpler and
needs no event/turn correlation of its own. The one thing read off the
events is the reset: a pause ends at the earlier of the first reply after
it (the user's return, or the app's resume ping) and the latest reset
among the limit lines in the gap, because the time after the reset is not
the limit's.

Similarly, the "cache writes after a pause" figures do not run their own
re-cache *detection* (``recache.RecacheThresholds``'s ``ctx_floor``/
``cr_ratio``): every turn with ``gap_cause == "limit"`` is priced via
``pricing.price_turn``'s default (observed) path, the same way
``ttl.observed()`` does, without needing ``RecacheThresholds`` at all. If
a stop outlasts the cache's hour, the first reply after it writes the
conversation to the cache again; a reply within the hour reads the cache
as usual. That rewrite is the price of carrying on, not a caching habit
to fix.

Spend over a stop's window is kept in the shape of the store's
``turns_agg`` table: UTC quarter-hour buckets, per transcript and model,
for every priced reply (``turn_index > 0``). The report is built from
full transcripts in the CLI and in the service alike, so
:meth:`LimitStats.add` builds the same buckets in memory rather than
reading the table back. The cut-off and post-pause *dollar* figures count
only runs since :attr:`LimitThresholds.current_since`; counts and tokens
stay all-time.
"""

from __future__ import annotations

import bisect
import csv as csv_mod
import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Sequence

from .model import Column, Event, EventKind, Section, Table, TranscriptResult, Turn, agent_type_label
from .pricing import ModelRates, Pricing, ResolvedRates, price_turn

#: This module's own reporting/cross-check assumptions, printed verbatim
#: in the report's "## Assumptions" block (``ReportMeta.assumptions``)
#: alongside ``recache.ASSUMPTIONS``/``ttl.ASSUMPTIONS``.
ASSUMPTIONS: tuple[str, ...] = (
    "a usage-cap pause's (start, end) interval is read off the turn that "
    "carries Turn.gap_cause == \"limit\" -- start = turn.ts - turn.gap_s, "
    "end = turn.ts, or the latest reset_ts among the LIMIT_HIT events in "
    "that gap when it is earlier -- not re-derived by matching raw "
    "LIMIT_HIT/LIMIT_RESUME events up by hand",
    "a turn immediately following a usage-cap pause is priced from the "
    "cache split it recorded (price_turn's default observed-split path), "
    "not re-detected against recache.py's ctx_floor/cr_ratio thresholds; "
    "whether the stop outlasted the cache's hour is not checked: if it "
    "did, that reply wrote the conversation to the cache again, and a "
    "reply within the hour read the cache as usual; that rewrite is the "
    "price of carrying on, not a caching habit to fix",
    "reset-hour-of-day prefers LIMIT_HIT's own reset_minutes_of_day "
    "(the literal local hour named in the synthetic text) over "
    "converting reset_ts (UTC) through the machine's local zone, which "
    "is used only as a fallback when the text carried no parseable "
    "clause",
    "a limit stop (episode) is keyed by the line's subkind and its reset "
    "floored to the minute, not by rateLimitType (69 real lines lack it); "
    "same-subkind keys whose resets fall within two hours merge, and a "
    "line with no reset joins the same-subkind stop whose reset lies up "
    "to five hours (weekly: seven days) after it. Lines that join none "
    "make one stop per storm, since a storm's lines are one stop however "
    "many retries wrote them",
    "a weekly stop is marked as not stopping work when a main-session "
    "reply lands more than ten minutes after its first line and before "
    "its reset (the margin absorbs agent replies already in flight); a "
    "weekly stop with no reset, or no such reply, counts as stopping work",
    "a subagent is cut off when it has at least one priced reply and its "
    "last limit line is later than its last priced reply; the spend "
    "counted for it is every priced reply it made, and an agent with no "
    "replies is not counted",
    "the usage-log.csv cross-check counts a row as an exhaustion signal "
    "purely on used_percentage >= csv_exhaustion_pct: a bare resets_at "
    "value is present on nearly every row regardless of exhaustion, so "
    "it is not treated as a signal on its own; it compares those rows "
    "with limit stops, and is left out when the log has no five-hour or "
    "weekly rows at all (the desktop app runs no status line)",
)

#: The two ``EventKind.LIMIT_HIT`` subkinds this module ever sees (the
#: other four ``classify_synthetic_text`` outcomes -- overloaded,
#: unsupported_model, autocompact_thrash, other_api_error -- never
#: synthesise a LIMIT_HIT event; see parse.py's module docstring).
HIT_KINDS: tuple[str, ...] = ("session_limit", "weekly_limit")

#: The two ``EventKind.AGENT_TERMINATED`` subkinds (events.py's
#: ``_agent_terminated_subkind``).
TERMINATED_KINDS: tuple[str, ...] = ("rate_limit", "other")

#: The two usage-log.csv windows this module cross-checks (the third,
#: spend_limit, has no transcript-derived analogue).
CSV_WINDOWS: tuple[str, ...] = ("five_hour", "seven_day")

#: Same-subkind limit lines whose resets fall within this many seconds of
#: each other are one stop (:meth:`LimitStats.episodes`).
EPISODE_MERGE_S = 2 * 3600

#: How far after a line without a reset the same-subkind stop's reset may
#: lie for the line to join it, in seconds: the length of the limit's own
#: window.
EPISODE_JOIN_S: dict[str, int] = {"session_limit": 5 * 3600, "weekly_limit": 7 * 86400}

#: A weekly stop did not stop work when a main-session reply lands more
#: than this many seconds after its first line and before its reset. The
#: margin absorbs agent replies already in flight when the limit hit,
#: which land within seconds.
KEPT_WORKING_AFTER_S = 600

#: The reset-hour histogram only earns its advice when one hour holds at
#: least this many stops and this share of them.
BUSY_HOUR_MIN_EPISODES = 3
BUSY_HOUR_MIN_SHARE_PCT = 30.0

#: Spend is kept in UTC quarter-hours, as the store's ``turns_agg`` keeps it.
BUCKET_S = 900

#: A stretch of spend counts as a burst while this many agent transcripts
#: (direct or workflow) are active at once: their first to their last
#: priced reply, to the quarter-hour, covers it.
BURST_AGENTS = 3

#: The burst line on the limit-pressure card shows only when the share of
#: a stop's spend that ran in a burst is this many points above the share
#: across all your work.
BURST_GAP_POINTS = 10.0

#: The three places spend comes from, as the by-source shares name them:
#: the main session, agents started straight from a session, and agents
#: started by a workflow script.
SPEND_SOURCES: tuple[str, ...] = ("main", "direct", "workflow")

#: How many agent type and model pairs a stop's row names.
TOP_SPENDERS = 2

#: The stops table shows this many of the most recent stops.
STOPS_TABLE_ROWS = 50

#: Runs before this day are left out of the cut-off and post-pause dollar
#: figures and the roll-up (:attr:`LimitThresholds.current_since`).
CURRENT_SINCE = "2026-09-18"

#: What woke a session between two limit lines, most telling first (see
#: :func:`wake_class`). ``meta_only`` is its own class: the gap held only
#: ``isMeta`` user lines, which Claude Code writes itself.
WAKE_CLASSES: tuple[str, ...] = ("typed", "resume", "scheduled", "agent_notice", "meta_only", "other")

_TYPED_KINDS = frozenset({EventKind.HUMAN_TEXT, EventKind.SLASH_COMMAND, EventKind.PLAN_FEEDBACK, EventKind.INTERRUPT})
_NOTICE_KINDS = frozenset({EventKind.TASK_NOTIFICATION, EventKind.AGENT_TERMINATED, EventKind.PEER_MESSAGE})


@dataclass(slots=True)
class LimitThresholds:
    """The tunable numbers this module's own logic depends on (its
    detection is entirely upstream, in events.py/parse.py). Mirrors
    ``RecacheThresholds``/``TtlThresholds``'s ``from_config``/``describe``
    convention for consistency.
    """

    #: A usage-log.csv five_hour/seven_day row counts as an exhaustion
    #: signal when its ``used_percentage`` is at or above this.
    csv_exhaustion_pct: float = 100.0
    #: The first day (``YYYY-MM-DD``, UTC) of the runs the cut-off and
    #: post-pause dollar figures and the stops roll-up count, because the
    #: settings you run now began then. Empty counts every run.
    current_since: str = CURRENT_SINCE

    @classmethod
    def from_config(cls, config: dict | None) -> "LimitThresholds":
        data = config or {}
        if not isinstance(data, dict):
            data = {}
        nested = data.get("thresholds")
        if isinstance(nested, dict):
            data = nested
        kwargs: dict = {}
        if "csv_exhaustion_pct" in data:
            kwargs["csv_exhaustion_pct"] = float(data["csv_exhaustion_pct"])
        if "current_since" in data:
            kwargs["current_since"] = str(data["current_since"] or "")
        return cls(**kwargs)

    @property
    def since(self) -> datetime | None:
        """``current_since`` as a UTC time, ``None`` when it is empty or
        not a date."""
        return _utc_ts(self.current_since) if self.current_since else None

    def describe(self) -> list[str]:
        lines = [
            f"A usage log row counts as at the limit when it shows "
            f"{self.csv_exhaustion_pct:.1f}% or more of the 5-hour or weekly "
            "limit used.",
        ]
        since = self.since
        if since is not None:
            lines.append(
                f"Cut-off agent spend, the cost of cache writes after a pause and the stops roll-up "
                f"count runs from {_day_text(since)} on."
            )
        return lines


_DEFAULT_THRESHOLDS = LimitThresholds()


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _utc_ts(ts: str | None) -> datetime | None:
    """``ts`` as an aware time, reading one with no zone as UTC (what the
    harness writes), so two of them can always be set against each other."""
    parsed = _parse_ts(ts)
    if parsed is not None and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _pct(part: float, whole: float) -> float:
    return 100.0 * part / whole if whole else 0.0


def _day_text(when: datetime) -> str:
    """``18 Sep 2026``."""
    return f"{when.day} {when:%b %Y}"


def _family(model: str | None) -> str:
    """The model family a model id names (``opus`` for ``claude-opus-5``),
    ``other`` for an id naming none. Closed words only, never the id."""
    for name in ("haiku", "sonnet", "opus", "fable"):
        if name in (model or ""):
            return name
    return "other"


def _spend_source(kind: str) -> str:
    """Where a transcript's spend counts: ``SPEND_SOURCES`` entry for its
    ``meta.kind``."""
    if kind == "top-level":
        return "main"
    return "workflow" if kind == "workflow-agent" else "direct"


# -- classify.py's and pieces.py's consumers ---------------------------------


def limit_pause_intervals(top: TranscriptResult) -> list[tuple[datetime, datetime]]:
    """Every usage-cap pause in ``top``'s own turns, as ``(start, end)``
    UTC-aware ``datetime`` pairs, oldest first and never overlapping.

    A pause is the wait before a turn with ``Turn.gap_cause == "limit"``:
    ``start`` is the previous reply (the turn's own timestamp minus its
    ``gap_s``, ``parse.py``'s own gap computation run backwards -- see the
    module docstring's deviation note). ``end`` is the earlier of that
    turn's own timestamp, which follows your return (or the desktop app's
    resume ping) by seconds, and the latest ``reset_ts`` among the
    ``LIMIT_HIT`` events in the wait: a limit stops you until its reset,
    and the time after it is yours. A limit line with no ``reset_ts`` adds
    none, so a wait whose lines have none ends at the return.

    Only ``top``'s turns are considered, never any subagent's -- a pause
    is an account-wide event, but the gap/span statistics this feeds
    (``classify._median_and_max_gap``, and the away time behind
    ``classify.classify_mode``'s overnight check) are themselves
    top-level-only (see ``classify.py``'s module docstring on which
    signals are ``top``-only vs. summed across ``subs``). :meth:`LimitStats.add` calls it for a subagent's transcript
    too, to time that transcript's own pauses.
    """
    waits: list[tuple[datetime, datetime]] = []
    for turn in top.turns:
        if turn.gap_cause != "limit" or turn.gap_s is None:
            continue
        end = _utc_ts(turn.ts)
        if end is None:
            continue
        waits.append((end - timedelta(seconds=turn.gap_s), end))
    if not waits:
        return []
    resets = _limit_resets(top)
    intervals = []
    for start, end in waits:
        reset = max((at for hit, at in resets if start <= hit <= end and at > start), default=None)
        intervals.append((start, end if reset is None else min(end, reset)))
    return intervals


def _limit_resets(top: TranscriptResult) -> list[tuple[datetime, datetime]]:
    """``(when the limit line was written, its reset)`` for every
    ``LIMIT_HIT`` event in ``top`` that carries a ``reset_ts``. Both are
    UTC-aware (a time with no zone is read as UTC), and a line whose time
    doesn't parse is left out."""
    out = []
    for event in top.events:
        if event.kind != EventKind.LIMIT_HIT:
            continue
        hit, reset = _utc_ts(event.ts), _utc_ts(event.detail.get("reset_ts"))
        if hit is not None and reset is not None:
            out.append((hit, reset))
    return out


def pause_overlap_s(start: datetime, end: datetime, pauses: Sequence[tuple[datetime, datetime]]) -> float:
    """Seconds of ``[start, end]`` inside ``pauses`` (``limit_pause_intervals``'
    pairs, which never overlap each other, so each one's share is summed). A
    silence minus this is the time nothing but you was holding the work up."""
    total = 0.0
    for p_start, p_end in pauses:
        overlap = (min(end, p_end) - max(start, p_start)).total_seconds()
        if overlap > 0:
            total += overlap
    return total


def limit_markers(result: TranscriptResult) -> list[tuple[str, str, dict]]:
    """Every ``LIMIT_HIT``/``LIMIT_RESUME``/``AGENT_TERMINATED`` event in
    ``result``, as ``(ts, kind, detail)`` triples, sorted by ``ts``
    (events with no timestamp sort first, as ``""``).

    ``kind`` is the event kind's own string value
    (``"limit_hit"``/``"limit_resume"``/``"agent_terminated"``);
    ``detail`` is a shallow copy of the event's own ``detail`` dict, plus
    ``"subkind"`` when the event carries one -- ready for a session
    timeline API to render as markers without importing ``EventKind``
    itself. See the module docstring's marker-contract note.
    """
    markers: list[tuple[str, str, dict]] = []
    for event in result.events:
        if event.kind not in (EventKind.LIMIT_HIT, EventKind.LIMIT_RESUME, EventKind.AGENT_TERMINATED):
            continue
        detail = dict(event.detail)
        if event.subkind is not None:
            detail["subkind"] = event.subkind
        markers.append((event.ts or "", event.kind.value, detail))
    markers.sort(key=lambda m: m[0])
    return markers


def _reset_local_hour(detail: dict) -> int | None:
    """Local hour of day (0-23) a ``LIMIT_HIT`` event's reset falls at.

    Prefers ``reset_minutes_of_day`` (the literal local hour the
    synthetic text itself named, e.g. "resets 3:00pm") over converting
    ``reset_ts`` (always UTC) through the machine's own local zone --
    the latter is only a fallback for the minority of hits whose text
    carried no parseable "resets ..." clause at all (see the module
    docstring's assumptions).
    """
    minutes = detail.get("reset_minutes_of_day")
    if isinstance(minutes, int):
        return (minutes // 60) % 24
    reset_ts = detail.get("reset_ts")
    if not isinstance(reset_ts, str):
        return None
    dt = _parse_ts(reset_ts)
    if dt is None:
        return None
    return dt.astimezone().hour


# -- stops (episodes) ---------------------------------------------------------

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def wake_class(kinds: set[EventKind] | frozenset[EventKind]) -> str:
    """What woke a session between two limit lines, from the kinds of line
    the gap held. Something you typed comes first, then the desktop app's
    resume ping, a scheduled task, a background agent's notice, and last
    the ``isMeta`` user lines Claude Code writes itself (``meta_only``: the
    gap held no other kind of user line). Anything else, an empty gap
    included, is ``other``.
    """
    if kinds & _TYPED_KINDS:
        return "typed"
    if EventKind.LIMIT_RESUME in kinds:
        return "resume"
    if EventKind.SCHEDULED_TASK in kinds:
        return "scheduled"
    if kinds & _NOTICE_KINDS:
        return "agent_notice"
    if EventKind.META in kinds:
        return "meta_only"
    return "other"


def busy_reset_hour(counts: dict[int, int]) -> int | None:
    """The local hour holding at least ``BUSY_HOUR_MIN_EPISODES`` stops and
    ``BUSY_HOUR_MIN_SHARE_PCT`` of all of them, else ``None``. ``counts``
    is :meth:`LimitStats.reset_hour_counts`' shape; on a tie the earlier
    hour wins."""
    total = sum(counts.values())
    if not total:
        return None
    hour, stops = max(sorted(counts.items()), key=lambda item: item[1])
    if stops >= BUSY_HOUR_MIN_EPISODES and _pct(stops, total) >= BUSY_HOUR_MIN_SHARE_PCT:
        return hour
    return None


@dataclass(slots=True)
class _Hit:
    """One limit line, kept until :meth:`LimitStats.episodes` groups it."""

    #: The ``LIMIT_HIT`` subkind: ``session_limit`` or ``weekly_limit``.
    kind: str
    #: When the line was written, and the reset it named floored to the
    #: minute, each ``None`` when absent or unreadable.
    at: datetime | None
    reset: datetime | None
    #: The local hour of that reset (:func:`_reset_local_hour`).
    hour: int | None
    session_id: str
    #: The transcript's ``meta.kind``.
    transcript_kind: str
    #: Set on a subagent's last limit line when the limit cut that agent
    #: off: the agent's ``meta.kind``, and the list-price spend of its
    #: priced replies.
    cut_off: str | None = None
    cut_off_cost_usd: float = 0.0


@dataclass(slots=True)
class Episode:
    """One limit stop: every limit line that belongs to it, whichever
    transcript wrote it (see the module docstring for the key)."""

    #: ``session_limit`` (the 5-hour limit) or ``weekly_limit``.
    kind: str
    first_at: datetime | None
    last_at: datetime | None
    #: The latest reset among its lines, to the minute; ``None`` when no
    #: line named one.
    reset: datetime | None
    reset_hour: int | None
    #: Limit lines in the stop: all of them, those in main sessions, the rest.
    messages: int
    main_messages: int
    agent_messages: int
    #: Top-level sessions with a line in the stop.
    sessions: frozenset[str]
    #: Subagents this stop cut off, by ``meta.kind``, with their spend.
    cut_off_direct: int = 0
    cut_off_workflow: int = 0
    cut_off_direct_cost_usd: float = 0.0
    cut_off_workflow_cost_usd: float = 0.0
    #: A weekly stop only: main-session replies carried on after it began
    #: and before its reset, so it did not stop the work.
    kept_working: bool = False
    #: The earliest reset among its lines, to the minute: the reset of the
    #: window its first line was written in, which opens its spend window.
    #: ``None`` when no line named one.
    first_reset: datetime | None = None

    @property
    def cut_off(self) -> int:
        return self.cut_off_direct + self.cut_off_workflow

    @property
    def cut_off_cost_usd(self) -> float:
        return self.cut_off_direct_cost_usd + self.cut_off_workflow_cost_usd

    @property
    def stopped_work(self) -> bool:
        return not self.kept_working


def _group_hits(hits: list[_Hit], kind: str) -> list[list[_Hit]]:
    """``hits`` (all of one subkind) grouped into stops: lines with a reset
    by that reset, merging resets within ``EPISODE_MERGE_S``; a line with
    none joins the stop whose reset lies soonest after it, within
    ``EPISODE_JOIN_S``; the rest make one stop per storm."""
    keyed = sorted((h for h in hits if h.reset is not None), key=lambda h: h.reset)
    groups: list[list[_Hit]] = []
    for hit in keyed:
        if groups and (hit.reset - groups[-1][-1].reset).total_seconds() <= EPISODE_MERGE_S:
            groups[-1].append(hit)
        else:
            groups.append([hit])
    # Read before any joining: a joined line has no reset to read.
    resets = [group[-1].reset for group in groups]
    window = EPISODE_JOIN_S[kind]
    storms: list[list[_Hit]] = []
    for hit in sorted((h for h in hits if h.reset is None), key=lambda h: (h.at is None, h.at or _EPOCH)):
        ahead = [(reset - hit.at).total_seconds() for reset in resets] if hit.at is not None else []
        joinable = [(wait, i) for i, wait in enumerate(ahead) if 0 <= wait <= window]
        if joinable:
            groups[min(joinable)[1]].append(hit)
            continue
        first = storms[-1][0] if storms else None
        same_storm = first is not None and (
            first.at is None
            if hit.at is None
            else first.at is not None and (hit.at - first.at).total_seconds() <= window
        )
        if same_storm:
            storms[-1].append(hit)
        else:
            storms.append([hit])
    return [*groups, *storms]


def _make_episode(kind: str, hits: list[_Hit], replies_s: list[float]) -> Episode:
    """One :class:`Episode` from its lines. ``replies_s`` is every
    main-session reply's time in epoch seconds, sorted."""
    ats = [h.at for h in hits if h.at is not None]
    dated = [h for h in hits if h.reset is not None]
    latest = max(dated, key=lambda h: h.reset) if dated else None
    reset = latest.reset if latest is not None else None
    hour = latest.hour if latest is not None else None
    if hour is None:
        common = Counter(h.hour for h in hits if h.hour is not None).most_common(1)
        hour = common[0][0] if common else None
    first_at = min(ats, default=None)
    episode = Episode(
        kind=kind,
        first_at=first_at,
        last_at=max(ats, default=None),
        reset=reset,
        reset_hour=hour,
        messages=len(hits),
        main_messages=sum(1 for h in hits if h.transcript_kind == "top-level"),
        agent_messages=sum(1 for h in hits if h.transcript_kind != "top-level"),
        sessions=frozenset(h.session_id for h in hits if h.session_id),
        first_reset=min((h.reset for h in dated), default=None),
    )
    for hit in hits:
        if hit.cut_off == "workflow-agent":
            episode.cut_off_workflow += 1
            episode.cut_off_workflow_cost_usd += hit.cut_off_cost_usd
        elif hit.cut_off is not None:
            episode.cut_off_direct += 1
            episode.cut_off_direct_cost_usd += hit.cut_off_cost_usd
    if kind == "weekly_limit" and first_at is not None and reset is not None:
        # The first reply later than the margin after the first line.
        index = bisect.bisect_right(replies_s, first_at.timestamp() + KEPT_WORKING_AFTER_S)
        episode.kept_working = index < len(replies_s) and replies_s[index] < reset.timestamp()
    return episode


# -- spend over a stop ---------------------------------------------------------


@dataclass(slots=True)
class StopSpend:
    """List-price spend over a stretch of quarter-hours, split by who
    spent it."""

    total: float = 0.0
    #: Spend by ``SPEND_SOURCES`` entry.
    by_source: dict[str, float] = field(default_factory=lambda: {name: 0.0 for name in SPEND_SOURCES})
    #: The part of ``total`` spent in quarter-hours with ``BURST_AGENTS`` or
    #: more agent transcripts active.
    burst: float = 0.0
    #: ``(agent type, model family)`` -> spend; the agent type is
    #: ``"top-level"`` for the main session.
    by_spender: dict[tuple[str, str], float] = field(default_factory=dict)

    def share(self, part: float) -> float | None:
        """``part`` as a percentage of ``total``, ``None`` when nothing was
        spent (a stretch with no spend has no shares)."""
        return _pct(part, self.total) if self.total > 0 else None

    @property
    def largest_source(self) -> str | None:
        """The ``SPEND_SOURCES`` entry that spent the most (the earlier one
        on a tie), ``None`` when nothing was spent."""
        if self.total <= 0:
            return None
        return max(SPEND_SOURCES, key=self.by_source.__getitem__)

    def top_spenders(self, count: int = TOP_SPENDERS) -> list[tuple[str, str, float]]:
        """The ``count`` biggest ``(agent type, model family, spend)``."""
        ranked = sorted(self.by_spender.items(), key=lambda item: (-item[1], item[0]))
        return [(label, family, cost) for (label, family), cost in ranked[:count] if cost > 0]


@dataclass(slots=True)
class StopsRollup:
    """The stops since ``LimitThresholds.current_since`` that stopped work
    and have a window. ``five_hour`` and ``weekly`` count them by kind.
    ``stops`` and ``spend`` cover the 5-hour stops' windows, counted once
    per quarter-hour; the weekly stops' windows only when no 5-hour stop
    counts, because a weekly window holds the whole week before the stop
    and would set the burst share against itself."""

    since: datetime | None
    #: How many stops ``spend`` covers: the 5-hour ones, or the weekly ones
    #: when there is no 5-hour stop.
    stops: int
    five_hour: int
    weekly: int
    spend: StopSpend
    #: Share of all the spend since ``since`` that ran in a burst, ``None``
    #: when nothing was spent.
    all_burst_pct: float | None

    @property
    def burst_pct(self) -> float | None:
        return self.spend.share(self.spend.burst)

    @property
    def burst_stands_out(self) -> bool:
        """Whether a stop's spend ran in a burst clearly more often than
        your work does overall (``BURST_GAP_POINTS`` more)."""
        return burst_stands_out(self.burst_pct, self.all_burst_pct)


def burst_stands_out(in_stops_pct: float | None, overall_pct: float | None) -> bool:
    """``in_stops_pct`` is at least ``BURST_GAP_POINTS`` above
    ``overall_pct`` (both percentages, either may be missing)."""
    if in_stops_pct is None or overall_pct is None:
        return False
    return in_stops_pct >= overall_pct + BURST_GAP_POINTS


def stop_window(episode: Episode) -> range | None:
    """The quarter-hour buckets (start times, epoch seconds) from the one
    holding the start of the limit's window (5 hours before the earliest
    reset its lines named; weekly, 7 days) to the one holding the stop's
    first message. ``None`` when the stop has no reset or no time. Empty
    when the first message came before that start."""
    if episode.reset is None or episode.first_at is None:
        return None
    start = (episode.first_reset or episode.reset) - timedelta(seconds=EPISODE_JOIN_S[episode.kind])
    first = int(start.timestamp()) // BUCKET_S * BUCKET_S
    last = int(episode.first_at.timestamp()) // BUCKET_S * BUCKET_S
    return range(first, last + 1, BUCKET_S)


# -- accumulator --------------------------------------------------------------


@dataclass(slots=True)
class _RawAccumulator:
    """Mutable running totals for one agent-type key (mirrors
    ``ttl._RawAccumulator``'s own pattern)."""

    key: str
    transcripts: int = 0
    session_limit_hits: int = 0
    weekly_limit_hits: int = 0
    resumes: int = 0
    terminated_rate_limit: int = 0
    terminated_other: int = 0
    pause_seconds: list[float] = field(default_factory=list)
    limit_turn_cc_tokens: int = 0
    #: One entry per reply after a pause: its time in epoch seconds (``None``
    #: when unreadable), its cache-write cost and its total cost.
    limit_turns: list[tuple[float | None, float, float]] = field(default_factory=list)


@dataclass(slots=True)
class LimitTypeStats:
    """Rolled-up usage-limit stats for one agent type (or
    ``"top-level"``) -- the values behind one row of ``build_section``'s
    by-agent-type table. ``hits`` counts limit messages, not stops."""

    key: str
    transcripts: int
    session_limit_hits: int
    weekly_limit_hits: int
    resumes: int
    terminated_rate_limit: int
    terminated_other: int
    pause_count: int
    pause_total_s: float
    pause_median_s: float | None
    pause_max_s: float | None
    limit_turn_cc_tokens: int
    limit_turn_write_cost_usd: float
    limit_turn_total_cost_usd: float

    @property
    def hits(self) -> int:
        return self.session_limit_hits + self.weekly_limit_hits

    @property
    def terminated(self) -> int:
        return self.terminated_rate_limit + self.terminated_other


class LimitStats:
    """Accumulates usage-limit facts across every transcript in a corpus,
    for :func:`build_section` to render. Mirrors ``RecacheStats``'s/
    ``TtlStats``'s own accumulate-then-render shape.
    """

    def __init__(self) -> None:
        self.transcripts = 0
        #: Top-level sessions with a limit line in them: a subagent's line
        #: counts for the session that ran it.
        self.sessions_affected: set[str] = set()
        self._by_key: dict[str, _RawAccumulator] = {}
        self._hits: list[_Hit] = []
        #: Every main-session reply's time (epoch seconds), for telling
        #: whether work carried on through a weekly stop.
        self._main_reply_s: list[float] = []
        #: The span the corpus covers (epoch seconds), for ``window_days``.
        self._first_s: float | None = None
        self._last_s: float | None = None
        self._wake_gaps: dict[str, int] = {name: 0 for name in WAKE_CLASSES}
        self._episodes: list[Episode] | None = None
        #: List-price spend per quarter-hour bucket (epoch seconds), by
        #: ``(spend source, agent type, model family)``: ``turns_agg``'s shape.
        self._spend: dict[int, dict[tuple[str, str, str], float]] = {}
        #: Each agent transcript's first and last priced bucket.
        self._agent_spans: list[tuple[int, int]] = []
        self._agent_index: tuple[list[int], list[int]] | None = None

    def _acc(self, key: str) -> _RawAccumulator:
        acc = self._by_key.get(key)
        if acc is None:
            acc = _RawAccumulator(key=key)
            self._by_key[key] = acc
        return acc

    def _see_time(self, at: datetime | None) -> None:
        if at is None:
            return
        seconds = at.timestamp()
        if self._first_s is None or seconds < self._first_s:
            self._first_s = seconds
        if self._last_s is None or seconds > self._last_s:
            self._last_s = seconds

    def add(
        self,
        result: TranscriptResult,
        rates_lookup: Callable[[str], "ResolvedRates | ModelRates | None"] | None = None,
    ) -> None:
        """Fold one transcript (top-level or subagent) into the
        accumulator. ``rates_lookup`` (a per-model rate resolver, e.g.
        ``pricing.Pricing.resolve_model``) is optional -- omitting it
        still accumulates every count/duration table, only the
        limit-turn and cut-off-agent cost columns stay at zero.
        """
        self.transcripts += 1
        self._episodes = None
        agent_type = agent_type_label(result)
        acc = self._acc(agent_type)
        acc.transcripts += 1

        top_level = result.meta.kind == "top-level"
        hits: list[_Hit] = []
        # Kinds of line seen since the last limit line, once there was one.
        between: set[EventKind] | None = None
        for event in result.events:
            if event.kind == EventKind.LIMIT_HIT:
                if event.subkind == "weekly_limit":
                    acc.weekly_limit_hits += 1
                else:
                    acc.session_limit_hits += 1
                reset = _utc_ts(event.detail.get("reset_ts"))
                hits.append(
                    _Hit(
                        kind="weekly_limit" if event.subkind == "weekly_limit" else "session_limit",
                        at=_utc_ts(event.ts),
                        reset=reset.replace(second=0, microsecond=0) if reset is not None else None,
                        hour=_reset_local_hour(event.detail),
                        session_id=result.meta.session_id,
                        transcript_kind=result.meta.kind,
                    )
                )
                if between is not None and top_level:
                    self._wake_gaps[wake_class(between)] += 1
                between = set()
                continue
            if between is not None:
                between.add(event.kind)
            if event.kind == EventKind.LIMIT_RESUME:
                acc.resumes += 1
            elif event.kind == EventKind.AGENT_TERMINATED:
                if event.subkind == "rate_limit":
                    acc.terminated_rate_limit += 1
                else:
                    acc.terminated_other += 1

        for start, end in limit_pause_intervals(result):
            acc.pause_seconds.append((end - start).total_seconds())

        source = _spend_source(result.meta.kind)
        buckets: list[int] = []
        for turn in result.turns:
            if turn.turn_index <= 0:
                continue
            at = _utc_ts(turn.ts)
            breakdown = price_turn(turn, rates_lookup(turn.model)) if rates_lookup is not None else None
            if turn.gap_cause == "limit":
                acc.limit_turn_cc_tokens += turn.cache_creation_tokens
                if breakdown is not None:
                    seen = at.timestamp() if at is not None else None
                    acc.limit_turns.append((seen, breakdown.cache_write_cost, breakdown.total))
            if at is None:
                continue
            bucket = int(at.timestamp()) // BUCKET_S * BUCKET_S
            buckets.append(bucket)
            if breakdown is not None:
                entries = self._spend.setdefault(bucket, {})
                key = (source, agent_type, _family(turn.model))
                entries[key] = entries.get(key, 0.0) + breakdown.total
        if buckets and source != "main":
            self._agent_spans.append((min(buckets), max(buckets)))
            self._agent_index = None

        if hits and result.meta.session_id:
            self.sessions_affected.add(result.meta.session_id)
        if hits and not top_level:
            self._mark_cut_off(result, hits, rates_lookup)
        self._hits.extend(hits)
        self._see_span(result, hits, top_level)

    def _mark_cut_off(
        self,
        result: TranscriptResult,
        hits: list[_Hit],
        rates_lookup: Callable[[str], "ResolvedRates | ModelRates | None"] | None,
    ) -> None:
        """Mark a subagent's last limit line as the one that cut it off:
        the agent has a priced reply, and that line is later than its last
        one. An agent with no reply, or no line time, is never cut off."""
        replies = [
            at
            for turn in result.turns
            if turn.turn_index > 0 and not turn.estimated
            for at in [_utc_ts(turn.ts)]
            if at is not None
        ]
        dated = [hit for hit in hits if hit.at is not None]
        if not replies or not dated:
            return
        last = max(dated, key=lambda hit: hit.at)
        if last.at <= max(replies):
            return
        last.cut_off = result.meta.kind
        if rates_lookup is not None:
            last.cut_off_cost_usd = sum(
                price_turn(turn, rates_lookup(turn.model)).total for turn in result.turns if turn.turn_index > 0
            )

    def _see_span(self, result: TranscriptResult, hits: list[_Hit], top_level: bool) -> None:
        """Fold this transcript's times into the corpus span, and, for a
        main session, its replies into the list ``kept_working`` reads."""
        for hit in hits:
            self._see_time(hit.at)
        if top_level:
            for turn in result.turns:
                if turn.turn_index > 0 and not turn.estimated:
                    at = _utc_ts(turn.ts)
                    if at is not None:
                        self._see_time(at)
                        self._main_reply_s.append(at.timestamp())
        elif result.turns:
            self._see_time(_utc_ts(result.turns[0].ts))
            self._see_time(_utc_ts(result.turns[-1].ts))

    def episodes(self) -> list[Episode]:
        """Every limit stop so far, oldest first (a stop with no time
        last). See the module docstring for how lines make a stop."""
        if self._episodes is None:
            replies = sorted(self._main_reply_s)
            built = [
                _make_episode(kind, group, replies)
                for kind in HIT_KINDS
                for group in _group_hits([h for h in self._hits if h.kind == kind], kind)
            ]
            built.sort(key=lambda e: (e.first_at is None, e.first_at or _EPOCH, e.kind))
            self._episodes = built
        return self._episodes

    def stop_counts(self) -> dict[str, int]:
        """``HIT_KINDS`` entry -> number of stops of that kind."""
        counts = {kind: 0 for kind in HIT_KINDS}
        for episode in self.episodes():
            counts[episode.kind] += 1
        return counts

    @property
    def window_days(self) -> int:
        """Days the corpus covers, from its earliest to its latest
        timestamp, rounded up and never below 1. The report's window is
        only a label, so the stops-per-week rate is read against this."""
        if self._first_s is None or self._last_s is None:
            return 1
        return max(1, math.ceil((self._last_s - self._first_s) / 86400))

    def wake_gap_counts(self) -> dict[str, int]:
        """``WAKE_CLASSES`` entry -> gaps between two limit lines in a main
        session that it describes (:func:`wake_class`)."""
        return dict(self._wake_gaps)

    def by_key(self, since: datetime | None = None) -> list[LimitTypeStats]:
        """One :class:`LimitTypeStats` per agent type seen so far, sorted by
        key. The two post-pause dollar figures count only replies at or
        after ``since`` (all of them when it is ``None``); every count and
        token figure is all-time."""
        floor = since.timestamp() if since is not None else None
        rows = []
        for key in sorted(self._by_key):
            acc = self._by_key[key]
            counted = [
                (write, total) for seen, write, total in acc.limit_turns if floor is None or (seen is not None and seen >= floor)
            ]
            rows.append(
                LimitTypeStats(
                    key=key,
                    transcripts=acc.transcripts,
                    session_limit_hits=acc.session_limit_hits,
                    weekly_limit_hits=acc.weekly_limit_hits,
                    resumes=acc.resumes,
                    terminated_rate_limit=acc.terminated_rate_limit,
                    terminated_other=acc.terminated_other,
                    pause_count=len(acc.pause_seconds),
                    pause_total_s=sum(acc.pause_seconds),
                    pause_median_s=_median(acc.pause_seconds),
                    pause_max_s=max(acc.pause_seconds) if acc.pause_seconds else None,
                    limit_turn_cc_tokens=acc.limit_turn_cc_tokens,
                    limit_turn_write_cost_usd=sum(write for write, _total in counted),
                    limit_turn_total_cost_usd=sum(total for _write, total in counted),
                )
            )
        return rows

    def cut_off_spend(self, since: datetime | None = None) -> tuple[float, float]:
        """``(direct, workflow)``: what the agents cut off by stops that
        began at or after ``since`` spent (every stop when it is ``None``;
        a stop with no time is then left out)."""
        direct = workflow = 0.0
        for episode in self.episodes():
            if since is not None and (episode.first_at is None or episode.first_at < since):
                continue
            direct += episode.cut_off_direct_cost_usd
            workflow += episode.cut_off_workflow_cost_usd
        return direct, workflow

    def _agents_at(self, bucket: int) -> int:
        """Agent transcripts (direct and workflow) whose first to last
        priced bucket covers ``bucket``."""
        if self._agent_index is None:
            self._agent_index = (
                sorted(first for first, _last in self._agent_spans),
                sorted(last for _first, last in self._agent_spans),
            )
        firsts, lasts = self._agent_index
        return bisect.bisect_right(firsts, bucket) - bisect.bisect_left(lasts, bucket)

    def spend_over(self, buckets: Sequence[int]) -> StopSpend:
        """The list-price spend in ``buckets`` (quarter-hour start times,
        epoch seconds, each counted once), by who spent it and how much of
        it ran while ``BURST_AGENTS`` or more agents were active."""
        spend = StopSpend()
        for bucket in sorted(set(buckets)):
            entries = self._spend.get(bucket)
            if not entries:
                continue
            bucket_total = 0.0
            for (source, label, family), cost in entries.items():
                bucket_total += cost
                spend.by_source[source] += cost
                spend.by_spender[(label, family)] = spend.by_spender.get((label, family), 0.0) + cost
            spend.total += bucket_total
            if self._agents_at(bucket) >= BURST_AGENTS:
                spend.burst += bucket_total
        return spend

    def stop_spend(self, episode: Episode) -> StopSpend | None:
        """:meth:`spend_over` the stop's own window (:func:`stop_window`),
        ``None`` when it has none."""
        window = stop_window(episode)
        return None if window is None else self.spend_over(window)

    def rollup(self, since: datetime | None = None) -> StopsRollup:
        """The stops that began at or after ``since`` (every one when it is
        ``None``), stopped work and have a window, against the burst share
        of all the spend since ``since``. The spend is that of the 5-hour
        stops' windows, counted once per quarter-hour; the weekly stops'
        windows are used only when no 5-hour stop counts."""
        counted = [
            episode
            for episode in self.episodes()
            if episode.stopped_work
            and episode.first_at is not None
            and (since is None or episode.first_at >= since)
            and stop_window(episode) is not None
        ]
        five_hour = [episode for episode in counted if episode.kind == "session_limit"]
        # A weekly window is the whole week before the stop, so it would set the
        # burst share against itself. Add up the 5-hour windows, and the weekly
        # ones only when no 5-hour stop counts.
        summed = five_hour or counted
        buckets = [bucket for episode in summed for bucket in stop_window(episode) or ()]
        floor = int(since.timestamp()) // BUCKET_S * BUCKET_S if since is not None else None
        everything = self.spend_over([bucket for bucket in self._spend if floor is None or bucket >= floor])
        return StopsRollup(
            since=since,
            stops=len(summed),
            five_hour=len(five_hour),
            weekly=len(counted) - len(five_hour),
            spend=self.spend_over(buckets),
            all_burst_pct=everything.share(everything.burst),
        )

    def reset_hour_counts(self) -> dict[int, int]:
        """Hour (0-23) -> number of stops whose reset resolved to that
        local hour: one per stop, however many lines it wrote."""
        counts: dict[int, int] = {h: 0 for h in range(24)}
        for episode in self.episodes():
            if episode.reset_hour is not None:
                counts[episode.reset_hour] = counts.get(episode.reset_hour, 0) + 1
        return counts


# -- report section -----------------------------------------------------------


def build_section(stats: LimitStats, pricing: Pricing | None = None, th: LimitThresholds | None = None) -> Section:
    """Render ``stats`` into the ``limits`` report section: summary
    (stops first), message-kind split, terminated split, pause
    distribution, reset-hour histogram, what woke the session between
    limit messages, and the by-agent-type roll-up. The stops table and its
    roll-up sit beside the summary.
    """
    th = th or _DEFAULT_THRESHOLDS
    since = th.since
    rows = stats.by_key(since)

    tables = [
        _summary_table(stats, rows, since),
        _rollup_table(stats.rollup(since), since),
        _stops_table(stats),
        _hit_kind_table(rows),
        _terminated_table(rows),
        _pause_table(rows),
        _reset_hour_table(stats.reset_hour_counts()),
        _wake_table(stats.wake_gap_counts()),
        _by_agent_type_table(rows),
    ]

    notes = [f"Thresholds: {' '.join(th.describe())}"]
    if pricing is not None:
        notes.append(f"The cost of cache writes after a pause uses prices from pricing.toml, version {pricing.version}.")

    return Section(key="limits", title="Usage limits", tables=tables, notes=notes)


def _summary_table(stats: LimitStats, rows: list[LimitTypeStats], since: datetime | None = None) -> Table:
    episodes = stats.episodes()
    stops = stats.stop_counts()
    weekly = [e for e in episodes if e.kind == "weekly_limit"]
    total_hits = sum(r.hits for r in rows)
    total_session = sum(r.session_limit_hits for r in rows)
    total_weekly = sum(r.weekly_limit_hits for r in rows)
    total_resumes = sum(r.resumes for r in rows)
    total_terminated = sum(r.terminated for r in rows)
    total_terminated_rate_limit = sum(r.terminated_rate_limit for r in rows)
    total_pause_count = sum(r.pause_count for r in rows)
    total_pause_s = sum(r.pause_total_s for r in rows)
    total_cc_tokens = sum(r.limit_turn_cc_tokens for r in rows)
    total_write_cost = sum(r.limit_turn_write_cost_usd for r in rows)
    cut_direct = sum(e.cut_off_direct for e in episodes)
    cut_workflow = sum(e.cut_off_workflow for e in episodes)
    cut_direct_cost, cut_workflow_cost = stats.cut_off_spend(since)
    return Table(
        name="limits_summary",
        title="Usage-limits summary",
        columns=[
            Column(key="metric", label="Metric", kind="str"),
            Column(key="five_hour_stops", label="5-hour limit stops", kind="int"),
            Column(key="weekly_stops", label="Weekly limit stops", kind="int"),
            Column(key="weekly_stops_stopped_work", label="Weekly stops that stopped work", kind="int"),
            Column(key="window_days", label="Days covered", kind="int"),
            Column(key="sessions_affected", label="Sessions affected", kind="int"),
            Column(key="agents_cut_off", label="Agents cut off", kind="int"),
            Column(key="agents_cut_off_direct", label="...direct agents", kind="int"),
            Column(key="agents_cut_off_workflow", label="...workflow agents", kind="int"),
            Column(key="cut_off_direct_cost_usd", label="Spend of direct agents cut off", kind="money"),
            Column(key="cut_off_workflow_cost_usd", label="Spend of workflow agents cut off", kind="money"),
            Column(key="limit_hits", label="Limit messages", kind="int"),
            Column(key="session_limit_hits", label="5-hour limit messages", kind="int"),
            Column(key="weekly_limit_hits", label="Weekly limit messages", kind="int"),
            Column(key="limit_resumes", label="Limit resumes", kind="int"),
            Column(key="agents_terminated", label="Agents terminated", kind="int"),
            Column(key="agents_terminated_rate_limit", label="...by rate limit", kind="int"),
            Column(key="pause_count", label="Pauses", kind="int"),
            Column(key="pause_total_s", label="Total pause time", kind="secs"),
            Column(key="limit_turn_cc_tokens", label="Cache-creation tokens (post-pause turns)", kind="tokens"),
            Column(key="limit_turn_write_cost_usd", label="Cache-write cost (post-pause turns)", kind="money"),
            Column(key="transcripts", label="Transcripts", kind="int"),
        ],
        rows=[
            [
                "all",
                stops["session_limit"],
                stops["weekly_limit"],
                sum(1 for e in weekly if e.stopped_work),
                stats.window_days,
                len(stats.sessions_affected),
                cut_direct + cut_workflow,
                cut_direct,
                cut_workflow,
                round(cut_direct_cost, 6),
                round(cut_workflow_cost, 6),
                total_hits,
                total_session,
                total_weekly,
                total_resumes,
                total_terminated,
                total_terminated_rate_limit,
                total_pause_count,
                round(total_pause_s, 3),
                total_cc_tokens,
                round(total_write_cost, 6),
                stats.transcripts,
            ]
        ],
        notes=[
            "A stop is one limit being reached, however many limit messages "
            "it wrote: each retry and each cut-off agent's notice adds one. "
            "Limit lines with the same kind and a reset within two hours of "
            "each other are one stop.",
            "A weekly stop did not stop work when your main session kept "
            "replying more than ten minutes after it began and before its "
            "reset. An agent counts as cut off when it made at least one "
            "reply and its last limit message is later than its last reply.",
            "If a stop outlasts the cache's hour, the first reply after it "
            "writes the conversation to the cache again. A reply within the "
            "hour reads the cache as usual. That rewrite is the price of "
            "carrying on, not a caching habit to fix.",
            "The spend of agents cut off and the cost of cache writes after "
            "a pause count only runs since the date in the thresholds note. "
            "Every count and token figure covers the whole window.",
            "\"Cost of cache writes after a pause\" counts every reply after "
            "a pause. The rebuild cost after a pause in the cache rebuild "
            "tables counts only replies that also pass the rebuild check. "
            "So the two are related but not equal: "
            "docs/limits.md explains the difference.",
        ],
    )


#: How the roll-up names each ``SPEND_SOURCES`` entry in its sentence.
_SOURCE_NAMES: dict[str, str] = {"main": "The main session", "direct": "Direct agents", "workflow": "Workflow agents"}

#: A stop's kind as the stops table shows it.
_KIND_LABELS: dict[str, str] = {"session_limit": "5-hour", "weekly_limit": "Weekly"}


def _share_pct(spend: StopSpend, source: str) -> float | None:
    return spend.share(spend.by_source[source])


def _rollup_sentence(rollup: StopsRollup) -> str:
    """The line the roll-up table leads with: how many stops, who spent the
    most before them, and how much of it ran in a burst. It names the 5-hour
    limit, or the weekly one when no 5-hour stop counts, to match the stops
    whose spend it describes. Numbers only, never a name from a transcript."""
    since = f"Since {_day_text(rollup.since)} " if rollup.since is not None else ""
    if not rollup.stops:
        return (
            f"No limit stop since {_day_text(rollup.since)} stopped your work and named a reset."
            if rollup.since is not None
            else "No limit stop stopped your work and named a reset."
        )
    times = "1 time" if rollup.stops == 1 else f"{rollup.stops} times"
    what = "your 5-hour limit" if rollup.five_hour else "your weekly limit"
    sentences = [f"{since}{what} stopped you {times}." if since else f"{what.capitalize()} stopped you {times}."]
    largest = rollup.spend.largest_source
    if largest is not None:
        share = _share_pct(rollup.spend, largest)
        sentences.append(f"{_SOURCE_NAMES[largest]} spent the most before them: {share:.0f}% of list-price spend.")
    burst, overall = rollup.burst_pct, rollup.all_burst_pct
    if burst and overall is not None:
        sentences.append(
            f"{burst:.0f}% of that spend ran while {BURST_AGENTS} or more agents worked at once, "
            f"against {overall:.0f}% across all your work."
        )
    return " ".join(sentences)


def _rollup_table(rollup: StopsRollup, since: datetime | None) -> Table:
    spend = rollup.spend
    largest = spend.largest_source
    return Table(
        name="limits_stops_rollup",
        title="Your limit stops, rolled up",
        columns=[
            Column(key="metric", label="Metric", kind="str"),
            Column(key="stops", label="Stops counted", kind="int"),
            Column(key="spend_usd", label="List-price spend before them", kind="money"),
            Column(key="main_share_pct", label="Main session", kind="pct"),
            Column(key="direct_share_pct", label="Direct agents", kind="pct"),
            Column(key="workflow_share_pct", label="Workflow agents", kind="pct"),
            Column(key="largest_centre", label="Biggest spender", kind="str"),
            Column(key="largest_share_pct", label="Biggest spender's share", kind="pct"),
            Column(key="burst_share_pct", label="Spend with 3+ agents at once", kind="pct"),
            Column(key="all_burst_share_pct", label="Same, across all your work", kind="pct"),
            Column(key="since", label="Stops counted since", kind="str"),
        ],
        rows=[
            [
                "all",
                rollup.stops,
                round(spend.total, 6),
                _share_pct(spend, "main"),
                _share_pct(spend, "direct"),
                _share_pct(spend, "workflow"),
                largest,
                _share_pct(spend, largest) if largest is not None else None,
                rollup.burst_pct,
                rollup.all_burst_pct,
                since.date().isoformat() if since is not None else None,
            ]
        ],
        notes=[
            _rollup_sentence(rollup),
            "Each share is a share of list-price spend; the limit may weigh models differently.",
            "A stop counts when it stopped work and named a reset. The spend covers the 5-hour windows "
            "before your 5-hour stops. A weekly window holds your whole week, so it is used only when "
            "no 5-hour stop counts.",
        ],
    )


def _spender_text(label: str, family: str, cost: float, total: float) -> str:
    """``Explore, Haiku (12%)``: who spent it, on which model family, and
    the share of the window's spend."""
    who = {"top-level": "Main session", "unknown": "Subagent (type not recorded)"}.get(label, label)
    return f"{who}, {family.capitalize()} ({_pct(cost, total):.0f}%)"


def _local_text(when: datetime) -> str:
    """``when`` in this machine's zone as ``2026-10-02 15:00``."""
    return when.astimezone().strftime("%Y-%m-%d %H:%M")


def _stops_table(stats: LimitStats) -> Table:
    episodes = sorted(stats.episodes(), key=lambda e: (e.first_at is not None, e.first_at or _EPOCH), reverse=True)
    shown = episodes[:STOPS_TABLE_ROWS]
    rows = []
    for episode in shown:
        spend = stats.stop_spend(episode)
        minutes = None
        if episode.reset is not None and episode.first_at is not None:
            minutes = max(0, round((episode.reset - episode.first_at).total_seconds() / 60))
        spenders = spend.top_spenders() if spend is not None else []
        texts = [_spender_text(label, family, cost, spend.total) for label, family, cost in spenders]
        rows.append(
            [
                _local_text(episode.reset) if episode.reset is not None else "unknown",
                episode.kind,
                minutes,
                round(spend.total, 6) if spend is not None else None,
                _share_pct(spend, "main") if spend is not None else None,
                _share_pct(spend, "direct") if spend is not None else None,
                _share_pct(spend, "workflow") if spend is not None else None,
                spend.share(spend.burst) if spend is not None else None,
                texts[0] if texts else None,
                texts[1] if len(texts) > 1 else None,
                episode.stopped_work,
                episode.cut_off,
            ]
        )
    notes = [
        "Each share is a share of list-price spend; the limit may weigh models differently.",
        "Spend runs from the start of the limit's window to the stop's first limit message, in quarter-hours. "
        "The window opens 5 hours (weekly, 7 days) before the earliest reset the stop's messages named.",
    ]
    if len(episodes) > len(shown):
        notes.append(f"Showing the {len(shown)} most recent of {len(episodes)} stops.")
    return Table(
        name="limits_stops",
        title="Your recent limit stops",
        columns=[
            Column(key="reset", label="Reset time", kind="str"),
            Column(key="kind", label="Limit", kind="str"),
            Column(key="minutes_before_reset", label="Minutes before the reset", kind="int"),
            Column(key="spend_usd", label="List-price spend in the window", kind="money"),
            Column(key="main_share_pct", label="Main session", kind="pct"),
            Column(key="direct_share_pct", label="Direct agents", kind="pct"),
            Column(key="workflow_share_pct", label="Workflow agents", kind="pct"),
            Column(key="burst_share_pct", label="Spend with 3+ agents at once", kind="pct"),
            Column(key="top_spender", label="Biggest spender", kind="str"),
            Column(key="second_spender", label="Second biggest", kind="str"),
            Column(key="stopped_work", label="Stopped work", kind="str"),
            Column(key="agents_cut_off", label="Agents cut off", kind="int"),
        ],
        rows=rows,
        value_labels=dict(_KIND_LABELS),
        notes=notes,
    )


def _hit_kind_table(rows: list[LimitTypeStats]) -> Table:
    total = sum(r.hits for r in rows)
    counts = {
        "session_limit": sum(r.session_limit_hits for r in rows),
        "weekly_limit": sum(r.weekly_limit_hits for r in rows),
    }
    table_rows = [[kind, counts[kind], _pct(counts[kind], total)] for kind in HIT_KINDS]
    return Table(
        name="limits_hits_by_kind",
        title="Limit messages by kind",
        columns=[
            Column(key="kind", label="Kind", kind="str"),
            Column(key="hits", label="Messages", kind="int"),
            Column(key="share_pct", label="Share", kind="pct"),
        ],
        rows=table_rows,
        notes=[
            "5-hour session limit: \"You've hit your session limit\" (the "
            "rolling 5-hour window). Weekly limit: \"You've hit your "
            "weekly limit\".",
        ],
    )


def _terminated_table(rows: list[LimitTypeStats]) -> Table:
    total = sum(r.terminated for r in rows)
    counts = {
        "rate_limit": sum(r.terminated_rate_limit for r in rows),
        "other": sum(r.terminated_other for r in rows),
    }
    table_rows = [[kind, counts[kind], _pct(counts[kind], total)] for kind in TERMINATED_KINDS]
    return Table(
        name="limits_agent_terminated",
        title="Agents terminated early",
        columns=[
            Column(key="kind", label="Reason", kind="str"),
            Column(key="terminated", label="Terminated", kind="int"),
            Column(key="share_pct", label="Share", kind="pct"),
        ],
        rows=table_rows,
        notes=[
            "A subagent Claude Code stopped mid-task, from its task "
            "notification's own \"Agent terminated early due to ...\" "
            "text. Usage limit: the notification named a rate limit as "
            "the error type. Other: any other reason, or none stated.",
        ],
    )


def _pause_table(rows: list[LimitTypeStats]) -> Table:
    # A corpus-wide median can't be exactly derived from already-aggregated
    # per-agent-type medians, so this table reports count/total/mean only;
    # per-agent-type median/max live on the by-agent-type table instead.
    total_count = sum(r.pause_count for r in rows)
    total_s = sum(r.pause_total_s for r in rows)
    mean_s = (total_s / total_count) if total_count else None
    return Table(
        name="limits_pauses",
        title="Usage-cap pause durations",
        columns=[
            Column(key="metric", label="Metric", kind="str"),
            Column(key="pause_count", label="Pauses", kind="int"),
            Column(key="total_s", label="Total pause time", kind="secs"),
            Column(key="mean_s", label="Mean pause", kind="secs"),
        ],
        rows=[["all", total_count, round(total_s, 3), round(mean_s, 3) if mean_s is not None else None]],
        notes=[
            "One pause per reply whose wait since the previous reply "
            "spanned a usage-limit pause. Its length is that wait, or the "
            "time until the limit reset when you came back later. The "
            "typical and longest pause per agent type are in the "
            "by-agent-type table. An overall typical pause can't be "
            "worked out from the per-type ones.",
        ],
    )


def _reset_hour_table(counts: dict[int, int]) -> Table:
    total = sum(counts.values())
    rows = [[f"{hour:02d}", counts.get(hour, 0), _pct(counts.get(hour, 0), total)] for hour in range(24)]
    return Table(
        name="limits_reset_hour_histogram",
        title="Limit resets by local hour of day",
        columns=[
            Column(key="local_hour", label="Local hour", kind="str"),
            Column(key="resets", label="Stops", kind="int"),
            Column(key="share_pct", label="Share", kind="pct"),
        ],
        rows=rows,
        notes=[
            "Each limit stop counts once, however many limit messages it "
            "wrote. The local hour comes from the limit message's own "
            "\"resets H:MMam/pm\" text when present, else from its reset "
            "time in this machine's time zone. A stop with neither is left "
            "out of this chart but still counted in the summary.",
        ],
    )


def _wake_table(counts: dict[str, int]) -> Table:
    total = sum(counts.values())
    return Table(
        name="limits_wake_gaps",
        title="What woke the session between limit messages",
        columns=[
            Column(key="wake", label="What woke it", kind="str"),
            Column(key="gaps", label="Gaps", kind="int"),
            Column(key="share_pct", label="Share", kind="pct"),
        ],
        rows=[[name, counts.get(name, 0), _pct(counts.get(name, 0), total)] for name in WAKE_CLASSES],
        notes=[
            "One gap per pair of consecutive limit messages in a main "
            "session, filed by what its lines held: something you typed, "
            "the desktop app's resume, a scheduled task, a background "
            "agent's notice, or only user lines Claude Code wrote itself "
            "(the isMeta flag). Only the kind of line is read, never its text.",
        ],
    )


def _by_agent_type_table(rows: list[LimitTypeStats]) -> Table:
    table_rows = [
        [
            r.key,
            r.transcripts,
            r.hits,
            r.resumes,
            r.terminated,
            r.pause_count,
            round(r.pause_total_s, 3),
            r.pause_median_s,
            r.pause_max_s,
            r.limit_turn_cc_tokens,
            round(r.limit_turn_write_cost_usd, 6),
        ]
        for r in rows
    ]
    # Deterministic order: descending by hits, tie-broken by the row key.
    table_rows.sort(key=lambda row: (row[2], row[0]), reverse=True)
    return Table(
        name="limits_by_agent_type",
        title="Who got the limit message",
        columns=[
            Column(key="agent_type", label="Agent type", kind="str"),
            Column(key="transcripts", label="Transcripts", kind="int"),
            Column(key="limit_hits", label="Limit messages received", kind="int"),
            Column(key="limit_resumes", label="Limit resumes", kind="int"),
            Column(key="agents_terminated", label="Agents terminated", kind="int"),
            Column(key="pause_count", label="Pauses", kind="int"),
            Column(key="pause_total_s", label="Total pause time", kind="secs"),
            Column(key="pause_median_s", label="Median pause", kind="secs"),
            Column(key="pause_max_s", label="Max pause", kind="secs"),
            Column(key="limit_turn_cc_tokens", label="Cache-creation tokens (post-pause turns)", kind="tokens"),
            Column(key="limit_turn_write_cost_usd", label="Cache-write cost (post-pause turns)", kind="money"),
        ],
        rows=table_rows,
        notes=[
            "Each subagent type as Claude Code recorded it, plus one row "
            "for the main session. The main session relays a message for "
            "each agent a limit cut off, so it receives more than the "
            "agents it ran.",
        ],
    )


# -- usage-log.csv cross-check -------------------------------------------------


def read_usage_log_rows(csv_path: str | Path) -> list[dict]:
    """Load ``csv_path`` (``tools/log_usage.py``'s own CSV shape:
    ``logged_at, session_id, window, used_percentage, resets_at,
    source``) for :func:`csv_cross_check`. Returns ``[]`` when the file
    doesn't exist -- the log is optional, never required.

    A thin, independent re-implementation of
    ``tools.log_usage.load_usage_log`` (not a call to it): that module
    is one of two other concurrent writers' surface for this batch, and
    this module must not depend on its internals changing shape under
    it. The two are expected to agree on ``CSV_FIELDS``' meaning by
    convention, not by sharing code.
    """
    csv_path = Path(csv_path)
    if not csv_path.exists():
        return []
    rows: list[dict] = []
    with open(csv_path, "r", encoding="utf-8", newline="") as fh:
        reader = csv_mod.DictReader(fh, restkey="_extra")
        for raw_row in reader:
            row = dict(raw_row)
            used = row.get("used_percentage")
            if used not in (None, ""):
                try:
                    row["used_percentage"] = float(used)
                except ValueError:
                    pass
            rows.append(row)
    return rows


def has_rate_limit_rows(rows: Sequence[dict]) -> bool:
    """Whether ``rows`` (see :func:`read_usage_log_rows`) hold any
    ``five_hour``/``seven_day`` row at all. A log without one (the desktop
    app runs no status line, so it logs only context-window rows) has
    nothing to compare limit stops with, so :func:`csv_cross_check` is left
    out of the report rather than showing zero against a real count."""
    return any(row.get("window") in CSV_WINDOWS for row in rows)


def csv_cross_check(rows: Sequence[dict], stats: LimitStats, th: LimitThresholds | None = None) -> Table:
    """Cross-check ``usage-log.csv`` rows (see :func:`read_usage_log_rows`)
    against the transcript-derived limit stops in ``stats``: for each of
    ``CSV_WINDOWS`` (``five_hour``/``seven_day``), how many CSV rows
    reported ``used_percentage >= th.csv_exhaustion_pct`` versus how many
    stops ``stats`` recorded for the matching kind (``five_hour`` ->
    ``session_limit``, ``seven_day`` -> ``weekly_limit``). Stops, not
    lines: one stop writes a storm of limit lines.

    A sanity check, not a second detector: the two sources sample the
    account's usage independently (the statusline polls
    ``rate_limits`` continuously; a transcript only records a hit at the
    moment a request was actually blocked), so exact agreement is not
    expected -- a large, persistent gap either way is what is worth a
    human's attention, not the raw numbers alone. The report only builds
    it when :func:`has_rate_limit_rows`.
    """
    th = th or _DEFAULT_THRESHOLDS
    stops = stats.stop_counts()
    transcript_stops = {"five_hour": stops["session_limit"], "seven_day": stops["weekly_limit"]}
    csv_counts: dict[str, int] = {window: 0 for window in CSV_WINDOWS}
    for row in rows:
        window = row.get("window")
        if window not in CSV_WINDOWS:
            continue
        used = row.get("used_percentage")
        if not isinstance(used, (int, float)):
            continue
        if used >= th.csv_exhaustion_pct:
            csv_counts[window] += 1

    table_rows = []
    for window in CSV_WINDOWS:
        csv_n = csv_counts[window]
        transcript_n = transcript_stops[window]
        table_rows.append([window, csv_n, transcript_n, transcript_n - csv_n])

    return Table(
        name="limits_csv_cross_check",
        title="Usage-log.csv cross-check",
        columns=[
            Column(key="window", label="Window", kind="str"),
            Column(key="csv_exhaustion_rows", label="usage-log.csv exhaustion rows", kind="int"),
            Column(key="transcript_stops", label="Transcript-derived stops", kind="int"),
            Column(key="delta", label="Transcript minus CSV", kind="int"),
        ],
        rows=table_rows,
        notes=[
            f"A usage log row counts as at the limit at "
            f"{th.csv_exhaustion_pct:.1f}% used or more. 5-hour rows are "
            "compared with 5-hour limit stops, weekly rows with weekly "
            "limit stops. A stop is counted once however many limit "
            "messages it wrote. The two sides are counted separately, so "
            "they needn't match exactly.",
        ],
    )


def signals_cross_check(session_signals, stats: LimitStats) -> Table:
    """Cross-check the free ``waits``/``turn_signals`` capture signals
    (SIG-2, SIG-3) against the transcript-derived limit-hit count in
    ``stats``: how many sessions logged a ``quota`` wait (a claude.ai
    usage-limit auto-resume notification) or a ``rate_limit``/
    ``overloaded`` ``StopFailure``, against how many transcript-derived
    ``session_limit``/``weekly_limit`` hits ``stats`` recorded in total.

    ``session_signals`` is ``signals.by_session(...)``'s own return
    shape: ``{session_id: signals.SessionSignals}``. Unlike
    :func:`csv_cross_check`, neither signal names its window
    (``five_hour``/``seven_day``), so this compares one combined figure
    per side, not a per-window breakdown -- a sanity check, not a second
    detector, same posture as :func:`csv_cross_check` (see its own
    docstring for why exact agreement isn't expected between two
    independently-sampled sources).
    """
    type_stats = stats.by_key()
    transcript_hits = sum(r.session_limit_hits + r.weekly_limit_hits for r in type_stats)
    quota_waits = sum(seen.waits.get("quota", 0) for seen in session_signals.values())
    limit_failures = sum(
        seen.failures.get("rate_limit", 0) + seen.failures.get("overloaded", 0) for seen in session_signals.values()
    )
    rows = [
        ["quota wait signals (Notification)", quota_waits, transcript_hits, transcript_hits - quota_waits],
        ["rate_limit/overloaded turn failures (StopFailure)", limit_failures, transcript_hits, transcript_hits - limit_failures],
    ]
    return Table(
        name="limits_signals_cross_check",
        title="Free-signal cross-check",
        columns=[
            Column(key="signal", label="Signal", kind="str"),
            Column(key="signal_count", label="Logged", kind="int"),
            Column(key="transcript_hits", label="Limit messages in transcripts (both windows)", kind="int"),
            Column(key="delta", label="Transcript minus signal", kind="int"),
        ],
        rows=rows,
        notes=[
            "Neither signal names its window, so both rows compare with the 5-hour and weekly limit messages "
            "together. The two sides are counted separately, so they needn't match exactly.",
        ],
    )


__all__ = [
    "ASSUMPTIONS",
    "HIT_KINDS",
    "TERMINATED_KINDS",
    "CSV_WINDOWS",
    "EPISODE_MERGE_S",
    "EPISODE_JOIN_S",
    "BUCKET_S",
    "BURST_AGENTS",
    "BURST_GAP_POINTS",
    "SPEND_SOURCES",
    "TOP_SPENDERS",
    "STOPS_TABLE_ROWS",
    "CURRENT_SINCE",
    "KEPT_WORKING_AFTER_S",
    "BUSY_HOUR_MIN_EPISODES",
    "BUSY_HOUR_MIN_SHARE_PCT",
    "WAKE_CLASSES",
    "LimitThresholds",
    "limit_pause_intervals",
    "pause_overlap_s",
    "limit_markers",
    "wake_class",
    "busy_reset_hour",
    "burst_stands_out",
    "stop_window",
    "Episode",
    "StopSpend",
    "StopsRollup",
    "LimitTypeStats",
    "LimitStats",
    "build_section",
    "read_usage_log_rows",
    "has_rate_limit_rows",
    "csv_cross_check",
    "signals_cross_check",
]

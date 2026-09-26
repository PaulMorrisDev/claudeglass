"""Compaction-event analytics and post-compaction rediscovery cost (WP6).

Reads the ``COMPACT_BOUNDARY`` events a transcript already carries (see
``events.py``'s A2 row 1 and ``parse.py``'s ``compactMetadata`` mapping)
and answers two questions a report needs:

- :func:`compaction_records_for_transcript` / :class:`CompactionStats` —
  per-compaction facts (trigger, pre/post/dropped tokens, duration,
  compression ratio) plus what happened to the very next priced turn: how
  much it wrote back into the cache and what that write cost, and whether
  that turn itself looks like a RE-CACHE (a full re-send of the prefix
  rather than a cheap continuation).
- :func:`rediscovery` — whether the turns right after a compaction lean on
  Read/Grep/Glob more than the turns right before it, i.e. whether the
  agent is re-learning things the compaction just dropped.

RE-CACHE detector (deviation, reported rather than made silently — see
``model.py``'s module docstring for this project's convention on that):
the plan's WP6 brief asks for "whether that turn was a re-cache per WP3's
rule re-implemented minimally here as ``ctx > 20k and cache_read <
0.2*ctx``". WP3 (``recache.py``) has since landed, so :func:`is_recache_turn`
below (fix item 6) took its ctx_floor/cr_ratio pair from
``recache.RecacheThresholds`` instead of an independent hardcoded copy —
one fewer place the two numbers could drift apart — but it still only
implemented that two-number minimal rule and nothing more: no signature
classification (full-expiry vs prefix-invalidated), no override beyond
the two shared numbers. **WP10b addition:** :func:`compaction_records_for_transcript`
now calls :func:`recache.apply` on ``tr`` before correlating turns and
reads the resulting ``Turn.is_recache`` for ``next_turn_is_recache``,
instead of calling :func:`is_recache_turn` on the raw next turn — the
shared detector's full signature classification (``turn_index > 1``,
non-synthetic, plus the same ctx/cache_read numbers) now decides
"re-cache" here exactly as it does in the RE-CACHE section itself, so
this module's notion of "re-cache" can no longer drift from the
corpus-wide one. :func:`is_recache_turn` itself is left in place (its own
tests exercise it directly, and nothing else in the codebase besides this
module and its tests calls it) but is no longer used by this function.

Correlating a compaction to "the following turn": ``Turn`` doesn't carry
a back-reference to the ``Event`` objects that preceded it (only their
``kind``s, via ``preceding_event_kinds`` — see ``model.py``), so the
compaction's own token/trigger/duration fields (only present on the
``Event``) have to be matched to a ``Turn`` by timestamp instead. Both
``TranscriptResult.events`` and ``TranscriptResult.turns`` are already in
file order (chronological), so this is a single forward merge: for each
``COMPACT_BOUNDARY`` event, walk the priced-turn list forward until a
turn's timestamp is at or after the event's, and skip turns already
consumed by an earlier compaction. An event with no matching later turn
(the transcript ends right after it), an event with an unparsable
timestamp of its own, or turns with unparsable timestamps in between
(skipped rather than matched — see
``_correlate_compactions_to_turn_index``'s robustness note) still
produce a ``CompactionRecord`` — just with ``next_turn_*`` fields left
``None``, rather than being dropped.

``CompactionStats`` aggregates across as many transcripts as
:meth:`CompactionStats.add_transcript` is called with (the plan calls
this shape "sessions with >=1 compaction" etc. — genuinely corpus-wide
numbers). The plan's own phrase "``CompactionStats`` fed by
``(TranscriptResult, rates)``" describes that one-call shape, not a
single-transcript-only constructor; :func:`compaction_records_for_transcript`
is the standalone function that produces exactly the "per transcript list
of compaction records" the brief also asks for, and
``CompactionStats.add_transcript`` is built on top of it.

Privacy: nothing here retains message text, tool_result content, or a
full path — only counts, token totals (already present on ``Turn``/
``Event``), and the small set of tool names in ``_REDISCOVERY_TOOLS``.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

from . import recache
from .model import Column, Event, EventKind, Section, Table, TranscriptResult, Turn, scheduled_main_session
from .pricing import ModelRates, ResolvedRates, price_turn
from .recache import RecacheThresholds

#: Tool names counted as "rediscovery" work in :func:`rediscovery`.
_REDISCOVERY_TOOLS = frozenset({"Read", "Grep", "Glob"})

#: Turns considered on each side of a compaction in :func:`rediscovery`.
_REDISCOVERY_WINDOW = 10

#: Rows shown in the per-session table :func:`build_section` emits.
_PER_SESSION_TABLE_LIMIT = 20

#: A ``CompactionRecord`` whose ``join_delta_s`` exceeds this many seconds
#: (15 minutes) is excluded from every aggregate built from its
#: ``next_turn_*`` fields (fix item 9) — the correlation only guarantees
#: "the next priced turn chronologically", and a gap this large means
#: that turn's cache-write cost most likely belongs to a resumed session,
#: not to recovering from the compaction. ``None`` (unparsable timestamp)
#: is treated as tight — there's no evidence the join is loose, only that
#: it can't be measured.
_MAX_JOIN_DELTA_S = 900.0


def _join_is_tight(record: CompactionRecord) -> bool:
    return record.join_delta_s is None or record.join_delta_s <= _MAX_JOIN_DELTA_S


def effective_autocompact_threshold(records: Iterable[CompactionRecord]) -> float | None:
    """The observed effective autocompact threshold: the median
    ``pre_tokens`` across every ``records`` entry whose ``trigger`` is
    exactly ``"auto"`` (a manual/unknown-trigger compaction says nothing
    about where the harness itself decided to fire, so it's excluded).
    ``None`` when no auto-triggered record carries a ``pre_tokens`` value
    at all -- deliberately not ``0``, which would misleadingly read as
    "compacts almost immediately".

    S1-context-budget addition: this is the "new ``compaction.py``
    helper" the context-budget work package asks for, kept as a
    standalone function (rather than a ``CompactionStats`` property) so a
    caller building a *per-project* breakdown (``context_budget.py``, one
    ``CompactionStats``-shaped list of records per project rather than
    one corpus-wide accumulator) can call it directly against its own
    project-scoped record list without needing a whole second
    ``CompactionStats`` instance per project.
    """
    values = [r.pre_tokens for r in records if r.trigger == "auto" and r.pre_tokens is not None]
    return statistics.median(values) if values else None


def is_recache_turn(turn: Turn, thresholds: RecacheThresholds | None = None) -> bool:
    """Minimal RE-CACHE test: a large context whose cache-read share is
    small, i.e. the turn looks like it re-sent most of its prefix as a
    fresh write rather than reading it back from cache.

    See the module docstring's deviation note — this is deliberately not
    WP3's full detector (no signature classification), just the two
    numbers ("ctx > ctx_floor and cache_read < cr_ratio*ctx") the WP6
    brief specifies. Fix item 6: those two numbers now come from
    ``thresholds`` (a ``recache.RecacheThresholds``, defaulting to its
    own defaults — ctx_floor=20_000, cr_ratio=0.2 — when omitted)
    instead of a second, independently hardcoded copy of the same pair,
    so this module and ``recache.py`` can never drift apart on them.
    """
    th = thresholds or RecacheThresholds()
    if turn.ctx <= th.ctx_floor:
        return False
    return turn.cache_read_tokens < th.cr_ratio * turn.ctx


def _parse_ts(ts_raw: str | None) -> datetime | None:
    if not ts_raw:
        return None
    try:
        return datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def new_tokens(turn: Turn) -> int:
    """``input_tokens + cache_creation_tokens`` for one turn: the tokens
    that entered the context fresh this turn, as opposed to
    ``cache_read_tokens`` replayed from an existing cache.

    Fix item 9: defined once here (rather than re-derived at each call
    site) as an alternative denominator to ``cache_creation_tokens``
    alone for "what share of this turn's tokens..." questions —
    ``cache_creation`` alone undercounts a turn whose prefix was never
    cacheable in the first place (no cache_control breakpoint hit at
    all), where every token still arrived as plain ``input_tokens``.
    """
    return turn.input_tokens + turn.cache_creation_tokens


def _priced_turns(tr: TranscriptResult) -> list[Turn]:
    """Turns that actually went through the pricing/gap machinery — see
    ``parse.py``'s ``turn_index`` convention (0 for synthetic/no-usage
    turns, 1-based otherwise). Compaction records and rediscovery windows
    are both about priced work, so unpriced turns are excluded from both.
    So is an estimated compaction call (``Turn.estimated``): it is the
    compaction itself, not a reply before or after it.
    """
    return [t for t in tr.turns if t.turn_index > 0 and not t.estimated]


def _compaction_events(tr: TranscriptResult) -> list[Event]:
    return [e for e in tr.events if e.kind == EventKind.COMPACT_BOUNDARY]


def _correlate_compactions_to_turn_index(
    tr: TranscriptResult, priced_turns: list[Turn]
) -> list[tuple[Event, int | None]]:
    """Pair each ``COMPACT_BOUNDARY`` event with the index (into
    ``priced_turns``) of the first priced turn at or after it, per the
    module docstring's forward-merge. ``None`` when no later turn exists.

    Two or more compactions with no priced turn between them (rare, but
    not impossible — e.g. a compaction immediately followed by another)
    correctly resolve to the *same* index: the pointer only advances past
    a turn once its timestamp is confirmed to be before the event being
    matched, never merely because it was returned for an earlier event.

    Robustness (fix 4): an event with no parsable ``ts`` of its own can't
    be correlated to anything and resolves to ``None`` outright, rather
    than grabbing whatever turn the shared pointer currently sits on (the
    previous behaviour — since the "no timestamp" break condition fired
    immediately, it silently attributed an arbitrary, possibly much
    earlier, turn as "the turn right after this compaction"). Likewise a
    turn with no parsable ``ts`` is skipped (the pointer advances past it)
    instead of being treated as a match, since its position relative to
    the event can't be confirmed either way.
    """
    results: list[tuple[Event, int | None]] = []
    turn_idx = 0
    n = len(priced_turns)
    for event in _compaction_events(tr):
        event_dt = _parse_ts(event.ts)
        if event_dt is None:
            results.append((event, None))
            continue
        while turn_idx < n:
            turn_dt = _parse_ts(priced_turns[turn_idx].ts)
            if turn_dt is None:
                turn_idx += 1
                continue
            if turn_dt >= event_dt:
                break
            turn_idx += 1
        results.append((event, turn_idx if turn_idx < n else None))
    return results


@dataclass(slots=True)
class CompactionRecord:
    """One ``COMPACT_BOUNDARY`` event, plus what its transcript's very
    next priced turn did with the cache immediately afterwards.
    """

    session_id: str = ""
    ts: str | None = None
    trigger: str | None = None
    pre_tokens: int | None = None
    post_tokens: int | None = None
    #: Tokens dropped BY THIS compaction alone — a delta computed from the
    #: transcript's running ``compactMetadata.cumulativeDroppedTokens``
    #: counter (see :func:`compaction_records_for_transcript`'s docstring),
    #: never that raw cumulative-since-session-start value itself.
    dropped_tokens: int | None = None
    duration_ms: int | None = None
    #: post_tokens / pre_tokens; ``None`` when ``pre_tokens`` is missing
    #: or zero (nothing to divide by).
    ratio: float | None = None
    next_turn_cache_creation: int | None = None
    next_turn_write_cost: float | None = None
    #: ``None`` only when there is no next turn to test at all.
    next_turn_is_recache: bool | None = None
    #: Seconds between this compaction event's own ``ts`` and the matched
    #: next turn's ``ts`` (fix item 9). ``_correlate_compactions_to_turn_index``
    #: only guarantees "the next priced turn chronologically" — with no
    #: turn immediately after (the session paused, or ended), that can be
    #: a turn far later, whose cache-write cost has nothing to do with
    #: this compaction. ``None`` when there is no next turn, or either
    #: timestamp is unparsable. See ``CompactionStats``'s join-tightness
    #: gate on the aggregates that read ``next_turn_*``.
    join_delta_s: float | None = None


def compaction_records_for_transcript(
    tr: TranscriptResult,
    rates: ResolvedRates | ModelRates | None,
    thresholds: RecacheThresholds | None = None,
) -> list[CompactionRecord]:
    """The "per transcript list of compaction records" the WP6 brief
    asks for: one :class:`CompactionRecord` per ``COMPACT_BOUNDARY``
    event in ``tr.events``, correlated to the priced turn immediately
    following it (see the module docstring).

    ``thresholds`` (fix item 6) is forwarded to :func:`recache.apply` (via
    :func:`recache.detect`) for the ``next_turn_is_recache`` field — see
    the module docstring's WP10b addition note.

    ``rates`` prices that following turn's observed cache-write split
    (``price_turn``'s default path — ``turn.cc_5m``/``turn.cc_1h``, not a
    simulated one) via :func:`~claudeglass.pricing.price_turn`. An
    unresolved/``None`` rate (unknown model, or no pricing available at
    all) leaves ``next_turn_write_cost`` at ``0.0`` — ``price_turn``'s own
    contract for an unpriced turn — rather than ``None``, so a caller
    summing this field never has to special-case it.

    ``CompactionRecord.dropped_tokens`` is a **per-compaction delta**, not
    ``Event.dropped_tokens`` copied straight through. Verified against a
    real 30-day corpus of a project's transcripts under
    ``~/.claude/projects/<project-slug>``:
    ``compactMetadata.cumulativeDroppedTokens`` is a running total *for the
    whole session*, monotonically non-decreasing across that session's
    compactions (e.g. observed values 555197 then 1017658 in one
    transcript with two compactions — the second is the first plus that
    compaction's own ~462k drop, not a fresh 1017658-token drop). Summing
    the raw field across a multi-compaction session therefore massively
    over-counts (this was the fix-4 bug: it inflated
    ``dropped_share_of_cache_creation`` to ~99% on the same corpus where
    the delta-based total lands far lower). This function subtracts each
    session's previous cumulative value to recover the tokens dropped by
    that one compaction; the first compaction in a transcript uses a
    baseline of 0. A cumulative value that goes backwards (unexpected, but
    seen only in synthetic/malformed data, never in the real corpus above)
    is treated as a counter reset: the delta falls back to the raw value
    and the running baseline restarts from it, rather than going negative.
    """
    th = thresholds or RecacheThresholds()
    applied = recache.apply(tr, th)
    priced_turns = _priced_turns(applied)
    session_id = tr.meta.session_id
    records: list[CompactionRecord] = []
    prev_cumulative_dropped = 0
    for event, turn_idx in _correlate_compactions_to_turn_index(applied, priced_turns):
        ratio: float | None = None
        if event.pre_tokens:
            ratio = event.post_tokens / event.pre_tokens if event.post_tokens is not None else None

        dropped_delta: int | None = None
        if event.dropped_tokens is not None:
            if event.dropped_tokens >= prev_cumulative_dropped:
                dropped_delta = event.dropped_tokens - prev_cumulative_dropped
            else:
                dropped_delta = event.dropped_tokens
            prev_cumulative_dropped = event.dropped_tokens

        next_cache_creation: int | None = None
        next_write_cost: float | None = None
        next_is_recache: bool | None = None
        join_delta_s: float | None = None
        if turn_idx is not None:
            next_turn = priced_turns[turn_idx]
            next_cache_creation = next_turn.cache_creation_tokens
            next_write_cost = price_turn(next_turn, rates).cache_write_cost
            next_is_recache = next_turn.is_recache
            event_dt = _parse_ts(event.ts)
            turn_dt = _parse_ts(next_turn.ts)
            if event_dt is not None and turn_dt is not None:
                join_delta_s = (turn_dt - event_dt).total_seconds()

        records.append(
            CompactionRecord(
                session_id=session_id,
                ts=event.ts,
                trigger=event.trigger,
                pre_tokens=event.pre_tokens,
                post_tokens=event.post_tokens,
                dropped_tokens=dropped_delta,
                duration_ms=event.duration_ms,
                ratio=ratio,
                next_turn_cache_creation=next_cache_creation,
                next_turn_write_cost=next_write_cost,
                next_turn_is_recache=next_is_recache,
                join_delta_s=join_delta_s,
            )
        )
    return records


@dataclass(slots=True)
class CompactionStats:
    """Corpus-wide compaction aggregates, built by folding in one
    transcript (and its resolved pricing rate) at a time via
    :meth:`add_transcript`. See the module docstring for why this is a
    fold rather than a single-transcript-only constructor.
    """

    records: list[CompactionRecord] = field(default_factory=list)
    _sessions_seen: set[str] = field(default_factory=set, repr=False)
    _sessions_with_compaction: set[str] = field(default_factory=set, repr=False)
    _compactions_per_session: dict[str, int] = field(default_factory=dict, repr=False)
    #: Sum of ``cache_creation_tokens`` across every priced turn in every
    #: transcript folded in so far (not just turns following a
    #: compaction) — the denominator for "dropped tokens' share of total
    #: cache_creation".
    total_cache_creation: int = 0
    #: Sum of :func:`new_tokens` (``input_tokens + cache_creation_tokens``)
    #: across the same priced turns (fix item 9) — the alternative
    #: denominator that doesn't undercount a turn whose prefix was never
    #: cacheable at all.
    total_new_tokens: int = 0
    #: Main sessions a scheduled task started with no message of yours
    #: (``model.scheduled_main_session``), left out of every figure.
    scheduled_sessions: int = 0
    #: The estimated request that wrote each summary (``Turn.estimated``,
    #: see ``parse.py``): how many, and what they cost. They count in
    #: spend; these figures only show that part of it.
    summary_requests: int = 0
    summary_request_cost: float = 0.0

    @classmethod
    def build(
        cls, items: Iterable[tuple[TranscriptResult, ResolvedRates | ModelRates | None]]
    ) -> "CompactionStats":
        """Convenience constructor: fold in every ``(TranscriptResult,
        rates)`` pair from ``items`` in order.
        """
        stats = cls()
        for tr, rates in items:
            stats.add_transcript(tr, rates)
        return stats

    def add_transcript(
        self,
        tr: TranscriptResult,
        rates: ResolvedRates | ModelRates | None,
        thresholds: RecacheThresholds | None = None,
    ) -> list[CompactionRecord]:
        """Fold one transcript's compactions (and overall cache_creation
        total) into the running aggregates. Returns this transcript's own
        record list (the "per transcript list" the brief names), which is
        also appended to ``self.records``. ``thresholds`` (fix item 6) is
        forwarded to :func:`compaction_records_for_transcript`.
        """
        if scheduled_main_session(tr):
            # A scheduled check never compacts; counting it as a session
            # would dilute compactions per session.
            self.scheduled_sessions += 1
            return []
        session_id = tr.meta.session_id
        self._sessions_seen.add(session_id)
        priced = _priced_turns(tr)
        self.total_cache_creation += sum(t.cache_creation_tokens for t in priced)
        self.total_new_tokens += sum(new_tokens(t) for t in priced)
        for turn in tr.turns:
            if turn.estimated == "compaction":
                self.summary_requests += 1
                self.summary_request_cost += price_turn(turn, rates).total

        records = compaction_records_for_transcript(tr, rates, thresholds)
        if records:
            self._sessions_with_compaction.add(session_id)
            self._compactions_per_session[session_id] = (
                self._compactions_per_session.get(session_id, 0) + len(records)
            )
        self.records.extend(records)
        return records

    # -- aggregates -------------------------------------------------------

    @property
    def total_sessions(self) -> int:
        return len(self._sessions_seen)

    @property
    def sessions_with_compaction(self) -> int:
        return len(self._sessions_with_compaction)

    @property
    def compactions_per_session_mean(self) -> float | None:
        """Mean compactions per session across every session folded in so
        far (``total_sessions``) -- NOT just sessions that actually
        compacted (R8 fix). Dividing by ``sessions_with_compaction`` alone
        silently drops every zero-compaction session from the
        denominator, inflating the reported mean (e.g. 1 compacting
        session out of 10 total, with 2 compactions, used to report a
        mean of 2.0 instead of 0.2). ``None`` only when no session has
        been seen at all. See
        :attr:`compactions_per_compacting_session_mean` for the previous
        (compacting-sessions-only) denominator, kept as a separate stat.
        """
        if self.total_sessions == 0:
            return None
        return sum(self._compactions_per_session.values()) / self.total_sessions

    @property
    def compactions_per_compacting_session_mean(self) -> float | None:
        """Mean compactions per session, counting only sessions that had
        at least one compaction -- the denominator
        ``compactions_per_session_mean`` used before the R8 fix. Kept
        separately since "how bad is it when a session does compact" is a
        different question from "how often does compaction happen across
        the whole corpus" (the latter is what ``compactions_per_session_mean``
        now answers).
        """
        counts = list(self._compactions_per_session.values())
        return statistics.mean(counts) if counts else None

    @property
    def compactions_per_session_max(self) -> int | None:
        counts = list(self._compactions_per_session.values())
        return max(counts) if counts else None

    @property
    def trigger_mix(self) -> dict[str, int]:
        mix: dict[str, int] = {}
        for record in self.records:
            key = record.trigger or "unknown"
            mix[key] = mix.get(key, 0) + 1
        return mix

    @property
    def pre_median(self) -> float | None:
        values = [r.pre_tokens for r in self.records if r.pre_tokens is not None]
        return statistics.median(values) if values else None

    @property
    def post_median(self) -> float | None:
        values = [r.post_tokens for r in self.records if r.post_tokens is not None]
        return statistics.median(values) if values else None

    @property
    def dropped_total(self) -> int:
        return sum(r.dropped_tokens for r in self.records if r.dropped_tokens is not None)

    @property
    def dropped_share_of_cache_creation(self) -> float | None:
        """Dropped tokens as a percentage of every priced turn's
        cache_creation across the whole corpus folded in so far. ``None``
        when no cache_creation has been observed at all (nothing to
        divide by) — deliberately not 0.0, which would misleadingly read
        as "no tokens dropped".
        """
        if self.total_cache_creation == 0:
            return None
        return 100.0 * self.dropped_total / self.total_cache_creation

    @property
    def dropped_share_of_new_tokens(self) -> float | None:
        """Dropped tokens as a percentage of every priced turn's
        :func:`new_tokens` (``input_tokens + cache_creation_tokens``)
        across the whole corpus (fix item 9) — a second denominator
        alongside :attr:`dropped_share_of_cache_creation` that doesn't
        undercount a corpus where much of the traffic never hit a
        cache_control breakpoint at all. ``None`` when nothing has been
        observed to divide by.
        """
        if self.total_new_tokens == 0:
            return None
        return 100.0 * self.dropped_total / self.total_new_tokens

    @property
    def mean_duration_ms(self) -> float | None:
        values = [r.duration_ms for r in self.records if r.duration_ms is not None]
        return statistics.mean(values) if values else None

    @property
    def total_post_compaction_recache_cost(self) -> float:
        """Sum of ``next_turn_write_cost`` for compactions whose next
        turn both trips the minimal RE-CACHE heuristic and is tightly
        joined (fix item 9: ``join_delta_s`` at or under 15 minutes) —
        see :func:`_join_is_tight`.
        """
        return sum(
            r.next_turn_write_cost
            for r in self.records
            if r.next_turn_is_recache and r.next_turn_write_cost is not None and _join_is_tight(r)
        )

    @property
    def total_post_compaction_write_cost(self) -> float:
        """Sum of ``next_turn_write_cost`` for EVERY compaction's
        immediate next turn, regardless of the RE-CACHE flag, excluding
        loosely-joined records (fix item 9: ``join_delta_s`` over 15
        minutes — see :func:`_join_is_tight`).

        ``total_post_compaction_recache_cost`` only counts turns the
        shared RE-CACHE detector flags (:func:`recache.apply` — see the
        module docstring's WP10b addition note) — on a real 30-day corpus
        that detector fires for only a small fraction
        of post-compaction turns (most turns still hit a warm cache even
        right after a compaction), so that narrower total reads as a few
        dollars while every post-compaction turn's actual cache-write
        spend is an order of magnitude higher. This property is the
        all-inclusive figure a "what does compaction cost in re-written
        cache" report line should show.
        """
        return sum(
            r.next_turn_write_cost
            for r in self.records
            if r.next_turn_write_cost is not None and _join_is_tight(r)
        )

    def per_session_summary(self) -> list[tuple[str, int, int, float]]:
        """``(session_id, compaction_count, dropped_tokens, post-compaction
        write cost)`` for every session with >=1 compaction, sorted by
        dropped tokens descending (the ordering :func:`build_section`'s
        per-session table uses).

        ``compaction_count`` and ``dropped_tokens`` count every record
        regardless of join tightness (they don't depend on the next-turn
        join at all); the write-cost column excludes loosely-joined
        records (fix item 9), matching ``total_post_compaction_write_cost``.
        """
        by_session: dict[str, tuple[int, int, float]] = {}
        for record in self.records:
            count, dropped, cost = by_session.get(record.session_id, (0, 0, 0.0))
            count += 1
            dropped += record.dropped_tokens or 0
            if _join_is_tight(record):
                cost += record.next_turn_write_cost or 0.0
            by_session[record.session_id] = (count, dropped, cost)
        rows = [
            (session_id, count, dropped, cost)
            for session_id, (count, dropped, cost) in by_session.items()
        ]
        rows.sort(key=lambda row: row[2], reverse=True)
        return rows


# -- report section ---------------------------------------------------------


def build_section(stats: CompactionStats) -> Section:
    """The "Compactions" report section (key ``compactions``): a summary
    table, a trigger-mix table, and a per-session table (top 20 by
    dropped tokens).
    """
    summary_table = Table(
        name="compactions_summary",
        title="Compaction summary",
        columns=[
            Column(key="metric", label="Metric", kind="str"),
            Column(key="value", label="Value", kind="str"),
        ],
        rows=[
            ["Sessions with >=1 compaction", stats.sessions_with_compaction],
            ["Total sessions", stats.total_sessions],
            ["Compactions per session (mean)", stats.compactions_per_session_mean],
            ["Compactions per compacting session (mean)", stats.compactions_per_compacting_session_mean],
            ["Compactions per session (max)", stats.compactions_per_session_max],
            ["Pre-compaction tokens (median)", stats.pre_median],
            ["Post-compaction tokens (median)", stats.post_median],
            ["Dropped tokens (total)", stats.dropped_total],
            ["Dropped tokens (share of cache_creation)", stats.dropped_share_of_cache_creation],
            ["Dropped tokens (share of new_tokens: input+cache_creation)", stats.dropped_share_of_new_tokens],
            ["Mean duration (ms)", stats.mean_duration_ms],
            ["Summary requests (estimated, USD)", stats.summary_request_cost],
            ["Total post-compaction write cost (USD)", stats.total_post_compaction_write_cost],
            [
                "Total post-compaction RE-CACHE-flagged write cost (USD)",
                stats.total_post_compaction_recache_cost,
            ],
        ],
    )

    trigger_mix = stats.trigger_mix
    total_compactions = sum(trigger_mix.values())
    trigger_table = Table(
        name="compactions_trigger_mix",
        title="Trigger mix",
        columns=[
            Column(key="trigger", label="Trigger", kind="str"),
            Column(key="count", label="Count", kind="int"),
            Column(key="pct", label="Share", kind="pct"),
        ],
        rows=[
            [
                trigger,
                count,
                100.0 * count / total_compactions if total_compactions else None,
            ]
            for trigger, count in sorted(trigger_mix.items(), key=lambda kv: kv[0])
        ],
    )

    per_session_rows = stats.per_session_summary()[:_PER_SESSION_TABLE_LIMIT]
    per_session_table = Table(
        name="compactions_per_session",
        title=f"Top {_PER_SESSION_TABLE_LIMIT} sessions by dropped tokens",
        columns=[
            Column(key="session", label="Session", kind="str"),
            Column(key="count", label="Compactions", kind="int"),
            Column(key="dropped_tokens", label="Dropped tokens", kind="tokens"),
            Column(key="write_cost", label="Post-compaction write cost", kind="money"),
        ],
        rows=[list(row) for row in per_session_rows],
    )

    # The reply after a summary is judged by the shared recache.py
    # detector (recache.apply's full signature classification), the one
    # the cache-rebuild section uses, so the two can't drift apart;
    # is_recache_turn's standalone rule is kept only for its own tests.
    notes = [
        "The reply right after a summary counts as a cache rebuild by the "
        "same check the cache rebuild section uses. So the two always agree.",
        "\"Tokens removed, as a % of all cache writes\" divides the tokens "
        "removed by every reply's cache writes, not only the replies after a "
        "summary. So it can pass 100% when summaries are large next to "
        "ordinary cache growth. The row below it divides by new input and "
        "cache writes together. So it doesn't undercount sessions where much "
        "of the traffic never reached the cache. Tokens removed are counted "
        "per summary, not from Claude Code's running total (which would count "
        "some tokens two or three times).",
        "\"Cache write cost on the reply after a summary\" adds up the cache "
        "write on the reply right after every summary. The row below it counts "
        "only the replies that rebuilt most of the cache. It is usually far "
        "smaller, since most replies after a summary still read a warm cache. "
        "Both totals, and the per-session write cost, leave out a summary "
        "whose next reply came more than 15 minutes later. That reply most "
        "likely belongs to a resumed session.",
        "\"Cost of writing the summaries (estimated)\" is the request that wrote each summary. "
        "Claude Code bills it but does not log it, so it is estimated. That is the context read "
        "from the cache once more, with the summary as output. It counts in spend.",
    ]
    if stats.scheduled_sessions:
        notes.append(
            f"{stats.scheduled_sessions} main session{'s' if stats.scheduled_sessions != 1 else ''} a scheduled "
            "task started, with no message of yours, are left out. Counting checks that never summarise would "
            "lower the summaries per session."
        )
    if not stats.records:
        notes.insert(0, "No conversation summaries found in this window.")

    return Section(
        key="compactions",
        title="Compactions",
        tables=[summary_table, trigger_table, per_session_table],
        notes=notes,
    )


# -- rediscovery --------------------------------------------------------


@dataclass(slots=True)
class RediscoveryWindow:
    """Read/Grep/Glob turn counts in the ``_REDISCOVERY_WINDOW`` priced
    turns immediately before and after one compaction. ``*_total`` is the
    number of turns actually available in each window — it can be less
    than ``_REDISCOVERY_WINDOW`` near either end of a transcript.
    """

    session_id: str = ""
    compaction_ts: str | None = None
    before_count: int = 0
    before_total: int = 0
    after_count: int = 0
    after_total: int = 0


def _is_rediscovery_turn(turn: Turn) -> bool:
    return any(name in _REDISCOVERY_TOOLS for name in turn.tool_names)


def rediscovery(tr: TranscriptResult) -> list[RediscoveryWindow]:
    """One :class:`RediscoveryWindow` per ``COMPACT_BOUNDARY`` event in
    ``tr``: how many of the ``_REDISCOVERY_WINDOW`` (10) priced turns
    right after it used Read/Grep/Glob, versus the ``_REDISCOVERY_WINDOW``
    right before — counts only, no file identity or content.
    """
    priced_turns = _priced_turns(tr)
    session_id = tr.meta.session_id
    windows: list[RediscoveryWindow] = []
    for event, turn_idx in _correlate_compactions_to_turn_index(tr, priced_turns):
        # "Before" turns: the up-to-10 priced turns strictly earlier than
        # the compaction's own following turn (or, if there was no
        # following turn, strictly earlier than the transcript's end).
        before_end = turn_idx if turn_idx is not None else len(priced_turns)
        before_slice = priced_turns[max(0, before_end - _REDISCOVERY_WINDOW):before_end]
        after_slice = (
            priced_turns[turn_idx:turn_idx + _REDISCOVERY_WINDOW] if turn_idx is not None else []
        )

        windows.append(
            RediscoveryWindow(
                session_id=session_id,
                compaction_ts=event.ts,
                before_count=sum(1 for t in before_slice if _is_rediscovery_turn(t)),
                before_total=len(before_slice),
                after_count=sum(1 for t in after_slice if _is_rediscovery_turn(t)),
                after_total=len(after_slice),
            )
        )
    return windows


__all__ = [
    "is_recache_turn",
    "new_tokens",
    "CompactionRecord",
    "compaction_records_for_transcript",
    "CompactionStats",
    "effective_autocompact_threshold",
    "build_section",
    "RediscoveryWindow",
    "rediscovery",
]

"""RE-CACHE detection and reporting: which turns paid to re-write a prompt
prefix that should have been a cheap cache hit, and why.

This is WP3 of the project plan (see plan "RE-CACHE" section and Appendix
A5's ``notification-invalidation``/``long-tool-waits`` recommendation
thresholds, which read the tables built here). It depends only on
``model.py`` and ``pricing.py``.

Three things live here:

- :class:`RecacheThresholds` — the four tunable numbers that decide
  whether a turn counts as a re-cache, plus its signatures.
- :func:`detect` — the pure classifier: given a transcript's turns and a
  set of thresholds, which turns are re-caches, and which signature.
- :func:`gap_bucket` — buckets a turn's inter-turn gap for the gap-cause
  table.
- :class:`RecacheStats` / :func:`build_section` — a corpus-wide
  accumulator and the ``Section`` it renders into: summary, signature
  split, gap buckets, preceding tool, top command prefixes, primary
  cause, event co-occurrence, attachment sub-split, by-agent-type, and
  huge-context cache-read volume.

Definitions (plan "RE-CACHE" section):

A turn is a **re-cache** when it is not the transcript's first priced
turn, its context is above ``ctx_floor``, and the part of that context
that is the session's own was not read from the cache -- the model had to
pay to write it again instead of reading it from a warm cache entry.

Every context starts with a prefix the sessions of one kind share (the
tool definitions, written once and read by every later session), so the
first priced call's ``cache_read_tokens`` (``cr0``) is the size of that
shared start and it survives an expiry: an expired cache still reads
about ``cr0``. A rebuild is therefore judged on what lies beyond ``cr0``
(Phase 8 "Cache rebuild labels"). A flagged turn is one of:

- the original test: it read under ``cr_ratio`` of its whole context; or
- its session part was lost: it read no more than ``cr0`` +
  ``SESSION_READ_MARGIN`` while the previous call had left meaningfully
  more than that warm, and its context did not shrink (a shrink is a
  compaction or a ``/clear``, which replaces the session part rather
  than letting it expire); or
- it read under ``cr_ratio`` of the part beyond ``cr0``.

Every re-cache turn gets one of four **signatures**, tested in this
order, the first that applies winning:

1. ``limit-expiry`` -- the gap spanned a usage-cap pause (below).
2. ``post-compaction`` -- the turn is the first reply after a compaction
   (a ``compact_boundary`` before it, or the estimated compaction call
   right before it). The summary replaced the session part, so the
   write is the compaction's, whatever the wait or the read looked like.
   It is still a rebuild (``is_recache``), so everything that counts
   rebuilds counts it as before; only the label differs.
3. ``full-expiry`` -- the gap was at or past the time the previous call's
   cache lasted: 1 hour when its 1-hour write exceeded its 5-minute
   write, else 5 minutes (carried forward from the last call that wrote
   when it wrote nothing). The cache entry timed out.
4. ``full-expiry`` -- the turn read no more than ``cr0`` +
   ``SESSION_READ_MARGIN`` (or under ``full_expiry_cr`` outright): the
   session part had gone even though the gap was shorter than the
   lifetime, so for this purpose it expired.
5. ``prefix-invalidated`` -- only then: part of the session was read, so
   it had not expired, but something upstream of the cached prefix
   changed (a notification, an attachment, a model switch, ...) and broke
   it anyway.

When ``cr0`` or the previous call is not known (a list that does not
start at the transcript's first turn), only the original test and the
``full_expiry_cr`` floor apply, as before.

A turn's **avoidable cost** is what its own cache-creation tokens cost at
the write rate they were actually billed at, minus what those same
tokens would have cost at the flat cache-read rate had the cache not
been invalidated — computed with two ``pricing.price_turn`` calls on the
same turn so every other cost component (input, output, the turn's own
already-observed cache reads) cancels out of the subtraction exactly.

Privacy: every field this module reads off a ``Turn`` (tool names, event
kinds, a ``<=40``-char command prefix, token counts) is already
privacy-clean per ``model.py``'s contract; this module never adds a new
field that could hold message text, a full path, or a full command.

Usage-limits batch (v3-limits): a re-cache turn whose ``Turn.gap_cause ==
"limit"`` (the gap to the previous turn spanned a usage-cap pause -- see
model.py's/parse.py's module docstrings) gets the signature
``"limit-expiry"`` instead of full-expiry/prefix-invalidated -- checked
first in :func:`detect`, since the cache genuinely had nothing left to
hit (same shape as full-expiry) but the cause was an external pause, not
a caching problem. :func:`build_section` shows it as its own row in
``recache_signature_split`` (still computed from the *full* re-cache
population, so its own per-signature cost is never hidden) and excludes
it from every behavioural cause-attribution table (gap buckets,
preceding tool, top command prefixes, primary cause, event
co-occurrence, by-agent-type) via a ``behavioural_turns``/
``behavioural_records`` population computed once at the top of
:func:`build_section` -- a usage-cap pause isn't a caching behaviour to
diagnose. ``recommend.py``'s ``_rule_long_tool_waits``/
``_rule_notification_invalidation`` read exactly these behavioural
tables, so they automatically ignore limit-induced turns with no changes
of their own needed.

Review B5: ``recache_summary``'s headline ``avoidable_cost_usd`` is
likewise computed from ``behavioural_records`` only, never the full
re-cache population -- a limit-expiry turn's cost is unavoidable (an
external pause, not a caching problem to fix) and is already reported
separately by ``limits.py``'s ``limits_summary.limit_turn_write_cost_usd``,
so folding it into the headline "money you could save" figure would both
mislabel it and double-count it against that other table. It is instead
surfaced as its own ``unavoidable_limit_expiry_cost_usd`` column on the
same row. See :func:`_summary_table`'s notes and ``docs/limits.md`` for
exactly how that figure relates to (but does not equal)
``limits_summary``'s own -- ``recache.detect`` additionally requires
``ctx > ctx_floor`` and the ``cr_ratio`` test that ``limits.py`` does not,
so the two populations differ (N2).

v0.2.0 fix A2: ``recache_summary`` and ``recache_huge_context`` are each a
single-row table whose row used to start with a bare numeric count
(``transcripts`` / ``len(huge_turns)``) as ``row[0]`` — a table's row-key
column must be a non-empty string (every other table's row key already is;
see ``tests/test_recommend_contract.py``), so both now carry an explicit
leading ``metric`` column, always the literal string ``"all"`` (there is
only ever one row, covering the whole corpus or the one ``group`` a caller
filtered to).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Callable, Sequence

from .model import Column, EventKind, Section, Table, TranscriptResult, Turn, agent_type_label
from .pricing import ModelRates, Pricing, ResolvedRates, price_turn

#: Bucket labels, in report order. "unknown" covers a turn with no
#: measurable gap (the transcript's first priced turn, or a timestamp
#: parse failure — see parse.py's ``Diagnostics.timestamp_parse_failures``).
GAP_BUCKETS: tuple[str, ...] = ("<1m", "1-5m", "5-15m", "15-60m", ">60m", "unknown")

#: The re-cache signatures, in report order. "limit-expiry" (usage-limits
#: addition, see module docstring) is checked first in detect() and takes
#: priority over the others; "post-compaction" comes next.
SIGNATURES: tuple[str, ...] = ("full-expiry", "prefix-invalidated", "post-compaction", "limit-expiry")

#: How far past the shared start (the first call's cache read, ``cr0``) a
#: read may go and still be an expiry of the session part. A warm call
#: reads everything the previous call left cached, so a read this close to
#: ``cr0`` means the session's own part was not read; the margin absorbs
#: the few thousand tokens a re-sent harness block adds to that prefix.
SESSION_READ_MARGIN: int = 3_000

#: The version of the rules ``detect`` flags and labels rebuilds by,
#: stored with a baseline (``recache_rules``) so a later comparison can
#: tell a share counted by older rules. 2: the Phase 8 order.
RULES = 2

#: How long a cache entry lasts, by the TTL the call wrote it at.
_TTL_5M_S: float = 300.0
_TTL_1H_S: float = 3600.0

#: This module's own detection/costing assumptions, printed verbatim in
#: the report's "## Assumptions" block (``ReportMeta.assumptions`` —
#: see ``report.py``) alongside ``ttl.ASSUMPTIONS``.
ASSUMPTIONS: tuple[str, ...] = (
    "a turn's own ctx/cache_read_tokens fields are the whole story: a "
    "large context with little of its own part read from the cache is "
    "treated as a re-cache regardless of what caused it",
    "the first priced turn of a transcript is never itself a re-cache "
    "(nothing existed to invalidate yet), and its cache read is the "
    "prefix every session of that kind shares, which an expired cache "
    "still reads",
    "a flagged turn is an expiry when its wait reached the time the "
    "previous call's cache lasted (1 hour if that call wrote more at 1 "
    "hour than at 5 minutes, else 5 minutes, carried forward when it "
    "wrote nothing), or when it read no more than the shared start plus "
    f"{SESSION_READ_MARGIN:,} tokens; only a turn that read more of its "
    "session than that is called broken by a change",
    "a context that shrank since the previous call is a compaction or a "
    "/clear, not an expiry, so only the original read-ratio test can flag it",
    "avoidable cost is computed by re-pricing the same turn as if its "
    "cache-creation tokens had instead been a cache read, holding every "
    "other component of the turn fixed",
)

#: The note on every per-agent-type table (``value_labels`` shows the
#: main session's ``"top-level"`` row key as "Main session").
_AGENT_TYPE_NOTE = "Each subagent type as Claude Code recorded it, plus one row for the main session."


@dataclass(slots=True)
class RecacheThresholds:
    """The four tunable numbers that decide re-cache detection (plan
    Appendix A5's defaults). All overridable via ``config.toml``'s
    ``[thresholds]`` table or CLI flags — :meth:`from_config` reads
    either a flat dict of these four keys or a full config dict with a
    nested ``thresholds`` table, so a caller can pass either shape.
    """

    #: A turn's ``ctx`` must exceed this before it can ever be flagged,
    #: however low its cache-read ratio — a small early-conversation turn
    #: re-writing a small prefix isn't worth reporting on.
    ctx_floor: int = 20_000
    #: A turn is flagged when it read under this share of its whole context,
    #: or of the part beyond ``cr0`` (see the module docstring).
    cr_ratio: float = 0.2
    #: A flagged turn that read under this many tokens is "full-expiry"
    #: outright; otherwise the signature follows the order in the module docstring.
    full_expiry_cr: int = 2_000
    #: The context-size threshold for the huge-context cache-read-volume
    #: table (plan: "36.5% of recent top-level turns had ctx > 200k" —
    #: reported as a context-hygiene metric, not a pricing surcharge).
    huge_ctx: int = 200_000

    @classmethod
    def from_config(cls, config: dict | None) -> "RecacheThresholds":
        """Build thresholds from a config dict, keeping this class's
        defaults for any key that's absent or of the wrong shape.

        Accepts either a flat dict of the four field names directly, or
        a full ``config.toml``-shaped dict with a nested ``thresholds``
        table (``{"thresholds": {"ctx_floor": ...}}``) — whichever a
        caller happens to have loaded. Unknown keys are ignored.
        """
        data = config or {}
        if not isinstance(data, dict):
            data = {}
        nested = data.get("thresholds")
        if isinstance(nested, dict):
            data = nested

        kwargs: dict = {}
        if "ctx_floor" in data:
            kwargs["ctx_floor"] = int(data["ctx_floor"])
        if "cr_ratio" in data:
            kwargs["cr_ratio"] = float(data["cr_ratio"])
        if "full_expiry_cr" in data:
            kwargs["full_expiry_cr"] = int(data["full_expiry_cr"])
        if "huge_ctx" in data:
            kwargs["huge_ctx"] = int(data["huge_ctx"])
        return cls(**kwargs)

    def describe(self) -> list[str]:
        """One sentence per threshold, for the report's thresholds block
        (and this module's table notes)."""
        return [
            f"A reply only counts as a cache rebuild when its context is over {self.ctx_floor:,} tokens.",
            f"It counts when it read less than {self.cr_ratio:.0%} of that context's own part from the cache, "
            "or when it read only the start every session shares although the previous reply had left more "
            "than that cached. Compactions and /clear, which shrink the context, are not counted this way.",
            "A rebuild is \"Cache expired\" when the wait since the previous reply reached the time its cache "
            f"lasts (5 minutes, or 1 hour for an hour-long write), or when it read no more than "
            f"{SESSION_READ_MARGIN:,} tokens beyond the shared start (or under {self.full_expiry_cr:,} tokens "
            "in all). Any other rebuild is \"Cache broken by a change\". The first reply after a "
            "conversation summary is \"Rewritten after a summary\" instead, whatever the wait.",
            f"A context counts as very large at {self.huge_ctx:,} tokens when its model's own context "
            "window isn't known.",
        ]


def _write_ttl_s(turn: Turn) -> float | None:
    """How long the cache entry this call wrote lasts, or ``None`` when it
    wrote nothing. 1 hour when its 1-hour write exceeds its 5-minute
    write, else 5 minutes; a call whose JSONL recorded no TTL split
    (``ttl_split_unknown``) is 5 minutes, as ``ttl.normalize_ttl_split``
    assumes."""
    if turn.cc_1h > turn.cc_5m:
        return _TTL_1H_S
    if turn.cc_5m > 0 or turn.cc_1h > 0:
        return _TTL_5M_S
    if turn.cache_creation_tokens > 0 and turn.ttl_split_unknown:
        return _TTL_5M_S
    return None


def _is_rebuild(turn: Turn, prev: Turn | None, cr0: int | None, th: RecacheThresholds) -> bool:
    """Whether ``turn`` paid to write again context that should have been
    read. The original test is kept as it was; the two widenings need
    ``cr0`` and the previous call, and say nothing without them."""
    cr = turn.cache_read_tokens
    if cr < th.cr_ratio * turn.ctx:
        return True
    if cr0 is None or prev is None:
        return False
    if turn.ctx < prev.ctx:
        # Shrunk: a compaction or /clear replaced the session part. The
        # shared start is all a fresh context reads, but nothing expired.
        return False
    # What a warm cache would have served: everything the previous call
    # read or wrote. If that was little more than the shared start, a warm
    # read and an expired one look the same, so neither is flagged here.
    if prev.cache_read_tokens + prev.cache_creation_tokens - cr <= SESSION_READ_MARGIN:
        return False
    if cr <= cr0 + SESSION_READ_MARGIN:
        return True
    own_part = turn.ctx - cr0
    return own_part > 0 and cr - cr0 < th.cr_ratio * own_part


def detect(turns: Sequence[Turn], th: RecacheThresholds) -> list[Turn]:
    """Return copies of every re-cache turn in ``turns``, with
    ``is_recache`` and ``recache_signature`` set.

    Pure: never mutates ``turns`` or any ``Turn`` in it. A turn qualifies
    when it is not the transcript's first priced turn (``turn_index >
    1``), is not synthetic, its ``ctx`` exceeds ``th.ctx_floor``, and
    :func:`_is_rebuild` finds that it did not read its own part of the
    context (see the module docstring). Its signature is the first that
    applies of: ``limit-expiry`` (the gap spanned a usage-cap pause);
    ``post-compaction`` (the first reply after a compaction);
    ``full-expiry`` when the gap reached the TTL the previous call wrote
    at; ``full-expiry`` when it read no more than ``cr0`` +
    :data:`SESSION_READ_MARGIN` (or under ``th.full_expiry_cr``);
    otherwise ``prefix-invalidated``. ``cr0`` is the cache read of the
    transcript's first priced turn when ``turns`` includes it.

    The returned list contains only the qualifying turns (as new objects,
    via ``dataclasses.replace``), in the same relative order as ``turns``
    -- callers that need the full turn list with these substituted in
    should index by ``message_id`` (unique within one transcript).
    """
    detected: list[Turn] = []
    cr0 = next((t.cache_read_tokens for t in turns if t.turn_index == 1 and not t.is_synthetic), None)
    prev: Turn | None = None
    ttl_s: float | None = None
    summarised = False
    for turn in turns:
        if turn.is_synthetic:
            # The estimated request that wrote a compaction's summary: the
            # next reply is the first one after it.
            summarised = summarised or turn.estimated == "compaction"
            continue
        # The first real reply after a compaction, from either mark of it.
        after_summary = summarised or EventKind.COMPACT_BOUNDARY in turn.preceding_event_kinds
        summarised = False
        # The previous call and the TTL it wrote at are updated after this
        # turn is judged, whichever way it falls.
        before, before_ttl = prev, ttl_s
        prev = turn
        ttl_s = _write_ttl_s(turn) or ttl_s
        if turn.turn_index <= 1:
            continue
        if turn.ctx <= th.ctx_floor:
            continue
        if not _is_rebuild(turn, before, cr0, th):
            continue
        if turn.gap_cause == "limit":
            # Usage-limits addition (see module docstring): the cache had
            # nothing left to hit because a usage-cap pause intervened,
            # not because of ordinary TTL expiry or an invalidation.
            signature = "limit-expiry"
        elif after_summary:
            # The summary replaced the session part: the wait since the last
            # real reply and the small read are the compaction's doing, not an
            # expiry of the session part.
            signature = "post-compaction"
        elif turn.gap_s is not None and before_ttl is not None and turn.gap_s >= before_ttl:
            signature = "full-expiry"
        elif turn.cache_read_tokens < th.full_expiry_cr or (
            cr0 is not None and turn.cache_read_tokens <= cr0 + SESSION_READ_MARGIN
        ):
            signature = "full-expiry"
        else:
            signature = "prefix-invalidated"
        detected.append(dataclasses.replace(turn, is_recache=True, recache_signature=signature))
    return detected


def apply(result: TranscriptResult, th: RecacheThresholds) -> TranscriptResult:
    """Return a new :class:`TranscriptResult` whose ``turns`` carry the
    ``is_recache``/``recache_signature`` :func:`detect` computed against
    ``th``, in place of the transcript's own as-parsed turns (which never
    set those two fields — see ``parse.py``).

    Pure, like :func:`detect`: ``result`` and every ``Turn`` in it are
    left untouched; the returned ``TranscriptResult`` is a shallow copy
    (``dataclasses.replace``) with only ``turns`` substituted. Every
    non-qualifying turn is carried through unchanged (same object, not a
    copy) so identity-sensitive callers (e.g. a cache keyed by object id)
    still see the same turn for anything :func:`detect` didn't flag.

    This is what makes ``Turn.recache_signature`` actually reach a
    turn's real, in-report copy: :class:`RecacheStats` computes the same
    ``detect`` result internally for its own accumulation, but never
    mutates the ``TranscriptResult`` it was handed, so a caller that
    wants the signature to be visible on the turns themselves (e.g.
    ``ttl.py``'s ``simulate``/``TtlStats``, run over the same corpus)
    must call ``apply`` first and feed the result onward.
    """
    detected_by_id = {t.message_id: t for t in detect(result.turns, th)}
    new_turns = [detected_by_id.get(t.message_id, t) for t in result.turns]
    return dataclasses.replace(result, turns=new_turns)


def gap_bucket(gap_s: float | None) -> str:
    """Bucket an inter-turn gap into one of :data:`GAP_BUCKETS`.

    ``None`` (no previous turn, or a timestamp parse failure) buckets as
    "unknown". The 5-minute boundary is inclusive on its lower side: a
    gap of exactly 300 seconds falls in "5-15m", not "1-5m" — each
    bucket's lower bound is inclusive, its upper bound exclusive.
    """
    if gap_s is None:
        return "unknown"
    if gap_s < 60:
        return "<1m"
    if gap_s < 300:
        return "1-5m"
    if gap_s < 900:
        return "5-15m"
    if gap_s < 3600:
        return "15-60m"
    return ">60m"


def _pct(part: float, whole: float) -> float:
    return 100.0 * part / whole if whole else 0.0


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _avoidable_cost(turn: Turn, rates: ResolvedRates | ModelRates | None) -> float:
    """The re-cache turn's avoidable cost: its own cache-creation tokens
    priced at the write rate they were actually billed at, minus those
    same tokens priced at the flat cache-read rate. Both calls hold
    every other component of the turn fixed, so input/output/already-
    observed cache-read cost cancel out of the subtraction exactly (see
    the module docstring's worked example).
    """
    if rates is None:
        return 0.0
    actual = price_turn(turn, rates)
    hypothetical_read = turn.cache_read_tokens + turn.cache_creation_tokens
    hypothetical = price_turn(turn, rates, write_split={"5m": 0, "1h": 0}, read_tokens=hypothetical_read)
    return actual.total - hypothetical.total


@dataclass(slots=True)
class _Record:
    """One priced turn folded into a :class:`RecacheStats` accumulator.
    ``turn`` is the re-cache-detected copy when it qualified, else the
    turn as parsed — so a single pass over ``RecacheStats.records``
    yields both the re-cache population (``turn.is_recache``) and the
    "all priced turns" control population in one list.
    """

    turn: Turn
    avoidable_cost: float
    agent_type: str
    group: str


class RecacheStats:
    """Accumulates re-cache facts across every transcript in a corpus (or
    a ``--group-by`` slice of one), for :func:`build_section` to render.

    ``group_key``, when given, is called once per :meth:`add` with the
    ``TranscriptResult`` and must return a short grouping label (plan:
    "so WP10 can pass session mode"). :func:`build_section` can then be
    called once per group by passing that same label as its ``group``
    argument, or once for the whole corpus by leaving it ``None``.
    """

    def __init__(
        self,
        th: RecacheThresholds,
        group_key: Callable[[TranscriptResult], str] | None = None,
    ) -> None:
        self.th = th
        self.group_key = group_key
        self.transcripts = 0
        #: group label -> transcripts folded into that group, so a
        #: group-filtered ``build_section`` reports the right transcript
        #: count instead of the corpus-wide total.
        self.transcripts_by_group: dict[str, int] = {}
        self.records: list[_Record] = []

    def add(
        self,
        result: TranscriptResult,
        rates_lookup: Callable[[str], ResolvedRates | ModelRates | None],
    ) -> None:
        """Fold one transcript's priced, non-synthetic turns into the
        accumulator, running :func:`detect` internally against
        ``self.th``. Every priced turn is recorded, not just the
        detected re-cache ones, so :func:`build_section` can compute
        both the re-cache tables and their all-priced-turns control
        columns from the same population.
        """
        self.transcripts += 1
        group = self.group_key(result) if self.group_key is not None else "all"
        self.transcripts_by_group[group] = self.transcripts_by_group.get(group, 0) + 1
        agent_type = agent_type_label(result)

        recache_by_id = {t.message_id: t for t in detect(result.turns, self.th)}
        for turn in result.turns:
            if turn.is_synthetic or turn.turn_index <= 0:
                continue
            effective = recache_by_id.get(turn.message_id, turn)
            avoidable = 0.0
            if effective.is_recache:
                rates = rates_lookup(effective.model)
                avoidable = _avoidable_cost(effective, rates)
            self.records.append(_Record(turn=effective, avoidable_cost=avoidable, agent_type=agent_type, group=group))

    def groups(self) -> tuple[str, ...]:
        """Every distinct group label seen so far, sorted."""
        return tuple(sorted({r.group for r in self.records}))


def build_section(stats: RecacheStats, pricing: Pricing, th: RecacheThresholds, group: str | None = None) -> Section:
    """Render ``stats`` into the ``recache`` report section: summary,
    signature split, gap buckets, preceding tool, top command prefixes,
    primary cause, event co-occurrence, attachment sub-split,
    by-agent-type, and huge-context cache-read volume.

    ``group``, when given, restricts the section to records whose
    :class:`RecacheStats` ``group_key`` returned that label (see
    :class:`RecacheStats`'s docstring); ``None`` (the default) covers
    every record regardless of group.
    """
    records = [r for r in stats.records if group is None or r.group == group]
    all_turns = [r.turn for r in records]
    recache_records = [r for r in records if r.turn.is_recache]
    recache_turns = [r.turn for r in recache_records]

    transcripts = stats.transcripts if group is None else stats.transcripts_by_group.get(group, 0)
    total_priced = len(all_turns)
    total_recache = len(recache_turns)
    total_cc_all = sum(t.cache_creation_tokens for t in all_turns)
    total_cc_recache = sum(t.cache_creation_tokens for t in recache_turns)

    prefix_invalidated_turns = [t for t in recache_turns if t.recache_signature == "prefix-invalidated"]

    # Usage-limits addition (see module docstring): the behavioural
    # population feeds every cause-attribution table -- a usage-cap pause
    # isn't a caching behaviour to diagnose, so it's excluded here while
    # still counted (and shown as its own signature row) above.
    behavioural_records = [r for r in recache_records if r.turn.recache_signature != "limit-expiry"]
    behavioural_turns = [r.turn for r in behavioural_records]
    total_behavioural = len(behavioural_turns)
    total_cc_behavioural = sum(t.cache_creation_tokens for t in behavioural_turns)

    # Review B5: the headline "avoidable" figure must only ever total the
    # behavioural population -- a limit-expiry turn's cost delta is
    # unavoidable (the module docstring's own framing), and it is also
    # already reported by limits.py's limits_summary, so folding it into
    # avoidable_cost_usd both mis-labels it and double-counts it against
    # that other table. Reported separately here instead.
    total_avoidable = sum(r.avoidable_cost for r in behavioural_records)
    total_unavoidable_limit = sum(
        r.avoidable_cost for r in recache_records if r.turn.recache_signature == "limit-expiry"
    )

    tables = [
        _summary_table(
            transcripts,
            total_priced,
            total_recache,
            total_cc_recache,
            total_cc_all,
            total_avoidable,
            total_unavoidable_limit,
        ),
        _signature_table(recache_turns, recache_records),
        _gap_bucket_table(all_turns, behavioural_turns, total_cc_behavioural, total_cc_all, total_priced),
        _preceding_tool_table(all_turns, behavioural_turns, total_cc_behavioural, total_cc_all, total_priced),
        _top_command_prefix_table(behavioural_turns),
        _primary_cause_table(
            all_turns, behavioural_turns, behavioural_records, total_behavioural, total_priced, total_cc_behavioural, total_cc_all
        ),
        _primary_cause_prefix_invalidated_table(all_turns, prefix_invalidated_turns, total_priced, total_cc_all),
        _cooccurrence_table(all_turns, behavioural_turns, total_behavioural, total_priced),
        _attachment_subsplit_table(recache_turns),
        _by_agent_type_table(records),
        _huge_context_table(all_turns, th, pricing),
    ]

    notes = [f"Thresholds: {' '.join(th.describe())}"]
    if group is not None:
        notes.append(f"Filtered to group: {group}.")
    notes.append(f"Avoidable cost uses prices from pricing.toml, version {pricing.version}.")

    return Section(key="recache", title="Re-cache events", tables=tables, notes=notes)


def _summary_table(
    transcripts: int,
    total_priced: int,
    total_recache: int,
    total_cc_recache: int,
    total_cc_all: int,
    total_avoidable: float,
    total_unavoidable_limit: float,
) -> Table:
    return Table(
        name="recache_summary",
        title="Re-cache summary",
        columns=[
            Column(key="metric", label="Metric", kind="str"),
            Column(key="transcripts", label="Transcripts", kind="int"),
            Column(key="priced_turns", label="Priced turns", kind="int"),
            Column(key="recache_turns", label="Re-cache turns", kind="int"),
            Column(key="recache_turn_share_pct", label="Re-cache turn share", kind="pct"),
            Column(key="recache_cc_tokens", label="Cache-creation tokens (re-cache)", kind="tokens"),
            Column(key="total_cc_tokens", label="Cache-creation tokens (all)", kind="tokens"),
            Column(key="recache_cc_share_pct", label="Cache-creation share", kind="pct"),
            Column(key="avoidable_cost_usd", label="Avoidable cost", kind="money"),
            Column(
                key="unavoidable_limit_expiry_cost_usd",
                label="Unavoidable cost (limit-expiry)",
                kind="money",
            ),
        ],
        rows=[
            [
                "all",
                transcripts,
                total_priced,
                total_recache,
                _pct(total_recache, total_priced),
                total_cc_recache,
                total_cc_all,
                _pct(total_cc_recache, total_cc_all),
                round(total_avoidable, 6),
                round(total_unavoidable_limit, 6),
            ]
        ],
        notes=[
            "A reply counts as a cache rebuild when three things hold. It isn't the first reply in its "
            "conversation. Its context is large. And it read only a small share of that context from the "
            "cache. The thresholds below set both.",
            "Avoidable cost leaves out rebuilds right after a usage-limit pause, which have their own "
            "column. Waiting for a limit to reset isn't a caching habit to fix. The usage limits "
            "section prices every reply after a pause, not only the ones that count as rebuilds. So "
            "its figure is related to this one but not the same.",
        ],
    )


def _signature_table(recache_turns: list[Turn], recache_records: list[_Record]) -> Table:
    rows = []
    for sig in SIGNATURES:
        sub = [t for t in recache_turns if t.recache_signature == sig]
        cc = sum(t.cache_creation_tokens for t in sub)
        cost = sum(r.avoidable_cost for r in recache_records if r.turn.recache_signature == sig)
        ctx_med = _median([float(t.ctx) for t in sub])
        gap_med = _median([t.gap_s for t in sub if t.gap_s is not None])
        rows.append([sig, len(sub), cc, round(cost, 6), ctx_med, gap_med])
    return Table(
        name="recache_signature_split",
        title="Re-cache signature split",
        columns=[
            Column(key="signature", label="Signature", kind="str"),
            Column(key="turns", label="Turns", kind="int"),
            Column(key="cc_tokens", label="Cache-creation tokens", kind="tokens"),
            Column(key="cost_delta_usd", label="Avoidable cost", kind="money"),
            Column(key="median_ctx", label="Median ctx", kind="tokens"),
            Column(key="median_gap_s", label="Median gap", kind="secs"),
        ],
        rows=rows,
        notes=[
            "Cache expired: the wait reached the time the cache lasts, or the reply read only the start "
            "every session shares and none of its own part. Cache broken by a change: it read part of "
            "its own part, so the cache hadn't expired, but something earlier in the context "
            "changed. Rewritten after a summary: the first reply after a conversation summary, which "
            "replaced what the cache held. Expired during a usage-limit pause: the wait before the reply "
            "spanned a pause for a usage limit. That last row is shown here but left out of every other "
            "table's causes.",
        ],
    )


def _weighted_bucket_stats(
    keys: Sequence,
    key_of: Callable[[Turn], object],
    control_turns: list[Turn],
    target_turns: list[Turn],
    total_target: int,
    total_control: int,
    total_cc_target: int,
    total_cc_control: int,
) -> dict:
    """Fix item 4: shared turn-count *and* token-weighted tallying for
    every table that buckets re-cache turns by some key (gap bucket,
    preceding tool, primary cause) and reports a control column next to
    it. Returns ``{key: [turns, share_pct_turns, control_turns,
    control_share_pct_turns, cc_tokens, cc_share_pct, control_cc_tokens,
    control_cc_share_pct]}`` for every key in ``keys``.

    Reporting only a token-weighted share for the target population next
    to a turn-count share for the control population (the pre-fix shape
    of ``recache_gap_buckets``/``recache_preceding_tool``) mixes bases:
    a bucket holding few turns but a lot of tokens looks artificially
    under-represented against a turn-counted baseline. Both bases are
    computed here so a caller can report either pair, or both.
    """
    control_counts: dict = {}
    control_cc: dict = {}
    for t in control_turns:
        k = key_of(t)
        control_counts[k] = control_counts.get(k, 0) + 1
        control_cc[k] = control_cc.get(k, 0) + t.cache_creation_tokens
    target_counts: dict = {}
    target_cc: dict = {}
    for t in target_turns:
        k = key_of(t)
        target_counts[k] = target_counts.get(k, 0) + 1
        target_cc[k] = target_cc.get(k, 0) + t.cache_creation_tokens

    stats = {}
    for k in keys:
        turns_n = target_counts.get(k, 0)
        cc = target_cc.get(k, 0)
        ctrl_n = control_counts.get(k, 0)
        ctrl_cc = control_cc.get(k, 0)
        stats[k] = [
            turns_n,
            _pct(turns_n, total_target),
            ctrl_n,
            _pct(ctrl_n, total_control),
            cc,
            _pct(cc, total_cc_target),
            ctrl_cc,
            _pct(ctrl_cc, total_cc_control),
        ]
    return stats


def _gap_bucket_table(
    all_turns: list[Turn], recache_turns: list[Turn], total_cc_recache: int, total_cc_all: int, total_priced: int
) -> Table:
    total_recache = len(recache_turns)
    stats_by_bucket = _weighted_bucket_stats(
        GAP_BUCKETS, lambda t: gap_bucket(t.gap_s), all_turns, recache_turns, total_recache, total_priced, total_cc_recache, total_cc_all
    )
    rows = [[bucket, *stats_by_bucket[bucket]] for bucket in GAP_BUCKETS]
    return Table(
        name="recache_gap_buckets",
        title="Re-cache by inter-turn gap",
        columns=[
            Column(key="bucket", label="Gap since previous turn", kind="str"),
            Column(key="turns", label="Re-cache turns", kind="int"),
            Column(key="share_pct_turns", label="Share of re-cache turns", kind="pct"),
            Column(key="control_turns", label="All priced turns (control)", kind="int"),
            Column(key="control_share_pct_turns", label="Control share (turns)", kind="pct"),
            Column(key="cc_tokens", label="Cache-creation tokens", kind="tokens"),
            Column(key="cc_share_pct", label="Share of re-cache cc tokens", kind="pct"),
            Column(key="control_cc_tokens", label="Cache-creation tokens (control)", kind="tokens"),
            Column(key="control_cc_share_pct", label="Control share (cc tokens)", kind="pct"),
        ],
        rows=rows,
        notes=[
            "A wait of exactly 5 minutes falls in the 5 to 15 minute row, not the 1 to 5 minute one. "
            "The \"All replies\" and \"All cache writes\" columns show the same rows over every reply, "
            "by count and by tokens. Compare a rebuild share with the matching all-replies share: "
            "counts with counts, tokens with tokens.",
        ],
    )


def _preceding_tool_table(
    all_turns: list[Turn], recache_turns: list[Turn], total_cc_recache: int, total_cc_all: int, total_priced: int
) -> Table:
    total_recache = len(recache_turns)
    tools = sorted({t.preceding_tool for t in all_turns} | {t.preceding_tool for t in recache_turns})
    stats_by_tool = _weighted_bucket_stats(
        tools, lambda t: t.preceding_tool, all_turns, recache_turns, total_recache, total_priced, total_cc_recache, total_cc_all
    )
    rows = [[tool, *stats_by_tool[tool]] for tool in tools]
    # Fix recache/deterministic-order: tie-break on the row key string
    # (row[0]) so tied rows (e.g. all-zero cc_tokens) sort the same way
    # every run, not by whatever order a set comprehension upstream
    # happened to iterate in.
    rows.sort(key=lambda row: (row[5], row[0]), reverse=True)  # cc_tokens
    return Table(
        name="recache_preceding_tool",
        title="Re-cache by preceding tool",
        columns=[
            Column(key="preceding_tool", label="Preceding tool", kind="str"),
            Column(key="turns", label="Re-cache turns", kind="int"),
            Column(key="share_pct_turns", label="Share of re-cache turns", kind="pct"),
            Column(key="control_turns", label="All priced turns (control)", kind="int"),
            Column(key="control_share_pct_turns", label="Control share (turns)", kind="pct"),
            Column(key="cc_tokens", label="Cache-creation tokens", kind="tokens"),
            Column(key="cc_share_pct", label="Share of re-cache cc tokens", kind="pct"),
            Column(key="control_cc_tokens", label="Cache-creation tokens (control)", kind="tokens"),
            Column(key="control_cc_share_pct", label="Control share (cc tokens)", kind="pct"),
        ],
        rows=rows,
        notes=[
            "The tool is Bash if the previous reply used it, else PowerShell, else the previous reply's "
            "first tool, else none. As in the wait table, compare counts with counts and tokens with "
            "tokens.",
        ],
    )


def _top_command_prefix_table(recache_turns: list[Turn], limit: int = 12) -> Table:
    cc_by_prefix: dict[str, int] = {}
    turns_by_prefix: dict[str, int] = {}
    for t in recache_turns:
        prefix = t.preceding_cmd_prefix
        if not prefix:
            continue
        cc_by_prefix[prefix] = cc_by_prefix.get(prefix, 0) + t.cache_creation_tokens
        turns_by_prefix[prefix] = turns_by_prefix.get(prefix, 0) + 1
    # Fix recache/deterministic-order: tie-break on the prefix string
    # itself so tied cc_tokens totals sort deterministically.
    top = sorted(cc_by_prefix.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)[:limit]
    rows = [[prefix, turns_by_prefix[prefix], cc] for prefix, cc in top]
    return Table(
        name="recache_top_command_prefixes",
        title="Top preceding command prefixes (re-cache)",
        columns=[
            Column(key="preceding_cmd_prefix", label="Preceding command prefix", kind="str"),
            Column(key="turns", label="Turns", kind="int"),
            Column(key="cc_tokens", label="Cache-creation tokens", kind="tokens"),
        ],
        rows=rows,
        notes=[
            f"Top {limit} by cache-creation tokens among re-cache turns "
            "whose previous turn's Bash/PowerShell command left a "
            "recorded (<=40-char, path-redacted) prefix.",
        ],
    )


def _primary_cause_table(
    all_turns: list[Turn],
    recache_turns: list[Turn],
    recache_records: list[_Record],
    total_recache: int,
    total_priced: int,
    total_cc_recache: int,
    total_cc_all: int,
) -> Table:
    cost_by_primary: dict[EventKind, float] = {}
    for r in recache_records:
        cost_by_primary[r.turn.preceding_primary] = cost_by_primary.get(r.turn.preceding_primary, 0.0) + r.avoidable_cost

    primaries = {t.preceding_primary for t in all_turns} | {t.preceding_primary for t in recache_turns}
    stats_by_primary = _weighted_bucket_stats(
        primaries, lambda t: t.preceding_primary, all_turns, recache_turns, total_recache, total_priced, total_cc_recache, total_cc_all
    )

    rows = []
    for primary in primaries:
        turns_n, share_turns, ctrl_n, ctrl_share_turns, cc, cc_share, ctrl_cc, ctrl_cc_share = stats_by_primary[primary]
        rows.append(
            [
                primary.value,
                turns_n,
                share_turns,
                ctrl_share_turns,
                share_turns - ctrl_share_turns,
                cc,
                cc_share,
                ctrl_cc,
                ctrl_cc_share,
                cc_share - ctrl_cc_share,
                round(cost_by_primary.get(primary, 0.0), 6),
            ]
        )
    # Fix recache/deterministic-order: tie-break on the row key string
    # (row[0], preceding_primary.value) -- primaries above is built from a
    # set union, whose iteration order isn't guaranteed stable across runs.
    rows.sort(key=lambda row: (row[5], row[0]), reverse=True)  # cc_tokens
    return Table(
        name="recache_primary_cause",
        title="Re-cache primary cause",
        columns=[
            Column(key="preceding_primary", label="Preceding primary", kind="str"),
            Column(key="turns", label="Turns", kind="int"),
            Column(key="share_pct_turns", label="Share of re-cache turns", kind="pct"),
            Column(key="control_share_pct_turns", label="Control share (all priced turns)", kind="pct"),
            Column(key="over_representation_points_turns", label="Over-representation (turns)", kind="float"),
            Column(key="cc_tokens", label="Cache-creation tokens", kind="tokens"),
            Column(key="cc_share_pct", label="Share of re-cache cc tokens", kind="pct"),
            Column(key="control_cc_tokens", label="Cache-creation tokens (control)", kind="tokens"),
            Column(key="control_cc_share_pct", label="Control share (cc tokens)", kind="pct"),
            Column(key="over_representation_points_tokens", label="Over-representation (tokens)", kind="float"),
            Column(key="avoidable_cost_usd", label="Avoidable cost", kind="money"),
        ],
        rows=rows,
        notes=[
            "Each rebuild is put down to the one event that ranks highest among those since the "
            "previous reply. Shares are shown by count of replies and by tokens. A cause behind a few "
            "very large rebuilds looks small by count and large by tokens.",
        ],
    )


def _primary_cause_prefix_invalidated_table(
    all_turns: list[Turn],
    prefix_invalidated_turns: list[Turn],
    total_priced: int,
    total_cc_all: int,
) -> Table:
    """Fix item 4: the same primary-cause breakdown as
    :func:`_primary_cause_table`, restricted to prefix-invalidated
    re-cache turns only (full-expiry turns excluded — their cache had
    already fully expired regardless of what preceded them, so any
    "cause" attribution is noise). Denominators (``share_pct_turns``,
    ``cc_share_pct``) are against this restricted population, not the
    full re-cache population, so shares sum to 100% within this table.
    """
    total_pi = len(prefix_invalidated_turns)
    total_cc_pi = sum(t.cache_creation_tokens for t in prefix_invalidated_turns)
    primaries = {t.preceding_primary for t in all_turns} | {t.preceding_primary for t in prefix_invalidated_turns}
    stats_by_primary = _weighted_bucket_stats(
        primaries, lambda t: t.preceding_primary, all_turns, prefix_invalidated_turns, total_pi, total_priced, total_cc_pi, total_cc_all
    )

    rows = []
    for primary in primaries:
        turns_n, share_turns, ctrl_n, ctrl_share_turns, cc, cc_share, ctrl_cc, ctrl_cc_share = stats_by_primary[primary]
        rows.append(
            [
                primary.value,
                turns_n,
                share_turns,
                ctrl_share_turns,
                cc,
                cc_share,
                ctrl_cc,
                ctrl_cc_share,
                cc_share - ctrl_cc_share,
            ]
        )
    # Fix recache/deterministic-order: tie-break on the row key string
    # (row[0]), same reasoning as _primary_cause_table above.
    rows.sort(key=lambda row: (row[4], row[0]), reverse=True)  # cc_tokens
    return Table(
        name="recache_primary_cause_prefix_invalidated",
        title="Re-cache primary cause (prefix-invalidated only)",
        columns=[
            Column(key="preceding_primary", label="Preceding primary", kind="str"),
            Column(key="turns", label="Turns", kind="int"),
            Column(key="share_pct_turns", label="Share of prefix-invalidated turns", kind="pct"),
            Column(key="control_share_pct_turns", label="Control share (all priced turns)", kind="pct"),
            Column(key="cc_tokens", label="Cache-creation tokens", kind="tokens"),
            Column(key="cc_share_pct", label="Share of prefix-invalidated cc tokens", kind="pct"),
            Column(key="control_cc_tokens", label="Cache-creation tokens (control)", kind="tokens"),
            Column(key="control_cc_share_pct", label="Control share (cc tokens)", kind="pct"),
            Column(key="over_representation_points_tokens", label="Over-representation (tokens)", kind="float"),
        ],
        rows=rows,
        notes=[
            "Only rebuilds where the cache was broken by a change. Expired caches are left out: they "
            "had run out whatever came before them, as in the Claude Code notes table. The comparison "
            "is still every reply, so this asks what comes before a broken cache compared with an "
            "ordinary reply, not compared with any rebuild.",
        ],
    )


def _cooccurrence_table(
    all_turns: list[Turn], recache_turns: list[Turn], total_recache: int, total_priced: int
) -> Table:
    kinds = sorted(
        {k for t in all_turns for k in t.preceding_event_kinds} | {k for t in recache_turns for k in t.preceding_event_kinds},
        key=lambda k: k.value,
    )
    rows = []
    for kind in kinds:
        recache_count = sum(1 for t in recache_turns if kind in t.preceding_event_kinds)
        control_count = sum(1 for t in all_turns if kind in t.preceding_event_kinds)
        rows.append(
            [
                kind.value,
                recache_count,
                _pct(recache_count, total_recache),
                control_count,
                _pct(control_count, total_priced),
            ]
        )
    # Fix recache/deterministic-order: tie-break on the row key string
    # (row[0], event kind).
    rows.sort(key=lambda row: (row[1], row[0]), reverse=True)
    return Table(
        name="recache_event_cooccurrence",
        title="Re-cache event co-occurrence",
        columns=[
            Column(key="event_kind", label="Event kind", kind="str"),
            Column(key="recache_turns", label="Re-cache turns containing it", kind="int"),
            Column(key="recache_share_pct", label="Share of re-cache turns", kind="pct"),
            Column(key="control_turns", label="All priced turns containing it (control)", kind="int"),
            Column(key="control_share_pct", label="Control share", kind="pct"),
        ],
        rows=rows,
        notes=[
            "Counts every kind of event since the previous reply, not only "
            "the highest-ranked one: a reply can have several. So a cause "
            "that \"What happened right before each cache rebuild\" hides, "
            "because a higher-ranked event came in the same reply, still "
            "shows here.",
        ],
    )


def _attachment_subsplit_table(recache_turns: list[Turn]) -> Table:
    prefix_invalidated = [t for t in recache_turns if t.recache_signature == "prefix-invalidated"]
    turns_by_type: dict[str, int] = {}
    cc_by_type: dict[str, int] = {}
    for t in prefix_invalidated:
        for atype in set(t.preceding_attachment_types):
            turns_by_type[atype] = turns_by_type.get(atype, 0) + 1
            cc_by_type[atype] = cc_by_type.get(atype, 0) + t.cache_creation_tokens
    # Fix recache/deterministic-order: tie-break on the row key string
    # (row[0], attachment type) -- turns_by_type is built by iterating a
    # per-turn set(), whose iteration order isn't guaranteed stable.
    rows = sorted(
        ([atype, turns_by_type[atype], cc_by_type[atype]] for atype in turns_by_type),
        key=lambda row: (row[2], row[0]),
        reverse=True,
    )
    return Table(
        name="recache_attachment_subsplit",
        title="Prefix-invalidated attachment types",
        columns=[
            Column(key="attachment_type", label="Attachment type", kind="str"),
            Column(key="turns", label="Turns", kind="int"),
            Column(key="cc_tokens", label="Cache-creation tokens", kind="tokens"),
        ],
        rows=rows,
        notes=[
            "Only rebuilds where the cache was broken by a change. Expired caches are left out: they "
            "had run out whatever notes came before them. A reply with several kinds of note counts "
            "once for each.",
        ],
    )


def _by_agent_type_table(records: list[_Record]) -> Table:
    agent_types = sorted({r.agent_type for r in records})
    rows = []
    for agent_type in agent_types:
        agent_all = [r.turn for r in records if r.agent_type == agent_type]
        # Usage-limits addition (see module docstring): limit-expiry
        # turns are excluded from this behavioural recache/cost count
        # (priced_turns above is unaffected -- every priced turn for this
        # agent still counts there).
        agent_recache = [t for t in agent_all if t.is_recache and t.recache_signature != "limit-expiry"]
        cc = sum(t.cache_creation_tokens for t in agent_recache)
        cost = sum(
            r.avoidable_cost
            for r in records
            if r.agent_type == agent_type and r.turn.recache_signature != "limit-expiry"
        )
        rows.append(
            [
                agent_type,
                len(agent_all),
                len(agent_recache),
                _pct(len(agent_recache), len(agent_all)),
                cc,
                round(cost, 6),
            ]
        )
    # Fix recache/deterministic-order: tie-break on the row key string
    # (row[0], agent_type).
    rows.sort(key=lambda row: (row[4], row[0]), reverse=True)
    return Table(
        name="recache_by_agent_type",
        title="Re-cache by agent type",
        columns=[
            Column(key="agent_type", label="Agent type", kind="str"),
            Column(key="priced_turns", label="Priced turns", kind="int"),
            Column(key="recache_turns", label="Re-cache turns", kind="int"),
            Column(key="recache_share_pct", label="Re-cache share", kind="pct"),
            Column(key="cc_tokens", label="Cache-creation tokens (re-cache)", kind="tokens"),
            Column(key="avoidable_cost_usd", label="Avoidable cost", kind="money"),
        ],
        rows=rows,
        notes=[_AGENT_TYPE_NOTE],
    )


def _huge_context_table(all_turns: list[Turn], th: RecacheThresholds, pricing: Pricing) -> Table:
    # D2/D4/COV-12: "huge" used to mean a flat 200k tokens for every
    # turn, whatever model it ran on -- on a natively 1M-context Claude 5
    # model (V24) that is only a fifth of the window, not the
    # near-the-limit signal the metric means to flag. Each turn is now
    # compared to its own resolved model's context_window_tokens; a turn
    # whose model doesn't resolve (or no ``pricing``) falls back to
    # th.huge_ctx, same as every turn got before this fix.
    def _threshold_for(turn: Turn) -> int:
        resolved = pricing.resolve_model(turn.model) if pricing is not None else None
        return resolved.rates.context_window_tokens if resolved is not None else th.huge_ctx

    huge_turns = [t for t in all_turns if t.ctx >= _threshold_for(t)]
    total_cache_read = sum(t.cache_read_tokens for t in all_turns)
    huge_cache_read = sum(t.cache_read_tokens for t in huge_turns)
    return Table(
        name="recache_huge_context",
        title="Huge-context cache-read volume",
        columns=[
            Column(key="metric", label="Metric", kind="str"),
            Column(key="huge_ctx_turns", label="Turns with a very large context", kind="int"),
            Column(key="total_priced_turns", label="Total priced turns", kind="int"),
            Column(key="huge_ctx_cache_read_tokens", label="Cache-read tokens from huge-ctx turns", kind="tokens"),
            Column(key="total_cache_read_tokens", label="Total cache-read tokens", kind="tokens"),
            Column(key="share_pct", label="Share of cache-read volume", kind="pct"),
        ],
        rows=[
            [
                "all",
                len(huge_turns),
                len(all_turns),
                huge_cache_read,
                total_cache_read,
                _pct(huge_cache_read, total_cache_read),
            ]
        ],
        notes=[
            "A context is very large when it reaches its model's own context window. That is 1,000,000 "
            "tokens for a Claude 5 model with a 1M-token window, and 200,000 otherwise. It is "
            f"{th.huge_ctx:,} tokens when the model isn't in pricing.toml. This is about keeping context "
            "small, not a price rise: models from 4.6 on bill the whole 1M-token window at standard rates.",
        ],
    )


__all__ = [
    "GAP_BUCKETS",
    "SIGNATURES",
    "ASSUMPTIONS",
    "RecacheThresholds",
    "detect",
    "apply",
    "gap_bucket",
    "RecacheStats",
    "build_section",
]

"""Context files: how often each CLAUDE.md-family file and each skill is
put in front of the model, by whom, and roughly what carrying it costs.

Fed from the per-file and per-skill records ``events.py`` keeps on
``instructions``, ``nested_memory``, ``skill_listing`` and
``invoked_skills`` attachments (a salted path hash, type, path-scoped
flag and size for a file; name and size for a skill), plus
``Turn.skills_invoked`` and ``Turn.attribution_skill``. Nothing here
holds file text, a path or a skill description: ``claude_md_review``
and the ``/api/skills`` route read those from disk on request and join
on the hash or name.

Cost of carrying text is an estimate: each time a file or listing is
sent, it is written to the prompt cache once, then read from cache on
every later turn of that transcript until it is sent again (after a
conversation summary), plus written again on each turn that rebuilt the
cache (:func:`recache.detect`). Sizes are characters over the characters
per token measured on the corpus's own first calls (:mod:`calibration`,
``chars / 4`` until a model has ten); no tokenizer is run.

Phase 8a adds the project files your agents consume. Besides the files
Claude Code puts in front of the model by itself (``auto``, and ``import``
for one an ``@path`` in a CLAUDE.md pulled in), it follows the files agents
*read* with the Read tool (``read``): per file and per reach, the runs that
read it, how many times and how big, priced as the same carry as any other
text (a write when read, a cache read on each later reply until the next
conversation summary). A *standing read* is one an agent type does again and
again (:func:`is_standing`). Sizes are kept per week too (Monday, UTC), so a
file that grows shows. Only salted path hashes, counts and sizes are kept;
``claude_md_review.local_names`` puts a relative name to a hash on the
dashboard, from disk, when it is asked for. The table lists every such file,
text files first; the Overview check (:func:`check_rows`) covers the text
files only.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Iterable

from . import recache
from .calibration import Calibration
from .model import Column, Event, EventKind, Table, TranscriptResult, Turn
from .pricing import Pricing, effective_rates

#: Key used for the main conversation in the per-reach counts.
MAIN = "main"

#: A read is a standing read of an agent type when the type did it in this
#: many runs, or in this share of its runs (and had at least
#: :data:`STANDING_MIN_TYPE_RUNS` runs, so one run of two is not a habit).
STANDING_MIN_RUNS = 3
STANDING_MIN_SHARE = 0.20
STANDING_MIN_TYPE_RUNS = 3
#: A file smaller than this is not kept in the report (a few lines: not
#: worth a row), and no more than this many are, the dearest first.
MIN_TRACKED_TOKENS = 500
MAX_READ_ROWS = 200
#: The Overview check: a file this big that this many agent types read, or
#: one that grew this much (percent) over about 30 days.
CHECK_MIN_TOKENS = 5000
CHECK_MIN_TYPES = 3
CHECK_GROWTH_PCT = 25.0
#: ...but only a file at least this big now: a small file that doubled is
#: still small.
CHECK_MIN_GROWN_TOKENS = 2000
#: The change over about 30 days compares the newest week with the largest
#: size seen from :data:`GROWTH_WEEKS` to just under :data:`GROWTH_LOOKBACK_WEEKS`
#: weeks before it. Weeks start on a Monday and the newest reply falls
#: anywhere in the newest week, so three weeks back is 21 to 33 days.
GROWTH_WEEKS = 3
GROWTH_LOOKBACK_WEEKS = 7
#: A file not seen in the newest two weeks has no change to report.
IDLE_WEEKS = 2
#: Weeks in a sparkline, and weekly sizes kept per file.
SERIES_WEEKS = 13
WEEKLY_KEPT = 26
#: Fewer days than this and a monthly cost would be a guess (the same floor
#: as ``context_budget.MIN_WINDOW_DAYS``).
MIN_WINDOW_DAYS = 7
#: The sources of a project file, in the words the dashboard uses.
SOURCES = ("auto", "import", "read")
#: The extension classes (``claude_md_review.ext_class``) listed first:
#: prose is what a file can be split, trimmed or moved out of. They are also
#: the only classes the Overview check covers (:func:`check_rows`); code and
#: data files stay in the table, after them.
TEXT_EXTS = ("md", "txt")


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _utc(moment: datetime) -> datetime:
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment.astimezone(timezone.utc)


def week_of(moment: datetime | None) -> str:
    """The Monday (UTC) of the week ``moment`` falls in, as ``YYYY-MM-DD``;
    ``""`` for no time."""
    if moment is None:
        return ""
    day = _utc(moment).date()
    return (day - timedelta(days=day.weekday())).isoformat()


@dataclass(slots=True)
class _FileAcc:
    hash: str
    type: str = "Other"
    scoped: bool = False
    chars: int = 0
    last_seen: str = ""
    #: reach ("main" or an agent type) -> transcripts it was sent in.
    transcripts: dict[str, int] = field(default_factory=dict)
    sends: int = 0
    cost_usd: float = 0.0
    cost_by_reach: dict[str, float] = field(default_factory=dict)
    #: Monday (UTC) -> the largest size sent that week, in characters.
    weekly: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class _ReadAcc:
    """A file agents read with the Read tool, by its path hash."""

    hash: str
    #: reach -> runs that read it / reads made / characters read.
    runs: dict[str, int] = field(default_factory=dict)
    reads: dict[str, int] = field(default_factory=dict)
    chars: dict[str, int] = field(default_factory=dict)
    cost_usd: float = 0.0
    cost_by_reach: dict[str, float] = field(default_factory=dict)
    #: Monday (UTC) -> the largest read that week, in characters.
    weekly: dict[str, int] = field(default_factory=dict)
    last_seen: str = ""


@dataclass(slots=True)
class _SkillAcc:
    name: str
    listing_chars: int = 0
    last_seen: str = ""
    listed: dict[str, int] = field(default_factory=dict)
    listing_cost_usd: float = 0.0
    invoked: int = 0
    invoked_by: dict[str, int] = field(default_factory=dict)
    resent_chars: int = 0
    resent_cost_usd: float = 0.0
    attributed_turns: int = 0
    attributed_cost_usd: float = 0.0


class _Carry:
    """Prices carrying some text from one point in a transcript onward:
    a cache write when sent, a cache read per later turn until
    ``until`` (the next re-send), and a write per cache rebuild.

    ROB-P2 (P10a's handoff): a transcript's ``_Carry`` is built once but
    queried once per note/segment/tag it prices, so :meth:`index_at` and
    :meth:`cost` used to be O(T) each, O(k*T) across the k queries. Both
    are now O(1) after one O(T) pass here in ``__init__``:
    :meth:`index_at` bisects a filtered, known-timestamps array instead
    of scanning (**Assumption:** turn timestamps are non-decreasing in
    transcript order -- true of every real transcript, since turns are
    read off the JSONL file in sequence; ``test_context_files.py``'s
    randomised equivalence test only generates monotonic timestamps,
    matching that), and :meth:`cost` sums two prefix arrays of a
    per-turn write/read rate instead of re-walking ``turns[start:end]``
    and re-resolving a model every call (the resolve step itself is
    cached by model string in :meth:`_resolve`, since the same string
    repeats across almost every turn of a session).

    The per-turn write/read split mirrors the old per-call loop exactly:
    a turn always writes (never reads) when it is the *first* turn of
    the queried range, regardless of ``rebuilt_ids`` -- P10a's warning
    that a naive rebuilt-only prefix sum misses this, since the "first
    turn of the range" is call-relative, not a fixed property of the
    turn. So the write/read total is built from a read-rate baseline
    (``_prefix_read``) plus a correction for turns inside
    ``rebuilt_ids`` (``_prefix_rebuilt_extra``), plus a direct O(1)
    correction for ``start`` itself when it isn't already counted via
    ``rebuilt_ids`` membership."""

    def __init__(
        self, result: TranscriptResult, pricing: Pricing | None, calibration: Calibration | None = None
    ) -> None:
        self.turns = [turn for turn in result.turns if turn.turn_index > 0]
        self.calibration = calibration or Calibration()
        #: The model the text was sent to, for its characters per token.
        self.model = next((turn.model for turn in self.turns if turn.model), None)
        self.times = [_parse_ts(turn.ts) for turn in self.turns]
        rebuilt = recache.detect(self.turns, recache.RecacheThresholds())
        self.rebuilt_ids = {turn.message_id for turn in rebuilt if turn.message_id}
        self.pricing = pricing
        #: When this transcript started, to keep the newest size seen.
        self.stamp = next((turn.ts for turn in self.turns if turn.ts), "")

        self._resolved_cache: dict[str, object] = {}
        # index_at: only the turns with a parseable timestamp, in
        # transcript order (see the monotonicity assumption above).
        self._known_times: list[datetime] = []
        self._known_indices: list[int] = []
        for index, turn_time in enumerate(self.times):
            if turn_time is not None:
                self._known_times.append(turn_time)
                self._known_indices.append(index)

        # cost: a per-turn write and read rate (0.0 where the model
        # can't be priced, matching the old loop's ``continue``), folded
        # into two prefix sums so any [start, end) range sums in O(1).
        n = len(self.turns)
        self._write_rate = [0.0] * n
        self._read_rate = [0.0] * n
        prefix_read = [0.0] * (n + 1)
        prefix_rebuilt_extra = [0.0] * (n + 1)
        for i, turn in enumerate(self.turns):
            rates = self._rates(turn)
            if rates is not None:
                # CAP-2: a turn billed under the 1-hour TTL (subscription
                # billing, mainly) writes at the 1h rate, not 5m -- mirrors
                # ``habits._Rates.write``.
                self._write_rate[i] = rates.cache_write_1h if turn.cc_1h > turn.cc_5m else rates.cache_write_5m
                self._read_rate[i] = rates.cache_read
            prefix_read[i + 1] = prefix_read[i] + self._read_rate[i]
            extra = (self._write_rate[i] - self._read_rate[i]) if turn.message_id in self.rebuilt_ids else 0.0
            prefix_rebuilt_extra[i + 1] = prefix_rebuilt_extra[i] + extra
        self._prefix_read = prefix_read
        self._prefix_rebuilt_extra = prefix_rebuilt_extra

    def _resolve(self, model: str):
        """``self.pricing.resolve_model(model)``, cached by the model
        string: it's a pure string lookup (alias/prefix matching, no
        turn-specific field), and the same string repeats across almost
        every turn of a session."""
        if model not in self._resolved_cache:
            self._resolved_cache[model] = self.pricing.resolve_model(model)
        return self._resolved_cache[model]

    def _rates(self, turn: Turn):
        """The rates the turn was charged at, fast mode included."""
        if self.pricing is None:
            return None
        resolved = self._resolve(turn.model)
        return effective_rates(turn, resolved) if resolved is not None else None

    def index_at(self, ts: str | None) -> int:
        """Index of the first turn at or after ``ts`` (0 when unknown)."""
        moment = _parse_ts(ts)
        if moment is None:
            return 0
        pos = bisect.bisect_left(self._known_times, moment)
        return self._known_indices[pos] if pos < len(self._known_indices) else len(self.turns)

    def cost(self, chars: int, start: int, end: int) -> float:
        """Estimated USD of carrying ``chars`` across turns ``start..end-1``."""
        if chars <= 0 or start >= end:
            return 0.0
        end = min(end, len(self.turns))
        if start >= end:
            return 0.0
        tokens_m = self.calibration.text_tokens(chars, self.model) / 1_000_000
        rate = (self._prefix_read[end] - self._prefix_read[start]) + (
            self._prefix_rebuilt_extra[end] - self._prefix_rebuilt_extra[start]
        )
        if self.turns[start].message_id not in self.rebuilt_ids:
            rate += self._write_rate[start] - self._read_rate[start]
        return tokens_m * rate


def _inject_events(result: TranscriptResult, subkinds: Iterable[str]) -> list[Event]:
    wanted = set(subkinds)
    return [event for event in result.events if event.kind == EventKind.CONTEXT_INJECT and event.subkind in wanted]


@dataclass(slots=True)
class ContextFileStats:
    """Corpus-wide accumulator: :meth:`add` once per transcript."""

    files: dict[str, _FileAcc] = field(default_factory=dict)
    skills: dict[str, _SkillAcc] = field(default_factory=dict)
    #: Files read with the Read tool, by path hash.
    reads: dict[str, _ReadAcc] = field(default_factory=dict)
    #: reach -> transcripts seen (main sessions, spawns per agent type).
    transcripts: dict[str, int] = field(default_factory=dict)
    #: Characters per token by model family (``report.py`` sets it before
    #: any transcript is added).
    calibration: Calibration = field(default_factory=Calibration)
    #: The first and last turn time of any transcript added, for the span
    #: a monthly cost is scaled from and for "now" in a size change.
    first_time: datetime | None = None
    last_time: datetime | None = None

    def add(self, result: TranscriptResult, pricing: Pricing | None = None, *, is_main: bool) -> None:
        reach = MAIN if is_main else (result.meta.agent_type or "(unknown)")
        self.transcripts[reach] = self.transcripts.get(reach, 0) + 1
        carry = _Carry(result, pricing, self.calibration)
        self._note_span(carry)
        self._add_files(result, carry, reach)
        self._add_reads(result, carry, reach)
        self._add_skills(result, carry, reach)

    def _note_span(self, carry: _Carry) -> None:
        if not carry._known_times:
            return
        first, last = _utc(carry._known_times[0]), _utc(carry._known_times[-1])
        if self.first_time is None or first < self.first_time:
            self.first_time = first
        if self.last_time is None or last > self.last_time:
            self.last_time = last

    def window_days(self) -> float:
        """Days between the first and last turn seen, at least
        :data:`MIN_WINDOW_DAYS`."""
        if self.first_time is None or self.last_time is None:
            return float(MIN_WINDOW_DAYS)
        return max(float(MIN_WINDOW_DAYS), round((self.last_time - self.first_time).total_seconds() / 86400, 1))

    def _add_files(self, result: TranscriptResult, carry: _Carry, reach: str) -> None:
        sends: list[tuple[int, dict, str]] = []
        for event in _inject_events(result, ("instructions", "nested_memory")):
            start = carry.index_at(event.ts)
            week = week_of(_parse_ts(event.ts) or _parse_ts(carry.stamp))
            for record in event.detail.get("files") or ():
                if isinstance(record, dict) and record.get("hash"):
                    sends.append((start, record, week))
        seen_here: set[str] = set()
        for position, (start, record, week) in enumerate(sends):
            file_hash = record["hash"]
            later = [s for s, r, _w in sends[position + 1 :] if r["hash"] == file_hash]
            end = later[0] if later else len(carry.turns)
            acc = self.files.setdefault(file_hash, _FileAcc(hash=file_hash))
            acc.type = record.get("type") or acc.type
            acc.scoped = bool(record.get("scoped"))
            ts = carry.stamp
            if ts >= acc.last_seen:
                acc.last_seen = ts
                acc.chars = int(record.get("chars") or 0)
            acc.sends += 1
            cost = carry.cost(int(record.get("chars") or 0), start, end)
            acc.cost_usd += cost
            acc.cost_by_reach[reach] = acc.cost_by_reach.get(reach, 0.0) + cost
            if week:
                acc.weekly[week] = max(acc.weekly.get(week, 0), int(record.get("chars") or 0))
            if file_hash not in seen_here:
                seen_here.add(file_hash)
                acc.transcripts[reach] = acc.transcripts.get(reach, 0) + 1

    def _add_reads(self, result: TranscriptResult, carry: _Carry, reach: str) -> None:
        """Files read with the Read tool. A read's result is in context
        from the next reply on, until a conversation summary drops it, so
        it is priced as the same carry as an instructions file. A read
        that failed (0 characters) put nothing in context and is left out."""
        turns = carry.turns
        boundaries = sorted(
            carry.index_at(event.ts)
            for event in result.events
            if event.kind == EventKind.COMPACT_BOUNDARY and event.ts
        )
        seen_here: set[str] = set()
        for index, turn in enumerate(turns):
            if not turn.read_target_hashes:
                continue
            pos = bisect.bisect_right(boundaries, index)
            end = boundaries[pos] if pos < len(boundaries) else len(turns)
            week = week_of(carry.times[index])
            for position, file_hash in enumerate(turn.read_target_hashes):
                chars = turn.read_target_chars[position] if position < len(turn.read_target_chars) else 0
                if not file_hash or chars <= 0:
                    continue
                acc = self.reads.setdefault(file_hash, _ReadAcc(hash=file_hash))
                acc.reads[reach] = acc.reads.get(reach, 0) + 1
                acc.chars[reach] = acc.chars.get(reach, 0) + chars
                cost = carry.cost(chars, index + 1, end)
                acc.cost_usd += cost
                acc.cost_by_reach[reach] = acc.cost_by_reach.get(reach, 0.0) + cost
                if week:
                    acc.weekly[week] = max(acc.weekly.get(week, 0), chars)
                if carry.stamp >= acc.last_seen:
                    acc.last_seen = carry.stamp
                if file_hash not in seen_here:
                    seen_here.add(file_hash)
                    acc.runs[reach] = acc.runs.get(reach, 0) + 1

    def _add_skills(self, result: TranscriptResult, carry: _Carry, reach: str) -> None:
        # A later listing may add only new skills, so each skill is carried
        # until the next listing that names it again, not the next listing.
        sends: list[tuple[int, str, int]] = []
        for event in _inject_events(result, ("skill_listing",)):
            start = carry.index_at(event.ts)
            for record in event.detail.get("skills") or ():
                name = record.get("name") if isinstance(record, dict) else None
                if name:
                    sends.append((start, name, int(record.get("chars") or 0)))
        listed_here: set[str] = set()
        for position, (start, name, chars) in enumerate(sends):
            later = [s for s, n, _c in sends[position + 1 :] if n == name]
            end = later[0] if later else len(carry.turns)
            acc = self.skills.setdefault(name, _SkillAcc(name=name))
            if carry.stamp >= acc.last_seen:
                acc.last_seen = carry.stamp
                acc.listing_chars = chars
            acc.listing_cost_usd += carry.cost(chars, start, end)
            if name not in listed_here:
                listed_here.add(name)
                acc.listed[reach] = acc.listed.get(reach, 0) + 1
        for event in _inject_events(result, ("invoked_skills",)):
            start = carry.index_at(event.ts)
            for record in event.detail.get("skills") or ():
                name = record.get("name") if isinstance(record, dict) else None
                if not name:
                    continue
                acc = self.skills.setdefault(name, _SkillAcc(name=name))
                chars = int(record.get("chars") or 0)
                acc.resent_chars += chars
                acc.resent_cost_usd += carry.cost(chars, start, len(carry.turns))
        for turn in carry.turns:
            for name in turn.skills_invoked:
                acc = self.skills.setdefault(name, _SkillAcc(name=name))
                acc.invoked += 1
                acc.invoked_by[reach] = acc.invoked_by.get(reach, 0) + 1
            if turn.attribution_skill:
                acc = self.skills.setdefault(turn.attribution_skill, _SkillAcc(name=turn.attribution_skill))
                acc.attributed_turns += 1
                rates = carry._rates(turn)
                if rates is not None:
                    acc.attributed_cost_usd += (
                        turn.input_tokens * rates.input
                        + turn.output_tokens * rates.output
                        + turn.cache_creation_tokens * rates.cache_write_5m
                        + turn.cache_read_tokens * rates.cache_read
                    ) / 1_000_000

    def _weekly_tokens(self, weekly: dict[str, int]) -> dict[str, int]:
        """A file's weekly sizes in tokens, the newest :data:`WEEKLY_KEPT` weeks."""
        return {week: round(self.calibration.text_tokens(weekly[week])) for week in sorted(weekly)[-WEEKLY_KEPT:]}

    def _reads_summary(self) -> tuple[list[dict], dict[str, dict]]:
        """The standing reads worth a row, and what each reach reads
        by habit. A standing read is :func:`is_standing` for at least one
        reach; a row is kept when its average read is at least
        :data:`MIN_TRACKED_TOKENS`, the dearest :data:`MAX_READ_ROWS` of them.
        The per-reach totals count every standing read, small or not."""
        tokens = self.calibration.text_tokens
        habit_tokens: dict[str, float] = {}
        habit_files: dict[str, int] = {}
        rows: list[dict] = []
        for acc in self.reads.values():
            habitual = [reach for reach, runs in acc.runs.items() if is_standing(runs, self.transcripts.get(reach, 0))]
            if not habitual:
                continue
            for reach in habitual:
                habit_tokens[reach] = habit_tokens.get(reach, 0.0) + tokens(acc.chars.get(reach, 0))
                habit_files[reach] = habit_files.get(reach, 0) + 1
            reads = sum(acc.reads.values())
            mean = round(tokens(sum(acc.chars.values()) / reads)) if reads else 0
            if mean < MIN_TRACKED_TOKENS:
                continue
            rows.append(
                {
                    "hash": acc.hash,
                    "source": "read",
                    "tokens": mean,
                    "reach": dict(sorted(acc.runs.items())),
                    "reads": dict(sorted(acc.reads.items())),
                    "read_tokens": {reach: round(tokens(chars)) for reach, chars in sorted(acc.chars.items())},
                    "cost_usd": round(acc.cost_usd, 6),
                    "cost_by_reach": {reach: round(cost, 6) for reach, cost in sorted(acc.cost_by_reach.items())},
                    "weekly": self._weekly_tokens(acc.weekly),
                    "last_seen": acc.last_seen,
                }
            )
        rows.sort(key=lambda row: (-row["cost_usd"], row["hash"]))
        standing = {
            reach: {
                "runs": self.transcripts.get(reach, 0),
                "files": habit_files[reach],
                "tokens_per_run": round(habit_tokens[reach] / self.transcripts[reach], 1)
                if self.transcripts.get(reach)
                else 0.0,
            }
            for reach in sorted(habit_tokens)
        }
        return rows[:MAX_READ_ROWS], standing

    def to_dict(self) -> dict:
        """The JSON-ready summary ``ReportModel.context_files`` carries.

        ``files`` are the files Claude Code loaded by itself (their rows
        feed the CLAUDE.md prices, so a file that was only read is never
        among them); ``reads`` are the standing reads, in the same row
        shape (:meth:`_reads_summary`)."""
        files = [
            {
                "hash": acc.hash,
                "type": acc.type,
                "scoped": acc.scoped,
                "tokens": round(self.calibration.text_tokens(acc.chars)),
                "sends": acc.sends,
                "reach": dict(sorted(acc.transcripts.items())),
                "cost_usd": round(acc.cost_usd, 6),
                "cost_by_reach": {reach: round(cost, 6) for reach, cost in sorted(acc.cost_by_reach.items())},
                "weekly": self._weekly_tokens(acc.weekly),
                "last_seen": acc.last_seen,
            }
            for acc in sorted(self.files.values(), key=lambda a: -a.cost_usd)
        ]
        skills = [
            {
                "name": acc.name,
                "listing_tokens": round(self.calibration.text_tokens(acc.listing_chars)),
                "listed": dict(sorted(acc.listed.items())),
                "listing_cost_usd": round(acc.listing_cost_usd, 6),
                "invoked": acc.invoked,
                "invoked_by": dict(sorted(acc.invoked_by.items())),
                "resent_tokens": round(self.calibration.text_tokens(acc.resent_chars)),
                "resent_cost_usd": round(acc.resent_cost_usd, 6),
                "attributed_turns": acc.attributed_turns,
                "attributed_cost_usd": round(acc.attributed_cost_usd, 6),
                "last_seen": acc.last_seen,
            }
            for acc in sorted(self.skills.values(), key=lambda a: a.name)
        ]
        reads, standing = self._reads_summary()
        return {
            "transcripts": dict(sorted(self.transcripts.items())),
            "files": files,
            "skills": skills,
            "reads": reads,
            "standing": standing,
            "window_days": self.window_days(),
            "newest": self.last_time.date().isoformat() if self.last_time else "",
        }


# -- standing reads, sizes over time, and the unified file view ----------------------------------


def is_standing(runs: int, type_runs: int) -> bool:
    """Whether doing something in ``runs`` of a reach's ``type_runs`` runs
    is a habit: in :data:`STANDING_MIN_RUNS` of them, or in
    :data:`STANDING_MIN_SHARE` of them. A reach with fewer than
    :data:`STANDING_MIN_TYPE_RUNS` runs has no habits yet."""
    if type_runs < STANDING_MIN_TYPE_RUNS or runs <= 0:
        return False
    return runs >= STANDING_MIN_RUNS or runs / type_runs >= STANDING_MIN_SHARE


def monday_of(day: str) -> date | None:
    """The Monday of the week an ISO date falls in."""
    try:
        parsed = date.fromisoformat(day[:10])
    except (TypeError, ValueError):
        return None
    return parsed - timedelta(days=parsed.weekday())


def _points(weekly: dict[str, int]) -> dict[date, int]:
    points: dict[date, int] = {}
    for week, tokens in (weekly or {}).items():
        monday = monday_of(week)
        if monday is not None:
            points[monday] = tokens
    return points


def weekly_series(weekly: dict[str, int], newest: str) -> list[int]:
    """Tokens per week, one value for each of the last :data:`SERIES_WEEKS`
    weeks up to the newest, from the file's first week. A week the file was
    not seen carries the size before it. Empty with no sizes."""
    current = monday_of(newest)
    points = _points(weekly)
    if not points or current is None:
        return []
    first = max(min(points), current - timedelta(weeks=SERIES_WEEKS - 1))
    before = [monday for monday in points if monday < first]
    carried = points[max(before)] if before else None
    out: list[int] = []
    day = first
    while day <= current:
        if day in points:
            carried = points[day]
        if carried is not None:
            out.append(carried)
        day += timedelta(weeks=1)
    return out


def size_change(weekly: dict[str, int], newest: str) -> dict:
    """The size now, and how it changed over about 30 days: ``{"now",
    "then", "change_pct"}``. ``now`` is the newest week's size. ``then`` is
    the largest size seen from :data:`GROWTH_LOOKBACK_WEEKS` to
    :data:`GROWTH_WEEKS` weeks before the newest week (the largest, because
    a read of part of a file shows only that part). ``then`` and the change
    are ``None`` with no such week, or for a file not seen in the newest
    :data:`IDLE_WEEKS` weeks."""
    current = monday_of(newest)
    points = _points(weekly)
    if not points:
        return {"now": 0, "then": None, "change_pct": None}
    last = max(points)
    now = points[last]
    if current is None or (current - last).days >= 7 * IDLE_WEEKS:
        return {"now": now, "then": None, "change_pct": None}
    older = [
        tokens
        for monday, tokens in points.items()
        if current - timedelta(weeks=GROWTH_LOOKBACK_WEEKS) < monday <= current - timedelta(weeks=GROWTH_WEEKS)
    ]
    then = max(older) if older else None
    change = round((now / then - 1) * 100, 1) if then else None
    return {"now": now, "then": then, "change_pct": change}


def _reach_rows(
    reach: dict[str, int],
    transcripts: dict[str, int],
    reads: dict[str, int] | None = None,
    read_tokens: dict[str, int] | None = None,
) -> list[dict]:
    """Who has the file, each with its runs, its share of that reach's runs,
    whether that is a habit, and the mean size of one read (``None`` when
    that reach only had it loaded)."""
    rows = [
        {
            "reach": name,
            "runs": runs,
            "share": round(min(1.0, runs / transcripts[name]), 3) if transcripts.get(name) else 0.0,
            "standing": is_standing(runs, transcripts.get(name, 0)),
            "mean_tokens": round(read_tokens[name] / reads[name]) if (reads or {}).get(name) and name in (read_tokens or {}) else None,
        }
        for name, runs in reach.items()
        if runs > 0
    ]
    return sorted(rows, key=lambda row: (row["reach"] != MAIN, -row["share"], row["reach"]))


def _merge_reach(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
    return {name: max(left.get(name, 0), right.get(name, 0)) for name in {*left, *right}}


def project_files(data: dict, local: dict | None = None) -> list[dict]:
    """Every project file the agents consume, as one list: the files
    Claude Code loads by itself (source ``auto``, or ``import`` when an
    ``@path`` in a CLAUDE.md pulls it in) and the standing reads (source
    ``read``), each with its size now and change over about 30 days, a
    weekly series, who reads it and in what share of their runs, and what
    it costs a month. The managed policy CLAUDE.md is left out: it cannot
    be changed.

    ``local`` is what :func:`claude_md_review.local_names` found on disk,
    as a dict: ``names`` (hash -> ``name``, ``ext``, ``project``),
    ``imports`` (hashes some CLAUDE.md pulls in with ``@path``) and
    ``inlined`` (hash -> ``parent`` hash and ``tokens``, for an import whose
    own entry the transcripts do not have, so its row is worked out from the
    file that imports it). With it each row also carries ``name``,
    ``ext`` and ``project``. Without it a row has only a hash.

    **Assumption:** an ``@import``ed file shows up as its own entry in the
    ``instructions`` attachment, so it is a row of its own with its own
    size and cost. No CLAUDE.md on the machine this was written on imports
    anything, so that could not be checked. If Claude Code inlines it into
    the importing file instead, the file has no entry of its own: its row
    is then taken from disk (its size) and from the importing file (who
    gets it, and a share of what that file costs by size)."""
    local = local or {}
    names = local.get("names") or {}
    imports = set(local.get("imports") or ())
    inlined = local.get("inlined") or {}
    transcripts = data.get("transcripts") or {}
    newest = data.get("newest") or ""
    days = float(data.get("window_days") or MIN_WINDOW_DAYS)

    merged: dict[str, dict] = {}
    for item in data.get("files") or ():
        if not isinstance(item, dict) or not item.get("hash") or item.get("type") == "Managed":
            continue
        merged[item["hash"]] = {
            "hash": item["hash"],
            "source": "import" if item["hash"] in imports else "auto",
            "type": item.get("type") or "",
            "scoped": bool(item.get("scoped")),
            "tokens": int(item.get("tokens") or 0),
            "weekly": dict(item.get("weekly") or {}),
            "reach": dict(item.get("reach") or {}),
            "reads": {},
            "read_tokens": {},
            "cost_usd": float(item.get("cost_usd") or 0.0),
            "last_seen": item.get("last_seen") or "",
        }
    autos = dict(merged)
    for item in data.get("reads") or ():
        if not isinstance(item, dict) or not item.get("hash"):
            continue
        known = merged.get(item["hash"])
        if known is not None:
            known["reach"] = _merge_reach(known["reach"], item.get("reach") or {})
            known["reads"] = dict(item.get("reads") or {})
            known["read_tokens"] = dict(item.get("read_tokens") or {})
            known["cost_usd"] += float(item.get("cost_usd") or 0.0)
            continue
        merged[item["hash"]] = {
            "hash": item["hash"],
            "source": "import" if item["hash"] in imports else "read",
            "type": "",
            "scoped": False,
            "tokens": int(item.get("tokens") or 0),
            "weekly": dict(item.get("weekly") or {}),
            "reach": dict(item.get("reach") or {}),
            "reads": dict(item.get("reads") or {}),
            "read_tokens": dict(item.get("read_tokens") or {}),
            "cost_usd": float(item.get("cost_usd") or 0.0),
            "last_seen": item.get("last_seen") or "",
        }
    for file_hash, link in inlined.items():
        parent = autos.get(link.get("parent"))
        if file_hash in merged or parent is None or not parent["tokens"]:
            continue
        tokens = int(link.get("tokens") or 0)
        share = min(1.0, tokens / parent["tokens"])
        merged[file_hash] = {
            "hash": file_hash,
            "source": "import",
            "type": parent["type"],
            "scoped": parent["scoped"],
            "tokens": tokens,
            "weekly": {},
            "reach": dict(parent["reach"]),
            "reads": {},
            "read_tokens": {},
            "cost_usd": parent["cost_usd"] * share,
            "last_seen": parent["last_seen"],
        }

    rows: list[dict] = []
    for row in merged.values():
        change = size_change(row["weekly"], newest)
        size = change["now"] or row["tokens"]
        if size < MIN_TRACKED_TOKENS:
            continue
        reaches = _reach_rows(row.pop("reach"), transcripts, row.pop("reads"), row.pop("read_tokens"))
        row.update(
            {
                "reach": reaches,
                "types": sum(1 for item in reaches if item["standing"] and item["reach"] != MAIN),
                "tokens": size,
                "then": change["then"],
                "change_pct": change["change_pct"],
                "series": weekly_series(row.pop("weekly"), newest),
                "cost_usd": round(row["cost_usd"], 6),
                "cost_month_usd": round(row["cost_usd"] * 30 / days, 6),
            }
        )
        info = names.get(row["hash"]) or {}
        row["name"] = info.get("name") or ""
        row["ext"] = info.get("ext") or ""
        row["project"] = info.get("project") or ""
        rows.append(row)
    rows.sort(key=lambda row: (row["ext"] not in TEXT_EXTS, -row["cost_month_usd"], row["hash"]))
    return rows


def dearest(rows: list[dict], limit: int) -> list[dict]:
    """The ``limit`` rows of :func:`project_files` that cost most a month,
    kept in the list's own order (text files first)."""
    kept = {row["hash"] for row in sorted(rows, key=lambda row: (-row["cost_month_usd"], row["hash"]))[:limit]}
    return [row for row in rows if row["hash"] in kept]


def check_rows(rows: list[dict], *, maybe_imports: bool = False) -> list[dict]:
    """The rows the Overview check flags, dearest first: a file of
    :data:`CHECK_MIN_TOKENS` or more that :data:`CHECK_MIN_TYPES` or more agent
    types read, or one that grew :data:`CHECK_GROWTH_PCT` percent or more
    in about 30 days. A grown file must be at least
    :data:`CHECK_MIN_GROWN_TOKENS` now: a small file that doubled is still
    small. Each flagged row gets a ``reasons`` list (``wide`` and/or
    ``grew``). Only files agents read and files a CLAUDE.md imports are
    flagged: the CLAUDE.md files Claude Code loads by itself are the
    CLAUDE.md check's.

    Only text files are flagged: the check is about the documents sessions
    and agents take in (CLAUDE.md, a context.md, specs, plans, any ``.md``),
    which a split, a trim or a move into a skill helps. A code or data file
    stays in the Agents page's table, after the text files, but never fires
    the check or counts toward it, and neither does a file this machine could
    not name, because its class is not known. A row's class is its ``ext``
    (:data:`TEXT_EXTS`).

    Which loaded file is an import, and what class a file is, are known
    only from disk (:func:`project_files` ``local``). With ``maybe_imports``
    a loaded file counts as one that could be an import and a file of unknown
    class as one that could be text, so a caller can tell cheaply, without
    reading disk, that nothing at all could be flagged."""
    flagged: list[dict] = []
    for row in rows:
        if row["source"] == "auto" and not maybe_imports:
            continue
        ext = row.get("ext") or ""
        if ext not in TEXT_EXTS and not (maybe_imports and not ext):
            continue
        reasons = []
        if row["tokens"] >= CHECK_MIN_TOKENS and row["types"] >= CHECK_MIN_TYPES:
            reasons.append("wide")
        if (
            row["change_pct"] is not None
            and row["change_pct"] >= CHECK_GROWTH_PCT
            and row["tokens"] >= CHECK_MIN_GROWN_TOKENS
        ):
            reasons.append("grew")
        if reasons:
            flagged.append({**row, "reasons": reasons})
    flagged.sort(key=lambda row: (-row["cost_month_usd"], row["hash"]))
    return flagged


# -- the Agents page's starting-context stack --------------------------------------------------


def build_stack_table(data: dict, startup: Table | None) -> Table | None:
    """What each agent type carries into a run, in the order it is stacked:
    the system prompt and tools (everything in the first call but the
    next two), the CLAUDE.md files Claude Code loads, the files it reads
    by habit (:func:`is_standing`, tokens per run), then the brief it is
    given. ``startup`` is the ``agent_startup_breakdown`` table the first
    call's sizes come from."""
    if startup is None or not startup.rows:
        return None
    keys = [column.key for column in startup.columns]
    standing = data.get("standing") or {}
    rows: list[list] = []
    for source in startup.rows:
        row = dict(zip(keys, source))
        startup_tokens = float(row.get("startup_tokens") or 0.0)
        files = float(row.get("claude_md") or 0.0)
        brief = float(row.get("task_prompt") or 0.0)
        system = max(0.0, startup_tokens - files - brief)
        habit = standing.get(row["agent_type"]) or {}
        reads = float(habit.get("tokens_per_run") or 0.0)
        rows.append(
            [row["agent_type"], row.get("spawns") or 0, system, files, reads, brief, system + files + reads + brief,
             int(habit.get("files") or 0)]
        )
    return Table(
        name="agent_startup_stack",
        title="What each agent type carries into a run",
        columns=[
            Column(key="agent_type", label="Agent type", kind="str"),
            Column(key="spawns", label="Spawns", kind="int"),
            Column(key="system_tools", label="System and tools", kind="tokens"),
            Column(key="auto_files", label="Files loaded for it", kind="tokens"),
            Column(key="standing_reads", label="Files it reads by habit", kind="tokens"),
            Column(key="brief", label="The brief", kind="tokens"),
            Column(key="total", label="Total", kind="tokens"),
            Column(key="standing_files", label="Files read by habit", kind="int"),
        ],
        rows=rows,
        notes=[
            "System and tools is the first call's whole input less the next two columns. It holds the system "
            "prompt, tool definitions, skills list, hook output and environment notes.",
            "Files it reads by habit are files this agent type read in "
            f"{STANDING_MIN_RUNS} or more runs, or in {round(STANDING_MIN_SHARE * 100)}% or more of its runs. "
            "They are shown as tokens per run. They arrive after the first call and are read again on every reply after.",
        ],
    )


__all__ = [
    "CHECK_GROWTH_PCT",
    "CHECK_MIN_GROWN_TOKENS",
    "CHECK_MIN_TOKENS",
    "CHECK_MIN_TYPES",
    "MAIN",
    "MAX_READ_ROWS",
    "MIN_TRACKED_TOKENS",
    "SOURCES",
    "TEXT_EXTS",
    "ContextFileStats",
    "build_stack_table",
    "check_rows",
    "dearest",
    "is_standing",
    "monday_of",
    "project_files",
    "size_change",
    "week_of",
    "weekly_series",
]

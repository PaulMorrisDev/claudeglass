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
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

from . import recache
from .calibration import Calibration
from .model import Event, EventKind, TranscriptResult, Turn
from .pricing import Pricing, effective_rates

#: Key used for the main conversation in the per-reach counts.
MAIN = "main"


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


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
    #: reach -> transcripts seen (main sessions, spawns per agent type).
    transcripts: dict[str, int] = field(default_factory=dict)
    #: Characters per token by model family (``report.py`` sets it before
    #: any transcript is added).
    calibration: Calibration = field(default_factory=Calibration)

    def add(self, result: TranscriptResult, pricing: Pricing | None = None, *, is_main: bool) -> None:
        reach = MAIN if is_main else (result.meta.agent_type or "(unknown)")
        self.transcripts[reach] = self.transcripts.get(reach, 0) + 1
        carry = _Carry(result, pricing, self.calibration)
        self._add_files(result, carry, reach)
        self._add_skills(result, carry, reach)

    def _add_files(self, result: TranscriptResult, carry: _Carry, reach: str) -> None:
        sends: list[tuple[int, dict]] = []
        for event in _inject_events(result, ("instructions", "nested_memory")):
            start = carry.index_at(event.ts)
            for record in event.detail.get("files") or ():
                if isinstance(record, dict) and record.get("hash"):
                    sends.append((start, record))
        seen_here: set[str] = set()
        for position, (start, record) in enumerate(sends):
            file_hash = record["hash"]
            later = [s for s, r in sends[position + 1 :] if r["hash"] == file_hash]
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
            if file_hash not in seen_here:
                seen_here.add(file_hash)
                acc.transcripts[reach] = acc.transcripts.get(reach, 0) + 1

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

    def to_dict(self) -> dict:
        """The JSON-ready summary ``ReportModel.context_files`` carries."""
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
        return {"transcripts": dict(sorted(self.transcripts.items())), "files": files, "skills": skills}


__all__ = ["MAIN", "ContextFileStats"]

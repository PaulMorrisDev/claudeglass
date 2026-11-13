"""Checks that read the answers metrics capture collects, and switch on
what the answers are good for only once they prove themselves.

**The summary question** (``lost``). The session note after a compaction
asks Claude to say, in its next final reply, whether the summary just made
left out something the work needed (``capture_catalogue.Metric
.compact_line``). :func:`summary_answers` finds the cycle that answers each
summary and whether the transcript backs the answer with evidence;
:func:`lost_check` decides, from the counts alone, whether the answers are
worth showing. After a summary about half of the answering cycles read a
file again or are corrected whatever Claude answered, so the check needs
the ``yes`` answers to show the evidence clearly more often than the ``no``
answers do. No person switches it.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from . import capture as capture_mod
from . import compaction
from .model import EventKind, TranscriptResult, Turn

#: ``yes`` answers and ``no`` answers it takes before the check can pass.
LOST_MIN_ANSWERS = 10
#: Of the ``yes`` answers, the least share that show the evidence.
LOST_MIN_YES_EVIDENCE = Fraction(1, 2)
#: How much more often the ``yes`` answers show it than the ``no`` answers.
LOST_MIN_MARGIN = Fraction(1, 5)


@dataclass(frozen=True, slots=True)
class SummaryAnswer:
    """The cycle that answers one summary (or more, when two came in one
    cycle): the ``lost`` word its tag settled on (``None`` when it gave
    none) and whether the transcript shows what ``yes`` claims."""

    lost: str | None
    #: A file the work read or edited after the summary was read or edited
    #: before it, or the work was corrected.
    evidence: bool


@dataclass(frozen=True, slots=True)
class BreakAnswer:
    """The message that came back after a long break: the ``prior`` word its
    tag settled on (``None`` when it gave none) and whether the work read or
    edited a file touched before the break."""

    prior: str | None
    reused: bool


@dataclass(frozen=True, slots=True)
class LostCheck:
    """The counts :func:`lost_check` decides on: the answers by word and,
    of each, those with evidence."""

    yes: int
    yes_evidence: int
    no: int
    no_evidence: int
    passed: bool


def _touched(turns) -> set[str]:
    """The salted hashes of the files these turns read or edited."""
    out: set[str] = set()
    for turn in turns:
        out.update(turn.read_target_hashes)
        out.update(turn.edit_target_hashes)
    return out


def _corrected(cycles, n: int) -> bool:
    """Whether cycle ``n`` was corrected: a message you typed while it ran
    said so, or the next message did (or Claude settled it as a fix or a
    redo)."""
    if any(turn.queued_correction for turn in cycles[n].turns):
        return True
    if n + 1 >= len(cycles):
        return False
    following = cycles[n + 1]
    settled = following.settled
    return bool(following.turns[0].human_correction or (settled is not None and settled.shift in ("fix", "redo")))


def summary_answers(top: TranscriptResult, cycles) -> list[SummaryAnswer]:
    """One :class:`SummaryAnswer` for each prompt cycle (``cycles``, from
    ``capture.prompt_cycles(top)``) that holds the first reply after a
    summary of this transcript. A summary a continued session inherited is
    not this session's, and two summaries in one cycle give one answer. A
    ``lost`` word in any other cycle is not read."""
    replies = capture_mod._priced(top)
    position = {id(turn): n for n, turn in enumerate(replies)}
    cycle_of = {id(turn): n for n, cycle in enumerate(cycles) for turn in cycle.turns}
    priced = compaction._priced_turns(top)
    answered: set[int] = set()
    out: list[SummaryAnswer] = []
    for event, index in compaction._correlate_compactions_to_turn_index(top, priced):
        if event.detail.get("inherited") or index is None:
            continue
        first: Turn = priced[index]
        n = cycle_of.get(id(first))
        if n is None or n in answered:
            continue
        answered.add(n)
        at = position[id(first)]
        cycle = cycles[n]
        after = [turn for turn in cycle.turns if position[id(turn)] >= at]
        reread = bool(_touched(after) & _touched(replies[:at]))
        settled = cycle.settled
        out.append(SummaryAnswer(settled.lost if settled is not None else None, reread or _corrected(cycles, n)))
    return out


def break_answers(top: TranscriptResult, cycles) -> list[BreakAnswer]:
    """One :class:`BreakAnswer` for each prompt cycle (``cycles``, from
    ``capture.prompt_cycles(top)``) that opens a break: a message you typed
    at least :data:`habits.LONG_BREAK_S` seconds after a reply whose context
    and output came to :data:`habits.STALE_TOKENS` or more (the reader
    ``_item_clear_between`` uses), not a wait for a usage limit and not a
    gap across a summary."""
    from . import habits  # habits imports this module

    replies = capture_mod._priced(top)
    position = {id(turn): n for n, turn in enumerate(replies)}
    opening = {id(cycle.turns[0]): cycle for cycle in cycles}
    compacted = {EventKind.COMPACT_BOUNDARY, EventKind.COMPACT_SUMMARY}
    out: list[BreakAnswer] = []
    before: list[Turn] = []
    for turn in replies:
        if turn.is_synthetic or turn.estimated:
            continue
        last = before[-1] if before else None
        cycle = opening.get(id(turn))
        if (
            cycle is not None
            and last is not None
            and not turn.preceding_not_typed
            and turn.gap_cause != "limit"
            and not compacted.intersection(turn.preceding_event_kinds)
        ):
            at, last_at = capture_mod._moment(turn.ts), capture_mod._moment(last.ts)
            if (
                at is not None
                and last_at is not None
                and (at - last_at).total_seconds() >= habits.LONG_BREAK_S
                and last.ctx + last.output_tokens >= habits.STALE_TOKENS
            ):
                reused = bool(_touched(cycle.turns) & _touched(replies[: position[id(turn)]]))
                settled = cycle.settled
                out.append(BreakAnswer(settled.prior if settled is not None else None, reused))
        before.append(turn)
    return out


def lost_check(answers) -> LostCheck:
    """Count ``answers`` (:class:`SummaryAnswer`, one per answering cycle)
    by word and decide whether the question has proved itself: at least
    :data:`LOST_MIN_ANSWERS` of each word, at least
    :data:`LOST_MIN_YES_EVIDENCE` of the ``yes`` answers with evidence, and
    their share :data:`LOST_MIN_MARGIN` above the ``no`` answers'. The
    shares are exact fractions, so a margin of exactly twenty points
    passes."""
    yes = [a for a in answers if a.lost == "yes"]
    no = [a for a in answers if a.lost == "no"]
    yes_evidence = sum(a.evidence for a in yes)
    no_evidence = sum(a.evidence for a in no)
    passed = False
    if len(yes) >= LOST_MIN_ANSWERS and len(no) >= LOST_MIN_ANSWERS:
        yes_share, no_share = Fraction(yes_evidence, len(yes)), Fraction(no_evidence, len(no))
        passed = yes_share >= LOST_MIN_YES_EVIDENCE and yes_share - no_share >= LOST_MIN_MARGIN
    return LostCheck(len(yes), yes_evidence, len(no), no_evidence, passed)

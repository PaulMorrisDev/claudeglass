"""Pieces of work: the stretches of a session that were one job, told apart
without asking you.

A *piece of work* is the run of prompt cycles (``capture.Cycle``) from one
fresh start to the next. It never needs a /cg-feedback answer: with no
rating and no tag at all, :func:`pieces_of` still draws it, from the
transcript's own facts. Counts, ids and closed words only; nothing a
transcript said is kept.

**Where a piece starts.** Only at:

- the session start (high confidence);
- a /clear, unless the message after it is a handoff (high);
- a settled ``shift=new`` on a substantive message (high);
- with no ``shift`` word at all: a gap of :data:`GAP_S` or more, and the
  files the message touches share less than :data:`JACCARD_BELOW` of their
  names with the last ones touched (at least :data:`MIN_FILES` files on
  each side), and a substantive message (low confidence).

Never at a queued message or an AskUserQuestion answer, which open no
cycle, nor at a plan reply, which asks for nothing, nor at an aside (below).
A *substantive* message is one that asks for something: not a go-ahead, a
status check, a thank-you or a reply to a plan Claude had just put up
(unless a message you typed while it ran did ask). A /cg-feedback run is no
piece's work: it stays where it ran and counts for nothing but ``rated``. A
session with no other start is one *unsegmented* piece: it counts its
substantive cycles, not itself, in a per-piece figure.

**Asides.** A cycle is an *aside*, a message you sent while background
work ran, when its message asks for something, it changes no files (no edit
of the main session's, of a subagent's or of a workflow's, and no shell
command that changes files: the ``changed`` facts the tag's grounding reads)
and, at its first reply, an agent or a workflow that an earlier cycle of the
same piece launched was still running (``Cycle.running``: launched, with no
report yet, or for a run that held the session no result). Most are side
questions or remarks; some steer the running work with a requirement, a
clarification or a correction. An aside never starts a piece, whatever its
``shift`` says, and is never rework, whatever its ``shift`` or ``why`` says.
It is left out of ``WorkPiece.substantive``, of the per-cycle rates and of
the piece's ``task``, ``level`` and ``size``, but what it cost stays in the
piece (``aside_cycles``, ``aside_cost``). A /clear still starts a piece,
aside or not. An aside's files do not stand for the piece's, so they never
change the files a later silence is compared with. A cycle that changes
files while the work runs is no aside: it is an ordinary cycle.

**Handoffs.** A session that opens with a handoff, within
:data:`HANDOFF_WITHIN_S` of the end of the same project's previous piece,
joins that piece (:func:`pieces_in`, the one place that sees two sessions);
so does the message after a /clear. A first message is a handoff only when
it carries the previous piece on, in one of two ways. A long one (a paste,
or :data:`HANDOFF_CHARS` characters) carries it on only when the first cycle
reads or edits a file the piece edited, whatever paths it names. Any other
one names a file path (the transcript keeps no flag for "names a plan file",
so a path in the message is taken to be one) and either the piece had a plan
approved or the first cycle reads or edits a file the piece edited. A long
brief that touches none of the piece's files is a new piece's, a plan
approved before it or not. Files are compared as the salted hashes the
units carry: reads and edits are hashed the same way with the same salt
(``parse.path_hash``), so they are in one space.

**Rework** is a cycle after the piece's first delivery (the first cycle
that changed files) that has any of: a settled ``shift`` of ``redo`` or
``fix``; a correction you typed or queued; or an adjustment (typed or
queued) that changes files the piece already changed. Each reads what you
said or Claude tagged. A short message that changes the files the cycle
before it changed is no rework on that alone: it was right for 1 of 12
cycles on a hand-check, the rest being a resumed run, a permission, an
answer, a new instruction or a further step. It is never rework when it
asks for nothing (a go-ahead, status check or thank-you, whatever its tag
says), when it is an aside, when it is a plan-feedback round (you sent a plan
back, or wrote in plan mode), when the plan check says ``new`` or ``none``,
or when your /cg-feedback answers say the plan was ``new`` or the follow-ups
were a change of mind alone (``why=changed``).

**Cause** of each rework cycle, in this order, each with the word for where
it came from (:data:`SOURCES`): your /cg-feedback answers (the plan check,
then ``plan``, then ``why``, with ``missed_in`` kept alongside), then the
cycle's settled ``why`` tag (Claude's, or Haiku's when Haiku wrote every
tag), else ``not_reported``, with the source ``inferred``: the follow-up was
read from your message or the tag's ``shift`` alone. ``missed``, which is
Claude's mistake, only ever comes from your answers or a tag: a correction
alone never says Claude got it wrong.

**Admissions** are the cycles whose reply owns a mistake of Claude's: the
settled ``admit`` word, never the bare pattern match
(``Cycle.admit_possible``, counted apart as a possibility). Each is
``user`` when a message of yours pushed back first, else ``self``. The
rework after the ones you caught is the admitting cycle (when it was
rework) and the run of rework cycles after it, each counted once.

**Cost** is ``capture.cycle_spend``'s: every turn of the main session and
of the agents it started, with a reply to an agent's or a workflow's report
charged to the cycle that started the agent, not to the one open when the
report arrived. The reply keeps its place in the timeline; the piece's
``moved_cost`` is how much of its cost came that way.

**Asks** (:func:`asks`) count the messages of a cycle that asked for
something, the ones you typed while Claude worked included: a typed message
is one unless it is a go-ahead, a status check, a thank-you or a reply to a
plan Claude had just put up; a queued one is one unless it was a go-ahead or
a status check. The rates in ``habits`` and ``prompting`` divide by them.

``habits.Piece`` is the habits tables' own row: the work one /cg-feedback
answer or dashboard rating covers, with its outcome, and one row with no
outcome for each :class:`WorkPiece` no answer covers. A :class:`WorkPiece`
never needs an answer.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Iterable, Sequence

from . import capture as capture_mod
from . import capture_catalogue as catalogue
from .model import Feedback, Turn

#: How long a silence is, with different files after it, before a message
#: with no ``shift`` word starts a piece. A gap alone was right 38% of the
#: time on a set of 285 hand-labelled messages.
GAP_S = 3 * 3600

#: The share of file names two messages have in common (Jaccard) below
#: which they are about different files, and the fewest files each side
#: must name to say so.
JACCARD_BELOW = 0.1
MIN_FILES = 2

#: How long after a piece ended a session that opens with a handoff still
#: joins it.
HANDOFF_WITHIN_S = 72 * 3600

#: A first message of this many characters, or one flagged as a paste
#: (``Turn.human_prompt_has_paste``), is a handoff only when its first cycle
#: reads or edits a file the piece edited, whatever paths it names and
#: whether or not a plan was approved (:func:`_is_handoff`).
HANDOFF_CHARS = 1500

#: Where a piece started, and how sure that start is.
STARTS = ("start", "clear", "new", "gap")
LOW_CONFIDENCE = frozenset({"gap"})
CONFIDENCE = ("high", "low")

#: Why a rework cycle was needed. ``left_out`` is something you hadn't
#: said, ``missed`` Claude missing what was in your request or the plan,
#: ``changed`` a change of mind, ``tools`` a tool that failed,
#: ``plan_gap`` something the plan left out, ``mixed`` more than one of
#: those with nothing to say which this was, and ``not_reported`` that
#: nothing says.
CAUSES = ("left_out", "missed", "changed", "tools", "plan_gap", "mixed", "not_reported")

#: Where a cause came from. ``inferred`` is for a follow-up that no answer
#: and no tag gave a cause: the follow-up itself was read from what you said
#: (a correction, an adjustment) or from the tag's ``shift`` alone, so its
#: cause is ``not_reported``.
SOURCES = ("feedback", "Claude tag", "Haiku tag", "inferred")

#: The words ``levels`` and ``sizes`` are counted under: the tag's, and
#: ``unknown`` for a cycle with none.
UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class WorkPiece:
    """One piece of work, as counts and ids."""

    #: The sessions it ran in, oldest first (more than one after a
    #: handoff), and the project they share (``""`` when unknown).
    session_ids: tuple[str, ...] = ()
    project: str = ""
    #: ``(session id, cycle index)`` of each prompt cycle in it, a
    #: /cg-feedback run left out.
    cycle_ids: tuple[tuple[str, int], ...] = ()
    #: When its first message was sent and when its last reply ran.
    start_ts: str = ""
    end_ts: str = ""
    #: Tokens (input, cache writes, cache reads and output) of the main
    #: session's replies charged to it: the capture hook's own count. The
    #: agents' are ``agent_tokens``.
    tokens: int = 0
    agent_tokens: int = 0
    #: Main-session replies charged to it.
    replies: int = 0
    #: List-price USD of every turn charged to it, the agents' included.
    cost: float = 0.0
    #: Part of ``cost``: replies to a report that arrived after you had
    #: sent another message, charged to the cycle that started the agent.
    moved_cost: float = 0.0
    #: Cycles in it, and those that asked for something (see the module
    #: docstring), the asides left out.
    cycles: int = 0
    substantive: int = 0
    #: Messages you sent while background work ran (the module docstring's
    #: asides), and what they cost: part of ``cost``, left out of
    #: ``substantive`` and of the rework counts.
    aside_cycles: int = 0
    aside_cost: float = 0.0
    #: ``(session id, cycle index)`` of each aside, the same ids as
    #: ``cycle_ids``.
    aside_ids: tuple[tuple[str, int], ...] = ()
    #: A cycle after the first delivery needed rework, and what those
    #: cycles cost.
    rework: int = 0
    rework_cost: float = 0.0
    #: ``(session id, cycle index)`` of each rework cycle, the same ids as
    #: ``cycle_ids``.
    rework_ids: tuple[tuple[str, int], ...] = ()
    #: Tokens of the rework cycles, the agents' included.
    rework_tokens: int = 0
    #: Whether any cycle changed files.
    delivered: bool = False
    #: ``(cause, source, cycles)``, in the order of :data:`CAUSES` and
    #: :data:`SOURCES`; the cycles add up to ``rework``.
    causes: tuple[tuple[str, str, int], ...] = ()
    #: ``(cause, source, cost, tokens)`` of the same cycles, in the same
    #: order; the costs add up to ``rework_cost``.
    cause_spend: tuple[tuple[str, str, float, int], ...] = ()
    #: Cycles whose reply admits a mistake of Claude's (the settled ``admit``
    #: word, never a bare pattern match), split by who found it: you (a
    #: message of yours pushed back first) or Claude itself. ``instruction``
    #: counts those that were an instruction it had been given.
    admitted: int = 0
    admitted_user: int = 0
    admitted_self: int = 0
    admitted_instruction: int = 0
    #: Cycles whose reply reads like an admission that no ``admit`` word
    #: confirms. A possibility, never added to a total above.
    admit_possible: int = 0
    #: What the rework after the admissions you caught cost: the admitting
    #: cycle when it was rework, and the run of rework cycles after it, each
    #: counted once.
    admit_user_cost: float = 0.0
    #: Where Claude's mistake was, from your answer: ``(word, cycles)``.
    missed_in: tuple[tuple[str, int], ...] = ()
    #: ``(word, substantive cycles, rework cycles, rework cost)`` by the
    #: settled ``level`` and ``size`` words, then ``unknown``.
    levels: tuple[tuple[str, int, int, float], ...] = ()
    sizes: tuple[tuple[str, int, int, float], ...] = ()
    #: The hardest ``level`` and biggest ``size`` a cycle of it had, and the
    #: ``task`` most of its cycles had (``""`` when none was tagged); an
    #: aside's tag is no part of them.
    level: str = ""
    size: str = ""
    task: str = ""
    #: A plan was approved in it.
    plan_approved: bool = False
    #: Some /cg-feedback answer, a skipped one too, covers it, or you ran
    #: /cg-feedback in it, whether or not an answer was kept. A run before
    #: the piece's first message rates none of it.
    rated: bool = False
    #: ``"high"`` when it starts at the session start, a /clear or a
    #: settled ``shift=new``; ``"low"`` when only the tag-free rule says so.
    confidence: str = "high"
    #: No start was found inside the session: it is one piece whose
    #: structure isn't known, and counts its substantive cycles.
    unsegmented: bool = False


@dataclass(slots=True)
class PieceSession:
    """One session's cycles, for :func:`pieces_in`."""

    session_id: str
    cycles: list
    project: str = ""
    #: ``None`` (read each cycle's /cg-feedback answer from the cycles), a
    #: list of ``capture.FeedbackSpan``, or one ``Feedback`` that rates
    #: every cycle (the dashboard's rating of the session).
    feedback: object = None


# -- facts of one cycle ----------------------------------------------------------


@dataclass(slots=True)
class _Unit:
    """What a cycle says, worked out once."""

    index: int
    cycle: capture_mod.Cycle
    session_id: str
    #: A /cg-feedback run: it rates work and is none.
    run: bool
    substantive: bool
    spend: capture_mod.CycleSpend
    #: A cycle changed files: edits, shell writes, or a subagent's.
    changed: bool
    #: The files it changed, and those it changed or read (salted hashes).
    edited: frozenset
    touched: frozenset
    #: A plan was approved in it.
    approved: bool = False
    tag: object = None
    #: At its first reply, an agent or workflow that an earlier cycle of the
    #: piece it would carry on launched was still running. Set by
    #: :func:`_segments`, which knows that piece.
    beside: bool = False
    #: A message sent while background work ran (the module docstring's
    #: asides): a ``beside`` cycle that asks for something and changes no
    #: files. Set by :func:`_segments`.
    aside: bool = False

    @property
    def opening(self) -> Turn:
        return self.cycle.turns[0]


def _substantive(cycle: capture_mod.Cycle, previous: capture_mod.Cycle | None = None) -> bool:
    """Whether ``cycle`` asked for something: its message is no go-ahead,
    status check, thank-you or reply to a plan Claude had just put up
    (``previous`` is the cycle before it), or a message you typed while it ran
    was a correction or an adjustment."""
    opening = cycle.turns[0]
    if not (opening.human_go or opening.human_status or opening.human_ack or _plan_reply(cycle, previous)):
        return True
    return any(t.queued_correction or t.queued_adjust for t in cycle.turns)


def queued_asks(turn: Turn) -> int:
    """How many of the messages you typed while Claude worked (``turn``'s
    ``queued_prompts``) asked for something. The parser flags whether *any*
    of them was a go-ahead or a status check, so one of each is taken off the
    count; a correction or an adjustment among them is one that asked."""
    queued = turn.queued_prompts
    if queued <= 0:
        return 0
    quiet = int(turn.queued_go) + int(turn.queued_status)
    return max(queued - min(quiet, queued), int(turn.queued_correction or turn.queued_adjust))


def _plan_reply(cycle: capture_mod.Cycle, previous: capture_mod.Cycle | None) -> bool:
    """Whether ``cycle`` opens with your reply to a plan: you wrote in plan
    mode straight after the cycle before it put up a plan that was not
    approved."""
    if previous is None or not cycle.turns[0].prompt_plan_mode:
        return False
    plans = [t for t in previous.turns if t.plan_stats is not None]
    return bool(plans) and not capture_mod._plan_approved(plans[-1])


def asks(cycle: capture_mod.Cycle, previous: capture_mod.Cycle | None = None) -> int:
    """How many messages of ``cycle`` asked for something (see the module
    docstring): its opening one, unless it is a go-ahead, status check,
    thank-you or plan reply, plus those you typed while it ran. ``previous``
    is the cycle before it, to tell a plan reply. A /cg-feedback run asks
    nothing."""
    if not cycle.turns or capture_mod.is_feedback_run(cycle):
        return 0
    opening = cycle.turns[0]
    quiet = opening.human_go or opening.human_status or opening.human_ack or _plan_reply(cycle, previous)
    return int(not quiet) + sum(queued_asks(t) for t in cycle.turns)


def _unit(
    index: int, cycle: capture_mod.Cycle, session_id: str, pricing, previous: capture_mod.Cycle | None = None
) -> _Unit:
    turns = cycle.tag_turns
    edited = frozenset(h for t in turns for h in t.edit_target_hashes)
    read = frozenset(h for t in turns for h in t.read_target_hashes)
    changed = bool(
        edited
        or sum(t.edit_call_count + t.shell_write_count + t.shell_change_count + t.agent_edit_files for t in turns)
        or any(
            t.edit_call_count + t.shell_write_count + t.shell_change_count
            for sub in cycle.subs
            for t in capture_mod._priced(sub)
        )
    )
    return _Unit(
        index=index,
        cycle=cycle,
        session_id=session_id,
        run=capture_mod.is_feedback_run(cycle),
        substantive=_substantive(cycle, previous),
        spend=capture_mod.cycle_spend(cycle, pricing),
        changed=changed,
        edited=edited,
        touched=edited | read,
        approved=any(capture_mod._plan_approved(t) for t in turns),
        tag=cycle.settled,
    )


def _jaccard(a: frozenset, b: frozenset) -> float:
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def _plan_word(cycle: capture_mod.Cycle) -> str:
    """The plan check answer on ``cycle``'s replies, or ``""``."""
    return next((t.plan_check.word for t in cycle.turns if t.plan_check is not None and t.plan_check.word), "")


def _is_handoff(unit: _Unit, previous: _Segment) -> bool:
    """Whether ``unit``, the first cycle after a fresh start, carries
    ``previous``, the piece before it, on. A long message (or a paste) does
    only when the cycle reads or edits a file ``previous`` edited: a long brief
    for other files is a new job's, with a path in it and a plan approved
    before it or not. Any other message does when it names a file path and
    either ``previous`` had a plan approved or the cycle reads or edits a file
    ``previous`` edited; a path with nothing to tie it to the piece is a new
    job's. Both sides are salted hashes of the same kind
    (``parse.path_hash``), so they compare."""
    opening = unit.opening
    carried = bool(unit.touched & previous.edited)
    if (opening.human_prompt_chars or 0) >= HANDOFF_CHARS or opening.human_prompt_has_paste:
        return carried
    return "path" in opening.prompt_flags and (previous.plan_approved or carried)


# -- where pieces start ----------------------------------------------------------


@dataclass(slots=True)
class _Segment:
    """The cycles of one piece, a /cg-feedback run included."""

    units: list = field(default_factory=list)
    sessions: list = field(default_factory=list)
    project: str = ""
    start: str = "start"
    unsegmented: bool = False

    @property
    def work(self) -> list[_Unit]:
        return [u for u in self.units if not u.run]

    @property
    def plan_approved(self) -> bool:
        return any(u.approved for u in self.work)

    @property
    def edited(self) -> frozenset:
        """Every file (salted hash) the piece edited."""
        return frozenset().union(*(u.edited for u in self.work))

    def opening_moment(self):
        for u in self.work:
            return capture_mod._moment(u.opening.ts)
        return None

    def closing_moment(self):
        for u in reversed(self.work):
            for turn in reversed(u.cycle.turns):
                moment = capture_mod._moment(turn.ts)
                if moment is not None:
                    return moment
        return None


def _gap_s(unit: _Unit, before: _Unit | None) -> float | None:
    """Seconds of silence before ``unit``'s message: the transcript's own
    reading, else from the two times."""
    if unit.opening.gap_s is not None:
        return unit.opening.gap_s
    if before is None:
        return None
    now = capture_mod._moment(unit.opening.ts)
    last = capture_mod._moment(before.cycle.turns[-1].ts)
    return (now - last).total_seconds() if now is not None and last is not None else None


def _is_beside(unit: _Unit, members: set[int]) -> bool:
    """Whether an agent or workflow launched by a cycle in ``members`` (the
    piece ``unit`` would carry on) was still running at ``unit``'s first
    reply."""
    return not members.isdisjoint(unit.cycle.running)


def _is_aside(unit: _Unit) -> bool:
    """Whether ``unit`` is a message sent while background work ran (the
    module docstring's asides): it asks for something and changes no files,
    with work of its piece running beside it."""
    return unit.substantive and not unit.changed and unit.beside


def _start_of(unit: _Unit, before: _Unit | None, last_files: frozenset, previous: _Segment) -> str | None:
    """The word in :data:`STARTS` for the piece ``unit`` starts, or
    ``None`` when it carries on ``previous``, the one before. An aside starts
    nothing, but a /clear is a fresh start whatever is running."""
    if "clear" in unit.opening.commands_run:
        return None if _is_handoff(unit, previous) else "clear"
    if unit.aside:
        return None
    shift = unit.tag.shift if unit.tag is not None else None
    if shift == "new" and unit.substantive:
        return "new"
    if shift is None and unit.substantive:
        gap = _gap_s(unit, before)
        if (
            gap is not None
            and gap >= GAP_S
            and len(unit.touched) >= MIN_FILES
            and len(last_files) >= MIN_FILES
            and _jaccard(unit.touched, last_files) < JACCARD_BELOW
        ):
            return "gap"
    return None


def _segments(session: PieceSession, pricing) -> list[_Segment]:
    """``session``'s cycles cut into pieces."""
    segments: list[_Segment] = []
    held: list[_Unit] = []
    before: _Unit | None = None
    last_files: frozenset = frozenset()
    #: The indexes of the cycles in the piece being built: what an aside
    #: ran beside has to have been launched by one of them.
    members: set[int] = set()
    for index, cycle in enumerate(session.cycles):
        if not cycle.turns:
            continue
        unit = _unit(index, cycle, session.session_id, pricing, session.cycles[index - 1] if index else None)
        if unit.run:
            if segments:
                segments[-1].units.append(unit)
            else:
                held.append(unit)
            continue
        unit.beside = _is_beside(unit, members)
        unit.aside = _is_aside(unit)
        start = "start" if not segments else _start_of(unit, before, last_files, segments[-1])
        if start is not None:
            segments.append(_Segment(units=held, sessions=[session.session_id], project=session.project, start=start))
            held = []
            members = set()
            # Nothing of an earlier piece's runs beside the first cycle of a new one.
            unit.beside = unit.aside = False
        segments[-1].units.append(unit)
        members.add(index)
        before = unit
        if unit.touched and not unit.aside:
            last_files = unit.touched
    if len(segments) == 1:
        segments[0].unsegmented = True
    return segments


# -- feedback --------------------------------------------------------------------


def _feedback_of(cycles: Sequence, feedback) -> tuple[dict[int, Feedback], set[int]]:
    """``(id(cycle) -> the Feedback that covers it, ids of the cycles any
    feedback covers or ran in)``."""
    if isinstance(feedback, Feedback):
        return {id(c): feedback for c in cycles}, {id(c) for c in cycles}
    spans = capture_mod.feedback_spans(list(cycles)) if feedback is None else list(feedback)
    by_cycle: dict[int, Feedback] = {}
    covered: set[int] = set()
    for span in spans:
        covered.add(id(span.run))
        for cycle in span.cycles:
            by_cycle[id(cycle)] = span.feedback
            covered.add(id(cycle))
    return by_cycle, covered


# -- rework ----------------------------------------------------------------------


def _plan_round(cycle: capture_mod.Cycle) -> bool:
    """A plan-feedback round: you sent a plan back, or wrote in plan mode.
    It is planning, whatever the tag says."""
    return cycle.feedback_rounds > 0 or cycle.turns[0].prompt_plan_mode


def _reasons(unit: _Unit, files: set) -> bool:
    """Whether ``unit``, a cycle after the first delivery, reads as rework
    on what you said or Claude tagged: a settled ``redo`` or ``fix``, a
    correction, or an adjustment re-changing the piece's files (``files``,
    those it has changed so far). The size of the message and the files it
    changes say nothing on their own: a short message that changes the files
    the cycle before it changed, with no such flag or tag, is no rework. A
    cycle that asks for nothing (a go-ahead, status check or thank-you with
    no correction or adjustment typed while it ran) is never rework, whatever
    its tag says; nor is an aside, a message sent while background work
    ran."""
    if not unit.substantive or unit.aside:
        return False
    turns = unit.cycle.turns
    opening = unit.opening
    if unit.tag is not None and unit.tag.shift in ("redo", "fix"):
        return True
    if opening.human_correction or any(t.queued_correction for t in turns):
        return True
    return bool(opening.human_adjust or any(t.queued_adjust for t in turns)) and bool(unit.edited & files)


def _excused(unit: _Unit, fb: Feedback | None, plan_before: bool = True) -> bool:
    """Whether what you said rules ``unit`` out as rework though it reads
    like it: the plan check says it was new or not a fix, your plan answer
    says the plan missed nothing it should have (``new``), or the follow-ups
    were a change of mind alone. A mix of reasons rules nothing out. The plan
    answer speaks only for the cycles after a plan was approved
    (``plan_before``)."""
    word = _plan_word(unit.cycle)
    if word in ("new", "none"):
        return True
    if fb is None or word in ("covered", "gap"):
        return False
    if fb.plan == "new" and plan_before:
        return True
    return {w for w in fb.why if w != "none"} == {"changed"}


def _cause(unit: _Unit, fb: Feedback | None, plan_before: bool) -> tuple[str, str, str]:
    """``(cause, source, missed_in)`` of a rework cycle: your answers, then
    the settled ``why`` tag, else ``not_reported`` with the source
    ``inferred``."""
    word = _plan_word(unit.cycle)
    if word == "covered":
        return "missed", "feedback", "plan"
    if word == "gap":
        return "plan_gap", "feedback", ""
    tag = unit.tag
    tagged = tag.why if tag is not None and tag.why in CAUSES else ""
    if fb is not None:
        if fb.plan == "covered" and plan_before:
            return "missed", "feedback", "plan"
        if fb.plan == "gap" and plan_before:
            return "plan_gap", "feedback", ""
        words = [w for w in fb.why if w in CAUSES]
        if len(words) > 1:
            words = [tagged] if tagged in words else ["mixed"]
        if words:
            return words[0], "feedback", (fb.missed_in or "") if words[0] == "missed" else ""
    if tagged:
        return tagged, "Haiku tag" if unit.cycle.haiku_only else "Claude tag", ""
    return "not_reported", "inferred", ""


def _admission(unit: _Unit) -> tuple[str, str]:
    """``(word, who)`` of the admission ``unit``'s reply makes: the settled
    ``admit`` word, and ``user`` when a message of yours pushed back before
    it (``Turn.admit_caught``), else ``self``. A tag with no pattern match
    behind it has no ``admit_caught``, so your message is read instead: a
    correction, an adjustment, or one you queued. ``("", "")`` for none."""
    tag = unit.tag
    word = tag.admit if tag is not None and tag.admit in catalogue.TAG_VOCAB["admit"] else ""
    if not word:
        return "", ""
    caught = {t.admit_caught for t in unit.cycle.tag_turns if t.admit_candidate and t.admit_caught}
    if caught:
        return word, "user" if "user" in caught else "self"
    opening = unit.opening
    pushed = (
        opening.human_correction
        or opening.human_adjust
        or any(t.queued_correction or t.queued_adjust for t in unit.cycle.turns)
    )
    return word, "user" if pushed else "self"


def _highest(words: Iterable[str], key: str) -> str:
    order = catalogue.TAG_VOCAB[key]
    found = [w for w in words if w in order]
    return max(found, key=order.index) if found else ""


def _mix(rows: dict[str, list], key: str) -> tuple[tuple[str, int, int, float], ...]:
    """``rows`` (word -> ``[substantive, rework, rework cost]``) as
    ``levels`` or ``sizes`` rows, in the order of the tag's words."""
    return tuple((word, *rows[word]) for word in (*catalogue.TAG_VOCAB[key], UNKNOWN) if word in rows)


def _build(segment: _Segment, by_cycle: dict[int, Feedback], covered: set[int]) -> WorkPiece | None:
    """The :class:`WorkPiece` of ``segment``, or ``None`` when it holds no
    work (a /cg-feedback run alone)."""
    work = segment.work
    if not work:
        return None
    delivered = False
    plan_before = False
    files: set = set()
    rework = 0
    rework_cost = 0.0
    rework_ids: list[tuple[str, int]] = []
    causes: Counter = Counter()
    spent: dict[tuple[str, str], list] = {}
    where: Counter = Counter()
    mixes: dict[str, dict[str, list]] = {"level": {}, "size": {}}
    flags: list[bool] = []
    for unit in work:
        reworked = False
        fb = by_cycle.get(id(unit.cycle))
        if (
            delivered
            and not _plan_round(unit.cycle)
            and _reasons(unit, files)
            and not _excused(unit, fb, plan_before)
        ):
            reworked = True
            cause, source, missed_in = _cause(unit, fb, plan_before)
            causes[(cause, source)] += 1
            row = spent.setdefault((cause, source), [0.0, 0])
            row[0] += unit.spend.cost
            row[1] += unit.spend.tokens + unit.spend.agent_tokens
            if missed_in:
                where[missed_in] += 1
            rework += 1
            rework_cost += unit.spend.cost
            rework_ids.append((unit.session_id, unit.index))
        if unit.substantive and not unit.aside:
            for key in ("level", "size"):
                word = getattr(unit.tag, key, None) if unit.tag is not None else None
                row = mixes[key].setdefault(word if word in catalogue.TAG_VOCAB[key] else UNKNOWN, [0, 0, 0.0])
                row[0] += 1
                row[1] += int(reworked)
                row[2] += unit.spend.cost if reworked else 0.0
        flags.append(reworked)
        delivered = delivered or unit.changed
        plan_before = plan_before or unit.approved
        files |= unit.edited
    admits = [_admission(u) for u in work]
    # The rework after an admission you caught: its own cycle when that was
    # rework, then the run of rework cycles that follows. Each counts once.
    after_caught: set[int] = set()
    for n, (_, who) in enumerate(admits):
        if who == "user":
            k = n if flags[n] else n + 1
            while k < len(work) and flags[k]:
                after_caught.add(k)
                k += 1
    # What the piece is called comes from the work, not from a message sent while it ran.
    tags = [u.tag for u in work if u.tag is not None and not u.aside]
    tasks = Counter(t.task for t in tags if t.task)
    last_ts = next((t.ts for u in reversed(work) for t in reversed(u.cycle.turns) if t.ts), "")
    # A /cg-feedback run held from before the piece's first message rates none of it.
    first = next(n for n, u in enumerate(segment.units) if not u.run)
    return WorkPiece(
        session_ids=tuple(dict.fromkeys(segment.sessions)),
        project=segment.project,
        cycle_ids=tuple((u.session_id, u.index) for u in work),
        start_ts=work[0].opening.ts,
        end_ts=last_ts,
        tokens=sum(u.spend.tokens for u in work),
        agent_tokens=sum(u.spend.agent_tokens for u in work),
        replies=sum(u.spend.replies for u in work),
        cost=sum(u.spend.cost for u in work),
        moved_cost=sum(u.spend.moved_in for u in work),
        cycles=len(work),
        substantive=sum(1 for u in work if u.substantive and not u.aside),
        aside_cycles=sum(1 for u in work if u.aside),
        aside_cost=sum(u.spend.cost for u in work if u.aside),
        aside_ids=tuple((u.session_id, u.index) for u in work if u.aside),
        rework=rework,
        rework_cost=rework_cost,
        rework_ids=tuple(rework_ids),
        rework_tokens=sum(row[1] for row in spent.values()),
        delivered=delivered,
        causes=tuple(
            (cause, source, n)
            for cause in CAUSES
            for source in SOURCES
            if (n := causes.get((cause, source), 0))
        ),
        cause_spend=tuple(
            (cause, source, *spent[(cause, source)]) for cause in CAUSES for source in SOURCES if (cause, source) in spent
        ),
        admitted=sum(1 for word, _ in admits if word),
        admitted_user=sum(1 for _, who in admits if who == "user"),
        admitted_self=sum(1 for _, who in admits if who == "self"),
        admitted_instruction=sum(1 for word, _ in admits if word == "instruction"),
        admit_possible=sum(1 for u in work if u.cycle.admit_possible),
        admit_user_cost=sum(work[k].spend.cost for k in after_caught),
        missed_in=tuple(
            (word, where[word]) for word in (*catalogue.FEEDBACK_VOCAB["missed_in"], "") if where.get(word)
        ),
        levels=_mix(mixes["level"], "level"),
        sizes=_mix(mixes["size"], "size"),
        level=_highest((t.level for t in tags), "level"),
        size=_highest((t.size for t in tags), "size"),
        task=tasks.most_common(1)[0][0] if tasks else "",
        plan_approved=segment.plan_approved,
        rated=any(id(u.cycle) in covered for u in work) or any(u.run for u in segment.units[first + 1 :]),
        confidence="low" if segment.start in LOW_CONFIDENCE else "high",
        unsegmented=segment.unsegmented,
    )


# -- the public builders ---------------------------------------------------------


def _pricing(rates):
    """The price table behind ``rates``: a ``Pricing``, ``habits``' ``_Rates``
    (which holds one), or ``None`` for no costs."""
    return getattr(rates, "pricing", rates)


def pieces_of(
    cycles: Sequence, rates=None, feedback=None, *, session_id: str = "", project: str = ""
) -> list[WorkPiece]:
    """The pieces of work in one session's ``cycles`` (``capture.prompt_cycles``),
    oldest first, with no feedback needed. ``rates`` prices them (a ``Pricing``
    or ``habits``' ``_Rates``; none gives costs of ``0.0``). ``feedback`` says
    what you rated: ``None`` reads each /cg-feedback answer from the cycles, a
    list of ``capture.FeedbackSpan`` gives them, and one ``Feedback`` rates
    every cycle. A session that opened with a handoff does not join an earlier
    session here: that is :func:`pieces_in`."""
    pricing = _pricing(rates)
    by_cycle, covered = _feedback_of(cycles, feedback)
    segments = _segments(PieceSession(session_id, list(cycles), project), pricing)
    return [piece for piece in (_build(s, by_cycle, covered) for s in segments) if piece is not None]


def _joins(previous: _Segment, head: _Segment) -> bool:
    """Whether ``head``, the first piece of a session, carries on
    ``previous``: the same known project, within :data:`HANDOFF_WITHIN_S`
    of where it ended, and ``head`` opens with a handoff
    (:func:`_is_handoff`)."""
    if not head.project or head.project != previous.project:
        return False
    began = head.opening_moment()
    ended = previous.closing_moment()
    if began is None or ended is None or not timedelta(0) <= began - ended <= timedelta(seconds=HANDOFF_WITHIN_S):
        return False
    return _is_handoff(head.work[0], previous)


def pieces_in(sessions: Iterable[PieceSession], rates=None) -> list[WorkPiece]:
    """The pieces of work across ``sessions``, oldest first. A session that
    opens with a handoff joins the piece the same project last ended (see the
    module docstring); the rework and cost of the joined piece are worked out
    over all of it."""
    pricing = _pricing(rates)
    built: list[tuple[PieceSession, list[_Segment]]] = []
    for session in sessions:
        segments = _segments(session, pricing)
        if segments:
            built.append((session, segments))
    built.sort(key=lambda item: item[1][0].opening_moment() or capture_mod._FLOOR)
    latest: dict[str, _Segment] = {}
    kept: list[_Segment] = []
    by_cycle: dict[int, Feedback] = {}
    covered: set[int] = set()
    for session, segments in built:
        mine, seen = _feedback_of(session.cycles, session.feedback)
        by_cycle.update(mine)
        covered |= seen
        head = segments[0]
        previous = latest.get(session.project) if session.project else None
        if previous is not None and _joins(previous, head):
            # A /cg-feedback run held from before this session's first message
            # rated none of the piece it joins: its span is empty.
            previous.units.extend(head.units[next(n for n, u in enumerate(head.units) if not u.run) :])
            previous.sessions.append(session.session_id)
            previous.unsegmented = previous.unsegmented and head.unsegmented
            segments = [previous, *segments[1:]]
            kept.extend(segments[1:])
        else:
            kept.extend(segments)
        if session.project:
            latest[session.project] = segments[-1]
    pieces = [piece for piece in (_build(s, by_cycle, covered) for s in kept) if piece is not None]
    pieces.sort(key=lambda piece: capture_mod._moment(piece.start_ts) or capture_mod._FLOOR)
    return pieces


def corpus_pieces(corpus, rates=None) -> list[WorkPiece]:
    """The pieces of work in every session of ``corpus`` that has a main
    transcript, handoffs joined. Two sessions are of one project when they
    share the project folder (or, from the store, its slug)."""
    sessions = []
    for bundle in corpus.sessions:
        if bundle.top is None:
            continue
        cycles = capture_mod.prompt_cycles(bundle.top, bundle.subs, getattr(bundle, "workflows", ()))
        # A corpus rebuilt from the store has no project folder, only its slug.
        project = getattr(bundle, "project_dir", "") or getattr(bundle, "slug", "") or ""
        sessions.append(PieceSession(bundle.session_id, cycles, project))
    return pieces_in(sessions, rates)


# -- normalising rework ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReworkRate:
    """Rework over some pieces of work, in the two denominators that mean
    something: per substantive cycle, and per piece."""

    #: ``""`` for all of them, else the ``level`` or ``size`` word.
    key: str = ""
    pieces: int = 0
    #: Pieces whose start was found inside their session: the denominator
    #: of ``per_piece``. An unsegmented piece is a whole session of unknown
    #: structure, and counts its substantive cycles instead.
    segmented: int = 0
    #: Pieces with any rework, among the ``segmented`` ones.
    reworked: int = 0
    substantive: int = 0
    rework: int = 0
    rework_cost: float = 0.0

    @property
    def per_cycle(self) -> float | None:
        """Rework cycles over substantive cycles, ``None`` with none."""
        return self.rework / self.substantive if self.substantive else None

    @property
    def per_piece(self) -> float | None:
        """The share of segmented pieces that needed rework, ``None`` with
        none."""
        return self.reworked / self.segmented if self.segmented else None


def rework_rate(pieces: Iterable[WorkPiece], key: str = "") -> ReworkRate:
    """:class:`ReworkRate` of all ``pieces``."""
    pieces = list(pieces)
    segmented = [p for p in pieces if not p.unsegmented]
    return ReworkRate(
        key=key,
        pieces=len(pieces),
        segmented=len(segmented),
        reworked=sum(1 for p in segmented if p.rework),
        substantive=sum(p.substantive for p in pieces),
        rework=sum(p.rework for p in pieces),
        rework_cost=sum(p.rework_cost for p in pieces),
    )


def _word(piece: WorkPiece, key: str) -> str:
    return (piece.level if key == "level" else piece.size) or UNKNOWN


def _rework_by(pieces: Iterable[WorkPiece], mix: str, key: str) -> dict[str, ReworkRate]:
    """Rework split by a tag word: the cycles by each cycle's word
    (``levels`` or ``sizes``), the pieces by the piece's own (``level`` or
    ``size``). Only the words something was counted under are returned."""
    pieces = list(pieces)
    out: dict[str, ReworkRate] = {}
    for word in (*catalogue.TAG_VOCAB[key], UNKNOWN):
        rows = [row for p in pieces for row in getattr(p, mix) if row[0] == word]
        mine = [p for p in pieces if _word(p, key) == word]
        if not rows and not mine:
            continue
        segmented = [p for p in mine if not p.unsegmented]
        out[word] = ReworkRate(
            key=word,
            pieces=len(mine),
            segmented=len(segmented),
            reworked=sum(1 for p in segmented if p.rework),
            substantive=sum(row[1] for row in rows),
            rework=sum(row[2] for row in rows),
            rework_cost=sum(row[3] for row in rows),
        )
    return out


def rework_by_level(pieces: Iterable[WorkPiece]) -> dict[str, ReworkRate]:
    """:func:`rework_rate` for each ``level`` word, then ``unknown``."""
    return _rework_by(pieces, "levels", "level")


def rework_by_size(pieces: Iterable[WorkPiece]) -> dict[str, ReworkRate]:
    """:func:`rework_rate` for each ``size`` word, then ``unknown``."""
    return _rework_by(pieces, "sizes", "size")


__all__ = [
    "CAUSES",
    "CONFIDENCE",
    "GAP_S",
    "HANDOFF_CHARS",
    "HANDOFF_WITHIN_S",
    "JACCARD_BELOW",
    "MIN_FILES",
    "PieceSession",
    "ReworkRate",
    "SOURCES",
    "STARTS",
    "UNKNOWN",
    "WorkPiece",
    "asks",
    "corpus_pieces",
    "pieces_in",
    "pieces_of",
    "queued_asks",
    "rework_by_level",
    "rework_by_size",
    "rework_rate",
]

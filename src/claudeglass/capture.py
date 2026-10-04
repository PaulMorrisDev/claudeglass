"""What metrics capture costs.

Two views, both in list-price USD (``units.Units.money`` phrases them
for the billing mode):

- :func:`usage` measures what capture actually cost while it ran, from
  the transcripts: each capture note is a ``capture_note`` event whose
  size is exactly what the model was shown, carried from where it was
  injected until the next compaction (a cache write, then a cache read
  per turn, and a write again whenever the cache was rebuilt); each tag
  is output its writer paid for at that turn's own rate, fast mode and
  data residency included (``pricing.effective_rates``). It also says
  how often Claude tagged what it was asked to (coverage), and how much
  of each metric has been collected. A /cg-feedback run is priced whole
  (every turn of the cycle it ran in), in any session, captured or not:
  the skill works at every level. So are the plan check's and the rating
  reminder's notes, and what Claude wrote for them (the question it asked,
  the reminder line it ended a reply with): scope ``feedback``, whatever
  the capture level (:func:`_add_feedback_hints`).
- :func:`history` replays your own recent sessions to price one
  character of note or tag in each place capture puts them, so
  :func:`estimate` can price any level or set of metrics before you turn
  it on, and :func:`metric_estimates` what each metric adds on its own.

A *prompt cycle* (:func:`prompt_cycles`) is one message of yours and
everything Claude did about it: the turn after a human message up to
the next one, with the subagents those turns started, at any depth. A
workflow's agents join the cycle of the reply that started (or resumed)
their run (:class:`WorkflowLaunches`). A tag written in reply to a
background agent's or a workflow's report counts for the cycle whose call
launched it, even when you had sent another message by then
(``Cycle.late_turns``), and so does what that reply cost
(:func:`cycle_spend`). The turns stay where they ran, so the timeline is
unchanged; only the charge moves.

:func:`feedback_spans` ties each /cg-feedback answer to the work it
rates: the cycles since the previous feedback (answered or declined),
or since the session started. Running it again with no work since the
previous run replaces that run's answers.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field, fields, replace
from datetime import datetime, timezone

from . import capture_catalogue as catalogue
from .capture_tags import MAIN_TAG_FIELDS, SUB_TAG_FIELDS, merge_feedback, settle_feedback, settle_tag
from .context_files import _Carry, _parse_ts
from .model import CaptureTag, EventKind, Feedback, TranscriptResult, Turn
from .pricing import Pricing, effective_rates, price_turn
from .testrun import widest
from .topology import agent_key

CHARS_PER_TOKEN = 4

#: Days of your own sessions replayed to estimate what capture would
#: cost (``capture status``, ``init`` and the Capture tab).
HISTORY_DAYS = 14

#: What Claude Code adds around a hook note, by hook event.
_WRAP = {event: catalogue.NOTE_WRAP_CHARS + len(event) for event in ("SessionStart", "SubagentStart", "PostToolUse")}

#: Characters of "[cg: " and "]" around a reply tag, and the line break
#: before it.
_TAG_FRAME_CHARS = 7

#: How many answers a metric needs before its suggestions are firm; the
#: Capture page says when a metric has enough and could be turned off.
ENOUGH = {"main": 40, "subagent": 25, "brief": 20, "tool": 15, "signal": 20, "feedback": 10}


def _scope_of(metric_id: str) -> str:
    m = catalogue.METRICS_BY_ID[metric_id]
    if m.group == "free":
        return "signal"
    if m.group == "feedback":
        return "feedback"
    if m.tool_note:
        return "tool"
    if m.main_extra or m.sub_extra:
        return "brief"
    return "main" if m.main_line else "subagent"


def enough_target(metric_id: str) -> int:
    return ENOUGH[_scope_of(metric_id)] if metric_id in catalogue.METRICS_BY_ID else 0


# -- prompt cycles -------------------------------------------------------------


@dataclass(slots=True)
class Cycle:
    """One message of yours and the work that answered it."""

    #: Indexes into the transcript's priced turns: ``start`` is the turn
    #: after your message, ``end`` the first turn of the next cycle.
    start: int
    end: int
    turns: list[Turn] = field(default_factory=list)
    #: Subagent transcripts started in this cycle, at any depth.
    subs: list[TranscriptResult] = field(default_factory=list)
    #: Replies in later cycles that answered the report of an agent (or
    #: workflow) this cycle launched, in order. Their tags and their cost
    #: are this cycle's; the turns still belong to the cycle they ran in.
    #: A reply is every turn from the one that read the report to the last
    #: of the tool calls it made (``_hand_off_replies``).
    late_turns: list[Turn] = field(default_factory=list)
    #: Positions in ``turns`` of replies that answered an earlier cycle's
    #: report: their tags and their cost are that cycle's, not this one's.
    handed_off: set[int] = field(default_factory=set)
    #: What the transcript says about this cycle's work, the facts
    #: ``capture_tags.settle`` puts right the tag's words with (see its
    #: docstring for the keys). ``prompt_cycles`` fills it; a cycle built
    #: without it has none, and ``settled`` is then ``tag``.
    facts: dict = field(default_factory=dict)

    @property
    def tag_turns(self) -> list[Turn]:
        """The turns whose tags, and whose cost, are this cycle's: its
        own, less the replies to an earlier cycle's agents, then the later
        replies to its own."""
        own = [turn for n, turn in enumerate(self.turns) if n not in self.handed_off]
        return own + self.late_turns

    @property
    def tag(self):
        """Every ``[cg: ...]`` tag in the cycle, merged key by key: for
        each field, the last turn to answer it wins that field, even
        when an earlier or a later tag in the same cycle (a retry, a
        correction) left it unset (CAP-10 -- this used to keep only the
        last tag whole, silently losing a key an earlier tag answered
        and the last one didn't). The exception is ``level`` and ``size``:
        a cycle's work was as hard and as big as its hardest and biggest
        reply, so the highest word wins. A tag written in reply to the
        report of an agent this cycle launched counts here, in whichever
        cycle the reply ran (``tag_turns``). When Claude tagged any reply
        of the cycle, a tag Haiku filled in for one it left untagged (a
        reply before a background command finished, say) gives no words:
        Claude judged the whole piece of work. ``None`` when no turn wrote
        one. The words are as written; ``settled`` has the transcript's
        facts applied."""
        tags = self._tags()
        if not tags:
            return None
        words = [t for t in tags if not t.judged] or tags
        merged = words[0]
        for tag in words[1:]:
            changes = {
                f.name: getattr(tag, f.name)
                for f in fields(tag)
                if f.name not in _TAG_COST_FIELDS and getattr(tag, f.name) not in (None, ())
            }
            if changes:
                merged = replace(merged, **changes)
        # has_tl is true if any of them wrote a [cg: ...]; chars sums
        # what every tag actually cost to write, judge_usd what Haiku's did.
        return replace(
            merged,
            level=_highest(words, "level"),
            size=_highest(words, "size"),
            has_tl=True,
            chars=sum(t.chars for t in tags),
            judged=any(t.judged for t in tags),
            judge_usd=sum(t.judge_usd for t in tags),
            grounded=tuple(dict.fromkeys(item for t in tags for item in t.grounded)),
        )

    def _tags(self) -> list[CaptureTag]:
        return [t.cap for t in self.tag_turns if t.cap is not None and t.cap.has_tl]

    @property
    def haiku_only(self) -> bool:
        """The cycle has a tag and Claude wrote none of them: Haiku
        wrote every one (``CaptureTag.judged``)."""
        tags = self._tags()
        return bool(tags) and all(t.judged for t in tags)

    @property
    def settled(self) -> CaptureTag | None:
        """:attr:`tag` with what the transcript settles put right
        (``capture_tags.settle``): a plan the cycle made, tests it ran, a
        first message's ``shift``, a ``why`` with no redo behind it. What
        was changed is in its ``grounded``. Read this, not ``tag``, for
        anything that counts or compares tags; ``tag`` is for what Claude
        wrote. Same as ``tag`` when the cycle has no facts."""
        tag = self.tag
        if tag is None or not self.facts:
            return tag
        facts = dict(self.facts)
        # ``admit`` needs a candidate in the reply only when Haiku wrote it.
        facts["judged"] = next((t.judged for t in reversed(self._tags()) if t.admit), False)
        return settle_tag(tag, facts)

    @property
    def admit_possible(self) -> bool:
        """A reply says it got something wrong (``Turn.admit_candidate``)
        but no ``admit`` stands for it: a pattern match, not a confirmed
        admission, so it is never added to a total."""
        if not self.facts.get("admit_candidate"):
            return False
        settled = self.settled
        return settled is None or settled.admit is None

    @property
    def plan_rounds(self) -> int:
        """Plans Claude put up in this cycle: its ``ExitPlanMode`` calls."""
        return sum(1 for turn in self.turns if turn.plan_stats is not None)

    @property
    def rejected_rounds(self) -> int:
        """Plans you sent back in the dialog, even one you then approved by
        typing a go-ahead."""
        return sum(1 for turn in self.turns if turn.plan_stats is not None and turn.plan_stats.rejected)

    @property
    def feedback_rounds(self) -> int:
        """Rejected plans you typed feedback for."""
        return sum(1 for turn in self.turns if turn.plan_stats is not None and turn.plan_stats.feedback_chars)

    @property
    def plan_feedback_classes(self) -> tuple[str, ...]:
        """How each round's feedback read, in order: one word from
        ``capture_catalogue.PLAN_FEEDBACK_CLASSES`` per round with any."""
        return tuple(
            turn.plan_stats.feedback_class
            for turn in self.turns
            if turn.plan_stats is not None and turn.plan_stats.feedback_class
        )

    @property
    def ask_rounds(self) -> int:
        """Clarifying questions Claude asked you in this cycle that you
        answered."""
        return sum(turn.ask_rounds for turn in self.turns)


#: ``CaptureTag`` fields about the tag itself, not what it says.
_TAG_COST_FIELDS = ("has_tl", "chars", "judged", "judge_usd", "grounded")


def _highest(tags: list[CaptureTag], key: str) -> str | None:
    """The highest word any of ``tags`` gives ``key`` (``level``:
    easy < normal < hard; ``size``: xs < s < m < l < xl), ``None`` when
    none gives one."""
    order = catalogue.TAG_VOCAB[key]
    found = [word for word in (getattr(t, key) for t in tags) if word in order]
    return max(found, key=order.index) if found else None


def _priced(result: TranscriptResult) -> list[Turn]:
    return [turn for turn in result.turns if turn.turn_index > 0]


_FLOOR = datetime.min.replace(tzinfo=timezone.utc)


def _moment(ts: str | None) -> datetime | None:
    """``ts`` as an aware time (UTC when it names no zone), or ``None``."""
    moment = _parse_ts(ts) if ts else None
    if moment is not None and moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def _timeline(turns: list[Turn]) -> list[datetime]:
    """One time per turn that never runs backwards, so it can be searched:
    a turn with no time (or one stamped before the turn before it) takes
    the earlier one's."""
    out = []
    last = _FLOOR
    for turn in turns:
        moment = _moment(turn.ts)
        if moment is not None and moment > last:
            last = moment
        out.append(last)
    return out


def _first_moment(sub: TranscriptResult) -> datetime | None:
    """When a subagent first spoke: its first priced turn with a time,
    else its first turn with one."""
    for turns in (_priced(sub), sub.turns):
        for turn in turns:
            moment = _moment(turn.ts)
            if moment is not None:
                return moment
    return None


class WorkflowLaunches:
    """Which main-session reply started each workflow agent.

    A workflow agent's metadata names its run (``workflow_run_id``, the
    run's directory) but no tool call, so the join is through the
    ``Workflow`` calls themselves (``Turn.workflow_runs``). A resumed run
    keeps its ``runId`` and its directory, so one run can have several
    calls; its agents are split between them by time: each agent goes to
    the latest call at or before its first priced turn. Failing that (a
    digest from before the parser recorded the calls, or a call whose
    result was never logged) the agent goes to the reply before the run
    file's ``started`` time, and failing that to none.

    Only a ``kind == "workflow-agent"`` transcript is ever joined: a
    workflow agent's ``agent_type`` is a custom name as often as
    ``workflow-subagent``, so the kind is what says it is one.

    Every answer is an index into the main session's priced turns, the
    one :func:`prompt_cycles` and ``habits`` both use for "the reply
    that started it".
    """

    def __init__(self, top: TranscriptResult, runs=()):
        turns = _priced(top)
        self.stamps = _timeline(turns)
        #: runId -> [(priced-turn index, tool_use_id)], in turn order.
        self._calls: dict[str, list[tuple[int, str]]] = {}
        #: taskId -> the tool_use_id of the call that launched it.
        self._tasks: dict[str, str] = {}
        for i, turn in enumerate(turns):
            for tool_use_id, (run_id, task_id) in turn.workflow_runs.items():
                self._calls.setdefault(run_id, []).append((i, tool_use_id))
                if task_id:
                    self._tasks.setdefault(task_id, tool_use_id)
        self._started: dict[str, datetime] = {}
        for run in runs:
            moment = _moment(run.started)
            if run.run_id and moment is not None:
                self._started.setdefault(run.run_id, moment)

    def task_use(self, task_id: str) -> str | None:
        """The tool_use_id of the ``Workflow`` call that launched
        ``task_id`` (what the run's task notification names)."""
        return self._tasks.get(task_id)

    def call_for(self, sub: TranscriptResult) -> tuple[int, str] | None:
        """``(priced-turn index, tool_use_id)`` of the ``Workflow`` call
        that started ``sub``, or ``None`` for an agent that isn't a
        workflow agent or whose run has no call at or before it."""
        run_id = sub.meta.workflow_run_id
        if sub.meta.kind != "workflow-agent" or not run_id:
            return None
        calls = self._calls.get(run_id)
        if not calls:
            return None
        first = _first_moment(sub)
        if first is None:
            return calls[0]
        found = None
        for i, tool_use_id in calls:
            if self.stamps[i] <= first:
                found = (i, tool_use_id)
        return found

    def turn_index(self, sub: TranscriptResult) -> int | None:
        """The priced turn of the main session that started ``sub``, or
        ``None``."""
        if sub.meta.kind != "workflow-agent":
            return None
        call = self.call_for(sub)
        if call is not None:
            return call[0]
        started = self._started.get(sub.meta.workflow_run_id or "")
        if started is None:
            return None
        i = bisect.bisect_right(self.stamps, started) - 1
        return i if i >= 0 else None


def prompt_cycles(top: TranscriptResult, subs=(), workflows=()) -> list[Cycle]:
    """The prompt cycles of a main-session transcript, each with the
    subagents it started. Turns before your first message (a resumed
    session's leftovers) make no cycle. ``workflows`` (the session's
    ``WorkflowRun`` files) lets a workflow agent whose run has no logged
    call still find its cycle by when the run started."""
    turns = _priced(top)
    starts = [i for i, turn in enumerate(turns) if turn.human_prompt_chars is not None]
    cycles = [
        Cycle(start=s, end=e, turns=turns[s:e]) for s, e in zip(starts, starts[1:] + [len(turns)])
    ]
    if not cycles:
        return cycles
    cycle_of_use: dict[str, int] = {}
    for n, cycle in enumerate(cycles):
        for turn in cycle.turns:
            for tool_use_id in turn.tool_use_ids:
                cycle_of_use[tool_use_id] = n
    launches = WorkflowLaunches(top, workflows)
    by_workflow: dict[int, int] = {}
    for sub in subs:
        at = launches.turn_index(sub)
        if at is not None and at >= starts[0]:
            by_workflow[id(sub)] = bisect.bisect_right(starts, at) - 1
    by_agent = {agent_key(sub.meta.agent_id): sub for sub in subs if sub.meta.agent_id}
    for sub in subs:
        n = _cycle_for(sub, cycle_of_use, by_agent, by_workflow)
        if n is not None:
            cycles[n].subs.append(sub)
    _hand_off_replies(top, turns, starts, cycles, cycle_of_use, subs, launches)
    _cycle_facts(cycles, 1 if starts[0] else 0)
    return cycles


def _plan_approved(turn: Turn) -> bool:
    """Whether this reply's plan was approved, in the dialog or by a
    go-ahead message (``handoff._approved``; the hook's ``plan_before``)."""
    return turn.plan_stats is not None and turn.plan_stats.outcome in ("approved", "approved_by_message")


def _cycle_facts(cycles: list[Cycle], before: int) -> None:
    """Fill each cycle's ``facts`` for ``capture_tags.settle``, from what
    the parser counted: the plan, skills, edits, subagents' and workflow
    agents' edits, shell commands, tests, errors and admissions of the
    turns whose tags are the cycle's (``Cycle.tag_turns``), and how the
    message that opened it reads. ``before`` is how many messages came
    before the first cycle (1 when the transcript starts mid-session, with
    replies to a message it doesn't hold)."""
    approved = False
    last: frozenset[str] = frozenset()
    for n, cycle in enumerate(cycles):
        turns = cycle.tag_turns
        opening = cycle.turns[0]
        edits = sum(t.edit_call_count for t in turns)
        hashes = frozenset(h for t in turns for h in t.edit_target_hashes)
        agent_files = sum(t.agent_edit_files for t in turns)
        for sub in cycle.subs:
            agent_files += sum(t.edit_call_count + t.shell_write_count + t.shell_change_count for t in _priced(sub))
        cycle.facts = {
            "plan_now": any(t.plan_stats is not None for t in turns),
            "plan_before": approved,
            "skills": sum(len(t.skills_invoked) for t in turns),
            "files": edits,
            "agent_files": agent_files,
            "shell_changes": sum(t.shell_write_count + t.shell_change_count for t in turns),
            "tests": widest(t.tests_run for t in turns),
            "docs_only": edits > 0 and sum(t.edit_doc_count for t in turns) == edits,
            "earlier": before + n,
            "correction": opening.human_correction,
            "adjust": opening.human_adjust,
            "same_files": bool(hashes & last),
            "tool_errors": sum(t.tool_error_count for t in turns),
            "admit_candidate": any(t.admit_candidate for t in turns),
        }
        approved = approved or any(_plan_approved(t) for t in turns)
        last = hashes


#: What can come right before a turn that carries on a reply: Claude's own
#: tool results and the harness notes around them, never a message, a
#: queued message, an interruption or another report. The turn that read
#: the report and every turn after it that runs for one of these is one
#: reply.
_REPLY_CONTINUES = frozenset(
    {
        EventKind.TOOL_RESULT,
        EventKind.ATTACHMENT,
        EventKind.REMINDER,
        EventKind.HOOK_OUTPUT,
        EventKind.TOOL_DENIAL,
        EventKind.CONTEXT_INJECT,
        EventKind.META,
        EventKind.UNKNOWN,
        EventKind.API_ERROR,
        EventKind.TASK_STATUS,
    }
)


def _hand_off_replies(top, turns, starts, cycles, cycle_of_use, subs, launches) -> None:
    """A reply to an agent's report is about that agent's work, so its tag
    and its cost belong to the cycle whose call launched the agent, not to
    the cycle that happened to be open when the report arrived. The report
    is a task notification (``task_id`` is the background agent's id, or
    the workflow's task id); the reply starts at the first turn after it,
    when nothing outranked it as the reason that turn ran
    (``preceding_primary``): a message of yours between them starts a
    cycle of its own and hands nothing off. It goes on through the turns
    that follow its tool calls (:data:`_REPLY_CONTINUES`) to the end of the
    cycle it ran in, because the answer and its ``[cg: ...]`` line come
    last. The first report a reply answers decides where it goes."""
    launched = {
        agent_key(sub.meta.agent_id): sub.meta.tool_use_id
        for sub in subs
        if sub.meta.agent_id and sub.meta.tool_use_id and sub.meta.kind != "workflow-agent"
    }
    stamps = launches.stamps
    handed: dict[int, int] = {}
    decided: set[int] = set()
    for event in top.events:
        if event.kind != EventKind.TASK_NOTIFICATION:
            continue
        task_id = event.detail.get("task_id")
        if not isinstance(task_id, str):
            continue
        use_id = launched.get(task_id) or launches.task_use(task_id)
        origin = cycle_of_use.get(use_id) if use_id else None
        moment = _moment(event.ts)
        if origin is None or moment is None:
            continue
        i = bisect.bisect_left(stamps, moment)
        if i >= len(turns) or i in decided or turns[i].preceding_primary != EventKind.TASK_NOTIFICATION:
            continue
        decided.add(i)
        n = bisect.bisect_right(starts, i) - 1
        if n <= origin:
            continue
        end = cycles[n].end
        j = i
        while True:
            handed[j] = origin
            j += 1
            if j >= end or turns[j].preceding_primary not in _REPLY_CONTINUES:
                break
    for i, origin in sorted(handed.items()):
        n = bisect.bisect_right(starts, i) - 1
        cycles[n].handed_off.add(i - cycles[n].start)
        cycles[origin].late_turns.append(turns[i])


@dataclass(slots=True)
class FeedbackSpan:
    """One /cg-feedback answer and the work it rates."""

    feedback: Feedback
    #: The cycle /cg-feedback ran in.
    run: Cycle
    #: The cycles it rates: those since the previous feedback, or since
    #: the session started. Empty when you ran it first thing.
    cycles: list[Cycle] = field(default_factory=list)


def cycle_feedback(cycle: Cycle) -> Feedback | None:
    """The feedback given in ``cycle``, or ``None``. A ``[cg-fb: ...]``
    tag counts only in a genuine /cg-feedback run (SEC-P1): elsewhere it
    could be forged or quoted reply text, so it's dropped. What each kind
    gave is merged across the turns (the second AskUserQuestion call's
    questions come on a later turn), then :func:`settle_feedback` decides:
    the answers read from the skill's questions outrank its tag, which
    adds only a word for a key you answered in your own words."""
    genuine = is_feedback_run(cycle)
    by_source: dict[str, Feedback] = {}
    for turn in cycle.turns:
        fb = turn.feedback
        if fb is None or (fb.source == "tag" and not genuine):
            continue
        earlier = by_source.get(fb.source)
        by_source[fb.source] = fb if earlier is None else merge_feedback(earlier, fb)
    return settle_feedback(by_source.get("answers"), by_source.get("tag"), by_source.get("skipped"))


def is_feedback_run(cycle: Cycle) -> bool:
    """Whether ``cycle`` is a /cg-feedback run: you ran the skill in its
    first turn. SEC-P1: this alone decides it -- it no longer also asks
    whether feedback was found, which let a forged ``[cg-fb: ...]`` tag
    manufacture a "feedback run" to hide behind."""
    return any(not catalogue.FEEDBACK_SKILL_NAMES.isdisjoint(turn.commands_run) for turn in cycle.turns[:1])


def _excluded_from_coverage(cycle: Cycle, all_turns: list[Turn]) -> bool:
    """CAP-10: a cycle coverage can't fairly judge by whether it ended
    with a ``[cg: ...]`` tag -- a /cg-feedback run (it answers /cg-
    feedback's own question, not the one an ordinary reply reports on),
    one whose last turn hit ``max_tokens`` before it could write its
    tag, or one cut off by an interruption before Claude could finish."""
    if is_feedback_run(cycle):
        return True
    if any(turn.stop_reason == "max_tokens" for turn in cycle.turns):
        return True
    return cycle.end < len(all_turns) and all_turns[cycle.end].preceding_primary == EventKind.INTERRUPT


def feedback_spans(cycles: list[Cycle]) -> list[FeedbackSpan]:
    """Each feedback in ``cycles`` (one session's, from
    :func:`prompt_cycles`) with the cycles it rates. A declined question
    ends a span too, so the next answer rates only what came after it. A
    run with no work since the previous one is a rerun to change that
    run's answers: it replaces them, over the same cycles, and a declined
    rerun leaves them as they were."""
    spans: list[FeedbackSpan] = []
    begin = 0
    for n, cycle in enumerate(cycles):
        fb = cycle_feedback(cycle)
        if fb is None:
            continue
        rated = [c for c in cycles[begin:n] if not is_feedback_run(c)]
        begin = n + 1
        if spans and not rated:
            if fb.source != "skipped":
                spans[-1] = FeedbackSpan(feedback=fb, run=cycle, cycles=spans[-1].cycles)
            continue
        spans.append(FeedbackSpan(feedback=fb, run=cycle, cycles=rated))
    return spans


def _cycle_for(sub, cycle_of_use, by_agent, by_workflow=None) -> int | None:
    """The cycle a subagent belongs to: the one whose turn started it,
    or its parent agent's, for a nested spawn. A workflow agent has no
    tool call to follow: ``by_workflow`` (``id(sub)`` -> cycle) says where
    its run was started."""
    seen = set()
    while sub is not None and id(sub) not in seen:
        seen.add(id(sub))
        if sub.meta.tool_use_id in cycle_of_use:
            return cycle_of_use[sub.meta.tool_use_id]
        if by_workflow and id(sub) in by_workflow:
            return by_workflow[id(sub)]
        sub = by_agent.get(agent_key(sub.meta.parent_agent_id)) if sub.meta.parent_agent_id else None
    return None


# -- measured use --------------------------------------------------------------


@dataclass(slots=True)
class ScopeUse:
    note_tokens: int = 0
    note_cost: float = 0.0
    tag_tokens: int = 0
    tag_cost: float = 0.0

    @property
    def cost(self) -> float:
        return self.note_cost + self.tag_cost


@dataclass(slots=True)
class CaptureUsage:
    """What capture cost from ``since`` on (every captured session when
    ``since`` is empty)."""

    since: str = ""
    #: Sessions and subagents that carried a capture note.
    sessions: int = 0
    subagents: int = 0
    notes: int = 0
    #: ``main``, ``subagent``, ``tool`` (notes after tool results),
    #: ``brief`` (the ``[spawn: ...]``/``[retry: ...]`` words a brief
    #: starts with), ``haiku`` (Claude Haiku's calls, while it writes
    #: the tags: their whole cost, as ``tag_cost``) and ``feedback`` (the
    #: plan check's and the rating reminder's notes and Claude's words
    #: for them).
    scopes: dict[str, ScopeUse] = field(default_factory=dict)
    #: metric id -> USD, the note and tag cost split by each metric's share.
    by_metric: dict[str, float] = field(default_factory=dict)
    #: metric id -> answers collected.
    answers: dict[str, int] = field(default_factory=dict)
    #: ISO date -> USD.
    daily: dict[str, float] = field(default_factory=dict)
    #: Everything the captured sessions cost, capture included.
    spend: float = 0.0
    cycles: int = 0
    tagged_cycles: int = 0
    #: Tagged cycles whose only tag Claude Haiku wrote (no reply carried
    #: one): the fallback's fills while Claude writes the tags.
    filled_cycles: int = 0
    reports: int = 0
    tagged_reports: int = 0
    #: Turns Claude Haiku tagged: the main session's replies while it
    #: writes the tags (``[capture] tagger = "haiku"``), the replies it
    #: filled in when Claude left them without a tag, and every agent
    #: run it judged, whoever writes them.
    judged: int = 0
    #: /cg-feedback runs, what they cost (every turn of each), and how
    #: many ended with answers rather than a declined question.
    feedback_runs: int = 0
    feedback_cost: float = 0.0
    feedback_answered: int = 0
    #: The plan check and the rating reminder (scope ``feedback``): notes
    #: that asked for either, plan checks Claude asked and how many you
    #: answered with one of the four words, and replies that ended with
    #: the reminder line.
    feedback_notes: int = 0
    plan_checks: int = 0
    plan_checks_answered: int = 0
    reminders: int = 0
    #: SURV-3: notes that land after a compact boundary -- the carried
    #: prefix a compaction would otherwise have discounted is gone, so
    #: these notes carry at the fuller, post-compaction rate. Counted
    #: separately (not folded into ``scopes``) so their cost shows as its
    #: own line rather than changing what "main"/"subagent"/"tool" mean.
    after_compact_notes: int = 0
    after_compact_cost: float = 0.0

    @property
    def note_tokens(self) -> int:
        return sum(s.note_tokens for s in self.scopes.values())

    @property
    def tag_tokens(self) -> int:
        return sum(s.tag_tokens for s in self.scopes.values())

    @property
    def cost(self) -> float:
        return sum(s.cost for s in self.scopes.values()) + self.feedback_cost

    @property
    def share(self) -> float | None:
        """Capture's percentage of what the captured sessions cost."""
        return 100.0 * self.cost / self.spend if self.spend > 0 else None

    @property
    def coverage(self) -> float | None:
        """Percentage of your messages whose answer ended with a tag."""
        return 100.0 * self.tagged_cycles / self.cycles if self.cycles else None

    @property
    def report_coverage(self) -> float | None:
        return 100.0 * self.tagged_reports / self.reports if self.reports else None

    def _add(self, scope: str, *, note_chars: int = 0, note_cost: float = 0.0, tag_chars: int = 0,
             tag_cost: float = 0.0, day: str = "") -> None:
        use = self.scopes.setdefault(scope, ScopeUse())
        use.note_tokens += round(note_chars / CHARS_PER_TOKEN)
        use.note_cost += note_cost
        use.tag_tokens += round(tag_chars / CHARS_PER_TOKEN)
        use.tag_cost += tag_cost
        if day:
            self.daily[day] = self.daily.get(day, 0.0) + note_cost + tag_cost

    def _split(self, weights: dict[str, float], cost: float) -> None:
        total = sum(weights.values())
        for metric_id, weight in weights.items():
            if total > 0:
                self.by_metric[metric_id] = self.by_metric.get(metric_id, 0.0) + cost * weight / total

    def _count(self, metric_id: str) -> None:
        self.answers[metric_id] = self.answers.get(metric_id, 0) + 1


def _day(ts: str | None) -> str:
    moment = _parse_ts(ts)
    return moment.astimezone(timezone.utc).date().isoformat() if moment is not None else ""


def _output_usd_per_char(turn: Turn | None, pricing: Pricing | None) -> float:
    if turn is None or pricing is None:
        return 0.0
    resolved = pricing.resolve_model(turn.model)
    rates = effective_rates(turn, resolved) if resolved is not None else None
    return rates.output / CHARS_PER_TOKEN / 1_000_000 if rates is not None else 0.0


def _note_weights(codes, scope: str) -> dict[str, float]:
    """Each metric's share of a note: the length of what it adds there."""
    weights = {}
    for code in codes:
        m = catalogue.METRICS_BY_ID.get(code)
        if m is None:
            continue
        if scope == "tool":
            weights[code] = len(m.tool_note)
        elif scope == "main":
            weights[code] = len(m.main_line) + len(m.main_extra)
        else:
            weights[code] = len(m.sub_line) + len(m.sub_extra)
    return {k: v for k, v in weights.items() if v > 0}


def _tag_weights(turn: Turn, subagent: bool) -> dict[str, float]:
    """The metrics a tag answered, weighted by what each usually costs."""
    weights: dict[str, float] = {}
    tag = turn.cap
    fields = SUB_TAG_FIELDS if subagent else MAIN_TAG_FIELDS
    if tag is not None:
        for name, metric_id in fields.items():
            value = getattr(tag, name, None)
            # A retired metric (CAP-5: detour, web) still parses from an
            # older transcript but has no catalogue entry to weigh it by.
            metric = catalogue.METRICS_BY_ID.get(metric_id)
            if value not in (None, ()) and metric is not None:
                # An agent metric Haiku judges costs Claude nothing to write:
                # its share of Haiku's call is even.
                weights[metric_id] = metric.out_chars or 1
    if subagent and turn.result_marker:
        weights["result"] = catalogue.METRICS_BY_ID["result"].out_chars or 1
    return weights


def _segment_ends(result: TranscriptResult, carry: _Carry) -> list[int]:
    """Turn indexes where a compaction drops what was carried."""
    return sorted(
        carry.index_at(event.ts) for event in result.events if event.kind == EventKind.COMPACT_BOUNDARY and event.ts
    )


def _add_notes(use: CaptureUsage, result: TranscriptResult, carry: _Carry, subagent: bool, since) -> None:
    ends = _segment_ends(result, carry)
    for event in result.events:
        if event.subkind != "capture_note" or not event.size_chars:
            continue
        moment = _parse_ts(event.ts)
        if since is not None and (moment is None or moment < since):
            continue
        hook_event = event.detail.get("hook")
        scope = "tool" if hook_event == "PostToolUse" else "subagent" if subagent else "main"
        start = carry.index_at(event.ts)
        end = next((e for e in ends if e > start), len(carry.turns))
        cost = carry.cost(event.size_chars, start, end)
        use.notes += 1
        use._add(scope, note_chars=event.size_chars, note_cost=cost, day=_day(event.ts))
        use._split(_note_weights(event.detail.get("codes", ()), scope), cost)
        # SURV-3: a boundary at or before this note's own turn means at
        # least one compaction already ran by the time it landed.
        if ends and ends[0] <= start:
            use.after_compact_notes += 1
            use.after_compact_cost += cost


@dataclass(slots=True)
class CoachingUsage:
    """What coaching notes (``coaching_notes``) cost from ``since`` on:
    each note carried in context until the next summary or the
    transcript's end, as a capture note is priced. Claude's one-line
    mention of a hint, when it makes one, isn't counted."""

    since: str = ""
    #: Sessions with a coaching note, in their main transcript or a subagent.
    sessions: int = 0
    notes: int = 0
    note_tokens: int = 0
    cost: float = 0.0
    #: Hint (``capture_catalogue.COACHING_HINTS``, or "other") -> notes.
    by_kind: dict[str, int] = field(default_factory=dict)
    #: Everything those sessions cost from ``since`` on.
    spend: float = 0.0

    @property
    def share(self) -> float | None:
        return 100.0 * self.cost / self.spend if self.spend > 0 else None


#: Kinds of a note under the coaching marker that aren't coaching: the
#: plan check, the rating reminder and the facts line a /cg-feedback run
#: starts with.
_FEEDBACK_NOTE_KINDS = frozenset((*catalogue.FEEDBACK_HINTS, "feedback_facts"))


def _coaching_notes(result: TranscriptResult):
    """``(ts, chars, kind)`` per coaching note in ``result``, alone or
    sharing an attachment with a capture note."""
    for event in result.events:
        if event.subkind == "coaching_note" and event.size_chars:
            kind = event.detail.get("kind") or "other"
            # The feedback notes are priced with their own metrics
            # (:func:`_add_feedback_hints`), and the facts line with the
            # /cg-feedback run it opens.
            if kind not in _FEEDBACK_NOTE_KINDS:
                yield event.ts, event.size_chars, kind
        elif event.subkind == "capture_note" and event.detail.get("coach_chars"):
            yield event.ts, event.detail["coach_chars"], event.detail.get("coach") or "other"


def coaching_usage(corpus, pricing: Pricing | None, since: str = "") -> CoachingUsage:
    """What coaching notes cost across ``corpus`` from ``since`` (an ISO
    time) on, whatever the capture level."""
    use = CoachingUsage(since=since)
    start = _start(since)
    for bundle in corpus.sessions:
        found = False
        results = ([bundle.top] if bundle.top is not None else []) + list(bundle.subs)
        for result in results:
            notes = [
                (ts, chars, kind) for ts, chars, kind in _coaching_notes(result)
                if start is None or ((moment := _parse_ts(ts)) is not None and moment >= start)
            ]
            if not notes:
                continue
            found = True
            carry = _Carry(result, pricing)
            ends = _segment_ends(result, carry)
            for ts, chars, kind in notes:
                at = carry.index_at(ts)
                end = next((e for e in ends if e > at), len(carry.turns))
                use.notes += 1
                use.note_tokens += round(chars / CHARS_PER_TOKEN)
                use.cost += carry.cost(chars, at, end)
                use.by_kind[kind] = use.by_kind.get(kind, 0) + 1
        if found:
            use.sessions += 1
            use.spend += sum(_spend(result, pricing, start) for result in results)
    return use


def _add_tags(use: CaptureUsage, result: TranscriptResult, carry: _Carry, pricing, subagent: bool, since) -> None:
    ends = _segment_ends(result, carry)
    for turn in _priced(result):
        moment = _parse_ts(turn.ts)
        if since is not None and (moment is None or moment < since):
            continue
        if turn.cap is not None and turn.cap.judged:
            # Haiku's call is the whole cost: no reply carried the tag.
            use.judged += 1
            use._add("haiku", tag_cost=turn.cap.judge_usd, day=_day(turn.ts))
            weights = _tag_weights(turn, subagent)
            use._split(weights, turn.cap.judge_usd)
            for metric_id in weights:
                use._count(metric_id)
            continue
        chars = turn.cap.chars if turn.cap is not None else 0
        if subagent and turn.result_marker and (turn.cap is None or not turn.cap.chars):
            chars = len(f"[result: {turn.result_marker}]")
        if not chars:
            continue
        write_chars = chars + 1
        cost = write_chars * _output_usd_per_char(turn, pricing)
        # CAP-2: the tag was Claude's own output on this turn (priced
        # above), but the words stay in the transcript and get carried
        # -- cache-written into the next turn's prompt, then cache-read
        # on every turn after that until the next compaction.
        start = carry.index_at(turn.ts) + 1
        end = next((e for e in ends if e > start), len(carry.turns))
        cost += carry.cost(write_chars, start, end)
        use._add("subagent" if subagent else "main", tag_chars=write_chars, tag_cost=cost, day=_day(turn.ts))
        weights = _tag_weights(turn, subagent)
        use._split(weights, cost)
        for metric_id in weights:
            use._count(metric_id)


def _add_brief_markers(use: CaptureUsage, sub: TranscriptResult, spawner: Turn | None, pricing, since) -> None:
    first = next(iter(_priced(sub)), None)
    if first is None:
        return
    moment = _parse_ts(first.ts)
    if since is not None and (moment is None or moment < since):
        return
    # A retry Haiku judged was never written into the brief: its cost is
    # Haiku's call, counted with the run's other words.
    judged = any(turn.cap is not None and turn.cap.judged for turn in sub.turns)
    for metric_id, word in (("spawn", first.spawn_marker), ("retry", first.retry_marker)):
        if not word:
            continue
        if judged:
            use._count(metric_id)
            continue
        chars = len(f"[{metric_id}: {word}]") + 1
        cost = chars * _output_usd_per_char(spawner or first, pricing)
        use._add("brief", tag_chars=chars, tag_cost=cost, day=_day(first.ts))
        use._split({metric_id: 1.0}, cost)
        use._count(metric_id)


def _spend(result: TranscriptResult, pricing, since) -> float:
    if pricing is None:
        return 0.0
    total = 0.0
    for turn in _priced(result):
        moment = _parse_ts(turn.ts)
        if since is not None and (moment is None or moment < since):
            continue
        total += price_turn(turn, pricing.resolve_model(turn.model)).total
    return total


@dataclass(frozen=True, slots=True)
class CycleSpend:
    """What one cycle's work is charged: its own replies, less the ones
    that answered an earlier cycle's agents (``Cycle.handed_off``), the
    replies to its own agents' reports that ran in later cycles
    (``Cycle.late_turns``), and every turn of the subagents and workflow
    agents it started. Over a whole session each turn is charged once, so
    the cycles' charges add up to the session's cost."""

    #: USD at list price; ``0.0`` with no price table.
    cost: float = 0.0
    #: Part of ``cost``: later replies to this cycle's agents' reports.
    moved_in: float = 0.0
    #: Not in ``cost``: this cycle's own replies to earlier cycles' agents,
    #: charged to the cycle that started them.
    moved_out: float = 0.0
    #: Tokens (input, cache writes, cache reads and output) of the main
    #: session's replies charged to this cycle, the same replies as
    #: ``cost`` less the agents.
    tokens: int = 0
    #: The tokens of the agents' turns.
    agent_tokens: int = 0
    #: How many main-session replies are charged (the turns behind ``tokens``).
    replies: int = 0


def _turn_tokens(turn: Turn) -> int:
    return turn.input_tokens + turn.cache_creation_tokens + turn.cache_read_tokens + turn.output_tokens


def cycle_spend(cycle: Cycle, pricing: Pricing | None) -> CycleSpend:
    """:class:`CycleSpend` of ``cycle``: the one place a cycle's cost is
    worked out, so :func:`usage`, ``habits`` and ``pieces`` agree on what
    a reply to an agent's report cost and whose it was. The reply keeps
    its place in ``cycle.turns``, the timeline."""
    agents = [turn for sub in cycle.subs for turn in _priced(sub)]
    moved_out = [turn for n, turn in enumerate(cycle.turns) if n in cycle.handed_off]
    own = [turn for n, turn in enumerate(cycle.turns) if n not in cycle.handed_off]
    charged = own + cycle.late_turns

    def usd(turns) -> float:
        if pricing is None:
            return 0.0
        return sum(price_turn(turn, pricing.resolve_model(turn.model)).total for turn in turns)

    moved_in = usd(cycle.late_turns)
    return CycleSpend(
        cost=usd(own) + moved_in + usd(agents),
        moved_in=moved_in,
        moved_out=usd(moved_out),
        tokens=sum(_turn_tokens(t) for t in charged),
        agent_tokens=sum(_turn_tokens(t) for t in agents),
        replies=len(charged),
    )


def _cycle_cost(cycle: Cycle, pricing) -> float:
    return cycle_spend(cycle, pricing).cost


def _add_feedback_runs(use: CaptureUsage, top: TranscriptResult, subs, pricing, since, workflows=()) -> bool:
    """Price this session's /cg-feedback runs; ``True`` when it had any."""
    found = False
    for cycle in prompt_cycles(top, subs, workflows):
        if not is_feedback_run(cycle):
            continue
        moment = _parse_ts(cycle.turns[0].ts) if cycle.turns else None
        if since is not None and (moment is None or moment < since):
            continue
        found = True
        cost = _cycle_cost(cycle, pricing)
        use.feedback_runs += 1
        use.feedback_cost += cost
        use.by_metric["feedback_skill"] = use.by_metric.get("feedback_skill", 0.0) + cost
        day = _day(cycle.turns[0].ts)
        if day:
            use.daily[day] = use.daily.get(day, 0.0) + cost
        fb = cycle_feedback(cycle)
        if fb is not None and fb.source != "skipped":
            use.feedback_answered += 1
            use._count("feedback_skill")
    return found


def _add_feedback_hints(use: CaptureUsage, result: TranscriptResult, pricing, since) -> bool:
    """Price the plan check and the rating reminder in one main
    transcript, whatever the capture level: each note that asked for
    either, carried until the next summary as a capture note is; the
    question Claude asked for the plan check, and the reminder line a
    reply ended with, as output at that turn's rate and then carried on
    (as a tag is). Each goes to its own metric and to scope ``feedback``.
    ``True`` when the transcript had any."""
    notes = [
        event for event in result.events
        if event.subkind == "coaching_note"
        and event.size_chars
        and event.detail.get("kind") in catalogue.FEEDBACK_NOTE_METRIC
    ]
    turns = [turn for turn in _priced(result) if turn.plan_check is not None or turn.coach_reminder]
    if not notes and not turns:
        return False
    carry = _Carry(result, pricing)
    ends = _segment_ends(result, carry)

    def counted(ts) -> bool:
        if since is None:
            return True
        moment = _parse_ts(ts)
        return moment is not None and moment >= since

    def carried(ts, chars: int, after: int = 0) -> float:
        start = carry.index_at(ts) + after
        return carry.cost(chars, start, next((e for e in ends if e > start), len(carry.turns)))

    found = False
    for event in notes:
        if not counted(event.ts):
            continue
        found = True
        cost = carried(event.ts, event.size_chars)
        metric_id = catalogue.FEEDBACK_NOTE_METRIC[event.detail["kind"]]
        use.feedback_notes += 1
        use._add("feedback", note_chars=event.size_chars, note_cost=cost, day=_day(event.ts))
        use.by_metric[metric_id] = use.by_metric.get(metric_id, 0.0) + cost
    for turn in turns:
        if not counted(turn.ts):
            continue
        found = True
        for metric_id, chars, here in (
            ("plan_check", catalogue.PLAN_CHECK_ASK_CHARS, turn.plan_check is not None),
            ("feedback_reminder", catalogue.REMINDER_REPLY_CHARS, turn.coach_reminder),
        ):
            if not here:
                continue
            # Output at this turn's rate; the words then stay in context
            # from the next turn on, as a tag's do.
            cost = chars * _output_usd_per_char(turn, pricing) + carried(turn.ts, chars, 1)
            use._add("feedback", tag_chars=chars, tag_cost=cost, day=_day(turn.ts))
            use.by_metric[metric_id] = use.by_metric.get(metric_id, 0.0) + cost
            if metric_id == "plan_check":
                use.plan_checks += 1
                if turn.plan_check.word:
                    use.plan_checks_answered += 1
                    use._count("plan_check")
            else:
                use.reminders += 1
    return found


def is_captured(result) -> bool:
    """Whether a main transcript or a subagent's carried a capture note, or
    Claude Haiku tagged one of its turns: what :func:`usage` counts as a
    captured session, so every figure about "your messages" counts the
    same ones."""
    return result.meta.cap_injections > 0 or any(t.cap is not None and t.cap.judged for t in result.turns)


def _start(since: str):
    start = _parse_ts(since) if since else None
    if start is not None and start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    return start


def feedback_usage(corpus, pricing: Pricing | None, since: str = "") -> CaptureUsage:
    """Only the /cg-feedback runs in ``corpus`` from ``since`` on, with
    the plan check and the rating reminder: what they cost and how many
    were answered. For the Capture tab, which shows them whatever the
    capture level."""
    use = CaptureUsage(since=since)
    start = _start(since)
    for bundle in corpus.sessions:
        if bundle.top is None:
            continue
        rated = _add_feedback_runs(use, bundle.top, bundle.subs, pricing, start, getattr(bundle, "workflows", ()))
        hinted = _add_feedback_hints(use, bundle.top, pricing, start)
        if rated or hinted:
            use.spend += _spend(bundle.top, pricing, start)
    return use


def usage(corpus, pricing: Pricing | None, since: str = "") -> CaptureUsage:
    """What capture cost across ``corpus`` from ``since`` (an ISO time)
    on. A session counts once its main transcript carries a capture note;
    its subagents count with it. /cg-feedback runs, plan checks and rating
    reminders count in any session."""
    use = CaptureUsage(since=since)
    start = _start(since)
    for bundle in corpus.sessions:
        top = bundle.top
        # A session Haiku tagged counts even when its note asked Claude
        # for nothing (no retry or reminder line): there was none.
        captured_top = top is not None and is_captured(top)
        subs = [sub for sub in bundle.subs if captured_top or is_captured(sub)]
        workflows = getattr(bundle, "workflows", ())
        rated = top is not None and _add_feedback_runs(use, top, bundle.subs, pricing, start, workflows)
        # The plan check and the reminder work at every level too.
        hinted = top is not None and _add_feedback_hints(use, top, pricing, start)
        if (rated or hinted) and not captured_top:
            # Its spend, so capture's share stays a share of what the
            # sessions it cost anything in spent.
            use.spend += _spend(top, pricing, start)
        if not captured_top and not subs:
            continue
        spawners: dict[str, Turn] = {}
        for result in ([top] if captured_top else []) + list(bundle.subs):
            for turn in result.turns:
                for tool_use_id in turn.tool_use_ids:
                    spawners[tool_use_id] = turn
        if captured_top:
            use.sessions += 1
            carry = _Carry(top, pricing)
            _add_notes(use, top, carry, False, start)
            _add_tags(use, top, carry, pricing, False, start)
            use.spend += _spend(top, pricing, start)
            top_turns = _priced(top)
            for cycle in prompt_cycles(top, bundle.subs, workflows):
                moment = _parse_ts(cycle.turns[0].ts) if cycle.turns else None
                if start is not None and (moment is None or moment < start):
                    continue
                if _excluded_from_coverage(cycle, top_turns):
                    continue
                use.cycles += 1
                tag = cycle.tag
                if tag is not None:
                    use.tagged_cycles += 1
                    if tag.judged and not tag.chars:
                        use.filled_cycles += 1
        for sub in subs:
            use.subagents += 1
            carry = _Carry(sub, pricing)
            _add_notes(use, sub, carry, True, start)
            _add_tags(use, sub, carry, pricing, True, start)
            _add_brief_markers(use, sub, spawners.get(sub.meta.tool_use_id or ""), pricing, start)
            use.spend += _spend(sub, pricing, start)
            turns = _priced(sub)
            if sub.meta.agent_type in catalogue.SKIP_AGENT_TYPES or not turns:
                continue
            moment = _parse_ts(turns[0].ts)
            if start is not None and (moment is None or moment < start):
                continue
            use.reports += 1
            if any(turn.result_marker for turn in turns):
                use.tagged_reports += 1
    return use


# -- estimates from your own history ---------------------------------------------


@dataclass(slots=True)
class History:
    """What one character costs in each place capture would put it,
    summed over the sessions replayed (see :func:`history`)."""

    days: int = 0
    sessions: int = 0
    cycles: int = 0
    subagents: int = 0
    #: Notes a level would inject: one per session start and compaction.
    main_notes: int = 0
    sub_notes: int = 0
    #: USD per note character, summed over every injection.
    main_note: float = 0.0
    sub_note: float = 0.0
    #: The same for Explore and Plan agents, whose note leaves out
    #: ``rules`` and ``agent_brief``.
    sub_note_no_rules: float = 0.0
    #: USD per tag character: one per prompt cycle, per subagent report,
    #: per brief.
    reply_tag: float = 0.0
    report_tag: float = 0.0
    brief_tag: float = 0.0
    #: Tool results a Deep tool note would follow, what one character of
    #: that note costs carried onward, and one character of the word
    #: Claude adds to its tag.
    big_outputs: int = 0
    big_output_note: float = 0.0
    big_output_tag: float = 0.0
    web_results: int = 0
    web_note: float = 0.0
    web_tag: float = 0.0
    #: Plans approved in the replayed sessions: the most times the plan check
    #: could ask (once per plan).
    plans_approved: int = 0
    #: What the replayed sessions cost.
    spend: float = 0.0


def _carry_per_char(carry: _Carry, start: int, end: int) -> float:
    return carry.cost(1_000_000, start, end) / 1_000_000


def _tag_carry_per_char(carry: _Carry, ends: list[int], ts: str | None) -> float:
    """CAP-2: USD per character of a reply/report/big-output/web tag,
    carried from the turn after it was written until the next
    compaction. The pre-enable estimate path (:func:`history`,
    :func:`_replay_notes`)'s counterpart to :func:`_add_tags`'s own
    ``carry.cost(write_chars, start, end)``, which prices this same
    carry in the real, post-hoc :func:`usage`."""
    start = carry.index_at(ts) + 1
    end = next((e for e in ends if e > start), len(carry.turns))
    return _carry_per_char(carry, start, end)


def _segments(result: TranscriptResult, carry: _Carry) -> list[tuple[int, int]]:
    """``(start, end)`` turn ranges between compactions: a note is
    injected at each start."""
    cuts = [0] + [e for e in _segment_ends(result, carry) if 0 < e < len(carry.turns)]
    cuts = sorted(set(cuts))
    return [(s, e) for s, e in zip(cuts, cuts[1:] + [len(carry.turns)]) if e > s]


def _big_output_calls(turn: Turn) -> int:
    """How many of this turn's tool calls are estimated to have crossed
    Deep's big-output threshold (CAP-10) among the tools the hook is run
    after (:data:`~claudeglass.capture_catalogue.BIG_OUTPUT_TOOLS`: not
    the shell or MCP tools): Claude Code fires the note
    once per matching *call*, but ``tool_result_chars_by_tool`` only
    totals a tool's calls for the turn, so a turn with several big
    calls of the same tool is estimated as that total split evenly
    across its calls, rather than counted as one note regardless of how
    many actually crossed it."""
    threshold = catalogue.BIG_OUTPUT_TOKENS * CHARS_PER_TOKEN
    total = 0
    for name, chars in turn.tool_result_chars_by_tool.items():
        if chars < threshold or name not in catalogue.BIG_OUTPUT_TOOLS:
            continue
        calls = turn.tool_calls_by_tool.get(name, 1)
        total += min(calls, chars // threshold)
    return total


def _web_calls(turn: Turn) -> int:
    return sum(turn.tool_calls_by_tool.get(tool, 0) for tool in catalogue.WEB_TOOLS)


def history(corpus, pricing: Pricing | None, days: int = 14) -> History:
    """Replay ``corpus`` (load it for the last ``days`` days) as if
    capture had been on for every session."""
    out = History(days=days)
    for bundle in corpus.sessions:
        top = bundle.top
        spawners: dict[str, Turn] = {}
        if top is not None and _priced(top):
            out.sessions += 1
            carry = _Carry(top, pricing)
            _replay_notes(out, top, carry, "main")
            top_ends = _segment_ends(top, carry)
            for turn in _priced(top):
                for tool_use_id in turn.tool_use_ids:
                    spawners[tool_use_id] = turn
            out.plans_approved += sum(1 for turn in _priced(top) if _plan_approved(turn))
            for cycle in prompt_cycles(top):
                out.cycles += 1
                tag_turn = cycle.turns[-1]
                out.reply_tag += _output_usd_per_char(tag_turn, pricing) + _tag_carry_per_char(
                    carry, top_ends, tag_turn.ts
                )
            out.spend += _spend(top, pricing, None)
        for sub in bundle.subs:
            turns = _priced(sub)
            out.spend += _spend(sub, pricing, None)
            if not turns or sub.meta.agent_type in catalogue.SKIP_AGENT_TYPES:
                continue
            out.subagents += 1
            carry = _Carry(sub, pricing)
            sub_ends = _segment_ends(sub, carry)
            scope = "no_rules" if sub.meta.agent_type in catalogue.NO_RULES_AGENT_TYPES else "sub"
            _replay_notes(out, sub, carry, scope)
            out.report_tag += _output_usd_per_char(turns[-1], pricing) + _tag_carry_per_char(
                carry, sub_ends, turns[-1].ts
            )
            # The brief marker ([spawn: ...]/[retry: ...]) isn't a
            # [cg:]/[result:] tag -- it's words inside the spawning tool
            # call's own prompt, not carried through capture.usage()'s
            # own accounting either (_add_brief_markers), so it stays at
            # its own output cost here too.
            out.brief_tag += _output_usd_per_char(spawners.get(sub.meta.tool_use_id or "", turns[0]), pricing)
    return out


def _replay_notes(out: History, result: TranscriptResult, carry: _Carry, scope: str) -> None:
    segments = _segments(result, carry)
    ends = _segment_ends(result, carry)
    for start, end in segments:
        per_char = _carry_per_char(carry, start, end)
        if scope == "main":
            out.main_notes += 1
            out.main_note += per_char
        else:
            out.sub_notes += 1
            if scope == "no_rules":
                out.sub_note_no_rules += per_char
            else:
                out.sub_note += per_char
    for start, end in segments:
        for index in range(start, end):
            turn = carry.turns[index]
            # A tool result arrives with the next turn, and the note with it.
            follow = min(index + 1, end - 1)
            if index + 1 >= end:
                continue
            big = _big_output_calls(turn)
            if big:
                out.big_outputs += big
                out.big_output_note += big * _carry_per_char(carry, follow, end)
                out.big_output_tag += big * (
                    _output_usd_per_char(carry.turns[follow], carry.pricing)
                    + _tag_carry_per_char(carry, ends, carry.turns[follow].ts)
                )
            web = _web_calls(turn)
            if web:
                out.web_results += web
                out.web_note += web * _carry_per_char(carry, follow, end)
                out.web_tag += web * (
                    _output_usd_per_char(carry.turns[follow], carry.pricing)
                    + _tag_carry_per_char(carry, ends, carry.turns[follow].ts)
                )


@dataclass(frozen=True, slots=True)
class Estimate:
    """What a set of metrics would have cost over the replayed days."""

    cost: float = 0.0
    #: Note tokens injected and tag tokens written over those days.
    note_tokens: int = 0
    tag_tokens: int = 0
    days: int = 0
    spend: float = 0.0

    @property
    def per_week(self) -> float:
        return self.cost * 7 / self.days if self.days else 0.0

    @property
    def share(self) -> float | None:
        return 100.0 * self.cost / self.spend if self.spend > 0 else None


def _note_chars(ids, scope: str, agent_type: str = "", tagger: str = catalogue.DEFAULT_TAGGER) -> int:
    text = catalogue.note_text(ids, scope, agent_type, tagger)
    return len(text) + _WRAP["SessionStart" if scope == "main" else "SubagentStart"] if text else 0


def estimate(past: History, ids, sample: int = 100, tagger: str = catalogue.DEFAULT_TAGGER) -> Estimate:
    """What the metrics in ``ids`` would have cost over ``past``, with
    ``sample`` percent of sessions captured. While Claude Haiku writes the
    tags (``tagger``), the main note carries no tag list and replies no
    tag; a Haiku call per message of yours
    (:data:`~claudeglass.capture_catalogue.JUDGE_USD_PER_CALL`) takes
    their place. The agent metrics are a Haiku call per agent run,
    whoever writes the tags."""
    ids = tuple(ids)
    wanted = set(ids)
    haiku = tagger == "haiku" and bool(catalogue.tagged_keys(ids))
    # Agent runs are judged by Haiku whoever writes the main session's
    # tags: a call per run, and nothing asked of the agent.
    agents = bool(catalogue.agent_metric_ids(ids))
    main = _note_chars(ids, "main", tagger=tagger)
    sub = _note_chars(ids, "subagent", "general-purpose")
    no_rules = _note_chars(ids, "subagent", "Explore")
    enabled = [catalogue.METRICS_BY_ID[i] for i in ids if i in catalogue.METRICS_BY_ID]
    reply = 0 if haiku else sum(m.out_chars for m in enabled if m.main_line)
    reply = reply + _TAG_FRAME_CHARS if reply else 0
    report = sum(m.out_chars for m in enabled if m.sub_line)
    report = report + _TAG_FRAME_CHARS if report else 0
    # A brief's [spawn:]/[retry:] words, per subagent (none now).
    brief = sum(m.out_chars for m in enabled if m.main_extra or m.sub_extra)
    cost = (
        main * past.main_note
        + sub * past.sub_note
        + no_rules * past.sub_note_no_rules
        + reply * past.reply_tag
        + report * past.report_tag
        + brief * past.brief_tag
    )
    note_tokens = main * past.main_notes + max(sub, no_rules) * past.sub_notes
    tag_tokens = reply * past.cycles + report * past.subagents + brief * past.subagents
    # The feedback notes: a note on one of your messages (carried like any
    # note), and the words Claude writes for it, at the rate of an
    # average reply. The reminder comes at most once in the rest period
    # (and once a session); the plan check once per approved plan.
    # Assumption: an upper bound, since not every plan gets edited nor
    # every piece reaches the reminder's size.
    th = catalogue.COACHING_THRESHOLDS
    per_cycle = past.reply_tag / past.cycles if past.cycles else 0.0
    per_note = past.main_note / past.main_notes if past.main_notes else 0.0
    for metric_id, hint, count in (
        ("feedback_reminder", "rating_reminder", min(past.sessions, past.days / th["rating_rest_days"])),
        ("plan_check", "plan_check", past.plans_approved),
    ):
        if metric_id not in wanted:
            continue
        note = len(catalogue.FEEDBACK_NOTE_TEXT[hint]) + catalogue.NOTE_WRAP_CHARS + len("UserPromptSubmit")
        out_chars = catalogue.METRICS_BY_ID[metric_id].out_chars
        cost += count * (note * per_note + out_chars * per_cycle)
        note_tokens += note * count
        tag_tokens += out_chars * count
    if haiku:
        cost += past.cycles * catalogue.JUDGE_USD_PER_CALL
    if agents:
        cost += past.subagents * catalogue.JUDGE_USD_PER_CALL
    for metric_id, count, note, tag in (
        ("big_output", past.big_outputs, past.big_output_note, past.big_output_tag),
        ("web", past.web_results, past.web_note, past.web_tag),
    ):
        if metric_id in wanted:
            chars = (
                len(catalogue.tool_note_text(metric_id))
                + _WRAP["PostToolUse"]
                + catalogue.tool_suffix_chars(metric_id)
            )
            out_chars = catalogue.METRICS_BY_ID[metric_id].out_chars
            cost += chars * note + out_chars * tag
            note_tokens += chars * count
            tag_tokens += out_chars * count
    share = max(0, min(100, sample)) / 100
    return Estimate(
        cost=cost * share,
        note_tokens=round(note_tokens * share / CHARS_PER_TOKEN),
        tag_tokens=round(tag_tokens * share / CHARS_PER_TOKEN),
        days=past.days,
        spend=past.spend,
    )


def level_estimates(past: History, sample: int = 100, tagger: str = catalogue.DEFAULT_TAGGER) -> dict[str, Estimate]:
    """:func:`estimate` for each level from Free to Deep, with everything
    picking it turns on (:func:`~claudeglass.capture_catalogue.level_includes`)."""
    return {
        level: estimate(past, catalogue.level_includes(level), sample, tagger) for level in catalogue.LEVELS[1:]
    }


def metric_estimates(past: History, ids, sample: int = 100, tagger: str = catalogue.DEFAULT_TAGGER) -> dict[str, float]:
    """What each metric in ``ids`` adds to them, in USD over the replayed
    days: the estimate with it minus the estimate without it (and
    without what needs it). Its share of the note's fixed lines is
    included, so the parts don't sum exactly to the whole."""
    ids = tuple(ids)
    whole = estimate(past, ids, sample, tagger).cost
    out = {}
    for metric_id in ids:
        if metric_id not in catalogue.METRICS_BY_ID:
            continue
        without = tuple(
            i for i in ids if i != metric_id and metric_id not in catalogue.METRICS_BY_ID[i].requires
        )
        out[metric_id] = max(0.0, whole - estimate(past, without, sample, tagger).cost)
    return out


def enough_data(use: CaptureUsage, metric_id: str, signal_sessions: int = 0) -> tuple[int, int]:
    """``(answers collected, answers wanted)`` for one metric."""
    target = enough_target(metric_id)
    if metric_id in catalogue.METRICS_BY_ID and catalogue.METRICS_BY_ID[metric_id].group == "free":
        return signal_sessions, target
    return use.answers.get(metric_id, 0), target


def weeks_since(since: str, now: datetime | None = None) -> float | None:
    """Weeks between ``since`` (an ISO time, typically ``capture.
    enabled_at``) and ``now``. ``None`` without a parseable ``since``, or
    less than a day since it -- too little to spread a week's figure
    over."""
    start = _start(since)
    if start is None:
        return None
    days = ((now or datetime.now(timezone.utc)) - start).total_seconds() / 86400
    return days / 7 if days >= 1 else None


def weekly_cost(use: CaptureUsage, now: datetime | None = None) -> float | None:
    """What capture has cost a week, from ``use.cost`` spread over the
    time since ``use.since``. ``None`` without a start time to divide by,
    or less than a day since it (too little to price a week from)."""
    weeks = weeks_since(use.since, now)
    return use.cost / weeks if weeks else None


__all__ = [
    "CaptureUsage",
    "CoachingUsage",
    "Cycle",
    "ENOUGH",
    "Estimate",
    "FeedbackSpan",
    "HISTORY_DAYS",
    "History",
    "ScopeUse",
    "coaching_usage",
    "cycle_feedback",
    "enough_data",
    "enough_target",
    "estimate",
    "feedback_spans",
    "feedback_usage",
    "history",
    "is_feedback_run",
    "level_estimates",
    "metric_estimates",
    "prompt_cycles",
    "usage",
    "weekly_cost",
    "weeks_since",
]

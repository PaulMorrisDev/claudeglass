"""Quality signals: did the work go well, not just what it cost.

A cheaper model or a lower effort only saves money if the work still gets
done. This module measures, from the transcripts alone, the signs that it
didn't: agent runs that failed, were stopped or were cut off before they
answered; tool calls and shell commands that failed; tool calls you
denied; replies you stopped; your messages that corrected Claude; files
edited again after you had already replied; agent runs retried on a
larger model (the cheaper model wasn't enough); and replies cut off at
the output limit. Alongside them,
neutral measures of how much work a run took: replies, tool calls,
output tokens, time and cost per run.

One *run* is one transcript: a main session or one subagent run. Every
signal is a ratio of two counts summed over runs (tool errors over tool
calls, say), so a table can show both counts and a comparison can test
the difference properly.

Comparing two sets of runs (before and after a change, or one agent's
setups against each other) uses a two-sided z-test on the ratio with a
variance estimated from the runs themselves (the delta method for a
ratio of sums), so fifty tool errors in one bad run count as one bad run,
not fifty independent failures. With many signals tested at once some
will differ by chance, so the p-values within one comparison are
Holm-corrected: a difference is "worse" or "better" only when it holds
after the correction, "possibly worse"/"possibly better" when it only
holds on its own, and "no clear change" otherwise. With fewer than
:data:`MIN_RUNS` runs (or :data:`MIN_DENOMINATOR` of what a rate counts)
on either side, it is "too little data". A share that moved by less than
:data:`MIN_SHARE_CHANGE` is "no clear change" however many runs back
it: with thousands of tool calls, 0.04% against none is real but not
worth acting on.

What is kept: counts and flags only. Whether a message of yours looks
like a correction is a yes/no from a fixed phrase list (see
``events._CORRECTION_RE``); the text is never stored. Files are known
only by a salted hash.

Where agent outcomes come from: a background agent's task notification
carries its task id and status (``completed``, ``failed``, ``stopped``);
the agent's own transcript is ``agent-<task id>.jsonl``, so the two join
on the id. (A notification that arrives while Claude is mid-reply is
queued first, so queued lines are read too; the last status wins.) A
synchronous agent's tool result carries its agent id and
status. A workflow agent gets no notification; its outcome is its own
state in the finished run file (done, error, or still in progress when
the workflow ended, which counts as stopped). A run with none of these
has no recorded outcome and is left out of the outcome rates (but not
of the others).

Retried on a larger model: a run of the same agent type on a larger
model family (haiku, sonnet, opus, fable, smallest first), started in
the same session after this one's last reply, edited one of the files
(by salted hash) this one edited within :data:`RETRY_WINDOW`: whatever
dispatched it judged the cheaper model's work not enough. A different
agent type or the main session editing the file afterwards doesn't
count (a reviewer after a writer is often the plan), nor does a run
working alongside. Nothing records why the file was edited again, so
one retry is a sign and several are a pattern.

Markers, when Claude writes them (:data:`MARKER_LINES`, added to
CLAUDE.md from the quality quick action): a brief that starts
``[retry: model|brief|tools|other]`` says the agent is being run again
because its last run's work wasn't good enough, and why; a subagent's last
reply ending ``[result: done|partial|blocked]`` says whether it finished.
Only the word is kept. A retry that gives a reason is matched to the agent
run it retries: the latest one that ended before it started, within
:data:`RETRY_WINDOW`, preferring one whose files it edits and then one of
the same agent type. "model" on a larger model family counts as retried on
a larger model even for a different agent type or with no file in common;
"brief", "tools" and "other" mean the cheaper model wasn't the problem, so
that retry never counts against it. A run that says "partial" or
"blocked" didn't finish.

An edit is a file changed with Edit, Write, MultiEdit or NotebookEdit,
or written by a Bash or PowerShell command with content it authored
(``Turn.edit_target_hashes``); a failed edit tool call isn't one.

Retries stay out of the setup comparisons: the largest model can never
be retried on a larger one, so the test would favour it by construction.
They show in the tables, in before-and-after comparisons of one agent,
and through :func:`retried_models`.

Scheduled runs stay out of the setup comparisons too: a main session a
scheduled or looped task started, with no message of yours
(``Run.scheduled``), is usually a check that runs a command or two and
stops, a different job from the work you steer, and a few dozen of them
would otherwise become the main session's most-used setup. And a setup
is compared only when its runs' mean replies per run is within
:data:`COMPARABLE_SIZE` times the other's: a verdict between two-reply
checks and five-hundred-reply sessions would be about the work, not the
setup ("not comparable").
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Callable, Iterable

from . import capture_catalogue
from .fixes import _model_family
from .model import Column, EventKind, Section, Table, TranscriptResult, Turn, scheduled_main_session
from .pricing import Pricing, effective_rates, price_turn
from .workstyle import model_tier

if TYPE_CHECKING:
    from .units import Units

#: The group name of main-session runs; subagent runs are grouped by
#: agent type.
MAIN = "(main session)"
#: The pooled row of every subagent run.
ALL_AGENTS = "(all subagents)"

#: Runs needed on each side of a comparison.
MIN_RUNS = 5
#: Of what a rate counts (tool calls, messages...), needed on each side.
MIN_DENOMINATOR = 10
#: Two-sided significance level, before the Holm correction.
ALPHA = 0.05
#: The smallest move in a share (half a percentage point) worth a label.
MIN_SHARE_CHANGE = 0.005
#: Two setups are compared only when one's mean replies per run is within
#: this many times the other's.
COMPARABLE_SIZE = 5.0

#: How long after an agent run's last reply a run of the same agent on a
#: larger model editing the same files counts as a retry.
RETRY_WINDOW = timedelta(hours=2)
#: The share of an agent's runs on one model (of those that edited
#: files) retried on a larger model, from which that model is no longer
#: suggested for that agent.
RETRIED_SHARE = 0.10
#: Retried runs needed before the quality check suggests moving an agent
#: back up to the model it was retried on; with fewer it is a tip.
MIN_RETRIED_RUNS = 2

#: The CLAUDE.md lines that ask Claude for the markers (see the module
#: docstring). Kept short: they are sent with every session and most
#: subagents. The heading keeps the tool's name before 0.9.0: it is how
#: :mod:`quick_actions` finds the section already written into CLAUDE.md.
MARKER_HEADING = "## Token Lens markers"
MARKER_LINES = (
    f"{MARKER_HEADING}\n"
    "- When you start an agent again because its last run's work wasn't good enough, begin the new brief with "
    "[retry: model], [retry: brief], [retry: tools], [retry: scope] or [retry: other]: model if it needed a "
    "stronger model, brief if your instructions were unclear, tools if it lacked a tool or permission, scope if "
    "the task itself changed.\n"
    "- As a subagent, end your final reply with [result: done], [result: partial] or [result: blocked]."
)
#: About how many output tokens one marker costs ("[result: partial]" and
#: the line break before it).
MARKER_TOKENS = 6
#: The words the markers take, shared with metrics capture's parser.
RETRY_REASONS = capture_catalogue.RETRY_REASONS
RESULT_WORDS = capture_catalogue.RESULT_WORDS
#: Built-in agent types Claude Code starts without CLAUDE.md, so they never
#: see :data:`MARKER_LINES` (same list as ``recommend._SKIPS_CLAUDE_MD``).
_SKIPS_CLAUDE_MD = frozenset({"Explore", "Plan"})

_SHELL_TOOLS = ("Bash", "PowerShell")
#: Tools whose call is the agent's answer: a run that ends on one finished.
_ANSWER_TOOLS = frozenset({"StructuredOutput"})

#: Outcome statuses, normalised.
_OUTCOME = {
    "completed": "completed",
    "success": "completed",
    "failed": "failed",
    "error": "failed",
    "stopped": "stopped",
    "killed": "stopped",
    "cancelled": "stopped",
    "canceled": "stopped",
}


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


#: A workflow agent's end state in its finished run file, as an outcome.
#: "progress" in a finished run means the workflow ended while it ran.
_WORKFLOW_STATE = {"done": "completed", "error": "failed", "progress": "stopped"}


def _bare_id(agent_id: str | None) -> str:
    """``agent-abc`` and ``abc`` name the same agent."""
    return (agent_id or "").removeprefix("agent-")


# -- one run -----------------------------------------------------------------


@dataclass(slots=True)
class Run:
    """The counts for one transcript."""

    group: str = MAIN
    kind: str = "top-level"
    session_id: str = ""
    agent_id: str = ""
    start: datetime | None = None
    #: The model most of its replies used, and the effort most were sent at.
    model: str = ""
    effort: str = ""
    replies: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    shell_calls: int = 0
    shell_errors: int = 0
    denials: int = 0
    interrupts: int = 0
    api_errors: int = 0
    fallbacks: int = 0
    compactions: int = 0
    max_tokens: int = 0
    human_messages: int = 0
    corrections: int = 0
    edits: int = 0
    files_edited: int = 0
    #: Edits to a file already edited before your latest message.
    rework_edits: int = 0
    #: (time, file hash) of every edit, for :func:`_mark_retried`.
    edit_log: list = field(default_factory=list)
    #: The time of its last reply.
    end: datetime | None = None
    #: Subagents only: retried on a larger model (see the module
    #: docstring); how many of the files it edited the retry edited again
    #: soon after, and the model of the retry.
    retried: bool = False
    retried_files: int = 0
    retried_on: str = ""
    #: Subagents only: the reason the run that retried it gave (a
    #: :data:`RETRY_REASONS` word), whatever its model; "" when none did.
    retried_for: str = ""
    #: Subagents only: the markers it carried (see the module docstring):
    #: why its own brief said it was a retry, and what its last reply said
    #: about finishing. "" when absent.
    retry_marker: str = ""
    result_marker: str = ""
    #: What writing those markers cost, at the output rate of the model
    #: that wrote each (the main session's for a retry marker).
    retry_marker_cost: float = 0.0
    result_marker_cost: float = 0.0
    output_tokens: int = 0
    thinking_tokens: int = 0
    duration_s: float = 0.0
    cost: float = 0.0
    #: Subagents only: the last reply asked for a tool and never got to
    #: answer, or the run was stopped. None when that isn't known.
    cut_off: bool | None = None
    #: Subagents only: cut off after its last tool call's result came back,
    #: without being stopped. Nothing records why, but that is how a run
    #: ends when its ``maxTurns`` runs out (such runs cluster at round
    #: reply counts like 30, 60 and 200), so this is "likely ran out of turns".
    turn_limit: bool = False
    #: Subagents only: "completed", "failed", "stopped", "other", or None
    #: when no outcome was recorded.
    outcome: str | None = None
    #: The agent was ended early by Claude Code (a rate limit, say).
    terminated_early: bool = False
    #: Main session only: started by a scheduled or looped task, with no
    #: message of yours. Left out of the setup comparisons.
    scheduled: bool = False
    tool_errors_by_tool: dict = field(default_factory=dict)

    @property
    def is_agent(self) -> bool:
        return self.kind != "top-level"


def _dominant(values: Iterable[str]) -> str:
    counts = Counter(v for v in values if v)
    return counts.most_common(1)[0][0] if counts else ""


def _outcomes(transcripts: Iterable[TranscriptResult]) -> tuple[dict[str, str], set[str]]:
    """Agent id -> last reported status, and the ids ended early, from
    every task notification and agent result in a session."""
    status: dict[str, str] = {}
    terminated: set[str] = set()
    for result in transcripts:
        for event in result.events:
            detail = event.detail or {}
            if event.kind in (EventKind.TASK_NOTIFICATION, EventKind.AGENT_TERMINATED, EventKind.QUEUE_OPERATION):
                task_id = detail.get("task_id")
                if not task_id:
                    continue
                if event.kind == EventKind.AGENT_TERMINATED:
                    terminated.add(task_id)
                if detail.get("status"):
                    status[task_id] = str(detail["status"])
            elif event.kind == EventKind.TOOL_RESULT:
                for pair in detail.get("agents") or ():
                    if isinstance(pair, (list, tuple)) and len(pair) == 2 and pair[0] and pair[1]:
                        status[_bare_id(str(pair[0]))] = str(pair[1])
    return status, terminated


def run_facts(
    result: TranscriptResult,
    pricing: Pricing | None,
    *,
    outcomes: dict[str, str] | None = None,
    terminated: set[str] | None = None,
    session_id: str = "",
) -> Run:
    """The :class:`Run` for one transcript. ``outcomes``/``terminated``
    come from :func:`_outcomes` over the whole session."""
    meta = result.meta
    kind = meta.kind or "top-level"
    run = Run(
        group=MAIN if kind == "top-level" else (meta.agent_type or kind),
        kind=kind,
        session_id=session_id or meta.session_id,
        agent_id=_bare_id(meta.agent_id),
    )
    priced = [turn for turn in result.turns if turn.turn_index > 0]
    run.model = _dominant(turn.model for turn in priced)
    run.effort = _dominant(turn.effort or "" for turn in priced) or "default"
    times = [t for t in (_ts(turn.ts) for turn in priced) if t is not None]
    if times:
        run.start, run.end = min(times), max(times)
        run.duration_s = (run.end - run.start).total_seconds()
    edited_before: set[str] = set()
    edited_this_round: set[str] = set()
    for turn in result.turns:
        if turn.human_prompt_chars is not None and not run.is_agent:
            run.human_messages += 1
            run.corrections += int(turn.human_correction)
            edited_before |= edited_this_round
            edited_this_round = set()
        if turn.stop_reason == "max_tokens":
            run.max_tokens += 1
        calls = turn.tool_calls_by_tool or {}
        run.tool_calls += sum(calls.values()) if calls else len(turn.tool_use_ids)
        run.shell_calls += sum(calls.get(name, 0) for name in _SHELL_TOOLS)
        run.tool_errors += turn.tool_error_count
        run.shell_errors += sum((turn.tool_errors_by_tool or {}).get(name, 0) for name in _SHELL_TOOLS)
        for name, count in (turn.tool_errors_by_tool or {}).items():
            run.tool_errors_by_tool[name] = run.tool_errors_by_tool.get(name, 0) + count
        edited_at = _ts(turn.ts)
        for target in turn.edit_target_hashes:
            run.edits += 1
            if target in edited_before:
                run.rework_edits += 1
            edited_this_round.add(target)
            if edited_at is not None:
                run.edit_log.append((edited_at, target))
        if turn.turn_index > 0:
            run.replies += not turn.is_synthetic
            run.output_tokens += turn.output_tokens
            run.thinking_tokens += turn.thinking_tokens or 0
            if pricing is not None:
                run.cost += price_turn(turn, pricing.resolve_model(turn.model)).total
    run.files_edited = len(edited_before | edited_this_round)
    kinds = Counter(event.kind for event in result.events)
    run.denials = kinds[EventKind.TOOL_DENIAL]
    run.interrupts = kinds[EventKind.INTERRUPT]
    run.api_errors = kinds[EventKind.API_ERROR]
    run.fallbacks = kinds[EventKind.MODEL_FALLBACK]
    run.compactions = kinds[EventKind.COMPACT_BOUNDARY]
    run.scheduled = scheduled_main_session(result)
    if run.is_agent:
        # Cut off: stopped, never replied, or the last reply asked for a
        # tool and nothing came after it. A final StructuredOutput call is
        # a workflow agent's answer, not a cut-off. The last reply's own
        # stop_reason is often not recorded, so the tool calls decide.
        last = next((turn for turn in reversed(result.turns) if not turn.is_synthetic), None)
        if meta.stopped_by_user or last is None:
            run.cut_off = True
        else:
            run.cut_off = bool(last.tool_use_ids) and not set(last.tool_names) <= _ANSWER_TOOLS
            last_at = _ts(last.ts)
            run.turn_limit = run.cut_off and last_at is not None and any(
                event.kind == EventKind.TOOL_RESULT and (_ts(event.ts) or last_at) > last_at for event in result.events
            )
        status = (outcomes or {}).get(run.agent_id) or _WORKFLOW_STATE.get(meta.workflow_agent_state or "")
        if status is not None:
            run.outcome = _OUTCOME.get(status.lower(), "other")
        run.terminated_early = run.agent_id in (terminated or set())
        run.retry_marker = next((turn.retry_marker for turn in result.turns if turn.retry_marker), None) or ""
        run.result_marker = (last.result_marker or "") if last is not None else ""
    return run


def _edited_again(run: Run, other: Run) -> set[str]:
    """The files ``run`` edited that ``other`` edited within
    :data:`RETRY_WINDOW` of ``run``'s last reply."""
    files = {target for _at, target in run.edit_log}
    until = run.end + RETRY_WINDOW if run.end is not None else None
    return {target for at, target in other.edit_log if target in files and until is not None and at <= until}


def _declared_retries(runs: list[Run]) -> dict[int, int]:
    """Index of each agent run whose brief gave a retry reason -> index of
    the run it retries: the latest agent run that ended before it started,
    within :data:`RETRY_WINDOW`, preferring one whose files it edits, then
    one of the same agent type."""
    out: dict[int, int] = {}
    for j, other in enumerate(runs):
        if not other.is_agent or not other.retry_marker or other.start is None:
            continue
        best: tuple[tuple, int] | None = None
        for i, run in enumerate(runs):
            if (
                i == j
                or not run.is_agent
                or run.end is None
                or run.end > other.start
                or other.start - run.end > RETRY_WINDOW
            ):
                continue
            key = (bool(_edited_again(run, other)), run.group == other.group, run.end)
            if best is None or key > best[0]:
                best = (key, i)
        if best is not None:
            out[j] = best[1]
    return out


def _mark_retried(runs: list[Run]) -> None:
    """Set ``retried*`` on each agent run of one session that was retried
    on a larger model: the same agent type, started again on a larger
    model, edited one of its files within :data:`RETRY_WINDOW` of its last
    reply, unless that retry's brief gave a reason other than the model; or
    a retry on a larger model whose brief said the model was the reason
    (see the module docstring)."""
    tiers = [model_tier(run.model) for run in runs]
    for i, run in enumerate(runs):
        if not run.is_agent or run.end is None or not run.edit_log or tiers[i] < 0:
            continue
        retried: set[str] = set()
        most: tuple[int, Run] | None = None
        for j, other in enumerate(runs):
            if (
                tiers[j] <= tiers[i]
                or not other.is_agent
                or other.group != run.group
                or other.start is None
                or other.start < run.end
                or other.retry_marker not in ("", "model")
            ):
                continue
            hit = _edited_again(run, other)
            retried |= hit
            if hit and (most is None or len(hit) > most[0]):
                most = (len(hit), other)
        if most is not None:
            run.retried = True
            run.retried_files = len(retried)
            run.retried_on = most[1].model
    for j, i in _declared_retries(runs).items():
        run, other = runs[i], runs[j]
        run.retried_for = other.retry_marker
        if other.retry_marker == "model" and tiers[i] >= 0 and tiers[j] > tiers[i] and not run.retried:
            run.retried = True
            run.retried_files = len(_edited_again(run, other))
            run.retried_on = other.model


def session_runs(bundle, pricing: Pricing | None) -> list[Run]:
    """Every run in one session: the main session first, then its
    subagents, each with its outcome joined in and, when it was retried
    on a larger model, that marked."""
    transcripts = ([bundle.top] if bundle.top is not None else []) + list(bundle.subs)
    outcomes, terminated = _outcomes(transcripts)
    runs = [
        run_facts(result, pricing, outcomes=outcomes, terminated=terminated, session_id=bundle.session_id)
        for result in transcripts
    ]
    _mark_retried(runs)
    if pricing is not None:
        spawners = {
            tool_use_id: turn
            for result in transcripts
            for turn in result.turns
            for tool_use_id in turn.tool_use_ids
        }
        main_turns = [t for t in transcripts[0].turns if t.turn_index > 0] if runs and not runs[0].is_agent else []
        for run, result in zip(runs, transcripts):
            turns = [turn for turn in result.turns if turn.turn_index > 0]
            if run.result_marker:
                writer = next((t for t in reversed(turns) if t.result_marker), turns[-1] if turns else None)
                run.result_marker_cost = _marker_cost(pricing, writer)
            if run.retry_marker:
                # The retry word opens the brief, so the agent's parent
                # wrote it: the turn that started the agent, else the
                # main session's last turn, else the agent's own first.
                writer = spawners.get(result.meta.tool_use_id or "")
                writer = writer or (main_turns[-1] if main_turns else turns[0] if turns else None)
                run.retry_marker_cost = _marker_cost(pricing, writer)
    return runs


def _marker_cost(pricing: Pricing, turn: Turn | None) -> float:
    """A marker's output at the rate its writing turn was charged,
    fast mode included."""
    resolved = pricing.resolve_model(turn.model) if turn is not None and turn.model else None
    rates = effective_rates(turn, resolved) if resolved is not None else None
    return MARKER_TOKENS * rates.output / 1e6 if rates is not None else 0.0


def corpus_runs(corpus, pricing: Pricing | None) -> list[Run]:
    return [run for bundle in corpus.sessions for run in session_runs(bundle, pricing)]


# -- signals ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Signal:
    key: str
    label: str
    #: The per-run numerator and denominator; the signal is their sums' ratio.
    num: Callable[[Run], float]
    den: Callable[[Run], float]
    #: "pct" (a share, shown as a percentage) or "per_run" (a mean per run).
    kind: str
    #: "higher" when a rise is worse; None for a neutral measure of work.
    worse: str | None
    #: Which runs it applies to: "all", "main" or "agents".
    scope: str
    #: What the denominator counts, for "3 of 40 tool calls".
    of: str
    #: How a per-run value is formatted: "count", "tokens", "money", "minutes".
    unit: str = "count"


def _unfinished(run: Run) -> float:
    return float(
        run.outcome in ("failed", "stopped")
        or bool(run.cut_off)
        or run.terminated_early
        or run.result_marker in ("partial", "blocked")
    )


def _finish_known(run: Run) -> float:
    return float(
        run.outcome is not None or run.cut_off is not None or run.terminated_early or bool(run.result_marker)
    )


SIGNALS: tuple[Signal, ...] = (
    Signal("unfinished", "Agent runs that didn't finish", _unfinished, _finish_known, "pct", "higher", "agents",
           "agent runs"),
    Signal("failed", "Agent runs that reported failure", lambda r: float(r.outcome == "failed"),
           lambda r: float(r.outcome is not None), "pct", "higher", "agents", "agent runs with an outcome"),
    Signal("stopped", "Agent runs stopped", lambda r: float(r.outcome == "stopped"),
           lambda r: float(r.outcome is not None), "pct", "higher", "agents", "agent runs with an outcome"),
    Signal("turn_limit", "Agent runs that likely ran out of turns", lambda r: float(r.turn_limit),
           lambda r: float(r.cut_off is not None), "pct", "higher", "agents", "agent runs"),
    Signal("cut_off", "Agent runs cut off before answering", lambda r: float(bool(r.cut_off)),
           lambda r: float(r.cut_off is not None), "pct", "higher", "agents", "agent runs"),
    Signal("retried", "Agent runs retried on a larger model", lambda r: float(r.retried),
           lambda r: float(r.edits > 0 or r.retried), "pct", "higher", "agents", "agent runs that edited files"),
    Signal("tool_errors", "Tool calls that failed", lambda r: r.tool_errors, lambda r: r.tool_calls, "pct",
           "higher", "all", "tool calls"),
    Signal("shell_errors", "Shell commands that failed", lambda r: r.shell_errors, lambda r: r.shell_calls, "pct",
           "higher", "all", "shell commands"),
    Signal("denials", "Tool calls you denied", lambda r: r.denials, lambda r: r.tool_calls, "pct", "higher", "all",
           "tool calls"),
    Signal("interrupts", "Replies you stopped", lambda r: r.interrupts, lambda r: r.replies, "pct", "higher", "main",
           "replies"),
    Signal("corrections", "Your messages that corrected Claude", lambda r: r.corrections, lambda r: r.human_messages,
           "pct", "higher", "main", "messages"),
    Signal("rework", "Edits to files already changed before your last message", lambda r: r.rework_edits,
           lambda r: r.edits, "pct", "higher", "main", "edits"),
    Signal("max_tokens", "Replies cut off at the output limit", lambda r: r.max_tokens, lambda r: r.replies, "pct",
           "higher", "all", "replies"),
    Signal("api_errors", "API errors per reply", lambda r: r.api_errors, lambda r: r.replies, "pct", "higher", "all",
           "replies"),
    Signal("fallbacks", "Replies after a fallback to another model", lambda r: r.fallbacks, lambda r: r.replies, "pct",
           "higher", "all", "replies"),
    Signal("replies", "Replies per run", lambda r: r.replies, lambda r: 1.0, "per_run", None, "all", "runs"),
    Signal("tool_calls", "Tool calls per run", lambda r: r.tool_calls, lambda r: 1.0, "per_run", None, "all", "runs"),
    Signal("edits_per_file", "Edits per file edited", lambda r: r.edits, lambda r: r.files_edited, "per_run", None,
           "all", "files edited"),
    Signal("output", "Output tokens per run", lambda r: r.output_tokens, lambda r: 1.0, "per_run", None, "all", "runs",
           "tokens"),
    Signal("thinking_share", "Share of output spent thinking", lambda r: r.thinking_tokens, lambda r: r.output_tokens,
           "pct", None, "all", "output tokens"),
    Signal("minutes", "Minutes per run", lambda r: r.duration_s / 60.0, lambda r: 1.0, "per_run", None, "all", "runs",
           "minutes"),
    Signal("cost", "Cost per run", lambda r: r.cost, lambda r: 1.0, "per_run", None, "all", "runs", "money"),
)
SIGNAL_BY_KEY = {signal.key: signal for signal in SIGNALS}


def signals_for(group: str) -> list[Signal]:
    agents = group != MAIN
    return [s for s in SIGNALS if s.scope == "all" or (s.scope == "agents") == agents]


@dataclass(frozen=True, slots=True)
class Estimate:
    """A signal over a set of runs."""

    value: float | None
    #: Summed numerator and denominator.
    num: float
    den: float
    #: Runs that contributed to the denominator.
    runs: int
    #: Variance of ``value`` (None with fewer than two runs).
    variance: float | None


def estimate(signal: Signal, runs: list[Run]) -> Estimate:
    pairs = [(float(signal.num(run)), float(signal.den(run))) for run in runs]
    pairs = [(y, x) for y, x in pairs if x > 0]
    total_x = sum(x for _y, x in pairs)
    total_y = sum(y for y, _x in pairs)
    if total_x <= 0:
        return Estimate(None, total_y, total_x, len(pairs), None)
    ratio = total_y / total_x
    n = len(pairs)
    variance = None
    if n >= 2:
        variance = n / (n - 1) * sum((y - ratio * x) ** 2 for y, x in pairs) / total_x**2
    return Estimate(ratio, total_y, total_x, n, variance)


def _p_value(z: float) -> float:
    return math.erfc(abs(z) / math.sqrt(2.0))


def _enough(signal: Signal, est: Estimate) -> bool:
    if est.value is None or est.runs < MIN_RUNS:
        return False
    return signal.kind == "per_run" or est.den >= MIN_DENOMINATOR


def compare_runs(
    before: list[Run],
    after: list[Run],
    signals: Iterable[Signal],
    money: Callable[[float], str] | None = None,
) -> list[dict]:
    """One row per signal: the value on each side, the counts behind it,
    and a label (see the module docstring). Holm-corrected across the
    signals compared. ``money`` formats a cost (billing-mode units)."""
    rows = []
    for signal in signals:
        old, new = estimate(signal, before), estimate(signal, after)
        row = {
            "key": signal.key,
            "label": signal.label,
            "kind": signal.kind,
            "worse_when": signal.worse,
            "before": old.value,
            "after": new.value,
            "unit": signal.unit,
            "before_text": value_text(signal, old.value, money),
            "after_text": value_text(signal, new.value, money),
            "before_counts": counts_text(signal, old),
            "after_counts": counts_text(signal, new),
            "before_runs": old.runs,
            "after_runs": new.runs,
            "p": None,
            "label_key": "too_little_data",
        }
        if _enough(signal, old) and _enough(signal, new):
            variance = (old.variance or 0.0) + (new.variance or 0.0)
            diff = new.value - old.value
            if variance > 0:
                row["p"] = _p_value(diff / math.sqrt(variance))
            else:
                row["p"] = 1.0 if diff == 0 else 0.0
            row["label_key"] = "no_clear_change"
        rows.append(row)
    tested = sorted((r for r in rows if r["p"] is not None), key=lambda r: r["p"])
    m = len(tested)
    holding = True
    for rank, row in enumerate(tested):
        significant_alone = row["p"] < ALPHA
        holding = holding and row["p"] < ALPHA / (m - rank)
        if not significant_alone or (row["kind"] == "pct" and abs(row["after"] - row["before"]) < MIN_SHARE_CHANGE):
            continue
        rising = row["after"] > row["before"]
        if row["worse_when"] is None:
            direction = "higher" if rising else "lower"
            row["label_key"] = direction if holding else f"possibly_{direction}"
        else:
            direction = "worse" if rising else "better"
            row["label_key"] = direction if holding else f"possibly_{direction}"
    for row in rows:
        row["verdict"] = LABELS[row["label_key"]]
        if row["p"] is not None:
            row["p"] = round(row["p"], 4)
    return rows


LABELS = {
    "worse": "Worse",
    "better": "Better",
    "possibly_worse": "Possibly worse",
    "possibly_better": "Possibly better",
    "higher": "Higher",
    "lower": "Lower",
    "possibly_higher": "Possibly higher",
    "possibly_lower": "Possibly lower",
    "no_clear_change": "No clear change",
    "too_little_data": "Too little data",
}


def value_text(signal: Signal, value: float | None, money: Callable[[float], str] | None = None) -> str:
    if value is None:
        return "no data"
    if signal.kind == "pct":
        if value == 0:
            return "0%"
        if value < 0.001:
            return "under 0.1%"
        return f"{100.0 * value:.1f}%" if value < 0.1 else f"{100.0 * value:.0f}%"
    if signal.unit == "money":
        return money(value) if money is not None else f"${value:,.2f}"
    if signal.unit == "tokens":
        return f"{round(value):,} tokens"
    if signal.unit == "minutes":
        return f"{value:.1f} min"
    return f"{value:.1f}"


def counts_text(signal: Signal, est: Estimate) -> str:
    if signal.kind == "per_run":
        return f"{est.runs} {signal.of}" if signal.den(Run()) == 1.0 else f"{round(est.den):,} {signal.of}"
    return f"{round(est.num):,} of {round(est.den):,} {signal.of}"


def verdict(rows: list[dict]) -> str:
    """One line for a comparison's quality rows."""
    worse = [r for r in rows if r["label_key"] == "worse"]
    better = [r for r in rows if r["label_key"] == "better"]
    maybe = [r for r in rows if r["label_key"] == "possibly_worse"]
    tested = [r for r in rows if r["label_key"] != "too_little_data" and r["worse_when"]]
    if worse:
        return "Quality looks worse: " + "; ".join(
            f"{r['label'].lower()} rose from {r['before_text']} to {r['after_text']}" for r in worse
        ) + "."
    if better and not maybe:
        return "Quality looks better: " + "; ".join(
            f"{r['label'].lower()} fell from {r['before_text']} to {r['after_text']}" for r in better
        ) + "."
    if maybe:
        return "No clear sign of worse quality, but watch: " + "; ".join(
            f"{r['label'].lower()} ({r['before_text']} before, {r['after_text']} after)" for r in maybe
        ) + ". It may be chance; it firms up as more runs come in."
    if tested:
        return f"No clear change in quality on {len(tested)} signal{'s' if len(tested) != 1 else ''}."
    before = max((r["before_runs"] for r in rows), default=0)
    after = max((r["after_runs"] for r in rows), default=0)
    return (f"Too few runs to judge quality yet: {before} before and {after} after, and at least {MIN_RUNS} are "
            "needed on each side.")


# -- the report section ---------------------------------------------------------


def _groups(runs: list[Run]) -> list[tuple[str, list[Run]]]:
    by_group: dict[str, list[Run]] = {}
    for run in runs:
        by_group.setdefault(run.group, []).append(run)
    agents = sorted((g for g in by_group if g != MAIN), key=lambda g: (-len(by_group[g]), g))
    out = [(MAIN, by_group[MAIN])] if MAIN in by_group else []
    out += [(g, by_group[g]) for g in agents]
    agent_runs = [run for run in runs if run.is_agent]
    if len(agents) > 1:
        out.append((ALL_AGENTS, agent_runs))
    return out


def _value(signal: Signal, group: str, runs: list[Run]):
    applies = signal.scope == "all" or (signal.scope == "agents") == (group != MAIN)
    if not applies:
        return None
    value = estimate(signal, runs).value
    if value is None:
        return None
    return round(100.0 * value, 1) if signal.kind == "pct" else round(value, 4 if signal.unit == "money" else 1)


_BY_AGENT_SIGNALS = (
    "unfinished", "turn_limit", "retried", "tool_errors", "shell_errors", "denials", "interrupts", "corrections",
    "rework", "max_tokens", "replies", "cost",
)
_SETUP_SIGNALS = ("unfinished", "retried", "tool_errors", "shell_errors", "max_tokens", "replies", "tool_calls", "cost")


def _column(signal: Signal) -> Column:
    kind = "pct" if signal.kind == "pct" else ("money" if signal.unit == "money" else "float")
    return Column(key=f"{signal.key}_{'pct' if signal.kind == 'pct' else 'per_run'}", label=signal.label, kind=kind)


def _by_agent_table(runs: list[Run]) -> Table:
    signals = [SIGNAL_BY_KEY[key] for key in _BY_AGENT_SIGNALS]
    return Table(
        name="quality_by_agent",
        title="Quality signals by agent",
        columns=[Column(key="agent_type", label="Agent", kind="str"), Column(key="runs", label="Runs", kind="int")]
        + [_column(s) for s in signals],
        rows=[[group, len(group_runs)] + [_value(s, group, group_runs) for s in signals]
              for group, group_runs in _groups(runs)],
    )


def _counts_table(runs: list[Run]) -> Table:
    fields = (
        ("replies", "Replies"), ("tool_calls", "Tool calls"), ("tool_errors", "Failed tool calls"),
        ("shell_calls", "Shell commands"), ("shell_errors", "Failed shell commands"), ("denials", "Denied"),
        ("interrupts", "Stopped by you"), ("human_messages", "Your messages"), ("corrections", "Corrections"),
        ("edits", "Edits"), ("rework_edits", "Edits to already-changed files"),
        ("retried_files", "Files edited again on a larger model"),
        ("max_tokens", "Cut off at output limit"), ("api_errors", "API errors"), ("fallbacks", "Model fallbacks"),
        ("compactions", "Summaries"),
    )
    rows = []
    for group, group_runs in _groups(runs):
        outcomes = Counter(run.outcome for run in group_runs if run.is_agent)
        rows.append(
            [group, len(group_runs)]
            + [sum(getattr(run, key) for run in group_runs) for key, _label in fields]
            + [
                outcomes["completed"], outcomes["failed"], outcomes["stopped"], outcomes["other"], outcomes[None],
                sum(1 for run in group_runs if run.cut_off),
                sum(1 for run in group_runs if run.turn_limit),
                sum(1 for run in group_runs if run.retried),
                sum(1 for run in group_runs if run.terminated_early),
                sum(1 for run in group_runs if not run.replies),
            ]
            + [sum(1 for run in group_runs if run.result_marker == word) for word in RESULT_WORDS]
        )
    return Table(
        name="quality_counts",
        title="Quality signal counts",
        columns=[Column(key="agent_type", label="Agent", kind="str"), Column(key="runs", label="Runs", kind="int")]
        + [Column(key=key, label=label, kind="int") for key, label in fields]
        + [
            Column(key="outcome_completed", label="Reported done", kind="int"),
            Column(key="outcome_failed", label="Reported failure", kind="int"),
            Column(key="outcome_stopped", label="Stopped", kind="int"),
            Column(key="outcome_other", label="Other outcome", kind="int"),
            Column(key="outcome_unknown", label="No outcome recorded", kind="int"),
            Column(key="cut_off", label="Cut off", kind="int"),
            Column(key="turn_limit", label="Likely out of turns", kind="int"),
            Column(key="retried", label="Retried on a larger model", kind="int"),
            Column(key="terminated_early", label="Ended early", kind="int"),
            Column(key="never_replied", label="Never replied", kind="int"),
            Column(key="said_done", label="Said done", kind="int"),
            Column(key="said_partial", label="Said partly done", kind="int"),
            Column(key="said_blocked", label="Said blocked", kind="int"),
        ],
        rows=rows,
    )


def _setup(run: Run) -> tuple[str, str]:
    return run.model or "(unknown)", run.effort or "default"


def setup_rows(runs: list[Run], money: Callable[[float], str] | None = None) -> list[dict]:
    """Per agent (and the main session), per model and effort: the setup
    signals, and how each setup compares with the agent's most-used one.
    Runs with no reply have no model, and scheduled runs are a different
    job (see the module docstring), so both are left out here (the other
    tables count them). A setup whose runs are not :data:`COMPARABLE_SIZE`
    close in replies per run to the most-used one's is "not comparable".
    ``money`` (UX-2) formats a cost-unit signal's
    ``before_text``/``after_text`` for the report's billing mode; see
    :func:`compare_runs`."""
    out = []
    for group, group_runs in _groups(runs):
        if group == ALL_AGENTS:
            continue
        by_setup: dict[tuple[str, str], list[Run]] = {}
        for run in group_runs:
            if run.replies and not run.scheduled:
                by_setup.setdefault(_setup(run), []).append(run)
        if not by_setup:
            continue
        ordered = sorted(by_setup, key=lambda k: (-len(by_setup[k]), k))
        base = ordered[0]
        signals = [s for s in (SIGNAL_BY_KEY[k] for k in _SETUP_SIGNALS) if s in signals_for(group)]
        for setup in ordered:
            setup_runs = by_setup[setup]
            row = {
                "agent_type": group,
                "model": setup[0],
                "effort": setup[1],
                "runs": len(setup_runs),
                "values": {s.key: _value(s, group, setup_runs) for s in signals},
                "compared_with": "",
                "compared_model": "",
                "compared_effort": "",
                "difference": "",
                "setup_verdict": "only",
                "comparison": [],
            }
            if setup != base:
                row["compared_with"] = f"{base[0]}, effort {base[1]}"
                row["compared_model"], row["compared_effort"] = base
                size, base_size = _replies_per_run(setup_runs), _replies_per_run(by_setup[base])
                if max(size, base_size) > COMPARABLE_SIZE * min(size, base_size):
                    row["difference"] = (
                        f"Not compared: its runs averaged {size:,.1f} replies against {base_size:,.1f}, more "
                        f"than {COMPARABLE_SIZE:g} times apart, so a difference would be the work, not the setup."
                    )
                    row["setup_verdict"] = "not_comparable"
                    out.append(row)
                    continue
                # Not retries: the largest model can never be retried on a
                # larger one, so the test would favour it by construction.
                compared = [s for s in signals_for(group) if s.key != "retried"]
                comparison = compare_runs(by_setup[base], setup_runs, compared, money)
                row["comparison"] = comparison
                row["difference"] = _difference(comparison)
                row["setup_verdict"] = setup_verdict(comparison)
            elif len(ordered) > 1:
                row["difference"] = "The most-used setup; others are compared with it."
                row["setup_verdict"] = "baseline"
            out.append(row)
    return out


def _replies_per_run(runs: list[Run]) -> float:
    return sum(run.replies for run in runs) / len(runs)


#: ``setup_verdict`` values, strongest first: a setup is as bad as its
#: worst signal that has a direction, except that clearly worse on some
#: signals and clearly better on others is "mixed". "not_comparable" is
#: set by :func:`setup_rows` before any test, not by :func:`setup_verdict`.
SETUP_VERDICTS = (
    "worse",
    "mixed",
    "possibly_worse",
    "better",
    "possibly_better",
    "no_clear_difference",
    "too_little_data",
    "not_comparable",
)


def setup_verdict(rows: list[dict]) -> str:
    """One word for a comparison, from its signals that have a direction.
    Clearly worse on one signal and clearly better on another (more failed
    tool calls, but every run finished) is "mixed": no reason to switch
    either way."""
    keys = {r["label_key"] for r in rows if r["worse_when"]}
    if {"worse", "better"} <= keys:
        return "mixed"
    for key in ("worse", "possibly_worse", "better", "possibly_better"):
        if key in keys:
            return key
    return "no_clear_difference" if keys - {"too_little_data"} else "too_little_data"


def worse_models(setup_rows: Iterable[dict]) -> dict[tuple[str, str], dict]:
    """``{(agent, model family): row}`` for each ``quality_by_setup`` row
    whose setup did clearly worse than the agent's most-used one on a
    different model, so the models check doesn't suggest that model to
    that agent. The main session is ``"top-level"``, as in the model-swap
    table."""
    out: dict[tuple[str, str], dict] = {}
    for row in setup_rows:
        family = _model_family(str(row.get("model") or ""))
        if row.get("setup_verdict") != "worse" or family == _model_family(str(row.get("compared_model") or "")):
            continue
        agent = "top-level" if row.get("agent_type") == MAIN else row.get("agent_type")
        out.setdefault((agent, family), row)
    return out


def retried_rows(runs: list[Run]) -> list[dict]:
    """Per agent and model, when at least one of its runs was retried on a
    larger model: its runs that edited files, how many were retried, the
    files, and the model they were most often retried on. Most retried
    first."""
    by_setup: dict[tuple[str, str], list[Run]] = {}
    for run in runs:
        if run.is_agent and (run.edits or run.retried) and run.model:
            by_setup.setdefault((run.group, run.model), []).append(run)
    rows = []
    for (group, model), setup_runs in by_setup.items():
        retried = [run for run in setup_runs if run.retried]
        if not retried:
            continue
        last = max((run.end for run in retried if run.end is not None), default=None)
        rows.append({
            "agent_type": group,
            "model": model,
            "runs": len(setup_runs),
            "retried": len(retried),
            "retried_pct": round(100.0 * len(retried) / len(setup_runs), 1),
            "said_model": sum(1 for run in retried if run.retried_for == "model"),
            "files_edited_again": sum(run.retried_files for run in retried),
            "files_edited": sum(run.files_edited for run in retried),
            "retried_on": Counter(run.retried_on for run in retried).most_common(1)[0][0],
            "last_retried": last.date().isoformat() if last is not None else "",
        })
    rows.sort(key=lambda r: (-r["retried"], -r["retried_pct"], r["agent_type"], r["model"]))
    return rows


def retried_models(rows: Iterable[dict], *, min_sessions: int | None = None) -> dict[tuple[str, str], dict]:
    """``{(agent, model family): row}`` for each ``quality_retried`` row
    where at least :data:`RETRIED_SHARE` of the agent's runs on that model
    were retried on a larger one, so the models check doesn't suggest that
    model to that agent. Each row gains ``reason``, a clause for "it
    wasn't suggested because ...". The main session is never a row.
    ``min_sessions``, when given, is an extra floor on ``runs`` -- without
    it, one retried run out of one is enough to blacklist a model
    forever (``model_gate`` passes ``ModelSwapThresholds.min_sessions``)."""
    out: dict[tuple[str, str], dict] = {}
    for row in rows:
        runs, retried = row.get("runs") or 0, row.get("retried") or 0
        if not retried or retried < RETRIED_SHARE * runs:
            continue
        if min_sessions is not None and runs < min_sessions:
            continue
        family = _model_family(str(row.get("model") or ""))
        on = _model_family(str(row.get("retried_on") or "")) or "a larger model"
        said = row.get("said_model") or 0
        reason = (
            f"{retried} of its {runs} run{'s' if runs != 1 else ''} on {family} that edited files "
            f"{'was' if retried == 1 else 'were'} run again on {on}, "
            + (f"and {'that retry' if said == 1 else f'{said} of those retries'} said {family} wasn't enough"
               if said else "which edited the same files soon after")
        )
        out.setdefault((row.get("agent_type"), family), {**row, "reason": reason})
    return out


def _retried_table(rows: list[dict]) -> Table:
    keys = (
        ("agent_type", "Agent", "str"), ("model", "Model", "str"), ("runs", "Runs that edited files", "int"),
        ("retried", "Retried on a larger model", "int"), ("retried_pct", "Share retried", "pct"),
        ("said_model", "Retries that said the model wasn't enough", "int"),
        ("files_edited_again", "Files edited again", "int"), ("files_edited", "Files those runs edited", "int"),
        ("retried_on", "Retried on", "str"), ("last_retried", "Last time", "str"),
    )
    return Table(
        name="quality_retried",
        title="Agent runs retried on a larger model",
        columns=[Column(key=key, label=label, kind=kind) for key, label, kind in keys],
        rows=[[row[key] for key, _label, _kind in keys] for row in rows],
    )


def retry_reason_rows(runs: list[Run]) -> list[dict]:
    """Per agent and model, when a retry of one of its runs said why
    (see the module docstring): how many, by reason. Most first."""
    by_setup: dict[tuple[str, str], list[Run]] = {}
    for run in runs:
        if run.is_agent and run.retried_for and run.model:
            by_setup.setdefault((run.group, run.model), []).append(run)
    rows = []
    for (group, model), retried in by_setup.items():
        reasons = Counter(run.retried_for for run in retried)
        last = max((run.end for run in retried if run.end is not None), default=None)
        rows.append({
            "agent_type": group,
            "model": model,
            "retries": len(retried),
            **{f"said_{reason}": reasons[reason] for reason in RETRY_REASONS},
            "last_retried": last.date().isoformat() if last is not None else "",
        })
    rows.sort(key=lambda r: (-r["retries"], r["agent_type"], r["model"]))
    return rows


def _retry_reasons_table(rows: list[dict]) -> Table:
    keys = (
        ("agent_type", "Agent", "str"), ("model", "Model", "str"), ("retries", "Retries that said why", "int"),
        ("said_model", "The model", "int"), ("said_brief", "The brief", "int"), ("said_tools", "Tools", "int"),
        ("said_scope", "The task", "int"), ("said_other", "Other", "int"), ("last_retried", "Last time", "str"),
    )
    return Table(
        name="quality_retry_reasons",
        title="Why agents were run again",
        columns=[Column(key=key, label=label, kind=kind) for key, label, kind in keys],
        rows=[[row[key] for key, _label, _kind in keys] for row in rows],
    )


def _breakdown(runs: list[Run], attr: str, words: tuple[str, ...]) -> str:
    counts = Counter(getattr(run, attr) for run in runs)
    return ", ".join(f"{word} {counts[word]}" for word in words if counts[word])


def _markers_table(runs: list[Run]) -> Table:
    """How often each marker was written, of the agent runs that could
    have, and what writing them cost."""
    agents = [run for run in runs if run.is_agent]
    readers = [run for run in agents if run.group not in _SKIPS_CLAUDE_MD]
    retries = [run for run in agents if run.retry_marker]
    results = [run for run in readers if run.result_marker]
    rows = []
    for marker, what, marked, of, attr, words, cost in (
        ("[retry: ...]", "Agent runs whose brief said they were a retry, and why", retries, agents, "retry_marker",
         RETRY_REASONS, "retry_marker_cost"),
        ("[result: ...]", "Agent runs whose last reply said whether they finished", results, readers,
         "result_marker", RESULT_WORDS, "result_marker_cost"),
    ):
        rows.append([
            marker, what, len(marked), len(of), round(100.0 * len(marked) / len(of), 1) if of else None,
            _breakdown(marked, attr, words), MARKER_TOKENS * len(marked),
            round(sum(getattr(run, cost) for run in marked), 4),
        ])
    return Table(
        name="quality_markers",
        title="Markers Claude wrote",
        columns=[
            Column(key="marker", label="Marker", kind="str"),
            Column(key="what", label="What it records", kind="str"),
            Column(key="runs", label="Agent runs with it", kind="int"),
            Column(key="of_runs", label="Agent runs that could have", kind="int"),
            Column(key="share", label="Share", kind="pct"),
            Column(key="breakdown", label="Said", kind="str"),
            Column(key="tokens", label="Output tokens, about", kind="int"),
            Column(key="cost", label="Cost, about", kind="money"),
        ],
        rows=rows if agents else [],
    )


def _difference(rows: list[dict]) -> str:
    # Cost has its own column, in the report's billing units.
    notable = [
        r for r in rows if r["label_key"] not in ("no_clear_change", "too_little_data") and r["unit"] != "money"
    ]
    if not notable:
        tested = [r for r in rows if r["label_key"] != "too_little_data"]
        return "No clear difference." if tested else "Too little data to compare."
    return "; ".join(f"{r['verdict']}: {r['label'].lower()} {r['after_text']} against {r['before_text']}"
                     for r in notable) + "."


def _by_setup_table(rows: list[dict]) -> Table:
    signals = [SIGNAL_BY_KEY[key] for key in _SETUP_SIGNALS]
    return Table(
        name="quality_by_setup",
        title="Quality by model and effort",
        columns=[
            Column(key="agent_type", label="Agent", kind="str"),
            Column(key="model", label="Model", kind="str"),
            Column(key="effort", label="Effort", kind="str"),
            Column(key="runs", label="Runs", kind="int"),
        ]
        + [_column(s) for s in signals]
        + [
            Column(key="compared_with", label="Compared with", kind="str"),
            Column(key="setup_verdict", label="Verdict", kind="str"),
            Column(key="difference", label="Difference", kind="str"),
            Column(key="compared_model", label="Compared model", kind="str"),
            Column(key="compared_effort", label="Compared effort", kind="str"),
        ],
        rows=[
            [r["agent_type"], r["model"], r["effort"], r["runs"]]
            + [r["values"].get(s.key) for s in signals]
            + [r["compared_with"], r["setup_verdict"], r["difference"], r["compared_model"], r["compared_effort"]]
            for r in rows
        ],
    )


def _failing_tools_table(runs: list[Run]) -> Table:
    errors: Counter = Counter()
    for run in runs:
        errors.update({(run.group, name): count for name, count in run.tool_errors_by_tool.items()})
    return Table(
        name="quality_failing_tools",
        title="Which tools failed",
        columns=[
            Column(key="agent_type", label="Agent", kind="str"),
            Column(key="tool", label="Tool", kind="str"),
            Column(key="errors", label="Failed calls", kind="int"),
            Column(key="runs_with_errors", label="Runs with a failure", kind="int"),
        ],
        rows=[
            [group, tool, count,
             sum(1 for run in runs if run.group == group and run.tool_errors_by_tool.get(tool))]
            for (group, tool), count in errors.most_common(25)
        ],
    )


def build_section(runs: list[Run], units: "Units | None" = None) -> Section:
    # UX-2: units may be unset (a caller without a billing config) --
    # money_text still gives a plain currency-suffixed number rather than
    # a bare "$" in that case.
    setups = setup_rows(runs, money=units.money_text if units is not None else None)
    retried = retried_rows(runs)
    reasons = retry_reason_rows(runs)
    notes = [
        "An agent run is cut off when it was stopped, never replied, or its last reply asked for a tool and "
        "nothing followed. A workflow agent that ends by handing back its structured answer counts as "
        "finished. One cut off after its last tool result came back, without being stopped, most likely reached "
        "its turn limit. Claude Code doesn't record why. The quality-by-setup table leaves out runs that never "
        "replied, since they have no model.",
        "A difference is marked only when it is unlikely to be chance (a two-sided test at "
        f"{ALPHA:.0%}, corrected for comparing several signals). It is marked \"possibly\" when it passes the "
        "test on its own but not after the correction. "
        f"Fewer than {MIN_RUNS} runs (or {MIN_DENOMINATOR} of what a rate counts) on either side is too little data. "
        f"A share that moved by less than {100 * MIN_SHARE_CHANGE:.1f} percentage points is not marked.",
        "Setups are compared across the whole window. So a setup used for different kinds of work, or in a "
        "different week, can differ for that reason alone. {{page:changes}} compares before and after "
        "each change you made.",
        "The quality-by-setup table leaves out main sessions a scheduled or looped task started with no message of "
        f"yours ({sum(1 for run in runs if run.scheduled)} in this window). A check that runs and stops is a "
        "different job from the work you steer. A setup is tested against the most-used one only when their "
        f"runs' mean replies are within {COMPARABLE_SIZE:g} times of each other. Otherwise it is marked not "
        "comparable.",
        "Corrections are messages that start or contain a fixed phrase such as \"that's wrong\" or \"still "
        "broken\". Only the yes/no is kept, never the text.",
        "A run counts as retried on a larger model when a later run of the same agent type edited one of its "
        "files. That later run used a larger model family (Haiku, Sonnet, Opus, Fable, smallest first). It "
        "started after the first run ended, in the same session, and edited the file within "
        f"{RETRY_WINDOW.seconds // 3600} hours of its last reply. Another agent type or the "
        "main session editing the file afterwards doesn't count, since a reviewer after a writer is often the plan. "
        "Files are compared by salted hash. Retries aren't part of the setup comparisons: the largest model can "
        "never be retried on a larger one.",
        "An edit is a file changed with Edit, Write, MultiEdit or NotebookEdit. A shell command that writes "
        "content it authored also counts (sed -i, Set-Content, a heredoc redirected to a file). A program's output "
        "captured to a log isn't an edit, and neither is an edit whose tool call failed.",
        "Markers are words Claude writes when CLAUDE.md asks it to (Quick actions, \"Is any agent struggling?\"). "
        f"They are a brief starting [retry: {'|'.join(RETRY_REASONS)}], and a subagent's last reply ending "
        f"[result: {'|'.join(RESULT_WORDS)}]. Only the word is kept. A retry that says the brief, tools or "
        "something else was the problem never counts against the cheaper model. One that says the model does, "
        "even for a different agent. A run that says partial or blocked didn't finish. Explore and Plan start without "
        "CLAUDE.md, so they are left out of the result marker's share.",
    ]
    return Section(
        key="quality",
        title="Quality signals",
        tables=[
            _by_agent_table(runs),
            _by_setup_table(setups),
            _retried_table(retried),
            _retry_reasons_table(reasons),
            _failing_tools_table(runs),
            _counts_table(runs),
            _markers_table(runs),
        ],
        notes=notes,
    )


ASSUMPTIONS: tuple[str, ...] = (
    "Quality signals are counted from the transcripts only. A failed tool call is one whose result is marked as an "
    "error. An agent's outcome is the status its notification or result reported.",
    "A message counts as a correction when it contains a fixed phrase such as \"that's wrong\" or \"still "
    "broken\". Plain disagreement worded differently is missed, so the rate is a floor.",
    "An agent run again on a larger model soon after, editing the same files, is taken as a retry: the cheaper "
    "model's work wasn't enough. Unless the retry's brief says why, a single case is a sign, not proof.",
    "A marker Claude writes ([retry: ...], [result: ...]) is Claude's own account; it is taken at its word.",
)


__all__ = [
    "ALL_AGENTS",
    "ASSUMPTIONS",
    "COMPARABLE_SIZE",
    "LABELS",
    "MAIN",
    "MIN_DENOMINATOR",
    "MIN_SHARE_CHANGE",
    "MIN_RUNS",
    "MIN_RETRIED_RUNS",
    "MARKER_HEADING",
    "MARKER_LINES",
    "MARKER_TOKENS",
    "RESULT_WORDS",
    "RETRIED_SHARE",
    "RETRY_REASONS",
    "RETRY_WINDOW",
    "Run",
    "SIGNALS",
    "SIGNAL_BY_KEY",
    "Signal",
    "build_section",
    "compare_runs",
    "corpus_runs",
    "estimate",
    "retried_models",
    "retried_rows",
    "retry_reason_rows",
    "run_facts",
    "session_runs",
    "SETUP_VERDICTS",
    "worse_models",
    "setup_rows",
    "setup_verdict",
    "signals_for",
    "verdict",
]

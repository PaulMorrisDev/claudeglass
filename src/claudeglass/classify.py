"""Session mode/purpose classification (WP5).

Implements the project plan's "Classification" section and Appendix A1's
``Classification``/``SessionRecord`` contracts. Three layers:

- :func:`extract_features` reduces one session (its top-level transcript
  plus every subagent transcript) to a small :class:`SessionFeatures`
  bundle of numeric/boolean signals — never message text or paths.
- :func:`classify_mode` and :func:`classify_purpose` are pure functions
  of :class:`SessionFeatures`: first-match-wins rule tables per the plan.
- :func:`classify_session` ties the two together, applying a
  ``~/.claude/claudeglass/sessions.toml`` override (via
  ``config.load_session_overrides``) ahead of the rules when present.

:func:`build_session_record` and :func:`group_sessions`/:func:`build_section`
implement Appendix A1's ``SessionRecord`` construction and the report
section the plan's Classification section describes
(``--group-by mode|purpose|...``).

Scope decisions made here, not silently, because the plan's feature list
doesn't say which of ``top``/``subs`` each signal is drawn from:

- ``human_prompts``, the human-gap statistics, and ``start_local_hour``/
  ``end_local_hour`` are computed from ``top`` only. A subagent transcript
  has no human on the other end of it — its user-role lines are harness
  plumbing (tool results, task notifications), not a person typing — so
  counting them as "human prompts" would misrepresent how interactive a
  session actually was.
- ``assistant_turns`` is ``top`` only, for the same reason the plan's
  long-agentic rule needs it: "the top-level conversation kept turning
  itself with hardly any human input" is a statement about the top-level
  turn count, not the combined turn count across every spawned subagent
  (which ``subagent_count``/``has_chain`` already capture separately).
- Every other counter (``queue_ops``, ``compactions``, ``task_notifications``,
  ``peer_messages``, ``plan_mode_events``, and the purpose-signal counters
  ``local_llm_hits``/``agent_tool_calls``/``workflow_tool_calls``/
  ``test_tool_hits``/``review_markers``/``edit_turns``/``read_turns``) are
  summed across ``top`` *and every* transcript in ``subs``. Purpose
  classification asks "what kind of work did this session as a whole do",
  and in real-world multi-agent setups the actual editing/testing often
  happens inside a spawned implementation/verification subagent while the
  top-level turn just orchestrates — restricting these to ``top`` would
  make "refactor" and "test-triage" nearly unreachable.

Two features the brief calls out with an explicit instruction to skip:

- ``doc_edit_share`` ("edits whose... no path available: skip") is not
  implemented — no dataclass field anywhere retains a file path (see
  ``model.py``'s privacy invariant), so there is no way to tell a doc
  edit from any other edit. The "docs" purpose rule uses the brief's own
  fallback condition instead (see ``classify_purpose``), and is reported
  as the literal value ``"docs-or-light-edit"`` per the brief's wording.
- The plan's ``extract_features(top, subs, tz)`` signature has no
  parameter for ``workflows`` (a count) or ``entrypoint`` (a carry-through
  value), yet lists both among the features it produces. Neither was
  derivable from a ``TranscriptResult`` at the time this module was
  written — ``workflows`` comes from ``WorkflowRun`` parsing (WP8) and
  ``entrypoint`` had no home anywhere in the frozen ``model.py`` contract.
  Both remain optional keyword-only parameters here with inert defaults
  (``workflows=0``, ``entrypoint=None``) so a caller that has the data can
  supply it without ``extract_features`` guessing (``classify_session``
  passes both straight through). Batch C added ``TranscriptMeta.entrypoint``
  (first-seen, from ``parse_transcript``) and ``SessionRecord.entrypoint``;
  ``build_session_record`` fills the latter from ``top.meta.entrypoint``,
  so ``group_sessions(records, key="entrypoint")`` now groups by that
  value (falling back to ``"unknown"`` only for a session with none
  recorded) rather than always returning a single bucket.

Usage-limits batch (v3-limits) addition: a usage-cap pause (``limits.
limit_pause_intervals``, itself read straight off ``Turn.gap_cause ==
"limit"``/``Turn.gap_s`` -- see ``limits.py``'s module docstring) should
never read as real idle/overnight time. Two places discount it, both
``top``-only for the same reason every other gap/span signal here is:
``_median_and_max_gap`` now takes an optional ``pause_intervals``
parameter that subtracts any overlapping pause duration from each
consecutive HUMAN_TEXT gap before computing median/max (so
``human_gap_median_s``/``human_gap_max_s`` themselves are already
net-of-pause by the time ``extract_features`` returns), and
``SessionFeatures.limit_pause_s`` (the pauses' own total duration) is
subtracted from ``span_s`` inside ``classify_mode``'s overnight rule
only (as ``effective_span_s`` — ``span_s`` itself is left alone since
other consumers, and the multi-day flag below, want the session's real
wall-clock extent).

Timezone conversion (``start_local_hour``/``end_local_hour``) goes
through ``discovery.to_local``: ``UTC`` and ``Etc/UTC`` resolve without a
tz database, other names through ``zoneinfo.ZoneInfo``. On a machine
with no system tz database and no ``tzdata`` package installed (a bare
Windows install, common on this
project's own dev machine — confirmed by hand: ``ZoneInfo("America/
New_York")`` raises ``ZoneInfoNotFoundError`` here), a named zone that
can't be resolved degrades to the same behaviour as ``tz=None`` (the
machine's own local zone via ``datetime.astimezone()``, which needs no
tz database) rather than raising. This is a real limitation of a
stdlib-only, zero-dependency tool on Windows, not an edge case worth
hiding; ``tests/test_classify.py`` exercises both branches depending on
what the running machine actually has available.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable
from . import discovery, limits
from .model import (
    Classification,
    Column,
    EventKind,
    Section,
    SessionRecord,
    Table,
    TranscriptResult,
    WorkflowRun,
)

# -- Thresholds -------------------------------------------------------------

#: Default thresholds for :func:`classify_mode`, overridable per call via
#: this function's own ``thresholds`` parameter, or corpus-wide via
#: ``Config.thresholds["classify"]["mode"]`` (fix 5 addition — see
#: :func:`mode_and_purpose_thresholds_from_config`). ``Config.thresholds``
#: itself stays the flat, free-form dict ``config.py``'s module docstring
#: describes ("passed to other packages' from_config"); this module reads
#: its own ``"classify"`` sub-key rather than the dict's top level so it
#: never collides with another package reading the same ``Config``.
DEFAULT_MODE_THRESHOLDS: dict = {
    "overnight_span_s": 4 * 3600,
    "overnight_gap_s": 60 * 60,
    #: Local-time window a session's activity must fall inside for
    #: "overnight" to fire (fix 5: span + gap alone used to over-fire on
    #: sessions that merely spanned a lunch break or a long meeting with
    #: no actual night-time activity). Wraps midnight: hour >= start OR
    #: hour < end.
    "overnight_night_start_hour": 22,
    "overnight_night_end_hour": 7,
    #: Share of top-level assistant turns that must fall in the local
    #: night window above for "overnight" to fire, UNLESS the long human
    #: gap itself overlaps that window (``SessionFeatures.long_gap_in_night``)
    #: — either signal alone is enough, per the plan's Risk 9.
    "overnight_night_turn_share": 0.3,
    #: When "overnight" doesn't fire, a span longer than this still gets
    #: flagged ``mode_evidence["multi_day"] = True`` on whichever mode the
    #: rest of the table picks, so a report can tell "genuinely overnight"
    #: apart from "just a very long-running session" (fix 5).
    "multi_day_span_s": 24 * 3600,
    # Raised from the plan's implied starting point of 5 to 10 against a
    # real 30-day corpus (120 top-level sessions in one
    # project, scratch script corpus_classify_tune.py): at 5,
    # sessions with a real subagent chain or a
    # long run of self-chained turns but a handful more human nudges
    # (6-10) than the cap allowed fell through into "mixed" - the
    # corpus's mixed share sat at 23.3%. Raising just this one
    # threshold (leaving long_agentic_min_turns, interactive_gap_s, and
    # the overnight thresholds at their prior values) took the corpus's
    # mixed share to 15.8%, without moving overnight's own share.
    "long_agentic_max_human_prompts": 10,
    # Tuned from the plan's implied starting point of 50 down to 30
    # against a real 30-day corpus (131 top-level sessions, see this
    # WP's report): at 50, sessions that were plainly autonomous
    # top-level runs (dozens of self-chained turns, at most a couple of
    # human nudges) but topped out under 50 turns fell through every
    # rule into "mixed" - 39% of that corpus's "mixed" sessions had
    # >=30 assistant_turns and <=8 human_prompts. Lowering just this one
    # threshold (leaving long_agentic_max_human_prompts,
    # interactive_gap_s, and the overnight thresholds at the plan's
    # values) took the corpus's mixed share from 23.7% to 17.6%, under
    # the <20% bar, without touching interactive/overnight's own share
    # of the corpus.
    "long_agentic_min_turns": 30,
    "interactive_gap_s": 5 * 60,
    "interactive_max_subagents": 2,
}

#: Default thresholds for :func:`classify_purpose`, same override shape.
DEFAULT_PURPOSE_THRESHOLDS: dict = {
    "local_llm_min_hits": 3,
    "agent_fanout_min_calls": 3,
    "test_triage_min_hits": 3,
    "planning_max_edit_turns": 2,
    "docs_min_assistant_turns": 5,
    "docs_min_edit_turns": 3,
    "refactor_min_edit_turns": 10,
    "refactor_min_test_hits": 1,
}


def mode_and_purpose_thresholds_from_config(config_thresholds: dict) -> tuple[dict, dict]:
    """Pull this module's own threshold overrides out of ``Config.thresholds``
    (fix 5): ``config_thresholds["classify"]["mode"]`` and
    ``config_thresholds["classify"]["purpose"]``.

    ``Config.thresholds`` is a flat, free-form dict shared by every
    package's own override seam (see ``config.py``'s module docstring —
    RE-CACHE has its own dedicated ``Config.recache`` field instead, so
    this isn't every package's convention, just this module's). Reading
    it through a ``"classify"`` sub-key rather than the dict's top level
    means another package's keys living alongside it in the same
    ``Config.thresholds`` table can never collide with these.

    Returns ``(mode_thresholds, purpose_thresholds)``, each ``{}`` when
    absent or malformed (never raises on a foreign shape — same posture
    ``config.py``/``pricing.py``/``snapshots.py`` already take). Pass the
    results straight through to :func:`classify_session`'s
    ``mode_thresholds``/``purpose_thresholds`` keyword arguments.
    """
    classify_cfg = config_thresholds.get("classify") if config_thresholds else None
    if not isinstance(classify_cfg, dict):
        return {}, {}
    mode_thresholds = classify_cfg.get("mode")
    purpose_thresholds = classify_cfg.get("purpose")
    return (
        dict(mode_thresholds) if isinstance(mode_thresholds, dict) else {},
        dict(purpose_thresholds) if isinstance(purpose_thresholds, dict) else {},
    )


#: Bash ``cmd_prefix`` substrings identifying a call into a local LLM
#: server (plan "Classification" section / "Other clients").
_LOCAL_LLM_MARKERS = (
    "localhost:1234",
    "127.0.0.1:1234",
    "lmstudio",
    "ollama",
    "/v1/chat/completions",
)

#: ``cmd_prefix`` prefixes identifying a test-runner invocation.
_TEST_TOOL_PREFIXES = ("pytest", "dotnet test", "npm test", "npx vitest", "go test", "cargo test")


# -- SessionFeatures ----------------------------------------------------------


@dataclass(slots=True)
class SessionFeatures:
    """Numeric/boolean signals reduced from one session, never message
    text or paths. Feeds :func:`classify_mode`/:func:`classify_purpose`.
    """

    human_prompts: int = 0
    human_gap_median_s: float | None = None
    human_gap_max_s: float | None = None
    assistant_turns: int = 0
    subagent_count: int = 0
    max_spawn_depth: int = 0
    has_chain: bool = False
    span_s: float = 0.0
    queue_ops: int = 0
    compactions: int = 0
    task_notifications: int = 0
    peer_messages: int = 0
    #: Count of ``WorkflowRun``s for this session, computed by
    #: ``workflows.py``'s own parsing (see the module docstring for why
    #: this module doesn't compute it itself). Caller-supplied, default 0.
    workflows: int = 0
    #: Carry-through value (see the module docstring); caller-supplied.
    entrypoint: str | None = None
    local_llm_hits: int = 0
    agent_tool_calls: int = 0
    workflow_tool_calls: int = 0
    test_tool_hits: int = 0
    review_markers: int = 0
    plan_mode_events: int = 0
    edit_turns: int = 0
    read_turns: int = 0
    start_local_hour: int | None = None
    end_local_hour: int | None = None
    #: Share (0.0-1.0) of top-level, non-synthetic assistant turns whose
    #: local timestamp falls inside the night window (fix 5; see
    #: ``DEFAULT_MODE_THRESHOLDS``). 0.0 when there are no assistant
    #: turns to measure, same "nothing to divide by" convention as
    #: ``compaction.py``'s share properties use elsewhere in this
    #: project — never mistaken for "definitely not overnight" on its
    #: own, since ``long_gap_in_night`` can still carry the signal.
    night_turn_share: float = 0.0
    #: Whether the session's single longest human-to-human gap overlaps
    #: the local night window on any day it spans (fix 5) — a session
    #: with hardly any assistant turns overall (so ``night_turn_share``
    #: is a poor sample) can still clearly be "worked overnight" if the
    #: one big idle gap itself sat in the night hours.
    long_gap_in_night: bool = False
    #: Usage-limits addition (v3-limits): total seconds of ``top``'s own
    #: usage-cap pauses (``limits.limit_pause_intervals``) that overlap
    #: the gaps between consecutive HUMAN_TEXT timestamps -- see
    #: ``_median_and_max_gap``'s ``pause_intervals`` parameter and
    #: ``classify_mode``'s overnight rule, both of which subtract this so
    #: a usage-cap pause is never mistaken for real idle/overnight time.
    limit_pause_s: float = 0.0


# -- Timestamp / timezone helpers -------------------------------------------


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _ts_range(transcripts: Iterable[TranscriptResult | None]) -> tuple[str | None, str | None]:
    """Earliest/latest ``Turn.ts`` across every turn in ``transcripts``, as
    the original ISO strings (not parsed ``datetime``s) so callers that
    just want to store them never need to reformat.
    """
    first: datetime | None = None
    first_raw: str | None = None
    last: datetime | None = None
    last_raw: str | None = None
    for result in transcripts:
        if result is None:
            continue
        for turn in result.turns:
            dt = _parse_ts(turn.ts)
            if dt is None:
                continue
            if first is None or dt < first:
                first, first_raw = dt, turn.ts
            if last is None or dt > last:
                last, last_raw = dt, turn.ts
    return first_raw, last_raw


def _span_seconds(first_ts: str | None, last_ts: str | None) -> float:
    first = _parse_ts(first_ts)
    last = _parse_ts(last_ts)
    if first is None or last is None:
        return 0.0
    return max(0.0, (last - first).total_seconds())


def _human_text_timestamps(top: TranscriptResult) -> list[datetime]:
    stamps = []
    for event in top.events:
        if event.kind != EventKind.HUMAN_TEXT:
            continue
        dt = _parse_ts(event.ts)
        if dt is not None:
            stamps.append(dt)
    stamps.sort()
    return stamps


def _pause_overlap_seconds(
    start: datetime, end: datetime, intervals: list[tuple[datetime, datetime]]
) -> float:
    """Total seconds of ``[start, end]`` covered by any interval in
    ``intervals`` (usage-limits addition, v3-limits). Intervals are
    assumed non-overlapping with each other (each comes from a distinct
    turn's own gap -- see ``limits.limit_pause_intervals``), so their
    individual overlaps with ``[start, end]`` are simply summed rather
    than merged first.
    """
    total = 0.0
    for p_start, p_end in intervals:
        overlap = min(end, p_end) - max(start, p_start)
        if overlap.total_seconds() > 0:
            total += overlap.total_seconds()
    return total


def _median_and_max_gap(
    stamps: list[datetime], pause_intervals: list[tuple[datetime, datetime]] | None = None
) -> tuple[float | None, float | None]:
    """Median/max gap between consecutive ``stamps``.

    ``pause_intervals`` (usage-limits addition, v3-limits): when given,
    each consecutive gap has any overlapping usage-cap pause duration
    (:func:`_pause_overlap_seconds`) subtracted before the median/max is
    computed -- a gap that was mostly a 5-hour usage-cap pause should not
    read as a real multi-hour idle gap. Clamped at 0 so a gap can never
    go negative.
    """
    if len(stamps) < 2:
        return None, None
    gaps = []
    for earlier, later in zip(stamps, stamps[1:]):
        gap = (later - earlier).total_seconds()
        if pause_intervals:
            gap = max(0.0, gap - _pause_overlap_seconds(earlier, later, pause_intervals))
        gaps.append(gap)
    return statistics.median(gaps), max(gaps)


def _max_gap_pair(stamps: list[datetime]) -> tuple[datetime, datetime] | None:
    """The ``(earlier, later)`` pair of consecutive ``stamps`` achieving
    the single largest gap — the same gap :func:`_median_and_max_gap`
    reports the duration of, but with both endpoints, so
    :func:`_gap_overlaps_night` can check what time of day that gap
    actually spanned rather than just how long it was.
    """
    if len(stamps) < 2:
        return None
    pairs = list(zip(stamps, stamps[1:]))
    return max(pairs, key=lambda pair: (pair[1] - pair[0]).total_seconds())


def _to_local(dt: datetime, tz: str | None) -> datetime:
    """Convert a UTC-aware ``dt`` to ``tz`` (an IANA name), or the
    machine's own local zone when ``tz`` is falsy or can't be resolved
    (see the module docstring's timezone-conversion note).
    """
    return discovery.to_local(dt, tz)


def _local_hour(ts: str | None, tz: str | None) -> int | None:
    """Convert ``ts`` (a ``Turn``/``Event`` UTC ISO timestamp) to the hour
    of day in ``tz`` (an IANA name), or the machine's own local zone when
    ``tz`` is falsy or can't be resolved (see the module docstring).
    """
    dt = _parse_ts(ts)
    if dt is None:
        return None
    return _to_local(dt, tz).hour


def _in_night_window(hour: int, night_start_hour: int, night_end_hour: int) -> bool:
    """Whether ``hour`` (0-23) falls in the ``[night_start_hour,
    night_end_hour)`` window, wrapping past midnight (the default 22-7
    window means hour >= 22 or hour < 7).
    """
    if night_start_hour <= night_end_hour:
        return night_start_hour <= hour < night_end_hour
    return hour >= night_start_hour or hour < night_end_hour


def _gap_overlaps_night(
    start: datetime, end: datetime, tz: str | None, night_start_hour: int, night_end_hour: int
) -> bool:
    """Whether the ``[start, end]`` gap (UTC-aware) overlaps the local
    night window on ANY calendar day it spans — checked as interval
    overlap against that day's night window, not just by looking at the
    two endpoints' own hours (a gap from 18:00 to 09:00 the next day
    plainly spans the night even though neither endpoint's hour is
    itself in the window).
    """
    start_local = _to_local(start, tz)
    end_local = _to_local(end, tz)
    if end_local <= start_local:
        return False

    from datetime import time, timedelta

    # Start one day early: a wrapping window (e.g. 22:00-07:00) that began
    # the evening before ``start_local``'s own date can still be the
    # window ``start_local`` itself falls inside (a gap starting at
    # 02:00 is inside the night that began at 22:00 the previous day).
    day = start_local.date() - timedelta(days=1)
    last_day = end_local.date()
    while day <= last_day:
        night_begin = datetime.combine(day, time(night_start_hour, 0), tzinfo=start_local.tzinfo)
        if night_start_hour <= night_end_hour:
            night_end = datetime.combine(day, time(night_end_hour, 0), tzinfo=start_local.tzinfo)
        else:
            night_end = datetime.combine(
                day + timedelta(days=1), time(night_end_hour, 0), tzinfo=start_local.tzinfo
            )
        if start_local < night_end and end_local > night_begin:
            return True
        day += timedelta(days=1)
    return False


# -- extract_features ---------------------------------------------------------


def _matches_local_llm(cmd_prefix: str) -> bool:
    lowered = cmd_prefix.lower()
    return any(marker in lowered for marker in _LOCAL_LLM_MARKERS)


def _matches_test_tool(cmd_prefix: str) -> bool:
    return cmd_prefix.lstrip().startswith(_TEST_TOOL_PREFIXES)


def extract_features(
    top: TranscriptResult,
    subs: list[TranscriptResult] | None,
    tz: str | None = None,
    *,
    workflows: int = 0,
    entrypoint: str | None = None,
    thresholds: dict | None = None,
) -> SessionFeatures:
    """Reduce one session (``top`` plus its ``subs``) to a
    :class:`SessionFeatures` bundle. See the module docstring for which
    signals are ``top``-only vs. summed across ``top`` and every
    transcript in ``subs``, and for the ``workflows``/``entrypoint``
    keyword-only parameters (data ``TranscriptResult`` alone can't supply
    yet — see the module docstring).

    ``thresholds`` (fix 5) only supplies the night-window bounds
    (``overnight_night_start_hour``/``overnight_night_end_hour``) used to
    compute ``night_turn_share``/``long_gap_in_night`` — merged over
    ``DEFAULT_MODE_THRESHOLDS`` the same way :func:`classify_mode` merges
    its own ``thresholds`` argument, so a caller can pass the very same
    dict to both without the window bounds ever disagreeing between the
    feature computed here and the rule that reads it.
    """
    subs = subs or []
    all_transcripts = (top, *subs)
    t = {**DEFAULT_MODE_THRESHOLDS, **(thresholds or {})}
    night_start_hour = t["overnight_night_start_hour"]
    night_end_hour = t["overnight_night_end_hour"]

    human_stamps = _human_text_timestamps(top)
    # Usage-limits addition (v3-limits): discount usage-cap pauses from
    # every human-to-human gap before computing median/max (see
    # limits.limit_pause_intervals's own module-docstring rationale for
    # why this is top-only, matching every other top-only signal here).
    pause_intervals = limits.limit_pause_intervals(top)
    human_gap_median_s, human_gap_max_s = _median_and_max_gap(human_stamps, pause_intervals)
    limit_pause_s = sum((end - start).total_seconds() for start, end in pause_intervals)

    first_ts, last_ts = _ts_range(all_transcripts)
    span_s = _span_seconds(first_ts, last_ts)

    assistant_turns = sum(1 for turn in top.turns if not turn.is_synthetic)

    night_turns = sum(
        1
        for turn in top.turns
        if not turn.is_synthetic
        and (hour := _local_hour(turn.ts, tz)) is not None
        and _in_night_window(hour, night_start_hour, night_end_hour)
    )
    night_turn_share = (night_turns / assistant_turns) if assistant_turns else 0.0

    long_gap_in_night = False
    gap_pair = _max_gap_pair(human_stamps)
    if gap_pair is not None:
        long_gap_in_night = _gap_overlaps_night(
            gap_pair[0], gap_pair[1], tz, night_start_hour, night_end_hour
        )

    max_spawn_depth = max((s.meta.spawn_depth for s in subs), default=0)
    has_chain = max_spawn_depth >= 2 or any(s.meta.parent_agent_id for s in subs)

    queue_ops = 0
    compactions = 0
    task_notifications = 0
    peer_messages = 0
    plan_mode_events = 0
    for result in all_transcripts:
        for event in result.events:
            if event.kind == EventKind.QUEUE_OPERATION:
                queue_ops += 1
            elif event.kind == EventKind.COMPACT_BOUNDARY:
                compactions += 1
            elif event.kind == EventKind.TASK_NOTIFICATION:
                task_notifications += 1
            elif event.kind == EventKind.PEER_MESSAGE:
                peer_messages += 1
            elif event.kind == EventKind.CACHE_SIGNAL and event.subkind == "plan_mode":
                plan_mode_events += 1

    local_llm_hits = 0
    agent_tool_calls = 0
    workflow_tool_calls = 0
    test_tool_hits = 0
    review_markers = 0
    edit_turns = 0
    read_turns = 0
    for result in all_transcripts:
        if result.meta.agent_type and "review" in result.meta.agent_type.lower():
            review_markers += 1
        for turn in result.turns:
            if "Agent" in turn.tool_names:
                agent_tool_calls += 1
            if "Workflow" in turn.tool_names:
                workflow_tool_calls += 1
            if turn.edit_kind == "real":
                edit_turns += 1
            if any(name in turn.tool_names for name in ("Read", "Grep", "Glob")):
                read_turns += 1
            if turn.attribution_skill and "review" in turn.attribution_skill.lower():
                review_markers += 1
            if turn.cmd_prefix:
                if "Bash" in turn.tool_names and _matches_local_llm(turn.cmd_prefix):
                    local_llm_hits += 1
                if _matches_test_tool(turn.cmd_prefix):
                    test_tool_hits += 1

    start_local_hour = _local_hour(first_ts, tz)
    end_local_hour = _local_hour(last_ts, tz)

    return SessionFeatures(
        human_prompts=len(human_stamps),
        human_gap_median_s=human_gap_median_s,
        human_gap_max_s=human_gap_max_s,
        assistant_turns=assistant_turns,
        subagent_count=len(subs),
        max_spawn_depth=max_spawn_depth,
        has_chain=has_chain,
        span_s=span_s,
        queue_ops=queue_ops,
        compactions=compactions,
        task_notifications=task_notifications,
        peer_messages=peer_messages,
        workflows=workflows,
        entrypoint=entrypoint,
        local_llm_hits=local_llm_hits,
        agent_tool_calls=agent_tool_calls,
        workflow_tool_calls=workflow_tool_calls,
        test_tool_hits=test_tool_hits,
        review_markers=review_markers,
        plan_mode_events=plan_mode_events,
        edit_turns=edit_turns,
        read_turns=read_turns,
        start_local_hour=start_local_hour,
        end_local_hour=end_local_hour,
        night_turn_share=night_turn_share,
        long_gap_in_night=long_gap_in_night,
        limit_pause_s=limit_pause_s,
    )


# -- classify_mode / classify_purpose ----------------------------------------


def classify_mode(f: SessionFeatures, thresholds: dict | None = None) -> tuple[str, dict]:
    """First-match-wins mode classification (plan "Classification"
    section): overnight -> long-agentic -> interactive -> mixed.

    Fix 5 (plan Risk 9: "overnight detection needs local time"): span and
    the max human gap alone used to fire "overnight" on any session that
    merely spanned a long lunch break or an afternoon meeting, with no
    actual night-time activity. Overnight now additionally requires
    ``night_turn_share`` to clear ``overnight_night_turn_share`` OR
    ``long_gap_in_night`` to be set — real local-night evidence, not just
    a long idle gap at an arbitrary hour. A session that fails only that
    extra check (still a long span and a long gap, just not at night)
    falls through to the rest of the table as normal; if its span also
    exceeds ``multi_day_span_s``, whichever mode it lands on gets
    ``mode_evidence["multi_day"] = True`` merged in, so a report can tell
    "genuinely overnight" apart from "just a very long-running session".

    Usage-limits addition (v3-limits): the span side of the check
    compares against ``effective_span_s = max(0.0, f.span_s -
    f.limit_pause_s)`` rather than ``f.span_s`` directly -- a session
    that merely spanned a long usage-cap pause (paused at 11pm, resumed
    at 6am with zero actual overnight work) should not read as
    overnight purely because the pause made its raw span long.
    ``f.human_gap_max_s`` is unaffected here (``extract_features``
    already discounted pause overlap from every gap via
    ``_median_and_max_gap``'s ``pause_intervals`` parameter). Both
    ``span_s`` and ``effective_span_s`` are recorded in the evidence
    dict so a report can show the discount was applied.
    """
    t = {**DEFAULT_MODE_THRESHOLDS, **(thresholds or {})}
    effective_span_s = max(0.0, f.span_s - f.limit_pause_s)

    if (
        effective_span_s > t["overnight_span_s"]
        and f.human_gap_max_s is not None
        and f.human_gap_max_s > t["overnight_gap_s"]
        and (f.night_turn_share >= t["overnight_night_turn_share"] or f.long_gap_in_night)
    ):
        return "overnight", {
            "span_s": f.span_s,
            "effective_span_s": effective_span_s,
            "human_gap_max_s": f.human_gap_max_s,
            "night_turn_share": f.night_turn_share,
            "long_gap_in_night": f.long_gap_in_night,
        }

    if f.has_chain:
        mode, evidence = "long-agentic", {"has_chain": f.has_chain}
    elif f.subagent_count >= 1 and f.human_prompts <= t["long_agentic_max_human_prompts"]:
        mode, evidence = "long-agentic", {
            "subagent_count": f.subagent_count,
            "human_prompts": f.human_prompts,
        }
    elif (
        f.assistant_turns >= t["long_agentic_min_turns"]
        and f.human_prompts <= t["long_agentic_max_human_prompts"]
    ):
        mode, evidence = "long-agentic", {
            "assistant_turns": f.assistant_turns,
            "human_prompts": f.human_prompts,
        }
    elif (
        f.human_gap_median_s is not None
        and f.human_gap_median_s < t["interactive_gap_s"]
        and f.subagent_count <= t["interactive_max_subagents"]
    ):
        mode, evidence = "interactive", {
            "human_gap_median_s": f.human_gap_median_s,
            "subagent_count": f.subagent_count,
        }
    else:
        mode, evidence = "mixed", {
            "span_s": f.span_s,
            "human_gap_max_s": f.human_gap_max_s,
            "human_gap_median_s": f.human_gap_median_s,
            "subagent_count": f.subagent_count,
            "assistant_turns": f.assistant_turns,
            "human_prompts": f.human_prompts,
            "has_chain": f.has_chain,
        }

    if f.span_s > t["multi_day_span_s"]:
        evidence = {**evidence, "multi_day": True}
    return mode, evidence


def classify_purpose(f: SessionFeatures, thresholds: dict | None = None) -> tuple[str, dict]:
    """First-match-wins purpose classification (plan "Classification"
    section, with the brief's ``docs-or-light-edit`` substitution for the
    unreachable path-based "docs" rule — see the module docstring).

    Fix 8: ``agent-fanout`` used to be tested third, ahead of every
    intent-signature rule (``review``/``test-triage``/``planning``/
    ``docs-or-light-edit``/``refactor``), so a session that both delegated
    to subagents *and* carried a clear intent signature (e.g. a review
    pass that happened to fan a couple of checks out to agents) always
    lost that signature to the generic "agent-fanout" bucket. Moved to
    just above the ``general-dev`` catch-all: an intent signature now
    wins whenever one is present, and ``agent-fanout`` only catches
    fan-out-heavy sessions that don't otherwise say what they were for.

    ``review`` and ``test-triage`` are also relaxed here: ``review`` used
    to require zero edits at all (``edit_turns == 0``), missing the
    common case of a review pass that also lands one or two small fixes;
    it now tolerates up to 2. ``test-triage`` used to require
    ``test_tool_hits >= edit_turns`` unconditionally, missing a session
    that ran many test commands alongside a larger edit count; that
    comparison is now dropped once ``test_tool_hits`` alone clears 5 —
    a session hitting test tooling that often is a test-triage session
    regardless of how much editing happened alongside it.
    """
    t = {**DEFAULT_PURPOSE_THRESHOLDS, **(thresholds or {})}

    if f.local_llm_hits >= t["local_llm_min_hits"]:
        return "local-llm-pipeline", {"local_llm_hits": f.local_llm_hits}

    if f.workflows >= 1 or f.workflow_tool_calls >= 1:
        return "workflow-run", {
            "workflows": f.workflows,
            "workflow_tool_calls": f.workflow_tool_calls,
        }

    if f.review_markers >= 1 and f.edit_turns <= 2:
        return "review", {"review_markers": f.review_markers, "edit_turns": f.edit_turns}

    if f.test_tool_hits >= t["test_triage_min_hits"] and (
        f.test_tool_hits >= f.edit_turns or f.test_tool_hits >= 5
    ):
        return "test-triage", {"test_tool_hits": f.test_tool_hits, "edit_turns": f.edit_turns}

    if f.plan_mode_events >= 1 and f.edit_turns <= t["planning_max_edit_turns"]:
        return "planning", {
            "plan_mode_events": f.plan_mode_events,
            "edit_turns": f.edit_turns,
        }

    if (
        f.assistant_turns >= t["docs_min_assistant_turns"]
        and f.edit_turns >= t["docs_min_edit_turns"]
        and f.read_turns <= f.edit_turns
        and f.test_tool_hits == 0
    ):
        return "docs-or-light-edit", {
            "assistant_turns": f.assistant_turns,
            "edit_turns": f.edit_turns,
            "read_turns": f.read_turns,
        }

    if f.edit_turns >= t["refactor_min_edit_turns"] and f.test_tool_hits >= t["refactor_min_test_hits"]:
        return "refactor", {"edit_turns": f.edit_turns, "test_tool_hits": f.test_tool_hits}

    if f.agent_tool_calls >= t["agent_fanout_min_calls"]:
        return "agent-fanout", {"agent_tool_calls": f.agent_tool_calls}

    return "general-dev", {
        "assistant_turns": f.assistant_turns,
        "edit_turns": f.edit_turns,
        "test_tool_hits": f.test_tool_hits,
    }


#: A task kind metrics capture reported that maps one to one onto a
#: purpose. The others (feature, bugfix, debug, research, ops, chat) span
#: several purposes, so the rules decide those sessions.
REPORTED_PURPOSES = {
    "review": "review",
    "test": "test-triage",
    "plan": "planning",
    "docs": "docs-or-light-edit",
    "refactor": "refactor",
}

#: Purposes the transcript's structure settles whatever Claude reported.
_STRUCTURAL_PURPOSES = frozenset({"local-llm-pipeline", "workflow-run"})


def reported_task(top: TranscriptResult | None) -> tuple[str | None, int]:
    """The kind of task Claude reported (metrics capture's ``task=``) for
    at least half of a session's tagged messages, twice or more, and how
    many messages it tagged."""
    return reported_word(top, "task")


def reported_word(top: TranscriptResult | None, field: str) -> tuple[str | None, int]:
    """The word Claude reported for one capture tag field (``task``,
    ``level``, ``size``...) on at least half of a session's messages that
    carry it, twice or more, and how many messages carry it."""
    if top is None:
        return None, 0
    words = [getattr(t.cap, field) for t in top.turns if t.cap is not None and t.cap.has_tl and getattr(t.cap, field)]
    if len(words) < 2:
        return None, len(words)
    counts: dict[str, int] = {}
    for word in words:
        counts[word] = counts.get(word, 0) + 1
    word = max(counts, key=lambda k: (counts[k], k))
    return (word if 2 * counts[word] >= len(words) else None), len(words)


def classify_session(
    top: TranscriptResult,
    subs: list[TranscriptResult] | None,
    overrides: dict,
    tz: str | None,
    *,
    workflows: int = 0,
    entrypoint: str | None = None,
    mode_thresholds: dict | None = None,
    purpose_thresholds: dict | None = None,
) -> Classification:
    """Classify one session, applying a ``sessions.toml`` override (see
    ``config.load_session_overrides``) ahead of the rules. ``overrides``
    is keyed by session id, each value optionally holding ``"mode"``
    and/or ``"purpose"`` (independently — a session can override one and
    let the other run through the rules).

    Without an override, the kind of task metrics capture reported
    (:func:`reported_task`) decides the purpose where it maps one to one
    (:data:`REPORTED_PURPOSES`), with ``purpose_source`` ``"reported"``;
    a local-LLM pipeline or a workflow run still wins, since the
    transcript shows those outright.
    """
    features = extract_features(
        top, subs, tz, workflows=workflows, entrypoint=entrypoint, thresholds=mode_thresholds
    )
    override = overrides.get(top.meta.session_id, {}) if overrides else {}

    if "mode" in override:
        mode = override["mode"]
        mode_source = "override"
        mode_evidence = {"value": mode}
    else:
        mode, mode_evidence = classify_mode(features, mode_thresholds)
        mode_source = "rule"

    if "purpose" in override:
        purpose = override["purpose"]
        purpose_source = "override"
        purpose_evidence = {"value": purpose}
    else:
        purpose, purpose_evidence = classify_purpose(features, purpose_thresholds)
        purpose_source = "rule"
        task, tagged = reported_task(top)
        if task in REPORTED_PURPOSES and purpose not in _STRUCTURAL_PURPOSES:
            purpose = REPORTED_PURPOSES[task]
            purpose_source = "reported"
            purpose_evidence = {"reported_task": task, "tagged_messages": tagged}

    return Classification(
        mode=mode,
        mode_source=mode_source,
        mode_evidence=mode_evidence,
        purpose=purpose,
        purpose_source=purpose_source,
        purpose_evidence=purpose_evidence,
    )


# -- SessionRecord construction / grouping / report section -----------------


def build_session_record(
    top: TranscriptResult,
    subs: list[TranscriptResult],
    workflows: list[WorkflowRun],
    classification: Classification,
    slug: str,
) -> SessionRecord:
    """Build one ``SessionRecord``. ``archetype``/``snapshot_id``/
    ``profile_id`` are left ``None`` here: ``report.py`` fills in
    ``archetype`` afterwards, from ``workstyle.detect_archetype()``
    against that session's own extracted features; ``snapshot_id`` stays
    unset even after a report renders — the session-to-config join is
    resolved on demand at render time instead, via
    ``snapshots.snapshot_for(record.first_ts, snapshots)``, rather than
    stored back onto the record; ``profile_id`` needs the config
    directory's snapshots and apply history, so each caller that has
    them fills it in with ``snapshots.profile_for`` (``report.py``,
    ``compare.py`` and the service's watcher).
    ``entrypoint`` (batch C addition) is carried straight through from
    ``top.meta.entrypoint`` — ``parse_transcript`` already derived it as
    the first non-empty ``entrypoint`` field seen anywhere in the top
    transcript's raw lines (see ``model.py``'s ``TranscriptMeta``
    docstring); this is no longer a proposed field with nowhere to live.
    """
    first_ts, last_ts = _ts_range([top, *subs])
    span_s = _span_seconds(first_ts, last_ts)
    return SessionRecord(
        session_id=top.meta.session_id,
        slug=slug,
        first_ts=first_ts,
        last_ts=last_ts,
        span_s=span_s,
        top=top,
        subs=list(subs),
        workflows=list(workflows),
        classification=classification,
        entrypoint=top.meta.entrypoint,
    )


_GROUP_KEYS = frozenset({"mode", "purpose", "project", "model", "agent", "entrypoint"})


def _dominant_model(top: TranscriptResult | None) -> str:
    if top is None:
        return "unknown"
    counts: dict[str, int] = {}
    for turn in top.turns:
        if turn.is_synthetic or not turn.model:
            continue
        counts[turn.model] = counts.get(turn.model, 0) + 1
    if not counts:
        return "unknown"
    return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]


def _dominant_agent(record: SessionRecord) -> str:
    if record.top is not None:
        settings = record.top.diagnostics.agent_settings
        if settings:
            return max(settings.items(), key=lambda kv: (kv[1], kv[0]))[0]
    counts: dict[str, int] = {}
    for sub in record.subs:
        if sub.meta.agent_type:
            counts[sub.meta.agent_type] = counts.get(sub.meta.agent_type, 0) + 1
    if counts:
        return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]
    return "unknown"


def _group_key_value(record: SessionRecord, key: str) -> str:
    if key == "mode":
        return record.classification.mode if record.classification else "unknown"
    if key == "purpose":
        return record.classification.purpose if record.classification else "unknown"
    if key == "project":
        return record.top.meta.project_slug if record.top else "unknown"
    if key == "model":
        return _dominant_model(record.top)
    if key == "agent":
        return _dominant_agent(record)
    if key == "entrypoint":
        # Batch C addition: SessionRecord.entrypoint, carried through from
        # top.meta.entrypoint by build_session_record.
        return record.entrypoint or "unknown"
    raise ValueError(f"unknown group_sessions key: {key!r} (expected one of {sorted(_GROUP_KEYS)})")


def group_sessions(records: list[SessionRecord], key: str) -> dict[str, list[SessionRecord]]:
    """Group ``records`` by ``key`` (``"mode"``, ``"purpose"``,
    ``"project"``, ``"model"``, ``"agent"``, or ``"entrypoint"``). See
    ``_group_key_value`` for how each key's value is derived
    (``"entrypoint"`` groups by ``SessionRecord.entrypoint``, falling
    back to ``"unknown"`` for a session with none recorded).
    """
    if key not in _GROUP_KEYS:
        raise ValueError(f"unknown group_sessions key: {key!r} (expected one of {sorted(_GROUP_KEYS)})")
    groups: dict[str, list[SessionRecord]] = {}
    for record in records:
        group_key = _group_key_value(record, key)
        groups.setdefault(group_key, []).append(record)
    return groups


def _turns_count(record: SessionRecord) -> int:
    total = len(record.top.turns) if record.top else 0
    total += sum(len(s.turns) for s in record.subs)
    return total


def _group_summary_table(
    records: list[SessionRecord],
    features_by_id: dict[str, SessionFeatures],
    *,
    key: str,
    name: str,
    title: str,
) -> Table:
    groups = group_sessions(records, key)
    columns = [
        Column(key="value", label=key.capitalize(), kind="str"),
        Column(key="sessions", label="Sessions", kind="int"),
        Column(key="turns", label="Turns", kind="int"),
        Column(key="subagents", label="Subagents", kind="int"),
        Column(key="median_span_s", label="Median span", kind="secs"),
        Column(key="human_prompts_median", label="Human prompts (median)", kind="float"),
    ]
    rows: list[list] = []
    for value in sorted(groups, key=lambda v: (-len(groups[v]), v)):
        group = groups[value]
        turns_total = sum(_turns_count(r) for r in group)
        subagents_total = sum(len(r.subs) for r in group)
        spans = [r.span_s for r in group]
        prompts = [
            features_by_id[r.session_id].human_prompts
            for r in group
            if r.session_id in features_by_id
        ]
        rows.append(
            [
                value,
                len(group),
                turns_total,
                subagents_total,
                statistics.median(spans) if spans else None,
                statistics.median(prompts) if prompts else None,
            ]
        )
    return Table(name=name, title=title, columns=columns, rows=rows)


_PER_SESSION_ROW_CAP = 50


def _per_session_table(records: list[SessionRecord]) -> Table:
    columns = [
        Column(key="session_id", label="Session", kind="str"),
        Column(key="slug", label="Project", kind="str"),
        Column(key="mode", label="Mode", kind="str"),
        Column(key="purpose", label="Purpose", kind="str"),
        Column(key="sources", label="Sources", kind="str"),
        Column(key="first_ts", label="Started", kind="str"),
        Column(key="span_s", label="Span", kind="secs"),
        Column(key="turns", label="Turns", kind="int"),
        Column(key="subs", label="Subagents", kind="int"),
    ]
    ordered = sorted(records, key=lambda r: r.first_ts or "", reverse=True)
    shown = ordered[:_PER_SESSION_ROW_CAP]
    rows = []
    for record in shown:
        classification = record.classification
        mode = classification.mode if classification else ""
        purpose = classification.purpose if classification else ""
        sources = (
            f"{classification.mode_source}/{classification.purpose_source}" if classification else ""
        )
        rows.append(
            [
                record.session_id,
                record.slug,
                mode,
                purpose,
                sources,
                record.first_ts or "",
                record.span_s,
                _turns_count(record),
                len(record.subs),
            ]
        )
    notes = []
    if len(records) > _PER_SESSION_ROW_CAP:
        notes.append(
            f"Showing the {_PER_SESSION_ROW_CAP} most recently started of {len(records)} sessions."
        )
    return Table(name="sessions_detail", title="Sessions", columns=columns, rows=rows, notes=notes)


def build_section(records: list[SessionRecord], mode_thresholds: dict | None = None) -> Section:
    """Build the "Sessions" report section: summary tables by mode and by
    purpose, plus a per-session detail table capped at 50 rows.

    ``mode_thresholds`` (fix 8) is only consulted for its
    ``overnight_night_start_hour``/``overnight_night_end_hour`` pair,
    reported as a section note — a reader looking at the mode breakdown
    has no other way to tell which local-time window "overnight" actually
    means without this.
    """
    features_by_id = {
        record.session_id: extract_features(record.top, record.subs, tz=None)
        for record in records
        if record.top is not None
    }
    mode_table = _group_summary_table(
        records, features_by_id, key="mode", name="sessions_by_mode", title="Sessions by mode"
    )
    purpose_table = _group_summary_table(
        records, features_by_id, key="purpose", name="sessions_by_purpose", title="Sessions by purpose"
    )
    per_session_table = _per_session_table(records)
    t = {**DEFAULT_MODE_THRESHOLDS, **(mode_thresholds or {})}
    night_start = t["overnight_night_start_hour"]
    night_end = t["overnight_night_end_hour"]
    notes = [f"Overnight window: {night_start:02d}:00-{night_end:02d}:00 local."]
    return Section(
        key="sessions",
        title="Sessions",
        tables=[mode_table, purpose_table, per_session_table],
        notes=notes,
    )


__all__ = [
    "SessionFeatures",
    "DEFAULT_MODE_THRESHOLDS",
    "DEFAULT_PURPOSE_THRESHOLDS",
    "mode_and_purpose_thresholds_from_config",
    "extract_features",
    "classify_mode",
    "classify_purpose",
    "classify_session",
    "reported_task",
    "reported_word",
    "REPORTED_PURPOSES",
    "build_session_record",
    "group_sessions",
    "build_section",
]

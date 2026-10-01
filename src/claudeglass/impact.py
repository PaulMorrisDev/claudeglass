"""Did it work? For each change point (:mod:`change_points`), the
sessions started before it against those started after it, on the
measures that change should move.

Per session, not per window, so a quiet week after a change doesn't
read as a saving: cost per session, cost per reply, the share of cache
writes that rebuilt expired context, summaries per session, the context
at session start, and, for a change to one agent, that agent's cost and
start-up context per spawn. "Before" is the sessions started in the
:data:`LOOKBACK_DAYS` before the change (and after the change before
it); "after" is those started from the change until the next one. Two
changes with fewer than :data:`MIN_SESSIONS` sessions between them
share their before and after rather than cut each other's to too few
for good: too few sessions ran with one and not the other to judge
either alone, so each is read with the other. A change made in one
project (``ChangePoint.project``) is judged on that project's sessions
only, and only changes that apply there bound it. A change to every
project is bounded in each project on its own (:func:`bounds`): by the
changes that apply there, counting that project's sessions, so a change
to one project never cuts another project's before or after, and the
all-projects reading is the project readings put together.

Sessions differ in size and kind of work, so a difference is a signal,
not proof; with fewer than :data:`MIN_SESSIONS` on either side there is
no verdict at all. Where there are enough sessions, each measure also
gets a ratio test: a ratio-of-sums estimate with delta-method variance,
Holm-corrected across the measures compared in one call (the same math
as :mod:`quality`, duplicated here since the pairs a measure draws from
sessions aren't :class:`quality.Run` signals). The "after" side is
stratum-reweighted to "before"'s mix of task/purpose (EST-P3) first, so
a change in the kind of work people did after a change doesn't read as
the change's own effect. Once at least half the sessions compared carry
metrics capture's ``level`` and ``size`` tags, how hard and how big the
work was split the strata too (:func:`stratum_fields`). Main sessions a scheduled task started with no
message of yours are a stratum of their own: their cost still counts,
but more or fewer of them running after a change doesn't read as a
saving or a rise. ``label_key`` carries the closed verdict:
``lower``, ``possibly_lower``, ``higher``, ``possibly_higher``,
``no_clear_change`` or ``too_little_data``.

A change to metrics capture is measured by what capture itself adds
per session (its notes and tags, in tokens) and how many of your
messages Claude tagged. A change to live coaching (``[capture]
coaching``) is measured by the prompting habits it warns about
(:mod:`prompting`), per 100 of your messages, and the share of your
messages that were small requests sent one at a time, as well as what
its notes add.

Each change is also checked for quality (:mod:`quality`): the runs of the
agent it changed (or the main session, for any other setting) before and
after, on every quality signal, each marked worse, better, no clear
change or too little data. A cheaper setting that makes the work worse
shows up here. Main sessions a scheduled task started with no message of
yours (``quality.Run.scheduled``) are left out of that check, as they are
of the setup comparisons.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import capture as capture_mod
from . import classify as classify_mod
from . import prompting, quality, recache
from .change_points import ChangePoint, applies_to
from .model import EventKind, TranscriptResult, scheduled_main_session
from .pricing import Pricing, price_turn
from . import snapshots as snapshots_mod
from .units import Units

#: Sessions needed on each side of a change before comparing.
MIN_SESSIONS = 3
#: How far back "before" reaches.
LOOKBACK_DAYS = 14
#: A change smaller than this (either way) reads as "about the same".
NOISE_PCT = 5.0
#: Changes this close together (one apply writing several files, say)
#: share their before and after instead of cutting each other's short,
#: as do changes with fewer than MIN_SESSIONS sessions between them.
TOGETHER = timedelta(minutes=10)
#: Two-sided significance threshold for the ratio test (see module docstring).
ALPHA = quality.ALPHA

CAVEAT = (
    "Sessions differ in size and kind of work, so read a difference as a signal, not proof. "
    "It firms up as more sessions run after the change."
)


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _priced(result: TranscriptResult):
    return [turn for turn in result.turns if turn.turn_index > 0]


@dataclass(slots=True)
class _Transcript:
    cost: float = 0.0
    turns: int = 0
    startup_tokens: int = 0
    peak_context: int = 0
    rebuild_tokens: int = 0
    write_tokens: int = 0
    summaries: int = 0
    #: Characters of capture notes injected and tags written.
    capture_chars: int = 0


@dataclass(slots=True)
class SessionFacts:
    start: datetime
    main: _Transcript
    #: Empty unless built by session_facts(); tests may build these directly.
    session_id: str = ""
    #: The kind of task Claude reported (metrics capture's task=), or None
    #: without at least two tagged messages agreeing (see classify.reported_task).
    task: str | None = None
    #: How hard and how big Claude reported the work (capture's level= and
    #: size=), the same way as ``task``; they refine its stratum.
    level: str | None = None
    size: str | None = None
    #: Heuristic purpose and mode (classify.classify_session), used to
    #: stratify the "after" side onto "before"'s mix of work (EST-P3).
    purpose: str = ""
    mode: str = ""
    #: Started by a scheduled task with no message of yours
    #: (model.scheduled_main_session): a stratum of its own.
    scheduled: bool = False
    #: Your messages, and how many of them Claude tagged.
    messages: int = 0
    tagged: int = 0
    #: How-you-prompt habits (``prompting``) and the messages that were
    #: small requests sent one at a time, for a change to coaching.
    habits: int = 0
    drip_messages: int = 0
    #: (agent type, facts) per subagent spawn.
    spawns: list[tuple[str, _Transcript]] = field(default_factory=list)
    #: Quality counts per transcript: the main session and each spawn.
    runs: list[quality.Run] = field(default_factory=list)
    #: The project, as ``snapshots.snapshot_project_key`` names it (the
    #: canonical key).
    project: str = ""
    #: Every key the project goes by (``snapshots.snapshot_project_keys``:
    #: a Windows folder can spell its drive letter either way), so a
    #: change recorded under either one applies. Empty means just ``project``.
    keys: tuple[str, ...] = ()

    @property
    def cost(self) -> float:
        return self.main.cost + sum(spawn.cost for _agent, spawn in self.spawns)


def _session_keys(session: SessionFacts) -> tuple[str, ...]:
    return session.keys or (session.project,)


def _transcript(result: TranscriptResult, pricing: Pricing) -> _Transcript:
    facts = _Transcript()
    turns = _priced(result)
    if turns:
        first = turns[0]
        facts.startup_tokens = first.input_tokens + first.cache_creation_tokens + first.cache_read_tokens
    rebuilt = {t.message_id for t in recache.detect(turns, recache.RecacheThresholds()) if t.message_id}
    for turn in turns:
        resolved = pricing.resolve_model(turn.model)
        facts.cost += price_turn(turn, resolved).total
        facts.turns += not turn.is_synthetic
        facts.peak_context = max(
            facts.peak_context, turn.input_tokens + turn.cache_creation_tokens + turn.cache_read_tokens
        )
        facts.write_tokens += turn.cache_creation_tokens
        facts.capture_chars += turn.cap_note_chars + (turn.cap.chars if turn.cap is not None else 0)
        if turn.message_id in rebuilt:
            facts.rebuild_tokens += turn.cache_creation_tokens
    facts.summaries = sum(1 for event in result.events if event.kind == EventKind.COMPACT_BOUNDARY)
    return facts


def session_facts(corpus, pricing: Pricing) -> list[SessionFacts]:
    """One :class:`SessionFacts` per session with a top-level transcript
    and a start time, oldest first. ``task``/``purpose``/``mode`` are
    classified standalone (no ``sessions.toml`` overrides or timezone --
    ``corpus.SessionBundle`` carries neither), which only affects a
    manual per-session override or timezone-sensitive mode evidence, not
    the signal EST-P3 stratifies on."""
    out: list[SessionFacts] = []
    prices = prompting._Prices(pricing)
    for bundle in corpus.sessions:
        top = bundle.top
        if top is None:
            continue
        start = next((_ts(turn.ts) for turn in _priced(top) if _ts(turn.ts)), None)
        if start is None:
            continue
        keys = snapshots_mod.snapshot_project_keys(bundle.slug) if bundle.slug else ()
        cycles = capture_mod.prompt_cycles(top)
        habit_facts = prompting.session_prompting(bundle, prices)
        habits, drip, _messages = prompting.habit_rates(habit_facts) if habit_facts else (0, 0, 0)
        task, _tagged = classify_mod.reported_task(top)
        classification = classify_mod.classify_session(
            top, bundle.subs, {}, None, workflows=len(bundle.workflows), entrypoint=top.meta.entrypoint
        )
        out.append(
            SessionFacts(
                start=start,
                main=_transcript(top, pricing),
                session_id=bundle.session_id,
                task=task,
                level=classify_mod.reported_word(top, "level")[0],
                size=classify_mod.reported_word(top, "size")[0],
                purpose=classification.purpose,
                mode=classification.mode,
                scheduled=scheduled_main_session(top),
                spawns=[(sub.meta.agent_type or "(unknown)", _transcript(sub, pricing)) for sub in bundle.subs],
                runs=quality.session_runs(bundle, pricing),
                messages=len(cycles),
                tagged=sum(1 for cycle in cycles if cycle.tag is not None),
                habits=habits,
                drip_messages=drip,
                project=keys[0] if keys else "",
                keys=keys,
            )
        )
    out.sort(key=lambda s: s.start)
    return out


#: Capture fields that refine a stratum, in order (:func:`stratum_fields`).
STRATUM_FIELDS = ("level", "size")


def stratum_fields(sessions: list[SessionFacts]) -> tuple[str, ...]:
    """The :data:`STRATUM_FIELDS` at least half of ``sessions`` carry.
    Fewer would put the rest in strata of their own, with nothing like
    them on the other side of the change."""
    return tuple(
        name for name in STRATUM_FIELDS if sessions and 2 * sum(1 for s in sessions if getattr(s, name)) >= len(sessions)
    )


def stratum(session: SessionFacts, fields: tuple[str, ...] = ()) -> str:
    """EST-P3's stratification key: ``"(scheduled)"`` for a main session a
    scheduled task started with no message of yours, else the
    capture-reported task where we have one, else the heuristic purpose,
    else a catch-all bucket; then each of ``fields`` (how hard and how
    big, from :func:`stratum_fields`), so like is compared with like."""
    if session.scheduled:
        return "(scheduled)"
    key = session.task or session.purpose or "(unspecified)"
    for name in fields:
        key += "/" + (getattr(session, name) or "-")
    return key


# -- measures ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Measure:
    key: str
    label: str
    #: "money" or "tokens" or "pct" or "count".
    kind: str
    agent: str | None = None


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _pairs(measure: Measure, sessions: list[SessionFacts]) -> list[tuple[float, float]]:
    """(numerator, denominator) pairs this measure draws from ``sessions``,
    one per session or per spawn. A ratio of sums, so a per-turn or
    per-message measure naturally weights by how many turns or messages a
    session had; a plain per-session measure (denominator 1) reduces to a
    plain average."""
    if measure.agent is not None:
        spawns = [facts for s in sessions for agent, facts in s.spawns if agent == measure.agent]
        if measure.key == "agent_cost":
            return [(f.cost, 1.0) for f in spawns]
        return [(float(f.startup_tokens), 1.0) for f in spawns]
    if measure.key == "cost_per_session":
        return [(s.cost, 1.0) for s in sessions]
    if measure.key == "cost_per_turn":
        return [(s.main.cost, float(s.main.turns)) for s in sessions]
    if measure.key == "rebuild_share":
        return [(100.0 * s.main.rebuild_tokens, float(s.main.write_tokens)) for s in sessions]
    if measure.key == "summaries":
        return [(float(s.main.summaries), 1.0) for s in sessions]
    if measure.key == "peak_context":
        return [(float(s.main.peak_context), 1.0) for s in sessions]
    if measure.key == "startup_tokens":
        return [(float(s.main.startup_tokens), 1.0) for s in sessions]
    if measure.key == "capture_tokens":
        return [
            (
                (s.main.capture_chars + sum(f.capture_chars for _agent, f in s.spawns)) / capture_mod.CHARS_PER_TOKEN,
                1.0,
            )
            for s in sessions
        ]
    if measure.key == "tagged_share":
        return [(100.0 * s.tagged, float(s.messages)) for s in sessions]
    if measure.key == "prompting_habits":
        return [(100.0 * s.habits, float(s.messages)) for s in sessions]
    if measure.key == "drip_share":
        return [(100.0 * s.drip_messages, float(s.messages)) for s in sessions]
    return []  # pragma: no cover


def _ratio_estimate(pairs: list[tuple[float, float]]) -> quality.Estimate:
    """Ratio-of-sums estimate with delta-method variance -- the same
    math as :func:`quality.estimate`, duplicated here since impact's
    pairs aren't quality.Run signals."""
    pairs = [(y, x) for y, x in pairs if x > 0]
    total_x = sum(x for _y, x in pairs)
    total_y = sum(y for y, _x in pairs)
    if total_x <= 0:
        return quality.Estimate(None, total_y, total_x, len(pairs), None)
    ratio = total_y / total_x
    n = len(pairs)
    variance = None
    if n >= 2:
        variance = n / (n - 1) * sum((y - ratio * x) ** 2 for y, x in pairs) / total_x**2
    return quality.Estimate(ratio, total_y, total_x, n, variance)


def _stratified_estimate(
    measure: Measure, before: list[SessionFacts], after: list[SessionFacts]
) -> quality.Estimate:
    """The "after" estimate, standardised to "before"'s stratum mix
    (EST-P3): each stratum's own after-side ratio, weighted by that
    stratum's share of "before". A stratum with no after-side sessions
    of its own falls back to the pooled (unweighted) after estimate for
    its contribution; with only one stratum (the common case, when
    tasks/purposes aren't set) this reduces exactly to the pooled
    estimate."""
    pooled = _ratio_estimate(_pairs(measure, after))
    if not before or not after:
        return pooled
    fields = stratum_fields(before + after)
    weights: dict[str, float] = {}
    for session in before:
        key = stratum(session, fields)
        weights[key] = weights.get(key, 0.0) + 1.0
    total = sum(weights.values())
    if total <= 0:
        return pooled
    value = 0.0
    variance = 0.0
    have_variance = True
    contributed = False
    for key, count in weights.items():
        weight = count / total
        group = [s for s in after if stratum(s, fields) == key]
        est = _ratio_estimate(_pairs(measure, group)) if group else pooled
        if est.value is None:
            est = pooled
        if est.value is None:
            continue
        contributed = True
        value += weight * est.value
        if est.variance is None:
            have_variance = False
        else:
            variance += (weight**2) * est.variance
    if not contributed:
        return pooled
    return quality.Estimate(value, pooled.num, pooled.den, pooled.runs, variance if have_variance else None)


def _value(measure: Measure, sessions: list[SessionFacts]) -> tuple[float | None, int]:
    """The measure over ``sessions``, and how many sessions or spawns it rests on."""
    est = _ratio_estimate(_pairs(measure, sessions))
    return est.value, est.runs


_COST = Measure("cost_per_session", "Cost per session", "money")
_TURN = Measure("cost_per_turn", "Cost per reply", "money")
_REBUILD = Measure("rebuild_share", "Cache writes that rebuilt expired context", "pct")
_SUMMARIES = Measure("summaries", "Conversation summaries per session", "count")
_PEAK = Measure("peak_context", "Largest context per session", "tokens")
_STARTUP = Measure("startup_tokens", "Context at the start of a session", "tokens")
_CAPTURE = Measure("capture_tokens", "Metrics capture notes and tags per session", "tokens")
_TAGGED = Measure("tagged_share", "Messages Claude tagged", "pct")
_HABITS = Measure("prompting_habits", "Prompting habits per 100 of your messages", "count")
_DRIP = Measure("drip_share", "Messages that were small requests sent one at a time", "pct")


def measures_for(point: ChangePoint) -> list[Measure]:
    """The measures a change should move, most telling first; cost per
    session always comes last as the overall check."""
    chosen: list[Measure] = []

    def add(measure: Measure) -> None:
        if measure not in chosen:
            chosen.append(measure)

    for label in point.keys or ():
        agent, _, key = label.rpartition(": ")
        key = key.split(".")[-1] if key.startswith(("effective.", "user_settings.", "project_settings.")) else key
        if label.startswith("agents."):
            parts = label.split(".")
            agent, key = (parts[1], parts[-1]) if len(parts) >= 3 else ("", key)
        if key == "capture.coaching":
            # Coaching warns about how you prompt: did it change?
            add(_HABITS)
            add(_DRIP)
            add(_CAPTURE)
        elif key.startswith("capture."):
            add(_CAPTURE)
            add(_TAGGED)
        elif agent:
            add(Measure("agent_cost", f"{agent}: cost per spawn", "money", agent))
            add(Measure("agent_startup", f"{agent}: context at the start of each spawn", "tokens", agent))
        elif key in ("model", "effortLevel", "alwaysThinkingEnabled", "MAX_THINKING_TOKENS", "fastMode"):
            add(_TURN)
        elif key == "autoCompactWindow":
            add(_SUMMARIES)
            add(_PEAK)
        elif "ttl" in key.lower():
            add(_REBUILD)
        elif key in ("skillOverrides", "enabledPlugins", "enabledMcpjsonServers", "disabledMcpjsonServers") or key.startswith(
            ("mcp_servers", "enabled_plugins")
        ):
            add(_STARTUP)
    if not chosen:
        add(_STARTUP)
    add(_COST)
    return chosen


# -- comparing -------------------------------------------------------------


def _text(kind: str, value: float | None, units: Units) -> str:
    if value is None:
        return "no data"
    if kind == "money":
        return units.money_cell(value)
    if kind == "pct":
        return f"{value:.0f}%"
    if kind == "count":
        return f"{value:.1f}"
    return f"{round(value):,} tokens"


def _p_value(z: float) -> float:
    return math.erfc(abs(z) / math.sqrt(2.0))


def _enough_estimate(est: quality.Estimate) -> bool:
    return est.value is not None and est.runs >= MIN_SESSIONS


def _measure_row(measure: Measure, before: list[SessionFacts], after: list[SessionFacts], units: Units) -> dict:
    old = _ratio_estimate(_pairs(measure, before))
    new = _stratified_estimate(measure, before, after)
    change_pct = (new.value - old.value) / old.value * 100.0 if old.value and new.value is not None else None
    row = {
        "key": measure.key,
        "label": measure.label,
        # How to read the figures: the unit ("money", "pct", "tokens",
        # "count") and which way is good ("lower", or None when neither
        # is: more of your messages tagged is coverage, not a saving).
        "kind": measure.kind,
        "better": None if measure.key == _TAGGED.key else "lower",
        "before": _text(measure.kind, old.value, units),
        "after": _text(measure.kind, new.value, units),
        "before_value": old.value,
        "after_value": new.value,
        "before_n": old.runs,
        "after_n": new.runs,
        "change_pct": round(change_pct, 1) if change_pct is not None else None,
        "direction": (
            None if change_pct is None else "same" if abs(change_pct) < NOISE_PCT
            else "lower" if change_pct < 0 else "higher"
        ),
        "p": None,
        "label_key": "too_little_data",
    }
    if _enough_estimate(old) and _enough_estimate(new):
        variance = (old.variance or 0.0) + (new.variance or 0.0)
        diff = new.value - old.value
        if variance > 0:
            row["p"] = _p_value(diff / math.sqrt(variance))
        else:
            row["p"] = 1.0 if diff == 0 else 0.0
        row["label_key"] = "no_clear_change"
    return row


def _label_rows(rows: list[dict]) -> None:
    """Holm-corrected significance labels across the measures compared in
    one :func:`compare` call (mirrors :func:`quality.compare_runs`)."""
    tested = sorted((r for r in rows if r["p"] is not None), key=lambda r: r["p"])
    m = len(tested)
    holding = True
    for rank, row in enumerate(tested):
        significant_alone = row["p"] < ALPHA
        holding = holding and row["p"] < ALPHA / (m - rank)
        if not significant_alone:
            continue
        direction = "higher" if row["after_value"] > row["before_value"] else "lower"
        row["label_key"] = direction if holding else f"possibly_{direction}"
    for row in rows:
        row["label_text"] = quality.LABELS[row["label_key"]]
        if row["p"] is not None:
            row["p"] = round(row["p"], 4)


def compare(
    point: ChangePoint,
    sessions: list[SessionFacts],
    units: Units,
    *,
    previous: ChangePoint | None = None,
    following: ChangePoint | None = None,
    per_project: dict[str, tuple[ChangePoint | None, ChangePoint | None]] | None = None,
    now: datetime | None = None,
    without=None,
) -> dict:
    """``previous``, ``following`` and ``per_project`` are what
    :func:`bounds` returns. ``without``, when given, is called with
    ``(point, before, after)`` for what the sessions after the change
    would have cost without it (``counterfactual.for_impact``); its answer
    is ``"without"``."""
    before, after = sides(
        point, sessions, previous=previous, following=following, per_project=per_project, now=now
    )
    enough = len(before) >= MIN_SESSIONS and len(after) >= MIN_SESSIONS
    rows = [_measure_row(measure, before, after, units) for measure in measures_for(point)]
    _label_rows(rows)
    return {
        "change": point.to_dict(),
        "before_sessions": len(before),
        "after_sessions": len(after),
        "enough": enough,
        "verdict": _verdict(rows, len(before), len(after), enough),
        "measures": rows,
        "quality": _quality(point, before, after, units),
        "without": without(point, before, after) if without is not None else None,
    }


def sides(
    point: ChangePoint,
    sessions: list[SessionFacts],
    *,
    previous: ChangePoint | None = None,
    following: ChangePoint | None = None,
    per_project: dict[str, tuple[ChangePoint | None, ChangePoint | None]] | None = None,
    now: datetime | None = None,
) -> tuple[list[SessionFacts], list[SessionFacts]]:
    """The sessions before ``point`` (back :data:`LOOKBACK_DAYS`, or to
    ``previous``) and after it (to ``following``, or ``now``), in the
    projects it applies to. ``per_project``, from :func:`bounds`, gives a
    project's own ``(previous, following)``, which its sessions use in
    place of the two given."""
    now = now or datetime.now(timezone.utc)
    before: list[SessionFacts] = []
    after: list[SessionFacts] = []
    for session in sessions:
        keys = _session_keys(session)
        if not applies_to(point, keys):
            continue
        own = next((per_project[k] for k in keys if k in per_project), None) if per_project else None
        earlier, later = own or (previous, following)
        start = point.ts - timedelta(days=LOOKBACK_DAYS)
        if earlier is not None and earlier.ts > start:
            start = earlier.ts
        end = later.ts if later is not None else now
        if start <= session.start < point.ts:
            before.append(session)
        elif point.ts <= session.start < end:
            after.append(session)
    return before, after


def _overlap(a: ChangePoint, b: ChangePoint) -> bool:
    """Whether two changes apply in a project in common."""
    return not a.project or not b.project or a.project == b.project


def _apart(earlier: ChangePoint, later: ChangePoint, sessions: list[SessionFacts] | None) -> bool:
    """Whether two changes are far enough apart to judge each on its own:
    more than :data:`TOGETHER` apart and, given ``sessions``, with at
    least :data:`MIN_SESSIONS` sessions they both apply to started in
    between. With fewer, cutting one's after and the other's before at
    each other would leave both too few sessions for good (nothing new
    can start in between), so they share their before and after instead."""
    if later.ts - earlier.ts <= TOGETHER:
        return False
    if sessions is None:
        return True
    between = sum(
        1
        for s in sessions
        if earlier.ts <= s.start < later.ts
        and applies_to(earlier, _session_keys(s))
        and applies_to(later, _session_keys(s))
    )
    return between >= MIN_SESSIONS


def neighbours(
    points: list[ChangePoint], point: ChangePoint, sessions: list[SessionFacts] | None = None
) -> tuple[ChangePoint | None, ChangePoint | None]:
    """The nearest earlier and later change that applies in a project in
    common with ``point`` and is :func:`_apart` from it: the changes that
    bound its before and after."""
    earlier = [p for p in points if p.ts < point.ts and _overlap(p, point)]
    later = [p for p in points if p.ts > point.ts and _overlap(p, point)]
    previous = next((p for p in reversed(earlier) if _apart(p, point, sessions)), None)
    following = next((p for p in later if _apart(point, p, sessions)), None)
    return previous, following


def bounds(
    points: list[ChangePoint], point: ChangePoint, sessions: list[SessionFacts]
) -> tuple[
    ChangePoint | None,
    ChangePoint | None,
    dict[str, tuple[ChangePoint | None, ChangePoint | None]] | None,
]:
    """``(previous, following, per_project)``: the changes that bound
    ``point``'s before and after, for :func:`sides` and :func:`compare`.

    A change made in one project is bounded by :func:`neighbours`, and
    ``per_project`` is ``None``. A change to every project is bounded in
    each project by the changes that apply there alone, with
    :func:`_apart` counted on that project's sessions: ``per_project``
    maps each project's key to its own ``(previous, following)``, so a
    change made in one project cuts that project's before and after and
    no other's, and the all-projects reading is the project readings put
    together. ``previous`` and ``following`` come from the changes to every
    project alone: they bound a session whose project isn't known, and
    ``following`` is ``None`` while no later change to every project
    closes the window, so a project with no later change of its own can
    still add sessions after ``point`` (one not seen yet included)."""
    if point.project:
        return (*neighbours(points, point, sessions), None)
    previous, following = neighbours([p for p in points if not p.project], point, sessions)
    by_project: dict[str, list[SessionFacts]] = {}
    for session in sessions:
        if session.project:
            by_project.setdefault(session.project, []).append(session)
    per_project = {}
    for project, mine in by_project.items():
        keys = _session_keys(mine[0])
        per_project[project] = neighbours([p for p in points if applies_to(p, keys)], point, mine)
    return previous, following, per_project


def quality_groups(point: ChangePoint) -> list[str]:
    """Whose runs a change's quality is judged on: each agent it changed,
    else the main session."""
    agents = [m.agent for m in measures_for(point) if m.agent]
    return list(dict.fromkeys(agents)) or [quality.MAIN]


def _quality(point: ChangePoint, before: list[SessionFacts], after: list[SessionFacts], units: Units) -> list[dict]:
    out = []
    for group in quality_groups(point):
        old = [run for s in before for run in s.runs if run.group == group and not run.scheduled]
        new = [run for s in after for run in s.runs if run.group == group and not run.scheduled]
        rows = quality.compare_runs(
            old, new, quality.signals_for(group), money=lambda value: _text("money", value, units)
        )
        out.append(
            {
                "group": group,
                "label": "Main session" if group == quality.MAIN else group,
                "before_runs": len(old),
                "after_runs": len(new),
                "verdict": quality.verdict(rows),
                #: False when every signal had too little data to compare.
                "judged": any(row["label_key"] != "too_little_data" for row in rows),
                "min_runs": quality.MIN_RUNS,
                "signals": rows,
            }
        )
    return out


def _verdict(rows: list[dict], before: int, after: int, enough: bool) -> str:
    if not enough:
        if after < MIN_SESSIONS:
            return (
                f"Too few sessions since the change to compare yet: {after} so far. "
                f"Check back after {MIN_SESSIONS}."
            )
        return (
            f"Too few sessions before the change to compare: {before} of the {MIN_SESSIONS} needed. "
            "Only sessions started before it count here."
        )
    lead = next((row for row in rows if row["change_pct"] is not None), None)
    if lead is None:
        return "No data on the measures this change should move."
    if lead["direction"] == "same":
        return f"{lead['label']}: about the same ({lead['before']} before, {lead['after']} after)."
    word = "fell" if lead["direction"] == "lower" else "rose"
    return (
        f"{lead['label']} {word} {abs(lead['change_pct']):.0f}%, from {lead['before']} to {lead['after']} "
        f"({before} sessions before, {after} after)."
    )


def impact(
    points: list[ChangePoint],
    sessions: list[SessionFacts],
    units: Units,
    *,
    limit: int | None = 10,
    without=None,
    listed=None,
) -> list[dict]:
    """Newest change first, at most ``limit`` (every one when it is
    ``None``). A change made within :data:`TOGETHER` of another, or with
    fewer than :data:`MIN_SESSIONS` sessions between them, doesn't bound
    its before or after, and a change to one project bounds that
    project's sessions only (:func:`bounds`). ``without`` is passed to
    :func:`compare`.

    ``listed``, when given, is called with a point and says whether to
    compare it: only the points it accepts are compared and returned,
    and ``limit`` counts those. Every point still bounds its neighbours,
    so a change's before and after are the same whichever are listed."""
    out = []
    for point in reversed(points):
        if listed is not None and not listed(point):
            continue
        previous, following, per_project = bounds(points, point, sessions)
        out.append(
            compare(
                point, sessions, units,
                previous=previous, following=following, per_project=per_project, without=without,
            )
        )
        if limit is not None and len(out) >= limit:
            break
    return out


__all__ = [
    "CAVEAT",
    "LOOKBACK_DAYS",
    "MIN_SESSIONS",
    "SessionFacts",
    "bounds",
    "compare",
    "impact",
    "measures_for",
    "neighbours",
    "quality_groups",
    "session_facts",
    "sides",
    "stratum",
]

"""Did a fix that landed do what its card or profile said it would?

A **fix** is a card the dashboard showed (``card_marks``) or a profile change
whose estimate was logged (``predictions``), matched to the change point it
turned into (:func:`match_point`, ``backtest._match_point``). The rows of
several views that match one point for one rule and agent are one fix
(:func:`fix_key`). :func:`read_fix` reads one fix:

* **The unit** is a spawn of the agent the change names, or, for a settings
  change, a main session that no scheduled task started.
* **The method** follows from the lever (:func:`method_for`). ``exact``: the
  same replies priced both ways (a model, fast mode, cache lifetime or raised
  compaction window; ``counterfactual.without_change``). ``start_context``:
  the median context at the start of a spawn, held on one model family (an
  agent's tools, disallowed tools, CLAUDE.md, skills or MCP servers), or a
  main-session plugins, MCP or skills change. Anything else is ``none``: no
  honest measure, because cost per session and per spawn are noise at the
  size of a card's estimate.
* **The sides** are the sessions started in the 14 days before the change
  point and in the 30 days after it (or until now), each read in the project
  alone, and cut by any other change to the same lever there, and for
  ``start_context`` by one that changes what every start carries.
* **Enough** is :data:`MIN_UNITS` units on each side (``start_context``, per
  project, on the held family) or after (``exact``, pooled). Below that the
  reading is ``too_little_data``, never a number.
* **Measured** is what the units after the change saved: ``exact`` is the
  repriced or simulated cost without the change, less what was paid;
  ``start_context`` is the drop in median start context, carried on every
  reply of each spawn after the change (``counterfactual.carry_on``).
* **Predicted** is the card's own basis, priced on the same units the
  measure is taken on: the tokens it said leave each start, carried the same
  way (``start_context``), or its saving as a share of the cost it repriced,
  applied to what the units after the change would have cost without it
  (``exact``). A card with no basis is ``not_measured``.
* **The verdict** is ``half_or_more`` when measured over predicted is 0.5 or
  more, else ``under_half``.
* **The guardrail** is the main session's corrections and rework in the
  sessions each side (for an agent, those that ran it), and the **control**
  is the same reading a fortnight earlier.

Pure: sessions, change points and transcripts come in, a :class:`Reading`
goes out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import median

from . import backtest, change_points, counterfactual, impact, quality
from .calibration import FALLBACK
from .change_points import ChangePoint

#: Units needed on each side (``start_context``) or after (``exact``).
MIN_UNITS = counterfactual.MIN_SPAWNS
#: How far before a change point the sessions read start (``impact.LOOKBACK_DAYS``).
LOOKBACK = timedelta(days=impact.LOOKBACK_DAYS)
#: How far after it they run, at most.
AFTER_MAX = timedelta(days=30)
#: Settings whose change is read by repricing the same replies.
EXACT_KEYS = frozenset({"model", "fastMode", "promptCacheTtl", "subagentPromptCacheTtl", "autoCompactWindow"})
#: An agent's settings whose change adds or removes context at each of its starts.
AGENT_START_KEYS = counterfactual.AGENT_START_KEYS
#: A change point key (prefix) that changes what every start carries.
START_SENSITIVE = (
    "enabled_plugins", "mcp_servers", "user_settings.enabledPlugins", "user_settings.skillOverrides",
    "user_settings.enabledMcpjsonServers", "user_settings.disabledMcpjsonServers", "effective.enabledPlugins",
    "effective.skillOverrides", "effective.enabledMcpjsonServers", "effective.disabledMcpjsonServers",
    "claude_md_chars",
)
#: The least share of the before median start a start-context basis may be. Where nothing
#: changed, the median start of an agent's spawns moved by up to 21.6% between two
#: fortnights on the frozen data, so a smaller basis cannot be told from that.
START_FLOOR = 0.45
#: Why a start-context reading under :data:`START_FLOOR` is not a verdict.
NOTE_BASIS_TOO_SMALL = "the basis is too small a share of the start to tell from noise"
VERDICTS = ("half_or_more", "under_half", "too_little_data", "not_measured")
GUARD_STATES = ("holds", "worse", "unclear", "too_little_data")
METHODS = ("exact", "start_context", "none")
#: The quality signals the guardrail reads: your messages that corrected Claude, and edits to files already changed.
GUARD_SIGNALS = ("corrections", "rework")


@dataclass(frozen=True)
class Prediction:
    """What a card or profile estimate rests on: its ``saving_usd`` and its
    ``basis`` (the tokens per start it takes out, or the observed cost its
    saving is a share of). ``None`` for whatever it does not name."""

    saving_usd: float | None
    basis: float | None


@dataclass
class Reading:
    """One fix, read. ``measured_usd`` and ``predicted_usd`` are ``None``
    unless the verdict is ``half_or_more`` or ``under_half``."""

    method: str
    verdict: str
    before_units: int = 0
    after_units: int = 0
    #: Projects the figures were read in.
    projects: int = 0
    predicted_usd: float | None = None
    measured_usd: float | None = None
    guard: str = "too_little_data"
    #: ``corrections`` and ``rework`` as (before count, before total, after
    #: count, after total), and ``runs`` as (before, after).
    guard_counts: dict = field(default_factory=dict)
    control: str = "too_little_data"
    note: str = ""
    #: ``too_little_data`` because the export starts less than a lookback
    #: before the change, so its before side is short: more days fix that.
    short_export: bool = False


def lever_of(change: dict) -> tuple[str | None, str]:
    """``(agent, key)`` as a change point's keys name it
    (``backtest._point_settings_keys``): the key's last dot segment, and the
    agent only for an agent's own field."""
    agent = change.get("agent") if change.get("target") == "agent" else None
    return agent or None, str(change["key"]).split(".")[-1]


def method_for(agent: str | None, key: str) -> str:
    """How a lever can be measured: ``exact``, ``start_context`` or ``none``."""
    if key in EXACT_KEYS and (agent is None or key == "model"):
        return "exact"
    if agent is not None and key in AGENT_START_KEYS:
        return "start_context"
    if agent is None and (key in counterfactual.CARRIED_KEYS or key.startswith(counterfactual._CARRIED_PREFIXES)):
        return "start_context"
    return "none"


def lever_for(levers: list[tuple[str | None, str]]) -> tuple[str | None, str]:
    """The one ``(agent, key)`` a card's changes read as: the first named,
    when all of them are for one method and all for one agent or none. A card
    that names none, or several that differ in method or in who they change
    (an agent and no agent, two agents), has no single lever: ``(None, "")``,
    which :func:`method_for` reads as ``none``."""
    if not levers:
        return None, ""
    methods = {method_for(agent, key) for agent, key in levers}
    agents = {agent for agent, _key in levers if agent is not None}
    if len(methods) > 1 or len(agents) > 1 or (agents and any(agent is None for agent, _key in levers)):
        return None, ""
    return levers[0]


def names(point: ChangePoint, agent: str | None, key: str) -> bool:
    """Whether ``point`` changes the lever ``(agent, key)``."""
    return (agent, key) in backtest._point_settings_keys(point)


def match_point(
    levers: list[tuple[str | None, str]], view_keys: set[str], landed_at: datetime, points: list[ChangePoint]
) -> ChangePoint | None:
    """The latest point (not an undo) at or before the landing, plus one
    edit's slack, that names one of the card's levers and applies in the
    card's view: ``view_keys`` is the snapshot project keys of the view, empty
    for every project. ``None`` when there is none."""
    best: ChangePoint | None = None
    for point in points:
        if point.source == "revert" or point.ts > landed_at + change_points.ONE_EDIT:
            continue
        if not any(names(point, agent, key) for agent, key in levers):
            continue
        if view_keys and point.project and point.project not in view_keys:
            continue
        if best is None or point.ts > best.ts:
            best = point
    return best


def view_rank(point: ChangePoint, view_keys: set[str]) -> int:
    """Which of the rows that match one point is the fix's own: the row whose
    view is the point's project (2), else the every-project row (1), else any
    (0). The highest rank is kept."""
    if point.project and view_keys and point.project in view_keys:
        return 2
    return 1 if not view_keys else 0


def fix_key(rule_or_lever: str, agent_type: str | None, point: ChangePoint) -> tuple:
    """What makes rows one fix: the rule (or lever), the agent, the change
    point and its project."""
    return rule_or_lever, agent_type, point.iso(), point.project


# -- the sessions each side of a change point ---------------------------------------------


def _sensitive(point: ChangePoint) -> bool:
    """Whether ``point`` changes what every start carries."""
    return any(label.startswith(START_SENSITIVE) for label in point.keys)


def _bounds(
    project_keys: set[str], point: ChangePoint, agent: str | None, key: str, method: str,
    points: list[ChangePoint], now: datetime,
) -> tuple[datetime, datetime]:
    """Where the sides of ``point`` stop in one project: a lookback before it
    and :data:`AFTER_MAX` after it (or now), each cut by another change that
    applies there and names the same lever. For ``start_context`` also by one
    that changes what every start carries, or any other setting of the same
    agent. Other changes do not cut: the method does not read cost, so a
    change elsewhere cannot move it."""
    start, end = point.ts - LOOKBACK, min(point.ts + AFTER_MAX, now)
    for other in points:
        if other is point or not change_points.applies_to(other, project_keys):
            continue
        cuts = names(other, agent, key) or (
            method == "start_context"
            and (_sensitive(other) or (agent and any(label.startswith(f"agents.{agent}.") for label in other.keys)))
        )
        if not cuts:
            continue
        if other.ts < point.ts:
            start = max(start, other.ts)
        elif other.ts > point.ts:
            end = min(end, other.ts)
    return start, end


def _sides(
    point: ChangePoint, agent: str | None, key: str, method: str, sessions: list[impact.SessionFacts],
    points: list[ChangePoint], now: datetime,
) -> list[tuple[list[impact.SessionFacts], list[impact.SessionFacts]]]:
    """``(before, after)`` per project the point applies to: the point's own
    project, or for a change to every project each project alone (as
    ``impact.bounds`` reads it)."""
    if point.project:
        mine = [s for s in sessions if point.project in impact._session_keys(s)]
        groups = [({point.project, *(k for s in mine for k in impact._session_keys(s))}, mine)]
    else:
        by_project: dict[str, list[impact.SessionFacts]] = {}
        for s in sessions:
            if s.project:
                by_project.setdefault(s.project, []).append(s)
        groups = [
            ({k for s in mine for k in impact._session_keys(s)} or {project}, mine)
            for project, mine in sorted(by_project.items())
        ]
    out = []
    for keys, mine in groups:
        start, end = _bounds(keys, point, agent, key, method, points, now)
        out.append(
            (
                [s for s in mine if start <= s.start < point.ts],
                [s for s in mine if point.ts <= s.start < end],
            )
        )
    return out


def _main_units(sessions: list[impact.SessionFacts]) -> list[impact.SessionFacts]:
    """The main sessions that count as units: those no scheduled task started."""
    return [s for s in sessions if not s.scheduled]


def _units(sessions: list[impact.SessionFacts], agent: str | None, bundles: dict) -> int:
    """How many units ``sessions`` hold: main sessions, or for an agent its runs
    whose model its agent file decides, counted where the counterfactual prices
    them (``counterfactual.agent_file_runs``), so the count and the rows agree."""
    if agent is None:
        return len(_main_units(sessions))
    mine = [bundles[s.session_id] for s in sessions if s.session_id in bundles]
    return len(counterfactual.agent_file_runs(mine, agent)[0])


def _carry_on_mains(after: list[impact.SessionFacts], tokens: float, bundles: dict, pricing) -> float:
    """What carrying ``tokens`` of context on every reply of the main
    sessions in ``after`` costs: the carry ``counterfactual._carried`` prices
    a start-context change with."""
    chars = int(round(tokens * FALLBACK))
    total = 0.0
    for s in after:
        top = bundles[s.session_id].top
        total += counterfactual._Carry(top, pricing).cost(chars, 0, len(counterfactual._priced(top)))
    return total


def _usable(number: float | None) -> bool:
    return isinstance(number, (int, float)) and not isinstance(number, bool) and number > 0


def _guard(
    before: list[impact.SessionFacts], after: list[impact.SessionFacts], agent: str | None
) -> tuple[str, dict]:
    """The guardrail on two sides: the main session's corrections and rework,
    in the sessions each side (for an agent, those that ran it), by
    ``quality.compare_runs`` over those two signals (Holm-corrected, with its
    own floors of runs and of messages and edits). ``worse`` when either
    signal is, ``too_little_data`` when both are, ``unclear`` when either is
    ``possibly_worse`` or one is ``too_little_data``, else ``holds``. With the
    counts it read."""

    def runs(side: list[impact.SessionFacts]) -> list[quality.Run]:
        ran = side if agent is None else [s for s in side if any(name == agent for name, _facts in s.spawns)]
        return [r for s in ran for r in s.runs if r.group == quality.MAIN and not r.scheduled]

    old, new = runs(before), runs(after)
    rows = quality.compare_runs(old, new, [quality.SIGNAL_BY_KEY[signal] for signal in GUARD_SIGNALS])
    counts = {
        "corrections": (
            sum(r.corrections for r in old), sum(r.human_messages for r in old),
            sum(r.corrections for r in new), sum(r.human_messages for r in new),
        ),
        "rework": (
            sum(r.rework_edits for r in old), sum(r.edits for r in old),
            sum(r.rework_edits for r in new), sum(r.edits for r in new),
        ),
        "runs": (len(old), len(new)),
    }
    labels = [row["label_key"] for row in rows]
    if "worse" in labels:
        state = "worse"
    elif all(label == "too_little_data" for label in labels):
        state = "too_little_data"
    elif "possibly_worse" in labels or "too_little_data" in labels:
        state = "unclear"
    else:
        state = "holds"
    return state, counts


def read_fix(
    point: ChangePoint,
    agent: str | None,
    key: str,
    prediction: Prediction | None,
    sessions: list[impact.SessionFacts],
    points: list[ChangePoint],
    bundles: dict,
    pricing,
    now: datetime,
    corpus_start: datetime,
) -> Reading:
    """Read one fix: the change ``point`` that landed the lever ``(agent,
    key)``, against the ``prediction`` its card or profile made.

    ``sessions`` is ``impact.session_facts(corpus, pricing)``; ``points`` every
    change point (to cut the sides); ``bundles`` maps a session id to its
    ``corpus.SessionBundle`` (a spawn's transcript is ``bundles[id].subs[i]``,
    ``i`` its index in the session's ``spawns``); ``now`` is when the sides
    end at the latest; ``corpus_start`` is the corpus's first session.

    The guardrail is read for every fix, measured or not. Its control is the
    same reading a lookback earlier (the fortnight before the change against
    the one before that), in the point's project or in every project: how
    often it says ``worse`` where nothing was changed."""
    method = method_for(agent, key)
    sides = _sides(point, agent, key, method, sessions, points, now)
    reading = _measure(point, agent, key, method, prediction, sides, bundles, pricing, corpus_start)
    reading.guard, reading.guard_counts = _guard(
        [s for before, _after in sides for s in before], [s for _before, after in sides for s in after], agent
    )
    earlier = point.ts - LOOKBACK
    mine = [s for s in sessions if not point.project or point.project in impact._session_keys(s)]
    reading.control, _counts = _guard(
        [s for s in mine if earlier - LOOKBACK <= s.start < earlier],
        [s for s in mine if earlier <= s.start < point.ts],
        agent,
    )
    return reading


def _measure(
    point: ChangePoint,
    agent: str | None,
    key: str,
    method: str,
    prediction: Prediction | None,
    sides: list[tuple[list[impact.SessionFacts], list[impact.SessionFacts]]],
    bundles: dict,
    pricing,
    corpus_start: datetime,
) -> Reading:
    """What ``read_fix`` says before the guardrail: the method, the units, the
    figures and the verdict."""
    reading = Reading(method=method, verdict="not_measured")
    if method == "none":
        reading.note = "lever has no honest measure"
        return reading
    basis = prediction.basis if prediction is not None else None
    saving = prediction.saving_usd if prediction is not None else None
    if not _usable(basis):
        reading.note = "the card or profile names no basis"
        return reading
    if method == "exact" and not _usable(saving):
        reading.note = "no saving to take a share of"
        return reading

    measured = predicted = 0.0
    before_n = after_n = counted = floored = 0
    if method == "start_context" and agent is not None:
        for before, after in sides:
            start = counterfactual.agent_start_reading(agent, before, after)
            if start is None:
                continue
            if basis / start.before_tokens < START_FLOOR:
                floored += 1
                continue
            measured += counterfactual.carry_on(start, start.drop_tokens, bundles, pricing)
            predicted += counterfactual.carry_on(start, basis, bundles, pricing)
            before_n += start.before_n
            after_n += start.after_n
            counted += 1
    elif method == "start_context":
        for before, after in sides:
            before, after = _main_units(before), _main_units(after)
            family = impact._held_model(impact._STARTUP, before, after)
            held_before = sum(1 for s in before if s.main.model == family and s.main.startup_tokens)
            held_after = sum(1 for s in after if s.main.model == family and s.main.startup_tokens)
            if family is None or held_before < MIN_UNITS or held_after < MIN_UNITS:
                continue
            # The floor is against the median start on both paths, though this measure reads the mean.
            before_median = median(s.main.startup_tokens for s in before if s.main.model == family and s.main.startup_tokens)
            if basis / before_median < START_FLOOR:
                floored += 1
                continue
            result = counterfactual.without_change(point, before, after, bundles, pricing)
            row = next(
                (
                    r
                    for r in (result.per_key if result else [])
                    if (r.agent or None, r.key) == (None, key) and r.fidelity == "approximate"
                ),
                None,
            )
            if row is None:
                continue
            measured += row.without_usd - row.paid_usd
            predicted += _carry_on_mains(after, basis, bundles, pricing)
            before_n += held_before
            after_n += held_after
            counted += 1
    else:
        before = [s for b, _a in sides for s in b]
        after = [s for _b, a in sides for s in a]
        if _units(after, agent, bundles) >= MIN_UNITS:
            result = counterfactual.without_change(point, before, after, bundles, pricing)
            row = next(
                (
                    r
                    for r in (result.per_key if result else [])
                    if (r.agent or None, r.key) == (agent, key) and r.fidelity in ("repriced", "simulated")
                ),
                None,
            )
            if row is not None:
                measured = row.without_usd - row.paid_usd
                # The card's saving as a share of the cost it repriced, applied to what the units
                # after the change would have cost without it.
                predicted = saving / basis * row.without_usd
                before_n, after_n, counted = _units(before, agent, bundles), _units(after, agent, bundles), 1

    reading.before_units, reading.after_units, reading.projects = before_n, after_n, counted
    if counted == 0 and floored:
        # Every pair that could be read had a basis too small a share of its start: not a verdict.
        reading.note = NOTE_BASIS_TOO_SMALL
        return reading
    if counted == 0:
        reading.verdict = "too_little_data"
        reading.short_export = method == "start_context" and point.ts - LOOKBACK < corpus_start
        return reading
    if predicted <= 0:
        reading.note = "the basis prices to nothing on these units"
        return reading
    reading.measured_usd, reading.predicted_usd = measured, predicted
    reading.verdict = "half_or_more" if measured / predicted >= 0.5 else "under_half"
    return reading

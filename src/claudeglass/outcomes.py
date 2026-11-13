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

from . import backtest, change_points, counterfactual, impact
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
VERDICTS = ("half_or_more", "under_half", "too_little_data", "not_measured")
GUARD_STATES = ("holds", "worse", "unclear", "too_little_data")
METHODS = ("exact", "start_context", "none")


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

"""Workstyle archetype detection (WP8): what a session's (or a corpus's)
working pattern is, so the recommendation engine (WP10) never tells an
overseer user to "stop spawning agents" or a chat-only user about
subagent TTLs.

:class:`SessionFeatures` is the evidence a caller (a later report-
assembly package, once ``SessionRecord``s exist end-to-end) extracts from
one session; this module never reads a ``TranscriptResult`` or
``WorkflowRun`` directly, so :func:`detect_archetype` stays a pure
function testable from hand-built feature values, per the plan's test
list. Raw model ids/aliases, not pre-resolved tiers, are what
``SessionFeatures`` carries — :func:`model_tier` is the one place that
resolves a tier, so a test (or a future caller) can hand it either
observed shape (a full id like ``claude-fable-5-1``, or a short alias
like ``sonnet``) and get the same answer ``detect_archetype`` would.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence, Union

from .model import Column, Section, SessionRecord, Table

#: Tier order, lowest rank first, per the plan: "fable > opus > sonnet >
#: haiku, resolved from model id substrings and agent_model_alias."
_TIER_FAMILIES: tuple[str, ...] = ("haiku", "sonnet", "opus", "fable")


def model_tier(model_id: str | None, agent_model_alias: str | None = None) -> int:
    """Resolve a model tier rank (fable=3, opus=2, sonnet=1, haiku=0) from
    a raw model id and/or an agent's declared model alias, by case-
    insensitive substring match against the family name — the observed
    id/alias shapes (``claude-fable-5-1``, ``fable[1m]``, ``sonnet``,
    ``claude-haiku-4-5-20251001``) all embed the family name literally,
    so a substring match is sufficient and doesn't need a pricing-style
    alias table.

    ``model_id`` is tried first, then ``agent_model_alias`` — a
    subagent's own turns (``Turn.model``) carry the ground truth actually
    billed, so that takes priority over the ``.meta.json`` alias when
    both are given and happen to disagree.

    Returns ``-1`` when neither string names a recognised family, so
    "unknown" always sorts below every real tier without a separate
    ``None``-handling branch at every call site.
    """
    for candidate in (model_id, agent_model_alias):
        if not candidate:
            continue
        lowered = candidate.lower()
        for rank, family in enumerate(_TIER_FAMILIES):
            if family in lowered:
                return rank
    return -1


@dataclass(slots=True)
class SessionFeatures:
    """Evidence extracted from one session, used to detect its workstyle
    archetype. See module docstring for why this holds raw model
    ids/aliases rather than pre-resolved tiers.
    """

    #: Turn.model values observed on the top-level transcript's own
    #: priced turns.
    top_level_models: tuple[str, ...] = ()
    #: (Turn.model, TranscriptMeta.agent_model_alias) pairs, one per
    #: spawned subagent transcript — either element may be ``None``.
    subagent_models: tuple[tuple[str | None, str | None], ...] = ()
    #: Subagents the main session started itself with the Agent tool: not
    #: a workflow's own agents (``meta.kind == "workflow-agent"``), and not
    #: one an agent started in turn (``report._direct_spawn_count``).
    spawn_count: int = 0
    #: Whether the session ran at least one Workflow (ultracode) run.
    has_workflow: bool = False
    #: effort value -> turn count, over every priced turn (top-level and
    #: subagent) that carried a non-None ``Turn.effort``.
    effort_turn_counts: dict[str, int] = field(default_factory=dict)
    #: Whether a ``CACHE_SIGNAL`` ``plan_mode`` attachment was observed on
    #: the top-level transcript.
    plan_mode_seen: bool = False
    #: Whether a lower-tier model turn (top-level) or a lower-tier
    #: implementer agent was observed after plan mode exited.
    post_plan_lower_tier: bool = False
    #: Distinct tool names used on the top-level transcript's priced turns.
    top_level_tool_names: frozenset[str] = frozenset()
    #: Diagnostics.agent_settings from the top-level transcript (persona
    #: evidence), carried through as evidence only — never a deciding
    #: input to any archetype's condition.
    agent_settings: dict[str, int] = field(default_factory=dict)


#: Tool names a chat-only session is allowed to have used, beyond none at
#: all — read-only inspection, never a mutation or a spawn.
_CHAT_ONLY_TOOLS = frozenset({"Read", "Grep", "Glob"})

#: Minimum share (of effort-tagged turns) an effort value needs to count
#: as one of "effort-varied"'s two-or-more qualifying levels.
_EFFORT_VARIED_MIN_SHARE = 0.20

#: Minimum direct spawns for "overseer-fanout".
_OVERSEER_MIN_SPAWNS = 3

#: Maximum direct spawns still compatible with "single-model" (a solo
#: operator who occasionally delegates a small task is still solo).
_SINGLE_MODEL_MAX_SPAWNS = 2


def detect_archetype(features: SessionFeatures) -> tuple[str, dict]:
    """Classify one session's workstyle archetype from its
    :class:`SessionFeatures`, first match wins:

    - ``overseer-fanout``: top-level model tier is strictly higher than
      every subagent's tier, AND at least 3 spawns.
    - ``plan-high-implement-low``: a plan-mode signal was seen, followed
      by a lower-tier model turn or implementer agent.
    - ``workflow-heavy``: at least one Workflow run.
    - ``effort-varied``: at least 2 distinct effort values each carrying
      at least 20% of effort-tagged turns.
    - ``single-model``: exactly one model family observed (top-level and
      subagent combined) and at most 2 spawns.
    - ``chat-only``: no spawns and no tool beyond Read/Grep/Glob.
    - ``mixed``: none of the above — a documented fallback, not itself
      one of the plan's six archetypes.

    Returns ``(archetype, evidence)``; ``evidence`` always carries
    ``features.agent_settings`` (persona evidence, per the plan) plus
    whatever specific values decided the match, so a report can cite
    exactly why a session landed where it did.
    """
    evidence: dict = {"agent_settings": dict(features.agent_settings)}

    top_tier = max((model_tier(m) for m in features.top_level_models), default=-1)
    subagent_tiers = [model_tier(model, alias) for model, alias in features.subagent_models]
    subagent_tier = max(subagent_tiers, default=-1)
    evidence["top_level_tier"] = top_tier
    evidence["subagent_tier"] = subagent_tier
    evidence["spawn_count"] = features.spawn_count

    if features.spawn_count >= _OVERSEER_MIN_SPAWNS and top_tier > subagent_tier:
        return "overseer-fanout", evidence

    if features.plan_mode_seen and features.post_plan_lower_tier:
        evidence["plan_mode_seen"] = True
        evidence["post_plan_lower_tier"] = True
        return "plan-high-implement-low", evidence

    if features.has_workflow:
        evidence["has_workflow"] = True
        return "workflow-heavy", evidence

    total_effort_turns = sum(features.effort_turn_counts.values())
    effort_shares = (
        {effort: count / total_effort_turns for effort, count in features.effort_turn_counts.items()}
        if total_effort_turns
        else {}
    )
    evidence["effort_shares"] = effort_shares
    qualifying_efforts = [
        effort for effort, share in effort_shares.items() if share >= _EFFORT_VARIED_MIN_SHARE
    ]
    if len(qualifying_efforts) >= 2:
        return "effort-varied", evidence

    # Fix (coordinator follow-up, WP12a diversity fixtures): chat-only
    # must be tested before single-model. A genuine chat-only session
    # (zero spawns, zero workflows, no plan-mode signal, and no tool
    # beyond Read/Grep/Glob) still resolves a real, single model family
    # from its own turns - so testing single-model first made chat-only
    # unreachable except when the model failed to resolve at all.
    if features.spawn_count == 0 and features.top_level_tool_names <= _CHAT_ONLY_TOOLS:
        evidence["top_level_tool_names"] = sorted(features.top_level_tool_names)
        return "chat-only", evidence

    known_families = {tier for tier in (top_tier, *subagent_tiers) if tier != -1}
    evidence["known_model_families"] = len(known_families)
    if len(known_families) == 1 and features.spawn_count <= _SINGLE_MODEL_MAX_SPAWNS:
        return "single-model", evidence

    return "mixed", evidence


def _tally(
    records: Sequence[SessionRecord], costs: Mapping[str, float] | None
) -> tuple[dict[str, int], dict[str, float]]:
    """Sessions and spend per archetype over ``records``. ``costs`` maps a
    session id to what the session cost; a record with no entry costs 0.
    A record with no archetype is in neither."""
    counts: dict[str, int] = {}
    spend: dict[str, float] = {}
    for record in records:
        if not record.archetype:
            continue
        counts[record.archetype] = counts.get(record.archetype, 0) + 1
        spend[record.archetype] = spend.get(record.archetype, 0.0) + (costs or {}).get(record.session_id, 0.0)
    return counts, spend


def _by_spend(counts: Mapping[str, int], spend: Mapping[str, float]) -> list[str]:
    """The archetypes in ``counts``, the one that cost the most first. A tie
    goes to the one with more sessions, then to the first by name. The
    workstyle table and :func:`corpus_archetype` both order with this, so
    the table's first row is always the corpus's archetype."""
    return sorted(counts, key=lambda archetype: (-spend.get(archetype, 0.0), -counts[archetype], archetype))


def corpus_archetype(
    records: Sequence[SessionRecord], costs: Mapping[str, float] | None = None
) -> tuple[str | None, dict]:
    """The archetype that accounts for the most spend across a corpus of
    already-classified ``SessionRecord``s (``record.archetype``, set per
    session by a caller via :func:`detect_archetype`), with the vote counts
    and spend as evidence.

    ``costs`` maps a session id to what that session cost, subagents
    included. Spend decides, not the count of sessions: a corpus of twenty
    short chats and three large fan-out runs that cost most of the money is
    a fan-out corpus, whatever its session count says. Without ``costs``
    (or with every session free), the count decides, as before. A tie in
    spend goes to the archetype with more sessions, then to the first by
    name (:func:`_by_spend`).

    Records with no archetype set (``None``) are counted in ``sessions``
    but not in the vote. Returns ``(None, evidence)`` when nothing has an
    archetype yet, rather than raising, since a partially-classified
    corpus is a normal intermediate state.
    """
    counts, spend = _tally(records, costs)
    evidence = {"sessions": len(records), "counts": counts, "spend": spend}
    if not counts:
        return None, evidence
    return _by_spend(counts, spend)[0], evidence


_ARCHETYPE_DESCRIPTIONS: dict[str, str] = {
    "overseer-fanout": (
        "The main session runs a larger model than its subagents and starts"
        " three or more of them, handing the work out to cheaper models."
    ),
    "plan-high-implement-low": (
        "The session plans on a larger model, then hands the building to a"
        " smaller model or to subagents once plan mode ends."
    ),
    "workflow-heavy": (
        "The session runs at least one workflow, which starts many subagents"
        " in planned phases rather than one at a time."
    ),
    "effort-varied": (
        "The session uses two or more effort levels, each for at least a"
        " fifth of its replies, rather than one level throughout."
    ),
    "single-model": (
        "The session stays on one model and starts at most two subagents:"
        " you and Claude do the work directly."
    ),
    "chat-only": (
        "The session starts no subagents and only reads and searches files"
        " (Read, Grep, Glob): a conversation rather than a build."
    ),
    "mixed": (
        "The session doesn't clearly fit any one of the other patterns."
    ),
}


def describe_archetype(archetype: str) -> str:
    """One sentence describing ``archetype``, for the report."""
    return _ARCHETYPE_DESCRIPTIONS.get(archetype, f"Unrecognised archetype: {archetype!r}.")


# -- report section -----------------------------------------------------


def build_section(
    records_or_features: Sequence[Union[SessionRecord, SessionFeatures]],
    costs: Mapping[str, float] | None = None,
) -> Section:
    """Build the "Workstyle" report section (fix item 10): one row per
    archetype, with its session count, corpus share, spend and description,
    the archetype that cost the most first (:func:`_by_spend`).

    ``costs`` maps a session id to what the session cost, as for
    :func:`corpus_archetype`. Given, each row's ``spend`` is the cost of
    its sessions and the rows run by spend; left out, ``spend`` is ``None``
    (not known, not zero) and they run by session count. Spend is only
    known for a ``SessionRecord``: raw ``SessionFeatures`` carry no id to
    look a cost up by.

    Accepts either already-classified ``SessionRecord``s (archetype read
    straight from ``record.archetype``) or raw ``SessionFeatures``
    (classified here via :func:`detect_archetype`) — a caller upstream of
    full ``SessionRecord`` assembly can still get a workstyle table
    straight from extracted evidence, and one that already has
    classified records doesn't pay to re-run detection.

    A ``SessionRecord`` with no archetype set (``None`` — not yet
    classified) is counted in a note rather than a row, the same
    "don't drop it, don't guess it" posture :func:`corpus_archetype`
    already takes.
    """
    archetypes: list[str | None] = []
    spend: dict[str, float] = {}
    for item in records_or_features:
        if isinstance(item, SessionFeatures):
            archetype, _ = detect_archetype(item)
            cost = 0.0
        else:
            archetype = item.archetype
            cost = (costs or {}).get(item.session_id, 0.0)
        archetypes.append(archetype)
        if archetype is not None:
            spend[archetype] = spend.get(archetype, 0.0) + cost

    counts: dict[str, int] = {}
    unclassified = 0
    for archetype in archetypes:
        if archetype is None:
            unclassified += 1
        else:
            counts[archetype] = counts.get(archetype, 0) + 1

    total = len(archetypes)
    rows = [
        [
            archetype,
            counts[archetype],
            100.0 * counts[archetype] / total if total else None,
            spend[archetype] if costs is not None else None,
            describe_archetype(archetype),
        ]
        for archetype in _by_spend(counts, spend if costs is not None else {})
    ]

    notes: list[str] = []
    if unclassified:
        plural = "s" if unclassified != 1 else ""
        notes.append(
            f"{unclassified} session{plural} had no archetype set and are excluded "
            "from the table above."
        )
    if not archetypes:
        notes.insert(0, "No sessions to classify in this window.")

    table = Table(
        name="workstyle_archetypes",
        title="Workstyle archetypes",
        columns=[
            Column(key="archetype", label="Archetype", kind="str"),
            Column(key="sessions", label="Sessions", kind="int"),
            Column(key="pct", label="Share", kind="pct"),
            Column(key="spend", label="Spend", kind="money"),
            Column(key="description", label="Description", kind="str"),
        ],
        rows=rows,
    )
    return Section(key="workstyle", title="Workstyle", tables=[table], notes=notes)


__all__ = [
    "SessionFeatures",
    "model_tier",
    "detect_archetype",
    "corpus_archetype",
    "describe_archetype",
    "build_section",
]

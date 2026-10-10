"""Effect measurement for the ClaudeGlass mod: a small, honest comparison of
the sessions that ran with the mod and the ones that did not.

The research on feedback tools says to expect small effects, so the
dashboard measures this tool's own. The ``prompting_effect`` table (the
``prompting`` section, beside ``prompting_glass``) compares, over the
sessions of the report's window and filters, the sessions the mod wrote that
it was present in (any ``present`` event of ``Store.read_mod_events``) with
the rest, on:

1. ``cost_per_reply`` -- the median over sessions of the session's cost
   (``report.build_report``'s per-session cost: the main conversation plus
   its agents, list price) divided by its replies (priced main-session
   turns, ``turn_index > 0`` and not the estimated compaction call).
2. ``end_context`` -- the median over sessions of the context of the last
   priced main-session turn: the same figure ``Store.median_end_context``
   takes. A ``/clear`` starts a new session, so the session before it ends
   with the context as full as it was when you cleared (or closed it); the
   transcript has no better mark of that moment.
3. ``cold_per_100`` -- cold-cache sends per 100 replies to a message of yours
   (the numerator counts only those, so the denominator does too), pooled over the
   sessions: a main-session reply to a message of yours, sent after the
   cache lifetime of the reply before it had passed (the re-cache
   detection's ``full-expiry`` by the clock, ``recache.detect``), with a
   context of ``cold_min_tokens`` or more (the coaching threshold, the size
   below which rewriting the cache costs cents). It is worked out from the
   transcripts the same way for both groups, so the comparison is fair.
   The mod's own ``cold_send`` events add what only mod sessions have: how
   many sends it warned about (a note under the table, not a comparison).
4. ``compaction_churn`` -- the share of sessions with two or more
   compactions in the main conversation. The plan names "replies past the
   compaction point"; a reply count needs the right place of each
   compaction in each transcript and says little more than this, while two
   or more compactions in one session is the churn the plan warns about
   (a session that keeps refilling and summarising), and it counts the same
   events (``compaction.compactions_in``) for both groups.
5. ``tips_desktop`` -- tips shown per Desktop session. Without the mod it is
   the tips Claude passed on in its reply (``SessionPrompting.tips``, found
   in the transcripts); with the mod, those plus the ones the mod showed
   itself (``prompting.glass_tips``). A session is a Desktop one when its
   transcript names the Claude desktop app or the mod wrote that it ran
   there. This is delivery, not behaviour: it shows how far the move to the
   mod keeps tips reaching Desktop users.

Honesty rules: both groups' values and session counts are always shown.
With fewer than :data:`MIN_SESSIONS` sessions in either group the row says
so ("Too few sessions with ClaudeGlass yet to compare") and gives no
difference. Otherwise the difference is stated in plain words ("about 12%
lower"), and called measurable only when a seeded bootstrap of the
difference (the sessions resampled, :data:`RESAMPLES` times) has a 99%
interval that leaves zero. Five figures are read at once, so each gets a
99% interval rather than 95%: the Bonferroni split of a 5% chance of a
false "Measurable" across the table. The seed is fixed per figure, so a
report built twice reads the same. Sessions with the mod may differ from the
others in other ways (who runs it, what they work on), so the table makes no
causal claim.

Non-interactive runs (the Agent SDK, :func:`is_headless`) never write a
``present`` event, since they have no surface for the mod, so they would
always land in the "without" group whatever the mod did. They are left out of
both groups. Most sessions without the mod are from before it was added, so
the comparison is mostly before and after.

Beside it, the ``prompting_tweaks`` table (:func:`build_tweaks_table`) counts
the tweaks the mod's buttons made: one row per kind of ``tweak`` event with
how often it was pressed, for ``summarise`` the share of keep-list items the
summary kept (kept / (kept + missing), pooled over the ``compaction`` events)
with the summariser told the keep list (``instructed`` = 1) and without
(``instructed`` = 0), and for ``readd`` the re-added items later replies
referenced (the ``referenced`` counts). A summary whose ``instructed`` the
mod left out (an older mod) is in neither rate. It is counts, not a
comparison of sessions, so it makes no claim about cause either.

Privacy: only counts and the figures above leave this module; no message
text, path or session id is kept in the table.
"""

from __future__ import annotations

import random
import statistics
from dataclasses import dataclass

from . import capture_catalogue as catalogue
from . import compaction, prompting, recache
from .model import Column, EventKind, Table

#: Each group needs this many sessions (with a figure) before the table
#: compares them.
MIN_SESSIONS = 10

#: The bootstrap's resamples per figure, and the two ends of its interval: 99%,
#: the Bonferroni share of a 5% error across the five figures of the table.
RESAMPLES = 1000
INTERVAL = (0.005, 0.995)

#: The Claude desktop app's transcript ``entrypoint``.
DESKTOP_ENTRYPOINT = "claude-desktop"

#: ``entrypoint`` values with no surface for the mod (``mcp``, and the Agent SDK
#: ones: ``sdk``, ``sdk-cli``, ``sdk-ts``, ``sdk-py``, ...).
HEADLESS_ENTRYPOINTS = frozenset({"mcp"})
HEADLESS_PREFIX = "sdk"

#: Main-conversation compactions that make a session "churning".
CHURN_COMPACTIONS = 2

#: The figures, in the order of the table's rows.
FIGURES = ("cost_per_reply", "end_context", "cold_per_100", "compaction_churn", "tips_desktop")

#: How each figure's cells read (``Table.row_kinds``).
ROW_KINDS = {
    "cost_per_reply": "money",
    "end_context": "tokens",
    "cold_per_100": "float",
    "compaction_churn": "pct",
    "tips_desktop": "float",
}

#: The seed of a figure's bootstrap: a constant and the figure's place, so
#: adding a figure leaves the others' draws as they were.
_SEED = 20_261_009


@dataclass
class SessionFacts:
    """What the comparison reads of one session."""

    session_id: str
    glass: bool = False
    desktop: bool = False
    cost: float = 0.0
    replies: int = 0
    #: The replies that answer a message of yours (``replies`` less the ones in
    #: the middle of the work): the base of the cold-send rate.
    answered: int = 0
    end_context: int | None = None
    cold_sends: int = 0
    compactions: int = 0
    #: Tips shown in the session: those Claude passed on, plus the mod's.
    tips: int = 0


def is_headless(entrypoint: str | None) -> bool:
    """Whether an ``entrypoint`` is a non-interactive run (the Agent SDK, the
    MCP server): the mod has no surface there."""
    word = (entrypoint or "").lower()
    return word in HEADLESS_ENTRYPOINTS or word.startswith(HEADLESS_PREFIX)


def cold_sends(top, th: recache.RecacheThresholds, min_tokens: int) -> int:
    """The cold-cache sends in a main conversation: replies to a message of
    yours that rebuilt a context of ``min_tokens`` or more after the cache
    lifetime of the reply before had passed. A rebuild that no wait caused
    (a changed prefix, the first reply after a compaction, a usage-limit
    pause) is no cold send: the hint can't warn about it."""
    flagged = {t.message_id: t for t in recache.detect(top.turns, th)}
    count = 0
    ttl: float | None = None
    for turn in top.turns:
        if turn.is_synthetic:
            continue
        before, ttl = ttl, recache.write_ttl_s(turn) or ttl
        hit = flagged.get(turn.message_id)
        if (
            hit is not None
            and hit.recache_signature == "full-expiry"
            and EventKind.HUMAN_TEXT in turn.preceding_event_kinds
            and turn.ctx >= min_tokens
            and recache.expired_by_clock(turn.gap_s, before)
        ):
            count += 1
    return count


def facts_of(sessions, corpus, session_cost: dict, th: recache.RecacheThresholds, mod_events) -> list[SessionFacts]:
    """:class:`SessionFacts` for each of ``sessions`` (``prompting.collect``:
    the sessions with a message of yours) in ``corpus``. ``session_cost`` is
    the report's cost per session id."""
    wanted = {s.session_id: s for s in sessions}
    by_session: dict[str, list[dict]] = {}
    for event in mod_events or ():
        by_session.setdefault(event.get("session_id"), []).append(event)
    min_tokens = catalogue.COACHING_THRESHOLDS["cold_min_tokens"]
    out: list[SessionFacts] = []
    for bundle in corpus.sessions:
        mine = wanted.get(bundle.session_id)
        top = bundle.top
        if mine is None or top is None or is_headless(top.meta.entrypoint):
            continue
        glass = prompting.glass_tips(by_session.get(bundle.session_id), {bundle.session_id})
        priced = [t for t in top.turns if t.turn_index > 0]
        out.append(SessionFacts(
            session_id=bundle.session_id,
            glass=bundle.session_id in glass.present,
            desktop=top.meta.entrypoint == DESKTOP_ENTRYPOINT or glass.present_surfaces["desktop"] > 0,
            cost=float(session_cost.get(bundle.session_id) or 0.0),
            replies=sum(1 for t in priced if not t.estimated),
            answered=sum(1 for t in priced if not t.estimated and EventKind.HUMAN_TEXT in t.preceding_event_kinds),
            end_context=priced[-1].ctx if priced else None,
            cold_sends=cold_sends(top, th, min_tokens),
            compactions=compaction.compactions_in(top)[0],
            tips=sum(mine.tips.values()) + sum(glass.shown.values()),
        ))
    return out


# -- the figures --------------------------------------------------------------------------
#
# Each figure is a ``values(facts)`` that picks what one session contributes
# (``None`` when it has no figure) and a ``stat(values)`` that sums the
# sessions up, so the bootstrap can resample the values.


def _median(values: list) -> float | None:
    return float(statistics.median(values)) if values else None


def _rate_per_100(values: list) -> float | None:
    replies = sum(v[1] for v in values)
    return 100.0 * sum(v[0] for v in values) / replies if replies else None


def _share_pct(values: list) -> float | None:
    return 100.0 * sum(values) / len(values) if values else None


def _mean(values: list) -> float | None:
    return sum(values) / len(values) if values else None


#: ``{figure: (values(facts) -> value | None, stat(values) -> float | None)}``.
_FIGURES = {
    "cost_per_reply": (lambda f: f.cost / f.replies if f.replies and f.cost > 0 else None, _median),
    "end_context": (lambda f: f.end_context, _median),
    "cold_per_100": (lambda f: (f.cold_sends, f.answered) if f.answered else None, _rate_per_100),
    "compaction_churn": (lambda f: f.compactions >= CHURN_COMPACTIONS, _share_pct),
    "tips_desktop": (lambda f: f.tips if f.desktop else None, _mean),
}


def _interval(figure: str, with_values: list, without_values: list, stat) -> tuple[float, float] | None:
    """The 99% bootstrap interval of the difference ``stat(with) -
    stat(without)``, the sessions of each group resampled with a generator
    seeded by the figure, so the same sessions always give the same interval."""
    rng = random.Random(_SEED + FIGURES.index(figure))
    diffs = []
    for _ in range(RESAMPLES):
        a = stat(rng.choices(with_values, k=len(with_values)))
        b = stat(rng.choices(without_values, k=len(without_values)))
        if a is not None and b is not None:
            diffs.append(a - b)
    if not diffs:
        return None
    diffs.sort()
    return diffs[int(INTERVAL[0] * (len(diffs) - 1))], diffs[int(INTERVAL[1] * (len(diffs) - 1))]


def difference_words(with_value: float | None, without_value: float | None) -> str | None:
    """How the value with the mod compares with the one without, in plain
    words: "about 12% lower"; ``None`` without both."""
    if with_value is None or without_value is None:
        return None
    if without_value == 0:
        return "about the same" if with_value == 0 else "higher, from none"
    change = 100.0 * (with_value - without_value) / abs(without_value)
    if round(abs(change)) < 1:
        return "about the same"
    return f"about {round(abs(change))}% {'lower' if change < 0 else 'higher'}"


@dataclass
class Row:
    """One figure's row, before it is written into the table."""

    figure: str
    with_value: float | None
    without_value: float | None
    with_sessions: int
    without_sessions: int
    difference: str | None
    verdict: str | None


def compare(figure: str, facts: list[SessionFacts]) -> Row:
    """One figure over the sessions with the mod and the ones without."""
    values, stat = _FIGURES[figure]
    with_values = [v for v in (values(f) for f in facts if f.glass) if v is not None]
    without_values = [v for v in (values(f) for f in facts if not f.glass) if v is not None]
    a, b = stat(with_values), stat(without_values)
    row = Row(figure, a, b, len(with_values), len(without_values), None, None)
    short = next((word for word, n in (("with", len(with_values)), ("without", len(without_values))) if n < MIN_SESSIONS), None)
    if short:
        row.difference = f"Too few sessions {short} ClaudeGlass yet to compare"
        return row
    row.difference = difference_words(a, b)
    interval = _interval(figure, with_values, without_values, stat)
    measurable = interval is not None and (interval[0] > 0 or interval[1] < 0)
    row.verdict = "Measurable" if measurable else "No measurable difference yet"
    return row


VALUE_LABELS = {
    "cost_per_reply": "Cost per reply",
    "end_context": "Context when the session ended",
    "cold_per_100": "Cold-cache sends per 100 replies to your messages",
    "compaction_churn": "Sessions with 2 or more conversation summaries",
    "tips_desktop": "Tips shown per Desktop session",
}


def build_table(sessions, corpus, session_cost: dict, th: recache.RecacheThresholds, mod_events) -> Table | None:
    """The ``prompting_effect`` table, or ``None`` when no session of the
    report has the mod (so a report on a machine without it reads as it
    always did)."""
    facts = facts_of(sessions, corpus, session_cost, th, mod_events)
    if not any(f.glass for f in facts):
        return None
    rows = [compare(figure, facts) for figure in FIGURES]
    min_tokens = catalogue.COACHING_THRESHOLDS["cold_min_tokens"]
    notes = [
        "Sessions with ClaudeGlass may differ from the others in other ways, such as what you worked on. "
        "A difference here does not show that ClaudeGlass caused it.",
        "Most sessions without ClaudeGlass are from before you added it, so the comparison is mostly before and "
        "after. Runs from the Agent SDK are left out of both groups.",
        "Measurable means the difference stayed clear of zero in a 99% resampling of the sessions, checked across "
        "the five figures together.",
        "Cost per reply and the context at the end are the middle value across sessions. The context is the last "
        "reply's, when you cleared or closed the session.",
        f"A cold-cache send is a message sent after the cache expired, with {min_tokens:,} tokens or more in "
        "context. Tips per Desktop session count the tips Claude passed on plus the ones ClaudeGlass showed.",
    ]
    ids = {f.session_id for f in facts if f.glass}
    cold = [e for e in mod_events or () if e.get("event") == "cold_send" and e.get("session_id") in ids]
    if cold:
        warned = sum(1 for e in cold if e.get("outcome") == "warned")
        notes.append(f"ClaudeGlass recorded {len(cold)} cold-cache sends in its sessions. It warned before {warned} of them.")
    return Table(
        name="prompting_effect",
        title="Sessions with and without ClaudeGlass",
        columns=[
            Column(key="figure", label="Figure", kind="str"),
            Column(key="with_glass", label="With ClaudeGlass", kind="str"),
            Column(key="without_glass", label="Without", kind="str"),
            Column(key="with_sessions", label="Sessions with", kind="int"),
            Column(key="without_sessions", label="Sessions without", kind="int"),
            Column(key="difference", label="Difference", kind="str"),
            Column(key="verdict", label="Measurable?", kind="str"),
        ],
        rows=[
            [r.figure, r.with_value, r.without_value, r.with_sessions, r.without_sessions, r.difference, r.verdict]
            for r in rows
        ],
        value_labels=dict(VALUE_LABELS),
        row_kinds=dict(ROW_KINDS),
        notes=notes,
    )


#: What each tweak is called on the page, by its ``tweak`` event ``kind``.
TWEAK_LABELS = {
    "summarise": "Summarise now",
    "readd": "Re-add lost items",
    "check": "Check with a model",
    "hold": "Hold auto-summaries for review",
}


def _kept_rate(events: list[dict]) -> float | None:
    """The share, in percent, of the keep-list items the summaries kept:
    kept / (kept + missing) pooled over ``events``; ``None`` with none."""
    kept = sum(e.get("kept") or 0 for e in events)
    total = kept + sum(e.get("missing") or 0 for e in events)
    return 100.0 * kept / total if total else None


def build_tweaks_table(sessions, mod_events) -> Table | None:
    """The ``prompting_tweaks`` table, or ``None`` when no session of the
    report has an event from the mod (so a report on a machine without it
    reads as it always did). One row per kind of tweak (:data:`TWEAK_LABELS`)
    with ``pressed`` (the ``tweak`` events of that kind), and on the
    ``summarise`` row the kept rate of the ``compaction`` events the
    summariser was told the keep list for (``kept_with``) and the ones it was
    not (``kept_without``), and on the ``readd`` row the re-added items later
    replies referenced (``referenced``, summed over the ``compaction``
    events that carry it). The rows are left out, and the dashboard says
    nothing has been pressed, when there is no tweak and no summary with an
    ``instructed`` count to show."""
    ids = {s.session_id for s in sessions}
    mine = [e for e in mod_events or () if e.get("session_id") in ids and e.get("event") in ("present", "tweak", "compaction")]
    if not mine:
        return None
    tweaks = [e for e in mine if e.get("event") == "tweak"]
    compactions = [e for e in mine if e.get("event") == "compaction"]
    with_list = [e for e in compactions if e.get("instructed") == 1]
    without_list = [e for e in compactions if e.get("instructed") == 0]
    referenced = [e["referenced"] for e in compactions if e.get("referenced") is not None]
    rows: list[list] = []
    if tweaks or with_list or without_list:
        for kind in catalogue.SENSOR_TWEAK_KINDS:
            summarise, readd = kind == "summarise", kind == "readd"
            rows.append([
                kind,
                sum(1 for e in tweaks if e.get("kind") == kind),
                _kept_rate(with_list) if summarise else None,
                _kept_rate(without_list) if summarise else None,
                sum(referenced) if readd and referenced else None,
            ])
    return Table(
        name="prompting_tweaks",
        title="Tweaks",
        columns=[
            Column(key="tweak", label="Tweak", kind="str"),
            Column(key="pressed", label="Pressed", kind="int"),
            Column(key="kept_with", label="Kept with the keep list", kind="pct"),
            Column(key="kept_without", label="Kept without it", kind="pct"),
            Column(key="referenced", label="Re-added items used later", kind="int"),
        ],
        rows=rows,
        value_labels=dict(TWEAK_LABELS),
        notes=[
            "Pressed counts each time you used the button. Kept is the share of keep-list items a summary still held, "
            "found by matching words, so a reworded item may count as missing.",
            "Kept with the keep list counts summaries where ClaudeGlass told the summariser what to keep. "
            "Kept without it counts the ones where it did not.",
            "Re-added items used later counts the ones a later reply of Claude's referred to. "
            "These are counts from your own sessions and do not show that a tweak caused a change.",
        ],
    )

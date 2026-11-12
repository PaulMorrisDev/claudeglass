"""What the dashboard keeps about a recommendation card it showed: the row
(:func:`card_row`) and whether the card's fix has landed (:func:`landing`).

A card is **shown** when its title is drawn on a page you have open, and
**copied** when a button copied its prompt (``service/api.py``'s
``POST /api/recommendations/marks`` records both in the store's
``card_marks`` table). It has **landed** when both hold:

1. it has left the view's list, which only the dashboard knows (it checks
   when a view's cards are marked shown); and
2. every setting it names has moved since the card was first shown, and
   stayed moved: over the view's snapshots, oldest first, the merged
   snapshot at each one (:func:`snapshots.with_every_project_agents` of the
   snapshots up to it) holds, for every change, a value that is not the same
   (:func:`snapshots.same_value` on the flattened key) as the value in force
   when the card was first shown (the merged snapshot of those taken at or
   before that time; unset when there was none), from some snapshot after it
   through the newest one, unbroken.

:func:`landing` answers the second half. It is not
:func:`fixes.already_set` over every change, the test ``advice`` uses to drop
a card: a fix is often made a little wider than the card asked (a tools line
with a tool or two more), and that test then reports that no advice is ever
taken. ``already_set`` is kept for ``exact``, whether the newest settings
hold the card's own values.

No I/O: the snapshots come in, a time and a flag go out.
"""

from __future__ import annotations

from datetime import datetime

from claudeglass import snapshots as snapshots_mod
from claudeglass.fixes import already_set


def flat_key(change: dict) -> str:
    """The flattened snapshot key a stored change names: ``agents.<agent>.<key>``
    for an agent's frontmatter field, else ``effective.<key>``."""
    if change["target"] == "agent":
        return f"agents.{change['agent']}.{change['key']}"
    return f"effective.{change['key']}"


def value_in(snapshot, change: dict):
    """The value a stored change's setting holds in ``snapshot``, as the
    advice rules read it (``advice._Context.agent_now`` / ``setting_now``):
    the agent's field from the snapshot's ``agents`` map, else the effective
    setting in force. ``None`` when any level is missing or not a dict."""
    if change["target"] == "agent":
        agents = snapshot.data.get("agents")
        entry = agents.get(change["agent"]) if isinstance(agents, dict) else None
        return entry.get(change["key"]) if isinstance(entry, dict) else None
    return snapshots_mod.effective_config_in_force(snapshot).get(change["key"])


def landing(
    changes: list[dict], snaps: list, canonical: dict, first_shown: datetime, cache: dict | None = None
) -> tuple[datetime, bool] | None:
    """``(landed_at, exact)``, or ``None`` when the settings ``changes`` name
    have not all moved since ``first_shown`` and stayed moved.

    ``snaps`` ascend by ``ts`` with tool lines normalised, as
    :func:`snapshots.load_snapshots` leaves them; ``canonical`` is
    :func:`snapshots.canonical_project_keys`. ``cache`` (snapshot index to
    merged snapshot) may be shared by the cards of one view. A snapshot whose
    ``ts`` does not parse is skipped. ``landed_at`` is the first snapshot of
    the final unbroken run of moved settings; ``exact`` is whether the newest
    merged snapshot holds every change's own value."""
    if not changes:
        return None
    cache = {} if cache is None else cache
    stamps = [snapshots_mod._parse_ts(s.ts) for s in snaps]

    def merged(i: int):
        if i not in cache:
            cache[i] = snapshots_mod.with_every_project_agents(snaps[: i + 1], canonical)
        return cache[i]

    start = None
    for i, when in enumerate(stamps):
        if when is not None and when <= first_shown:
            start = i
    then = merged(start) if start is not None else None
    before = [value_in(then, c) if then is not None else None for c in changes]
    run = None
    for i in range((start + 1) if start is not None else 0, len(snaps)):
        if stamps[i] is None:
            continue
        snap = merged(i)
        moved = snap is not None and all(
            not snapshots_mod.same_value(flat_key(c), old, value_in(snap, c)) for c, old in zip(changes, before)
        )
        if not moved:
            run = None
        elif run is None:
            run = stamps[i]
    if run is None:
        return None
    newest = merged(len(snaps) - 1)
    exact = all(already_set(c["key"], c["value"], value_in(newest, c)) for c in changes)
    return run, exact


def card_row(rec) -> dict:
    """What the store keeps of a card (``Store.mark_cards``): its key, the
    rule and agent it is about, the changes it names and its own saving."""
    return {
        "key": rec.key,
        "rule_id": rec.id,
        "agent_type": rec.agent_type,
        "changes": [
            {"target": c.target, "key": c.key, "agent": c.agent, "value": c.value} for c in rec.changes or ()
        ],
        "saving_usd": rec.saving_usd,
    }

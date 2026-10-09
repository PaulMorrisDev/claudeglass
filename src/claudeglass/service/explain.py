"""Why was this session expensive? In plain sentences.

:func:`explain_session` turns ``Store.session``/``Store.session_parts``
aggregates into a headline, a handful of template sentences and a cost
split. No model is asked: every sentence is a fixed template filled with
this session's own numbers, so the same data always reads the same way.
The cost split prices each model's token counts with the loaded rate
card, so its shares are list-price shares whatever the billing mode.
"""

from __future__ import annotations

from ..pricing import Pricing
from ..render.tables import format_cell
from ..units import Units

#: Parts of a reply's cost, in plain words, in the order they are shown.
COST_PARTS = (
    ("cache_read", "Re-reading the conversation from the cache"),
    ("cache_write", "Writing to the cache"),
    ("output", "Output, including thinking"),
    ("input", "New input not cached"),
)

#: What each part means for you when it leads the split.
_LEAD_ADVICE = {
    "cache_read": (
        "Every reply re-reads the whole conversation, so long conversations cost more per reply. Start a new "
        "session for a new task, or let Claude Code summarise the conversation sooner."
    ),
    "cache_write": (
        "Writing to the cache happens when a conversation starts, grows, or has to be rebuilt after a pause "
        "or a change. See the rebuilds below and {{page:cache/rebuilds}}."
    ),
    "output": "Output is the costliest token type. A lower effort level cuts thinking, which is billed as output.",
    "input": "Uncached input is usually small. A large share means the cache was often not used.",
}

_REBUILD_LABELS = {
    "full-expiry": "the cache had expired after a pause",
    "prefix-invalidated": "something changed early in the conversation",
    "post-compaction": "the conversation had just been summarised",
    "limit-expiry": "the cache expired during a usage-limit pause",
}


def _pct(part: float, whole: float) -> float:
    return 100.0 * part / whole if whole > 0 else 0.0


def cost_split(by_model: list[dict], pricing: Pricing) -> list[dict]:
    """``by_model`` priced per token type with the rate card: each part's
    list-price cost and share. Models the rate card doesn't know are
    left out of the split."""
    totals = {key: 0.0 for key, _ in COST_PARTS}
    for row in by_model:
        resolved = pricing.resolve_model(row.get("model"))
        if resolved is None:
            continue
        rates = resolved.rates
        cc_5m = row.get("cc_5m") or 0
        cc_1h = row.get("cc_1h") or 0
        # A write not split by lifetime is priced at the 5-minute rate.
        cc_other = max((row.get("cache_creation_tokens") or 0) - cc_5m - cc_1h, 0)
        totals["cache_read"] += (row.get("cache_read_tokens") or 0) * rates.cache_read / 1e6
        totals["cache_write"] += ((cc_5m + cc_other) * rates.cache_write_5m + cc_1h * rates.cache_write_1h) / 1e6
        totals["output"] += (row.get("output_tokens") or 0) * rates.output / 1e6
        totals["input"] += (row.get("input_tokens") or 0) * rates.input / 1e6
    whole = sum(totals.values())
    return [
        {"part": key, "label": label, "cost": totals[key], "share_pct": _pct(totals[key], whole)}
        for key, label in COST_PARTS
    ]


def _times(count: int) -> str:
    return "once" if count == 1 else f"{count:,} times"


def _amount(units: Units, usd: float) -> str:
    amount = units.money(usd)
    return amount.text() if amount is not None else format_cell(0.0, "money", units.currency)


def explain_session(
    detail: dict, parts: dict, pricing: Pricing, units: Units, median_cost: float | None = None
) -> dict:
    """``{"headline", "sentences", "cost_split"}`` for one session."""
    total_cost = float(detail.get("total_cost") or 0.0)
    by_agent = parts.get("by_agent", [])
    replies = sum(int(row.get("turns") or 0) for row in by_agent)
    tokens = int(detail.get("total_tokens") or 0)
    sentences: list[str] = []

    headline = f"This session cost {_amount(units, total_cost)} over {replies:,} replies ({tokens:,} tokens)."
    if median_cost and total_cost > 0:
        ratio = total_cost / median_cost
        if ratio >= 1.5:
            sentences.append(f"That is {ratio:,.1f} times your typical session.")
        elif ratio <= 0.67:
            sentences.append("That is less than your typical session.")
        else:
            sentences.append("That is about the same as your typical session.")

    split = cost_split(parts.get("by_model", []), pricing)
    ranked = sorted((row for row in split if row["share_pct"] > 0), key=lambda row: -row["share_pct"])
    if ranked:
        lead = ranked[0]
        sentences.append(f"Most of the cost went on {lead['label'].lower()}: {lead['share_pct']:.0f}%.")
        sentences.append(_LEAD_ADVICE[lead["part"]])

    sub_rows = [row for row in by_agent if row.get("kind") != "top-level"]
    sub_cost = sum(float(row.get("cost") or 0.0) for row in sub_rows)
    if sub_rows and sub_cost > 0:
        runs = sum(int(row.get("runs") or 0) for row in sub_rows)
        # One row per kind and type: a type a workflow also starts has two.
        cost_by_type: dict = {}
        for row in sub_rows:
            agent_type = row.get("agent_type")
            cost_by_type[agent_type] = cost_by_type.get(agent_type, 0.0) + float(row.get("cost") or 0.0)
        top_type, top_cost = max(cost_by_type.items(), key=lambda item: item[1])
        agent = top_type or "subagents with no recorded type"
        sentences.append(
            f"{runs:,} subagent runs made {_pct(sub_cost, total_cost):.0f}% of the cost; the costliest type was "
            f"{agent} ({_pct(top_cost, total_cost):.0f}%)."
        )
    elif not sub_rows:
        sentences.append("No subagents ran in this session.")

    rebuilds = parts.get("rebuilds", [])
    rebuild_count = sum(int(row.get("turns") or 0) for row in rebuilds)
    if rebuild_count:
        top_rebuild = max(rebuilds, key=lambda row: int(row.get("tokens") or 0))
        reason = _REBUILD_LABELS.get(top_rebuild.get("signature"), "an unrecorded reason")
        rebuilt = sum(int(row.get("tokens") or 0) for row in rebuilds)
        cause = "because" if rebuild_count == 1 else "most often because"
        sentences.append(
            f"The cache was rebuilt {_times(rebuild_count)} ({rebuilt:,} tokens written again), {cause} {reason}."
        )
    compactions = int(parts.get("compactions") or 0)
    if compactions:
        times = _times(compactions)
        sentences.append(f"Claude Code summarised the conversation {times} to make room.")

    return {"headline": headline, "sentences": sentences, "cost_split": split}


__all__ = ["COST_PARTS", "cost_split", "explain_session"]

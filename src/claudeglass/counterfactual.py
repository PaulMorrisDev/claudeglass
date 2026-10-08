"""What the sessions since a change would have cost without it.

For one change point (:mod:`change_points`) and the sessions on each side
of it (:func:`impact.sides`), each changed setting is undone on the
sessions after the change, in the most exact way that setting allows:

- **Repriced** (``"repriced"``): a model or fast mode change. The same
  replies, priced at the old model's rates (``pricing.price_turn``), or
  with fast mode the other way. The replies themselves would have
  differed on another model, so read it as the price of the same work.
- **Simulated** (``"simulated"``): a cache lifetime change, replayed
  under the old lifetime (``ttl.simulate``), or an ``autoCompactWindow``
  the change raised, replayed with the old, smaller window
  (``compaction_sim.replay_cost``). A lowered window can't be undone:
  its real compactions already happened.
- **Approximate** (``"approximate"``): context the change removed or
  added at the start of every session (a CLAUDE.md size change, MCP
  servers, plugins or skills), priced as carried on every reply
  (``context_files._Carry``). The size is the change's own CLAUDE.md
  sizes, or else the drop in context at session start from before the
  change to after it.
- **From before** (``"before"``): anything else. Each session after the
  change, at the cost per reply of the sessions before it that did the
  same kind of work, as hard and as big where capture says
  (``impact.stratum``), or all of them when none did.

A change to several settings at once is headlined by the before method:
their effects overlap, so the per-setting figures, still listed, don't
add up. With fewer than ``impact.MIN_SESSIONS`` sessions after the
change (or before it, for the before method) there is no figure.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace

from . import compaction_sim, impact, ttl
from .calibration import FALLBACK
from .change_points import ChangePoint
from .context_files import _Carry
from .pricing import Pricing, price_turn
from .units import Units

#: Settings keys whose change adds or removes context at session start.
CARRIED_KEYS = frozenset(
    ("claude_md_chars", "skillOverrides", "enabledPlugins", "enabledMcpjsonServers", "disabledMcpjsonServers")
)
_CARRIED_PREFIXES = ("mcp_servers", "enabled_plugins")
#: Cache lifetime keys, and the transcripts each applies to.
_TTL_KEYS = ("promptCacheTtl", "subagentPromptCacheTtl", "experimental.cacheTtl", "cacheTtl")
_TTL_SECONDS = {"5m": ttl.POLICY_5M, "1h": ttl.POLICY_1H}
#: A saving smaller than this either way reads as "about the same".
_SAME_USD = 0.005

FIDELITY_TEXT = {
    "repriced": "Repriced: the same replies at the old setting's prices.",
    "simulated": "Simulated: the same replies replayed under the old setting.",
    "approximate": "Approximate: the context the change removed or added, priced on every reply.",
    "before": "Estimated from your sessions before the change: their cost per reply, for each kind of work.",
}
#: Most exact first.
FIDELITY_ORDER = ("repriced", "simulated", "approximate", "before")


@dataclass(slots=True)
class KeyResult:
    key: str
    agent: str | None
    fidelity: str
    paid_usd: float
    without_usd: float
    basis: str


@dataclass(slots=True)
class Counterfactual:
    #: What the sessions after the change cost, at list price.
    paid_usd: float
    #: What they would have cost without it.
    without_usd: float
    fidelity: str
    basis: str
    sessions: int
    per_key: list[KeyResult] = field(default_factory=list)

    @property
    def saved_usd(self) -> float:
        return self.without_usd - self.paid_usd

    def to_dict(self, units: Units) -> dict:
        return {
            "paid_usd": round(self.paid_usd, 6),
            "without_usd": round(self.without_usd, 6),
            "saved_usd": round(self.saved_usd, 6),
            "fidelity": self.fidelity,
            "fidelity_text": FIDELITY_TEXT[self.fidelity],
            "basis": self.basis,
            "sessions": self.sessions,
            "text": headline(self, units),
            "since_text": since_line(self, units),
            "per_key": [
                {
                    "key": row.key,
                    "agent": row.agent,
                    "fidelity": row.fidelity,
                    "fidelity_text": FIDELITY_TEXT[row.fidelity],
                    "saved_usd": round(row.without_usd - row.paid_usd, 6),
                    "saved_text": _saved_text(row.without_usd - row.paid_usd, units),
                    "basis": row.basis,
                }
                for row in self.per_key
            ],
        }


def _saved_text(saved: float, units: Units) -> str:
    if abs(saved) < _SAME_USD:
        return "About the same"
    if saved > 0:
        return f"Saved {units.money_text(saved, prefix='about ')}"
    return f"Cost {units.money_text(-saved, prefix='about ')} more"


def headline(result: Counterfactual, units: Units) -> str:
    """"Without this change: about X. You paid Y, so it saved about Z.\""""
    lead = (
        f"Without this change: {units.money_text(result.without_usd, prefix='about ')}. "
        f"You paid {units.money_text(result.paid_usd)}"
    )
    saved = result.saved_usd
    if abs(saved) < _SAME_USD:
        return f"{lead}, about the same."
    if saved > 0:
        return f"{lead}, so it saved {units.money_text(saved, prefix='about ')}."
    return f"{lead}, so it cost {units.money_text(-saved, prefix='about ')} more."


def since_line(result: Counterfactual, units: Units) -> str:
    """The Overview's "since my last change" sentence, after its
    "Without your last change (<label>, <date>), ": the sessions it
    counts, what they would have cost, and the difference."""
    lead = (
        f"the {result.sessions:,} sessions started since would have cost "
        f"{units.money_text(result.without_usd, prefix='about ')}"
    )
    saved = result.saved_usd
    if abs(saved) < _SAME_USD:
        return f"{lead}, about what you paid."
    word = "more" if saved > 0 else "less"
    return f"{lead}: {units.money_text(abs(saved))} {word} than you paid."


# -- the parts of a session a key applies to ---------------------------------


def _mains(bundles) -> list:
    return [b.top for b in bundles if b.top is not None]


def _subs(bundles, agent: str | None) -> list:
    """Every subagent transcript, or one agent type's."""
    return [sub for b in bundles for sub in b.subs if agent is None or (sub.meta.agent_type or "(unknown)") == agent]


def _priced(result) -> list:
    return [turn for turn in result.turns if turn.turn_index > 0]


def _cost(results, pricing: Pricing) -> float:
    total = 0.0
    for result in results:
        for turn in _priced(result):
            total += price_turn(turn, pricing.resolve_model(turn.model)).total
    return total


def _dominant_model(bundles) -> str:
    counts = Counter(turn.model for top in _mains(bundles) for turn in _priced(top) if turn.model)
    return counts.most_common(1)[0][0] if counts else ""


# -- one method per kind of key -------------------------------------------------


def _repriced_model(change: dict, after, before, pricing: Pricing) -> KeyResult | None:
    agent = change.get("agent")
    old = change.get("old") or (None if agent else _dominant_model(before))
    rates = pricing.resolve_model(old) if old else None
    if rates is None:
        return None
    results = _subs(after, agent) if agent else _mains(after)
    paid = _cost(results, pricing)
    without = sum(price_turn(turn, rates).total for result in results for turn in _priced(result))
    whose = f"{agent}'s replies" if agent else "Main-session replies"
    return KeyResult(change["key"], agent, "repriced", paid, without, f"{whose} priced at {old}.")


def _repriced_fast(change: dict, after, pricing: Pricing) -> KeyResult | None:
    old = change.get("old")
    speed = "fast" if old is True else "standard" if old in (False, None) else None
    if speed is None:
        return None
    results = _mains(after)
    paid = _cost(results, pricing)
    without = sum(
        price_turn(replace(turn, speed=speed), pricing.resolve_model(turn.model)).total
        for result in results
        for turn in _priced(result)
    )
    word = "with" if speed == "fast" else "without"
    return KeyResult(change["key"], None, "repriced", paid, without, f"Main-session replies priced {word} fast mode.")


def _simulated_ttl(change: dict, after, pricing: Pricing) -> KeyResult | None:
    key, agent = change["key"], change.get("agent")
    old = _TTL_SECONDS.get(str(change.get("old")))
    if old is None:
        return None
    if key == "promptCacheTtl" and not agent:
        results, whose = _mains(after), "Main-session replies"
    else:
        results = _subs(after, agent)
        whose = f"{agent}'s replies" if agent else "Subagent replies"
    new = _TTL_SECONDS.get(str(change.get("new")))
    paid = _cost(results, pricing)
    lookup = pricing.resolve_model
    delta = 0.0
    for result in results:
        now = ttl.simulate(result.turns, lookup, new).cost if new else ttl.observed(result.turns, lookup).cost
        delta += ttl.simulate(result.turns, lookup, old).cost - now
    return KeyResult(key, agent, "simulated", paid, paid + delta, f"{whose} replayed with a {change.get('old')} cache lifetime.")


def _simulated_window(change: dict, after, pricing: Pricing) -> KeyResult | None:
    old, new = change.get("old"), change.get("new")
    if not isinstance(old, int) or isinstance(old, bool) or (isinstance(new, int) and new <= old):
        return None
    results = _mains(after)
    paid = _cost(results, pricing)
    without = compaction_sim.replay_cost(results, pricing.resolve_model, old)
    return KeyResult(
        change["key"], None, "simulated", paid, without, f"Main sessions replayed with a {old:,}-token compaction window."
    )


def _carried(key: str, change: dict | None, before_facts, after_facts, after, pricing: Pricing) -> KeyResult | None:
    """Context the change took off (or added to) the start of every
    session, carried on every main-session reply."""
    if change is not None and key == "claude_md_chars" and isinstance(change.get("old"), int) and isinstance(change.get("new"), int):
        removed_chars = change["old"] - change["new"]
        source = "the CLAUDE.md size change"
    else:
        # The start of a session is a different size on each model (the
        # tools alone are 51.5k tokens on Haiku 4.5 and 69.4k on Sonnet 5),
        # so both sides are read on one model, never a mix.
        family = impact._held_model(impact._STARTUP, before_facts, after_facts)
        if family is None:
            return None
        before_on = [s for s in before_facts if s.main.model == family and s.main.startup_tokens]
        after_on = [s for s in after_facts if s.main.model == family and s.main.startup_tokens]
        if len(before_on) < impact.MIN_SESSIONS or len(after_on) < impact.MIN_SESSIONS:
            return None
        before_start = sum(s.main.startup_tokens for s in before_on) / len(before_on)
        after_start = sum(s.main.startup_tokens for s in after_on) / len(after_on)
        removed_chars = int(round((before_start - after_start) * FALLBACK))
        source = "the change in context at session start"
    results = _mains(after)
    paid = _cost(results, pricing)
    carried = sum(_Carry(result, pricing).cost(abs(removed_chars), 0, len(_priced(result))) for result in results)
    without = paid + carried if removed_chars > 0 else paid - carried
    tokens = int(abs(removed_chars) / FALLBACK)
    word = "more" if removed_chars > 0 else "less"
    return KeyResult(
        key, None, "approximate", paid, without, f"About {tokens:,} tokens {word} carried on every reply, from {source}."
    )


def _from_before(before_facts, after_facts) -> tuple[float, float, str] | None:
    """(paid, without, basis): each session after the change at the cost
    per reply of the sessions before it that did the same kind of work."""
    if len(before_facts) < impact.MIN_SESSIONS or len(after_facts) < impact.MIN_SESSIONS:
        return None
    pooled_turns = sum(s.main.turns for s in before_facts)
    if pooled_turns <= 0:
        return None
    pooled = sum(s.cost for s in before_facts) / pooled_turns
    fields = impact.stratum_fields(before_facts + after_facts)
    by_stratum: dict[str, list[float]] = {}
    for s in before_facts:
        acc = by_stratum.setdefault(impact.stratum(s, fields), [0.0, 0.0])
        acc[0] += s.cost
        acc[1] += s.main.turns
    paid = sum(s.cost for s in after_facts)
    without = 0.0
    for s in after_facts:
        cost, turns = by_stratum.get(impact.stratum(s, fields), (0.0, 0.0))
        without += s.main.turns * (cost / turns if turns > 0 else pooled)
    basis = (
        f"{len(after_facts)} sessions since the change, at the cost per reply of "
        f"{len(before_facts)} sessions before it."
    )
    return paid, without, basis


def _key_result(key: str, change: dict | None, before_facts, after_facts, before, after, pricing: Pricing) -> KeyResult | None:
    if key in CARRIED_KEYS or key.startswith(_CARRIED_PREFIXES):
        return _carried(key, change, before_facts, after_facts, after, pricing)
    if change is None:
        return None
    if key == "model":
        return _repriced_model(change, after, before, pricing)
    if key == "fastMode":
        return _repriced_fast(change, after, pricing)
    if key in _TTL_KEYS:
        return _simulated_ttl(change, after, pricing)
    if key == "autoCompactWindow" and not change.get("agent"):
        return _simulated_window(change, after, pricing)
    return None


def _plain(label: str) -> tuple[str | None, str]:
    """(agent, key) for a change point's key label, as ``impact.measures_for`` reads it."""
    agent, _, key = label.rpartition(": ")
    if key.startswith(("effective.", "user_settings.", "project_settings.")):
        key = key.split(".", 1)[1]
    if label.startswith("agents."):
        parts = label.split(".")
        agent, key = (parts[1], parts[-1]) if len(parts) >= 3 else ("", key)
    return agent or None, key


def without_change(
    point: ChangePoint,
    before_facts: list[impact.SessionFacts],
    after_facts: list[impact.SessionFacts],
    bundles: dict,
    pricing: Pricing,
) -> Counterfactual | None:
    """What the sessions in ``after_facts`` would have cost without
    ``point``. ``bundles`` maps a session id to its
    ``corpus.SessionBundle``. ``None`` with too few sessions to say."""
    if len(after_facts) < impact.MIN_SESSIONS:
        return None
    after = [bundles[s.session_id] for s in after_facts if s.session_id in bundles]
    before = [bundles[s.session_id] for s in before_facts if s.session_id in bundles]
    changes = {(c.get("agent"), c.get("key")): c for c in point.changes if isinstance(c, dict) and "old" in c}
    wanted = list(dict.fromkeys([_plain(label) for label in point.keys] + list(changes)))
    per_key: list[KeyResult] = []
    for agent, key in wanted:
        result = _key_result(key, changes.get((agent, key)), before_facts, after_facts, before, after, pricing)
        if result is not None:
            per_key.append(result)
    paid = sum(s.cost for s in after_facts)
    if len(wanted) == 1 and len(per_key) == 1:
        only = per_key[0]
        # The method priced the transcripts it applies to; the rest of
        # the session cost the same either way.
        return Counterfactual(
            paid, paid - only.paid_usd + only.without_usd, only.fidelity, only.basis, len(after_facts), per_key
        )
    before_method = _from_before(before_facts, after_facts)
    if before_method is None:
        return None
    _paid, without, basis = before_method
    if len(wanted) > 1 and per_key:
        basis += " It changed several settings at once, so the figures per setting overlap and don't add up."
    return Counterfactual(paid, without, "before", basis, len(after_facts), per_key)


def for_impact(corpus, pricing: Pricing, units: Units):
    """A ``without`` callback for :func:`impact.impact`: the
    counterfactual for a change point and its sessions, as a dict, or
    ``None``."""
    bundles = {bundle.session_id: bundle for bundle in corpus.sessions}

    def without(point: ChangePoint, before, after) -> dict | None:
        result = without_change(point, before, after, bundles, pricing)
        return result.to_dict(units) if result is not None else None

    return without


__all__ = [
    "CARRIED_KEYS",
    "Counterfactual",
    "FIDELITY_ORDER",
    "FIDELITY_TEXT",
    "KeyResult",
    "for_impact",
    "headline",
    "since_line",
    "without_change",
]

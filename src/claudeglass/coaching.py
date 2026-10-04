"""Your own coaching thresholds (``coaching.json``), for the capture hook's
coaching notes (``coaching_notes``) and its feedback items (the facts line,
the plan check and the rating reminder).

The hook uses only the standard library and must answer in milliseconds,
so it can't build a report. This module works out, from a report of your
recent sessions, what depends on your history, and writes it to
``<config_dir>/coaching.json`` for the hook to read:

- **split_run**: agent type -> replies. An agent type's long runs would
  have cost you less split every that many replies: the ``run-split``
  tip's "Split every (replies)". Only agent types with that tip, and not
  one you ignored on the dashboard, get the hint.
- **plan_fresh**: whether the fresh-session hint after an approved plan is
  on. Off when you ignored the ``plan-handoff`` tip, or when most of your
  /cg-feedback answers say the builds after a plan relied on the
  discussion before it (``handoff._feedback_on_plans``): a fresh start
  would have lost what they needed. On otherwise, the hook's default.
- **thresholds.plan_fresh_tokens**: the planning context a plan must keep
  before the hint applies, ``plan_handoff_min_dropped_tokens`` (the same
  line the tip draws). Lowered by ``coaching_rearm_factor`` when most of
  your handoff answers say the plan was enough: the hint then speaks at the
  approval sooner.
- **muted** and **thresholds** (``drip_count``, ``big_paste_tokens``,
  ``cold_min_tokens``): what your answers to the tip question and Claude's
  own misfire calls do to a tip (``capture_catalogue.TIP_WRONG_MIN``). A
  hint called wrong that many times in all has its number raised by
  ``coaching_rearm_factor``, or, with no number of its own, is muted: the
  hook skips it.
- **once**: hints you answered "right but I knew" more often than "useful",
  at least ``capture_catalogue.TIP_KNOWN_MIN`` times: the hook shows each
  once a session.
- **typical_piece_tokens**: the tokens in your median piece of work, for the
  facts line (``typical=``) and the rating reminder's size threshold
  (twice this, at least a million). A piece of work is what
  ``pieces.corpus_pieces`` draws from the transcripts, with no rating or
  tag needed: a session starts one, a /clear or a new task starts
  another, and a session that opens with a handoff joins the piece it
  carries on. The median of the pieces' main-transcript tokens (input,
  cache writes, cache reads and output), over those with at least
  :data:`MIN_PIECE_REPLIES` replies, and only once there are
  :data:`MIN_PIECES` of them; ``0`` before that.

The file holds agent-type names, hint ids and numbers only: no paths,
prompts or session ids. The dashboard's service rewrites it once a day
(``service/coaching_job.py``); ``claudeglass capture refresh``
rewrites it now. The hook reads a missing or unreadable file as empty:
no split hint, the plan hint on.
"""

from __future__ import annotations

import json
import math
import statistics
from datetime import datetime, timezone
from pathlib import Path

from . import capture_catalogue, handoff, ignores, pieces, prompting
from .config import _write_atomic

#: Days of sessions the file is worked out from.
DAYS = 30
#: The service rewrites the file once it is this old.
MAX_AGE_HOURS = 24
_VERSION = 1
#: Fewest replies a piece of work needs to count towards the typical one,
#: and fewest such pieces before their median is trusted.
MIN_PIECE_REPLIES = 3
MIN_PIECES = 5


def path(config_dir: str | Path) -> Path:
    return Path(config_dir) / capture_catalogue.COACHING_FILE


def read(config_dir: str | Path) -> dict:
    """``coaching.json`` as written, or ``{}`` when it's missing or
    unreadable."""
    try:
        data = json.loads(path(config_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def age_hours(config_dir: str | Path, now: datetime | None = None) -> float | None:
    """How long ago the file was built, or ``None`` without one."""
    built = read(config_dir).get("built_at")
    try:
        at = datetime.fromisoformat(built) if isinstance(built, str) else None
    except ValueError:
        return None
    if at is None:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return ((now or datetime.now(timezone.utc)) - at).total_seconds() / 3600


def session_tokens(top) -> int:
    """The tokens one main transcript used: input, cache writes, cache
    reads and output (the report's own definition), summed over its
    replies."""
    return sum(
        turn.input_tokens + turn.cache_creation_tokens + turn.cache_read_tokens + turn.output_tokens
        for turn in top.turns
        if turn.turn_index > 0
    )


def typical_piece_tokens(corpus) -> int:
    """The median of the main-session tokens of ``corpus``'s pieces of work
    (``pieces.corpus_pieces``: a reply to an agent's report counts for the
    piece that started the agent, and a session that opens with a handoff
    is part of the piece it carries on), over the pieces with at least
    :data:`MIN_PIECE_REPLIES` replies; ``0`` with fewer than
    :data:`MIN_PIECES` of them. Counts only: nothing of a transcript is
    kept."""
    sizes = [piece.tokens for piece in pieces.corpus_pieces(corpus) if piece.replies >= MIN_PIECE_REPLIES]
    return int(statistics.median(sizes)) if len(sizes) >= MIN_PIECES else 0


def _evidence_value(rec, label: str):
    return next((value for item_label, value, *_ in rec.evidence if item_label == label), None)


def _rearm_factor(config_thresholds: dict | None) -> float:
    """``coaching_rearm_factor``: ``config.toml``'s own, else the default
    (at least 1: a factor under it would lower what it raises)."""
    value = (config_thresholds or {}).get("coaching_rearm_factor")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        value = capture_catalogue.COACHING_THRESHOLDS["rearm_factor"]
    return max(1.0, float(value))


def tip_rules(report, factor: float) -> tuple[list[str], list[str], dict[str, int]]:
    """``(muted, once, raised)`` from the tip answers in ``report``'s
    ``prompting_tips`` table. A hint called wrong
    (:data:`capture_catalogue.TIP_WRONG_MIN` answers of ``wrong`` and times
    Claude disowned it, in all) has its threshold raised by ``factor`` when
    it has one of its own (:data:`capture_catalogue.TIP_THRESHOLD_KEYS`),
    else it is muted. One you knew
    (:data:`capture_catalogue.TIP_KNOWN_MIN` answers of ``known``, more than
    of ``useful``) shows once a session. Wrong comes first: a hint called
    wrong is not shown once either."""
    muted: list[str] = []
    once: list[str] = []
    raised: dict[str, int] = {}
    for hint, tally in sorted(prompting.tip_tallies(report).items()):
        if hint not in capture_catalogue.TIP_HINT_TITLES:
            continue
        if tally["wrong"] + tally["misfires"] >= capture_catalogue.TIP_WRONG_MIN:
            key = capture_catalogue.TIP_THRESHOLD_KEYS.get(hint)
            if key is None:
                muted.append(hint)
            else:
                raised[key] = math.ceil(capture_catalogue.COACHING_THRESHOLDS[key] * factor)
        elif tally["known"] >= capture_catalogue.TIP_KNOWN_MIN and tally["known"] > tally["useful"]:
            once.append(hint)
    return muted, once, raised


def from_report(report, config_dir: str | Path, config_thresholds: dict | None = None,
                now: datetime | None = None, typical: int = 0) -> dict:
    """What ``coaching.json`` holds, from ``report`` (every project).
    ``config_thresholds``: ``config.toml``'s ``[thresholds]``. ``typical``:
    :func:`typical_piece_tokens` of the corpus the report was built from."""
    recs = list(report.recommendations)
    skip = ignores.skip_keys(config_dir, recs, None)
    split_run: dict[str, int] = {}
    plan_fresh = True
    for rec in recs:
        if rec.id == "run-split" and rec.agent_type and rec.key not in skip:
            every_n = _evidence_value(rec, "Split every (replies)")
            if isinstance(every_n, int) and every_n > 0:
                split_run[rec.agent_type] = every_n
        elif rec.id == "plan-handoff" and rec.key in skip:
            plan_fresh = False
    feedback = handoff._feedback_on_plans(report)
    if feedback["answers"] >= handoff.MIN_FEEDBACK_ANSWERS and feedback["no"] * 2 > feedback["answers"]:
        plan_fresh = False
    th = handoff.HandoffThresholds.from_config(config_thresholds)
    factor = _rearm_factor(config_thresholds)
    plan_fresh_tokens = int(th.min_dropped_tokens)
    if feedback["answers"] >= handoff.MIN_FEEDBACK_ANSWERS and feedback["yes"] * 2 > feedback["answers"]:
        # Most of your builds could have started from the plan alone: speak
        # at the approval after less planning.
        plan_fresh_tokens = int(plan_fresh_tokens / factor)
    muted, once, raised = tip_rules(report, factor)
    return {
        "version": _VERSION,
        "built_at": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
        "days": DAYS,
        "split_run": dict(sorted(split_run.items())),
        "plan_fresh": plan_fresh,
        "thresholds": {"plan_fresh_tokens": plan_fresh_tokens, **raised},
        "muted": muted,
        "once": once,
        "typical_piece_tokens": max(0, int(typical)),
    }


def write(config_dir: str | Path, data: dict) -> Path:
    target = path(config_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(target, json.dumps(data, indent=2, sort_keys=True) + "\n")
    return target


def _hint_name(hint: str) -> str:
    return capture_catalogue.TIP_HINT_TITLES.get(hint, hint)


def _raised(data: dict, key: str) -> bool:
    """Whether ``data`` raises ``key`` above its default."""
    value = (data.get("thresholds") or {}).get(key)
    return (
        isinstance(value, (int, float)) and not isinstance(value, bool)
        and value > capture_catalogue.COACHING_THRESHOLDS[key]
    )


def describe(data: dict) -> list[str]:
    """``coaching.json`` in words, for ``capture status``."""
    if not data:
        return ["No split points yet: the dashboard's service works them out from your sessions once a day."]
    splits = data.get("split_run") or {}
    lines = []
    if splits:
        lines.append(
            "Split hint for: " + ", ".join(f"{agent} (every {n} replies)" for agent, n in sorted(splits.items()))
        )
    else:
        lines.append("No agent type's runs cost you more for running long, so no split hint.")
    if data.get("plan_fresh") is False:
        lines.append("The fresh-session hint after a plan is off: you ignored its tip, or said builds need the discussion.")
    muted = [hint for hint in data.get("muted") or () if hint in capture_catalogue.TIP_HINT_TITLES]
    if muted:
        lines.append("Tips left out because you called them wrong: " + ", ".join(_hint_name(hint) for hint in muted) + ".")
    raised = [
        _hint_name(hint)
        for hint, key in capture_catalogue.TIP_THRESHOLD_KEYS.items()
        if _raised(data, key)
    ]
    if raised:
        lines.append("Tips that now wait for more because you called them wrong: " + ", ".join(raised) + ".")
    once = [hint for hint in data.get("once") or () if hint in capture_catalogue.TIP_HINT_TITLES]
    if once:
        lines.append("Tips shown once a session because you already knew them: " + ", ".join(_hint_name(hint) for hint in once) + ".")
    typical = data.get("typical_piece_tokens")
    if isinstance(typical, int) and typical > 0:
        lines.append(f"Your typical piece of work is about {typical:,} tokens (the median piece of your last {DAYS} days).")
    else:
        lines.append("Your typical piece of work isn't known yet: it needs a few more sessions.")
    return lines


__all__ = [
    "DAYS",
    "MAX_AGE_HOURS",
    "MIN_PIECES",
    "MIN_PIECE_REPLIES",
    "age_hours",
    "describe",
    "from_report",
    "tip_rules",
    "path",
    "read",
    "session_tokens",
    "typical_piece_tokens",
    "write",
]

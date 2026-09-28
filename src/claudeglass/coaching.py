"""Your own coaching thresholds (``coaching.json``), for the capture hook's
coaching notes (``coaching_notes``).

The hook uses only the standard library and must answer in milliseconds,
so it can't build a report. This module works out, from a report of your
recent sessions, the two hints that depend on your history, and writes
them to ``<config_dir>/coaching.json`` for the hook to read:

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
  line the tip draws).

The file holds agent-type names and numbers only: no paths, prompts or
session ids. The dashboard's service rewrites it once a day
(``service/coaching_job.py``); ``claudeglass capture refresh``
rewrites it now. The hook reads a missing or unreadable file as empty:
no split hint, the plan hint on.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import capture_catalogue, handoff, ignores
from .config import _write_atomic

#: Days of sessions the file is worked out from.
DAYS = 30
#: The service rewrites the file once it is this old.
MAX_AGE_HOURS = 24
_VERSION = 1


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


def _evidence_value(rec, label: str):
    return next((value for item_label, value, *_ in rec.evidence if item_label == label), None)


def from_report(report, config_dir: str | Path, config_thresholds: dict | None = None,
                now: datetime | None = None) -> dict:
    """What ``coaching.json`` holds, from ``report`` (every project).
    ``config_thresholds``: ``config.toml``'s ``[thresholds]``."""
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
    return {
        "version": _VERSION,
        "built_at": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
        "days": DAYS,
        "split_run": dict(sorted(split_run.items())),
        "plan_fresh": plan_fresh,
        "thresholds": {"plan_fresh_tokens": int(th.min_dropped_tokens)},
    }


def write(config_dir: str | Path, data: dict) -> Path:
    target = path(config_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(target, json.dumps(data, indent=2, sort_keys=True) + "\n")
    return target


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
    return lines


__all__ = ["DAYS", "MAX_AGE_HOURS", "age_hours", "describe", "from_report", "path", "read", "write"]

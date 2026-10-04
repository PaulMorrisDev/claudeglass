"""What the dashboard shows about metrics capture: the Capture tab and
the banner on every tab.

:func:`view` turns ``[capture]`` as set, the metric catalogue, what
each level and metric would cost (``capture.history``, your own last
14 days) and what capture has cost since it was turned on
(``capture.usage``) into one JSON-safe dict, with amounts phrased for
the billing mode (``units.Units``). :func:`config_block` is the cheap
part ``/api/health`` carries on every poll. Nothing here reads a file
or the store: ``service/api.py`` gathers the inputs and caches them.

No path or project name ever goes into the output: hook problems are
counted, not quoted (their text can name a script path), and
``[capture] projects`` patterns are reported only as "limited".
"""

from __future__ import annotations

from datetime import datetime

from . import capture as capture_mod
from . import capture_catalogue as catalogue
from . import habits
from . import ratings as ratings_mod
from .config import CAPTURE_SAMPLES, CaptureConfig
from .render.tables import format_cell
from .units import NO_LIMIT_SHARE_HINT

#: The cost warning, shown before anything that makes Claude use more
#: tokens (the init question and ``capture on`` say the same).
WARNING = (
    "Metrics capture uses your tokens. Claude reads a short note when a session or subagent starts, "
    "and ends each reply with a one-line tag you will see, such as [cg: task=bugfix brief=clear]. "
    "The free level only logs a few events to a local file."
)

CONNECT_COMMAND = "claudeglass capture connect"
STATUS_COMMAND = "claudeglass capture status"
FEEDBACK_COMMAND = "claudeglass capture feedback on"
BRIEF_COMMAND = "claudeglass capture brief on"


def _skill_words(name: str, command: str) -> tuple[dict[str, str], dict[str, str]]:
    states = {
        "missing": f"The /{name} skill isn't installed",
        "outdated": f"The /{name} skill is out of date",
        "renamed": f"The /{name} skill is still under an old name",
        "foreign": f"Another skill named {name} is in the way: move it elsewhere first",
    }
    notes = {
        "missing": f"{states['missing']}: {command}",
        "outdated": f"{states['outdated']}: {command}",
        "renamed": f"{states['renamed']}: {command}",
        "foreign": f"Another skill named {name} is in the way: move it elsewhere, then run {command}",
    }
    return states, notes


#: What to say when the /cg-feedback skill is on but its file isn't right
#: (``footprint.feedback_skill_state``), and the same with the command,
#: for the banner and ``capture status``. The dashboard never writes it:
#: it lives in Claude Code's own folder.
SKILL_STATES, SKILL_NOTES = _skill_words(catalogue.FEEDBACK_SKILL, FEEDBACK_COMMAND)
#: The same for the /cg-brief skill (brief templates).
BRIEF_SKILL_STATES, BRIEF_SKILL_NOTES = _skill_words(catalogue.BRIEF_SKILL, BRIEF_COMMAND)

#: A metric that installs a skill -> (its state's key in the feedback
#: facts, the state words, the command that installs it).
_SKILL_METRICS = {
    "feedback_skill": ("skill", SKILL_STATES, FEEDBACK_COMMAND),
    "brief_templates": ("brief_skill", BRIEF_SKILL_STATES, BRIEF_COMMAND),
}

#: What to say when a status-line toggle is on but Claude Code's status
#: line isn't this tool's (``footprint.is_own_statusline``), so the
#: second line never shows.
STATUSLINE_NOTES = {
    "feedback_note": "Your status line isn't ClaudeGlass's, so this second line won't show there; the banner "
    "here still does. 'claudeglass init --connect' offers to set the status line up.",
    "coaching_line": "Your status line isn't ClaudeGlass's, so this line won't show. "
    "'claudeglass init --connect' offers to set the status line up. Where there's no status line, "
    "such as the desktop app, coaching notes bring the same hints into the conversation.",
}

#: What a metric costs when it uses tokens without Claude being asked to
#: write anything for it, in place of the Capture page's "No tokens."
COST_NOTES = {
    "coaching_notes": "About 50 to 140 tokens a note, only when a hint applies.",
}

#: Said when ``coaching_notes`` is turned on, before the yes/no: what the
#: notes are, when they come and what they cost.
COACHING_NOTES_ON = (
    "Coaching notes: when a hint applies, a hook adds a short note (about 50 to 140 tokens) to Claude's context, "
    "after a tool result or when you send a message, and Claude acts on it or tells you in one line. "
    "They run at any capture level, at most one of a kind every half hour in a session. "
    "'claudeglass capture status' shows how many there were and what they cost."
)

#: Said when the tagger changes (``capture tagger``): who writes the tags
#: now, what that sends where, and what it costs.
TAGGER_TEXT = {
    "haiku": "Claude Haiku now writes the tags. After each of your messages is answered, the hook sends Haiku a "
    "short excerpt, through your own Claude Code login. It holds your message, what Claude did and the end of its "
    "reply. Only the tag's words are kept, in a local file. Claude's replies no longer end with a tag, and the "
    f"session no longer carries the tag list. Each call costs about ${catalogue.JUDGE_USD_PER_CALL:.4f} of Haiku, "
    "in the background, so you never wait for it. It takes effect in new sessions.",
    "claude": "Claude now writes the tags again, at the end of its final reply to each of your messages. "
    "When it leaves one out, the hook asks Haiku for it in the background, through your own Claude Code login, "
    f"at about ${catalogue.JUDGE_USD_PER_CALL:.4f} a call. It takes effect in new sessions.",
}

#: Below this percentage of your messages tagged, once there are
#: :data:`LOW_COVERAGE_MIN_CYCLES` of them, the banner says Claude is
#: skipping tags.
LOW_COVERAGE_PCT = 60.0
LOW_COVERAGE_MIN_CYCLES = 20

#: Sessions started since capture was turned on, with no note in any of
#: them, before the banner says the hook doesn't seem to run.
NO_NOTES_MIN_SESSIONS = 3


def amount_text(units, usd: float, period: str = "", *, prefix: str = "") -> str:
    """``usd`` phrased for the billing mode (a share of the weekly limit
    on a subscription, when it can be worked out); ``"nothing"`` for
    zero. ``prefix`` (UX-2, e.g. ``"about "``) is joined via
    ``Amount.phrase``, which avoids doubling "about" when the phrased
    text already opens with it -- not applied to the tiny-API-amount
    "under $0.01" branch, which already reads as an approximation."""
    if usd <= 0:
        return "nothing"
    if units.billing_mode != "subscription" and usd < 0.005:
        return f"under {format_cell(0.01, 'money', units.currency)}" + (f" {period}" if period else "")
    amount = units.money(usd, period=period)
    return amount.phrase(prefix) if amount is not None else "nothing"


def describe(capture: CaptureConfig) -> str:
    """One line: the level and, when on, its date, end and sample."""
    if not capture.is_on:
        return "Off"
    parts = [catalogue.LEVEL_TITLES.get(capture.level, capture.level)]
    if capture.enabled_at:
        parts.append(f"since {capture.enabled_at[:10]}")
    if capture.until:
        parts.append(f"until {capture.until[:16].replace('T', ' ')}")
    if capture.sample < 100:
        parts.append(f"{capture.sample}% of sessions")
    if capture.haiku_tags:
        parts.append("tags by Haiku")
    return parts[0] + (f" ({', '.join(parts[1:])})" if len(parts) > 1 else "")


def config_block(capture: CaptureConfig, now: datetime | None = None) -> dict:
    """``[capture]`` as set, for ``/api/health`` and the Capture tab."""
    expired = capture.is_on and capture.expired(now)
    return {
        "level": capture.level,
        "title": catalogue.LEVEL_TITLES.get(capture.level, capture.level),
        "describe": describe(capture),
        "on": capture.is_on,
        "expired": expired,
        "effective": capture.is_on and not expired,
        "enabled_at": capture.enabled_at,
        "until": capture.until,
        "timebox_days": catalogue.DEFAULT_CAPTURE_TIMEBOX_DAYS,
        "sample": capture.sample,
        "metrics": list(capture.active_metrics()),
        "feedback": list(capture.feedback),
        "coaching": list(capture.coaching),
        "tagger": capture.tagger,
        "projects_limited": bool(capture.projects),
    }


def hooks_block(health) -> dict:
    """Whether settings.json runs the entries the chosen metrics need.
    Problems are counted, never quoted: their text can hold a path."""
    if health is None:
        return {
            "ok": None, "summary": "", "missing": [], "missing_events": [], "problems": 0,
            "connect_command": CONNECT_COMMAND, "blocked_by": None,
        }
    blocked_by = getattr(health, "blocked_by", None)
    if health.ok or blocked_by is not None:
        # A settings policy (hook_health.POLICY_TEXT) is the one reason
        # 'capture connect' can't fix, so its sentence stands alone.
        summary = health.summary()
    else:
        # One sentence however many are missing: the banner shows this on
        # every tab, and the Capture tab lists them (``missing``).
        missing = len(health.missing)
        if missing == 1:
            parts = [f"settings.json does not run {health.missing[0].describe()}."]
        elif missing:
            parts = [f"settings.json lacks {missing} of the hook entries your metrics need, so they aren't captured."]
        else:
            parts = []
        if health.problems:
            count = len(health.problems)
            parts.append(
                f"{count} capture hook entr{'y' if count == 1 else 'ies'} in settings.json can't run "
                f"('{STATUS_COMMAND}' says why)."
            )
        summary = " ".join(parts) + f" Run '{CONNECT_COMMAND}' to fix it."
    return {
        "ok": health.ok,
        "summary": summary,
        "missing": [spec.describe() for spec in health.missing],
        "missing_events": sorted({spec.event for spec in health.missing}),
        "problems": len(health.problems),
        "connect_command": CONNECT_COMMAND,
        "blocked_by": blocked_by,
    }


def _pct(value: float | None) -> str:
    return format_cell(value, "pct") if value is not None else ""


def share_text(value: float | None) -> str:
    """A share of spend: "under 0.1%" rather than a bare "0.0%"."""
    if value is None:
        return ""
    return "under 0.1%" if 0 < value < 0.05 else _pct(value)


def _tokens(value: int) -> str:
    return format_cell(value, "tokens")


def _money(units, usd: float, period: str = "", *, prefix: str = "") -> dict:
    return {
        "usd": round(usd, 6),
        "text": amount_text(units, usd, period, prefix=prefix) if units is not None else "",
    }


def _estimate_block(est, units) -> dict:
    tokens = round((est.note_tokens + est.tag_tokens) * 7 / est.days) if est.days else 0
    return {
        "tokens_per_week": tokens,
        "tokens_text": _tokens(tokens),
        **_money(units, est.per_week, "a week"),
        "share_pct": est.share,
        "share_text": share_text(est.share),
    }


def _levels(capture: CaptureConfig, past, units) -> list[dict]:
    estimates = (
        capture_mod.level_estimates(past, capture.sample, capture.tagger) if past is not None and past.sessions else {}
    )
    out = []
    for level in catalogue.LEVELS:
        ids = catalogue.level_includes(level)
        adds = [m.title for m in catalogue.METRICS if m.group == level or (m.group == "feedback" and m.id in ids)]
        est = estimates.get(level)
        out.append(
            {
                "id": level,
                "title": catalogue.LEVEL_TITLES[level],
                "summary": catalogue.LEVEL_SUMMARIES[level],
                "adds": adds,
                "metrics": list(ids),
                "asks_claude": any(catalogue.asks_claude(i) for i in ids),
                "current": capture.level == level,
                "rough": catalogue.rough_tokens(ids, capture.tagger),
                "estimate": _estimate_block(est, units) if est is not None and est.cost > 0 else None,
            }
        )
    custom_ids = catalogue.with_requirements(capture.metrics) if capture.level == catalogue.CUSTOM_LEVEL else ()
    custom = (
        capture_mod.estimate(past, custom_ids, capture.sample, capture.tagger)
        if custom_ids and past is not None and past.sessions
        else None
    )
    out.append(
        {
            "id": catalogue.CUSTOM_LEVEL,
            "title": catalogue.LEVEL_TITLES[catalogue.CUSTOM_LEVEL],
            "summary": "Pick the metrics one by one in the table below.",
            "adds": [],
            "metrics": list(custom_ids),
            "asks_claude": any(catalogue.asks_claude(i) for i in custom_ids),
            "current": capture.level == catalogue.CUSTOM_LEVEL,
            "rough": catalogue.rough_tokens(custom_ids, capture.tagger),
            "estimate": _estimate_block(custom, units) if custom is not None and custom.cost > 0 else None,
        }
    )
    return out


def _marginal(past, active: tuple[str, ...], metric_id: str, sample: int, tagger: str = catalogue.DEFAULT_TAGGER) -> float:
    """USD over the replayed days that ``metric_id`` adds to ``active``
    (or saves, when it is already on)."""
    def cost(ids) -> float:
        return capture_mod.estimate(past, ids, sample, tagger).cost

    if metric_id in active:
        without = tuple(
            i for i in active if i != metric_id and metric_id not in catalogue.METRICS_BY_ID[i].requires
        )
        return cost(active) - cost(without)
    with_it = catalogue.with_requirements(active + (metric_id,)) + tuple(
        i for i in active + (metric_id,) if i in catalogue.FEEDBACK_IDS
    )
    return cost(with_it) - cost(active)


def _kind(metric) -> str:
    if metric.group in catalogue.LEVEL_GROUPS:
        return "level"
    return metric.group


def _feedback_facts(metric_id: str, feedback: dict) -> tuple[dict | None, int | None, int | None]:
    """``(actual, answers, target)`` for the skill, the plan check and
    the dashboard rating, over the last :data:`capture.HISTORY_DAYS`
    days."""
    target = capture_mod.ENOUGH["feedback"]
    if metric_id == "feedback_skill" and feedback.get("use") is not None:
        use = feedback["use"]
        return _money(feedback.get("units"), use.feedback_cost), use.feedback_answered, target
    if metric_id == "plan_check" and feedback.get("use") is not None:
        use = feedback["use"]
        return _money(feedback.get("units"), use.by_metric.get(metric_id, 0.0)), use.plan_checks_answered, target
    if metric_id == "feedback_reminder" and feedback.get("use") is not None:
        use = feedback["use"]
        return _money(feedback.get("units"), use.by_metric.get(metric_id, 0.0)), None, None
    if metric_id == "dashboard_rating" and feedback.get("ratings") is not None:
        return None, feedback["ratings"], target
    return None, None, None


def _metric_row(
    metric, capture: CaptureConfig, active, past, units, use, signal_sessions, missing_events, feedback=None
) -> dict:
    kind = _kind(metric)
    if kind == "derived":
        on = True
    elif kind == "feedback":
        on = metric.id in capture.feedback
    elif kind == "coaching":
        on = metric.id in capture.coaching
    else:
        on = metric.id in active
    asks = catalogue.asks_claude(metric.id)
    estimate = None
    if asks and past is not None and past.sessions:
        usd = max(0.0, _marginal(past, active, metric.id, capture.sample, capture.tagger)) * 7 / past.days if past.days else 0.0
        estimate = _money(units, usd, "a week")
    actual = None
    actual_label = "Since it was turned on"
    have = want = None
    if use is not None and on and kind in ("level", "feedback"):
        if asks:
            actual = _money(units, use.by_metric.get(metric.id, 0.0))
        if kind == "level":
            have, want = capture_mod.enough_data(use, metric.id, signal_sessions.get(metric.id, 0))
    skill = (feedback or {}).get("skill")
    if on and kind == "feedback" and metric.id in ("feedback_skill", "dashboard_rating", "plan_check", "feedback_reminder"):
        fb_actual, have, want = _feedback_facts(metric.id, {**(feedback or {}), "units": units})
        if fb_actual is not None:
            actual, actual_label = fb_actual, f"Over the last {capture_mod.HISTORY_DAYS} days"
    coaching_use = (feedback or {}).get("coaching_use")
    if on and metric.id == "coaching_notes" and coaching_use is not None:
        actual = _money(units, coaching_use.cost)
        actual_label = f"{_plural(coaching_use.notes, 'note')} over the last {capture_mod.HISTORY_DAYS} days"
    install = _SKILL_METRICS.get(metric.id)
    skill_now = (feedback or {}).get(install[0]) if install else None
    needs_install = bool(on and install and skill_now not in (None, "installed"))
    no_statusline = bool(on and metric.id in STATUSLINE_NOTES and (feedback or {}).get("statusline") is False)
    needs_hook = bool(on and kind != "derived" and set(metric.hooks) & missing_events)
    return {
        "id": metric.id,
        "kind": kind,
        "group": metric.group,
        "section": metric.section,
        "title": metric.title,
        "what": metric.what,
        "why": metric.why,
        "powers": [catalogue.THEMES.get(p, p) for p in metric.powers],
        "tag": metric.tag,
        "hooks": list(metric.hooks),
        "requires": list(metric.requires),
        "on": on,
        "toggle": kind != "derived",
        "asks_claude": asks,
        "needs_hook": needs_hook,
        "hook_command": CONNECT_COMMAND if needs_hook else None,
        "needs_install": needs_install,
        "install_note": install[1].get(skill_now) if needs_install else None,
        "install_command": install[2] if needs_install else None,
        "statusline_note": STATUSLINE_NOTES[metric.id] if no_statusline else None,
        "cost_note": COST_NOTES.get(metric.id),
        "estimate": estimate,
        "actual": actual,
        "actual_label": actual_label,
        "answers": have,
        "target": want,
        "enough": (have >= want) if have is not None and want else None,
    }


def _worth(rows: list[dict], capture: CaptureConfig, use, units, now: datetime | None = None) -> list[dict]:
    """CAP-5 (gap 4): each active, asked metric's measured cost a week
    against the decisions it feeds (``powers``, already worked out on
    each row) -- the same trace ``capture_catalogue.render_markdown``
    gives statically (rough output tokens per occurrence), measured here
    from your own transcripts instead, in money a week (``use.by_metric``
    only splits cost, not raw tokens, per metric). Empty while there's no
    start time to spread a week's figure over, nothing measured yet, or
    (SURV-8) fewer than ``habits.MIN_GROUP`` sessions have a note to
    measure from yet -- a metric's per-note weighting looks stable well
    before that many sessions, and a table built from one session's notes
    (formerly a dozen rows from a single session) is noise, not a trend."""
    if use is None or not capture.enabled_at:
        return []
    if use.sessions < habits.MIN_GROUP:
        return []
    weeks = capture_mod.weeks_since(capture.enabled_at, now)
    if not weeks:
        return []
    worth = []
    for row in rows:
        if not row["on"] or not row["asks_claude"]:
            continue
        total = use.by_metric.get(row["id"], 0.0)
        if total <= 0:
            continue
        worth.append({"id": row["id"], "title": row["title"], "feeds": row["powers"], **_money(units, total / weeks, "a week")})
    worth.sort(key=lambda r: r["usd"], reverse=True)
    return worth


def _measured(use, units) -> dict | None:
    if use is None:
        return None
    return {
        "since": use.since,
        "sessions": use.sessions,
        "subagents": use.subagents,
        "notes": use.notes,
        "note_tokens": use.note_tokens,
        "tag_tokens": use.tag_tokens,
        "tokens_text": _tokens(use.note_tokens + use.tag_tokens),
        **_money(units, use.cost),
        "share_pct": use.share,
        "share_text": share_text(use.share),
        "cycles": use.cycles,
        "tagged_cycles": use.tagged_cycles,
        "coverage_pct": use.coverage,
        "coverage_text": _pct(use.coverage),
        # While Claude writes the tags: the share it tagged itself and the
        # share Claude Haiku filled in, each empty until the fallback filled one.
        "own_coverage_text": (
            _pct(100.0 * (use.tagged_cycles - use.filled_cycles) / use.cycles) if use.cycles and use.filled_cycles else ""
        ),
        "filled_text": _pct(100.0 * use.filled_cycles / use.cycles) if use.cycles and use.filled_cycles else "",
        "reports": use.reports,
        "tagged_reports": use.tagged_reports,
        "judged": use.judged,
        "report_coverage_pct": use.report_coverage,
        "scopes": {
            scope: {"note_tokens": s.note_tokens, "tag_tokens": s.tag_tokens, **_money(units, s.cost)}
            for scope, s in sorted(use.scopes.items())
        },
        # SURV-3: notes landing after a compact boundary, shown as their
        # own line rather than folded into a scope's cost -- the carried
        # prefix a compaction would have discounted them against is gone.
        "after_compact": {"notes": use.after_compact_notes, **_money(units, use.after_compact_cost)},
        "daily": [{"day": day, "usd": round(usd, 6)} for day, usd in sorted(use.daily.items())],
    }


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def _roi(weekly_cost: float | None, dependent_value: float | None, units) -> dict | None:
    """What capture costs a week against what suggestions that depend on
    it or your feedback are worth a week, in billing units. ``None``
    while there's no start time to price a weekly cost from (capture
    off, or never turned on with a start time). ``value`` stays ``None``,
    not a zero, while nothing measured yet depends on either."""
    if weekly_cost is None:
        return None
    # UX-2: weekly_cost/dependent_value are already per-week figures. A
    # subscription's own phrasing already says "of your weekly usage
    # limit" (units.Units.money), so period="a week" there would read as
    # "weekly usage limit a week" -- only stated for API billing, where
    # the phrased amount is just a dollar figure. Both amounts also carry
    # "about " via Amount.phrase, which dedupes against a subscription's
    # own "about" rather than doubling it (_banner/page-capture.js's ROI note
    # doesn't repeat "about" itself, relying on this). A subscription with
    # no usage-limit readings is phrased in list-price dollars instead,
    # which need "a week" like API billing's, or they read as a total.
    share = units is not None and units.billing_mode == "subscription"
    if share:
        amount = units.money(weekly_cost)
        share = amount is not None and amount.basis != NO_LIMIT_SHARE_HINT
    period = "" if share else "a week"
    return {
        "cost": _money(units, weekly_cost, period, prefix="about "),
        "value": _money(units, dependent_value, period, prefix="about ") if dependent_value is not None else None,
        "measured": dependent_value is not None,
    }


def _step_down_note(capture: CaptureConfig, rows: list[dict]) -> str | None:
    """CAP-7: a specific one-level step-down command, once every metric
    a step down would actually drop has enough of its own evidence
    (the same per-metric readiness the "Enough collected" note below
    already uses, scoped to just those metrics rather than every active
    one). Cheap -- only looks at ``rows``, already built from
    ``capture.usage``, no new replay -- so it runs on every ``/api/
    capture`` poll. Unlike ``habits.capture_step_down_suggestion`` (the
    report's own CAP-7 note), this never checks whether ``d_level`` has
    settled: that needs a full habits pass over the corpus, which this
    endpoint doesn't already pay for and polling shouldn't add. The
    report's capture section carries the fuller, calibration-gated
    suggestion; this is the lighter dashboard hint the "if cheap"
    allowance covers."""
    target = habits.CAPTURE_STEP_DOWN.get(capture.level)
    if target is None:
        return None
    dropped_ids = set(catalogue.level_metrics(capture.level)) - set(catalogue.level_metrics(target))
    dropped_rows = [r for r in rows if r["id"] in dropped_ids and r["asks_claude"]]
    if not dropped_rows or any(r["enough"] is not True for r in dropped_rows):
        return None
    return (
        f"Every metric {catalogue.LEVEL_TITLES[capture.level]} adds over {catalogue.LEVEL_TITLES[target]} has "
        f"enough collected ({habits.metric_list(r['id'] for r in dropped_rows)}). "
        + habits.step_down_terms(capture.level, target)
    )


def _unrated_block(unrated: dict | None) -> dict | None:
    """The banner's list of sessions waiting for a rating: ``unrated``
    (``api.py``'s ``_capture_unrated``) with the sentence that introduces
    it, or ``None`` when there are none."""
    if not unrated or not unrated["pieces"]:
        return None
    total = unrated["total"]
    return {
        **unrated,
        "text": f"{_plural(total, 'session')} used at least {unrated['threshold_text']} tokens and "
        f"{'has' if total == 1 else 'have'} no rating yet. Rating them makes your savings tips fit how you work.",
    }


def _banner(
    capture, config, levels, measured, use, rows, hooks, started_since, skill=None, brief_skill=None, roi=None,
    unrated=None,
) -> dict:
    """The banner's lines: a headline, then any notes worth acting on, and
    the sessions waiting for a rating (``unrated``)."""
    notes: list[str] = []
    feedback_note = catalogue.FEEDBACK_NOTE if "feedback_note" in capture.feedback else None
    waiting = _unrated_block(unrated)
    for metric_id, state, words in (
        ("feedback_skill", skill, SKILL_NOTES),
        ("brief_templates", brief_skill, BRIEF_SKILL_NOTES),
    ):
        skill_row = next((r for r in rows if r["id"] == metric_id), None)
        if skill_row is not None and skill_row["needs_install"] and state in words:
            notes.append(words[state])
    if not capture.is_on:
        essentials = next((lv for lv in levels if lv["id"] == "essentials"), None)
        est = essentials["estimate"] if essentials else None
        invite = "Metrics capture is off. Turn it on to get suggestions that fit how you work."
        if est is not None and est["text"]:
            share = f", {est['share_text']} of what you spent" if est["share_text"] else ""
            invite = (
                "Metrics capture is off. At Essentials it would have cost about "
                f"{est['tokens_text']} tokens and {est['text']}{share}, for suggestions that fit how you work."
            )
        return {"on": False, "headline": invite, "notes": notes, "feedback_note": feedback_note, "unrated": waiting}

    parts = [f"Metrics capture: {config['title']}"]
    if capture.enabled_at:
        parts.append(f"since {capture.enabled_at[:10]}")
    if capture.sample < 100:
        parts.append(f"{capture.sample}% of sessions")
    if measured is not None and (measured["sessions"] or measured["subagents"]):
        parts.append(f"{measured['tokens_text']} tokens")
        if measured["text"]:
            share = f" ({measured['share_text']} of spend)" if measured["share_text"] else ""
            parts.append(measured["text"] + share)
        if measured["coverage_text"]:
            parts.append(f"tagged on {measured['coverage_text']} of messages")
    elif measured is not None:
        parts.append("no captured sessions yet")
    if config["expired"]:
        notes.append(
            f"Its end time ({capture.until[:16].replace('T', ' ')}) has passed, so nothing is captured now."
        )
    if hooks.get("ok") is False:
        notes.append(hooks["summary"])
    elif (
        use is not None
        and not use.sessions
        and started_since * capture.sample / 100 >= NO_NOTES_MIN_SESSIONS
        and any(catalogue.asks_claude(i) for i in capture.active_metrics())
    ):
        notes.append(
            f"No capture note seen in the {_plural(started_since, 'session')} started since it was turned on: "
            f"the hook may be blocked. '{STATUS_COMMAND}' checks it."
        )
    if (
        use is not None
        and use.cycles >= LOW_COVERAGE_MIN_CYCLES
        and use.coverage is not None
        and use.coverage < LOW_COVERAGE_PCT
    ):
        notes.append(
            f"Claude tagged only {_pct(use.coverage)} of your messages, so some figures rest on few answers."
        )
    if roi is not None and roi["cost"]["usd"] > 0 and roi["cost"]["text"]:
        # UX-2: roi["cost"]["text"]/["value"]["text"] already carry their
        # own "about" (see _roi) -- not repeated here, or a subscription's
        # would double into "about about X%...".
        if roi["measured"]:
            notes.append(
                f"Capture cost {roi['cost']['text']}; suggestions that rely on it are worth "
                f"{roi['value']['text']}."
            )
        else:
            notes.append(f"Capture cost {roi['cost']['text']}; nothing measured yet relies on it.")
    counted = [r for r in rows if r["enough"] is not None and r["asks_claude"]]
    ready = [r for r in counted if r["enough"]]
    step_note = _step_down_note(capture, rows)
    if step_note is not None:
        notes.append(step_note)
    elif counted and len(ready) == len(counted):
        notes.append("Enough collected for every metric on: you could lower the level to save its cost.")
    elif ready:
        notes.append(
            f"Enough collected for {', '.join(r['title'].lower() for r in ready)}: "
            "you could switch them off to save their cost."
        )
    return {
        "on": True, "headline": " · ".join(parts), "notes": notes, "feedback_note": feedback_note, "unrated": waiting,
    }


def view(
    capture: CaptureConfig,
    *,
    past=None,
    units=None,
    use=None,
    hooks=None,
    signal_sessions: dict[str, int] | None = None,
    started_since: int = 0,
    feedback_use=None,
    skill: str | None = None,
    brief_skill: str | None = None,
    ratings: int | None = None,
    statusline: bool | None = None,
    weekly_cost: float | None = None,
    dependent_value: float | None = None,
    coaching_use=None,
    unrated: dict | None = None,
    now: datetime | None = None,
) -> dict:
    """Everything the Capture tab and the banner show.

    ``past`` is ``capture.history`` over your recent sessions (``None``
    when it couldn't be worked out, and then nothing is estimated);
    ``use`` is ``capture.usage`` since capture was turned on (``None``
    while it is off); ``hooks`` is ``hook_health.check_capture`` for the
    metrics on; ``signal_sessions`` maps each free signal's metric id to
    the sessions that logged it since capture was turned on;
    ``started_since`` is how many sessions started since then.
    ``feedback_use`` is ``capture.feedback_usage`` over the last
    :data:`capture.HISTORY_DAYS` days, ``skill`` the
    ``footprint.feedback_skill_state`` of the /cg-feedback skill,
    ``brief_skill`` that of the /cg-brief skill and
    ``ratings`` how many sessions you rated on the dashboard (each only
    while its toggle is on). ``coaching_use`` is
    ``capture.coaching_usage`` over the same days, while coaching notes
    are on. ``statusline`` is whether Claude Code's
    status line is this tool's (``None`` when not checked).
    ``weekly_cost`` is ``capture.weekly_cost(use)`` and ``dependent_value``
    ``habits.capture_dependent_value`` over the same window: together
    they're the capture-pays-for-itself figures in ``roi`` and the
    banner (``None`` while there's no measured cost to weigh anything
    against). ``unrated`` is the sessions big enough for the rating reminder
    that have no rating (``None`` while the reminder and the dashboard
    rating are both off): the banner lists them.
    """
    signal_sessions = signal_sessions or {}
    config = config_block(capture, now)
    hooks_data = hooks_block(hooks)
    missing_events = set(hooks_data["missing_events"])
    active = capture.active_metrics()
    levels = _levels(capture, past, units)
    feedback = {
        "use": feedback_use,
        "skill": skill,
        "brief_skill": brief_skill,
        "ratings": ratings,
        "statusline": statusline,
        "coaching_use": coaching_use,
    }
    rows = [
        _metric_row(m, capture, active, past, units, use, signal_sessions, missing_events, feedback)
        for m in catalogue.METRICS
    ]
    sections = [
        {"id": section, "title": title, "metrics": [r for r in rows if r["section"] == section]}
        for section, title in catalogue.SECTIONS.items()
    ]
    measured = _measured(use, units)
    worth = _worth(rows, capture, use, units, now)
    history = (
        {"days": past.days, "sessions": past.sessions, "subagents": past.subagents, "cycles": past.cycles}
        if past is not None
        else None
    )
    roi = _roi(weekly_cost, dependent_value, units)
    return {
        "config": config,
        "warning": WARNING,
        "tagger_text": dict(TAGGER_TEXT),
        "samples": list(CAPTURE_SAMPLES),
        "levels": levels,
        "sections": [s for s in sections if s["metrics"]],
        "measured": measured,
        "worth": worth,
        # SURV-8: so a consumer can explain an empty ``worth`` as "N of
        # <worth_min_sessions> sessions with notes" rather than silence.
        "worth_min_sessions": habits.MIN_GROUP,
        "history": history,
        "hooks": hooks_data,
        "billing": {
            "mode": units.billing_mode if units is not None else "",
            "basis": units.basis() if units is not None else "",
        },
        "roi": roi,
        "banner": _banner(
            capture, config, levels, measured, use, rows, hooks_data, started_since, skill, brief_skill, roi, unrated
        ),
        "feedback": {
            "skill": skill,
            "runs": feedback_use.feedback_runs if feedback_use is not None else None,
            "answered": feedback_use.feedback_answered if feedback_use is not None else None,
            "ratings": ratings,
            "days": capture_mod.HISTORY_DAYS,
            "questions": [
                {
                    "key": q.key,
                    "question": ratings_mod.fill_question(q),
                    "multi": q.multi,
                    "options": [{"word": word, "label": label} for word, label, _text in q.options],
                }
                for q in catalogue.RATING_QUESTIONS
            ],
        },
        "commands": {
            "status": STATUS_COMMAND,
            "connect": CONNECT_COMMAND,
            "feedback": FEEDBACK_COMMAND,
            "brief": BRIEF_COMMAND,
        },
    }


def change_commands(before: CaptureConfig, changes: dict) -> list[str]:
    """The ``claudeglass capture`` commands that make ``changes``
    (``set_capture``'s arguments) from the CLI, for when the dashboard
    can't write ``config.toml`` itself."""
    out: list[str] = []
    level = changes.get("level")
    if level == "off":
        return ["claudeglass capture off"]
    if level is not None:
        out.append(f"claudeglass capture level {level}")
    if "metrics" in changes:
        now_on = [i for i in before.active_metrics() if i in catalogue.LEVEL_METRIC_IDS]
        wanted = list(catalogue.with_requirements(changes["metrics"]))
        added = [i for i in wanted if i not in now_on]
        dropped = [i for i in now_on if i not in wanted]
        if added:
            out.append("claudeglass capture enable " + " ".join(added))
        if dropped:
            out.append("claudeglass capture disable " + " ".join(dropped))
    for key in ("feedback", "coaching"):
        if key in changes:
            current = list(getattr(before, key))
            added = [i for i in changes[key] if i not in current]
            dropped = [i for i in current if i not in changes[key]]
            if added:
                out.append("claudeglass capture enable " + " ".join(added))
            if dropped:
                out.append("claudeglass capture disable " + " ".join(dropped))
    options = []
    if changes.get("sample") is not None:
        options.append(f"--sample {changes['sample']}")
    if changes.get("until"):
        options.append(f"--until {changes['until']}")
    if options:
        out.append("claudeglass capture on " + " ".join(options))
    return out


__all__ = [
    "CONNECT_COMMAND",
    "FEEDBACK_COMMAND",
    "BRIEF_COMMAND",
    "BRIEF_SKILL_NOTES",
    "BRIEF_SKILL_STATES",
    "COACHING_NOTES_ON",
    "SKILL_NOTES",
    "SKILL_STATES",
    "STATUSLINE_NOTES",
    "STATUS_COMMAND",
    "WARNING",
    "amount_text",
    "change_commands",
    "config_block",
    "describe",
    "hooks_block",
    "share_text",
    "view",
]

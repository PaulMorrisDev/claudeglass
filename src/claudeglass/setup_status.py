"""Is ClaudeGlass set up and working? One checklist for ``init``'s closing
summary, ``claudeglass status`` and the dashboard's Setup card.

:func:`check_setup` asks the underlying checks directly
(``hook_health``, ``installer``, ``config``, ``footprint``'s skill
state) rather than going through ``footprint.inventory``, which reports
what is *installed* and walks the whole data folder; this answers
whether each part *works*, cheaply enough for the dashboard to call on
every Overview load.

Each :class:`SetupItem` has one of four states:

- ``ok``: set up and working.
- ``waiting``: set up, but nothing has happened yet to prove it works
  (no Claude Code session since connecting, say). Not a fault.
- ``problem``: something to fix. Only an ``essential`` problem makes
  ``status`` exit 1 and keeps the dashboard's Setup card showing.
- ``off``: left off by choice.

No absolute path reaches the output: paths are written with the home
folder as ``~`` (``footprint.home_label``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import capture_view, hook_health, installer
from . import footprint
from .config import CaptureConfig, ConfigError, load_config, saved_billing

#: The word shown for each state, in place of a tick mark.
STATE_WORDS = {"ok": "Done", "waiting": "Waiting", "problem": "Needs attention", "off": "Off"}

INIT_COMMAND = "claudeglass init"
SERVICE_COMMAND = "claudeglass install-service"
CAPTURE_COMMAND = "claudeglass capture on"
REPAIR_COMMAND = "claudeglass init --repair-hook"


@dataclass(frozen=True, slots=True)
class SetupItem:
    key: str
    label: str
    #: ``ok``, ``waiting``, ``problem`` or ``off``.
    state: str
    #: One or two plain sentences.
    detail: str
    #: A command that fixes it, when there is one.
    fix: str | None = None
    #: Setup isn't done while this isn't ``ok`` (the dashboard's Setup
    #: card), and ``status`` exits 1 when it's a ``problem``.
    essential: bool = False

    @property
    def word(self) -> str:
        return STATE_WORDS[self.state]


def _home(text: str) -> str:
    """``text`` with every mention of the home folder written as ``~``."""
    home = str(Path.home())
    return re.sub(re.escape(home), lambda _m: "~", text, flags=re.IGNORECASE) if home else text


def _billing(config_dir: Path) -> SetupItem:
    label = "How you pay"
    try:
        saved = saved_billing(config_dir)
        config = load_config(config_dir)
    except ConfigError as exc:
        return SetupItem("billing", label, "problem", _home(str(exc)), essential=True)
    if saved is None:
        return SetupItem(
            "billing",
            label,
            "problem",
            "Not chosen yet, so amounts show in dollars even if you pay for a plan.",
            INIT_COMMAND,
            essential=True,
        )
    if saved == "subscription":
        return SetupItem("billing", label, "ok", "A plan (Pro, Max, Team or Enterprise): amounts show as a share of your usage limits.")
    if saved == "api":
        return SetupItem("billing", label, "ok", "An API key: amounts show in dollars.")
    if config.billing == "subscription":
        return SetupItem(
            "billing",
            label,
            "ok",
            "Worked out from your usage-limit readings: a plan, so amounts show as a share of your usage limits.",
        )
    return SetupItem(
        "billing",
        label,
        "waiting",
        "Set to be worked out from usage-limit readings, but none have been logged, so amounts show in dollars. "
        "The desktop app never runs the statusline that logs them: choose 1 or 2 with 'claudeglass init'.",
        INIT_COMMAND,
    )


def _snapshot_hook(config_dir: Path, claude_root, now: datetime | None) -> SetupItem:
    label = "Connected to Claude Code"
    health = hook_health.check(config_dir, now=now, claude_root=claude_root)
    if health.blocked_by is not None:
        return SetupItem("hook", label, "problem", hook_health.POLICY_TEXT[health.blocked_by], essential=True)
    if health.command is None:
        return SetupItem(
            "hook",
            label,
            "problem",
            "Not connected, so the settings you change aren't recorded and their effect can't be measured.",
            INIT_COMMAND,
            essential=True,
        )
    if not health.ok:
        return SetupItem(
            "hook",
            label,
            "problem",
            _home(health.summary()),
            REPAIR_COMMAND if health.fixed_command else INIT_COMMAND,
            essential=True,
        )
    if health.last_snapshot_days is None:
        return SetupItem(
            "hook",
            label,
            "waiting",
            "No Claude Code session has started since you connected.",
            essential=True,
        )
    days = int(health.last_snapshot_days)
    when = "today" if days == 0 else f"{days} day{'s' if days != 1 else ''} ago"
    return SetupItem("hook", label, "ok", f"Your settings were last recorded {when}.", essential=True)


def _statusline(config_dir: Path, claude_root, entrypoints: dict[str, dict]) -> SetupItem:
    label = "Usage-limit readings"
    working, sentence = hook_health.statusline_check(config_dir, entrypoints, claude_root=claude_root)
    if working:
        return SetupItem("statusline", label, "ok", sentence)
    _path, settings = footprint._settings(claude_root)
    state = "waiting" if footprint.is_own_statusline(settings) else "off"
    return SetupItem("statusline", label, state, sentence)


def _service(
    registered: bool | None, running: bool, url: str | None
) -> SetupItem:
    label = "Dashboard at logon"
    keeps = (
        "Claude Code deletes transcripts after 30 days (cleanupPeriodDays), and the dashboard keeps "
        "their history only while it runs."
    )
    at = f" at {url}" if url else ""
    if registered is True and running:
        return SetupItem("service", label, "ok", f"Starts when you log on, and is running{at}.", essential=True)
    if registered is True:
        return SetupItem(
            "service",
            label,
            "problem",
            f"Set to start when you log on, but nothing answers{at}. {keeps}",
            SERVICE_COMMAND,
            essential=True,
        )
    if registered is None:
        if running:
            return SetupItem("service", label, "ok", f"Running{at}.", essential=True)
        return SetupItem(
            "service", label, "off", f"Not running, and whether it starts at logon couldn't be checked. {keeps}", SERVICE_COMMAND
        )
    # Left off by choice, so never a problem, but the dashboard's Setup
    # card keeps saying so: history is lost while it doesn't run.
    if running:
        return SetupItem(
            "service",
            label,
            "off",
            f"Running{at}, but not set to start when you log on. {keeps}",
            SERVICE_COMMAND,
            essential=True,
        )
    return SetupItem("service", label, "off", f"Not set to start when you log on. {keeps}", SERVICE_COMMAND, essential=True)


def _capture(config_dir: Path, claude_root, capture: CaptureConfig, now: datetime | None) -> SetupItem:
    label = "Sharper tips (metrics capture)"
    if not capture.is_on:
        return SetupItem(
            "capture",
            label,
            "off",
            "Turned on, Claude tags each reply with the kind of work it was, so the tips fit how you work.",
            CAPTURE_COMMAND,
        )
    if capture.expired(now):
        return SetupItem(
            "capture",
            label,
            "off",
            f"Stopped by itself on {capture.until[:10]}, when its time-box ended.",
            CAPTURE_COMMAND,
        )
    described = capture_view.describe(capture)
    health = hook_health.check_capture(
        hook_health.capture_specs(capture.hook_metrics()), claude_root=claude_root, config_dir=config_dir
    )
    if health.ok:
        return SetupItem("capture", label, "ok", f"{described}.")
    # An older copy of this tool's own hook file only: the next update
    # or dashboard restart refreshes it.
    stale_only = (
        not health.missing
        and health.blocked_by is None
        and bool(health.outdated)
        and len(health.problems) == len(health.outdated)
    )
    if stale_only:
        return SetupItem(
            "capture",
            label,
            "waiting",
            f"{described}. Its hook files are an older copy; the next dashboard restart refreshes them.",
            capture_view.CONNECT_COMMAND,
        )
    return SetupItem(
        "capture",
        label,
        "problem",
        f"{described}. {_home(health.summary())}",
        None if health.blocked_by is not None else capture_view.CONNECT_COMMAND,
        essential=True,
    )


def _feedback_skill(claude_root, capture: CaptureConfig) -> SetupItem:
    label = "/cl-feedback skill"
    state = footprint.feedback_skill_state(claude_root)
    wanted = bool(capture.feedback) and capture.is_on
    if state == "installed":
        return SetupItem("skill", label, "ok", "Run /cl-feedback in Claude Code after a piece of work.")
    if state == "foreign":
        return SetupItem(
            "skill",
            label,
            "problem",
            f"{footprint.home_label(footprint.feedback_skill_path(claude_root))} is a skill this tool didn't write, "
            "so it's left alone.",
        )
    if state == "outdated":
        return SetupItem(
            "skill", label, "waiting", "An older copy: the next update refreshes it.", capture_view.FEEDBACK_COMMAND
        )
    if state == "renamed":
        return SetupItem(
            "skill", label, "waiting", "Still called /tl-feedback, its name before 0.12.0.", capture_view.FEEDBACK_COMMAND
        )
    if wanted:
        return SetupItem("skill", label, "problem", "Feedback is on, but the skill isn't installed.", capture_view.FEEDBACK_COMMAND)
    return SetupItem("skill", label, "off", "Not installed. It comes with sharper tips.", capture_view.FEEDBACK_COMMAND)


def check_setup(
    config_dir: str | Path,
    claude_root: str | Path | None = None,
    *,
    now: datetime | None = None,
    entrypoints: dict[str, dict] | None = None,
    is_registered: Callable[[], bool | None] | None = None,
    running: bool | None = None,
    health_check: Callable[[str], bool] | None = None,
    url: str | None = installer.DEFAULT_URL,
) -> list[SetupItem]:
    """Every part of the setup, in the order ``init`` sets it up.

    ``entrypoints`` is ``Store.entrypoint_counts()`` (empty when there is
    no store yet). ``is_registered`` defaults to
    :func:`installer.is_registered`; it returning ``None`` means
    "couldn't tell", never a problem. ``running`` says whether the
    dashboard answers: the dashboard itself passes ``True``; left
    ``None``, ``health_check`` (default :func:`installer.http_health_ok`)
    asks ``url``. The dashboard passes ``url=None``: the page is already
    open there, so the text names no address. Never raises for an
    unreadable file: that item reports it.
    """
    config_dir = Path(config_dir)
    capture = footprint.capture_setting(config_dir)
    registered = (is_registered or installer.is_registered)()
    if running is None:
        running = url is not None and (health_check or installer.http_health_ok)(url)
    return [
        _billing(config_dir),
        _snapshot_hook(config_dir, claude_root, now),
        _service(registered, running, url),
        _capture(config_dir, claude_root, capture, now),
        _feedback_skill(claude_root, capture),
        _statusline(config_dir, claude_root, entrypoints or {}),
    ]


def needs_attention(items: list[SetupItem]) -> list[SetupItem]:
    """The items that are a problem."""
    return [item for item in items if item.state == "problem"]


def essential_problem(items: list[SetupItem]) -> bool:
    """Whether an essential item is a problem (``status`` exits 1)."""
    return any(item.essential and item.state == "problem" for item in items)


def verdict(items: list[SetupItem]) -> str:
    """The closing line: all set up, or how many things need attention."""
    count = len(needs_attention(items))
    if not count:
        return "Everything's set up."
    return f"{count} thing{'s' if count != 1 else ''} need{'s' if count == 1 else ''} attention."


def lines(items: list[SetupItem]) -> list[str]:
    """The checklist as plain text lines, for ``status`` and ``init``."""
    width = max((len(item.label) for item in items), default=0)
    out: list[str] = []
    for item in items:
        out.append(f"  {item.label.ljust(width)}  {item.word}: {item.detail}")
        if item.fix and item.state != "ok":
            out.append(f"  {''.ljust(width)}  Fix: {item.fix}")
    return out


def done(items: list[SetupItem]) -> bool:
    """Whether every essential item is ``ok``: until then the dashboard
    shows its Setup card."""
    return all(item.state == "ok" for item in items if item.essential)


def to_jsonable(item: SetupItem) -> dict:
    return {
        "key": item.key,
        "label": item.label,
        "state": item.state,
        "word": item.word,
        "detail": item.detail,
        "fix": item.fix,
        "essential": item.essential,
    }


__all__ = [
    "STATE_WORDS",
    "SetupItem",
    "check_setup",
    "needs_attention",
    "essential_problem",
    "done",
    "verdict",
    "lines",
    "to_jsonable",
]

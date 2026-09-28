"""``init``: a few questions, one review of every change, then the
changes in order and a summary of what works.

The default path asks four things: how you pay, whether to connect to
Claude Code, whether to start the dashboard at logon and, optionally,
whether to turn on sharper tips (metrics capture at Essentials, plus the
``/tl-feedback`` skill). ``--advanced`` also asks every other question
:func:`onboarding.gather_answers` knows, and the full capture and
feedback questions. ``--non-interactive`` asks nothing: ``--answers`` and
the flags decide, and a ``(derived)`` line says what was assumed.

Nothing is written until the review is confirmed, and ``--dry-run``
stops at the review. After a yes the changes run in an order that never
leaves settings.json naming a file that isn't on disk yet, and the slow
baseline read comes last, so stopping it with Ctrl-C still leaves a
working setup. The closing summary is :func:`setup_status.check_setup`,
the same checklist ``claudeglass status`` prints.

:mod:`onboarding` never installs anything, and this module never imports
:mod:`cli`: the commands settings.json runs, the snapshot hook's
installer and the capture and feedback questions come in as
:class:`Tools`. :mod:`installer`'s functions are looked up when called,
so tests can replace them.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import IO, Callable

from . import capture_catalogue, capture_view, discovery, footprint, hook_health, installer, onboarding, pages, setup_status
from .config import CaptureConfig, ConfigError, check_config_values, feedback_ids, load_config, set_capture
from .fixes import RESTART_NOTE
from .parse import load_or_create_salt

#: How long ``init`` waits for a just-started dashboard to answer.
HEALTH_WAIT_S = 10.0
_HEALTH_STEP_S = 0.5

#: What each platform's logon task is called.
_SERVICE_WORDS = {"windows": "a Scheduled Task", "linux": "a systemd user service", "macos": "a LaunchAgent"}

#: The billing question's answers.
_BILLING_CHOICES = {"1": "subscription", "2": "api"}
_BILLING_DEFAULTS = {"subscription": "1", "api": "2", "auto": "auto"}
_BILLING_REVIEW = {
    "subscription": "a plan (Pro, Max, Team or Enterprise), so amounts show as a share of your usage limits",
    "api": "an API key, so amounts show in dollars",
    "auto": "worked out from your usage-limit readings",
}

_TIPS = "Sharper tips (metrics capture)"


@dataclass(frozen=True, slots=True)
class Options:
    """``init``'s flags."""

    config_dir: Path
    #: ``--projects-root`` and any extra roots: the first is Claude
    #: Code's own.
    projects_roots: list[Path]
    claude_root: Path
    answers_path: str | None = None
    non_interactive: bool = False
    advanced: bool = False
    no_install: bool = False
    connect: bool = False
    install_service: bool = False
    no_service: bool = False
    dry_run: bool = False
    repair_hook: bool = False
    #: ``--capture-level`` and ``--feedback``.
    capture_level: str | None = None
    feedback: str | None = None
    all_projects: bool = False
    project: list[str] | None = None
    project_family: str | None = None


@dataclass(frozen=True, slots=True)
class Tools:
    """What the flow needs from ``cli``."""

    #: The SessionStart command that runs the snapshot hook, or ``None``
    #: when its path can't be written into a command safely (ROB-P9).
    hook_command: str | None
    #: Copies the snapshot hook script into ``<config_dir>/hooks``.
    install_hook: Callable[[Path], Path]
    #: The statusline command added when settings.json has none.
    statusline_command: str | None
    #: A command per capture hook script (``None`` for an unsafe path).
    capture_commands: dict[str, str | None]
    #: The full capture question (``cli._init_capture_choice``, bound to
    #: this run's flags): ``(stdin, stdout, now) -> (level, until)``, or
    #: ``None`` to leave capture as it is.
    capture_choice: Callable
    #: The full feedback question (``cli._init_feedback_choice``):
    #: ``(stdin, stdout) -> (on, was_on, notes)`` or ``None``.
    feedback_choice: Callable
    find_wsl_roots: Callable[[], list[Path]] | None = None
    python: str = field(default_factory=lambda: sys.executable)


@dataclass(slots=True)
class _Setup:
    """Everything ``init`` decided, before anything is written."""

    answers: onboarding.Answers
    updates: dict
    #: Review lines, one per part of the setup.
    review: list[str] = field(default_factory=list)
    #: The combined settings.json change, or ``None`` when settings.json
    #: isn't touched.
    settings: hook_health.ConnectPlan | None = None
    #: Whether to copy the hook scripts and change settings.json.
    touch_settings: bool = False
    capture_specs: tuple = ()
    #: ``(level, until)`` to save, or ``None`` to leave the level.
    capture: tuple[str, str | None] | None = None
    #: The survey on or off, or ``None`` to leave it.
    feedback: bool | None = None
    #: ``"write"``, ``"remove"`` or ``None``.
    skill: str | None = None
    service: installer.InstallPlan | None = None
    registered: bool | None = None
    running: bool = False


def _yes_no(prompt: str, default: bool, *, stdin: IO[str], stdout: IO[str]) -> bool:
    """Ask until the answer is yes, no or blank (the default). The end of
    input counts as the default."""
    hint = "[Y/n]" if default else "[y/N]"
    while True:
        stdout.write(f"{prompt} {hint}: ")
        stdout.flush()
        raw = stdin.readline()
        if not raw:
            stdout.write("\n")
            return default
        word = raw.strip().lower()
        if not word:
            return default
        if word in ("y", "yes"):
            return True
        if word in ("n", "no"):
            return False
        stdout.write("Type y or n.\n")


def _header(detection: onboarding.Detection, stdout: IO[str]) -> None:
    stdout.write("\nClaudeGlass setup\n")
    stdout.write("Everything stays on this computer: ClaudeGlass reads Claude Code's own files and sends nothing anywhere.\n")
    count = detection.project_count
    where = " and ".join(dict.fromkeys(discovery.source_label(root) for root in detection.wsl_roots))
    if count:
        more = f", and more in {where}" if where else ""
        stdout.write(f"Found {count} project{'s' if count != 1 else ''} with Claude Code history{more}.\n")
    elif where:
        stdout.write(f"Found Claude Code history in {where}.\n")
    else:
        stdout.write("No Claude Code history yet: ClaudeGlass starts measuring from your next session.\n")


def _ask_billing(
    detection: onboarding.Detection, answers_data: dict | None, *, non_interactive: bool, stdin, stdout, notes: list[str]
) -> str:
    """Question 1: a plan or an API key. The saved answer is the default,
    and a saved ``auto`` stays ``auto``; with nothing saved, a blank
    answer asks again."""
    saved = detection.saved_billing
    if answers_data is not None and "billing" in answers_data:
        raw = str(answers_data["billing"]).strip()
        # Anything else is left as typed, for the config check to reject.
        return _BILLING_CHOICES.get(raw) or onboarding.BILLING_WORDS.get(raw.lower(), raw)
    if non_interactive:
        value = saved or "auto"
        notes.append(
            f"billing: not given in --answers; used {value!r}"
            + ("" if saved else ", worked out from usage-limit readings")
        )
        return value
    stdout.write(
        "\nHow do you pay for Claude Code?\n"
        "  1  A plan (Pro, Max, Team or Enterprise): amounts show as a share of your usage limits.\n"
        "  2  An API key: amounts show in dollars.\n"
    )
    default = _BILLING_DEFAULTS.get(saved or "")
    while True:
        stdout.write(f"Choose 1 or 2{f' [{default}]' if default else ''}: ")
        stdout.flush()
        raw = stdin.readline()
        if not raw:
            stdout.write("\n")
            return saved or "auto"
        word = raw.strip().lower()
        if not word:
            if saved:
                return saved
            continue
        if word in _BILLING_CHOICES:
            return _BILLING_CHOICES[word]
        if word in onboarding.BILLING_WORDS:
            return onboarding.BILLING_WORDS[word]
        stdout.write("Type 1 for a plan or 2 for an API key.\n")


def _connect(opts: Options, tools: Tools, health, *, stdin, stdout, notes: list[str]) -> tuple[str, str]:
    """Question 2: connect to Claude Code. Returns the outcome
    (``yes``, ``already``, ``no``, or ``blocked`` when it can't be done)
    and its review line."""
    label = "Connect to Claude Code"
    if opts.no_install:
        return "no", f"{label}: skipped (--no-install)."
    if health.command is not None:
        return "already", f"{label}: already connected."
    base = hook_health.plan_connect(
        opts.config_dir,
        hook_command=tools.hook_command or "",
        statusline_command=tools.statusline_command,
        claude_root=opts.claude_root,
    )
    if base.new_text is None and base.changes:
        return "blocked", f"{label}: {' '.join(base.changes)}"
    if tools.hook_command is None:
        # ROB-P9: the Python's or the script's own path can't be safely
        # written into a command string -- refuse rather than write a
        # broken or unsafe one into settings.json.
        return "blocked", (
            f"{label}: not possible, because this Python's path or ClaudeGlass's folder holds a quote, $, backtick, "
            "or is a UNC path. Move the folder somewhere with a plain path, then run 'claudeglass init' again."
        )
    if opts.connect:
        return "yes", ""
    if opts.non_interactive:
        notes.append("connect: not given on the command line; left unconnected (pass --connect to connect)")
        return "no", f"{label}: not now. Run 'claudeglass init --connect' to connect."
    stdout.write(
        "\nConnect to Claude Code\n"
        "ClaudeGlass adds a hook to Claude Code's settings.json. When a session starts, it records which settings "
        "the session ran with (names, a few safe values and file sizes, never contents), so ClaudeGlass can measure "
        "what your changes did. It runs in the background and adds no tokens.\n"
    )
    if _yes_no("Connect?", True, stdin=stdin, stdout=stdout):
        return "yes", ""
    return "no", f"{label}: not now. Run 'claudeglass init --connect' to connect later."


def _service(opts: Options, tools: Tools, setup: _Setup, *, stdin, stdout, notes: list[str]) -> str:
    """Question 3: start the dashboard at logon, or repair a logon task
    whose dashboard doesn't answer. Sets ``setup.service``; returns the
    review line."""
    label = "Dashboard at logon"
    if opts.no_service:
        return f"{label}: skipped (--no-service)."
    registered, running = setup.registered, setup.running
    if running and registered is not False:
        return f"{label}: already running at {installer.DEFAULT_URL}."
    try:
        plan = installer.plan_service_install(tools.python, opts.projects_roots, opts.config_dir)
    except installer.InstallerError as exc:
        return f"{label}: can't be set up here ({exc})."
    what = _SERVICE_WORDS.get(plan.platform, "a logon task")
    repair = registered is True
    if opts.install_service:
        want = True
    elif opts.non_interactive:
        notes.append(
            "run_service: not given on the command line; used default False "
            "(pass --install-service to install non-interactively)"
        )
        want = False
    elif repair:
        stdout.write(f"\nThe dashboard's logon task is set up, but nothing answers at {installer.DEFAULT_URL}.\n")
        want = _yes_no("Repair the dashboard's logon task?", True, stdin=stdin, stdout=stdout)
    else:
        stdout.write(
            "\nStart the dashboard when you log on\n"
            "Claude Code deletes transcripts after 30 days, and the dashboard keeps their history only while it "
            f"runs. This adds {what} that starts it when you log on, and starts it now.\n"
        )
        want = _yes_no("Start it at logon?", True, stdin=stdin, stdout=stdout)
    if not want:
        return f"{label}: not now. Run 'claudeglass install-service' any time to add it."
    setup.service = plan
    if repair:
        return f"{label}: set up {what} again, and start the dashboard now."
    return f"{label}: add {what} that starts the dashboard when you log on, and start it now."


def _tips_question(current: CaptureConfig, *, now: datetime, stdin, stdout) -> tuple[tuple[str, str | None] | None, bool | None]:
    """Question 4, sharper tips: Essentials for the default time-box, plus
    the ``/tl-feedback`` skill. Returns ``(capture, feedback)`` changes."""
    essentials_on = current.level == "essentials" and not current.expired(now)
    cost = capture_catalogue.rough_tokens(capture_catalogue.level_includes("essentials"))
    days = capture_catalogue.DEFAULT_CAPTURE_TIMEBOX_DAYS
    stdout.write(
        "\nSharper tips (optional)\n"
        "Claude ends each reply with a short tag saying what kind of work it was, such as "
        "[tl: task=bugfix brief=clear], so the tips fit how you work. That costs about "
        f"{cost['session_note']} tokens when a session starts and {cost['reply_tag']} per reply, plus a Claude "
        f"Haiku call of about ${capture_catalogue.JUDGE_USD_PER_CALL:.3f} after each subagent run, and it switches "
        f"itself off after {days} days. It also adds the /tl-feedback skill, for rating a piece of work when it's "
        "done.\n"
    )
    yes = _yes_no("Turn on sharper tips?", essentials_on, stdin=stdin, stdout=stdout)
    feedback_on = "feedback_skill" in current.feedback
    if not yes:
        return (("off", None) if essentials_on else None), None
    until = (now + timedelta(days=days)).isoformat(timespec="seconds")
    return (None if essentials_on else ("essentials", until)), (None if feedback_on else True)


def _tips(opts: Options, tools: Tools, setup: _Setup, current: CaptureConfig, connected: bool, *, stdin, stdout, now) -> bool:
    """Question 4, or the full capture and feedback questions under
    ``--advanced``, ``--non-interactive`` or a capture level given by
    flag or answers file. Sets ``setup.capture`` and ``setup.feedback``;
    returns whether question 4 was left out for want of a connection."""
    given = onboarding.capture_answer(opts.answers_path, opts.capture_level)
    given_feedback = onboarding.feedback_answer(opts.answers_path, opts.feedback)
    if opts.advanced or opts.non_interactive or given is not None:
        setup.capture = tools.capture_choice(stdin, stdout, now)
        if setup.capture is not None:
            after = set_capture(opts.config_dir, level=setup.capture[0], until=setup.capture[1], now=now, dry_run=True)
            if given_feedback is None and "feedback_skill" in after.feedback and "feedback_skill" not in current.feedback:
                # Deep turns the survey on (config.set_capture).
                return False
        _feedback(tools, setup, stdin=stdin, stdout=stdout)
        return False
    other_level = current.is_on and not current.expired(now) and current.level != "essentials"
    if connected and not other_level:
        setup.capture, setup.feedback = _tips_question(current, now=now, stdin=stdin, stdout=stdout)
    if given_feedback is not None:
        # --feedback (or the answers file) wins over the tips answer.
        setup.feedback = None
        _feedback(tools, setup, stdin=stdin, stdout=stdout)
    return not connected


def _feedback(tools: Tools, setup: _Setup, *, stdin, stdout) -> None:
    choice = tools.feedback_choice(stdin, stdout)
    if choice is not None and choice[0] != choice[1]:
        setup.feedback = choice[0]


def _tips_line(before: CaptureConfig, after: CaptureConfig, setup: _Setup, unconnected: bool, now: datetime) -> str:
    if setup.capture is None and setup.feedback is None:
        if before.is_on and not before.expired(now):
            return f"{_TIPS}: {capture_view.describe(before)}, unchanged."
        if unconnected:
            return "Sharper tips need the Claude Code connection, so they stay off."
        return f"{_TIPS}: off."
    parts = []
    if setup.capture is not None and setup.capture[0] == "off":
        parts.append("turn off" + (", and take their hooks out of settings.json" if setup.touch_settings else ""))
    elif setup.capture is not None:
        title = capture_catalogue.LEVEL_TITLES.get(after.level, after.level)
        parts.append(f"turn on {title} " + (f"until {after.until[:10]}" if after.until else "with no end date"))
    if setup.feedback is True:
        parts.append("add the /tl-feedback skill")
    elif setup.feedback is False:
        parts.append("turn off the /tl-feedback survey")
    return f"{_TIPS}: {', and '.join(parts)}."


def _decide(opts: Options, tools: Tools, detection, health, *, stdin, stdout, now: datetime) -> _Setup | None:
    """Ask (or derive) everything and work out each change, writing
    nothing. ``None`` when an answer can't be saved (said on
    ``stdout``)."""
    notes: list[str] = []
    billing_notes: list[str] = []
    answers_data = onboarding.load_answers_file(opts.answers_path) if opts.answers_path is not None else None
    billing = _ask_billing(
        detection, answers_data, non_interactive=opts.non_interactive, stdin=stdin, stdout=stdout, notes=billing_notes
    )
    asked = frozenset(onboarding.QUESTION_KEYS) - {"billing"} if opts.advanced else frozenset()
    if asked and not opts.non_interactive:
        stdout.write("\nMore settings (--advanced)\n")
    answers = onboarding.gather_answers(
        detection=detection,
        answers_path=opts.answers_path,
        non_interactive=opts.non_interactive,
        stdin=stdin,
        stdout=stdout,
        questions=asked,
    )
    answers.billing = billing
    updates = onboarding.config_updates(answers, detection, now)
    try:
        check_config_values(opts.config_dir, updates)
    except ConfigError as exc:
        stdout.write(f"claudeglass init: {exc}\n")
        return None
    setup = _Setup(answers=answers, updates=updates)
    setup.review.append(
        "Save your answers in config.toml, in ClaudeGlass's own folder. How you pay: " + _BILLING_REVIEW.get(billing, billing) + "."
    )

    connect, connect_line = _connect(opts, tools, health, stdin=stdin, stdout=stdout, notes=notes)
    may_touch = not opts.no_install and (opts.connect or not opts.non_interactive)
    setup.touch_settings = connect == "yes" or (connect == "already" and may_touch)
    connected = connect in ("yes", "already")

    setup.registered = installer.is_registered()
    setup.running = installer.http_health_ok(installer.DEFAULT_URL)
    service_line = _service(opts, tools, setup, stdin=stdin, stdout=stdout, notes=notes)
    # Now, before the capture and feedback questions print their own, so
    # the notes come in the order the questions do.
    for note in billing_notes + answers.notes + notes:
        stdout.write(f"(derived) {note}\n")

    current = footprint.capture_setting(opts.config_dir)
    unconnected = _tips(opts, tools, setup, current, connected, stdin=stdin, stdout=stdout, now=now)
    after = _capture_after(opts, setup, current, now)

    settings_lines: list[str] = []
    capture_line = None
    if setup.touch_settings:
        commands = None
        if current.is_on or after.is_on or current.coaching_notes_on:
            setup.capture_specs = (
                hook_health.capture_specs(after.hook_metrics()) if after.is_on or after.coaching_notes_on else ()
            )
            commands = tools.capture_commands
            if any(commands.get(spec.script) is None for spec in setup.capture_specs):
                commands = None
                setup.capture_specs = ()
                capture_line = (
                    f"{_TIPS}: its hooks can't be added, because this Python's path or ClaudeGlass's folder holds a "
                    "quote, $, backtick, or is a UNC path. Move the folder somewhere with a plain path, then run "
                    "'claudeglass capture connect'."
                )
        setup.settings = hook_health.plan_connect(
            opts.config_dir,
            hook_command=tools.hook_command or "",
            statusline_command=tools.statusline_command,
            claude_root=opts.claude_root,
            capture_specs_wanted=setup.capture_specs,
            capture_commands=commands,
        )
        if setup.settings.new_text is None and setup.settings.changes:
            settings_lines.append(f"Claude Code's settings.json: {' '.join(setup.settings.changes)}")
            setup.settings = None
        elif setup.settings.new_text is not None:
            where = footprint.home_label(setup.settings.settings_path)
            settings_lines.append(f"Change Claude Code's settings.json ({where}), backed up first:")
            settings_lines += [f"  - {change}" for change in _grouped(setup.settings.changes)]
            policy = hook_health.hook_policy(opts.claude_root)
            if policy is not None:
                settings_lines.append(
                    f"  {hook_health.POLICY_TEXT[policy]} The entries can still be written, but Claude Code won't "
                    "run them while that holds."
                )
    elif setup.capture is not None and after.is_on:
        capture_line = f"{_TIPS}: add the hooks it needs later with 'claudeglass capture connect'."

    skill_line = _skill(opts, setup, current, after, may_touch)

    if connect_line:
        setup.review.append(connect_line)
    setup.review += settings_lines
    setup.review.append(_tips_line(current, after, setup, unconnected, now))
    if capture_line:
        setup.review.append(capture_line)
    if skill_line:
        setup.review.append(skill_line)
    setup.review.append(service_line)
    return setup


#: How :func:`hook_health.plan_connect` words each capture entry it
#: changes, and the one line the review shows for all of them.
_CAPTURE_CHANGES = {
    "Add the capture hook that runs ": "Add {n} capture hook entries: Claude Code runs capture-hook.py when a session "
    "starts, a turn ends and so on, and it adds the short notes and tags sharper tips need.",
    "Update the capture hook that runs ": "Update {n} capture hook entries to the ones this version writes.",
    "Remove the capture hook that runs ": "Remove {n} capture hook entries.",
}


def _grouped(changes: list[str]) -> list[str]:
    """``changes`` with the capture entries counted, not listed one by one
    (``d`` shows them all)."""
    out: list[str] = []
    counts = dict.fromkeys(_CAPTURE_CHANGES, 0)
    for change in changes:
        prefix = next((p for p in _CAPTURE_CHANGES if change.startswith(p)), None)
        if prefix is None:
            out.append(change)
        else:
            counts[prefix] += 1
    for prefix, count in counts.items():
        if count == 1:
            out.append(next(c for c in changes if c.startswith(prefix)))
        elif count:
            out.append(_CAPTURE_CHANGES[prefix].format(n=count))
    return out


def _capture_after(opts: Options, setup: _Setup, current: CaptureConfig, now: datetime) -> CaptureConfig:
    """``[capture]`` as it will be once saved, the way :func:`_apply`
    saves it: the level first (Deep turns the survey on), then the
    survey."""
    change: dict = {}
    if setup.capture is not None:
        change = {"level": setup.capture[0], "until": setup.capture[1]}
    after = set_capture(opts.config_dir, now=now, dry_run=True, **change) if change else current
    if setup.feedback is not None:
        after = set_capture(
            opts.config_dir, now=now, dry_run=True, feedback=feedback_ids(after.feedback, setup.feedback), **change
        )
    return after


def _skill(opts: Options, setup: _Setup, before: CaptureConfig, after: CaptureConfig, may_touch: bool) -> str | None:
    """Whether to write or remove the ``/tl-feedback`` skill (sets
    ``setup.skill``); returns a review line when it isn't done."""
    want = "feedback_skill" in after.feedback
    state = footprint.feedback_skill_state(opts.claude_root)
    if want and state in ("missing", "outdated"):
        if not may_touch:
            return "The /tl-feedback skill: add it with 'claudeglass capture feedback on'."
        setup.skill = "write"
        if setup.feedback is not True:
            return "The /tl-feedback skill: " + ("update it." if state == "outdated" else "add it.")
    elif want and state == "foreign":
        where = footprint.home_label(footprint.feedback_skill_path(opts.claude_root))
        return f"The /tl-feedback skill: {where} is a skill this tool didn't write, so it's left alone."
    elif not want and "feedback_skill" in before.feedback and state in ("installed", "outdated"):
        if not may_touch:
            return "The /tl-feedback skill: remove it with 'claudeglass capture feedback off'."
        setup.skill = "remove"
        return "The /tl-feedback skill: remove it."
    return None


def _baseline_scope(opts: Options) -> str:
    return "this project's" if not (opts.all_projects or opts.project or opts.project_family) else "the chosen projects'"


def _baseline_dirs(opts: Options, config) -> list[Path]:
    return onboarding.baseline_project_dirs(
        config,
        projects_root_path=opts.projects_roots[0],
        extra_projects_roots=opts.projects_roots[1:],
        all_projects=opts.all_projects,
        project=opts.project,
        project_family=opts.project_family,
    )


def _review(setup: _Setup, opts: Options, detection: onboarding.Detection, stdout: IO[str]) -> None:
    stdout.write("\nReady to set up:\n")
    for line in setup.review:
        stdout.write(("    " + line[2:] if line.startswith("  ") else f"  {line}") + "\n")
    if _baseline_dirs(opts, detection.existing_config):
        stdout.write(f"  Then read {_baseline_scope(opts)} history for a first baseline.\n")


def _details(setup: _Setup, opts: Options, stdout: IO[str]) -> None:
    """The exact changes: the settings.json diff, the skill's file and the
    logon task's commands."""
    shown = False
    if setup.settings is not None and setup.settings.diff:
        stdout.write("\n" + setup.settings.diff.rstrip("\n") + "\n")
        shown = True
    if setup.skill is not None:
        where = footprint.home_label(footprint.feedback_skill_path(opts.claude_root))
        stdout.write(f"\nThe /tl-feedback skill: {'writes' if setup.skill == 'write' else 'removes'} {where}\n")
        shown = True
    if setup.service is not None:
        plan = setup.service
        stdout.write("\n")
        for line in installer.plan_lines("install-service", plan, commands=plan.commands, files=list(plan.files_to_write)):
            stdout.write(line + "\n")
        shown = True
    if not shown:
        stdout.write("\nNothing outside config.toml changes.\n")


def _confirm(setup: _Setup, opts: Options, *, stdin, stdout) -> bool:
    """One yes for everything; ``d`` shows the exact changes first."""
    if opts.non_interactive:
        return True
    while True:
        stdout.write("Go ahead? (d shows the exact changes) [Y/n/d]: ")
        stdout.flush()
        raw = stdin.readline()
        if not raw:
            stdout.write("\n")
            return True
        word = raw.strip().lower()
        if word in ("", "y", "yes"):
            return True
        if word in ("n", "no"):
            return False
        if word in ("d", "diff", "details"):
            _details(setup, opts, stdout)
            stdout.write("\n")
            continue
        stdout.write("Type y, n or d.\n")


def _wait_for_dashboard(*, sleep: Callable[[float], None], clock: Callable[[], float]) -> bool:
    """Whether the dashboard answers within :data:`HEALTH_WAIT_S`."""
    deadline = clock() + HEALTH_WAIT_S
    while True:
        if installer.http_health_ok(installer.DEFAULT_URL):
            return True
        if clock() >= deadline:
            return False
        sleep(_HEALTH_STEP_S)


def _apply(setup: _Setup, opts: Options, tools: Tools, *, stdout, now, sleep, clock) -> tuple[int, bool]:
    """Make every change, quick ones first. Returns ``(exit code, whether
    settings.json changed)``."""
    config_dir = opts.config_dir
    rc = onboarding.save_config(
        config_dir, setup.updates, answers=setup.answers if opts.advanced else None, stdout=stdout
    )
    if rc != 0:
        return rc, False

    try:
        if setup.capture is not None:
            capture = set_capture(config_dir, level=setup.capture[0], until=setup.capture[1], now=now)
            stdout.write(
                "Metrics capture switched off.\n"
                if setup.capture[0] == "off"
                else f"Saved to config.toml: metrics capture {capture_view.describe(capture)}.\n"
            )
        if setup.feedback is not None:
            current = load_config(config_dir).capture.feedback
            set_capture(config_dir, feedback=feedback_ids(current, setup.feedback), now=now)
            stdout.write("Saved to config.toml: feedback " + ("on" if setup.feedback else "off") + ".\n")
    except ConfigError as exc:
        stdout.write(f"{exc}\n")
        rc = 2

    changed = False
    if setup.touch_settings:
        if setup.capture_specs:
            for script in hook_health.CAPTURE_SCRIPTS:
                hook_health.install_hook_files(config_dir, hook_health.CAPTURE_FILES[script])
            load_or_create_salt(config_dir)
        tools.install_hook(Path(config_dir).resolve())
        if setup.settings is not None and setup.settings.new_text is not None:
            try:
                backup = hook_health.connect(setup.settings, now=now)
            except (OSError, ValueError) as exc:
                stdout.write(f"Could not change settings.json: {exc}\n")
                rc = 2
            else:
                changed = True
                stdout.write(
                    "Changed Claude Code's settings.json."
                    + (f" The previous copy is at {footprint.home_label(backup)}" if backup else "")
                    + "\n"
                )

    if setup.skill == "write":
        footprint.write_feedback_skill(opts.claude_root)
        stdout.write("Added the /tl-feedback skill.\n")
    elif setup.skill == "remove":
        footprint.remove_feedback_skill(opts.claude_root)
        stdout.write("Removed the /tl-feedback skill.\n")

    if setup.service is not None:
        stdout.write("Starting the dashboard... ")
        stdout.flush()
        try:
            installer.install(setup.service, quiet=True)
        except installer.InstallerError as exc:
            stdout.write(f"it couldn't be set up:\n{exc}\n")
            rc = 2
        else:
            setup.running = _wait_for_dashboard(sleep=sleep, clock=clock)
            setup.registered = installer.is_registered()
            stdout.write(
                "running.\n" if setup.running else "not answering yet: it can take a few seconds to start.\n"
            )

    _baseline(opts, stdout=stdout, now=now)
    return rc, changed


def _baseline(opts: Options, *, stdout: IO[str], now: datetime) -> None:
    """The first baseline, last because it's the slow part. Skipped
    silently when there's no history to read; Ctrl-C stops it and keeps
    everything else."""
    project_dirs = _baseline_dirs(opts, load_config(opts.config_dir))
    if not project_dirs:
        return
    stdout.write(f"Reading {_baseline_scope(opts)} history for a first baseline... ")
    stdout.flush()
    try:
        saved = onboarding.run_baseline(opts.config_dir, project_dirs, now=now, stdout=stdout)
    except KeyboardInterrupt:
        stdout.write("stopped. Run 'claudeglass baseline' to take it later.\n")
        return
    if saved is not None:
        count = saved[0]["sessions_analysed"]
        stdout.write(f"{count} session{'s' if count != 1 else ''}.\n")


def _summary(opts: Options, setup: _Setup, *, changed: bool, stdout: IO[str], now: datetime) -> None:
    from .service.serve import STORE_FILENAME
    from .service.store import read_entrypoint_counts

    items = setup_status.check_setup(
        opts.config_dir,
        opts.claude_root,
        now=now,
        entrypoints=read_entrypoint_counts(Path(opts.config_dir) / STORE_FILENAME),
        is_registered=lambda: setup.registered,
        running=setup.running,
    )
    stdout.write("\nYour setup\n")
    for line in setup_status.lines(items):
        stdout.write(pages.plain(line) + "\n")
    stdout.write(f"\n{setup_status.verdict(items)}\n\n")
    if setup.running:
        stdout.write(f"Next: open {installer.DEFAULT_URL} to see where your tokens go.\n")
    else:
        stdout.write(
            f"Next: run 'claudeglass serve', then open {installer.DEFAULT_URL} to see where your tokens go.\n"
        )
    if footprint.feedback_skill_state(opts.claude_root) == "installed":
        stdout.write("After a piece of work, run /tl-feedback in Claude Code.\n")
    if changed:
        stdout.write(f"{RESTART_NOTE}\n")
    stdout.write("Check your setup any time: claudeglass status\n")


def run(
    opts: Options,
    tools: Tools,
    *,
    stdin: IO[str],
    stdout: IO[str],
    now: datetime | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Run ``init``. Returns the exit code: 0, or 2 for an answer that
    can't be saved (a malformed ``--answers`` file, a bad value) or a
    change that failed."""
    now = now or datetime.now(timezone.utc)
    stdout.write("Looking for Claude Code history...\n")
    stdout.flush()
    detection = onboarding.detect(
        opts.config_dir,
        opts.projects_roots[0],
        extra_projects_roots=opts.projects_roots[1:],
        find_wsl_roots=tools.find_wsl_roots,
    )
    health = hook_health.check(opts.config_dir, now=now, claude_root=opts.claude_root)
    _header(detection, stdout)
    onboarding.offer_hook_repair(
        health,
        repair_hook=opts.repair_hook and not opts.dry_run,
        non_interactive=opts.non_interactive or opts.dry_run,
        stdin=stdin,
        stdout=stdout,
        now=now,
    )

    try:
        setup = _decide(opts, tools, detection, health, stdin=stdin, stdout=stdout, now=now)
    except (onboarding.OnboardingError, ConfigError) as exc:
        stdout.write(f"claudeglass init: {exc}\n")
        return 2
    if setup is None:
        return 2

    _review(setup, opts, detection, stdout)
    if opts.dry_run:
        _details(setup, opts, stdout)
        stdout.write("\nDry run: nothing was written. Run 'claudeglass init' without --dry-run to set up.\n")
        return 0
    if not _confirm(setup, opts, stdin=stdin, stdout=stdout):
        stdout.write("Nothing was changed. Run 'claudeglass init' again when you're ready.\n")
        return 0

    stdout.write("\n")
    rc, changed = _apply(setup, opts, tools, stdout=stdout, now=now, sleep=sleep, clock=clock)
    _summary(opts, setup, changed=changed, stdout=stdout, now=now)
    return rc


__all__ = ["Options", "Tools", "run", "HEALTH_WAIT_S"]

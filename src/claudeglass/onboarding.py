"""The questions ``claudeglass init`` asks, and the parts of it
that write ``config.toml`` and the first baseline. :mod:`setup_flow`
runs them in order, with one review before anything is written.

:func:`detect` looks at what's already on the machine; :func:`gather_answers`
asks (or derives, under ``--non-interactive``) the settings this codebase
can't infer on its own, all of them under ``init --advanced`` and none
by default; :func:`save_config` and :func:`run_baseline` write the
result. Nothing here installs anything or dynamic-imports the packaged
``hooks/snapshot-config.py``: that stays ``cli.py``'s job, so this
module stays importable and unit-testable without packaged-resource
plumbing.

The only ``settings.json`` write here is repairing a broken hook
command, after showing it and a yes (or ``--repair-hook``), with a
backup first (:func:`offer_hook_repair`). Connecting the hook and
statusline is :mod:`setup_flow`'s, after its review; profiles are
written only by ``apply``.

The full capture question, :func:`ask_capture_level`, warns that capture
uses tokens and shows what each level would have cost. When a level goes
on, :func:`ask_capture_until` follows with a default time-box
(:data:`DEFAULT_CAPTURE_TIMEBOX_DAYS` days) so capture doesn't run on
forever unnoticed, or "no limit" if asked for. Then :func:`ask_feedback`
offers the ``/cl-feedback`` skill and its status-line reminder.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import IO

from . import baseline as baseline_mod
from . import capture_catalogue, discovery, footprint, hook_health, pages, snapshots
from .fixes import RESTART_NOTE
from .config import (
    Config,
    ConfigError,
    ProjectConfig,
    load_config,
    save_project_config,
    saved_billing,
    write_config_values,
)
from .pricing import PricingError, load_pricing

__all__ = [
    "OnboardingError",
    "Detection",
    "detect",
    "Answers",
    "load_answers_file",
    "gather_answers",
    "capture_answer",
    "ask_capture_level",
    "capture_no_limit_answer",
    "ask_capture_until",
    "ask_feedback",
    "feedback_answer",
    "QUESTION_KEYS",
    "BILLING_WORDS",
    "offer_hook_repair",
    "config_updates",
    "save_config",
    "baseline_project_dirs",
    "run_baseline",
]

#: Default onboarding capture window length in days, when neither the
#: answers file nor an existing config.toml names one.
DEFAULT_CAPTURE_WINDOW_DAYS = 7

#: Default length of the metrics-capture time-box :func:`ask_capture_until`
#: offers when a level is turned on: capture switches itself off this many
#: days after ``now`` unless the user says otherwise, so it can't run
#: forever unnoticed. The same length ``capture on --for 14d`` would give.
#: Defined in :mod:`capture_catalogue` (CAP-8: :func:`~claudeglass.
#: config.set_capture` needs it too, and can't import this module, which
#: already imports :mod:`~claudeglass.config`) and re-exported here
#: under its established name.
DEFAULT_CAPTURE_TIMEBOX_DAYS = capture_catalogue.DEFAULT_CAPTURE_TIMEBOX_DAYS

_TRUE_STRINGS = frozenset({"y", "yes", "true", "1", "on"})

#: The keys :func:`gather_answers` can ask, in the order it asks them.
QUESTION_KEYS = (
    "billing",
    "exclude_projects",
    "launch_overlays",
    "shared_project_config",
    "tz",
    "apply_scope",
    "capture_window",
    "extra_projects_roots",
)


def _relative_label(path: Path, base: Path) -> str:
    """``path`` rendered relative to ``base`` when possible, matching
    ``profiles.apply._relative_label``'s convention: ``base`` (here,
    always ``config_dir``, a path the caller explicitly supplied via
    ``--config-dir`` or its documented default) is not itself a privacy
    leak, so printing paths *relative to it* keeps the "Wrote ..."
    confirmation messages useful without ever putting a raw absolute
    filesystem path (home directory, username, drive letter) on stdout.
    Falls back to ``path``'s own name when it isn't under ``base``.
    """
    try:
        return str(path.relative_to(base))
    except ValueError:
        return path.name


class OnboardingError(Exception):
    """An ``--answers`` file could not be read or is not valid JSON.
    Same "stand-alone, user-facing" convention as
    :class:`~claudeglass.config.ConfigError`.
    """


@dataclass(slots=True)
class Detection:
    """What ``init`` found already on the machine before asking
    anything -- printed verbatim so the user can see what's being
    derived from, and never itself containing a raw filesystem path
    (only a redacted slug -- see ``discovery.redact_slug``) or session
    content, per this project's privacy invariant.
    """

    config_dir: Path
    config_dir_exists: bool
    existing_config: Config
    snapshot_count: int
    usage_log_present: bool
    #: The current directory's project slug, already redacted.
    project_slug: str
    #: How many project directories are discoverable under
    #: ``projects_root`` at all (not filtered to the current one).
    project_count: int
    #: Claude Code project folders found inside WSL distros that
    #: ``config.toml`` doesn't list yet (``init`` offers to add them).
    wsl_roots: list[Path] = field(default_factory=list)
    #: ``billing`` as written in ``config.toml`` (``auto`` stays
    #: ``auto``), or ``None`` when it was never answered.
    saved_billing: str | None = None


def _root_key(path: str | Path) -> str:
    return os.path.normcase(str(path)).rstrip("\\/")


def detect(
    config_dir: str | Path,
    projects_root_path: str | Path,
    *,
    extra_projects_roots: list[Path] | None = None,
    find_wsl_roots=None,
) -> Detection:
    """Gather :class:`Detection` for ``config_dir``/``projects_root_path``.
    Tolerant throughout: a missing config dir, no snapshots, and no
    discoverable projects are all just facts to report, not errors.

    ``find_wsl_roots`` (``discovery.find_wsl_projects_roots`` from the
    CLI; ``None`` skips the search) lists WSL project folders to offer.
    """
    config_dir = Path(config_dir)
    projects_root_path = Path(projects_root_path)

    try:
        existing_config = load_config(config_dir)
    except ConfigError:
        # A malformed config.toml is surfaced later, when init tries to
        # write it back (write_config_values re-validates) -- detection
        # itself never fails, it just reports the defaults for now.
        existing_config = Config()

    try:
        snapshot_count = len(snapshots.load_snapshots(config_dir))
    except Exception:
        snapshot_count = 0

    try:
        saved = saved_billing(config_dir)
    except ConfigError:
        saved = None

    usage_log_present = (config_dir / "usage-log.csv").exists()
    project_slug = discovery.redact_slug(discovery.slug_for(os.getcwd()))

    roots = discovery.projects_roots(
        [projects_root_path, *(extra_projects_roots or ())], existing_config.extra_projects_roots
    )
    project_count = len(
        discovery.resolve_project_dirs(roots, all_projects=True, exclude_projects=existing_config.exclude_projects)
    )

    known = {_root_key(r) for r in roots}
    wsl_roots = [r for r in (find_wsl_roots() if find_wsl_roots else []) if _root_key(r) not in known]

    return Detection(
        config_dir=config_dir,
        config_dir_exists=config_dir.exists(),
        existing_config=existing_config,
        snapshot_count=snapshot_count,
        usage_log_present=usage_log_present,
        project_slug=project_slug,
        project_count=project_count,
        wsl_roots=wsl_roots,
        saved_billing=saved,
    )


@dataclass(slots=True)
class Answers:
    """The "Asked, not guessed (v0.3 init)" question set, resolved --
    from an ``--answers`` file, interactively, or (``--non-interactive``)
    derived from the current :class:`Detection`/:class:`Config`. Every
    value here ends up in either ``config.toml`` or this project's own
    ``projects/<slug>.toml``.
    """

    billing: str = "api"
    exclude_projects: list[str] = field(default_factory=list)
    launch_overlays: bool = False
    shared_project_config: bool = False
    tz: str | None = None
    apply_scope: str = "user"
    capture_window: int = DEFAULT_CAPTURE_WINDOW_DAYS
    #: ``config.toml``'s ``extra_projects_roots``: the ones already
    #: there plus each WSL folder the user said yes to.
    extra_projects_roots: list[str] = field(default_factory=list)
    #: One line per question answered by derivation rather than by the
    #: user -- printed so a ``--non-interactive`` run never silently
    #: guesses without saying so (this task's own "derive instead of
    #: ask, and say how" acceptance rule).
    notes: list[str] = field(default_factory=list)


def load_answers_file(path: str | Path) -> dict:
    """Parse an ``--answers`` file: a flat JSON object whose keys are
    any of :class:`Answers`' field names, plus ``capture_level`` for
    :func:`ask_capture_level` and ``feedback`` for :func:`ask_feedback` (any subset; omitted keys fall back to
    derivation the same as if no file were given at all).
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise OnboardingError(f"cannot read answers file: {path} ({exc})") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise OnboardingError(f"malformed answers file ({path}): not valid JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise OnboardingError(f"malformed answers file ({path}): expected a JSON object")
    return data


#: Answers accepted for the billing question, including the plan names
#: people type instead of "subscription".
BILLING_WORDS = {
    "api": "api",
    "subscription": "subscription",
    "auto": "auto",
    "pro": "subscription",
    "max": "subscription",
    "team": "subscription",
    "enterprise": "subscription",
    "plan": "subscription",
}


def _ask(
    key: str,
    prompt: str,
    default: str,
    *,
    answers_data: dict | None,
    non_interactive: bool,
    stdin: IO[str],
    stdout: IO[str],
    notes: list[str],
) -> str:
    if answers_data is not None and key in answers_data:
        return str(answers_data[key])
    if non_interactive:
        notes.append(f"{key}: not given in --answers; used default {default!r}")
        return default
    stdout.write(f"{prompt} [{default}]: ")
    stdout.flush()
    raw = stdin.readline()
    raw = (raw or "").strip()
    return raw if raw else default


def _ask_bool(
    key: str,
    prompt: str,
    default: bool,
    *,
    answers_data: dict | None,
    non_interactive: bool,
    stdin: IO[str],
    stdout: IO[str],
    notes: list[str],
) -> bool:
    if answers_data is not None and key in answers_data:
        raw = answers_data[key]
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in _TRUE_STRINGS
    raw = _ask(
        key,
        f"{prompt} (y/n)",
        "y" if default else "n",
        answers_data=answers_data,
        non_interactive=non_interactive,
        stdin=stdin,
        stdout=stdout,
        notes=notes,
    )
    return raw.strip().lower() in _TRUE_STRINGS


def gather_answers(
    *,
    detection: Detection,
    answers_path: str | Path | None = None,
    non_interactive: bool = False,
    stdin: IO[str] = sys.stdin,
    stdout: IO[str] = sys.stdout,
    questions: frozenset[str] | None = None,
) -> Answers:
    """Resolve every :class:`Answers` field: an ``--answers`` file wins
    for any key it names; otherwise interactive prompting (default:
    derived from ``detection.existing_config``); under
    ``--non-interactive`` with no matching answers-file key, the derived
    default is used and recorded in ``Answers.notes``.

    ``questions`` (default: all of :data:`QUESTION_KEYS`) are the keys
    asked. Any other key takes the answers file's value or its current
    one, without a note (``init`` asks how you pay itself, and its
    default path asks none of these). A saved ``billing = "auto"`` is
    offered as ``auto``, not as what it works out to, so pressing Enter
    keeps it.
    """
    answers_data = load_answers_file(answers_path) if answers_path is not None else None
    notes: list[str] = []
    existing = detection.existing_config
    asked = frozenset(QUESTION_KEYS) if questions is None else questions

    def quiet(key: str) -> bool:
        return non_interactive or key not in asked

    def notes_for(key: str) -> list[str]:
        return notes if key in asked else []

    billing_raw = _ask(
        "billing",
        "How do you pay for Claude Code? Type subscription for a Pro, Max, Team or Enterprise plan, "
        "api to pay per token with an API key, or auto to let this tool work it out",
        detection.saved_billing or existing.billing,
        answers_data=answers_data,
        non_interactive=quiet("billing"),
        stdin=stdin,
        stdout=stdout,
        notes=notes_for("billing"),
    )
    # Anything else is left as typed, for the config check to reject.
    billing = BILLING_WORDS.get(billing_raw.strip().lower(), billing_raw)

    exclude_raw = answers_data.get("exclude_projects") if answers_data is not None else None
    if exclude_raw is None:
        exclude_str = _ask(
            "exclude_projects",
            "Projects to always leave out: folder names under ~/.claude/projects, separated by commas (blank for none)",
            ",".join(existing.exclude_projects),
            answers_data=None,
            non_interactive=quiet("exclude_projects"),
            stdin=stdin,
            stdout=stdout,
            notes=notes_for("exclude_projects"),
        )
        exclude_projects = [s.strip() for s in exclude_str.split(",") if s.strip()]
    elif isinstance(exclude_raw, list):
        exclude_projects = [str(item) for item in exclude_raw]
    else:
        exclude_projects = [s.strip() for s in str(exclude_raw).split(",") if s.strip()]

    launch_overlays = _ask_bool(
        "launch_overlays",
        "Do you start Claude Code with --settings or CLAUDE_CONFIG_DIR pointing at extra settings "
        "(most people don't)",
        existing.launch_overlays,
        answers_data=answers_data,
        non_interactive=quiet("launch_overlays"),
        stdin=stdin,
        stdout=stdout,
        notes=notes_for("launch_overlays"),
    )

    shared_project_config = _ask_bool(
        "shared_project_config",
        "Is this project's .claude folder (agents, skills) committed to a repo colleagues use",
        existing.shared_project_config,
        answers_data=answers_data,
        non_interactive=quiet("shared_project_config"),
        stdin=stdin,
        stdout=stdout,
        notes=notes_for("shared_project_config"),
    )

    tz_raw = _ask(
        "tz",
        "Time zone, such as Europe/London (blank uses this computer's)",
        existing.tz or "",
        answers_data=answers_data,
        non_interactive=quiet("tz"),
        stdin=stdin,
        stdout=stdout,
        notes=notes_for("tz"),
    )
    tz = tz_raw or None

    apply_scope = _ask(
        "apply_scope",
        "Where should changes you apply go by default: user (all your projects), "
        "project-local (this project, just you) or repo (this project, everyone)",
        existing.apply_scope,
        answers_data=answers_data,
        non_interactive=quiet("apply_scope"),
        stdin=stdin,
        stdout=stdout,
        notes=notes_for("apply_scope"),
    )

    capture_window_raw = _ask(
        "capture_window",
        "How many days to collect data before the first baseline",
        str(existing.capture_window or DEFAULT_CAPTURE_WINDOW_DAYS),
        answers_data=answers_data,
        non_interactive=quiet("capture_window"),
        stdin=stdin,
        stdout=stdout,
        notes=notes_for("capture_window"),
    )
    try:
        capture_window = int(capture_window_raw)
    except (TypeError, ValueError):
        capture_window = DEFAULT_CAPTURE_WINDOW_DAYS
        notes_for("capture_window").append(
            f"capture_window: {capture_window_raw!r} is not an integer; used default "
            f"{capture_window!r}"
        )

    extra_projects_roots = _gather_extra_roots(
        detection,
        answers_data=answers_data,
        non_interactive=quiet("extra_projects_roots"),
        stdin=stdin,
        stdout=stdout,
        notes=notes_for("extra_projects_roots"),
    )

    return Answers(
        billing=billing,
        exclude_projects=exclude_projects,
        launch_overlays=launch_overlays,
        shared_project_config=shared_project_config,
        tz=tz,
        apply_scope=apply_scope,
        capture_window=capture_window,
        extra_projects_roots=extra_projects_roots,
        notes=notes,
    )


#: Answers to the capture question that mean a level.
_CAPTURE_WORDS = {"no": "off", "n": "off", "none": "off", "yes": "essentials", "y": "essentials", "on": "essentials"}

CAPTURE_INTRO = (
    "Metrics capture (optional)\n"
    "ClaudeGlass can have Claude note a few words about each piece of work, such as the kind of task, how clear "
    "the request was and whether an agent finished, so its suggestions fit how you work. This uses your tokens: "
    "Claude reads a short note when a session or subagent starts, and ends each reply with a one-line tag such as "
    "[tl: task=bugfix brief=clear], which you will see. The free level only logs a few events to a local file.\n"
)


def capture_answer(answers_path: str | Path | None = None, preset: str | None = None) -> str | None:
    """The capture level given without asking: ``--capture-level``
    (``preset``), else the answers file's ``capture_level`` key, else
    ``None``."""
    if preset is not None:
        return preset
    if answers_path is None:
        return None
    raw = load_answers_file(answers_path).get("capture_level")
    return None if raw is None else str(raw)


def ask_capture_level(
    *,
    estimates,
    preset: str | None = None,
    answers_path: str | Path | None = None,
    non_interactive: bool = False,
    stdin: IO[str] = sys.stdin,
    stdout: IO[str] = sys.stdout,
) -> tuple[str, list[str]]:
    """init's last question: turn on metrics capture, and at which level.

    ``preset`` (``--capture-level``) or the answers file's
    ``capture_level`` key answers it; otherwise it is asked, after the
    token-use warning and ``estimates()`` (lines saying what each level
    would have cost you, worked out only when they are shown). Under
    ``--non-interactive`` with no answer, capture stays off and a note
    says so. Returns ``(level, notes)``; yes/no answers become
    ``essentials``/``off``, and anything else comes back as typed for the
    caller to reject.
    """
    notes: list[str] = []
    given = capture_answer(answers_path, preset)
    if given is None and non_interactive:
        notes.append(
            "capture_level: not given in --answers; metrics capture left off "
            "(turn it on later with 'claudeglass capture on')"
        )
        return "off", notes
    stdout.write("\n" + CAPTURE_INTRO)
    lines = estimates()
    if lines:
        stdout.write("\n".join(lines) + "\n")
    stdout.write(pages.plain("Change it or turn it off any time: 'claudeglass capture', or {{page:setup/capture}}.\n"))
    raw = _ask(
        "capture_level",
        "Metrics capture level: " + ", ".join(capture_catalogue.LEVELS),
        "off",
        answers_data={"capture_level": given} if given is not None else None,
        non_interactive=non_interactive,
        stdin=stdin,
        stdout=stdout,
        notes=notes,
    )
    word = raw.strip().lower()
    return _CAPTURE_WORDS.get(word, word), notes


def capture_no_limit_answer(answers_path: str | Path | None = None, preset: bool | None = None) -> bool | None:
    """The time-box question's answer given without asking:
    ``--capture-no-limit`` (``preset``), else the answers file's
    ``capture_no_limit`` key, else ``None`` (not answered -- ask, or
    under ``--non-interactive``, leave today's ``until`` as it is)."""
    if preset is not None:
        return preset
    if answers_path is None:
        return None
    raw = load_answers_file(answers_path).get("capture_no_limit")
    if raw is None:
        return None
    return raw if isinstance(raw, bool) else str(raw).strip().lower() in _TRUE_STRINGS


def ask_capture_until(
    *,
    now: datetime,
    preset: bool | None = None,
    answers_path: str | Path | None = None,
    non_interactive: bool = False,
    stdin: IO[str] = sys.stdin,
    stdout: IO[str] = sys.stdout,
) -> tuple[str, list[str]]:
    """After :func:`ask_capture_level` turns a level on: offers a
    time-box (:data:`DEFAULT_CAPTURE_TIMEBOX_DAYS` days from ``now``, by
    default) so capture doesn't run forever unnoticed, reusing the same
    ISO-8601 ``until`` :func:`~claudeglass.config.set_capture` and
    ``capture on --for``/``--until`` already understand.

    ``preset`` (``--capture-no-limit``) or the answers file's
    ``capture_no_limit`` key answers it without asking; when true,
    capture gets no time-box at all. Under ``--non-interactive`` with
    neither, the default time-box is used, the same "no answer -> the
    derived default, and a note says so" rule every other onboarding
    question already follows (CAP-8: a scripted, unattended ``init`` is
    exactly the case this default most needs to reach -- capture left
    running forever with nobody watching is the failure mode, not a
    surprise end date). This is only ever reached from ``off``, so there
    is never an *existing* ``until`` for it to clobber; ``ask_capture_until``
    is skipped entirely when capture is already on and no new level was
    asked for (see ``cli._init_capture_choice``).

    Returns ``(until, notes)``: ``until`` is an ISO-8601 time, or ``""``
    for an explicit "no limit".
    """
    notes: list[str] = []
    given = capture_no_limit_answer(answers_path, preset)
    until = (now + timedelta(days=DEFAULT_CAPTURE_TIMEBOX_DAYS)).isoformat(timespec="seconds")
    if given is None:
        stdout.write(
            "\nMetrics capture will switch itself off on "
            f"{until[:16].replace('T', ' ')} UTC ({DEFAULT_CAPTURE_TIMEBOX_DAYS} days from now) unless you say "
            "otherwise. Keep it on longer with 'claudeglass capture on --for 30d', or turn off the time "
            "limit below so it runs until you switch it off.\n"
        )
    no_limit = _ask_bool(
        "capture_no_limit",
        "Turn off that time limit (capture then runs until you switch it off)",
        False,
        answers_data={"capture_no_limit": given} if given is not None else None,
        non_interactive=non_interactive,
        stdin=stdin,
        stdout=stdout,
        notes=notes,
    )
    return ("" if no_limit else until), notes


FEEDBACK_INTRO = (
    "Feedback after a piece of work (optional)\n"
    "ClaudeGlass can add a /cl-feedback skill to Claude Code. Run it when you finish a piece of work and tick four "
    "quick questions: did it deliver, what slowed it, was it worth the tokens, and what would have helped. Your "
    "answers show which work paid off, so the tips fit how you work. It costs nothing until you run it, then about "
    "two short turns, and a second status line reminds you it's there. It works at any capture level, even off.\n"
)


def feedback_answer(answers_path: str | Path | None = None, preset: str | None = None) -> str | None:
    """The feedback answer given without asking: ``--feedback``
    (``preset``), else the answers file's ``feedback`` key, else
    ``None``."""
    if preset is not None:
        return preset
    if answers_path is None:
        return None
    raw = load_answers_file(answers_path).get("feedback")
    if raw is None:
        return None
    return ("on" if raw else "off") if isinstance(raw, bool) else str(raw)


def ask_feedback(
    *,
    preset: str | None = None,
    answers_path: str | Path | None = None,
    non_interactive: bool = False,
    stdin: IO[str] = sys.stdin,
    stdout: IO[str] = sys.stdout,
) -> tuple[bool, list[str]]:
    """init's question after capture: add the ``/cl-feedback`` skill and
    its status-line reminder. ``preset`` (``--feedback``) or the answers
    file's ``feedback`` key answers it; under ``--non-interactive`` with
    neither it stays off and a note says so. Returns ``(on, notes)``."""
    notes: list[str] = []
    given = feedback_answer(answers_path, preset)
    if given is None and non_interactive:
        notes.append("feedback: not given in --answers; left off (add it later with 'claudeglass capture feedback on')")
        return False, notes
    if given is None:
        stdout.write("\n" + FEEDBACK_INTRO)
    on = _ask_bool(
        "feedback",
        "Add the /cl-feedback skill?",
        False,
        answers_data={"feedback": given} if given is not None else None,
        non_interactive=non_interactive,
        stdin=stdin,
        stdout=stdout,
        notes=notes,
    )
    return on, notes


def _gather_extra_roots(
    detection: Detection,
    *,
    answers_data: dict | None,
    non_interactive: bool,
    stdin: IO[str],
    stdout: IO[str],
    notes: list[str],
) -> list[str]:
    """The folders for ``extra_projects_roots``. An ``--answers`` list
    replaces them outright. Otherwise the existing ones are kept and each
    newly found WSL folder is offered (yes by default: it holds the same
    person's own sessions); ``--non-interactive`` adds them and says so."""
    raw = answers_data.get("extra_projects_roots") if answers_data is not None else None
    if raw is not None:
        items = raw if isinstance(raw, list) else str(raw).split(",")
        return [str(item).strip() for item in items if str(item).strip()]
    roots = list(detection.existing_config.extra_projects_roots)
    for found in detection.wsl_roots:
        label = discovery.source_label(found)
        if non_interactive:
            notes.append(f"extra_projects_roots: found Claude Code sessions in {label}; added {found}")
            roots.append(str(found))
            continue
        if _ask_bool(
            "extra_projects_roots",
            f"Claude Code also runs in {label} on this computer ({found}). Include those sessions",
            True,
            answers_data=None,
            non_interactive=False,
            stdin=stdin,
            stdout=stdout,
            notes=notes,
        ):
            roots.append(str(found))
    return roots


def offer_hook_repair(health, *, repair_hook: bool, non_interactive: bool, stdin, stdout, now) -> None:
    """A broken SessionStart hook command that can be fixed (see
    ``hook_health``): repair it with ``--repair-hook`` or a yes at the
    prompt, never silently. The fix changes only that command string,
    and settings.json is backed up first."""
    if health.fixed_command is None:
        return
    stdout.write(
        f"\nThe snapshot hook in Claude Code's settings.json ({footprint.home_label(health.settings_path)}) "
        f"can't run. Its command is\n  {health.command!r}\nand should be\n  {health.fixed_command!r}\n"
    )
    if not repair_hook:
        if non_interactive:
            stdout.write("Run 'claudeglass init --repair-hook' to fix it (settings.json is backed up first).\n\n")
            return
        stdout.write("Fix it now? settings.json is backed up first. (y/n) [n]: ")
        stdout.flush()
        if (stdin.readline() or "").strip().lower() not in _TRUE_STRINGS:
            stdout.write("Left unchanged.\n\n")
            return
    try:
        backup = hook_health.repair(health, now=now)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        stdout.write(f"Could not fix the hook command: {exc}\n\n")
        return
    stdout.write(f"Fixed. The previous settings.json is at {footprint.home_label(backup)}\n{RESTART_NOTE}\n\n")


def config_updates(answers: Answers, detection: Detection, now: datetime) -> dict:
    """The ``config.toml`` values ``answers`` sets. Writes nothing."""
    updates: dict = {
        "billing": answers.billing,
        "exclude_projects": answers.exclude_projects,
        "launch_overlays": answers.launch_overlays,
        "shared_project_config": answers.shared_project_config,
        "apply_scope": answers.apply_scope,
        "capture_window": answers.capture_window,
        "extra_projects_roots": answers.extra_projects_roots,
        # Set once, on the first init: re-running init (to change an
        # answer, or with --repair-hook) must not restart the capture
        # window the user is already part-way through.
        "capture_started": detection.existing_config.capture_started or now.isoformat(),
    }
    if answers.tz is not None:
        updates["tz"] = answers.tz
    return updates


def save_config(config_dir: Path, updates: dict, *, answers: Answers | None = None, stdout: IO[str]) -> int:
    """Write ``updates`` to ``config.toml`` and, when ``answers`` is
    given, this project's ``projects/<slug>.toml``. Returns 0, or 2 when
    ``config.toml`` can't be validated (nothing is written then)."""
    try:
        written_path = write_config_values(config_dir, updates)
    except ConfigError as exc:
        stdout.write(f"claudeglass init: {exc}\n")
        return 2

    stdout.write(f"Wrote {_relative_label(written_path, config_dir)}\n")
    if written_path.name == "config.toml.new":
        stdout.write(
            "(the existing config.toml had a shape init could not merge automatically -- "
            "reconcile config.toml.new by hand and rename it into place)\n"
        )
    if answers is None:
        return 0

    project_config = ProjectConfig(
        kind=None,
        shared_project_config=answers.shared_project_config,
        launch_overlays=answers.launch_overlays,
        apply_scope=answers.apply_scope,
    )
    project_slug = discovery.slug_for(os.getcwd())
    project_path = save_project_config(config_dir, project_slug, project_config)
    # The file is named after the project's folder, user name and all;
    # the output names it the redacted way.
    stdout.write(f"Wrote this project's settings, projects/{discovery.redact_slug(project_path.stem)}.toml\n")
    return 0


def baseline_project_dirs(
    config: Config,
    *,
    projects_root_path: Path,
    extra_projects_roots: list[Path] | None = None,
    all_projects: bool = False,
    project: list[str] | None = None,
    project_family: str | None = None,
) -> list[Path]:
    """The project folders the initial baseline reads: the current
    folder's own, unless a selection flag says otherwise (fix S6)."""
    baseline_slugs = list(project) if project else None
    if not all_projects and not project_family and not baseline_slugs:
        baseline_slugs = [discovery.slug_for(os.getcwd())]
    return discovery.resolve_project_dirs(
        discovery.projects_roots([projects_root_path, *(extra_projects_roots or ())], config.extra_projects_roots),
        slugs=baseline_slugs,
        all_projects=all_projects,
        family_regex=project_family,
        exclude_projects=config.exclude_projects,
    )


def run_baseline(config_dir: Path, project_dirs: list[Path], *, now: datetime, stdout: IO[str]) -> tuple[dict, Path] | None:
    """Build and save the initial baseline from ``project_dirs``. Returns
    the record and the path it was saved to, or ``None`` when pricing
    can't be loaded (said on ``stdout``)."""
    config = load_config(config_dir)
    try:
        pricing = load_pricing(path=config.pricing_path, config_dir=config_dir)
    except PricingError as exc:
        stdout.write(f"claudeglass init: {exc}\n")
        return None
    record, _model = baseline_mod.build_baseline(
        config=config,
        pricing=pricing,
        config_dir=config_dir,
        project_dirs=project_dirs,
        now=now,
    )
    report_markdown = baseline_mod.render_onboarding_report(record)
    return record, baseline_mod.save_baseline(config_dir, record, report_markdown)

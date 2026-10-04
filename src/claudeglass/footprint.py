"""What claudeglass has installed and changed on this machine, and
how to take each part back out.

One list, used by ``claudeglass changes``, ``claudeglass
uninstall`` and the Data quality tab (``GET /api/setup``):

- the SessionStart snapshot hook and the statusline in Claude Code's
  ``settings.json`` (``hook_health.settings_path``: ``--claude-root``,
  else ``$CLAUDE_CONFIG_DIR``, else ``~/.claude``);
- the metrics-capture hook entries, when capture was connected;
- the ``/cg-feedback`` skill (``<claude-root>/skills/cg-feedback``), when
  feedback was turned on;
- the logon service (``install-service`` or ``init``);
- each change ``apply`` made to your Claude Code settings or agent files
  that has not been reverted;
- the active-profile marker and any one-session profile files;
- this tool's own data folder.

Every item says what it does, whether it costs tokens, and the command
that undoes it. :func:`plan_uninstall` and :func:`remove_settings_entries`
back ``uninstall``: they show the exact ``settings.json`` change first and
back the file up before writing, like ``init``'s connect step.
"""

from __future__ import annotations

import difflib
import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import capture_catalogue, discovery, hook_health
from .config import CaptureConfig, ConfigError, load_config
from .profiles import apply as apply_mod

#: What a statusLine command of this tool's looks like.
_STATUSLINE_MARKERS = ("claudeglass.statusline", "claudeglass")


@dataclass(slots=True)
class FootprintItem:
    key: str
    title: str
    #: "installed", "not installed", "in place", "undone" or "unknown".
    status: str
    where: str
    what_it_does: str
    #: Whether it adds tokens to Claude's context, in plain words.
    token_cost: str
    #: The command (or plain instruction) that removes or undoes it.
    undo: str

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "title": self.title,
            "status": self.status,
            "where": self.where,
            "what_it_does": self.what_it_does,
            "token_cost": self.token_cost,
            "undo": self.undo,
        }


def home_label(path: Path | str) -> str:
    """``path`` with the home folder written as ``~``, so output names
    the file without your user name."""
    text = str(path)
    home = str(Path.home())
    if text.lower().startswith(home.lower()):
        return "~" + text[len(home):]
    return text


def _settings(claude_root: str | Path | None) -> tuple[Path, dict | None]:
    path = hook_health.settings_path(claude_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return path, None
    return path, data if isinstance(data, dict) else None


def is_own_statusline(settings: dict | None) -> bool:
    status_line = (settings or {}).get("statusLine")
    command = status_line.get("command") if isinstance(status_line, dict) else None
    return isinstance(command, str) and any(marker in command for marker in _STATUSLINE_MARKERS)


def _folder_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total


def _size_label(size: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:,.0f} {unit}" if unit == "bytes" else f"{size:,.1f} {unit}"
        size /= 1024
    return f"{size:,.1f} GB"


#: What to expect from installing and using this tool, in plain words:
#: (title, text), while metrics capture is off. Shown by ``changes``, the
#: Data quality tab (``GET /api/setup``) and docs/first-run.md;
#: :func:`expectations` swaps the first item while capture is on.
EXPECTATIONS: tuple[tuple[str, str], ...] = (
    (
        "It never uses your Claude tokens",
        "This tool reads files Claude Code already writes. It never calls Claude, so it adds nothing to your "
        "usage, on the first run or after, unless you turn on metrics capture.",
    ),
    (
        "The hook and statusline add nothing to Claude's context",
        "The snapshot hook prints nothing and the statusline is shown only to you, so neither is sent to Claude. "
        "The hook starts a short Python process at each session start, which takes well under a second.",
    ),
    (
        "The first scan takes a while",
        "The first report or dashboard start reads every transcript under ~/.claude/projects, which can take a few "
        "minutes and use a CPU core. Later runs read only new or changed files.",
    ),
    (
        "It reads, it doesn't change",
        "The dashboard never changes Claude Code. Every fix is a prompt for Claude, which shows you the diff and "
        "asks before editing, or an apply command you run yourself with --dry-run first.",
    ),
    (
        "A change takes effect in new sessions",
        "Claude Code reads settings when a session starts, and every session builds its cache from scratch anyway, "
        "so a change costs nothing extra to switch on. Switching model inside a running session (/model) does "
        "rebuild that session's cache once.",
    ),
    (
        "Cheaper isn't free",
        "A cheaper model, lower effort or earlier summaries can mean more replies or missed details on hard work. "
        "Check {{page:changes}} after a few sessions, and undo with the apply --revert command if it's worse.",
    ),
    (
        "Amounts are list-price equivalents",
        "Costs are worked out from each model's list price. On a Pro or Max plan you don't pay per token, so they "
        "show as a share of your weekly limit when the statusline has recorded limit readings.",
    ),
    (
        "Your data stays on this machine",
        "Transcripts and settings are read locally. CLAUDE.md text and skill descriptions are read when you ask "
        "for a review and never stored. Nothing is uploaded.",
    ),
)

#: The one command that takes everything back out, shown dry-run first.
UNINSTALL_COMMAND = "claudeglass uninstall --revert-changes --delete-data --dry-run"


#: What coaching notes cost, for :func:`expectations` and the hooks row.
COACHING_COST = (
    "Coaching notes are on: when a hint applies, a hook adds a short note (about 50 to 140 tokens) to Claude's "
    "context. {{page:setup/capture}} shows what they cost. Turn them off with "
    "'claudeglass capture disable coaching_notes'."
)


def expectations(capture: CaptureConfig | None = None) -> tuple[tuple[str, str], ...]:
    """:data:`EXPECTATIONS`, with the first item saying what metrics
    capture costs while ``capture`` is on, and what coaching notes cost
    while they are."""
    if capture is None or not (capture.is_on or capture.coaching_notes_on):
        return EXPECTATIONS
    if not capture.is_on:
        return (("It uses a few of your Claude tokens while coaching notes are on", COACHING_COST),) + EXPECTATIONS[1:]
    level = capture_catalogue.LEVEL_TITLES.get(capture.level, capture.level)
    if _uses_tokens(capture):
        text = (
            f"Metrics capture is on ({level}). Claude reads a short note when a session or subagent starts and "
            "writes a one-line tag at the end of its replies, so it uses some of your tokens. {{page:setup/capture}} "
            "shows how many. Turn it off with 'claudeglass capture off'."
        )
    else:
        text = (
            f"Metrics capture is on ({level}), but at this level it only logs a few free signals to a local "
            "file, so it adds no tokens. This tool never calls Claude itself."
        )
    if capture.coaching_notes_on:
        text += " " + COACHING_COST
    return ((("It uses a few of your Claude tokens while capture is on"), text),) + EXPECTATIONS[1:]


def _hooks_token_cost(capture: CaptureConfig, level: str) -> str:
    """The capture hooks row's token cost: capture's note and tags, and
    coaching notes while they're on."""
    if capture.is_on and _uses_tokens(capture):
        text = f"Some while capture is on (now: {level}): the note and the tags."
    elif capture.is_on:
        text = f"None at {level}: the free signals only write to a local file."
    else:
        text = "None from capture while it's off." if capture.coaching_notes_on else "None while capture is off."
    if capture.coaching_notes_on:
        text += " Coaching notes: about 50 to 140 tokens each time a hint applies."
    if (capture.is_on and _uses_tokens(capture)) or capture.coaching_notes_on:
        text += " {{page:setup/capture}} shows the measured amount."
    return text


def _uses_tokens(capture: CaptureConfig) -> bool:
    """Whether any metric switched on has Claude read a note or write a
    tag (the free signals don't)."""
    return any(capture_catalogue.asks_claude(i) for i in capture.active_metrics())


#: The skills this tool can write into Claude Code's skills folder, and
#: what each ``SKILL.md`` holds: ``/cg-feedback`` (``capture feedback on``)
#: and ``/cg-brief`` (``capture brief on``).
SKILL_TEXTS = {
    capture_catalogue.FEEDBACK_SKILL: capture_catalogue.feedback_skill_text,
    capture_catalogue.BRIEF_SKILL: capture_catalogue.brief_skill_text,
}


def skill_path(name: str, claude_root: str | Path | None = None) -> Path:
    """Where this tool writes the skill ``name``."""
    return discovery.claude_root(claude_root) / "skills" / name / "SKILL.md"


def is_own_skill(name: str, text: str) -> bool:
    """Whether a ``SKILL.md`` is the ``name`` skill this tool writes, in
    any version."""
    return f"name: {name}\n" in text and "ClaudeGlass" in text


def read_skill(name: str, claude_root: str | Path | None = None) -> str | None:
    """The skill's ``SKILL.md`` as it is now, or ``None`` when there is
    none (or it can't be read)."""
    try:
        return skill_path(name, claude_root).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def old_skill(name: str, claude_root: str | Path | None = None) -> str | None:
    """An earlier name of ``name`` (``tl-feedback`` or ``cl-feedback`` for
    ``cg-feedback``), when this tool's copy is still there under it."""
    for old, new in capture_catalogue.RENAMED_SKILLS.items():
        if new == name:
            text = read_skill(old, claude_root)
            if text is not None and is_own_skill(old, text):
                return old
    return None


def skill_state(name: str, claude_root: str | Path | None = None) -> str:
    """``installed`` (this version), ``outdated`` (an earlier one),
    ``renamed`` (none, but ours is still under its old name: see
    :func:`old_skill`), ``foreign`` (a SKILL.md there this tool didn't
    write) or ``missing``."""
    text = read_skill(name, claude_root)
    if text is None:
        return "renamed" if old_skill(name, claude_root) else "missing"
    if not is_own_skill(name, text):
        return "foreign"
    return "installed" if text == SKILL_TEXTS[name]() else "outdated"


def write_skill(name: str, claude_root: str | Path | None = None) -> Path:
    """Write the skill (a temporary file, then a rename)."""
    path = skill_path(name, claude_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(SKILL_TEXTS[name](), encoding="utf-8", newline="\n")
    os.replace(tmp, path)
    return path


def remove_skill(name: str, claude_root: str | Path | None = None) -> Path:
    """Delete the skill, and its folder once empty."""
    path = skill_path(name, claude_root)
    path.unlink()
    try:
        path.parent.rmdir()
    except OSError:
        pass
    return path


def rename_skill(old: str, new: str, claude_root: str | Path | None = None) -> Path:
    """Write the skill under its new name, then delete the old one."""
    path = write_skill(new, claude_root)
    remove_skill(old, claude_root)
    return path


def feedback_skill_path(claude_root: str | Path | None = None) -> Path:
    """Where ``capture feedback on`` writes the ``/cg-feedback`` skill."""
    return skill_path(capture_catalogue.FEEDBACK_SKILL, claude_root)


def is_own_feedback_skill(text: str) -> bool:
    """Whether a ``SKILL.md`` is the ``/cg-feedback`` this tool writes."""
    return is_own_skill(capture_catalogue.FEEDBACK_SKILL, text)


def read_feedback_skill(claude_root: str | Path | None = None) -> str | None:
    """The ``/cg-feedback`` ``SKILL.md`` as it is now, or ``None``."""
    return read_skill(capture_catalogue.FEEDBACK_SKILL, claude_root)


def feedback_skill_state(claude_root: str | Path | None = None) -> str:
    """:func:`skill_state` of ``/cg-feedback``."""
    return skill_state(capture_catalogue.FEEDBACK_SKILL, claude_root)


def write_feedback_skill(claude_root: str | Path | None = None) -> Path:
    """Write the ``/cg-feedback`` skill."""
    return write_skill(capture_catalogue.FEEDBACK_SKILL, claude_root)


def remove_feedback_skill(claude_root: str | Path | None = None) -> Path:
    """Delete the ``/cg-feedback`` skill."""
    return remove_skill(capture_catalogue.FEEDBACK_SKILL, claude_root)


def capture_setting(config_dir: str | Path) -> CaptureConfig:
    """``[capture]`` from ``config.toml``; off when it can't be read."""
    try:
        return load_config(config_dir=config_dir).capture
    except (ConfigError, OSError, ValueError):
        return CaptureConfig()


def inventory(
    config_dir: str | Path,
    *,
    service_registered: bool | None = None,
    claude_root: str | Path | None = None,
) -> list[FootprintItem]:
    """Everything this tool has put on the machine, in the order
    ``uninstall`` removes it. ``service_registered`` comes from
    ``installer.is_registered`` (``None`` means it could not be checked)."""
    config_dir = Path(config_dir)
    settings_path, settings = _settings(claude_root)
    items: list[FootprintItem] = []

    health = hook_health.check(config_dir, claude_root=claude_root)
    items.append(
        FootprintItem(
            key="snapshot_hook",
            title="Config snapshot hook",
            status="installed" if health.command else "not installed",
            where=home_label(settings_path),
            what_it_does=(
                "When a Claude Code session starts, runs a small Python script that records your settings: "
                "key names, a few safe values and file sizes, never file contents. It runs in the background "
                "and prints nothing."
            ),
            token_cost="None. The hook prints nothing, so nothing is added to Claude's context.",
            undo="claudeglass uninstall",
        )
    )
    items.append(
        FootprintItem(
            key="statusline",
            title="Statusline",
            status="installed" if is_own_statusline(settings) else "not installed",
            where=home_label(settings_path),
            what_it_does=(
                "Shows context, usage limits and cache health under the prompt in the terminal, and logs them "
                "for {{page:spend/usage}} and {{page:cache/rebuilds}}. Claude Code runs it in the terminal only, not in the desktop app."
            ),
            token_cost="None. The statusline is shown to you; Claude never reads it.",
            undo="claudeglass uninstall",
        )
    )
    capture = capture_setting(config_dir)
    capture_health = hook_health.check_capture(
        hook_health.capture_specs(capture.hook_metrics()), claude_root=claude_root, config_dir=config_dir
    )
    installed = capture_health.needed + capture_health.extra
    installed = tuple(spec for spec in installed if spec not in capture_health.missing)
    if installed or capture.hooked:
        level = capture_catalogue.LEVEL_TITLES.get(capture.level, capture.level)
        items.append(
            FootprintItem(
                key="capture_hooks",
                title=f"Metrics capture hooks ({len(installed)} entr{'y' if len(installed) == 1 else 'ies'})",
                status="installed" if installed else "not installed",
                where=home_label(settings_path),
                what_it_does=(
                    "While metrics capture is on, adds a short note when a session or subagent starts asking Claude "
                    "to end its replies with a one-line tag (task kind, how clear the request was, and so on), so "
                    "this tool can tell where your tokens go. The free signals (why sessions end, when Claude "
                    "waited for you, which tools asked for permission) go to a file in this tool's data folder. "
                    "With coaching notes on, they also add a short hint to Claude's context when one applies, at "
                    "any capture level. With /cg-feedback on, they add a line of counts when you run it. The plan check "
                    "and rating reminder add a note when they are on. With all of these off the hooks add nothing. "
                    "Claude Code runs "
                    "a small launcher, which runs the hook's code and its word list, all three in this tool's data "
                    "folder; removing that folder takes them all out. Claude Code waits for the hook after a read, "
                    "search or web result and each message you send, and never after a shell command."
                ),
                token_cost=_hooks_token_cost(capture, level),
                undo="claudeglass capture off, then claudeglass capture remove",
            )
        )
    skill_text = read_feedback_skill(claude_root)
    own_skill = skill_text is not None and is_own_feedback_skill(skill_text)
    # Still under an earlier name: installed all the same.
    old_feedback = None if own_skill else old_skill(capture_catalogue.FEEDBACK_SKILL, claude_root)
    own_skill = own_skill or old_feedback is not None
    if own_skill or "feedback_skill" in capture.feedback:
        items.append(
            FootprintItem(
                key="feedback_skill",
                title="The /cg-feedback skill",
                status="installed" if own_skill else "not installed",
                where=home_label(skill_path(old_feedback, claude_root) if old_feedback else feedback_skill_path(claude_root)),
                what_it_does=(
                    "A skill you run after a piece of work: a few checkbox questions (more after an approved plan) "
                    "whose answers ClaudeGlass reads from the transcript, so its suggestions fit how you work. "
                    "Claude never runs it by itself."
                ),
                token_cost=(
                    "None until you run it: Claude doesn't see its description. Each run costs about two short "
                    "turns, three when a second round of questions applies, shown on {{page:setup/capture}}."
                ),
                undo="claudeglass capture feedback off",
            )
        )
    brief_name = capture_catalogue.BRIEF_SKILL
    brief_text = read_skill(brief_name, claude_root)
    own_brief = brief_text is not None and is_own_skill(brief_name, brief_text)
    old_brief = None if own_brief else old_skill(brief_name, claude_root)
    own_brief = own_brief or old_brief is not None
    if own_brief or "brief_templates" in capture.coaching:
        items.append(
            FootprintItem(
                key="brief_skill",
                title="The /cg-brief skill",
                status="installed" if own_brief else "not installed",
                where=home_label(skill_path(old_brief or brief_name, claude_root)),
                what_it_does=(
                    "A skill you run with a request: Claude checks it against a short checklist for its kind of "
                    "task and asks once for anything missing. Claude never runs it by itself."
                ),
                token_cost=(
                    "None until you run it: Claude doesn't see its description. Each run adds the checklist, "
                    "about 400 tokens, and at most one short question."
                ),
                undo="claudeglass capture brief off",
            )
        )
    items.append(
        FootprintItem(
            key="service",
            title="Dashboard at logon",
            status={True: "installed", False: "not installed"}.get(service_registered, "unknown"),
            where="Scheduled task, launch agent or systemd user unit",
            what_it_does=(
                "Starts the dashboard when you log on. It reads your transcripts on this machine and makes no "
                "network calls."
            ),
            token_cost="None. It reads files; it never calls Claude.",
            undo="claudeglass uninstall-service",
        )
    )

    for backup in apply_mod.list_backups(config_dir):
        # "one-off" is cli.ONE_OFF_PROFILE_ID, recorded for apply --set.
        label = "one-off change" if backup.profile_id in ("one-off", "") else f"profile {backup.profile_id}"
        items.append(
            FootprintItem(
                key=f"apply:{backup.ts}",
                title=f"Applied {label} ({backup.file_count} file{'s' if backup.file_count != 1 else ''})",
                status="undone" if backup.reverted_at else "in place",
                where=f"{_scope_label(backup.scope)}; backup in {home_label(config_dir / 'backups' / backup.ts)}",
                what_it_does="Changed Claude Code settings or agent files. This changes how Claude works from the next session.",
                token_cost=(
                    "None by itself: Claude Code reads settings when a session starts, and every session builds "
                    "its cache from scratch anyway. After that it saves or costs what its estimate said."
                ),
                undo=f"claudeglass apply --revert {backup.ts}",
            )
        )

    active = config_dir / "active-profile"
    if active.is_file():
        try:
            profile_id = active.read_text(encoding="utf-8").strip()
        except OSError:
            profile_id = ""
        items.append(
            FootprintItem(
                key="active_profile",
                title=f"Active profile marker ({profile_id or 'unknown'})",
                status="installed",
                where=home_label(active),
                what_it_does="Remembers which profile you applied last, so snapshots can be tagged with it.",
                token_cost="None.",
                undo="Deleted with this tool's data folder.",
            )
        )

    data_size = _folder_size(config_dir) if config_dir.is_dir() else 0
    items.append(
        FootprintItem(
            key="data",
            title="This tool's data folder",
            status="installed" if config_dir.is_dir() else "not installed",
            where=home_label(config_dir),
            what_it_does=(
                f"Holds the dashboard database, parsed-transcript cache, settings snapshots, usage log, your "
                f"profiles and the backups that let you undo applied changes ({_size_label(data_size)})."
            ),
            token_cost="None.",
            undo="claudeglass uninstall --delete-data",
        )
    )
    return items


def _scope_label(scope: str) -> str:
    return {
        "user": "your user settings (every project)",
        "project-local": "this project, on your machine only",
        "repo": "this project, for everyone who uses the repository",
    }.get(scope, scope or "unknown scope")


@dataclass(slots=True)
class UninstallPlan:
    settings_path: Path
    #: Plain sentences, one per settings.json entry removed.
    settings_changes: list[str] = field(default_factory=list)
    settings_diff: str = ""
    new_settings_text: str | None = None
    #: settings.json as the plan was worked out from (``""``: no file);
    #: nothing is written if it changed since.
    old_settings_text: str | None = None
    #: Applied changes still in place, newest first.
    applied: list[apply_mod.BackupInfo] = field(default_factory=list)
    data_dir: Path | None = None
    #: The ``/cg-feedback`` skill this tool wrote, when it is there.
    feedback_skill: Path | None = None
    #: The ``/cg-brief`` skill this tool wrote, when it is there.
    brief_skill: Path | None = None
    #: Either skill under an earlier name (``/tl-feedback``,
    #: ``/cl-brief``, ...), when this tool's copy is still there: old name
    #: -> path.
    old_skills: dict[str, Path] = field(default_factory=dict)


def plan_uninstall(config_dir: str | Path, *, claude_root: str | Path | None = None) -> UninstallPlan:
    """What ``uninstall`` would remove from ``settings.json``, and which
    applied changes are still in place. Writes nothing."""
    config_dir = Path(config_dir)
    settings_path, settings = _settings(claude_root)
    plan = UninstallPlan(settings_path=settings_path, data_dir=config_dir if config_dir.is_dir() else None)
    plan.applied = [b for b in reversed(apply_mod.list_backups(config_dir)) if not b.reverted_at]
    skill_text = read_feedback_skill(claude_root)
    if skill_text is not None and is_own_feedback_skill(skill_text):
        plan.feedback_skill = feedback_skill_path(claude_root)
    brief_text = read_skill(capture_catalogue.BRIEF_SKILL, claude_root)
    if brief_text is not None and is_own_skill(capture_catalogue.BRIEF_SKILL, brief_text):
        plan.brief_skill = skill_path(capture_catalogue.BRIEF_SKILL, claude_root)
    for old in capture_catalogue.RENAMED_SKILLS:
        old_text = read_skill(old, claude_root)
        if old_text is not None and is_own_skill(old, old_text):
            plan.old_skills[old] = skill_path(old, claude_root)
    if settings is None:
        return plan

    before = settings_path.read_text(encoding="utf-8")
    after_settings = json.loads(before)
    hooks = after_settings.get("hooks")
    groups = hooks.get("SessionStart") if isinstance(hooks, dict) else None
    if isinstance(groups, list):
        kept_groups = []
        for group in groups:
            entries = group.get("hooks") if isinstance(group, dict) else None
            if isinstance(entries, list):
                kept = [
                    e
                    for e in entries
                    if not (isinstance(e, dict) and hook_health.HOOK_SCRIPT_NAME in str(e.get("command", "")))
                ]
                if len(kept) != len(entries):
                    plan.settings_changes.append("Remove the SessionStart hook that runs the config snapshot script.")
                if not kept:
                    continue
                group = {**group, "hooks": kept}
            kept_groups.append(group)
        if kept_groups:
            hooks["SessionStart"] = kept_groups
        else:
            hooks.pop("SessionStart", None)
            if not hooks:
                after_settings.pop("hooks", None)
    plan.settings_changes += hook_health.remove_capture_entries(after_settings)
    if is_own_statusline(after_settings):
        after_settings.pop("statusLine", None)
        plan.settings_changes.append("Remove the claudeglass statusline.")

    if plan.settings_changes:
        after = json.dumps(after_settings, indent=2, ensure_ascii=False) + "\n"
        plan.new_settings_text = after
        plan.old_settings_text = before
        plan.settings_diff = "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile="settings.json (now)",
                tofile="settings.json (after)",
            )
        )
    return plan


def remove_settings_entries(plan: UninstallPlan, *, now: datetime | None = None) -> Path:
    """Write ``plan``'s ``settings.json`` after backing the file up to
    ``settings.json.bak-<UTC timestamp>``. Returns the backup path."""
    if plan.new_settings_text is None:
        raise ValueError("nothing to remove")
    hook_health.check_unchanged(plan.settings_path, plan.old_settings_text)
    now = now or datetime.now(timezone.utc)
    backup = hook_health.backup_path(plan.settings_path, now)
    shutil.copy2(plan.settings_path.resolve(), backup)
    hook_health.replace_settings(plan.settings_path, plan.new_settings_text, plan.old_settings_text)
    return backup


def delete_data(config_dir: str | Path) -> list[str]:
    """Delete this tool's data folder. Returns the paths that could not
    be removed (a database still open by a running dashboard, say)."""
    failures: list[str] = []

    def _onerror(_func, path, exc_info):
        failures.append(f"{path}: {exc_info[1]}")

    shutil.rmtree(Path(config_dir), onerror=_onerror)
    return failures


__all__ = [
    "EXPECTATIONS",
    "FootprintItem",
    "UNINSTALL_COMMAND",
    "UninstallPlan",
    "capture_setting",
    "delete_data",
    "expectations",
    "feedback_skill_path",
    "feedback_skill_state",
    "home_label",
    "inventory",
    "is_own_feedback_skill",
    "is_own_skill",
    "is_own_statusline",
    "old_skill",
    "plan_uninstall",
    "read_feedback_skill",
    "read_skill",
    "remove_feedback_skill",
    "remove_skill",
    "remove_settings_entries",
    "rename_skill",
    "write_feedback_skill",
    "write_skill",
]

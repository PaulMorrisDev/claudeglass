"""Review the skills Claude Code lists to Claude: what each one is, how
much of every session its listing takes, whether Claude ever uses it,
and how to hide the ones you don't need.

Claude Code sends a line per skill -- its name and description -- at the
start of each session and each subagent (the ``skill_listing``
attachment). Usage comes from ``ReportModel.context_files`` (names and
sizes only). The descriptions are read on request from the newest
transcripts' own listing, and never stored.

Where a skill comes from:

- ``plugin``: the name has a ``plugin:`` prefix;
- ``user`` / ``project``: a ``SKILL.md`` under ``~/.claude/skills/<name>/``
  or a project's ``.claude/skills/<name>/``;
- ``command``: a ``.claude/commands/<name>.md`` file;
- ``workflow``: a saved ``.claude/workflows/<name>.js`` script;
- ``built-in``: anything else (shipped with Claude Code);
- ``removed``: no file on disk, and last listed more than
  :data:`REMOVED_AFTER_DAYS` before the newest listing in the window: a
  skill you deleted, or one Claude Code no longer lists. It gets no
  fixes, since hiding it would save nothing from now on.

The lever is the ``skillOverrides`` setting (documented values ``on``,
``name-only``, ``user-invocable-only``, ``off``), made through the usual
``apply --set`` command or a prompt; for your own skills,
``disable-model-invocation: true`` in the ``SKILL.md`` frontmatter does
the same from the file.

A skill that ``~/.claude/settings.json`` already hides (a ``skillOverrides``
value other than ``on``, or its plugin turned off in ``enabledPlugins``)
is marked ``hidden`` and gets no fix: its listings in the window predate
the change, and offering ``user-invocable-only`` for a skill set to
``off`` would show it again. The settings file is read now, never stored.

A built-in skill that one of Claude Code's own tools tells Claude to load
(:data:`TOOL_LOADED_SKILLS`: the Artifact tool's page-writing skills, the
Workflow tool's script reference) is never offered for hiding: hidden,
the tool's instructions point at a skill Claude can't load. Unused in the
window only means that tool wasn't used; such a skill is marked ``needed
by a tool`` and offered ``name-only`` instead, since the tool names it.

A report limited to one project (``only``) saw only that project's
sessions, and a skill Claude never used there may be used in another. So
its changes go in that project's ``.claude/settings.local.json``
(``project-local``), never your user settings; the frontmatter route is
offered only for the project's own skills, whose file no other project
reads; and the project's settings files count for ``hidden`` too.
Limited to several projects, or to one whose folder isn't known, nothing
is offered for hiding.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from . import fixes as fixes_mod
from .context_files import _parse_ts
from .footprint import home_label
from .model import Recommendation, SettingChange
from .units import Units

#: Duplicated per this package's small-constant convention: a skill's
#: listing line is sized at the uncalibrated characters per token.
_CHARS_PER_TOKEN_APPROX = 4

#: Listed in at least this many sessions or spawns with no use before a
#: skill counts as unused.
UNUSED_MIN_LISTINGS = 3
#: A listing line this long (tokens) is worth shortening.
LONG_DESCRIPTION_TOKENS = 100
#: A skill with no file on disk that hasn't been listed for this many
#: days before the newest listing counts as removed.
REMOVED_AFTER_DAYS = 14
#: Newest transcripts read for descriptions, at most.
_MAX_TRANSCRIPTS = 20
#: skillOverrides values that already keep a skill's description out of
#: the listing.
_HIDING_OVERRIDES = frozenset({"off", "user-invocable-only", "name-only"})
#: Built-in skills that Claude Code's own tools tell Claude to load, and
#: the tool. Tool descriptions aren't in transcripts, so this is kept by
#: hand from Claude Code's current ones.
TOOL_LOADED_SKILLS = {
    "artifact-design": "Artifact",
    "artifact-capabilities": "Artifact",
    "artifact-diagramming": "Artifact",
    "workshop": "Artifact",
    "workflow-authoring": "Workflow",
}

SOURCE_LABELS = {
    "plugin": "From a plugin",
    "user": "Your skill (all projects)",
    "project": "Project skill",
    "command": "Custom command",
    "workflow": "Saved workflow",
    "built-in": "Built into Claude Code",
    "removed": "Removed",
}


def _claude_root(config_dir: Path) -> Path:
    return Path(config_dir).parent


def descriptions(claude_root: Path, names: set[str] | None = None) -> dict[str, str]:
    """``{name: description}`` from the newest transcripts' skill
    listings, newest first. Read now, never stored."""
    found: dict[str, str] = {}
    projects = claude_root / "projects"
    try:
        transcripts = sorted(projects.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return found
    for transcript in transcripts[:_MAX_TRANSCRIPTS]:
        for content, listed in _listings(transcript):
            for line in content.splitlines():
                if not line.startswith("- "):
                    continue
                # The longest listed name that the line starts with, so a
                # plugin name with a colon is read whole.
                match = max(
                    (n for n in listed if line.startswith(f"- {n}: ") or line == f"- {n}:"), key=len, default=None
                )
                if match and match not in found:
                    found[match] = line[len(match) + 4 :].strip()
        if names is not None and names <= set(found):
            break
    return found


def _listings(transcript: Path):
    try:
        with transcript.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if '"skill_listing"' not in line:
                    continue
                try:
                    attachment = json.loads(line).get("attachment") or {}
                except (ValueError, AttributeError):
                    continue
                content, names = attachment.get("content"), attachment.get("names")
                if isinstance(content, str) and isinstance(names, list):
                    yield content, [n for n in names if isinstance(n, str)]
    except OSError:
        return


def locate(name: str, claude_root: Path, projects: list[Path]) -> tuple[str, Path | None]:
    """Where skill ``name`` comes from, and its file when it has one."""
    if ":" in name:
        return "plugin", None
    for root, source in [(claude_root, "user")] + [(p / ".claude", "project") for p in projects]:
        skill = root / "skills" / name / "SKILL.md"
        if skill.is_file():
            return source, skill
    for root in [claude_root] + [p / ".claude" for p in projects]:
        command = root / "commands" / f"{name}.md"
        if command.is_file():
            return "command", command
    for root in [claude_root] + [p / ".claude" for p in projects]:
        workflow = root / "workflows" / f"{name}.js"
        if workflow.is_file():
            return "workflow", workflow
    return "built-in", None


def _settings_file(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _settings(claude_root: Path, project: Path | None) -> dict:
    """The user settings, with ``project``'s shared and local settings
    over them for the two keys :func:`hidden_by` reads."""
    settings = _settings_file(claude_root / "settings.json")
    if project is None:
        return settings
    for name in ("settings.json", "settings.local.json"):
        layer = _settings_file(project / ".claude" / name)
        for key in ("skillOverrides", "enabledPlugins"):
            below = settings.get(key) if isinstance(settings.get(key), dict) else {}
            if isinstance(layer.get(key), dict):
                settings = {**settings, key: {**below, **layer[key]}}
    return settings


def project_folders_for(claude_root: Path, slugs) -> tuple[Path, ...]:
    """The folder each project slug stands for, from the ``cwd`` of its
    newest transcripts under ``projects/<slug>/``; a slug with none is
    left out. Read on request, never stored."""
    from .claude_md_review import _first_cwd

    folders = []
    for slug in slugs:
        try:
            transcripts = sorted(
                (claude_root / "projects" / slug).glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
            )
        except OSError:
            continue
        for transcript in transcripts[:3]:
            cwd = _first_cwd(transcript, 60)
            if cwd and Path(cwd).is_dir():
                folders.append(Path(cwd))
                break
    return tuple(folders)


def hidden_by(name: str, source: str, settings: dict) -> str:
    """Why the user settings already keep ``name`` out of Claude's
    listing, as a phrase, or ``""`` when they don't."""
    overrides = settings.get("skillOverrides")
    value = overrides.get(name) if isinstance(overrides, dict) else None
    if value in _HIDING_OVERRIDES:
        return f"skillOverrides sets it to {value}"
    plugins = settings.get("enabledPlugins")
    if source == "plugin" and isinstance(plugins, dict):
        plugin = name.split(":", 1)[0]
        states = [enabled for key, enabled in plugins.items() if str(key).split("@", 1)[0] == plugin]
        if states and not any(states):
            return "its plugin is turned off"
    return ""


@dataclass(slots=True)
class SkillRow:
    name: str
    description: str
    source: str
    path: Path | None
    usage: dict
    hidden: str = ""
    #: The Claude Code tool that tells Claude to load this skill, or "".
    needed_by: str = ""


def _listed_total(usage: dict) -> int:
    return sum((usage.get("listed") or {}).values())


def _reach_text(reach: dict) -> str:
    parts = []
    if reach.get("main"):
        parts.append(f"{reach['main']} main session{'s' if reach['main'] != 1 else ''}")
    for agent, count in sorted(reach.items(), key=lambda kv: -kv[1]):
        if agent != "main":
            parts.append(f"{count} {agent} spawn{'s' if count != 1 else ''}")
    return ", ".join(parts) or "none in this window"


def _amount(units: Units, usd: float, period: str, *, prefix: str = "") -> str:
    amount = units.money(usd, period=period)
    if amount is None:
        return ""
    # UX-2: Amount.phrase avoids "about about X% of your weekly usage
    # limit" -- a subscription's own share text already opens with
    # "about" (units.Units.money), so a plain f"{prefix}{...}"
    # concatenation would otherwise double it (finding F3).
    return amount.phrase(prefix)


def _saving(units: Units, usd: float, period: str) -> str:
    """An amount as a sentence for a fix's "Expected effect"."""
    text = _amount(units, usd, period)
    return f"{text[:1].upper()}{text[1:]}." if text else ""


def _listed_at(usage: dict) -> float | None:
    """When a skill was last listed, as a timestamp, or ``None``."""
    moment = _parse_ts(usage.get("last_seen"))
    return moment.timestamp() if moment is not None else None


def build_rows(
    config_dir: Path, context_files: dict, *, projects: list[Path] | None = None, only: tuple[Path, ...] | None = None
) -> list[SkillRow]:
    from .claude_md_review import project_folders, split_worktrees

    claude_root = _claude_root(config_dir)
    folders = split_worktrees(projects if projects is not None else project_folders(claude_root))[0]
    usage = {row["name"]: row for row in context_files.get("skills") or () if isinstance(row, dict)}
    texts = descriptions(claude_root, set(usage))
    settings = _settings(claude_root, _one(only))
    newest = max((t for t in map(_listed_at, usage.values()) if t is not None), default=None)
    rows = []
    for name in sorted(set(usage) | set(texts), key=str.lower):
        source, path = locate(name, claude_root, folders)
        listed_at = _listed_at(usage.get(name, {}))
        if source == "built-in" and listed_at is not None and newest - listed_at > REMOVED_AFTER_DAYS * 86400:
            source = "removed"
        rows.append(
            SkillRow(
                name=name,
                description=texts.get(name, ""),
                source=source,
                path=path,
                usage=usage.get(name, {}),
                hidden=hidden_by(name, source, settings),
                needed_by=TOOL_LOADED_SKILLS.get(name, "") if source == "built-in" else "",
            )
        )
    return rows


def _one(only: tuple[Path, ...] | None) -> Path | None:
    """The project a report limited to one saw, or ``None``."""
    return only[0] if only is not None and len(only) == 1 else None


def _hide_scope(only: tuple[Path, ...] | None) -> str | None:
    """Where a change for a skill Claude never used goes: your user
    settings when the report saw every project, the one project's local
    settings when it saw that project, ``None`` (nowhere) otherwise."""
    if only is None:
        return "user"
    return "project-local" if _one(only) is not None else None


def _never_used(row: SkillRow) -> bool:
    u = row.usage
    return _listed_total(u) >= UNUSED_MIN_LISTINGS and not u.get("invoked") and not u.get("attributed_turns")


def _unused(row: SkillRow) -> bool:
    """Never used, still listed, and safe to hide from Claude."""
    return not row.hidden and not row.needed_by and row.source != "removed" and _never_used(row)


def _name_only_fix(row: SkillRow, units: Units, period: str, scope: str) -> dict:
    """``name-only`` for a skill a Claude Code tool loads by name: its
    description goes, the name the tool points at stays."""
    u = row.usage
    tokens = int(u.get("listing_tokens") or 0)
    kept = round(len(f"- {row.name}") / _CHARS_PER_TOKEN_APPROX)
    saving = float(u.get("listing_cost_usd") or 0.0) * (1 - kept / tokens) if tokens > kept else 0.0
    rec = Recommendation(
        id="tool-skill-name-only",
        title=f"List {row.name} by name only",
        scope=scope,
        why=(
            f"Claude Code listed {row.name} in {_reach_text(u.get('listed') or {})} and Claude never used it, "
            f"but Claude Code's {row.needed_by} tool tells Claude to load it, so hiding it would break that "
            f"tool's instructions. Listed by name only, it drops its {tokens}-token description."
        ),
        estimated_saving=_saving(units, saving, period),
        saving_basis="Its description, written to the cache at each start and read back on every turn.",
        changes=[
            SettingChange(
                key="skillOverrides",
                value={row.name: "name-only"},
                note=f"The {row.needed_by} tool names this skill when Claude should load it, so Claude still can.",
            )
        ],
    )
    fix = fixes_mod.build_fix(rec, rec.changes[0])
    fix["title"] = "List it by name only"
    return fix


def build_fixes(row: SkillRow, units: Units, period: str, *, only: tuple[Path, ...] | None = None) -> list[dict]:
    out: list[dict] = []
    if row.hidden or row.source == "removed":
        return out
    scope = _hide_scope(only)
    if row.needed_by and _never_used(row):
        return [_name_only_fix(row, units, period, scope)] if scope else []
    u = row.usage
    listing_cost = float(u.get("listing_cost_usd") or 0.0)
    listed = _reach_text(u.get("listed") or {})
    if _unused(row) and scope:
        saving = _saving(units, listing_cost, period)
        rec = Recommendation(
            id="unused-skill",
            title=f"Hide {row.name} from Claude",
            scope=scope,
            why=(
                f"Claude Code listed {row.name} in {listed} and Claude never used it, but every listing "
                f"carries its {u.get('listing_tokens', 0)}-token description."
            ),
            estimated_saving=saving,
            saving_basis="Its listing line, written to the cache at each start and read back on every turn.",
            changes=[
                SettingChange(
                    key="skillOverrides",
                    value={row.name: "user-invocable-only"},
                    note=(
                        "Plugin skills are listed under their plugin:skill name; Claude Code's docs don't say "
                        "which name skillOverrides expects for them, so check /skills after the change."
                        if row.source == "plugin"
                        else ""
                    ),
                )
            ],
        )
        fix = fixes_mod.build_fix(rec, rec.changes[0])
        fix["title"] = "Hide it from Claude, keep it in your / menu"
        out.append(fix)
        # Your own skill's file reaches every project, not only the one seen.
        if row.path is not None and (row.source == "project" or (row.source == "user" and only is None)):
            out.append(
                {
                    "key": None,
                    "agent": None,
                    "title": "Or turn off automatic use in the skill's own file",
                    "explainer": [],
                    "command": None,
                    "command_warning": "",
                    "prompt": (
                        f"In {home_label(row.path)}, add `disable-model-invocation: true` to the frontmatter. "
                        f"Why: Claude never used the {row.name} skill on its own, and its description is sent "
                        "at the start of every session and subagent. With this set, only I can start it, with "
                        f"/{row.name}. Show me the diff before saving. Change nothing else. " + fixes_mod.PROMPT_RESTART
                    ),
                }
            )
    tokens = int(u.get("listing_tokens") or 0)
    if tokens >= LONG_DESCRIPTION_TOKENS and not _unused(row):
        if row.path is not None and row.source in ("user", "project"):
            prompt = (
                f"The description of the {row.name} skill in {home_label(row.path)} takes about {tokens} tokens, "
                f"and Claude Code sends it at the start of every session and subagent ({listed}). Rewrite the "
                "`description` (and `when_to_use`, if set) in the frontmatter to one or two sentences: what "
                "the skill does and when to use it, key trigger words first. Keep anything Claude needs to "
                "decide when to use it; move detail into the body of SKILL.md, which loads only when the skill "
                "runs. Show me the diff before saving. Change nothing else. " + fixes_mod.PROMPT_RESTART
            )
        else:
            prompt = (
                f"The {row.name} skill's description takes about {tokens} tokens of every session. I can't "
                "edit it (it ships with "
                + ("a plugin" if row.source == "plugin" else "Claude Code")
                + f"). Add \"{row.name}\": \"name-only\" to skillOverrides in ~/.claude/settings.json, so "
                "it is listed by name only. Why: a shorter listing at the start of every session. Show me the "
                "diff before saving. Claude Code will ask my permission to edit files under .claude; that is "
                "expected. Change nothing else. " + fixes_mod.PROMPT_RESTART
            )
        out.append(
            {
                "key": None,
                "agent": None,
                "title": "Shorten its description",
                "explainer": [
                    ["What this changes", "The skill's listing line: the description Claude reads to decide when to use it."],
                    ["Now and after", f"Now: about {tokens:,} tokens. After: one or two sentences, about 30 to 50 tokens."],
                    ["Where and who it affects", "Every session and subagent that lists the skill."],
                    [
                        "Expected effect",
                        (f"Up to {_amount(units, listing_cost * 0.6, period)}." if listing_cost else "A shorter listing."),
                    ],
                    ["Trade-off", "Too short a description and Claude may stop using the skill when it should."],
                    ["How to undo it", "Put the old description back; Claude shows the diff before saving."],
                ],
                "command": None,
                "command_warning": "",
                "prompt": prompt,
            }
        )
    return out


def row_dict(row: SkillRow, units: Units, period: str, *, only: tuple[Path, ...] | None = None) -> dict:
    u = row.usage
    status = "unused" if _unused(row) else ("used" if u.get("invoked") or u.get("attributed_turns") else "")
    if row.needed_by and _never_used(row):
        status = "needed by a tool"
    if row.source == "removed":
        status = "no longer listed"
    if row.hidden:
        status = "hidden"
    if not status:
        status = "not listed" if not u else "listed"
    return {
        "name": row.name,
        "description": row.description,
        "source": row.source,
        "source_label": SOURCE_LABELS.get(row.source, row.source),
        "path": home_label(row.path) if row.path else "",
        "listing_tokens": int(u.get("listing_tokens") or 0),
        "listed": u.get("listed") or {},
        "listed_text": _reach_text(u.get("listed") or {}),
        "invoked": int(u.get("invoked") or 0),
        "invoked_by": u.get("invoked_by") or {},
        "listing_cost_usd": float(u.get("listing_cost_usd") or 0.0),
        "listing_cost_text": _amount(units, float(u.get("listing_cost_usd") or 0.0), period),
        "use_cost_text": _amount(units, float(u.get("attributed_cost_usd") or 0.0), period),
        "resent_tokens": int(u.get("resent_tokens") or 0),
        "status": status,
        "use_text": _use_text(u),
        "hidden": row.hidden,
        "needed_by": row.needed_by,
        "fixes": build_fixes(row, units, period, only=only),
    }


def _use_text(u: dict) -> str:
    """How the skill was used in the window, as a phrase."""
    started, turns = int(u.get("invoked") or 0), int(u.get("attributed_turns") or 0)
    parts = []
    if started:
        parts.append(f"started {started} time{'s' if started != 1 else ''}")
    if turns:
        parts.append(f"{turns} turn{'s' if turns != 1 else ''} ran under it")
    return ", ".join(parts) if parts else "never used"


def review(
    config_dir: Path,
    context_files: dict,
    units: Units,
    period: str,
    *,
    projects: list[Path] | None = None,
    only: tuple[Path, ...] | None = None,
) -> dict:
    """``only``: the project folders the report was limited to, or
    ``None`` when it saw every project (see the module docstring)."""
    rows = [
        row_dict(row, units, period, only=only)
        for row in build_rows(config_dir, context_files, projects=projects, only=only)
    ]
    rows.sort(key=lambda r: (r["status"] != "unused", -r["listing_cost_usd"], r["name"].lower()))
    total_cost = sum(r["listing_cost_usd"] for r in rows)
    unused = [r for r in rows if r["status"] == "unused"]
    kept = [r for r in rows if r["status"] == "needed by a tool"]
    return {
        "period": period,
        "skills": rows,
        "listing_tokens": sum(r["listing_tokens"] for r in rows),
        "listing_cost_text": _amount(units, total_cost, period),
        "unused": len(unused),
        "needed_by_a_tool": len(kept),
        "fixes": _hide_all_fix(unused, units, period, kept=kept, scope=_hide_scope(only)),
        "limited_text": limited_text(only),
    }


def kept_text(kept: list[dict]) -> str:
    """Why skills Claude never used aren't in the hide-all list, as a
    sentence, or ``""``."""
    if not kept:
        return ""
    tools = sorted({r["needed_by"] for r in kept})
    return (
        f"Left out: {', '.join(r['name'] for r in kept)}. Claude Code's own "
        f"{' and '.join(tools)} {'tools tell' if len(tools) != 1 else 'tool tells'} Claude to load "
        f"{'them' if len(kept) != 1 else 'it'}, so hidden, "
        f"{'those tools' if len(tools) != 1 else 'that tool'} would break."
    )


def _hide_all_fix(
    unused: list[dict], units: Units, period: str, *, kept: list[dict] = (), scope: str | None = "user"
) -> list[dict]:
    """One change that hides every unused skill at once, so you needn't
    copy a fix per skill."""
    if len(unused) < 2 or scope is None:
        return []
    cost = sum(r["listing_cost_usd"] for r in unused)
    tokens = sum(r["listing_tokens"] for r in unused)
    rec = Recommendation(
        id="unused-skills",
        title="Hide the skills Claude never uses",
        scope=scope,
        why=(
            f"{len(unused)} skills were listed at the start of sessions and subagents but never used, "
            f"about {tokens} tokens of every listing."
        ),
        estimated_saving=_saving(units, cost, period),
        saving_basis="Their listing lines, written to the cache at each start and read back on every turn.",
        changes=[
            SettingChange(
                key="skillOverrides",
                value={r["name"]: "user-invocable-only" for r in unused},
                note=" ".join(
                    part
                    for part in (
                        "Skills you still want Claude to pick on its own: take them out of the list first.",
                        kept_text(list(kept)),
                    )
                    if part
                ),
            )
        ],
    )
    fix = fixes_mod.build_fix(rec, rec.changes[0])
    fix["title"] = f"Hide all {len(unused)} unused skills from Claude"
    return [fix]


def render_markdown(data: dict) -> str:
    lines = ["# Skills review", ""]
    if not data["skills"]:
        return "# Skills review\n\nNo skill listings found in this window.\n"
    cost_text = data["listing_cost_text"]
    # UX-2: cost_text is already a rendered string here (not an Amount),
    # so Amount.phrase's dedup is redone by hand -- a subscription's own
    # text already opens with "about" (units.Units.money), which would
    # otherwise double into "about about X%..." (finding F3).
    cost_clause = ""
    if cost_text:
        cost_clause = f", {cost_text}" if cost_text.lower().startswith("about ") else f", about {cost_text}"
    lines.append(
        f"Skill listings take about {data['listing_tokens']} tokens at the start of each session and subagent"
        + cost_clause + "."
    )
    if data.get("limited_text"):
        lines += ["", data["limited_text"]]
    lines.append("")
    for fix in data.get("fixes") or ():
        lines += _fix_markdown(fix, "##")
    for row in data["skills"]:
        lines.append(f"## {row['name']} ({row['source_label']})")
        lines.append("")
        if row["description"]:
            lines.append(row["description"])
            lines.append("")
        lines.append(f"About {row['listing_tokens']} tokens; listed in {row['listed_text']}; {row['use_text']}.")
        if row["hidden"]:
            lines.append(f"Already hidden: {row['hidden']}.")
        elif row["status"] == "needed by a tool":
            lines.append(
                f"Not offered for hiding: Claude Code's {row['needed_by']} tool tells Claude to load it."
            )
        elif row["status"] == "no longer listed":
            lines.append("No longer listed, and no file for it is left on disk, so hiding it would save nothing.")
        for fix in row["fixes"]:
            lines += _fix_markdown(fix, "###")
        lines.append("")
    return "\n".join(lines)


def limited_text(only: tuple[Path, ...] | None) -> str:
    """Where a review limited to some projects hides skills, as a
    sentence, or ``""`` for one that saw every project."""
    if only is None:
        return ""
    if _one(only) is not None:
        return (
            f"Only {home_label(_one(only))} was read, and a skill Claude never used there may be used in your "
            "other projects, so it is hidden in that project only (.claude/settings.local.json)."
        )
    return "Only some projects were read, and a skill may be used in the others, so none is offered for hiding."


def _fix_markdown(fix: dict, heading: str) -> list[str]:
    lines = ["", f"{heading} {fix.get('title') or 'Fix'}", "", "```text", fix["prompt"], "```"]
    if fix.get("command"):
        lines += ["", "Or run:", "", "```bash", fix["command"], "```"]
    return lines + [""]


__all__ = [
    "LONG_DESCRIPTION_TOKENS",
    "REMOVED_AFTER_DAYS",
    "SOURCE_LABELS",
    "TOOL_LOADED_SKILLS",
    "UNUSED_MIN_LISTINGS",
    "descriptions",
    "hidden_by",
    "kept_text",
    "limited_text",
    "locate",
    "project_folders_for",
    "render_markdown",
    "review",
]

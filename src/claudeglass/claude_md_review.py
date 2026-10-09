"""Review your CLAUDE.md files: how big each one is, how often Claude
Code sends it, what that costs, and what could be trimmed, moved or
fixed, with a ready-to-copy prompt for each change.

Files are read from disk when you ask for a review and never stored.
The candidates are:

- your user file, ``~/.claude/CLAUDE.md``, and ``~/.claude/rules/*.md``;
- for each project you have used Claude Code in, or only the project a
  report is limited to (its folder is read from the newest transcript's
  ``cwd`` field): ``CLAUDE.md``,
  ``.claude/CLAUDE.md``, ``CLAUDE.local.md``, ``.claude/rules/**/*.md``,
  ``CLAUDE.md`` files in subfolders (a bounded walk) and in parent
  folders, and the project's auto memory ``MEMORY.md``;
- files pulled in with ``@path`` imports, one level deep.

Each file is matched to what transcripts recorded about it
(``ReportModel.context_files``) by the salted path hash
(``parse.path_hash``), so how often it was sent, to which agents, and the
estimated cost come from real sessions. A file never seen in a
transcript in the window is still listed, with "not seen" usage.

Findings per file:

- **sections**: size of each heading section;
- **agent-specific sections**: a section whose heading names one of your
  agents (``.claude/agents/*.md`` or an agent type seen in your sessions),
  or whose text names it twice -- every main session and subagent that
  gets the file pays for it, but only that agent needs it. A one-word
  name (``claude``, ``Explore``, ``Plan``) counts only in backticks or
  next to "agent", since it is also ordinary prose or a path;
- **duplicates**: paragraphs repeated within the file or across files;
- **stale references**: backticked paths with a folder in them, ``@``
  imports and ``npm run`` scripts that no longer exist.

Actions are fix dicts in :func:`fixes.build_fix`'s shape (``title``,
``explainer``, ``prompt``; ``command`` is always ``None`` because the
change is an edit to your own words, not a setting). The dashboard never
edits a file itself.

Phase 8a adds the other files your agents consume (``context_files``):
:func:`local_names` hashes the files under the project folders with the
same salt transcripts used, so a hash from a transcript gets the path from
its project folder on the dashboard (never stored, never exported), and
finds the files a CLAUDE.md imports with ``@path``. :func:`project_file_fixes`
drafts the prompts for a big, wide or growing file.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from . import pages
from . import parse as parse_mod
from .fixes import PROMPT_RESTART
from .footprint import home_label
from .units import Units

#: Duplicated per this package's small-constant convention.
_CHARS_PER_TOKEN_APPROX = 4

#: A file this big (tokens) is worth trimming even without cost data.
TRIM_TOKENS = 1500
#: A section this big (tokens) is worth loading on demand.
ON_DEMAND_SECTION_TOKENS = 300
#: Paragraphs shorter than this (characters, normalised) are not
#: compared for duplicates: short lines repeat by chance.
_MIN_DUPLICATE_CHARS = 60

#: Subfolder walk bounds, so a review of a large repository stays fast.
_WALK_MAX_DEPTH = 4
_WALK_MAX_DIRS = 4000
_WALK_SKIP = frozenset(
    {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".next", "target", ".tox", ".mypy_cache"}
)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
_IMPORT_RE = re.compile(r"(?:^|\s)@((?:~|\.{1,2}|/|[A-Za-z]:)?[\w./\\~:-]+\.\w+)")
_NPM_RUN_RE = re.compile(r"\b(?:npm|pnpm|yarn)\s+run\s+([\w:.-]+)")

#: Level labels, in the order the review lists files.
LEVELS = (
    "User",
    "User rule",
    "Parent folder",
    "Project",
    "Project (.claude folder)",
    "Local (not shared)",
    "Project rule",
    "Subfolder",
    "Auto memory",
    "Import",
)

_LEVEL_WHO = {
    "User": "you, in every project",
    "User rule": "you, in every project",
    "Parent folder": "every project under that folder",
    "Project": "everyone who works in this project",
    "Project (.claude folder)": "everyone who works in this project",
    "Local (not shared)": "you, in this project only",
    "Project rule": "everyone who works in this project",
    "Subfolder": "sessions that read files in that folder",
    "Auto memory": "you, in this project only",
    "Import": "whoever gets the file that imports it",
}


@dataclass(slots=True)
class Section:
    heading: str
    level: int
    line: int
    chars: int
    agents: list[str] = field(default_factory=list)

    @property
    def tokens(self) -> int:
        return round(self.chars / _CHARS_PER_TOKEN_APPROX)


@dataclass(slots=True)
class Candidate:
    path: Path
    level: str
    project_root: Path | None = None
    scoped: bool = False


@dataclass(slots=True)
class FileReview:
    id: str
    path: Path
    level: str
    project: str
    chars: int
    scoped: bool
    sections: list[Section]
    imports: list[str]
    duplicates: list[dict] = field(default_factory=list)
    stale: list[dict] = field(default_factory=list)
    usage: dict | None = None

    @property
    def tokens(self) -> int:
        return round(self.chars / _CHARS_PER_TOKEN_APPROX)


# -- finding files -----------------------------------------------------


def _claude_root(config_dir: Path) -> Path:
    return Path(config_dir).parent


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _has_paths_frontmatter(text: str) -> bool:
    """A rule file with ``paths:`` in its frontmatter loads only when
    Claude works on a matching file."""
    if not text.startswith("---"):
        return False
    end = text.find("\n---", 3)
    return end > 0 and re.search(r"^paths\s*:", text[3:end], re.MULTILINE) is not None


def project_folders(claude_root: Path, *, max_lines: int = 60) -> list[Path]:
    """Each project folder Claude Code ran in, from the ``cwd`` field of
    the newest transcript in each ``projects/<slug>/`` folder. Read on
    request, never stored."""
    folders: dict[str, Path] = {}
    projects = claude_root / "projects"
    try:
        slug_dirs = [entry for entry in projects.iterdir() if entry.is_dir()]
    except OSError:
        return []
    for slug_dir in slug_dirs:
        try:
            transcripts = sorted(slug_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            continue
        for transcript in transcripts[:3]:
            cwd = _first_cwd(transcript, max_lines)
            if cwd:
                path = Path(cwd)
                if path.is_dir():
                    folders.setdefault(os.path.normcase(str(path)), path)
                break
    return sorted(folders.values(), key=lambda p: str(p).lower())


def _worktree_parts(path: Path) -> tuple[Path, Path] | None:
    """``(main project, worktree)`` when ``path`` is a git worktree
    Claude Code made under ``<project>/.claude/worktrees/<name>``."""
    parts = path.parts
    for index in range(len(parts) - 2):
        if parts[index] == ".claude" and parts[index + 1] == "worktrees":
            return Path(*parts[:index]), Path(*parts[: index + 3])
    return None


def split_worktrees(folders: list[Path]) -> tuple[list[Path], dict[str, list[Path]]]:
    """Main project folders, and each one's worktrees. A worktree holds
    a copy of the same CLAUDE.md files, so it is reviewed as its main
    project, with the worktree copies' usage added in."""
    mains: dict[str, Path] = {}
    worktrees: dict[str, list[Path]] = {}
    for folder in folders:
        split = _worktree_parts(folder)
        main = split[0] if split else folder
        key = os.path.normcase(str(main))
        mains.setdefault(key, main)
        if split:
            worktrees.setdefault(key, []).append(split[1])
    return sorted(mains.values(), key=lambda p: str(p).lower()), worktrees


def _first_cwd(transcript: Path, max_lines: int) -> str | None:
    try:
        with transcript.open(encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle):
                if index >= max_lines:
                    break
                if '"cwd"' not in line:
                    continue
                try:
                    value = json.loads(line).get("cwd")
                except (ValueError, AttributeError):
                    continue
                if isinstance(value, str) and value:
                    return value
    except OSError:
        return None
    return None


def _memory_file(claude_root: Path, project: Path) -> Path:
    """Where Claude Code keeps a project's auto memory index: the
    project folder with every non-alphanumeric character as ``-``."""
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(project))
    return claude_root / "projects" / slug / "memory" / "MEMORY.md"


def _walk_subfolders(root: Path) -> list[Path]:
    found: list[Path] = []
    seen = 0
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack and seen < _WALK_MAX_DIRS:
        folder, depth = stack.pop()
        seen += 1
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                if depth < _WALK_MAX_DEPTH and entry.name not in _WALK_SKIP and not entry.name.startswith("."):
                    stack.append((Path(entry.path), depth + 1))
            elif folder != root and entry.name == "CLAUDE.md":
                found.append(Path(entry.path))
    return sorted(found)


def discover(config_dir: Path, *, projects: list[Path] | None = None) -> list[Candidate]:
    """Every CLAUDE.md-family file that exists, with its level."""
    claude_root = _claude_root(config_dir)
    home = Path.home()
    out: list[Candidate] = []
    seen: set[str] = set()

    def add(path: Path, level: str, project: Path | None = None) -> None:
        key = os.path.normcase(str(path))
        if key in seen or not path.is_file():
            return
        seen.add(key)
        scoped = level.endswith("rule") and _has_paths_frontmatter(_read(path) or "")
        out.append(Candidate(path=path, level=level, project_root=project, scoped=scoped))

    add(claude_root / "CLAUDE.md", "User")
    for rule in sorted((claude_root / "rules").rglob("*.md")) if (claude_root / "rules").is_dir() else ():
        add(rule, "User rule")
    for project in projects if projects is not None else project_folders(claude_root):
        parent = project.parent
        while parent != parent.parent and os.path.normcase(str(parent)) != os.path.normcase(str(home.parent)):
            add(parent / "CLAUDE.md", "Parent folder", project)
            parent = parent.parent
        add(project / "CLAUDE.md", "Project", project)
        add(project / ".claude" / "CLAUDE.md", "Project (.claude folder)", project)
        add(project / "CLAUDE.local.md", "Local (not shared)", project)
        rules = project / ".claude" / "rules"
        if rules.is_dir():
            for rule in sorted(rules.rglob("*.md")):
                add(rule, "Project rule", project)
        for nested in _walk_subfolders(project):
            add(nested, "Subfolder", project)
        add(_memory_file(claude_root, project), "Auto memory", project)
    for candidate in list(out):
        text = _read(candidate.path) or ""
        for target in _imports(text, candidate.path):
            if target.is_file():
                add(target, "Import", candidate.project_root)
    return out


# -- reading a file ----------------------------------------------------


def _strip_frontmatter(text: str) -> str:
    """``text`` with a leading ``---`` frontmatter block blanked out
    (line numbers kept)."""
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    if end < 0:
        return text
    close = text.find("\n", end + 4)
    close = len(text) if close < 0 else close
    return "\n" * text[:close].count("\n") + text[close:]


def _prose_lines(text: str) -> list[tuple[int, str]]:
    """``(line number, line)`` for every line outside fenced code."""
    lines: list[tuple[int, str]] = []
    in_fence = False
    for number, line in enumerate(text.splitlines(), start=1):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence:
            lines.append((number, line))
    return lines


def sections(text: str) -> list[Section]:
    """Split ``text`` at every Markdown heading outside code. Text before
    the first heading is "(top of file)"."""
    out: list[Section] = []
    in_fence = False
    current = Section(heading="(top of file)", level=0, line=1, chars=0)
    for number, line in enumerate(text.splitlines(keepends=True), start=1):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
        match = None if in_fence else _HEADING_RE.match(line.rstrip("\r\n"))
        if match:
            if current.chars or current.level:
                out.append(current)
            current = Section(heading=match.group(2) or "(untitled)", level=len(match.group(1)), line=number, chars=0)
        current.chars += len(line)
    if current.chars:
        out.append(current)
    return out


def _section_text(text: str, section_list: list[Section]) -> list[str]:
    lines = text.splitlines(keepends=True)
    starts = [s.line - 1 for s in section_list] + [len(lines)]
    return ["".join(lines[starts[i] : starts[i + 1]]) for i in range(len(section_list))]


def _imports(text: str, source: Path) -> list[Path]:
    targets: list[Path] = []
    for _number, line in _prose_lines(text):
        stripped = _BACKTICK_RE.sub("", line)
        for match in _IMPORT_RE.finditer(stripped):
            targets.append(_resolve(match.group(1), source.parent))
    return targets


def _resolve(ref: str, base: Path) -> Path:
    ref = ref.strip()
    if ref.startswith("~"):
        return Path(os.path.expanduser(ref))
    path = Path(ref)
    return path if path.is_absolute() else base / path


#: Absolute POSIX roots worth checking; any other leading ``/`` is more
#: likely a URL route or a slash command than a file.
_POSIX_ROOTS = ("/home/", "/Users/", "/etc/", "/usr/", "/var/", "/opt/", "/tmp/", "/mnt/")


def _looks_like_path(token: str) -> bool:
    if any(ch in token for ch in " *?<>{}$|\"'^()[]+=,;") or "://" in token:
        return False
    if token.startswith(("-", "@", "#", "\\")) or token.count("/") + token.count("\\") == 0:
        return False
    if token.startswith("/") and not token.startswith(_POSIX_ROOTS):
        return False
    if re.search(r"\\[bdswBDSW](?![A-Za-z])", token):
        return False  # a regex escape such as \d, not a Windows path
    return re.search(r"[A-Za-z]", token) is not None


class _ProjectIndex:
    """Every relative path in a project (bounded walk), so a reference
    written relative to a subfolder (``Shared/Foo.cs`` for
    ``src/App/Shared/Foo.cs``) still counts as found."""

    _MAX_ENTRIES = 60000

    def __init__(self, root: Path) -> None:
        self.root = root
        self._paths: str | None = None

    def _load(self) -> str:
        """Every relative path, lower-cased, each after a newline."""
        if self._paths is None:
            root = str(self.root)
            skip = len(os.path.join(root, ""))
            paths: list[str] = []
            stack = [root]
            while stack and len(paths) < self._MAX_ENTRIES:
                folder = stack.pop()
                try:
                    entries = list(os.scandir(folder))
                except OSError:
                    continue
                for entry in entries:
                    if entry.name in _WALK_SKIP or entry.name == "worktrees":
                        continue
                    paths.append(entry.path[skip:])
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
            self._paths = ("\n" + "\n".join(paths)).replace("\\", "/").lower()
        return self._paths

    def has(self, token: str) -> bool:
        wanted = token.replace("\\", "/").strip("./").lower()
        if not wanted or "xx" in wanted or "..." in wanted:
            return True  # a placeholder such as sql/0xx, not a real path
        if "\n" in wanted:
            return False  # no one path holds it; searching could match across two
        # A prefix counts too: `sql/067` names the sql/067_*.sql file.
        paths = self._load()
        return ("\n" + wanted) in paths or ("/" + wanted) in paths


#: Walked project indexes kept across calls, by project root: every load
#: of the CLAUDE.md view and of Checks asks about the same projects, and
#: walking a large one takes seconds. A reference at its exact path is
#: found by ``exists()`` before the index is asked, so a kept index only
#: delays noticing a file matched relative to a subfolder.
_INDEX_TTL_S = 120.0
_INDEXES: dict[str, tuple[float, _ProjectIndex]] = {}
_INDEXES_LOCK = threading.Lock()


def _project_index(root: Path) -> _ProjectIndex:
    now = time.monotonic()
    key = os.path.normcase(str(root))
    with _INDEXES_LOCK:
        for old_key in [k for k, (at, _) in _INDEXES.items() if now - at >= _INDEX_TTL_S]:
            del _INDEXES[old_key]
        if key not in _INDEXES:
            _INDEXES[key] = (now, _ProjectIndex(root))
        return _INDEXES[key][1]


def _stale_references(
    text: str, source: Path, project: Path | None, index: "_ProjectIndex | None" = None
) -> list[dict]:
    stale: list[dict] = []
    seen: set[str] = set()
    package_scripts = _package_scripts(project) if project is not None else None
    if project is not None and index is None:
        index = _ProjectIndex(project)
    for number, line in _prose_lines(_strip_frontmatter(text)):
        for match in _BACKTICK_RE.finditer(line):
            token = re.sub(r":\d+(?::\d+)?$", "", match.group(1).strip()).rstrip("/\\")
            if not _looks_like_path(token) or token in seen:
                continue
            candidate = Path(os.path.expanduser(token))
            if not candidate.is_absolute() and not token.startswith("~"):
                if project is None:
                    continue  # a relative path in a user-level file has no fixed base
                bases = [project, source.parent]
                if any((base / token).exists() for base in bases) or (index is not None and index.has(token)):
                    continue
            elif candidate.exists():
                continue
            seen.add(token)
            stale.append({"line": number, "reference": token, "kind": "path"})
        without_code = _BACKTICK_RE.sub("", line)
        for match in _IMPORT_RE.finditer(without_code):
            target = _resolve(match.group(1), source.parent)
            if not target.exists() and match.group(1) not in seen:
                seen.add(match.group(1))
                stale.append({"line": number, "reference": "@" + match.group(1), "kind": "import"})
        if package_scripts is not None:
            for match in _NPM_RUN_RE.finditer(line):
                script = match.group(1)
                if script not in package_scripts and script not in seen:
                    seen.add(script)
                    stale.append({"line": number, "reference": match.group(0), "kind": "script"})
    return stale


def _package_scripts(project: Path) -> set[str] | None:
    text = _read(project / "package.json")
    if text is None:
        return None
    try:
        scripts = json.loads(text).get("scripts")
    except (ValueError, AttributeError):
        return None
    return set(scripts) if isinstance(scripts, dict) else set()


def _normalise_paragraph(paragraph: str) -> str:
    text = re.sub(r"[`*_>#\-•]+", " ", paragraph.lower())
    return re.sub(r"\s+", " ", text).strip()


def _paragraphs(text: str) -> list[tuple[int, str]]:
    """``(first line, paragraph)`` for each blank-line-separated block of
    prose, list items split one per item."""
    out: list[tuple[int, str]] = []
    block: list[str] = []
    start = 0
    for number, line in _prose_lines(_strip_frontmatter(text)) + [(10**9, "")]:
        is_item = re.match(r"^\s*(?:[-*+]|\d+\.)\s+", line) is not None
        if not line.strip() or is_item or _HEADING_RE.match(line):
            if block:
                out.append((start, " ".join(block)))
            block = []
            if is_item:
                block, start = [line], number
            continue
        if not block:
            start = number
        block.append(line)
    return out


def agent_names(config_dir: Path, projects: list[Path], seen_types: list[str]) -> list[str]:
    """Agent names to look for in sections: your agent files (user and
    each project's), plus agent types seen in your sessions."""
    names: set[str] = set()
    folders = [_claude_root(config_dir) / "agents"] + [project / ".claude" / "agents" for project in projects]
    for folder in folders:
        if folder.is_dir():
            names.update(path.stem for path in folder.glob("*.md"))
    names.update(name for name in seen_types if name not in ("main", "(unknown)", "fork"))
    return sorted(names, key=str.lower)


def _mention_pattern(name: str) -> re.Pattern:
    """How a section names an agent. A name with a hyphen, underscore or
    digit (``db-migrator``) is distinctive, so any whole-word use counts.
    A plain word (``claude``, ``Explore``, ``Plan``) is also prose or a
    path ("Claude Code", ``.claude/``, "plan the change"), so it counts
    only in backticks, before "agent" or "subagent", or as a
    ``subagent_type``."""
    word = re.escape(name)
    if re.search(r"[-_\d]", name):
        return re.compile(r"(?<![\w-])" + word + r"(?![\w-])", re.IGNORECASE)
    return re.compile(
        r"`" + word + r"`"
        + r"|(?<![\w./-])" + word + r"\s+(?:sub)?agent\b(?!\s+SDK)"
        + r"|\bsubagent_type\W{1,4}" + word + r"\b",
        re.IGNORECASE,
    )


def _agents_in(heading: str, body: str, names: list[str]) -> list[str]:
    found = []
    for name in names:
        pattern = _mention_pattern(name)
        if pattern.search(heading) or len(pattern.findall(body)) >= 2:
            found.append(name)
    return found


def read_file(
    candidate: Candidate, salt: bytes, names: list[str], index: "_ProjectIndex | None" = None
) -> FileReview | None:
    text = _read(candidate.path)
    if text is None:
        return None
    section_list = sections(text)
    bodies = _section_text(text, section_list)
    for section, body in zip(section_list, bodies):
        section.agents = _agents_in(section.heading, body, names)
    return FileReview(
        id=parse_mod.path_hash(str(candidate.path), salt),
        path=candidate.path,
        level=candidate.level,
        project=candidate.project_root.name if candidate.project_root else "",
        chars=len(text),
        scoped=candidate.scoped,
        sections=section_list,
        imports=[str(target) for target in _imports(text, candidate.path)],
        stale=_stale_references(text, candidate.path, candidate.project_root, index),
    )


def _find_duplicates(reviews: list[FileReview]) -> None:
    by_hash: dict[str, list[tuple[FileReview, int, str]]] = {}
    for review in reviews:
        text = _read(review.path) or ""
        for line, paragraph in _paragraphs(text):
            normalised = _normalise_paragraph(paragraph)
            if len(normalised) < _MIN_DUPLICATE_CHARS:
                continue
            digest = hashlib.sha256(normalised.encode("utf-8")).hexdigest()
            by_hash.setdefault(digest, []).append((review, line, paragraph.strip()))
    for places in by_hash.values():
        if len(places) < 2:
            continue
        for review, line, paragraph in places:
            others = [
                {"file": home_label(other.path), "line": other_line}
                for other, other_line, _p in places
                if not (other is review and other_line == line)
            ]
            review.duplicates.append(
                {
                    "line": line,
                    "excerpt": _excerpt(paragraph),
                    "tokens": round(len(paragraph) / _CHARS_PER_TOKEN_APPROX),
                    "also_in": others,
                }
            )


def _excerpt(text: str, limit: int = 90) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# -- the review ----------------------------------------------------------


def _reach_text(usage: dict | None) -> str:
    if not usage:
        return "not seen in your sessions in this window"
    reach = usage.get("reach") or {}
    parts = []
    main = reach.get("main")
    if main:
        parts.append(f"{main} main session{'s' if main != 1 else ''}")
    for agent, count in sorted(reach.items(), key=lambda kv: -kv[1]):
        if agent != "main":
            parts.append(f"{count} {agent} spawn{'s' if count != 1 else ''}")
    return ", ".join(parts) or "not seen in your sessions in this window"


def _amount(units: Units, usd: float, period: str, *, prefix: str = "") -> str:
    amount = units.money(usd, period=period)
    if amount is None:
        return "too small to measure"
    # UX-2: Amount.phrase avoids "About about X% of your weekly usage
    # limit" -- a subscription's own share text already opens with
    # "about" (units.Units.money), so a plain f"{prefix}{...}"
    # concatenation would otherwise double it (finding F3).
    return amount.phrase(prefix)


def _explainer(what: str, now_after: str, review: FileReview, effect: str, tradeoff: str) -> list[list[str]]:
    return [
        ["What this changes", what],
        ["Now and after", now_after],
        ["Where and who it affects", f"{home_label(review.path)}: {_LEVEL_WHO.get(review.level, 'sessions that load it')}."],
        ["Expected effect", effect],
        ["Trade-off", tradeoff],
        [
            "How to undo it",
            "Claude shows the diff before saving. To go back, restore the file from git or ask Claude to undo the edit.",
        ],
    ]


def _fix(title: str, explainer: list[list[str]], prompt: str) -> dict:
    return {
        "key": None,
        "agent": None,
        "title": title,
        "explainer": explainer,
        "command": None,
        "command_warning": "",
        "prompt": prompt,
    }


_PROMPT_TAIL = (
    "Before saving, list what you will remove or move and show me the diff. Keep every rule that changes "
    "how you work; only cut what you can work out from the code or what repeats. Change nothing else. "
    + PROMPT_RESTART
)


def _largest(section_list: list[Section], count: int = 5) -> list[Section]:
    return sorted((s for s in section_list if s.tokens), key=lambda s: -s.tokens)[:count]


def build_fixes(review: FileReview, units: Units, period: str) -> list[dict]:
    fixes: list[dict] = []
    path = home_label(review.path)
    usage = review.usage or {}
    cost = float(usage.get("cost_usd") or 0.0)
    reach = _reach_text(review.usage)
    per_token = cost / review.tokens if review.tokens else 0.0

    agent_sections = [s for s in review.sections if s.agents]
    if agent_sections:
        moved = sum(s.tokens for s in agent_sections)
        lines = [
            f'- "{s.heading}" (line {s.line}, about {s.tokens:,} tokens), for {", ".join(s.agents)}'
            for s in agent_sections
        ]
        saving = per_token * moved
        fixes.append(
            _fix(
                "Move agent-only sections into agent files",
                _explainer(
                    "Sections that only one agent needs move out of this file, into that agent's own file "
                    "(its prompt) or a skill it lists.",
                    f"Now: {len(agent_sections)} section{'s' if len(agent_sections) != 1 else ''} "
                    f"({moved} tokens) go to {reach}. After: only that agent gets them.",
                    review,
                    f"Up to {_amount(units, saving, period)} if nothing else needed them."
                    if saving
                    else "Smaller startup for every session and subagent that loads this file.",
                    "Your main session no longer sees these rules. If it also relies on them, keep a one-line "
                    "pointer here.",
                ),
                "\n".join(
                    [
                        f"In {path}, these sections are only for specific agents:",
                        *lines,
                        "Move each one into that agent's file (~/.claude/agents/<name>.md or "
                        ".claude/agents/<name>.md, in the body below the frontmatter). If the agent is built "
                        "into Claude Code and has no file, put the section in a skill instead "
                        "(.claude/skills/<name>/SKILL.md) and add the skill to the agent's `skills:` list. "
                        "Leave a one-line pointer in the original file only if the main session needs to know "
                        "the rule exists.",
                        _PROMPT_TAIL,
                    ]
                ),
            )
        )

    if review.duplicates:
        repeated = sum(d["tokens"] for d in review.duplicates)
        lines = []
        for dup in review.duplicates[:8]:
            where = ", ".join(f"{o['file']} line {o['line']}" for o in dup["also_in"][:3])
            lines.append(f'- line {dup["line"]}: "{dup["excerpt"]}" (also in {where})')
        fixes.append(
            _fix(
                "Remove repeated text",
                _explainer(
                    "Paragraphs that say the same thing twice are cut to one copy, in the file where they "
                    "belong.",
                    f"Now: {len(review.duplicates)} repeated paragraph{'s' if len(review.duplicates) != 1 else ''} "
                    f"(about {repeated:,} tokens). After: each is said once.",
                    review,
                    f"Up to {_amount(units, per_token * repeated, period)}." if per_token else "A smaller file.",
                    "None, if the copy you keep reaches every session that needs it. A user-level rule reaches "
                    "every project; a project rule reaches only that project.",
                ),
                "\n".join(
                    [
                        f"These paragraphs in {path} are repeated:",
                        *lines,
                        "Keep one copy of each, in the most specific file that every session needing it "
                        "loads, and remove the others.",
                        _PROMPT_TAIL,
                    ]
                ),
            )
        )

    if review.stale:
        lines = [f"- line {s['line']}: {s['reference']}" for s in review.stale[:12]]
        fixes.append(
            _fix(
                "Fix references to things that no longer exist",
                _explainer(
                    "Paths, imports and scripts named in the file that can't be found are updated or "
                    "removed.",
                    f"Now: {len(review.stale)} reference{'s' if len(review.stale) != 1 else ''} point to "
                    "nothing. After: every reference is real.",
                    review,
                    "Fewer wasted steps: Claude stops looking for files and scripts that are gone.",
                    "None, unless a path is created later on purpose (for example a build output).",
                ),
                "\n".join(
                    [
                        f"{path} refers to things I can't find:",
                        *lines,
                        "For each one, find what it should point to now (search the repository and git "
                        "history) and update it, or remove the line if the thing is gone for good. Tell me "
                        "which you updated and which you removed.",
                        "Show me the diff before saving. Change nothing else. " + PROMPT_RESTART,
                    ]
                ),
            )
        )

    big = [s for s in review.sections if s.tokens >= ON_DEMAND_SECTION_TOKENS and not s.agents]
    if big and review.level not in ("Auto memory",) and not review.scoped:
        lines = [f'- "{s.heading}" (line {s.line}, about {s.tokens:,} tokens)' for s in big[:8]]
        on_demand = sum(s.tokens for s in big)
        fixes.append(
            _fix(
                "Load large sections only when needed",
                _explainer(
                    "Large sections about one part of the code, or one kind of task, move out of the "
                    "always-loaded file. A path-scoped rule (.claude/rules/<name>.md with `paths:` "
                    "frontmatter) loads only when Claude works on matching files; a skill loads only when "
                    "the task calls for it.",
                    f"Now: {on_demand:,} tokens in {len(big)} section{'s' if len(big) != 1 else ''} go to {reach}. "
                    "After: they load only when relevant.",
                    review,
                    f"Up to {_amount(units, per_token * on_demand, period)} if they are rarely needed."
                    if per_token
                    else "A smaller startup for every session.",
                    "Claude sees these rules only when a matching file or task comes up. Pick the paths or "
                    "skill description carefully so they load when they should.",
                ),
                "\n".join(
                    [
                        f"These sections of {path} are large and always loaded:",
                        *lines,
                        "For each one, decide whether it applies to specific files or folders (move it to "
                        ".claude/rules/<topic>.md with `paths:` frontmatter listing the globs) or to a kind "
                        "of task (move it to a skill, .claude/skills/<topic>/SKILL.md, with a one-line "
                        "description of when to use it). Keep it here if it applies to almost every task.",
                        _PROMPT_TAIL,
                    ]
                ),
            )
        )

    if review.tokens >= TRIM_TOKENS:
        top = _largest(review.sections)
        lines = [f'- "{s.heading}" (about {s.tokens:,} tokens)' for s in top]
        target = review.tokens // 2
        fixes.append(
            _fix(
                "Trim this file",
                _explainer(
                    "The file is shortened: rules said once, in short lines, without anything Claude can "
                    "read from the code itself.",
                    f"Now: about {review.tokens:,} tokens, sent to {reach}. After: aim for about {target:,}.",
                    review,
                    (
                        f"{_amount(units, cost / 2, period, prefix='About ')} if you halve it."
                        if cost
                        else "A smaller startup."
                    ),
                    "Cutting too much loses rules Claude needs. Review the diff line by line.",
                ),
                "\n".join(
                    [
                        f"{path} is about {review.tokens:,} tokens and is sent to {reach}. Its largest "
                        "sections are:",
                        *lines,
                        f"Shorten it to about {target:,} tokens. Remove explanations of things Claude can "
                        "learn from the code, merge rules that overlap, and turn paragraphs into short "
                        "imperative lines.",
                        _PROMPT_TAIL,
                    ]
                ),
            )
        )
    return fixes


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _findings(review: FileReview) -> list[str]:
    notes = []
    if review.scoped:
        notes.append("Loads only when Claude works on matching files (path-scoped).")
    agent_sections = sum(1 for s in review.sections if s.agents)
    if agent_sections:
        verb = "names" if agent_sections == 1 else "name"
        notes.append(f"{_count(agent_sections, 'section', 'sections')} {verb} a specific agent.")
    if review.duplicates:
        notes.append(f"{_count(len(review.duplicates), 'repeated paragraph', 'repeated paragraphs')}.")
    if review.stale:
        notes.append(f"{_count(len(review.stale), 'reference', 'references')} to things that no longer exist.")
    if review.tokens >= TRIM_TOKENS:
        notes.append(f"Large: about {review.tokens:,} tokens.")
    return notes


@dataclass(slots=True)
class Review:
    files: list[FileReview]
    transcripts: dict[str, int]


def build_review(
    config_dir: Path,
    context_files: dict,
    *,
    salt: bytes | None = None,
    projects: list[Path] | None = None,
) -> Review:
    """Read every CLAUDE.md-family file and join it to its usage.

    ``projects``: the project folders to read (the ones a report limited
    to one project saw, ``skills_review.project_folders_for``), or
    ``None`` for every project Claude Code ran in. Your own files are
    read either way: every project gets them."""
    config_dir = Path(config_dir)
    salt = salt if salt is not None else parse_mod.load_or_create_salt(config_dir)
    claude_root = _claude_root(config_dir)
    folders, worktrees = split_worktrees(projects if projects is not None else project_folders(claude_root))
    transcripts = dict(context_files.get("transcripts") or {})
    names = agent_names(config_dir, folders, list(transcripts))
    usage = {row["hash"]: row for row in context_files.get("files") or () if isinstance(row, dict)}
    reviews: list[FileReview] = []
    for candidate in discover(config_dir, projects=folders):
        index = _project_index(candidate.project_root) if candidate.project_root is not None else None
        review = read_file(candidate, salt, names, index)
        if review is None:
            continue
        rows = [usage[review.id]] if review.id in usage else []
        for copy in _worktree_copies(candidate, worktrees, claude_root):
            row = usage.get(parse_mod.path_hash(str(copy), salt))
            if row is not None:
                rows.append(row)
        review.usage = _merge_usage(rows)
        reviews.append(review)
    _find_duplicates(reviews)
    order = {level: index for index, level in enumerate(LEVELS)}
    reviews.sort(key=lambda r: (-(r.usage or {}).get("cost_usd", 0.0), order.get(r.level, 99), str(r.path).lower()))
    return Review(files=reviews, transcripts=transcripts)


def _worktree_copies(candidate: Candidate, worktrees: dict[str, list[Path]], claude_root: Path) -> list[Path]:
    """The same file's path in each worktree of its project."""
    if candidate.project_root is None:
        return []
    copies: list[Path] = []
    for worktree in worktrees.get(os.path.normcase(str(candidate.project_root)), ()):
        if candidate.level == "Auto memory":
            copies.append(_memory_file(claude_root, worktree))
            continue
        try:
            relative = candidate.path.relative_to(candidate.project_root)
        except ValueError:
            continue
        copies.append(worktree / relative)
    return copies


def _merge_usage(rows: list[dict]) -> dict | None:
    """One usage row from a file's main copy and its worktree copies."""
    if not rows:
        return None
    if len(rows) == 1:
        return rows[0]
    merged = dict(rows[0])
    merged["reach"] = {}
    merged["cost_by_reach"] = {}
    merged["sends"] = 0
    merged["cost_usd"] = 0.0
    for row in rows:
        merged["sends"] += row.get("sends", 0)
        merged["cost_usd"] += row.get("cost_usd", 0.0)
        for field_name in ("reach", "cost_by_reach"):
            for reach, value in (row.get(field_name) or {}).items():
                merged[field_name][reach] = merged[field_name].get(reach, 0) + value
        if (row.get("last_seen") or "") > (merged.get("last_seen") or ""):
            merged["last_seen"] = row["last_seen"]
    return merged


def file_summary(review: FileReview, units: Units, period: str) -> dict:
    usage = review.usage or {}
    cost = float(usage.get("cost_usd") or 0.0)
    return {
        "id": review.id,
        "path": home_label(review.path),
        "name": review.path.name,
        "level": review.level,
        "project": review.project,
        "who": _LEVEL_WHO.get(review.level, ""),
        "tokens": review.tokens,
        "scoped": review.scoped,
        "sections": len(review.sections),
        "seen": bool(usage),
        "sends": usage.get("sends", 0),
        "reach": usage.get("reach", {}),
        "reach_text": _reach_text(review.usage),
        "cost_usd": round(cost, 6),
        "cost_text": _amount(units, cost, period) if cost else "",
        "findings": _findings(review),
        "fix_count": len(build_fixes(review, units, period)),
    }


def file_detail(review: FileReview, units: Units, period: str) -> dict:
    detail = file_summary(review, units, period)
    per_token = detail["cost_usd"] / review.tokens if review.tokens else 0.0
    detail.update(
        {
            "section_rows": [
                {
                    "heading": s.heading,
                    "level": s.level,
                    "line": s.line,
                    "tokens": s.tokens,
                    "share": round(s.chars / review.chars, 4) if review.chars else 0.0,
                    "cost_text": _amount(units, per_token * s.tokens, period) if per_token * s.tokens else "",
                    "agents": s.agents,
                }
                for s in review.sections
            ],
            "imports": [home_label(p) for p in review.imports],
            "duplicates": review.duplicates,
            "stale": review.stale,
            "cost_by_reach": (review.usage or {}).get("cost_by_reach", {}),
            "fixes": build_fixes(review, units, period),
        }
    )
    return detail


# -- project files: their names, from disk, and what to do about one ------------


#: What a file's extension says about it, in a closed set of words: prose
#: first (the files a split or a trim helps), then data, configuration and code.
EXT_CLASSES = ("md", "txt", "json", "config", "code", "other")
_EXT_SETS = {
    "md": frozenset({".md", ".markdown", ".mdx"}),
    "txt": frozenset({".txt", ".text", ".rst", ".adoc"}),
    "json": frozenset({".json", ".jsonl", ".ndjson", ".json5"}),
    "config": frozenset(
        {".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env", ".xml", ".csv", ".tsv", ".lock", ".properties"}
    ),
    "code": frozenset(
        {
            ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".cs", ".java", ".kt", ".go", ".rs", ".rb", ".php",
            ".c", ".h", ".cpp", ".hpp", ".swift", ".sh", ".ps1", ".bat", ".sql", ".html", ".css", ".scss", ".vue",
            ".svelte", ".lua", ".dart", ".scala",
        }
    ),
}


def ext_class(name: str) -> str:
    """The class of a file name's extension (:data:`EXT_CLASSES`): ``other``
    for any it does not know, or for none."""
    suffix = os.path.splitext(str(name))[1].lower()
    return next((word for word, suffixes in _EXT_SETS.items() if suffix in suffixes), "other")


#: Bounds on the walk that names files, so a large repository stays quick:
#: how deep, how many folders per project, and how many files in all.
_NAMES_MAX_DEPTH = 10
_NAMES_MAX_DIRS = 20000
NAMES_MAX_FILES = 60000


@dataclass(slots=True)
class LocalNames:
    """What a walk of the project folders found, keyed by the salted hash of
    each file's path (:func:`parse.path_hash`), the same hash transcripts
    keep. Lives on the dashboard's machine and is never stored or exported."""

    #: hash -> ``name`` (the path from its project folder, or ``~``-relative
    #: outside one), ``ext`` (:func:`ext_class`) and ``project`` (the folder's name).
    names: dict[str, dict] = field(default_factory=dict)
    #: Files some CLAUDE.md pulls in with ``@path``.
    imports: set[str] = field(default_factory=set)
    #: hash of an imported file -> the CLAUDE.md that imports it (``parent``,
    #: its hash) and the import's size (``tokens``).
    inlined: dict[str, dict] = field(default_factory=dict)
    #: The walk stopped at a bound, so some files may have no name.
    truncated: bool = False

    def as_dict(self) -> dict:
        """The shape ``context_files.project_files`` takes as ``local``."""
        return {"names": self.names, "imports": sorted(self.imports), "inlined": self.inlined}


def _walk_files(root: Path, limit: int) -> tuple[list[Path], bool]:
    """Files under ``root``, shallow ones first, skipping :data:`_WALK_SKIP`
    folders (``.git``, ``node_modules``, build output) and git worktrees.
    Stops at ``limit`` files, :data:`_NAMES_MAX_DIRS` folders and
    :data:`_NAMES_MAX_DEPTH` levels; the flag says it stopped early."""
    found: list[Path] = []
    dirs = 0
    queue: deque[tuple[Path, int]] = deque([(root, 0)])
    while queue:
        folder, depth = queue.popleft()
        if dirs >= _NAMES_MAX_DIRS:
            return found, True
        dirs += 1
        try:
            entries = sorted(os.scandir(folder), key=lambda entry: entry.name)
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    skipped = entry.name in _WALK_SKIP or (entry.name == "worktrees" and folder.name == ".claude")
                    if depth < _NAMES_MAX_DEPTH and not skipped:
                        queue.append((Path(entry.path), depth + 1))
                elif entry.is_file(follow_symlinks=False):
                    if len(found) >= limit:
                        return found, True
                    found.append(Path(entry.path))
            except OSError:
                continue
    return found, False


_NAMES_TTL_S = 600.0
_NAMES: dict[tuple, tuple[float, LocalNames]] = {}
_NAMES_LOCK = threading.Lock()


def local_names(
    config_dir: Path,
    *,
    projects: list[Path] | None = None,
    salt: bytes | None = None,
    max_files: int = NAMES_MAX_FILES,
    wanted: Iterable[str] | None = None,
) -> LocalNames:
    """Name the files transcripts only know by hash: hash the files under
    each project folder (and its git worktrees, as the same path), your
    plans, rules, user CLAUDE.md and each project's auto memory, with the
    salt transcripts were hashed with, and keep the path from the project
    folder beside each hash. Also finds the files CLAUDE.md files import
    with ``@path``. The result is kept for :data:`_NAMES_TTL_S` seconds, as a
    page asks for it several times.

    This is read from disk when the dashboard asks and stays there. The
    walk skips ``.git``, ``node_modules`` and build folders, and stops at
    ``max_files`` files (:attr:`LocalNames.truncated` says so).

    ``projects``: the folders a report limited to one project saw, or
    ``None`` for every project Claude Code ran in. ``wanted``: the hashes
    the caller has a use for; only those are kept, so a large repository
    does not fill memory with names nobody asked for."""
    config_dir = Path(config_dir)
    wanted = None if wanted is None else frozenset(wanted)
    salt = salt if salt is not None else parse_mod.load_or_create_salt(config_dir)
    claude_root = _claude_root(config_dir)
    folders, worktrees = split_worktrees(projects if projects is not None else project_folders(claude_root))
    key = (
        os.path.normcase(str(config_dir)),
        tuple(os.path.normcase(str(folder)) for folder in folders),
        hashlib.sha256(salt).hexdigest(),
        max_files,
        None if wanted is None else hashlib.sha256("".join(sorted(wanted)).encode()).hexdigest(),
    )
    now = time.monotonic()
    with _NAMES_LOCK:
        for old_key in [k for k, (at, _) in _NAMES.items() if now - at >= _NAMES_TTL_S]:
            del _NAMES[old_key]
        if key in _NAMES:
            return _NAMES[key][1]
    found = _find_names(config_dir, claude_root, folders, worktrees, salt, max_files, wanted)
    with _NAMES_LOCK:
        _NAMES[key] = (time.monotonic(), found)
    return found


def _find_names(
    config_dir: Path,
    claude_root: Path,
    folders: list[Path],
    worktrees: dict[str, list[Path]],
    salt: bytes,
    max_files: int,
    wanted: frozenset[str] | None = None,
) -> LocalNames:
    found = LocalNames()
    left = max_files

    def note(path: Path, name: str, project: str, *, force: bool = False) -> None:
        file_hash = parse_mod.path_hash(str(path), salt)
        if force or wanted is None or file_hash in wanted:
            found.names.setdefault(file_hash, {"name": name, "ext": ext_class(path.name), "project": project})

    for folder in folders:
        files, cut = _walk_files(folder, max(left, 0))
        left -= len(files)
        found.truncated = found.truncated or cut
        copies = worktrees.get(os.path.normcase(str(folder)), ())
        for path in files:
            try:
                relative = path.relative_to(folder)
            except ValueError:
                continue
            name = relative.as_posix()
            note(path, name, folder.name)
            for copy in copies:
                note(copy / relative, name, folder.name)
        memory = _memory_file(claude_root, folder).parent
        for path in sorted(memory.glob("*.md")) if memory.is_dir() else ():
            note(path, home_label(path), "")
    # Your own plans and rules sit outside every project, so they are named
    # on top of the cap: there are few of them.
    for extra in (claude_root / "plans", claude_root / "rules"):
        if extra.is_dir():
            files, _cut = _walk_files(extra, 2000)
            for path in files:
                note(path, home_label(path), "")
    user_file = claude_root / "CLAUDE.md"
    if user_file.is_file():
        note(user_file, home_label(user_file), "")

    for candidate in discover(config_dir, projects=folders):
        if candidate.level == "Import":
            continue
        parent_hash = parse_mod.path_hash(str(candidate.path), salt)
        for target in _imports(_read(candidate.path) or "", candidate.path):
            if not target.is_file():
                continue
            child_hash = parse_mod.path_hash(str(target), salt)
            text = _read(target) or ""
            found.imports.add(child_hash)
            found.inlined.setdefault(
                child_hash, {"parent": parent_hash, "tokens": round(len(text) / _CHARS_PER_TOKEN_APPROX)}
            )
            if child_hash not in found.names:
                project = candidate.project_root
                try:
                    name = target.relative_to(project).as_posix() if project is not None else home_label(target)
                except ValueError:
                    name = home_label(target)
                note(target, name, project.name if project is not None else "", force=True)
    return found


def project_file_rows(
    config_dir: Path, data: dict, *, projects: list[Path] | None = None
) -> tuple[list[dict], LocalNames]:
    """The project files agents consume (``context_files.project_files``)
    with the names this machine can give them: the rows of ``data`` (a
    report's ``context_files``) and the walk that named them. Only the
    hashes the report has a use for are kept from the walk."""
    from . import context_files

    rows = context_files.project_files(data)
    local = local_names(config_dir, projects=projects, wanted={row["hash"] for row in rows})
    return context_files.project_files(data, local.as_dict()), local


def _type_names(row: dict) -> list[str]:
    """The agent types that read a file by habit, biggest share first."""
    return [
        item["reach"]
        for item in sorted(row.get("reach") or (), key=lambda item: -item.get("share", 0))
        if item.get("standing") and item.get("reach") != "main"
    ]


def _file_explainer(row: dict, label: str, what: str, now_after: str, effect: str, tradeoff: str) -> list[list[str]]:
    types = _type_names(row)
    loaded = ", and loaded for them by Claude Code" if row.get("source") != "read" else ""
    return [
        ["What this changes", what],
        ["Now and after", now_after],
        ["Where and who it affects", f"{label}: read by {', '.join(types) if types else 'your agents'}{loaded}."],
        ["Expected effect", effect],
        ["Trade-off", tradeoff],
        [
            "How to undo it",
            "Claude shows the diff before saving. To go back, restore the file from git or ask Claude to undo the edit.",
        ],
    ]


def project_file_fixes(row: dict, units: Units) -> list[dict]:
    """What to do about a project file (a row of
    ``context_files.project_files``) that is big, read by many agent types
    or growing: trim what is stale, split it by who needs it, move
    rule-like parts into path-scoped rules, move reference material into a
    skill, and, for a file agents read, put the few lines an agent needs in
    its definition and drop its read. Each is a prompt to copy: the
    dashboard edits nothing. A file
    with no name on this machine (``row["name"]`` empty) has no fixes."""
    name = row.get("name") or ""
    if not name:
        return []
    project = row.get("project") or ""
    label = f"{name} (in {project})" if project else name
    tokens = int(row.get("tokens") or 0)
    types = _type_names(row)
    cost = float(row.get("cost_month_usd") or 0.0)
    each_1k = _amount(units, cost * 1000 / tokens, "a month", prefix="about ") if tokens and cost > 0 else ""
    effect = (
        f"It costs {_amount(units, cost, 'a month')} now. Every 1,000 tokens cut saves {each_1k}."
        if each_1k
        else "A smaller file is written to the cache and read back less each time."
    )
    now = f"Now: about {tokens:,} tokens, read by {len(types)} agent type{'s' if len(types) != 1 else ''}."
    who = ", ".join(types) if types else "your agents"
    stem, ext = os.path.splitext(os.path.basename(name))
    grew = row.get("change_pct")
    pace = f" It grew {grew:.0f}% in about 30 days." if isinstance(grew, (int, float)) and grew >= 1 else ""
    opening = f"{label} is about {tokens:,} tokens and is read by {who}.{pace}"
    fixes = [
        _fix(
            "Trim what is stale",
            _file_explainer(
                row,
                label,
                "Finished work, decisions the code already shows, repeated text and notes about files that no "
                "longer exist are cut.",
                f"{now} After: smaller, and it stops growing by what is no longer true.",
                effect,
                "A note you cut cannot be read later. Git keeps the old text.",
            ),
            "\n".join(
                [
                    opening,
                    "Read it and list what is stale. That means finished work, decisions the code already shows, "
                    "text that repeats, and notes about files or functions that no longer exist. Cut those.",
                    _PROMPT_TAIL,
                ]
            ),
        ),
        _fix(
            "Split it by who needs it",
            _file_explainer(
                row,
                label,
                "The file is split into a short shared part and a file for each group that needs more.",
                f"{now} After: each agent type reads the shared part and only its own file.",
                effect,
                "More files to keep up to date, and an agent must know which one is its own.",
            ),
            "\n".join(
                [
                    opening,
                    "Work out which parts each of those agents uses. Keep a short shared part in the file, and "
                    f"move what only some of them need into separate files beside it (for example {stem}-<topic>{ext}). "
                    "Then change each agent's definition (.claude/agents/<name>.md) so it reads only the files it needs.",
                    _PROMPT_TAIL,
                ]
            ),
        ),
        _fix(
            "Move rule-like parts into path-scoped rules",
            _file_explainer(
                row,
                label,
                "Rules that apply to one part of the code move into their own files under .claude/rules. They load "
                "only when Claude works on a matching path.",
                f"{now} After: those parts load only for the paths they name.",
                effect,
                "A rule loads only when a matching file is touched, so check the globs name the right paths.",
            ),
            "\n".join(
                [
                    opening,
                    "Find the parts that are rules about one folder or file type: a style, naming or safety rule. "
                    "Move each into its own file under .claude/rules/ with a `paths:` list of globs in its "
                    "frontmatter. It then loads only when Claude works on a matching file. Leave a one-line pointer "
                    "behind only if the rule matters elsewhere.",
                    _PROMPT_TAIL,
                ]
            ),
        ),
        _fix(
            "Move reference material into a skill",
            _file_explainer(
                row,
                label,
                "Reference material (long lists, tables, how-tos) moves into a skill that loads only when a task "
                "needs it.",
                f"{now} After: only the skill's one-line description is always present.",
                effect,
                "Claude must choose to load the skill, so write its description to say when it applies.",
            ),
            "\n".join(
                [
                    opening,
                    "Find the reference material: long lists, tables and step-by-step guides that only some tasks "
                    "need. Move it into a skill (.claude/skills/<name>/SKILL.md) with a description that says "
                    "when to use it, and remove it from the file.",
                    _PROMPT_TAIL,
                ]
            ),
        ),
    ]
    if types and row.get("source") == "read":
        fixes.append(
            _fix(
                "Put the essential lines in the agent definition and drop the read",
                _file_explainer(
                    row,
                    label,
                    "Each agent keeps the few lines it uses in its own definition, and stops reading the file.",
                    f"{now} After: no agent reads it by habit.",
                    effect,
                    "The copied lines can drift from the file, so copy only what rarely changes.",
                ),
                "\n".join(
                    [
                        opening,
                        f"For each of these agents ({who}), find the few lines it really uses and copy them into "
                        "its definition (.claude/agents/<name>.md). Then remove the instruction to read the file "
                        "from that definition. Keep the file for people.",
                        _PROMPT_TAIL,
                    ]
                ),
            )
        )
    return fixes


def render_markdown(review: Review, units: Units, period: str) -> str:
    """The review as Markdown, for ``claudeglass review claude-md``."""
    lines = ["# CLAUDE.md review", ""]
    if not review.files:
        return "# CLAUDE.md review\n\nNo CLAUDE.md files found.\n"
    for file_review in review.files:
        summary = file_summary(file_review, units, period)
        lines.append(f"## {summary['path']}")
        lines.append("")
        lines.append(
            f"{summary['level']}, about {summary['tokens']} tokens. Sent to {summary['reach_text']}."
            + (f" Estimated cost: {summary['cost_text']}." if summary["cost_text"] else "")
        )
        for note in summary["findings"]:
            lines.append(f"- {pages.plain(note)}")
        lines.append("")
        for fix in build_fixes(file_review, units, period):
            lines.append(f"### {fix['title']}")
            lines.append("")
            for heading, text in fix["explainer"]:
                lines.append(f"- **{heading}:** {pages.plain(text)}")
            lines.append("")
            lines.append("Ask Claude:")
            lines.append("")
            lines.append("```text")
            lines.append(fix["prompt"])
            lines.append("```")
            lines.append("")
    return "\n".join(lines)


__all__ = [
    "Candidate",
    "EXT_CLASSES",
    "FileReview",
    "LEVELS",
    "LocalNames",
    "NAMES_MAX_FILES",
    "Review",
    "Section",
    "agent_names",
    "build_fixes",
    "build_review",
    "discover",
    "ext_class",
    "file_detail",
    "file_summary",
    "local_names",
    "project_file_fixes",
    "project_file_rows",
    "project_folders",
    "render_markdown",
    "sections",
]

"""Is the SessionStart snapshot hook actually running?

``hooks/snapshot-config.py`` writes a config snapshot at the start of
every Claude Code session, but only if Claude Code's ``settings.json``
(``~/.claude``, or ``$CLAUDE_CONFIG_DIR`` -- see :func:`settings_path`) has a
SessionStart hook whose command points at it. A hand-edited command can
be quietly wrong: in a Windows path written into JSON with single
backslashes, a folder whose name starts with ``t`` (``tools``,
``temp``) decodes the backslash-t as a tab, the path no longer exists, and Claude Code runs
the hook without complaint while no snapshot is ever written. The same
silence follows when the interpreter the command names is not installed
(``py -3`` with no Python launcher on PATH), or when the command relies on
a ``%VAR%`` that Claude Code's shell on Windows (Git Bash) never expands.

:func:`check` reports what it finds in plain words for the Data quality
tab (``GET /api/diagnostics``) and ``init``; :func:`repair` rewrites
only that one command string, after a backup, when ``init`` is told to.

Metrics capture adds its own entries (``hooks/capture-hook.py`` on
SessionStart and SubagentStart for its note, PostToolUse for Deep, and
SessionEnd, Notification and PermissionRequest for the free signals),
described by :class:`HookSpec`. :func:`check_capture` checks them the same way;
:func:`plan_capture` works out the change that makes settings.json run
exactly the entries the chosen metrics need, and :func:`connect` writes
it. :func:`install_hook_files` copies the hook files (the launcher
``capture-hook.py``, the module ``capture_hook.py`` it runs and the
catalogue) out of the package, which works inside the ``.pyz`` build too.

None of the above needs a transcript. :func:`count_hook_errors` does --
it tallies each hook event's non-blocking errors (``PreToolUse``,
``PostToolUse``, and so on; never the matcher or tool-name suffix, so an
MCP server name never surfaces) across already-parsed transcripts, and
:meth:`HookErrorHealth.recommendation` turns a hook that fails on most
of its recorded runs into one plain-English prompt naming where to look for it,
the latency/noise trade-off, and the undo. It only ever prints; nothing
here writes to settings.json on that account.

:func:`measure_deep_wait` reads the same transcripts for Deep's own
big_output/web PostToolUse hook's real ``durationMs`` (CAP-9/F10: it used
to be dropped, and the catalogue guessed at a figure with no source
behind it) and turns it into a median/p90 :class:`DeepWaitStats`.
"""

from __future__ import annotations

import csv
import difflib
import hashlib
import importlib.resources
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from . import capture_catalogue, discovery, snapshots
from .model import EventKind, TranscriptResult

HOOK_SCRIPT_NAME = "snapshot-config.py"

#: Hook scripts metrics capture installs; an entry whose command runs one
#: of them belongs to capture.
CAPTURE_SCRIPTS = (capture_catalogue.HOOK_SCRIPT,)

#: Files each capture script needs next to it under ``<config-dir>/hooks``:
#: the launcher Claude Code runs, the module it imports and the catalogue
#: the module reads. The launcher comes last, as :func:`install_hook_files`
#: writes it last: it is the file that starts using the others.
CAPTURE_FILES = {
    capture_catalogue.HOOK_SCRIPT: (
        capture_catalogue.CATALOGUE_FILE,
        capture_catalogue.HOOK_MODULE,
        capture_catalogue.HOOK_SCRIPT,
    )
}

#: Every file name this tool ever installs under ``<config-dir>/hooks``.
ALL_HOOK_FILES = frozenset({HOOK_SCRIPT_NAME} | {name for names in CAPTURE_FILES.values() for name in names})

#: Seconds Claude Code waits for a capture hook before giving up on it.
CAPTURE_TIMEOUT_S = 5

#: The oldest Python a hook command can safely name: ``tomllib``, which
#: ``capture_hook.py`` reads config.toml with, is stdlib only from here.
_MIN_PYTHON = (3, 11)


@dataclass(frozen=True, slots=True)
class HookSpec:
    """One settings.json hook entry this tool wants: the script it runs,
    the event, the matcher (``""`` matches everything) and whether Claude
    Code runs it in the background."""

    script: str
    event: str
    matcher: str = ""
    async_: bool = False

    def describe(self) -> str:
        tools = set(self.matcher.split("|"))
        when = {
            "SessionStart": "when a session starts, is cleared or compacts",
            "SubagentStart": "when a subagent starts",
            "SubagentStop": "when a subagent finishes",
            "UserPromptSubmit": "when you send a message",
            "PostToolUse": "after "
            + ("web results" if tools - {"ExitPlanMode"} <= set(capture_catalogue.WEB_TOOLS) else "read, search and web results")
            + (" and an approved plan" if "ExitPlanMode" in tools else ""),
            "SessionEnd": "when a session ends",
            "Notification": "when Claude waits for you, in the background",
            "PermissionRequest": "when Claude asks for permission, in the background",
            "Stop": "when a turn ends",
            "StopFailure": "when a turn ends in an API error",
        }.get(self.event, f"on {self.event}")
        return f"{self.script} {when}"


def capture_specs(ids) -> tuple[HookSpec, ...]:
    """The hook entries the metrics in ``ids`` need
    (``capture_catalogue.hook_specs``)."""
    return tuple(HookSpec(*spec) for spec in capture_catalogue.hook_specs(ids))


#: A JSON string escape that silently turned part of a Windows path into
#: a control character, and the two characters it came from.
_DECODED_ESCAPES = {"\t": "\\t", "\n": "\\n", "\r": "\\r", "\b": "\\b", "\f": "\\f"}

_QUOTED_SCRIPT_RE = re.compile(r'"([^"]*' + re.escape(HOOK_SCRIPT_NAME) + r')"')
_BARE_SCRIPT_RE = re.compile(r"(\S*" + re.escape(HOOK_SCRIPT_NAME) + r")")
#: The command's first word: a quoted path or a bare name.
_FIRST_WORD_RE = re.compile(r'\s*(?:"([^"]+)"|(\S+))')
#: A cmd.exe-style variable, which Git Bash passes through unexpanded.
_PERCENT_VAR_RE = re.compile(r"%[A-Za-z_][A-Za-z0-9_]*%")


@dataclass(slots=True)
class HookHealth:
    settings_path: Path
    #: The SessionStart command that runs the snapshot hook, if any.
    command: str | None = None
    #: The script path that command runs, with variables expanded.
    script_path: Path | None = None
    script_exists: bool = False
    #: The program the command starts (its first word), as written.
    interpreter: str | None = None
    #: That program is an existing file or found on PATH.
    interpreter_found: bool = True
    #: The command uses a ``%VAR%`` that Git Bash does not expand.
    percent_vars: bool = False
    #: The command holds a control character: a mis-escaped path.
    mis_escaped: bool = False
    #: Days since the newest snapshot, or None when there is none.
    last_snapshot_days: float | None = None
    #: A corrected command, when one can be worked out and its script exists.
    fixed_command: str | None = None
    #: A :data:`POLICY_TEXT` key when a settings policy stops Claude Code
    #: running the user's hooks at all (:func:`hook_policy`).
    blocked_by: str | None = None

    @property
    def ok(self) -> bool:
        return (
            self.command is not None
            and self.script_exists
            and not self.mis_escaped
            and self.interpreter_found
            and not self.percent_vars
            and self.blocked_by is None
        )

    def summary(self) -> str:
        """One plain sentence for the Data quality tab and ``init``."""
        if self.last_snapshot_days is None:
            age = "No automatic config snapshot has been taken yet"
        else:
            days = int(self.last_snapshot_days)
            age = "Last config snapshot: " + ("today" if days == 0 else f"{days} day{'s' if days != 1 else ''} ago")
        if self.blocked_by is not None:
            return f"{age}. {POLICY_TEXT[self.blocked_by]}"
        if self.command is None:
            return f"{age}. No SessionStart hook runs {HOOK_SCRIPT_NAME} (see 'claudeglass snapshot-config --print-hook')."
        if self.mis_escaped:
            return (
                f"{age}. The SessionStart hook command in settings.json has a broken path: a single backslash "
                "in JSON turned part of it into a tab or newline, so the hook never runs. "
                "Run 'claudeglass init --repair-hook' to fix it."
            )
        if not self.script_exists:
            return f"{age}. The SessionStart hook runs {self.script_path}, which does not exist."
        if not self.interpreter_found:
            return (
                f"{age}. The SessionStart hook starts '{self.interpreter}', which is not installed or not on "
                "your PATH, so the hook never runs. Run 'claudeglass init --repair-hook' to point it "
                "at this Python."
            )
        if self.percent_vars:
            return (
                f"{age}. The SessionStart hook command uses a %VARIABLE%, which Claude Code's shell on Windows "
                "(Git Bash) does not expand, so the hook may never run. Run 'claudeglass init "
                "--repair-hook' to write the full path instead."
            )
        return f"{age}. The SessionStart hook is set up."


def settings_path(claude_root: str | Path | None = None) -> Path:
    """Claude Code's user ``settings.json``: in ``claude_root`` when
    given (``--claude-root``), else where :func:`discovery.claude_root`
    finds it (``$CLAUDE_CONFIG_DIR``, else ``~/.claude``). Never next to
    ``--config-dir``, which can point this tool's folder anywhere."""
    return discovery.claude_root(claude_root) / "settings.json"


#: Why Claude Code won't run a hook from the user's settings.json at all,
#: whatever the entry says (docs/en/settings-reference.md: "What runs
#: under allowManagedHooksOnly" -- "user, project, and local hooks ... are
#: blocked", and the status line narrows to managed settings; and
#: ``disableAllHooks`` -- "In managed settings: Claude Code disables every
#: configured hook"; "In any other settings file: Claude Code disables
#: user, project, local, and plugin hooks"). A closed vocabulary.
POLICY_MANAGED_ONLY = "managed_only"
POLICY_ALL_OFF_MANAGED = "all_off_managed"
POLICY_ALL_OFF = "all_off"

POLICY_TEXT = {
    POLICY_MANAGED_ONLY: (
        "Your organisation's managed settings allow only the hooks they deploy (allowManagedHooksOnly), so "
        "Claude Code won't run hooks from your own settings.json (capture's and the config-snapshot hook "
        "included) or a custom status line. Reports and the dashboard still work from your transcripts; "
        "ask your administrator if you need the hooks."
    ),
    POLICY_ALL_OFF_MANAGED: (
        "Your organisation's managed settings turn off every hook (disableAllHooks), so Claude Code won't "
        "run capture's hooks, the config-snapshot hook or a custom status line. Reports and the dashboard "
        "still work from your transcripts."
    ),
    POLICY_ALL_OFF: (
        "settings.json sets disableAllHooks, so Claude Code runs none of your hooks (capture's and the "
        "config-snapshot hook included) and no custom status line. Remove it, or set it to false, to let "
        "them run."
    ),
}


def managed_settings_dir() -> Path:
    """The platform's system managed-settings directory. The same rule as
    ``hooks/snapshot-config.py``'s ``default_managed_settings_dir`` (a
    standalone script this package can't import; a test keeps the two in
    step): ``%ProgramFiles%\\ClaudeCode`` on Windows, ``/Library/
    Application Support/ClaudeCode`` on macOS, ``/etc/claude-code``
    elsewhere."""
    if sys.platform == "win32":
        return Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "ClaudeCode"
    if sys.platform == "darwin":
        return Path("/Library/Application Support/ClaudeCode")
    return Path("/etc/claude-code")


def _read_json_object(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def hook_policy(claude_root: str | Path | None = None, managed_dir: str | Path | None = None) -> str | None:
    """A :data:`POLICY_TEXT` key when a settings file stops Claude Code
    running the user's own hooks, else ``None``. Reads the file-based
    managed settings (``managed-settings.json`` and its
    ``managed-settings.d/*.json`` drop-ins) and the user settings.json.
    Managed settings delivered another way (the Windows registry, a macOS
    profile, server-managed settings) aren't visible on disk, nor is a
    project's own ``disableAllHooks``; there, capture status's measured
    "0 sessions captured" is the tell. Never raises."""
    base = Path(managed_dir) if managed_dir is not None else managed_settings_dir()
    managed = [_read_json_object(base / "managed-settings.json")]
    try:
        managed += [_read_json_object(p) for p in sorted((base / "managed-settings.d").glob("*.json"))]
    except OSError:
        pass
    if any(doc.get("disableAllHooks") is True for doc in managed):
        return POLICY_ALL_OFF_MANAGED
    if any(doc.get("allowManagedHooksOnly") is True for doc in managed):
        return POLICY_MANAGED_ONLY
    if _read_json_object(settings_path(claude_root)).get("disableAllHooks") is True:
        return POLICY_ALL_OFF
    return None


def _event_entries(settings: dict, event: str) -> list[tuple[str, dict]]:
    """``(matcher, entry)`` for every command entry under ``event``."""
    found = []
    hooks = settings.get("hooks")
    groups = hooks.get(event) if isinstance(hooks, dict) else None
    for group in groups if isinstance(groups, list) else []:
        entries = group.get("hooks") if isinstance(group, dict) else None
        matcher = group.get("matcher") if isinstance(group, dict) else None
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, dict) and isinstance(entry.get("command"), str):
                found.append((matcher if isinstance(matcher, str) else "", entry))
    return found


def _event_commands(settings: dict, event: str) -> list[str]:
    return [entry["command"] for _matcher, entry in _event_entries(settings, event)]


def _expand(text: str) -> str:
    """``text`` with ``~``, ``$VAR`` and ``%VAR%`` expanded the same way
    on every platform (``os.path.expandvars`` only knows ``%VAR%`` on
    Windows). A variable that isn't set is left as written."""
    text = _PERCENT_VAR_RE.sub(lambda m: os.environ.get(m.group(0)[1:-1], m.group(0)), text)
    return os.path.expanduser(os.path.expandvars(text))


def _script_path(command: str) -> Path | None:
    match = _QUOTED_SCRIPT_RE.search(command) or _BARE_SCRIPT_RE.search(command)
    if match is None:
        return None
    return Path(_expand(match.group(1)))


def _args_after_script(command: str) -> str:
    """Whatever follows the script path in ``command`` (leading space
    kept), or an empty string."""
    match = _QUOTED_SCRIPT_RE.search(command) or _BARE_SCRIPT_RE.search(command)
    return command[match.end():].rstrip() if match else ""


def _interpreter(command: str) -> str | None:
    match = _FIRST_WORD_RE.match(command)
    if match is None:
        return None
    return match.group(1) or match.group(2)


def _interpreter_found(program: str) -> bool:
    expanded = _expand(program)
    if os.path.isabs(expanded) or os.sep in expanded or (os.altsep and os.altsep in expanded):
        return Path(expanded).is_file()
    return shutil.which(expanded) is not None


def stable_python() -> str:
    """The Python a hook command should name: the base interpreter when
    this one runs in a virtual environment, since the hook script uses
    only the standard library and a venv can be deleted or rebuilt."""
    base = getattr(sys, "_base_executable", "") or ""
    return base if base and Path(base).is_file() else sys.executable


def _interpreter_version(program: str) -> tuple[int, int] | None:
    """``(major, minor)`` reported by the Python ``program`` names, or
    ``None`` when it can't be run in a few seconds. Bounded (ROB-P7): a
    broken, hanging or non-Python interpreter never blocks a health
    check, it just reads as "unknown" rather than as a problem."""
    expanded = _expand(program)
    try:
        result = subprocess.run(
            [expanded, "-I", "-S", "-c", "import sys; print(sys.version_info[0], sys.version_info[1])"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        major, minor = result.stdout.split()
        return int(major), int(minor)
    except ValueError:
        return None


#: SEC-P7/ROB-P7: hash-stamp manifest recording the SHA-256 of every hook
#: file this tool itself last wrote under ``<config-dir>/hooks``, so
#: :func:`check_capture` can tell a file that is merely an older release
#: this tool wrote ("outdated" -- :func:`refresh_hook_files` fixes it)
#: apart from one that was changed by something else since ("modified" --
#: left alone, only reported).
_MANIFEST_NAME = ".manifest.json"


def _manifest_path(config_dir: str | Path) -> Path:
    return Path(config_dir) / "hooks" / _MANIFEST_NAME


def _load_manifest(config_dir: str | Path) -> dict[str, str]:
    try:
        data = json.loads(_manifest_path(config_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_manifest(config_dir: str | Path, manifest: dict[str, str]) -> None:
    path = _manifest_path(config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{_MANIFEST_NAME}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def _packaged_bytes(name: str) -> bytes:
    return (importlib.resources.files("claudeglass") / "hooks" / name).read_bytes()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_provenance(path: Path, name: str, manifest: dict[str, str]) -> str:
    """``"ok"`` when ``path`` matches the packaged copy of ``name`` this
    version ships, ``"missing"`` when it isn't there, ``"outdated"`` when
    it doesn't match but the manifest confirms this tool wrote exactly
    that older copy (or the manifest predates this file, in which case a
    file living in a folder only this tool writes to is assumed to be its
    own older copy rather than raising a false "modified" alarm on
    upgrade), and ``"modified"`` when it matches neither the current
    package nor its own last-known stamp -- something else changed it."""
    if not path.is_file():
        return "missing"
    try:
        on_disk = path.read_bytes()
    except OSError:
        return "missing"
    packaged = _packaged_bytes(name)
    if on_disk == packaged:
        return "ok"
    stamp = manifest.get(name)
    on_disk_hash = _sha256(on_disk)
    return "outdated" if stamp is None or stamp == on_disk_hash else "modified"


#: ROB-P9: a character a double-quoted JSON command string and Claude
#: Code's shell on Windows (Git Bash) cannot both carry safely. A quote
#: would close the string early; ``$`` or a backtick would let the shell
#: interpolate a variable or run a command instead of passing the path
#: through literally.
_UNSAFE_COMMAND_CHARS = ('"', "$", "`")


def _quote_for_command(path: str) -> str | None:
    """``path`` double-quoted for a hook command, or ``None`` when it
    holds something that quoting alone can't make safe (ROB-P9):

    - a quote, ``$`` or a backtick (:data:`_UNSAFE_COMMAND_CHARS`) --
      refused outright, rather than escaped, since the escape that is
      correct inside a POSIX double-quoted string (what Git Bash reads
      the command as) is not the same one that is correct for Windows's
      own argv parsing, and this one command string has to work as both;
    - a UNC path (``\\\\server\\share\\...``) -- its leading double
      backslash is itself a POSIX double-quote escape sequence for one
      literal backslash, so passing it through unescaped would silently
      collapse it to a single backslash and break the path.

    A single trailing backslash *is* escaped (doubled): both Windows's
    own argv parsing and a POSIX double-quoted string treat a backslash
    right before the closing quote as escaping that quote rather than
    ending the string, so an unmodified trailing backslash would swallow
    the closing ``"`` and run on into whatever follows.
    """
    if any(ch in path for ch in _UNSAFE_COMMAND_CHARS):
        return None
    if path.startswith("\\\\") or path.startswith("//"):
        return None
    n = len(path) - len(path.rstrip("\\"))
    if n:
        path = path + "\\" * n
    return f'"{path}"'


def _python_command(script: Path, python: str | None = None) -> str | None:
    """A hook command that names a Python and the script by their full
    paths, so it depends on neither PATH nor shell variables, with
    ``-I -S`` (ROB-P8: isolated mode plus no ``site`` import) so a
    stdlib-only hook script never picks up a ``PYTHON*`` environment
    variable, a ``sitecustomize.py``, or a ``.pth`` file from whatever
    happens to be on this machine. ``None`` when the Python or the
    script's path can't be safely written into a command string
    (:func:`_quote_for_command`, ROB-P9) -- the caller's job to refuse
    building the hook entry at all rather than write a broken or unsafe
    one."""
    quoted_python = _quote_for_command(python or stable_python())
    quoted_script = _quote_for_command(str(script))
    if quoted_python is None or quoted_script is None:
        return None
    return f"{quoted_python} -I -S {quoted_script}"


def _expand_percent_vars(command: str) -> str | None:
    """``command`` with each ``%VAR%`` replaced by its value here, or
    ``None`` when one isn't set."""
    missing = False

    def value(match: re.Match) -> str:
        nonlocal missing
        found = os.environ.get(match.group(0)[1:-1])
        if found is None:
            missing = True
            return match.group(0)
        return found

    expanded = _PERCENT_VAR_RE.sub(value, command)
    return None if missing else expanded


def _unescape_decoded(command: str) -> str:
    return "".join(_DECODED_ESCAPES.get(ch, ch) for ch in command)


def _newest_snapshot_days(config_dir: Path, now: datetime) -> float | None:
    newest = None
    for snap in snapshots.load_snapshots(config_dir):
        ts = snapshots._parse_ts(snap.ts)
        if ts is not None and (newest is None or ts > newest):
            newest = ts
    if newest is None:
        return None
    return max(0.0, (now - newest).total_seconds() / 86400)


def check(
    config_dir: str | Path,
    *,
    now: datetime | None = None,
    python: str | None = None,
    claude_root: str | Path | None = None,
    managed_dir: str | Path | None = None,
) -> HookHealth:
    """Inspect Claude Code's ``settings.json`` (see :func:`settings_path`)
    and the snapshot history in ``config_dir``. Never raises: an
    unreadable settings file reads as "no hook". ``python`` is the
    interpreter a fixed command names (default: the one running this
    code)."""
    config_dir = Path(config_dir)
    now = now or datetime.now(timezone.utc)
    health = HookHealth(settings_path=settings_path(claude_root))
    health.blocked_by = hook_policy(claude_root, managed_dir)
    health.last_snapshot_days = _newest_snapshot_days(config_dir, now)
    try:
        settings = json.loads(health.settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return health
    if not isinstance(settings, dict):
        return health

    command = next((c for c in _event_commands(settings, "SessionStart") if HOOK_SCRIPT_NAME in c), None)
    if command is None:
        return health
    health.command = command
    health.mis_escaped = any(ch in command for ch in _DECODED_ESCAPES)
    health.script_path = _script_path(command)
    health.script_exists = bool(health.script_path and health.script_path.is_file())
    health.interpreter = _interpreter(command)
    health.interpreter_found = health.interpreter is not None and _interpreter_found(health.interpreter)
    health.percent_vars = bool(_PERCENT_VAR_RE.search(command))

    if not health.ok:
        candidate = _unescape_decoded(command)
        candidate_path = _script_path(candidate)
        if candidate_path is None or not candidate_path.is_file():
            return health
        program = _interpreter(candidate)
        expanded = _expand_percent_vars(candidate) if _PERCENT_VAR_RE.search(candidate) else candidate
        if (
            program is not None
            and _interpreter_found(program)
            and expanded is not None
            and _script_path(expanded) is not None
            and _script_path(expanded).is_file()
        ):
            # Only the escaping or a %VAR% was wrong: keep the user's own
            # interpreter and arguments, with the variable written out.
            if expanded != command:
                health.fixed_command = expanded
        else:
            # Keep any arguments after the script (such as the
            # --config-dir init adds for a non-default data folder).
            # ROB-P9: a path that can't be safely written into a command
            # string at all leaves fixed_command None -- there is no fix
            # to offer, only "move it somewhere else and try again".
            fixed = _python_command(candidate_path.resolve(), python)
            if fixed is not None:
                health.fixed_command = fixed + _args_after_script(candidate)
    return health


def repair(health: HookHealth, *, now: datetime | None = None) -> Path:
    """Replace the broken command with ``health.fixed_command`` in
    ``settings.json``, after copying it to ``settings.json.bak-<UTC
    timestamp>``. Only that one string changes; the file is re-written
    with two-space indentation. Returns the backup path."""
    if health.command is None or health.fixed_command is None:
        raise ValueError("nothing to repair")
    now = now or datetime.now(timezone.utc)
    before = health.settings_path.read_text(encoding="utf-8")
    settings = json.loads(before)
    replaced = 0
    for event in list(settings.get("hooks", {})):
        for _matcher, entry in _event_entries(settings, event):
            if entry.get("command") == health.command:
                entry["command"] = health.fixed_command
                replaced += 1
    if replaced == 0:
        raise ValueError("the hook command changed since it was checked")
    backup = backup_path(health.settings_path, now)
    shutil.copy2(_settings_target(health.settings_path), backup)
    replace_settings(health.settings_path, json.dumps(settings, indent=2, ensure_ascii=False) + "\n", before)
    return backup


def backup_path(settings_path: Path, now: datetime) -> Path:
    """``settings.json.bak-<UTC timestamp>`` next to ``settings_path``,
    with ``-2``, ``-3``... added when two changes land in one second, so
    one backup never overwrites another."""
    base = f"{settings_path.name}.bak-{now.strftime('%Y%m%dT%H%M%SZ')}"
    candidate = settings_path.with_name(base)
    n = 2
    while candidate.exists():
        candidate = settings_path.with_name(f"{base}-{n}")
        n += 1
    return candidate


#: Session entrypoints that run in a terminal, where Claude Code runs a
#: statusline. The desktop app and claude.ai/code never do.
TERMINAL_ENTRYPOINTS = frozenset({"cli"})


def statusline_check(
    config_dir: str | Path, entrypoints: dict[str, dict], *, claude_root: str | Path | None = None
) -> tuple[bool, str]:
    """Is the statusline feeding the usage log? ``entrypoints`` is
    ``Store.entrypoint_counts()``. Returns (working, one plain sentence).
    Usage-limit amounts for Pro and Max plans come only from here."""
    from .footprint import is_own_statusline

    config_dir = Path(config_dir)
    try:
        settings = json.loads(settings_path(claude_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        settings = None
    last_logged = None
    try:
        with (config_dir / "usage-log.csv").open(encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                if row.get("source") == "statusline" and row.get("logged_at"):
                    last_logged = max(last_logged or "", row["logged_at"])
    except (OSError, csv.Error):
        pass
    terminal = sum(v.get("count", 0) for k, v in entrypoints.items() if k in TERMINAL_ENTRYPOINTS)
    total = sum(v.get("count", 0) for v in entrypoints.values())
    desktop_note = (
        f" {total - terminal} of your {total} sessions ran outside a terminal (for example in the desktop app), "
        "where Claude Code does not run a statusline, so usage limits are only logged from terminal sessions."
        if total and terminal < total
        else ""
    )
    if not is_own_statusline(settings if isinstance(settings, dict) else None):
        return False, (
            "The statusline is not set up, so usage limits are not logged and Pro and Max amounts show as "
            "list-price equivalents. Run 'claudeglass init --connect' to add it." + desktop_note
        )
    if total and not terminal:
        return False, (
            f"All {total} of your sessions ran outside a terminal (for example in the desktop app), where Claude "
            "Code does not run a statusline, so no usage limits are logged. Pro and Max amounts show as "
            "list-price equivalents. Using Claude Code in a terminal now and then is enough to calibrate them."
        )
    if last_logged is None:
        return False, "The statusline is set up but has not logged anything yet." + desktop_note
    newest_terminal = max(
        (v.get("last_ts") or "" for k, v in entrypoints.items() if k in TERMINAL_ENTRYPOINTS), default=""
    )
    when = last_logged[:16].replace("T", " ")
    if newest_terminal[:16] > last_logged[:16] and newest_terminal[:10] > last_logged[:10]:
        return False, (
            f"The statusline last logged at {when} UTC, but terminal sessions ran later. Check that its "
            "command still runs: 'claudeglass changes' shows it." + desktop_note
        )
    return True, f"The statusline last logged at {when} UTC." + desktop_note


@dataclass(slots=True)
class ConnectPlan:
    """What ``init`` would add to ``settings.json`` so Claude Code feeds
    this tool: the snapshot hook, and a statusline when none is set."""

    settings_path: Path
    #: Plain sentences, one per change.
    changes: list[str]
    #: A unified diff of settings.json before and after.
    diff: str
    #: The whole file after the change; ``None`` when nothing changes.
    new_text: str | None
    #: The file as the change was worked out from (``""`` when there was
    #: none); ``None`` when not known. :func:`connect` writes nothing if
    #: it no longer holds this.
    old_text: str | None = None


def plan_connect(
    config_dir: str | Path,
    *,
    hook_command: str,
    statusline_command: str | None,
    claude_root: str | Path | None = None,
    capture_specs_wanted: tuple[HookSpec, ...] = (),
    capture_commands: dict[str, str] | None = None,
) -> ConnectPlan:
    """Work out the ``settings.json`` change that connects this tool,
    without writing anything. The snapshot hook is added only when no
    SessionStart hook runs it yet (a broken one is :func:`repair`'s job);
    the statusline only when ``statusLine`` is unset, so a statusline of
    your own is never replaced. With ``capture_commands``, the capture
    entries are made to match ``capture_specs_wanted`` as well
    (:func:`plan_capture`)."""
    path, before, settings, refusal = _read_settings(claude_root)
    if refusal is not None:
        return refusal

    changes = []
    if not any(HOOK_SCRIPT_NAME in c for c in _event_commands(settings, "SessionStart")):
        hooks = settings.setdefault("hooks", {})
        if isinstance(hooks, dict) and isinstance(hooks.setdefault("SessionStart", []), list):
            # async: Claude Code starts the hook and carries on without
            # waiting for it; it prints nothing, so it adds no tokens.
            hooks["SessionStart"].append({"hooks": [{"type": "command", "command": hook_command, "async": True}]})
            changes.append(
                "Add a SessionStart hook that records your settings (key names, a few safe values and file sizes, "
                "never contents) when a session starts. It runs in the background and prints nothing, so it adds "
                "no tokens."
            )
    if statusline_command and "statusLine" not in settings:
        settings["statusLine"] = {"type": "command", "command": statusline_command}
        changes.append(
            "Add a statusline that logs your usage limits and cache health. It runs in the terminal only, "
            "not in the desktop app."
        )
    if capture_commands is not None:
        changes += _sync_capture_entries(settings, capture_specs_wanted, capture_commands)
    return _finish_plan(path, before, settings, changes)


#: This tool's statusline run as a module by a named Python:
#: ``"<python>" -m claudeglass.statusline ...`` (the ``.pyz`` form
#: carries its own code and needs no installed copy).
_STATUSLINE_PYTHON = re.compile(r'^"([^"]+)"(?= -m claudeglass\.statusline\b)')


def statusline_python(claude_root: str | Path | None = None) -> str | None:
    """The Python this tool's statusline runs its module with, or ``None``
    when there is no such statusline (or settings.json can't be read)."""
    _path, _before, settings, refusal = _read_settings(claude_root)
    status_line = settings.get("statusLine") if refusal is None else None
    command = status_line.get("command") if isinstance(status_line, dict) else None
    match = _STATUSLINE_PYTHON.match(command) if isinstance(command, str) else None
    return match.group(1) if match else None


def plan_statusline_python(python: str, *, claude_root: str | Path | None = None) -> ConnectPlan:
    """Point this tool's statusline at ``python`` when it runs the module
    with another one: that Python's copy is the one an update leaves
    behind, still on the old version (or gone). Only the interpreter
    changes; the rest of the command stays as written."""
    path, before, settings, refusal = _read_settings(claude_root)
    if refusal is not None:
        return refusal
    status_line = settings.get("statusLine")
    command = status_line.get("command") if isinstance(status_line, dict) else None
    match = _STATUSLINE_PYTHON.match(command) if isinstance(command, str) else None
    changes = []
    if match and os.path.normcase(os.path.abspath(match.group(1))) != os.path.normcase(os.path.abspath(python)):
        status_line["command"] = f'"{python}"' + command[match.end():]
        changes.append(
            f"Run the statusline with {python}, the Python this version is installed for, instead of "
            f"{match.group(1)}."
        )
    return _finish_plan(path, before, settings, changes)


def _read_settings(claude_root: str | Path | None) -> tuple[Path, str, dict, ConnectPlan | None]:
    """settings.json's path, text and parsed object, or a plan that
    refuses to change a file it can't read."""
    path = settings_path(claude_root)
    try:
        before = path.read_text(encoding="utf-8")
        settings = json.loads(before)
    except FileNotFoundError:
        return path, "", {}, None
    except (OSError, ValueError):
        return path, "", {}, ConnectPlan(path, ["settings.json could not be read, so nothing will be changed."], "", None)
    if not isinstance(settings, dict):
        return path, "", {}, ConnectPlan(path, ["settings.json is not a JSON object, so nothing will be changed."], "", None)
    return path, before, settings, None


def _finish_plan(path: Path, before: str, settings: dict, changes: list[str]) -> ConnectPlan:
    if not changes:
        return ConnectPlan(path, [], "", None)
    after = json.dumps(settings, indent=2, ensure_ascii=False) + "\n"
    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile="settings.json (now)",
            tofile="settings.json (after)",
        )
    )
    return ConnectPlan(path, changes, diff, after, before)


def _entry_spec(event: str, matcher: str, entry: dict) -> HookSpec | None:
    command = entry.get("command", "")
    script = next((name for name in CAPTURE_SCRIPTS if name in command), None)
    if script is None:
        return None
    return HookSpec(script, event, matcher or "", bool(entry.get("async")))


def _capture_entries(settings: dict) -> list[tuple[HookSpec, dict]]:
    hooks = settings.get("hooks")
    found = []
    for event in list(hooks) if isinstance(hooks, dict) else []:
        for matcher, entry in _event_entries(settings, event):
            spec = _entry_spec(event, matcher, entry)
            if spec is not None:
                found.append((spec, entry))
    return found


def _sync_capture_entries(settings: dict, wanted: tuple[HookSpec, ...], commands: dict[str, str]) -> list[str]:
    """Make ``settings`` run exactly the capture entries in ``wanted``,
    each with the command ``commands`` gives its script: entries no
    longer wanted, or with the wrong command, matcher or background
    setting, are replaced. Other hooks are never touched. Returns one
    plain sentence per change."""
    present = _capture_entries(settings)
    keep = {
        spec
        for spec, entry in present
        if spec in wanted and entry.get("command") == commands.get(spec.script) and entry.get("timeout") == CAPTURE_TIMEOUT_S
    }
    changes = []
    hooks = settings.get("hooks")
    if isinstance(hooks, dict):
        for event in list(hooks):
            groups = hooks[event]
            if not isinstance(groups, list):
                continue
            kept_groups = []
            for group in groups:
                entries = group.get("hooks") if isinstance(group, dict) else None
                if isinstance(entries, list):
                    matcher = group.get("matcher") if isinstance(group.get("matcher"), str) else ""
                    kept = []
                    for entry in entries:
                        spec = _entry_spec(event, matcher, entry) if isinstance(entry, dict) and isinstance(entry.get("command"), str) else None
                        if spec is None or spec in keep:
                            kept.append(entry)
                        elif spec not in wanted:
                            changes.append(f"Remove the capture hook that runs {spec.describe()}.")
                    if not kept:
                        continue
                    if len(kept) != len(entries):
                        group = {**group, "hooks": kept}
                kept_groups.append(group)
            if kept_groups:
                hooks[event] = kept_groups
            else:
                del hooks[event]
        if not hooks:
            settings.pop("hooks", None)
    for spec in wanted:
        if spec in keep:
            continue
        hooks = settings.setdefault("hooks", {})
        if not isinstance(hooks, dict) or not isinstance(hooks.setdefault(spec.event, []), list):
            continue  # a hooks section of an unexpected shape is left alone; check_capture reports the gap
        entry = {"type": "command", "command": commands[spec.script], "timeout": CAPTURE_TIMEOUT_S}
        if spec.async_:
            entry["async"] = True
        group = {"matcher": spec.matcher, "hooks": [entry]} if spec.matcher else {"hooks": [entry]}
        hooks[spec.event].append(group)
        replacing = any(s.event == spec.event and s.script == spec.script for s, _entry in present)
        changes.append(("Update" if replacing else "Add") + f" the capture hook that runs {spec.describe()}.")
    return changes


def remove_capture_entries(settings: dict) -> list[str]:
    """Take every capture entry out of ``settings``; one plain sentence
    per entry removed."""
    return _sync_capture_entries(settings, (), {})


def plan_capture(
    wanted: tuple[HookSpec, ...],
    commands: dict[str, str],
    *,
    claude_root: str | Path | None = None,
) -> ConnectPlan:
    """The settings.json change that makes it run exactly the capture
    entries in ``wanted`` (none, to take capture back out), without
    writing anything. ``commands`` maps each script to its command."""
    path, before, settings, refusal = _read_settings(claude_root)
    if refusal is not None:
        return refusal
    return _finish_plan(path, before, settings, _sync_capture_entries(settings, tuple(wanted), commands))


@dataclass(slots=True)
class CaptureHookHealth:
    """Whether settings.json runs the capture entries the chosen metrics
    need."""

    settings_path: Path
    needed: tuple[HookSpec, ...] = ()
    #: Needed entries settings.json lacks.
    missing: tuple[HookSpec, ...] = ()
    #: Capture entries no chosen metric needs. Harmless: the hook adds
    #: nothing for a metric that is off.
    extra: tuple[HookSpec, ...] = ()
    #: Needed entries whose script or catalogue is this tool's own older
    #: copy (SEC-P7/ROB-P7): ``refresh_hook_files`` or ``capture connect``
    #: fixes it.
    outdated: tuple[HookSpec, ...] = ()
    #: Needed entries whose script or catalogue matches neither this
    #: tool's current package nor its own last-known stamp -- changed by
    #: something else since this tool wrote it. Reported, never rewritten.
    modified: tuple[HookSpec, ...] = ()
    #: Plain sentences, one per problem with an entry that is there.
    problems: list[str] = field(default_factory=list)
    #: A :data:`POLICY_TEXT` key when a settings policy stops Claude Code
    #: running these entries at all (:func:`hook_policy`); ``capture
    #: connect`` can't fix that.
    blocked_by: str | None = None

    @property
    def ok(self) -> bool:
        return not self.missing and not self.problems and self.blocked_by is None

    def summary(self) -> str:
        if not self.needed and not self.extra:
            return "No capture hooks are needed or installed."
        if self.blocked_by is not None:
            return POLICY_TEXT[self.blocked_by]
        if self.ok:
            return "The capture hooks are set up." + (
                " settings.json also runs capture hooks no chosen metric needs; they add nothing."
                if self.extra
                else ""
            )
        parts = [f"settings.json does not run {spec.describe()}." for spec in self.missing] + self.problems
        return " ".join(parts) + " Run 'claudeglass capture connect' to fix it."


def check_capture(
    wanted: tuple[HookSpec, ...],
    *,
    claude_root: str | Path | None = None,
    config_dir: str | Path | None = None,
    check_python: bool = False,
    managed_dir: str | Path | None = None,
) -> CaptureHookHealth:
    """Compare settings.json's capture entries with ``wanted``. Never
    raises: an unreadable settings file reads as no entries.

    With ``config_dir``, each entry's launcher, module and catalogue (SEC-P7/ROB-P7)
    are also hash-stamp checked against the manifest :func:`install_hook_files`
    writes, adding to ``.outdated``/``.modified`` -- cheap (a few files
    hashed, no transcript read), so safe for a hot status path. With
    ``check_python`` too, each distinct interpreter is also asked its own
    version once (:func:`_interpreter_version`, a bounded subprocess
    call) and flagged when older than 3.11, since the hook
    needs ``tomllib`` to read config.toml at all -- left off by default
    since spawning a process isn't free."""
    health = CaptureHookHealth(settings_path=settings_path(claude_root), needed=tuple(wanted))
    health.blocked_by = hook_policy(claude_root, managed_dir)
    try:
        settings = json.loads(health.settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        settings = {}
    present = _capture_entries(settings if isinstance(settings, dict) else {})
    specs = [spec for spec, _entry in present]
    health.missing = tuple(spec for spec in wanted if spec not in specs)
    health.extra = tuple(spec for spec in specs if spec not in wanted)
    manifest = _load_manifest(config_dir) if config_dir is not None else {}
    outdated: list[HookSpec] = []
    modified: list[HookSpec] = []
    python_versions: dict[str, tuple[int, int] | None] = {}
    for spec, entry in present:
        if spec not in wanted:
            continue
        command = entry["command"]
        if any(ch in command for ch in _DECODED_ESCAPES):
            health.problems.append(f"The command for {spec.describe()} has a broken path (a single backslash in JSON).")
            continue
        script = _script_path_for(command, spec.script)
        if script is None or not script.is_file():
            health.problems.append(f"The command for {spec.describe()} runs {script or spec.script}, which does not exist.")
        elif config_dir is not None:
            worst = "ok"
            for name in CAPTURE_FILES.get(spec.script, (spec.script,)):
                target = script if name == spec.script else script.with_name(name)
                state = _file_provenance(target, name, manifest)
                if state == "missing" and name == capture_catalogue.HOOK_MODULE:
                    health.problems.append(
                        f"The module {name} next to {spec.script} is missing, so the hook does nothing. "
                        "'claudeglass capture connect' puts it back."
                    )
                elif state == "missing" and name != spec.script:
                    health.problems.append(
                        f"The catalogue {name} next to {spec.script} is missing, so its notes fall back to "
                        "plain wording."
                    )
                elif state == "modified":
                    worst = "modified"
                elif state == "outdated" and worst != "modified":
                    worst = "outdated"
            if worst == "modified":
                modified.append(spec)
                health.problems.append(
                    f"The command for {spec.describe()} does not match what claudeglass installed or "
                    "ships now, so it may have been edited by hand."
                )
            elif worst == "outdated":
                outdated.append(spec)
                health.problems.append(
                    f"The command for {spec.describe()} runs an older copy than this version of "
                    "claudeglass ships. 'claudeglass capture connect' refreshes it, as does the "
                    "next 'update' or dashboard restart."
                )
        program = _interpreter(command)
        if program is None or not _interpreter_found(program):
            health.problems.append(f"The command for {spec.describe()} starts '{program}', which is not installed or not on your PATH.")
        elif check_python:
            if program not in python_versions:
                python_versions[program] = _interpreter_version(program)
            version = python_versions[program]
            if version is not None and version < _MIN_PYTHON:
                health.problems.append(
                    f"The command for {spec.describe()} starts Python {version[0]}.{version[1]}, older than "
                    "3.11, so it can't read config.toml (tomllib) and capture stays off."
                )
        if _PERCENT_VAR_RE.search(command):
            health.problems.append(f"The command for {spec.describe()} uses a %VARIABLE%, which Git Bash does not expand.")
    health.outdated = tuple(outdated)
    health.modified = tuple(modified)
    return health


def _script_path_for(command: str, script_name: str) -> Path | None:
    match = re.search(r'"([^"]*' + re.escape(script_name) + r')"', command) or re.search(
        r"(\S*" + re.escape(script_name) + r")", command
    )
    return Path(_expand(match.group(1))) if match else None


def hook_command(script: Path, extra_args: str = "", python: str | None = None) -> str | None:
    """The command a hook entry runs: a Python and ``script`` by their
    full paths, then ``extra_args`` as written. ``None`` (ROB-P9) when
    the Python or the script's own path can't be safely written into a
    command string -- the caller's job to refuse the hook entry rather
    than write one."""
    command = _python_command(script, python)
    return command + extra_args if command is not None else None


def install_hook_files(config_dir: str | Path, names) -> list[Path]:
    """Copy the packaged ``hooks/<name>`` files into
    ``<config_dir>/hooks/``, replacing older copies, and stamp each in
    the SHA-256 manifest (:func:`_save_manifest`) so a later
    :func:`check_capture` can tell this tool's own older copy apart from
    one someone else changed. Reads the packaged bytes through
    ``importlib.resources``, so it works from a ``.pyz`` too. When the
    final rename fails (ROB-P7: a locked file on Windows, say), that one
    file is skipped -- its previous copy is left running rather than the
    whole install failing -- and it keeps its old manifest stamp, so
    :func:`check_capture` still reports it accurately. A capture script
    (the launcher) is written last, and only once the module it imports is
    there: it is the file that starts using the others, so an install cut
    short never leaves one pointing at nothing. Returns the paths actually
    written."""
    dest_dir = Path(config_dir) / "hooks"
    dest_dir.mkdir(parents=True, exist_ok=True)
    written = []
    manifest = _load_manifest(config_dir)
    changed = False
    for name in sorted(names, key=lambda name: name in CAPTURE_SCRIPTS):
        if name in CAPTURE_SCRIPTS and not (dest_dir / capture_catalogue.HOOK_MODULE).is_file():
            continue
        data = (importlib.resources.files("claudeglass") / "hooks" / name).read_bytes()
        dest = dest_dir / name
        tmp = dest.with_name(f"{name}.{os.getpid()}.tmp")
        tmp.write_bytes(data)
        try:
            os.replace(tmp, dest)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass
            continue
        manifest[name] = _sha256(data)
        changed = True
        written.append(dest)
    if changed:
        _save_manifest(config_dir, manifest)
    return written


def refresh_hook_files(config_dir: str | Path) -> list[Path]:
    """Re-copy each packaged hook file already installed under
    ``<config_dir>/hooks`` (:data:`ALL_HOOK_FILES`) whose on-disk copy is
    only outdated -- this tool's own older copy, per
    :func:`_file_provenance` -- never one :func:`check_capture` would
    call modified. Called when ``update`` finishes and when ``serve``
    starts (ROB-P7), so a newer pip install reaches the hook scripts
    Claude Code actually runs without waiting for the next ``capture
    connect``. A file nothing has installed yet, or one already current,
    is left alone, except that a capture script that is installed and
    isn't hand-edited gets back any of its own files that are missing: an
    install from before the hook moved into a module has none, and the
    launcher that replaces its script needs one. Returns the paths
    actually rewritten."""
    dest_dir = Path(config_dir) / "hooks"
    if not dest_dir.is_dir():
        return []
    manifest = _load_manifest(config_dir)
    states = {name: _file_provenance(dest_dir / name, name, manifest) for name in sorted(ALL_HOOK_FILES)}
    stale = []
    healed = False
    for name, state in states.items():
        if state == "ok":
            packaged_hash = _sha256(_packaged_bytes(name))
            if manifest.get(name) != packaged_hash:
                manifest[name] = packaged_hash  # self-heal a manifest that predates this file
                healed = True
        elif state == "outdated":
            stale.append(name)
    for script, names in CAPTURE_FILES.items():
        if states.get(script) in ("missing", "modified"):
            continue
        stale += [name for name in names if states[name] == "missing" and name not in stale]
    if healed and not stale:
        _save_manifest(config_dir, manifest)
    if stale:
        return install_hook_files(config_dir, stale)
    return []


class SettingsChanged(ValueError):
    """settings.json changed between working out an edit and writing it."""

    def __init__(self, path: Path):
        super().__init__(
            f"{path} changed while this was waiting (Claude Code writes it too, when you allow a tool for good, "
            "say), so nothing was written. Run the command again to work the change out from the file as it is now."
        )


def _settings_target(path: Path) -> Path:
    """The file to write for ``path``: a symlinked settings.json (kept in
    a dotfiles folder, say) is written through, never replaced by a
    plain file."""
    return path.resolve() if path.is_symlink() else path


def _current_text(path: Path) -> str:
    try:
        return _settings_target(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def check_unchanged(path: Path, old_text: str | None) -> None:
    """Raise :class:`SettingsChanged` when ``path`` no longer holds
    ``old_text`` (``""``: no file); ``None`` checks nothing."""
    if old_text is not None and _current_text(path) != old_text:
        raise SettingsChanged(path)


def replace_settings(path: Path, new_text: str, old_text: str | None = None) -> None:
    """Write ``new_text`` to ``path`` in one step: a temp file beside it,
    then ``os.replace``, keeping the file's permissions, so a reader never
    sees half a file. Raises :class:`SettingsChanged`, writing nothing,
    when the file no longer holds ``old_text`` (what the edit was worked
    out from): an edit planned before a yes/no question must never undo
    one Claude Code made while it waited."""
    check_unchanged(path, old_text)
    target = _settings_target(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(new_text, encoding="utf-8")
        if target.exists():
            shutil.copymode(target, tmp)
        os.replace(tmp, target)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def connect(plan: ConnectPlan, *, now: datetime | None = None) -> Path | None:
    """Write ``plan`` after backing up the current file to
    ``settings.json.bak-<UTC timestamp>``. Returns the backup path, or
    ``None`` when there was no file to back up. Raises
    :class:`SettingsChanged`, writing nothing, when the file changed since
    the plan was worked out."""
    if plan.new_text is None:
        raise ValueError("nothing to change")
    check_unchanged(plan.settings_path, plan.old_text)
    now = now or datetime.now(timezone.utc)
    backup = None
    if plan.settings_path.exists():
        backup = backup_path(plan.settings_path, now)
        shutil.copy2(_settings_target(plan.settings_path), backup)
    replace_settings(plan.settings_path, plan.new_text, plan.old_text)
    return backup


# -- SURV-HE: non-blocking hook errors, by hook event (S6) -------------------

#: A hook call counts toward a hook event's tally only for these two
#: outcomes -- a non-blocking error is a silent failure worth flagging
#: (S6: "That's latency on every call, and it would bury capture-hook
#: failures"); a *blocking* error is often a hook working exactly as
#: designed (e.g. a permission-denial hook), so it's left out of both the
#: numerator and the denominator here rather than counted as a "failure";
#: hook_system_message/hook_cancelled/capture_note aren't a pass/fail
#: outcome of the hook itself and are left out too.
_HOOK_CALL_SUBKINDS = frozenset({"hook_success", "hook_non_blocking_error"})

#: Below this many calls in the window, a hook's error rate is too noisy
#: to act on (a hook that ran twice and failed once is not "fails on
#: most calls" in any useful sense) -- same reasoning as
#: ``config.min_sessions``/``min_turns`` gating other corpus-wide advice.
_MIN_CALLS_FOR_RECOMMENDATION = 20

#: SURV-HE's own recommendation threshold: "fails on most calls".
_RECOMMEND_ERROR_RATE = 0.5


@dataclass(slots=True)
class HookErrorStat:
    """One hook event's non-blocking call/error tally over a window."""

    hook_name: str
    calls: int = 0
    errors: int = 0
    #: The latest failure's timestamp, as the transcript wrote it.
    last_error_ts: str | None = None

    @property
    def error_rate(self) -> float:
        return self.errors / self.calls if self.calls else 0.0


@dataclass(slots=True)
class HookErrorHealth:
    """Non-blocking hook errors seen across a corpus, tallied by hook
    event name (SURV-HE, S6: one real corpus had 47,858 non-blocking
    PreToolUse errors, almost all from one Bash hook -- latency on every
    matching tool call, and enough noise to bury a genuine capture-hook
    failure among it). Built by :func:`count_hook_errors`.
    """

    stats: tuple[HookErrorStat, ...] = ()

    def worst(self) -> HookErrorStat | None:
        """The highest error-rate hook with at least
        :data:`_MIN_CALLS_FOR_RECOMMENDATION` calls, or ``None`` when no
        hook has enough calls to judge."""
        candidates = [s for s in self.stats if s.calls >= _MIN_CALLS_FOR_RECOMMENDATION]
        if not candidates:
            return None
        return max(candidates, key=lambda s: s.error_rate)

    def recommendation(self) -> str | None:
        """A plain-English prompt naming the worst hook, its failure
        share, where it's configured, the trade-off and the undo --
        ``None`` when nothing crosses :data:`_RECOMMEND_ERROR_RATE`.
        Text only: this ships as a prompt, never as an edit -- nothing in
        this module ever touches settings.json on its account."""
        worst = self.worst()
        if worst is None or worst.error_rate < _RECOMMEND_ERROR_RATE:
            return None
        pct = round(worst.error_rate * 100)
        # A hook fixed mid-window keeps its old failures until they age
        # out, so say when the last one was.
        last = ""
        try:
            at = datetime.fromisoformat(worst.last_error_ts.replace("Z", "+00:00")) if worst.last_error_ts else None
        except ValueError:
            at = None
        if at is not None and at.tzinfo is not None:
            when = at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            last = f"The last failure was at {when}; if you fixed it since, this clears as the window moves on. "
        # Claude Code writes a hook_success attachment only for some passing
        # runs (one real corpus: 2 PreToolUse successes against 38,840
        # non-blocking errors), so the share is of *recorded* runs and can
        # overstate the real one -- the count leads. The hook can sit in
        # any settings layer or a plugin, not just the user settings.json.
        return (
            f"Your {worst.hook_name} hook(s) failed (non-blocking) {worst.errors} times this window, "
            f"{pct}% of the {worst.calls} runs Claude Code recorded (it doesn't record every run that "
            f"passes, so the real share can be lower). {last}To find which one, check hooks.{worst.hook_name} in "
            "~/.claude/settings.json, in each project's .claude/settings.json and .claude/settings.local.json, "
            "and in your enabled plugins. Trade-off: every failing "
            "call still adds that hook's own latency before the tool runs, and a hook failing this often can "
            "bury a real capture-hook failure in the same noise; the failing entry is probably doing little for "
            "you either way. Undo: whatever it was for stops working once you remove or fix it, so put it back "
            "if you need it."
        )


def count_hook_errors(results: Iterable[TranscriptResult], stopped: Iterable[str] = ()) -> HookErrorHealth:
    """Tally ``HOOK_OUTPUT`` events across ``results`` (already-parsed
    transcripts -- this module never reads or parses one itself, matching
    :func:`check_capture`'s own "cheap, no transcript read here"
    contract; the caller does the parsing, e.g. via ``corpus.load_corpus``)
    by hook event name (never the matcher/tool-name suffix -- see
    ``events._hook_name_bucket``'s docstring for why).

    ``stopped`` names hooks (by label) that have stopped failing
    (``hook_costs.HookRow.stopped``): their old failures are left out, so
    a fixed hook doesn't keep the warning up until they age out.
    """
    quiet = frozenset(stopped)
    tally: dict[str, HookErrorStat] = {}
    for result in results:
        for event in result.events:
            if event.kind != EventKind.HOOK_OUTPUT or event.subkind not in _HOOK_CALL_SUBKINDS:
                continue
            name = event.detail.get("hookName")
            if not isinstance(name, str):
                continue
            if event.subkind == "hook_non_blocking_error" and event.detail.get("script") in quiet:
                continue
            stat = tally.setdefault(name, HookErrorStat(hook_name=name))
            stat.calls += 1
            if event.subkind == "hook_non_blocking_error":
                stat.errors += 1
                if event.ts and (stat.last_error_ts is None or event.ts > stat.last_error_ts):
                    stat.last_error_ts = event.ts
    return HookErrorHealth(stats=tuple(tally[name] for name in sorted(tally)))


# -- CAP-9: Deep's measured wait (F10) ---------------------------------------


@dataclass(slots=True)
class DeepWaitStats:
    """How long Deep's big_output/web PostToolUse hook actually took, from
    real ``durationMs`` values on ClaudeGlass's own calls (F10: this used
    to be dropped, and the catalogue guessed "a fraction of a second"
    with no source behind it). Only a median and a p90 are kept -- never
    the raw per-call durations -- built by :func:`measure_deep_wait`.
    """

    calls: int = 0
    median_ms: float | None = None
    p90_ms: float | None = None

    def summary(self) -> str | None:
        """``"Deep waited ~=N s this week"``, or ``None`` with no calls to
        measure from (capture off, Deep's tool-note metrics off, or no
        matching tool result yet)."""
        if not self.calls or self.median_ms is None or self.p90_ms is None:
            return None
        return (
            f"Deep's large-output/web hook waited ≈{self.median_ms / 1000:.1f}s (median, "
            f"p90 ≈{self.p90_ms / 1000:.1f}s) over {self.calls} calls this week."
        )


def _percentile(sorted_values: list[float], fraction: float) -> float:
    """Nearest-rank percentile; fine for the handful of calls a week
    of Deep hook activity produces -- no interpolation needed."""
    index = min(len(sorted_values) - 1, int(fraction * len(sorted_values)))
    return sorted_values[index]


def measure_deep_wait(results: Iterable[TranscriptResult]) -> DeepWaitStats:
    """Median/p90 ``durationMs`` (:func:`_hook_output_detail` <- events.py)
    across every PostToolUse call ClaudeGlass's own capture hook made in
    ``results`` (already-parsed transcripts -- same "no I/O here"
    contract as :func:`count_hook_errors`) -- every outcome counts, not
    only a successful one, because Claude Code waited for the hook to
    finish either way.
    """
    durations: list[float] = []
    for result in results:
        for event in result.events:
            if event.kind != EventKind.HOOK_OUTPUT:
                continue
            if event.detail.get("hookName") != "PostToolUse" or not event.detail.get("capture"):
                continue
            duration = event.detail.get("durationMs")
            if isinstance(duration, (int, float)):
                durations.append(float(duration))
    if not durations:
        return DeepWaitStats()
    durations.sort()
    return DeepWaitStats(
        calls=len(durations),
        median_ms=_percentile(durations, 0.5),
        p90_ms=_percentile(durations, 0.9),
    )


__all__ = [
    "ALL_HOOK_FILES",
    "CAPTURE_SCRIPTS",
    "CaptureHookHealth",
    "ConnectPlan",
    "DeepWaitStats",
    "HOOK_SCRIPT_NAME",
    "HookErrorHealth",
    "HookErrorStat",
    "HookHealth",
    "HookSpec",
    "POLICY_ALL_OFF",
    "POLICY_ALL_OFF_MANAGED",
    "POLICY_MANAGED_ONLY",
    "POLICY_TEXT",
    "backup_path",
    "capture_specs",
    "check",
    "check_capture",
    "connect",
    "count_hook_errors",
    "hook_command",
    "hook_policy",
    "install_hook_files",
    "managed_settings_dir",
    "measure_deep_wait",
    "plan_capture",
    "plan_connect",
    "refresh_hook_files",
    "remove_capture_entries",
    "repair",
    "settings_path",
]

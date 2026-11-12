"""How many times a Bash or PowerShell command reads files, so a read made
through the shell is counted like one made with Read, Grep or Glob
(``Turn.shell_read_count``, with the size of the result in
``Turn.shell_read_chars``).

A pipeline counts once, by the command that starts it: ``grep``, ``egrep``,
``fgrep``, ``rg`` and ``findstr``; ``sed -n``; ``cat``, ``head`` and
``tail`` given a file; ``find``; ``git log``, ``show``, ``diff`` and
``grep``; PowerShell's ``Get-Content`` (``gc``, ``type``) and
``Select-String`` (``sls``). What follows a pipe filters that output and
isn't another read, and a leading ``cd X &&`` is a pipeline of its own that
reads nothing. Two pipelines that list files to search them are one read
each: ``git ls-files | xargs grep`` and, in PowerShell,
``Get-ChildItem -Recurse | Select-String``.
A read after ``||`` is a fallback for the read before it
(``cat a 2>/dev/null || cat b``) and is not counted again.

Not a read: a command whose output goes to a file (``cat a b > c``,
``git diff > x.patch``) or is fed by a heredoc (``cat <<EOF``), ``sed -i``,
``find -delete``, ``cat`` or ``head`` with no file (they read standard
input), ``ls``, ``wc`` and ``git status``.

The command is read here and dropped: only the count is returned.
"""

from __future__ import annotations

import re

from . import shell_writes
from .shell_writes import SimpleCommand

#: Cheap pre-check so most commands skip tokenising altogether.
_MAYBE_READS_RE = re.compile(
    r"\b(?:grep|egrep|fgrep|rg|findstr|sed|cat|head|tail|find|git|get-content|gc|type|select-string|sls)\b",
    re.IGNORECASE,
)

#: Programs that search or list files by themselves.
_SEARCH_PROGRAMS = frozenset({"grep", "egrep", "fgrep", "rg", "findstr", "select-string", "sls"})

#: Programs that read the files they are given: with none, they read
#: standard input.
_FILE_PROGRAMS = frozenset({"cat", "head", "tail", "get-content", "gc"})

#: PowerShell's name for ``Get-Content``; in Bash ``type`` finds a command.
_POWERSHELL_FILE_PROGRAMS = frozenset({"type"})

_GIT_READS = frozenset({"log", "show", "diff", "grep"})

#: PowerShell's recursive listing (``Get-ChildItem`` and its aliases) and
#: the commands that search the files it lists.
_POWERSHELL_LISTINGS = frozenset({"get-childitem", "gci", "ls", "dir"})
_POWERSHELL_SEARCHES = frozenset({"select-string", "sls"})

#: ``xargs`` options that take their value in the next word.
_XARGS_VALUE_OPTIONS = frozenset({"-n", "-P", "-I", "-L", "-s", "-d", "-E", "-a", "-l"})

#: ``git`` options before the subcommand that take a value in the next word.
_GIT_VALUE_OPTIONS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})

_SED_QUIET_RE = re.compile(r"^-[A-Za-z]*n[A-Za-z]*$")
_SED_IN_PLACE_RE = re.compile(r"^-[A-Za-z]*i")


def read_count(command: str, *, powershell: bool) -> int:
    """How many of ``command``'s pipelines read files (see the module
    docstring)."""
    if not _MAYBE_READS_RE.search(command):
        return 0
    count = 0
    previous = False
    for pipeline in shell_writes.simple_commands(command, powershell=powershell):
        reads = _pipeline_reads(pipeline, powershell)
        if reads and not (pipeline[0].fallback and previous):
            count += 1
        previous = reads
    return count


def _pipeline_reads(pipeline: list[SimpleCommand], powershell: bool) -> bool:
    """Whether the pipeline reads: its first command does, or it lists
    files for a search that follows (``git ls-files | xargs grep``,
    ``Get-ChildItem -Recurse | Select-String``)."""
    first = pipeline[0]
    if _reads(first, powershell):
        return True
    if first.redirected or first.fed:
        return False
    later = pipeline[1:]
    if first.program == "git" and _git_subcommand(first.args) == "ls-files":
        return any(cmd.program == "xargs" and not cmd.redirected and _xargs_runs_search(cmd.args) for cmd in later)
    if powershell and first.program in _POWERSHELL_LISTINGS and _recurses(first.args):
        return any(cmd.program in _POWERSHELL_SEARCHES and not cmd.redirected for cmd in later)
    return False


def _recurses(args: tuple[str, ...]) -> bool:
    """A ``-Recurse`` switch, or any shortening of it PowerShell accepts
    (``-r``, ``-rec``)."""
    return any(len(word) > 1 and "-recurse".startswith(word.lower()) for word in args)


def _xargs_runs_search(args: tuple[str, ...]) -> bool:
    """Whether the program ``xargs`` runs (the first word after its own
    options) is a search."""
    skip = False
    for word in args:
        if skip:
            skip = False
        elif word in _XARGS_VALUE_OPTIONS:
            skip = True
        elif not word.startswith("-"):
            return word in _SEARCH_PROGRAMS
    return False


def _reads(cmd: SimpleCommand, powershell: bool) -> bool:
    if cmd.redirected or cmd.fed:
        return False
    program = cmd.program
    if program in _SEARCH_PROGRAMS:
        return True
    if program in _FILE_PROGRAMS or (powershell and program in _POWERSHELL_FILE_PROGRAMS):
        return any(not word.startswith("-") for word in cmd.args)
    if program == "find":
        return "-delete" not in cmd.args
    if program == "sed":
        return _sed_prints(cmd.args)
    return program == "git" and _git_subcommand(cmd.args) in _GIT_READS


def _sed_prints(args: tuple[str, ...]) -> bool:
    """``sed -n``, not ``-i``: it prints chosen lines and edits nothing."""
    flags = [word for word in args if word.startswith("-") and not word.startswith("--")]
    quiet = "--quiet" in args or "--silent" in args or any(_SED_QUIET_RE.match(word) for word in flags)
    in_place = any(word.startswith("--in-place") for word in args) or any(_SED_IN_PLACE_RE.match(word) for word in flags)
    return quiet and not in_place


def _git_subcommand(args: tuple[str, ...]) -> str:
    """The word after ``git``'s own options (``git -C dir --no-pager log``
    is ``log``), or ``""`` when there isn't one."""
    skip = False
    for word in args:
        if skip:
            skip = False
        elif word in _GIT_VALUE_OPTIONS:
            skip = True
        elif not word.startswith("-"):
            return word
    return ""

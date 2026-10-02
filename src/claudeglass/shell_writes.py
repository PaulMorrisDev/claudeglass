"""Which files a Bash or PowerShell command writes, so an edit made
through a shell command is recorded like one made with Edit or Write
(``Turn.edit_target_hashes``, as salted hashes only), and counted
(``Turn.shell_write_count``: outside the temp dir, with no salt needed,
a count only).

Only writes whose content the command itself authors count:

- in-place edits: ``sed -i`` and ``perl -i``;
- PowerShell's ``Set-Content``/``Add-Content`` and ``[IO.File]::WriteAll*``/
  ``AppendAll*`` with an absolute path;
- a ``>``/``>>`` redirection, ``tee``, ``Out-File`` or ``Tee-Object`` fed
  by content the command supplies: ``cat`` (with a heredoc or files),
  ``echo``, ``printf``, ``Get-Content``, a literal string, a variable or a
  here-string at the start of the pipeline.

Capturing a program's output (``npm test > test.log``,
``pytest 2>&1 | tee out.txt``) writes a log, not an edit, and isn't
counted; neither are stderr redirections. Paths holding a variable, a glob
or ``~``, and device files (``/dev/null``, ``NUL``, ``$null``), are
skipped. A relative path is resolved against the directory the command ran
in (the transcript line's ``cwd``, then any ``cd``/``Set-Location`` earlier
in the same command), and skipped when that isn't known. A .NET write
resolves against the process directory, not the shell's, so only an
absolute one counts.

The command is read here and dropped: :func:`write_targets` returns the
paths for the caller to hash and count, then discard.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Cheap pre-check so most commands skip tokenising altogether.
_MAYBE_WRITES_RE = re.compile(
    r">|\btee\b|\bsed\b|\bperl\b|-content\b|\bsc\b|\bac\b|out-file|tee-object|::", re.IGNORECASE
)

#: ``[IO.File]::WriteAllText('C:\x', ...)`` with a literal first argument.
_DOTNET_WRITE_RE = re.compile(
    r"\[(?:System\.)?IO\.File\]::(?:WriteAll(?:Text|Lines|Bytes)|AppendAll(?:Text|Lines))"
    r"\(\s*(?:'([^']*)'|\"([^\"$`]*)\")",
    re.IGNORECASE,
)

#: Redirections that write the command's standard output to a file.
_STDOUT_REDIRECTS = frozenset({">", ">>", ">|", "1>", "1>>", "1>|", "&>", "&>>"})

#: Leading words that aren't the program itself.
_PREFIX_WORDS = frozenset(
    {"sudo", "command", "builtin", "exec", "time", "nohup", "!", "{", "}", "then", "do", "else", "elif", "if",
     "while", "until"}
)

#: Programs whose output is content the command authored (see the module
#: docstring), per shell.
_BASH_AUTHORING = frozenset({"cat", "echo", "printf"})
_PS_AUTHORING = frozenset({"echo", "write-output", "write", "get-content", "gc", "cat", "type"})

_CD_PROGRAMS = frozenset({"cd", "chdir", "pushd", "set-location", "sl", "push-location"})

#: PowerShell parameters naming the file written, and those taking a value
#: that isn't (matched by prefix, as PowerShell allows).
_PS_PATH_PARAMS = frozenset({"path", "literalpath", "filepath", "pspath", "lp"})
_PS_VALUE_PARAMS = (
    "value", "encoding", "width", "inputobject", "delimiter", "stream", "filter", "include", "exclude",
    "credential", "variable", "erroraction", "warningaction", "informationaction", "errorvariable",
    "outvariable", "outbuffer", "pipelinevariable", "warningvariable", "informationvariable",
)

_DEVICES = frozenset({"nul", "nul:", "con", "con:", "/dev/null"})
_UNUSABLE_CHARS = frozenset("$*?`%{}<>|\n")
_ABSOLUTE_RE = re.compile(r"^(?:[A-Za-z]:/|/)")
_DRIVE_RELATIVE_RE = re.compile(r"^[A-Za-z]:")
_FD_RE = re.compile(r"^(?:\d+|\*)$")
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_]\w*=")
_BASH_OPERATOR_CHARS = frozenset(" \t\r\n;|&<>()")


@dataclass(slots=True)
class _Command:
    """One simple command: its words, where its standard output is
    redirected, and whether a heredoc or here-string feeds it."""

    words: list[tuple[str, bool]] = field(default_factory=list)
    writes: list[str] = field(default_factory=list)
    fed: bool = False


def write_targets(command: str, *, powershell: bool, cwd: str | None) -> list[str]:
    """The files ``command`` writes (see the module docstring), each as an
    absolute path with forward slashes, in order, repeats kept."""
    if not _MAYBE_WRITES_RE.search(command):
        return []
    targets: list[str] = []
    if powershell:
        for match in _DOTNET_WRITE_RE.finditer(command):
            raw = match.group(1) if match.group(1) is not None else match.group(2)
            resolved = _absolute(raw, None) if _usable(raw) else None
            if resolved is not None:
                targets.append(resolved)
    here = _absolute(cwd, None) if isinstance(cwd, str) and cwd else None
    for pipeline in _pipelines(command, powershell):
        authored = _authored(pipeline[0], powershell)
        for position, cmd in enumerate(pipeline):
            program, args = _program(cmd.words, powershell)
            if program in _CD_PROGRAMS:
                here = _cd(args, here, powershell)
                continue
            paths = list(cmd.writes) if authored else []
            paths += _program_writes(program, args, powershell, piped_authored=authored and position > 0)
            for raw in paths:
                resolved = _absolute(raw, here) if _usable(raw) else None
                if resolved is not None:
                    targets.append(resolved)
    return targets


# -- reading the command ------------------------------------------------------------


def _pipelines(command: str, powershell: bool) -> list[list[_Command]]:
    """Split ``command`` into pipelines of simple commands. Quotes,
    escapes, comments, heredocs and here-strings are honoured; anything
    fancier (nested substitutions, arithmetic) may split oddly, which at
    worst yields a path that matches nothing."""
    pipelines: list[list[_Command]] = [[_Command()]]
    expect: str | None = None
    for kind, text, quoted in _tokens(command, powershell):
        cmd = pipelines[-1][-1]
        if kind == "word":
            if expect == "write":
                cmd.writes.append(text)
            elif expect == "fed":
                cmd.fed = True
            elif expect is None:
                cmd.words.append((text, quoted))
            expect = None
        elif kind == "fed":
            cmd.fed = True
            expect = None
        elif kind == "redirect":
            expect = "write" if text in _STDOUT_REDIRECTS else "fed" if text == "<<<" else "skip"
        else:
            expect = None
            if text == "|":
                pipelines[-1].append(_Command())
            else:
                pipelines.append([_Command()])
    return [p for p in pipelines if any(c.words or c.writes or c.fed for c in p)]


def _tokens(command: str, powershell: bool):
    """Yield ``(kind, text, quoted)``: ``word``, ``redirect`` (``>``,
    ``2>``, ``>&``, ``<``, ...), ``sep`` (``|``, ``;``, ``&&``, newline,
    ...) or ``fed`` (a heredoc or here-string, whose body is skipped)."""
    n = len(command)
    i = 0
    word: list[str] = []
    in_word = False
    quoted = False
    heredocs: list[tuple[str, bool]] = []

    def flush():
        nonlocal word, in_word, quoted
        token = ("word", "".join(word), quoted) if in_word else None
        word, in_word, quoted = [], False, False
        return token

    while i < n:
        c = command[i]
        nxt = command[i + 1] if i + 1 < n else ""
        if c in " \t\r":
            if (token := flush()) is not None:
                yield token
            i += 1
        elif c == "\n":
            if (token := flush()) is not None:
                yield token
            yield ("sep", "\n", False)
            i += 1
            if heredocs:
                i = _skip_heredoc_bodies(command, i, heredocs)
                heredocs = []
        elif c == "#" and not in_word:
            end = command.find("\n", i)
            i = n if end < 0 else end
        elif c == "'":
            j = i + 1
            chunk: list[str] = []
            while j < n:
                if command[j] == "'":
                    if powershell and j + 1 < n and command[j + 1] == "'":
                        chunk.append("'")
                        j += 2
                        continue
                    break
                chunk.append(command[j])
                j += 1
            word.extend(chunk)
            in_word = quoted = True
            i = j + 1
        elif c == '"':
            j = i + 1
            while j < n:
                ch = command[j]
                if powershell and ch == "`" and j + 1 < n:
                    word.append(command[j + 1])
                    j += 2
                elif powershell and ch == '"' and j + 1 < n and command[j + 1] == '"':
                    word.append('"')
                    j += 2
                elif not powershell and ch == "\\" and j + 1 < n and command[j + 1] in '"\\$`':
                    word.append(command[j + 1])
                    j += 2
                elif ch == '"':
                    break
                else:
                    word.append(ch)
                    j += 1
            in_word = quoted = True
            i = j + 1
        elif powershell and c == "@" and not in_word and nxt in "'\"" and _rest_of_line_blank(command, i + 2):
            # A here-string: @' ... '@ with the closer at the start of a line.
            end = command.find("\n" + nxt + "@", i + 2)
            i = n if end < 0 else end + 3
            yield ("fed", "", True)
        elif (c == "\\" and not powershell) or (c == "`" and powershell):
            if nxt == "\n":
                i += 2
            else:
                word.append(nxt)
                in_word = True
                i += 2
        elif c == ";" or (c == "|" and nxt == "|") or (c == "&" and nxt == "&"):
            if (token := flush()) is not None:
                yield token
            yield ("sep", c * (1 if c == ";" else 2), False)
            i += 1 if c == ";" else 2
        elif c == "|":
            if (token := flush()) is not None:
                yield token
            yield ("sep", "|", False)
            i += 1
        elif c == "&" and nxt == ">":
            if (token := flush()) is not None:
                yield token
            append = i + 2 < n and command[i + 2] == ">"
            yield ("redirect", "&>>" if append else "&>", False)
            i += 3 if append else 2
        elif c == "&":
            # Bash: run in the background (a separator). PowerShell: the
            # call operator, which only precedes the program.
            if (token := flush()) is not None:
                yield token
            if not powershell:
                yield ("sep", "&", False)
            i += 1
        elif c == ">":
            fd = ""
            if in_word and not quoted and _FD_RE.match("".join(word)):
                fd = "".join(word)
                word, in_word = [], False
            elif (token := flush()) is not None:
                yield token
            if nxt == "&":
                yield ("redirect", fd + ">&", False)
                i += 2
            elif nxt in (">", "|"):
                yield ("redirect", fd + ">" + nxt, False)
                i += 2
            else:
                yield ("redirect", fd + ">", False)
                i += 1
        elif c == "<":
            if (token := flush()) is not None:
                yield token
            if not powershell and command.startswith("<<<", i):
                yield ("redirect", "<<<", False)
                i += 3
            elif not powershell and nxt == "<":
                strip_tabs = i + 2 < n and command[i + 2] == "-"
                delimiter, i = _read_heredoc_delimiter(command, i + (3 if strip_tabs else 2))
                if delimiter:
                    heredocs.append((delimiter, strip_tabs))
                yield ("fed", "", True)
            else:
                yield ("redirect", "<", False)
                i += 1
        elif c in "()" and not powershell:
            if (token := flush()) is not None:
                yield token
            yield ("sep", c, False)
            i += 1
        elif c in "{}" and powershell:
            if (token := flush()) is not None:
                yield token
            yield ("sep", c, False)
            i += 1
        elif c == "$" and nxt == "{" and not powershell:
            end = command.find("}", i)
            end = n - 1 if end < 0 else end
            word.append(command[i:end + 1])
            in_word = True
            i = end + 1
        else:
            word.append(c)
            in_word = True
            i += 1
    if (token := flush()) is not None:
        yield token


def _rest_of_line_blank(command: str, start: int) -> bool:
    end = command.find("\n", start)
    return end >= 0 and not command[start:end].strip()


def _read_heredoc_delimiter(command: str, i: int) -> tuple[str, int]:
    n = len(command)
    while i < n and command[i] in " \t":
        i += 1
    delimiter: list[str] = []
    while i < n and command[i] not in _BASH_OPERATOR_CHARS:
        c = command[i]
        if c in "'\"":
            end = command.find(c, i + 1)
            end = n if end < 0 else end
            delimiter.append(command[i + 1:end])
            i = end + 1
        elif c == "\\":
            i += 1
        else:
            delimiter.append(c)
            i += 1
    return "".join(delimiter), i


def _skip_heredoc_bodies(command: str, i: int, heredocs: list[tuple[str, bool]]) -> int:
    n = len(command)
    for delimiter, strip_tabs in heredocs:
        while i < n:
            end = command.find("\n", i)
            line = command[i:n if end < 0 else end].rstrip("\r")
            i = n if end < 0 else end + 1
            if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                break
    return i


def _program(words: list[tuple[str, bool]], powershell: bool) -> tuple[str, list[tuple[str, bool]]]:
    """The program a simple command runs (lower case, no directory or
    ``.exe``) and its arguments."""
    start = 0
    while start < len(words):
        text, quoted = words[start]
        if quoted or not (text.lower() in _PREFIX_WORDS or (not powershell and _ASSIGNMENT_RE.match(text))):
            break
        start += 1
    if start >= len(words):
        return "", []
    text, quoted = words[start]
    if quoted:
        # PowerShell: a literal string; Bash: an unusual way to name a program.
        return "", words[start:]
    name = text.lstrip("(&").replace("\\", "/").rsplit("/", 1)[-1].lower()
    if name.endswith(".exe"):
        name = name[:-4]
    return name, words[start + 1:]


def _authored(source: _Command, powershell: bool) -> bool:
    """Whether the first command of a pipeline supplies content the
    command authored (see the module docstring)."""
    program, _args = _program(source.words, powershell)
    if program in (_PS_AUTHORING if powershell else _BASH_AUTHORING):
        return True
    if not powershell:
        return False
    if not source.words:
        return source.fed
    # A literal string or a variable on its own: "text" > file, $c | Out-File x.
    return len(source.words) == 1 and (source.words[0][1] or source.words[0][0].startswith("$"))


def _program_writes(program: str, args: list[tuple[str, bool]], powershell: bool, *, piped_authored: bool) -> list[str]:
    if program == "sed":
        return _in_place_files(args, value_letters="efl", script_letters="ef")
    if program == "perl":
        return _in_place_files(args, value_letters="eEIMmx", script_letters="eE")
    if powershell and program in ("set-content", "sc", "add-content", "ac"):
        return _ps_path(args)
    if powershell and program in ("out-file", "tee-object", "tee"):
        return _ps_path(args) if piped_authored else []
    if program == "tee":
        return [text for text, quoted in args if quoted or not text.startswith("-")] if piped_authored else []
    return []


def _in_place_files(args: list[tuple[str, bool]], *, value_letters: str, script_letters: str) -> list[str]:
    """The files ``sed``/``perl`` edit in place: none without ``-i``; the
    script is the first positional unless ``-e``/``-f`` gave one. Letters
    after ``i`` in a flag cluster are the backup suffix (``-i.bak``, and
    perl's ``-pie``, which is ``-p -i`` with suffix ``e``)."""
    in_place = False
    script_given = False
    positionals: list[str] = []
    i = 0
    while i < len(args):
        text, quoted = args[i]
        if quoted or not text.startswith("-") or text == "-":
            positionals.append(text)
        elif text.startswith("--"):
            if text.startswith("--in-place"):
                in_place = True
            elif text in ("--expression", "--file"):
                script_given = True
                i += 1
            elif text.startswith(("--expression=", "--file=")):
                script_given = True
        else:
            letters = text[1:]
            for j, letter in enumerate(letters):
                if letter == "i":
                    in_place = True
                    # BSD sed: -i '' (an empty backup suffix).
                    if letters == "i" and i + 1 < len(args) and args[i + 1] == ("", True):
                        i += 1
                    break
                if letter in value_letters:
                    script_given = script_given or letter in script_letters
                    if j == len(letters) - 1:
                        i += 1
                    break
        i += 1
    if not in_place:
        return []
    return positionals if script_given else positionals[1:]


def _ps_path(args: list[tuple[str, bool]]) -> list[str]:
    """The file a PowerShell cmdlet writes: its ``-Path``/``-LiteralPath``/
    ``-FilePath`` value, else its first positional argument."""
    positionals: list[str] = []
    i = 0
    while i < len(args):
        text, quoted = args[i]
        if not quoted and text.startswith("-") and len(text) > 1:
            name, colon, value = text[1:].partition(":")
            name = name.lower()
            if name in _PS_PATH_PARAMS:
                if colon:
                    return [value]
                return [args[i + 1][0]] if i + 1 < len(args) else []
            if not colon and any(param.startswith(name) for param in _PS_VALUE_PARAMS):
                i += 1
        else:
            positionals.append(text)
        i += 1
    return positionals[:1]


def _cd(args: list[tuple[str, bool]], here: str | None, powershell: bool) -> str | None:
    """The directory after ``cd``/``Set-Location``, or ``None`` when it
    can't be told (no argument, ``-``, a variable)."""
    target = _ps_path(args) if powershell else [text for text, quoted in args if quoted or not text.startswith("-")][:1]
    if not target or not _usable(target[0]):
        return None
    return _absolute(target[0], here)


# -- paths --------------------------------------------------------------------------


def _usable(path: str) -> bool:
    if not path or path == "-" or path.startswith(("~", "(")):
        return False
    if any(c in _UNUSABLE_CHARS for c in path):
        return False
    lowered = path.lower().replace("\\", "/")
    return lowered not in _DEVICES and not lowered.startswith(("/dev/", "/proc/"))


def _absolute(path: str, here: str | None) -> str | None:
    """``path`` with forward slashes, joined to ``here`` when relative;
    ``None`` when relative with no ``here``, or drive-relative (``C:x``)."""
    path = path.replace("\\", "/")
    if _ABSOLUTE_RE.match(path):
        return path
    if _DRIVE_RELATIVE_RE.match(path) or here is None:
        return None
    return here.rstrip("/") + "/" + path

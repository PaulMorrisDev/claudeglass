"""The command that runs this copy of claudeglass in a terminal.

The dashboard shows commands to copy (``claudeglass capture
connect``, ``... --dry-run``). The short ``claudeglass`` only
works when pip's Scripts folder is on ``PATH``, which a default Windows
Python install doesn't arrange, and never for the ``.pyz`` build. The
service knows how it was installed, so it names the form that works
(:func:`command_prefix`), in this order:

1. ``CLAUDEGLASS_COMMAND``, when set, word for word.
2. The ``.pyz`` this process runs from: ``python <archive>``.
3. ``claudeglass``, when the launcher of that name on ``PATH``
   belongs to this Python.
4. ``python -m claudeglass``.

In 2 and 4, ``python`` stands for this interpreter only when the
``python`` on ``PATH`` *is* this interpreter (same path, symlinks not
followed, so a venv's link to its base Python doesn't count); otherwise
the full path is written, quoted when it needs it. On Windows a quoted
path takes PowerShell's call operator (``& "C:\\Program Files\\..."``),
because PowerShell is the terminal Windows opens by default.

:func:`rewrite` and :func:`rewrite_payload` swap the prefix into text that
names a subcommand, so help written with the short form reads right on
every install: the dashboard's API, the CLI's own output
(:class:`RewritingStream`, set up by ``cli.main``) and the reports it
writes (:func:`rewrite_rendered`).
"""

from __future__ import annotations

import functools
import html
import json
import os
import re
import shlex
import shutil
import sys
import sysconfig
from pathlib import Path

from . import installer

#: The console-script name pip installs, and the form every help text,
#: note and command in the package is written with.
SHORT = "claudeglass"

#: Overrides the detected command, word for word (for example ``cg`` for
#: a shell alias, or a path the detection can't see).
ENV_VAR = "CLAUDEGLASS_COMMAND"

_MODULE = "claudeglass"

#: A path needs no quotes when it holds only these characters.
_PLAIN_PATH = re.compile(r"[\w.:\\/+=@-]+")


def command_prefix() -> str:
    """The words that start a claudeglass command on this install."""
    override = os.environ.get(ENV_VAR, "").strip()
    if override:
        return override
    return _detected_prefix()


@functools.lru_cache(maxsize=1)
def _detected_prefix() -> str:
    python = _terminal_python(sys.executable)
    pyz = installer.detect_pyz_path()
    if pyz is not None:
        return f"{_python_word(python)} {_quote(str(pyz))}"
    launcher = shutil.which(SHORT)
    if launcher and _belongs_to_this_python(launcher):
        return SHORT
    return f"{_python_word(python)} -m {_MODULE}"


def _terminal_python(executable: str) -> str:
    """``python.exe`` beside a ``pythonw.exe`` (the service runs
    windowless at logon), because pythonw prints nothing to a terminal.
    """
    path = Path(executable)
    if path.name.lower() == "pythonw.exe":
        console = path.with_name("python.exe")
        if console.is_file():
            return str(console)
    return executable


def _python_word(python: str) -> str:
    """``python`` or ``python3`` when that name on ``PATH`` is ``python``
    itself, else ``python``'s own path, quoted when it needs it."""
    for name in ("python", "python3"):
        found = shutil.which(name)
        if found and _same_path(found, python):
            return name
    return _quote(python)


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _belongs_to_this_python(launcher: str) -> bool:
    """Whether ``launcher`` sits in one of this interpreter's Scripts
    folders (the environment's own, or the user one ``pip --user``
    fills). A link to it counts too, as pipx installs them."""
    folders = [sysconfig.get_path("scripts")]
    try:
        folders.append(sysconfig.get_path("scripts", sysconfig.get_preferred_scheme("user")))
    except (KeyError, ValueError):
        pass
    path = Path(launcher)
    try:
        candidates = {path.parent, path.resolve().parent}
    except OSError:
        candidates = {path.parent}
    return any(folder and _same_path(str(parent), folder) for folder in folders for parent in candidates)


def _quote(path: str) -> str:
    if _PLAIN_PATH.fullmatch(path):
        return path
    if sys.platform == "win32":
        return '& "' + path + '"'
    return shlex.quote(path)


@functools.lru_cache(maxsize=1)
def _command_pattern() -> re.Pattern[str]:
    """``claudeglass`` followed by a real subcommand or an option,
    and not part of a longer name (``claudeglass.pyz``) or a
    sentence ("claudeglass ships ..."). Nor a message's label, the
    command's name and a colon ("claudeglass update: installed",
    "claudeglass serve --purge: will delete"), which names the
    command that is talking rather than one to run. Nor the module in
    ``python -m claudeglass ...``, which has the same name: a command
    already in that form stays as it is."""
    from .cli import SUBCOMMANDS  # the CLI imports the service; import late

    words = "|".join(re.escape(word) for word in sorted(SUBCOMMANDS, key=len, reverse=True))
    return re.compile(
        r"(?<![\w./\\-])(?<!-m )"
        + re.escape(SHORT)
        + r"(?= (?:(?:" + words + r")(?![\w-])|--?[a-z]))"
        + r"(?! (?:\S+ )?\S*:(?:\s|$))"
    )


def rewrite(text: str, prefix: str) -> str:
    """``text`` with each command's ``claudeglass`` replaced by
    ``prefix``."""
    if prefix == SHORT or SHORT not in text:
        return text
    return _command_pattern().sub(lambda _match: prefix, text)


def rewrite_payload(value: object, prefix: str) -> object:
    """:func:`rewrite` applied to every string in a JSON-ready value
    (keys stay as they are). Strings, not the serialised text, because
    in JSON a command after a line break follows the ``n`` of ``\\n``."""
    if prefix == SHORT:
        return value
    if isinstance(value, str):
        return rewrite(value, prefix)
    if isinstance(value, dict):
        return {key: rewrite_payload(item, prefix) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [rewrite_payload(item, prefix) for item in value]
    return value


def rewrite_rendered(text: str, kind: str, prefix: str | None = None) -> str:
    """A rendered report with its commands in this install's form.
    ``kind`` is how it was rendered: ``"json"`` (``render_json``: parsed,
    rewritten string by string and rendered again the same way, sorted
    keys and a 2-space indent), ``"html"`` (the prefix HTML-escaped, since
    it may hold ``&`` and quotes) or ``"markdown"``."""
    prefix = command_prefix() if prefix is None else prefix
    if prefix == SHORT or SHORT not in text:
        return text
    if kind == "json":
        return json.dumps(rewrite_payload(json.loads(text), prefix), sort_keys=True, indent=2)
    if kind == "html":
        return rewrite(text, html.escape(prefix, quote=True))
    return rewrite(text, prefix)


class RewritingStream:
    """A text stream that writes through :func:`rewrite`, so every command
    the CLI prints, whichever module wrote the text, is in this install's
    form. ``cli.main`` puts it on ``sys.stdout``/``sys.stderr`` for
    output people read; everything but ``write`` goes to the stream it
    wraps."""

    def __init__(self, stream, prefix: str) -> None:
        self._stream = stream
        self._prefix = prefix

    def write(self, text: str) -> int:
        return self._stream.write(rewrite(text, self._prefix))

    def writelines(self, lines) -> None:
        for line in lines:
            self.write(line)

    def __getattr__(self, name: str):
        return getattr(self._stream, name)


def shell_line(argv: list[str]) -> str:
    """``argv`` as one line to paste into this platform's terminal: the
    program through :func:`_quote`, each argument quoted when it needs it."""
    if not argv:
        return ""
    words = [_quote(argv[0])]
    for arg in argv[1:]:
        if _PLAIN_PATH.fullmatch(arg):
            words.append(arg)
        elif sys.platform == "win32":
            words.append('"' + arg + '"')
        else:
            words.append(shlex.quote(arg))
    return " ".join(words)

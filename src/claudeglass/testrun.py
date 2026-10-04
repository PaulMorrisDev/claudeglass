"""Whether a shell command runs a project's tests, and how much of them:
``"full"`` for the whole suite, ``"targeted"`` for chosen tests (a file, a
``file::test`` id, a name filter, a package), ``""`` for none.

The one matcher the parser (``Turn.tests_run``), the purpose rules
(``classify._matches_test_tool``) and the capture hook share. Its pieces
live in ``capture_catalogue`` (``TEST_*_PATTERN``), which the hook reads
from ``capture-catalogue.json``: it runs without this package and carries
a copy of the steps below, which ``tests/test_testrun.py`` holds to this
one.

A heredoc's body is dropped first: a script or commit message that
mentions pytest runs nothing. The line is cut into commands at ``&&``,
``||``, ``;``, ``|``, a closing bracket and line breaks. Each command
loses what comes before its program (a ``(`` or ``&``, ``VAR=value``,
``time``, ``timeout 60``, ``uv run``, ``npx``) and the folder and ``.exe``
of the program word, so ``C:/Python311/python.exe -m pytest`` and
``& ".venv/Scripts/pytest.exe"`` read like ``python -m pytest`` and
``pytest``. What is left must open with a test runner, and its arguments
must not say nothing is run (``--collect-only``).

A line that runs a whole suite anywhere is ``full``, even if it also runs
one test: the work was checked as a whole. The command is read here and
dropped; only the word is kept.
"""

from __future__ import annotations

import re

from .capture_catalogue import (
    TEST_BARE_TARGET_PATTERN,
    TEST_BARE_TARGET_RUNNER_PATTERN,
    TEST_COMMAND_SPLIT_PATTERN,
    TEST_HEREDOC_PATTERN,
    TEST_NO_RUN_PATTERN,
    TEST_NO_TARGET_PATTERN,
    TEST_PREFIX_PATTERN,
    TEST_PROGRAM_PATTERN,
    TEST_RUNNER_PATTERN,
    TEST_TARGET_PATTERN,
    TEST_WHOLE_SUITE_PATTERN,
)

FULL = "full"
TARGETED = "targeted"

_HEREDOC_RE = re.compile(TEST_HEREDOC_PATTERN)
_SPLIT_RE = re.compile(TEST_COMMAND_SPLIT_PATTERN)
_PREFIX_RE = re.compile(TEST_PREFIX_PATTERN)
_PROGRAM_RE = re.compile(TEST_PROGRAM_PATTERN)
_RUNNER_RE = re.compile(rf"(?P<runner>{TEST_RUNNER_PATTERN})(?=\s|$)(?P<args>.*)")
_NO_RUN_RE = re.compile(TEST_NO_RUN_PATTERN)
_NO_TARGET_RE = re.compile(TEST_NO_TARGET_PATTERN)
_WHOLE_SUITE_RE = re.compile(TEST_WHOLE_SUITE_PATTERN)
_TARGET_RE = re.compile(TEST_TARGET_PATTERN)
_BARE_TARGET_RE = re.compile(TEST_BARE_TARGET_PATTERN)
_BARE_TARGET_RUNNER_RE = re.compile(TEST_BARE_TARGET_RUNNER_PATTERN)


def run_scope(command: str) -> str:
    """``"full"`` when ``command`` runs a whole suite, ``"targeted"`` when
    it runs only chosen tests, ``""`` when it runs none."""
    scope = ""
    for part in _SPLIT_RE.split(_HEREDOC_RE.sub(r"\g<rest>", command)):
        found = _part_scope(part)
        if found == FULL:
            return FULL
        scope = scope or found
    return scope


def widest(scopes) -> str:
    """The widest of several ``run_scope`` results: full, else targeted,
    else none."""
    found = set(scopes)
    return FULL if FULL in found else TARGETED if TARGETED in found else ""


def command_parts(command: str) -> list[str]:
    """The commands of a shell line, in order, as ``run_scope`` reads them:
    heredoc bodies dropped, the line cut apart, and what comes before each
    program (an assignment, ``timeout 60``, ``uv run``) and the folder and
    ``.exe`` of the program word removed. ``shell_writes.changes_files``
    reads a command's program the same way (:func:`normalize`)."""
    return [normalize(part) for part in _SPLIT_RE.split(_HEREDOC_RE.sub(r"\g<rest>", command))]


def normalize(part: str) -> str:
    """One command without what comes before its program (a ``(`` or
    ``&``, ``VAR=value``, ``time``, ``timeout 60``, ``uv run``, ``npx``)
    and the folder and ``.exe`` of the program word."""
    part = part.strip()
    while (prefix := _PREFIX_RE.match(part)) is not None:
        part = part[prefix.end():]
    return _PROGRAM_RE.sub(r"\1", part, count=1)


def _part_scope(part: str) -> str:
    part = normalize(part)
    match = _RUNNER_RE.match(part)
    if match is None or _NO_RUN_RE.search(match["args"]):
        return ""
    args = _WHOLE_SUITE_RE.sub(" ", _NO_TARGET_RE.sub(" ", match["args"]))
    if _TARGET_RE.search(args) or (_BARE_TARGET_RUNNER_RE.fullmatch(match["runner"]) and _BARE_TARGET_RE.search(args)):
        return TARGETED
    return FULL

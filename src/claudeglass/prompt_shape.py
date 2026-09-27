"""The shape of a message you typed, for the prompting hints and the "How
you prompt" section: how many separate changes it asks for, whether it's
the same request as an earlier one, whether it only acknowledges, and
whether it's a vague correction.

Pure functions on text, used while a transcript is parsed (``events``)
and by the status line; ``hooks/capture-hook.py`` carries its own copy
(it runs without this package), which a test holds to these. Only the
results are kept: a count or a yes/no, never the words.
"""

from __future__ import annotations

import re

from .capture_catalogue import (
    ACK_PATTERN,
    ACTION_PATTERN,
    CORRECTION_PATTERN,
    CORRECTION_SCAN_CHARS,
    FIX_PATTERN,
    ITEM_SEPARATOR_PATTERN,
    LIST_ITEM_PATTERN,
    SENTENCE_END_PATTERN,
    SPECIFIC_PATTERN,
)

#: How much of a message is read for its steps: a huge paste costs no
#: more to scan than a long request.
STEP_SCAN_CHARS = 8_000

_LIST_ITEM_RE = re.compile(LIST_ITEM_PATTERN)
_ACTION_RE = re.compile(ACTION_PATTERN, re.IGNORECASE)
_SEPARATOR_RE = re.compile(ITEM_SEPARATOR_PATTERN, re.IGNORECASE)
_SENTENCE_END_RE = re.compile(SENTENCE_END_PATTERN)
_WORD_RE = re.compile(r"[a-z0-9']+")
_ACK_RE = re.compile(ACK_PATTERN, re.IGNORECASE)
_FIX_RE = re.compile(FIX_PATTERN, re.IGNORECASE)
_CORRECTION_RE = re.compile(CORRECTION_PATTERN, re.IGNORECASE)
_SPECIFIC_RE = re.compile(SPECIFIC_PATTERN)


def request_steps(text: str) -> int:
    """How many separate changes ``text`` asks for: the most of its list
    lines ("1. ...", "- ..."), the change verbs it uses ("add ...",
    "then move ..."), and the items of one sentence that starts with a
    change verb ("Add login, a settings page and an admin screen" is 3)."""
    text = text[:STEP_SCAN_CHARS]
    items = len(_LIST_ITEM_RE.findall(text))
    actions = len(_ACTION_RE.findall(text))
    listed = 0
    for sentence in _SENTENCE_END_RE.split(text):
        sentence = sentence.strip()
        if sentence and _ACTION_RE.match(sentence):
            listed = max(listed, 1 + len(_SEPARATOR_RE.findall(sentence)))
    return max(items, actions, listed)


_PLAN_WORD_RE = re.compile(r"\bplan\b", re.IGNORECASE)


def mentions_plan(text: str) -> bool:
    """Whether ``text`` talks about a plan ("carry out the plan"): a big
    request that does is following one, not skipping it."""
    return _PLAN_WORD_RE.search(text[:STEP_SCAN_CHARS]) is not None


def words(text: str) -> frozenset[str]:
    """The different words of ``text``, lower-cased."""
    return frozenset(_WORD_RE.findall(text.lower()))


def similarity(a: frozenset[str], b: frozenset[str]) -> float:
    """The share of words two messages have in common (Jaccard)."""
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def is_ack(text: str) -> bool:
    """Whether ``text`` only acknowledges: "thanks", "ok", "looks good"."""
    return _ACK_RE.fullmatch(text.strip()) is not None


def is_vague_fix(text: str, max_chars: int) -> bool:
    """A fix request of ``max_chars`` or less that names nothing specific
    and doesn't say what it should be instead ("it's broken")."""
    head = text[:CORRECTION_SCAN_CHARS]
    asks_fix = _FIX_RE.search(head) or _CORRECTION_RE.search(head)
    return bool(asks_fix) and len(text.strip()) <= max_chars and not _SPECIFIC_RE.search(text)

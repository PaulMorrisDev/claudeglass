"""Telling an agent that writes code from one that decides, by its name only.

An agent's phase, agent type or description often says what it was for:
"implement", "fix", "review", "audit". :func:`role_word` looks for one of
those words, whole, in that text, and :func:`role_class` sorts the word into
writer, integrates or decider. A writer that wrote code, or a decider that
also applied changes, is what the model-choice checks look at; a writer
that never wrote, and an integrator, are left alone. The words are how they
find out which kind of agent they have.

Only the canonical word is ever stored (``TranscriptMeta.role_word``). The
phase, label and description it was found in are dropped at once, so a
description like ``impl:C:/Users/x/secret.py`` leaves ``implement`` and
nothing else.

The vocabulary is closed. Each canonical word has a fixed list of forms in
:data:`FORMS`, and a token that isn't on the list is not a match: ``fixture``
is not ``fix`` and ``mapping`` is not ``map``. Adding a word or changing a
form changes what is stored for a transcript that was already read, so it
needs a ``PARSER_VERSION`` bump, the same as any other change to what the
parser keeps.
"""

from __future__ import annotations

import re
from itertools import islice

#: Canonical word -> every form that stands for it, per class.
_WRITER_FORMS = {
    "implement": (
        "implement implements implemented implementing implementation implementations"
        " implementer implementers implementor impl"
    ),
    "fix": "fix fixes fixed fixing fixer fixers fixup",
    "apply": "apply applies applied applying applier",
    "test": "test tests tested testing tester testers",
    "build": "build builds building builder built",
    "write": "write writes writing writer writers wrote",
    "migrate": "migrate migrates migrated migrating migration migrations",
    "refactor": "refactor refactors refactored refactoring",
}
_INTEGRATES_FORMS = {
    "integrate": "integrate integrates integrated integrating integration integrator",
}
_DECIDER_FORMS = {
    "review": "review reviews reviewed reviewing reviewer reviewers",
    "verify": "verify verifies verified verifying verification verifier verifiers",
    "refute": "refute refutes refuting refutation",
    "judge": "judge judges judging judgement judgment",
    "decide": "decide decides deciding decision",
    "audit": "audit audits audited auditing auditor",
    "research": "research researcher researching",
    "design": "design designs designing designer",
    "challenge": "challenge challenger challenging",
    "critique": "critique critic critics",
    "synthesise": (
        "synthesise synthesize synthesis synthesising synthesizing synthesiser synthesizer"
    ),
    "plan": "plan plans planning planner",
    "find": "find finds finder finders",
    "map": "map maps mapper",
    "assess": "assess assesses assessing assessment",
    "check": "check checks checking checker",
    "completeness": "completeness",
    "explore": "explore explores exploring exploration explorer",
    "investigate": "investigate investigates investigating investigation investigator",
    "inventory": "inventory",
    "baseline": "baseline",
    "adversarial": "adversarial",
    "analyse": "analyse analyze analysis analyst",
}

#: Canonical words that write code.
WRITER = frozenset(_WRITER_FORMS)
#: Canonical word that decides and may edit; never flagged as a writer.
INTEGRATES = frozenset(_INTEGRATES_FORMS)
#: Canonical words that decide.
DECIDER = frozenset(_DECIDER_FORMS)

#: Every accepted lower-case form -> its canonical word.
FORMS: dict[str, str] = {
    form: word
    for forms in (_WRITER_FORMS, _INTEGRATES_FORMS, _DECIDER_FORMS)
    for word, listed in forms.items()
    for form in listed.split()
}

#: Agent types that name no role, so their text is skipped.
_UNNAMED_TYPES = frozenset({"workflow-subagent", "general-purpose", "claude", "fork", "unknown"})

#: How many leading tokens of a description are read.
_DESCRIPTION_TOKENS = 4

#: A token is a run of ASCII letters in the lower-cased text.
_TOKEN_RE = re.compile(r"[a-z]+")


def _first_word(text: object, limit: int | None = None) -> str | None:
    """The canonical word for the first known token in the first ``limit``
    tokens of ``text`` (all of them without a limit); None when there is none
    or ``text`` isn't a string."""
    if not isinstance(text, str):
        return None
    for match in islice(_TOKEN_RE.finditer(text.lower()), limit):
        word = FORMS.get(match.group())
        if word is not None:
            return word
    return None


def role_word(phase: object, agent_type: object, description: object) -> str | None:
    """The canonical role word an agent's text names, or None.

    The sources are tried in order, and the first with a known token wins; in
    it the first known token wins: the phase (all its tokens), then the agent
    type (all its tokens, unless it is one of the types that name nothing,
    compared as given), then the first four tokens of the description. A
    value that isn't a string counts as absent. Only the word comes back,
    never the text.
    """
    word = _first_word(phase)
    if word is not None:
        return word
    if isinstance(agent_type, str) and agent_type not in _UNNAMED_TYPES:
        word = _first_word(agent_type)
        if word is not None:
            return word
    return _first_word(description, _DESCRIPTION_TOKENS)


def role_class(word: object) -> str | None:
    """``'writer'``, ``'integrates'`` or ``'decider'`` for a canonical role
    word; None for None or a word that isn't one."""
    if not isinstance(word, str):
        return None
    if word in WRITER:
        return "writer"
    if word in INTEGRATES:
        return "integrates"
    if word in DECIDER:
        return "decider"
    return None

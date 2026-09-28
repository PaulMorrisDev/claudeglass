"""Read the metrics-capture tags Claude writes (see ``capture_catalogue``).

Two places carry them:

- **The end of a reply.** ``[tl: task=bugfix brief=partial ...]`` ends the
  final reply to each of your messages in the main session, and a
  subagent's final report ends ``[result: done fit=right rules=used]``.
  Only the reply's last :data:`TAIL_SCAN_CHARS` characters are read, and
  the tags must be the very last thing in it (trailing markdown aside), so
  a tag quoted further up a reply -- when Claude explains the format, say
  -- is never counted.
- **The start of a brief.** ``[retry: brief]`` and ``[spawn: isolate]``
  open the brief handed to an agent, in either order.
- **Your feedback.** ``/cl-feedback`` ends with ``[tl-fb: outcome=met
  slow=none ...]`` on a line of its own, followed by a thank-you line.
  The answers to its AskUserQuestion call are read too, by matching the
  labels you ticked, for when the line is missing.

Every value is checked against the closed vocabularies in
``capture_catalogue``; unknown keys and words are dropped, so nothing
Claude wrote in its own words is kept. The one name that can survive is a
skill name in ``skill=would-help:<name>``, and only when it matches a
skill this transcript listed or used.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import fields, replace

from .capture_catalogue import (
    ALL_FEEDBACK_QUESTIONS,
    FEEDBACK_LIST_KEYS,
    FEEDBACK_REMINDER_LINE,
    FEEDBACK_TAG,
    FEEDBACK_VOCAB,
    LIST_KEYS,
    RESULT_WORDS,
    RETRY_REASONS,
    SKILL_NAME_PATTERN,
    SPAWN_REASONS,
    TAG_VOCAB,
)
from .model import CaptureTag, Feedback

#: How much of a reply's end is searched for tags. A full Deep ``[tl:]`` tag
#: is about 220 characters; a ``[result:]`` tag can sit next to it.
TAIL_SCAN_CHARS = 480

#: One or more tags ending the text, each on one line, with only
#: whitespace or markdown (backticks, emphasis, a full stop) between and
#: after them.
_TRAILING_TAGS_RE = re.compile(
    r"(?:\[(?:tl|result):[^\[\]\n]{0,300}\][`*_.\s]*){1,3}$",
    re.IGNORECASE,
)
_ONE_TAG_RE = re.compile(r"\[(tl|result):([^\[\]\n]{0,300})\]", re.IGNORECASE)

#: A skill name shaped the way Claude Code names skills (SEC-P3): used
#: both to validate a tag's own ``skill=would-help:<name>`` claim here
#: and, in ``parse.py``, to validate a ``Skill`` tool_use's own input
#: before it is ever trusted as a real invocation.
SKILL_NAME_RE = re.compile(rf"^{SKILL_NAME_PATTERN}$")

#: CAP-1: the feedback reminder line (``capture_catalogue.
#: FEEDBACK_REMINDER_LINE``), stripped from a reply's tail before the
#: trailing-tag match, so a tag still counts whether Claude wrote the
#: reminder before or after it. It may carry its quote-block label
#: (``capture_catalogue.REMINDER_LABEL``) or be a bare line, as older
#: notes asked.
_FEEDBACK_REMINDER_TAIL_RE = re.compile(
    r"\n?[ \t]*(?:>[ \t]*)?(?:\U0001F4A1\uFE0F?[ \t]*)?(?:[*_]*ClaudeGlass:[*_]*[ \t]*)?[`*_]*"
    + re.escape(FEEDBACK_REMINDER_LINE) + r"[`*_]*[ \t]*$"
)

#: Brief-start markers: up to two ``[retry: x]``/``[spawn: x]`` tags before
#: anything else, optionally in backticks.
_BRIEF_PREFIX_RE = re.compile(r"^\s*(?:`?\[(?:retry|spawn):\s*[A-Za-z-]+\s*\]`?\s*){1,2}", re.IGNORECASE)
_BRIEF_MARKER_RE = re.compile(r"\[(retry|spawn):\s*([A-Za-z-]+)\s*\]", re.IGNORECASE)

#: A capture note's marker: ``tl-cap v1`` then the metric codes, comma
#: separated (see ``capture_catalogue.NOTE_MARKER``).
_NOTE_RE = re.compile(r"tl-cap v(\d{1,3})(?: ([a-z_,]{0,400}))?")
_CODE_RE = re.compile(r"^[a-z_]{1,24}$")

_VOCAB_SETS = {key: frozenset(words) for key, words in TAG_VOCAB.items()}

#: ``[tl-fb: ...]`` on a line of its own, optionally in backticks or
#: emphasis. Unlike the reply tags it needn't end the reply: the skill
#: writes a thank-you line after it.
_FEEDBACK_TAG_RE = re.compile(
    r"^[ \t`*_]*\[" + re.escape(FEEDBACK_TAG) + r":([^\[\]\n]{0,200})\][`*_.]*[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
_FEEDBACK_SETS = {key: frozenset(words) for key, words in FEEDBACK_VOCAB.items()}
#: AskUserQuestion header -> the question, and its labels -> words.
_FEEDBACK_BY_HEADER = {q.header: q for q in ALL_FEEDBACK_QUESTIONS}
_FEEDBACK_LABELS = {q.key: {label: word for word, label, _ in q.options} for q in ALL_FEEDBACK_QUESTIONS}

#: The AskUserQuestion headers /cl-feedback asks with.
FEEDBACK_HEADERS = frozenset(_FEEDBACK_BY_HEADER)


def _apply_word(values: dict, key: str, value: str, skill_names: Collection[str]) -> None:
    """Fold one ``key=value`` word into ``values`` if both are known."""
    vocab = _VOCAB_SETS.get(key)
    if vocab is None:
        return
    if key in LIST_KEYS:
        words = tuple(dict.fromkeys(w for w in value.lower().split(",") if w in vocab))
        if words:
            values[key] = words
        return
    if key == "skill" and ":" in value:
        word, _, name = value.partition(":")
        word = word.lower()
        if word != "would-help":
            return
        values["skill"] = word
        values["skill_name"] = name if SKILL_NAME_RE.match(name) and name in skill_names else None
        return
    word = value.lower()
    if word in vocab:
        values[key] = word
        if key == "skill":
            values["skill_name"] = None


def parse_reply_tags(text: str, skill_names: Collection[str] = ()) -> tuple[CaptureTag | None, str | None]:
    """The capture tag and the ``[result: ...]`` word ending ``text``.

    Returns ``(tag, result_word)``: ``tag`` is ``None`` when the reply ends
    in no tag at all, and ``result_word`` is ``None`` without a valid
    ``[result: ...]``. ``skill_names`` are the skills this transcript has
    listed or used so far; a ``would-help:<name>`` naming any other skill
    keeps the ``would-help`` and drops the name.
    """
    if not text or "[" not in text:
        return None, None
    tail = text[-TAIL_SCAN_CHARS:]
    # CAP-1: the feedback reminder line asked for around the tag (see
    # capture_catalogue.FEEDBACK_REMINDER_LINE) doesn't count as trailing
    # text of its own -- strip one occurrence before the tail match so the
    # tag is still found whichever side of it Claude wrote the line on.
    tail = _FEEDBACK_REMINDER_TAIL_RE.sub("", tail, count=1)
    match = _TRAILING_TAGS_RE.search(tail)
    if match is None:
        return None, None
    values: dict = {}
    result_word: str | None = None
    has_tl = False
    for kind, body in _ONE_TAG_RE.findall(match.group(0)):
        words = body.split()
        if kind.lower() == "result":
            if not words or words[0].lower() not in RESULT_WORDS:
                continue
            result_word = words[0].lower()
            words = words[1:]
        else:
            has_tl = True
        for word in words:
            key, sep, value = word.partition("=")
            if sep and value:
                _apply_word(values, key.lower(), value.strip("`*_.,;"), skill_names)
    if result_word is None and not has_tl:
        return None, None
    tag = CaptureTag(has_tl=has_tl, chars=len(match.group(0).rstrip()), **values)
    return tag, result_word


def _feedback(values: dict, source: str) -> Feedback:
    """A :class:`Feedback` from ``values``; "skipped" when nothing usable
    was answered."""
    return Feedback(source=source, **values) if values else Feedback(source="skipped")


def merge_feedback(earlier: Feedback, later: Feedback) -> Feedback:
    """Two answers of the same kind in one /cl-feedback run, as one: the
    handoff question comes back from a second AskUserQuestion call. A
    later answer wins where both answered."""
    values = {
        f.name: getattr(later, f.name) or getattr(earlier, f.name) for f in fields(Feedback) if f.name != "source"
    }
    return Feedback(source=later.source, **values)


def parse_feedback_tag(text: str) -> Feedback | None:
    """The ``[tl-fb: ...]`` line in the end of ``text`` (the last one when
    there are several), or ``None`` without one. Unknown keys and words
    are dropped."""
    if not text or FEEDBACK_TAG not in text.lower():
        return None
    matches = _FEEDBACK_TAG_RE.findall(text[-TAIL_SCAN_CHARS:])
    if not matches:
        return None
    values: dict = {}
    for word in matches[-1].split():
        key, sep, value = word.partition("=")
        key = key.lower()
        vocab = _FEEDBACK_SETS.get(key)
        if not sep or vocab is None:
            continue
        words = tuple(dict.fromkeys(w for w in value.strip("`*_.;").lower().split(",") if w in vocab))
        if words:
            values[key] = words if key in FEEDBACK_LIST_KEYS else words[0]
    return _feedback(values, "tag")


def feedback_from_answers(result) -> Feedback | None:
    """/cl-feedback's answers from an AskUserQuestion ``toolUseResult``
    (``{"questions": [...], "answers": {question text: answer}}``), or
    ``None`` when it asked none of the feedback questions. An answer is
    a label, a list of labels, or labels joined with commas; anything that
    isn't one of the question's labels (a free-text "Other") is dropped."""
    if not isinstance(result, dict):
        return None
    questions, answers = result.get("questions"), result.get("answers")
    if not isinstance(questions, list) or not isinstance(answers, dict):
        return None
    asked = False
    values: dict = {}
    for question in questions:
        if not isinstance(question, dict):
            continue
        spec = _FEEDBACK_BY_HEADER.get(question.get("header"))
        text = question.get("question")
        if spec is None or not isinstance(text, str):
            continue
        asked = True
        answer = answers.get(text)
        labels = _FEEDBACK_LABELS[spec.key]
        if isinstance(answer, str):
            picked = [answer] if answer in labels else [part.strip() for part in answer.split(",")]
        elif isinstance(answer, list):
            picked = [part for part in answer if isinstance(part, str)]
        else:
            continue
        words = tuple(dict.fromkeys(labels[label] for label in picked if label in labels))
        if words:
            values[spec.key] = words if spec.multi else words[0]
    return _feedback(values, "answers") if asked else None


def asks_for_feedback(tool_input) -> bool:
    """Whether an AskUserQuestion call's input asks /cl-feedback's
    questions."""
    questions = tool_input.get("questions") if isinstance(tool_input, dict) else None
    return isinstance(questions, list) and any(
        isinstance(q, dict) and q.get("header") in FEEDBACK_HEADERS for q in questions
    )


def parse_brief_markers(text: str) -> tuple[str | None, str | None]:
    """``(retry_reason, spawn_reason)`` from the start of a brief; either
    is ``None`` when absent or not a known word."""
    if not text or "[" not in text[:40]:
        return None, None
    prefix = _BRIEF_PREFIX_RE.match(text)
    if prefix is None:
        return None, None
    retry: str | None = None
    spawn: str | None = None
    for kind, word in _BRIEF_MARKER_RE.findall(prefix.group(0)):
        kind, word = kind.lower(), word.lower()
        if kind == "retry" and retry is None and word in RETRY_REASONS:
            retry = word
        elif kind == "spawn" and spawn is None and word in SPAWN_REASONS:
            spawn = word
    return retry, spawn


def parse_note_codes(text: str) -> tuple[int | None, tuple[str, ...]]:
    """The format version and metric codes of a capture note's
    ``tl-cap v1 task,brief`` marker; ``(None, ())`` without one."""
    match = _NOTE_RE.search(text)
    if match is None:
        return None, ()
    codes = tuple(c for c in (match.group(2) or "").split(",") if _CODE_RE.match(c))
    return int(match.group(1)), codes


#: ``CaptureTag`` field -> the metric it answers, in a main-session tag
#: and in a subagent's ``[result: ...]`` tag. :func:`filter_tag` (SEC-P2)
#: uses these to keep only what a session's own notes asked for; the
#: cost accounting in ``capture.py`` uses them to weigh what a tag
#: answered.
MAIN_TAG_FIELDS = {
    "task": "task", "brief": "brief", "level": "level", "shift": "shift", "size": "size", "missing": "missing",
    "plan": "plan", "skill": "skill", "found": "found", "prior": "prior", "detour": "detour", "check": "check",
    "out": "big_output", "useful": "web",
}
#: ``out`` is in both: the PostToolUse note after a large result reaches
#: subagents too, and asks them for it in their ``[result: ...]``.
SUB_TAG_FIELDS = {
    "fit": "fit", "rules": "rules", "brief": "agent_brief", "missing": "agent_brief", "out": "big_output",
}
#: Every ``CaptureTag`` field a tag key fills, in either scope.
_TAG_FIELDS = tuple(dict.fromkeys([*MAIN_TAG_FIELDS, *SUB_TAG_FIELDS]))


def filter_tag(
    cap: CaptureTag | None, result_marker: str | None, *, requested: Collection[str], subagent: bool
) -> tuple[CaptureTag | None, str | None]:
    """Keep only what this transcript's own capture notes actually asked
    for (SEC-P2): ``requested`` is the metric ids named in a note this
    transcript saw (from ``TranscriptMeta.cap_metrics``), empty when it
    never got one. A tag key, the ``[result: ...]`` marker, or the tag
    as a whole is dropped unless its metric is in ``requested`` --
    closing the gap where a tag Claude wrote unprompted (habit, an
    example it saw, a copied transcript) would otherwise be trusted
    just because it parses. Keys only the other scope is ever asked for
    go too, and a subagent's ``[tl: ...]`` doesn't count as a tag (it is
    only ever asked for ``[result: ...]``): a subagent writing a
    main-session tag out of habit would otherwise set the task or level
    of the whole prompt cycle it ran in.
    """
    if not requested:
        return None, None
    if result_marker is not None and "result" not in requested:
        result_marker = None
    if cap is not None:
        fields = SUB_TAG_FIELDS if subagent else MAIN_TAG_FIELDS
        foreign = {
            name: (() if name == "missing" else None)
            for name in _TAG_FIELDS
            if name not in fields and getattr(cap, name) not in (None, ())
        }
        if "skill" in foreign:
            foreign["skill_name"] = None
        if subagent and cap.has_tl:
            foreign["has_tl"] = False
        if foreign:
            cap = replace(cap, **foreign)
        if not any(metric_id in requested for metric_id in fields.values()):
            # No metric this tag could answer was ever requested: even a
            # well-formed [tl:]/[result:] here is unearned.
            cap = None if result_marker is None else replace(
                cap, has_tl=False, chars=0, **{name: (() if name == "missing" else None) for name in fields}
            )
        else:
            drop = [
                name for name in fields
                if fields[name] not in requested and getattr(cap, name) not in (None, ())
            ]
            if drop:
                changes = {name: (() if name == "missing" else None) for name in drop}
                if "skill" in drop:
                    changes.setdefault("skill_name", None)
                cap = replace(cap, **changes)
    return cap, result_marker

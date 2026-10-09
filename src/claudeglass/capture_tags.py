"""Read the metrics-capture tags Claude writes (see ``capture_catalogue``).

Two places carry them:

- **The end of a reply.** ``[cg: task=bugfix brief=partial ...]`` ends the
  final reply to each of your messages in the main session, and a
  subagent's final report ends ``[result: done fit=right rules=used]``
  (an older transcript's: no agent is asked for a tag now).
  Only the reply's last :data:`TAIL_SCAN_CHARS` characters are read, and
  the tags must be the very last thing in it (trailing markdown aside), so
  a tag quoted further up a reply -- when Claude explains the format, say
  -- is never counted.
- **The start of a brief.** ``[retry: brief]`` and ``[spawn: isolate]``
  open the brief handed to an agent, in either order.
- **Your feedback.** ``/cg-feedback`` ends with ``[cg-fb: outcome=met
  why=left_out ...]`` as the last line of its reply, after a short summary
  of what it recorded. The answers to its AskUserQuestion calls are read
  too, by matching the labels you ticked, for when the line is missing. An
  answer you typed under "Other" is never kept: Claude picks the closest
  word of that question's list and adds ``from_text=<keys>``, and that word
  counts only when the AskUserQuestion result shows a non-label answer for
  that key. A ticked answer always wins over the tag.

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
    FEEDBACK_ANSWER_KEYS,
    FEEDBACK_LIST_KEYS,
    FEEDBACK_REMINDER_LINE,
    FEEDBACK_TAG,
    FEEDBACK_VOCAB,
    LIST_KEYS,
    OLD_FEEDBACK_TAG,
    PLAN_CHECK_HEADER,
    PLAN_CHECK_OPTIONS,
    RESULT_WORDS,
    RETRY_REASONS,
    SKILL_NAME_PATTERN,
    SLOW_TO_WHY,
    SPAWN_REASONS,
    TAG_VOCAB,
    TIP_HINT_TITLES,
)
from .model import CaptureTag, Feedback

#: How much of a reply's end is searched for the reminder line: the line,
#: its quote label, a blank line and a full tag after it.
REMINDER_SCAN_CHARS = 1024

#: How much of a reply's end is searched for tags. A full Deep ``[cg:]`` tag
#: is about 220 characters; a ``[result:]`` tag can sit next to it.
TAIL_SCAN_CHARS = 480

#: One or more tags ending the text, each on one line, with only
#: whitespace or markdown (backticks, emphasis, a full stop) between and
#: after them. ``[tl: ...]`` is the reply tag's name until 0.12.1.
_TRAILING_TAGS_RE = re.compile(
    r"(?:\[(?:cg|tl|result):[^\[\]\n]{0,300}\][`*_.\s]*){1,3}$",
    re.IGNORECASE,
)
_ONE_TAG_RE = re.compile(r"\[(cg|tl|result):([^\[\]\n]{0,300})\]", re.IGNORECASE)

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

#: A capture note's marker: ``cg-cap v1`` then the metric codes, comma
#: separated (see ``capture_catalogue.NOTE_MARKER``); ``tl-cap`` until
#: 0.12.1.
_NOTE_RE = re.compile(r"(?:cg|tl)-cap v(\d{1,3})(?: ([a-z_,]{0,400}))?")
_CODE_RE = re.compile(r"^[a-z_]{1,24}$")

_VOCAB_SETS = {key: frozenset(words) for key, words in TAG_VOCAB.items()}

#: ``[cg-fb: ...]`` (``[tl-fb: ...]`` until 0.12.1) on a line of its own,
#: optionally in backticks or emphasis. Unlike the reply tags it needn't
#: end the reply: an older skill wrote a thank-you line after it. The
#: longest tag the skill can write is about 250 characters, so the body
#: may run to 300.
_FEEDBACK_TAG_RE = re.compile(
    r"^[ \t`*_]*\[(?:" + re.escape(FEEDBACK_TAG) + "|" + re.escape(OLD_FEEDBACK_TAG) + r"):([^\[\]\n]{0,300})\][`*_.]*[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
_FEEDBACK_SETS = {key: frozenset(words) for key, words in FEEDBACK_VOCAB.items()}
#: AskUserQuestion header -> the question, and (by header, since two
#: questions can share a key: the older and the current handoff) its
#: labels -> words.
_FEEDBACK_BY_HEADER = {q.header: q for q in ALL_FEEDBACK_QUESTIONS}
_FEEDBACK_LABELS = {q.header: {label: word for word, label, _ in q.options} for q in ALL_FEEDBACK_QUESTIONS}
#: The ``Feedback`` fields that hold answers: a tuple for the multi-select
#: questions, a word for the rest.
_FEEDBACK_WORD_KEYS = tuple(key for key in FEEDBACK_ANSWER_KEYS if key in FEEDBACK_VOCAB)
_FEEDBACK_KEY_SET = frozenset(FEEDBACK_ANSWER_KEYS)
#: A tip's id by the title the tip question names it with.
_TIP_HINT_BY_TITLE = {title: hint for hint, title in TIP_HINT_TITLES.items()}

#: The AskUserQuestion headers /cg-feedback asks with.
FEEDBACK_HEADERS = frozenset(_FEEDBACK_BY_HEADER)

#: The plan check's option labels -> words (``PLAN_CHECK_OPTIONS``).
_PLAN_CHECK_LABELS = {label: word for word, label, _description in PLAN_CHECK_OPTIONS}


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
    """Two answers of the same kind in one /cg-feedback run, as one: the
    second AskUserQuestion call's questions come back separately. A later
    answer wins where both answered; the keys answered in your own words
    (``other``, ``from_text``) are joined."""
    values = {
        f.name: getattr(later, f.name) or getattr(earlier, f.name)
        for f in fields(Feedback)
        if f.name not in ("source", "other", "from_text")
    }
    for name in ("other", "from_text"):
        values[name] = tuple(dict.fromkeys((*getattr(earlier, name), *getattr(later, name))))
    return Feedback(source=later.source, **values)


def with_older_why(feedback: Feedback) -> Feedback:
    """``feedback`` with ``why`` read from an older run's ``slow`` answer
    when it has none (``why_older`` says so): ``unclear`` was "my request
    left it out" and ``none`` was "nothing slowed it". ``rework`` didn't
    say who was at fault and ``tools`` was never a reason you gave, so
    neither adds a word."""
    if feedback.why or not feedback.slow:
        return feedback
    why = tuple(dict.fromkeys(SLOW_TO_WHY[word] for word in feedback.slow if word in SLOW_TO_WHY))
    return replace(feedback, why=why, why_older=True) if why else feedback


def _has_answers(feedback: Feedback) -> bool:
    return any(getattr(feedback, key) for key in _FEEDBACK_WORD_KEYS)


def _only_verified(tag: Feedback) -> Feedback:
    """A tag read with no answers behind it: the words it says came from
    your own note can't be checked, so they're dropped."""
    claimed = [key for key in tag.from_text if key in _FEEDBACK_WORD_KEYS]
    if not claimed:
        return tag
    cleared = replace(
        tag,
        from_text=(),
        **{key: () if key in FEEDBACK_LIST_KEYS else None for key in claimed},
        **({"tip_hint": None} if "tip" in claimed else {}),
    )
    return cleared if _has_answers(cleared) else Feedback(source="skipped")


def _take_from_tag(base: Feedback, tag: Feedback | None, other: frozenset[str]) -> Feedback:
    """``base`` (answers read from the AskUserQuestion result) plus the
    words ``tag`` gives for the keys you answered in your own words. A key
    you ticked keeps its ticked word: only an empty single answer is
    filled, and a multi-select answer gains the tag's words after the
    ticked ones. A word for a key with no non-label answer is dropped."""
    if tag is None or not other:
        return base
    values: dict = {}
    taken = []
    for key in _FEEDBACK_WORD_KEYS:
        words = getattr(tag, key)
        if key not in other or not words:
            continue
        have = getattr(base, key)
        if key in FEEDBACK_LIST_KEYS:
            joined = tuple(dict.fromkeys((*have, *words)))
            if joined != have:
                values[key] = joined
                taken.append(key)
        elif not have:
            values[key] = words
            taken.append(key)
    if not taken:
        return base
    return replace(base, source="answers", from_text=tuple(dict.fromkeys((*base.from_text, *taken))), **values)


def settle_feedback(answers: Feedback | None, tag: Feedback | None, skipped: Feedback | None) -> Feedback | None:
    """One /cg-feedback run's feedback from what it gave: ``answers`` read
    from its AskUserQuestion results, ``tag`` from the line ending its
    reply, ``skipped`` for a declined question (each already merged across
    the run's turns, ``None`` for a kind it didn't give). SEC-P1: the
    answers outrank the tag, as Claude could write any tag but not the
    result of the question you answered. So a tag adds a word only for a
    key whose answer shows words you typed under "Other" (``other``), and
    never replaces a ticked one. Without any answers the tag stands, minus
    the words it says came from your note, which nothing can confirm. An
    older run's ``slow`` also gives ``why``."""
    other_keys = tuple(dict.fromkeys((*(answers.other if answers else ()), *(skipped.other if skipped else ()))))
    other = frozenset(other_keys)
    if answers is not None:
        settled = replace(_take_from_tag(answers, tag, other), other=other_keys)
    elif other and skipped is not None:
        settled = replace(_take_from_tag(skipped, tag, other), other=other_keys)
    elif tag is not None:
        settled = _only_verified(tag)
    else:
        settled = skipped
    if settled is None:
        return None
    if tag is not None and tag.tip_hint and settled.tip and not settled.tip_hint:
        settled = replace(settled, tip_hint=tag.tip_hint)
    return with_older_why(settled)


def parse_feedback_tag(text: str) -> Feedback | None:
    """The ``[cg-fb: ...]`` line in the end of ``text`` (the last one when
    there are several), or ``None`` without one. Unknown keys and words
    are dropped."""
    if not text or (FEEDBACK_TAG not in text.lower() and OLD_FEEDBACK_TAG not in text.lower()):
        return None
    matches = _FEEDBACK_TAG_RE.findall(text[-TAIL_SCAN_CHARS:])
    if not matches:
        return None
    values: dict = {}
    claimed: tuple[str, ...] = ()
    for word in matches[-1].split():
        key, sep, value = word.partition("=")
        key = key.lower()
        if not sep:
            continue
        if key == "from_text":
            claimed = tuple(dict.fromkeys(w for w in value.strip("`*_.;").lower().split(",") if w in _FEEDBACK_KEY_SET))
            continue
        vocab = _FEEDBACK_SETS.get(key)
        if vocab is None:
            continue
        words = tuple(dict.fromkeys(w for w in value.strip("`*_.;").lower().split(",") if w in vocab))
        if words:
            values[key] = words if key in FEEDBACK_LIST_KEYS else words[0]
    # Only a key that has a word can have come from your note.
    from_text = tuple(key for key in claimed if key in values)
    if from_text and values:
        values["from_text"] = from_text
    return _feedback(values, "tag")


def feedback_from_answers(result) -> Feedback | None:
    """/cg-feedback's answers from an AskUserQuestion ``toolUseResult``
    (``{"questions": [...], "answers": {question text: answer}}``), or
    ``None`` when it asked none of the feedback questions. An answer is
    a label, a list of labels, or labels joined with commas. Anything that
    isn't one of the question's labels (a free-text "Other") is dropped,
    and only its key is kept, in ``other``: that is what lets a word
    Claude picked from the note into the tag count
    (:func:`settle_feedback`). A single-choice question's answer is a
    label or all "Other"; commas split only a multi-select's."""
    if not isinstance(result, dict):
        return None
    questions, answers = result.get("questions"), result.get("answers")
    if not isinstance(questions, list) or not isinstance(answers, dict):
        return None
    asked = False
    values: dict = {}
    other: list[str] = []
    for question in questions:
        if not isinstance(question, dict):
            continue
        spec = _FEEDBACK_BY_HEADER.get(question.get("header"))
        text = question.get("question")
        if spec is None or not isinstance(text, str):
            continue
        asked = True
        answer = answers.get(text)
        labels = _FEEDBACK_LABELS[spec.header]
        if isinstance(answer, str):
            if answer not in labels and spec.multi:
                picked = [part.strip() for part in answer.split(",")]
            else:
                picked = [answer]
        elif isinstance(answer, list):
            picked = [part for part in answer if isinstance(part, str)]
        else:
            continue
        words = tuple(dict.fromkeys(labels[label] for label in picked if label in labels))
        if words:
            values[spec.key] = words if spec.multi else words[0]
        if any(part.strip() and part not in labels for part in picked):
            other.append(spec.key)
        if spec.key == "tip" and "tip" in values:
            hints = {hint for title, hint in _TIP_HINT_BY_TITLE.items() if title in text}
            if len(hints) == 1:
                values["tip_hint"] = hints.pop()
    if not asked:
        return None
    feedback = _feedback(values, "answers")
    return replace(feedback, other=tuple(dict.fromkeys(other))) if other else feedback


def asks_for_feedback(tool_input) -> bool:
    """Whether an AskUserQuestion call's input asks /cg-feedback's
    questions."""
    questions = tool_input.get("questions") if isinstance(tool_input, dict) else None
    return isinstance(questions, list) and any(
        isinstance(q, dict) and q.get("header") in FEEDBACK_HEADERS for q in questions
    )


def asks_plan_check(tool_input) -> bool:
    """Whether an AskUserQuestion call's input asks the plan check
    (``capture_catalogue.PLAN_CHECK_HEADER``)."""
    questions = tool_input.get("questions") if isinstance(tool_input, dict) else None
    return isinstance(questions, list) and any(
        isinstance(q, dict) and q.get("header") == PLAN_CHECK_HEADER for q in questions
    )


def plan_check_from_answers(result) -> str | None:
    """The plan check's word from an AskUserQuestion ``toolUseResult``
    (``{"questions": [...], "answers": {question text: label}}``): a word
    of ``capture_catalogue.PLAN_CHECK_WORDS``; ``""`` when the question
    was asked and answered in the user's own words (an "Other", read here
    and dropped) or not at all; ``None`` when the call didn't ask the
    plan check."""
    if not isinstance(result, dict):
        return None
    questions, answers = result.get("questions"), result.get("answers")
    if not isinstance(questions, list):
        return None
    for question in questions:
        if not isinstance(question, dict) or question.get("header") != PLAN_CHECK_HEADER:
            continue
        text = question.get("question")
        answer = answers.get(text) if isinstance(answers, dict) and isinstance(text, str) else None
        return _PLAN_CHECK_LABELS.get(answer, "") if isinstance(answer, str) else ""
    return None


def carries_reminder(text: str) -> bool:
    """Whether the end of a reply's text carries the /cg-feedback reminder
    line (``capture_catalogue.FEEDBACK_REMINDER_LINE``), which a hook note
    asks Claude to write. A yes or no; the text is not kept."""
    return FEEDBACK_REMINDER_LINE in text[-REMINDER_SCAN_CHARS:]


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
    ``cg-cap v1 task,brief`` marker; ``(None, ())`` without one."""
    match = _NOTE_RE.search(text)
    if match is None:
        return None, ()
    codes = tuple(c for c in (match.group(2) or "").split(",") if _CODE_RE.match(c))
    return int(match.group(1)), codes


#: ``CaptureTag`` field -> the metric it answers, in a main-session tag
#: and in a subagent's ``[result: ...]`` tag. :func:`filter_tag` (SEC-P2)
#: uses these to keep only what a session's own notes asked for; the
#: cost accounting in ``capture.py`` uses them to weigh what a tag
#: answered. ``why`` and ``admit`` ride on the ``shift`` switch
#: (``Metric.extra_keys``), so a note that asked for ``shift`` asked for
#: them. ``found``, ``detour`` and ``useful`` are retired metrics
#: (``capture_catalogue.RETIRED_METRIC_IDS``): a note from before the
#: retirement listed them, and the transcript's tags still count.
MAIN_TAG_FIELDS = {
    "task": "task", "brief": "brief", "level": "level", "shift": "shift", "why": "shift", "admit": "shift",
    "size": "size", "missing": "missing", "plan": "plan", "skill": "skill", "found": "found", "prior": "prior",
    "detour": "detour", "check": "check", "out": "big_output", "useful": "web",
}
#: ``out`` is in both: the PostToolUse note after a large result reaches
#: subagents too, and asks them for it in their ``[result: ...]``. ``fit``
#: and ``rules`` are retired the same way.
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
    go too, and a subagent's ``[cg: ...]`` doesn't count as a tag (it is
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
            # well-formed [cg:]/[result:] here is unearned.
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


# -- grounding ----------------------------------------------------------------------

#: The tag keys :func:`settle` can change, in a tag's own order.
GROUNDED_KEYS = ("task", "shift", "why", "admit", "plan", "skill", "check", "prior")

#: ``shift`` words that mean the work builds on the last piece, which a
#: correction (or an adjustment to the same files) turns into ``fix``.
_BUILDING = ("build", "grew")

#: How the settled words differ from the ones written, ``key:from>to``,
#: ``to`` empty for a word dropped. The shape ``haiku_tags`` accepts in a
#: row's ``g`` field.
CHANGE_PATTERN = r"[a-z]{2,8}:[a-z_-]{2,12}>[a-z_-]{0,12}"
CHANGE_RE = re.compile(rf"^{CHANGE_PATTERN}$")


def settle(words: str, facts: dict | None) -> str:
    """``words`` (``task=bugfix plan=made ...``) with what the transcript
    settles put right. This is the twin of ``grounded`` in
    ``hooks/capture_hook.py``, which does the same for the words Haiku
    writes from the live transcript: the hook can't import this package,
    so the rules are written twice, in the same order, and
    ``tests/test_capture.py`` feeds both the same table. Change one and
    change the other.

    ``facts`` keys (a missing one reads as none): ``plan_now`` (a plan was
    written in this piece of work), ``plan_before`` (one was approved
    earlier), ``skills`` run, ``files`` of yours edited, ``agent_files``
    changed by subagents and workflow agents, ``shell_changes`` (commands
    that wrote or moved files), ``tests`` (``full``, ``targeted`` or
    none), ``docs_only`` (every edit was to documentation), ``earlier``
    (messages of yours before this one), ``correction`` (the message
    corrects Claude), ``adjust`` (it asks for a tweak), ``same_files`` (the
    tweak is to the files the last reply changed), ``tool_errors``,
    ``admit_candidate`` (the reply says it got something wrong) and
    ``judged`` (Haiku wrote the tag, so its ``admit`` needs a candidate).

    - ``plan`` is ``made`` when a plan was written, and can't be ``made``
      after one was approved earlier (it reads ``following``).
    - ``skill`` can't be ``helped`` or ``unneeded`` when no skill ran.
    - ``check`` is ``none`` when nothing changed, tests run or not (there
      was no change to check), else ``targeted`` or ``full`` when tests
      ran (a whole suite outranks chosen tests).
    - ``task`` is ``docs`` when only documentation changed.
    - A first message has no ``shift`` but ``new``, and no ``prior`` but
      ``none``. ``build`` and ``grew`` read ``fix`` after a correction, or
      an adjustment to the files just changed.
    - ``admit`` from Haiku needs a candidate in the reply.
    - ``why`` needs ``shift`` to be ``redo`` or ``fix``, and ``tools``
      needs a tool error.
    """
    if not facts or not words:
        return words
    first = facts.get("earlier") == 0
    changed = bool(facts.get("files") or facts.get("agent_files") or facts.get("shell_changes"))
    docs = bool(facts.get("docs_only")) and not facts.get("agent_files") and not facts.get("shell_changes")
    fixing = bool(facts.get("correction") or (facts.get("adjust") and facts.get("same_files")))
    pairs: list[list[str]] = []
    for word in words.split():
        key, _, value = word.partition("=")
        if key == "plan" and facts.get("plan_now"):
            value = "made"
        elif key == "plan" and facts.get("plan_before") and value == "made":
            value = "following"
        elif key == "skill" and not facts.get("skills") and value in ("helped", "unneeded"):
            value = "none"
        elif key == "check" and not changed:
            value = "none"
        elif key == "check" and facts.get("tests"):
            value = facts["tests"]
        elif key == "task" and docs and value in ("bugfix", "feature", "refactor"):
            value = "docs"
        elif key == "shift" and first and value != "new":
            continue
        elif key == "shift" and value in _BUILDING and fixing:
            value = "fix"
        elif key == "prior" and first:
            value = "none"
        elif key == "admit" and facts.get("judged") and not facts.get("admit_candidate"):
            continue
        pairs.append([key, value])
    shift = next((value for key, value in pairs if key == "shift"), "")
    out = []
    for key, value in pairs:
        if key == "why" and (shift not in ("redo", "fix") or (value == "tools" and not facts.get("tool_errors"))):
            continue
        out.append(f"{key}={value}")
    return " ".join(out)


def grounding_changes(before: str, after: str) -> tuple[str, ...]:
    """How ``after`` differs from ``before``, as ``key:from>to`` items in
    ``before``'s order (``to`` is empty for a word dropped), for the words
    :func:`settle` may change. Twin of the hook's ``grounding_changes``."""
    now = {key: value for key, _, value in (word.partition("=") for word in after.split())}
    found = []
    for word in before.split():
        key, _, value = word.partition("=")
        item = f"{key}:{value}>{now.get(key, '')}"
        if key in GROUNDED_KEYS and now.get(key, "") != value and CHANGE_RE.match(item):
            found.append(item)
    return tuple(found)


def settle_tag(cap: CaptureTag | None, facts: dict | None) -> CaptureTag | None:
    """``cap`` with :func:`settle` applied to the words it can change,
    and what it changed noted in ``grounded`` (after any changes already
    noted there). ``cap`` itself when nothing changed."""
    if cap is None or not facts:
        return cap
    before = " ".join(f"{key}={getattr(cap, key)}" for key in GROUNDED_KEYS if getattr(cap, key))
    after = settle(before, facts)
    if after == before:
        return cap
    now = {key: value for key, _, value in (word.partition("=") for word in after.split())}
    changes = {key: now.get(key) for key in GROUNDED_KEYS if getattr(cap, key) != now.get(key)}
    return replace(cap, grounded=cap.grounded + grounding_changes(before, after), **changes)

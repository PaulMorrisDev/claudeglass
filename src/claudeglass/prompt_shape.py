"""The shape of a message you typed, for the prompting hints and the "How
you prompt" section: how many separate changes it asks for, whether it's
the same request as an earlier one, whether it only acknowledges, and
whether it's a vague correction. Also the shape of a reply of Claude's:
whether it ends on a question, owns a mistake or disowns a tip, and of
the work that answered a message: whether it changed a file.

Pure functions on text, used while a transcript is parsed (``events``,
``parse``) and by the status line; ``hooks/capture_hook.py`` carries its
own copy of the ones it needs (it runs without this package), which a
test holds to these. Only the results are kept: a count or a yes/no,
never the words.
"""

from __future__ import annotations

import re

from .capture_catalogue import (
    ACK_PATTERN,
    ACTION_PATTERN,
    ADJUST_PATTERN,
    ADMIT_PATTERN,
    ADMIT_SCAN_CHARS,
    ASKS_PATTERN,
    BAD_OUTCOME_PATTERN,
    CHANGE_PATTERN,
    CHANGE_SCAN_CHARS,
    CORRECTION_PATTERN,
    CONFIG_PATH_PATTERN,
    CORRECTION_SCAN_CHARS,
    EDIT_TOOLS,
    ERROR_TEXT_PATTERN,
    GO_MAX_CHARS,
    GO_PATTERN,
    ITEM_SEPARATOR_PATTERN,
    LIST_ITEM_PATTERN,
    MISFIRE_NEAR_CHARS,
    MISFIRE_PATTERN,
    PASTE_MARKER,
    PLAN_CRITIQUE_PATTERN,
    PLAN_DOC_CHARS,
    PLAN_DOC_ITEMS,
    PLAN_FEEDBACK_SCAN_CHARS,
    PLAN_HEADING_PATTERN,
    PLAN_ITEM_PATTERN,
    PLAN_LONG_CHARS,
    PLAN_QUESTION_PATTERN,
    PLAN_UNSURE_PATTERN,
    PROSE_NOISE_PATTERN,
    QUESTION_PATTERN,
    REMIND_PATTERN,
    REPLY_FENCE_PATTERN,
    REPLY_INLINE_CODE_PATTERN,
    REPLY_LIST_START_PATTERN,
    REPLY_QUESTION_TRIM,
    REPLY_QUOTED_PATTERN,
    REPLY_REMINDER_LINE_PATTERN,
    REPLY_SCAN_CHARS,
    REPLY_TAGS_PATTERN,
    REPLY_TIP_BLOCK_PATTERN,
    REPLY_UNIT_PATTERN,
    REPLY_URL_PATTERN,
    RELEASE_PATTERN,
    REVIEW_PATTERN,
    SENTENCE_END_PATTERN,
    SHELL_WRITE_PATTERN,
    SPECIFIC_PATTERN,
    STATUS_MAX_CHARS,
    STATUS_PATTERN,
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
_BAD_OUTCOME_RE = re.compile(BAD_OUTCOME_PATTERN, re.IGNORECASE)
_CORRECTION_RE = re.compile(CORRECTION_PATTERN, re.IGNORECASE)
_SPECIFIC_RE = re.compile(SPECIFIC_PATTERN)
_QUESTION_RE = re.compile(QUESTION_PATTERN, re.IGNORECASE)
_ADJUST_RE = re.compile(ADJUST_PATTERN, re.IGNORECASE)
_REMIND_RE = re.compile(REMIND_PATTERN, re.IGNORECASE)
_GO_RE = re.compile(GO_PATTERN, re.IGNORECASE)
_STATUS_RE = re.compile(STATUS_PATTERN, re.IGNORECASE)
_PLAN_UNSURE_RE = re.compile(PLAN_UNSURE_PATTERN, re.IGNORECASE)
_PLAN_QUESTION_RE = re.compile(PLAN_QUESTION_PATTERN, re.IGNORECASE)
_PLAN_CRITIQUE_RE = re.compile(PLAN_CRITIQUE_PATTERN, re.IGNORECASE)
_ASKS_RE = re.compile(ASKS_PATTERN, re.IGNORECASE)
_ADMIT_RE = re.compile(ADMIT_PATTERN, re.IGNORECASE)
_MISFIRE_RE = re.compile(MISFIRE_PATTERN, re.IGNORECASE)
_CLAUDEGLASS_RE = re.compile(r"claudeglass", re.IGNORECASE)
_PROSE_NOISE_RE = re.compile(PROSE_NOISE_PATTERN)
_PLAN_HEADING_RE = re.compile(PLAN_HEADING_PATTERN)
_PLAN_ITEM_RE = re.compile(PLAN_ITEM_PATTERN)
_CHANGE_RE = re.compile(CHANGE_PATTERN, re.IGNORECASE)
_RELEASE_RE = re.compile(RELEASE_PATTERN, re.IGNORECASE)
_ERROR_TEXT_RE = re.compile(ERROR_TEXT_PATTERN)
_REVIEW_RE = re.compile(REVIEW_PATTERN, re.IGNORECASE)
_CONFIG_PATH_RE = re.compile(CONFIG_PATH_PATTERN)
_SHELL_WRITE_RE = re.compile(SHELL_WRITE_PATTERN, re.IGNORECASE)
_SHELL_TOOLS = ("Bash", "PowerShell")

#: What is cut from a reply before its words are read, and how it is cut
#: into sentences (see ``REPLY_SCAN_CHARS`` in the catalogue, which the
#: hook and the status line read too).
_FENCE_RE = re.compile(REPLY_FENCE_PATTERN)
_TIP_BLOCK_RE = re.compile(REPLY_TIP_BLOCK_PATTERN)
_INLINE_CODE_RE = re.compile(REPLY_INLINE_CODE_PATTERN)
_URL_RE = re.compile(REPLY_URL_PATTERN)
_QUOTED_RE = re.compile(REPLY_QUOTED_PATTERN)
_REMINDER_LINE_RE = re.compile(REPLY_REMINDER_LINE_PATTERN)
_TRAILING_TAGS_RE = re.compile(REPLY_TAGS_PATTERN)
_LIST_START_RE = re.compile(REPLY_LIST_START_PATTERN)
_UNIT_RE = re.compile(REPLY_UNIT_PATTERN)


def prose(text: str) -> str:
    """The first :data:`STEP_SCAN_CHARS` characters of ``text`` without
    what isn't your own writing: a fenced block, a quoted line and a pasted
    log or stack-trace line."""
    return _PROSE_NOISE_RE.sub(" ", text[:STEP_SCAN_CHARS])


def request_steps(text: str) -> int:
    """How many separate changes ``text`` asks for, in your own prose (see
    :func:`prose`): the most of its list lines ("1. ...", "- ..."), the
    change verbs it uses ("add ...", "then move ..."), and the items of one
    sentence that starts with a change verb ("Add login, a settings page
    and an admin screen" is 3). A message with no change verb asks for no
    change, whatever it lists: 0."""
    text = prose(text)
    actions = len(_ACTION_RE.findall(text))
    if not actions:
        return 0
    items = len(_LIST_ITEM_RE.findall(text))
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


def is_review(text: str) -> bool:
    """Whether ``text`` opens by asking Claude to look rather than change
    ("review the diff", "explain how X works", "can you check Y")."""
    return _REVIEW_RE.match(text) is not None


def is_plan(text: str) -> bool:
    """Whether ``text`` is a plan already: at least ``PLAN_LONG_CHARS``
    characters, whatever its formatting (a brief that long is written
    out), or, in its own prose, at least ``PLAN_DOC_CHARS`` characters with
    a heading, or ``PLAN_DOC_ITEMS`` listed items (numbered "1." or "1)",
    or opening with "-", "*" or a bullet)."""
    if len(text.strip()) >= PLAN_LONG_CHARS:
        return True
    text = prose(text)
    return (
        len(text.strip()) >= PLAN_DOC_CHARS and _PLAN_HEADING_RE.search(text) is not None
    ) or len(_PLAN_ITEM_RE.findall(text)) >= PLAN_DOC_ITEMS


def asks_for_release(text: str) -> bool:
    """Whether ``text`` asks to merge, release, ship or publish: the next
    step of work you already have, not a piece of work to plan first."""
    return _RELEASE_RE.search(text[:STEP_SCAN_CHARS]) is not None


def is_pasted(text: str) -> bool:
    """Whether ``text`` holds pasted code or a pasted log rather than only
    your own words: a fenced block, a stack trace or error line, or the
    desktop app's paste marker (the ``code`` and ``error`` words of
    ``Turn.prompt_flags``, and a paste)."""
    return "```" in text or PASTE_MARKER in text or _ERROR_TEXT_RE.search(text) is not None


def plan_steps(text: str) -> int:
    """The changes ``text`` asks for that call for a plan first
    (``plan_first``): :func:`request_steps`, or 0 for a message that
    mentions a plan (it follows one), opens by asking to review, is a plan
    already, asks to merge or release, or holds a pasted log or code (the
    steps counted in it are the paste's, not yours)."""
    if mentions_plan(text) or is_review(text) or is_plan(text) or asks_for_release(text) or is_pasted(text):
        return 0
    return request_steps(text)


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
    """A correction of ``max_chars`` or less that says something went
    wrong ("it's broken", "still failing", "that didn't work") but names
    nothing specific and doesn't say what it should be instead. It needs a
    correction or bad-outcome phrase, so a bare "fix this" is no match.
    Not a question ("what can you fix?", or anything ending in "?"), a
    go-ahead or an acknowledgement. A message with an image, or a retry
    after a reply that failed, is left out by whoever can tell: the
    parser (``has_image``) and ``prompting``."""
    head = text[:CORRECTION_SCAN_CHARS]
    return (
        (_BAD_OUTCOME_RE.search(head) is not None or _CORRECTION_RE.search(head) is not None)
        and len(text.strip()) <= max_chars
        and not _SPECIFIC_RE.search(text)
        and not _QUESTION_RE.match(text)
        and not text.rstrip().endswith("?")
        and not is_go(text)
        and not is_ack(text)
    )


def is_adjust(text: str) -> bool:
    """Whether ``text`` tweaks Claude's work ("actually, make it blue",
    "rename it", "a bit smaller") rather than asking: a message that ends
    in a question mark never does."""
    return _ADJUST_RE.search(text[:CORRECTION_SCAN_CHARS]) is not None and not text.rstrip().endswith("?")


def is_remind(text: str) -> bool:
    """Whether ``text`` repeats what you already said ("I told you",
    "you didn't", "why did you")."""
    return _REMIND_RE.search(text[:CORRECTION_SCAN_CHARS]) is not None


def is_go(text: str) -> bool:
    """Whether ``text`` only tells Claude to carry on: "continue", "go
    ahead", "do it", "implement the plan", "merge it"."""
    text = text.strip()
    return len(text) <= GO_MAX_CHARS and _GO_RE.fullmatch(text) is not None


def is_status(text: str) -> bool:
    """Whether ``text`` only asks how the work is going: "how is it
    going", "is it done", "any updates", "progress?"."""
    text = text.strip()
    return len(text) < STATUS_MAX_CHARS and _STATUS_RE.fullmatch(text) is not None


def is_question(text: str) -> bool:
    """Whether ``text`` asks something: it ends in a question mark or
    opens with a question word."""
    text = text.strip()
    return text.endswith("?") or _ASKS_RE.match(text) is not None


def is_change_request(text: str) -> bool:
    """Whether ``text`` asks Claude to change something: a change verb that
    opens a sentence ("make it bigger", "now move the logo", "can you add
    a footer.", see ``CHANGE_PATTERN``). Not a statement or a report ("the
    button is too small", "it does not load"), an explain or clarify
    request, a question, a go-ahead, a thank-you or a status check
    (``drip_feed``, the live hint and the report's small-requests count,
    counts these only: a run of small changes is what sending them as one
    message would have saved)."""
    text = text.strip()
    return (
        bool(text)
        and _CHANGE_RE.search(text[:CHANGE_SCAN_CHARS]) is not None
        and not (is_ack(text) or is_go(text) or is_status(text) or is_question(text) or is_review(text))
    )


def is_config_path(path: str) -> bool:
    """Whether ``path`` is inside a ``.claude`` folder: Claude's memory,
    plans and scripts, or a project's agents and skills, not your work."""
    return _CONFIG_PATH_RE.search(path) is not None


def writes_files(command: str) -> bool:
    """Whether a shell command looks like it changes a file the way
    ``shell_writes.write_targets`` reads one (the hook's approximation, as
    ``SHELL_WRITE_PATTERN``), outside a ``.claude`` folder."""
    return _SHELL_WRITE_RE.search(command) is not None and _CONFIG_PATH_RE.search(command) is None


def edits_files(name: str, tool_input: dict, edit_tools=EDIT_TOOLS) -> bool:
    """Whether a tool call changes a file of yours: an edit tool aimed
    outside a ``.claude`` folder, or a shell command that writes one."""
    if name in edit_tools:
        path = tool_input.get("file_path") or tool_input.get("notebook_path")
        return not (isinstance(path, str) and is_config_path(path))
    if name in _SHELL_TOOLS:
        command = tool_input.get("command")
        return isinstance(command, str) and writes_files(command)
    return False


def agent_edit_files(record: dict) -> int:
    """How many files the subagent whose result is this ``user`` line
    changed (``toolUseResult.toolStats.editFileCount``): 0 for any other
    line."""
    result = record.get("toolUseResult")
    stats = result.get("toolStats") if isinstance(result, dict) else None
    count = stats.get("editFileCount") if isinstance(stats, dict) else None
    return count if isinstance(count, int) and not isinstance(count, bool) and count > 0 else 0


def drip_count(earlier: list[dict], prompt: str, since, answer: bool, th: dict) -> int:
    """How many small change requests in a row ``prompt`` makes: it and
    the messages before it (``earlier``, each ``{"text", "answered",
    "answer", "edited", "since"}``, oldest first), each short, asking for a
    change (:func:`is_change_request`) and sent within
    ``drip_window_minutes`` of the message of yours before it (``since``,
    in seconds: your messages alone set the window), the earlier ones each
    answered with a change to a file, by the reply that message started
    (``edited``). Whatever asks for no change (a go-ahead, a thank-you, a
    status check, a question, a statement, a report or an explain request)
    neither counts nor ends a run, nor does an answer to Claude's question
    or a message stopped before any reply and sent again. ``prompt`` itself
    must be a change request, sent in time and not an answer; the run it
    makes is ``0`` otherwise. ``prompting._drip_runs`` counts the same runs
    after the fact, and ``capture_hook.py`` keeps a copy of this, held to
    it by a test."""
    window = th["drip_window_minutes"] * 60

    def small(text: str) -> bool:
        return len(text.strip()) <= th["drip_chars"]

    if not small(prompt) or not is_change_request(prompt) or answer or since is None or since > window:
        return 0
    count = 1
    for ex in reversed(earlier):
        if ex["answer"] or not ex["answered"] or not is_change_request(ex["text"]):
            continue
        if not (small(ex["text"]) and ex["edited"] and ex["since"] is not None and ex["since"] <= window):
            break
        count += 1
    return count


def _reply_prose(text: str) -> str:
    """A reply's words without its fenced code, a ClaudeGlass quote block
    (tip or reminder), inline code and URLs."""
    text = _FENCE_RE.sub(" ", text)
    text = _TIP_BLOCK_RE.sub("", text)
    return _URL_RE.sub(" ", _INLINE_CODE_RE.sub(" ", text))


def _asks(unit: str) -> bool:
    return unit.rstrip().rstrip(REPLY_QUESTION_TRIM).endswith("?")


def ends_on_question(text: str) -> bool:
    """Whether a reply ends on a question to you. The question mark must
    close one of its last two sentences, or one of the list items that
    end it ("Which do you want?", "- Should I keep X?"), after cutting
    code, URLs, a ClaudeGlass tip and the feedback reminder, a question
    inside quotation marks and the ``[cg: ...]`` tag. A question mark in
    the middle of the last paragraph's words, or a rhetorical one many
    sentences back, doesn't count."""
    prose = _QUOTED_RE.sub(" ", _REMINDER_LINE_RE.sub("", _reply_prose(text)))
    prose = _TRAILING_TAGS_RE.sub("", prose.rstrip()).rstrip()
    units = [unit for unit in _UNIT_RE.split(prose[-REPLY_SCAN_CHARS:]) if unit.strip()]
    end = len(units)
    while end and _LIST_START_RE.match(units[end - 1]):
        if _asks(units[end - 1]):
            return True
        end -= 1
    return any(_asks(unit) for unit in units[max(0, end - 2):end])


def admits_mistake(text: str) -> bool:
    """Whether a reply's text block owns a mistake ("I was wrong", "my
    mistake", "I should have checked", "you're right"): the catalogue's
    admit pattern, read in the block's first characters after cutting
    code, URLs and a ClaudeGlass quote block."""
    head = _reply_prose(text)[:ADMIT_SCAN_CHARS]
    return _ADMIT_RE.search(head.replace("\u2019", "'")) is not None


def disowns_tip(text: str) -> bool:
    """Whether a reply says, near the word "ClaudeGlass" and outside a
    ClaudeGlass quote block, that a tip misfired, was a false positive or
    doesn't apply. The caller decides whether the reply carries a tip."""
    if _CLAUDEGLASS_RE.search(text) is None:
        return False
    prose = _reply_prose(text).replace("\u2019", "'")
    return any(
        _MISFIRE_RE.search(prose[max(0, mention.start() - MISFIRE_NEAR_CHARS):mention.end() + MISFIRE_NEAR_CHARS])
        for mention in _CLAUDEGLASS_RE.finditer(prose)
    )


def plan_feedback_class(text: str) -> str:
    """How feedback on a rejected plan reads, a word from
    ``PLAN_FEEDBACK_CLASSES``: "unsure" when you say you aren't sure,
    "question" when you ask something, "critique" when you point at
    something wrong or to change, else "other". Only the word is kept."""
    head = text.strip()[:PLAN_FEEDBACK_SCAN_CHARS]
    if _PLAN_UNSURE_RE.search(head):
        return "unsure"
    if head.rstrip().endswith("?") or _PLAN_QUESTION_RE.match(head):
        return "question"
    if _PLAN_CRITIQUE_RE.search(head) or _CORRECTION_RE.search(head[:CORRECTION_SCAN_CHARS]) or _ADJUST_RE.search(head):
        return "critique"
    return "other"

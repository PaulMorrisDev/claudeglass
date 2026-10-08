"""Event detection: turn one non-assistant JSONL line into an ``Event``.

Implements plan Appendix A2's detection table exactly, in the table's
order (first match wins), plus its ``PRECEDENCE`` list for resolving
``Turn.preceding_primary`` from the events collected since the previous
finalised turn.

Two members of ``model.EventKind`` are not covered by the plan's
precedence text (``LOCAL_COMMAND`` and ``META``) but are exercised by
other rows of the detection table. This module places them just above
``UNKNOWN`` at the bottom of ``PRECEDENCE`` (lowest precedence, below
``API_ERROR``) — a documented judgement call, not a plan restatement.

Usage-limits batch (v3-limits, see model.py's module docstring for the
new ``EventKind``/``Turn``/``Diagnostics`` fields this adds): three new
rows in the detection table, inserted ahead of the more generic row they
would otherwise fall into --

- ``AGENT_TERMINATED`` is checked before ``TASK_NOTIFICATION`` (13.5):
  the "Agent terminated early due to ..." lines are shaped exactly like
  a task-notification (same ``origin.kind``/``<task-notification`` tag)
  but report a more specific harness event.
- ``LIMIT_RESUME`` is checked before ``HUMAN_TEXT`` (19.5): the desktop
  app's automatic resume ping is a human-origin, ``promptSource: "sdk"``
  line, which would otherwise match the generic HUMAN_TEXT row.
- ``LIMIT_HIT`` is *not* produced by :func:`classify_line` at all --
  the "You've hit your session/weekly limit" text lives on a synthetic
  *assistant* line (``model: "<synthetic>"``), and assistant lines are
  never events (see this function's docstring). ``parse.py`` classifies
  that text via :func:`classify_synthetic_text` and synthesises the
  ``LIMIT_HIT`` event itself once it knows the turn is synthetic.
  :func:`classify_synthetic_text`, :func:`parse_limit_reset_clause` and
  :func:`parse_limit_reset_date` live here anyway, alongside every other
  piece of text-shape knowledge.

Both new precedence entries (``LIMIT_HIT``, ``LIMIT_RESUME``) rank above
``INTERRUPT`` in ``PRECEDENCE`` per the v3-limits brief: a usage-cap
pause is a stronger explanation for a gap than a plain interrupt.
``AGENT_TERMINATED`` ranks just above ``TASK_NOTIFICATION`` (a judgement
call, not part of the brief's explicit precedence list — a terminated
subagent is a more specific/important signal than a generic
notification, but not as strong as an interrupt or a human message).

Parser-signals batch (SURV-4/6/7, ``PARSER_VERSION`` 19, see model.py's
module docstring for the new ``EventKind``/``TranscriptResult`` fields):
new row 10.5 (``TASK_STATUS``/``STRUCTURED_OUTPUT``, ranked alongside
``QUEUE_OPERATION`` in ``PRECEDENCE``), ``thinking_drop`` joining the
``CACHE_SIGNAL`` family (rule 7), and a shared image/document block
sizing helper (:func:`content_block_size`, :func:`image_token_estimate`)
used both by :func:`_human_text_metrics` here and by ``parse.py``'s
``_tool_result_length``. :func:`sanitize_line_type` is this batch's other
export, used by ``parse.py`` for the new ``parser_notes
["unknown_line_types"]`` counter (SURV-6) so a corrupted/hostile ``type``
field can never reach a diagnostic counter's key verbatim.

Parser-signals batch (``PARSER_VERSION`` 37, see model.py's module
docstring): a ``queued_command`` that carries a message you typed while
Claude was working keeps its ``QUEUE_OPERATION`` kind, so it never opens
a cycle, but its detail now says who sent it and how it reads
(:func:`_queued_prompt_detail`, sharing :func:`_message_flags` with a
typed message). A ``user`` line you didn't type -- one of
``capture_catalogue.NOT_TYPED_PREFIXES``, or one a ``turnOrigin`` rules
out -- is a ``META`` event with subkind ``not_typed`` (19.9), or
``resume`` for the notes that carry on your last message's work,
checked just before ``HUMAN_TEXT``. ``output_style`` is a ``REMINDER`` (``parse.py``
promotes a change of style), and ``permission-mode`` and ``system``
``informational`` lines are ignored.

Plan-feedback batch (``PARSER_VERSION`` 37, see model.py's module
docstring): an interrupt line gets a subkind from :func:`_interrupt_subkind`
(``tool_refusal`` for the "for tool use" tail of a turned-down call,
``shutdown`` for the session's end), its kind unchanged so it is still no
typed message. ``DENIAL_BUCKETS`` names what each denied call was, set by
``parse.py`` into ``detail["bucket"]`` (:func:`denial_bucket_of`), and
:func:`is_stop` / :func:`stop_window` say which interrupts are you stopping
a reply, for quality, prompting and waste. ``PLAN_FEEDBACK`` is emitted by
``parse.py``, never classified from a line, and ranks just below
``HUMAN_TEXT``.

Startup-measures batch (``PARSER_VERSION`` 41): a ``prompt_snapshot``'s
detail also carries each built-in tool's definition size by name
(``tool_chars``) and each MCP server's total (``server_chars``), no
descriptions and no other name. ``BUILT_IN_TOOLS``, ``TOOL_NAME_RE`` and
:func:`tool_server`, the rule that splits them, live here and ``parse.py``
re-exports them.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Iterable, Sequence

from . import known_savers, prompt_shape
from .capture_catalogue import (
    COACH_MARKER,
    COACHING_HINTS,
    CORRECTION_PATTERN,
    CORRECTION_SCAN_CHARS,
    ERROR_TEXT_PATTERN,
    FEEDBACK_FACTS_MARKER,
    FEEDBACK_HINTS,
    HOOK_SCRIPT,
    LIMIT_RESUME_PREFIX,
    NOT_TYPED_PREFIXES,
    NOT_TYPED_TURN_ORIGINS,
    NOTE_MARKER,
    OLD_COACH_MARKER,
    OLD_NOTE_MARKER,
    PASTE_MARKER,
    REPORT_THRESHOLDS,
    RESUME_PREFIXES,
    RETIRED_COACHING_HINTS,
    SESSION_LIMIT_PREFIX,
    WEEKLY_LIMIT_PREFIX,
)
from .capture_tags import parse_brief_markers, parse_note_codes
from .model import Event, EventKind

# -- Ignored outright but counted by the caller (returns None) ----------

#: Exact top-level ``type`` values that are ignored outright. The
#: prefix-matched families (``file-history-*``, ``artifact-*``) are
#: handled separately in ``_is_ignorable_type``.
_IGNORABLE_TYPES = frozenset(
    {
        "bridge-session",
        "last-prompt",
        "custom-title",
        "ai-title",
        "agent-name",
        "agent-setting",
        "pr-link",
        "atis-latch",
        "mode",
        "frame-link",
        #: Parser-signals addition (see model.py's module docstring): a
        #: known, deliberately-ignored-as-an-event type, like "mode"/
        #: "agent-setting" above -- its own totalCostUSD/hasUnknownModelCost
        #: are read directly by parse.parse_transcript instead.
        "cost-state",
        #: Parser-signals addition (PARSER_VERSION 37): the permission
        #: mode is already on each user line's own ``permissionMode``.
        "permission-mode",
    }
)
_IGNORABLE_PREFIXES = ("file-history-", "artifact-")
#: Parser-signals addition (PARSER_VERSION 37): ``system`` lines of these
#: subtypes say nothing a counter uses, so they are ignored like the
#: types above rather than left to land as ``UNKNOWN``.
_IGNORABLE_SYSTEM_SUBTYPES = frozenset({"informational"})


def _is_ignorable_type(line_type: str | None) -> bool:
    if line_type in _IGNORABLE_TYPES:
        return True
    if isinstance(line_type, str):
        return line_type.startswith(_IGNORABLE_PREFIXES)
    return False


# -- Attachment-type buckets (plan A2's CACHE_SIGNAL/REMINDER/etc rows) --

_HOOK_ATTACHMENT_TYPES = frozenset(
    {
        "hook_success",
        "hook_non_blocking_error",
        "hook_blocking_error",
        "hook_system_message",
        "hook_additional_context",
        "hook_cancelled",
    }
)

#: SURV-HE: every hook *event* name Claude Code documents (verified
#: against code.claude.com/docs/en/hooks.md 2026-09-24) -- closed, so a
#: hook attachment's event is safe to keep on ``Event.detail``. The
#: matcher/tool-name suffix after ":" (e.g. the "Bash" in
#: "PreToolUse:Bash") is always dropped: an MCP-matched hook's name there
#: (``"PreToolUse:mcp__server__tool"``) could otherwise identify which
#: MCP servers/tools someone has configured (G7's "MCP tool names in
#: signals" caution) -- the same reason ``_CAPTURE_NOTE_HOOKS`` below
#: only ever keeps the event name, never the matcher, for a capture
#: note's own ``detail["hook"]``. Not exhaustive against every future
#: hook event Claude Code might add; anything not in this set becomes
#: "other" (see :func:`_hook_name_bucket`), the same closed-vocabulary-
#: plus-fallback shape ``_CAPTURE_NOTE_HOOKS`` already uses.
_HOOK_EVENT_NAMES = frozenset(
    {
        "SessionStart",
        "SessionEnd",
        "UserPromptSubmit",
        "Stop",
        "StopFailure",
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "PostToolBatch",
        "SubagentStart",
        "SubagentStop",
        "PreCompact",
        "PostCompact",
        "Notification",
        "PermissionRequest",
        "PermissionDenied",
        "TeammateIdle",
        "TaskCreated",
        "TaskCompleted",
        "WorktreeCreate",
        "WorktreeRemove",
        "MessageDisplay",
        "PreModelSwitch",
        "PostModelSwitch",
        "ConfigChange",
        "InstructionsLoaded",
        "CwdChanged",
        "FileChanged",
        "DirectoryAdded",
        "Elicitation",
        "ElicitationResult",
        "Setup",
    }
)


def hook_event_name(attachment: dict) -> str:
    """Public name for :func:`_hook_name_bucket`, for parse.py's pairing
    of a hook run with the context it added."""
    return _hook_name_bucket(attachment)


def _hook_name_bucket(attachment: dict) -> str:
    """The hook *event* ``attachment.get("hookName")`` fired under
    (``"PreToolUse"``, never ``"PreToolUse:Bash"``), or ``"other"`` when
    it's missing, not a string, or not one of :data:`_HOOK_EVENT_NAMES`
    -- see that set's own docstring for why the matcher/tool-name suffix
    is always dropped."""
    hook_name = attachment.get("hookName")
    if not isinstance(hook_name, str):
        return "other"
    event = hook_name.split(":", 1)[0]
    return event if event in _HOOK_EVENT_NAMES else "other"


def _hook_output_detail(attachment: dict) -> dict:
    """``detail`` for a non-capture-note HOOK_OUTPUT event: the closed
    hook-event bucket (:func:`_hook_name_bucket`), the real
    ``durationMs`` Claude Code recorded for the call when there is one
    (CAP-9/F10: a hook_success entry carries it -- fixture: 140 ms for a
    SessionStart hook -- and this used to be dropped), and, only when
    ``True``, whether the call ran ClaudeGlass's own hook script. That
    last check never keeps the command string itself -- only whether it
    names ``capture_catalogue.HOOK_SCRIPT``, the same substring check
    ``hook_health.py`` already uses on settings.json commands -- so the
    measured hook overhead can find ClaudeGlass's own calls among a
    settings.json that may run other hooks on the same events too.
    """
    detail: dict = {"hookName": _hook_name_bucket(attachment)}
    duration = attachment.get("durationMs")
    if isinstance(duration, (int, float)):
        detail["durationMs"] = duration
    command = attachment.get("command")
    if isinstance(command, str) and HOOK_SCRIPT in command:
        detail["capture"] = True
    return detail

_CACHE_SIGNAL_TYPES = frozenset(
    {
        "model",
        "thinking_stripped",
        #: Parser-signals addition (SURV-4, see model.py's module
        #: docstring): a model dropped its own prior extended-thinking
        #: blocks (a prefix mismatch) -- a likely cache-bust, same family
        #: as "thinking_stripped".
        "thinking_drop",
        "ultra_effort_enter",
        "ultra_effort_exit",
        "deferred_tools_delta",
        "deferred_tools_record",
        "mcp_instructions_delta",
        "agent_listing_delta",
        "plan_mode",
        "plan_mode_exit",
        "auto_mode",
        "auto_mode_exit",
        "output_style_instructions",
    }
)

#: The subset of CACHE_SIGNAL subkinds that rank in the *high* precedence
#: band (just below MODEL_FALLBACK); every other CACHE_SIGNAL subkind
#: ranks in the *other* band (between SLASH_COMMAND and HOOK_OUTPUT).
_CACHE_SIGNAL_HIGH_SUBKINDS = frozenset(
    {"model", "thinking_stripped", "ultra_effort_enter", "ultra_effort_exit"}
)

#: attachment.type -> (added-list key, removed-list key) for the three
#: delta types whose ``Event.detail`` carries counts only (their raw
#: added-content lists, e.g. ``mcp_instructions_delta.addedBlocks``, hold
#: full instruction/skill text and must never be stored).
_DELTA_COUNT_KEYS = {
    "deferred_tools_delta": ("addedNames", "removedNames"),
    "agent_listing_delta": ("addedTypes", "removedTypes"),
    "mcp_instructions_delta": ("addedBlocks", "removedNames"),
}

_REMINDER_TYPES = frozenset(
    {
        "total_tokens_reminder",
        "batching_reminder_sent",
        "silent_turn_reminder",
        "task_reminder",
        "date",
        "date_change",
        #: Parser-signals addition (PARSER_VERSION 37): Claude Code writes
        #: one on every request, with the same style each time, so it
        #: marks nothing. ``parse.parse_transcript`` promotes one to a
        #: ``CACHE_SIGNAL`` when the style differs from the last.
        "output_style",
    }
)

_CONTEXT_INJECT_TYPES = frozenset(
    {
        "file",
        "edited_text_file",
        "read_truncation_notice",
        "nested_memory",
        "prompt_snapshot",
        "compact_file_reference",
        "plan_file_reference",
        "session_context",
        "environment",
        "instructions",
        "skill_listing",
        "invoked_skills",
        "directory",
        "inlined_image_paths",
        "remote_session_change",
        "workflow_keyword_request",
    }
)

_SLASH_COMMAND_PREFIXES = ("<command-name", "<local-command-stdout", "<local-command-caveat")
#: A slash command's own name (``<command-name>/grill-me</command-name>``),
#: kept on ``Event.detail["command"]`` only when it has the shape of a
#: command or skill name -- never its arguments.
_COMMAND_NAME_RE = re.compile(r"<command-name>/?([A-Za-z0-9][A-Za-z0-9_.:-]{0,63})</command-name>")
#: A skill you ran with a slash (``/grill-me``) is written the other way
#: round, ``<command-message>`` first, and is your message for that
#: cycle: a HUMAN_TEXT line whose ``detail["command"]`` names the skill.
_SKILL_COMMAND_PREFIX = "<command-message>"
_SCHEDULED_TASK_PREFIXES = ("<scheduled-task", "[SYSTEM NOTIFICATION", "<<autonomous-loop")
_SCHEDULED_TASK_ORIGIN_KINDS = frozenset({"cron", "loop"})


def _user_str_content(d: dict) -> str | None:
    message = d.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    return content if isinstance(content, str) else None


def _user_has_text_or_image_list(d: dict) -> bool:
    message = d.get("message")
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    if not isinstance(content, list):
        return False
    return any(
        isinstance(block, dict) and block.get("type") in ("text", "image") for block in content
    )


def _interrupt_block_text(d: dict) -> str | None:
    """The text of a ``[Request interrupted`` text block in a user line's
    content, or ``None``. Read here for its wording and dropped."""
    message = d.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if not isinstance(content, list):
        return None
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str) and text.startswith("[Request interrupted"):
                return text
    return None


def _user_has_tool_result(d: dict) -> bool:
    message = d.get("message")
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    if not isinstance(content, list):
        return False
    return any(isinstance(block, dict) and block.get("type") == "tool_result" for block in content)


def _is_saver_redirect(d: dict) -> bool:
    """Whether a denied call's tool result is a known token saver's
    redirect (``known_savers.saver_for_text``). Text is matched, never
    kept."""
    message = d.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return False
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        inner = block.get("content")
        if isinstance(inner, str):
            texts = [inner]
        elif isinstance(inner, list):
            texts = [part.get("text") for part in inner if isinstance(part, dict)]
        else:
            continue
        if any(isinstance(text, str) and known_savers.saver_for_text(text) for text in texts):
            return True
    return False


#: What a denied tool call was, as ``Event.detail["bucket"]`` (set by
#: ``parse.py``, which knows the tool and what its result said): the plan
#: you sent back, a question you declined to answer, a hook's or guard's
#: block, the auto mode classifier's refusal or its failure to answer, a
#: dialog you closed without answering, and a call you turned down.
DENIAL_BUCKETS = (
    "plan_rejected", "question_declined", "hook_blocked", "auto_blocked", "auto_unavailable", "aborted", "refused",
)

#: The bucket of a denial by its ``toolDenialKind`` alone, when no tool or
#: result text says more. A kind not listed here isn't a call you turned
#: down.
_BUCKET_BY_KIND = {
    "permission-rule": "refused",
    "user-rejected": "refused",
    "automode-blocked": "auto_blocked",
    "automode-unavailable": "auto_unavailable",
    known_savers.REDIRECT_DENIAL_KIND: "hook_blocked",
}


def denial_bucket_for_kind(kind: str | None) -> str:
    """The bucket word for a ``toolDenialKind`` alone."""
    return _BUCKET_BY_KIND.get(kind or "", "aborted")


def denial_bucket_of(event: Event) -> str:
    """The bucket word of a ``TOOL_DENIAL`` event: the one ``parse.py``
    recorded, else one from its ``toolDenialKind``."""
    bucket = event.detail.get("bucket")
    return bucket if bucket in DENIAL_BUCKETS else denial_bucket_for_kind(event.subkind)


#: The wording of an interrupt that tells its ``subkind`` apart: the end
#: of the session, or the ``for tool use`` tail of a turned-down call.
_INTERRUPT_SHUTDOWN_RE = re.compile(r"shut ?down|session (?:ended|closed)|app (?:was )?(?:quit|closed)", re.IGNORECASE)
_INTERRUPT_TOOL_RE = re.compile(r"for tool use", re.IGNORECASE)
_INTERRUPT_SCAN_CHARS = 200


def _interrupt_subkind(text: str) -> str | None:
    """``"shutdown"`` for an interrupt the session's end wrote,
    ``"tool_refusal"`` for the ``for tool use`` line that follows a tool
    call turned down, else ``None`` (you stopped a reply). Read here for
    its wording and dropped."""
    head = text[:_INTERRUPT_SCAN_CHARS]
    if _INTERRUPT_SHUTDOWN_RE.search(head):
        return "shutdown"
    if _INTERRUPT_TOOL_RE.search(head):
        return "tool_refusal"
    return None


#: The buckets after which a ``for tool use`` interrupt is still you
#: stopping Claude: a call you turned down, a dialog you closed. After a
#: plan or question you answered, or a call a hook or the classifier
#: blocked, the line is only how Claude Code ends that turn.
_STOP_AFTER_BUCKETS = ("refused", "aborted")


def is_stop(event: Event) -> bool:
    """Whether an ``INTERRUPT`` event is you stopping a reply. Not the
    session's end, and not the tail Claude Code writes after a plan or
    question you answered or a call a hook or the classifier blocked
    (``detail["after"]`` is that denial's bucket)."""
    if event.kind != EventKind.INTERRUPT or event.subkind == "shutdown":
        return False
    return event.subkind != "tool_refusal" or event.detail.get("after") in (None, *_STOP_AFTER_BUCKETS)


def is_bare_stop(event: Event) -> bool:
    """Whether an ``INTERRUPT`` event is a bare stop: Esc on a reply, with
    no subkind. The tail of a call you turned down (``tool_refusal``,
    whatever denial it follows) and the session's end (``shutdown``) are
    not: a refusal is a decision about one call, not Claude being stopped
    again and again. The stop-loop report counts these only; the waste and
    quality sections keep :func:`is_stop`."""
    return event.kind == EventKind.INTERRUPT and not event.subkind


def stop_window(denials: dict) -> bool:
    """Whether an interrupt among the denials since a reply
    (``Turn.preceding_denials``) is you stopping it: there were none, or
    only calls you turned down or dialogs you closed."""
    return all(bucket in _STOP_AFTER_BUCKETS for bucket, count in denials.items() if count)


#: attachment.type -> the raw attachment fields holding the text the
#: model is shown, used when a line carries no ``rendered`` field (e.g.
#: ~15% of real ``skill_listing`` lines). Lengths only -- never stored.
#: ``hook_system_message`` is deliberately absent: it is a message shown to
#: you in the terminal, never to the model (no real line of it carries
#: ``rendered``, even from versions that render every other hook type), so
#: it takes no context.
_CONTENT_SIZE_FIELDS = {
    "skill_listing": ("content",),
    "deferred_tools_delta": ("addedLines",),
    "mcp_instructions_delta": ("addedBlocks",),
    "agent_listing_delta": ("addedLines",),
    "hook_additional_context": ("content",),
    "hook_success": ("content",),
    "total_tokens_reminder": ("text",),
    "batching_reminder_sent": ("text",),
    "silent_turn_reminder": ("text",),
    "model": ("text",),
    "queued_command": ("prompt",),
    "edited_text_file": ("snippet",),
    "plan_file_reference": ("planContent",),
    "directory": ("content",),
}


def _text_chars(value: object) -> int:
    """Summed length of a string, or of every string in a list."""
    if isinstance(value, str):
        return len(value)
    if isinstance(value, list):
        return sum(len(item) for item in value if isinstance(item, str))
    return 0


def _attachment_content_chars(attachment: dict) -> int | None:
    """Size of an attachment measured from its own content fields, for a
    line with no ``rendered`` field. ``None`` for a type with no known
    content field."""
    attachment_type = attachment.get("type")
    if attachment_type == "instructions":
        files = attachment.get("files")
        if not isinstance(files, list):
            return None
        return sum(_text_chars(f.get("content")) for f in files if isinstance(f, dict))
    if attachment_type == "nested_memory":
        content = attachment.get("content")
        return _text_chars(content.get("content")) if isinstance(content, dict) else None
    fields = _CONTENT_SIZE_FIELDS.get(attachment_type)
    if fields is None:
        return None
    present = [attachment[field] for field in fields if field in attachment]
    if not present:
        return None
    return sum(_text_chars(value) for value in present)


#: The text Claude Code wraps a hook's additional context in, around the
#: hook's name (``SessionStart``, ``PostToolUse:Bash``): "<system-reminder>\n"
#: + name + " hook additional context: " + text + "\n</system-reminder>".
#: Measured against real ``rendered`` lines; used to size a capture note
#: when a line has no ``rendered`` field.
_HOOK_CONTEXT_WRAPPER_CHARS = 63

#: The hook events a capture or coaching note is injected by, kept on the
#: note's ``Event.detail["hook"]``; anything else is recorded as "other".
_CAPTURE_NOTE_HOOKS = frozenset({"SessionStart", "SubagentStart", "PostToolUse", "UserPromptSubmit"})

#: A coaching note's marker: ``cg-coach v1 quiet_output`` (``tl-coach``
#: until 0.12.1).
_COACH_RE = re.compile(f"(?:{re.escape(COACH_MARKER)}|{re.escape(OLD_COACH_MARKER)})" + r"(\d+) ([a-z_]+)")

#: The kind of the line a /cg-feedback run starts with (the facts line,
#: ``capture_catalogue.FEEDBACK_FACTS_MARKER``), kept as a coaching note's
#: kind so it counts as ClaudeGlass's own hook context.
FEEDBACK_FACTS_KIND = "feedback_facts"

#: The hints a note's kind may name: today's, the plan check's and the
#: rating reminder's (``capture_catalogue.FEEDBACK_HINTS``), and the ones
#: that no longer show live but are still in old transcripts, so a note
#: from before the change keeps its kind instead of reading as ``other``.
_KNOWN_HINTS = frozenset((*COACHING_HINTS, *FEEDBACK_HINTS, *RETIRED_COACHING_HINTS, FEEDBACK_FACTS_KIND))


def _find_marker(text: str, *markers: str) -> int:
    """Where the first of ``markers`` starts in ``text``, or -1."""
    found = [at for at in (text.find(m) for m in markers) if at >= 0]
    return min(found) if found else -1


def _capture_note(d: dict, attachment: dict) -> tuple[str, int, dict] | None:
    """Metrics-capture addition: ``(subkind, chars, detail)`` for a
    ``hook_additional_context`` line carrying ClaudeGlass's capture note
    (``capture_catalogue.NOTE_MARKER``, subkind ``capture_note``) or only
    a coaching note (``COACH_MARKER``, ``coaching_note``), else ``None``.
    The facts line a /cg-feedback run starts with
    (``FEEDBACK_FACTS_MARKER``) is a ``coaching_note`` of kind
    ``feedback_facts``.
    ``chars`` is what the model was shown, from ``rendered`` when
    present. ``detail`` holds the note format version, its metric codes
    (a coaching note: its hint, ``kind``) and the hook event -- never the
    note's text. A capture note the same hook call added a coaching note
    to keeps the coaching part apart: ``detail["coach"]`` (its hint) and
    ``detail["coach_chars"]``, taken out of ``chars``."""
    content = attachment.get("content")
    if isinstance(content, str):
        texts = [content]
    elif isinstance(content, list):
        texts = [item for item in content if isinstance(item, str)]
    else:
        texts = []
    text = "\n".join(texts)
    note_at = _find_marker(text, NOTE_MARKER, OLD_NOTE_MARKER)
    coach_at = _find_marker(text, COACH_MARKER, OLD_COACH_MARKER)
    facts = note_at < 0 and coach_at < 0 and FEEDBACK_FACTS_MARKER in text
    if note_at < 0 and coach_at < 0 and not facts:
        return None
    chars = _rendered_size_chars(d, attachment) if d.get("rendered") is not None else None
    if chars is None:
        hook_name = attachment.get("hookName")
        chars = len(text) + _HOOK_CONTEXT_WRAPPER_CHARS + (len(hook_name) if isinstance(hook_name, str) else 0)
    hook_event = attachment.get("hookEvent")
    hook = hook_event if hook_event in _CAPTURE_NOTE_HOOKS else "other"
    coach: dict = {}
    if facts:
        return "coaching_note", chars, {"v": 1, "kind": FEEDBACK_FACTS_KIND, "hook": hook}
    if coach_at >= 0:
        match = _COACH_RE.match(text, coach_at)
        coach = {
            "v": int(match.group(1)) if match else None,
            "kind": match.group(2) if match and match.group(2) in _KNOWN_HINTS else "other",
        }
    if note_at < 0 or note_at > coach_at >= 0:
        return "coaching_note", chars, {**coach, "hook": hook}
    version, codes = parse_note_codes(text[:coach_at] if coach_at >= 0 else text)
    detail = {
        "v": version,
        "codes": list(codes),  # a list, as it reads back from the cache
        "hook": hook,
    }
    if coach:
        # The hook joins the two with a newline, the coaching note last.
        coach_chars = min(chars, len(text) - coach_at + 1)
        detail.update(coach=coach["kind"], coach_chars=coach_chars)
        chars -= coach_chars
    return "capture_note", chars, detail


def _rendered_size_chars(d: dict, attachment: dict) -> int | None:
    """Length of the text an attachment line puts in front of the model.

    Real transcripts carry it as a top-level ``rendered`` list of
    ``{"content": str}`` blocks (a bare string is accepted too, for older
    lines). Without ``rendered``, falls back to the attachment's own
    content fields (:func:`_attachment_content_chars`).
    """
    rendered = d.get("rendered")
    if isinstance(rendered, str):
        return len(rendered)
    if isinstance(rendered, list):
        blocks = [block.get("content") for block in rendered if isinstance(block, dict)]
        texts = [text for text in blocks if isinstance(text, str)]
        if texts:
            return sum(len(text) for text in texts)
    return _attachment_content_chars(attachment)


#: ``instructions.files[].type`` values kept as ``Event.detail`` keys
#: (a fixed label set, never a path) so startup context can be split by
#: where each instruction file comes from.
_INSTRUCTION_FILE_TYPES = frozenset({"User", "Project", "Local", "AutoMem", "Managed"})


def _path_hash(path: object) -> str | None:
    """Salted hash of an instruction file's path (``parse._read_target_hash``),
    so a file can be matched to the same file on disk later without the
    path itself being stored. ``None`` with no salt set or no path."""
    if not isinstance(path, str) or not path:
        return None
    from . import parse  # parse imports this module; resolved at call time

    return parse._read_target_hash(path)


def _instruction_file_record(entry: dict) -> dict:
    """One instruction file as ``{"hash", "type", "scoped", "chars"}``:
    a salted path hash, the type label, whether it loads only for
    matching paths (it has ``globs``), and its size. Never its text or
    path."""
    file_type = entry.get("type")
    record: dict = {
        "type": file_type if file_type in _INSTRUCTION_FILE_TYPES else "Other",
        "scoped": bool(entry.get("globs")),
        "chars": _text_chars(entry.get("content")),
    }
    hashed = _path_hash(entry.get("path"))
    if hashed is not None:
        record["hash"] = hashed
    return record


def _instructions_detail(attachment: dict) -> dict:
    """Chars per instruction-file type (``User``/``Project``/...), a file
    count, and one record per file (:func:`_instruction_file_record`).
    Unknown types are summed under ``Other``."""
    files = attachment.get("files")
    if not isinstance(files, list):
        return {}
    by_type: dict[str, int] = {}
    records: list[dict] = []
    for entry in files:
        if not isinstance(entry, dict):
            continue
        record = _instruction_file_record(entry)
        records.append(record)
        by_type[record["type"]] = by_type.get(record["type"], 0) + record["chars"]
    return {"count": len(records), "chars_by_type": by_type, "files": records}


def _nested_memory_detail(attachment: dict) -> dict:
    """The one file a ``nested_memory`` attachment loads (a subfolder
    CLAUDE.md or a ``.claude/rules`` file), as a single file record."""
    content = attachment.get("content")
    if not isinstance(content, dict):
        return {}
    return {"files": [_instruction_file_record(content)]}


#: One ``- name: description`` line of a ``skill_listing``'s content.
_SKILL_LINE_RE = re.compile(r"^- ([^\s:][^\n]*?): ", re.MULTILINE)


def _skill_listing_detail(attachment: dict) -> dict:
    """Skill count, plus each listed skill's name and the characters its
    listing line takes. Names are kept (they are labels, like
    ``Turn.attribution_skill``); descriptions never are. A name is kept
    only when it is also in the attachment's own ``names`` list."""
    detail: dict = {}
    if isinstance(attachment.get("skillCount"), int):
        detail["count"] = attachment["skillCount"]
    content = attachment.get("content")
    names = attachment.get("names")
    if not isinstance(content, str) or not isinstance(names, list):
        return detail
    known = {name for name in names if isinstance(name, str)}
    starts = [(match.start(), match.group(1)) for match in _SKILL_LINE_RE.finditer(content)]
    skills: list[dict] = []
    for index, (start, name) in enumerate(starts):
        if name not in known:
            continue
        end = starts[index + 1][0] if index + 1 < len(starts) else len(content)
        skills.append({"name": name, "chars": len(content[start:end].rstrip("\n"))})
    if skills:
        detail["skills"] = skills
    return detail


def _invoked_skills_detail(attachment: dict) -> dict:
    """Skill count, plus each re-sent skill's name and the characters of
    its instructions. ``invoked_skills`` re-sends the full text of skills
    already used, e.g. after a conversation summary."""
    skills = attachment.get("skills")
    names = attachment.get("names")
    detail: dict = {}
    if isinstance(names, list):
        detail["count"] = len(names)
    if isinstance(skills, list):
        records = [
            {"name": skill["name"], "chars": _text_chars(skill.get("content"))}
            for skill in skills
            if isinstance(skill, dict) and isinstance(skill.get("name"), str)
        ]
        detail["count"] = len(records)
        if records:
            detail["skills"] = records
    return detail


#: A tool name kept as a key: the API's own tool-name alphabet, so never a
#: path or free text.
TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")

#: The server of a tool that isn't an MCP tool.
BUILT_IN_TOOLS = "built-in"


def tool_server(name: str) -> str:
    """The MCP server a tool comes from (``mcp__<server>__<tool>``), or
    :data:`BUILT_IN_TOOLS` for one of Claude Code's own."""
    parts = name.split("__")
    if len(parts) >= 3 and parts[0] == "mcp" and parts[1]:
        return parts[1][:64]
    return BUILT_IN_TOOLS


def _prompt_snapshot_detail(attachment: dict) -> dict:
    """Sizes of the system prompt and tool definitions a ``prompt_snapshot``
    records. The snapshot is not itself sent as a message, so these sit in
    ``Event.detail`` rather than ``Event.size_chars`` (which feeds the
    injected-attachment totals).

    Startup-parts addition (PARSER_VERSION 41): ``tool_chars`` is each
    built-in tool's definition size by name, and ``server_chars`` each MCP
    server's tools' total by server name. Only lengths and tool or server
    names are kept: never a description or a schema, and no other name.
    """
    detail: dict = {"system_chars": _text_chars(attachment.get("systemPrompt"))}
    tools = attachment.get("tools")
    if isinstance(tools, list) and tools:
        detail["tool_count"] = len(tools)
        total = 0
        by_tool: dict[str, int] = {}
        by_server: dict[str, int] = {}
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            chars = len(json.dumps(tool, separators=(",", ":"), ensure_ascii=False))
            total += chars
            name = tool.get("name")
            if not isinstance(name, str) or not TOOL_NAME_RE.match(name):
                continue
            server = tool_server(name)
            if server == BUILT_IN_TOOLS:
                by_tool[name] = by_tool.get(name, 0) + chars
            else:
                by_server[server] = by_server.get(server, 0) + chars
        detail["tools_chars"] = total
        if by_tool:
            detail["tool_chars"] = by_tool
        if by_server:
            detail["server_chars"] = by_server
    return detail


#: Matches an opening angle-bracket tag at the very start of a string,
#: e.g. ``<local-command-stdout>`` -> ``local-command-stdout``. Used only
#: to label META events with something more specific than "plain" — the
#: tag name itself is a structural marker, never message text, so it is
#: privacy-safe to store.
_LEADING_TAG_RE = re.compile(r"^<\s*/?\s*([A-Za-z][A-Za-z0-9_-]*)")


def _leading_tag_name(text: str) -> str | None:
    match = _LEADING_TAG_RE.match(text)
    return match.group(1) if match else None


# -- Image/document block sizing (parser-signals addition, SURV-7) ------
#
# Anthropic's documented image-token rule (platform.claude.com/docs, the
# vision page, curled and hand-verified against the doc's own example
# table -- 200x200 -> 64, 1000x1000 -> 1296, 1092x1092 -> 1521 tokens,
# all exact matches): tokens = ceil(width/28) * ceil(height/28) (28x28px
# patches), for an image at or under the "Standard" resolution tier's cap
# (long edge <= 1568px, <= 1568 tokens -- every model today except Claude
# 4.7+, which gets a "High-resolution" tier this parser does not
# implement). An image over that cap, or whose dimensions this parser
# can't read at all, is flagged unsized (``parser_notes
# ["unsized_blocks"]``) rather than guessed at, per this phase's brief.
# A document (PDF) block is always flagged unsized: pdf-support.md gives
# only an approximate per-page range (1,500-3,000 tokens/page for text,
# plus the same image formula per page), not a deterministic formula
# computable from the block alone.

#: Project-wide chars<->token approximation, duplicated locally per the
#: convention every other module using it documents (savers.py,
#: topology.py, context_budget.py, ...) -- used only to convert an
#: exactly-computed image token count back into the chars unit the rest
#: of this module's sizing (``_rendered_size_chars``, ``_human_text_metrics``)
#: already works in.
_CHARS_PER_TOKEN_APPROX = 4

_IMAGE_TOKEN_PATCH_PX = 28
_STANDARD_TIER_MAX_LONG_EDGE_PX = 1568
_STANDARD_TIER_MAX_TOKENS = 1568
#: How much of an image's own base64 ``source.data`` this module decodes
#: to look for its dimensions -- comfortably more than any PNG/GIF/WebP
#: header needs, and more than a JPEG's own pre-SOF metadata (EXIF/ICC
#: segments) commonly runs to; a JPEG whose SOF marker sits past this
#: prefix is flagged unsized rather than decoding the whole image just to
#: read a handful of header bytes. Kept a multiple of 4 so slicing the
#: base64 string here never breaks its own padding.
_IMAGE_HEADER_PROBE_B64_CHARS = 200_000


def image_token_estimate(width: int, height: int) -> int | None:
    """Anthropic's documented image-token rule for one image already
    known to be ``width`` x ``height`` px, or ``None`` when it falls
    outside the Standard resolution tier this parser implements (see
    module docstring section above) -- never guesses at the
    High-resolution tier's (Claude 4.7+) downscaling.
    """
    if width <= 0 or height <= 0:
        return None
    if max(width, height) > _STANDARD_TIER_MAX_LONG_EDGE_PX:
        return None
    tokens = -(-width // _IMAGE_TOKEN_PATCH_PX) * -(-height // _IMAGE_TOKEN_PATCH_PX)
    return tokens if tokens <= _STANDARD_TIER_MAX_TOKENS else None


def _png_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    return (width, height) if width and height else None


def _gif_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 10 or data[:6] not in (b"GIF87a", b"GIF89a"):
        return None
    width = int.from_bytes(data[6:8], "little")
    height = int.from_bytes(data[8:10], "little")
    return (width, height) if width and height else None


#: JPEG Start-Of-Frame markers (baseline/progressive/... -- every SOFn
#: except the DHT/DAC-adjacent 0xC4/0xC8/0xCC, which are not frame markers).
_JPEG_SOF_MARKERS = frozenset({0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF})


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    offset = 2
    length = len(data)
    while offset + 1 < length:
        if data[offset] != 0xFF:
            offset += 1
            continue
        marker = data[offset + 1]
        if marker == 0xFF:  # fill byte between markers
            offset += 1
            continue
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:  # no-length markers
            offset += 2
            continue
        if marker == 0xD9 or offset + 4 > length:  # EOI, or truncated
            return None
        if marker in _JPEG_SOF_MARKERS:
            if offset + 9 > length:
                return None
            height = int.from_bytes(data[offset + 5:offset + 7], "big")
            width = int.from_bytes(data[offset + 7:offset + 9], "big")
            return (width, height) if width and height else None
        seg_len = int.from_bytes(data[offset + 2:offset + 4], "big")
        if seg_len < 2:
            return None
        offset += 2 + seg_len
    return None


def _webp_dimensions(data: bytes) -> tuple[int, int] | None:
    """VP8 (lossy)/VP8L (lossless)/VP8X (extended) chunk dimensions --
    WebP has no single fixed-offset header field, unlike PNG/GIF."""
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    fourcc = data[12:16]
    payload = data[20:]
    if fourcc == b"VP8 ":
        # 3-byte frame tag, 3-byte start code, then 2+2 little-endian
        # dims (bottom 14 bits each; top 2 bits are an unused scale).
        if len(payload) < 10 or payload[3:6] != b"\x9d\x01\x2a":
            return None
        width = int.from_bytes(payload[6:8], "little") & 0x3FFF
        height = int.from_bytes(payload[8:10], "little") & 0x3FFF
        return (width, height) if width and height else None
    if fourcc == b"VP8L":
        if len(payload) < 5 or payload[0] != 0x2F:
            return None
        b1, b2, b3, b4 = payload[1], payload[2], payload[3], payload[4]
        width = 1 + (((b2 & 0x3F) << 8) | b1)
        height = 1 + (((b4 & 0x0F) << 10) | (b3 << 2) | ((b2 & 0xC0) >> 6))
        return (width, height) if width and height else None
    if fourcc == b"VP8X":
        if len(payload) < 10:
            return None
        width = 1 + int.from_bytes(payload[4:7], "little")
        height = 1 + int.from_bytes(payload[7:10], "little")
        return (width, height) if width and height else None
    return None


_IMAGE_DIMENSION_PARSERS = {
    "image/png": _png_dimensions,
    "image/jpeg": _jpeg_dimensions,
    "image/gif": _gif_dimensions,
    "image/webp": _webp_dimensions,
}


def content_block_size(block: dict) -> tuple[int | None, str | None]:
    """``(size_chars, block_type)`` for one ``image``/``document`` content
    block -- shared by ``_human_text_metrics`` (a top-level block in a
    human prompt) and ``parse._tool_result_length`` (a block nested in a
    tool_result's own content). ``block_type`` is ``"image"``/
    ``"document"`` when this function recognises the block's own ``type``
    at all, else ``None`` (not a block kind this function sizes -- the
    caller's own dispatch, e.g. "text", stands). ``size_chars`` is
    ``None`` when ``block_type`` is not ``None`` but the block could not
    be sized (see module docstring section above); the caller counts
    that in ``parser_notes["unsized_blocks"]``. The block's own image
    bytes are decoded transiently to read a handful of header bytes and
    never retained.
    """
    block_type = block.get("type")
    if block_type not in ("image", "document"):
        return None, None
    if block_type == "document":
        return None, "document"
    source = block.get("source")
    if not isinstance(source, dict) or source.get("type") != "base64":
        return None, "image"
    parser = _IMAGE_DIMENSION_PARSERS.get(source.get("media_type"))
    data_b64 = source.get("data")
    if parser is None or not isinstance(data_b64, str):
        return None, "image"
    probe = data_b64 if len(data_b64) <= _IMAGE_HEADER_PROBE_B64_CHARS else data_b64[:_IMAGE_HEADER_PROBE_B64_CHARS]
    try:
        raw = base64.b64decode(probe, validate=False)
    except (ValueError, TypeError):
        return None, "image"
    dims = parser(raw)
    if dims is None:
        return None, "image"
    tokens = image_token_estimate(*dims)
    if tokens is None:
        return None, "image"
    return tokens * _CHARS_PER_TOKEN_APPROX, "image"


#: A JSONL line's own top-level ``type`` field, sanitised to a safe,
#: closed-shape token for ``parser_notes["unknown_line_types"]`` (SURV-6):
#: every real ``type`` observed across the whole detection table is a
#: short lowercase word, optionally hyphenated/underscored (see
#: ``_IGNORABLE_TYPES``, ``_CACHE_SIGNAL_TYPES``, ... above) -- the same
#: shape ``_COMMAND_NAME_RE`` already trusts for a slash-command name.
#: ``type`` is attacker-controlled input off the wire, not a trusted
#: enum, so anything outside this shape (or too long) becomes "other"
#: rather than reaching a diagnostic counter's key verbatim.
_LINE_TYPE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,39}$")


def sanitize_line_type(line_type: object) -> str:
    return line_type if isinstance(line_type, str) and _LINE_TYPE_RE.match(line_type) else "other"


#: Capture-improvements addition (A4, see model.py's ``Turn.
#: human_prompt_chars``/``human_prompt_has_paste`` docstrings): a text
#: block at or beyond this length is treated as pasted, same as the
#: ``[Pasted text`` marker Claude Code's own composer inserts.
_PASTE_CHAR_THRESHOLD = 2000


#: Quality-signals addition: phrases that mark a message as correcting
#: Claude (``capture_catalogue.CORRECTION_PATTERN``, shared with the
#: capture hook's prompting hints). Matched in the first
#: :data:`_CORRECTION_SCAN_CHARS` characters only, and only the resulting
#: yes/no is kept -- never the text.
_CORRECTION_RE = re.compile(CORRECTION_PATTERN, re.IGNORECASE)
_CORRECTION_SCAN_CHARS = CORRECTION_SCAN_CHARS

#: Quality-markers addition: a brief that starts "[retry: <reason>]" says
#: the agent is being run again because its last run's work wasn't good
#: enough, and why; metrics capture adds "[spawn: <reason>]", why the work
#: was handed to an agent at all (``capture_tags.parse_brief_markers``).
#: Only the words are kept.

#: Metrics-capture addition (derived, no tokens): what a human message or
#: brief contains, kept as ``model.PROMPT_FLAGS`` words on ``Event.detail
#: ["flags"]``, never the text. Only the first :data:`_PROMPT_SCAN_CHARS`
#: characters are read, so a huge paste costs no more to scan than a
#: long message.
_PROMPT_SCAN_CHARS = 20_000
_URL_RE = re.compile(r"\bhttps?://\S+|\bwww\.\S+", re.IGNORECASE)
_PATH_RE = re.compile(
    r"(?:[A-Za-z]:[\\/]|\.{1,2}[\\/]|~[\\/]|\b[\w.-]+[\\/])[\w.\\/-]*[\w-]\.[A-Za-z0-9]{1,8}\b"
    r"|\b[\w-]+\.(?:py|pyi|ts|tsx|js|jsx|mjs|cjs|go|rs|java|kt|kts|cs|cpp|cc|hpp|rb|php|swift|scala|sql"
    r"|sh|ps1|psm1|md|json|jsonl|toml|ya?ml|ini|cfg|css|scss|html|vue|svelte|xml|gradle|tf|proto)\b"
    r"|(?:^|\s)(?:src|lib|app|tests?|docs|packages|scripts|config)/[\w.-]+"
)
_ERROR_TEXT_RE = re.compile(ERROR_TEXT_PATTERN)
_DONE_RE = re.compile(
    r"\b(?:done when|definition of done|acceptance criteria|success criteria"
    r"|expected (?:output|result|behaviou?r)"
    r"|should (?:now )?(?:pass|return|output|print|show|display)"
    r"|must (?:pass|return)|until (?:the |all )?tests? pass)\b",
    re.IGNORECASE,
)
_STEP_LINE_RE = re.compile(r"^\s*(?:\d{1,2}[.)]|step \d{1,2}[:.)]?)\s+\S", re.IGNORECASE | re.MULTILINE)
_SHORT_REPORT_RE = re.compile(
    r"\b(?:(?:under|fewer than|less than|at most|no more than|max(?:imum)?(?: of)?|within)\s+\d{1,4}\s+"
    r"(?:words|lines|sentences|bullets|bullet points|tokens|characters|chars)"
    r"|(?:brief|short|concise|one-line|terse)\s+(?:report|summary|answer|reply|response)"
    r"|(?:report|reply|respond|answer)\s+(?:back\s+)?(?:briefly|concisely|tersely))\b",
    re.IGNORECASE,
)


def path_count(text: str) -> int:
    """How many distinct file paths ``text`` names (URLs aside)."""
    text = _URL_RE.sub(" ", text[:_PROMPT_SCAN_CHARS])
    return len({match.group(0).strip() for match in _PATH_RE.finditer(text)})


def prompt_flags(texts: Sequence[str]) -> tuple[str, ...]:
    """``model.PROMPT_FLAGS`` words for what ``texts`` contain, in that
    order."""
    text = "\n".join(t for t in texts if t)[:_PROMPT_SCAN_CHARS]
    if not text:
        return ()
    flags: list[str] = []
    has_url = _URL_RE.search(text) is not None
    without_urls = _URL_RE.sub(" ", text) if has_url else text
    if _PATH_RE.search(without_urls):
        flags.append("path")
    if "```" in text:
        flags.append("code")
    if _ERROR_TEXT_RE.search(text):
        flags.append("error")
    if has_url:
        flags.append("url")
    if _DONE_RE.search(text):
        flags.append("done")
    if len(_STEP_LINE_RE.findall(text, 0, 8_000)) >= 2:
        flags.append("steps")
    if _SHORT_REPORT_RE.search(text):
        flags.append("short")
    return tuple(flags)


def _looks_like_correction(texts: list[str]) -> bool:
    return any(_CORRECTION_RE.search(text[:_CORRECTION_SCAN_CHARS]) for text in texts if text)


def _human_text_metrics(d: dict, str_content: str | None) -> tuple[int, bool, dict[str, int]]:
    """Chars, paste-flag and unsized-block counts for a HUMAN_TEXT (or
    TASK_NOTIFICATION) line's own content (A4, extended by SURV-7): sums
    the plain string content, every ``text`` block's length, plus every
    sizeable top-level ``image``/``document`` block's own token-rule
    estimate (``content_block_size``) for a list-content line -- these
    used to silently count as 0 chars. Flags a paste when any one text
    block exceeds ``_PASTE_CHAR_THRESHOLD`` chars or contains
    ``PASTE_MARKER``. Never retains any block's own content.
    ``unsized_counts`` is block type -> count for a block this function
    recognises (image/document) but could not size -- the caller folds
    it into ``parser_notes["unsized_blocks"]``.
    """
    return _content_metrics(str_content, _user_content_blocks(d))


def _content_metrics(str_content: str | None, blocks: Sequence | None) -> tuple[int, bool, dict[str, int]]:
    """:func:`_human_text_metrics` for content already split into a plain
    string or a list of blocks (a queued message's ``prompt`` is either)."""
    texts: list[str] = []
    block_chars = 0
    unsized_counts: dict[str, int] = {}
    if str_content is not None:
        texts.append(str_content)
    elif blocks is not None:
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    texts.append(text)
                continue
            chars, block_type = content_block_size(block)
            if block_type is None:
                continue
            if chars is not None:
                block_chars += chars
            else:
                unsized_counts[block_type] = unsized_counts.get(block_type, 0) + 1
    total_chars = sum(len(text) for text in texts) + block_chars
    has_paste = any(len(text) > _PASTE_CHAR_THRESHOLD or PASTE_MARKER in text for text in texts)
    return total_chars, has_paste, unsized_counts


def _user_content_blocks(d: dict) -> list | None:
    """A ``user`` line's content blocks, or ``None`` for plain-string
    content."""
    message = d.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return content if isinstance(content, list) else None


def _has_image_block(blocks: Sequence | None) -> bool:
    """Whether a list of content blocks holds an image. A desktop image
    message has no "[Image #" placeholder in its text, so the block is
    the only sign."""
    return any(isinstance(block, dict) and block.get("type") == "image" for block in blocks or ())


def _message_flags(texts: Sequence[str], *, has_image: bool = False, skill_command: bool = False) -> dict:
    """The ``Event.detail`` flags a message you typed, or typed while
    Claude was working, carries: ``correction``, ``adjust``, ``remind``,
    ``go``, ``status``, ``ack``, ``question``, ``change``, ``vague``, ``has_image``,
    ``steps`` (two or more changes, ``prompt_shape.plan_steps``: none for a
    message that mentions a plan, opens by asking to review or is a plan
    already) and ``flags`` (``model.PROMPT_FLAGS``). Each is there only
    when true. A skill you
    ran with a slash gets ``correction``, ``has_image`` and ``flags``
    only: the rest read what you wrote, and a skill's text is not that.
    Flags only -- never the text."""
    detail: dict = {}
    if _looks_like_correction(texts):
        detail["correction"] = True
    flags = prompt_flags(texts)
    if flags:
        detail["flags"] = flags
    if has_image:
        detail["has_image"] = True
    if skill_command:
        return detail
    # Prompting-habits addition (see model.py's module docstring): counts
    # and flags only.
    text = "\n".join(t for t in texts if t)
    steps = prompt_shape.plan_steps(text)
    if steps >= 2:
        detail["steps"] = steps
    if not has_image and prompt_shape.is_vague_fix(text, int(REPORT_THRESHOLDS["vague_fix_chars"])):
        detail["vague"] = True
    if text.strip():
        for key, matches in (
            ("ack", prompt_shape.is_ack),
            ("go", prompt_shape.is_go),
            ("status", prompt_shape.is_status),
            ("question", prompt_shape.is_question),
            ("change", prompt_shape.is_change_request),
            ("adjust", prompt_shape.is_adjust),
            ("remind", prompt_shape.is_remind),
        ):
            if matches(text):
                detail[key] = True
    return detail


def _human_text_detail(d: dict, str_content: str | None) -> tuple[int, dict]:
    """Size and the detail flags for a HUMAN_TEXT line: ``has_paste``,
    ``correction``, ``unsized_blocks`` (SURV-7, only when non-empty) and
    the rest of :func:`_message_flags`. Flags only -- never the text."""
    human_chars, has_paste, unsized_counts = _human_text_metrics(d, str_content)
    texts = human_texts(d)
    detail: dict = {"has_paste": has_paste, "correction": False}
    if unsized_counts:
        detail["unsized_blocks"] = unsized_counts
    retry = spawn = None
    for text in texts:
        if text and (retry is None or spawn is None):
            found_retry, found_spawn = parse_brief_markers(text)
            retry, spawn = retry or found_retry, spawn or found_spawn
    if retry is not None:
        detail["retry"] = retry
    if spawn is not None:
        detail["spawn"] = spawn
    skill_command = str_content is not None and str_content.startswith(_SKILL_COMMAND_PREFIX)
    detail.update(
        _message_flags(texts, has_image=_has_image_block(_user_content_blocks(d)), skill_command=skill_command)
    )
    if skill_command:
        command = _COMMAND_NAME_RE.search(str_content)
        if command:
            detail["command"] = command.group(1)
    if d.get("permissionMode") == "plan":
        detail["plan_mode"] = True
    return human_chars, detail


def human_texts(d: dict) -> list[str]:
    """The text blocks of a ``user`` line you typed, for reading its shape
    while the line is parsed. Never stored."""
    str_content = _user_str_content(d)
    if str_content is not None:
        return [str_content]
    message = d.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return [
        block.get("text")
        for block in (content if isinstance(content, list) else ())
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
    ]


def _prompt_texts(prompt: object) -> list[str]:
    """The texts of a ``queued_command`` prompt: a string, or the text
    blocks of a list of text and image blocks."""
    if isinstance(prompt, str):
        return [prompt]
    return [
        block.get("text")
        for block in (prompt if isinstance(prompt, list) else ())
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
    ]


def queued_texts(d: dict) -> list[str]:
    """The text of a ``queued_command`` attachment's message, for reading
    one you typed while Claude was working. Never stored."""
    attachment = d.get("attachment")
    return _prompt_texts(attachment.get("prompt") if isinstance(attachment, dict) else None)


def _queued_prompt_detail(attachment: dict) -> dict | None:
    """Parser-signals addition (PARSER_VERSION 37): the detail of a
    ``queued_command`` that carries a message, or ``None`` for one that
    doesn't (a task notification, a coordinator's note). Only
    ``commandMode`` "prompt" is a message: a task notification has no
    origin at all. ``{"origin": "peer"}`` for another session's message,
    else, for a human or missing origin, ``{"origin": "human", "chars":
    n}`` with :func:`_message_flags`, ``has_paste`` and ``unsized_blocks``,
    each only when true. Never the text. A line you didn't type
    (:data:`NOT_TYPED_PREFIXES`) is no message."""
    if attachment.get("commandMode") != "prompt":
        return None
    origin = attachment.get("origin")
    kind = origin.get("kind") if isinstance(origin, dict) else None
    if kind == "peer":
        return {"origin": "peer"}
    prompt = attachment.get("prompt")
    if kind not in (None, "human") or attachment.get("isMeta") or not isinstance(prompt, (str, list)):
        return None
    texts = _prompt_texts(prompt)
    first = next((text for text in texts if text), "")
    if first.lstrip().startswith(NOT_TYPED_PREFIXES) and not first.startswith(_SKILL_COMMAND_PREFIX):
        return None
    blocks = prompt if isinstance(prompt, list) else None
    chars, has_paste, unsized_counts = _content_metrics(prompt if blocks is None else None, blocks)
    detail: dict = {"origin": "human", "chars": chars}
    if has_paste:
        detail["has_paste"] = True
    if unsized_counts:
        detail["unsized_blocks"] = unsized_counts
    detail.update(_message_flags(texts, has_image=_has_image_block(blocks)))
    return detail


#: Usage-limits addition (see module docstring): the six known synthetic
#: assistant texts, matched by ordered startswith/substring checks
#: verified against the real corpus (the two usage-limit prefixes are the
#: catalogue's, which the capture hook reads too). Never stored -- only
#: the resulting enum-like label survives onto ``Turn.synthetic_kind``.
_OVERLOADED_PREFIX = "API Error: 529"
_AUTOCOMPACT_THRASH_PREFIX = "Autocompact is thrashing"


def classify_synthetic_text(text: str | None) -> str:
    """Classify a synthetic assistant line's own text into one of six
    enum values: ``session_limit``, ``weekly_limit``, ``overloaded``,
    ``unsupported_model``, ``autocompact_thrash``, or (anything else,
    including no text at all) ``other_api_error``.
    """
    if not isinstance(text, str):
        return "other_api_error"
    if text.startswith(SESSION_LIMIT_PREFIX):
        return "session_limit"
    if text.startswith(WEEKLY_LIMIT_PREFIX):
        return "weekly_limit"
    if text.startswith(_OVERLOADED_PREFIX):
        return "overloaded"
    if text.startswith("API Error:") and "does not support" in text:
        return "unsupported_model"
    if text.startswith(_AUTOCOMPACT_THRASH_PREFIX):
        return "autocompact_thrash"
    return "other_api_error"


#: Usage-limits addition (see module docstring): the "resets [Mon D, ]H[:MM]am|pm
#: (IANA tz)" clause trailing a session/weekly-limit synthetic text. The
#: weekly form names the day ("resets Oct 3, 9am (Europe/London)"); the
#: five-hour form doesn't.
_LIMIT_RESET_RE = re.compile(
    r"resets\s+(?:(?P<month>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(?P<day>\d{1,2}),?\s*)?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<meridiem>am|pm)\s*\((?P<tz>[^)]+)\)",
    re.IGNORECASE,
)
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
#: Single-slash IANA zone names only (e.g. ``Europe/London``) -- a
#: multi-part name like ``America/Argentina/Buenos_Aires`` is deliberately
#: excluded rather than guessed at (see model.py's module docstring).
_IANA_TZ_RE = re.compile(r"^[A-Za-z_]+/[A-Za-z_]+$")


def parse_limit_reset_clause(text: str | None) -> tuple[int | None, str | None]:
    """Parse a synthetic limit-hit line's trailing "resets ..." clause
    into ``(reset_minutes_of_day, reset_tz)``. ``reset_tz`` is ``None``
    unless the parenthesised zone name matches the single-slash IANA
    form. Returns ``(None, None)`` when ``text`` doesn't carry a
    matching clause at all.
    """
    if not isinstance(text, str):
        return None, None
    match = _LIMIT_RESET_RE.search(text)
    if not match:
        return None, None
    hour_raw, minute_raw, meridiem, tz_raw = match.group("hour", "minute", "meridiem", "tz")
    try:
        hour = int(hour_raw)
        minute = int(minute_raw) if minute_raw else 0
    except ValueError:
        return None, None
    if not (1 <= hour <= 12) or not (0 <= minute <= 59):
        return None, None
    hour24 = hour % 12
    if meridiem.lower() == "pm":
        hour24 += 12
    minutes_of_day = hour24 * 60 + minute
    tz = tz_raw if _IANA_TZ_RE.match(tz_raw) else None
    return minutes_of_day, tz


def parse_limit_reset_date(text: str | None) -> tuple[int, int] | None:
    """The ``(month, day)`` a synthetic limit-hit line's "resets ..." clause
    names ("resets Oct 3, 9am (Europe/London)" gives ``(10, 3)``), or
    ``None`` for the five-hour form, which names a time of day alone, and
    for a text with no matching clause. The day is checked against 1-31
    only: whether it exists that month is for the caller that dates it.
    """
    if not isinstance(text, str):
        return None
    match = _LIMIT_RESET_RE.search(text)
    if not match or not match.group("month"):
        return None
    day = int(match.group("day"))
    if not 1 <= day <= 31:
        return None
    return _MONTHS.index(match.group("month").lower()) + 1, day


#: Usage-limits addition (see module docstring): a subagent killed
#: mid-task by the harness, and the structured "error type X" clause
#: used to tell a usage-limit kill apart from any other reason.
_TERMINATED_EARLY_MARKER = "terminated early due to"
_ERROR_TYPE_RE = re.compile(r"error type ([a-zA-Z_]+)")


#: Quality-signals addition: a task notification's own ``<task-id>`` and
#: ``<status>`` tags (``completed``/``failed``/``stopped`` observed). The
#: id is the agent's id for a background agent (its transcript is
#: ``agent-<id>.jsonl``) or a background shell task's id.
_TASK_ID_RE = re.compile(r"<task-id>([A-Za-z0-9_-]{1,64})</task-id>")
_TASK_STATUS_RE = re.compile(r"<status>([a-z_]{1,24})</status>")


def _task_notification_detail(text: str | None) -> dict:
    if not text or "<task-id>" not in text:
        return {}
    detail = {}
    task_id = _TASK_ID_RE.search(text)
    status = _TASK_STATUS_RE.search(text)
    if task_id:
        detail["task_id"] = task_id.group(1)
    if status:
        detail["status"] = status.group(1)
    return detail


def _agent_results_detail(d: dict) -> dict:
    """Quality-signals addition: a synchronous agent's result
    (``toolUseResult`` with an ``agentId`` and a final ``status``) as
    ``{"agents": [[agent_id, status]]}``; ``{}`` for any other tool
    result, including a background launch (``async_launched``)."""
    result = d.get("toolUseResult")
    if not isinstance(result, dict):
        return {}
    agent_id, status = result.get("agentId"), result.get("status")
    if not isinstance(agent_id, str) or not isinstance(status, str) or result.get("isAsync"):
        return {}
    if status == "async_launched" or not _TASK_ID_RE.fullmatch(f"<task-id>{agent_id}</task-id>"):
        return {}
    return {"agents": [[agent_id, status[:24]]]}


def _agent_terminated_subkind(text: str) -> str:
    match = _ERROR_TYPE_RE.search(text)
    if match and match.group(1).lower() == "rate_limit":
        return "rate_limit"
    return "other"


def _first_user_text(d: dict, str_content: str | None) -> str | None:
    """The first text a ``type=user`` line carries, for structural
    prefix/substring matching only -- never stored on any ``Event`` (see
    SECURITY.md). Handles both a plain-string ``message.content`` and a
    list of content blocks (mixed shapes observed in the real corpus for
    task-notification and resume-prompt lines, unlike the always
    list-shaped ``isApiErrorMessage`` lines).
    """
    if str_content is not None:
        return str_content
    message = d.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    return text
    return None


def _delta_detail(attachment_type: str, attachment: dict) -> dict:
    keys = _DELTA_COUNT_KEYS.get(attachment_type)
    if keys is None:
        return {}
    added_key, removed_key = keys
    added = attachment.get(added_key)
    removed = attachment.get(removed_key)
    detail = {
        "added": len(added) if isinstance(added, list) else 0,
        "removed": len(removed) if isinstance(removed, list) else 0,
    }
    if attachment_type == "deferred_tools_delta" and isinstance(added, list):
        # How many of the added deferred tools come from MCP servers
        # (``mcp__<server>__<tool>`` names) -- a count, never the names.
        detail["mcp_added"] = sum(1 for name in added if isinstance(name, str) and name.startswith("mcp__"))
    return detail


# -- task_status / structured_output (parser-signals addition, SURV-4) --

#: ``thinking_drop.newlyDropped.reason`` -- only "prefix_mismatch" is
#: observed in the real corpus; any other/future value is "other" rather
#: than stored verbatim.
_THINKING_DROP_REASONS = frozenset({"prefix_mismatch"})


def _thinking_drop_detail(attachment: dict) -> dict:
    dropped = attachment.get("newlyDropped")
    if not isinstance(dropped, dict):
        return {}
    detail: dict = {}
    reason = dropped.get("reason")
    if isinstance(reason, str):
        detail["reason"] = reason if reason in _THINKING_DROP_REASONS else "other"
    for key in ("blockCount", "turnCount"):
        value = dropped.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            detail[key] = value
    return detail


#: ``task_status.status`` -- "running"/"completed" observed; "failed"/
#: "stopped"/"cancelled" kept as forward-compatible closed words (the same
#: vocabulary ``_TASK_STATUS_RE``'s task-notification status uses above),
#: anything else "other".
_TASK_STATUS_WORDS = frozenset({"running", "completed", "failed", "stopped", "cancelled"})
#: ``task_status.taskType`` -- "local_bash"/"local_agent" observed.
_TASK_TYPE_WORDS = frozenset({"local_bash", "local_agent"})


def _task_status_detail(attachment: dict) -> dict:
    """Closed status words only -- never ``description``, ``deltaSummary``,
    ``outputFilePath`` (a real filesystem path, confirmed in the real
    corpus) or ``shell``."""
    detail: dict = {}
    status = attachment.get("status")
    if isinstance(status, str):
        detail["status"] = status if status in _TASK_STATUS_WORDS else "other"
    task_type = attachment.get("taskType")
    if isinstance(task_type, str):
        detail["task_type"] = task_type if task_type in _TASK_TYPE_WORDS else "other"
    return detail


def _structured_output_size(attachment: dict) -> int | None:
    """Size only -- the JSON-encoded length of ``data``, an arbitrary
    tool-defined structured payload (schema varies per skill/tool) never
    otherwise inspected or stored."""
    if "data" not in attachment:
        return None
    try:
        return len(json.dumps(attachment["data"], separators=(",", ":")))
    except (TypeError, ValueError):
        return None


def _is_not_typed(d: dict, str_content: str | None) -> bool:
    """Whether a ``user`` line that would otherwise be your message is one
    you didn't type. ``turnOrigin`` only rules a line out (``human`` is
    on scheduled tasks and resume pings too); a skill you ran with a slash
    is your message, whatever its ``<command-`` prefix."""
    if d.get("turnOrigin") in NOT_TYPED_TURN_ORIGINS:
        return True
    text = _first_user_text(d, str_content)
    if text is None or text.startswith(_SKILL_COMMAND_PREFIX):
        return False
    return text.lstrip().startswith(NOT_TYPED_PREFIXES)


def _not_typed_subkind(d: dict, str_content: str | None) -> str:
    """The ``META`` subkind of a line you didn't type: ``resume`` for a
    note that carries on your last message's work
    (``capture_catalogue.RESUME_PREFIXES``, unless a ``turnOrigin`` rules
    it out), ``not_typed`` for every other line, which Claude answers by
    itself and which ends the reply to your last message (the hook's
    ``_untyped_start`` reads the same lines)."""
    if d.get("turnOrigin") in NOT_TYPED_TURN_ORIGINS:
        return "not_typed"
    text = _first_user_text(d, str_content)
    return "resume" if text is not None and text.lstrip().startswith(RESUME_PREFIXES) else "not_typed"


def classify_line(d: dict) -> Event | None:
    """Classify one already-parsed JSONL line per plan Appendix A2.

    Returns ``None`` for assistant lines (handled separately by
    ``parse.py``, not as an Event) and for lines whose top-level ``type``
    is in the plan's "ignored outright but counted" list — the caller is
    expected to count those by ``type`` itself (``Diagnostics.
    ignored_line_types``). Every other line yields an ``Event``, ending
    with ``EventKind.UNKNOWN`` for anything the table's rows never
    matched (also meant to be counted by ``type`` — see A2's UNKNOWN row).
    """
    line_type = d.get("type")
    if line_type == "assistant":
        return None
    if _is_ignorable_type(line_type):
        return None
    if line_type == "system" and d.get("subtype") in _IGNORABLE_SYSTEM_SUBTYPES:
        return None

    ts = d.get("timestamp")
    str_content = _user_str_content(d)
    origin = d.get("origin")
    origin = origin if isinstance(origin, dict) else None
    origin_kind = origin.get("kind") if origin else None

    # 1. COMPACT_BOUNDARY
    if line_type == "system" and d.get("subtype") == "compact_boundary":
        meta = d.get("compactMetadata")
        meta = meta if isinstance(meta, dict) else {}
        return Event(
            kind=EventKind.COMPACT_BOUNDARY,
            subkind="compact_boundary",
            ts=ts,
            pre_tokens=meta.get("preTokens"),
            post_tokens=meta.get("postTokens"),
            dropped_tokens=meta.get("cumulativeDroppedTokens"),
            duration_ms=meta.get("durationMs"),
            trigger=meta.get("trigger"),
        )

    # 2. COMPACT_SUMMARY
    if line_type == "user" and (
        d.get("isCompactSummary")
        or (str_content is not None and str_content.startswith("This session is being continued"))
    ):
        # Sized for parse.py's compaction-call estimate: the summary's
        # own length, never its text.
        return Event(
            kind=EventKind.COMPACT_SUMMARY, subkind=None, ts=ts, size_chars=_human_text_metrics(d, str_content)[0]
        )

    # 3. API_ERROR
    if line_type == "system" and d.get("subtype") == "api_error":
        error = d.get("error")
        detail: dict = {}
        if isinstance(error, dict) and error.get("status") is not None:
            detail["status"] = error.get("status")
        if d.get("retryAttempt") is not None:
            detail["retryAttempt"] = d.get("retryAttempt")
        # Usage-limits addition (see module docstring): retryInMs/source
        # alongside the existing status/retryAttempt.
        if d.get("retryInMs") is not None:
            detail["retryInMs"] = d.get("retryInMs")
        if d.get("source") is not None:
            detail["source"] = d.get("source")
        return Event(kind=EventKind.API_ERROR, subkind="api_error", ts=ts, detail=detail)

    # 4. MODEL_FALLBACK
    if line_type == "system" and d.get("subtype") == "model_refusal_fallback":
        detail = {}
        if d.get("originalModel") is not None:
            detail["originalModel"] = d.get("originalModel")
        if d.get("fallbackModel") is not None:
            detail["fallbackModel"] = d.get("fallbackModel")
        return Event(
            kind=EventKind.MODEL_FALLBACK, subkind="model_refusal_fallback", ts=ts, detail=detail
        )

    # 5. LOCAL_COMMAND
    if line_type == "system" and d.get("subtype") == "local_command":
        return Event(kind=EventKind.LOCAL_COMMAND, subkind="local_command", ts=ts)

    attachment_type: str | None = None
    attachment: dict = {}
    size_chars: int | None = None
    if line_type == "attachment":
        raw_attachment = d.get("attachment")
        if isinstance(raw_attachment, dict):
            attachment = raw_attachment
            attachment_type = attachment.get("type")
        size_chars = _rendered_size_chars(d, attachment)

    # 6. HOOK_OUTPUT
    if line_type == "system" and d.get("subtype") == "stop_hook_summary":
        return Event(kind=EventKind.HOOK_OUTPUT, subkind="stop_hook_summary", ts=ts)
    if attachment_type in _HOOK_ATTACHMENT_TYPES:
        if attachment_type == "hook_additional_context":
            note = _capture_note(d, attachment)
            if note is not None:
                note_kind, note_chars, detail = note
                return Event(
                    kind=EventKind.HOOK_OUTPUT, subkind=note_kind, ts=ts, size_chars=note_chars, detail=detail
                )
        # SURV-HE/CAP-9: which hook event this ran under (closed bucket,
        # never the matcher/tool-name suffix -- see _hook_name_bucket),
        # its real durationMs, and whether it was ClaudeGlass's own hook
        # -- see _hook_output_detail -- so hook_health.count_hook_errors
        # and hook_health.measure_hook_overhead can both work from this one
        # parse, without a second read of the transcript.
        return Event(
            kind=EventKind.HOOK_OUTPUT,
            subkind=attachment_type,
            ts=ts,
            size_chars=size_chars,
            detail=_hook_output_detail(attachment),
        )

    # 7. CACHE_SIGNAL
    if attachment_type in _CACHE_SIGNAL_TYPES:
        detail = {}
        if attachment_type == "model":
            identity = attachment.get("identity")
            if isinstance(identity, dict) and identity.get("modelId") is not None:
                detail["modelId"] = identity.get("modelId")
        elif attachment_type == "thinking_stripped":
            if attachment.get("scope") is not None:
                detail["scope"] = attachment.get("scope")
        elif attachment_type == "thinking_drop":
            detail = _thinking_drop_detail(attachment)
        elif attachment_type in _DELTA_COUNT_KEYS:
            detail = _delta_detail(attachment_type, attachment)
        return Event(
            kind=EventKind.CACHE_SIGNAL, subkind=attachment_type, ts=ts, size_chars=size_chars, detail=detail
        )

    # 8. REMINDER
    if attachment_type in _REMINDER_TYPES:
        return Event(kind=EventKind.REMINDER, subkind=attachment_type, ts=ts, size_chars=size_chars)

    # 9. CONTEXT_INJECT
    if attachment_type in _CONTEXT_INJECT_TYPES:
        detail = {}
        if attachment_type == "invoked_skills":
            detail = _invoked_skills_detail(attachment)
        elif attachment_type == "skill_listing":
            detail = _skill_listing_detail(attachment)
        elif attachment_type == "instructions":
            detail = _instructions_detail(attachment)
        elif attachment_type == "nested_memory":
            detail = _nested_memory_detail(attachment)
        elif attachment_type == "prompt_snapshot":
            detail = _prompt_snapshot_detail(attachment)
            size_chars = None
        return Event(
            kind=EventKind.CONTEXT_INJECT, subkind=attachment_type, ts=ts, size_chars=size_chars, detail=detail
        )

    # 10. QUEUE_OPERATION
    # A task notification that arrives while Claude is mid-reply is
    # queued (and then attached) rather than sent as a user line, so its
    # task id and status are read here too (quality signals).
    if line_type == "queue-operation":
        content = d.get("content")
        return Event(
            kind=EventKind.QUEUE_OPERATION,
            subkind=d.get("operation"),
            ts=ts,
            detail=_task_notification_detail(content if isinstance(content, str) else None),
        )
    if attachment_type == "queued_command":
        prompt = attachment.get("prompt") if isinstance(attachment, dict) else None
        # Parser-signals addition (PARSER_VERSION 37): a message you typed
        # while Claude was working. Still a QUEUE_OPERATION, never
        # HUMAN_TEXT, so it never opens a cycle; its time is when you
        # typed it, not when Claude Code attached it.
        queued = _queued_prompt_detail(attachment)
        typed_at = attachment.get("timestamp")
        return Event(
            kind=EventKind.QUEUE_OPERATION,
            subkind=attachment_type,
            ts=typed_at if queued is not None and isinstance(typed_at, str) and typed_at else ts,
            size_chars=size_chars,
            detail=queued if queued is not None else _task_notification_detail(prompt if isinstance(prompt, str) else None),
        )

    # 10.5. TASK_STATUS / STRUCTURED_OUTPUT (parser-signals addition,
    # SURV-4: their own kinds instead of the generic ATTACHMENT catch-all
    # below -- closed status words / a size only, see the two helpers'
    # own docstrings for what is deliberately never captured.)
    if attachment_type == "task_status":
        return Event(
            kind=EventKind.TASK_STATUS, subkind=attachment_type, ts=ts, detail=_task_status_detail(attachment)
        )
    if attachment_type == "structured_output":
        return Event(
            kind=EventKind.STRUCTURED_OUTPUT,
            subkind=attachment_type,
            ts=ts,
            size_chars=_structured_output_size(attachment),
        )

    # 11. ATTACHMENT (catch-all for any attachment type not listed above)
    if line_type == "attachment":
        return Event(kind=EventKind.ATTACHMENT, subkind=attachment_type, ts=ts, size_chars=size_chars)

    # 12. TOOL_DENIAL
    tool_denial_kind = d.get("toolDenialKind")
    if line_type == "user" and tool_denial_kind:
        if _is_saver_redirect(d):
            tool_denial_kind = known_savers.REDIRECT_DENIAL_KIND
        return Event(kind=EventKind.TOOL_DENIAL, subkind=tool_denial_kind, ts=ts)

    # 13. TOOL_RESULT
    if line_type == "user" and _user_has_tool_result(d):
        return Event(kind=EventKind.TOOL_RESULT, subkind=None, ts=ts, detail=_agent_results_detail(d))

    is_task_notification_line = line_type == "user" and (
        origin_kind == "task-notification" or (str_content is not None and str_content.startswith("<task-notification"))
    )

    # 13.5. AGENT_TERMINATED (usage-limits addition, see module docstring:
    # checked before TASK_NOTIFICATION so a terminated-early notification
    # is classified as this more specific kind instead of the generic one).
    if is_task_notification_line:
        text = _first_user_text(d, str_content)
        if text is not None and _TERMINATED_EARLY_MARKER in text:
            return Event(
                kind=EventKind.AGENT_TERMINATED,
                subkind=_agent_terminated_subkind(text),
                ts=ts,
                detail=_task_notification_detail(text),
            )

    # 14. TASK_NOTIFICATION (sized: a background agent's notification
    # carries the report it hands back)
    if is_task_notification_line:
        return Event(
            kind=EventKind.TASK_NOTIFICATION,
            subkind=None,
            ts=ts,
            size_chars=_human_text_metrics(d, str_content)[0],
            detail=_task_notification_detail(_first_user_text(d, str_content)),
        )

    # 15. PEER_MESSAGE
    if line_type == "user" and origin_kind == "peer":
        return Event(kind=EventKind.PEER_MESSAGE, subkind=None, ts=ts)

    # 16. META (checked after the four user-line kinds above so a meta
    # line that also happens to carry a tool result/denial/task-notification/
    # peer origin is classified as that more specific kind instead).
    if line_type == "user" and d.get("isMeta"):
        if origin_kind is not None:
            meta_subkind = origin_kind
        else:
            tag = _leading_tag_name(str_content) if str_content is not None else None
            meta_subkind = tag if tag is not None else "plain"
        return Event(kind=EventKind.META, subkind=meta_subkind, ts=ts)

    # 17. SLASH_COMMAND (metrics-capture addition: the command's name, so
    # a skill you ran yourself can be told from one Claude invoked)
    if str_content is not None and str_content.startswith(_SLASH_COMMAND_PREFIXES):
        command = _COMMAND_NAME_RE.match(str_content)
        detail = {"command": command.group(1)} if command else {}
        return Event(kind=EventKind.SLASH_COMMAND, subkind=None, ts=ts, detail=detail)

    # 18. SCHEDULED_TASK
    if str_content is not None and str_content.startswith(_SCHEDULED_TASK_PREFIXES):
        return Event(kind=EventKind.SCHEDULED_TASK, subkind=None, ts=ts)
    if origin_kind in _SCHEDULED_TASK_ORIGIN_KINDS:
        return Event(kind=EventKind.SCHEDULED_TASK, subkind=None, ts=ts)

    # 19. INTERRUPT
    if str_content is not None and str_content.startswith("[Request interrupted"):
        return Event(kind=EventKind.INTERRUPT, subkind=_interrupt_subkind(str_content), ts=ts)
    if line_type == "user":
        interrupt_text = _interrupt_block_text(d)
        if interrupt_text is not None:
            return Event(kind=EventKind.INTERRUPT, subkind=_interrupt_subkind(interrupt_text), ts=ts)

    # 19.5. LIMIT_RESUME (usage-limits addition, see module docstring:
    # checked before HUMAN_TEXT so the desktop app's automatic resume
    # ping -- a human-origin, promptSource:"sdk" line -- is classified as
    # this more specific kind instead of the generic one. Ignores the
    # queue-operation/last-prompt variants of similar text: those are a
    # different top-level ``type``, already filtered out at the top of
    # this function.)
    if line_type == "user" and d.get("promptSource") == "sdk" and origin_kind == "human":
        text = _first_user_text(d, str_content)
        if text is not None and text.startswith(LIMIT_RESUME_PREFIX):
            return Event(kind=EventKind.LIMIT_RESUME, subkind=None, ts=ts)

    # 19.9. NOT_TYPED (parser-signals addition, PARSER_VERSION 37): a line
    # that looks like your message but isn't -- one list shared with the
    # hook (``capture_catalogue.NOT_TYPED_PREFIXES``), and a
    # ``turnOrigin`` that rules it out. Checked here so everything
    # above that classified such a line more specifically still wins. A
    # resume note is subkind ``resume``, the rest ``not_typed``.
    if line_type == "user" and _is_not_typed(d, str_content):
        return Event(kind=EventKind.META, subkind=_not_typed_subkind(d, str_content), ts=ts)

    # 20. HUMAN_TEXT
    if line_type == "user" and (
        origin_kind == "human" or d.get("promptSource") is not None or d.get("permissionMode") is not None
    ):
        human_chars, detail = _human_text_detail(d, str_content)
        return Event(kind=EventKind.HUMAN_TEXT, subkind=None, ts=ts, size_chars=human_chars, detail=detail)
    if line_type == "user" and (str_content is not None or _user_has_text_or_image_list(d)):
        human_chars, detail = _human_text_detail(d, str_content)
        return Event(kind=EventKind.HUMAN_TEXT, subkind=None, ts=ts, size_chars=human_chars, detail=detail)

    # 21. UNKNOWN
    return Event(kind=EventKind.UNKNOWN, subkind=None, ts=ts)


# -- Precedence -----------------------------------------------------------

#: plan A2's precedence order for ``preceding_primary``, highest first.
#: CACHE_SIGNAL appears twice because the plan splits it into two bands;
#: ``_PRECEDENCE_SUBKIND_FILTER`` (same length, positionally aligned)
#: restricts each occurrence to the subkinds that belong in that band.
PRECEDENCE: tuple[EventKind, ...] = (
    EventKind.COMPACT_BOUNDARY,
    EventKind.COMPACT_SUMMARY,
    EventKind.MODEL_FALLBACK,
    EventKind.CACHE_SIGNAL,  # high band: model, thinking_stripped, ultra_effort_*
    # Usage-limits addition (see module docstring): ranked above INTERRUPT
    # per the v3-limits brief -- a usage-cap pause outranks a plain
    # interrupt as the explanation for a gap.
    EventKind.LIMIT_HIT,
    EventKind.LIMIT_RESUME,
    EventKind.INTERRUPT,
    EventKind.HUMAN_TEXT,
    # Parser-signals addition (PARSER_VERSION 37): your answer to a plan
    # is a message of yours, ranked just below one you typed.
    EventKind.PLAN_FEEDBACK,
    EventKind.PEER_MESSAGE,
    # Usage-limits addition: not ranked by the plan (it predates this
    # kind); placed just above TASK_NOTIFICATION -- a documented
    # judgement call, see module docstring.
    EventKind.AGENT_TERMINATED,
    EventKind.TASK_NOTIFICATION,
    EventKind.SCHEDULED_TASK,
    EventKind.SLASH_COMMAND,
    EventKind.LOCAL_COMMAND,  # not ranked by the plan; placed here (command-adjacent)
    EventKind.CACHE_SIGNAL,  # other band: every other CACHE_SIGNAL subkind
    EventKind.HOOK_OUTPUT,
    EventKind.QUEUE_OPERATION,
    # Parser-signals addition: not ranked by the plan; placed here
    # (harness-plumbing kinds, same band as QUEUE_OPERATION/HOOK_OUTPUT).
    EventKind.TASK_STATUS,
    EventKind.STRUCTURED_OUTPUT,
    EventKind.CONTEXT_INJECT,
    EventKind.REMINDER,
    EventKind.TOOL_DENIAL,
    EventKind.ATTACHMENT,
    EventKind.TOOL_RESULT,
    EventKind.API_ERROR,
    EventKind.META,  # not ranked by the plan; placed here, above UNKNOWN
    EventKind.UNKNOWN,
)

_PRECEDENCE_SUBKIND_FILTER: tuple[frozenset[str] | None, ...] = (
    None,
    None,
    None,
    _CACHE_SIGNAL_HIGH_SUBKINDS,
    None,  # LIMIT_HIT
    None,  # LIMIT_RESUME
    None,
    None,
    None,  # PLAN_FEEDBACK
    None,
    None,  # AGENT_TERMINATED
    None,
    None,
    None,
    None,
    None,  # other band: matches any CACHE_SIGNAL the high-band entry didn't
    None,
    None,
    None,  # TASK_STATUS
    None,  # STRUCTURED_OUTPUT
    None,
    None,
    None,
    None,
    None,
    None,
    None,
    None,
)


def _rank(event: Event) -> int:
    for index, kind in enumerate(PRECEDENCE):
        if kind != event.kind:
            continue
        subkind_filter = _PRECEDENCE_SUBKIND_FILTER[index]
        if subkind_filter is None or event.subkind in subkind_filter:
            return index
    return len(PRECEDENCE)


def primary_kind(events: Iterable[Event] | Sequence[Event]) -> EventKind:
    """Resolve the single highest-precedence kind among ``events`` per
    plan A2's ``PRECEDENCE`` list. Returns ``EventKind.UNKNOWN`` for an
    empty sequence — the same value ``Turn.preceding_primary`` defaults
    to, standing in for the table's "none".
    """
    best: Event | None = None
    best_rank = len(PRECEDENCE) + 1
    for event in events:
        rank = _rank(event)
        if rank < best_rank:
            best_rank = rank
            best = event
    return best.kind if best is not None else EventKind.UNKNOWN


__all__ = [
    "BUILT_IN_TOOLS",
    "TOOL_NAME_RE",
    "tool_server",
    "classify_line",
    "PRECEDENCE",
    "primary_kind",
    "classify_synthetic_text",
    "parse_limit_reset_clause",
    "parse_limit_reset_date",
    "image_token_estimate",
    "content_block_size",
    "sanitize_line_type",
]

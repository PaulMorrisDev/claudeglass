"""Single-pass transcript parsing: JSONL lines in, ``TranscriptResult`` out.

Implements the project plan's "Parsing" section: assistant lines are
grouped into ``Turn`` records by ``message.id`` (fallback ``requestId``,
then ``uuid``); every other line becomes an ``Event`` (via
``events.classify_line``) attached to the turn it PRECEDES as
``preceding_event_kinds``/``preceding_attachment_types``/
``preceding_primary`` — i.e. the turn whose assistant line comes right
after it, not the turn whose assistant line came right before it. A
task-notification line sitting between turn A and turn B describes what
happened just before B ran, so it must attach to B; it says nothing about
what preceded A. This is implemented with two rotating buffers
(``events_for_current``/``events_since_current`` in ``parse_transcript``):
whatever accumulates while a turn is the in-progress ``current`` precedes
the *next* turn, not this one, so it's held back and only handed to
``_finalize_turn`` once the next turn actually starts. Events seen after
the transcript's last assistant line have no later turn to attach to and
are counted in ``Diagnostics.trailing_events`` instead.

``preceding_tool`` (for a non-first turn) is "Bash" if the previous turn's
``tool_names`` contains Bash, else "PowerShell" if it contains PowerShell,
else the previous turn's first tool name, else "none" if it had no tools
at all — a priority scan, not simply the previous turn's first tool name,
so a shell call is surfaced even when it wasn't the first tool invoked in
that turn. The first turn in a transcript has no previous turn, so its
``preceding_tool`` is "n/a".

Replay dedup: a rewind/resume can re-emit a whole block of lines verbatim
(same ``uuid``) later in the same file. Every line (including assistant
lines, which are also deduped separately by message id) is skipped and
counted in ``Diagnostics.replayed_lines`` the second and later time its
``uuid`` is seen; lines with no ``uuid`` (``queue-operation``,
``bridge-session``) are never subject to this check.

Copied sessions: after ``/clear`` in a web or mobile session, Claude Code
writes the new session's lines both to that session's own file and into
the earlier session's file, keeping the new session's ``sessionId``. In a
top-level transcript, a line whose ``sessionId`` names another session
is skipped and counted in ``Diagnostics.copied_lines`` when that
session's own ``<sessionId>.jsonl`` sits in the same folder, so its
replies are priced once, from its own file. When there is no such file
the line is kept: many logs (and fixtures) carry a ``sessionId`` that
isn't their file name, and a copy with no original is the only record.

Tool search: Claude Code lists deferred tools by name
(``deferred_tools_delta``: ``addedNames``/``removedNames``) and records
each full definition it loads (``deferred_tools_record``: ``entries``).
Each turn keeps how many listed tools had no definition loaded when it
was requested, by MCP server (``Turn.deferred_tools_by_server``), and the
size of the name list (``deferred_list_chars``); the transcript keeps
each loaded definition's size by tool name
(``TranscriptResult.tool_definition_chars``). ``tool_search.py`` prices
what that kept out of each request.

Compaction calls: Claude Code bills the request that writes a
compaction's summary, but logs only the ``compact_boundary`` line, never
the request as a reply. After the pass, each boundary gets one estimated
turn (``Turn.estimated == "compaction"``) placed right after the reply
before it, on that reply's model. It reads that reply's cached prefix
(its cache read plus cache write) from the cache, or writes it again at
the same lifetime when the cache would have expired by the time the
compaction started (the boundary's time less ``durationMs``). What
``preTokens`` counts beyond that prefix is plain input, and ``postTokens``
is taken as the output: the summary, plus the few files Claude Code
attaches again, so it can run a little high. A boundary with no earlier
reply or no ``postTokens`` is left out and counted
(``Diagnostics.compaction_calls_unsized``). On a real 780,000-token
compaction this came within 2% of Claude Code's own ``cost-state``.

``gap_s`` is measured request-start to request-start: the interval between
the *first* JSONL line's timestamp of one priced turn and the first line's
timestamp of the previous priced turn, not (say) a turn's finalisation
time or its last line. ``discovery.find_sessions``'s ``--window-by
timestamp`` mode is the same convention applied at the session level: it
reads the first ``user``/``assistant`` line's timestamp, not the file's
own mtime. Both are deliberate, not an oversight — a turn/session's
*start* is the meaningful instant for gap and window calculations, and
it's the one value guaranteed to exist before any tool call or streaming
delay could skew it.

Usage is the opposite: a streamed reply is written as one line per
content block, each with a usage snapshot taken so far, so a turn's
token counts come from the most complete snapshot among its lines (the
largest ``output_tokens``), not the first. The first line's
``output_tokens`` is a partial count and only the last line carries
``output_tokens_details.thinking_tokens``.

Privacy: no raw JSONL line, message content, tool_result content, file
path, or command is ever retained past the single line/block that
produces it. Only lengths, short prefixes (<=40 chars), names, and counts
survive into ``Turn``/``Event``/``Diagnostics``. Absolute- and
relative-path-shaped tokens, URLs, and any ``@``-bearing token (an
``ssh user@host`` target, an email address) inside a Bash/PowerShell
command are redacted to ``<path>``/``<url>``/``<user@host>`` before the
40-char truncation (see ``_redact_paths``), so a path, host, or address
near the cutoff can never leak a partial drive letter, username, or
domain.

Batch C addition: ``meta`` is provenance the caller already knows (from
``discovery.py``) and this function never mutates the object it was
handed — but the ``TranscriptResult.meta`` it *returns* can carry three
more fields than the input, derived from the transcript's own content:
``claude_version``/``entrypoint`` (first non-empty ``version``/
``entrypoint`` field seen on any raw line) and ``provider`` (from the
first turn with a model, via ``detect_provider``). A field already set on
the input ``meta`` (e.g. by ``discovery.load_meta``, which derives
``provider`` from a subagent's ``.meta.json`` model alias before any
turn is known) is left as-is, never overwritten by the scan — except
``provider``, where the transcript's own per-turn model is the more
authoritative source and takes precedence once a turn with a model
exists.

Usage-limits batch (v3-limits, see model.py's/events.py's module
docstrings): a synthetic assistant line's own text is classified with
``events.classify_synthetic_text``/``events.parse_limit_reset_clause``
in ``_new_pending`` and stored on ``Turn.synthetic_kind``. For the two
kinds that mean a usage cap was hit (``session_limit``/``weekly_limit``),
``parse_transcript``'s main loop synthesises a ``LIMIT_HIT`` event itself
right after ``current`` is set (the synthetic text lives on an
*assistant* line, and assistant lines never reach ``events.classify_line``
— see that module's docstring) and appends it to both ``events`` and
``events_since_current``, so — per the two-buffer scheme above — it
correctly precedes the *next* turn, not the synthetic line's own. The
reset instant (``Event.detail["reset_ts"]``) prefers the line's own
``quotaLimits.resetsAt`` epoch (see :func:`_limit_reset_ts`); the
parsed local-time-plus-zone clause is only a fallback for the ~24% of
limit-hit lines that don't carry ``quotaLimits``. ``Turn.gap_cause`` is
set to ``"limit"`` in ``_finalize_turn`` whenever a ``LIMIT_HIT``/
``LIMIT_RESUME`` event precedes that turn.

Wasted-turns batch (v4-wasted-turns, see model.py's ``Turn.
tool_error_count``/``tool_error_chars`` docstrings): ``_accumulate_tool_
results`` already walks every ``tool_result`` block answering the current
turn's own ``tool_use_ids`` to build ``tool_result_chars_by_tool``; the
same loop now also checks each block's own ``is_error`` field (``bool``
on the real corpus, verified read-only against the full local project
tree before implementing) and, when it is ``True``, increments
``current.tool_error_count`` and adds the same ``_tool_result_length``
figure already computed for that block onto ``current.tool_error_chars``
— no second pass over the content list, keeping the parser single-pass.
The same loop reads the start of each error's text to record why it
failed (:func:`_tool_error_kind`, ``Turn.tool_errors_by_kind``); only
the kind is kept.

Parser-signals batch (SURV-5/6/7, ``PARSER_VERSION`` 19, see model.py's
and events.py's own module docstrings): ``"cost-state"`` joins the
``elif line_type ==`` chain above ``events_mod.classify_line`` the same
way ``"mode"``/``"agent-setting"`` already do -- its own value (the last
``totalCostUSD``/``hasUnknownModelCost`` seen) is read directly onto
``final_meta`` rather than carried as an ``Event``, and the type itself
now sits in ``events._IGNORABLE_TYPES`` so it lands in the long-standing
``Diagnostics.ignored_line_types`` bucket like every other deliberately-
ignored type. ``parser_notes["unknown_line_types"]``/``["unsized_blocks"]``
are two new counters that don't fit ``Diagnostics`` (whose field list
this phase was told not to touch): the former is populated in the
existing ``EventKind.UNKNOWN`` branch below (a type classify_line had no
rule for at all, sanitised via ``events.sanitize_line_type``); the
latter is populated by ``_tool_result_length`` for a tool_result's own
image/document blocks and folded in from a HUMAN_TEXT event's own
``detail["unsized_blocks"]`` for a top-level human-prompt image (see
``events.content_block_size``) -- one counter, two sources, both flagged
"unsized" rather than guessed at when this parser can't size a block.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import posixpath
import re
import secrets
import tempfile
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import capture_tags
from . import events as events_mod
from . import jsonl
from . import shell_writes
from .model import (
    PROMPT_FLAGS,
    CaptureTag,
    Diagnostics,
    Event,
    EventKind,
    Feedback,
    PlanStats,
    Turn,
    TranscriptMeta,
    TranscriptResult,
)

#: Tool names whose first ``input.command`` becomes a turn's ``cmd_prefix``.
_SHELL_TOOL_NAMES = ("Bash", "PowerShell")

#: tool name -> the input key holding the path to check against the
#: system temp dir for ``edit_kind`` ("scratch" vs "real").
_EDIT_TOOL_PATH_KEYS = {
    "Edit": "file_path",
    "Write": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}

#: Capture-improvements addition (A2): tool_use names whose own ``prompt``
#: input string is a spawned-agent brief. Only its length is ever kept
#: (``Turn.agent_brief_chars``) — never the text.
_AGENT_TOOL_NAMES = ("Agent", "Task")

#: Capture-improvements addition (A3): tool name -> the input key holding
#: the read target path to hash (``Turn.read_target_hashes``). Read only
#: since ``PARSER_VERSION`` 15: an edit is not a read, and counting edits
#: here made every file edited after reading it look read twice. Edit
#: targets are hashed into ``edit_target_hashes`` from
#: ``_EDIT_TOOL_PATH_KEYS`` instead.
_READ_TARGET_PATH_KEYS = {
    "Read": "file_path",
}

#: Error kinds (``_tool_error_kind``) meaning a shell command never ran, so
#: the files it would have written weren't. A command that ran and exited
#: non-zero may still have written them.
_SHELL_NOT_RUN_KINDS = ("blocked", "denied")

#: Quality-markers and metrics-capture addition: a reply ending
#: "[result: <word> ...]" is a subagent's own account of whether it
#: finished, and "[tl: ...]" the capture tag (``capture_tags``). Only the
#: last text block's last characters are kept while the turn is open, and
#: only the parsed words once it is finalised.
_TAG_TAIL_CHARS = capture_tags.TAIL_SCAN_CHARS

#: Metrics-capture addition: an ``ExitPlanMode`` plan's numbered steps
#: (``1.`` or ``1)``), or its bullets when it numbers none.
_PLAN_NUMBERED_RE = re.compile(r"^\s*\d{1,3}[.)]\s+\S", re.MULTILINE)
_PLAN_BULLET_RE = re.compile(r"^\s*[-*]\s+\S", re.MULTILINE)

#: MSYS/Git Bash drive form (``/c/Dev/x``), mapped to ``c:/Dev/x``.
_MSYS_DRIVE_RE = re.compile(r"^/([A-Za-z])(?=/|$)")

_CMD_PREFIX_MAX_CHARS = 40

#: H1/ROB-P1: only the first ``_CMD_PREFIX_MAX_CHARS`` of a redacted
#: command are ever kept, but ``_redact_paths``'s regexes previously ran
#: on the whole, unbounded ``input.command`` string first -- a command
#: embedding a huge unbroken blob (a base64 heredoc, say) paid to
#: redact content nobody stores. ``_cap_command_for_redaction`` below
#: caps the input to this many characters first, well above
#: ``_CMD_PREFIX_MAX_CHARS`` so the kept prefix is unaffected.
_CMD_REDACT_INPUT_MAX_CHARS = 512

#: Usage-limits addition (see module docstring): event kinds whose
#: presence among a turn's preceding events marks its gap as a usage-cap
#: pause rather than idle/behavioural time (``Turn.gap_cause``).
_LIMIT_GAP_KINDS = (EventKind.LIMIT_HIT, EventKind.LIMIT_RESUME)

#: Absolute-path token shapes to redact out of a command prefix before it
#: is truncated (privacy criterion: zero drive letters/usernames in
#: exports). Matches a whole "word" starting with one of these shapes,
#: stopping at the first whitespace or quote so the verb and flags around
#: it survive. Order doesn't matter — each alternative is anchored to a
#: distinct prefix shape.
#:
#: R4 fix: the previous alternatives only caught *absolute* forms
#: (``\Users\...``, ``/home/...``) — a *relative* Windows path with no
#: leading separator (``cd Users\paulm\proj``, the shape a shell prints
#: for a path relative to the drive root) survived untouched. The new
#: last alternative catches just the ``Users``/``home``/``Documents and
#: Settings`` segment plus its own username component (stopping at the
#: next separator, same as the bare-backslash alternative above it) —
#: deliberately narrower than the greedy absolute-path alternatives,
#: since only the username segment is sensitive; a following relative
#: path component (a repo name, say) isn't. The ``(?<![\w:])`` lookbehind
#: keeps it from firing mid-word or right after a drive-letter colon,
#: where the absolute-path alternatives above already have it covered.
_ABS_PATH_TOKEN_RE = re.compile(
    r"""
    [A-Za-z]:[\\/][^\s"']*                       # C:\... or C:/...
    | /(?:home|Users|tmp|var|mnt|etc)/[^\s"']*    # POSIX absolute homes/tmp
    | /[a-zA-Z]/[^\s"']*                          # MSYS/Git Bash drive form: /c/Dev/x
    | \\(?:Users|home)\\[^\s"']*                  # bare \Users\... or \home\... (no drive letter)
    | ~[\\/][^\s"']*                              # ~/... or ~\...
    | %[A-Z_]+%[^\s"']*                           # %USERPROFILE%\...
    | \$HOME[^\s"']*                              # $HOME/...
    | (?<![\w:])(?:Users|home|Documents\ and\ Settings)[\\/][^\s\\/]+
                                                   # relative Users\name / home/name (no leading separator)
    """,
    re.VERBOSE,
)

#: Fix item 3: URLs in a command prefix are as identity-leaking as an
#: absolute path (a bug tracker link, an internal hostname, a signed
#: URL's query string) and were previously left untouched by
#: ``_redact_paths``. Matches an ``http``/``https`` URL, or a bare
#: ``www.`` form with no scheme, up to the next whitespace/quote.
_URL_TOKEN_RE = re.compile(r"""https?://[^\s"']+|www\.[^\s"']+""")

#: R4 fix: any whitespace-delimited token containing ``@`` is redacted
#: wholesale — this covers both an ``ssh user@host`` target and a bare
#: email address (e.g. inside a commit message), neither of which the
#: path/URL patterns above ever matched. Deliberately not anchored to
#: a stricter user@host/email shape: a bare ``@`` in a command is
#: already a strong enough identity signal (an account/host name) that
#: erring toward over-redaction here is the right trade-off.
_AT_TOKEN_RE = re.compile(r"""[^\s"']*@[^\s"']*""")


def _cap_command_for_redaction(command: str) -> str:
    """``command`` capped to ``_CMD_REDACT_INPUT_MAX_CHARS`` (H1/ROB-P1),
    at a whitespace boundary rather than a hard character cut: a token
    straddling the cutoff -- e.g. an ``ssh user@host`` target whose
    ``@`` lands just past it -- is dropped whole rather than left as an
    identity-bearing fragment whose own redaction trigger got cut off.
    A command with no whitespace at all in its first
    ``_CMD_REDACT_INPUT_MAX_CHARS`` characters (one huge unbroken token
    -- a base64 heredoc, say) falls back to a hard cut: every
    ``_redact_paths`` pattern is a run of non-whitespace/quote
    characters with no required closing delimiter, so a hard cut mid
    such a token still matches (and still collapses to the same fixed
    placeholder) all the way to the truncated end, same as the
    uncapped input would past that point.
    """
    if len(command) <= _CMD_REDACT_INPUT_MAX_CHARS:
        return command
    head = command[:_CMD_REDACT_INPUT_MAX_CHARS]
    for i in range(len(head) - 1, -1, -1):
        if head[i].isspace():
            return head[:i]
    return head


def _redact_paths(text: str) -> str:
    """Replace every absolute- or relative-path-shaped token in ``text``
    with ``<path>``, every URL with ``<url>``, and every ``@``-bearing
    token (an ``ssh user@host`` target, an email address) with
    ``<user@host>`` — keeping the surrounding verb/flags intact. Called
    before truncation so a path, URL, or user@host/email near the
    40-char cutoff can't leak a partial drive letter, username fragment,
    query string, or domain. The caller already caps ``text`` to
    ``_CMD_REDACT_INPUT_MAX_CHARS`` (H1/ROB-P1) before it reaches here,
    at a whitespace boundary, so this never walks more of a huge
    command than the kept prefix could ever need.

    URLs are redacted first: ``_ABS_PATH_TOKEN_RE``'s drive-letter
    alternative (``[A-Za-z]:[\\/]``) is happy to match the single
    letter before a scheme's ``://`` (e.g. the "s" in "https://"),
    which would otherwise mangle a URL into "http<path>" before the URL
    regex ever saw it intact. The ``@`` rule runs next (a URL's own
    ``user:pass@host`` form, if any, has already been swallowed whole
    into ``<url>`` by then), and the path rule runs last.
    """
    without_urls = _URL_TOKEN_RE.sub("<url>", text)
    without_at = _AT_TOKEN_RE.sub("<user@host>", without_urls)
    return _ABS_PATH_TOKEN_RE.sub("<path>", without_at)


#: Capture-improvements addition (A3): module-level salt used by
#: ``_read_target_hash``. Threaded through a setter rather than a
#: ``parse_transcript`` parameter so the function's public signature
#: stays stable (per this task's contract) while still letting a
#: ``ProcessPoolExecutor`` worker (spawned fresh under Windows ``spawn``)
#: initialise it once before parsing any file — see ``set_salt``.
_SALT: bytes | None = None

_SALT_FILENAME = "salt"

#: ``0`` on POSIX (no such flag; binary is the only mode ``open()``/
#: ``os.open()`` ever use there). On Windows, ``os.open()`` without this
#: flag defaults to *text* mode, which silently rewrites any ``b"\n"``
#: (0x0a) byte in the data to ``b"\r\n"`` on write -- fatal for a random
#: 32-byte salt, where roughly one in eight salts contains at least one
#: 0x0a byte. Omitting it was a real, intermittent bug (not just a flaky
#: test): the freshly written salt and the salt read back moments later
#: would silently differ whenever the random salt happened to contain a
#: newline byte, corrupting every hash taken with it as "the" salt for
#: this config dir.
_O_BINARY = getattr(os, "O_BINARY", 0)


#: The only valid salt length -- ``secrets.token_bytes(32)``'s own output
#: size. Enforced by both :func:`set_salt` and :func:`load_or_create_salt`
#: (fix #4): a shorter salt collapses HMAC-SHA256's effective key space and
#: a longer one is simply not what this module ever writes, so either is
#: treated as a corrupted/foreign file rather than accepted silently.
_SALT_LENGTH_BYTES = 32


def set_salt(salt: bytes) -> None:
    """Set the process-wide salt used by ``_read_target_hash`` for
    ``Turn.read_target_hashes``. Must be called once (per process) before
    ``parse_transcript`` if hashed read targets are wanted — with no salt
    set, ``Turn.read_target_hashes`` is always empty (see
    ``_read_target_hash``). A plain module global, not a
    ``parse_transcript`` argument, so callers running under
    ``ProcessPoolExecutor`` (``corpus.py``) can initialise each worker
    process once via an initializer rather than threading the salt
    through every call.

    Raises ``ValueError`` if ``salt`` is not exactly
    :data:`_SALT_LENGTH_BYTES` long (fix #4) — an unsalted-strength hash
    from a truncated or foreign salt is worse than no hash at all (see
    ``_read_target_hash``'s own "``None`` rather than unsalted" contract).
    """
    if len(salt) != _SALT_LENGTH_BYTES:
        raise ValueError(f"salt must be {_SALT_LENGTH_BYTES} bytes, got {len(salt)}")
    global _SALT
    _SALT = salt


def _default_claudeglass_dir() -> Path:
    """``$CLAUDE_CONFIG_DIR/claudeglass``, else ``~/.claude/claudeglass``.
    Mirrors ``cli.py``'s own ``_resolve_config_dir`` (see its docstring on
    why each module keeps its own copy of this lookup rather than
    importing one from another) — this module deliberately doesn't import
    ``cli.py``/``config.py`` to stay a leaf dependency.
    """
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    root = Path(base) if base else (Path.home() / ".claude")
    return root / "claudeglass"


def load_or_create_salt(config_dir: str | Path | None = None) -> bytes:
    """Load the 32-byte salt at ``<config_dir>/salt``, creating it with
    ``secrets.token_bytes(32)`` on first use. ``config_dir`` defaults to
    ``_default_claudeglass_dir()``. Does not call ``set_salt`` itself — the
    caller decides when the process-wide salt is wired up.

    Fix #4, narrowed by SEC-P8/G7: only a missing file (``FileNotFoundError``)
    is treated as "no salt yet" and silently regenerated. A read failure
    that means the file is *there* but unreadable right now
    (``PermissionError``, ``IsADirectoryError``, a dead network mount, ...)
    propagates instead of being folded into the same "missing" case --
    the old, broader ``except OSError`` would rotate the salt on a
    transient permission problem exactly as if the file had never
    existed, silently breaking every session-id hash correlation this
    tool has ever written to ``signals/`` or a cache's provenance header,
    for a condition that is usually temporary. A salt file whose length
    is not exactly :data:`_SALT_LENGTH_BYTES` (a zero-byte file from an
    interrupted first write, a truncated sync, a hand-edited file) is
    still treated as absent and regenerated -- returning it unsalted
    would defeat the whole hashing mechanism (a zero-length salt makes
    ``_read_target_hash`` produce a plain, rainbow-table-able HMAC). The
    replacement file is created via ``os.open`` with ``O_CREAT`` and mode
    ``0o600`` together, so a brand-new file is never briefly
    world-readable between creation and a separate ``chmod`` call;
    ``chmod`` still runs afterwards (best-effort, ignored on Windows,
    which has no equivalent bit) to cover the overwrite-an-existing-but-
    invalid-file branch, where ``O_CREAT``'s mode argument has no effect
    on an already-existing inode's permissions.
    """
    directory = Path(config_dir) if config_dir is not None else _default_claudeglass_dir()
    directory.mkdir(parents=True, exist_ok=True)
    salt_path = directory / _SALT_FILENAME
    try:
        existing = salt_path.read_bytes()
    except FileNotFoundError:
        existing = None
    if existing is not None and len(existing) == _SALT_LENGTH_BYTES:
        return existing

    salt = secrets.token_bytes(_SALT_LENGTH_BYTES)
    try:
        fd = os.open(salt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY, 0o600)
    except FileExistsError:
        # The file exists but was rejected above (missing/unreadable/wrong
        # length) -- overwrite it in place rather than trying (and racing)
        # to delete-then-recreate it.
        fd = os.open(salt_path, os.O_WRONLY | os.O_TRUNC | _O_BINARY)
    try:
        os.write(fd, salt)
    finally:
        os.close(fd)
    try:
        os.chmod(salt_path, 0o600)
    except OSError:
        pass
    return salt


def _normalize_path_for_hash(path_value: str) -> str:
    """Normalise a tool_use target path before hashing, so the same real
    location hashes the same way regardless of slash direction, case
    (Windows paths are case-insensitive), Git Bash's ``/c/`` drive form or
    ``.``/``..`` segments (a shell command's relative path joined to its
    directory) — deliberately *not* resolving against the filesystem
    (``Path.resolve()``), since this path may not exist on this machine
    (e.g. a subagent transcript scrubbed for fixtures) and a hash function
    must never raise on its input.
    """
    normalized = _MSYS_DRIVE_RE.sub(r"\1:", path_value.replace("\\", "/"))
    return posixpath.normpath(normalized).casefold()


def _read_target_hash(path_value: str) -> str | None:
    """Salted HMAC-SHA256 of a normalised read/write target path, truncated
    to 16 hex chars — ``None`` when no salt has been set yet (see
    ``set_salt``), so a caller that never wires up hashing simply gets no
    hashes rather than an unsalted (crackable) one.
    """
    if _SALT is None:
        return None
    return path_hash(path_value, _SALT)


def path_hash(path_value: str, salt: bytes) -> str:
    """The same salted hash as :func:`_read_target_hash`, with the salt
    passed in, so a file found on disk can be matched to the records
    transcripts keep for it (``claude_md_review``)."""
    normalized = _normalize_path_for_hash(path_value)
    return hmac.new(salt, normalized.encode("utf-8"), hashlib.sha256).hexdigest()[:16]


def detect_provider(model_id: str | None) -> str | None:
    """Classify which API surface a model id was billed through, from the
    id's own form (plan Enterprise-use section): a Bedrock id is prefixed
    ``anthropic.``/``us.anthropic.`` or suffixed ``-v1:0``; a Vertex id
    carries an ``@<date>`` suffix; anything else is a direct Anthropic API
    id. Returns ``None`` for an empty/missing id — nothing to classify.

    Shared with ``discovery.load_meta``, which derives a subagent's
    ``provider`` from its ``.meta.json`` model alias before any turn is
    parsed; ``parse_transcript`` recomputes it from the transcript's own
    turns once one exists, since a turn's ``model`` is the authoritative
    source (see this module's docstring).
    """
    if not model_id:
        return None
    lowered = model_id.lower()
    if lowered.startswith("anthropic.") or lowered.startswith("us.anthropic.") or lowered.endswith("-v1:0"):
        return "bedrock"
    if "@" in model_id:
        return "vertex"
    return "anthropic"


def _escape_newlines(text: str) -> str:
    return text.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")


def _parse_ts(ts_raw: str) -> datetime | None:
    if not ts_raw:
        return None
    try:
        return datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _synthetic_text(content: object) -> str | None:
    """The text of a synthetic assistant line's ``message.content``,
    which the real corpus always shows as a list of content blocks (a
    plain string is tolerated defensively but not observed) -- for
    classification only, never retained.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    return text
    return None


def _limit_reset_ts(
    d: dict, ts_dt: datetime | None, minutes_of_day: int | None, reset_tz: str | None
) -> str | None:
    """UTC ISO timestamp of a limit-hit's reset. Prefers the line's own
    ``quotaLimits.resetsAt`` (a precise Unix epoch, UTC) -- present on
    only around three-quarters of limit-hit lines in the sampled corpus
    -- over reconstructing one from the parsed local-time-of-day clause
    plus ``reset_tz``, which needs ``zoneinfo`` to resolve the zone (may
    be unavailable, e.g. missing tzdata on a bare Windows install) and a
    reference date (this line's own timestamp, in that zone) to anchor
    "today" vs. "tomorrow". Returns ``None`` when neither source is
    usable.
    """
    quota_limits = d.get("quotaLimits")
    if isinstance(quota_limits, dict):
        resets_at = quota_limits.get("resetsAt")
        if isinstance(resets_at, (int, float)) and not isinstance(resets_at, bool):
            try:
                return (
                    datetime.fromtimestamp(float(resets_at), tz=timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z")
                )
            except (OverflowError, OSError, ValueError):
                pass

    if minutes_of_day is None or reset_tz is None or ts_dt is None:
        return None
    try:
        from zoneinfo import ZoneInfo

        zone = ZoneInfo(reset_tz)
    except Exception:
        # Missing tzdata, or a name zoneinfo doesn't recognise -- never
        # let this fall through to an exception escaping the parser.
        return None
    try:
        local_ts = ts_dt.astimezone(zone)
        reset_hour, reset_minute = divmod(minutes_of_day, 60)
        candidate = local_ts.replace(hour=reset_hour, minute=reset_minute, second=0, microsecond=0)
        if candidate <= local_ts:
            candidate = candidate + timedelta(days=1)
        return candidate.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        return None


def _turn_key(d: dict) -> str:
    """The grouping key for one assistant line: ``message.id``, falling
    back to ``requestId``, then ``uuid``. Prefixed by source so the three
    id spaces never collide with each other.
    """
    message = d.get("message")
    message_id = message.get("id") if isinstance(message, dict) else None
    if isinstance(message_id, str) and message_id:
        return f"id:{message_id}"
    request_id = d.get("requestId")
    if isinstance(request_id, str) and request_id:
        return f"req:{request_id}"
    uuid = d.get("uuid")
    if isinstance(uuid, str) and uuid:
        return f"uuid:{uuid}"
    return "key:"


@dataclass(slots=True)
class _PendingTurn:
    """Scalar accumulator for one in-progress ``message.id`` group. Never
    retains a raw line dict or message content past the block that fed
    it — only the small derived fields ``Turn`` itself needs.
    """

    message_id: str = ""
    request_id: str = ""
    ts_raw: str = ""
    model: str = ""
    service_tier: str | None = None
    is_synthetic: bool = False
    effort: str | None = None
    per_turn_effort: str | None = None
    has_usage: bool = False
    input_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    web_search_requests: int = 0
    web_fetch_requests: int = 0
    cc_5m: int = 0
    cc_1h: int = 0
    #: Set when the raw ``cache_creation_input_tokens`` flat field didn't
    #: equal ``cc_5m + cc_1h`` for this turn's usage, computed once at
    #: ``_new_pending`` time (before any reconciliation) so a later
    #: correction to ``cache_creation_tokens`` doesn't erase the signal.
    ttl_sum_mismatch: bool = False
    #: Set when ``usage`` had no nested ``cache_creation`` object at all
    #: (older, pre-split JSONL) - see ``Turn.ttl_split_unknown``. Never
    #: set alongside ``ttl_sum_mismatch``: a missing split is a format
    #: difference, not a sum invariant breach.
    ttl_split_unknown: bool = False
    inference_geo: str | None = None
    attribution_mcp_server: str | None = None
    attribution_mcp_tool: str | None = None
    attribution_skill: str | None = None
    tool_names: list[str] = field(default_factory=list)
    #: Batch C addition (see model.py's ``Turn.tool_use_ids`` docstring):
    #: every ``tool_use`` block's ``id`` in this turn, in encounter order.
    tool_use_ids: list[str] = field(default_factory=list)
    cmd_prefix: str | None = None
    edit_real_found: bool = False
    edit_scratch_found: bool = False
    #: Capture-improvements addition (A2, see model.py's ``Turn.
    #: agent_brief_chars``/``tool_input_chars_by_tool`` docstrings).
    agent_brief_chars: int | None = None
    tool_input_chars_by_tool: dict[str, int] = field(default_factory=dict)
    #: Capture-improvements addition (A3, see model.py's ``Turn.
    #: read_target_hashes`` docstring).
    read_target_hashes: list[str] = field(default_factory=list)
    #: Capture-improvements addition (A1, see model.py's ``Turn.
    #: tool_result_chars_by_tool``/``tool_wait_s``/``model_latency_s``
    #: docstrings): populated by ``_accumulate_tool_results`` for
    #: tool_result lines answering *this* turn's own ``tool_use_ids``.
    tool_result_chars_by_tool: dict[str, int] = field(default_factory=dict)
    tool_result_ts_values: list[str] = field(default_factory=list)
    #: Usage-limits addition (see module docstring): set only when
    #: ``is_synthetic`` is True.
    synthetic_kind: str | None = None
    reset_minutes_of_day: int | None = None
    reset_tz: str | None = None
    reset_ts: str | None = None
    #: Wasted-turns addition (see model.py's ``Turn.tool_error_count``/
    #: ``tool_error_chars`` docstrings): populated by
    #: ``_accumulate_tool_results`` for tool_result lines answering this
    #: turn's own ``tool_use_ids`` that carry ``is_error: true``.
    tool_error_count: int = 0
    tool_error_chars: int = 0
    #: Context-files addition (see model.py's ``Turn.skills_invoked``).
    #: SEC-P3: keyed by the ``Skill`` tool_use's own id rather than
    #: appended eagerly, so a call whose result later errors can be taken
    #: back (mirrors ``edit_hashes_by_tool_use`` below) instead of
    #: self-authorising its own name for this same turn's tag claim.
    skill_calls_by_tool_use: dict[str, str] = field(default_factory=dict)
    #: Quality-signals addition (see model.py's ``Turn.stop_reason``/
    #: ``tool_errors_by_tool``/``edit_target_hashes``).
    stop_reason: str | None = None
    tool_calls_by_tool: dict[str, int] = field(default_factory=dict)
    tool_errors_by_tool: dict[str, int] = field(default_factory=dict)
    tool_errors_by_kind: dict[str, int] = field(default_factory=dict)
    edit_target_hashes: list[str] = field(default_factory=list)
    #: tool_use id -> the hashes it added to ``edit_target_hashes``, so
    #: ``_accumulate_tool_results`` can take back an edit that failed.
    edit_hashes_by_tool_use: dict[str, list[str]] = field(default_factory=dict)
    #: Fast-mode addition (see model.py's ``Turn.speed`` docstring).
    speed: str | None = None
    #: Quality-markers/metrics-capture addition: the end of this reply's
    #: last text block, parsed for tags when the turn is finalised and
    #: then dropped (see model.py's ``Turn.result_marker``/``Turn.cap``).
    last_text_tail: str | None = None
    #: Metrics-capture addition (see model.py's ``Turn.agent_result_chars``/
    #: ``Turn.plan_stats``).
    agent_result_chars: dict[str, int] = field(default_factory=dict)
    plan_stats: PlanStats | None = None
    #: Feedback addition (see model.py's ``Turn.feedback``): this turn's
    #: AskUserQuestion calls that ask /tl-feedback's questions, and what
    #: their answers said.
    feedback_asks: list[str] = field(default_factory=list)
    feedback: Feedback | None = None
    #: Your-hooks addition (see model.py's ``Turn.hook_blocks``/
    #: ``hook_resends``): tool_use id -> a hash of its tool name and
    #: input, in memory only, so a blocked call's unchanged re-send can be
    #: spotted later in the transcript.
    call_keys: dict[str, int] = field(default_factory=dict)
    hook_blocks: dict[str, int] = field(default_factory=dict)
    hook_resends: dict[str, int] = field(default_factory=dict)
    #: Tool-search addition (see model.py's ``Turn.deferred_tools_by_server``/
    #: ``deferred_list_chars``): set once, when the reply is requested.
    deferred_tools_by_server: dict[str, int] = field(default_factory=dict)
    deferred_list_chars: int = 0


#: A tool name kept as a key (``TranscriptResult.tool_definition_chars``):
#: the API's own tool-name alphabet, so never a path or free text.
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")

#: The server of a deferred tool that isn't an MCP tool.
BUILT_IN_TOOLS = "built-in"


def tool_server(name: str) -> str:
    """The MCP server a tool comes from (``mcp__<server>__<tool>``), or
    :data:`BUILT_IN_TOOLS` for one of Claude Code's own."""
    parts = name.split("__")
    if len(parts) >= 3 and parts[0] == "mcp" and parts[1]:
        return parts[1][:64]
    return BUILT_IN_TOOLS


class _DeferredTools:
    """Tool search, as a transcript records it (see the module docstring):
    the tools listed by name only, and the full definitions loaded since."""

    __slots__ = ("names", "loaded", "definition_chars", "_snapshot")

    def __init__(self) -> None:
        self.names: set[str] = set()
        self.loaded: set[str] = set()
        #: Tool name -> characters of its full definition, as loaded.
        self.definition_chars: dict[str, int] = {}
        self._snapshot: tuple[dict[str, int], int] | None = ({}, 0)

    def note(self, attachment: dict) -> None:
        kind = attachment.get("type")
        if kind == "deferred_tools_delta":
            for name in _tool_names(attachment.get("removedNames")):
                self.names.discard(name)
            for name in _tool_names(attachment.get("addedNames")):
                self.names.add(name)
        elif kind == "deferred_tools_record":
            entries = attachment.get("entries")
            for entry in entries if isinstance(entries, list) else ():
                if not isinstance(entry, dict):
                    continue
                name = entry.get("name")
                if not isinstance(name, str) or not _TOOL_NAME_RE.match(name):
                    continue
                self.loaded.add(name)
                definition = {key: entry.get(key) for key in ("name", "description", "input_schema")}
                chars = len(json.dumps(definition, ensure_ascii=False, separators=(",", ":"), default=str))
                self.definition_chars[name] = max(chars, self.definition_chars.get(name, 0))
        else:
            return
        self._snapshot = None

    def snapshot(self) -> tuple[dict[str, int], int]:
        """Tools listed by name only when a reply is requested, by server,
        and the characters of the name list sent in their place."""
        if self._snapshot is None:
            by_server: dict[str, int] = {}
            for name in self.names - self.loaded:
                server = tool_server(name)
                by_server[server] = by_server.get(server, 0) + 1
            self._snapshot = (by_server, sum(len(name) + 1 for name in self.names))
        by_server, list_chars = self._snapshot
        return dict(by_server), list_chars


def _tool_names(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [name for name in value if isinstance(name, str) and _TOOL_NAME_RE.match(name)]


def _plan_stats(plan: str) -> PlanStats:
    """Counts for one ``ExitPlanMode`` plan: never its text."""
    steps = len(_PLAN_NUMBERED_RE.findall(plan)) or len(_PLAN_BULLET_RE.findall(plan))
    return PlanStats(steps=steps, files=events_mod.path_count(plan), chars=len(plan))


def _merge_content_blocks(
    pending: _PendingTurn,
    content,
    tool_use_names: dict[str, str],
    cwd: str | None = None,
    blocked_calls: dict[int, str] | None = None,
) -> None:
    """Fold one line's tool_use blocks into ``pending``. ``cwd`` is the
    line's own working directory, used only to resolve a shell command's
    relative write targets before they are hashed. ``blocked_calls`` maps
    the key of each call a hook blocked earlier in the transcript to that
    hook's label: a call here with the same key is an unchanged re-send."""
    if not isinstance(content, list):
        return
    tmpdir = tempfile.gettempdir().lower()
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            # The reply's last text block decides: a marker further up
            # was quoted, not reported.
            pending.last_text_tail = block["text"][-_TAG_TAIL_CHARS:]
            continue
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name = block.get("name")
        if not isinstance(name, str) or not name:
            continue
        if name not in pending.tool_names:
            pending.tool_names.append(name)
        pending.tool_calls_by_tool[name] = pending.tool_calls_by_tool.get(name, 0) + 1
        tool_use_id = block.get("id")
        if isinstance(tool_use_id, str) and tool_use_id:
            tool_use_names[tool_use_id] = name
            pending.tool_use_ids.append(tool_use_id)
        tool_input = block.get("input")
        if not isinstance(tool_input, dict):
            continue
        if pending.cmd_prefix is None and name in _SHELL_TOOL_NAMES:
            command = tool_input.get("command")
            if isinstance(command, str) and command:
                capped = _cap_command_for_redaction(command)
                redacted = _redact_paths(_escape_newlines(capped))
                pending.cmd_prefix = redacted[:_CMD_PREFIX_MAX_CHARS]
        path_key = _EDIT_TOOL_PATH_KEYS.get(name)
        if path_key is not None:
            path_value = tool_input.get(path_key)
            if isinstance(path_value, str) and path_value:
                if path_value.lower().startswith(tmpdir):
                    pending.edit_scratch_found = True
                else:
                    pending.edit_real_found = True

        # A2: agent-brief size -- the Agent/Task tool_use's own `prompt`
        # input length, never the prompt text itself -- plus a per-tool
        # total of every tool_use's JSON-encoded input size.
        if name in _AGENT_TOOL_NAMES:
            prompt = tool_input.get("prompt")
            if isinstance(prompt, str) and prompt:
                pending.agent_brief_chars = (pending.agent_brief_chars or 0) + len(prompt)
        encoded_input = json.dumps(tool_input, ensure_ascii=False, default=str)
        input_chars = len(encoded_input)
        pending.tool_input_chars_by_tool[name] = (
            pending.tool_input_chars_by_tool.get(name, 0) + input_chars
        )
        # Your-hooks addition: the same call a hook blocked, sent again
        # with the same input. Only a hash is kept, and only in memory.
        call_key = hash((name, encoded_input))
        if isinstance(tool_use_id, str) and tool_use_id:
            pending.call_keys[tool_use_id] = call_key
        if blocked_calls:
            blocked_by = blocked_calls.pop(call_key, None)
            if blocked_by is not None:
                pending.hook_resends[blocked_by] = pending.hook_resends.get(blocked_by, 0) + 1

        # A3: hash Read targets, Edit/Write/MultiEdit/NotebookEdit targets
        # and the files a shell command writes, instead of ever storing
        # the path.
        edited: list[str] = []
        read_target_key = _READ_TARGET_PATH_KEYS.get(name)
        if read_target_key is not None:
            target_value = tool_input.get(read_target_key)
            if isinstance(target_value, str) and target_value:
                hashed = _read_target_hash(target_value)
                if hashed is not None:
                    pending.read_target_hashes.append(hashed)
        elif path_key is not None:
            target_value = tool_input.get(path_key)
            if isinstance(target_value, str) and target_value:
                hashed = _read_target_hash(target_value)
                if hashed is not None:
                    edited.append(hashed)
        elif name in _SHELL_TOOL_NAMES and _SALT is not None:
            command = tool_input.get("command")
            if isinstance(command, str) and command:
                for target in shell_writes.write_targets(
                    command, powershell=name == "PowerShell", cwd=cwd if isinstance(cwd, str) else None
                ):
                    edited.append(path_hash(target, _SALT))
        if edited:
            pending.edit_target_hashes.extend(edited)
            if isinstance(tool_use_id, str) and tool_use_id:
                pending.edit_hashes_by_tool_use.setdefault(tool_use_id, []).extend(edited)

        if name == "Skill":
            # SEC-P3: only a name shaped like a real skill is even a
            # candidate, and it's provisional until the call comes back
            # without an error -- see _accumulate_tool_results.
            skill_name = tool_input.get("skill")
            if (
                isinstance(skill_name, str)
                and capture_tags.SKILL_NAME_RE.match(skill_name)
                and isinstance(tool_use_id, str)
                and tool_use_id
            ):
                pending.skill_calls_by_tool_use[tool_use_id] = skill_name
        elif name == "ExitPlanMode":
            plan = tool_input.get("plan")
            if isinstance(plan, str) and plan:
                pending.plan_stats = _plan_stats(plan)
        elif name == "AskUserQuestion" and isinstance(tool_use_id, str) and tool_use_id:
            if capture_tags.asks_for_feedback(tool_input):
                pending.feedback_asks.append(tool_use_id)


def _new_pending(
    d: dict, tool_use_names: dict[str, str], blocked_calls: dict[int, str] | None = None
) -> _PendingTurn:
    message = d.get("message")
    message = message if isinstance(message, dict) else {}
    usage = message.get("usage")

    pending = _PendingTurn()
    message_id = message.get("id")
    pending.message_id = message_id if isinstance(message_id, str) else ""
    request_id = d.get("requestId")
    pending.request_id = request_id if isinstance(request_id, str) else ""
    ts_raw = d.get("timestamp")
    pending.ts_raw = ts_raw if isinstance(ts_raw, str) else ""
    model = message.get("model")
    pending.model = model if isinstance(model, str) else ""
    pending.is_synthetic = pending.model == "<synthetic>" or bool(d.get("isApiErrorMessage"))
    if pending.is_synthetic:
        # Usage-limits addition (see module docstring): classify the
        # synthetic text and, for a usage-cap hit, its reset clause.
        text = _synthetic_text(message.get("content"))
        pending.synthetic_kind = events_mod.classify_synthetic_text(text)
        if pending.synthetic_kind in ("session_limit", "weekly_limit"):
            minutes_of_day, reset_tz = events_mod.parse_limit_reset_clause(text)
            pending.reset_minutes_of_day = minutes_of_day
            pending.reset_tz = reset_tz
            pending.reset_ts = _limit_reset_ts(d, _parse_ts(pending.ts_raw), minutes_of_day, reset_tz)
    effort = d.get("effort")
    pending.effort = effort if isinstance(effort, str) else None
    per_turn_effort = d.get("perTurnEffort")
    pending.per_turn_effort = per_turn_effort if isinstance(per_turn_effort, str) else None
    for attr, key in (
        ("attribution_mcp_server", "attributionMcpServer"),
        ("attribution_mcp_tool", "attributionMcpTool"),
        ("attribution_skill", "attributionSkill"),
    ):
        value = d.get(key)
        setattr(pending, attr, value if isinstance(value, str) else None)

    if isinstance(usage, dict):
        _apply_usage(pending, usage)

    _merge_content_blocks(pending, message.get("content"), tool_use_names, d.get("cwd"), blocked_calls)
    _merge_stop_reason(pending, message)
    return pending


def _apply_usage(pending: _PendingTurn, usage: dict) -> None:
    """Set every usage-derived field of ``pending`` from one line's
    ``usage`` object, replacing whatever an earlier line of the same
    message set. Every field is assigned (not just the ones present), so a
    later, more complete snapshot fully supersedes an earlier partial one.
    """
    pending.has_usage = True
    pending.input_tokens = int(usage.get("input_tokens") or 0)
    pending.cache_creation_tokens = int(usage.get("cache_creation_input_tokens") or 0)
    pending.cache_read_tokens = int(usage.get("cache_read_input_tokens") or 0)
    pending.output_tokens = int(usage.get("output_tokens") or 0)
    service_tier = usage.get("service_tier")
    pending.service_tier = service_tier if isinstance(service_tier, str) else None
    inference_geo = usage.get("inference_geo")
    pending.inference_geo = inference_geo if isinstance(inference_geo, str) else None
    speed = usage.get("speed")
    pending.speed = speed if isinstance(speed, str) else None
    details = usage.get("output_tokens_details")
    pending.thinking_tokens = int(details.get("thinking_tokens") or 0) if isinstance(details, dict) else 0
    server_tool_use = usage.get("server_tool_use")
    if isinstance(server_tool_use, dict):
        pending.web_search_requests = int(server_tool_use.get("web_search_requests") or 0)
        pending.web_fetch_requests = int(server_tool_use.get("web_fetch_requests") or 0)
    else:
        pending.web_search_requests = 0
        pending.web_fetch_requests = 0
    pending.ttl_sum_mismatch = False
    pending.ttl_split_unknown = False
    cache_creation = usage.get("cache_creation")
    if isinstance(cache_creation, dict):
        pending.cc_5m = int(cache_creation.get("ephemeral_5m_input_tokens") or 0)
        pending.cc_1h = int(cache_creation.get("ephemeral_1h_input_tokens") or 0)

        # Reconcile the flat cache_creation_input_tokens field against
        # the 5m/1h split: some usage payloads under-report the flat
        # field relative to its own ephemeral breakdown. The mismatch
        # is always recorded (even when nothing needs correcting, e.g.
        # the flat field is *larger* than the split); ctx (computed
        # from cache_creation_tokens in _finalize_turn) picks up the
        # corrected value automatically.
        ttl_sum = pending.cc_5m + pending.cc_1h
        if ttl_sum != pending.cache_creation_tokens:
            pending.ttl_sum_mismatch = True
        if pending.cache_creation_tokens < ttl_sum:
            pending.cache_creation_tokens = ttl_sum
    else:
        # Coordinator follow-up (WP12a diversity fixtures): older,
        # pre-5m/1h-split Claude Code JSONL has no nested
        # cache_creation object at all. cc_5m/cc_1h stay 0 (the split
        # was never recorded, not that nothing was written) and the
        # flat cache_creation_input_tokens is kept as the write total
        # unchanged - this is a format difference, not a sum mismatch,
        # so it must never set ttl_sum_mismatch (0 != a nonzero flat
        # value is not evidence of anything broken here).
        pending.cc_5m = 0
        pending.cc_1h = 0
        pending.ttl_split_unknown = True


def _merge_stop_reason(pending: _PendingTurn, message) -> None:
    """Quality-signals addition: keep the last non-null ``stop_reason``
    across the lines of one message (streamed lines carry null until the
    final one)."""
    stop_reason = message.get("stop_reason") if isinstance(message, dict) else None
    if isinstance(stop_reason, str) and stop_reason:
        pending.stop_reason = stop_reason[:32]


def _merge_into_pending(
    pending: _PendingTurn, d: dict, tool_use_names: dict[str, str], blocked_calls: dict[int, str] | None = None
) -> None:
    if not pending.is_synthetic:
        message = d.get("message")
        model = message.get("model") if isinstance(message, dict) else None
        if model == "<synthetic>" or d.get("isApiErrorMessage"):
            pending.is_synthetic = True
    message = d.get("message")
    # A streamed reply writes one line per content block, each carrying a
    # usage snapshot taken so far: the first line's output_tokens is a
    # partial count (often single digits) and only the last line carries
    # output_tokens_details. Keep the most complete snapshot seen.
    usage = message.get("usage") if isinstance(message, dict) else None
    if isinstance(usage, dict) and (
        not pending.has_usage or int(usage.get("output_tokens") or 0) >= pending.output_tokens
    ):
        _apply_usage(pending, usage)
    content = message.get("content") if isinstance(message, dict) else None
    _merge_content_blocks(pending, content, tool_use_names, d.get("cwd"), blocked_calls)
    _merge_stop_reason(pending, message)


def _tool_result_length(content, unsized_blocks: dict[str, int]) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    total += len(text)
                continue
            # Parser-signals addition (SURV-7, see events.py's module
            # docstring): an image/document block nested in a tool_result's
            # own content, sized by the same documented token rule as a
            # top-level human-prompt image -- previously silently counted
            # as 0 chars (plan finding S4: 1,207 blocks).
            chars, block_type = events_mod.content_block_size(block)
            if block_type is None:
                continue
            if chars is not None:
                total += chars
            else:
                unsized_blocks[block_type] = unsized_blocks.get(block_type, 0) + 1
        return total
    return 0


#: How much of an error's text :func:`_tool_error_kind` reads.
_ERROR_TEXT_CHARS = 600
#: A hook or a Claude Code guard stopped the call before it ran.
_ERROR_BLOCKED_RE = re.compile(
    r"^(?:\w+:\w+ hook error|<tool_use_error>Blocked:|This agent is isolated in the worktree)"
    r"|blocked by (?:a |the )?hook",
    re.IGNORECASE,
)
#: You, or the permission classifier, said no.
_ERROR_DENIED_RE = re.compile(
    r"^(?:The user doesn't want to (?:proceed|take this action)|Permission to use |Permission for this action was denied)"
)
#: A command that exited non-zero.
_ERROR_EXIT_RE = re.compile(r"^Exit code \d+")
#: In a non-zero exit's output: the command itself was wrong (a path that
#: isn't there, bad quoting, an unknown option, a one-off script that
#: crashed), not a test or build that failed.
_ERROR_COMMAND_WRONG_RE = re.compile(
    r"unexpected EOF|syntax error|No such file or directory|command not found|cannot access|can't read"
    r"|is not recognized as|Cannot find path|unknown option|unknown revision|ambiguous argument"
    r"|cannot change to|not a git repository|File \"<(?:stdin|string)>\"",
    re.IGNORECASE,
)


#: A hook's block, as Claude Code words it: ``PreToolUse:Read hook error:
#: [<command>]: <message>``. Group 1 is the hook's command.
_HOOK_BLOCK_RE = re.compile(r"^\w+:[\w.-]+ hook error: \[(.+?)\]: ")
#: A hook script's file name inside its command: the label a hook is
#: shown by. The character class stops at path separators, so a
#: directory never comes with it.
_HOOK_SCRIPT_RE = re.compile(
    r"([A-Za-z0-9_][A-Za-z0-9_.-]{0,79}\.(?:ps1|psm1|py|sh|bash|zsh|js|mjs|cjs|ts|cmd|bat|rb|pl|php))(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
#: A hook that failed because its script or program wasn't there.
_HOOK_NOT_FOUND_RE = re.compile(
    r"does not exist|No such file|can't open file|cannot find|is not recognized as|command not found",
    re.IGNORECASE,
)
_HOOK_TIMEOUT_RE = re.compile(r"timed? ?out", re.IGNORECASE)
#: A Windows ``%VAR%`` variable, which neither shell Claude Code runs
#: hooks in (bash or PowerShell) expands.
_HOOK_UNEXPANDED_RE = re.compile(r"%[A-Za-z_][A-Za-z0-9_]*%")
#: Hook events whose plain (non-JSON) stdout Claude Code adds as context.
_HOOK_PLAIN_STDOUT_CONTEXT_EVENTS = frozenset({"SessionStart", "UserPromptSubmit"})


def _hook_label(command) -> str | None:
    """The label a hook is shown by: its script's file name when the
    command names one, else the command's first 40 characters with paths
    redacted (the ``cmd_prefix`` rule). ``None`` for no command."""
    if not isinstance(command, str) or not command.strip():
        return None
    capped = _cap_command_for_redaction(command)
    script = _HOOK_SCRIPT_RE.search(capped)
    if script is not None:
        return script.group(1)
    return _redact_paths(_escape_newlines(capped))[:_CMD_PREFIX_MAX_CHARS].strip() or None


def _hook_script_is_relative(command: str) -> bool:
    """Whether ``command`` names its script by a relative path
    (``.claude/hooks/guard.ps1``), which only resolves when Claude Code
    runs the hook from the project root. A bare file name, an absolute
    path, or one starting with a variable (``$CLAUDE_PROJECT_DIR``,
    ``%USERPROFILE%``) or ``~`` is not. Inside quotes the path starts at
    the opening quote, so a space or tab in it (a settings.json path
    whose backslash-t was read as a tab) doesn't split it."""
    capped = _cap_command_for_redaction(command)
    script = _HOOK_SCRIPT_RE.search(capped)
    if script is None:
        return False
    head = capped[: script.start(1)]
    quoted = [q for q in "\"'" if head.count(q) % 2]
    if quoted:
        prefix = head[head.rfind(quoted[0]) + 1 :]
    else:
        prefix = head[max(head.rfind(c) for c in " \t\"'=") + 1 :]
    if not prefix:
        return False
    return not (prefix[0] in "/\\~$%" or re.match(r"[A-Za-z]:", prefix))


def _hook_error_cause(attachment: dict) -> str:
    """Why a hook failed, from its stderr and exit code, read here and
    dropped: ``not-found``, ``timeout`` or ``failed``."""
    stderr = attachment.get("stderr")
    text = stderr[:_ERROR_TEXT_CHARS] if isinstance(stderr, str) else ""
    if str(attachment.get("exitCode")) == "127" or _HOOK_NOT_FOUND_RE.search(text):
        return "not-found"
    if _HOOK_TIMEOUT_RE.search(text):
        return "timeout"
    return "failed"


def _annotate_hook_event(event: Event, attachment, context_queue: dict[tuple[str, str], list[str]]) -> None:
    """Add the hook's label (and, for an error, why it failed) to a hook
    event's ``detail``. Context a hook adds arrives as its own attachment
    with no command, so a run whose output carried context queues its
    label under its tool call (for a tool hook) and hook event, and the
    context takes it from there; context no run of yours produced is
    Claude Code's own (``built-in``). A session-level hook's run and its
    context carry different ids and names (``SessionStart:clear`` and a
    run id, against ``SessionStart`` twice), so only the event is kept."""
    if not isinstance(attachment, dict):
        return
    tool_use_id = attachment.get("toolUseID")
    key = (
        tool_use_id if isinstance(tool_use_id, str) and tool_use_id.startswith("toolu_") else "",
        events_mod.hook_event_name(attachment),
    )
    if event.subkind in ("hook_additional_context", "capture_note", "coaching_note"):
        queued = context_queue.get(key)
        label = queued.pop(0) if queued else None
        if event.subkind == "hook_additional_context":
            event.detail["script"] = label or "built-in"
        return
    command = attachment.get("command")
    blocking = attachment.get("blockingError")
    if command is None and isinstance(blocking, dict):
        # A Stop hook's block keeps its command inside ``blockingError``.
        command = blocking.get("command")
    label = _hook_label(command)
    if label is None:
        return
    event.detail["script"] = label
    if _hook_script_is_relative(command):
        event.detail["relative"] = True
    if _HOOK_UNEXPANDED_RE.search(command):
        event.detail["unexpanded"] = True
    if event.subkind == "hook_non_blocking_error":
        event.detail["cause"] = _hook_error_cause(attachment)
    stdout = attachment.get("stdout")
    if event.subkind == "hook_success" and isinstance(stdout, str) and stdout.strip():
        plain = not stdout.lstrip().startswith("{")
        if "additionalContext" in stdout or (plain and event.detail.get("hookName") in _HOOK_PLAIN_STDOUT_CONTEXT_EVENTS):
            context_queue.setdefault(key, []).append(label)


def _hook_block_label(content) -> str | None:
    """The label of the hook that blocked a tool call, from the start of
    the blocked result's text (read here and dropped), or ``None`` when a
    hook of yours didn't block it."""
    if isinstance(content, list):
        content = "".join(
            b.get("text", "") for b in content if isinstance(b, dict) and isinstance(b.get("text"), str)
        )
    if not isinstance(content, str):
        return None
    match = _HOOK_BLOCK_RE.match(content.strip()[:_ERROR_TEXT_CHARS])
    return _hook_label(match.group(1)) if match else None


def _tool_error_kind(content) -> str:
    """Why an erroring tool_result failed, from the start of its text:
    ``blocked``, ``denied``, ``failed`` or ``misfire`` (see model.py's
    ``Turn.tool_errors_by_kind``). The text is read here and dropped."""
    if isinstance(content, list):
        content = "".join(
            b.get("text", "") for b in content if isinstance(b, dict) and isinstance(b.get("text"), str)
        )
    text = content.strip()[:_ERROR_TEXT_CHARS] if isinstance(content, str) else ""
    if _ERROR_BLOCKED_RE.search(text.split("\n", 1)[0]):
        return "blocked"
    if _ERROR_DENIED_RE.match(text):
        return "denied"
    if _ERROR_EXIT_RE.match(text):
        return "misfire" if _ERROR_COMMAND_WRONG_RE.search(text) else "failed"
    if text.startswith("Command timed out"):
        return "failed"
    return "misfire"


def _accumulate_tool_results(
    d: dict,
    tool_use_names: dict[str, str],
    tool_result_chars: dict[str, int],
    tool_result_calls: dict[str, int],
    unsized_blocks: dict[str, int],
    current: _PendingTurn | None = None,
    blocked_calls: dict[int, str] | None = None,
) -> None:
    message = d.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return
    #: A1 addition: this tool_result line's own timestamp, recorded on
    #: ``current`` only for the tool_use_ids that are actually its own
    #: (see model.py's ``Turn.tool_wait_s``/``tool_result_chars_by_tool``
    #: docstrings) -- a tool_result can answer a tool_use from an earlier
    #: turn, which must not pollute this turn's own timing/composition.
    ts_raw = d.get("timestamp")
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        tool_use_id = block.get("tool_use_id")
        name = tool_use_names.pop(tool_use_id, None) if isinstance(tool_use_id, str) else None
        if name is None:
            continue
        length = _tool_result_length(block.get("content"), unsized_blocks)
        tool_result_chars[name] = tool_result_chars.get(name, 0) + length
        tool_result_calls[name] = tool_result_calls.get(name, 0) + 1
        if (
            current is not None
            and isinstance(tool_use_id, str)
            and tool_use_id in current.tool_use_ids
        ):
            current.tool_result_chars_by_tool[name] = (
                current.tool_result_chars_by_tool.get(name, 0) + length
            )
            if isinstance(ts_raw, str) and ts_raw:
                current.tool_result_ts_values.append(ts_raw)
            # Wasted-turns addition (see model.py's ``Turn.
            # tool_error_count``/``tool_error_chars`` docstrings): a
            # tool_result answering this turn's own tool_use flagged
            # ``is_error: true`` -- length only, never the error content.
            if block.get("is_error") is True:
                current.tool_error_count += 1
                current.tool_error_chars += length
                current.tool_errors_by_tool[name] = current.tool_errors_by_tool.get(name, 0) + 1
                kind = _tool_error_kind(block.get("content"))
                current.tool_errors_by_kind[kind] = current.tool_errors_by_kind.get(kind, 0) + 1
                # Your-hooks addition: which of your hooks blocked it, and
                # the call's key, so an unchanged re-send can be spotted.
                blocked_by = _hook_block_label(block.get("content")) if kind == "blocked" else None
                if blocked_by is not None:
                    current.hook_blocks[blocked_by] = current.hook_blocks.get(blocked_by, 0) + 1
                    call_key = current.call_keys.get(tool_use_id)
                    if call_key is not None and blocked_calls is not None:
                        blocked_calls[call_key] = blocked_by
                # A failed edit changed nothing, so it isn't an edit. A shell
                # command that ran and failed may still have written its
                # files; one that was blocked or denied didn't.
                edited = current.edit_hashes_by_tool_use.pop(tool_use_id, None)
                if edited and (name not in _SHELL_TOOL_NAMES or kind in _SHELL_NOT_RUN_KINDS):
                    for hashed in edited:
                        current.edit_target_hashes.remove(hashed)
                # SEC-P3: a Skill call that errored never happened as far
                # as "known skills" is concerned -- take back its
                # provisional name so it can't self-authorise this same
                # turn's own tag claim.
                current.skill_calls_by_tool_use.pop(tool_use_id, None)
            # Metrics-capture addition: the report a synchronous agent
            # handed back (a background agent's launch message is not its
            # report; that arrives later as a task notification), and
            # whether a plan was approved.
            if name in _AGENT_TOOL_NAMES and not _is_async_launch(d):
                current.agent_result_chars[tool_use_id] = current.agent_result_chars.get(tool_use_id, 0) + length
            elif name == "ExitPlanMode" and current.plan_stats is not None:
                current.plan_stats.outcome = "rejected" if block.get("is_error") is True else "approved"
            elif name == "AskUserQuestion" and tool_use_id in current.feedback_asks:
                # Feedback addition: the answers, matched to known labels;
                # a declined call answers nothing.
                if block.get("is_error") is True:
                    current.feedback = Feedback(source="skipped")
                else:
                    answered = capture_tags.feedback_from_answers(d.get("toolUseResult"))
                    if answered is not None and current.feedback is not None and current.feedback.source == answered.source:
                        answered = capture_tags.merge_feedback(current.feedback, answered)
                    current.feedback = answered or current.feedback


def _is_async_launch(d: dict) -> bool:
    """Whether a tool_result line is a background agent's launch message
    rather than its report."""
    result = d.get("toolUseResult")
    return isinstance(result, dict) and (result.get("isAsync") is True or result.get("status") == "async_launched")


def _resolve_preceding_tool(previous_turn: Turn | None) -> tuple[str, str | None]:
    if previous_turn is None:
        return "n/a", None
    if not previous_turn.tool_names:
        return "none", previous_turn.cmd_prefix
    if "Bash" in previous_turn.tool_names:
        return "Bash", previous_turn.cmd_prefix
    if "PowerShell" in previous_turn.tool_names:
        return "PowerShell", previous_turn.cmd_prefix
    return previous_turn.tool_names[0], previous_turn.cmd_prefix


def _finalize_turn(
    pending: _PendingTurn,
    pending_events: list[Event],
    pending_attachment_types: list[str],
    previous_turn: Turn | None,
    previous_non_synthetic_ts: datetime | None,
    priced_turn_count: int,
    diagnostics: Diagnostics,
    next_ts_raw: str | None = None,
    skill_names: set[str] | None = None,
) -> tuple[Turn, datetime | None, int]:
    ts_dt = _parse_ts(pending.ts_raw)
    ctx = pending.input_tokens + pending.cache_creation_tokens + pending.cache_read_tokens

    if pending.has_usage:
        if pending.ttl_sum_mismatch:
            diagnostics.ttl_sum_mismatch += 1
        if pending.ttl_split_unknown:
            diagnostics.pre_split_turns += 1
    else:
        diagnostics.turns_missing_usage += 1

    if pending.is_synthetic:
        diagnostics.synthetic_turns += 1

    turn_index = 0
    gap_s: float | None = None
    new_prev_ts = previous_non_synthetic_ts
    new_priced_count = priced_turn_count
    if not pending.is_synthetic and pending.has_usage:
        new_priced_count = priced_turn_count + 1
        turn_index = new_priced_count
        if pending.ts_raw and ts_dt is None:
            diagnostics.timestamp_parse_failures += 1
        if previous_non_synthetic_ts is not None and ts_dt is not None:
            gap_s = (ts_dt - previous_non_synthetic_ts).total_seconds()
        if ts_dt is not None:
            new_prev_ts = ts_dt

    edit_kind: str | None
    if pending.edit_real_found:
        edit_kind = "real"
    elif pending.edit_scratch_found:
        edit_kind = "scratch"
    else:
        edit_kind = None

    preceding_tool, preceding_cmd_prefix = _resolve_preceding_tool(previous_turn)
    preceding_primary = events_mod.primary_kind(pending_events)

    # A1: timing either side of this turn's own tool calls, from the
    # tool_result timestamp(s) ``_accumulate_tool_results`` recorded onto
    # this pending turn (see model.py's ``Turn.tool_wait_s``/
    # ``model_latency_s`` docstrings). Both stay None when this turn made
    # no tool calls, or a timestamp is missing/unparsable.
    tool_wait_s: float | None = None
    model_latency_s: float | None = None
    if pending.tool_result_ts_values:
        parsed_result_ts = [
            parsed for parsed in (_parse_ts(raw) for raw in pending.tool_result_ts_values) if parsed is not None
        ]
        if parsed_result_ts:
            max_tool_result_ts = max(parsed_result_ts)
            if ts_dt is not None:
                tool_wait_s = (max_tool_result_ts - ts_dt).total_seconds()
            next_ts_dt = _parse_ts(next_ts_raw) if next_ts_raw else None
            if next_ts_dt is not None:
                model_latency_s = (next_ts_dt - max_tool_result_ts).total_seconds()

    # A4: human-prompt size/paste-flag, from any HUMAN_TEXT event(s) that
    # preceded this turn (see model.py's ``Turn.human_prompt_chars``/
    # ``human_prompt_has_paste`` docstrings).
    human_prompt_chars: int | None = None
    human_prompt_has_paste = False
    human_correction = False
    retry_marker: str | None = None
    # Metrics-capture addition (see model.py's module docstring).
    spawn_marker: str | None = None
    flags: set[str] = set()
    cap_note_chars = 0
    hook_context_chars: dict[str, int] = {}
    commands_run: list[str] = []
    for pending_event in pending_events:
        if pending_event.kind == EventKind.HOOK_OUTPUT and pending_event.subkind in ("capture_note", "coaching_note"):
            # A coaching note is ClaudeGlass's too: it counts to the hook's
            # context, never to capture's note count (cap_injections).
            cap_note_chars += (pending_event.size_chars or 0) + (pending_event.detail.get("coach_chars") or 0)
            continue
        if pending_event.kind == EventKind.HOOK_OUTPUT and pending_event.subkind == "hook_additional_context":
            label = pending_event.detail.get("script") or "built-in"
            hook_context_chars[label] = hook_context_chars.get(label, 0) + (pending_event.size_chars or 0)
            continue
        if pending_event.kind == EventKind.SLASH_COMMAND:
            command = pending_event.detail.get("command")
            if isinstance(command, str) and command:
                commands_run.append(command)
            continue
        if pending_event.kind != EventKind.HUMAN_TEXT:
            continue
        chars = pending_event.size_chars or 0
        human_prompt_chars = chars if human_prompt_chars is None else human_prompt_chars + chars
        # A skill you ran with a slash is your message too (see events.py).
        command = pending_event.detail.get("command")
        if isinstance(command, str) and command:
            commands_run.append(command)
        if pending_event.detail.get("has_paste"):
            human_prompt_has_paste = True
        if pending_event.detail.get("correction"):
            human_correction = True
        retry_marker = pending_event.detail.get("retry") or retry_marker
        spawn_marker = pending_event.detail.get("spawn") or spawn_marker
        flags.update(pending_event.detail.get("flags") or ())

    cap: CaptureTag | None = None
    result_marker: str | None = None
    feedback = pending.feedback
    if pending.last_text_tail:
        known_skills = set(skill_names or ()) | set(pending.skill_calls_by_tool_use.values())
        cap, result_marker = capture_tags.parse_reply_tags(pending.last_text_tail, known_skills)
        # SEC-P1: the answers to /tl-feedback's own question (or a
        # declined question) beat a `[tl-fb: ...]` tag -- Claude could
        # forge that tag in any reply, but not the AskUserQuestion call
        # its answers are read from.
        if feedback is None:
            feedback = capture_tags.parse_feedback_tag(pending.last_text_tail)

    # Usage-limits addition (see module docstring): a limit-hit/resume
    # among the events preceding this turn means the gap to the previous
    # turn was (at least in part) a usage-cap pause, not idle time.
    gap_cause = (
        "limit"
        if any(pending_event.kind in _LIMIT_GAP_KINDS for pending_event in pending_events)
        else None
    )

    turn = Turn(
        message_id=pending.message_id,
        request_id=pending.request_id,
        turn_index=turn_index,
        ts=pending.ts_raw,
        gap_s=gap_s,
        model=pending.model,
        service_tier=pending.service_tier,
        is_synthetic=pending.is_synthetic,
        effort=pending.effort,
        per_turn_effort=pending.per_turn_effort,
        input_tokens=pending.input_tokens,
        cache_creation_tokens=pending.cache_creation_tokens,
        cache_read_tokens=pending.cache_read_tokens,
        output_tokens=pending.output_tokens,
        thinking_tokens=pending.thinking_tokens,
        web_search_requests=pending.web_search_requests,
        web_fetch_requests=pending.web_fetch_requests,
        cc_5m=pending.cc_5m,
        cc_1h=pending.cc_1h,
        ttl_split_unknown=pending.ttl_split_unknown,
        ctx=ctx,
        tool_names=tuple(pending.tool_names),
        tool_use_ids=tuple(pending.tool_use_ids),
        cmd_prefix=pending.cmd_prefix,
        edit_kind=edit_kind,
        attribution_mcp_server=pending.attribution_mcp_server,
        attribution_mcp_tool=pending.attribution_mcp_tool,
        attribution_skill=pending.attribution_skill,
        preceding_tool=preceding_tool,
        preceding_cmd_prefix=preceding_cmd_prefix,
        preceding_event_kinds=tuple(event.kind for event in pending_events),
        preceding_attachment_types=tuple(pending_attachment_types),
        preceding_primary=preceding_primary,
        inference_geo=pending.inference_geo,
        tool_wait_s=tool_wait_s,
        model_latency_s=model_latency_s,
        tool_result_chars_by_tool=dict(pending.tool_result_chars_by_tool),
        agent_brief_chars=pending.agent_brief_chars,
        tool_input_chars_by_tool=dict(pending.tool_input_chars_by_tool),
        read_target_hashes=tuple(pending.read_target_hashes),
        human_prompt_chars=human_prompt_chars,
        human_prompt_has_paste=human_prompt_has_paste,
        synthetic_kind=pending.synthetic_kind,
        gap_cause=gap_cause,
        tool_error_count=pending.tool_error_count,
        tool_error_chars=pending.tool_error_chars,
        skills_invoked=tuple(pending.skill_calls_by_tool_use.values()),
        stop_reason=pending.stop_reason,
        tool_calls_by_tool=dict(pending.tool_calls_by_tool),
        tool_errors_by_tool=dict(pending.tool_errors_by_tool),
        tool_errors_by_kind=dict(pending.tool_errors_by_kind),
        edit_target_hashes=tuple(pending.edit_target_hashes),
        human_correction=human_correction,
        speed=pending.speed,
        retry_marker=retry_marker,
        result_marker=result_marker,
        cap=cap,
        cap_note_chars=cap_note_chars,
        spawn_marker=spawn_marker,
        agent_result_chars=dict(pending.agent_result_chars),
        prompt_flags=tuple(flag for flag in PROMPT_FLAGS if flag in flags),
        plan_stats=pending.plan_stats,
        commands_run=tuple(commands_run),
        feedback=feedback,
        hook_context_chars=hook_context_chars,
        hook_blocks=dict(pending.hook_blocks),
        hook_resends=dict(pending.hook_resends),
        deferred_tools_by_server=dict(pending.deferred_tools_by_server),
        deferred_list_chars=pending.deferred_list_chars,
    )
    return turn, new_prev_ts, new_priced_count


#: A session id safe to use as a file name: Claude Code's are UUIDs. A
#: ``sessionId`` with a path separator or a dot is never looked up.
_SESSION_FILE_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _has_own_file(folder: Path, session_id: str) -> bool:
    """Whether ``session_id``'s own transcript sits in ``folder``, so a
    copy of its lines in another transcript can be skipped (see the
    module docstring's copied sessions)."""
    if not _SESSION_FILE_RE.match(session_id):
        return False
    try:
        return (folder / f"{session_id}.jsonl").is_file()
    except OSError:
        return False


#: Cost record (see ``model.py``'s module docstring): a model id as Claude
#: Code's ``cost-state`` names it; anything else is dropped.
_MODEL_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\[\]-]{0,63}")


def _cost_by_model(model_usage) -> dict[str, float]:
    """``cost-state``'s ``modelUsage`` as model id -> ``costUSD``."""
    out: dict[str, float] = {}
    if not isinstance(model_usage, dict):
        return out
    for model, usage in model_usage.items():
        cost = usage.get("costUSD") if isinstance(usage, dict) else None
        if (
            isinstance(model, str)
            and _MODEL_ID_RE.fullmatch(model)
            and isinstance(cost, (int, float))
            and not isinstance(cost, bool)
            and cost >= 0
        ):
            out[model] = float(cost)
    return out


#: Compaction calls (see module docstring): the cache lifetimes a write
#: can carry, in seconds.
_ONE_HOUR_S = 3600.0
_FIVE_MINUTES_S = 300.0


def _utc(ts_raw: str | None) -> datetime | None:
    """``_parse_ts``, read as UTC when the text carries no zone."""
    parsed = _parse_ts(ts_raw or "")
    if parsed is not None and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _count(value) -> int | None:
    """A token count from ``compactMetadata``, or ``None``."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _cache_lifetime_s(replies: list[Turn]) -> float:
    """The lifetime of the latest cache write among ``replies``, newest
    first; five minutes when none of them recorded one."""
    for reply in reversed(replies):
        if reply.cc_1h:
            return _ONE_HOUR_S
        if reply.cc_5m:
            return _FIVE_MINUTES_S
    return _FIVE_MINUTES_S


def _compaction_turn(replies: list[Turn], event: Event) -> Turn | None:
    """The estimated request that wrote ``event``'s summary, sized from
    the last of ``replies`` (see the module docstring), or ``None`` when
    it can't be sized."""
    output = _count(event.post_tokens)
    if not replies or output is None:
        return None
    previous = replies[-1]
    prefix = previous.cache_read_tokens + previous.cache_creation_tokens
    pre = _count(event.pre_tokens)
    new = max(pre - prefix, 0) if pre is not None else 0

    previous_at = _utc(previous.ts)
    end = _utc(event.ts)
    started = None
    if end is not None:
        started = end.astimezone(timezone.utc) - timedelta(milliseconds=max(_count(event.duration_ms) or 0, 1))
        if previous_at is not None and started < previous_at:
            started = previous_at
    lifetime = _cache_lifetime_s(replies)
    warm = started is not None and previous_at is not None and (started - previous_at).total_seconds() <= lifetime
    read, write = (prefix, 0) if warm else (0, prefix)
    one_hour = write if lifetime == _ONE_HOUR_S else 0
    ts = started.strftime("%Y-%m-%dT%H:%M:%S.") + f"{started.microsecond // 1000:03d}Z" if started else previous.ts
    return Turn(
        message_id=f"compaction-{ts}",
        turn_index=1,
        ts=ts,
        model=previous.model,
        service_tier=previous.service_tier,
        is_synthetic=True,
        effort=previous.effort,
        input_tokens=new,
        cache_creation_tokens=write,
        cache_read_tokens=read,
        output_tokens=output,
        cc_5m=write - one_hour,
        cc_1h=one_hour,
        ctx=new + prefix,
        preceding_tool="n/a",
        inference_geo=previous.inference_geo,
        speed=previous.speed,
        estimated="compaction",
    )


def _with_compaction_calls(
    turns: list[Turn], compactions: list[tuple[int, Event]], diagnostics: Diagnostics
) -> list[Turn]:
    """``turns`` with each compaction's estimated request inserted after
    the reply before it, and ``turn_index`` renumbered over the priced
    turns (see the module docstring)."""
    if not compactions:
        return turns
    out: list[Turn] = []
    position = 0
    for before, event in compactions:
        out.extend(turns[position:before])
        position = max(position, before)
        replies = [turn for turn in out if turn.turn_index > 0 and not turn.estimated]
        estimate = _compaction_turn(replies, event)
        if estimate is None:
            diagnostics.compaction_calls_unsized += 1
            continue
        out.append(estimate)
        diagnostics.compaction_calls += 1
    out.extend(turns[position:])
    index = 0
    for i, turn in enumerate(out):
        if turn.turn_index > 0:
            index += 1
            if turn.turn_index != index:
                out[i] = replace(turn, turn_index=index)
    return out


def parse_transcript(path: str | Path, meta: TranscriptMeta) -> TranscriptResult:
    """Parse one transcript JSONL file in a single streaming pass.

    ``meta`` is provenance the caller already knows (from
    ``discovery.py``) — this function fills in ``turns``, ``events``,
    ``diagnostics``, ``tool_result_chars``, ``tool_result_calls`` and
    ``parser_notes`` around it; it never mutates ``meta`` (see module
    docstring for the ``claude_version``/``entrypoint``/``provider``/
    ``cc_cost_usd``/``cc_cost_has_unknown_model`` derivation this
    function's *returned* meta copy adds on top).
    """
    line_stats = jsonl.LineStats()
    diagnostics = Diagnostics()
    turns: list[Turn] = []
    events: list[Event] = []
    tool_result_chars: dict[str, int] = {}
    tool_result_calls: dict[str, int] = {}
    #: tool_use_id -> tool name, for attributing tool_result lengths.
    #: Deliberately not scoped to the current turn: a tool_result can
    #: reference a tool_use from an earlier turn.
    tool_use_names: dict[str, str] = {}
    #: Parser-signals addition (SURV-6/7, see model.py's module
    #: docstring): counters for ``TranscriptResult.parser_notes``, kept
    #: apart from ``Diagnostics`` (off limits this phase). ``unsized_blocks``
    #: is shared by the tool_result path (``_tool_result_length``, below)
    #: and the human-prompt path (a HUMAN_TEXT event's own
    #: ``detail["unsized_blocks"]``, folded in once that event is built).
    unsized_blocks: dict[str, int] = {}
    unknown_line_types: dict[str, int] = {}
    #: Parser-signals addition (SURV-5): the last ``cost-state`` line's
    #: own ``totalCostUSD``/``hasUnknownModelCost`` (a running total, so
    #: the last one seen in file order is the most complete).
    cc_cost_usd: float | None = None
    cc_cost_has_unknown_model = False
    #: Cost record: the latest line time seen so far, and that time and
    #: the cost by model as of the last ``cost-state`` line.
    latest_at: datetime | None = None
    latest_ts: str | None = None
    cc_cost_as_of: str | None = None
    cc_cost_by_model: dict[str, float] = {}
    #: Batch C addition: first non-empty ``entrypoint``/``version`` field
    #: seen on any raw line, in file order. Every line type carries these
    #: (when present), not just assistant lines.
    first_entrypoint: str | None = None
    first_claude_version: str | None = None
    #: Batch C addition: provider derived from the first turn with a
    #: model, overriding whatever ``meta.provider`` already held (see
    #: ``detect_provider``/module docstring).
    provider = meta.provider

    #: Events attached to the turn currently being accumulated in
    #: ``current`` (i.e. observed *before* ``current`` started): what
    #: ``current`` finalises with. ``*_since_current`` accumulates events
    #: seen *while* ``current`` is in progress — these precede the NEXT
    #: turn, not this one, and become ``*_for_current`` when that next
    #: turn starts (see the module docstring's two-buffer fix).
    events_for_current: list[Event] = []
    attachments_for_current: list[str] = []
    events_since_current: list[Event] = []
    attachments_since_current: list[str] = []
    finalized_keys: set[str] = set()
    #: Lines already processed once, by ``uuid`` — a rewind/resume can
    #: replay a whole block of user/attachment/system (and, rarely,
    #: assistant) lines verbatim with the same uuid; the replay is
    #: skipped outright and counted, not reprocessed as new activity.
    #: ``queue-operation``/``bridge-session`` lines carry no uuid, so they
    #: fall through this check untouched (guarded by the ``isinstance``/
    #: truthiness check below) rather than being treated as replays.
    seen_uuids: set[str] = set()
    #: Metrics-capture addition: skill names this transcript listed
    #: (``skill_listing`` attachments' ``names``) or used, in memory only:
    #: a capture tag's ``skill=would-help:<name>`` keeps a name only if it
    #: is one of these.
    skill_names: set[str] = set()
    #: Your-hooks addition, in memory only: the key of each tool call a
    #: hook of yours blocked -> that hook's label (see
    #: ``_merge_content_blocks``), and the labels of hook runs whose
    #: output carried context, waiting for that context's own attachment
    #: (see ``_annotate_hook_event``).
    blocked_calls: dict[int, str] = {}
    hook_context_queue: dict[tuple[str, str], list[str]] = {}
    #: Copied sessions (see module docstring): only a top-level
    #: transcript is named after its session. Other session id -> whether
    #: that session's own file is beside this one, looked up once each.
    own_session_id = meta.session_id if meta.kind == "top-level" else ""
    session_folder = Path(path).parent
    copied_from: dict[str, bool] = {}
    #: Tool search (see module docstring): the deferred list and the
    #: definitions loaded from it so far.
    deferred_tools = _DeferredTools()
    #: Compaction calls (see module docstring): each ``compact_boundary``
    #: event, with how many turns come before it in ``turns`` once the
    #: turn still open is finalised.
    compactions: list[tuple[int, Event]] = []

    current: _PendingTurn | None = None
    current_key: str | None = None
    previous_turn: Turn | None = None
    previous_non_synthetic_ts: datetime | None = None
    priced_turn_count = 0

    for _line_no, d in jsonl.iter_lines(path, stats=line_stats):
        if own_session_id:
            line_session = d.get("sessionId")
            if isinstance(line_session, str) and line_session and line_session != own_session_id:
                copied = copied_from.get(line_session)
                if copied is None:
                    copied = copied_from[line_session] = _has_own_file(session_folder, line_session)
                if copied:
                    diagnostics.copied_lines += 1
                    continue

        uuid_val = d.get("uuid")
        if isinstance(uuid_val, str) and uuid_val:
            if uuid_val in seen_uuids:
                diagnostics.replayed_lines += 1
                continue
            seen_uuids.add(uuid_val)

        line_type = d.get("type")

        line_ts = d.get("timestamp")
        if isinstance(line_ts, str) and line_ts:
            line_at = _utc(line_ts)
            if line_at is not None and (latest_at is None or line_at > latest_at):
                latest_at, latest_ts = line_at, line_ts

        if first_entrypoint is None:
            entrypoint_raw = d.get("entrypoint")
            if isinstance(entrypoint_raw, str) and entrypoint_raw:
                first_entrypoint = entrypoint_raw
        if first_claude_version is None:
            version_raw = d.get("version")
            if isinstance(version_raw, str) and version_raw:
                first_claude_version = version_raw

        if line_type == "assistant":
            diagnostics.assistant_lines += 1
            key = _turn_key(d)
            if current is not None and key == current_key:
                _merge_into_pending(current, d, tool_use_names, blocked_calls)
                continue
            if key in finalized_keys:
                diagnostics.late_duplicate_ids += 1
                continue
            if current is not None:
                turn, previous_non_synthetic_ts, priced_turn_count = _finalize_turn(
                    current,
                    events_for_current,
                    attachments_for_current,
                    previous_turn,
                    previous_non_synthetic_ts,
                    priced_turn_count,
                    diagnostics,
                    next_ts_raw=d.get("timestamp"),
                    skill_names=skill_names,
                )
                turns.append(turn)
                skill_names.update(turn.skills_invoked)
                finalized_keys.add(current_key)  # type: ignore[arg-type]
                previous_turn = turn
            # Rotate regardless of whether `current` was None: whatever
            # accumulated since it started (or, for the first turn,
            # since the file began) precedes the turn about to start.
            events_for_current = events_since_current
            attachments_for_current = attachments_since_current
            events_since_current = []
            attachments_since_current = []
            current = _new_pending(d, tool_use_names, blocked_calls)
            current.deferred_tools_by_server, current.deferred_list_chars = deferred_tools.snapshot()
            current_key = key
            # Usage-limits addition (see module docstring): a usage-cap
            # hit lives on the synthetic assistant line's own text, which
            # never reaches events.classify_line (assistant lines aren't
            # events) -- synthesise the LIMIT_HIT event here instead, and
            # route it through events_since_current so it precedes the
            # *next* turn per the two-buffer scheme, not this synthetic one.
            if current.synthetic_kind in ("session_limit", "weekly_limit"):
                limit_detail: dict = {}
                if current.reset_minutes_of_day is not None:
                    limit_detail["reset_minutes_of_day"] = current.reset_minutes_of_day
                if current.reset_tz is not None:
                    limit_detail["reset_tz"] = current.reset_tz
                if current.reset_ts is not None:
                    limit_detail["reset_ts"] = current.reset_ts
                limit_event = Event(
                    kind=EventKind.LIMIT_HIT,
                    subkind=current.synthetic_kind,
                    ts=current.ts_raw or None,
                    detail=limit_detail,
                )
                events.append(limit_event)
                events_since_current.append(limit_event)
                diagnostics.limit_hits += 1
            continue

        if line_type == "user":
            _accumulate_tool_results(
                d, tool_use_names, tool_result_chars, tool_result_calls, unsized_blocks, current, blocked_calls
            )
        elif line_type == "agent-setting":
            value = d.get("agentSetting")
            if isinstance(value, str) and value:
                diagnostics.agent_settings[value] = diagnostics.agent_settings.get(value, 0) + 1
        elif line_type == "mode":
            value = d.get("mode")
            if isinstance(value, str) and value:
                diagnostics.modes[value] = diagnostics.modes.get(value, 0) + 1
        elif line_type == "cost-state":
            # Parser-signals addition (SURV-5): numbers only, no OTel --
            # feeds reconcile.claude_code_reported_costs (see that
            # module's own docstring).
            cost_raw = d.get("totalCostUSD")
            if isinstance(cost_raw, (int, float)) and not isinstance(cost_raw, bool):
                cc_cost_usd = float(cost_raw)
                cc_cost_has_unknown_model = bool(d.get("hasUnknownModelCost"))
                cc_cost_as_of = latest_ts
                cc_cost_by_model = _cost_by_model(d.get("modelUsage"))
        elif line_type == "attachment":
            attachment = d.get("attachment")
            if isinstance(attachment, dict) and attachment.get("type") == "skill_listing":
                names = attachment.get("names")
                if isinstance(names, list):
                    skill_names.update(name for name in names if isinstance(name, str))
            elif isinstance(attachment, dict):
                deferred_tools.note(attachment)

        event = events_mod.classify_line(d)
        if event is None:
            # P10b privacy fix: ignored_line_types is a Diagnostics dict
            # field, so its key -- like unknown_line_types's below -- must
            # be the sanitised type, never the raw (attacker-controlled)
            # ``type`` verbatim. This dict isn't walked by
            # tests/test_privacy.py's generic length check (see that
            # module's docstring), so an unsanitised key here would never
            # have been caught by it.
            safe_line_type = events_mod.sanitize_line_type(line_type)
            diagnostics.ignored_line_types[safe_line_type] = (
                diagnostics.ignored_line_types.get(safe_line_type, 0) + 1
            )
            continue
        if event.kind == EventKind.HOOK_OUTPUT and line_type == "attachment":
            _annotate_hook_event(event, d.get("attachment"), hook_context_queue)
        if event.kind == EventKind.COMPACT_BOUNDARY:
            compactions.append((len(turns) + (current is not None), event))
        events.append(event)
        events_since_current.append(event)
        if line_type == "attachment":
            attachments_since_current.append(event.subkind or "")
        if event.kind == EventKind.UNKNOWN:
            # Parser-signals addition (SURV-6, see model.py's module
            # docstring): apart from ignored_line_types above, which also
            # holds types the parser recognises and deliberately drops --
            # this is specifically a type classify_line had no rule for
            # at all. Sanitised: ``type`` is attacker-controlled input.
            sanitized_type = events_mod.sanitize_line_type(line_type)
            diagnostics.ignored_line_types[sanitized_type] = (
                diagnostics.ignored_line_types.get(sanitized_type, 0) + 1
            )
            unknown_line_types[sanitized_type] = unknown_line_types.get(sanitized_type, 0) + 1
        if event.kind == EventKind.HUMAN_TEXT:
            # Parser-signals addition (SURV-7): fold a human prompt's own
            # unsized image/document blocks into the same counter the
            # tool_result path (_tool_result_length) uses.
            for block_type, count in (event.detail.get("unsized_blocks") or {}).items():
                unsized_blocks[block_type] = unsized_blocks.get(block_type, 0) + count
        if event.kind == EventKind.ATTACHMENT:
            subkind = event.subkind or ""
            diagnostics.attachment_catch_all[subkind] = (
                diagnostics.attachment_catch_all.get(subkind, 0) + 1
            )
        # Usage-limits addition (see module docstring): LIMIT_RESUME and
        # AGENT_TERMINATED both reach here via events_mod.classify_line
        # (unlike LIMIT_HIT, synthesised above from a synthetic assistant
        # line).
        if event.kind == EventKind.LIMIT_RESUME:
            diagnostics.limit_resumes += 1
        elif event.kind == EventKind.AGENT_TERMINATED:
            diagnostics.agents_terminated += 1

    if current is not None:
        turn, previous_non_synthetic_ts, priced_turn_count = _finalize_turn(
            current,
            events_for_current,
            attachments_for_current,
            previous_turn,
            previous_non_synthetic_ts,
            priced_turn_count,
            diagnostics,
            skill_names=skill_names,
        )
        turns.append(turn)

    # Events observed after the last finalised turn's assistant line have
    # no later turn to attach to (see the module docstring).
    diagnostics.trailing_events = len(events_since_current)

    diagnostics.lines = line_stats.lines
    diagnostics.unparsable_lines = line_stats.unparsable_lines
    diagnostics.truncated_final_line = line_stats.truncated_final_line
    diagnostics.oversized_lines = line_stats.oversized_lines
    diagnostics.distinct_turns = len(turns)
    turns = _with_compaction_calls(turns, compactions, diagnostics)

    # Batch C addition: the transcript's own first turn with a model is
    # the authoritative provider signal (see module docstring), taking
    # precedence over whatever meta.provider already held.
    for turn in turns:
        if turn.model:
            detected = detect_provider(turn.model)
            if detected:
                provider = detected
            break

    # Metrics-capture addition: which capture notes this transcript saw.
    cap_version: int | None = None
    cap_codes: dict[str, None] = {}
    cap_injections = 0
    for event in events:
        if event.kind == EventKind.HOOK_OUTPUT and event.subkind == "capture_note":
            cap_injections += 1
            version = event.detail.get("v")
            if isinstance(version, int) and (cap_version is None or version > cap_version):
                cap_version = version
            cap_codes.update(dict.fromkeys(event.detail.get("codes") or ()))

    # SEC-P2: a turn's tag is trusted only for what a note this
    # transcript actually saw asked for -- empty when it never saw one
    # at all (capture_tags.filter_tag).
    requested = frozenset(cap_codes) if cap_injections else frozenset()
    subagent = meta.kind != "top-level"
    for i, turn in enumerate(turns):
        if turn.cap is None and turn.result_marker is None:
            continue
        cap, result_marker = capture_tags.filter_tag(turn.cap, turn.result_marker, requested=requested, subagent=subagent)
        if cap is not turn.cap or result_marker != turn.result_marker:
            turns[i] = replace(turn, cap=cap, result_marker=result_marker)

    final_meta = replace(
        meta,
        entrypoint=meta.entrypoint if meta.entrypoint is not None else first_entrypoint,
        claude_version=meta.claude_version if meta.claude_version is not None else first_claude_version,
        provider=provider,
        cap_version=cap_version,
        cap_metrics=tuple(cap_codes),
        cap_injections=cap_injections,
        cc_cost_usd=cc_cost_usd,
        cc_cost_has_unknown_model=cc_cost_has_unknown_model,
        cc_cost_as_of=cc_cost_as_of,
        cc_cost_by_model=cc_cost_by_model,
    )

    # Parser-signals addition (SURV-6/7): only present when non-empty, so
    # a transcript that saw neither carries no side-channel at all.
    parser_notes: dict[str, dict[str, int]] = {}
    if unknown_line_types:
        parser_notes["unknown_line_types"] = unknown_line_types
    if unsized_blocks:
        parser_notes["unsized_blocks"] = unsized_blocks

    return TranscriptResult(
        meta=final_meta,
        turns=turns,
        events=events,
        diagnostics=diagnostics,
        tool_result_chars=tool_result_chars,
        tool_result_calls=tool_result_calls,
        parser_notes=parser_notes,
        tool_definition_chars=dict(deferred_tools.definition_chars),
    )


#: Capture-improvements addition (A6): top-level keys this module actually
#: looks up, per raw line ``type``, kept alongside the parser so
#: ``probe.compare_with_parser`` can audit real transcripts for keys the
#: parser never reads without re-deriving the parser's own control flow.
#: Base keys are read for *every* line regardless of type: ``type``/
#: ``uuid`` (dedup, in ``parse_transcript``'s main loop) and
#: ``entrypoint``/``version`` (first-seen capture, also in the main loop,
#: before any type dispatch) and ``sessionId`` (copied sessions, first of
#: all) and ``timestamp`` (the cost record's cut-off, see ``model.py``).
#: A type not listed here (every other ``events._IGNORABLE_TYPES`` member,
#: plus the ``file-history-*``/``artifact-*`` prefix families) is read no
#: further than those six base keys -- ``classify_line`` returns ``None``
#: for them before even computing ``message``. ``cost-state`` is read in
#: the main loop itself, for its totals.
#:
#: ``user``/``system``/``attachment``/``queue-operation`` all reach
#: ``classify_line``, which unconditionally reads ``timestamp`` and
#: ``message`` (via ``_user_str_content``) before any type-specific
#: check -- except ``attachment``/``queue-operation``, which always
#: return via their own unconditional catch-all (checks 11 and 10) before
#: classify_line's later, type-unguarded ``origin`` read; ``system``
#: (when its ``subtype`` matches none of the earlier checks) and ``user``
#: can both fall through as far as that ``origin`` read, so both list it.
_BASE_READ_KEYS = frozenset({"type", "uuid", "entrypoint", "version", "sessionId", "timestamp"})

READ_KEYS: dict[str, frozenset[str]] = {
    "assistant": _BASE_READ_KEYS
    | frozenset(
        {
            "message",
            "requestId",
            "timestamp",
            "effort",
            "perTurnEffort",
            "attributionMcpServer",
            "attributionMcpTool",
            "attributionSkill",
            "isApiErrorMessage",
            "quotaLimits",
        }
    ),
    "user": _BASE_READ_KEYS
    | frozenset(
        {
            "timestamp",
            "message",
            "isCompactSummary",
            "toolDenialKind",
            "origin",
            "isMeta",
            "promptSource",
            "permissionMode",
        }
    ),
    "system": _BASE_READ_KEYS
    | frozenset(
        {
            "timestamp",
            "message",
            "subtype",
            "compactMetadata",
            "error",
            "retryAttempt",
            "retryInMs",
            "source",
            "originalModel",
            "fallbackModel",
            "origin",
        }
    ),
    "attachment": _BASE_READ_KEYS | frozenset({"timestamp", "message", "attachment", "rendered"}),
    "queue-operation": _BASE_READ_KEYS | frozenset({"timestamp", "message", "operation"}),
    "agent-setting": _BASE_READ_KEYS | frozenset({"agentSetting"}),
    "mode": _BASE_READ_KEYS | frozenset({"mode"}),
    "cost-state": _BASE_READ_KEYS | frozenset({"totalCostUSD", "hasUnknownModelCost", "modelUsage"}),
}


__all__ = ["parse_transcript", "detect_provider", "set_salt", "load_or_create_salt", "READ_KEYS"]

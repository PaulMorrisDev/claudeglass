"""Claude Code hook: ClaudeGlass's metrics capture.

While metrics capture is on (``[capture]`` in ClaudeGlass's ``config.toml``),
this adds a short note to Claude's context asking it to end its replies
with a one-line tag of closed-vocabulary words, such as
``[cg: task=bugfix brief=partial level=normal]``. ClaudeGlass reads the
tags back from the transcripts. It also logs a few free signals that
cost no tokens.

Claude Code runs ``capture-hook.py``, a small launcher that imports this
module and calls :func:`main`. Python keeps bytecode for a module it
imports and none for the script it is started on, so keeping the work
here saves compiling all of it on every call (about 12 ms of a 60 ms
call). The libraries a call may not need (``hashlib``, ``hmac``,
``subprocess``, ``tomllib``) are imported where they are used, and the
two options are read by hand rather than by ``argparse``.

It runs on these hook events, each added to Claude Code's settings.json
only when a chosen metric needs it (``claudeglass capture
connect``):

- ``SessionStart`` (matcher ``startup|clear|compact``): the main
  session's note. A resumed session already has it, so ``resume`` is not
  matched. A SessionStart inside a subagent (after it compacts) gets
  nothing, and it names the main session's transcript with no agent
  field, so it's the subagent's only when a file under the session's
  ``subagents`` folder gained a ``compact_boundary`` in the last few
  seconds (:func:`session_scope`; Claude Code writes it a fraction of a
  second before the hook runs, while the main session's own boundary
  usually isn't written yet, so one is never required). An ``agent_id``
  or a transcript under a ``subagents`` folder marks one outright. The
  note itself says a subagent should ignore it, for a compaction that
  check misses. The key names of each kind of SessionStart payload (names
  only, never a value) are written once to ``payload-keys.json``, to see
  whether Claude Code ever sends an agent field there. A subagent is
  never asked for anything (``SubagentStart``, which older settings may
  still run this on, adds nothing either).
- ``SubagentStop``, for the agent metrics: the finished run's brief,
  what it did, the end of its report and the session's earlier agent
  runs become a short excerpt for Claude Haiku, as ``Stop`` does below
  for the main session, and its words land in the same tag files. The
  agent's report is left exactly as it was.
- ``PostToolUse``: a one-line note after a large read, search or web
  result, for the Deep level, in the main session while Claude writes
  the tags. How large is measured on what Claude reads of the result
  (:func:`result_chars`), not on the payload: a picture counts for at
  most 1,600 tokens, a result Claude Code saved to a file counts for its
  preview alone, and what only Claude Code shows (a diff, a file's
  earlier text) never counts. An async hook's ``additionalContext``
  does reach Claude (docs/en/hooks.md), but only on the next
  conversation turn -- a full reply late for a note about the result
  Claude just saw -- so this entry runs in the foreground instead,
  matched only to tools whose results can be large, and returns at once
  for any other: the shell and MCP tools are no longer matched, but a
  settings.json written before that still runs the hook after them until
  ``claudeglass capture connect``.
- ``UserPromptSubmit`` and ``PostToolUse`` (also matched to
  ``ExitPlanMode``) for coaching notes (``[capture] coaching`` has
  ``coaching_notes``): a short ``cg-coach`` note when a hint applies --
  a message sent after a break that outlasted the prompt cache (a receipt
  for the rewrite, ``cold_return``), a message asking how background work
  is going while it still runs (``status_poll``), small change requests
  sent one at a time (``drip_feed``), a huge paste, a large result, a plan
  approved after a lot of planning, or a message sent in plan mode after a
  lot of planning (the note asks Claude to end the plan it submits with
  the tip to approve it with a clear context). A tip
  reaches you through
  Claude: the note's first sentence tells it to write the tip and its
  last line is the tip, word for word, so the reply carries it in every
  app. The notes never ask Claude to change how it works. Your message
  is read only for its length and whether it is a status check, a
  go-ahead, a question or a request to change something (by a change
  verb opening one of its sentences, for ``drip_feed``); its words never
  leave the hook. A message sent while Claude was still working (queued:
  the transcript ends on a tool call or a tool's result under
  ``coaching_queued_minutes`` old) gets no note at all. Where Claude Code
  shows hook messages, the same
  tip also appears at once as a one-line notice (``systemMessage``,
  never sent to Claude); the desktop app's Code tab doesn't show one
  (it folds it into a collapsed row of the run summary), so there
  (:func:`delivery`) the note alone carries the tip. A subagent run past
  the length its type's runs are best split at (``coaching.json``, from
  your own sessions) shows you a notice, once, and tells the subagent
  nothing; the desktop app never shows a subagent's notices. Every
  entry that can return output (``SessionStart``, ``SubagentStart``,
  ``UserPromptSubmit``, ``PostToolUse``) runs in the foreground, as an
  async hook's output only arrives on the next turn. Each hint rests for
  a while once shown, twice as long each time it comes back in the same
  session, and the cold-return receipt for half a day (``coach-state.json``).
  The history hints read up to 4 MB of the transcript's end, without
  the lines a desktop resume writes again (:func:`_ordered`). They run at any capture
  level and in every session, but not in a project ``[capture]
  projects`` leaves out. A capture note and a coaching note for the same
  call go out as one note, the capture note first, so a tip is the last
  line.
- ``Stop`` and ``PostToolUse``, also for coaching notes: the time, context
  and cache lifetime of the newest reply, written to ``coach-state.json``
  with no output (:func:`remember_reply`). A replay of old lines at the end
  of a transcript can't move it, and neither can a usage limit or an API
  error written in a reply's place. Only numbers are kept; ``Stop`` runs in
  the background for it.
- ``SessionEnd``, ``Notification``, ``PermissionRequest``, ``Stop`` and
  ``StopFailure`` (all but ``SessionEnd`` async): one line each in
  ``<config-dir>/signals/YYYY-MM.jsonl`` saying why a session ended, what
  Claude waited for, which tool asked for permission, whether a turn
  ended normally or the Stop hook was asked again, or the kind of API
  error that ended one. A line holds the time, a salted hash of the
  session id, and a word from a fixed list or a tool name; never a
  message, a tool's input, a path, ``last_assistant_message``,
  ``error_details`` or a cron's ``prompt``. ``Stop`` fires on every turn,
  so only a sample of its calls is logged (:data:`_TURN_SAMPLE_PCT`);
  ``StopFailure`` is rare enough that every one is kept. Nothing is
  logged until ClaudeGlass has made its salt.
- ``Stop``, while Claude Haiku writes the tags (``[capture] tagger =
  "haiku"``, when the session note asks Claude for none): the end of the
  main session's transcript becomes a short excerpt of the turn (your
  message, what Claude did, the end of its reply; never a tool's
  output), handed to a worker (the launcher with ``--judge``) that
  outlives the call, so the hook returns at once. The worker runs
  ``claude -p --model haiku`` with no tools, settings, MCP servers or
  saved session, the excerpt on stdin, and appends the tag's checked
  words, the reply's id and the call's cost to
  ``<config-dir>/tags/YYYY-MM.jsonl``. This entry runs in the
  foreground: ``claude -p`` exits without waiting for a background hook.

What the note says comes from ``capture-catalogue.json`` next to this
module, written from ``claudeglass.capture_catalogue``;
:func:`build_note` builds the same text as ``capture_catalogue.note_text``.

Coaching notes aside, it adds nothing when capture is off, past its
``until`` time, outside the sampled share of sessions (a hash of the
session id, so a session's subagents follow it), or in a project left
out by ``[capture] projects`` or ``exclude_projects``, and logs nothing
then either. A run with nobody at the screen (``claude -p`` or the Agent
SDK: ``CLAUDE_CODE_ENTRYPOINT`` starts with ``sdk``) gets no note, no
coaching and no Haiku call, since a script reads what it prints; its
signal lines are still logged. A message you didn't type (a background
agent's report, a scheduled task, a command's output, the app's resume
note) gets no prompting or context hint. It uses only the standard
library, and always exits 0 without printing anything on an error, so
it can never block or break a session.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

CATALOGUE_FILE = "capture-catalogue.json"

#: The script Claude Code runs, which imports this module (``main`` below).
LAUNCHER_FILE = "capture-hook.py"

#: Mirrors ``discovery.slug_for``: Claude Code's project folder name.
_NON_ALNUM_RE = re.compile(r"[^A-Za-z0-9]")
_SLUG_MAX_CHARS = 200
_SLUG_HASH_HEX_CHARS = 8

#: Characters per token, for the large-output threshold.
_CHARS_PER_TOKEN = 4

#: ClaudeGlass's salt (``parse.load_or_create_salt``), which the session
#: id is hashed with, and its length.
SALT_FILE = "salt"
_SALT_BYTES = 32

#: The short event names in a signal line.
_SIGNAL_CODES = {
    "SessionEnd": "end",
    "Notification": "wait",
    "PermissionRequest": "perm",
    "Stop": "turn",
    "StopFailure": "fail",
}

#: Stop fires on every turn (unlike SessionEnd/Notification/
#: PermissionRequest, which are comparatively rare), and only the
#: aggregate rate of normal-vs-reentrant turns is of any use, so only
#: this share of Stop calls is logged -- independent of, and on top of,
#: the session-level ``capture.sample`` that ``_capture_for`` already
#: applies. StopFailure is not sampled: a failed turn is rare and worth
#: keeping every time.
_TURN_SAMPLE_PCT = 10

#: Notification types (and, for older Claude Code versions without them,
#: the start of the message) -> what Claude waited for. The full list
#: (SIG-1, curl-verified against docs/en/hooks.md) is
#: ``permission_prompt``, ``idle_prompt``, ``auth_success``,
#: ``elicitation_dialog``, ``elicitation_url_dialog``,
#: ``elicitation_complete``, ``elicitation_response``,
#: ``agent_needs_input``, ``agent_completed``, ``quota_auto_resume_fired``,
#: ``quota_auto_resume_stale``, ``quota_auto_resume_disabled``; anything
#: not mapped here (a completion notice, not a wait, or a type newer
#: than this list) reads as "other".
_WAIT_TYPES = {
    "permission_prompt": "permission",
    "idle_prompt": "idle",
    "elicitation_dialog": "question",
    "elicitation_url_dialog": "question",
    "agent_needs_input": "agent",
    "quota_auto_resume_fired": "quota",
    "quota_auto_resume_stale": "quota",
    "quota_auto_resume_disabled": "quota",
}
_WAIT_MESSAGES = (("Claude needs your permission", "permission"), ("Claude is waiting for your input", "idle"))

#: What a tool name may look like to be logged; anything else is "other".
_TOOL_NAME_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


def resolve_config_dir(cli_arg: str | None = None) -> Path:
    """``--config-dir`` (the claudeglass folder itself), else
    ``<CLAUDE_CONFIG_DIR or ~/.claude>/claudeglass``."""
    if cli_arg:
        return Path(cli_arg)
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else Path.home() / ".claude") / "claudeglass"


def load_catalogue(path: Path | None = None) -> dict:
    path = path or Path(__file__).resolve().with_name(CATALOGUE_FILE)
    return json.loads(path.read_text(encoding="utf-8"))


def load_config(config_dir: Path) -> dict:
    """``config.toml`` as a dict (``{}`` when there is none, or on a
    Python older than 3.11, which has no ``tomllib`` at all -- ROB-P8:
    the import lives here, inside ``main``'s catch-everything, rather
    than at module level, where it would raise before ``main`` ever
    runs and break the "always exits 0" contract)."""
    try:
        import tomllib
    except ImportError:
        return {}
    try:
        text = (config_dir / "config.toml").read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    return tomllib.loads(text)


def project_dir(payload: dict) -> str | None:
    """The folder the session started in: ``CLAUDE_PROJECT_DIR``, which
    Claude Code sets for every hook, else the payload's ``cwd``. ``cwd`` is
    the shell's folder now, so a session that ran ``cd`` into a worktree
    would otherwise be filtered as that worktree's project. Inside the
    project folder, ``cwd``'s own spelling of it is kept (Windows paths
    differ in case), so the slug matches the one ``cwd`` gave."""
    cwd = payload.get("cwd")
    cwd = cwd if isinstance(cwd, str) and cwd else None
    project = (os.environ.get("CLAUDE_PROJECT_DIR") or "").rstrip("\\/")
    if not project:
        return cwd
    if cwd:
        folded, root = os.path.normcase(cwd), os.path.normcase(project)
        if folded == root or folded.startswith(root.rstrip(os.sep) + os.sep):
            return cwd[: len(project)]
    return project


def _sha256_hex(text: str) -> str:
    """The SHA-256 of ``text``, in hex. ``hashlib`` is imported here, not
    at the top: most calls never hash anything."""
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def slug_for(cwd: str) -> str:
    project_dir_name = os.environ.get("CLAUDE_CODE_PROJECT_DIR_NAME")
    if project_dir_name:
        return project_dir_name
    slug = _NON_ALNUM_RE.sub("-", cwd)
    if len(slug) <= _SLUG_MAX_CHARS:
        return slug
    digest = _sha256_hex(cwd)[:_SLUG_HASH_HEX_CHARS]
    return f"{slug[:_SLUG_MAX_CHARS]}-{digest}"


def sampled_in(session_id: str, sample: int) -> bool:
    """Whether this session is in the captured share: the same answer for
    every hook call in the session, subagents included."""
    if sample >= 100:
        return True
    bucket = int(_sha256_hex(session_id)[:8], 16) % 100
    return bucket < sample


def _pattern_matches(pattern: str, slug: str) -> bool:
    """``re.search(pattern, slug, re.IGNORECASE)``, treating a malformed
    ``pattern`` as simply not matching (SEC-P5) rather than raising --
    ``main`` swallows every error and exits 0 regardless, so an
    unguarded ``re.error`` here didn't crash anything, but it took the
    *whole* hook call down with it (no note for the whole session, not
    just this one bad pattern), the same "one bad pattern shouldn't cost
    you the rest of the list" posture ``discovery.resolve_project_dirs``
    and ``corpus._filter_excluded_dirs`` already take."""
    try:
        return bool(re.search(pattern, slug, re.IGNORECASE))
    except re.error:
        return False


def project_allowed(slug: str, projects: list, exclude_projects: list) -> bool:
    """``projects`` holds slug patterns capture runs in, and ``!pattern``
    ones it skips; an empty list means every project. A project Token
    Lens leaves out altogether (``exclude_projects``) is skipped too."""
    for pattern in exclude_projects:
        if isinstance(pattern, str) and _pattern_matches(pattern, slug):
            return False
    includes = [p for p in projects if isinstance(p, str) and not p.startswith("!")]
    for pattern in projects:
        if isinstance(pattern, str) and pattern.startswith("!") and _pattern_matches(pattern[1:], slug):
            return False
    return not includes or any(_pattern_matches(p, slug) for p in includes)


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def active_ids(catalogue: dict, capture: dict) -> tuple[str, ...]:
    """The metrics switched on: a preset level's own, or the ``custom``
    list with what each needs, then the feedback toggles."""
    level = capture.get("level", "off")
    metrics = {m["id"]: m for m in catalogue["metrics"]}
    if level == "custom":
        wanted = {i for i in capture.get("metrics", []) if i in metrics}
        for i in list(wanted):
            wanted.update(metrics[i]["requires"])
        ids = tuple(m["id"] for m in catalogue["metrics"] if m["id"] in wanted)
    else:
        ids = tuple(catalogue["levels"].get(level, ()))
    feedback = set(capture.get("feedback", []))
    return ids + tuple(i for i in catalogue["feedback_ids"] if i in feedback)


def build_note(catalogue: dict, ids, scope: str, agent_type: str = "", tagger: str = "claude") -> str:
    """The note for ``scope`` (``"main"`` or ``"subagent"``); ``""`` when
    none of ``ids`` asks anything there. While Haiku writes the tags
    (``tagger``), the main note asks for no tag. Same text as
    ``capture_catalogue.note_text``."""
    if scope == "subagent":
        # A subagent is asked for nothing: Haiku judges its run afterwards.
        return ""
    wanted = set(ids)
    enabled = [m for m in catalogue["metrics"] if m["id"] in wanted]
    main = scope == "main"
    untagged = main and tagger == "haiku"
    lines = ["" if untagged else m["main_line"] if main else m["sub_line"] for m in enabled]
    extras = [
        (m.get("main_extra_untagged") or m["main_extra"]) if untagged else m["main_extra"] if main else m["sub_extra"]
        for m in enabled
    ]
    codes = [m["id"] for m, line, x in zip(enabled, lines, extras) if line or x]
    if not codes:
        return ""
    text = catalogue["text"]
    out = [f"{catalogue['marker']}{catalogue['version']} {','.join(codes)}", text["intro"]]
    # CAP-1: an extra marked extra_before_tag (feedback_reminder) tells
    # Claude to end its reply with something too, so it goes before the
    # tag block, not after -- the tag instruction stays the last thing
    # the note asks for. Same split as capture_catalogue.note_text.
    before_tag = [x for m, x in zip(enabled, extras) if x and main and m.get("extra_before_tag")]
    after_tag = [x for m, x in zip(enabled, extras) if x and not (main and m.get("extra_before_tag"))]
    out += before_tag
    if any(lines):
        if main:
            out.append(text["main_tag_intro"])
        else:
            keys = any(line for m, line in zip(enabled, lines) if m["id"] != "result")
            out.append(text["sub_tag_intro"].format(tag=text["sub_tag_with_keys"] if keys else text["sub_tag"]))
        out += [line for line in lines if line]
        if main:
            out.append(text["skip_key_line"])
    out += after_tag
    return "\n".join(out)


def tagger_of(capture: dict) -> str:
    """Who writes the main session's tags: ``"haiku"`` or ``"claude"``."""
    return "haiku" if capture.get("tagger") == "haiku" else "claude"


def build_judge_prompt(catalogue: dict, ids) -> str:
    """What Haiku is told when it writes the tags; ``""`` when none of
    ``ids`` asks for a key. Same text as ``capture_catalogue.judge_text``."""
    wanted = set(ids)
    judge = catalogue["judge"]
    lines = [
        judge["lines"].get(m["id"], m["main_line"])
        for m in catalogue["metrics"] if m["id"] in wanted and m["main_line"]
    ]
    if not lines:
        return ""
    return "\n".join([judge["intro"], *lines, judge["rule"]])


def build_tool_note(catalogue: dict, metric_id: str) -> str:
    metric = next((m for m in catalogue["metrics"] if m["id"] == metric_id), None)
    if metric is None or not metric["tool_note"]:
        return ""
    return f"{catalogue['marker']}{catalogue['version']} {metric_id}\n{metric['tool_note']}"


def _in_subagent(payload: dict) -> bool:
    """Whether a SessionStart payload itself says it comes from a
    subagent's compaction: an ``agent_id``, or a transcript inside a
    ``subagents`` folder. Claude Code sends neither today for a subagent's
    compaction, which names the main session's transcript, so
    :func:`session_scope` also asks :func:`_subagent_compacted`.

    A subagent transcript always sits somewhere under the session's
    ``subagents`` folder -- directly, for an ordinary subagent
    (``subagents/agent-<hex>.jsonl``), or one level deeper for a
    workflow-nested one (``subagents/workflows/<run_id>/agent-<hex>.jsonl``
    -- see ``discovery.py``'s module docstring for why that shape
    exists), so this checks every ancestor directory (SURV-2), not just
    the immediate parent as before -- the workflow-nested shape's
    immediate parent is the run id, never literally ``subagents``. The
    filename itself (``agent-*.jsonl``, the same glob
    ``discovery.find_subagents`` globs by) is a second, independent
    signal, for a transcript path shape this doesn't otherwise recognise.
    """
    if payload.get("agent_id"):
        return True
    transcript = payload.get("transcript_path")
    if not isinstance(transcript, str):
        return False
    path = Path(transcript.replace("\\", "/"))
    if path.name.startswith("agent-") and path.name.endswith(".jsonl"):
        return True
    return "subagents" in path.parent.parts


#: A subagent's ``compact_boundary`` is written this many seconds before
#: the SessionStart it causes, at most (audited: 0.1 to 0.6 s in every
#: case), so a boundary this recent in a subagent's transcript puts a
#: SessionStart:compact down to that subagent.
_SUBAGENT_COMPACT_WINDOW_S = 5
#: How far ahead of the hook's clock a record's timestamp may be.
_CLOCK_SKEW_S = 1


def _subagent_compacted(payload: dict, now: datetime) -> bool:
    """Whether a subagent of this session compacted a moment ago: a file
    under ``<session>/subagents/**`` (a workflow's agents sit a level
    deeper) holds a ``compact_boundary`` written within
    :data:`_SUBAGENT_COMPACT_WINDOW_S` seconds. Reads only the record's
    type and time. The main session's own boundary is never needed: it is
    usually not written yet when its SessionStart runs, so a compaction
    with no recent subagent boundary is the main session's."""
    path = payload.get("transcript_path")
    if not isinstance(path, str) or not path:
        return False
    now_ts = now.timestamp()
    try:
        recent = [
            f for f in (Path(path).with_suffix("") / "subagents").rglob("*.jsonl")
            if -_CLOCK_SKEW_S <= now_ts - f.stat().st_mtime <= _SUBAGENT_COMPACT_WINDOW_S
        ]
    except OSError:
        return False
    for file in recent:
        for record in _tail(str(file)):
            if record.get("type") != "system" or record.get("subtype") != "compact_boundary":
                continue
            stamp = _reply_time(record)
            if stamp is not None and -_CLOCK_SKEW_S <= now_ts - stamp.timestamp() <= _SUBAGENT_COMPACT_WINDOW_S:
                return True
    return False


def session_scope(payload: dict, now: datetime) -> str:
    """Whose SessionStart this is: ``"subagent"`` when the payload says so
    (:func:`_in_subagent`) or a subagent just compacted
    (:func:`_subagent_compacted`, only for ``compact``), else ``"main"``."""
    if _in_subagent(payload):
        return "subagent"
    if payload.get("source") == "compact" and _subagent_compacted(payload, now):
        return "subagent"
    return "main"


#: The ``source`` values a SessionStart payload carries.
_SESSION_SOURCES = ("startup", "resume", "clear", "compact")
#: Where :func:`log_payload_keys` writes, in the data folder.
PAYLOAD_KEYS_FILE = "payload-keys.json"
_KEY_NAME_RE = re.compile(r"[A-Za-z0-9_]{1,64}")
_MAX_LOGGED_KEYS = 40


def log_payload_keys(config_dir: Path, payload: dict, scope: str) -> None:
    """Write the key names of this SessionStart payload to
    ``payload-keys.json``, once for each source and scope (``startup``,
    ``compact``, ``compact:subagent`` ...), to see whether Claude Code
    ever sends an agent field there. Names only, in a closed shape: never
    a value, and a name that isn't plain letters, digits and underscores is
    left out."""
    source = payload.get("source")
    label = source if source in _SESSION_SOURCES else "other"
    if scope != "main":
        label = f"{label}:{scope}"
    path = config_dir / PAYLOAD_KEYS_FILE
    seen = _read_json(path)
    if label in seen:
        return
    seen[label] = sorted(k for k in payload if isinstance(k, str) and _KEY_NAME_RE.fullmatch(k))[:_MAX_LOGGED_KEYS]
    _write_json(path, seen)


def _capture_for(payload: dict, config: dict, now: datetime) -> dict | None:
    """The ``[capture]`` table when capture applies to this hook call:
    on, not past its end, this session sampled in, and the project not
    left out. ``None`` otherwise."""
    capture = config.get("capture")
    if not isinstance(capture, dict) or capture.get("level", "off") == "off":
        return None
    until = capture.get("until") or ""
    if until:
        stop = _parse_time(until)
        if stop is None or now >= stop:
            return None
    if not sampled_in(str(payload.get("session_id") or ""), int(capture.get("sample", 100))):
        return None
    cwd = project_dir(payload)
    if cwd:
        exclude = config.get("exclude_projects", [])
        if not project_allowed(slug_for(cwd), capture.get("projects", []), exclude if isinstance(exclude, list) else []):
            return None
    return capture


#: How deep into a tool's result the size measure looks.
_RESULT_DEPTH = 6

#: The keys of a tool result that hold text Claude reads. Anything else
#: (a shell result's ``bashEditDiff``, a read's ``originalFile``, an
#: edit's ``structuredPatch``) is there for Claude Code's own display and
#: never counts.
_RESULT_TEXT_KEYS = ("stdout", "stderr", "content", "result", "results", "text", "output", "title", "url")


def _image_chars(dimensions, catalogue: dict) -> int:
    """What an image counts for, in the characters the size thresholds
    use: its tokens (one per 28 by 28 pixel patch, ``ceil(width / 28) *
    ceil(height / 28)``, at most ``result_image_max_tokens``; the most
    when the size isn't known) times :data:`_CHARS_PER_TOKEN`. Never its
    base64 length, which is not text Claude read."""
    tokens = catalogue["result_image_max_tokens"]
    if isinstance(dimensions, dict):
        width = _number(dimensions.get("displayWidth")) or _number(dimensions.get("originalWidth"))
        height = _number(dimensions.get("displayHeight")) or _number(dimensions.get("originalHeight"))
        if width and height and width > 0 and height > 0:
            patch = catalogue["result_image_patch_px"]
            tokens = min(tokens, int(-(-width // patch) * -(-height // patch)))
    return tokens * _CHARS_PER_TOKEN


def _text_chars(value, catalogue: dict, depth: int = 0) -> int:
    """The characters Claude reads in ``value``, a tool result or part of
    one: its text keys (:data:`_RESULT_TEXT_KEYS`), a read's file content,
    the names a search or glob lists, and an image at what it counts for
    (:func:`_image_chars`). Never keeps anything it looks at."""
    if depth > _RESULT_DEPTH:
        return 0
    if isinstance(value, str):
        return len(value)
    if isinstance(value, list):
        return sum(_text_chars(item, catalogue, depth + 1) for item in value)
    if not isinstance(value, dict):
        return 0
    file = value.get("file") if isinstance(value.get("file"), dict) else {}
    if value.get("type") == "image" or "base64" in file:
        return _image_chars(file.get("dimensions") or value.get("dimensions"), catalogue)
    total = _text_chars(file.get("content"), catalogue, depth + 1)
    total += sum(_text_chars(value.get(key), catalogue, depth + 1) for key in _RESULT_TEXT_KEYS)
    names = value.get("filenames")
    if isinstance(names, list) and not isinstance(value.get("content"), (str, list)):
        total += sum(len(name) + 1 for name in names if isinstance(name, str))
    return total


def result_chars(payload: dict, catalogue: dict) -> int:
    """How much of a PostToolUse call's result Claude reads, in
    characters: the same measure for the large-output note and the
    coaching hint. Only the words of the result count (:func:`_text_chars`),
    a picture for at most 1,600 tokens, and a result Claude Code saved to
    a file, which leaves only a preview in context, for that preview. A
    result is saved when it carries ``persistedOutputPath`` or runs past
    its tool's limit (``result_persist_chars``)."""
    response = payload.get("tool_response")
    chars = _text_chars(response, catalogue)
    limit = catalogue["result_persist_chars"].get(str(payload.get("tool_name") or ""))
    saved = isinstance(response, dict) and bool(response.get("persistedOutputPath"))
    if saved or (limit is not None and chars > limit):
        return min(chars, catalogue["result_preview_chars"])
    return chars


def note_for(
    payload: dict,
    config: dict,
    catalogue: dict,
    now: datetime | None = None,
    result_len: int | None = None,
    scope: str = "",
) -> str:
    """The note this hook call should add, or ``""``. ``result_len`` is
    :func:`result_chars` of a PostToolUse call when the caller already
    measured it. ``scope`` is a SessionStart's :func:`session_scope` when
    the caller already worked it out."""
    event = payload.get("hook_event_name")
    big = False
    if event == "PostToolUse":
        # The size first: under the threshold nothing below applies, so
        # the sampling hash and the project check aren't worth running.
        if result_len is None:
            result_len = result_chars(payload, catalogue)
        big = result_len >= catalogue["big_output_tokens"] * _CHARS_PER_TOKEN
        if not big:
            return ""
    now = now or datetime.now(timezone.utc)
    capture = _capture_for(payload, config, now)
    if capture is None:
        return ""
    ids = active_ids(catalogue, capture)
    agent_type = str(payload.get("agent_type") or "")
    if event == "SubagentStart":
        return build_note(catalogue, ids, "subagent", agent_type)
    if event == "SessionStart":
        if payload.get("source") == "resume":
            return ""
        return build_note(catalogue, ids, scope or session_scope(payload, now), agent_type, tagger_of(capture))
    if big:
        # A subagent's report carries no tag for the note's word to go in,
        # and nor does the main session's reply while Haiku writes the tags.
        untagged = tagger_of(capture) == "haiku" or bool(payload.get("agent_id"))
        if "big_output" in ids and not untagged:
            return build_tool_note(catalogue, "big_output")
    return ""


# -- coaching notes -----------------------------------------------------------

#: How much of a transcript's end the plan hints read: enough for the last
#: few messages and the replies to them, as the status line's hints read.
_COACH_TAIL_BYTES = 256 * 1024
#: How much of its end the hints for a message you send read: a long run of
#: tool calls and results can put the last reply and the last background
#: launch far apart.
_COACH_PROMPT_TAIL_BYTES = 4 * 1024 * 1024
#: How much of its end ``Stop`` and ``PostToolUse`` read to keep the newest
#: reply's time and size: that reply is in the last few lines.
_COACH_STATE_TAIL_BYTES = 64 * 1024
#: How much of a transcript's start :func:`_starting_context` reads to
#: find the first reply.
_COACH_HEAD_BYTES = 512 * 1024
#: A session's (or a run's) row in the coach state is dropped once
#: untouched this long.
_COACH_STATE_MAX_AGE_S = 24 * 3600
#: Cache lifetimes: 5 minutes, or an hour when the session writes to the
#: 1-hour cache.
_CACHE_TTL_S = 300
_CACHE_TTL_1H_S = 3600


def coaching_on(config: dict) -> bool:
    """Whether ``coaching_notes`` is in ``[capture] coaching``, whatever
    the capture level."""
    capture = config.get("capture")
    coaching = capture.get("coaching") if isinstance(capture, dict) else None
    return isinstance(coaching, list) and "coaching_notes" in coaching


def _coaching_applies(payload: dict, config: dict) -> bool:
    """Coaching notes run at any capture level, past ``until`` and in
    every session (no sampling), but only in the projects ``[capture]
    projects`` and ``exclude_projects`` leave in."""
    if not coaching_on(config):
        return False
    capture = config["capture"]
    cwd = project_dir(payload)
    if cwd:
        exclude = config.get("exclude_projects", [])
        projects = capture.get("projects", [])
        return project_allowed(
            slug_for(cwd), projects if isinstance(projects, list) else [], exclude if isinstance(exclude, list) else []
        )
    return True


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict) -> None:
    """Best-effort atomic write (a temp file, then ``os.replace``); a
    clash with another hook call writing at the same moment loses one of
    the two, which only means a hint may repeat."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def coaching_thresholds(coaching: dict, config: dict, personal: dict) -> dict:
    """The catalogue's thresholds, then yours from ``coaching.json``,
    then any ``coaching_<key>`` in ``config.toml``'s ``[thresholds]``."""
    out = dict(coaching["thresholds"])
    mine = personal.get("thresholds")
    configured = config.get("thresholds")
    for source, prefix in ((mine, ""), (configured, "coaching_")):
        if not isinstance(source, dict):
            continue
        for key in out:
            value = _number(source.get(prefix + key))
            if value is not None and value >= 0:
                out[key] = value
    return out


def _session_key(session_id: str, config_dir: Path) -> str:
    """The session id as ``coach-state.json`` keeps it: salted as a signal
    line keeps it (:func:`session_hash`), or plainly hashed before there's
    a salt."""
    salt = read_salt(config_dir)
    if salt is not None:
        return session_hash(salt, session_id)
    return _sha256_hex(session_id)[:16]


def _prune(rows: dict, now_ts: float) -> None:
    for stale in [k for k, row in rows.items() if not isinstance(row, dict)
                  or now_ts - (_number(row.get("touched_at")) or 0) > _COACH_STATE_MAX_AGE_S]:
        del rows[stale]


#: The hints about starting afresh: a build in a fresh session at the plan
#: (``plan_fresh_early``) and after it (``plan_fresh``), and a new task
#: after a break (``cold_return``). Each says "start fresh", so one that
#: showed rests the others for the cooldown, never the other way: a rest
#: that already runs longer is kept.
_FRESH_START_HINTS = ("plan_fresh", "plan_fresh_early", "cold_return")


def _session_row(state: dict, session: str, now_ts: float) -> dict:
    """The session's row in the coach state, made if it isn't there, and
    marked as touched now."""
    sessions = state.setdefault("sessions", {})
    if not isinstance(sessions, dict):
        sessions = state["sessions"] = {}
    _prune(sessions, now_ts)
    row = sessions.setdefault(session, {})
    row["touched_at"] = now_ts
    return row


def _hint_row(row: dict, kind: str) -> dict | None:
    hints = row.get("hints")
    prior = hints.get(kind) if isinstance(hints, dict) else None
    return prior if isinstance(prior, dict) else None


def _rest_s(prior: dict, th: dict) -> float:
    """How long a stamped hint rests: the time stored with it, or the
    cooldown for a stamp written before one was stored."""
    stored = _number(prior.get("rest"))
    return stored if stored is not None and stored > 0 else th["cooldown_minutes"] * 60


def _gate(state: dict, session: str, kind: str, stake: float, now_ts: float, th: dict) -> bool:
    """Whether a hint of ``kind`` may show: not yet in this session, its
    rest is over, or what's at stake has grown ``rearm_factor`` times
    since it last showed. Same rule as the status line's hints."""
    sessions = state.get("sessions")
    row = sessions.get(session) if isinstance(sessions, dict) else None
    prior = _hint_row(row, kind) if isinstance(row, dict) else None
    if prior is None:
        return True
    last_ts = _number(prior.get("ts"))
    if last_ts is not None and now_ts - last_ts >= _rest_s(prior, th):
        return True
    last_stake = _number(prior.get("stake"))
    return last_stake is not None and last_stake > 0 and stake >= last_stake * th["rearm_factor"]


def _stamp(state: dict, session: str, kind: str, stake: float, now_ts: float, th: dict, rest: float | None = None) -> None:
    """Records that ``kind`` showed. It then rests for ``rest`` seconds, or,
    unless told how long, the cooldown doubled for each earlier time it
    showed in this session (up to ``max_backoff`` doublings): a hint that
    keeps coming back is one you have decided to ignore."""
    row = _session_row(state, session, now_ts)
    if not isinstance(row.get("hints"), dict):
        row["hints"] = {}
    prior = _hint_row(row, kind)
    shown = int(_number(prior.get("n")) or 0) + 1 if prior else 1
    if rest is None:
        rest = th["cooldown_minutes"] * 60 * 2 ** min(shown - 1, int(th["max_backoff"]))
    row["hints"][kind] = {"ts": now_ts, "stake": stake, "n": shown, "rest": rest}


def _rest_beside(state: dict, session: str, kind: str, now_ts: float, th: dict) -> None:
    """Rests ``kind`` for the cooldown because a hint like it just showed,
    whatever is at stake now. It isn't counted as a time it showed, and a
    longer rest it already has is kept."""
    row = _session_row(state, session, now_ts)
    if not isinstance(row.get("hints"), dict):
        row["hints"] = {}
    cooldown = th["cooldown_minutes"] * 60
    prior = _hint_row(row, kind)
    if prior is not None:
        last_ts = _number(prior.get("ts"))
        if last_ts is not None and last_ts + _rest_s(prior, th) >= now_ts + cooldown:
            prior["stake"] = 0
            return
    shown = int(_number(prior.get("n")) or 0) if prior else 0
    row["hints"][kind] = {"ts": now_ts, "stake": 0, "n": shown, "rest": cooldown}


def _records(data: bytes) -> list[dict]:
    """The JSON lines in ``data``; a line cut short fails to parse and is
    skipped."""
    out = []
    for raw_line in data.decode("utf-8", errors="replace").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            out.append(record)
    return out


def _tail(path: str, max_bytes: int = _COACH_TAIL_BYTES) -> list[dict]:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            return _records(handle.read())
    except OSError:
        return []


def _head(path: str, max_bytes: int = _COACH_HEAD_BYTES) -> list[dict]:
    try:
        with open(path, "rb") as handle:
            return _records(handle.read(max_bytes))
    except OSError:
        return []


def _usage(record: dict) -> dict:
    message = record.get("message")
    usage = message.get("usage") if isinstance(message, dict) else None
    return usage if isinstance(usage, dict) else {}


def _context(record: dict) -> int:
    """The context a reply read: its input, cached or not."""
    usage = _usage(record)
    return int(sum(_number(usage.get(k)) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")))


def _is_reply(record: dict) -> bool:
    """A reply of Claude's in the main chain that read a context. A line
    Claude Code wrote in its place (an API error, an overload, a usage
    limit: :func:`_is_synthetic`) is none, whatever usage it carries: it
    must never set the clock the cache check runs on or stand for a
    context."""
    return (
        record.get("type") == "assistant"
        and not record.get("isSidechain")
        and not _is_synthetic(record)
        and _context(record) > 0
    )


def _blocks(record: dict) -> list:
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return content if isinstance(content, list) else []


def _is_synthetic(record: dict) -> bool:
    """A line Claude Code wrote in place of a reply: an API error, an
    overload or a usage limit (``parse``'s ``is_synthetic``)."""
    message = record.get("message")
    return bool(record.get("isApiErrorMessage")) or (isinstance(message, dict) and message.get("model") == "<synthetic>")


def _is_human_prompt(record: dict) -> bool:
    """A message you typed: a ``user`` line that isn't meta, carries no
    tool result and has no ``turnOrigin`` that rules it out
    (``statusline._is_human_prompt``)."""
    if record.get("type") != "user" or record.get("isMeta") or "toolUseResult" in record:
        return False
    if record.get("turnOrigin") in _not_typed_origins():
        return False
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return bool(content.strip())
    if not isinstance(content, list):
        return False
    kinds = {b.get("type") for b in content if isinstance(b, dict)}
    return "tool_result" not in kinds and bool(kinds & {"text", "image"})


def _prompt_chars(record: dict) -> int:
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return len(content)
    return sum(len(b.get("text") or "") for b in content if isinstance(b, dict)) if isinstance(content, list) else 0


def _k(tokens: float) -> str:
    return f"{round(tokens / 1000):,}k"


def _idle_text(seconds: float) -> str:
    minutes = round(seconds / 60)
    if minutes < 90:
        return f"{minutes} minutes"
    hours = round(seconds / 3600)
    return f"{hours} hours" if hours < 48 else f"{round(hours / 24)} days"


def _reply_time(record: dict) -> datetime | None:
    stamp = record.get("timestamp")
    return _parse_time(stamp.replace("Z", "+00:00")) if isinstance(stamp, str) else None


def _cache_ttl_s(replies: list[dict]) -> int:
    """An hour when any recent reply wrote to the 1-hour cache, else five
    minutes (as the status line works it out)."""
    for record in replies:
        created = _usage(record).get("cache_creation")
        if isinstance(created, dict) and (_number(created.get("ephemeral_1h_input_tokens")) or 0) > 0:
            return _CACHE_TTL_1H_S
    return _CACHE_TTL_S


#: How far back a record's time may lie behind the newest one before it
#: counts as out of order, in seconds (two writers of one transcript, or a
#: line stamped as it was queued, are a moment apart, not a replay).
_ORDER_TOLERANCE_S = 1
#: The line types that make up the conversation: what a replay repeats.
_CONVERSATION_TYPES = ("user", "assistant", "system")


def _is_typed_text(record: dict) -> bool:
    """A ``user`` line that carries words of yours rather than a tool's
    result. Claude Code stamps one you typed while it worked with the
    moment you typed it, so its time may legitimately lie behind the
    lines written since."""
    return record.get("type") == "user" and not any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in _blocks(record)
    )


def _ordered(records: list[dict]) -> tuple[list[dict], bool]:
    """``records`` without what a replay wrote again, and whether the
    conversation's last line was such a one. The desktop app re-emits
    earlier lines when it resumes a session: the same ``uuid`` again, or a
    time behind the lines already written (a typed line aside, see
    :func:`_is_typed_text`; and only a conversation line counts, as the
    others carry the time they were queued). Left in, they put an old
    reply last. A tail that ends in a replay doesn't say when the real
    last reply was."""
    seen: set[str] = set()
    newest = None
    kept: list[dict] = []
    replayed = False
    for record in records:
        conversation = record.get("type") in _CONVERSATION_TYPES
        again = False
        uuid = record.get("uuid")
        if isinstance(uuid, str) and uuid:
            again = uuid in seen
            seen.add(uuid)
        if not again and conversation and not _is_typed_text(record):
            stamp = _reply_time(record)
            if stamp is not None:
                if newest is not None and (newest - stamp).total_seconds() > _ORDER_TOLERANCE_S:
                    again = True
                elif newest is None or stamp > newest:
                    newest = stamp
        if conversation:
            replayed = again
        if not again:
            kept.append(record)
    return kept, replayed


def _reply_ctx(record: dict) -> int:
    """The context a reply leaves for the next call: what it read plus what
    it wrote."""
    return _context(record) + int(_number(_usage(record).get("output_tokens")) or 0)


def _newest_reply(records: list[dict]) -> dict | None:
    """The newest reply of Claude's in the main chain, as the coach state
    keeps it: when it was written (``at``, a timestamp), the context it
    leaves for the next call (``ctx``: what it read plus what it wrote) and
    how long the cache it wrote lasts (``ttl``). ``None`` when there isn't
    one or it has no time."""
    replies = [r for r in records if _is_reply(r)]
    if not replies:
        return None
    last = replies[-1]
    at = _reply_time(last)
    if at is None:
        return None
    return {"at": at.timestamp(), "ctx": _reply_ctx(last), "ttl": _cache_ttl_s(replies[-5:])}


def _stored_reply(state: dict, session: str) -> dict | None:
    """The newest reply ``Stop`` or ``PostToolUse`` kept for this session
    (:func:`_newest_reply`'s shape), if its numbers are all there."""
    sessions = state.get("sessions")
    row = sessions.get(session) if isinstance(sessions, dict) else None
    kept = row.get("last") if isinstance(row, dict) else None
    if not isinstance(kept, dict):
        return None
    at, ctx, ttl = (_number(kept.get(key)) for key in ("at", "ctx", "ttl"))
    if at is None or ctx is None or ttl is None or ttl <= 0:
        return None
    return {"at": at, "ctx": int(ctx), "ttl": int(ttl)}


def _last_reply(records: list[dict], state: dict, session: str) -> dict | None:
    """The newest of the transcript tail's last reply and the one kept in
    the coach state, which a replay can't have moved."""
    found = [r for r in (_newest_reply(records), _stored_reply(state, session)) if r is not None]
    return max(found, key=lambda r: r["at"]) if found else None


def remember_reply(state: dict, session: str, path, now_ts: float) -> bool:
    """Keeps the newest reply's time, context and cache lifetime in the
    coach state, for the cold-return receipt: a replay can put an old reply
    at the end of a transcript, but not into this. Numbers only. Never moves
    back to an older reply. Returns whether the state changed."""
    if not isinstance(path, str) or not path:
        return False
    records, _replayed = _ordered(_tail(path, _COACH_STATE_TAIL_BYTES))
    found = _newest_reply(records)
    if found is None:
        return False
    kept = _stored_reply(state, session)
    if kept is not None and (kept["at"] > found["at"] or kept == found):
        return False
    _session_row(state, session, now_ts)["last"] = found
    return True


def _compactions(path: str) -> int:
    """How many times the session was compacted: the distinct
    ``compact_boundary`` lines of the whole transcript (a replay writes the
    same line again). Reads a line at a time and keeps only the ids."""
    seen: set[str] = set()
    anonymous = 0
    try:
        with open(path, "rb") as handle:
            for line in handle:
                if not _COMPACT_RE.search(line):
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict) or record.get("type") != "system" or record.get("subtype") != "compact_boundary":
                    continue
                uuid = record.get("uuid")
                if isinstance(uuid, str) and uuid:
                    seen.add(uuid)
                else:
                    anonymous += 1
    except OSError:
        return 0
    return len(seen) + anonymous


def _compacted_text(count: int) -> str:
    """The clause ``cold_return``'s tip adds for a session compacted
    ``count`` times already; none for a session that wasn't."""
    if count <= 0:
        return ""
    times = {1: "once", 2: "twice"}.get(count, f"{count} times")
    return f", in a session already compacted {times}"


def _compacted_since(records: list[dict], at: float) -> bool:
    """Whether a ``compact_boundary`` was written at or after ``at``, a
    timestamp (or, with no time of its own, after the tail's last reply):
    the context that reply read is gone, so a cold cache costs a summary,
    not that reply's context."""
    after_last_reply = True
    for record in reversed(records):
        if _is_reply(record):
            after_last_reply = False
        elif record.get("type") == "system" and record.get("subtype") == "compact_boundary":
            stamp = _reply_time(record)
            if (stamp is None and after_last_reply) or (stamp is not None and stamp.timestamp() >= at):
                return True
    return False


def _limit_stop(records: list[dict], at: float, prefixes: tuple[str, ...]) -> tuple[bool, float | None]:
    """Whether a usage-limit line was written after the reply at ``at`` (a
    timestamp, or, with no time of its own, after the tail's last reply),
    and the latest time a limit it names resets (epoch seconds from the
    line's ``quotaLimits.resetsAt``, ``None`` when no line carries one).
    A limit line is Claude Code's own line in place of a reply, its text
    opening with one of ``prefixes``. Only the time is read from it."""
    found = False
    reset = None
    after_last_reply = True
    for record in reversed(records):
        if _is_reply(record):
            after_last_reply = False
            continue
        if record.get("type") != "assistant" or record.get("isSidechain") or not _is_synthetic(record):
            continue
        if not any(
            isinstance(block, dict) and block.get("type") == "text" and str(block.get("text") or "").startswith(prefixes)
            for block in _blocks(record)
        ):
            continue
        stamp = _reply_time(record)
        if not ((stamp is None and after_last_reply) or (stamp is not None and stamp.timestamp() >= at)):
            continue
        found = True
        quota = record.get("quotaLimits")
        moment = _number(quota.get("resetsAt")) if isinstance(quota, dict) else None
        if moment is not None and (reset is None or moment > reset):
            reset = moment
    return found, reset


def _cold_hint(
    path: str, records: list[dict], replayed: bool, last: dict | None, kept: bool, th: dict, now_ts: float,
    limit_prefixes: tuple[str, ...] = (),
) -> tuple[str, float, dict] | None:
    """``cold_return`` for a message sent after a break that outlasted the
    prompt cache: the gap since the newest reply is longer than its cache
    lifetime and the context it left is ``cold_min_tokens`` or more, a
    receipt whatever the message is. ``last`` is that reply (:func:`_last_reply`;
    ``kept`` says it came from the coach state too, so a replay at the end
    of the tail can't have misled it). Silent with no reply known, when
    the tail ends in a replay and nothing was kept, and after a compaction
    since that reply. Silent too when a usage-limit line followed that
    reply and you are back within a cache lifetime of the limit's reset (or
    the reset isn't known): the wait was the limit's, not a break you took,
    and the cache couldn't have outlived it. Back later than that, it is a
    break like any other. The context is stated less the shared start
    (``warm_prefix_tokens``): that part is rewritten for everyone."""
    if last is None or (replayed and not kept) or last["ctx"] < th["cold_min_tokens"]:
        return None
    idle = now_ts - last["at"]
    if idle <= last["ttl"] or _compacted_since(records, last["at"]):
        return None
    stopped, reset = _limit_stop(records, last["at"], limit_prefixes) if limit_prefixes else (False, None)
    if stopped and (reset is None or now_ts - reset <= last["ttl"]):
        return None
    ctx = max(0, last["ctx"] - th["warm_prefix_tokens"])
    return "cold_return", last["ctx"], {
        "idle": _idle_text(idle), "ctx": _k(ctx), "compacted": _compacted_text(_compactions(path)),
    }


def _result_head(block: dict, limit: int) -> str:
    """The first ``limit`` characters of a tool result's text."""
    content = block.get("content")
    if isinstance(content, str):
        return content[:limit]
    parts: list[str] = []
    size = 0
    for part in content if isinstance(content, list) else ():
        if size >= limit:
            break
        if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
            parts.append(part["text"][:limit])
            size += len(parts[-1])
    return " ".join(parts)[:limit]


def _opens_with(record: dict, prefix: str) -> bool:
    """Whether a ``user`` line's text starts with ``prefix`` (after any
    leading space), looking only at the start of each piece of text."""
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    texts = [content] if isinstance(content, str) else [
        b.get("text") for b in content if isinstance(b, dict) and b.get("type") == "text"
    ] if isinstance(content, list) else []
    return any(isinstance(t, str) and t[: len(prefix) + 64].lstrip().startswith(prefix) for t in texts)


def _is_task_notification(record: dict, prefix: str) -> bool:
    """Whether a line is a background task's finishing message, in any of
    the three shapes Claude Code writes it: a user line, a queued command
    and a queue operation."""
    kind = record.get("type")
    if kind == "queue-operation":
        content = record.get("content")
        return isinstance(content, str) and content.lstrip().startswith(prefix)
    if kind == "attachment":
        attachment = record.get("attachment")
        if not isinstance(attachment, dict) or attachment.get("type") != "queued_command":
            return False
        prompt = attachment.get("prompt")
        texts = [prompt] if isinstance(prompt, str) else [
            b.get("text") for b in prompt if isinstance(b, dict)
        ] if isinstance(prompt, list) else []
        return any(isinstance(t, str) and t.lstrip().startswith(prefix) for t in texts)
    if kind != "user":
        return False
    origin = record.get("origin")
    if (isinstance(origin, dict) and origin.get("kind") == "task-notification") or record.get("turnOrigin") == "task_notification":
        return True
    return _opens_with(record, prefix)


def _background_pending(records: list[dict], coaching: dict) -> bool:
    """Whether a tool's result said that work went to the background and no
    task notification has come in since: Claude is carrying on without it,
    and a message asking how it's going is asking about work that is still
    running, or has finished with nothing sent. The result's own wording is
    matched in memory (``background_launch_pattern``), not the call's
    ``run_in_background``: an agent or a workflow goes to the background
    without it. Nothing of the result is kept."""
    launch = re.compile(coaching["background_launch_pattern"])
    limit = coaching["background_scan_chars"]
    prefix = coaching["task_notification_prefix"]
    for record in reversed(records):
        if record.get("isSidechain"):
            continue
        if _is_task_notification(record, prefix):
            return False
        if record.get("type") != "user":
            continue
        for block in _blocks(record):
            if isinstance(block, dict) and block.get("type") == "tool_result" and launch.search(_result_head(block, limit)):
                return True
    return False


def _poll_hint(
    prompt, records: list[dict], replayed: bool, last: dict | None, kept: bool, coaching: dict, now_ts: float
) -> tuple[str, float, dict] | None:
    """``status_poll`` for a message that only asks how the work is going
    while a background run is still out (:func:`_background_pending`), each
    one a reply that reads the whole session to say little. After a break
    that outlasted the cache, the cold-return receipt speaks instead, so
    this is for a warm cache only. Never promises a notification."""
    if not isinstance(prompt, str) or not _is_status(prompt, coaching):
        return None
    if last is None or (replayed and not kept) or now_ts - last["at"] > last["ttl"]:
        return None
    if not _background_pending(records, coaching):
        return None
    return "status_poll", last["ctx"], {"ctx": _k(last["ctx"])}


#: What a line written as your message starts with when you didn't type
#: it, and the ``turnOrigin`` values that rule one out: a slash command
#: and its output, a shell command run with ``!``, a scheduled task, a
#: background agent's report, another session's message, the desktop
#: app's resume notes. Read once from the catalogue
#: (``capture_catalogue.NOT_TYPED_PREFIXES`` and ``NOT_TYPED_TURN_ORIGINS``,
#: the lists the parser and the status line share) rather than copied
#: here, with the resume notes among them that carry on your last
#: message's work (``RESUME_PREFIXES``).
_NOT_TYPED: dict = {}


def _not_typed_prefixes() -> tuple[str, ...]:
    if "prefixes" not in _NOT_TYPED:
        coaching = load_catalogue()["coaching"]
        _NOT_TYPED["prefixes"] = tuple(coaching["not_typed_prefixes"])
        _NOT_TYPED["origins"] = tuple(coaching["not_typed_turn_origins"])
        _NOT_TYPED["resume"] = tuple(coaching["resume_prefixes"])
    return _NOT_TYPED["prefixes"]


def _not_typed_origins() -> tuple[str, ...]:
    _not_typed_prefixes()
    return _NOT_TYPED["origins"]


def _resume_prefixes() -> tuple[str, ...]:
    _not_typed_prefixes()
    return _NOT_TYPED["resume"]


def _text_of(record: dict) -> str:
    """A ``user`` line's text; an image counts as ``[image]``."""
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
        elif isinstance(block, dict) and block.get("type") == "image":
            parts.append("[image]")
    return "\n".join(parts)


#: The patterns that find a reply's closing question, compiled on first
#: use from the catalogue (``capture_catalogue.REPLY_*``, which
#: ``prompt_shape.ends_on_question`` reads too).
_REPLY: dict = {}


def _reply_patterns() -> dict:
    if not _REPLY:
        coaching = load_catalogue()["coaching"]
        for name in ("fence", "tip_block", "inline_code", "url", "quoted", "reminder_line", "tags", "list_start", "unit"):
            _REPLY[name] = re.compile(coaching["reply_%s_pattern" % name])
        _REPLY["scan_chars"] = coaching["reply_scan_chars"]
        _REPLY["trim"] = coaching["reply_question_trim"]
    return _REPLY


def _ends_on_question(text: str) -> bool:
    """Whether a reply ends on a question to you: the same steps as
    ``prompt_shape.ends_on_question`` (held to it by a test)."""
    r = _reply_patterns()
    text = r["fence"].sub(" ", text)
    text = r["tip_block"].sub("", text)
    text = r["url"].sub(" ", r["inline_code"].sub(" ", text))
    prose = r["quoted"].sub(" ", r["reminder_line"].sub("", text))
    prose = r["tags"].sub("", prose.rstrip()).rstrip()
    units = [unit for unit in r["unit"].split(prose[-r["scan_chars"]:]) if unit.strip()]

    def asks(unit: str) -> bool:
        return unit.rstrip().rstrip(r["trim"]).endswith("?")

    end = len(units)
    while end and r["list_start"].match(units[end - 1]):
        if asks(units[end - 1]):
            return True
        end -= 1
    return any(asks(unit) for unit in units[max(0, end - 2):end])


#: The patterns that tell a tool call that changes a file of yours from one
#: that doesn't, compiled on first use from the catalogue
#: (``capture_catalogue.CONFIG_PATH_PATTERN`` and ``SHELL_WRITE_PATTERN``,
#: which ``prompt_shape.edits_files`` reads too).
_CHANGE: dict = {}


def _change_patterns() -> dict:
    if not _CHANGE:
        coaching = load_catalogue()["coaching"]
        _CHANGE["config_path"] = re.compile(coaching["config_path_pattern"])
        _CHANGE["shell_write"] = re.compile(coaching["shell_write_pattern"], re.IGNORECASE)
    return _CHANGE


def _changes_file(block: dict, edit_tools) -> bool:
    """Whether a ``tool_use`` block changes a file of yours: an edit tool
    aimed outside a ``.claude`` folder, or a shell command that writes a
    file (the catalogue's approximation of ``shell_writes``), neither
    touching one. The same steps as ``prompt_shape.edits_files`` (held to
    it by a test)."""
    patterns = _change_patterns()
    tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
    name = block.get("name")
    if name in edit_tools:
        path = tool_input.get("file_path") or tool_input.get("notebook_path")
        return not (isinstance(path, str) and patterns["config_path"].search(path))
    if name in _SHELL_TOOLS:
        command = tool_input.get("command")
        return (
            isinstance(command, str)
            and patterns["shell_write"].search(command) is not None
            and patterns["config_path"].search(command) is None
        )
    return False


def _agent_edit_files(record: dict) -> int:
    """How many files the subagent behind this ``user`` line changed
    (``toolUseResult.toolStats.editFileCount``; ``prompt_shape.agent_edit_files``)."""
    result = record.get("toolUseResult")
    stats = result.get("toolStats") if isinstance(result, dict) else None
    count = stats.get("editFileCount") if isinstance(stats, dict) else None
    return count if isinstance(count, int) and not isinstance(count, bool) and count > 0 else 0


def _is_async_launch(record: dict) -> bool:
    """Whether a tool result is a background agent's launch message
    rather than its report (``parse._is_async_launch``)."""
    result = record.get("toolUseResult")
    return isinstance(result, dict) and (result.get("isAsync") is True or result.get("status") == "async_launched")


def _untyped_start(record: dict) -> bool:
    """Whether a ``user`` line is one you didn't type, that Claude
    answers by itself and that ends the reply to your last message: a
    background agent's report, a scheduled task, a slash command's output,
    a message from another session. Not a resume note, which carries on
    that message's work (``capture_catalogue.RESUME_PREFIXES``), nor a
    stopped reply's marker (``prompting._UNTYPED_STARTS`` names the same
    lines for the report). The line itself, written when Claude begins
    answering it, marks the end, not the queue operation that held it."""
    if (
        record.get("type") != "user" or record.get("isMeta") or record.get("isCompactSummary")
        or "toolUseResult" in record or not _is_typed_text(record)
    ):
        return False
    if record.get("turnOrigin") in _not_typed_origins():
        return True
    text = _text_of(record).lstrip()
    return text.startswith(_not_typed_prefixes()) and not text.startswith(_resume_prefixes())


def _exchanges(records: list[dict], prefix: str, edit_tools) -> tuple[list[dict], dict]:
    """The messages you typed, oldest first, each with its time
    (``at``), the seconds since your message before it (``since``: your
    own messages alone set the clock), whether the reply before it ended
    on a question, so the message answers it (``answer``; a question that
    closes a reply that changed a file is an offer, and the next message
    isn't taken for an answer to it), whether Claude replied to it at all
    (``answered``: one it didn't reply to before another was sent was
    stopped before any reply, and Claude Code put it back to edit; an API
    error, an overload or a usage limit in a reply's place doesn't answer
    it either) and whether Claude changed a file in reply to it
    (``edited``, from ``edits``: its own edit calls and shell writes
    outside a ``.claude`` folder, and the files its subagents changed,
    less the edit calls that failed). Only the reply the message started
    counts: a line you didn't type (:func:`_untyped_start`) ends it, and
    what Claude does after that is nobody's request; a subagent's files go
    to the message whose reply launched it, whenever its result comes
    back. As the second item, ``typed_at`` (your last message's time) and
    ``answer`` for a message sent now. A stopped reply's marker, a slash
    command, a compaction summary or a subagent's line isn't a message.
    ``statusline._exchanges`` reads the same (held to it by a test)."""
    out: list[dict] = []
    typed_at = None
    said = ""
    edit_calls: dict[str, dict] = {}
    launches: dict[str, dict] = {}
    for record in records:
        if record.get("isSidechain"):
            continue
        if record.get("type") == "assistant" and _is_synthetic(record):
            continue
        if _untyped_start(record):
            # Before Claude's first reply to the message, the line still
            # leaves that reply its own.
            if out and out[-1]["answered"]:
                out[-1]["closed"] = True
            continue
        if record.get("type") == "assistant":
            if out:
                out[-1]["answered"] = True
            for block in _blocks(record):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and isinstance(block.get("text"), str) and block["text"].strip():
                    said = block["text"]
                elif block.get("type") == "tool_use" and out and not out[-1]["closed"]:
                    if _changes_file(block, edit_tools):
                        out[-1]["edits"] += 1
                        if block.get("name") in edit_tools:
                            edit_calls[str(block.get("id"))] = out[-1]
                    elif block.get("name") in _AGENT_TOOLS:
                        launches[str(block.get("id"))] = out[-1]
            continue
        results = [b for b in _blocks(record) if isinstance(b, dict) and b.get("type") == "tool_result"]
        if record.get("type") == "user" and results:
            for block in results:
                key = str(block.get("tool_use_id"))
                call = edit_calls.pop(key, None)
                launched = launches.pop(key, None)
                if block.get("is_error"):
                    if call is not None:
                        call["edits"] = max(0, call["edits"] - 1)
                elif launched is not None and not _is_async_launch(record):
                    launched["edits"] += _agent_edit_files(record)
            continue
        if record.get("isCompactSummary") or not _is_human_prompt(record):
            continue
        text = _text_of(record)
        if text.lstrip().startswith((prefix, *_not_typed_prefixes())):
            continue
        at = _reply_time(record)
        out.append({
            "text": text, "at": at, "edits": 0, "edited": False, "answered": False, "closed": False,
            "since": (at - typed_at).total_seconds() if at is not None and typed_at is not None else None,
            "asked": _ends_on_question(said),
        })
        typed_at = at or typed_at
        said = ""
    for i, ex in enumerate(out):
        ex["edited"] = ex["edits"] > 0
        ex["answer"] = ex.pop("asked") and not (i > 0 and out[i - 1]["edits"] > 0)
        del ex["closed"]
    return out, {"typed_at": typed_at, "answer": _ends_on_question(said) and not (out and out[-1]["edits"] > 0)}


def _is_status(text: str, coaching: dict) -> bool:
    """A message that only asks how the work is going: the catalogue's
    status pattern (``prompt_shape.is_status``)."""
    text = text.strip()
    return len(text) < coaching["status_max_chars"] and re.fullmatch(coaching["status_pattern"], text, re.IGNORECASE) is not None


def _is_question(text: str, coaching: dict) -> bool:
    """A message that asks something: it ends in a question mark or opens
    with a question word (``prompt_shape.is_question``)."""
    text = text.strip()
    return text.endswith("?") or re.match(coaching["asks_pattern"], text, re.IGNORECASE) is not None


def _is_change_request(text: str, coaching: dict) -> bool:
    """A message that asks Claude to change something: a change verb that
    opens a sentence (``change_pattern``), and not a thank-you, a go-ahead,
    a status check, a question or a request to look rather than change
    (``prompt_shape.is_change_request``)."""
    text = text.strip()
    return (
        bool(text)
        and re.search(coaching["change_pattern"], text[:coaching["change_scan_chars"]], re.IGNORECASE) is not None
        and not (
            re.fullmatch(coaching["ack_pattern"], text, re.IGNORECASE)
            or _is_go(text, coaching)
            or _is_status(text, coaching)
            or _is_question(text, coaching)
            or re.match(coaching["review_pattern"], text, re.IGNORECASE)
        )
    )


def _drip_count(earlier: list[dict], prompt: str, since, answer: bool, coaching: dict, th: dict) -> int:
    """How many small change requests in a row ``prompt`` makes: it and the
    messages before it, each short, asking for a change and sent within
    ``drip_window_minutes`` of the message of yours before it, the earlier
    ones each answered with a file change by the reply they started. A
    go-ahead, a thank-you, a status check, a question, a statement and an
    explain request ask for no change (:func:`_is_change_request`): none
    counts and none ends a run, nor does an answer to Claude's question or
    a message stopped before any reply and sent again, and one of them
    sent now gives no run (``prompt_shape.drip_count``, which a test holds
    this to)."""
    window = th["drip_window_minutes"] * 60

    def small(text: str) -> bool:
        return len(text.strip()) <= th["drip_chars"]

    if not small(prompt) or not _is_change_request(prompt, coaching) or answer or since is None or since > window:
        return 0
    count = 1
    for ex in reversed(earlier):
        if ex["answer"] or not ex["answered"] or not _is_change_request(ex["text"], coaching):
            continue
        if not (small(ex["text"]) and ex["edited"] and ex["since"] is not None and ex["since"] <= window):
            break
        count += 1
    return count


def _drip_hint(
    prompt, records: list[dict], coaching: dict, th: dict, now: datetime, ctx: int = 0
) -> tuple[str, float, dict] | None:
    """``drip_feed`` for the message you just sent (``prompt``) when it
    makes ``drip_count`` or more small change requests in a row, as
    ``(kind, stake, fields)``; ``ctx`` is the context the last reply left,
    for the tip's figure. Only lengths, times, counts and what Claude did
    are used; your words never leave this function. The caller has left
    out a queued message and a line you didn't type. A big job sent
    outside plan mode, vague corrections, repeated requests and stopped
    replies are counted after the fact only (``prompting.py``): as live
    hints they fired on questions, polls and refusals, or were right too
    rarely."""
    if not isinstance(prompt, str) or not prompt.strip():
        return None
    earlier, state = _exchanges(records, coaching["interrupt_prefix"], coaching["edit_tools"])
    since = None
    # Claude Code may already have written this message to the transcript.
    last = earlier[-1] if earlier else None
    if last and last["text"] == prompt and last["at"] is not None and (now - last["at"]).total_seconds() < 10:
        earlier.pop()
        answer, since = last["answer"], last["since"]
    else:
        answer = state["answer"]
        if state["typed_at"] is not None:
            since = (now - state["typed_at"]).total_seconds()
    count = _drip_count(earlier, prompt, since, answer, coaching, th)
    return ("drip_feed", count, {"count": count, "ctx": _k(ctx)}) if count >= th["drip_count"] else None


def _agent_earlier_in_reply(records: list[dict], index: int) -> bool:
    """Whether a line of the reply that ``records[index]`` belongs to,
    before it, launched a subagent. Claude Code writes each tool call of a
    reply as a line of its own with the reply's message id, so a subagent
    started beside another tool ([Agent, Bash]) isn't on the newest line."""
    message = records[index].get("message")
    reply = message.get("id") if isinstance(message, dict) else None
    if not reply:
        return False
    for record in reversed(records[:index]):
        if record.get("isSidechain") or record.get("type") not in ("user", "assistant"):
            continue
        earlier = record.get("message")
        if record.get("type") != "assistant" or not isinstance(earlier, dict) or earlier.get("id") != reply:
            return False
        if any(isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") in _AGENT_TOOLS
               for b in _blocks(record)):
            return True
    return False


def _queued(records: list[dict], prefix: str, now: datetime, max_age_s: float) -> bool:
    """Whether the message being sent arrived while Claude was still
    working: the last line of the main chain ends on a tool call or is a
    tool's result (a stopped tool's note aside), not a finished reply, and
    that line is under ``max_age_s`` old. An older one is where work
    stopped, not work under way: you came back to it and typed. Such a
    message is a nudge into work under way, not a new request. A line with
    no time of its own can't be told from a recent one, so it counts. A
    call that launched a subagent and has no result yet is work under way
    whatever its age: a subagent runs as long as it needs. So is one
    launched beside another tool in the same reply, whose own line comes
    later: the results are written together at the end."""
    for index in range(len(records) - 1, -1, -1):
        record = records[index]
        kind = record.get("type")
        if record.get("isSidechain") or kind not in ("user", "assistant"):
            continue
        blocks = [b for b in _blocks(record) if isinstance(b, dict)]
        if kind == "assistant":
            working = bool(blocks) and blocks[-1].get("type") == "tool_use"
            if working and (blocks[-1].get("name") in _AGENT_TOOLS or _agent_earlier_in_reply(records, index)):
                return True
        else:
            working = any(b.get("type") == "tool_result" for b in blocks) and not any(
                b.get("type") == "text" and str(b.get("text") or "").startswith(prefix) for b in blocks
            )
        stamp = _reply_time(record)
        return working and (stamp is None or (now - stamp).total_seconds() < max_age_s)
    return False


#: How much of a transcript :func:`_file_has` reads at a time, and how
#: much of one chunk's end it keeps in front of the next, so a match
#: across the join is found.
_SCAN_CHUNK_BYTES = 1024 * 1024
_SCAN_OVERLAP_BYTES = 128

#: A compaction, as a transcript line writes it (the whole file is read:
#: it isn't always in the tail).
_COMPACT_RE = re.compile(rb'"subtype"\s*:\s*"compact_boundary"')


def _file_has(path: str, pattern: re.Pattern) -> bool:
    """Whether the bytes of the file at ``path`` match ``pattern``, read a
    chunk at a time. Nothing is kept."""
    try:
        with open(path, "rb") as handle:
            carry = b""
            while True:
                chunk = handle.read(_SCAN_CHUNK_BYTES)
                if not chunk:
                    return False
                data = carry + chunk
                if pattern.search(data):
                    return True
                carry = data[-_SCAN_OVERLAP_BYTES:]
    except OSError:
        return False


def _paste_hint(prompt, th: dict) -> tuple[str, float, dict] | None:
    """``big_paste`` for the message you just sent (``prompt``) when it is
    about ``big_paste_tokens`` or more, as ``(kind, stake, fields)``. Only
    its length is used; your words never leave this function. Vague
    corrections, repeated requests, stopped replies and a big job sent
    outside plan mode are counted after the fact only (``prompting.py``):
    as live hints they fired on questions, polls and refusals, or were
    right too rarely."""
    if not isinstance(prompt, str) or not prompt.strip():
        return None
    tokens = len(prompt) / _CHARS_PER_TOKEN
    return ("big_paste", tokens, {"tokens": _k(tokens)}) if tokens >= th["big_paste_tokens"] else None


def _starting_context(path: str) -> int | None:
    """What a fresh session starts with: the first reply's context less
    your first message (``handoff.starting_context``), or ``None`` when
    the first reply lies beyond the start of the transcript that is read
    (a first message too big to tell it from the rest)."""
    records = _head(path)
    first = next((r for r in records if _is_reply(r)), None)
    if first is None:
        return None
    prompt = next((r for r in records if _is_human_prompt(r)), None)
    return max(0, _context(first) - (_prompt_chars(prompt) // _CHARS_PER_TOKEN if prompt else 0))


def _fresh_hint(path: str, plan_chars: int, th: dict) -> tuple[str, float, dict] | None:
    """``plan_fresh`` for a plan of ``plan_chars`` characters approved
    after a lot of planning. What a fresh start would drop: the approving
    reply's context less the session's starting context and the plan
    (``handoff``'s model). None when the starting context isn't known, or
    when the session was compacted since it started: the first reply's
    context isn't what it carries then."""
    replies = [r for r in _tail(path) if _is_reply(r)]
    if not replies:
        return None
    start = _starting_context(path)
    if start is None:
        return None
    kept = _context(replies[-1]) - start - plan_chars // _CHARS_PER_TOKEN
    if kept < th["plan_fresh_tokens"] or _file_has(path, _COMPACT_RE):
        return None
    return "plan_fresh", kept, {"kept": _k(kept)}


def _planning_hint(
    payload: dict, records: list[dict], coaching: dict, th: dict, now: datetime
) -> tuple[str, float, dict] | None:
    """``plan_fresh_early`` for a message sent in plan mode after a lot of
    planning: the plan isn't written yet, so the note asks Claude to end
    the plan it submits with the tip to approve it with a clear context.
    What it holds is the last reply's context less the session's starting
    context. Not for a queued message, nor after a compaction."""
    path = payload.get("transcript_path")
    if payload.get("permission_mode") != "plan" or not isinstance(path, str) or not path:
        return None
    if _queued(records, coaching["interrupt_prefix"], now, th["queued_minutes"] * 60):
        return None
    replies = [r for r in records if _is_reply(r)]
    start = _starting_context(path) if replies else None
    if start is None:
        return None
    kept = _context(replies[-1]) - start
    if kept < th["plan_fresh_tokens"] or _file_has(path, _COMPACT_RE):
        return None
    return "plan_fresh_early", kept, {"kept": _k(kept)}


def _plan_hint(payload: dict, th: dict) -> tuple[str, float, dict] | None:
    """``plan_fresh`` for a plan approved in the dialog: PostToolUse sees
    only those (a rejected plan comes back as an error)."""
    path = payload.get("transcript_path")
    if not isinstance(path, str) or not path:
        return None
    tool_input = payload.get("tool_input")
    plan = tool_input.get("plan") if isinstance(tool_input, dict) else None
    return _fresh_hint(path, len(plan) if isinstance(plan, str) else 0, th)


def _is_go(text: str, coaching: dict) -> bool:
    """A message that only tells Claude to carry on ("continue", "go
    ahead", "implement the plan"): the catalogue's go pattern
    (``prompt_shape.is_go``)."""
    text = text.strip()
    return len(text) <= coaching["go_max_chars"] and re.fullmatch(coaching["go_pattern"], text, re.IGNORECASE) is not None


def _plan_answers(records: list[dict], prefix: str, coaching: dict) -> tuple[list[dict], dict | None, str | None]:
    """The plans among ``records`` (``{"chars", "approved"}``, one per
    ``ExitPlanMode`` call, in order), the one still waiting for your word,
    and the last ``permissionMode`` seen. A plan is approved when its call
    came back without an error, or, when it didn't, when a message of
    yours that only says to carry on follows before the next call, or the
    mode leaves ``plan`` (``parse``'s ``approved_by_message``)."""
    plans: list[dict] = []
    by_id: dict[str, dict] = {}
    waiting = None
    mode = None
    for record in records:
        if record.get("isSidechain"):
            continue
        if record.get("type") == "assistant":
            for block in _blocks(record):
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "ExitPlanMode":
                    given = block.get("input") if isinstance(block.get("input"), dict) else {}
                    plan = {"chars": len(given["plan"]) if isinstance(given.get("plan"), str) else 0, "approved": False}
                    plans.append(plan)
                    by_id[str(block.get("id"))] = plan
                    waiting = None
        elif record.get("type") == "user":
            now = record.get("permissionMode")
            if isinstance(now, str) and now:
                if mode == "plan" and now != "plan" and waiting is not None:
                    waiting["approved"], waiting = True, None
                mode = now
            for block in _blocks(record):
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                plan = by_id.get(str(block.get("tool_use_id")))
                if plan is None:
                    continue
                if block.get("is_error"):
                    waiting = plan
                else:
                    plan["approved"], waiting = True, None
            if waiting is not None and _typed(record, prefix) and _is_go(_text_of(record), coaching):
                waiting["approved"], waiting = True, None
    return plans, waiting, mode


def _typed_plan_hint(payload: dict, records: list[dict], coaching: dict, th: dict) -> tuple[str, float, dict] | None:
    """``plan_fresh`` for a plan you approve by typing a go-ahead, or by
    leaving plan mode, instead of by the dialog. PostToolUse never sees
    that approval, so the dialog can't be the only trigger."""
    path = payload.get("transcript_path")
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not isinstance(path, str) or not path:
        return None
    _plans, waiting, mode = _plan_answers(records, coaching["interrupt_prefix"], coaching)
    if waiting is None:
        return None
    now = payload.get("permission_mode")
    left_plan_mode = mode == "plan" and isinstance(now, str) and now not in ("", "plan")
    if not (_is_go(prompt, coaching) or left_plan_mode):
        return None
    return _fresh_hint(path, waiting["chars"], th)


def _quiet_hint(payload: dict, result_len: int, quiet_how: dict, th: dict) -> tuple[str, float, dict] | None:
    """``quiet_output``: a result about ``quiet_output_tokens`` long, as
    :func:`result_chars` measures it. A read already given a limit is left
    alone. It rests per context: the main session and each subagent keep
    their own stamp (``quiet_output:<agent id>``), as a subagent's big
    result is no reason to stay quiet to you, or the other way round."""
    tokens = result_len / _CHARS_PER_TOKEN
    if tokens < th["quiet_output_tokens"]:
        return None
    tool = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input")
    if tool == "Read" and isinstance(tool_input, dict) and tool_input.get("limit"):
        return None
    agent_id = str(payload.get("agent_id") or "")
    kind = f"quiet_output:{agent_id}" if agent_id else "quiet_output"
    return kind, tokens, {"tokens": _k(tokens), "how": quiet_how.get(tool, quiet_how[""])}


def _agent_transcript(payload: dict) -> Path | None:
    """The subagent's own transcript, under its session's ``subagents``
    folder. A workflow agent's sits a level deeper, so it isn't found:
    its script decides how its work is split."""
    agent_id = str(payload.get("agent_id") or "")
    path = payload.get("transcript_path")
    if not agent_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", agent_id) or not isinstance(path, str) or not path:
        return None
    given = Path(path)
    if given.name == f"agent-{agent_id}.jsonl":
        return given
    own = given.with_suffix("") / "subagents" / f"agent-{agent_id}.jsonl"
    return own if own.is_file() else None


def _count_replies(path: Path, row: dict) -> int:
    """The run's replies since its start or its last summary, reading
    only what was added since the last call (``row`` keeps the place)."""
    offset = int(_number(row.get("offset")) or 0)
    try:
        with open(path, "rb") as handle:
            handle.seek(offset)
            data = handle.read()
    except OSError:
        return int(_number(row.get("replies")) or 0)
    end = data.rfind(b"\n") + 1
    replies = int(_number(row.get("replies")) or 0)
    last_id = row.get("last_id")
    for record in _records(data[:end]):
        if record.get("type") == "system" and record.get("subtype") == "compact_boundary":
            replies, last_id = 0, None
        elif record.get("type") == "assistant":
            message = record.get("message")
            message_id = message.get("id") if isinstance(message, dict) else None
            if message_id != last_id or message_id is None:
                replies += 1
                last_id = message_id
    row.update(offset=offset + end, replies=replies, last_id=last_id)
    return replies


def _split_hint(payload: dict, personal: dict, state: dict, now_ts: float) -> tuple[str, float, dict] | None:
    """``split_run``: a subagent whose type's long runs cost you less split
    (``coaching.json``'s ``split_run``, from the run-split tip), past
    that many replies, once a run. It shows you a notice and tells the
    subagent nothing."""
    splits = personal.get("split_run")
    agent_type = str(payload.get("agent_type") or "")
    every_n = _number(splits.get(agent_type)) if isinstance(splits, dict) else None
    if not every_n or every_n < 1:
        return None
    path = _agent_transcript(payload)
    if path is None:
        return None
    agents = state.setdefault("agents", {})
    if not isinstance(agents, dict):
        agents = state["agents"] = {}
    _prune(agents, now_ts)
    row = agents.setdefault(str(payload["agent_id"]), {})
    row["touched_at"] = now_ts
    replies = _count_replies(path, row)
    if replies < every_n or row.get("told"):
        return None
    return f"split_run:{payload['agent_id']}", replies, {"replies": replies, "agent": agent_type, "every_n": int(every_n)}


def keep_reply(
    payload: dict, config: dict, catalogue: dict, config_dir: Path, now: datetime | None = None
) -> bool:
    """What ``Stop`` does for coaching: keeps the newest reply's time,
    context and cache lifetime in the coach state (:func:`remember_reply`)
    and prints nothing, so it runs in the background. A usage-limit line
    or an API error at the end of the turn is no reply, so the clock the
    cold-return receipt runs on never restarts for one. Returns whether the
    state changed."""
    if not _coaching_applies(payload, config):
        return False
    session_id = payload.get("session_id")
    if payload.get("agent_id") or not isinstance(session_id, str) or not session_id:
        return False
    now = now or datetime.now(timezone.utc)
    state_path = config_dir / catalogue["coaching"]["state_file"]
    state = _read_json(state_path)
    if not remember_reply(state, _session_key(session_id, config_dir), payload.get("transcript_path"), now.timestamp()):
        return False
    _write_json(state_path, state)
    return True


def coaching_note_for(
    payload: dict,
    config: dict,
    catalogue: dict,
    config_dir: Path,
    now: datetime | None = None,
    result_len: int | None = None,
) -> str:
    """The coaching note this hook call should add for Claude, or ``""``
    (see :func:`coaching_for`)."""
    return coaching_for(payload, config, catalogue, config_dir, now, result_len)[0]


def coaching_for(
    payload: dict,
    config: dict,
    catalogue: dict,
    config_dir: Path,
    now: datetime | None = None,
    result_len: int | None = None,
) -> tuple[str, str]:
    """``(note, notice)`` for this hook call: the coaching note to add for
    Claude, and what to show you at once (Claude Code's ``systemMessage``;
    every hint that carries a tip has one), each ``""`` when there's none. At
    most one hint, the first that applies and isn't resting (see
    :func:`_gate`). Reads ``coaching.json`` and the transcript; writes
    ``coach-state.json`` when a hint shows, a subagent's run was counted or
    the newest reply's time and size changed (``PostToolUse`` keeps those,
    printing nothing). A message sent while Claude was still working
    (queued) gets no hint: it nudges work under way, and a reply to it
    isn't one that ends the turn. ``result_len`` is :func:`result_chars`
    of a PostToolUse call when the caller already measured it."""
    if not _coaching_applies(payload, config):
        return "", ""
    event = payload.get("hook_event_name")
    session_id = payload.get("session_id")
    if event not in ("PostToolUse", "UserPromptSubmit") or not isinstance(session_id, str) or not session_id:
        return "", ""
    agent_type = str(payload.get("agent_type") or "")
    in_agent = bool(payload.get("agent_id"))
    if in_agent and agent_type in catalogue["skip_agent_types"]:
        return "", ""
    coaching = catalogue["coaching"]
    now = now or datetime.now(timezone.utc)
    now_ts = now.timestamp()
    personal = _read_json(config_dir / coaching["file"])
    th = coaching_thresholds(coaching, config, personal)
    state_path = config_dir / coaching["state_file"]
    state = _read_json(state_path)
    session = _session_key(session_id, config_dir)
    dirty = False
    candidates: list = []
    tool = str(payload.get("tool_name") or "")
    if event == "UserPromptSubmit":
        path = payload.get("transcript_path")
        prompt = payload.get("prompt")
        typed = not (isinstance(prompt, str) and prompt.lstrip().startswith(_not_typed_prefixes()))
        if not in_agent and typed and isinstance(path, str) and path:
            records, replayed = _ordered(_tail(path, _COACH_PROMPT_TAIL_BYTES))
            if not _queued(records, coaching["interrupt_prefix"], now, th["queued_minutes"] * 60):
                fresh = personal.get("plan_fresh", True) is not False
                last = _last_reply(records, state, session)
                known = _stored_reply(state, session) is not None
                ctx = last["ctx"] if last else next((_reply_ctx(r) for r in reversed(records) if _is_reply(r)), 0)
                candidates = [
                    *([_typed_plan_hint(payload, records, coaching, th), _planning_hint(payload, records, coaching, th, now)] if fresh else []),
                    _drip_hint(prompt, records, coaching, th, now, ctx),
                    _paste_hint(prompt, th),
                    _poll_hint(prompt, records, replayed, last, known, coaching, now_ts),
                    _cold_hint(path, records, replayed, last, known, th, now_ts, tuple(coaching["limit_line_prefixes"])),
                ]
    elif tool == "ExitPlanMode":
        if not in_agent:
            dirty = remember_reply(state, session, payload.get("transcript_path"), now_ts)
            if personal.get("plan_fresh", True) is not False:
                candidates = [_plan_hint(payload, th)]
    else:
        if in_agent:
            candidates.append(_split_hint(payload, personal, state, now_ts))
            dirty = str(payload.get("agent_id")) in (state.get("agents") or {})
        else:
            dirty = remember_reply(state, session, payload.get("transcript_path"), now_ts)
        if result_len is None:
            result_len = result_chars(payload, catalogue)
        candidates.append(_quiet_hint(payload, result_len, coaching["quiet_how"], th))
    # How to do what a tip says depends on the app: a few words each tip fills in.
    how = {key: variants["desktop" if desktop() else "terminal"] for key, variants in coaching["how"].items()}
    for found in candidates:
        if found is None:
            continue
        kind, stake, fields = found
        if not _gate(state, session, kind, stake, now_ts, th):
            continue
        hint = kind.split(":", 1)[0]
        if hint == "cold_return":
            # A receipt for one break: it rests half a day, however the context grows.
            _stamp(state, session, kind, 0, now_ts, th, rest=th["cold_rest_hours"] * 3600)
        else:
            _stamp(state, session, kind, stake, now_ts, th)
        if hint == "split_run":
            # One notice a run: it's for you, and saying it again adds nothing.
            state["agents"][str(payload["agent_id"])]["told"] = True
        if hint in _FRESH_START_HINTS:
            # Resting the others for the cooldown, however much grows.
            for other in _FRESH_START_HINTS:
                if other != hint:
                    _rest_beside(state, session, other, now_ts, th)
        _write_json(state_path, state)
        fields = {**how, **fields}
        text = coaching["text"].get(hint) or ""
        note = f"{coaching['marker']}{coaching['version']} {hint}\n{text.format(**fields)}" if text else ""
        notice = coaching.get("notice", {}).get(hint, "")
        return note, notice.format(**fields) if notice else ""
    if dirty:
        _write_json(state_path, state)
    return "", ""


# -- Haiku writes the tags ------------------------------------------------------

#: How much of a transcript's end the excerpt is read from, and how much
#: when that holds no message of yours (a long turn).
_JUDGE_TAIL_BYTES = 512 * 1024
_JUDGE_LONG_TAIL_BYTES = 4 * 1024 * 1024

#: The tools that start a subagent.
_AGENT_TOOLS = ("Agent", "Task")

#: The tools that run a shell command.
_SHELL_TOOLS = ("Bash", "PowerShell")

#: The most tool names the excerpt lists.
_JUDGE_TOOL_NAMES = 10

#: A reply's tag: ``[cg: ...]``, or ``[tl: ...]`` as notes asked until
#: 0.12.1 (a session started before an update still does).
_TAG_RE = re.compile(r"\[(?:cg|tl):([^\[\]\n]{0,400})\]", re.IGNORECASE)

#: A shell command that runs tests, and how much of them: the same steps
#: as ``testrun.run_scope`` (held to it by a test), with its patterns
#: read on first use from the catalogue (``capture_catalogue.TEST_*_PATTERN``,
#: the one set the parser and the purpose rules share). A heredoc's body is
#: dropped, the line is cut into commands, each loses what comes before its
#: program (a VAR=value, timeout, uv run, an interpreter's folder) and must
#: open with a test runner whose arguments don't say nothing is run.
_TEST: dict = {}


def _test_patterns() -> dict:
    if not _TEST:
        coaching = load_catalogue()["coaching"]
        for name in (
            "command_split", "heredoc", "prefix", "program", "no_run", "no_target", "whole_suite", "target",
            "bare_target", "bare_target_runner",
        ):
            _TEST[name] = re.compile(coaching["test_%s_pattern" % name])
        _TEST["runner"] = re.compile(r"(?P<runner>%s)(?=\s|$)(?P<args>.*)" % coaching["test_runner_pattern"])
    return _TEST


def _test_scope(command: str) -> str:
    """``"full"`` when ``command`` runs a whole suite (even if it also
    runs one test), ``"targeted"`` when it runs only chosen tests, ``""``
    when it runs none."""
    t = _test_patterns()
    scope = ""
    for part in t["command_split"].split(t["heredoc"].sub(r"\g<rest>", command)):
        found = _part_test_scope(part, t)
        if found == "full":
            return "full"
        scope = scope or found
    return scope


def _part_test_scope(part: str, t: dict) -> str:
    part = part.strip()
    while (prefix := t["prefix"].match(part)) is not None:
        part = part[prefix.end():]
    part = t["program"].sub(r"\1", part, count=1)
    match = t["runner"].match(part)
    if match is None or t["no_run"].search(match["args"]):
        return ""
    args = t["whole_suite"].sub(" ", t["no_target"].sub(" ", match["args"]))
    if t["target"].search(args) or (t["bare_target_runner"].fullmatch(match["runner"]) and t["bare_target"].search(args)):
        return "targeted"
    return "full"


#: Files whose change alone makes the work documentation.
_DOC_SUFFIXES = (".md", ".rst", ".txt", ".adoc")

_SKILL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")
_MODEL_RE = re.compile(r"[a-z0-9.-]{1,64}")


def _typed(record: dict, prefix: str) -> bool:
    """A message you typed: not a stopped reply's marker, a slash
    command's lines, a summary or a subagent's line."""
    if record.get("isSidechain") or record.get("isCompactSummary") or not _is_human_prompt(record):
        return False
    text = _text_of(record).lstrip()
    return not text.startswith(prefix) and not text.startswith(_not_typed_prefixes())


def _cut(text: str, limit: int) -> str:
    """``text`` on one line, at most about ``limit`` characters: its start
    and end when longer."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    head = limit * 3 // 4
    return f"{flat[:head]} [...] {flat[-(limit - head):]}"


def _end(text: str, limit: int) -> str:
    """The last ``limit`` characters of ``text``, on one line."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else f"[...] {flat[-limit:]}"


def _judge_records(path: str, prefix: str) -> tuple[list[dict], bool]:
    """The end of the transcript, from far enough back to hold your last
    message when it can, and whether that is the whole file."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return [], False
    records = _tail(path, _JUDGE_TAIL_BYTES)
    if size > _JUDGE_TAIL_BYTES and not any(_typed(r, prefix) for r in records):
        return _tail(path, _JUDGE_LONG_TAIL_BYTES), size <= _JUDGE_LONG_TAIL_BYTES
    return records, size <= _JUDGE_TAIL_BYTES


def _is_plan_file(path) -> bool:
    """A plan Claude Code's plan mode writes, under ``.claude/plans/``."""
    return isinstance(path, str) and "/.claude/plans/" in path.replace("\\", "/")


def _shown_path(path: str, cwd) -> str:
    """``path`` as the excerpt names it: from the project folder when it's
    inside it, else its last two parts."""
    path = path.replace("\\", "/")
    root = cwd.replace("\\", "/").rstrip("/") + "/" if isinstance(cwd, str) and cwd else ""
    if root and path.startswith(root):
        return path[len(root):]
    return "/".join(path.split("/")[-2:])


def _last_text(records: list[dict]) -> str:
    for record in reversed(records):
        if record.get("type") != "assistant":
            continue
        for block in reversed(_blocks(record)):
            if isinstance(block, dict) and block.get("type") == "text" and str(block.get("text") or "").strip():
                return block["text"]
    return ""


def judge_excerpt(
    records: list[dict], payload: dict, catalogue: dict, whole: bool = True, facts: dict | None = None
) -> tuple[str, str]:
    """What Haiku reads about the turn that just ended, and the id of
    Claude's reply its tag belongs to: your message (and, before it, your
    previous message and the end of Claude's reply to it), how many you
    sent before, what Claude did (model calls, output, tools, the files it
    changed, shell commands, skills, subagents, errors, a plan) and the
    end of its final reply. ``("", "")`` without a message of yours with a
    reply after it in ``records``. ``facts``, when given, gets what the
    transcript settles for :func:`grounded`: ``plan_now`` (a plan written
    this turn, by ExitPlanMode or as plan mode's plan file),
    ``plan_before`` (one approved earlier, in the dialog or by a message),
    and how many ``skills`` ran, ``files``
    changed, shell ``commands`` ran and messages came ``earlier``."""
    prefix = catalogue["coaching"]["interrupt_prefix"]
    edit_tools = set(catalogue["coaching"]["edit_tools"])
    limits = catalogue["judge"]["limits"]
    main = [r for r in records if not r.get("isSidechain")]
    typed = [i for i, r in enumerate(main) if _typed(r, prefix)]
    if not typed:
        return "", ""
    start = typed[-1]
    reply = ""
    output: dict[str, int] = {}
    tools: dict[str, int] = {}
    files: dict[str, None] = {}
    commands: list[str] = []
    skills: list[str] = []
    tests: dict[str, None] = {}
    agents = errors = 0
    plan_now = False
    said = ""
    for record in main[start + 1:]:
        if record.get("type") == "user":
            for block in _blocks(record):
                if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
                    errors += 1
            continue
        if record.get("type") != "assistant":
            continue
        message = record.get("message") if isinstance(record.get("message"), dict) else {}
        reply = str(message.get("id") or record.get("requestId") or record.get("uuid") or reply)
        tokens = int(_number(_usage(record).get("output_tokens")) or 0)
        output[reply] = max(output.get(reply, 0), tokens)
        for block in _blocks(record):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str) and block["text"].strip():
                said = block["text"]
            if block.get("type") != "tool_use" or not isinstance(block.get("name"), str):
                continue
            name = block["name"]
            tools[name] = tools.get(name, 0) + 1
            given = block.get("input") if isinstance(block.get("input"), dict) else {}
            target = given.get("file_path") or given.get("notebook_path")
            if name == "ExitPlanMode" or (name in edit_tools and _is_plan_file(target)):
                plan_now = True
            elif name in edit_tools and isinstance(target, str) and target:
                files[_shown_path(target, payload.get("cwd"))] = None
            elif name in _SHELL_TOOLS and isinstance(given.get("command"), str):
                first = given["command"].strip().split("\n")[0]
                if first and len(commands) < limits["commands"]:
                    commands.append(_cut(first, limits["command"]))
                scope = _test_scope(given["command"])
                if scope:
                    tests[scope] = None
            elif name == "Skill" and isinstance(given.get("skill"), str) and _SKILL_RE.fullmatch(given["skill"]):
                skills.append(given["skill"])
            elif name in _AGENT_TOOLS:
                agents += 1
    if not reply:
        return "", ""
    # An approved plan earlier, counting the message just typed: a go-ahead
    # approves a plan the dialog sent back. A rejected plan, or a plan file
    # nobody approved, is no plan to follow.
    plan_before = any(plan["approved"] for plan in _plan_answers(main[:start + 1], prefix, catalogue["coaching"])[0])
    earlier = len(typed) - 1
    # Run a chosen test anywhere and the check was targeted.
    test_scope = "targeted" if "targeted" in tests else "full" if tests else ""
    docs_only = bool(files) and all(name.lower().endswith(_DOC_SUFFIXES) for name in files)
    if facts is not None:
        facts.update(plan_now=plan_now, plan_before=plan_before, skills=len(skills), files=len(files),
                     commands=len(commands), earlier=earlier, tests=test_scope, docs_only=docs_only)
    last = payload.get("last_assistant_message")
    final = last if isinstance(last, str) and last.strip() else said
    lines = []
    if earlier:
        lines.append(f'The user\'s message before this one: "{_cut(_text_of(main[typed[-2]]), limits["previous"])}"')
        answer = _last_text(main[typed[-2] + 1:start])
        if answer:
            lines.append(f'The end of Claude\'s reply to it: "{_end(answer, limits["previous_reply"])}"')
    lines.append(f'The user\'s message: "{_cut(_text_of(main[start]), limits["prompt"])}"')
    lines.append(f"Messages the user sent before it in this session: {earlier}{'' if whole else ' or more'}.")
    named = sorted(tools.items(), key=lambda item: (-item[1], item[0]))[:_JUDGE_TOOL_NAMES]
    changed = list(files)
    shown = ", ".join(changed[:limits["files"]]) + (" and more" if len(changed) > limits["files"] else "")
    did = [
        f"{len(output)} model call{'s' if len(output) != 1 else ''}",
        f"{sum(output.values()):,} output tokens",
        "tools: " + (", ".join(f"{name} {count}" for name, count in named) if named else "none"),
        f"files changed: {len(changed)}" + (f" ({shown})" if changed else ""),
        f"tool errors: {errors}",
    ]
    if agents:
        did.append(f"subagents started: {agents}")
    lines.append("What Claude did: " + "; ".join(did) + ".")
    # Said even when there were none: Haiku otherwise guesses.
    lines.append("Shell commands: " + ("; ".join(f"`{c}`" for c in commands) if commands else "none") + ".")
    lines.append("Skills run: " + (", ".join(dict.fromkeys(skills)) if skills else "none") + ".")
    lines.append(
        "Tests run: " + {"targeted": "chosen tests", "full": "the whole test suite", "": "none"}[test_scope] + "."
    )
    if plan_now:
        lines.append("Plan mode: Claude wrote a plan in this turn.")
    elif plan_before:
        lines.append("Plan mode: the user approved a plan earlier in this session.")
    elif payload.get("permission_mode") == "plan":
        lines.append("Plan mode: on, no plan written yet.")
    else:
        lines.append("Plan mode: not used in this session.")
    lines.append(f'The end of Claude\'s final reply: "{_end(final, limits["reply"])}"')
    return "\n".join(lines), reply


def judge_tag(text, keys, judge: dict, vocab: dict | None = None) -> str:
    """The ``key=word`` pairs of the last ``[cg: ...]`` in ``text`` whose
    key is one of ``keys`` and whose words are known ones (in ``vocab``,
    by default the main session's), as the tag file keeps them
    (``task=bugfix brief=clear``); ``""`` without any. A skill name after
    ``would-help:`` is dropped."""
    matches = _TAG_RE.findall(text) if isinstance(text, str) else []
    if not matches:
        return ""
    vocab = vocab or judge["vocab"]
    list_keys = set(judge["list_keys"])
    wanted = set(keys)
    found: dict[str, str] = {}
    for word in matches[-1].split():
        key, sep, value = word.partition("=")
        key = key.lower()
        value = value.strip("`*_.,;").lower()
        if not sep or key not in wanted or key in found or key not in vocab:
            continue
        if key in list_keys:
            words = [w for w in dict.fromkeys(value.split(",")) if w in vocab[key]]
            if words:
                found[key] = ",".join(words)
            continue
        if key == "skill":
            value = value.partition(":")[0]
        if value in vocab[key]:
            found[key] = value
    return " ".join(f"{key}={value}" for key, value in found.items())


def grounded(words: str, facts: dict | None) -> str:
    """``words`` (``task=bugfix plan=made ...``) with what the transcript
    settles put right: ``plan`` is ``made`` when Claude wrote a plan in
    plan mode this turn, and can't be ``made`` after one written earlier
    (it reads ``following``); ``skill`` can't be ``helped`` or
    ``unneeded`` when no skill ran; ``check`` is ``none`` when Claude
    changed no file, and ``targeted`` or ``full`` when it ran chosen
    tests or a whole suite; ``task`` is ``docs`` when only documentation
    files changed; and a first message has no ``shift`` but ``new``, and
    no ``prior`` but ``none``."""
    if not facts or not words:
        return words
    out = []
    for word in words.split():
        key, _, value = word.partition("=")
        if key == "plan" and facts.get("plan_now"):
            value = "made"
        elif key == "plan" and facts.get("plan_before") and value == "made":
            value = "following"
        elif key == "skill" and not facts.get("skills") and value in ("helped", "unneeded"):
            value = "none"
        elif key == "check" and not facts.get("files"):
            value = "none"
        elif key == "check" and facts.get("tests"):
            value = facts["tests"]
        elif key == "task" and facts.get("docs_only") and value in ("bugfix", "feature", "refactor"):
            value = "docs"
        elif key == "shift" and facts.get("earlier") == 0 and value != "new":
            continue
        elif key == "prior" and facts.get("earlier") == 0:
            value = "none"
        out.append(f"{key}={value}")
    return " ".join(out)


def _agents_running(payload: dict) -> bool:
    """Whether a background agent of this session is still running as the
    turn ends: its report comes back as the next message, and the turn
    that answers it is judged with the whole piece of work. A background
    shell (a dev server, say) doesn't count."""
    tasks = payload.get("background_tasks")
    return isinstance(tasks, list) and any(
        isinstance(t, dict) and t.get("type") == "subagent" and t.get("status") == "running" for t in tasks
    )


def judge_job(payload: dict, config: dict, catalogue: dict, now: datetime | None = None) -> dict | None:
    """What the worker needs to ask Haiku for this turn's tag, or ``None``:
    not a ``Stop`` of the main session, Haiku doesn't write the tags,
    capture doesn't apply here, a Stop asked again, a background agent
    still running (the turn after its report is judged instead), the call
    Haiku itself runs in, or nothing to tag."""
    judge = catalogue["judge"]
    if payload.get("hook_event_name") != "Stop" or os.environ.get(judge["env"]):
        return None
    if payload.get("stop_hook_active") or _in_subagent(payload) or _agents_running(payload):
        return None
    now = now or datetime.now(timezone.utc)
    capture = _capture_for(payload, config, now)
    if capture is None or tagger_of(capture) != "haiku":
        return None
    ids = active_ids(catalogue, capture)
    system = build_judge_prompt(catalogue, ids)
    path = payload.get("transcript_path")
    if not system or not isinstance(path, str):
        return None
    records, whole = _judge_records(path, catalogue["coaching"]["interrupt_prefix"])
    facts: dict = {}
    excerpt, reply = judge_excerpt(records, payload, catalogue, whole, facts)
    if not excerpt:
        return None
    wanted = set(ids)
    return {
        "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "reply": reply,
        "keys": [m["id"] for m in catalogue["metrics"] if m["id"] in wanted and m["main_line"]],
        "system": system,
        "excerpt": excerpt,
        "facts": facts,
    }


# -- Haiku judges agent runs ----------------------------------------------------

#: How much of an agent's transcript is read: its end, for what it did
#: and its report, and its start, for its brief when the file is longer.
_AGENT_TAIL_BYTES = 4 * 1024 * 1024
_AGENT_HEAD_BYTES = 256 * 1024

_TASK_ID_RE = re.compile(r"<task-id>\s*([A-Za-z0-9_-]{1,64})\s*</task-id>")
_TASK_RESULT_RE = re.compile(r"<result>(.*?)</result>", re.DOTALL)
#: Tools whose call is the agent's answer (``quality._ANSWER_TOOLS``): a
#: workflow agent hands its result back through one, not in a reply.
_ANSWER_TOOLS = frozenset({"StructuredOutput"})
#: The line a workflow script's harness puts before the brief it
#: computed, which says who wrote it, not what to do.
_HARNESS_FRAME_RE = re.compile(r"\A\s*\[Workflow harness[^\]\n]*\][^\n]*\n")


def build_agent_judge_prompt(catalogue: dict, ids) -> str:
    """What Haiku is told when it judges an agent run; ``""`` when none of
    ``ids`` is an agent metric. Same text as
    ``capture_catalogue.agent_judge_text``."""
    agent = catalogue["judge"]["agent"]
    wanted = set(ids)
    lines = [m["sub_line"] for m in catalogue["metrics"] if m["id"] in wanted and m["id"] in agent["keys"]]
    if not lines:
        return ""
    return "\n".join([agent["intro"], *lines, agent["rule"]])


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b["text"] for b in content if isinstance(b, dict) and isinstance(b.get("text"), str))
    return ""


def _session_runs(records: list[dict], current: str, brief: str) -> tuple[list[dict], str]:
    """The session's agent runs started before run ``current`` (an agent
    id, whose brief is ``brief``), oldest first, each with its ``type``,
    ``brief`` and ``report``; and what Claude said just before starting
    ``current``, where it usually says why (as when it runs an agent
    again). A run started in the same reply as ``current`` ran beside it,
    not before it, and is left out. Read from the main transcript: the
    Agent calls, a finished run's result, and a background run's report
    in the task notification that brought it back."""
    main = [r for r in records if not r.get("isSidechain")]
    calls: dict[str, dict] = {}  # tool_use id -> the call
    said = ""
    for index, record in enumerate(main):
        if record.get("type") == "user" and _is_human_prompt(record) and not _text_of(record).lstrip().startswith(
            _not_typed_prefixes()
        ):
            said = ""
        if record.get("type") != "assistant":
            continue
        message = record.get("message") if isinstance(record.get("message"), dict) else {}
        for block in _blocks(record):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str) and block["text"].strip():
                said = block["text"]
            elif block.get("type") == "tool_use" and block.get("name") in _AGENT_TOOLS:
                given = block.get("input") if isinstance(block.get("input"), dict) else {}
                calls[str(block.get("id"))] = {
                    "index": index, "message": str(message.get("id") or index), "said": said,
                    "type": str(given.get("subagent_type") or "general-purpose"), "brief": str(given.get("prompt") or ""),
                }
    by_agent: dict[str, str] = {}  # agent id -> tool_use id
    for record in main:
        result = record.get("toolUseResult")
        if record.get("type") != "user":
            continue
        if isinstance(result, dict) and isinstance(result.get("agentId"), str):
            use = next((str(b.get("tool_use_id")) for b in _blocks(record)
                        if isinstance(b, dict) and b.get("type") == "tool_result"), "")
            if use in calls:
                by_agent[result["agentId"]] = use
                if result.get("status") == "completed":
                    calls[use]["report"] = _result_text(result.get("content"))
            continue
        text = _text_of(record)
        if text.lstrip().startswith("<task-notification"):
            found = _TASK_ID_RE.search(text)
            report = _TASK_RESULT_RE.search(text)
            use = by_agent.get(found.group(1)) if found else None
            if use and report:
                calls[use]["report"] = report.group(1)
    mine = by_agent.get(current) or next(
        (use for use, call in reversed(calls.items()) if brief.strip() and call["brief"].strip() == brief.strip()), ""
    )
    if not mine:
        return [], ""
    me = calls[mine]
    earlier = [
        call for use, call in calls.items()
        if use != mine and call["index"] < me["index"] and call["message"] != me["message"] and call["brief"]
    ]
    return earlier, me["said"]


def agent_excerpt(
    records: list[dict], payload: dict, catalogue: dict, earlier: list[dict], started_with: str = ""
) -> tuple[str, str]:
    """What Haiku reads about a finished agent run, and the id of its last
    reply: its type and model, its brief, what the session said as it
    started it (``started_with``), what it did (model calls, output, tools, the
    files it changed, shell commands, errors), the end of its report, or
    the answer it handed back through an answer tool when that came last,
    and the ``earlier`` runs of the session. ``("", "")`` without a brief
    and a reply."""
    limits = catalogue["judge"]["agent"]["limits"]
    edit_tools = set(catalogue["coaching"]["edit_tools"])
    brief = next((_text_of(r) for r in records if r.get("type") == "user" and _is_human_prompt(r)), "")
    brief = _HARNESS_FRAME_RE.sub("", brief, count=1)
    reply = model = said = ""
    answered = False
    output: dict[str, int] = {}
    tools: dict[str, int] = {}
    files: dict[str, None] = {}
    commands: list[str] = []
    errors = 0
    for record in records:
        if record.get("type") == "user":
            for block in _blocks(record):
                if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
                    errors += 1
            continue
        if record.get("type") != "assistant" or _is_synthetic(record):
            continue
        message = record.get("message") if isinstance(record.get("message"), dict) else {}
        reply = str(message.get("id") or record.get("requestId") or record.get("uuid") or reply)
        model = str(message.get("model") or model)
        output[reply] = max(output.get(reply, 0), int(_number(_usage(record).get("output_tokens")) or 0))
        for block in _blocks(record):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str) and block["text"].strip():
                said, answered = block["text"], False
            if block.get("type") != "tool_use" or not isinstance(block.get("name"), str):
                continue
            name = block["name"]
            tools[name] = tools.get(name, 0) + 1
            given = block.get("input") if isinstance(block.get("input"), dict) else {}
            if name in _ANSWER_TOOLS:
                said, answered = json.dumps(given, ensure_ascii=False), True
            target = given.get("file_path") or given.get("notebook_path")
            if name in edit_tools and isinstance(target, str) and target:
                files[_shown_path(target, payload.get("cwd"))] = None
            elif name == "Bash" and isinstance(given.get("command"), str):
                first = given["command"].strip().split("\n")[0]
                if first and len(commands) < limits["commands"]:
                    commands.append(_cut(first, limits["command"]))
    if not brief.strip() or not reply:
        return "", ""
    last = payload.get("last_assistant_message")
    report = last if isinstance(last, str) and last.strip() and not answered else said
    agent_type = str(payload.get("agent_type") or "") or "general-purpose"
    named = sorted(tools.items(), key=lambda item: (-item[1], item[0]))[:_JUDGE_TOOL_NAMES]
    changed = list(files)
    shown = ", ".join(changed[:limits["files"]]) + (" and more" if len(changed) > limits["files"] else "")
    did = [
        f"{len(output)} model call{'s' if len(output) != 1 else ''}",
        f"{sum(output.values()):,} output tokens",
        "tools: " + (", ".join(f"{name} {count}" for name, count in named) if named else "none"),
        f"files changed: {len(changed)}" + (f" ({shown})" if changed else ""),
        f"tool errors: {errors}",
    ]
    lines = [
        f"Agent type: {agent_type}" + (f", on {model}" if _MODEL_RE.fullmatch(model) else "") + ".",
        f'Its brief: "{_cut(brief, limits["brief"])}"',
    ]
    if started_with.strip():
        lines.append(
            f'What the session said as it started this run: "{_end(started_with, limits["earlier_report"])}"'
        )
    lines += [
        "What it did: " + "; ".join(did) + ".",
        "Shell commands: " + ("; ".join(f"`{c}`" for c in commands) if commands else "none") + ".",
        f'Its answer, handed back as structured output: "{_cut(report, limits["report"])}"' if answered
        else f'The end of its report: "{_end(report, limits["report"])}"',
    ]
    shown_runs = earlier[-limits["earlier"]:]
    if shown_runs:
        lines.append("Earlier agent runs in this session, oldest first:")
        for n, run in enumerate(shown_runs, 1):
            ended = _end(run["report"], limits["earlier_report"]) if run.get("report") else "(no report)"
            lines.append(
                f'{n}. {run.get("type") or "agent"}. Brief: "{_cut(run["brief"], limits["earlier_brief"])}" '
                f'Report ended: "{ended}"'
            )
    else:
        lines.append("Earlier agent runs in this session: none.")
    return "\n".join(lines), reply


def agent_judge_job(payload: dict, config: dict, catalogue: dict, now: datetime | None = None) -> dict | None:
    """What the worker needs to ask Haiku about a finished agent run, or
    ``None``: not a ``SubagentStop``, no agent metric on, capture doesn't
    apply here, a stop asked again, an agent that sets up Claude Code
    itself, the call Haiku itself runs in, or nothing to judge."""
    judge = catalogue["judge"]
    if payload.get("hook_event_name") != "SubagentStop" or os.environ.get(judge["env"]):
        return None
    if payload.get("stop_hook_active") or str(payload.get("agent_type") or "") in catalogue["skip_agent_types"]:
        return None
    now = now or datetime.now(timezone.utc)
    capture = _capture_for(payload, config, now)
    if capture is None:
        return None
    ids = active_ids(catalogue, capture)
    system = build_agent_judge_prompt(catalogue, ids)
    given = payload.get("agent_transcript_path")
    path = Path(given) if isinstance(given, str) and given else _agent_transcript(payload)
    if not system or path is None or not path.is_file():
        return None
    records = _tail(str(path), _AGENT_TAIL_BYTES)
    if path.stat().st_size > _AGENT_TAIL_BYTES:
        records = [*_head(str(path), _AGENT_HEAD_BYTES)[:1], *records]
    session = payload.get("transcript_path")
    main = _tail(session) if isinstance(session, str) and session else []
    current = str(payload.get("agent_id") or "")
    brief = next((_text_of(r) for r in records if r.get("type") == "user" and _is_human_prompt(r)), "")
    earlier, started_with = _session_runs(main, current, brief)
    excerpt, reply = agent_excerpt(records, payload, catalogue, earlier, started_with)
    if not excerpt:
        return None
    wanted = set(ids)
    keys = [key for metric_id, metric_keys in judge["agent"]["keys"].items() if metric_id in wanted
            for key in metric_keys]
    return {
        "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "kind": "agent",
        "reply": reply,
        "keys": keys,
        "system": system,
        "excerpt": excerpt,
    }


def spawn_judge(config_dir: Path, job: dict) -> None:
    """Hand ``job`` to a worker (the launcher with ``--judge``) that
    outlives this call, so the Stop hook returns at once and Haiku's
    answer still lands when Claude Code has moved on or, run with ``-p``,
    exited."""
    import subprocess

    options: dict = {"stdin": subprocess.PIPE, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        options["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    # -I -S as the hook itself runs: no user site or PYTHON* settings.
    launcher = Path(__file__).resolve().with_name(LAUNCHER_FILE)
    command = [sys.executable, "-I", "-S", str(launcher), "--judge", "--config-dir", str(config_dir)]
    worker = subprocess.Popen(command, **options)
    worker.stdin.write(json.dumps(job).encode("utf-8"))
    worker.stdin.close()


def _claude_command() -> str | None:
    import shutil

    found = shutil.which("claude")
    if found:
        return found
    fallback = os.environ.get("CLAUDE_CODE_EXECPATH")
    return fallback if fallback and os.path.isfile(fallback) else None


#: What ``claude -p`` answers when it has no working login ("Not logged
#: in · Please run /login", "Failed to authenticate: OAuth session expired
#: and could not be refreshed", "Invalid API key"). The desktop app keeps
#: its own login, so the ``claude`` command can have none.
_NO_LOGIN_RE = re.compile(r"(?i)not logged in|/login|failed to authenticate|invalid api key|oauth")


class NoLogin(Exception):
    """``claude -p`` answered that it isn't signed in."""


def ask_haiku(job: dict, judge: dict, cwd: Path | None = None) -> dict:
    """Claude Code's JSON answer to ``job``, from ``claude -p`` on Haiku
    with no tools, settings, MCP servers or saved session: the excerpt on
    stdin (never on the command line, where other users could read it),
    the instructions as the system prompt, from a file. On the command
    line they would pass through cmd.exe wherever ``claude`` is npm's
    ``claude.cmd``, which reads their ``|`` and line breaks as its own.
    Raises ``FileNotFoundError`` without a ``claude`` command,
    ``subprocess.TimeoutExpired``, :class:`NoLogin` when it isn't signed
    in, or ``ValueError`` when the call fails otherwise."""
    import subprocess
    import tempfile

    command = _claude_command()
    if command is None:
        raise FileNotFoundError("claude")
    env = dict(os.environ)
    env[judge["env"]] = "1"
    env["MAX_THINKING_TOKENS"] = str(int(judge.get("thinking_tokens") or 0))
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    folder = str(cwd) if cwd is not None and cwd.is_dir() else None
    handle, prompt_file = tempfile.mkstemp(prefix=".judge-", suffix=".txt", dir=folder)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as out:
            out.write(job["system"])
        args = [
            command, "-p", "--model", judge["model"], "--tools", "", "--setting-sources", "", "--strict-mcp-config",
            "--no-session-persistence", "--output-format", "json", "--system-prompt-file", prompt_file,
        ]
        extra: dict = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        done = subprocess.run(
            args, input=job["excerpt"].encode("utf-8"), capture_output=True, timeout=judge["timeout_s"], env=env,
            cwd=folder, **extra,
        )
    finally:
        try:
            os.unlink(prompt_file)
        except OSError:
            pass
    answer = json.loads(done.stdout.decode("utf-8", errors="replace") or "null")
    if isinstance(answer, dict) and answer.get("is_error") and _NO_LOGIN_RE.search(str(answer.get("result") or "")):
        raise NoLogin()
    if done.returncode != 0 or not isinstance(answer, dict) or answer.get("is_error"):
        raise ValueError("failed")
    return answer


def run_judge(config_dir: Path, catalogue: dict, job: dict, ask=None) -> dict:
    """Ask Haiku for ``job``'s tag (``ask``: :func:`ask_haiku`) and log
    the answer, or why there is none, in this month's tag file: the
    line written. Only the tag's words, the reply's id and what the call
    cost are kept."""
    import subprocess

    judge = catalogue["judge"]
    agent = job.get("kind") == "agent"
    record: dict = {"ts": job["ts"], "reply": job["reply"]}
    if agent:
        # Marked from the start, so a run that got no words still reads
        # as an agent run, not as a turn of the main session.
        record["agent"] = ""
    try:
        answer = (ask or ask_haiku)(job, judge, config_dir)
    except FileNotFoundError:
        record["err"] = "no_cli"
    except subprocess.TimeoutExpired:
        record["err"] = "timeout"
    except NoLogin:
        record["err"] = "no_login"
    except (OSError, ValueError):
        record["err"] = "failed"
    else:
        if agent:
            tag = judge_tag(answer.get("result"), job["keys"], judge, judge["agent"]["vocab"])
            # "retry=none" only says the run was no retry.
            tag = " ".join(word for word in tag.split() if word != "retry=none")
        else:
            tag = grounded(judge_tag(answer.get("result"), job["keys"], judge), job.get("facts"))
        if tag:
            record["agent" if agent else "tl"] = tag
        else:
            record["err"] = "no_tag"
        usage = answer.get("usage") if isinstance(answer.get("usage"), dict) else {}
        cost = _number(answer.get("total_cost_usd"))
        if cost is not None and 0 <= cost < 10:
            record["usd"] = round(cost, 6)
        record["in"] = int(sum(
            _number(usage.get(k)) or 0
            for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
        ))
        record["out"] = int(_number(usage.get("output_tokens")) or 0)
        models = answer.get("modelUsage")
        model = next(iter(models), "") if isinstance(models, dict) else ""
        if isinstance(model, str) and _MODEL_RE.fullmatch(model):
            record["model"] = model
    folder = config_dir / judge["dir"]
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / f"{record['ts'][:7]}.jsonl", "a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")
    return record


def read_salt(config_dir: Path) -> bytes | None:
    """ClaudeGlass's salt, or ``None`` when it isn't there (yet) or is the
    wrong length: a line hashed with anything else could never be joined
    to its session."""
    try:
        salt = (config_dir / SALT_FILE).read_bytes()
    except OSError:
        return None
    return salt if len(salt) == _SALT_BYTES else None


def session_hash(salt: bytes, session_id: str) -> str:
    """The session id as a signal line keeps it (``signals.session_hash``)."""
    import hashlib
    import hmac

    return hmac.new(salt, session_id.encode("utf-8"), hashlib.sha256).hexdigest()[:16]


def _turn_sampled(session_id: str, now: datetime, pct: int) -> bool:
    """Whether this particular Stop call is in the sampled share -- the
    same hash-modulo shape as :func:`sampled_in`, but keyed by the call's
    own timestamp too (not just the session id), so it varies turn to
    turn within one session instead of being all-or-nothing for it."""
    if pct >= 100:
        return True
    if pct <= 0:
        return False
    key = f"{session_id}:{now.isoformat()}"
    bucket = int(_sha256_hex(key)[:8], 16) % 100
    return bucket < pct


def _wait_kind(payload: dict) -> str:
    notification_type = payload.get("notification_type")
    if notification_type:
        return _WAIT_TYPES.get(str(notification_type), "other")
    message = payload.get("message")
    if isinstance(message, str):
        for start, found in _WAIT_MESSAGES:
            if message.startswith(start):
                return found
    return "other"


def signal_for(payload: dict, config: dict, catalogue: dict, salt: bytes | None, now: datetime | None = None) -> dict | None:
    """The line to log for a SessionEnd, Notification, PermissionRequest,
    Stop or StopFailure call, or ``None`` when its metric is off, there's
    no salt, or (Stop only) this call fell outside the turn sample.

    Reads only ``payload["reason"]``/``notification_type"]``/
    ``message"]``/``tool_name"]``/``stop_hook_active"]``/``error"]`` --
    never ``last_assistant_message``, ``error_details`` or a
    ``session_crons`` entry's ``prompt``, so none of those free-text
    fields can ever reach a signal line."""
    event = payload.get("hook_event_name")
    metric = catalogue["signal_events"].get(event) if isinstance(event, str) else None
    session_id = payload.get("session_id")
    if metric is None or salt is None or not isinstance(session_id, str) or not session_id:
        return None
    now = now or datetime.now(timezone.utc)
    capture = _capture_for(payload, config, now)
    if capture is None or metric not in active_ids(catalogue, capture):
        return None
    if event == "Stop" and not _turn_sampled(session_id, now, _TURN_SAMPLE_PCT):
        return None
    record = {"ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "sid": session_hash(salt, session_id), "e": _SIGNAL_CODES[event]}
    if event == "SessionEnd":
        reason = payload.get("reason")
        record["reason"] = reason if reason in catalogue["session_end_reasons"] else "other"
    elif event == "Notification":
        record["kind"] = _wait_kind(payload)
    elif event == "PermissionRequest":
        tool = payload.get("tool_name")
        record["tool"] = tool if isinstance(tool, str) and _TOOL_NAME_RE.fullmatch(tool) else "other"
    elif event == "Stop":
        record["state"] = "reentrant" if payload.get("stop_hook_active") else "normal"
    else:  # StopFailure
        error = payload.get("error")
        record["error"] = error if error in catalogue["stop_failure_errors"] else "unknown"
    if payload.get("agent_id"):
        record["sub"] = 1
    return record


def write_signal(config_dir: Path, catalogue: dict, record: dict) -> None:
    """Append ``record`` to this month's signal file, as one write."""
    folder = config_dir / catalogue["signals_dir"]
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / f"{record['ts'][:7]}.jsonl", "a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")


#: Claude Code's ``CLAUDE_CODE_ENTRYPOINT`` starts with this for a run
#: with nobody at the screen: ``claude -p`` (``sdk-cli``) and the Agent
#: SDK (``sdk-ts``, ``sdk-py``). A script reads what such a run prints, so
#: it gets no note, no coaching and no Haiku call: only the free signal
#: lines. Claude Code sets ``sdk-cli`` for ``claude -p`` even when it is
#: started from inside a terminal session, where the variable says ``cli``.
_HEADLESS_ENTRYPOINT = "sdk"


def headless() -> bool:
    """Whether this hook call comes from a run with nobody at the screen."""
    return os.environ.get("CLAUDE_CODE_ENTRYPOINT", "").startswith(_HEADLESS_ENTRYPOINT)


#: ``CLAUDE_CODE_ENTRYPOINT`` for the Claude desktop app's Code tab, which
#: folds a hook's ``systemMessage`` into a collapsed "Claude Code notice"
#: row of the run summary, and shows none from a subagent.
_DESKTOP_ENTRYPOINT = "claude-desktop"


def desktop() -> bool:
    """Whether this hook call comes from the desktop app's Code tab."""
    return os.environ.get("CLAUDE_CODE_ENTRYPOINT", "") == _DESKTOP_ENTRYPOINT


def delivery(note: str, notice: str) -> str:
    """The ``systemMessage`` to send for a coaching hint, given its note to
    Claude and its notice: the notice, except where the app would hide it
    and Claude's note already carries the same tip (the desktop app, whose
    note ends on the tip for Claude to write). A hint with no note keeps
    its notice there, the only way it can reach you."""
    return "" if note and desktop() else notice


def _parse_args(argv: list[str]) -> tuple[str | None, bool] | None:
    """``(config_dir, judge)`` from the hook's command line, or ``None``
    for anything else, which the hook ignores. Read by hand: ``argparse``
    costs about 10 ms to import, on every call, for two options.
    ``--judge`` is the worker :func:`spawn_judge` starts, which asks Haiku
    for one turn's tag."""
    config_dir, judge = None, False
    args = iter(argv)
    for arg in args:
        if arg == "--judge":
            judge = True
        elif arg == "--config-dir":
            config_dir = next(args, None)
            if config_dir is None:
                return None
        elif arg.startswith("--config-dir="):
            config_dir = arg.partition("=")[2]
        else:
            return None
    return config_dir, judge


def _unwatched(raw: str) -> bool:
    """Whether ``raw`` is a PostToolUse call for a tool the hook doesn't
    watch (``result_tools``): a settings.json written before the shell and
    MCP tools were dropped from the matcher still runs the hook after
    them, until ``capture connect``. Answered before ``config.toml`` is
    read, the dearest import a call makes, so those calls cost almost
    nothing. Anything it can't tell is not unwatched."""
    if "PostToolUse" not in raw:
        return False
    try:
        payload = json.loads(raw)
        return (
            isinstance(payload, dict)
            and payload.get("hook_event_name") == "PostToolUse"
            and payload.get("tool_name") not in load_catalogue()["result_tools"]
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _run(argv: list[str]) -> None:
    parsed = _parse_args(argv)
    if parsed is None:
        return
    config_dir_arg, judge = parsed
    # Claude Code sends UTF-8 whatever the console's code page is. Read
    # to the end no matter what: leaving stdin unread on an early return
    # is the kind of thing that has surprised a caller elsewhere in the
    # hooks ecosystem, and it costs nothing here.
    raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    config_dir = resolve_config_dir(config_dir_arg)
    if judge:
        job = json.loads(raw)
        if isinstance(job, dict):
            run_judge(config_dir, load_catalogue(), job)
        return
    if _unwatched(raw):
        return
    # ROB-P8: config_dir/config are worked out and checked BEFORE the
    # payload is parsed. Most calls are on a machine where capture is
    # off (it is opt-in), so this skips json.loads on the -- sometimes
    # large -- payload, and load_catalogue()'s own file read, for the
    # common case, without changing what a call that IS captured sees.
    try:
        config = load_config(config_dir)
    except (OSError, ValueError):
        return  # an unreadable or half-written config reads as off
    capture = config.get("capture")
    coach = coaching_on(config)
    if not isinstance(capture, dict) or (capture.get("level", "off") == "off" and not coach):
        return
    payload = json.loads(raw) if raw.strip() else {}
    if not isinstance(payload, dict):
        return
    catalogue = load_catalogue()
    unattended = headless()
    if payload.get("hook_event_name") == "Stop" and coach and not unattended:
        try:
            keep_reply(payload, config, catalogue, config_dir)
        except Exception:  # noqa: BLE001 - a coaching fault must not cost the turn's own signals
            pass
    if payload.get("hook_event_name") in catalogue["signal_events"]:
        record = signal_for(payload, config, catalogue, read_salt(config_dir))
        if record:
            write_signal(config_dir, catalogue, record)
        job = None if unattended else judge_job(payload, config, catalogue)
        if job:
            spawn_judge(config_dir, job)
        return
    if unattended:
        return
    if payload.get("hook_event_name") == "SubagentStop":
        job = agent_judge_job(payload, config, catalogue)
        if job:
            spawn_judge(config_dir, job)
        return
    scope = ""
    if payload.get("hook_event_name") == "SessionStart":
        now = datetime.now(timezone.utc)
        if _capture_for(payload, config, now) is not None:
            scope = session_scope(payload, now)
            try:
                log_payload_keys(config_dir, payload, scope)
            except Exception:  # noqa: BLE001 - a log fault must not cost the note
                pass
    # What Claude reads of a tool's result, measured once for both notes.
    result_len = result_chars(payload, catalogue) if payload.get("hook_event_name") == "PostToolUse" else None
    note = note_for(payload, config, catalogue, result_len=result_len, scope=scope)
    tip = notice = ""
    if coach:
        try:
            tip, notice = coaching_for(payload, config, catalogue, config_dir, result_len=result_len)
        except Exception:  # noqa: BLE001 - a coaching fault must not cost the capture note
            tip = notice = ""
    # One attachment for both: the capture note first, so its marker
    # opens it, and the parser splits the two at the coaching marker.
    text = "\n".join(part for part in (note, tip) if part)
    output: dict = {}
    if text:
        output["hookSpecificOutput"] = {"hookEventName": payload.get("hook_event_name"), "additionalContext": text}
    notice = delivery(tip, notice)
    if notice:
        # Shown to you straight away; never sent to Claude.
        output["systemMessage"] = notice
    if output:
        sys.stdout.write(json.dumps(output))


def main(argv: list[str] | None = None) -> int:
    try:
        _run(sys.argv[1:] if argv is None else list(argv))
    except BaseException:  # noqa: BLE001 - must never fail or block a session
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

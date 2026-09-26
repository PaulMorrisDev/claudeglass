#!/usr/bin/env python3
"""Claude Code hook: ClaudeGlass's metrics capture.

While metrics capture is on (``[capture]`` in ClaudeGlass's ``config.toml``),
this adds a short note to Claude's context asking it to end its replies
with a one-line tag of closed-vocabulary words, such as
``[tl: task=bugfix brief=partial level=normal]``. ClaudeGlass reads the
tags back from the transcripts. It also logs a few free signals that
cost no tokens.

It runs on these hook events, each added to Claude Code's settings.json
only when a chosen metric needs it (``claudeglass capture
connect``):

- ``SessionStart`` (matcher ``startup|clear|compact``): the main
  session's note. A resumed session already has it, so ``resume`` is not
  matched. A SessionStart inside a subagent (after it compacts) gets the
  subagent note: it carries an ``agent_id``, and if a Claude Code
  version leaves that out, a transcript under a ``subagents`` folder
  counts as one too.
- ``SubagentStart``: the subagent note, at every depth.
- ``PostToolUse``: a one-line note after a large tool result or a web
  result, for the Deep level. An async hook's ``additionalContext``
  does reach Claude (docs/en/hooks.md), but only on the next
  conversation turn -- a full reply late for a note about the result
  Claude just saw -- so this entry runs in the foreground instead,
  matched only to tools whose results can be large, and returns at once
  for the rest.
- ``UserPromptSubmit`` and ``PostToolUse`` (also matched to
  ``ExitPlanMode``) for coaching notes (``[capture] coaching`` has
  ``coaching_notes``): a short ``tl-coach`` note when a hint applies --
  an expired cache or a large context when you send a message, a run of
  short fix requests, a vague correction, a huge paste, stopping Claude
  again and again, a large result, many reads for one message, a plan
  approved after a lot of planning, or a subagent run past the length
  its type's runs are best split at (``coaching.json``, from your own
  sessions). Your message is read only for its length and whether it
  asks for a fix; its words never leave the hook. Each hint rests
  for a while once shown (``coach-state.json``). They run at any capture
  level and in every session, but not in a project ``[capture]
  projects`` leaves out. A capture note and a coaching note for the same
  call go out as one note, the capture note first.
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

What the note says comes from ``capture-catalogue.json`` next to this
script, written from ``claudeglass.capture_catalogue``;
:func:`build_note` builds the same text as ``capture_catalogue.note_text``.

Coaching notes aside, it adds nothing when capture is off, past its
``until`` time, outside the sampled share of sessions (a hash of the
session id, so a session's subagents follow it), or in a project left
out by ``[capture] projects`` or ``exclude_projects``, and logs nothing
then either. It uses only the standard library, and always exits 0
without printing anything on an error, so it can never block or break a
session.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

CATALOGUE_FILE = "capture-catalogue.json"

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


def slug_for(cwd: str) -> str:
    project_dir_name = os.environ.get("CLAUDE_CODE_PROJECT_DIR_NAME")
    if project_dir_name:
        return project_dir_name
    slug = _NON_ALNUM_RE.sub("-", cwd)
    if len(slug) <= _SLUG_MAX_CHARS:
        return slug
    digest = hashlib.sha256(cwd.encode("utf-8")).hexdigest()[:_SLUG_HASH_HEX_CHARS]
    return f"{slug[:_SLUG_MAX_CHARS]}-{digest}"


def sampled_in(session_id: str, sample: int) -> bool:
    """Whether this session is in the captured share: the same answer for
    every hook call in the session, subagents included."""
    if sample >= 100:
        return True
    bucket = int(hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:8], 16) % 100
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


def build_note(catalogue: dict, ids, scope: str, agent_type: str = "") -> str:
    """The note for ``scope`` (``"main"`` or ``"subagent"``); ``""`` when
    none of ``ids`` asks anything there. Same text as
    ``capture_catalogue.note_text``."""
    if scope == "subagent" and agent_type in catalogue["skip_agent_types"]:
        return ""
    wanted = set(ids)
    enabled = [m for m in catalogue["metrics"] if m["id"] in wanted]
    if scope == "subagent" and agent_type in catalogue["no_rules_agent_types"]:
        enabled = [m for m in enabled if m["id"] not in ("rules", "agent_brief")]
    main = scope == "main"
    lines = [m["main_line"] if main else m["sub_line"] for m in enabled]
    extras = [m["main_extra"] if main else m["sub_extra"] for m in enabled]
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


def build_tool_note(catalogue: dict, metric_id: str) -> str:
    metric = next((m for m in catalogue["metrics"] if m["id"] == metric_id), None)
    if metric is None or not metric["tool_note"]:
        return ""
    return f"{catalogue['marker']}{catalogue['version']} {metric_id}\n{metric['tool_note']}"


def _in_subagent(payload: dict) -> bool:
    """Whether a SessionStart comes from a subagent's compaction. It
    carries no agent fields today, only the transcript it belongs to.

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
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        exclude = config.get("exclude_projects", [])
        if not project_allowed(slug_for(cwd), capture.get("projects", []), exclude if isinstance(exclude, list) else []):
            return None
    return capture


def note_for(payload: dict, config: dict, catalogue: dict, now: datetime | None = None, raw_len: int = 0) -> str:
    """The note this hook call should add, or ``""``. ``raw_len`` (ROB-P8)
    is the length of the whole stdin payload as Claude Code sent it: a
    cheap stand-in for the tool result's own size that costs no extra
    JSON re-encoding, close enough for a threshold this coarse (the
    result is normally most of the payload)."""
    capture = _capture_for(payload, config, now or datetime.now(timezone.utc))
    if capture is None:
        return ""
    ids = active_ids(catalogue, capture)
    event = payload.get("hook_event_name")
    agent_type = str(payload.get("agent_type") or "")
    if event == "SubagentStart":
        return build_note(catalogue, ids, "subagent", agent_type)
    if event == "SessionStart":
        if payload.get("source") == "resume":
            return ""
        return build_note(catalogue, ids, "subagent" if _in_subagent(payload) else "main", agent_type)
    if event == "PostToolUse":
        threshold = catalogue["big_output_tokens"] * _CHARS_PER_TOKEN
        if "big_output" in ids and raw_len >= threshold:
            return build_tool_note(catalogue, "big_output")
    return ""


# -- coaching notes -----------------------------------------------------------

#: How much of a transcript's end the coaching hints read: enough for a
#: message's reads and searches, as the status line's hints read.
_COACH_TAIL_BYTES = 256 * 1024
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
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
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
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]


def _prune(rows: dict, now_ts: float) -> None:
    for stale in [k for k, row in rows.items() if not isinstance(row, dict)
                  or now_ts - (_number(row.get("touched_at")) or 0) > _COACH_STATE_MAX_AGE_S]:
        del rows[stale]


def _gate(state: dict, session: str, kind: str, stake: float, now_ts: float, th: dict) -> bool:
    """Whether a hint of ``kind`` may show: not yet in this session, its
    cooldown is over, or what's at stake has grown ``rearm_factor`` times
    since it last showed. Same rule as the status line's hints."""
    sessions = state.get("sessions")
    row = sessions.get(session) if isinstance(sessions, dict) else None
    hints = row.get("hints") if isinstance(row, dict) else None
    prior = hints.get(kind) if isinstance(hints, dict) else None
    if not isinstance(prior, dict):
        return True
    last_ts = _number(prior.get("ts"))
    if last_ts is not None and now_ts - last_ts >= th["cooldown_minutes"] * 60:
        return True
    last_stake = _number(prior.get("stake"))
    return last_stake is not None and last_stake > 0 and stake >= last_stake * th["rearm_factor"]


def _stamp(state: dict, session: str, kind: str, stake: float, now_ts: float) -> None:
    sessions = state.setdefault("sessions", {})
    if not isinstance(sessions, dict):
        sessions = state["sessions"] = {}
    _prune(sessions, now_ts)
    row = sessions.setdefault(session, {})
    row["touched_at"] = now_ts
    if not isinstance(row.get("hints"), dict):
        row["hints"] = {}
    row["hints"][kind] = {"ts": now_ts, "stake": stake}


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
    return record.get("type") == "assistant" and not record.get("isSidechain") and _context(record) > 0


def _blocks(record: dict) -> list:
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return content if isinstance(content, list) else []


def _is_human_prompt(record: dict) -> bool:
    """A message you typed: a ``user`` line that isn't meta and carries no
    tool result (``statusline._is_human_prompt``)."""
    if record.get("type") != "user" or record.get("isMeta") or "toolUseResult" in record:
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


def _result_chars(block: dict) -> int:
    content = block.get("content")
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(len(b.get("text", "")) for b in content if isinstance(b, dict) and isinstance(b.get("text"), str))
    return 0


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


def _prompt_hints(records: list[dict], th: dict, now: datetime) -> list[tuple[str, float, dict]]:
    """``cache_cold`` and ``clear_context`` for a message you just sent,
    as ``(kind, stake, fields)``."""
    replies = [r for r in records if _is_reply(r)]
    if not replies:
        return []
    last = replies[-1]
    ctx = _context(last) + int(_number(_usage(last).get("output_tokens")) or 0)
    out = []
    at = _reply_time(last)
    if at is not None and ctx >= th["cold_min_tokens"]:
        idle = (now - at).total_seconds()
        if idle > _cache_ttl_s(replies[-5:]):
            out.append(("cache_cold", ctx, {"idle": _idle_text(idle), "ctx": _k(ctx)}))
    if ctx >= th["clear_context_tokens"]:
        out.append(("clear_context", ctx, {"ctx": _k(ctx)}))
    return out


#: Lines written as your message that you didn't type: a slash command
#: and its output, a shell command run with ``!``, a scheduled task, a
#: background agent's report.
_NOT_TYPED_PREFIXES = (
    "<command-", "<local-command-", "<bash-", "<scheduled-task", "<<autonomous-loop", "<task-notification",
    "[SYSTEM NOTIFICATION",
)


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


def _is_interrupt(record: dict, prefix: str) -> bool:
    """A line saying you stopped a reply (Esc)."""
    return (
        record.get("type") == "user" and not record.get("isSidechain")
        and _text_of(record).lstrip().startswith(prefix)
    )


def _typed(records: list[dict], prefix: str) -> list[tuple[str, datetime | None]]:
    """The messages you typed, oldest first, with their times: no stopped
    reply's marker, slash command, compaction summary or subagent line."""
    out = []
    for record in records:
        if record.get("isSidechain") or record.get("isCompactSummary") or not _is_human_prompt(record):
            continue
        text = _text_of(record)
        if text.lstrip().startswith((prefix, *_NOT_TYPED_PREFIXES)):
            continue
        out.append((text, _reply_time(record)))
    return out


def _practice_hints(prompt, records: list[dict], coaching: dict, th: dict, now: datetime) -> list[tuple[str, float, dict]]:
    """``fix_drip``, ``stop_loop``, ``vague_fix`` and ``big_paste`` for the
    message you just sent (``prompt``), as ``(kind, stake, fields)``.
    Only lengths, counts and whether a pattern matched are used; your
    words never leave this function."""
    prefix = coaching["interrupt_prefix"]
    window_s = th["stop_window_minutes"] * 60
    stops = sum(
        1 for r in records if _is_interrupt(r, prefix)
        and (at := _reply_time(r)) is not None and 0 <= (now - at).total_seconds() <= window_s
    )
    stop = None
    if stops >= th["stop_loop_count"]:
        stop = ("stop_loop", stops, {"count": stops, "minutes": round(th["stop_window_minutes"])})
    if not isinstance(prompt, str) or not prompt.strip():
        return [stop] if stop else []
    scan = int(coaching["correction_scan_chars"])
    fix_re = re.compile(coaching["fix_pattern"], re.IGNORECASE)
    correction_re = re.compile(coaching["correction_pattern"], re.IGNORECASE)

    def is_fix(text: str) -> bool:
        head = text[:scan]
        return len(text.strip()) <= th["fix_chars"] and bool(fix_re.search(head) or correction_re.search(head))

    earlier = _typed(records, prefix)
    # Claude Code may already have written this message to the transcript.
    if earlier and earlier[-1][0] == prompt and earlier[-1][1] is not None and (now - earlier[-1][1]).total_seconds() < 10:
        earlier.pop()
    streak = 0
    if is_fix(prompt):
        streak, after = 1, now
        for text, at in reversed(earlier):
            if not is_fix(text) or at is None or (after - at).total_seconds() > th["fix_window_minutes"] * 60:
                break
            streak, after = streak + 1, at
    drip = vague = paste = None
    if streak >= th["fix_drip_count"]:
        drip = ("fix_drip", streak, {"count": streak})
    elif (
        streak and len(prompt.strip()) <= th["vague_fix_chars"]
        and not re.search(coaching["specific_pattern"], prompt)
    ):
        vague = ("vague_fix", 1, {})
    tokens = len(prompt) / _CHARS_PER_TOKEN
    if tokens >= th["big_paste_tokens"]:
        paste = ("big_paste", tokens, {"tokens": _k(tokens)})
    return [hint for hint in (drip, stop, vague, paste) if hint]


def _starting_context(path: str) -> int:
    """What a fresh session starts with: the first reply's context less
    your first message (``handoff.starting_context``)."""
    records = _head(path)
    first = next((r for r in records if _is_reply(r)), None)
    if first is None:
        return 0
    prompt = next((r for r in records if _is_human_prompt(r)), None)
    return max(0, _context(first) - (_prompt_chars(prompt) // _CHARS_PER_TOKEN if prompt else 0))


def _plan_hint(payload: dict, th: dict) -> tuple[str, float, dict] | None:
    """``plan_fresh``: an approved plan (a rejected one comes back as an
    error, which PostToolUse doesn't see) after a lot of planning. What a
    fresh start would drop: the approving reply's context less the
    session's starting context and the plan (``handoff``'s model)."""
    path = payload.get("transcript_path")
    if not isinstance(path, str) or not path:
        return None
    replies = [r for r in _tail(path) if _is_reply(r)]
    if not replies:
        return None
    tool_input = payload.get("tool_input")
    plan = tool_input.get("plan") if isinstance(tool_input, dict) else None
    plan_tokens = len(plan) // _CHARS_PER_TOKEN if isinstance(plan, str) else 0
    kept = _context(replies[-1]) - _starting_context(path) - plan_tokens
    if kept < th["plan_fresh_tokens"]:
        return None
    return "plan_fresh", kept, {"kept": _k(kept)}


def _reads_hint(payload: dict, raw_len: int, read_tools, th: dict) -> tuple[str, float, dict] | None:
    """``explore_reads``: this many reads and searches since your last
    message, counted from the transcript's tool calls, with this one's
    result (not in the transcript yet) added."""
    path = payload.get("transcript_path")
    if not isinstance(path, str) or not path:
        return None
    records = [r for r in _tail(path) if not r.get("isSidechain")]
    start = max((i for i, r in enumerate(records) if _is_human_prompt(r)), default=-1)
    current = records[start + 1:]
    reads = {
        str(block.get("id")) for r in current if r.get("type") == "assistant" for block in _blocks(r)
        if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") in read_tools
    }
    this_call = str(payload.get("tool_use_id") or "")
    reads.add(this_call or "this call")
    if len(reads) < th["explore_reads"]:
        return None
    seen = {
        str(block.get("tool_use_id")): _result_chars(block) for r in current if r.get("type") == "user"
        for block in _blocks(r) if isinstance(block, dict) and block.get("type") == "tool_result"
    }
    chars = sum(n for use_id, n in seen.items() if use_id in reads)
    if this_call not in seen:
        chars += raw_len
    return "explore_reads", len(reads), {"reads": len(reads), "tokens": _k(chars / _CHARS_PER_TOKEN)}


def _quiet_hint(payload: dict, raw_len: int, quiet_how: dict, th: dict) -> tuple[str, float, dict] | None:
    """``quiet_output``: a result about ``quiet_output_tokens`` long. A
    read already given a limit is left alone."""
    tokens = raw_len / _CHARS_PER_TOKEN
    if tokens < th["quiet_output_tokens"]:
        return None
    tool = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input")
    if tool == "Read" and isinstance(tool_input, dict) and tool_input.get("limit"):
        return None
    return "quiet_output", tokens, {"tokens": _k(tokens), "how": quiet_how.get(tool, quiet_how[""])}


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
    that many replies."""
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
    if replies < every_n:
        return None
    return f"split_run:{payload['agent_id']}", replies, {"replies": replies, "agent": agent_type, "every_n": int(every_n)}


def coaching_note_for(
    payload: dict, config: dict, catalogue: dict, config_dir: Path, now: datetime | None = None, raw_len: int = 0
) -> str:
    """The coaching note this hook call should add, or ``""``: at most one
    hint, the first that applies and isn't resting (see :func:`_gate`),
    in ``COACHING_HINTS`` order. Reads ``coaching.json`` and the
    transcript; writes ``coach-state.json`` when a hint shows or a
    subagent's run was counted."""
    if not _coaching_applies(payload, config):
        return ""
    event = payload.get("hook_event_name")
    session_id = payload.get("session_id")
    if event not in ("PostToolUse", "UserPromptSubmit") or not isinstance(session_id, str) or not session_id:
        return ""
    agent_type = str(payload.get("agent_type") or "")
    in_agent = bool(payload.get("agent_id"))
    if in_agent and agent_type in catalogue["skip_agent_types"]:
        return ""
    coaching = catalogue["coaching"]
    now = now or datetime.now(timezone.utc)
    now_ts = now.timestamp()
    personal = _read_json(config_dir / coaching["file"])
    th = coaching_thresholds(coaching, config, personal)
    state_path = config_dir / coaching["state_file"]
    state = _read_json(state_path)
    counted = False
    candidates: list = []
    tool = str(payload.get("tool_name") or "")
    if event == "UserPromptSubmit":
        path = payload.get("transcript_path")
        if not in_agent and isinstance(path, str) and path:
            records = _tail(path)
            candidates = [*_practice_hints(payload.get("prompt"), records, coaching, th, now), *_prompt_hints(records, th, now)]
    elif tool == "ExitPlanMode":
        if not in_agent and personal.get("plan_fresh", True) is not False:
            candidates = [_plan_hint(payload, th)]
    else:
        if in_agent:
            candidates.append(_split_hint(payload, personal, state, now_ts))
            counted = str(payload.get("agent_id")) in (state.get("agents") or {})
        candidates.append(_quiet_hint(payload, raw_len, coaching["quiet_how"], th))
        if not in_agent and tool in coaching["read_tools"]:
            candidates.append(_reads_hint(payload, raw_len, coaching["read_tools"], th))
    session = _session_key(session_id, config_dir)
    for found in candidates:
        if found is None:
            continue
        kind, stake, fields = found
        if not _gate(state, session, kind, stake, now_ts, th):
            continue
        _stamp(state, session, kind, stake, now_ts)
        _write_json(state_path, state)
        hint = kind.split(":", 1)[0]
        return f"{coaching['marker']}{coaching['version']} {hint}\n{coaching['text'][hint].format(**fields)}"
    if counted:
        _write_json(state_path, state)
    return ""


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
    bucket = int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:8], 16) % 100
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


def _run(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="capture-hook.py")
    parser.add_argument("--config-dir", default=None)
    args = parser.parse_args(argv)
    # Claude Code sends UTF-8 whatever the console's code page is. Read
    # to the end no matter what: leaving stdin unread on an early return
    # is the kind of thing that has surprised a caller elsewhere in the
    # hooks ecosystem, and it costs nothing here.
    raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    # ROB-P8: config_dir/config are worked out and checked BEFORE the
    # payload is parsed. Most calls are on a machine where capture is
    # off (it is opt-in), so this skips json.loads on the -- sometimes
    # large -- payload, and load_catalogue()'s own file read, for the
    # common case, without changing what a call that IS captured sees.
    config_dir = resolve_config_dir(args.config_dir)
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
    if payload.get("hook_event_name") in catalogue["signal_events"]:
        record = signal_for(payload, config, catalogue, read_salt(config_dir))
        if record:
            write_signal(config_dir, catalogue, record)
        return
    note = note_for(payload, config, catalogue, raw_len=len(raw))
    tip = ""
    if coach:
        try:
            tip = coaching_note_for(payload, config, catalogue, config_dir, raw_len=len(raw))
        except Exception:  # noqa: BLE001 - a coaching fault must not cost the capture note
            tip = ""
    # One attachment for both: the capture note first, so its marker
    # opens it, and the parser splits the two at the coaching marker.
    text = "\n".join(part for part in (note, tip) if part)
    if text:
        output = {"hookSpecificOutput": {"hookEventName": payload.get("hook_event_name"), "additionalContext": text}}
        sys.stdout.write(json.dumps(output))


def main(argv: list[str] | None = None) -> int:
    try:
        _run(sys.argv[1:] if argv is None else list(argv))
    except BaseException:  # noqa: BLE001 - must never fail or block a session
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

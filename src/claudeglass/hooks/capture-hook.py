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
  an expired cache or a large context when you send a message, the same
  request again, small requests sent one at a time, a big task sent
  without a plan, a vague correction, a huge paste, stopping Claude
  again and again, a large result, many reads for one message, a plan
  approved after a lot of planning, or a subagent run past the length
  its type's runs are best split at (``coaching.json``, from your own
  sessions). Your message is read only for its length and whether it
  asks for a fix; its words never leave the hook. The prompting hints
  also show you a one-line notice at once (``systemMessage``, never sent
  to Claude). Each hint rests
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
- ``Stop``, while Claude Haiku writes the tags (``[capture] tagger =
  "haiku"``, when the session note asks Claude for none): the end of the
  main session's transcript becomes a short excerpt of the turn (your
  message, what Claude did, the end of its reply; never a tool's
  output), handed to a worker (this script with ``--judge``) that
  outlives the call, so the hook returns at once. The worker runs
  ``claude -p --model haiku`` with no tools, settings, MCP servers or
  saved session, the excerpt on stdin, and appends the tag's checked
  words, the reply's id and the call's cost to
  ``<config-dir>/tags/YYYY-MM.jsonl``. This entry runs in the
  foreground: ``claude -p`` exits without waiting for a background hook.

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


def build_note(catalogue: dict, ids, scope: str, agent_type: str = "", tagger: str = "claude") -> str:
    """The note for ``scope`` (``"main"`` or ``"subagent"``); ``""`` when
    none of ``ids`` asks anything there. While Haiku writes the tags
    (``tagger``), the main note asks for no tag. Same text as
    ``capture_catalogue.note_text``."""
    if scope == "subagent" and agent_type in catalogue["skip_agent_types"]:
        return ""
    wanted = set(ids)
    enabled = [m for m in catalogue["metrics"] if m["id"] in wanted]
    if scope == "subagent" and agent_type in catalogue["no_rules_agent_types"]:
        enabled = [m for m in enabled if m["id"] not in ("rules", "agent_brief")]
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
        scope = "subagent" if _in_subagent(payload) else "main"
        return build_note(catalogue, ids, scope, agent_type, tagger_of(capture))
    if event == "PostToolUse":
        threshold = catalogue["big_output_tokens"] * _CHARS_PER_TOKEN
        # While Haiku writes the tags, the main session ends its replies
        # with none for the note's word to go in: subagents still get it.
        untagged = tagger_of(capture) == "haiku" and not payload.get("agent_id")
        if "big_output" in ids and raw_len >= threshold and not untagged:
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


#: How far back from the end of Claude's last words a question mark
#: makes your next message an answer to it, not a new request.
_QUESTION_TAIL_CHARS = 300


def _exchanges(records: list[dict], prefix: str, edit_tools) -> tuple[list[dict], dict]:
    """The messages you typed, oldest first, each with its time
    (``at``), the seconds since Claude's reply before it (``gap``),
    whether that reply ended on a question, so the message answers it
    (``answer``), whether Claude replied to it at all (``answered``: one
    it didn't reply to before another was sent was stopped before any
    reply, and Claude Code put it back to edit) and whether Claude changed
    a file in reply to it (``edited``); and, as the second item,
    ``replied_at`` and ``answer`` for a message sent now. A stopped reply's
    marker, a slash command, a compaction summary or a subagent's line
    isn't a message."""
    out: list[dict] = []
    replied_at = None
    said = ""
    for record in records:
        if record.get("isSidechain"):
            continue
        if record.get("type") == "assistant":
            replied_at = _reply_time(record) or replied_at
            if out:
                out[-1]["answered"] = True
            for block in _blocks(record):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and isinstance(block.get("text"), str) and block["text"].strip():
                    said = block["text"]
                elif block.get("type") == "tool_use" and block.get("name") in edit_tools and out:
                    out[-1]["edited"] = True
            continue
        if record.get("isCompactSummary") or not _is_human_prompt(record):
            continue
        text = _text_of(record)
        if text.lstrip().startswith(prefix):
            if out:
                out[-1]["stopped"] = True
            continue
        if text.lstrip().startswith(_NOT_TYPED_PREFIXES):
            continue
        at = _reply_time(record)
        out.append({
            "text": text, "at": at, "edited": False, "answered": False, "stopped": False,
            "gap": (at - replied_at).total_seconds() if at is not None and replied_at is not None else None,
            "answer": "?" in said.rstrip()[-_QUESTION_TAIL_CHARS:],
        })
        said = ""
    return out, {"replied_at": replied_at, "answer": "?" in said.rstrip()[-_QUESTION_TAIL_CHARS:]}


#: How much of a message is read for its steps (``prompt_shape.STEP_SCAN_CHARS``).
_STEP_SCAN_CHARS = 8_000
_WORD_RE = re.compile(r"[a-z0-9']+")
_PLAN_WORD_RE = re.compile(r"\bplan\b", re.IGNORECASE)


def _request_steps(text: str, coaching: dict) -> int:
    """How many separate changes ``text`` asks for: the most of its list
    lines, its change verbs, and the items of one sentence that starts
    with a change verb (``prompt_shape.request_steps``, which a test holds
    this to)."""
    text = text[:_STEP_SCAN_CHARS]
    action_re = re.compile(coaching["action_pattern"], re.IGNORECASE)
    separator_re = re.compile(coaching["item_separator_pattern"], re.IGNORECASE)
    items = len(re.findall(coaching["list_item_pattern"], text))
    actions = len(action_re.findall(text))
    listed = 0
    for sentence in re.split(coaching["sentence_end_pattern"], text):
        sentence = sentence.strip()
        if sentence and action_re.match(sentence):
            listed = max(listed, 1 + len(separator_re.findall(sentence)))
    return max(items, actions, listed)


def _words(text: str) -> frozenset:
    return frozenset(_WORD_RE.findall(text.lower()))


def _similarity(a: frozenset, b: frozenset) -> float:
    """The share of words two messages have in common (``prompt_shape.similarity``)."""
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def _drip_count(earlier: list[dict], prompt: str, gap, answer: bool, ack_re, th: dict) -> int:
    """How many small requests in a row ``prompt`` makes: it and the
    messages before it, each short and sent within ``drip_window_minutes``
    of Claude's reply, the earlier ones each answered with a file change.
    Words don't matter. An answer to Claude's question, or a message
    stopped before any reply and sent again, is skipped, and a bare
    "thanks" or "ok" sent now counts for nothing."""
    window = th["drip_window_minutes"] * 60

    def small(text: str) -> bool:
        return len(text.strip()) <= th["drip_chars"]

    if not small(prompt) or answer or gap is None or gap > window or ack_re.fullmatch(prompt.strip()):
        return 0
    count = 1
    for ex in reversed(earlier):
        if ex["answer"] or not ex["answered"]:
            continue
        if not (small(ex["text"]) and ex["edited"] and ex["gap"] is not None and ex["gap"] <= window):
            break
        count += 1
    return count


def _practice_hints(
    prompt, records: list[dict], coaching: dict, th: dict, now: datetime, mode=None
) -> list[tuple[str, float, dict]]:
    """``repeat_ask``, ``drip_feed``, ``stop_loop``, ``plan_first``,
    ``vague_fix`` and ``big_paste`` for the message you just sent
    (``prompt``, in permission ``mode``), as ``(kind, stake, fields)``, in
    that order. Only lengths, times, counts, what Claude did and whether
    a pattern matched are used; your words never leave this function."""
    prefix = coaching["interrupt_prefix"]
    window_s = th["stop_window_minutes"] * 60
    earlier, now_state = _exchanges(records, prefix, coaching["edit_tools"])
    gap = None
    if isinstance(prompt, str):
        # Claude Code may already have written this message to the transcript.
        last = earlier[-1] if earlier else None
        if last and last["text"] == prompt and last["at"] is not None and (now - last["at"]).total_seconds() < 10:
            earlier.pop()
            now_state = {"replied_at": None, "answer": last["answer"]}
            gap = last["gap"]
        elif now_state["replied_at"] is not None:
            gap = (now - now_state["replied_at"]).total_seconds()

    def recent(at) -> bool:
        return at is not None and 0 <= (now - at).total_seconds() <= window_s

    # A stop mid-reply leaves a marker line; a stop before any reply only
    # leaves a message Claude never answered.
    stops = sum(1 for r in records if _is_interrupt(r, prefix) and recent(_reply_time(r)))
    stops += sum(1 for ex in earlier if not ex["answered"] and not ex["stopped"] and recent(ex["at"]))
    stop = None
    if stops >= th["stop_loop_count"]:
        stop = ("stop_loop", stops, {"count": stops, "minutes": round(th["stop_window_minutes"])})
    if not isinstance(prompt, str) or not prompt.strip():
        return [stop] if stop else []
    count = _drip_count(
        earlier, prompt, gap, now_state["answer"], re.compile(coaching["ack_pattern"], re.IGNORECASE), th
    )
    repeat = drip = plan = vague = paste = None
    mine = _words(prompt)
    if len(mine) >= th["repeat_min_words"] and not re.fullmatch(coaching["ack_pattern"], prompt.strip(), re.IGNORECASE):
        window = th["repeat_window_minutes"] * 60
        # Only a message Claude answered was an attempt: one stopped
        # before any reply and sent again is the same attempt.
        if any(
            ex["answered"] and ex["at"] is not None and 0 <= (now - ex["at"]).total_seconds() <= window
            and _similarity(mine, _words(ex["text"])) >= th["repeat_similarity"]
            for ex in earlier
        ):
            repeat = ("repeat_ask", 1, {})
    if count >= th["drip_count"]:
        drip = ("drip_feed", count, {"count": count})
    else:
        scan = int(coaching["correction_scan_chars"])
        head = prompt[:scan]
        asks_fix = re.search(coaching["fix_pattern"], head, re.IGNORECASE) or re.search(
            coaching["correction_pattern"], head, re.IGNORECASE
        )
        if asks_fix and len(prompt.strip()) <= th["vague_fix_chars"] and not re.search(coaching["specific_pattern"], prompt):
            vague = ("vague_fix", 1, {})
    tokens = len(prompt) / _CHARS_PER_TOKEN
    if tokens >= th["big_paste_tokens"]:
        paste = ("big_paste", tokens, {"tokens": _k(tokens)})
    elif isinstance(mode, str) and mode and mode != "plan" and len(prompt.strip()) >= th["plan_min_chars"]:
        # Building a plan already approved, or a message about a plan,
        # doesn't need another.
        planned = any(
            isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "ExitPlanMode"
            for r in records if r.get("type") == "assistant" for block in _blocks(r)
        )
        steps = _request_steps(prompt, coaching)
        if steps >= th["plan_steps"] and not planned and not _PLAN_WORD_RE.search(prompt):
            plan = ("plan_first", steps, {"steps": steps})
    return [hint for hint in (repeat, drip, stop, plan, vague, paste) if hint]


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
    """The coaching note this hook call should add for Claude, or ``""``
    (see :func:`coaching_for`)."""
    return coaching_for(payload, config, catalogue, config_dir, now, raw_len)[0]


def coaching_for(
    payload: dict, config: dict, catalogue: dict, config_dir: Path, now: datetime | None = None, raw_len: int = 0
) -> tuple[str, str]:
    """``(note, notice)`` for this hook call: the coaching note to add for
    Claude, and what to show you at once (Claude Code's ``systemMessage``;
    only the prompting hints have one), each ``""`` when there's none. At
    most one hint, the first that applies and isn't resting (see
    :func:`_gate`), in ``COACHING_HINTS`` order. Reads ``coaching.json``
    and the transcript; writes ``coach-state.json`` when a hint shows or
    a subagent's run was counted."""
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
    counted = False
    candidates: list = []
    tool = str(payload.get("tool_name") or "")
    if event == "UserPromptSubmit":
        path = payload.get("transcript_path")
        if not in_agent and isinstance(path, str) and path:
            records = _tail(path)
            candidates = [
                *_practice_hints(payload.get("prompt"), records, coaching, th, now, payload.get("permission_mode")),
                *_prompt_hints(records, th, now),
            ]
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
        note = f"{coaching['marker']}{coaching['version']} {hint}\n{coaching['text'][hint].format(**fields)}"
        notice = coaching.get("notice", {}).get(hint, "")
        return note, notice.format(**fields) if notice else ""
    if counted:
        _write_json(state_path, state)
    return "", ""


# -- Haiku writes the tags ------------------------------------------------------

#: How much of a transcript's end the excerpt is read from, and how much
#: when that holds no message of yours (a long turn).
_JUDGE_TAIL_BYTES = 512 * 1024
_JUDGE_LONG_TAIL_BYTES = 4 * 1024 * 1024

#: The tools that start a subagent.
_AGENT_TOOLS = ("Agent", "Task")

#: The most tool names the excerpt lists.
_JUDGE_TOOL_NAMES = 10

_TL_RE = re.compile(r"\[tl:([^\[\]\n]{0,400})\]", re.IGNORECASE)

#: A shell command that runs tests, by its runner at the start of one of
#: the command's parts (after any VAR=value, timeout or uv/poetry run):
#: pytest, Python's unittest, tox or nox, npm/yarn/pnpm/bun test, jest,
#: vitest, mocha, go, cargo or dotnet test, mvn or gradle test, rspec,
#: phpunit, make test. The group is its arguments.
_TEST_RUNNER_RE = re.compile(
    r"^(?:\w+=\S*\s+)*(?:timeout\s+\S+\s+|(?:uv|poetry|pipenv)\s+run\s+|npx\s+|bunx\s+)*"
    r"(?:pytest|py\.test|python3?\s+-m\s+(?:pytest|unittest)|tox|nox"
    r"|(?:npm|yarn|pnpm|bun)(?:\s+run)?\s+test|jest|vitest|mocha"
    r"|go\s+test|cargo\s+test|dotnet\s+test|mvnw?\s+test|(?:\./)?gradlew?\s+test|rspec|phpunit|make\s+test)"
    r"(?=\s|$)(.*)"
)
_COMMAND_PARTS_RE = re.compile(r"&&|\|\||;|\|")
#: What in a test command's arguments picks particular tests: a test file
#: or folder, a ``file::test`` id, a name filter, or (go, cargo) a package
#: or test name.
_TEST_TARGET_RE = re.compile(
    r"(?:^|\s)(?:-k\b|-t\b|--testNamePattern|--testPathPattern|-run\b|--grep\b|\S*::\S+"
    r"|\S*tests?/\S*|\S*test_\S+|\S+_test\.\w+|\S+\.(?:test|spec)\.\w+|\S*spec/\S*)"
)
_BARE_ARG_RE = re.compile(r"(?:^|\s)(?!-)(?!\./\.\.\.(?:\s|$))[\w./:-]+")


def _test_scope(command: str) -> str:
    """``"targeted"`` when ``command`` runs chosen tests, ``"full"`` when
    it runs a whole suite, ``""`` when it runs none."""
    scope = ""
    for part in _COMMAND_PARTS_RE.split(command):
        match = _TEST_RUNNER_RE.match(part.strip())
        if match is None:
            continue
        args = re.sub(r"\s+\d?>\S*", " ", match.group(1))  # redirections aren't targets
        chosen = _TEST_TARGET_RE.search(args) or (
            part.strip().split()[0] in ("go", "cargo") and _BARE_ARG_RE.search(args)
        )
        if chosen:
            return "targeted"
        scope = "full"
    return scope


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
    return not text.startswith(prefix) and not text.startswith(_NOT_TYPED_PREFIXES)


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
    ``plan_before`` (one earlier), and how many ``skills`` ran, ``files``
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
            elif name == "Bash" and isinstance(given.get("command"), str):
                first = given["command"].strip().split("\n")[0]
                if first and len(commands) < limits["commands"]:
                    commands.append(_cut(first, limits["command"]))
                for line in given["command"].split("\n"):
                    scope = _test_scope(line)
                    if scope:
                        tests[scope] = None
            elif name == "Skill" and isinstance(given.get("skill"), str) and _SKILL_RE.fullmatch(given["skill"]):
                skills.append(given["skill"])
            elif name in _AGENT_TOOLS:
                agents += 1
    if not reply:
        return "", ""
    plan_before = any(
        isinstance(b, dict) and b.get("type") == "tool_use" and (
            b.get("name") == "ExitPlanMode"
            or (b.get("name") in edit_tools and _is_plan_file((b.get("input") or {}).get("file_path")))
        )
        for r in main[:start] if r.get("type") == "assistant" for b in _blocks(r)
    )
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


def judge_tag(text, keys, judge: dict) -> str:
    """The ``key=word`` pairs of the last ``[tl: ...]`` in ``text`` whose
    key is one of ``keys`` and whose words are known ones, as the tag
    file keeps them (``task=bugfix brief=clear``); ``""`` without any. A
    skill name after ``would-help:`` is dropped."""
    matches = _TL_RE.findall(text) if isinstance(text, str) else []
    if not matches:
        return ""
    vocab = judge["vocab"]
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


def judge_job(payload: dict, config: dict, catalogue: dict, now: datetime | None = None) -> dict | None:
    """What the worker needs to ask Haiku for this turn's tag, or ``None``:
    not a ``Stop`` of the main session, Haiku doesn't write the tags,
    capture doesn't apply here, a Stop asked again, the call Haiku itself
    runs in, or nothing to tag."""
    judge = catalogue["judge"]
    if payload.get("hook_event_name") != "Stop" or os.environ.get(judge["env"]):
        return None
    if payload.get("stop_hook_active") or _in_subagent(payload):
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


def spawn_judge(config_dir: Path, job: dict) -> None:
    """Hand ``job`` to a worker (this script with ``--judge``) that
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
    command = [sys.executable, "-I", "-S", str(Path(__file__).resolve()), "--judge", "--config-dir", str(config_dir)]
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


def ask_haiku(job: dict, judge: dict, cwd: Path | None = None) -> dict:
    """Claude Code's JSON answer to ``job``, from ``claude -p`` on Haiku
    with no tools, settings, MCP servers or saved session: the excerpt on
    stdin (never on the command line, where other users could read it),
    the instructions as the system prompt. Raises ``FileNotFoundError``
    without a ``claude`` command, ``subprocess.TimeoutExpired``, or
    ``ValueError`` when the call fails."""
    import subprocess

    command = _claude_command()
    if command is None:
        raise FileNotFoundError("claude")
    env = dict(os.environ)
    env[judge["env"]] = "1"
    env["MAX_THINKING_TOKENS"] = str(int(judge.get("thinking_tokens") or 0))
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    args = [
        command, "-p", "--model", judge["model"], "--tools", "", "--setting-sources", "", "--strict-mcp-config",
        "--no-session-persistence", "--output-format", "json", "--system-prompt", job["system"],
    ]
    extra: dict = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    done = subprocess.run(
        args, input=job["excerpt"].encode("utf-8"), capture_output=True, timeout=judge["timeout_s"], env=env,
        cwd=str(cwd) if cwd is not None and cwd.is_dir() else None, **extra,
    )
    answer = json.loads(done.stdout.decode("utf-8", errors="replace") or "null")
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
    record: dict = {"ts": job["ts"], "reply": job["reply"]}
    try:
        answer = (ask or ask_haiku)(job, judge, config_dir)
    except FileNotFoundError:
        record["err"] = "no_cli"
    except subprocess.TimeoutExpired:
        record["err"] = "timeout"
    except (OSError, ValueError):
        record["err"] = "failed"
    else:
        tag = grounded(judge_tag(answer.get("result"), job["keys"], judge), job.get("facts"))
        if tag:
            record["tl"] = tag
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
    # The worker spawn_judge starts: ask Haiku for one turn's tag.
    parser.add_argument("--judge", action="store_true")
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
    if args.judge:
        job = json.loads(raw)
        if isinstance(job, dict):
            run_judge(config_dir, load_catalogue(), job)
        return
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
        job = judge_job(payload, config, catalogue)
        if job:
            spawn_judge(config_dir, job)
        return
    note = note_for(payload, config, catalogue, raw_len=len(raw))
    tip = notice = ""
    if coach:
        try:
            tip, notice = coaching_for(payload, config, catalogue, config_dir, raw_len=len(raw))
        except Exception:  # noqa: BLE001 - a coaching fault must not cost the capture note
            tip = notice = ""
    # One attachment for both: the capture note first, so its marker
    # opens it, and the parser splits the two at the coaching marker.
    text = "\n".join(part for part in (note, tip) if part)
    output: dict = {}
    if text:
        output["hookSpecificOutput"] = {"hookEventName": payload.get("hook_event_name"), "additionalContext": text}
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

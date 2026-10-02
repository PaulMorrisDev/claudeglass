"""Transcript scrubbing tool (WP12a): whitelist-rewrite a Claude Code
session directory (top-level transcript + subagents + workflows) into an
output directory that is safe to commit as a test fixture.

This is a *whitelist* rewrite, not a blacklist redaction pass: every
field in the output is either (a) one of a small set of fields the
project's plan names as safe to keep verbatim, (b) an id-shaped field
rehashed via HMAC-SHA256 into a same-length hex string (a random key by
default; ``--key-seed`` only for reproducible tests), (c) a small
numeric/boolean field, or (d) collapsed to a same-length run of the
character ``"x"`` (with a couple of documented exceptions below) so
downstream length-based accounting (``Event.size_chars``, tool_result
character counts, etc.) still works without ever storing the original
text. Anything not explicitly reconstructed here is dropped.

Deviations / judgement calls beyond the plan's literal wording (kept
here rather than silently decided, per project convention):

* The plan's id list names ``toolUseId``/``tool_use_id`` (the
  ``.meta.json`` field and a ``tool_result`` block's own field) but not
  a ``tool_use`` content block's own ``id``. That id is rehashed too --
  with the *same* HMAC function -- so a ``tool_use``/``tool_result``
  pair still joins correctly after scrubbing (parse.py's tool-result
  accounting depends on that join). Two occurrences of the same original
  id always rehash to the same value, since the transform is a pure
  function of (key, original value).
* The A2 user-string category prefixes (``<task-notification``,
  ``[Request interrupted``, ...) are preserved by first matching the
  known literal prefix constant in full (so ``str.startswith(...)``
  checks in ``events.py`` keep working on the scrubbed fixture), then
  extending the kept region to the next ``>`` or space in the
  *remainder* of the string (closing a variable-length tag), then
  x-filling the rest.
* ``compactMetadata.trigger`` and workflow phase ``detail`` are *not* on
  any keep-list in the plan, so they are dropped/replaced like any other
  unlisted field, even though ``events.py`` reads ``trigger`` when it is
  present -- the scrubbed fixture simply carries a blank trigger, which
  does not affect any WP12a test assertion.
* ``workflowPhase`` and a sidecar ``description`` keep one thing of their
  text: the canonical role word ``agent_roles`` finds in them. The phase
  is kept as that word alone (dropped when it has none); a description
  becomes that word, a space, then ``x``s to the original length (or
  plain ``x``s when it has no role word). Nothing else of either string
  survives, so the scrubbed meta still says what kind of agent it was
  without saying what it was asked to do.
* Directory/file names under the output (``<session_id>.jsonl``,
  ``agent-<hex>.jsonl``/``.meta.json``, ``wf_<hex>.json``) are also
  rehashed with the same key, mirroring ``discovery.py``'s layout
  exactly so the scrubbed fixture round-trips through
  ``discovery.find_sessions``/``find_subagents``/``load_meta`` unchanged.

Usage::

    python -m claudeglass.tools.scrub \\
        --session-dir "<project_dir>/<session_id>" --out OUT_DIR
    python -m claudeglass.tools.scrub --verify OUT_DIR
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import re
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .. import agent_roles, jsonl

SCRUB_TOOL_VERSION = 1

# -- id / string transforms ---------------------------------------------

#: Fields whose value is HMAC-rehashed (length-preserving hex) rather
#: than kept verbatim or dropped -- every id-shaped field the plan names,
#: found at the top level of a JSONL line.
_TOP_LEVEL_ID_KEYS: tuple[str, ...] = (
    "uuid",
    "parentUuid",
    "sessionId",
    "requestId",
    "parentAgentId",
    "runId",
)

#: Top-level string/bool/dict fields kept verbatim (by key name), found
#: directly on a JSONL line dict. Handled in ``_copy_common_fields``.
_TOP_LEVEL_STR_KEYS: tuple[str, ...] = ("type", "subtype", "timestamp", "version", "entrypoint", "operation")
_TOP_LEVEL_BOOL_KEYS: tuple[str, ...] = ("isCompactSummary", "isMeta", "isApiErrorMessage")
_TOP_LEVEL_ENUM_KEYS: tuple[str, ...] = (
    "toolDenialKind",
    "promptSource",
    "permissionMode",
    "effort",
    "perTurnEffort",
    "attributionMcpServer",
    "attributionMcpTool",
    "attributionSkill",
    "originalModel",
    "fallbackModel",
)

#: Every key ``scrub_line`` reconstructs explicitly -- anything else on a
#: line falls through to the generic "string -> x run / other -> drop"
#: rule so no field is silently forgotten either way.
_ALREADY_HANDLED_TOP_LEVEL_KEYS = frozenset(
    _TOP_LEVEL_ID_KEYS
    + _TOP_LEVEL_STR_KEYS
    + _TOP_LEVEL_BOOL_KEYS
    + _TOP_LEVEL_ENUM_KEYS
    + ("cwd", "origin", "error", "compactMetadata", "message", "attachment", "rendered", "retryAttempt")
)

#: The A2 user-string category markers (task/scheduled/slash-command
#: notifications, interrupts, compaction summaries) -- see events.py's
#: ``_SLASH_COMMAND_PREFIXES``/``_SCHEDULED_TASK_PREFIXES`` and the
#: task-notification/interrupt/compact-summary literals it checks via
#: ``str.startswith``.
_PREFIX_PRESERVE: tuple[str, ...] = (
    "<task-notification",
    "<command-name",
    "<local-command-stdout",
    "<local-command-caveat",
    "<scheduled-task",
    "<<autonomous-loop",
    "[SYSTEM NOTIFICATION",
    "[Request interrupted",
    "This session is being continued",
)

_COMMAND_VERB_RE = re.compile(r"^\S+")

#: A "verb" is only preserved when it's a bare word (letters/digits/
#: ``_``/``.``/``-`` only) -- a first token containing ``/``, ``\``,
#: ``:``, ``=``, ``$``, ``~`` or a quote is not a safe thing to keep
#: verbatim (e.g. a leading ``VAR="/c/Users/..."`` assignment embeds a
#: path in what looks like the first token), so the whole command falls
#: back to a full-length ``x`` run instead.
_SAFE_VERB_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _rehash_id(key: bytes, value: str) -> str:
    """HMAC-SHA256-rehash ``value`` into a same-length lowercase-hex
    string, deterministic given ``key`` so the same original value always
    rehashes to the same output (needed to keep e.g. a ``tool_use``/
    ``tool_result`` id pair joined after scrubbing).
    """
    if not value:
        return value
    digest = hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()
    length = len(value)
    out = digest
    counter = 0
    while len(out) < length:
        counter += 1
        out += hmac.new(key, f"{value}:{counter}".encode("utf-8"), hashlib.sha256).hexdigest()
    return out[:length]


def _hash_slug(value: str) -> str:
    """A short, non-length-preserving opaque slug for ``cwd`` and the
    manifest's source-session marker -- these are never re-joined against
    another rehashed field, so length preservation doesn't matter here.
    """
    return "proj-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _x_run(text: str) -> str:
    return "x" * len(text)


def _x_run_len(n: int) -> str:
    return "x" * max(n, 0)


def _scrub_description(description: str) -> str:
    """A sidecar ``description`` -> its canonical role word (if it has
    one), a space, then ``x``s so the whole is the original length; plain
    ``x``s when it has no role word. The word alone when the original is
    too short to hold the word and a space.
    """
    word = agent_roles.role_word(None, None, description)
    if word is None:
        return _x_run(description)
    if len(description) < len(word) + 1:
        return word
    return word + " " + _x_run_len(len(description) - len(word) - 1)


def _is_pure_x_run(s: str) -> bool:
    return len(s) >= 1 and all(ch == "x" for ch in s)


def _scrub_user_string(text: str) -> str:
    """Collapse a user-message string to same-length ``x``s, except for
    the A2 category markers: their literal prefix constant is kept in
    full (so classification keeps working after scrubbing), extended to
    the next ``>``/space in the remainder to close a variable-length tag,
    with the rest x-filled.
    """
    for prefix in _PREFIX_PRESERVE:
        if text.startswith(prefix):
            remainder = text[len(prefix) :]
            cut = None
            for i, ch in enumerate(remainder):
                if ch in (">", " "):
                    cut = i + 1
                    break
            keep_len = len(prefix) + (cut if cut is not None else 0)
            keep_len = min(keep_len, len(text))
            kept = text[:keep_len]
            return kept + ("x" * (len(text) - keep_len))
    return _x_run(text)


def _scrub_command(command: str) -> str:
    """Bash/PowerShell ``input.command`` -> its first whitespace-
    delimited token (the verb) followed by a same-length ``x`` run for
    the rest, so ``cmd_prefix``'s test-tool-prefix matching (e.g.
    ``pytest``) still has a chance to work on the scrubbed fixture, while
    every argument/path after the verb is destroyed.
    """
    match = _COMMAND_VERB_RE.match(command)
    verb = match.group(0) if match else ""
    if verb and _SAFE_VERB_RE.match(verb):
        return verb + ("x" * (len(command) - len(verb)))
    return "x" * len(command)


def _tool_result_content_length(content: Any) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                total += len(block["text"])
        return total
    return 0


# -- per-shape scrubbers ---------------------------------------------------


def _scrub_usage(usage: dict) -> dict:
    out: dict[str, Any] = {}
    for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"):
        value = usage.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[key] = value
    cache_creation = usage.get("cache_creation")
    if isinstance(cache_creation, dict):
        cc_out = {}
        for key in ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"):
            value = cache_creation.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                cc_out[key] = value
        if cc_out:
            out["cache_creation"] = cc_out
    details = usage.get("output_tokens_details")
    if isinstance(details, dict):
        value = details.get("thinking_tokens")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out["output_tokens_details"] = {"thinking_tokens": value}
    server_tool_use = usage.get("server_tool_use")
    if isinstance(server_tool_use, dict):
        stu_out = {}
        for key in ("web_search_requests", "web_fetch_requests"):
            value = server_tool_use.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                stu_out[key] = value
        if stu_out:
            out["server_tool_use"] = stu_out
    return out


def _scrub_tool_input(name: str | None, input_obj: dict) -> dict:
    if name in ("Bash", "PowerShell"):
        command = input_obj.get("command")
        if isinstance(command, str):
            return {"command": _scrub_command(command)}
        return {"command": ""}
    return {"_len": len(json.dumps(input_obj, sort_keys=True, ensure_ascii=False))}


def _scrub_content_block(block: Any, hmac_key: bytes) -> dict | None:
    if not isinstance(block, dict):
        return None
    btype = block.get("type")
    if not isinstance(btype, str):
        return None
    out: dict[str, Any] = {"type": btype}
    if btype == "tool_use":
        if isinstance(block.get("id"), str):
            out["id"] = _rehash_id(hmac_key, block["id"])
        name = block.get("name")
        if isinstance(name, str):
            out["name"] = name
        input_obj = block.get("input")
        out["input"] = _scrub_tool_input(name if isinstance(name, str) else None, input_obj if isinstance(input_obj, dict) else {})
    elif btype == "tool_result":
        if isinstance(block.get("tool_use_id"), str):
            out["tool_use_id"] = _rehash_id(hmac_key, block["tool_use_id"])
        length = min(_tool_result_content_length(block.get("content")), 2000)
        out["content"] = _x_run_len(length)
        if isinstance(block.get("is_error"), bool):
            out["is_error"] = block["is_error"]
    elif btype == "text":
        text = block.get("text")
        if isinstance(text, str):
            out["text"] = _scrub_user_string(text)
    # image / thinking / other block types: type only, no content kept.
    return out


def _scrub_message(message: dict, hmac_key: bytes) -> dict:
    out: dict[str, Any] = {}
    if isinstance(message.get("role"), str):
        out["role"] = message["role"]
    if isinstance(message.get("id"), str):
        out["id"] = _rehash_id(hmac_key, message["id"])
    if isinstance(message.get("model"), str):
        out["model"] = message["model"]
    if isinstance(message.get("stop_reason"), str):
        out["stop_reason"] = message["stop_reason"]
    if isinstance(message.get("usage"), dict):
        out["usage"] = _scrub_usage(message["usage"])
    content = message.get("content")
    if isinstance(content, str):
        out["content"] = _scrub_user_string(content)
    elif isinstance(content, list):
        blocks = [b for b in (_scrub_content_block(item, hmac_key) for item in content) if b is not None]
        out["content"] = blocks
    return out


def _scrub_compact_metadata(meta: dict) -> dict:
    out: dict[str, Any] = {}
    for key in ("preTokens", "postTokens", "cumulativeDroppedTokens", "durationMs"):
        value = meta.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[key] = value
    return out


def _copy_common_fields(d: dict, out: dict, hmac_key: bytes) -> None:
    for key in _TOP_LEVEL_STR_KEYS:
        value = d.get(key)
        if isinstance(value, str):
            out[key] = value
    for key in _TOP_LEVEL_ID_KEYS:
        value = d.get(key)
        if isinstance(value, str):
            out[key] = _rehash_id(hmac_key, value)
    cwd = d.get("cwd")
    if isinstance(cwd, str):
        out["cwd"] = _hash_slug(cwd)
    for key in _TOP_LEVEL_BOOL_KEYS:
        value = d.get(key)
        if isinstance(value, bool):
            out[key] = value
    for key in _TOP_LEVEL_ENUM_KEYS:
        value = d.get(key)
        if isinstance(value, str):
            out[key] = value
    retry = d.get("retryAttempt")
    if isinstance(retry, (int, float)) and not isinstance(retry, bool):
        out["retryAttempt"] = retry
    origin = d.get("origin")
    if isinstance(origin, dict) and isinstance(origin.get("kind"), str):
        out["origin"] = {"kind": origin["kind"]}
    error = d.get("error")
    if isinstance(error, dict) and error.get("status") is not None:
        out["error"] = {"status": error["status"]}
    compact_metadata = d.get("compactMetadata")
    if isinstance(compact_metadata, dict):
        scrubbed = _scrub_compact_metadata(compact_metadata)
        if scrubbed:
            out["compactMetadata"] = scrubbed


def scrub_line(d: dict, hmac_key: bytes) -> dict:
    """Whitelist-rewrite one raw JSONL line dict into its scrubbed form.

    Always returns a valid (possibly near-empty) dict -- an
    unrecognised/malformed line never raises and never becomes an
    unparsable line in the scrubbed output, it just carries less.
    """
    out: dict[str, Any] = {}
    _copy_common_fields(d, out, hmac_key)

    message = d.get("message")
    if isinstance(message, dict):
        out["message"] = _scrub_message(message, hmac_key)

    attachment = d.get("attachment")
    if isinstance(attachment, dict) and isinstance(attachment.get("type"), str):
        out["attachment"] = {"type": attachment["type"]}

    rendered = d.get("rendered")
    if isinstance(rendered, str):
        out["rendered"] = _x_run(rendered)
    elif isinstance(rendered, list):
        # Real lines carry ``rendered`` as ``[{"content": str}, ...]``;
        # keep each block's length so ``Event.size_chars`` still works.
        out["rendered"] = [
            {"content": _x_run(block["content"])}
            for block in rendered
            if isinstance(block, dict) and isinstance(block.get("content"), str)
        ]

    for key, value in d.items():
        if key in _ALREADY_HANDLED_TOP_LEVEL_KEYS:
            continue
        if isinstance(value, str):
            out[key] = _x_run(value)
        # dict/list/number/bool values on unlisted keys are dropped.
    return out


# -- meta.json / workflow json ------------------------------------------

_META_STR_KEEP_KEYS = ("agentType", "model", "requestShape")


def scrub_meta_json(d: dict, hmac_key: bytes) -> dict:
    """Whitelist-rewrite one subagent ``.meta.json`` sidecar.

    ``workflowPhase`` is kept only as its canonical role word and
    ``description`` as that word plus an ``x`` run (see
    ``_scrub_description``); neither string's own text survives.
    """
    out: dict[str, Any] = {}
    for key in _META_STR_KEEP_KEYS:
        value = d.get(key)
        if isinstance(value, str):
            out[key] = value
    spawn_depth = d.get("spawnDepth")
    if isinstance(spawn_depth, int) and not isinstance(spawn_depth, bool):
        out["spawnDepth"] = spawn_depth
    stopped_by_user = d.get("stoppedByUser")
    if isinstance(stopped_by_user, bool):
        out["stoppedByUser"] = stopped_by_user
    tool_use_id = d.get("toolUseId")
    if isinstance(tool_use_id, str):
        out["toolUseId"] = _rehash_id(hmac_key, tool_use_id)
    parent_agent_id = d.get("parentAgentId")
    if isinstance(parent_agent_id, str):
        out["parentAgentId"] = _rehash_id(hmac_key, parent_agent_id)
    workflow_phase = agent_roles.role_word(d.get("workflowPhase"), None, None)
    if workflow_phase is not None:
        out["workflowPhase"] = workflow_phase
    description = d.get("description")
    if isinstance(description, str):
        out["description"] = _scrub_description(description)
    worktree_branch = d.get("worktreeBranch")
    if isinstance(worktree_branch, str):
        out["worktreeBranch"] = _x_run(worktree_branch)
    elif isinstance(worktree_branch, bool):
        out["worktreeBranch"] = worktree_branch
    return out


#: Generic phase-name words allowed to survive verbatim, plus every form
#: ``agent_roles.FORMS`` accepts; anything else becomes ``phase-<index>``
#: -- the plan's "kept only if allowlisted else phase-N" rule for
#: workflow phase titles.
_PHASE_TITLE_ALLOWLIST = frozenset(
    {
        "plan",
        "planning",
        "implement",
        "implementation",
        "test",
        "testing",
        "review",
        "discovery",
        "verification",
        "setup",
        "cleanup",
        "docs",
        "documentation",
        "finalize",
        "research",
        "design",
    }
)


def _scrub_phase_title(title: Any, index: int) -> str:
    if isinstance(title, str):
        key = title.strip().lower()
        if key in _PHASE_TITLE_ALLOWLIST or key in agent_roles.FORMS:
            return title
    return f"phase-{index}"


def scrub_workflow_json(d: dict, hmac_key: bytes) -> dict:
    """Whitelist-rewrite one ``<session>/workflows/wf_*.json`` run file.

    ``script``, ``scriptPath``, ``taskId``, ``workflowName``,
    ``defaultModel``, ``logs``, ``summary``, ``result``,
    ``workflowProgress`` and phase ``detail`` are never read into the
    output at all (per ``workflows.py``'s own docstring, these are
    exactly the "prompts or results" that must never be stored).
    """
    out: dict[str, Any] = {}
    run_id = d.get("runId")
    if isinstance(run_id, str):
        out["runId"] = _rehash_id(hmac_key, run_id)
    status = d.get("status")
    if isinstance(status, str):
        out["status"] = status
    for key in ("agentCount", "durationMs", "startTime", "totalTokens", "totalToolCalls"):
        value = d.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[key] = value
    phases = d.get("phases")
    if isinstance(phases, list):
        out["phases"] = [
            {"title": _scrub_phase_title(entry.get("title") if isinstance(entry, dict) else None, i)}
            for i, entry in enumerate(phases)
        ]
    elif isinstance(phases, int) and not isinstance(phases, bool):
        out["phases"] = phases
    timestamp = d.get("timestamp")
    if isinstance(timestamp, str):
        out["timestamp"] = timestamp
    return out


# -- session-directory orchestration -------------------------------------

_AGENT_STEM_RE = re.compile(r"^(agent-)([0-9a-fA-F-]+)$")


def _rehash_agent_stem(key: bytes, stem: str) -> str:
    match = _AGENT_STEM_RE.match(stem)
    if match:
        return match.group(1) + _rehash_id(key, match.group(2))
    return "agent-" + _rehash_id(key, stem)


def _scrub_jsonl_file(src: Path, dst: Path, hmac_key: bytes, line_type_counts: dict[str, int]) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    out_lines: list[str] = []
    for _line_no, d in jsonl.iter_lines(src):
        line_type = d.get("type")
        if isinstance(line_type, str):
            line_type_counts[line_type] = line_type_counts.get(line_type, 0) + 1
        out_lines.append(json.dumps(scrub_line(d, hmac_key), ensure_ascii=False))
    text = "\n".join(out_lines)
    if out_lines:
        text += "\n"
    dst.write_text(text, encoding="utf-8")


def _scrub_meta_sidecar(src_meta: Path, dst_meta: Path, hmac_key: bytes) -> None:
    if not src_meta.exists():
        return
    try:
        raw = json.loads(src_meta.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    dst_meta.parent.mkdir(parents=True, exist_ok=True)
    dst_meta.write_text(json.dumps(scrub_meta_json(raw, hmac_key), ensure_ascii=False, indent=2), encoding="utf-8")


def scrub_session(session_dir: Path | str, out_dir: Path | str, hmac_key: bytes) -> dict:
    """Scrub one ``<project_dir>/<session_id>`` session (its sibling
    top-level ``<session_id>.jsonl``, its ``subagents/`` including
    workflow-nested agents, and its ``workflows/`` run files) into
    ``out_dir``, mirroring ``discovery.py``'s exact layout so the result
    parses unchanged, and writes ``out_dir/manifest.json``.

    Returns the manifest dict (also written to disk).
    """
    session_dir = Path(session_dir)
    out_dir = Path(out_dir)
    session_id = session_dir.name
    project_dir = session_dir.parent
    top_jsonl = project_dir / f"{session_id}.jsonl"

    out_dir.mkdir(parents=True, exist_ok=True)
    out_session_id = _rehash_id(hmac_key, session_id) if session_id else "session"

    line_type_counts: dict[str, int] = {}
    subagent_count = 0
    workflow_count = 0

    if top_jsonl.exists():
        _scrub_jsonl_file(top_jsonl, out_dir / f"{out_session_id}.jsonl", hmac_key, line_type_counts)

    subagents_dir = session_dir / "subagents"
    if subagents_dir.exists():
        for src_jsonl in sorted(subagents_dir.glob("agent-*.jsonl")):
            out_stem = _rehash_agent_stem(hmac_key, src_jsonl.stem)
            dst_jsonl = out_dir / out_session_id / "subagents" / f"{out_stem}.jsonl"
            _scrub_jsonl_file(src_jsonl, dst_jsonl, hmac_key, line_type_counts)
            subagent_count += 1
            _scrub_meta_sidecar(
                src_jsonl.with_name(src_jsonl.stem + ".meta.json"),
                out_dir / out_session_id / "subagents" / f"{out_stem}.meta.json",
                hmac_key,
            )

        workflows_subdir = subagents_dir / "workflows"
        if workflows_subdir.exists():
            for run_dir in sorted(p for p in workflows_subdir.iterdir() if p.is_dir()):
                out_run_id = _rehash_id(hmac_key, run_dir.name)
                for src_jsonl in sorted(run_dir.glob("agent-*.jsonl")):
                    out_stem = _rehash_agent_stem(hmac_key, src_jsonl.stem)
                    dst_jsonl = out_dir / out_session_id / "subagents" / "workflows" / out_run_id / f"{out_stem}.jsonl"
                    _scrub_jsonl_file(src_jsonl, dst_jsonl, hmac_key, line_type_counts)
                    subagent_count += 1
                    _scrub_meta_sidecar(
                        src_jsonl.with_name(src_jsonl.stem + ".meta.json"),
                        out_dir / out_session_id / "subagents" / "workflows" / out_run_id / f"{out_stem}.meta.json",
                        hmac_key,
                    )

    workflows_dir = session_dir / "workflows"
    if workflows_dir.exists():
        for wf_path in sorted(workflows_dir.glob("wf_*.json")):
            try:
                raw = json.loads(wf_path.read_text(encoding="utf-8", errors="replace"))
            except (OSError, ValueError):
                raw = {}
            if not isinstance(raw, dict):
                raw = {}
            out_name = "wf_" + _rehash_id(hmac_key, wf_path.stem[3:] if wf_path.stem.startswith("wf_") else wf_path.stem)
            dst = out_dir / out_session_id / "workflows" / f"{out_name}.json"
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(json.dumps(scrub_workflow_json(raw, hmac_key), ensure_ascii=False, indent=2), encoding="utf-8")
            workflow_count += 1

    manifest = {
        "schema": 1,
        "scrub_tool_version": SCRUB_TOOL_VERSION,
        "source_session_hash": _hash_slug(session_id),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + "000Z",
        "line_type_counts": line_type_counts,
        "subagent_count": subagent_count,
        "workflow_count": workflow_count,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return manifest


# -- verify ----------------------------------------------------------------

#: Field names known (by construction, above) to legitimately carry
#: alphanumeric content of 4+ chars -- ids (hex), enum-ish short strings,
#: model ids, tool/attachment/phase names, and the deliberately-truncated
#: cwd slug and Bash/PowerShell verb.
_VERIFY_SAFE_KEYS = frozenset(
    {
        "type",
        "subtype",
        "timestamp",
        "started",
        "finished",
        "role",
        "stop_reason",
        "model",
        "effort",
        "perTurnEffort",
        "entrypoint",
        "version",
        "promptSource",
        "permissionMode",
        "toolDenialKind",
        "kind",
        "attributionMcpServer",
        "attributionMcpTool",
        "attributionSkill",
        "name",
        "operation",
        "agentType",
        "requestShape",
        "status",
        "title",
        "originalModel",
        "fallbackModel",
        "cwd",
        "command",
        "uuid",
        "parentUuid",
        "sessionId",
        "requestId",
        "parentAgentId",
        "runId",
        "id",
        "toolUseId",
        "tool_use_id",
        "source_session_hash",
        "scrub_tool_version",
        "schema",
        "generated_at",
    }
)

_ALNUM_RUN_RE = re.compile(r"[A-Za-z0-9]{4,}")

#: The canonical role words ``scrub_meta_json`` may leave in a sidecar's
#: ``workflowPhase`` and ``description``.
_ROLE_WORDS = frozenset(agent_roles.FORMS.values())

_UNIVERSAL_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"[A-Za-z]:[\\/]"), "drive path"),
    (re.compile(r"/home/"), "/home/ path"),
    (re.compile(r"/Users/"), "/Users/ path"),
    (re.compile(r"/[a-zA-Z]/"), "/c/ MSYS path"),
    (re.compile(r"@"), "'@'"),
    (re.compile(r"http"), "'http'"),
)


def _is_role_word_value(value: str, key: str | None) -> bool:
    """True for exactly what ``scrub_meta_json`` writes under
    ``workflowPhase`` (a canonical role word) or ``description`` (a
    canonical role word alone, or followed by a space and an ``x`` run).
    """
    if key == "workflowPhase":
        return value in _ROLE_WORDS
    if key == "description":
        word, _, rest = value.partition(" ")
        return word in _ROLE_WORDS and (not rest or _is_pure_x_run(rest))
    return False


def _check_string_value(value: str, key: str | None, where: str, violations: list[str]) -> None:
    for pattern, label in _UNIVERSAL_PATTERNS:
        if pattern.search(value):
            violations.append(f"{where}: contains {label}: {value!r}")
    if any(value.startswith(p) for p in _PREFIX_PRESERVE):
        return
    if key in _VERIFY_SAFE_KEYS or _is_role_word_value(value, key):
        return
    for match in _ALNUM_RUN_RE.finditer(value):
        run = match.group(0)
        if _is_pure_x_run(run):
            continue
        violations.append(f"{where}: alphanumeric run outside allowlist: {run!r} in {value!r}")


def _walk_json(obj: Any, key: str | None, where: str, violations: list[str]) -> None:
    if isinstance(obj, str):
        _check_string_value(obj, key, where, violations)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _walk_json(v, k, f"{where}.{k}", violations)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            _walk_json(item, key, f"{where}[{i}]", violations)


def verify_dir(out_dir: Path | str) -> tuple[bool, list[str]]:
    """Independent privacy scan over an already-scrubbed directory: every
    ``.jsonl``/``.json`` file's string values are checked for a leaked
    alphanumeric run (4+ chars, not a pure ``x`` run, outside the safe-
    key allowlist) or an absolute-path/``@``/``http`` shape. Returns
    ``(ok, violations)``.
    """
    out_dir = Path(out_dir)
    violations: list[str] = []
    for path in sorted(out_dir.rglob("*")):
        if path.is_dir():
            continue
        if path.suffix == ".jsonl":
            try:
                raw_text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line_no, raw_line in enumerate(raw_text.splitlines(), start=1):
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    d = json.loads(raw_line)
                except ValueError:
                    violations.append(f"{path.name}:{line_no}: not valid JSON")
                    continue
                _walk_json(d, None, f"{path.name}:{line_no}", violations)
        elif path.suffix == ".json":
            try:
                d = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            except (OSError, ValueError):
                violations.append(f"{path.name}: not valid JSON")
                continue
            _walk_json(d, None, path.name, violations)
    return (len(violations) == 0, violations)


# -- CLI -------------------------------------------------------------------


def _resolve_key(key_seed: str | None) -> bytes:
    if key_seed:
        return hashlib.sha256(key_seed.encode("utf-8")).digest()
    return secrets.token_bytes(32)


def _parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="claudeglass-scrub", description=__doc__)
    parser.add_argument("--session-dir", help="path to <project_dir>/<session_id> to scrub")
    parser.add_argument("--out", help="output directory to write the scrubbed session into")
    parser.add_argument("--verify", metavar="OUT_DIR", help="run the privacy scan over an already-scrubbed directory")
    parser.add_argument(
        "--key-seed",
        default=None,
        help="deterministic HMAC key seed (tests only) -- omit for a random key",
    )
    return parser.parse_args(list(argv))


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)

    if args.verify:
        ok, violations = verify_dir(Path(args.verify))
        if ok:
            print(f"OK: {args.verify} passes the privacy scan")
            return 0
        for violation in violations[:50]:
            print(f"VIOLATION: {violation}")
        print(f"{len(violations)} violation(s) found in {args.verify}")
        return 1

    if not args.session_dir or not args.out:
        print("error: --session-dir and --out are required unless --verify is given", file=sys.stderr)
        return 2

    key = _resolve_key(args.key_seed)
    manifest = scrub_session(Path(args.session_dir), Path(args.out), key)
    print(f"Scrubbed session written to {args.out}")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "SCRUB_TOOL_VERSION",
    "scrub_line",
    "scrub_meta_json",
    "scrub_workflow_json",
    "scrub_session",
    "verify_dir",
    "main",
]

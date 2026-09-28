"""Test-only builders for synthetic Claude Code JSONL fixtures.

Not a package under test itself — imported directly by test modules that
need realistic-shaped transcript lines without depending on the parser
(which doesn't exist yet in WP0).
"""

from __future__ import annotations

import dataclasses
import itertools
import json
import re
from pathlib import Path
from typing import Any

_counter = itertools.count(1)


def elasticity_with_slope(slope: float = 0.5):
    """A minimal, *accepted* :class:`elasticity.ElasticityStats` whose
    weekly "usd" fit has the given percent-per-dollar ``slope`` -- enough
    for :func:`elasticity.express_in_window` to return a share, and so
    for :meth:`units.Units.money` to phrase a subscription amount as a
    weekly-usage-limit share rather than falling back to a plain
    list-price equivalent. Shared by every test module that needs a
    subscription :class:`units.Units` with a real fit (UX-2 / finding
    F3's "no bare '$' and no doubled 'about' under a subscription" test)."""
    from claudeglass import elasticity as elasticity_mod

    thresholds = elasticity_mod.ElasticityThresholds()
    fit = elasticity_mod.FitResult(
        window=thresholds.weekly_window,
        metric="usd",
        slope=slope,
        r2=0.9,
        n_pairs=10,
        residual_std=1.0,
        accepted=True,
        reason=None,
    )
    return elasticity_mod.ElasticityStats(fits={thresholds.weekly_window: {"usd": fit}}, thresholds=thresholds)


def turn_line(**overrides: Any) -> dict:
    """Build one assistant JSONL line with a realistic shape:
    ``type``, ``message.{id, model, usage{input_tokens,
    cache_creation_input_tokens, cache_read_input_tokens, output_tokens,
    cache_creation{ephemeral_5m_input_tokens, ephemeral_1h_input_tokens}},
    content}``, ``requestId``, ``uuid``, ``timestamp``.

    Convenience kwargs (all optional) flatten the common fields callers
    want to vary: ``message_id``, ``model``, ``input_tokens``,
    ``cache_creation_input_tokens``, ``cache_read_input_tokens``,
    ``output_tokens``, ``ephemeral_5m_input_tokens``,
    ``ephemeral_1h_input_tokens``, ``content``, ``request_id``, ``uuid``,
    ``timestamp``, ``speed`` (``usage.speed``, e.g. ``"fast"`` --
    omitted from ``usage`` entirely when left at its default ``None``,
    matching real JSONL where the field is absent rather than null on
    older/standard-speed replies). Any other keyword is merged into the
    top-level line dict as-is (e.g. ``isApiErrorMessage=True``), which
    also lets a caller override ``type`` or replace ``message`` wholesale.
    """
    n = next(_counter)

    message_id = overrides.pop("message_id", f"msg_{n:06d}")
    model = overrides.pop("model", "claude-sonnet-5")
    input_tokens = overrides.pop("input_tokens", 100)
    cache_creation_input_tokens = overrides.pop("cache_creation_input_tokens", 0)
    cache_read_input_tokens = overrides.pop("cache_read_input_tokens", 0)
    output_tokens = overrides.pop("output_tokens", 50)
    ephemeral_5m_input_tokens = overrides.pop("ephemeral_5m_input_tokens", 0)
    ephemeral_1h_input_tokens = overrides.pop("ephemeral_1h_input_tokens", 0)
    content = overrides.pop("content", [{"type": "text", "text": "ok"}])
    request_id = overrides.pop("request_id", f"req_{n:06d}")
    uuid = overrides.pop("uuid", f"uuid_{n:06d}")
    timestamp = overrides.pop("timestamp", "2026-09-18T12:00:00.000Z")
    speed = overrides.pop("speed", None)

    usage: dict[str, Any] = {
        "input_tokens": input_tokens,
        "cache_creation_input_tokens": cache_creation_input_tokens,
        "cache_read_input_tokens": cache_read_input_tokens,
        "output_tokens": output_tokens,
        "cache_creation": {
            "ephemeral_5m_input_tokens": ephemeral_5m_input_tokens,
            "ephemeral_1h_input_tokens": ephemeral_1h_input_tokens,
        },
    }
    if speed is not None:
        usage["speed"] = speed

    line: dict[str, Any] = {
        "type": "assistant",
        "message": {
            "id": message_id,
            "model": model,
            "usage": usage,
            "content": content,
        },
        "requestId": request_id,
        "uuid": uuid,
        "timestamp": timestamp,
    }
    line.update(overrides)
    return line


def write_jsonl(path: Path, dicts: list[dict]) -> None:
    """Write an iterable of dicts to ``path`` as newline-delimited JSON."""
    with open(path, "w", encoding="utf-8") as fh:
        for d in dicts:
            fh.write(json.dumps(d))
            fh.write("\n")


# -- WP1 additions: non-assistant line builders --------------------------
#
# ``turn_line``/``write_jsonl`` above are WP0. WP1 (jsonl.py, events.py,
# parse.py, discovery.py) needs realistic-shaped non-assistant lines too,
# for every EventKind/subtype in plan Appendix A2. Same convention:
# convenience kwargs for the common fields, ``**overrides`` merges into
# the top-level dict as-is for anything else.


def _base_line(line_type: str, **overrides: Any) -> dict:
    n = next(_counter)
    timestamp = overrides.pop("timestamp", "2026-09-18T12:00:00.000Z")
    uuid = overrides.pop("uuid", f"uuid_{n:06d}")
    line: dict[str, Any] = {
        "type": line_type,
        "timestamp": timestamp,
        "uuid": uuid,
        "sessionId": "session_test",
    }
    line.update(overrides)
    return line


def user_str_line(content: str, **overrides: Any) -> dict:
    """Build a ``type=user`` line whose ``message.content`` is a plain
    string (the shape used by compaction summaries, slash commands,
    scheduled-task/task-notification/interrupt markers, and genuine human
    text prompts).
    """
    message = overrides.pop("message", None)
    if message is None:
        message = {"role": "user", "content": content}
    line = _base_line("user", **overrides)
    line["message"] = message
    return line


def user_block_line(content: list[dict], **overrides: Any) -> dict:
    """Build a ``type=user`` line whose ``message.content`` is a block
    list (tool_result / text / image blocks)."""
    message = overrides.pop("message", None)
    if message is None:
        message = {"role": "user", "content": content}
    line = _base_line("user", **overrides)
    line["message"] = message
    return line


def tool_result_block(tool_use_id: str, content: str | list[dict], **overrides: Any) -> dict:
    """Build one ``tool_result`` content block for ``user_block_line``."""
    block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
    block.update(overrides)
    return block


def tool_use_block(name: str, tool_use_id: str, input: dict | None = None, **overrides: Any) -> dict:
    """Build one ``tool_use`` content block for ``turn_line(content=...)``."""
    block = {"type": "tool_use", "id": tool_use_id, "name": name, "input": input or {}}
    block.update(overrides)
    return block


def system_line(subtype: str, **overrides: Any) -> dict:
    """Build a ``type=system`` line (compact_boundary, api_error,
    model_refusal_fallback, local_command, stop_hook_summary)."""
    line = _base_line("system", **overrides)
    line["subtype"] = subtype
    return line


def attachment_line(attachment_type: str, rendered: str | None = None, **attachment_overrides: Any) -> dict:
    """Build a ``type=attachment`` line with ``attachment.type`` set and
    the rest of ``attachment_overrides`` merged into the nested
    ``attachment`` dict (e.g. ``addedNames=[...]`` for a delta type).
    ``rendered``, when given, becomes the top-level ``rendered`` field
    ``Event.size_chars`` is measured from, in the shape real transcripts
    use: a list of ``{"content": str}`` blocks.
    """
    line = _base_line("attachment")
    attachment = {"type": attachment_type}
    attachment.update(attachment_overrides)
    line["attachment"] = attachment
    if rendered is not None:
        line["rendered"] = [{"content": rendered}]
    return line


def queue_operation_line(operation: str, **overrides: Any) -> dict:
    """Build a ``type=queue-operation`` line."""
    line = _base_line("queue-operation", **overrides)
    line["operation"] = operation
    return line


def ignorable_line(line_type: str, **overrides: Any) -> dict:
    """Build a line whose top-level ``type`` is one of the plan's
    "ignored outright but counted" values (e.g. ``bridge-session``)."""
    return _base_line(line_type, **overrides)


# -- Independent-review follow-up: reusable privacy regex scan -----------
#
# Task 6's redaction fix (parse.py's ``_redact_paths``) removes absolute-
# and relative-path-shaped tokens, URLs, and "@"-bearing tokens from
# ``cmd_prefix``/``preceding_cmd_prefix``, but the privacy criterion is
# broader than that one field: no dataclass field anywhere in a
# ``TranscriptResult`` (or a rendered-report dataclass — ``Table``,
# ``Section``, ``Recommendation``, ``ReportMeta``, ...) should ever match
# a drive letter, a POSIX home path (``/home/`` or bare ``home/``), a
# Windows ``Users\``/``Users/`` path (absolute or relative), an MSYS/Git
# Bash drive path (``/c/...``), a bare "@", or a URL (aside from the one
# known-legitimate pricing source citation — see
# ``_PRIVACY_URL_ALLOWED_VALUES``). ``assert_privacy`` is the reusable
# scan for that, on top of test_privacy.py's existing length-based walk,
# and (per its own docstring) descends into every table cell, section
# note, and Recommendation field it reaches via a dataclass field.
#
# v0.2-exports review fix: also flags a slug-shaped username segment
# (``Users-<name>-``/``home-<name>-``) — the shape a raw, un-hashed,
# un-redacted project slug carries — so running the full suite after
# adding this check surfaces any other place a raw slug still leaks
# (see ``_PRIVACY_SLUG_USER_RE`` below).

_PRIVACY_DRIVE_RE = re.compile(r"[A-Za-z]:\\")
_PRIVACY_POSIX_HOME_RE = re.compile(r"/home/")
#: R4 fix: broadened from ``\Users\`` (absolute, backslash-only) to also
#: flag a *relative* Windows path with no leading separator
#: (``Users\paulm``, the shape ``cd Users\paulm\proj`` leaves if
#: redaction ever regresses) and the forward-slash form a normalized
#: path or a Bash-on-Windows tool might use (``Users/paulm``).
_PRIVACY_WIN_USERS_RE = re.compile(r"Users[\\/]")
#: R4 fix: bare "home/" (no leading slash) — the relative-path
#: counterpart to ``_PRIVACY_POSIX_HOME_RE``'s absolute ``/home/`` form.
_PRIVACY_BARE_HOME_RE = re.compile(r"home/")
#: MSYS/Git Bash drive form, e.g. ``/c/Dev/x`` — the same leak shape as
#: ``C:\`` but produced by a Bash tool call on a Windows machine.
_PRIVACY_MSYS_DRIVE_RE = re.compile(r"/[a-zA-Z]/")
_PRIVACY_AT_RE = re.compile(r"@")
#: Independent-review item 3: a URL is as identity-leaking as an
#: absolute path or a bare "@" (query strings, hostnames, tokens in the
#: path segment), so it gets the same forbidden-pattern treatment.
_PRIVACY_URL_RE = re.compile(r"https?://|www\.")

#: Review v0.2-exports locked decision: a slug-shaped username segment,
#: e.g. the ``Users-paulm-`` or ``home-paulm-`` fragment
#: ``discovery.slug_for``/``exports._redact_slug`` produce from a real
#: filesystem path before redaction. The allowed character class
#: (``[A-Za-z0-9_.]+``) deliberately excludes ``<``/``>``, so a properly
#: redacted segment (``Users-<user>-``) can never match this pattern —
#: there is no separate "unless it's <user>" exemption to code, the
#: character class itself is that exemption.
_PRIVACY_SLUG_USER_RE = re.compile(r"(?i)(^|-)(Users|home)-[A-Za-z0-9_.]+-")

#: Field names holding values that are allowed to contain the above
#: shapes by design, not by accident.
_PRIVACY_EXCLUDED_FIELDS = {
    # TranscriptMeta.path is the transcript's own source file path,
    # kept deliberately for provenance - never derived from message
    # content, so it's out of scope for this leak scan (mirrors
    # test_privacy.py's own _LONG_FIELD_ALLOWLIST treatment of "path").
    "path",
}

#: Field names where a bare "@" is a legitimate identifier shape (a
#: cloud-provider model id's Vertex "@YYYYMMDD" date suffix), not a
#: username/email leak - excluded from the "@" check only.
_PRIVACY_AT_SIGN_ALLOWED_FIELDS = {"model"}

#: R4 fix: parse.py's own ``@``-redaction marker (see ``_redact_paths``)
#: is a constant, known-safe string that legitimately contains "@" —
#: stripped out before the "@" check so a redacted cmd_prefix like
#: ``"ssh <user@host>"`` doesn't flag itself as the very leak it just
#: fixed, while a real, un-redacted "@" anywhere else in the same string
#: is still caught.
_PRIVACY_AT_MARKER = "<user@host>"

#: R4 fix: values that legitimately contain a URL (and, incidentally,
#: no "@") and are expected to appear verbatim in report output — the
#: pricing rate card's own source citation — excluded from the URL
#: check only, by exact string match rather than a broad field-name
#: exemption (this is the one specific string, not a whole field).
_PRIVACY_URL_ALLOWED_VALUES = {
    "https://platform.claude.com/docs/en/about-claude/pricing",
}


def assert_privacy(result) -> None:
    """Recursively scan ``result`` for string values shaped like an
    absolute path or username/email/URL leak: a drive letter (``C:\\``),
    a POSIX ``/home/`` path, a Windows ``\\Users\\`` path, an MSYS/Git
    Bash drive path (``/c/...``), a bare ``@``, or a URL (``https?://``
    / ``www.``). Raises via ``assert`` with every violation listed, so a
    failure names exactly which field/index and value tripped it.

    ``result`` may be a ``TranscriptResult`` (the original, most common
    shape - walks ``meta``, ``diagnostics``, every ``Turn`` in ``turns``,
    every ``Event`` in ``events``), or - independent-review item 3 -
    any other dataclass (``Table``, ``Section``, ``Recommendation``,
    ...), a plain ``list``/``tuple``, or a ``dict``. Nested lists/tuples
    reached through a dataclass field (e.g. ``Table.rows``, a
    ``list[list]``, or ``Recommendation.evidence``, a ``list[tuple]``)
    are walked all the way down, not just one level - the earlier
    version silently skipped a list-of-lists because it only recursed
    into an item when the item was itself a dataclass.

    Dict values reached through a dataclass field are not walked, matching
    test_privacy.py's scope note -- except ``Event.detail``, which is
    walked keys and values all the way down: it holds what events.py read
    from a line, so a raw value passed through would leak there. The
    others are small counters keyed by closed labels
    (``Diagnostics.agent_settings``, ...). A dict passed as ``result``
    itself (the top-level argument) *is* walked, since item 3 requires
    ``assert_privacy`` to accept a plain ``dict`` as input.
    """
    violations: list[str] = []

    def _check(value: str, where: str, field_name: str) -> None:
        if _PRIVACY_DRIVE_RE.search(value):
            violations.append(f"{where} matches a Windows drive path: {value!r}")
        if _PRIVACY_POSIX_HOME_RE.search(value) or _PRIVACY_BARE_HOME_RE.search(value):
            violations.append(f"{where} matches a /home/ path: {value!r}")
        if _PRIVACY_WIN_USERS_RE.search(value):
            violations.append(f"{where} matches a Users\\ path: {value!r}")
        if _PRIVACY_MSYS_DRIVE_RE.search(value):
            violations.append(f"{where} matches an MSYS drive path: {value!r}")
        if field_name not in _PRIVACY_AT_SIGN_ALLOWED_FIELDS and _PRIVACY_AT_RE.search(
            value.replace(_PRIVACY_AT_MARKER, "")
        ):
            violations.append(f"{where} contains '@': {value!r}")
        if _PRIVACY_URL_RE.search(value) and value not in _PRIVACY_URL_ALLOWED_VALUES:
            violations.append(f"{where} contains a URL: {value!r}")
        if _PRIVACY_SLUG_USER_RE.search(value):
            violations.append(f"{where} matches a slug-shaped username segment: {value!r}")

    def _walk_value(value, where: str, field_name: str) -> None:
        """Walk a value reached via a dataclass field (or the top-level
        argument): strings are checked, dataclasses/lists/tuples are
        recursed into fully, dicts are left alone (see docstring)."""
        if isinstance(value, str):
            _check(value, where, field_name)
        elif isinstance(value, (tuple, list)):
            for i, item in enumerate(value):
                _walk_value(item, f"{where}[{i}]", field_name)
        elif dataclasses.is_dataclass(value) and not isinstance(value, type):
            _walk_dataclass(value, where)
        # dict: intentionally not walked when reached through a field.

    def _walk_detail(value, where: str, key: str) -> None:
        if isinstance(value, str):
            _check(value, where, key)
        elif isinstance(value, dict):
            for k, v in value.items():
                if isinstance(k, str):
                    _check(k, f"{where}.<key>", "")
                _walk_detail(v, f"{where}[{k!r}]", k if isinstance(k, str) else key)
        elif isinstance(value, (list, tuple)):
            for i, item in enumerate(value):
                _walk_detail(item, f"{where}[{i}]", key)

    def _walk_dataclass(obj, where: str) -> None:
        for f in dataclasses.fields(obj):
            if f.name in _PRIVACY_EXCLUDED_FIELDS:
                continue
            value = getattr(obj, f.name)
            if f.name == "detail" and isinstance(value, dict):
                _walk_detail(value, f"{where}.detail", "")
                continue
            _walk_value(value, f"{where}.{f.name}", f.name)

    is_transcript_result = all(
        hasattr(result, attr) for attr in ("meta", "diagnostics", "turns", "events")
    )
    if is_transcript_result:
        _walk_value(result.meta, "meta", "meta")
        _walk_value(result.diagnostics, "diagnostics", "diagnostics")
        for i, turn in enumerate(result.turns):
            _walk_value(turn, f"turns[{i}]", "turns")
        for i, event in enumerate(result.events):
            _walk_value(event, f"events[{i}]", "events")
    elif isinstance(result, dict):
        for key, value in result.items():
            if isinstance(key, str):
                _check(key, "root.<key>", "")
            _walk_value(value, f"root[{key!r}]", "")
    else:
        _walk_value(result, "root", "")

    assert violations == [], violations


# -- Review v0.2 fix #3: a deep scan that does not stop at a dict boundary --
#
# ``assert_privacy`` above deliberately leaves a ``dict`` reached through a
# dataclass field unwalked (see its docstring) -- a reasonable exemption for
# the small, module-controlled counters it was written against
# (``Event.detail``, ``Diagnostics.agent_settings``), but it makes the guard
# blind to a snapshot-shaped ``dict`` passed in directly, since a dict's own
# *values* are themselves dicts (``settings_layers``, ``effective``,
# ``effective_provenance``, ``effective_agents``, ``claude_json``,
# ``content_layers``). ``assert_privacy_deep`` reuses the exact same
# forbidden-shape patterns and allowlists defined above -- it does not
# change what counts as a violation, only how far it looks -- and walks
# every dict, list, tuple and dataclass all the way down, checking both
# keys and values.

#: Deep-scan-only "@" exemption, built by adding to (never mutating)
#: ``assert_privacy``'s own ``_PRIVACY_AT_SIGN_ALLOWED_FIELDS``. Claude
#: Code's plugin identifier convention is ``<name>@<marketplace>`` (see
#: ``hooks/snapshot-config.py``'s ``_extract_enabled_plugins`` and its
#: ``settings_layers.user`` copy) -- fix #5 already caps every recorded
#: plugin name's length and masks a path/URL *shape*, so a bare "@" in
#: this one field is the plugin naming scheme itself, not a leak, the
#: same reasoning that already exempts "model" for a Vertex "@date"
#: suffix.
_PRIVACY_DEEP_AT_SIGN_ALLOWED_KEYS = _PRIVACY_AT_SIGN_ALLOWED_FIELDS | {
    "enabled_plugins",
    # COV-03 (P7a): the deep-merged, cross-layer counterpart of
    # enabled_plugins above -- same "name@marketplace" plugin-id shape.
    "effective_enabled_plugins",
}


def assert_privacy_deep(obj) -> None:
    """Recursively scan ``obj`` -- typically a raw snapshot ``dict`` (e.g.
    loaded straight from a written snapshot JSON file), but also any
    dataclass/list/tuple/dict nesting -- for the same forbidden shapes
    ``assert_privacy`` checks (a Windows drive path, a POSIX ``/home/``
    path, a Windows ``Users\\``/``Users/`` path, an MSYS drive path, a bare
    ``@``, or a URL), except it never stops at a ``dict`` boundary: every
    dict's keys *and* values are walked, at every depth. Raises via
    ``assert`` with every violation listed, so a failure names exactly
    which path/index/key and value tripped it.
    """
    violations: list[str] = []

    def _check(value: str, where: str, key_name: str) -> None:
        if _PRIVACY_DRIVE_RE.search(value):
            violations.append(f"{where} matches a Windows drive path: {value!r}")
        if _PRIVACY_POSIX_HOME_RE.search(value) or _PRIVACY_BARE_HOME_RE.search(value):
            violations.append(f"{where} matches a /home/ path: {value!r}")
        if _PRIVACY_WIN_USERS_RE.search(value):
            violations.append(f"{where} matches a Users\\ path: {value!r}")
        if _PRIVACY_MSYS_DRIVE_RE.search(value):
            violations.append(f"{where} matches an MSYS drive path: {value!r}")
        if key_name not in _PRIVACY_DEEP_AT_SIGN_ALLOWED_KEYS and _PRIVACY_AT_RE.search(
            value.replace(_PRIVACY_AT_MARKER, "")
        ):
            violations.append(f"{where} contains '@': {value!r}")
        if _PRIVACY_URL_RE.search(value) and value not in _PRIVACY_URL_ALLOWED_VALUES:
            violations.append(f"{where} contains a URL: {value!r}")

    def _walk(value, where: str, key_name: str) -> None:
        if key_name in _PRIVACY_EXCLUDED_FIELDS:
            return
        if isinstance(value, str):
            _check(value, where, key_name)
        elif isinstance(value, dict):
            for key, item in value.items():
                key_name_str = key if isinstance(key, str) else ""
                if isinstance(key, str):
                    _check(key, f"{where}.<key>", "")
                _walk(item, f"{where}[{key!r}]", key_name_str)
        elif isinstance(value, (list, tuple)):
            for i, item in enumerate(value):
                _walk(item, f"{where}[{i}]", key_name)
        elif dataclasses.is_dataclass(value) and not isinstance(value, type):
            for f in dataclasses.fields(value):
                _walk(getattr(value, f.name), f"{where}.{f.name}", f.name)

    _walk(obj, "root", "")
    assert violations == [], violations


#: The metrics an agent's note named before 0.11.0, when agents tagged their
#: own reports; Explore and Plan agents weren't asked about rules or briefs.
_OLD_AGENT_CODES = ("result", "retry", "fit", "rules", "agent_brief")


def old_agent_note_text(ids, agent_type: str = "") -> str:
    """The note an agent got before 0.11.0 for the metrics in ``ids``, as
    older transcripts hold it: what the parser reads is its marker's
    codes. ``""`` when none of them is an agent metric, as then."""
    from claudeglass import capture_catalogue

    skip = ("rules", "agent_brief") if agent_type in ("Explore", "Plan") else ()
    codes = [code for code in _OLD_AGENT_CODES if code in set(ids) and code not in skip]
    if not codes or agent_type in capture_catalogue.SKIP_AGENT_TYPES:
        return ""
    return (
        f"{capture_catalogue.NOTE_MARKER}{capture_catalogue.NOTE_VERSION} {','.join(codes)}\n"
        f"{capture_catalogue.NOTE_INTRO}\n"
        "End your final report with one line, [result: done|partial|blocked key=word ...], using only these words:"
    )


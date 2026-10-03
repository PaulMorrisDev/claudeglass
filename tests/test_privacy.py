"""Hard privacy rule (project plan, Architecture section; model.py module
docstring): no dataclass field ever holds message text, tool_result
content, a full file path, or a command longer than 40 characters.

This test walks every dataclass field produced by ``parse_transcript``
(and the ``discovery.load_meta``/``TranscriptMeta`` it's paired with)
across a battery of synthetic fixtures exercising every EventKind, every
system subtype, real-looking tool_use/tool_result content, and long
human-prompt text — and asserts no ``str`` field exceeds 64 characters
outside the documented allowlist, and ``cmd_prefix``/
``preceding_cmd_prefix`` never exceed 40.

Scope note: ``Event.detail`` *is* walked, keys and values, all the way
down: it is where events.py keeps what it reads from a line (retry and
spawn markers, prompt flags, capture-note codes, skill names), so an
accidental "just pass the raw value through" there would be a leak. The
other ``dict``-typed fields (``Diagnostics.ignored_line_types``,
``Classification.*_evidence``, …) are small counters keyed by closed
labels and are not walked key-by-key.

Independent-review follow-up (task 6): every fixture below is also run
through ``helpers.assert_privacy``, a second, shape-based scan (not
length-based) that asserts no field matches a Windows drive path
(``C:\\``), a POSIX ``/home/`` path, a Windows ``\\Users\\`` path, an
MSYS/Git Bash drive path (``/c/...``), or a bare ``@`` — the concrete
regressions a length cap alone wouldn't catch (e.g. a short absolute
path under 64 chars).
"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path

from claudeglass import cache
from claudeglass.discovery import load_meta
from claudeglass.model import PROMPT_FLAGS, Column, EventKind, Recommendation, Section, Table, TranscriptMeta
from claudeglass.parse import parse_transcript

from helpers import (
    assert_privacy,
    attachment_line,
    ignorable_line,
    queue_operation_line,
    system_line,
    tool_use_block,
    tool_result_block,
    turn_line,
    user_block_line,
    user_str_line,
    write_jsonl,
)

_MAX_STR_LEN = 64
_MAX_CMD_PREFIX_LEN = 40

#: Field names allowed to exceed 64 characters (plan Architecture
#: section's own allowlist, spelled out per WP1's brief): agent identity
#: strings, tool names, session/slug identifiers, attachment subkinds,
#: and the transcript file path itself.
_LONG_FIELD_ALLOWLIST = {
    "agent_type",
    "model",
    "tool_names",  # tuple[str, ...] — each element checked individually
    "session_id",
    "slug",
    "subkind",  # attachment.type values
    "path",  # TranscriptMeta.path only
}

_CMD_PREFIX_FIELDS = {"cmd_prefix", "preceding_cmd_prefix"}


def _walk(obj, violations: list[str], where: str) -> None:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        for f in dataclasses.fields(obj):
            value = getattr(obj, f.name)
            field_where = f"{where}.{f.name}"
            if f.name in _CMD_PREFIX_FIELDS:
                if isinstance(value, str) and len(value) > _MAX_CMD_PREFIX_LEN:
                    violations.append(f"{field_where} exceeds {_MAX_CMD_PREFIX_LEN} chars: {value!r}")
                continue
            if isinstance(value, str):
                if f.name not in _LONG_FIELD_ALLOWLIST and len(value) > _MAX_STR_LEN:
                    violations.append(f"{field_where} exceeds {_MAX_STR_LEN} chars: {value!r}")
            elif isinstance(value, (tuple, list)):
                for i, item in enumerate(value):
                    if isinstance(item, str):
                        if f.name not in _LONG_FIELD_ALLOWLIST and len(item) > _MAX_STR_LEN:
                            violations.append(
                                f"{field_where}[{i}] exceeds {_MAX_STR_LEN} chars: {item!r}"
                            )
                    else:
                        _walk(item, violations, f"{field_where}[{i}]")
            elif dataclasses.is_dataclass(value):
                _walk(value, violations, field_where)
            elif f.name == "detail" and isinstance(value, dict):
                _walk_detail(value, violations, field_where)
            # other dict-typed fields intentionally not walked — see module docstring.


def _walk_detail(value, violations: list[str], where: str, key: str = "") -> None:
    """``Event.detail``, keys and values, at every depth."""
    if isinstance(value, str):
        if key not in _LONG_FIELD_ALLOWLIST and len(value) > _MAX_STR_LEN:
            violations.append(f"{where} exceeds {_MAX_STR_LEN} chars: {value!r}")
    elif isinstance(value, dict):
        for k, v in value.items():
            if isinstance(k, str) and len(k) > _MAX_STR_LEN:
                violations.append(f"{where} key exceeds {_MAX_STR_LEN} chars: {k!r}")
            _walk_detail(v, violations, f"{where}[{k!r}]", k if isinstance(k, str) else key)
    elif isinstance(value, (list, tuple)):
        for i, item in enumerate(value):
            _walk_detail(item, violations, f"{where}[{i}]", key)


def _assert_no_violations(result) -> None:
    violations: list[str] = []
    _walk(result.meta, violations, "meta")
    _walk(result.diagnostics, violations, "diagnostics")
    for i, turn in enumerate(result.turns):
        _walk(turn, violations, f"turns[{i}]")
    for i, event in enumerate(result.events):
        _walk(event, violations, f"events[{i}]")
    assert violations == []
    # Independent-review follow-up (task 6): every fixture below also
    # goes through the absolute-path/username regex scan, not just the
    # length-based walk above. helpers.assert_privacy is the reusable
    # form of this same scan for later packages.
    assert_privacy(result)


def test_privacy_long_human_prompt_is_never_stored(tmp_path: Path):
    long_prompt = "please refactor this function to handle the edge case " * 20  # > 64 chars
    lines = [
        user_str_line(long_prompt, origin={"kind": "human"}),
        turn_line(message_id="msg_1", input_tokens=100, output_tokens=10),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    _assert_no_violations(result)


def test_privacy_long_bash_command_is_capped_at_40_chars(tmp_path: Path):
    long_command = "echo " + ("x" * 200)
    lines = [
        turn_line(
            message_id="msg_1",
            input_tokens=100,
            output_tokens=10,
            content=[tool_use_block("Bash", "tu1", {"command": long_command})],
        ),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    assert result.turns[0].cmd_prefix is not None
    assert len(result.turns[0].cmd_prefix) <= _MAX_CMD_PREFIX_LEN
    _assert_no_violations(result)


def test_privacy_long_file_path_is_never_stored_only_edit_kind(tmp_path: Path):
    long_path = "C:/Users/paulm/very/deeply/nested/project/src/module/" + ("sub/" * 20) + "file.py"
    lines = [
        turn_line(
            message_id="msg_1",
            input_tokens=100,
            output_tokens=10,
            content=[tool_use_block("Write", "tu1", {"file_path": long_path, "content": "..."})],
        ),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    assert result.turns[0].edit_kind == "real"
    _assert_no_violations(result)


_MSYS_DRIVE_RE = re.compile(r"/[a-z]/")


def test_privacy_msys_drive_path_is_never_stored(tmp_path: Path):
    # Git Bash on Windows renders "C:\Dev\x" as "/c/Dev/x"; that shape
    # must be redacted out of cmd_prefix just like the C:\ form is.
    lines = [
        turn_line(
            message_id="msg_1",
            input_tokens=100,
            output_tokens=10,
            content=[tool_use_block("Bash", "tu1", {"command": "cd /c/Dev/secret_project && pytest"})],
        ),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    assert result.turns[0].cmd_prefix == "cd <path> && pytest"
    assert not _MSYS_DRIVE_RE.search(result.turns[0].cmd_prefix)
    _assert_no_violations(result)


def test_privacy_tool_result_text_is_never_stored(tmp_path: Path):
    long_result_text = "here is a large file dump " * 500
    lines = [
        turn_line(
            message_id="msg_1",
            input_tokens=100,
            output_tokens=10,
            content=[tool_use_block("Read", "tu1", {"file_path": "C:/x.py"})],
        ),
        user_block_line([{"type": "tool_result", "tool_use_id": "tu1", "content": long_result_text}]),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    assert result.tool_result_chars["Read"] == len(long_result_text)
    _assert_no_violations(result)


def test_privacy_a_malformed_skill_name_never_reaches_skills_invoked(tmp_path: Path):
    # SEC-P3: a Skill tool_use's own "skill" input is free text Claude
    # controls. This value is short and has no drive-letter/URL/@ shape,
    # so neither the length cap above nor assert_privacy's shape scan
    # would catch it on their own -- SKILL_NAME_PATTERN is the actual
    # gate (a space isn't in its allowed character set), and it must
    # keep a string like this out of Turn.skills_invoked altogether.
    lines = [
        turn_line(
            message_id="msg_1",
            input_tokens=100,
            output_tokens=10,
            content=[tool_use_block("Skill", "tu1", {"skill": "leak project codename"})],
        ),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    assert result.turns[0].skills_invoked == ()
    _assert_no_violations(result)


def test_privacy_mcp_instructions_delta_text_is_never_stored(tmp_path: Path):
    huge_instructions = "## some-mcp-server\nlots of instruction text here " * 200
    lines = [
        attachment_line(
            "mcp_instructions_delta",
            addedNames=["some-mcp-server"],
            addedBlocks=[huge_instructions],
            removedNames=[],
        ),
        turn_line(message_id="msg_1", input_tokens=100, output_tokens=10),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    _assert_no_violations(result)
    for event in result.events:
        assert "instruction text" not in repr(event.detail)


def test_privacy_every_event_kind_fixture(tmp_path: Path):
    """Broad sweep: one line per EventKind, all through the same walk."""
    lines = [
        turn_line(message_id="msg_1", input_tokens=100, output_tokens=10),
        user_str_line("a human prompt " * 10, origin={"kind": "human"}),
        user_str_line("<command-name>review</command-name>"),
        user_str_line("<scheduled-task>nightly</scheduled-task>"),
        user_str_line("[Request interrupted by user]"),
        user_str_line("(notification)", origin={"kind": "task-notification"}),
        user_str_line("hey", origin={"kind": "peer"}),
        user_str_line("(denied)", toolDenialKind="user-rejected"),
        user_str_line("[Image #1]", isMeta=True),
        user_block_line([{"type": "tool_result", "tool_use_id": "tu-x", "content": "ok " * 50}]),
        system_line("compact_boundary", compactMetadata={"trigger": "auto", "preTokens": 1000}),
        system_line("api_error", error={"status": 529}),
        system_line("model_refusal_fallback", originalModel="claude-opus-5", fallbackModel="claude-sonnet-5"),
        system_line("local_command"),
        system_line("stop_hook_summary"),
        attachment_line("hook_success", rendered="x" * 300),
        attachment_line("model", identity={"modelId": "claude-sonnet-5"}),
        attachment_line("thinking_stripped", scope="session"),
        attachment_line("deferred_tools_delta", addedNames=["Read", "Grep"], removedNames=[]),
        attachment_line("total_tokens_reminder"),
        attachment_line("environment", rendered="y" * 500),
        attachment_line("queued_command"),
        attachment_line("an_unclassified_future_type"),
        # Parser-signals batch (SURV-4/5/6, PARSER_VERSION 19): the three
        # new line shapes this phase adds, in the same broad sweep.
        attachment_line("thinking_drop", newlyDropped={"reason": "prefix_mismatch", "blockCount": 2, "turnCount": 1}),
        attachment_line("task_status", status="completed", taskType="local_bash"),
        attachment_line("structured_output", data={"ok": True}),
        ignorable_line("cost-state", totalCostUSD=1.23, hasUnknownModelCost=False),
        turn_line(message_id="msg_2", input_tokens=50, output_tokens=5),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    assert len(result.events) >= 20
    _assert_no_violations(result)


# -- Parser-signals batch (SURV-4/5/6/7, PARSER_VERSION 19): dedicated ---
# -- fixtures for each new field/kind, per the phase brief's own          -
# -- "add privacy fixtures for each new field or kind" requirement.       -


def test_privacy_thinking_drop_never_carries_identifying_fields(tmp_path: Path):
    # newlyDropped can carry far more than reason/blockCount/turnCount in
    # the real corpus (request ids, model names, the actual dropped block
    # hashes/content) -- _thinking_drop_detail must only ever pick out the
    # three closed fields, never pass the rest through.
    lines = [
        attachment_line(
            "thinking_drop",
            newlyDropped={
                "reason": "prefix_mismatch",
                "blockCount": 3,
                "turnCount": 1,
                "first": "here is the actual thinking text that was dropped",
                "last": "and the final dropped block's text too",
                "clientChange": True,
                "blockHashes": ["deadbeef" * 8, "cafebabe" * 8],
                "requestId": "req_secret_12345",
                "querySource": "user-secret-source",
                "model": "claude-opus-5-secret-snapshot",
            },
        ),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    events = [e for e in result.events if e.subkind == "thinking_drop"]
    assert len(events) == 1
    detail = events[0].detail
    assert set(detail) <= {"reason", "blockCount", "turnCount"}
    assert detail["reason"] == "prefix_mismatch"
    forbidden = ("first", "last", "clientChange", "blockHashes", "requestId", "querySource", "model", "dropped text")
    for token in forbidden:
        assert token not in repr(detail)
    _assert_no_violations(result)


def test_privacy_thinking_drop_unknown_reason_becomes_other(tmp_path: Path):
    lines = [
        attachment_line("thinking_drop", newlyDropped={"reason": "some-new-internal-reason-code", "blockCount": 1}),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    events = [e for e in result.events if e.subkind == "thinking_drop"]
    assert events[0].detail["reason"] == "other"
    assert "some-new-internal-reason-code" not in repr(events[0].detail)
    _assert_no_violations(result)


def test_privacy_task_status_never_reaches_description_or_paths(tmp_path: Path):
    # Mirrors test_privacy_a_malformed_skill_name_never_reaches_skills_invoked:
    # a task_status line carries plenty of identifying/free-text fields in
    # the real corpus (description, deltaSummary, outputFilePath, shell) --
    # _task_status_detail must only ever surface the two closed-vocabulary
    # words, never these.
    long_path = "C:\\Users\\paulm\\secret-project\\output\\result.json"
    lines = [
        attachment_line(
            "task_status",
            status="completed",
            taskType="local_bash",
            description="refactor the auth module to fix the login bug for jane.doe@acme.com",
            deltaSummary="changed 14 files, added retry logic",
            outputFilePath=long_path,
            shell="/bin/bash -c 'cat ~/.ssh/id_rsa'",
        ),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    events = [e for e in result.events if e.kind == EventKind.TASK_STATUS]
    assert len(events) == 1
    detail = events[0].detail
    assert set(detail) <= {"status", "task_type"}
    assert detail == {"status": "completed", "task_type": "local_bash"}
    forbidden = ("refactor", "jane.doe", "acme.com", "changed 14 files", "Users", "paulm", "ssh", "id_rsa")
    for token in forbidden:
        assert token not in repr(detail)
    _assert_no_violations(result)


def test_privacy_task_status_unknown_words_become_other(tmp_path: Path):
    lines = [
        attachment_line("task_status", status="some-future-status", taskType="some-future-type"),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    events = [e for e in result.events if e.kind == EventKind.TASK_STATUS]
    assert events[0].detail == {"status": "other", "task_type": "other"}
    _assert_no_violations(result)


def test_privacy_structured_output_data_is_never_stored_only_its_size(tmp_path: Path):
    long_free_text = "this is the actual structured payload content " * 20
    lines = [
        attachment_line(
            "structured_output",
            data={"summary": long_free_text, "path": "C:\\Users\\paulm\\secret\\out.json"},
        ),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    events = [e for e in result.events if e.kind == EventKind.STRUCTURED_OUTPUT]
    assert len(events) == 1
    event = events[0]
    assert not event.detail
    assert event.size_chars is not None and event.size_chars > 0
    assert "structured payload content" not in repr(event)
    assert "Users" not in repr(event)
    _assert_no_violations(result)


def test_privacy_cost_state_total_is_always_a_plain_float(tmp_path: Path):
    # totalCostUSD is a line straight from Claude Code's own on-disk
    # transcript, not validated input -- a malicious/malformed value must
    # never end up stored verbatim (e.g. as a string) on TranscriptMeta.
    lines = [
        ignorable_line("cost-state", totalCostUSD="1.23; DROP TABLE users", hasUnknownModelCost=False),
        ignorable_line("cost-state", totalCostUSD=4.56, hasUnknownModelCost=True),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    meta = result.meta
    # The malformed first line is ignored (not int/float); the second,
    # well-formed line is the last one seen and wins.
    assert meta.cc_cost_usd == 4.56
    assert isinstance(meta.cc_cost_usd, float)
    assert meta.cc_cost_has_unknown_model is True
    assert "DROP TABLE" not in repr(meta)
    _assert_no_violations(result)


def test_privacy_unknown_line_type_is_sanitised_not_stored_verbatim(tmp_path: Path):
    malicious_type = "evil<script>alert(1)</script>" + ("x" * 100)
    lines = [
        ignorable_line(malicious_type),
        ignorable_line("also-not-a-real-type"),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    unknown = result.parser_notes.get("unknown_line_types", {})
    assert malicious_type not in unknown
    assert "<script>" not in repr(result.parser_notes)
    # Bucketed to "other" (fails the closed token-pattern) rather than
    # dropped, so the count is still visible.
    assert unknown.get("other", 0) >= 1
    for key in unknown:
        assert len(key) <= 40
        assert "<" not in key and ">" not in key
    _assert_no_violations(result)


def test_privacy_image_and_document_blocks_never_leak_bytes_or_paths(tmp_path: Path):
    # A 1x1 PNG (valid header, tiny) plus a bogus/oversized "image" and a
    # document block -- content_block_size must only ever produce counts
    # (sized into size_chars, or bucketed into unsized_blocks), never the
    # base64 payload, decoded bytes, or any path-shaped string.
    tiny_png_b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    lines = [
        turn_line(
            message_id="msg_1",
            input_tokens=100,
            output_tokens=10,
            content=[tool_use_block("Read", "tu1", {"file_path": "x"})],
        ),
        user_block_line(
            [
                tool_result_block(
                    "tu1",
                    [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": tiny_png_b64}},
                        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "not-real-pdf-bytes"}},
                        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "not-a-real-image"}},
                    ],
                )
            ]
        ),
        user_str_line(
            "look at this",
            message={
                "role": "user",
                "content": [
                    {"type": "text", "text": "look at this"},
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": tiny_png_b64},
                    },
                ],
            },
        ),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    blob = repr(result.parser_notes) + repr(result.events) + repr(result.tool_result_chars)
    assert tiny_png_b64 not in blob
    assert "not-real-pdf-bytes" not in blob
    assert "not-a-real-image" not in blob
    # Only counts, keyed by closed block-type labels.
    for counts in result.parser_notes.get("unsized_blocks", {}).values():
        assert isinstance(counts, int)
    _assert_no_violations(result)


# -- Independent-review item 3: assert_privacy inspects table cells and --
# -- sections, not just TranscriptResult's own dataclass fields. ---------


def test_assert_privacy_flags_a_windows_path_in_a_table_cell():
    # The earlier assert_privacy only recursed into a list item when the
    # item was itself a dataclass, so Table.rows (list[list[str | int]])
    # was never actually scanned - a leaking cell passed silently.
    table = Table(
        name="workstyle_by_hour",
        title="Workstyle by hour",
        columns=[Column(key="hour", label="Hour", kind="int"), Column(key="note", label="Note")],
        rows=[[9, "normal"], [14, "cwd=C:\\Users\\paulm\\secret_project"]],
    )
    try:
        assert_privacy(table)
    except AssertionError as exc:
        assert any("Users" in v for v in exc.args[0])
    else:
        raise AssertionError("assert_privacy should have flagged the leaking table cell")


def test_assert_privacy_walks_section_tables_and_notes():
    leaking_table = Table(
        name="t",
        title="T",
        columns=[Column(key="k", label="K")],
        rows=[["cd /home/paulm/project && ls"]],
    )
    section = Section(key="workstyle", title="Workstyle", tables=[leaking_table], notes=["fine note"])
    try:
        assert_privacy(section)
    except AssertionError as exc:
        assert any("/home/" in v for v in exc.args[0])
    else:
        raise AssertionError("assert_privacy should have flagged the leaking nested table")

    clean_section = Section(key="workstyle", title="Workstyle", tables=[], notes=["nothing to see here"])
    assert_privacy(clean_section)  # no raise


def test_assert_privacy_walks_recommendation_evidence_tuples():
    rec = Recommendation(
        id="r1",
        title="Move off ad-hoc paths",
        action="Stop hardcoding paths",
        evidence=[("cmd_prefix", "cd C:\\Users\\paulm\\proj", "workstyle_by_hour", "14")],
    )
    try:
        assert_privacy(rec)
    except AssertionError as exc:
        assert any("Users" in v for v in exc.args[0])
    else:
        raise AssertionError("assert_privacy should have flagged the leaking evidence tuple")


def test_assert_privacy_flags_url_in_table_cell():
    table = Table(
        name="t",
        title="T",
        columns=[Column(key="k", label="K")],
        rows=[["curl https://x.example/a"]],
    )
    try:
        assert_privacy(table)
    except AssertionError as exc:
        assert any("URL" in v for v in exc.args[0])
    else:
        raise AssertionError("assert_privacy should have flagged the URL in the table cell")


def test_assert_privacy_accepts_a_plain_dict():
    assert_privacy({"count": 3, "label": "fine"})
    try:
        assert_privacy({"cmd": "cd C:\\Users\\paulm\\proj"})
    except AssertionError as exc:
        assert any("Users" in v for v in exc.args[0])
    else:
        raise AssertionError("assert_privacy should have flagged the leaking dict value")


# -- R4: parse-time redaction must survive every renderer -----------------
#
# _redact_paths runs once, at parse time, on cmd_prefix/preceding_cmd_prefix
# (see parse.py's module docstring) — so the digest cache never stores the
# raw prefix and every renderer downstream (markdown/json/html/csv) only
# ever sees the already-redacted text. This is the fixture-driven,
# full-pipeline proof of that: a relative Windows path with no drive
# letter, an ssh user@host target, and an email address, none of which
# the pre-R4 regexes caught. Two of the five turns are engineered to trip
# RE-CACHE detection (big ctx, ~0 cache_read, 45 minutes after the turn
# that ran the sensitive command) so each command actually surfaces as a
# preceding_cmd_prefix in the recache section's top-command-prefix table
# — this exercises the redaction through a real rendered report, not just
# parse_transcript in isolation.


def test_redacted_commands_never_leak_through_any_renderer(tmp_path: Path):
    from claudeglass.config import Config
    from claudeglass.corpus import load_corpus
    from claudeglass.pricing import load_pricing
    from claudeglass.render.csv_out import write_csv_dir
    from claudeglass.render.html import render_html
    from claudeglass.render.json_out import render_json
    from claudeglass.render.markdown import render_markdown
    from claudeglass.report import build_report

    cmd1 = "cd Users\\paulm\\secret-repo && ssh deploy@internal-build-01.acme.local make"
    cmd2 = "git commit -m 'fix login for jane.doe@acme.com'"

    lines = [
        turn_line(message_id="msg1", timestamp="2026-09-18T12:00:00.000Z", input_tokens=100, output_tokens=20),
        turn_line(
            message_id="msg2",
            timestamp="2026-09-18T12:01:00.000Z",
            input_tokens=100,
            output_tokens=20,
            content=[tool_use_block("Bash", "tu2", {"command": cmd1})],
        ),
        # 45-minute gap since msg2, big ctx, ~0 cache_read: a full-expiry
        # re-cache row whose preceding_cmd_prefix is msg2's (redacted)
        # command.
        turn_line(
            message_id="msg3",
            timestamp="2026-09-18T12:46:00.000Z",
            input_tokens=30000,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
            output_tokens=20,
        ),
        turn_line(
            message_id="msg4",
            timestamp="2026-09-18T12:47:00.000Z",
            input_tokens=100,
            output_tokens=20,
            content=[tool_use_block("Bash", "tu4", {"command": cmd2})],
        ),
        # Second 45-minute-gap full-expiry row, surfacing msg4's command.
        turn_line(
            message_id="msg5",
            timestamp="2026-09-18T13:32:00.000Z",
            input_tokens=30000,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
            output_tokens=20,
        ),
    ]
    project_dir = tmp_path / "proj-redact"
    project_dir.mkdir()
    write_jsonl(project_dir / "session-redact.jsonl", lines)

    corpus = load_corpus([project_dir])
    pricing = load_pricing()
    report = build_report(corpus, pricing, Config(), projects=("proj-redact",), window="w")

    # Sanity: the fixture actually tripped RE-CACHE detection, so the
    # commands really do reach a rendered table rather than sitting
    # unused in the corpus.
    recache_section = next(s for s in report.sections if s.key == "recache")
    summary_row = recache_section.tables[0].rows[0]
    assert summary_row[2] >= 2  # recache_turns

    assert_privacy(report)

    # "<user@host>" is _redact_paths's own redaction marker (see parse.py)
    # and legitimately contains "@" — stripped out (both raw and, for
    # HTML, html.escape'd as "&lt;user@host&gt;") before the "@" check so
    # the marker doesn't flag itself as the very leak it just fixed, while
    # a real, un-redacted "@" anywhere else in the output is still caught.
    # HTML's own <style>/<script> boilerplate (a dark-mode "@media" query,
    # the click-to-sort script) is never populated from report data, so
    # it's stripped too rather than tripping the same "@" check.
    forbidden = ("paulm", "acme", "@", "Users\\")

    def _assert_clean(name: str, text: str) -> None:
        scrubbed = text.replace("<user@host>", "").replace("&lt;user@host&gt;", "")
        if name == "html":
            scrubbed = re.sub(r"<style>.*?</style>", "", scrubbed, flags=re.DOTALL)
            scrubbed = re.sub(r"<script>.*?</script>", "", scrubbed, flags=re.DOTALL)
        for token in forbidden:
            assert token not in scrubbed, f"{name} output leaked {token!r}"

    rendered = {
        "markdown": render_markdown(report),
        "json": render_json(report),
        "html": render_html(report),
    }
    for name, text in rendered.items():
        _assert_clean(name, text)

    csv_dir = tmp_path / "csv-out"
    write_csv_dir(report, csv_dir)
    csv_text = "\n".join(p.read_text(encoding="utf-8") for p in csv_dir.rglob("*.csv"))
    _assert_clean("csv", csv_text)


# -- P10b: capture tags/notes, feedback and Diagnostics dict keys --------
#
# Skill names already have their own fixture above
# (test_privacy_a_malformed_skill_name_never_reaches_skills_invoked,
# SEC-P3) -- not duplicated here. These fill the remaining gaps: no
# fixture in this file had ever put a `[cg: ...]`/`[cg-fb: ...]` tag or a
# capture note on a real parsed Turn/Event before (test_capture_parse.py
# covers the string-level parsing with the looser assert_privacy scan;
# this is the integration proof under the stricter 64-char _walk), and
# none had exercised Diagnostics's own dict-typed fields, which (per the
# module docstring above) the generic walk deliberately does not open.


def test_privacy_capture_tag_never_carries_unknown_keys_or_paths(tmp_path: Path):
    # capture_tags.parse_reply_tags already drops unknown keys/words at
    # the string level (test_capture_parse.py's
    # test_unknown_keys_words_and_paths_are_dropped) -- this is the
    # integration proof that once a [cg: ...] tag lands on Turn.cap, the
    # walk (which recurses into CaptureTag like any other dataclass
    # field) never finds a leftover long or free-text value either.
    # SEC-P2 (capture_tags.filter_tag): a tag is trusted only for what
    # this transcript's own capture note asked for, so the note has to
    # precede the reply or the whole tag is dropped as unearned.
    lines = [
        attachment_line(
            "hook_additional_context",
            rendered="cg-cap v1 task,brief,level,shift,missing",
            content=["cg-cap v1 task,brief,level,shift,missing"],
            hookName="SessionStart",
            hookEvent="SessionStart",
            toolUseID="SessionStart",
        ),
        turn_line(
            message_id="msg_1",
            input_tokens=100,
            output_tokens=10,
            content=[
                {
                    "type": "text",
                    "text": (
                        "Fixed the bug.\n\n"
                        "[cg: task=bugfix brief=clear level=normal shift=build "
                        "secret=C:\\Users\\paulm\\secret.py "
                        "notes=jane.doe@acme.com missing=files,done]"
                    ),
                }
            ],
        ),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    turn = result.turns[0]
    assert turn.cap is not None
    assert (turn.cap.task, turn.cap.brief, turn.cap.level, turn.cap.shift) == ("bugfix", "clear", "normal", "build")
    assert turn.cap.missing == ("files", "done")
    forbidden = ("secret", "Users", "paulm", "jane.doe", "acme.com")
    for token in forbidden:
        assert token not in repr(turn.cap)
    _assert_no_violations(result)


def test_privacy_feedback_tag_never_carries_unknown_keys_or_free_text(tmp_path: Path):
    lines = [
        turn_line(
            message_id="msg_1",
            input_tokens=100,
            output_tokens=10,
            content=[
                {
                    "type": "text",
                    "text": (
                        "Rated the last piece of work.\n\n"
                        "[cg-fb: outcome=met slow=unclear,tools worth=yes helped=context "
                        "note=contact+jane.doe@acme.com path=C:\\Users\\paulm\\secret]"
                    ),
                }
            ],
        ),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    turn = result.turns[0]
    assert turn.feedback is not None
    assert turn.feedback.source == "tag"
    assert turn.feedback.outcome == "met"
    assert set(turn.feedback.slow) == {"unclear", "tools"}
    assert turn.feedback.worth == "yes"
    assert set(turn.feedback.helped) == {"context"}
    forbidden = ("acme.com", "jane.doe", "Users", "paulm", "note=", "path=")
    for token in forbidden:
        assert token not in repr(turn.feedback)
    _assert_no_violations(result)


def test_privacy_feedback_answers_drop_free_text_other(tmp_path: Path):
    # SEC-P1/SECURITY.md: /cg-feedback's four questions are checkbox-only
    # on the dashboard, but AskUserQuestion always offers a free-text
    # "Other" option -- feedback_from_answers must drop anything that
    # isn't one of the question's own closed labels, never store what
    # someone typed into "Other".
    question_text = "Did this piece of work deliver what you expected?"
    lines = [
        turn_line(
            message_id="msg_ask",
            input_tokens=50,
            output_tokens=10,
            content=[
                tool_use_block(
                    "AskUserQuestion",
                    "tu_ask",
                    {
                        "questions": [
                            {
                                "header": "TL outcome",
                                "question": question_text,
                                "options": ["Yes", "Partly", "No", "Stopped early"],
                            }
                        ]
                    },
                )
            ],
        ),
        user_block_line(
            [tool_result_block("tu_ask", "Recorded.")],
            toolUseResult={
                "questions": [{"header": "TL outcome", "question": question_text}],
                "answers": {
                    question_text: "Other: it leaked my ssh key at ~/.ssh/id_rsa and emailed paulm@example.com"
                },
            },
        ),
        turn_line(message_id="msg_2", input_tokens=20, output_tokens=5),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    # The answer lands on the turn that asked the question (msg_ask), the
    # same way tool_result_chars_by_tool/agent_result_chars do -- not on
    # the turn that follows it (msg_2).
    turn = next(t for t in result.turns if t.message_id == "msg_ask")
    assert turn.feedback is not None
    assert turn.feedback.source == "skipped"  # the free-text answer matched no known label
    assert turn.feedback.outcome is None
    forbidden = ("ssh", "id_rsa", "example.com", "leaked")
    for token in forbidden:
        assert token not in repr(turn.feedback)
    _assert_no_violations(result)


def test_privacy_capture_note_size_is_kept_never_its_text(tmp_path: Path):
    # A capture note's own content is a closed-vocabulary question list
    # today, but it's still hook-script output, not a trusted enum -- only
    # its length (Turn.cap_note_chars) and the closed detail (version,
    # codes, hook event) may ever reach a Turn/Event; the note text itself
    # must never.
    secret = "session token abc123 for jane.doe@acme.com at C:\\Users\\paulm\\project"
    note_text = f"cg-cap v1 task,brief,level -- {secret}"
    wrapped = f"<system-reminder>\nSessionStart hook additional context: {note_text}\n</system-reminder>"
    lines = [
        attachment_line(
            "hook_additional_context",
            rendered=wrapped,
            content=[note_text],
            hookName="SessionStart",
            hookEvent="SessionStart",
            toolUseID="SessionStart",
        ),
        turn_line(message_id="msg_1", input_tokens=100, output_tokens=10),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    turn = result.turns[0]
    assert turn.cap_note_chars == len(wrapped)
    note_events = [e for e in result.events if e.subkind == "capture_note"]
    assert len(note_events) == 1
    detail = note_events[0].detail
    assert set(detail) == {"v", "codes", "hook"}
    assert detail["v"] == 1
    assert detail["codes"] == ["task", "brief", "level"]
    blob = repr(result.events) + repr(result.turns)
    forbidden = ("acme.com", "jane.doe", "Users", "paulm", "abc123", "session token")
    for token in forbidden:
        assert token not in blob
    _assert_no_violations(result)


def test_privacy_diagnostics_ignored_line_type_is_sanitised_not_stored_verbatim(tmp_path: Path):
    # P10b fix: ignored_line_types (a Diagnostics dict field, populated
    # from the raw, attacker-controlled top-level `type` of a line the
    # parser deliberately drops -- an exact _IGNORABLE_TYPES member, an
    # ignorable file-history-*/artifact-* prefix match, or an UNKNOWN
    # event) used to store that `type` verbatim as a dict key. Its sibling
    # counter, parser_notes["unknown_line_types"], already sanitised the
    # same class of value (see
    # test_privacy_unknown_line_type_is_sanitised_not_stored_verbatim
    # above) -- ignored_line_types was the one place this had been missed.
    # Neither the generic _walk (dict fields aren't walked key-by-key,
    # see this module's docstring) nor assert_privacy would have caught
    # this, so it's asserted directly.
    malicious_prefix_type = "file-history-" + "<script>alert(1)</script>" + ("y" * 100)
    malicious_unknown_type = "evil<script>alert(2)</script>" + ("z" * 100)
    lines = [
        ignorable_line(malicious_prefix_type),
        ignorable_line(malicious_unknown_type),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    ignored = result.diagnostics.ignored_line_types
    assert malicious_prefix_type not in ignored
    assert malicious_unknown_type not in ignored
    assert "<script>" not in repr(ignored)
    for key in ignored:
        assert len(key) <= 40
        assert "<" not in key and ">" not in key
    _assert_no_violations(result)


def test_privacy_mcp_server_fields_hold_only_identifiers_and_numbers(tmp_path: Path):
    """The MCP server fields are dicts keyed by server names from the
    transcript, which the generic walk above doesn't enter: keys must be
    tool-name-shaped identifiers, never raw text or paths, and the values
    counts, lengths, capped tool-name suffixes or a closed status word."""
    hostile = "C:\\Users\\someone\\secret notes " + "x" * 200
    long_tool = "mcp__srv__" + "t" * 110
    lines = [
        attachment_line(
            "deferred_tools_delta", addedNames=[long_tool], addedLines=[long_tool], removedNames=[],
            surfacedNames=[], pendingMcpServers=[hostile], needsAuthMcpServers=["a b c"], failedMcpServers=[],
        ),
        attachment_line("mcp_instructions_delta", addedNames=[hostile], addedBlocks=["Private instructions. " * 10],
                        removedNames=[]),
        attachment_line("prompt_snapshot", tools=[{"name": "mcp__up__" + "u" * 100, "description": "Private.",
                                                    "schema": {}}]),
        turn_line(message_id="msg_1", content=[
            tool_use_block("ReadMcpResourceTool", "toolu_1", {"server": hostile, "uri": "file:///C:/Users/x"}),
        ]),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path), kind="top-level", session_id="s"))
    ident = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
    fields = [
        *(getattr(turn, name) for turn in result.turns for name in (
            "deferred_list_chars_by_server", "mcp_instruction_chars_by_server", "mcp_resource_servers")),
        result.upfront_definition_chars_by_server,
        result.mcp_tool_suffixes_by_server,
        result.mcp_connection_status,
    ]
    assert all(fields), "every field should hold something for this fixture"
    for field in fields:
        for key, value in field.items():
            assert ident.match(key), key
            if isinstance(value, int):
                continue
            if isinstance(value, str):
                assert value in {"pending", "needs sign-in", "failed to connect"}, value
                continue
            assert all(len(suffix) <= _MAX_STR_LEN and ident.match(suffix) for suffix in value), value
    assert "Private" not in repr(result) and "secret notes" not in repr(result)
    assert_privacy(result)


def test_privacy_meta_phase_and_description_leave_only_the_role_word(tmp_path: Path):
    """A workflow agent's phase and description are free text. The meta keeps
    one canonical role word and nothing they said: no sentence, no path, no
    fragment of either, in any field."""
    sentence = "Review the parser rewrite in C:/Users/someone/secret-project/src/parser.py and report back"
    run_dir = tmp_path / "session-p" / "subagents" / "workflows" / "wf_p"
    run_dir.mkdir(parents=True)
    meta_path = run_dir / "agent-p1.meta.json"
    meta_path.write_text(
        json.dumps(
            {
                "agentType": "workflow-subagent",
                "workflowPhase": sentence + " " + sentence,
                "description": "impl:/home/someone/private/notes.txt " + sentence,
            }
        )
    )
    write_jsonl(run_dir / "agent-p1.jsonl", [turn_line(message_id="msg_1", input_tokens=100, output_tokens=10)])

    meta = load_meta(meta_path)
    result = parse_transcript(run_dir / "agent-p1.jsonl", meta)

    for held in (meta, result.meta):
        assert held.role_word == "review"  # the phase wins; the word, not the sentence
        assert held.model_recorded is True
        strings = [
            value
            for f in dataclasses.fields(held)
            if f.name != "path"
            for value in (getattr(held, f.name),)
            if isinstance(value, str)
        ]
        for forbidden in ("parser", "rewrite", "someone", "secret", "notes", "report back", "C:/", "/home/"):
            assert not any(forbidden in value for value in strings), forbidden
            assert forbidden not in repr(dataclasses.replace(held, path=""))
    assert meta.description_len == len("impl:/home/someone/private/notes.txt " + sentence)
    _assert_no_violations(result)
    assert_privacy(meta)


# -- Parser signals (PARSER_VERSION 37): a message typed while Claude works --


_QUEUED_DETAIL_KEYS = {
    "origin", "chars", "has_paste", "unsized_blocks", "correction", "flags", "has_image", "steps",
    "vague", "ack", "go", "status", "adjust", "remind", "dup",
}


def test_privacy_queued_message_text_never_reaches_the_digest(tmp_path: Path):
    # Words, a path, an address and a link in what you typed while Claude
    # worked: the patterns read them in memory, and only counts and flags
    # are kept. The same goes for a peer's message, an enqueue line, the
    # typed copy of a queued message and a line you didn't type.
    secrets = [
        "zebrafish", "quokka", "narwhal", "pangolin", "axolotl", "capybara", "wombat", "ocelot",
        "mongoose", "platypus",
    ]
    long_text = f"actually rename {secrets[0]} to {secrets[1]} in C:/Users/someone/private/{secrets[2]}.py " + "x" * 120
    png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    lines = [
        user_str_line("refactor the parser", origin={"kind": "human"}, timestamp="2026-09-18T12:00:00.000Z"),
        turn_line(content=[tool_use_block("Bash", "tu_q", {"command": "ls"})], timestamp="2026-09-18T12:00:05.000Z"),
        attachment_line("queued_command", prompt=long_text, commandMode="prompt", origin={"kind": "human"},
                        timestamp="2026-09-18T12:00:10.000Z"),
        attachment_line("queued_command", prompt=f"I told you to email {secrets[3]}@example.com", commandMode="prompt"),
        attachment_line("queued_command", prompt=f"see https://example.com/{secrets[4]}?q=1 and continue",
                        commandMode="prompt", origin={"kind": "human"}),
        attachment_line("queued_command",
                        prompt=[{"type": "text", "text": f"it looks wrong {secrets[5]}"},
                                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": png}}],
                        commandMode="prompt", origin={"kind": "human"}),
        attachment_line("queued_command", prompt=f"please review {secrets[6]}", commandMode="prompt",
                        origin={"kind": "peer"}, isMeta=True),
        attachment_line("queued_command", prompt=f"<task-notification><task-id>t1</task-id>{secrets[7]}</task-notification>",
                        commandMode="task-notification"),
        queue_operation_line("enqueue", content=f"also fix {secrets[8]}"),
        user_str_line(f"The app was quit while you were working {secrets[9]}", origin={"kind": "human"}),
        user_block_line([tool_result_block("tu_q", "ok")]),
        turn_line(message_id="msg_q", input_tokens=50, output_tokens=5, timestamp="2026-09-18T12:00:40.000Z"),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    queued = [e for e in result.events if e.subkind == "queued_command"]
    assert [e.detail.get("origin") for e in queued] == ["human", "human", "human", "human", "peer", None]
    assert result.turns[-1].queued_prompts == 4

    encoded = json.dumps(cache.encode_result(result))
    blob = encoded + repr(result.events) + repr(result.turns) + repr(result.diagnostics) + repr(result.meta)
    for word in secrets + ["example.com", "someone", "private", "C:/Users", png]:
        assert word not in blob, word
    assert "x" * 20 not in blob
    for event in queued:
        assert set(event.detail) <= _QUEUED_DETAIL_KEYS | {"task_id", "status"}, event.detail
        for value in event.detail.get("flags", ()):
            assert value in PROMPT_FLAGS
    _assert_no_violations(result)


def test_privacy_message_flag_patterns_keep_a_flag_never_the_words(tmp_path: Path):
    # The go, status, adjust and remind patterns match words you typed:
    # what stays is a flag on the event and the turn.
    typed = [
        "continue", "how is it going?", "actually, rename the quokka widget", "I already told you about the narwhal",
    ]
    lines = []
    for n, text in enumerate(typed):
        lines.append(user_str_line(text, origin={"kind": "human"}, timestamp=f"2026-09-18T12:0{n}:00.000Z"))
        lines.append(turn_line(message_id=f"msg_{n}", timestamp=f"2026-09-18T12:0{n}:30.000Z"))
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    assert [(t.human_go, t.human_status, t.human_adjust, t.human_remind) for t in result.turns] == [
        (True, False, False, False), (False, True, False, False), (False, False, True, False),
        (False, False, False, True),
    ]
    blob = json.dumps(cache.encode_result(result)) + repr(result.events) + repr(result.turns)
    for word in ("quokka", "narwhal", "continue", "going", "rename", "already told"):
        assert word not in blob, word
    _assert_no_violations(result)


def test_privacy_plan_feedback_and_denial_text_leave_a_length_and_a_word_never_the_words(tmp_path: Path):
    # What you type into a rejected plan, a declined question or a deny
    # rule's message, and the text a hook or the classifier answers with,
    # is read in memory for its bucket and class: only counts, a length
    # and a closed word are kept.
    secrets = ["zebrafish", "quokka", "narwhal", "pangolin", "axolotl", "capybara", "wombat", "ocelot", "mongoose"]
    sent_back = (
        "The user doesn't want to proceed with this tool use. The tool use was rejected (eg. if it was a file "
        "edit, the new_string was NOT written to the file). To tell you how to proceed, the user said:\n"
    )
    feedback = f"why not use {secrets[0]} in C:/Users/someone/private/{secrets[1]}.py? mail {secrets[2]}@example.com"
    lines = [
        user_str_line("plan the change", origin={"kind": "human"}, timestamp="2026-09-18T12:00:00.000Z"),
        turn_line(content=[tool_use_block("ExitPlanMode", "tu_p", {"plan": f"1. edit {secrets[3]}.py"}),
                           tool_use_block("AskUserQuestion", "tu_q", {"questions": [{"question": secrets[4]}]}),
                           tool_use_block("Bash", "tu_h", {"command": "make"}),
                           tool_use_block("Bash", "tu_a", {"command": "make"}),
                           tool_use_block("Bash", "tu_r", {"command": "make"})],
                  timestamp="2026-09-18T12:00:05.000Z"),
        user_block_line([tool_result_block("tu_p", sent_back + feedback, is_error=True)],
                        toolDenialKind="user-rejected", timestamp="2026-09-18T12:00:10.000Z"),
        user_block_line([tool_result_block("tu_q", sent_back + f"skip it, {secrets[5]}", is_error=True)],
                        toolDenialKind="user-rejected", timestamp="2026-09-18T12:00:11.000Z"),
        user_block_line([tool_result_block("tu_h", f"PreToolUse:Bash hook error: [{secrets[6]}.sh] STOP",
                                           is_error=True)],
                        toolDenialKind="permission-rule", timestamp="2026-09-18T12:00:12.000Z"),
        user_block_line([tool_result_block("tu_a", f"Blocked by the classifier: {secrets[7]}", is_error=True)],
                        toolDenialKind="automode-blocked", timestamp="2026-09-18T12:00:13.000Z"),
        user_block_line([tool_result_block("tu_r", f"{secrets[8]}, not now", is_error=True)],
                        toolDenialKind="permission-rule", timestamp="2026-09-18T12:00:14.000Z"),
        user_str_line("[Request interrupted by user for tool use]", timestamp="2026-09-18T12:00:15.000Z"),
        turn_line(message_id="msg_after", input_tokens=50, output_tokens=5, timestamp="2026-09-18T12:00:40.000Z"),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))

    [event] = [e for e in result.events if e.kind == EventKind.PLAN_FEEDBACK]
    assert (event.size_chars, event.subkind, event.detail) == (len(feedback), "question", {})
    buckets = [e.detail["bucket"] for e in result.events if e.kind == EventKind.TOOL_DENIAL]
    assert buckets == ["plan_rejected", "question_declined", "hook_blocked", "auto_blocked", "refused"]
    assert result.turns[1].preceding_denials == {
        "plan_rejected": 1, "question_declined": 1, "hook_blocked": 1, "auto_blocked": 1, "refused": 1,
    }

    encoded = json.dumps(cache.encode_result(result))
    blob = encoded + repr(result.events) + repr(result.turns) + repr(result.diagnostics) + repr(result.meta)
    for word in secrets + ["example.com", "someone", "private", "C:/Users", "why not use"]:
        assert word not in blob, word
    for e in result.events:
        if e.kind in (EventKind.TOOL_DENIAL, EventKind.PLAN_FEEDBACK, EventKind.INTERRUPT):
            assert set(e.detail) <= {"bucket", "after"}, e.detail
    _assert_no_violations(result)


def test_privacy_reply_text_and_shell_reads_leave_yes_no_answers_and_sizes_never_the_words(tmp_path: Path):
    # A reply that owns a mistake, asks a question and disowns a tip, the
    # words and paths of a Read, a shell search and a test run, and what
    # came back from them: read in memory, and only a yes/no, a count, a
    # size and a closed word are kept.
    from claudeglass import parse

    parse.set_salt(b"p" * 32)
    secrets = ["zebrafish", "quokka", "narwhal", "pangolin", "axolotl", "capybara", "wombat", "ocelot"]
    persisted = (
        f"<persisted-output>\nOutput too large (90KB). Full output saved to: /tmp/{secrets[5]}/out.txt\n\n"
        f"Preview (first 2KB):\n{secrets[6]} line\n</persisted-output>"
    )
    lines = [
        user_str_line("why did you change that?", origin={"kind": "human"}, timestamp="2026-09-18T12:00:00.000Z"),
        turn_line(
            message_id="msg_a",
            content=[
                {"type": "text", "text": f"My mistake, I misread {secrets[0]}. That ClaudeGlass tip was a false positive."},
                tool_use_block("Read", "tu_r", {"file_path": f"C:/work/{secrets[1]}/notes.txt"}),
                tool_use_block("Bash", "tu_g", {"command": "git status"}),
                tool_use_block("Bash", "tu_s", {"command": f"cd C:/work/{secrets[2]} && grep -rn {secrets[3]} src/"}),
                tool_use_block("Bash", "tu_t", {"command": f"pytest tests/test_{secrets[4]}.py -q"}),
                {"type": "text", "text": f"Which of {secrets[7]} do you want? [cg: task=bugfix]"},
            ],
            timestamp="2026-09-18T12:00:05.000Z",
        ),
        user_block_line([tool_result_block("tu_r", persisted)], timestamp="2026-09-18T12:00:06.000Z"),
        user_block_line([tool_result_block("tu_g", "clean")], timestamp="2026-09-18T12:00:07.000Z"),
        user_block_line([tool_result_block("tu_s", f"src/a.py:1:{secrets[3]}")], timestamp="2026-09-18T12:00:08.000Z"),
        user_block_line([tool_result_block("tu_t", "1 passed")], timestamp="2026-09-18T12:00:09.000Z"),
        turn_line(message_id="msg_b", timestamp="2026-09-18T12:00:40.000Z"),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))

    turn = result.turns[0]
    assert (turn.reply_asked, turn.admit_candidate, turn.admit_caught, turn.tip_disowned) == (True, True, "user", False)
    assert (turn.read_target_chars, turn.shell_read_count, turn.shell_read_chars, turn.tests_run) == (
        (len(persisted),), 1, len(f"src/a.py:1:{secrets[3]}"), "targeted",
    )

    encoded = json.dumps(cache.encode_result(result))
    blob = encoded + repr(result.events) + repr(result.turns) + repr(result.diagnostics) + repr(result.meta)
    for word in secrets[:3] + secrets[4:] + ["/tmp/", "C:/work", "Preview", "misread", "false positive"]:
        assert word not in blob, word
    # Of the commands, only the first one's first words (the existing prefix) are kept.
    assert result.turns[0].cmd_prefix == "git status"
    _assert_no_violations(result)
    for t in result.turns:
        assert t.admit_caught in ("", "user", "self") and t.tests_run in ("", "targeted", "full")

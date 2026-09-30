"""Every EventKind resolves from a realistic line, precedence resolves as
specified, and the WP1 acceptance-criteria fixture shape (every user-
string category, every system subtype, 8+ attachment types incl.
thinking_stripped/model/deferred_tools_delta, a queue operation, an
ignorable type, an unparsable line, a truncated final line) round-trips
through both ``events.classify_line`` directly and a full
``parse_transcript`` pass.
"""

from __future__ import annotations

from pathlib import Path

from claudeglass import events
from claudeglass.model import EventKind, TranscriptMeta
from claudeglass.parse import parse_transcript

from helpers import (
    attachment_line,
    ignorable_line,
    queue_operation_line,
    system_line,
    tool_result_block,
    tool_use_block,
    turn_line,
    user_block_line,
    user_str_line,
)


# -- Direct classify_line coverage, one case per EventKind ---------------


def test_compact_boundary():
    line = system_line(
        "compact_boundary",
        compactMetadata={
            "trigger": "auto",
            "preTokens": 100000,
            "postTokens": 20000,
            "cumulativeDroppedTokens": 80000,
            "durationMs": 1500,
        },
    )
    event = events.classify_line(line)
    assert event.kind == EventKind.COMPACT_BOUNDARY
    assert event.pre_tokens == 100000
    assert event.post_tokens == 20000
    assert event.dropped_tokens == 80000
    assert event.duration_ms == 1500
    assert event.trigger == "auto"


def test_compact_summary_via_flag():
    line = user_str_line("(ignored)", isCompactSummary=True)
    assert events.classify_line(line).kind == EventKind.COMPACT_SUMMARY


def test_compact_summary_via_string_prefix():
    line = user_str_line("This session is being continued from a previous one.")
    assert events.classify_line(line).kind == EventKind.COMPACT_SUMMARY


def test_api_error():
    line = system_line("api_error", error={"status": 529}, retryAttempt=2)
    event = events.classify_line(line)
    assert event.kind == EventKind.API_ERROR
    assert event.detail == {"status": 529, "retryAttempt": 2}


def test_model_fallback():
    line = system_line(
        "model_refusal_fallback", originalModel="claude-opus-5", fallbackModel="claude-sonnet-5"
    )
    event = events.classify_line(line)
    assert event.kind == EventKind.MODEL_FALLBACK
    assert event.detail == {"originalModel": "claude-opus-5", "fallbackModel": "claude-sonnet-5"}


def test_local_command():
    line = system_line("local_command")
    assert events.classify_line(line).kind == EventKind.LOCAL_COMMAND


def test_hook_output_system_subtype():
    line = system_line("stop_hook_summary")
    assert events.classify_line(line).kind == EventKind.HOOK_OUTPUT


def test_hook_output_attachment_type():
    line = attachment_line("hook_success")
    assert events.classify_line(line).kind == EventKind.HOOK_OUTPUT


def test_hook_output_captures_the_hook_event_name_not_the_matcher():
    # SURV-HE: the event bucket is kept (closed vocabulary), the
    # matcher/tool-name suffix after ":" is always dropped (G7).
    line = attachment_line("hook_non_blocking_error", hookName="PreToolUse:Bash")
    event = events.classify_line(line)
    assert event.detail == {"hookName": "PreToolUse"}


def test_hook_output_unrecognised_hook_event_becomes_other():
    line = attachment_line("hook_blocking_error", hookName="SomeFutureEvent:Foo")
    assert events.classify_line(line).detail == {"hookName": "other"}


def test_hook_output_missing_hook_name_becomes_other():
    line = attachment_line("hook_success")
    assert events.classify_line(line).detail == {"hookName": "other"}


def test_hook_output_mcp_matched_hook_name_never_reaches_detail():
    # The exact G7 shape: an MCP tool's name in the matcher must never
    # survive into Event.detail, even bucketed under a real hook event.
    line = attachment_line("hook_non_blocking_error", hookName="PreToolUse:mcp__some_server__do_thing")
    detail = events.classify_line(line).detail
    assert detail == {"hookName": "PreToolUse"}
    assert "mcp__some_server__do_thing" not in str(detail)


def test_hook_output_capture_note_detail_has_no_hook_name_key():
    # The capture-note path keeps its own, narrower "hook" vocabulary
    # (_CAPTURE_NOTE_HOOKS) and must not also gain a "hookName" key.
    line = attachment_line(
        "hook_additional_context",
        hookName="SessionStart",
        hookEvent="SessionStart",
        content=["ClaudeGlass metrics capture (cg-cap v1 task): ..."],
    )
    event = events.classify_line(line)
    assert event.subkind == "capture_note"
    assert "hookName" not in event.detail


# -- CAP-9/F10: durationMs and the "own hook" flag ----------------------


def test_hook_output_keeps_the_real_duration():
    line = attachment_line("hook_success", hookName="SessionStart:startup", durationMs=140)
    assert events.classify_line(line).detail == {"hookName": "SessionStart", "durationMs": 140}


def test_hook_output_missing_duration_has_no_duration_key():
    line = attachment_line("hook_success", hookName="SessionStart:startup")
    detail = events.classify_line(line).detail
    assert "durationMs" not in detail


def test_hook_output_non_numeric_duration_is_dropped():
    line = attachment_line("hook_success", hookName="SessionStart:startup", durationMs="not-a-number")
    assert "durationMs" not in events.classify_line(line).detail


def test_hook_output_flags_a_call_that_ran_claudeglass_own_hook_script():
    command = '"C:\\Python311\\python.exe" -I -S "C:\\Users\\me\\scratch\\tl\\hooks\\capture-hook.py" --config-dir "C:\\Users\\me\\scratch\\tl"'
    line = attachment_line("hook_success", hookName="PostToolUse:Bash", durationMs=210, command=command)
    detail = events.classify_line(line).detail
    assert detail == {"hookName": "PostToolUse", "durationMs": 210, "capture": True}


def test_hook_output_a_third_party_hooks_command_is_never_flagged_as_capture():
    line = attachment_line(
        "hook_success", hookName="PostToolUse:Bash", durationMs=5, command='"/usr/bin/some-other-hook.sh"'
    )
    detail = events.classify_line(line).detail
    assert "capture" not in detail


def test_hook_output_command_string_never_reaches_detail():
    # Only a bool may ever come from `command` -- the path itself (which
    # can carry a username) must never land in Event.detail.
    command = 'python.exe "C:\\Users\\alice\\scratch\\tl\\hooks\\capture-hook.py"'
    line = attachment_line("hook_success", hookName="PostToolUse:Bash", command=command)
    detail = events.classify_line(line).detail
    assert detail == {"hookName": "PostToolUse", "capture": True}
    assert "alice" not in str(detail)
    assert "command" not in detail
    assert "\\" not in str(detail)


def test_cache_signal_model():
    line = attachment_line("model", identity={"modelId": "claude-sonnet-5"})
    event = events.classify_line(line)
    assert event.kind == EventKind.CACHE_SIGNAL
    assert event.subkind == "model"
    assert event.detail == {"modelId": "claude-sonnet-5"}


def test_cache_signal_thinking_stripped():
    line = attachment_line("thinking_stripped", scope="session")
    event = events.classify_line(line)
    assert event.kind == EventKind.CACHE_SIGNAL
    assert event.detail == {"scope": "session"}


def test_cache_signal_deferred_tools_delta_carries_counts_only():
    line = attachment_line(
        "deferred_tools_delta",
        addedNames=["Read", "Grep", "mcp__github__get_issue"],
        removedNames=["Write"],
    )
    event = events.classify_line(line)
    assert event.kind == EventKind.CACHE_SIGNAL
    assert event.subkind == "deferred_tools_delta"
    assert event.detail == {"added": 3, "removed": 1, "mcp_added": 1}


def test_cache_signal_agent_listing_delta_counts_only():
    line = attachment_line("agent_listing_delta", addedTypes=["a", "b", "c"], removedTypes=[])
    event = events.classify_line(line)
    assert event.detail == {"added": 3, "removed": 0}


def test_cache_signal_mcp_instructions_delta_never_carries_added_blocks_text():
    line = attachment_line(
        "mcp_instructions_delta",
        addedNames=["claude-in-chrome"],
        addedBlocks=["## claude-in-chrome\nfull instructions text here " * 50],
        removedNames=[],
    )
    event = events.classify_line(line)
    assert event.detail == {"added": 1, "removed": 0}
    assert "addedBlocks" not in repr(event.detail)
    assert "full instructions text" not in repr(event.detail)


def test_reminder():
    line = attachment_line("total_tokens_reminder")
    assert events.classify_line(line).kind == EventKind.REMINDER


def test_context_inject():
    line = attachment_line("environment", rendered="x" * 500)
    event = events.classify_line(line)
    assert event.kind == EventKind.CONTEXT_INJECT
    assert event.size_chars == 500


def test_rendered_block_list_is_measured():
    # Real transcripts carry ``rendered`` as a list of content blocks.
    line = attachment_line("environment")
    line["rendered"] = [{"content": "a" * 300}, {"content": "b" * 200}]
    assert events.classify_line(line).size_chars == 500


def test_size_falls_back_to_content_fields_without_rendered():
    skill = attachment_line("skill_listing", content="s" * 900, skillCount=12, names=["x"])
    event = events.classify_line(skill)
    assert event.size_chars == 900
    assert event.detail == {"count": 12}
    delta = attachment_line("deferred_tools_delta", addedNames=["a", "b"], addedLines=["a" * 40, "b" * 60])
    assert events.classify_line(delta).size_chars == 100
    memory = attachment_line("nested_memory", content={"path": "p", "content": "m" * 70})
    assert events.classify_line(memory).size_chars == 70


def test_instructions_split_by_file_type_without_paths():
    line = attachment_line(
        "instructions",
        files=[
            {"path": "C:/Users/u/.claude/CLAUDE.md", "type": "User", "content": "u" * 100},
            {"path": "C:/repo/CLAUDE.md", "type": "Project", "content": "p" * 300},
            {"path": "C:/x", "type": "Weird", "content": "w" * 5},
        ],
    )
    event = events.classify_line(line)
    assert event.size_chars == 405
    assert event.detail["count"] == 3
    assert event.detail["chars_by_type"] == {"User": 100, "Project": 300, "Other": 5}
    # No salt set: one record per file, with no path hash.
    assert event.detail["files"] == [
        {"type": "User", "scoped": False, "chars": 100},
        {"type": "Project", "scoped": False, "chars": 300},
        {"type": "Other", "scoped": False, "chars": 5},
    ]
    assert "C:" not in repr(event.detail)


def test_instruction_files_carry_a_salted_path_hash_never_the_path(monkeypatch):
    from claudeglass import parse

    monkeypatch.setattr(parse, "_SALT", b"s" * 32)
    line = attachment_line(
        "instructions", files=[{"path": "C:\\Repo\\CLAUDE.md", "type": "Project", "content": "p" * 40}]
    )
    record = events.classify_line(line).detail["files"][0]
    assert record["hash"] == parse.path_hash("c:/repo/claude.md", b"s" * 32)
    assert "Repo" not in repr(record) and "p" * 40 not in repr(record)


def test_nested_memory_records_a_path_scoped_rule(monkeypatch):
    from claudeglass import parse

    monkeypatch.setattr(parse, "_SALT", b"s" * 32)
    line = attachment_line(
        "nested_memory",
        path="C:/repo/.claude/rules/db.md",
        content={"path": "C:/repo/.claude/rules/db.md", "type": "Project", "globs": ["src/db/**"], "content": "r" * 80},
    )
    event = events.classify_line(line)
    assert event.detail["files"] == [
        {"type": "Project", "scoped": True, "chars": 80, "hash": parse.path_hash("C:/repo/.claude/rules/db.md", b"s" * 32)}
    ]


def test_skill_listing_keeps_names_and_sizes_not_descriptions():
    content = "- grill-me: Interview the user.\n- impeccable:impeccable: Design: polish UI.\n- stray: not in names\n"
    line = attachment_line(
        "skill_listing", content=content, skillCount=2, names=["grill-me", "impeccable:impeccable"]
    )
    detail = events.classify_line(line).detail
    assert detail["count"] == 2
    assert detail["skills"] == [
        {"name": "grill-me", "chars": len("- grill-me: Interview the user.")},
        {"name": "impeccable:impeccable", "chars": len("- impeccable:impeccable: Design: polish UI.")},
    ]
    assert "Interview" not in repr(detail)


def test_invoked_skills_records_each_skills_size():
    line = attachment_line("invoked_skills", skills=[{"name": "pdf", "path": "bundled:pdf", "content": "c" * 120}])
    detail = events.classify_line(line).detail
    assert detail == {"count": 1, "skills": [{"name": "pdf", "chars": 120}]}


def test_prompt_snapshot_sizes_go_to_detail_not_size_chars():
    line = attachment_line(
        "prompt_snapshot",
        systemPrompt=["x" * 1000, "y" * 500],
        tools=[{"name": "Read", "description": "d" * 50}, {"name": "Bash"}],
    )
    event = events.classify_line(line)
    assert event.size_chars is None
    assert event.detail["system_chars"] == 1500
    assert event.detail["tool_count"] == 2
    assert event.detail["tools_chars"] > 50


def test_context_inject_invoked_skills_counts_names():
    line = attachment_line("invoked_skills", names=["grill-me", "ai-tool"])
    event = events.classify_line(line)
    assert event.detail == {"count": 2}
    assert "grill-me" not in repr(event.detail)


def test_queue_operation_type():
    line = queue_operation_line("enqueue")
    event = events.classify_line(line)
    assert event.kind == EventKind.QUEUE_OPERATION
    assert event.subkind == "enqueue"


def test_queue_operation_attachment_type():
    line = attachment_line("queued_command")
    assert events.classify_line(line).kind == EventKind.QUEUE_OPERATION


def test_attachment_catch_all_for_unlisted_type():
    line = attachment_line("some_brand_new_type_not_in_any_table_row")
    event = events.classify_line(line)
    assert event.kind == EventKind.ATTACHMENT
    assert event.subkind == "some_brand_new_type_not_in_any_table_row"


def test_meta():
    line = user_str_line("[Image #1]", isMeta=True)
    event = events.classify_line(line)
    assert event.kind == EventKind.META
    assert event.subkind == "plain"


def test_meta_subkind_uses_origin_kind_when_present():
    line = user_str_line("(background)", isMeta=True, origin={"kind": "loop"})
    event = events.classify_line(line)
    assert event.kind == EventKind.META
    assert event.subkind == "loop"


def test_meta_subkind_uses_leading_tag_name_when_no_origin():
    line = user_str_line("<system-reminder>ignore this</system-reminder>", isMeta=True)
    event = events.classify_line(line)
    assert event.kind == EventKind.META
    assert event.subkind == "system-reminder"


def test_meta_subkind_falls_back_to_plain_without_tag_or_origin():
    line = user_str_line("just some meta text", isMeta=True)
    event = events.classify_line(line)
    assert event.kind == EventKind.META
    assert event.subkind == "plain"


def test_tool_denial():
    line = user_str_line("(denied)", toolDenialKind="user-rejected")
    event = events.classify_line(line)
    assert event.kind == EventKind.TOOL_DENIAL
    assert event.subkind == "user-rejected"


def test_tool_denial_from_a_token_saver_is_a_redirect_not_a_refusal():
    """Claude Code tags tokensave's JSON deny "permission-rule", the same
    as a deny rule you wrote (seen in a real v7.13.0 transcript); the
    reason's own words mark it as a redirect."""
    reason = (
        "PreToolUse:Grep hook error: STOP: This Grep targets a code file in a tokensave-indexed project and the "
        "pattern looks like a symbol name. Set TOKENSAVE_DISABLE_GREP_HOOK=1 to turn this off."
    )
    line = user_block_line(
        [{"type": "tool_result", "tool_use_id": "tu1", "content": reason, "is_error": True}],
        toolDenialKind="permission-rule",
    )
    event = events.classify_line(line)
    assert event.kind == EventKind.TOOL_DENIAL
    assert event.subkind == "saver-redirect"

    listed = user_block_line(
        [{"type": "tool_result", "tool_use_id": "tu1", "content": [{"type": "text", "text": reason}], "is_error": True}],
        toolDenialKind="permission-rule",
    )
    assert events.classify_line(listed).subkind == "saver-redirect"

    rule = user_block_line(
        [{"type": "tool_result", "tool_use_id": "tu1", "content": "Permission to use Bash has been denied.", "is_error": True}],
        toolDenialKind="permission-rule",
    )
    assert events.classify_line(rule).subkind == "permission-rule"


def test_tool_denial_beats_meta_flag():
    """Dispatch order (item 7): TOOL_DENIAL is tested before isMeta, so a
    line that is both a tool denial and flagged isMeta classifies as the
    more specific TOOL_DENIAL kind."""
    line = user_str_line("(denied)", isMeta=True, toolDenialKind="user-rejected")
    event = events.classify_line(line)
    assert event.kind == EventKind.TOOL_DENIAL
    assert event.subkind == "user-rejected"


def test_tool_result():
    line = user_block_line([{"type": "tool_result", "tool_use_id": "tu1", "content": "ok"}])
    assert events.classify_line(line).kind == EventKind.TOOL_RESULT


def test_tool_result_beats_meta_flag():
    """Dispatch order (item 7): TOOL_RESULT is tested before isMeta."""
    line = user_block_line(
        [{"type": "tool_result", "tool_use_id": "tu1", "content": "ok"}], isMeta=True
    )
    assert events.classify_line(line).kind == EventKind.TOOL_RESULT


def test_task_notification_via_origin():
    line = user_str_line("(notification)", origin={"kind": "task-notification"})
    assert events.classify_line(line).kind == EventKind.TASK_NOTIFICATION


def test_task_notification_via_string_prefix():
    line = user_str_line("<task-notification>done</task-notification>")
    assert events.classify_line(line).kind == EventKind.TASK_NOTIFICATION


def test_task_notification_beats_meta_flag():
    """Dispatch order (item 7): TASK_NOTIFICATION is tested before isMeta."""
    line = user_str_line("(notification)", isMeta=True, origin={"kind": "task-notification"})
    assert events.classify_line(line).kind == EventKind.TASK_NOTIFICATION


def test_peer_message():
    line = user_str_line("hey", origin={"kind": "peer"})
    assert events.classify_line(line).kind == EventKind.PEER_MESSAGE


def test_peer_message_beats_meta_flag():
    """Dispatch order (item 7): PEER_MESSAGE is tested before isMeta."""
    line = user_str_line("hey", isMeta=True, origin={"kind": "peer"})
    assert events.classify_line(line).kind == EventKind.PEER_MESSAGE


def test_slash_command():
    line = user_str_line("<command-name>review</command-name>")
    assert events.classify_line(line).kind == EventKind.SLASH_COMMAND


def test_scheduled_task_string_prefix():
    line = user_str_line("<scheduled-task>nightly</scheduled-task>")
    assert events.classify_line(line).kind == EventKind.SCHEDULED_TASK


def test_scheduled_task_origin_marker():
    line = user_str_line("(loop tick)", origin={"kind": "loop"})
    assert events.classify_line(line).kind == EventKind.SCHEDULED_TASK


def test_interrupt_string_prefix():
    line = user_str_line("[Request interrupted by user]")
    assert events.classify_line(line).kind == EventKind.INTERRUPT


def test_human_text_via_origin():
    line = user_str_line("please fix the bug", origin={"kind": "human"})
    assert events.classify_line(line).kind == EventKind.HUMAN_TEXT


def test_human_text_fallback_plain_string():
    line = user_str_line("please fix the bug")
    assert events.classify_line(line).kind == EventKind.HUMAN_TEXT


def test_unknown_for_unrecognised_type():
    line = ignorable_line("some-future-type-nobody-has-seen-yet")
    event = events.classify_line(line)
    assert event.kind == EventKind.UNKNOWN


def test_assistant_line_returns_none():
    assert events.classify_line(turn_line()) is None


def test_ignorable_type_returns_none():
    assert events.classify_line(ignorable_line("bridge-session")) is None
    assert events.classify_line(ignorable_line("last-prompt")) is None
    assert events.classify_line(ignorable_line("custom-title")) is None


def test_ignorable_prefix_families_return_none():
    assert events.classify_line(ignorable_line("file-history-snapshot")) is None
    assert events.classify_line(ignorable_line("artifact-created")) is None


# -- Precedence ------------------------------------------------------------


def test_precedence_human_text_beats_attachment():
    kinds = [
        events.classify_line(attachment_line("total_tokens_reminder")),
        events.classify_line(user_str_line("do the thing", origin={"kind": "human"})),
    ]
    assert events.primary_kind(kinds) == EventKind.HUMAN_TEXT


def test_precedence_compact_boundary_beats_everything():
    kinds = [
        events.classify_line(user_str_line("do the thing", origin={"kind": "human"})),
        events.classify_line(system_line("compact_boundary", compactMetadata={})),
    ]
    assert events.primary_kind(kinds) == EventKind.COMPACT_BOUNDARY


def test_precedence_high_band_cache_signal_beats_interrupt():
    kinds = [
        events.classify_line(user_str_line("[Request interrupted by user]")),
        events.classify_line(attachment_line("model", identity={"modelId": "claude-sonnet-5"})),
    ]
    assert events.primary_kind(kinds) == EventKind.CACHE_SIGNAL


def test_precedence_low_band_cache_signal_loses_to_slash_command():
    kinds = [
        events.classify_line(attachment_line("output_style")),
        events.classify_line(user_str_line("<command-name>review</command-name>")),
    ]
    assert events.primary_kind(kinds) == EventKind.SLASH_COMMAND


def test_primary_kind_of_empty_sequence_is_unknown():
    assert events.primary_kind([]) == EventKind.UNKNOWN


def test_task_notification_beats_plain_attachment_regression():
    """Regression: the seed's last-user-line attribution missed that a
    task-notification followed by attachments should still surface the
    notification as the primary cause, not whichever attachment happens
    to be nearest the turn.
    """
    kinds = [
        events.classify_line(user_str_line("<task-notification>done</task-notification>")),
        events.classify_line(attachment_line("total_tokens_reminder")),
        events.classify_line(attachment_line("batching_reminder_sent")),
    ]
    assert events.primary_kind(kinds) == EventKind.TASK_NOTIFICATION


# -- Full parse_transcript pass over one fixture with everything ----------


def test_full_fixture_every_kind_resolves_and_diagnostics_count_correctly(tmp_path: Path):
    lines = [
        turn_line(
            message_id="msg_1",
            input_tokens=100,
            output_tokens=10,
            content=[tool_use_block("Bash", "tu-err", {"command": "ls /nope"})],
        ),
        user_block_line([tool_result_block("tu-err", "No such file or directory", is_error=True)]),
        user_str_line("please fix the bug", origin={"kind": "human"}),
        user_str_line("<command-name>review</command-name>"),
        user_str_line("<scheduled-task>nightly</scheduled-task>"),
        user_str_line("[Request interrupted by user]"),
        user_str_line("This session is being continued from a previous one.", isCompactSummary=True),
        user_str_line("(notification)", origin={"kind": "task-notification"}),
        user_str_line("hey", origin={"kind": "peer"}),
        user_str_line("(denied)", toolDenialKind="user-rejected"),
        user_str_line("[Image #1]", isMeta=True),
        user_block_line([{"type": "tool_result", "tool_use_id": "tu-missing", "content": "ok"}]),
        system_line("compact_boundary", compactMetadata={"trigger": "auto"}),
        system_line("api_error", error={"status": 529}),
        system_line("model_refusal_fallback", originalModel="a", fallbackModel="b"),
        system_line("local_command"),
        system_line("stop_hook_summary"),
        attachment_line("hook_success"),
        attachment_line("model", identity={"modelId": "claude-sonnet-5"}),
        attachment_line("thinking_stripped", scope="session"),
        attachment_line("deferred_tools_delta", addedNames=["Read"], removedNames=[]),
        attachment_line("total_tokens_reminder"),
        attachment_line("environment", rendered="abc"),
        attachment_line("queued_command"),
        attachment_line("a_brand_new_unclassified_type"),
        queue_operation_line("enqueue"),
        ignorable_line("bridge-session"),
        "{not valid json",
        turn_line(message_id="msg_2", input_tokens=50, output_tokens=5),
    ]
    path = tmp_path / "session.jsonl"
    # write_jsonl expects dicts; the malformed line is inserted as raw text.
    with open(path, "w", encoding="utf-8") as fh:
        for entry in lines:
            if isinstance(entry, str):
                fh.write(entry + "\n")
            else:
                fh.write(__import__("json").dumps(entry) + "\n")
        fh.write("truncated because the writer got cut off mid-lin")

    result = parse_transcript(path, TranscriptMeta(path=str(path)))

    seen_kinds = {e.kind for e in result.events}
    assert EventKind.HUMAN_TEXT in seen_kinds
    assert EventKind.SLASH_COMMAND in seen_kinds
    assert EventKind.SCHEDULED_TASK in seen_kinds
    assert EventKind.INTERRUPT in seen_kinds
    assert EventKind.COMPACT_SUMMARY in seen_kinds
    assert EventKind.TASK_NOTIFICATION in seen_kinds
    assert EventKind.PEER_MESSAGE in seen_kinds
    assert EventKind.TOOL_DENIAL in seen_kinds
    assert EventKind.META in seen_kinds
    assert EventKind.TOOL_RESULT in seen_kinds
    assert EventKind.COMPACT_BOUNDARY in seen_kinds
    assert EventKind.API_ERROR in seen_kinds
    assert EventKind.MODEL_FALLBACK in seen_kinds
    assert EventKind.LOCAL_COMMAND in seen_kinds
    assert EventKind.HOOK_OUTPUT in seen_kinds
    assert EventKind.CACHE_SIGNAL in seen_kinds
    assert EventKind.REMINDER in seen_kinds
    assert EventKind.CONTEXT_INJECT in seen_kinds
    assert EventKind.QUEUE_OPERATION in seen_kinds
    assert EventKind.ATTACHMENT in seen_kinds

    assert result.diagnostics.ignored_line_types.get("bridge-session") == 1
    # An ATTACHMENT-kind Event still gets returned (not dropped like an
    # ignorable type), so its subkind stays discoverable via
    # TranscriptResult.events for "classify it next release" triage —
    # see A2's ATTACHMENT row and WP1 deliverable 6.
    unclassified_attachment_subkinds = {
        e.subkind for e in result.events if e.kind == EventKind.ATTACHMENT
    }
    assert unclassified_attachment_subkinds == {"a_brand_new_unclassified_type"}
    # Wasted-turns addition (see model.py's Turn.tool_error_count/
    # tool_error_chars docstrings): msg_1's own tool_use answered by an
    # is_error:true tool_result is attributed onto msg_1's Turn, length
    # only.
    assert result.turns[0].tool_error_count == 1
    assert result.turns[0].tool_error_chars == len("No such file or directory")
    assert result.diagnostics.unparsable_lines == 1  # the "{not valid json" line
    assert result.diagnostics.truncated_final_line is True
    assert len(result.turns) == 2

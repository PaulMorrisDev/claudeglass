"""Capture-improvements batch (parse.py/events.py/model.py/probe.py):
``Turn.tool_wait_s``/``model_latency_s``/``tool_result_chars_by_tool``
(A1), ``Turn.agent_brief_chars``/``tool_input_chars_by_tool`` (A2),
``Turn.read_target_hashes``/``parse.set_salt``/``parse.load_or_create_salt``
(A3), ``Turn.human_prompt_chars``/``human_prompt_has_paste`` (A4),
``probe.compare_with_parser``/``parse.READ_KEYS`` (A6), and the
v4-wasted-turns batch's ``Turn.tool_error_count``/``tool_error_chars``
(reusing A1's ``_accumulate_tool_results`` attribution, so it inherits
the same current-turn-only attribution rule). See model.py's module
docstring for the full field list and parse.py's for the salt
architecture.

Each test exercises the behaviour through a full ``parse_transcript`` pass
over a small synthetic fixture, matching this codebase's own convention
(see test_parse_batch_c.py).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from claudeglass import events, parse, probe
from claudeglass.model import TranscriptMeta
from claudeglass.parse import parse_transcript

from helpers import (
    assert_privacy,
    tool_result_block,
    tool_use_block,
    turn_line,
    user_block_line,
    user_str_line,
    write_jsonl,
)


# -- A1: Turn.tool_wait_s / model_latency_s / tool_result_chars_by_tool ----


def test_tool_wait_model_latency_and_result_chars_by_tool(tmp_path: Path):
    lines = [
        turn_line(
            message_id="msg_1",
            timestamp="2026-09-18T12:00:00.000Z",
            content=[tool_use_block("Bash", "tu_a", {"command": "ls"})],
        ),
        user_block_line(
            [tool_result_block("tu_a", "file1\nfile2")],
            timestamp="2026-09-18T12:00:05.000Z",
        ),
        turn_line(message_id="msg_2", timestamp="2026-09-18T12:00:08.000Z"),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))

    first = result.turns[0]
    assert first.tool_wait_s == pytest.approx(5.0)
    assert first.model_latency_s == pytest.approx(3.0)
    assert first.tool_result_chars_by_tool == {"Bash": len("file1\nfile2")}


def test_tool_wait_and_result_chars_none_when_turn_has_no_tool_calls(tmp_path: Path):
    lines = [turn_line(message_id="msg_1")]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))

    turn = result.turns[0]
    assert turn.tool_wait_s is None
    assert turn.model_latency_s is None
    assert turn.tool_result_chars_by_tool == {}


def test_model_latency_none_when_no_next_turn(tmp_path: Path):
    lines = [
        turn_line(
            message_id="msg_1",
            timestamp="2026-09-18T12:00:00.000Z",
            content=[tool_use_block("Read", "tu_a", {"file_path": "x.py"})],
        ),
        user_block_line(
            [tool_result_block("tu_a", "data")],
            timestamp="2026-09-18T12:00:04.000Z",
        ),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))

    turn = result.turns[0]
    assert turn.tool_wait_s == pytest.approx(4.0)
    assert turn.model_latency_s is None


def test_tool_result_from_earlier_turn_does_not_pollute_later_turn(tmp_path: Path):
    """A tool_result can answer a tool_use from an earlier turn (the
    docstring's own caveat on _accumulate_tool_results) -- it must only
    ever be folded into the turn that actually owns that tool_use_id.
    """
    lines = [
        turn_line(
            message_id="msg_1",
            timestamp="2026-09-18T12:00:00.000Z",
            content=[tool_use_block("Read", "tu_a", {"file_path": "x.py"})],
        ),
        turn_line(message_id="msg_2", timestamp="2026-09-18T12:00:01.000Z"),
        # Answers msg_1's tool_use, arriving after msg_2 has already
        # started (a real-world ordering the parser must tolerate).
        user_block_line(
            [tool_result_block("tu_a", "data")],
            timestamp="2026-09-18T12:00:09.000Z",
        ),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))

    assert result.turns[0].tool_result_chars_by_tool == {}
    assert result.turns[1].tool_result_chars_by_tool == {}


# -- A2: Turn.agent_brief_chars / tool_input_chars_by_tool -----------------


def test_agent_brief_chars_and_tool_input_chars_by_tool(tmp_path: Path):
    lines = [
        turn_line(
            message_id="msg_1",
            content=[
                tool_use_block("Agent", "tu_a", {"subagent_type": "x", "prompt": "do the thing"}),
                tool_use_block("Bash", "tu_b", {"command": "ls -la"}),
            ],
        ),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))

    turn = result.turns[0]
    assert turn.agent_brief_chars == len("do the thing")
    assert turn.tool_input_chars_by_tool["Bash"] > 0
    assert "Agent" in turn.tool_input_chars_by_tool


def test_agent_brief_chars_recognises_task_tool_name_too(tmp_path: Path):
    lines = [turn_line(message_id="msg_1", content=[tool_use_block("Task", "tu_a", {"prompt": "hi"})])]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    assert result.turns[0].agent_brief_chars == 2


def test_agent_brief_chars_sums_multiple_agent_spawns_in_one_turn(tmp_path: Path):
    lines = [
        turn_line(
            message_id="msg_1",
            content=[
                tool_use_block("Agent", "tu_a", {"prompt": "aaa"}),
                tool_use_block("Task", "tu_b", {"prompt": "bb"}),
            ],
        ),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    assert result.turns[0].agent_brief_chars == 5


def test_agent_brief_chars_none_and_tool_input_chars_empty_with_no_tool_use(tmp_path: Path):
    lines = [turn_line(message_id="msg_1")]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    turn = result.turns[0]
    assert turn.agent_brief_chars is None
    assert turn.tool_input_chars_by_tool == {}


def test_batch_a2_fields_pass_privacy_scan(tmp_path: Path):
    lines = [
        turn_line(
            message_id="msg_1",
            content=[
                tool_use_block("Agent", "tu_a", {"prompt": "please read C:/Users/paulm/secret.txt"}),
                tool_use_block("Bash", "tu_b", {"command": "cd /c/Dev/x && pytest"}),
            ],
        ),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    assert_privacy(result)


# -- A3: Turn.read_target_hashes / set_salt / load_or_create_salt --------


def test_read_target_hashes_empty_without_salt(tmp_path: Path):
    lines = [turn_line(message_id="msg_1", content=[tool_use_block("Read", "tu_a", {"file_path": "x.py"})])]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    assert result.turns[0].read_target_hashes == ()


def test_read_target_hash_same_path_same_salt_is_stable(tmp_path: Path):
    parse.set_salt(b"a" * 32)
    lines_a = [
        turn_line(message_id="msg_1", content=[tool_use_block("Read", "tu_a", {"file_path": "C:/Dev/x/file.py"})])
    ]
    path_a = tmp_path / "a.jsonl"
    write_jsonl(path_a, lines_a)
    result_a = parse_transcript(path_a, TranscriptMeta(path=str(path_a)))

    lines_b = [
        turn_line(message_id="msg_1", content=[tool_use_block("Read", "tu_b", {"file_path": "C:/Dev/x/file.py"})])
    ]
    path_b = tmp_path / "b.jsonl"
    write_jsonl(path_b, lines_b)
    result_b = parse_transcript(path_b, TranscriptMeta(path=str(path_b)))

    assert result_a.turns[0].read_target_hashes == result_b.turns[0].read_target_hashes
    assert len(result_a.turns[0].read_target_hashes) == 1
    assert len(result_a.turns[0].read_target_hashes[0]) == 16


def test_read_target_hash_differs_with_different_salt(tmp_path: Path):
    lines = [
        turn_line(message_id="msg_1", content=[tool_use_block("Read", "tu_a", {"file_path": "C:/Dev/x/file.py"})])
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)

    parse.set_salt(b"a" * 32)
    result_a = parse_transcript(path, TranscriptMeta(path=str(path)))
    parse.set_salt(b"b" * 32)
    result_b = parse_transcript(path, TranscriptMeta(path=str(path)))

    assert result_a.turns[0].read_target_hashes != result_b.turns[0].read_target_hashes


def test_read_target_hash_never_contains_a_path_segment(tmp_path: Path):
    parse.set_salt(b"c" * 32)
    lines = [
        turn_line(
            message_id="msg_1",
            content=[tool_use_block("Read", "tu_a", {"file_path": "C:/Users/paulm/secret.txt"})],
        )
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    hashed = result.turns[0].read_target_hashes[0].lower()
    for segment in ("users", "paulm", "secret", "c:"):
        assert segment not in hashed


def test_read_target_hashes_cover_reads_and_edits_go_to_edit_target_hashes(tmp_path: Path):
    parse.set_salt(b"d" * 32)
    lines = [
        turn_line(
            message_id="msg_1",
            content=[
                tool_use_block("Read", "tu_a", {"file_path": "a.py"}),
                tool_use_block("Edit", "tu_b", {"file_path": "b.py"}),
                tool_use_block("Write", "tu_c", {"file_path": "c.py"}),
                tool_use_block("NotebookEdit", "tu_d", {"notebook_path": "d.ipynb"}),
                tool_use_block("MultiEdit", "tu_e", {"file_path": "e.py"}),
            ],
        ),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    # Only Read is a read (PARSER_VERSION 15); the other four are edits.
    assert len(result.turns[0].read_target_hashes) == 1
    assert len(result.turns[0].edit_target_hashes) == 4
    assert not set(result.turns[0].read_target_hashes) & set(result.turns[0].edit_target_hashes)


def test_load_or_create_salt_persists_across_calls(tmp_path: Path):
    config_dir = tmp_path / "claudeglass"
    salt1 = parse.load_or_create_salt(config_dir)
    assert len(salt1) == 32
    salt2 = parse.load_or_create_salt(config_dir)
    assert salt1 == salt2
    assert (config_dir / "salt").exists()


def test_load_or_create_salt_missing_file_regenerates(tmp_path: Path):
    """A missing salt file (the common, expected "first use" case) is
    still treated as "no salt yet" and silently regenerated."""
    config_dir = tmp_path / "claudeglass"
    salt = parse.load_or_create_salt(config_dir)
    assert len(salt) == 32


def test_load_or_create_salt_other_read_errors_propagate(monkeypatch, tmp_path: Path):
    """SEC-P8/G7: a read failure that is not "the file doesn't exist"
    (a permission problem, a dead network mount, ...) must not be folded
    into the same silent-regenerate path as a missing file -- doing so
    would rotate the salt on what is usually a transient condition,
    breaking every session-id hash this tool has already written
    (signals/, a cache's provenance header) without telling anyone."""
    config_dir = tmp_path / "claudeglass"
    config_dir.mkdir(parents=True)
    (config_dir / "salt").write_bytes(b"x" * 32)

    def _boom(self, *a, **k):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "read_bytes", _boom)
    with pytest.raises(PermissionError):
        parse.load_or_create_salt(config_dir)


def test_load_or_create_salt_survives_a_newline_byte(monkeypatch, tmp_path: Path):
    """Regression test for a real (not merely flaky-test) bug: the file
    was opened via ``os.open`` without ``os.O_BINARY``, which on Windows
    defaults to text mode and silently rewrites any ``b"\\n"`` (0x0a)
    byte to ``b"\\r\\n"`` on write. A genuinely random 32-byte salt hits
    this on roughly one in eight calls, corrupting the on-disk salt
    relative to what ``secrets.token_bytes`` actually returned and
    making ``load_or_create_salt``'s own second, read-back call disagree
    with the first -- this is what surfaced as an intermittently failing
    ``test_load_or_create_salt_persists_across_calls`` rather than a
    test-isolation problem. Pins a salt containing 0x0a (and 0x0d, for
    good measure) so this reproduces deterministically on every run
    rather than ~1-in-8 of them.
    """
    pinned = bytes(range(32 - 2)) + b"\x0a\x0d"
    assert len(pinned) == 32
    monkeypatch.setattr(parse.secrets, "token_bytes", lambda n: pinned)

    config_dir = tmp_path / "claudeglass"
    salt1 = parse.load_or_create_salt(config_dir)
    assert salt1 == pinned
    assert (config_dir / "salt").stat().st_size == 32

    salt2 = parse.load_or_create_salt(config_dir)
    assert salt2 == pinned == salt1


@pytest.mark.skipif(os.name == "nt", reason="POSIX-only file mode bits")
def test_load_or_create_salt_sets_owner_only_perms(tmp_path: Path):
    config_dir = tmp_path / "claudeglass"
    parse.load_or_create_salt(config_dir)
    mode = (config_dir / "salt").stat().st_mode & 0o777
    assert mode == 0o600


def test_load_or_create_salt_defaults_to_claude_config_dir(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    salt = parse.load_or_create_salt()
    assert len(salt) == 32
    assert (tmp_path / ".claude" / "claudeglass" / "salt").exists()


# -- A4: Turn.human_prompt_chars / human_prompt_has_paste ------------------


def test_human_prompt_chars_captured_with_no_paste(tmp_path: Path):
    lines = [
        user_str_line("please fix the bug", origin={"kind": "human"}),
        turn_line(message_id="msg_1"),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    turn = result.turns[0]
    assert turn.human_prompt_chars == len("please fix the bug")
    assert turn.human_prompt_has_paste is False


def test_human_prompt_has_paste_flag_for_long_text(tmp_path: Path):
    long_text = "x" * 2001
    lines = [
        user_str_line(long_text, origin={"kind": "human"}),
        turn_line(message_id="msg_1"),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    turn = result.turns[0]
    assert turn.human_prompt_chars == 2001
    assert turn.human_prompt_has_paste is True


def test_human_prompt_has_paste_flag_for_pasted_text_marker(tmp_path: Path):
    lines = [
        user_str_line("see this: [Pasted text #1 +50 lines]", origin={"kind": "human"}),
        turn_line(message_id="msg_1"),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    assert result.turns[0].human_prompt_has_paste is True


def test_human_prompt_chars_none_when_no_human_text_precedes(tmp_path: Path):
    lines = [turn_line(message_id="msg_1")]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    turn = result.turns[0]
    assert turn.human_prompt_chars is None
    assert turn.human_prompt_has_paste is False


def test_human_text_event_size_chars_and_detail_populated():
    line = user_str_line("please fix the bug", origin={"kind": "human"})
    event = events.classify_line(line)
    assert event.size_chars == len("please fix the bug")
    # A short fix request naming nothing specific is a vague one, and asks for a change.
    assert event.detail == {"has_paste": False, "correction": False, "vague": True, "change": True}


def test_human_text_event_detects_paste_marker_without_origin():
    line = user_str_line("[Pasted text #1 +10 lines] rest of message")
    event = events.classify_line(line)
    assert event.size_chars is not None
    assert event.detail == {"has_paste": True, "correction": False}


def test_batch_a4_fields_pass_privacy_scan(tmp_path: Path):
    lines = [
        user_str_line("please read C:/Users/paulm/secret.txt and fix it", origin={"kind": "human"}),
        turn_line(message_id="msg_1"),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    assert_privacy(result)


# -- v4-wasted-turns: Turn.tool_error_count / tool_error_chars -------------


def test_tool_error_count_and_chars_from_is_error_tool_result(tmp_path: Path):
    lines = [
        turn_line(
            message_id="msg_1",
            content=[tool_use_block("Bash", "tu_a", {"command": "cat missing.txt"})],
        ),
        user_block_line([tool_result_block("tu_a", "cat: missing.txt: No such file", is_error=True)]),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))

    turn = result.turns[0]
    assert turn.tool_error_count == 1
    assert turn.tool_error_chars == len("cat: missing.txt: No such file")


def test_tool_error_count_ignores_successful_tool_result(tmp_path: Path):
    lines = [
        turn_line(
            message_id="msg_1",
            content=[tool_use_block("Read", "tu_a", {"file_path": "x.py"})],
        ),
        user_block_line([tool_result_block("tu_a", "print('ok')", is_error=False)]),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))

    turn = result.turns[0]
    assert turn.tool_error_count == 0
    assert turn.tool_error_chars == 0


def test_tool_error_count_sums_multiple_erroring_tool_calls_in_one_turn(tmp_path: Path):
    lines = [
        turn_line(
            message_id="msg_1",
            content=[
                tool_use_block("Bash", "tu_a", {"command": "ls /nope"}),
                tool_use_block("Read", "tu_b", {"file_path": "missing.py"}),
            ],
        ),
        user_block_line(
            [
                tool_result_block("tu_a", "no such directory", is_error=True),
                tool_result_block("tu_b", "file not found", is_error=True),
            ]
        ),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))

    turn = result.turns[0]
    assert turn.tool_error_count == 2
    assert turn.tool_error_chars == len("no such directory") + len("file not found")


def test_tool_error_count_zero_when_no_is_error_key_present(tmp_path: Path):
    """Most of the real corpus's tool_result blocks carry no ``is_error``
    key at all (only ~63% do, per the read-only corpus check run before
    implementing this) -- absence must mean "not an error", not raise.
    """
    lines = [
        turn_line(
            message_id="msg_1",
            content=[tool_use_block("Bash", "tu_a", {"command": "ls"})],
        ),
        user_block_line([tool_result_block("tu_a", "file1\nfile2")]),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))

    turn = result.turns[0]
    assert turn.tool_error_count == 0
    assert turn.tool_error_chars == 0


def test_tool_error_from_earlier_turn_does_not_pollute_later_turn(tmp_path: Path):
    """Same non-attribution rule as A1's ``tool_result_chars_by_tool``:
    an is_error tool_result answering an *earlier* turn's tool_use, once
    the next turn has already started, is credited to neither turn.
    """
    lines = [
        turn_line(
            message_id="msg_1",
            timestamp="2026-09-18T12:00:00.000Z",
            content=[tool_use_block("Read", "tu_a", {"file_path": "x.py"})],
        ),
        turn_line(message_id="msg_2", timestamp="2026-09-18T12:00:01.000Z"),
        user_block_line(
            [tool_result_block("tu_a", "not found", is_error=True)],
            timestamp="2026-09-18T12:00:09.000Z",
        ),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))

    assert result.turns[0].tool_error_count == 0
    assert result.turns[1].tool_error_count == 0


def test_tool_error_count_and_chars_default_to_zero_with_no_tool_calls(tmp_path: Path):
    lines = [turn_line(message_id="msg_1")]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))

    turn = result.turns[0]
    assert turn.tool_error_count == 0
    assert turn.tool_error_chars == 0


def test_tool_error_chars_never_carries_error_text_itself(tmp_path: Path):
    lines = [
        turn_line(
            message_id="msg_1",
            content=[tool_use_block("Bash", "tu_a", {"command": "cat /c/Users/paulm/secret.txt"})],
        ),
        user_block_line(
            [
                tool_result_block(
                    "tu_a",
                    "cat: /c/Users/paulm/secret.txt: No such file or directory",
                    is_error=True,
                )
            ]
        ),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    assert_privacy(result)


# -- A6: probe.compare_with_parser / parse.READ_KEYS -----------------------


def test_compare_with_parser_flags_a_key_the_parser_never_reads():
    result = probe.ProbeResult()
    probe.probe_line({"type": "user", "message": {"role": "user", "content": "hi"}, "someNewField": 1}, result)
    unread = probe.compare_with_parser(result)
    assert "user.someNewField" in unread


def test_compare_with_parser_empty_when_every_key_is_read():
    result = probe.ProbeResult()
    probe.probe_line({"type": "user", "timestamp": "2026-09-18T12:00:00.000Z", "message": {}}, result)
    unread = probe.compare_with_parser(result)
    assert unread == []


def test_compare_with_parser_falls_back_to_base_keys_for_an_ignored_type():
    result = probe.ProbeResult()
    probe.probe_line({"type": "bridge-session", "someField": 1}, result)
    unread = probe.compare_with_parser(result)
    assert "bridge-session.someField" in unread
    assert "bridge-session.type" not in unread


def test_compare_with_parser_entries_are_clipped_to_max_value_chars():
    result = probe.ProbeResult()
    long_key = "k" * 300
    probe.probe_line({"type": "user", long_key: 1}, result)
    unread = probe.compare_with_parser(result)
    assert unread
    for entry in unread:
        assert len(entry) <= probe.MAX_VALUE_CHARS


def test_render_probe_includes_unread_keys_section():
    result = probe.ProbeResult()
    probe.probe_line({"type": "user", "mysteryKey": True}, result)
    text = probe.render_probe(result)
    assert "## unread keys (vs parse.py)" in text
    assert "user.mysteryKey" in text


def test_compare_with_parser_runs_clean_over_the_real_fixture():
    fixture_dir = Path(__file__).parent / "fixtures" / "real" / "session-a"
    paths = sorted(fixture_dir.rglob("*.jsonl"))
    assert paths, "expected at least one real fixture file"
    result = probe.probe_paths(paths)
    unread = probe.compare_with_parser(result)
    # Informational rather than pinned: the real fixture's shape may grow
    # new keys over time, and the actual current list is reported in the
    # coordinator handoff rather than hardcoded here. This just proves
    # the function runs clean (no exception, list of strings) over a
    # real, full-sized transcript tree.
    assert isinstance(unread, list)
    assert all(isinstance(entry, str) for entry in unread)

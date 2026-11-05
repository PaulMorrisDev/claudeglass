"""Copied sessions: after ``/clear`` in a web or mobile session, Claude
Code writes the new session's lines both to its own file and into the
earlier session's file, keeping the new session's ``sessionId`` (seen in
Claude Code 2.1.283). Those lines must be priced once, from their own
file -- see ``parse.py``'s module docstring.
"""

from __future__ import annotations

from pathlib import Path

from claudeglass.corpus import load_corpus
from claudeglass.model import Diagnostics, TranscriptMeta
from claudeglass.parse import parse_transcript
from claudeglass.report import _merge_diagnostics

from helpers import system_line, turn_line, user_str_line, write_jsonl

EARLIER = "7e5fd47d-1816-569f-9241-e02958cf9196"
CLEARED = "2936e571-ffaa-4e4c-bfdd-cbb1c84d40d4"


def _cleared_lines() -> list[dict]:
    """The session started by /clear: a prompt and two replies."""
    return [
        user_str_line("/clear", sessionId=CLEARED, uuid="c-user-1"),
        turn_line(message_id="msg_c1", output_tokens=700, sessionId=CLEARED, uuid="c-asst-1"),
        user_str_line("next", sessionId=CLEARED, uuid="c-user-2"),
        turn_line(message_id="msg_c2", output_tokens=300, sessionId=CLEARED, uuid="c-asst-2"),
    ]


def _earlier_lines() -> list[dict]:
    """The earlier session: its own replies, then a verbatim copy of the
    cleared session's lines (same uuids, message ids and sessionId), then
    more of its own."""
    return [
        user_str_line("start", sessionId=EARLIER, uuid="e-user-1"),
        turn_line(message_id="msg_e1", output_tokens=100, sessionId=EARLIER, uuid="e-asst-1"),
        *[dict(line) for line in _cleared_lines()],
        user_str_line("carry on", sessionId=EARLIER, uuid="e-user-2"),
        turn_line(message_id="msg_e2", output_tokens=200, sessionId=EARLIER, uuid="e-asst-2"),
    ]


def _write_project(folder: Path, *, with_cleared_file: bool = True) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    write_jsonl(folder / f"{EARLIER}.jsonl", _earlier_lines())
    if with_cleared_file:
        write_jsonl(folder / f"{CLEARED}.jsonl", _cleared_lines())
    return folder


def _top_meta(path: Path, session_id: str) -> TranscriptMeta:
    return TranscriptMeta(path=str(path), kind="top-level", session_id=session_id)


def test_copy_is_skipped_when_its_own_file_is_beside_it(tmp_path: Path):
    folder = _write_project(tmp_path / "-home-user-shop")
    path = folder / f"{EARLIER}.jsonl"

    result = parse_transcript(path, _top_meta(path, EARLIER))

    assert [turn.message_id for turn in result.turns] == ["msg_e1", "msg_e2"]
    assert sum(turn.output_tokens for turn in result.turns) == 300
    assert result.diagnostics.copied_lines == 4
    assert result.diagnostics.replayed_lines == 0


def test_copy_is_kept_when_there_is_no_file_of_its_own(tmp_path: Path):
    folder = _write_project(tmp_path / "-home-user-shop", with_cleared_file=False)
    path = folder / f"{EARLIER}.jsonl"

    result = parse_transcript(path, _top_meta(path, EARLIER))

    assert [turn.message_id for turn in result.turns] == ["msg_e1", "msg_c1", "msg_c2", "msg_e2"]
    assert result.diagnostics.copied_lines == 0


def test_a_log_whose_session_id_is_not_its_file_name_keeps_every_line(tmp_path: Path):
    # Many logs (and most fixtures) carry a sessionId that isn't their
    # file name; with no file of that name beside them, nothing is skipped.
    path = tmp_path / "session-renamed.jsonl"
    write_jsonl(path, _cleared_lines())

    result = parse_transcript(path, _top_meta(path, "session-renamed"))

    assert len(result.turns) == 2
    assert result.diagnostics.copied_lines == 0


def test_only_a_top_level_transcript_skips_copies(tmp_path: Path):
    folder = _write_project(tmp_path / "-home-user-shop")
    path = folder / f"{EARLIER}.jsonl"

    result = parse_transcript(path, TranscriptMeta(path=str(path), kind="subagent", session_id=EARLIER))

    assert len(result.turns) == 4
    assert result.diagnostics.copied_lines == 0


def test_a_session_id_that_is_not_a_file_name_is_never_looked_up(tmp_path: Path):
    folder = tmp_path / "-home-user-shop"
    folder.mkdir()
    # A file the traversal would find if the id were used as a path.
    write_jsonl(tmp_path / "outside.jsonl", [])
    lines = [
        turn_line(message_id="msg_1", sessionId=EARLIER),
        turn_line(message_id="msg_2", sessionId="../outside"),
        turn_line(message_id="msg_3", sessionId="a.b"),
    ]
    path = folder / f"{EARLIER}.jsonl"
    write_jsonl(path, lines)

    result = parse_transcript(path, _top_meta(path, EARLIER))

    assert len(result.turns) == 3
    assert result.diagnostics.copied_lines == 0


def test_corpus_prices_each_reply_once(tmp_path: Path):
    folder = _write_project(tmp_path / "-home-user-shop")

    corpus = load_corpus([folder])

    by_session = {bundle.session_id: bundle for bundle in corpus.sessions}
    assert set(by_session) == {EARLIER, CLEARED}
    assert [t.message_id for t in by_session[EARLIER].top.turns] == ["msg_e1", "msg_e2"]
    assert [t.message_id for t in by_session[CLEARED].top.turns] == ["msg_c1", "msg_c2"]
    message_ids = [t.message_id for bundle in corpus.sessions for t in bundle.top.turns]
    assert len(message_ids) == len(set(message_ids))
    assert sum(t.output_tokens for bundle in corpus.sessions for t in bundle.top.turns) == 1300


def test_copied_lines_add_up_across_transcripts():
    total = Diagnostics()
    _merge_diagnostics(total, Diagnostics(copied_lines=4))
    _merge_diagnostics(total, Diagnostics(copied_lines=3))
    assert total.copied_lines == 7


# -- a fork in another project folder, and copied agent transcripts -----------

SOURCE = "11111111-1816-569f-9241-e02958cf9196"
FORK = "22222222-ffaa-4e4c-bfdd-cbb1c84d40d4"


def _source_lines() -> list[dict]:
    """The session a fork came from: two replies and a compaction."""
    return [
        user_str_line("first", sessionId=SOURCE, uuid="s-user-1", timestamp="2026-10-20T09:00:00.000Z"),
        turn_line(message_id="msg_s1", output_tokens=700, sessionId=SOURCE, uuid="s-asst-1",
                  timestamp="2026-10-20T09:00:05.000Z"),
        system_line("compact_boundary", sessionId=SOURCE, uuid="s-compact", timestamp="2026-10-20T09:05:00.000Z",
                    compactMetadata={"trigger": "auto", "preTokens": 150000, "postTokens": 9000, "durationMs": 40000}),
        user_str_line("second", sessionId=SOURCE, uuid="s-user-2", timestamp="2026-10-20T09:06:00.000Z"),
        turn_line(message_id="msg_s2", output_tokens=300, sessionId=SOURCE, uuid="s-asst-2",
                  timestamp="2026-10-20T09:06:05.000Z"),
    ]


def _fork_lines() -> list[dict]:
    """A fork made in another worktree: the source's lines, copied, then
    its own prompt and reply a day later."""
    return [
        *[dict(line) for line in _source_lines()],
        user_str_line("forked", sessionId=FORK, uuid="f-user-1", timestamp="2026-10-21T09:00:00.000Z"),
        turn_line(message_id="msg_f1", output_tokens=50, sessionId=FORK, uuid="f-asst-1",
                  timestamp="2026-10-21T09:00:05.000Z"),
    ]


def _write_fork(root: Path, *, with_source: bool = True) -> Path:
    project_a = root / "-home-user-shop"
    project_b = root / "-home-user-shop-worktrees-fork"
    project_a.mkdir(parents=True)
    project_b.mkdir(parents=True)
    if with_source:
        write_jsonl(project_a / f"{SOURCE}.jsonl", _source_lines())
    write_jsonl(project_b / f"{FORK}.jsonl", _fork_lines())
    return project_b / f"{FORK}.jsonl"


def test_a_fork_in_another_project_folder_prices_only_its_own_replies(tmp_path: Path):
    path = _write_fork(tmp_path)

    result = parse_transcript(path, _top_meta(path, FORK))

    assert [turn.message_id for turn in result.turns] == ["msg_f1"]
    assert result.diagnostics.copied_lines == 5
    # Nothing copied is an event either: the first one is the fork's own.
    assert min(event.ts for event in result.events if event.ts) >= "2026-10-21T09:00:00.000Z"


def test_a_compaction_copied_into_a_fork_is_not_the_forks_event(tmp_path: Path):
    path = _write_fork(tmp_path)

    result = parse_transcript(path, _top_meta(path, FORK))

    assert not [event for event in result.events if event.kind.value == "compact_boundary"]
    assert result.diagnostics.compaction_calls == 0
    assert result.diagnostics.compaction_calls_unsized == 0


def test_a_fork_keeps_the_copies_when_the_source_has_been_deleted(tmp_path: Path):
    path = _write_fork(tmp_path, with_source=False)

    result = parse_transcript(path, _top_meta(path, FORK))

    assert [turn.message_id for turn in result.turns if not turn.estimated] == ["msg_s1", "msg_s2", "msg_f1"]
    assert result.diagnostics.copied_lines == 0


def test_a_corpus_prices_a_fork_in_another_folder_once(tmp_path: Path):
    _write_fork(tmp_path)

    corpus = load_corpus([tmp_path / "-home-user-shop", tmp_path / "-home-user-shop-worktrees-fork"])

    message_ids = [t.message_id for bundle in corpus.sessions for t in bundle.top.turns if not t.estimated]
    assert sorted(message_ids) == ["msg_f1", "msg_s1", "msg_s2"]


def _agent_lines(session_id: str) -> list[dict]:
    return [
        user_str_line("work", sessionId=session_id, uuid="a-user-1"),
        turn_line(message_id="msg_a1", output_tokens=120, sessionId=session_id, uuid="a-asst-1"),
        turn_line(message_id="msg_a2", output_tokens=80, sessionId=session_id, uuid="a-asst-2"),
    ]


def _agent_path(root: Path, project: str, session_id: str, *parts: str) -> Path:
    folder = root / project / session_id / "subagents"
    for part in parts:
        folder = folder / part
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "agent-x.jsonl"


def _agent_meta(path: Path, session_id: str) -> TranscriptMeta:
    return TranscriptMeta(path=str(path), kind="subagent", session_id=session_id)


def test_an_agent_transcript_copied_into_a_fork_prices_nothing(tmp_path: Path):
    source = _agent_path(tmp_path, "-home-user-shop", SOURCE)
    write_jsonl(source, _agent_lines(SOURCE))
    copy = _agent_path(tmp_path, "-home-user-shop-worktrees-fork", FORK)
    write_jsonl(copy, _agent_lines(SOURCE))

    original = parse_transcript(source, _agent_meta(source, SOURCE))
    copied = parse_transcript(copy, _agent_meta(copy, FORK))

    assert len(original.turns) == 2
    assert len(copied.turns) == 0
    assert copied.diagnostics.copied_lines == 3


def test_a_workflow_agent_transcript_copy_is_found_by_its_path_below_the_session(tmp_path: Path):
    source = _agent_path(tmp_path, "-home-user-shop", SOURCE, "workflows", "wf_run1")
    write_jsonl(source, _agent_lines(SOURCE))
    copy = _agent_path(tmp_path, "-home-user-shop", FORK, "workflows", "wf_run1")
    write_jsonl(copy, _agent_lines(SOURCE))

    copied = parse_transcript(copy, _agent_meta(copy, FORK))

    assert len(copied.turns) == 0


def test_an_agent_copy_is_kept_when_the_source_session_no_longer_holds_that_file(tmp_path: Path):
    copy = _agent_path(tmp_path, "-home-user-shop-worktrees-fork", FORK)
    write_jsonl(copy, _agent_lines(SOURCE))

    copied = parse_transcript(copy, _agent_meta(copy, FORK))

    assert len(copied.turns) == 2
    assert copied.diagnostics.copied_lines == 0


def test_an_agent_transcript_with_its_own_session_id_keeps_every_line(tmp_path: Path):
    path = _agent_path(tmp_path, "-home-user-shop", SOURCE)
    write_jsonl(path, _agent_lines(SOURCE))

    result = parse_transcript(path, _agent_meta(path, SOURCE))

    assert len(result.turns) == 2
    assert result.diagnostics.copied_lines == 0


# -- a session continued after a compaction ----------------------------------


def _continued_lines() -> list[dict]:
    """A session continued after a compaction: it opens with the boundary
    of the session it came from and a zeroed copy of the reply Claude Code
    preserved across it (every count 0), then carries on with its own."""
    zeroed = turn_line(message_id="msg_kept", input_tokens=0, output_tokens=0, sessionId=CLEARED, uuid="z-asst-1",
                       timestamp="2026-10-20T09:00:01.000Z")
    return [
        system_line("compact_boundary", sessionId=CLEARED, uuid="z-compact", timestamp="2026-10-20T09:00:00.000Z",
                    compactMetadata={"trigger": "auto", "preTokens": 150000, "postTokens": 9000, "durationMs": 40000}),
        zeroed,
        user_str_line("carry on", sessionId=CLEARED, uuid="z-user-1", timestamp="2026-10-20T09:01:00.000Z"),
        turn_line(message_id="msg_own", output_tokens=200, sessionId=CLEARED, uuid="z-asst-2",
                  timestamp="2026-10-20T09:01:05.000Z"),
    ]


def _parse_continued(tmp_path: Path):
    folder = tmp_path / "-home-user-shop"
    folder.mkdir()
    path = folder / f"{CLEARED}.jsonl"
    write_jsonl(path, _continued_lines())
    return parse_transcript(path, _top_meta(path, CLEARED))


def test_a_zeroed_copy_of_the_preserved_reply_is_no_priced_turn(tmp_path: Path):
    result = _parse_continued(tmp_path)

    assert [turn.message_id for turn in result.turns if turn.turn_index > 0] == ["msg_own"]
    assert result.diagnostics.copied_lines == 1
    assert result.diagnostics.turns_missing_usage == 0


def test_the_inherited_boundary_is_marked_and_sizes_no_summary_request(tmp_path: Path):
    result = _parse_continued(tmp_path)

    [boundary] = [event for event in result.events if event.kind.value == "compact_boundary"]
    assert boundary.detail["inherited"] is True
    assert result.diagnostics.compaction_calls == 0
    assert result.diagnostics.compaction_calls_unsized == 0
    assert not any(turn.estimated for turn in result.turns)


def test_a_zeroed_synthetic_placeholder_still_counts_as_a_turn_without_usage(tmp_path: Path):
    """Only a real model's all-zero reply is a copy; the placeholder Claude
    Code writes for an error or a refusal (model ``<synthetic>``) has no
    usage by design and is not one."""
    placeholder = turn_line(message_id="msg_placeholder", model="<synthetic>", input_tokens=0, output_tokens=0,
                            sessionId=CLEARED, uuid="p-asst-1")
    folder = tmp_path / "-home-user-shop"
    folder.mkdir()
    path = folder / f"{CLEARED}.jsonl"
    write_jsonl(path, [user_str_line("hi", sessionId=CLEARED), placeholder])

    result = parse_transcript(path, _top_meta(path, CLEARED))

    assert result.diagnostics.copied_lines == 0
    assert any(turn.is_synthetic for turn in result.turns)

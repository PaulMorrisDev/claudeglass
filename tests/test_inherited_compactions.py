"""A session continued after a compaction opens with the boundary of the
session it came from. ``parse.py`` marks it ``detail["inherited"]`` and
every count of compactions has to leave it out, or a continued session
shows one compaction it never had (the source session already counts it).

The compaction tables and the replay skip it in their own tests
(``test_compaction*.py``); these cover the other places that count
``COMPACT_BOUNDARY`` events: the compare, savers, quality, impact and
hook-health figures and the session metrics the ``recommend`` view uses.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claudeglass import capture, classify, cli, compare as compare_mod, hook_health, impact, quality, recache, savers
from claudeglass.config import Config
from claudeglass.corpus import load_corpus
from claudeglass.model import Classification, EventKind, SessionRecord, TranscriptMeta, TranscriptResult
from claudeglass.parse import parse_transcript
from claudeglass.pricing import PricingCoverage, load_pricing
from claudeglass.topology import TopologyStats

from helpers import system_line, turn_line, user_str_line, write_jsonl

PRICING = load_pricing()
SESSION = "2936e571-ffaa-4e4c-bfdd-cbb1c84d40d4"


def _continued_lines(extra_compaction: bool = False) -> list[dict]:
    """A continued session: the inherited boundary and a zeroed copy of the
    preserved reply, then the session's own replies. With
    ``extra_compaction`` it also compacts once itself, which must count."""
    lines = [
        system_line("compact_boundary", sessionId=SESSION, uuid="z-compact", timestamp="2026-10-20T09:00:00.000Z",
                    compactMetadata={"trigger": "auto", "preTokens": 150000, "postTokens": 9000, "durationMs": 40000}),
        turn_line(message_id="msg_kept", input_tokens=0, output_tokens=0, sessionId=SESSION, uuid="z-asst-1",
                  timestamp="2026-10-20T09:00:01.000Z"),
        user_str_line("carry on", sessionId=SESSION, uuid="z-user-1", timestamp="2026-10-20T09:01:00.000Z"),
        turn_line(message_id="msg_own", output_tokens=200, sessionId=SESSION, uuid="z-asst-2",
                  timestamp="2026-10-20T09:01:05.000Z"),
    ]
    if extra_compaction:
        lines += [
            system_line("compact_boundary", sessionId=SESSION, uuid="z-compact-2", timestamp="2026-10-20T09:30:00.000Z",
                        compactMetadata={"trigger": "manual", "preTokens": 120000, "postTokens": 8000, "durationMs": 30000}),
            user_str_line("and again", sessionId=SESSION, uuid="z-user-2", timestamp="2026-10-20T09:31:00.000Z"),
            turn_line(message_id="msg_after", output_tokens=100, sessionId=SESSION, uuid="z-asst-3",
                      timestamp="2026-10-20T09:31:05.000Z"),
        ]
    return lines


def _folder(tmp_path: Path, **kwargs) -> Path:
    folder = tmp_path / "-home-user-shop"
    folder.mkdir()
    write_jsonl(folder / f"{SESSION}.jsonl", _continued_lines(**kwargs))
    return folder


def _parsed(tmp_path: Path, **kwargs) -> TranscriptResult:
    folder = _folder(tmp_path, **kwargs)
    path = folder / f"{SESSION}.jsonl"
    return parse_transcript(path, TranscriptMeta(path=str(path), kind="top-level", session_id=SESSION))


def _record(result: TranscriptResult) -> SessionRecord:
    return SessionRecord(
        session_id=SESSION,
        slug="shop",
        first_ts="2026-10-20T09:00:00.000Z",
        last_ts="2026-10-20T09:31:05.000Z",
        top=result,
        subs=[],
        classification=Classification(mode="build", purpose="feature"),
    )


@pytest.mark.parametrize("extra, expected", [(False, 0), (True, 1)])
def test_a_continued_session_counts_only_its_own_compactions(tmp_path: Path, extra: bool, expected: int):
    result = _parsed(tmp_path, extra_compaction=extra)
    boundaries = [event for event in result.events if event.kind == EventKind.COMPACT_BOUNDARY]
    assert len(boundaries) == 1 + extra
    assert boundaries[0].detail["inherited"] is True

    corpus = load_corpus([tmp_path / "-home-user-shop"])
    [metrics] = compare_mod._collect_session_metrics(
        corpus, PRICING, Config(), {}, recache.RecacheThresholds(), PricingCoverage()
    )
    assert metrics.compactions == expected

    [agg] = savers._build_session_aggs([_record(result)], [], [], PRICING, recache.RecacheThresholds())
    assert agg.compactions == expected

    assert quality.run_facts(result, PRICING).compactions == expected
    assert impact._transcript(result, PRICING).summaries == expected


def test_a_continued_session_adds_no_session_start_for_its_inherited_boundary(tmp_path: Path):
    result = _parsed(tmp_path)
    spec = hook_health.HookSpec("capture-hook.py", "SessionStart", "compact")
    [row] = hook_health.measure_hook_overhead([result], [spec]).rows
    assert row.runs == 0


def test_the_session_metrics_leave_out_the_inherited_boundary(tmp_path: Path):
    corpus = load_corpus([_folder(tmp_path)])
    [metrics] = cli._build_session_metrics(corpus, PRICING, recache.RecacheThresholds(), Config(), {})
    assert metrics["compactions"] == 0


def test_the_counts_over_events_leave_out_the_inherited_boundary_and_summary(tmp_path: Path):
    """The readers that count events (not turns) leave out what the session
    this one continues already counts: the classifier's compaction count,
    the topology's summary count and the capture segments."""
    own_summary = "This session is being continued from a previous conversation. Its own summary."
    result = _parsed(tmp_path)
    assert classify.extract_features(result, [], tz=None).compactions == 0
    assert capture._segment_ends(result, capture._Carry(result, PRICING)) == []

    # One of its own, with the summary that follows it.
    path = tmp_path / "own" / f"{SESSION}.jsonl"
    path.parent.mkdir()
    lines = _continued_lines(extra_compaction=True)
    lines.insert(-2, user_str_line(own_summary, sessionId=SESSION, uuid="z-summary-2",
                                   timestamp="2026-10-20T09:30:30.000Z"))
    write_jsonl(path, lines)
    own = parse_transcript(path, TranscriptMeta(path=str(path), kind="top-level", session_id=SESSION))
    assert classify.extract_features(own, [], tz=None).compactions == 1
    assert len(capture._segment_ends(own, capture._Carry(own, PRICING))) == 1

    for result_, expected in ((result, 0), (own, 1)):
        stats = TopologyStats()
        stats.add_session(SESSION, result_, [], PRICING)
        assert [n for counts in stats.composition_compactions.values() for n in counts] == [expected]


def test_a_continued_sessions_first_reply_does_not_follow_a_summary_of_its_own(tmp_path: Path):
    """The first reply of a session that was continued after a compaction
    follows the boundary and summary of the session it came from. Turn-level
    readers see only kinds, and all of them mean "this reply follows a
    summary of this session", so the parser leaves the inherited pair out."""
    path = tmp_path / f"{SESSION}.jsonl"
    write_jsonl(path, [
        system_line("compact_boundary", sessionId=SESSION, uuid="z-compact", timestamp="2026-10-20T09:00:00.000Z",
                    compactMetadata={"trigger": "auto", "preTokens": 150000, "postTokens": 9000, "durationMs": 40000}),
        user_str_line("This session is being continued from a previous conversation. Summary of that work.",
                      sessionId=SESSION, uuid="z-summary", timestamp="2026-10-20T09:00:01.000Z"),
        user_str_line("carry on", sessionId=SESSION, uuid="z-user-1", timestamp="2026-10-20T09:01:00.000Z"),
        turn_line(message_id="msg_own", output_tokens=200, sessionId=SESSION, uuid="z-asst-2",
                  timestamp="2026-10-20T09:01:05.000Z"),
    ])

    result = parse_transcript(path, TranscriptMeta(path=str(path), kind="top-level", session_id=SESSION))

    [turn] = result.turns
    assert turn.turn_index == 1
    assert EventKind.COMPACT_BOUNDARY not in turn.preceding_event_kinds
    assert EventKind.COMPACT_SUMMARY not in turn.preceding_event_kinds
    assert turn.preceding_primary == EventKind.HUMAN_TEXT
    inherited = {event.kind: event.detail.get("inherited") for event in result.events
                 if event.kind in (EventKind.COMPACT_BOUNDARY, EventKind.COMPACT_SUMMARY)}
    assert inherited == {EventKind.COMPACT_BOUNDARY: True, EventKind.COMPACT_SUMMARY: True}

    # Its own compaction still precedes the reply after it.
    own = _parsed(tmp_path, extra_compaction=True)
    after = next(t for t in own.turns if t.message_id == "msg_after")
    assert EventKind.COMPACT_BOUNDARY in after.preceding_event_kinds
    assert after.preceding_primary == EventKind.COMPACT_BOUNDARY
    assert all(EventKind.COMPACT_BOUNDARY not in t.preceding_event_kinds for t in own.turns if t is not after)


def test_a_file_that_opens_with_a_summary_and_no_boundary_is_left_alone(tmp_path: Path):
    path = tmp_path / f"{SESSION}.jsonl"
    write_jsonl(path, [
        user_str_line("This session is being continued from a previous conversation. Summary of that work.",
                      sessionId=SESSION, uuid="z-summary", timestamp="2026-10-20T09:00:01.000Z"),
        turn_line(message_id="msg_own", output_tokens=200, sessionId=SESSION, uuid="z-asst-2",
                  timestamp="2026-10-20T09:01:05.000Z"),
    ])
    result = parse_transcript(path, TranscriptMeta(path=str(path), kind="top-level", session_id=SESSION))
    assert result.turns[0].preceding_primary == EventKind.COMPACT_SUMMARY
    assert [e.detail.get("inherited") for e in result.events if e.kind == EventKind.COMPACT_SUMMARY] == [None]

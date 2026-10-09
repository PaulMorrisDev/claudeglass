"""Tests for ``limits.py``: usage-cap pause intervals/markers feeding
classify.py and the session-timeline API, and the ``limits`` report
section's accumulation/rendering.

Fixtures are built with ``turn_line``/``user_str_line`` (never real
transcript text — see tests/helpers.py and SECURITY.md).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from claudeglass import events, helptext, limits, parse, recommend
from claudeglass.model import EventKind, ReportMeta, ReportModel, TranscriptMeta
from claudeglass.parse import parse_transcript
from claudeglass.pricing import ModelRates

from helpers import assert_privacy, turn_line, user_str_line, write_jsonl

#: A session-limit line naming a reset at 3pm London time: 14:00 UTC on the
#: fixtures' day, in summer time.
_SESSION_LIMIT_TEXT = "You've hit your session limit · resets 3pm (Europe/London)"
_RESET = datetime(2026, 9, 18, 14, 0, tzinfo=timezone.utc)
_BACK = datetime(2026, 9, 18, 15, 0, tzinfo=timezone.utc)


def _iso(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _utc(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 18, hour, minute, tzinfo=timezone.utc)


def _session_limit_fixture(
    tmp_path: Path,
    session_id: str = "sess1",
    *,
    text: str = _SESSION_LIMIT_TEXT,
    resets_at: datetime | None = _RESET,
    back: datetime = _BACK,
) -> Path:
    """A reply at 12:00:00, a limit line at 12:00:05 (with the line's own
    ``quotaLimits.resetsAt`` when ``resets_at`` is given, so the reset does
    not depend on this machine's zone), and a return typed at ``back`` that
    the next reply follows by ten seconds."""
    quota = {"quotaLimits": {"resetsAt": resets_at.timestamp()}} if resets_at is not None else {}
    lines = [
        turn_line(message_id="msg_1", input_tokens=30_000, timestamp="2026-09-18T12:00:00.000Z"),
        turn_line(
            message_id="msg_synth",
            model="<synthetic>",
            isApiErrorMessage=True,
            input_tokens=0,
            output_tokens=0,
            content=[{"type": "text", "text": text}],
            timestamp="2026-09-18T12:00:05.000Z",
            **quota,
        ),
        user_str_line(
            "I hit my usage limit while you were working, but it has reset now.",
            promptSource="sdk",
            origin={"kind": "human"},
            timestamp=_iso(back),
        ),
        turn_line(
            message_id="msg_2",
            input_tokens=30_000,
            cache_creation_input_tokens=25_000,
            cache_read_input_tokens=0,
            timestamp=_iso(back + timedelta(seconds=10)),
        ),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    return path


def _parse(path: Path):
    return parse_transcript(path, TranscriptMeta(path=str(path), session_id="sess1"))


def _hit_detail(result) -> dict:
    return next(detail for _ts, kind, detail in limits.limit_markers(result) if kind == "limit_hit")


# -- limit stops (episodes) ------------------------------------------------------

#: Every rate $1 per million tokens, so a reply of a million input tokens costs $1.
_RATES = ModelRates(
    canonical_id="claude-sonnet-5", input=1.0, output=1.0, cache_write_5m=1.0, cache_write_1h=1.0, cache_read=1.0
)


def _when(day: int, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 9, day, hour, minute, second, tzinfo=timezone.utc)


def _limit_line(
    message_id: str,
    at: datetime,
    resets_at: datetime | None = _RESET,
    *,
    weekly: bool = False,
    text: str | None = None,
    **extra,
) -> dict:
    """A synthetic limit line written at ``at``, naming ``resets_at`` through
    its own ``quotaLimits.resetsAt`` when given (so the reset does not depend
    on this machine's zone)."""
    quota = {"quotaLimits": {"resetsAt": resets_at.timestamp()}} if resets_at is not None else {}
    if text is None:
        text = "You've hit your weekly limit" if weekly else "You've hit your session limit"
    return turn_line(
        message_id=message_id,
        model="<synthetic>",
        isApiErrorMessage=True,
        input_tokens=0,
        output_tokens=0,
        content=[{"type": "text", "text": text}],
        timestamp=_iso(at),
        **quota,
        **extra,
    )


def _reply(message_id: str, at: datetime, **extra) -> dict:
    return turn_line(message_id=message_id, timestamp=_iso(at), **extra)


def _transcript(
    tmp_path: Path, name: str, lines: list[dict], *, kind: str = "top-level", session_id: str = "sess1", agent_type=None
):
    path = tmp_path / f"{name}.jsonl"
    write_jsonl(path, lines)
    return parse_transcript(
        path, TranscriptMeta(path=str(path), kind=kind, session_id=session_id, agent_type=agent_type)
    )


def _stats(*results, rates_lookup=None) -> limits.LimitStats:
    stats = limits.LimitStats()
    for result in results:
        stats.add(result, rates_lookup)
    return stats


def _summary(stats: limits.LimitStats) -> dict:
    table = limits.build_section(stats).tables[0]
    assert table.name == "limits_summary"
    return {column.key: value for column, value in zip(table.columns, table.rows[0])}


def _rule_fires(stats: limits.LimitStats) -> bool:
    report = ReportModel(meta=ReportMeta(), sections=[limits.build_section(stats)])
    return bool(recommend._rule_limit_pressure(report, recommend.RecommendThresholds()))


def test_limit_pause_intervals_reads_gap_backward_from_the_post_pause_turn(tmp_path: Path):
    # No reset is named, so the pause runs to the reply that follows the return.
    path = _session_limit_fixture(tmp_path, text="You've hit your session limit", resets_at=None)
    result = _parse(path)

    intervals = limits.limit_pause_intervals(result)
    assert len(intervals) == 1
    start, end = intervals[0]
    assert end.isoformat().startswith("2026-09-18T15:00:10")
    # From the last real turn (12:00:00), not the synthetic limit notice.
    assert start.isoformat().startswith("2026-09-18T12:00:00")
    assert (end - start).total_seconds() == 3 * 3600 + 10


def test_a_pause_ends_at_the_reset_when_you_came_back_later(tmp_path: Path):
    result = _parse(_session_limit_fixture(tmp_path))  # reset 14:00, back 15:00

    assert limits.limit_pause_intervals(result) == [(_utc(12), _utc(14))]


def test_a_return_three_hours_after_the_reset_takes_only_the_time_up_to_the_reset(tmp_path: Path):
    result = _parse(_session_limit_fixture(tmp_path, back=_RESET + timedelta(hours=3)))

    [(start, end)] = limits.limit_pause_intervals(result)
    assert (start, end) == (_utc(12), _utc(14))
    assert (end - start).total_seconds() == 2 * 3600


def test_a_pause_ends_at_the_return_when_that_comes_before_the_reset(tmp_path: Path):
    result = _parse(_session_limit_fixture(tmp_path, resets_at=_utc(18)))

    [(start, end)] = limits.limit_pause_intervals(result)
    assert (start, end) == (_utc(12), _BACK + timedelta(seconds=10))


def test_a_limit_line_with_no_reset_ends_its_pause_at_the_return(tmp_path: Path):
    result = _parse(_session_limit_fixture(tmp_path, text="You've hit your session limit", resets_at=None))

    assert "reset_ts" not in _hit_detail(result)
    assert limits.limit_pause_intervals(result) == [(_utc(12), _BACK + timedelta(seconds=10))]


def test_a_reset_that_was_over_before_the_limit_line_is_no_reset(tmp_path: Path):
    result = _parse(_session_limit_fixture(tmp_path, resets_at=_utc(11)))

    assert limits.limit_pause_intervals(result) == [(_utc(12), _BACK + timedelta(seconds=10))]


def test_a_wait_with_two_limit_lines_ends_at_the_later_reset(tmp_path: Path):
    def limit_line(message_id: str, at: str, resets_at: datetime) -> dict:
        return turn_line(
            message_id=message_id,
            model="<synthetic>",
            isApiErrorMessage=True,
            input_tokens=0,
            output_tokens=0,
            content=[{"type": "text", "text": "You've hit your session limit"}],
            timestamp=at,
            quotaLimits={"resetsAt": resets_at.timestamp()},
        )

    lines = [
        turn_line(message_id="msg_1", timestamp="2026-09-18T12:00:00.000Z"),
        limit_line("msg_synth_1", "2026-09-18T12:00:05.000Z", _utc(13)),
        user_str_line("try again", origin={"kind": "human"}, timestamp="2026-09-18T12:30:00.000Z"),
        limit_line("msg_synth_2", "2026-09-18T12:30:05.000Z", _utc(14)),
        user_str_line("back now", origin={"kind": "human"}, timestamp="2026-09-18T16:00:00.000Z"),
        turn_line(message_id="msg_2", timestamp="2026-09-18T16:00:10.000Z"),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)

    assert limits.limit_pause_intervals(_parse(path)) == [(_utc(12), _utc(14))]


def test_a_session_with_no_limit_has_no_pause_intervals(tmp_path: Path):
    path = tmp_path / "session.jsonl"
    write_jsonl(path, [turn_line(message_id="msg_1", timestamp="2026-09-18T12:00:00.000Z")])

    assert limits.limit_pause_intervals(_parse(path)) == []


def test_pause_overlap_s_counts_only_the_time_inside_pauses():
    pauses = [(_utc(10), _utc(11)), (_utc(12), _utc(13))]

    assert limits.pause_overlap_s(_utc(9), _utc(14), pauses) == 2 * 3600
    assert limits.pause_overlap_s(_utc(10, 30), _utc(12, 30), pauses) == 3600
    assert limits.pause_overlap_s(_utc(13), _utc(14), pauses) == 0.0
    assert limits.pause_overlap_s(_utc(9), _utc(14), []) == 0.0


# -- the reset time of a limit line ---------------------------------------------------


def test_a_reset_in_a_zone_that_cannot_be_resolved_is_read_in_the_machine_zone(tmp_path: Path):
    # A name zoneinfo can't resolve (every one of them on a Windows install without
    # tzdata) is the machine's own zone, as every other local time here is.
    text = "You've hit your session limit · resets 3pm (Mars/Olympus)"
    detail = _hit_detail(_parse(_session_limit_fixture(tmp_path, text=text, resets_at=None)))

    hit = datetime(2026, 9, 18, 12, 0, 5, tzinfo=timezone.utc)
    wall = hit.astimezone().replace(tzinfo=None, hour=15, minute=0, second=0, microsecond=0)
    expected = wall.astimezone(timezone.utc)
    if expected <= hit:
        expected = (wall + timedelta(days=1)).astimezone(timezone.utc)
    assert detail["reset_ts"] == expected.isoformat().replace("+00:00", "Z")
    assert detail["reset_minutes_of_day"] == 15 * 60


def test_a_reset_in_a_zone_that_resolves_is_read_in_that_zone(tmp_path: Path):
    text = "You've hit your session limit · resets 3pm (Etc/UTC)"
    detail = _hit_detail(_parse(_session_limit_fixture(tmp_path, text=text, resets_at=None)))
    assert detail["reset_ts"] == "2026-09-18T15:00:00Z"

    # A time of day that has gone by is the next day's.
    text = "You've hit your session limit · resets 9am (Etc/UTC)"
    detail = _hit_detail(_parse(_session_limit_fixture(tmp_path, text=text, resets_at=None)))
    assert detail["reset_ts"] == "2026-09-19T09:00:00Z"


def test_the_line_s_own_reset_time_wins_over_the_clause(tmp_path: Path):
    text = "You've hit your session limit · resets 3pm (Etc/UTC)"
    detail = _hit_detail(_parse(_session_limit_fixture(tmp_path, text=text, resets_at=_utc(13, 30))))

    assert detail["reset_ts"] == "2026-09-18T13:30:00Z"


def test_the_weekly_form_with_a_date_resets_on_that_date(tmp_path: Path):
    text = "You've hit your weekly limit · resets Sep 22, 9am (Etc/UTC)"
    detail = _hit_detail(_parse(_session_limit_fixture(tmp_path, text=text, resets_at=None)))

    assert detail["subkind"] == "weekly_limit"
    assert detail["reset_ts"] == "2026-09-22T09:00:00Z"


def test_a_dated_reset_that_has_gone_by_this_year_is_next_year_s():
    ts = datetime(2026, 12, 30, 12, 0, tzinfo=timezone.utc)
    assert parse._limit_reset_ts({}, ts, 9 * 60, "Etc/UTC", (1, 2)) == "2027-01-02T09:00:00Z"
    # The same day, an hour after the time named: still this year's (logged late).
    ts = datetime(2026, 10, 3, 10, 0, tzinfo=timezone.utc)
    assert parse._limit_reset_ts({}, ts, 9 * 60, "Etc/UTC", (10, 3)) == "2026-10-03T09:00:00Z"
    # Just after New Year, naming 31 December: last year's.
    new_year = datetime(2027, 1, 1, 0, 30, tzinfo=timezone.utc)
    assert parse._limit_reset_ts({}, new_year, 23 * 60, "Etc/UTC", (12, 31)) == "2026-12-31T23:00:00Z"
    # A day that does not exist is no reset.
    assert parse._limit_reset_ts({}, ts, 9 * 60, "Etc/UTC", (2, 30)) is None


def test_a_dated_reset_in_the_machine_zone_follows_that_day_s_clocks():
    # A week at most ahead, with the clocks changing in between in Europe.
    ts = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
    reset = parse._limit_reset_ts({}, ts, 9 * 60, "Mars/Olympus", (10, 28))

    expected = datetime(2026, 10, 28, 9, 0).astimezone(timezone.utc)
    assert reset == expected.isoformat().replace("+00:00", "Z")


def test_the_reset_clause_names_the_date_of_the_weekly_form():
    assert events.parse_limit_reset_clause("resets Oct 3, 9am (Europe/London)") == (9 * 60, "Europe/London")
    assert events.parse_limit_reset_date("resets Oct 3, 9am (Europe/London)") == (10, 3)
    assert events.parse_limit_reset_date("resets September 30, 9:30pm (Europe/London)") == (9, 30)
    assert events.parse_limit_reset_date("resets Sept 3 9am (Europe/London)") == (9, 3)


def test_the_five_hour_form_names_a_time_of_day_and_no_date():
    assert events.parse_limit_reset_clause("resets 3pm (Europe/London)") == (15 * 60, "Europe/London")
    assert events.parse_limit_reset_date("resets 3pm (Europe/London)") is None
    assert events.parse_limit_reset_date("resets 9:30am (Europe/London)") is None


def test_a_text_with_no_reset_clause_or_a_day_off_the_calendar_names_no_date():
    assert events.parse_limit_reset_date(None) is None
    assert events.parse_limit_reset_date("You've hit your session limit") is None
    assert events.parse_limit_reset_date("resets Oct 0, 9am (Europe/London)") is None
    assert events.parse_limit_reset_date("resets Oct 32, 9am (Europe/London)") is None
    assert events.parse_limit_reset_clause("resets Oct 32, 9am (Europe/London)") == (9 * 60, "Europe/London")




def test_limit_markers_carries_kind_and_detail(tmp_path: Path):
    path = _session_limit_fixture(tmp_path)
    result = parse_transcript(path, TranscriptMeta(path=str(path), session_id="sess1"))

    markers = limits.limit_markers(result)
    kinds = [m[1] for m in markers]
    assert "limit_hit" in kinds
    assert "limit_resume" in kinds
    hit_marker = next(m for m in markers if m[1] == "limit_hit")
    assert hit_marker[2]["subkind"] == "session_limit"
    assert hit_marker[2]["reset_minutes_of_day"] == 15 * 60


def test_limit_stats_accumulates_hits_resumes_and_pause(tmp_path: Path):
    path = _session_limit_fixture(tmp_path)
    result = parse_transcript(path, TranscriptMeta(path=str(path), session_id="sess1"))

    stats = limits.LimitStats()
    stats.add(result)

    rows = stats.by_key()
    assert len(rows) == 1
    row = rows[0]
    assert row.key == "top-level"
    assert row.session_limit_hits == 1
    assert row.weekly_limit_hits == 0
    assert row.resumes == 1
    assert row.pause_count == 1
    # From the reply before the limit to the reset, not to the return an hour later.
    assert row.pause_total_s == 2 * 3600
    assert row.limit_turn_cc_tokens == 25_000
    assert stats.sessions_affected == {"sess1"}


def test_reset_hour_counts_from_reset_minutes_of_day(tmp_path: Path):
    path = _session_limit_fixture(tmp_path)
    result = parse_transcript(path, TranscriptMeta(path=str(path), session_id="sess1"))

    stats = limits.LimitStats()
    stats.add(result)
    counts = stats.reset_hour_counts()
    assert counts[15] == 1
    assert sum(counts.values()) == 1


def test_agent_terminated_split_by_subkind(tmp_path: Path):
    lines = [
        turn_line(message_id="msg_1", timestamp="2026-09-18T12:00:00.000Z"),
        user_str_line(
            "Agent terminated early due to an API error: rate limit hit (error type rate_limit, HTTP 429).",
            origin={"kind": "task-notification"},
            timestamp="2026-09-18T12:00:05.000Z",
        ),
        user_str_line(
            "Agent terminated early due to a network error.",
            origin={"kind": "task-notification"},
            timestamp="2026-09-18T12:00:06.000Z",
        ),
        turn_line(message_id="msg_2", timestamp="2026-09-18T12:05:00.000Z"),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path), session_id="sess2"))

    stats = limits.LimitStats()
    stats.add(result)
    rows = stats.by_key()
    row = rows[0]
    assert row.terminated_rate_limit == 1
    assert row.terminated_other == 1
    # A terminated notice is no limit line, so it puts no session in a stop.
    assert stats.sessions_affected == set()


def test_build_section_shape_and_privacy(tmp_path: Path):
    path = _session_limit_fixture(tmp_path)
    result = parse_transcript(path, TranscriptMeta(path=str(path), session_id="sess1"))

    stats = limits.LimitStats()
    stats.add(result, rates_lookup=lambda _model: None)
    section = limits.build_section(stats)

    assert section.key == "limits"
    table_names = {t.name for t in section.tables}
    assert table_names == {
        "limits_summary",
        "limits_stops_rollup",
        "limits_stops",
        "limits_hits_by_kind",
        "limits_agent_terminated",
        "limits_pauses",
        "limits_reset_hour_histogram",
        "limits_wake_gaps",
        "limits_by_agent_type",
    }
    summary = next(t for t in section.tables if t.name == "limits_summary")
    assert summary.rows[0][0] == "all"
    assert_privacy(section)


def test_csv_cross_check_counts_exhaustion_rows_against_transcript_hits(tmp_path: Path):
    path = _session_limit_fixture(tmp_path)
    result = parse_transcript(path, TranscriptMeta(path=str(path), session_id="sess1"))
    stats = limits.LimitStats()
    stats.add(result)

    rows = [
        {"window": "five_hour", "used_percentage": 100.0},
        {"window": "five_hour", "used_percentage": 42.0},
        {"window": "seven_day", "used_percentage": 12.0},
    ]
    table = limits.csv_cross_check(rows, stats)
    assert [column.key for column in table.columns] == ["window", "csv_exhaustion_rows", "transcript_stops", "delta"]
    by_window = {row[0]: row for row in table.rows}
    assert by_window["five_hour"][1] == 1  # one row >= 100%
    assert by_window["five_hour"][2] == 1  # one transcript-derived five-hour stop
    assert by_window["seven_day"][1] == 0
    assert by_window["seven_day"][2] == 0


def test_two_transcripts_sharing_a_reset_are_one_stop(tmp_path: Path):
    main = _transcript(tmp_path, "main", [_reply("m1", _when(18, 12)), _limit_line("l1", _when(18, 12, 0, 5))])
    agent = _transcript(
        tmp_path,
        "agent",
        [_reply("a1", _when(18, 11, 59)), _limit_line("l2", _when(18, 12, 0, 20))],
        kind="subagent",
        agent_type="reviewer",
    )
    stats = _stats(main, agent)

    [episode] = stats.episodes()
    assert (episode.kind, episode.messages, episode.main_messages, episode.agent_messages) == (
        "session_limit",
        2,
        1,
        1,
    )
    assert episode.reset == _RESET
    summary = _summary(stats)
    assert (summary["five_hour_stops"], summary["weekly_stops"], summary["limit_hits"]) == (1, 0, 2)


def test_resets_a_minute_apart_share_a_key_and_resets_two_hours_apart_merge(tmp_path: Path):
    def stops(*resets: datetime) -> list:
        lines = [_reply("m0", _when(18, 12))] + [
            _limit_line(f"l{i}", _when(18, 12, 0, 5 + i), reset) for i, reset in enumerate(resets)
        ]
        return _stats(_transcript(tmp_path, f"s{len(resets)}{resets[-1].minute}", lines)).episodes()

    [merged] = stops(_utc(14, 0), _utc(14, 59), _utc(15, 59))
    assert merged.messages == 3 and merged.reset == _utc(15, 59)
    # More than two hours from the one before is another stop.
    assert [e.messages for e in stops(_utc(14, 0), _utc(16, 1))] == [1, 1]


def test_a_reset_is_floored_to_the_minute(tmp_path: Path):
    line = _limit_line("l1", _when(18, 12, 0, 5), _RESET + timedelta(seconds=42))
    [episode] = _stats(_transcript(tmp_path, "s", [_reply("m1", _when(18, 12)), line])).episodes()

    assert episode.reset == _RESET


def test_a_line_with_no_reset_joins_the_stop_whose_reset_follows_it(tmp_path: Path):
    main = _transcript(tmp_path, "main", [_reply("m1", _when(18, 12)), _limit_line("l1", _when(18, 12, 0, 5))])
    agent = _transcript(
        tmp_path,
        "agent",
        [_reply("a1", _when(18, 12, 20)), _limit_line("l2", _when(18, 12, 30), None)],
        kind="workflow-agent",
    )
    stats = _stats(main, agent)

    [episode] = stats.episodes()
    assert episode.messages == 2
    assert (episode.main_messages, episode.agent_messages) == (1, 1)


def test_a_line_with_no_reset_does_not_join_a_reset_outside_its_window(tmp_path: Path):
    # Five-hour: the reset must lie 0-5 h after the line. At 20:00 the 14:00 reset has gone by,
    # and at 08:30 it is 5.5 h away.
    keyed = _transcript(tmp_path, "keyed", [_reply("m1", _when(18, 12)), _limit_line("l1", _when(18, 12, 0, 5))])
    late = _transcript(tmp_path, "late", [_reply("a1", _when(18, 19)), _limit_line("l2", _when(18, 20), None)])
    early = _transcript(tmp_path, "early", [_reply("a2", _when(18, 8)), _limit_line("l3", _when(18, 8, 30), None)])

    assert sorted(e.messages for e in _stats(keyed, late, early).episodes()) == [1, 1, 1]


def test_a_weekly_line_with_no_reset_joins_a_reset_up_to_seven_days_ahead(tmp_path: Path):
    reset = _when(25, 9)
    keyed = _transcript(
        tmp_path, "keyed", [_reply("m1", _when(22, 10)), _limit_line("l1", _when(22, 10, 0, 5), reset, weekly=True)]
    )
    near = _transcript(
        tmp_path, "near", [_reply("m2", _when(19, 10)), _limit_line("l2", _when(19, 10, 0, 5), None, weekly=True)]
    )
    far = _transcript(
        tmp_path, "far", [_reply("m3", _when(17, 8)), _limit_line("l3", _when(17, 8, 0, 5), None, weekly=True)]
    )

    episodes = _stats(keyed, near, far).episodes()
    assert sorted(e.messages for e in episodes) == [1, 2]
    # A weekly line never joins a five-hour stop.
    five = _transcript(tmp_path, "five", [_reply("m4", _when(24, 9)), _limit_line("l4", _when(24, 9, 0, 5), reset)])
    assert sorted((e.kind, e.messages) for e in _stats(keyed, five).episodes()) == [
        ("session_limit", 1),
        ("weekly_limit", 1),
    ]


def test_lines_with_no_reset_and_no_stop_to_join_make_one_stop_per_storm(tmp_path: Path):
    lines = [_reply("m1", _when(18, 12))]
    lines += [_limit_line(f"a{i}", _when(18, 12, i), None) for i in range(1, 6)]
    lines += [_limit_line("b1", _when(18, 20), None)]
    stats = _stats(_transcript(tmp_path, "s", lines))

    assert sorted(e.messages for e in stats.episodes()) == [1, 5]
    assert stats.reset_hour_counts() == {hour: 0 for hour in range(24)}


def test_replayed_limit_lines_count_once_by_file_and_uuid(tmp_path: Path):
    line = _limit_line("l1", _when(18, 12, 0, 5), uuid="uuid_replayed")
    result = _transcript(tmp_path, "s", [_reply("m1", _when(18, 12)), line, line, line])

    assert result.diagnostics.replayed_lines == 2
    stats = _stats(result)
    assert _summary(stats)["limit_hits"] == 1
    assert [e.messages for e in stats.episodes()] == [1]


def test_a_six_line_storm_is_one_stop_and_does_not_trigger_the_rule(tmp_path: Path):
    lines = [_reply("m1", _when(18, 12))] + [_limit_line(f"l{i}", _when(18, 12, 0, 5 + i)) for i in range(6)]
    stats = _stats(_transcript(tmp_path, "s", lines))

    summary = _summary(stats)
    assert (summary["five_hour_stops"], summary["limit_hits"], summary["session_limit_hits"]) == (1, 6, 6)
    assert not _rule_fires(stats)


def test_two_five_hour_stops_in_a_week_trigger_the_rule(tmp_path: Path):
    lines = [_reply("m1", _when(18, 12)), _limit_line("l1", _when(18, 12, 0, 5), _when(18, 14))]
    lines += [_reply("m2", _when(19, 12)), _limit_line("l2", _when(19, 12, 0, 5), _when(19, 14))]
    stats = _stats(_transcript(tmp_path, "s", lines))

    assert _summary(stats)["five_hour_stops"] == 2
    assert _rule_fires(stats)


def test_a_58_line_stop_adds_one_to_its_hour(tmp_path: Path):
    lines = [_reply("m1", _when(18, 12))]
    lines += [
        _limit_line(f"l{i}", _when(18, 12, 0, 5) + timedelta(seconds=i), text=_SESSION_LIMIT_TEXT) for i in range(58)
    ]
    stats = _stats(_transcript(tmp_path, "s", lines))

    assert _summary(stats)["limit_hits"] == 58
    counts = stats.reset_hour_counts()
    assert counts[15] == 1
    assert sum(counts.values()) == 1


def test_a_weekly_stop_with_a_main_reply_thirty_minutes_later_did_not_stop_work(tmp_path: Path):
    reset = _when(25, 9)
    lines = [
        _reply("m1", _when(22, 9, 59)),
        _limit_line("l1", _when(22, 10, 0, 5), reset, weekly=True),
        _reply("m2", _when(22, 10, 30)),
    ]
    stats = _stats(_transcript(tmp_path, "s", lines))

    [episode] = stats.episodes()
    assert episode.kind == "weekly_limit"
    assert episode.kept_working and not episode.stopped_work
    summary = _summary(stats)
    assert (summary["weekly_stops"], summary["weekly_stops_stopped_work"]) == (1, 0)
    assert not _rule_fires(stats)


def test_a_weekly_stop_still_stops_work_when_replies_are_in_flight_or_after_the_reset(tmp_path: Path):
    reset = _when(25, 9)
    in_flight = [
        _reply("m1", _when(22, 9, 59)),
        _limit_line("l1", _when(22, 10, 0, 5), reset, weekly=True),
        _reply("m2", _when(22, 10, 5)),  # five minutes later: inside the ten-minute margin
        _reply("m3", _when(25, 9, 30)),  # after the reset
    ]
    stats = _stats(_transcript(tmp_path, "s", in_flight))

    assert not stats.episodes()[0].kept_working
    assert _summary(stats)["weekly_stops_stopped_work"] == 1
    assert _rule_fires(stats)


def test_a_reply_in_another_main_session_shows_a_weekly_stop_did_not_stop_work(tmp_path: Path):
    reset = _when(25, 9)
    stopped = _transcript(
        tmp_path,
        "stopped",
        [_reply("m1", _when(22, 9, 59)), _limit_line("l1", _when(22, 10, 0, 5), reset, weekly=True)],
        session_id="sess1",
    )
    other = _transcript(tmp_path, "other", [_reply("o1", _when(22, 11))], session_id="sess2")

    assert _stats(stopped, other).episodes()[0].kept_working
    # An agent's reply is no main-session reply.
    agent = _transcript(tmp_path, "agent", [_reply("a1", _when(22, 11))], kind="subagent", session_id="sess2")
    assert not _stats(stopped, agent).episodes()[0].kept_working


def test_a_weekly_stop_with_no_reset_counts_as_stopping_work(tmp_path: Path):
    lines = [_reply("m1", _when(22, 9, 59)), _limit_line("l1", _when(22, 10, 0, 5), None, weekly=True)]
    lines += [_reply("m2", _when(22, 11))]
    stats = _stats(_transcript(tmp_path, "s", lines))

    assert _summary(stats)["weekly_stops_stopped_work"] == 1


def test_a_workflow_agent_whose_last_line_is_a_limit_line_is_cut_off(tmp_path: Path):
    main = _transcript(tmp_path, "main", [_reply("m1", _when(18, 12)), _limit_line("l1", _when(18, 12, 0, 5))])
    agent = _transcript(
        tmp_path,
        "agent",
        [
            _reply("a1", _when(18, 11, 58), input_tokens=1_000_000, output_tokens=0),
            _reply("a2", _when(18, 11, 59), input_tokens=1_000_000, output_tokens=0),
            _limit_line("l2", _when(18, 12, 0, 20)),
        ],
        kind="workflow-agent",
        agent_type="implementer",
    )
    stats = _stats(main, agent, rates_lookup=lambda _model: _RATES)

    [episode] = stats.episodes()
    assert (episode.cut_off_direct, episode.cut_off_workflow, episode.cut_off) == (0, 1, 1)
    assert episode.cut_off_workflow_cost_usd == pytest.approx(2.0)
    summary = _summary(stats)
    assert (summary["agents_cut_off"], summary["agents_cut_off_direct"], summary["agents_cut_off_workflow"]) == (1, 0, 1)
    assert summary["cut_off_workflow_cost_usd"] == pytest.approx(2.0)
    assert summary["cut_off_direct_cost_usd"] == 0
    assert _rows(_table(stats, "limits_stops"))[0]["agents_cut_off"] == 1


def test_a_direct_agent_is_counted_apart_from_a_workflow_agent(tmp_path: Path):
    def agent(name: str, kind: str):
        return _transcript(
            tmp_path,
            name,
            [_reply(f"{name}1", _when(18, 11, 59), input_tokens=1_000_000, output_tokens=0), _limit_line(name, _when(18, 12))],
            kind=kind,
            agent_type="reviewer",
        )

    stats = _stats(agent("direct", "subagent"), agent("flow", "workflow-agent"), rates_lookup=lambda _model: _RATES)

    [episode] = stats.episodes()
    assert (episode.cut_off_direct, episode.cut_off_workflow) == (1, 1)
    assert (episode.cut_off_direct_cost_usd, episode.cut_off_workflow_cost_usd) == (pytest.approx(1.0), pytest.approx(1.0))


def test_an_agent_with_no_replies_or_that_carried_on_is_not_cut_off(tmp_path: Path):
    silent = _transcript(tmp_path, "silent", [_limit_line("l1", _when(18, 12))], kind="workflow-agent")
    carried_on = _transcript(
        tmp_path,
        "carried",
        [_reply("a1", _when(18, 11, 59)), _limit_line("l2", _when(18, 12)), _reply("a2", _when(18, 14, 5))],
        kind="subagent",
        agent_type="reviewer",
    )
    stats = _stats(silent, carried_on, rates_lookup=lambda _model: _RATES)

    [episode] = stats.episodes()
    assert episode.messages == 2 and episode.cut_off == 0
    assert _summary(stats)["agents_cut_off"] == 0


def test_a_cut_off_agent_triggers_the_rule_on_its_own(tmp_path: Path):
    agent = _transcript(
        tmp_path,
        "agent",
        [_reply("a1", _when(18, 11, 59)), _limit_line("l1", _when(18, 12))],
        kind="workflow-agent",
    )

    assert _rule_fires(_stats(agent))


def test_a_limit_line_in_an_agent_puts_its_main_session_among_the_affected(tmp_path: Path):
    agent = _transcript(
        tmp_path,
        "agent",
        [_reply("a1", _when(18, 11, 59)), _limit_line("l1", _when(18, 12))],
        kind="subagent",
        session_id="sess1",
    )
    untouched = _transcript(tmp_path, "untouched", [_reply("m1", _when(18, 12))], session_id="sess2")

    stats = _stats(agent, untouched)
    assert stats.sessions_affected == {"sess1"}
    assert _summary(stats)["sessions_affected"] == 1


def test_the_window_is_the_days_the_corpus_spans_and_never_under_one(tmp_path: Path):
    early = _transcript(tmp_path, "early", [_reply("m1", _when(10, 12))], session_id="sess1")
    day_later = _transcript(tmp_path, "later", [_reply("m2", _when(10, 20))], session_id="sess2")
    ten_days = _transcript(tmp_path, "ten", [_reply("m3", _when(20, 12))], session_id="sess3")
    just_over = _transcript(tmp_path, "over", [_reply("m4", _when(20, 12, 0, 1))], session_id="sess4")

    assert _stats().window_days == 1
    assert _stats(early, day_later).window_days == 1
    assert _stats(early, ten_days).window_days == 10
    assert _stats(early, just_over).window_days == 11
    assert _summary(_stats(early, ten_days))["window_days"] == 10


def test_the_summary_leads_with_the_stops_and_names_the_messages(tmp_path: Path):
    stats = _stats(_transcript(tmp_path, "s", [_reply("m1", _when(18, 12)), _limit_line("l1", _when(18, 12, 0, 5))]))
    section = limits.build_section(stats)
    helptext.annotate_section(section)
    summary = section.tables[0]

    assert [c.key for c in summary.columns[:3]] == ["metric", "five_hour_stops", "weekly_stops"]
    assert summary.lead_columns[:2] == ["five_hour_stops", "weekly_stops"]
    messages = next(c for c in summary.columns if c.key == "limit_hits")
    assert messages.label == "Limit messages"
    assert messages.help == "Usage-limit messages across main sessions and agents."


def test_the_agent_type_table_is_named_for_who_got_the_message(tmp_path: Path):
    stats = _stats(_transcript(tmp_path, "s", [_reply("m1", _when(18, 12)), _limit_line("l1", _when(18, 12, 0, 5))]))
    section = limits.build_section(stats)
    helptext.annotate_section(section)
    table = next(t for t in section.tables if t.name == "limits_by_agent_type")

    assert table.title == "Who got the limit message"
    assert next(c for c in table.columns if c.key == "limit_hits").label == "Limit messages received"
    assert "share_pct" not in [c.key for c in table.columns]


# -- what woke the session between limit lines -----------------------------------


def test_a_gap_holding_only_meta_lines_is_its_own_class(tmp_path: Path):
    lines = [
        _reply("m1", _when(18, 12)),
        _limit_line("l1", _when(18, 12, 0, 5)),
        user_str_line("an automatic note", isMeta=True, timestamp=_iso(_when(18, 12, 1))),
        _limit_line("l2", _when(18, 12, 2)),
        user_str_line("keep going", origin={"kind": "human"}, timestamp=_iso(_when(18, 12, 3))),
        _limit_line("l3", _when(18, 12, 4)),
        _limit_line("l4", _when(18, 12, 5)),
        user_str_line(
            "<task-notification>done</task-notification>",
            origin={"kind": "task-notification"},
            timestamp=_iso(_when(18, 12, 6)),
        ),
        user_str_line("an automatic note", isMeta=True, timestamp=_iso(_when(18, 12, 6, 30))),
        _limit_line("l5", _when(18, 12, 7)),
    ]
    result = _transcript(tmp_path, "s", lines)
    assert [e.kind for e in result.events].count(EventKind.META) == 2

    counts = _stats(result).wake_gap_counts()
    assert counts == {"typed": 1, "resume": 0, "scheduled": 0, "agent_notice": 1, "meta_only": 1, "other": 1}


def test_the_wake_table_counts_gaps_in_main_sessions_only(tmp_path: Path):
    lines = [
        _reply("a1", _when(18, 12)),
        _limit_line("l1", _when(18, 12, 0, 5)),
        user_str_line("an automatic note", isMeta=True, timestamp=_iso(_when(18, 12, 1))),
        _limit_line("l2", _when(18, 12, 2)),
    ]
    agent = _transcript(tmp_path, "agent", lines, kind="subagent")
    main = _transcript(tmp_path, "main", lines, kind="top-level")
    stats = _stats(agent, main)

    table = next(t for t in limits.build_section(stats).tables if t.name == "limits_wake_gaps")
    assert [row[0] for row in table.rows] == list(limits.WAKE_CLASSES)
    assert {row[0]: row[1] for row in table.rows}["meta_only"] == 1
    assert sum(row[1] for row in table.rows) == 1
    assert_privacy(limits.build_section(stats))


def test_wake_class_ranks_what_the_gap_held():
    kinds = EventKind
    assert limits.wake_class({kinds.META, kinds.HUMAN_TEXT, kinds.LIMIT_RESUME}) == "typed"
    assert limits.wake_class({kinds.META, kinds.LIMIT_RESUME, kinds.TASK_NOTIFICATION}) == "resume"
    assert limits.wake_class({kinds.META, kinds.SCHEDULED_TASK, kinds.TASK_NOTIFICATION}) == "scheduled"
    assert limits.wake_class({kinds.META, kinds.AGENT_TERMINATED}) == "agent_notice"
    assert limits.wake_class({kinds.META, kinds.ATTACHMENT}) == "meta_only"
    assert limits.wake_class({kinds.ATTACHMENT, kinds.TOOL_RESULT}) == "other"
    assert limits.wake_class(set()) == "other"


# -- the reset-hour act line -----------------------------------------------------


def test_the_busiest_reset_hour_needs_three_stops_and_a_third_of_them():
    spread = {hour: 1 for hour in range(24)}
    assert limits.busy_reset_hour({}) is None
    assert limits.busy_reset_hour({15: 2}) is None
    assert limits.busy_reset_hour({15: 3}) == 15
    # Three of ten is exactly 30%; three of eleven is under it.
    assert limits.busy_reset_hour({**{h: 1 for h in range(7)}, 15: 3}) == 15
    assert limits.busy_reset_hour({**{h: 1 for h in range(8)}, 15: 3}) is None
    assert limits.busy_reset_hour({**spread, 9: 3}) is None
    # The earlier hour wins a tie.
    assert limits.busy_reset_hour({9: 3, 15: 3}) == 9


def _stops_at(tmp_path: Path, hours: list[int]) -> limits.LimitStats:
    """One five-hour stop per entry, a day apart, each naming a reset at that
    evening hour (13-23) in its text."""
    results = []
    for day, hour in enumerate(hours, start=1):
        text = f"You've hit your session limit \u00b7 resets {hour - 12}pm (Europe/London)"
        lines = [_reply(f"m{day}", _when(day, 12)), _limit_line(f"l{day}", _when(day, 12, 0, 5), _when(day, 14), text=text)]
        results.append(_transcript(tmp_path, f"s{day}", lines, session_id=f"sess{day}"))
    return _stats(*results)


def _annotated(stats: limits.LimitStats):
    section = limits.build_section(stats)
    helptext.annotate_section(section)
    histogram = next(t for t in section.tables if t.name == "limits_reset_hour_histogram")
    return section, histogram


def test_the_reset_hour_advice_shows_only_when_one_hour_is_busy(tmp_path: Path):
    section, histogram = _annotated(_stops_at(tmp_path, [15, 15, 15, 20]))
    assert histogram.help.act and section.help.act

    section, histogram = _annotated(_stops_at(tmp_path, [13, 14, 15, 16]))
    assert histogram.help.act == "" and section.help.act == ""
    # The copy every report shares is left as it was.
    assert helptext.TABLE_COPY["limits_reset_hour_histogram"].help.act
    assert helptext.SECTION_COPY["limits"].help.act
    # No stops at all is no busy hour either.
    section, histogram = _annotated(limits.LimitStats())
    assert histogram.help.act == ""


def test_the_histogram_counts_stops_not_lines(tmp_path: Path):
    lines = [_reply("m1", _when(1, 12))]
    lines += [
        _limit_line(f"l{i}", _when(1, 12, 0, 5 + i), _when(1, 14), text=_SESSION_LIMIT_TEXT) for i in range(5)
    ]
    _section, histogram = _annotated(_stats(_transcript(tmp_path, "s", lines)))

    assert {row[0]: row[1] for row in histogram.rows}["15"] == 1
    assert [c.label for c in histogram.columns][1] == "Limit stops"


# -- the usage-log cross-check ---------------------------------------------------


def test_the_usage_log_cross_check_counts_a_storm_as_one_stop(tmp_path: Path):
    lines = [_reply("m1", _when(18, 12))] + [_limit_line(f"l{i}", _when(18, 12, 0, 5 + i)) for i in range(6)]
    stats = _stats(_transcript(tmp_path, "s", lines))

    table = limits.csv_cross_check([{"window": "five_hour", "used_percentage": 100.0}], stats)
    assert {row[0]: row[1:] for row in table.rows}["five_hour"] == [1, 1, 0]


def test_the_usage_log_is_compared_only_when_it_holds_rate_limit_rows():
    context_only = [{"window": "context_window", "used_percentage": 40.0}] * 3
    assert not limits.has_rate_limit_rows([])
    assert not limits.has_rate_limit_rows(context_only)
    assert limits.has_rate_limit_rows([*context_only, {"window": "five_hour", "used_percentage": 3.0}])
    assert limits.has_rate_limit_rows([{"window": "seven_day", "used_percentage": 3.0}])


def test_signals_cross_check_counts_quota_waits_and_turn_failures(tmp_path: Path):
    from claudeglass import signals

    path = _session_limit_fixture(tmp_path)
    result = parse_transcript(path, TranscriptMeta(path=str(path), session_id="sess1"))
    stats = limits.LimitStats()
    stats.add(result)  # one session_limit hit

    session_signals = {
        "sess1": signals.SessionSignals(waits={"quota": 2, "idle": 1}, failures={"rate_limit": 1, "invalid_request": 3}),
        "sess2": signals.SessionSignals(failures={"overloaded": 1}),
    }
    table = limits.signals_cross_check(session_signals, stats)
    by_signal = {row[0]: row for row in table.rows}
    assert by_signal["quota wait signals (Notification)"][1:] == [2, 1, -1]
    assert by_signal["rate_limit/overloaded turn failures (StopFailure)"][1:] == [2, 1, -1]
    assert "limit messages" in table.notes[0] and "limit stops" not in table.notes[0]
    assert limits.signals_cross_check({}, stats).rows[0][1] == 0


def test_read_usage_log_rows_missing_file_returns_empty(tmp_path: Path):
    assert limits.read_usage_log_rows(tmp_path / "nope.csv") == []


# -- the stops table and its roll-up -------------------------------------------------


def _spend_reply(message_id: str, at: datetime, dollars: int = 1, **extra) -> dict:
    """A reply that costs ``dollars`` at the $1-per-million-tokens rates."""
    return _reply(message_id, at, input_tokens=dollars * 1_000_000, output_tokens=0, **extra)


def _stats_priced(*results) -> limits.LimitStats:
    return _stats(*results, rates_lookup=lambda _model: _RATES)


def _table(stats: limits.LimitStats, name: str, th: limits.LimitThresholds | None = None):
    return next(t for t in limits.build_section(stats, th=th).tables if t.name == name)


def _rows(table) -> list[dict]:
    keys = [column.key for column in table.columns]
    return [dict(zip(keys, row)) for row in table.rows]


def test_a_stop_row_sums_main_agent_and_workflow_spend_in_its_window(tmp_path: Path):
    reset = _when(18, 14)
    main = _transcript(
        tmp_path,
        "main",
        [
            _spend_reply("m0", _when(18, 8, 30), 5),  # before the window opens at 09:00
            _spend_reply("m1", _when(18, 10, 0)),
            _limit_line("l1", _when(18, 12, 0, 5), reset),
            _spend_reply("m2", _when(18, 13, 0), 7),  # after the stop
        ],
    )
    direct = _transcript(
        tmp_path, "direct", [_spend_reply("d1", _when(18, 10, 30), 2)], kind="subagent", agent_type="reviewer"
    )
    workflow = _transcript(
        tmp_path, "flow", [_spend_reply("w1", _when(18, 11, 0), 3)], kind="workflow-agent", agent_type="implementer"
    )

    [row] = _rows(_table(_stats_priced(main, direct, workflow), "limits_stops"))

    assert row["kind"] == "session_limit"
    assert row["reset"] == reset.astimezone().strftime("%Y-%m-%d %H:%M")
    assert row["minutes_before_reset"] == 120
    assert row["spend_usd"] == pytest.approx(6.0)
    assert row["main_share_pct"] == pytest.approx(100 / 6)
    assert row["direct_share_pct"] == pytest.approx(200 / 6)
    assert row["workflow_share_pct"] == pytest.approx(300 / 6)
    assert row["top_spender"] == "implementer, Sonnet (50%)"
    assert row["second_spender"] == "reviewer, Sonnet (33%)"
    assert row["burst_share_pct"] == 0.0
    assert row["stopped_work"] is True


def test_a_stop_whose_lines_name_two_resets_opens_its_window_at_the_earlier_one(tmp_path: Path):
    main = _transcript(
        tmp_path, "main", [_spend_reply("m1", _when(20, 9, 50), dollars=2), _limit_line("l1", _when(20, 10), _when(20, 14))]
    )
    other = _transcript(tmp_path, "other", [_limit_line("l2", _when(20, 13), _when(20, 15, 30))], session_id="sess2")
    stats = _stats_priced(main, other)
    [episode] = stats.episodes()
    assert episode.reset == _when(20, 15, 30) and episode.first_reset == _when(20, 14)
    [row] = _rows(_table(stats, "limits_stops"))
    assert row["spend_usd"] == pytest.approx(2.0)


def test_a_subagent_with_no_type_is_named_as_such_among_the_spenders(tmp_path: Path):
    main = _transcript(tmp_path, "main", [_limit_line("l1", _when(18, 12), _when(18, 14))])
    agent = _transcript(tmp_path, "agent", [_spend_reply("a1", _when(18, 11), dollars=3)], kind="subagent", agent_type=None)
    [row] = _rows(_table(_stats_priced(main, agent), "limits_stops"))
    assert row["top_spender"].startswith("Subagent (type not recorded), ")


def test_a_weekly_stop_spends_over_seven_days_before_its_first_message(tmp_path: Path):
    reset = _when(25, 9)
    main = _transcript(
        tmp_path,
        "main",
        [
            _spend_reply("m0", _when(18, 8, 45), 9),  # before the window opens at 09:00
            _spend_reply("m1", _when(18, 9, 10)),
            _spend_reply("m2", _when(21, 9, 0), 2),
            _limit_line("l1", _when(22, 10, 0, 5), reset, weekly=True),
        ],
    )

    [row] = _rows(_table(_stats_priced(main), "limits_stops"))

    assert row["kind"] == "weekly_limit"
    assert row["spend_usd"] == pytest.approx(3.0)
    assert row["minutes_before_reset"] == round((reset - _when(22, 10, 0, 5)).total_seconds() / 60)


def test_a_stop_with_no_reset_has_no_window_and_no_shares(tmp_path: Path):
    stats = _stats_priced(
        _transcript(tmp_path, "s", [_spend_reply("m1", _when(18, 11)), _limit_line("l1", _when(18, 12), None)])
    )

    [row] = _rows(_table(stats, "limits_stops"))
    assert row["reset"] == "unknown"
    assert row["minutes_before_reset"] is None
    assert row["spend_usd"] is None
    assert (row["main_share_pct"], row["burst_share_pct"], row["top_spender"]) == (None, None, None)
    assert _rows(_table(stats, "limits_stops_rollup"))[0]["stops"] == 0


def test_stops_are_listed_newest_first_and_capped(tmp_path: Path):
    results = []
    for day in range(1, 8):
        lines = [_reply(f"m{day}", _when(day, 11)), _limit_line(f"l{day}", _when(day, 12), _when(day, 14))]
        results.append(_transcript(tmp_path, f"s{day}", lines, session_id=f"sess{day}"))
    table = _table(_stats(*results), "limits_stops")
    assert [row["reset"][:10] for row in _rows(table)] == [f"2026-09-0{day}" for day in range(7, 0, -1)]
    assert not any("Showing" in note for note in table.notes)

    # One stop a day for a month and a half: more than the table holds.
    many = []
    for n in range(limits.STOPS_TABLE_ROWS + 5):
        at = datetime(2026, 8, 1, 11, tzinfo=timezone.utc) + timedelta(days=n)
        lines = [_reply(f"m{n}", at), _limit_line(f"l{n}", at + timedelta(hours=1), at + timedelta(hours=3))]
        many.append(_transcript(tmp_path, f"many{n}", lines, session_id=f"sessmany{n}"))
    stats = _stats(*many)
    table = _table(stats, "limits_stops")
    assert len(table.rows) == limits.STOPS_TABLE_ROWS
    total = len(stats.episodes())
    assert total == limits.STOPS_TABLE_ROWS + 5
    assert f"Showing the {limits.STOPS_TABLE_ROWS} most recent of {total} stops." in table.notes


def _burst_share(tmp_path: Path, agents: int, *, other_main_sessions: int = 0) -> float:
    tmp_path.mkdir(parents=True, exist_ok=True)
    reset = _when(18, 14)
    main = _transcript(
        tmp_path,
        "main",
        [
            _spend_reply("m1", _when(18, 9, 20)),  # before any agent
            _spend_reply("m2", _when(18, 10, 30)),  # while the agents' spans cover it
            _spend_reply("m3", _when(18, 11, 50)),  # after their last reply
            _limit_line("l1", _when(18, 12, 0, 5), reset),
        ],
    )
    others = [
        _transcript(
            tmp_path,
            f"x{i}",
            [_spend_reply(f"x{i}a", _when(18, 10, 0)), _spend_reply(f"x{i}b", _when(18, 11, 0))],
            kind="subagent" if i % 2 else "workflow-agent",
            agent_type="reviewer",
            session_id=f"s{i}",
        )
        for i in range(agents)
    ]
    # A second main session replying over the same hour is no agent.
    sessions = [
        _transcript(
            tmp_path,
            f"y{i}",
            [_spend_reply(f"y{i}a", _when(18, 10, 0)), _spend_reply(f"y{i}b", _when(18, 11, 0))],
            session_id=f"t{i}",
        )
        for i in range(other_main_sessions)
    ]
    [row] = _rows(_table(_stats_priced(main, *others, *sessions), "limits_stops"))
    return row["burst_share_pct"]


def test_three_overlapping_agent_transcripts_count_toward_the_burst_share_and_two_do_not(tmp_path: Path):
    # Each agent spans 10:00 to 11:00 on its two replies, so it is active at 10:30
    # with no reply of its own then. Direct and workflow agents count alike.
    three = _burst_share(tmp_path / "three", 3)
    assert three == pytest.approx(100 * 7 / 9)  # main's 10:30 reply and the agents' six
    assert _burst_share(tmp_path / "two", 2) == 0.0
    # Main sessions never count, however many reply at once.
    assert _burst_share(tmp_path / "mixed", 2, other_main_sessions=3) == 0.0


def test_an_agent_with_one_priced_reply_is_active_for_one_quarter_hour_only(tmp_path: Path):
    reset = _when(18, 14)
    main = _transcript(
        tmp_path,
        "main",
        [
            _spend_reply("m1", _when(18, 10, 5)),
            _spend_reply("m2", _when(18, 10, 50)),
            _limit_line("l1", _when(18, 12), reset),
        ],
    )
    agents = [
        _transcript(
            tmp_path,
            f"a{i}",
            [_spend_reply(f"a{i}", _when(18, 10, 2))],
            kind="subagent",
            agent_type="reviewer",
            session_id=f"s{i}",
        )
        for i in range(3)
    ]

    [row] = _rows(_table(_stats_priced(main, *agents), "limits_stops"))

    # Only the 10:00 quarter hour held three agents: main's 10:05 reply and the agents' three.
    assert row["spend_usd"] == pytest.approx(5.0)
    assert row["burst_share_pct"] == pytest.approx(100 * 4 / 5)


def _stop_on(tmp_path: Path, day: int):
    """A main session stopped at 12:00 on ``day`` (reset 14:00) that came back
    after the reset, with a direct agent the stop cut off."""
    reset = _when(day, 14)
    main = _transcript(
        tmp_path,
        f"main{day}",
        [
            _spend_reply(f"m{day}", _when(day, 11)),
            _limit_line(f"l{day}", _when(day, 12, 0, 5), reset),
            user_str_line("back", origin={"kind": "human"}, timestamp=_iso(_when(day, 15))),
            _reply(
                f"p{day}",
                _when(day, 15, 0, 10),
                input_tokens=0,
                output_tokens=0,
                cache_creation_input_tokens=1_000_000,
                ephemeral_5m_input_tokens=1_000_000,
            ),
        ],
        session_id=f"sess{day}",
    )
    agent = _transcript(
        tmp_path,
        f"agent{day}",
        [_spend_reply(f"a{day}", _when(day, 11, 30), 2), _limit_line(f"la{day}", _when(day, 12, 0, 20), reset)],
        kind="subagent",
        agent_type="reviewer",
        session_id=f"sess{day}",
    )
    return main, agent


def test_a_stop_before_18_september_stays_out_of_the_roll_up_and_the_dollar_figures(tmp_path: Path):
    old_main, old_agent = _stop_on(tmp_path, 10)
    new_main, new_agent = _stop_on(tmp_path, 22)
    stats = _stats_priced(old_main, old_agent, new_main, new_agent)

    # Both stops are listed, newest first.
    assert [row["reset"][:10] for row in _rows(_table(stats, "limits_stops"))] == ["2026-09-22", "2026-09-10"]
    # The roll-up counts the later one only: its main reply and its agent's.
    [rollup] = _rows(_table(stats, "limits_stops_rollup"))
    assert rollup["stops"] == 1
    assert rollup["spend_usd"] == pytest.approx(3.0)
    assert rollup["since"] == "2026-09-18"
    # Counts and tokens cover every run; the two dollar figures, only the later one.
    summary = _summary(stats)
    assert summary["agents_cut_off"] == 2
    assert summary["cut_off_direct_cost_usd"] == pytest.approx(2.0)
    assert summary["limit_turn_cc_tokens"] == 2_000_000
    assert summary["limit_turn_write_cost_usd"] == pytest.approx(1.0)
    by_type = {row[0]: row for row in _table(stats, "limits_by_agent_type").rows}
    assert by_type["top-level"][-1] == pytest.approx(1.0)

    # An empty date counts every run, and a config date moves the line.
    everything = limits.LimitThresholds.from_config({"current_since": ""})
    assert _rows(_table(stats, "limits_stops_rollup", everything))[0]["stops"] == 2
    assert _rows(_table(stats, "limits_summary", everything))[0]["cut_off_direct_cost_usd"] == pytest.approx(4.0)
    earlier = limits.LimitThresholds.from_config({"thresholds": {"current_since": "2026-09-01"}})
    assert _rows(_table(stats, "limits_stops_rollup", earlier))[0]["stops"] == 2
    assert earlier.since == _when(1, 0)
    assert everything.since is None
    assert limits.LimitThresholds().since == _when(18, 0)


def test_the_thresholds_note_names_the_day_the_dollar_figures_start_from():
    assert "18 Sep 2026" in " ".join(limits.LimitThresholds().describe())
    assert "Sep" not in " ".join(limits.LimitThresholds(current_since="").describe())


def test_the_roll_up_counts_each_quarter_hour_once_and_names_the_biggest_spender(tmp_path: Path):
    main = _transcript(
        tmp_path,
        "main",
        [
            _spend_reply("m1", _when(22, 8, 0)),  # inside the weekly window only: left out
            _spend_reply("m2", _when(22, 9, 30)),  # inside the 5-hour window
            _limit_line("l1", _when(22, 10, 0, 5), _when(22, 14)),
            _limit_line("l2", _when(22, 10, 0, 10), _when(25, 9), weekly=True),
        ],
    )
    direct = _transcript(
        tmp_path, "direct", [_spend_reply("d1", _when(22, 9, 45))], kind="subagent", agent_type="reviewer"
    )
    stats = _stats_priced(main, direct)

    # The weekly stop is counted by kind but its week-long window is not added up.
    counted = stats.rollup(limits.LimitThresholds().since)
    assert (counted.stops, counted.five_hour, counted.weekly) == (1, 1, 1)
    [rollup] = _rows(_table(stats, "limits_stops_rollup"))
    assert rollup["stops"] == 1
    assert rollup["spend_usd"] == pytest.approx(2.0)
    assert rollup["main_share_pct"] == pytest.approx(50.0)
    assert rollup["largest_centre"] == "main"
    assert rollup["largest_share_pct"] == pytest.approx(50.0)
    notes = _table(stats, "limits_stops_rollup").notes
    assert notes[0] == (
        "Since 18 Sep 2026 your 5-hour limit stopped you 1 time. "
        "The main session spent the most before them: 50% of list-price spend."
    )
    assert "share of list-price spend; the limit may weigh models differently" in " ".join(notes)
    assert "only when no 5-hour stop counts" in " ".join(notes)


def test_two_5_hour_windows_that_overlap_count_a_shared_quarter_hour_once(tmp_path: Path):
    main = _transcript(
        tmp_path,
        "main",
        [
            _spend_reply("m1", _when(22, 9, 0)),  # inside the first window only
            _limit_line("l1", _when(22, 10, 0, 5), _when(22, 12, 30)),  # window 07:30 to 10:00
            _spend_reply("m2", _when(22, 10, 5)),  # inside both windows
            _spend_reply("m3", _when(22, 10, 30)),  # inside the second window only
            _limit_line("l2", _when(22, 11, 0, 5), _when(22, 15)),  # window 10:00 to 11:00
        ],
    )

    [rollup] = _rows(_table(_stats_priced(main), "limits_stops_rollup"))

    assert rollup["stops"] == 2
    assert rollup["spend_usd"] == pytest.approx(3.0)  # not 4.0: the 10:00 quarter hour counts once


def test_a_weekly_window_counts_in_the_roll_up_only_when_no_five_hour_stop_does(tmp_path: Path):
    def weekly_lines(name: str):
        return _transcript(
            tmp_path / name,
            "main",
            [
                _spend_reply("m1", _when(22, 8, 0)),  # inside the weekly window only
                _spend_reply("m2", _when(22, 9, 30)),
                _limit_line("l2", _when(22, 10, 0, 10), _when(25, 9), weekly=True),
            ],
        )

    (tmp_path / "alone").mkdir()
    alone = _stats_priced(weekly_lines("alone"))
    [rollup] = _rows(_table(alone, "limits_stops_rollup"))
    counted = alone.rollup(limits.LimitThresholds().since)
    assert (counted.stops, counted.five_hour, counted.weekly) == (1, 0, 1)
    assert rollup["stops"] == 1
    assert rollup["spend_usd"] == pytest.approx(2.0)  # the week before the stop
    assert _table(alone, "limits_stops_rollup").notes[0].startswith(
        "Since 18 Sep 2026 your weekly limit stopped you 1 time."
    )

    # With a 5-hour stop too, the weekly window is left out.
    (tmp_path / "both").mkdir()
    both = _transcript(
        tmp_path / "both",
        "main",
        [
            _spend_reply("m1", _when(22, 8, 0)),
            _spend_reply("m2", _when(22, 9, 30)),
            _limit_line("l1", _when(22, 10, 0, 5), _when(22, 14)),
            _limit_line("l2", _when(22, 10, 0, 10), _when(25, 9), weekly=True),
        ],
    )
    [rollup] = _rows(_table(_stats_priced(both), "limits_stops_rollup"))
    assert rollup["stops"] == 1
    assert rollup["spend_usd"] == pytest.approx(1.0)  # the 5-hour window holds m2 only

    # With no date, the sentence opens with a capital.
    everything = limits.LimitThresholds.from_config({"current_since": ""})
    assert _table(alone, "limits_stops_rollup", everything).notes[0].startswith("Your weekly limit stopped you 1 time.")


def test_a_weekly_stop_you_worked_through_is_left_out_of_the_roll_up_but_still_listed(tmp_path: Path):
    main = _transcript(
        tmp_path,
        "main",
        [
            _spend_reply("m1", _when(22, 9, 0)),
            _limit_line("l1", _when(22, 10, 0, 5), _when(25, 9), weekly=True),
            _spend_reply("m2", _when(22, 11, 0)),  # past the ten-minute margin, before the reset
        ],
    )
    stats = _stats_priced(main)

    [row] = _rows(_table(stats, "limits_stops"))
    assert row["stopped_work"] is False
    [rollup] = _rows(_table(stats, "limits_stops_rollup"))
    assert rollup["stops"] == 0 and rollup["spend_usd"] == 0
    assert (rollup["largest_centre"], rollup["main_share_pct"], rollup["burst_share_pct"]) == (None, None, None)
    assert _table(stats, "limits_stops_rollup").notes[0] == (
        "No limit stop since 18 Sep 2026 stopped your work and named a reset."
    )


def test_the_roll_up_sentence_adds_the_burst_only_when_some_spend_ran_in_one(tmp_path: Path):
    quiet = _stats_priced(
        _transcript(
            tmp_path, "s", [_spend_reply("m1", _when(18, 11)), _limit_line("l1", _when(18, 12), _when(18, 14))]
        )
    )
    assert "agents worked at once" not in _table(quiet, "limits_stops_rollup").notes[0]

    reset = _when(18, 14)
    busy = tmp_path / "busy"
    busy.mkdir()
    main = _transcript(
        busy, "main", [_spend_reply("m1", _when(18, 10, 30)), _limit_line("l1", _when(18, 12), reset)]
    )
    agents = [
        _transcript(
            busy,
            f"a{i}",
            [_spend_reply(f"a{i}", _when(18, 10, 0)), _spend_reply(f"b{i}", _when(18, 11, 0))],
            kind="subagent",
            agent_type="reviewer",
            session_id=f"s{i}",
        )
        for i in range(3)
    ]
    note = _table(_stats_priced(main, *agents), "limits_stops_rollup").notes[0]
    assert "100% of that spend ran while 3 or more agents worked at once, against 100% across all your work." in note


def test_the_burst_stands_out_from_ten_points_over_the_overall_share():
    assert limits.BURST_GAP_POINTS == 10.0
    assert limits.burst_stands_out(40.0, 30.0)
    assert not limits.burst_stands_out(39.9, 30.0)
    assert limits.burst_stands_out(10.0, 0.0)
    assert not limits.burst_stands_out(None, 30.0)
    assert not limits.burst_stands_out(40.0, None)
    assert not limits.burst_stands_out(0.0, 0.0)


def test_the_all_work_burst_share_counts_spend_since_the_roll_up_day_only(tmp_path: Path):
    def agents_at(day: int, tag: str):
        return [
            _transcript(
                tmp_path,
                f"{tag}{i}",
                [_spend_reply(f"{tag}{i}a", _when(day, 10, 0)), _spend_reply(f"{tag}{i}b", _when(day, 11, 0))],
                kind="subagent",
                agent_type="reviewer",
                session_id=f"{tag}{i}",
            )
            for i in range(3)
        ]

    since = limits.LimitThresholds().since
    stats = _stats_priced(*agents_at(10, "old"))  # a burst, but before the day the roll-up starts
    assert stats.rollup(since).all_burst_pct is None
    assert stats.rollup(None).all_burst_pct == pytest.approx(100.0)
    stats = _stats_priced(*agents_at(10, "old"), *agents_at(19, "new"))
    assert stats.rollup(since).all_burst_pct == pytest.approx(100.0)


def test_spend_is_left_at_zero_when_no_rate_lookup_is_given(tmp_path: Path):
    reset = _when(18, 14)
    stats = _stats(
        _transcript(tmp_path, "s", [_spend_reply("m1", _when(18, 11)), _limit_line("l1", _when(18, 12), reset)])
    )

    [row] = _rows(_table(stats, "limits_stops"))
    assert row["spend_usd"] == 0
    assert (row["main_share_pct"], row["top_spender"]) == (None, None)


def test_the_new_tables_hold_numbers_and_closed_words_only(tmp_path: Path):
    main, agent = _stop_on(tmp_path, 22)
    section = limits.build_section(_stats_priced(main, agent))
    assert_privacy(section)
    stops = next(t for t in section.tables if t.name == "limits_stops")
    assert [c.key for c in stops.columns][:2] == ["reset", "kind"]
    assert stops.value_labels == {"session_limit": "5-hour", "weekly_limit": "Weekly"}
    helptext.annotate_section(section)
    assert stops.lead_columns[0] == "reset" and len(stops.lead_columns) <= 7
    rollup = next(t for t in section.tables if t.name == "limits_stops_rollup")
    assert len(rollup.lead_columns) <= 4 and "metric" not in rollup.lead_columns


def test_the_post_stop_note_says_the_rewrite_follows_a_stop_that_outlasts_the_cache(tmp_path: Path):
    stats = _stats_priced(*_stop_on(tmp_path, 22))
    notes = " ".join(_table(stats, "limits_summary").notes)
    assert "If a stop outlasts the cache's hour, the first reply after it writes the conversation to the cache again." in notes
    assert "A reply within the hour reads the cache as usual." in notes
    assert "That rewrite is the price of carrying on, not a caching habit to fix." in notes
    assert "always rewrites" not in notes and "can't be avoided" not in notes
    assert "always did a full prefix rewrite" not in " ".join(limits.ASSUMPTIONS)

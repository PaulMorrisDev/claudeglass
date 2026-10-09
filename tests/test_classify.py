"""Tests for WP5: session mode/purpose classification and ``SessionRecord``
construction/grouping (``src/claudeglass/classify.py``).

The three fixture session directories under ``tests/fixtures/classify/``
are hand-built, realistic-shaped JSONL (the same schema
``tests/helpers.py`` produces) parsed through the real
``discovery``/``parse`` pipeline, one per mode this WP's rules must
reach: ``interactive_chat`` (four short human/assistant exchanges, no
subagents), ``long_agentic`` (three spawned subagents, only two human
prompts), and ``overnight`` (five hours of Claude working from 22:00
UTC, with 90 minutes between the two messages typed). ``classify_purpose``'s individual rules are exercised directly
against hand-built ``SessionFeatures`` instead — they're pure functions
of the feature bundle, so there's no need to round-trip through a parser
fixture for each one.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from claudeglass import classify, discovery
from claudeglass.model import Event, EventKind, TranscriptMeta, TranscriptResult, Turn
from claudeglass.parse import parse_transcript

from helpers import turn_line, user_str_line, write_jsonl

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "classify"


def _load_session(project_dir: Path, session_id: str):
    """Parse one fixture session directory the way real code would:
    ``discovery.find_subagents`` + ``parse.parse_transcript`` for the
    top-level file and every subagent transcript.
    """
    top_path = project_dir / f"{session_id}.jsonl"
    top_meta = TranscriptMeta(
        path=str(top_path),
        kind="top-level",
        session_id=session_id,
        project_slug=project_dir.name,
    )
    top = parse_transcript(top_path, top_meta)

    subs = []
    for jsonl_path, _raw_meta in discovery.find_subagents(project_dir, session_id):
        meta_path = jsonl_path.with_name(jsonl_path.stem + ".meta.json")
        sub_meta = discovery.load_meta(meta_path)
        subs.append(parse_transcript(jsonl_path, sub_meta))
    return top, subs


def _tzdata_has(name: str) -> bool:
    try:
        ZoneInfo(name)
        return True
    except ZoneInfoNotFoundError:
        return False


def _stamp(hour: int, minute: int = 0, day: int = 18, sec: int = 0) -> str:
    """An ISO UTC time on 2026-09-``day``; ``hour`` may run past 23 into the next days."""
    at = datetime(2026, 9, day, tzinfo=timezone.utc) + timedelta(hours=hour, minutes=minute, seconds=sec)
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def _run(start: tuple[int, int], end: tuple[int, int], every_min: int = 5, day: int = 18) -> list[str]:
    """Times from ``start`` to ``end`` (hour, minute pairs, ends included)
    ``every_min`` minutes apart: Claude steadily at work."""
    first = datetime.fromisoformat(_stamp(*start, day=day).replace("Z", "+00:00"))
    last = datetime.fromisoformat(_stamp(*end, day=day).replace("Z", "+00:00"))
    out = []
    while first <= last:
        out.append(first.strftime("%Y-%m-%dT%H:%M:%SZ"))
        first += timedelta(minutes=every_min)
    return out


def _built(typed=(), turns=(), extra_events=(), subs=()):
    """A top transcript with a typed line at each of ``typed`` and a reply
    at each of ``turns`` (ISO times), and one subagent transcript per list
    in ``subs``."""
    events = [Event(kind=EventKind.HUMAN_TEXT, ts=ts) for ts in typed] + list(extra_events)
    top = TranscriptResult(turns=[Turn(ts=ts) for ts in turns], events=events)
    return top, [TranscriptResult(turns=[Turn(ts=ts) for ts in sub]) for sub in subs]


class _StepZone(tzinfo):
    """A zone whose clock steps once: ``before`` from the start of time,
    ``after`` from ``cut`` (UTC), for testing a clock change without tzdata."""

    def __init__(self, cut: datetime, before_h: int, after_h: int):
        self.cut = cut
        self.before = timedelta(hours=before_h)
        self.after = timedelta(hours=after_h)

    def utcoffset(self, dt):
        wall = dt.replace(tzinfo=None, fold=0)
        first = (self.cut + self.before).replace(tzinfo=None)
        second = (self.cut + self.after).replace(tzinfo=None)
        if self.after < self.before:  # clocks go back: [second, first) happens twice
            if wall < second:
                return self.before
            if wall >= first:
                return self.after
            return self.after if dt.fold else self.before
        return self.before if wall < first else self.after  # clocks go forward

    def dst(self, dt):
        return timedelta(0)

    def fromutc(self, dt):
        utc = dt.replace(tzinfo=timezone.utc)
        local = (utc + (self.before if utc < self.cut else self.after)).replace(tzinfo=self)
        if self.after < self.before and self.cut <= utc < self.cut + (self.before - self.after):
            local = local.replace(fold=1)
        return local


# --------------------------------------------------------------------
# Fixture-driven mode classification
# --------------------------------------------------------------------


def test_interactive_chat_classifies_as_interactive():
    top, subs = _load_session(FIXTURES / "interactive_chat", "session-interactive-001")
    features = classify.extract_features(top, subs, tz=None)

    assert features.human_prompts == 4
    assert features.subagent_count == 0
    assert features.human_gap_median_s == pytest.approx(120.0)

    mode, evidence = classify.classify_mode(features)
    assert mode == "interactive"
    assert "human_gap_median_s" in evidence
    assert "subagent_count" in evidence


def test_long_agentic_classifies_as_long_agentic():
    top, subs = _load_session(FIXTURES / "long_agentic", "session-long-agentic-001")
    features = classify.extract_features(top, subs, tz=None)

    assert features.subagent_count == 3
    assert features.human_prompts == 2

    mode, evidence = classify.classify_mode(features)
    assert mode == "long-agentic"
    assert "subagent_count" in evidence
    assert "human_prompts" in evidence

    # A reasonable companion signal: three separate top-level turns each
    # spawned exactly one Agent tool call. Fix 8: intent signatures (here,
    # one review marker with only 2 edit turns) now win over the generic
    # agent-fanout bucket, so this fixture classifies as "review" even
    # though it also fanned three calls out to subagents.
    purpose, purpose_evidence = classify.classify_purpose(features)
    assert features.agent_tool_calls == 3
    assert features.review_markers == 1
    assert features.edit_turns == 2
    assert purpose == "review"
    assert "review_markers" in purpose_evidence


def test_overnight_classifies_as_overnight():
    top, subs = _load_session(FIXTURES / "overnight", "session-overnight-001")
    # tz="UTC" so the fixture's UTC hours are the local hours checked,
    # whatever zone the machine running the test is in.
    features = classify.extract_features(top, subs, tz="UTC")

    assert features.span_s == pytest.approx(5 * 3600)
    assert features.human_gap_max_s == pytest.approx(90 * 60)
    assert features.typed_messages == 2
    # Claude worked all five hours (a reply at least every 8 minutes),
    # every minute of it at night and after you had gone: the 90 minutes
    # between your two messages are away time and so is the time after
    # the second.
    assert features.busy_s == pytest.approx(5 * 3600)
    assert features.night_busy_s == pytest.approx(5 * 3600)
    assert features.night_busy_share == pytest.approx(1.0)
    assert features.unattended_night_s == pytest.approx(5 * 3600)

    mode, evidence = classify.classify_mode(features)
    assert mode == "overnight"
    assert set(evidence) == {
        "unattended_night_s",
        "night_busy_s",
        "busy_s",
        "night_busy_share",
        "human_gap_max_s",
    }
    assert evidence["unattended_night_s"] == pytest.approx(5 * 3600)


def test_mode_mixed_fallback_on_hand_built_features():
    f = classify.SessionFeatures(
        span_s=100.0,
        human_gap_median_s=1000.0,
        human_gap_max_s=1000.0,
        subagent_count=5,
        # Above long_agentic_max_human_prompts (10) so this still falls
        # through past the subagent-chain rule into "mixed".
        human_prompts=11,
        typed_messages=11,
        assistant_turns=5,
    )
    mode, evidence = classify.classify_mode(f)
    assert mode == "mixed"
    assert "human_prompts" in evidence


# --------------------------------------------------------------------
# Overnight is Claude working unattended at night
# --------------------------------------------------------------------


def _night_features(**overrides) -> classify.SessionFeatures:
    """Features of a session with ``overrides`` set over two hours of
    night work, all of it unattended and all of the work there is."""
    base = dict(unattended_night_s=7200.0, night_busy_s=7200.0, busy_s=7200.0)
    return classify.SessionFeatures(**{**base, **overrides})


def test_overnight_does_not_fire_without_night_time_evidence():
    """A long span with a long gap between your messages in the middle of
    the afternoon, say a lunch break or an all-afternoon meeting, is not
    overnight: nothing was done at night."""
    f = classify.SessionFeatures(
        span_s=6 * 3600,
        human_gap_max_s=90 * 60,
        human_gap_median_s=90 * 60,
        typed_messages=4,
        busy_s=5 * 3600,
        night_busy_s=0.0,
        unattended_night_s=0.0,
    )
    mode, _ = classify.classify_mode(f)
    assert mode != "overnight"


def test_overnight_needs_two_hours_of_unattended_night_work():
    mode, evidence = classify.classify_mode(_night_features())
    assert mode == "overnight"
    assert evidence["unattended_night_s"] == pytest.approx(7200.0)

    mode, _ = classify.classify_mode(_night_features(unattended_night_s=7199.0))
    assert mode != "overnight"


def test_overnight_does_not_fire_on_night_work_you_watched():
    """Two hours of night work with you at the keyboard is a late
    session, not an overnight run: ``unattended_night_s`` is what counts,
    not ``night_busy_s``."""
    f = _night_features(unattended_night_s=600.0, night_busy_s=7200.0, human_gap_max_s=300.0)
    mode, _ = classify.classify_mode(f)
    assert mode != "overnight"


def test_overnight_needs_thirty_percent_of_the_busy_time_at_night():
    # 2 unattended hours of a much longer working day are under 30% at
    # night: a long day that ran into the evening is not overnight.
    day_long = _night_features(busy_s=7200.0 / 0.3 + 60)
    assert day_long.night_busy_share < 0.3
    mode, _ = classify.classify_mode(day_long)
    assert mode != "overnight"

    exactly = _night_features(busy_s=7200.0 / 0.3)
    mode, evidence = classify.classify_mode(exactly)
    assert mode == "overnight"
    assert evidence["night_busy_share"] == pytest.approx(0.3)


def test_night_busy_share_is_zero_with_no_busy_time():
    assert classify.SessionFeatures().night_busy_share == 0.0


def test_overnight_thresholds_are_overridable():
    f = _night_features(unattended_night_s=3600.0, night_busy_s=3600.0, busy_s=3600.0)
    mode, _ = classify.classify_mode(f)
    assert mode != "overnight"  # one hour < the 2 hour default

    mode, _ = classify.classify_mode(f, thresholds={"overnight_active_s": 3600})
    assert mode == "overnight"

    share = _night_features(busy_s=7200.0 * 5)  # 20% at night
    mode, _ = classify.classify_mode(share)
    assert mode != "overnight"
    mode, _ = classify.classify_mode(share, thresholds={"overnight_night_share": 0.2})
    assert mode == "overnight"


def test_retired_overnight_thresholds_are_read_and_ignored():
    """``overnight_span_s``, ``overnight_gap_s`` and
    ``overnight_night_turn_share`` belonged to the rule this replaced; a
    config that still sets them must keep working, with no effect."""
    retired = {"overnight_span_s": 1, "overnight_gap_s": 1, "overnight_night_turn_share": 0.0}
    assert set(retired) == set(classify.RETIRED_MODE_THRESHOLDS)

    merged = classify._mode_thresholds(retired)
    assert not set(retired) & set(merged)
    assert merged == classify.DEFAULT_MODE_THRESHOLDS

    # The old rule would have called this overnight on any of the three keys.
    f = classify.SessionFeatures(span_s=6 * 3600, human_gap_max_s=90 * 60, typed_messages=4, assistant_turns=4)
    assert classify.classify_mode(f, thresholds=retired) == classify.classify_mode(f)

    top = TranscriptResult(turns=[Turn(ts="2026-09-18T12:00:00Z")])
    assert classify.extract_features(top, [], tz="UTC", thresholds=retired) == classify.extract_features(
        top, [], tz="UTC"
    )
    classify.build_section([], mode_thresholds=retired)


def test_retired_thresholds_come_through_the_config_untouched():
    mode_t, _ = classify.mode_and_purpose_thresholds_from_config(
        {"classify": {"mode": {"overnight_gap_s": 1800, "overnight_active_s": 3600}}}
    )
    # Read as-is; classify_mode and extract_features are what ignore the old key.
    assert mode_t == {"overnight_gap_s": 1800, "overnight_active_s": 3600}


def test_the_new_thresholds_have_defaults():
    t = classify.DEFAULT_MODE_THRESHOLDS
    assert t["overnight_active_s"] == 7200
    assert t["overnight_night_share"] == 0.3
    assert t["activity_idle_s"] == 600
    assert t["dormant_gap_s"] == 4 * 3600
    assert not set(classify.RETIRED_MODE_THRESHOLDS) & set(t)


def test_a_session_with_one_typed_message_and_no_other_rule_is_one_shot():
    f = classify.SessionFeatures(
        span_s=900.0,
        typed_messages=1,
        assistant_turns=5,
        human_prompts=1,
    )
    mode, evidence = classify.classify_mode(f)
    assert mode == "one-shot"
    assert evidence == {"typed_messages": 1, "assistant_turns": 5, "subagent_count": 0}


def test_a_session_with_nothing_typed_is_one_shot_too():
    mode, _ = classify.classify_mode(classify.SessionFeatures(assistant_turns=3))
    assert mode == "one-shot"


@pytest.mark.parametrize("typed", [0, 1])
def test_a_session_claude_never_replied_to_stays_mixed(typed):
    f = classify.SessionFeatures(typed_messages=typed, assistant_turns=0)
    mode, _ = classify.classify_mode(f)
    assert mode == "mixed"


def test_two_typed_messages_with_no_other_rule_stay_mixed():
    f = classify.SessionFeatures(typed_messages=2, human_gap_median_s=2 * 3600.0, assistant_turns=3)
    mode, _ = classify.classify_mode(f)
    assert mode == "mixed"


def test_a_one_shot_session_with_a_chain_is_still_long_agentic():
    mode, _ = classify.classify_mode(classify.SessionFeatures(typed_messages=1, has_chain=True))
    assert mode == "long-agentic"


def test_a_one_shot_session_that_ran_all_night_is_overnight():
    mode, _ = classify.classify_mode(_night_features(typed_messages=1))
    assert mode == "overnight"


def test_multi_day_evidence_added_on_fallthrough_when_active_span_exceeds_24h():
    f = classify.SessionFeatures(
        span_s=30 * 3600,  # > 24h
        human_gap_max_s=90 * 60,
        subagent_count=5,
        # Above long_agentic_max_human_prompts (10) so this still falls
        # through past the subagent-chain rule into "mixed".
        human_prompts=11,
        typed_messages=11,
        assistant_turns=5,
    )
    mode, evidence = classify.classify_mode(f)
    assert mode == "mixed"
    assert evidence["multi_day"] is True


def test_multi_day_evidence_absent_when_span_under_24h():
    f = classify.SessionFeatures(
        span_s=6 * 3600,
        human_gap_max_s=90 * 60,
        subagent_count=5,
        human_prompts=11,
        typed_messages=11,
        assistant_turns=5,
    )
    mode, evidence = classify.classify_mode(f)
    assert mode == "mixed"
    assert "multi_day" not in evidence


def test_multi_day_evidence_not_added_when_overnight_fires():
    f = _night_features(span_s=30 * 3600, human_gap_max_s=90 * 60)
    mode, evidence = classify.classify_mode(f)
    assert mode == "overnight"
    assert "multi_day" not in evidence


def test_multi_day_span_threshold_is_overridable():
    f = classify.SessionFeatures(
        span_s=10 * 3600,
        human_gap_max_s=90 * 60,
        typed_messages=11,
        human_prompts=11,
        subagent_count=5,
        assistant_turns=5,
    )
    mode, evidence = classify.classify_mode(f, thresholds={"multi_day_span_s": 8 * 3600})
    assert mode == "mixed"
    assert evidence["multi_day"] is True


def test_resumed_silences_are_left_out_of_multi_day():
    """A session picked up again on a later day spans over 24 hours but
    was only ever busy for some of it: ``active_span_s`` leaves the
    silences you came back from out."""
    f = classify.SessionFeatures(
        span_s=30 * 3600,
        resumed_s=20 * 3600,
        resumed_gaps=1,
        typed_messages=3,
        human_gap_median_s=120.0,
        human_gap_max_s=600.0,
        assistant_turns=8,
    )
    assert f.active_span_s == pytest.approx(10 * 3600)
    mode, evidence = classify.classify_mode(f)
    assert mode == "interactive"
    assert "multi_day" not in evidence
    assert evidence["resumed_gaps"] == 1


def test_active_span_is_never_negative():
    assert classify.SessionFeatures(span_s=100.0, resumed_s=500.0).active_span_s == 0.0


# --------------------------------------------------------------------
# Usage-limits addition (v3-limits): pause discounting
# --------------------------------------------------------------------


def test_pause_overlap_seconds_sums_overlapping_intervals():
    from datetime import datetime, timezone

    start = datetime(2026, 9, 18, 10, 0, tzinfo=timezone.utc)
    end = datetime(2026, 9, 18, 14, 0, tzinfo=timezone.utc)
    intervals = [
        (datetime(2026, 9, 18, 9, 0, tzinfo=timezone.utc), datetime(2026, 9, 18, 11, 0, tzinfo=timezone.utc)),
        (datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc), datetime(2026, 9, 18, 13, 0, tzinfo=timezone.utc)),
        (datetime(2026, 9, 18, 20, 0, tzinfo=timezone.utc), datetime(2026, 9, 18, 21, 0, tzinfo=timezone.utc)),
    ]
    # Overlaps: [10:00-11:00] = 1h, [12:00-13:00] = 1h, third interval
    # doesn't overlap [10:00, 14:00] at all.
    assert classify._pause_overlap_seconds(start, end, intervals) == pytest.approx(2 * 3600)


def test_pause_overlap_seconds_zero_when_no_overlap():
    from datetime import datetime, timezone

    start = datetime(2026, 9, 18, 10, 0, tzinfo=timezone.utc)
    end = datetime(2026, 9, 18, 11, 0, tzinfo=timezone.utc)
    intervals = [(datetime(2026, 9, 18, 20, 0, tzinfo=timezone.utc), datetime(2026, 9, 18, 21, 0, tzinfo=timezone.utc))]
    assert classify._pause_overlap_seconds(start, end, intervals) == 0.0


def test_median_and_max_gap_discounts_overlapping_pause():
    from datetime import datetime, timezone

    stamps = [
        datetime(2026, 9, 18, 10, 0, tzinfo=timezone.utc),
        datetime(2026, 9, 18, 15, 0, tzinfo=timezone.utc),  # 5h raw gap
    ]
    # A 4-hour usage-cap pause sits entirely inside that gap.
    intervals = [(datetime(2026, 9, 18, 10, 30, tzinfo=timezone.utc), datetime(2026, 9, 18, 14, 30, tzinfo=timezone.utc))]
    median, max_gap = classify._median_and_max_gap(stamps, intervals)
    assert median == pytest.approx(3600.0)  # 5h - 4h
    assert max_gap == pytest.approx(3600.0)


def test_median_and_max_gap_without_pause_intervals_is_unchanged():
    from datetime import datetime, timezone

    stamps = [
        datetime(2026, 9, 18, 10, 0, tzinfo=timezone.utc),
        datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc),
    ]
    median, max_gap = classify._median_and_max_gap(stamps)
    assert median == pytest.approx(2 * 3600)
    assert max_gap == pytest.approx(2 * 3600)


def test_max_gap_pair_names_the_longest_gap_once_pauses_are_taken_out():
    stamps = [
        datetime(2026, 9, 18, 10, 0, tzinfo=timezone.utc),
        datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc),  # 2h after the first
        datetime(2026, 9, 18, 17, 0, tzinfo=timezone.utc),  # 5h after the second
    ]
    # A 4-hour usage-cap pause sits inside the second gap, leaving it 1h.
    pauses = [(datetime(2026, 9, 18, 12, 30, tzinfo=timezone.utc), datetime(2026, 9, 18, 16, 30, tzinfo=timezone.utc))]

    assert classify._max_gap_pair(stamps) == (stamps[1], stamps[2])  # the raw longest
    assert classify._max_gap_pair(stamps, pauses) == (stamps[0], stamps[1])
    # ...which is also the one _median_and_max_gap measures.
    assert classify._median_and_max_gap(stamps, pauses)[1] == pytest.approx(2 * 3600)


def test_max_gap_pair_is_none_with_fewer_than_two_stamps():
    assert classify._max_gap_pair([]) is None
    assert classify._max_gap_pair([datetime(2026, 9, 18, 10, 0, tzinfo=timezone.utc)]) is None


def test_extract_features_limit_pause_s_from_synthetic_fixture(tmp_path: Path):
    lines = [
        turn_line(message_id="msg_1", input_tokens=10, timestamp="2026-09-18T12:00:00.000Z"),
        turn_line(
            message_id="msg_synth",
            model="<synthetic>",
            isApiErrorMessage=True,
            input_tokens=0,
            output_tokens=0,
            # No reset time: the pause is the whole wait, whatever zone this machine is in.
            content=[{"type": "text", "text": "You've hit your session limit"}],
            timestamp="2026-09-18T12:00:05.000Z",
        ),
        turn_line(message_id="msg_2", input_tokens=10, timestamp="2026-09-18T15:00:10.000Z"),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    top = parse_transcript(path, TranscriptMeta(path=str(path), session_id="sess1"))

    features = classify.extract_features(top, [], tz=None)
    # The pause runs from msg_1's own ts (12:00:00) to msg_2's ts
    # (15:00:10), matching Turn.gap_s on the post-pause turn exactly.
    assert features.limit_pause_s == pytest.approx(3 * 3600 + 10)
    assert features.span_s == pytest.approx(3 * 3600 + 10)


def test_extract_features_a_return_three_hours_after_the_reset_takes_only_the_time_up_to_the_reset(tmp_path: Path):
    reset = datetime(2026, 9, 18, 13, 0, tzinfo=timezone.utc)
    lines = [
        user_str_line("start", origin={"kind": "human"}, timestamp="2026-09-18T11:59:50.000Z"),
        turn_line(message_id="msg_1", input_tokens=10, timestamp="2026-09-18T12:00:00.000Z"),
        turn_line(
            message_id="msg_synth",
            model="<synthetic>",
            isApiErrorMessage=True,
            input_tokens=0,
            output_tokens=0,
            content=[{"type": "text", "text": "You've hit your session limit"}],
            timestamp="2026-09-18T12:00:05.000Z",
            quotaLimits={"resetsAt": reset.timestamp()},
        ),
        # Typed at 16:00, three hours after the 13:00 reset.
        user_str_line("back", origin={"kind": "human"}, timestamp="2026-09-18T16:00:00.000Z"),
        turn_line(message_id="msg_2", input_tokens=10, timestamp="2026-09-18T16:00:10.000Z"),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    top = parse_transcript(path, TranscriptMeta(path=str(path), session_id="sess1"))

    features = classify.extract_features(top, [], tz=None)
    # The pause is 12:00:00 to the reset, not on to the reply at 16:00:10.
    assert features.limit_pause_s == pytest.approx(3600)
    # The human gap (11:59:50 to 16:00:00) loses that hour and keeps the three after it.
    assert features.human_gap_max_s == pytest.approx(3 * 3600 + 10)
    assert features.span_s == pytest.approx(4 * 3600 + 10)


def test_a_usage_cap_wait_is_neither_away_time_nor_night_work(tmp_path: Path):
    """Typed at 22:00 and answered for an hour, then the cap hits at 23:00
    with a reset at 03:00; back at 03:20. The five hours between the two
    messages are mostly the wait: an hour of work was unattended at night,
    not the five."""
    reset = datetime(2026, 9, 19, 3, 0, tzinfo=timezone.utc)
    lines = [user_str_line("go", origin={"kind": "human"}, timestamp=_stamp(22))]
    lines += [turn_line(message_id=f"a{i}", timestamp=ts) for i, ts in enumerate(_run((22, 0), (23, 0)))]
    lines.append(
        turn_line(
            message_id="synth",
            model="<synthetic>",
            isApiErrorMessage=True,
            input_tokens=0,
            output_tokens=0,
            content=[{"type": "text", "text": "You've hit your session limit"}],
            timestamp=_stamp(23, 0, sec=5),
            quotaLimits={"resetsAt": reset.timestamp()},
        )
    )
    lines.append(user_str_line("back", origin={"kind": "human"}, timestamp=_stamp(27, 20)))
    lines.append(turn_line(message_id="b", timestamp=_stamp(27, 20, sec=10)))
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    top = parse_transcript(path, TranscriptMeta(path=str(path), session_id="sess1"))

    features = classify.extract_features(top, [], tz="UTC")
    assert features.limit_pause_s == pytest.approx(4 * 3600)
    # 22:00 to 03:20 is 5h20; the 4 hour wait leaves 1h20.
    assert features.human_gap_max_s == pytest.approx(80 * 60)
    assert features.unattended_night_s == pytest.approx(3600)
    assert classify.classify_mode(features)[0] != "overnight"


def test_typed_messages_count_what_you_typed_and_nothing_else():
    top, _ = _built(
        typed=[_stamp(10), _stamp(10, 5)],
        extra_events=[
            Event(kind=EventKind.PLAN_FEEDBACK, ts=_stamp(10, 10)),
            Event(kind=EventKind.QUEUE_OPERATION, ts=_stamp(10, 15), detail={"origin": "human"}),
            # The copy of a message also written as a line, one a tool queued, a command, an interrupt:
            Event(kind=EventKind.QUEUE_OPERATION, ts=_stamp(10, 16), detail={"origin": "human", "dup": True}),
            Event(kind=EventKind.QUEUE_OPERATION, ts=_stamp(10, 17), detail={"origin": "task-notification"}),
            Event(kind=EventKind.SLASH_COMMAND, ts=_stamp(10, 18)),
            Event(kind=EventKind.INTERRUPT, ts=_stamp(10, 19)),
        ],
        turns=_run((10, 0), (10, 20)),
    )
    features = classify.extract_features(top, [], tz="UTC")
    assert features.typed_messages == 4
    # human_prompts is the older count of typed lines alone.
    assert features.human_prompts == 2


def test_replies_ten_minutes_apart_are_one_stretch_of_work_and_more_are_two():
    joined, _ = _built(turns=[_stamp(23, 0), _stamp(23, 10), _stamp(23, 20)])
    assert classify.extract_features(joined, [], tz="UTC").busy_s == pytest.approx(20 * 60)

    split, _ = _built(turns=[_stamp(23, 0), _stamp(23, 10, sec=1), _stamp(23, 30)])
    # 601 seconds apart: neither pair is work, and a lone reply adds nothing.
    assert classify.extract_features(split, [], tz="UTC").busy_s == 0.0


def test_the_idle_gap_that_joins_replies_is_a_threshold():
    top, _ = _built(turns=[_stamp(23, 0), _stamp(23, 20)])
    assert classify.extract_features(top, [], tz="UTC").busy_s == 0.0
    features = classify.extract_features(top, [], tz="UTC", thresholds={"activity_idle_s": 20 * 60})
    assert features.busy_s == pytest.approx(20 * 60)


def test_subagent_replies_fill_the_time_between_the_sessions_own():
    # The main session replies every 40 minutes, a subagent in between.
    top, subs = _built(
        turns=[_stamp(23, 0), _stamp(23, 40), _stamp(24, 20)],
        subs=[_run((23, 5), (23, 35), every_min=10), _run((23, 45), (24, 15), every_min=10)],
    )
    features = classify.extract_features(top, subs, tz="UTC")
    assert features.busy_s == pytest.approx(80 * 60)  # 23:00 to 00:20, joined


def test_a_usage_limit_notice_is_not_work():
    top = TranscriptResult(
        turns=[Turn(ts=_stamp(23, 0)), Turn(ts=_stamp(23, 5), is_synthetic=True), Turn(ts=_stamp(23, 8))]
    )
    assert classify.extract_features(top, [], tz="UTC").busy_s == pytest.approx(8 * 60)


def test_work_you_left_running_at_night_is_overnight():
    top, subs = _built(typed=[_stamp(23, 30)], turns=_run((23, 30), (26, 0)))  # to 02:00, 150 minutes
    features = classify.extract_features(top, subs, tz="UTC")
    assert features.unattended_night_s == pytest.approx(150 * 60)
    assert features.night_busy_share == pytest.approx(1.0)
    assert classify.classify_mode(features)[0] == "overnight"


def test_ninety_minutes_of_unattended_night_work_is_not_overnight_unless_asked():
    top, subs = _built(typed=[_stamp(23, 30)], turns=_run((23, 30), (25, 0)))  # to 01:00
    features = classify.extract_features(top, subs, tz="UTC")
    assert features.unattended_night_s == pytest.approx(90 * 60)
    assert classify.classify_mode(features)[0] != "overnight"
    assert classify.classify_mode(features, thresholds={"overnight_active_s": 3600})[0] == "overnight"


def test_night_work_with_you_there_is_not_unattended():
    # You typed every ten minutes while Claude worked from 22:00 to 00:30.
    typed = _run((22, 0), (24, 30), every_min=10)
    top, subs = _built(typed=typed, turns=_run((22, 0), (24, 30)))
    features = classify.extract_features(top, subs, tz="UTC")
    assert features.night_busy_s == pytest.approx(150 * 60)
    assert features.unattended_night_s == 0.0
    assert classify.classify_mode(features)[0] != "overnight"


def test_a_long_day_that_ran_into_the_night_is_not_overnight():
    # 12:00 to 21:30 with you there, then "go" at 21:30 and Claude on until 01:30:
    # 3h30 of unattended night work, but under 30% of 13h30 busy.
    typed = [*_run((12, 0), (21, 20), every_min=10), _stamp(21, 30)]
    top, subs = _built(typed=typed, turns=_run((12, 0), (25, 30)))
    features = classify.extract_features(top, subs, tz="UTC")
    assert features.unattended_night_s == pytest.approx(210 * 60)
    assert features.busy_s == pytest.approx(810 * 60)
    assert features.night_busy_share == pytest.approx(210 / 810)
    assert classify.classify_mode(features)[0] != "overnight"


def test_a_gap_between_your_messages_over_an_hour_is_away_time():
    # Typed at 21:00 and 23:30, with Claude working from 21:00 to midnight:
    # the 2.5 hours between, and the half hour after, are away.
    top, subs = _built(typed=[_stamp(21), _stamp(23, 30)], turns=_run((21, 0), (24, 0)))
    features = classify.extract_features(top, subs, tz="UTC")
    assert features.unattended_night_s == pytest.approx(2 * 3600)  # 22:00 to 24:00
    assert classify.classify_mode(features)[0] == "overnight"


def test_a_gap_of_exactly_an_hour_is_not_away():
    top, subs = _built(typed=[_stamp(22), _stamp(23)], turns=_run((22, 0), (23, 0)))
    features = classify.extract_features(top, subs, tz="UTC")
    assert features.unattended_night_s == 0.0


def test_with_nothing_typed_all_the_activity_is_away():
    top, subs = _built(turns=_run((22, 0), (24, 30)))
    features = classify.extract_features(top, subs, tz="UTC")
    assert features.typed_messages == 0
    assert features.unattended_night_s == pytest.approx(150 * 60)
    assert classify.classify_mode(features)[0] == "overnight"


def test_the_night_is_the_night_where_you_are(monkeypatch):
    # 13:00 to 15:30 UTC is 22:00 to 00:30 on a clock nine hours ahead.
    top, subs = _built(typed=[_stamp(13)], turns=_run((13, 0), (15, 30)))
    assert classify.extract_features(top, subs, tz="UTC").unattended_night_s == 0.0
    ahead = _StepZone(datetime(2000, 1, 1, tzinfo=timezone.utc), 9, 9)
    monkeypatch.setattr(classify, "_to_local", lambda dt, tz: dt.astimezone(ahead))
    features = classify.extract_features(top, subs, tz="ahead")
    assert features.unattended_night_s == pytest.approx(150 * 60)


def test_a_session_picked_up_the_next_day_is_resumed_not_a_long_gap():
    # An hour of work at 10:00, then the same again at 10:00 the next day.
    first = _run((10, 0), (11, 0))
    second = _run((10, 0), (11, 0), day=19)
    top, subs = _built(typed=[first[0], second[0]], turns=[*first, *second])
    features = classify.extract_features(top, subs, tz="UTC")
    assert features.resumed_gaps == 1
    assert features.resumed_s == pytest.approx(23 * 3600)  # 11:00 to 10:00 the next day
    assert features.span_s == pytest.approx(25 * 3600)
    assert features.active_span_s == pytest.approx(2 * 3600)
    # The 24 hours between your two messages are not the "longest gap" either.
    assert features.human_gap_max_s == pytest.approx(3600)
    mode, evidence = classify.classify_mode(features)
    assert mode != "overnight"
    assert "multi_day" not in evidence
    assert evidence["resumed_gaps"] == 1


def test_a_silence_just_under_four_hours_is_not_resumed():
    top, subs = _built(typed=[_stamp(10), _stamp(14)], turns=[_stamp(10, 0), _stamp(10, 5), _stamp(13, 55), _stamp(14, 0)])
    features = classify.extract_features(top, subs, tz="UTC")
    assert features.resumed_gaps == 0
    assert features.resumed_s == 0.0
    assert features.human_gap_max_s == pytest.approx(4 * 3600)


def test_the_resumed_gap_is_a_threshold():
    top, subs = _built(typed=[_stamp(10), _stamp(14)], turns=[_stamp(10, 0), _stamp(10, 5), _stamp(13, 55), _stamp(14, 0)])
    features = classify.extract_features(top, subs, tz="UTC", thresholds={"dormant_gap_s": 3 * 3600})
    assert features.resumed_gaps == 1


def test_the_hour_a_silence_spent_waiting_for_a_cap_does_not_make_it_resumed(tmp_path: Path):
    # The silence 10:05 to 14:55 is 4h50, but 1h of it was waiting on the cap.
    reset = datetime(2026, 9, 18, 11, 5, tzinfo=timezone.utc)
    lines = [
        user_str_line("go", origin={"kind": "human"}, timestamp=_stamp(10)),
        turn_line(message_id="a", timestamp=_stamp(10, 5)),
        turn_line(
            message_id="synth",
            model="<synthetic>",
            isApiErrorMessage=True,
            input_tokens=0,
            output_tokens=0,
            content=[{"type": "text", "text": "You've hit your session limit"}],
            timestamp=_stamp(10, 6),
            quotaLimits={"resetsAt": reset.timestamp()},
        ),
        user_str_line("again", origin={"kind": "human"}, timestamp=_stamp(14, 55)),
        turn_line(message_id="b", timestamp=_stamp(15, 0)),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    top = parse_transcript(path, TranscriptMeta(path=str(path), session_id="sess1"))
    features = classify.extract_features(top, [], tz="UTC")
    assert features.limit_pause_s == pytest.approx(3600)
    assert features.resumed_gaps == 0  # 4h55 less the hour is 3h55, under the four


def test_naive_timestamps_read_as_utc():
    assert classify._parse_ts("2026-09-18T12:00:00").tzinfo is not None
    assert classify._parse_ts("2026-09-18T12:00:00") == classify._parse_ts("2026-09-18T12:00:00Z")
    assert classify._parse_ts("") is None
    assert classify._parse_ts("not a time") is None


def test_naive_turn_times_with_a_limit_pause_classify_without_error():
    # Times with no zone are read as UTC on both sides, so a pause can be set against the typed times.
    turns = [
        Turn(ts="2026-09-18T10:00:00"),
        Turn(ts="2026-09-18T10:05:00"),
        Turn(ts="2026-09-18T16:00:00", gap_cause="limit", gap_s=5 * 3600 + 55 * 60),
        Turn(ts="2026-09-18T16:05:00"),
    ]
    events = [
        Event(kind=EventKind.HUMAN_TEXT, ts="2026-09-18T09:59:00"),
        Event(kind=EventKind.HUMAN_TEXT, ts="2026-09-18T15:59:00"),
    ]
    features = classify.extract_features(TranscriptResult(turns=turns, events=events), [], tz="UTC")
    assert features.human_gap_max_s == pytest.approx(360.0)
    assert features.limit_pause_s == pytest.approx(21300.0)


def test_interval_helpers_merge_intersect_and_subtract():
    def at(h, m=0):
        return datetime(2026, 9, 18, tzinfo=timezone.utc) + timedelta(hours=h, minutes=m)

    assert classify._merged([(at(3), at(4)), (at(1), at(2)), (at(2), at(2, 30)), (at(3, 30), at(5))]) == [
        (at(1), at(2, 30)),
        (at(3), at(5)),
    ]
    assert classify._intersection([(at(1), at(4))], [(at(0), at(2)), (at(3), at(6))]) == [(at(1), at(2)), (at(3), at(4))]
    assert classify._intersection([(at(1), at(2))], [(at(2), at(3))]) == []
    assert classify._without([(at(1), at(6))], [(at(2), at(3)), (at(5), at(7))]) == [(at(1), at(2)), (at(3), at(5))]
    assert classify._without([(at(1), at(2))], [(at(0), at(3))]) == []
    assert classify._seconds([(at(1), at(2)), (at(3), at(3, 30))]) == pytest.approx(5400)


def test_away_time_leaves_out_the_cap_pauses():
    def at(h, m=0):
        return datetime(2026, 9, 18, tzinfo=timezone.utc) + timedelta(hours=h, minutes=m)

    # 21:00 to 02:30 with a wait from 21:45 to 02:00: the work before and the half hour after are away.
    away = classify._away_intervals(
        [at(21), at(26, 30)], [at(21), at(21, 45), at(26, 30)], [(at(21, 45), at(26))], 3600
    )
    assert away == [(at(21), at(21, 45)), (at(26), at(26, 30))]
    # The same gap with a longer wait leaves under an hour: nobody was away.
    away = classify._away_intervals([at(22), at(23, 30)], [at(22), at(23, 30)], [(at(22, 20), at(23, 10))], 3600)
    assert away == []


def test_night_seconds_follow_a_clock_that_goes_back(monkeypatch):
    # US Eastern, 1 Nov 2026: 02:00 EDT is 01:00 EST, so the night 22:00 to 07:00 is ten hours long.
    zone = _StepZone(datetime(2026, 11, 1, 6, tzinfo=timezone.utc), -4, -5)
    span = [(datetime(2026, 11, 1, 0, tzinfo=timezone.utc), datetime(2026, 11, 1, 14, tzinfo=timezone.utc))]
    monkeypatch.setattr(classify, "_to_local", lambda dt, tz: dt.astimezone(zone))
    assert classify._night_seconds(span, "x", 22, 7) == pytest.approx(10 * 3600)


def test_night_seconds_follow_a_clock_that_goes_forward(monkeypatch):
    # US Eastern, 8 Mar 2026: 02:00 EST is 03:00 EDT, so the night is eight hours long.
    zone = _StepZone(datetime(2026, 3, 8, 7, tzinfo=timezone.utc), -5, -4)
    span = [(datetime(2026, 3, 8, 0, tzinfo=timezone.utc), datetime(2026, 3, 8, 14, tzinfo=timezone.utc))]
    monkeypatch.setattr(classify, "_to_local", lambda dt, tz: dt.astimezone(zone))
    assert classify._night_seconds(span, "x", 22, 7) == pytest.approx(8 * 3600)


def test_night_seconds_follow_a_clock_change_through_a_fixed_offset(monkeypatch):
    # The machine's zone with no tzdata: astimezone() gives the offset in force at each moment.
    # London, 29 Mar 2026: 01:00 GMT is 02:00 BST, so the night 22:00 to 07:00 is eight hours long.
    change = datetime(2026, 3, 29, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(
        classify, "_to_local", lambda dt, tz: dt.astimezone(timezone(timedelta(hours=1 if dt >= change else 0)))
    )
    span = [(datetime(2026, 3, 28, 20, tzinfo=timezone.utc), datetime(2026, 3, 29, 10, tzinfo=timezone.utc))]
    assert classify._night_seconds(span, None, 22, 7) == pytest.approx(8 * 3600)


def test_night_seconds_count_only_the_night_part_of_an_interval():
    span = [(datetime(2026, 9, 18, 21, tzinfo=timezone.utc), datetime(2026, 9, 18, 23, 30, tzinfo=timezone.utc))]
    assert classify._night_seconds(span, "UTC", 22, 7) == pytest.approx(90 * 60)
    # A different window moves it.
    assert classify._night_seconds(span, "UTC", 23, 6) == pytest.approx(30 * 60)
    # An interval over a whole day takes the night at each end.
    day = [(datetime(2026, 9, 18, 0, tzinfo=timezone.utc), datetime(2026, 9, 19, 0, tzinfo=timezone.utc))]
    assert classify._night_seconds(day, "UTC", 22, 7) == pytest.approx((7 + 2) * 3600)


def test_night_window_wraps_midnight():
    assert classify._in_night_window(23, 22, 7) is True
    assert classify._in_night_window(3, 22, 7) is True
    assert classify._in_night_window(21, 22, 7) is False
    assert classify._in_night_window(7, 22, 7) is False
    assert classify._in_night_window(22, 22, 7) is True


def test_gap_overlaps_night_detects_overlap_even_when_endpoints_are_daytime():
    from datetime import datetime, timezone

    # A gap from 18:00 to 09:00 the next day plainly spans the night even
    # though neither endpoint's own hour is inside the 22:00-07:00
    # window -- the interval-overlap check must catch this, not just an
    # endpoint-hour check. tz="UTC" is passed explicitly so the fixed UTC
    # inputs below are also the "local" hours being checked, independent
    # of the machine running the test ("UTC" resolves without tzdata --
    # see discovery._zone).
    start = datetime(2026, 9, 17, 18, 0, tzinfo=timezone.utc)
    end = datetime(2026, 9, 18, 9, 0, tzinfo=timezone.utc)
    assert classify._gap_overlaps_night(start, end, "UTC", 22, 7) is True


def test_utc_resolves_without_tzdata_whatever_the_machine_zone():
    from datetime import datetime, timedelta, timezone

    from claudeglass import exports, monthly, usage

    # "UTC" is the one name a bare Windows install (no tzdata) can't look
    # up through zoneinfo: each module's _to_local must still honour it,
    # not fall back to the machine's own zone.
    dt = datetime(2026, 9, 17, 23, 30, tzinfo=timezone.utc)
    for module in (classify, usage, exports, monthly):
        assert module._to_local(dt, "UTC").utcoffset() == timedelta(0), module.__name__


def test_gap_overlaps_night_false_for_a_purely_daytime_gap():
    from datetime import datetime, timezone

    start = datetime(2026, 9, 17, 9, 0, tzinfo=timezone.utc)
    end = datetime(2026, 9, 17, 17, 0, tzinfo=timezone.utc)
    assert classify._gap_overlaps_night(start, end, "UTC", 22, 7) is False


# --------------------------------------------------------------------
# Fix 5: Config.thresholds["classify"] sub-dict
# --------------------------------------------------------------------


def test_mode_and_purpose_thresholds_from_config_reads_classify_subdict():
    config_thresholds = {
        "classify": {
            "mode": {"overnight_active_s": 3600},
            "purpose": {"local_llm_min_hits": 1},
        },
        "some_other_package": {"unrelated": True},
    }
    mode_t, purpose_t = classify.mode_and_purpose_thresholds_from_config(config_thresholds)
    assert mode_t == {"overnight_active_s": 3600}
    assert purpose_t == {"local_llm_min_hits": 1}


def test_mode_and_purpose_thresholds_from_config_empty_when_absent():
    assert classify.mode_and_purpose_thresholds_from_config({}) == ({}, {})
    assert classify.mode_and_purpose_thresholds_from_config({"classify": "not-a-dict"}) == ({}, {})
    assert classify.mode_and_purpose_thresholds_from_config(
        {"classify": {"mode": "not-a-dict"}}
    ) == ({}, {})


# --------------------------------------------------------------------
# classify_session: rules vs. override
# --------------------------------------------------------------------


def test_classify_session_uses_rules_by_default():
    top, subs = _load_session(FIXTURES / "interactive_chat", "session-interactive-001")
    classification = classify.classify_session(top, subs, overrides={}, tz=None)
    assert classification.mode == "interactive"
    assert classification.mode_source == "rule"
    assert classification.purpose_source == "rule"


def test_classify_session_override_wins_for_both_fields():
    top, subs = _load_session(FIXTURES / "interactive_chat", "session-interactive-001")
    overrides = {"session-interactive-001": {"mode": "overnight", "purpose": "planning"}}
    classification = classify.classify_session(top, subs, overrides=overrides, tz=None)
    assert classification.mode == "overnight"
    assert classification.mode_source == "override"
    assert classification.purpose == "planning"
    assert classification.purpose_source == "override"


def test_classify_session_override_can_set_just_one_field():
    top, subs = _load_session(FIXTURES / "interactive_chat", "session-interactive-001")
    overrides = {"session-interactive-001": {"mode": "mixed"}}
    classification = classify.classify_session(top, subs, overrides=overrides, tz=None)
    assert classification.mode == "mixed"
    assert classification.mode_source == "override"
    # purpose has no override entry, so it still runs through the rules.
    assert classification.purpose_source == "rule"


def test_classify_session_ignores_overrides_for_other_sessions():
    top, subs = _load_session(FIXTURES / "interactive_chat", "session-interactive-001")
    overrides = {"some-other-session": {"mode": "overnight"}}
    classification = classify.classify_session(top, subs, overrides=overrides, tz=None)
    assert classification.mode == "interactive"
    assert classification.mode_source == "rule"


# --------------------------------------------------------------------
# classify_purpose: each rule hit on a hand-built SessionFeatures
# --------------------------------------------------------------------


def test_purpose_local_llm_pipeline():
    f = classify.SessionFeatures(local_llm_hits=3)
    purpose, evidence = classify.classify_purpose(f)
    assert purpose == "local-llm-pipeline"
    assert evidence["local_llm_hits"] == 3


def test_purpose_workflow_run_via_workflow_count():
    f = classify.SessionFeatures(workflows=1)
    purpose, _ = classify.classify_purpose(f)
    assert purpose == "workflow-run"


def test_purpose_workflow_run_via_workflow_tool_calls():
    f = classify.SessionFeatures(workflow_tool_calls=1)
    purpose, _ = classify.classify_purpose(f)
    assert purpose == "workflow-run"


def test_purpose_agent_fanout():
    f = classify.SessionFeatures(agent_tool_calls=3)
    purpose, evidence = classify.classify_purpose(f)
    assert purpose == "agent-fanout"
    assert evidence["agent_tool_calls"] == 3


def test_purpose_agent_fanout_loses_to_an_intent_signature():
    """Fix 8: agent-fanout was tested third, ahead of every intent-signature
    rule, so heavy fan-out plus a review marker always won as
    agent-fanout. It's now tested last (just above general-dev), so the
    more specific "review" signature wins instead."""
    f = classify.SessionFeatures(agent_tool_calls=3, review_markers=1, edit_turns=0)
    purpose, evidence = classify.classify_purpose(f)
    assert purpose == "review"
    assert evidence["review_markers"] == 1


def test_purpose_agent_fanout_still_fires_without_any_intent_signature():
    f = classify.SessionFeatures(agent_tool_calls=5, edit_turns=8, test_tool_hits=0)
    purpose, evidence = classify.classify_purpose(f)
    assert purpose == "agent-fanout"
    assert evidence["agent_tool_calls"] == 5


def test_purpose_review():
    f = classify.SessionFeatures(review_markers=1, edit_turns=0)
    purpose, evidence = classify.classify_purpose(f)
    assert purpose == "review"
    assert evidence["review_markers"] == 1


def test_purpose_review_tolerates_up_to_two_edit_turns():
    """Fix 8: review used to require edit_turns == 0 exactly; a review
    pass that also lands one or two small fixes now still counts."""
    f = classify.SessionFeatures(review_markers=1, edit_turns=2)
    purpose, evidence = classify.classify_purpose(f)
    assert purpose == "review"
    assert evidence["edit_turns"] == 2


def test_purpose_review_still_excludes_three_or_more_edit_turns():
    f = classify.SessionFeatures(review_markers=1, edit_turns=3)
    purpose, _ = classify.classify_purpose(f)
    assert purpose != "review"


def test_purpose_test_triage():
    f = classify.SessionFeatures(test_tool_hits=3, edit_turns=1)
    purpose, evidence = classify.classify_purpose(f)
    assert purpose == "test-triage"
    assert evidence["test_tool_hits"] == 3


def test_purpose_test_triage_drops_edit_turns_comparison_at_five_hits():
    """Fix 8: test_tool_hits >= edit_turns used to be required
    unconditionally; once test_tool_hits alone reaches 5 that comparison
    is dropped, so a heavily-edited session with plenty of test activity
    still classifies as test-triage."""
    f = classify.SessionFeatures(test_tool_hits=5, edit_turns=10)
    purpose, evidence = classify.classify_purpose(f)
    assert purpose == "test-triage"
    assert evidence["test_tool_hits"] == 5


def test_purpose_test_triage_still_requires_hits_ge_edit_turns_below_five():
    f = classify.SessionFeatures(test_tool_hits=4, edit_turns=5)
    purpose, _ = classify.classify_purpose(f)
    assert purpose != "test-triage"


def test_purpose_planning():
    f = classify.SessionFeatures(plan_mode_events=1, edit_turns=2)
    purpose, evidence = classify.classify_purpose(f)
    assert purpose == "planning"
    assert evidence["plan_mode_events"] == 1


def test_purpose_docs_or_light_edit():
    f = classify.SessionFeatures(assistant_turns=5, edit_turns=3, read_turns=2, test_tool_hits=0)
    purpose, evidence = classify.classify_purpose(f)
    assert purpose == "docs-or-light-edit"
    assert evidence["edit_turns"] == 3


def test_purpose_refactor():
    f = classify.SessionFeatures(edit_turns=10, test_tool_hits=1)
    purpose, evidence = classify.classify_purpose(f)
    assert purpose == "refactor"
    assert evidence["edit_turns"] == 10


def test_purpose_general_dev_fallback():
    f = classify.SessionFeatures()
    purpose, _ = classify.classify_purpose(f)
    assert purpose == "general-dev"


def test_purpose_thresholds_are_overridable():
    f = classify.SessionFeatures(local_llm_hits=2)
    # Default threshold is 3 - two hits doesn't qualify.
    assert classify.classify_purpose(f)[0] != "local-llm-pipeline"
    purpose, _ = classify.classify_purpose(f, thresholds={"local_llm_min_hits": 2})
    assert purpose == "local-llm-pipeline"


# --------------------------------------------------------------------
# Timezone conversion (start_local_hour / end_local_hour)
# --------------------------------------------------------------------


def test_local_hour_unknown_zone_falls_back_to_system_local():
    ts = "2026-09-18T12:00:00.000Z"
    assert classify._local_hour(ts, "Definitely/Not-A-Real-Zone") == classify._local_hour(ts, None)


def test_extract_features_converts_start_local_hour_to_named_timezone():
    top, subs = _load_session(FIXTURES / "interactive_chat", "session-interactive-001")
    # First top-level turn timestamp is 2026-09-18T12:00:05Z.
    local_features = classify.extract_features(top, subs, tz=None)
    named_features = classify.extract_features(top, subs, tz="America/New_York")

    assert named_features.start_local_hour is not None
    if _tzdata_has("America/New_York"):
        # EDT is UTC-4 in September.
        assert named_features.start_local_hour == 8
    else:
        # No system/tzdata zoneinfo source available on this machine
        # (e.g. a bare Windows install without the tzdata package) -
        # extract_features must degrade gracefully to the same value
        # tz=None produces, never raise.
        assert named_features.start_local_hour == local_features.start_local_hour


# --------------------------------------------------------------------
# build_session_record / group_sessions / build_section
# --------------------------------------------------------------------

#: Same shape-based leak scan ``tests/helpers.py``'s ``assert_privacy``
#: applies to a ``TranscriptResult``'s dataclass fields, applied here to
#: raw ``Table.rows`` cell values instead (``assert_privacy`` only
#: recurses into dataclass instances nested in a list/tuple, and a
#: ``Table.rows`` entry is a plain list of scalars, not a dataclass, so
#: it needs its own walk).
_PRIVACY_DRIVE_RE = re.compile(r"[A-Za-z]:\\")
_PRIVACY_POSIX_HOME_RE = re.compile(r"/home/")
_PRIVACY_WIN_USERS_RE = re.compile(r"\\Users\\")
_PRIVACY_AT_RE = re.compile(r"@")


def _assert_table_rows_privacy(tables) -> None:
    violations: list[str] = []
    for table in tables:
        for row_index, row in enumerate(table.rows):
            for col_index, cell in enumerate(row):
                if not isinstance(cell, str):
                    continue
                where = f"{table.name}.rows[{row_index}][{col_index}]"
                if _PRIVACY_DRIVE_RE.search(cell):
                    violations.append(f"{where} matches a Windows drive path: {cell!r}")
                if _PRIVACY_POSIX_HOME_RE.search(cell):
                    violations.append(f"{where} matches a POSIX /home/ path: {cell!r}")
                if _PRIVACY_WIN_USERS_RE.search(cell):
                    violations.append(f"{where} matches a \\Users\\ path: {cell!r}")
                if _PRIVACY_AT_RE.search(cell):
                    violations.append(f"{where} contains '@': {cell!r}")
    assert violations == [], violations


def _build_all_records():
    records = []
    for project_name, session_id in (
        ("interactive_chat", "session-interactive-001"),
        ("long_agentic", "session-long-agentic-001"),
        ("overnight", "session-overnight-001"),
    ):
        project_dir = FIXTURES / project_name
        top, subs = _load_session(project_dir, session_id)
        # The fixtures' hours are UTC; the overnight one is night only there.
        classification = classify.classify_session(top, subs, overrides={}, tz="UTC")
        record = classify.build_session_record(
            top, subs, workflows=[], classification=classification, slug=project_dir.name
        )
        records.append(record)
    return records


def test_build_session_record_populates_span_and_timestamps():
    top, subs = _load_session(FIXTURES / "overnight", "session-overnight-001")
    classification = classify.classify_session(top, subs, overrides={}, tz=None)
    record = classify.build_session_record(
        top, subs, workflows=[], classification=classification, slug="overnight"
    )
    assert record.session_id == "session-overnight-001"
    assert record.slug == "overnight"
    assert record.first_ts == "2026-09-17T22:00:05.000Z"
    assert record.last_ts == "2026-09-18T03:00:05.000Z"
    assert record.span_s == pytest.approx(5 * 3600)
    assert record.classification is classification
    assert record.archetype is None
    assert record.snapshot_id is None
    assert record.profile_id is None
    assert record.entrypoint is None  # fixture carries no entrypoint field


def test_group_sessions_by_mode_and_project():
    records = _build_all_records()

    by_mode = classify.group_sessions(records, "mode")
    assert set(by_mode) == {"interactive", "long-agentic", "overnight"}
    assert all(len(group) == 1 for group in by_mode.values())

    by_project = classify.group_sessions(records, "project")
    assert set(by_project) == {"interactive_chat", "long_agentic", "overnight"}


def test_group_sessions_entrypoint_falls_back_to_unknown_when_absent():
    records = _build_all_records()
    by_entrypoint = classify.group_sessions(records, "entrypoint")
    # None of these fixtures' raw lines carry an entrypoint field, so
    # every session falls back to "unknown" (batch C: SessionRecord.entrypoint
    # is None, not a missing feature — see test_group_sessions_by_entrypoint
    # below for the populated case).
    assert set(by_entrypoint) == {"unknown"}
    assert len(by_entrypoint["unknown"]) == len(records)


def _build_session_record_with_entrypoint(session_dir: Path, entrypoint: str | None):
    session_dir.mkdir(parents=True, exist_ok=True)
    lines = [turn_line(message_id="msg_1", input_tokens=100, output_tokens=10)]
    if entrypoint is not None:
        lines[0]["entrypoint"] = entrypoint
    top_path = session_dir / "session.jsonl"
    write_jsonl(top_path, lines)
    top = parse_transcript(top_path, TranscriptMeta(path=str(top_path), session_id=top_path.stem))
    classification = classify.classify_session(top, [], overrides={}, tz=None)
    return classify.build_session_record(top, [], workflows=[], classification=classification, slug="proj")


def test_build_session_record_fills_entrypoint_from_top_meta(tmp_path: Path):
    record = _build_session_record_with_entrypoint(tmp_path, "sdk-python")
    assert record.entrypoint == "sdk-python"


def test_group_sessions_by_entrypoint(tmp_path: Path):
    cli_record = _build_session_record_with_entrypoint(tmp_path / "a", "cli")
    sdk_record = _build_session_record_with_entrypoint(tmp_path / "b", "sdk-python")
    unknown_record = _build_session_record_with_entrypoint(tmp_path / "c", None)

    by_entrypoint = classify.group_sessions([cli_record, sdk_record, unknown_record], "entrypoint")
    assert set(by_entrypoint) == {"cli", "sdk-python", "unknown"}
    assert by_entrypoint["cli"] == [cli_record]
    assert by_entrypoint["sdk-python"] == [sdk_record]
    assert by_entrypoint["unknown"] == [unknown_record]


def test_group_sessions_rejects_unknown_key():
    records = _build_all_records()
    with pytest.raises(ValueError):
        classify.group_sessions(records, "not-a-real-key")


def test_build_section_produces_expected_tables_and_is_privacy_clean():
    records = _build_all_records()
    section = classify.build_section(records)

    assert section.key == "sessions"
    assert section.title == "Sessions"
    table_names = {table.name for table in section.tables}
    assert table_names == {"sessions_by_mode", "sessions_by_purpose", "sessions_detail"}

    detail = next(t for t in section.tables if t.name == "sessions_detail")
    assert len(detail.rows) == 3

    by_mode = next(t for t in section.tables if t.name == "sessions_by_mode")
    assert sum(row[1] for row in by_mode.rows) == 3  # sessions column sums to 3

    _assert_table_rows_privacy(section.tables)


def test_build_section_caps_detail_rows_at_fifty_with_a_note():
    records = _build_all_records()
    # Duplicate the same three records many times over (distinct only in
    # identity, which is all group_sessions/build_section look at) to
    # exceed the 50-row cap without needing 50 real fixture directories.
    many = records * 20
    section = classify.build_section(many)
    detail = next(t for t in section.tables if t.name == "sessions_detail")
    assert len(detail.rows) == 50
    assert detail.notes
    assert "50" in detail.notes[0]


def test_build_section_notes_report_default_overnight_window():
    records = _build_all_records()
    section = classify.build_section(records)
    assert any("22:00" in note and "07:00" in note for note in section.notes)


def test_build_section_notes_report_configured_overnight_window():
    records = _build_all_records()
    section = classify.build_section(
        records,
        mode_thresholds={"overnight_night_start_hour": 23, "overnight_night_end_hour": 6},
    )
    assert any("23:00" in note and "06:00" in note for note in section.notes)


def test_build_section_note_states_the_night_work_overnight_asks_for():
    records = _build_all_records()
    note = next(n for n in classify.build_section(records).notes if "Overnight window" in n)
    assert "at least 2 hours" in note and "while you were away" in note

    section = classify.build_section(records, mode_thresholds={"overnight_active_s": 5400})
    assert any("at least 90 minutes" in n for n in section.notes)
    section = classify.build_section(records, mode_thresholds={"overnight_active_s": 3600})
    assert any("at least 1 hour " in n for n in section.notes)


def test_the_overnight_fixture_session_is_the_only_overnight_one_in_the_section():
    records = _build_all_records()
    section = classify.build_section(records)
    table = next(t for t in section.tables if t.name == "sessions_by_mode")
    assert {row[0]: row[1] for row in table.rows}["overnight"] == 1


def test_duration_names_whole_hours_and_otherwise_minutes():
    assert classify._duration(7200) == "2 hours"
    assert classify._duration(3600) == "1 hour"
    assert classify._duration(5400) == "90 minutes"
    assert classify._duration(60) == "1 minute"


# --------------------------------------------------------------------
# classify_session: the kind of task metrics capture reported
# --------------------------------------------------------------------


def _tagged_top(tmp_path, *tasks, session_id="s-tagged"):
    from helpers import attachment_line, user_str_line

    # SEC-P2: a `[cg: ...]` tag only counts once a capture note has been
    # seen and the metric it answers was requested -- "task" here.
    note_text = "ClaudeGlass metrics capture (cg-cap v1 task): ..."
    note = attachment_line(
        "hook_additional_context",
        rendered=f"<system-reminder>\nSessionStart hook additional context: {note_text}\n</system-reminder>",
        content=[note_text], hookName="SessionStart", hookEvent="SessionStart", toolUseID="SessionStart",
    )
    note["timestamp"] = "2026-09-18T11:59:59.000Z"
    lines = [note]
    for n, task in enumerate(tasks):
        ts = f"2026-09-18T12:00:{2 * n:02d}.000Z"
        lines.append(user_str_line("go on", origin={"kind": "human"}, timestamp=ts))
        tag = f"[cg: task={task}]" if task else "no tag"
        lines.append(turn_line(content=[{"type": "text", "text": f"Done.\n{tag}"}],
                               timestamp=f"2026-09-18T12:00:{2 * n + 1:02d}.000Z"))
    path = tmp_path / f"{session_id}.jsonl"
    write_jsonl(path, lines)
    return parse_transcript(path, TranscriptMeta(path=str(path), kind="top-level", session_id=session_id))


@pytest.mark.parametrize(
    "tasks, expected",
    [
        (("review", "review", "bugfix"), ("review", 3)),
        (("review",), (None, 1)),
        (("review", "bugfix", "feature"), (None, 3)),
        (("docs", "docs", None), ("docs", 2)),
    ],
)
def test_reported_task_needs_two_tags_and_half_of_them(tmp_path, tasks, expected):
    assert classify.reported_task(_tagged_top(tmp_path, *tasks)) == expected
    assert classify.reported_task(None) == (None, 0)


def test_a_reported_task_that_maps_one_to_one_decides_the_purpose(tmp_path):
    top = _tagged_top(tmp_path, "review", "review", "review")
    classification = classify.classify_session(top, [], overrides={}, tz=None)
    assert classification.purpose == "review" and classification.purpose_source == "reported"
    assert classification.purpose_evidence == {"reported_task": "review", "tagged_messages": 3}


def test_a_reported_task_spanning_several_purposes_leaves_the_rules_to_decide(tmp_path):
    top = _tagged_top(tmp_path, "bugfix", "bugfix")
    assert "bugfix" not in classify.REPORTED_PURPOSES
    assert classify.classify_session(top, [], overrides={}, tz=None).purpose_source == "rule"


def test_an_override_still_wins_over_the_reported_task(tmp_path):
    top = _tagged_top(tmp_path, "review", "review")
    overrides = {"s-tagged": {"purpose": "planning"}}
    classification = classify.classify_session(top, [], overrides=overrides, tz=None)
    assert (classification.purpose, classification.purpose_source) == ("planning", "override")


# -- the label chip: a catch-all a rule fell back to -------------------------------------


@pytest.mark.parametrize(
    ("mode", "mode_source", "purpose", "purpose_source", "expected"),
    [
        # The catch-all of either label, reached by a rule, is a guess.
        ("mixed", "rule", "refactor", "intent-signature", True),
        ("agentic", "tool-signature", "general-dev", "rule", True),
        ("mixed", "rule", "general-dev", "rule", True),
        # A specific label is not one, whatever its source.
        ("agentic", "tool-signature", "refactor", "intent-signature", False),
        ("interactive", "rule", "docs", "rule", False),
        # A label you set or Claude reported is yours, even when it is the catch-all.
        ("mixed", "override", "general-dev", "override", False),
        ("mixed", "reported", "general-dev", "reported", False),
        # One label still a guess while the other is yours.
        ("mixed", "rule", "docs", "override", True),
        ("agentic", "override", "general-dev", "rule", True),
        # Nothing known: not a guess to show.
        ("", "", "", "", False),
    ],
)
def test_a_label_is_low_confidence_only_when_a_rule_chose_the_catch_all(
    mode, mode_source, purpose, purpose_source, expected
):
    assert classify.is_low_confidence(mode, mode_source, purpose, purpose_source) is expected


def test_the_catch_all_labels_are_the_ones_the_last_rules_give():
    assert classify.CATCH_ALL_MODE == "mixed" and classify.CATCH_ALL_PURPOSE == "general-dev"
    assert classify.is_low_confidence(classify.CATCH_ALL_MODE, "rule", "docs", "rule")
    assert classify.is_low_confidence("interactive", "rule", classify.CATCH_ALL_PURPOSE, "rule")

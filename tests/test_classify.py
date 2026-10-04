"""Tests for WP5: session mode/purpose classification and ``SessionRecord``
construction/grouping (``src/claudeglass/classify.py``).

The three fixture session directories under ``tests/fixtures/classify/``
are hand-built, realistic-shaped JSONL (the same schema
``tests/helpers.py`` produces) parsed through the real
``discovery``/``parse`` pipeline, one per mode this WP's rules must
reach: ``interactive_chat`` (four short human/assistant exchanges, no
subagents), ``long_agentic`` (three spawned subagents, only two human
prompts), and ``overnight`` (a five-hour span with a 90-minute human
gap). ``classify_purpose``'s individual rules are exercised directly
against hand-built ``SessionFeatures`` instead — they're pure functions
of the feature bundle, so there's no need to round-trip through a parser
fixture for each one.
"""

from __future__ import annotations

import re
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from claudeglass import classify, discovery
from claudeglass.model import TranscriptMeta
from claudeglass.parse import parse_transcript

from helpers import turn_line, write_jsonl

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
    # Fix 5: the fixture's activity (22:00-04:00 UTC) is genuine
    # night-time local activity, not just a long span/gap at an
    # arbitrary hour -- the real evidence this WP's overnight rule now
    # requires.
    assert features.night_turn_share > 0.0 or features.long_gap_in_night

    mode, evidence = classify.classify_mode(features)
    assert mode == "overnight"
    assert "span_s" in evidence
    assert "human_gap_max_s" in evidence
    assert "night_turn_share" in evidence
    assert "long_gap_in_night" in evidence


def test_mode_mixed_fallback_on_hand_built_features():
    f = classify.SessionFeatures(
        span_s=100.0,
        human_gap_median_s=1000.0,
        human_gap_max_s=1000.0,
        subagent_count=5,
        # Above long_agentic_max_human_prompts (10) so this still falls
        # through past the subagent-chain rule into "mixed".
        human_prompts=11,
        assistant_turns=5,
    )
    mode, evidence = classify.classify_mode(f)
    assert mode == "mixed"
    assert "human_prompts" in evidence


# --------------------------------------------------------------------
# Fix 5: overnight requires genuine local night-time activity
# --------------------------------------------------------------------


def test_overnight_does_not_fire_without_night_time_evidence():
    """A long span with a long human gap in the middle of the (local)
    afternoon -- e.g. a session spanning a long lunch break or an
    all-afternoon meeting -- must not be classified overnight just
    because it happens to clear the span/gap thresholds.
    """
    f = classify.SessionFeatures(
        span_s=6 * 3600,
        human_gap_max_s=90 * 60,
        human_gap_median_s=90 * 60,
        night_turn_share=0.0,
        long_gap_in_night=False,
    )
    mode, evidence = classify.classify_mode(f)
    assert mode != "overnight"


def test_overnight_fires_via_night_turn_share_alone():
    f = classify.SessionFeatures(
        span_s=6 * 3600,
        human_gap_max_s=90 * 60,
        night_turn_share=0.5,
        long_gap_in_night=False,
    )
    mode, evidence = classify.classify_mode(f)
    assert mode == "overnight"
    assert evidence["night_turn_share"] == pytest.approx(0.5)


def test_overnight_fires_via_long_gap_in_night_alone():
    """A session with almost no top-level assistant turns to sample
    (night_turn_share near 0) can still be overnight if the one long
    idle gap itself sat in the night hours.
    """
    f = classify.SessionFeatures(
        span_s=6 * 3600,
        human_gap_max_s=90 * 60,
        night_turn_share=0.0,
        long_gap_in_night=True,
    )
    mode, evidence = classify.classify_mode(f)
    assert mode == "overnight"
    assert evidence["long_gap_in_night"] is True


def test_overnight_night_turn_share_threshold_is_overridable():
    f = classify.SessionFeatures(
        span_s=6 * 3600,
        human_gap_max_s=90 * 60,
        night_turn_share=0.1,
        long_gap_in_night=False,
    )
    mode, _ = classify.classify_mode(f)
    assert mode != "overnight"  # 0.1 < the 0.3 default

    mode, _ = classify.classify_mode(f, thresholds={"overnight_night_turn_share": 0.05})
    assert mode == "overnight"


def test_multi_day_evidence_added_on_fallthrough_when_span_exceeds_24h():
    f = classify.SessionFeatures(
        span_s=30 * 3600,  # > 24h
        human_gap_max_s=90 * 60,
        night_turn_share=0.0,
        long_gap_in_night=False,  # overnight's night check fails -> falls through
        subagent_count=5,
        # Above long_agentic_max_human_prompts (10) so this still falls
        # through past the subagent-chain rule into "mixed".
        human_prompts=11,
        assistant_turns=5,
    )
    mode, evidence = classify.classify_mode(f)
    assert mode == "mixed"
    assert evidence["multi_day"] is True


def test_multi_day_evidence_absent_when_span_under_24h():
    f = classify.SessionFeatures(
        span_s=6 * 3600,
        human_gap_max_s=90 * 60,
        night_turn_share=0.0,
        long_gap_in_night=False,
        subagent_count=5,
        # Above long_agentic_max_human_prompts (10) so this still falls
        # through past the subagent-chain rule into "mixed".
        human_prompts=11,
        assistant_turns=5,
    )
    mode, evidence = classify.classify_mode(f)
    assert mode == "mixed"
    assert "multi_day" not in evidence


def test_multi_day_evidence_not_added_when_overnight_fires():
    f = classify.SessionFeatures(
        span_s=30 * 3600,
        human_gap_max_s=90 * 60,
        night_turn_share=0.5,
    )
    mode, evidence = classify.classify_mode(f)
    assert mode == "overnight"
    assert "multi_day" not in evidence


def test_multi_day_span_threshold_is_overridable():
    f = classify.SessionFeatures(
        span_s=10 * 3600,
        human_gap_max_s=90 * 60,
        night_turn_share=0.0,
        long_gap_in_night=False,
    )
    mode, evidence = classify.classify_mode(f, thresholds={"multi_day_span_s": 8 * 3600})
    assert mode == "mixed"
    assert evidence["multi_day"] is True


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


def test_classify_mode_overnight_uses_effective_span_not_raw_span():
    """A session whose raw span clears overnight_span_s only because of a
    usage-cap pause must not fire overnight once limit_pause_s discounts
    it back under the threshold.
    """
    f = classify.SessionFeatures(
        span_s=6 * 3600,
        limit_pause_s=5 * 3600,  # effective_span_s = 1h, well under the 4h default
        human_gap_max_s=90 * 60,
        night_turn_share=0.5,
    )
    mode, evidence = classify.classify_mode(f)
    assert mode != "overnight"


def test_classify_mode_overnight_still_fires_when_effective_span_clears_threshold():
    f = classify.SessionFeatures(
        span_s=10 * 3600,
        limit_pause_s=3 * 3600,  # effective_span_s = 7h, still > the 4h default
        human_gap_max_s=90 * 60,
        night_turn_share=0.5,
    )
    mode, evidence = classify.classify_mode(f)
    assert mode == "overnight"
    assert evidence["span_s"] == pytest.approx(10 * 3600)
    assert evidence["effective_span_s"] == pytest.approx(7 * 3600)


def test_extract_features_limit_pause_s_from_synthetic_fixture(tmp_path: Path):
    lines = [
        turn_line(message_id="msg_1", input_tokens=10, timestamp="2026-09-18T12:00:00.000Z"),
        turn_line(
            message_id="msg_synth",
            model="<synthetic>",
            isApiErrorMessage=True,
            input_tokens=0,
            output_tokens=0,
            content=[{"type": "text", "text": "You've hit your session limit · resets 3pm (Europe/London)"}],
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
            "mode": {"overnight_night_turn_share": 0.1},
            "purpose": {"local_llm_min_hits": 1},
        },
        "some_other_package": {"unrelated": True},
    }
    mode_t, purpose_t = classify.mode_and_purpose_thresholds_from_config(config_thresholds)
    assert mode_t == {"overnight_night_turn_share": 0.1}
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

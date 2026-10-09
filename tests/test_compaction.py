"""Tests for WP6's compaction analytics (src/claudeglass/compaction.py).

Two fixtures:

- A small, fully hand-checked transcript ("Fixture A" below), built at
  test time via ``tests/helpers.write_jsonl`` per the WP6 brief: 4 priced
  turns and 2 ``compact_boundary`` system lines (triggers ``auto`` then
  ``manual``), used for every arithmetic assertion (ratio, dropped-token
  share, post-compaction write cost, RE-CACHE flag, per-session summary).
- The committed ``tests/fixtures/compaction/rediscovery_window.jsonl``
  (27 priced turns, same two triggers), used only for the rediscovery
  before/after counts, where a realistic 10-turn window either side of
  each compaction actually matters. See
  ``scripts/_gen_compaction_fixture.py``-equivalent generation logic
  inlined in this docstring's sibling comment below for the exact tool
  assignment per turn, reproduced here for the assertions:

  Pre-compaction #1 tools (turns 1-12): Bash, Read, Grep, Write, Read,
  Bash, Glob, Read, Bash, Grep, Write, Bash.
  Post-compaction #1 tools (turns 13-24): none, Read, Read, Grep, Read,
  Bash, Glob, Read, Read, Bash, Grep, Bash.
  Trailing tools (turns 25-27): Bash, Read, Glob.

  Compaction #1's "before" window is the last 10 pre-compaction turns
  (turns 3-12): Grep, Write, Read, Bash, Glob, Read, Bash, Grep, Write,
  Bash -> 5 of 10 are Read/Grep/Glob.
  Compaction #1's "after" window is the first 10 post-compaction turns
  (turns 13-22): none, Read, Read, Grep, Read, Bash, Glob, Read, Read,
  Bash -> 7 of 10 are Read/Grep/Glob.
  Compaction #2's "before" window is turns 15-24: Read, Grep, Read, Bash,
  Glob, Read, Read, Bash, Grep, Bash -> 7 of 10.
  Compaction #2's "after" window is only turns 25-27 (transcript ends):
  Bash, Read, Glob -> 2 of 3.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claudeglass import compaction
from claudeglass.model import TranscriptMeta
from claudeglass.parse import parse_transcript
from claudeglass.pricing import load_pricing

from helpers import assert_privacy, system_line, tool_use_block, turn_line, user_str_line, write_jsonl

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "compaction"

_SONNET_5_CACHE_WRITE_1H = 4.0
_SONNET_5_CACHE_WRITE_5M = 2.5


def _build_fixture_a(tmp_path: Path, session_id: str = "sess_A") -> tuple[Path, TranscriptMeta]:
    """4 priced turns, 2 compact_boundary lines (auto then manual): see
    this module's docstring for the exact numbers. Turn 2 (right after
    compaction #1) is a RE-CACHE turn (ctx=26000 > 20000, cache_read=1000
    < 0.2*26000=5200); turn 4 (right after compaction #2) is not
    (ctx=600 <= 20000).
    """
    lines = [
        turn_line(
            model="claude-sonnet-5",
            timestamp="2026-09-18T12:00:00.000Z",
            ephemeral_5m_input_tokens=1000,
            input_tokens=100,
            output_tokens=50,
            content=[tool_use_block("Bash", "tu1", {"command": "echo hi"})],
        ),
        system_line(
            "compact_boundary",
            timestamp="2026-09-18T12:00:30.000Z",
            compactMetadata={
                "trigger": "auto",
                "preTokens": 100000,
                "postTokens": 20000,
                "cumulativeDroppedTokens": 80000,
                "durationMs": 1200,
            },
        ),
        turn_line(
            model="claude-sonnet-5",
            timestamp="2026-09-18T12:01:00.000Z",
            ephemeral_1h_input_tokens=25000,
            cache_read_input_tokens=1000,
            input_tokens=0,
            output_tokens=80,
        ),
        turn_line(
            model="claude-sonnet-5",
            timestamp="2026-09-18T12:02:00.000Z",
            ephemeral_5m_input_tokens=300,
            cache_read_input_tokens=25000,
            input_tokens=50,
            output_tokens=30,
            content=[tool_use_block("Read", "tu2", {"file_path": "x.txt"})],
        ),
        system_line(
            "compact_boundary",
            timestamp="2026-09-18T12:02:30.000Z",
            compactMetadata={
                "trigger": "manual",
                "preTokens": 50000,
                "postTokens": 15000,
                # Real cumulativeDroppedTokens is a running total across the
                # whole session (verified against a real corpus -- see
                # compaction.py's docstring): 80000 from compaction #1 plus
                # this compaction's own 35000 = 115000, not a fresh 35000.
                "cumulativeDroppedTokens": 115000,
                "durationMs": 900,
            },
        ),
        turn_line(
            model="claude-sonnet-5",
            timestamp="2026-09-18T12:03:00.000Z",
            ephemeral_5m_input_tokens=300,
            cache_read_input_tokens=100,
            input_tokens=200,
            output_tokens=40,
            content=[tool_use_block("Bash", "tu3", {"command": "echo done"})],
        ),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    return path, TranscriptMeta(path=str(path), session_id=session_id)


@pytest.fixture()
def sonnet_rates():
    pricing = load_pricing()
    return pricing.resolve_model("claude-sonnet-5")


# -- is_recache_turn --------------------------------------------------------


def test_is_recache_turn_true_above_floor_and_below_ratio():
    from claudeglass.model import Turn

    turn = Turn(ctx=26000, cache_read_tokens=1000)
    assert compaction.is_recache_turn(turn) is True


def test_is_recache_turn_false_at_or_below_ctx_floor():
    from claudeglass.model import Turn

    turn = Turn(ctx=20000, cache_read_tokens=0)
    assert compaction.is_recache_turn(turn) is False


def test_is_recache_turn_false_when_cache_read_share_high():
    from claudeglass.model import Turn

    turn = Turn(ctx=26000, cache_read_tokens=10000)  # 10000 >= 0.2*26000=5200
    assert compaction.is_recache_turn(turn) is False


def test_is_recache_turn_honors_a_custom_recache_thresholds():
    """Fix item 6: ``ctx=15000`` never qualifies under the default
    ``RecacheThresholds`` (ctx_floor=20_000), but does once a caller
    passes a lower ``ctx_floor`` — same shared dataclass ``recache.py``
    itself uses, not an independent copy of the two numbers."""
    from claudeglass.model import Turn
    from claudeglass.recache import RecacheThresholds

    turn = Turn(ctx=15000, cache_read_tokens=1000)
    assert compaction.is_recache_turn(turn) is False
    assert compaction.is_recache_turn(turn, RecacheThresholds(ctx_floor=10_000)) is True


# -- new_tokens --------------------------------------------------------------


def test_new_tokens_is_input_plus_cache_creation():
    from claudeglass.model import Turn

    turn = Turn(input_tokens=100, cache_creation_tokens=250, cache_read_tokens=999999)
    assert compaction.new_tokens(turn) == 350


def test_new_tokens_ignores_cache_read():
    from claudeglass.model import Turn

    turn = Turn(input_tokens=0, cache_creation_tokens=0, cache_read_tokens=50000)
    assert compaction.new_tokens(turn) == 0


# -- compaction_records_for_transcript --------------------------------------


def test_compaction_records_two_triggers_ratio_and_recache(tmp_path, sonnet_rates):
    path, meta = _build_fixture_a(tmp_path)
    result = parse_transcript(path, meta)

    records = compaction.compaction_records_for_transcript(result, sonnet_rates)
    assert len(records) == 2

    first, second = records
    assert first.trigger == "auto"
    assert first.pre_tokens == 100000
    assert first.post_tokens == 20000
    assert first.dropped_tokens == 80000
    assert first.duration_ms == 1200
    assert first.ratio == pytest.approx(0.2)
    assert first.next_turn_cache_creation == 25000
    assert first.next_turn_write_cost == pytest.approx(25000 * _SONNET_5_CACHE_WRITE_1H / 1_000_000)
    assert first.next_turn_is_recache is True
    # Fix item 9: event ts 12:00:30 -> next turn ts 12:01:00 is a 30s join.
    assert first.join_delta_s == pytest.approx(30.0)

    assert second.trigger == "manual"
    assert second.pre_tokens == 50000
    assert second.post_tokens == 15000
    assert second.dropped_tokens == 35000
    assert second.duration_ms == 900
    assert second.ratio == pytest.approx(0.3)
    assert second.next_turn_cache_creation == 300
    assert second.next_turn_write_cost == pytest.approx(300 * _SONNET_5_CACHE_WRITE_5M / 1_000_000)
    assert second.next_turn_is_recache is False
    assert second.join_delta_s == pytest.approx(30.0)

    assert_privacy(result)


def test_compaction_record_session_id_from_meta(tmp_path, sonnet_rates):
    path, meta = _build_fixture_a(tmp_path, session_id="sess_XYZ")
    result = parse_transcript(path, meta)
    records = compaction.compaction_records_for_transcript(result, sonnet_rates)
    assert all(r.session_id == "sess_XYZ" for r in records)


def test_compaction_record_ratio_none_when_pre_tokens_zero(tmp_path, sonnet_rates):
    lines = [
        turn_line(model="claude-sonnet-5", timestamp="2026-09-18T12:00:00.000Z"),
        system_line(
            "compact_boundary",
            timestamp="2026-09-18T12:00:30.000Z",
            compactMetadata={"trigger": "auto", "postTokens": 0, "cumulativeDroppedTokens": 0},
        ),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path), session_id="sess_Z"))
    records = compaction.compaction_records_for_transcript(result, sonnet_rates)
    assert len(records) == 1
    assert records[0].pre_tokens is None
    assert records[0].ratio is None


def test_compaction_record_no_following_turn_leaves_next_fields_none(tmp_path, sonnet_rates):
    lines = [
        turn_line(model="claude-sonnet-5", timestamp="2026-09-18T12:00:00.000Z"),
        system_line(
            "compact_boundary",
            timestamp="2026-09-18T12:01:00.000Z",
            compactMetadata={"trigger": "auto", "preTokens": 1000, "postTokens": 100},
        ),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path), session_id="sess_tail"))
    records = compaction.compaction_records_for_transcript(result, sonnet_rates)
    assert len(records) == 1
    assert records[0].next_turn_cache_creation is None
    assert records[0].next_turn_write_cost is None
    assert records[0].next_turn_is_recache is None
    assert records[0].join_delta_s is None


def test_compaction_record_dropped_tokens_is_delta_not_raw_cumulative(tmp_path, sonnet_rates):
    """Fix 4: compactMetadata.cumulativeDroppedTokens is a running total for
    the whole session (verified against a real corpus, see compaction.py's
    module docstring), so a session with two compactions where the field
    reads 80000 then 115000 must report per-compaction drops of 80000 and
    35000 -- not 80000 and 115000 (which would double count the first
    compaction's drop).
    """
    path, meta = _build_fixture_a(tmp_path)
    result = parse_transcript(path, meta)
    records = compaction.compaction_records_for_transcript(result, sonnet_rates)
    assert [r.dropped_tokens for r in records] == [80000, 35000]


def test_compaction_record_dropped_tokens_counter_reset_falls_back_to_raw(tmp_path, sonnet_rates):
    """A cumulative counter that goes backwards (never seen on the real
    corpus, but not something to crash or go negative on) is treated as a
    fresh counter: the delta falls back to the raw value instead of
    subtracting into a nonsensical negative number.
    """
    lines = [
        turn_line(model="claude-sonnet-5", timestamp="2026-09-18T12:00:00.000Z"),
        system_line(
            "compact_boundary",
            timestamp="2026-09-18T12:00:30.000Z",
            compactMetadata={"trigger": "auto", "preTokens": 100000, "cumulativeDroppedTokens": 80000},
        ),
        turn_line(model="claude-sonnet-5", timestamp="2026-09-18T12:01:00.000Z"),
        system_line(
            "compact_boundary",
            timestamp="2026-09-18T12:01:30.000Z",
            compactMetadata={"trigger": "auto", "preTokens": 50000, "cumulativeDroppedTokens": 10000},
        ),
        turn_line(model="claude-sonnet-5", timestamp="2026-09-18T12:02:00.000Z"),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path), session_id="sess_reset"))
    records = compaction.compaction_records_for_transcript(result, sonnet_rates)
    assert [r.dropped_tokens for r in records] == [80000, 10000]


def test_compaction_record_dropped_tokens_none_when_field_absent(tmp_path, sonnet_rates):
    lines = [
        turn_line(model="claude-sonnet-5", timestamp="2026-09-18T12:00:00.000Z"),
        system_line(
            "compact_boundary",
            timestamp="2026-09-18T12:00:30.000Z",
            compactMetadata={"trigger": "auto", "preTokens": 1000, "postTokens": 100},
        ),
        turn_line(model="claude-sonnet-5", timestamp="2026-09-18T12:01:00.000Z"),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path), session_id="sess_no_drop"))
    records = compaction.compaction_records_for_transcript(result, sonnet_rates)
    assert records[0].dropped_tokens is None


def test_compaction_join_event_with_unparsable_timestamp_leaves_next_fields_none(tmp_path, sonnet_rates):
    lines = [
        turn_line(model="claude-sonnet-5", timestamp="2026-09-18T12:00:00.000Z"),
        system_line(
            "compact_boundary",
            timestamp="not-a-timestamp",
            compactMetadata={"trigger": "auto", "preTokens": 1000, "postTokens": 100},
        ),
        turn_line(model="claude-sonnet-5", timestamp="2026-09-18T12:01:00.000Z"),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path), session_id="sess_bad_ts"))
    records = compaction.compaction_records_for_transcript(result, sonnet_rates)
    assert len(records) == 1
    # Must not silently grab the pointer's current turn -- an event with no
    # parsable timestamp of its own can't be correlated to anything.
    assert records[0].next_turn_cache_creation is None
    assert records[0].next_turn_write_cost is None
    assert records[0].next_turn_is_recache is None
    assert records[0].join_delta_s is None


def test_compaction_join_skips_turn_with_unparsable_timestamp(tmp_path, sonnet_rates):
    """A priced turn with no parsable timestamp can't be confirmed as
    before or after the compaction event, so it's skipped rather than
    wrongly matched as "the turn right after this compaction" -- the turn
    that follows it (with a real timestamp after the event) is the one
    that should be correlated instead.
    """
    lines = [
        turn_line(model="claude-sonnet-5", timestamp="2026-09-18T12:00:00.000Z"),
        system_line(
            "compact_boundary",
            timestamp="2026-09-18T12:00:30.000Z",
            compactMetadata={"trigger": "auto", "preTokens": 1000, "postTokens": 100},
        ),
        turn_line(
            model="claude-sonnet-5",
            timestamp="not-a-timestamp",
            ephemeral_5m_input_tokens=999999,
        ),
        turn_line(
            model="claude-sonnet-5",
            timestamp="2026-09-18T12:01:00.000Z",
            ephemeral_5m_input_tokens=42,
        ),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path), session_id="sess_bad_turn_ts"))
    records = compaction.compaction_records_for_transcript(result, sonnet_rates)
    assert len(records) == 1
    assert records[0].next_turn_cache_creation == 42


def test_compaction_records_empty_when_no_compact_boundary(tmp_path, sonnet_rates):
    lines = [turn_line(model="claude-sonnet-5")]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path), session_id="sess_none"))
    assert compaction.compaction_records_for_transcript(result, sonnet_rates) == []


# -- CompactionStats ----------------------------------------------------


def test_a_scheduled_main_session_is_not_counted_as_a_session(tmp_path, sonnet_rates):
    """A watchdog a scheduled task started, with no message of yours,
    never summarises: counted, 22 of them would cut compactions per
    session five-fold."""
    path, meta = _build_fixture_a(tmp_path)
    watchdog = tmp_path / "watchdog.jsonl"
    write_jsonl(watchdog, [
        user_str_line('<scheduled-task name="heartbeat">Check the heartbeat.</scheduled-task>'),
        turn_line(model="claude-sonnet-5", timestamp="2026-09-18T13:00:00.000Z"),
    ])
    stats = compaction.CompactionStats()
    stats.add_transcript(parse_transcript(path, meta), sonnet_rates)
    for i in range(4):
        stats.add_transcript(parse_transcript(watchdog, TranscriptMeta(path=str(watchdog), session_id=f"w{i}")),
                             sonnet_rates)
    assert (stats.total_sessions, stats.scheduled_sessions) == (1, 4)
    assert stats.compactions_per_session_mean == pytest.approx(2.0)
    notes = compaction.build_section(stats).notes
    assert any(note.startswith("4 main sessions a scheduled task started") for note in notes)


def test_compaction_stats_aggregates_over_fixture_a(tmp_path, sonnet_rates):
    path, meta = _build_fixture_a(tmp_path)
    result = parse_transcript(path, meta)

    stats = compaction.CompactionStats()
    stats.add_transcript(result, sonnet_rates)

    assert stats.total_sessions == 1
    assert stats.sessions_with_compaction == 1
    assert stats.compactions_per_session_mean == pytest.approx(2.0)
    # total_sessions == sessions_with_compaction here, so the R8 fix
    # (dividing by total_sessions) and the old compacting-sessions-only
    # denominator agree.
    assert stats.compactions_per_compacting_session_mean == pytest.approx(2.0)
    assert stats.compactions_per_session_max == 2
    assert stats.trigger_mix == {"auto": 1, "manual": 1}
    assert stats.pre_median == pytest.approx(75000.0)
    assert stats.post_median == pytest.approx(17500.0)
    assert stats.dropped_total == 115000
    # total cache_creation across all 4 priced turns: 1000+25000+300+300
    assert stats.total_cache_creation == 26600
    assert stats.dropped_share_of_cache_creation == pytest.approx(115000 / 26600 * 100)
    # Fix item 9: total new_tokens (input+cache_creation) across the same 4
    # turns: (100+1000)+(0+25000)+(50+300)+(200+300) = 26950.
    assert stats.total_new_tokens == 26950
    assert stats.dropped_share_of_new_tokens == pytest.approx(115000 / 26950 * 100)
    assert stats.mean_duration_ms == pytest.approx(1050.0)
    # Only the first record (next_turn_is_recache=True) counts.
    assert stats.total_post_compaction_recache_cost == pytest.approx(
        25000 * _SONNET_5_CACHE_WRITE_1H / 1_000_000
    )
    # Fix 4: the all-inclusive total counts BOTH records' next-turn write
    # cost, regardless of the RE-CACHE flag -- strictly larger than the
    # RE-CACHE-only total above whenever a non-recache turn also wrote to
    # cache (turn 4 here: 300 tokens at the 5m rate).
    assert stats.total_post_compaction_write_cost == pytest.approx(
        25000 * _SONNET_5_CACHE_WRITE_1H / 1_000_000 + 300 * _SONNET_5_CACHE_WRITE_5M / 1_000_000
    )
    assert stats.total_post_compaction_write_cost > stats.total_post_compaction_recache_cost


def _build_fixture_loose_join(tmp_path, session_id: str = "sess_loose"):
    """One compaction whose matched next turn lands 1000s later — past
    fix item 9's 900s (15 minute) join-tightness threshold. Its per-record
    ``next_turn_*`` fields are still populated (the correlation genuinely
    found that turn), but ``CompactionStats``' aggregates must exclude it:
    a gap this large means the turn most likely belongs to a resumed
    session, not to recovering from this compaction.
    """
    lines = [
        turn_line(model="claude-sonnet-5", timestamp="2026-09-18T12:00:00.000Z"),
        system_line(
            "compact_boundary",
            timestamp="2026-09-18T12:00:30.000Z",
            compactMetadata={
                "trigger": "auto",
                "preTokens": 100000,
                "postTokens": 20000,
                "cumulativeDroppedTokens": 80000,
                "durationMs": 1200,
            },
        ),
        turn_line(
            model="claude-sonnet-5",
            # 1000s after the compact_boundary event (12:00:30 + 1000s).
            timestamp="2026-09-18T12:17:10.000Z",
            ephemeral_1h_input_tokens=25000,
            cache_read_input_tokens=1000,
            input_tokens=0,
            output_tokens=80,
        ),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    return path, TranscriptMeta(path=str(path), session_id=session_id)


def test_join_delta_s_over_900s_reported_on_the_record(tmp_path, sonnet_rates):
    path, meta = _build_fixture_loose_join(tmp_path)
    result = parse_transcript(path, meta)
    records = compaction.compaction_records_for_transcript(result, sonnet_rates)
    assert len(records) == 1
    assert records[0].join_delta_s == pytest.approx(1000.0)
    # The per-record fields are unaffected by join tightness -- the
    # correlation did find this turn; only the aggregates below gate on it.
    assert records[0].next_turn_write_cost == pytest.approx(
        25000 * _SONNET_5_CACHE_WRITE_1H / 1_000_000
    )
    assert records[0].next_turn_is_recache is True


def test_compaction_stats_excludes_loose_join_from_write_cost_aggregates(tmp_path, sonnet_rates):
    path, meta = _build_fixture_loose_join(tmp_path)
    result = parse_transcript(path, meta)

    stats = compaction.CompactionStats()
    stats.add_transcript(result, sonnet_rates)

    # Fix item 9: join_delta_s (1000s) exceeds the 900s threshold, so
    # neither cost aggregate counts this record's next_turn_write_cost.
    assert stats.total_post_compaction_write_cost == 0.0
    assert stats.total_post_compaction_recache_cost == 0.0
    # The compaction's own dropped-token delta has nothing to do with the
    # next-turn join, so it still counts in full.
    assert stats.dropped_total == 80000

    rows = stats.per_session_summary()
    assert len(rows) == 1
    session_id, count, dropped, cost = rows[0]
    assert session_id == "sess_loose"
    assert count == 1
    assert dropped == 80000
    assert cost == 0.0


def test_compaction_stats_build_classmethod_matches_manual_fold(tmp_path, sonnet_rates):
    path, meta = _build_fixture_a(tmp_path)
    result = parse_transcript(path, meta)

    via_build = compaction.CompactionStats.build([(result, sonnet_rates)])
    via_manual = compaction.CompactionStats()
    via_manual.add_transcript(result, sonnet_rates)

    assert via_build.dropped_total == via_manual.dropped_total
    assert via_build.trigger_mix == via_manual.trigger_mix
    assert len(via_build.records) == len(via_manual.records)


def test_compaction_stats_sessions_without_compaction_are_not_counted(tmp_path, sonnet_rates):
    no_compaction_lines = [turn_line(model="claude-sonnet-5", ephemeral_5m_input_tokens=50)]
    path = tmp_path / "no_compaction.jsonl"
    write_jsonl(path, no_compaction_lines)
    no_compaction_result = parse_transcript(
        path, TranscriptMeta(path=str(path), session_id="sess_none")
    )

    fixture_path, meta = _build_fixture_a(tmp_path, session_id="sess_with")
    with_compaction_result = parse_transcript(fixture_path, meta)

    stats = compaction.CompactionStats.build(
        [(no_compaction_result, sonnet_rates), (with_compaction_result, sonnet_rates)]
    )
    assert stats.total_sessions == 2
    assert stats.sessions_with_compaction == 1
    # R8 fix: compactions_per_session_mean now divides by every session
    # folded in (total_sessions=2), not just the compacting one, so the
    # zero-compaction session pulls the mean down to 1.0 (2 compactions /
    # 2 sessions) instead of silently vanishing from the denominator.
    assert stats.compactions_per_session_mean == pytest.approx(1.0)
    # The old (compacting-sessions-only) denominator is kept as its own
    # stat: 2 compactions / 1 compacting session = 2.0.
    assert stats.compactions_per_compacting_session_mean == pytest.approx(2.0)


def test_per_session_summary_sorted_by_dropped_tokens_desc(tmp_path, sonnet_rates):
    path, meta = _build_fixture_a(tmp_path, session_id="sess_only")
    result = parse_transcript(path, meta)
    stats = compaction.CompactionStats.build([(result, sonnet_rates)])
    rows = stats.per_session_summary()
    assert rows == [("sess_only", 2, 115000, pytest.approx(0.1 + 0.00075))]


# -- build_section --------------------------------------------------------


def test_build_section_shape_and_notes(tmp_path, sonnet_rates):
    path, meta = _build_fixture_a(tmp_path)
    result = parse_transcript(path, meta)
    stats = compaction.CompactionStats.build([(result, sonnet_rates)])

    section = compaction.build_section(stats)
    assert section.key == "compactions"
    assert section.title == "Compactions"
    table_names = [t.name for t in section.tables]
    assert table_names == [
        "compactions_summary",
        "compactions_trigger_mix",
        "compactions_per_session",
    ]
    # R19: this note used to promise a future switch ("WP10 will switch
    # this section to the shared recache.py detector once WP3 lands"),
    # stale ever since the WP10b addition described in the module
    # docstring actually landed that switch. It now describes current
    # behaviour instead of a pending plan: the shared recache.py detector,
    # in plain words.
    assert any("same check the cache rebuild section uses" in note for note in section.notes)

    summary_table = section.tables[0]
    summary_metrics = {row[0]: row[1] for row in summary_table.rows}
    assert summary_metrics["Total post-compaction write cost (USD)"] == pytest.approx(
        stats.total_post_compaction_write_cost
    )
    assert summary_metrics["Total post-compaction RE-CACHE-flagged write cost (USD)"] == pytest.approx(
        stats.total_post_compaction_recache_cost
    )
    # Fix item 9: dropped-share is now reported against both denominators.
    assert summary_metrics[
        "Dropped tokens (share of new_tokens: input+cache_creation)"
    ] == pytest.approx(stats.dropped_share_of_new_tokens)
    assert any("more than 15 minutes later" in note for note in section.notes)

    trigger_table = section.tables[1]
    trigger_rows = {row[0]: row[1] for row in trigger_table.rows}
    assert trigger_rows == {"auto": 1, "manual": 1}

    per_session_table = section.tables[2]
    assert len(per_session_table.rows) == 1
    assert per_session_table.rows[0][0] == "sess_A"


def test_build_section_empty_stats_has_no_data_note():
    stats = compaction.CompactionStats()
    section = compaction.build_section(stats)
    assert section.tables[0].rows  # summary table still has metric rows
    assert any("No conversation summaries found" in note for note in section.notes)


def test_build_section_per_session_table_limited_to_20(tmp_path, sonnet_rates):
    stats = compaction.CompactionStats()
    for i in range(25):
        lines = [
            turn_line(model="claude-sonnet-5", timestamp="2026-09-18T12:00:00.000Z"),
            system_line(
                "compact_boundary",
                timestamp="2026-09-18T12:00:30.000Z",
                compactMetadata={
                    "trigger": "auto",
                    "preTokens": 1000,
                    "postTokens": 100,
                    "cumulativeDroppedTokens": 900 + i,
                },
            ),
            turn_line(model="claude-sonnet-5", timestamp="2026-09-18T12:01:00.000Z"),
        ]
        path = tmp_path / f"session_{i}.jsonl"
        write_jsonl(path, lines)
        result = parse_transcript(path, TranscriptMeta(path=str(path), session_id=f"sess_{i}"))
        stats.add_transcript(result, sonnet_rates)

    section = compaction.build_section(stats)
    per_session_table = section.tables[2]
    assert len(per_session_table.rows) == 20
    # Sorted descending by dropped tokens: session 24 (900+24=924) first.
    assert per_session_table.rows[0][0] == "sess_24"


# -- rediscovery ----------------------------------------------------------


def _parse_rediscovery_fixture():
    path = FIXTURES / "rediscovery_window.jsonl"
    return parse_transcript(path, TranscriptMeta(path=str(path), session_id="sess_rediscovery"))


def test_rediscovery_before_after_counts_both_compactions():
    result = _parse_rediscovery_fixture()
    windows = compaction.rediscovery(result)
    assert len(windows) == 2

    first, second = windows
    assert first.before_total == 10
    assert first.before_count == 5
    assert first.after_total == 10
    assert first.after_count == 7

    assert second.before_total == 10
    assert second.before_count == 7
    # Only 3 turns remain after the second compaction (transcript ends).
    assert second.after_total == 3
    assert second.after_count == 2


def test_rediscovery_window_session_id_and_ts(tmp_path):
    path, meta = _build_fixture_a(tmp_path, session_id="sess_short")
    result = parse_transcript(path, meta)
    windows = compaction.rediscovery(result)
    assert len(windows) == 2
    assert all(w.session_id == "sess_short" for w in windows)
    assert windows[0].compaction_ts == "2026-09-18T12:00:30.000Z"
    assert windows[1].compaction_ts == "2026-09-18T12:02:30.000Z"


def test_rediscovery_empty_when_no_compaction(tmp_path):
    lines = [turn_line(model="claude-sonnet-5")]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    result = parse_transcript(path, TranscriptMeta(path=str(path), session_id="sess_none"))
    assert compaction.rediscovery(result) == []


# -- agent runs: subagent and workflow transcripts ------------------------


def _compacting_transcript(
    tmp_path: Path, name: str, session_id: str, compactions: int, kind: str = "top-level", agent_id: str | None = None
):
    """A transcript with ``compactions`` automatic summaries, each followed by a reply."""
    lines = [turn_line(model="claude-sonnet-5", timestamp="2026-09-18T12:00:00.000Z", ephemeral_5m_input_tokens=1000)]
    for i in range(compactions):
        lines.append(
            system_line(
                "compact_boundary",
                timestamp=f"2026-09-18T12:0{i + 1}:00.000Z",
                compactMetadata={
                    "trigger": "auto",
                    "preTokens": 100000,
                    "postTokens": 20000,
                    "cumulativeDroppedTokens": 80000 * (i + 1),
                    "durationMs": 1000,
                },
            )
        )
        lines.append(
            turn_line(model="claude-sonnet-5", timestamp=f"2026-09-18T12:0{i + 1}:30.000Z", ephemeral_5m_input_tokens=500)
        )
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    write_jsonl(path, lines)
    return parse_transcript(
        path, TranscriptMeta(path=str(path), session_id=session_id, kind=kind, agent_id=agent_id)
    )


def test_compactions_in_counts_one_transcript_the_same_whoever_ran_it(tmp_path):
    for kind in ("top-level", "subagent", "workflow-agent"):
        result = _compacting_transcript(tmp_path, f"{kind}.jsonl", "s", 3, kind=kind)
        assert compaction.compactions_in(result) == (3, 3), kind
    path, meta = _build_fixture_a(tmp_path)
    assert compaction.compactions_in(parse_transcript(path, meta)) == (2, 1)


def test_a_subagent_and_a_workflow_agent_file_are_counted_apart_from_the_main_session(tmp_path, sonnet_rates):
    main = _compacting_transcript(tmp_path, "main.jsonl", "s1", 1)
    sub = _compacting_transcript(tmp_path / "s1" / "subagents", "agent-a1.jsonl", "s1", 2, "subagent", "a1")
    workflow = _compacting_transcript(
        tmp_path / "s1" / "subagents" / "workflows" / "wf_1", "agent-b1.jsonl", "s1", 4, "workflow-agent", "b1"
    )
    stats = compaction.CompactionStats.build([(main, sonnet_rates), (sub, sonnet_rates), (workflow, sonnet_rates)])
    # Every record says whose it is.
    assert sorted(r.kind for r in stats.records) == ["subagent"] * 2 + ["top-level"] + ["workflow-agent"] * 4
    assert stats.agent_compactions == {"subagent": 2, "workflow-agent": 4}
    # The session's own figures are the main conversation's alone.
    assert stats.total_sessions == 1 and stats.sessions_with_compaction == 1
    assert stats.compactions_per_session_mean == pytest.approx(1.0)
    assert stats.compactions_per_session_max == 1
    assert [(sid, count) for sid, count, _dropped, _cost in stats.per_session_summary()] == [("s1", 1)]
    # The section shows the agent counts as rows.
    metrics = {row[0]: row[1] for row in compaction.build_section(stats).tables[0].rows}
    assert metrics["Compactions inside subagent runs"] == 2
    assert metrics["Compactions inside workflow agent runs"] == 4
    assert metrics["Sessions with >=1 compaction"] == 1


def test_a_session_whose_agents_compacted_three_times_is_not_a_heavy_session(tmp_path, sonnet_rates):
    main = _compacting_transcript(tmp_path, "main.jsonl", "s1", 1)
    sub = _compacting_transcript(tmp_path / "subagents", "agent-a1.jsonl", "s1", 5, "subagent", "a1")
    stats = compaction.CompactionStats.build([(main, sonnet_rates), (sub, sonnet_rates)])
    assert stats.heavy_session_cost_share({"s1": 10.0}) == (0, 0.0, 0.0)
    assert stats.agent_compactions == {"subagent": 5}


def test_the_share_of_main_session_cost_in_sessions_with_three_or_more_compactions(tmp_path, sonnet_rates):
    stats = compaction.CompactionStats()
    cost = {"heavy": 30.0, "edge": 20.0, "light": 40.0, "none": 10.0}
    for name, count in (("heavy", 5), ("edge", 3), ("light", 2), ("none", 0)):
        stats.add_transcript(_compacting_transcript(tmp_path, f"{name}.jsonl", name, count), sonnet_rates)
    # An agent run's cost is not in the base: the caller passes the main conversation's only.
    heavy, heavy_cost, share = stats.heavy_session_cost_share(cost)
    assert (heavy, heavy_cost) == (2, 50.0)
    assert share == pytest.approx(50.0)
    assert compaction.HEAVY_COMPACTIONS == 3
    section = compaction.build_section(stats, cost)
    metrics = {row[0]: row[1] for row in section.tables[0].rows}
    assert metrics["Sessions with 3+ compactions"] == 2
    assert metrics["Main-session cost in sessions with 3+ compactions (USD)"] == pytest.approx(50.0)
    assert metrics["Share of main-session cost in sessions with 3+ compactions"] == pytest.approx(50.0)
    assert any("main conversation only" in note for note in section.notes)


def test_the_compaction_cost_rows_are_left_out_when_there_is_no_cost_to_take_a_share_of(tmp_path, sonnet_rates):
    stats = compaction.CompactionStats()
    stats.add_transcript(_compacting_transcript(tmp_path, "a.jsonl", "a", 3), sonnet_rates)
    for given in (None, {}, {"a": 0.0}):
        metrics = {row[0] for row in compaction.build_section(stats, given).tables[0].rows}
        assert "Sessions with 3+ compactions" not in metrics, given
    # With no main session folded in there is nothing to take a share of.
    assert compaction.CompactionStats().heavy_session_cost_share({"x": 5.0}) is None


def test_the_compaction_cost_row_labels_are_in_the_help_copy():
    from claudeglass import helptext

    copy = helptext.TABLE_COPY["compactions_summary"]
    for key in (
        "Compactions inside subagent runs",
        "Compactions inside workflow agent runs",
        "Sessions with 3+ compactions",
        "Main-session cost in sessions with 3+ compactions (USD)",
        "Share of main-session cost in sessions with 3+ compactions",
    ):
        assert key in copy.value_labels and copy.row_kinds[key]

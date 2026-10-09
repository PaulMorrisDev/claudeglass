"""Context files: how often each CLAUDE.md-family file and skill is sent,
to whom, and what carrying it costs (``context_files.ContextFileStats``).
"""

from __future__ import annotations

import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from claudeglass import calibration, context_files, parse
from claudeglass.model import Column, Table, TranscriptMeta, TranscriptResult, Turn
from claudeglass.parse import parse_transcript
from claudeglass.pricing import effective_rates, load_pricing

from helpers import (
    attachment_line,
    system_line,
    tool_result_block,
    tool_use_block,
    turn_line,
    user_block_line,
    write_jsonl,
)

SALT = b"c" * 32


@pytest.fixture(autouse=True)
def _salt(monkeypatch):
    monkeypatch.setattr(parse, "_SALT", SALT)


def _stamp(line: dict, minute: int) -> dict:
    line["timestamp"] = f"2026-09-18T12:{minute:02d}:00.000Z"
    return line


def _transcript(tmp_path: Path, name: str, *, agent_type: str | None = None, skill_call: bool = False):
    lines = [
        _stamp(
            attachment_line(
                "instructions",
                files=[{"path": "C:/Users/u/.claude/CLAUDE.md", "type": "User", "content": "u" * 400}],
            ),
            0,
        ),
        _stamp(
            attachment_line(
                "skill_listing",
                content="- grill-me: Interview the user.\n- pdf: Read PDFs.\n",
                skillCount=2,
                names=["grill-me", "pdf"],
            ),
            0,
        ),
    ]
    for minute in (1, 2, 3):
        content = [{"type": "text", "text": "ok"}]
        if skill_call and minute == 2:
            content = [tool_use_block("Skill", "toolu_1", {"skill": "grill-me"})]
        lines.append(_stamp(turn_line(content=content, cache_read_input_tokens=1000), minute))
    path = tmp_path / f"{name}.jsonl"
    write_jsonl(path, lines)
    return parse_transcript(path, TranscriptMeta(path=str(path), agent_type=agent_type))


def test_counts_sends_and_reach_per_file_without_paths(tmp_path):
    stats = context_files.ContextFileStats()
    pricing = load_pricing()
    stats.add(_transcript(tmp_path, "main", skill_call=True), pricing, is_main=True)
    stats.add(_transcript(tmp_path, "sub", agent_type="Explore"), pricing, is_main=False)
    data = stats.to_dict()

    assert data["transcripts"] == {"Explore": 1, "main": 1}
    [row] = data["files"]
    assert row["hash"] == parse.path_hash("C:/Users/u/.claude/CLAUDE.md", SALT)
    assert row["tokens"] == 100
    assert row["sends"] == 2
    assert row["reach"] == {"Explore": 1, "main": 1}
    assert row["cost_usd"] > 0
    assert set(row["cost_by_reach"]) == {"Explore", "main"}
    assert "Users" not in repr(data) and "Interview" not in repr(data)


def test_skills_record_listing_and_use(tmp_path):
    stats = context_files.ContextFileStats()
    pricing = load_pricing()
    stats.add(_transcript(tmp_path, "main", skill_call=True), pricing, is_main=True)
    stats.add(_transcript(tmp_path, "sub", agent_type="Explore"), pricing, is_main=False)
    skills = {row["name"]: row for row in stats.to_dict()["skills"]}

    assert skills["grill-me"]["listed"] == {"Explore": 1, "main": 1}
    assert skills["grill-me"]["invoked"] == 1
    assert skills["grill-me"]["invoked_by"] == {"main": 1}
    assert skills["pdf"]["invoked"] == 0
    assert skills["pdf"]["listing_tokens"] == round(len("- pdf: Read PDFs.") / 4)
    assert skills["pdf"]["listing_cost_usd"] > 0


def test_carrying_costs_nothing_without_pricing(tmp_path):
    stats = context_files.ContextFileStats()
    stats.add(_transcript(tmp_path, "main"), None, is_main=True)
    data = stats.to_dict()
    assert data["files"][0]["cost_usd"] == 0
    assert data["files"][0]["sends"] == 1


def test_a_files_tokens_and_cost_use_the_calibrated_characters_per_token(tmp_path):
    """A file is 400 characters: 100 tokens at the default 4.0, 200 at a
    measured 2.0 for the model that read it (cost scales with the tokens)."""
    pricing = load_pricing()
    measured = calibration.Calibration(text={"claude-sonnet-5": 2.0}, default_family="claude-sonnet-5")
    result = _transcript(tmp_path, "main")

    plain = context_files.ContextFileStats()
    plain.add(result, pricing, is_main=True)
    found = context_files.ContextFileStats(calibration=measured)
    found.add(result, pricing, is_main=True)

    a = plain.to_dict()["files"][0]
    b = found.to_dict()["files"][0]
    assert a["tokens"] == 100 and b["tokens"] == 200
    assert a["cost_usd"] > 0
    assert b["cost_usd"] == pytest.approx(a["cost_usd"] * 2)
    skills_a = {row["name"]: row for row in plain.to_dict()["skills"]}
    skills_b = {row["name"]: row for row in found.to_dict()["skills"]}
    assert skills_b["pdf"]["listing_tokens"] == round(len("- pdf: Read PDFs.") / 2.0)
    assert skills_a["pdf"]["listing_tokens"] == round(len("- pdf: Read PDFs.") / 4.0)


# -- ROB-P2: index_at (bisect) / cost (prefix sums) match the old O(k*T)
# algorithm exactly ----------------------------------------------------------
#
# Reference copies of the pre-ROB-P2 ``_Carry.index_at``/``cost``/``_rates``
# (context_files.py, before the bisect/prefix-sum/resolve-cache rewrite),
# kept here so a change to the optimised version can never silently drift
# from the algorithm it replaced. They read only public/already-existing
# ``_Carry`` attributes (``turns``, ``times``, ``rebuilt_ids``, ``pricing``),
# so they work unchanged against the current class.


def _reference_rates(carry: context_files._Carry, turn: Turn):
    if carry.pricing is None:
        return None
    resolved = carry.pricing.resolve_model(turn.model)
    return effective_rates(turn, resolved) if resolved is not None else None


def _reference_index_at(carry: context_files._Carry, ts: str | None) -> int:
    moment = context_files._parse_ts(ts)
    if moment is None:
        return 0
    for index, turn_time in enumerate(carry.times):
        if turn_time is not None and turn_time >= moment:
            return index
    return len(carry.turns)


def _reference_cost(carry: context_files._Carry, chars: int, start: int, end: int) -> float:
    if chars <= 0 or start >= end:
        return 0.0
    tokens_m = chars / calibration.FALLBACK / 1_000_000
    total = 0.0
    for offset, turn in enumerate(carry.turns[start:end]):
        rates = _reference_rates(carry, turn)
        if rates is None:
            continue
        if offset == 0 or turn.message_id in carry.rebuilt_ids:
            write_rate = rates.cache_write_1h if turn.cc_1h > turn.cc_5m else rates.cache_write_5m
            total += tokens_m * write_rate
        else:
            total += tokens_m * rates.cache_read
    return total


#: A few resolvable model ids (one that never gets a long-context rule at
#: these token counts) plus one unresolvable id, to exercise both the
#: priced and the "rates is None" branch.
_RANDOM_MODELS = ("claude-sonnet-5", "claude-opus-5-5", "claude-haiku-4-5", "not-a-real-model")


def _random_turns(rng: random.Random, n: int) -> list[Turn]:
    """``n`` turns with monotonically non-decreasing timestamps (an
    occasional ``ts=""`` gap), random models (including an unresolvable
    one), and enough high-``ctx``/low-``cache_read_tokens`` turns to make
    ``recache.detect`` flag some of them -- so the equivalence check below
    exercises every branch of the old ``cost`` loop (first-turn write,
    rebuilt write, plain read, unpriced skip)."""
    turns = []
    moment = datetime(2026, 9, 18, 0, 0, 0, tzinfo=timezone.utc)
    for i in range(1, n + 1):
        moment += timedelta(seconds=rng.choice((0, 0, 20, 45)))
        ts = "" if rng.random() < 0.1 else moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        recache_shape = rng.random() < 0.15
        turns.append(
            Turn(
                message_id=f"msg_{i:05d}",
                request_id=f"req_{i:05d}",
                turn_index=i,
                ts=ts,
                model=rng.choice(_RANDOM_MODELS),
                cc_5m=rng.choice((0, 100, 500)),
                cc_1h=rng.choice((0, 0, 400)),
                ctx=30_000 if recache_shape else rng.choice((0, 500, 5_000)),
                cache_read_tokens=100 if recache_shape else rng.choice((0, 100, 5_000)),
            )
        )
    return turns


def test_index_at_and_cost_match_the_old_algorithm_on_fixtures():
    pricing = load_pricing()
    turns = [
        Turn(message_id="a", turn_index=1, ts="2026-09-18T12:00:00.000Z", model="claude-sonnet-5", cc_5m=100),
        Turn(message_id="b", turn_index=2, ts="2026-09-18T12:01:00.000Z", model="claude-sonnet-5", cache_read_tokens=100),
        Turn(message_id="c", turn_index=3, ts="", model="claude-sonnet-5", cache_read_tokens=100),
        Turn(
            message_id="d", turn_index=4, ts="2026-09-18T12:03:00.000Z", model="claude-sonnet-5",
            ctx=30_000, cache_read_tokens=50,
        ),
        Turn(message_id="e", turn_index=5, ts="2026-09-18T12:04:00.000Z", model="not-a-real-model"),
    ]
    carry = context_files._Carry(TranscriptResult(turns=turns), pricing)
    for ts in (None, "", "2026-09-18T11:59:00.000Z", "2026-09-18T12:01:00.000Z", "2026-09-18T12:02:30.000Z", "2026-09-18T13:00:00.000Z"):
        assert carry.index_at(ts) == _reference_index_at(carry, ts)
    for start in range(0, 6):
        for end in range(0, 6):
            for chars in (0, 400, 40_000):
                assert carry.cost(chars, start, end) == pytest.approx(
                    _reference_cost(carry, chars, start, end), abs=1e-12
                )


@pytest.mark.parametrize("seed", range(8))
def test_index_at_and_cost_match_the_old_algorithm_on_random_sessions(seed):
    pricing = load_pricing()
    rng = random.Random(seed)
    turns = _random_turns(rng, 120)
    carry = context_files._Carry(TranscriptResult(turns=turns), pricing)

    # A spread of timestamps, including some between turns and one before
    # and after the whole session, and the fixture's own "" for unknown.
    base = datetime(2026, 9, 18, 0, 0, 0, tzinfo=timezone.utc)
    offsets = sorted({rng.randint(-300, 6_000) for _ in range(20)})
    for offset in (*offsets, None):
        ts = None if offset is None else (base + timedelta(seconds=offset)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        assert carry.index_at(ts) == _reference_index_at(carry, ts)

    for _ in range(200):
        start = rng.randint(0, len(turns))
        end = rng.randint(0, len(turns) + 5)
        chars = rng.choice((0, -10, 1, 400, 12_345, 1_000_000))
        got = carry.cost(chars, start, end)
        want = _reference_cost(carry, chars, start, end)
        assert got == pytest.approx(want, abs=1e-12)


def test_carry_scales_to_a_3000_turn_session_fast(tmp_path):
    """ROB-P2: a 3,000-turn session queries ``_Carry`` once per file/skill
    send it replays (``context_files._add_files``/``_add_skills``) --
    hundreds of ``index_at``/``cost`` calls against the same instance. The
    old O(T)-per-call algorithm was still fast enough for one call, but
    slow enough across hundreds of them on a session this size to show up
    in ``SURV-9``'s post-parse profile; this is a generous bound (a
    healthy machine finishes in well under a second), not a tight one."""
    pricing = load_pricing()
    rng = random.Random(0)
    turns = _random_turns(rng, 3_000)

    start = time.perf_counter()
    carry = context_files._Carry(TranscriptResult(turns=turns), pricing)
    for i in range(500):
        ts_index = (i * 7) % len(turns)
        idx = carry.index_at(turns[ts_index].ts)
        carry.cost(1_000 + i, idx, min(idx + 50, len(turns)))
    elapsed = time.perf_counter() - start

    assert elapsed < 5.0


# -- project files: files agents read, standing reads, sizes over time ----------------------------

#: A Monday, so a "week" in these tests is easy to read off the date.
MONDAY = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
CONTEXT_MD = "C:/proj/docs/context.md"


def _iso(days: float = 0, minutes: float = 0) -> str:
    return (MONDAY + timedelta(days=days, minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _read_session(
    tmp_path: Path,
    name: str,
    reads: list[list[tuple[str, int]]],
    *,
    agent_type: str | None = None,
    days: float = 0.0,
    compact_after: int | None = None,
    failed: bool = False,
    loaded: list[tuple[str, int]] | None = None,
):
    """A transcript with one assistant reply a minute per entry of
    ``reads``; each entry lists the ``(path, characters)`` the reply read
    with the Read tool. ``compact_after`` puts a compaction after that
    reply, ``failed`` makes every read fail, ``loaded`` is what Claude Code
    loaded as instructions."""
    lines = []
    if loaded:
        lines.append(
            {
                **attachment_line(
                    "instructions",
                    files=[{"path": path, "type": "Project", "content": "x" * chars} for path, chars in loaded],
                ),
                "timestamp": _iso(days),
            }
        )
    for index, wanted in enumerate(reads):
        content = [{"type": "text", "text": "ok"}]
        for position, (path, _chars) in enumerate(wanted):
            content.append(tool_use_block("Read", f"{name}_{index}_{position}", {"file_path": path}))
        lines.append(turn_line(content=content, cache_read_input_tokens=1000, timestamp=_iso(days, index + 1)))
        if wanted:
            results = [
                tool_result_block(
                    f"{name}_{index}_{position}",
                    "File not found" if failed else "x" * chars,
                    **({"is_error": True} if failed else {}),
                )
                for position, (_path, chars) in enumerate(wanted)
            ]
            lines.append(user_block_line(results, timestamp=_iso(days, index + 1.2)))
        if compact_after == index:
            lines.append(system_line("compact_boundary", timestamp=_iso(days, index + 1.5)))
    path = tmp_path / f"{name}.jsonl"
    write_jsonl(path, lines)
    return parse_transcript(path, TranscriptMeta(path=str(path), agent_type=agent_type))


def _quiet_runs(tmp_path: Path, stats, reach: str, count: int, prefix: str = "q") -> None:
    """``count`` runs of ``reach`` that read nothing."""
    pricing = load_pricing()
    for index in range(count):
        result = _read_session(tmp_path, f"{prefix}{reach}{index}", [[], [], []], agent_type=reach)
        stats.add(result, pricing, is_main=False)


def test_a_file_agents_read_gets_its_reach_reads_size_and_cost_without_the_path(tmp_path):
    pricing = load_pricing()
    stats = context_files.ContextFileStats()
    sizes = ([4000], [4000, 8000], [4000])
    for index, run in enumerate(sizes):
        reads = [[(CONTEXT_MD, chars)] for chars in run] + [[], []]
        stats.add(_read_session(tmp_path, f"e{index}", reads, agent_type="Explore"), pricing, is_main=False)
    stats.add(_read_session(tmp_path, "m", [[(CONTEXT_MD, 4000)], [], []]), pricing, is_main=True)
    data = stats.to_dict()

    [row] = data["reads"]
    assert row["hash"] == parse.path_hash(CONTEXT_MD, SALT)
    assert row["source"] == "read"
    assert row["reach"] == {"Explore": 3, "main": 1}
    assert row["reads"] == {"Explore": 4, "main": 1}
    # 24,000 characters over 5 reads: 4,800 each, 1,200 tokens.
    assert row["tokens"] == 1200
    assert row["read_tokens"] == {"Explore": 5000, "main": 1000}
    assert row["cost_usd"] > 0
    assert set(row["cost_by_reach"]) == {"Explore", "main"}
    # The largest read of the week stands for the file's size that week.
    assert row["weekly"] == {"2026-09-14": 2000}
    assert row["last_seen"].startswith("2026-09-14")
    # A read is not an auto-loaded file: the CLAUDE.md prices never see it.
    assert data["files"] == []
    assert "context.md" not in repr(data) and "proj" not in repr(data)


def test_standing_reads_count_per_reach_and_only_for_a_reach_with_runs_to_judge(tmp_path):
    pricing = load_pricing()
    stats = context_files.ContextFileStats()
    for index in range(3):
        reads = [[(CONTEXT_MD, 4000)], [], []]
        stats.add(_read_session(tmp_path, f"e{index}", reads, agent_type="Explore"), pricing, is_main=False)
    stats.add(_read_session(tmp_path, "p0", [[(CONTEXT_MD, 4000)], [], []], agent_type="Plan"), pricing, is_main=False)
    data = stats.to_dict()

    # Explore read it in all 3 runs; Plan had one run, too few to call a habit.
    assert data["standing"] == {"Explore": {"runs": 3, "files": 1, "tokens_per_run": 1000.0}}
    [row] = data["reads"]
    assert row["reach"] == {"Explore": 3, "Plan": 1}


def test_a_read_in_one_run_of_many_is_not_a_standing_read(tmp_path):
    pricing = load_pricing()
    stats = context_files.ContextFileStats()
    stats.add(_read_session(tmp_path, "r", [[(CONTEXT_MD, 4000)], [], []], agent_type="Explore"), pricing, is_main=False)
    _quiet_runs(tmp_path, stats, "Explore", 5)
    data = stats.to_dict()

    assert data["transcripts"] == {"Explore": 6}
    assert data["reads"] == [] and data["standing"] == {}


def test_a_read_in_a_fifth_of_a_types_runs_is_standing_but_not_a_tiny_type(tmp_path):
    pricing = load_pricing()
    stats = context_files.ContextFileStats()
    stats.add(_read_session(tmp_path, "r", [[(CONTEXT_MD, 4000)], [], []], agent_type="Explore"), pricing, is_main=False)
    _quiet_runs(tmp_path, stats, "Explore", 3)
    stats.add(_read_session(tmp_path, "x", [[(CONTEXT_MD, 4000)], [], []], agent_type="Plan"), pricing, is_main=False)
    _quiet_runs(tmp_path, stats, "Plan", 1)
    data = stats.to_dict()

    # 1 of 4 Explore runs is 25%; 1 of 2 Plan runs is half, but 2 runs are too few.
    assert data["standing"] == {"Explore": {"runs": 4, "files": 1, "tokens_per_run": 250.0}}
    assert data["reads"][0]["reach"] == {"Explore": 1, "Plan": 1}


@pytest.mark.parametrize(
    ("runs", "type_runs", "expected"),
    [
        (3, 100, True),
        (2, 10, True),
        (1, 10, False),
        (1, 4, True),
        (1, 6, False),
        (2, 2, False),
        (1, 2, False),
        (0, 5, False),
    ],
)
def test_is_standing_is_three_runs_or_a_fifth_of_them_with_three_runs_to_judge(runs, type_runs, expected):
    assert context_files.is_standing(runs, type_runs) is expected


def test_a_small_standing_read_counts_toward_the_reach_but_gets_no_row(tmp_path):
    pricing = load_pricing()
    stats = context_files.ContextFileStats()
    for index in range(3):
        stats.add(
            _read_session(tmp_path, f"e{index}", [[(CONTEXT_MD, 400)], [], []], agent_type="Explore"),
            pricing,
            is_main=False,
        )
    data = stats.to_dict()

    assert data["reads"] == []
    assert data["standing"] == {"Explore": {"runs": 3, "files": 1, "tokens_per_run": 100.0}}


def test_a_failed_read_put_nothing_in_context_and_is_left_out(tmp_path):
    pricing = load_pricing()
    stats = context_files.ContextFileStats()
    result = _read_session(tmp_path, "bad", [[(CONTEXT_MD, 4000)], [], []], failed=True)
    assert [list(turn.read_target_chars) for turn in result.turns if turn.read_target_hashes] == [[0]]
    stats.add(result, pricing, is_main=True)

    assert stats.reads == {}
    assert stats.to_dict()["reads"] == []


def test_a_read_is_carried_until_the_conversation_is_summarised(tmp_path):
    """The read lands after reply 1 of 6 and is paid for on replies 2 to 6,
    or only on 2 and 3 when a compaction came after reply 3."""
    pricing = load_pricing()
    reads = [[(CONTEXT_MD, 8000)], [], [], [], [], []]
    whole = _read_session(tmp_path, "whole", reads)
    cut = _read_session(tmp_path, "cut", reads, compact_after=2)

    plain = context_files.ContextFileStats()
    plain.add(whole, pricing, is_main=True)
    short = context_files.ContextFileStats()
    short.add(cut, pricing, is_main=True)
    key = parse.path_hash(CONTEXT_MD, SALT)

    carry = context_files._Carry(whole, pricing)
    assert plain.reads[key].cost_usd == pytest.approx(carry.cost(8000, 1, 6))
    assert short.reads[key].cost_usd == pytest.approx(carry.cost(8000, 1, 3))
    assert 0 < short.reads[key].cost_usd < plain.reads[key].cost_usd


def test_a_read_in_the_last_reply_is_counted_but_costs_nothing_more(tmp_path):
    stats = context_files.ContextFileStats()
    stats.add(_read_session(tmp_path, "last", [[], [(CONTEXT_MD, 8000)]]), load_pricing(), is_main=True)
    acc = stats.reads[parse.path_hash(CONTEXT_MD, SALT)]
    assert acc.reads == {"main": 1} and acc.cost_usd == 0


def test_a_read_costs_nothing_without_pricing(tmp_path):
    stats = context_files.ContextFileStats()
    stats.add(_read_session(tmp_path, "free", [[(CONTEXT_MD, 8000)], [], []]), None, is_main=True)
    assert stats.reads[parse.path_hash(CONTEXT_MD, SALT)].cost_usd == 0


def test_a_file_loaded_and_read_is_one_hash_and_a_read_never_changes_the_loaded_prices(tmp_path):
    """The same path is the same hash whether Claude Code loaded it or an
    agent read it: the loaded file keeps its own row and price."""
    pricing = load_pricing()
    shared = "C:/proj/CLAUDE.md"
    with_read = context_files.ContextFileStats()
    with_read.add(
        _read_session(tmp_path, "a", [[(shared, 4000)], [], []], loaded=[(shared, 4000)]), pricing, is_main=True
    )
    without = context_files.ContextFileStats()
    without.add(_read_session(tmp_path, "b", [[], [], []], loaded=[(shared, 4000)]), pricing, is_main=True)

    assert with_read.to_dict()["files"] == without.to_dict()["files"]
    assert set(with_read.reads) == set(with_read.files) == {parse.path_hash(shared, SALT)}


def test_the_window_runs_from_the_first_to_the_last_reply_with_a_floor_of_a_week(tmp_path):
    pricing = load_pricing()
    stats = context_files.ContextFileStats()
    assert stats.window_days() == context_files.MIN_WINDOW_DAYS
    stats.add(_read_session(tmp_path, "one", [[], []], days=0), pricing, is_main=True)
    assert stats.window_days() == context_files.MIN_WINDOW_DAYS
    stats.add(_read_session(tmp_path, "two", [[], []], days=30), pricing, is_main=True)
    data = stats.to_dict()
    assert data["window_days"] == pytest.approx(30.0, abs=0.1)
    assert data["newest"] == "2026-10-14"


# -- weekly sizes and the change over about 30 days ----------------------------------------------


def test_weekly_series_is_one_value_per_week_carrying_a_quiet_week_forward():
    weekly = {"2026-09-07": 1000, "2026-09-21": 1500}
    # 09-30 falls in the week of Monday 09-28.
    assert context_files.weekly_series(weekly, "2026-09-30") == [1000, 1000, 1500, 1500]


def test_weekly_series_keeps_the_last_thirteen_weeks_and_starts_from_the_size_before_them():
    weekly = {"2026-01-05": 700, "2026-09-28": 900}
    series = context_files.weekly_series(weekly, "2026-09-30")
    assert len(series) == context_files.SERIES_WEEKS
    assert series[0] == 700 and series[-1] == 900


def test_weekly_series_is_empty_with_no_sizes_or_no_newest_day():
    assert context_files.weekly_series({}, "2026-09-30") == []
    assert context_files.weekly_series({"2026-09-07": 1000}, "") == []


def test_size_change_compares_the_newest_week_with_the_largest_size_three_to_seven_weeks_back():
    weekly = {"2026-08-24": 2000, "2026-09-07": 5000, "2026-09-28": 8000}
    # The newest week is Monday 09-28; "then" is the largest of 08-17..09-07.
    assert context_files.size_change(weekly, "2026-09-30") == {"now": 8000, "then": 5000, "change_pct": 60.0}


def test_size_change_has_no_baseline_without_a_week_in_the_look_back_range():
    # 09-14 is only two weeks back and 08-10 is seven: neither is in range.
    weekly = {"2026-08-10": 2000, "2026-09-14": 3000, "2026-09-28": 8000}
    assert context_files.size_change(weekly, "2026-09-30") == {"now": 8000, "then": None, "change_pct": None}


def test_size_change_reports_no_change_for_a_file_not_seen_in_the_newest_two_weeks():
    weekly = {"2026-08-31": 5000, "2026-09-14": 6000}
    # Last seen two weeks before the newest week: the size is what it was then.
    assert context_files.size_change(weekly, "2026-09-30") == {"now": 6000, "then": None, "change_pct": None}
    # One week before is still current.
    weekly = {"2026-08-31": 5000, "2026-09-21": 6000}
    assert context_files.size_change(weekly, "2026-09-30")["change_pct"] == 20.0


def test_size_change_with_no_sizes_or_no_newest_day():
    assert context_files.size_change({}, "2026-09-30") == {"now": 0, "then": None, "change_pct": None}
    assert context_files.size_change({"2026-09-28": 800}, "") == {"now": 800, "then": None, "change_pct": None}


# -- the unified file view -----------------------------------------------------------------------

WEEKS_UP = {"2026-08-31": 5000, "2026-09-28": 8000}


def _data(files=(), reads=(), **over) -> dict:
    base = {
        "transcripts": {"main": 10, "Explore": 10, "Plan": 10, "Review": 10},
        "window_days": 30.0,
        "newest": "2026-09-30",
        "files": list(files),
        "reads": list(reads),
    }
    return {**base, **over}


def _auto(file_hash: str, tokens: int = 1000, **over) -> dict:
    return {
        "hash": file_hash,
        "type": "Project",
        "scoped": False,
        "tokens": tokens,
        "sends": 10,
        "reach": {"main": 10, "Explore": 10},
        "cost_usd": 3.0,
        "weekly": {},
        "last_seen": "2026-09-30T10:00:00.000Z",
        **over,
    }


def _read(file_hash: str, tokens: int = 1000, **over) -> dict:
    return {
        "hash": file_hash,
        "source": "read",
        "tokens": tokens,
        "reach": {"Explore": 5, "Plan": 4},
        "cost_usd": 3.0,
        "weekly": {},
        "last_seen": "2026-09-30T10:00:00.000Z",
        **over,
    }


def test_project_files_labels_how_each_file_arrives():
    data = _data(files=[_auto("aaaa"), _auto("bbbb")], reads=[_read("cccc"), _read("dddd")])
    rows = {row["hash"]: row for row in context_files.project_files(data, {"imports": ["bbbb", "dddd"]})}
    assert {h: row["source"] for h, row in rows.items()} == {
        "aaaa": "auto",
        "bbbb": "import",
        "cccc": "read",
        "dddd": "import",
    }
    assert all(row["source"] in context_files.SOURCES for row in rows.values())


def test_project_files_leaves_out_the_managed_policy_file_and_files_too_small_to_matter():
    data = _data(files=[_auto("aaaa", type="Managed"), _auto("bbbb", tokens=499)], reads=[_read("cccc", tokens=499)])
    assert context_files.project_files(data) == []


def test_project_files_merges_a_file_that_is_loaded_and_read_under_the_loaded_row():
    data = _data(
        files=[_auto("aaaa", reach={"main": 10, "Explore": 2}, cost_usd=3.0)],
        reads=[_read("aaaa", reach={"Explore": 6, "Plan": 5}, cost_usd=1.5)],
    )
    [row] = context_files.project_files(data)
    assert row["source"] == "auto" and row["type"] == "Project"
    assert {item["reach"]: item["runs"] for item in row["reach"]} == {"main": 10, "Explore": 6, "Plan": 5}
    assert row["cost_usd"] == pytest.approx(4.5)


def test_project_files_gives_the_size_now_the_change_a_sparkline_and_a_monthly_cost():
    data = _data(reads=[_read("aaaa", tokens=2000, weekly=WEEKS_UP, cost_usd=6.0)], window_days=60.0)
    [row] = context_files.project_files(data)
    assert (row["tokens"], row["then"], row["change_pct"]) == (8000, 5000, 60.0)
    assert row["series"] == [5000, 5000, 5000, 5000, 8000]
    assert row["cost_usd"] == 6.0
    assert row["cost_month_usd"] == pytest.approx(3.0)


def test_project_files_reach_is_the_share_of_each_reachs_runs_with_main_first():
    data = _data(
        reads=[_read("aaaa", reach={"Explore": 5, "Plan": 10, "main": 1, "Review": 1})],
        transcripts={"main": 10, "Explore": 10, "Plan": 10, "Review": 10},
    )
    [row] = context_files.project_files(data)
    assert [(item["reach"], item["runs"], item["share"], item["standing"]) for item in row["reach"]] == [
        ("main", 1, 0.1, False),
        ("Plan", 10, 1.0, True),
        ("Explore", 5, 0.5, True),
        ("Review", 1, 0.1, False),
    ]
    # Standing agent types only: the main session is not an agent type.
    assert row["types"] == 2


def test_project_files_puts_text_files_first_then_the_dearest():
    data = _data(
        reads=[_read("code", cost_usd=9.0), _read("note", cost_usd=1.0), _read("plan", cost_usd=2.0)],
    )
    names = {
        "code": {"name": "src/app.py", "ext": "code", "project": "proj"},
        "note": {"name": "notes.md", "ext": "md", "project": "proj"},
        "plan": {"name": "plan.txt", "ext": "txt", "project": "proj"},
    }
    rows = context_files.project_files(data, {"names": names})
    assert [row["name"] for row in rows] == ["plan.txt", "notes.md", "src/app.py"]
    assert rows[0]["project"] == "proj" and rows[0]["ext"] == "txt"


def test_dearest_keeps_the_dearest_rows_in_the_lists_own_order():
    rows = [{"hash": f"m{i}", "ext": "md", "cost_month_usd": 0.01 * (i + 1)} for i in range(3)]
    rows.append({"hash": "j", "ext": "json", "cost_month_usd": 100.0})
    # The list puts text files first, so a plain slice would drop the dear json file.
    assert [row["hash"] for row in rows[:2]] == ["m0", "m1"]
    assert [row["hash"] for row in context_files.dearest(rows, 2)] == ["m2", "j"]
    assert context_files.dearest(rows, 10) == rows


def test_project_files_reach_carries_the_mean_size_of_one_read_and_the_row_keeps_no_read_counts():
    data = {
        "files": [{"hash": "a", "type": "Project", "tokens": 6000, "reach": {"main": 3}, "cost_usd": 1.0}],
        "reads": [
            {
                "hash": "r",
                "tokens": 10000,
                "reach": {"Explore": 5},
                "reads": {"Explore": 10},
                "read_tokens": {"Explore": 100000},
                "cost_usd": 1.0,
            }
        ],
        "transcripts": {"main": 3, "Explore": 5},
        "newest": "2026-09-28",
        "window_days": 30,
    }
    rows = {row["hash"]: row for row in context_files.project_files(data)}
    assert {item["reach"]: item["mean_tokens"] for item in rows["r"]["reach"]} == {"Explore": 10000}
    assert {item["reach"]: item["mean_tokens"] for item in rows["a"]["reach"]} == {"main": None}
    assert not [row for row in rows.values() if "reads" in row or "read_tokens" in row]


def test_a_reach_that_only_had_a_file_loaded_has_no_mean_read_size_even_when_another_reach_read_it():
    data = _data(
        files=[_auto("aaaa", reach={"main": 10, "Explore": 2})],
        reads=[_read("aaaa", reach={"Plan": 5}, reads={"Plan": 5}, read_tokens={"Plan": 15000})],
    )
    [row] = context_files.project_files(data)
    assert {item["reach"]: item["mean_tokens"] for item in row["reach"]} == {"main": None, "Explore": None, "Plan": 3000}


def test_project_files_rows_have_no_name_without_local_names():
    [row] = context_files.project_files(_data(reads=[_read("aaaa")]))
    assert (row["name"], row["ext"], row["project"]) == ("", "", "")


def test_an_import_with_no_entry_of_its_own_is_worked_out_from_the_file_that_imports_it():
    """Assumption: an imported file has its own entry; if it does not, its
    size comes from disk and its readers and a share of the cost from the
    file that imports it."""
    parent = _auto("pppp", tokens=3000, cost_usd=6.0, reach={"main": 10, "Explore": 8})
    local = {"imports": ["iiii"], "inlined": {"iiii": {"parent": "pppp", "tokens": 1500}}}
    rows = {row["hash"]: row for row in context_files.project_files(_data(files=[parent]), local)}
    row = rows["iiii"]
    assert row["source"] == "import" and row["tokens"] == 1500
    assert row["cost_usd"] == pytest.approx(3.0)
    assert {item["reach"]: item["runs"] for item in row["reach"]} == {"main": 10, "Explore": 8}
    # An import the transcripts do have an entry for keeps that entry.
    own = _auto("iiii", tokens=900, cost_usd=1.0)
    rows = {row["hash"]: row for row in context_files.project_files(_data(files=[parent, own]), local)}
    assert rows["iiii"]["tokens"] == 900 and rows["iiii"]["cost_usd"] == 1.0
    # With no parent to take it from, there is no row.
    assert [row["hash"] for row in context_files.project_files(_data(), local)] == []


# -- the Overview check's thresholds -------------------------------------------------------------


def _row(**over) -> dict:
    return {
        "hash": "aaaa",
        "source": "read",
        "tokens": 6000,
        "types": 3,
        "change_pct": None,
        "cost_month_usd": 5.0,
        **over,
    }


def test_a_file_of_five_thousand_tokens_read_by_three_agent_types_is_flagged_wide():
    assert [r["reasons"] for r in context_files.check_rows([_row(tokens=5000, types=3)])] == [["wide"]]
    assert context_files.check_rows([_row(tokens=4999, types=3)]) == []
    assert context_files.check_rows([_row(tokens=9000, types=2)]) == []


def test_a_file_that_grew_a_quarter_in_thirty_days_is_flagged_if_it_is_two_thousand_tokens_now():
    assert [r["reasons"] for r in context_files.check_rows([_row(tokens=2000, types=0, change_pct=25.0)])] == [["grew"]]
    assert context_files.check_rows([_row(tokens=2000, types=0, change_pct=24.9)]) == []
    # A small file that doubled is still small.
    assert context_files.check_rows([_row(tokens=1999, types=0, change_pct=100.0)]) == []
    assert context_files.check_rows([_row(tokens=9000, types=0, change_pct=None)]) == []


def test_a_wide_file_that_also_grew_says_both_and_the_dearest_comes_first():
    rows = [
        _row(hash="cheap", tokens=6000, types=3, change_pct=60.0, cost_month_usd=1.0),
        _row(hash="dear", tokens=6000, types=4, cost_month_usd=9.0),
    ]
    flagged = context_files.check_rows(rows)
    assert [(r["hash"], r["reasons"]) for r in flagged] == [("dear", ["wide"]), ("cheap", ["wide", "grew"])]


def test_files_claude_code_loads_itself_are_the_claude_md_checks_not_this_ones():
    big = _row(source="auto", tokens=9000, types=5, change_pct=80.0)
    assert context_files.check_rows([big]) == []
    # Unless the caller cannot yet tell an import from a loaded file.
    assert len(context_files.check_rows([big], maybe_imports=True)) == 1
    assert len(context_files.check_rows([{**big, "source": "import"}])) == 1


# -- the starting-context stack ------------------------------------------------------------------


def _startup(*rows) -> Table:
    return Table(
        name="agent_startup_breakdown",
        title="What each subagent is given at startup",
        columns=[
            Column(key="agent_type", label="Agent type", kind="str"),
            Column(key="spawns", label="Spawns", kind="int"),
            Column(key="startup_tokens", label="Startup size", kind="tokens"),
            Column(key="claude_md", label="CLAUDE.md and memory", kind="tokens"),
            Column(key="task_prompt", label="Task prompt", kind="tokens"),
        ],
        rows=[list(row) for row in rows],
    )


def test_the_stack_splits_a_first_call_into_system_files_standing_reads_and_the_brief():
    startup = _startup(("Explore", 8, 20000.0, 3000.0, 500.0), ("Plan", 4, 15000.0, 0.0, 200.0))
    data = {"standing": {"Explore": {"runs": 8, "files": 2, "tokens_per_run": 4000.0}}}
    table = context_files.build_stack_table(data, startup)

    assert table.name == "agent_startup_stack"
    keys = [column.key for column in table.columns]
    assert keys == [
        "agent_type", "spawns", "system_tools", "auto_files", "standing_reads", "brief", "total", "standing_files",
    ]
    rows = {row[0]: dict(zip(keys, row)) for row in table.rows}
    assert rows["Explore"]["system_tools"] == 16500.0
    assert (rows["Explore"]["auto_files"], rows["Explore"]["standing_reads"], rows["Explore"]["brief"]) == (
        3000.0, 4000.0, 500.0,
    )
    assert rows["Explore"]["total"] == 24000.0 and rows["Explore"]["standing_files"] == 2
    # An agent type with no habits has no standing reads.
    assert rows["Plan"]["standing_reads"] == 0.0 and rows["Plan"]["standing_files"] == 0


def test_the_stack_never_shows_a_negative_system_part():
    startup = _startup(("Odd", 3, 1000.0, 900.0, 400.0))
    [row] = context_files.build_stack_table({}, startup).rows
    assert row[2] == 0.0


def test_the_stack_has_no_table_without_a_startup_table():
    assert context_files.build_stack_table({}, None) is None
    assert context_files.build_stack_table({}, _startup()) is None


def test_the_stack_table_has_help_on_every_column():
    from claudeglass import helptext
    from claudeglass.model import Section

    startup = _startup(("Explore", 8, 20000.0, 3000.0, 500.0))
    table = context_files.build_stack_table({}, startup)
    section = Section(key="agent_startup", title="Agent startup", tables=[table])
    helptext.annotate_section(section)
    assert table.help and table.help.shows
    assert all(column.help for column in table.columns)

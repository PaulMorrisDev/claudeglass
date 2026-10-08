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
from claudeglass.model import TranscriptMeta, TranscriptResult, Turn
from claudeglass.parse import parse_transcript
from claudeglass.pricing import effective_rates, load_pricing

from helpers import attachment_line, tool_use_block, turn_line, write_jsonl

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

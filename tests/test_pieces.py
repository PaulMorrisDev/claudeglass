"""Pieces of work (``pieces.py``): where a piece starts and ends, which
cycles in it were rework and why, what it cost, and the rates that
normalise rework.

Most tests build cycles from ``Turn`` objects directly, so each fact a rule
reads is set by name. The chains that charge a reply to an agent's report to
the cycle that started the agent are parsed from real transcripts. Amounts
use ``tests/fixtures/pricing_min.toml``'s ``claude-widget-9``: a reply of 100
input and 50 output tokens costs ``REPLY`` USD (input 1.0 and output 2.0 per
million tokens).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from claudeglass import capture, parse, pieces
from claudeglass.capture import Cycle
from claudeglass.model import CaptureTag, Feedback, PlanCheck, PlanStats, Turn
from claudeglass.pieces import PieceSession, WorkPiece, pieces_in, pieces_of
from claudeglass.pricing import load_pricing

from helpers import tool_result_block, tool_use_block, user_block_line
from test_capture import (
    TAG_IDS,
    _agent_that_reports,
    _ask,
    _background,
    _launched,
    _note,
    _reply,
    _report,
    _top,
    _ts,
    _wf_agent,
    _wf_call,
    _wf_launched,
    _wf_steps,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MODEL = "claude-widget-9"
#: What one reply of 100 input and 50 output tokens costs.
REPLY = 100 / 1e6 * 1.0 + 50 / 1e6 * 2.0
BASE = datetime(2026, 9, 18, 9, 0, tzinfo=timezone.utc)
HOUR = 60


@pytest.fixture(autouse=True)
def _salt():
    parse.set_salt(b"p" * 32)


@pytest.fixture()
def pricing():
    return load_pricing(path=FIXTURES / "pricing_min.toml")


def _at(minutes: float, base: datetime = BASE) -> str:
    return (base + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _tag(**words) -> CaptureTag:
    return CaptureTag(has_tl=True, **words)


def _turn(minutes: float, *, files=(), reads=(), tag=None, base: datetime = BASE, **kw) -> Turn:
    """One reply: it edits ``files`` and reads ``reads`` (names stand in for
    the salted hashes), and carries what the message before it said."""
    kw.setdefault("human_prompt_chars", 200)
    if files:
        kw.setdefault("edit_call_count", len(files))
        kw.setdefault("edit_kind", "real")
    return Turn(
        turn_index=1,
        ts=_at(minutes, base),
        model=MODEL,
        input_tokens=100,
        output_tokens=50,
        edit_target_hashes=tuple(files),
        read_target_hashes=tuple(reads),
        cap=tag,
        **kw,
    )


def _cycle(*turns: Turn) -> Cycle:
    return Cycle(start=0, end=len(turns), turns=list(turns))


def _msg(minutes: float, **kw) -> Cycle:
    """A message answered by one reply."""
    return _cycle(_turn(minutes, **kw))


def _build(minutes: float = 10, files=("a", "b")) -> Cycle:
    return _msg(minutes, files=files, human_prompt_chars=400)


def _fix(minutes: float, files=("a",), **kw) -> Cycle:
    """A short message that changes the files the cycle before it did. It is
    no rework on that alone: ``kw`` adds the correction, adjustment or tag
    that makes it one."""
    kw.setdefault("human_prompt_chars", 40)
    return _msg(minutes, files=files, **kw)


def _run(minutes: float, fb: Feedback | None = None) -> Cycle:
    """A /cg-feedback run that gave ``fb``."""
    return _msg(minutes, commands_run=("cg-feedback",), feedback=fb or Feedback(source="answers"))


def _during(minutes: float, running=(0,), **kw) -> Cycle:
    """A message sent while background work ran: at its first reply the work
    cycle ``running`` started was still going (the cycle index in its
    session). It changes the files ``kw`` says, so it is no aside when it
    changes any."""
    cycle = _msg(minutes, **kw)
    cycle.running = tuple(running)
    return cycle


def _aside(minutes: float, running=(0,), **kw) -> Cycle:
    """A message sent while background work ran that asks for something and
    changes no files: a side question, or one that steers the running work."""
    return _during(minutes, running, **kw)


def _only(cycles, **kw) -> WorkPiece:
    [piece] = pieces_of(cycles, **kw)
    return piece


def _causes(piece: WorkPiece) -> dict[tuple[str, str], int]:
    return {(cause, source): n for cause, source, n in piece.causes}


# -- where a piece starts ----------------------------------------------------------


def test_a_session_with_no_other_start_is_one_unsegmented_piece_that_counts_its_substantive_cycles():
    cycles = [
        _msg(0),
        _build(10),
        _msg(20, human_go=True, human_prompt_chars=3),
        _msg(30, human_status=True, human_prompt_chars=12),
        _msg(40, human_ack=True, human_prompt_chars=6),
    ]
    piece = _only(cycles)
    assert (piece.cycles, piece.substantive) == (5, 2)
    assert piece.unsegmented and piece.confidence == "high"
    assert piece.cycle_ids == tuple(("", n) for n in range(5))
    assert (piece.start_ts, piece.end_ts) == (_at(0), _at(40))
    assert pieces_of([]) == []


def test_a_session_cut_into_pieces_has_none_of_them_unsegmented():
    new = _tag(shift="new")
    one = pieces_of([_msg(0), _msg(10)])
    two = pieces_of([_msg(0), _msg(10, tag=new)])
    assert [p.unsegmented for p in one] == [True]
    assert [p.unsegmented for p in two] == [False, False]


def test_a_settled_shift_new_on_a_message_that_asks_for_something_starts_a_piece():
    first, second = pieces_of([_msg(0), _build(10), _msg(30, files=("c",), tag=_tag(shift="new"))])
    assert (first.cycles, second.cycles) == (2, 1)
    assert first.confidence == second.confidence == "high"
    assert second.start_ts == _at(30)


@pytest.mark.parametrize("flag", ["human_go", "human_status"])
def test_a_shift_new_on_a_go_ahead_or_a_status_check_starts_nothing(flag):
    cycles = [_msg(0), _msg(10, tag=_tag(shift="new"), human_prompt_chars=5, **{flag: True})]
    assert [p.cycles for p in pieces_of(cycles)] == [2]


def test_a_shift_new_on_the_first_message_of_a_session_is_not_a_second_piece():
    assert [p.cycles for p in pieces_of([_msg(0, tag=_tag(shift="new")), _msg(10)])] == [2]


def test_a_clear_starts_a_piece_unless_the_message_after_it_is_a_handoff():
    short = pieces_of([_build(0), _msg(30, commands_run=("clear",), human_prompt_chars=80)])
    assert [p.cycles for p in short] == [1, 1] and short[1].confidence == "high"
    # A long message or a paste that picks up the piece's files carries it on.
    pasted = pieces_of([_build(0), _msg(30, commands_run=("clear",), human_prompt_chars=80, human_prompt_has_paste=True, reads=("a",))])
    long = pieces_of([_build(0), _msg(30, commands_run=("clear",), human_prompt_chars=pieces.HANDOFF_CHARS, files=("b",))])
    assert [p.cycles for p in pasted] == [2] and [p.cycles for p in long] == [2]
    # One that touches none of them is another job's.
    other = pieces_of([_build(0), _msg(30, commands_run=("clear",), human_prompt_chars=pieces.HANDOFF_CHARS, reads=("q",))])
    assert [p.cycles for p in other] == [1, 1]


def test_a_clear_then_a_message_naming_a_path_after_an_approved_plan_is_a_handoff():
    plan = _msg(0, plan_stats=PlanStats(outcome="approved"), prompt_plan_mode=True)
    named = _msg(30, commands_run=("clear",), human_prompt_chars=90, prompt_flags=("path",))
    assert [p.cycles for p in pieces_of([plan, _build(10), named])] == [3]
    # With no plan approved, a path alone is no handoff.
    assert [p.cycles for p in pieces_of([_build(10), named])] == [1, 1]


def _gap_pieces(gap_minutes=3 * HOUR, *, last=("a", "b"), now=("c", "d"), reads=(), tag=None, **kw):
    cycles = [_msg(0, files=last), _msg(gap_minutes, files=now, reads=reads, tag=tag, **kw)]
    return pieces_of(cycles)


def test_with_no_tag_a_long_gap_and_different_files_and_a_message_that_asks_start_a_low_confidence_piece():
    first, second = _gap_pieces()
    assert (first.cycles, second.cycles) == (1, 1)
    assert (first.confidence, second.confidence) == ("high", "low")


@pytest.mark.parametrize(
    "kw",
    [
        # The gap is a minute short of three hours.
        {"gap_minutes": 3 * HOUR - 1},
        # One side names a single file.
        {"now": ("c",)},
        {"last": ("a",)},
        # Some of the same files: Jaccard is 1/3, not below 0.1.
        {"now": ("a", "x")},
        # Read, not changed, still counts as a file it was about.
        {"now": ("c",), "reads": ("a", "b")},
        # A message that asks for nothing.
        {"human_go": True, "human_prompt_chars": 4},
        {"human_status": True, "human_prompt_chars": 9},
        # Any shift word at all hands the call to the tag.
        {"tag": _tag(shift="build")},
    ],
)
def test_the_tag_free_start_needs_all_three_conditions(kw):
    assert [p.cycles for p in _gap_pieces(**kw)] == [2]


def test_a_tag_free_start_judges_files_by_the_overlap_not_the_count():
    # Ten files in common out of eleven is one piece; one in twenty-one is two.
    many = tuple(f"f{n}" for n in range(10))
    assert [p.cycles for p in _gap_pieces(last=many + ("g",), now=many + ("h",))] == [2]
    wide = tuple(f"f{n}" for n in range(20))
    assert [p.cycles for p in _gap_pieces(last=wide, now=("f0",) + tuple(f"z{n}" for n in range(20)))] == [1, 1]


def test_a_gap_is_the_transcripts_own_reading_when_it_has_one():
    cycles = [_msg(0, files=("a", "b")), _msg(10, files=("c", "d"), gap_s=4 * 3600.0)]
    assert [p.cycles for p in pieces_of(cycles)] == [1, 1]
    cycles = [_msg(0, files=("a", "b")), _msg(600, files=("c", "d"), gap_s=60.0)]
    assert [p.cycles for p in pieces_of(cycles)] == [2]


def test_a_feedback_run_is_no_pieces_work_and_never_starts_one():
    piece = _only([_build(0), _run(5), _fix(10, human_correction=True)])
    assert (piece.cycles, piece.substantive, piece.rework) == (2, 2, 1)
    assert piece.cycle_ids == (("", 0), ("", 2))
    # It costs nothing here: it is what rates the work, not part of it.
    assert piece.replies == 2


def test_a_piece_of_nothing_but_a_feedback_run_is_none():
    assert pieces_of([_run(0)]) == []


# -- handoffs ------------------------------------------------------------------------


def _session(name, cycles, project="proj", **kw) -> PieceSession:
    return PieceSession(name, cycles, project, **kw)


def _later(minutes, **kw) -> Cycle:
    """A session's first cycle, ``minutes`` after the base time: a handoff
    message, long, that reads a file ``_build`` edited."""
    kw.setdefault("human_prompt_chars", 2500)
    kw.setdefault("reads", ("a",))
    return _cycle(_turn(minutes, **kw))


def test_a_session_that_opens_with_a_handoff_within_72_hours_joins_the_previous_piece():
    first = _session("a", [_build(0), _fix(10, files=("a",))])
    second = _session("b", [_cycle(_turn(2 * HOUR, human_prompt_chars=2500, files=("a",))), _msg(2 * HOUR + 10)])
    [piece] = pieces_in([first, second])
    assert piece.session_ids == ("a", "b")
    assert piece.cycle_ids == (("a", 0), ("a", 1), ("b", 0), ("b", 1))
    assert (piece.cycles, piece.replies) == (4, 4)
    assert (piece.start_ts, piece.end_ts) == (_at(0), _at(2 * HOUR + 10))


def test_a_handoff_joins_when_the_next_session_is_listed_first():
    first = _session("a", [_build(0)])
    second = _session("b", [_later(HOUR)])
    assert [p.session_ids for p in pieces_in([second, first])] == [("a", "b")]


@pytest.mark.parametrize(
    "gap_hours, joined",
    [(71.5, True), (72, True), (72.5, False), (200, False)],
)
def test_a_handoff_joins_only_within_72_hours_of_the_end_of_the_previous_piece(gap_hours, joined):
    first = _session("a", [_build(0)])
    second = _session("b", [_later(gap_hours * HOUR)])
    assert [len(p.session_ids) for p in pieces_in([first, second])] == ([2] if joined else [1, 1])


def test_a_session_that_does_not_open_with_a_handoff_stays_a_piece_of_its_own():
    first = _session("a", [_build(0)])
    second = _session("b", [_msg(HOUR, human_prompt_chars=300)])
    assert [p.session_ids for p in pieces_in([first, second])] == [("a",), ("b",)]


@pytest.mark.parametrize("project", ["other", ""])
def test_a_handoff_joins_only_the_same_known_project(project):
    first = _session("a", [_build(0)])
    second = _session("b", [_later(HOUR)], project=project)
    assert len(pieces_in([first, second])) == 2
    # An unknown project on both sides is no match either.
    assert len(pieces_in([_session("a", [_build(0)], ""), _session("b", [_later(HOUR)], "")])) == 2


def test_a_session_after_a_handoff_joins_the_last_piece_of_its_project_not_an_older_one():
    one = _session("a", [_build(0), _msg(100, files=("z", "y"), tag=_tag(shift="new"))])
    two = _session("b", [_later(3 * HOUR, reads=("z",))])
    pieces_ = pieces_in([one, two])
    assert [p.session_ids for p in pieces_] == [("a",), ("a", "b")]


def test_the_handoff_names_a_plan_file_after_an_approved_plan():
    plan = _msg(0, plan_stats=PlanStats(outcome="approved"), prompt_plan_mode=True)
    named = _cycle(_turn(HOUR, human_prompt_chars=90, prompt_flags=("path",)))
    assert len(pieces_in([_session("a", [plan, _build(10)]), _session("b", [named])])) == 1
    assert len(pieces_in([_session("a", [_build(10)]), _session("b", [named])])) == 2


def _head(**kw) -> Cycle:
    """A session's first cycle, an hour after the base time, whose message is
    a handoff only as ``kw`` makes it one."""
    kw.setdefault("human_prompt_chars", 90)
    return _cycle(_turn(HOUR, **kw))


def _joined(head: Cycle, *, plan: bool = False) -> bool:
    """Whether the session that opens with ``head`` joins the piece before
    it, which built ``a`` and ``b`` (after an approved plan when ``plan``)."""
    before = [_build(10)]
    if plan:
        before.insert(0, _msg(0, plan_stats=PlanStats(outcome="approved"), prompt_plan_mode=True))
    return len(pieces_in([_session("a", before), _session("b", [head])])) == 1


@pytest.mark.parametrize(
    "plan, kw, joined",
    [
        # A path joins after an approved plan, whatever the first cycle touched.
        (True, {}, True),
        (True, {"reads": ("q",)}, True),
        (True, {"human_prompt_chars": pieces.HANDOFF_CHARS - 1}, True),
        # Or when the first cycle reads or edits a file the piece edited.
        (False, {"reads": ("a",)}, True),
        (False, {"files": ("b",)}, True),
        # With no plan and no file in common it is a path to something else.
        (False, {}, False),
        (False, {"reads": ("q",)}, False),
        (False, {"files": ("q",), "reads": ("r",)}, False),
    ],
)
def test_a_path_joins_after_an_approved_plan_or_when_it_picks_up_a_file_the_piece_edited(plan, kw, joined):
    assert _joined(_head(prompt_flags=("path",), **kw), plan=plan) is joined


@pytest.mark.parametrize("plan", [False, True])
@pytest.mark.parametrize("flags", [(), ("path",)])
@pytest.mark.parametrize("long", [{"human_prompt_chars": pieces.HANDOFF_CHARS}, {"human_prompt_has_paste": True}])
def test_a_long_message_or_a_paste_joins_only_with_a_file_the_piece_edited(plan, flags, long):
    # No file in common: another job's paste, plan or no plan, paths named or not.
    assert not _joined(_head(prompt_flags=flags, **long), plan=plan)
    assert not _joined(_head(prompt_flags=flags, reads=("q",), files=("r",), **long), plan=plan)
    # One read or one edit in common carries the piece on.
    assert _joined(_head(prompt_flags=flags, reads=("a",), **long), plan=plan)
    assert _joined(_head(prompt_flags=flags, files=("b",), **long), plan=plan)


def test_a_long_paste_naming_paths_after_an_approved_plan_with_no_shared_file_is_a_new_piece():
    """The replay's wrong join: a pasted brief for different work that names
    paths, straight after a piece whose plan was approved."""
    paste = _head(prompt_flags=("path",), human_prompt_chars=4000, human_prompt_has_paste=True, reads=("q",))
    assert not _joined(paste, plan=True)
    [first, second] = pieces_in([
        _session("a", [_msg(0, plan_stats=PlanStats(outcome="approved"), prompt_plan_mode=True), _build(10)]),
        _session("b", [paste]),
    ])
    assert (first.session_ids, second.session_ids) == (("a",), ("b",))


def test_a_short_path_message_after_an_approved_plan_joins_and_a_long_paste_with_a_shared_file_does_too():
    short = _head(prompt_flags=("path",), human_prompt_chars=120, reads=("q",))
    assert _joined(short, plan=True)
    paste = _head(prompt_flags=("path",), human_prompt_chars=4000, human_prompt_has_paste=True, reads=("a",))
    assert _joined(paste, plan=True) and _joined(paste, plan=False)


def test_a_short_message_with_no_path_is_no_handoff_even_when_it_reads_the_pieces_files():
    assert not _joined(_head(reads=("a", "b")))
    assert not _joined(_head(reads=("a", "b")), plan=True)


def test_a_file_the_first_cycle_reads_counts_but_one_the_piece_only_read_does_not():
    read_only = _session("a", [_msg(0, reads=("a",)), _msg(5, files=("b",))])
    carried = _session("b", [_head(prompt_flags=("path",), reads=("a",))])
    assert len(pieces_in([read_only, carried])) == 2
    edited = _session("b", [_head(prompt_flags=("path",), reads=("b",))])
    assert len(pieces_in([read_only, edited])) == 1


def test_rework_in_a_joined_piece_counts_from_the_delivery_in_the_earlier_session():
    delivered = _session("a", [_build(0)])
    correction = _cycle(
        _turn(HOUR, human_prompt_chars=2500, reads=("a",)), _turn(HOUR + 5, human_correction=True, files=("q",))
    )
    again = _msg(HOUR + 20, human_correction=True, files=("q",), human_prompt_chars=50)
    [joined] = pieces_in([delivered, _session("b", [correction, again])])
    assert (joined.cycles, joined.rework) == (3, 1)
    # Alone it has no delivery before it, so the same message is not rework.
    assert pieces_in([_session("b", [correction])])[0].rework == 0


def test_a_joined_piece_is_unsegmented_only_when_every_session_in_it_was():
    first = _session("a", [_build(0)])
    second = _session("b", [_later(HOUR), _msg(HOUR + 30, tag=_tag(shift="new"))])
    pieces_ = pieces_in([first, second])
    assert [p.session_ids for p in pieces_] == [("a", "b"), ("b",)]
    assert [p.unsegmented for p in pieces_] == [False, False]
    plain = pieces_in([_session("a", [_build(0)]), _session("b", [_later(HOUR)])])
    assert [p.unsegmented for p in plain] == [True]


def test_pieces_across_sessions_come_out_oldest_first():
    late = _session("late", [_msg(5 * HOUR)], "x")
    early = _session("early", [_msg(0)], "y")
    assert [p.session_ids for p in pieces_in([late, early])] == [("early",), ("late",)]


# -- asides: messages sent while background work runs -----------------------------------


@pytest.mark.parametrize("shift", ["new", "fix", "redo"])
def test_an_aside_never_starts_a_piece_and_is_never_rework(pricing, shift):
    piece = _only([_build(0), _aside(10, tag=_tag(shift=shift, why="missed"))], rates=pricing)
    assert (piece.cycles, piece.substantive, piece.aside_cycles) == (2, 1, 1)
    assert piece.aside_ids == (("", 1),)
    assert piece.rework == 0 and piece.causes == ()
    assert piece.aside_cost == pytest.approx(REPLY) and piece.cost == pytest.approx(2 * REPLY)
    assert piece.unsegmented


@pytest.mark.parametrize("shift, count, rework", [("new", 2, 0), ("fix", 1, 1), ("redo", 1, 1)])
def test_the_same_message_with_nothing_running_is_an_ordinary_one(pricing, shift, count, rework):
    found = pieces_of([_build(0), _msg(10, tag=_tag(shift=shift))], rates=pricing)
    assert len(found) == count
    assert sum(p.rework for p in found) == rework
    assert sum(p.aside_cycles for p in found) == 0 and sum(p.aside_cost for p in found) == 0.0


def test_an_aside_is_left_out_of_the_requests_the_mixes_and_the_pieces_labels_but_its_cost_stays(pricing):
    asked = _tag(task="chat", level="hard", size="xl")
    piece = _only(
        [
            _msg(0, files=("a",), tag=_tag(task="feature", level="easy", size="s")),
            _aside(10, tag=asked),
            _aside(20, tag=asked),
        ],
        rates=pricing,
    )
    assert piece.substantive == 1
    assert piece.levels == (("easy", 1, 0, 0.0),) and piece.sizes == (("s", 1, 0, 0.0),)
    assert (piece.task, piece.level, piece.size) == ("feature", "easy", "s")
    assert piece.cost == pytest.approx(3 * REPLY) and piece.replies == 3
    assert (piece.aside_cycles, piece.aside_cost) == (2, pytest.approx(2 * REPLY))


def test_a_message_that_asks_for_nothing_is_no_aside_however_much_is_running():
    piece = _only([
        _build(0),
        _aside(10, human_go=True, human_prompt_chars=3),
        _aside(20, human_status=True, human_prompt_chars=9),
    ])
    assert (piece.aside_cycles, piece.aside_cost) == (0, 0.0)


@pytest.mark.parametrize("changes", [{"files": ("q",)}, {"shell_write_count": 1}])
def test_a_message_that_changes_files_while_work_runs_is_no_aside(changes):
    ordinary = pieces_of([_build(0), _aside(10, tag=_tag(shift="new"), **changes)])
    assert [p.cycles for p in ordinary] == [1, 1]
    assert sum(p.aside_cycles for p in ordinary) == 0
    fix = _only([_build(0), _aside(10, tag=_tag(shift="fix"), **changes)])
    assert (fix.rework, fix.aside_cycles) == (1, 0)


def test_only_work_an_earlier_cycle_of_the_same_piece_started_makes_an_aside():
    # The run started in the first piece; the third message is in the second.
    first, second = pieces_of([_build(0), _msg(10, files=("x",), tag=_tag(shift="new")), _aside(20, running=(0,))])
    assert (first.cycles, second.cycles, second.aside_cycles) == (1, 2, 0)
    # Started by the second piece's own first cycle, it is an aside there.
    _, second = pieces_of([_build(0), _msg(10, files=("x",), tag=_tag(shift="new")), _aside(20, running=(1,))])
    assert second.aside_cycles == 1
    # A cycle can not be an aside to work it is itself running.
    assert _only([_aside(0, running=(0,))]).aside_cycles == 0


def test_a_clear_still_starts_a_piece_when_work_is_running():
    first, second = pieces_of([_build(0), _aside(30, commands_run=("clear",), human_prompt_chars=80)])
    assert (first.cycles, second.cycles) == (1, 1)
    assert (first.aside_cycles, second.aside_cycles) == (0, 0)


def test_an_aside_never_decides_where_the_next_piece_starts_by_its_files():
    # With no tag, a long gap and different files start a piece...
    cycles = [_msg(0, files=("a", "b")), _msg(3 * HOUR, reads=("c", "d"))]
    assert len(pieces_of(cycles)) == 2
    # ...but not for an aside, which is part of the piece it was asked in.
    assert len(pieces_of([_msg(0, files=("a", "b")), _aside(3 * HOUR, reads=("c", "d"))])) == 1


def test_an_asides_reads_do_not_join_a_later_session_to_the_piece():
    first = _session("a", [_build(0), _aside(10, reads=("q",))])
    head = _head(prompt_flags=("path",), reads=("q",))
    assert len(pieces_in([first, _session("b", [head])])) == 2


# -- a short message re-changing the last cycle's files is no rework on its own ---------


@pytest.mark.parametrize(
    "kw, files",
    [
        ({}, ("a",)),
        ({}, ("a", "b")),
        ({"human_prompt_chars": 12}, ("a",)),
        ({"human_prompt_chars": 1200}, ("a",)),
        ({}, ("c",)),
    ],
)
def test_a_message_that_changes_files_with_no_flag_or_tag_is_not_rework(kw, files):
    piece = _only([_build(0), _fix(10, files=files, **kw)])
    assert (piece.cycles, piece.substantive, piece.delivered) == (2, 2, True)
    assert piece.rework == 0 and piece.rework_ids == () and piece.causes == ()


def test_a_short_message_that_changes_the_last_cycles_files_is_not_rework_even_in_a_run_of_them():
    cycles = [_build(0), _fix(10), _fix(20), _fix(30)]
    piece = _only(cycles)
    assert (piece.cycles, piece.rework, piece.rework_cost) == (4, 0, 0.0)


def test_a_short_message_that_changes_the_last_cycles_files_is_not_rework_while_background_work_runs_either():
    # It changes files, so it is no aside: an ordinary cycle that steers the running work.
    piece = _only([_build(0), _during(10, files=("a",), human_prompt_chars=40)])
    assert (piece.cycles, piece.substantive, piece.aside_cycles) == (2, 2, 0)
    assert piece.rework == 0 and piece.causes == ()


@pytest.mark.parametrize(
    "kw",
    [
        {"human_correction": True},
        {"human_adjust": True},
        {"queued_correction": True},
        {"queued_adjust": True},
        {"tag": _tag(shift="fix")},
        {"tag": _tag(shift="redo")},
    ],
)
def test_the_same_short_message_is_rework_once_a_flag_or_a_tag_says_so(kw):
    piece = _only([_build(0), _fix(10, **kw)])
    assert (piece.rework, piece.rework_ids) == (1, (("", 1),))
    assert _causes(piece) == {("not_reported", "inferred"): 1}


@pytest.mark.parametrize(
    "kw, files",
    [
        ({"human_correction": True}, ("z",)),
        ({"human_adjust": True}, ("a",)),
        ({"tag": _tag(shift="fix")}, ("z",)),
        ({"tag": _tag(shift="redo")}, ("z",)),
    ],
)
def test_a_flagged_follow_up_that_changes_files_while_background_work_runs_is_still_rework(kw, files):
    piece = _only([_build(0), _during(10, files=files, human_prompt_chars=40, **kw)])
    assert (piece.rework, piece.aside_cycles) == (1, 0)


def test_a_correction_queued_while_background_work_runs_is_still_rework():
    working = _cycle(_turn(10, files=("a",), human_go=True, human_prompt_chars=3), _turn(12, queued_correction=True))
    working.running = (0,)
    assert _only([_build(0), working]).rework == 1


def test_an_adjustment_that_changes_other_files_while_background_work_runs_is_not_rework():
    assert _only([_build(0), _during(10, human_adjust=True, files=("q",), human_prompt_chars=40)]).rework == 0


# -- rework -------------------------------------------------------------------------


def test_a_plan_a_build_and_three_short_corrections_are_one_piece_with_three_rework_cycles():
    cycles = [
        _msg(0, plan_stats=PlanStats(outcome="approved", steps=3, files=2), prompt_plan_mode=True),
        _build(10),
        _fix(20, human_correction=True),
        _fix(25, human_correction=True),
        _fix(30, human_correction=True),
    ]
    piece = _only(cycles)
    assert (piece.cycles, piece.substantive, piece.rework) == (5, 5, 3)
    assert piece.delivered and piece.plan_approved and piece.unsegmented
    # No tag, no answer: nothing says why, and nothing blames Claude.
    assert piece.causes == (("not_reported", "inferred", 3),)
    assert piece.missed_in == ()


def test_a_short_message_that_changes_nothing_is_not_rework():
    assert _only([_build(0), _msg(10, human_prompt_chars=40)]).rework == 0


def test_nothing_is_rework_before_the_first_delivery():
    cycles = [_msg(0, human_correction=True), _msg(10, human_correction=True), _build(20)]
    assert _only(cycles).rework == 0


def test_go_ahead_runs_are_not_rework():
    cycles = [
        _build(0),
        _msg(10, files=("a",), human_go=True, human_prompt_chars=4),
        _msg(20, files=("a",), human_go=True, human_prompt_chars=2),
        _msg(30, files=("a", "b"), human_status=True, human_prompt_chars=14),
        _msg(40, files=("a",), human_ack=True, human_prompt_chars=6),
    ]
    piece = _only(cycles)
    assert (piece.cycles, piece.substantive, piece.rework) == (5, 1, 0)


def test_a_go_ahead_status_check_or_thank_you_tagged_fix_is_still_not_rework():
    cycles = [
        _build(0),
        _fix(10, human_correction=True, tag=_tag(shift="fix")),
        _msg(20, files=("a",), human_go=True, human_prompt_chars=8, tag=_tag(shift="fix")),
        _msg(30, files=("a",), human_status=True, human_prompt_chars=14, tag=_tag(shift="fix")),
        _msg(40, files=("a",), human_ack=True, human_prompt_chars=6, tag=_tag(shift="redo")),
    ]
    piece = _only(cycles)
    assert (piece.rework, piece.substantive, piece.rework_ids) == (1, 2, (("", 1),))
    assert sum(row[2] for row in piece.levels) == piece.rework


def test_a_correction_you_typed_is_rework_whatever_it_changed():
    piece = _only([_build(0), _msg(10, human_correction=True, human_prompt_chars=900, files=("z",))])
    assert piece.rework == 1


def test_a_correction_queued_while_claude_worked_counts():
    working = _cycle(_turn(10, files=("a",), human_go=True, human_prompt_chars=3), _turn(12, queued_correction=True))
    piece = _only([_build(0), working])
    assert (piece.rework, piece.substantive) == (1, 2)
    # Without it the go-ahead asks for nothing and is no rework.
    plain = _only([_build(0), _cycle(_turn(10, files=("a",), human_go=True, human_prompt_chars=3), _turn(12))])
    assert (plain.rework, plain.substantive) == (0, 1)


def test_an_adjustment_is_rework_only_when_it_changes_files_the_piece_already_changed():
    on_piece = _msg(10, human_adjust=True, files=("b",), human_prompt_chars=800)
    elsewhere = _msg(10, human_adjust=True, files=("q",), human_prompt_chars=800)
    assert _only([_build(0), on_piece]).rework == 1
    assert _only([_build(0), elsewhere]).rework == 0
    queued = _cycle(_turn(10, human_go=True, human_prompt_chars=3), _turn(12, queued_adjust=True, files=("a",)))
    assert _only([_build(0), queued]).rework == 1


def test_an_adjustment_with_no_answer_or_tag_has_no_cause_reported():
    piece = _only([_build(0), _msg(10, human_adjust=True, files=("b",), human_prompt_chars=800)])
    assert piece.causes == (("not_reported", "inferred", 1),)


def test_a_settled_redo_or_fix_is_rework():
    for shift in ("redo", "fix"):
        piece = _only([_build(0), _msg(10, files=("q",), human_prompt_chars=800, tag=_tag(shift=shift))])
        assert piece.rework == 1
    assert _only([_build(0), _msg(10, files=("q",), human_prompt_chars=800, tag=_tag(shift="build"))]).rework == 0


@pytest.mark.parametrize(
    "kw",
    [
        {"plan_stats": PlanStats(rejected=True, feedback_chars=60)},
        {"prompt_plan_mode": True},
    ],
)
def test_a_round_of_plan_feedback_is_never_rework(kw):
    plan = _msg(10, human_correction=True, files=("a",), tag=_tag(shift="fix"), **kw)
    assert _only([_build(0), plan]).rework == 0


def test_a_rejected_plan_with_no_feedback_typed_is_not_a_plan_round():
    rejected = _msg(10, human_correction=True, plan_stats=PlanStats(rejected=True))
    assert _only([_build(0), rejected]).rework == 1


def test_rework_costs_what_its_cycles_cost(pricing):
    piece = _only([_build(0), _fix(10, human_correction=True), _fix(20, human_correction=True)], rates=pricing)
    assert piece.cost == pytest.approx(3 * REPLY)
    assert piece.rework_cost == pytest.approx(2 * REPLY)
    assert _only([_build(0), _fix(10, human_correction=True)]).cost == 0.0


def test_a_piece_lists_the_ids_of_its_rework_cycles_in_the_order_they_came():
    again = {"human_correction": True}
    piece = _only(
        [_build(0), _fix(10, **again), _build(15, files=("c",)), _fix(20, files=("c",), **again), _fix(30, files=("c",), **again)]
    )
    assert piece.rework_ids == (("", 1), ("", 3), ("", 4))
    assert len(piece.rework_ids) == piece.rework
    assert _only([_build(0), _fix(10, files=("c",), human_adjust=True)]).rework_ids == ()
    # Across a handoff the ids name the session each cycle is in.
    first = _session("a", [_build(0), _fix(10, files=("a",), **again)])
    second = _session(
        "b", [_cycle(_turn(2 * HOUR, human_prompt_chars=2500, files=("a",))), _fix(2 * HOUR + 10, **again)]
    )
    [joined] = pieces_in([first, second])
    assert [sid for sid, _n in joined.rework_ids] == ["a", "b"]


# -- the messages that asked for something ------------------------------------------------


@pytest.mark.parametrize("flag", ["human_go", "human_status", "human_ack"])
def test_a_go_ahead_a_status_check_and_a_thank_you_ask_for_nothing(flag):
    assert pieces.asks(_msg(0)) == 1
    assert pieces.asks(_msg(0, **{flag: True, "human_prompt_chars": 5})) == 0


def test_a_reply_to_a_plan_you_sent_back_asks_for_nothing_but_one_to_an_approved_plan_does():
    rejected = _msg(0, plan_stats=PlanStats(outcome="rejected", steps=3, files=2), prompt_plan_mode=True)
    approved = _msg(0, plan_stats=PlanStats(outcome="approved", steps=3, files=2), prompt_plan_mode=True)
    reply = _msg(5, prompt_plan_mode=True)
    assert pieces.asks(reply, rejected) == 0
    assert pieces.asks(reply, approved) == 1
    # Nothing before it, or not written in plan mode: an ordinary message.
    assert pieces.asks(reply) == 1
    assert pieces.asks(_msg(5), rejected) == 1


def test_a_plan_reply_never_starts_a_piece_and_asks_for_nothing():
    plan = _cycle(_turn(10, reads=("x", "y"), plan_stats=PlanStats(outcome="rejected", rejected=True)))
    tagged = _msg(20, prompt_plan_mode=True, tag=_tag(shift="new"))
    late = _msg(10 + 4 * HOUR, prompt_plan_mode=True, reads=("e", "f"), gap_s=4 * 3600.0)
    for reply in (tagged, late):
        piece = _only([_build(0), plan, reply])
        assert piece.cycles == 3 and piece.substantive == 2


def test_a_feedback_run_and_an_empty_cycle_ask_for_nothing():
    assert pieces.asks(_run(0)) == 0
    assert pieces.asks(Cycle(start=0, end=0, turns=[])) == 0


def test_each_message_you_typed_while_claude_worked_is_one_more_that_asked():
    working = _cycle(_turn(0, queued_prompts=1, queued_adjust=True), _turn(5, queued_prompts=2, queued_correction=True))
    # The opening message and the three after it.
    assert pieces.asks(working) == 4
    assert pieces.queued_asks(_turn(0)) == 0
    assert pieces.queued_asks(_turn(0, queued_prompts=2)) == 2


def test_a_go_ahead_and_a_status_check_among_the_queued_messages_are_taken_off_the_count():
    assert pieces.queued_asks(_turn(0, queued_prompts=1, queued_go=True)) == 0
    assert pieces.queued_asks(_turn(0, queued_prompts=2, queued_go=True, queued_status=True)) == 0
    assert pieces.queued_asks(_turn(0, queued_prompts=3, queued_go=True)) == 2
    # The parser flags that a correction was among them: that one asked, whatever else was queued.
    mixed = _turn(0, queued_prompts=2, queued_go=True, queued_status=True, queued_correction=True)
    assert pieces.queued_asks(mixed) == 1
    # A go-ahead you typed as the cycle's own message does not take away the ones queued behind it.
    both = _cycle(_turn(0, human_go=True, human_prompt_chars=3), _turn(5, queued_prompts=2, queued_adjust=True))
    assert pieces.asks(both) == 2


# -- feedback excludes what you said was not rework -----------------------------------


def test_a_change_of_mind_alone_is_not_rework():
    cycles = [_build(0), _fix(10, human_correction=True)]
    assert _only(cycles, feedback=Feedback(source="answers", why=("changed",))).rework == 0
    # With another reason beside it the cycle stands.
    mixed = Feedback(source="answers", why=("changed", "left_out"))
    assert _only(cycles, feedback=mixed).rework == 1
    assert _only(cycles, feedback=Feedback(source="answers", why=("none", "changed"))).rework == 0


def test_a_plan_you_say_was_new_makes_the_later_cycles_no_rework():
    cycles = [_msg(0, plan_stats=PlanStats(outcome="approved")), _build(10), _fix(20, human_correction=True)]
    assert _only(cycles, feedback=Feedback(source="answers", plan="new")).rework == 0
    assert _only(cycles, feedback=Feedback(source="answers", plan="gap")).rework == 1


def test_a_plan_you_say_was_new_leaves_the_rework_before_the_plan_alone():
    cycles = [
        _build(0),
        _fix(10, human_correction=True),
        _cycle(_turn(20, plan_stats=PlanStats(outcome="approved"))),
        _build(30, files=("c", "d")),
        _fix(40, files=("c",), human_correction=True),
    ]
    assert _only(cycles, feedback=Feedback(source="answers", plan="new")).rework_ids == (("", 1),)


def test_a_plan_check_of_new_or_none_rules_a_cycle_out_and_covered_or_gap_never_does():
    def checked(word):
        return _only([_build(0), _fix(10, human_correction=True, plan_check=PlanCheck(word=word))])

    assert checked("new").rework == 0 and checked("none").rework == 0
    assert checked("covered").rework == 1 and checked("gap").rework == 1
    # It outranks a change-of-mind answer that would have ruled the cycle out.
    cycles = [_build(0), _fix(10, human_correction=True, plan_check=PlanCheck(word="gap"))]
    assert _only(cycles, feedback=Feedback(source="answers", why=("changed",))).rework == 1


def test_feedback_that_rates_a_cycle_makes_the_piece_rated():
    cycles = [_build(0), _fix(10), _run(20, Feedback(source="answers", why=("left_out",)))]
    assert _only(cycles).rated
    assert not _only([_build(0), _fix(10)]).rated
    assert _only([_build(0)], feedback=Feedback(source="skipped")).rated


def test_a_feedback_run_that_kept_no_answer_still_marks_its_piece_rated():
    asked_nothing = _msg(20, commands_run=("cg-feedback",))
    assert _only([_build(0), asked_nothing]).rated
    # A run in a later piece does not rate the earlier one.
    first, second = pieces_of([_build(0), _msg(30, tag=_tag(shift="new"), files=("z",)), asked_nothing])
    assert (first.rated, second.rated) == (False, True)


def test_a_feedback_run_before_any_work_does_not_rate_the_piece_after_it():
    piece = _only([_run(0, Feedback(source="answers", worth="yes")), _build(10), _fix(20)])
    assert not piece.rated


def test_a_feedback_run_that_opens_a_joining_session_does_not_rate_the_piece():
    first = _session("a", [_build(0), _fix(10, files=("a",))])
    second = _session("b", [_run(2 * HOUR - 5, Feedback(source="answers", worth="yes")), _later(2 * HOUR, files=("a",))])
    [piece] = pieces_in([first, second])
    assert piece.session_ids == ("a", "b") and not piece.rated


def test_a_skipped_rating_still_covers_the_work_before_it_and_rules_nothing_out():
    cycles = [_build(0), _fix(10, human_correction=True), _run(20, Feedback(source="skipped"))]
    piece = _only(cycles)
    assert piece.rated and piece.rework == 1


def test_a_rating_covers_only_the_cycles_before_it():
    spans = capture.feedback_spans([_build(0), _run(5, Feedback(source="answers", why=("changed",))), _fix(10)])
    piece = _only([_build(0), _fix(10, human_correction=True)], feedback=spans)
    # The run never ran in this list; the spans name their own cycles.
    assert piece.rework == 1


def test_feedback_by_cycle_reads_each_runs_answers_for_the_cycles_it_rates():
    changed = Feedback(source="answers", why=("changed",))
    left = Feedback(source="answers", why=("left_out",))
    cycles = [
        _build(0),
        _fix(10, human_correction=True),
        _run(20, changed),
        _fix(30, human_correction=True),
        _run(40, left),
    ]
    piece = _only(cycles)
    # The first correction was a change of mind; the second was something left out.
    assert (piece.rework, piece.rated) == (1, True)
    assert _causes(piece) == {("left_out", "feedback"): 1}


# -- the cause of rework ---------------------------------------------------------------


def test_feedback_beats_the_tag_which_beats_cause_not_reported():
    fixing = {"human_correction": True, "tag": _tag(shift="fix", why="tools")}
    from_feedback = _only([_build(0), _fix(10, **fixing)], feedback=Feedback(source="answers", why=("left_out",)))
    assert _causes(from_feedback) == {("left_out", "feedback"): 1}
    from_tag = _only([_build(0), _fix(10, **fixing)])
    assert _causes(from_tag) == {("tools", "Claude tag"): 1}
    adjusted = _only([_build(0), _msg(10, human_adjust=True, files=("a",), human_prompt_chars=900)])
    assert _causes(adjusted) == {("not_reported", "inferred"): 1}
    nothing = _only([_build(0), _fix(10, human_correction=True)])
    assert _causes(nothing) == {("not_reported", "inferred"): 1}


def test_a_tag_written_by_haiku_is_labelled_so():
    tag = CaptureTag(has_tl=True, judged=True, shift="fix", why="missed")
    piece = _only([_build(0), _fix(10, tag=tag)])
    assert _causes(piece) == {("missed", "Haiku tag"): 1}


def test_a_correction_alone_never_says_claude_got_it_wrong():
    for kw in (
        {"human_correction": True},
        {"queued_correction": True, "human_go": True, "human_prompt_chars": 3},
        {"tag": _tag(shift="redo")},
    ):
        piece = _only([_build(0), _fix(10, **kw)])
        assert piece.rework == 1
        assert {cause for cause, _source, _n in piece.causes} == {"not_reported"}


def test_missed_comes_only_from_your_answers_or_a_tag():
    answered = _only(
        [_build(0), _fix(10, human_correction=True)],
        feedback=Feedback(source="answers", why=("missed",), missed_in="message"),
    )
    assert answered.causes == (("missed", "feedback", 1),)
    assert answered.missed_in == (("message", 1),)
    tagged = _only([_build(0), _fix(10, tag=_tag(shift="fix", why="missed"))])
    assert tagged.causes == (("missed", "Claude tag", 1),)
    assert tagged.missed_in == ()


def test_the_plan_check_names_the_cause_before_anything_else():
    tag = _tag(shift="fix", why="left_out")
    gap = _only([_build(0), _fix(10, tag=tag, plan_check=PlanCheck(word="gap"))])
    assert gap.causes == (("plan_gap", "feedback", 1),)
    covered = _only([_build(0), _fix(10, tag=tag, plan_check=PlanCheck(word="covered"))])
    assert covered.causes == (("missed", "feedback", 1),)
    assert covered.missed_in == (("plan", 1),)


def test_a_plan_answer_names_the_cause_after_an_approved_plan_and_only_then():
    cycles = [_msg(0, plan_stats=PlanStats(outcome="approved")), _build(10), _fix(20, human_correction=True)]
    gap = _only(cycles, feedback=Feedback(source="answers", plan="gap", why=("left_out",)))
    assert gap.causes == (("plan_gap", "feedback", 1),)
    covered = _only(cycles, feedback=Feedback(source="answers", plan="covered"))
    assert covered.causes == (("missed", "feedback", 1),) and covered.missed_in == (("plan", 1),)
    # No plan was approved before the fix, so the plan answer has nothing to say.
    no_plan = _only([_build(0), _fix(10, human_correction=True)], feedback=Feedback(source="answers", plan="gap"))
    assert no_plan.causes == (("not_reported", "inferred", 1),)


def test_several_answered_causes_resolve_to_the_tags_word_or_mixed():
    both = Feedback(source="answers", why=("left_out", "missed"))
    tagged = _only([_build(0), _fix(10, tag=_tag(shift="fix", why="missed"))], feedback=both)
    assert tagged.causes == (("missed", "feedback", 1),)
    untagged = _only([_build(0), _fix(10, human_correction=True)], feedback=both)
    assert untagged.causes == (("mixed", "feedback", 1),)


def test_the_causes_of_a_piece_add_up_to_its_rework_in_the_order_of_the_words():
    cycles = [
        _build(0),
        _fix(10, tag=_tag(shift="fix", why="tools")),
        _fix(20, human_correction=True),
        _fix(30, tag=_tag(shift="fix", why="left_out")),
        _fix(40, human_correction=True),
    ]
    piece = _only(cycles)
    assert piece.rework == 4 == sum(n for _c, _s, n in piece.causes)
    assert piece.causes == (
        ("left_out", "Claude tag", 1),
        ("tools", "Claude tag", 1),
        ("not_reported", "inferred", 2),
    )
    assert {c for c, _s, _n in piece.causes} <= set(pieces.CAUSES)
    assert {s for _c, s, _n in piece.causes} <= set(pieces.SOURCES)


def test_each_cause_is_priced_and_its_tokens_counted(pricing):
    cycles = [
        _build(0),
        _fix(10, tag=_tag(shift="fix", why="tools")),
        _fix(20, human_correction=True),
        _fix(30, human_correction=True),
    ]
    piece = _only(cycles, rates=pricing)
    assert piece.cause_spend == (
        ("tools", "Claude tag", pytest.approx(REPLY), 150),
        ("not_reported", "inferred", pytest.approx(2 * REPLY), 300),
    )
    assert [(c, s) for c, s, *_ in piece.cause_spend] == [(c, s) for c, s, _n in piece.causes]
    assert sum(row[2] for row in piece.cause_spend) == pytest.approx(piece.rework_cost)
    assert piece.rework_tokens == sum(row[3] for row in piece.cause_spend) == 450
    # No price table: the tokens are still counted, the cost is not.
    unpriced = _only(cycles)
    assert (unpriced.rework_cost, unpriced.rework_tokens) == (0.0, 450)


def test_an_admission_is_the_settled_admit_word_and_who_found_it():
    admit = {"admit_candidate": True}
    cycles = [
        _build(0),
        _fix(10, human_correction=True, tag=_tag(shift="fix", admit="claim"), admit_caught="user", **admit),
        _msg(20, files=("c",), human_prompt_chars=400, tag=_tag(admit="instruction"), admit_caught="self", **admit),
        _msg(30, files=("d",), human_prompt_chars=400, tag=_tag(admit="change"), admit_caught="user", **admit),
    ]
    piece = _only(cycles)
    assert (piece.admitted, piece.admitted_user, piece.admitted_self, piece.admitted_instruction) == (3, 2, 1, 1)
    assert piece.admit_possible == 0
    # An admit word the catalogue doesn't know is no admission.
    assert _only([_build(0), _msg(10, tag=_tag(admit="oops"), **admit)]).admitted == 0


def test_a_tag_with_no_pattern_match_behind_it_reads_who_found_it_from_your_message():
    corrected = _only([_build(0), _fix(10, human_correction=True, tag=_tag(shift="fix", admit="claim"))])
    assert (corrected.admitted_user, corrected.admitted_self) == (1, 0)
    queued = _only([_build(0), _fix(10, queued_adjust=True, tag=_tag(shift="fix", admit="claim"))])
    assert (queued.admitted_user, queued.admitted_self) == (1, 0)
    alone = _only([_build(0), _msg(10, files=("c",), human_prompt_chars=400, tag=_tag(admit="claim"))])
    assert (alone.admitted_user, alone.admitted_self) == (0, 1)


def test_a_reply_that_reads_like_an_admission_nobody_tagged_is_only_possible():
    possible = _msg(10, files=("c",), human_prompt_chars=400)
    possible.facts = {"admit_candidate": True}
    piece = _only([_build(0), possible])
    assert (piece.admit_possible, piece.admitted, piece.admitted_user, piece.admitted_self) == (1, 0, 0, 0)


def test_the_rework_after_an_admission_you_caught_is_its_own_cycle_and_the_run_after_it(pricing):
    caught = {"human_correction": True, "tag": _tag(shift="fix", admit="claim"), "admit_candidate": True}
    cycles = [
        _build(0),
        _fix(10, admit_caught="user", **caught),
        _fix(20, human_correction=True),
        _msg(30, files=("z",), human_prompt_chars=400),
        _fix(40, files=("z",), human_correction=True),
    ]
    piece = _only(cycles, rates=pricing)
    assert piece.rework == 3
    # Cycles 1 and 2 are the run after the admission; cycle 4 is a later, unrelated fix.
    assert piece.admit_user_cost == pytest.approx(2 * REPLY)
    # An admission on a cycle that was no rework starts the run at the next cycle, once.
    late = [
        _build(0),
        _msg(10, files=("z",), human_prompt_chars=400, tag=_tag(admit="claim"), admit_candidate=True, admit_caught="user"),
        _fix(20, files=("z",), human_correction=True),
        _fix(30, files=("z",), human_correction=True),
    ]
    assert _only(late, rates=pricing).admit_user_cost == pytest.approx(2 * REPLY)
    # One Claude owned up to itself costs the rework nothing here.
    itself = {**caught, "admit_caught": "self", "human_correction": False}
    assert _only([_build(0), _fix(10, **itself)], rates=pricing).admit_user_cost == 0.0


# -- what a piece says about itself -----------------------------------------------------


def test_a_pieces_task_level_and_size_come_from_its_tags():
    cycles = [
        _msg(0, tag=_tag(task="feature", level="easy", size="s")),
        _build(10),
        _msg(20, tag=_tag(task="feature", level="hard", size="m")),
        _msg(30, tag=_tag(task="bugfix", level="normal", size="xs")),
    ]
    piece = _only(cycles)
    assert (piece.task, piece.level, piece.size) == ("feature", "hard", "m")
    assert _only([_msg(0), _msg(10)]).task == ""


def test_a_piece_counts_its_cycles_by_level_and_by_size_with_unknown_for_none():
    cycles = [
        _msg(0, tag=_tag(level="easy", size="s")),
        _build(10, files=("a", "b")),
        _fix(20, human_correction=True, tag=_tag(level="hard", size="m", shift="fix")),
        _msg(30, human_go=True, human_prompt_chars=3),
    ]
    piece = _only(cycles)
    assert piece.levels == (("easy", 1, 0, 0.0), ("hard", 1, 1, 0.0), ("unknown", 1, 0, 0.0))
    assert piece.sizes == (("s", 1, 0, 0.0), ("m", 1, 1, 0.0), ("unknown", 1, 0, 0.0))


def test_a_cycle_with_a_word_the_catalogue_does_not_know_is_counted_as_unknown():
    piece = _only([_msg(0, tag=_tag(level="gigantic"))])
    assert piece.levels == (("unknown", 1, 0, 0.0),)


def test_a_piece_with_a_plan_approved_in_it_says_so():
    assert _only([_msg(0, plan_stats=PlanStats(outcome="approved_by_message")), _build(10)]).plan_approved
    assert not _only([_msg(0, plan_stats=PlanStats(outcome="rejected")), _build(10)]).plan_approved
    assert not _only([_build(0)]).plan_approved


def test_a_piece_delivered_when_a_cycle_changed_files_by_any_route():
    assert not _only([_msg(0)]).delivered
    assert _only([_msg(0, files=("a",))]).delivered
    assert _only([_msg(0, shell_write_count=1)]).delivered
    assert _only([_msg(0, shell_change_count=1)]).delivered
    assert _only([_msg(0, agent_edit_files=2)]).delivered


def test_a_piece_is_counts_and_ids_only():
    piece = _only([_build(0), _fix(10, tag=_tag(task="feature", shift="fix", why="missed"))])
    for name in WorkPiece.__slots__:
        value = getattr(piece, name)
        assert isinstance(value, (str, int, float, bool, tuple)), name
    with pytest.raises(AttributeError):
        piece.rework = 5  # frozen


def test_a_piece_holds_tokens_and_replies_apart_from_what_agents_used(pricing):
    piece = _only([_build(0), _fix(10)], rates=pricing)
    assert (piece.tokens, piece.agent_tokens, piece.replies) == (300, 0, 2)
    assert piece.moved_cost == 0.0


# -- cost: a reply to an agent's report is charged to the cycle that started the agent --


def _edits(second: int, text: str) -> dict:
    """One reply that says ``text`` and edits a file."""
    return _reply(second, {"type": "text", "text": text}, tool_use_block("Edit", f"toolu_E{second}", {"file_path": "b.md"}))


def _agent_chain(tmp_path, *, tags=True):
    """Two messages. The first starts a background agent; a second, with a
    tag that starts a new piece, runs while it works (it edits a file, so it
    is no aside); its report arrives after the second message, and
    the reply to it is the first piece's."""
    second = "Done.\n[cg: task=docs shift=new]" if tags else "Done."
    top = _top(tmp_path, [
        _note(0, TAG_IDS),
        _ask(1, "research this"),
        _background(2, "toolu_A", text="Launched.\n[cg: task=research]"),
        _launched(3, "toolu_A"),
        _reply(4, text="Launched.\n[cg: task=research]"),
        _ask(10, "something else"),
        _edits(11, second),
        _report(20, "a1"),
        _reply(21, text="It found the cause.\n[cg: task=research]"),
    ])
    return top, [_agent_that_reports(tmp_path, "a1", "toolu_A", 6)]


def _workflow_chain(tmp_path, *, tags=True):
    second = "Sure.\n[cg: task=docs shift=new]" if tags else "Sure."
    top = _top(tmp_path, [
        _note(0, TAG_IDS),
        _ask(1, "run it"),
        _wf_call(2, "tu_w"),
        _wf_launched(3, "tu_w", "wf_a", "t_a"),
        _reply(4, text="Running.\n[cg: task=research]"),
        _ask(10, "meanwhile"),
        _edits(11, second),
        _report(20, "t_a"),
        _reply(21, text="It finished.\n[cg: task=research]"),
    ])
    return top, [_wf_agent(tmp_path, "w1", "wf_a", _wf_steps(6))]


@pytest.mark.parametrize("chain", [_agent_chain, _workflow_chain])
def test_a_reply_to_an_agents_report_is_charged_to_the_piece_that_started_the_agent(tmp_path, pricing, chain):
    top, subs = chain(tmp_path)
    cycles = capture.prompt_cycles(top, subs)
    first, second = pieces_of(cycles, pricing)
    # First: its two replies, the agent's one, and the reply to the report.
    assert (first.cycles, first.replies, first.cost) == (1, 3, pytest.approx(4 * REPLY))
    assert first.moved_cost == pytest.approx(REPLY)
    assert (first.tokens, first.agent_tokens) == (450, 150)
    # Second: only the reply it ran.
    assert (second.cycles, second.replies, second.cost) == (1, 1, pytest.approx(REPLY))
    assert second.moved_cost == 0.0
    # Each turn is charged once: the pieces add up to the session.
    assert first.cost + second.cost == pytest.approx(5 * REPLY)
    # The reply keeps its place in the timeline.
    assert [len(c.turns) for c in cycles] == [2, 2]
    assert second.end_ts == _ts(21)


@pytest.mark.parametrize("chain", [_agent_chain, _workflow_chain])
def test_the_moved_charge_stays_in_the_piece_when_both_cycles_are_one_piece(tmp_path, pricing, chain):
    top, subs = chain(tmp_path, tags=False)
    cycles = capture.prompt_cycles(top, subs)
    # The reply is still the first cycle's, so the piece's cost labels it as moved.
    single = _only(cycles, rates=pricing)
    assert single.cost == pytest.approx(5 * REPLY)
    assert (single.cycles, single.replies) == (2, 5 - 1)
    assert single.moved_cost == pytest.approx(REPLY)


def test_a_report_answered_in_the_cycle_that_started_the_agent_moves_nothing(tmp_path, pricing):
    top = _top(tmp_path, [
        _note(0, TAG_IDS),
        _ask(1, "research this"),
        _background(2, "toolu_A", text="Launched.\n[cg: task=research]"),
        _report(8, "a1"),
        _reply(9, text="Found it.\n[cg: task=research]"),
    ])
    [piece] = pieces_of(capture.prompt_cycles(top, [_agent_that_reports(tmp_path, "a1", "toolu_A", 6)]), pricing)
    assert piece.moved_cost == 0.0
    assert (piece.replies, piece.cost) == (2, pytest.approx(3 * REPLY))


def test_every_turn_of_a_reply_to_a_report_goes_with_it(tmp_path, pricing):
    """A reply that read the report and then made a tool call is two turns:
    both are the first piece's."""
    top = _top(tmp_path, [
        _note(0, TAG_IDS),
        _ask(1, "research this"),
        _background(2, "toolu_A", text="Launched.\n[cg: task=research]"),
        _launched(3, "toolu_A"),
        _reply(4, text="Launched.\n[cg: task=research]"),
        _ask(10, "something else"),
        _edits(11, "Done.\n[cg: task=docs shift=new]"),
        _report(20, "a1"),
        _reply(21, tool_use_block("Read", "toolu_R", {"file_path": "notes.md"})),
        user_block_line([tool_result_block("toolu_R", "text")], timestamp=_ts(22)),
        _reply(23, text="It found the cause.\n[cg: task=research]"),
    ])
    first, second = pieces_of(
        capture.prompt_cycles(top, [_agent_that_reports(tmp_path, "a1", "toolu_A", 6)]), pricing
    )
    assert first.replies == 4 and second.replies == 1
    assert first.moved_cost == pytest.approx(2 * REPLY)
    assert first.cost + second.cost == pytest.approx(6 * REPLY)


def test_a_piece_with_no_price_table_has_no_cost_but_still_has_its_tokens(tmp_path):
    top, subs = _agent_chain(tmp_path)
    first, second = pieces_of(capture.prompt_cycles(top, subs))
    assert (first.cost, first.moved_cost, second.cost) == (0.0, 0.0, 0.0)
    assert (first.tokens, second.tokens) == (450, 150)


# -- asides, on real transcripts ---------------------------------------------------------


def _aside_chain(tmp_path, shift: str, *, edits: bool = False, after_report: bool = False):
    """A message edits a file and starts a workflow. A second message, tagged
    ``shift``, comes while it runs (it edits a file when ``edits``) or once its
    report has been answered (``after_report``)."""
    text = f"Sure.\n[cg: task=chat shift={shift}]"
    reply = _edits(11, text) if edits else _reply(11, text=text)
    lines = [
        _note(0, TAG_IDS),
        _ask(1, "build it"),
        _reply(2, {"type": "text", "text": "Building."}, tool_use_block("Edit", "toolu_E0", {"file_path": "a.md"})),
        _wf_call(3, "tu_w"),
        _wf_launched(4, "tu_w", "wf_a", "t_a"),
        _reply(5, text="Running.\n[cg: task=feature]"),
    ]
    if after_report:
        lines += [
            _report(8, "t_a"),
            _reply(9, text="It finished.\n[cg: task=feature]"),
            _ask(30, "a question"),
            _reply(31, text=text),
        ]
    else:
        lines += [_ask(10, "a question"), reply, _report(20, "t_a"), _reply(21, text="It finished.\n[cg: task=feature]")]
    return capture.prompt_cycles(_top(tmp_path, lines), [_wf_agent(tmp_path, "w1", "wf_a", _wf_steps(6))])


@pytest.mark.parametrize("shift", ["new", "fix"])
def test_a_question_asked_while_a_workflow_runs_is_neither_a_boundary_nor_rework(tmp_path, pricing, shift):
    cycles = _aside_chain(tmp_path, shift)
    assert [c.running for c in cycles] == [(), (0,)]
    piece = _only(cycles, rates=pricing)
    assert (piece.cycles, piece.substantive, piece.aside_cycles) == (2, 1, 1)
    assert piece.rework == 0
    # Its one reply is the aside's cost; the piece holds every reply.
    assert piece.aside_cost == pytest.approx(REPLY)
    assert piece.cost == pytest.approx(6 * REPLY)


@pytest.mark.parametrize("shift, count, rework", [("new", 2, 0), ("fix", 1, 1)])
def test_the_same_question_after_the_workflows_report_is_an_ordinary_cycle(tmp_path, pricing, shift, count, rework):
    cycles = _aside_chain(tmp_path, shift, after_report=True)
    assert [c.running for c in cycles] == [(), ()]
    found = pieces_of(cycles, rates=pricing)
    assert len(found) == count
    assert sum(p.rework for p in found) == rework
    assert sum(p.aside_cycles for p in found) == 0 and sum(p.aside_cost for p in found) == 0.0


@pytest.mark.parametrize("shift, count, rework", [("new", 2, 0), ("fix", 1, 1)])
def test_a_cycle_that_edits_a_file_while_the_workflow_runs_is_not_an_aside(tmp_path, pricing, shift, count, rework):
    cycles = _aside_chain(tmp_path, shift, edits=True)
    assert [c.running for c in cycles] == [(), (0,)]
    found = pieces_of(cycles, rates=pricing)
    assert len(found) == count
    assert sum(p.rework for p in found) == rework
    assert sum(p.aside_cycles for p in found) == 0 and sum(p.aside_cost for p in found) == 0.0


def _steer_chain(tmp_path, kind: str, *, after_report: bool = False):
    """A message edits ``a.md`` and starts a background agent or workflow
    (``kind``). A short second message, with no tag, edits ``a.md`` again
    while it runs, or once its report has been answered (``after_report``)."""
    edit = tool_use_block("Edit", "toolu_E0", {"file_path": "a.md"})
    again = tool_use_block("Edit", "toolu_E1", {"file_path": "a.md"})
    if kind == "agent":
        start = [_background(3, "toolu_A", text="Launched."), _launched(4, "toolu_A"), _reply(5, text="Launched.")]
        subs = [_agent_that_reports(tmp_path, "a1", "toolu_A", 6)]
        report = "a1"
    else:
        start = [_wf_call(3, "tu_w"), _wf_launched(4, "tu_w", "wf_a", "t_a"), _reply(5, text="Running.")]
        subs = [_wf_agent(tmp_path, "w1", "wf_a", _wf_steps(6))]
        report = "t_a"
    lines = [_note(0, TAG_IDS), _ask(1, "build it"), _reply(2, {"type": "text", "text": "Building."}, edit), *start]
    if after_report:
        lines += [_report(8, report), _reply(9, text="It finished.")]
        lines += [_ask(30, "one more pass"), _reply(31, {"type": "text", "text": "Done."}, again)]
    else:
        lines += [_ask(10, "one more pass"), _reply(11, {"type": "text", "text": "Done."}, again)]
        lines += [_report(20, report), _reply(21, text="It finished.")]
    return capture.prompt_cycles(_top(tmp_path, lines), subs)


@pytest.mark.parametrize("kind", ["agent", "workflow"])
def test_a_short_message_that_re_edits_a_file_while_background_work_runs_is_not_rework(tmp_path, pricing, kind):
    cycles = _steer_chain(tmp_path, kind)
    assert [c.running for c in cycles] == [(), (0,)]
    piece = _only(cycles, rates=pricing)
    assert (piece.cycles, piece.substantive, piece.aside_cycles) == (2, 2, 0)
    assert piece.rework == 0 and piece.causes == ()


@pytest.mark.parametrize("kind", ["agent", "workflow"])
def test_the_same_message_once_the_report_has_arrived_is_not_rework_either(tmp_path, pricing, kind):
    cycles = _steer_chain(tmp_path, kind, after_report=True)
    assert [c.running for c in cycles] == [(), ()]
    piece = _only(cycles, rates=pricing)
    assert (piece.cycles, piece.rework, piece.aside_cycles) == (2, 0, 0)
    assert piece.causes == ()


# -- real transcripts ------------------------------------------------------------------


def test_a_queued_message_opens_no_piece_and_a_queued_correction_is_rework(tmp_path):
    from claudeglass.model import TranscriptMeta
    from claudeglass.parse import parse_transcript
    from helpers import attachment_line, write_jsonl

    def queued(prompt, at):
        line = attachment_line(
            "queued_command", prompt=prompt, commandMode="prompt", origin={"kind": "human"}, timestamp=_ts(at)
        )
        line["timestamp"] = _ts(at + 15)
        return line

    path = tmp_path / "queued.jsonl"
    write_jsonl(path, [
        _ask(1, "build the parser in full, from the spec, with tests for every rule in it"),
        _reply(2, tool_use_block("Edit", "tu_1", {"file_path": "src/p.py", "old_string": "a", "new_string": "b"})),
        user_block_line([tool_result_block("tu_1", "ok")], timestamp=_ts(3)),
        _reply(4, tool_use_block("Bash", "tu_2", {"command": "pytest"})),
        queued("no, that's wrong, it's broken", 5),
        user_block_line([tool_result_block("tu_2", "ok")], timestamp=_ts(30)),
        _reply(31, text="Done."),
        _ask(40, "no, that is wrong, it is broken again"),
        _reply(41, tool_use_block("Edit", "tu_3", {"file_path": "src/p.py", "old_string": "b", "new_string": "c"})),
    ])
    top = parse_transcript(path, TranscriptMeta(path=str(path), kind="top-level"))
    cycles = capture.prompt_cycles(top)
    assert len(cycles) == 2
    [piece] = pieces_of(cycles)
    # The queued message is part of the first cycle: no cycle of its own, no piece.
    assert piece.cycles == 2 and piece.unsegmented
    assert (piece.delivered, piece.rework) == (True, 1)


def test_a_corpus_of_sessions_is_cut_into_pieces_with_handoffs_joined(tmp_path):
    top = _top(tmp_path, [_ask(1, "do the first thing"), _reply(2), _ask(10, "and the second"), _reply(11)])
    one = NS(top=top, subs=[], session_id="s1", project_dir="/work/app", slug="app", workflows=())
    other = NS(top=None, subs=[], session_id="s2", project_dir="/work/app", slug="app", workflows=())
    [piece] = pieces.corpus_pieces(NS(sessions=[one, other]))
    assert (piece.session_ids, piece.cycles, piece.project) == (("s1",), 2, "/work/app")


def test_a_corpus_rebuilt_from_the_store_has_only_a_slug_to_say_the_project(tmp_path):
    top = _top(tmp_path, [_ask(1, "do the first thing"), _reply(2)])
    store = NS(top=top, subs=[], session_id="s1", project_dir="", slug="app", workflows=())
    assert pieces.corpus_pieces(NS(sessions=[store]))[0].project == "app"


# -- normalising rework ---------------------------------------------------------------


def _piece(**kw) -> WorkPiece:
    return WorkPiece(**kw)


def test_rework_is_normalised_per_substantive_cycle_and_per_segmented_piece():
    done = [
        _piece(cycles=6, substantive=4, rework=2, rework_cost=0.5, unsegmented=False),
        _piece(cycles=3, substantive=3, rework=0, unsegmented=False),
        # A whole session of unknown structure: its cycles count, it is no piece.
        _piece(cycles=20, substantive=10, rework=5, rework_cost=1.5, unsegmented=True),
    ]
    rate = pieces.rework_rate(done)
    assert (rate.pieces, rate.segmented, rate.reworked) == (3, 2, 1)
    assert (rate.substantive, rate.rework) == (17, 7)
    assert rate.rework_cost == pytest.approx(2.0)
    assert rate.per_cycle == pytest.approx(7 / 17)
    assert rate.per_piece == pytest.approx(0.5)


def test_a_rate_over_nothing_is_none_not_zero():
    rate = pieces.rework_rate([])
    assert (rate.per_cycle, rate.per_piece) == (None, None)
    only_unsegmented = pieces.rework_rate([_piece(substantive=2, rework=1, unsegmented=True)])
    assert only_unsegmented.per_piece is None and only_unsegmented.per_cycle == 0.5


def test_rework_by_level_splits_cycles_by_their_word_and_pieces_by_the_hardest():
    cycles = [
        _msg(0, tag=_tag(level="easy", size="s")),
        _build(10),
        _fix(20, human_correction=True, tag=_tag(level="hard", size="m", shift="fix")),
        _msg(30, tag=_tag(level="easy", shift="new")),
        _fix(35, files=("a",), tag=_tag(level="easy", size="s")),
    ]
    pieces_ = pieces_of(cycles)
    assert [(p.level, p.rework) for p in pieces_] == [("hard", 1), ("easy", 0)]
    by_level = pieces.rework_by_level(pieces_)
    assert list(by_level) == ["easy", "hard", "unknown"]
    assert (by_level["hard"].substantive, by_level["hard"].rework) == (1, 1)
    assert (by_level["easy"].substantive, by_level["easy"].rework) == (3, 0)
    assert (by_level["hard"].pieces, by_level["easy"].pieces) == (1, 1)
    by_size = pieces.rework_by_size(pieces_)
    assert list(by_size) == ["s", "m", "unknown"]
    assert by_size["m"].rework == 1


def test_rework_by_a_word_prices_the_rework_cycles_of_that_word(pricing):
    cycles = [_build(0), _fix(10, human_correction=True, tag=_tag(level="hard", shift="fix"))]
    by_level = pieces.rework_by_level(pieces_of(cycles, pricing))
    assert by_level["hard"].rework_cost == pytest.approx(REPLY)
    assert by_level["unknown"].rework_cost == 0.0


def test_the_words_pieces_are_counted_under_are_the_catalogues():
    assert pieces.STARTS == ("start", "clear", "new", "gap")
    assert pieces.CONFIDENCE == ("high", "low")
    assert pieces.SOURCES == ("feedback", "Claude tag", "Haiku tag", "inferred")
    assert pieces.UNKNOWN == "unknown"
    assert "not_reported" in pieces.CAUSES and all(" " not in c for c in pieces.CAUSES)

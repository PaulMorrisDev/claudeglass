"""Q1 part D: the checks that read the answers capture collects
(``capture_checks``): the summary question ``lost`` and its check. The
evidence reads the salted read and edit hashes of a turn, which the
dashboard gets from the store, not from the transcript files.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from claudeglass import parse
from claudeglass.parse import parse_transcript
from claudeglass.model import TranscriptMeta
from claudeglass.service.rebuild import corpus_from_store

from helpers import (
    attachment_line,
    store_with_older_sessions,
    system_line,
    tool_result_block,
    tool_use_block,
    turn_line,
    user_block_line,
    user_str_line,
    write_jsonl,
)

_COMMON = {"cwd": "C:/work/proj"}


def _stamper(when: datetime):
    def stamp(seconds: int) -> str:
        return (when + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    return stamp


def _read_and_edit_session(session_id: str, when: datetime) -> list[dict]:
    """A message, a reply that reads ``a.py`` and one that edits ``b.py``."""
    stamp = _stamper(when)
    common = {"sessionId": session_id, **_COMMON}
    return [
        user_str_line("look at a and change b", timestamp=stamp(0), **common),
        turn_line(
            content=[tool_use_block("Read", "tu_read", {"file_path": "C:/work/proj/a.py"})],
            timestamp=stamp(1),
            **common,
        ),
        user_block_line([tool_result_block("tu_read", "contents")], timestamp=stamp(2), **common),
        turn_line(
            content=[
                tool_use_block(
                    "Edit", "tu_edit", {"file_path": "C:/work/proj/b.py", "old_string": "x", "new_string": "y"}
                )
            ],
            timestamp=stamp(3),
            **common,
        ),
        user_block_line([tool_result_block("tu_edit", "edited")], timestamp=stamp(4), **common),
        turn_line(content=[{"type": "text", "text": "done"}], timestamp=stamp(5), **common),
    ]


def test_the_read_and_edit_hashes_survive_the_store(tmp_path):
    # Part D's evidence (a file read again after a summary) needs both hashes on the dashboard's path,
    # which is corpus_from_store: a store that dropped them would leave only corrections as evidence.
    parse.set_salt(b"s" * 32)
    when = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=2)
    lines = _read_and_edit_session("sess-a", when)
    path = tmp_path / "direct.jsonl"
    write_jsonl(path, lines)
    direct = parse_transcript(path, TranscriptMeta(path=str(path), kind="top-level", session_id="sess-a"))
    wanted = [(t.read_target_hashes, t.edit_target_hashes) for t in direct.turns]
    assert any(reads for reads, _ in wanted) and any(edits for _, edits in wanted)

    store, _ = store_with_older_sessions(tmp_path / "store", {"sess-a": lines}, older=set())
    [bundle] = corpus_from_store(store).sessions
    assert [(t.read_target_hashes, t.edit_target_hashes) for t in bundle.top.turns] == wanted


# -- the summary question: which cycle answers, and what counts as evidence ------------------------------------

SALT = b"s" * 32
#: A session whose notes asked for these keys: a compaction's note lists prior, so ``lost`` is kept.
HEAD = "ClaudeGlass metrics capture (cg-cap v1 task,shift,prior): ..."


def _clock(seconds: int) -> str:
    return (datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )


def _head(session_id: str) -> dict:
    wrapped = f"<system-reminder>\nSessionStart hook additional context: {HEAD}\n</system-reminder>"
    return attachment_line(
        "hook_additional_context", rendered=wrapped, content=[HEAD], hookName="SessionStart",
        hookEvent="SessionStart", toolUseID="SessionStart", sessionId=session_id, **_COMMON,
    )


class _Session:
    """Lines of one transcript, each a second after the one before it."""

    def __init__(self, session_id: str):
        self.id = session_id
        self.lines = [_head(session_id)]
        self.at = 0
        self.uses = 0
        #: The context every reply from here on read.
        self.ctx = 0

    def context(self, tokens: int) -> "_Session":
        self.ctx = tokens
        return self

    def later(self, seconds: int) -> "_Session":
        """Let ``seconds`` pass before the next line."""
        self.at += seconds
        return self

    def _stamp(self) -> dict:
        self.at += 1
        return {"sessionId": self.id, "timestamp": _clock(self.at), **_COMMON}

    def typed(self, text: str) -> "_Session":
        self.lines.append(user_str_line(text, origin={"kind": "human"}, **self._stamp()))
        return self

    def say(self, text: str, tag: str = "") -> "_Session":
        body = f"{text}\n{tag}" if tag else text
        self.lines.append(
            turn_line(content=[{"type": "text", "text": body}], cache_read_input_tokens=self.ctx, **self._stamp())
        )
        return self

    def use(self, tool: str, path: str) -> "_Session":
        self.uses += 1
        use_id = f"tu_{self.id}_{self.uses}"
        target = {"file_path": f"C:/work/proj/{path}"}
        if tool == "Edit":
            target.update(old_string="x", new_string="y")
        self.lines.append(
            turn_line(
                content=[tool_use_block(tool, use_id, target)], cache_read_input_tokens=self.ctx, **self._stamp()
            )
        )
        self.lines.append(user_block_line([tool_result_block(use_id, "ok")], **self._stamp()))
        return self

    def boundary(self) -> "_Session":
        self.lines.append(system_line(
            "compact_boundary", compactMetadata={"trigger": "auto", "preTokens": 150_000, "postTokens": 20_000},
            **self._stamp(),
        ))
        return self

    def queued_correction(self) -> "_Session":
        """A message you typed while Claude worked, saying it was wrong."""
        self.uses += 1
        use_id = f"tu_{self.id}_{self.uses}"
        self.lines.append(turn_line(content=[tool_use_block("Bash", use_id, {"command": "ls"})], **self._stamp()))
        self.lines.append(attachment_line(
            "queued_command", prompt="no, that's wrong, it's broken", commandMode="prompt",
            origin={"kind": "human"}, **self._stamp(),
        ))
        self.lines.append(user_block_line([tool_result_block(use_id, "ok")], **self._stamp()))
        return self


def _parsed(tmp_path, session: _Session):
    parse.set_salt(SALT)
    path = tmp_path / f"{session.id}.jsonl"
    write_jsonl(path, session.lines)
    return parse_transcript(path, TranscriptMeta(path=str(path), kind="top-level", session_id=session.id))


def _answers(top):
    from claudeglass import capture, capture_checks

    return capture_checks.summary_answers(top, capture.prompt_cycles(top))


def test_summary_answers_take_the_cycle_after_each_boundary(tmp_path):
    from claudeglass.capture_checks import SummaryAnswer

    # Two summaries inside one cycle give one answer, from the cycle's own tag; a lost word in a cycle that
    # follows no summary is ignored; a summary before the first reply (a continued session's) is not this one's.
    session = (
        _Session("sess-a")
        .typed("start").use("Read", "a.py").say("one")
        .boundary().say("two", "[cg: task=bugfix shift=new prior=needed]")
        .boundary().say("three", "[cg: task=bugfix shift=new prior=needed lost=yes]")
        .typed("next").say("four", "[cg: task=bugfix shift=build prior=needed lost=no]")
        .typed("and then").say("five", "[cg: task=bugfix shift=build prior=needed]")
        .boundary().say("six", "[cg: task=bugfix shift=build prior=needed]")
    )
    answers = _answers(_parsed(tmp_path, session))
    assert [a.lost for a in answers] == ["yes", None]
    assert all(isinstance(a, SummaryAnswer) for a in answers)
    inherited = _Session("sess-b")
    inherited.lines.insert(1, system_line("compact_boundary", **inherited._stamp()))
    inherited.typed("start").say("one", "[cg: task=bugfix shift=new prior=none lost=yes]")
    assert _answers(_parsed(tmp_path, inherited)) == []
    assert _answers(_parsed(tmp_path, _Session("sess-c").typed("start").say("one"))) == []


def _evidence_sessions() -> dict[str, tuple[_Session, bool]]:
    """Name -> (the session, whether its one answering cycle holds the evidence)."""
    tag = "[cg: task=bugfix shift={} prior=needed lost=no]"

    def opened(name: str) -> _Session:
        return _Session(name).typed("start").use("Read", "a.py").say("one", tag.format("new")).typed("next")

    return {
        "a file read before is read again": (
            opened("reread").boundary().use("Read", "a.py").say("done", tag.format("build")), True
        ),
        "a file read before is edited": (
            opened("edited").boundary().use("Edit", "a.py").say("done", tag.format("build")), True
        ),
        "a new file": (opened("other").boundary().use("Read", "c.py").say("done", tag.format("build")), False),
        "the file was read in the cycle's own reply before the summary": (
            _Session("early").typed("start").use("Read", "a.py").boundary().say("done", tag.format("new")), False
        ),
        "the next message corrects it": (
            opened("said").boundary().say("done", tag.format("build"))
            .typed("no, that's wrong, it's broken").say("sorry"),
            True,
        ),
        "the next message is a fix": (
            opened("fix").boundary().say("done", tag.format("build")).typed("tweak it").say("ok", tag.format("fix")),
            True,
        ),
        "a correction typed while it worked": (opened("queued").boundary().queued_correction().say("done"), True),
        "the next message goes on": (
            opened("onward").boundary().say("done", tag.format("build"))
            .typed("thanks, now the docs").say("ok", tag.format("build")),
            False,
        ),
    }


def test_evidence_is_a_file_read_again_or_a_correction(tmp_path):
    from claudeglass import capture, capture_checks

    sessions = _evidence_sessions()
    for name, (session, evidence) in sessions.items():
        [answer] = _answers(_parsed(tmp_path, session))
        assert answer.evidence is evidence, name
    # The same through the store, which is what the dashboard reads: the salted hashes come back with it.
    parse.set_salt(SALT)
    store, _ = store_with_older_sessions(tmp_path / "store", {s.id: s.lines for s, _ in sessions.values()}, older=set())
    by_id = {b.session_id: b for b in corpus_from_store(store).sessions}
    assert set(by_id) == {s.id for s, _ in sessions.values()}
    for name, (session, evidence) in sessions.items():
        top = by_id[session.id].top
        [answer] = capture_checks.summary_answers(top, capture.prompt_cycles(top))
        assert answer.evidence is evidence, name


def test_lost_check_needs_ten_each_half_and_a_twenty_point_margin():
    from claudeglass.capture_checks import LostCheck, SummaryAnswer, lost_check

    def answers(yes, yes_evidence, no, no_evidence):
        return (
            [SummaryAnswer("yes", n < yes_evidence) for n in range(yes)]
            + [SummaryAnswer("no", n < no_evidence) for n in range(no)]
            + [SummaryAnswer(None, True)] * 7
        )

    assert lost_check(answers(10, 6, 10, 3)) == LostCheck(yes=10, yes_evidence=6, no=10, no_evidence=3, passed=True)
    # Not enough margin, too few of either answer, too little evidence behind the yes.
    assert lost_check(answers(10, 6, 10, 5)).passed is False
    assert lost_check(answers(9, 9, 20, 0)).passed is False
    assert lost_check(answers(20, 20, 9, 0)).passed is False
    assert lost_check(answers(10, 4, 10, 0)).passed is False
    # Exactly twenty points, which float arithmetic reads as 19.999...
    assert lost_check(answers(10, 7, 10, 5)).passed is True
    # Half of the yes answers is the least, and what Claude did not answer is left out.
    assert lost_check(answers(10, 5, 10, 0)).passed is True
    assert lost_check([SummaryAnswer(None, True)] * 50) == LostCheck(0, 0, 0, 0, False)
    assert lost_check([]).passed is False


# -- the break question: the prior word at each break of an hour on a context of 20,000 -------------------------

TAG = "[cg: task=bugfix shift=new prior={}]"


def _break_session(session_id: str = "brk") -> _Session:
    """One break of each kind, then the gaps that are no break: ``prior`` answered ``needed`` with a file
    read again, ``some`` with a new file, ``none``, and not answered; then a gap under the hour, a break
    after a small context, and a break across a summary."""
    return (
        _Session(session_id).context(30_000)
        .typed("start").use("Read", "a.py").say("one", TAG.format("needed"))
        .later(4_000).typed("again").use("Read", "a.py").say("two", TAG.format("needed"))
        .later(4_000).typed("other").use("Read", "c.py").say("three", TAG.format("some"))
        .later(4_000).typed("fresh").say("four", TAG.format("none"))
        .later(4_000).typed("quiet").say("five")
        .later(1_800).typed("soon").say("six", TAG.format("needed"))
        .context(5_000).typed("small").say("seven", TAG.format("needed"))
        .later(4_000).typed("after small").say("eight", TAG.format("needed"))
        .context(30_000).typed("big again").say("nine", TAG.format("needed"))
        .later(4_000).boundary().typed("after a summary").say("ten", TAG.format("needed"))
    )  # fmt: skip


def _breaks(top):
    from claudeglass import capture, capture_checks

    return capture_checks.break_answers(top, capture.prompt_cycles(top))


def test_break_answers_take_the_prior_word_at_each_break_and_whether_a_file_came_back(tmp_path):
    from claudeglass.capture_checks import BreakAnswer

    answers = _breaks(_parsed(tmp_path, _break_session()))
    # The gap of 1,800 s, the break after 5,000 tokens and the break across a summary are no breaks.
    assert answers == [
        BreakAnswer("needed", True),
        BreakAnswer("some", False),
        BreakAnswer("none", False),
        BreakAnswer(None, False),
    ]
    assert _breaks(_parsed(tmp_path, _Session("calm").typed("start").say("one").typed("more").say("two"))) == []


def test_break_answers_leave_out_a_wait_for_a_usage_limit(tmp_path):
    top = _parsed(tmp_path, _break_session("flags"))
    wanted = len(_breaks(top))
    opened = [t for t in top.turns if t.gap_s is not None and t.gap_s >= 3_600 and not t.is_synthetic]
    assert len(opened) >= wanted
    # The first break's reply marked as the end of a wait for a limit.
    opened[0].gap_cause = "limit"
    assert len(_breaks(top)) == wanted - 1


def test_a_shell_command_between_the_old_reply_and_the_typed_message_does_not_end_the_break(tmp_path):
    # A line you did not type (a ! shell command) came in the gap; the prompt that returned is still typed.
    session = _Session("bash").context(30_000).typed("start").use("Read", "a.py").say("one", TAG.format("needed"))
    session.later(7_200)
    session.lines.append(user_str_line("<bash-input>ls</bash-input>", **session._stamp()))
    session.lines.append(
        user_str_line("<bash-stdout>a.py</bash-stdout><bash-stderr></bash-stderr>", **session._stamp())
    )
    session.typed("back to it").use("Read", "a.py").say("two", TAG.format("needed"))
    top = _parsed(tmp_path, session)
    opening = [t for t in top.turns if t.gap_s is not None and t.gap_s >= 7_200]
    assert opening and all(t.preceding_not_typed for t in opening)
    from claudeglass.capture_checks import BreakAnswer

    assert _breaks(top) == [BreakAnswer("needed", True)]


# -- the joins: tags against what became of the work ----------------------------------------------------------


def _fact(check, *, redone=False, changed=True, tagged=True):
    from claudeglass.habits import CycleFact
    from claudeglass.model import CaptureTag

    return CycleFact(
        session_id="s", ts=None, week="", cost=0.0, turns=1, redone=redone, changed=changed,
        tag=CaptureTag(check=check) if tagged else None,
    )  # fmt: skip


def test_redone_by_check_counts_the_changed_cycles_that_were_tagged_with_a_check_word():
    from claudeglass.capture_checks import redone_by_check

    cycles = [
        _fact("full", redone=True), _fact("full"), _fact("full", changed=False, redone=True),
        _fact("run", redone=True), _fact("run", redone=True), _fact(None, redone=True), _fact("manual", tagged=False),
        _fact("not a word", redone=True),
    ]  # fmt: skip
    assert redone_by_check(cycles) == {"full": {"cycles": 2, "redone": 1}, "run": {"cycles": 2, "redone": 2}}
    assert redone_by_check([]) == {}


def _piece(level, ids, *, rework=0, admitted=0):
    from claudeglass.pieces import WorkPiece

    return WorkPiece(cycle_ids=tuple(ids), level=level, rework=rework, admitted=admitted)


def _turn(model, effort):
    from types import SimpleNamespace as NS

    return NS(model=model, effort=effort)


def _joined_world():
    """Three hard pieces (two reworked, one admitted), a normal one and a piece whose cycles carried no level;
    efforts and models as the turns of their cycles say, the most common of each deciding."""
    from types import SimpleNamespace as NS

    cycles = {
        ("s", 0): NS(turns=[_turn("claude-opus-5-5", "xhigh"), _turn("claude-opus-5-5", "xhigh"), _turn("claude-haiku-4-5", None)]),
        ("s", 1): NS(turns=[_turn("claude-sonnet-5", "high")]),
        ("s", 2): NS(turns=[_turn("claude-opus-5-5", "high"), _turn("", "low")]),
        ("s", 3): NS(turns=[_turn("some-other-model", "ultra")]),
        ("s", 4): NS(turns=[_turn("claude-fable-5-2", None)]),
        ("s", 5): NS(turns=[_turn("claude-opus-5-5", "medium")]),
    }  # fmt: skip
    pieces = [
        _piece("hard", [("s", 0)], rework=2, admitted=1),
        _piece("hard", [("s", 1), ("s", 2)], rework=1),
        _piece("hard", [("s", 3)]),
        _piece("normal", [("s", 4)], admitted=2),
        _piece("", [("s", 5)], rework=1),
    ]
    return pieces, cycles


def test_pieces_by_level_count_the_reworked_and_the_admitted_ones():
    from claudeglass.capture_checks import pieces_by_level

    pieces, _cycles = _joined_world()
    assert pieces_by_level(pieces) == {
        "hard": {"pieces": 3, "reworked": 2, "admitted": 1},
        "normal": {"pieces": 1, "reworked": 0, "admitted": 1},
    }


def test_level_by_effort_takes_the_effort_most_of_a_pieces_turns_ran_at():
    from claudeglass.capture_checks import level_by_effort

    pieces, cycles = _joined_world()
    # The first piece ran at xhigh (two turns of three); the second at high (the turn with no model is not
    # counted); a word the list does not have, and a turn with no effort, are the default.
    assert level_by_effort(pieces, cycles) == {
        "hard": {"xhigh": {"pieces": 1, "reworked": 1}, "high": {"pieces": 1, "reworked": 1}, "default": {"pieces": 1, "reworked": 0}},
        "normal": {"default": {"pieces": 1, "reworked": 0}},
    }  # fmt: skip


def test_level_by_family_takes_the_model_family_most_of_a_pieces_turns_ran_on():
    from claudeglass.capture_checks import level_by_family

    pieces, cycles = _joined_world()
    # Opus ran two turns of the first piece's three; the second piece's two turns tie, and the first one wins;
    # a model with no family is other.
    assert level_by_family(pieces, cycles) == {
        "hard": {"opus": {"pieces": 1, "reworked": 1}, "sonnet": {"pieces": 1, "reworked": 1}, "other": {"pieces": 1, "reworked": 0}},
        "normal": {"fable": {"pieces": 1, "reworked": 0}},
    }  # fmt: skip


def test_the_joins_leave_out_a_piece_whose_cycles_are_gone():
    from claudeglass.capture_checks import level_by_effort, level_by_family

    pieces = [_piece("hard", [("gone", 0)], rework=1)]
    assert level_by_effort(pieces, {}) == {} and level_by_family(pieces, {}) == {}


def test_habits_collect_gathers_the_answers_of_every_main_session(tmp_path):
    from pathlib import Path
    from types import SimpleNamespace as NS

    from claudeglass import habits
    from claudeglass.pricing import load_pricing

    tag = "[cg: task=bugfix shift=new prior=needed lost={}]"
    sessions = [
        _Session("sess-x").typed("start").say("one").boundary().say("two", tag.format("yes")),
        _Session("sess-y").typed("start").say("one").boundary().say("two", tag.format("no")),
        _Session("sess-z").typed("start").say("one", tag.format("no")),
    ]
    bundles = [
        NS(top=_parsed(tmp_path, s), subs=[], session_id=s.id, project_dir="p", slug="p", workflows=[])
        for s in sessions
    ]
    pricing = load_pricing(path=Path(__file__).resolve().parent / "fixtures" / "pricing_min.toml")
    h = habits.collect(NS(sessions=bundles), pricing)
    assert [a.lost for a in h.summary_answers] == ["yes", "no"]
    # Two answers are far short of what the check needs, so the dashboard has no table for them.
    assert "habits_summary_losses" not in [t.name for t in habits.section_from(h).tables]

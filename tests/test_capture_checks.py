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

    def _stamp(self) -> dict:
        self.at += 1
        return {"sessionId": self.id, "timestamp": _clock(self.at), **_COMMON}

    def typed(self, text: str) -> "_Session":
        self.lines.append(user_str_line(text, origin={"kind": "human"}, **self._stamp()))
        return self

    def say(self, text: str, tag: str = "") -> "_Session":
        body = f"{text}\n{tag}" if tag else text
        self.lines.append(turn_line(content=[{"type": "text", "text": body}], **self._stamp()))
        return self

    def use(self, tool: str, path: str) -> "_Session":
        self.uses += 1
        use_id = f"tu_{self.id}_{self.uses}"
        target = {"file_path": f"C:/work/proj/{path}"}
        if tool == "Edit":
            target.update(old_string="x", new_string="y")
        self.lines.append(turn_line(content=[tool_use_block(tool, use_id, target)], **self._stamp()))
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

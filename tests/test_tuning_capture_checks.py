"""The tuning export's ``capture_checks`` block (Q1 part G): the joins of what Claude tagged to what became of the
work, as counts, and the two checks on the questions capture asks at a moment (the summary question and the break).

The shared helpers and worlds are ``test_tuning``'s; the transcripts are written with ``test_capture_checks``'s
session builder, so each count can be read off the story it is made from.
"""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace as NS

import pytest

from claudeglass import capture_catalogue as catalogue
from claudeglass import parse, tuning
from claudeglass.capture_checks import BreakAnswer, SummaryAnswer
from claudeglass.config import Config
from claudeglass.corpus import load_corpus
from claudeglass.habits import CycleFact
from claudeglass.model import CaptureTag
from claudeglass.pieces import WorkPiece
from claudeglass.pricing import load_pricing

from helpers import assert_privacy_deep, attachment_line, write_jsonl
from test_capture_checks import SALT, _COMMON, _Session
from test_tuning import DAYS, PRIVATE, TODAY, World, _build, _make_world, _plain, _put, _sample

#: What the story's notes asked for, so every key of its tags is kept.
KEYS = "task,level,size,shift,check,prior"


@pytest.fixture(autouse=True)
def _salt():
    parse.set_salt(SALT)


def _tagged(session_id: str) -> _Session:
    """A session whose first note asked for the keys of ``KEYS``."""
    session = _Session(session_id)
    head = f"ClaudeGlass metrics capture (cg-cap v1 {KEYS}): ..."
    wrapped = f"<system-reminder>\nSessionStart hook additional context: {head}\n</system-reminder>"
    session.lines[0] = attachment_line(
        "hook_additional_context", rendered=wrapped, content=[head], hookName="SessionStart",
        hookEvent="SessionStart", toolUseID="SessionStart", sessionId=session_id, **_COMMON,
    )  # fmt: skip
    return session


def _story(session_id: str = "story") -> _Session:
    """One piece of hard work: built and checked by running the suite, fixed (the first is redone), notes
    written and a summary made, the summary question answered ``yes`` with a file read again, and the user back
    after more than an hour, with that file read again."""
    return (
        _tagged(session_id).context(30_000)
        .typed("build the secret-widget").use("Edit", "secret_widget.py")
        .say("done", "[cg: task=feature level=hard shift=new size=m check=full prior=needed]")
        .typed("that is wrong, fix it").use("Edit", "secret_widget.py")
        .say("fixed", "[cg: task=bugfix level=hard shift=fix check=manual prior=needed why=missed]")
        .typed("now the notes").use("Edit", "notes.py").boundary().use("Read", "secret_widget.py")
        .say("written", "[cg: task=docs level=normal shift=build check=none prior=needed lost=yes]")
        .later(4_000).typed("come back to the widget").use("Read", "secret_widget.py")
        .say("looked", "[cg: task=docs level=easy shift=build prior=needed]")
    )  # fmt: skip


def _plain_session(session_id: str = "plain") -> _Session:
    """A session with no tag and no summary."""
    return _Session(session_id).typed("what is in the secret-widget").say("a widget").typed("thanks").say("welcome")


def _world(root, *sessions: _Session) -> World:
    project = root / "projects" / "proj-secret"
    project.mkdir(parents=True)
    config_dir, claude_root = root / "claudeglass", root / "claude"
    config_dir.mkdir()
    claude_root.mkdir()
    for session in sessions:
        write_jsonl(project / f"{session.id}.jsonl", session.lines)
    return World(load_corpus([project]), load_pricing(), Config(tz="UTC"), config_dir, claude_root, root)


def _valid(block: dict) -> None:
    assert tuning.validate(_put(_sample(), "capture_checks", block)) == []


# -- the built block ----------------------------------------------------------------------------------------

_STORY = {
    "lost": {"cycles": 1, "evidence": 1, "yes": {"answers": 1, "evidence": 1}, "no": {"answers": 0, "evidence": 0}},
    "breaks": {"cycles": 1, "reused": 1, "prior": {"needed": {"answers": 1, "reused": 1}}},
    "check": {
        "full": {"cycles": 1, "redone": 1},
        "manual": {"cycles": 1, "redone": 0},
        "none": {"cycles": 1, "redone": 0},
    },
    "level": {"hard": {"pieces": 1, "reworked": 1, "admitted": 0}},
    "level_effort": {"hard": {"default": {"pieces": 1, "reworked": 1}}},
    "level_family": {"hard": {"sonnet": {"pieces": 1, "reworked": 1}}},
}


def test_the_capture_checks_block_is_valid_and_counts_only(tmp_path):
    w = _world(tmp_path, _story())
    doc = _build(w)
    assert tuning.validate(doc) == []
    assert doc["capture_checks"] == _STORY
    assert list(doc)[-1] == "capture_checks"
    assert list(doc).index("capture_checks") > list(doc).index("figures")
    text = tuning.dumps(doc)
    for name in ("secret", "widget", "proj-secret", "notes.py", str(tmp_path)):
        assert name not in text, name
    assert_privacy_deep(doc)
    assert tuning.validate(json.loads(text)) == []
    # Every key is a word of a closed list, every leaf a count.
    assert set(doc["capture_checks"]["check"]) <= set(catalogue.TAG_VOCAB["check"])
    assert set(doc["capture_checks"]["level_effort"]["hard"]) <= set(tuning.capture_checks_mod.EFFORTS)
    assert set(doc["capture_checks"]["level_family"]["hard"]) <= set(tuning.capture_checks_mod.FAMILIES)


def test_a_world_with_planted_names_builds_a_block_that_names_none_of_them(tmp_path):
    w = _make_world(tmp_path / "a")
    doc = _build(w)
    assert tuning.validate(doc) == []
    text = tuning.dumps(doc)
    for name in PRIVATE:
        if not name.startswith("claude-"):
            assert name not in text, name
    assert_privacy_deep(doc)


def test_the_block_is_left_out_without_tags_or_summaries(tmp_path):
    bare = _world(tmp_path / "a", _plain_session())
    doc = _build(bare)
    assert "capture_checks" not in doc
    assert tuning.validate(doc) == []
    assert tuning._capture_checks(tuning._Ctx(bare.corpus, bare.config, bare.pricing, bare.config_dir, DAYS, TODAY, bare.claude_root)) is None
    # Tags and no summary or break: the other parts are there, the two moments are not, and empty maps are left out.
    tagged = _world(
        tmp_path / "b",
        _tagged("t").context(30_000).typed("build it").use("Edit", "a.py")
        .say("done", "[cg: task=feature level=hard shift=new size=m check=run prior=needed]"),
    )  # fmt: skip
    block = _build(tagged)["capture_checks"]
    assert set(block) == {"check", "level", "level_effort", "level_family"}
    assert block["check"] == {"run": {"cycles": 1, "redone": 0}}
    # A summary that went unanswered still counts as one, with no word behind it.
    unanswered = _world(
        tmp_path / "c",
        _tagged("u").typed("start").use("Edit", "a.py").say("one", "[cg: task=feature shift=new prior=needed]")
        .typed("next").boundary().say("two", "[cg: task=feature shift=build prior=needed]"),
    )  # fmt: skip
    assert _build(unanswered)["capture_checks"]["lost"] == {
        "cycles": 1, "evidence": 0, "yes": {"answers": 0, "evidence": 0}, "no": {"answers": 0, "evidence": 0},
    }  # fmt: skip


# -- the reference figures ---------------------------------------------------------------------------------


def _facts(word: str, cycles: int, redone: int) -> list[CycleFact]:
    return [
        CycleFact(session_id="s", ts=None, week="", cost=0.0, turns=1, tag=CaptureTag(check=word), changed=True, redone=n < redone)
        for n in range(cycles)
    ]  # fmt: skip


def _setup_pieces(level: str, groups) -> tuple[list[WorkPiece], dict]:
    """Pieces of ``level`` from ``(effort, model, pieces, reworked, admitted)`` groups, each piece with one
    cycle of one turn."""
    pieces, cycles = [], {}
    for effort, model, count, reworked, admitted in groups:
        for n in range(count):
            key = (f"{level}-{effort}-{model}", n)
            cycles[key] = NS(turns=[NS(model=model, effort=effort)])
            pieces.append(WorkPiece(level=level, cycle_ids=(key,), rework=int(n < reworked), admitted=int(n < admitted)))
    return pieces, cycles


def test_the_reference_figures(monkeypatch):
    """The figures of ``expect/joins-30d.txt`` (the designer's script on the home data, 30 days), built as a
    synthetic world of the same shape: the ``check`` and ``level`` maps are the table's, and the moments are the
    example block's."""
    facts = [
        *_facts("full", 13, 3), *_facts("manual", 22, 2), *_facts("none", 16, 1), *_facts("run", 23, 4),
        *_facts("targeted", 16, 0),
        # A message that changed nothing, and one with no check word, are in no row.
        CycleFact(session_id="s", ts=None, week="", cost=0.0, turns=1, tag=CaptureTag(check="manual"), changed=False, redone=True),
        CycleFact(session_id="s", ts=None, week="", cost=0.0, turns=1, tag=CaptureTag(task="ops"), changed=True, redone=True),
    ]  # fmt: skip
    easy, easy_cycles = _setup_pieces("easy", [("xhigh", "claude-opus-5-5", 2, 0, 0), ("xhigh", "claude-sonnet-5", 1, 0, 0), ("medium", "claude-opus-5-5", 1, 0, 0)])
    normal, normal_cycles = _setup_pieces("normal", [("xhigh", "claude-opus-5-5", 4, 0, 0)])
    hard_a, hard_a_cycles = _setup_pieces("hard", [("xhigh", "claude-opus-5-5", 12, 6, 5)])
    hard_b, hard_b_cycles = _setup_pieces("hard", [("high", "claude-opus-5-5", 1, 0, 0), ("high", "claude-fable-5-2", 1, 0, 0)])
    # Easy: 4 pieces, 3 of them xhigh (one of those on Sonnet, one on Opus at medium). Hard: 14, 12 at xhigh.
    ctx = NS(
        habits=NS(
            cycles=facts,
            work_pieces=[*easy, *normal, *hard_a, *hard_b],
            summary_answers=[SummaryAnswer(None, n < 35) for n in range(70)],
        ),
        cycles_by_id={**easy_cycles, **normal_cycles, **hard_a_cycles, **hard_b_cycles},
        sessions=[],
    )  # fmt: skip
    breaks = (
        [BreakAnswer("needed", n < 5) for n in range(14)]
        + [BreakAnswer("some", False)] * 3
        + [BreakAnswer(None, n < 8) for n in range(26)]
    )
    monkeypatch.setattr(tuning, "_break_answers", lambda _ctx: breaks)
    block = tuning._capture_checks(ctx)
    _valid(block)
    assert block["check"] == {
        "full": {"cycles": 13, "redone": 3},
        "manual": {"cycles": 22, "redone": 2},
        "none": {"cycles": 16, "redone": 1},
        "run": {"cycles": 23, "redone": 4},
        "targeted": {"cycles": 16, "redone": 0},
    }
    assert block["level"] == {
        "easy": {"pieces": 4, "reworked": 0, "admitted": 0},
        "normal": {"pieces": 4, "reworked": 0, "admitted": 0},
        "hard": {"pieces": 14, "reworked": 6, "admitted": 5},
    }
    assert block["level_effort"] == {
        "easy": {"xhigh": {"pieces": 3, "reworked": 0}, "medium": {"pieces": 1, "reworked": 0}},
        "normal": {"xhigh": {"pieces": 4, "reworked": 0}},
        "hard": {"xhigh": {"pieces": 12, "reworked": 6}, "high": {"pieces": 2, "reworked": 0}},
    }
    assert block["level_family"] == {
        "easy": {"opus": {"pieces": 3, "reworked": 0}, "sonnet": {"pieces": 1, "reworked": 0}},
        "normal": {"opus": {"pieces": 4, "reworked": 0}},
        "hard": {"opus": {"pieces": 13, "reworked": 6}, "fable": {"pieces": 1, "reworked": 0}},
    }
    assert block["lost"] == {
        "cycles": 70, "evidence": 35, "yes": {"answers": 0, "evidence": 0}, "no": {"answers": 0, "evidence": 0},
    }  # fmt: skip
    assert block["breaks"] == {
        "cycles": 43, "reused": 13, "prior": {"needed": {"answers": 14, "reused": 5}, "some": {"answers": 3, "reused": 0}},
    }  # fmt: skip


# -- the spec node and the summary line -----------------------------------------------------------------------

_LOST = {"cycles": 20, "evidence": 11, "yes": {"answers": 10, "evidence": 6}, "no": {"answers": 10, "evidence": 3}}
_LINE = "Claude answered the summary question 20 times: yes 10 (6 with a file read again or a correction), no 10 (3)."


def test_the_spec_takes_each_part_alone_and_refuses_a_name_or_a_wrong_kind():
    _valid({"check": {"full": {"cycles": 3, "redone": 1}}})
    _valid({"lost": _LOST})
    _valid({"breaks": {"cycles": 4, "reused": 1, "prior": {"none": {"answers": 1, "reused": 0}}}})
    _valid({"level": {"easy": {"pieces": 1, "reworked": 0, "admitted": 0}}})
    _valid({"level_effort": {"easy": {"default": {"pieces": 1, "reworked": 0}}}})
    _valid({"level_family": {"easy": {"other": {"pieces": 1, "reworked": 0}}}})
    for bad in (
        {"check": {"my-private-check": {"cycles": 1, "redone": 0}}},
        {"check": {"full": {"cycles": 1, "redone": -1}}},
        {"check": {"full": {"cycles": 1.5, "redone": 0}}},
        {"level": {"unknown": {"pieces": 1, "reworked": 0, "admitted": 0}}},
        {"level_effort": {"hard": {"ultra": {"pieces": 1, "reworked": 0}}}},
        {"level_family": {"hard": {"claude-opus-5-5": {"pieces": 1, "reworked": 0}}}},
        {"lost": {"cycles": 1}},
        {"breaks": {"cycles": 1, "reused": 0, "prior": {"maybe": {"answers": 1, "reused": 0}}}},
        {"rare": {"skill": 151}},
    ):
        assert tuning.validate(_put(_sample(), "capture_checks", bad)), bad


def test_the_summary_says_how_the_summary_question_was_answered_only_when_it_was():
    doc = _put(_sample(), "capture_checks", {"lost": _LOST})
    lines = tuning.summary_text(doc).splitlines()
    assert _LINE in lines
    assert lines.index(_LINE) > max(i for i, line in enumerate(lines) if line.startswith("Cost per reply"))
    _plain(_LINE)
    # One answer reads as one.
    one = {"cycles": 5, "evidence": 1, "yes": {"answers": 1, "evidence": 1}, "no": {"answers": 0, "evidence": 0}}
    assert "Claude answered the summary question 1 time: yes 1 (1 with a file read again or a correction), no 0 (0)." in (
        tuning.summary_text(_put(_sample(), "capture_checks", {"lost": one})).splitlines()
    )
    # Summaries with no answer, and every other part, say nothing.
    asked = {"cycles": 9, "evidence": 4, "yes": {"answers": 0, "evidence": 0}, "no": {"answers": 0, "evidence": 0}}
    for block in ({"lost": asked}, {"check": {"full": {"cycles": 3, "redone": 1}}}):
        text = tuning.summary_text(_put(_sample(), "capture_checks", block))
        assert "summary question" not in text
    without = _sample()
    without.pop("capture_checks", None)
    assert "summary question" not in tuning.summary_text(without)

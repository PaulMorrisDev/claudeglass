"""Your /cl-feedback answers: the skill and its questions
(``capture_catalogue``), reading them back from a transcript (the
``[tl-fb: ...]`` line, or the AskUserQuestion result when the line is
missing), the work each answer rates (``capture.feedback_spans``), and
what the runs cost (``capture.usage``).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from claudeglass import capture, capture_catalogue as catalogue, parse
from claudeglass.capture_tags import (
    asks_for_feedback,
    feedback_from_answers,
    parse_feedback_tag,
)
from claudeglass.model import Feedback, TranscriptMeta
from claudeglass.parse import parse_transcript
from claudeglass.pricing import load_pricing, price_turn

from helpers import (
    assert_privacy,
    tool_result_block,
    tool_use_block,
    turn_line,
    user_block_line,
    user_str_line,
    write_jsonl,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MODEL = "claude-widget-9"
Q = {q.key: q for q in catalogue.FEEDBACK_QUESTIONS}


@pytest.fixture(autouse=True)
def _salt():
    parse.set_salt(b"f" * 32)


def _ts(second: int) -> str:
    return f"2026-09-18T12:{second // 60:02d}:{second % 60:02d}.000Z"


def _ask(second: int, text: str = "do it") -> dict:
    return user_str_line(text, origin={"kind": "human"}, timestamp=_ts(second))


def _reply(second: int, *blocks, text: str = "ok") -> dict:
    return turn_line(content=list(blocks) or [{"type": "text", "text": text}], model=MODEL, timestamp=_ts(second))


def _questions(questions=catalogue.FEEDBACK_QUESTIONS) -> list[dict]:
    return [
        {
            "question": q.question,
            "header": q.header,
            "multiSelect": q.multi,
            "options": [{"label": label, "description": text} for _word, label, text in q.options],
        }
        for q in questions
    ]


def _run(
    second: int,
    answers=None,
    *,
    tag: str | None = None,
    declined: bool = False,
    tu: str = "tu_q",
    handoff: str | None = None,
    skill: str = "cl-feedback",
) -> list:
    """A /cl-feedback run as Claude Code writes it: the skill you ran
    (``<command-message>`` first, then its body), Claude's question, your
    answers, and the reply. ``handoff`` adds the second call, asked after
    an approved plan, answered with that label."""
    lines = [
        user_str_line(f"<command-message>{skill}</command-message>\n<command-name>/{skill}</command-name>",
                      timestamp=_ts(second)),
        user_block_line([{"type": "text", "text": catalogue.feedback_skill_text()}], isMeta=True,
                        timestamp=_ts(second)),
        _reply(second + 1, tool_use_block("AskUserQuestion", tu, {"questions": _questions()})),
    ]
    if declined:
        lines.append(user_block_line([tool_result_block(tu, "User rejected tool use", is_error=True)],
                                     toolUseResult="User rejected tool use", timestamp=_ts(second + 2)))
        lines.append(_reply(second + 3, text="No problem."))
        return lines
    result = {"questions": _questions(), "answers": answers or {}}
    lines.append(user_block_line([tool_result_block(tu, "User has answered your questions.")],
                                 toolUseResult=result, timestamp=_ts(second + 2)))
    if handoff is not None:
        asked = _questions([catalogue.HANDOFF_QUESTION])
        lines.append(_reply(second + 2, tool_use_block("AskUserQuestion", f"{tu}_h", {"questions": asked})))
        result = {"questions": asked, "answers": {catalogue.HANDOFF_QUESTION.question: handoff}}
        lines.append(user_block_line([tool_result_block(f"{tu}_h", "User has answered your questions.")],
                                     toolUseResult=result, timestamp=_ts(second + 2)))
    thanks = "Thanks: ClaudeGlass will use this for your savings tips."
    lines.append(_reply(second + 3, text=f"Recorded.\n\n{tag}\n{thanks}" if tag else thanks))
    return lines


def _parse(tmp_path, lines, name: str = "top.jsonl"):
    path = tmp_path / name
    write_jsonl(path, lines)
    return parse_transcript(path, TranscriptMeta(path=str(path), kind="top-level"))


ANSWERS = {
    Q["outcome"].question: "Partly",
    Q["slow"].question: ["My request was unclear", "Wrong approach or rework"],
    Q["worth"].question: "About right",
    Q["helped"].question: "More context up front",
}


# -- the questions and the skill ------------------------------------------------


def test_questions_fit_ask_user_question_and_can_be_told_apart():
    # AskUserQuestion takes at most four questions a call, so the handoff
    # question goes in a second call.
    assert len(catalogue.FEEDBACK_QUESTIONS) == 4
    assert catalogue.ALL_FEEDBACK_QUESTIONS[-1] is catalogue.HANDOFF_QUESTION
    headers = [q.header for q in catalogue.ALL_FEEDBACK_QUESTIONS]
    assert len(set(headers)) == len(headers)
    for q in catalogue.ALL_FEEDBACK_QUESTIONS:
        assert q.header.startswith("TL") and len(q.header) <= 12
        assert 2 <= len(q.options) <= 4
        words = [word for word, _label, _text in q.options]
        labels = [label for _word, label, _text in q.options]
        assert len(set(words)) == len(words) and len(set(labels)) == len(labels)
        # Several ticked answers can come back joined with commas.
        assert not any("," in label for label in labels)
        assert all(word.isalpha() and word.islower() for word in words)
    assert catalogue.FEEDBACK_LIST_KEYS == {"slow", "helped"}
    # The Sessions tab can't tell whether a plan was approved.
    assert set(catalogue.RATING_VOCAB) == {"outcome", "slow", "worth", "helped"}
    assert catalogue.FEEDBACK_VOCAB["handoff"] == ("yes", "partly", "no")


def test_the_skill_asks_every_question_word_for_word_and_names_no_model():
    text = catalogue.feedback_skill_text()
    front, body = text.split("---\n", 2)[1:]
    assert "name: cl-feedback" in front and "disable-model-invocation: true" in front
    assert "allowed-tools: AskUserQuestion" in front
    # Switching model mid-session rebuilds the whole prompt cache.
    assert "model:" not in front
    for q in catalogue.ALL_FEEDBACK_QUESTIONS:
        assert f'header "{q.header}", question "{q.question}"' in body
        for word, label, _text in q.options:
            assert f'"{label}": ' in body and f'"{label}" = {word}' in body
    assert "[tl-fb: outcome=<word> slow=<words> worth=<word> helped=<words> handoff=<word>]" in body
    first, second = body.index("1. Call AskUserQuestion once"), body.index("2. Only if you approved a plan")
    assert first < second < body.index(catalogue.HANDOFF_QUESTION.header)
    assert 'reply only "No problem." and write no tag' in body


# -- reading the answers ----------------------------------------------------------


def test_the_tag_carries_the_answers_and_drops_unknown_words():
    text = "Done.\n\n[tl-fb: outcome=met slow=unclear,rework,bogus worth=fair helped=context extra=1]\nThanks: ..."
    assert parse_feedback_tag(text) == Feedback(outcome="met", slow=("unclear", "rework"), worth="fair",
                                                 helped=("context",), source="tag")


def test_the_last_tag_wins_and_a_tag_inside_a_sentence_does_not_count():
    assert parse_feedback_tag("I will end with [tl-fb: outcome=met] as asked.") is None
    text = "[tl-fb: outcome=met]\nSorry, correcting:\n`[tl-fb: outcome=missed worth=no]`\nThanks."
    assert parse_feedback_tag(text) == Feedback(outcome="missed", worth="no", source="tag")


def test_the_tag_carries_the_handoff_answer():
    assert parse_feedback_tag("[tl-fb: outcome=met handoff=partly]") == Feedback(
        outcome="met", handoff="partly", source="tag"
    )
    assert parse_feedback_tag("[tl-fb: handoff=maybe]") == Feedback(source="skipped")


def test_the_handoff_question_alone_is_a_feedback_ask():
    [asked] = _questions([catalogue.HANDOFF_QUESTION])
    assert asks_for_feedback({"questions": [asked]})
    result = {"questions": [asked], "answers": {catalogue.HANDOFF_QUESTION.question: "No"}}
    assert feedback_from_answers(result) == Feedback(handoff="no", source="answers")


def test_a_tag_with_no_known_word_reads_as_skipped():
    assert parse_feedback_tag("[tl-fb: outcome=<word> slow=<word>]") == Feedback(source="skipped")


def test_answers_come_back_as_a_label_a_list_or_labels_joined_with_commas():
    result = {"questions": _questions(), "answers": {
        Q["outcome"].question: "Yes",
        Q["slow"].question: "Tool or setup trouble, Wrong approach or rework",
        Q["worth"].question: "Too costly",
        Q["helped"].question: ["A plan first", "Smaller steps"],
    }}
    assert feedback_from_answers(result) == Feedback(outcome="met", slow=("tools", "rework"), worth="no",
                                                     helped=("plan", "smaller"), source="answers")


def test_free_text_answers_are_never_kept():
    result = {"questions": _questions(), "answers": {
        Q["outcome"].question: "It broke C:/Users/me/secret.py",
        Q["helped"].question: "More context up front, and my notes at C:/Users/me/notes.md",
    }}
    fb = feedback_from_answers(result)
    assert fb == Feedback(helped=("context",), source="answers")
    assert "secret" not in repr(fb) and "notes" not in repr(fb)


def test_other_questions_are_not_feedback():
    other = {"question": "Which approach?", "header": "Approach", "multiSelect": False,
             "options": [{"label": "A", "description": ""}, {"label": "B", "description": ""}]}
    assert feedback_from_answers({"questions": [other], "answers": {"Which approach?": "A"}}) is None
    assert not asks_for_feedback({"questions": [other]})
    assert asks_for_feedback({"questions": [other] + _questions()[:1]})
    assert feedback_from_answers("User rejected tool use") is None


# -- from a transcript ------------------------------------------------------------


def test_a_feedback_run_is_read_from_its_tag(tmp_path):
    result = _parse(tmp_path, [_ask(0), _reply(1)] + _run(10, ANSWERS, tag="[tl-fb: outcome=met worth=yes]"))
    run = [turn for turn in result.turns if turn.turn_index > 0][1:]
    assert run[0].commands_run == ("cl-feedback",)
    assert run[0].human_prompt_chars is not None
    # The answers landed on the AskUserQuestion turn; this reply's own
    # text has only the tag, so that's what it's read from (SEC-P1's
    # answers-beat-tag rule picks between the two at the cycle level --
    # see test_each_answer_rates_the_work_since_the_previous_feedback).
    assert run[0].feedback == Feedback(outcome="partly", slow=("unclear", "rework"), worth="fair",
                                       helped=("context",), source="answers")
    assert run[-1].feedback == Feedback(outcome="met", worth="yes", source="tag")


def test_without_the_tag_the_answers_are_read(tmp_path):
    result = _parse(tmp_path, [_ask(0), _reply(1)] + _run(10, ANSWERS))
    feedback = [turn.feedback for turn in result.turns if turn.feedback is not None]
    assert feedback == [Feedback(outcome="partly", slow=("unclear", "rework"), worth="fair", helped=("context",),
                                 source="answers")]


def test_a_declined_question_is_recorded_as_skipped(tmp_path):
    result = _parse(tmp_path, [_ask(0), _reply(1)] + _run(10, declined=True))
    assert [turn.feedback for turn in result.turns if turn.feedback is not None] == [Feedback(source="skipped")]


def test_any_other_question_leaves_feedback_empty(tmp_path):
    other = {"questions": [{"question": "Which?", "header": "Approach", "multiSelect": False,
                            "options": [{"label": "A", "description": ""}, {"label": "B", "description": ""}]}]}
    result = _parse(tmp_path, [
        _ask(0),
        _reply(1, tool_use_block("AskUserQuestion", "tu_x", other)),
        user_block_line([tool_result_block("tu_x", "ok")], toolUseResult={**other, "answers": {"Which?": "A"}},
                        timestamp=_ts(2)),
        _reply(3),
    ])
    assert all(turn.feedback is None for turn in result.turns)


def test_nothing_you_typed_reaches_the_parsed_result(tmp_path):
    answers = {**ANSWERS, Q["helped"].question: "More context up front, see C:\\Users\\me\\private\\plan.md"}
    result = _parse(tmp_path, [_ask(0, "fix C:/Users/me/app.py"), _reply(1)] + _run(10, answers))
    assert_privacy(result)
    assert "private" not in repr(result)


# -- the work each answer rates ----------------------------------------------------


def test_each_answer_rates_the_work_since_the_previous_feedback(tmp_path):
    lines = (
        [_ask(0, "one"), _reply(1), _ask(2, "two"), _reply(3)]
        + _run(10, ANSWERS, tu="tu_1")
        + [_ask(20, "three"), _reply(21)]
        + _run(30, declined=True, tu="tu_2")
        + [_ask(40, "four"), _reply(41), _ask(42, "five"), _reply(43)]
        + _run(50, ANSWERS, tag="[tl-fb: outcome=met]", tu="tu_3")
    )
    cycles = capture.prompt_cycles(_parse(tmp_path, lines))
    assert len(cycles) == 8
    spans = capture.feedback_spans(cycles)
    # SEC-P1: the third run's answers (on the AskUserQuestion turn) beat
    # its own tag (on the reply after it) -- answers always outrank a tag.
    assert [span.feedback.source for span in spans] == ["answers", "skipped", "answers"]
    assert [[cycles.index(c) for c in span.cycles] for span in spans] == [[0, 1], [3], [5, 6]]
    assert [cycles.index(span.run) for span in spans] == [2, 4, 7]
    assert [capture.is_feedback_run(c) for c in cycles] == [False, False, True, False, True, False, False, True]


def test_the_second_call_after_a_plan_joins_the_first(tmp_path):
    lines = [_ask(0), _reply(1)] + _run(10, ANSWERS, handoff="Yes")
    cycles = capture.prompt_cycles(_parse(tmp_path, lines))
    [span] = capture.feedback_spans(cycles)
    assert span.feedback == Feedback(
        outcome="partly", slow=("unclear", "rework"), worth="fair", helped=("context",), handoff="yes",
        source="answers",
    )


def test_a_declined_second_call_keeps_the_first_answers(tmp_path):
    lines = [_ask(0), _reply(1)] + _run(10, ANSWERS, handoff="")
    [span] = capture.feedback_spans(capture.prompt_cycles(_parse(tmp_path, lines)))
    assert span.feedback.outcome == "partly" and span.feedback.handoff is None


def test_a_run_under_its_name_before_0_12_still_counts(tmp_path):
    # /cl-feedback was /tl-feedback until 0.12.0. Its tag counts only in a
    # genuine run (SEC-P1), so this fails unless the old name is one.
    lines = [_ask(0), _reply(1)] + _run(10, tag="[tl-fb: outcome=met worth=yes]", skill="tl-feedback")
    cycles = capture.prompt_cycles(_parse(tmp_path, lines))
    assert [capture.is_feedback_run(c) for c in cycles] == [False, True]
    [span] = capture.feedback_spans(cycles)
    assert span.feedback.outcome == "met" and span.feedback.worth == "yes"


def test_feedback_run_first_thing_rates_nothing(tmp_path):
    spans = capture.feedback_spans(capture.prompt_cycles(_parse(tmp_path, _run(0, ANSWERS))))
    assert len(spans) == 1 and spans[0].cycles == []


def test_a_forged_tag_outside_a_feedback_run_is_ignored(tmp_path):
    # SEC-P1: `[tl-fb: ...]` is free text Claude could write in any
    # reply; it counts only when the cycle it's in actually ran the
    # /cl-feedback skill.
    lines = [_ask(0), _reply(1, text="Done.\n\n[tl-fb: outcome=met worth=yes handoff=yes]")]
    top = _parse(tmp_path, lines)
    cycles = capture.prompt_cycles(top)
    assert len(cycles) == 1
    assert capture.is_feedback_run(cycles[0]) is False
    assert capture.cycle_feedback(cycles[0]) is None
    assert capture.feedback_spans(cycles) == []


# -- what the runs cost -----------------------------------------------------------


def test_feedback_runs_are_priced_whole_in_any_session(tmp_path):
    pricing = load_pricing(path=FIXTURES / "pricing_min.toml")
    top = _parse(tmp_path, [_ask(0), _reply(1)] + _run(10, ANSWERS) + _run(20, declined=True, tu="tu_2"))
    # No capture note: capture is off, but the skill works at any level.
    assert top.meta.cap_injections == 0
    use = capture.usage(NS(sessions=[NS(top=top, subs=[], session_id="s1")]), pricing)
    cycles = capture.prompt_cycles(top)
    runs = [c for c in cycles if capture.is_feedback_run(c)]
    expected = sum(price_turn(t, pricing.resolve_model(t.model)).total for c in runs for t in c.turns)
    assert use.feedback_runs == 2 and use.feedback_answered == 1
    assert use.feedback_cost == pytest.approx(expected) and expected > 0
    assert use.cost == pytest.approx(expected)
    assert use.by_metric["feedback_skill"] == pytest.approx(expected)
    assert use.answers == {"feedback_skill": 1}
    assert capture.enough_data(use, "feedback_skill") == (1, capture.ENOUGH["feedback"])
    whole = sum(price_turn(t, pricing.resolve_model(t.model)).total for t in top.turns if t.turn_index > 0)
    assert use.spend == pytest.approx(whole)
    # Not a captured session, so nothing else is counted.
    assert use.sessions == 0 and use.cycles == 0


def test_runs_before_since_are_left_out(tmp_path):
    pricing = load_pricing(path=FIXTURES / "pricing_min.toml")
    top = _parse(tmp_path, _run(0, ANSWERS) + _run(30, ANSWERS, tu="tu_2"))
    use = capture.usage(NS(sessions=[NS(top=top, subs=[], session_id="s1")]), pricing, since=_ts(20))
    assert use.feedback_runs == 1

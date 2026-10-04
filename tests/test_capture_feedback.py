"""Your /cg-feedback answers: the skill and its questions
(``capture_catalogue``), reading them back from a transcript (the
``[cg-fb: ...]`` line, or the AskUserQuestion result when the line is
missing), the work each answer rates (``capture.feedback_spans``), and
what the runs cost (``capture.usage``).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from claudeglass import capture, capture_catalogue as catalogue, footprint, parse
from claudeglass.capture_tags import (
    asks_for_feedback,
    feedback_from_answers,
    merge_feedback,
    parse_feedback_tag,
    settle_feedback,
    with_older_why,
)
from claudeglass.model import Feedback, TranscriptMeta
from claudeglass.parse import parse_transcript
from claudeglass.pricing import load_pricing, price_turn

from helpers import (
    assert_privacy,
    attachment_line,
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
OLD = {q.key: q for q in catalogue.LEGACY_FEEDBACK_QUESTIONS}

#: What the facts line would fill into the questions that carry numbers.
FILL = {"n": 3, "q": 1, "tokens": "1.3M", "x": "2.0", "hint title": catalogue.TIP_HINT_TITLES["drip_feed"]}


@pytest.fixture(autouse=True)
def _salt():
    parse.set_salt(b"f" * 32)


def _text(q) -> str:
    """The question as Claude asks it, with its numbers filled in."""
    return q.question.format(**FILL)


#: The question text each answer is keyed by.
T = {q.key: _text(q) for q in catalogue.FEEDBACK_QUESTIONS}
OLD_T = {q.key: q.question for q in catalogue.LEGACY_FEEDBACK_QUESTIONS}


def _label(q, word: str) -> str:
    return next(label for w, label, _text in q.options if w == word)


def _ts(second: int) -> str:
    return f"2026-09-18T12:{second // 60:02d}:{second % 60:02d}.000Z"


def _ask(second: int, text: str = "do it") -> dict:
    return user_str_line(text, origin={"kind": "human"}, timestamp=_ts(second))


def _reply(second: int, *blocks, text: str = "ok") -> dict:
    return turn_line(content=list(blocks) or [{"type": "text", "text": text}], model=MODEL, timestamp=_ts(second))


def _questions(questions) -> list[dict]:
    return [
        {
            "question": _text(q),
            "header": q.header,
            "multiSelect": q.multi,
            "options": [{"label": label, "description": text} for _word, label, text in q.options],
        }
        for q in questions
    ]


CALL_1 = catalogue.feedback_questions()[0]
OLD_CALL = catalogue.LEGACY_FEEDBACK_QUESTIONS[:4]


def _run(
    second: int,
    answers=None,
    *,
    tag: str | None = None,
    declined: bool = False,
    tu: str = "tu_q",
    later: tuple | None = None,
    skill: str = "cg-feedback",
    asked=CALL_1,
    no_questions: bool = False,
) -> list:
    """A /cg-feedback run as Claude Code writes it: the skill you ran
    (``<command-message>`` first, then its body), Claude's question, your
    answers, and the reply ending with the tag. ``later`` is a second call,
    ``(questions, answers)``, whose answers are ``None`` when you declined
    it. ``no_questions`` is a run where Claude asked nothing."""
    lines = [
        user_str_line(f"<command-message>{skill}</command-message>\n<command-name>/{skill}</command-name>",
                      timestamp=_ts(second)),
        user_block_line([{"type": "text", "text": catalogue.feedback_skill_text()}], isMeta=True,
                        timestamp=_ts(second)),
    ]
    summary = "Recorded for ClaudeGlass: Outcome Partly. Run /cg-feedback again now to change it."
    final = f"{summary}\n{tag}" if tag else summary
    if no_questions:
        return lines + [_reply(second + 3, text=final)]
    lines.append(_reply(second + 1, tool_use_block("AskUserQuestion", tu, {"questions": _questions(asked)})))
    if declined:
        lines.append(user_block_line([tool_result_block(tu, "User rejected tool use", is_error=True)],
                                     toolUseResult="User rejected tool use", timestamp=_ts(second + 2)))
        lines.append(_reply(second + 3, text=f"No problem.\n{tag}" if tag else "No problem."))
        return lines
    result = {"questions": _questions(asked), "answers": answers or {}}
    lines.append(user_block_line([tool_result_block(tu, "User has answered your questions.")],
                                 toolUseResult=result, timestamp=_ts(second + 2)))
    if later is not None:
        questions, answered = later
        call = _questions(questions)
        lines.append(_reply(second + 2, tool_use_block("AskUserQuestion", f"{tu}_2", {"questions": call})))
        if answered is None:
            lines.append(user_block_line([tool_result_block(f"{tu}_2", "User rejected tool use", is_error=True)],
                                         toolUseResult="User rejected tool use", timestamp=_ts(second + 2)))
        else:
            lines.append(user_block_line([tool_result_block(f"{tu}_2", "User has answered your questions.")],
                                         toolUseResult={"questions": call, "answers": answered},
                                         timestamp=_ts(second + 2)))
    lines.append(_reply(second + 3, text=final))
    return lines


def _parse(tmp_path, lines, name: str = "top.jsonl"):
    path = tmp_path / name
    write_jsonl(path, lines)
    return parse_transcript(path, TranscriptMeta(path=str(path), kind="top-level"))


def _settled(tmp_path, lines) -> Feedback | None:
    """The feedback of the last message in ``lines``."""
    return capture.cycle_feedback(capture.prompt_cycles(_parse(tmp_path, lines))[-1])


ANSWERS = {
    T["outcome"]: "Partly",
    T["why"]: ["Things I hadn't said", "A change of mind"],
    T["worth"]: "About right",
    T["helped"]: "More in my first message",
}
OLD_ANSWERS = {
    OLD_T["outcome"]: "Partly",
    OLD_T["slow"]: ["My request was unclear", "Wrong approach or rework"],
    OLD_T["worth"]: "About right",
    OLD_T["helped"]: "More context up front",
}
#: What ``ANSWERS`` reads as.
ANSWERED = dict(outcome="partly", why=("left_out", "changed"), worth="fair", helped=("context",))


def _keys(asked) -> list[list[str]]:
    return [[q.key for q in call] for call in asked]


FACTS = {
    "tokens": 1_300_000,
    "typical": 420_000,
    "followups": 5,
    "queued": 1,
    "plan": "approved",
    "plan_followups": 2,
    "plan_asked": 0,
    "build": "same",
    "tip": "drip_feed",
}


# -- the questions and the skill ------------------------------------------------


def test_the_questions_are_the_plans_in_its_order_with_the_tables_words():
    table = [
        ("outcome", "CG outcome", False,
         [("met", "Yes"), ("partly", "Partly"), ("missed", "No"), ("stopped", "Stopped early")]),
        ("why", "CG followups", True,
         [("left_out", "Things I hadn't said"),
          ("missed", "Claude missed something (it was in my request or the plan)"),
          ("changed", "A change of mind"), ("none", "Questions or go-aheads")]),
        ("missed_in", "CG missed", False,
         [("message", "My message"), ("plan", "The plan"), ("standing", "CLAUDE.md or memory"),
          ("earlier", "Earlier in this chat")]),
        ("worth", "CG worth", False, [("yes", "Worth it"), ("fair", "About right"), ("no", "Too costly")]),
        ("helped", "CG next time", True,
         [("context", "More in my first message"), ("plan", "A plan first"), ("smaller", "Smaller pieces"),
          ("none", "Nothing")]),
        ("plan", "CG plan", False,
         [("covered", "It was in the plan"), ("gap", "The plan missed it"), ("new", "It was new")]),
        ("handoff", "CG handoff", False, [("yes", "Yes"), ("partly", "Partly"), ("no", "No")]),
        ("tip", "CG tip", False, [("useful", "Useful"), ("known", "Right but I knew"), ("wrong", "Wrong here")]),
    ]
    assert [
        (q.key, q.header, q.multi, [(word, label) for word, label, _text in q.options])
        for q in catalogue.FEEDBACK_QUESTIONS
    ] == table
    assert Q["why"].question == (
        "You sent {n} more messages after your first ({q} while Claude was working). What were they mostly?"
    )
    assert Q["worth"].question == (
        "This work used about {tokens} tokens, about {x}× your usual piece. Was the result worth it?"
    )
    assert Q["tip"].question == "ClaudeGlass showed a tip about {hint title}. Was it right for this work?"


def test_questions_fit_ask_user_question_in_two_calls_and_can_be_told_apart():
    headers = [q.header for q in catalogue.ALL_FEEDBACK_QUESTIONS]
    assert len(set(headers)) == len(headers)
    for q in catalogue.FEEDBACK_QUESTIONS:
        assert q.header.startswith("CG") and len(q.header) <= 12, q.header
    for q in catalogue.LEGACY_FEEDBACK_QUESTIONS:
        assert q.header.startswith("TL") and len(q.header) <= 12
    for q in catalogue.ALL_FEEDBACK_QUESTIONS:
        assert 2 <= len(q.options) <= 4
        words = [word for word, _label, _text in q.options]
        labels = [label for _word, label, _text in q.options]
        assert len(set(words)) == len(words) and len(set(labels)) == len(labels)
        # Several ticked answers can come back joined with commas.
        assert not any("," in label for label in labels)
        assert all(word.replace("_", "").isalpha() and word.islower() for word in words)
    # Two calls of at most four questions each, even with every question
    # in play: the missed question needs the answer to the follow-ups one,
    # so it opens the second.
    first, second = catalogue.feedback_questions(FACTS, why=("missed",))
    assert _keys((first, second)) == [["outcome", "why", "worth", "helped"], ["missed_in", "plan", "handoff", "tip"]]
    assert {q.call for q in first} == {1} and {q.call for q in second} == {2}
    assert len(catalogue.FEEDBACK_QUESTIONS) == len(first) + len(second)
    assert catalogue.FEEDBACK_LIST_KEYS == {"why", "helped", "slow"}
    # CG clear and CG notice are gone.
    assert not [h for h in headers if h in ("CG clear", "CG notice", "TL clear", "TL notice")]
    # The Sessions tab takes the words of every question it can ask, the
    # retired slow-down question's (an old answer stays readable) and the
    # tip the tip answer is about. The service leaves out the questions
    # a session's facts say don't apply.
    assert set(catalogue.RATING_VOCAB) == {q.key for q in catalogue.RATING_QUESTIONS} | {"slow", "tip_hint"}
    assert catalogue.RATING_QUESTIONS is catalogue.FEEDBACK_QUESTIONS
    assert catalogue.FEEDBACK_VOCAB["why"] == ("left_out", "missed", "changed", "none")
    assert catalogue.FEEDBACK_VOCAB["tip_hint"] == tuple(catalogue.TIP_HINT_TITLES)


def test_without_a_facts_line_the_followups_question_is_asked_and_the_skill_drops_the_numbers():
    assert _keys(catalogue.feedback_questions()) == [["outcome", "why", "worth", "helped"], []]
    # An approved plan Claude saw in this piece of work is enough for the handoff.
    assert _keys(catalogue.feedback_questions(plan_approved=True))[1] == ["handoff"]
    # The tip and plan questions need the line.
    assert _keys(catalogue.feedback_questions(plan_approved=True, why=("missed",)))[1] == ["missed_in", "handoff"]


@pytest.mark.parametrize(
    ("facts", "why", "asked"),
    [
        ({"followups": 0}, (), [["outcome", "worth", "helped"], []]),
        ({"followups": 1}, (), [["outcome", "why", "worth", "helped"], []]),
        ({"followups": 1}, ("left_out", "changed"), [["outcome", "why", "worth", "helped"], []]),
        ({"followups": 1}, ("missed",), [["outcome", "why", "worth", "helped"], ["missed_in"]]),
        ({"followups": 0}, ("missed",), [["outcome", "worth", "helped"], ["missed_in"]]),
        ({"followups": 0, "plan": "approved", "plan_followups": 1, "plan_asked": 0}, (),
         [["outcome", "worth", "helped"], ["plan"]]),
        ({"followups": 0, "plan": "approved", "plan_followups": 0, "plan_asked": 0}, (),
         [["outcome", "worth", "helped"], []]),
        ({"followups": 0, "plan": "approved", "plan_followups": 3, "plan_asked": 1}, (),
         [["outcome", "worth", "helped"], []]),
        ({"followups": 0, "plan": "none", "plan_followups": 3, "plan_asked": 0}, (),
         [["outcome", "worth", "helped"], []]),
        ({"followups": 0, "plan": "approved", "build": "same"}, (), [["outcome", "worth", "helped"], ["handoff"]]),
        ({"followups": 0, "plan": "approved", "build": "fresh"}, (), [["outcome", "worth", "helped"], []]),
        ({"followups": 0, "plan": "none", "build": "same"}, (), [["outcome", "worth", "helped"], []]),
        ({"followups": 0, "tip": "drip_feed"}, (), [["outcome", "worth", "helped"], ["tip"]]),
        ({"followups": 0, "tip": "none"}, (), [["outcome", "worth", "helped"], []]),
        ({"followups": 0, "tip": "no_such_hint"}, (), [["outcome", "worth", "helped"], []]),
    ],
)
def test_each_question_is_asked_only_when_its_condition_holds(facts, why, asked):
    assert _keys(catalogue.feedback_questions(facts, why)) == asked


def test_every_tip_has_a_title_the_question_can_name():
    for hint, title in catalogue.TIP_HINT_TITLES.items():
        assert _keys(catalogue.feedback_questions({"followups": 0, "tip": hint}))[1] == ["tip"]
        assert title and "{" not in title
        assert "ClaudeGlass showed a tip about " + title in Q["tip"].question.format(**{"hint title": title})


def test_the_skill_asks_every_question_word_for_word_and_names_no_model():
    text = catalogue.feedback_skill_text()
    front, body = text.split("---\n", 2)[1:]
    assert "name: cg-feedback" in front and "disable-model-invocation: true" in front
    assert "allowed-tools: AskUserQuestion" in front
    # Switching model mid-session rebuilds the whole prompt cache.
    assert "model:" not in front
    for q in catalogue.FEEDBACK_QUESTIONS:
        assert f'header "{q.header}", question "{q.question}"' in body
        for word, label, description in q.options:
            assert f'"{label}" = {word}: {description}' in body
        if q.when:
            assert f"Ask when {q.when}." in body
        if q.plain:
            assert f'Plain wording: "{q.plain}"' in body
        assert q.short in body
    # Call 1 holds the first four questions, call 2 the rest.
    one, two = body.index("1. Call AskUserQuestion once"), body.index("2. When the answers are back")
    assert one < two < body.index("3. An answer that is not one of")
    for q in catalogue.FEEDBACK_QUESTIONS:
        at = body.index(f'header "{q.header}"')
        assert (one < at < two) == (q.call == 1) and (two < at) == (q.call == 2)
    assert 'reply only "No problem." and write no tag' in body
    # The old questions are gone from it.
    assert "TL " not in text and "What slowed it down" not in text and "CG clear" not in text


def test_the_skill_reads_the_facts_line_and_works_without_it():
    body = catalogue.feedback_skill_text()
    assert f"`{catalogue.FEEDBACK_FACTS_MARKER} key=value ...`" in body
    assert catalogue.FEEDBACK_FACTS_MARKER == "cg-fb-facts v1"
    for key in catalogue.FEEDBACK_FACT_KEYS:
        assert key in body
    assert "Fill {n} from followups and {q} from queued" in body
    assert "{tokens}" in body and "{x}" in body and "{hint title}" in body
    for hint, title in catalogue.TIP_HINT_TITLES.items():
        assert f"{hint} = {title}" in body
    assert "Without it, or without a number a question needs, ask the plain wording" in body
    assert "Never guess a number." in body
    assert "leaving out any that don't apply" in body


def test_the_skill_maps_other_to_a_word_and_never_keeps_the_words():
    body = catalogue.feedback_skill_text()
    assert "Map it to the closest word or words of that question's list and to nothing else" in body
    assert "leave the key out when nothing fits" in body
    assert "Never copy, quote or save their words" in body
    assert "A ticked answer always stands as ticked." in body
    assert "from_text=<keys>" in body and "tip_hint=<hint id>" in body


def test_the_skill_ends_with_the_fixed_template_and_one_next_time_line():
    body = catalogue.feedback_skill_text()
    assert "Recorded for ClaudeGlass: Outcome Partly · Follow-ups Claude missed something (from your note) · " \
           "Worth Too costly · Next time A plan first. Not recorded: Plan (your note didn't match an option). " \
           "Run /cg-feedback again now to change it." in body
    assert "Next time: <the line from step 5>" in body
    # The tag is the last line of the reply: the capture reads the end of it.
    tag = (
        "[cg-fb: outcome=<word> why=<words> missed_in=<word> worth=<word> helped=<words> plan=<word> "
        "handoff=<word> tip=<word> tip_hint=<hint id> from_text=<keys>]"
    )
    assert tag in body
    assert body.index("Recorded for ClaudeGlass") < body.index("Next time: <") < body.index(tag)
    for when, line in catalogue.FEEDBACK_NEXT_TIME:
        assert f'{when}: "{line}"' in body
    # In the plan's priority order.
    order = [
        "why includes missed and missed_in=message", "why includes missed and missed_in=plan",
        "why includes missed and missed_in=standing", "why includes missed and missed_in=earlier",
        "why includes missed and there is no missed_in", "plan=gap", "why includes left_out",
        "worth=no and helped includes smaller",
    ]
    assert [when for when, _line in catalogue.FEEDBACK_NEXT_TIME] == order
    assert "/cg-brief" in dict(catalogue.FEEDBACK_NEXT_TIME)["why includes left_out"]
    assert "one piece of work per session" in dict(catalogue.FEEDBACK_NEXT_TIME)["worth=no and helped includes smaller"]


def test_the_catalogue_describes_the_tag_with_every_word():
    tag = catalogue._feedback_tag_words()
    assert tag.startswith("[cg-fb: outcome=met|partly|missed|stopped why=left_out,missed,changed,none ")
    assert "tip=useful|known|wrong tip_hint=<hint id> from_text=<keys>]" in tag
    assert "slow=" not in tag
    assert "slow" in catalogue.FEEDBACK_LIST_KEYS


def test_an_installed_skill_from_before_the_redesign_is_outdated_and_is_rewritten(tmp_path):
    root = tmp_path / "claude"
    path = footprint.skill_path("cg-feedback", root)
    path.parent.mkdir(parents=True)
    old = catalogue.feedback_skill_text().replace("a few quick checkbox questions", "four quick checkbox questions")
    path.write_text(old, encoding="utf-8")
    assert footprint.skill_state("cg-feedback", root) == "outdated"
    footprint.write_skill("cg-feedback", root)
    assert footprint.skill_state("cg-feedback", root) == "installed"
    assert path.read_text(encoding="utf-8") == catalogue.feedback_skill_text()


# -- reading the tag --------------------------------------------------------------


def test_the_tag_carries_the_answers_and_drops_unknown_words():
    text = "Done.\n\n[cg-fb: outcome=met why=left_out,missed,bogus worth=fair helped=context extra=1]"
    assert parse_feedback_tag(text) == Feedback(outcome="met", why=("left_out", "missed"), worth="fair",
                                                 helped=("context",), source="tag")


def test_the_tag_carries_every_new_key():
    tag = ("[cg-fb: outcome=partly why=missed missed_in=plan worth=no helped=smaller,plan plan=gap handoff=partly "
           "tip=wrong tip_hint=drip_feed from_text=why,plan]")
    assert parse_feedback_tag(tag) == Feedback(
        outcome="partly", why=("missed",), missed_in="plan", worth="no", helped=("smaller", "plan"), plan="gap",
        handoff="partly", tip="wrong", tip_hint="drip_feed", from_text=("why", "plan"), source="tag",
    )
    assert parse_feedback_tag("[cg-fb: tip=known tip_hint=made_up]") == Feedback(tip="known", source="tag")
    assert parse_feedback_tag("[cg-fb: missed_in=nowhere plan=maybe]") == Feedback(source="skipped")


def test_from_text_lists_only_keys_that_have_a_word_and_are_questions():
    tag = "[cg-fb: outcome=met why=bogus from_text=outcome,why,worth,tip_hint,nothing]"
    assert parse_feedback_tag(tag) == Feedback(outcome="met", from_text=("outcome",), source="tag")
    assert parse_feedback_tag("[cg-fb: from_text=outcome]") == Feedback(source="skipped")


def test_the_longest_tag_the_skill_can_write_is_read_whole():
    parts = []
    for q in catalogue.FEEDBACK_QUESTIONS:
        words = [word for word, _label, _text in q.options]
        parts.append(f"{q.key}=" + (",".join(words) if q.multi else max(words, key=len)))
        if q.key == "tip":
            parts.append("tip_hint=plan_fresh_early")
    parts.append("from_text=" + ",".join(q.key for q in catalogue.FEEDBACK_QUESTIONS))
    tag = "[cg-fb: " + " ".join(parts) + "]"
    # More than the body limit this was before the redesign.
    assert 200 < len(tag) < 300
    fb = parse_feedback_tag(f"Recorded.\n{tag}")
    assert fb is not None and fb.source == "tag"
    assert fb.why == ("left_out", "missed", "changed", "none") and fb.helped == ("context", "plan", "smaller", "none")
    assert fb.tip == "useful" and fb.tip_hint == "plan_fresh_early" and fb.missed_in == "standing"
    assert fb.from_text == tuple(q.key for q in catalogue.FEEDBACK_QUESTIONS)


def test_a_tag_body_of_300_characters_is_read_and_one_more_is_not():
    head = " outcome=met "
    body = head + "x" * (300 - len(head))
    assert len(body) == 300
    assert parse_feedback_tag(f"[cg-fb:{body}]") == Feedback(outcome="met", source="tag")
    assert parse_feedback_tag(f"[cg-fb:{body}x]") is None


def test_the_tag_is_read_when_the_capture_tag_follows_it():
    # The capture note asks for a `[cg: ...]` line at the end of every
    # reply; with capture on it can come after the feedback tag.
    text = "Recorded.\n[cg-fb: outcome=met worth=yes]\n[cg: task=chat brief=clear level=easy size=xs]"
    assert parse_feedback_tag(text) == Feedback(outcome="met", worth="yes", source="tag")
    assert "only a [cg: ...] line the capture note asks for may follow it" in catalogue.feedback_skill_text()


def test_the_last_tag_wins_and_a_tag_inside_a_sentence_does_not_count():
    assert parse_feedback_tag("I will end with [cg-fb: outcome=met] as asked.") is None
    text = "[cg-fb: outcome=met]\nSorry, correcting:\n`[cg-fb: outcome=missed worth=no]`"
    assert parse_feedback_tag(text) == Feedback(outcome="missed", worth="no", source="tag")


def test_the_tag_carries_the_handoff_answer():
    assert parse_feedback_tag("[cg-fb: outcome=met handoff=partly]") == Feedback(
        outcome="met", handoff="partly", source="tag"
    )
    assert parse_feedback_tag("[cg-fb: handoff=maybe]") == Feedback(source="skipped")


def test_a_tag_with_no_known_word_reads_as_skipped():
    assert parse_feedback_tag("[cg-fb: outcome=<word> why=<words> from_text=<keys>]") == Feedback(source="skipped")


def test_an_older_tag_with_slow_and_the_older_name_still_reads():
    text = "Thanks.\n[tl-fb: outcome=met slow=unclear,rework worth=fair helped=context]"
    assert parse_feedback_tag(text) == Feedback(outcome="met", slow=("unclear", "rework"), worth="fair",
                                                 helped=("context",), source="tag")


# -- reading the answers ----------------------------------------------------------


def test_answers_come_back_as_a_label_a_list_or_labels_joined_with_commas():
    result = {"questions": _questions(CALL_1), "answers": {
        T["outcome"]: "Yes",
        T["why"]: "Things I hadn't said, A change of mind",
        T["worth"]: "Too costly",
        T["helped"]: ["A plan first", "Smaller pieces"],
    }}
    assert feedback_from_answers(result) == Feedback(outcome="met", why=("left_out", "changed"), worth="no",
                                                     helped=("plan", "smaller"), source="answers")


def test_the_second_call_questions_are_read_too():
    asked = [Q["missed_in"], Q["plan"], Q["handoff"], Q["tip"]]
    result = {"questions": _questions(asked), "answers": {
        T["missed_in"]: "CLAUDE.md or memory", T["plan"]: "The plan missed it", T["handoff"]: "No",
        T["tip"]: "Wrong here",
    }}
    assert feedback_from_answers(result) == Feedback(
        missed_in="standing", plan="gap", handoff="no", tip="wrong", tip_hint="drip_feed", source="answers"
    )
    assert asks_for_feedback({"questions": _questions(asked[:1])})


def test_the_tip_answer_carries_the_tip_its_question_named():
    for hint, title in catalogue.TIP_HINT_TITLES.items():
        text = Q["tip"].question.format(**{"hint title": title})
        question = {"header": "CG tip", "question": text}
        assert feedback_from_answers({"questions": [question], "answers": {text: "Right but I knew"}}) == Feedback(
            tip="known", tip_hint=hint, source="answers"
        )
    # A question that names none of them leaves the hint to the tag.
    question = {"header": "CG tip", "question": "Was that tip right?"}
    answered = feedback_from_answers({"questions": [question], "answers": {"Was that tip right?": "Useful"}})
    assert answered == Feedback(tip="useful", source="answers")


def test_free_text_answers_are_never_kept_only_which_question_they_answered():
    result = {"questions": _questions(CALL_1), "answers": {
        T["outcome"]: "It broke C:/Users/me/secret.py",
        T["helped"]: "More in my first message, and my notes at C:/Users/me/notes.md",
    }}
    fb = feedback_from_answers(result)
    assert fb == Feedback(helped=("context",), other=("outcome", "helped"), source="answers")
    assert "secret" not in repr(fb) and "notes" not in repr(fb)


def test_a_single_choice_answer_is_a_label_or_all_your_own_words():
    result = {"questions": _questions(CALL_1), "answers": {T["outcome"]: "Yes, but it also broke the build"}}
    assert feedback_from_answers(result) == Feedback(source="skipped", other=("outcome",))


def test_other_questions_are_not_feedback():
    other = {"question": "Which approach?", "header": "Approach", "multiSelect": False,
             "options": [{"label": "A", "description": ""}, {"label": "B", "description": ""}]}
    assert feedback_from_answers({"questions": [other], "answers": {"Which approach?": "A"}}) is None
    assert not asks_for_feedback({"questions": [other]})
    assert asks_for_feedback({"questions": [other] + _questions(CALL_1)[:1]})
    assert feedback_from_answers("User rejected tool use") is None


# -- the older questions still parse -----------------------------------------------


def test_the_older_questions_and_their_tl_headers_still_parse():
    asked = _questions(catalogue.LEGACY_FEEDBACK_QUESTIONS)
    assert [q["header"] for q in asked] == ["TL outcome", "TL slowdown", "TL worth", "TL helped", "TL handoff"]
    assert asks_for_feedback({"questions": asked[1:2]})
    result = {"questions": asked, "answers": {
        **OLD_ANSWERS,
        OLD_T["slow"]: "Tool or setup trouble, Wrong approach or rework",
        OLD_T["handoff"]: "No",
        OLD_T["helped"]: ["A plan first", "Smaller steps"],
    }}
    assert feedback_from_answers(result) == Feedback(
        outcome="partly", slow=("tools", "rework"), worth="fair", helped=("plan", "smaller"), handoff="no",
        source="answers",
    )


def test_the_older_handoff_question_alone_is_a_feedback_ask():
    [asked] = _questions([OLD["handoff"]])
    assert asks_for_feedback({"questions": [asked]})
    result = {"questions": [asked], "answers": {OLD_T["handoff"]: "No"}}
    assert feedback_from_answers(result) == Feedback(handoff="no", source="answers")


@pytest.mark.parametrize(
    ("slow", "why", "older"),
    [
        (("unclear",), ("left_out",), True),
        (("none",), ("none",), True),
        (("rework",), (), False),
        (("tools",), (), False),
        (("unclear", "rework"), ("left_out",), True),
        (("tools", "none", "unclear"), ("none", "left_out"), True),
        ((), (), False),
    ],
)
def test_an_older_slow_answer_gives_why_and_is_marked_older(slow, why, older):
    fb = with_older_why(Feedback(outcome="met", slow=slow, source="answers"))
    assert (fb.why, fb.why_older, fb.slow) == (why, older, slow)


def test_a_new_why_is_not_overwritten_by_an_older_slow():
    fb = Feedback(slow=("unclear",), why=("changed",))
    assert with_older_why(fb) == fb and not fb.why_older


def test_an_older_run_read_from_a_transcript_gets_why_from_its_slow(tmp_path):
    lines = [_ask(0), _reply(1)] + _run(10, OLD_ANSWERS, asked=OLD_CALL)
    fb = _settled(tmp_path, lines)
    assert fb == Feedback(outcome="partly", slow=("unclear", "rework"), worth="fair", helped=("context",),
                          why=("left_out",), why_older=True, source="answers")
    # The turn itself still holds what was read, slow only.
    turns = [t.feedback for t in _parse(tmp_path, lines, "again.jsonl").turns if t.feedback is not None]
    assert turns == [Feedback(outcome="partly", slow=("unclear", "rework"), worth="fair", helped=("context",),
                              source="answers")]


def test_an_older_tag_gets_why_from_its_slow(tmp_path):
    lines = [_ask(0), _reply(1)] + _run(10, tag="[tl-fb: outcome=met slow=unclear worth=yes]", no_questions=True)
    assert _settled(tmp_path, lines) == Feedback(outcome="met", slow=("unclear",), worth="yes", why=("left_out",),
                                                 why_older=True, source="tag")


# -- from a transcript ------------------------------------------------------------


def test_a_feedback_run_is_read_from_its_tag(tmp_path):
    result = _parse(tmp_path, [_ask(0), _reply(1)] + _run(10, ANSWERS, tag="[cg-fb: outcome=met worth=yes]"))
    run = [turn for turn in result.turns if turn.turn_index > 0][1:]
    assert run[0].commands_run == ("cg-feedback",)
    assert run[0].human_prompt_chars is not None
    # The answers landed on the AskUserQuestion turn; this reply's own
    # text has only the tag, so that's what it's read from (SEC-P1's
    # answers-beat-tag rule picks between the two at the cycle level --
    # see test_each_answer_rates_the_work_since_the_previous_feedback).
    assert run[0].feedback == Feedback(**ANSWERED, source="answers")
    assert run[-1].feedback == Feedback(outcome="met", worth="yes", source="tag")


def test_without_the_tag_the_answers_are_read(tmp_path):
    result = _parse(tmp_path, [_ask(0), _reply(1)] + _run(10, ANSWERS))
    feedback = [turn.feedback for turn in result.turns if turn.feedback is not None]
    assert feedback == [Feedback(**ANSWERED, source="answers")]


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
    answers = {**ANSWERS, T["helped"]: "More context up front, see C:\\Users\\me\\private\\plan.md"}
    result = _parse(tmp_path, [_ask(0, "fix C:/Users/me/app.py"), _reply(1)] + _run(10, answers))
    assert_privacy(result)
    assert "private" not in repr(result)


def test_the_second_call_joins_the_first(tmp_path):
    later = ([Q["missed_in"], Q["plan"], Q["handoff"], Q["tip"]], {
        T["missed_in"]: "The plan", T["plan"]: "It was new", T["handoff"]: "Yes", T["tip"]: "Useful",
    })
    lines = [_ask(0), _reply(1)] + _run(10, {**ANSWERS, T["why"]: "Claude missed something (it was in my request or "
                                                                  "the plan)"}, later=later)
    fb = _settled(tmp_path, lines)
    assert fb == Feedback(
        outcome="partly", why=("missed",), missed_in="plan", worth="fair", helped=("context",), plan="new",
        handoff="yes", tip="useful", tip_hint="drip_feed", source="answers",
    )


def test_a_declined_second_call_keeps_the_first_answers(tmp_path):
    lines = [_ask(0), _reply(1)] + _run(10, ANSWERS, later=([Q["handoff"]], None))
    fb = _settled(tmp_path, lines)
    assert fb is not None and fb.outcome == "partly" and fb.handoff is None and fb.source == "answers"


# -- a word picked from your own note ------------------------------------------------


def test_a_word_picked_from_your_note_counts_when_the_answer_was_not_a_label(tmp_path):
    answers = {**ANSWERS, T["why"]: "Claude ignored the file I named in my first message"}
    tag = "[cg-fb: outcome=partly why=missed worth=fair helped=context from_text=why]"
    fb = _settled(tmp_path, [_ask(0), _reply(1)] + _run(10, answers, tag=tag))
    assert fb == Feedback(outcome="partly", why=("missed",), worth="fair", helped=("context",),
                          from_text=("why",), other=("why",), source="answers")
    assert "ignored" not in repr(fb)


def test_a_word_for_a_key_you_ticked_is_dropped(tmp_path):
    # `why` was ticked, so the tag's word for it can't have come from a note.
    tag = "[cg-fb: outcome=partly why=missed worth=fair helped=context from_text=why]"
    fb = _settled(tmp_path, [_ask(0), _reply(1)] + _run(10, ANSWERS, tag=tag))
    assert fb == Feedback(**ANSWERED, source="answers")
    assert fb.from_text == ()


def test_a_tag_cannot_override_a_ticked_answer(tmp_path):
    tag = "[cg-fb: outcome=met worth=no helped=smaller plan=gap from_text=outcome,worth,plan]"
    fb = _settled(tmp_path, [_ask(0), _reply(1)] + _run(10, ANSWERS, tag=tag))
    assert fb == Feedback(**ANSWERED, source="answers")


def test_a_tag_cannot_override_a_tick_even_when_another_answer_was_your_own_words(tmp_path):
    answers = {**ANSWERS, T["why"]: "my own words"}
    tag = "[cg-fb: outcome=met why=changed worth=no from_text=outcome,why,worth]"
    fb = _settled(tmp_path, [_ask(0), _reply(1)] + _run(10, answers, tag=tag))
    assert (fb.outcome, fb.worth, fb.why, fb.from_text) == ("partly", "fair", ("changed",), ("why",))


def test_a_word_for_a_question_that_was_not_answered_is_dropped(tmp_path):
    # You answered the questions that were asked; the tag adds a plan word
    # for a question that was never put to you.
    tag = "[cg-fb: outcome=partly plan=gap from_text=plan]"
    fb = _settled(tmp_path, [_ask(0), _reply(1)] + _run(10, ANSWERS, tag=tag))
    assert fb.plan is None and fb.from_text == ()


def test_your_note_adds_its_word_after_the_ticked_ones_on_a_multiple_choice(tmp_path):
    answers = {**ANSWERS, T["why"]: "Things I hadn't said, Claude forgot what I told it earlier"}
    tag = "[cg-fb: outcome=partly why=missed,left_out worth=fair helped=context from_text=why]"
    fb = _settled(tmp_path, [_ask(0), _reply(1)] + _run(10, answers, tag=tag))
    assert fb.why == ("left_out", "missed") and fb.from_text == ("why",) and fb.other == ("why",)


def test_a_single_answer_in_your_own_words_gets_its_word_from_the_tag(tmp_path):
    answers = {**ANSWERS, T["worth"]: "Not really, it took far too long"}
    tag = "[cg-fb: outcome=met worth=no from_text=worth]"
    fb = _settled(tmp_path, [_ask(0), _reply(1)] + _run(10, answers, tag=tag))
    assert (fb.outcome, fb.worth, fb.from_text) == ("partly", "no", ("worth",))


def test_the_second_calls_note_is_read_too_and_the_next_call_sees_the_missed_word(tmp_path):
    first = {**ANSWERS, T["why"]: "Claude forgot something I said"}
    later = ([Q["missed_in"]], {T["missed_in"]: "It was somewhere in the chat before the plan"})
    tag = "[cg-fb: outcome=partly why=missed missed_in=earlier worth=fair helped=context from_text=why,missed_in]"
    fb = _settled(tmp_path, [_ask(0), _reply(1)] + _run(10, first, tag=tag, later=later))
    assert (fb.why, fb.missed_in, fb.from_text, fb.other) == (
        ("missed",), "earlier", ("why", "missed_in"), ("why", "missed_in"),
    )


def test_answers_only_in_your_own_words_are_skipped_unless_the_tag_picks_words(tmp_path):
    answers = {T["outcome"]: "it kind of worked", T["worth"]: "no idea"}
    plain = _settled(tmp_path, [_ask(0), _reply(1)] + _run(10, answers))
    assert plain == Feedback(source="skipped", other=("outcome", "worth"))
    tag = "[cg-fb: outcome=partly worth=fair from_text=outcome,worth]"
    picked = _settled(tmp_path, [_ask(0), _reply(1)] + _run(10, answers, tag=tag))
    assert picked == Feedback(outcome="partly", worth="fair", from_text=("outcome", "worth"),
                              other=("outcome", "worth"), source="answers")


def test_a_tag_and_a_declined_question_do_not_confirm_a_note(tmp_path):
    tag = "[cg-fb: outcome=met worth=yes why=missed from_text=why]"
    lines = [_ask(0), _reply(1)] + _run(10, declined=True, tag=tag)
    assert _settled(tmp_path, lines) == Feedback(outcome="met", worth="yes", source="tag")


def test_a_tag_with_no_question_behind_it_stands_without_the_words_from_a_note(tmp_path):
    tag = "[cg-fb: outcome=met why=missed worth=yes from_text=why]"
    lines = [_ask(0), _reply(1)] + _run(10, tag=tag, no_questions=True)
    assert _settled(tmp_path, lines) == Feedback(outcome="met", worth="yes", source="tag")
    only = "[cg-fb: why=missed from_text=why]"
    assert _settled(tmp_path, [_ask(0), _reply(1)] + _run(10, tag=only, no_questions=True)) == Feedback(
        source="skipped"
    )


def test_the_tag_names_the_tip_when_the_question_did_not():
    answered = Feedback(tip="known", source="answers")
    tag = Feedback(tip="known", tip_hint="plan_fresh", source="tag")
    assert settle_feedback(answered, tag, None) == Feedback(tip="known", tip_hint="plan_fresh", source="answers")
    named = Feedback(tip="known", tip_hint="drip_feed", source="answers")
    assert settle_feedback(named, tag, None).tip_hint == "drip_feed"


def test_merging_two_answers_joins_the_keys_that_were_your_own_words():
    first = Feedback(outcome="met", other=("outcome",), from_text=("outcome",), source="answers")
    second = Feedback(why=("missed",), other=("why",), source="answers")
    merged = merge_feedback(first, second)
    assert (merged.outcome, merged.why, merged.other, merged.from_text) == (
        "met", ("missed",), ("outcome", "why"), ("outcome",),
    )
    # Every field a Feedback has is carried.
    assert {f.name for f in dataclasses.fields(Feedback)} >= {
        "why", "missed_in", "plan", "tip", "tip_hint", "from_text", "other", "why_older",
    }


# -- the work each answer rates ----------------------------------------------------


def test_each_answer_rates_the_work_since_the_previous_feedback(tmp_path):
    lines = (
        [_ask(0, "one"), _reply(1), _ask(2, "two"), _reply(3)]
        + _run(10, ANSWERS, tu="tu_1")
        + [_ask(20, "three"), _reply(21)]
        + _run(30, declined=True, tu="tu_2")
        + [_ask(40, "four"), _reply(41), _ask(42, "five"), _reply(43)]
        + _run(50, ANSWERS, tag="[cg-fb: outcome=met]", tu="tu_3")
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


def test_a_rerun_with_no_work_since_replaces_the_previous_answers(tmp_path):
    changed = {**ANSWERS, T["outcome"]: "Yes", T["worth"]: "Too costly"}
    lines = [_ask(0, "one"), _reply(1)] + _run(10, ANSWERS, tu="tu_1") + _run(30, changed, tu="tu_2")
    cycles = capture.prompt_cycles(_parse(tmp_path, lines))
    [span] = capture.feedback_spans(cycles)
    assert (span.feedback.outcome, span.feedback.worth) == ("met", "no")
    assert cycles.index(span.run) == 2 and [cycles.index(c) for c in span.cycles] == [0]


def test_a_rerun_can_replace_a_run_made_first_thing_and_a_declined_one(tmp_path):
    lines = _run(0, ANSWERS, tu="tu_1") + _run(20, {**ANSWERS, T["outcome"]: "No"}, tu="tu_2")
    [span] = capture.feedback_spans(capture.prompt_cycles(_parse(tmp_path, lines)))
    assert span.feedback.outcome == "missed" and span.cycles == []
    lines = [_ask(0), _reply(1)] + _run(10, declined=True, tu="tu_1") + _run(30, ANSWERS, tu="tu_2")
    cycles = capture.prompt_cycles(_parse(tmp_path, lines, "declined_first.jsonl"))
    [span] = capture.feedback_spans(cycles)
    assert span.feedback.source == "answers" and [cycles.index(c) for c in span.cycles] == [0]


def test_a_declined_rerun_leaves_the_answers_as_they_were(tmp_path):
    lines = [_ask(0), _reply(1)] + _run(10, ANSWERS, tu="tu_1") + _run(30, declined=True, tu="tu_2")
    [span] = capture.feedback_spans(capture.prompt_cycles(_parse(tmp_path, lines)))
    assert span.feedback == Feedback(**ANSWERED, source="answers")


def test_a_rerun_after_more_work_rates_that_work_as_its_own_span(tmp_path):
    changed = {**ANSWERS, T["outcome"]: "Yes"}
    lines = (
        [_ask(0), _reply(1)] + _run(10, ANSWERS, tu="tu_1") + [_ask(30, "more"), _reply(31)]
        + _run(40, changed, tu="tu_2")
    )
    cycles = capture.prompt_cycles(_parse(tmp_path, lines))
    spans = capture.feedback_spans(cycles)
    assert [s.feedback.outcome for s in spans] == ["partly", "met"]
    assert [[cycles.index(c) for c in s.cycles] for s in spans] == [[0], [2]]


def test_the_runs_are_still_counted_when_a_rerun_replaces_the_answers(tmp_path):
    pricing = load_pricing(path=FIXTURES / "pricing_min.toml")
    top = _parse(tmp_path, [_ask(0), _reply(1)] + _run(10, ANSWERS, tu="tu_1") + _run(30, ANSWERS, tu="tu_2"))
    use = capture.usage(NS(sessions=[NS(top=top, subs=[], session_id="s1")]), pricing)
    assert use.feedback_runs == 2 and use.feedback_answered == 2


@pytest.mark.parametrize("old", ["tl-feedback", "cl-feedback"])
def test_a_run_under_an_earlier_name_still_counts(tmp_path, old):
    # /cg-feedback was /tl-feedback until 0.12.0, then /cl-feedback. Its
    # tag counts only in a genuine run (SEC-P1), so this fails unless the
    # old name is one.
    lines = [_ask(0), _reply(1)] + _run(10, tag="[cg-fb: outcome=met worth=yes]", skill=old, no_questions=True)
    cycles = capture.prompt_cycles(_parse(tmp_path, lines))
    assert [capture.is_feedback_run(c) for c in cycles] == [False, True]
    [span] = capture.feedback_spans(cycles)
    assert span.feedback.outcome == "met" and span.feedback.worth == "yes"


def test_feedback_run_first_thing_rates_nothing(tmp_path):
    spans = capture.feedback_spans(capture.prompt_cycles(_parse(tmp_path, _run(0, ANSWERS))))
    assert len(spans) == 1 and spans[0].cycles == []


def test_a_forged_tag_outside_a_feedback_run_is_ignored(tmp_path):
    # SEC-P1: `[cg-fb: ...]` is free text Claude could write in any
    # reply; it counts only when the cycle it's in actually ran the
    # /cg-feedback skill.
    lines = [_ask(0), _reply(1, text="Done.\n\n[cg-fb: outcome=met worth=yes handoff=yes]")]
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


# -- the plan check and the rating reminder: what they cost ---------------------------

_REMINDER_LINE = f"{catalogue.REMINDER_LABEL} {catalogue.FEEDBACK_REMINDER_LINE}"
_CHECK_Q = {"question": catalogue.PLAN_CHECK_QUESTION, "header": catalogue.PLAN_CHECK_HEADER}


def _coach_note(hint: str, text: str) -> str:
    """A feedback note as the hook writes it: the coaching marker and the hint, then the text."""
    return f"{catalogue.COACH_MARKER}{catalogue.COACH_VERSION} {hint}\n{text}"


def _hook_note(second: int, text: str) -> dict:
    """The hook's note on your message, as a transcript holds it."""
    wrapped = f"<system-reminder>\nUserPromptSubmit hook additional context: {text}\n</system-reminder>"
    line = attachment_line(
        "hook_additional_context", rendered=wrapped, content=[text], hookName="UserPromptSubmit",
        hookEvent="UserPromptSubmit", toolUseID="UserPromptSubmit",
    )
    line["timestamp"] = _ts(second)
    return line


def _fix_session(*, answer: str | None = "gap", declined: bool = False, reminder: bool = True) -> list[dict]:
    """A plan, a build, a fix of yours (the plan check's note, its question and how that came back), then a
    message of yours with the reminder's note and a reply ending with the reminder line."""
    lines = [
        _ask(0, "plan it"),
        _reply(1, tool_use_block("ExitPlanMode", "tu_plan", {"plan": "1. a\n2. b"})),
        user_block_line([tool_result_block("tu_plan", "ok")], timestamp=_ts(2)),
        _reply(3, tool_use_block("Edit", "tu_e", {"file_path": "src/a.py"})),
        user_block_line([tool_result_block("tu_e", "ok")], timestamp=_ts(4)),
        _reply(5, text="built"),
        _ask(10, "that is wrong"),
        _hook_note(10, _coach_note("plan_check", catalogue.FEEDBACK_NOTE_TEXT["plan_check"])),
        _reply(11, tool_use_block("AskUserQuestion", "tu_q", {"questions": [_CHECK_Q]})),
    ]
    if declined:
        lines.append(user_block_line([tool_result_block("tu_q", "no", is_error=True)], timestamp=_ts(12)))
    else:
        label = dict((w, label) for w, label, _d in catalogue.PLAN_CHECK_OPTIONS).get(answer, "my own words")
        result = {"questions": [_CHECK_Q], "answers": {_CHECK_Q["question"]: label}}
        lines.append(user_block_line([tool_result_block("tu_q", "ok")], toolUseResult=result, timestamp=_ts(12)))
    lines.append(_reply(13, text="fixed"))
    if reminder:
        lines += [
            _ask(20, "now the next thing"),
            _hook_note(20, _coach_note("rating_reminder", catalogue.FEEDBACK_NOTE_TEXT["rating_reminder"].replace("{tokens}", "1.3M"))),
            _reply(21, text=f"Done.\n\n{_REMINDER_LINE}"),
        ]
    return lines


def _fix_usage(tmp_path, lines, since: str = ""):
    pricing = load_pricing(path=FIXTURES / "pricing_min.toml")
    top = _parse(tmp_path, lines)
    # No capture note: capture is off, but the plan check and the reminder work at any level.
    assert top.meta.cap_injections == 0
    return capture.usage(NS(sessions=[NS(top=top, subs=[], session_id="s1")]), pricing, since=since), top, pricing


def test_the_plan_check_and_the_reminder_are_priced_in_any_session(tmp_path):
    use, top, _pricing = _fix_usage(tmp_path, _fix_session())
    assert (use.feedback_notes, use.plan_checks, use.plan_checks_answered, use.reminders) == (2, 1, 1, 1)
    assert use.answers == {"plan_check": 1}
    assert use.by_metric["plan_check"] > 0 and use.by_metric["feedback_reminder"] > 0
    # Both land in the feedback scope: the notes by size, Claude's words by what the question and the line are.
    scope = use.scopes["feedback"]
    notes = [e.size_chars for e in top.events if e.subkind == "coaching_note"]
    per = capture.CHARS_PER_TOKEN
    assert len(notes) == 2 and scope.note_tokens == sum(round(n / per) for n in notes)
    assert scope.tag_tokens == round(catalogue.PLAN_CHECK_ASK_CHARS / per) + round(catalogue.REMINDER_REPLY_CHARS / per)
    assert use.cost == pytest.approx(scope.cost) == pytest.approx(use.by_metric["plan_check"] + use.by_metric["feedback_reminder"])
    # Not a captured session: nothing else is counted.
    assert use.sessions == 0 and use.cycles == 0 and use.feedback_runs == 0
    assert capture.enough_data(use, "plan_check")[0] == 1


def test_a_declined_plan_check_is_counted_but_not_as_an_answer(tmp_path):
    use, _top, _pricing = _fix_usage(tmp_path, _fix_session(declined=True, reminder=False))
    assert (use.plan_checks, use.plan_checks_answered, use.reminders) == (1, 0, 0)
    assert use.answers == {} and use.feedback_notes == 1
    other, _top, _pricing = _fix_usage(tmp_path, _fix_session(answer=None, reminder=False))
    assert (other.plan_checks, other.plan_checks_answered) == (1, 0)


def test_the_reminder_alone_costs_its_note_and_its_line(tmp_path):
    lines = _fix_session()[-3:]
    use, _top, _pricing = _fix_usage(tmp_path, lines)
    assert (use.feedback_notes, use.plan_checks, use.reminders) == (1, 0, 1)
    assert use.by_metric.get("plan_check") is None and use.by_metric["feedback_reminder"] > 0
    assert use.scopes["feedback"].tag_tokens == round(catalogue.REMINDER_REPLY_CHARS / capture.CHARS_PER_TOKEN)


def test_a_reminder_note_nobody_followed_still_costs_the_note(tmp_path):
    lines = _fix_session()[:-1] + [_reply(21, text="Done.")]
    use, _top, _pricing = _fix_usage(tmp_path, lines)
    assert (use.feedback_notes, use.reminders) == (2, 0)
    assert use.by_metric["feedback_reminder"] > 0


def test_the_plan_check_and_the_reminder_before_since_are_left_out(tmp_path):
    use, _top, _pricing = _fix_usage(tmp_path, _fix_session(), since=_ts(15))
    assert (use.feedback_notes, use.plan_checks, use.reminders) == (1, 0, 1)
    assert "plan_check" not in use.by_metric


def test_feedback_usage_counts_them_for_the_capture_tab_whatever_the_level(tmp_path):
    pricing = load_pricing(path=FIXTURES / "pricing_min.toml")
    top = _parse(tmp_path, _fix_session())
    use = capture.feedback_usage(NS(sessions=[NS(top=top, subs=[], session_id="s1")]), pricing)
    assert (use.plan_checks, use.plan_checks_answered, use.reminders, use.feedback_notes) == (1, 1, 1, 2)
    assert use.spend > 0 and use.sessions == 0


def test_the_facts_line_is_no_coaching_note_to_price_and_the_feedback_notes_are_not_tips(tmp_path):
    facts = f"{catalogue.FEEDBACK_FACTS_MARKER} tokens=1300000 typical=0 followups=2"
    top = _parse(tmp_path, [*_fix_session(), _hook_note(30, facts)])
    notes = list(capture._coaching_notes(top))
    assert notes == []
    pricing = load_pricing(path=FIXTURES / "pricing_min.toml")
    assert capture.coaching_usage(NS(sessions=[NS(top=top, subs=[], session_id="s1")]), pricing).sessions == 0

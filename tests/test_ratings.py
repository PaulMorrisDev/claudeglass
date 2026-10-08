"""What the dashboard's rating asks of a session (``ratings.py``): the facts
worked out from a stored transcript, the questions those facts leave in,
a row for each plan build, and the sessions the banner lists.

Amounts use ``tests/fixtures/pricing_min.toml``'s ``claude-widget-9``;
only token counts matter here.
"""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from claudeglass import capture, capture_catalogue as catalogue, parse, ratings
from claudeglass.model import TranscriptMeta
from claudeglass.parse import parse_transcript

from test_capture_feedback import _run as feedback_run
from test_pieces import limited_session_lines

from helpers import (
    attachment_line,
    tool_result_block,
    tool_use_block,
    turn_line,
    user_block_line,
    user_str_line,
    write_jsonl,
)

MODEL = "claude-widget-9"


@pytest.fixture(autouse=True)
def _salt():
    parse.set_salt(b"r" * 32)


def _ts(second: int) -> str:
    return f"2026-09-18T12:{second // 60:02d}:{second % 60:02d}.000Z"


def _ask(second: int, text: str = "do it") -> dict:
    return user_str_line(text, origin={"kind": "human"}, timestamp=_ts(second))


def _reply(second: int, *blocks, text: str = "ok", **kw) -> dict:
    content = list(blocks) or [{"type": "text", "text": text}]
    return turn_line(content=content, model=MODEL, timestamp=_ts(second), **kw)


def _ok(second: int, *ids: str) -> dict:
    return user_block_line([tool_result_block(tool_use_id, "ok") for tool_use_id in ids], timestamp=_ts(second))


def _edit(tool_id: str, path: str = "C:/Dev/repo/a.py") -> dict:
    return tool_use_block("Edit", tool_id, {"file_path": path})


def _queued(prompt: str, second: int) -> dict:
    """A message typed while Claude was working, as its attachment is
    written some time later."""
    line = attachment_line(
        "queued_command", prompt=prompt, commandMode="prompt", origin={"kind": "human"}, timestamp=_ts(second)
    )
    line["timestamp"] = _ts(second + 15)
    return line


def _bundle(tmp_path, lines) -> NS:
    path = tmp_path / "top.jsonl"
    write_jsonl(path, lines)
    top = parse_transcript(path, TranscriptMeta(path=str(path), kind="top-level"))
    return NS(top=top, subs=[], session_id="s1", workflows=())


def _plain(tmp_path) -> NS:
    return _bundle(tmp_path, [
        _ask(0, "build the thing"),
        _reply(1, _edit("e1")), _ok(2, "e1"), _reply(3, text="Done."),
        _ask(10, "the title is missing"),
        _reply(11, _edit("e2")), _ok(12, "e2"), _reply(13, text="Fixed."),
        _ask(20, "and the footer too"),
        _reply(21, _edit("e3")), _ok(22, "e3"), _reply(23, text="Done."),
    ])


def _two_plans(tmp_path) -> NS:
    return _bundle(tmp_path, [
        _ask(0, "plan the change"),
        _reply(1, tool_use_block("ExitPlanMode", "tu_p1", {"plan": "1. Edit a.py"})), _ok(2, "tu_p1"),
        _reply(3, _edit("e1")), _ok(4, "e1"), _reply(5, text="Built."),
        _ask(10, "the title is wrong"),
        _reply(11, _edit("e2")), _ok(12, "e2"), _reply(13, text="Fixed."),
        _ask(20, "now plan the second part"),
        _reply(21, tool_use_block("ExitPlanMode", "tu_p2", {"plan": "1. Edit b.py"})), _ok(22, "tu_p2"),
        _reply(23, _edit("e3", "C:/Dev/repo/b.py")), _ok(24, "e3"), _reply(25, text="Built."),
    ])


# -- how a count reads ----------------------------------------------------------


@pytest.mark.parametrize(
    ("tokens", "text"),
    [(0, "0"), (456, "456"), (2400, "2.4k"), (15_000, "15k"), (420_000, "420k"), (999_999, "1M"), (1_300_000, "1.3M")],
)
def test_tokens_read_as_the_hook_says_them(tokens, text):
    assert ratings.tokens_text(tokens) == text


# -- the facts ---------------------------------------------------------------------


def test_the_facts_hold_counts_and_words_only(tmp_path):
    facts = ratings.session_facts(_plain(tmp_path), typical=500)
    assert set(facts) == set(catalogue.FEEDBACK_FACT_KEYS)
    assert facts["followups"] == 2 and facts["queued"] == 0
    assert facts["plan"] == "none" and facts["build"] == "none" and facts["plan_followups"] == 0
    assert facts["typical"] == 500 and facts["tokens"] > 0
    assert facts["tips"] == "none" and facts["tip"] == "none" and facts["admits"] == 0
    assert all(isinstance(value, (int, str)) for value in facts.values())


def test_a_message_typed_while_claude_works_is_a_followup_and_is_queued(tmp_path):
    bundle = _bundle(tmp_path, [
        _ask(0, "refactor the parser"),
        _reply(5, tool_use_block("Bash", "tu_5", {"command": "ls"})),
        _queued("also add a test for the parser", 10),
        _ok(30, "tu_5"), _reply(40, text="Done."),
        _ask(60, "and rename the helper"),
        _reply(70, text="Done."),
    ])
    facts = ratings.session_facts(bundle)
    assert facts["queued"] == 1 and facts["followups"] == 2


def test_a_plan_you_approved_shows_in_the_plan_facts(tmp_path):
    bundle = _bundle(tmp_path, [
        _ask(0, "plan it"),
        _reply(1, tool_use_block("ExitPlanMode", "tu_p", {"plan": "1. Edit a.py"})), _ok(2, "tu_p"),
        _reply(3, _edit("e1")), _ok(4, "e1"), _reply(5, text="Built."),
        _ask(10, "the title is wrong"),
        _reply(11, _edit("e2")), _ok(12, "e2"), _reply(13, text="Fixed."),
    ])
    facts = ratings.session_facts(bundle)
    assert (facts["plan"], facts["plan_followups"], facts["plan_asked"], facts["build"]) == ("approved", 1, 0, "same")
    assert [b["build"] for b in ratings.session_builds(bundle)] == [1]


def test_a_session_without_a_plan_has_no_builds(tmp_path):
    assert ratings.session_builds(_plain(tmp_path)) == []


def test_two_approved_plans_are_two_builds_each_with_its_own_followups(tmp_path):
    builds = ratings.session_builds(_two_plans(tmp_path))
    assert [b["build"] for b in builds] == [1, 2]
    assert builds[0]["plan_followups"] >= 1 and builds[1]["plan_followups"] == 0
    assert builds[0]["built"] and builds[1]["built"]


def test_a_tip_claude_showed_names_the_hint(tmp_path, monkeypatch):
    bundle = _plain(tmp_path)
    shown = {"drip_feed": 2, "big_paste": 1}
    monkeypatch.setattr(ratings.prompting, "_tips", lambda top: (dict(shown), dict(shown), {}))
    facts = ratings.session_facts(bundle)
    assert facts["tip"] == "drip_feed"
    assert facts["tips"] == "drip_feed:2,big_paste:1"


# -- the questions -----------------------------------------------------------------


def _keys(rows) -> list[str]:
    return [row["key"] for row in rows]


def test_a_session_with_no_followups_skips_the_followup_questions():
    facts = {"followups": 0, "plan": "none", "tip": "none", "tokens": 5000, "typical": 0}
    assert _keys(ratings.question_rows(facts)) == ["outcome", "worth", "helped"]


def test_followups_bring_the_why_question_and_the_one_that_waits_for_missed():
    rows = ratings.question_rows({"followups": 3, "queued": 1, "plan": "none", "tip": "none"})
    assert _keys(rows) == ["outcome", "why", "missed_in", "worth", "helped"]
    by_key = {row["key"]: row for row in rows}
    assert by_key["missed_in"]["needs"] == "missed" and by_key["why"]["needs"] == ""
    assert "3 more messages" in by_key["why"]["question"] and "1 " in by_key["why"]["question"]
    assert all(option["description"] for row in rows for option in row["options"])


def test_without_facts_the_page_asks_only_what_needs_none():
    rows = ratings.question_rows(None)
    assert _keys(rows) == ["outcome", "why", "missed_in", "worth", "helped"]
    assert all("{" not in row["question"] for row in rows)
    assert all(row["tip_hint"] == "" and row["builds"] == [] for row in rows)


def test_a_tip_the_session_showed_gets_its_question_and_its_hint():
    rows = ratings.question_rows({"followups": 0, "plan": "none", "tip": "drip_feed"})
    tip = next(row for row in rows if row["key"] == "tip")
    assert tip["tip_hint"] == "drip_feed"
    assert catalogue.TIP_HINT_TITLES["drip_feed"] in tip["question"]


def test_one_plan_is_asked_about_once():
    facts = {"followups": 1, "plan": "approved", "plan_followups": 1, "plan_asked": 0, "build": "same", "tip": "none"}
    builds = [{"build": 1, "plan_followups": 1, "plan_asked": 0, "built": True}]
    rows = ratings.question_rows(facts, builds)
    assert {"plan", "handoff"} <= set(_keys(rows))
    assert all(row["builds"] == [] for row in rows)


def test_two_plans_get_a_row_for_each_build_under_the_plan_and_handoff_questions():
    facts = {"followups": 2, "plan": "approved", "plan_followups": 0, "plan_asked": 0, "build": "same", "tip": "none"}
    builds = [
        {"build": 1, "plan_followups": 2, "plan_asked": 0, "built": True},
        {"build": 2, "plan_followups": 0, "plan_asked": 0, "built": True},
    ]
    rows = {row["key"]: row for row in ratings.question_rows(facts, builds)}
    # The plan question applies where follow-ups came after the approval.
    assert [item["build"] for item in rows["plan"]["builds"]] == [1]
    assert [item["build"] for item in rows["handoff"]["builds"]] == [1, 2]
    assert [item["label"] for item in rows["handoff"]["builds"]] == ["Plan 1 of 2", "Plan 2 of 2"]
    assert rows["outcome"]["builds"] == []


def test_a_build_nobody_built_has_no_handoff_row():
    facts = {"followups": 0, "plan": "approved", "build": "same", "tip": "none"}
    builds = [
        {"build": 1, "plan_followups": 0, "plan_asked": 0, "built": True},
        {"build": 2, "plan_followups": 0, "plan_asked": 0, "built": False},
    ]
    rows = {row["key"]: row for row in ratings.question_rows(facts, builds)}
    assert [item["build"] for item in rows["handoff"]["builds"]] == [1]


def test_the_session_rating_questions_are_the_skills_questions():
    assert [q.key for q in catalogue.RATING_QUESTIONS] == [q.key for q in catalogue.FEEDBACK_QUESTIONS]
    for q in catalogue.RATING_QUESTIONS:
        assert set(catalogue.RATING_VOCAB[q.key]) >= {word for word, _label, _text in q.options}


def test_a_filled_question_carries_no_placeholder():
    for q in catalogue.RATING_QUESTIONS:
        for facts in (None, {}, {"followups": 4, "queued": 2, "tokens": 2_500_000, "typical": 500_000, "tip": "drip_feed"}):
            assert "{" not in ratings.fill_question(q, facts)


def test_the_worth_question_says_how_big_the_work_was():
    worth = next(q for q in catalogue.RATING_QUESTIONS if q.key == "worth")
    text = ratings.fill_question(worth, {"tokens": 2_500_000, "typical": 1_000_000})
    assert text.startswith("This work used about 2.5M tokens, about 2.5") and "your usual piece" in text
    # With no typical piece to compare to, it says the size and nothing else.
    assert ratings.fill_question(worth, {"tokens": 2_500_000, "typical": 0}) == (
        "This work used about 2.5M tokens. Was the result worth it?"
    )
    assert ratings.fill_question(worth, None) == worth.plain


# -- the sessions the banner lists ------------------------------------------------------------


def test_the_reminder_threshold_is_the_larger_of_the_floor_and_a_multiple_of_the_typical_piece():
    thresholds = ratings.coaching_thresholds(None, {})
    floor = thresholds["rating_min_tokens"]
    factor = thresholds["rating_typical_factor"]
    assert ratings.reminder_tokens(thresholds, 0) == int(floor)
    assert ratings.reminder_tokens(thresholds, int(floor)) == int(floor * factor)


def test_your_own_thresholds_move_it_the_way_the_hook_reads_them():
    personal = {"thresholds": {"rating_min_tokens": 2_000_000, "rating_typical_factor": "bad"}}
    out = ratings.coaching_thresholds({"coaching_rating_min_tokens": 3_000_000}, personal)
    assert out["rating_min_tokens"] == 3_000_000
    assert out["rating_typical_factor"] == catalogue.COACHING_THRESHOLDS["rating_typical_factor"]
    assert ratings.coaching_thresholds({"coaching_rating_min_tokens": -1}, {})["rating_min_tokens"] == (
        catalogue.COACHING_THRESHOLDS["rating_min_tokens"]
    )


def _cleared(tmp_path, *, rated: bool = False) -> NS:
    """Two pieces of work in one session: a /clear sits between them. With ``rated``, a /cg-feedback run
    follows the first one."""
    lines = [
        _ask(0, "build the thing"),
        _reply(1, _edit("e1")), _ok(2, "e1"), _reply(3, text="Done."),
    ]
    if rated:
        # A run in which Claude asked nothing: no answer was kept, and it still rates the work.
        lines += feedback_run(5, no_questions=True)
    lines += [
        user_str_line("<command-name>/clear</command-name>\n<command-message>clear</command-message>\n"
                      "<command-args></command-args>", timestamp=_ts(10)),
        _ask(20, "now something else entirely"),
        _reply(21, _edit("e2")), _ok(22, "e2"), _reply(23, text="Done."),
    ]
    return _bundle(tmp_path, lines)


def test_a_piece_is_listed_once_it_reaches_the_threshold(tmp_path):
    bundle = _plain(tmp_path)
    tokens = ratings.session_facts(bundle)["tokens"]
    [piece] = ratings.unrated_pieces(bundle, tokens)
    assert (piece["tokens"], piece["part"]) == (tokens, 1)
    assert ratings.unrated_pieces(bundle, tokens + 1) == []


def test_a_usage_limit_pause_is_no_silence_between_pieces(tmp_path):
    # An hour of the 3h29m50s between the messages was a limit that held the work up: one piece.
    [only] = ratings.unrated_pieces(_bundle(tmp_path, limited_session_lines()), 1)
    assert only["part"] == 1
    # With no limit between them the same two messages are two pieces.
    first, second = ratings.unrated_pieces(_bundle(tmp_path, limited_session_lines(limit=False)), 1)
    assert (first["part"], second["part"]) == (1, 2)


def test_a_piece_is_named_by_its_place_its_task_and_its_messages(tmp_path):
    [piece] = ratings.unrated_pieces(_plain(tmp_path), 1)
    assert set(piece) == {"tokens", "end_ts", "part", "label"}
    assert piece["label"] == "3 messages" and piece["end_ts"] == _ts(23)
    assert ratings.piece_label("", 0, 1, 1) == ""
    assert ratings.piece_label("feature", 1, 1, 1) == "feature, 1 message"
    assert ratings.piece_label("bugfix", 4, 2, 3) == "piece 2 of 3, bugfix, 4 messages"


def test_a_pieces_label_counts_the_messages_sent_while_background_work_ran(tmp_path, monkeypatch):
    # A message sent while background work ran is out of the piece's requests, but you still typed it.
    piece = ratings.pieces_mod.WorkPiece(tokens=500, substantive=2, aside_cycles=3, task="feature")
    monkeypatch.setattr(ratings.pieces_mod, "pieces_of", lambda *args, **kwargs: [piece])
    [listed] = ratings.unrated_pieces(_plain(tmp_path), 1)
    assert listed["label"] == "feature, 5 messages"


def test_a_session_of_two_pieces_lists_each_that_reaches_the_threshold(tmp_path):
    bundle = _cleared(tmp_path)
    first, second = ratings.unrated_pieces(bundle, 1)
    assert (first["part"], second["part"]) == (1, 2)
    assert first["label"] == "piece 1 of 2, 1 message" and second["label"] == "piece 2 of 2, 1 message"
    # Each is a piece of its own: 2 replies of 150 tokens, not the whole session's 4.
    assert (first["tokens"], second["tokens"]) == (300, 300)
    assert [p["part"] for p in ratings.unrated_pieces(bundle, 301)] == []
    assert ratings.session_facts(bundle)["tokens"] == 600


def test_a_piece_you_rated_with_a_feedback_run_is_not_listed_but_the_one_after_it_is(tmp_path):
    [only] = ratings.unrated_pieces(_cleared(tmp_path, rated=True), 1)
    assert only["part"] == 2


def test_a_session_you_rated_with_a_feedback_run_is_not_listed(tmp_path, monkeypatch):
    bundle = _plain(tmp_path)
    monkeypatch.setattr(capture, "is_feedback_run", lambda cycle: True)
    assert ratings.unrated_pieces(bundle, 1) == []


def test_a_session_with_no_main_transcript_lists_no_pieces():
    assert ratings.unrated_pieces(NS(top=None, subs=[], workflows=()), 1) == []


def test_a_queued_message_written_again_as_a_user_line_is_one_followup_and_queued(tmp_path):
    bundle = _bundle(tmp_path, [
        _ask(0, "refactor the parser"),
        _reply(5, tool_use_block("Bash", "tu_5", {"command": "ls"})),
        _queued("also add a test for the parser", 10),
        _ok(30, "tu_5"), _reply(40, text="Done."),
        _ask(42, "also add a test for the parser"),
        _reply(50, text="Done."),
    ])
    facts = ratings.session_facts(bundle)
    assert (facts["followups"], facts["queued"]) == (1, 1)

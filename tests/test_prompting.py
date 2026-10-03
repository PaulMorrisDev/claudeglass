"""How you prompt (``prompting``): what the parser keeps about each message
and reply for it, each habit counted after the fact with the live hints'
rules, what each cost, and the section built from them.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest

from claudeglass import capture_catalogue as cat, events as events_mod, habits, prompting
from claudeglass.model import TranscriptMeta
from claudeglass.parse import parse_transcript
from claudeglass.pricing import load_pricing

from helpers import (
    attachment_line,
    tool_result_block,
    tool_use_block,
    turn_line,
    user_block_line,
    user_str_line,
    write_jsonl,
)

PRICING = load_pricing()


def _at(minute: float) -> str:
    whole = int(minute)
    return f"2026-09-18T12:{whole:02d}:{int((minute - whole) * 60):02d}.000Z"


def _said(text: str, minute: float, **extra) -> dict:
    return user_str_line(text, timestamp=_at(minute), origin={"kind": "human"}, **extra)


def _reply(minute: float, *, edit: bool = False, say: str = "Done.", plan: bool = False, ctx: int = 40_000) -> dict:
    content = []
    if edit:
        content.append(tool_use_block("Edit", f"toolu_{minute}"))
    if plan:
        content.append(tool_use_block("ExitPlanMode", f"toolu_p{minute}", {"plan": "1. do it"}))
    content.append({"type": "text", "text": say})
    return turn_line(timestamp=_at(minute), content=content, cache_read_input_tokens=ctx, output_tokens=200)


def _stop(minute: float) -> dict:
    return user_str_line("[Request interrupted by user]", timestamp=_at(minute))


def _call(minute: float, name: str, tool_input: dict, *, say: str = "Done.") -> dict:
    """A reply that makes one ``name`` call, then says ``say``."""
    content = [tool_use_block(name, f"toolu_c{minute}", tool_input), {"type": "text", "text": say}]
    return turn_line(timestamp=_at(minute), content=content, cache_read_input_tokens=40_000, output_tokens=200)


def _came_back(minute: float, call_minute: float, *, is_error: bool = False, edited_files: int = 0) -> dict:
    """The result of ``_call(call_minute, ...)``; a subagent's also says how
    many files it changed."""
    extra = {"toolUseResult": {"toolStats": {"editFileCount": edited_files}}} if edited_files else {}
    block = tool_result_block(f"toolu_c{call_minute}", "no" if is_error else "ok", is_error=is_error)
    return user_block_line([block], timestamp=_at(minute), **extra)


def _parse(tmp_path, lines, name="s.jsonl"):
    path = tmp_path / name
    write_jsonl(path, lines)
    return parse_transcript(path, TranscriptMeta(path=str(path)))


def _session(tmp_path, lines, name="s.jsonl"):
    top = _parse(tmp_path, lines, name)
    return prompting.session_prompting(NS(top=top, session_id=name), prompting._Prices(PRICING))


#: The work's first message: one request in a few plain sentences, not a
#: list of changes (so it isn't a job to plan first).
_START = [
    _said(
        "Build the settings page to look just like the dashboard does. The form sits in the middle of it. "
        "The header shows the name of the signed in user. The footer stays the way it is today.", 0,
    ),
    _reply(1, edit=True),
]


# -- what the parser keeps ----------------------------------------------------------


def test_the_parser_keeps_counts_and_flags_about_each_message_and_reply(tmp_path):
    big = "Add a login page, a settings page, email alerts and an admin screen, and move the DB to Postgres."
    result = _parse(tmp_path, [
        _said(big, 0, permissionMode="default"), _reply(1, say="Which database version do you use?"),
        _said("it's broken", 2), _reply(3, say="Fixed.\n\n> **ClaudeGlass tip:** Say what you saw."),
        _said("thanks!", 4), _reply(5),
        _said("Add a login page with email and a password field", 6, permissionMode="plan"), _reply(7),
        _said("add a login page with email and a password field", 8), _reply(9),
    ])
    turns = result.turns
    assert turns[0].prompt_steps == 5 and not turns[0].prompt_plan_mode and turns[0].reply_asked
    assert turns[1].human_vague and turns[1].coach_tip and not turns[1].reply_asked
    assert turns[2].human_ack and not turns[2].human_vague
    assert turns[3].prompt_plan_mode and not turns[3].human_repeat
    assert turns[4].human_repeat
    # Nothing about the words is kept on the events either.
    human = [e for e in result.events if e.kind.name == "HUMAN_TEXT"]
    assert all(set(e.detail) <= {"has_paste", "correction", "steps", "vague", "ack", "repeat", "plan_mode", "flags"}
               for e in human)


def test_a_message_resent_after_esc_before_any_reply_is_one_message_and_a_stop(tmp_path):
    # As a real interactive session writes it: Esc before any reply puts the
    # message back to edit; the resend has the same parent line.
    first = _said("Use SQLite instead of JSON for the store, with the same class", 2, parentUuid="p1")
    again = _said("Use SQLite instead of JSON for the store, with the same class", 3, parentUuid="p1")
    result = _parse(tmp_path, [*_START, first, again, _reply(4, edit=True)])
    humans = [e for e in result.events if e.kind.name == "HUMAN_TEXT"]
    assert humans[1].detail.get("replaced") and not humans[2].detail.get("replaced")
    # Not a repeat of itself, and its length counted once.
    turn = result.turns[1]
    assert not turn.human_repeat and turn.human_prompt_chars == len(first["message"]["content"])
    session = prompting.session_prompting(NS(top=result, session_id="s"), prompting._Prices(PRICING))
    assert "repeat_ask" not in session.counts()


def test_stops_before_any_reply_count_towards_a_stop_loop(tmp_path):
    session = _session(tmp_path, [
        *_START,
        _said("refactor the store", 2, parentUuid="a"), _said("refactor the store module", 3, parentUuid="a"),
        _reply(4, edit=True), _stop(5),
        _said("keep the API", 6, parentUuid="b"), _said("keep the API as it is", 7, parentUuid="b"),
        _reply(8, edit=True),
    ])
    # One stop mid-reply, two before any reply: three in 20 minutes.
    assert session.counts()["stop_loop"] == 1


def test_a_message_about_a_plan_has_no_step_count(tmp_path):
    result = _parse(tmp_path, [_said("Carry out the plan: add login, settings, alerts and an admin screen.", 0),
                               _reply(1)])
    assert result.turns[0].prompt_steps == 0


def test_the_step_count_is_of_your_own_prose_and_not_of_a_review_or_a_plan_already(tmp_path):
    big = "Add a login page, a settings page, email alerts and an admin screen, and move the DB to Postgres."
    lines = [
        # Your own prose, a list of changes.
        _said(big, 0), _reply(1),
        # A pasted block and a quoted line aren't steps you asked for.
        _said("Why does this fail?\n```\nadd a\nadd b\nadd c\nadd d\n```\nand\n> add e\n> add f", 2), _reply(3),
        # A review asks for no change.
        _said("Review this: " + big, 4), _reply(5),
        # A plan already: five numbered items, or a long message with headings.
        _said("Do these:\n1. add a\n2. add b\n3. add c\n4. add d\n5. add e", 6), _reply(7),
        _said("# Work\n\n## Pages\n" + big + "\n\n" + "More detail on how it should look. " * 50, 8), _reply(9),
        # Two changes: below the line a plan is called for at, but a count all the same.
        _said("Add a login page and rename the old one", 10), _reply(11),
    ]
    steps = [turn.prompt_steps for turn in _parse(tmp_path, lines).turns]
    assert steps == [5, 0, 0, 0, 0, 2]


def test_the_parser_flags_a_message_that_only_asks_something(tmp_path):
    result = _parse(tmp_path, [
        _said("why is the button blue?", 0), _reply(1), _said("make the button red", 2), _reply(3),
        _said("what does the footer do", 4), _reply(5), _said("go ahead", 6), _reply(7),
    ])
    assert [turn.human_question for turn in result.turns] == [True, False, True, False]
    # A flag on the event, never the words.
    human = [e for e in result.events if e.kind.name == "HUMAN_TEXT"]
    assert [bool(e.detail.get("question")) for e in human] == [True, False, True, False]


# -- each habit after the fact ------------------------------------------------------


def test_small_requests_answered_with_file_changes_are_one_drip_run(tmp_path):
    session = _session(tmp_path, [
        *_START,
        # A question after a change is an offer: the next message is a request like any other.
        _said("make the button bigger", 2), _reply(3, edit=True, say="Done. Left or right?"),
        _said("left", 4), _reply(5, edit=True),
        _said("now move the logo", 6), _reply(7, edit=True),
        _said("and the footer text", 8), _reply(9, edit=True),
        _said("thanks", 10), _reply(11),
    ])
    drips = [o for o in session.occurrences if o.habit == "drip_feed"]
    assert len(drips) == 1
    assert not session.messages[2].answers
    # What the messages after the first paid to take in the context.
    rereads = [m.reread for m in session.messages]
    assert drips[0].cost > 0 and abs(drips[0].cost - (rereads[2] + rereads[3] + rereads[4])) < 1e-9


def test_a_go_ahead_a_question_and_the_answer_to_one_neither_count_nor_end_a_run(tmp_path):
    session = _session(tmp_path, [
        *_START,
        _said("make the button bigger", 2), _reply(3, edit=True),
        _said("go ahead", 4), _reply(5),
        _said("which side suits the logo?", 6), _reply(7, say="Left or right of the title?"),
        _said("left", 8), _reply(9, edit=True),
        _said("now move the logo", 10), _reply(11, edit=True),
        _said("and the footer text", 12), _reply(13, edit=True),
    ])
    drips = [o for o in session.occurrences if o.habit == "drip_feed"]
    assert len(drips) == 1
    assert [m.question for m in session.messages] == [False, False, False, True, False, False, False]
    assert session.messages[4].answers
    # The request that began the run, and the two after it.
    rereads = [m.reread for m in session.messages]
    assert abs(drips[0].cost - (rereads[5] + rereads[6])) < 1e-9


#: The reply to a message, and whether it changed a file of yours.
_EDITING_REPLIES = {
    "an edit": (lambda: [_call(3, "Edit", {"file_path": "/work/app/a.css"})], True),
    "a shell write": (lambda: [_call(3, "Bash", {"command": "sed -i 's/a/b/' /work/app/a.css"})], True),
    "a subagent's edits": (
        lambda: [_call(3, "Agent", {"prompt": "go"}), _came_back(3.5, 3, edited_files=2), _reply(4)], True),
    "an edit under .claude": (lambda: [_call(3, "Edit", {"file_path": "/home/me/.claude/plans/p.md"})], False),
    "a shell write under .claude": (
        lambda: [_call(3, "Bash", {"command": "echo hi > /home/me/.claude/notes.md"})], False),
    "a failed edit": (
        lambda: [_call(3, "Edit", {"file_path": "/work/app/a.css"}), _came_back(3.5, 3, is_error=True), _reply(4)],
        False),
    "a command that reads": (lambda: [_call(3, "Bash", {"command": "ls -la"})], False),
    "a subagent that changed nothing": (
        lambda: [_call(3, "Agent", {"prompt": "go"}), _came_back(3.5, 3), _reply(4)], False),
    "a subagent that failed": (
        lambda: [_call(3, "Agent", {"prompt": "go"}), _came_back(3.5, 3, is_error=True, edited_files=2), _reply(4)],
        False),
}


@pytest.mark.parametrize("name", list(_EDITING_REPLIES))
def test_a_message_is_answered_by_a_change_to_a_file_of_yours(tmp_path, name):
    reply, edited = _EDITING_REPLIES[name]
    session = _session(tmp_path, [*_START, _said("make the button bigger", 2), *reply()])
    assert session.messages[1].edited is edited, name


def test_the_parser_counts_what_went_into_a_claude_folder_and_the_files_a_subagent_changed(tmp_path):
    result = _parse(tmp_path, [
        *_START, _said("save a note", 2),
        _call(3, "Write", {"file_path": "C:\\Users\\me\\.claude\\memory\\m.md"}),
        _said("and the plan", 4),
        _call(5, "Bash", {"command": "echo a > /home/me/.claude/a.md && echo b > /work/app/b.md"}),
        _said("and a subagent", 6),
        _call(7, "Agent", {"prompt": "go"}), _came_back(7.5, 7, edited_files=3), _reply(8),
    ])
    turns = [t for t in result.turns if t.tool_use_ids]
    assert [(t.config_edit_count, t.shell_write_count, t.agent_edit_files) for t in turns[-3:]] == [
        (1, 0, 0), (1, 2, 0), (0, 0, 3)]


def test_a_reply_that_changed_nothing_or_a_long_wait_breaks_the_run(tmp_path):
    no_change = _session(tmp_path, [
        *_START, _said("make the button bigger", 2), _reply(3, edit=True),
        _said("now move the logo", 4), _reply(5), _said("and the footer text", 6), _reply(7, edit=True),
    ], "a.jsonl")
    assert "drip_feed" not in no_change.counts()
    late = _session(tmp_path, [
        *_START, _said("make the button bigger", 2), _reply(3, edit=True),
        _said("now move the logo", 40), _reply(41, edit=True), _said("and the footer text", 42), _reply(43, edit=True),
    ], "b.jsonl")
    assert "drip_feed" not in late.counts()


def test_repeats_vague_fixes_big_pastes_and_big_tasks(tmp_path):
    big_task = (
        "Add a login page with email and password, a settings page where people change their name, email alerts "
        "when a report is ready, and an admin screen that lists every account."
    )
    session = _session(tmp_path, [
        *_START,
        _said("Make the save button bigger and move it to the right", 2), _reply(3, edit=True),
        _said("make the save button bigger and move it right", 4), _reply(5, edit=True),
        _said("still broken", 6), _reply(7, say="What do you see?"),
        _said("the button is grey", 8), _reply(9, edit=True),
        _said("Why does this fail?\n" + "log line\n" * 5_000, 10), _reply(11), _reply(12), _reply(13),
        _said(big_task, 14, permissionMode="default"), _reply(15, edit=True),
    ])
    by = {o.habit: o for o in session.occurrences}
    assert set(by) == {"repeat_ask", "vague_fix", "big_paste", "plan_first"}
    # The attempt that missed, no figure at all for a vague correction, carrying the paste.
    assert by["repeat_ask"].cost == session.messages[1].cost
    assert by["vague_fix"].cost is None
    assert by["big_paste"].cost > 0 and by["plan_first"].cost is None


def test_a_repeat_needs_the_earlier_message_to_have_been_answered_with_an_edit(tmp_path):
    ask = "Make the save button bigger and move it to the right"
    again = "make the save button bigger and move it right"
    edited = _session(tmp_path, [*_START, _said(ask, 2), _reply(3, edit=True), _said(again, 4), _reply(5, edit=True)],
                      "a.jsonl")
    assert edited.counts()["repeat_ask"] == 1
    # Claude only talked: nothing missed, so asking again isn't a repeat of a miss.
    talked = _session(tmp_path, [*_START, _said(ask, 2), _reply(3), _said(again, 4), _reply(5, edit=True)], "b.jsonl")
    assert "repeat_ask" not in talked.counts()
    assert talked.messages[2].repeat and not talked.messages[1].edited


def test_a_poll_or_a_go_ahead_is_never_a_repeat(tmp_path):
    """The parser leaves them unflagged, and the habit would skip them
    anyway: each poll is a ``status_poll``, priced from its own reply."""
    session = _session(tmp_path, [
        *_START,
        _said("are you done yet?", 2), _reply(3, edit=True),
        _said("are you done yet?", 4), _reply(5, edit=True),
        _said("is it finished yet", 6), _reply(7, edit=True),
        _said("thanks!", 8), _reply(9, edit=True), _said("thanks!", 10), _reply(11, edit=True),
    ])
    assert "repeat_ask" not in session.counts()
    assert [m.repeat for m in session.messages] == [False] * len(session.messages)
    assert [m.status for m in session.messages[1:4]] == [True, True, True]
    assert session.messages[4].ack and session.messages[5].ack
    # The three polls are the ones counted, each at the cost of the reply it drew.
    polls = [o for o in session.occurrences if o.habit == "status_poll"]
    assert len(polls) == 3 and [o.cost for o in polls] == [m.cost for m in session.messages[1:4]]
    assert all(o.cost > 0 for o in polls)


def _message(**fields) -> prompting.Message:
    return prompting.Message(at=datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc), chars=40, **fields)


def test_a_poll_a_go_ahead_or_a_thank_you_never_counts_as_a_repeat_or_a_vague_fix():
    for kind in ({"ack": True}, {"go": True}):
        messages = [_message(edited=True, cost=2.0), _message(repeat=True, vague=True, **kind)]
        assert prompting.occurrences(messages, []) == [], kind
    # A poll counts as a poll, with its own reply's cost, and as neither of the others.
    poll = [_message(edited=True, cost=2.0), _message(repeat=True, vague=True, status=True, cost=1.25)]
    assert [(o.habit, o.cost) for o in prompting.occurrences(poll, [])] == [("status_poll", 1.25)]
    plain = [_message(edited=True, cost=2.0), _message(repeat=True)]
    assert [(o.habit, o.cost) for o in prompting.occurrences(plain, [])] == [("repeat_ask", 2.0)]


def test_a_status_poll_row_says_what_it_cost_and_what_to_try_instead(tmp_path):
    session = _session(tmp_path, [
        *_START,
        _said("how is it going?", 2), _reply(3, ctx=200_000),
        _said("any updates?", 4), _reply(5, ctx=200_000),
    ])
    row = next(r for r in prompting.build_section([session]).tables[0].rows if r[0] == "status_poll")
    assert row[1] == 2 and row[3] > 0 and row[4] == prompting.BASIS["status_poll"] and row[7] == prompting.TRY["status_poll"]
    assert "task panel" in row[7] and "/tasks" in row[7]
    # It is a habit a live hint speaks up for, so it counts in the before-and-after comparison.
    assert "status_poll" in prompting.COACHED_HABITS and prompting.HABITS.index("status_poll") < prompting.HABITS.index("stop_loop")


def test_a_vague_correction_has_no_cost_and_skips_a_retry_after_a_failed_reply():
    found = prompting.occurrences([_message(), _message(vague=True)], [])
    assert [(o.habit, o.cost) for o in found] == [("vague_fix", None)]
    retry = prompting.occurrences([_message(), _message(vague=True, after_failed=True)], [])
    assert retry == []
    assert prompting.BASIS["vague_fix"] == ""


def test_a_retry_after_a_failed_reply_is_not_a_vague_correction(tmp_path):
    failed = turn_line(
        timestamp=_at(3), content=[{"type": "text", "text": "API Error: 529 Overloaded"}], model="<synthetic>",
        isApiErrorMessage=True,
    )
    session = _session(tmp_path, [*_START, _said("add a dark mode toggle", 2), failed, _said("try again", 4),
                                  _reply(5, edit=True)])
    # The message the failed reply answered has no priced reply, so the retry is the second message.
    assert len(session.messages) == 2
    assert session.messages[1].vague and session.messages[1].after_failed
    assert "vague_fix" not in session.counts()
    ordinary = _session(tmp_path, [*_START, _said("add a dark mode toggle", 2), _reply(3, edit=True),
                                   _said("it's still broken", 4), _reply(5, say="What do you see?")], "b.jsonl")
    assert ordinary.counts()["vague_fix"] == 1 and not ordinary.messages[2].after_failed


def test_a_big_task_in_plan_mode_or_after_an_approved_plan_is_fine(tmp_path):
    big_task = (
        "Add a login page with email and password, a settings page where people change their name, email alerts "
        "when a report is ready, and an admin screen that lists every account."
    )
    in_plan = _session(tmp_path, [*_START, _said(big_task, 2, permissionMode="plan"), _reply(3)], "a.jsonl")
    after_plan = _session(tmp_path, [
        _said("Plan the settings page", 0), _reply(1, plan=True), _said(big_task, 2), _reply(3, edit=True),
    ], "b.jsonl")
    assert "plan_first" not in in_plan.counts() and "plan_first" not in after_plan.counts()


def test_stopping_claude_three_times_in_twenty_minutes_is_one_loop(tmp_path):
    session = _session(tmp_path, [
        *_START,
        _said("refactor the store", 2), _reply(3, edit=True), _stop(4),
        _said("keep the API", 5), _reply(6, edit=True), _stop(7),
        _said("use the cache", 8), _reply(9, edit=True), _stop(10),
        _said("rename it for now", 11), _reply(12, edit=True),
    ])
    loops = [o for o in session.occurrences if o.habit == "stop_loop"]
    assert len(loops) == 1 and loops[0].cost > 0


_SENT_BACK = (
    "The user doesn't want to proceed with this tool use. The tool use was rejected (eg. if it was a file edit, "
    "the new_string was NOT written to the file). To tell you how to proceed, the user said:\n"
)


def _tool_stop(minute: float) -> dict:
    return user_str_line("[Request interrupted by user for tool use]", timestamp=_at(minute))


def _turned_away(minute: float, tool_use_id: str, text: str, kind: str) -> dict:
    return user_block_line([tool_result_block(tool_use_id, text, is_error=True)], timestamp=_at(minute),
                           toolDenialKind=kind)


def _stopped_after(tmp_path, call: dict, answer, name: str) -> prompting.SessionPrompting:
    """Three messages, each answered by a call turned away (``answer``
    says how, given the minute and tool id) and then the tool-use
    interrupt line, all within twenty minutes."""
    lines = [*_START]
    for at in (2, 5, 8):
        lines += [
            _said(f"refactor the store, part {at}", at),
            turn_line(timestamp=_at(at + 1), content=[call["block"](at + 1)], cache_read_input_tokens=40_000,
                      output_tokens=200),
            answer(at + 1.5, call["id"](at + 1)),
            _tool_stop(at + 2),
        ]
    lines += [_said("rename it for now", 11), _reply(12, edit=True)]
    return _session(tmp_path, lines, name)


_PLAN_CALL = {"block": lambda m: tool_use_block("ExitPlanMode", f"toolu_p{m}", {"plan": "1. do it"}),
              "id": lambda m: f"toolu_p{m}"}
_EDIT_CALL = {"block": lambda m: tool_use_block("Edit", f"toolu_e{m}"), "id": lambda m: f"toolu_e{m}"}
_BASH_CALL = {"block": lambda m: tool_use_block("Bash", f"toolu_b{m}", {"command": "make"}),
              "id": lambda m: f"toolu_b{m}"}


def test_the_tool_use_line_after_a_plan_you_sent_back_is_not_a_stop(tmp_path):
    session = _stopped_after(
        tmp_path, _PLAN_CALL, lambda at, tool_id: _turned_away(at, tool_id, _SENT_BACK + "smaller", "user-rejected"),
        "plan.jsonl",
    )
    assert "stop_loop" not in session.counts()


def test_the_tool_use_line_after_a_declined_question_or_a_hook_block_is_not_a_stop(tmp_path):
    asked = {"block": lambda m: tool_use_block("AskUserQuestion", f"toolu_q{m}", {"questions": []}),
             "id": lambda m: f"toolu_q{m}"}
    question = _stopped_after(
        tmp_path, asked, lambda at, tool_id: _turned_away(at, tool_id, _SENT_BACK, "user-rejected"), "question.jsonl"
    )
    hook = _stopped_after(
        tmp_path, _BASH_CALL,
        lambda at, tool_id: _turned_away(at, tool_id, "PreToolUse:Bash hook error: [guard.sh] STOP: no make",
                                         "permission-rule"),
        "hook.jsonl",
    )
    assert "stop_loop" not in question.counts() and "stop_loop" not in hook.counts()


def test_the_tool_use_line_after_a_call_you_turned_down_is_not_a_bare_stop(tmp_path):
    """Only Esc on a reply is counted: turning down a call is a decision
    about that call, so three of them are no loop. The waste and quality
    sections still read that line as a stop (``events.is_stop``)."""
    session = _stopped_after(
        tmp_path, _EDIT_CALL,
        lambda at, tool_id: _turned_away(at, tool_id, "Permission to use Edit has been denied.", "permission-rule"),
        "refused.jsonl",
    )
    assert "stop_loop" not in session.counts()
    result = _parse(tmp_path, [
        *_START, _said("refactor the store", 2),
        turn_line(timestamp=_at(3), content=[_EDIT_CALL["block"](3)], cache_read_input_tokens=40_000, output_tokens=200),
        _turned_away(3.5, _EDIT_CALL["id"](3), "Permission to use Edit has been denied.", "permission-rule"),
        _tool_stop(4),
    ], "events.jsonl")
    interrupt = next(e for e in result.events if e.kind.name == "INTERRUPT")
    assert events_mod.is_stop(interrupt) and not events_mod.is_bare_stop(interrupt)


def test_only_esc_on_a_reply_is_a_bare_stop(tmp_path):
    result = _parse(tmp_path, [*_START, _said("refactor the store", 2), _reply(3, edit=True), _stop(4)])
    interrupt = next(e for e in result.events if e.kind.name == "INTERRUPT")
    assert events_mod.is_bare_stop(interrupt)
    ended = {**_stop(5), "message": {"role": "user", "content": "[Request interrupted by user: session shut down]"}}
    result = _parse(tmp_path, [*_START, _said("refactor the store", 2), _reply(3, edit=True), ended], "ended.jsonl")
    assert not any(events_mod.is_bare_stop(e) for e in result.events)


def _day(hour: int, minute: int = 0) -> str:
    return f"2026-09-18T{hour:02d}:{minute:02d}:00.000Z"


def _capture_note() -> dict:
    """The note the capture hook injects, naming the tags it asks for; a
    transcript that never saw one has every tag it carries dropped."""
    text = cat.note_text(cat.level_metrics("deep"), "main")
    wrapped = f"<system-reminder>\nSessionStart hook additional context: {text}\n</system-reminder>"
    return attachment_line(
        "hook_additional_context", rendered=wrapped, content=[text], hookName="SessionStart",
        hookEvent="SessionStart", toolUseID="SessionStart",
    )


def _later(text: str, hour: int, *, ctx: int, say: str = "Done.") -> list[dict]:
    """A message at ``hour`` o'clock and a reply to it, in a context of ``ctx``."""
    return [
        user_str_line(text, timestamp=_day(hour), origin={"kind": "human"}),
        turn_line(timestamp=_day(hour, 1), content=[{"type": "text", "text": say}], cache_read_input_tokens=ctx,
                  output_tokens=200),
    ]


def test_a_new_piece_that_began_with_a_lot_of_earlier_work_in_context_is_counted(tmp_path):
    """Replaces the live /clear note: counted when the reply's tag says the
    message is a new task, with what its replies paid to read the old work again."""
    session = _session(tmp_path, [
        _capture_note(), *_START,
        *_later("Now something else: add a changelog page", 14, ctx=90_000, say="Done.\n\n[cg: task=feature shift=new]"),
    ])
    carried = [o for o in session.occurrences if o.habit == "context_carried"]
    assert len(carried) == 1 and carried[0].cost > 0
    message = session.messages[1]
    assert message.new_piece == "tagged" and message.carried >= prompting.CARRIED_MIN_TOKENS
    # Only the message that began the piece, not the work before it.
    assert not session.messages[0].new_piece and session.messages[0].carried == 0


def test_a_long_break_starts_a_new_piece_only_when_no_tag_says_the_work_went_on(tmp_path):
    untagged = _session(tmp_path, [*_START, *_later("add a changelog page", 14, ctx=90_000)], "a.jsonl")
    assert untagged.messages[1].new_piece == "break" and untagged.counts()["context_carried"] == 1
    built_on = _session(tmp_path, [
        _capture_note(), *_START, *_later("add a changelog page", 14, ctx=90_000, say="Done.\n\n[cg: task=feature shift=build]"),
    ], "b.jsonl")
    assert built_on.messages[1].new_piece == "" and "context_carried" not in built_on.counts()
    # No break, no tag: the same work going on.
    soon = _session(tmp_path, [*_START, _said("add a changelog page", 5), _reply(6, edit=True, ctx=90_000)], "c.jsonl")
    assert soon.messages[1].new_piece == "" and "context_carried" not in soon.counts()


def test_little_earlier_context_is_not_counted_as_carried(tmp_path):
    session = _session(tmp_path, [
        _capture_note(), *_START, *_later("add a changelog page", 14, ctx=50_000, say="Done.\n\n[cg: task=feature shift=new]"),
    ])
    assert session.messages[1].new_piece == "tagged" and session.messages[1].carried < prompting.CARRIED_MIN_TOKENS
    assert "context_carried" not in session.counts()


def test_the_carried_context_thresholds_match_the_habits_page():
    assert prompting.CARRIED_MIN_TOKENS == habits.STALE_TOKENS
    assert prompting.NEW_PIECE_BREAK_S == habits.LONG_BREAK_S


# -- which habits a live hint covers ------------------------------------------------


def test_only_the_habits_a_coaching_note_warns_about_are_judged_against_the_notes(tmp_path):
    assert set(prompting.COACHED_HABITS) <= set(cat.COACHING_HINTS)
    assert set(prompting.HABITS) - set(prompting.COACHED_HABITS) == {
        "repeat_ask", "stop_loop", "vague_fix", "context_carried",
    }
    assert set(prompting.HABITS) >= set(prompting.COACHED_HABITS)
    # No live hint of any retired habit is left in the catalogue.
    assert not set(cat.RETIRED_COACHING_HINTS) & set(cat.COACHING_HINTS)
    assert not set(cat.RETIRED_COACHING_HINTS) & set(cat.COACHING_TEXT)


def test_habit_rates_count_only_the_coached_habits(tmp_path):
    session = _session(tmp_path, [
        *_START,
        _said("make the button bigger", 2), _reply(3, edit=True),
        _said("now move the logo", 4), _reply(5, edit=True),
        _said("and the footer text", 6), _reply(7, edit=True),
        _said("still broken", 8), _reply(9, say="What do you see?"),
    ])
    assert session.counts()["vague_fix"] == 1 and session.counts()["drip_feed"] == 1
    # One drip run (three messages) of the two habits seen: the vague correction doesn't count.
    habits_seen, drip_messages, messages = prompting.habit_rates(session)
    assert (habits_seen, drip_messages, messages) == (1, 3, 5)


# -- the section --------------------------------------------------------------------


def test_the_section_shows_each_habit_seen_with_its_cost_and_what_to_try(tmp_path):
    session = _session(tmp_path, [
        *_START,
        _said("make the button bigger", 2), _reply(3, edit=True),
        _said("now move the logo", 4), _reply(5, edit=True),
        _said("and the footer text", 6), _reply(7, edit=True),
        _said("still broken", 8), _reply(9, say="What do you see?"),
    ])
    section = prompting.build_section([session])
    assert section.key == "prompting"
    table = section.tables[0]
    cols = [c.key for c in table.columns]
    rows = {row[0]: dict(zip(cols, row)) for row in table.rows}
    assert set(rows) == {"drip_feed", "vague_fix"}
    assert rows["drip_feed"]["times"] == 1 and rows["drip_feed"]["per_100"] == 20.0
    assert rows["drip_feed"]["try"] == prompting.TRY["drip_feed"] and rows["drip_feed"]["trend"] == "new"
    # Costliest first.
    assert [row[0] for row in table.rows] == sorted(rows, key=lambda h: -(rows[h]["cost"] or 0))
    assert [t.name for t in section.tables] == ["prompting_habits"]


def test_an_empty_window_still_has_the_section_with_no_rows():
    section = prompting.build_section([])
    assert section.key == "prompting" and section.tables[0].rows == []


def _note_line(kind, minute):
    text = f"{cat.COACH_MARKER}{cat.COACH_VERSION} {kind}\nSome hint text."
    line = attachment_line(
        "hook_additional_context", rendered=f"<system-reminder>\nUserPromptSubmit hook additional context: {text}\n"
        "</system-reminder>", content=[text], hookName="UserPromptSubmit",
    )
    line["timestamp"] = _at(minute)
    return line


_TIP = "> **ClaudeGlass tip:** Plan it."


def test_the_tips_table_counts_the_notes_claude_passed_on(tmp_path):
    session = _session(tmp_path, [
        *_START,
        _said("make the button bigger", 2), _note_line("drip_feed", 2), _reply(3, edit=True, say=f"Done.\n\n{_TIP}"),
        _said("now the logo", 4), _note_line("drip_feed", 4), _reply(5, edit=True),
        _said("next", 6), _note_line("quiet_output", 6), _reply(7),
    ])
    assert session.notes == {"drip_feed": 2} and session.tips == {"drip_feed": 1} and not session.misfires
    tips = prompting.build_section([session]).tables[1]
    assert tips.name == "prompting_tips"
    assert [c.key for c in tips.columns] == ["hint", "notes", "shown", "shown_pct", "relay", "misfires"]
    assert tips.rows == [["drip_feed", 2, 1, 50.0, "relayed 1 of 2", 0]]


def test_the_tips_table_splits_relayed_from_judged_and_counts_the_misfires(tmp_path):
    # drip_feed asks for its tip every time: shown twice in three (one of them
    # called a misfire). big_paste leaves it to Claude's judgement; cold_return asks for its tip every time.
    disowned = f"Done. That ClaudeGlass tip was a false alarm.\n\n{_TIP}"
    session = _session(tmp_path, [
        *_START,
        _said("one", 2), _note_line("drip_feed", 2), _reply(3, edit=True, say=f"Done.\n\n{_TIP}"),
        _said("two", 4), _note_line("drip_feed", 4), _reply(5, edit=True, say=disowned),
        _said("three", 6), _note_line("drip_feed", 6), _reply(7, edit=True),
        _said("four", 8), _note_line("big_paste", 8), _reply(9, say=f"Done.\n\n{_TIP}"),
        _said("five", 10), _note_line("big_paste", 10), _reply(11),
        _said("six", 12), _note_line("cold_return", 12), _reply(13),
    ])
    assert session.misfires == {"drip_feed": 1}
    table = prompting.build_section([session]).tables[1]
    assert table.rows == [
        ["drip_feed", 3, 2, 100.0 * 2 / 3, "relayed 2 of 3", 1],
        ["cold_return", 1, 0, 0.0, "relayed 0 of 1", 0],
        ["big_paste", 2, 1, 50.0, "judged relevant 1 of 2", 0],
    ]
    assert any("Relayed" in note for note in table.notes) and any("Judged relevant" in note for note in table.notes)
    # A reply that disowns a tip with no note behind it is nobody's misfire here.
    plain = _session(tmp_path, [*_START, _said("one", 2), _reply(3, edit=True, say=disowned)], "plain.jsonl")
    assert not plain.misfires and not plain.notes


def test_a_misfire_belongs_to_the_message_the_note_came_with(tmp_path):
    disowned = f"Done. That ClaudeGlass tip was a false alarm.\n\n{_TIP}"
    session = _session(tmp_path, [
        *_START,
        _said("one", 2), _note_line("plan_first", 2), _reply(3, edit=True),
        _said("two", 4), _reply(5, edit=True, say=disowned),
    ])
    assert session.notes == {"plan_first": 1} and not session.tips and not session.misfires


def test_the_plan_mode_tips_name_the_desktop_way_and_the_terminal_key():
    for habit in ("plan_first", "stop_loop"):
        text = prompting.TRY[habit]
        assert "start the message with /plan" in text and "mode menu next to Send" in text, habit
        # The key stays, for the terminal.
        assert "in the terminal, Shift+Tab" in text, habit


def test_the_tips_are_relayed_or_judged_by_whether_the_note_leaves_it_to_claude():
    assert prompting.relay_text("drip_feed", 5, 4) == "relayed 4 of 5"
    assert prompting.relay_text("plan_fresh", 1, 0) == "relayed 0 of 1"
    for hint in cat.CONDITIONAL_TIP_HINTS:
        assert prompting.relay_text(hint, 6, 2) == "judged relevant 2 of 6", hint
    # The hints Claude is told to relay come first, the ones it judges last.
    judged = [hint in cat.CONDITIONAL_TIP_HINTS for hint in prompting.TIP_HINTS]
    assert judged == sorted(judged) and any(judged) and not all(judged)


def test_every_tip_hint_asks_for_the_highlighted_block():
    assert set(prompting.TIP_HINTS) == {
        "plan_fresh", "plan_fresh_early", "drip_feed", "plan_first", "big_paste", "status_poll", "cold_return"}
    # Report-only habits have no note, so they have no tip to count.
    assert set(prompting.HABITS) & set(cat.COACHING_HINTS) == set(prompting.COACHED_HABITS)

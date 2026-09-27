"""How you prompt (``prompting``): what the parser keeps about each message
and reply for it, each habit counted after the fact with the live hints'
rules, what each cost, and the section built from them.
"""

from __future__ import annotations

from types import SimpleNamespace as NS

from claudeglass import capture_catalogue as cat, prompting
from claudeglass.model import TranscriptMeta
from claudeglass.parse import parse_transcript
from claudeglass.pricing import load_pricing

from helpers import attachment_line, tool_use_block, turn_line, user_str_line, write_jsonl

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


def _parse(tmp_path, lines, name="s.jsonl"):
    path = tmp_path / name
    write_jsonl(path, lines)
    return parse_transcript(path, TranscriptMeta(path=str(path)))


def _session(tmp_path, lines, name="s.jsonl"):
    top = _parse(tmp_path, lines, name)
    return prompting.session_prompting(NS(top=top, session_id=name), prompting._Prices(PRICING))


_START = [_said("Build the settings page: " + "a form, a save button and a header. " * 6, 0), _reply(1, edit=True)]


# -- what the parser keeps ----------------------------------------------------------


def test_the_parser_keeps_counts_and_flags_about_each_message_and_reply(tmp_path):
    big = "Add a login page, a settings page, email alerts and an admin screen, and move the DB to Postgres."
    result = _parse(tmp_path, [
        _said(big, 0, permissionMode="default"), _reply(1, say="Which database version do you use?"),
        _said("it's broken", 2), _reply(3, say="Fixed.\n\n> ⚠️ **ClaudeGlass tip:** Say what you saw."),
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


def test_a_message_about_a_plan_has_no_step_count(tmp_path):
    result = _parse(tmp_path, [_said("Carry out the plan: add login, settings, alerts and an admin screen.", 0),
                               _reply(1)])
    assert result.turns[0].prompt_steps == 0


# -- each habit after the fact ------------------------------------------------------


def test_small_requests_answered_with_file_changes_are_one_drip_run(tmp_path):
    session = _session(tmp_path, [
        *_START,
        _said("make the button bigger", 2), _reply(3, edit=True, say="Done. Left or right?"),
        _said("left", 4), _reply(5, edit=True),
        _said("now move the logo", 6), _reply(7, edit=True),
        _said("and the footer text", 8), _reply(9, edit=True),
        _said("thanks", 10), _reply(11),
    ])
    drips = [o for o in session.occurrences if o.habit == "drip_feed"]
    assert len(drips) == 1
    # What the messages after the first paid to take in the context.
    rereads = [m.reread for m in session.messages]
    assert drips[0].cost > 0 and abs(drips[0].cost - (rereads[3] + rereads[4])) < 1e-9


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
    # The attempt that missed, the reply that had to ask, carrying the paste.
    assert by["repeat_ask"].cost == session.messages[1].cost
    assert by["vague_fix"].cost == session.messages[3].cost > 0
    assert by["big_paste"].cost > 0 and by["plan_first"].cost is None


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


def test_the_tips_table_counts_the_notes_claude_passed_on(tmp_path):
    def note(kind, minute):
        text = f"{cat.COACH_MARKER}{cat.COACH_VERSION} {kind}\nSome hint text."
        line = attachment_line(
            "hook_additional_context", rendered=f"<system-reminder>\nUserPromptSubmit hook additional context: {text}\n"
            "</system-reminder>", content=[text], hookName="UserPromptSubmit",
        )
        line["timestamp"] = _at(minute)
        return line

    session = _session(tmp_path, [
        *_START,
        _said("make the button bigger", 2), note("drip_feed", 2), _reply(3, edit=True, say="Done.\n\n> ⚠️ **ClaudeGlass tip:** Plan it."),
        _said("now the logo", 4), note("drip_feed", 4), _reply(5, edit=True),
        _said("next", 6), note("quiet_output", 6), _reply(7),
    ])
    assert session.notes == {"drip_feed": 2} and session.tips == {"drip_feed": 1}
    tips = prompting.build_section([session]).tables[1]
    assert tips.name == "prompting_tips" and tips.rows == [["drip_feed", 2, 1, 50.0]]


def test_every_tip_hint_asks_for_the_highlighted_block():
    assert set(prompting.TIP_HINTS) == {
        "plan_fresh", "repeat_ask", "drip_feed", "stop_loop", "plan_first", "vague_fix", "big_paste", "cache_cold",
        "clear_context",
    }
    assert set(prompting.HABITS) <= set(cat.COACHING_HINTS)

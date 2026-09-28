"""Metrics capture, parser side (PARSER_VERSION 15): the tags Claude writes,
the notes the capture hook injects, and the measures derived from text the
parser already reads (prompt flags, plan counts, slash commands, agent
report sizes). Every value kept is a closed-vocabulary word, a count or a
flag, never text.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claudeglass import cache, capture_catalogue, capture_tags, events
from claudeglass.model import CaptureTag, EventKind, PlanStats, TranscriptMeta
from claudeglass.parse import parse_transcript

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


def _parse(tmp_path: Path, lines: list[dict], **meta):
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    return parse_transcript(path, TranscriptMeta(path=str(path), **meta))


def _reply(text: str, **kw) -> dict:
    return turn_line(content=[{"type": "text", "text": text}], **kw)


def _note(text: str, *, hook: str = "SessionStart", rendered: bool = True, **kw) -> dict:
    wrapped = f"<system-reminder>\n{hook} hook additional context: {text}\n</system-reminder>"
    return attachment_line(
        "hook_additional_context",
        rendered=wrapped if rendered else None,
        content=[text],
        hookName=hook,
        hookEvent=hook.split(":")[0],
        toolUseID=hook,
        **kw,
    )


# -- the [cg: ...] and [result: ...] tags --------------------------------------


def test_a_tag_ending_the_reply_is_read_into_closed_words():
    tag, result = capture_tags.parse_reply_tags(
        "Fixed it.\n\n[cg: task=bugfix brief=partial level=normal shift=build missing=files,done]"
    )
    assert result is None
    assert (tag.task, tag.brief, tag.level, tag.shift, tag.missing) == (
        "bugfix", "partial", "normal", "build", ("files", "done")
    )
    assert tag.has_tl and tag.chars == len("[cg: task=bugfix brief=partial level=normal shift=build missing=files,done]")


@pytest.mark.parametrize("word", ["new", "build", "grew", "redo", "fix"])
def test_every_shift_word_is_kept_including_fix_for_a_fault_in_earlier_work(word):
    tag, _ = capture_tags.parse_reply_tags(f"Done.\n\n[cg: task=bugfix shift={word}]")
    assert (tag.task, tag.shift) == ("bugfix", word)


@pytest.mark.parametrize("text", [
    "Write [cg: task=bugfix] at the end of each reply, like that. Then carry on.",  # quoted mid-reply
    "[cg: task=bugfix]\nand then more text after it",  # not the last thing
    "no tag at all",
    "",
])
def test_a_tag_that_is_not_the_end_of_the_reply_is_ignored(text):
    assert capture_tags.parse_reply_tags(text) == (None, None)


def test_unknown_keys_words_and_paths_are_dropped():
    tag, _ = capture_tags.parse_reply_tags(
        "ok [cg: task=C:\\Users\\me\\secret.py brief=clear secret=hunter2 level=impossible missing=files,/etc/passwd]"
    )
    assert tag == CaptureTag(brief="clear", missing=("files",), has_tl=True, chars=tag.chars)
    assert "secret" not in repr(tag) and "Users" not in repr(tag)


def test_a_result_tag_carries_the_subagent_extras_and_can_share_a_line_with_a_tl_tag():
    tag, result = capture_tags.parse_reply_tags(
        "Done.\n`[result: done fit=smaller rules=unused brief=vague missing=goal]` [cg: task=research]"
    )
    assert result == "done"
    assert (tag.fit, tag.rules, tag.brief, tag.missing, tag.task) == ("smaller", "unused", "vague", ("goal",), "research")


def test_a_bare_result_tag_is_still_the_result_marker():
    tag, result = capture_tags.parse_reply_tags("All done.\n\n**[result: partial]**\n")
    assert result == "partial"
    assert tag is not None and not tag.has_tl


def test_the_feedback_reminder_line_does_not_hide_a_tag_on_either_side():
    # CAP-1: the note now asks for the reminder line before the tag, but
    # the parser tolerates either order.
    reminder = capture_catalogue.FEEDBACK_REMINDER_LINE
    tag, _ = capture_tags.parse_reply_tags(f"Fixed it.\n\n[cg: task=bugfix]\n{reminder}")
    assert tag is not None and tag.task == "bugfix"  # tag then reminder
    tag, _ = capture_tags.parse_reply_tags(f"Fixed it.\n\n{reminder}\n[cg: task=bugfix]")
    assert tag is not None and tag.task == "bugfix"  # reminder then tag
    # As a quote block with its label, as the note now asks.
    labelled = f"{capture_catalogue.REMINDER_LABEL} {reminder}"
    tag, _ = capture_tags.parse_reply_tags(f"Fixed it.\n\n[cg: task=bugfix]\n\n{labelled}")
    assert tag is not None and tag.task == "bugfix"
    tag, _ = capture_tags.parse_reply_tags(f"Fixed it.\n\n{labelled}\n[cg: task=bugfix]")
    assert tag is not None and tag.task == "bugfix"


def test_a_would_help_skill_keeps_its_name_only_when_the_transcript_knows_it():
    tag, _ = capture_tags.parse_reply_tags("x [cg: skill=would-help:grill-me]", {"grill-me"})
    assert (tag.skill, tag.skill_name) == ("would-help", "grill-me")
    tag, _ = capture_tags.parse_reply_tags("x [cg: skill=would-help:private-thing]", {"grill-me"})
    assert (tag.skill, tag.skill_name) == ("would-help", None)
    tag, _ = capture_tags.parse_reply_tags("x [cg: skill=helped:grill-me]", {"grill-me"})
    assert tag.skill is None


@pytest.mark.parametrize("text, expected", [
    ("[spawn: isolate] [retry: scope] Do X", ("scope", "isolate")),
    ("`[retry: brief]` Do X", ("brief", None)),
    ("[Spawn: PARALLEL] search the repo", (None, "parallel")),
    ("Do [retry: brief] X", (None, None)),  # not at the start
    ("[spawn: because] X", (None, None)),  # not a known reason
])
def test_brief_markers_at_the_start(text, expected):
    assert capture_tags.parse_brief_markers(text) == expected


def test_every_vocabulary_word_is_short_and_plain():
    for key, words in capture_catalogue.TAG_VOCAB.items():
        assert key.isidentifier()
        for word in words:
            assert len(word) <= 16 and all(c.isalnum() or c == "-" for c in word), (key, word)


# -- tags reach the turn --------------------------------------------------------


def test_the_reply_tag_and_brief_markers_land_on_the_turns(tmp_path):
    result = _parse(tmp_path, [
        _note("ClaudeGlass metrics capture (cg-cap v1 task,level,skill): ..."),
        attachment_line("skill_listing", rendered="- grill-me: x", names=["grill-me"]),
        user_str_line("[spawn: specialist] [retry: scope] fix the flaky test in tests/test_x.py", origin={"kind": "human"}),
        _reply("Fixed.\n[cg: task=test level=hard skill=would-help:grill-me]"),
    ])
    turn = result.turns[0]
    assert (turn.spawn_marker, turn.retry_marker) == ("specialist", "scope")
    assert (turn.cap.task, turn.cap.level, turn.cap.skill_name) == ("test", "hard", "grill-me")
    assert "path" in turn.prompt_flags
    assert_privacy(result)


def test_a_malformed_skill_name_never_reaches_skills_invoked(tmp_path):
    # SEC-P3: SKILL_NAME_PATTERN gates a Skill tool_use's own "skill"
    # input before it's ever trusted as a real invocation.
    result = _parse(tmp_path, [
        turn_line(content=[tool_use_block("Skill", "tu1", {"skill": "not a skill name!"})]),
    ])
    assert result.turns[0].skills_invoked == ()


def test_a_skill_call_that_errors_cannot_self_authorise_a_later_tag(tmp_path):
    # SEC-P3: a Skill call becomes provisional evidence (skill_names)
    # for a *later* reply's `[cg: skill=would-help:...]` claim -- but
    # only while it stands. One that comes back with is_error: true is
    # taken back before it ever reaches Turn.skills_invoked, so it can't
    # self-authorise the very claim about the skill it tried and failed.
    result = _parse(tmp_path, [
        _note("ClaudeGlass metrics capture (cg-cap v1 skill): ..."),
        turn_line(content=[
            tool_use_block("Skill", "tu1", {"skill": "grill-me"}),
        ]),
        user_block_line([tool_result_block("tu1", "denied", is_error=True)]),
        _reply("Never mind.\n[cg: skill=would-help:grill-me]"),
    ])
    assert result.turns[0].skills_invoked == ()
    reply = result.turns[-1]
    assert reply.cap is not None and reply.cap.skill == "would-help"
    assert reply.cap.skill_name is None


def test_a_skill_call_that_succeeds_can_authorise_a_later_tag(tmp_path):
    # Positive control for the previous test: a genuinely successful
    # call is real evidence, and still validates a later claim about it.
    result = _parse(tmp_path, [
        _note("ClaudeGlass metrics capture (cg-cap v1 skill): ..."),
        turn_line(content=[
            tool_use_block("Skill", "tu1", {"skill": "grill-me"}),
        ]),
        user_block_line([tool_result_block("tu1", "done")]),
        _reply("Never mind.\n[cg: skill=would-help:grill-me]"),
    ])
    assert result.turns[0].skills_invoked == ("grill-me",)
    reply = result.turns[-1]
    assert reply.cap is not None and reply.cap.skill_name == "grill-me"


def test_the_last_text_block_decides(tmp_path):
    result = _parse(tmp_path, [
        user_str_line("go", origin={"kind": "human"}),
        turn_line(content=[
            {"type": "text", "text": "[cg: task=docs]"},
            {"type": "text", "text": "Actually, one more thing."},
        ]),
    ])
    assert result.turns[0].cap is None


# -- capture notes -------------------------------------------------------------


def test_a_capture_note_is_its_own_hook_output_sized_from_rendered(tmp_path):
    text = "ClaudeGlass metrics capture (cg-cap v1 task,brief,level): end each final reply with one line ..."
    line = _note(text)
    event = events.classify_line(line)
    assert (event.kind, event.subkind) == (EventKind.HOOK_OUTPUT, "capture_note")
    assert event.size_chars == len(line["rendered"][0]["content"])
    assert event.detail == {"v": 1, "codes": ["task", "brief", "level"], "hook": "SessionStart"}


def test_sessions_from_before_0_12_1_read_the_same():
    # Until 0.12.1 every tag and marker began tl (claude-token-lens), not cg.
    old, _ = capture_tags.parse_reply_tags("Done.\n\n[tl: task=bugfix brief=clear]")
    new, _ = capture_tags.parse_reply_tags("Done.\n\n[cg: task=bugfix brief=clear]")
    assert old == new and old.task == "bugfix"
    assert capture_tags.parse_note_codes("(tl-cap v1 task,brief)") == (1, ("task", "brief"))
    feedback = capture_tags.parse_feedback_tag("Thanks.\n[tl-fb: outcome=met worth=yes]\n")
    assert feedback is not None and feedback == capture_tags.parse_feedback_tag("[cg-fb: outcome=met worth=yes]")
    note = events.classify_line(_note("ClaudeGlass metrics capture (tl-cap v1 task): ..."))
    assert (note.subkind, note.detail["codes"]) == ("capture_note", ["task"])
    coach = events.classify_line(_note("tl-coach v1 quiet_output\nThat result was large."))
    assert (coach.subkind, coach.detail["kind"]) == ("coaching_note", "quiet_output")


def test_a_capture_note_without_rendered_adds_the_wrapper_it_is_shown_in():
    text = "ClaudeGlass metrics capture (cg-cap v1 task): ..."
    with_rendered = events.classify_line(_note(text, hook="SubagentStart"))
    without = events.classify_line(_note(text, hook="SubagentStart", rendered=False))
    assert without.size_chars == with_rendered.size_chars
    assert without.detail["hook"] == "SubagentStart"


def test_a_pre_rendered_capture_note_still_takes_the_fallback_path(tmp_path):
    """SURV-10: a hook_additional_context line from a Claude Code version
    that doesn't yet send ``rendered`` (real example: 2.1.258) carries no
    ``rendered`` field at all -- not ``null``, simply absent -- and must
    still take the fallback path (``_rendered_size_chars`` ->
    ``_attachment_content_chars``), not silently come back sized ``None``.

    Pinned against real numbers, not just internal consistency: the
    essentials level's SessionStart note is exactly 670 characters, and
    the wrapper Claude Code puts around a hook's additional context
    (``_HOOK_CONTEXT_WRAPPER_CHARS``, 63) plus ``len("SessionStart")``
    (12) is exactly 75, for 745 total.
    """
    text = capture_catalogue.note_text(capture_catalogue.level_metrics("essentials"), "main")
    assert len(text) == 670
    line = _note(text, hook="SessionStart", rendered=False)
    assert "rendered" not in line
    event = events.classify_line(line)
    assert (event.kind, event.subkind) == (EventKind.HOOK_OUTPUT, "capture_note")
    assert event.size_chars == 745


def test_other_hook_context_is_unchanged():
    event = events.classify_line(_note("A preview server is running."))
    assert event.subkind == "hook_additional_context"


def test_a_hook_system_message_takes_no_context():
    """It is shown to you in the terminal, never to the model."""
    line = attachment_line("hook_system_message", content="Reminder: update the backlog.", hookName="Stop")
    event = events.classify_line(line)
    assert event.kind == EventKind.HOOK_OUTPUT and event.size_chars is None


def test_note_chars_land_on_the_next_turn_and_the_meta_counts_notes(tmp_path):
    first = _note("ClaudeGlass metrics capture (cg-cap v1 task,brief): ...")
    again = _note("ClaudeGlass metrics capture (cg-cap v1 task,brief,size): ...")
    result = _parse(tmp_path, [
        first,
        user_str_line("hi", origin={"kind": "human"}),
        _reply("hello"),
        again,
        user_str_line("more", origin={"kind": "human"}),
        _reply("sure"),
    ])
    assert result.turns[0].cap_note_chars == len(first["rendered"][0]["content"])
    assert result.turns[1].cap_note_chars == len(again["rendered"][0]["content"])
    meta = result.meta
    assert (meta.cap_version, meta.cap_metrics, meta.cap_injections) == (1, ("task", "brief", "size"), 2)


# -- derived measures -------------------------------------------------------------


@pytest.mark.parametrize("text, flags", [
    ("Fix src/app/models.py", ("path",)),
    ("Update README.md", ("path",)),
    ("```\nprint(1)\n```", ("code",)),
    ("Traceback (most recent call last):\n  File x", ("error",)),
    ("TypeError: cannot read x", ("error",)),
    ("see https://docs.example.com/page.html", ("url",)),
    ("done when the tests pass", ("done",)),
    ("1. read it\n2. fix it", ("steps",)),
    ("report back in under 200 words", ("short",)),
    ("please tidy things up a bit", ()),
])
def test_prompt_flags(text, flags):
    assert events.prompt_flags([text]) == flags


def test_prompt_flags_reach_the_turn_and_never_the_text(tmp_path):
    result = _parse(tmp_path, [
        user_str_line("The build fails with TypeError: x in C:/secret/app.py\n1. find it\n2. fix it",
                      origin={"kind": "human"}),
        _reply("ok"),
    ])
    assert result.turns[0].prompt_flags == ("path", "error", "steps")
    assert "secret" not in repr(result.turns) + repr(result.events)
    assert_privacy(result)


@pytest.mark.parametrize("is_error, outcome", [(False, "approved"), (True, "rejected")])
def test_a_plan_is_counted_and_its_answer_recorded(tmp_path, is_error, outcome):
    plan = "# Plan\n\n1. Edit src/a.py\n2. Edit src/b.py and src/a.py\n3. Run tests/test_a.py\n"
    result = _parse(tmp_path, [
        turn_line(content=[tool_use_block("ExitPlanMode", "tu_p", {"plan": plan})]),
        user_block_line([tool_result_block("tu_p", "no" if is_error else "ok", is_error=is_error)]),
        _reply("next"),
    ])
    assert result.turns[0].plan_stats == PlanStats(steps=3, files=3, chars=len(plan), outcome=outcome)


def test_slash_commands_you_ran_are_named_on_the_next_turn(tmp_path):
    result = _parse(tmp_path, [
        user_str_line("<command-name>/grill-me</command-name>\n<command-message>grill-me</command-message>\n"
                      "<command-args>C:/secret/plan.md</command-args>"),
        _reply("ok"),
    ])
    assert result.turns[0].commands_run == ("grill-me",)
    assert "secret" not in repr(result.events)


def test_a_skill_you_ran_is_named_and_is_your_message(tmp_path):
    # Claude Code writes a skill run with a slash <command-message> first,
    # then the skill's body as a meta line; a local command like /compact
    # is written <command-name> first.
    result = _parse(tmp_path, [
        user_str_line("<command-message>grill-me</command-message>\n<command-name>/grill-me</command-name>\n"
                      "<command-args>C:/secret/plan.md</command-args>"),
        user_block_line([{"type": "text", "text": "Base directory for this skill: C:/secret"}], isMeta=True),
        _reply("ok"),
    ])
    assert result.turns[0].commands_run == ("grill-me",)
    assert result.turns[0].human_prompt_chars is not None
    assert "secret" not in repr(result.events)


def test_a_synchronous_agent_report_is_sized_but_a_background_launch_is_not(tmp_path):
    result = _parse(tmp_path, [
        turn_line(content=[
            tool_use_block("Agent", "tu_sync", {"prompt": "look"}),
            tool_use_block("Agent", "tu_bg", {"prompt": "look", "run_in_background": True}),
        ]),
        user_block_line([tool_result_block("tu_sync", "r" * 400)],
                        toolUseResult={"agentId": "a1", "status": "completed"}),
        user_block_line([tool_result_block("tu_bg", "Async agent launched successfully.")],
                        toolUseResult={"agentId": "a2", "status": "async_launched", "isAsync": True}),
        _reply("waiting"),
    ])
    assert result.turns[0].agent_result_chars == {"tu_sync": 400}


def test_a_task_notification_is_sized(tmp_path):
    text = "<task-notification><task-id>a2</task-id><status>completed</status><result>" + "r" * 300 + "</result></task-notification>"
    event = events.classify_line(user_str_line(text, origin={"kind": "task-notification"}))
    assert event.kind == EventKind.TASK_NOTIFICATION
    assert event.size_chars == len(text) and event.detail["task_id"] == "a2"


# -- the digest cache keeps them --------------------------------------------------


def test_the_new_fields_survive_the_digest_cache(tmp_path):
    result = _parse(tmp_path, [
        _note("ClaudeGlass metrics capture (cg-cap v1 task,missing): ..."),
        user_str_line("[spawn: isolate] do it in src/a.py", origin={"kind": "human"}),
        turn_line(content=[tool_use_block("ExitPlanMode", "tu_p", {"plan": "1. a\n2. b"})]),
        user_block_line([tool_result_block("tu_p", "ok")]),
        _reply("Done.\n[result: done fit=right missing=files,goal] [cg: task=feature]"),
    ])
    decoded = cache.result_from_jsonable(json.loads(json.dumps(cache.encode_result(result))))
    assert decoded.turns == result.turns
    assert decoded.meta == result.meta
    assert isinstance(decoded.turns[-1].cap, CaptureTag)
    assert decoded.turns[-1].cap.missing == ("files", "goal")
    assert isinstance(decoded.turns[0].plan_stats, PlanStats)


# -- SEC-P2 scope: keys the other scope is asked for ---------------------------------------


def test_a_subagent_s_main_session_tag_is_not_trusted():
    # Seen live: an Explore agent asked for result,retry,fit wrote a full
    # [cg: ...] tag beside its [result: ...].
    cap, marker = capture_tags.parse_reply_tags(
        "Done.\n\n[cg: task=research brief=clear level=normal size=l missing=none plan=none "
        "skill=unneeded found=yes prior=needed check=none] [result: done fit=right out=part]"
    )
    kept, marker = capture_tags.filter_tag(cap, marker, requested={"result", "retry", "fit"}, subagent=True)
    assert marker == "done" and kept.fit == "right" and kept.has_tl is False
    for name in ("task", "level", "size", "plan", "skill", "found", "prior", "check", "brief", "out"):
        assert getattr(kept, name) is None, name
    assert kept.missing == ()


def test_a_subagent_keeps_out_when_a_large_output_note_asked_for_it():
    cap, marker = capture_tags.parse_reply_tags("Done. [result: done fit=right out=part]")
    kept, _ = capture_tags.filter_tag(cap, marker, requested={"result", "fit", "big_output"}, subagent=True)
    assert kept.out == "part" and kept.fit == "right"


def test_a_main_session_tag_drops_subagent_only_keys():
    cap, marker = capture_tags.parse_reply_tags("Fixed. [cg: task=bugfix fit=right rules=used]")
    kept, _ = capture_tags.filter_tag(cap, marker, requested={"task"}, subagent=False)
    assert kept.task == "bugfix" and kept.has_tl is True
    assert kept.fit is None and kept.rules is None

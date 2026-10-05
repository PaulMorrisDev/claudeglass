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

from claudeglass import cache, capture, capture_catalogue, capture_tags, events, prompt_shape
from claudeglass.model import CaptureTag, EventKind, PlanCheck, PlanStats, TranscriptMeta
from claudeglass.parse import parse_transcript

from helpers import (
    assert_privacy,
    attachment_line,
    queue_operation_line,
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


def test_why_and_admit_parse_with_their_own_words_and_unknown_words_are_dropped():
    tag, _ = capture_tags.parse_reply_tags(
        "Redone.\n\n[cg: task=bugfix shift=redo why=left_out admit=instruction]"
    )
    assert (tag.shift, tag.why, tag.admit) == ("redo", "left_out", "instruction")
    for why in ("changed", "missed", "tools"):
        assert capture_tags.parse_reply_tags(f"Fixed.\n[cg: shift=fix why={why}]")[0].why == why
    for admit in ("claim", "change"):
        assert capture_tags.parse_reply_tags(f"Fixed.\n[cg: shift=fix admit={admit}]")[0].admit == admit
    tag, _ = capture_tags.parse_reply_tags("Fixed.\n[cg: shift=fix why=because admit=maybe]")
    assert (tag.shift, tag.why, tag.admit) == ("fix", None, None)
    assert "because" not in repr(tag)


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
            assert len(word) <= 16 and all(c.isalnum() or c in "-_" for c in word), (key, word)


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
    essentials level's SessionStart note is exactly 1665 characters, and
    the wrapper Claude Code puts around a hook's additional context
    (``_HOOK_CONTEXT_WRAPPER_CHARS``, 63) plus ``len("SessionStart")``
    (12) is exactly 75, for 1740 total.
    """
    text = capture_catalogue.note_text(capture_catalogue.level_metrics("essentials"), "main")
    assert len(text) == 1665
    line = _note(text, hook="SessionStart", rendered=False)
    assert "rendered" not in line
    event = events.classify_line(line)
    assert (event.kind, event.subkind) == (EventKind.HOOK_OUTPUT, "capture_note")
    assert event.size_chars == 1740


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
    assert result.turns[0].plan_stats == PlanStats(
        steps=3, files=3, chars=len(plan), outcome=outcome, rejected=is_error
    )


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


def test_why_and_admit_are_kept_only_where_the_shift_switch_is_on():
    cap, marker = capture_tags.parse_reply_tags("Redone.\n\n[cg: task=bugfix shift=redo why=missed admit=claim]")
    kept, _ = capture_tags.filter_tag(cap, marker, requested={"task", "shift"}, subagent=False)
    assert (kept.shift, kept.why, kept.admit) == ("redo", "missed", "claim")
    # A custom config without shift never asked for any of the three.
    kept, _ = capture_tags.filter_tag(cap, marker, requested={"task"}, subagent=False)
    assert kept.task == "bugfix"
    assert (kept.shift, kept.why, kept.admit) == (None, None, None)
    # A subagent's own [cg: ...] is never trusted, why and admit included.
    kept, _ = capture_tags.filter_tag(cap, marker, requested={"result", "shift"}, subagent=True)
    assert kept is None


def test_older_found_and_detour_words_are_still_kept_where_their_note_asked():
    """Notes written before found, fit and detour were retired still name them,
    so a transcript of that time keeps them: the switch is the note's code."""
    cap, marker = capture_tags.parse_reply_tags("Found.\n\n[cg: task=research found=partial detour=reread]")
    kept, _ = capture_tags.filter_tag(cap, marker, requested={"task", "found", "detour"}, subagent=False)
    assert (kept.found, kept.detour) == ("partial", "reread")
    kept, _ = capture_tags.filter_tag(cap, marker, requested={"task"}, subagent=False)
    assert (kept.found, kept.detour) == (None, None)


def test_why_and_admit_land_on_the_turn_and_survive_the_digest_cache(tmp_path):
    result = _parse(tmp_path, [
        _note("ClaudeGlass metrics capture (cg-cap v1 task,shift): ..."),
        user_str_line("that is not what I asked, redo it", origin={"kind": "human"}),
        _reply("Redone.\n[cg: task=bugfix shift=redo why=left_out admit=claim]"),
    ])
    cap = result.turns[0].cap
    assert (cap.shift, cap.why, cap.admit) == ("redo", "left_out", "claim")
    decoded = cache.result_from_jsonable(json.loads(json.dumps(cache.encode_result(result))))
    assert decoded.turns == result.turns
    assert (decoded.turns[0].cap.why, decoded.turns[0].cap.admit) == ("left_out", "claim")
    assert_privacy(result)


# -- messages typed while Claude works (PARSER_VERSION 37) ---------------------------


def _at(seconds: int) -> str:
    """A timestamp ``seconds`` after 12:00:00 on the fixture day."""
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"2026-09-18T{12 + hours:02d}:{minutes:02d}:{secs:02d}.000Z"


def _typed(text: str, at: int, **kw) -> dict:
    return user_str_line(text, origin={"kind": "human"}, timestamp=_at(at), **kw)


def _queued(prompt, at: int, **kw) -> dict:
    """A message you typed while Claude was working, as its attachment is
    written some time later (the line's own time is later than ``at``)."""
    line = attachment_line(
        "queued_command", prompt=prompt, commandMode="prompt", origin={"kind": "human"}, timestamp=_at(at), **kw
    )
    line["timestamp"] = _at(at + 15)
    return line


def _working(at: int) -> dict:
    return turn_line(content=[tool_use_block("Bash", f"tu_{at}", {"command": "ls"})], timestamp=_at(at))


def _worked(at: int, of: int = 5) -> dict:
    return user_block_line([tool_result_block(f"tu_{of}", "ok")], timestamp=_at(at))


def test_a_message_typed_while_claude_works_is_counted_and_opens_no_cycle(tmp_path):
    text = "also rename the helper to build_index"
    result = _parse(tmp_path, [
        _typed("refactor the parser", 0),
        _working(5),
        _queued(text, 10),
        _worked(30),
        _reply("done", timestamp=_at(40)),
    ])
    first, last = result.turns
    assert (first.queued_prompts, first.human_prompt_chars is not None) == (0, True)
    assert last.queued_prompts == 1
    assert last.queued_chars == len(text)
    assert last.queued_adjust is True
    assert last.human_prompt_chars is None
    assert len(capture.prompt_cycles(result)) == 1
    queued = [e for e in result.events if e.subkind == "queued_command"]
    assert [e.kind for e in queued] == [EventKind.QUEUE_OPERATION]
    # Its time is when you typed it.
    assert queued[0].ts == _at(10)


def test_the_queued_messages_of_one_turn_are_added_up(tmp_path):
    result = _parse(tmp_path, [
        _typed("refactor the parser", 0),
        _working(5),
        _queued("continue", 10),
        _queued("how is it going?", 12),
        _queued("1. add a test\n2. add a doc\n3. add a flag", 14),
        _worked(30),
        _reply("done", timestamp=_at(40)),
    ])
    turn = result.turns[-1]
    assert turn.queued_prompts == 3
    assert turn.queued_steps == 3
    assert turn.queued_go and turn.queued_status
    assert not turn.queued_correction and not turn.queued_adjust
    assert turn.queued_chars == len("continue") + len("how is it going?") + len("1. add a test\n2. add a doc\n3. add a flag")


def test_a_queued_correction_is_noted(tmp_path):
    result = _parse(tmp_path, [
        _typed("fix the parser", 0), _working(5), _queued("no, that's wrong, it's broken", 10), _worked(30),
        _reply("done", timestamp=_at(40)),
    ])
    assert result.turns[-1].queued_correction is True


def test_only_a_queued_message_you_typed_is_counted(tmp_path):
    notification = attachment_line(
        "queued_command",
        prompt="<task-notification><task-id>t1</task-id><status>completed</status></task-notification>",
        commandMode="task-notification",
    )
    peer = attachment_line("queued_command", prompt="please review", commandMode="prompt",
                           origin={"kind": "peer"}, isMeta=True)
    coordinator = attachment_line("queued_command", prompt="carry on", isMeta=True)
    result = _parse(tmp_path, [
        _typed("fix the parser", 0), _working(5), notification, peer, coordinator,
        queue_operation_line("enqueue", content="please rename the secret file"), _worked(30),
        _reply("done", timestamp=_at(40)),
    ])
    turn = result.turns[-1]
    assert (turn.queued_prompts, turn.queued_chars, turn.queued_steps) == (0, 0, 0)
    assert not (turn.queued_correction or turn.queued_adjust or turn.queued_go or turn.queued_status)


def test_a_queue_operation_enqueue_is_never_a_message(tmp_path):
    result = _parse(tmp_path, [
        _typed("fix the parser", 0), _working(5),
        queue_operation_line("enqueue", content="also add a test", timestamp=_at(10)),
        queue_operation_line("dequeue", timestamp=_at(11)),
        queue_operation_line("remove", timestamp=_at(12)),
        _worked(30), _reply("done", timestamp=_at(40)),
    ])
    assert result.turns[-1].queued_prompts == 0
    assert "also add a test" not in repr(result.events)


def test_a_replay_block_of_rewritten_old_lines_counts_each_message_once(tmp_path):
    # After a compaction or a resume, Claude Code writes its old lines again
    # with new times and the same uuids.
    queued = _queued("also add a test", 10)
    typed = _typed("refactor the parser", 0)
    work, done = _working(5), _worked(30)
    replay = []
    for line in (typed, work, queued, done):
        copy = json.loads(json.dumps(line))
        copy["timestamp"] = _at(900)
        if "attachment" in copy:
            copy["attachment"]["timestamp"] = _at(900)
        replay.append(copy)
    result = _parse(tmp_path, [typed, work, queued, done, *replay, _reply("done", timestamp=_at(1000))])
    assert result.diagnostics.replayed_lines == 4
    assert result.turns[-1].queued_prompts == 1
    assert len([e for e in result.events if e.kind == EventKind.HUMAN_TEXT]) == 1


def test_a_queued_message_also_written_as_a_user_line_counts_once(tmp_path):
    # You typed it while Claude worked, pressed Esc, and Claude Code put it
    # back: the same words are a queued message and then a user line.
    text = "also add a test for the parser"
    result = _parse(tmp_path, [
        _typed("refactor the parser", 0), _working(5), _queued(text, 10), _worked(20),
        user_str_line("[Request interrupted by user]", timestamp=_at(25)),
        _typed(text, 40), _reply("done", timestamp=_at(50)),
    ])
    turn = result.turns[-1]
    assert turn.queued_prompts == 0
    assert turn.human_prompt_chars == len(text)


def test_a_user_line_written_before_its_queued_copy_counts_once(tmp_path):
    text = "also add a test for the parser"
    result = _parse(tmp_path, [
        _typed("refactor the parser", 0), _working(5), _typed(text, 10), _queued(text, 12), _worked(20),
        _reply("done", timestamp=_at(50)),
    ])
    turn = result.turns[-1]
    assert turn.queued_prompts == 0
    assert turn.human_prompt_chars == len(text)


def test_a_queued_copy_a_minute_later_is_another_message(tmp_path):
    text = "also add a test for the parser"
    result = _parse(tmp_path, [
        _typed("refactor the parser", 0), _working(5), _typed(text, 10), _reply("ok", timestamp=_at(20)),
        _working(30), _queued(text, 200), _worked(230, 30), _reply("done", timestamp=_at(240)),
    ])
    assert result.turns[-1].queued_prompts == 1


def test_a_different_queued_message_is_not_a_copy(tmp_path):
    result = _parse(tmp_path, [
        _typed("refactor the parser", 0), _working(5), _typed("also add a test", 10),
        _queued("also add a doc", 12), _worked(20), _reply("done", timestamp=_at(50)),
    ])
    assert result.turns[-1].queued_prompts == 1


def test_the_typed_message_flags_reach_the_turn(tmp_path):
    result = _parse(tmp_path, [_typed("continue", 0), _reply("ok", timestamp=_at(5))])
    turn = result.turns[0]
    assert turn.human_go is True
    assert (turn.human_status, turn.human_adjust, turn.human_remind) == (False, False, False)
    for text, field in (
        ("how is it going?", "human_status"),
        ("actually, make it blue", "human_adjust"),
        ("I already told you to use tabs", "human_remind"),
    ):
        turn = _parse(tmp_path, [_typed(text, 0), _reply("ok", timestamp=_at(5))]).turns[0]
        assert getattr(turn, field) is True, text
        assert turn.human_go is False, text


def test_a_status_poll_typed_without_an_apostrophe_is_still_a_status():
    for text in ("whats the status", "hows it going?", "what's the status", "how's it going?"):
        assert prompt_shape.is_status(text), text
    assert not prompt_shape.is_status("whatsapp the team")


def test_a_go_or_a_status_is_all_of_what_you_typed_before_a_reply(tmp_path):
    both = _parse(tmp_path, [_typed("continue", 0), _typed("go ahead", 1), _reply("ok", timestamp=_at(5))]).turns[0]
    assert both.human_go is True
    mixed = _parse(tmp_path, [
        _typed("continue", 0), _typed("fix the parser and add a test", 1), _reply("ok", timestamp=_at(5)),
    ]).turns[0]
    assert mixed.human_go is False
    # An adjust or a remind is any of them.
    assert _parse(tmp_path, [
        _typed("fix the parser and add a test", 0), _typed("actually, make it blue", 1),
        _reply("ok", timestamp=_at(5)),
    ]).turns[0].human_adjust is True
    # A turn with no message of yours has none of the flags.
    turn = _parse(tmp_path, [_reply("ok", timestamp=_at(5))]).turns[0]
    assert (turn.human_go, turn.human_status, turn.human_adjust, turn.human_remind) == (False,) * 4


def test_a_line_you_didnt_type_opens_no_cycle(tmp_path):
    result = _parse(tmp_path, [
        _typed("refactor the parser", 0), _reply("ok", timestamp=_at(5)),
        user_str_line("The app was quit while you were working. Carry on.", origin={"kind": "human"},
                      timestamp=_at(900)),
        user_str_line("<cross-session-message from=\"x\">hi</cross-session-message>", timestamp=_at(905)),
        user_str_line("please do the thing", origin={"kind": "human"}, turnOrigin="scheduled", timestamp=_at(910)),
        _reply("ok", timestamp=_at(920)),
    ])
    assert [t.human_prompt_chars is not None for t in result.turns] == [True, False]
    assert len(capture.prompt_cycles(result)) == 1
    # The app-quit note carries on your last message's work (resume); the other two end its reply.
    assert [e.subkind for e in result.events if e.kind == EventKind.META] == ["resume", "not_typed", "not_typed"]
    # Only a line that ends the reply marks the turn it came before.
    assert [t.preceding_not_typed for t in result.turns] == [False, True]
    assert not _parse(tmp_path, [
        _typed("refactor the parser", 0), _reply("ok", timestamp=_at(5)),
        user_str_line("The app was quit while you were working. Carry on.", origin={"kind": "human"},
                      timestamp=_at(900)),
        _reply("ok", timestamp=_at(920)),
    ]).turns[1].preceding_not_typed


# -- output style: a change is a signal, a repeat is not -----------------------------


def test_an_output_style_that_changes_is_a_cache_signal(tmp_path):
    def style(name, at):
        line = attachment_line("output_style", style=name)
        line["timestamp"] = _at(at)
        return line

    result = _parse(tmp_path, [
        style("default", 0), _reply("a", timestamp=_at(1)),
        style("default", 2), _reply("b", timestamp=_at(3)),
        style("concise", 4), _reply("c", timestamp=_at(5)),
        style("concise", 6), _reply("d", timestamp=_at(7)),
        style("default", 8), _reply("e", timestamp=_at(9)),
    ])
    seen = [(e.kind, e.subkind) for e in result.events if e.subkind == "output_style"]
    reminder, signal = (EventKind.REMINDER, "output_style"), (EventKind.CACHE_SIGNAL, "output_style")
    assert seen == [reminder, reminder, signal, reminder, signal]


def test_an_output_style_with_no_name_is_never_a_change(tmp_path):
    lines = [attachment_line("output_style", style="default"), _reply("a"),
             attachment_line("output_style"), _reply("b"), attachment_line("output_style", style=7), _reply("c")]
    result = _parse(tmp_path, lines)
    assert {e.kind for e in result.events if e.subkind == "output_style"} == {EventKind.REMINDER}


# -- images -------------------------------------------------------------------------

_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _png_block() -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": _TINY_PNG_B64}}


def test_an_image_in_a_tool_result_is_sized_and_never_kept(tmp_path):
    result = _parse(tmp_path, [
        _typed("what does the screenshot show", 0),
        turn_line(content=[tool_use_block("Read", "tu_img", {"file_path": "shot.png"})], timestamp=_at(5)),
        user_block_line([tool_result_block("tu_img", [_png_block()])], timestamp=_at(10)),
        _reply("a button", timestamp=_at(15)),
    ])
    assert result.turns[0].tool_result_chars_by_tool == {
        "Read": events.image_token_estimate(1, 1) * events._CHARS_PER_TOKEN_APPROX
    }
    assert _TINY_PNG_B64 not in repr(result.turns) + repr(result.events)
    assert_privacy(result)


def test_a_desktop_image_message_with_no_placeholder_text_is_flagged(tmp_path):
    result = _parse(tmp_path, [
        user_block_line([{"type": "text", "text": "it's broken"}, _png_block()], origin={"kind": "human"},
                        timestamp=_at(0)),
        _reply("looking", timestamp=_at(5)),
    ])
    typed = next(e for e in result.events if e.kind == EventKind.HUMAN_TEXT)
    assert typed.detail["has_image"] is True
    assert "vague" not in typed.detail
    assert result.turns[0].human_vague is False


def test_a_queued_image_message_is_flagged_and_counted(tmp_path):
    result = _parse(tmp_path, [
        _typed("refactor the parser", 0), _working(5),
        _queued([{"type": "text", "text": "it looks wrong"}, _png_block()], 10),
        _worked(30), _reply("done", timestamp=_at(40)),
    ])
    queued = next(e for e in result.events if e.subkind == "queued_command")
    assert queued.detail["has_image"] is True
    assert result.turns[-1].queued_prompts == 1
    assert _TINY_PNG_B64 not in repr(result.events)


# -- the digest cache keeps the new fields ------------------------------------------


def test_the_message_fields_survive_the_digest_cache(tmp_path):
    result = _parse(tmp_path, [
        _typed("actually, make it blue", 0), _working(5),
        _queued("continue", 10), _queued("how is it going?", 12), _queued("it's wrong, I told you", 14),
        _worked(30), _reply("done", timestamp=_at(40)),
    ])
    decoded = cache.result_from_jsonable(json.loads(json.dumps(cache.encode_result(result))))
    assert decoded.turns == result.turns
    assert decoded.turns[0].human_adjust is True
    assert (decoded.turns[-1].queued_prompts, decoded.turns[-1].queued_go, decoded.turns[-1].queued_status) == (
        3, True, True
    )


# -- plan answers and denied calls (PARSER_VERSION 37) -------------------------------

_SENT_BACK = (
    "The user doesn't want to proceed with this tool use. The tool use was rejected (eg. if it was a file edit, "
    "the new_string was NOT written to the file). To tell you how to proceed, the user said:\n"
)
_PLAN = "# Plan\n\n1. Edit src/a.py\n2. Run tests/test_a.py\n"
_ASK = "add a retry to the fetcher"


def _asked(text: str = _ASK, **line) -> dict:
    return user_str_line(text, origin={"kind": "human"}, **line)


def _calls(name: str, *ids: str, **given) -> dict:
    return turn_line(content=[tool_use_block(name, tool_id, given or {"command": "ls"}) for tool_id in ids])


def _plan_call(tool_id: str = "tu_p") -> dict:
    return _calls("ExitPlanMode", tool_id, plan=_PLAN)


def _plan_back(feedback: str = "", tool_id: str = "tu_p", **line) -> dict:
    return user_block_line(
        [tool_result_block(tool_id, _SENT_BACK + feedback, is_error=True)], toolDenialKind="user-rejected", **line
    )


def _denied(tool_id: str, text: str, kind: str | None, **line) -> dict:
    extra = {"toolDenialKind": kind} if kind else {}
    return user_block_line([tool_result_block(tool_id, text, is_error=True)], **extra, **line)


def _events(result, kind: EventKind) -> list:
    return [event for event in result.events if event.kind == kind]


@pytest.mark.parametrize("tool, kind, text, bucket", [
    ("ExitPlanMode", "user-rejected", _SENT_BACK + "use the other approach", "plan_rejected"),
    ("ExitPlanMode", "user-rejected", _SENT_BACK, "plan_rejected"),
    ("AskUserQuestion", "user-rejected", _SENT_BACK, "question_declined"),
    ("Bash", "permission-rule", "PreToolUse:Bash hook error: [guard.sh] STOP: no rm here", "hook_blocked"),
    ("Grep", "saver-redirect", "redirected", "hook_blocked"),
    ("Bash", "automode-blocked", "Blocked by the auto mode classifier", "auto_blocked"),
    ("Bash", "automode-unavailable", "The server-side auto mode classifier gave no verdict", "auto_unavailable"),
    ("Bash", "interrupted", "", "aborted"),
    ("Bash", "cancelled", "cancelled", "aborted"),
    ("Bash", "user-rejected", "The permission request was aborted", "aborted"),
    ("Bash", "permission-rule", "Permission to use Bash has been denied.", "refused"),
    ("Edit", "user-rejected", _SENT_BACK + "not that file", "refused"),
])
def test_every_denial_gets_a_bucket_word_and_the_next_reply_counts_it(tmp_path, tool, kind, text, bucket):
    given = {"plan": _PLAN} if tool == "ExitPlanMode" else None
    result = _parse(tmp_path, [
        _asked(), turn_line(content=[tool_use_block(tool, "tu_1", given or {"command": "ls"})]),
        _denied("tu_1", text, kind), _reply("ok"),
    ])
    [denial] = _events(result, EventKind.TOOL_DENIAL)
    assert denial.detail["bucket"] == bucket
    assert denial.subkind == kind
    assert result.turns[0].preceding_denials == {}
    assert result.turns[1].preceding_denials == {bucket: 1}
    assert "guard.sh" not in repr(result.events) + repr(result.turns)


def test_the_denial_buckets_are_a_closed_set_and_only_one_is_a_call_you_turned_down():
    assert set(events.DENIAL_BUCKETS) == {
        "plan_rejected", "question_declined", "hook_blocked", "auto_blocked", "auto_unavailable", "aborted", "refused",
    }


def test_a_denial_with_no_tool_result_is_still_a_refusal(tmp_path):
    result = _parse(tmp_path, [
        _asked(), _calls("Bash", "tu_1"), user_str_line("(denied)", toolDenialKind="user-rejected"), _reply("ok"),
    ])
    assert result.turns[1].preceding_denials == {"refused": 1}


def test_a_plan_sent_back_with_feedback_is_one_event_of_length_and_class_only(tmp_path):
    feedback = "why not reuse the cache in /secret/vault/notes.txt?"
    result = _parse(tmp_path, [_asked(), _plan_call(), _plan_back(feedback), _reply("replanning")])
    [event] = _events(result, EventKind.PLAN_FEEDBACK)
    assert (event.size_chars, event.subkind, event.detail) == (len(feedback), "question", {})
    assert result.turns[0].plan_stats == PlanStats(
        steps=2, files=2, chars=len(_PLAN), outcome="rejected", rejected=True,
        feedback_chars=len(feedback), feedback_class="question",
    )
    # It never opens a cycle, and it isn't a message you typed.
    assert result.turns[1].human_prompt_chars is None
    assert len(capture.prompt_cycles(result)) == 1
    assert [e.kind for e in result.events].count(EventKind.HUMAN_TEXT) == 1
    # The reply after it follows the dialog's answer, not a denial.
    assert result.turns[1].preceding_primary == EventKind.PLAN_FEEDBACK
    assert result.turns[1].preceding_denials == {"plan_rejected": 1}
    assert "secret" not in repr(result)
    assert_privacy(result)


def test_a_plan_sent_back_with_nothing_typed_has_no_feedback_event(tmp_path):
    result = _parse(tmp_path, [_asked(), _plan_call(), _plan_back(), _reply("replanning")])
    assert _events(result, EventKind.PLAN_FEEDBACK) == []
    plan = result.turns[0].plan_stats
    assert (plan.outcome, plan.rejected, plan.feedback_chars, plan.feedback_class) == ("rejected", True, 0, None)
    assert result.turns[1].preceding_primary == EventKind.TOOL_DENIAL


def test_a_deny_rules_text_after_a_plan_is_its_feedback_but_a_stock_denial_is_not(tmp_path):
    typed = _parse(tmp_path, [
        _asked(), _plan_call(),
        _denied("tu_p", "the second step should come first", "permission-rule"), _reply("ok"),
    ])
    assert typed.turns[0].plan_stats.feedback_chars == len("the second step should come first")
    stock = _parse(tmp_path, [
        _asked(), _plan_call(),
        _denied("tu_p", "Permission to use ExitPlanMode has been denied.", "permission-rule"), _reply("ok"),
    ])
    assert stock.turns[0].plan_stats.feedback_chars == 0
    assert stock.turns[0].plan_stats.outcome == "rejected"


@pytest.mark.parametrize("text, word", [
    ("why not reuse the existing cache?", "question"),
    ("what about the tests", "question"),
    ("I'm not sure about step 2", "unsure"),
    ("hmm, not sure", "unsure"),
    ("that's wrong, the helper lives in src/b.py", "critique"),
    ("make step two a bit smaller", "critique"),
    ("don't touch the parser", "critique"),
    ("sounds fine", "other"),
])
def test_plan_feedback_reads_as_one_closed_word(text, word):
    assert prompt_shape.plan_feedback_class(text) == word
    assert word in capture_catalogue.PLAN_FEEDBACK_CLASSES


def test_the_dialog_closed_or_a_hook_blocking_the_plan_gives_no_feedback_and_no_answer(tmp_path):
    closed = _parse(tmp_path, [
        _asked(), _plan_call(), _denied("tu_p", "The permission request was aborted", "user-rejected"), _reply("ok"),
    ])
    assert closed.turns[0].plan_stats.outcome is None and closed.turns[0].plan_stats.feedback_chars == 0
    assert closed.turns[1].preceding_denials == {"aborted": 1}
    assert _events(closed, EventKind.PLAN_FEEDBACK) == []


def test_an_exit_plan_mode_answer_adds_no_tool_error_but_a_hook_blocking_it_does(tmp_path):
    sent_back = _parse(tmp_path, [_asked(), _plan_call(), _plan_back("rename it"), _reply("ok")])
    closed = _parse(tmp_path, [
        _asked(), _plan_call(), _denied("tu_p", "The permission request was aborted", "interrupted"), _reply("ok"),
    ])
    for result in (sent_back, closed):
        turn = result.turns[0]
        assert (turn.tool_error_count, turn.tool_error_chars, turn.tool_errors_by_tool) == (0, 0, {})
        assert turn.tool_errors_by_kind == {}
    blocked = _parse(tmp_path, [
        _asked(), _plan_call(),
        _denied("tu_p", "PreToolUse:ExitPlanMode hook error: [check.sh] needs a test step", "permission-rule"),
        _reply("ok"),
    ])
    assert blocked.turns[0].tool_error_count == 1
    assert blocked.turns[0].tool_errors_by_tool == {"ExitPlanMode": 1}
    assert blocked.turns[0].plan_stats.outcome is None
    # Any other tool turned down is still a failed call.
    refused = _parse(tmp_path, [
        _asked(), _calls("Bash", "tu_1"), _denied("tu_1", "Permission to use Bash has been denied.", "permission-rule"),
        _reply("ok"),
    ])
    assert refused.turns[0].tool_error_count == 1


def test_a_go_ahead_you_type_approves_a_plan_the_dialog_sent_back(tmp_path):
    result = _parse(tmp_path, [
        _asked(), _plan_call(), _plan_back("add a step first"), _asked("go ahead"), _reply("building"),
    ])
    plan = result.turns[0].plan_stats
    assert plan.outcome == "approved_by_message"
    assert plan.rejected is True and plan.feedback_chars == len("add a step first")
    assert_privacy(result)


def test_leaving_plan_mode_approves_a_plan_the_dialog_sent_back(tmp_path):
    result = _parse(tmp_path, [
        _asked(permissionMode="plan"), _plan_call(), _plan_back("rename the helper", permissionMode="plan"),
        _asked("build it with the second option", permissionMode="acceptEdits"), _reply("building"),
    ])
    assert result.turns[0].plan_stats.outcome == "approved_by_message"


def test_a_mode_change_written_as_its_own_line_approves_a_plan_the_dialog_sent_back(tmp_path):
    result = _parse(tmp_path, [
        _asked(permissionMode="plan"), _plan_call(), _plan_back("rename the helper", permissionMode="plan"),
        {"type": "permission-mode", "permissionMode": "default", "sessionId": "s"}, _reply("building"),
    ])
    assert result.turns[0].plan_stats.outcome == "approved_by_message"
    stays = _parse(tmp_path, [
        _asked(permissionMode="plan"), _plan_call(), _plan_back("rename the helper", permissionMode="plan"),
        {"type": "permission-mode", "permissionMode": "plan", "sessionId": "s"}, _reply("replanning"),
    ])
    assert stays.turns[0].plan_stats.outcome == "rejected"


def test_a_message_that_isnt_a_go_ahead_or_a_mode_that_stays_in_plan_approves_nothing(tmp_path):
    kept_planning = _parse(tmp_path, [
        _asked(permissionMode="plan"), _plan_call(), _plan_back("make step two smaller", permissionMode="plan"),
        _asked("also look at the cache layer", permissionMode="plan"), _reply("replanning"),
    ])
    assert kept_planning.turns[0].plan_stats.outcome == "rejected"
    # Feedback that is itself a go-ahead is feedback, not your approval.
    feedback_only = _parse(tmp_path, [_asked(), _plan_call(), _plan_back("go ahead"), _reply("ok")])
    assert feedback_only.turns[0].plan_stats.outcome == "rejected"


def test_a_go_ahead_after_the_next_plan_call_belongs_to_that_plan(tmp_path):
    result = _parse(tmp_path, [
        _asked(), _plan_call("tu_p"), _plan_back("again, shorter", "tu_p"),
        _plan_call("tu_q"), _plan_back("", "tu_q"),
        _asked("continue"), _reply("building"),
    ])
    first = next(t.plan_stats for t in result.turns if t.plan_stats and t.plan_stats.feedback_chars)
    second = result.turns[1].plan_stats
    assert first.outcome == "rejected"
    assert second.outcome == "approved_by_message"


def test_a_plan_approved_in_the_dialog_stays_approved_when_you_later_say_go(tmp_path):
    result = _parse(tmp_path, [
        _asked(), _plan_call(), user_block_line([tool_result_block("tu_p", "ok")]), _asked("continue"), _reply("ok"),
    ])
    assert result.turns[0].plan_stats.outcome == "approved"


def test_an_answered_question_is_a_round_and_a_declined_one_is_not(tmp_path):
    result = _parse(tmp_path, [
        _asked(),
        _calls("AskUserQuestion", "q1", "q2", questions=[]),
        user_block_line([tool_result_block("q1", "answered")]),
        _denied("q2", _SENT_BACK, "user-rejected"),
        _reply("ok"),
        _calls("AskUserQuestion", "q3", questions=[]),
        user_block_line([tool_result_block("q3", "answered")]),
        _reply("done"),
    ])
    assert [turn.ask_rounds for turn in result.turns] == [1, 0, 1, 0]
    assert result.turns[1].preceding_denials == {"question_declined": 1}
    [cycle] = capture.prompt_cycles(result)
    assert cycle.ask_rounds == 2


def test_a_cycle_counts_its_plan_rounds_and_what_you_said_about_each(tmp_path):
    result = _parse(tmp_path, [
        _asked(), _plan_call("tu_1"), _plan_back("why not reuse the cache?", "tu_1"),
        _plan_call("tu_2"), _plan_back("", "tu_2"),
        _plan_call("tu_3"), _plan_back("I'm not sure about step 2", "tu_3"),
        _plan_call("tu_4"), user_block_line([tool_result_block("tu_4", "ok")]),
        _reply("building"),
    ])
    [cycle] = capture.prompt_cycles(result)
    assert (cycle.plan_rounds, cycle.rejected_rounds, cycle.feedback_rounds) == (4, 3, 2)
    assert cycle.plan_feedback_classes == ("question", "unsure")
    assert cycle.ask_rounds == 0


def test_a_cycle_with_a_plan_approved_by_typing_still_counts_the_rejection(tmp_path):
    result = _parse(tmp_path, [
        _asked(), _plan_call(), _plan_back("make it smaller"), _asked("go ahead"), _reply("building"),
    ])
    [first, second] = capture.prompt_cycles(result)
    assert (first.plan_rounds, first.rejected_rounds, first.feedback_rounds) == (1, 1, 1)
    assert (second.plan_rounds, second.rejected_rounds) == (0, 0)


@pytest.mark.parametrize("answer, after, stop", [
    (lambda: _denied("tu_1", "Permission to use Bash has been denied.", "permission-rule"), "refused", True),
    (lambda: _denied("tu_1", "The permission request was aborted", "interrupted"), "aborted", True),
    (lambda: _denied("tu_1", "PreToolUse:Bash hook error: [guard.sh] no", "permission-rule"), "hook_blocked", False),
    (lambda: _denied("tu_1", "Blocked by the classifier", "automode-blocked"), "auto_blocked", False),
])
def test_a_tool_use_interrupt_follows_the_denial_before_it_and_only_some_are_stops(tmp_path, answer, after, stop):
    result = _parse(tmp_path, [
        _asked(), _calls("Bash", "tu_1"), answer(),
        user_str_line("[Request interrupted by user for tool use]"), _reply("ok"),
    ])
    [interrupt] = _events(result, EventKind.INTERRUPT)
    assert (interrupt.subkind, interrupt.detail["after"]) == ("tool_refusal", after)
    assert events.is_stop(interrupt) is stop
    assert events.stop_window(result.turns[1].preceding_denials) is stop


def test_a_plan_you_answered_then_a_tool_use_interrupt_is_not_a_stop(tmp_path):
    result = _parse(tmp_path, [
        _asked(), _plan_call(), _plan_back("rename it"),
        user_str_line("[Request interrupted by user for tool use]"), _reply("ok"),
    ])
    [interrupt] = _events(result, EventKind.INTERRUPT)
    assert interrupt.detail["after"] == "plan_rejected" and not events.is_stop(interrupt)
    # It is still the interrupt it was for the coverage count.
    assert result.turns[1].preceding_primary == EventKind.INTERRUPT


def test_a_plain_interrupt_and_one_with_no_denial_before_it_are_stops(tmp_path):
    result = _parse(tmp_path, [
        _asked(), _reply("working"), user_str_line("[Request interrupted by user for tool use]"), _reply("ok"),
    ])
    [interrupt] = _events(result, EventKind.INTERRUPT)
    assert "after" not in interrupt.detail and events.is_stop(interrupt)
    assert events.stop_window({})


def test_the_new_plan_and_denial_fields_survive_the_digest_cache(tmp_path):
    result = _parse(tmp_path, [
        _asked(), _plan_call("tu_1"), _plan_back("why not reuse the cache?", "tu_1"),
        _asked("go ahead"), _reply("building"),
        _calls("AskUserQuestion", "q1", questions=[]), user_block_line([tool_result_block("q1", "answered")]),
        _calls("Bash", "tu_2"), _denied("tu_2", "PreToolUse:Bash hook error: [guard.sh] no", "permission-rule"),
        user_str_line("[Request interrupted by user for tool use]"), _reply("done"),
    ])
    decoded = cache.result_from_jsonable(json.loads(json.dumps(cache.encode_result(result))))
    assert decoded.turns == result.turns
    assert decoded.events == result.events
    assert decoded.turns[0].plan_stats.outcome == "approved_by_message"
    assert decoded.turns[0].plan_stats.feedback_class == "question"
    assert [turn.ask_rounds for turn in decoded.turns] == [turn.ask_rounds for turn in result.turns]
    assert any(turn.preceding_denials == {"hook_blocked": 1} for turn in decoded.turns)
    assert any(e.kind == EventKind.PLAN_FEEDBACK and e.subkind == "question" for e in decoded.events)
    assert any(e.detail.get("after") == "hook_blocked" for e in decoded.events if e.kind == EventKind.INTERRUPT)


# -- workflow runs (PARSER_VERSION 37) ----------------------------------------


def _workflow_result(tool_use_id: str, **result) -> dict:
    """A ``Workflow`` call's result line: the launch note in the block, the
    run's ids (and the text that must not be kept) on ``toolUseResult``."""
    body = {
        "status": "async_launched",
        "taskId": "wv62qx65p",
        "taskType": "local_workflow",
        "workflowName": "sentinel-workflow-name",
        "runId": "wf_6119c640-76c",
        "summary": "sentinel summary of the work",
        "transcriptDir": "sentinel/transcript/dir",
        "scriptPath": "sentinel/script/path.js",
    }
    body.update(result)
    return user_block_line(
        [tool_result_block(tool_use_id, "Workflow launched in background.")], toolUseResult=body
    )


def _workflow_session(tmp_path: Path, result_line: dict):
    return _parse(tmp_path, [
        _asked(),
        turn_line(content=[tool_use_block("Workflow", "tu_w", {"script": "return 1"})]),
        result_line,
        _reply("launched"),
    ])


def test_a_workflow_launch_keeps_its_run_and_task_ids_on_the_reply_that_made_the_call(tmp_path):
    result = _workflow_session(tmp_path, _workflow_result("tu_w"))
    assert [turn.workflow_runs for turn in result.turns] == [{"tu_w": ("wf_6119c640-76c", "wv62qx65p")}, {}]


def test_only_the_two_ids_of_a_workflow_launch_are_kept(tmp_path):
    result = _workflow_session(tmp_path, _workflow_result("tu_w"))
    kept = json.dumps(cache.encode_result(result))
    for sentinel in ("sentinel", "return 1", "launched in background"):
        assert sentinel not in kept
    assert_privacy(result)


@pytest.mark.parametrize("override", [
    {"taskType": "local_agent"},
    {"taskType": None},
    {"runId": None},
    {"runId": ""},
    {"runId": "wf_has a space"},
    {"runId": "wf_" + "x" * 80},
    {"runId": 7},
])
def test_a_workflow_result_that_names_no_usable_run_records_nothing(tmp_path, override):
    result = _workflow_session(tmp_path, _workflow_result("tu_w", **override))
    assert all(not turn.workflow_runs for turn in result.turns)


def test_a_workflow_call_that_failed_records_no_run(tmp_path):
    failed = user_block_line(
        [tool_result_block("tu_w", "Workflow failed to start", is_error=True)],
        toolUseResult={"taskType": "local_workflow", "runId": "wf_1", "taskId": "t1"},
    )
    result = _workflow_session(tmp_path, failed)
    assert all(not turn.workflow_runs for turn in result.turns)


def test_a_missing_or_malformed_task_id_leaves_the_run_with_an_empty_one(tmp_path):
    for task_id in (None, "has a space", 3):
        result = _workflow_session(tmp_path, _workflow_result("tu_w", taskId=task_id))
        assert result.turns[0].workflow_runs == {"tu_w": ("wf_6119c640-76c", "")}


def test_a_resumed_workflow_keeps_one_run_id_under_each_of_its_calls(tmp_path):
    result = _parse(tmp_path, [
        _asked(),
        turn_line(content=[tool_use_block("Workflow", "tu_w1", {"script": "a"})]),
        _workflow_result("tu_w1", taskId="t_first"),
        _asked("again"),
        turn_line(content=[tool_use_block("Workflow", "tu_w2", {"script": "a"})]),
        _workflow_result("tu_w2", taskId="t_second"),
        _reply("resumed"),
    ])
    runs = {}
    for turn in result.turns:
        runs.update(turn.workflow_runs)
    assert runs == {"tu_w1": ("wf_6119c640-76c", "t_first"), "tu_w2": ("wf_6119c640-76c", "t_second")}


def test_workflow_runs_survive_the_digest_cache_as_tuples(tmp_path):
    result = _workflow_session(tmp_path, _workflow_result("tu_w"))
    decoded = cache.result_from_jsonable(json.loads(json.dumps(cache.encode_result(result))))
    assert decoded.turns == result.turns
    assert decoded.turns[0].workflow_runs == {"tu_w": ("wf_6119c640-76c", "wv62qx65p")}
    assert isinstance(decoded.turns[0].workflow_runs["tu_w"], tuple)


# -- the plan check and the rating reminder (PARSER_VERSION 39) ----------------------

_CHECK_Q = {"question": capture_catalogue.PLAN_CHECK_QUESTION, "header": capture_catalogue.PLAN_CHECK_HEADER}
_REMINDER = f"{capture_catalogue.REMINDER_LABEL} {capture_catalogue.FEEDBACK_REMINDER_LINE}"


def _plan_check_session(tmp_path, answer=None, *, declined: bool = False):
    """A plan, a build, then a fix message and the plan check's question and how it came back."""
    lines = [
        _asked(), _plan_call("tu_plan"),
        user_block_line([tool_result_block("tu_plan", "ok")]),
        _calls("Edit", "tu_e", file_path="src/a.py"),
        user_block_line([tool_result_block("tu_e", "ok")]),
        _reply("built"),
        _asked("that is wrong, it fails on empty input"),
        turn_line(content=[tool_use_block("AskUserQuestion", "tu_q", {"questions": [_CHECK_Q]})]),
    ]
    if declined:
        lines.append(user_block_line([tool_result_block("tu_q", "User rejected tool use", is_error=True)]))
    else:
        result = {"questions": [_CHECK_Q], "answers": {_CHECK_Q["question"]: answer}}
        lines.append(user_block_line([tool_result_block("tu_q", "ok")], toolUseResult=result))
    lines.append(_reply("fixed"))
    return _parse(tmp_path, lines)


def _plan_checks(result) -> list:
    return [turn.plan_check for turn in result.turns if turn.plan_check is not None]


@pytest.mark.parametrize("option", capture_catalogue.PLAN_CHECK_OPTIONS, ids=lambda o: o[0])
def test_the_plan_checks_answer_is_read_as_its_word_and_the_plan_it_is_about(tmp_path, option):
    word, label, _description = option
    result = _plan_check_session(tmp_path, label)
    assert _plan_checks(result) == [PlanCheck("tu_plan", word)]
    assert word in capture_catalogue.PLAN_CHECK_WORDS
    assert_privacy(result)


@pytest.mark.parametrize("answer, declined", [("zebra-passphrase-4821 in my own words", False), (None, True)])
def test_a_declined_plan_check_or_an_other_answer_has_no_word_and_keeps_none_of_your_words(tmp_path, answer, declined):
    result = _plan_check_session(tmp_path, answer, declined=declined)
    assert _plan_checks(result) == [PlanCheck("tu_plan", "")]
    assert "zebra" not in repr(result.turns) + repr(result.events)
    assert_privacy(result)


def test_a_plan_check_is_about_the_latest_plan_asked_before_it(tmp_path):
    answer = capture_catalogue.PLAN_CHECK_OPTIONS[0][1]
    result = _parse(tmp_path, [
        _asked(), _plan_call("tu_old"), user_block_line([tool_result_block("tu_old", "ok")]),
        _asked("again"), _plan_call("tu_new"), user_block_line([tool_result_block("tu_new", "ok")]),
        turn_line(content=[tool_use_block("AskUserQuestion", "tu_q", {"questions": [_CHECK_Q]})]),
        user_block_line(
            [tool_result_block("tu_q", "ok")],
            toolUseResult={"questions": [_CHECK_Q], "answers": {_CHECK_Q["question"]: answer}},
        ),
        _reply("done"),
    ])
    assert _plan_checks(result) == [PlanCheck("tu_new", "covered")]


def test_the_feedback_questions_and_other_questions_are_no_plan_check(tmp_path):
    feedback_q = {"question": "How did it go?", "header": capture_catalogue.FEEDBACK_QUESTIONS[0].header}
    result = _parse(tmp_path, [
        _asked(), _plan_call("tu_plan"), user_block_line([tool_result_block("tu_plan", "ok")]),
        turn_line(content=[tool_use_block("AskUserQuestion", "tu_f", {"questions": [feedback_q]})]),
        user_block_line([tool_result_block("tu_f", "ok")]),
        turn_line(content=[tool_use_block("AskUserQuestion", "tu_o", {"questions": [
            {"question": "Which file?", "header": "File"}]})]),
        user_block_line([tool_result_block("tu_o", "ok")]),
        _reply("done"),
    ])
    assert _plan_checks(result) == []
    assert capture_tags.asks_plan_check({"questions": [feedback_q]}) is False


def test_the_plan_check_survives_the_digest_cache(tmp_path):
    result = _plan_check_session(tmp_path, capture_catalogue.PLAN_CHECK_OPTIONS[1][1])
    decoded = cache.result_from_jsonable(json.loads(json.dumps(cache.encode_result(result))))
    assert decoded.turns == result.turns
    assert _plan_checks(decoded) == [PlanCheck("tu_plan", "gap")]


@pytest.mark.parametrize("tool_input, asks", [
    ({"questions": [_CHECK_Q]}, True),
    ({"questions": [{"question": "x", "header": "Other"}, _CHECK_Q]}, True),
    ({"questions": [{"question": "x", "header": "Other"}]}, False),
    ({"questions": []}, False),
    ({"questions": "CG plan fix"}, False),
    ({"questions": [None, 3, "CG plan fix"]}, False),
    ({}, False),
    (None, False),
    ("CG plan fix", False),
])
def test_asks_plan_check_looks_only_at_the_header(tool_input, asks):
    assert capture_tags.asks_plan_check(tool_input) is asks


def _answered(answer) -> dict:
    return {"questions": [_CHECK_Q], "answers": {_CHECK_Q["question"]: answer}}


@pytest.mark.parametrize("result, word", [
    (_answered("Claude missed the plan"), "covered"),
    (_answered("The plan missed it"), "gap"),
    (_answered("Something new"), "new"),
    (_answered("Not a fix"), "none"),
    (_answered("something else entirely"), ""),
    (_answered(["Not a fix"]), ""),
    ({"questions": [_CHECK_Q], "answers": {}}, ""),
    ({"questions": [_CHECK_Q], "answers": None}, ""),
    ({"questions": [{"question": "q", "header": "Other"}], "answers": {"q": "Not a fix"}}, None),
    ({"questions": "nope", "answers": {}}, None),
    ({}, None),
    ("Not a fix", None),
    (None, None),
])
def test_plan_check_from_answers_gives_a_word_of_the_list_or_nothing_of_your_own(result, word):
    assert capture_tags.plan_check_from_answers(result) == word


@pytest.mark.parametrize("text, carries", [
    (f"Done.\n\n{_REMINDER}", True),
    (f"Done.\n\n{_REMINDER}\n\n[cg: task=feature]", True),
    (capture_catalogue.FEEDBACK_REMINDER_LINE, True),
    ("Done.", False),
    ("", False),
    # Quoted far above the end of a long reply: not written as the reminder.
    (f"{_REMINDER}\n" + "x" * (capture_tags.REMINDER_SCAN_CHARS + 50), False),
])
def test_carries_reminder_looks_at_the_end_of_the_reply_for_the_line(text, carries):
    assert capture_tags.carries_reminder(text) is carries


def test_a_reply_that_ends_with_the_reminder_line_is_marked_and_its_text_is_not_kept(tmp_path):
    result = _parse(tmp_path, [
        _asked("zebra-passphrase-4821"), _reply(f"Done with zebra-passphrase-4821.\n\n{_REMINDER}"),
        _asked("go on"), _reply("Fine."),
        _asked("again"), _reply(f"{_REMINDER}\n" + "y" * 2000),
    ])
    assert [turn.coach_reminder for turn in result.turns] == [True, False, False]
    assert "zebra" not in repr(result.turns) + repr(result.events)
    assert_privacy(result)


def test_the_last_text_block_decides_whether_a_reply_carries_the_reminder(tmp_path):
    result = _parse(tmp_path, [
        _asked(),
        turn_line(content=[{"type": "text", "text": _REMINDER}, {"type": "text", "text": "Actually, one more thing."}]),
    ])
    assert result.turns[0].coach_reminder is False


@pytest.mark.parametrize("text, kind", [
    ("cg-coach v1 plan_check\nThe user approved a plan.", "plan_check"),
    ("cg-coach v1 rating_reminder\nThis piece of work has used about 1.3M tokens.", "rating_reminder"),
    (f"{capture_catalogue.FEEDBACK_FACTS_MARKER} tokens=1300000 typical=0 followups=2", "feedback_facts"),
])
def test_the_feedback_notes_and_the_facts_line_are_the_hooks_own_context(text, kind):
    line = _note(text, hook="UserPromptSubmit")
    event = events.classify_line(line)
    assert (event.kind, event.subkind) == (EventKind.HOOK_OUTPUT, "coaching_note")
    assert event.detail == {"v": 1, "kind": kind, "hook": "UserPromptSubmit"}
    assert event.size_chars == len(line["rendered"][0]["content"])


def test_the_facts_line_without_rendered_is_sized_with_its_wrapper():
    text = f"{capture_catalogue.FEEDBACK_FACTS_MARKER} tokens=5 typical=0"
    event = events.classify_line(_note(text, hook="UserPromptSubmit", rendered=False))
    assert event.detail["kind"] == "feedback_facts"
    assert event.size_chars == len(text) + 63 + len("UserPromptSubmit")

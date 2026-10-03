"""Coaching notes (``coaching_notes``): the capture hook's live hints, how
the parser finds them again and prices them, the ``coaching.json`` split
points worked out from a report, the service job that keeps it fresh, and
the ``capture`` command around them.
"""

from __future__ import annotations

import importlib.util
import io
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from importlib import resources
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from claudeglass import capture, capture_catalogue as cat, cli, coaching, hook_health, ignores, installer, parse, prompt_shape
from claudeglass.config import load_config
from claudeglass.model import Recommendation, TranscriptMeta
from claudeglass.parse import parse_transcript
from claudeglass.pricing import load_pricing
from claudeglass.service.coaching_job import CoachingJob

from helpers import attachment_line, turn_line, user_str_line, write_jsonl

SCRIPT = Path(str(resources.files("claudeglass") / "hooks" / cat.HOOK_SCRIPT))
FIXTURES = Path(__file__).resolve().parent / "fixtures"
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


def _load_hook_module():
    spec = importlib.util.spec_from_file_location("_coaching_hook_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HOOK = _load_hook_module()
CATALOGUE = HOOK.load_catalogue()
ON = {"capture": {"level": "off", "coaching": ["coaching_notes"]}}


# -- the hook's hints --------------------------------------------------------------


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _reply(ctx: int, *, ago_s: float = 60, one_hour: bool = False, message_id: str | None = None, content=None) -> dict:
    return {
        "type": "assistant",
        "timestamp": _iso(NOW - timedelta(seconds=ago_s)),
        "message": {
            "id": message_id or f"msg_{ctx}_{ago_s}",
            "usage": {
                "input_tokens": 10,
                "cache_read_input_tokens": ctx - 10,
                "cache_creation_input_tokens": 0,
                "output_tokens": 0,
                "cache_creation": {"ephemeral_1h_input_tokens": 50 if one_hour else 0},
            },
            "content": content or [{"type": "text", "text": "ok"}],
        },
    }


def _prompt(text: str = "do it") -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}}


def _read_call(n: int) -> dict:
    return _reply(5_000 + n, content=[{"type": "tool_use", "id": f"toolu_{n}", "name": "Read", "input": {}}])


def _read_result(n: int, chars: int = 400) -> dict:
    return {
        "type": "user",
        "toolUseResult": {},
        "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"toolu_{n}", "content": "x" * chars}]},
    }


def _transcript(tmp_path: Path, records: list[dict], name: str = "session.jsonl") -> str:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return str(path)


def _config_dir(tmp_path: Path) -> Path:
    config_dir = tmp_path / "claudeglass"
    config_dir.mkdir(exist_ok=True)
    return config_dir


def _coach(tmp_path, payload, config=ON, *, now=NOW, raw_len=0) -> str:
    base = {"session_id": "s1", "cwd": "/work/app"}
    return HOOK.coaching_note_for({**base, **payload}, config, CATALOGUE, _config_dir(tmp_path), now=now, raw_len=raw_len)


def _prompt_payload(path: str) -> dict:
    return {"hook_event_name": "UserPromptSubmit", "transcript_path": path, "prompt": "next"}


def _kind(note: str) -> str:
    assert note.startswith(cat.COACH_MARKER + str(cat.COACH_VERSION) + " "), note
    return note.split("\n", 1)[0].rsplit(" ", 1)[1]


def test_nothing_is_added_while_coaching_notes_are_off(tmp_path):
    path = _transcript(tmp_path, [_prompt(), _reply(150_000)])
    assert _coach(tmp_path, _prompt_payload(path), {"capture": {"level": "deep"}}) == ""
    assert _coach(tmp_path, _prompt_payload(path), {}) == ""


def test_a_large_context_gets_the_clear_hint_when_you_send_a_message(tmp_path):
    path = _transcript(tmp_path, [_prompt(), _reply(120_000)])
    note = _coach(tmp_path, _prompt_payload(path))
    assert _kind(note) == "clear_context" and "about 120k tokens" in note
    small = _transcript(tmp_path, [_prompt(), _reply(60_000)], "small.jsonl")
    assert _coach(tmp_path, {**_prompt_payload(small), "session_id": "s2"}) == ""


def test_an_expired_cache_gets_the_cold_hint(tmp_path):
    path = _transcript(tmp_path, [_prompt(), _reply(30_000, ago_s=20 * 60)])
    note = _coach(tmp_path, _prompt_payload(path))
    assert _kind(note) == "cache_cold" and "idle for 20 minutes" in note and "about 30k tokens" in note
    # The 1-hour cache is still warm after 20 minutes.
    warm = _transcript(tmp_path, [_prompt(), _reply(30_000, ago_s=20 * 60, one_hour=True)], "warm.jsonl")
    assert _coach(tmp_path, {**_prompt_payload(warm), "session_id": "s2"}) == ""
    # Too small a context to mention.
    tiny = _transcript(tmp_path, [_prompt(), _reply(5_000, ago_s=20 * 60)], "tiny.jsonl")
    assert _coach(tmp_path, {**_prompt_payload(tiny), "session_id": "s3"}) == ""


def test_a_hint_rests_after_it_shows_unless_the_stake_grows(tmp_path):
    # The 1-hour cache keeps the cold hint out of the way half an hour on.
    path = _transcript(tmp_path, [_prompt(), _reply(120_000, one_hour=True)])
    assert _kind(_coach(tmp_path, _prompt_payload(path))) == "clear_context"
    assert _coach(tmp_path, _prompt_payload(path)) == ""
    # Another session has its own rest.
    assert _coach(tmp_path, {**_prompt_payload(path), "session_id": "s2"})
    grown = _transcript(tmp_path, [_prompt(), _reply(200_000)], "grown.jsonl")
    assert _kind(_coach(tmp_path, _prompt_payload(grown))) == "clear_context"
    assert _coach(tmp_path, _prompt_payload(path)) == ""
    later = NOW + timedelta(minutes=31)
    assert _kind(_coach(tmp_path, _prompt_payload(path), now=later)) == "clear_context"


def test_the_state_keeps_a_session_by_its_salted_hash(tmp_path):
    config_dir = _config_dir(tmp_path)
    salt = b"s" * 32
    (config_dir / HOOK.SALT_FILE).write_bytes(salt)
    _coach(tmp_path, _prompt_payload(_transcript(tmp_path, [_prompt(), _reply(120_000)])))
    state = json.loads((config_dir / cat.COACH_STATE_FILE).read_text(encoding="utf-8"))
    assert list(state["sessions"]) == [HOOK.session_hash(salt, "s1")]


def test_a_large_result_gets_the_quiet_hint_for_its_tool(tmp_path):
    bash = {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"}}
    note = _coach(tmp_path, bash, raw_len=40_000)
    assert _kind(note) == "quiet_output" and "about 10k tokens" in note and cat.COACHING_QUIET_HOW["Bash"] in note
    mcp = {"hook_event_name": "PostToolUse", "tool_name": "mcp__docs__search", "session_id": "s2"}
    assert cat.COACHING_QUIET_HOW[""] in _coach(tmp_path, mcp, raw_len=40_000)
    assert _coach(tmp_path, {**bash, "session_id": "s3"}, raw_len=4_000) == ""
    limited = {"hook_event_name": "PostToolUse", "tool_name": "Read", "tool_input": {"limit": 2000}, "session_id": "s4"}
    assert _coach(tmp_path, limited, raw_len=40_000) == ""


def test_many_reads_for_one_message_get_the_explore_hint(tmp_path):
    earlier = [_read_call(n) for n in range(90, 95)]
    calls = [record for n in range(1, 8) for record in (_read_call(n), _read_result(n))]
    path = _transcript(tmp_path, [_prompt("first"), *earlier, _prompt("second"), *calls, _read_call(8)])
    read = {"hook_event_name": "PostToolUse", "tool_name": "Read", "tool_use_id": "toolu_8", "transcript_path": path}
    note = _coach(tmp_path, read, raw_len=400)
    assert _kind(note) == "explore_reads" and "made 8 reads and searches" in note
    few = _transcript(tmp_path, [_prompt(), *calls[:10], _read_call(8)], "few.jsonl")
    assert _coach(tmp_path, {**read, "transcript_path": few, "session_id": "s2"}, raw_len=400) == ""
    # Inside a subagent, reads are its job.
    agent = {**read, "session_id": "s3", "agent_id": "a1", "agent_type": "Explore"}
    assert _coach(tmp_path, agent, raw_len=400) == ""


def test_the_explore_hint_points_at_tokensave_when_the_project_is_indexed(tmp_path):
    """A project with a ``.tokensave/`` index gets an Explore agent
    blocked by tokensave's own hook, so the advice switches to its own
    tools -- checked from the payload's ``cwd``, inlined
    (``HOOK._tokensave_indexed``) since this hook can't import
    ``claudeglass.known_savers``."""
    earlier = [_read_call(n) for n in range(90, 95)]
    calls = [record for n in range(1, 8) for record in (_read_call(n), _read_result(n))]
    path = _transcript(tmp_path, [_prompt("first"), *earlier, _prompt("second"), *calls, _read_call(8)])
    indexed = tmp_path / "indexed"
    (indexed / ".tokensave").mkdir(parents=True)
    read = {"hook_event_name": "PostToolUse", "tool_name": "Read", "tool_use_id": "toolu_8", "transcript_path": path}

    note = _coach(tmp_path, {**read, "cwd": str(indexed)}, raw_len=400)
    assert _kind(note) == "explore_reads" and "tokensave_context" in note and "Explore agent" not in note

    not_indexed = _coach(tmp_path, {**read, "cwd": str(tmp_path / "elsewhere"), "session_id": "s2"}, raw_len=400)
    assert _kind(not_indexed) == "explore_reads" and "hand it to an Explore agent" in not_indexed


def test_an_approved_plan_after_a_lot_of_planning_gets_the_fresh_session_hint(tmp_path):
    path = _transcript(tmp_path, [_prompt("x" * 4_000), _reply(16_000), _reply(90_000)])
    plan = {"hook_event_name": "PostToolUse", "tool_name": "ExitPlanMode", "transcript_path": path,
            "tool_input": {"plan": "p" * 4_000}}
    note = _coach(tmp_path, plan)
    # 90k less the 15k start (16k less the 1k message) less the 1k plan.
    assert _kind(note) == "plan_fresh" and "about 74k tokens" in note
    (tmp_path / "claudeglass" / cat.COACHING_FILE).write_text(json.dumps({"plan_fresh": False}), encoding="utf-8")
    assert _coach(tmp_path, {**plan, "session_id": "s2"}) == ""


def test_a_small_plan_context_gets_no_hint(tmp_path):
    path = _transcript(tmp_path, [_prompt(), _reply(16_000), _reply(50_000)])
    plan = {"hook_event_name": "PostToolUse", "tool_name": "ExitPlanMode", "transcript_path": path, "tool_input": {}}
    assert _coach(tmp_path, plan) == ""


def _plan_call(plan_id: str = "toolu_p", chars: int = 4_000, ctx: int = 16_000) -> dict:
    return _reply(ctx, ago_s=300, message_id=f"msg_{plan_id}", content=[
        {"type": "tool_use", "id": plan_id, "name": "ExitPlanMode", "input": {"plan": "p" * chars}}])


def _plan_answer(plan_id: str = "toolu_p", *, is_error: bool, **line) -> dict:
    return {"type": "user", **line, "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": plan_id, "content": "no" if is_error else "ok", "is_error": is_error}]}}


def _typed_approval(tmp_path, prompt: str, *records: dict, mode: str | None = None) -> str:
    path = _transcript(tmp_path, [_prompt("x" * 4_000), *records, _reply(90_000)])
    payload = {"hook_event_name": "UserPromptSubmit", "transcript_path": path, "prompt": prompt}
    return _coach(tmp_path, {**payload, "permission_mode": mode} if mode else payload)


def test_a_go_ahead_typed_after_a_plan_was_sent_back_gets_the_fresh_session_hint(tmp_path):
    note = _typed_approval(tmp_path, "go ahead", _plan_call(), _plan_answer(is_error=True))
    # 90k less the 15k start (16k less the 1k message) less the 1k plan.
    assert _kind(note) == "plan_fresh" and "about 74k tokens" in note


def test_the_fresh_session_hint_waits_for_a_go_ahead_and_a_plan_nobody_approved(tmp_path):
    sent_back = [_plan_call(), _plan_answer(is_error=True)]
    assert "plan_fresh" not in _typed_approval(tmp_path, "make the second step smaller", *sent_back)
    # Approved in the dialog, PostToolUse already had its say.
    approved = [_plan_call(), _plan_answer(is_error=False)]
    assert "plan_fresh" not in _typed_approval(tmp_path, "go ahead", *approved)
    # A newer plan call is the one waiting now, and it has no answer yet.
    replanned = [*sent_back, _plan_call("toolu_q", ctx=17_000), _plan_answer("toolu_q", is_error=False)]
    assert "plan_fresh" not in _typed_approval(tmp_path, "continue", *replanned)


def test_leaving_plan_mode_after_a_plan_was_sent_back_gets_the_fresh_session_hint(tmp_path):
    sent_back = [_plan_call(), _plan_answer(is_error=True, permissionMode="plan")]
    note = _typed_approval(tmp_path, "Build it, but keep the old name for the helper", *sent_back, mode="acceptEdits")
    assert _kind(note) == "plan_fresh"
    assert "plan_fresh" not in _typed_approval(tmp_path, "Keep planning, but add a step", *sent_back, mode="plan")


def test_the_typed_approval_hint_follows_the_plan_fresh_setting(tmp_path):
    (_config_dir(tmp_path) / cat.COACHING_FILE).write_text(json.dumps({"plan_fresh": False}), encoding="utf-8")
    assert "plan_fresh" not in _typed_approval(tmp_path, "go ahead", _plan_call(), _plan_answer(is_error=True))


def _said(text: str, ago_s: float, *, blocks: bool = False) -> dict:
    """A message you typed ``ago_s`` seconds before :data:`NOW`."""
    content = [{"type": "text", "text": text}] if blocks else text
    return {**_prompt(), "timestamp": _iso(NOW - timedelta(seconds=ago_s)), "message": {"role": "user", "content": content}}


def _stopped(ago_s: float, *, blocks: bool = False, sidechain: bool = False) -> dict:
    """The line Claude Code writes when you stop a reply with Esc."""
    record = _said("[Request interrupted by user]", ago_s, blocks=blocks)
    return {**record, "isSidechain": True} if sidechain else record


def _send(tmp_path, records, prompt: str, *, session: str = "s1", config=ON, now=NOW, name: str = "") -> str:
    path = _transcript(tmp_path, records, name or f"{session}.jsonl")
    payload = {"hook_event_name": "UserPromptSubmit", "transcript_path": path, "prompt": prompt, "session_id": session}
    return _coach(tmp_path, payload, config, now=now)


def _changed(ago_s: float, ctx: int = 30_000, *, say: str = "Done.", tool: str = "Edit", **kw) -> dict:
    """Claude's reply to a message: it changed a file, then said ``say``.
    On the 1-hour cache, so a reply minutes old isn't cold."""
    content = [{"type": "tool_use", "id": f"toolu_e{ago_s}", "name": tool, "input": {}}, {"type": "text", "text": say}]
    return _reply(ctx, ago_s=ago_s, content=content, message_id=f"msg_e{ago_s}", **{"one_hour": True, **kw})


#: A session's first message and Claude's work on it: the start of the
#: work, before any follow-up (the first message never counts as one).
_START = [_said("Build the settings page: " + "a form, a save button, a header, a footer. " * 8, 1_150),
          _changed(1_100)]


def test_small_requests_one_at_a_time_get_the_plan_it_as_one_prompt_hint(tmp_path):
    # No "fix" anywhere: what counts is that each short message got a file change.
    records = [
        _said("Build the settings page from the plan we agreed: " + "a form, a save button, a header. " * 10, 1_500),
        _changed(1_000),
        _said("make the save button bigger", 900), _changed(880, tool="Write"),
        _said("<command-name>/cost</command-name>", 700),
        _stopped(650, blocks=True),
        _said("now move the logo to the left", 600), _changed(580, tool="MultiEdit"),
    ]
    note = _send(tmp_path, records, "and the footer text should be grey")
    assert _kind(note) == "drip_feed" and "sent 3 small change requests in a row" in note
    # The note carries counts, never your words.
    assert "footer" not in note and "logo" not in note
    # A message Claude Code has already written to the transcript isn't counted twice.
    written = [*records, _said("and the footer text should be grey", 2)]
    assert "sent 3 small" in _send(tmp_path, written, "and the footer text should be grey", session="s2")


def test_a_run_needs_file_changes_short_messages_and_quick_follow_ups(tmp_path):
    def run(first_reply, middle="now move the logo to the left", gap_s=20):
        return [*_START, _said("make the save button bigger", 900), first_reply,
                _said(middle, 600), _changed(600 - gap_s)]

    assert _kind(_send(tmp_path, run(_changed(880)), "and the footer too")) == "drip_feed"
    # A reply that changed nothing (Claude only answered) breaks the run.
    assert _send(tmp_path, run(_reply(30_000, ago_s=880, one_hour=True)), "and the footer too", session="s2") == ""
    # So does a detailed message that plans several changes at once.
    detailed = run(_changed(880), middle="Now: " + "move the logo left, grey footer, wider form; " * 8)
    assert _send(tmp_path, detailed, "and the footer too", session="s3") == ""
    # And a message sent long after Claude's last reply.
    spaced = [_said("Build the settings page: " + "a form and a header. " * 20, 9_000), _changed(8_900),
              _said("make the save button bigger", 8_800), _changed(8_700), _said("now the logo", 8_600),
              _changed(8_500)]
    # (Two hours on, the cache has gone cold, which is its own hint.)
    assert _kind(_send(tmp_path, spaced, "and the footer too", session="s4")) == "cache_cold"
    # Your own count wins.
    two = [*_START, _said("make the save button bigger", 900), _changed(880)]
    assert _send(tmp_path, two, "and the footer too", session="s5") == ""
    fewer = {**ON, "thresholds": {"coaching_drip_count": 2}}
    assert _kind(_send(tmp_path, two, "and the footer too", session="s6", config=fewer)) == "drip_feed"


def test_answers_to_claudes_questions_and_thanks_are_not_requests(tmp_path):
    asked = [
        *_START, _said("make the save button bigger", 900), _changed(880),
        _said("now move the logo", 600), _changed(580, say="Moved it. Left or right of the title?"),
    ]
    # Answering Claude's question isn't another request...
    assert _send(tmp_path, asked, "left of the title") == ""
    # ...and doesn't break a run either.
    answered = [*asked, _said("left of the title", 400), _changed(380)]
    assert _kind(_send(tmp_path, answered, "and the footer too", session="s2")) == "drip_feed"
    done = [*_START, _said("make the save button bigger", 900), _changed(880), _said("now move the logo", 600),
            _changed(580)]
    for n, thanks in enumerate(("thanks", "ok, looks good!", "perfect, thank you")):
        assert _send(tmp_path, done, thanks, session=f"t{n}") == "", thanks


def test_a_vague_correction_gets_the_say_what_you_saw_hint(tmp_path):
    records = [_said("Add a dark mode toggle", 200), _reply(30_000, ago_s=100)]
    vagues = ("it's still broken", "doesn't work", "fix it", "wrong", "still broken, it should work", "wrong, make it right",
              "why is it still broken?")
    for n, vague in enumerate(vagues):
        assert _kind(_send(tmp_path, records, vague, session=f"v{n}")) == "vague_fix", vague
        assert prompt_shape.is_vague_fix(vague, 80), vague
    for n, fine in enumerate((
        "the toggle in settings.py still fails with KeyError",
        "fix line 42",
        "it's wrong: the toggle should say `Dark`",
        "looks good, thanks",
        "Now add a light mode toggle as well",
        # Saying what it should be instead is specific enough.
        "fix the greeting, it should say Hi",
        "wrong colour, make it red",
        "change the toggle to blue",
        # A question about fixes asks for an answer, not a fix.
        "What problems can you fix now you are on my machine",
        "how do I fix the build?",
    )):
        assert _send(tmp_path, records, fine, session=f"f{n}") == "", fine
        assert not prompt_shape.is_vague_fix(fine, 80), fine


def test_one_note_at_a_time_while_the_small_requests_keep_coming(tmp_path):
    records = [*_START, _said("fix the header", 600), _changed(580), _said("still wrong", 300), _changed(280)]
    assert _kind(_send(tmp_path, records, "fix it")) == "drip_feed"
    # The run hint is resting; the next vague fix in the same run adds nothing.
    more = [*records, _said("fix it", 200), _changed(180)]
    assert _send(tmp_path, more, "broken again", now=NOW + timedelta(seconds=30)) == ""


def test_a_huge_paste_gets_the_paste_less_hint(tmp_path):
    records = [_said("hi", 200), _reply(20_000, ago_s=100)]
    note = _send(tmp_path, records, "Why does this fail?\n" + "log line\n" * 5_000)
    assert _kind(note) == "big_paste" and "about 11k tokens" in note and "log line" not in note
    assert _send(tmp_path, records, "Why does this fail?\n" + "log line\n" * 1_000, session="s2") == ""


def test_stopping_claude_again_and_again_gets_the_agree_the_approach_hint(tmp_path):
    records = [
        _said("Refactor the store", 1_150), _stopped(1_100), _said("no, keep the API", 1_000),
        _stopped(600, blocks=True), _said("use the cache instead", 500), _stopped(60),
    ]
    note = _send(tmp_path, records, "just rename it for now")
    assert _kind(note) == "stop_loop" and "stopped you 3 times in the last 20 minutes" in note
    # Stops before the window, or inside a subagent, don't count.
    older = [*records[:2], _stopped(3_000), _stopped(60, sidechain=True), _stopped(60)]
    assert _send(tmp_path, older, "just rename it for now", session="s2") == ""


_BIG_TASK = (
    "Add a login page with email and password, a settings page where people change their name, email alerts "
    "when a report is ready, and an admin screen that lists every account."
)


def test_a_big_task_outside_plan_mode_gets_the_plan_first_hint(tmp_path):
    records = [*_START]
    note = _coach(tmp_path, {**_prompt_payload(_transcript(tmp_path, records)), "prompt": _BIG_TASK,
                             "permission_mode": "default"})
    assert _kind(note) == "plan_first" and "separate changes, outside plan mode" in note and "login" not in note
    listed = "Please do these:\n1. add a login page\n2. add a settings page\n3. email alerts\n4. an admin screen\n" + (
        "Keep the existing styles and tests passing throughout, and don't touch the database schema."
    )
    note = _coach(tmp_path, {**_prompt_payload(_transcript(tmp_path, records, "l.jsonl")), "prompt": listed,
                             "permission_mode": "acceptEdits", "session_id": "s2"})
    assert _kind(note) == "plan_first"


def test_the_plan_first_hint_stays_out_of_the_way(tmp_path):
    path = _transcript(tmp_path, [*_START])

    def send(prompt, mode, session, records_path=path):
        payload = {**_prompt_payload(records_path), "prompt": prompt, "session_id": session}
        return _coach(tmp_path, {**payload, "permission_mode": mode} if mode else payload)

    # Already in plan mode, or no mode reported.
    assert send(_BIG_TASK, "plan", "a") == ""
    assert send(_BIG_TASK, None, "b") == ""
    # A message about a plan, or building one already approved.
    assert send("Carry out the plan: " + _BIG_TASK, "default", "c") == ""
    approved = _transcript(tmp_path, [*_START, _reply(30_000, ago_s=100, one_hour=True, content=[
        {"type": "tool_use", "id": "toolu_p", "name": "ExitPlanMode", "input": {}}])], "approved.jsonl")
    assert send(_BIG_TASK, "default", "d", approved) == ""
    # A short request, or one asking for fewer changes.
    assert send("Add login, settings, alerts and an admin screen", "default", "e") == ""
    one_change = (
        "Make the header sticky so it stays at the top when the page scrolls, and keep its shadow the same as it is "
        "today on the settings page and on the dashboard."
    )
    assert send(one_change, "default", "f") == ""


def test_sending_the_same_request_again_gets_the_say_what_was_wrong_hint(tmp_path):
    records = [*_START, _said("Make the save button bigger and move it to the right", 600), _changed(580)]
    note = _send(tmp_path, records, "make the save button bigger and move it right")
    assert _kind(note) == "repeat_ask" and "button" not in note
    # A different request, an old one, or a short one isn't a repeat.
    assert "repeat_ask" not in _send(tmp_path, records, "now add a cancel button next to it", session="s2")
    old = [*_START[:1], _said("Make the save button bigger and move it to the right", 7_000), _changed(6_900)]
    assert "repeat_ask" not in _send(tmp_path, old, "make the save button bigger and move it right", session="s3")
    short = [*_START, _said("do it", 600), _changed(580)]
    assert "repeat_ask" not in _send(tmp_path, short, "do it", session="s4")


def test_a_message_resent_after_esc_before_any_reply_is_not_a_repeat_but_a_stop(tmp_path):
    # Esc before any reply leaves no stop marker: only the message Claude
    # never answered, sent again.
    sqlite = "Use SQLite instead of JSON for the store, with the same class and docstrings"
    records = [*_START, _said(sqlite, 300), _said(sqlite, 200)]
    assert "repeat_ask" not in _send(tmp_path, records, sqlite)
    stops = [*_START, _said("refactor the store", 900), _changed(880), _stopped(870, blocks=True),
             _said("keep the API", 600), _said("keep the API as it is", 500)]
    note = _send(tmp_path, stops, "use the cache instead of the API", session="s2")
    assert _kind(note) == "stop_loop" and "stopped you 3 times" in note
    # A stop that left a marker isn't counted twice.
    marked = [*_START, _said("refactor the store", 900), _changed(895), _stopped(890), _said("keep the API", 600),
              _changed(595), _stopped(590), _said("use the cache", 400), _changed(380)]
    assert "stop_loop" not in _send(tmp_path, marked, "just rename it", session="s3")


def _failed(ago_s: float) -> dict:
    """What Claude Code writes when a reply dies on an API error."""
    return {
        "type": "assistant", "timestamp": _iso(NOW - timedelta(seconds=ago_s)), "isApiErrorMessage": True,
        "message": {"id": f"msg_f{ago_s}", "model": "<synthetic>", "role": "assistant",
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                    "content": [{"type": "text", "text": "API Error: 529 Overloaded"}]},
    }


def test_sending_a_request_again_after_an_api_error_is_not_a_repeat_or_a_stop(tmp_path):
    ask = "Refactor the payment retry logic in billing/retry.py to use exponential backoff"
    records = [*_START, _said(ask, 600), _failed(598)]
    assert _send(tmp_path, records, ask) == ""
    # Three failures in a row aren't three stops either.
    failures = [*_START, _said(ask, 600), _failed(598), _said(ask, 500), _failed(498), _said(ask, 400), _failed(398)]
    assert _send(tmp_path, failures, ask, session="s2") == ""
    # A reply that got through and was sent again still is a repeat.
    answered = [*_START, _said(ask, 600), _failed(598), _said(ask, 500), _changed(480)]
    assert _kind(_send(tmp_path, answered, ask, session="s3")) == "repeat_ask"


@pytest.mark.parametrize("prompt", [
    "<task-notification>\n<task-id>a1</task-id>\n<status>completed</status>\n<result>Done.\n"
    + "".join(f"- mod{n}.py: added the docstrings\n" for n in range(12)) + "</result>\n</task-notification>",
    "<task-notification>\n<result>" + "a long report " * 4_000 + "</result>\n</task-notification>",
    "<scheduled-task>Check the build and fix whatever broke, then add tests, update docs and bump it</scheduled-task>",
    'Another Claude session sent a message:\n<agent-message from="a57b4a7d930e32da8">\n[Subagent hand-back] '
    + "".join(f"\n  - fix {n}: changed mod{n}.py and added a test" for n in range(12)) + "\n</agent-message>",
], ids=["agent report listing files", "long agent report", "scheduled task", "subagent hand-back"])  # short: Windows caps env vars
def test_a_message_you_didnt_type_gets_no_prompting_or_context_hint(tmp_path, prompt):
    # A background agent's report comes back as the next message: it isn't
    # yours, however long or list-shaped it is, and even in a big context.
    records = [*_START, _said("fix the header", 600), _changed(580, 150_000), _said("still wrong", 300),
               _changed(280, 150_000)]
    path = _transcript(tmp_path, records)
    payload = {"hook_event_name": "UserPromptSubmit", "transcript_path": path, "prompt": prompt,
               "permission_mode": "default"}
    assert HOOK.coaching_for({"session_id": "s1", "cwd": "/w", **payload}, ON, CATALOGUE, _config_dir(tmp_path),
                             now=NOW) == ("", "")
    # The same list typed by you still gets its hint.
    typed = "Add these: " + "".join(f"\n- add a check to mod{n}.py" for n in range(6))
    note = _coach(tmp_path, {**payload, "prompt": typed, "session_id": "s2"})
    assert note and _kind(note) != ""


def test_the_status_line_and_the_parser_skip_a_failed_reply_too(tmp_path):
    from claudeglass import statusline

    ask = "Refactor the payment retry logic in billing/retry.py to use exponential backoff"
    tail = [*_START, _said(ask, 600), _failed(598), _said(ask, 500), _failed(498), _said(ask, 400), _failed(398),
            _said(ask, 5)]
    assert not [k for *_, k in statusline._prompt_habits(tail, 30_000, NOW) if k in ("repeat_ask", "stop_loop")]
    path = Path(_transcript(tmp_path, tail, "parsed.jsonl"))
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    assert not any(e.detail.get("repeat") for e in result.events if e.kind.name == "HUMAN_TEXT")


def test_the_hook_counts_steps_and_likeness_as_the_package_does():
    from claudeglass import prompt_shape

    samples = [
        _BIG_TASK, "Create page.html: a simple settings page with a heading, a name field and a save button.",
        "1. add a\n2. add b\n- c\n* d", "Why does the build fail?", "Add tests. Then rename the store and bump it.",
    ]
    for text in samples:
        assert HOOK._request_steps(text, CATALOGUE["coaching"]) == prompt_shape.request_steps(text), text
    a, b = "make the save button bigger please", "Make the save button bigger"
    assert HOOK._similarity(HOOK._words(a), HOOK._words(b)) == prompt_shape.similarity(
        prompt_shape.words(a), prompt_shape.words(b))


def test_the_hook_reads_the_not_typed_lists_from_the_catalogue():
    assert HOOK._not_typed_prefixes() == cat.NOT_TYPED_PREFIXES
    assert HOOK._not_typed_origins() == cat.NOT_TYPED_TURN_ORIGINS
    assert tuple(CATALOGUE["coaching"]["not_typed_prefixes"]) == cat.NOT_TYPED_PREFIXES
    assert tuple(CATALOGUE["coaching"]["not_typed_turn_origins"]) == cat.NOT_TYPED_TURN_ORIGINS


#: A message the hook, the status line and the parser must each read the same way.
_NOT_TYPED_SAMPLES = [
    *(prefix + " rest of the line" for prefix in cat.NOT_TYPED_PREFIXES),
    "   " + cat.APP_QUIT_PREFIX + ", so carry on",
    "<cross-session-message from=\"a\">hello</cross-session-message>",
]


@pytest.mark.parametrize("text", _NOT_TYPED_SAMPLES)
def test_the_hook_the_status_line_and_the_parser_agree_on_what_you_didnt_type(text):
    from claudeglass import events, statusline
    from claudeglass.model import EventKind

    line = {"type": "user", "message": {"role": "user", "content": text}, "origin": {"kind": "human"},
            "timestamp": "2026-09-18T12:00:00.000Z"}
    hook_exchanges, _ = HOOK._exchanges([line], cat.INTERRUPT_PREFIX, ())
    status_exchanges = statusline._exchanges([line], cat.INTERRUPT_PREFIX, ())
    assert hook_exchanges == [] and status_exchanges == []
    assert events.classify_line(line).kind != EventKind.HUMAN_TEXT


@pytest.mark.parametrize("turn_origin, typed", [
    ("task_notification", False), ("peer", False), ("scheduled", False),
    ("human", True), ("sdk", True), (None, True),
])
def test_the_hook_the_status_line_and_the_parser_agree_on_a_turn_origin(turn_origin, typed):
    from claudeglass import events, statusline
    from claudeglass.model import EventKind

    line = {"type": "user", "message": {"role": "user", "content": "fix the header"}, "isSidechain": False,
            "timestamp": "2026-09-18T12:00:00.000Z", "origin": {"kind": "human"}}
    if turn_origin is not None:
        line["turnOrigin"] = turn_origin
    assert HOOK._is_human_prompt(line) is typed
    assert statusline._is_human_prompt(line) is typed
    assert (events.classify_line(line).kind == EventKind.HUMAN_TEXT) is typed


def test_a_message_that_only_looks_like_a_resume_note_is_still_yours():
    # The prefix must start the line: a message that mentions the note does not.
    text = "why does it say " + cat.APP_QUIT_PREFIX + " when I closed it?"
    from claudeglass import events
    from claudeglass.model import EventKind

    line = {"type": "user", "message": {"role": "user", "content": text}, "origin": {"kind": "human"}}
    assert events.classify_line(line).kind == EventKind.HUMAN_TEXT
    assert len(HOOK._exchanges([line], cat.INTERRUPT_PREFIX, ())[0]) == 1


def test_a_prompt_hint_comes_before_the_context_hints(tmp_path):
    records = [*_START, _said("fix the header", 600), _changed(580, 150_000), _said("still wrong", 300),
               _changed(280, 150_000)]
    assert _kind(_send(tmp_path, records, "fix it")) == "drip_feed"
    assert _kind(_send(tmp_path, records, "fix it", now=NOW + timedelta(seconds=30))) == "clear_context"


def _agent(tmp_path, records, agent_id="abc", *, nested: bool = False) -> tuple[str, Path]:
    session = _transcript(tmp_path, [_prompt(), _reply(20_000)], "sess.jsonl")
    folder = tmp_path / "sess" / "subagents"
    if nested:
        folder = folder / "workflows" / "run1"
    agent_path = Path(_transcript(folder, records, f"agent-{agent_id}.jsonl"))
    return session, agent_path


def test_a_long_subagent_run_gets_the_split_hint_at_your_split_point(tmp_path):
    config_dir = _config_dir(tmp_path)
    (config_dir / cat.COACHING_FILE).write_text(json.dumps({"split_run": {"general-purpose": 3}}), encoding="utf-8")
    # Two records of one reply count once.
    session, agent_path = _agent(tmp_path, [_prompt(), _reply(1_000, message_id="m1"), _reply(1_000, message_id="m1"),
                                            _reply(2_000, message_id="m2")])
    call = {"hook_event_name": "PostToolUse", "tool_name": "Bash", "transcript_path": session, "agent_id": "abc",
            "agent_type": "general-purpose"}
    assert _coach(tmp_path, call) == ""
    with open(agent_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(_reply(3_000, message_id="m3")) + "\n")
    # You get a notice; the subagent gets nothing.
    note, notice = HOOK.coaching_for({"session_id": "s1", "cwd": "/work/app", **call}, ON, CATALOGUE, config_dir, now=NOW)
    assert note == "" and notice == cat.COACHING_NOTICE["split_run"].format(agent="general-purpose", replies=3, every_n=3)
    state = json.loads((config_dir / cat.COACH_STATE_FILE).read_text(encoding="utf-8"))
    assert state["agents"]["abc"]["replies"] == 3
    # Once a run, however long it goes on.
    with open(agent_path, "a", encoding="utf-8") as handle:
        handle.writelines(json.dumps(_reply(3_000 + n, message_id=f"m{n}")) + "\n" for n in range(10, 20))
    assert HOOK.coaching_for({"session_id": "s1", "cwd": "/work/app", **call}, ON, CATALOGUE, config_dir,
                             now=NOW + timedelta(hours=2)) == ("", "")
    # A summary starts the count again.
    with open(agent_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "system", "subtype": "compact_boundary"}) + "\n")
        handle.write(json.dumps(_reply(1_000, message_id="m4")) + "\n")
    assert _coach(tmp_path, call, now=NOW + timedelta(hours=3)) == ""
    assert json.loads((config_dir / cat.COACH_STATE_FILE).read_text(encoding="utf-8"))["agents"]["abc"]["replies"] == 1


def test_the_split_hint_needs_your_split_point_and_skips_workflow_agents(tmp_path):
    config_dir = _config_dir(tmp_path)
    (config_dir / cat.COACHING_FILE).write_text(json.dumps({"split_run": {"general-purpose": 1}}), encoding="utf-8")
    session, _ = _agent(tmp_path, [_reply(1_000, message_id="m1")])
    other = {"hook_event_name": "PostToolUse", "tool_name": "Bash", "transcript_path": session, "agent_id": "abc",
             "agent_type": "Explore"}
    assert _coach(tmp_path, other) == ""
    assert not (config_dir / cat.COACH_STATE_FILE).exists()
    nested_session, _ = _agent(tmp_path, [_reply(1_000, message_id="m1")], "wf1", nested=True)
    workflow = {**other, "agent_type": "general-purpose", "agent_id": "wf1", "transcript_path": nested_session}
    assert _coach(tmp_path, workflow) == ""


def test_your_thresholds_win_over_the_file_and_the_defaults(tmp_path):
    config_dir = _config_dir(tmp_path)
    (config_dir / cat.COACHING_FILE).write_text(
        json.dumps({"thresholds": {"clear_context_tokens": 70_000}}), encoding="utf-8"
    )
    path = _transcript(tmp_path, [_prompt(), _reply(60_000)])
    assert _coach(tmp_path, _prompt_payload(path)) == ""
    configured = {**ON, "thresholds": {"coaching_clear_context_tokens": 50_000}}
    assert _kind(_coach(tmp_path, _prompt_payload(path), configured)) == "clear_context"


def test_a_project_capture_leaves_out_gets_no_coaching(tmp_path):
    path = _transcript(tmp_path, [_prompt(), _reply(150_000)])
    limited = {"capture": {**ON["capture"], "projects": ["other-project"]}}
    assert _coach(tmp_path, _prompt_payload(path), limited) == ""
    excluded = {**ON, "exclude_projects": ["work-app"]}
    assert _coach(tmp_path, _prompt_payload(path), excluded) == ""


def test_every_hint_text_takes_the_fields_the_hook_fills():
    assert set(cat.COACHING_TEXT) == set(cat.COACHING_HINTS)
    assert CATALOGUE["coaching"]["text"] == cat.COACHING_TEXT


def test_coaching_notes_add_the_prompt_and_plan_hooks():
    specs = cat.hook_specs(("coaching_notes",))
    assert (cat.HOOK_SCRIPT, "UserPromptSubmit", "", False) in specs
    tool = next(spec for spec in specs if spec[1] == "PostToolUse")
    assert "ExitPlanMode" in tool[2].split("|") and "Read" in tool[2].split("|")
    deep = cat.hook_specs((*cat.level_metrics("deep"), "coaching_notes"))
    assert sum(1 for spec in deep if spec[1] == "PostToolUse") == 1


# -- the hook, run as Claude Code runs it ------------------------------------------


def _undated(record: dict) -> dict:
    """A reply without a time, so the real clock the hook runs on can't make
    its cache look cold."""
    return {key: value for key, value in record.items() if key != "timestamp"}


def _run(config_dir: Path, payload: dict) -> tuple[int, str, str]:
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "--config-dir", str(config_dir)],
        input=json.dumps(payload).encode("utf-8"),
        capture_output=True,
        timeout=30,
    )
    return done.returncode, done.stdout.decode("utf-8"), done.stderr.decode("utf-8")


def test_the_hook_runs_coaching_notes_with_capture_off(tmp_path):
    config_dir = _config_dir(tmp_path)
    (config_dir / "config.toml").write_text('[capture]\nlevel = "off"\ncoaching = ["coaching_notes"]\n', encoding="utf-8")
    path = _transcript(tmp_path, [_prompt(), _undated(_reply(150_000))])
    rc, out, err = _run(config_dir, {"session_id": "s1", "cwd": "/w", **_prompt_payload(path)})
    assert rc == 0 and err == ""
    output = json.loads(out)["hookSpecificOutput"]
    assert output["hookEventName"] == "UserPromptSubmit"
    assert _kind(output["additionalContext"]) == "clear_context"
    # A context hint depends on what the message is about, so it has no notice.
    assert "systemMessage" not in json.loads(out)


def test_a_prompting_hint_also_shows_you_a_notice_at_once(tmp_path):
    config_dir = _config_dir(tmp_path)
    (config_dir / "config.toml").write_text('[capture]\nlevel = "off"\ncoaching = ["coaching_notes"]\n', encoding="utf-8")
    now = datetime.now(timezone.utc)

    def said(text, minutes):
        return {**_prompt(text), "timestamp": _iso(now - timedelta(minutes=minutes))}

    def changed(minutes):
        # Timed on the real clock, with the 1-hour cache so it isn't cold.
        return {**_changed(0, 20_000, one_hour=True), "timestamp": _iso(now - timedelta(minutes=minutes))}

    records = [said("Build the settings page: " + "a form and a header. " * 20, 15), changed(14),
               said("make the save button bigger", 10), changed(9), said("now move the logo", 5), changed(4)]
    payload = {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "cwd": "/w",
               "transcript_path": _transcript(tmp_path, records), "prompt": "and the footer text too"}
    rc, out, err = _run(config_dir, payload)
    assert rc == 0 and err == ""
    output = json.loads(out)
    assert _kind(output["hookSpecificOutput"]["additionalContext"]) == "drip_feed"
    assert output["systemMessage"] == cat.COACHING_NOTICE["drip_feed"].format(count=3)
    assert "footer" not in out


def test_a_tip_for_the_user_is_a_highlighted_block_and_the_notices_are_the_prompting_hints():
    to_the_user = {
        "plan_fresh", "cache_cold", "clear_context", "repeat_ask", "drip_feed", "stop_loop", "plan_first", "vague_fix",
        "big_paste",
    }
    for hint, text in cat.COACHING_TEXT.items():
        assert (cat.TIP_LABEL in text) == (hint in to_the_user), hint
    assert set(cat.COACHING_NOTICE) == {
        "repeat_ask", "drip_feed", "stop_loop", "plan_first", "vague_fix", "big_paste", "split_run",
    }
    # split_run tells the subagent nothing.
    assert cat.COACHING_TEXT["split_run"] == ""
    assert CATALOGUE["coaching"]["notice"] == cat.COACHING_NOTICE
    assert all(notice.startswith("⚠️ ClaudeGlass: ") for notice in cat.COACHING_NOTICE.values())
    reminder = cat.note_text(["feedback_reminder"], "main")
    assert f"{cat.REMINDER_LABEL} {cat.FEEDBACK_REMINDER_LINE}" in reminder


def test_the_prompting_hints_ask_only_for_a_tip_and_never_steer_the_work():
    # They're about how the user prompts: planning first, asking first,
    # waiting for a go-ahead or dropping an approach changed the work.
    steering = re.compile(
        r"before (?:changing|you change) anything|wait for a go-ahead|don't repeat|try a different way"
        r"|ask one short question|set out in a few lines|carry on unless",
        re.IGNORECASE,
    )
    for hint in ("repeat_ask", "drip_feed", "stop_loop", "plan_first", "vague_fix"):
        text = cat.COACHING_TEXT[hint]
        assert not steering.search(text), hint
        assert "changes nothing about the work" in text and cat.TIP_LABEL in text, hint


def test_the_feedback_reminder_is_asked_for_once_a_session():
    for tagger in cat.TAGGERS:
        note = cat.note_text(["feedback_reminder"], "main", tagger=tagger)
        assert "first time in this session" in note and "never again" in note, tagger


_EMOJI = re.compile("[←-⯿\U0001f000-\U0001faff️]")


def test_nothing_claude_reads_carries_an_emoji():
    # Claude copies what its context shows: with a ⚠️ and a 💡 in the tip
    # and reminder labels, it began using them as its own markers in
    # unrelated work. The notices shown only to you never reach it.
    everything = {*cat.LEVEL_METRIC_IDS, *cat.FEEDBACK_IDS, *cat.COACHING_IDS}
    texts = {f"coaching {hint}": text for hint, text in cat.COACHING_TEXT.items()}
    for scope, agent_type in (("main", ""), ("subagent", "general-purpose"), ("subagent", "Explore")):
        for tagger in cat.TAGGERS:
            texts[f"{scope} note ({tagger})"] = cat.note_text(everything, scope, agent_type, tagger)
    texts["judge"] = cat.judge_text(everything)
    for metric in cat.METRICS:
        texts[f"{metric.id} tool note"] = cat.tool_note_text(metric.id)
    for name, text in texts.items():
        assert not _EMOJI.search(text), name
    assert all(_EMOJI.search(notice) for notice in cat.COACHING_NOTICE.values())


def test_the_notice_takes_the_same_fields_as_the_note(tmp_path):
    records = [_said("Refactor the store", 1_150), _stopped(1_100), _stopped(600), _stopped(60)]
    path = _transcript(tmp_path, records)
    payload = {"session_id": "s1", "cwd": "/w", "hook_event_name": "UserPromptSubmit", "transcript_path": path,
               "prompt": "just rename it"}
    note, notice = HOOK.coaching_for(payload, ON, CATALOGUE, _config_dir(tmp_path), now=NOW)
    assert _kind(note) == "stop_loop" and notice == cat.COACHING_NOTICE["stop_loop"].format(count=3, minutes=20)
    big = {**payload, "session_id": "s2", "prompt": "x" * 48_000,
           "transcript_path": _transcript(tmp_path, [_said("hi", 200)], "big.jsonl")}
    assert HOOK.coaching_for(big, ON, CATALOGUE, _config_dir(tmp_path), now=NOW)[1].startswith(
        "⚠️ ClaudeGlass: this message is about 12k tokens")


def test_a_capture_note_and_a_coaching_note_go_out_as_one(tmp_path):
    config_dir = _config_dir(tmp_path)
    (config_dir / "config.toml").write_text('[capture]\nlevel = "deep"\ncoaching = ["coaching_notes"]\n', encoding="utf-8")
    big = {"session_id": "s1", "cwd": "/w", "hook_event_name": "PostToolUse", "tool_name": "Bash",
           "tool_response": {"stdout": "x" * cat.BIG_OUTPUT_TOKENS * 4}}
    rc, out, _ = _run(config_dir, big)
    text = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    capture_note, coaching_note = text.split("\n" + cat.COACH_MARKER)
    assert rc == 0 and capture_note == cat.tool_note_text("big_output")
    assert coaching_note.startswith(f"{cat.COACH_VERSION} quiet_output\n")


def test_a_broken_coaching_file_or_state_is_read_as_empty(tmp_path):
    config_dir = _config_dir(tmp_path)
    (config_dir / "config.toml").write_text('[capture]\ncoaching = ["coaching_notes"]\n', encoding="utf-8")
    (config_dir / cat.COACHING_FILE).write_text("{not json", encoding="utf-8")
    (config_dir / cat.COACH_STATE_FILE).write_text('{"sessions": [1, 2]}', encoding="utf-8")
    path = _transcript(tmp_path, [_prompt(), _undated(_reply(150_000))])
    rc, out, err = _run(config_dir, {"session_id": "s1", **_prompt_payload(path)})
    assert rc == 0 and err == "" and _kind(json.loads(out)["hookSpecificOutput"]["additionalContext"]) == "clear_context"
    rc, out, err = _run(config_dir, {"session_id": "s1", "hook_event_name": "UserPromptSubmit", "transcript_path": 5})
    assert (rc, out, err) == (0, "", "")


# -- the parser and what the notes cost -----------------------------------------------


@pytest.fixture()
def _salt():
    parse.set_salt(b"c" * 32)


def _note_line(text: str, hook: str, second: int) -> dict:
    wrapped = f"<system-reminder>\n{hook} hook additional context: {text}\n</system-reminder>"
    line = attachment_line("hook_additional_context", rendered=wrapped, content=[text], hookName=hook,
                           hookEvent=hook.split(":")[0], toolUseID=hook)
    line["timestamp"] = f"2026-09-18T12:00:{second:02d}.000Z"
    return line


def _coach_text(kind: str) -> str:
    return f"{cat.COACH_MARKER}{cat.COACH_VERSION} {kind}\nSome hint text."


def _session(tmp_path, lines, name="top.jsonl", **meta):
    path = tmp_path / name
    write_jsonl(path, lines)
    return parse_transcript(path, TranscriptMeta(path=str(path), **meta))


def _ask(second: int) -> dict:
    return user_str_line("go", origin={"kind": "human"}, timestamp=f"2026-09-18T12:00:{second:02d}.000Z")


def _turn(second: int) -> dict:
    return turn_line(content=[{"type": "text", "text": "ok"}], model="claude-widget-9",
                     timestamp=f"2026-09-18T12:00:{second:02d}.000Z", cache_read_input_tokens=5_000)


def test_a_coaching_note_is_its_own_event_and_not_a_capture_note(tmp_path, _salt):
    note = _note_line(_coach_text("clear_context"), "UserPromptSubmit", 1)
    result = _session(tmp_path, [_ask(0), note, _turn(2)])
    event = next(e for e in result.events if e.subkind == "coaching_note")
    assert event.detail == {"v": cat.COACH_VERSION, "kind": "clear_context", "hook": "UserPromptSubmit"}
    assert result.turns[0].cap_note_chars == len(note["rendered"][0]["content"])
    assert result.meta.cap_injections == 0 and result.meta.cap_metrics == ()


def test_a_shared_attachment_splits_into_its_capture_and_coaching_parts(tmp_path, _salt):
    text = cat.tool_note_text("big_output") + "\n" + _coach_text("quiet_output")
    note = _note_line(text, "PostToolUse:Bash", 1)
    result = _session(tmp_path, [_ask(0), note, _turn(2)])
    event = next(e for e in result.events if e.subkind == "capture_note")
    coach_chars = len(_coach_text("quiet_output")) + 1
    assert event.detail["codes"] == ["big_output"] and event.detail["coach"] == "quiet_output"
    assert event.detail["coach_chars"] == coach_chars
    assert event.size_chars == len(note["rendered"][0]["content"]) - coach_chars
    assert result.turns[0].cap_note_chars == len(note["rendered"][0]["content"])
    assert result.meta.cap_injections == 1


def test_coaching_usage_counts_and_prices_the_notes_by_hint(tmp_path, _salt):
    pricing = load_pricing(path=FIXTURES / "pricing_min.toml")
    top = _session(tmp_path, [
        _ask(0), _note_line(_coach_text("cache_cold"), "UserPromptSubmit", 1), _turn(2),
        _ask(3), _note_line(cat.tool_note_text("big_output") + "\n" + _coach_text("quiet_output"), "PostToolUse", 4),
        _turn(5), _turn(6),
    ], kind="top-level")
    use = capture.coaching_usage(NS(sessions=[NS(top=top, subs=[], session_id="s1")]), pricing)
    assert (use.sessions, use.notes) == (1, 2)
    assert use.by_kind == {"cache_cold": 1, "quiet_output": 1}
    assert use.cost > 0 and use.spend > use.cost and use.note_tokens > 0
    later = capture.coaching_usage(NS(sessions=[NS(top=top, subs=[], session_id="s1")]), pricing,
                                   since="2026-09-18T12:00:03+00:00")
    assert later.by_kind == {"quiet_output": 1}
    # Capture's own usage prices only its part.
    assert capture.usage(NS(sessions=[NS(top=top, subs=[], session_id="s1")]), pricing).notes == 1


# -- coaching.json ------------------------------------------------------------------


def _split_rec(agent: str, every_n: int) -> Recommendation:
    return Recommendation(id="run-split", title=f"Give {agent} smaller tasks", agent_type=agent,
                          key=f"run-split:{agent}", evidence=[("Split every (replies)", every_n, "run_split.x", agent)])


def _report(*recs) -> NS:
    return NS(recommendations=list(recs), sections=[])


def test_the_file_holds_the_split_points_you_have_not_ignored(tmp_path):
    ignored = _split_rec("Plan", 50)
    ignores.set_ignored(tmp_path, [ignored], ignored=True, project=None)
    data = coaching.from_report(_report(_split_rec("general-purpose", 150), ignored), tmp_path, now=NOW)
    assert data["split_run"] == {"general-purpose": 150}
    assert data["plan_fresh"] is True
    assert data["thresholds"] == {"plan_fresh_tokens": 40_000}
    assert data["built_at"] == NOW.isoformat(timespec="seconds")
    configured = coaching.from_report(_report(), tmp_path, {"plan_handoff_min_dropped_tokens": 60_000})
    assert configured["thresholds"]["plan_fresh_tokens"] == 60_000


def test_an_ignored_plan_tip_turns_the_plan_hint_off(tmp_path):
    rec = Recommendation(id="plan-handoff", title="Start building in a fresh session", key="plan-handoff")
    ignores.set_ignored(tmp_path, [rec], ignored=True, project=None)
    assert coaching.from_report(_report(rec), tmp_path)["plan_fresh"] is False


def test_the_file_round_trips_and_says_how_old_it_is(tmp_path):
    written = coaching.write(tmp_path, coaching.from_report(_report(_split_rec("Explore", 75)), tmp_path, now=NOW))
    assert written == coaching.path(tmp_path)
    assert coaching.read(tmp_path)["split_run"] == {"Explore": 75}
    assert coaching.age_hours(tmp_path, NOW + timedelta(hours=3)) == pytest.approx(3)
    assert "Explore (every 75 replies)" in coaching.describe(coaching.read(tmp_path))[0]
    assert coaching.read(tmp_path / "missing") == {} and coaching.age_hours(tmp_path / "missing") is None


def test_the_service_job_rewrites_the_file_once_a_day_while_coaching_notes_are_on(tmp_path):
    config_dir = _config_dir(tmp_path)
    built = []

    def build(days):
        built.append(days)
        return _report(_split_rec("general-purpose", 100))

    clock = [NOW]
    job = CoachingJob(NS(config_dir=config_dir), build, now_fn=lambda: clock[0], log=lambda text: None)
    assert job.run_once() is None and built == []
    (config_dir / "config.toml").write_text('[capture]\ncoaching = ["coaching_notes"]\n', encoding="utf-8")
    assert job.run_once() == coaching.path(config_dir) and built == [coaching.DAYS]
    clock[0] = NOW + timedelta(hours=5)
    assert job.run_once() is None and built == [coaching.DAYS]
    clock[0] = NOW + timedelta(hours=coaching.MAX_AGE_HOURS + 1)
    assert job.run_once() == coaching.path(config_dir) and len(built) == 2


def test_a_failing_build_is_logged_and_never_raises(tmp_path):
    config_dir = _config_dir(tmp_path)
    (config_dir / "config.toml").write_text('[capture]\ncoaching = ["coaching_notes"]\n', encoding="utf-8")
    logged = []

    def build(days):
        raise RuntimeError("store is busy")

    job = CoachingJob(NS(config_dir=config_dir), build, now_fn=lambda: NOW, log=logged.append)
    assert job.run_once() is None and "store is busy" in logged[0]


# -- the capture command ---------------------------------------------------------------


@pytest.fixture()
def _claude_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setattr(installer, "is_registered", lambda *a, **k: False)
    config_dir = tmp_path / "claude" / "claudeglass"
    config_dir.mkdir(parents=True)
    return config_dir


def _capture(config_dir, *argv, stdin=""):
    args = cli._make_parser().parse_args(["capture", *argv, "--config-dir", str(config_dir)])
    out = io.StringIO()
    rc = cli._cmd_capture(args, stdin=io.StringIO(stdin), stdout=out, now=NOW)
    return rc, out.getvalue()


def test_turning_coaching_notes_on_says_what_they_cost_and_asks(_claude_folder):
    config_dir = _claude_folder
    rc, out = _capture(config_dir, "enable", "coaching_notes", stdin="n\n")
    assert rc == 1 and "Coaching notes:" in out and "Left unchanged" in out
    assert "Metrics capture: Off -> Off" not in out
    rc, out = _capture(config_dir, "enable", "coaching_notes", stdin="y\ny\n")
    assert rc == 0 and load_config(config_dir).capture.coaching_notes_on
    specs = hook_health.capture_specs(load_config(config_dir).capture.hook_metrics())
    assert hook_health.check_capture(specs, claude_root=config_dir.parent, config_dir=config_dir).ok


def test_off_leaves_coaching_notes_on_and_remove_turns_them_off(_claude_folder):
    config_dir = _claude_folder
    _capture(config_dir, "on", "--yes")
    _capture(config_dir, "enable", "coaching_notes", "--yes")
    rc, out = _capture(config_dir, "off")
    assert rc == 0 and "Coaching notes stay on" in out
    assert load_config(config_dir).capture.coaching_notes_on
    rc, out = _capture(config_dir, "remove", "--yes")
    assert rc == 0 and not load_config(config_dir).capture.coaching_notes_on


# -- what the dashboard says about them -------------------------------------------------


def test_the_footprint_says_coaching_notes_cost_tokens_with_capture_off():
    from claudeglass import footprint
    from claudeglass.config import CaptureConfig

    coach = footprint.expectations(CaptureConfig(coaching=["coaching_notes"]))
    assert coach[0] == ("It uses a few of your Claude tokens while coaching notes are on", footprint.COACHING_COST)
    assert coach[1:] == footprint.EXPECTATIONS[1:]
    both = footprint.expectations(CaptureConfig(level="free", coaching=["coaching_notes"]))
    assert "adds no tokens" in both[0][1] and both[0][1].endswith(footprint.COACHING_COST)
    assert footprint._hooks_token_cost(CaptureConfig(coaching=["coaching_notes"]), "Off").startswith(
        "None from capture while it's off. Coaching notes:"
    )


def test_the_hook_list_says_when_the_coaching_entries_run():
    specs = {spec.event: spec for spec in hook_health.capture_specs(("coaching_notes",))}
    assert specs["UserPromptSubmit"].describe() == "capture-hook.py when you send a message"
    assert specs["PostToolUse"].describe().endswith("MCP results and an approved plan")


def test_setup_capture_shows_what_coaching_notes_cost(tmp_path):
    from claudeglass import capture_view
    from claudeglass.config import CaptureConfig

    use = capture.CoachingUsage(since="", sessions=2, notes=3, note_tokens=240, cost=0.02, by_kind={"quiet_output": 3})
    data = capture_view.view(CaptureConfig(coaching=["coaching_notes"]), coaching_use=use)
    row = next(r for s in data["sections"] for r in s["metrics"] if r["id"] == "coaching_notes")
    assert row["on"] and row["actual"]["usd"] == pytest.approx(0.02)
    assert row["actual_label"] == f"3 notes over the last {capture.HISTORY_DAYS} days"


def test_refresh_works_the_split_points_out_now(_claude_folder):
    config_dir = _claude_folder
    rc, out = _capture(config_dir, "refresh", "--dry-run")
    assert rc == 0 and not coaching.path(config_dir).exists()
    rc, out = _capture(config_dir, "refresh")
    assert rc == 0 and coaching.read(config_dir)["split_run"] == {}
    assert "coaching_notes" in out


# -- what a reply says: a closing question, an admission, a disowned tip -----------

_TIP = "> **ClaudeGlass tip:** Try plan mode (is it on?)\n> and a second line?"
_REMINDER = "> **ClaudeGlass:** Finished? Run /cg-feedback: a few ticks make your savings tips fit how you work."


@pytest.mark.parametrize("text", [
    "Which database do you want?",
    "Done. Should I also update the docs?",
    "Done.\n\nWant me to carry on?\n\n[cg: task=feature size=m]",
    "Two options.\n- Should I keep the old name?\n- Should I drop the flag?",
    "1. Which file?\n2. Which line?",
    "Do you want **option A?**",
    "Should I continue?\n\n" + _TIP,
    "Should I continue?\n\n" + _REMINDER + "\n\n[cg: task=chat]",
])
def test_a_reply_that_ends_on_a_question_to_you_asks_one(text):
    assert prompt_shape.ends_on_question(text)


@pytest.mark.parametrize("text", [
    "",
    "Fixed it.",
    "Run `grep 'a?'` and it works.",
    "Done.\n```\nwhat?\n```",
    "Here you go:\n```py\nx = 1  # ok?",
    "See https://example.com/a?",
    # Rhetorical, four sentences back.
    "Why did it fail? The cache was cold. I cleared it. Then I reran the suite. All green now.",
    'He asked "is it done?" and I said yes.',
    # Only a ClaudeGlass tip or the tag carries the question mark.
    "Done.\n\n" + _TIP,
    "Done.\n\n" + _REMINDER,
    "Done.\n\n[cg: task=chat]",
])
def test_a_question_mark_in_code_a_link_a_tip_or_far_back_is_not_a_question(text):
    assert not prompt_shape.ends_on_question(text)


def test_only_the_end_of_a_huge_reply_is_read_for_a_question():
    assert prompt_shape.ends_on_question("filler. " * 5_000 + "Which one?")
    assert not prompt_shape.ends_on_question("Which one? " + "filler. " * 5_000)


@pytest.mark.parametrize("text", [
    "I was wrong about the path.",
    "My mistake, I misread the config.",
    "Good catch, I missed that file.",
    "You're right, I should have checked first.",
    "You’re right, that was my mistake.",
    "I didn't run the tests before saying it passed.",
    "I got that wrong.",
])
def test_an_admission_is_first_person_and_past_tense(text):
    assert prompt_shape.admits_mistake(text)


@pytest.mark.parametrize("text", [
    "Good catch.",
    "That's a good point about caching.",
    "I should have a look at the logs.",
    "The test was wrong.",
    "Fine.\n```\nI was wrong\n```",
    "Done.\n\n> **ClaudeGlass tip:** my mistake is not yours",
    "x" * 700 + " I was wrong",
])
def test_thanks_for_the_catch_a_third_person_fault_code_and_a_late_mention_are_no_admission(text):
    assert not prompt_shape.admits_mistake(text)


@pytest.mark.parametrize("text", [
    "That ClaudeGlass tip was a false positive; I had already planned it.",
    "The ClaudeGlass tip doesn't apply here.",
    "ClaudeGlass misfired on this one.",
    "The ClaudeGlass tip is not relevant to this change.",
])
def test_a_reply_that_calls_a_claudeglass_tip_a_misfire_disowns_it(text):
    assert prompt_shape.disowns_tip(text)


@pytest.mark.parametrize("text", [
    "That was a false positive.",
    "ClaudeGlass is installed. " + "x " * 100 + "a false positive.",
    "Done.\n\n> **ClaudeGlass tip:** this may be a false positive",
])
def test_a_false_positive_away_from_claudeglass_or_inside_the_tip_is_not_a_disowned_tip(text):
    assert not prompt_shape.disowns_tip(text)


def test_the_hook_finds_a_closing_question_as_the_package_does():
    texts = [
        "Which database do you want?", "Done. Should I also update the docs?", "Fixed it.", "See https://example.com/a?",
        "Two options.\n- Should I keep the old name?\n- Should I drop the flag?", "Should I continue?\n\n" + _TIP,
        "Done.\n\n" + _REMINDER, "Done.\n\n[cg: task=chat]", "Why did it fail? The cache was cold. I cleared it. Then I reran.",
        "Do you want **option A?**", 'He asked "is it done?" and I said yes.', "Here you go:\n```py\nx = 1  # ok?", "",
        "filler. " * 5_000 + "Which one?", "Which one? " + "filler. " * 5_000,
    ]
    for text in texts:
        assert HOOK._ends_on_question(text) == prompt_shape.ends_on_question(text), text[:40]


def test_the_hook_reads_a_go_ahead_as_the_package_does():
    coaching = HOOK.load_catalogue()["coaching"]
    texts = [
        "continue", "Go ahead.", "implement the plan", "merge it", "yes, do it", "ok", "do it please",
        "continue with the tests", "how is it going?", "", "x" * 70,
    ]
    for text in texts:
        assert HOOK._is_go(text, coaching) == prompt_shape.is_go(text), text


def test_the_hook_and_the_status_line_read_a_message_as_an_answer_by_the_same_rule():
    from claudeglass import statusline

    def said(text):
        return [
            {"type": "assistant", "timestamp": "2026-09-18T12:00:00.000Z",
             "message": {"id": "m1", "content": [{"type": "text", "text": text}]}},
            {"type": "user", "timestamp": "2026-09-18T12:00:30.000Z", "origin": {"kind": "human"},
             "message": {"role": "user", "content": "the second one"}},
        ]

    for text, answer in [("Which one?", True), ("Which one? I'd pick the first. It is simpler. Done.", False),
                         ("Use `a?b` here.", False)]:
        assert HOOK._exchanges(said(text), cat.INTERRUPT_PREFIX, ())[0][0]["answer"] is answer
        assert statusline._exchanges(said(text), cat.INTERRUPT_PREFIX, ())[0]["answer"] is answer


def test_the_catalogue_carries_the_reply_patterns_the_hook_compiles():
    coaching = CATALOGUE["coaching"]
    assert coaching["reply_scan_chars"] == cat.REPLY_SCAN_CHARS
    assert coaching["reply_unit_pattern"] == cat.REPLY_UNIT_PATTERN
    assert coaching["reply_question_trim"] == cat.REPLY_QUESTION_TRIM
    for name in ("fence", "tip_block", "inline_code", "url", "quoted", "reminder_line", "tags", "list_start", "unit"):
        assert coaching[f"reply_{name}_pattern"] == getattr(cat, f"REPLY_{name.upper()}_PATTERN")


# -- what the parser keeps of a reply ----------------------------------------------


def _line_at(second: int) -> str:
    return f"2026-09-18T12:00:{second:02d}.000Z"


def _human(text: str, second: int, **extra) -> dict:
    return user_str_line(text, origin={"kind": "human"}, timestamp=_line_at(second), **extra)


def _said_back(text: str, second: int, *more: str) -> dict:
    return turn_line(content=[{"type": "text", "text": block} for block in (text, *more)], model="claude-widget-9",
                     timestamp=_line_at(second), cache_read_input_tokens=5_000)


def test_a_reply_that_ends_on_a_question_is_marked_asked(tmp_path):
    result = _session(tmp_path, [
        _human("add a flag", 0), _said_back("Which name do you want for it?", 1),
        _human("--quiet", 2), _said_back("Done. See https://example.com/docs?", 3),
        _human("thanks", 4), _said_back("Is it fine? I think so.", 5, "Anything else?"),
    ])
    assert [t.reply_asked for t in result.turns] == [True, False, True]


def test_a_reply_asks_by_its_last_text_block_not_an_earlier_one(tmp_path):
    result = _session(tmp_path, [
        _human("go", 0), _said_back("Should I check the docs first?", 1, "I checked them. All fine."),
    ])
    assert not result.turns[0].reply_asked


def test_an_admission_after_your_push_back_is_caught_by_you_and_after_none_by_itself(tmp_path):
    result = _session(tmp_path, [
        _human("rename the helper", 0), _said_back("Done.", 1),
        _human("that's wrong, it should keep the old name", 2), _said_back("You're right, I misread it.", 3),
        _human("continue", 4), _said_back("Careful: I was wrong earlier about the cache.", 5),
        _human("why did you remove the flag?", 6), _said_back("My mistake, I removed it by accident.", 7),
        _human("looks good", 8), _said_back("Thanks.", 9),
    ])
    assert [t.admit_candidate for t in result.turns] == [False, True, True, True, False]
    assert [t.admit_caught for t in result.turns] == ["", "user", "self", "user", ""]


def test_a_slash_command_or_a_go_ahead_between_is_no_push_back(tmp_path):
    command = "<command-message>grill-me</command-message>\n<command-name>/grill-me</command-name>"
    result = _session(tmp_path, [
        _human("that's wrong", 0), _said_back("Done.", 1),
        _human(command, 2), _said_back("I was wrong about that.", 3),
        _human("why is it slow?", 4), _said_back("Done.", 5),
        _human("go ahead", 6), _said_back("I missed that file.", 7),
    ])
    assert [t.admit_caught for t in result.turns] == ["", "self", "", "self"]


def test_the_reply_after_a_queued_correction_follows_a_push_back(tmp_path):
    queued = attachment_line("queued_command", prompt="no, that's not what I asked", commandMode="prompt",
                             origin={"kind": "human"}, timestamp=_line_at(1))
    result = _session(tmp_path, [_human("rename the helper", 0), queued, _said_back("My mistake.", 2)])
    assert result.turns[0].admit_caught == "user"


def test_a_tip_the_reply_disowns_is_marked_and_a_plain_misfire_mention_is_not(tmp_path):
    tip = "> **ClaudeGlass tip:** Say what you saw and what you expected."
    result = _session(tmp_path, [
        _human("it's broken", 0), _said_back("Fixed.\n\n" + tip, 1, "That ClaudeGlass tip was a false positive."),
        _human("again", 2), _said_back("Fixed. That ClaudeGlass tip was a false positive.", 3),
        _human("again", 4), _said_back("Fixed.\n\n" + tip, 5),
    ])
    assert [t.tip_disowned for t in result.turns] == [True, False, False]
    assert [t.coach_tip for t in result.turns] == [False, False, True]

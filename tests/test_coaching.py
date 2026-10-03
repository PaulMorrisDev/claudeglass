"""Coaching notes (``coaching_notes``): the capture hook's live hints, how
the parser finds them again and prices them, the ``coaching.json`` split
points worked out from a report, the service job that keeps it fresh, and
the ``capture`` command around them.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
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
MODULE = SCRIPT.with_name(cat.HOOK_MODULE)
FIXTURES = Path(__file__).resolve().parent / "fixtures"
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


def _load_hook_module():
    spec = importlib.util.spec_from_file_location("_coaching_hook_under_test", MODULE)
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


def _coach(tmp_path, payload, config=ON, *, now=NOW, result_len=None) -> str:
    base = {"session_id": "s1", "cwd": "/work/app"}
    return HOOK.coaching_note_for({**base, **payload}, config, CATALOGUE, _config_dir(tmp_path), now=now, result_len=result_len)


def _prompt_payload(path: str) -> dict:
    return {"hook_event_name": "UserPromptSubmit", "transcript_path": path, "prompt": "next"}


def _kind(note: str) -> str:
    assert note.startswith(cat.COACH_MARKER + str(cat.COACH_VERSION) + " "), note
    return note.split("\n", 1)[0].rsplit(" ", 1)[1]


def test_nothing_is_added_while_coaching_notes_are_off(tmp_path):
    path = _transcript(tmp_path, [_prompt(), _reply(150_000)])
    assert _coach(tmp_path, _prompt_payload(path), {"capture": {"level": "deep"}}) == ""
    assert _coach(tmp_path, _prompt_payload(path), {}) == ""


def test_a_large_context_gets_no_hint_when_you_send_a_message(tmp_path):
    """The /clear note is dropped: it applied to almost every message. What
    a new piece carried over is a row on the prompting page instead."""
    path = _transcript(tmp_path, [_prompt(), _reply(120_000, one_hour=True)])
    assert _coach(tmp_path, _prompt_payload(path)) == ""
    assert "clear_context" not in cat.COACHING_HINTS and "clear_context" in cat.RETIRED_COACHING_HINTS


def test_a_return_after_the_cache_expired_gets_the_cold_return_receipt(tmp_path):
    path = _transcript(tmp_path, [_prompt(), _reply(150_000, ago_s=20 * 60)])
    note = _coach(tmp_path, _prompt_payload(path))
    # 150k less the 42k every session starts with.
    assert _kind(note) == "cold_return" and "idle for 20 minutes" in note and "about 108k tokens" in note
    # The receipt sits on the note whatever the message is: a new task or a question alike.
    assert _kind(_coach(tmp_path, {**_prompt_payload(path), "session_id": "s2", "prompt": "why is the sky blue?"})) == "cold_return"
    # The 1-hour cache is still warm after 20 minutes.
    warm = _transcript(tmp_path, [_prompt(), _reply(150_000, ago_s=20 * 60, one_hour=True)], "warm.jsonl")
    assert _coach(tmp_path, {**_prompt_payload(warm), "session_id": "s3"}) == ""
    # Too small a context to mention.
    small = _transcript(tmp_path, [_prompt(), _reply(90_000, ago_s=20 * 60)], "small.jsonl")
    assert _coach(tmp_path, {**_prompt_payload(small), "session_id": "s4"}) == ""


def test_the_cold_return_receipt_says_what_to_do_and_names_no_one_of_your_words(tmp_path):
    path = _transcript(tmp_path, [_prompt("secret plan"), _reply(150_000, ago_s=3 * 3600)])
    note = _coach(tmp_path, _prompt_payload(path))
    assert "idle for 3 hours" in note
    assert "last reply or the task panel" in note and "/clear" in note
    assert "secret" not in note


def test_the_cold_return_receipt_rests_half_a_day_however_the_context_grows(tmp_path):
    cold = 20 * 60
    path = _transcript(tmp_path, [_prompt(), _reply(120_000, ago_s=cold)])
    assert _kind(_coach(tmp_path, _prompt_payload(path))) == "cold_return"
    assert _coach(tmp_path, _prompt_payload(path)) == ""
    # Another session has its own rest.
    assert _coach(tmp_path, {**_prompt_payload(path), "session_id": "s2"})
    # A context twice the size doesn't wake it: it is a receipt for one break, not a growing stake.
    grown = _transcript(tmp_path, [_prompt(), _reply(400_000, ago_s=cold)], "grown.jsonl")
    assert _coach(tmp_path, _prompt_payload(grown)) == ""
    hours = cat.COACHING_THRESHOLDS["cold_rest_hours"]
    assert _coach(tmp_path, _prompt_payload(path), now=NOW + timedelta(hours=hours - 1)) == ""
    assert _kind(_coach(tmp_path, _prompt_payload(path), now=NOW + timedelta(hours=hours, minutes=1))) == "cold_return"


def test_a_hint_rests_after_it_shows_unless_the_stake_grows(tmp_path):
    grep = {"hook_event_name": "PostToolUse", "tool_name": "Grep", "tool_input": {"pattern": "ls"}}
    assert _kind(_coach(tmp_path, grep, result_len=40_000)) == "quiet_output"
    assert _coach(tmp_path, grep, result_len=40_000) == ""
    # Another session has its own rest.
    assert _coach(tmp_path, {**grep, "session_id": "s2"}, result_len=40_000)
    # A result 1.5 times as long wakes it.
    assert _kind(_coach(tmp_path, grep, result_len=60_000)) == "quiet_output"
    assert _coach(tmp_path, grep, result_len=40_000) == ""
    later = NOW + timedelta(minutes=cat.COACHING_THRESHOLDS["cooldown_minutes"] * 2 + 1)
    assert _kind(_coach(tmp_path, grep, now=later, result_len=40_000)) == "quiet_output"


def test_a_hint_that_keeps_coming_back_rests_twice_as_long_each_time(tmp_path):
    grep = {"hook_event_name": "PostToolUse", "tool_name": "Grep", "tool_input": {"pattern": "ls"}}
    cooldown = cat.COACHING_THRESHOLDS["cooldown_minutes"]
    clock = NOW
    for shown in range(1, 5):
        assert _kind(_coach(tmp_path, grep, now=clock, result_len=40_000)) == "quiet_output"
        rest = cooldown * 2 ** min(shown - 1, cat.COACHING_THRESHOLDS["max_backoff"])
        # Not a minute before the rest is over; at it, the hint shows again.
        assert _coach(tmp_path, grep, now=clock + timedelta(minutes=rest - 1), result_len=40_000) == ""
        clock += timedelta(minutes=rest)
    state = json.loads((tmp_path / "claudeglass" / cat.COACH_STATE_FILE).read_text(encoding="utf-8"))
    (row,) = state["sessions"].values()
    assert row["hints"]["quiet_output"]["n"] == 4


def test_the_state_keeps_a_session_by_its_salted_hash(tmp_path):
    config_dir = _config_dir(tmp_path)
    salt = b"s" * 32
    (config_dir / HOOK.SALT_FILE).write_bytes(salt)
    _coach(tmp_path, _prompt_payload(_transcript(tmp_path, [_prompt(), _reply(120_000, ago_s=20 * 60)])))
    state = json.loads((config_dir / cat.COACH_STATE_FILE).read_text(encoding="utf-8"))
    assert list(state["sessions"]) == [HOOK.session_hash(salt, "s1")]


def test_a_large_result_gets_the_quiet_hint_for_its_tool(tmp_path):
    grep = {"hook_event_name": "PostToolUse", "tool_name": "Grep", "tool_input": {"pattern": "ls"}}
    note = _coach(tmp_path, grep, result_len=40_000)
    assert _kind(note) == "quiet_output" and "about 10k tokens" in note and cat.COACHING_QUIET_HOW["Grep"] in note
    fetch = {"hook_event_name": "PostToolUse", "tool_name": "WebFetch", "session_id": "s2"}
    assert cat.COACHING_QUIET_HOW[""] in _coach(tmp_path, fetch, result_len=40_000)
    assert _coach(tmp_path, {**grep, "session_id": "s3"}, result_len=4_000) == ""
    limited = {"hook_event_name": "PostToolUse", "tool_name": "Read", "tool_input": {"limit": 2000}, "session_id": "s4"}
    assert _coach(tmp_path, limited, result_len=40_000) == ""


def _result(tool: str, response, **extra) -> dict:
    return {"hook_event_name": "PostToolUse", "tool_name": tool, "tool_response": response,
            "tool_input": {}, "session_id": "s1", **extra}


def _read_text(chars: int) -> dict:
    return {"type": "text", "file": {"filePath": "/w/a.py", "content": "x" * chars, "numLines": 1}}


def _read_image(*, base64_chars: int = 400_000, dimensions=None) -> dict:
    file = {"type": "image/png", "base64": "A" * base64_chars, "originalSize": base64_chars}
    if dimensions is not None:
        file["dimensions"] = dimensions
    return {"type": "image", "file": file}


def test_the_size_counts_the_words_claude_reads_and_nothing_only_claude_code_shows():
    measure = lambda tool, response: HOOK.result_chars({"tool_name": tool, "tool_response": response}, CATALOGUE)
    assert measure("Read", _read_text(12_345)) == 12_345
    assert measure("Grep", {"mode": "content", "content": "c" * 900, "filenames": ["a.py"] * 50, "numLines": 9}) == 900
    assert measure("Grep", {"mode": "files_with_matches", "filenames": ["abc", "de"], "numFiles": 2}) == 4 + 3
    assert measure("WebFetch", {"result": "r" * 700, "url": "u" * 30, "bytes": 99_999, "code": 200}) == 730
    assert measure("Glob", {"filenames": ["a/b.py"], "numFiles": 1, "truncated": False}) == 7
    assert measure("mcp__docs__search", [{"type": "text", "text": "t" * 500}, {"type": "text", "text": "u" * 10}]) == 510
    assert measure("Read", "an error message") == len("an error message")
    # What Claude Code keeps for its own display never counts, however big.
    assert measure("Bash", {"stdout": "o" * 100, "stderr": "e" * 20, "bashEditDiff": {"diff": "d" * 90_000}}) == 120
    assert measure("Edit", {"originalFile": "f" * 90_000, "structuredPatch": [{"lines": ["l" * 90_000]}]}) == 0
    assert measure("Read", None) == 0 and measure("Read", {"file": {}}) == 0


def test_a_picture_counts_for_its_tokens_never_its_encoding():
    measure = lambda response: HOOK.result_chars({"tool_name": "Read", "tool_response": response}, CATALOGUE)
    cap = cat.RESULT_IMAGE_MAX_TOKENS * 4
    # 1568 x 1000 is 56 x 36 patches of 28 pixels: over the cap, so the cap.
    big = {"originalWidth": 4000, "originalHeight": 2500, "displayWidth": 1568, "displayHeight": 1000}
    assert measure(_read_image(dimensions=big)) == cap
    # A small picture counts for its patches: 10 x 10 of them.
    assert measure(_read_image(dimensions={"originalWidth": 280, "originalHeight": 280})) == 100 * 4
    # One without a size counts for the most it can.
    assert measure(_read_image()) == cap
    # An MCP image block counts the same, beside its text.
    blocks = [{"type": "text", "text": "t" * 40}, {"type": "image", "source": {"type": "base64", "data": "B" * 500_000}}]
    assert HOOK.result_chars({"tool_name": "mcp__shot__take", "tool_response": blocks}, CATALOGUE) == 40 + cap


def test_a_picture_alone_gets_no_hint_but_words_beside_it_still_count(tmp_path):
    threshold = cat.COACHING_THRESHOLDS["quiet_output_tokens"] * 4
    picture = _read_image(dimensions={"displayWidth": 1568, "displayHeight": 1000})
    assert _coach(tmp_path, _result("Read", picture)) == ""
    # Words just under the threshold are pushed over it by the picture that comes with them.
    mixed = [{"type": "text", "text": "x" * (threshold - 100)}, {"type": "image", "source": {"data": "B"}}]
    assert _kind(_coach(tmp_path, _result("mcp__shot__take", mixed, session_id="s2"))) == "quiet_output"
    assert _coach(tmp_path, _result("mcp__shot__take", mixed[:1], session_id="s3")) == ""


def test_a_result_saved_to_a_file_counts_for_its_preview_only(tmp_path):
    limit = cat.RESULT_PERSIST_CHARS
    preview = cat.RESULT_PREVIEW_CHARS
    measure = lambda tool, response: HOOK.result_chars({"tool_name": tool, "tool_response": response}, CATALOGUE)
    # Past its tool's limit Claude Code keeps a preview in context.
    assert measure("Grep", {"mode": "content", "content": "c" * (limit["Grep"] + 1)}) == preview
    assert measure("Grep", {"mode": "content", "content": "c" * limit["Grep"]}) == limit["Grep"]
    assert measure("WebFetch", {"result": "r" * (limit["WebFetch"] + 1)}) == preview
    assert measure("WebFetch", {"result": "r" * 40_000}) == 40_000
    # A result that says where it was saved counts for the preview, whatever its size.
    saved = {"stdout": "o" * 5_000, "persistedOutputPath": "/tmp/x.txt", "persistedOutputSize": 90_000}
    assert measure("Bash", saved) == preview
    assert measure("PowerShell", {"stdout": "o" * 40_000}) == preview
    # A read is never saved, so a long one counts whole.
    assert measure("Read", _read_text(62_000)) == 62_000
    # So a saved search is no big result, and a fetch under its limit is.
    saved_search = _result("Grep", {"mode": "content", "content": "c" * 60_000})
    assert _coach(tmp_path, saved_search) == ""
    assert _kind(_coach(tmp_path, _result("WebFetch", {"result": "r" * 40_000}, session_id="s2"))) == "quiet_output"
    assert _coach(tmp_path, _result("WebFetch", {"result": "r" * 60_000}, session_id="s3")) == ""


def test_the_main_session_and_each_subagent_rest_apart(tmp_path):
    main = _result("Read", _read_text(40_000))
    assert _kind(_coach(tmp_path, main)) == "quiet_output"
    assert _coach(tmp_path, main) == ""
    # A subagent's big result isn't silenced by the main session's, and the other way round.
    agent = {**main, "agent_id": "abc", "agent_type": "general-purpose"}
    assert _kind(_coach(tmp_path, agent)) == "quiet_output"
    assert _coach(tmp_path, agent) == ""
    assert _kind(_coach(tmp_path, {**agent, "agent_id": "def"})) == "quiet_output"
    state = json.loads((tmp_path / "claudeglass" / cat.COACH_STATE_FILE).read_text(encoding="utf-8"))
    (row,) = state["sessions"].values()
    assert set(row["hints"]) == {"quiet_output", "quiet_output:abc", "quiet_output:def"}
    # The marker names the hint, not the context it was for.
    other = _coach(tmp_path, {**main, "session_id": "s2", "agent_id": "ghi", "agent_type": "general-purpose"})
    assert other.split("\n", 1)[0] == cat.COACH_MARKER + str(cat.COACH_VERSION) + " quiet_output"


def test_many_reads_for_one_message_get_no_hint(tmp_path):
    """The Explore note is dropped: most Explore runs here ran on the
    largest model, and the message that read a lot usually edited too, so it
    needed those reads. The habits page shows Explore cost by model."""
    earlier = [_read_call(n) for n in range(90, 95)]
    calls = [record for n in range(1, 8) for record in (_read_call(n), _read_result(n))]
    path = _transcript(tmp_path, [_prompt("first"), *earlier, _prompt("second"), *calls, _read_call(8)])
    read = {"hook_event_name": "PostToolUse", "tool_name": "Read", "tool_use_id": "toolu_8", "transcript_path": path}
    assert _coach(tmp_path, read, result_len=400) == ""
    indexed = tmp_path / "indexed"
    (indexed / ".tokensave").mkdir(parents=True)
    assert _coach(tmp_path, {**read, "cwd": str(indexed), "session_id": "s2"}, result_len=400) == ""
    assert not hasattr(HOOK, "_tokensave_indexed") and not hasattr(HOOK, "_reads_hint")


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


def _planning(tmp_path, records=None, *, mode: str | None = "plan", session: str = "s1", name: str = "plan.jsonl",
              now=NOW) -> str:
    """A message sent after 75k tokens of planning chat (90k less the 15k a
    fresh session starts with, which is 16k less the first message's 1k)."""
    records = records if records is not None else [_prompt("x" * 4_000), _reply(16_000), _reply(90_000)]
    payload = {"hook_event_name": "UserPromptSubmit", "transcript_path": _transcript(tmp_path, records, name),
               "prompt": "and an admin screen too", "session_id": session}
    return _coach(tmp_path, {**payload, "permission_mode": mode} if mode else payload, now=now)


def test_planning_for_a_long_time_in_plan_mode_gets_the_clear_context_hint(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    note = _planning(tmp_path)
    assert _kind(note) == "plan_fresh_early"
    lines = note.splitlines()
    # The order to write the tip leads, and says where: ending the plan Claude will submit.
    assert lines[1].startswith("Write the tip on this note's last line, word for word, as the last line of the plan")
    assert lines[-1].startswith(cat.TIP_LABEL + " Approve with clear context: this session holds about 75k tokens")
    # Counts only: nothing of the message or the session.
    assert "admin screen" not in note and "xxxx" not in note


@pytest.mark.parametrize("entrypoint, how", [
    ("claude-desktop", cat.COACHING_HOW["clear_how"]["desktop"]),
    ("cli", cat.COACHING_HOW["clear_how"]["terminal"]),
    ("", cat.COACHING_HOW["clear_how"]["terminal"]),
])
def test_the_clear_context_hint_names_the_way_to_clear_in_the_app_you_use(tmp_path, monkeypatch, entrypoint, how):
    if entrypoint:
        monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", entrypoint)
    else:
        monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    note = _planning(tmp_path)
    assert note.endswith(how)
    # The built-in option first, with /clear and the plan's file as the way out, on the desktop.
    assert ("approval option" in how) == (entrypoint == "claude-desktop")
    assert "/clear" in how and "implement the plan in" in how


def test_the_clear_context_hint_needs_plan_mode_a_lot_of_planning_and_a_known_start(tmp_path):
    # Not in plan mode, or no mode reported.
    assert _planning(tmp_path, mode="default", session="a") == ""
    assert _planning(tmp_path, mode="acceptEdits", session="b") == ""
    assert _planning(tmp_path, mode=None, session="c") == ""
    # A small planning context.
    small = [_prompt("x" * 4_000), _reply(16_000), _reply(50_000)]
    assert _planning(tmp_path, small, session="d") == ""
    # The first reply lies beyond the start of the transcript that is read: the start isn't known.
    beyond = [_prompt("x" * 600_000), _reply(16_000), _reply(90_000)]
    assert _planning(tmp_path, beyond, session="e") == ""
    # A compaction since the session started.
    compacted = [_prompt("x" * 4_000), _reply(16_000), {"type": "system", "subtype": "compact_boundary"},
                 _reply(90_000)]
    assert _planning(tmp_path, compacted, session="f") == ""
    # The same transcript with nothing in the way gets the hint.
    assert _kind(_planning(tmp_path, session="g")) == "plan_fresh_early"


def test_the_clear_context_hint_skips_a_message_sent_while_claude_is_working(tmp_path):
    working = [_prompt("x" * 4_000), _reply(16_000), _reply(90_000), _acted(30, "Read", {"file_path": "/w/a.py"}, say="")]
    assert _planning(tmp_path, working, session="a") == ""
    back = [*working, _came_back(30)]
    assert _planning(tmp_path, back, session="b") == ""


def test_the_clear_context_hint_follows_the_plan_fresh_setting(tmp_path):
    (_config_dir(tmp_path) / cat.COACHING_FILE).write_text(json.dumps({"plan_fresh": False}), encoding="utf-8")
    assert _planning(tmp_path) == ""


def test_the_clear_context_hint_and_the_fresh_session_hint_share_one_rest(tmp_path):
    assert _kind(_planning(tmp_path)) == "plan_fresh_early"
    # Said once: the same message again, and a typed approval of the plan that follows, add nothing.
    assert _planning(tmp_path, name="again.jsonl") == ""
    assert _typed_approval(tmp_path, "go ahead", _plan_call(), _plan_answer(is_error=True)) == ""
    # However far the context has grown, only the cooldown lifts the rest: the other hint stakes nothing.
    grown = _transcript(tmp_path, [_prompt("x" * 4_000), _reply(16_000), _reply(400_000)], "grown.jsonl")
    plan = {"hook_event_name": "PostToolUse", "tool_name": "ExitPlanMode", "transcript_path": grown,
            "tool_input": {"plan": "p" * 4_000}}
    assert _coach(tmp_path, plan) == ""
    later = NOW + timedelta(minutes=cat.COACHING_THRESHOLDS["cooldown_minutes"] + 1)
    assert _kind(_coach(tmp_path, plan, now=later)) == "plan_fresh"


def test_a_plan_approved_in_the_dialog_rests_the_clear_context_hint(tmp_path):
    path = _transcript(tmp_path, [_prompt("x" * 4_000), _reply(16_000), _reply(90_000)])
    plan = {"hook_event_name": "PostToolUse", "tool_name": "ExitPlanMode", "transcript_path": path,
            "tool_input": {"plan": "p" * 4_000}}
    assert _kind(_coach(tmp_path, plan)) == "plan_fresh"
    assert _planning(tmp_path, name="after.jsonl") == ""


def test_a_typed_approval_still_gets_the_fresh_session_hint_in_a_new_session(tmp_path):
    # The hook's earlier typed-approval path (Phase 1's ``approved_by_message``) is untouched.
    note = _typed_approval(tmp_path, "go ahead", _plan_call(), _plan_answer(is_error=True))
    assert _kind(note) == "plan_fresh"
    assert note.splitlines()[-1].startswith(cat.TIP_LABEL + " This plan was approved with about 74k tokens")


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


def _acted(ago_s: float, name: str, tool_input: dict, *, say: str = "Done.", ctx: int = 30_000) -> dict:
    """Claude's reply to a message: one ``name`` call with ``tool_input``,
    then ``say`` (nothing when empty). On the 1-hour cache."""
    content = [{"type": "tool_use", "id": f"toolu_a{ago_s}", "name": name, "input": tool_input}]
    if say:
        content.append({"type": "text", "text": say})
    return _reply(ctx, ago_s=ago_s, content=content, message_id=f"msg_a{ago_s}", one_hour=True)


def _came_back(call_ago_s: float, *, is_error: bool = False, edited_files: int = 0, background: bool = False) -> dict:
    """The line that brings the result of ``_acted(call_ago_s, ...)`` back;
    a subagent's also says how many files it changed (``background``: it
    went to the background, so the line is only its launch message)."""
    line = {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": f"toolu_a{call_ago_s}", "content": "no" if is_error else "ok",
         "is_error": is_error}]}}
    if edited_files or background:
        line["toolUseResult"] = {"toolStats": {"editFileCount": edited_files}}
    if background:
        line["toolUseResult"].update({"isAsync": True, "status": "async_launched"})
    return line


def _real(record: dict) -> dict:
    """``record`` as Claude Code writes it: a message you typed says so, and
    a reply names its model, which the parser needs and the hook doesn't."""
    if record["type"] == "user" and isinstance(record["message"]["content"], str):
        return {**record, "origin": {"kind": "human"}}
    if record["type"] == "assistant" and not record.get("isApiErrorMessage"):
        return {**record, "message": {**record["message"], "model": "claude-sonnet-5"}}
    return record


def _spoke(ago_s: float, say: str = "ok") -> dict:
    """A reply that only talks, with no tool call. On the 1-hour cache."""
    return _reply(30_000, ago_s=ago_s, one_hour=True, content=[{"type": "text", "text": say}])


#: A session's first message and Claude's work on it: the start of the
#: work, before any follow-up (the first message never counts as one).
_START = [_said("Build the settings page: " + "a form, a save button, a header, a footer. " * 8, 1_150),
          _changed(1_100)]


def test_small_requests_one_at_a_time_get_the_plan_it_as_one_prompt_hint(tmp_path):
    # No "fix" anywhere: what counts is that each short message asked for a change and got a file change.
    records = [
        _said("Build the settings page from the plan we agreed: " + "a form, a save button, a header. " * 10, 1_500),
        _changed(1_000),
        _said("make the save button bigger", 900), _changed(880, tool="Write"),
        _said("<command-name>/cost</command-name>", 700),
        _stopped(650, blocks=True),
        _said("now move the logo to the left", 600), _changed(580, tool="MultiEdit"),
    ]
    note = _send(tmp_path, records, "and make the footer text grey")
    assert _kind(note) == "drip_feed" and "sent 3 small change requests in a row" in note
    # The note carries counts, never your words.
    assert "footer" not in note and "logo" not in note
    # A message Claude Code has already written to the transcript isn't counted twice.
    written = [*records, _said("and make the footer text grey", 2)]
    assert "sent 3 small" in _send(tmp_path, written, "and make the footer text grey", session="s2")


def test_a_run_needs_file_changes_short_messages_and_quick_follow_ups(tmp_path):
    def run(first_reply, middle="now move the logo to the left", gap_s=20):
        return [*_START, _said("make the save button bigger", 900), first_reply,
                _said(middle, 600), _changed(600 - gap_s)]

    assert _kind(_send(tmp_path, run(_changed(880)), "and make the footer grey")) == "drip_feed"
    # A reply that changed nothing (Claude only answered) breaks the run.
    assert _send(tmp_path, run(_reply(30_000, ago_s=880, one_hour=True)), "and make the footer grey", session="s2") == ""
    # So does a detailed message that plans several changes at once.
    detailed = run(_changed(880), middle="Now: " + "move the logo left, grey footer, wider form; " * 8)
    assert _send(tmp_path, detailed, "and make the footer grey", session="s3") == ""
    # And a message sent long after your last one.
    spaced = [_said("Build the settings page: " + "a form and a header. " * 20, 9_000), _changed(8_900),
              _said("make the save button bigger", 8_800), _changed(8_700), _said("now move the logo", 8_600),
              _changed(8_500)]
    assert _send(tmp_path, spaced, "and make the footer grey", session="s4") == ""
    # Your own count wins.
    two = [*_START, _said("make the save button bigger", 900), _changed(880)]
    assert _send(tmp_path, two, "and make the footer grey", session="s5") == ""
    fewer = {**ON, "thresholds": {"coaching_drip_count": 2}}
    assert _kind(_send(tmp_path, two, "and make the footer grey", session="s6", config=fewer)) == "drip_feed"


def test_the_window_is_timed_between_your_messages_not_from_claudes_last_reply(tmp_path):
    prompt = "and make the footer grey"
    opening = [_said("Build the settings page: " + "a form and a header. " * 20, 4_000), _changed(3_900)]
    # The second message came 47 minutes after the first: too slow to join a run, however quick Claude was.
    slow = [*opening, _said("make the save button bigger", 3_800), _changed(3_700),
            _said("now move the logo", 1_000), _changed(900)]
    assert _send(tmp_path, slow, prompt) == ""
    # Your own window decides what is slow.
    wider = {**ON, "thresholds": {"coaching_drip_window_minutes": 60}}
    assert _kind(_send(tmp_path, slow, prompt, session="s2", config=wider)) == "drip_feed"
    # Claude's work took half an hour, but your messages came 7 and 17 minutes after the one before: a run.
    long = [_said("Build the settings page: " + "a form and a header. " * 20, 1_500), _changed(1_450),
            _said("make the save button bigger", 1_100), _changed(150), _said("now move the logo", 100), _changed(80)]
    assert _kind(_send(tmp_path, long, prompt, session="s3")) == "drip_feed"


def test_answers_to_claudes_questions_and_thanks_are_not_requests(tmp_path):
    asked = [*_START, _said("make the save button bigger", 900), _changed(880), _said("now move the logo", 600),
             _spoke(580, "Left or right of the title?")]
    # Answering Claude's question isn't another request, even when the answer reads as a change...
    assert _send(tmp_path, asked, "make it left of the title") == ""
    # ...and doesn't break a run either: the question followed a message that asked for nothing.
    answered = [*_START, _said("make the save button bigger", 900), _changed(880), _said("now move the logo", 800),
                _changed(780), _said("where should the logo go?", 600), _spoke(580, "Left or right of the title?"),
                _said("left of the title", 400), _spoke(380, "Got it.")]
    assert _kind(_send(tmp_path, answered, "and make the footer grey", session="s2")) == "drip_feed"
    done = [*_START, _said("make the save button bigger", 900), _changed(880), _said("now move the logo", 600),
            _changed(580)]
    for n, thanks in enumerate(("thanks", "ok, looks good!", "perfect, thank you")):
        assert _send(tmp_path, done, thanks, session=f"t{n}") == "", thanks


def test_a_question_closing_a_reply_that_changed_a_file_is_an_offer_not_one_you_answer(tmp_path):
    offered = [*_START, _said("make the save button bigger", 900), _changed(880), _said("now move the logo", 600),
               _changed(580, say="Moved it. Want me to move the footer too?")]
    # Claude changed the file and then offered more: the next message is a request like any other.
    assert _kind(_send(tmp_path, offered, "yes, move the footer too")) == "drip_feed"
    # A question that closes a reply that changed nothing is the one you answer.
    asked = [*offered[:-1], _spoke(580, "Want me to move the footer too?")]
    assert _send(tmp_path, asked, "yes, move the footer too", session="s2") == ""


@pytest.mark.parametrize("between", [
    "go ahead", "thanks!", "how is it going?", "why is the button blue?", "the button is too small",
    "it does not load", "explain how the header works",
])
def test_a_go_ahead_a_thank_you_a_status_check_a_question_and_a_statement_do_not_end_a_run(tmp_path, between):
    records = [*_START, _said("make the save button bigger", 900), _changed(880), _said(between, 700), _spoke(680),
               _said("now move the logo", 600), _changed(580)]
    assert _kind(_send(tmp_path, records, "and make the footer too")) == "drip_feed"


@pytest.mark.parametrize("prompt", [
    "go ahead", "continue", "how is it going?", "any update on the footer?", "why is the button blue?",
    "what does the footer do", "can you move the logo left?", "the footer is too small", "it does not load",
    "explain how the footer works", "and the footer too", "merge it",
])
def test_a_message_that_asks_for_no_change_is_not_one_more_small_request(tmp_path, prompt):
    done = [*_START, _said("make the save button bigger", 900), _changed(880), _said("now move the logo", 600),
            _changed(580)]
    assert _send(tmp_path, done, prompt) == "", prompt
    assert _kind(_send(tmp_path, done, "and make the footer too", session="s2")) == "drip_feed"


#: The reply to a request in a run of small ones, and whether it changed a
#: file of yours (a ``.claude`` folder is Claude's own, a failed edit changed nothing).
_THIRD_REPLIES = {
    "an edit": (lambda: [_acted(580, "Edit", {"file_path": "/work/app/app.css"})], True),
    "a shell write": (lambda: [_acted(580, "Bash", {"command": "sed -i 's/a/b/' app.css"})], True),
    "a redirect": (lambda: [_acted(580, "Bash", {"command": "echo hi > out.txt"})], True),
    "a subagent's edits": (
        lambda: [_acted(580, "Agent", {"prompt": "go"}, say=""), _came_back(580, edited_files=2), _spoke(570)], True),
    "an edit under .claude": (lambda: [_acted(580, "Edit", {"file_path": "/home/me/.claude/plans/plan.md"})], False),
    "a write under .claude": (lambda: [_acted(580, "Write", {"file_path": "C:/Users/me/.claude/memory/notes.md"})], False),
    "a shell write under .claude": (
        lambda: [_acted(580, "Bash", {"command": "echo hi > /home/me/.claude/notes.md"})], False),
    "a failed edit": (
        lambda: [_acted(580, "Edit", {"file_path": "/work/app/app.css"}, say=""), _came_back(580, is_error=True),
                 _spoke(570, "That didn't apply.")], False),
    "a command that only reads": (lambda: [_acted(580, "Bash", {"command": "ls -la"})], False),
    "a subagent that changed nothing": (
        lambda: [_acted(580, "Agent", {"prompt": "go"}, say=""), _came_back(580), _spoke(570)], False),
    "a subagent that failed": (
        lambda: [_acted(580, "Agent", {"prompt": "go"}, say=""), _came_back(580, is_error=True, edited_files=2),
                 _spoke(570)], False),
    "a subagent sent to the background": (
        lambda: [_acted(580, "Agent", {"prompt": "go"}, say=""), _came_back(580, edited_files=2, background=True),
                 _spoke(570)], False),
}


@pytest.mark.parametrize("name", list(_THIRD_REPLIES))
def test_only_a_change_to_a_file_of_yours_answers_a_request_in_a_run(tmp_path, name):
    reply, counts = _THIRD_REPLIES[name]
    records = [*_START, _said("make the save button bigger", 900), _changed(880), _said("now move the logo", 600),
               *reply()]
    note = _send(tmp_path, records, "and make the footer too")
    assert (note != "" and _kind(note) == "drip_feed") is counts, name


#: What Claude does after your message was answered, in answer to a line you didn't type.
_NOT_YOURS = [
    "<task-notification>\n<task-id>a1</task-id>\n<result>Done.</result>\n</task-notification>",
    "<scheduled-task>Check the build</scheduled-task>",
    "<command-name>/cost</command-name>",
    'Another Claude session sent a message:\n<agent-message from="a57b4a7d930e32da8">hello</agent-message>',
]


@pytest.mark.parametrize("line", _NOT_YOURS, ids=["agent report", "scheduled task", "slash command", "session message"])
def test_only_the_reply_a_message_started_is_credited_to_it(tmp_path, line):
    def run(*between):
        # The first message was answered with words, then Claude changed a file a line later.
        return [*_START, _said("make the save button bigger", 900), _spoke(880, "Looking at it."), *between,
                _acted(790, "Edit", {"file_path": "/work/app/app.css"}), _said("now move the logo", 600),
                _changed(580)]

    prompt = "and make the footer too"
    # Straight after the reply, the edit is the message's own: a run of three.
    assert _kind(_send(tmp_path, run(), prompt)) == "drip_feed"
    # After a line you didn't type, the edit answers that line, not your message.
    assert _send(tmp_path, run(_said(line, 800)), prompt, session="s2") == ""
    # The same when the line says it is not yours by its origin rather than its words.
    by_origin = {**_said("check the build", 800), "turnOrigin": "task_notification"}
    assert _send(tmp_path, run(by_origin), prompt, session="s3") == ""


def test_a_resume_note_carries_on_the_work_of_your_last_message(tmp_path):
    def run(note):
        return [*_START, _said("make the save button bigger", 900), _spoke(880, "Working on it."), _said(note, 800),
                _acted(790, "Edit", {"file_path": "/work/app/app.css"}), _said("now move the logo", 600),
                _changed(580)]

    prompt = "and make the footer too"
    # You hit a usage limit, or the app was quit, and Claude went on with the same work: still your message's.
    for n, note in enumerate((cat.LIMIT_RESUME_PREFIX + ", carry on", cat.APP_QUIT_PREFIX + ", so carry on")):
        assert _kind(_send(tmp_path, run(note), prompt, session=f"r{n}")) == "drip_feed", note
    assert _send(tmp_path, run("<task-notification>done</task-notification>"), prompt, session="x") == ""


def test_a_subagents_edits_go_to_the_message_whose_reply_launched_it():
    launch = _acted(880, "Agent", {"prompt": "go"}, say="")
    # You typed again while it ran; the files are still the first message's, whenever its result comes back.
    records = [_said("make the save button bigger", 900), launch, _said("now move the logo", 600),
               _came_back(880, edited_files=2), _changed(560)]
    exchanges, _ = HOOK._exchanges(records, cat.INTERRUPT_PREFIX, cat.EDIT_TOOLS)
    assert [(ex["text"], ex["edits"], ex["edited"]) for ex in exchanges] == [
        ("make the save button bigger", 2, True), ("now move the logo", 1, True)]


def test_a_message_you_sent_again_after_stopping_it_is_not_a_request_in_its_own_right(tmp_path):
    # Stopped before any reply and typed again: the first copy asked for nothing Claude answered.
    records = [*_START, _said("make the save button bigger", 1_000), _stopped(990),
               _said("make the save button bigger", 900), _changed(880), _said("now move the logo", 600),
               _changed(580)]
    assert _kind(_send(tmp_path, records, "and make the footer too")) == "drip_feed"


def test_a_message_sent_while_claude_is_still_working_gets_no_run_hint(tmp_path):
    base = [*_START, _said("make the save button bigger", 900), _changed(880), _said("now move the logo", 600),
            _changed(580), _said("and make the header blue", 100)]
    # The reply it was sent into has finished: the third small request in a row.
    assert _kind(_send(tmp_path, [*base, _changed(60)], "and make the footer too")) == "drip_feed"
    # Sent while Claude was mid-call, or as a tool's result came back, it is a nudge into work under way.
    working = [*base, _acted(60, "Read", {"file_path": "/work/app/a.py"}, say="")]
    assert _send(tmp_path, working, "and make the footer too", session="s2") == ""
    back = [*working, _came_back(60)]
    assert _send(tmp_path, back, "and make the footer too", session="s3") == ""
    # A call that is long over is where work stopped, not work under way: the same message is a request.
    def editing(ago_s):
        block = {"type": "tool_use", "id": "toolu_z", "name": "Edit", "input": {"file_path": "/work/app/a.css"}}
        return _reply(30_000, ago_s=ago_s, one_hour=True, content=[{"type": "text", "text": "On it."}, block])

    head = [*_START, _said("make the save button bigger", 900), _changed(880), _said("now move the logo", 700)]
    assert _send(tmp_path, [*head, editing(100)], "and make the footer too", session="s4") == ""
    assert _kind(_send(tmp_path, [*head, editing(650)], "and make the footer too", session="s5")) == "drip_feed"


def test_the_report_counts_the_run_the_live_hint_flags(tmp_path):
    """The hint, the status line and the report's small-requests count read
    a run by one rule: each scenario ends on a message, and the hook's count
    for it is the length of the run the report puts it in (0 when none)."""
    from claudeglass import prompting

    prompt = "and make the footer grey"
    long = "make the save button bigger and " + "keep the colours as they are, " * 12
    notice = "<task-notification>\n<task-id>a1</task-id>\n<result>Done.</result>\n</task-notification>"
    edit = {"file_path": "/work/app/app.css"}
    scenarios = {
        "three in a row": ([*_START, _said("make the save button bigger", 900), _changed(880),
                            _said("now move the logo", 600), _changed(580)], 3),
        "a go-ahead between": ([*_START, _said("make the save button bigger", 900), _changed(880),
                                _said("go ahead", 700), _spoke(680), _said("now move the logo", 600),
                                _changed(580)], 3),
        "a status check between": ([*_START, _said("make the save button bigger", 900), _changed(880),
                                    _said("how is it going?", 700), _spoke(680), _said("now move the logo", 600),
                                    _changed(580)], 3),
        "a question between": ([*_START, _said("make the save button bigger", 900), _changed(880),
                                _said("why is the button blue?", 700), _spoke(680), _said("now move the logo", 600),
                                _changed(580)], 3),
        "a statement between": ([*_START, _said("make the save button bigger", 900), _changed(880),
                                 _said("the button is too small", 700), _spoke(680), _said("now move the logo", 600),
                                 _changed(580)], 3),
        "a thank-you between": ([*_START, _said("make the save button bigger", 900), _changed(880),
                                 _said("thanks!", 700), _spoke(680), _said("now move the logo", 600),
                                 _changed(580)], 3),
        "a reply that changed nothing": ([*_START, _said("make the save button bigger", 900), _spoke(880),
                                          _said("now move the logo", 600), _changed(580)], 0),
        "a subagent's edits": ([*_START, _said("make the save button bigger", 900),
                                _acted(880, "Agent", {"prompt": "go"}, say=""), _came_back(880, edited_files=2),
                                _spoke(870), _said("now move the logo", 600), _changed(580)], 3),
        "a failed subagent": ([*_START, _said("make the save button bigger", 900),
                               _acted(880, "Agent", {"prompt": "go"}, say=""),
                               _came_back(880, is_error=True, edited_files=2), _spoke(870),
                               _said("now move the logo", 600), _changed(580)], 0),
        "an edit under .claude": ([*_START, _said("make the save button bigger", 900),
                                   _acted(880, "Edit", {"file_path": "/home/me/.claude/plans/p.md"}),
                                   _said("now move the logo", 600), _changed(580)], 0),
        "a message that was long": ([*_START, _said(long, 900), _changed(880), _said("now move the logo", 600),
                                     _changed(580)], 0),
        "a slow message": ([_said("Build the settings page: " + "a form and a header. " * 20, 4_000),
                            _changed(3_900), _said("make the save button bigger", 3_800), _changed(3_700),
                            _said("now move the logo", 1_000), _changed(900)], 0),
        "an answer to a question": ([*_START, _said("make the save button bigger", 900), _changed(880),
                                     _said("now move the logo", 600), _spoke(580, "Left or right?")], 0),
        "an offer after a change": ([*_START, _said("make the save button bigger", 900), _changed(880),
                                     _said("now move the logo", 600),
                                     _changed(580, say="Done. Want me to move the footer too?")], 3),
        "an edit for an agent's report": ([*_START, _said("make the save button bigger", 900), _spoke(880),
                                           _said(notice, 800), _acted(790, "Edit", edit),
                                           _said("now move the logo", 600), _changed(580)], 0),
        "an edit after a shell line you ran": ([*_START, _said("make the save button bigger", 900), _spoke(880),
                                                _said("<bash-input>ls</bash-input>", 800), _acted(790, "Edit", edit),
                                                _said("now move the logo", 600), _changed(580)], 0),
        "an edit after another session's message": ([*_START, _said("make the save button bigger", 900),
                                                     _spoke(880),
                                                     _said("Another Claude session sent a message: hello", 800),
                                                     _acted(790, "Edit", edit), _said("now move the logo", 600),
                                                     _changed(580)], 0),
        "an edit after a peer's line": ([*_START, _said("make the save button bigger", 900), _spoke(880),
                                         {**_said("check the build", 800), "turnOrigin": "peer"},
                                         _acted(790, "Edit", edit), _said("now move the logo", 600),
                                         _changed(580)], 0),
        "an edit after a resume note": ([*_START, _said("make the save button bigger", 900), _spoke(880),
                                         _said(cat.APP_QUIT_PREFIX + ", so carry on", 800), _acted(790, "Edit", edit),
                                         _said("now move the logo", 600), _changed(580)], 3),
        "a failed reply between": ([*_START, _said("make the save button bigger", 900), _changed(880),
                                    _said("tighten the footer", 700), _failed(680), _said("now move the logo", 600),
                                    _changed(580)], 3),
    }
    coaching = CATALOGUE["coaching"]
    for n, (name, (records, expected)) in enumerate(scenarios.items()):
        at = NOW - timedelta(seconds=5)
        hook = HOOK._drip_hint(prompt, records, coaching, cat.COACHING_THRESHOLDS, at, 30_000)
        assert (hook[1] if hook else 0) == expected, name
        # The report reads the same lines once Claude has answered the last message too.
        lines = [*records, _said(prompt, 5), _changed(3)]
        path = Path(_transcript(tmp_path, [_real(r) for r in lines], f"scenario{n}.jsonl"))
        top = parse_transcript(path, TranscriptMeta(path=str(path)))
        session = prompting.session_prompting(NS(top=top, session_id="s"), prompting._Prices(load_pricing()))
        last = len(session.messages) - 1
        runs = [run for run in prompting._drip_runs(session.messages) if last in run]
        assert (len(runs[0]) if runs else 0) == expected, name


def test_a_vague_correction_is_report_only_and_needs_a_bad_outcome_phrase(tmp_path):
    records = [_said("Add a dark mode toggle", 200), _reply(30_000, ago_s=100)]
    vagues = ("it's still broken", "doesn't work", "wrong", "still broken, it should work", "wrong, make it right",
              "that didn't work", "still failing", "no change", "try again")
    for n, vague in enumerate(vagues):
        # No live note: the habit is counted after the fact (``prompting.py``).
        assert _send(tmp_path, records, vague, session=f"v{n}") == "", vague
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
        # No correction or bad-outcome phrase: "fix" alone says nothing went wrong.
        "fix it",
        "fix this",
        # A question asks for an answer, not a fix.
        "What problems can you fix now you are on my machine",
        "how do I fix the build?",
        "why is it still broken?",
        # A go-ahead or an acknowledgement is not a correction.
        "ok",
        "go ahead",
    )):
        assert _send(tmp_path, records, fine, session=f"f{n}") == "", fine
        assert not prompt_shape.is_vague_fix(fine, 80), fine


def test_one_note_at_a_time_while_the_small_requests_keep_coming(tmp_path):
    records = [*_START, _said("fix the header", 600), _changed(580), _said("tighten the footer", 300), _changed(280)]
    assert _kind(_send(tmp_path, records, "fix it")) == "drip_feed"
    # The run hint is resting; the next small change in the same run adds nothing.
    more = [*records, _said("fix it", 200), _changed(180)]
    assert _send(tmp_path, more, "fix the logo too", now=NOW + timedelta(seconds=30)) == ""


def test_a_huge_paste_gets_the_paste_less_hint(tmp_path):
    records = [_said("hi", 200), _reply(20_000, ago_s=100)]
    note = _send(tmp_path, records, "Why does this fail?\n" + "log line\n" * 5_000)
    assert _kind(note) == "big_paste" and "about 11k tokens" in note and "log line" not in note
    assert _send(tmp_path, records, "Why does this fail?\n" + "log line\n" * 1_000, session="s2") == ""


def test_stopping_claude_again_and_again_gets_no_hint(tmp_path):
    """Report-only: most stops mid-reply were meant, so no note is added.
    Counted after the fact, bare stops only (``prompting.py``)."""
    records = [
        _said("Refactor the store", 1_150), _stopped(1_100), _said("no, keep the API", 1_000),
        _stopped(600, blocks=True), _said("use the cache instead", 500), _stopped(60),
    ]
    assert _send(tmp_path, records, "just rename it for now") == ""
    # A message sent again after Esc before any reply, and stops after one reply, add nothing either.
    sqlite = "Use SQLite instead of JSON for the store, with the same class and docstrings"
    resent = [*_START, _said(sqlite, 300), _said(sqlite, 200)]
    assert _send(tmp_path, resent, sqlite, session="s2") == ""
    stops = [*_START, _said("refactor the store", 900), _changed(880), _stopped(870, blocks=True),
             _said("keep the API", 600), _said("keep the API as it is", 500)]
    assert _send(tmp_path, stops, "use the cache instead of the API", session="s3") == ""
    assert not {"stop_loop_count", "stop_window_minutes"} & set(cat.COACHING_THRESHOLDS)


_BIG_TASK = (
    "Add a login page with email and password, a settings page where people change their name, email alerts "
    "when a report is ready, and an admin screen that lists every account."
)


def test_a_big_task_outside_plan_mode_gets_no_hint(tmp_path):
    """Report-only: replayed over 30 days of real sessions, the hint was
    right in none of 7 firings, and with its rules tightened every message
    of 3 or more steps was rightly left alone. Counted after the fact
    (``prompting.py``)."""
    path = _transcript(tmp_path, [*_START])

    def send(prompt, mode, session, config=ON):
        payload = {**_prompt_payload(path), "prompt": prompt, "session_id": session, "permission_mode": mode}
        return _coach(tmp_path, payload, config)

    assert send(_BIG_TASK, "default", "a") == ""
    listed = "Please do these:\n1. add a login page\n2. add a settings page\n3. email alerts\n4. an admin screen\n" + (
        "Keep the existing styles and tests passing throughout, and don't touch the database schema."
    )
    assert send(listed, "acceptEdits", "b") == ""
    # Nothing in your settings brings it back: the keys are ignored.
    two_changes = (
        "Make the header sticky so it stays at the top when the page scrolls, and keep its shadow the same as it is "
        "today on the settings page."
    )
    looser = {**ON, "thresholds": {"coaching_plan_steps": 2, "coaching_plan_min_chars": 20}}
    assert send(two_changes, "default", "c", looser) == ""


def test_the_hint_made_report_only_has_nothing_left_for_the_hook(tmp_path):
    for hint in ("plan_first",):
        # Old notes in earlier transcripts are still recognised, so what they cost is still counted.
        assert hint in cat.RETIRED_COACHING_HINTS and hint not in cat.COACHING_HINTS, hint
        for table in (cat.COACHING_TEXT, cat.COACHING_TIP, cat.COACHING_NOTICE, CATALOGUE["coaching"]["text"],
                      CATALOGUE["coaching"]["notice"]):
            assert hint not in table, hint
    gone = ("plan_steps", "plan_min_chars")
    # Its rule is fixed now, as the other report-only habits' are: no key of the file or the config.
    assert not set(gone) & set(cat.COACHING_THRESHOLDS)
    assert not set(gone) & set(CATALOGUE["coaching"]["thresholds"])
    assert set(gone) <= set(cat.REPORT_THRESHOLDS)
    assert HOOK.coaching_thresholds(CATALOGUE["coaching"], {"thresholds": {"coaching_plan_steps": 2}}, {}).get(
        "plan_steps") is None
    # drip_feed is not one of them: it stays live, with the keys that tune it.
    assert "drip_feed" in cat.COACHING_HINTS and "drip_feed" not in cat.RETIRED_COACHING_HINTS
    assert {"drip_count", "drip_window_minutes", "drip_chars"} <= set(cat.COACHING_THRESHOLDS)
    assert HOOK.coaching_thresholds(CATALOGUE["coaching"], {"thresholds": {"coaching_drip_count": 2}}, {})[
        "drip_count"] == 2


def test_sending_the_same_request_again_gets_no_hint(tmp_path):
    """Report-only: a repeat is mostly a poll or a go-ahead, so no note is
    added. Counted after the fact, when the earlier message was answered
    with an edit (``prompting.py``)."""
    records = [*_START, _said("Make the save button bigger and move it to the right", 600), _changed(580)]
    assert _send(tmp_path, records, "make the save button bigger and move it right") == ""
    assert not {"repeat_similarity", "repeat_min_words", "repeat_window_minutes"} & set(cat.COACHING_THRESHOLDS)


def _failed(ago_s: float) -> dict:
    """What Claude Code writes when a reply dies on an API error."""
    return {
        "type": "assistant", "timestamp": _iso(NOW - timedelta(seconds=ago_s)), "isApiErrorMessage": True,
        "message": {"id": f"msg_f{ago_s}", "model": "<synthetic>", "role": "assistant",
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                    "content": [{"type": "text", "text": "API Error: 529 Overloaded"}]},
    }


def test_a_failed_reply_is_not_an_answer_to_a_run_of_small_requests(tmp_path):
    # A reply that died on an API error changed nothing: it neither answers a
    # message nor joins a run of small requests that were each answered by a change.
    records = [*_START, _said("fix the header", 600), _failed(598), _said("tighten the footer", 500), _failed(498),
               _said("move the logo left", 400), _failed(398)]
    assert _send(tmp_path, records, "make the save button bigger") == ""
    answered = [*_START, _said("fix the header", 600), _changed(580), _said("tighten the footer", 500), _failed(498),
                _said("move the logo left", 400), _changed(380)]
    # The failed one in the middle is skipped, not a break: the run holds the two answered messages.
    assert _kind(_send(tmp_path, answered, "make the save button bigger", session="s2")) == "drip_feed"


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
    # yours, however long it is, and even in a big context.
    records = [*_START, _said("fix the header", 600), _changed(580, 150_000), _said("tighten the footer", 300),
               _changed(280, 150_000)]
    path = _transcript(tmp_path, records)
    payload = {"hook_event_name": "UserPromptSubmit", "transcript_path": path, "prompt": prompt,
               "permission_mode": "default"}
    assert HOOK.coaching_for({"session_id": "s1", "cwd": "/w", **payload}, ON, CATALOGUE, _config_dir(tmp_path),
                             now=NOW) == ("", "")
    # The same small change typed by you still gets its hint, and so does a paste.
    note = _coach(tmp_path, {**payload, "prompt": "fix the logo", "session_id": "s2"})
    assert _kind(note) == "drip_feed"
    typed = "Why does this fail?\n" + "log line\n" * 5_000
    note = _coach(tmp_path, {**payload, "prompt": typed, "session_id": "s3"})
    assert _kind(note) == "big_paste"


def test_the_status_line_and_the_parser_skip_a_failed_reply_too(tmp_path):
    from claudeglass import statusline

    ask = "Refactor the payment retry logic in billing/retry.py to use exponential backoff"
    tail = [*_START, _said(ask, 600), _failed(598), _said(ask, 500), _failed(498), _said(ask, 400), _failed(398),
            _said(ask, 5)]
    assert statusline._prompt_habits(tail, 30_000, NOW) == []
    path = Path(_transcript(tmp_path, tail, "parsed.jsonl"))
    result = parse_transcript(path, TranscriptMeta(path=str(path)))
    assert not any(e.detail.get("repeat") for e in result.events if e.kind.name == "HUMAN_TEXT")


#: A message and whether it asks Claude to change something (``prompt_shape.is_change_request``):
#: a change verb opening a sentence, and not a go-ahead, a thank-you, a status check, a question,
#: a report, a statement or an explain request.
_REQUESTS = [
    ("thanks", False), ("ok, looks good!", False), ("go ahead", False), ("continue", False),
    ("how is it going?", False), ("any update?", False), ("why is it blue?", False), ("What does this do", False),
    ("make the button bigger", True), ("fix line 42", True), ("move the logo left", True),
    ("can you make it bigger?", False), ("and the footer too", False), ("yes, move the footer too", True),
    ("left of the title", False), ("tighten the footer", True), ("now add a footer.", True),
    ("Can you add a footer. Thanks", True), ("Please make it wider", True), ("let's add a footer", True),
    ("Looks good. Now add the footer", True), ("Thanks! Now rename the store", True), ("it's broken, fix it", True),
    # A statement, a report and an explain or clarify request are no change request.
    ("the button is too small", False), ("it does not load", False), ("this is a new bug: the page 500s", False),
    ("explain how this works", False), ("clarify what you mean", False), ("review this and fix it", False),
    # A change verb used as a noun or as the subject of a report is no request either.
    ("build failed", False), ("Build is broken again", False), ("Update: it works now", False),
    ("Install failed on windows", False), ("Style looks off", False), ("Support for tabs is missing", False),
    ("Group chat is broken", False), ("Turn 5 was slow", False), ("Change log looks good", False),
    ("Build script failed", False), ("add support for Windows", True),
    # What a go-ahead is made of: the next step of work, not a change.
    ("merge it", False), ("run the tests", False), ("ship it", False), ("commit and push", False),
    ("carry on", False), ("yes, release it", False), ("do 1 and 2", False),
]


@pytest.mark.parametrize("text, asks", _REQUESTS)
def test_a_message_asks_for_a_change_by_the_rule_the_report_counts_it_with(text, asks):
    assert prompt_shape.is_change_request(text) is asks
    # The hook reads it, a question, a poll and a go-ahead as the package does.
    coaching = CATALOGUE["coaching"]
    assert HOOK._is_change_request(text, coaching) is asks
    assert HOOK._is_question(text, coaching) == prompt_shape.is_question(text)
    assert HOOK._is_status(text, coaching) == prompt_shape.is_status(text)
    assert HOOK._is_go(text, coaching) == prompt_shape.is_go(text)


def test_the_hook_counts_a_run_of_small_requests_as_the_package_does():
    th = cat.COACHING_THRESHOLDS
    coaching = CATALOGUE["coaching"]

    def ex(text, *, edited=True, answered=True, answer=False, since=60):
        return {"text": text, "edited": edited, "answered": answered, "answer": answer, "since": since}

    run = [ex("make the button bigger"), ex("move the logo")]
    prompt = "and make the footer grey"
    cases = [
        ([], prompt, 30, False),
        (run, prompt, 30, False),
        (run, prompt, 30, True),
        (run, prompt, 9_999, False),
        (run, prompt, None, False),
        (run, "and the footer too", 30, False),
        (run, "go ahead", 30, False),
        (run, "why is it blue?", 30, False),
        (run, "the footer is too small", 30, False),
        (run, "make " + "x" * 400, 30, False),
        # Asking for no change: neither counts nor ends the run.
        ([*run, ex("thanks", edited=False)], prompt, 30, False),
        ([*run, ex("how is it going?", edited=False)], prompt, 30, False),
        ([*run, ex("the button is too small", edited=False)], prompt, 30, False),
        ([*run, ex("explain the footer", edited=False)], prompt, 30, False),
        ([*run, ex("why is it blue?", edited=False)], prompt, 30, False),
        ([*run, ex("left of the title", edited=False, answer=True)], prompt, 30, False),
        ([*run, ex("make it left of the title", edited=False, answer=True)], prompt, 30, False),
        ([*run, ex("never answered, make it", answered=False, edited=False)], prompt, 30, False),
        # A change request that got no change, or came too slowly, ends it.
        ([*run, ex("make another one", edited=False)], prompt, 30, False),
        ([*run, ex("make it slow", since=9_999)], prompt, 30, False),
        ([*run, ex("make it first", since=None)], prompt, 30, False),
        ([ex("make " + "x" * 400), *run], prompt, 30, False),
    ]
    for earlier, text, since, answer in cases:
        assert HOOK._drip_count(earlier, text, since, answer, coaching, th) == prompt_shape.drip_count(
            earlier, text, since, answer, th), (earlier, text, since, answer)
    assert prompt_shape.drip_count(run, prompt, 30, False, th) == 3
    assert prompt_shape.drip_count([*run, ex("thanks", edited=False)], prompt, 30, False, th) == 3
    assert prompt_shape.drip_count([*run, ex("the button is too small", edited=False)], prompt, 30, False, th) == 3
    assert prompt_shape.drip_count([*run, ex("make another one", edited=False)], prompt, 30, False, th) == 1
    assert prompt_shape.drip_count([*run, ex("make it slow", since=9_999)], prompt, 30, False, th) == 1
    # The prompt itself must be a short change request, sent in time and not an answer.
    assert prompt_shape.drip_count(run, "and the footer too", 30, False, th) == 0
    assert prompt_shape.drip_count(run, prompt, 30, True, th) == 0
    assert prompt_shape.drip_count(run, prompt, th["drip_window_minutes"] * 60 + 1, False, th) == 0


#: Tool calls and results as the hook, the status line and the package must each read them.
_EXCHANGE_RECORDS = [
    _said("make the save button bigger", 900),
    _acted(880, "Edit", {"file_path": "/work/app/app.css"}, say="Done. Anything else?"),
    _said("now the footer", 800),
    _acted(780, "Write", {"file_path": "/home/me/.claude/plans/p.md"}),
    _said("and the header", 700),
    _acted(680, "Edit", {"file_path": "/work/app/h.css"}, say=""), _came_back(680, is_error=True),
    _spoke(670, "That didn't apply. Want me to retry?"),
    _said("yes", 600),
    _acted(580, "Bash", {"command": "sed -i 's/a/b/' app.css"}, say="Done."),
    _said("and the logo", 500),
    _acted(480, "Agent", {"prompt": "go"}, say=""), _came_back(480, edited_files=2), _spoke(470, "Which side?"),
    _said("left", 400),
    _acted(380, "Bash", {"command": "ls"}, say="Looks fine."),
]


def test_the_hook_and_the_status_line_read_what_claude_changed_by_the_same_rule():
    from claudeglass import statusline

    hook_exchanges, state = HOOK._exchanges(_EXCHANGE_RECORDS, cat.INTERRUPT_PREFIX, cat.EDIT_TOOLS)
    status_exchanges = statusline._exchanges(_EXCHANGE_RECORDS, cat.INTERRUPT_PREFIX, cat.EDIT_TOOLS)
    assert hook_exchanges == status_exchanges
    assert [(ex["text"], ex["edits"], ex["answered"], ex["answer"], ex["since"]) for ex in hook_exchanges] == [
        ("make the save button bigger", 1, True, False, None),
        # An offer after a change isn't a question: "now the footer" answers nothing.
        ("now the footer", 0, True, False, 100),
        ("and the header", 0, True, False, 100),
        # Claude asked and changed nothing, so this one answers it.
        ("yes", 1, True, True, 100),
        ("and the logo", 2, True, False, 100),
        # The subagent changed files, then Claude offered: not a question this one answers.
        ("left", 0, True, False, 100),
    ]
    assert state["typed_at"] == hook_exchanges[-1]["at"] and state["answer"] is False


#: Lines you didn't type, and what Claude does after them, as the hook and the status line must each read them.
_CLOSED_RECORDS = [
    _said("make the save button bigger", 900),
    _spoke(880, "Looking at it."),
    _said("<task-notification>\n<result>Done.</result>\n</task-notification>", 800),
    _acted(790, "Edit", {"file_path": "/work/app/app.css"}, say="Applied it."),
    _said("now move the logo", 600),
    _acted(580, "Agent", {"prompt": "go"}, say=""), _came_back(580, edited_files=2, background=True),
    _spoke(570, "Started it."),
    _said("and the header", 500),
    _acted(480, "Edit", {"file_path": "/work/app/h.css"}, say="Done."),
    _said(cat.APP_QUIT_PREFIX + ", so carry on", 450),
    _acted(440, "Edit", {"file_path": "/work/app/h2.css"}, say="Done."),
    _said("and the footer", 400),
    _acted(380, "Agent", {"prompt": "go"}, say=""), _came_back(380, edited_files=3), _spoke(370, "Done."),
    {**_said("check the build", 300), "turnOrigin": "scheduled"},
    _acted(290, "Edit", {"file_path": "/work/app/f.css"}, say="Done."),
    {"type": "user", "isSidechain": True, "message": {"role": "user", "content": "do it"}},
    {"type": "user", "isMeta": True, "message": {"role": "user", "content": "caveat"}},
]


def test_a_line_you_didnt_type_ends_the_reply_a_message_started_for_the_hook_and_the_status_line_alike():
    from claudeglass import statusline

    hook_exchanges, _ = HOOK._exchanges(_CLOSED_RECORDS, cat.INTERRUPT_PREFIX, cat.EDIT_TOOLS)
    status_exchanges = statusline._exchanges(_CLOSED_RECORDS, cat.INTERRUPT_PREFIX, cat.EDIT_TOOLS)
    assert hook_exchanges == status_exchanges
    assert [(ex["text"], ex["edits"], ex["edited"]) for ex in hook_exchanges] == [
        # An agent's report came after the reply, so the edit that followed answered that.
        ("make the save button bigger", 0, False),
        # Sent to the background: its launch message changed nothing.
        ("now move the logo", 0, False),
        # A resume note carries on the same work.
        ("and the header", 2, True),
        # The subagent's files are the message's, and the scheduled run's edit after it is not.
        ("and the footer", 3, True),
    ]


#: Shell commands and what ``shell_writes`` makes of them: the hook's pattern must agree.
_SHELL_CORPUS = [
    ("sed -i 's/a/b/' app.css", False), ("sed -n '1,5p' app.css", False), ("perl -pi -e 's/a/b/' app.css", False),
    ("cat > notes.txt <<'EOF'\nhello\nEOF", False), ("echo hi > out.txt", False), ("echo hi >> out.txt", False),
    ("echo x | tee out.txt", False), ("echo hi > /dev/null", False), ("ls 2>&1", False), ("ls -la", False),
    ("npm test > test.log", False), ("git status > /dev/null 2>&1", False), ("echo hi 2> err.log", False),
    ("Set-Content -Path out.txt -Value hi", True), ("Add-Content out.txt 'more'", True),
    ("'hi' | Out-File out.txt", True), ("grep foo src/*.py", False), ("mkdir -p build && cp a b", False),
]


@pytest.mark.parametrize("command, powershell", _SHELL_CORPUS)
def test_the_hook_reads_a_shell_write_as_the_parser_does(command, powershell):
    from claudeglass import shell_writes

    writes = bool(shell_writes.write_targets(command, powershell=powershell, cwd="/work/app"))
    block = {"type": "tool_use", "name": "PowerShell" if powershell else "Bash", "input": {"command": command}}
    assert HOOK._changes_file(block, cat.EDIT_TOOLS) is writes, command
    assert prompt_shape.edits_files(block["name"], block["input"], cat.EDIT_TOOLS) is writes, command
    assert prompt_shape.writes_files(command) is writes, command


def test_the_hook_reads_an_edit_call_as_the_package_does():
    calls = [
        ("Edit", {"file_path": "/work/app/a.py"}), ("Write", {"file_path": "C:\\Users\\me\\.claude\\plans\\p.md"}),
        ("MultiEdit", {"file_path": "/home/me/.claude/memory/m.md"}), ("NotebookEdit", {"notebook_path": "/work/n.ipynb"}),
        ("Edit", {}), ("Read", {"file_path": "/work/app/a.py"}), ("Bash", {}), ("Bash", {"command": 5}),
        ("Bash", {"command": "echo hi > /home/me/.claude/x.md"}),
    ]
    for name, tool_input in calls:
        block = {"type": "tool_use", "name": name, "input": tool_input}
        assert HOOK._changes_file(block, cat.EDIT_TOOLS) == prompt_shape.edits_files(name, tool_input, cat.EDIT_TOOLS), (
            name, tool_input)
    assert HOOK._agent_edit_files({"toolUseResult": {"toolStats": {"editFileCount": 3}}}) == 3
    assert prompt_shape.agent_edit_files({"toolUseResult": {"toolStats": {"editFileCount": 3}}}) == 3
    for odd in ({}, {"toolUseResult": "x"}, {"toolUseResult": {"toolStats": {"editFileCount": True}}},
                {"toolUseResult": {"toolStats": {"editFileCount": 0}}}):
        assert HOOK._agent_edit_files(odd) == prompt_shape.agent_edit_files(odd) == 0, odd


def test_the_hook_the_status_line_and_the_parser_tell_a_background_launch_from_a_report():
    from claudeglass import statusline

    for result, background in [({"isAsync": True}, True), ({"status": "async_launched"}, True),
                               ({"status": "completed"}, False), ({"toolStats": {"editFileCount": 2}}, False),
                               ("text", False), (None, False)]:
        line = {"type": "user", "toolUseResult": result, "message": {"role": "user", "content": []}}
        assert HOOK._is_async_launch(line) is background, result
        assert statusline._is_async_launch(line) is background, result
        assert parse._is_async_launch(line) is background, result


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
    assert HOOK._typed(line, cat.INTERRUPT_PREFIX) is False
    assert HOOK._exchanges([line], cat.INTERRUPT_PREFIX, ())[0] == []
    assert statusline._exchanges([line], cat.INTERRUPT_PREFIX, ()) == []
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
    from claudeglass import events, statusline
    from claudeglass.model import EventKind

    line = {"type": "user", "message": {"role": "user", "content": text}, "origin": {"kind": "human"}}
    assert events.classify_line(line).kind == EventKind.HUMAN_TEXT
    assert HOOK._typed(line, cat.INTERRUPT_PREFIX) is True
    assert len(HOOK._exchanges([line], cat.INTERRUPT_PREFIX, ())[0]) == 1
    assert len(statusline._exchanges([line], cat.INTERRUPT_PREFIX, ())) == 1


def test_a_prompt_hint_comes_before_the_cold_return_receipt(tmp_path):
    # Every reply on the 5-minute cache: a 1-hour write among the last five would keep it warm.
    records = [_START[0], _changed(1_100, one_hour=False), _said("fix the header", 600),
               _changed(580, 150_000, one_hour=False), _said("tighten the footer", 300),
               _changed(280, 150_000, one_hour=False)]
    assert _kind(_send(tmp_path, records, "fix it")) == "drip_feed"
    # Half a minute on, the drip-feed hint is resting and the 5-minute cache has gone cold.
    assert _kind(_send(tmp_path, records, "fix it", now=NOW + timedelta(seconds=30))) == "cold_return"
    # A paste comes first too.
    paste = "Why does this fail?\n" + "log line\n" * 5_000
    assert _kind(_send(tmp_path, records, paste, session="s2")) == "big_paste"
    assert _kind(_send(tmp_path, records, paste, session="s2", now=NOW + timedelta(seconds=30))) == "cold_return"


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
    call = {"hook_event_name": "PostToolUse", "tool_name": "Grep", "transcript_path": session, "agent_id": "abc",
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
    other = {"hook_event_name": "PostToolUse", "tool_name": "Grep", "transcript_path": session, "agent_id": "abc",
             "agent_type": "Explore"}
    assert _coach(tmp_path, other) == ""
    assert not (config_dir / cat.COACH_STATE_FILE).exists()
    nested_session, _ = _agent(tmp_path, [_reply(1_000, message_id="m1")], "wf1", nested=True)
    workflow = {**other, "agent_type": "general-purpose", "agent_id": "wf1", "transcript_path": nested_session}
    assert _coach(tmp_path, workflow) == ""


def test_your_thresholds_win_over_the_file_and_the_defaults(tmp_path):
    config_dir = _config_dir(tmp_path)
    (config_dir / cat.COACHING_FILE).write_text(
        json.dumps({"thresholds": {"cold_min_tokens": 70_000}}), encoding="utf-8"
    )
    path = _transcript(tmp_path, [_prompt(), _reply(60_000, ago_s=20 * 60)])
    assert _coach(tmp_path, _prompt_payload(path)) == ""
    configured = {**ON, "thresholds": {"coaching_cold_min_tokens": 50_000}}
    assert _kind(_coach(tmp_path, _prompt_payload(path), configured)) == "cold_return"


def test_a_project_capture_leaves_out_gets_no_coaching(tmp_path):
    path = _transcript(tmp_path, [_prompt(), _reply(150_000, ago_s=20 * 60)])
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
    # A background Stop keeps the newest reply for the cold-return receipt: it prints nothing, so nothing waits.
    assert (cat.HOOK_SCRIPT, "Stop", "", True) in specs
    assert sum(1 for spec in deep if spec[1] == "Stop") == 1


# -- cold_return: the receipt for a return after the prompt cache expired -------------


_SESSION_LIMIT = "You've hit your session limit · resets 3pm (Europe/London)"
_WEEKLY_LIMIT = "You've hit your weekly limit · resets Mon 9am (Europe/London)"


def _limit_line(ago_s: float, *, tokens: int = 0, text: str = _SESSION_LIMIT, resets_in_s: float | None = None) -> dict:
    """The line Claude Code writes in place of a reply when a usage limit
    stops it: no real reply, whatever usage it carries. ``resets_in_s``
    is when the limit resets, in seconds from :data:`NOW` (negative: already
    over), as the line's ``quotaLimits.resetsAt``; left out when ``None``."""
    usage = {"input_tokens": tokens, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    record = {
        "type": "assistant",
        "isApiErrorMessage": True,
        "timestamp": _iso(NOW - timedelta(seconds=ago_s)),
        "message": {"id": f"msg_limit_{ago_s}", "model": "<synthetic>", "usage": usage,
                    "content": [{"type": "text", "text": text}]},
    }
    if resets_in_s is not None:
        record["quotaLimits"] = {"resetsAt": NOW.timestamp() + resets_in_s}
    return record


def _compact(ago_s: float | None, uuid: str | None = None) -> dict:
    record: dict = {"type": "system", "subtype": "compact_boundary"}
    if ago_s is not None:
        record["timestamp"] = _iso(NOW - timedelta(seconds=ago_s))
    if uuid:
        record["uuid"] = uuid
    return record


def _with_uuid(record: dict, uuid: str) -> dict:
    return {**record, "uuid": uuid}


def _stop(tmp_path, records, *, session: str = "s1", config=ON, now=NOW, name: str = "stop.jsonl", **extra) -> bool:
    payload = {"hook_event_name": "Stop", "session_id": session, "cwd": "/work/app",
               "transcript_path": _transcript(tmp_path, records, name), **extra}
    return HOOK.keep_reply(payload, config, CATALOGUE, _config_dir(tmp_path), now=now)


def _kept(tmp_path, session: str = "s1") -> dict | None:
    config_dir = tmp_path / "claudeglass"
    state = json.loads((config_dir / cat.COACH_STATE_FILE).read_text(encoding="utf-8"))
    return state["sessions"][HOOK._session_key(session, config_dir)].get("last")


def _replayed(first: dict, second: dict) -> list[dict]:
    """Two replies, then the first again as the desktop app re-emits it."""
    return [_prompt(), _with_uuid(first, "u1"), _with_uuid(second, "u2"), _with_uuid(first, "u1")]


def test_usage_limit_lines_after_a_reply_never_restart_the_cold_return_clock(tmp_path):
    # The last real reply was two hours ago; the turn since ended in usage-limit lines that reset an hour ago.
    records = [_prompt(), _reply(200_000, ago_s=7_200), _limit_line(5_000, resets_in_s=-3_600),
               _limit_line(4_900, tokens=5_000, resets_in_s=-3_600)]
    note = _send(tmp_path, records, "try again")
    # Timed from the real reply, and the context that reply left, less the shared start.
    assert _kind(note) == "cold_return" and "idle for 2 hours" in note and "about 158k tokens" in note


@pytest.mark.parametrize("name, limit, expected", [
    # You come back to a stop the limit made: the wait was the limit's, not a break you took.
    ("a reset not known", dict(), ""),
    ("a reset still ahead", dict(resets_in_s=1_800), ""),
    ("a reset that just passed", dict(resets_in_s=-60), ""),
    ("a reset a cache lifetime ago", dict(resets_in_s=-300), ""),
    ("a weekly limit just reset", dict(text=_WEEKLY_LIMIT, resets_in_s=-120), ""),
    # Later than that, the limit's wait is over and what follows is a break like any other.
    ("a reset just over a cache lifetime ago", dict(resets_in_s=-301), "cold_return"),
    ("a reset an hour ago", dict(resets_in_s=-3_600), "cold_return"),
    ("a weekly limit that reset hours ago", dict(text=_WEEKLY_LIMIT, resets_in_s=-14_400), "cold_return"),
    # Not a usage limit: an overload, or an unknown line, is no reason to stay silent.
    ("an overload", dict(text="API Error: 529 Overloaded"), "cold_return"),
    ("a line that isn't a limit", dict(text="Usage limit reached."), "cold_return"),
])
def test_a_return_soon_after_a_usage_limit_reset_gets_no_cold_return_receipt(tmp_path, name, limit, expected):
    # Last real reply two hours ago, on the 5-minute cache; then a usage limit stopped Claude.
    records = [_prompt(), _reply(200_000, ago_s=7_200), _limit_line(5_000, **limit)]
    case = tmp_path / "case"
    case.mkdir()
    note = _send(case, records, "try again")
    assert (_kind(note) if note else "") == expected, name


def test_a_limit_line_from_before_the_reply_is_no_stop_and_a_longer_cache_waits_longer(tmp_path):
    # A limit line from before the last real reply says nothing about the wait since it.
    before = [_prompt(), _limit_line(9_000), _reply(200_000, ago_s=7_200)]
    assert _kind(_send(tmp_path, before, "next", session="a")) == "cold_return"
    # On the 1-hour cache a reset that is 30 minutes old is still within the cache's life: silent.
    hour = [_prompt(), _reply(200_000, ago_s=10_000, one_hour=True), _limit_line(5_000, resets_in_s=-1_800)]
    assert _send(tmp_path, hour, "next", session="b") == ""
    # An hour and a half on it is not.
    later = [_prompt(), _reply(200_000, ago_s=10_000, one_hour=True), _limit_line(5_000, resets_in_s=-5_400)]
    assert _kind(_send(tmp_path, later, "next", session="c")) == "cold_return"


def test_the_cold_return_receipt_takes_the_context_the_reply_left_less_the_shared_start(tmp_path):
    reply = _reply(150_000, ago_s=3_600)
    reply["message"]["usage"]["output_tokens"] = 6_000
    note = _send(tmp_path, [_prompt(), reply], "next")
    # 150k read plus 6k written is the context the next call reads, less the 42k every session starts with.
    assert "about 114k tokens" in note


def test_the_cold_return_receipt_counts_the_compactions_as_one_clause(tmp_path):
    before = [_prompt(), _compact(20_000, "c1"), _reply(150_000, ago_s=1_200)]
    assert "in a session already compacted once. " in _send(tmp_path, before, "next", session="a")
    twice = [_prompt(), _compact(30_000, "c1"), _compact(30_000, "c1"), _compact(20_000, "c2"),
             _reply(150_000, ago_s=1_200)]
    assert "in a session already compacted twice. " in _send(tmp_path, twice, "next", session="b")
    thrice = [_prompt(), _compact(40_000, "c1"), _compact(30_000, "c2"), _compact(20_000, "c3"),
              _reply(150_000, ago_s=1_200)]
    assert "already compacted 3 times. " in _send(tmp_path, thrice, "next", session="c")
    # A session never compacted has no clause.
    note = _send(tmp_path, [_prompt(), _reply(150_000, ago_s=1_200)], "next", session="d")
    assert "compacted" not in note and "tokens of context again. If you came back" in note


def test_a_compaction_after_the_last_reply_leaves_no_receipt_for_that_reply(tmp_path):
    # The summary replaced the context the reply read: what a cold cache costs now is a summary's worth.
    after = [_prompt(), _reply(150_000, ago_s=7_200), _compact(3_600, "c1")]
    assert _send(tmp_path, after, "next", session="a") == ""
    # With no time of its own, the boundary is judged by where it sits.
    undated = [_prompt(), _reply(150_000, ago_s=7_200), _compact(None)]
    assert _send(tmp_path, undated, "next", session="b") == ""
    # One before that reply leaves its context as it was.
    before = [_prompt(), _compact(None), _reply(150_000, ago_s=7_200)]
    assert _kind(_send(tmp_path, before, "next", session="c")) == "cold_return"


def test_a_message_sent_while_claude_was_working_gets_no_cold_return_receipt(tmp_path):
    def call(ago_s):
        return _reply(150_000, ago_s=ago_s, content=[{"type": "tool_use", "id": "toolu_q", "name": "Read", "input": {}}])

    def result(ago_s):
        return {"type": "user", "timestamp": _iso(NOW - timedelta(seconds=ago_s)), "message": {
            "role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_q", "content": "ok"}]}}

    # A call or a result 8 minutes old (under ``queued_minutes``) is work under way, though the 5-minute cache
    # has gone cold: the message is a nudge, not a return.
    assert _send(tmp_path, [_prompt(), call(480)], "also check the footer", session="a") == ""
    assert _send(tmp_path, [_prompt(), call(500), result(480)], "also check the footer", session="b") == ""
    # Your own number of minutes sets how long a call stays work under way.
    shorter = {**ON, "thresholds": {"coaching_queued_minutes": 5}}
    assert _kind(_send(tmp_path, [_prompt(), call(480)], "also check the footer", session="f", config=shorter)) == "cold_return"
    # The same break with a finished reply gets it.
    assert _kind(_send(tmp_path, [_prompt(), _reply(150_000, ago_s=7_200)], "next", session="c")) == "cold_return"
    # And so does a message typed hours after a call or a result: that is where work stopped, not work under way.
    assert _kind(_send(tmp_path, [_prompt(), call(7_200)], "next", session="d")) == "cold_return"
    assert _kind(_send(tmp_path, [_prompt(), call(7_300), result(7_200)], "next", session="e")) == "cold_return"


def test_a_subagent_call_with_no_result_yet_is_work_under_way_however_old_it_is(tmp_path):
    def call(name, ago_s):
        return _reply(150_000, ago_s=ago_s, content=[{"type": "tool_use", "id": "toolu_a", "name": name, "input": {}}])

    prefix = CATALOGUE["coaching"]["interrupt_prefix"]
    max_age_s = cat.COACHING_THRESHOLDS["queued_minutes"] * 60
    # A subagent runs as long as it needs: a message typed 30 minutes into one is sent to work under way.
    for name in cat.AGENT_TOOLS:
        assert HOOK._queued([call(name, 1_800)], prefix, NOW, max_age_s), name
    # Any other call that old is where work stopped.
    assert not HOOK._queued([call("Read", 1_800)], prefix, NOW, max_age_s)
    # Once the subagent's result is in, the age limit applies as it does to any result.
    result = {"type": "user", "timestamp": _iso(NOW - timedelta(seconds=1_700)), "message": {
        "role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_a", "content": "ok"}]}}
    assert not HOOK._queued([call("Agent", 1_800), result], prefix, NOW, max_age_s)
    # So the message gets a nudge's treatment: no cold_return receipt, 150k of context and 30 minutes on.
    assert _send(tmp_path, [_prompt(), call("Agent", 1_800)], "also check the footer", session="a") == ""
    assert _kind(_send(tmp_path, [_prompt(), call("Read", 1_800)], "next", session="b")) == "cold_return"


def test_a_subagent_launched_beside_another_tool_is_work_under_way_however_old_it_is(tmp_path):
    def call(name, tool_id, message_id, ago_s=1_800):
        return _reply(150_000, ago_s=ago_s, message_id=message_id,
                      content=[{"type": "tool_use", "id": tool_id, "name": name, "input": {}}])

    prefix = CATALOGUE["coaching"]["interrupt_prefix"]
    max_age_s = cat.COACHING_THRESHOLDS["queued_minutes"] * 60
    # Each tool call of one reply is a line of its own with the reply's message id, and the results come
    # together at the end: the newest line is the Bash call, but the subagent started on the one before.
    for name in cat.AGENT_TOOLS:
        beside = [call(name, "toolu_a", "msg_pair"), call("Bash", "toolu_b", "msg_pair")]
        assert HOOK._queued(beside, prefix, NOW, max_age_s), name
        assert _send(tmp_path, [_prompt(), *beside], "also check the footer", session=name) == ""
    # Another reply's subagent is no part of this one: a Bash call alone that old is where work stopped.
    other = [call("Agent", "toolu_a", "msg_one"), call("Bash", "toolu_b", "msg_two")]
    assert not HOOK._queued(other, prefix, NOW, max_age_s)
    assert not HOOK._queued([call("Read", "toolu_a", "msg_pair"), call("Bash", "toolu_b", "msg_pair")], prefix,
                            NOW, max_age_s)
    # A line with no message id can't be matched to it.
    bare = [call("Agent", "toolu_a", "msg_pair"), call("Bash", "toolu_b", "msg_pair")]
    bare[1]["message"].pop("id")
    assert not HOOK._queued(bare, prefix, NOW, max_age_s)


def test_a_message_you_did_not_type_gets_no_cold_return_receipt(tmp_path):
    records = [_prompt(), _reply(150_000, ago_s=7_200)]
    for n, prefix in enumerate(cat.NOT_TYPED_PREFIXES):
        assert _send(tmp_path, records, f"{prefix} x", session=f"auto{n}") == "", prefix


def test_the_cold_return_receipt_does_not_follow_the_plan_fresh_setting(tmp_path):
    # The plan_fresh switch is about plans; the receipt is about breaks.
    (_config_dir(tmp_path) / cat.COACHING_FILE).write_text(json.dumps({"plan_fresh": False}), encoding="utf-8")
    assert _kind(_send(tmp_path, [_prompt(), _reply(150_000, ago_s=7_200)], "next")) == "cold_return"


def test_the_cold_return_receipt_and_the_fresh_start_hints_share_one_rest(tmp_path):
    cooldown = cat.COACHING_THRESHOLDS["cooldown_minutes"]
    cold = [_prompt(), _reply(150_000, ago_s=1_200)]
    assert _kind(_send(tmp_path, cold, "next")) == "cold_return"
    # It said "start fresh": the planning hint that says it too rests the cooldown.
    assert _planning(tmp_path, name="after.jsonl") == ""
    # And the other way round: a planning hint rests the receipt, then the cooldown lifts it.
    assert _kind(_planning(tmp_path, session="s2")) == "plan_fresh_early"
    assert _send(tmp_path, cold, "next", session="s2") == ""
    later = NOW + timedelta(minutes=cooldown + 1)
    assert _kind(_send(tmp_path, cold, "next", session="s2", now=later)) == "cold_return"


def test_a_cold_return_receipt_rests_the_planning_hints_for_the_cooldown_only(tmp_path):
    cooldown = cat.COACHING_THRESHOLDS["cooldown_minutes"]
    assert _kind(_send(tmp_path, [_prompt(), _reply(150_000, ago_s=1_200)], "next")) == "cold_return"
    later = NOW + timedelta(minutes=cooldown + 1)
    assert _kind(_planning(tmp_path, name="later.jsonl", now=later)) == "plan_fresh_early"


def test_a_tail_ending_in_a_replay_with_nothing_kept_leaves_no_receipt(tmp_path):
    records = _replayed(_reply(150_000, ago_s=10_000), _reply(150_000, ago_s=7_200))
    # Which reply was the last isn't known: better silent than a wrong break.
    assert _send(tmp_path, records, "next") == ""
    # The same lines without the repeat are a plain return.
    assert _kind(_send(tmp_path, records[:-1], "next", session="s2")) == "cold_return"


def test_a_replay_cannot_make_a_warm_cache_look_cold(tmp_path):
    # Stop kept the real last reply, four minutes old.
    assert _stop(tmp_path, [_prompt(), _reply(150_000, ago_s=240)])
    records = _replayed(_reply(150_000, ago_s=10_000), _reply(150_000, ago_s=7_200))
    assert _send(tmp_path, records, "next") == ""


def test_the_receipt_is_timed_from_the_reply_the_coach_state_kept(tmp_path):
    assert _stop(tmp_path, [_prompt(), _reply(180_000, ago_s=4_200)])
    # The tail ends in a replay of an older reply: the state's reply is the one the break runs from.
    records = _replayed(_reply(150_000, ago_s=10_000), _reply(150_000, ago_s=9_000))
    note = _send(tmp_path, records, "next")
    assert _kind(note) == "cold_return" and "idle for 70 minutes" in note and "about 138k tokens" in note


def test_the_whole_of_a_long_tail_is_read_for_the_last_reply(tmp_path):
    filler = {"type": "progress", "data": "x" * 9_000}
    # More than the 256 KB a tool call reads, well inside the 4 MB a message does.
    far = [_prompt(), _reply(150_000, ago_s=7_200), *([filler] * 40)]
    assert _kind(_send(tmp_path, far, "next", session="a")) == "cold_return"
    # Beyond the 4 MB there is no reply to time a break from.
    beyond = [_prompt(), _reply(150_000, ago_s=7_200), *([filler] * 500)]
    assert _send(tmp_path, beyond, "next", session="b") == ""


def test_a_tool_call_keeps_the_newest_reply_in_the_coach_state_and_says_nothing(tmp_path):
    path = _transcript(tmp_path, [_prompt(), _reply(150_000, ago_s=7_200, one_hour=True)])
    read = {"hook_event_name": "PostToolUse", "tool_name": "Read", "tool_input": {}, "transcript_path": path}
    assert _coach(tmp_path, read, result_len=400) == ""
    assert _kept(tmp_path) == {"at": (NOW - timedelta(seconds=7_200)).timestamp(), "ctx": 150_000, "ttl": 3_600}


def test_a_subagents_tool_call_keeps_nothing_of_the_main_sessions_reply(tmp_path):
    session, _ = _agent(tmp_path, [_prompt(), _reply(1_000, message_id="m1")])
    call = {"hook_event_name": "PostToolUse", "tool_name": "Grep", "transcript_path": session, "agent_id": "abc",
            "agent_type": "general-purpose"}
    assert _coach(tmp_path, call, result_len=400) == ""
    assert not (tmp_path / "claudeglass" / cat.COACH_STATE_FILE).exists()


def test_stop_keeps_the_real_reply_when_the_turn_ended_in_usage_limit_lines(tmp_path):
    assert _stop(tmp_path, [_prompt(), _reply(200_000, ago_s=7_200), _limit_line(40), _limit_line(20, tokens=5_000)])
    assert _kept(tmp_path) == {"at": (NOW - timedelta(seconds=7_200)).timestamp(), "ctx": 200_000, "ttl": 300}
    # A turn that is only limit lines has no reply: nothing to keep, nothing changes.
    assert not _stop(tmp_path, [_prompt(), _limit_line(20)], name="limit.jsonl")
    assert _kept(tmp_path)["ctx"] == 200_000


def test_stop_keeps_numbers_only_and_never_moves_back_to_an_older_reply(tmp_path):
    newer = _reply(160_000, ago_s=120, one_hour=True)
    newer["message"]["usage"]["output_tokens"] = 2_000
    assert _stop(tmp_path, [_prompt("a secret request"), _reply(150_000, ago_s=600), newer])
    assert _kept(tmp_path) == {"at": (NOW - timedelta(seconds=120)).timestamp(), "ctx": 162_000, "ttl": 3_600}
    state_text = (tmp_path / "claudeglass" / cat.COACH_STATE_FILE).read_text(encoding="utf-8")
    assert "secret" not in state_text
    # The same reply again changes nothing, and nor does a replay that ends on an older one.
    assert not _stop(tmp_path, [_prompt(), newer])
    assert not _stop(tmp_path, _replayed(newer, _reply(150_000, ago_s=700)), name="replay.jsonl")
    assert _kept(tmp_path)["ctx"] == 162_000
    # A later reply replaces it.
    assert _stop(tmp_path, [_prompt(), newer, _reply(170_000, ago_s=30)], name="later.jsonl")
    assert _kept(tmp_path)["ctx"] == 170_000


def test_stop_does_nothing_for_a_subagent_a_session_without_coaching_or_a_missing_transcript(tmp_path):
    records = [_prompt(), _reply(150_000, ago_s=600)]
    assert not _stop(tmp_path, records, agent_id="abc")
    assert not _stop(tmp_path, records, config={"capture": {"level": "deep"}})
    assert not _stop(tmp_path, records, session="")
    assert not HOOK.keep_reply({"hook_event_name": "Stop", "session_id": "s1", "cwd": "/w"}, ON, CATALOGUE,
                               _config_dir(tmp_path), now=NOW)
    assert not (tmp_path / "claudeglass" / cat.COACH_STATE_FILE).exists()


# -- what a replay does to the order of a transcript's lines ------------------------


def _stamped(kind: str, at_s: float, uuid: str | None = None, **extra) -> dict:
    record = {"type": kind, "timestamp": _iso(NOW + timedelta(seconds=at_s)), **extra}
    return {**record, "uuid": uuid} if uuid else record


_RESULT = {"message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "x"}]}}
_TYPED = {"message": {"role": "user", "content": "hello"}}


def _ids(records) -> list:
    return [r.get("uuid") or r["type"] for r in records]


def test_a_line_written_again_with_the_same_uuid_is_dropped():
    records = [_stamped("assistant", 0, "u1"), _stamped("assistant", 10, "u2"), _stamped("assistant", 0, "u1")]
    kept, replayed = HOOK._ordered(records)
    assert _ids(kept) == ["u1", "u2"] and replayed


def test_a_line_stamped_behind_the_newest_is_a_replay_whatever_its_kind():
    kept, replayed = HOOK._ordered([_stamped("assistant", 100, "a"), _stamped("assistant", 50, "b")])
    assert _ids(kept) == ["a"] and replayed
    mixed = [_stamped("assistant", 100, "a"), _stamped("system", 10, "b"), _stamped("user", 20, "c", **_RESULT)]
    kept, replayed = HOOK._ordered(mixed)
    assert _ids(kept) == ["a"] and replayed


def test_a_line_a_moment_behind_is_not_a_replay_and_a_typed_one_never_is():
    # Two writers of one transcript are a moment apart.
    kept, replayed = HOOK._ordered([_stamped("assistant", 100, "a"), _stamped("assistant", 99.5, "b")])
    assert _ids(kept) == ["a", "b"] and not replayed
    # Claude Code stamps what you typed while it worked with the moment you typed it.
    kept, replayed = HOOK._ordered([_stamped("assistant", 100, "a"), _stamped("user", 50, "b", **_TYPED)])
    assert _ids(kept) == ["a", "b"] and not replayed


def test_only_the_last_conversation_line_decides_whether_the_tail_ends_in_a_replay():
    early = _stamped("queue-operation", -500)
    # A line that isn't part of the conversation carries the time it was queued: neither dropped nor judged.
    kept, replayed = HOOK._ordered([_stamped("assistant", 100, "a"), early])
    assert _ids(kept) == ["a", "queue-operation"] and not replayed
    kept, replayed = HOOK._ordered([_stamped("assistant", 100, "a"), _stamped("assistant", 50, "b"), early])
    assert _ids(kept) == ["a", "queue-operation"] and replayed
    # New lines after the repeated ones end the replay.
    kept, replayed = HOOK._ordered(
        [_stamped("assistant", 100, "a"), _stamped("assistant", 50, "b"), _stamped("assistant", 150, "c")])
    assert _ids(kept) == ["a", "c"] and not replayed
    # Lines with no time or no id are all kept.
    kept, replayed = HOOK._ordered([{"type": "assistant"}, {"type": "assistant"}, {"type": "user"}])
    assert len(kept) == 3 and not replayed


# -- status_poll: asking how it's going while work runs in the background ---------------

_LAUNCH_RESULTS = {
    "shell": "Command running in background with ID: bk1a2b. Output is being written to a file.",
    "timeout": "Command 'make' was moved to the background (ID: bk1a2b) after it ran past its time limit.",
    "agent": "Async agent launched successfully. It works in the background.",
    "workflow": "Workflow launched in background. Task ID: wf1a2b",
}

#: Each way Claude Code records that a background task finished.
_NOTIFICATIONS = {
    "user line by origin": {"type": "user", "origin": {"kind": "task-notification"},
                            "message": {"role": "user", "content": "done"}},
    "user line by turn origin": {"type": "user", "turnOrigin": "task_notification",
                                 "message": {"role": "user", "content": "done"}},
    "user line by text": {"type": "user", "message": {"role": "user", "content": [
        {"type": "text", "text": "<task-notification><task-id>bk1a2b</task-id></task-notification>"}]}},
    "queue operation": {"type": "queue-operation", "operation": "enqueue",
                        "content": "<task-notification><task-id>bk1a2b</task-id></task-notification>"},
    "queued command": {"type": "attachment", "attachment": {
        "type": "queued_command", "prompt": "<task-notification><task-id>bk1a2b</task-id></task-notification>"}},
    "queued command in blocks": {"type": "attachment", "attachment": {
        "type": "queued_command", "prompt": [{"type": "text", "text": "<task-notification>x</task-notification>"}]}},
}


def _went_to_background(result: str, *, ago_s: float = 200, ctx: int = 50_000, as_blocks: bool = False,
                        flag: bool = True, sidechain: bool = False, call_id: str = "toolu_bg") -> list[dict]:
    """A reply that starts work, the tool result saying where it went, and a
    short reply after it. Every reply on the 5-minute cache."""
    tool_input = {"command": "make", **({"run_in_background": True} if flag else {})}
    call = _reply(ctx - 10_000, ago_s=ago_s, message_id=f"msg_bg{ago_s}",
                  content=[{"type": "tool_use", "id": call_id, "name": "Bash", "input": tool_input}])
    result_content = [{"type": "text", "text": result}] if as_blocks else result
    back = {"type": "user", "toolUseResult": {}, "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": call_id, "content": result_content}]}}
    if sidechain:
        back["isSidechain"] = True
    said = _reply(ctx, ago_s=ago_s - 60, message_id=f"msg_say{ago_s}", content=[{"type": "text", "text": "Started it."}])
    return [call, back, said]


@pytest.mark.parametrize("wording", list(_LAUNCH_RESULTS))
def test_asking_how_it_is_going_while_work_runs_in_the_background_gets_the_status_poll_hint(tmp_path, wording):
    for n, prompt in enumerate(("how is it going?", "any updates?", "is it done yet")):
        note = _send(tmp_path, [_prompt(), *_went_to_background(_LAUNCH_RESULTS[wording])], prompt, session=f"s{n}")
        assert _kind(note) == "status_poll" and "about 50k tokens" in note, prompt
        # It never promises a notification: a task only sends one when it finishes, and not every kind does.
        assert "notif" not in note.lower()


def test_the_status_poll_names_where_to_look_in_the_app_you_use(tmp_path, monkeypatch):
    records = [_prompt(), *_went_to_background(_LAUNCH_RESULTS["shell"])]
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "claude-desktop")
    assert _send(tmp_path, records, "how is it going?", session="a").endswith(cat.COACHING_HOW["poll_how"]["desktop"])
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT")
    assert _send(tmp_path, records, "how is it going?", session="b").endswith(cat.COACHING_HOW["poll_how"]["terminal"])
    assert "task panel" in cat.COACHING_HOW["poll_how"]["desktop"] and "/tasks" in cat.COACHING_HOW["poll_how"]["terminal"]


def test_a_launch_result_in_text_blocks_is_found_too(tmp_path):
    records = [_prompt(), *_went_to_background(_LAUNCH_RESULTS["agent"], as_blocks=True, flag=False)]
    assert _kind(_send(tmp_path, records, "how is it going?")) == "status_poll"


@pytest.mark.parametrize("shape", list(_NOTIFICATIONS))
def test_a_finished_background_task_ends_the_status_poll_hint(tmp_path, shape):
    started = [_prompt(), *_went_to_background(_LAUNCH_RESULTS["shell"])]
    assert _kind(_send(tmp_path, started, "how is it going?", session="a")) == "status_poll"
    # The task's own message came in after it started: nothing is running that Claude doesn't know of.
    assert _send(tmp_path, [*started, _NOTIFICATIONS[shape]], "how is it going?", session="b") == ""
    # A second launch after it is pending again.
    again = [*started, _NOTIFICATIONS[shape], *_went_to_background(_LAUNCH_RESULTS["agent"], ago_s=120, call_id="toolu_b2")]
    assert _kind(_send(tmp_path, again, "how is it going?", session="c")) == "status_poll"


def test_the_status_poll_matches_what_a_result_says_not_what_the_call_asked_for(tmp_path):
    # The call asked for the background, the result says it ran to the end.
    ran = _went_to_background("Command finished with exit code 0.", flag=True)
    assert _send(tmp_path, [_prompt(), *ran], "how is it going?", session="a") == ""
    # Only the start of the result is read for the wording.
    late = _went_to_background("x" * 1_000 + " " + _LAUNCH_RESULTS["shell"])
    assert _send(tmp_path, [_prompt(), *late], "how is it going?", session="b") == ""
    # A subagent's own results are not the session's work.
    side = _went_to_background(_LAUNCH_RESULTS["shell"], sidechain=True)
    assert _send(tmp_path, [_prompt(), *side], "how is it going?", session="c") == ""
    # Nothing was started.
    assert _send(tmp_path, [_prompt(), _spoke(100)], "how is it going?", session="d") == ""


def test_the_status_poll_needs_a_message_that_only_asks_how_it_is_going(tmp_path):
    records = [_prompt(), *_went_to_background(_LAUNCH_RESULTS["shell"])]
    assert _send(tmp_path, records, "now add a test for the footer", session="a") == ""
    assert _send(tmp_path, records, "how is it going? please also rename the footer", session="b") == ""
    # Not one you typed.
    assert _send(tmp_path, records, "<task-notification>how is it going?", session="c") == ""


def test_after_a_break_the_cold_return_receipt_speaks_instead_of_the_status_poll(tmp_path):
    records = [_prompt(), *_went_to_background(_LAUNCH_RESULTS["shell"], ctx=150_000)]
    assert _kind(_send(tmp_path, records, "how is it going?", session="a")) == "status_poll"
    later = NOW + timedelta(minutes=10)
    assert _kind(_send(tmp_path, records, "how is it going?", session="b", now=later)) == "cold_return"
    # With a context too small for a receipt, a message after the break is silent.
    small = [_prompt(), *_went_to_background(_LAUNCH_RESULTS["shell"], ctx=50_000)]
    assert _send(tmp_path, small, "how is it going?", session="c", now=later) == ""


def test_a_message_sent_while_claude_was_working_gets_no_status_poll_hint(tmp_path):
    records = [_prompt(), *_went_to_background(_LAUNCH_RESULTS["shell"])[:2]]
    assert _send(tmp_path, records, "how is it going?") == ""


def test_the_status_poll_hint_rests_and_comes_back_when_the_context_has_grown(tmp_path):
    cooldown = cat.COACHING_THRESHOLDS["cooldown_minutes"]

    def poll(*, ctx: int = 50_000, minutes: float = 0):
        records = [_prompt(), *_went_to_background(_LAUNCH_RESULTS["shell"], ctx=ctx, ago_s=200 - minutes * 60)]
        return _send(tmp_path, records, "how is it going?", now=NOW + timedelta(minutes=minutes))

    assert _kind(poll()) == "status_poll"
    assert poll(minutes=1) == ""
    assert _kind(poll(ctx=80_000, minutes=2)) == "status_poll"
    # The second time, it rests twice the cooldown.
    assert poll(minutes=2 + cooldown + 1, ctx=80_000) == ""
    assert _kind(poll(minutes=2 + 2 * cooldown + 1, ctx=80_000)) == "status_poll"


def test_the_status_poll_note_carries_a_count_and_never_your_words(tmp_path):
    records = [_prompt("a secret plan"), *_went_to_background(_LAUNCH_RESULTS["shell"])]
    note = _send(tmp_path, records, "how is it going?")
    assert "secret" not in note and "bk1a2b" not in note


# -- the hook, run as Claude Code runs it ------------------------------------------


def _undated(record: dict) -> dict:
    """A reply without a time, so the real clock the hook runs on can't make
    its cache look cold."""
    return {key: value for key, value in record.items() if key != "timestamp"}


def _run(config_dir: Path, payload: dict, entrypoint: str = "") -> tuple[int, str, str]:
    # The app the hook runs in decides who sees a tip (see ``delivery``): never the one this shell runs in.
    env = {key: value for key, value in os.environ.items() if key != "CLAUDE_CODE_ENTRYPOINT"}
    if entrypoint:
        env["CLAUDE_CODE_ENTRYPOINT"] = entrypoint
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "--config-dir", str(config_dir)],
        input=json.dumps(payload).encode("utf-8"),
        capture_output=True,
        timeout=30,
        env=env,
    )
    return done.returncode, done.stdout.decode("utf-8"), done.stderr.decode("utf-8")


def test_the_hook_runs_coaching_notes_with_capture_off(tmp_path):
    config_dir = _config_dir(tmp_path)
    (config_dir / "config.toml").write_text('[capture]\nlevel = "off"\ncoaching = ["coaching_notes"]\n', encoding="utf-8")
    # The hook runs on the real clock, so a reply dated in 2026 left the cache cold long ago.
    path = _transcript(tmp_path, [_prompt(), _reply(150_000)])
    rc, out, err = _run(config_dir, {"session_id": "s1", "cwd": "/w", **_prompt_payload(path)})
    assert rc == 0 and err == ""
    output = json.loads(out)["hookSpecificOutput"]
    assert output["hookEventName"] == "UserPromptSubmit"
    assert _kind(output["additionalContext"]) == "cold_return"
    # Outside the desktop app the same tip shows at once as a notice.
    notice = json.loads(out)["systemMessage"]
    assert notice.startswith(cat.NOTICE_LABEL) and "idle for" in notice and "about 108k tokens" in notice


def _drip_feed_payload(tmp_path: Path) -> dict:
    """Three small changes in a row, timed on the real clock the hook runs
    on, so the next message draws ``drip_feed``."""
    now = datetime.now(timezone.utc)

    def said(text, minutes):
        return {**_prompt(text), "timestamp": _iso(now - timedelta(minutes=minutes))}

    def changed(minutes):
        # Timed on the real clock, with the 1-hour cache so it isn't cold.
        return {**_changed(0, 20_000, one_hour=True), "timestamp": _iso(now - timedelta(minutes=minutes))}

    records = [said("Build the settings page: " + "a form and a header. " * 20, 15), changed(14),
               said("make the save button bigger", 10), changed(9), said("now move the logo", 5), changed(4)]
    return {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "cwd": "/w",
            "transcript_path": _transcript(tmp_path, records), "prompt": "and make the footer text bigger"}


def _paste_payload(tmp_path: Path) -> dict:
    """A message of about 12k tokens, so the next message draws ``big_paste``
    whatever the clock."""
    return {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "cwd": "/w",
            "transcript_path": _transcript(tmp_path, [_said("hi", 200)]), "prompt": "x" * 48_000}


def _coaching_config(config_dir: Path) -> Path:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.toml").write_text('[capture]\nlevel = "off"\ncoaching = ["coaching_notes"]\n', encoding="utf-8")
    return config_dir


def test_a_prompting_hint_also_shows_you_a_notice_at_once(tmp_path):
    config_dir = _coaching_config(_config_dir(tmp_path))
    rc, out, err = _run(config_dir, _drip_feed_payload(tmp_path))
    assert rc == 0 and err == ""
    output = json.loads(out)
    assert _kind(output["hookSpecificOutput"]["additionalContext"]) == "drip_feed"
    assert output["systemMessage"] == cat.COACHING_NOTICE["drip_feed"].format(count=3, ctx="20k")
    assert "footer" not in out
    # The same for a message too big to paste.
    (tmp_path / "paste").mkdir()
    rc, out, err = _run(_coaching_config(_config_dir(tmp_path / "paste")), _paste_payload(tmp_path / "paste"))
    assert rc == 0 and err == ""
    output = json.loads(out)
    assert _kind(output["hookSpecificOutput"]["additionalContext"]) == "big_paste"
    assert output["systemMessage"] == cat.COACHING_NOTICE["big_paste"].format(tokens="12k")
    assert "xxxx" not in out


#: Every placeholder a tip or its note can carry, filled the way the hook does.
_FIELDS = {
    "idle": "40 minutes", "ctx": "150k", "kept": "90k", "count": 3, "tokens": "12k", "compacted": "",
    **{key: variants["terminal"] for key, variants in cat.COACHING_HOW.items()},
}
_TIP_HINTS = [hint for hint, text in cat.COACHING_TEXT.items() if cat.TIP_LABEL in text]


@pytest.mark.parametrize("hint", _TIP_HINTS)
def test_a_tip_note_says_to_write_the_tip_first_and_ends_on_the_tip(hint):
    # The desktop app hides a hook's systemMessage, so the note is the only way
    # the tip reaches the user: its first sentence orders the tip written, and
    # its last line is the tip, word for word, for Claude to copy.
    lines = cat.COACHING_TEXT[hint].format(**_FIELDS).split("\n")
    assert len(lines) == 2 and lines[-1] == f"{cat.TIP_LABEL} {cat.COACHING_TIP[hint].format(**_FIELDS)}"
    first = re.split(r"(?<=[.!?])\s", lines[0])[0]
    assert "write the tip on this note's last line" in first.lower(), hint
    assert cat.TIP_LABEL not in lines[0]
    # A hint Claude is to judge opens with the condition, not the order.
    assert first.startswith("If ") == (hint in cat.CONDITIONAL_TIP_HINTS), hint


def test_the_tip_hints_are_split_into_unconditional_and_conditional():
    assert set(cat.COACHING_TIP) == set(_TIP_HINTS)
    assert set(cat.CONDITIONAL_TIP_HINTS) == {"big_paste"}
    assert set(cat.CONDITIONAL_TIP_HINTS) <= set(_TIP_HINTS)
    unconditional = set(_TIP_HINTS) - set(cat.CONDITIONAL_TIP_HINTS)
    assert unconditional == {"plan_fresh", "plan_fresh_early", "drip_feed", "status_poll", "cold_return"}
    # A conditional note tells Claude when to stay quiet; an unconditional one never does.
    for hint in _TIP_HINTS:
        assert ("don't mention this note" in cat.COACHING_TEXT[hint]) == (hint in cat.CONDITIONAL_TIP_HINTS), hint


def test_a_notice_is_the_tip_behind_the_notice_label():
    for hint in cat.NOTICE_HINTS:
        assert cat.COACHING_NOTICE[hint] == cat.NOTICE_LABEL + cat.COACHING_TIP[hint], hint
    # Every hint with a tip has a notice, and no other hint but the subagent split does.
    assert set(cat.NOTICE_HINTS) == set(cat.COACHING_TIP)
    assert set(cat.COACHING_NOTICE) == set(cat.COACHING_TIP) | {"split_run"}


@pytest.mark.parametrize("entrypoint, note, notice, shown", [
    ("claude-desktop", "a note", "a notice", ""),
    # A hint with no note to Claude has no other way to reach you.
    ("claude-desktop", "", "a notice", "a notice"),
    ("cli", "a note", "a notice", "a notice"),
    ("vscode", "a note", "a notice", "a notice"),
    ("", "a note", "a notice", "a notice"),
    ("cli", "a note", "", ""),
])
def test_the_desktop_app_gets_no_notice_where_a_note_carries_the_tip(monkeypatch, entrypoint, note, notice, shown):
    if entrypoint:
        monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", entrypoint)
    else:
        monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    assert HOOK.delivery(note, notice) == shown


@pytest.mark.parametrize("entrypoint", ["claude-desktop", "cli", ""])
def test_a_tip_always_reaches_claude_and_a_notice_follows_outside_the_desktop_app(tmp_path, entrypoint):
    config_dir = _coaching_config(tmp_path / (entrypoint or "unset") / "claudeglass")
    rc, out, err = _run(config_dir, _drip_feed_payload(tmp_path), entrypoint)
    assert rc == 0 and err == ""
    output = json.loads(out)
    note = output["hookSpecificOutput"]["additionalContext"]
    assert _kind(note) == "drip_feed"
    assert note.splitlines()[-1] == f"{cat.TIP_LABEL} {cat.COACHING_TIP['drip_feed'].format(count=3, ctx='20k')}"
    if entrypoint == "claude-desktop":
        assert "systemMessage" not in output
    else:
        assert output["systemMessage"] == cat.NOTICE_LABEL + cat.COACHING_TIP["drip_feed"].format(count=3, ctx="20k")


def test_a_subagent_notice_stays_where_the_desktop_app_cannot_show_it(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "claude-desktop")
    config_dir = _config_dir(tmp_path)
    (config_dir / cat.COACHING_FILE).write_text(json.dumps({"split_run": {"general-purpose": 1}}), encoding="utf-8")
    session, _ = _agent(tmp_path, [_prompt(), _reply(1_000, message_id="m1")])
    call = {"hook_event_name": "PostToolUse", "tool_name": "Grep", "transcript_path": session, "agent_id": "abc",
            "agent_type": "general-purpose", "session_id": "s1", "cwd": "/work/app"}
    note, notice = HOOK.coaching_for(call, ON, CATALOGUE, config_dir, now=NOW)
    assert note == "" and HOOK.delivery(note, notice) == notice != ""


def test_a_tip_for_the_user_is_a_highlighted_block_and_the_notices_are_the_prompting_hints():
    to_the_user = {"plan_fresh", "plan_fresh_early", "cold_return", "status_poll", "drip_feed", "big_paste"}
    for hint, text in cat.COACHING_TEXT.items():
        assert (cat.TIP_LABEL in text) == (hint in to_the_user), hint
    assert set(cat.COACHING_NOTICE) == {
        "plan_fresh", "plan_fresh_early", "cold_return", "status_poll", "drip_feed", "big_paste", "split_run"}
    # split_run tells the subagent nothing.
    assert cat.COACHING_TEXT["split_run"] == ""
    assert CATALOGUE["coaching"]["notice"] == cat.COACHING_NOTICE
    assert all(notice.startswith(cat.NOTICE_LABEL) for notice in cat.COACHING_NOTICE.values())
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
    for hint in ("drip_feed", "cold_return", "status_poll"):
        text = cat.COACHING_TEXT[hint]
        assert not steering.search(text), hint
        assert "changes nothing about the work" in text and cat.TIP_LABEL in text, hint
    assert not steering.search(cat.COACHING_TEXT["big_paste"])


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
    payload = _drip_feed_payload(tmp_path)
    note, notice = HOOK.coaching_for(payload, ON, CATALOGUE, _config_dir(tmp_path))
    assert _kind(note) == "drip_feed" and notice == cat.COACHING_NOTICE["drip_feed"].format(count=3, ctx="20k")
    big = {**_paste_payload(tmp_path), "session_id": "s2"}
    note, notice = HOOK.coaching_for(big, ON, CATALOGUE, _config_dir(tmp_path), now=NOW)
    assert _kind(note) == "big_paste" and notice == cat.COACHING_NOTICE["big_paste"].format(tokens="12k")
    assert notice.startswith("⚠️ ClaudeGlass: Your message is about 12k tokens")


def test_a_capture_note_and_a_coaching_note_go_out_as_one(tmp_path):
    config_dir = _config_dir(tmp_path)
    (config_dir / "config.toml").write_text('[capture]\nlevel = "deep"\ncoaching = ["coaching_notes"]\n', encoding="utf-8")
    big = {"session_id": "s1", "cwd": "/w", "hook_event_name": "PostToolUse", "tool_name": "Read",
           "tool_response": {"type": "text", "file": {"content": "x" * cat.BIG_OUTPUT_TOKENS * 4}}}
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
    path = _transcript(tmp_path, [_prompt(), _reply(150_000)])
    rc, out, err = _run(config_dir, {"session_id": "s1", **_prompt_payload(path)})
    assert rc == 0 and err == "" and _kind(json.loads(out)["hookSpecificOutput"]["additionalContext"]) == "cold_return"
    rc, out, err = _run(config_dir, {"session_id": "s1", "hook_event_name": "UserPromptSubmit", "transcript_path": 5})
    assert (rc, out, err) == (0, "", "")


def test_stop_keeps_the_newest_reply_and_prints_nothing(tmp_path):
    config_dir = _coaching_config(_config_dir(tmp_path))
    path = _transcript(tmp_path, [_prompt(), _reply(150_000, ago_s=7_200)])
    stop = {"hook_event_name": "Stop", "session_id": "s1", "cwd": "/w", "transcript_path": path}
    assert _run(config_dir, stop) == (0, "", "")
    assert _kept(tmp_path)["ctx"] == 150_000
    # A run with nobody at the screen keeps nothing.
    other = _coaching_config(tmp_path / "headless" / "claudeglass")
    assert _run(other, stop, "sdk-cli") == (0, "", "")
    assert not (other / cat.COACH_STATE_FILE).exists()


def test_a_tool_call_run_by_the_hook_keeps_the_reply_and_prints_nothing(tmp_path):
    config_dir = _coaching_config(_config_dir(tmp_path))
    path = _transcript(tmp_path, [_prompt(), _reply(150_000, ago_s=7_200)])
    read = {"hook_event_name": "PostToolUse", "tool_name": "Read", "session_id": "s1", "cwd": "/w",
            "tool_input": {}, "tool_response": {"content": "short"}, "transcript_path": path}
    assert _run(config_dir, read) == (0, "", "")
    assert _kept(tmp_path)["ctx"] == 150_000


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


@pytest.mark.parametrize("kind", cat.RETIRED_COACHING_HINTS)
def test_a_note_for_a_retired_hint_in_an_old_transcript_keeps_its_kind(tmp_path, _salt, kind):
    result = _session(tmp_path, [_ask(0), _note_line(_coach_text(kind), "UserPromptSubmit", 1), _turn(2)])
    event = next(e for e in result.events if e.subkind == "coaching_note")
    assert event.detail["kind"] == kind
    odd = _session(tmp_path, [_ask(0), _note_line(_coach_text("never_a_hint"), "UserPromptSubmit", 1), _turn(2)], "odd.jsonl")
    assert next(e for e in odd.events if e.subkind == "coaching_note").detail["kind"] == "other"


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
    assert specs["PostToolUse"].describe().endswith("after read, search and web results and an approved plan")


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


def test_the_catalogue_carries_the_patterns_the_hook_compiles():
    coaching = CATALOGUE["coaching"]
    assert coaching["reply_scan_chars"] == cat.REPLY_SCAN_CHARS
    assert coaching["reply_unit_pattern"] == cat.REPLY_UNIT_PATTERN
    assert coaching["reply_question_trim"] == cat.REPLY_QUESTION_TRIM
    for name in ("fence", "tip_block", "inline_code", "url", "quoted", "reminder_line", "tags", "list_start", "unit"):
        assert coaching[f"reply_{name}_pattern"] == getattr(cat, f"REPLY_{name.upper()}_PATTERN")
    # What it takes to tell a change request from a statement, a question and a go-ahead.
    for name in ("change", "ack", "review", "asks", "config_path", "shell_write"):
        assert coaching[f"{name}_pattern"] == getattr(cat, f"{name.upper()}_PATTERN")
        re.compile(coaching[f"{name}_pattern"])
    assert coaching["change_scan_chars"] == cat.CHANGE_SCAN_CHARS
    # The hook compiles them in the same way the package does: case-blind where it is.
    assert HOOK._is_change_request("now MAKE it bigger", coaching) and prompt_shape.is_change_request("now MAKE it bigger")
    assert not HOOK._is_change_request("the button is too small", coaching)


def test_the_hook_reads_a_go_ahead_as_the_package_does():
    coaching = HOOK.load_catalogue()["coaching"]
    texts = [
        "continue", "Go ahead.", "implement the plan", "merge it", "yes, do it", "ok", "do it please",
        "continue with the tests", "how is it going?", "", "x" * 70,
    ]
    for text in texts:
        assert HOOK._is_go(text, coaching) == prompt_shape.is_go(text), text


#: The next step of work, as a go-ahead says it: merge, release, push, commit, run, ship, carry on, or a pick.
_GO_AHEADS = [
    "continue", "go ahead", "implement the plan", "yes, do it", "do it please", "ok",
    "merge it", "merge and push", "release it", "ship it", "Ship it!", "ok, ship it", "lgtm, merge",
    "run the tests", "run the test suite", "run it", "yes run it", "commit and push", "push it to main",
    "push to origin", "carry on", "yes, carry on", "go ahead and merge", "go ahead and push it up",
    "carry on and run the tests", "merge it and release a new version", "commit and merge these to main",
    "once done - merge and push up a new version", "do 1 and 2",
]
#: What only looks like one: a step on something of your own, a retry, a correction, a plain request.
_NOT_GO_AHEADS = [
    "merge the auth logic into the helper", "commit the changes to auth.py", "push the button color to blue",
    "run the migration on the staging database and check it", "ship the fix to the customer and write the notes",
    "release notes for 1.2 need a rewrite", "merge conflicts in app.py, fix them", "run a full audit of the parser",
    "carry on with the footer redesign using the new palette", "try again", "retry", "still broken",
    "continue with the tests", "make the footer bigger", "how is it going?", "",
]


def test_a_go_ahead_is_the_next_step_of_the_work_and_nothing_of_your_own():
    coaching = CATALOGUE["coaching"]
    for text in _GO_AHEADS:
        assert prompt_shape.is_go(text) and HOOK._is_go(text, coaching), text
        # A go-ahead asks for no change, however its verbs read.
        assert not prompt_shape.is_change_request(text), text
    for text in _NOT_GO_AHEADS:
        assert not prompt_shape.is_go(text) and not HOOK._is_go(text, coaching), text
    # The Phase 1 reading of a retry stays: "try again" is a vague correction, not a go-ahead.
    assert prompt_shape.is_vague_fix("try again", 80) and not prompt_shape.is_vague_fix("go ahead", 80)


def test_the_catalogue_carries_the_go_ahead_pattern_and_the_lists_the_hook_and_the_parser_share():
    from claudeglass import events, parse

    coaching = CATALOGUE["coaching"]
    assert coaching["go_pattern"] == cat.GO_PATTERN and coaching["go_max_chars"] == cat.GO_MAX_CHARS
    re.compile(coaching["go_pattern"])
    # What only the report reads is not in the file the hook loads.
    report_only = {"release_pattern", "error_text_pattern", "plan_item_pattern", "paste_marker", "plan_long_chars"}
    assert not report_only & set(coaching)
    # The resume notes carry on your last message's work, for the hook as for the parser.
    assert tuple(coaching["resume_prefixes"]) == cat.RESUME_PREFIXES == HOOK._resume_prefixes()
    # The usage-limit lines the cold-return rule looks for are the ones the parser names.
    assert tuple(coaching["limit_line_prefixes"]) == cat.LIMIT_LINE_PREFIXES
    assert cat.LIMIT_LINE_PREFIXES == (cat.SESSION_LIMIT_PREFIX, cat.WEEKLY_LIMIT_PREFIX)
    assert events.classify_synthetic_text(_SESSION_LIMIT) == "session_limit"
    assert events.classify_synthetic_text(_WEEKLY_LIMIT) == "weekly_limit"
    # One list of the tools that launch a subagent, for the hook, the status line and the parser.
    assert tuple(coaching["agent_tools"]) == cat.AGENT_TOOLS == HOOK._AGENT_TOOLS == tuple(parse._AGENT_TOOL_NAMES)
    assert "queued_minutes" in cat.COACHING_THRESHOLDS


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


def test_a_plan_that_ends_with_the_tip_the_note_asked_for_counts_as_a_relayed_tip(tmp_path):
    # ``plan_fresh_early`` asks for the tip as the last line of the plan Claude submits: the plan is the reply that shows it.
    tip = "> **ClaudeGlass tip:** Approve with clear context: this session holds about 75k tokens of planning chat."

    def plan(second: int, text: str) -> dict:
        call = {"type": "tool_use", "id": f"toolu_p{second}", "name": "ExitPlanMode", "input": {"plan": text}}
        return turn_line(content=[call], model="claude-widget-9", timestamp=_line_at(second), cache_read_input_tokens=5_000)

    result = _session(tmp_path, [
        _human("plan the admin screen", 0), plan(1, "1. Add the screen.\n\n" + tip),
        _human("make it smaller", 2), plan(3, "1. Add the screen, smaller."),
    ])
    assert [t.coach_tip for t in result.turns] == [True, False]

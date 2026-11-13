"""Whether a coaching hint could have fired (``hint_occasions``): the parser's
``Turn.background_waiting``, which the status-check moment reads, and the
count of moments by the hook's own triggers.
"""

from __future__ import annotations

import base64
import itertools
import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest

from claudeglass import capture, capture_catalogue as cat, capture_view, hint_occasions, hook_health, jsonl, measured
from claudeglass import prompt_shape, prompting, ratings
from claudeglass.config import CaptureConfig, Config
from claudeglass.corpus import load_corpus
from claudeglass.hooks import capture_hook
from claudeglass.model import TranscriptMeta
from claudeglass.parse import parse_transcript
from claudeglass.service import rebuild

import agreement
import test_tuning

from helpers import (
    attachment_line,
    store_with_older_sessions,
    tool_result_block,
    tool_use_block,
    turn_line,
    user_block_line,
    user_str_line,
    write_jsonl,
)
from test_prompting import _DRIP, _START, _feedback_lines, _say as _answer


def _at(minute: float) -> str:
    whole = int(minute)
    return f"2026-09-18T{12 + whole // 60:02d}:{whole % 60:02d}:{int((minute - whole) * 60):02d}.000Z"


def _said(text: str, minute: float, **extra) -> dict:
    return user_str_line(text, timestamp=_at(minute), origin={"kind": "human"}, **extra)


def _reply(minute: float, *, say: str = "Done.", ctx: int = 40_000, one_hour: int = 0, content=None) -> dict:
    content = content if content is not None else [{"type": "text", "text": say}]
    return turn_line(
        timestamp=_at(minute), content=content, cache_read_input_tokens=ctx, output_tokens=200,
        ephemeral_1h_input_tokens=one_hour,
    )


LAUNCH_SHELL = "Command running in background with ID: b1. Output is being written to a file."
LAUNCH_AGENT = "Async agent launched successfully. It works in the background."


def _finished(call_id: str | None, minute: float) -> dict:
    """A background task's finishing message as a queue operation, naming the
    call that started it (or none)."""
    named = f"<tool-use-id>{call_id}</tool-use-id>\n" if call_id else ""
    return {
        "type": "queue-operation", "operation": "enqueue", "timestamp": _at(minute),
        "content": f"<task-notification>\n<task-id>b1</task-id>\n{named}<status>completed</status>\n</task-notification>",
    }


def _started(result: str, minute: float = 1, *, call_id: str = "toolu_bg", sidechain: bool = False, **reply) -> list[dict]:
    """A message, a reply that starts work, the tool result saying where it
    went and a short reply after it."""
    back = user_block_line([tool_result_block(call_id, result)], timestamp=_at(minute + 0.5))
    if sidechain:
        back["isSidechain"] = True
    return [
        _said("run the build", minute - 1),
        _reply(minute, content=[tool_use_block("Bash", call_id, {"command": "make"})], **reply),
        back,
        _reply(minute + 1, say="Started it.", **reply),
    ]


def _parse(tmp_path, lines, name="s.jsonl"):
    path = tmp_path / name
    write_jsonl(path, lines)
    return parse_transcript(path, TranscriptMeta(path=str(path)))


# -- what the parser keeps ------------------------------------------------------------


def _scenarios() -> dict[str, list[dict]]:
    ask = _said("how is it going?", 4)
    answer = _reply(5)
    return {
        "shell launch": [*_started(LAUNCH_SHELL), ask, answer],
        "agent launch": [*_started(LAUNCH_AGENT), ask, answer],
        "finished, named": [*_started(LAUNCH_SHELL), _finished("toolu_bg", 3), ask, answer],
        "finished, another call": [*_started(LAUNCH_SHELL), _finished("toolu_other", 3), ask, answer],
        "finished, names no call": [*_started(LAUNCH_SHELL), _finished(None, 3), ask, answer],
        "a subagent's launch": [*_started(LAUNCH_SHELL, sidechain=True), ask, answer],
        "ran to the end": [*_started("Command finished with exit code 0."), ask, answer],
        "launch past the first 400 characters": [*_started("x" * 500 + LAUNCH_SHELL), ask, answer],
        "launched again after a finish": [
            *_started(LAUNCH_SHELL), _finished("toolu_bg", 3),
            *_started(LAUNCH_AGENT, 6, call_id="toolu_two"), _said("any updates?", 9), _reply(10),
        ],
        "nothing started": [_said("hello there", 0), _reply(1), ask, answer],
    }


def test_the_parser_flags_background_waiting_as_the_hook_reads_it(tmp_path):
    coaching = capture_hook.load_catalogue()["coaching"]
    seen = {True: 0, False: 0}
    for index, (name, lines) in enumerate(_scenarios().items()):
        result = _parse(tmp_path, lines, name=f"s{index}.jsonl")
        for n, line in enumerate(lines):
            if line.get("origin") != {"kind": "human"}:
                continue
            asked = datetime.fromisoformat(line["timestamp"].replace("Z", "+00:00")).astimezone(timezone.utc)
            opened = next(t for t in result.turns if datetime.fromisoformat(t.ts.replace("Z", "+00:00")) > asked)
            expected = capture_hook._background_pending(lines[:n], coaching)
            assert opened.background_waiting is expected, (name, n)
            seen[expected] += 1
    # Both answers came up, so the comparison is not vacuous.
    assert seen[True] >= 3 and seen[False] >= 8


@pytest.mark.parametrize("fillers", [3, 38, 40, 41, 42, 43, 46, 60])
def test_a_background_launch_the_hook_can_no_longer_see_stops_counting(tmp_path, fillers):
    """The hook reads the last 4 MB of a transcript: a launch that never got a notification (a dev server) lies
    beyond it once enough was written since, and the typed line after that finds nothing waiting. The parser
    counts bytes as it reads them, so it agrees with the hook's own reading of the same file, line for line."""
    coaching = capture_hook.load_catalogue()["coaching"]
    padding = [_reply(3 + i * 0.5, say="y" * 100_000) for i in range(fillers)]
    lines = [*_started(LAUNCH_SHELL), *padding, _said("how is it going?", 3 + fillers * 0.5 + 1), _reply(3 + fillers * 0.5 + 2)]
    result = _parse(tmp_path, lines)
    through_the_question = lines[:-1]
    path = tmp_path / "hook-view.jsonl"
    write_jsonl(path, through_the_question)
    seen_by_hook = capture_hook._background_pending(capture_hook._tail(str(path), capture_hook._COACH_PROMPT_TAIL_BYTES), coaching)
    assert result.turns[-1].background_waiting is seen_by_hook
    assert seen_by_hook is (fillers < 41.5)
    typed = [e for e in result.events if e.kind.name == "HUMAN_TEXT"]
    assert typed[-1].detail.get("waiting") is (True if seen_by_hook else None)


def test_the_line_reader_counts_the_files_bytes_as_the_hook_would_seek_them(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_bytes(
        b'{"type": "user", "text": "caf\xc3\xa9 \xe2\x82\xac"}\r\n\r\n   \n{not json}\n'
        + b'{"a": 1}\n' + b"x" * 40 + b'\n{"b": 2}'
    )
    stats = jsonl.LineStats()
    starts = []
    for _no, _line in jsonl.iter_lines(path, stats=stats):
        starts.append((stats.line_start, stats.bytes_read))
    assert stats.bytes_read == path.stat().st_size
    assert starts[0][0] == 0 and [b - a for a, b in starts] == [len(b'{"type": "user", "text": "caf\xc3\xa9 \xe2\x82\xac"}\r\n'), 9, 8]


def test_a_typed_line_with_background_work_out_keeps_a_flag_and_no_words(tmp_path):
    result = _parse(tmp_path, [*_started(LAUNCH_SHELL), _said("how is it going?", 4), _reply(5)])
    typed = [e for e in result.events if e.kind.name == "HUMAN_TEXT"]
    assert [e.detail.get("waiting") for e in typed] == [None, True]
    assert [t.background_waiting for t in result.turns] == [False, False, True]
    assert LAUNCH_SHELL not in repr(result.events)


# -- the moments themselves -----------------------------------------------------------------

SINCE = datetime(2026, 9, 1, tzinfo=timezone.utc)
UNTIL = datetime(2026, 10, 1, tzinfo=timezone.utc)
BASE = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
TH = ratings.coaching_thresholds({}, {})
DAY = timedelta(days=1)


def _png(width: int, height: int) -> str:
    """A PNG header that says how big the picture is, which is all the parser reads."""
    header = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + width.to_bytes(4, "big") + height.to_bytes(4, "big")
    return base64.b64encode(header + b"\x08\x06\x00\x00\x00").decode()


def _picture() -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": _png(1000, 1000)}}


def _pasted(characters: int, minute: float, **extra) -> dict:
    return _said("x" * characters, minute, **extra)


def _edit_reply(minute: float, *, ask: bool = False) -> dict:
    return _reply(
        minute, say="Which one do you mean?" if ask else "Done.",
        content=[tool_use_block("Edit", f"toolu_e{minute}", {"file_path": "src/a.py"}),
                 {"type": "text", "text": "Which one do you mean?" if ask else "Done."}],
    )


def _parsed(tmp_path, lines, name="s.jsonl"):
    return _parse(tmp_path, lines, name)


def _occasions(tmp_path, lines, th=TH, name="s.jsonl"):
    top = _parsed(tmp_path, lines, name)
    cycles = capture.prompt_cycles(top)
    messages = prompting.messages_of(top, prompting._Prices(None), cycles)
    return hint_occasions.session_occasions(NS(top=top), messages, cycles, th)


def _note_line(kind: str, minute: float) -> dict:
    text = f"{cat.COACH_MARKER}{cat.COACH_VERSION} {kind}\nSome hint text."
    line = attachment_line(
        "hook_additional_context", rendered=f"<system-reminder>\nUserPromptSubmit hook additional context: {text}\n"
        "</system-reminder>", content=[text], hookName="UserPromptSubmit",
    )
    line["timestamp"] = _at(minute)
    return line


def _shift(lines: list[dict], days: float) -> list[dict]:
    """The same lines ``days`` later."""
    out = []
    for line in lines:
        line = dict(line)
        stamp = line.get("timestamp")
        if isinstance(stamp, str):
            moved = datetime.fromisoformat(stamp.replace("Z", "+00:00")) + timedelta(days=days)
            line["timestamp"] = moved.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        out.append(line)
    return out


def test_the_hints_counted_are_the_live_hints():
    assert hint_occasions.HINTS == tuple(agreement.live_hints())
    assert hint_occasions.HINTS == ("drip_feed", "big_paste", "status_poll")
    assert hint_occasions.HEADLESS_PREFIX == capture_hook._HEADLESS_ENTRYPOINT
    assert set(hint_occasions.LABELS) == set(hint_occasions.HINTS)
    assert hint_occasions.SILENT_MIN_SESSIONS == 2
    assert "desktop" not in hint_occasions.MOD_HINT_SURFACES
    assert set(hint_occasions.MOD_HINT_SURFACES) == set(cat.SENSOR_PRESENT_SURFACES) - {"desktop"}


def test_a_big_paste_is_an_occasion_by_its_typed_text_and_an_image_is_not(tmp_path):
    assert TH["big_paste_tokens"] == 10_000
    reply = [_reply(1)]
    assert _occasions(tmp_path, [_pasted(40_001, 0), *reply])["big_paste"] == [0]
    assert _occasions(tmp_path, [_pasted(39_996, 0), *reply], name="b.jsonl")["big_paste"] == []
    assert _occasions(tmp_path, [_pasted(40_000, 0), *reply], name="c.jsonl")["big_paste"] == [0]
    # The picture's size estimate tips the message over, but is no text the hook measures.
    with_picture = user_block_line(
        [{"type": "text", "text": "y" * 39_000}, _picture()], timestamp=_at(0), origin={"kind": "human"}
    )
    top = _parsed(tmp_path, [with_picture, *reply], name="d.jsonl")
    assert top.turns[0].human_prompt_chars / 4 >= TH["big_paste_tokens"]
    assert _occasions(tmp_path, [with_picture, *reply], name="e.jsonl")["big_paste"] == []


def _script(steps) -> list[dict]:
    """Messages ``(text, minute, edited[, asks])`` each answered by a reply a minute later: one that
    changes a file, one that asks you something, or neither."""
    lines = []
    for text, minute, edited, *asks in steps:
        reply = _edit_reply(minute + 1) if edited else _reply(minute + 1, say="Which one do you mean?" if asks else "Done.")
        lines += [_said(text, minute), reply]
    return lines


def _hook_drip(steps, th=TH) -> list[int]:
    """The messages where ``prompt_shape.drip_count`` reaches the threshold on the same facts."""
    out, earlier, typed_at, asked = [], [], None, False
    for i, (text, minute, edited, *asks) in enumerate(steps):
        since = None if typed_at is None else (minute - typed_at) * 60
        if prompt_shape.drip_count(earlier, text, since, asked, th) >= th["drip_count"]:
            out.append(i)
        earlier.append({"text": text, "answered": True, "answer": asked, "edited": edited, "since": since})
        typed_at, asked = minute, bool(asks) and not edited
    return out


_DRIP_SCRIPTS = {
    "four small edits two minutes apart": [
        ("make the header bigger", 0, True), ("now move the logo left", 2, True),
        ("change the footer colour to blue", 4, True), ("make the button rounder", 6, True),
    ],
    "the run's third reply edited nothing": [
        ("make the header bigger", 0, True), ("now move the logo left", 2, True),
        ("change the footer colour to blue", 4, True), ("make the button rounder", 6, False),
    ],
    "a question in the run": [
        ("make the header bigger", 0, True), ("does the header look right?", 2, False),
        ("now move the logo left", 4, True), ("make the button rounder", 6, True),
    ],
    "a 21 minute gap": [
        ("make the header bigger", 0, True), ("now move the logo left", 2, True),
        ("change the footer colour to blue", 4, True), ("make the button rounder", 25, True),
    ],
    "an answer to Claude's question": [
        ("make the header bigger", 0, True), ("now move the logo left", 2, True),
        ("change the footer colour to blue", 4, True), ("what size should the logo be?", 6, False, True),
        ("make the logo smaller", 8, True),
    ],
    "an answer inside the run": [
        ("make the header bigger", 0, True), ("make the logo smaller", 2, False, True),
        ("make the left one smaller", 4, True), ("now move the footer text", 6, True),
        ("change the title colour", 8, True),
    ],
    "no edits at all": [
        ("make the header bigger", 0, False), ("now move the logo left", 2, False),
        ("change the footer colour to blue", 4, False),
    ],
}


@pytest.mark.parametrize("name", list(_DRIP_SCRIPTS))
def test_a_drip_occasion_is_the_hooks_own_count(tmp_path, name):
    steps = _DRIP_SCRIPTS[name]
    found = _occasions(tmp_path, _script(steps))["drip_feed"]
    assert found == _hook_drip(steps)


def test_the_drip_rule_counts_a_run_the_after_the_fact_count_does_not_see(tmp_path):
    steps = _DRIP_SCRIPTS["the run's third reply edited nothing"]
    lines = _script(steps)
    assert _hook_drip(steps) == [3]
    assert _occasions(tmp_path, lines)["drip_feed"] == [3]
    top = _parsed(tmp_path, lines, name="b.jsonl")
    messages = prompting.messages_of(top, prompting._Prices(None))
    # The reply to the third small request changed nothing, so the run never reaches three.
    assert prompting._drip_runs(messages) == []


def test_a_drip_run_you_called_a_miss_is_still_a_moment_the_hook_saw(tmp_path):
    answers = {**_answer("outcome", "partly"), **_answer("why", "changed")}
    lines = [*_START, *_DRIP, *_feedback_lines(50, answers)]
    top = _parsed(tmp_path, lines)
    messages = prompting.messages_of(top, prompting._Prices(None))
    assert [m.excused for m in messages[1:4]] == [True] * 3 and prompting._drip_runs(messages) == []
    # The hook could not know it: the third small edit was a moment, and the feedback run is none.
    assert _occasions(tmp_path, lines, name="b.jsonl")["drip_feed"] == [3]


def _asked_after(launch: str, *, ask_at: float, one_hour: int = 0, finished=None) -> list[dict]:
    lines = _started(launch, one_hour=one_hour)
    if finished is not None:
        lines.append(finished)
    return [*lines, _said("how is it going?", ask_at), _reply(ask_at + 1, one_hour=one_hour)]


def test_a_status_check_is_an_occasion_only_while_background_work_waits_and_the_cache_is_warm(tmp_path):
    def status(lines, name):
        return _occasions(tmp_path, lines, name=name)["status_poll"]

    assert status(_asked_after(LAUNCH_SHELL, ask_at=4), "a.jsonl") == [1]
    assert status(_asked_after(LAUNCH_SHELL, ask_at=4, finished=_finished("toolu_bg", 3)), "b.jsonl") == []
    assert status(_asked_after(LAUNCH_SHELL, ask_at=8), "c.jsonl") == []
    assert status(_asked_after(LAUNCH_SHELL, ask_at=32), "d.jsonl") == []
    assert status(_asked_after(LAUNCH_SHELL, ask_at=32, one_hour=1000), "e.jsonl") == [1]
    assert status(_asked_after(LAUNCH_AGENT, ask_at=4), "f.jsonl") == [1]
    # The first message has no reply before it, and nothing is out.
    assert status([_said("how is it going?", 0), _reply(1)], "g.jsonl") == []
    # The cache lifetimes are the hook's own.
    assert (hint_occasions.CACHE_TTL_S, hint_occasions.CACHE_TTL_1H_S) == (
        capture_hook._CACHE_TTL_S, capture_hook._CACHE_TTL_1H_S,
    )


def test_no_occasion_where_the_hook_gives_no_hint(tmp_path):
    paste = _pasted(41_000, 5)
    work = [_said("do the thing", 0), _reply(1)]
    # Sent while Claude worked: the reply before it ended on a tool call two minutes before.
    working = _reply(3, content=[tool_use_block("Bash", "toolu_w", {"command": "make"})])
    working["message"]["stop_reason"] = "tool_use"
    assert _occasions(tmp_path, [*work, working, paste, _reply(6)], name="a.jsonl")["big_paste"] == []
    # The same message after the work ended is one.
    assert _occasions(tmp_path, [*work, _pasted(41_000, 5), _reply(6)], name="b.jsonl")["big_paste"] == [1]
    # After you pressed Esc it is a new message, not one typed into work.
    stopped = user_str_line("[Request interrupted by user for tool use]", timestamp=_at(4))
    assert _occasions(tmp_path, [*work, working, stopped, paste, _reply(6)], name="c.jsonl")["big_paste"] == [1]
    # A /cg-feedback run is none.
    feedback = _feedback_lines(10, {**_answer("outcome", "partly")})
    feedback[0]["message"]["content"] += "x" * 41_000
    found = _occasions(tmp_path, [*work, *feedback], name="d.jsonl")
    assert found["big_paste"] == []


def _env(tmp_path, *, setup_at: datetime | None = SINCE, log=None, muted=None):
    """A config folder, a Claude folder with a settings.json of the given age, and the coaching file."""
    config_dir, claude_root = tmp_path / "config", tmp_path / "claude"
    config_dir.mkdir(parents=True, exist_ok=True)
    claude_root.mkdir(parents=True, exist_ok=True)
    settings = claude_root / "settings.json"
    settings.write_text("{}", encoding="utf-8")
    if setup_at is not None:
        os.utime(settings, (setup_at.timestamp(), setup_at.timestamp()))
    if log is not None:
        (config_dir / "capture-log.jsonl").write_text("\n".join(json.dumps(r) for r in log) + "\n", encoding="utf-8")
    if muted is not None:
        (config_dir / "coaching.json").write_text(json.dumps({"muted": muted}), encoding="utf-8")
    return config_dir, claude_root


def _config(*, notes: bool = True, projects=(), exclude=()) -> Config:
    coaching = ["coaching_line", "coaching_notes"] if notes else ["coaching_line"]
    return Config(
        tz="UTC", capture=CaptureConfig(level="standard", coaching=coaching, projects=list(projects)),
        exclude_projects=list(exclude),
    )


_folders = itertools.count()


def _corpus(tmp_path, sessions: dict[str, list[dict]], slug="proj-a"):
    folder = tmp_path / f"projects{next(_folders)}" / slug
    folder.mkdir(parents=True)
    for name, lines in sessions.items():
        write_jsonl(folder / f"{name}.jsonl", lines)
    return load_corpus([folder])


def _collect(tmp_path, sessions, *, config=None, env=None, mod_events=None, slug="proj-a", **kw):
    config_dir, claude_root = env or _env(tmp_path)
    corpus = sessions if hasattr(sessions, "sessions") else _corpus(tmp_path, sessions, slug)
    kw.setdefault("since", SINCE)
    kw.setdefault("until", UNTIL)
    return hint_occasions.collect(
        corpus, config or _config(), config_dir=config_dir, thresholds=TH, mod_events=mod_events,
        claude_root=claude_root, **kw,
    )


_PASTE = [_pasted(41_000, 0), _reply(1)]


def test_the_block_counts_a_session_with_a_moment_and_a_session_that_got_its_note(tmp_path):
    occ = _collect(tmp_path, {
        "s1": _PASTE, "s2": [*_PASTE, _note_line("big_paste", 1.5)], "s3": [_said("hello", 0), _reply(1)],
    })
    paste = occ.hints["big_paste"]
    assert (paste.occasions, paste.sessions, paste.noted, paste.notes) == (2, 2, 1, 1)
    assert (paste.glass, paste.mod_sessions, paste.older) == (0, 0, 0)
    assert occ.hints["drip_feed"] == hint_occasions.HintCount()
    assert hint_occasions.silent(occ) == {} and occ.muted == ()
    assert list(occ.hints) == list(hint_occasions.HINTS)


def test_a_session_with_one_moment_and_no_note_is_not_yet_a_missing_tip(tmp_path):
    assert hint_occasions.SILENT_MIN_SESSIONS == 2
    one = _collect(tmp_path, {"s1": _PASTE})
    assert one.hints["big_paste"].sessions == 1 and hint_occasions.silent(one) == {}
    two = _collect(tmp_path, {"s1": _PASTE, "s2": _PASTE}, env=_env(tmp_path / "x"))
    assert hint_occasions.silent(two) == {"big_paste": 2}
    assert two.silent_distinct(hint_occasions.silent(two)) == 2
    # A note in one of the two is a hook that works.
    noted = _collect(tmp_path, {"s1": _PASTE, "s2": [*_PASTE, _note_line("big_paste", 1.5)]}, env=_env(tmp_path / "y"))
    assert hint_occasions.silent(noted) == {}


def test_the_distinct_sessions_of_a_line_count_a_session_once_for_every_hint_it_names(tmp_path):
    both = [_pasted(41_000, 0), _edit_reply(1), _said("make the header bigger", 2), _edit_reply(3),
            _said("now move the logo left", 4), _edit_reply(5), _said("change the footer colour", 6), _edit_reply(7)]
    occ = _collect(tmp_path, {"s1": both, "s2": both, "s3": _PASTE})
    found = hint_occasions.silent(occ)
    assert found == {"drip_feed": 2, "big_paste": 3}
    assert occ.silent_distinct(found) == 3
    assert occ.silent_distinct({}) == 0


def test_no_moment_in_a_headless_session_a_project_left_out_or_a_session_without_a_message(tmp_path):
    headless = [_pasted(41_000, 0, entrypoint="sdk-cli"), _reply(1)]
    occ = _collect(tmp_path, {"s1": headless, "s2": _PASTE})
    assert occ.hints["big_paste"].sessions == 1
    left_out = _collect(tmp_path, {"s1": _PASTE, "s2": _PASTE}, config=_config(projects=["!proj-a"]), env=_env(tmp_path / "a"))
    assert left_out.hints["big_paste"].sessions == 0
    excluded = _collect(tmp_path, {"s1": _PASTE}, config=_config(exclude=["proj-a"]), env=_env(tmp_path / "b"))
    assert excluded.hints["big_paste"].sessions == 0
    only = _collect(tmp_path, {"s1": _PASTE}, config=_config(projects=["proj-a"]), env=_env(tmp_path / "c"))
    assert only.hints["big_paste"].sessions == 1
    empty = _collect(tmp_path, {"s1": [_reply(1)]}, env=_env(tmp_path / "d"))
    assert empty.hints["big_paste"].sessions == 0


def test_nothing_is_counted_while_coaching_notes_is_off(tmp_path):
    assert _collect(tmp_path, {"s1": _PASTE}, config=_config(notes=False)) is None


def test_only_the_window_counts(tmp_path):
    occ = _collect(tmp_path, {"s1": _PASTE}, since=BASE + DAY, until=BASE + 2 * DAY)
    assert occ.hints["big_paste"].occasions == 0
    occ = _collect(tmp_path, {"s1": _PASTE}, since=BASE - DAY, until=BASE + 2 * DAY, env=_env(tmp_path / "b"))
    assert occ.hints["big_paste"].occasions == 1


def _switch(ts: datetime, frm, to) -> dict:
    return {"ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "level": "standard", "changed": {"coaching": {"from": frm, "to": to}}}


def test_occasions_and_notes_count_only_while_coaching_notes_was_on(tmp_path):
    on = datetime(2026, 9, 19, tzinfo=timezone.utc)
    log = [_switch(datetime(2026, 8, 1, tzinfo=timezone.utc), ["coaching_notes"], []), _switch(on, [], ["coaching_notes"])]
    early = [*_PASTE, _note_line("big_paste", 1.5)]
    sessions = {"early": early, "late": _shift(early, 2)}
    occ = _collect(tmp_path, sessions, env=_env(tmp_path, log=log))
    paste = occ.hints["big_paste"]
    assert (paste.occasions, paste.sessions, paste.notes, paste.noted) == (1, 1, 1, 1)
    assert occ.on_hours == int((UNTIL - on).total_seconds() // 3600)
    # Switched on, off and on again: the time between counts for nothing.
    off, back = datetime(2026, 9, 20, tzinfo=timezone.utc), datetime(2026, 9, 25, tzinfo=timezone.utc)
    log = [*log, _switch(off, ["coaching_notes"], []), _switch(back, [], ["coaching_notes"])]
    occ = _collect(tmp_path, {"mid": _shift(early, 3)}, env=_env(tmp_path / "b", log=log))
    assert occ.hints["big_paste"].occasions == 0 and occ.hints["big_paste"].notes == 0
    assert occ.on_hours == int(((off - on) + (UNTIL - back)).total_seconds() // 3600)
    # A log with no coaching record: on since before the log began, so both count.
    other = [{"ts": "2026-08-01T00:00:00Z", "level": "standard", "changed": {"level": {"from": "off", "to": "standard"}}}]
    occ = _collect(tmp_path, sessions, env=_env(tmp_path / "c", log=other))
    assert (occ.hints["big_paste"].occasions, occ.hints["big_paste"].notes) == (2, 2)
    assert occ.on_hours == int((UNTIL - SINCE).total_seconds() // 3600)
    occ = _collect(tmp_path, sessions, env=_env(tmp_path / "d"))
    assert occ.hints["big_paste"].occasions == 2


def test_coaching_spans_read_the_log_and_skip_what_it_cannot(tmp_path):
    config_dir, _ = _env(tmp_path)
    end = datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert hint_occasions.coaching_spans(config_dir, end) == [(datetime.min.replace(tzinfo=timezone.utc), end)]
    assert hint_occasions.coaching_spans(None, end) == [(datetime.min.replace(tzinfo=timezone.utc), end)]
    on, off = datetime(2026, 9, 5, tzinfo=timezone.utc), datetime(2026, 9, 6, tzinfo=timezone.utc)
    log = [
        _switch(on, [], ["coaching_notes", "coaching_line"]),
        {"ts": "not a time", "changed": {"coaching": {"from": [], "to": ["coaching_notes"]}}},
        _switch(on + timedelta(hours=1), ["coaching_notes"], ["coaching_notes", "coaching_line"]),
        _switch(off, ["coaching_notes"], ["coaching_line"]),
    ]
    (config_dir / "capture-log.jsonl").write_text("\n".join(json.dumps(r) for r in log) + "\n", encoding="utf-8")
    assert hint_occasions.coaching_spans(config_dir, end) == [(on, off)]


def test_coaching_on_before_the_log_and_another_coaching_item_toggled_since(tmp_path):
    """The switch-on record was pruned (retention) or never written (config.toml edited by hand); since then
    another coaching item was switched. Coaching notes was on the whole time and is on now."""
    config_dir, _ = _env(tmp_path)
    end = datetime(2026, 10, 1, tzinfo=timezone.utc)
    floor = datetime.min.replace(tzinfo=timezone.utc)
    log = [_switch(datetime(2026, 9, 20, tzinfo=timezone.utc), ["coaching_notes"], ["coaching_notes", "brief_templates"])]
    (config_dir / "capture-log.jsonl").write_text("\n".join(json.dumps(r) for r in log) + "\n", encoding="utf-8")
    assert hint_occasions.coaching_spans(config_dir, end) == [(floor, end)]
    # Switched off later: the time before the switch-off counts, from the start of time.
    off = datetime(2026, 9, 25, tzinfo=timezone.utc)
    log.append(_switch(off, ["coaching_notes", "brief_templates"], ["brief_templates"]))
    (config_dir / "capture-log.jsonl").write_text("\n".join(json.dumps(r) for r in log) + "\n", encoding="utf-8")
    assert hint_occasions.coaching_spans(config_dir, end) == [(floor, off)]
    # The export's on_hours is the time on in the window, not 0.
    log.pop()
    occ = _collect(tmp_path, {"s1": _PASTE}, env=_env(tmp_path / "b", log=log))
    assert occ.on_hours == int((UNTIL - SINCE).total_seconds() // 3600)
    assert occ.hints["big_paste"].occasions == 1


def test_a_session_the_mod_ran_in_off_the_desktop_is_counted_apart(tmp_path):
    corpus = _corpus(tmp_path, {"s1": _PASTE, "s2": _PASTE})

    def mod(session, surface, event="present", **extra):
        return {"session_id": session, "ts": int(BASE.timestamp()), "event": event, "kind": None, "surface": surface, **extra}

    occ = _collect(tmp_path, corpus, mod_events=[mod("s1", "terminal"), mod("s2", "desktop")])
    paste = occ.hints["big_paste"]
    assert (paste.sessions, paste.mod_sessions, paste.occasions) == (1, 1, 2)
    assert hint_occasions.silent(occ) == {}
    both = _collect(tmp_path, corpus, mod_events=[mod("s1", "desktop"), mod("s2", "desktop")], env=_env(tmp_path / "b"))
    assert (both.hints["big_paste"].sessions, both.hints["big_paste"].mod_sessions) == (2, 0)
    assert hint_occasions.silent(both) == {"big_paste": 2}
    # A note of the mod's own, for that hint, is a tip given.
    shown = mod("s1", "hint", event="note", kind="big_paste", outcome="sent")
    noted = _collect(tmp_path, corpus, mod_events=[mod("s1", "desktop"), mod("s2", "desktop"), shown], env=_env(tmp_path / "c"))
    assert noted.hints["big_paste"].glass == 1 and hint_occasions.silent(noted) == {}
    # The store keeps the time as the UTC ISO text mod_sensor.clean_event writes (epoch text is read too).
    for n, stamp in enumerate((BASE.strftime("%Y-%m-%dT%H:%M:%SZ"), str(int(BASE.timestamp())))):
        as_text = {**shown, "ts": stamp}
        stored = _collect(tmp_path, corpus, mod_events=[mod("s1", "desktop"), mod("s2", "desktop"), as_text], env=_env(tmp_path / f"e{n}"))
        assert stored.hints["big_paste"].glass == 1
    # Not a hint's note, or outside the window: nothing.
    other = mod("s1", "note", event="note", kind="cold_return")
    late = {**shown, "ts": int((UNTIL + DAY).timestamp())}
    quiet = _collect(tmp_path, corpus, mod_events=[other, late], env=_env(tmp_path / "d"))
    assert quiet.hints["big_paste"].glass == 0


def test_an_older_digest_leaves_status_checks_out_and_says_how_many(tmp_path):
    when = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=2)
    delta = (when - BASE).total_seconds() / 86400
    lines = _shift([*_started(LAUNCH_SHELL), _said("how is it going?", 4), _reply(5), _pasted(41_000, 7), _reply(8)], delta)
    store, _ = store_with_older_sessions(tmp_path / "store", {"s-new": lines, "s-old": lines}, older={"s-old"})
    corpus = rebuild.corpus_from_store(store)
    assert {b.session_id: measured.measures(b.top, *measured.OCCASION_FIELDS) for b in corpus.sessions} == {
        "s-new": True, "s-old": False,
    }
    config_dir, claude_root = _env(tmp_path, setup_at=when - 3 * DAY)
    occ = hint_occasions.collect(
        corpus, _config(), config_dir=config_dir, thresholds=TH, since=when - 5 * DAY, until=when + 5 * DAY,
        claude_root=claude_root,
    )
    status, paste = occ.hints["status_poll"], occ.hints["big_paste"]
    assert (status.occasions, status.sessions, status.older) == (1, 1, 1)
    # A status check outside the window is nobody's: the older session is not counted for it.
    later = hint_occasions.collect(
        corpus, _config(), config_dir=config_dir, thresholds=TH, since=when + DAY, until=when + 5 * DAY,
        claude_root=claude_root,
    )
    assert later.hints["status_poll"].older == 0 and later.hints["status_poll"].occasions == 0
    # The big paste is older than every digest in use: it still counts in both.
    assert (paste.occasions, paste.sessions, paste.older) == (2, 2, 0)


def test_the_line_waits_for_a_moment_after_the_setup_last_changed(tmp_path):
    sessions = {"s1": _PASTE, "s2": _PASTE}
    after = _collect(tmp_path, sessions, env=_env(tmp_path / "a", setup_at=BASE + timedelta(hours=1)))
    assert hint_occasions.silent(after) == {} and after.hints["big_paste"].sessions == 2
    assert after.silent_since == BASE + timedelta(hours=1)
    before = _collect(tmp_path, sessions, env=_env(tmp_path / "b", setup_at=BASE - timedelta(hours=1)))
    assert hint_occasions.silent(before) == {"big_paste": 2}
    # A hook file changed since counts as the setup changing.
    config_dir, claude_root = _env(tmp_path / "c", setup_at=BASE - timedelta(hours=1))
    folder = config_dir / "hooks"
    folder.mkdir()
    name = hook_health.CAPTURE_FILES[cat.HOOK_SCRIPT][0]
    (folder / name).write_text("x", encoding="utf-8")
    os.utime(folder / name, (BASE.timestamp() + 3600, BASE.timestamp() + 3600))
    assert hint_occasions.setup_changed_at(claude_root, config_dir) == BASE + timedelta(hours=1)
    changed = _collect(tmp_path, sessions, env=(config_dir, claude_root))
    assert hint_occasions.silent(changed) == {}
    assert hint_occasions.setup_changed_at(tmp_path / "nothing", tmp_path / "nowhere") is None


def test_the_line_waits_for_a_moment_after_coaching_notes_was_last_switched_on(tmp_path):
    on = BASE - timedelta(hours=1)
    log = [_switch(datetime(2026, 8, 1, tzinfo=timezone.utc), ["coaching_notes"], []), _switch(on, [], ["coaching_notes"])]
    occ = _collect(tmp_path, {"s1": _PASTE, "s2": _PASTE}, env=_env(tmp_path, log=log))
    assert occ.silent_since == on and hint_occasions.silent(occ) == {"big_paste": 2}
    later = _switch(BASE + timedelta(hours=1), [], ["coaching_notes"])
    log = [log[0], later]
    occ = _collect(tmp_path, {"s1": _PASTE, "s2": _PASTE}, env=_env(tmp_path / "b", log=log))
    assert occ.hints["big_paste"].occasions == 0 and hint_occasions.silent(occ) == {}


def test_a_muted_hint_is_never_silent(tmp_path):
    occ = _collect(tmp_path, {"s1": _PASTE, "s2": _PASTE}, env=_env(tmp_path, muted=["big_paste", "not_a_hint"]))
    assert occ.muted == ("big_paste",)
    assert occ.hints["big_paste"].sessions == 2 and hint_occasions.silent(occ) == {}


def test_the_inputs_fingerprint_moves_with_each_thing_collect_reads_besides_the_corpus(tmp_path):
    config_dir, claude_root = _env(tmp_path)
    config = _config()

    def now():
        return hint_occasions.inputs_fingerprint(claude_root, config_dir, config)

    seen = [now()]
    assert now() == seen[0]  # nothing changed, nothing moves

    def moved():
        seen.append(now())
        assert seen[-1] != seen[-2]

    (claude_root / "settings.json").write_text('{"hooks": {}}', encoding="utf-8")
    moved()
    (config_dir / "coaching.json").write_text(json.dumps({"muted": ["big_paste"]}), encoding="utf-8")
    moved()
    (config_dir / "capture-log.jsonl").write_text(json.dumps(_switch(SINCE, [], ["coaching_notes"])) + "\n", encoding="utf-8")
    moved()
    (config_dir / "hooks").mkdir()
    (config_dir / "hooks" / hook_health.CAPTURE_FILES[cat.HOOK_SCRIPT][0]).write_text("# hook", encoding="utf-8")
    moved()
    config.capture.projects = ["proj-a"]
    moved()
    config.exclude_projects = ["proj-b"]
    moved()
    config.thresholds = {"coaching_drip_count": 4, "idle_minutes": 9}
    moved()
    config.thresholds = {"coaching_drip_count": 4, "idle_minutes": 10}  # not a coaching threshold: not an input
    assert now() == seen[-1]


def test_a_moment_is_found_by_when_the_message_was_typed(tmp_path):
    top = _parsed(tmp_path, [_said("one", 0), _reply(2), _said("two", 3), _reply(4), _said("three", 5), _said("four", 5.5), _reply(6)])
    cycles = capture.prompt_cycles(top)
    times = hint_occasions.typed_times(top, cycles)
    assert [t.strftime("%M:%S") for t in times] == ["00:00", "03:00", "05:30"]


def _plain(text: str) -> None:
    test_tuning._plain(text, "line")


def test_the_silent_sentence_keeps_to_the_copy_rules():
    one = hint_occasions.silent_sentence({"big_paste": 1}, 1)
    assert one == (
        "1 session had a moment for a coaching tip and none came: a big paste 1. "
        "'claudeglass capture status' checks the capture hook."
    )
    two = hint_occasions.silent_sentence({"big_paste": 2}, 2)
    assert two == (
        "2 sessions had a moment for a coaching tip and none came: a big paste 2. "
        "'claudeglass capture status' checks the capture hook."
    )
    three = hint_occasions.silent_sentence({"drip_feed": 2, "big_paste": 2, "status_poll": 2}, 3)
    assert three == (
        "3 sessions had a moment for a coaching tip and none came: small requests 2, a big paste 2, a status check 2. "
        "'claudeglass capture status' checks the capture hook."
    )
    assert hint_occasions.silent_sentence({}, 0) is None
    for text in (one, two, three):
        _plain(text)
        assert capture_view.STATUS_COMMAND in text

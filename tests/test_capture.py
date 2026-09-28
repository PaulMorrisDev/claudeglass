"""What metrics capture costs (``capture.py``): prompt cycles and the
subagents they started, what capture cost while it ran (measured from the
notes and tags in the transcripts), and what each level or metric would
cost, replayed from your own history.

Amounts use ``tests/fixtures/pricing_min.toml``'s ``claude-widget-9``
(per million tokens: input 1.0, output 2.0, 5-minute cache write 0.5,
cache read 0.1; fast mode doubles every rate), so each expected value can
be worked out by hand.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from claudeglass import capture, capture_catalogue as catalogue, parse
from claudeglass.model import TranscriptMeta
from claudeglass.parse import parse_transcript
from claudeglass.pricing import load_pricing

from helpers import (
    old_agent_note_text,
    attachment_line,
    system_line,
    tool_result_block,
    tool_use_block,
    turn_line,
    user_block_line,
    user_str_line,
    write_jsonl,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MODEL = "claude-widget-9"
#: USD per character, at 4 characters a token.
OUT = 2.0 / 4 / 1e6
WRITE = 0.5 / 4 / 1e6
READ = 0.1 / 4 / 1e6


@pytest.fixture(autouse=True)
def _salt():
    parse.set_salt(b"c" * 32)


@pytest.fixture()
def pricing():
    return load_pricing(path=FIXTURES / "pricing_min.toml")


def _ts(second: int) -> str:
    return f"2026-09-18T12:{second // 60:02d}:{second % 60:02d}.000Z"


def _ask(second: int, text: str = "do it") -> dict:
    return user_str_line(text, origin={"kind": "human"}, timestamp=_ts(second))


def _reply(second: int, *blocks, text: str = "ok", **kw) -> dict:
    content = list(blocks) or [{"type": "text", "text": text}]
    return turn_line(content=content, model=MODEL, timestamp=_ts(second), **kw)


def _note(second: int, ids, *, hook: str = "SessionStart", agent_type: str = "") -> dict:
    # An agent's note is one from before 0.11.0: older transcripts hold it,
    # and are still priced.
    text = catalogue.note_text(ids, "main") if hook == "SessionStart" else old_agent_note_text(ids, agent_type)
    wrapped = f"<system-reminder>\n{hook} hook additional context: {text}\n</system-reminder>"
    line = attachment_line("hook_additional_context", rendered=wrapped, content=[text], hookName=hook,
                           hookEvent=hook, toolUseID=hook)
    line["timestamp"] = _ts(second)
    return line


def _chars(note_line: dict) -> int:
    return len(note_line["rendered"][0]["content"])


def _parse(tmp_path, name: str, lines, **meta):
    path = tmp_path / name
    write_jsonl(path, lines)
    return parse_transcript(path, TranscriptMeta(path=str(path), **meta))


def _top(tmp_path, lines):
    return _parse(tmp_path, "top.jsonl", lines, kind="top-level")


def _sub(tmp_path, agent: str, lines, *, tool_use_id: str, agent_type: str = "general-purpose", parent: str = ""):
    return _parse(tmp_path, f"agent-{agent}.jsonl", lines, kind="subagent", agent_id=f"agent-{agent}",
                  agent_type=agent_type, tool_use_id=tool_use_id, parent_agent_id=parent or None)


def _corpus(top, *subs):
    return NS(sessions=[NS(top=top, subs=list(subs), session_id="s1")])


# -- prompt cycles ---------------------------------------------------------------


def test_cycles_run_from_each_message_to_the_next_and_collect_nested_agents(tmp_path):
    top = _top(tmp_path, [
        _reply(0, text="left over from a resumed session"),
        _ask(1, "first"),
        _reply(2, tool_use_block("Agent", "toolu_A", {"prompt": "look"})),
        user_block_line([tool_result_block("toolu_A", "found")], timestamp=_ts(8)),
        _reply(9),
        _ask(10, "second"),
        _reply(11, tool_use_block("Agent", "toolu_C", {"prompt": "fix"})),
    ])
    a = _sub(tmp_path, "a1", [
        user_str_line("look", timestamp=_ts(3)),
        _reply(4, tool_use_block("Agent", "toolu_B", {"prompt": "deeper"})),
    ], tool_use_id="toolu_A")
    # A nested agent's parentAgentId is the bare id, without "agent-".
    b = _sub(tmp_path, "b2", [user_str_line("deeper", timestamp=_ts(5)), _reply(6)], tool_use_id="toolu_B",
             parent="a1")
    c = _sub(tmp_path, "c3", [user_str_line("fix", timestamp=_ts(12)), _reply(13)], tool_use_id="toolu_C")
    stray = _sub(tmp_path, "d4", [user_str_line("?", timestamp=_ts(14)), _reply(15)], tool_use_id="toolu_X")

    cycles = capture.prompt_cycles(top, [a, b, c, stray])
    assert [len(cycle.turns) for cycle in cycles] == [2, 1]
    assert [cycle.subs for cycle in cycles] == [[a, b], [c]]


def test_the_last_tag_in_a_cycle_is_the_one_that_counts(tmp_path):
    top = _top(tmp_path, [
        _note(0, ["task"]),
        _ask(1),
        _reply(2, text="Looking.\n[cg: task=debug]"),
        _reply(3, text="Found it.\n[cg: task=bugfix]"),
        _ask(4),
        _reply(5, text="untagged"),
    ])
    first, second = capture.prompt_cycles(top)
    assert first.tag.task == "bugfix"
    assert second.tag is None


def test_a_cycles_tags_merge_key_by_key(tmp_path):
    # CAP-10: a retry's tag winning "task" doesn't erase a key only the
    # earlier tag answered -- they merge key by key, not tag-for-tag.
    top = _top(tmp_path, [
        _note(0, ["task", "level", "shift"]),
        _ask(1),
        _reply(2, text="Looking.\n[cg: task=debug level=hard]"),
        _reply(3, text="Found it.\n[cg: task=bugfix shift=redo]"),
    ])
    [cycle] = capture.prompt_cycles(top)
    turn2, turn3 = top.turns[0], top.turns[1]
    tag = cycle.tag
    assert (tag.task, tag.level, tag.shift) == ("bugfix", "hard", "redo")
    assert tag.has_tl is True
    assert tag.chars == turn2.cap.chars + turn3.cap.chars


# -- measured use ------------------------------------------------------------------


def _captured_session(tmp_path, *, speed=None):
    """A captured session: an Essentials note, a tagged reply that starts
    an agent, an untagged reply, a compaction, and one more message."""
    ids = catalogue.level_metrics("essentials")
    note = _note(0, ids)
    top = _top(tmp_path, [
        note,
        _ask(1, "hi"),
        _reply(2, tool_use_block("Agent", "toolu_A", {"prompt": "[spawn: isolate] check it"}),
               {"type": "text", "text": "Started.\n[cg: task=bugfix brief=clear]"}, speed=speed),
        user_block_line([tool_result_block("toolu_A", "Done.")], timestamp=_ts(6)),
        _ask(7, "more"),
        _reply(8),
        system_line("compact_boundary", timestamp=_ts(9)),
        _ask(10, "again"),
        _reply(11),
    ])
    sub_note = _note(3, ids, hook="SubagentStart", agent_type="general-purpose")
    sub = _sub(tmp_path, "a1", [
        sub_note,
        user_str_line("[spawn: isolate] check it", timestamp=_ts(3)),
        _reply(4, text="Checked.\n[result: done]"),
    ], tool_use_id="toolu_A")
    return top, sub, _chars(note), _chars(sub_note)


def test_usage_prices_notes_until_the_compaction_and_tags_at_the_writer_rate(tmp_path, pricing):
    top, sub, note_chars, sub_note_chars = _captured_session(tmp_path)
    use = capture.usage(_corpus(top, sub), pricing)

    # The session note is written with the first turn, read by the next,
    # and dropped by the compaction before the third.
    main = use.scopes["main"]
    assert main.note_cost == pytest.approx(note_chars * (WRITE + READ))
    assert main.note_tokens == round(note_chars / 4)
    tag = len("[cg: task=bugfix brief=clear]") + 1
    # CAP-2: the tag is Claude's own output on turn 1 (OUT), then sits in
    # context and is cache-written once more into turn 2's prompt (WRITE);
    # the compaction before turn 3 drops it before it is ever read back.
    assert main.tag_cost == pytest.approx(tag * (OUT + WRITE))
    # The agent's note is written once; its report tag is output.
    agent = use.scopes["subagent"]
    assert agent.note_cost == pytest.approx(sub_note_chars * WRITE)
    assert agent.tag_cost == pytest.approx((len("[result: done]") + 1) * OUT)
    # The spawn word opens the brief, so the main session wrote it.
    assert use.scopes["brief"].tag_cost == pytest.approx((len("[spawn: isolate]") + 1) * OUT)

    assert (use.sessions, use.subagents, use.notes) == (1, 1, 2)
    assert (use.cycles, use.tagged_cycles, use.reports, use.tagged_reports) == (3, 1, 1, 1)
    assert use.coverage == pytest.approx(100 / 3)
    assert use.report_coverage == 100.0
    assert use.cost == pytest.approx(sum(s.cost for s in use.scopes.values()))
    assert sum(use.by_metric.values()) == pytest.approx(use.cost)
    assert sum(use.daily.values()) == pytest.approx(use.cost)
    assert list(use.daily) == ["2026-09-18"]
    assert 0 < use.share < 100
    assert {k: use.answers[k] for k in ("task", "brief", "result", "spawn")} == {
        "task": 1, "brief": 1, "result": 1, "spawn": 1}
    assert "level" not in use.answers


def test_a_note_after_a_compact_boundary_is_priced_and_counted_separately(tmp_path, pricing):
    """SURV-3: a note whose own turn lands at or after a real
    compact_boundary is tallied under after_compact_notes/
    after_compact_cost in addition to its ordinary scope -- the carried
    prefix a compaction would otherwise have discounted it against is
    gone by then."""
    before = _note(0, ["task"])
    after = _note(4, ["task"])
    top = _top(tmp_path, [
        before,
        _ask(1),
        _reply(2, text="Looking.\n[cg: task=debug]"),
        system_line("compact_boundary", timestamp=_ts(3)),
        after,
        _ask(5),
        _reply(6, text="Done.\n[cg: task=bugfix]"),
    ])
    use = capture.usage(_corpus(top), pricing)
    assert use.notes == 2
    assert use.after_compact_notes == 1
    assert use.after_compact_cost == pytest.approx(_chars(after) * WRITE)
    # Both notes individually cost the same write-only amount here, so the
    # scope total (both notes) is strictly more than the after-compact
    # share (one of them).
    assert use.after_compact_cost < use.scopes["main"].note_cost


def test_no_after_compact_notes_without_a_real_compaction(tmp_path, pricing):
    top, sub, _, _ = _captured_session(tmp_path)
    use = capture.usage(_corpus(top, sub), pricing)
    # _captured_session's only note (t=0) precedes its one compaction, so
    # nothing here lands after a boundary.
    assert use.after_compact_notes == 0
    assert use.after_compact_cost == 0.0


def test_fast_mode_doubles_what_the_fast_turn_wrote_and_carried(tmp_path, pricing):
    top, sub, note_chars, _ = _captured_session(tmp_path, speed="fast")
    use = capture.usage(_corpus(top, sub), pricing)
    # The first turn ran fast: its cache write, its tag and the spawn word
    # it wrote cost double; the next turn's read does not.
    assert use.scopes["main"].note_cost == pytest.approx(note_chars * (2 * WRITE + READ))
    # The tag's own output doubles (turn 1 ran fast), but turn 2 -- the
    # turn that carries it forward into its prompt -- did not, so that
    # carry-write portion stays at the normal rate.
    assert use.scopes["main"].tag_cost == pytest.approx((len("[cg: task=bugfix brief=clear]") + 1) * (2 * OUT + WRITE))
    assert use.scopes["brief"].tag_cost == pytest.approx((len("[spawn: isolate]") + 1) * 2 * OUT)


def test_usage_since_leaves_out_what_came_before(tmp_path, pricing):
    top, sub, _, _ = _captured_session(tmp_path)
    use = capture.usage(_corpus(top, sub), pricing, since="2026-09-18T12:00:07Z")
    assert (use.notes, use.cycles, use.tagged_cycles, use.reports) == (0, 2, 0, 0)
    assert use.cost == 0.0
    assert use.spend > 0


def test_sessions_without_a_capture_note_are_not_counted(tmp_path, pricing):
    top = _top(tmp_path, [_ask(0), _reply(1, text="Done.\n[cg: task=bugfix]")])
    use = capture.usage(_corpus(top), pricing)
    assert (use.sessions, use.cycles, use.cost, use.coverage, use.share) == (0, 0, 0.0, None, None)


def test_a_feedback_run_cycle_is_left_out_of_the_coverage_denominator(tmp_path, pricing):
    # CAP-10: a /cg-feedback run answers /cg-feedback's own question, not
    # the one an ordinary reply reports on -- it shouldn't count against
    # coverage just because it never wrote a [cg: ...] task tag either.
    top = _top(tmp_path, [
        _note(0, ["task"]),
        user_str_line(
            "<command-message>cg-feedback</command-message>\n<command-name>/cg-feedback</command-name>",
            timestamp=_ts(1),
        ),
        _reply(2, text="Thanks: ClaudeGlass will use this for your savings tips."),
        _ask(3),
        _reply(4, text="Done.\n[cg: task=bugfix]"),
    ])
    use = capture.usage(_corpus(top), pricing)
    assert (use.cycles, use.tagged_cycles) == (1, 1)
    assert use.coverage == 100.0


def test_a_max_tokens_cycle_is_left_out_of_the_coverage_denominator(tmp_path, pricing):
    # CAP-10: a reply cut off by max_tokens never got to write its tag.
    cut_off = _reply(2, text="Still working")
    cut_off["message"]["stop_reason"] = "max_tokens"
    top = _top(tmp_path, [
        _note(0, ["task"]),
        _ask(1),
        cut_off,
        _ask(3),
        _reply(4, text="Done.\n[cg: task=bugfix]"),
    ])
    use = capture.usage(_corpus(top), pricing)
    assert (use.cycles, use.tagged_cycles) == (1, 1)
    assert use.coverage == 100.0


def test_an_interrupted_cycle_is_left_out_of_the_coverage_denominator(tmp_path, pricing):
    # CAP-10: the user cut Claude off before it could write its tag.
    top = _top(tmp_path, [
        _note(0, ["task"]),
        _ask(1),
        _reply(2, text="cut off mid-thought"),
        user_str_line("[Request interrupted by user]", timestamp=_ts(3)),
        _ask(4, "try again"),
        _reply(5, text="Done.\n[cg: task=bugfix]"),
    ])
    use = capture.usage(_corpus(top), pricing)
    assert (use.cycles, use.tagged_cycles) == (1, 1)
    assert use.coverage == 100.0


def test_a_tool_note_counts_in_the_tool_scope(tmp_path, pricing):
    ids = catalogue.level_metrics("deep")
    text = catalogue.tool_note_text("big_output")
    tool_note = attachment_line("hook_additional_context", rendered=f"<system-reminder>\nPostToolUse hook "
                                f"additional context: {text}\n</system-reminder>", content=[text],
                                hookName="PostToolUse", hookEvent="PostToolUse", toolUseID="toolu_1")
    tool_note["timestamp"] = _ts(3)
    top = _top(tmp_path, [
        _note(0, ids),
        _ask(1),
        _reply(2, tool_use_block("Bash", "toolu_1", {"command": "make"})),
        user_block_line([tool_result_block("toolu_1", "x" * 40_000)], timestamp=_ts(3)),
        tool_note,
        _reply(4, text="Built.\n[cg: task=ops out=part]"),
    ])
    use = capture.usage(_corpus(top), pricing)
    assert use.scopes["tool"].note_cost == pytest.approx(_chars(tool_note) * WRITE)
    assert use.by_metric["big_output"] > 0
    assert use.answers["big_output"] == 1


# -- estimates from your own history ------------------------------------------------


def _history_corpus(tmp_path):
    top = _top(tmp_path, [
        _ask(0),
        _reply(1, tool_use_block("Agent", "toolu_A", {"prompt": "a"}), tool_use_block("Agent", "toolu_E", {"prompt": "e"})),
        user_block_line([tool_result_block("toolu_A", "a"), tool_result_block("toolu_E", "e")], timestamp=_ts(6)),
        _reply(7, tool_use_block("Bash", "toolu_B", {"command": "make"})),
        user_block_line([tool_result_block("toolu_B", "x" * 40_000)], timestamp=_ts(8)),
        _reply(9),
        _ask(10),
        _reply(11),
    ])
    general = _sub(tmp_path, "a1", [user_str_line("a", timestamp=_ts(2)), _reply(3), _reply(4)], tool_use_id="toolu_A")
    explore = _sub(tmp_path, "e1", [user_str_line("e", timestamp=_ts(2)), _reply(3)], tool_use_id="toolu_E",
                   agent_type="Explore")
    setup = _sub(tmp_path, "s1", [user_str_line("s", timestamp=_ts(2)), _reply(3)], tool_use_id="toolu_S",
                 agent_type="statusline-setup")
    return _corpus(top, general, explore, setup)


def test_history_prices_one_character_in_each_place(tmp_path, pricing):
    past = capture.history(_history_corpus(tmp_path), pricing, days=7)
    assert (past.days, past.sessions, past.cycles, past.subagents) == (7, 1, 2, 2)
    assert (past.main_notes, past.sub_notes) == (1, 2)
    # Main: written with the first of 4 turns, read by the other 3.
    assert past.main_note == pytest.approx(WRITE + 3 * READ)
    assert past.sub_note == pytest.approx(WRITE + READ)
    assert past.sub_note_no_rules == pytest.approx(WRITE)
    # CAP-2: each cycle's reply tag prices its own output plus, when a
    # later turn follows it, one cache-write carry into that turn. The
    # first cycle's tag (turn 3 of 4) carries into the 4th; the second
    # cycle's tag is the corpus's last turn, so it has nothing to carry
    # into.
    assert past.reply_tag == pytest.approx(2 * OUT + WRITE)
    # Both subagents' report tags are their transcript's own last turn,
    # so neither has a later turn to carry into -- unchanged from output
    # cost alone.
    assert past.report_tag == pytest.approx(2 * OUT)
    # The brief marker isn't a [cg:]/[result:] tag -- it stays priced at
    # output cost alone here too, same as the real usage() path's own
    # _add_brief_markers.
    assert past.brief_tag == pytest.approx(2 * OUT)
    # The 40k-character result arrives with the third turn, carried to the end.
    assert past.big_outputs == 1
    assert past.big_output_note == pytest.approx(WRITE + READ)
    # CAP-2: the big-output tag is written with the 3rd (of 4) turns and
    # carried into the 4th.
    assert past.big_output_tag == pytest.approx(OUT + WRITE)
    assert past.spend > 0


def test_big_output_notes_are_estimated_per_call_not_per_turn(tmp_path, pricing):
    # CAP-10: two big results from the same tool in one turn cost two
    # notes, not one -- Claude Code fires PostToolUse per call, not once
    # per turn regardless of how many of its calls crossed the threshold.
    big = "x" * 35_000
    top = _top(tmp_path, [
        _ask(0),
        _reply(1, tool_use_block("Bash", "toolu_B", {"command": "make"}),
               tool_use_block("Bash", "toolu_C", {"command": "test"})),
        user_block_line([tool_result_block("toolu_B", big), tool_result_block("toolu_C", big)], timestamp=_ts(2)),
        _reply(3),
    ])
    past = capture.history(_corpus(top), pricing, days=7)
    assert past.big_outputs == 2


def test_estimates_rise_with_the_level_and_scale_with_sampling(tmp_path, pricing):
    past = capture.history(_history_corpus(tmp_path), pricing, days=7)
    levels = capture.level_estimates(past)
    assert list(levels) == ["free", "essentials", "standard", "deep"]
    assert levels["free"].cost == 0.0 and levels["free"].note_tokens == 0
    assert 0 < levels["essentials"].cost < levels["standard"].cost < levels["deep"].cost
    half = capture.level_estimates(past, sample=50)["standard"]
    assert half.cost == pytest.approx(levels["standard"].cost / 2)
    assert levels["standard"].per_week == pytest.approx(levels["standard"].cost)
    assert levels["standard"].share == pytest.approx(100 * levels["standard"].cost / past.spend)
    assert capture.estimate(past, ()).cost == 0.0


def test_an_estimate_is_the_note_and_tag_sizes_times_the_unit_costs(tmp_path, pricing):
    past = capture.history(_history_corpus(tmp_path), pricing, days=7)
    ids = ("task",)
    main = len(catalogue.note_text(ids, "main")) + catalogue.NOTE_WRAP_CHARS + len("SessionStart")
    reply = catalogue.METRICS_BY_ID["task"].out_chars + 7
    assert capture.estimate(past, ids).cost == pytest.approx(main * past.main_note + reply * past.reply_tag)


def test_metric_estimates_price_what_each_metric_adds(tmp_path, pricing):
    past = capture.history(_history_corpus(tmp_path), pricing, days=7)
    ids = catalogue.level_metrics("standard")
    parts = capture.metric_estimates(past, ids)
    assert set(parts) == set(ids)
    assert parts["session_end"] == 0.0
    assert parts["task"] > 0
    # Agent runs cost a Haiku call each, whichever agent metrics are on:
    # leaving out result leaves out the rest that need it, and the call;
    # leaving out one of the rest saves nothing.
    needs_result = [i for i in ids if "result" in catalogue.METRICS_BY_ID[i].requires]
    assert needs_result and all(parts[i] == 0.0 for i in needs_result)
    assert past.subagents and parts["result"] == pytest.approx(past.subagents * catalogue.JUDGE_USD_PER_CALL)


def test_enough_data_counts_answers_against_each_target():
    use = capture.CaptureUsage(answers={"task": 12})
    assert capture.enough_data(use, "task") == (12, capture.ENOUGH["main"])
    assert capture.enough_data(use, "fit") == (0, capture.ENOUGH["subagent"])
    # Judged per agent run, as the rest of an agent's words are.
    assert capture.enough_data(use, "retry") == (0, capture.ENOUGH["subagent"])
    assert capture.enough_data(use, "big_output") == (0, capture.ENOUGH["tool"])
    assert capture.enough_data(use, "waits", signal_sessions=5) == (5, capture.ENOUGH["signal"])
    assert capture.enough_target("no-such-metric") == 0


def test_estimate_prices_the_feedback_reminder_once_per_session():
    past = capture.History(days=14, sessions=2, cycles=10, subagents=3, main_notes=2, main_note=1e-6, reply_tag=2e-6,
                           brief_tag=5e-6)
    est = capture.estimate(past, ("feedback_reminder",))
    note = len(catalogue.note_text(("feedback_reminder",), "main")) + capture._WRAP["SessionStart"]
    out = catalogue.METRICS_BY_ID["feedback_reminder"].out_chars
    # 2 sessions of 10 messages: 2 reminders, each carried like a reply's tag.
    assert est.cost == pytest.approx(note * 1e-6 + out * 2e-6 * 2 / 10)
    assert est.tag_tokens == round(out * 2 / 4)
    rough = catalogue.rough_tokens(("task", "feedback_reminder"))
    assert rough["reminder"] == round(out / 4) and rough["reply_tag"] == round((13 + 6) / 4)


# -- weekly_cost --------------------------------------------------------------------


def _priced_use(since: str, note_cost: float = 0.0, tag_cost: float = 0.0) -> capture.CaptureUsage:
    use = capture.CaptureUsage(since=since)
    use.scopes["main"] = capture.ScopeUse(note_cost=note_cost, tag_cost=tag_cost)
    return use


def test_weekly_cost_spreads_what_was_measured_over_the_weeks_since_it_began():
    use = _priced_use("2026-09-01T00:00:00+00:00", note_cost=1.0, tag_cost=0.4)
    now = datetime(2026, 9, 15, tzinfo=timezone.utc)
    assert capture.weekly_cost(use, now=now) == pytest.approx(1.4 / 2)


def test_weekly_cost_is_none_without_a_start_time():
    assert capture.weekly_cost(capture.CaptureUsage(since="")) is None


def test_weekly_cost_is_none_less_than_a_day_after_it_began():
    use = _priced_use("2026-09-24T00:00:00+00:00", note_cost=1.0)
    now = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
    assert capture.weekly_cost(use, now=now) is None

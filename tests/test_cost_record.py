"""Claude Code's own cost record (``cost-state``) against ClaudeGlass's
pricing -- ``parse.py`` keeps when the total was written and its cost by
model, ``reconcile.claude_code_reported_costs`` compares the same span,
and the ``cost-record`` check reports it. Shapes are from real Claude Code
2.1.283 sessions, where a ``cost-state`` line is a running total written
now and then (see ``model.py``'s module docstring).
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from claudeglass import quick_actions as qa
from claudeglass import reconcile
from claudeglass.corpus import load_corpus
from claudeglass.model import TranscriptMeta, Turn
from claudeglass.parse import parse_transcript
from claudeglass.pricing import load_pricing, price_turn
from claudeglass.units import Units

from helpers import tool_result_block, tool_use_block, turn_line, user_block_line, user_str_line, write_jsonl

SESSION = "7e5fd47d-1816-569f-9241-e02958cf9196"
SONNET = "claude-sonnet-5"
HAIKU = "claude-haiku-4-5-20251001"


def _reply(message_id: str, timestamp: str, stop_reason: str | None = "end_turn", **usage) -> dict:
    line = turn_line(message_id=message_id, model=SONNET, timestamp=timestamp, **usage)
    line["message"]["stop_reason"] = stop_reason
    return line


def _cost_state(total: float, by_model: dict[str, float]) -> dict:
    return {
        "type": "cost-state",
        "sessionId": SESSION,
        "totalCostUSD": total,
        "startTime": 1790415397937,
        "modelUsage": {model: {"inputTokens": 1, "outputTokens": 1, "costUSD": cost} for model, cost in by_model.items()},
        "hasUnknownModelCost": False,
    }


def _sonnet_cost(**usage) -> float:
    """What a Sonnet 5 reply with ``usage`` costs."""
    return price_turn(Turn(model=SONNET, turn_index=1, **usage), load_pricing().resolve_model(SONNET)).total


def _session_lines(*, stopped: bool = True, after: bool = True) -> list[dict]:
    """Two replies, one maybe stopped mid-stream, then Claude Code's total
    (which counts neither the stopped reply nor the reply after it, but
    does count a title request on Haiku), then one more reply."""
    first = _sonnet_cost(input_tokens=1000, output_tokens=500)
    haiku = 0.001
    lines = [
        user_str_line("start", timestamp="2026-09-26T10:00:00.000Z", sessionId=SESSION),
        _reply("msg_1", "2026-09-26T10:00:05.000Z", input_tokens=1000, output_tokens=500, sessionId=SESSION),
    ]
    if stopped:
        lines.append(_reply("msg_2", "2026-09-26T10:01:00.000Z", stop_reason=None, input_tokens=2000,
                            output_tokens=9, sessionId=SESSION))
    lines.append(user_str_line("[Request interrupted by user]", timestamp="2026-09-26T10:01:01.000Z",
                               sessionId=SESSION))
    lines.append(_cost_state(first + haiku, {SONNET: first, HAIKU: haiku, "bad id/../x": 5.0}))
    if after:
        lines.append(_reply("msg_3", "2026-09-26T11:00:00.000Z", input_tokens=4000, output_tokens=800,
                            sessionId=SESSION))
    return lines


def _write(tmp_path: Path, lines: list[dict]) -> Path:
    folder = tmp_path / "-home-user-shop"
    folder.mkdir(parents=True, exist_ok=True)
    write_jsonl(folder / f"{SESSION}.jsonl", lines)
    return folder


def test_the_parser_keeps_when_the_total_was_written_and_its_models(tmp_path: Path):
    path = _write(tmp_path, _session_lines()) / f"{SESSION}.jsonl"

    meta = parse_transcript(path, TranscriptMeta(path=str(path), kind="top-level", session_id=SESSION)).meta

    assert meta.cc_cost_as_of == "2026-09-26T10:01:01.000Z"
    assert set(meta.cc_cost_by_model) == {SONNET, HAIKU}


def test_the_comparison_stops_where_claude_codes_total_stops(tmp_path: Path):
    corpus = load_corpus([_write(tmp_path, _session_lines(stopped=False))])

    [cost] = reconcile.claude_code_reported_costs(corpus, load_pricing())

    first = _sonnet_cost(input_tokens=1000, output_tokens=500)
    assert cost.as_of == "2026-09-26T10:01:01.000Z"
    assert cost.local_cost_usd == pytest.approx(first)
    assert cost.local_by_model == {SONNET: pytest.approx(first)}


def test_a_stopped_reply_and_an_unlogged_request_explain_the_difference(tmp_path: Path):
    corpus = load_corpus([_write(tmp_path, _session_lines())])

    [cost] = reconcile.claude_code_reported_costs(corpus, load_pricing())

    # The reply has no stop reason, so it also counts as logged mid-stream
    # and carries an output estimate; pressing Esc after it makes it a
    # stopped one, and its whole cost, estimate included, is that cause.
    [stopped_turn] = [t for t in corpus.sessions[0].top.turns if t.message_id == "msg_2"]
    assert stopped_turn.output_partial
    assert cost.stopped_usd == pytest.approx(
        price_turn(stopped_turn, load_pricing().resolve_model(SONNET)).total
    )
    assert cost.unlogged_usd == pytest.approx(0.001)
    assert cost.local_cost_usd - cost.cc_cost_usd == pytest.approx(cost.stopped_usd - cost.unlogged_usd)
    assert cost.unexplained_usd == pytest.approx(0.0, abs=1e-12)


def test_no_reply_counts_as_stopped_when_the_log_records_no_stop_reasons(tmp_path: Path):
    lines = [
        _reply("msg_1", "2026-09-26T10:00:05.000Z", stop_reason=None, sessionId=SESSION),
        _reply("msg_2", "2026-09-26T10:00:10.000Z", stop_reason=None, sessionId=SESSION),
        _cost_state(0.01, {SONNET: 0.01}),
    ]
    corpus = load_corpus([_write(tmp_path, lines)])

    [cost] = reconcile.claude_code_reported_costs(corpus, load_pricing())

    assert cost.stopped_usd == 0.0


def test_a_resumed_sessions_total_is_compared_from_the_resume(tmp_path: Path):
    """``startTime`` is when the process whose total it is started: after
    a resume, the replies before it are in no total Claude Code wrote."""
    state = _cost_state(0.01, {SONNET: 0.01})
    state["startTime"] = 1790416830000  # 2026-09-26T10:00:30Z
    lines = [
        _reply("msg_1", "2026-09-26T10:00:05.000Z", input_tokens=1000, output_tokens=500, sessionId=SESSION),
        _reply("msg_2", "2026-09-26T10:01:00.000Z", input_tokens=4000, output_tokens=800, sessionId=SESSION),
        state,
    ]
    folder = _write(tmp_path, lines)
    path = folder / f"{SESSION}.jsonl"

    meta = parse_transcript(path, TranscriptMeta(path=str(path), kind="top-level", session_id=SESSION)).meta
    [cost] = reconcile.claude_code_reported_costs(load_corpus([folder]), load_pricing())

    assert meta.cc_cost_since == "2026-09-26T10:00:30Z"
    assert cost.since == "2026-09-26T10:00:30Z"
    assert cost.local_cost_usd == pytest.approx(_sonnet_cost(input_tokens=4000, output_tokens=800))


def test_a_reply_that_called_a_tool_was_not_stopped(tmp_path: Path):
    """A subagent's reply is often logged mid-stream, with no
    ``stop_reason``, while the tool it asked for already runs; Claude Code
    bills it, so it isn't taken out of ClaudeGlass's side."""
    called = _reply("msg_2", "2026-09-26T10:00:10.000Z", stop_reason=None, input_tokens=2000, output_tokens=3,
                    content=[tool_use_block("Read", "toolu_1", {"file_path": "a.py"})], sessionId=SESSION)
    lines = [_reply("msg_1", "2026-09-26T10:00:05.000Z", sessionId=SESSION), called, _cost_state(0.01, {SONNET: 0.01})]
    corpus = load_corpus([_write(tmp_path, lines)])

    [cost] = reconcile.claude_code_reported_costs(corpus, load_pricing())

    assert cost.stopped_usd == 0.0


def _check(tmp_path: Path, costs) -> dict:
    section = reconcile.build_cost_record_section(costs)
    config_dir = tmp_path / ".claude" / "claudeglass"
    config_dir.mkdir(parents=True, exist_ok=True)
    ctx = qa.Context(
        model=NS(sections=[section], recommendations=[]),
        units=Units(billing_mode="api", currency="USD"),
        period="over the last 14 days",
        config_dir=config_dir,
        effective={},
        effective_agents={},
    )
    return qa.run("cost-record", ctx)


def test_the_check_is_ok_when_the_known_reasons_explain_the_difference(tmp_path: Path):
    costs = reconcile.claude_code_reported_costs(load_corpus([_write(tmp_path, _session_lines())]), load_pricing())

    result = _check(tmp_path, costs)

    assert result["status"] == "ok"
    assert "0.0% is left" in result["summary"]
    assert result["table"]["rows"][0][:2] == [SESSION[:8], "2026-09-26 10:01 UTC"]


def test_the_check_asks_for_a_report_when_much_is_left_unexplained(tmp_path: Path):
    costs = [reconcile.ClaudeCodeCost(session_id=SESSION, cc_cost_usd=10.0, local_cost_usd=11.0, as_of="x")]

    result = _check(tmp_path, costs)

    assert result["status"] == "act"
    assert "+10.0%" in result["summary"]


def test_the_check_has_nothing_to_say_without_a_record(tmp_path: Path):
    result = _check(tmp_path, [])

    assert result["status"] == "no_data"


def test_the_section_adds_up_the_sessions_and_their_reasons():
    costs = [
        reconcile.ClaudeCodeCost(session_id="a", cc_cost_usd=10.0, local_cost_usd=10.5, as_of="2026-09-26T10:00:00Z",
                                 stopped_usd=0.5),
        reconcile.ClaudeCodeCost(session_id="b", cc_cost_usd=5.0, local_cost_usd=4.9, as_of="2026-09-26T11:00:00Z",
                                 unlogged_usd=0.1),
    ]

    section = reconcile.build_cost_record_section(costs)
    summary, sessions = section.tables
    row = dict(zip([c.key for c in summary.columns], summary.rows[0]))

    assert row["sessions"] == 2
    assert row["difference_pct"] == pytest.approx(100 * 0.4 / 15)
    assert row["unexplained_pct"] == pytest.approx(0.0, abs=1e-9)
    assert [r[0] for r in sessions.rows] == ["b", "a"]


# -- replies logged mid-stream (output estimated) ---------------------------


def _partial_lines(*, esc: bool = False, tool: bool = True) -> list[dict]:
    """A finished reply, a reply logged only mid-stream (output 7, no stop
    reason) whose context the next request shows grew by 2,000 tokens
    with 4,000 characters of tool result (so it wrote 1,000), a finished
    reply, then Claude Code's total. Without a tool the reply is a text
    one, and ``esc`` adds the interrupt line Esc leaves after it."""
    content = [tool_use_block("Read", "toolu_2", {"file_path": "a.py"})] if tool else [{"type": "text", "text": "ok"}]
    lines = [
        user_str_line("start", timestamp="2026-09-26T10:00:00.000Z", sessionId=SESSION),
        _reply("msg_1", "2026-09-26T10:00:05.000Z", input_tokens=3000, output_tokens=40, sessionId=SESSION),
        _reply("msg_2", "2026-09-26T10:00:10.000Z", stop_reason=None, input_tokens=5000, output_tokens=7,
               content=content, sessionId=SESSION),
    ]
    if tool:
        lines.append(user_block_line([tool_result_block("toolu_2", "x" * 4000)], timestamp="2026-09-26T10:00:12.000Z",
                                     sessionId=SESSION))
    if esc:
        lines.append(user_str_line("[Request interrupted by user]", timestamp="2026-09-26T10:00:13.000Z",
                                   sessionId=SESSION))
    lines.append(_reply("msg_3", "2026-09-26T10:00:20.000Z", input_tokens=7000, output_tokens=300, sessionId=SESSION))
    local = (
        _sonnet_cost(input_tokens=3000, output_tokens=40)
        + _sonnet_cost(input_tokens=5000, output_tokens=1000)
        + _sonnet_cost(input_tokens=7000, output_tokens=300)
    )
    lines.append(_cost_state(local, {SONNET: local}))
    return lines


def test_the_estimated_output_of_a_reply_logged_mid_stream_is_its_own_share(tmp_path: Path):
    corpus = load_corpus([_write(tmp_path, _partial_lines())])

    [cost] = reconcile.claude_code_reported_costs(corpus, load_pricing())

    # 993 tokens beyond the 7 logged, at Sonnet 5's $10 per million.
    assert cost.partial_usd == pytest.approx(993 * 10e-6)
    assert cost.stopped_usd == 0.0
    assert cost.local_cost_usd == pytest.approx(cost.cc_cost_usd)
    assert cost.unexplained_usd == pytest.approx(0.0, abs=1e-12)


def test_a_mid_stream_reply_nobody_stopped_is_not_listed_as_stopped_too(tmp_path: Path):
    """A subagent's last reply is a text one, logged mid-stream; Claude
    Code bills it, so it is not taken out of ClaudeGlass's side."""
    corpus = load_corpus([_write(tmp_path, _partial_lines(tool=False))])

    [cost] = reconcile.claude_code_reported_costs(corpus, load_pricing())

    assert cost.stopped_usd == 0.0
    assert cost.partial_usd > 0.0


def test_a_mid_stream_reply_you_pressed_esc_on_is_a_stopped_one(tmp_path: Path):
    corpus = load_corpus([_write(tmp_path, _partial_lines(tool=False, esc=True))])

    [cost] = reconcile.claude_code_reported_costs(corpus, load_pricing())

    [stopped_turn] = [t for t in corpus.sessions[0].top.turns if t.message_id == "msg_2"]
    assert cost.stopped_usd == pytest.approx(price_turn(stopped_turn, load_pricing().resolve_model(SONNET)).total)


def test_the_estimated_share_is_a_column_of_the_summary():
    costs = [reconcile.ClaudeCodeCost(session_id="a", cc_cost_usd=10.0, local_cost_usd=10.0, as_of="x", partial_usd=1.5)]

    summary = reconcile.build_cost_record_section(costs).tables[0]
    row = dict(zip([c.key for c in summary.columns], summary.rows[0]))

    assert row["partial_usd"] == pytest.approx(1.5)
    assert row["unexplained_pct"] == pytest.approx(0.0, abs=1e-9)


# -- one row per Claude Code process ----------------------------------------

PROCESS_START = 1790415397937  # 2026-09-26T09:36:37Z, before every reply below
FORK_A = "aaaaaaaa-1816-569f-9241-e02958cf9196"
FORK_B = "bbbbbbbb-1816-569f-9241-e02958cf9196"
FORK_C = "cccccccc-1816-569f-9241-e02958cf9196"


def _fork_lines(session_id: str, reply_ts: str, total: float, *, start: int | None = PROCESS_START) -> list[dict]:
    """One session of a process that moves through several: its own reply,
    then the process's whole running total so far (the same Haiku request
    in every file)."""
    state = _cost_state(total, {SONNET: total - 0.126, HAIKU: 0.126})
    state["sessionId"] = session_id
    if start is None:
        del state["startTime"]
    else:
        state["startTime"] = start
    return [
        user_str_line("go", timestamp=reply_ts, sessionId=session_id),
        _reply(f"msg_{session_id[:4]}", reply_ts, input_tokens=2000, output_tokens=300, sessionId=session_id),
        state,
    ]


def _forks(tmp_path: Path, **overrides) -> Path:
    folder = tmp_path / "-home-user-shop"
    folder.mkdir(parents=True, exist_ok=True)
    spec = {
        FORK_A: ("2026-10-01T10:00:05.000Z", 9.9649),
        FORK_B: ("2026-10-01T10:10:05.000Z", 10.6284),
        FORK_C: ("2026-10-01T10:20:05.000Z", 11.2921),
    }
    for session_id, (ts, total) in spec.items():
        write_jsonl(folder / f"{session_id}.jsonl", _fork_lines(session_id, ts, total, **overrides))
    return folder


def test_forked_sessions_that_share_a_process_make_one_row(tmp_path: Path):
    corpus = load_corpus([_forks(tmp_path)])

    costs = reconcile.claude_code_reported_costs(corpus, load_pricing())

    [cost] = costs
    assert cost.cc_cost_usd == pytest.approx(11.2921)
    assert cost.session_id == FORK_C
    assert set(cost.sessions) == {FORK_A, FORK_B, FORK_C}
    # The three sessions' replies, all of them, against the one total.
    assert cost.local_cost_usd == pytest.approx(3 * _sonnet_cost(input_tokens=2000, output_tokens=300))


def test_a_model_only_claude_code_billed_is_counted_once_across_the_forks(tmp_path: Path):
    corpus = load_corpus([_forks(tmp_path)])

    [cost] = reconcile.claude_code_reported_costs(corpus, load_pricing())

    assert cost.unlogged_usd == pytest.approx(0.126)


def test_sessions_with_no_process_start_stay_one_row_each(tmp_path: Path):
    corpus = load_corpus([_forks(tmp_path, start=None)])

    costs = reconcile.claude_code_reported_costs(corpus, load_pricing())

    assert len(costs) == 3
    assert {c.session_id for c in costs} == {FORK_A, FORK_B, FORK_C}


def test_every_session_of_a_process_is_covered_by_the_cost_state_row(tmp_path: Path):
    corpus = load_corpus([_forks(tmp_path)])

    gaps = reconcile.cost_ground_truth_gaps(corpus, load_pricing(), None)

    assert [g.source for g in gaps] == ["cost_state"]
    assert gaps[0].cc_cost_usd == pytest.approx(11.2921)


def test_the_summary_counts_the_sessions_of_a_process(tmp_path: Path):
    corpus = load_corpus([_forks(tmp_path)])
    costs = reconcile.claude_code_reported_costs(corpus, load_pricing())

    summary = reconcile.build_cost_record_section(costs).tables[0]
    row = dict(zip([c.key for c in summary.columns], summary.rows[0]))

    assert row["sessions"] == 3
    assert row["cc_usd"] == pytest.approx(11.2921)


def test_the_parser_keeps_when_the_process_started(tmp_path: Path):
    path = _write(tmp_path, _session_lines()) / f"{SESSION}.jsonl"

    meta = parse_transcript(path, TranscriptMeta(path=str(path), kind="top-level", session_id=SESSION)).meta

    assert meta.cc_cost_process_start == 1790415397937


# -- a response stopped by safeguards ---------------------------------------

FABLE = "claude-fable-5-1"


def _refusal_lines(*, refused: bool = True) -> list[dict]:
    """A Sonnet reply, then (when ``refused``) the placeholder reply Claude
    Code logs for a response stopped by safeguards, with no usage; Claude
    Code's total bills Fable 4.084 for the refused request and a title
    request on Haiku."""
    first = _sonnet_cost(input_tokens=1000, output_tokens=500)
    lines = [
        user_str_line("start", timestamp="2026-09-26T10:00:00.000Z", sessionId=SESSION),
        _reply("msg_1", "2026-09-26T10:00:05.000Z", input_tokens=1000, output_tokens=500, sessionId=SESSION),
    ]
    if refused:
        placeholder = turn_line(message_id="msg_refused", model="<synthetic>", timestamp="2026-09-26T10:00:30.000Z",
                                input_tokens=0, output_tokens=0, sessionId=SESSION)
        placeholder["message"]["stop_reason"] = "refusal"
        lines.append(placeholder)
    lines.append(_cost_state(first + 4.084 + 0.001, {SONNET: first, FABLE: 4.084, HAIKU: 0.001}))
    return lines


def test_spend_on_a_response_stopped_by_safeguards_is_labelled_not_left_with_the_small_requests(tmp_path: Path):
    corpus = load_corpus([_write(tmp_path, _refusal_lines())])

    [cost] = reconcile.claude_code_reported_costs(corpus, load_pricing())

    assert cost.safeguards_usd == pytest.approx(4.084)
    # Still unlogged: the safeguards share is part of it, the Haiku title
    # request is the rest.
    assert cost.unlogged_usd == pytest.approx(4.085)


def test_unlogged_spend_with_no_refusal_in_the_log_is_not_called_safeguards(tmp_path: Path):
    corpus = load_corpus([_write(tmp_path, _refusal_lines(refused=False))])

    [cost] = reconcile.claude_code_reported_costs(corpus, load_pricing())

    assert cost.safeguards_usd == 0.0
    assert cost.unlogged_usd == pytest.approx(4.085)


def test_the_safeguards_share_is_a_column_of_the_summary():
    costs = [reconcile.ClaudeCodeCost(session_id="a", cc_cost_usd=10.0, local_cost_usd=6.0, as_of="x",
                                      unlogged_usd=4.0, safeguards_usd=4.0)]

    summary = reconcile.build_cost_record_section(costs).tables[0]
    row = dict(zip([c.key for c in summary.columns], summary.rows[0]))

    assert row["safeguards_usd"] == pytest.approx(4.0)
    assert "A response stopped by safeguards" in summary.columns[-1].label


def test_the_cost_record_notes_keep_to_short_plain_sentences():
    """The dashboard shows these notes as written: 25 words at most to a
    sentence, no camelCase setting key, no " -- " aside."""
    costs = [reconcile.ClaudeCodeCost(session_id="a", cc_cost_usd=10.0, local_cost_usd=6.0, as_of="x",
                                      unlogged_usd=4.0, safeguards_usd=4.0)]

    notes = reconcile.build_cost_record_section(costs).notes

    assert len(notes) >= 6
    for note in notes:
        for sentence in re.split(r"(?<=[.?!])[\"')]*\s+", note):
            assert len(sentence.split()) <= 25, sentence
        assert not re.search(r"[a-z]+[A-Z][A-Za-z]*", note), note
        assert " -- " not in note, note

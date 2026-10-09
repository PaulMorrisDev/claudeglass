"""Single lookups and replies to agent reports (``habits.py``, with the
report sizes it shares with ``topology.py``).

Replies are built by hand so each count and amount can be worked out
from the transcript: ``claude-widget-9`` at ``tests/fixtures/
pricing_min.toml``'s prices, a fixed context read and write on every reply.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from claudeglass import capture as capture_mod, habits, parse
from claudeglass.model import EventKind
from claudeglass.pricing import load_pricing, price_turn
from claudeglass.topology import _report_index

from helpers import attachment_line, tool_result_block, tool_use_block, turn_line, user_block_line, user_str_line

from test_habits import MODEL, _parse, _rows, _table

FIXTURES = Path(__file__).resolve().parent / "fixtures"
START = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _salt():
    parse.set_salt(b"h" * 32)


@pytest.fixture()
def pricing():
    return load_pricing(path=FIXTURES / "pricing_min.toml")


def _at(second: float) -> str:
    return (START + timedelta(seconds=second)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _human(second: float, text: str = "go") -> dict:
    return user_str_line(text, origin={"kind": "human"}, timestamp=_at(second))


def _step(second: float, *blocks, message_id: str | None = None, **usage) -> dict:
    """One reply, reading 40k cached tokens and writing 2k (``usage`` changes
    that), with ``blocks`` as its content or a line of text."""
    fields = {"cache_read_input_tokens": 40_000, "cache_creation_input_tokens": 2_000, **usage}
    if message_id:
        fields["message_id"] = message_id
    return turn_line(
        content=list(blocks) or [{"type": "text", "text": "ok"}], model=MODEL, timestamp=_at(second), **fields
    )


def _result(second: float, use_id: str, text: str = "ok", **line) -> dict:
    return user_block_line([tool_result_block(use_id, text)], timestamp=_at(second), **line)


def _notification(second: float, task_id: str, report: str = "done") -> dict:
    text = f"<task-notification><task-id>{task_id}</task-id><status>completed</status><result>{report}</result></task-notification>"
    return user_str_line(text, origin={"kind": "task-notification"}, timestamp=_at(second))


def _read(use_id: str, name: str = "src/a.py") -> dict:
    return tool_use_block("Read", use_id, {"file_path": name})


def _corpus(top, *subs) -> NS:
    return NS(sessions=[NS(top=top, subs=list(subs), session_id="s1", project_dir="p")])


def _cost(turn, pricing) -> float:
    return price_turn(turn, pricing.resolve_model(MODEL)).total


def _read_cost(turn, pricing) -> float:
    return price_turn(turn, pricing.resolve_model(MODEL)).cache_read_cost


# -- single lookups, counted by message ----------------------------------------------------------


def _lookups_transcript(tmp_path):
    return _parse(tmp_path, "top.jsonl", [
        _human(0),
        _step(1, _read("r1")),
        _result(2, "r1"),
        _step(3, tool_use_block("Bash", "b1", {"command": "grep -n foo src/a.py"})),
        _result(4, "b1"),
        # One message written as two lines, as a reply with two calls is: not a single lookup.
        _step(5, _read("r2", "src/b.py"), message_id="m_two"),
        _step(5, _read("r3", "src/c.py"), message_id="m_two"),
        user_block_line([tool_result_block("r2", "x"), tool_result_block("r3", "x")], timestamp=_at(6)),
        _step(7, _read("r4", "src/d.py")),
        _result(8, "r4"),
        _step(9, tool_use_block("Edit", "e1", {"file_path": "src/a.py", "old_string": "a", "new_string": "b"})),
        _result(10, "e1"),
        _step(11),
    ], kind="top-level")


def test_single_lookups_are_counted_by_message_with_the_shell_ones_apart(tmp_path, pricing):
    top = _lookups_transcript(tmp_path)
    assert [t.shell_read_count for t in top.turns][1] > 0, "the fixture's grep must read as a shell read"
    h = habits.collect(_corpus(top), pricing)
    (cycle,) = h.cycles
    # Six messages; the Read, the grep and the last Read each made one read-only call and nothing else.
    assert cycle.calls == 6
    assert (cycle.probe_calls, cycle.probe_shell_calls) == (3, 1)
    # The first two are a run, the third a lookup on its own (the two-call message broke the run).
    assert cycle.probe_runs == 1
    # What the second one paid to read the cache is what one message holding both would have spared.
    assert cycle.probe_batch_cost == pytest.approx(_read_cost(top.turns[1], pricing))
    assert cycle.calls_before_edit == 4


def test_a_run_of_lookups_is_priced_from_its_second_reply(tmp_path, pricing):
    third = tool_use_block("Glob", "g1", {"pattern": "src/*.py"})
    top = _parse(tmp_path, "top.jsonl", [
        _human(0),
        _step(1, _read("r1")), _result(2, "r1"),
        _step(3, _read("r2", "src/b.py")), _result(4, "r2"),
        _step(5, third), _result(6, "g1"),
        _step(7),
    ], kind="top-level")
    (cycle,) = habits.collect(_corpus(top), pricing).cycles
    assert (cycle.probe_calls, cycle.probe_shell_calls, cycle.probe_runs) == (3, 0, 1)
    assert cycle.probe_batch_cost == pytest.approx(_read_cost(top.turns[1], pricing) + _read_cost(top.turns[2], pricing))


def test_an_agents_lookups_reach_its_row_and_the_probes_table(tmp_path, pricing):
    top = _parse(tmp_path, "top.jsonl", [
        _human(0),
        _step(1, tool_use_block("Agent", "toolu_A", {"prompt": "find it"})),
        _result(8, "toolu_A", "found it"),
        _step(9),
    ], kind="top-level")
    sub = _parse(tmp_path, "agent-a1.jsonl", [
        user_str_line("find it", timestamp=_at(2)),
        _step(3, _read("s1")), _result(4, "s1"),
        _step(5, tool_use_block("Bash", "s2", {"command": "cat src/a.py"})), _result(6, "s2"),
        _step(7),
    ], kind="subagent", agent_id="agent-a1", agent_type="Explore", tool_use_id="toolu_A")
    h = habits.collect(_corpus(top, sub), pricing)
    (agent,) = h.agents
    assert (agent.calls, agent.probe_calls, agent.probe_shell_calls, agent.probe_runs) == (3, 2, 1, 1)
    assert agent.probe_batch_cost == pytest.approx(_read_cost(sub.turns[1], pricing))
    section = habits.section_from(h)
    row = {r["agent_type"]: r for r in _rows(_table(section, "habits_agents"))}["Explore"]
    assert row["probe_pct"] == pytest.approx(100 * 2 / 3) and row["probe_shell_pct"] == pytest.approx(100 / 3)
    probes = {r["agent_type"]: r for r in _rows(_table(section, "habits_probes"))}
    assert probes["Explore"]["probes"] == 2 and probes["Explore"]["shell"] == 1
    assert probes["Explore"]["runs"] == 1 and probes["Explore"]["batch_cost"] == pytest.approx(agent.probe_batch_cost)


def test_the_probes_table_leaves_out_where_nothing_looked_once_and_ranks_by_what_a_batch_spares():
    h = habits.Habits()
    h.agents = [
        habits.AgentFact(session_id="s1", agent_type="quiet", week="2026-08-03", cost=1.0, calls=5),
        habits.AgentFact(session_id="s1", agent_type="small", week="2026-08-03", cost=1.0, calls=4, probe_calls=2,
                         probe_runs=1, probe_batch_cost=0.1),
        habits.AgentFact(session_id="s1", agent_type="big", week="2026-08-03", cost=1.0, calls=9, probe_calls=6,
                         probe_shell_calls=2, probe_runs=2, probe_batch_cost=0.9),
    ]
    rows = _rows(_table(habits.section_from(h), "habits_probes"))
    assert [r["agent_type"] for r in rows] == ["big", "small"]


def _looker(reads: int, calls: int, probes: int, **kw) -> habits.CycleFact:
    return habits.CycleFact(session_id="s", ts=None, week="2026-08-03", cost=1.0, turns=calls, reads=reads,
                            read_tokens=4_000, read_carry=reads * 0.5, calls=calls, probe_calls=probes, **kw)


def test_the_lookups_feed_the_name_files_and_explore_research_evidence():
    h = habits.Habits()
    h.cycles = [_looker(2, 4, 0, flags=("path",)) for _ in range(habits.MIN_GROUP)]
    h.cycles += [_looker(8, 4, 3) for _ in range(habits.MIN_GROUP)]
    item = habits._item_name_files(h)
    assert item is not None
    assert "75% of the replies to those that didn't made one lookup and nothing else, against 0%." in item.evidence
    heavy = habits._item_explore_research(h)
    assert heavy is not None
    assert f"{3 * habits.MIN_GROUP} of their {4 * habits.MIN_GROUP} replies made one lookup and nothing else" in heavy.evidence
    # With no lone lookups the evidence has no such clause.
    h.cycles = [_looker(2, 4, 0, flags=("path",)) for _ in range(habits.MIN_GROUP)] + [
        _looker(8, 4, 0) for _ in range(habits.MIN_GROUP)
    ]
    assert "one lookup" not in habits._item_name_files(h).evidence
    assert "one lookup" not in habits._item_explore_research(h).evidence


# -- replies to an agent's report ---------------------------------------------------------------


def _report_session(tmp_path):
    """Four reports that came to the main session. The first reply only
    acknowledges its report, the second edits a file, the third starts an
    agent again, and the fourth answers a report that woke the session after an
    hour and so wrote 120k tokens to the cache again. Returns the main session
    and the four agents' transcripts."""
    subs = [
        _parse(tmp_path, f"agent-{name}.jsonl", [user_str_line("dig", timestamp=_at(second)), _step(second + 1)],
               kind="subagent", agent_id=f"agent-{name}", agent_type="Explore", tool_use_id=f"toolu_{name}")
        for name, second in (("a1", 50), ("a2", 150), ("a3", 250), ("a4", 350))
    ]
    return _parse(tmp_path, "top.jsonl", [
        _human(0),
        _step(1),
        _notification(100, "a1"), _step(101),
        _notification(200, "a2"),
        _step(201, tool_use_block("Edit", "e1", {"file_path": "src/a.py", "old_string": "a", "new_string": "b"})),
        _result(202, "e1"), _step(203),
        _notification(300, "a3"),
        _step(301, tool_use_block("Agent", "toolu_N", {"prompt": "again", "run_in_background": True})),
        _result(302, "toolu_N", "launched"), _step(303),
        _notification(4200, "a4", "late"),
        _step(4201, cache_creation_input_tokens=120_000),
    ], kind="top-level"), subs


def test_a_reply_to_a_report_acknowledged_it_or_acted_on_it_or_started_more_agents(tmp_path, pricing):
    top, subs = _report_session(tmp_path)
    h = habits.collect(_corpus(top, *subs), pricing)
    assert [r.kind for r in h.report_turns] == ["acknowledged", "acted", "respawned", "acknowledged"]
    # What a reply cost takes in the calls it went on to make.
    turns = capture_mod._priced(top)
    cost = lambda *at: sum(_cost(turns[i], pricing) for i in at)  # noqa: E731
    assert [round(r.cost, 9) for r in h.report_turns] == [
        round(cost(1), 9), round(cost(2, 3), 9), round(cost(4, 5), 9), round(cost(6), 9)
    ]


def test_a_report_after_an_hour_is_a_wake_up_with_what_it_wrote_again(tmp_path, pricing):
    top, subs = _report_session(tmp_path)
    h = habits.collect(_corpus(top, *subs), pricing)
    assert [r.woke for r in h.report_turns] == [False, False, False, True]
    assert [r.rewritten for r in h.report_turns] == [0, 0, 0, 120_000]
    rows = {r["kind"]: r for r in _rows(_table(habits.section_from(h), "habits_report_turns"))}
    assert rows["acknowledged"]["replies"] == 2 and rows["acknowledged"]["share_pct"] == 50.0
    assert (rows["acknowledged"]["woke"], rows["acknowledged"]["woke_tokens"]) == (1, 120_000)
    assert (rows["acted"]["woke"], rows["respawned"]["woke"]) == (0, 0)


def test_the_wake_up_is_the_tuning_exports_definition(tmp_path, pricing):
    from claudeglass import tuning

    assert tuning.WAKE_GAP_S == habits.WAKE_GAP_S == 3_600
    top, _subs = _report_session(tmp_path)
    turns = capture_mod._priced(top)
    assert [habits.is_wake_up(t, gap) for t, gap in ((turns[6], 3_898.0), (turns[6], 3_599.0), (turns[2], 4_000.0))] == [
        True, False, True,
    ]
    # A message of yours after a long break is a return, not a wake-up.
    assert not habits.is_wake_up(turns[0], 9_999.0)


def test_no_report_turn_without_a_report():
    h = habits.collect(NS(sessions=[]), None)
    assert h.report_turns == []
    assert _rows(_table(habits.section_from(h), "habits_report_turns")) == []


# -- an async agent whose report arrives as a task notification ------------------------------------


def test_a_background_command_finishing_is_not_a_report_but_a_workflows_report_is(tmp_path, pricing):
    top = _parse(tmp_path, "top.jsonl", [
        _human(0),
        *_workflow_launch(1, "toolu_W", task="t_w"),
        _step(3),
        _notification(100, "b7si0b9jf"), _step(101),
        _notification(200, "t_w"), _step(201),
    ], kind="top-level")
    h = habits.collect(_corpus(top), pricing)
    assert [r.kind for r in h.report_turns] == ["acknowledged"]


def _async_session(tmp_path, report: str):
    top = _parse(tmp_path, "top.jsonl", [
        _human(0),
        _step(1, tool_use_block("Agent", "toolu_A", {"prompt": "dig", "run_in_background": True})),
        _result(2, "toolu_A", "Async agent launched successfully.",
                toolUseResult={"isAsync": True, "status": "async_launched", "agentId": "a1"}),
        _step(3),
        _human(20, "something else"), _step(21),
        _notification(60, "a1", report),
        _step(61),
        _human(70, "and then"), _step(71),
    ], kind="top-level")
    sub = _parse(tmp_path, "agent-a1.jsonl", [
        user_str_line("dig", timestamp=_at(5)),
        _step(6),
    ], kind="subagent", agent_id="agent-a1", agent_type="Explore", tool_use_id="toolu_A")
    return top, sub


def test_a_background_agents_queued_report_is_sized_and_priced_from_the_reply_that_read_it(tmp_path, pricing):
    """A report that arrives while the main session is mid-reply is queued:
    a queued_command attachment, read by the next reply."""
    report = (
        "<task-notification><task-id>a1</task-id><status>completed</status><result>"
        + "r" * 4_000
        + "</result></task-notification>"
    )
    queued = attachment_line("queued_command", rendered=report, prompt=report, commandMode="task-notification")
    queued["timestamp"] = _at(60)
    top = _parse(tmp_path, "top.jsonl", [
        _human(0),
        _step(1, tool_use_block("Agent", "toolu_A", {"prompt": "dig", "run_in_background": True})),
        _result(2, "toolu_A", "Async agent launched successfully.",
                toolUseResult={"isAsync": True, "status": "async_launched", "agentId": "a1"}),
        _step(3), _human(20, "something else"),
        _step(50, _read("r9")), _result(59, "r9"),
        queued,
        _step(61), _step(62),
    ], kind="top-level")
    sub = _parse(tmp_path, "agent-a1.jsonl", [user_str_line("dig", timestamp=_at(5)), _step(6)],
                 kind="subagent", agent_id="agent-a1", agent_type="Explore", tool_use_id="toolu_A")
    (agent,) = habits.collect(_corpus(top, sub), pricing).agents
    assert agent.report_tokens == len(report) // capture_mod.CHARS_PER_TOKEN
    assert agent.report_carry > 0


def test_a_background_agents_report_is_sized_and_priced_from_the_reply_that_read_it(tmp_path, pricing):
    report = "r" * 4_000
    top, sub = _async_session(tmp_path, report)
    h = habits.collect(_corpus(top, sub), pricing)
    (agent,) = h.agents
    # The notification carries the report, so the agent has a report size at all.
    event = next(e for e in top.events if e.kind == EventKind.TASK_NOTIFICATION)
    assert agent.report_tokens == event.size_chars // capture_mod.CHARS_PER_TOKEN
    assert agent.report_tokens > 1_000
    # Carried from the reply after the notification (the fourth), not the one that launched it (the first).
    turns = capture_mod._priced(top)
    arrival = next(i for i, t in enumerate(turns) if EventKind.TASK_NOTIFICATION in t.preceding_event_kinds)
    assert arrival == 3
    carry = habits._CarryCost(turns, habits._Rates(pricing))
    assert agent.report_carry == pytest.approx(carry.cost_from(arrival, agent.report_tokens))
    assert agent.report_carry > 0
    from_the_launch = carry.cost(0, agent.report_tokens)
    assert agent.report_carry < from_the_launch


def test_a_report_with_no_reply_to_read_it_carries_nothing(tmp_path, pricing):
    top = _parse(tmp_path, "top.jsonl", [
        _human(0),
        _step(1, tool_use_block("Agent", "toolu_A", {"prompt": "dig", "run_in_background": True})),
        _result(2, "toolu_A", "Async agent launched successfully.",
                toolUseResult={"isAsync": True, "status": "async_launched", "agentId": "a1"}),
        _step(3),
        _notification(60, "a1", "r" * 400),
    ], kind="top-level")
    sub = _parse(tmp_path, "agent-a1.jsonl", [user_str_line("dig", timestamp=_at(5)), _step(6)],
                 kind="subagent", agent_id="agent-a1", agent_type="Explore", tool_use_id="toolu_A")
    (agent,) = habits.collect(_corpus(top, sub), pricing).agents
    assert agent.report_tokens > 100 and agent.report_carry == 0.0


def test_a_synchronous_report_is_still_priced_from_the_call_that_returned_it(tmp_path, pricing):
    top = _parse(tmp_path, "top.jsonl", [
        _human(0),
        _step(1, tool_use_block("Agent", "toolu_A", {"prompt": "dig"})),
        _result(8, "toolu_A", "x" * 2_000),
        _step(9), _human(20), _step(21),
    ], kind="top-level")
    sub = _parse(tmp_path, "agent-a1.jsonl", [user_str_line("dig", timestamp=_at(2)), _step(3)],
                 kind="subagent", agent_id="agent-a1", agent_type="Explore", tool_use_id="toolu_A")
    (agent,) = habits.collect(_corpus(top, sub), pricing).agents
    carry = habits._CarryCost(capture_mod._priced(top), habits._Rates(pricing))
    assert agent.report_tokens == 500
    assert agent.report_carry == pytest.approx(carry.cost(0, 500))
    assert agent.report_carry > 0


def test_a_workflow_agent_has_no_report_and_the_sizes_are_shared_with_topology(tmp_path):
    top, sub = _async_session(tmp_path, "r" * 400)
    index = _report_index(top, [sub])
    assert index.chars_for(sub) == 400 + len("<task-notification><task-id>a1</task-id><status>completed</status>"
                                             "<result></result></task-notification>")
    # The same agent id, made by a workflow, hands its report to the script.
    workflow = _parse(tmp_path, "agent-a1.jsonl", [user_str_line("dig", timestamp=_at(5)), _step(6)],
                      kind="workflow-agent", agent_id="agent-a1", agent_type="workflow-subagent")
    assert index.chars_for(workflow) is None


# -- run receipts: what each agent run did ------------------------------------------------------------


def _wf_agent(tmp_path, name: str, second: float, *, run: str = "wf_a", reads: str | None = "docs/plan.md",
              compact: bool = False, size: int = 4_000):
    """One workflow agent: it reads ``reads`` (when given) in its first reply, then answers.
    ``compact`` puts a compaction in the middle of the run."""
    from helpers import system_line

    lines = [user_str_line("do it", timestamp=_at(second))]
    lines += [_step(second + 1, _read(f"{name}-r", reads)), _result(second + 2, f"{name}-r", "x" * size)] if reads else [
        _step(second + 1)
    ]
    if compact:
        lines.append(system_line("compact_boundary", timestamp=_at(second + 3), compactMetadata={
            "trigger": "auto", "preTokens": 150_000, "postTokens": 20_000, "durationMs": 900}))
    lines.append(_step(second + 4))
    return _parse(tmp_path, f"{name}.jsonl", lines, kind="workflow-agent", agent_id=f"agent-{name}",
                  agent_type="workflow-subagent", workflow_run_id=run)


def _workflow_launch(second: float, use_id: str, run: str = "wf_a", task: str = "t_a") -> list[dict]:
    return [
        _step(second, tool_use_block("Workflow", use_id, {"script": "return 1"})),
        _result(second + 1, use_id, "Workflow launched in background.",
                toolUseResult={"status": "async_launched", "taskType": "local_workflow", "runId": run, "taskId": task}),
    ]


def _receipt_rows(h) -> dict:
    return {r["launch"]: r for r in _rows(_table(habits.section_from(h), "habits_agent_runs"))}


def test_a_resumed_workflow_run_is_split_between_its_launches_by_each_agents_own_start(tmp_path, pricing):
    """A resumed run keeps its run directory, so four agents of one directory belong to two launches
    and only the agents of one launch are siblings."""
    top = _parse(tmp_path, "top.jsonl", [
        _human(0), *_workflow_launch(10, "tu_1"), _step(12),
        _human(90), *_workflow_launch(100, "tu_2", task="t_b"), _step(102),
    ], kind="top-level")
    agents = [
        _wf_agent(tmp_path, "w1", 20), _wf_agent(tmp_path, "w2", 22),
        _wf_agent(tmp_path, "w3", 110), _wf_agent(tmp_path, "w4", 112),
    ]
    h = habits.collect(_corpus(top, *agents), pricing)
    assert len({a.run for a in h.agents}) == 2 and len({a.group for a in h.agents}) == 2
    row = _receipt_rows(h)["workflow"]
    assert (row["runs"], row["agents"]) == (2, 4)
    # One re-read in each launch (the second agent of each), not three over the whole directory.
    assert row["shared_reads"] == 2
    tokens = 4_000 // capture_mod.CHARS_PER_TOKEN
    assert row["shared_tokens"] == 2 * tokens and row["shared_cost"] > 0


def test_siblings_of_one_launch_that_read_the_same_file_charge_every_reader_after_the_first(tmp_path, pricing):
    top = _parse(tmp_path, "top.jsonl", [_human(0), *_workflow_launch(10, "tu_1"), _step(12)], kind="top-level")
    # Out of order on purpose: the first reader is found by time, not by file order.
    agents = [_wf_agent(tmp_path, "w3", 40), _wf_agent(tmp_path, "w1", 20), _wf_agent(tmp_path, "w2", 30)]
    h = habits.collect(_corpus(top, *agents), pricing)
    assert [a.shared_reads for a in h.agents] == [1, 0, 1]
    assert _receipt_rows(h)["workflow"]["shared_reads"] == 2


def test_a_file_only_one_agent_read_and_an_unread_one_are_not_shared(tmp_path, pricing):
    top = _parse(tmp_path, "top.jsonl", [_human(0), *_workflow_launch(10, "tu_1"), _step(12)], kind="top-level")
    agents = [
        _wf_agent(tmp_path, "w1", 20, reads="docs/plan.md"),
        _wf_agent(tmp_path, "w2", 30, reads="docs/other.md"),
        _wf_agent(tmp_path, "w3", 40, reads=None),
    ]
    h = habits.collect(_corpus(top, *agents), pricing)
    assert _receipt_rows(h)["workflow"]["shared_reads"] == 0
    # Only the file most siblings read counts, once for each group.
    more = [_wf_agent(tmp_path, f"x{n}", 50 + 10 * n, reads="docs/plan.md") for n in range(3)]
    more.append(_wf_agent(tmp_path, "y1", 90, reads="docs/other.md"))
    h = habits.collect(_corpus(top, *agents, *more), pricing)
    assert _receipt_rows(h)["workflow"]["shared_reads"] == 3


def test_a_workflow_agent_that_summarised_is_counted_in_the_run_receipts(tmp_path, pricing):
    top = _parse(tmp_path, "top.jsonl", [_human(0), *_workflow_launch(10, "tu_1"), _step(12)], kind="top-level")
    agents = [_wf_agent(tmp_path, "w1", 20, compact=True), _wf_agent(tmp_path, "w2", 30)]
    h = habits.collect(_corpus(top, *agents), pricing)
    row = _receipt_rows(h)["workflow"]
    assert (row["compactions"], row["compacted_agents"]) == (1, 1)
    first, second = h.agents
    assert (first.compactions, first.auto_compactions, second.compactions) == (1, 1, 0)


def test_direct_agents_are_runs_of_their_own_with_their_starting_context_times_replies(tmp_path, pricing):
    top = _parse(tmp_path, "top.jsonl", [
        _human(0),
        _step(1, tool_use_block("Agent", "toolu_A", {"prompt": "a"}), tool_use_block("Agent", "toolu_B", {"prompt": "b"})),
        _result(8, "toolu_A", "ok"), _result(8, "toolu_B", "ok"), _step(9),
    ], kind="top-level")

    def agent(name, use_id, second):
        return _parse(tmp_path, f"agent-{name}.jsonl", [
            user_str_line("go", timestamp=_at(second)),
            _step(second + 1, _read(f"{name}r", "docs/plan.md")), _result(second + 2, f"{name}r", "x" * 4_000),
            _step(second + 3, tool_use_block("Bash", f"{name}b", {"command": "grep -n x docs/plan.md"})),
            _result(second + 4, f"{name}b"),
            _step(second + 5),
        ], kind="subagent", agent_id=f"agent-{name}", agent_type="Explore", tool_use_id=use_id)

    h = habits.collect(_corpus(top, agent("a1", "toolu_A", 2), agent("a2", "toolu_B", 3)), pricing)
    row = _receipt_rows(h)["foreground"]
    assert (row["runs"], row["agents"], row["calls"]) == (2, 2, 6)
    # Both made two single read-only calls in three replies.
    assert row["probe_pct"] == pytest.approx(100 * 4 / 6)
    # Each started at the first call's context (40k read, 2k written and a little input) and made three replies.
    start = h.agents[0].start_tokens
    assert 42_000 <= start < 43_000 and row["start_reads"] == 2 * start * 3
    # Started by the same reply, so they are siblings: the second read the plan again.
    assert row["shared_reads"] == 1 and row["compactions"] == 0
    assert "background" not in _receipt_rows(h)


def test_a_background_agent_is_its_own_row_and_the_table_is_empty_without_agents(tmp_path, pricing):
    top = _parse(tmp_path, "top.jsonl", [
        _human(0),
        _step(1, tool_use_block("Agent", "toolu_A", {"prompt": "a", "run_in_background": True})),
        _result(2, "toolu_A", "Async agent launched successfully.", toolUseResult={"isAsync": True, "agentId": "a1"}),
        _step(3),
    ], kind="top-level")
    sub = _parse(tmp_path, "agent-a1.jsonl", [user_str_line("a", timestamp=_at(5)), _step(6)],
                 kind="subagent", agent_id="agent-a1", agent_type="Explore", tool_use_id="toolu_A")
    rows = _receipt_rows(habits.collect(_corpus(top, sub), pricing))
    assert list(rows) == ["background"] and rows["background"]["runs"] == 1
    assert _rows(_table(habits.section_from(habits.Habits()), "habits_agent_runs")) == []


def test_the_run_receipts_keep_only_counts_and_opaque_keys(tmp_path, pricing):
    top = _parse(tmp_path, "top.jsonl", [_human(0), *_workflow_launch(10, "tu_1"), _step(12)], kind="top-level")
    h = habits.collect(_corpus(top, _wf_agent(tmp_path, "w1", 20), _wf_agent(tmp_path, "w2", 30)), pricing)
    table = _table(habits.section_from(h), "habits_agent_runs")
    cells = [cell for row in table.rows for cell in row]
    assert all(isinstance(cell, (int, float)) or cell in ("background", "foreground", "workflow") for cell in cells)
    assert not any("plan.md" in str(cell) or "wf_a" in str(cell) for cell in cells)

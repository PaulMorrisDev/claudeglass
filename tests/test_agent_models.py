"""Tests for ``src/claudeglass/agent_models.py``: agents that ran on a larger
model than their work needed.

The whole-corpus cases run on ``tests/fixtures/agent_models/runs.json``, the
derived fields of 82 real agent runs with synthetic ids and times (no label,
description, phase, prompt or path). ``_result_from_agent`` turns each into a
``TranscriptResult`` the same way: a group of N turns becomes N ``Turn``
objects, the first carrying the group's tokens and the rest none, so it
prices exactly like the real turns did. The single-case tests use small
hand-built results at the packaged rate card (``load_pricing()``), never a
fake one.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from claudeglass import agent_models, ignores, model_swap, recommend
from claudeglass.agent_models import AgentModelThresholds, compute_agent_models
from claudeglass.model import ReportModel, Section, TranscriptMeta, TranscriptResult, Turn
from claudeglass.pricing import load_pricing

PRICING = load_pricing()

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "agent_models" / "runs.json"

OPUS = "claude-opus-5-5"
SONNET = "claude-sonnet-5-5"
HAIKU = "claude-haiku-4-5-20251001"
FABLE = "claude-fable-5-1"

#: The columns, in order (the contract the rules and the card copy rely on).
COLUMNS = [
    "case",
    "agent_type",
    "verdict",
    "runs",
    "roles",
    "model",
    "cost_usd",
    "cost_on_sonnet_usd",
    "saving_usd",
    "saving_pct",
    "write_turns",
    "workflow_runs",
    "first_seen",
    "last_seen",
    "later_compliant",
    "env_var_set",
]

EVIDENCE_LABELS = [
    "Agents",
    "Roles",
    "Model",
    "Cost (USD)",
    "Cost on Sonnet (USD)",
    "Ceiling saving (USD)",
    "Ceiling saving (%)",
    "Edit turns",
    "Workflow runs",
    "First seen",
    "Last seen",
    "Later compliant writers",
    "CLAUDE_CODE_SUBAGENT_MODEL set",
]


# -- the synthetic fixture -----------------------------------------------------


def _result_from_agent(agent: dict, group_fields: list[str]) -> TranscriptResult:
    """One fixture agent as a ``TranscriptResult``: ``turns`` Turn objects
    per group (turn_index counting up from 1, ts one second apart from the
    agent's ``first_ts``), the first of each group carrying all its tokens;
    the first ``real_edit_turns`` turns edit, the next ``shell_write_turns``
    write through the shell."""
    meta = TranscriptMeta(
        path=f"synthetic/{agent['agent_id']}.jsonl",
        kind=agent["kind"],
        session_id=agent["session_id"],
        agent_id=agent["agent_id"],
        agent_type=agent["agent_type"],
        agent_model_alias=agent["agent_model_alias"],
        workflow_run_id=agent["workflow_run_id"],
        role_word=agent["role_word"],
        model_recorded=agent["model_recorded"],
    )
    start = datetime.fromisoformat(agent["first_ts"].replace("Z", "+00:00"))
    turns: list[Turn] = []
    for group in agent["groups"]:
        fields = dict(zip(group_fields, group))
        for i in range(fields["turns"]):
            first = i == 0
            turns.append(
                Turn(
                    turn_index=len(turns) + 1,
                    ts=(start + timedelta(seconds=len(turns))).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                    model=fields["model"],
                    speed=fields["speed"],
                    inference_geo=fields["inference_geo"],
                    input_tokens=fields["input_tokens"] if first else 0,
                    output_tokens=fields["output_tokens"] if first else 0,
                    cache_read_tokens=fields["cache_read_tokens"] if first else 0,
                    cache_creation_tokens=fields["cache_creation_tokens"] if first else 0,
                    cc_5m=fields["cc_5m"] if first else 0,
                    cc_1h=fields["cc_1h"] if first else 0,
                )
            )
    edits = agent["real_edit_turns"]
    for turn in turns[:edits]:
        turn.edit_kind = "real"
    for turn in turns[edits : edits + agent["shell_write_turns"]]:
        turn.shell_write_count = 1
    return TranscriptResult(meta=meta, turns=turns)


@pytest.fixture(scope="module")
def fixture_results() -> list[TranscriptResult]:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return [_result_from_agent(agent, data["group_fields"]) for agent in data["agents"]]


@pytest.fixture(scope="module")
def fixture_stats(fixture_results):
    return compute_agent_models(fixture_results, PRICING)


def _rows(table) -> dict[tuple[str, str], dict]:
    """(agent type, verdict) -> the row as {column key: cell}."""
    keys = [column.key for column in table.columns]
    cells = [dict(zip(keys, row)) for row in table.rows]
    return {(row["agent_type"], row["verdict"]): row for row in cells}


def test_fixture_flags_exactly_three_groups(fixture_stats):
    table = agent_models.build_table(fixture_stats)
    assert [column.key for column in table.columns] == COLUMNS
    assert table.name == "model_swap_agent_models"
    rows = _rows(table)
    assert set(rows) == {
        ("workflow-subagent", "inherited"),
        ("general-purpose", "inherited"),
        ("general-purpose", "decide-apply"),
    }
    # Verdicts in order, then the biggest saving first. The row key is the
    # type and the verdict: general-purpose has a row for two verdicts.
    assert [row[0] for row in table.rows] == [
        "general-purpose:inherited",
        "workflow-subagent:inherited",
        "general-purpose:decide-apply",
    ]
    assert table.value_labels == {
        "general-purpose:inherited": "general-purpose agents, no model set",
        "workflow-subagent:inherited": "Workflow agents, no model set",
        "general-purpose:decide-apply": "general-purpose agents, decided and changed code",
    }


def test_fixture_workflow_row_is_the_four_implementers_and_a_fixer(fixture_stats):
    row = _rows(agent_models.build_table(fixture_stats))[("workflow-subagent", "inherited")]
    assert row["runs"] == 5
    assert row["roles"] == "implement 4, fix 1"
    assert row["model"] == OPUS
    assert round(row["cost_usd"], 2) == 15.43
    assert round(row["cost_on_sonnet_usd"], 2) == 12.52
    assert round(row["saving_usd"], 2) == 2.91
    assert row["saving_pct"] == pytest.approx(100 * row["saving_usd"] / row["cost_usd"])
    assert row["workflow_runs"] == 1
    assert row["later_compliant"] == 25
    assert row["env_var_set"] == "no"


def test_fixture_general_purpose_writers_are_inherited(fixture_stats):
    row = _rows(agent_models.build_table(fixture_stats))[("general-purpose", "inherited")]
    assert row["runs"] == 3
    assert row["roles"] == "fix 2, other 1"
    assert round(row["cost_usd"], 2) == 13.31
    assert round(row["cost_on_sonnet_usd"], 2) == 9.68
    assert round(row["saving_usd"], 2) == 3.63
    assert row["workflow_runs"] == 0
    assert row["later_compliant"] == 0


def test_fixture_deciders_that_edited_get_cost_but_no_saving(fixture_stats):
    row = _rows(agent_models.build_table(fixture_stats))[("general-purpose", "decide-apply")]
    assert row["runs"] == 4
    assert row["roles"] == "audit 4"
    assert round(row["cost_usd"], 2) == 24.24
    assert row["cost_on_sonnet_usd"] is None
    assert row["saving_usd"] is None
    assert row["saving_pct"] is None
    assert row["write_turns"] >= 4 * AgentModelThresholds().decide_apply_min_edit_turns
    assert row["later_compliant"] == 0


def test_fixture_table_holds_no_paths_and_every_row_key_is_a_string(fixture_stats):
    table = agent_models.build_table(fixture_stats)
    assert table.rows
    for row in table.rows:
        assert isinstance(row[0], str) and row[0]
        for cell in row:
            if isinstance(cell, str):
                assert "/" not in cell and "\\" not in cell, cell
    for note in table.notes:
        assert "/" not in note and "\\" not in note


def test_fixture_old_unset_deciders_and_later_runs_flag_nothing(fixture_results):
    """The Plan deciders ran with no model and edited nothing; the later
    runs all set a model: neither adds a row."""
    only = [
        r
        for r in fixture_results
        if r.meta.workflow_run_id == "wf_test_unset_deciders" or (r.meta.workflow_run_id or "").startswith("wf_test_later_")
    ]
    assert only
    assert compute_agent_models(only, PRICING).groups == {}


# -- hand-built runs -----------------------------------------------------------

_TOKENS = dict(input_tokens=1_000, output_tokens=2_000, cache_read_tokens=100_000, cache_creation_tokens=10_000, cc_5m=10_000)


def _agent(
    *,
    kind: str = "subagent",
    agent_type: str | None = "general-purpose",
    role_word: str | None = None,
    model: str = OPUS,
    turns: int = 3,
    real_edits: int = 0,
    shell_writes: int = 0,
    alias: str | None = None,
    recorded: bool = True,
    first_ts: str = "2026-09-23T12:00:00.000Z",
    workflow_run_id: str | None = None,
) -> TranscriptResult:
    start = datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
    items = [
        Turn(
            turn_index=i + 1,
            ts=(start + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            model=model,
            edit_kind="real" if i < real_edits else None,
            shell_write_count=1 if real_edits <= i < real_edits + shell_writes else 0,
            **_TOKENS,
        )
        for i in range(turns)
    ]
    meta = TranscriptMeta(
        path="synthetic.jsonl",
        kind=kind,
        session_id="sess-synthetic",
        agent_type=agent_type,
        agent_model_alias=alias,
        role_word=role_word,
        model_recorded=recorded,
        workflow_run_id=workflow_run_id,
    )
    return TranscriptResult(meta=meta, turns=items)


def _workflow(**kw) -> TranscriptResult:
    kw.setdefault("kind", "workflow-agent")
    kw.setdefault("agent_type", "workflow-subagent")
    kw.setdefault("workflow_run_id", "wf_test_hand")
    return _agent(**kw)


def _verdicts(results, **kw) -> dict[tuple[str, str], dict]:
    return _rows(agent_models.build_table(compute_agent_models(results, PRICING, **kw)))


def _cost(model_id: str, **fields) -> float:
    """Hand-computed cost of one turn at ``model_id``'s packaged rates: an
    independent restatement of ``pricing.price_turn``, not a call into it."""
    rates = PRICING.models[model_id]
    return (
        fields.get("input_tokens", 0) / 1_000_000 * rates.input
        + fields.get("output_tokens", 0) / 1_000_000 * rates.output
        + fields.get("cc_5m", 0) / 1_000_000 * rates.cache_write_5m
        + fields.get("cache_read_tokens", 0) / 1_000_000 * rates.cache_read
    )


def test_saving_is_the_same_tokens_at_sonnets_list_rate():
    rows = _verdicts([_workflow(role_word="implement", turns=3, real_edits=1)])
    row = rows[("workflow-subagent", "inherited")]
    sonnet = PRICING.aliases["sonnet"]
    assert row["cost_usd"] == pytest.approx(3 * _cost(OPUS, **_TOKENS))
    assert row["cost_on_sonnet_usd"] == pytest.approx(3 * _cost(sonnet, **_TOKENS))
    assert row["saving_usd"] == pytest.approx(row["cost_usd"] - row["cost_on_sonnet_usd"])
    assert row["saving_pct"] == pytest.approx(100 * row["saving_usd"] / row["cost_usd"])
    assert row["write_turns"] == 1
    assert row["first_seen"] == row["last_seen"] == "2026-09-23"


def test_a_mixed_model_run_is_priced_turn_by_turn_on_the_dominant_model_tier():
    run = _workflow(role_word="fix", turns=3, real_edits=1)
    run.turns[2].model = SONNET
    row = _verdicts([run])[("workflow-subagent", "inherited")]
    assert row["model"] == OPUS
    assert row["cost_usd"] == pytest.approx(2 * _cost(OPUS, **_TOKENS) + _cost(SONNET, **_TOKENS))


def test_a_reply_on_a_model_the_rate_card_cannot_price_counts_as_zero_and_is_counted():
    run = _workflow(role_word="fix", turns=3, real_edits=1)
    run.turns[2].model = "mystery-model-9"
    stats = compute_agent_models([run], PRICING)
    assert stats.unpriced_turns == 1
    row = _rows(agent_models.build_table(stats))[("workflow-subagent", "inherited")]
    assert row["cost_usd"] == pytest.approx(2 * _cost(OPUS, **_TOKENS))
    assert row["cost_on_sonnet_usd"] == pytest.approx(2 * _cost(PRICING.aliases["sonnet"], **_TOKENS))
    assert any("no price" in note for note in agent_models.build_table(stats).notes)


def test_without_a_sonnet_rate_the_row_has_no_saving():
    aliases = {alias: model for alias, model in PRICING.aliases.items() if alias != "sonnet"}
    pricing = dataclasses.replace(PRICING, aliases=aliases)
    stats = compute_agent_models([_workflow(role_word="fix", real_edits=1)], pricing)
    row = _rows(agent_models.build_table(stats))[("workflow-subagent", "inherited")]
    assert row["cost_usd"] > 0
    assert row["cost_on_sonnet_usd"] is None
    assert row["saving_usd"] is None
    assert row["saving_pct"] is None


def test_a_writer_on_opus_with_no_model_set_is_inherited():
    rows = _verdicts([_agent(role_word="fix", real_edits=1)])
    assert set(rows) == {("general-purpose", "inherited")}


def test_a_run_whose_agent_file_names_the_same_family_is_file_chosen():
    """The existing model-tier card owns it: no row."""
    run = _agent(role_word="fix", real_edits=1)
    assert _verdicts([run], agent_files={"general-purpose": "opus"}) == {}
    assert _verdicts([run], agent_files={"general-purpose": "opus[1m]"}) == {}
    assert _verdicts([run], agent_files={"general-purpose": "inherit"}) == {}


def test_a_file_naming_another_family_does_not_excuse_the_run():
    run = _agent(role_word="fix", real_edits=1)
    assert set(_verdicts([run], agent_files={"general-purpose": "sonnet"})) == {("general-purpose", "inherited")}
    assert set(_verdicts([run], agent_files={"general-purpose": None})) == {("general-purpose", "inherited")}
    assert set(_verdicts([run], agent_files={"other-type": "opus"})) == {("general-purpose", "inherited")}


def test_a_plain_workflow_agent_has_no_agent_file():
    run = _workflow(role_word="implement", real_edits=1)
    assert set(_verdicts([run], agent_files={"workflow-subagent": "opus"})) == {("workflow-subagent", "inherited")}


def test_a_workflow_agent_with_an_agent_type_follows_its_file_but_files_under_the_workflow_group():
    """A file-chosen workflow run gets no row, and model-tier never prices
    workflow runs: the file chose the model on purpose."""
    run = _workflow(agent_type="claude-implementer", role_word="implement", real_edits=1)
    assert _verdicts([run], agent_files={"claude-implementer": "opus"}) == {}
    assert set(_verdicts([run])) == {("workflow-subagent", "inherited")}


def test_a_workflow_agent_counts_as_a_workflow_run_by_its_kind_even_when_its_run_is_unnamed():
    """The run id says which run; the kind says it came from one at all."""
    unnamed = _workflow(role_word="implement", real_edits=1, workflow_run_id=None)
    assert unnamed.meta.workflow_run_id is None
    assert _verdicts([unnamed])[("workflow-subagent", "inherited")]["workflow_runs"] == 1
    named = [_workflow(role_word="implement", real_edits=1, workflow_run_id=f"wf_{n}") for n in range(2)]
    assert _verdicts(named)[("workflow-subagent", "inherited")]["workflow_runs"] == 2
    # A direct agent never counts, whatever its agent type is called.
    direct = _agent(agent_type="general-purpose", role_word="implement", real_edits=1)
    assert _verdicts([direct])[("general-purpose", "inherited")]["workflow_runs"] == 0


def test_a_model_named_in_the_call_is_asked_not_inherited():
    run = _agent(agent_type="claude-implementer", role_word="implement", real_edits=2, alias="opus")
    rows = _verdicts([run])
    assert set(rows) == {("claude-implementer", "asked")}
    row = rows[("claude-implementer", "asked")]
    assert row["saving_usd"] > 0
    assert row["later_compliant"] == 0


def test_a_call_that_named_opus_beats_an_agent_file_that_names_it_too():
    run = _agent(role_word="fix", real_edits=1, alias="opus")
    assert set(_verdicts([run], agent_files={"general-purpose": "opus"})) == {("general-purpose", "asked")}


def test_an_older_meta_that_cannot_say_whether_a_model_was_set_is_skipped():
    assert _verdicts([_agent(role_word="fix", real_edits=2, recorded=False)]) == {}


def test_forks_untyped_subagents_and_direct_runs_are_never_judged():
    assert _verdicts([_agent(agent_type="fork", role_word="fix", real_edits=2)]) == {}
    assert _verdicts([_agent(agent_type="unknown", role_word="fix", real_edits=2)]) == {}
    assert _verdicts([_agent(agent_type=None, role_word="fix", real_edits=2)]) == {}
    assert _verdicts([_agent(kind="top-level", agent_type=None, role_word="fix", real_edits=2)]) == {}
    assert _verdicts([_workflow(agent_type="fork", role_word="fix", real_edits=2)]) == {}


def test_a_workflow_agent_with_no_agent_type_files_under_the_workflow_group():
    rows = _verdicts([_workflow(agent_type=None, role_word="fix", real_edits=1)])
    assert set(rows) == {("workflow-subagent", "inherited")}


def test_an_integrator_that_edited_a_lot_is_never_flagged():
    assert _verdicts([_workflow(role_word="integrate", turns=12, real_edits=10)]) == {}
    assert _verdicts([_agent(role_word="integrate", turns=12, real_edits=10)]) == {}


def test_a_writer_word_that_never_wrote_is_not_flagged():
    assert _verdicts([_workflow(role_word="test", real_edits=0)]) == {}


def test_a_shell_write_counts_as_writing():
    rows = _verdicts([_workflow(role_word="implement", real_edits=0, shell_writes=1)])
    assert rows[("workflow-subagent", "inherited")]["write_turns"] == 1


def test_an_unknown_role_needs_two_edit_turns_to_count_as_a_writer():
    assert _verdicts([_agent(role_word=None, real_edits=1)]) == {}
    rows = _verdicts([_agent(role_word=None, real_edits=2)])
    row = rows[("general-purpose", "inherited")]
    assert row["roles"] == "other 1"
    # Edits and shell writes both count towards the two.
    assert set(_verdicts([_agent(role_word=None, real_edits=1, shell_writes=1)])) == {("general-purpose", "inherited")}


def test_the_unknown_role_threshold_is_configurable():
    th = AgentModelThresholds(unknown_min_edit_turns=1)
    assert set(_verdicts([_agent(role_word=None, real_edits=1)], thresholds=th)) == {("general-purpose", "inherited")}


def test_a_writer_on_sonnet_or_smaller_is_not_flagged():
    assert _verdicts([_agent(role_word="fix", real_edits=2, model=SONNET)]) == {}
    assert _verdicts([_agent(role_word="fix", real_edits=2, model=HAIKU)]) == {}
    assert set(_verdicts([_agent(role_word="fix", real_edits=2, model=FABLE)])) == {("general-purpose", "inherited")}


def test_a_decider_that_edited_enough_is_decide_apply_whoever_chose_the_model():
    run = _agent(role_word="review", turns=6, real_edits=3)
    assert set(_verdicts([run])) == {("general-purpose", "decide-apply")}
    assert set(_verdicts([run], agent_files={"general-purpose": "opus"})) == {("general-purpose", "decide-apply")}
    asked = _agent(role_word="review", turns=6, real_edits=3, alias="opus")
    assert set(_verdicts([asked])) == {("general-purpose", "decide-apply")}


def test_a_decider_that_edited_less_than_the_threshold_is_not_flagged():
    assert _verdicts([_agent(role_word="review", turns=6, real_edits=2)]) == {}
    th = AgentModelThresholds(decide_apply_min_edit_turns=2)
    assert set(_verdicts([_agent(role_word="review", turns=6, real_edits=2)], thresholds=th)) == {
        ("general-purpose", "decide-apply")
    }


def test_a_decider_on_sonnet_is_not_flagged():
    assert _verdicts([_agent(role_word="review", turns=6, real_edits=5, model=SONNET)]) == {}


def test_env_var_set_follows_the_snapshots_env_names():
    run = _workflow(role_word="fix", real_edits=1)
    assert _verdicts([run])[("workflow-subagent", "inherited")]["env_var_set"] == "no"
    rows = _verdicts([run], env_names=["PATH", "CLAUDE_CODE_SUBAGENT_MODEL"])
    assert rows[("workflow-subagent", "inherited")]["env_var_set"] == "yes"
    rows = _verdicts([run], env_names=["PATH"])
    assert rows[("workflow-subagent", "inherited")]["env_var_set"] == "no"


def test_the_force_switch_alone_is_not_the_variable_and_changes_no_verdict():
    asked = _agent(agent_type="claude-implementer", role_word="implement", real_edits=2, alias="opus")
    inherited = _agent(role_word="fix", real_edits=1)
    rows = _verdicts([asked, inherited], env_names=["CLAUDE_CODE_SUBAGENT_MODEL_FORCE"])
    assert set(rows) == {("claude-implementer", "asked"), ("general-purpose", "inherited")}
    assert rows[("general-purpose", "inherited")]["env_var_set"] == "no"


def test_roles_are_counted_most_first_with_other_for_no_word():
    runs = [
        _workflow(role_word="fix", real_edits=1),
        _workflow(role_word="implement", real_edits=1),
        _workflow(role_word="implement", real_edits=1),
        _workflow(role_word=None, real_edits=2),
    ]
    row = _verdicts(runs)[("workflow-subagent", "inherited")]
    assert row["runs"] == 4
    assert row["roles"] == "implement 2, fix 1, other 1"


def test_dates_and_distinct_workflow_runs():
    runs = [
        _workflow(role_word="fix", real_edits=1, first_ts="2026-09-23T08:00:00.000Z", workflow_run_id="wf_test_x"),
        _workflow(role_word="fix", real_edits=1, first_ts="2026-10-01T08:00:00.000Z", workflow_run_id="wf_test_x"),
        _workflow(role_word="fix", real_edits=1, first_ts="2026-09-30T08:00:00.000Z", workflow_run_id="wf_test_y"),
    ]
    row = _verdicts(runs)[("workflow-subagent", "inherited")]
    assert row["first_seen"] == "2026-09-23"
    assert row["last_seen"] == "2026-10-01"
    assert row["workflow_runs"] == 2


# -- later compliant writers ---------------------------------------------------


def test_later_compliant_counts_writers_of_the_same_kind_that_started_after_the_last_flagged_run():
    flagged = [
        _workflow(role_word="fix", real_edits=1, first_ts="2026-09-23T08:00:00.000Z"),
        _workflow(role_word="fix", real_edits=1, first_ts="2026-09-23T09:00:00.000Z"),
    ]
    later = [
        _workflow(role_word="implement", real_edits=1, model=SONNET, alias="sonnet", first_ts="2026-09-24T08:00:00.000Z"),
        _workflow(role_word="fix", real_edits=2, model=SONNET, first_ts="2026-09-25T08:00:00.000Z"),
    ]
    before = _workflow(role_word="implement", real_edits=1, model=SONNET, first_ts="2026-09-22T08:00:00.000Z")
    not_a_writer = _workflow(role_word="implement", real_edits=0, model=SONNET, first_ts="2026-09-26T08:00:00.000Z")
    still_opus = _workflow(role_word="implement", real_edits=1, first_ts="2026-09-27T08:00:00.000Z", alias="opus")
    agent_tool = _agent(role_word="fix", real_edits=1, model=SONNET, first_ts="2026-09-28T08:00:00.000Z")
    row = _verdicts([*flagged, *later, before, not_a_writer, still_opus, agent_tool])[("workflow-subagent", "inherited")]
    assert row["later_compliant"] == 2


def test_a_writer_on_a_model_with_no_known_tier_is_not_a_later_compliant_writer():
    flagged = _workflow(role_word="fix", real_edits=1, first_ts="2026-09-23T08:00:00.000Z")
    unknown = [
        _workflow(role_word="fix", real_edits=1, model="claude-mystery-1", first_ts="2026-09-24T08:00:00.000Z"),
        _workflow(role_word="implement", real_edits=1, model="", first_ts="2026-09-25T08:00:00.000Z"),
    ]
    sonnet = _workflow(role_word="fix", real_edits=1, model=SONNET, first_ts="2026-09-26T08:00:00.000Z")
    row = _verdicts([flagged, *unknown, sonnet])[("workflow-subagent", "inherited")]
    assert row["later_compliant"] == 1


def test_later_compliant_for_an_agent_tool_group_counts_agent_tool_writers_of_any_type():
    flagged = _agent(role_word="fix", real_edits=1, first_ts="2026-09-23T08:00:00.000Z")
    later = [
        _agent(agent_type="claude-implementer", role_word="implement", real_edits=1, model=SONNET, alias="sonnet",
               first_ts="2026-09-24T08:00:00.000Z"),
        _agent(role_word="fix", real_edits=1, model=HAIKU, first_ts="2026-09-25T08:00:00.000Z"),
        _workflow(role_word="fix", real_edits=1, model=SONNET, first_ts="2026-09-26T08:00:00.000Z"),
    ]
    row = _verdicts([flagged, *later])[("general-purpose", "inherited")]
    assert row["later_compliant"] == 2


def test_later_compliant_is_zero_for_asked_and_decide_apply():
    runs = [
        _agent(role_word="fix", real_edits=1, alias="opus", first_ts="2026-09-23T08:00:00.000Z"),
        _agent(role_word="review", turns=6, real_edits=4, first_ts="2026-09-23T08:00:00.000Z"),
        _agent(role_word="fix", real_edits=1, model=SONNET, first_ts="2026-09-25T08:00:00.000Z"),
    ]
    rows = _verdicts(runs)
    assert rows[("general-purpose", "asked")]["later_compliant"] == 0
    assert rows[("general-purpose", "decide-apply")]["later_compliant"] == 0


# -- thresholds ----------------------------------------------------------------


def test_thresholds_default_and_describe():
    th = AgentModelThresholds()
    assert (th.later_compliant_for_info, th.unknown_min_edit_turns, th.decide_apply_min_edit_turns) == (3, 2, 3)
    described = th.describe()
    assert described and all(isinstance(line, str) and line for line in described)
    assert all(len(line.split()) <= 25 for line in described)


def test_thresholds_from_config_reads_the_agent_models_sub_dict():
    th = AgentModelThresholds.from_config(
        {"agent_models": {"later_compliant_for_info": 5, "unknown_min_edit_turns": 4, "decide_apply_min_edit_turns": 6},
         "model_swap": {"min_sessions": 99}}
    )
    assert (th.later_compliant_for_info, th.unknown_min_edit_turns, th.decide_apply_min_edit_turns) == (5, 4, 6)


def test_thresholds_from_config_accepts_the_flat_shape_and_keeps_defaults():
    assert AgentModelThresholds.from_config({"unknown_min_edit_turns": 7}).unknown_min_edit_turns == 7
    assert AgentModelThresholds.from_config({"unknown_min_edit_turns": 7}).later_compliant_for_info == 3
    assert AgentModelThresholds.from_config({"model_swap": {"min_sessions": 1}}) == AgentModelThresholds()
    assert AgentModelThresholds.from_config(None) == AgentModelThresholds()
    assert AgentModelThresholds.from_config({}) == AgentModelThresholds()
    assert AgentModelThresholds.from_config({"agent_models": "nope"}) == AgentModelThresholds()


def test_assumptions_are_two_lines():
    assert len(agent_models.ASSUMPTIONS) == 2
    assert all(isinstance(line, str) and line for line in agent_models.ASSUMPTIONS)


# -- rules ---------------------------------------------------------------------


def _report(stats) -> ReportModel:
    return ReportModel(
        sections=[Section(key="model_swap", title="Model-swap counterfactual", tables=[agent_models.build_table(stats)])]
    )


def _fire(report, rule_id, th=None, archetype=None):
    return agent_models.RULES[rule_id](report, th or AgentModelThresholds(), archetype, None)


def test_rules_are_the_three_ids():
    assert set(agent_models.RULES) == {"agent-model-inherited", "agent-model-asked", "agent-decide-apply"}


def test_inherited_rule_is_info_fixed_when_later_runs_comply_and_advice_when_they_do_not(fixture_stats):
    recs = _fire(_report(fixture_stats), "agent-model-inherited")
    by_type = {rec.agent_type: rec for rec in recs}
    assert set(by_type) == {"workflow-subagent", "general-purpose"}

    fixed = by_type["workflow-subagent"]
    assert (fixed.severity, fixed.variant) == ("info", "fixed")
    assert fixed.subject == "2026-01-13"

    open_ = by_type["general-purpose"]
    assert (open_.severity, open_.variant) == ("advice", "")
    assert open_.subject == "2026-01-05"

    for rec in recs:
        assert rec.id == "agent-model-inherited"
        assert rec.category == "workflow"
        assert rec.lever == "model"
        assert rec.scope == "user"
        assert rec.archetypes == model_swap._ALL_ARCHETYPES
        assert rec.saving_usd is not None and rec.saving_usd > 0
        assert rec.title and rec.action
        assert rec.why == ""


def test_the_fixed_variant_follows_the_later_compliant_threshold(fixture_stats):
    report = _report(fixture_stats)
    strict = AgentModelThresholds(later_compliant_for_info=26)
    by_type = {rec.agent_type: rec for rec in _fire(report, "agent-model-inherited", strict)}
    assert (by_type["workflow-subagent"].severity, by_type["workflow-subagent"].variant) == ("advice", "")
    exact = AgentModelThresholds(later_compliant_for_info=25)
    by_type = {rec.agent_type: rec for rec in _fire(report, "agent-model-inherited", exact)}
    assert (by_type["workflow-subagent"].severity, by_type["workflow-subagent"].variant) == ("info", "fixed")


def test_an_ignored_inherited_card_stays_ignored_until_a_run_on_a_later_day(tmp_path):
    """The card suggests no setting change, so ``ignores.fingerprint`` keys
    it on its rule, agent type, lever and subject (the last-seen date). More
    runs on the same day move the saving but keep the ignore; a run on a
    later day is a new card, so it shows again."""

    def card(*runs):
        [rec] = _fire(_report(compute_agent_models(list(runs), PRICING)), "agent-model-inherited")
        rec.key = recommend._rec_key(rec)
        return rec

    first = _workflow(role_word="implement", real_edits=2, first_ts="2026-09-20T10:00:00.000Z")
    ignored = card(first)
    assert (ignored.key, ignored.lever, ignored.subject, ignored.changes) == (
        "agent-model-inherited:workflow-subagent",
        "model",
        "2026-09-20",
        [],
    )
    ignores.set_ignored(tmp_path, [ignored], ignored=True, project=None)

    same_day = card(first, _workflow(role_word="fix", real_edits=1, first_ts="2026-09-20T15:00:00.000Z"))
    assert same_day.saving_usd > ignored.saving_usd
    assert ignores.fingerprint(same_day) == ignores.fingerprint(ignored)
    assert ignores.skip_keys(tmp_path, [same_day], None) == frozenset({same_day.key})

    later = card(first, _workflow(role_word="fix", real_edits=1, first_ts="2026-09-24T09:00:00.000Z"))
    assert (later.key, later.subject) == (ignored.key, "2026-09-24")
    assert ignores.fingerprint(later) != ignores.fingerprint(ignored)
    assert ignores.skip_keys(tmp_path, [later], None) == frozenset()


def test_decide_apply_rule_is_info_with_no_saving(fixture_stats):
    recs = _fire(_report(fixture_stats), "agent-decide-apply")
    assert len(recs) == 1
    rec = recs[0]
    assert rec.id == "agent-decide-apply"
    assert rec.severity == "info"
    assert rec.variant == ""
    assert rec.agent_type == "general-purpose"
    assert rec.subject == "2026-01-05"
    assert rec.saving_usd is None


def test_asked_rule_is_info_and_carries_the_ceiling_saving():
    stats = compute_agent_models(
        [_agent(agent_type="claude-implementer", role_word="implement", real_edits=2, alias="opus")], PRICING
    )
    recs = _fire(_report(stats), "agent-model-asked")
    assert len(recs) == 1
    rec = recs[0]
    assert rec.id == "agent-model-asked"
    assert (rec.severity, rec.variant) == ("info", "")
    assert rec.agent_type == "claude-implementer"
    assert rec.subject == "2026-09-23"
    assert rec.saving_usd is not None and rec.saving_usd > 0
    # An asked row never fires the other two rules.
    assert _fire(_report(stats), "agent-model-inherited") == []
    assert _fire(_report(stats), "agent-decide-apply") == []


def test_each_rule_fires_only_for_its_own_verdict(fixture_stats):
    report = _report(fixture_stats)
    assert _fire(report, "agent-model-asked") == []
    assert len(_fire(report, "agent-model-inherited")) == 2
    assert len(_fire(report, "agent-decide-apply")) == 1


def test_evidence_labels_are_exactly_the_contract_and_resolve_to_the_row(fixture_stats):
    report = _report(fixture_stats)
    table = report.sections[0].tables[0]
    rows = _rows(table)
    cells = {
        "Agents": "runs",
        "Roles": "roles",
        "Model": "model",
        "Cost (USD)": "cost_usd",
        "Cost on Sonnet (USD)": "cost_on_sonnet_usd",
        "Ceiling saving (USD)": "saving_usd",
        "Ceiling saving (%)": "saving_pct",
        "Edit turns": "write_turns",
        "Workflow runs": "workflow_runs",
        "First seen": "first_seen",
        "Last seen": "last_seen",
        "Later compliant writers": "later_compliant",
        "CLAUDE_CODE_SUBAGENT_MODEL set": "env_var_set",
    }
    for rule_id in agent_models.RULES:
        for rec in _fire(report, rule_id):
            assert [label for label, *_ in rec.evidence] == EVIDENCE_LABELS
            verdict = {"agent-model-inherited": "inherited", "agent-model-asked": "asked", "agent-decide-apply": "decide-apply"}[rule_id]
            row = rows[(rec.agent_type, verdict)]
            for label, value, source, row_key in rec.evidence:
                assert source == "model_swap.model_swap_agent_models"
                assert row_key == row["case"] == f"{rec.agent_type}:{verdict}"
                assert value == row[cells[label]]


def test_env_var_value_reaches_the_evidence():
    stats = compute_agent_models(
        [_workflow(role_word="fix", real_edits=1)], PRICING, env_names=["CLAUDE_CODE_SUBAGENT_MODEL"]
    )
    rec = _fire(_report(stats), "agent-model-inherited")[0]
    assert dict((label, value) for label, value, *_ in rec.evidence)["CLAUDE_CODE_SUBAGENT_MODEL set"] == "yes"


def test_rules_stay_quiet_for_an_archetype_that_never_spawns_subagents(fixture_stats):
    report = _report(fixture_stats)
    for archetype in model_swap._NO_SUBAGENT_ARCHETYPES:
        for rule_id in agent_models.RULES:
            assert _fire(report, rule_id, archetype=archetype) == []
    # Any other archetype is not gated.
    assert len(_fire(report, "agent-model-inherited", archetype="workflow-heavy")) == 2


def test_rules_are_quiet_without_the_table_or_with_an_empty_one(fixture_stats):
    assert _fire(ReportModel(), "agent-model-inherited") == []
    empty = compute_agent_models([], PRICING)
    for rule_id in agent_models.RULES:
        assert _fire(_report(empty), rule_id) == []


def test_empty_stats_build_an_empty_table_with_the_same_columns():
    table = agent_models.build_table(compute_agent_models([], PRICING))
    assert table.rows == []
    assert [column.key for column in table.columns] == COLUMNS

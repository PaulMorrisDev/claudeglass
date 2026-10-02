"""``report.build_report`` and ``recommend.recommend`` wiring for the
agent-model cards (``agent_models.py``): the ``model_swap_agent_models``
table lands in the ``model_swap`` section, its three rules fire from the
rendered table, the report's assumptions carry the module's own, and the
latest config snapshot's agent files and environment names reach it.

The corpus is built on disk the way ``test_corpus.py`` and
``test_report.py`` build theirs: top-level sessions plus a workflow-nested
agent and direct subagents, each with a ``.meta.json`` in the newer shape
(a ``workflowPhase`` or ``description`` key, so an absent ``model`` means
the call set none). Every id, role and path here is made up.
"""

from __future__ import annotations

import json
from pathlib import Path

from claudeglass import agent_models
from claudeglass.config import Config
from claudeglass.corpus import load_corpus
from claudeglass.pricing import load_pricing
from claudeglass.report import build_report
from claudeglass.snapshots import Snapshot

from helpers import tool_use_block, turn_line, write_jsonl

PRICING = load_pricing()

OPUS = "claude-opus-5-5"
SONNET = "claude-sonnet-5-5"
PROJECT = "proj-agent-models"
#: Outside the system temp dir, so the parser counts the edit as real.
_REAL_FILE = "/home/dev/project/app.py"
_TABLE = "model_swap_agent_models"


def _edit_turn(model: str, tool_id: str, day: str, minute: int) -> dict:
    return turn_line(
        model=model,
        input_tokens=4000,
        output_tokens=1500,
        cache_read_input_tokens=30_000,
        timestamp=f"{day}T10:{minute:02d}:00.000Z",
        content=[tool_use_block("Edit", tool_id, {"file_path": _REAL_FILE, "old_string": "a", "new_string": "b"})],
    )


def _write_agent(agent_dir: Path, agent_id: str, meta: dict, model: str, edits: int, day: str) -> None:
    agent_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(
        agent_dir / f"{agent_id}.jsonl",
        [_edit_turn(model, f"toolu_{agent_id}_{n}", day, n) for n in range(edits)],
    )
    (agent_dir / f"{agent_id}.meta.json").write_text(json.dumps(meta), encoding="utf-8")


def _write_workflow_agent(
    project_dir: Path, agent_id: str, meta: dict, model: str, edits: int = 2, day: str = "2026-09-20"
) -> None:
    agent_dir = project_dir / "session-0" / "subagents" / "workflows" / "wf_test_run"
    _write_agent(agent_dir, agent_id, meta, model, edits, day)


def _write_subagent(
    project_dir: Path, agent_id: str, meta: dict, model: str, edits: int = 2, day: str = "2026-09-20"
) -> None:
    _write_agent(project_dir / "session-0" / "subagents", agent_id, meta, model, edits, day)


def _corpus(tmp_path: Path):
    """Six sessions (the gate wants five), the first with the agents the
    caller adds under ``project_dir``. Each runs a shell command, so none
    is a chat-only session (the rules skip those)."""
    project_dir = tmp_path / PROJECT
    project_dir.mkdir()
    for n in range(6):
        write_jsonl(
            project_dir / f"session-{n}.jsonl",
            [
                turn_line(
                    model=OPUS,
                    timestamp=f"2026-09-20T09:{n:02d}:00.000Z",
                    content=[tool_use_block("Bash", f"toolu_top_{n}_{i}", {"command": "ls"})],
                )
                for i in range(3)
            ],
        )
    return project_dir


def _build(project_dir: Path, config: Config | None = None, **kwargs):
    corpus = load_corpus([project_dir])
    return build_report(corpus, PRICING, config or Config(), projects=(PROJECT,), window="agent models test", **kwargs)


def _agent_models_rows(model) -> list[dict]:
    section = next(s for s in model.sections if s.key == "model_swap")
    table = next(t for t in section.tables if t.name == _TABLE)
    keys = [c.key for c in table.columns]
    return [dict(zip(keys, row)) for row in table.rows]


def _recs(model, rule_id: str) -> list:
    return [r for r in model.recommendations if r.id == rule_id]


def _snapshot(agents: dict | None = None, env_names: list[str] | None = None) -> Snapshot:
    ts = "2026-09-25T00:00:00.000Z"
    data = {"schema": 2, "ts": ts, "effective": {}, "agents": agents or {}, "env_names": env_names or []}
    return Snapshot(path="cfg", ts=ts, data=data)


_IMPLEMENT_WITH_NO_MODEL = {"agentType": "workflow-subagent", "workflowPhase": "Implement"}
_IMPLEMENT_ON_SONNET = {"agentType": "workflow-subagent", "workflowPhase": "Implement", "model": "sonnet"}


def test_inherited_workflow_writer_is_in_the_model_swap_section_and_the_recommendations(tmp_path):
    project_dir = _corpus(tmp_path)
    _write_workflow_agent(project_dir, "agent-impl1", _IMPLEMENT_WITH_NO_MODEL, OPUS)

    model = _build(project_dir)

    section = next(s for s in model.sections if s.key == "model_swap")
    assert _TABLE in [t.name for t in section.tables]
    # The table is added to the section, not in place of its own three.
    assert {"model_swap_by_agent_type", "model_swap_summary", "model_swap_agent_file_runs"} <= {
        t.name for t in section.tables
    }

    (row,) = _agent_models_rows(model)
    assert row["agent_type"] == "workflow-subagent"
    assert row["verdict"] == "inherited"
    assert row["runs"] == 1
    assert row["roles"] == "implement 1"
    assert row["model"] == OPUS
    assert row["write_turns"] == 2
    assert 0 < row["cost_on_sonnet_usd"] < row["cost_usd"]
    assert row["saving_usd"] == row["cost_usd"] - row["cost_on_sonnet_usd"]
    assert row["env_var_set"] == "no"

    (rec,) = _recs(model, "agent-model-inherited")
    assert rec.severity == "advice"
    assert rec.agent_type == "workflow-subagent"
    assert rec.saving_usd == row["saving_usd"]
    assert [r.id for r in model.recommendations if r.id.startswith("agent-")] == ["agent-model-inherited"]


def _write_later_sonnet_writers(project_dir: Path, count: int) -> None:
    for n in range(count):
        _write_workflow_agent(project_dir, f"agent-later{n}", _IMPLEMENT_ON_SONNET, SONNET, day="2026-09-25")


def test_three_later_writers_on_sonnet_turn_the_advice_into_a_tip(tmp_path):
    project_dir = _corpus(tmp_path)
    _write_workflow_agent(project_dir, "agent-impl1", _IMPLEMENT_WITH_NO_MODEL, OPUS)
    _write_later_sonnet_writers(project_dir, 3)

    model = _build(project_dir)

    (row,) = _agent_models_rows(model)
    assert row["later_compliant"] == 3
    (rec,) = _recs(model, "agent-model-inherited")
    assert (rec.severity, rec.variant) == ("info", "fixed")


def test_the_later_writer_count_comes_from_the_agent_models_thresholds_in_the_config(tmp_path):
    project_dir = _corpus(tmp_path)
    _write_workflow_agent(project_dir, "agent-impl1", _IMPLEMENT_WITH_NO_MODEL, OPUS)
    _write_later_sonnet_writers(project_dir, 3)

    stricter = Config(thresholds={"agent_models": {"later_compliant_for_info": 4}})
    (rec,) = _recs(_build(project_dir, stricter), "agent-model-inherited")

    assert (rec.severity, rec.variant) == ("advice", "")


def test_the_reports_assumptions_carry_the_agent_model_ones(tmp_path):
    project_dir = _corpus(tmp_path)

    model = _build(project_dir)

    assert agent_models.ASSUMPTIONS
    for line in agent_models.ASSUMPTIONS:
        assert line in model.meta.assumptions


def test_a_report_with_no_agent_runs_has_an_empty_table_and_no_card(tmp_path):
    model = _build(_corpus(tmp_path))

    assert _agent_models_rows(model) == []
    assert not [r for r in model.recommendations if r.id.startswith("agent-")]


def test_the_table_is_not_built_without_the_model_swap_section(tmp_path):
    project_dir = _corpus(tmp_path)
    _write_workflow_agent(project_dir, "agent-impl1", _IMPLEMENT_WITH_NO_MODEL, OPUS)

    model = _build(project_dir, include={"overview"})

    # The rules read the rendered table, so a report without it has no card.
    assert [s.key for s in model.sections] == ["overview"]
    assert not [r for r in model.recommendations if r.id.startswith("agent-")]


def test_a_writer_that_ran_on_sonnet_is_not_flagged(tmp_path):
    project_dir = _corpus(tmp_path)
    _write_workflow_agent(project_dir, "agent-impl1", _IMPLEMENT_WITH_NO_MODEL, SONNET)

    model = _build(project_dir)

    assert _agent_models_rows(model) == []
    assert not [r for r in model.recommendations if r.id.startswith("agent-")]


def test_an_agent_file_naming_the_runs_family_is_not_an_accident(tmp_path):
    project_dir = _corpus(tmp_path)
    meta = {"agentType": "claude-implementer", "description": "build thing"}
    _write_subagent(project_dir, "agent-impl1", meta, OPUS)

    without_file = _build(project_dir)
    assert [(r["agent_type"], r["verdict"]) for r in _agent_models_rows(without_file)] == [
        ("claude-implementer", "inherited")
    ]
    assert len(_recs(without_file, "agent-model-inherited")) == 1

    snapshot = _snapshot(agents={"claude-implementer": {"source": "user", "model": "opus"}})
    with_file = _build(project_dir, snapshots=[snapshot], all_projects=True)
    assert _agent_models_rows(with_file) == []
    assert not [r for r in with_file.recommendations if r.id.startswith("agent-")]


def test_an_agent_file_naming_another_family_does_not_excuse_the_run(tmp_path):
    project_dir = _corpus(tmp_path)
    meta = {"agentType": "claude-implementer", "description": "build thing"}
    _write_subagent(project_dir, "agent-impl1", meta, OPUS)

    snapshot = _snapshot(agents={"claude-implementer": {"source": "user", "model": "sonnet"}})
    model = _build(project_dir, snapshots=[snapshot], all_projects=True)

    assert [(r["agent_type"], r["verdict"]) for r in _agent_models_rows(model)] == [
        ("claude-implementer", "inherited")
    ]


def test_the_subagent_model_variable_in_the_snapshot_reaches_the_table(tmp_path):
    project_dir = _corpus(tmp_path)
    _write_workflow_agent(project_dir, "agent-impl1", _IMPLEMENT_WITH_NO_MODEL, OPUS)

    snapshot = _snapshot(env_names=["CLAUDE_CODE_SUBAGENT_MODEL"])
    model = _build(project_dir, snapshots=[snapshot], all_projects=True)

    (row,) = _agent_models_rows(model)
    assert row["env_var_set"] == "yes"


def test_asked_and_decide_apply_cards_fire_beside_the_inherited_one(tmp_path):
    project_dir = _corpus(tmp_path)
    _write_workflow_agent(project_dir, "agent-impl1", _IMPLEMENT_WITH_NO_MODEL, OPUS)
    _write_subagent(
        project_dir, "agent-asked1", {"agentType": "claude-implementer", "model": "opus", "description": "build"}, OPUS
    )
    _write_subagent(
        project_dir, "agent-audit1", {"agentType": "general-purpose", "description": "Audit the module"}, OPUS, edits=3
    )

    model = _build(project_dir)

    verdicts = {(r["agent_type"], r["verdict"]) for r in _agent_models_rows(model)}
    assert verdicts == {
        ("workflow-subagent", "inherited"),
        ("claude-implementer", "asked"),
        ("general-purpose", "decide-apply"),
    }
    severities = {r.id: r.severity for r in model.recommendations if r.id.startswith("agent-")}
    assert severities == {"agent-model-inherited": "advice", "agent-model-asked": "info", "agent-decide-apply": "info"}
    (decide,) = _recs(model, "agent-decide-apply")
    assert decide.saving_usd is None


def test_every_evidence_row_the_cards_cite_is_a_row_of_the_table(tmp_path):
    project_dir = _corpus(tmp_path)
    _write_workflow_agent(project_dir, "agent-impl1", _IMPLEMENT_WITH_NO_MODEL, OPUS)
    _write_subagent(
        project_dir, "agent-asked1", {"agentType": "claude-implementer", "model": "opus", "description": "build"}, OPUS
    )

    model = _build(project_dir)

    section = next(s for s in model.sections if s.key == "model_swap")
    table = next(t for t in section.tables if t.name == _TABLE)
    rows = {row[0]: row for row in table.rows}
    cards = [r for r in model.recommendations if r.id.startswith("agent-")]
    assert cards
    for card in cards:
        assert card.evidence
        for label, value, source_table, row_key in card.evidence:
            assert source_table == f"model_swap.{_TABLE}", label
            assert row_key in rows, (card.id, label, row_key)
            assert value in rows[row_key], (card.id, label, value)

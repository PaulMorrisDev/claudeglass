"""Tests for WP12a's phase classification and cost split
(src/claudeglass/phases.py).

All ``Turn``s here are hand-built directly (no jsonl round-trip needed --
``classify_turn_phase`` and ``PhaseStats`` operate on already-parsed
``Turn``/``TranscriptResult`` objects), one per phase-classification rule
in the module docstring, plus the aggregation and ``build_section``
behaviour.
"""

from __future__ import annotations

import json

import pytest

from claudeglass import discovery, phases
from claudeglass.model import Section, Table, TranscriptMeta, TranscriptResult, Turn
from claudeglass.pricing import load_pricing

pytestmark = pytest.mark.filterwarnings("ignore")


def _turn(turn_index: int = 1, **overrides) -> Turn:
    defaults = {"model": "claude-sonnet-5", "input_tokens": 100, "output_tokens": 50}
    defaults.update(overrides)
    return Turn(turn_index=turn_index, **defaults)


# -- classify_turn_phase: one case per rule in the docstring ----------------


def test_no_tools_is_other():
    turn = _turn(tool_names=())
    assert phases.classify_turn_phase(turn) == phases.PHASE_OTHER


def test_read_only_is_discovery():
    turn = _turn(tool_names=("Read",))
    assert phases.classify_turn_phase(turn) == phases.PHASE_DISCOVERY


def test_grep_glob_websearch_webfetch_listagents_are_all_discovery():
    for tool in ("Grep", "Glob", "WebFetch", "WebSearch", "ListAgents"):
        turn = _turn(tool_names=(tool,))
        assert phases.classify_turn_phase(turn) == phases.PHASE_DISCOVERY, tool


def test_multiple_discovery_tools_together_still_discovery():
    turn = _turn(tool_names=("Read", "Grep", "Glob"))
    assert phases.classify_turn_phase(turn) == phases.PHASE_DISCOVERY


def test_real_edit_is_implementation():
    turn = _turn(tool_names=("Edit",), edit_kind="real")
    assert phases.classify_turn_phase(turn) == phases.PHASE_IMPLEMENTATION


def test_ordinary_bash_command_is_implementation():
    turn = _turn(tool_names=("Bash",), cmd_prefix="git status")
    assert phases.classify_turn_phase(turn) == phases.PHASE_IMPLEMENTATION


def test_ordinary_powershell_command_is_implementation():
    turn = _turn(tool_names=("PowerShell",), cmd_prefix="Get-ChildItem")
    assert phases.classify_turn_phase(turn) == phases.PHASE_IMPLEMENTATION


def test_read_plus_bash_ordinary_command_is_implementation():
    # Not a subset of the discovery-only tool set (Bash disqualifies it),
    # and the Bash command isn't a recognised test/build tool.
    turn = _turn(tool_names=("Read", "Bash"), cmd_prefix="cat README.md")
    assert phases.classify_turn_phase(turn) == phases.PHASE_IMPLEMENTATION


@pytest.mark.parametrize(
    "cmd_prefix",
    [
        "pytest -q",
        "python -m pytest tests/",
        "dotnet test",
        "dotnet build",
        "npm test",
        "npx vitest run",
        "npx playwright test",
        "go test ./...",
        "cargo test",
        "make check",
        "mvn test",
        "gradle test",
    ],
)
def test_recognised_test_or_build_command_is_verification(cmd_prefix):
    turn = _turn(tool_names=("Bash",), cmd_prefix=cmd_prefix)
    assert phases.classify_turn_phase(turn) == phases.PHASE_VERIFICATION


def test_verification_prefix_match_is_not_anchored_to_full_string_start_only():
    # cmd_prefix may have leading whitespace (parse.py doesn't guarantee
    # a stripped value) -- classify_turn_phase must lstrip before matching,
    # matching classify.py's own _matches_test_tool convention.
    turn = _turn(tool_names=("Bash",), cmd_prefix="  pytest -q")
    assert phases.classify_turn_phase(turn) == phases.PHASE_VERIFICATION


def test_a_test_run_any_spelling_is_verification():
    assert phases.classify_turn_phase(_turn(tool_names=("Bash",), cmd_prefix="cd /c/x", tests_run="full")) == phases.PHASE_VERIFICATION


def test_scratch_edit_is_verification():
    turn = _turn(tool_names=("Edit",), edit_kind="scratch")
    assert phases.classify_turn_phase(turn) == phases.PHASE_VERIFICATION


def test_unrecognised_shell_command_prefix_is_not_verification():
    # "npm install" is not "npm test" -- must not match via a loose
    # substring check.
    turn = _turn(tool_names=("Bash",), cmd_prefix="npm install")
    assert phases.classify_turn_phase(turn) == phases.PHASE_IMPLEMENTATION


def test_task_only_tool_falls_back_to_other():
    turn = _turn(tool_names=("Task",))
    assert phases.classify_turn_phase(turn) == phases.PHASE_OTHER


def test_verification_prefix_list_is_fuller_than_classify_pys_own():
    from claudeglass.classify import _TEST_TOOL_PREFIXES

    assert set(_TEST_TOOL_PREFIXES) < set(phases._VERIFICATION_CMD_PREFIXES)


# -- PhaseStats aggregation ---------------------------------------------------


def _result(kind: str, agent_type: str | None, turns: list[Turn]) -> TranscriptResult:
    meta = TranscriptMeta(kind=kind, agent_type=agent_type, session_id="sess")
    return TranscriptResult(meta=meta, turns=turns)


def test_add_transcript_buckets_turns_by_phase():
    pricing = load_pricing()
    stats = phases.PhaseStats()
    turns = [
        _turn(1, tool_names=("Read",)),
        _turn(2, tool_names=("Edit",), edit_kind="real"),
        _turn(3, tool_names=("Bash",), cmd_prefix="pytest -q"),
        _turn(4, tool_names=()),
    ]
    stats.add_transcript(_result("top-level", None, turns), pricing)

    assert stats.overall[phases.PHASE_DISCOVERY].turns == 1
    assert stats.overall[phases.PHASE_IMPLEMENTATION].turns == 1
    assert stats.overall[phases.PHASE_VERIFICATION].turns == 1
    assert stats.overall[phases.PHASE_OTHER].turns == 1
    assert stats.total_turns == 4


def test_add_transcript_ignores_turn_index_zero():
    # turn_index == 0 is the "not actually a priced turn" sentinel
    # (mirrors topology.py/compaction.py's own _priced_turns filter).
    pricing = load_pricing()
    stats = phases.PhaseStats()
    turns = [_turn(0, tool_names=("Read",)), _turn(1, tool_names=("Read",))]
    stats.add_transcript(_result("top-level", None, turns), pricing)
    assert stats.total_turns == 1


def test_new_tokens_is_input_plus_cache_creation():
    pricing = load_pricing()
    stats = phases.PhaseStats()
    turn = _turn(1, tool_names=("Read",), input_tokens=100, cache_creation_tokens=40, cache_read_tokens=7, output_tokens=9)
    stats.add_transcript(_result("top-level", None, [turn]), pricing)
    bucket = stats.overall[phases.PHASE_DISCOVERY]
    assert bucket.new_tokens == 140
    assert bucket.cache_read_tokens == 7
    assert bucket.output_tokens == 9


def test_add_transcript_uses_price_turn_for_cost():
    from claudeglass.pricing import price_turn

    pricing = load_pricing()
    stats = phases.PhaseStats()
    turn = _turn(1, tool_names=("Read",), input_tokens=1000, output_tokens=500)
    stats.add_transcript(_result("top-level", None, [turn]), pricing)

    expected = price_turn(turn, pricing.resolve_model(turn.model)).total
    assert stats.overall[phases.PHASE_DISCOVERY].cost == pytest.approx(expected)
    assert stats.total_cost == pytest.approx(expected)


def test_by_kind_and_by_agent_type_breakdowns():
    pricing = load_pricing()
    stats = phases.PhaseStats()
    top_turns = [_turn(1, tool_names=("Read",))]
    sub_turns = [_turn(1, tool_names=("Edit",), edit_kind="real")]

    stats.add_transcript(_result("top-level", None, top_turns), pricing)
    stats.add_transcript(_result("subagent", "implementer", sub_turns), pricing)

    assert stats.by_kind[(phases.PHASE_DISCOVERY, "top-level")].turns == 1
    assert stats.by_kind[(phases.PHASE_IMPLEMENTATION, "subagent")].turns == 1
    assert stats.by_agent_type[(phases.PHASE_DISCOVERY, "top-level")].turns == 1
    assert stats.by_agent_type[(phases.PHASE_IMPLEMENTATION, "implementer")].turns == 1


def test_workflow_agents_get_their_own_by_kind_row(tmp_path):
    # An agent under a workflow run's folder, as discovery loads it.
    run_dir = tmp_path / "sess" / "subagents" / "workflows" / "wf_1"
    run_dir.mkdir(parents=True)
    meta_path = run_dir / "agent-a1.meta.json"
    meta_path.write_text(json.dumps({"agentType": "workflow-subagent"}))
    workflow = TranscriptResult(meta=discovery.load_meta(meta_path), turns=[_turn(1, tool_names=("Read",))])

    pricing = load_pricing()
    stats = phases.PhaseStats()
    stats.add_transcript(_result("subagent", "Explore", [_turn(1, tool_names=("Read",))]), pricing)
    stats.add_transcript(workflow, pricing)

    assert stats.by_kind[(phases.PHASE_DISCOVERY, "subagent")].turns == 1
    assert stats.by_kind[(phases.PHASE_DISCOVERY, "workflow-agent")].turns == 1
    assert stats.by_agent_type[(phases.PHASE_DISCOVERY, "workflow-subagent")].turns == 1
    table = next(t for t in phases.build_section(stats).tables if t.name == "phases_by_transcript_kind")
    assert sorted(row[0] for row in table.rows) == ["subagent", "workflow-agent"]


def test_agent_type_is_top_level_for_main_and_unknown_for_untyped_subagent():
    pricing = load_pricing()
    stats = phases.PhaseStats()
    stats.add_transcript(_result("top-level", None, [_turn(1, tool_names=("Read",))]), pricing)
    stats.add_transcript(_result("subagent", None, [_turn(1, tool_names=("Read",))]), pricing)
    assert (phases.PHASE_DISCOVERY, "top-level") in stats.by_agent_type
    assert (phases.PHASE_DISCOVERY, "unknown") in stats.by_agent_type


def test_kind_defaults_to_top_level_when_unset():
    pricing = load_pricing()
    stats = phases.PhaseStats()
    meta = TranscriptMeta(kind="", agent_type=None, session_id="sess")
    result = TranscriptResult(meta=meta, turns=[_turn(1, tool_names=("Read",))])
    stats.add_transcript(result, pricing)
    assert (phases.PHASE_DISCOVERY, "top-level") in stats.by_kind


# -- cost_share / discovery_share_exceeds_threshold ---------------------------


def test_cost_share_is_none_when_nothing_priced():
    stats = phases.PhaseStats()
    assert stats.cost_share(phases.PHASE_DISCOVERY) is None
    assert phases.discovery_share_exceeds_threshold(stats) is False


def test_cost_share_sums_to_one_across_phases():
    pricing = load_pricing()
    stats = phases.PhaseStats()
    turns = [
        _turn(1, tool_names=("Read",), input_tokens=1000),
        _turn(2, tool_names=("Edit",), edit_kind="real", input_tokens=1000),
    ]
    stats.add_transcript(_result("top-level", None, turns), pricing)
    total_share = sum(stats.cost_share(p) or 0.0 for p in phases.PHASES)
    assert total_share == pytest.approx(1.0)


def test_discovery_share_exceeds_threshold_true_when_over_35_pct():
    pricing = load_pricing()
    stats = phases.PhaseStats()
    # Heavily weight DISCOVERY's token volume so its cost share clears 35%.
    turns = [
        _turn(1, tool_names=("Read",), input_tokens=100_000),
        _turn(2, tool_names=("Edit",), edit_kind="real", input_tokens=1000),
    ]
    stats.add_transcript(_result("top-level", None, turns), pricing)
    assert stats.cost_share(phases.PHASE_DISCOVERY) > 0.35
    assert phases.discovery_share_exceeds_threshold(stats) is True


def test_discovery_share_exceeds_threshold_false_when_under_35_pct():
    pricing = load_pricing()
    stats = phases.PhaseStats()
    turns = [
        _turn(1, tool_names=("Read",), input_tokens=100),
        _turn(2, tool_names=("Edit",), edit_kind="real", input_tokens=100_000),
    ]
    stats.add_transcript(_result("top-level", None, turns), pricing)
    assert phases.discovery_share_exceeds_threshold(stats) is False


# -- build_section -------------------------------------------------------


def test_build_section_shape():
    pricing = load_pricing()
    stats = phases.PhaseStats()
    turns = [
        _turn(1, tool_names=("Read",)),
        _turn(2, tool_names=("Edit",), edit_kind="real"),
        _turn(3, tool_names=("Bash",), cmd_prefix="pytest -q"),
    ]
    stats.add_transcript(_result("top-level", None, turns), pricing)

    section = phases.build_section(stats)
    assert isinstance(section, Section)
    assert section.key == "phases"
    assert len(section.tables) == 3
    for table in section.tables:
        assert isinstance(table, Table)
    assert any(t.name == "phases_summary" for t in section.tables)
    assert any(t.name == "phases_by_transcript_kind" for t in section.tables)
    assert any(t.name == "phases_by_agent_type" for t in section.tables)
    assert section.notes, "expected non-empty notes describing the classification rule"


def test_build_section_summary_table_has_a_row_per_phase():
    stats = phases.PhaseStats()
    section = phases.build_section(stats)
    summary = next(t for t in section.tables if t.name == "phases_summary")
    assert [row[0] for row in summary.rows] == list(phases.PHASES)
    # Nothing priced yet -> share column is None, not 0.0.
    for row in summary.rows:
        assert row[-1] is None


def test_build_section_notes_mention_the_threshold():
    stats = phases.PhaseStats()
    section = phases.build_section(stats)
    assert any("35%" in note for note in section.notes)

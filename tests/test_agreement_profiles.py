"""The seven synthetic profiles (``agreement_profiles.py``) each produce the
cards the agreement checks read.

A check over a profile with no tools card, no info card with a saving and no
bundle proves nothing, and passes. So each profile is held to what it has to
build: a profile that silently stops producing a kind of card fails here, by
name, instead of letting every check pass over nothing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import agreement
import agreement_profiles as ap
from claudeglass import change_points, corpus, fixes, model_swap, recommend
from claudeglass.pricing import load_pricing

NAMES = list(ap.SPECS)
CUSTOM = [name for name in NAMES if ap.SPECS[name].custom_agents]


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    return lambda name: ap.profile_inputs(name, tmp_path_factory)


def _cards(inp, rec_id):
    return [rec for rec in inp.recs if rec.id == rec_id]


def test_there_are_seven_profiles_of_the_kinds_the_plan_names():
    assert NAMES == ["api", "cli_status", "bare", "old_version", "capture_on", "capture_off", "two_projects"]
    api, cli, bare, old, on, off, two = (ap.SPECS[n] for n in NAMES)
    assert api.billing == "api" and cli.billing == "subscription"
    assert cli.entrypoint == "cli" and cli.statusline and not api.statusline
    assert not bare.custom_agents and not bare.workflows
    assert old.version == "2.1.300" and not old.request_lines
    assert on.capture and not off.capture
    assert two.second_project and not two.custom_agents


@pytest.mark.parametrize("name", NAMES)
def test_every_profile_has_the_cards_every_check_reads(built, name):
    profile, inp = built(name)
    recs = inp.recs
    # A hook that blocks, and Claude sends the same call again.
    assert _cards(inp, "hook-block-resent"), name
    # An info card with a saving: it must add nothing to a total (check 3).
    assert [r for r in recs if r.severity == "info" and (r.saving_usd or 0) > 0], name
    # A main-session model card that changes a setting (check 7).
    model = _cards(inp, "model-tier-main")
    assert model and model[0].changes and model[0].changes[0].key == "model", name
    # A standing-rule card, which the bundle puts under a rule, not a file (check 5).
    rules = [
        r for r in recs if r.severity in agreement.COUNTING
        and any(fixes.fix_group(r, c)[1] == "rule" for c in (r.changes or [None]))
    ]
    assert rules, name
    # A built-in agent with no file whose card names ~/.claude/agents/ (check 2).
    builtin = [r for r in _cards(inp, "spawn-tools-list") if r.agent_type == "general-purpose"]
    assert builtin and "~/.claude/agents/" in builtin[0].action, name
    assert "general-purpose" not in inp.agents, name
    # The bundle has files, rules and the information it leaves out.
    bundle = fixes.project_prompt(recs, set(inp.skip))
    assert bundle["files"] and bundle["several"] and bundle["left_out"]["info"], name


@pytest.mark.parametrize("name", CUSTOM)
def test_a_custom_agent_with_a_tools_line_gets_a_card_that_leaves_tools_out(built, name):
    """The reviewer's line is in the snapshot; its spawns call Read and Grep
    every time, Edit in two of five and Bash once (check 1 and the Tools check)."""
    profile, inp = built(name)
    reviewer = [r for r in _cards(inp, "spawn-tools-list") if r.agent_type == "reviewer"]
    assert reviewer, name
    assert inp.agents["reviewer"]["tools"], name
    line = reviewer[0].changes[0].value
    assert "Read" in line and "Grep" in line and "NotebookEdit" not in line, line
    rows = {row["agent_type"]: row for row in agreement.tables_of(inp.model)["agent_startup_diet"]}
    assert "Bash" in rows["reviewer"]["rare_tools"] and "Edit" in rows["reviewer"]["keep_tools"]
    # A second agent whose tools line is a list with one MCP tool in it, which it calls: the
    # card writes the tool as the file has it.
    implementer = [r for r in _cards(inp, "spawn-tools-list") if r.agent_type == "claude-implementer"]
    assert implementer and "mcp__figma__get_node" in implementer[0].changes[0].value


@pytest.mark.parametrize("name", CUSTOM)
def test_workflow_agents_that_write_files_give_a_workflow_card_that_keeps_their_tools(built, name):
    profile, inp = built(name)
    workflow = [r for r in _cards(inp, "spawn-tools-list") if r.variant == "workflow-script"]
    assert workflow and not workflow[0].changes, name
    row = {r["agent_type"]: r for r in agreement.tables_of(inp.model)["agent_startup_diet"]}["workflow-subagent"]
    assert {"Edit", "Write"} <= set(agreement._names(row["rule_kept_tools"])), row
    assert re.search(r"Edit and Write stay too", workflow[0].why), workflow[0].why


def test_a_profile_with_no_custom_agents_has_no_workflow_card_and_no_custom_agent(built):
    profile, inp = built("bare")
    assert not [r for r in inp.recs if r.variant == "workflow-script"]
    assert not inp.agents
    types = {r["agent_type"] for r in agreement.tables_of(inp.model)["agent_startup_diet"]}
    assert types <= {"general-purpose", "Explore"}


def test_the_api_profile_has_a_stored_ignore_a_changed_tools_line_and_a_change_on_the_changes_page(built):
    profile, inp = built("api")
    assert inp.skip == {rec.key for rec in _cards(inp, "hook-block-resent")} and inp.skip
    assert any((r.saving_usd or 0) > 0 for r in inp.recs if r.key in inp.skip)
    # The newest snapshot's reviewer line has lost Edit, which its spawns called.
    assert "Edit" not in recommend.read_agent_tools(inp.agents["reviewer"]["tools"]).allow
    rows = {row["agent_type"]: row for row in agreement.tables_of(inp.model)["agent_startup_diet"]}
    assert "Edit" in rows["reviewer"]["keep_tools"]
    reviewer = [r for r in _cards(inp, "spawn-tools-list") if r.agent_type == "reviewer"][0]
    assert "Edit" not in reviewer.changes[0].value
    points = change_points.change_points(inp.config_dir)
    assert any(c["key"] == "model" and c["old"] != c["new"] for p in points for c in p.changes)


@pytest.mark.parametrize("name", [n for n in NAMES if n != "api"])
def test_the_other_profiles_ignore_nothing(built, name):
    assert not built(name)[1].skip


def test_only_the_profile_with_capture_on_gets_the_cards_capture_makes(built):
    assert _cards(built("capture_on")[1], "effort-mismatch")
    for name in NAMES:
        if name != "capture_on":
            assert not _cards(built(name)[1], "effort-mismatch"), name


def test_the_cli_profile_has_a_status_line_log_and_the_others_do_not(built):
    for name in NAMES:
        profile, _inp = built(name)
        assert (profile.config_dir / "usage-log.csv").exists() == (name == "cli_status"), name


def test_claude_code_before_2_1_308_writes_no_request_shape_lines(built):
    for name in NAMES:
        profile, _inp = built(name)
        transcripts = list(Path(profile.projects_root).rglob("*.jsonl"))
        assert transcripts
        has = any("api-request-shape" in path.read_text(encoding="utf-8") for path in transcripts)
        assert has == (name != "old_version"), name


# -- the main model's card counts what the setting still decides (R11) -------------


def _main_row(inp) -> dict:
    return next(r for r in agreement.tables_of(inp.model)["model_swap_by_agent_type"] if r["agent_type"] == "top-level")


def test_the_two_project_profile_prices_only_the_project_still_on_opus(built):
    """The first project moved to Sonnet part-way and runs Sonnet from then:
    its earlier sessions ran under a model that has since changed, its later
    ones are already on Sonnet. The all-projects card prices the second
    project's sessions, as that project's own view does."""
    profile, inp = built("two_projects")
    row = _main_row(inp)
    assert row["lever_runs"] == 8
    alone = agreement.load_inputs(profile.projects_root, profile.config_dir, ap.SLUG_B)
    (alone_card,) = _cards(alone, "model-tier-main")
    (card,) = _cards(inp, "model-tier-main")
    assert card.saving_usd == pytest.approx(alone_card.saving_usd, rel=1e-9)
    assert (row["spawns"], row["before_change_runs"], row["settled_runs"]) == (16, 4, 4)
    assert card.changes[0].current == "opus"
    assert "It leaves out the 4 main sessions that started before the model last changed in their project." in card.why
    assert agreement.check_same_setting(inp) == []
    first = agreement.load_inputs(profile.projects_root, profile.config_dir, ap.SLUG)
    assert not _cards(first, "model-tier-main")


def test_a_project_that_moved_its_model_and_back_counts_from_the_move_back(tmp_path):
    spec = ap.Spec("and_back", custom_agents=False, workflows=False, second_project=True, model_back=True)
    profile = ap.build(spec, tmp_path / "p")
    inp = agreement.load_inputs(profile.projects_root, profile.config_dir, None)
    row = _main_row(inp)
    assert row["lever_runs"] == 11
    assert (row["spawns"], row["before_change_runs"], row["settled_runs"]) == (16, 5, 0)
    folders = [profile.projects_root / ap.SLUG, profile.projects_root / ap.SLUG_B]
    loaded = corpus.load_corpus(folders, since="2000-01-01")
    counted = [
        b.top for b in loaded.sessions
        if b.top is not None and (b.slug == ap.SLUG_B or b.top.meta.session_id in ("sess-005", "sess-006", "sess-007"))
    ]
    expected = model_swap.compute_model_swap(counted, load_pricing()).by_key["top-level"].tier_verdict.saving_usd
    assert row["saving_usd"] == pytest.approx(expected, rel=1e-9)
    (card,) = _cards(inp, "model-tier-main")
    assert card.saving_usd == pytest.approx(expected, rel=1e-9)
    assert agreement.check_same_setting(inp) == []
    # The first project alone: three sessions (about 138 replies) are under the bar.
    first = agreement.load_inputs(profile.projects_root, profile.config_dir, ap.SLUG)
    first_row = _main_row(first)
    assert (first_row["lever_runs"], first_row["before_change_runs"]) == (3, 5)
    assert not _cards(first, "model-tier-main")


def test_the_card_stays_when_the_newest_snapshot_is_the_project_already_on_sonnet(tmp_path):
    spec = ap.Spec("a_newest", custom_agents=False, workflows=False, second_project=True, a_newest=True)
    profile = ap.build(spec, tmp_path / "p")
    inp = agreement.load_inputs(profile.projects_root, profile.config_dir, None)
    (card,) = _cards(inp, "model-tier-main")
    assert card.changes[0].current == "opus"
    alone = agreement.load_inputs(profile.projects_root, profile.config_dir, ap.SLUG_B)
    (alone_card,) = _cards(alone, "model-tier-main")
    assert card.saving_usd == pytest.approx(alone_card.saving_usd, rel=1e-9)

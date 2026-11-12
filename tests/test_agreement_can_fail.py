"""Each agreement check (``agreement.py``) can fail.

A check that cannot fail is not a check. For each of the seven, one test
breaks one side on purpose (a monkeypatch of the product code that builds it,
or of the page's source) and shows the check reports it, where on the
unbroken profile it reported nothing. Most rebuild the profile's report with
the break in place (``agreement.load_inputs`` is the product's own report
path, so the break reaches the cards); each test first holds that the
unbroken profile agrees, so the report it makes is the break's doing.

Each break is a defect this branch had, or one like it: a tools card that
dropped a tool, a built-in card that named the wrong agents folder, a headline
that priced info cards (R7), a bundle that counted a standing rule as a file
(R6), a text that names an off hint (R1), a card whose current value is not
the Changes page's.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

import pytest

import agreement
import agreement_profiles as ap
from claudeglass import capture_catalogue, change_points, fixes, quick_actions, recommend

import test_service_static


@pytest.fixture(scope="module")
def profile(tmp_path_factory):
    return lambda name: ap.profile_inputs(name, tmp_path_factory)


def _rebuilt(profile, name):
    built, _inp = profile(name)
    return agreement.load_inputs(built.projects_root, built.config_dir, ap.SLUG)


def test_check_1_reports_a_tools_card_that_leaves_out_a_tool_the_agent_called(profile, monkeypatch):
    assert agreement.check_tools_lines(profile("capture_off")[1]) == []
    real = recommend.tools_list_offer

    def drops_read(*args, **kwargs):
        offer = real(*args, **kwargs)
        return None if offer is None else ([name for name in offer[0] if name != "Read"], offer[1], offer[2])

    monkeypatch.setattr(recommend, "tools_list_offer", drops_read)
    problems = agreement.check_tools_lines(_rebuilt(profile, "capture_off"))
    assert any("leaves out Read" in p and "spawn-tools-list:reviewer" in p for p in problems), problems


def test_check_1_does_not_ask_for_a_tool_the_agents_disallowed_tools_withhold(profile):
    """Edit clears the bar on the reviewer's spawns and is not on its card's line,
    because the reviewer's file withholds it: not a disagreement. The same agent
    with no deny line is one."""
    denied = profile("denied")[1]
    assert agreement.check_tools_lines(denied) == []
    card = [r for r in denied.recs if r.id == "spawn-tools-list" and r.agent_type == "reviewer"][0]
    assert "Edit" not in card.changes[0].value
    # The same report and cards, read as if the file did not say so.
    agents = {name: {k: v for k, v in entry.items() if k != "disallowedTools"} for name, entry in denied.agents.items()}
    bare = dataclasses.replace(denied, ctx=dataclasses.replace(denied.ctx, agents=agents))
    problems = agreement.check_tools_lines(bare)
    assert any("spawn-tools-list:reviewer" in p and "leaves out Edit" in p for p in problems), problems


def test_check_2_reports_a_card_that_names_a_different_agents_folder_than_its_fix(profile, monkeypatch):
    assert agreement.check_agent_folders(profile("capture_off")[1]) == []
    monkeypatch.setattr(
        recommend, "_built_in_note", lambda agent, scope: f"Create the file .claude/agents/{agent}.md for it."
    )
    problems = agreement.check_agent_folders(_rebuilt(profile, "capture_off"))
    assert any("general-purpose" in p and ".claude/agents/" in p and "~/.claude/agents/general-purpose.md" in p
               for p in problems), problems


def test_check_3_reports_a_headline_that_prices_an_info_card(profile, monkeypatch):
    inp = profile("api")[1]
    assert any(rec.severity == "info" and (rec.saving_usd or 0) > 0 for rec in inp.recs)
    assert agreement.check_totals(inp) == []
    guard = 'if (group.severity !== "action" && group.severity !== "advice") return;'
    real = test_service_static._static_text

    def without_the_guard(name):
        text = real(name)
        if name == "page-overview.js":
            assert guard in text
            text = text.replace(guard, "")
        return text

    monkeypatch.setattr(test_service_static, "_static_text", without_the_guard)
    problems = agreement.check_totals(inp)
    assert any("headline saving" in p and "spawn-claude-md" in p for p in problems), problems


def test_check_4_reports_a_tools_check_that_counts_other_tools_than_the_card(profile, monkeypatch):
    assert agreement.check_tools_figures(profile("capture_off")[1]) == []
    real = recommend.current_diet

    def one_fewer(diet, current, disallowed=None):
        row = dict(real(diet, current, disallowed))
        row["rare_tools"] = ", ".join(agreement._names(row.get("rare_tools"))[:-1])
        return row

    # Only the card's copy: the Tools check imported its own.
    monkeypatch.setattr(recommend, "current_diet", one_fewer)
    problems = agreement.check_tools_figures(_rebuilt(profile, "capture_off"))
    assert any("the Tools check counts" in p and "the card" in p for p in problems), problems


def test_check_4_reports_a_check_that_still_offers_the_fix_of_an_ignored_card(profile, monkeypatch):
    assert agreement.check_ignored_adds_nothing(profile("capture_off")[1]) == []
    # The cards each check reads, as they were before R9: ignored ones included.
    monkeypatch.setattr(
        quick_actions, "_recommendations",
        lambda ctx, ids: [rec for rec in getattr(ctx.model, "recommendations", ()) or () if rec.id in ids],
    )
    problems = agreement.check_ignored_adds_nothing(profile("capture_off")[1])
    assert "tools: still offers the fix of spawn-tools-list:reviewer with that card ignored" in problems


def test_check_4_reports_a_summary_that_still_names_an_ignored_card(profile, monkeypatch):
    inp = profile("capture_off")[1]
    assert agreement.check_ignored_adds_nothing(inp) == []
    real = quick_actions.run

    def names_the_card(check_id, ctx):
        result = real(check_id, ctx)
        for rec in ctx.model.recommendations:
            if rec.id in {"spawn-tools-list"} and check_id == "tools" and rec.key in ctx.skip_keys:
                result["summary"] += f" {rec.title}."
        return result

    monkeypatch.setattr(quick_actions, "run", names_the_card)
    problems = agreement.check_ignored_adds_nothing(inp.with_skip(inp.skip))
    assert any(p.startswith("tools: its summary still names spawn-tools-list:") for p in problems), problems


def test_check_5_reports_a_bundle_that_counts_a_standing_rule_as_a_file(profile, monkeypatch):
    inp = profile("capture_off")[1]
    assert agreement.check_bundle(inp) == []
    real = fixes.fix_group

    def rule_is_a_file(rec, change):
        header, kind = real(rec, change)
        return header, "file" if kind == "rule" else kind

    monkeypatch.setattr(fixes, "fix_group", rule_is_a_file)
    problems = agreement.check_bundle(inp)
    assert any("CLAUDE.md (Claude asks which)" in p and "one file" in p for p in problems), problems


def test_check_6_reports_a_text_that_names_a_hint_that_is_off(monkeypatch):
    texts = {"coaching_notes.what": capture_catalogue.METRICS_BY_ID["coaching_notes"].what}
    before = agreement.check_live_hints(texts)
    assert not any("drip_feed" in p for p in before)
    # Switch the hint the text names off: the text now names an off hint.
    monkeypatch.setattr(
        capture_catalogue, "LIVE_OFF_HINTS", (*capture_catalogue.LIVE_OFF_HINTS, "drip_feed")
    )
    assert "drip_feed" not in agreement.live_hints()
    after = agreement.check_live_hints(texts)
    assert any("drip_feed" in p and "not live" in p for p in after), after
    assert agreement.check_live_hints({"t": "Notes cover a large read."}, ["big_paste"]) == [
        "t names quiet_output ('large read'), which is not live"
    ]


def test_check_7_reports_a_card_whose_current_value_is_not_the_changes_pages(profile, monkeypatch):
    inp = profile("api")[1]
    assert agreement.check_same_setting(inp) == []
    point = change_points.ChangePoint(
        ts=datetime(2030, 1, 1, tzinfo=timezone.utc),
        source="config",
        label="settings",
        keys=["effective.model"],
        changes=[{"key": "model", "agent": None, "old": "opus", "new": "haiku"}],
    )
    monkeypatch.setattr(change_points, "change_points", lambda *args, **kwargs: [point])
    problems = agreement.check_same_setting(inp)
    assert any("model-tier-main" in p and "'opus'" in p and "'haiku'" in p for p in problems), problems


def test_check_7_reports_a_check_that_sets_what_a_card_sets_to_another_value(profile, monkeypatch):
    inp = profile("capture_on")[1]
    assert agreement.check_same_setting(inp) == []
    from claudeglass import quick_actions

    real = quick_actions.run

    def other_effort(check_id, ctx):
        result = real(check_id, ctx)
        if check_id == "effort":
            for fix in result["fixes"]:
                if fix.get("command") and "effortLevel=medium" in fix["command"]:
                    fix["command"] = fix["command"].replace("effortLevel=medium", "effortLevel=low")
        return result

    monkeypatch.setattr(quick_actions, "run", other_effort)
    problems = agreement.check_same_setting(inp.with_skip(inp.skip))
    assert any(p.startswith("effort sets effortLevel to effortLevel=low") for p in problems), problems


def _with_cards(inp, *recs):
    return inp.with_recommendations([*inp.recs, *recs])


def test_check_2_leaves_a_card_that_sets_a_standing_rule_free_to_name_the_agents_file(profile):
    """The run-split prompt adds an instruction to ~/.claude/CLAUDE.md and offers a line in
    the agent's own file when it has one: the card's fix is not one exact file."""
    from claudeglass.model import Recommendation

    inp = profile("capture_off")[1]
    assert agreement.check_agent_folders(inp) == []
    split = Recommendation(id="run-split", key="run-split:reviewer", severity="advice", agent_type="reviewer",
                           title="Give reviewer smaller tasks", why="Long runs.",
                           action="Give reviewer one part of a large task per run.", saving_usd=2.0)
    fixes.attach_fixes([split])
    assert ".claude/agents/reviewer.md" in split.fixes[0]["prompt"]
    assert fixes.fix_group(split, None)[1] != "file"
    assert agreement.check_agent_folders(_with_cards(inp, split)) == []


def test_check_2_still_reports_a_card_of_one_exact_file_that_names_the_other_agents_folder(profile):
    """A settings card (kind file) whose action sends the user to an agents folder."""
    from claudeglass.model import Recommendation

    inp = profile("capture_off")[1]
    card = Recommendation(id="ttl-switch", key="ttl-switch", severity="advice", title="Longer cache",
                          why="Pauses.", action="Set it in .claude/agents/reviewer.md instead.", saving_usd=2.0)
    fixes.attach_fixes([card])
    assert fixes.fix_group(card, None)[1] == "file"
    problems = agreement.check_agent_folders(_with_cards(inp, card))
    assert any("ttl-switch" in p and "action names .claude/agents/" in p and "settings.json" in p for p in problems), problems


def test_check_2_also_judges_a_card_whose_fix_spans_several_files(profile):
    """Only a card that sets a standing rule may name the agent's own file as a second place.
    One whose fix spans several files (here the MCP servers and plugins) works in those files."""
    from claudeglass.model import Recommendation

    inp = profile("capture_off")[1]
    card = Recommendation(id="baseline-bloat", key="baseline-bloat", severity="advice", title="Big start",
                          why="Tools.", action="Trim it in .claude/agents/reviewer.md instead.", saving_usd=2.0)
    fixes.attach_fixes([card])
    assert fixes.fix_group(card, None)[1] == "several"
    problems = agreement.check_agent_folders(_with_cards(inp, card))
    assert any("baseline-bloat" in p and "action names .claude/agents/" in p for p in problems), problems


def test_check_7_reports_a_check_that_says_no_data_while_an_open_card_for_its_rules_changes_a_setting(profile, monkeypatch):
    base = profile("capture_off")[1]
    # The profile's CLAUDE.md cards are information only; one of them as advice is an open card.
    advice = [dataclasses.replace(rec, severity="advice") if rec.key == "spawn-claude-md:general-purpose" else rec
              for rec in base.recs]
    inp = base.with_recommendations(advice)
    assert agreement.check_same_setting(inp) == []
    real = quick_actions.run

    def no_data(check_id, ctx):
        result = real(check_id, ctx)
        return {**result, "status": "no_data", "fixes": []} if check_id == "claude-md" else result

    monkeypatch.setattr(quick_actions, "run", no_data)
    problems = agreement.check_same_setting(inp.with_skip(inp.skip))
    assert any(p.startswith("claude-md says no_data but the open card spawn-claude-md:general-purpose changes omitClaudeMd")
               for p in problems), problems


def test_a_config_copy_kept_apart_from_the_projects_answers_all_seven_checks_as_one_beside_them(tmp_path):
    """The layout of docs/deploy.md: the config folder is a copy somewhere else, and the projects
    folder the report reads is given. The CLAUDE.md check reads where the report read, so it
    finds the same files and every check agrees, as on an install with the config beside."""
    import shutil

    built = ap.build(ap.SPECS["capture_off"], tmp_path / "install")
    (built.root / "CLAUDE.md").write_text("# Rules\n\n" + "Be brief and careful.\n" * 100, encoding="utf-8")
    beside = agreement.load_inputs(built.projects_root, built.config_dir, ap.SLUG)
    apart_config = tmp_path / "copy" / "claudeglass"
    shutil.copytree(built.config_dir, apart_config)
    apart = agreement.load_inputs(built.projects_root, apart_config, ap.SLUG, claude_root=built.root)
    assert apart.ctx.projects_roots == (built.projects_root,) and apart.ctx.claude_root == built.root
    # All seven run. Check 6 waits on R1 (the xfail in test_agreement.py): it finds the same thing both ways.
    found = agreement.run_checks(apart)
    assert list(found) == list(agreement.CHECKS)
    assert found == agreement.run_checks(beside)
    assert {number: problems for number, problems in found.items() if number != 6} == {
        number: [] for number in agreement.CHECKS if number != 6}
    for inp in (beside, apart):
        assert any(row[0].endswith("CLAUDE.md") for row in inp.result("claude-md")["table"]["rows"])
    assert apart.result("claude-md")["summary"] == beside.result("claude-md")["summary"]
    # Without the Claude folder given, the copy's parent is taken for it and the user file is not found.
    lost = agreement.load_inputs(built.projects_root, apart_config, ap.SLUG)
    assert not any(row[0].endswith("CLAUDE.md") for row in (lost.result("claude-md").get("table") or {"rows": []})["rows"])


def _run_split_and_plan_handoff():
    from claudeglass.model import Recommendation

    split = Recommendation(id="run-split", key="run-split:reviewer", severity="advice", agent_type="reviewer",
                           title="Give reviewer smaller tasks", why="Long runs.", action="Split it.", saving_usd=2.0)
    plan = Recommendation(id="plan-handoff", key="plan-handoff", severity="advice", title="Build from a fresh session",
                          why="Plans.", action="Clear after a plan.", saving_usd=3.0)
    fixes.attach_fixes([split, plan])
    return split, plan


def test_check_5_holds_when_run_split_and_plan_handoff_share_a_header_in_either_order(profile):
    inp = profile("capture_off")[1]
    split, plan = _run_split_and_plan_handoff()
    for extra in ([split, plan], [plan, split]):
        assert agreement.check_bundle(_with_cards(inp.with_recommendations(extra), *inp.recs)) == []


def test_check_5_reports_a_bundle_whose_counts_depend_on_the_order_of_the_cards(profile, monkeypatch):
    inp = profile("capture_off")[1]
    split, plan = _run_split_and_plan_handoff()
    both = _with_cards(inp.with_recommendations([]), split, plan)
    assert agreement.check_bundle(both) == []
    real = fixes.project_prompt

    def first_kind_wins(recs, skip_keys=frozenset()):
        data = real(recs, skip_keys)
        if recs and recs[0].id == "plan-handoff":
            data = {**data, "several": data["several"] - 1}
        return data

    monkeypatch.setattr(fixes, "project_prompt", first_kind_wins)
    problems = agreement.check_bundle(both)
    assert any("order" in p for p in problems), problems

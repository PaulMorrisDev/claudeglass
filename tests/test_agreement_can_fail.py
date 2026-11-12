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

from datetime import datetime, timezone

import pytest

import agreement
import agreement_profiles as ap
from claudeglass import capture_catalogue, change_points, fixes, recommend

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

    def one_fewer(diet, current):
        row = dict(real(diet, current))
        row["rare_tools"] = ", ".join(agreement._names(row.get("rare_tools"))[:-1])
        return row

    # Only the card's copy: the Tools check imported its own.
    monkeypatch.setattr(recommend, "current_diet", one_fewer)
    problems = agreement.check_tools_figures(_rebuilt(profile, "capture_off"))
    assert any("the Tools check counts" in p and "the card" in p for p in problems), problems


def test_check_4_reports_a_check_that_still_offers_the_fix_of_an_ignored_card(profile):
    """Not a patch: this is the product as it stands (R9), the second half of
    check 4 failing on the unbroken profile, in the named card and check."""
    problems = agreement.check_ignored_adds_nothing(profile("capture_off")[1])
    assert "tools: still offers the fix of spawn-tools-list:reviewer with that card ignored" in problems


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

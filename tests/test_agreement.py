"""Every agreement check (``agreement.py``) holds on every synthetic profile
(``agreement_profiles.py``).

A check returns the disagreements it found, so each test expects the empty
list. Two halves are expected to fail today, for reasons the plan already
names, and are marked ``xfail(strict=True)`` so that fixing the cause makes
the test fail until the mark is removed:

- check 6 (generated text names only live hints) waits on R1, the text
  pass: the catalogue's coaching-notes text still describes hints that are off;
- the second half of check 4 (an ignored card adds nothing to a check) waits
  on R9: ``quick_actions._recommendations`` hands back ignored cards, and most
  of its callers do not leave them out.
"""

from __future__ import annotations

import pytest

import agreement
import agreement_profiles as ap
from claudeglass import capture_catalogue, quick_actions

NAMES = list(ap.SPECS)


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    return lambda name: ap.profile_inputs(name, tmp_path_factory)[1]


def _disagreements(check, inp) -> list[str]:
    try:
        return check(inp)
    except agreement.CheckUnavailable as exc:
        pytest.skip(str(exc))


def test_the_seven_checks_are_numbered_as_the_plan_numbers_them():
    assert list(agreement.CHECKS) == [1, 2, 3, 4, 5, 6, 7]
    assert agreement.CHECKS[1][1] is agreement.check_tools_lines
    assert agreement.CHECKS[2][1] is agreement.check_agent_folders
    assert agreement.CHECKS[3][1] is agreement.check_totals
    assert agreement.CHECKS[4][1] is agreement.check_figures
    assert agreement.CHECKS[5][1] is agreement.check_bundle
    assert agreement.CHECKS[7][1] is agreement.check_same_setting


@pytest.mark.parametrize("name", NAMES)
def test_a_tools_card_keeps_every_tool_its_agent_called(built, name):
    assert _disagreements(agreement.check_tools_lines, built(name)) == []


@pytest.mark.parametrize("name", NAMES)
def test_a_card_names_the_agents_folder_its_fix_works_in(built, name):
    assert _disagreements(agreement.check_agent_folders, built(name)) == []


@pytest.mark.parametrize("name", NAMES)
def test_an_info_fixed_or_ignored_card_adds_nothing_to_a_total(built, name):
    assert _disagreements(agreement.check_totals, built(name)) == []


@pytest.mark.parametrize("name", NAMES)
def test_the_tools_check_counts_what_the_tools_card_counts(built, name):
    assert _disagreements(agreement.check_tools_figures, built(name)) == []


@pytest.mark.xfail(
    strict=True,
    reason="R9: quick_actions._recommendations returns ignored cards and most of its callers keep them, so the "
    "Tools, Hooks and Effort checks still offer the fix of a card that is ignored",
)
@pytest.mark.parametrize("name", NAMES)
def test_an_ignored_card_adds_no_fix_tip_or_mention_to_any_check(built, name):
    assert _disagreements(agreement.check_ignored_adds_nothing, built(name)) == []


@pytest.mark.parametrize("name", NAMES)
def test_the_bundle_counts_add_up(built, name):
    assert _disagreements(agreement.check_bundle, built(name)) == []


@pytest.mark.parametrize("name", NAMES)
def test_a_card_and_a_check_about_one_setting_give_the_same_advice(built, name):
    assert _disagreements(agreement.check_same_setting, built(name)) == []


@pytest.mark.xfail(
    strict=True,
    reason="R1: the coaching-notes text still describes hints that are switched off (a large read, an agent run "
    "that ended too big, a plan, a break, background agents), in the catalogue and in the generated docs/capture.md",
)
def test_generated_text_names_only_live_hints():
    assert agreement.check_live_hints() == []


def test_the_live_list_is_the_three_hints_that_speak():
    assert agreement.live_hints() == ["drip_feed", "big_paste", "status_poll"]


def test_every_hint_the_catalogue_knows_has_words_the_text_check_looks_for():
    """A hint added later is not one the check silently ignores."""
    for hint in capture_catalogue.COACHING_HINTS:
        assert agreement.hint_words(hint), f"agreement.HINT_WORDS has no words for {hint}"


def test_text_that_names_only_live_hints_agrees():
    texts = {"a": "Notes cover sending small requests one at a time, or a huge paste, and asking how background work is going."}
    assert agreement.check_live_hints(texts, ["drip_feed", "big_paste", "status_poll"]) == []


def test_every_check_is_run_by_run_checks_and_numbers_pick_which(built):
    inp = built("bare")
    only = agreement.run_checks(inp, numbers={2, 5})
    assert list(only) == [2, 5] and only == {2: [], 5: []}
    assert set(agreement.run_checks(inp, numbers={6})) == {6}


def test_the_checks_use_the_products_own_quick_actions_and_bundle():
    """Both sides come from the product: the checks name no rule of their own
    for a tools line, a folder or a bundle (a source read of the module)."""
    source = open(agreement.__file__, encoding="utf-8").read()
    for used in ("fixes.fix_file", "fixes.project_prompt", "fixes.fix_group", "quick_actions.run", "recommend.read_agent_tools"):
        assert used in source, used
    assert quick_actions.CHECKS

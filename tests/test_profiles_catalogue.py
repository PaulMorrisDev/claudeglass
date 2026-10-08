"""Tests for ``profiles/catalogue.py``: the eight shipped starting-point
profiles and :func:`suggest`'s deterministic archetype/purpose mapping.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from claudeglass.profiles.catalogue import (
    CATALOGUE_IDS,
    UNREACHABLE_BY_SUGGEST,
    get,
    list_profiles,
    suggest,
)
from claudeglass.profiles.schema import ARCHETYPES, dump_profile, loads_profile, validate

EXPECTED_IDS = (
    "interactive-chat",
    "discovery-scrape",
    "planning-requirements",
    "implementation-heavy",
    "overseer-fanout",
    "overnight-batch",
    "workflow-ultracode",
    "plan-then-build",
)


def test_catalogue_ids_are_the_seven_the_plan_names_then_plan_then_build():
    assert CATALOGUE_IDS == EXPECTED_IDS


def test_list_profiles_returns_all_eight_in_order():
    profiles = list_profiles()
    assert [p.id for p in profiles] == list(EXPECTED_IDS)


@pytest.mark.parametrize("profile_id", EXPECTED_IDS)
def test_every_catalogue_file_is_a_valid_profile(profile_id):
    """Loads each catalogue file through the real validator (not just
    ``load_profile``, which already validates -- this asserts zero
    problems explicitly, per the deliverable's own wording)."""
    profile = get(profile_id)
    assert profile is not None
    assert profile.id == profile_id
    # Re-parse the raw TOML and validate the dict directly too, so a
    # hand-edited catalogue file that somehow bypassed load_profile's own
    # validate() call (it can't, but this is the explicit check the
    # deliverable asks for) is still covered.
    raw = tomllib.loads(
        (Path(profile.source_path)).read_text(encoding="utf-8") if profile.source_path else ""
    )
    assert validate(raw) == []


@pytest.mark.parametrize("profile_id", EXPECTED_IDS)
def test_every_catalogue_profile_has_an_archetype_and_justification_notes(profile_id):
    profile = get(profile_id)
    assert profile.archetype in ARCHETYPES
    assert profile.notes.strip(), f"{profile_id} has no justification notes"
    assert profile.for_, f"{profile_id} has no 'for' task types"


@pytest.mark.parametrize("profile_id", EXPECTED_IDS)
def test_every_catalogue_profile_round_trips_through_dump_and_load(profile_id):
    original = get(profile_id)
    dumped = dump_profile(original)
    reloaded = loads_profile(dumped)
    assert reloaded == original


def test_get_returns_none_for_unknown_id():
    assert get("not-a-real-profile") is None


def test_overnight_batch_is_never_returned_by_suggest():
    """See catalogue.py's module docstring: suggest()'s signature has no
    session-mode input, and overnight-batch's justification is entirely
    mode-based, so it is unreachable through suggest() by design."""
    assert UNREACHABLE_BY_SUGGEST == "overnight-batch"
    seen = set()
    for archetype in (*ARCHETYPES, None):
        for purposes in (
            [],
            ["local-llm-pipeline"],
            ["workflow-run"],
            ["agent-fanout"],
            ["refactor"],
            ["test-triage"],
            ["review"],
            ["planning"],
            ["docs-or-light-edit"],
            ["general-dev"],
        ):
            seen.add(suggest(archetype, purposes))
    assert "overnight-batch" not in seen


# -- suggest(): at least six archetype/purpose combinations -----------------


@pytest.mark.parametrize(
    ("archetype", "purposes", "expected"),
    [
        ("chat-only", [], "interactive-chat"),
        ("single-model", ["local-llm-pipeline"], "discovery-scrape"),
        ("single-model", ["planning"], "planning-requirements"),
        ("plan-high-implement-low", [], "implementation-heavy"),
        ("plan-high-implement-low", ["refactor"], "implementation-heavy"),
        ("overseer-fanout", ["agent-fanout"], "overseer-fanout"),
        ("workflow-heavy", ["workflow-run"], "workflow-ultracode"),
        ("mixed", ["docs-or-light-edit"], "interactive-chat"),
        (None, [], "interactive-chat"),
    ],
)
def test_suggest_archetype_purpose_combinations(archetype, purposes, expected):
    assert suggest(archetype, purposes) == expected


def test_suggest_prefers_the_first_matching_purpose_in_list_order():
    # "local-llm-pipeline" appears before "planning" in the purposes list
    # -- suggest() must honour the caller's own dominant-purpose ordering
    # rather than some internal priority of its own.
    assert suggest("single-model", ["local-llm-pipeline", "planning"]) == "discovery-scrape"
    assert suggest("single-model", ["planning", "local-llm-pipeline"]) == "planning-requirements"


def test_suggest_falls_back_to_archetype_default_when_no_purpose_matches():
    assert suggest("overseer-fanout", ["some-unknown-purpose"]) == "overseer-fanout"


def test_suggest_is_deterministic():
    for _ in range(5):
        assert suggest("plan-high-implement-low", ["refactor", "test-triage"]) == "implementation-heavy"


# -- reported kinds of task (metrics capture) ------------------------------------------

from claudeglass import capture_catalogue  # noqa: E402
from claudeglass.profiles import catalogue as catalogue_mod  # noqa: E402


def test_for_words_normalise_to_the_task_vocabulary():
    vocab = set(capture_catalogue.TAG_VOCAB["task"])
    for tasks in catalogue_mod.FOR_TASKS.values():
        assert set(tasks) <= vocab


def test_tasks_for_normalises_catalogue_words_and_keeps_task_words():
    # F11: a catalogue word maps to its tasks, a saved task profile's
    # `for=[task]` stands for itself, and a way of running covers none.
    by_id = {p.id: p for p in catalogue_mod.list_profiles()}
    assert catalogue_mod.tasks_for(by_id["implementation-heavy"]) == (
        "feature", "bugfix", "debug", "refactor", "test", "review",
    )
    assert catalogue_mod.tasks_for(by_id["overnight-batch"]) == ()
    saved = loads_profile('id = "mine"\nfor = ["bugfix"]\n')
    assert catalogue_mod.tasks_for(saved) == ("bugfix",)
    vocab = set(capture_catalogue.TAG_VOCAB["task"])
    for profile in by_id.values():
        assert set(catalogue_mod.tasks_for(profile)) <= vocab


def test_every_task_has_a_catalogue_profile():
    # PROF-11/F11: "ops" used to be the one task word no catalogue
    # profile covered; workflow-ultracode's own "ops" for-word closes
    # that gap (see its notes for why that profile, of the catalogue).
    # plan-then-build's "plan-then-build" for-word is a way of working,
    # so it covers no task and changes nothing here.
    covered = {task: catalogue_mod.task_profile(task) for task in capture_catalogue.TAG_VOCAB["task"]}
    assert covered == {
        "feature": "implementation-heavy",
        "bugfix": "implementation-heavy",
        "refactor": "implementation-heavy",
        "debug": "implementation-heavy",
        "docs": "interactive-chat",
        "review": "implementation-heavy",
        "test": "implementation-heavy",
        "research": "discovery-scrape",
        "plan": "planning-requirements",
        "ops": "workflow-ultracode",
        "chat": "interactive-chat",
    }
    assert None not in covered.values()


def test_a_plan_then_build_shape_beats_the_tasks_but_not_a_structural_purpose():
    assert suggest("single-model", ["general-dev"], ["feature"], "plan-then-build") == "plan-then-build"
    assert suggest("single-model", ["general-dev"], (), "plan-then-build") == "plan-then-build"
    assert suggest("single-model", ["workflow-run"], ["feature"], "plan-then-build") == "workflow-ultracode"
    assert suggest("single-model", ["general-dev"], ["feature"], "not-a-shape") == "implementation-heavy"
    profile = catalogue_mod.get("plan-then-build")
    assert profile.archetype == "single-model" and catalogue_mod.tasks_for(profile) == ()
    assert "model" not in profile.settings and "/clear" in profile.notes


def test_tasks_for_reads_the_profiles_for_list():
    assert catalogue_mod.tasks_for(catalogue_mod.get("planning-requirements")) == ("plan",)
    assert catalogue_mod.tasks_for(catalogue_mod.get("overseer-fanout")) == ()


def test_a_reported_task_beats_a_guessed_purpose_but_not_a_structural_one():
    assert suggest("single-model", ["general-dev"], ["research"]) == "discovery-scrape"
    assert suggest("single-model", ["general-dev", "agent-fanout"], ["plan"]) == "overseer-fanout"
    # PROF-11/F11: ops now has its own catalogue profile, so it wins
    # over the guessed purpose, same as every other reported task.
    assert suggest("single-model", ["review"], ["ops"]) == "workflow-ultracode"
    # A task no profile covers (not even in the task vocabulary) falls
    # through to the next task, then the purposes.
    assert suggest("chat-only", [], ["not-a-real-task", "chat"]) == "interactive-chat"


def test_overnight_batch_notes_describe_the_unattended_night_mode_not_the_old_span_rule():
    """The profile is justified by the "overnight" mode, which now means
    Claude worked on its own at night while you were away. The old rule
    (a long span with a gap over an hour) is gone from its notes."""
    notes = get("overnight-batch").notes
    assert "two hours or more at night while you were away" in notes
    assert "30% of its working time was at night" in notes
    for old in ("span > 4 hours", "maximum human gap", "60 minutes"):
        assert old not in notes, old

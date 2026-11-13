"""Tests for ``profiles/roles.py``: which of the user's own agents is the one
that writes code, and how a profile's ``[roles.writer]`` table lands on it."""

from __future__ import annotations

from claudeglass.profiles import roles
from claudeglass.recommend import _CURRENT_UNKNOWN
from claudeglass.snapshots import NAME_CAP, NAME_CUT
from claudeglass.profiles.schema import load_dict

READ_ONLY = {"tools": "Read, Grep, Glob"}
CAN_EDIT = {"tools": "Read, Grep, Glob, Edit, Write, Bash"}


def _profile(**overrides):
    doc = {"id": "p", "settings": {"effortLevel": "medium"}}
    doc.update(overrides)
    return load_dict(doc)


def test_a_writer_is_found_by_its_name_and_tools():
    agents = {"implementer": CAN_EDIT, "researcher": READ_ONLY, "reviewer": READ_ONLY}
    assert roles.writers(agents) == ("implementer",)
    assert roles.is_writer("implementer", CAN_EDIT)
    assert not roles.is_writer("researcher", READ_ONLY)


def test_a_writer_named_agent_that_cannot_edit_is_not_a_writer():
    assert not roles.is_writer("implementer", READ_ONLY)
    # a tool on both lines is removed (disallowedTools is applied first)
    assert not roles.is_writer(
        "builder", {"tools": "Read, Edit, Write", "disallowedTools": "Edit, Write, MultiEdit, NotebookEdit"}
    )
    # one file tool is enough
    assert roles.is_writer("builder", {"tools": ["Read", "NotebookEdit"]})


def test_a_name_with_no_role_word_is_not_a_writer():
    assert not roles.is_writer("helper", CAN_EDIT)
    assert not roles.is_writer("fixture-maker", CAN_EDIT)  # "fixture" is not "fix"
    assert not roles.is_writer("general-purpose", CAN_EDIT)
    assert not roles.is_writer("workflow-subagent", CAN_EDIT)
    # a role word that decides is not a writer, even with Edit
    assert not roles.is_writer("code-reviewer", CAN_EDIT)
    assert roles.is_writer("claude-implementer", CAN_EDIT)


def test_a_name_with_a_second_role_word_of_another_class_is_not_a_writer():
    """The first role word used to win, so a reviewer or architect named
    after the thing it checks (``fix-verifier``) was moved to the writer's
    model. A writer word and no role word of another class."""
    for name in ("fix-verifier", "build-checker", "test-auditor", "implementer-reviewer", "plan-implementer"):
        assert not roles.is_writer(name, CAN_EDIT), name
    # the same word order the other way round
    assert not roles.is_writer("verifier-fixer", CAN_EDIT)
    # two writer words are still a writer
    for name in ("test-writer", "claude-implementer", "fix-builder", "test-runner", "docs-writer"):
        assert roles.is_writer(name, CAN_EDIT), name
    # a name the vocabulary has no word for is not caught by it
    assert roles.is_writer("test-architect", CAN_EDIT)


def test_an_integrator_that_can_edit_is_not_a_writer():
    assert not roles.is_writer("integrator", CAN_EDIT)
    assert not roles.is_writer("integration-tester", CAN_EDIT)


def test_a_cut_or_unknown_tools_line_still_counts_as_able_to_edit():
    assert roles.is_writer("implementer", None)
    assert roles.is_writer("implementer", {})
    assert roles.is_writer("implementer", {"tools": None})
    assert roles.is_writer("implementer", {"tools": ""})
    # a line the hook cut off is not a known line: every tool
    cut = ("Read, Grep, Glob, " + "a" * NAME_CAP)[: NAME_CAP - len(NAME_CUT)] + NAME_CUT
    assert roles.is_writer("implementer", {"tools": cut})
    assert roles.is_writer("implementer", {"tools": _CURRENT_UNKNOWN})


def test_explicit_agent_keys_win_over_the_role_key_by_key():
    profile = _profile(
        roles={"writer": {"model": "sonnet", "effort": "medium", "maxTurns": 60}},
        agents={"implementer": {"model": "haiku"}, "other": {"model": "opus"}},
    )
    out = roles.resolve(profile, {"implementer": CAN_EDIT})
    assert out.profile.agents["implementer"] == {"model": "haiku", "effort": "medium", "maxTurns": 60}
    # an agent the profile names that is not a writer stays as written
    assert out.profile.agents["other"] == {"model": "opus"}
    assert out.profile.roles == {}
    assert out.matched == {"writer": ("implementer",)}
    assert out.profile.settings == profile.settings
    assert out.profile.id == profile.id


def test_several_writers_each_get_the_role_keys():
    profile = _profile(roles={"writer": {"effort": "medium"}})
    agents = {"tester": CAN_EDIT, "implementer": CAN_EDIT, "researcher": READ_ONLY, "fixer": CAN_EDIT}
    out = roles.resolve(profile, agents)
    assert out.matched == {"writer": ("fixer", "implementer", "tester")}
    assert set(out.profile.agents) == {"fixer", "implementer", "tester"}
    assert all(v == {"effort": "medium"} for v in out.profile.agents.values())
    # each agent gets its own copy
    out.profile.agents["fixer"]["effort"] = "low"
    assert out.profile.agents["tester"] == {"effort": "medium"}
    assert profile.roles["writer"] == {"effort": "medium"}


def test_a_role_with_no_writer_adds_nothing():
    profile = _profile(roles={"writer": {"effort": "medium"}}, agents={"a": {"model": "haiku"}})
    out = roles.resolve(profile, {"researcher": READ_ONLY, "reviewer": READ_ONLY})
    assert out.profile.agents == {"a": {"model": "haiku"}}
    assert out.profile.roles == {}
    assert out.matched == {"writer": ()}
    assert roles.resolve(profile, {}).matched == {"writer": ()}


def test_a_profile_with_no_roles_comes_back_unchanged():
    profile = _profile(agents={"a": {"model": "haiku"}})
    out = roles.resolve(profile, {"implementer": CAN_EDIT})
    assert out.profile is profile
    assert out.matched == {}


def test_an_empty_role_table_matches_nothing_to_change():
    profile = _profile(roles={"writer": {}})
    out = roles.resolve(profile, {"implementer": CAN_EDIT})
    assert out.profile.agents == {}


def test_role_words_name_the_writer():
    assert roles.ROLE_WORDS == {"writer": "agent that writes code"}


def test_the_unusable_writers_note_says_how_to_rename_in_the_singular_and_the_plural():
    words = "one word of letters, digits, dots, underscores and hyphens"
    assert roles.unusable_writers_note(1) == (
        "One agent that writes code has a name ClaudeGlass can't use, so it is left out. "
        f"Rename it to {words} to include it."
    )
    assert roles.unusable_writers_note(3) == (
        "3 agents that write code have names ClaudeGlass can't use, so they are left out. "
        f"Rename each to {words} to include it."
    )

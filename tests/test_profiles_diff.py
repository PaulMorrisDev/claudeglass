"""Tests for ``profiles/diff.py``: ``diff_against_effective``,
``render_unified_diff`` (across all three scopes), managed-key exclusion,
and ``apply_command``'s privacy posture.
"""

from __future__ import annotations

import pytest

from helpers import assert_privacy

from claudeglass.profiles.diff import (
    DiffRow,
    ProfileDiff,
    apply_command,
    diff_against_effective,
    render_unified_diff,
)
from claudeglass.profiles.schema import load_dict


def _profile(**overrides):
    doc = {
        "id": "sample",
        "settings": {"effortLevel": "high", "autoCompactWindow": 150000},
        "agents": {"claude-implementer": {"effort": "medium", "experimental.cacheTtl": "5m"}},
        "env": {"CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL": "5m"},
    }
    doc.update(overrides)
    return load_dict(doc)


# --------------------------------------------------------------------
# diff_against_effective
# --------------------------------------------------------------------


def test_diff_lists_every_settings_key_the_profile_sets():
    profile = _profile()
    diff = diff_against_effective(profile, effective={}, effective_agents={}, provenance={}, managed_keys=set())
    keys = {row.key for row in diff.rows}
    assert "settings.effortLevel" in keys
    assert "settings.autoCompactWindow" in keys
    assert "agents.claude-implementer.effort" in keys
    assert "agents.claude-implementer.experimental.cacheTtl" in keys
    assert "env.CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL" in keys
    assert diff.profile_id == "sample"


def test_diff_reports_current_value_and_provenance_when_set():
    profile = _profile(settings={"effortLevel": "high"})
    diff = diff_against_effective(
        profile,
        effective={"effortLevel": "medium"},
        effective_agents={},
        provenance={"effortLevel": "user"},
        managed_keys=set(),
    )
    row = next(r for r in diff.rows if r.key == "settings.effortLevel")
    assert row.current_value == "medium"
    assert row.current_provenance == "user"
    assert row.proposed_value == "high"
    assert row.target_file == "user settings"
    assert row.managed is False


def test_diff_reports_unset_current_value():
    profile = _profile(settings={"effortLevel": "high"})
    diff = diff_against_effective(profile, effective={}, effective_agents={}, provenance={}, managed_keys=set())
    row = next(r for r in diff.rows if r.key == "settings.effortLevel")
    assert row.current_value is None
    assert row.current_provenance is None


def test_diff_rows_per_model_and_not_recorded_window():
    profile = _profile(
        settings={
            "modelSettings": {
                "claude-opus-5-5": {"effortLevel": "medium", "autoCompactWindow": 300_000},
                "claude-sonnet-4-6": {"effortLevel": "low"},
            }
        },
        agents={},
        env={},
    )
    diff = diff_against_effective(
        profile,
        # the hook keeps a model's effort and nothing else of its entry
        effective={"modelSettings": {"claude-opus-5-5": {"effortLevel": "high"}}},
        effective_agents={},
        provenance={"modelSettings": "project_local"},
        managed_keys=set(),
    )
    rows = {r.key: r for r in diff.rows}
    assert list(rows) == [
        "settings.modelSettings.claude-opus-5-5.effortLevel",
        "settings.modelSettings.claude-opus-5-5.autoCompactWindow",
        "settings.modelSettings.claude-sonnet-4-6.effortLevel",
    ]
    effort = rows["settings.modelSettings.claude-opus-5-5.effortLevel"]
    assert (effort.current_value, effort.proposed_value, effort.recorded) == ("high", "medium", True)
    assert effort.current_provenance == "project_local"
    assert effort.target_file == ".claude/settings.local.json"
    # the entry is there but carries no window: the snapshot does not say
    window = rows["settings.modelSettings.claude-opus-5-5.autoCompactWindow"]
    assert (window.current_value, window.proposed_value, window.recorded) == (None, 300_000, False)
    # a model with no entry at all has nothing set: that is known, not unrecorded
    other = rows["settings.modelSettings.claude-sonnet-4-6.effortLevel"]
    assert (other.current_value, other.proposed_value, other.recorded) == (None, "low", True)
    assert other.current_provenance is None and other.target_file == "user settings"
    # every other row is "recorded"
    plain = diff_against_effective(_profile(), {}, {}, {}, set())
    assert all(r.recorded for r in plain.rows)
    # a snapshot with no modelSettings at all gives no model rows, only these
    empty = diff_against_effective(profile, {}, {}, {}, set())
    assert len(empty.rows) == 3 and all(r.current_value is None for r in empty.rows)
    # the unified diff names each as modelSettings.<id>.<key>
    text = render_unified_diff(diff, scope="user")
    assert "-modelSettings.claude-opus-5-5.effortLevel: high\n+modelSettings.claude-opus-5-5.effortLevel: medium" in text
    assert "+modelSettings.claude-opus-5-5.autoCompactWindow: 300000" in text
    # managed
    locked = diff_against_effective(profile, {}, {}, {}, {"modelSettings"})
    assert all(r.managed for r in locked.rows)


def test_unified_diff_says_not_recorded_where_the_snapshot_does_not_keep_the_value():
    profile = _profile(
        settings={"modelSettings": {"claude-opus-5-5": {"effortLevel": "medium", "autoCompactWindow": 400_000}}},
        agents={},
        env={},
    )
    diff = diff_against_effective(
        profile, {"modelSettings": {"claude-opus-5-5": {"effortLevel": None}}}, {}, {}, set()
    )
    text = render_unified_diff(diff, scope="user")
    assert "-modelSettings.claude-opus-5-5.autoCompactWindow: not recorded\n" in text
    assert "+modelSettings.claude-opus-5-5.autoCompactWindow: 400000" in text
    # a recorded value that is unset still reads (unset)
    assert "-modelSettings.claude-opus-5-5.effortLevel: (unset)\n" in text


@pytest.mark.parametrize(
    ("provenance_layer", "expected_target_file"),
    [
        ("managed", "managed-settings.json"),
        ("project_local", ".claude/settings.local.json"),
        ("project_shared", ".claude/settings.json"),
        ("user", "user settings"),
    ],
)
def test_diff_target_file_follows_provenance_layer(provenance_layer, expected_target_file):
    profile = _profile(settings={"effortLevel": "high"})
    diff = diff_against_effective(
        profile,
        effective={"effortLevel": "medium"},
        effective_agents={},
        provenance={"effortLevel": provenance_layer},
        managed_keys=set(),
    )
    row = next(r for r in diff.rows if r.key == "settings.effortLevel")
    assert row.target_file == expected_target_file


def test_diff_reads_agent_current_value_from_effective_agents_field_map():
    profile = _profile(agents={"claude-implementer": {"maxTurns": 60, "experimental.cacheTtl": "1h"}})
    diff = diff_against_effective(
        profile,
        effective={},
        effective_agents={
            "claude-implementer": {"source": "project", "experimental_cache_ttl": "5m", "model": "sonnet"}
        },
        provenance={},
        managed_keys=set(),
    )
    ttl_row = next(r for r in diff.rows if r.key == "agents.claude-implementer.experimental.cacheTtl")
    assert ttl_row.current_value == "5m"
    assert ttl_row.current_provenance == "project"
    assert ttl_row.proposed_value == "1h"
    assert ttl_row.target_file == ".claude/agents/claude-implementer.md"

    # maxTurns has no counterpart in effective_agents' reduced field set
    # (docs/config-layers.md: {source, experimental_cache_ttl, model,
    # effort, max_turns} -- maxTurns *is* max_turns, but this profile's
    # effective_agents fixture above doesn't supply it) -- unset, not a
    # crash or a fabricated value.
    max_turns_row = next(r for r in diff.rows if r.key == "agents.claude-implementer.maxTurns")
    assert max_turns_row.current_value is None


def test_diff_agent_key_with_no_effective_agents_field_stays_unset():
    profile = _profile(agents={"claude-implementer": {"omitClaudeMd": True}})
    diff = diff_against_effective(
        profile,
        effective={},
        effective_agents={"claude-implementer": {"model": "sonnet"}},
        provenance={},
        managed_keys=set(),
    )
    row = next(r for r in diff.rows if r.key == "agents.claude-implementer.omitClaudeMd")
    assert row.current_value is None
    assert row.current_provenance is None


def test_diff_env_rows_have_no_current_value():
    profile = _profile(env={"MAX_THINKING_TOKENS": "8192"})
    diff = diff_against_effective(profile, effective={}, effective_agents={}, provenance={}, managed_keys=set())
    row = next(r for r in diff.rows if r.key == "env.MAX_THINKING_TOKENS")
    assert row.current_value is None
    assert row.proposed_value == "8192"
    assert row.target_file == "environment (names only; set the value yourself)"


def test_diff_marks_settings_key_managed():
    profile = _profile(settings={"effortLevel": "high"})
    diff = diff_against_effective(
        profile, effective={}, effective_agents={}, provenance={}, managed_keys={"effortLevel"}
    )
    row = next(r for r in diff.rows if r.key == "settings.effortLevel")
    assert row.managed is True


def test_diff_marks_agent_key_managed_via_blanket_agents_key():
    profile = _profile(agents={"claude-implementer": {"effort": "medium"}})
    diff = diff_against_effective(
        profile, effective={}, effective_agents={}, provenance={}, managed_keys={"agents"}
    )
    row = next(r for r in diff.rows if r.key == "agents.claude-implementer.effort")
    assert row.managed is True


def test_diff_marks_experimental_cache_ttl_managed_via_subagent_prompt_cache_ttl():
    profile = _profile(agents={"claude-implementer": {"experimental.cacheTtl": "1h"}})
    diff = diff_against_effective(
        profile,
        effective={},
        effective_agents={},
        provenance={},
        managed_keys={"subagentPromptCacheTtl"},
    )
    row = next(r for r in diff.rows if r.key == "agents.claude-implementer.experimental.cacheTtl")
    assert row.managed is True


# --------------------------------------------------------------------
# render_unified_diff: three scopes
# --------------------------------------------------------------------


def _basic_diff():
    profile = _profile(
        settings={"effortLevel": "high"},
        agents={"claude-implementer": {"experimental.cacheTtl": "1h"}},
        env={"MAX_THINKING_TOKENS": "8192"},
    )
    return diff_against_effective(
        profile,
        effective={"effortLevel": "medium"},
        effective_agents={"claude-implementer": {"experimental_cache_ttl": "5m"}},
        provenance={"effortLevel": "user"},
        managed_keys=set(),
    )


@pytest.mark.parametrize(
    ("scope", "expected_settings_file"),
    [
        ("user", "user settings"),
        ("project-local", ".claude/settings.local.json"),
        ("repo", ".claude/settings.json"),
    ],
)
def test_render_unified_diff_settings_file_header_follows_scope(scope, expected_settings_file):
    diff = _basic_diff()
    text = render_unified_diff(diff, scope=scope)
    assert f"--- {expected_settings_file}" in text
    assert f"+++ {expected_settings_file}" in text
    assert "-effortLevel: medium" in text
    assert "+effortLevel: high" in text


def test_render_unified_diff_agent_frontmatter_ignores_scope():
    for scope in ("user", "project-local", "repo"):
        text = render_unified_diff(_basic_diff(), scope=scope)
        assert "--- .claude/agents/claude-implementer.md" in text
        assert "-experimental.cacheTtl: 5m" in text
        assert "+experimental.cacheTtl: 1h" in text


def test_render_unified_diff_env_row_has_no_removal_line():
    text = render_unified_diff(_basic_diff(), scope="user")
    assert "--- environment (names only; set the value yourself)" in text
    assert "+MAX_THINKING_TOKENS=8192" in text
    assert "-MAX_THINKING_TOKENS" not in text


def test_render_unified_diff_rejects_unknown_scope():
    with pytest.raises(ValueError):
        render_unified_diff(_basic_diff(), scope="not-a-scope")


def test_render_unified_diff_is_byte_stable_across_calls():
    diff = _basic_diff()
    assert render_unified_diff(diff, scope="user") == render_unified_diff(diff, scope="user")


def test_render_unified_diff_sorts_multiple_agents_and_keys():
    profile = _profile(
        settings={},
        agents={
            "zzz-agent": {"effort": "high"},
            "aaa-agent": {"effort": "low"},
        },
        env={},
    )
    diff = diff_against_effective(profile, effective={}, effective_agents={}, provenance={}, managed_keys=set())
    text = render_unified_diff(diff, scope="user")
    aaa_index = text.index("aaa-agent")
    zzz_index = text.index("zzz-agent")
    assert aaa_index < zzz_index


# --------------------------------------------------------------------
# managed-key exclusion
# --------------------------------------------------------------------


def test_render_unified_diff_excludes_managed_settings_key_from_hunk_body():
    profile = _profile(settings={"effortLevel": "high", "autoCompactWindow": 150000})
    diff = diff_against_effective(
        profile,
        effective={"effortLevel": "medium", "autoCompactWindow": 100000},
        effective_agents={},
        provenance={},
        managed_keys={"effortLevel"},
    )
    text = render_unified_diff(diff, scope="user")
    # The managed key must never appear as a +/- diff line ...
    assert "-effortLevel:" not in text
    assert "+effortLevel:" not in text
    # ... but is still visible as an explicit note.
    assert "settings.effortLevel: managed by policy, raise with your administrator" in text
    # The non-managed key in the same profile still renders normally.
    assert "-autoCompactWindow: 100000" in text
    assert "+autoCompactWindow: 150000" in text


def test_render_unified_diff_managed_only_profile_has_no_hunks_but_has_notes():
    profile = _profile(settings={"effortLevel": "high"}, agents={}, env={})
    diff = diff_against_effective(
        profile,
        effective={"effortLevel": "medium"},
        effective_agents={},
        provenance={},
        managed_keys={"effortLevel"},
    )
    text = render_unified_diff(diff, scope="user")
    assert "--- user settings" not in text
    assert "managed by policy" in text


def test_render_unified_diff_drops_unchanged_rows_entirely():
    profile = _profile(settings={"effortLevel": "high"}, agents={}, env={})
    diff = diff_against_effective(
        profile,
        effective={"effortLevel": "high"},  # already matches -- no-op
        effective_agents={},
        provenance={},
        managed_keys=set(),
    )
    text = render_unified_diff(diff, scope="user")
    assert text == ""
    # But the row itself is still present in the full ProfileDiff.
    assert any(r.key == "settings.effortLevel" for r in diff.rows)


# --------------------------------------------------------------------
# apply_command
# --------------------------------------------------------------------


def test_apply_command_user_scope_has_no_project_flag():
    text = apply_command("interactive-chat", "user")
    apply_line, launch_line = text.splitlines()
    assert apply_line == "claudeglass apply interactive-chat"
    # The overlay file doesn't exist until apply --launch writes it, so
    # the one-session command is that one, not claude --settings.
    assert launch_line == "claudeglass apply interactive-chat --launch"


def test_apply_command_project_scope_without_path_names_the_current_folder():
    # apply refuses a project scope without --project-dir, so the command
    # names the folder it is run from rather than leave the flag out.
    for scope in ("project-local", "repo"):
        apply_line = apply_command("interactive-chat", scope).splitlines()[0]
        assert "--project-dir . " in apply_line + " "


def test_apply_command_project_local_with_path_prints_it_verbatim():
    text = apply_command("interactive-chat", "project-local", project_path="C:\\Dev\\SomeProject")
    apply_line = text.splitlines()[0]
    assert "--project-dir C:\\Dev\\SomeProject" in apply_line


def test_apply_command_repo_scope_adds_allow_tracked():
    text = apply_command("interactive-chat", "repo", project_path="/home/dev/project")
    apply_line = text.splitlines()[0]
    assert "--allow-tracked" in apply_line
    assert "--project-dir /home/dev/project" in apply_line


def test_apply_command_names_every_scope_but_user():
    """``apply`` falls back to user scope without --project-dir, so a
    project scope must be named or the command writes user settings."""
    assert "--scope" not in apply_command("interactive-chat", "user").splitlines()[0]
    assert "--scope project-local" in apply_command("interactive-chat", "project-local").splitlines()[0]
    repo = apply_command("interactive-chat", "repo", project_path="/p").splitlines()[0]
    assert "--scope repo" in repo and "--allow-tracked" in repo


def test_apply_command_rejects_unknown_scope():
    with pytest.raises(ValueError):
        apply_command("interactive-chat", "not-a-scope")


# --------------------------------------------------------------------
# privacy: no absolute paths unless project_path was given
# --------------------------------------------------------------------


def test_apply_command_without_project_path_is_privacy_clean():
    for scope in ("user", "project-local", "repo"):
        text = apply_command("interactive-chat", scope)
        assert_privacy({"command": text})


def test_apply_command_with_project_path_prints_only_that_path():
    project_path = "C:\\Dev\\SomeProject"
    text = apply_command("interactive-chat", "project-local", project_path=project_path)
    assert project_path in text
    # Strip out the one deliberately-included path, then the remainder
    # must still be privacy-clean -- no *other* leak rode in alongside it.
    scrubbed = text.replace(project_path, "<project-path>")
    assert_privacy({"command": scrubbed})


def test_render_unified_diff_is_privacy_clean():
    diff = _basic_diff()
    for scope in ("user", "project-local", "repo"):
        assert_privacy({"diff": render_unified_diff(diff, scope=scope)})


def test_diff_against_effective_rows_are_privacy_clean():
    diff = _basic_diff()
    for row in diff.rows:
        assert_privacy(
            {
                "key": row.key,
                "target_file": row.target_file,
                "current_provenance": row.current_provenance,
            }
        )

"""Tests for ``profiles/apply.py``: ``plan_apply``, ``execute``,
``revert``, ``list_backups`` and ``write_launch_overlay``. COV-07/
COV-11: a profile's ``env`` entries are written into the target
settings file's own ``"env"`` object, same as any other settings key
(see ``plan_apply``'s tests below) -- there is no more separate
env-lines-only code path.

Every test builds its own ``home``/``project``/``config_dir`` under
``tmp_path`` -- this module is the one place in the package that writes
real files outside a fixture directory, so nothing here should ever
touch the machine's actual ``~/.claude``.
"""

from __future__ import annotations

import difflib
import json
import subprocess
from pathlib import Path

import pytest

from helpers import assert_privacy

from claudeglass.profiles import apply as apply_mod
from claudeglass.profiles.frontmatter import parse_frontmatter
from claudeglass.profiles.schema import load_dict
from claudeglass.snapshots import Snapshot


def _profile(**overrides):
    doc = {"id": "sample", "settings": {"effortLevel": "high"}}
    doc.update(overrides)
    return load_dict(doc)


def _snapshot(**data):
    return Snapshot(path=Path("snap.json"), ts="20260101T000000Z", data=data)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _init_git_repo_with_file(project: Path, rel_path: str, content: str) -> None:
    """A minimal git repo whose ``rel_path`` is already committed --
    used by the tracked-file refusal tests."""
    project.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", cwd=project)
    target = project / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _git("add", rel_path, cwd=project)
    _git("-c", "user.email=a@example.com", "-c", "user.name=test", "commit", "-q", "-m", "init", cwd=project)


# --------------------------------------------------------------------
# plan_apply: scope resolution
# --------------------------------------------------------------------


def test_plan_apply_user_scope_targets_home_settings(tmp_path):
    home = tmp_path / "home"
    claude_root = home / ".claude"
    config_dir = home / ".claude" / "claudeglass"
    plan = apply_mod.plan_apply(_profile(), scope="user", project_path=None, config_dir=config_dir, claude_root=claude_root)
    settings_action = next(a for a in plan.actions if a.kind == "settings")
    assert settings_action.path == claude_root / "settings.json"


def test_plan_apply_project_local_scope_targets_settings_local(tmp_path):
    home = tmp_path / "home"
    claude_root = home / ".claude"
    project = tmp_path / "proj"
    config_dir = home / ".claude" / "claudeglass"
    plan = apply_mod.plan_apply(
        _profile(), scope="project-local", project_path=project, config_dir=config_dir, claude_root=claude_root
    )
    settings_action = next(a for a in plan.actions if a.kind == "settings")
    assert settings_action.path == project / ".claude" / "settings.local.json"


def test_plan_apply_repo_scope_targets_shared_settings(tmp_path):
    home = tmp_path / "home"
    claude_root = home / ".claude"
    project = tmp_path / "proj"
    config_dir = home / ".claude" / "claudeglass"
    plan = apply_mod.plan_apply(_profile(), scope="repo", project_path=project, config_dir=config_dir, claude_root=claude_root)
    settings_action = next(a for a in plan.actions if a.kind == "settings")
    assert settings_action.path == project / ".claude" / "settings.json"


def test_plan_apply_rejects_unknown_scope(tmp_path):
    home = tmp_path / "home"
    claude_root = home / ".claude"
    config_dir = home / ".claude" / "claudeglass"
    with pytest.raises(ValueError):
        apply_mod.plan_apply(_profile(), scope="not-a-scope", project_path=None, config_dir=config_dir, claude_root=claude_root)


def test_plan_apply_project_scope_without_project_path_raises(tmp_path):
    home = tmp_path / "home"
    claude_root = home / ".claude"
    config_dir = home / ".claude" / "claudeglass"
    with pytest.raises(ValueError):
        apply_mod.plan_apply(_profile(), scope="repo", project_path=None, config_dir=config_dir, claude_root=claude_root)


# --------------------------------------------------------------------
# dry-run diff (fix B4): rendered from the real target files, not the
# snapshot -- see apply.render_plan_diff and the module docstring's B4
# note for the bug this replaced (a stale/absent snapshot rendered the
# file's real current value as "(unset)", and the subsequent real apply
# then silently overwrote it).
# --------------------------------------------------------------------


def test_plan_apply_diff_text_reflects_the_real_file_not_a_stale_snapshot(tmp_path):
    """Reproduces the review's exact repro: an agent file already has
    ``model: opus`` on disk, but the snapshot handed to plan_apply
    claims nothing about this agent at all (an empty
    ``effective_agents`` -- the stale/absent case B4 was about). The
    diff must show the real current value, never "(unset)"."""
    home = tmp_path / "home"
    claude_root = home / ".claude"
    config_dir = home / ".claude" / "claudeglass"
    agents_dir = claude_root / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "reviewer.md").write_text(
        "---\nname: reviewer\nmodel: opus   # deliberate\n---\n\nBody.\n", encoding="utf-8"
    )
    profile = _profile(settings={}, agents={"reviewer": {"model": "sonnet"}})
    snapshot = _snapshot(effective_agents={})

    plan = apply_mod.plan_apply(
        profile, scope="user", project_path=None, config_dir=config_dir, claude_root=claude_root, snapshot=snapshot
    )

    assert "(unset)" not in plan.diff_text
    assert "-model: opus   # deliberate" in plan.diff_text
    assert "+model: sonnet   # deliberate" in plan.diff_text


def test_plan_apply_diff_text_equals_the_diff_of_a_real_apply(tmp_path):
    """The regression case the review asked for directly: the dry-run
    diff text must equal the unified diff of the target file's actual
    before/after content -- not a computation derived from anything
    else -- when a real (non-dry-run) apply is executed against the
    exact same starting state."""
    home = tmp_path / "home"
    claude_root = home / ".claude"
    config_dir = home / ".claude" / "claudeglass"
    claude_root.mkdir(parents=True)
    settings_path = claude_root / "settings.json"
    settings_path.write_text('{\n  "effortLevel": "medium"\n}\n', encoding="utf-8")

    profile = _profile(settings={"effortLevel": "high"})
    plan = apply_mod.plan_apply(
        profile, scope="user", project_path=None, config_dir=config_dir, claude_root=claude_root
    )
    before = settings_path.read_bytes()
    apply_mod.execute(plan, config_dir=config_dir)
    after = settings_path.read_bytes()
    assert before != after

    expected_lines = [
        line
        for line in difflib.unified_diff(
            before.decode("utf-8").splitlines(),
            after.decode("utf-8").splitlines(),
            fromfile="settings.json",
            tofile="settings.json",
            lineterm="",
        )
        if not line.startswith("@@")
    ]
    assert "\n".join(expected_lines) in plan.diff_text


def test_plan_apply_diff_text_is_privacy_clean_without_project_path(tmp_path):
    home = tmp_path / "home"
    claude_root = home / ".claude"
    config_dir = home / ".claude" / "claudeglass"
    profile = _profile(settings={"effortLevel": "high"}, agents={"reviewer": {"effort": "high"}})
    for scope in ("user", "project-local", "repo"):
        project_path = tmp_path / "proj" if scope != "user" else None
        plan = apply_mod.plan_apply(
            profile, scope=scope, project_path=project_path, config_dir=config_dir, claude_root=claude_root
        )
        assert_privacy({"diff": plan.diff_text})


# --------------------------------------------------------------------
# apply -> revert round trip
# --------------------------------------------------------------------


def test_apply_then_revert_restores_byte_identical_content(tmp_path):
    home = tmp_path / "home"
    claude_root = home / ".claude"
    project = tmp_path / "proj"
    config_dir = home / ".claude" / "claudeglass"
    agents_dir = project / ".claude" / "agents"
    agents_dir.mkdir(parents=True)

    settings_path = project / ".claude" / "settings.local.json"
    settings_path.write_text('{\n  "outputStyle": "concise"\n}\n', encoding="utf-8")
    agent_path = agents_dir / "reviewer.md"
    original_agent_text = (
        "---\n"
        "model: opus\n"
        "effort: low  # keep this reviewer careful\n"
        "---\n\n"
        "# Reviewer\n\nBody text unrelated to any lever.\n"
    )
    agent_path.write_text(original_agent_text, encoding="utf-8")
    original_settings_bytes = settings_path.read_bytes()
    original_agent_bytes = agent_path.read_bytes()

    profile = _profile(
        settings={"effortLevel": "high"}, agents={"reviewer": {"effort": "high"}}
    )
    plan = apply_mod.plan_apply(
        profile, scope="project-local", project_path=project, config_dir=config_dir, claude_root=claude_root
    )
    assert not plan.blocked
    result = apply_mod.execute(plan, config_dir=config_dir)

    # Content actually changed, and unrelated content survived.
    new_settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert new_settings["outputStyle"] == "concise"
    assert new_settings["effortLevel"] == "high"
    new_agent_text = agent_path.read_text(encoding="utf-8")
    assert "model: opus" in new_agent_text
    assert "# keep this reviewer careful" in new_agent_text
    assert "Body text unrelated to any lever." in new_agent_text
    assert parse_frontmatter(new_agent_text)["effort"] == "high"
    assert settings_path.read_bytes() != original_settings_bytes
    assert agent_path.read_bytes() != original_agent_bytes

    revert_result = apply_mod.revert(result.ts, config_dir=config_dir)
    assert settings_path in revert_result.restored
    assert agent_path in revert_result.restored
    assert settings_path.read_bytes() == original_settings_bytes
    assert agent_path.read_bytes() == original_agent_bytes


def test_apply_then_revert_deletes_files_that_did_not_exist_before(tmp_path):
    home = tmp_path / "home"
    claude_root = home / ".claude"
    config_dir = home / ".claude" / "claudeglass"

    profile = _profile(settings={"effortLevel": "high"})
    plan = apply_mod.plan_apply(profile, scope="user", project_path=None, config_dir=config_dir, claude_root=claude_root)
    result = apply_mod.execute(plan, config_dir=config_dir)

    settings_path = claude_root / "settings.json"
    assert settings_path.exists()
    active_path = config_dir / "active-profile"
    assert active_path.read_text(encoding="utf-8").strip() == "sample"

    revert_result = apply_mod.revert(result.ts, config_dir=config_dir)
    assert settings_path in revert_result.deleted
    assert active_path in revert_result.deleted
    assert not settings_path.exists()
    assert not active_path.exists()


def test_revert_raises_for_unknown_timestamp(tmp_path):
    config_dir = tmp_path / "home" / ".claude" / "claudeglass"
    with pytest.raises(apply_mod.ApplyError):
        apply_mod.revert("20200101T000000Z", config_dir=config_dir)


def test_execute_raises_and_writes_nothing_when_plan_is_blocked(tmp_path):
    home = tmp_path / "home"
    claude_root = home / ".claude"
    project = tmp_path / "proj"
    config_dir = home / ".claude" / "claudeglass"
    _init_git_repo_with_file(project, ".claude/settings.json", "{}\n")

    profile = _profile(settings={"effortLevel": "high"})
    plan = apply_mod.plan_apply(profile, scope="repo", project_path=project, config_dir=config_dir, claude_root=claude_root)
    assert plan.blocked
    with pytest.raises(apply_mod.ApplyError) as excinfo:
        apply_mod.execute(plan, config_dir=config_dir)
    assert list(excinfo.value.reasons) == list(plan.blocked)
    assert not (config_dir / "backups").exists()


# --------------------------------------------------------------------
# git-tracked file refusal + --allow-tracked
# --------------------------------------------------------------------


def test_plan_apply_blocks_tracked_settings_file(tmp_path):
    home = tmp_path / "home"
    claude_root = home / ".claude"
    project = tmp_path / "proj"
    config_dir = home / ".claude" / "claudeglass"
    _init_git_repo_with_file(project, ".claude/settings.json", "{}\n")

    profile = _profile(settings={"effortLevel": "high"})
    plan = apply_mod.plan_apply(profile, scope="repo", project_path=project, config_dir=config_dir, claude_root=claude_root)
    assert any("tracked by git" in reason for reason in plan.blocked)


def test_allow_tracked_permits_writing_a_tracked_settings_file(tmp_path):
    home = tmp_path / "home"
    claude_root = home / ".claude"
    project = tmp_path / "proj"
    config_dir = home / ".claude" / "claudeglass"
    _init_git_repo_with_file(project, ".claude/settings.json", "{}\n")

    profile = _profile(settings={"effortLevel": "high"})
    plan = apply_mod.plan_apply(
        profile, scope="repo", project_path=project, config_dir=config_dir, claude_root=claude_root, allow_tracked=True
    )
    assert not plan.blocked
    result = apply_mod.execute(plan, config_dir=config_dir)
    assert (project / ".claude" / "settings.json") in result.written


def test_plan_apply_blocks_tracked_settings_file_at_user_scope(tmp_path):
    """Fix S7: the tracked-file refusal used to be checked only when
    ``project_path is not None``, so a user-scope ``~/.claude`` kept in
    a dotfiles repository (common) was written without
    ``--allow-tracked`` -- this is the same refusal as the project-scope
    test above, exercised with ``claude_root`` itself as the git repo
    (mirroring a real dotfiles layout: ``settings.json`` committed
    directly under the tracked Claude root, no project involved)."""
    home = tmp_path / "home"
    claude_root = home / ".claude"
    config_dir = home / ".claude" / "claudeglass"
    _init_git_repo_with_file(claude_root, "settings.json", "{}\n")

    profile = _profile(settings={"effortLevel": "high"})
    plan = apply_mod.plan_apply(
        profile, scope="user", project_path=None, config_dir=config_dir, claude_root=claude_root
    )
    assert any("tracked by git" in reason for reason in plan.blocked)

    plan_allowed = apply_mod.plan_apply(
        profile,
        scope="user",
        project_path=None,
        config_dir=config_dir,
        claude_root=claude_root,
        allow_tracked=True,
    )
    assert not plan_allowed.blocked
    result = apply_mod.execute(plan_allowed, config_dir=config_dir)
    assert (claude_root / "settings.json") in result.written


def test_plan_apply_blocks_tracked_agent_file(tmp_path):
    home = tmp_path / "home"
    claude_root = home / ".claude"
    project = tmp_path / "proj"
    config_dir = home / ".claude" / "claudeglass"
    _init_git_repo_with_file(
        project, ".claude/agents/reviewer.md", "---\neffort: low\n---\n\nBody.\n"
    )

    profile = _profile(settings={}, agents={"reviewer": {"effort": "high"}})
    plan = apply_mod.plan_apply(
        profile, scope="project-local", project_path=project, config_dir=config_dir, claude_root=claude_root
    )
    assert any("tracked by git" in reason for reason in plan.blocked)

    plan_allowed = apply_mod.plan_apply(
        profile,
        scope="project-local",
        project_path=project,
        config_dir=config_dir,
        claude_root=claude_root,
        allow_tracked=True,
    )
    assert not plan_allowed.blocked
    apply_mod.execute(plan_allowed, config_dir=config_dir)
    assert parse_frontmatter((project / ".claude" / "agents" / "reviewer.md").read_text())["effort"] == "high"


def test_a_role_writer_file_that_git_tracks_is_refused_without_allow_tracked(tmp_path):
    """A role is never refused for a missing agent, but the writer file it
    reaches is an agent file like any other: git-tracked, it is refused
    without --allow-tracked (a project agent is usually committed)."""
    from claudeglass.profiles import catalogue

    home = tmp_path / "home"
    project = tmp_path / "proj"
    _init_git_repo_with_file(project, ".claude/agents/builder.md", _agent_text("builder", "Read, Edit"))
    kwargs = dict(
        scope="project-local", project_path=project, config_dir=home / ".claude" / "claudeglass", claude_root=home / ".claude"
    )
    plan = apply_mod.plan_apply(catalogue.get("implementation-heavy"), **kwargs)
    assert len(plan.blocked) == 1 and "builder.md is tracked by git" in plan.blocked[0]
    allowed = apply_mod.plan_apply(catalogue.get("implementation-heavy"), allow_tracked=True, **kwargs)
    assert allowed.blocked == ()
    apply_mod.execute(allowed, config_dir=kwargs["config_dir"])
    assert parse_frontmatter((project / ".claude" / "agents" / "builder.md").read_text())["model"] == "sonnet"


# --------------------------------------------------------------------
# missing agent file refusal + --force
# --------------------------------------------------------------------


def test_plan_apply_blocks_missing_agent_file(tmp_path):
    home = tmp_path / "home"
    claude_root = home / ".claude"
    project = tmp_path / "proj"
    config_dir = home / ".claude" / "claudeglass"
    (project / ".claude" / "agents").mkdir(parents=True)

    profile = _profile(settings={}, agents={"ghost": {"model": "opus"}})
    plan = apply_mod.plan_apply(
        profile, scope="project-local", project_path=project, config_dir=config_dir, claude_root=claude_root
    )
    assert any("no agent file found" in reason for reason in plan.blocked)
    assert not (project / ".claude" / "agents" / "ghost.md").exists()


def test_a_saved_current_profile_applies_at_user_scope(tmp_path, monkeypatch):
    """"Save my current settings" saved a project agent too, and apply, which looks for an agent's
    file in the user's folder at its default scope, then refused the whole saved profile."""
    from test_service_api import _start_server

    handle = _start_server(tmp_path, monkeypatch)
    try:
        handle.store.upsert_snapshot(
            project_slug="proj-a",
            project_root_path=r"C:\Users\definitely-not-a-real-person\.claude\projects\proj-a",
            ts="2026-09-19T12:00:00Z",
            schema_version=2,
            digest_json=json.dumps(
                {
                    "effective": {"effortLevel": "high", "modelSettings": {"claude-opus-5-5": {"effortLevel": "medium"}}},
                    "managed_keys": [],
                    "effective_agents": {
                        "helper": {"source": "user", "model": "sonnet", "effort": "low", "max_turns": 40},
                        "builder": {"source": "project", "model": "haiku"},
                    },
                }
            ),
        )
        resp, payload = handle.post_json("/api/profiles/from-current", {})
        assert resp.status == 201, payload
        saved_path = handle.options.config_dir / "profiles" / "my-current-settings.toml"
    finally:
        handle.close()
        handle.store.close()

    from claudeglass.profiles.schema import load_profile

    profile = load_profile(saved_path)
    assert set(profile.agents) == {"helper"}
    assert profile.settings["modelSettings"] == {"claude-opus-5-5": {"effortLevel": "medium"}}
    claude_root = tmp_path / "home" / ".claude"
    (claude_root / "agents").mkdir(parents=True)
    (claude_root / "agents" / "helper.md").write_text("---\nname: helper\n---\nBody.\n", encoding="utf-8")
    plan = apply_mod.plan_apply(
        profile, scope="user", project_path=None, config_dir=claude_root / "claudeglass", claude_root=claude_root
    )
    assert plan.blocked == ()
    helper = next(a for a in plan.actions if a.kind == "agent_frontmatter")
    assert b"maxTurns: 40" in helper.new_bytes and b"effort: low" in helper.new_bytes


def test_a_saved_current_profile_is_not_refused_for_an_agent_kept_in_a_subfolder(tmp_path, monkeypatch):
    """The config hook reads the agents folder recursively and names an agent by its frontmatter, so
    "Save my current settings" saves an agent apply finds only elsewhere under the folder. The saved
    profile must still apply: the settings and the other agents are written."""
    from claudeglass.profiles.schema import load_profile
    from test_service_api import _start_server

    handle = _start_server(tmp_path, monkeypatch)
    try:
        handle.store.upsert_snapshot(
            project_slug="proj-a",
            project_root_path=r"C:\Users\definitely-not-a-real-person\.claude\projects\proj-a",
            ts="2026-09-19T12:00:00Z",
            schema_version=2,
            digest_json=json.dumps(
                {
                    "effective": {"effortLevel": "high"},
                    "managed_keys": [],
                    "effective_agents": {
                        name: {"source": "user", "model": "sonnet", "max_turns": 30}
                        for name in ("helper", "nested", "implementer")
                    },
                }
            ),
        )
        assert handle.post_json("/api/profiles/from-current", {})[0].status == 201
        saved_path = handle.options.config_dir / "profiles" / "my-current-settings.toml"
    finally:
        handle.close()
        handle.store.close()

    profile = load_profile(saved_path)
    claude_root, config_dir, agents_dir = _role_world(tmp_path / "w", {"helper": _agent_text("helper", "Read")})
    _put(agents_dir, "team/nested.md", _agent_text("nested", "Read"))
    _put(agents_dir, "impl.md", _agent_text("implementer", "Read, Edit"))
    plan = apply_mod.plan_apply(
        profile, scope="user", project_path=None, config_dir=config_dir, claude_root=claude_root
    )
    assert plan.blocked == ()
    assert plan.role_notes == (_LEFT_AS_THEY_ARE + "implementer and nested.",)
    apply_mod.execute(plan, config_dir=config_dir)
    assert json.loads((claude_root / "settings.json").read_text(encoding="utf-8"))["effortLevel"] == "high"
    assert "maxTurns: 30" in (agents_dir / "helper.md").read_text(encoding="utf-8")


_LEFT_AS_THEY_ARE = "Left as they are, because the file is in a subfolder or not named after the agent: "


def test_an_explicit_agent_whose_file_is_elsewhere_is_left_as_it_is_and_named_not_refused(tmp_path):
    """Claude Code reads the agents folder recursively and names an agent by its frontmatter, so an
    agent kept in a subfolder, or in a file not named after it, is the user's own. apply patches
    <agents folder>/<name>.md only: it leaves those, says so, and applies the rest."""
    claude_root, config_dir, agents_dir = _role_world(tmp_path, {"helper": _agent_text("helper", "Read")})
    _put(agents_dir, "team/nested.md", _agent_text("nested", "Read"))
    _put(agents_dir, "impl.md", _agent_text("implementer", "Read, Edit"))
    profile = _profile(
        settings={"effortLevel": "low"},
        agents={"helper": {"model": "sonnet"}, "implementer": {"model": "sonnet"}, "nested": {"model": "sonnet"}},
    )
    plan = apply_mod.plan_apply(
        profile, scope="user", project_path=None, config_dir=config_dir, claude_root=claude_root
    )
    assert plan.blocked == ()
    assert sorted(a.agent_name for a in plan.actions if a.kind == "agent_frontmatter") == ["helper"]
    assert plan.role_notes == (_LEFT_AS_THEY_ARE + "implementer and nested.",)
    apply_mod.execute(plan, config_dir=config_dir)
    assert json.loads((claude_root / "settings.json").read_text(encoding="utf-8")) == {"effortLevel": "low"}
    assert "model: sonnet" in (agents_dir / "helper.md").read_text(encoding="utf-8")
    assert (agents_dir / "impl.md").read_text(encoding="utf-8") == _agent_text("implementer", "Read, Edit")
    # one name reads in the singular; --force creates no second file for an agent that exists
    one = _profile(settings={}, agents={"nested": {"model": "sonnet"}})
    plan = apply_mod.plan_apply(
        one, scope="user", project_path=None, config_dir=config_dir, claude_root=claude_root, force=True
    )
    assert plan.blocked == () and plan.role_notes == (
        "Left as it is, because the file is in a subfolder or not named after the agent: nested.",
    )
    assert not (agents_dir / "nested.md").exists()
    # an agent found nowhere is refused as before, and a role's writers join the same note
    ghost = _profile(settings={}, agents={"ghost": {"model": "sonnet"}, "nested": {"model": "sonnet"}})
    plan = apply_mod.plan_apply(
        ghost, scope="user", project_path=None, config_dir=config_dir, claude_root=claude_root
    )
    assert len(plan.blocked) == 1 and "no agent file found for 'ghost'" in plan.blocked[0]
    both = _profile(settings={}, agents={"nested": {"model": "sonnet"}}, roles={"writer": {"model": "sonnet"}})
    plan = apply_mod.plan_apply(
        both, scope="user", project_path=None, config_dir=config_dir, claude_root=claude_root
    )
    assert plan.blocked == ()
    assert plan.role_notes == (_LEFT_AS_THEY_ARE + "implementer and nested.",)


def test_apply_of_discovery_scrape_without_an_explore_file_writes_the_settings(tmp_path):
    """Explore is built into Claude Code, so nearly no user has an
    Explore.md: a shipped profile must not name it, or the whole apply
    (settings included) is refused."""
    from claudeglass.profiles import catalogue

    claude_root, config_dir, agents_dir = _role_world(tmp_path, {"researcher": _agent_text("researcher", "Read")})
    plan = apply_mod.plan_apply(
        catalogue.get("discovery-scrape"), scope="user", project_path=None, config_dir=config_dir, claude_root=claude_root
    )
    assert plan.blocked == ()
    assert [a.kind for a in plan.actions if a.kind != "active_profile"] == ["settings"]
    apply_mod.execute(plan, config_dir=config_dir)
    written = json.loads((claude_root / "settings.json").read_text(encoding="utf-8"))
    assert written == {"effortLevel": "low", "subagentPromptCacheTtl": "5m", "autoCompactWindow": 100000}
    assert sorted(p.name for p in agents_dir.iterdir()) == ["researcher.md"]


def test_force_creates_missing_agent_file_from_scratch(tmp_path):
    home = tmp_path / "home"
    claude_root = home / ".claude"
    project = tmp_path / "proj"
    config_dir = home / ".claude" / "claudeglass"
    (project / ".claude" / "agents").mkdir(parents=True)

    profile = _profile(settings={}, agents={"ghost": {"model": "opus"}})
    plan = apply_mod.plan_apply(
        profile, scope="project-local", project_path=project, config_dir=config_dir, claude_root=claude_root, force=True
    )
    assert not plan.blocked
    apply_mod.execute(plan, config_dir=config_dir)
    agent_path = project / ".claude" / "agents" / "ghost.md"
    assert agent_path.exists()
    assert parse_frontmatter(agent_path.read_text(encoding="utf-8"))["model"] == "opus"


def _agent_text(name: str | None, tools: str, model: str = "opus") -> str:
    head = f"name: {name}\n" if name else ""
    return f"---\n{head}tools: {tools}\nmodel: {model}\n---\n\nBody.\n"


def _role_world(tmp_path, agents: dict[str, str]):
    """A user whose own agent files are ``agents`` (file stem -> text)."""
    claude_root = tmp_path / "home" / ".claude"
    agents_dir = claude_root / "agents"
    agents_dir.mkdir(parents=True)
    for stem, text in agents.items():
        (agents_dir / f"{stem}.md").write_text(text, encoding="utf-8")
    return claude_root, tmp_path / "home" / ".claude" / "claudeglass", agents_dir


def _plan_role_profile(tmp_path, agents, profile=None, **kwargs):
    from claudeglass.profiles import catalogue

    claude_root, config_dir, agents_dir = _role_world(tmp_path, agents)
    plan = apply_mod.plan_apply(
        profile or catalogue.get("implementation-heavy"),
        scope=kwargs.pop("scope", "user"),
        project_path=kwargs.pop("project_path", None),
        config_dir=config_dir,
        claude_root=claude_root,
        **kwargs,
    )
    return plan, config_dir, claude_root, agents_dir


def test_apply_of_a_role_profile_with_no_writer_writes_the_settings(tmp_path):
    plan, config_dir, claude_root, agents_dir = _plan_role_profile(
        tmp_path,
        {
            "researcher": _agent_text("researcher", "Read, Grep"),
            "reviewer": _agent_text("reviewer", "Read, Grep"),
        },
    )
    assert plan.blocked == ()
    assert [a.kind for a in plan.actions if a.kind != "active_profile"] == ["settings"]
    assert plan.role_notes == (
        "No agent in agents is named like implementer, fixer or builder and can edit files. "
        "The changes for the agent that writes code are left out.",
    )
    apply_mod.execute(plan, config_dir=config_dir)
    written = json.loads((claude_root / "settings.json").read_text(encoding="utf-8"))
    assert written == {"effortLevel": "medium", "subagentPromptCacheTtl": "5m"}
    assert "model: opus" in (agents_dir / "researcher.md").read_text(encoding="utf-8")


def test_apply_of_a_role_profile_patches_the_users_writer(tmp_path):
    plan, config_dir, claude_root, agents_dir = _plan_role_profile(
        tmp_path,
        {
            "implementer": _agent_text("implementer", "Read, Grep, Edit, Write, Bash"),
            "researcher": _agent_text("researcher", "Read, Grep"),
            "reviewer": _agent_text("reviewer", "Read, Grep"),
        },
    )
    assert plan.blocked == ()
    assert plan.role_notes == ()
    assert [a.agent_name for a in plan.actions if a.kind == "agent_frontmatter"] == ["implementer"]
    apply_mod.execute(plan, config_dir=config_dir)
    fm = parse_frontmatter((agents_dir / "implementer.md").read_text(encoding="utf-8"))
    assert (fm["model"], fm["effort"], fm["maxTurns"]) == ("sonnet", "medium", 60)
    assert "experimental.cacheTtl" not in fm and "omitClaudeMd" not in fm
    # a read-only agent is left alone
    assert "effort" not in parse_frontmatter((agents_dir / "researcher.md").read_text(encoding="utf-8"))


def test_apply_of_a_role_profile_says_when_several_agents_write_code(tmp_path):
    plan, *_ = _plan_role_profile(
        tmp_path,
        {
            "tester": _agent_text("tester", "Read, Edit"),
            "implementer": _agent_text("implementer", "Read, Edit"),
            "researcher": _agent_text("researcher", "Read"),
        },
    )
    assert sorted(a.agent_name for a in plan.actions if a.kind == "agent_frontmatter") == ["implementer", "tester"]
    assert plan.role_notes == (
        "implementer and tester are both named like an agent that writes code, so each gets these changes.",
    )
    plan, *_ = _plan_role_profile(
        tmp_path / "three",
        {
            "tester": _agent_text("tester", "Read, Edit"),
            "implementer": _agent_text("implementer", "Read, Edit"),
            "fixer": _agent_text("fixer", "Read, Edit"),
        },
    )
    assert plan.role_notes == (
        "fixer, implementer and tester are all named like an agent that writes code, so each gets these changes.",
    )


def test_apply_of_a_role_profile_leaves_out_a_writer_that_cannot_edit_a_misnamed_file_and_a_broken_one(tmp_path):
    plan, *_ = _plan_role_profile(
        tmp_path,
        {
            # named like a writer, but cannot edit
            "implementer": _agent_text("implementer", "Read, Grep"),
            # the file's frontmatter name is not its stem: apply could not patch it
            "builder": _agent_text("something-else", "Read, Edit"),
            # frontmatter this parser cannot read
            "fixer": "---\nname: fixer\ntools: [Edit\n---\n",
            "notes": "no frontmatter at all\n",
        },
    )
    assert plan.blocked == ()
    assert [a.kind for a in plan.actions if a.kind != "active_profile"] == ["settings"]
    assert len(plan.role_notes) == 1 and plan.role_notes[0].startswith("No agent in agents is named like")


def test_a_writer_named_file_with_no_frontmatter_is_not_an_agent(tmp_path):
    """Claude Code does not load a markdown file with no frontmatter as an
    agent and the snapshot hook skips it, so a role must not reach it: it
    would make the whole apply fail on a file that is not an agent."""
    plan, config_dir, claude_root, agents_dir = _plan_role_profile(
        tmp_path,
        {
            "implementer": _agent_text("implementer", "Read, Edit"),
            "writing-style": "Prefer short sentences.\n",
            "testing": "# Notes on testing\n",
        },
    )
    assert plan.blocked == ()
    assert [a.agent_name for a in plan.actions if a.kind == "agent_frontmatter"] == ["implementer"]
    assert plan.role_notes == ()
    apply_mod.execute(plan, config_dir=config_dir)
    assert (agents_dir / "writing-style.md").read_text(encoding="utf-8") == "Prefer short sentences.\n"
    assert "model: sonnet" in (agents_dir / "implementer.md").read_text(encoding="utf-8")


def test_a_writer_file_that_starts_with_a_byte_order_mark_is_left_alone(tmp_path):
    """The parser reads a file that opens with a byte-order mark as having
    no frontmatter, so it is not found as a writer: the apply succeeds, the
    file is not touched, and the no-writer note says so."""
    text = "\ufeff" + _agent_text("implementer", "Read, Edit")
    claude_root, config_dir, agents_dir = _role_world(tmp_path, {})
    (agents_dir / "implementer.md").write_bytes(text.encode("utf-8"))
    from claudeglass.profiles import catalogue

    plan = apply_mod.plan_apply(
        catalogue.get("implementation-heavy"), scope="user", project_path=None, config_dir=config_dir, claude_root=claude_root
    )
    assert plan.blocked == ()
    assert [a.kind for a in plan.actions if a.kind != "active_profile"] == ["settings"]
    assert len(plan.role_notes) == 1 and plan.role_notes[0].startswith("No agent in agents is named like")
    apply_mod.execute(plan, config_dir=config_dir)
    assert (agents_dir / "implementer.md").read_bytes() == text.encode("utf-8")


def _put(agents_dir, relative: str, text: str) -> None:
    path = agents_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _plan_with_files(tmp_path, files: dict[str, str], **kwargs):
    from claudeglass.profiles import catalogue

    claude_root, config_dir, agents_dir = _role_world(tmp_path, {})
    for relative, text in files.items():
        _put(agents_dir, relative, text)
    plan = apply_mod.plan_apply(
        catalogue.get("implementation-heavy"),
        scope="user",
        project_path=None,
        config_dir=config_dir,
        claude_root=claude_root,
        **kwargs,
    )
    return plan, config_dir, claude_root, agents_dir


def test_writers_the_cli_cannot_patch_are_named_in_one_note_and_nothing_is_refused(tmp_path):
    """Claude Code reads the agents folder recursively and names an agent by
    its frontmatter, but apply patches ``<agents folder>/<name>.md`` only.
    A writer kept in a subfolder, in a file not named after it, or with a
    different capital letter is found, left as it is, and named: the
    no-writer note would be false for it."""
    files = {
        "impl.md": _agent_text("implementer", "Read, Edit"),
        "team/fixer.md": _agent_text("fixer", "Read, Edit"),
        "Builder.md": _agent_text("builder", "Read, Edit"),
    }
    plan, config_dir, claude_root, agents_dir = _plan_with_files(tmp_path, files)
    assert plan.blocked == ()
    assert [a.kind for a in plan.actions if a.kind != "active_profile"] == ["settings"]
    assert plan.role_notes == (
        "Left as they are, because the file is in a subfolder or not named after the agent: "
        "builder, fixer and implementer.",
    )
    # with --force no file is created for any of them
    plan, config_dir, claude_root, agents_dir = _plan_with_files(tmp_path / "forced", files, force=True)
    assert plan.blocked == ()
    apply_mod.execute(plan, config_dir=config_dir)
    assert sorted(p.relative_to(agents_dir).as_posix() for p in agents_dir.rglob("*") if p.is_file()) == sorted(files)
    for relative, text in files.items():
        assert (agents_dir / relative).read_text(encoding="utf-8") == text


def test_one_writer_the_cli_cannot_patch_is_named_with_the_singular_sentence(tmp_path):
    plan, *_ = _plan_with_files(tmp_path, {"team/fixer.md": _agent_text("fixer", "Read, Edit")})
    assert plan.blocked == ()
    assert plan.role_notes == (
        "Left as it is, because the file is in a subfolder or not named after the agent: fixer.",
    )


def test_a_writer_file_named_after_another_writer_name_is_not_patched_under_its_stem(tmp_path):
    """The name against stem guard: ``impl.md`` says it is ``implementer``,
    so the plan would look for ``implementer.md`` and refuse the whole apply
    (or, with --force, create a new file) unless the guard keeps it out."""
    plan, config_dir, claude_root, agents_dir = _plan_with_files(
        tmp_path,
        {"impl.md": _agent_text("implementer", "Read, Edit"), "tester.md": _agent_text("tester", "Read, Edit")},
        force=True,
    )
    assert plan.blocked == ()
    assert [a.agent_name for a in plan.actions if a.kind == "agent_frontmatter"] == ["tester"]
    assert plan.role_notes == (
        "Left as it is, because the file is in a subfolder or not named after the agent: implementer.",
    )
    apply_mod.execute(plan, config_dir=config_dir)
    assert not (agents_dir / "implementer.md").exists()


def test_a_subfolder_writer_beside_a_patchable_one_is_named_and_the_other_is_patched(tmp_path):
    plan, config_dir, claude_root, agents_dir = _plan_with_files(
        tmp_path,
        {"implementer.md": _agent_text("implementer", "Read, Edit"), "team/fixer.md": _agent_text("fixer", "Read, Edit")},
    )
    assert plan.blocked == ()
    assert [a.agent_name for a in plan.actions if a.kind == "agent_frontmatter"] == ["implementer"]
    assert plan.role_notes == (
        "Left as it is, because the file is in a subfolder or not named after the agent: fixer.",
    )
    apply_mod.execute(plan, config_dir=config_dir)
    assert "effort" not in parse_frontmatter((agents_dir / "team" / "fixer.md").read_text(encoding="utf-8"))


def test_the_no_writer_note_is_printed_only_when_no_writer_exists_anywhere_in_the_folder(tmp_path):
    plan, *_ = _plan_with_files(tmp_path / "none", {"team/helper.md": _agent_text("helper", "Read, Edit")})
    assert [n.startswith("No agent in agents is named like") for n in plan.role_notes] == [True]
    plan, *_ = _plan_with_files(tmp_path / "readonly", {"team/implementer.md": _agent_text("implementer", "Read")})
    assert [n.startswith("No agent in agents is named like") for n in plan.role_notes] == [True]


def test_apply_of_a_role_profile_reads_the_project_agents_at_a_project_scope(tmp_path):
    from claudeglass.profiles import catalogue

    project = tmp_path / "proj"
    project_agents = project / ".claude" / "agents"
    project_agents.mkdir(parents=True)
    (project_agents / "builder.md").write_text(_agent_text("builder", "Read, Edit"), encoding="utf-8")
    claude_root, config_dir, user_agents = _role_world(
        tmp_path, {"implementer": _agent_text("implementer", "Read, Edit")}
    )
    plan = apply_mod.plan_apply(
        catalogue.get("implementation-heavy"),
        scope="project-local",
        project_path=project,
        config_dir=config_dir,
        claude_root=claude_root,
    )
    assert [a.path for a in plan.actions if a.kind == "agent_frontmatter"] == [project_agents / "builder.md"]
    no_writer = apply_mod.plan_apply(
        catalogue.get("implementation-heavy"),
        scope="project-local",
        project_path=tmp_path / "other",
        config_dir=config_dir,
        claude_root=claude_root,
    )
    assert no_writer.role_notes == (
        f"No agent in {Path('.claude') / 'agents'} is named like implementer, fixer or builder and can edit files. "
        "The changes for the agent that writes code are left out.",
    )


def test_an_explicit_agent_with_no_file_is_still_refused(tmp_path):
    claude_root, config_dir, agents_dir = _role_world(tmp_path, {"implementer": _agent_text("implementer", "Edit")})
    profile = _profile(settings={}, agents={"ghost": {"model": "opus"}}, roles={"writer": {"effort": "low"}})
    plan = apply_mod.plan_apply(profile, scope="user", project_path=None, config_dir=config_dir, claude_root=claude_root)
    assert len(plan.blocked) == 1 and "no agent file found for 'ghost'" in plan.blocked[0]
    assert not (agents_dir / "ghost.md").exists()


def test_force_never_creates_a_file_for_a_role(tmp_path):
    plan, config_dir, claude_root, agents_dir = _plan_role_profile(
        tmp_path, {"researcher": _agent_text("researcher", "Read")}, force=True
    )
    assert plan.blocked == ()
    apply_mod.execute(plan, config_dir=config_dir)
    assert sorted(p.name for p in agents_dir.iterdir()) == ["researcher.md"]


def test_explicit_agent_values_win_over_the_role_when_both_name_the_writer(tmp_path):
    claude_root, config_dir, agents_dir = _role_world(tmp_path, {"implementer": _agent_text("implementer", "Edit")})
    profile = _profile(
        settings={}, agents={"implementer": {"model": "haiku"}}, roles={"writer": {"model": "sonnet", "effort": "low"}}
    )
    plan = apply_mod.plan_apply(profile, scope="user", project_path=None, config_dir=config_dir, claude_root=claude_root)
    apply_mod.execute(plan, config_dir=config_dir)
    fm = parse_frontmatter((agents_dir / "implementer.md").read_text(encoding="utf-8"))
    assert (fm["model"], fm["effort"]) == ("haiku", "low")


def test_force_does_not_affect_allow_tracked(tmp_path):
    """--force's only job is creating a missing agent file from scratch;
    it must not also waive the tracked-file refusal (that is
    --allow-tracked's job specifically, per the module docstring)."""
    home = tmp_path / "home"
    claude_root = home / ".claude"
    project = tmp_path / "proj"
    config_dir = home / ".claude" / "claudeglass"
    _init_git_repo_with_file(project, ".claude/settings.json", "{}\n")

    profile = _profile(settings={"effortLevel": "high"})
    plan = apply_mod.plan_apply(
        profile, scope="repo", project_path=project, config_dir=config_dir, claude_root=claude_root, force=True
    )
    assert any("tracked by git" in reason for reason in plan.blocked)


# --------------------------------------------------------------------
# managed keys: excluded from writes, listed in skipped_managed
# --------------------------------------------------------------------


def test_managed_settings_key_is_excluded_and_reported(tmp_path):
    home = tmp_path / "home"
    claude_root = home / ".claude"
    config_dir = home / ".claude" / "claudeglass"
    profile = _profile(settings={"effortLevel": "high", "outputStyle": "concise"})
    snapshot = _snapshot(managed_keys=["effortLevel"])

    plan = apply_mod.plan_apply(
        profile, scope="user", project_path=None, config_dir=config_dir, claude_root=claude_root, snapshot=snapshot
    )
    assert "settings.effortLevel" in plan.skipped_managed
    settings_action = next(a for a in plan.actions if a.kind == "settings")
    written = json.loads(settings_action.new_bytes.decode("utf-8"))
    assert "effortLevel" not in written
    assert written["outputStyle"] == "concise"


def test_managed_agent_key_is_excluded_via_agents_wildcard(tmp_path):
    home = tmp_path / "home"
    claude_root = home / ".claude"
    project = tmp_path / "proj"
    config_dir = home / ".claude" / "claudeglass"
    agents_dir = project / ".claude" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "reviewer.md").write_text("---\nmodel: opus\n---\n", encoding="utf-8")

    profile = _profile(settings={}, agents={"reviewer": {"effort": "high"}})
    snapshot = _snapshot(managed_keys=["agents"])
    plan = apply_mod.plan_apply(
        profile,
        scope="project-local",
        project_path=project,
        config_dir=config_dir,
        claude_root=claude_root,
        snapshot=snapshot,
    )
    assert "agents.reviewer.effort" in plan.skipped_managed
    assert not any(a.kind == "agent_frontmatter" for a in plan.actions)


def test_managed_env_block_excludes_all_env_changes(tmp_path):
    # COV-07/COV-11: managed_keys is top-level settings.json key names
    # only, so "is env managed" is a whole-"env"-block question -- there
    # is no per-name managed signal at this layer (matching how any other
    # settings key, e.g. "model", is all-or-nothing too).
    home = tmp_path / "home"
    claude_root = home / ".claude"
    config_dir = home / ".claude" / "claudeglass"
    profile = _profile(
        settings={}, env={"CLAUDE_CODE_PROMPT_CACHE_TTL": "5m", "MAX_THINKING_TOKENS": "1024"}
    )
    snapshot = _snapshot(managed_keys=["env"])
    plan = apply_mod.plan_apply(
        profile, scope="user", project_path=None, config_dir=config_dir, claude_root=claude_root, snapshot=snapshot
    )
    assert "env.CLAUDE_CODE_PROMPT_CACHE_TTL" in plan.skipped_managed
    assert "env.MAX_THINKING_TOKENS" in plan.skipped_managed
    assert not any(a.kind == "settings" for a in plan.actions)


# --------------------------------------------------------------------
# a profile's env entries, written into the settings file's "env" object
# --------------------------------------------------------------------


def test_env_entries_are_merged_into_the_settings_files_env_object(tmp_path):
    home = tmp_path / "home"
    claude_root = home / ".claude"
    claude_root.mkdir(parents=True)
    (claude_root / "settings.json").write_text(
        json.dumps({"model": "opus", "env": {"SOME_OTHER_VAR": "keep-me"}}), encoding="utf-8"
    )
    config_dir = home / ".claude" / "claudeglass"
    profile = _profile(
        settings={},
        env={"MAX_THINKING_TOKENS": "1024", "CLAUDE_CODE_PROMPT_CACHE_TTL": "5m"},
    )
    plan = apply_mod.plan_apply(
        profile, scope="user", project_path=None, config_dir=config_dir, claude_root=claude_root
    )
    settings_action = next(a for a in plan.actions if a.kind == "settings")
    written = json.loads(settings_action.new_bytes.decode("utf-8"))
    # The env entries the profile names are added; an existing env entry
    # it doesn't mention, and every other settings key, are untouched.
    assert written == {
        "model": "opus",
        "env": {
            "SOME_OTHER_VAR": "keep-me",
            "MAX_THINKING_TOKENS": "1024",
            "CLAUDE_CODE_PROMPT_CACHE_TTL": "5m",
        },
    }


# --------------------------------------------------------------------
# COV-04: plan.overridden -- a write whose effective value already comes
# from a higher-precedence layer
# --------------------------------------------------------------------


def test_overridden_warns_when_a_higher_layer_already_supplies_a_settings_key(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "proj"
    config_dir = home / ".claude" / "claudeglass"
    claude_root = home / ".claude"
    (project / ".claude").mkdir(parents=True)
    profile = _profile(settings={"model": "haiku"})
    # project-local (rank 1) outranks repo/project_shared (rank 2) in
    # SETTINGS_LAYER_NAMES, so a repo-scope write of "model" here would
    # have no visible effect: project-local already wins.
    snapshot = _snapshot(effective_provenance={"model": "project_local"})
    plan = apply_mod.plan_apply(
        profile,
        scope="repo",
        project_path=project,
        config_dir=config_dir,
        claude_root=claude_root,
        snapshot=snapshot,
    )
    assert len(plan.overridden) == 1
    assert plan.overridden[0].startswith("model: already set by")
    assert "local settings" in plan.overridden[0]
    # Never blocks -- the write still happens, just with a warning.
    assert any(a.kind == "settings" for a in plan.actions)
    assert not plan.blocked


def test_overridden_warns_for_an_env_key_via_its_own_provenance_field(tmp_path):
    home = tmp_path / "home"
    config_dir = home / ".claude" / "claudeglass"
    claude_root = home / ".claude"
    profile = _profile(settings={}, env={"ENABLE_TOOL_SEARCH": "true"})
    # env provenance lives in a different snapshot field than settings
    # provenance (effective_env_provenance, not effective_provenance).
    snapshot = _snapshot(effective_env_provenance={"ENABLE_TOOL_SEARCH": "project_shared"})
    plan = apply_mod.plan_apply(
        profile, scope="user", project_path=None, config_dir=config_dir, claude_root=claude_root, snapshot=snapshot
    )
    assert plan.overridden == ("env.ENABLE_TOOL_SEARCH: already set by this project's shared settings "
                                "(.claude/settings.json), which takes precedence",)


def test_overridden_is_empty_without_a_snapshot(tmp_path):
    home = tmp_path / "home"
    config_dir = home / ".claude" / "claudeglass"
    claude_root = home / ".claude"
    profile = _profile(settings={"model": "haiku"}, env={"ENABLE_TOOL_SEARCH": "true"})
    plan = apply_mod.plan_apply(
        profile, scope="user", project_path=None, config_dir=config_dir, claude_root=claude_root, snapshot=None
    )
    assert plan.overridden == ()


# --------------------------------------------------------------------
# settings file that isn't valid JSON
# --------------------------------------------------------------------


def test_plan_apply_raises_on_unparseable_existing_settings_json(tmp_path):
    home = tmp_path / "home"
    claude_root = home / ".claude"
    config_dir = home / ".claude" / "claudeglass"
    settings_path = claude_root / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text("not json at all", encoding="utf-8")

    profile = _profile(settings={"effortLevel": "high"})
    with pytest.raises(apply_mod.ApplyError):
        apply_mod.plan_apply(profile, scope="user", project_path=None, config_dir=config_dir, claude_root=claude_root)


# --------------------------------------------------------------------
# --launch: writes only the settings overlay, nothing else
# --------------------------------------------------------------------


def test_write_launch_overlay_writes_only_the_overlay_file(tmp_path):
    home = tmp_path / "home"
    claude_root = home / ".claude"
    config_dir = home / ".claude" / "claudeglass"
    profile = _profile(settings={"effortLevel": "high", "outputStyle": "concise"})

    path = apply_mod.write_launch_overlay(profile, config_dir=config_dir)
    assert path == config_dir / "profiles" / "sample.settings.json"
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written == {"effortLevel": "high", "outputStyle": "concise"}

    # Nothing else was created under config_dir -- no backups, no
    # manifest, no active-profile marker, no snapshot stamp.
    all_paths = sorted(p.relative_to(config_dir) for p in config_dir.rglob("*") if p.is_file())
    assert all_paths == [Path("profiles") / "sample.settings.json"]


def test_write_launch_overlay_excludes_managed_keys(tmp_path):
    home = tmp_path / "home"
    claude_root = home / ".claude"
    config_dir = home / ".claude" / "claudeglass"
    profile = _profile(settings={"effortLevel": "high", "outputStyle": "concise"})
    path = apply_mod.write_launch_overlay(profile, config_dir=config_dir, managed_keys={"effortLevel"})
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written == {"outputStyle": "concise"}


def test_write_launch_overlay_includes_env_entries(tmp_path):
    # COV-07/COV-11: a launch overlay is itself a settings.json-shaped
    # file, so a profile's env entries fold into its own "env" object the
    # same way plan_apply folds them into a real settings.json.
    home = tmp_path / "home"
    config_dir = home / ".claude" / "claudeglass"
    profile = _profile(settings={"effortLevel": "high"}, env={"ENABLE_TOOL_SEARCH": "true"})
    path = apply_mod.write_launch_overlay(profile, config_dir=config_dir)
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written == {"effortLevel": "high", "env": {"ENABLE_TOOL_SEARCH": "true"}}


def test_write_launch_overlay_excludes_env_when_env_block_is_managed(tmp_path):
    home = tmp_path / "home"
    config_dir = home / ".claude" / "claudeglass"
    profile = _profile(settings={"effortLevel": "high"}, env={"ENABLE_TOOL_SEARCH": "true"})
    path = apply_mod.write_launch_overlay(profile, config_dir=config_dir, managed_keys={"env"})
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written == {"effortLevel": "high"}


# --------------------------------------------------------------------
# list_backups
# --------------------------------------------------------------------


def test_list_backups_empty_when_no_backups_dir(tmp_path):
    config_dir = tmp_path / "home" / ".claude" / "claudeglass"
    assert apply_mod.list_backups(config_dir) == []


def test_list_backups_reports_ts_profile_scope_and_file_count(tmp_path):
    home = tmp_path / "home"
    claude_root = home / ".claude"
    config_dir = home / ".claude" / "claudeglass"

    profile_a = _profile(settings={"effortLevel": "high"})
    plan_a = apply_mod.plan_apply(profile_a, scope="user", project_path=None, config_dir=config_dir, claude_root=claude_root)
    result_a = apply_mod.execute(plan_a, config_dir=config_dir)

    profile_b = load_dict({"id": "other", "settings": {"outputStyle": "concise"}})
    plan_b = apply_mod.plan_apply(profile_b, scope="user", project_path=None, config_dir=config_dir, claude_root=claude_root)
    result_b = apply_mod.execute(plan_b, config_dir=config_dir)

    # Both executes can legitimately land in the same wall-clock second;
    # execute()'s own collision-avoidance (see its docstring/comment)
    # gives them distinct backup dirs regardless, which is exactly what
    # this test is asserting.
    assert result_a.ts != result_b.ts

    backups = apply_mod.list_backups(config_dir)
    ts_values = [b.ts for b in backups]
    assert ts_values == sorted(ts_values)
    assert result_a.ts in ts_values
    assert result_b.ts in ts_values
    by_ts = {b.ts: b for b in backups}
    assert by_ts[result_a.ts].profile_id == "sample"
    assert by_ts[result_a.ts].scope == "user"
    assert by_ts[result_a.ts].file_count >= 1
    assert by_ts[result_b.ts].profile_id == "other"


def test_execute_avoids_backup_collision_within_the_same_second(tmp_path, monkeypatch):
    """Force both executes to compute the exact same base timestamp
    (simulating two applies within the same UTC second) and confirm the
    first apply's backup survives untouched -- the collision this
    module's ``execute()`` disambiguates against."""
    home = tmp_path / "home"
    claude_root = home / ".claude"
    config_dir = home / ".claude" / "claudeglass"

    import claudeglass.profiles.apply as apply_module

    class _FrozenDatetime(apply_module.datetime):
        @classmethod
        def now(cls, tz=None):
            return apply_module.datetime(2026, 1, 1, tzinfo=tz)

    monkeypatch.setattr(apply_module, "datetime", _FrozenDatetime)

    profile_a = _profile(settings={"effortLevel": "high"})
    plan_a = apply_mod.plan_apply(profile_a, scope="user", project_path=None, config_dir=config_dir, claude_root=claude_root)
    result_a = apply_mod.execute(plan_a, config_dir=config_dir)

    profile_b = load_dict({"id": "other", "settings": {"outputStyle": "concise"}})
    plan_b = apply_mod.plan_apply(profile_b, scope="user", project_path=None, config_dir=config_dir, claude_root=claude_root)
    result_b = apply_mod.execute(plan_b, config_dir=config_dir)

    assert result_a.ts == "20260101T000000Z"
    assert result_b.ts == "20260101T000000Z-2"

    manifest_a = json.loads((result_a.backup_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest_b = json.loads((result_b.backup_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest_a["profile_id"] == "sample"
    assert manifest_b["profile_id"] == "other"

    # B rewrote the files A wrote, so reverting A alone is refused unless
    # the caller accepts discarding B's edits.
    with pytest.raises(apply_mod.ApplyError, match="--ignore-changes"):
        apply_mod.revert(result_a.ts, config_dir=config_dir)
    revert_a = apply_mod.revert(result_a.ts, config_dir=config_dir, ignore_changes=True)
    assert revert_a.deleted or revert_a.restored


def test_manifest_records_changes_and_revert_refuses_edited_file(tmp_path):
    claude_root = tmp_path / "home" / ".claude"
    config_dir = claude_root / "claudeglass"
    claude_root.mkdir(parents=True)
    settings = claude_root / "settings.json"
    settings.write_text('{"effortLevel": "low", "theme": "dark"}', encoding="utf-8")
    plan = apply_mod.plan_apply(
        _profile(settings={"effortLevel": "high"}), scope="user", project_path=None,
        config_dir=config_dir, claude_root=claude_root,
    )
    explanation = "\n".join(apply_mod.explain_plan(plan))
    assert "Change: effortLevel" in explanation
    assert "Now: low. After: high." in explanation
    assert "Undo:" in explanation
    result = apply_mod.execute(plan, config_dir=config_dir)
    manifest = json.loads((result.backup_dir / "manifest.json").read_text(encoding="utf-8"))
    entry = next(e for e in manifest["entries"] if e["kind"] == "settings")
    assert entry["changes"] == [{"key": "effortLevel", "agent": None, "old": "low", "new": "high"}]
    assert len(entry["new_sha256"]) == 64

    settings.write_text('{"effortLevel": "high", "theme": "light"}', encoding="utf-8")
    with pytest.raises(apply_mod.ApplyError, match="changed after this apply"):
        apply_mod.revert(result.ts, config_dir=config_dir)
    assert "light" in settings.read_text(encoding="utf-8")  # nothing restored
    apply_mod.revert(result.ts, config_dir=config_dir, ignore_changes=True)
    assert json.loads(settings.read_text(encoding="utf-8")) == {"effortLevel": "low", "theme": "dark"}


def test_revert_of_an_old_manifest_without_hashes_still_works(tmp_path):
    config_dir = tmp_path / "tl"
    target = tmp_path / "settings.json"
    target.write_text("{}", encoding="utf-8")
    backup = config_dir / "backups" / "20260101T000000Z"
    (backup / "files").mkdir(parents=True)
    (backup / "files" / "0000.bak").write_text('{"a": 1}', encoding="utf-8")
    (backup / "manifest.json").write_text(json.dumps({
        "ts": "20260101T000000Z", "profile_id": "p", "scope": "user",
        "entries": [{"kind": "settings", "path": str(target), "backup": "0000.bak", "agent_name": None}],
    }), encoding="utf-8")
    apply_mod.revert("20260101T000000Z", config_dir=config_dir)
    assert target.read_text(encoding="utf-8") == '{"a": 1}'


def test_list_backups_skips_unparseable_manifest(tmp_path):
    config_dir = tmp_path / "home" / ".claude" / "claudeglass"
    bad_dir = config_dir / "backups" / "20260101T000000Z"
    bad_dir.mkdir(parents=True)
    (bad_dir / "manifest.json").write_text("not json", encoding="utf-8")
    assert apply_mod.list_backups(config_dir) == []


# --------------------------------------------------------------------
# privacy: apply results/backups never leak paths beyond what the
# caller itself supplied (home/project_path/config_dir)
# --------------------------------------------------------------------


def test_apply_result_paths_are_all_under_caller_supplied_roots(tmp_path):
    home = tmp_path / "home"
    claude_root = home / ".claude"
    project = tmp_path / "proj"
    config_dir = home / ".claude" / "claudeglass"
    (project / ".claude" / "agents").mkdir(parents=True)

    profile = _profile(settings={"effortLevel": "high"})
    plan = apply_mod.plan_apply(
        profile, scope="project-local", project_path=project, config_dir=config_dir, claude_root=claude_root
    )
    result = apply_mod.execute(plan, config_dir=config_dir)

    for path in result.written:
        assert str(path).startswith(str(project)) or str(path).startswith(str(home))
    assert str(result.snapshot_path).startswith(str(config_dir))
    assert str(result.active_profile_path).startswith(str(config_dir))


def _model_settings_world(tmp_path, settings: dict):
    claude_root = tmp_path / "home" / ".claude"
    claude_root.mkdir(parents=True)
    settings_path = claude_root / "settings.json"
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return claude_root / "claudeglass", claude_root, settings_path


def test_apply_merges_one_models_entry_without_losing_the_rest(tmp_path):
    existing = {
        "model": "opus",
        "modelSettings": {
            "claude-opus-5-5": {"effortLevel": "high", "autoCompactWindow": 300_000, "maxEffortLevel": "xhigh"},
            "claude-sonnet-4-6": {"effortLevel": "low"},
        },
    }
    config_dir, claude_root, settings_path = _model_settings_world(tmp_path, existing)
    profile = _profile(
        settings={
            "modelSettings": {
                "claude-opus-5-5": {"effortLevel": "medium"},
                "claude-haiku-4-5": {"autoCompactWindow": "auto"},
            }
        }
    )
    plan = apply_mod.plan_apply(profile, scope="user", project_path=None, config_dir=config_dir, claude_root=claude_root)
    apply_mod.execute(plan, config_dir=config_dir)
    written = json.loads(settings_path.read_text(encoding="utf-8"))
    # the named model changes only the key the profile names; every other
    # key of its entry (even one this tool has no name for), every other
    # model and every other setting stay as they were
    assert written["modelSettings"] == {
        "claude-opus-5-5": {"effortLevel": "medium", "autoCompactWindow": 300_000, "maxEffortLevel": "xhigh"},
        "claude-sonnet-4-6": {"effortLevel": "low"},
        "claude-haiku-4-5": {"autoCompactWindow": "auto"},
    }
    assert written["model"] == "opus"
    # a file with no modelSettings at all gets the profile's, as written
    config_dir2, claude_root2, settings_path2 = _model_settings_world(tmp_path / "second", {"model": "opus"})
    plan = apply_mod.plan_apply(profile, scope="user", project_path=None, config_dir=config_dir2, claude_root=claude_root2)
    apply_mod.execute(plan, config_dir=config_dir2)
    assert json.loads(settings_path2.read_text(encoding="utf-8"))["modelSettings"] == profile.settings["modelSettings"]


def test_write_launch_overlay_carries_model_settings(tmp_path):
    profile = _profile(settings={"modelSettings": {"claude-opus-5-5": {"effortLevel": "medium"}}})
    path = apply_mod.write_launch_overlay(profile, config_dir=tmp_path / "cfg")
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "modelSettings": {"claude-opus-5-5": {"effortLevel": "medium"}}
    }


def test_apply_of_a_profile_with_no_model_settings_leaves_the_files_model_settings_byte_for_byte(tmp_path):
    existing = {
        "effortLevel": "low",
        "modelSettings": {
            "claude-opus-5-5": {"effortLevel": "high", "autoCompactWindow": 300_000},
            "claude-sonnet-4-6[1m]": {"autoCompactWindow": "auto"},
        },
    }
    config_dir, claude_root, settings_path = _model_settings_world(tmp_path, existing)
    old_text = settings_path.read_text(encoding="utf-8")
    block_start = old_text.index('"modelSettings"')
    plan = apply_mod.plan_apply(
        _profile(settings={"effortLevel": "high"}),
        scope="user", project_path=None, config_dir=config_dir, claude_root=claude_root,
    )
    apply_mod.execute(plan, config_dir=config_dir)
    new_text = settings_path.read_text(encoding="utf-8")
    assert new_text[new_text.index('"modelSettings"'):] == old_text[block_start:]
    assert json.loads(new_text)["effortLevel"] == "high"


def test_revert_restores_a_file_whose_model_settings_an_apply_changed(tmp_path):
    existing = {"modelSettings": {"claude-opus-5-5": {"effortLevel": "high", "autoCompactWindow": 300_000}}}
    config_dir, claude_root, settings_path = _model_settings_world(tmp_path, existing)
    original = settings_path.read_bytes()
    profile = _profile(settings={"modelSettings": {"claude-opus-5-5": {"effortLevel": "low"}}})
    plan = apply_mod.plan_apply(profile, scope="user", project_path=None, config_dir=config_dir, claude_root=claude_root)
    result = apply_mod.execute(plan, config_dir=config_dir)
    assert settings_path.read_bytes() != original
    assert json.loads(settings_path.read_text(encoding="utf-8"))["modelSettings"]["claude-opus-5-5"] == {
        "effortLevel": "low",
        "autoCompactWindow": 300_000,
    }
    apply_mod.revert(result.ts, config_dir=config_dir)
    assert settings_path.read_bytes() == original


def test_map_settings_merge_by_name_and_revert_cleanly(tmp_path):
    # skillOverrides and enabledPlugins are objects keyed by name: a
    # profile naming one entry changes that entry and keeps the rest.
    home = tmp_path / "home"
    claude_root = home / ".claude"
    config_dir = claude_root / "claudeglass"
    claude_root.mkdir(parents=True)
    settings_path = claude_root / "settings.json"
    settings_path.write_text(
        json.dumps({"skillOverrides": {"pdf": "off"}, "enabledPlugins": {"a@m": True, "b@m": True}}, indent=2) + "\n",
        encoding="utf-8",
    )
    original = settings_path.read_bytes()
    profile = _profile(settings={"skillOverrides": {"xlsx": "name-only"}, "enabledPlugins": {"b@m": False}})
    plan = apply_mod.plan_apply(profile, scope="user", project_path=None, config_dir=config_dir, claude_root=claude_root)
    result = apply_mod.execute(plan, config_dir=config_dir)
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    assert data["skillOverrides"] == {"pdf": "off", "xlsx": "name-only"}
    assert data["enabledPlugins"] == {"a@m": True, "b@m": False}
    apply_mod.revert(result.ts, config_dir=config_dir)
    assert settings_path.read_bytes() == original


def test_plan_apply_refuses_an_agent_name_nothing_checked(tmp_path):
    from claudeglass.profiles.schema import Profile

    rule = (
        "an agent name holds only letters, digits, single dots, underscores and hyphens, "
        "starts with a letter or digit, and is at most 128 characters"
    )
    root = tmp_path / "root"
    (root / "agents").mkdir(parents=True)
    with pytest.raises(apply_mod.ApplyError) as raised:
        apply_mod.plan_apply(
            Profile(id="x", agents={"../x": {"model": "sonnet"}}),
            scope="user",
            project_path=None,
            config_dir=tmp_path / "cfg",
            claude_root=root,
        )
    assert raised.value.reasons == [f"agent '../x': {rule}"]


def test_apply_leaves_out_a_writer_file_with_an_unusable_name(tmp_path):
    from claudeglass.profiles import catalogue

    root = tmp_path / "root"
    (root / "agents").mkdir(parents=True)
    (root / "agents" / "fixer x.md").write_text("---\nname: fixer x\ntools: Read, Edit\n---\n", encoding="utf-8")
    plan = apply_mod.plan_apply(
        catalogue.get("implementation-heavy"),
        scope="user",
        project_path=None,
        config_dir=tmp_path / "cfg",
        claude_root=root,
    )
    assert not [a for a in plan.actions if a.kind == "agent_frontmatter"]
    assert (
        "One agent that writes code has a name ClaudeGlass can't use, so it is left out. Rename it to one word of "
        "letters, digits, dots, underscores and hyphens to include it."
    ) in plan.role_notes


def test_a_per_model_effort_level_merges_and_is_explained_like_the_flat_one(tmp_path):
    # The thinking goal's main-session change, applied over a file that already has modelSettings.
    from claudeglass.fixes import SETTING_TEXT

    claude_root = tmp_path / ".claude"
    claude_root.mkdir()
    (claude_root / "settings.json").write_text(json.dumps({"modelSettings": {
        "claude-opus-5-5": {"autoCompactWindow": 500000}, "claude-fable-5-2": {"effortLevel": "high"}}}), encoding="utf-8")
    profile = _profile(settings={"modelSettings": {"claude-opus-5-5": {"effortLevel": "medium"}}})
    plan = apply_mod.plan_apply(
        profile, scope="user", project_path=None, config_dir=claude_root / "claudeglass", claude_root=claude_root,
    )
    action = next(a for a in plan.actions if a.kind == "settings")
    assert json.loads(action.new_bytes)["modelSettings"] == {
        "claude-opus-5-5": {"autoCompactWindow": 500000, "effortLevel": "medium"}, "claude-fable-5-2": {"effortLevel": "high"}}
    lines = apply_mod.explain_plan(plan)
    assert "Change: modelSettings.claude-opus-5-5.effortLevel" in lines
    assert f"  What it controls: {SETTING_TEXT['modelSettings.effortLevel'][0]}" in lines
    assert any(line.startswith("  Trade-off: Lower effort can miss things") for line in lines)


def test_the_apply_preview_says_a_project_files_flat_effort_beats_a_models_own_level(tmp_path):
    # N6: within a file a model's own level wins; across files the higher file's flat level does.
    claude_root = tmp_path / ".claude"
    project = tmp_path / "proj"
    (project / ".claude").mkdir(parents=True)
    snap = _snapshot(effective={"effortLevel": "high"}, effective_provenance={"effortLevel": "project_shared"})
    profile = _profile(settings={"modelSettings": {"claude-opus-5-5": {"effortLevel": "medium"}}})
    plan = apply_mod.plan_apply(profile, scope="user", project_path=None, config_dir=claude_root / "claudeglass",
                                claude_root=claude_root, snapshot=snap)
    assert plan.overridden == (
        "modelSettings.claude-opus-5-5.effortLevel: effortLevel is set by this project's shared settings "
        "(.claude/settings.json), which takes precedence",
    )
    same_file = apply_mod.plan_apply(profile, scope="repo", project_path=project, config_dir=claude_root / "claudeglass",
                                     claude_root=claude_root, snapshot=snap)
    assert same_file.overridden == ()


def test_the_apply_preview_says_a_project_files_flat_window_beats_a_models_own_window(tmp_path):
    # R2-4: within a file a model's own window wins; across files the higher file's flat window does.
    claude_root = tmp_path / ".claude"
    project = tmp_path / "proj"
    (project / ".claude").mkdir(parents=True)
    snap = _snapshot(effective={"autoCompactWindow": 400000}, effective_provenance={"autoCompactWindow": "project_shared"})
    profile = _profile(settings={"modelSettings": {"claude-opus-5-5": {"autoCompactWindow": 300000}}})
    plan = apply_mod.plan_apply(profile, scope="user", project_path=None, config_dir=claude_root / "claudeglass",
                                claude_root=claude_root, snapshot=snap)
    assert plan.overridden == (
        "modelSettings.claude-opus-5-5.autoCompactWindow: autoCompactWindow is set by this project's shared settings "
        "(.claude/settings.json), which takes precedence",
    )
    same_file = apply_mod.plan_apply(profile, scope="repo", project_path=project, config_dir=claude_root / "claudeglass",
                                     claude_root=claude_root, snapshot=snap)
    assert same_file.overridden == ()

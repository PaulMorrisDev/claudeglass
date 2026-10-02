"""``discovery.py``: slug derivation (incl. truncation), case-insensitive
project-dir de-dup, session window filters, and meta.json mapping.
"""

from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from claudeglass import discovery


# -- projects_root ---------------------------------------------------------


def test_projects_root_defaults_to_home_dot_claude(monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert discovery.projects_root() == Path.home() / ".claude" / "projects"


def test_projects_root_honours_claude_config_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    assert discovery.projects_root() == tmp_path / "projects"


# -- slug_for ---------------------------------------------------------------


def test_slug_for_replaces_non_alphanumerics():
    assert discovery.slug_for("C:\\Dev\\RevIXO") == "C--Dev-RevIXO"


def test_slug_for_short_path_is_unchanged_length():
    slug = discovery.slug_for("/home/me/project")
    assert len(slug) == len("/home/me/project")


def test_slug_for_truncates_long_paths_with_a_hash_suffix():
    long_path = "/" + ("a" * 300)
    slug = discovery.slug_for(long_path)
    assert len(slug) < len(long_path)
    # 200-char truncated prefix + "-" + 8 hex chars.
    prefix, _, suffix = slug.rpartition("-")
    assert len(prefix) == 200
    assert len(suffix) == 8
    assert all(c in "0123456789abcdef" for c in suffix)
    # Deterministic: same input, same slug.
    assert discovery.slug_for(long_path) == slug


def test_slug_for_honours_project_dir_name_override(monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/somewhere")
    monkeypatch.setenv("CLAUDE_CODE_PROJECT_DIR_NAME", "my-custom-slug")
    assert discovery.slug_for("C:\\Dev\\RevIXO") == "my-custom-slug"


def test_slug_for_honours_project_dir_name_even_without_config_dir(monkeypatch):
    # CLAUDE_CODE_PROJECT_DIR_NAME is an independent override, not
    # gated on CLAUDE_CONFIG_DIR also being set (fix: the two env vars
    # were previously wrongly coupled).
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_PROJECT_DIR_NAME", "my-custom-slug")
    assert discovery.slug_for("C:\\Dev\\RevIXO") == "my-custom-slug"


# -- resolve_project_dirs ----------------------------------------------------


def test_resolve_project_dirs_slug_matching_is_case_insensitive(tmp_path):
    (tmp_path / "C--Dev-RevIXO").mkdir()
    # Query with different casing than the on-disk directory name.
    result = discovery.resolve_project_dirs(tmp_path, slugs=["c--dev-revixo"])
    assert [p.name for p in result] == ["C--Dev-RevIXO"]


def test_resolve_project_dirs_dedups_by_realpath(tmp_path, monkeypatch):
    """A worktree slug and its parent project can realpath to the same
    directory (e.g. a worktree symlinked back to the main checkout).
    Exercised by faking ``os.path.realpath`` rather than creating an
    actual symlink, since Windows symlink creation needs a privilege this
    sandbox may not have — the de-dup logic under test is
    ``os.path.normcase(os.path.realpath(p))``, independent of *how* two
    directory entries end up pointing at the same target.
    """
    (tmp_path / "proj-a").mkdir()
    (tmp_path / "proj-a--claude-worktrees-foo-123456").mkdir()
    canonical = str(tmp_path / "canonical-target")
    monkeypatch.setattr(discovery.os.path, "realpath", lambda p: canonical)

    result = discovery.resolve_project_dirs(tmp_path, all_projects=True)
    assert len(result) == 1


def test_resolve_project_dirs_all_projects(tmp_path):
    (tmp_path / "proj-a").mkdir()
    (tmp_path / "proj-b").mkdir()
    (tmp_path / "not-a-dir.txt").write_text("x")
    result = discovery.resolve_project_dirs(tmp_path, all_projects=True)
    assert {p.name for p in result} == {"proj-a", "proj-b"}


def test_resolve_project_dirs_family_regex(tmp_path):
    (tmp_path / "C--Dev-RevIXO").mkdir()
    (tmp_path / "C--Dev-RevIXO--claude-worktrees-foo-abc123").mkdir()
    (tmp_path / "C--Dev-other-project").mkdir()
    result = discovery.resolve_project_dirs(tmp_path, family_regex="RevIXO")
    assert {p.name for p in result} == {"C--Dev-RevIXO", "C--Dev-RevIXO--claude-worktrees-foo-abc123"}


def test_resolve_project_dirs_missing_root_returns_empty(tmp_path):
    assert discovery.resolve_project_dirs(tmp_path / "does-not-exist", all_projects=True) == []


def test_resolve_project_dirs_no_selector_returns_empty(tmp_path):
    (tmp_path / "proj-a").mkdir()
    assert discovery.resolve_project_dirs(tmp_path) == []


def test_resolve_project_dirs_exclude_projects_filters_slug_selection(tmp_path):
    (tmp_path / "proj-a").mkdir()
    (tmp_path / "proj-b").mkdir()
    result = discovery.resolve_project_dirs(
        tmp_path, slugs=["proj-a", "proj-b"], exclude_projects=["^proj-b$"]
    )
    assert [p.name for p in result] == ["proj-a"]


def test_resolve_project_dirs_exclude_projects_filters_family_regex_selection(tmp_path):
    (tmp_path / "C--Dev-RevIXO").mkdir()
    (tmp_path / "C--Dev-RevIXO--claude-worktrees-foo-abc123").mkdir()
    result = discovery.resolve_project_dirs(
        tmp_path, family_regex="RevIXO", exclude_projects=["claude-worktrees"]
    )
    assert [p.name for p in result] == ["C--Dev-RevIXO"]


def test_resolve_project_dirs_exclude_projects_filters_all_projects_selection(tmp_path):
    (tmp_path / "proj-a").mkdir()
    (tmp_path / "scratch-throwaway").mkdir()
    result = discovery.resolve_project_dirs(
        tmp_path, all_projects=True, exclude_projects=["^scratch-"]
    )
    assert [p.name for p in result] == ["proj-a"]


def test_resolve_project_dirs_exclude_projects_is_case_insensitive(tmp_path):
    (tmp_path / "PROJ-A").mkdir()
    result = discovery.resolve_project_dirs(
        tmp_path, all_projects=True, exclude_projects=["proj-a"]
    )
    assert result == []


def test_resolve_project_dirs_exclude_projects_tolerates_malformed_regex(tmp_path):
    (tmp_path / "proj-a").mkdir()
    (tmp_path / "proj-b").mkdir()
    # An unbalanced group is invalid regex syntax; it must be skipped
    # rather than raising, while the well-formed entry after it still
    # applies (fix 6: "one bad entry in an exclude list shouldn't take
    # discovery down entirely").
    result = discovery.resolve_project_dirs(
        tmp_path, all_projects=True, exclude_projects=["(unbalanced", "^proj-b$"]
    )
    assert [p.name for p in result] == ["proj-a"]


def test_resolve_project_dirs_exclude_projects_none_or_empty_is_a_no_op(tmp_path):
    (tmp_path / "proj-a").mkdir()
    assert [p.name for p in discovery.resolve_project_dirs(tmp_path, all_projects=True, exclude_projects=None)] == [
        "proj-a"
    ]
    assert [p.name for p in discovery.resolve_project_dirs(tmp_path, all_projects=True, exclude_projects=[])] == [
        "proj-a"
    ]


# -- find_sessions -----------------------------------------------------------


def _touch_jsonl(path: Path, mtime: float | None = None) -> None:
    path.write_text('{"type":"user","timestamp":"2026-09-18T12:00:00.000Z"}\n')
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def test_find_sessions_sorts_newest_first_by_mtime(tmp_path):
    old = tmp_path / "old.jsonl"
    new = tmp_path / "new.jsonl"
    now = time.time()
    _touch_jsonl(old, mtime=now - 3600)
    _touch_jsonl(new, mtime=now)
    result = discovery.find_sessions(tmp_path, window_by="mtime")
    assert result == [new, old]


def test_find_sessions_limit(tmp_path):
    now = time.time()
    for i in range(5):
        _touch_jsonl(tmp_path / f"s{i}.jsonl", mtime=now - i)
    result = discovery.find_sessions(tmp_path, window_by="mtime", limit=2)
    assert len(result) == 2


def test_find_sessions_days_filter_excludes_old_files(tmp_path):
    now = time.time()
    recent = tmp_path / "recent.jsonl"
    stale = tmp_path / "stale.jsonl"
    _touch_jsonl(recent, mtime=now)
    _touch_jsonl(stale, mtime=now - 30 * 86400)
    result = discovery.find_sessions(tmp_path, days=7, window_by="mtime")
    assert result == [recent]


def test_find_sessions_window_by_timestamp_reads_first_line(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text(
        '{"type":"user","timestamp":"2020-01-01T00:00:00.000Z"}\n'
        '{"type":"assistant","timestamp":"2020-01-01T00:00:05.000Z"}\n'
    )
    # File mtime is "now", but window_by=timestamp must use the 2020 line,
    # so a since=2025 filter must exclude it.
    result = discovery.find_sessions(tmp_path, since="2025-01-01T00:00:00Z", window_by="timestamp")
    assert result == []
    result_all = discovery.find_sessions(tmp_path, window_by="timestamp")
    assert result_all == [path]


def test_find_sessions_since_bare_date_is_read_as_utc(tmp_path):
    # README documents --since DATE; a bare date parses without an offset
    # and used to crash comparing with the aware file times.
    now = time.time()
    recent = tmp_path / "recent.jsonl"
    stale = tmp_path / "stale.jsonl"
    _touch_jsonl(recent, mtime=now)
    _touch_jsonl(stale, mtime=now - 30 * 86400)
    since = time.strftime("%Y-%m-%d", time.gmtime(now - 7 * 86400))
    until = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now + 86400))
    assert discovery.find_sessions(tmp_path, since=since) == [recent]
    assert discovery.find_sessions(tmp_path, since=since, until=until, window_by="mtime") == [recent]


def test_find_sessions_missing_dir_returns_empty(tmp_path):
    assert discovery.find_sessions(tmp_path / "nope") == []


def test_find_sessions_unknown_window_by_raises(tmp_path):
    path = tmp_path / "s.jsonl"
    _touch_jsonl(path)
    with pytest.raises(ValueError):
        discovery.find_sessions(tmp_path, window_by="bogus")


# -- find_subagents / find_workflows -----------------------------------------


def test_find_subagents_pairs_jsonl_with_meta(tmp_path):
    session_id = "sess-1"
    subagents_dir = tmp_path / session_id / "subagents"
    subagents_dir.mkdir(parents=True)
    (subagents_dir / "agent-abc123.jsonl").write_text("")
    (subagents_dir / "agent-abc123.meta.json").write_text(json.dumps({"agentType": "claude-implementer"}))
    (subagents_dir / "agent-def456.jsonl").write_text("")  # no meta file

    result = discovery.find_subagents(tmp_path, session_id)
    assert len(result) == 2
    by_name = {p.name: meta for p, meta in result}
    assert by_name["agent-abc123.jsonl"] == {"agentType": "claude-implementer"}
    assert by_name["agent-def456.jsonl"] == {}


def test_find_subagents_missing_dir_returns_empty(tmp_path):
    assert discovery.find_subagents(tmp_path, "no-such-session") == []


def test_find_subagents_also_finds_workflow_nested_agents(tmp_path):
    session_id = "sess-1"
    subagents_dir = tmp_path / session_id / "subagents"
    subagents_dir.mkdir(parents=True)
    (subagents_dir / "agent-abc123.jsonl").write_text("")
    (subagents_dir / "agent-abc123.meta.json").write_text(json.dumps({"agentType": "claude-implementer"}))

    wf_dir = subagents_dir / "workflows" / "wf_run_a"
    wf_dir.mkdir(parents=True)
    (wf_dir / "agent-1.jsonl").write_text("")
    # Observed shape: workflow-nested meta carries no toolUseId.
    (wf_dir / "agent-1.meta.json").write_text(json.dumps({"agentType": "claude-implementer"}))
    (wf_dir / "agent-2.jsonl").write_text("")  # no meta file

    result = discovery.find_subagents(tmp_path, session_id)
    names = {p.name for p, _meta in result}
    assert names == {"agent-abc123.jsonl", "agent-1.jsonl", "agent-2.jsonl"}


def test_find_subagent_paths_lists_without_reading_meta(tmp_path, monkeypatch):
    session_id = "sess-1"
    subagents_dir = tmp_path / session_id / "subagents"
    subagents_dir.mkdir(parents=True)
    (subagents_dir / "agent-b.jsonl").write_text("")
    (subagents_dir / "agent-a.jsonl").write_text("")
    (subagents_dir / "agent-a.meta.json").write_text(json.dumps({"agentType": "claude-implementer"}))
    wf_dir = subagents_dir / "workflows" / "wf_run_a"
    wf_dir.mkdir(parents=True)
    (wf_dir / "agent-1.jsonl").write_text("")
    expected = [path for path, _meta in discovery.find_subagents(tmp_path, session_id)]
    monkeypatch.setattr(discovery, "_read_meta_dict", lambda _path: pytest.fail("meta read"))

    assert discovery.find_subagent_paths(tmp_path, session_id) == expected
    assert [path.name for path in expected] == ["agent-a.jsonl", "agent-b.jsonl", "agent-1.jsonl"]


def test_find_subagents_rejects_unknown_subagent_window(tmp_path):
    with pytest.raises(ValueError):
        discovery.find_subagents(tmp_path, "sess-1", subagent_window="bogus")


def _write_subagent_transcript(dir_: Path, name: str, timestamp: str) -> Path:
    dir_.mkdir(parents=True, exist_ok=True)
    path = dir_ / f"{name}.jsonl"
    path.write_text(f'{{"type":"user","timestamp":"{timestamp}"}}\n', encoding="utf-8")
    return path


def test_find_subagents_parent_window_ignores_own_timestamp(tmp_path):
    # Default subagent_window="parent": since/until are accepted but have
    # no effect -- every subagent of the session is returned regardless
    # of its own timestamp (seed-script parity).
    session_id = "sess-1"
    subagents_dir = tmp_path / session_id / "subagents"
    _write_subagent_transcript(subagents_dir, "agent-old", "2020-01-01T00:00:00.000Z")

    result = discovery.find_subagents(
        tmp_path, session_id, since="2025-01-01T00:00:00Z", subagent_window="parent"
    )
    assert {p.name for p, _meta in result} == {"agent-old.jsonl"}


def test_find_subagents_own_window_filters_by_own_timestamp(tmp_path):
    session_id = "sess-1"
    subagents_dir = tmp_path / session_id / "subagents"
    _write_subagent_transcript(subagents_dir, "agent-old", "2020-01-01T00:00:00.000Z")
    _write_subagent_transcript(subagents_dir, "agent-new", "2026-06-01T00:00:00.000Z")

    result = discovery.find_subagents(
        tmp_path, session_id, since="2025-01-01T00:00:00Z", subagent_window="own"
    )
    assert {p.name for p, _meta in result} == {"agent-new.jsonl"}


def test_filter_subagents_by_window_standalone(tmp_path):
    old_path = _write_subagent_transcript(tmp_path, "agent-old", "2020-01-01T00:00:00.000Z")
    new_path = _write_subagent_transcript(tmp_path, "agent-new", "2026-06-01T00:00:00.000Z")
    pairs = [(old_path, {}), (new_path, {})]

    kept = discovery.filter_subagents_by_window(pairs, since="2025-01-01T00:00:00Z", until=None)
    assert kept == [(new_path, {})]

    assert discovery.filter_subagents_by_window(pairs, None, None) == pairs


def test_find_workflows_lists_wf_json_files(tmp_path):
    session_id = "sess-1"
    workflows_dir = tmp_path / session_id / "workflows"
    workflows_dir.mkdir(parents=True)
    (workflows_dir / "wf_001.json").write_text("{}")
    (workflows_dir / "not-a-workflow.json").write_text("{}")
    result = discovery.find_workflows(tmp_path, session_id)
    assert [p.name for p in result] == ["wf_001.json"]


# -- load_meta ----------------------------------------------------------------


def test_load_meta_maps_documented_keys(tmp_path):
    meta_path = tmp_path / "agent-abc.meta.json"
    meta_path.write_text(
        json.dumps(
            {
                "agentType": "claude-implementer",
                "description": "a fairly long description of the task at hand",
                "spawnDepth": 2,
                "parentAgentId": "agent-parent-1",
                "model": "sonnet",
                "requestShape": "isolated",
                "worktreeBranch": "wp1-parse",
                "stoppedByUser": False,
                "toolUseId": "tu_01abc",
            }
        )
    )
    meta = discovery.load_meta(meta_path)
    assert meta.kind == "subagent"
    assert meta.agent_type == "claude-implementer"
    assert meta.description_len == len("a fairly long description of the task at hand")
    assert meta.spawn_depth == 2
    assert meta.parent_agent_id == "agent-parent-1"
    assert meta.agent_model_alias == "sonnet"
    assert meta.request_shape == "isolated"
    assert meta.worktree_branch_present is True  # bool only, never the branch name
    assert meta.stopped_by_user is False
    assert meta.tool_use_id == "tu_01abc"
    assert meta.provider == "anthropic"  # batch C: "sonnet" matches no cloud-provider form


def test_load_meta_derives_provider_from_bedrock_model_alias(tmp_path):
    meta_path = tmp_path / "agent-abc.meta.json"
    meta_path.write_text(json.dumps({"model": "us.anthropic.claude-sonnet-5-20260101-v1:0"}))
    meta = discovery.load_meta(meta_path)
    assert meta.provider == "bedrock"


def test_load_meta_derives_provider_from_vertex_model_alias(tmp_path):
    meta_path = tmp_path / "agent-abc.meta.json"
    meta_path.write_text(json.dumps({"model": "claude-sonnet-5@20260101"}))
    meta = discovery.load_meta(meta_path)
    assert meta.provider == "vertex"


def test_load_meta_no_model_leaves_provider_unset(tmp_path):
    meta_path = tmp_path / "agent-abc.meta.json"
    meta_path.write_text(json.dumps({"agentType": "claude-implementer"}))
    meta = discovery.load_meta(meta_path)
    assert meta.provider is None


def test_load_meta_derives_agent_id_and_session_id_from_path(tmp_path):
    # Realistic layout: <projects_root>/<slug>/<session_id>/subagents/agent-<hex>.meta.json
    subagents_dir = tmp_path / "session-xyz-789" / "subagents"
    subagents_dir.mkdir(parents=True)
    meta_path = subagents_dir / "agent-deadbeef.meta.json"
    meta_path.write_text(json.dumps({"agentType": "claude-implementer"}))

    meta = discovery.load_meta(meta_path)
    assert meta.agent_id == "agent-deadbeef"
    assert meta.session_id == "session-xyz-789"
    assert meta.workflow_run_id is None
    assert meta.kind == "subagent"


def test_load_meta_derives_session_id_and_run_id_for_workflow_nested_agent(tmp_path):
    # Layout: <session_id>/subagents/workflows/<run_id>/agent-<hex>.meta.json
    run_dir = tmp_path / "session-xyz-789" / "subagents" / "workflows" / "wf_run_a"
    run_dir.mkdir(parents=True)
    meta_path = run_dir / "agent-1.meta.json"
    meta_path.write_text(json.dumps({"agentType": "claude-implementer"}))

    meta = discovery.load_meta(meta_path)
    assert meta.agent_id == "agent-1"
    assert meta.session_id == "session-xyz-789"  # not "workflows" (the buggy shallow derivation)
    assert meta.workflow_run_id == "wf_run_a"


@pytest.mark.parametrize("agent_type", ["workflow-subagent", "code-reviewer"])
def test_load_meta_gives_a_workflow_nested_agent_its_own_kind_and_keeps_its_type(tmp_path, agent_type):
    # A named agent a workflow starts keeps its type; an unnamed one is
    # "workflow-subagent". Either way its kind says it ran under a workflow.
    run_dir = tmp_path / "session-xyz-789" / "subagents" / "workflows" / "wf_run_a"
    run_dir.mkdir(parents=True)
    meta_path = run_dir / "agent-1.meta.json"
    meta_path.write_text(json.dumps({"agentType": agent_type}))

    meta = discovery.load_meta(meta_path)
    assert meta.kind == "workflow-agent"
    assert meta.agent_type == agent_type


def test_load_meta_sets_path_mtime_and_size_from_sibling_transcript(tmp_path):
    subagents_dir = tmp_path / "session-xyz" / "subagents"
    subagents_dir.mkdir(parents=True)
    jsonl_path = subagents_dir / "agent-abc.jsonl"
    jsonl_path.write_text('{"type":"user","timestamp":"2026-01-01T00:00:00.000Z"}\n', encoding="utf-8")
    meta_path = subagents_dir / "agent-abc.meta.json"
    meta_path.write_text(json.dumps({"agentType": "claude-implementer"}))

    meta = discovery.load_meta(meta_path)
    assert meta.path == str(jsonl_path)
    assert meta.size_bytes == jsonl_path.stat().st_size
    assert meta.mtime_ns == jsonl_path.stat().st_mtime_ns
    assert meta.size_bytes > 0


def test_load_meta_mtime_and_size_are_zero_when_transcript_file_missing(tmp_path):
    meta_path = tmp_path / "agent-ghost.meta.json"
    meta_path.write_text(json.dumps({"agentType": "claude-implementer"}))

    meta = discovery.load_meta(meta_path)
    assert meta.path == str(tmp_path / "agent-ghost.jsonl")
    assert meta.mtime_ns == 0
    assert meta.size_bytes == 0


def test_load_meta_missing_file_returns_defaults(tmp_path):
    meta = discovery.load_meta(tmp_path / "does-not-exist.meta.json")
    assert meta.kind == "subagent"
    assert meta.agent_type is None
    assert meta.worktree_branch_present is False


def test_load_meta_malformed_json_returns_defaults(tmp_path):
    meta_path = tmp_path / "bad.meta.json"
    meta_path.write_text("{not valid json")
    meta = discovery.load_meta(meta_path)
    assert meta.kind == "subagent"
    assert meta.agent_type is None


def test_load_meta_never_stores_worktree_branch_name(tmp_path):
    meta_path = tmp_path / "agent-abc.meta.json"
    meta_path.write_text(json.dumps({"worktreeBranch": "some-branch-name-that-must-not-leak"}))
    meta = discovery.load_meta(meta_path)
    assert meta.worktree_branch_present is True
    # Only a bool field exists for this on TranscriptMeta — there's no
    # attribute to have leaked the branch name into.
    assert not hasattr(meta, "worktree_branch")


def _workflow_agent(tmp_path, status, state, agent="a1b2c3"):
    session = tmp_path / "session-wf"
    run_dir = session / "subagents" / "workflows" / "wf_0001"
    run_dir.mkdir(parents=True)
    (session / "workflows").mkdir()
    (session / "workflows" / "wf_0001.json").write_text(json.dumps({
        "runId": "wf_0001",
        "status": status,
        "workflowProgress": [
            {"type": "workflow_phase", "index": 1, "title": "Review"},
            {"type": "workflow_agent", "agentId": agent, "state": state, "promptPreview": "never kept"},
        ],
    }))
    meta_path = run_dir / f"agent-{agent}.meta.json"
    meta_path.write_text(json.dumps({"agentType": "workflow-subagent"}))
    return discovery.load_meta(meta_path)


@pytest.mark.parametrize("status, state, expected", [
    ("completed", "done", "done"),
    ("killed", "progress", "progress"),
    ("completed", "error", "error"),
    ("running", "progress", None),  # could still change
])
def test_load_meta_reads_a_workflow_agents_end_state_from_its_finished_run(tmp_path, status, state, expected):
    meta = _workflow_agent(tmp_path, status, state)
    assert meta.workflow_run_id == "wf_0001"
    assert meta.workflow_agent_state == expected
    assert "never kept" not in repr(meta)


def _workflow_meta_path(tmp_path, meta_body):
    run_dir = tmp_path / "session-roles" / "subagents" / "workflows" / "wf_x"
    run_dir.mkdir(parents=True)
    meta_path = run_dir / "agent-a1.meta.json"
    meta_path.write_text(json.dumps(meta_body))
    return meta_path


def test_load_meta_keeps_a_workflow_agents_role_word_and_none_of_its_label(tmp_path):
    meta_path = _workflow_meta_path(
        tmp_path,
        {"agentType": "workflow-subagent", "workflowPhase": "Implement", "description": "impl:C:/Users/x/secret.py"},
    )
    meta = discovery.load_meta(meta_path)
    assert meta.kind == "workflow-agent"
    assert meta.role_word == "implement"
    assert meta.model_recorded is True  # a new-shape meta, and it names no model
    assert meta.agent_model_alias is None
    assert "secret" not in repr(meta)
    assert "C:/" not in repr(meta)


def test_load_meta_an_old_shape_workflow_meta_has_no_role_and_records_no_model(tmp_path):
    meta = discovery.load_meta(_workflow_meta_path(tmp_path, {"agentType": "workflow-subagent", "spawnDepth": 1}))
    assert meta.role_word is None
    assert meta.model_recorded is False


def test_load_meta_an_agent_tool_meta_takes_its_role_from_the_description(tmp_path):
    meta_path = tmp_path / "agent-abc.meta.json"
    meta_path.write_text(
        json.dumps({"agentType": "general-purpose", "description": "Fix the parser", "model": "sonnet"})
    )
    meta = discovery.load_meta(meta_path)
    assert meta.role_word == "fix"
    assert meta.model_recorded is True
    assert meta.agent_model_alias == "sonnet"


def test_load_meta_a_fork_meta_has_no_role_word(tmp_path):
    meta_path = tmp_path / "agent-abc.meta.json"
    meta_path.write_text(json.dumps({"agentType": "fork", "description": "A side question about the config"}))
    meta = discovery.load_meta(meta_path)
    assert meta.role_word is None
    assert meta.model_recorded is True


def test_load_meta_a_named_type_gives_the_role_when_the_label_has_none(tmp_path):
    meta_path = tmp_path / "agent-abc.meta.json"
    meta_path.write_text(json.dumps({"agentType": "claude-implementer", "description": "Parser work"}))
    assert discovery.load_meta(meta_path).role_word == "implement"


def test_load_meta_the_phase_wins_over_the_description(tmp_path):
    meta_path = tmp_path / "agent-abc.meta.json"
    meta_path.write_text(json.dumps({"workflowPhase": "Verify", "description": "Implement the change"}))
    assert discovery.load_meta(meta_path).role_word == "verify"


@pytest.mark.parametrize("body", [{"description": None}, {"workflowPhase": ""}, {"description": 7}])
def test_load_meta_model_recorded_is_key_presence_whatever_the_value(tmp_path, body):
    meta_path = tmp_path / "agent-abc.meta.json"
    meta_path.write_text(json.dumps(body))
    meta = discovery.load_meta(meta_path)
    assert meta.model_recorded is True
    assert meta.role_word is None


def test_load_meta_a_missing_or_malformed_meta_has_no_role_and_records_no_model(tmp_path):
    bad = tmp_path / "bad.meta.json"
    bad.write_text("{not valid json")
    for path in (bad, tmp_path / "does-not-exist.meta.json"):
        meta = discovery.load_meta(path)
        assert meta.role_word is None
        assert meta.model_recorded is False


# -- calendar windows: window_start and its zone helpers ---------------------

_HOUR = timedelta(hours=1)
_IST = timezone(timedelta(hours=5, minutes=30))


def _utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=timezone.utc)


def _tzdata_has(name: str) -> bool:
    """Same helper as ``tests/test_classify.py`` and ``tests/test_monthly.py``:
    a bare Windows install has no ``tzdata`` for ``zoneinfo`` to read."""
    try:
        ZoneInfo(name)
        return True
    except ZoneInfoNotFoundError:
        return False


class _DstZone(tzinfo):
    """A time zone with one summer-time stretch, written out by hand so the
    tests need no ``tzdata``. The clocks go forward at the naive UTC instant
    ``forward`` and back at ``back``; ``std`` is the winter offset. Follows
    PEP 495 across the clock changes: a time that doesn't exist or happens
    twice takes its offset from its ``fold``."""

    def __init__(self, std: timedelta, forward: datetime, back: datetime) -> None:
        self.std, self.forward, self.back = std, forward, back

    def utcoffset(self, dt):
        wall = dt.replace(tzinfo=None)
        jump = self.forward + self.std  # the wall time the clocks jump from
        repeat = self.back + self.std  # the wall time the repeated hour starts at
        if wall < jump:
            return self.std
        if wall < jump + _HOUR:  # the hour that doesn't exist
            return self.std + _HOUR if dt.fold else self.std
        if wall < repeat:
            return self.std + _HOUR
        if wall < repeat + _HOUR:  # the hour that happens twice
            return self.std if dt.fold else self.std + _HOUR
        return self.std

    def dst(self, dt):
        return self.utcoffset(dt) - self.std

    def tzname(self, dt):
        return "summer" if self.dst(dt) else "winter"

    def fromutc(self, dt):
        instant = dt.replace(tzinfo=None)
        if self.forward <= instant < self.back:
            return (instant + self.std + _HOUR).replace(tzinfo=self)
        local = (instant + self.std).replace(tzinfo=self)
        return local.replace(fold=1) if self.back <= instant < self.back + _HOUR else local


#: London's 2026 rules: forward at 01:00 UTC on 29 March, back at 01:00 UTC
#: on 25 October.
_LONDON = _DstZone(timedelta(0), datetime(2026, 3, 29, 1), datetime(2026, 10, 25, 1))
#: A zone whose summer time starts at midnight (as Santiago's does): the
#: clocks jump from 00:00 to 01:00, so that midnight never happens.
_MIDNIGHT_JUMP = _DstZone(timedelta(hours=-4), datetime(2026, 9, 6, 4), datetime(2027, 4, 4, 3))


class _FrozenDatetime(datetime):
    """``discovery.datetime`` with a fixed "now", as ``test_service_api.py``
    freezes it."""

    @classmethod
    def now(cls, tz=None):
        return _utc(2026, 10, 1, 15, 0)


def test_window_start_is_local_midnight_of_the_first_day():
    now = _utc(2026, 10, 1, 15, 0)
    assert discovery.window_start(7, "UTC", now=now) == _utc(2026, 9, 25)
    assert discovery.window_start(1, "UTC", now=now) == _utc(2026, 10, 1)
    assert discovery.window_start(30, "UTC", now=now) == _utc(2026, 9, 2)
    # Just after midnight is still the day it just became.
    assert discovery.window_start(1, "UTC", now=_utc(2026, 10, 1, 0, 0)) == _utc(2026, 10, 1)
    assert discovery.window_start(1, "UTC", now=_utc(2026, 9, 30, 23, 59)) == _utc(2026, 9, 30)
    assert discovery.window_start(7, "UTC", now=now).tzinfo is timezone.utc


def test_window_start_iso_is_the_minute_shaped_utc_string():
    now = _utc(2026, 10, 1, 15, 37)
    assert discovery.window_start_iso(7, "UTC", now=now) == "2026-09-25T00:00:00Z"
    assert discovery.window_start_iso(1, _IST, now=now) == "2026-09-30T18:30:00Z"


@pytest.mark.parametrize("days", [0, -1, -30])
def test_window_start_needs_at_least_one_day(days):
    with pytest.raises(ValueError):
        discovery.window_start(days, "UTC")
    with pytest.raises(ValueError):
        discovery.window_start_iso(days, "UTC")


def test_window_start_follows_the_clocks_going_forward():
    now = _utc(2026, 3, 30, 12, 0)  # 13:00 on the 30th, an hour after the change
    assert discovery.window_start(1, _LONDON, now=now) == _utc(2026, 3, 29, 23, 0)
    assert discovery.window_start(7, _LONDON, now=now) == _utc(2026, 3, 24, 0, 0)
    # The day the clocks went forward has 23 hours.
    midnight = discovery.local_midnight
    assert midnight(date(2026, 3, 30), _LONDON) - midnight(date(2026, 3, 29), _LONDON) == timedelta(hours=23)
    # Before the change on the 29th it is still winter time.
    assert discovery.window_start(1, _LONDON, now=_utc(2026, 3, 29, 0, 30)) == _utc(2026, 3, 29, 0, 0)
    assert discovery.local_day("2026-03-29T23:30:00Z", _LONDON) == "2026-03-30"


def test_window_start_follows_the_clocks_going_back():
    now = _utc(2026, 10, 26, 12, 0)
    assert discovery.window_start(1, _LONDON, now=now) == _utc(2026, 10, 26, 0, 0)
    assert discovery.window_start(2, _LONDON, now=now) == _utc(2026, 10, 24, 23, 0)
    assert discovery.window_start(3, _LONDON, now=now) == _utc(2026, 10, 23, 23, 0)
    # The day the clocks went back has 25 hours.
    midnight = discovery.local_midnight
    assert midnight(date(2026, 10, 26), _LONDON) - midnight(date(2026, 10, 25), _LONDON) == timedelta(hours=25)
    # The repeated hour is one day, whichever side of it a reply falls on.
    assert discovery.local_day("2026-10-25T00:30:00Z", _LONDON) == "2026-10-25"
    assert discovery.local_day("2026-10-25T01:30:00Z", _LONDON) == "2026-10-25"
    assert discovery.local_day("2026-10-25T23:59:00Z", _LONDON) == "2026-10-25"
    assert discovery.local_day("2026-10-26T00:00:00Z", _LONDON) == "2026-10-26"


def test_local_midnight_that_never_happens_is_the_first_moment_of_the_day():
    # The clocks jump from 00:00 to 01:00 on 6 September, at 04:00 UTC.
    first = discovery.local_midnight(date(2026, 9, 6), _MIDNIGHT_JUMP)
    assert first == _utc(2026, 9, 6, 4, 0)
    assert discovery.local_midnight(date(2026, 9, 7), _MIDNIGHT_JUMP) == _utc(2026, 9, 7, 3, 0)
    assert discovery.window_start(1, _MIDNIGHT_JUMP, now=_utc(2026, 9, 6, 20, 0)) == first


def test_window_start_in_a_half_hour_zone():
    # 20:00 UTC is 01:30 on 2 October in India, so the day began at 18:30 UTC.
    now = _utc(2026, 10, 1, 20, 0)
    assert discovery.window_start(1, _IST, now=now) == _utc(2026, 10, 1, 18, 30)
    assert discovery.window_start(7, _IST, now=now) == _utc(2026, 9, 25, 18, 30)
    assert discovery.local_day("2026-09-18T23:45:00Z", _IST) == "2026-09-19"
    assert discovery.local_day("2026-09-18T18:29:00Z", _IST) == "2026-09-18"
    assert discovery.local_day("2026-09-18T18:30:00Z", _IST) == "2026-09-19"
    assert discovery.local_day("2026-09-18T18:30:00Z", "UTC") == "2026-09-18"


def test_local_day_reads_a_time_with_no_offset_as_utc():
    assert discovery.local_day("2026-09-18T23:45:00", _IST) == "2026-09-19"
    assert discovery.local_day("2026-09-18", _IST) == "2026-09-18"
    assert discovery.local_day(datetime(2026, 9, 18, 23, 45), _IST) == "2026-09-19"
    assert discovery.local_day(_utc(2026, 9, 18, 23, 45), _IST) == "2026-09-19"
    assert discovery.local_day("2026-09-18T23:45:00+05:30", "UTC") == "2026-09-18"
    with pytest.raises(ValueError):
        discovery.local_day("not a time", "UTC")


def test_window_start_in_the_machine_zone():
    now = _utc(2026, 10, 1, 15, 0)
    local_today = now.astimezone().date()
    expected = datetime.combine(local_today - timedelta(days=6), datetime.min.time()).astimezone(timezone.utc)
    assert discovery.window_start(7, None, now=now) == expected
    assert discovery.window_start(7, "", now=now) == expected
    assert discovery.window_start(7, now=now) == expected
    assert discovery.to_local(now, None) == now.astimezone()
    assert discovery.local_day(now, None) == local_today.isoformat()


def test_a_zone_that_cannot_be_resolved_is_the_machine_zone():
    now = _utc(2026, 10, 1, 15, 0)
    assert discovery.window_start(7, "Mars/Olympus", now=now) == discovery.window_start(7, None, now=now)
    assert discovery.local_day(now, "Mars/Olympus") == discovery.local_day(now, None)
    assert discovery.zone_name("Mars/Olympus") is None
    assert discovery.zone_name(None) is None
    assert discovery.zone_name("") is None


def test_a_zone_lookup_that_fails_in_any_way_is_the_machine_zone(monkeypatch):
    # ZoneInfo raises more than ZoneInfoNotFoundError for a bad key: a
    # folder where a zone file should be, an unreadable file.
    def fail(key):
        raise IsADirectoryError(key)

    monkeypatch.setattr(discovery, "ZoneInfo", fail)
    assert discovery._zone("America") is None
    now = _utc(2026, 10, 1, 15, 0)
    assert discovery.window_start(7, "America", now=now) == discovery.window_start(7, None, now=now)
    # UTC and a tzinfo never reach the lookup.
    assert discovery._zone("UTC") is timezone.utc
    assert discovery._zone(_LONDON) is _LONDON


def test_utc_resolves_without_tzdata(monkeypatch):
    def fail(key):
        raise ZoneInfoNotFoundError(key)

    monkeypatch.setattr(discovery, "ZoneInfo", fail)
    assert discovery._zone("UTC") is timezone.utc
    assert discovery._zone("Etc/UTC") is timezone.utc
    assert discovery.zone_name("UTC") == "UTC"
    assert discovery.zone_name("Etc/UTC") == "Etc/UTC"
    assert discovery.window_start(2, "Etc/UTC", now=_utc(2026, 10, 1, 15, 0)) == _utc(2026, 9, 30)


def test_zone_name_gives_the_iana_name():
    assert discovery.zone_name("UTC") == "UTC"
    assert discovery.zone_name(timezone.utc) == "UTC"
    # A fixed offset or a hand-written zone has no IANA name.
    assert discovery.zone_name(_IST) is None
    assert discovery.zone_name(_LONDON) is None


@pytest.mark.skipif(not _tzdata_has("Europe/London"), reason="no tzdata for Europe/London on this machine")
def test_window_start_in_a_named_zone_across_the_clock_changes():
    assert discovery.zone_name("Europe/London") == "Europe/London"
    now = _utc(2026, 3, 30, 12, 0)
    assert discovery.window_start(1, "Europe/London", now=now) == _utc(2026, 3, 29, 23, 0)
    assert discovery.window_start(7, "Europe/London", now=now) == _utc(2026, 3, 24, 0, 0)
    now = _utc(2026, 10, 26, 12, 0)
    assert discovery.window_start(2, "Europe/London", now=now) == _utc(2026, 10, 24, 23, 0)
    assert discovery.local_day("2026-03-29T23:30:00Z", "Europe/London") == "2026-03-30"


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="the machine zone can only be changed with tzset")
def test_window_start_in_the_machine_zone_across_the_clock_changes():
    # A POSIX rule string, so no zoneinfo files are needed: GMT, and BST from
    # the last Sunday in March (01:00 UTC) to the last Sunday in October.
    saved = os.environ.get("TZ")
    os.environ["TZ"] = "GMT0BST,M3.5.0/1,M10.5.0"
    time.tzset()
    try:
        assert discovery.window_start(1, None, now=_utc(2026, 3, 30, 12, 0)) == _utc(2026, 3, 29, 23, 0)
        assert discovery.window_start(7, None, now=_utc(2026, 3, 30, 12, 0)) == _utc(2026, 3, 24, 0, 0)
        assert discovery.window_start(2, None, now=_utc(2026, 10, 26, 12, 0)) == _utc(2026, 10, 24, 23, 0)
        assert discovery.window_start(1, None, now=_utc(2026, 10, 26, 12, 0)) == _utc(2026, 10, 26, 0, 0)
    finally:
        if saved is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = saved
        time.tzset()


def test_window_start_uses_the_clock_discovery_has_when_it_is_not_given_one(monkeypatch):
    monkeypatch.setattr(discovery, "datetime", _FrozenDatetime)
    assert discovery.window_start(7, "UTC") == _utc(2026, 9, 25)
    assert discovery.window_start_iso(1, _IST) == "2026-09-30T18:30:00Z"


def test_resolve_window_stays_rolling():
    # The replays that divide by their own length (capture, the baseline)
    # need N x 24 hours, not calendar days; the dashboard passes a calendar
    # ``since`` instead.
    since_dt, until_dt = discovery._resolve_window(7, None, None)
    assert until_dt is None
    assert abs((datetime.now(timezone.utc) - since_dt) - timedelta(days=7)) < timedelta(minutes=1)


def test_resolve_window_rolls_back_from_now_not_from_midnight(monkeypatch):
    monkeypatch.setattr(discovery, "datetime", _FrozenDatetime)
    since_dt, until_dt = discovery._resolve_window(7, None, None)
    assert since_dt == _utc(2026, 9, 24, 15, 0)
    assert since_dt != discovery.window_start(7, "UTC")
    assert until_dt is None


def test_resolve_window_lets_since_win_over_days():
    since_dt, until_dt = discovery._resolve_window(7, "2026-09-01T06:30:00Z", "2026-09-02")
    assert since_dt == _utc(2026, 9, 1, 6, 30)
    assert until_dt == _utc(2026, 9, 2)
    assert discovery._resolve_window(None, None, None) == (None, None)


def test_the_zone_helpers_are_exported():
    for name in ("to_local", "local_day", "zone_name", "local_midnight", "window_start", "window_start_iso"):
        assert name in discovery.__all__

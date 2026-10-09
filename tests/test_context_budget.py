"""Tests for S1-context-budget's context-budget analytics
(src/claudeglass/context_budget.py).

Fixtures are synthetic, built at test time via ``tests/helpers`` (matching
this codebase's established convention -- see e.g. ``test_compaction.py``'s
own module docstring), plus one smoke test over the committed real
fixture at ``tests/fixtures/real/session-a``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from claudeglass import calibration, context_budget, helptext, statusline, tool_search
from claudeglass.model import TranscriptMeta
from claudeglass.parse import parse_transcript
from claudeglass.pricing import load_pricing
from claudeglass.snapshots import Snapshot

PRICING = load_pricing()

from helpers import (
    assert_privacy,
    attachment_line,
    system_line,
    tool_use_block,
    turn_line,
    user_str_line,
    write_jsonl,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "real" / "session-a"


def _build_session(
    tmp_path: Path,
    name: str,
    *,
    session_id: str,
    human_text: str = "please fix the bug",
    with_skill_listing: bool = False,
    skill_listing_chars: int = 400,
    baseline_cache_creation: int = 40_000,
    model: str = "claude-sonnet-5",
    with_compaction: bool = False,
):
    """One top-level transcript: an optional ``skill_listing`` attachment,
    a human-text prompt, then one priced first turn carrying
    ``baseline_cache_creation`` -- optionally followed by an
    auto-triggered ``compact_boundary`` and a second turn, for the
    autocompact table's tests.
    """
    lines = []
    if with_skill_listing:
        # attachment_line's **overrides merge into the nested "attachment"
        # dict, not the top-level line -- set "timestamp" directly on the
        # returned dict to backdate it ahead of the first turn.
        skill_line = attachment_line("skill_listing", rendered="x" * skill_listing_chars)
        skill_line["timestamp"] = "2026-09-18T11:59:00.000Z"
        lines.append(skill_line)
    lines.append(user_str_line(human_text, timestamp="2026-09-18T11:59:30.000Z"))
    lines.append(
        turn_line(
            model=model,
            timestamp="2026-09-18T12:00:00.000Z",
            cache_creation_input_tokens=baseline_cache_creation,
        )
    )
    if with_compaction:
        lines.append(
            system_line(
                "compact_boundary",
                timestamp="2026-09-18T12:05:00.000Z",
                compactMetadata={"trigger": "auto", "preTokens": 180_000, "postTokens": 20_000},
            )
        )
        lines.append(turn_line(model=model, timestamp="2026-09-18T12:05:30.000Z"))
    path = tmp_path / f"{name}.jsonl"
    write_jsonl(path, lines)
    return parse_transcript(path, TranscriptMeta(path=str(path), session_id=session_id))


def _snapshot(project_slug: str, **data) -> Snapshot:
    payload = {"project_slug": project_slug}
    payload.update(data)
    return Snapshot(path=Path("snap.json"), ts="2026-09-18T00:00:00.000Z", data=payload)


# -- skip path --------------------------------------------------------------


def test_build_section_skips_cleanly_with_no_sessions():
    stats = context_budget.ContextBudgetStats()
    section = context_budget.build_section(stats)
    assert section.key == "context_budget"
    assert section.tables == []
    assert len(section.notes) == 1
    assert "No main sessions" in section.notes[0]


# -- baseline table -----------------------------------------------------


def test_baseline_table_human_prompt_and_skills_listing_estimates(tmp_path):
    top = _build_session(
        tmp_path,
        "s1",
        session_id="sess_1",
        human_text="x" * 40,  # 40 chars -> 10 tokens
        with_skill_listing=True,
        skill_listing_chars=400,  # -> 100 tokens
        baseline_cache_creation=40_000,
    )
    stats = context_budget.ContextBudgetStats()
    stats.add_session("proj-a", top)

    section = context_budget.build_section(stats)
    table = next(t for t in section.tables if t.name == "context_budget_baseline")
    col = {c.key: i for i, c in enumerate(table.columns)}
    for label in table.columns:
        assert label.key == label.key.strip()

    row_by_project = {row[col["project"]]: row for row in table.rows}
    assert "all" in row_by_project
    assert "proj-a" in row_by_project

    proj_row = row_by_project["proj-a"]
    # The first call is everything it read: 100 uncached input tokens and a
    # 40,000-token cache write, split into the three parts it is made of.
    assert proj_row[col["mean_baseline"]] == pytest.approx(40_100)
    assert proj_row[col["median_baseline"]] == pytest.approx(40_100)
    assert proj_row[col["shared_prefix"]] == pytest.approx(0)
    assert proj_row[col["session_written"]] == pytest.approx(40_000)
    assert proj_row[col["first_prompt"]] == pytest.approx(100)
    assert proj_row[col["human_prompt_est"]] == pytest.approx(10.0)
    assert proj_row[col["skills_listing_est"]] == pytest.approx(100.0)
    # No snapshot: memory/agents/mcp all null.
    assert proj_row[col["memory_files_est"]] is None
    assert proj_row[col["custom_agents_est"]] is None
    assert proj_row[col["mcp_tools_est"]] is None
    # Residual = 40100 - (10 + 100) = 39990.
    assert proj_row[col["system_prompt_and_tools_est"]] == pytest.approx(39_990.0)
    # No snapshot and no MCP server: nothing to change, apart from the skills list.
    assert proj_row[col["mcp_tools_tokens"]] is None
    assert proj_row[col["mcp_removable_tokens"]] is None
    assert proj_row[col["controllable_est"]] == pytest.approx(100.0)

    every_column_label_ends_est = all(
        c.label.endswith("(est)") or c.key
        in ("project", "sessions", "mean_baseline", "median_baseline", "shared_prefix", "session_written", "first_prompt")
        for c in table.columns
    )
    assert every_column_label_ends_est

    # Every note documents the characters/4 approximation.
    assert any("characters" in n and "4" in n for n in table.notes)
    assert_privacy(section)


def test_the_mcp_part_you_can_change_leaves_out_the_desktop_apps_own_servers():
    first = SimpleNamespace(
        model="claude-sonnet-5",
        deferred_list_chars_by_server={"computer-use": 4_000, "github": 400},
        mcp_instruction_chars_by_server={"ccd_session": 800},
    )
    top = SimpleNamespace(upfront_definition_chars_by_server={})
    found = calibration.Calibration()
    assert context_budget._mcp_tools_tokens(top, first, found) == pytest.approx(1_300)
    assert context_budget._mcp_tools_tokens(top, first, found, skip_built_in=True) == pytest.approx(100)


def test_baseline_table_residual_is_none_when_est_exceeds_baseline(tmp_path):
    """Fix #24: a baseline smaller than the known (est) buckets must never
    silently report "the system prompt costs 0 tokens" -- that reads as a
    measurement, not as "the estimates over-shot". None is reported
    instead, with a note explaining why."""
    top = _build_session(
        tmp_path,
        "s1",
        session_id="sess_1",
        human_text="x" * 400_000,  # -> 100000 tokens, larger than the baseline
        baseline_cache_creation=1_000,
    )
    stats = context_budget.ContextBudgetStats()
    stats.add_session("proj-a", top)
    section = context_budget.build_section(stats)
    table = next(t for t in section.tables if t.name == "context_budget_baseline")
    col = {c.key: i for i, c in enumerate(table.columns)}
    proj_row = next(row for row in table.rows if row[col["project"]] == "proj-a")
    assert proj_row[col["system_prompt_and_tools_est"]] is None
    assert any("estimates overshot" in n for n in table.notes)


def test_baseline_table_residual_is_the_gap_when_baseline_exceeds_est(tmp_path):
    """The ordinary case: the residual is a real, positive figure equal to
    the baseline minus every known (est) bucket."""
    top = _build_session(
        tmp_path,
        "s1",
        session_id="sess_1",
        human_text="x" * 4_000,
        baseline_cache_creation=50_000,
    )
    stats = context_budget.ContextBudgetStats()
    stats.add_session("proj-a", top)
    section = context_budget.build_section(stats)
    table = next(t for t in section.tables if t.name == "context_budget_baseline")
    col = {c.key: i for i, c in enumerate(table.columns)}
    proj_row = next(row for row in table.rows if row[col["project"]] == "proj-a")
    human_est = proj_row[col["human_prompt_est"]]
    skills_est = proj_row[col["skills_listing_est"]]
    assert proj_row[col["system_prompt_and_tools_est"]] == pytest.approx(50_100 - human_est - skills_est)


def test_baseline_table_uses_snapshot_for_memory_agents_mcp(tmp_path):
    top = _build_session(tmp_path, "s1", session_id="sess_1", baseline_cache_creation=50_000)
    stats = context_budget.ContextBudgetStats()
    stats.add_session("proj-a", top)

    snapshot = _snapshot(
        "proj-a",
        content_layers={
            "claude_md": {"project_root_bytes": 4000, "user_bytes": 0, "project_local_bytes": 0, "nested_bytes": 0},
            "rules": {"count": 2, "bytes": 800},
            "agents_summary": {"count": 3},
        },
        mcp_servers={"names": ["playwright", "linear"]},
    )
    section = context_budget.build_section(stats, snapshots=[snapshot])
    table = next(t for t in section.tables if t.name == "context_budget_baseline")
    col = {c.key: i for i, c in enumerate(table.columns)}
    proj_row = next(row for row in table.rows if row[col["project"]] == "proj-a")

    assert proj_row[col["memory_files_est"]] == pytest.approx((4000 + 800) / 4)
    assert proj_row[col["custom_agents_est"]] == pytest.approx(3 * 60)
    assert proj_row[col["mcp_tools_est"]] == "present, size unknown"
    assert_privacy(section)


def test_baseline_table_snapshot_absent_leaves_nulls(tmp_path):
    top = _build_session(tmp_path, "s1", session_id="sess_1")
    stats = context_budget.ContextBudgetStats()
    stats.add_session("proj-a", top)
    section = context_budget.build_section(stats, snapshots=None)
    table = next(t for t in section.tables if t.name == "context_budget_baseline")
    col = {c.key: i for i, c in enumerate(table.columns)}
    proj_row = next(row for row in table.rows if row[col["project"]] == "proj-a")
    assert proj_row[col["memory_files_est"]] is None
    assert proj_row[col["custom_agents_est"]] is None
    assert proj_row[col["mcp_tools_est"]] is None


def test_baseline_table_all_row_aggregates_every_project(tmp_path):
    top_a = _build_session(tmp_path, "a", session_id="sess_a", baseline_cache_creation=10_000)
    top_b = _build_session(tmp_path, "b", session_id="sess_b", baseline_cache_creation=30_000)
    stats = context_budget.ContextBudgetStats()
    stats.add_session("proj-a", top_a)
    stats.add_session("proj-b", top_b)
    section = context_budget.build_section(stats)
    table = next(t for t in section.tables if t.name == "context_budget_baseline")
    col = {c.key: i for i, c in enumerate(table.columns)}
    all_row = next(row for row in table.rows if row[col["project"]] == "all")
    assert all_row[col["sessions"]] == 2
    assert all_row[col["mean_baseline"]] == pytest.approx(20_100.0)
    # The "all" row never carries per-project config-snapshot buckets.
    assert all_row[col["memory_files_est"]] is None
    assert all_row[col["custom_agents_est"]] is None


def _mcp_row(kind, usd_by_project, server="srv"):
    return tool_search.McpServerRow(server=server, kind=kind, usd_by_project=dict(usd_by_project))


def _baseline_rows(stats, **kwargs):
    section = context_budget.build_section(stats, **kwargs)
    table = next(t for t in section.tables if t.name == "context_budget_baseline")
    col = {c.key: i for i, c in enumerate(table.columns)}
    return section, col, {row[col["project"]]: row for row in table.rows}


def test_baseline_mcp_column_reuses_the_tool_search_prices(tmp_path):
    top_a = _build_session(tmp_path, "a", session_id="sess_a")
    top_b = _build_session(tmp_path, "b", session_id="sess_b")
    stats = context_budget.ContextBudgetStats()
    stats.add_session("proj-a", top_a)
    stats.add_session("proj-b", top_b)
    servers = [
        _mcp_row(tool_search.KIND_DESKTOP_CONNECTOR, {"proj-a": 4.0, "proj-b": 1.0}),
        _mcp_row(tool_search.KIND_DESKTOP_BUILTIN, {"proj-a": 2.5}),
        _mcp_row(tool_search.KIND_USER, {"proj-b": 0.5}),
    ]
    section, col, rows = _baseline_rows(stats, mcp_servers=servers)

    assert rows["proj-a"][col["mcp_tools_est"]] == "2 offered: 1 you can turn off, 1 built into the desktop app"
    assert rows["proj-a"][col["mcp_servers_usd"]] == pytest.approx(6.5)
    assert rows["proj-b"][col["mcp_tools_est"]] == "2 offered: 2 you can turn off"
    assert rows["proj-b"][col["mcp_servers_usd"]] == pytest.approx(1.5)
    assert rows["all"][col["mcp_tools_est"]] == "3 offered: 2 you can turn off, 1 built into the desktop app"
    assert rows["all"][col["mcp_servers_usd"]] == pytest.approx(8.0)
    assert_privacy(section)


def test_baseline_mcp_column_never_calls_a_built_in_server_removable(tmp_path):
    top = _build_session(tmp_path, "a", session_id="sess_a")
    stats = context_budget.ContextBudgetStats()
    stats.add_session("proj-a", top)
    servers = [_mcp_row(tool_search.KIND_DESKTOP_BUILTIN, {"proj-a": 3.0})]
    _, col, rows = _baseline_rows(stats, mcp_servers=servers)

    text = rows["proj-a"][col["mcp_tools_est"]]
    assert text == "1 offered: 1 built into the desktop app"
    assert "turn off" not in text
    # Its cost is still shown: the section sizes it, it just offers no fix.
    assert rows["proj-a"][col["mcp_servers_usd"]] == pytest.approx(3.0)


def test_baseline_mcp_column_counts_only_servers_the_project_was_offered(tmp_path):
    top = _build_session(tmp_path, "a", session_id="sess_a")
    stats = context_budget.ContextBudgetStats()
    stats.add_session("proj-a", top)
    servers = [_mcp_row(tool_search.KIND_CONNECTOR, {"proj-elsewhere": 9.0})]
    _, col, rows = _baseline_rows(stats, mcp_servers=servers)

    assert rows["proj-a"][col["mcp_tools_est"]] is None
    assert rows["proj-a"][col["mcp_servers_usd"]] is None


def test_baseline_mcp_column_falls_back_to_the_config_when_no_server_was_offered(tmp_path):
    top = _build_session(tmp_path, "a", session_id="sess_a")
    stats = context_budget.ContextBudgetStats()
    stats.add_session("proj-a", top)
    snapshot = _snapshot("proj-a", mcp_servers={"names": ["playwright"]})
    _, col, rows = _baseline_rows(stats, snapshots=[snapshot], mcp_servers=[])

    assert rows["proj-a"][col["mcp_tools_est"]] == "present, size unknown"
    assert rows["proj-a"][col["mcp_servers_usd"]] is None


# -- autocompact table ----------------------------------------------------


def test_autocompact_table_drift_true_when_far_from_configured(tmp_path):
    top = _build_session(tmp_path, "s1", session_id="sess_1", with_compaction=True)
    stats = context_budget.ContextBudgetStats()
    stats.add_session("proj-a", top)
    snapshot = _snapshot("proj-a", effective={"autoCompactWindow": 100_000})
    section = context_budget.build_section(stats, snapshots=[snapshot])
    table = next(t for t in section.tables if t.name == "context_budget_autocompact")
    col = {c.key: i for i, c in enumerate(table.columns)}
    row = table.rows[0]
    assert row[col["project"]] == "proj-a"
    assert row[col["configured_window"]] == 100_000
    # Observed threshold is the single auto compaction's preTokens: 180000.
    assert row[col["observed_threshold"]] == pytest.approx(180_000.0)
    assert row[col["auto_compactions"]] == 1
    # |180000 - 100000| / 100000 = 0.8 > 0.10 -> drift.
    assert row[col["drift"]] is True
    assert row[col["context_window_source"]] == "assumed"
    assert row[col["context_window_size"]] == 200_000


def test_autocompact_table_drift_false_when_close_to_configured(tmp_path):
    top = _build_session(tmp_path, "s1", session_id="sess_1", with_compaction=True)
    stats = context_budget.ContextBudgetStats()
    stats.add_session("proj-a", top)
    snapshot = _snapshot("proj-a", effective={"autoCompactWindow": 180_000})
    section = context_budget.build_section(stats, snapshots=[snapshot])
    table = next(t for t in section.tables if t.name == "context_budget_autocompact")
    col = {c.key: i for i, c in enumerate(table.columns)}
    row = table.rows[0]
    assert row[col["drift"]] is False


def test_autocompact_table_null_drift_without_configured_window(tmp_path):
    top = _build_session(tmp_path, "s1", session_id="sess_1", with_compaction=True)
    stats = context_budget.ContextBudgetStats()
    stats.add_session("proj-a", top)
    section = context_budget.build_section(stats, snapshots=None)
    table = next(t for t in section.tables if t.name == "context_budget_autocompact")
    col = {c.key: i for i, c in enumerate(table.columns)}
    row = table.rows[0]
    assert row[col["configured_window"]] is None
    assert row[col["drift"]] is None
    assert row[col["observed_threshold"]] == pytest.approx(180_000.0)


def test_autocompact_table_1m_alias_assumes_million_window(tmp_path):
    """Fix #25: the "[1m]" alias only ever appears in a *setting*, never
    on an observed transcript model (Turn.model is always a full API id)
    -- so this must be read from the project's own snapshot, not from
    ``model=`` on the transcript itself (which stays a plain, unaliased
    id here to prove the transcript side is no longer consulted)."""
    top = _build_session(tmp_path, "s1", session_id="sess_1", model="claude-sonnet-5")
    stats = context_budget.ContextBudgetStats()
    stats.add_session("proj-a", top)
    snapshot = _snapshot("proj-a", effective={"model": "sonnet[1m]"})
    section = context_budget.build_section(stats, snapshots=[snapshot], pricing=PRICING)
    table = next(t for t in section.tables if t.name == "context_budget_autocompact")
    col = {c.key: i for i, c in enumerate(table.columns)}
    row = table.rows[0]
    assert row[col["context_window_size"]] == 1_000_000
    assert row[col["context_window_source"]] == "assumed"
    assert row[col["auto_compactions"]] == 0
    assert row[col["observed_threshold"]] is None


def test_autocompact_table_1m_alias_falls_back_to_schema1_user_settings(tmp_path):
    """Schema-1 snapshots predate ``effective`` entirely; the alias must
    still be found via the raw ``user_settings.model`` field."""
    top = _build_session(tmp_path, "s1", session_id="sess_1", model="claude-sonnet-5")
    stats = context_budget.ContextBudgetStats()
    stats.add_session("proj-a", top)
    snapshot = _snapshot("proj-a", user_settings={"model": "sonnet[1m]"})
    section = context_budget.build_section(stats, snapshots=[snapshot], pricing=PRICING)
    table = next(t for t in section.tables if t.name == "context_budget_autocompact")
    col = {c.key: i for i, c in enumerate(table.columns)}
    row = table.rows[0]
    assert row[col["context_window_size"]] == 1_000_000


def test_autocompact_table_bare_alias_on_1m_model_assumes_million_window(tmp_path):
    """D2/D4/COV-12: a bare, un-suffixed alias ("sonnet") on a natively
    1M-context Claude 5 model (V24) must resolve to 1M too -- the old
    code assumed 200k unless the alias literally ended in "[1m]"."""
    top = _build_session(tmp_path, "s1", session_id="sess_1", model="claude-sonnet-5")
    stats = context_budget.ContextBudgetStats()
    stats.add_session("proj-a", top)
    snapshot = _snapshot("proj-a", effective={"model": "sonnet"})
    section = context_budget.build_section(stats, snapshots=[snapshot], pricing=PRICING)
    table = next(t for t in section.tables if t.name == "context_budget_autocompact")
    col = {c.key: i for i, c in enumerate(table.columns)}
    row = table.rows[0]
    assert row[col["context_window_size"]] == 1_000_000


def test_autocompact_table_1m_alias_without_pricing_falls_back_to_default_window(tmp_path):
    """Without a rate card to resolve against, the table degrades to the
    200k fallback rather than guessing -- same optional/degrade-
    gracefully convention as ``limits.build_section``'s own ``pricing``
    parameter."""
    top = _build_session(tmp_path, "s1", session_id="sess_1", model="claude-sonnet-5")
    stats = context_budget.ContextBudgetStats()
    stats.add_session("proj-a", top)
    snapshot = _snapshot("proj-a", effective={"model": "sonnet[1m]"})
    section = context_budget.build_section(stats, snapshots=[snapshot])
    table = next(t for t in section.tables if t.name == "context_budget_autocompact")
    col = {c.key: i for i, c in enumerate(table.columns)}
    row = table.rows[0]
    assert row[col["context_window_size"]] == 200_000


def test_autocompact_table_no_alias_uses_default_window_even_with_1m_transcript_model(tmp_path):
    """A full API model id that happens to contain "[1m]"-shaped text on
    the transcript side must not be mistaken for the settings alias --
    only the joined snapshot's own model setting counts."""
    top = _build_session(tmp_path, "s1", session_id="sess_1", model="claude-sonnet-5[1m]")
    stats = context_budget.ContextBudgetStats()
    stats.add_session("proj-a", top)
    section = context_budget.build_section(stats, snapshots=None)
    table = next(t for t in section.tables if t.name == "context_budget_autocompact")
    col = {c.key: i for i, c in enumerate(table.columns)}
    row = table.rows[0]
    assert row[col["context_window_size"]] == 200_000


# -- statusline table -----------------------------------------------------


def test_statusline_table_empty_without_usage_log_rows(tmp_path):
    top = _build_session(tmp_path, "s1", session_id="sess_1")
    stats = context_budget.ContextBudgetStats()
    stats.add_session("proj-a", top)
    section = context_budget.build_section(stats, usage_log_rows=None)
    table = next(t for t in section.tables if t.name == "context_budget_statusline")
    assert table.rows == []
    assert table.notes


def test_statusline_table_renders_ground_truth_row(tmp_path):
    top = _build_session(tmp_path, "s1", session_id="sess_1")
    stats = context_budget.ContextBudgetStats()
    stats.add_session("proj-a", top)
    usage_log_rows = [
        {
            "session_id": "sess_1",
            "context_window_used_tokens": 143_000,
            "context_window_size": 200_000,
            "context_window_used_percentage": 71.5,
        }
    ]
    section = context_budget.build_section(stats, usage_log_rows=usage_log_rows)
    table = next(t for t in section.tables if t.name == "context_budget_statusline")
    assert len(table.rows) == 1
    col = {c.key: i for i, c in enumerate(table.columns)}
    row = table.rows[0]
    assert row[col["session"]] == "sess_1"
    assert row[col["used_tokens"]] == 143_000
    assert row[col["context_window_size"]] == 200_000
    assert row[col["used_percentage"]] == 71.5
    assert not table.notes


def test_autocompact_table_uses_statusline_window_when_available(tmp_path):
    top = _build_session(tmp_path, "s1", session_id="sess_1", with_compaction=True)
    stats = context_budget.ContextBudgetStats()
    stats.add_session("proj-a", top)
    usage_log_rows = [
        {"session_id": "sess_1", "context_window_used_tokens": 100, "context_window_size": 500_000}
    ]
    section = context_budget.build_section(stats, usage_log_rows=usage_log_rows)
    table = next(t for t in section.tables if t.name == "context_budget_autocompact")
    col = {c.key: i for i, c in enumerate(table.columns)}
    row = table.rows[0]
    assert row[col["context_window_size"]] == 500_000
    assert row[col["context_window_source"]] == "statusline"


# -- statusline.py write/read round-trip -----------------------------------


def test_statusline_appends_context_window_trailing_columns(tmp_path):
    csv_path = tmp_path / "usage-log.csv"
    payload = {
        "session_id": "sess_ctx",
        "context_window": {
            "used_tokens": 143_000,
            "context_window_size": 200_000,
            "used_percentage": 71.5,
            "current_usage": {"cache_read_input_tokens": 120_000},
        },
    }
    statusline._append_context_window_row(csv_path, payload, __import__("datetime").datetime(2026, 9, 18, tzinfo=__import__("datetime").timezone.utc))

    rows = context_budget.load_context_window_rows(csv_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["session_id"] == "sess_ctx"
    assert row["context_window_used_tokens"] == 143_000
    assert row["context_window_size"] == 200_000
    assert row["context_window_used_percentage"] == 71.5
    assert row["context_window_cache_read_tokens"] == 120_000


def test_statusline_context_window_row_dedupes_identical_repeat(tmp_path):
    csv_path = tmp_path / "usage-log.csv"
    payload = {
        "session_id": "sess_ctx",
        "context_window": {"used_tokens": 100, "context_window_size": 200_000},
    }
    now = __import__("datetime").datetime(2026, 9, 18, tzinfo=__import__("datetime").timezone.utc)
    statusline._append_context_window_row(csv_path, payload, now)
    statusline._append_context_window_row(csv_path, payload, now)
    rows = context_budget.load_context_window_rows(csv_path)
    assert len(rows) == 1


def test_statusline_context_window_accepts_total_tokens_size_key(tmp_path):
    csv_path = tmp_path / "usage-log.csv"
    payload = {
        "session_id": "sess_ctx",
        "context_window": {"used_tokens": 100, "total_tokens": 1_000_000},
    }
    now = __import__("datetime").datetime(2026, 9, 18, tzinfo=__import__("datetime").timezone.utc)
    statusline._append_context_window_row(csv_path, payload, now)
    rows = context_budget.load_context_window_rows(csv_path)
    assert rows[0]["context_window_size"] == 1_000_000


def test_load_context_window_rows_tolerates_old_format_rows(tmp_path):
    """A usage-log CSV written entirely by the pre-S1-context-budget
    ``log_usage.append_rows`` (6 columns, no trailing context_window
    fields) must be tolerated, not raise -- it simply carries no
    context-window rows."""
    from claudeglass.tools import log_usage

    csv_path = tmp_path / "usage-log.csv"
    log_usage.append_rows(
        csv_path,
        [{"session_id": "sess_old", "window": "five_hour", "used_percentage": 42.0, "resets_at": ""}],
        source="statusline",
    )
    rows = context_budget.load_context_window_rows(csv_path)
    assert rows == []

    # A subsequent new-format append on top of the old-format file must
    # still work (the header is never rewritten -- a new row simply has
    # more columns than the header names, which csv.reader tolerates).
    now = __import__("datetime").datetime(2026, 9, 18, tzinfo=__import__("datetime").timezone.utc)
    statusline._append_context_window_row(
        csv_path, {"session_id": "sess_new", "context_window": {"used_tokens": 55}}, now
    )
    rows = context_budget.load_context_window_rows(csv_path)
    assert len(rows) == 1
    assert rows[0]["session_id"] == "sess_new"
    assert rows[0]["context_window_used_tokens"] == 55


def test_load_context_window_rows_missing_file_returns_empty(tmp_path):
    assert context_budget.load_context_window_rows(tmp_path / "does-not-exist.csv") == []


# -- real fixture smoke test ------------------------------------------------


@pytest.mark.skipif(
    not FIXTURE_DIR.exists() or not any(FIXTURE_DIR.glob("*.jsonl")),
    reason="tests/fixtures/real/session-a/ not present (real fixture not checked out)",
)
def test_real_fixture_renders_without_error():
    top_paths = list(FIXTURE_DIR.glob("*.jsonl"))
    assert len(top_paths) == 1
    top_path = top_paths[0]
    session_id = top_path.stem
    top = parse_transcript(top_path, TranscriptMeta(path=str(top_path), kind="top-level", session_id=session_id))

    stats = context_budget.ContextBudgetStats()
    stats.add_session("session-a", top)
    section = context_budget.build_section(stats)

    assert section.key == "context_budget"
    assert len(section.tables) == 4
    assert_privacy(section)


def test_baseline_table_finds_the_snapshot_by_its_hashed_project_key(tmp_path):
    from claudeglass import snapshots as snap_mod

    top = _build_session(tmp_path, "s1", session_id="sess_1", baseline_cache_creation=50_000)
    stats = context_budget.ContextBudgetStats()
    stats.add_session("C--Users-<user>-proj", top, raw_slug="C--Users-alice-proj")
    snapshot = _snapshot(
        snap_mod.snapshot_project_key("C--Users-alice-proj"),
        content_layers={"agents_summary": {"count": 2}},
    )
    section = context_budget.build_section(stats, snapshots=[snapshot])
    table = next(t for t in section.tables if t.name == "context_budget_baseline")
    col = {c.key: i for i, c in enumerate(table.columns)}
    row = next(r for r in table.rows if r[col["project"]] == "C--Users-<user>-proj")
    assert row[col["custom_agents_est"]] == pytest.approx(2 * 60)


def _custom_agents_est(stats, snapshots):
    section = context_budget.build_section(stats, snapshots=snapshots)
    table = next(t for t in section.tables if t.name == "context_budget_baseline")
    col = {c.key: i for i, c in enumerate(table.columns)}
    row = next(r for r in table.rows if r[col["project"]] == "c--Users-<user>-proj")
    return row[col["custom_agents_est"]]


def _session_with_roster(tmp_path, name, *, listed_types, chars, session_id):
    """A top-level transcript whose first call carries the sibling-agent
    roster: ``listed_types`` agent types in ``chars`` characters."""
    roster = attachment_line("agent_listing_delta", rendered="r" * chars, addedTypes=[f"t{n}" for n in range(listed_types)])
    roster["timestamp"] = "2026-09-18T11:59:00.000Z"
    lines = [
        roster,
        user_str_line("please fix the bug", timestamp="2026-09-18T11:59:30.000Z"),
        turn_line(model="claude-sonnet-5", timestamp="2026-09-18T12:00:00.000Z", cache_creation_input_tokens=40_000),
    ]
    path = tmp_path / f"{name}.jsonl"
    write_jsonl(path, lines)
    return parse_transcript(path, TranscriptMeta(path=str(path), session_id=session_id))


def test_custom_agents_are_sized_from_the_roster_the_first_call_carried(tmp_path):
    from claudeglass import snapshots as snap_mod

    top = _session_with_roster(tmp_path, "s1", listed_types=4, chars=2_000, session_id="sess_1")
    stats = context_budget.ContextBudgetStats()
    stats.add_session("c--Users-<user>-proj", top, raw_slug="c--Users-alice-proj")
    canonical, _legacy = snap_mod.snapshot_project_keys("c--Users-alice-proj")
    two = _snapshot(canonical, content_layers={"agents_summary": {"count": 2}})
    # The roster is 2,000 characters (500 tokens) for 4 types; 2 of them are the user's own.
    assert _custom_agents_est(stats, [two]) == pytest.approx(500 * 2 / 4)
    # A count beyond the types the roster lists is capped at the whole roster.
    many = _snapshot(canonical, content_layers={"agents_summary": {"count": 9}})
    assert _custom_agents_est(stats, [many]) == pytest.approx(500)


def test_custom_agents_fall_back_to_the_per_agent_constant_without_a_roster(tmp_path):
    from claudeglass import snapshots as snap_mod

    top = _build_session(tmp_path, "s1", session_id="sess_1", baseline_cache_creation=50_000)
    stats = context_budget.ContextBudgetStats()
    stats.add_session("c--Users-<user>-proj", top, raw_slug="c--Users-alice-proj")
    canonical, _legacy = snap_mod.snapshot_project_keys("c--Users-alice-proj")
    snapshot = _snapshot(canonical, content_layers={"agents_summary": {"count": 3}})
    assert _custom_agents_est(stats, [snapshot]) == pytest.approx(3 * 60)


def test_baseline_table_joins_a_lower_case_drive_folder_to_a_snapshot_under_either_key(tmp_path):
    from claudeglass import snapshots as snap_mod

    top = _build_session(tmp_path, "s1", session_id="sess_1", baseline_cache_creation=50_000)
    stats = context_budget.ContextBudgetStats()
    stats.add_session("c--Users-<user>-proj", top, raw_slug="c--Users-alice-proj")
    canonical, legacy = snap_mod.snapshot_project_keys("c--Users-alice-proj")
    layers = {"agents_summary": {"count": 2}}
    assert _custom_agents_est(stats, [_snapshot(canonical, content_layers=layers)]) == pytest.approx(2 * 60)
    assert _custom_agents_est(stats, [_snapshot(legacy, content_layers=layers)]) == pytest.approx(2 * 60)


def test_baseline_table_reads_the_newest_snapshot_of_a_project_filed_under_both_keys(tmp_path):
    from claudeglass import snapshots as snap_mod

    top = _build_session(tmp_path, "s1", session_id="sess_1", baseline_cache_creation=50_000)
    stats = context_budget.ContextBudgetStats()
    stats.add_session("c--Users-<user>-proj", top, raw_slug="c--Users-alice-proj")
    canonical, legacy = snap_mod.snapshot_project_keys("c--Users-alice-proj")
    old = Snapshot(
        path=Path("old.json"), ts="2026-09-10T00:00:00.000Z",
        data={"project_slug": legacy, "content_layers": {"agents_summary": {"count": 5}}},
    )
    new = Snapshot(
        path=Path("new.json"), ts="2026-09-20T00:00:00.000Z",
        data={"project_slug": canonical, "content_layers": {"agents_summary": {"count": 2}}},
    )
    other = Snapshot(
        path=Path("other.json"), ts="2026-09-25T00:00:00.000Z",
        data={"project_slug": snap_mod.snapshot_project_key("C--Users-bob-other"),
              "content_layers": {"agents_summary": {"count": 9}}},
    )
    assert _custom_agents_est(stats, [old, new, other]) == pytest.approx(2 * 60)
    # The order they arrive in doesn't matter.
    assert _custom_agents_est(stats, [new, old, other]) == pytest.approx(2 * 60)
    # The newest wins whichever key it carries.
    newer_legacy = Snapshot(
        path=Path("newer.json"), ts="2026-09-22T00:00:00.000Z",
        data={"project_slug": legacy, "content_layers": {"agents_summary": {"count": 7}}},
    )
    assert _custom_agents_est(stats, [old, new, newer_legacy, other]) == pytest.approx(7 * 60)


# -- statusline table: a window with no terminal session ------------------


def _statusline_table(stats, usage_log_rows=None):
    section = context_budget.build_section(stats, usage_log_rows=usage_log_rows)
    return next(t for t in section.tables if t.name == "context_budget_statusline")


def _stats_for(tmp_path, entrypoints):
    """Stats over one main session per ``entrypoint`` (``None`` when the
    transcript recorded none)."""
    stats = context_budget.ContextBudgetStats()
    for i, entrypoint in enumerate(entrypoints):
        top = _build_session(tmp_path, f"s{i}", session_id=f"sess_{i}")
        top.meta.entrypoint = entrypoint
        stats.add_session("proj-a", top)
    return stats


def test_desktop_only_is_true_only_when_every_session_ran_in_the_desktop_app(tmp_path):
    assert _stats_for(tmp_path, ["claude-desktop", "claude-desktop"]).desktop_only is True
    assert _stats_for(tmp_path, ["claude-desktop", "cli"]).desktop_only is False
    assert _stats_for(tmp_path, ["cli"]).desktop_only is False
    # A session with no recorded entrypoint is not known to be desktop.
    assert _stats_for(tmp_path, ["claude-desktop", None]).desktop_only is False
    assert context_budget.ContextBudgetStats().desktop_only is False


def test_stats_count_main_sessions_by_entrypoint(tmp_path):
    stats = _stats_for(tmp_path, ["claude-desktop", "claude-desktop", "cli", None])
    assert stats.entrypoints == {"claude-desktop": 2, "cli": 1, "": 1}


def test_an_empty_statusline_table_for_desktop_sessions_says_so_and_takes_the_desktop_variant(tmp_path):
    table = _statusline_table(_stats_for(tmp_path, ["claude-desktop"]))
    assert table.rows == []
    assert table.empty_variant == "desktop" == context_budget.DESKTOP_EMPTY_VARIANT
    assert table.notes == [context_budget.DESKTOP_EMPTY_NOTE]
    assert "doesn't run status lines" in table.notes[0]
    # It no longer tells the reader to install a logger that cannot work.
    assert "Install" not in table.notes[0] and "install" not in table.notes[0]


@pytest.mark.parametrize("entrypoints", [["cli"], ["claude-desktop", "cli"], ["claude-desktop", None], [None]])
def test_an_empty_statusline_table_keeps_the_install_note_when_a_session_may_have_run_a_status_line(
    tmp_path, entrypoints
):
    table = _statusline_table(_stats_for(tmp_path, entrypoints))
    assert table.empty_variant == ""
    assert table.notes and "Install the status line logger" in table.notes[0]


def test_a_statusline_table_with_rows_has_no_variant_even_for_desktop_sessions(tmp_path):
    stats = _stats_for(tmp_path, ["claude-desktop"])
    rows = [
        {
            "session_id": "sess_0",
            "context_window_used_tokens": 143_000,
            "context_window_size": 200_000,
            "context_window_used_percentage": 71.5,
        }
    ]
    table = _statusline_table(stats, rows)
    assert len(table.rows) == 1 and table.empty_variant == "" and not table.notes


def test_the_desktop_variant_reaches_the_json_output(tmp_path):
    from claudeglass.render.json_out import to_jsonable

    table = _statusline_table(_stats_for(tmp_path, ["claude-desktop"]))
    assert to_jsonable(table)["empty_variant"] == "desktop"
    assert to_jsonable(_statusline_table(_stats_for(tmp_path, ["cli"])))["empty_variant"] == ""


# -- subagent startup: the tools snapshot arrives after the first call -----------------------------------------------

def _stamp(line: dict, ts: str) -> dict:
    line["timestamp"] = ts
    return line


def _tool(name: str, size: int = 300) -> dict:
    return {"name": name, "description": "d" * size, "input_schema": {"type": "object"}}


def _tool_chars(tool: dict) -> int:
    return len(json.dumps(tool, separators=(",", ":"), ensure_ascii=False))


TOOLS = [_tool("Read"), _tool("Grep"), _tool("Bash", 900), _tool("mcp__figma__a"), _tool("mcp__figma__b", 500)]


def _sub_lines(
    *,
    model: str = "claude-sonnet-5",
    shared_prefix: int = 60_000,
    written: int = 3_000,
    uncached: int = 100,
    tools: list | None = None,
    header_after_second: bool = False,
    later_snapshot: list | None = None,
    used: str = "Read",
) -> list[dict]:
    """A subagent transcript the way Claude Code writes one: the system
    prompt before the first call, then the tools snapshot, the agent
    roster and the MCP instructions after it."""
    tools = TOOLS if tools is None else tools
    lines = [
        _stamp(user_str_line("investigate the parser " * 4), "2026-09-18T12:00:00.000Z"),
        _stamp(attachment_line("prompt_snapshot", systemPrompt=["s" * 2_000]), "2026-09-18T12:00:01.000Z"),
        _stamp(
            turn_line(
                message_id="sub_1",
                model=model,
                input_tokens=uncached,
                cache_creation_input_tokens=written,
                cache_read_input_tokens=shared_prefix,
            ),
            "2026-09-18T12:00:02.000Z",
        ),
    ]
    if tools:
        lines.append(
            _stamp(attachment_line("prompt_snapshot", systemPrompt=["s" * 2_000], tools=tools), "2026-09-18T12:00:03.000Z")
        )
    lines += [
        _stamp(attachment_line("agent_listing_delta", rendered="a" * 400), "2026-09-18T12:00:03.500Z"),
        _stamp(attachment_line("mcp_instructions_delta", rendered="m" * 200), "2026-09-18T12:00:03.600Z"),
        _stamp(
            turn_line(
                message_id="sub_2",
                model=model,
                cache_read_input_tokens=shared_prefix + written,
                content=[tool_use_block(used, "tu_sub_1", {"file_path": "x"})],
            ),
            "2026-09-18T12:00:10.000Z",
        ),
        # Both of these come after the second call: not part of the startup.
        _stamp(attachment_line("agent_listing_delta", rendered="z" * 4_000), "2026-09-18T12:00:11.000Z"),
    ]
    if header_after_second:
        lines.append(
            _stamp(
                attachment_line("prompt_snapshot", systemPrompt=["s" * 2_000], toolChangeHeader="tools changed"),
                "2026-09-18T12:00:12.000Z",
            )
        )
    if later_snapshot is not None:
        lines.append(
            _stamp(
                attachment_line("prompt_snapshot", systemPrompt=["s" * 2_000], tools=later_snapshot),
                "2026-09-18T12:00:13.000Z",
            )
        )
    lines.append(_stamp(turn_line(message_id="sub_3", model=model), "2026-09-18T12:00:20.000Z"))
    return lines


def _sub(tmp_path: Path, name: str, lines: list[dict], agent_type: str = "Explore"):
    path = tmp_path / f"{name}.jsonl"
    write_jsonl(path, lines)
    meta = TranscriptMeta(path=str(path), kind="subagent", agent_type=agent_type, agent_id=name, session_id="sess_sub")
    return parse_transcript(path, meta)


def _startup_tables(stats):
    section = context_budget.build_startup_section(stats)
    return {t.name: t for t in section.tables}


def _row(table, key_column, key):
    col = {c.key: i for i, c in enumerate(table.columns)}
    row = next(r for r in table.rows if r[col[key_column]] == key)
    return {name: row[i] for name, i in col.items()}


def test_tool_definitions_written_after_the_first_call_are_a_startup_part(tmp_path):
    """A subagent writes its tools snapshot, agent roster and MCP
    instructions after the first call: they still belong to its startup."""
    sub = _sub(tmp_path, "agent-1", _sub_lines())
    stats = context_budget.ContextBudgetStats()
    stats.add_subagent(sub)
    tables = _startup_tables(stats)
    row = _row(tables["agent_startup_breakdown"], "agent_type", "Explore")

    tools_chars = sum(_tool_chars(t) for t in TOOLS)
    assert row["tool_definitions"] == pytest.approx(tools_chars / 4.0)
    assert row["tool_definitions"] > 0
    # The system prompt is counted once, whatever number of snapshots repeat it.
    assert row["system_prompt"] == pytest.approx(2_000 / 4.0)
    # The roster and instructions before the second call count; the roster after it does not.
    assert row["tool_lists"] == pytest.approx((400 + 200) / 4.0)
    assert row["startup_tokens"] == 60_000 + 3_000 + 100
    # The note says why the snapshot is read from after the first call.
    notes = " ".join(tables["agent_startup_breakdown"].notes)
    assert "after its first call" in notes and "Not recorded" in notes


def test_each_built_in_tool_and_mcp_server_has_its_own_size_and_use(tmp_path):
    sub = _sub(tmp_path, "agent-1", _sub_lines(used="Read"))
    stats = context_budget.ContextBudgetStats()
    stats.add_subagent(sub)
    table = _startup_tables(stats)["agent_startup_tools"]
    rows = {r[2]: dict(zip([c.key for c in table.columns], r)) for r in table.rows}

    # Read is called; the others, and the MCP server as one row, are not.
    assert "Read" not in rows
    assert rows["Bash"]["definition_tokens"] == pytest.approx(_tool_chars(TOOLS[2]) / 4.0)
    assert rows["Bash"]["offered_spawns"] == 1 and rows["Bash"]["used_spawns"] == 0
    assert rows["mcp__figma__*"]["definition_tokens"] == pytest.approx(
        (_tool_chars(TOOLS[3]) + _tool_chars(TOOLS[4])) / 4.0
    )
    # Largest first.
    sizes = [r[5] for r in table.rows]
    assert sizes == sorted(sizes, reverse=True)
    # No description reaches the table.
    assert "ddd" not in repr(table)
    assert_privacy(table)


def test_the_removable_size_counts_tools_offered_and_hardly_used(tmp_path):
    stats = context_budget.ContextBudgetStats()
    for n in range(10):
        stats.add_subagent(_sub(tmp_path, f"agent-{n}", _sub_lines(used="Read" if n else "Bash")))
    row = _row(_startup_tables(stats)["agent_startup_breakdown"], "agent_type", "Explore")
    # Read is used by 9 of 10, Bash by 1 of 10 (exactly a tenth, so it stays), Grep and the MCP server by none.
    removable = (_tool_chars(TOOLS[1]) + _tool_chars(TOOLS[3]) + _tool_chars(TOOLS[4])) / 4.0
    assert row["removable_tools"] == pytest.approx(removable)


def test_a_tool_used_in_fewer_than_a_tenth_of_the_spawns_is_removable(tmp_path):
    stats = context_budget.ContextBudgetStats()
    for n in range(20):
        stats.add_subagent(_sub(tmp_path, f"agent-{n}", _sub_lines(used="Bash" if n == 0 else "Read")))
    row = _row(_startup_tables(stats)["agent_startup_breakdown"], "agent_type", "Explore")
    # Bash is used by 1 of 20 (5%), so it goes with Grep and the MCP server.
    removable = (_tool_chars(TOOLS[1]) + _tool_chars(TOOLS[2]) + _tool_chars(TOOLS[3]) + _tool_chars(TOOLS[4])) / 4.0
    assert row["removable_tools"] == pytest.approx(removable)
    notes = " ".join(_startup_tables(stats)["agent_startup_tools"].notes)
    assert "fewer than 10%" in notes


def test_a_later_header_only_snapshot_does_not_reset_the_tool_definitions(tmp_path):
    first_only = _sub(tmp_path, "agent-1", _sub_lines())
    with_header = _sub(tmp_path, "agent-2", _sub_lines(header_after_second=True))
    with_smaller_later = _sub(tmp_path, "agent-3", _sub_lines(header_after_second=True, later_snapshot=[_tool("Read")]))
    rows = []
    for sub in (first_only, with_header, with_smaller_later):
        stats = context_budget.ContextBudgetStats()
        stats.add_subagent(sub)
        rows.append(_row(_startup_tables(stats)["agent_startup_breakdown"], "agent_type", "Explore"))
    assert rows[0]["tool_definitions"] > 0
    assert rows[1]["tool_definitions"] == rows[0]["tool_definitions"]
    # A later snapshot that lists tools is not startup either.
    assert rows[2]["tool_definitions"] == rows[0]["tool_definitions"]
    assert rows[2]["system_prompt"] == rows[0]["system_prompt"]


def test_a_spawn_with_no_tools_snapshot_leaves_its_tool_definitions_not_recorded(tmp_path):
    stats = context_budget.ContextBudgetStats()
    stats.add_subagent(_sub(tmp_path, "agent-1", _sub_lines(tools=[])))
    tables = _startup_tables(stats)
    row = _row(tables["agent_startup_breakdown"], "agent_type", "Explore")
    assert row["tool_definitions"] == 0
    assert row["not_recorded"] > 50_000
    assert row["removable_tools"] is None
    assert tables["agent_startup_tools"].rows == []


def test_the_first_call_comparison_never_mixes_models(tmp_path):
    """The same tools are 51.5k tokens on Haiku 4.5 and 69.4k on Sonnet 5:
    one agent type on both is averaged on its own main model only."""
    stats = context_budget.ContextBudgetStats()
    for n in range(3):
        stats.add_subagent(_sub(tmp_path, f"haiku-{n}", _sub_lines(model="claude-haiku-4-5", shared_prefix=51_500)))
    stats.add_subagent(_sub(tmp_path, "sonnet-0", _sub_lines(model="claude-sonnet-5", shared_prefix=69_400)))
    for n in range(2):
        stats.add_subagent(
            _sub(tmp_path, f"plan-{n}", _sub_lines(model="claude-haiku-4-5", shared_prefix=51_500), agent_type="Plan")
        )
    stats.add_subagent(
        _sub(tmp_path, "plan-sonnet", _sub_lines(model="claude-sonnet-5", shared_prefix=69_400), agent_type="Plan")
    )
    tables = _startup_tables(stats)
    table = tables["agent_startup_breakdown"]
    explore = _row(table, "agent_type", "Explore")
    assert explore["model"] == "claude-haiku-4-5"
    assert explore["spawns"] == 4 and explore["other_model_spawns"] == 1
    assert explore["startup_tokens"] == pytest.approx(51_500 + 3_000 + 100)
    plan = _row(table, "agent_type", "Plan")
    assert plan["model"] == "claude-haiku-4-5" and plan["other_model_spawns"] == 1
    assert plan["startup_tokens"] == pytest.approx(51_500 + 3_000 + 100)
    assert any("claude-haiku-4-5" in note for note in tables["agent_startup_shared"].notes)


def test_a_model_is_calibrated_from_its_own_subagent_first_calls(tmp_path):
    subs = [
        _sub(tmp_path, f"agent-{n}", _sub_lines(model="claude-haiku-4-5", shared_prefix=1_000))
        for n in range(calibration.MIN_CALLS)
    ]
    calls = [context_budget.first_call(sub) for sub in subs]
    assert all(call is not None for call in calls)
    found = calibration.Calibration.from_calls(calls)
    tools_chars = sum(_tool_chars(t) for t in TOOLS)
    assert found.tool_chars_per_token("claude-haiku-4-5") == pytest.approx(tools_chars / 1_000)
    assert found.text_chars_per_token("claude-haiku-4-5") > 0
    assert found.basis() == calibration.BASIS

    stats = context_budget.ContextBudgetStats(calibration=found)
    stats.add_subagent(subs[0])
    row = _row(_startup_tables(stats)["agent_startup_breakdown"], "agent_type", "Explore")
    # The tool definitions read from cache are exactly the shared prefix: 1,000 tokens.
    assert row["tool_definitions"] == pytest.approx(1_000)


def test_a_fork_is_not_a_first_call_to_calibrate_on(tmp_path):
    fork = _sub(tmp_path, "agent-fork", _sub_lines(), agent_type="fork")
    assert context_budget.first_call(fork) is None


# -- subagent startup: what a tools list would take out ----------------------------------------------------------------


def _spawn_many(tmp_path, count, *, used, pricing=PRICING, **kwargs):
    """``count`` spawns of Explore; ``used(n)`` is the tool spawn ``n`` calls."""
    stats = context_budget.ContextBudgetStats()
    for n in range(count):
        stats.add_subagent(_sub(tmp_path, f"agent-{n}", _sub_lines(used=used(n), **kwargs)), pricing=pricing)
    return stats


def _diet_row(stats, agent_type="Explore"):
    return _row(_startup_tables(stats)["agent_startup_diet"], "agent_type", agent_type)


def test_the_diet_row_keeps_the_tools_called_in_a_tenth_of_the_spawns_and_sizes_the_rest(tmp_path):
    # Read in 9 of 10 spawns, Bash in 1 (exactly a tenth): both stay. Grep
    # and the figma server were never called.
    stats = _spawn_many(tmp_path, 10, used=lambda n: "Read" if n else "Bash")
    tables = _startup_tables(stats)
    row = _row(tables["agent_startup_diet"], "agent_type", "Explore")
    assert row["keep_tools"] == "Bash, Read"
    assert row["rare_tools"] == "Grep, mcp__figma__*"
    assert row["spawns"] == 10 and row["model"] == "claude-sonnet-5"
    # The same size as the breakdown's "tools never used" column.
    breakdown = _row(tables["agent_startup_breakdown"], "agent_type", "Explore")
    assert row["dropped_definitions"] == pytest.approx(breakdown["removable_tools"])
    assert row["dropped_definitions"] == pytest.approx(
        (_tool_chars(TOOLS[1]) + _tool_chars(TOOLS[3]) + _tool_chars(TOOLS[4])) / 4.0
    )
    assert (row["dropped_skills"], row["dropped_roster"], row["dropped_deferred"]) == (0.0, 0.0, 0.0)
    assert_privacy(tables["agent_startup_diet"])
    assert "ddd" not in repr(tables["agent_startup_diet"])


def test_a_tool_called_in_under_a_tenth_of_the_spawns_moves_from_the_list_to_the_rarely_used(tmp_path):
    stats = _spawn_many(tmp_path, 20, used=lambda n: "Bash" if n == 0 else "Read")
    row = _diet_row(stats)
    assert row["keep_tools"] == "Read"
    assert row["rare_tools"] == "Bash, Grep, mcp__figma__*"


def test_the_tools_claude_code_adds_are_in_neither_list(tmp_path):
    tools = [*TOOLS, _tool("StructuredOutput", 800), _tool("SubagentHandback", 800)]
    stats = _spawn_many(tmp_path, 10, used=lambda n: "StructuredOutput", tools=tools)
    row = _diet_row(stats)
    for name in ("StructuredOutput", "SubagentHandback"):
        assert name not in row["keep_tools"] and name not in row["rare_tools"]
    # Their definitions are not counted as something a list takes out.
    assert row["dropped_definitions"] == pytest.approx(sum(_tool_chars(t) for t in TOOLS) / 4.0)


def test_the_diet_has_no_row_for_a_spawn_that_recorded_no_tools(tmp_path):
    stats = context_budget.ContextBudgetStats()
    stats.add_subagent(_sub(tmp_path, "agent-1", _sub_lines(tools=[])), pricing=PRICING)
    tables = _startup_tables(stats)
    assert tables["agent_startup_diet"].rows == [] and tables["agent_startup_servers"].rows == []


def test_the_agent_list_goes_with_the_agent_tool_and_stays_when_the_agent_tool_is_called(tmp_path):
    # The roster in _sub_lines is 400 characters, 100 tokens.
    stats = _spawn_many(tmp_path, 10, used=lambda n: "Read", tools=[*TOOLS, _tool("Agent", 600)])
    row = _diet_row(stats)
    assert "Agent" in row["rare_tools"]
    assert row["dropped_roster"] == pytest.approx(100.0) and row["dropped_skills"] == 0.0
    stats = _spawn_many(tmp_path, 10, used=lambda n: "Agent", tools=[*TOOLS, _tool("Agent", 600)])
    row = _diet_row(stats)
    assert row["dropped_roster"] == 0.0 and "Agent" in row["keep_tools"]


def test_the_diet_counts_the_tool_prefix_at_the_write_price_only_for_the_spawns_that_wrote_it(tmp_path):
    # 1 spawn in 4 wrote the tool definitions (it read none of them from
    # cache); the other three read them.
    stats = context_budget.ContextBudgetStats()
    for n in range(4):
        lines = _sub_lines(shared_prefix=0, written=70_000) if n == 0 else _sub_lines()
        stats.add_subagent(_sub(tmp_path, f"agent-{n}", lines), pricing=PRICING)
    row = _diet_row(stats)
    assert stats.agents["Explore"].prefix_written == {"claude-sonnet-5": 1}
    assert row["prefix_write_share"] == pytest.approx(25.0)
    write, read, later = row["write_price"], row["read_price"], row["later_calls"]
    assert (write, read, later) == (2.5, pytest.approx(0.2), 2.0)
    first_call = 0.25 * write + 0.75 * read
    expected = 4 * row["dropped_definitions"] * (first_call + later * read) / 1_000_000
    assert row["saving_usd"] == pytest.approx(expected)
    # Priced as if every spawn wrote it, the saving would be larger.
    every = 4 * row["dropped_definitions"] * (write + later * read) / 1_000_000
    assert row["saving_usd"] < every


def test_the_startup_breakdown_carries_the_read_price_and_the_calls_after_the_first(tmp_path):
    stats = _spawn_many(tmp_path, 3, used=lambda n: "Read")
    row = _row(_startup_tables(stats)["agent_startup_breakdown"], "agent_type", "Explore")
    # Three calls in each transcript: two come after the first.
    assert row["later_calls"] == 2.0 and row["read_price"] == pytest.approx(0.2)
    # Without a rate card there is no read price, and still the calls.
    stats = _spawn_many(tmp_path, 3, used=lambda n: "Read", pricing=None)
    row = _row(_startup_tables(stats)["agent_startup_breakdown"], "agent_type", "Explore")
    assert row["later_calls"] == 2.0 and row["read_price"] is None


def test_the_diet_window_is_at_least_a_week_and_follows_the_span_of_the_calls(tmp_path):
    stats = _spawn_many(tmp_path, 3, used=lambda n: "Read")
    assert stats.window_days == context_budget.MIN_WINDOW_DAYS == 7
    assert _diet_row(stats)["window_days"] == 7.0
    stats.first_when, stats.last_when = 0.0, 20 * 86400.0
    assert stats.window_days == pytest.approx(20.0)
    assert _diet_row(stats)["window_days"] == pytest.approx(20.0)
    assert context_budget.ContextBudgetStats().window_days == 7.0


def test_rarely_used_is_under_a_tenth_with_a_tolerance_for_an_exact_tenth():
    assert not context_budget.rarely_used(3, 30)
    assert not context_budget.rarely_used(1, 10)
    assert context_budget.rarely_used(1, 11)
    assert context_budget.rarely_used(0, 5)
    assert not context_budget.rarely_used(10, 10)


def test_diet_usd_prices_the_prefix_by_who_wrote_it_and_the_body_as_written_by_every_spawn():
    price = dict(write=3.0, read=0.3, later_calls=4.0)
    diet_usd = context_budget.diet_usd
    # The prefix, every spawn wrote it: 10 spawns x 1,000 tokens x (3.0 + 4 x 0.3) per million.
    assert diet_usd(10, 1_000, 0, written_share=1.0, **price) == pytest.approx(10 * 1_000 * 4.2 / 1e6)
    # The prefix, none wrote it: the first call reads it too.
    assert diet_usd(10, 1_000, 0, written_share=0.0, **price) == pytest.approx(10 * 1_000 * 1.5 / 1e6)
    # A tenth wrote it.
    assert diet_usd(10, 1_000, 0, written_share=0.1, **price) == pytest.approx(
        10 * 1_000 * (0.1 * 3.0 + 0.9 * 0.3 + 1.2) / 1e6
    )
    # The body is written by every spawn whatever the prefix share.
    for share in (0.0, 0.5, 1.0):
        assert diet_usd(10, 0, 500, written_share=share, **price) == pytest.approx(10 * 500 * 4.2 / 1e6)
    # A share outside 0 to 1 is held to it.
    assert diet_usd(10, 1_000, 0, written_share=7.0, **price) == diet_usd(10, 1_000, 0, written_share=1.0, **price)
    # Without a read price only the writes count; without a write price nothing does.
    assert diet_usd(10, 1_000, 500, write=3.0, read=None, later_calls=4.0, written_share=1.0) == pytest.approx(
        10 * 1_500 * 3.0 / 1e6
    )
    assert diet_usd(10, 1_000, 500, write=None, read=0.3, later_calls=4.0, written_share=1.0) == 0.0
    assert diet_usd(0, 1_000, 500, written_share=1.0, **price) == 0.0


def test_the_servers_table_sizes_each_rarely_used_server_for_one_spawn_offered_it(tmp_path):
    stats = _spawn_many(tmp_path, 10, used=lambda n: "Read")
    offers = stats.agents["Explore"].servers["claude-sonnet-5"]
    # A deferred list and instructions beside the definitions, as the parser leaves them.
    offers["figma"].deferred, offers["figma"].instructions = 10 * 40.0, 10 * 25.0
    table = _startup_tables(stats)["agent_startup_servers"]
    row = _row(table, "agent_type", "Explore")
    assert row["server"] == "figma" and row["offered_spawns"] == 10 and row["used_spawns"] == 0
    assert row["definition_tokens"] == pytest.approx((_tool_chars(TOOLS[3]) + _tool_chars(TOOLS[4])) / 4.0)
    assert (row["deferred_tokens"], row["instruction_tokens"]) == (40.0, 25.0)
    expected = context_budget.diet_usd(
        10, row["definition_tokens"], 40.0, write=2.5, read=0.2, later_calls=2.0, written_share=0.0
    )
    assert row["saving_usd"] == pytest.approx(expected)
    assert row["window_days"] == 7.0
    assert_privacy(table)


def test_the_servers_table_leaves_out_a_server_that_is_called_often_enough(tmp_path):
    stats = _spawn_many(tmp_path, 10, used=lambda n: "mcp__figma__a" if n < 2 else "Read")
    assert _startup_tables(stats)["agent_startup_servers"].rows == []
    row = _diet_row(stats)
    assert "mcp__figma__*" in row["keep_tools"] and "mcp__figma__*" not in row["rare_tools"]


def test_the_servers_table_keeps_the_largest_servers_per_agent_type(tmp_path):
    stats = _spawn_many(tmp_path, 10, used=lambda n: "Read")
    offers = stats.agents["Explore"].servers["claude-sonnet-5"]
    for n in range(20):
        offers[f"srv{n:02d}"] = context_budget._ServerOffer(offered=10, used=0, definitions=10 * (300.0 + n))
    rows = _startup_tables(stats)["agent_startup_servers"].rows
    assert len(rows) == context_budget._SERVER_ROWS_PER_AGENT
    sizes = [r[5] for r in rows]
    assert sizes == sorted(sizes, reverse=True)
    assert rows[0][2] == "srv19"


def test_the_startup_tables_carry_help_for_every_kept_column(tmp_path):
    stats = context_budget.ContextBudgetStats()
    stats.add_subagent(_sub(tmp_path, "agent-1", _sub_lines()))
    section = context_budget.build_startup_section(stats)
    helptext.annotate_section(section, "api")
    for table in section.tables:
        assert table.dashboard == helptext.placement_for(table.name)
        if table.dashboard == "report":
            continue
        assert table.help is not None and table.help.shows, table.name
        assert [c.key for c in table.columns if not c.help] == [], table.name


def test_the_baseline_and_calibration_tables_carry_help_and_placement(tmp_path):
    top = _build_session(tmp_path, "s1", session_id="sess_1")
    stats = context_budget.ContextBudgetStats()
    stats.add_session("proj-a", top)
    section = context_budget.build_section(stats)
    helptext.annotate_section(section, "api")
    names = {t.name for t in section.tables}
    assert "context_budget_calibration" in names
    for table in section.tables:
        assert table.dashboard == helptext.placement_for(table.name)
        if table.dashboard == "report":
            continue
        assert table.help is not None and table.help.shows, table.name
        assert [c.key for c in table.columns if not c.help] == [], table.name


def test_the_controllable_part_of_the_first_call_is_not_the_whole_of_it(tmp_path):
    """The first call is mostly Claude Code's own tool JSON: the controllable
    part is the skills list, the memory files and the MCP tools."""
    top = _build_session(
        tmp_path, "s1", session_id="sess_1", with_skill_listing=True, skill_listing_chars=4_000,
        baseline_cache_creation=90_000,
    )
    stats = context_budget.ContextBudgetStats()
    stats.add_session("proj-a", top)
    table = next(t for t in context_budget.build_section(stats).tables if t.name == "context_budget_baseline")
    row = _row(table, "project", "proj-a")
    assert row["mean_baseline"] == pytest.approx(90_100)
    assert row["controllable_est"] == pytest.approx(1_000)

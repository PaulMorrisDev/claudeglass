"""Tests for S1-context-budget's context-budget analytics
(src/claudeglass/context_budget.py).

Fixtures are synthetic, built at test time via ``tests/helpers`` (matching
this codebase's established convention -- see e.g. ``test_compaction.py``'s
own module docstring), plus one smoke test over the committed real
fixture at ``tests/fixtures/real/session-a``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claudeglass import context_budget, statusline
from claudeglass.model import TranscriptMeta
from claudeglass.parse import parse_transcript
from claudeglass.pricing import load_pricing
from claudeglass.snapshots import Snapshot

PRICING = load_pricing()

from helpers import (
    assert_privacy,
    attachment_line,
    system_line,
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
    assert proj_row[col["mean_baseline"]] == pytest.approx(40_000)
    assert proj_row[col["median_baseline"]] == pytest.approx(40_000)
    assert proj_row[col["human_prompt_est"]] == pytest.approx(10.0)
    assert proj_row[col["skills_listing_est"]] == pytest.approx(100.0)
    # No snapshot: memory/agents/mcp all null.
    assert proj_row[col["memory_files_est"]] is None
    assert proj_row[col["custom_agents_est"]] is None
    assert proj_row[col["mcp_tools_est"]] is None
    # Residual = 40000 - (10 + 100) = 39890.
    assert proj_row[col["system_prompt_and_tools_est"]] == pytest.approx(39_890.0)

    every_column_label_ends_est = all(
        c.label.endswith("(est)") or c.key in ("project", "sessions", "mean_baseline", "median_baseline")
        for c in table.columns
    )
    assert every_column_label_ends_est

    # Every note documents the characters/4 approximation.
    assert any("characters" in n and "4" in n for n in table.notes)
    assert_privacy(section)


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
    assert proj_row[col["system_prompt_and_tools_est"]] == pytest.approx(50_000 - human_est - skills_est)


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
    assert all_row[col["mean_baseline"]] == pytest.approx(20_000.0)
    # The "all" row never carries per-project config-snapshot buckets.
    assert all_row[col["memory_files_est"]] is None
    assert all_row[col["custom_agents_est"]] is None


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
    assert len(section.tables) == 3
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

"""``Table.lead_columns`` (``helptext.TableCopy.lead_columns``): the
columns the dashboard shows first on a wide table, or as tiles on a
one-row summary table.

Every key must name a real column of its table, a summary table picks
at most 4 headline values and any other table at most 7, and every
dashboard table wider than 7 columns has them. The report is built over
the real fixture (an empty project when it isn't checked out), with the
optional sections switched on so every table that has lead columns is
built at least once.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from claudeglass import elasticity, helptext
from claudeglass.config import Config
from claudeglass.corpus import load_corpus
from claudeglass.pricing import load_pricing
from claudeglass.render.html import render_html
from claudeglass.render.json_out import render_json
from claudeglass.render.markdown import render_markdown
from claudeglass.report import build_report
from claudeglass.snapshots import Snapshot

FIXTURES = Path(__file__).parent / "fixtures"
REAL = FIXTURES / "real" / "session-a"

#: Tables whose builder always emits exactly one row. The dashboard shows
#: their lead columns as a strip of at most 4 tiles.
ONE_ROW_TABLES = {
    "cost_record_summary",
    "recache_summary",
    "recache_huge_context",
    "limits_summary",
    "limits_pauses",
    "model_swap_summary",
    "plan_handoff_summary",
    "hooks_summary",
    "tool_search_summary",
    "run_split_summary",
    "waste_summary",
    "topology_session_baseline",
    "overall",
    "elasticity_recent_burn",
}

#: The grid's default: this many columns before the "Columns (N)" chooser.
GRID_COLUMNS = 7
TILES = 4


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    if REAL.exists() and any(REAL.glob("*.jsonl")):
        project_dirs = [REAL]
    else:
        empty = tmp_path_factory.mktemp("lead") / "proj"
        empty.mkdir()
        project_dirs = [empty]
    snapshots = [
        Snapshot(path=str(path), ts=path.stem, data=json.loads(path.read_text(encoding="utf-8")))
        for path in sorted((FIXTURES / "snapshots").glob("*.json"))
    ]
    baseline_record = {
        "id": "abc123",
        "created_at": "2026-09-01T00:00:00+00:00",
        "window_days": 7,
        "sessions_analysed": 6,
        "mode_mix": {"interactive": 6},
        "by_mode": {"interactive": {"sessions": 6, "cost_per_session": 0.01}},
    }
    model = build_report(
        load_corpus(project_dirs),
        load_pricing(),
        Config(),
        projects=("proj",),
        window="w",
        phases=True,
        group_by="agent",
        snapshots=snapshots,
        baseline_record=baseline_record,
        usage_log_rows=[{"session_id": "s1", "cache_warm": False, "cache_misses": 2, "cache_miss_causes": "tools:2"}],
    )
    # The elasticity section needs subscription billing and usage-log
    # readings; its builder is called directly instead.
    section = elasticity.build_section(elasticity.ElasticityStats())
    helptext.annotate_section(section, "subscription")
    model.sections.append(section)
    return model


def _tables(report):
    return [table for section in report.sections for table in section.tables]


def test_every_lead_column_names_a_real_column(report):
    built = {t.name for t in _tables(report)}
    unbuilt = sorted(
        name
        for name, table_copy in helptext.TABLE_COPY.items()
        # "config-diff-" is a prefix: one table per changed setting.
        if table_copy.lead_columns and name not in built and not any(b.startswith(name) for b in built if name.endswith("-"))
    )
    assert unbuilt == [], f"build these tables in the fixture so their lead columns are checked: {unbuilt}"
    for table in _tables(report):
        keys = [c.key for c in table.columns]
        assert all(key in keys for key in table.lead_columns), f"{table.name}: {table.lead_columns} vs {keys}"
        assert len(set(table.lead_columns)) == len(table.lead_columns), table.name


def test_summary_tables_pick_at_most_four_tiles_and_others_at_most_seven(report):
    seen = set()
    for table in _tables(report):
        if table.name in ONE_ROW_TABLES:
            seen.add(table.name)
            assert len(table.rows) == 1, f"{table.name} is listed as one-row but has {len(table.rows)} rows"
            assert 0 < len(table.lead_columns) <= TILES, table.name
            assert table.columns[0].key not in table.lead_columns, f"{table.name}: the row key is not a headline"
        elif table.lead_columns:
            assert len(table.lead_columns) <= GRID_COLUMNS, table.name
            assert table.lead_columns[0] == table.columns[0].key, f"{table.name}: the row key comes first"
    assert seen == ONE_ROW_TABLES


def test_every_wide_dashboard_table_has_lead_columns(report):
    missing = sorted(
        table.name
        for table in _tables(report)
        if table.dashboard != "report" and len(table.columns) > GRID_COLUMNS and not table.lead_columns
    )
    assert missing == []


def test_lead_columns_reach_the_json_and_not_the_markdown_or_html(report):
    tables = {t["name"]: t for s in json.loads(render_json(report))["report"]["sections"] for t in s["tables"]}
    assert tables["ttl_by_agent_type"]["lead_columns"] == helptext.TABLE_COPY["ttl_by_agent_type"].lead_columns
    assert tables["waste_summary"]["lead_columns"] == helptext.TABLE_COPY["waste_summary"].lead_columns
    assert tables["by_day"]["lead_columns"] == []
    # Display only: the CLI renderers print the same report without it.
    bare = copy.deepcopy(report)
    for table in _tables(bare):
        table.lead_columns = []
    assert render_markdown(bare) == render_markdown(report)
    assert render_html(bare) == render_html(report)

"""Tests for claudeglass.snapshots (WP7): loading the fixture
snapshot files, the session/snapshot join, auto-detected diff keys,
co-changed keys, and the config-diff table's grouping/exclusion/notes.

Fixtures live under tests/fixtures/snapshots/: three real-shaped snapshot
JSON files at 2026-09-01, 2026-09-10 and 2026-09-15. Between the first two,
``user_settings.autoCompactWindow`` drops 300000 -> 150000 and
``user_settings.effortLevel`` drops high -> medium at the same time (a
deliberate co-change); the third snapshot repeats the second's values
unchanged, and ``agents.verification-runner.experimental.cacheTtl`` stays
"1h" throughout every snapshot.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from claudeglass import snapshots as snap_mod
from claudeglass.pricing import load_pricing

from helpers import assert_privacy

#: The fixture files live directly under tests/fixtures/snapshots/ (per the
#: WP7 brief), not under the <config_dir>/snapshots/ layout load_snapshots()
#: expects on a real config dir (config_dir being the claudeglass directory
#: itself -- see load_snapshots's docstring, fix config-dir). _load() below
#: reads them straight off disk into Snapshot objects for the join/diff/table
#: tests; test_load_snapshots_* separately proves load_snapshots() itself
#: walks that <config_dir>/snapshots/ layout correctly, using a copy of
#: these same fixture files.
_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "snapshots"


def _load() -> list[snap_mod.Snapshot]:
    result = []
    for path in sorted(_FIXTURES_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        result.append(snap_mod.Snapshot(path=path, ts=data["ts"], data=data))
    result.sort(key=lambda snap: snap.ts)
    return result


def _copy_fixtures_into_config_dir(config_dir: Path) -> None:
    # Fix config-dir: config_dir is the claudeglass directory itself
    # (matching every caller's convention now -- see load_snapshots's
    # docstring), so snapshots live directly under it, not nested one
    # more "claudeglass" level down.
    snapshots_dir = config_dir / "snapshots"
    snapshots_dir.mkdir(parents=True)
    for path in _FIXTURES_DIR.glob("*.json"):
        shutil.copy2(path, snapshots_dir / path.name)


# -- load_snapshots -----------------------------------------------------


def test_load_snapshots_returns_all_three_sorted_by_ts(tmp_path):
    _copy_fixtures_into_config_dir(tmp_path)
    result = snap_mod.load_snapshots(tmp_path)
    assert [s.ts for s in result] == [
        "20260901T000000Z",
        "20260910T000000Z",
        "20260915T000000Z",
    ]


def test_load_snapshots_missing_dir_returns_empty_list(tmp_path):
    assert snap_mod.load_snapshots(tmp_path / "does-not-exist") == []


def test_load_snapshots_skips_malformed_file(tmp_path):
    snapshots_dir = tmp_path / "snapshots"
    snapshots_dir.mkdir(parents=True)
    (snapshots_dir / "20260901T000000Z.json").write_text(
        "not valid json", encoding="utf-8"
    )
    (snapshots_dir / "20260902T000000Z.json").write_text(
        '{"ts": "20260902T000000Z", "user_settings": {}}', encoding="utf-8"
    )
    result = snap_mod.load_snapshots(tmp_path)
    assert [s.ts for s in result] == ["20260902T000000Z"]


# -- managed_keys (fix 7) ------------------------------------------------


def test_managed_keys_returns_recorded_key_list():
    snap = snap_mod.Snapshot(
        path=Path("x"), ts="t", data={"managed_keys": ["model", "permissions"]}
    )
    assert snap_mod.managed_keys(snap) == ["model", "permissions"]


def test_managed_keys_missing_field_returns_empty_list():
    # A snapshot written before fix 7 (or from a machine with no
    # managed-settings file) has no managed_keys field at all.
    snap = snap_mod.Snapshot(path=Path("x"), ts="t", data={})
    assert snap_mod.managed_keys(snap) == []


def test_managed_keys_malformed_field_returns_empty_list():
    snap = snap_mod.Snapshot(path=Path("x"), ts="t", data={"managed_keys": "not-a-list"})
    assert snap_mod.managed_keys(snap) == []


def test_flatten_snapshot_includes_managed_settings_section():
    snap = snap_mod.Snapshot(
        path=Path("x"),
        ts="t",
        data={"managed_settings": {"model": "sonnet"}, "managed_keys": ["model"]},
    )
    flat = snap_mod.flatten_snapshot(snap)
    assert flat["managed_settings.model"] == "sonnet"


# -- snapshot_for (the session/snapshot join) ----------------------------


def test_snapshot_for_returns_latest_at_or_before_session_start():
    snapshots = _load()
    found = snap_mod.snapshot_for("2026-09-12T00:00:00.000Z", snapshots)
    assert found is not None
    assert found.ts == "20260910T000000Z"


def test_snapshot_for_boundary_is_inclusive():
    snapshots = _load()
    found = snap_mod.snapshot_for("2026-09-01T00:00:00.000Z", snapshots)
    assert found is not None
    assert found.ts == "20260901T000000Z"


def test_snapshot_for_session_predating_every_snapshot_is_none():
    snapshots = _load()
    found = snap_mod.snapshot_for("2026-08-20T00:00:00.000Z", snapshots)
    assert found is None


def test_snapshot_for_unparsable_timestamp_is_none():
    snapshots = _load()
    assert snap_mod.snapshot_for("not-a-timestamp", snapshots) is None


def test_snapshot_for_picks_latest_of_two_candidates():
    snapshots = _load()
    found = snap_mod.snapshot_for("2026-09-20T00:00:00.000Z", snapshots)
    assert found is not None
    assert found.ts == "20260915T000000Z"


# -- diff_keys ------------------------------------------------------------


def test_diff_keys_includes_every_key_that_changed():
    snapshots = _load()
    diff = snap_mod.diff_keys(snapshots)
    assert "user_settings.autoCompactWindow" in diff
    assert diff["user_settings.autoCompactWindow"] == [300000, 150000, 150000]
    assert "user_settings.effortLevel" in diff
    assert diff["user_settings.effortLevel"] == ["high", "medium", "medium"]


def test_diff_keys_excludes_unchanged_keys():
    snapshots = _load()
    diff = snap_mod.diff_keys(snapshots)
    assert "user_settings.model" not in diff
    assert "agents.verification-runner.experimental.cacheTtl" not in diff
    assert "agents.verification-runner.model" not in diff
    assert "env_names" not in diff


# -- co_changed_keys --------------------------------------------------------


def test_co_changed_keys_between_two_snapshots():
    snapshots = _load()
    changed = snap_mod.co_changed_keys(snapshots[0], snapshots[1])
    assert "user_settings.autoCompactWindow" in changed
    assert "user_settings.effortLevel" in changed
    assert "user_settings.model" not in changed


def test_co_changed_keys_is_empty_between_identical_snapshots():
    snapshots = _load()
    assert snap_mod.co_changed_keys(snapshots[1], snapshots[2]) == []


# -- build_config_diff_table ------------------------------------------------


def _sessions_with_metrics() -> list[dict]:
    return [
        {
            "session_id": "predates-everything",
            "first_ts": "2026-08-20T00:00:00.000Z",
            "turns": 10,
            "cost": 1.0,
            "recache_cc": 0,
            "cc_total": 100,
            "compactions": 0,
            "span_s": 60,
        },
        {
            "session_id": "s1-300k",
            "first_ts": "2026-09-05T00:00:00.000Z",
            "turns": 100,
            "cost": 5.0,
            "recache_cc": 20,
            "cc_total": 200,
            "compactions": 1,
            "span_s": 600,
        },
        {
            "session_id": "s2-300k",
            "first_ts": "2026-09-06T00:00:00.000Z",
            "turns": 200,
            "cost": 7.0,
            "recache_cc": 30,
            "cc_total": 300,
            "compactions": 2,
            "span_s": 1200,
        },
        {
            "session_id": "s3-150k",
            "first_ts": "2026-09-12T00:00:00.000Z",
            "turns": 50,
            "cost": 2.0,
            "recache_cc": 5,
            "cc_total": 100,
            "compactions": 0,
            "span_s": 300,
        },
        {
            "session_id": "s4-150k",
            "first_ts": "2026-09-20T00:00:00.000Z",
            "turns": 150,
            "cost": 6.0,
            "recache_cc": 15,
            "cc_total": 150,
            "compactions": 1,
            "span_s": 900,
        },
    ]


def test_build_config_diff_table_groups_by_value():
    snapshots = _load()
    table = snap_mod.build_config_diff_table(
        _sessions_with_metrics(), snapshots, "user_settings.autoCompactWindow"
    )

    assert [col.key for col in table.columns] == [
        "value",
        "sessions",
        "turns",
        "cost",
        "cost_per_session",
        "recache_share",
        "compactions_per_session",
        "median_span",
    ]
    assert len(table.rows) == 2

    by_value = {row[0]: row for row in table.rows}

    row_150k = by_value[150000]
    assert row_150k[1] == 2  # sessions
    assert row_150k[2] == 200  # turns
    assert row_150k[3] == 8.0  # cost
    assert row_150k[4] == 4.0  # cost/session
    assert row_150k[5] == 8.0  # recache share pct: (5+15)/(100+150)*100
    assert row_150k[6] == 0.5  # compactions/session
    assert row_150k[7] == 600  # median span

    row_300k = by_value[300000]
    assert row_300k[1] == 2
    assert row_300k[2] == 300
    assert row_300k[3] == 12.0
    assert row_300k[4] == 6.0
    assert row_300k[5] == 10.0  # (20+30)/(200+300)*100
    assert row_300k[6] == 1.5
    assert row_300k[7] == 900


def test_build_config_diff_table_excludes_and_notes_predating_sessions():
    snapshots = _load()
    table = snap_mod.build_config_diff_table(
        _sessions_with_metrics(), snapshots, "user_settings.autoCompactWindow"
    )
    total_sessions_in_rows = sum(row[1] for row in table.rows)
    assert total_sessions_in_rows == 4  # the 5th (predating) session excluded

    assert any("predate the earliest config snapshot" in note for note in table.notes)
    assert any("1 session" in note for note in table.notes)


def test_build_config_diff_table_notes_co_changed_keys():
    snapshots = _load()
    table = snap_mod.build_config_diff_table(
        _sessions_with_metrics(), snapshots, "user_settings.autoCompactWindow"
    )
    co_changed_note = next(
        note for note in table.notes if "changed at the same time" in note
    )
    assert "effortLevel (your settings)" in co_changed_note


def test_build_config_diff_table_no_co_changed_keys_note():
    snapshots = _load()
    table = snap_mod.build_config_diff_table(
        _sessions_with_metrics(), snapshots, "user_settings.model"
    )
    assert any("No other setting changed" in note for note in table.notes)


# -- build_config_section -----------------------------------------------


def test_build_config_section_wraps_the_diff_table_in_a_section():
    snapshots = _load()
    section = snap_mod.build_config_section(
        _sessions_with_metrics(), snapshots, "user_settings.autoCompactWindow"
    )
    assert section.key == "config_diff"
    assert section.title == "Config diff"
    assert len(section.tables) == 1

    table = section.tables[0]
    diff_table = snap_mod.build_config_diff_table(
        _sessions_with_metrics(), snapshots, "user_settings.autoCompactWindow"
    )
    assert table.name == diff_table.name
    assert table.title == diff_table.title
    assert [col.key for col in table.columns] == [col.key for col in diff_table.columns]
    assert table.notes == diff_table.notes
    assert len(table.rows) == len(diff_table.rows)


def test_build_config_section_stringifies_the_value_column():
    """build_config_diff_table's own "value" column holds the config
    value verbatim (here, an int: 150000/300000) -- build_config_section
    stringifies it so every Section's Table has a first column usable as
    a row key regardless of the underlying config value's type."""
    snapshots = _load()
    section = snap_mod.build_config_section(
        _sessions_with_metrics(), snapshots, "user_settings.autoCompactWindow"
    )
    table = section.tables[0]
    for row in table.rows:
        assert isinstance(row[0], str) and row[0]
    assert {row[0] for row in table.rows} == {"150000", "300000"}


def test_build_config_section_renders_none_value_as_unset(tmp_path):
    # A key present in one snapshot's flattened config but absent from
    # another resolves to a raw None for the missing side -- exercise
    # that via a key that only appears in the later snapshot.
    snapshots = _load()
    table_key = "user_settings.effortLevel"
    section = snap_mod.build_config_section(_sessions_with_metrics(), snapshots, table_key)
    table = section.tables[0]
    # None the fixture doesn't need to actually trip: confirm the helper
    # itself renders None as "(unset)" and never as the literal "None".
    assert snap_mod._stringify_config_value(None) == "(unset)"
    for row in table.rows:
        assert row[0] != "None"


def test_build_config_section_is_privacy_clean():
    snapshots = _load()
    section = snap_mod.build_config_section(
        _sessions_with_metrics(), snapshots, "user_settings.autoCompactWindow"
    )
    assert_privacy(section)


# -- schema 2: effective_config / effective_provenance / layers -------------


def _schema2_snapshot(
    project_slug: str = "proj-a",
    effective: dict | None = None,
    effective_provenance: dict | None = None,
    settings_layers: dict | None = None,
    ts: str = "20260901T000000Z",
    **extra,
) -> snap_mod.Snapshot:
    data = {
        "schema": 2,
        "ts": ts,
        "project_slug": project_slug,
        "effective": effective if effective is not None else {},
        "effective_provenance": effective_provenance if effective_provenance is not None else {},
        "settings_layers": settings_layers if settings_layers is not None else {},
        **extra,
    }
    return snap_mod.Snapshot(path=Path(f"{ts}.json"), ts=ts, data=data)


def test_effective_config_returns_the_effective_field():
    snap = _schema2_snapshot(effective={"model": "sonnet", "effortLevel": "high"})
    assert snap_mod.effective_config(snap) == {"model": "sonnet", "effortLevel": "high"}


def test_effective_config_schema1_snapshot_is_empty():
    snap = snap_mod.Snapshot(path=Path("x"), ts="t", data={"schema": 1})
    assert snap_mod.effective_config(snap) == {}


def test_effective_provenance_returns_the_provenance_field():
    snap = _schema2_snapshot(effective_provenance={"model": "project_local"})
    assert snap_mod.effective_provenance(snap) == {"model": "project_local"}


def test_effective_provenance_schema1_snapshot_is_empty():
    snap = snap_mod.Snapshot(path=Path("x"), ts="t", data={"schema": 1})
    assert snap_mod.effective_provenance(snap) == {}


def test_auto_compact_window_reads_the_setting():
    snap = _schema2_snapshot(effective={"autoCompactWindow": 300_000})
    assert snap_mod.auto_compact_window(snap) == 300_000
    assert snap_mod.effective_config_in_force(snap) == {"autoCompactWindow": 300_000}
    assert snap_mod.auto_compact_window(None) is None


def test_the_env_variable_overrides_the_auto_compact_window_setting():
    # docs/en/env-vars.md: it beats the setting, and is clamped to 100K-1M.
    for value, expected in ((400_000, 400_000), (500, 100_000), (2_000_000, 1_000_000)):
        snap = _schema2_snapshot(
            effective={"autoCompactWindow": 300_000},
            env_names=["CLAUDE_CODE_AUTO_COMPACT_WINDOW"],
            env_numeric_caps={"CLAUDE_CODE_AUTO_COMPACT_WINDOW": value},
        )
        assert snap_mod.auto_compact_window(snap) == expected
        in_force = snap_mod.effective_config_in_force(snap)
        assert in_force["autoCompactWindow"] == expected
        assert in_force["env.CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == expected
        # The written settings stay as they are.
        assert snap_mod.effective_config(snap) == {"autoCompactWindow": 300_000}


def test_an_env_window_without_a_recorded_value_is_unknown_not_the_setting():
    # Set in a settings env block, captured by a hook that kept no value.
    snap = _schema2_snapshot(
        effective={"autoCompactWindow": 300_000},
        effective_env_names=["CLAUDE_CODE_AUTO_COMPACT_WINDOW"],
    )
    assert snap_mod.auto_compact_window(snap) is None
    assert snap_mod.effective_config_in_force(snap) == {"env.CLAUDE_CODE_AUTO_COMPACT_WINDOW": "set"}


def test_layers_returns_the_settings_layers_field():
    snap = _schema2_snapshot(settings_layers={"user": {"present": True}})
    assert snap_mod.layers(snap) == {"user": {"present": True}}


def test_layers_schema1_snapshot_is_empty():
    snap = snap_mod.Snapshot(path=Path("x"), ts="t", data={"schema": 1})
    assert snap_mod.layers(snap) == {}


# -- schema 2: latest_snapshot_per_project -----------------------------------


def test_latest_snapshot_per_project_keeps_the_last_one_seen():
    older = _schema2_snapshot(project_slug="proj-a", ts="20260901T000000Z", effective={"model": "sonnet"})
    newer = _schema2_snapshot(project_slug="proj-a", ts="20260910T000000Z", effective={"model": "opus"})
    other = _schema2_snapshot(project_slug="proj-b", ts="20260905T000000Z", effective={"model": "fable"})
    latest = snap_mod.latest_snapshot_per_project([older, newer, other])
    assert set(latest) == {"proj-a", "proj-b"}
    assert latest["proj-a"] is newer
    assert latest["proj-b"] is other


def test_latest_snapshot_per_project_schema1_snapshots_collapse_to_one_bucket():
    a = snap_mod.Snapshot(path=Path("a"), ts="20260901T000000Z", data={"schema": 1, "ts": "20260901T000000Z"})
    b = snap_mod.Snapshot(path=Path("b"), ts="20260902T000000Z", data={"schema": 1, "ts": "20260902T000000Z"})
    latest = snap_mod.latest_snapshot_per_project([a, b])
    assert list(latest) == ["(unknown project)"]
    assert latest["(unknown project)"] is b


# -- schema 2: with_every_project_agents -------------------------------------


def _agent(source: str, model: str) -> dict:
    return {"source": source, "model": model}


def test_with_every_project_agents_adds_another_projects_agents_to_the_newest_snapshot():
    """The hook records only the agents of the project a session started
    in, so the newest snapshot alone would call another project's agents
    unknown."""
    other = _schema2_snapshot(
        project_slug="revixo",
        ts="20260920T000000Z",
        effective={"model": "opus"},
        agents={"implementer": _agent("project", "haiku")},
        effective_agents={"implementer": {"source": "project", "model": "haiku"}},
    )
    newest = _schema2_snapshot(
        project_slug="claudeglass", ts="20260923T000000Z", effective={"model": "sonnet"}, agents={}, effective_agents={}
    )

    view = snap_mod.with_every_project_agents([other, newest])

    assert snap_mod.effective_config(view) == {"model": "sonnet"}
    assert view.ts == newest.ts
    assert view.data["agents"] == {"implementer": _agent("project", "haiku")}
    assert view.data["effective_agents"]["implementer"]["model"] == "haiku"
    assert newest.data["agents"] == {}


def test_with_every_project_agents_keeps_a_project_agent_over_a_newer_user_agent_of_the_same_name():
    project = _schema2_snapshot(
        project_slug="revixo", ts="20260920T000000Z", agents={"reviewer": _agent("project", "opus")}
    )
    user = _schema2_snapshot(
        project_slug="claudeglass", ts="20260923T000000Z", agents={"reviewer": _agent("user", "sonnet")}
    )

    view = snap_mod.with_every_project_agents([project, user])

    assert view.data["agents"]["reviewer"] == _agent("project", "opus")


def test_with_every_project_agents_takes_the_newer_of_two_user_agent_records():
    older = _schema2_snapshot(project_slug="a", ts="20260920T000000Z", agents={"helper": _agent("user", "opus")})
    newer = _schema2_snapshot(project_slug="b", ts="20260923T000000Z", agents={"helper": _agent("user", "haiku")})

    view = snap_mod.with_every_project_agents([older, newer])

    assert view.data["agents"]["helper"] == _agent("user", "haiku")


def test_with_every_project_agents_reads_settings_from_the_newest_snapshot_that_records_them():
    settings = _schema2_snapshot(project_slug="a", ts="20260920T000000Z", effective={"model": "sonnet"})
    schema1 = snap_mod.Snapshot(path=Path("b"), ts="20260923T000000Z", data={"schema": 1, "agents": {}})

    view = snap_mod.with_every_project_agents([settings, schema1])

    assert snap_mod.effective_config(view) == {"model": "sonnet"}


def test_with_every_project_agents_is_none_without_snapshots():
    assert snap_mod.with_every_project_agents([]) is None


# -- schema 2: build_effective_config_table ----------------------------------


def test_build_effective_config_table_one_row_per_project_and_key():
    proj_a = _schema2_snapshot(
        project_slug="proj-a",
        effective={"model": "sonnet", "effortLevel": "high"},
        effective_provenance={"model": "user", "effortLevel": "managed"},
    )
    proj_b = _schema2_snapshot(
        project_slug="proj-b", ts="20260902T000000Z", effective={"model": "opus"}, effective_provenance={"model": "project_shared"}
    )
    table = snap_mod.build_effective_config_table([proj_a, proj_b])
    rows_by_key = {(row[0], row[1]): row for row in table.rows}
    assert rows_by_key[("proj-a", "model")][2:] == ["sonnet", "user"]
    assert rows_by_key[("proj-a", "effortLevel")][2:] == ["high", "managed"]
    assert rows_by_key[("proj-b", "model")][2:] == ["opus", "project_shared"]
    assert table.notes == []


def test_build_effective_config_table_notes_when_no_schema2_data():
    schema1_snap = snap_mod.Snapshot(path=Path("x"), ts="t", data={"schema": 1, "ts": "t"})
    table = snap_mod.build_effective_config_table([schema1_snap])
    assert table.rows == []
    assert any("No schema-2 effective config" in note for note in table.notes)


def test_build_effective_config_table_empty_input_has_no_notes():
    table = snap_mod.build_effective_config_table([])
    assert table.rows == []
    assert table.notes == []


# -- schema 2: build_config_layers_table -------------------------------------


def test_build_config_layers_table_one_row_per_layer_per_project():
    snap = _schema2_snapshot(
        project_slug="proj-a",
        settings_layers={
            "managed": {"present": True},
            "project_local": {"present": False},
            "project_shared": {"present": True},
            "user": {"present": True},
        },
        content_layers={
            "agents_summary": {"count": 2},
            "skills": {"project": {"names": ["a"]}, "user": {"names": ["b", "c"]}},
            "rules": {"count": 3},
            "commands": {"count": 1},
            "claude_md": {"user_bytes": 10, "project_root_bytes": 20, "project_local_bytes": None, "nested_bytes": 5},
        },
        mcp_servers={"names": ["filesystem", "github"]},
    )
    table = snap_mod.build_config_layers_table([snap])
    assert len(table.rows) == 4  # one row per SETTINGS_LAYER_NAMES entry
    by_layer = {row[1]: row for row in table.rows}
    managed_row = by_layer["managed"]
    assert managed_row[0] == "proj-a"
    assert managed_row[2] is True
    assert managed_row[3] == 2  # agents
    assert managed_row[4] == 3  # skills (1 project + 2 user)
    assert managed_row[5] == 3  # rules
    assert managed_row[6] == 35  # claude_md_bytes (10+20+5, None skipped)
    assert managed_row[7] == 1  # commands
    assert managed_row[8] == 2  # mcp servers
    assert by_layer["project_local"][2] is False


def test_build_config_layers_table_missing_content_layers_degrades_to_zero():
    snap = _schema2_snapshot(project_slug="proj-a")
    table = snap_mod.build_config_layers_table([snap])
    assert len(table.rows) == 4
    for row in table.rows:
        assert row[3:] == [0, 0, 0, 0, 0, 0]


# -- schema 2: build_config_groups_table -------------------------------------


def test_build_config_groups_table_groups_identical_effective_configs():
    proj_a = _schema2_snapshot(project_slug="proj-a", effective={"model": "sonnet"})
    proj_b = _schema2_snapshot(project_slug="proj-b", ts="20260902T000000Z", effective={"model": "sonnet"})
    proj_c = _schema2_snapshot(project_slug="proj-c", ts="20260903T000000Z", effective={"model": "opus"})
    table = snap_mod.build_config_groups_table([proj_a, proj_b, proj_c])
    assert len(table.rows) == 2
    by_count = sorted(table.rows, key=lambda r: -r[1])
    assert by_count[0][1] == 2
    assert by_count[0][2] == "proj-a, proj-b"
    assert by_count[1][1] == 1
    assert by_count[1][2] == "proj-c"


def test_build_config_groups_table_counts_sessions_per_group():
    proj_a = _schema2_snapshot(project_slug="proj-a", effective={"model": "sonnet"})
    sessions = [
        {"session_id": "s1", "first_ts": "2026-09-05T00:00:00.000Z"},
        {"session_id": "s2", "first_ts": "2026-09-06T00:00:00.000Z"},
    ]
    table = snap_mod.build_config_groups_table([proj_a], sessions)
    assert table.rows[0][3] == 2


def test_build_config_groups_table_no_snapshots_is_empty():
    table = snap_mod.build_config_groups_table([])
    assert table.rows == []


# -- schema 2: detect_drift / build_config_drift_table -----------------------


def test_detect_drift_finds_mismatched_keys():
    snap = _schema2_snapshot(effective={"model": "sonnet", "effortLevel": "high"})
    mismatches = snap_mod.detect_drift(snap, {"model": "opus", "effortLevel": "high"})
    assert mismatches == [("model", "sonnet", "opus")]


def test_detect_drift_skips_keys_absent_from_effective_config():
    snap = _schema2_snapshot(effective={"model": "sonnet"})
    mismatches = snap_mod.detect_drift(snap, {"unrelated_key": "value"})
    assert mismatches == []


def test_detect_drift_no_mismatch_returns_empty_list():
    snap = _schema2_snapshot(effective={"model": "sonnet"})
    assert snap_mod.detect_drift(snap, {"model": "sonnet"}) == []


def test_build_config_drift_table_one_row_per_mismatch():
    snapshots = [_schema2_snapshot(ts="20260901T000000Z", effective={"model": "sonnet"})]
    sessions_with_observed = [
        {"session_id": "s1", "first_ts": "2026-09-05T00:00:00.000Z", "observed": {"model": "opus"}},
    ]
    table = snap_mod.build_config_drift_table(sessions_with_observed, snapshots)
    assert len(table.rows) == 1
    assert table.rows[0] == ["s1", "model", "sonnet", "opus"]
    assert table.notes == []


def test_build_config_drift_table_excludes_sessions_predating_every_snapshot():
    snapshots = [_schema2_snapshot(ts="20260901T000000Z", effective={"model": "sonnet"})]
    sessions_with_observed = [
        {"session_id": "s1", "first_ts": "2026-08-01T00:00:00.000Z", "observed": {"model": "opus"}},
    ]
    table = snap_mod.build_config_drift_table(sessions_with_observed, snapshots)
    assert table.rows == []
    assert any("predate the earliest config snapshot" in note for note in table.notes)


def test_build_config_drift_table_no_drift_notes_it():
    snapshots = [_schema2_snapshot(ts="20260901T000000Z", effective={"model": "sonnet"})]
    sessions_with_observed = [
        {"session_id": "s1", "first_ts": "2026-09-05T00:00:00.000Z", "observed": {"model": "sonnet"}},
    ]
    table = snap_mod.build_config_drift_table(sessions_with_observed, snapshots)
    assert table.rows == []
    assert any("No drift detected" in note for note in table.notes)


# -- detect_drift: a model setting compared by what it can mean --------------


def _model_drift(setting: object, observed: str, resolve: bool = True) -> list:
    """``detect_drift`` on the ``model`` key alone, resolved through the
    packaged rate card. Loaded inside the test, where conftest has already
    pointed the config dir at a throwaway one."""
    resolve_model = load_pricing().resolve_model if resolve else None
    snap = _schema2_snapshot(effective={"model": setting})
    return snap_mod.detect_drift(snap, {"model": observed}, resolve_model=resolve_model)


@pytest.mark.parametrize("setting", ["opus", "opus[1m]", "Opus"])
def test_detect_drift_opus_alias_accepts_an_older_opus(setting):
    """``opus`` follows the newest Opus, so a session that ran an older one
    is the same family, not drift."""
    assert _model_drift(setting, "claude-opus-5") == []
    assert _model_drift(setting, "claude-opus-4-5-20251101") == []


def test_detect_drift_alias_still_drifts_across_families():
    assert _model_drift("opus", "claude-sonnet-5-5") == [("model", "opus", "claude-sonnet-5-5")]
    assert _model_drift("sonnet[1m]", "claude-opus-5-5") == [("model", "sonnet[1m]", "claude-opus-5-5")]


def test_detect_drift_explicit_id_compares_exactly():
    assert _model_drift("claude-opus-5-5", "claude-opus-5") == [("model", "claude-opus-5-5", "claude-opus-5")]
    assert _model_drift("claude-opus-5-5", "claude-opus-5-5") == []


def test_detect_drift_explicit_alias_id_compares_by_canonical_id_not_family():
    """``claude-haiku-4-5`` is a rate-card alias but an explicit pin: it
    matches its own dated id and nothing else of the Haiku family."""
    assert _model_drift("claude-haiku-4-5", "claude-haiku-4-5-20251001") == []
    assert _model_drift("claude-haiku-4-5", "claude-3-5-haiku-20241022") != []


def test_detect_drift_newer_release_priced_as_an_older_one_is_its_own_pin():
    """Until the rate card has a row for ``claude-opus-5-7`` it is priced
    as ``claude-opus-5``, but a pin on one and a session on the other are
    still different releases. Its dated and cloud forms are the same one."""
    assert _model_drift("claude-opus-5-7", "claude-opus-5") == [("model", "claude-opus-5-7", "claude-opus-5")]
    assert _model_drift("claude-opus-5", "claude-opus-5-7") == [("model", "claude-opus-5", "claude-opus-5-7")]
    assert _model_drift("claude-opus-5-7", "claude-opus-5-7-20261101") == []
    assert _model_drift("claude-opus-5-7", "us.anthropic.claude-opus-5-7-v1:0") == []


@pytest.mark.parametrize(
    "old,new",
    [
        ("us.anthropic.claude-opus-4-1-20250805-v1:0", "us.anthropic.claude-opus-4-5-20251101-v1:0"),
        ("claude-opus-4-1@20250805", "claude-opus-4-5@20251101"),
        (
            "arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.anthropic.claude-opus-4-1-20250805-v1:0",
            "arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.anthropic.claude-opus-4-5-20251101-v1:0",
        ),
    ],
)
def test_detect_drift_cloud_pin_moved_within_a_family_still_drifts(old, new):
    """A Bedrock, Vertex or ARN pin carries digits, so it is never read as
    an alias: Opus 4.1 to 4.5 is drift even though both are Opus."""
    assert _model_drift(old, new) == [("model", old, new)]
    assert _model_drift(old, old) == []


@pytest.mark.parametrize("observed", ["claude-opus-5-5", "claude-haiku-4-5-20251001", "<unknown>"])
@pytest.mark.parametrize("resolve", [True, False])
def test_detect_drift_default_never_drifts(observed, resolve):
    assert _model_drift("default", observed, resolve=resolve) == []


@pytest.mark.parametrize("observed", ["claude-opus-5", "claude-sonnet-5", "claude-opus-5-5"])
def test_detect_drift_opusplan_accepts_opus_or_sonnet(observed):
    assert _model_drift("opusplan", observed) == []


@pytest.mark.parametrize("observed", ["claude-haiku-4-5-20251001", "claude-fable-5-1"])
def test_detect_drift_opusplan_flags_other_families(observed):
    assert _model_drift("opusplan", observed) == [("model", "opusplan", observed)]


def test_detect_drift_best_reads_as_fable_through_the_rate_card():
    assert _model_drift("best", "claude-fable-5") == []
    assert _model_drift("best", "claude-opus-5-5") == [("model", "best", "claude-opus-5-5")]


def test_detect_drift_alias_shaped_value_of_unknown_family_compares_exactly():
    assert _model_drift("my-model", "claude-opus-5") == [("model", "my-model", "claude-opus-5")]
    assert _model_drift("my-model", "my-model") == []


def test_detect_drift_alias_family_works_without_a_resolver():
    assert _model_drift("sonnet", "claude-sonnet-5", resolve=False) == []
    assert _model_drift("sonnet", "claude-opus-5", resolve=False) != []


def test_build_config_drift_table_older_opus_under_opus_is_no_drift():
    snapshots = [_schema2_snapshot(ts="20260901T000000Z", effective={"model": "opus"})]
    sessions_with_observed = [
        {"session_id": "s1", "first_ts": "2026-09-05T00:00:00.000Z", "observed": {"model": "claude-opus-5"}},
    ]
    table = snap_mod.build_config_drift_table(
        sessions_with_observed, snapshots, resolve_model=load_pricing().resolve_model
    )
    assert table.rows == []
    assert any("No drift detected" in note for note in table.notes)


# -- schema 2: claude_json_cross_check ----------------------------------------


def test_claude_json_cross_check_matches_by_session_id_and_finds_differences():
    snap = _schema2_snapshot(
        claude_json={
            "matched": True,
            "last_session": {
                "lastSessionId": "abc-123",
                "lastTotalInputTokens": 1000,
                "lastTotalOutputTokens": 200,
                "lastCost": 1.5,
            },
        }
    )
    result = snap_mod.claude_json_cross_check(
        snap,
        {
            "session_id": "abc-123",
            "input_tokens": 1000,
            "output_tokens": 999,  # deliberately mismatched
            "cost": 1.5,
        },
    )
    assert result["matched"] is True
    assert result["differences"] == {"output_tokens": (200, 999)}


def test_claude_json_cross_check_session_id_mismatch_is_unmatched():
    snap = _schema2_snapshot(
        claude_json={"matched": True, "last_session": {"lastSessionId": "abc-123"}}
    )
    result = snap_mod.claude_json_cross_check(snap, {"session_id": "different-session"})
    assert result == {"matched": False, "differences": {}}


def test_claude_json_cross_check_no_last_session_is_unmatched():
    snap = _schema2_snapshot(claude_json={"matched": False})
    result = snap_mod.claude_json_cross_check(snap, {"session_id": "abc-123"})
    assert result == {"matched": False, "differences": {}}


def test_claude_json_cross_check_missing_field_on_either_side_is_skipped():
    snap = _schema2_snapshot(
        claude_json={
            "matched": True,
            "last_session": {"lastSessionId": "abc-123", "lastTotalInputTokens": 1000},
        }
    )
    # observed has no "input_tokens" key at all -> skipped, not reported.
    result = snap_mod.claude_json_cross_check(snap, {"session_id": "abc-123"})
    assert result == {"matched": True, "differences": {}}


# -- schema 2: privacy over the new tables -----------------------------------


def test_schema2_tables_are_privacy_clean():
    snap = _schema2_snapshot(
        project_slug="proj-a",
        effective={"model": "sonnet"},
        effective_provenance={"model": "user"},
        settings_layers={"user": {"present": True, "source_path_hash": "sha256:aaaa"}},
        content_layers={"agents_summary": {"count": 1}},
        mcp_servers={"names": ["filesystem"]},
    )
    assert_privacy(snap_mod.build_effective_config_table([snap]))
    assert_privacy(snap_mod.build_config_layers_table([snap]))
    assert_privacy(snap_mod.build_config_groups_table([snap]))
    assert_privacy(
        snap_mod.build_config_drift_table(
            [{"session_id": "s1", "first_ts": "20260901T000000Z", "observed": {"model": "opus"}}],
            [snap],
        )
    )


def test_build_config_section_include_effective_appends_schema2_tables():
    snapshots = _load()
    section = snap_mod.build_config_section(
        _sessions_with_metrics(),
        snapshots,
        "user_settings.autoCompactWindow",
        include_effective=True,
    )
    assert [table.name for table in section.tables] == [
        f"config-diff-user_settings.autoCompactWindow",
        "effective-config",
        "config-layers",
        "config-groups",
    ]


def test_build_config_section_sessions_with_observed_appends_drift_table():
    snapshots = _load()
    section = snap_mod.build_config_section(
        _sessions_with_metrics(),
        snapshots,
        "user_settings.autoCompactWindow",
        sessions_with_observed=[
            {"session_id": "s1", "first_ts": "2026-09-05T00:00:00.000Z", "observed": {"unrelated": "x"}}
        ],
    )
    assert section.tables[-1].name == "config-drift"


def test_build_config_section_without_new_kwargs_stays_one_table():
    # Backward compatibility: an existing caller passing only the three
    # positional arguments must still get exactly the diff table.
    snapshots = _load()
    section = snap_mod.build_config_section(
        _sessions_with_metrics(), snapshots, "user_settings.autoCompactWindow"
    )
    assert len(section.tables) == 1


def test_snapshot_for_with_project_key_ignores_other_projects():
    mine = snap_mod.Snapshot(path=None, ts="20260901T000000Z", data={"project_slug": "slug:aaa"})
    other = snap_mod.Snapshot(path=None, ts="20260910T000000Z", data={"project_slug": "slug:bbb"})
    legacy = snap_mod.Snapshot(path=None, ts="20260905T000000Z", data={})
    snaps = [mine, legacy, other]
    assert snap_mod.snapshot_for("2026-09-12T00:00:00Z", snaps, "slug:aaa") is legacy
    assert snap_mod.snapshot_for("2026-09-12T00:00:00Z", [mine, other], "slug:aaa") is mine
    assert snap_mod.snapshot_for("2026-09-12T00:00:00Z", snaps) is other


def _sha(slug: str) -> str:
    import hashlib

    return "slug:" + hashlib.sha256(slug.encode("utf-8")).hexdigest()[:12]


def test_snapshot_project_keys_name_both_drive_letter_spellings_canonical_first():
    upper, lower = _sha("C--X"), _sha("c--X")
    assert snap_mod.snapshot_project_keys("c--X") == (upper, lower)
    assert snap_mod.snapshot_project_keys("C--X") == (upper, lower)
    assert snap_mod.snapshot_project_key("c--X") == snap_mod.snapshot_project_key("C--X") == upper


def test_snapshot_project_keys_leave_a_slug_with_no_drive_letter_alone():
    keys = snap_mod.snapshot_project_keys("-home-u-x")
    assert keys == (_sha("-home-u-x"), _sha("-home-u-x"))
    assert snap_mod.snapshot_project_key("-home-u-x") == keys[0]
    # Only the first letter is folded, and only when it is a drive.
    assert snap_mod.snapshot_project_key("c-Users-x") == _sha("c-Users-x")
    assert snap_mod.snapshot_project_key("C--dev-X") == _sha("C--dev-X")
    assert snap_mod.snapshot_project_key("c--dev-X") == _sha("C--dev-X")


def test_snapshot_project_key_keeps_the_hashes_snapshots_were_already_stored_under():
    assert snap_mod.snapshot_project_key("C--Dev-claude-token-lens") == "slug:60178b259715"
    assert snap_mod.snapshot_project_key("C--Dev-RevIXO") == "slug:3cfd027c1c29"
    assert snap_mod.snapshot_project_keys("c--Dev-RevIXO")[1] == "slug:b7aa2921726a"


def test_snapshot_for_takes_every_key_a_project_goes_by():
    upper, lower = snap_mod.snapshot_project_keys("c--X")
    legacy = snap_mod.Snapshot(path=None, ts="20260901T000000Z", data={"project_slug": lower})
    other = snap_mod.Snapshot(path=None, ts="20260910T000000Z", data={"project_slug": "slug:bbb"})
    fresh = snap_mod.Snapshot(path=None, ts="20260905T000000Z", data={"project_slug": upper})
    snaps = [legacy, fresh, other]
    when = "2026-09-12T00:00:00Z"
    assert snap_mod.snapshot_for(when, snaps, (upper, lower)) is fresh
    assert snap_mod.snapshot_for(when, snaps, snap_mod.snapshot_project_keys("C--X")) is fresh
    assert snap_mod.snapshot_for("2026-09-02T00:00:00Z", snaps, (upper, lower)) is legacy
    assert snap_mod.snapshot_for(when, snaps, {lower}) is legacy
    assert snap_mod.snapshot_for(when, snaps, upper) is fresh
    assert snap_mod.snapshot_for(when, snaps, ()) is None
    assert snap_mod.snapshot_for(when, snaps) is other


def test_latest_for_keys_picks_the_newest_across_a_projects_keys():
    upper, lower = snap_mod.snapshot_project_keys("c--X")
    old = snap_mod.Snapshot(path=None, ts="20260901T000000Z", data={"project_slug": lower})
    new = snap_mod.Snapshot(path=None, ts="20260905T000000Z", data={"project_slug": upper})
    stale = snap_mod.Snapshot(path=None, ts="20260903T000000Z", data={"project_slug": lower})
    other = snap_mod.Snapshot(path=None, ts="20260910T000000Z", data={"project_slug": "slug:bbb"})
    legacy = snap_mod.Snapshot(path=None, ts="20260911T000000Z", data={})
    assert snap_mod.latest_for_keys([old, new, stale, other, legacy], (upper, lower)) is new
    # The order the snapshots come in doesn't matter, nor does a lone alias.
    assert snap_mod.latest_for_keys([other, new, legacy, stale, old], (upper, lower)) is new
    assert snap_mod.latest_for_keys([old, new, stale, other], (lower,)) is stale
    assert snap_mod.latest_for_keys([old, new, stale, other], ("slug:ccc",)) is None
    assert snap_mod.latest_for_keys([], (upper, lower)) is None


def test_load_snapshots_skips_apply_stamps_that_record_no_config(tmp_path):
    import json as _json

    from claudeglass.snapshots import diff_keys, load_snapshots

    snaps_dir = tmp_path / "snapshots"
    snaps_dir.mkdir()
    full = {"ts": "20260901T000000Z", "schema_version": 2, "user_settings": {"model": "opus"}, "effective": {"model": "opus"}}
    stamp = {"ts": "20260902T000000Z", "schema_version": 2, "profile_id": "lean"}
    (snaps_dir / "a.json").write_text(_json.dumps(full), encoding="utf-8")
    (snaps_dir / "b.json").write_text(_json.dumps(stamp), encoding="utf-8")
    (snaps_dir / "c.json").write_text(_json.dumps({**full, "ts": "20260903T000000Z"}), encoding="utf-8")
    loaded = load_snapshots(tmp_path)
    assert [s.ts for s in loaded] == ["20260901T000000Z", "20260903T000000Z"]
    # Without the stamp nothing looks changed.
    assert diff_keys(loaded) == {}


# -- profile marks / profile_for ----------------------------------------


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_profile_for_follows_apply_stamps_hook_captures_and_undos(tmp_path):
    snaps = tmp_path / "snapshots"
    # Hook capture: no profile yet.
    _write_json(snaps / "20260901T000000Z.json", {"ts": "20260901T000000Z", "profile_id": None, "user_settings": {}})
    # apply lean (stamp), then a one-off --set that must not change it.
    _write_json(snaps / "20260905T000000Z.json", {"ts": "20260905T000000Z", "schema_version": 2, "profile_id": "lean"})
    _write_json(snaps / "20260906T000000Z-2.json", {"ts": "20260906T000000Z-2", "schema_version": 2, "profile_id": "one-off"})
    # apply deep, whose marker backup held "lean"; undone on 09-12.
    _write_json(snaps / "20260910T000000Z.json", {"ts": "20260910T000000Z", "schema_version": 2, "profile_id": "deep"})
    backup = tmp_path / "backups" / "20260910T000000Z"
    _write_json(
        backup / "manifest.json",
        {"ts": "20260910T000000Z", "profile_id": "deep", "entries": [{"kind": "active_profile", "path": "x", "backup": "0000.bak"}]},
    )
    (backup / "files").mkdir()
    (backup / "files" / "0000.bak").write_text("lean\n", encoding="utf-8")
    _write_json(backup / "reverted.json", {"reverted_at": "2026-09-12T00:00:00Z"})

    marks = snap_mod.load_profile_marks(tmp_path)
    assert snap_mod.profile_for("2026-08-30T00:00:00.000Z", marks) is None
    assert snap_mod.profile_for("2026-09-02T00:00:00.000Z", marks) is None
    assert snap_mod.profile_for("2026-09-07T00:00:00.000Z", marks) == "lean"
    assert snap_mod.profile_for("2026-09-11T00:00:00.000Z", marks) == "deep"
    assert snap_mod.profile_for("2026-09-13T00:00:00.000Z", marks) == "lean"
    assert snap_mod.profile_for("not a time", marks) is None


def test_profile_for_prefers_the_sessions_own_hook_capture(tmp_path):
    snaps = tmp_path / "snapshots"
    _write_json(snaps / "20260905T000000Z.json", {"ts": "20260905T000000Z", "schema_version": 2, "profile_id": "lean"})
    # The session's own capture was written a moment after its first turn.
    _write_json(
        snaps / "20260906T000005Z.json",
        {"ts": "20260906T000005Z", "session_id": "s1", "profile_id": "deep", "user_settings": {}},
    )
    marks = snap_mod.load_profile_marks(tmp_path)
    assert snap_mod.profile_for("2026-09-06T00:00:00.000Z", marks) == "lean"
    assert snap_mod.profile_for("2026-09-06T00:00:00.000Z", marks, "s1") == "deep"


def test_profile_marks_ignore_captures_without_a_profile_field(tmp_path):
    _copy_fixtures_into_config_dir(tmp_path)
    marks = snap_mod.load_profile_marks(tmp_path)
    fixtures_with_field = [s for s in _load() if "profile_id" in s.data]
    assert len(marks) == len(fixtures_with_field)
    assert snap_mod.load_profile_marks(tmp_path / "missing") == []

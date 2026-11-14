"""Tests for the v0.3 Task 1 ``src/claudeglass/team.py`` module:
:func:`~claudeglass.team.machine_id`'s stability/non-reversibility,
:func:`~claudeglass.team.build_team_aggregate`'s structure and
privacy guarantees, :func:`~claudeglass.team.validate_team_document`'s
schema/length checks, the ``<config_dir>/team/`` save/load round trip
(latest-per-machine), and :func:`~claudeglass.team.build_team_report_section`'s
cross-machine comparison tables with the minimum-sample rule.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claudeglass import team
from claudeglass.config import Config
from claudeglass.corpus import load_corpus
from claudeglass.pricing import load_pricing

from helpers import assert_privacy_deep, turn_line, write_jsonl

PRICING = load_pricing()


def _write_top(project_dir: Path, session_id: str, n_turns: int = 2, **overrides) -> Path:
    path = project_dir / f"{session_id}.jsonl"
    write_jsonl(
        path,
        [turn_line(input_tokens=100 + i, output_tokens=20 + i, **overrides) for i in range(n_turns)],
    )
    return path


def _write_subagent(
    project_dir: Path, session_id: str, agent_id: str, n_turns: int = 1, meta: dict | None = None
) -> Path:
    agent_dir = project_dir / session_id / "subagents"
    agent_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = agent_dir / f"{agent_id}.jsonl"
    write_jsonl(jsonl_path, [turn_line(input_tokens=50 + i, output_tokens=10 + i) for i in range(n_turns)])
    meta_path = agent_dir / f"{agent_id}.meta.json"
    payload = {"agentType": "claude-implementer", "model": "claude-sonnet-5"}
    if meta:
        payload.update(meta)
    meta_path.write_text(json.dumps(payload), encoding="utf-8")
    return jsonl_path


def _two_session_corpus(tmp_path: Path):
    project_dir = tmp_path / "proj-two"
    project_dir.mkdir()
    _write_top(project_dir, "session-001", n_turns=2, cache_creation_input_tokens=1000, cache_read_input_tokens=100)
    _write_top(project_dir, "session-002", n_turns=3, cache_creation_input_tokens=500, cache_read_input_tokens=50)
    _write_subagent(project_dir, "session-002", "agent-aaa111", n_turns=2)
    return load_corpus([project_dir])


# -- machine_id ---------------------------------------------------------


def test_machine_id_is_stable_across_calls_for_the_same_config_dir(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    first = team.machine_id(config_dir)
    second = team.machine_id(config_dir)
    assert first == second
    assert len(first) == 12
    # hex only
    int(first, 16)


def test_machine_id_differs_across_config_dirs(tmp_path):
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    assert team.machine_id(dir_a) != team.machine_id(dir_b)


def test_machine_id_never_contains_the_hostname_in_the_clear(tmp_path):
    import platform

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    mid = team.machine_id(config_dir)
    hostname = platform.node()
    assert hostname == "" or hostname.lower() not in mid.lower()


# -- build_team_aggregate: structure + privacy ---------------------------


def test_build_team_aggregate_has_every_required_top_level_key(tmp_path):
    corpus = _two_session_corpus(tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    doc = team.build_team_aggregate(corpus, PRICING, Config(), config_dir, window="last 7 days")

    for key in ("tool_version", "generated_at", "machine_id", "window", "scorecard"):
        assert key in doc
    for axis in team.GROUP_AXES:
        assert f"by_{axis}" in doc
    assert "projects" not in doc  # include_projects defaults to False


def test_build_team_aggregate_never_leaks_a_session_id_or_slug(tmp_path):
    corpus = _two_session_corpus(tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    doc = team.build_team_aggregate(
        corpus, PRICING, Config(), config_dir, window="last 7 days", projects=("proj-two",)
    )
    assert_privacy_deep(doc)
    serialised = json.dumps(doc)
    assert "session-001" not in serialised
    assert "session-002" not in serialised
    assert "proj-two" not in serialised


def test_include_projects_adds_only_hashed_slugs(tmp_path):
    corpus = _two_session_corpus(tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    without = team.build_team_aggregate(
        corpus, PRICING, Config(), config_dir, window="last 7 days", projects=("proj-two",), include_projects=False
    )
    assert "projects" not in without

    with_projects = team.build_team_aggregate(
        corpus, PRICING, Config(), config_dir, window="last 7 days", projects=("proj-two",), include_projects=True
    )
    assert "projects" in with_projects
    assert with_projects["projects"], "expected at least one hashed slug"
    for value in with_projects["projects"]:
        assert value != "proj-two"
        assert "proj-two" not in value
        assert len(value) == 12
    assert_privacy_deep(with_projects)


def test_by_agent_type_axis_distinguishes_top_level_from_subagents(tmp_path):
    corpus = _two_session_corpus(tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    doc = team.build_team_aggregate(corpus, PRICING, Config(), config_dir, window="last 7 days")
    values = {row["value"] for row in doc["by_agent_type"]}
    assert "top-level" in values
    # "claude-implementer" is a project-defined custom agent, not one of
    # Claude Code's own bundled agent types -- review S10 requires it be
    # hashed rather than exported verbatim (see the S10 tests below).
    assert "claude-implementer" not in values
    assert any(value.startswith("custom:") for value in values)


# -- by_agent_type: custom agent names are hashed (review S10) -----------


def test_agent_type_group_label_keeps_builtin_types_verbatim():
    salt = b"x" * 32
    for builtin in ("top-level", "unknown", "general-purpose", "Explore", "Plan", "claude"):
        assert team._agent_type_group_label(builtin, salt) == builtin


def test_agent_type_group_label_hashes_custom_names():
    salt = b"x" * 32
    label = team._agent_type_group_label("claude-implementer", salt)
    assert label.startswith("custom:")
    assert "claude-implementer" not in label
    assert len(label) == len("custom:") + 8
    int(label.removeprefix("custom:"), 16)  # hex only


def test_agent_type_group_label_is_stable_for_the_same_salt():
    salt = b"y" * 32
    first = team._agent_type_group_label("revixo-reviewer", salt)
    second = team._agent_type_group_label("revixo-reviewer", salt)
    assert first == second


def test_agent_type_group_label_differs_across_salts():
    label_a = team._agent_type_group_label("revixo-reviewer", b"a" * 32)
    label_b = team._agent_type_group_label("revixo-reviewer", b"b" * 32)
    assert label_a != label_b


def test_agent_type_group_label_differs_from_project_slug_hash_for_the_same_name_and_salt():
    # Different domain tag from exports._hash_slug -- same input string
    # and salt must not collide with the project-slug namespace.
    salt = b"z" * 32
    agent_label = team._agent_type_group_label("shared-name", salt)
    from claudeglass.exports import _hash_slug

    slug_hash = _hash_slug("shared-name", salt)
    assert agent_label.removeprefix("custom:") != slug_hash[:8]


def test_build_team_aggregate_never_leaks_a_custom_agent_type_name(tmp_path):
    corpus = _two_session_corpus(tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    doc = team.build_team_aggregate(corpus, PRICING, Config(), config_dir, window="last 7 days")
    serialised = json.dumps(doc)
    assert "claude-implementer" not in serialised
    assert_privacy_deep(doc)


def test_group_row_sessions_never_exceeds_total_sessions_in_corpus(tmp_path):
    corpus = _two_session_corpus(tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    doc = team.build_team_aggregate(corpus, PRICING, Config(), config_dir, window="last 7 days")
    total_sessions = len(corpus.sessions)
    for axis in team.GROUP_AXES:
        for row in doc[f"by_{axis}"]:
            assert row["sessions"] <= total_sessions
            assert row["sessions"] >= 1


def test_scorecard_in_document_matches_scorecard_dimensions_metric(tmp_path):
    from claudeglass.report import build_report, scorecard_dimensions_metric

    corpus = _two_session_corpus(tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    doc = team.build_team_aggregate(corpus, PRICING, Config(), config_dir, window="last 7 days")

    model = build_report(corpus, PRICING, Config(), projects=("proj-two",), window="last 7 days")
    expected = scorecard_dimensions_metric(model.sections)
    assert doc["scorecard"] == expected


# -- validate_team_document -----------------------------------------------


def _valid_doc(**overrides) -> dict:
    doc = {
        "tool_version": "0.2.0",
        "generated_at": "2026-01-01T00:00:00.000Z",
        "machine_id": "abc123abc123",
        "window": "last 7 days",
        "by_archetype": [],
        "by_mode": [],
        "by_purpose": [],
        "by_agent_type": [],
        "by_model": [],
        "scorecard": {"cache_efficiency": 5},
    }
    doc.update(overrides)
    return doc


def test_validate_team_document_accepts_a_well_formed_document():
    assert team.validate_team_document(_valid_doc()) is None


def test_validate_team_document_rejects_non_dict():
    reason = team.validate_team_document(["not", "a", "dict"])
    assert reason is not None


def test_validate_team_document_rejects_disallowed_top_level_key():
    doc = _valid_doc()
    doc["session_id"] = "abc"
    reason = team.validate_team_document(doc)
    assert reason is not None
    assert "session_id" in reason


def test_validate_team_document_rejects_missing_required_key():
    doc = _valid_doc()
    del doc["machine_id"]
    reason = team.validate_team_document(doc)
    assert reason is not None
    assert "machine_id" in reason


def test_validate_team_document_rejects_disallowed_group_row_key():
    doc = _valid_doc(by_archetype=[{"value": "chat-only", "session_id": "leak"}])
    reason = team.validate_team_document(doc)
    assert reason is not None


def test_validate_team_document_rejects_string_over_64_chars():
    doc = _valid_doc(window="x" * 65)
    reason = team.validate_team_document(doc)
    assert reason is not None
    assert "64" in reason


def test_validate_team_document_accepts_string_at_exactly_64_chars():
    doc = _valid_doc(window="x" * 64)
    assert team.validate_team_document(doc) is None


def test_validate_team_document_rejects_bad_scorecard_key():
    doc = _valid_doc(scorecard={"not_a_real_dimension": 3})
    reason = team.validate_team_document(doc)
    assert reason is not None


def test_validate_team_document_rejects_projects_not_a_list():
    doc = _valid_doc(projects="not-a-list")
    reason = team.validate_team_document(doc)
    assert reason is not None


# -- review B1/N4: machine_id/generated_at path-traversal hardening --------


def test_validate_team_document_rejects_relative_traversal_machine_id():
    doc = _valid_doc(machine_id="../../evil/PWNED")
    reason = team.validate_team_document(doc)
    assert reason is not None
    assert "machine_id" in reason


def test_validate_team_document_rejects_absolute_path_machine_id():
    doc = _valid_doc(machine_id=r"C:\x\y")
    reason = team.validate_team_document(doc)
    assert reason is not None


def test_validate_team_document_rejects_short_machine_id():
    doc = _valid_doc(machine_id="abc123")
    reason = team.validate_team_document(doc)
    assert reason is not None


def test_validate_team_document_rejects_uppercase_machine_id():
    doc = _valid_doc(machine_id="ABC123ABC123")
    reason = team.validate_team_document(doc)
    assert reason is not None


def test_validate_team_document_rejects_a_machine_id_with_a_trailing_line_break():
    """``$`` also matches before a final line break, so "0123456789ab\n" passed a ``^...$`` pattern."""
    doc = _valid_doc(machine_id="0123456789ab\n")
    reason = team.validate_team_document(doc)
    assert reason is not None
    assert "machine_id" in reason
    assert team.validate_team_document(_valid_doc(machine_id="0123456789ab")) is None


def test_save_team_document_rejects_a_machine_id_with_a_trailing_line_break(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    with pytest.raises(ValueError):
        team.save_team_document(config_dir, _valid_doc(machine_id="0123456789ab\n"))


@pytest.mark.parametrize("bad_machine_id", [123456789012, None, 12.5, ["a"], {"a": 1}])
def test_validate_team_document_rejects_non_string_machine_id(bad_machine_id):
    doc = _valid_doc(machine_id=bad_machine_id)
    reason = team.validate_team_document(doc)
    assert reason is not None


@pytest.mark.parametrize("key", ["generated_at", "window", "tool_version"])
def test_validate_team_document_rejects_non_string_required_keys(key):
    doc = _valid_doc(**{key: 12345})
    reason = team.validate_team_document(doc)
    assert reason is not None
    assert key in reason


def test_validate_team_document_rejects_traversal_shaped_generated_at():
    doc = _valid_doc(generated_at="../../evil/2026-01-01T00:00:00.000Z")
    reason = team.validate_team_document(doc)
    assert reason is not None
    assert "generated_at" in reason


def test_validate_team_document_accepts_generated_at_without_fractional_seconds():
    # _resolve_generated_at's SOURCE_DATE_EPOCH path produces this shape
    # (no ".000" -- an integer Unix timestamp has no fractional part).
    doc = _valid_doc(generated_at="2026-01-01T00:00:00Z")
    assert team.validate_team_document(doc) is None


def test_save_team_document_rejects_traversal_machine_id_even_without_prior_validation(tmp_path):
    """Defence in depth (review B1): save_team_document re-checks
    machine_id/generated_at itself, in case a caller ever skips
    validate_team_document."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    doc = _valid_doc(machine_id="../../evil/PWNED")
    with pytest.raises(ValueError):
        team.save_team_document(config_dir, doc)
    # Nothing was written outside (or inside) the team directory.
    assert not (tmp_path / "evil").exists()
    assert not team.team_dir(config_dir).exists() or not any(team.team_dir(config_dir).iterdir())


def test_save_team_document_rejects_traversal_generated_at_even_without_prior_validation(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    doc = _valid_doc(generated_at="../../evil/2026-01-01T00:00:00.000Z")
    with pytest.raises(ValueError):
        team.save_team_document(config_dir, doc)
    assert not (tmp_path / "evil").exists()


# -- save/load round trip --------------------------------------------------


def test_save_team_document_writes_under_config_dir_team(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    doc = _valid_doc()
    path = team.save_team_document(config_dir, doc)
    assert path.parent == team.team_dir(config_dir)
    assert path.exists()
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk == doc


def test_load_latest_team_documents_keeps_only_the_newest_per_machine(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    older = _valid_doc(generated_at="2020-01-01T00:00:00.000Z")
    newer = _valid_doc(generated_at="2026-01-01T00:00:00.000Z")
    team.save_team_document(config_dir, older)
    team.save_team_document(config_dir, newer)

    loaded = team.load_latest_team_documents(config_dir)
    assert len(loaded) == 1
    assert loaded[0]["generated_at"] == "2026-01-01T00:00:00.000Z"


def test_load_latest_team_documents_keeps_one_per_distinct_machine(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    # 12 lowercase hex chars each -- machine_id's real shape (review B1
    # tightened save_team_document/validate_team_document to reject
    # anything else, since an untrusted machine_id reaches a filename).
    team.save_team_document(config_dir, _valid_doc(machine_id="aaaaaaaaaaaa"))
    team.save_team_document(config_dir, _valid_doc(machine_id="bbbbbbbbbbbb"))

    loaded = team.load_latest_team_documents(config_dir)
    assert {doc["machine_id"] for doc in loaded} == {"aaaaaaaaaaaa", "bbbbbbbbbbbb"}


def test_load_latest_team_documents_skips_invalid_or_unparsable_files(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    team.save_team_document(config_dir, _valid_doc())

    directory = team.team_dir(config_dir)
    (directory / "garbage.json").write_text("{not valid json", encoding="utf-8")
    bad_doc = _valid_doc(machine_id="badmachine01")
    bad_doc["extra_disallowed_key"] = "x"
    (directory / "bad-schema.json").write_text(json.dumps(bad_doc), encoding="utf-8")

    loaded = team.load_latest_team_documents(config_dir)
    assert len(loaded) == 1


def test_load_latest_team_documents_returns_empty_list_when_no_team_dir(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    assert team.load_latest_team_documents(config_dir) == []


# -- build_team_report_section ---------------------------------------------


def _doc_with_archetype_row(machine_id: str, value: str, sessions: int) -> dict:
    return _valid_doc(
        machine_id=machine_id,
        by_archetype=[
            {
                "value": value,
                "sessions": sessions,
                "priced_turns": sessions * 2,
                "tokens": {"input": 1, "cache_creation": 1, "cache_read": 1, "output": 1},
                "cost_usd": 1.0 * sessions,
                "recache_share_pct": 0.0,
                "compaction_rate": 0.0,
                "ttl_mix": {"5m_pct": None, "1h_pct": None},
                "mean_spawn_write": None,
                "mean_report_size": None,
            }
        ],
    )


def test_team_report_section_has_archetype_and_agent_type_tables():
    docs = [_doc_with_archetype_row("machine-one1", "chat-only", 10)]
    section = team.build_team_report_section(docs)
    assert section.key == "team_report"
    table_names = {table.name for table in section.tables}
    assert "team_by_archetype" in table_names
    assert "team_by_agent_type" in table_names
    assert any("observed" in note.lower() for note in section.notes)


def test_team_report_suppresses_cells_below_min_sessions():
    docs = [_doc_with_archetype_row("machine-one1", "chat-only", 2)]
    section = team.build_team_report_section(docs, min_sessions=5)
    archetype_table = next(t for t in section.tables if t.name == "team_by_archetype")
    row = next(r for r in archetype_table.rows if r[0] == "chat-only")
    assert row[1] == "n<5"


def test_team_report_shows_real_numbers_above_min_sessions():
    docs = [_doc_with_archetype_row("machine-one1", "chat-only", 10)]
    section = team.build_team_report_section(docs, min_sessions=5)
    archetype_table = next(t for t in section.tables if t.name == "team_by_archetype")
    row = next(r for r in archetype_table.rows if r[0] == "chat-only")
    assert row[1] != "n<5"
    assert "10 sessions" in row[1]


def test_team_report_columns_are_machine_ids_not_hostnames():
    import platform

    docs = [
        _doc_with_archetype_row("machine-one1", "chat-only", 10),
        _doc_with_archetype_row("machine-two2", "chat-only", 10),
    ]
    section = team.build_team_report_section(docs)
    archetype_table = next(t for t in section.tables if t.name == "team_by_archetype")
    labels = {col.label for col in archetype_table.columns}
    assert "machine-one1" in labels
    assert "machine-two2" in labels
    hostname = platform.node()
    assert hostname == "" or hostname not in labels


def test_team_report_section_privacy(tmp_path):
    corpus = _two_session_corpus(tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    doc = team.build_team_aggregate(corpus, PRICING, Config(), config_dir, window="last 7 days")
    section = team.build_team_report_section([doc])
    assert_privacy_deep(section)


# -- CLI `import` wiring (review B1/S2) -------------------------------------


def test_cli_import_rejects_traversal_machine_id_and_exits_2(tmp_path, capsys):
    from claudeglass import cli

    doc_path = tmp_path / "evil.json"
    doc_path.write_text(json.dumps(_valid_doc(machine_id="../../evil/PWNED")), encoding="utf-8")
    config_dir = tmp_path / "home" / "claudeglass"

    exit_code = cli.main(["import", str(doc_path), "--config-dir", str(config_dir)])

    assert exit_code == 2
    err = capsys.readouterr().err
    assert "machine_id" in err
    # Nothing was written anywhere under tmp_path, in or out of config_dir.
    assert not (tmp_path / "evil").exists()
    assert not config_dir.exists()


def test_cli_import_accepts_a_well_formed_document(tmp_path, capsys):
    from claudeglass import cli

    doc_path = tmp_path / "good.json"
    doc_path.write_text(json.dumps(_valid_doc()), encoding="utf-8")
    config_dir = tmp_path / "home" / "claudeglass"

    exit_code = cli.main(["import", str(doc_path), "--config-dir", str(config_dir)])

    assert exit_code == 0
    saved = list((config_dir / "team").glob("*.json"))
    assert len(saved) == 1
    assert json.loads(saved[0].read_text(encoding="utf-8")) == _valid_doc()


def test_cli_import_creates_a_missing_team_directory_rather_than_crashing(tmp_path):
    """S2 regression: import used to raise an unhandled FileNotFoundError
    (a Python traceback, exit 1) when <config_dir>/team didn't exist yet
    -- it must create the directory and succeed instead."""
    from claudeglass import cli

    doc_path = tmp_path / "good.json"
    doc_path.write_text(json.dumps(_valid_doc()), encoding="utf-8")
    config_dir = tmp_path / "brand-new-home" / "claudeglass"
    assert not config_dir.exists()

    exit_code = cli.main(["import", str(doc_path), "--config-dir", str(config_dir)])

    assert exit_code == 0
    assert (config_dir / "team").is_dir()

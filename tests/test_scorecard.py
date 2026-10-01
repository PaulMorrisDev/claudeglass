"""``scorecard.py``: the five-dimension optimisation scorecard, on
hand-built ``ScorecardInputs`` (this module never reads a transcript
itself — see its own module docstring). Pins the level boundaries per
the plan's test list ("scorecard boundaries") and covers the
agent-efficiency-skipped-for-non-spawning-corpora and
no-snapshot-scores-5 special cases.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claudeglass.model import Section
from claudeglass.scorecard import (
    ALL_DIMENSIONS,
    LEVEL_LABELS,
    ScorecardError,
    ScorecardInputs,
    ScorecardThresholds,
    build_section,
)
from claudeglass.snapshots import Snapshot, changed_keys

from helpers import assert_privacy


def _assert_row_keys_are_valid(section: Section) -> None:
    for table in section.tables:
        for row in table.rows:
            assert row
            key = row[0]
            assert isinstance(key, (str, int)) and not isinstance(key, bool)
            if isinstance(key, str):
                assert key != ""


# -- level boundaries (pinned) --------------------------------------------


def test_cache_efficiency_level_boundaries_are_pinned():
    th = ScorecardThresholds()
    cases = [
        (0.0, 5),
        (5.0, 5),
        (5.1, 4),
        (15.0, 4),
        (15.1, 3),
        (30.0, 3),
        (30.1, 2),
        (50.0, 2),
        (50.1, 1),
        (100.0, 1),
    ]
    for value, expected_level in cases:
        inputs = ScorecardInputs(recache_share_pct=value)
        section = build_section(inputs, th)
        dims = {row[0]: row for row in section.tables[0].rows}
        assert dims["cache_efficiency"][1] == expected_level, (value, expected_level)
        assert dims["cache_efficiency"][2] == LEVEL_LABELS[expected_level]


def test_context_hygiene_level_boundaries_are_pinned():
    th = ScorecardThresholds()
    cases = [
        (10_000, 5),
        (50_000, 5),
        (50_001, 4),
        (100_000, 4),
        (100_001, 3),
        (150_000, 3),
        (150_001, 2),
        (200_000, 2),
        (200_001, 1),
    ]
    for value, expected_level in cases:
        inputs = ScorecardInputs(p90_top_level_ctx=value)
        section = build_section(inputs, th)
        dims = {row[0]: row for row in section.tables[0].rows}
        assert dims["context_hygiene"][1] == expected_level, (value, expected_level)


def test_agent_efficiency_level_boundaries_are_pinned():
    th = ScorecardThresholds()
    cases = [(1.0, 5), (1.2, 5), (1.3, 4), (1.5, 4), (1.6, 3), (2.0, 3), (2.1, 2), (3.0, 2), (3.1, 1)]
    for value, expected_level in cases:
        inputs = ScorecardInputs(has_spawns=True, agent_cost_variance_ratio=value)
        section = build_section(inputs, th)
        dims = {row[0]: row for row in section.tables[0].rows}
        assert dims["agent_efficiency"][1] == expected_level, (value, expected_level)


def test_config_fit_level_boundaries_are_pinned():
    th = ScorecardThresholds()
    cases = [(0, 5), (3, 4), (6, 3), (10, 2), (11, 1)]
    for value, expected_level in cases:
        inputs = ScorecardInputs(has_snapshot=True, changed_config_keys=value)
        section = build_section(inputs, th)
        dims = {row[0]: row for row in section.tables[0].rows}
        assert dims["config_fit"][1] == expected_level, (value, expected_level)


def test_data_quality_level_boundaries_are_pinned():
    th = ScorecardThresholds()
    cases = [(100.0, 5), (99.5, 5), (99.4, 4), (97.0, 4), (96.9, 3), (90.0, 3), (89.9, 2), (75.0, 2), (74.9, 1)]
    for value, expected_level in cases:
        inputs = ScorecardInputs(pricing_coverage_pct=value)
        section = build_section(inputs, th)
        dims = {row[0]: row for row in section.tables[0].rows}
        assert dims["data_quality"][1] == expected_level, (value, expected_level)


# -- skip / special-case semantics ---------------------------------------


def test_agent_efficiency_is_omitted_when_corpus_never_spawned():
    inputs = ScorecardInputs(recache_share_pct=1.0, has_spawns=False)
    section = build_section(inputs)
    dims = {row[0] for row in section.tables[0].rows}
    assert "agent_efficiency" not in dims
    assert any("no subagents ran" in note for note in section.notes)


def test_config_fit_scores_five_when_no_snapshot_available():
    inputs = ScorecardInputs(has_snapshot=False)
    section = build_section(inputs)
    dims = {row[0]: row for row in section.tables[0].rows}
    assert dims["config_fit"][1] == 5
    dimensions_table = section.tables[0]
    assert any("No settings snapshot" in (note or "") for note in dimensions_table.notes)


def test_config_fit_no_snapshot_note_names_the_projects_shown():
    section = build_section(ScorecardInputs(has_snapshot=False))
    notes = section.tables[0].notes
    assert any("recorded for the projects shown" in (note or "") for note in notes)


def _user_snapshot(project_slug: str, ts: str, **user_settings) -> Snapshot:
    data = {"schema": 2, "ts": ts, "project_slug": project_slug, "user_settings": user_settings}
    return Snapshot(path=Path(f"{ts}.json"), ts=ts, data=data)


def test_config_fit_counts_the_changes_within_each_projects_own_snapshots():
    """Two projects with different settings, alternating: every neighbouring
    pair differs, but no project changed anything, so config stability is
    stable. A change inside one project counts, once."""
    mine = {f"setting{i}": f"a{i}" for i in range(12)}
    theirs = {f"setting{i}": f"b{i}" for i in range(12)}
    snaps = [
        _user_snapshot("proj-a", "20260901T000000Z", **mine),
        _user_snapshot("proj-b", "20260902T000000Z", **theirs),
        _user_snapshot("proj-a", "20260903T000000Z", **mine),
        _user_snapshot("proj-b", "20260904T000000Z", **theirs),
    ]

    def dims_for(snapshots):
        inputs = ScorecardInputs(has_snapshot=True, changed_config_keys=len(changed_keys(snapshots)))
        return {row[0]: row for row in build_section(inputs).tables[0].rows}

    assert dims_for(snaps)["config_fit"][1] == 5
    snaps.append(_user_snapshot("proj-a", "20260905T000000Z", **dict(mine, setting0="changed")))
    assert dims_for(snaps)["config_fit"][1] == 4
    assert dims_for(snaps)["config_fit"][4] == 1


def test_dimensions_with_none_metric_are_skipped_entirely():
    inputs = ScorecardInputs()  # every optional metric left at None
    section = build_section(inputs)
    dims = {row[0] for row in section.tables[0].rows}
    assert "cache_efficiency" not in dims
    assert "context_hygiene" not in dims
    assert "agent_efficiency" not in dims
    # config_fit and data_quality always compute (no None-skip semantics).
    assert "config_fit" in dims
    assert "data_quality" in dims


def test_overall_is_the_minimum_of_the_four_non_data_dimensions_never_an_average():
    inputs = ScorecardInputs(
        recache_share_pct=1.0,  # -> 5
        p90_top_level_ctx=10_000,  # -> 5
        has_spawns=True,
        agent_cost_variance_ratio=5.0,  # -> 1
        has_snapshot=True,
        changed_config_keys=0,  # -> 5
        pricing_coverage_pct=1.0,  # -> 1 (data_quality, excluded from overall)
    )
    section = build_section(inputs)
    overall_row = section.tables[1].rows[0]
    assert overall_row == ["overall", 1, "very poor"]


def test_overall_falls_back_to_config_fit_alone_when_nothing_else_has_data():
    # cache_efficiency/context_hygiene/agent_efficiency all skip (None
    # metrics, no spawns); config_fit always computes (see its own
    # module docstring: "no observed instability" scores 5 rather than
    # being treated as missing), so overall can never actually be
    # "unmeasured" in practice -- it reflects config_fit alone here.
    inputs = ScorecardInputs(has_spawns=False)
    section = build_section(inputs)
    overall_row = section.tables[1].rows[0]
    assert overall_row == ["overall", 5, "excellent"]


def test_all_dimensions_constant_matches_the_five_named_dimensions():
    assert set(ALL_DIMENSIONS) == {
        "cache_efficiency",
        "context_hygiene",
        "agent_efficiency",
        "config_fit",
        "data_quality",
    }


def test_thresholds_from_config_reads_flat_dict_and_ignores_unknown_keys():
    th = ScorecardThresholds.from_config(
        {
            "cache_recache_share_pct": [1.0, 2.0, 3.0, 4.0],
            "unknown_key": [9, 9, 9, 9],
        }
    )
    assert th.cache_recache_share_pct == (1.0, 2.0, 3.0, 4.0)
    assert th.context_p90_ctx == ScorecardThresholds().context_p90_ctx  # default kept


def test_thresholds_from_config_keeps_defaults_for_malformed_input():
    assert ScorecardThresholds.from_config(None) == ScorecardThresholds()
    assert ScorecardThresholds.from_config("not a dict") == ScorecardThresholds()
    assert ScorecardThresholds.from_config({"cache_recache_share_pct": [1, 2]}) == ScorecardThresholds()


# -- Fix R20: 4-tuple ordering validation -----------------------------------


def test_thresholds_from_config_rejects_misordered_lower_is_better_tuple():
    with pytest.raises(ScorecardError) as exc_info:
        ScorecardThresholds.from_config({"cache_recache_share_pct": [50.0, 30.0, 15.0, 5.0]})
    assert "cache_recache_share_pct" in str(exc_info.value)
    assert "ascending" in str(exc_info.value)


def test_thresholds_from_config_rejects_misordered_higher_is_better_tuple():
    with pytest.raises(ScorecardError) as exc_info:
        ScorecardThresholds.from_config({"data_pricing_coverage_pct": [75.0, 90.0, 97.0, 99.5]})
    assert "data_pricing_coverage_pct" in str(exc_info.value)
    assert "descending" in str(exc_info.value)


def test_thresholds_from_config_accepts_ties_in_ordering():
    # <= / >= in _level_lower_is_better/_level_higher_is_better tolerate
    # equal neighbouring bounds -- so should the validation.
    th = ScorecardThresholds.from_config({"cache_recache_share_pct": [5.0, 5.0, 30.0, 50.0]})
    assert th.cache_recache_share_pct == (5.0, 5.0, 30.0, 50.0)


def test_thresholds_from_config_accepts_all_default_tuples_unchanged():
    # The class's own defaults must obviously pass their own validation.
    defaults = ScorecardThresholds()
    th = ScorecardThresholds.from_config(
        {
            "cache_recache_share_pct": list(defaults.cache_recache_share_pct),
            "context_p90_ctx": list(defaults.context_p90_ctx),
            "agent_cost_variance_ratio": list(defaults.agent_cost_variance_ratio),
            "config_changed_keys": list(defaults.config_changed_keys),
            "data_pricing_coverage_pct": list(defaults.data_pricing_coverage_pct),
        }
    )
    assert th == defaults


# -- v3-limits: limit-attributed re-cache / pause-session notes -----------


def test_cache_efficiency_excludes_limit_attributed_recache_share_from_level():
    # 40.0pp raw recache share alone would score level 2 (30.1-50.0 band),
    # but 35.0pp of it is attributed to usage-limit pauses, leaving 5.0pp
    # -- level 5.
    inputs = ScorecardInputs(recache_share_pct=40.0, limit_recache_share_pct=35.0)
    section = build_section(inputs)
    dims = {row[0]: row for row in section.tables[0].rows}
    assert dims["cache_efficiency"][1] == 5
    assert dims["cache_efficiency"][4] == pytest.approx(5.0)
    dimensions_table = section.tables[0]
    assert any("usage-limit pause" in (note or "") for note in dimensions_table.notes)


def test_cache_efficiency_clamps_at_zero_when_limit_share_exceeds_raw_share():
    inputs = ScorecardInputs(recache_share_pct=5.0, limit_recache_share_pct=9.0)
    section = build_section(inputs)
    dims = {row[0]: row for row in section.tables[0].rows}
    assert dims["cache_efficiency"][4] == pytest.approx(0.0)
    assert dims["cache_efficiency"][1] == 5


def test_cache_efficiency_unaffected_when_limit_recache_share_is_none():
    inputs = ScorecardInputs(recache_share_pct=40.0)
    section = build_section(inputs)
    dims = {row[0]: row for row in section.tables[0].rows}
    assert dims["cache_efficiency"][4] == pytest.approx(40.0)
    assert dims["cache_efficiency"][1] == 2
    dimensions_table = section.tables[0]
    assert not any("usage-limit pause" in (note or "") for note in dimensions_table.notes)


def test_cache_efficiency_unaffected_when_limit_recache_share_is_zero():
    inputs = ScorecardInputs(recache_share_pct=40.0, limit_recache_share_pct=0.0)
    section = build_section(inputs)
    dims = {row[0]: row for row in section.tables[0].rows}
    assert dims["cache_efficiency"][4] == pytest.approx(40.0)
    assert dims["cache_efficiency"][1] == 2


def test_data_quality_notes_limit_pause_session_count_when_nonzero():
    inputs = ScorecardInputs(pricing_coverage_pct=99.9, limit_pause_sessions=3)
    section = build_section(inputs)
    dimensions_table = section.tables[0]
    assert any(
        "3 sessions hit a usage limit and paused" in (note or "")
        for note in dimensions_table.notes
    )
    # A data-quality note never changes the level itself.
    dims = {row[0]: row for row in dimensions_table.rows}
    assert dims["data_quality"][1] == 5


def test_data_quality_note_uses_singular_session_for_count_of_one():
    inputs = ScorecardInputs(limit_pause_sessions=1)
    section = build_section(inputs)
    dimensions_table = section.tables[0]
    assert any(
        "1 session hit a usage limit and paused" in (note or "")
        for note in dimensions_table.notes
    )


def test_data_quality_has_no_limit_pause_note_when_zero():
    inputs = ScorecardInputs()
    section = build_section(inputs)
    dimensions_table = section.tables[0]
    assert not any("usage-limit pause" in (note or "") for note in dimensions_table.notes)


def test_data_quality_notes_closest_match_turn_count_when_nonzero():
    # Fix 2: a closest-match turn already counts as priced in
    # pricing_coverage_pct, so the level is untouched -- the note just
    # says it was only an estimate.
    inputs = ScorecardInputs(pricing_coverage_pct=100.0, closest_match_turns=4)
    section = build_section(inputs)
    dimensions_table = section.tables[0]
    assert any(
        "4 turns counted as priced above were only priced by closest match" in (note or "")
        for note in dimensions_table.notes
    )
    dims = {row[0]: row for row in dimensions_table.rows}
    assert dims["data_quality"][1] == 5


def test_data_quality_note_uses_singular_turn_for_count_of_one():
    inputs = ScorecardInputs(closest_match_turns=1)
    section = build_section(inputs)
    dimensions_table = section.tables[0]
    assert any(
        "1 turn counted as priced above was only priced by closest match" in (note or "")
        for note in dimensions_table.notes
    )


def test_data_quality_has_no_closest_match_note_when_zero():
    inputs = ScorecardInputs()
    section = build_section(inputs)
    dimensions_table = section.tables[0]
    assert not any("closest match" in (note or "") for note in dimensions_table.notes)


def test_data_quality_combines_limit_pause_and_closest_match_notes():
    inputs = ScorecardInputs(limit_pause_sessions=2, closest_match_turns=3)
    section = build_section(inputs)
    dimensions_table = section.tables[0]
    combined = " ".join(note or "" for note in dimensions_table.notes)
    assert "2 sessions hit a usage limit and paused" in combined
    assert "3 turns counted as priced above were only priced by closest match" in combined


def test_scorecard_with_limit_fields_passes_privacy():
    inputs = ScorecardInputs(
        recache_share_pct=40.0,
        limit_recache_share_pct=35.0,
        pricing_coverage_pct=98.0,
        limit_pause_sessions=2,
    )
    section = build_section(inputs)
    assert_privacy(section)


# -- contract / privacy ---------------------------------------------------


def test_scorecard_build_section_row_keys_are_all_str_or_int():
    inputs = ScorecardInputs(
        recache_share_pct=10.0,
        p90_top_level_ctx=60_000,
        has_spawns=True,
        agent_cost_variance_ratio=1.4,
        has_snapshot=True,
        changed_config_keys=2,
        pricing_coverage_pct=98.0,
    )
    section = build_section(inputs)
    assert section.key == "scorecard"
    _assert_row_keys_are_valid(section)
    assert_privacy(section)

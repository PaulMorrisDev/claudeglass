"""``report.py``: ``build_report``'s corpus-wide integration of every
WP3-WP9 accumulator into one :class:`~claudeglass.model.ReportModel`.
Synthetic corpora are built with ``tests/helpers``/``corpus.load_corpus``
(the same JSONL-fixture pattern ``test_corpus.py`` uses), per the plan's
test list: an empty corpus, a two-session corpus with one subagent,
group-sum invariants, a smoke render through all four renderers, and a
real-fixture check against ``tests/fixtures/real/session-a`` (skipped
when absent).
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from claudeglass.config import Config
from claudeglass.corpus import load_corpus
from claudeglass.model import Diagnostics
from claudeglass.pricing import load_pricing
from claudeglass.report import _SECTION_ORDER, _apply_autocompact_pct_override, build_report
from claudeglass.render.csv_out import write_csv_dir
from claudeglass.render.html import render_html
from claudeglass.render.json_out import render_json
from claudeglass.render.markdown import render_markdown
from claudeglass.snapshots import Snapshot, snapshot_project_key, snapshot_project_keys

from helpers import assert_privacy, turn_line, user_str_line, write_jsonl

PRICING = load_pricing()


def _write_top(project_dir: Path, session_id: str, n_turns: int = 2, **overrides) -> Path:
    """Write ``n_turns`` assistant lines, applying ``overrides`` (e.g.
    cache_creation_input_tokens/cache_read_input_tokens) to every turn
    uniformly -- simpler than varying it per-turn, and sufficient for
    this file's assertions, which only check totals/sums, not per-turn
    cache shape."""
    path = project_dir / f"{session_id}.jsonl"
    write_jsonl(
        path,
        [turn_line(input_tokens=100 + i, output_tokens=20 + i, **overrides) for i in range(n_turns)],
    )
    return path


def _write_subagent(project_dir: Path, session_id: str, agent_id: str, n_turns: int = 1, meta: dict | None = None) -> Path:
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


def _all_sections_row_keys_are_valid(sections) -> None:
    for section in sections:
        for table in section.tables:
            for row in table.rows:
                assert row, f"{section.key}/{table.name}: empty row"
                key = row[0]
                assert isinstance(key, (str, int)) and not isinstance(key, bool), (
                    f"{section.key}/{table.name}: row[0]={key!r} is not a str/int key"
                )
                if isinstance(key, str):
                    assert key != "", f"{section.key}/{table.name}: row[0] is empty string"


# -- empty corpus ----------------------------------------------------------


def test_empty_corpus_renders_without_exceptions(tmp_path):
    project_dir = tmp_path / "proj-empty"
    project_dir.mkdir()
    corpus = load_corpus([project_dir])
    report = build_report(corpus, PRICING, Config(), projects=("proj-empty",), window="last 7 days")

    assert [s.key for s in report.sections] == [k for k in _SECTION_ORDER if k not in ("phases", "config", "elasticity")]
    overview = next(s for s in report.sections if s.key == "overview")
    totals = {row[0]: row[1] for row in overview.tables[0].rows}
    assert totals["sessions"] == 0
    assert totals["total_cost_usd"] == 0.0
    assert overview.notes  # "no sessions" note present

    for section in report.sections:
        assert_privacy(section)
    _all_sections_row_keys_are_valid(report.sections)

    # Every renderer must still succeed on an empty report.
    assert render_markdown(report)
    assert render_json(report)
    assert render_html(report)


# -- two sessions, one subagent ---------------------------------------------


def _two_session_corpus(tmp_path: Path):
    project_dir = tmp_path / "proj-two"
    project_dir.mkdir()
    _write_top(project_dir, "session-001", n_turns=2, cache_creation_input_tokens=1000, cache_read_input_tokens=100)
    _write_top(project_dir, "session-002", n_turns=3, cache_creation_input_tokens=500, cache_read_input_tokens=50)
    _write_subagent(project_dir, "session-002", "agent-aaa111", n_turns=2)
    return load_corpus([project_dir])


def test_two_session_corpus_with_one_subagent_builds_every_section(tmp_path):
    corpus = _two_session_corpus(tmp_path)
    report = build_report(corpus, PRICING, Config(), projects=("proj-two",), window="last 7 days")

    overview = next(s for s in report.sections if s.key == "overview")
    totals = {row[0]: row[1] for row in overview.tables[0].rows}
    assert totals["sessions"] == 2
    assert totals["top_level_transcripts"] == 2
    assert totals["subagent_transcripts"] == 1
    # 2 + 3 top-level turns + 2 subagent turns = 7 priced turns.
    assert totals["priced_turns"] == 7
    assert totals["total_cost_usd"] > 0.0

    _all_sections_row_keys_are_valid(report.sections)
    for section in report.sections:
        assert_privacy(section)


def test_subagents_are_never_double_counted(tmp_path):
    corpus = _two_session_corpus(tmp_path)
    report = build_report(corpus, PRICING, Config(), projects=("proj-two",), window="last 7 days")
    overview = next(s for s in report.sections if s.key == "overview")
    totals = {row[0]: row[1] for row in overview.tables[0].rows}
    # 5 top-level + 2 subagent = 7, not e.g. double-counted to 9 or 14.
    assert totals["priced_turns"] == 7


def test_capture_section_gets_a_held_back_row_from_the_recommend_diff(tmp_path):
    """EST-P7 + CAP-3 wiring: build_report runs recommend() a second time
    without the habits section and folds the diff into the capture
    section afterwards. This corpus is too small for recommend() to
    surface anything (its own min-sample gate), so the diff is empty --
    this only proves the wiring runs end to end and leaves a well-formed
    row, not the arithmetic (that's habits.py's own unit tests)."""
    corpus = _two_session_corpus(tmp_path)
    report = build_report(corpus, PRICING, Config(), projects=("proj-two",), window="last 7 days")
    capture = next(s for s in report.sections if s.key == "capture")
    rows = {row[0]: row[1] for row in capture.tables[0].rows}
    assert rows["held_back"] == 0
    assert rows["habit_value"] is None


# -- group-sum invariant -----------------------------------------------------


def test_by_model_group_sums_equal_overview_totals(tmp_path):
    corpus = _two_session_corpus(tmp_path)
    report = build_report(corpus, PRICING, Config(), projects=("proj-two",), window="last 7 days")
    overview = next(s for s in report.sections if s.key == "overview")
    totals = {row[0]: row[1] for row in overview.tables[0].rows}
    by_model = overview.tables[1]

    assert sum(row[1] for row in by_model.rows) == totals["priced_turns"]
    assert sum(row[2] for row in by_model.rows) == totals["input_tokens"]
    assert sum(row[3] for row in by_model.rows) == totals["cache_creation_tokens"]
    assert sum(row[4] for row in by_model.rows) == totals["cache_read_tokens"]
    assert sum(row[5] for row in by_model.rows) == totals["output_tokens"]
    assert sum(row[6] for row in by_model.rows) == pytest.approx(totals["total_cost_usd"])


def _write_session_with_ctx_values(
    project_dir: Path, session_id: str, top_ctx_values: list[int], sub_ctx_values: list[int]
) -> None:
    """Write one session whose top-level turns' ``ctx`` (== ``input_tokens``
    here -- no cache tokens involved) are exactly ``top_ctx_values`` and
    whose single subagent's turns' ``ctx`` are exactly ``sub_ctx_values``,
    so a test can assert precisely which set a stat was computed from.
    """
    write_jsonl(
        project_dir / f"{session_id}.jsonl",
        [turn_line(input_tokens=v, output_tokens=20) for v in top_ctx_values],
    )
    if sub_ctx_values:
        agent_dir = project_dir / session_id / "subagents"
        agent_dir.mkdir(parents=True, exist_ok=True)
        write_jsonl(
            agent_dir / "agent-ctx.jsonl",
            [turn_line(input_tokens=v, output_tokens=20) for v in sub_ctx_values],
        )
        (agent_dir / "agent-ctx.meta.json").write_text(
            json.dumps({"agentType": "claude-implementer", "model": "claude-sonnet-5"}), encoding="utf-8"
        )


def test_scorecard_ctx_stats_use_top_level_transcripts_only(tmp_path, monkeypatch):
    """Regression test for review finding R7: the scorecard's
    context-hygiene ctx values were built from every transcript's turns,
    not top-level only, so a subagent with a much bigger ctx (subagents
    typically start from a large system-prompt/task payload) skewed both
    the median and the p90 upward.
    """
    project_dir = tmp_path / "proj-ctx"
    project_dir.mkdir()
    _write_session_with_ctx_values(project_dir, "session-ctx", top_ctx_values=[100, 200], sub_ctx_values=[500_000])

    corpus = load_corpus([project_dir])

    from claudeglass import report as report_mod

    captured = {}
    original_build_section = report_mod.scorecard.build_section

    def _capture(inputs, th):
        captured["inputs"] = inputs
        return original_build_section(inputs, th)

    monkeypatch.setattr(report_mod.scorecard, "build_section", _capture)

    build_report(corpus, PRICING, Config(), projects=("proj-ctx",), window="w")

    inputs = captured["inputs"]
    # Top-level turns only: ctx values [100, 200]. If the subagent's
    # 500_000-token turn leaked in, both stats would be orders of
    # magnitude bigger.
    assert inputs.median_top_level_ctx == pytest.approx(150.0)
    assert inputs.p90_top_level_ctx == pytest.approx(200.0)


def test_scorecard_context_hygiene_threshold_scales_for_1m_window_model(tmp_path):
    """D2/COV-12: ``ScorecardThresholds.context_p90_ctx``'s defaults
    (50k/100k/150k/200k) were sized for the 200k-window assumption. A
    corpus run entirely on ``claude-sonnet-5`` (natively 1M, V24) with a
    p90 top-level ctx of 300_000 scored "very poor" (level 1) against
    the flat default even though 300k tokens is a small fraction of that
    model's actual window; report assembly now scales the thresholds by
    the corpus's own resolved model window (5x here), landing 300_000 at
    level 4 instead.
    """
    project_dir = tmp_path / "proj-1m"
    project_dir.mkdir()
    # turn_line's default model is claude-sonnet-5 (1M context in the
    # packaged pricing.toml), so no override is needed here.
    _write_session_with_ctx_values(
        project_dir, "session-1m", top_ctx_values=[50_000, 100_000, 250_000, 300_000], sub_ctx_values=[]
    )

    corpus = load_corpus([project_dir])
    report = build_report(corpus, PRICING, Config(), projects=("proj-1m",), window="w")
    scorecard_section = next(s for s in report.sections if s.key == "scorecard")
    dim_table = next(t for t in scorecard_section.tables if t.name == "dimensions")
    row = next(r for r in dim_table.rows if r[0] == "context_hygiene")

    assert row[4] == pytest.approx(300_000.0)  # value: p90_top_level_ctx
    assert row[1] == 4  # level: level 1 under the unscaled 200k-tuned default


def test_overview_long_context_share_is_top_level_turn_count_basis(tmp_path):
    """Coordinator follow-up to R7: the overview's "long-context share of
    recent top-level turns" verification anchor needs a turn-count-basis,
    top-level-only stat to check against. Top-level ctx values
    [50_000, 100_000, 250_000, 300_000] -> median 175_000.0, and 2 of 4
    (50%) are >= huge_ctx (200_000). A subagent turn with an even bigger
    ctx must not shift either figure.
    """
    project_dir = tmp_path / "proj-ctx2"
    project_dir.mkdir()
    _write_session_with_ctx_values(
        project_dir,
        "session-ctx2",
        top_ctx_values=[50_000, 100_000, 250_000, 300_000],
        sub_ctx_values=[900_000],
    )

    corpus = load_corpus([project_dir])
    report = build_report(corpus, PRICING, Config(), projects=("proj-ctx2",), window="w")
    overview = next(s for s in report.sections if s.key == "overview")
    totals = {row[0]: row[1] for row in overview.tables[0].rows}

    assert totals["top_level_median_ctx"] == pytest.approx(175_000.0)
    assert totals["top_level_turns_ctx_ge_200k_pct"] == pytest.approx(50.0)


def test_recache_by_group_table_rows_sum_to_the_ungrouped_summary(tmp_path):
    corpus = _two_session_corpus(tmp_path)
    report = build_report(
        corpus, PRICING, Config(), projects=("proj-two",), window="last 7 days", group_by="mode"
    )
    recache_section = next(s for s in report.sections if s.key == "recache")
    summary_row = recache_section.tables[0].rows[0]  # recache_summary: metric, transcripts, priced_turns, ...
    group_table = next(t for t in recache_section.tables if t.name == "recache_by_group")
    assert group_table.rows
    # group column prepended, so index 1 onward mirrors recache_summary's columns (metric first).
    assert sum(row[2] for row in group_table.rows) == summary_row[1]  # transcripts
    assert sum(row[3] for row in group_table.rows) == summary_row[2]  # priced_turns


# -- v3-limits wiring --------------------------------------------------------


def _corpus_with_one_limit_hit(tmp_path: Path):
    # Mirrors tests/test_limits.py's own _session_limit_fixture: a
    # synthetic session-limit LIMIT_HIT, a human resume message, then a
    # post-pause turn that necessarily did a full-expiry re-cache.
    project_dir = tmp_path / "proj-limit"
    project_dir.mkdir()
    lines = [
        turn_line(message_id="msg_1", input_tokens=30_000, timestamp="2026-09-18T12:00:00.000Z"),
        turn_line(
            message_id="msg_synth",
            model="<synthetic>",
            isApiErrorMessage=True,
            input_tokens=0,
            output_tokens=0,
            content=[{"type": "text", "text": "You've hit your session limit · resets 3pm (Europe/London)"}],
            timestamp="2026-09-18T12:00:05.000Z",
        ),
        user_str_line(
            "I hit my usage limit while you were working, but it has reset now.",
            promptSource="sdk",
            origin={"kind": "human"},
            timestamp="2026-09-18T15:00:00.000Z",
        ),
        turn_line(
            message_id="msg_2",
            input_tokens=30_000,
            cache_creation_input_tokens=25_000,
            cache_read_input_tokens=0,
            timestamp="2026-09-18T15:00:10.000Z",
        ),
    ]
    write_jsonl(project_dir / "session-limit.jsonl", lines)
    return load_corpus([project_dir])


def test_limits_section_and_scorecard_receive_the_limit_hit(tmp_path):
    corpus = _corpus_with_one_limit_hit(tmp_path)
    report = build_report(corpus, PRICING, Config(), projects=("proj-limit",), window="w")

    limits_section = next(s for s in report.sections if s.key == "limits")
    summary_row = {c.key: v for c, v in zip(limits_section.tables[0].columns, limits_section.tables[0].rows[0])}
    assert summary_row["limit_hits"] == 1
    assert summary_row["session_limit_hits"] == 1
    assert summary_row["sessions_affected"] == 1
    assert summary_row["pause_count"] == 1
    assert summary_row["limit_turn_cc_tokens"] == 25_000

    assert any(a.startswith("a usage-cap pause's") for a in report.meta.assumptions)

    scorecard_section = next(s for s in report.sections if s.key == "scorecard")
    dimensions_table = next(t for t in scorecard_section.tables if t.name == "dimensions")
    # data_quality's note fires whenever ScorecardInputs.limit_pause_sessions
    # > 0 (scorecard.py's _data_quality) -- proves report.py actually
    # threaded LimitStats.sessions_affected through, not just built the
    # section.
    assert any("usage-limit pause" in note for note in dimensions_table.notes if note)

    for section in report.sections:
        assert_privacy(section)
    _all_sections_row_keys_are_valid(report.sections)


def _limit_events_session(project_dir: Path, session_id: str, terminated: int) -> None:
    """One session-limit hit, one automatic resume, then ``terminated``
    agent-terminated-early notifications."""
    lines = [
        turn_line(message_id=f"{session_id}_1", timestamp="2026-09-18T12:00:00.000Z"),
        turn_line(
            message_id=f"{session_id}_synth",
            model="<synthetic>",
            isApiErrorMessage=True,
            input_tokens=0,
            output_tokens=0,
            content=[{"type": "text", "text": "You've hit your session limit · resets 3pm (Europe/London)"}],
            timestamp="2026-09-18T12:00:05.000Z",
        ),
        user_str_line(
            "I hit my usage limit while you were working, but it has reset now.",
            promptSource="sdk",
            origin={"kind": "human"},
            timestamp="2026-09-18T15:00:00.000Z",
        ),
        *(
            user_str_line(
                "Agent terminated early due to a network error.",
                origin={"kind": "task-notification"},
                timestamp=f"2026-09-18T15:00:0{i + 1}.000Z",
            )
            for i in range(terminated)
        ),
        turn_line(message_id=f"{session_id}_2", timestamp="2026-09-18T15:00:10.000Z"),
    ]
    write_jsonl(project_dir / f"{session_id}.jsonl", lines)


def test_diagnostics_limit_counters_sum_across_transcripts(tmp_path):
    project_dir = tmp_path / "proj-limits"
    project_dir.mkdir()
    _limit_events_session(project_dir, "session-a", terminated=1)
    _limit_events_session(project_dir, "session-b", terminated=2)
    corpus = load_corpus([project_dir])
    report = build_report(corpus, PRICING, Config(), projects=("proj-limits",), window="w")

    # Each transcript contributes its own nonzero counts (not just the
    # last one's, and not the untouched default of 0).
    per_transcript = sorted(
        (b.top.diagnostics.limit_hits, b.top.diagnostics.limit_resumes, b.top.diagnostics.agents_terminated)
        for b in corpus.sessions
    )
    assert per_transcript == [(1, 1, 1), (1, 1, 2)]
    assert report.diagnostics.limit_hits == 2
    assert report.diagnostics.limit_resumes == 2
    assert report.diagnostics.agents_terminated == 3

    # Every per-transcript int counter must be summed into the report-wide
    # total, except the two pricing fields set once after the loop.
    post_hoc = {"pricing_closest_match_turns", "pricing_fast_priced_as_standard_turns"}
    transcripts = [tr for b in corpus.sessions for tr in (b.top, *b.subs)]
    for f in dataclasses.fields(Diagnostics):
        value = getattr(report.diagnostics, f.name)
        if f.name in post_hoc or not isinstance(value, int) or isinstance(value, bool):
            continue
        assert value == sum(getattr(tr.diagnostics, f.name) for tr in transcripts), f.name


# -- subscription vs api labelling (delegated to usage.py, exercised here) --


def test_usage_section_cost_label_reflects_billing_mode(tmp_path):
    corpus = _two_session_corpus(tmp_path)
    api_report = build_report(corpus, PRICING, Config(billing="api"), projects=("proj-two",), window="w")
    sub_report = build_report(corpus, PRICING, Config(billing="subscription"), projects=("proj-two",), window="w")

    api_usage = next(s for s in api_report.sections if s.key == "usage")
    sub_usage = next(s for s in sub_report.sections if s.key == "usage")

    api_project_table = next(t for t in api_usage.tables if t.name == "by_project")
    sub_project_table = next(t for t in sub_usage.tables if t.name == "by_project")
    assert api_project_table.columns[-1].label == "Cost"
    assert sub_project_table.columns[-1].label == "Cost (list-price equivalent)"


# -- phases / snapshots / include -------------------------------------------


def test_phases_flag_adds_phases_section(tmp_path):
    corpus = _two_session_corpus(tmp_path)
    report = build_report(corpus, PRICING, Config(), projects=("proj-two",), window="w", phases=True)
    assert "phases" in [s.key for s in report.sections]

    report_off = build_report(corpus, PRICING, Config(), projects=("proj-two",), window="w", phases=False)
    assert "phases" not in [s.key for s in report_off.sections]


def test_snapshots_add_config_section(tmp_path):
    corpus = _two_session_corpus(tmp_path)
    snaps = [
        Snapshot(path="cfg1", ts="2026-09-01T00:00:00.000Z", data={"billing": "api"}),
        Snapshot(path="cfg2", ts="2026-09-19T00:00:00.000Z", data={"billing": "subscription"}),
    ]
    report = build_report(corpus, PRICING, Config(), projects=("proj-two",), window="w", snapshots=snaps)
    assert "config" in [s.key for s in report.sections]

    report_none = build_report(corpus, PRICING, Config(), projects=("proj-two",), window="w", snapshots=None)
    assert "config" not in [s.key for s in report_none.sections]


# -- COV-09: CLAUDE_AUTOCOMPACT_PCT_OVERRIDE feeds compaction_sim -----------


def test_autocompact_pct_override_scales_the_configured_window():
    snap = Snapshot(
        path="cfg1",
        ts="2026-09-01T00:00:00.000Z",
        data={"env_numeric_caps": {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": 50}},
    )
    assert _apply_autocompact_pct_override(300_000, snap) == 150_000


def test_autocompact_pct_override_ignored_when_absent_or_out_of_range():
    no_override = Snapshot(path="cfg1", ts="2026-09-01T00:00:00.000Z", data={})
    assert _apply_autocompact_pct_override(300_000, no_override) == 300_000

    # docs/en/env-vars.md: "1-100" -- 0 and 150 are both out of range and
    # must not change the configured window.
    zero = Snapshot(path="cfg1", ts="2026-09-01T00:00:00.000Z", data={"env_numeric_caps": {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": 0}})
    assert _apply_autocompact_pct_override(300_000, zero) == 300_000
    over = Snapshot(path="cfg1", ts="2026-09-01T00:00:00.000Z", data={"env_numeric_caps": {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": 150}})
    assert _apply_autocompact_pct_override(300_000, over) == 300_000


def test_autocompact_pct_override_changes_the_report_end_to_end(tmp_path):
    """Same corpus and window, two snapshots differing only in
    CLAUDE_AUTOCOMPACT_PCT_OVERRIDE -- confirms build_report's own
    snapshot_windows loop (not just the helper in isolation) actually
    picks the override up and it changes what compaction_sim simulates.
    """
    corpus = _two_session_corpus(tmp_path)
    base_data = {"effective": {"autoCompactWindow": 300_000}}
    snap_no_override = Snapshot(path="cfg1", ts="2020-01-01T00:00:00.000Z", data=base_data)
    snap_with_override = Snapshot(
        path="cfg2",
        ts="2020-01-01T00:00:00.000Z",
        data={**base_data, "env_numeric_caps": {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": 10}},
    )

    report_a = build_report(
        corpus, PRICING, Config(), projects=("proj-two",), window="w", snapshots=[snap_no_override], phases=True
    )
    report_b = build_report(
        corpus, PRICING, Config(), projects=("proj-two",), window="w", snapshots=[snap_with_override], phases=True
    )

    sim_a = next(s for s in report_a.sections if s.key == "compaction_sim")
    sim_b = next(s for s in report_b.sections if s.key == "compaction_sim")
    # Not asserting exact figures (the sweep's internals aren't this
    # test's concern) -- just that feeding a much lower effective window
    # (10% of 300,000 = 30,000) changed the simulation's own output
    # versus the unscaled 300,000 window, proving the override reached it.
    assert sim_a.tables != sim_b.tables


def test_the_env_auto_compact_window_beats_the_setting_in_the_report(tmp_path):
    """CLAUDE_CODE_AUTO_COMPACT_WINDOW overrides autoCompactWindow
    (docs/en/env-vars.md), so compaction_sim replays the sessions at the
    variable's window, not the setting's."""
    corpus = _two_session_corpus(tmp_path)
    setting = {"effective": {"autoCompactWindow": 300_000}}
    env = {
        **setting,
        "env_names": ["CLAUDE_CODE_AUTO_COMPACT_WINDOW"],
        "env_numeric_caps": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": 100_000},
    }
    same_as_env = {"effective": {"autoCompactWindow": 100_000}}

    def sim(data):
        snap = Snapshot(path="cfg", ts="2020-01-01T00:00:00.000Z", data=data)
        report = build_report(corpus, PRICING, Config(), projects=("proj-two",), window="w", snapshots=[snap])
        return next(s for s in report.sections if s.key == "compaction_sim").tables

    assert sim(env) != sim(setting)
    assert sim(env) == sim(same_as_env)


# -- COV-02: observed model/effort vs. settings -> config-drift table -------


def test_config_drift_table_carries_observed_effort_level(tmp_path):
    """``sessions_with_observed``'s ``observed`` dict now carries
    ``effortLevel`` (the settings key it's compared against) alongside
    ``model``, sourced from each top-level transcript's own dominant
    ``Turn.effort`` (``_dominant_transcript_effort``) -- the effort half
    of COV-02's "CLI/overlay layer, inferred when the transcript's model
    or effort disagrees with the settings". A snapshot whose effective
    ``effortLevel`` disagrees with what every turn actually ran under
    must produce an ``effortLevel`` row in the ``config-drift`` table.
    """
    project_dir = tmp_path / "proj-effort"
    project_dir.mkdir()
    _write_top(project_dir, "session-001", n_turns=2, effort="high")
    corpus = load_corpus([project_dir])

    snap = Snapshot(
        path="cfg1",
        ts="2020-01-01T00:00:00.000Z",
        data={"effective": {"effortLevel": "low"}},
    )
    report = build_report(corpus, PRICING, Config(), projects=("proj-effort",), window="w", snapshots=[snap])

    config = next(s for s in report.sections if s.key == "config")
    drift = next(t for t in config.tables if t.name == "config-drift")
    effort_rows = [row for row in drift.rows if row[1] == "effortLevel"]
    assert effort_rows, f"expected an effortLevel drift row, got: {drift.rows}"
    assert effort_rows[0][2] == "low"  # snapshot_value
    assert effort_rows[0][3] == "high"  # observed_value


@pytest.mark.parametrize("which_key", [0, 1], ids=["canonical-key", "legacy-key"])
def test_a_lower_case_drive_folder_joins_its_snapshots_under_either_key(tmp_path, which_key):
    """The folder is ``c--Dev-effort``: the drift table and the compaction
    window both read the snapshot the hook filed for that project, under
    the upper-case drive's key now or the lower-case one's before, and not
    another project's newer snapshot."""
    project_dir = tmp_path / "c--Dev-effort"
    project_dir.mkdir()
    _write_top(project_dir, "session-001", n_turns=2, effort="high")
    corpus = load_corpus([project_dir])
    mine_key = snapshot_project_keys("c--Dev-effort")[which_key]
    other_key = snapshot_project_key("C--Dev-other")

    def snap(key, **effective):
        return Snapshot(
            path="cfg", ts="2020-01-01T00:00:00.000Z", data={"project_slug": key, "effective": effective}
        )

    def build(*snaps, phases=False):
        return build_report(
            corpus, PRICING, Config(), projects=("c--Dev-effort",), window="w", snapshots=list(snaps), phases=phases
        )

    def effort_rows(report):
        config = next(s for s in report.sections if s.key == "config")
        drift = next(t for t in config.tables if t.name == "config-drift")
        return [row for row in drift.rows if row[1] == "effortLevel"]

    assert effort_rows(build(snap(mine_key, effortLevel="low")))
    assert not effort_rows(build(snap(other_key, effortLevel="low")))

    def sim(report):
        return next(s for s in report.sections if s.key == "compaction_sim").tables

    mine = sim(build(snap(mine_key, autoCompactWindow=100_000), phases=True))
    assert mine != sim(build(snap(other_key, autoCompactWindow=100_000), phases=True))
    assert mine == sim(build(snap(mine_key, autoCompactWindow=100_000), snap(other_key, autoCompactWindow=300_000), phases=True))


def test_config_drift_table_no_effort_row_when_settings_agree(tmp_path):
    project_dir = tmp_path / "proj-effort-agree"
    project_dir.mkdir()
    _write_top(project_dir, "session-001", n_turns=2, effort="high")
    corpus = load_corpus([project_dir])

    snap = Snapshot(
        path="cfg1",
        ts="2020-01-01T00:00:00.000Z",
        data={"effective": {"effortLevel": "high"}},
    )
    report = build_report(corpus, PRICING, Config(), projects=("proj-effort-agree",), window="w", snapshots=[snap])

    config = next(s for s in report.sections if s.key == "config")
    drift = next(t for t in config.tables if t.name == "config-drift")
    assert not [row for row in drift.rows if row[1] == "effortLevel"]


def test_include_restricts_to_named_sections(tmp_path):
    corpus = _two_session_corpus(tmp_path)
    report = build_report(
        corpus, PRICING, Config(), projects=("proj-two",), window="w", include={"overview", "recache"}
    )
    assert [s.key for s in report.sections] == ["overview", "recache"]


# -- v0.3 Task 2: baseline_comparison ---------------------------------------


def _minimal_baseline_record(**overrides) -> dict:
    record = {
        "id": "abc123",
        "created_at": "2026-09-01T00:00:00+00:00",
        "window_days": 7,
        "sessions_analysed": 6,
        "mode_mix": {},
        "cost_per_session": 0.01,
        "recache_share_pct": 5.0,
        "compactions_per_session": 0.5,
        "ttl_mix_top_level": {"5m_pct": 80.0, "1h_pct": 20.0},
        "ttl_mix_by_agent_type": {"claude-implementer": {"5m_pct": 90.0, "1h_pct": 10.0}},
        "session_baseline_size": 1234.0,
        "mean_spawn_write_by_agent_type": {"claude-implementer": 500.0},
        "scorecard_dimensions": {"cache_efficiency": 3, "context_hygiene": 4},
        "by_mode": {},
    }
    record.update(overrides)
    return record


def test_baseline_record_adds_baseline_comparison_section(tmp_path):
    corpus = _two_session_corpus(tmp_path)
    baseline_record = _minimal_baseline_record()
    report = build_report(
        corpus, PRICING, Config(), projects=("proj-two",), window="w", baseline_record=baseline_record
    )
    assert "baseline_comparison" in [s.key for s in report.sections]
    section = next(s for s in report.sections if s.key == "baseline_comparison")
    overview_table = next(t for t in section.tables if t.name == "baseline_comparison_overview")
    metrics = {row[0] for row in overview_table.rows}
    assert "Cost per session" in metrics
    assert "Re-cache share of cache-creation" in metrics
    assert "Compactions per session" in metrics
    assert any(m.startswith("TTL mix - top-level") for m in metrics)
    assert any(m.startswith("TTL mix - claude-implementer") for m in metrics)
    assert any(m.startswith("Mean spawn write - claude-implementer") for m in metrics)
    assert any(m.startswith("Scorecard level - ") for m in metrics)
    # baseline_comparison is not part of _SECTION_ORDER's include-filtering
    # contract -- it bypasses `include` deliberately (see build_report's
    # own docstring), so a focused-view call still gets it.
    focused = build_report(
        corpus,
        PRICING,
        Config(),
        projects=("proj-two",),
        window="w",
        include={"overview"},
        baseline_record=baseline_record,
    )
    assert [s.key for s in focused.sections] == ["overview", "baseline_comparison"]
    for section in report.sections:
        assert_privacy(section)


def test_baseline_comparison_by_mode_table_only_when_mode_mix_recorded(tmp_path):
    corpus = _two_session_corpus(tmp_path)
    no_mode_record = _minimal_baseline_record(mode_mix={})
    report = build_report(
        corpus, PRICING, Config(), projects=("proj-two",), window="w", baseline_record=no_mode_record
    )
    section = next(s for s in report.sections if s.key == "baseline_comparison")
    assert [t.name for t in section.tables] == ["baseline_comparison_overview"]

    with_mode_record = _minimal_baseline_record(
        mode_mix={"interactive": 6}, by_mode={"interactive": {"sessions": 6, "cost_per_session": 0.01}}
    )
    report2 = build_report(
        corpus, PRICING, Config(), projects=("proj-two",), window="w", baseline_record=with_mode_record
    )
    section2 = next(s for s in report2.sections if s.key == "baseline_comparison")
    assert "baseline_comparison_by_mode" in [t.name for t in section2.tables]
    by_mode_table = next(t for t in section2.tables if t.name == "baseline_comparison_by_mode")
    # The table's row set is the union of the baseline's recorded modes and
    # whatever mode(s) the current window's own sessions classified as --
    # "interactive" (baseline-only, 0 current sessions) is always present;
    # any modes the current corpus itself produced are additional rows,
    # not a mismatch.
    assert "interactive" in {row[0] for row in by_mode_table.rows}


def test_baseline_comparison_by_mode_suppresses_below_min_sample(tmp_path):
    corpus = _two_session_corpus(tmp_path)  # only 2 sessions, below the 5-session gate
    record = _minimal_baseline_record(mode_mix={"interactive": 6}, by_mode={"interactive": {"sessions": 6}})
    report = build_report(corpus, PRICING, Config(), projects=("proj-two",), window="w", baseline_record=record)
    section = next(s for s in report.sections if s.key == "baseline_comparison")
    by_mode_table = next(t for t in section.tables if t.name == "baseline_comparison_by_mode")
    row = next(r for r in by_mode_table.rows if r[0] == "interactive")
    sample_ok_index = [c.key for c in by_mode_table.columns].index("sample_ok")
    assert row[sample_ok_index] == "no"


def test_no_baseline_record_omits_section_and_no_note_by_default(tmp_path):
    corpus = _two_session_corpus(tmp_path)
    report = build_report(corpus, PRICING, Config(), projects=("proj-two",), window="w")
    assert "baseline_comparison" not in [s.key for s in report.sections]
    assert not any("baseline" in a.lower() for a in report.meta.assumptions)


def test_baseline_note_is_recorded_in_assumptions_when_no_record(tmp_path):
    corpus = _two_session_corpus(tmp_path)
    report = build_report(
        corpus,
        PRICING,
        Config(),
        projects=("proj-two",),
        window="w",
        baseline_record=None,
        baseline_note="run `claudeglass baseline` first",
    )
    assert "baseline_comparison" not in [s.key for s in report.sections]
    assert "run `claudeglass baseline` first" in report.meta.assumptions


# -- meta ---------------------------------------------------------------


def test_report_meta_is_fully_populated(tmp_path):
    corpus = _two_session_corpus(tmp_path)
    report = build_report(corpus, PRICING, Config(), projects=("proj-two",), window="last 7 days")
    meta = report.meta
    assert meta.window == "last 7 days"
    assert meta.projects == ("proj-two",)
    assert meta.pricing.version == PRICING.version
    assert meta.pricing.sha8 == PRICING.sha8
    assert meta.pricing.currency == PRICING.currency
    assert 0.0 <= meta.pricing.coverage_pct <= 100.0
    assert meta.billing_mode == "api"
    assert meta.thresholds["recache"]["ctx_floor"] == 20_000
    assert meta.assumptions  # ttl + recache assumptions merged in
    assert meta.tool_version
    assert meta.generated_at.endswith("Z")
    # UX-1: meta.units {mode, share_per_usd, period_label, basis} -- the
    # JS mirror's (format.js money()) only source of billing-mode facts.
    assert meta.units["mode"] == "api"
    assert meta.units["share_per_usd"] is None  # API billing has no window share
    assert meta.units["period_label"] == "weekly usage limit"
    assert meta.units["basis"] == meta.amounts_basis


def test_report_meta_projects_sorts_by_cost_descending_ties_alphabetical(tmp_path):
    """Project-filter work: ``meta.projects`` sorts by this window's cost,
    highest first (the same ``(-cost, slug)`` order ``usage.by_project``
    already sorts its own rows by), not alphabetically -- so a dashboard
    project picker built from it lists the highest-spend project first
    without a second request. ``proj-zzz-cheap`` sorts alphabetically
    last but costs least, and still ends up last here too; ``proj-aaa``
    is expensive and alphabetically first, and still sorts first --
    confirming the order really is cost-driven, not a coincidence of
    name order."""
    root = tmp_path / "projects"
    root.mkdir()
    expensive_dir = root / "proj-aaa-expensive"
    cheap_dir = root / "proj-zzz-cheap"
    expensive_dir.mkdir()
    cheap_dir.mkdir()
    _write_top(expensive_dir, "session-expensive", n_turns=20)
    _write_top(cheap_dir, "session-cheap", n_turns=1)
    corpus = load_corpus([expensive_dir, cheap_dir])
    report = build_report(
        corpus, PRICING, Config(), projects=("proj-aaa-expensive", "proj-zzz-cheap"), window="w"
    )
    assert report.meta.projects == ("proj-aaa-expensive", "proj-zzz-cheap")


def test_report_meta_projects_ties_break_alphabetically(tmp_path):
    """Two projects costing exactly the same (both empty -- no session in
    the window) fall back to alphabetical order, the same tie-break
    ``usage.by_project`` uses."""
    root = tmp_path / "projects"
    root.mkdir()
    dir_b = root / "proj-b-empty"
    dir_a = root / "proj-a-empty"
    dir_b.mkdir()
    dir_a.mkdir()
    corpus = load_corpus([dir_b, dir_a])
    report = build_report(corpus, PRICING, Config(), projects=("proj-b-empty", "proj-a-empty"), window="w")
    assert report.meta.projects == ("proj-a-empty", "proj-b-empty")


def test_report_meta_units_reflects_subscription_billing_with_no_elasticity_fit(tmp_path):
    """UX-1: under a subscription with no elasticity fit yet (this
    corpus logs no statusline usage-limit samples), ``meta.units``
    still reports ``mode == "subscription"`` and a ``None`` share
    rather than crashing or silently defaulting to API's shape."""
    corpus = _two_session_corpus(tmp_path)
    report = build_report(
        corpus, PRICING, Config(billing="subscription"), projects=("proj-two",), window="last 7 days"
    )
    assert report.meta.units["mode"] == "subscription"
    assert report.meta.units["share_per_usd"] is None
    assert report.meta.units["period_label"] == "weekly usage limit"
    assert report.meta.units["basis"] == report.meta.amounts_basis


def test_thresholds_min_sample_reflects_recommend_overrides_not_config_defaults(tmp_path):
    """Regression test for the min-sample header fix: the thresholds
    header used to print ``config.min_sessions``/``config.min_turns``
    directly, but ``recommend.RecommendThresholds`` has its own
    independently overridable ``min_sessions``/``min_turns`` (via
    ``[thresholds.recommend]``), which is what ``recommend()`` actually
    gates on. A config that leaves the top-level fields at their defaults
    but overrides ``[thresholds.recommend]`` must show the *override* in
    the header, not the stale top-level default.
    """
    corpus = _two_session_corpus(tmp_path)
    config = Config(thresholds={"recommend": {"min_sessions": 42, "min_turns": 4242}})
    assert config.min_sessions == 5  # top-level default, deliberately left untouched
    assert config.min_turns == 200

    report = build_report(corpus, PRICING, config, projects=("proj-two",), window="w")
    assert report.meta.thresholds["min_sessions"] == 42
    assert report.meta.thresholds["min_turns"] == 4242


# -- smoke: full render through every renderer -------------------------


def test_full_report_renders_through_every_renderer(tmp_path):
    corpus = _two_session_corpus(tmp_path)
    report = build_report(
        corpus,
        PRICING,
        Config(),
        projects=("proj-two",),
        window="last 7 days",
        phases=True,
        snapshots=[Snapshot(path="cfg1", ts="2026-09-01T00:00:00.000Z", data={"billing": "api"})],
    )

    md = render_markdown(report)
    assert isinstance(md, str) and md

    js = render_json(report)
    parsed = json.loads(js)
    assert "report" in parsed
    assert "sections" in parsed["report"]

    html = render_html(report)
    assert isinstance(html, str) and html

    out_dir = tmp_path / "csv-out"
    write_csv_dir(report, out_dir)
    assert list(out_dir.rglob("*.csv"))


# -- real fixture (skipped when absent) --------------------------------

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "real" / "session-a"

pytestmark_real = pytest.mark.skipif(
    not FIXTURE_DIR.exists() or not any(FIXTURE_DIR.glob("*.jsonl")),
    reason="tests/fixtures/real/session-a/ not present (real fixture not checked out)",
)


@pytestmark_real
def test_build_report_against_real_fixture():
    corpus = load_corpus([FIXTURE_DIR])
    report = build_report(corpus, PRICING, Config(), projects=("session-a",), window="real fixture")

    assert [s.key for s in report.sections] == [k for k in _SECTION_ORDER if k not in ("phases", "config", "elasticity")]
    overview = next(s for s in report.sections if s.key == "overview")
    totals = {row[0]: row[1] for row in overview.tables[0].rows}
    assert totals["sessions"] > 0
    assert totals["priced_turns"] > 0
    assert totals["total_cost_usd"] > 0.0

    for section in report.sections:
        assert_privacy(section)
    _all_sections_row_keys_are_valid(report.sections)


# -- R5: group_by="agent" must key by transcript, not session ---------------


@pytestmark_real
def test_recache_by_group_agent_matches_recache_by_agent_type_on_real_fixture():
    """Regression test for review finding R5: the group-by re-fold used to
    key every transcript by its *session's* one dominant group, so every
    subagent in a session inherited that session's single label even when
    the session spawned several different agent types. The real fixture's
    one session spawns claude-implementer/general-purpose/revixo-researcher/
    verification-runner subagents (plus the top-level transcript), so a
    correct per-transcript ``group_by="agent"`` re-fold must produce one
    ``recache_by_group`` row per agent_type -- matching
    ``recache_by_agent_type`` exactly -- rather than collapsing them all
    into whichever single agent type the session-keyed lookup used to pick.
    """
    corpus = load_corpus([FIXTURE_DIR])
    report = build_report(
        corpus, PRICING, Config(), projects=("session-a",), window="real fixture", group_by="agent"
    )
    recache_section = next(s for s in report.sections if s.key == "recache")
    group_table = next(t for t in recache_section.tables if t.name == "recache_by_group")
    by_agent_type_table = next(t for t in recache_section.tables if t.name == "recache_by_agent_type")

    assert len(by_agent_type_table.rows) > 1  # the fixture spawns several distinct agent types
    assert len(group_table.rows) == len(by_agent_type_table.rows)

    priced_turns_by_agent_type = {row[0]: row[1] for row in by_agent_type_table.rows}
    recache_turns_by_agent_type = {row[0]: row[2] for row in by_agent_type_table.rows}
    for row in group_table.rows:
        label = row[0]
        assert label in priced_turns_by_agent_type, f"unexpected group label {label!r}"
        # group column prepended: row[1]=metric, row[2]=transcripts, row[3]=priced_turns, row[4]=recache_turns.
        assert row[3] == priced_turns_by_agent_type[label]
        assert row[4] == recache_turns_by_agent_type[label]


def test_limit_recache_share_counts_only_limit_expiry_rebuilds():
    from claudeglass.model import Turn
    from claudeglass.report import _recache_shares

    turns = [
        Turn(cache_creation_tokens=100, is_recache=True, recache_signature="limit-expiry", gap_cause="limit"),
        # After a limit pause, but the cache survived: not a rebuild.
        Turn(cache_creation_tokens=300, gap_cause="limit"),
        Turn(cache_creation_tokens=600),
    ]
    recache_share, limit_share = _recache_shares(turns)
    assert recache_share == 10.0
    assert limit_share == 10.0
    assert _recache_shares([]) == (None, None)


def test_session_records_carry_the_profile_active_at_their_start(tmp_path, monkeypatch):
    # The apply stamp for "lean" predates the session's first turn, so
    # its SessionRecord is filled in with that profile.
    from claudeglass import report as report_mod

    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    write_jsonl(project_dir / "s1.jsonl", [turn_line(timestamp="2026-09-18T12:00:00.000Z")])
    config_dir = tmp_path / "config"
    (config_dir / "snapshots").mkdir(parents=True)
    (config_dir / "snapshots" / "20260918T110000Z.json").write_text(
        json.dumps({"ts": "20260918T110000Z", "schema_version": 2, "profile_id": "lean"}), encoding="utf-8"
    )
    seen: list = []
    real = report_mod.workstyle.build_section
    monkeypatch.setattr(report_mod.workstyle, "build_section", lambda records: seen.extend(records) or real(records))

    build_report(load_corpus([project_dir]), PRICING, Config(), projects=("proj",), window="w", config_dir=config_dir)

    assert [r.profile_id for r in seen] == ["lean"]

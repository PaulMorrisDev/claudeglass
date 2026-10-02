"""Cross-module contract test (final task of the 12-item review fix list):
every ``Table`` produced by every ``build_section``/``build_config_section``
entry point must have a non-empty first column usable as a row key --
``row[0]`` is a non-empty ``str`` on every row, for every table. A report
renderer (Markdown/HTML, or a future ``--group-by`` drill-down) needs to
key off column zero uniformly, without a per-table special case for "this
one's first column might be ``None``/an int/a float/a tuple".

v0.2.0 fix A2: this used to also accept a plain ``int`` row key (excluding
``bool``), because ``recache.py``'s ``recache_summary``/``recache_huge_context``
tables each led with a bare numeric count rather than a label. Both now carry
an explicit leading ``metric`` string column (see ``recache.py``'s module
docstring), so every table's row key is a genuine label; this contract
tightens to match and would now catch a future table making the same
mistake.

Exercises every module that defines one of these entry points as of this
task: ``classify``, ``compaction``, ``recache``, ``ttl``, ``workstyle``,
``workflows``, and ``snapshots``. Each gets the smallest hand-built fixture
that produces at least one real (non-empty) row per table where the
module's own logic allows it, following the same ``_turn``/``write_jsonl``
construction patterns already used in each module's own test file.

WP10a addition: ``report.build_report``, ``usage.build_section`` and
``scorecard.build_section`` (and, transitively through ``build_report``,
every section ``report.py`` assembles, including ``topology``'s and
``phases``'s, which had no dedicated check here before) are exercised the
same way.
"""

from __future__ import annotations

from pathlib import Path

from claudeglass import (
    agent_models,
    classify,
    compaction,
    recache,
    recommend,
    report,
    scorecard,
    snapshots,
    ttl,
    usage,
    workflows,
    workstyle,
)
from claudeglass.config import Config
from claudeglass.corpus import load_corpus
from claudeglass.model import (
    Classification,
    EventKind,
    Section,
    SessionRecord,
    TranscriptMeta,
    TranscriptResult,
    Turn,
    WorkflowRun,
)
from claudeglass.parse import parse_transcript
from claudeglass.pricing import load_pricing

from helpers import system_line, tool_use_block, turn_line, write_jsonl

PRICING = load_pricing()
SONNET_RATES = PRICING.resolve_model("claude-sonnet-5")


def _assert_row_keys_are_valid(section: Section) -> None:
    """Every row, in every table, in ``section`` has a non-empty ``str``
    first column (fix A2: an ``int`` row key -- even excluding ``bool``,
    which was never what a caller meant by "row key" -- is no longer
    accepted either; see this module's docstring)."""
    for table in section.tables:
        for row in table.rows:
            assert row, f"{section.key}/{table.name}: a row is empty"
            key = row[0]
            assert isinstance(key, str), (
                f"{section.key}/{table.name}: row[0]={key!r} "
                f"({type(key).__name__}) is not a str row key"
            )
            assert key != "", f"{section.key}/{table.name}: row[0] is an empty string"


# -- classify.build_section ---------------------------------------------


def _classify_records() -> list[SessionRecord]:
    return [
        SessionRecord(
            session_id="s1",
            slug="proj-a",
            first_ts="2026-09-18T10:00:00.000Z",
            span_s=120.0,
            classification=Classification(mode="interactive", purpose="general-dev"),
        ),
        SessionRecord(
            session_id="s2",
            slug="proj-b",
            first_ts="2026-09-18T23:30:00.000Z",
            span_s=600.0,
            classification=Classification(mode="overnight", purpose="review"),
        ),
    ]


def test_classify_build_section_row_keys_are_all_str_or_int():
    section = classify.build_section(_classify_records())
    _assert_row_keys_are_valid(section)


# -- compaction.build_section ---------------------------------------------


def _compaction_stats(tmp_path: Path) -> compaction.CompactionStats:
    lines = [
        turn_line(
            model="claude-sonnet-5",
            timestamp="2026-09-18T12:00:00.000Z",
            ephemeral_5m_input_tokens=1000,
            input_tokens=100,
            output_tokens=50,
            content=[tool_use_block("Bash", "tu1", {"command": "echo hi"})],
        ),
        system_line(
            "compact_boundary",
            timestamp="2026-09-18T12:00:30.000Z",
            compactMetadata={
                "trigger": "auto",
                "preTokens": 100000,
                "postTokens": 20000,
                "cumulativeDroppedTokens": 80000,
                "durationMs": 1200,
            },
        ),
        turn_line(
            model="claude-sonnet-5",
            timestamp="2026-09-18T12:01:00.000Z",
            ephemeral_1h_input_tokens=25000,
            cache_read_input_tokens=1000,
            input_tokens=0,
            output_tokens=80,
        ),
    ]
    path = tmp_path / "session.jsonl"
    write_jsonl(path, lines)
    meta = TranscriptMeta(path=str(path), session_id="sess_contract")
    result = parse_transcript(path, meta)
    return compaction.CompactionStats.build([(result, SONNET_RATES)])


def test_compaction_build_section_row_keys_are_all_str_or_int(tmp_path):
    stats = _compaction_stats(tmp_path)
    section = compaction.build_section(stats)
    _assert_row_keys_are_valid(section)


# -- recache.build_section ---------------------------------------------


def _recache_turn(**overrides) -> Turn:
    fields = dict(
        message_id="msg",
        request_id="req",
        turn_index=1,
        ts="2026-09-18T12:00:00.000Z",
        gap_s=None,
        model="claude-sonnet-5",
        is_synthetic=False,
        input_tokens=100,
        cache_creation_tokens=100_000,
        cache_read_tokens=500,
        output_tokens=50,
        cc_5m=100_000,
        cc_1h=0,
        ctx=100_600,
        preceding_tool="none",
        preceding_primary=EventKind.UNKNOWN,
        preceding_event_kinds=(),
        preceding_attachment_types=(),
        preceding_cmd_prefix=None,
    )
    fields.update(overrides)
    return Turn(**fields)


def test_recache_build_section_row_keys_are_all_str_or_int():
    th = recache.RecacheThresholds()
    stats = recache.RecacheStats(th)
    result = TranscriptResult(
        meta=TranscriptMeta(agent_type="claude-implementer"),
        turns=[_recache_turn()],
    )
    stats.add(result, lambda model: PRICING.resolve_model(model))
    section = recache.build_section(stats, PRICING, th)
    _assert_row_keys_are_valid(section)


# -- ttl.build_section ---------------------------------------------------


def _ttl_turn(**overrides) -> Turn:
    fields = dict(
        message_id="msg_1",
        request_id="req_1",
        turn_index=1,
        ts="2026-09-18T12:00:00.000Z",
        model="claude-sonnet-5",
        input_tokens=100,
        cache_creation_tokens=1000,
        cache_read_tokens=500,
        output_tokens=50,
        cc_5m=1000,
        cc_1h=0,
        ctx=1600,
        gap_s=None,
    )
    fields.update(overrides)
    return Turn(**fields)


def test_ttl_build_section_row_keys_are_all_str_or_int():
    stats = ttl.TtlStats()
    stats.add(
        TranscriptResult(meta=TranscriptMeta(kind="top-level", agent_type="top-level"), turns=[_ttl_turn()]),
        SONNET_RATES,
    )
    section = ttl.build_section(stats, billing_mode="api")
    _assert_row_keys_are_valid(section)


# -- workstyle.build_section ---------------------------------------------


def test_workstyle_build_section_row_keys_are_all_str_or_int():
    records = [
        SessionRecord(session_id="s1", archetype="single-model"),
        SessionRecord(session_id="s2", archetype="chat-only"),
        SessionRecord(session_id="s3", archetype=None),  # excluded, not a row
    ]
    section = workstyle.build_section(records)
    _assert_row_keys_are_valid(section)


# -- workflows.build_section ----------------------------------------------


def test_workflows_build_section_row_keys_are_all_str_or_int():
    runs = [
        WorkflowRun(run_id="wf_a", session_id="s1", agent_count=2, phases=1, cost=1.0, status="completed"),
        WorkflowRun(run_id="wf_b", session_id="s2", agent_count=1, phases=1, cost=0.5, status=None),
    ]
    section = workflows.build_section(runs)
    _assert_row_keys_are_valid(section)


# -- snapshots.build_config_section ---------------------------------------


def _snapshot(tmp_path: Path, ts: str, data: dict) -> snapshots.Snapshot:
    return snapshots.Snapshot(path=tmp_path / f"{ts}.json", ts=ts, data=data)


def test_snapshots_build_config_section_row_keys_are_all_str_or_int(tmp_path):
    snaps = [
        _snapshot(tmp_path, "20260901T000000Z", {"user_settings": {"model": "sonnet"}}),
        _snapshot(tmp_path, "20260910T000000Z", {"user_settings": {"model": "opus"}}),
    ]
    sessions_with_metrics = [
        {
            "session_id": "s1",
            "first_ts": "2026-09-05T00:00:00.000Z",
            "turns": 10,
            "cost": 1.0,
            "recache_cc": 0,
            "cc_total": 100,
            "compactions": 0,
            "span_s": 60,
        },
        {
            "session_id": "s2",
            "first_ts": "2026-09-12T00:00:00.000Z",
            "turns": 5,
            "cost": 0.5,
            "recache_cc": 0,
            "cc_total": 50,
            "compactions": 0,
            "span_s": 30,
        },
    ]
    section = snapshots.build_config_section(sessions_with_metrics, snaps, "user_settings.model")
    _assert_row_keys_are_valid(section)


# -- usage.build_section ---------------------------------------------------


def test_usage_build_section_row_keys_are_all_str_or_int(tmp_path):
    project_dir = tmp_path / "proj-usage"
    project_dir.mkdir()
    write_jsonl(
        project_dir / "session-usage.jsonl",
        [turn_line(timestamp="2026-09-18T12:00:00.000Z", input_tokens=100, output_tokens=20)],
    )
    corpus = load_corpus([project_dir])
    section = usage.build_section(corpus, PRICING, Config())
    _assert_row_keys_are_valid(section)


# -- scorecard.build_section ------------------------------------------------


def test_scorecard_build_section_row_keys_are_all_str_or_int():
    inputs = scorecard.ScorecardInputs(
        recache_share_pct=10.0,
        p90_top_level_ctx=60_000,
        has_spawns=True,
        agent_cost_variance_ratio=1.4,
        has_snapshot=True,
        changed_config_keys=2,
        pricing_coverage_pct=98.0,
    )
    section = scorecard.build_section(inputs)
    _assert_row_keys_are_valid(section)


# -- report.build_report (every assembled section) --------------------------


def test_build_report_every_section_row_keys_are_all_str_or_int(tmp_path):
    project_dir = tmp_path / "proj-report"
    project_dir.mkdir()
    write_jsonl(
        project_dir / "session-report.jsonl",
        [
            turn_line(
                timestamp="2026-09-18T12:00:00.000Z",
                input_tokens=100,
                output_tokens=20,
                ephemeral_5m_input_tokens=1000,
                cache_read_input_tokens=100,
            )
        ],
    )
    agent_dir = project_dir / "session-report" / "subagents"
    agent_dir.mkdir(parents=True)
    write_jsonl(agent_dir / "agent-report.jsonl", [turn_line(input_tokens=50, output_tokens=10)])
    (agent_dir / "agent-report.meta.json").write_text(
        '{"agentType": "claude-implementer", "model": "claude-sonnet-5"}', encoding="utf-8"
    )

    corpus = load_corpus([project_dir])
    model = report.build_report(
        corpus, PRICING, Config(), projects=("proj-report",), window="contract test", phases=True
    )
    for section in model.sections:
        _assert_row_keys_are_valid(section)


# -- recommend.recommend (evidence row-keys resolve into real report rows) --
#
# WP10b addition: ``report.build_report`` now also populates
# ``ReportModel.recommendations`` (recommend.py). Every
# ``Recommendation.evidence`` tuple is ``(label, value, source_table,
# row_key)`` where ``row_key`` crosses the same module boundary this
# file's ``_assert_row_keys_are_valid`` already polices for a ``Table``'s
# own rows -- so it gets the same str/int-and-non-empty check, plus the
# stronger check that it actually resolves to a real row in the cited
# ``<section_key>.<table_name>``.


def _value_matches_some_cell(value, row: list) -> bool:
    """R11: whether ``value`` (an evidence tuple's cited number/string)
    is actually present -- formatted or raw -- among ``row``'s cells,
    rather than a number ``recommend.py`` merely derived from the row
    (e.g. a computed percentage share). Floats compare with tolerance
    (a cited value may have been read straight off the row, but could
    in principle pass through a formatting round-trip); everything
    else also matches on its string form so an int cited as a float
    (or vice versa) still counts."""
    for cell in row:
        if isinstance(value, float) or isinstance(cell, float):
            try:
                if isinstance(value, (int, float)) and isinstance(cell, (int, float)):
                    if abs(float(cell) - float(value)) < 1e-9:
                        return True
            except (TypeError, ValueError):
                pass
        if cell == value:
            return True
        if str(cell) == str(value):
            return True
    return False


def _assert_recommendation_evidence_is_valid(model: report.ReportModel) -> None:
    for rec in model.recommendations:
        for label, value, source_table, row_key in rec.evidence:
            # Fix A2: str-only row keys (see this module's docstring) --
            # an evidence tuple's row_key must resolve into a real table
            # row, and every table row key is now a non-empty str.
            assert isinstance(row_key, str), (
                f"{rec.id}: evidence {label!r} row_key={row_key!r} ({type(row_key).__name__}) "
                "is not a str row key"
            )
            assert row_key != "", f"{rec.id}: evidence {label!r} row_key is an empty string"
            section_key, _, table_name = source_table.partition(".")
            section = next((s for s in model.sections if s.key == section_key), None)
            assert section is not None, f"{rec.id}: no section {section_key!r} for evidence {label!r}"
            table = next((t for t in section.tables if t.name == table_name), None)
            assert table is not None, f"{rec.id}: no table {table_name!r} for evidence {label!r}"
            row = next((r for r in table.rows if r and r[0] == row_key), None)
            assert row is not None, f"{rec.id}: row_key {row_key!r} not found in {source_table}"
            # R11: the cited value itself must be a real cell in that row,
            # not a number recommend.py computed from it (e.g. a share
            # percentage derived across several rows) -- catches evidence
            # that resolves to a real row/table but still cites a made-up
            # number for it.
            assert _value_matches_some_cell(value, row), (
                f"{rec.id}: evidence {label!r} value {value!r} is not any cell of row {row!r} "
                f"in {source_table}/{row_key!r}"
            )


def test_recommend_evidence_row_keys_resolve_into_the_report(tmp_path):
    project_dir = tmp_path / "proj-recommend-contract"
    project_dir.mkdir()
    # 220 priced turns in one session clears the "200 priced turns" half
    # of recommend.py's minimum-sample gate; heavy cache_read relative to
    # input/output tokens trips cache-read-dominance, so this isn't a
    # vacuous walk over an empty recommendations list.
    lines = [
        turn_line(
            timestamp=f"2026-09-{10 + (i % 15):02d}T12:00:00.000Z",
            input_tokens=100,
            output_tokens=50,
            ephemeral_5m_input_tokens=1000,
            cache_read_input_tokens=5000,
        )
        for i in range(220)
    ]
    write_jsonl(project_dir / "session-recommend-contract.jsonl", lines)

    corpus = load_corpus([project_dir])
    model = report.build_report(
        corpus, PRICING, Config(), projects=("proj-recommend-contract",), window="contract test", phases=True
    )
    assert model.recommendations, "expected at least one recommendation from this cache-read-heavy corpus"
    _assert_recommendation_evidence_is_valid(model)


# -- R11: every rule must fire at least once, with evidence that survives
# the strengthened value check ----------------------------------------------

#: Every recommendation id ``recommend.recommend`` can currently produce
#: (see recommend.py's ``recommend()`` entry point) -- the fixture below
#: is built so each one fires at least once in a single pass.
_ALL_RULE_IDS = frozenset(
    {
        "ttl-switch",
        "long-tool-waits",
        "notification-invalidation",
        "batch-instructions",
        "subagent-volume",
        "compaction-churn",
        "long-context-share",
        "cache-read-dominance",
        "baseline-bloat",
        "agent-report-size",
        "spawn-cost",
        "effort-mismatch",
        "discovery-share",
        "pricing-coverage",
        "data-quality",
        "limit-pressure",
        "agent-model-inherited",
        "agent-model-asked",
        "agent-decide-apply",
    }
)


def _agent_models_table() -> "Table":
    """``model_swap_agent_models`` as ``agent_models.build_table`` makes it,
    from hand-built stats with one group per verdict. general-purpose has a
    row for two verdicts, so each card's evidence must cite its own row."""
    opus = "claude-opus-5-5"
    stats = agent_models.AgentModelStats(sonnet_model="claude-sonnet-5-5")
    for agent_type, kind, verdict, roles, cost, on_sonnet in (
        ("workflow-subagent", "workflow", "inherited", {"implement": 4, "fix": 1}, 30.0, 24.0),
        ("general-purpose", "agent tool", "asked", {"implement": 3}, 20.0, 16.0),
        ("general-purpose", "agent tool", "decide-apply", {"audit": 2}, 10.0, 0.0),
    ):
        stats.groups[(agent_type, verdict)] = agent_models.AgentModelGroup(
            key=agent_type,
            kind=kind,
            verdict=verdict,
            runs=sum(roles.values()),
            role_counts=roles,
            model_counts={opus: sum(roles.values())},
            cost=cost,
            cost_on_sonnet=on_sonnet,
            write_turns=12,
            workflow_run_ids={"wf_a"} if kind == "workflow" else set(),
            first_seen="2026-09-23",
            last_seen="2026-10-01",
        )
    return agent_models.build_table(stats)


def _build_every_rule_fixture() -> "report.ReportModel":
    from claudeglass.model import Column, Diagnostics, PricingMeta, ReportMeta, ReportModel, Table

    overview = Section(
        key="overview",
        title="Overview",
        tables=[
            Table(
                name="totals",
                title="Totals",
                columns=[Column(key="metric", label="Metric"), Column(key="value", label="Value")],
                rows=[
                    ["sessions", 10],
                    ["priced_turns", 1000],
                    ["cache_read_cost_share_pct", 60.0],
                ],
            )
        ],
    )

    ttl_section = Section(
        key="ttl",
        title="TTL",
        tables=[
            Table(
                name="ttl_by_agent_type",
                title="TTL by agent type",
                columns=[
                    Column(key="agent_type", label="Agent type"),
                    Column(key="cost_observed", label="Cost observed"),
                    Column(key="fidelity_pct", label="Fidelity"),
                    Column(key="recommendation", label="Recommendation"),
                    Column(key="lever", label="Lever"),
                ],
                rows=[
                    ["top-level", 40.0, 5.0, "switch to 1h", "promptCacheTtl"],
                    [
                        "claude-implementer",
                        60.0,
                        15.0,
                        "switch to 5m",
                        "experimental.cacheTtl in claude-implementer.md (or subagentPromptCacheTtl for all subagents)",
                    ],
                ],
            )
        ],
    )

    recache_section = Section(
        key="recache",
        title="Recache",
        tables=[
            Table(
                name="recache_summary",
                title="Recache summary",
                columns=[Column(key="metric", label="Metric"), Column(key="recache_cc_tokens", label="Recache cc tokens")],
                rows=[["all", 100_000]],
            ),
            Table(
                name="recache_signature_split",
                title="Signature split",
                columns=[Column(key="signature", label="Signature"), Column(key="cc_tokens", label="CC tokens")],
                rows=[["full-expiry", 30_000], ["prefix-invalidated", 70_000]],
            ),
            Table(
                name="recache_preceding_tool",
                title="Preceding tool",
                columns=[Column(key="tool", label="Tool"), Column(key="share_pct_turns", label="Share")],
                rows=[["Bash", 40.0], ["PowerShell", 30.0]],
            ),
            Table(
                name="recache_gap_buckets",
                title="Gap buckets",
                columns=[Column(key="bucket", label="Bucket"), Column(key="share_pct_turns", label="Share")],
                rows=[[">60m", 30.0], ["15-60m", 25.0], ["5-15m", 20.0], ["1-5m", 15.0], ["<1m", 10.0]],
            ),
            Table(
                name="recache_primary_cause_prefix_invalidated",
                title="Primary cause (prefix-invalidated)",
                columns=[
                    Column(key="primary", label="Primary"),
                    Column(key="cc_share_pct", label="CC share"),
                    Column(key="control_cc_share_pct", label="Control share"),
                    Column(key="over_representation_points_tokens", label="Over-rep points"),
                ],
                rows=[["task_notification", 40.0, 20.0, 20.0]],
            ),
            Table(
                name="recache_primary_cause",
                title="Primary cause",
                columns=[
                    Column(key="primary", label="Primary"),
                    Column(key="cc_share_pct", label="CC share"),
                    Column(key="over_representation_points_tokens", label="Over-rep points"),
                ],
                rows=[["queue_operation", 20.0, 15.0]],
            ),
        ],
    )

    compactions_section = Section(
        key="compactions",
        title="Compactions",
        tables=[
            Table(
                name="compactions_summary",
                title="Compactions summary",
                columns=[Column(key="metric", label="Metric"), Column(key="value", label="Value")],
                rows=[
                    ["Compactions per session (mean)", 3.0],
                    ["Dropped tokens (share of new_tokens: input+cache_creation)", 10.0],
                ],
            )
        ],
    )

    scorecard_section = Section(
        key="scorecard",
        title="Scorecard",
        tables=[
            Table(
                name="dimensions",
                title="Dimensions",
                columns=[
                    Column(key="dimension", label="Dimension"),
                    Column(key="level", label="Level"),
                    Column(key="label", label="Label"),
                    Column(key="metric", label="Metric"),
                    Column(key="value", label="Value"),
                    Column(key="threshold", label="Threshold"),
                ],
                rows=[
                    ["context_hygiene", "warn", "Context hygiene", "p90_top_level_ctx", 200_000, 150_000],
                    ["data_quality", "warn", "Data quality", "pricing_coverage_pct", 90.0, 100.0],
                ],
            )
        ],
    )

    agents_section = Section(
        key="agents",
        title="Agents",
        tables=[
            Table(
                name="topology_session_baseline",
                title="Session baseline",
                columns=[Column(key="agent_type", label="Agent type"), Column(key="mean_baseline", label="Mean baseline")],
                rows=[["top-level", 50_000]],
            ),
            Table(
                name="topology_report_proxy",
                title="Report proxy",
                columns=[Column(key="agent_type", label="Agent type"), Column(key="mean_proxy", label="Mean proxy")],
                rows=[["claude-implementer", 10_000]],
            ),
            Table(
                name="topology_spawn_write",
                title="Spawn write",
                columns=[Column(key="agent_type", label="Agent type"), Column(key="mean_write", label="Mean write")],
                rows=[["claude-implementer", 50_000]],
            ),
            Table(
                name="topology_effort_tokens",
                title="Effort tokens",
                columns=[Column(key="effort", label="Effort"), Column(key="thinking_share", label="Thinking share")],
                rows=[["high", 50.0]],
            ),
        ],
    )

    sessions_section = Section(
        key="sessions",
        title="Sessions",
        tables=[
            Table(
                name="sessions_by_purpose",
                title="Sessions by purpose",
                columns=[Column(key="purpose", label="Purpose"), Column(key="sessions", label="Sessions")],
                rows=[["general-dev", 5]],
            )
        ],
    )

    phases_section = Section(
        key="phases",
        title="Phases",
        tables=[
            Table(
                name="phases_summary",
                title="Phases summary",
                columns=[Column(key="phase", label="Phase"), Column(key="cost_share_pct", label="Cost share")],
                rows=[["discovery", 50.0]],
            )
        ],
    )

    limits_section = Section(
        key="limits",
        title="Usage limits",
        tables=[
            Table(
                name="limits_summary",
                title="Usage-limits summary",
                columns=[
                    Column(key="metric", label="Metric"),
                    Column(key="limit_hits", label="Limit hits"),
                    Column(key="agents_terminated_rate_limit", label="Agents terminated by rate limit"),
                    Column(key="sessions_affected", label="Sessions affected"),
                ],
                rows=[["all", 5, 1, 3]],
            )
        ],
    )

    model_swap_section = Section(key="model_swap", title="Model swap", tables=[_agent_models_table()])

    return ReportModel(
        meta=ReportMeta(pricing=PricingMeta(coverage_pct=90.0)),
        sections=[
            overview,
            ttl_section,
            recache_section,
            compactions_section,
            scorecard_section,
            agents_section,
            sessions_section,
            phases_section,
            limits_section,
            model_swap_section,
        ],
        recommendations=[],
        diagnostics=Diagnostics(lines=1000, unparsable_lines=0, ttl_sum_mismatch=1),
    )


def test_recommend_every_rule_fires_with_valid_evidence():
    """R11: strengthens the evidence-exists check above (which only ever
    exercised ``cache-read-dominance`` through a real corpus) to run
    every rule ``recommend()`` implements at least once, over a single
    hand-built ``ReportModel`` engineered to clear every rule's
    threshold simultaneously -- catching an evidence-contract violation
    (like R11's subagent-volume bug) in any rule, not just the one a
    real corpus happens to exercise."""
    model = _build_every_rule_fixture()
    # Fix #20: mcp_servers is the fixed three-key dict the hook actually
    # emits ({"names": [...], "enabled_mcpjson_servers": [...],
    # "disabled_mcpjson_servers": [...]}) -- server names live under
    # "names", not as top-level dict keys.
    snapshot = snapshots.Snapshot(
        path=Path("snap.json"),
        ts="20260918T000000Z",
        data={"mcp_servers": {"names": [f"server{i}" for i in range(5)]}},
    )
    recs = recommend.recommend(model, config=Config(), archetype=None, snapshot=snapshot)

    fired_ids = {rec.id for rec in recs}
    missing = _ALL_RULE_IDS - fired_ids
    assert not missing, f"rules that did not fire on the every-rule fixture: {sorted(missing)}"

    model.recommendations = recs
    _assert_recommendation_evidence_is_valid(model)


def test_every_rule_id_that_can_fire_with_no_setting_change_has_a_workflow_explainer():
    """UX-8 (rest): every id ``recommend.recommend()`` can natively
    produce (``_ALL_RULE_IDS``, none of which build a ``SettingChange`` --
    see ``_build_every_rule_fixture``) must be registered in
    ``fixes._WORKFLOW_EXPLAINER``, so ``build_fixes`` gives it a
    where/trade-off/undo entry instead of an empty or missing fix. This
    is a linkage check: it catches a new rule id added to
    ``_ALL_RULE_IDS`` (and therefore to ``recommend.py``) without a
    matching entry here, which is exactly the gap UX-8 closed."""
    from claudeglass import fixes

    missing = _ALL_RULE_IDS - set(fixes._WORKFLOW_EXPLAINER)
    assert not missing, f"rule ids with no where/trade-off/undo explainer: {sorted(missing)}"

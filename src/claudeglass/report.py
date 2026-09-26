"""Report assembly (WP10a): turn a :class:`~claudeglass.corpus.Corpus`
into the whole :class:`~claudeglass.model.ReportModel` every renderer
(Markdown, JSON, CSV, HTML) consumes.

This is the integration layer every other WP4-WP9 analytics module feeds
into: it walks every session bundle once, folds each transcript into the
existing accumulators (:class:`recache.RecacheStats`,
:class:`ttl.TtlStats`, :class:`compaction.CompactionStats`,
:class:`topology.TopologyStats`, :func:`classify.build_session_record`,
:class:`workstyle.SessionFeatures`/:func:`workstyle.detect_archetype`,
optionally :class:`phases.PhaseStats`), and assembles their
``build_section`` outputs into ``ReportModel.sections`` in a fixed order.

Section order and keys: ``overview``, ``usage``, ``elasticity`` (only
under subscription billing with usage-log readings, see
:func:`_report_units`), ``sessions``, ``recache``,
``ttl``, ``limits``, ``carry``, ``compaction_sim``, ``plan_handoff``,
``model_swap``, ``waste``, ``compactions``, ``agent_startup``, ``agents``, ``run_split``,
``hooks``, ``quality``,
``workstyle``,
``workflows``, ``phases`` (only when ``phases=True``), ``config`` (only
when snapshots are supplied), ``context_budget``, ``tool_search``, ``capture``,
``cost_record`` (Claude Code's own cost record against this tool's, from
``reconcile.py``), ``scorecard``,
``baseline_comparison``
(v4 wiring round: ``carry``/``compaction_sim``/``model_swap``/``waste``
are the four v4 analytics modules, wired in here immediately after
``limits`` -- grouped together, in the same order the wiring brief itself
lists them, rather than interleaved among the pre-existing sections)
(v0.3 Task 2 addition, only when a ``baseline_record`` is passed --
see ``build_report``'s own docstring; deliberately *not* subject to
``include`` filtering). ``include``, when given, keeps only sections
whose key is in it (used by the ``recache``/``ttl``/``compactions``/
``sessions`` subcommands to render a single focused section rather than
the whole report).

Deviations from the task brief, reported rather than made silently (see
``model.py``'s module docstring for this project's convention):

- The brief's ordered section-key list ends in ``"diagnostics"``, but
  ``model.py``'s frozen contract already has a *dedicated*
  ``ReportModel.diagnostics: Diagnostics`` field, and every renderer
  (confirmed in ``render/markdown.py``'s own module docstring: "6.
  Diagnostics") already renders it from that field directly, outside the
  ``sections`` list. Adding a *second*, table-shaped ``Section(key=
  "diagnostics", ...)`` would both duplicate that and not fit
  ``Diagnostics``'s scalar/dict shape into ``Table``'s rows-of-values
  shape well. This module aggregates Diagnostics into
  ``ReportModel.diagnostics`` (as instructed) and does not also emit a
  ``"diagnostics"`` Section — the existing renderer contract already
  covers it.
- The "config" section's task description ("config (only when snapshots
  present)") doesn't specify which config key(s) to diff — a report-wide
  ``build_report`` call has no single "the one key that changed" the way
  a ``config-diff <key>`` CLI subcommand would. This module calls
  :func:`snapshots.diff_keys` to find every key that changed across the
  supplied snapshots, and renders one :func:`snapshots.build_config_diff_table`
  per changed key (capped — see :data:`_MAX_CONFIG_DIFF_KEYS`), rather
  than reusing :func:`snapshots.build_config_section` directly (which
  takes exactly one ``key`` and returns ``Section(key="config_diff", ...)``
  — a different section key than this task specifies).
  Review fix #14: this section now additionally appends
  ``snapshots.build_effective_config_table``/``build_config_layers_table``/
  ``build_config_groups_table`` (no inputs beyond the snapshots already
  in hand) and, once a session's own dominant top-level model is known,
  ``build_config_drift_table`` (fix #15: model comparisons are alias-
  normalised via ``pricing.resolve_model``, so this no longer reports
  100% drift on ``model``). COV-02: the same table also carries an
  ``effortLevel`` row per session once a dominant top-level effort is
  known (``_dominant_transcript_effort``) -- together these are the
  "CLI/overlay layer" the plan asks for: a disagreement between the
  snapshot's effective settings and what the transcript actually ran
  under is evidence of a shell env var, a ``--model``/``--effort`` CLI
  flag, or a ``--settings`` one-launch overlay the config hook never
  sees. ``snapshots.claude_json_cross_check`` is
  still not surfaced as a report table: unlike the other five functions
  fix #14 names, it has no existing ``build_*_table`` wrapper to reuse
  (only the raw dict-returning comparison), and it also needs a full
  per-session input/output/cache-token accumulation this section doesn't
  otherwise keep, joined against ``~/.claude.json``'s own
  ``lastSessionId`` — designing that table shape is a larger addition
  than this fix round covers; it remains reachable only via direct
  library use.
- ``allow_titles`` is accepted (matching the required signature) but is
  currently a no-op: nothing in ``model.py``/``parse.py``/``events.py``
  captures ``customTitle``/``ai-title`` line text anywhere, even
  conditionally (``events.py`` ignores both outright, unconditionally —
  see its ``_IGNORABLE_TYPES``). There is no title data for this module
  to gate. Wiring ``--allow-titles`` up would mean touching ``parse.py``
  and probably adding a field to ``TranscriptMeta``/``Event`` in
  ``model.py`` — both out of this work package's file list — so this is
  flagged here rather than silently implemented as a real gate.
- ``build_report`` has no ``config_dir`` parameter, so per-session
  ``sessions.toml`` overrides (``config.load_session_overrides``) cannot
  be resolved and loaded here; ``classify.classify_session`` is called
  with an empty ``overrides`` dict unless a caller passes its own
  ``session_overrides`` (see ``build_report``'s own docstring — WP10b
  added that parameter as the proposed fix for this). WP10-merge: ``cli.py``
  is now that caller — every ``report``/``sessions``/``recache``/``ttl``/
  ``compactions`` subcommand loads ``<config_dir>/sessions.toml`` via
  ``config.load_session_overrides(config_dir)`` and passes the result as
  ``session_overrides=``, so this deviation is closed for the CLI path;
  it remains true only for a caller of ``build_report`` that omits the
  keyword.
- v4 wiring round: ``build_report`` now *does* take a ``config_dir``
  keyword after all -- but only for ``waste.WasteStats``'s salted
  session-id hash (``waste.py``'s own ``load_or_create_salt``), which
  touches disk (creates/reads a salt file under ``config_dir``) unless
  told where to look, and otherwise defaults to the real
  ``~/.claude/claudeglass``. This is a narrower purpose than the
  ``session_overrides``-loading deviation two bullets above still
  describes -- ``build_report`` still doesn't *load*
  ``sessions.toml``/``usage-log.csv`` itself, only resolves the waste
  salt's directory. A caller that omits ``config_dir`` gets
  ``waste.compute_waste``'s own default (the real ``~/.claude`` salt
  file) -- every caller in this codebase (``cli.py``, ``service/api.py``)
  passes its own already-resolved ``config_dir`` explicitly.
"""

from __future__ import annotations

import dataclasses
import statistics
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import __version__ as _TOOL_VERSION
from . import (
    carry,
    classify,
    compaction,
    compaction_sim,
    context_budget,
    context_files,
    discovery,
    elasticity,
    fixes,
    habits,
    handoff,
    helptext,
    hook_costs,
    limits,
    model_swap,
    quality,
    recache,
    reconcile,
    run_split,
    scorecard,
    snapshots as snapshots_mod,
    tool_search,
    topology,
    ttl,
    units as units_mod,
    waste,
    workflows,
    workstyle,
)
from .config import Config
from .corpus import Corpus, SessionBundle
from .model import (
    Column,
    Diagnostics,
    EventKind,
    PricingMeta,
    ReportMeta,
    ReportModel,
    Section,
    SessionRecord,
    Table,
    TranscriptResult,
    Turn,
    WorkflowRun,
)
from .phases import PhaseStats
from .phases import build_section as build_phases_section
from .pricing import Pricing, PricingCoverage, price_turn
from .recommend import recommend
from .render.tables import format_cell
from .snapshots import Snapshot
from .tools import log_usage

#: Fixed section order (before ``include`` filtering). Matches the task
#: brief exactly, minus "diagnostics" (see module docstring).
_SECTION_ORDER: tuple[str, ...] = (
    "overview",
    "usage",
    "elasticity",
    "sessions",
    "recache",
    "ttl",
    "limits",
    "carry",
    "compaction_sim",
    "plan_handoff",
    "model_swap",
    "waste",
    "compactions",
    "agent_startup",
    "agents",
    "run_split",
    "hooks",
    "quality",
    "workstyle",
    "habits",
    "workflows",
    "phases",
    "config",
    "context_budget",
    "tool_search",
    "capture",
    "cost_record",
    "scorecard",
)

#: How many changed config keys get their own diff table in the "config"
#: section (see module docstring's deviation note) — a corpus tracked
#: over a long window against a churning config could otherwise produce
#: an unbounded number of tables.
_MAX_CONFIG_DIFF_KEYS = 20


def _capture_signals(corpus: Corpus, config_dir: str | Path | None) -> dict | None:
    """The free signals metrics capture logged under ``config_dir``, by
    session id; ``None`` without a config directory, a signals folder or
    the salt the hook hashed session ids with (never created here)."""
    if config_dir is None:
        return None
    from . import parse, signals

    try:
        if not signals.signals_dir(config_dir).is_dir() or not (Path(config_dir) / "salt").is_file():
            return None
        salt = parse.load_or_create_salt(config_dir)
        return signals.by_session(signals.load(config_dir), [b.session_id for b in corpus.sessions], salt)
    except (OSError, ValueError):
        return None


def _default_waste_config_dir() -> Path:
    """Where ``waste.WasteStats``'s salted session-id hash reads/writes
    its salt file when a caller of :func:`build_report` doesn't supply
    its own ``config_dir`` -- deliberately *not* ``waste.py``'s own
    default (the real ``~/.claude/claudeglass``, via
    ``parse.load_or_create_salt``'s own ``config_dir=None`` fallback).
    ``waste.WasteStats.__init__`` calls ``load_or_create_salt``
    unconditionally and eagerly (not lazily on first ``.add()``), so
    every existing caller of ``build_report`` that predates this v4
    wiring round and doesn't pass ``config_dir`` -- every test in this
    repo, plus ``baseline.py``'s/``team.py``'s own ``build_report()``
    call sites -- would otherwise silently create a file in the user's
    real Claude Code config directory the first time it builds a
    ``waste`` section, which this project's own privacy convention (and
    this wiring round's own brief) both rule out. The OS temp directory
    is used instead, under a fixed subdirectory so repeated calls within
    one run/process still hash session ids consistently.
    """
    return Path(tempfile.gettempdir()) / "claudeglass" / "waste-salt-default"


def _priced_turns(result: TranscriptResult) -> list[Turn]:
    """Turns that actually got a ``turn_index`` (excludes synthetic and
    missing-usage turns, but keeps an estimated compaction call, which is
    spend -- see ``parse.py``). Deliberately duplicated rather than imported —
    same one-line-helper convention ``workflows.py``/``phases.py``
    document in their own module docstrings.
    """
    return [t for t in result.turns if t.turn_index > 0]


def _transcripts_of(bundle: SessionBundle) -> list[TranscriptResult]:
    transcripts: list[TranscriptResult] = []
    if bundle.top is not None:
        transcripts.append(bundle.top)
    transcripts.extend(bundle.subs)
    return transcripts


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _dominant_transcript_model(tr: TranscriptResult) -> str | None:
    """The most-observed ``Turn.model`` across ``tr``'s own priced turns,
    ties broken lexicographically. ``None`` when nothing resolves (no
    priced turns, or none carried a model id) — the caller then resolves
    against ``None``, which prices at zero with ``model_known=False``
    rather than guessing a rate.
    """
    counts: dict[str, int] = {}
    for turn in _priced_turns(tr):
        if not turn.model:
            continue
        counts[turn.model] = counts.get(turn.model, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]


def _dominant_transcript_effort(tr: TranscriptResult) -> str | None:
    """COV-02: the most-observed ``Turn.effort`` across ``tr``'s own priced
    turns, ties broken lexicographically -- the effort-side counterpart to
    :func:`_dominant_transcript_model`, feeding the same "observed" side of
    :func:`snapshots.build_config_drift_table` under the ``effortLevel``
    key (the settings key it's compared against; see
    ``SAFE_SETTINGS_KEYS`` in ``hooks/snapshot-config.py``). ``turn.effort``
    already uses the same enum as ``effortLevel``
    (``low``/``medium``/``high``/``xhigh``/``max``, docs/profiles.md), so no
    alias normalisation is needed the way ``model`` needs
    ``pricing.resolve_model`` -- plain equality in ``detect_drift`` is
    correct here. ``None`` when nothing resolves (no priced turns, or none
    carried an effort value).
    """
    counts: dict[str, int] = {}
    for turn in _priced_turns(tr):
        if not turn.effort:
            continue
        counts[turn.effort] = counts.get(turn.effort, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]


# -- baseline comparison (v0.3 Task 2): metric extraction from an
# already-assembled sections list. These are the shared implementation
# ``baseline.py`` imports (rather than duplicating table-lookup logic)
# for its own baseline-record extraction -- ``baseline.py`` already
# imports :func:`build_report` from this module, so the dependency only
# ever runs one way (no import cycle). Every function here follows the
# same "never fabricate, only cite the report's own tables" convention
# ``baseline.py``'s module docstring documents: each reads a value
# straight out of a ``Section``/``Table`` this module (or another
# writable-surface module it calls) already built, never recomputing it
# independently. -------------------------------------------------------


def _section_table(sections: list[Section], section_key: str, table_name: str) -> Table | None:
    for section in sections:
        if section.key != section_key:
            continue
        for table in section.tables:
            if table.name == table_name:
                return table
    return None


def _col_index(table: Table, key: str) -> int | None:
    for index, column in enumerate(table.columns):
        if column.key == key:
            return index
    return None


def overview_metric(sections: list[Section], metric: str) -> float | None:
    """A single metric's value from the "overview" section's "totals"
    table -- a label-keyed table (``row[0]`` is the metric name, e.g.
    ``"total_cost_usd"``/``"sessions"``, ``row[1]`` its value), unlike
    most tables in this project which are column-key-based.
    """
    table = _section_table(sections, "overview", "totals")
    if table is None:
        return None
    for row in table.rows:
        if row[0] == metric:
            value = row[1]
            return float(value) if isinstance(value, (int, float)) else None
    return None


def recache_share_pct_metric(sections: list[Section]) -> float | None:
    """The corpus-wide re-cache cache-creation share, from the "recache"
    section's single-row "recache_summary" table."""
    table = _section_table(sections, "recache", "recache_summary")
    if table is None or not table.rows:
        return None
    index = _col_index(table, "recache_cc_share_pct")
    if index is None:
        return None
    value = table.rows[0][index]
    return float(value) if isinstance(value, (int, float)) else None


def compactions_per_session_metric(sections: list[Section]) -> float | None:
    """"Compactions per session (mean)" from the "compactions" section's
    "compactions_summary" table -- like the overview "totals" table,
    this one is label-keyed (``row[0]`` a literal English label, see
    ``compaction.build_section``), so the lookup matches that exact
    string rather than a ``Column.key``.
    """
    table = _section_table(sections, "compactions", "compactions_summary")
    if table is None:
        return None
    for row in table.rows:
        if row[0] == "Compactions per session (mean)":
            value = row[1]
            return float(value) if isinstance(value, (int, float)) else None
    return None


def ttl_mix_by_agent_type_metric(sections: list[Section]) -> dict[str, dict[str, float | None]]:
    """``{agent_type: {"5m_pct": ..., "1h_pct": ...}}`` from the "ttl"
    section's "ttl_by_agent_type" table, including the "top-level" row.
    """
    table = _section_table(sections, "ttl", "ttl_by_agent_type")
    if table is None:
        return {}
    agent_index = _col_index(table, "agent_type")
    pct5_index = _col_index(table, "observed_5m_pct")
    pct1h_index = _col_index(table, "observed_1h_pct")
    if agent_index is None or pct5_index is None or pct1h_index is None:
        return {}
    result: dict[str, dict[str, float | None]] = {}
    for row in table.rows:
        v5, v1h = row[pct5_index], row[pct1h_index]
        result[str(row[agent_index])] = {
            "5m_pct": float(v5) if isinstance(v5, (int, float)) else None,
            "1h_pct": float(v1h) if isinstance(v1h, (int, float)) else None,
        }
    return result


def session_baseline_size_metric(sections: list[Section]) -> float | None:
    """Mean top-level first-turn cache-creation ("session baseline"),
    from the "agents" section's single-row "topology_session_baseline"
    table."""
    table = _section_table(sections, "agents", "topology_session_baseline")
    if table is None or not table.rows:
        return None
    index = _col_index(table, "mean_baseline")
    if index is None:
        return None
    value = table.rows[0][index]
    return float(value) if isinstance(value, (int, float)) else None


def mean_spawn_write_by_agent_type_metric(sections: list[Section]) -> dict[str, float]:
    """``{agent_type: mean_write}`` from the "agents" section's
    "topology_spawn_write" table."""
    table = _section_table(sections, "agents", "topology_spawn_write")
    if table is None:
        return {}
    agent_index = _col_index(table, "agent_type")
    mean_index = _col_index(table, "mean_write")
    if agent_index is None or mean_index is None:
        return {}
    result: dict[str, float] = {}
    for row in table.rows:
        value = row[mean_index]
        if isinstance(value, (int, float)):
            result[str(row[agent_index])] = float(value)
    return result


def scorecard_dimensions_metric(sections: list[Section]) -> dict[str, int]:
    """``{dimension: level}`` from the "scorecard" section's "dimensions"
    table."""
    table = _section_table(sections, "scorecard", "dimensions")
    if table is None:
        return {}
    dim_index = _col_index(table, "dimension")
    level_index = _col_index(table, "level")
    if dim_index is None or level_index is None:
        return {}
    result: dict[str, int] = {}
    for row in table.rows:
        value = row[level_index]
        if isinstance(value, int):
            result[str(row[dim_index])] = value
    return result


#: Minimum sessions required in BOTH the baseline and the current window
#: for a per-mode baseline_comparison row to show real numbers rather
#: than "suppressed" -- same default ``compare.py`` uses for its own
#: arm-vs-arm stratification.
_BASELINE_MIN_SESSIONS = 5


def _baseline_delta_row(label: str, kind: str, baseline_value, current_value, currency: str) -> list:
    """One before/after ``baseline_comparison_overview`` row: label plus
    pre-formatted baseline/current/delta/delta-% text -- the same
    pre-formatted-``kind="str"``-cell convention ``compare.py``'s own
    ``_fmt_metric_row`` uses, for the same reason (see that module's
    docstring): the table stacks metrics of different ``Column.kind``s
    into one shared pair of columns.
    """
    baseline_str = format_cell(baseline_value, kind, currency)
    current_str = format_cell(current_value, kind, currency)
    if baseline_value is None or current_value is None:
        return [label, baseline_str, current_str, "-", "-"]
    delta_raw = current_value - baseline_value
    delta_str = ("+" if delta_raw > 0 else "") + format_cell(delta_raw, kind, currency)
    if baseline_value == 0:
        delta_pct_str = "n/a (baseline = 0)"
    else:
        pct = (current_value - baseline_value) / baseline_value * 100.0
        delta_pct_str = ("+" if pct > 0 else "") + f"{pct:.1f}%"
    return [label, baseline_str, current_str, delta_str, delta_pct_str]


def _pct_of_baseline(baseline_value, current_value) -> float | None:
    if baseline_value is None or current_value is None or baseline_value == 0:
        return None
    return (current_value - baseline_value) / baseline_value * 100.0


def _build_baseline_by_mode_table(
    baseline_mode_mix: dict[str, int],
    baseline_by_mode: dict[str, dict],
    current_by_mode: dict[str, dict],
) -> Table:
    modes = sorted(set(baseline_mode_mix) | set(current_by_mode))
    columns = [
        Column(key="mode", label="Mode", kind="str"),
        Column(key="sessions_baseline", label="Sessions (baseline)", kind="int"),
        Column(key="sessions_current", label="Sessions (current)", kind="int"),
        Column(key="sample_ok", label="Sample OK", kind="str"),
        Column(key="cost_per_session_baseline", label="Cost/session (baseline)", kind="money"),
        Column(key="cost_per_session_current", label="Cost/session (current)", kind="money"),
        Column(key="cost_per_session_delta_pct", label="Cost/session delta (% of baseline)", kind="pct"),
        Column(key="recache_share_baseline", label="Re-cache share (baseline)", kind="pct"),
        Column(key="recache_share_current", label="Re-cache share (current)", kind="pct"),
        Column(key="recache_share_delta_pct", label="Re-cache share delta (% of baseline)", kind="pct"),
        Column(key="compactions_per_session_baseline", label="Compactions/session (baseline)", kind="float"),
        Column(key="compactions_per_session_current", label="Compactions/session (current)", kind="float"),
        Column(key="compactions_per_session_delta_pct", label="Compactions/session delta (% of baseline)", kind="pct"),
        Column(key="note", label="Note", kind="str"),
    ]
    rows = []
    for mode in modes:
        baseline_n = baseline_mode_mix.get(mode, 0)
        current_stats = current_by_mode.get(mode, {})
        current_n = int(current_stats.get("sessions", 0))
        if baseline_n < _BASELINE_MIN_SESSIONS or current_n < _BASELINE_MIN_SESSIONS:
            rows.append(
                [
                    mode,
                    baseline_n,
                    current_n,
                    "no",
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    f"suppressed: needs >={_BASELINE_MIN_SESSIONS} session(s) in both the baseline "
                    f"and the current window (baseline has {baseline_n}, current has {current_n})",
                ]
            )
            continue
        baseline_stats = baseline_by_mode.get(mode, {})
        b_cost = baseline_stats.get("cost_per_session")
        c_cost = current_stats.get("cost_per_session")
        b_recache = baseline_stats.get("recache_share_pct")
        c_recache = current_stats.get("recache_share_pct")
        b_comp = baseline_stats.get("compactions_per_session")
        c_comp = current_stats.get("compactions_per_session")
        rows.append(
            [
                mode,
                baseline_n,
                current_n,
                "yes",
                b_cost,
                c_cost,
                _pct_of_baseline(b_cost, c_cost),
                b_recache,
                c_recache,
                _pct_of_baseline(b_recache, c_recache),
                b_comp,
                c_comp,
                _pct_of_baseline(b_comp, c_comp),
                "",
            ]
        )
    return Table(
        name="baseline_comparison_by_mode",
        title="Baseline comparison by mode",
        columns=columns,
        rows=rows,
        notes=[
            "Observed, not controlled -- see the overview table's own note.",
            f"Minimum sample: {_BASELINE_MIN_SESSIONS} session(s) required in both the baseline "
            "and the current window for a mode to show real numbers; below that only session "
            "counts are shown (see the Note column).",
            "Only cost per session, cache rebuild share and summaries per session are split by "
            "mode here. Cache lifetime mix, startup size, average startup write and scorecard "
            "levels stay across all sessions (see the overview table), because the figures they "
            "come from aren't kept per session.",
        ],
    )


def _build_baseline_comparison_section(
    baseline_record: dict,
    sections: list[Section],
    *,
    currency: str,
    current_by_mode: dict[str, dict[str, float | None]],
) -> Section:
    """The ``baseline_comparison`` section (v0.3 Task 2): a before/after
    table of ``baseline_record`` (as produced by
    :func:`~claudeglass.baseline.build_baseline`) against this same
    window's own already-assembled ``sections``, plus a per-mode
    breakdown table when the baseline recorded a mode mix.
    """
    rows: list[list] = []

    baseline_sessions = baseline_record.get("sessions_analysed") or 0
    baseline_cost_per_session = baseline_record.get("cost_per_session")
    current_sessions = overview_metric(sections, "sessions")
    current_total_cost = overview_metric(sections, "total_cost_usd")
    current_cost_per_session = (
        current_total_cost / current_sessions if current_sessions else None
    )
    rows.append(
        _baseline_delta_row(
            "Cost per session", "money", baseline_cost_per_session, current_cost_per_session, currency
        )
    )
    rows.append(
        _baseline_delta_row(
            "Re-cache share of cache-creation",
            "pct",
            baseline_record.get("recache_share_pct"),
            recache_share_pct_metric(sections),
            currency,
        )
    )
    rows.append(
        _baseline_delta_row(
            "Compactions per session",
            "float",
            baseline_record.get("compactions_per_session"),
            compactions_per_session_metric(sections),
            currency,
        )
    )
    rows.append(
        _baseline_delta_row(
            "Session baseline size (mean top-level first-turn cache-creation)",
            "tokens",
            baseline_record.get("session_baseline_size"),
            session_baseline_size_metric(sections),
            currency,
        )
    )

    baseline_ttl_top = baseline_record.get("ttl_mix_top_level") or {}
    current_ttl = ttl_mix_by_agent_type_metric(sections)
    current_ttl_top = current_ttl.get("top-level", {})
    rows.append(
        _baseline_delta_row(
            "TTL mix - top-level (5m share)",
            "pct",
            baseline_ttl_top.get("5m_pct"),
            current_ttl_top.get("5m_pct"),
            currency,
        )
    )
    rows.append(
        _baseline_delta_row(
            "TTL mix - top-level (1h share)",
            "pct",
            baseline_ttl_top.get("1h_pct"),
            current_ttl_top.get("1h_pct"),
            currency,
        )
    )

    baseline_ttl_agents = baseline_record.get("ttl_mix_by_agent_type") or {}
    agent_types = sorted(set(baseline_ttl_agents) | {k for k in current_ttl if k != "top-level"})
    for agent_type in agent_types:
        b = baseline_ttl_agents.get(agent_type, {})
        c = current_ttl.get(agent_type, {})
        rows.append(
            _baseline_delta_row(
                f"TTL mix - {agent_type} (5m share)", "pct", b.get("5m_pct"), c.get("5m_pct"), currency
            )
        )
        rows.append(
            _baseline_delta_row(
                f"TTL mix - {agent_type} (1h share)", "pct", b.get("1h_pct"), c.get("1h_pct"), currency
            )
        )

    baseline_spawn = baseline_record.get("mean_spawn_write_by_agent_type") or {}
    current_spawn = mean_spawn_write_by_agent_type_metric(sections)
    for agent_type in sorted(set(baseline_spawn) | set(current_spawn)):
        rows.append(
            _baseline_delta_row(
                f"Mean spawn write - {agent_type}",
                "tokens",
                baseline_spawn.get(agent_type),
                current_spawn.get(agent_type),
                currency,
            )
        )

    baseline_scorecard = baseline_record.get("scorecard_dimensions") or {}
    current_scorecard = scorecard_dimensions_metric(sections)
    for dimension in scorecard.ALL_DIMENSIONS:
        rows.append(
            _baseline_delta_row(
                f"Scorecard level - {dimension}",
                "int",
                baseline_scorecard.get(dimension),
                current_scorecard.get(dimension),
                currency,
            )
        )

    columns = [
        Column(key="metric", label="Metric", kind="str"),
        Column(key="baseline", label="Baseline", kind="str"),
        Column(key="current", label="Current", kind="str"),
        Column(key="delta", label="Delta", kind="str"),
        Column(key="delta_pct", label="Delta (% of baseline)", kind="str"),
    ]
    overview_table = Table(
        name="baseline_comparison_overview",
        title=f"This window vs your baseline {baseline_record.get('id', '?')}",
        columns=columns,
        rows=rows,
        notes=[
            "Observed, not controlled: the baseline and the current window are not a "
            "randomised experiment -- a difference may reflect a changed workload mix, "
            "not the effect of any config change made in between.",
            f"Baseline {baseline_record.get('id', '?')!r} captured "
            f"{baseline_record.get('created_at', '?')} over {baseline_sessions} session(s) "
            f"({baseline_record.get('window_days')!r} day window).",
        ],
    )

    tables = [overview_table]
    baseline_mode_mix = baseline_record.get("mode_mix") or {}
    if baseline_mode_mix:
        tables.append(
            _build_baseline_by_mode_table(baseline_mode_mix, baseline_record.get("by_mode") or {}, current_by_mode)
        )

    return Section(
        key="baseline_comparison",
        title="Baseline comparison",
        tables=tables,
        notes=["Observed, not controlled -- see each table's own notes for exactly what that means here."],
    )


def _recommend_min_sample_values(config: Config) -> tuple[int, int]:
    """The min-sample values ``recommend()`` actually gates recommendations
    on, for display in the report's thresholds header -- NOT
    ``config.min_sessions``/``config.min_turns`` directly.
    ``recommend.RecommendThresholds`` has its own ``min_sessions``/
    ``min_turns`` defaults, independently overridable via
    ``config.thresholds["recommend"]`` (a ``[thresholds.recommend]`` TOML
    table distinct from the top-level ``Config.min_sessions``/
    ``min_turns``), so printing the ``Config`` fields verbatim can show a
    stale number when a corpus's ``[thresholds.recommend]`` overrides
    them. Prefers ``recommend.effective_min_sample(th)`` when that
    function exists (a future recommend.py addition this module doesn't
    own and can't rely on), otherwise reads ``RecommendThresholds``'s own
    resolved fields directly; falls back to the ``Config`` fields only if
    ``recommend.RecommendThresholds`` itself isn't importable.
    """
    from . import recommend as recommend_mod

    recommend_th_cls = getattr(recommend_mod, "RecommendThresholds", None)
    if recommend_th_cls is None:
        return config.min_sessions, config.min_turns

    recommend_th = recommend_th_cls.from_config(
        config.thresholds.get("recommend") if isinstance(config.thresholds, dict) else None
    )

    effective_min_sample = getattr(recommend_mod, "effective_min_sample", None)
    if effective_min_sample is not None:
        result = effective_min_sample(recommend_th)
        if isinstance(result, tuple) and len(result) == 2:
            return result

    return recommend_th.min_sessions, recommend_th.min_turns


def _effort_mismatch_share_threshold(config: Config) -> float:
    """UX-3: the thinking-share percent ``habits.py``'s ``effort_fit``
    item gates on, resolved from the same ``[thresholds.recommend]``
    config the ``effort-mismatch`` rule reads (``recommend
    .RecommendThresholds.effort_mismatch_thinking_share_pct``) -- one
    shared effort threshold instead of an independent number in each
    module. Same defensive posture as ``_recommend_min_sample_values``
    above: falls back to the class default if ``recommend.py`` isn't
    importable."""
    from . import recommend as recommend_mod

    recommend_th_cls = getattr(recommend_mod, "RecommendThresholds", None)
    if recommend_th_cls is None:
        return 30.0

    recommend_th = recommend_th_cls.from_config(
        config.thresholds.get("recommend") if isinstance(config.thresholds, dict) else None, config
    )
    return recommend_th.effort_mismatch_thinking_share_pct


def _merge_diagnostics(acc: Diagnostics, d: Diagnostics) -> None:
    """Fold one transcript's :class:`Diagnostics` into the running
    corpus-wide total: sum every int counter, merge every dict counter
    key-by-key, OR every bool. The two ``pricing_*`` totals are left
    alone: :func:`build_report` sets them once after the loop.
    """
    acc.lines += d.lines
    acc.unparsable_lines += d.unparsable_lines
    acc.truncated_final_line = acc.truncated_final_line or d.truncated_final_line
    acc.assistant_lines += d.assistant_lines
    acc.distinct_turns += d.distinct_turns
    acc.synthetic_turns += d.synthetic_turns
    acc.turns_missing_usage += d.turns_missing_usage
    acc.ttl_sum_mismatch += d.ttl_sum_mismatch
    acc.late_duplicate_ids += d.late_duplicate_ids
    acc.oversized_lines += d.oversized_lines
    acc.trailing_events += d.trailing_events
    acc.replayed_lines += d.replayed_lines
    acc.copied_lines += d.copied_lines
    acc.compaction_calls += d.compaction_calls
    acc.compaction_calls_unsized += d.compaction_calls_unsized
    acc.timestamp_parse_failures += d.timestamp_parse_failures
    acc.pre_split_turns += d.pre_split_turns
    acc.limit_hits += d.limit_hits
    acc.limit_resumes += d.limit_resumes
    acc.agents_terminated += d.agents_terminated
    for key, value in d.ignored_line_types.items():
        acc.ignored_line_types[key] = acc.ignored_line_types.get(key, 0) + value
    for key, value in d.agent_settings.items():
        acc.agent_settings[key] = acc.agent_settings.get(key, 0) + value
    for key, value in d.modes.items():
        acc.modes[key] = acc.modes.get(key, 0) + value
    for key, value in d.attachment_catch_all.items():
        acc.attachment_catch_all[key] = acc.attachment_catch_all.get(key, 0) + value


def _merge_parser_notes(acc: dict[str, dict[str, int]], notes: dict[str, dict[str, int]]) -> None:
    """Fold one transcript's ``TranscriptResult.parser_notes`` into the
    running corpus-wide total, the same two-level dict-of-counters merge
    ``_merge_diagnostics`` above does for its own dict fields -- kept as
    a sibling function (not a ``Diagnostics`` field) per this phase's own
    brief: don't edit ``_merge_diagnostics``/``DIAGNOSTIC_LABELS``.
    """
    for note_key, counts in notes.items():
        bucket = acc.setdefault(note_key, {})
        for key, value in counts.items():
            bucket[key] = bucket.get(key, 0) + value


# -- workstyle feature extraction (no existing helper does this: see
# workstyle.py's own module docstring, "this module never reads a
# TranscriptResult ... directly") -------------------------------------


def _extract_workstyle_features(
    top: TranscriptResult, subs: list[TranscriptResult], workflow_runs: list[WorkflowRun]
) -> workstyle.SessionFeatures:
    top_level_models = tuple(sorted({t.model for t in _priced_turns(top) if t.model}))

    subagent_models: list[tuple[str | None, str | None]] = []
    for sub in subs:
        model = _dominant_transcript_model(sub)
        subagent_models.append((model, sub.meta.agent_model_alias))

    effort_turn_counts: dict[str, int] = {}
    for tr in [top, *subs]:
        for turn in _priced_turns(tr):
            if turn.effort:
                effort_turn_counts[turn.effort] = effort_turn_counts.get(turn.effort, 0) + 1

    plan_mode_seen = False
    plan_mode_exit_ts: str | None = None
    for event in top.events:
        if event.kind != EventKind.CACHE_SIGNAL:
            continue
        if event.subkind == "plan_mode":
            plan_mode_seen = True
        elif event.subkind == "plan_mode_exit" and plan_mode_exit_ts is None:
            plan_mode_exit_ts = event.ts

    top_tier = max((workstyle.model_tier(m) for m in top_level_models), default=-1)
    post_plan_lower_tier = False
    if plan_mode_seen and plan_mode_exit_ts:
        exit_dt = _parse_ts(plan_mode_exit_ts)
        if exit_dt is not None:
            for turn in _priced_turns(top):
                turn_dt = _parse_ts(turn.ts)
                if turn_dt is None or turn_dt <= exit_dt:
                    continue
                tier = workstyle.model_tier(turn.model)
                if tier != -1 and tier < top_tier:
                    post_plan_lower_tier = True
                    break
            if not post_plan_lower_tier:
                for sub in subs:
                    first_priced = next(iter(_priced_turns(sub)), None)
                    sub_dt = _parse_ts(first_priced.ts) if first_priced is not None else None
                    if sub_dt is None or sub_dt <= exit_dt:
                        continue
                    tier = workstyle.model_tier(_dominant_transcript_model(sub), sub.meta.agent_model_alias)
                    if tier != -1 and tier < top_tier:
                        post_plan_lower_tier = True
                        break

    top_level_tool_names = frozenset(
        name for turn in _priced_turns(top) for name in turn.tool_names
    )

    return workstyle.SessionFeatures(
        top_level_models=top_level_models,
        subagent_models=tuple(subagent_models),
        spawn_count=len(subs),
        has_workflow=bool(workflow_runs),
        effort_turn_counts=effort_turn_counts,
        plan_mode_seen=plan_mode_seen,
        post_plan_lower_tier=post_plan_lower_tier,
        top_level_tool_names=top_level_tool_names,
        agent_settings=dict(top.diagnostics.agent_settings),
    )


# -- overview -------------------------------------------------------------


@dataclasses.dataclass(slots=True)
class _ModelCell:
    turns: int = 0
    input_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0


@dataclasses.dataclass(slots=True)
class _OverviewAcc:
    sessions: int = 0
    top_level_transcripts: int = 0
    subagent_transcripts: int = 0
    workflow_runs: int = 0
    priced_turns: int = 0
    input_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0
    total_cost: float = 0.0
    cache_read_cost: float = 0.0
    by_model: dict[str, _ModelCell] = dataclasses.field(default_factory=dict)


def _build_overview_section(
    acc: _OverviewAcc,
    cache_economy_totals: dict,
    top_level_median_ctx: float | None,
    top_level_turns_ctx_ge_200k_pct: float | None,
) -> Section:
    usage_tokens = acc.input_tokens + acc.cache_creation_tokens + acc.cache_read_tokens + acc.output_tokens
    new_tokens = acc.input_tokens + acc.cache_creation_tokens + acc.output_tokens
    cache_read_cost_share = 100.0 * acc.cache_read_cost / acc.total_cost if acc.total_cost else None

    totals_table = Table(
        name="totals",
        title="Overview totals",
        columns=[
            Column(key="metric", label="Metric", kind="str"),
            Column(key="value", label="Value", kind="str"),
        ],
        rows=[
            ["sessions", acc.sessions],
            ["top_level_transcripts", acc.top_level_transcripts],
            ["subagent_transcripts", acc.subagent_transcripts],
            ["workflow_runs", acc.workflow_runs],
            ["priced_turns", acc.priced_turns],
            ["input_tokens", acc.input_tokens],
            ["cache_creation_tokens", acc.cache_creation_tokens],
            ["cache_read_tokens", acc.cache_read_tokens],
            ["output_tokens", acc.output_tokens],
            ["usage_tokens", usage_tokens],
            ["new_tokens", new_tokens],
            ["total_cost_usd", acc.total_cost],
            ["cache_read_cost_share_pct", cache_read_cost_share],
            ["cache_roi", cache_economy_totals.get("cache_roi", 0.0)],
            # Top-level-only (agent_type == "top-level") ctx stats -- see
            # _top_level_ctx_values's docstring for why subagent transcripts
            # are excluded. Added so the "long-context share of recent
            # top-level turns" verification anchor has a turn-count-basis,
            # top-level-only table to check against (a subagent's ctx runs
            # far larger and would otherwise skew this upward).
            ["top_level_median_ctx", top_level_median_ctx],
            ["top_level_turns_ctx_ge_200k_pct", top_level_turns_ctx_ge_200k_pct],
        ],
    )

    by_model_rows = [
        [
            model,
            cell.turns,
            cell.input_tokens,
            cell.cache_creation_tokens,
            cell.cache_read_tokens,
            cell.output_tokens,
            cell.cost,
        ]
        for model, cell in sorted(acc.by_model.items(), key=lambda kv: (-kv[1].cost, kv[0]))
    ]
    by_model_table = Table(
        name="by_model",
        title="Overview by model",
        columns=[
            Column(key="model", label="Model", kind="str"),
            Column(key="turns", label="Turns", kind="int"),
            Column(key="input_tokens", label="Input tokens", kind="tokens"),
            Column(key="cache_creation_tokens", label="Cache-creation tokens", kind="tokens"),
            Column(key="cache_read_tokens", label="Cache-read tokens", kind="tokens"),
            Column(key="output_tokens", label="Output tokens", kind="tokens"),
            Column(key="cost", label="Cost", kind="money"),
        ],
        rows=by_model_rows,
    )

    notes = []
    if acc.sessions == 0:
        notes.append("No sessions found in this window.")
    return Section(key="overview", title="Overview", tables=[totals_table, by_model_table], notes=notes)


# -- config section (see module docstring's deviation note) -------------


def _build_config_section(
    sessions_with_metrics: list[dict],
    snaps: list[Snapshot],
    *,
    sessions_with_observed: list[dict] | None = None,
    resolve_model=None,
) -> Section:
    changed_keys = sorted(snapshots_mod.diff_keys(snaps).keys())
    shown_keys = changed_keys[:_MAX_CONFIG_DIFF_KEYS]

    tables: list[Table] = []
    for key in shown_keys:
        diff_table = snapshots_mod.build_config_diff_table(sessions_with_metrics, snaps, key)
        rows = [[_stringify(row[0]), *row[1:]] for row in diff_table.rows]
        tables.append(
            Table(
                name=diff_table.name,
                title=diff_table.title,
                columns=diff_table.columns,
                rows=rows,
                notes=diff_table.notes,
            )
        )

    notes = []
    if not changed_keys:
        notes.append("No config key changed across the supplied snapshots in this window.")
    elif len(changed_keys) > _MAX_CONFIG_DIFF_KEYS:
        notes.append(
            f"Showing the first {_MAX_CONFIG_DIFF_KEYS} of {len(changed_keys)} changed "
            "config keys, alphabetically."
        )

    # Fix #14: build_effective_config_table/build_config_layers_table/
    # build_config_groups_table need no inputs beyond the snapshots
    # already supplied here, so wire them in directly -- previously
    # reachable only via snapshots.build_config_section(...,
    # include_effective=True), which this section deliberately doesn't
    # reuse (see module docstring). Fix #15: build_config_drift_table now
    # normalises a settings model *alias* against an observed full model
    # id via resolve_model before comparing, so this is no longer the
    # "100% drift on model" trap #15 describes.
    if snaps:
        tables.append(snapshots_mod.build_effective_config_table(snaps))
        tables.append(snapshots_mod.build_config_layers_table(snaps))
        # COV-09: gives recommend.py's env-var-lever rules a real, citable
        # table row (see build_env_levers_table's own docstring) -- read
        # from the same corpus-wide snapshot recommend() itself uses.
        tables.append(snapshots_mod.build_env_levers_table(snaps))
        tables.append(snapshots_mod.build_config_groups_table(snaps, sessions_with_metrics))
        if sessions_with_observed:
            tables.append(
                snapshots_mod.build_config_drift_table(sessions_with_observed, snaps, resolve_model=resolve_model)
            )

    return Section(key="config", title="Config", tables=tables, notes=notes)


def _stringify(value: object) -> str:
    if value is None:
        return "(unset)"
    return str(value)


# -- recache group-by breakdown ------------------------------------------


#: ``group_by`` values that must be keyed per-*transcript* rather than
#: per-*session* (see ``_transcript_key_lookup``'s docstring for why).
_TRANSCRIPT_GROUP_KEYS = frozenset({"agent", "model", "entrypoint"})


def _group_key_lookup(records: list[SessionRecord], group_by: str) -> Callable[[TranscriptResult], str]:
    groups = classify.group_sessions(records, group_by)
    label_by_session: dict[str, str] = {}
    for label, group_records in groups.items():
        for record in group_records:
            label_by_session[record.session_id] = label

    def _key(result: TranscriptResult) -> str:
        return label_by_session.get(result.meta.session_id, "unknown")

    return _key


def _transcript_key_lookup(group_by: str) -> Callable[[TranscriptResult], str]:
    """Per-*transcript* group-key lookup for ``group_by in
    _TRANSCRIPT_GROUP_KEYS`` (``"agent"``/``"model"``/``"entrypoint"``).

    ``_group_key_lookup`` labels every transcript in a session with that
    *session's* one dominant group (``classify.group_sessions`` computes a
    single label per :class:`SessionRecord`), so a subagent inherits its
    session's key rather than its own -- fine for ``mode``/``purpose``/
    ``project`` (genuinely session-level properties) but wrong for
    ``agent``/``model``/``entrypoint``, which vary *per transcript* within
    one session (e.g. a session that spawns both a ``claude-implementer``
    and a ``general-purpose`` subagent has no single "session agent type").
    This keys each transcript by its own ``TranscriptMeta.agent_type``,
    dominant model, or ``TranscriptMeta.entrypoint`` instead, matching how
    :meth:`recache.RecacheStats.add` itself derives ``agent_type`` for the
    ``recache_by_agent_type`` table (``result.meta.agent_type or
    "top-level"``) so ``recache_by_group`` (grouped by ``"agent"``) sums to
    the same per-agent-type totals as that table.
    """

    def _key(result: TranscriptResult) -> str:
        if group_by == "agent":
            return result.meta.agent_type or "top-level"
        if group_by == "model":
            return _dominant_transcript_model(result) or "unknown"
        if group_by == "entrypoint":
            return result.meta.entrypoint or "unknown"
        raise ValueError(f"not a transcript-keyed group_by: {group_by!r}")

    return _key


def _build_recache_section(
    stats: recache.RecacheStats, pricing: Pricing, th: recache.RecacheThresholds, group_by: str | None
) -> Section:
    section = recache.build_section(stats, pricing, th, group=None)
    if not group_by:
        return section

    base_columns = section.tables[0].columns
    group_rows = []
    for group_label in stats.groups():
        group_section = recache.build_section(stats, pricing, th, group=group_label)
        summary_row = group_section.tables[0].rows[0]
        group_rows.append([group_label, *summary_row])

    group_table = Table(
        name="recache_by_group",
        title=f"Re-cache summary by {group_by}",
        columns=[Column(key="group", label=group_by.capitalize(), kind="str"), *base_columns],
        rows=group_rows,
    )
    return dataclasses.replace(section, tables=[*section.tables, group_table])


def _apply_autocompact_pct_override(configured_window: int, snap: Snapshot) -> int:
    """COV-09 (P7a): ``CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`` (docs/en/env-vars.md)
    sets what percentage (1-100) of ``autoCompactWindow`` auto-compaction
    actually triggers at -- "the variable can't raise the threshold, so
    values above the default percentage are ignored", i.e. it only ever
    lowers the real trigger point below the configured window. The
    caller's ``snapshot_windows`` is "the autoCompactWindow each session
    actually ran under" (its own comment), so the override, when present
    and in range, has to scale ``configured_window`` down here for that
    to stay true -- ``compaction_sim.simulate_compaction_windows`` would
    otherwise anchor the "already observed compaction" baseline to a
    window the session never really compacted at. Returns
    ``configured_window`` unchanged when the override is absent, out of
    the documented 1-100 range, or not an int (a snapshot's
    ``env_numeric_caps`` only ever holds ints -- see
    hooks/snapshot-config.py's ``env_numeric_caps`` assembly -- but a
    hand-built test snapshot could pass anything).
    """
    pct = (snap.data.get("env_numeric_caps") or {}).get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE")
    if isinstance(pct, int) and not isinstance(pct, bool) and 1 <= pct <= 100:
        return int(configured_window * pct / 100)
    return configured_window


# -- build_report -----------------------------------------------------------


def build_report(
    corpus: Corpus,
    pricing: Pricing,
    config: Config,
    *,
    projects: tuple[str, ...],
    window: str,
    group_by: str | None = None,
    phases: bool = False,
    snapshots: list[Snapshot] | None = None,
    allow_titles: bool = False,
    include: set[str] | None = None,
    session_overrides: dict | None = None,
    usage_log_rows: list[dict] | None = None,
    baseline_record: dict | None = None,
    baseline_note: str | None = None,
    config_dir: str | Path | None = None,
    ratings: dict | None = None,
) -> ReportModel:
    """Assemble the whole :class:`ReportModel` for ``corpus``. See the
    module docstring for section order/keys and the deviations from the
    task brief this function documents rather than silently resolves.

    ``session_overrides`` (WP10b addition) is the ``sessions.toml``-shaped
    dict ``config.load_session_overrides`` produces (session id ->
    ``{"mode": ..., "purpose": ...}``), passed straight through to
    :func:`classify.classify_session`. ``build_report`` still has no
    ``config_dir`` parameter of its own (see the module docstring's
    deviation note above), so it still cannot *load* the overrides file
    itself -- a caller that wants overrides applied loads it via
    ``config.load_session_overrides`` and passes the result here.
    Defaults to ``{}`` when omitted, matching the previous hardcoded
    behaviour exactly.

    ``usage_log_rows`` (S1-exports addition) is the usage-log CSV's
    ground-truth trailing-column rows, e.g. from
    ``statusline.load_usage_log_ground_truth`` -- a caller (``cli.py``'s
    ``report`` command) loads ``<config_dir>/usage-log.csv`` when it
    exists and passes the result here. It reaches two places: forwarded
    unchanged to :func:`context_budget.build_section`'s own
    ``usage_log_rows`` parameter, and used to append a ``cache_ground_truth``
    table onto the ``usage`` section via ``dataclasses.replace`` -- the
    table builder lives in ``statusline.py``, not ``usage.py``, because
    ``usage.py`` is not writable for this work package (see
    ``statusline.build_cache_ground_truth_table``'s own docstring).
    Defaults to ``None`` (no rows), which behaves exactly as it did
    before this parameter existed.

    ``baseline_record``/``baseline_note`` (v0.3 Task 2 addition): when
    ``baseline_record`` is given (a dict shaped like
    ``baseline.build_baseline``'s own return value), a ``baseline_comparison``
    section is appended -- unconditionally, i.e. it bypasses ``include``
    filtering, because the plan explicitly wants ``--baseline`` "also
    honoured by the report-like subcommands" that otherwise restrict the
    model to one focused section; suppressing this section for those
    subcommands would make the flag silently do nothing there.
    ``baseline_note``, when given and ``baseline_record`` is ``None``
    (the caller asked for a baseline but none resolved), is appended to
    ``ReportMeta.assumptions`` instead of a section -- ``model.py``'s
    ``Diagnostics`` dataclass has no free-text notes field to put this in
    (see the module docstring's first deviation note), so ``assumptions``
    is the pragmatic substitute for "a note in Diagnostics says how to
    create one".

    ``config_dir`` (v4 wiring round addition -- see the module docstring's
    deviation note) is passed straight through to ``waste.WasteStats`` so
    the ``waste`` section's salted session-id hash reads/creates its salt
    file under this directory -- privacy scoping, not a data source. When
    omitted (``None``, the default), :func:`_default_waste_config_dir`'s
    OS-temp-directory location is used instead of ``waste.py``'s own
    default (the real ``~/.claude/claudeglass``) -- deliberately: every
    existing caller of ``build_report`` that predates this parameter
    (every test in this repo, ``baseline.py``, ``team.py``) omits it, and
    none of them should silently start writing into the user's real
    Claude Code config directory just because a ``waste`` section is now
    always part of the assembled report. It is also read (never written)
    for each session's ``profile_id`` (``snapshots.load_profile_marks``:
    the profile active at the session's start) and, under subscription
    billing, the usage log behind :func:`_report_units`. Without it,
    ``profile_id`` comes from the hook captures in ``snapshots``. The
    ``habits`` section also reads the free signals metrics capture logged
    there (``signals.load``), only when its salt already exists.

    ``ratings`` holds your dashboard ratings by session id
    (``Store.all_feedback``), for the ``habits`` section's outcomes; the
    CLI and the service pass them when the store has any.
    """
    from . import usage as usage_mod  # local import: avoids a cycle risk with any future usage<->report coupling
    from . import statusline as statusline_mod  # local import: same rationale as usage_mod above

    recache_th = recache.RecacheThresholds.from_config(config.thresholds)
    ttl_th = ttl.TtlThresholds.from_config(config.thresholds)
    limits_th = limits.LimitThresholds.from_config(config.thresholds)
    mode_thresholds, purpose_thresholds = classify.mode_and_purpose_thresholds_from_config(config.thresholds)
    scorecard_th = scorecard.ScorecardThresholds.from_config(
        config.thresholds.get("scorecard") if isinstance(config.thresholds, dict) else None
    )
    # v4 wiring round: each module's own from_config convention (see
    # each one's own docstring) -- carry/compaction_sim read config.thresholds
    # flat, model_swap/waste read a nested sub-key, exactly like
    # scorecard_th above.
    carry_th = carry.CarryThresholds.from_config(config.thresholds)
    compaction_sim_th = compaction_sim.CompactionSimThresholds.from_config(config.thresholds)
    model_swap_th = model_swap.ModelSwapThresholds.from_config(config.thresholds)
    waste_th = waste.WasteThresholds.from_config(config.thresholds)
    handoff_th = handoff.HandoffThresholds.from_config(config.thresholds)
    hooks_th = hook_costs.HookThresholds.from_config(config.thresholds)
    tool_search_th = tool_search.ToolSearchThresholds.from_config(config.thresholds)
    run_split_th = run_split.RunSplitThresholds.from_config(config.thresholds)

    session_overrides = session_overrides or {}
    # Which profile was active at each session's start: the config
    # directory's full record (hook captures, apply stamps, undone
    # applies) when there is one, else what the loaded snapshots record.
    profile_marks = (
        snapshots_mod.load_profile_marks(config_dir)
        if config_dir is not None
        else snapshots_mod.profile_marks_from_snapshots(snapshots or [])
    )

    session_records: list[SessionRecord] = []
    session_cost: dict[str, float] = {}
    #: Additive (project-filter work): each *redacted* project slug's
    #: total cost across every session in this corpus, accumulated
    #: alongside ``session_cost`` below from the same per-session totals
    #: -- the source ``meta.projects`` sorts by, so a caller never has to
    #: separately request ``usage.by_project`` (which sorts its own rows
    #: the same way, ``(-cost, slug)``) just to know which project cost
    #: the most in this window.
    project_cost: dict[str, float] = {}
    session_snapshot_key: dict[str, str] = {}
    session_cc_total: dict[str, int] = {}
    session_recache_cc: dict[str, int] = {}
    #: v0.3 Task 2: session -> classification.mode, so a baseline
    #: comparison can stratify cost/recache/compactions by mode without
    #: re-classifying anything -- populated from the same
    #: classify.classify_session() call the loop below already makes.
    session_mode: dict[str, str] = {}
    #: Fix #14/#15: the top-level transcript's own dominant model per
    #: session, for the config-drift table's "observed" side --
    #: deliberately top-level only (a subagent's own model is a separate
    #: question from "did this session's own settings take effect").
    session_observed_model: dict[str, str] = {}
    #: COV-02: same idea, for the top-level transcript's own dominant
    #: effort -- the "CLI/overlay layer" the plan asks for is this pair
    #: (observed model, observed effort) read back from the transcript and
    #: compared against the snapshot's effective settings; a disagreement
    #: is evidence of a shell env var, ``--model``/``--effort`` CLI flag,
    #: or ``--settings`` overlay the config hook can't see (see
    #: ``detect_drift``'s own docstring).
    session_observed_effort: dict[str, str] = {}
    all_workflow_runs: list[WorkflowRun] = []

    overview = _OverviewAcc()
    pricing_coverage = PricingCoverage()
    diagnostics = Diagnostics()
    #: Parser-signals addition (SURV-6/7, see model.py's module
    #: docstring): merged alongside, not inside, ``_merge_diagnostics``.
    parser_notes: dict[str, dict[str, int]] = {}

    rs = recache.RecacheStats(recache_th)
    ls = limits.LimitStats()
    ts = ttl.TtlStats()
    cs = compaction.CompactionStats()
    tp = topology.TopologyStats()
    cb = context_budget.ContextBudgetStats()
    cf = context_files.ContextFileStats()
    ph = PhaseStats() if phases else None
    # v4 wiring round: waste.WasteStats accumulates per-transcript like
    # ls/ts/cs above (mirrors that shape); carry/model_swap/compaction_sim
    # are instead pure, whole-corpus functions (see each one's own
    # docstring) that need every TranscriptResult at once, so this list
    # collects them across the loop below for a single post-loop call
    # each, rather than an incremental .add() per transcript.
    ws = waste.WasteStats(waste_th, config_dir=config_dir if config_dir is not None else _default_waste_config_dir())
    all_results: list[TranscriptResult] = []

    for bundle in corpus.sessions:
        top = bundle.top
        if top is None:
            continue
        subs = bundle.subs
        transcripts = _transcripts_of(bundle)
        # Review finding 6: redact a raw home-directory username out of
        # the slug here, inside the one function both the CLI and the
        # service's API call, so a project slug built from a real
        # filesystem path (``C:\Users\<name>\...``, ``/home/<name>/...``)
        # never surfaces a real username through either surface, and the
        # two print the identical (redacted) slug for the same session.
        slug = discovery.redact_slug(bundle.slug)

        classification = classify.classify_session(
            top,
            subs,
            session_overrides,
            config.tz,
            workflows=len(bundle.workflows),
            entrypoint=top.meta.entrypoint,
            mode_thresholds=mode_thresholds,
            purpose_thresholds=purpose_thresholds,
        )
        record = classify.build_session_record(top, subs, bundle.workflows, classification, slug)
        session_mode[record.session_id] = classification.mode
        record.project_key = snapshots_mod.snapshot_project_key(bundle.slug)
        record.profile_id = snapshots_mod.profile_for(record.first_ts, profile_marks, record.session_id)
        session_snapshot_key[record.session_id] = record.project_key

        features = _extract_workstyle_features(top, subs, bundle.workflows)
        archetype, _evidence = workstyle.detect_archetype(features)
        record.archetype = archetype
        session_records.append(record)
        all_workflow_runs.extend(bundle.workflows)

        overview.sessions += 1
        overview.top_level_transcripts += 1
        overview.subagent_transcripts += len(subs)
        overview.workflow_runs += len(bundle.workflows)

        session_cost_total = 0.0
        session_cc_total_tokens = 0
        session_recache_cc_tokens = 0

        for tr in transcripts:
            _merge_diagnostics(diagnostics, tr.diagnostics)
            _merge_parser_notes(parser_notes, tr.parser_notes)

            rs.add(tr, pricing.resolve_model)
            ls.add(tr, pricing.resolve_model)
            ts.add(tr, pricing.resolve_model, ttl_th)
            ws.add(tr, pricing)
            all_results.append(tr)
            cf.add(tr, pricing, is_main=tr is top)

            dominant_model = _dominant_transcript_model(tr)
            dominant_rate = pricing.resolve_model(dominant_model) if dominant_model else None
            cs.add_transcript(tr, dominant_rate, recache_th)

            if tr is top and dominant_model:
                session_observed_model[record.session_id] = dominant_model
            if tr is top:
                dominant_effort = _dominant_transcript_effort(tr)
                if dominant_effort:
                    session_observed_effort[record.session_id] = dominant_effort

            if ph is not None:
                ph.add_transcript(tr, pricing)

            flagged = recache.detect(tr.turns, recache_th)
            session_recache_cc_tokens += sum(t.cache_creation_tokens for t in flagged)

            for turn in _priced_turns(tr):
                resolved = pricing.resolve_model(turn.model)
                breakdown = price_turn(turn, resolved)
                pricing_coverage.add(turn, breakdown, resolved)

                # An estimated compaction call is spend, not a reply.
                overview.priced_turns += not turn.is_synthetic
                overview.input_tokens += turn.input_tokens
                overview.cache_creation_tokens += turn.cache_creation_tokens
                overview.cache_read_tokens += turn.cache_read_tokens
                overview.output_tokens += turn.output_tokens
                overview.total_cost += breakdown.total
                overview.cache_read_cost += breakdown.cache_read_cost

                model_key = turn.model or "<unknown>"
                cell = overview.by_model.setdefault(model_key, _ModelCell())
                cell.turns += not turn.is_synthetic
                cell.input_tokens += turn.input_tokens
                cell.cache_creation_tokens += turn.cache_creation_tokens
                cell.cache_read_tokens += turn.cache_read_tokens
                cell.output_tokens += turn.output_tokens
                cell.cost += breakdown.total

                session_cost_total += breakdown.total
                session_cc_total_tokens += turn.cache_creation_tokens

        tp.add_session(record.session_id, top, list(subs), pricing)
        cb.add_session(slug, top, raw_slug=bundle.slug)
        # The spawning turn's context size, joined by tool_use_id, lets
        # add_subagent tell a fork (which inherits that context) from a
        # fresh spawn.
        spawn_ctx = {
            tool_use_id: turn.ctx for tr in transcripts for turn in tr.turns for tool_use_id in turn.tool_use_ids
        }
        for sub in subs:
            cb.add_subagent(
                sub, spawn_ctx.get(sub.meta.tool_use_id) if sub.meta.tool_use_id else None, pricing
            )

        session_cost[record.session_id] = session_cost_total
        project_cost[slug] = project_cost.get(slug, 0.0) + session_cost_total
        session_cc_total[record.session_id] = session_cc_total_tokens
        session_recache_cc[record.session_id] = session_recache_cc_tokens

    if group_by:
        # "agent"/"model"/"entrypoint" vary per transcript within a
        # session (see _transcript_key_lookup's docstring — R5 fix);
        # everything else (mode/purpose/project/...) is a genuinely
        # session-level property, so it keeps the session-keyed lookup.
        if group_by in _TRANSCRIPT_GROUP_KEYS:
            rs.group_key = _transcript_key_lookup(group_by)
        else:
            rs.group_key = _group_key_lookup(session_records, group_by)
        # RecacheStats folds groups in during .add(); since grouping was
        # decided only after the fact (group_key needs every session
        # classified first), re-fold every transcript now that the
        # lookup is known.
        rs = recache.RecacheStats(recache_th, group_key=rs.group_key)
        for bundle in corpus.sessions:
            if bundle.top is None:
                continue
            for tr in _transcripts_of(bundle):
                rs.add(tr, pricing.resolve_model)

    # -- cache economy (overview's cache ROI): summed per-transcript, not
    # over one concatenated turns list -- ttl.normalize_ttl_split/
    # dominant_ttl work from a single transcript's own whole-turns view
    # (see ttl.py's docstring), so combining every transcript's turns
    # into one list first would misattribute pre-TTL-split writes to a
    # corpus-wide dominant side instead of each transcript's own. -------
    cache_economy_totals = {"write_usd": 0.0, "net_saving_usd": 0.0, "cache_roi": 0.0}
    total_write_usd = 0.0
    total_net_saving = 0.0
    for bundle in corpus.sessions:
        if bundle.top is None:
            continue
        for tr in _transcripts_of(bundle):
            econ = ttl.cache_economy(tr.turns, pricing.resolve_model, ttl_th)
            total_write_usd += econ["write_usd"]
            total_net_saving += econ["net_saving_usd"]
    cache_economy_totals["write_usd"] = total_write_usd
    cache_economy_totals["net_saving_usd"] = total_net_saving
    cache_economy_totals["cache_roi"] = total_net_saving / total_write_usd if total_write_usd > 0 else 0.0

    # -- top-level-only ctx stats (R7 + coordinator follow-up): computed
    # once here, off the final ``rs`` (post group-by re-fold, if any),
    # and reused by both the overview totals table and the scorecard's
    # context-hygiene dimension. See _top_level_ctx_values's docstring.
    top_level_ctx_values = _top_level_ctx_values(rs)
    top_level_median_ctx = statistics.median(top_level_ctx_values) if top_level_ctx_values else None
    top_level_turns_ctx_ge_200k_pct = (
        100.0 * sum(1 for c in top_level_ctx_values if c >= recache_th.huge_ctx) / len(top_level_ctx_values)
        if top_level_ctx_values
        else None
    )

    # -- v0.3 Task 2: current-window per-mode metrics for a
    # baseline_comparison's by-mode table -- built once here (cheap: it
    # only re-sums dicts the loop above already populated) regardless of
    # whether a baseline was actually requested, since the cost of
    # skipping it conditionally isn't worth the branch.
    current_by_mode: dict[str, dict[str, float | None]] = {}
    mode_sessions: dict[str, list[str]] = {}
    for session_id, mode in session_mode.items():
        mode_sessions.setdefault(mode, []).append(session_id)
    compaction_counts = {sid: count for sid, count, _dropped, _cost in cs.per_session_summary()}
    for mode, session_ids in mode_sessions.items():
        n = len(session_ids)
        total_cost = sum(session_cost.get(sid, 0.0) for sid in session_ids)
        total_cc = sum(session_cc_total.get(sid, 0) for sid in session_ids)
        total_recache_cc = sum(session_recache_cc.get(sid, 0) for sid in session_ids)
        total_compactions = sum(compaction_counts.get(sid, 0) for sid in session_ids)
        current_by_mode[mode] = {
            "sessions": n,
            "cost_per_session": (total_cost / n) if n else None,
            # 0.0 (not None) on a zero-cache_creation denominator, matching
            # recache.py's own _pct() zero-denominator convention -- so a
            # mode with genuinely no cache-creation tokens reads as "0.0%"
            # rather than the misleadingly stronger "no data" of "-".
            "recache_share_pct": (100.0 * total_recache_cc / total_cc) if total_cc > 0 else 0.0,
            "compactions_per_session": (total_compactions / n) if n else None,
        }

    # -- v4 wiring round: carry/model_swap/compaction_sim are pure,
    # whole-corpus functions (see each one's own docstring) run once here
    # against ``all_results`` (every transcript the main loop above
    # collected), rather than an incremental per-transcript .add() --------

    carry_stats = carry.compute_carry(all_results, pricing.resolve_model, carry_th)
    model_swap_stats = model_swap.compute_model_swap(all_results, pricing, model_swap_th)

    # snapshot_windows: dict[session_id, int | None] -- the
    # autoCompactWindow each session actually ran under: the snapshot its
    # own project had when it started (``snapshot_for`` with the hashed
    # project key, so another project's settings never leak in).
    snapshot_windows: dict[str, int | None] = {}
    for record in session_records:
        snap = (
            snapshots_mod.snapshot_for(record.first_ts, snapshots, session_snapshot_key.get(record.session_id))
            if snapshots
            else None
        )
        configured_window = None
        if snap is not None:
            # CLAUDE_CODE_AUTO_COMPACT_WINDOW, when set, beats the setting.
            value = snapshots_mod.auto_compact_window(snap)
            if value is not None:
                configured_window = _apply_autocompact_pct_override(value, snap)
        snapshot_windows[record.session_id] = configured_window

    compaction_sim_stats = compaction_sim.simulate_compaction_windows(
        all_results, pricing.resolve_model, snapshot_windows, compaction_sim_th
    )
    # A fresh start re-reads files the way a summary does: the same
    # allowance, measured from this corpus's real summaries.
    handoff_stats = handoff.compute_handoff(
        all_results, pricing, handoff_th, compaction_sim_stats.rediscovery_allowance_usd
    )
    run_split_stats = run_split.compute_run_split(
        all_results, pricing, run_split_th, compaction_sim_stats.rediscovery_allowance_usd
    )
    hook_stats = hook_costs.compute_hook_costs(all_results, pricing, hooks_th)
    tool_search_stats = tool_search.compute_tool_search(all_results, pricing, tool_search_th)

    # How amounts are phrased (billing mode, and under subscription the
    # usage-limit fit). Built before the sections: the elasticity section
    # renders the same fit.
    units = _report_units(corpus, pricing, config, config_dir)

    # -- assemble sections ---------------------------------------------

    sections: list[Section] = []

    def _want(key: str) -> bool:
        return include is None or key in include

    if _want("overview"):
        sections.append(
            _build_overview_section(
                overview, cache_economy_totals, top_level_median_ctx, top_level_turns_ctx_ge_200k_pct
            )
        )

    if _want("usage"):
        usage_section = usage_mod.build_section(corpus, pricing, config)
        if usage_log_rows:
            # S1-exports: usage.py itself is not writable for this work
            # package, so the cache_ground_truth table is appended here
            # via dataclasses.replace rather than added inside
            # usage.build_section -- the same pattern _build_recache_section
            # already uses above to bolt an extra table onto a Section it
            # doesn't own the construction of.
            cache_table = statusline_mod.build_cache_ground_truth_table(usage_log_rows)
            usage_section = dataclasses.replace(usage_section, tables=[*usage_section.tables, cache_table])
        if pricing_coverage.unknown:
            # Replies from models pricing.toml doesn't list: named here so
            # the pricing-coverage recommendation can say which to add.
            usage_section = dataclasses.replace(
                usage_section, tables=[*usage_section.tables, pricing_coverage.as_table()]
            )
        if pricing_coverage.closest_matches:
            # Replies priced by closest (prefix) match rather than their
            # own pricing.toml row: visible here so 100% coverage doesn't
            # read as "every model has its own price" (fix 2).
            usage_section = dataclasses.replace(
                usage_section,
                tables=[*usage_section.tables, pricing_coverage.as_closest_match_table()],
            )
        if pricing_coverage.fast_priced_as_standard:
            # Fast-flagged replies priced at standard rate for lack of a
            # [.fast] table (fix 3).
            usage_section = dataclasses.replace(
                usage_section,
                tables=[*usage_section.tables, pricing_coverage.as_fast_priced_as_standard_table()],
            )
        if pricing_coverage.fast_applied:
            # PROF-08: the mirror image above -- replies actually priced
            # at a fast-mode rate, and what they'd have cost standard.
            # whatif._fast_mode reads this table for the fastMode lever.
            usage_section = dataclasses.replace(
                usage_section,
                tables=[*usage_section.tables, pricing_coverage.as_fast_applied_table()],
            )
        sections.append(usage_section)

    if units.elasticity is not None and _want("elasticity"):
        sections.append(elasticity.build_section(units.elasticity))

    if _want("sessions"):
        sections.append(classify.build_section(session_records, mode_thresholds))

    if _want("recache"):
        recache_section = _build_recache_section(rs, pricing, recache_th, group_by)
        # Claude Code's own diagnosis of the main session's cache misses
        # (statusline ``prompt_cache``), next to the inferred causes.
        measured = statusline_mod.build_measured_miss_causes_table(usage_log_rows)
        if measured is not None:
            recache_section = dataclasses.replace(recache_section, tables=[*recache_section.tables, measured])
        sections.append(recache_section)

    if _want("ttl"):
        sections.append(ttl.build_section(ts, billing_mode=config.billing, thresholds=ttl_th))

    # Computed once, reused by both the limits cross-check below and the
    # habits section further down -- signals.by_session() re-reads and
    # re-joins the same signal files either way, so this avoids doing it
    # twice per report.
    capture_signals = _capture_signals(corpus, config_dir)

    if _want("limits"):
        limits_section = limits.build_section(ls, pricing, limits_th)
        extra_tables = []
        if usage_log_rows:
            # Same dataclasses.replace-a-table-on pattern the "usage"
            # section above uses for cache_ground_truth: csv_cross_check
            # needs the already-loaded usage-log rows, which this
            # module doesn't otherwise keep.
            extra_tables.append(limits.csv_cross_check(usage_log_rows, ls, limits_th))
        if capture_signals:
            # SIG-2: the free "waits"/"turn_signals" signals cross-check
            # the same transcript-derived hit count, independent of the
            # usage-log.csv the block above needs.
            extra_tables.append(limits.signals_cross_check(capture_signals, ls))
        if extra_tables:
            limits_section = dataclasses.replace(limits_section, tables=[*limits_section.tables, *extra_tables])
        sections.append(limits_section)

    if _want("carry"):
        sections.append(carry.build_section(carry_stats, carry_th))

    if _want("compaction_sim"):
        sections.append(compaction_sim.build_section(compaction_sim_stats, compaction_sim_th, units=units))

    if _want("plan_handoff"):
        sections.append(handoff.build_section(handoff_stats, handoff_th))

    if _want("model_swap"):
        sections.append(model_swap.build_section(model_swap_stats, model_swap_th, units=units))

    if _want("waste"):
        sections.append(waste.build_section(ws, waste_th))

    if _want("compactions"):
        sections.append(compaction.build_section(cs))

    if _want("agent_startup"):
        sections.append(context_budget.build_startup_section(cb))

    if _want("agents"):
        sections.append(topology.build_section(tp))

    if _want("run_split"):
        sections.append(run_split.build_section(run_split_stats, run_split_th))

    if _want("hooks"):
        sections.append(hook_costs.build_section(hook_stats, hooks_th))

    if _want("quality"):
        sections.append(quality.build_section(quality.corpus_runs(corpus, pricing), units=units))

    if _want("workstyle"):
        sections.append(workstyle.build_section(session_records))

    # Perf (S5/ROB-P3): collect() walks the whole corpus, so build it once
    # here and pass it to both the "habits" and "capture" sections below
    # instead of each calling habits.build_section()/capture_section()
    # with their own independent collect() pass over the same corpus.
    _habits_built: habits.Habits | None = None
    if _want("habits"):
        _habits_built = habits.collect(
            corpus, pricing, ratings=ratings, signals=capture_signals,
            effort_share_threshold_pct=_effort_mismatch_share_threshold(config),
        )
        sections.append(habits.section_from(_habits_built, model_swap=model_swap_stats))

    if _want("workflows"):
        sections.append(workflows.build_section(all_workflow_runs))

    if phases and ph is not None and _want("phases"):
        sections.append(build_phases_section(ph))

    if snapshots and _want("config"):
        sessions_with_metrics = [
            {
                "session_id": record.session_id,
                "first_ts": record.first_ts,
                "project_key": session_snapshot_key.get(record.session_id),
                "turns": len(record.top.turns) + sum(len(s.turns) for s in record.subs) if record.top else 0,
                "cost": session_cost.get(record.session_id, 0.0),
                "recache_cc": session_recache_cc.get(record.session_id, 0),
                "cc_total": session_cc_total.get(record.session_id, 0),
                "compactions": next(
                    (count for sid, count, _dropped, _cost in cs.per_session_summary() if sid == record.session_id),
                    0,
                ),
                "span_s": record.span_s,
            }
            for record in session_records
        ]
        sessions_with_observed = [
            {
                "session_id": record.session_id,
                "first_ts": record.first_ts,
                "project_key": session_snapshot_key.get(record.session_id),
                "observed": {
                    **(
                        {"model": session_observed_model[record.session_id]}
                        if record.session_id in session_observed_model
                        else {}
                    ),
                    # COV-02: effort's own key on the "observed" dict is
                    # "effortLevel" (not "effort") to match the settings
                    # key detect_drift compares it against -- see
                    # SAFE_SETTINGS_KEYS/effective_config.
                    **(
                        {"effortLevel": session_observed_effort[record.session_id]}
                        if record.session_id in session_observed_effort
                        else {}
                    ),
                },
            }
            for record in session_records
            if record.session_id in session_observed_model or record.session_id in session_observed_effort
        ]
        sections.append(
            _build_config_section(
                sessions_with_metrics,
                snapshots,
                sessions_with_observed=sessions_with_observed,
                resolve_model=pricing.resolve_model,
            )
        )

    if _want("context_budget"):
        sections.append(
            context_budget.build_section(cb, snapshots=snapshots, usage_log_rows=usage_log_rows, pricing=pricing)
        )

    if _want("tool_search"):
        sections.append(tool_search.build_section(tool_search_stats, tool_search_th))

    if _want("capture"):
        sections.append(habits.capture_section(corpus, pricing, config.capture, ratings=ratings, h=_habits_built))

    if _want("cost_record"):
        sections.append(reconcile.build_cost_record_section(reconcile.claude_code_reported_costs(corpus, pricing)))

    if _want("scorecard"):
        sections.append(_build_scorecard_section(rs, ls, ts, tp, cs, pricing_coverage, diagnostics, session_records, snapshots, config, scorecard_th, pricing))

    if baseline_record is not None:
        # Deliberately not gated by _want()/include -- see build_report's
        # own docstring for why --baseline must keep working on the
        # focused report-like subcommands too.
        sections.append(
            _build_baseline_comparison_section(
                baseline_record, sections, currency=pricing.currency, current_by_mode=current_by_mode
            )
        )

    # -- meta -------------------------------------------------------------

    recommend_min_sessions, recommend_min_turns = _recommend_min_sample_values(config)
    thresholds_dict: dict = {
        "recache": {
            "ctx_floor": recache_th.ctx_floor,
            "cr_ratio": recache_th.cr_ratio,
            "full_expiry_cr": recache_th.full_expiry_cr,
            "huge_ctx": recache_th.huge_ctx,
        },
        "ttl": dataclasses.asdict(ttl_th) if dataclasses.is_dataclass(ttl_th) else {},
        "classify_mode": mode_thresholds,
        "classify_purpose": purpose_thresholds,
        # The min-sample gate recommend() actually applies, NOT
        # config.min_sessions/config.min_turns directly -- see
        # _recommend_min_sample_values's docstring (min-sample header fix).
        "min_sessions": recommend_min_sessions,
        "min_turns": recommend_min_turns,
    }

    assumptions: list[str] = (
        list(ttl.ASSUMPTIONS)
        + list(recache.ASSUMPTIONS)
        + list(limits.ASSUMPTIONS)
        + list(carry.ASSUMPTIONS)
        + list(compaction_sim.ASSUMPTIONS)
        + list(handoff.ASSUMPTIONS)
        + list(run_split.ASSUMPTIONS)
        + list(hook_costs.ASSUMPTIONS)
        + list(tool_search.ASSUMPTIONS)
        + list(model_swap.ASSUMPTIONS)
        + list(waste.ASSUMPTIONS)
        + list(quality.ASSUMPTIONS)
    )
    if units.elasticity is not None:
        assumptions.extend(elasticity.ASSUMPTIONS)
    if baseline_record is None and baseline_note:
        assumptions.append(baseline_note)

    meta = ReportMeta(
        tool_version=_TOOL_VERSION,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + "000Z",
        window=window,
        # Finding 6: `projects` is caller-supplied (the CLI's own
        # discovered slugs, or api.py's `_build_report_model`'s
        # store-derived set) -- redact defensively here too, not only at
        # the `bundle.slug` call sites above, so a caller that hasn't
        # redacted its own list can't leak a raw slug through this field.
        # Project-filter addition: sorted by this window's cost
        # (descending), ties broken alphabetically -- the same
        # ``(-cost, slug)`` key ``usage.by_project`` already sorts its own
        # rows by, so the highest-spend project a caller could filter to
        # always sorts first. A project with no session in this window
        # (the CLI's ``--all-projects`` can pass one that exists on disk
        # but never appears in ``corpus.sessions``) has no entry in
        # ``project_cost`` and sinks to the bottom, alphabetically among
        # its own kind.
        projects=tuple(
            sorted(
                {discovery.redact_slug(p) for p in projects},
                key=lambda slug: (-project_cost.get(slug, 0.0), slug),
            )
        ),
        pricing=PricingMeta(
            path=pricing.path,
            version=pricing.version,
            sha8=pricing.sha8,
            currency=pricing.currency,
            coverage_pct=pricing_coverage.coverage_pct,
        ),
        thresholds=thresholds_dict,
        billing_mode=config.billing,
        billing_source=config.billing_source,
        amounts_basis=units.basis(),
        assumptions=assumptions,
        units={
            "mode": units.billing_mode,
            "share_per_usd": (
                elasticity.express_in_window(1.0, units.elasticity) if units.elasticity is not None else None
            ),
            "period_label": "weekly usage limit",
            "basis": units.basis(),
        },
        rates=pricing.rates_meta(),
    )

    # Fixes 2/3: these two counters are pricing-time totals (every turn
    # has to be resolved and priced first), not something any single
    # transcript's own Diagnostics ever carries, so they're set once here
    # from the finished PricingCoverage accumulator rather than merged
    # per-transcript like every other Diagnostics field above.
    diagnostics.pricing_closest_match_turns = pricing_coverage.closest_match_turns
    diagnostics.pricing_fast_priced_as_standard_turns = pricing_coverage.fast_priced_as_standard_turns

    report_model = ReportModel(
        meta=meta,
        sections=sections,
        recommendations=[],
        diagnostics=diagnostics,
        context_files=cf.to_dict(),
        parser_notes=parser_notes,
    )

    # WP10b: recommendations are computed from the already-assembled
    # report (see recommend.py's module docstring for why it works from
    # rendered tables rather than the raw accumulators above), using the
    # corpus's majority archetype and the latest config snapshot (if any)
    # as of "now" -- a per-session snapshot join is not attempted here,
    # matching how ``_build_config_section``/``_build_scorecard_section``
    # already treat ``snapshots`` as a single corpus-wide input. Its agents
    # are widened to every project's, since the newest snapshot records
    # only the agents of the project it was taken in.
    corpus_archetype, _archetype_evidence = workstyle.corpus_archetype(session_records)
    latest_snapshot = snapshots_mod.with_every_project_agents(snapshots) if snapshots else None
    report_model.units = units
    report_model.recommendations = recommend(
        report_model,
        config=config,
        archetype=corpus_archetype,
        snapshot=latest_snapshot,
        units=units,
    )

    # EST-P7 + CAP-3: what the habits section buys recommend() -- run it
    # again on a variant with that section stripped and diff by (id,
    # agent_type, lever); recommend() doesn't read the "capture" section
    # (only "habits"), so this second run is otherwise identical. The
    # capture section, already in `sections` (mutated in place, so
    # report_model.sections sees it too), gets its habit_value/held_back
    # patched with the result.
    if _want("capture") and _want("habits"):
        without_habits_model = dataclasses.replace(
            report_model, sections=[s for s in sections if s.key != "habits"], recommendations=[],
        )
        without_habits_recs = recommend(
            without_habits_model,
            config=config,
            archetype=corpus_archetype,
            snapshot=latest_snapshot,
            units=units,
        )
        for i, section in enumerate(sections):
            if section.key == "capture":
                sections[i] = habits.patch_capture_recommend_value(
                    section, corpus, pricing, config.capture, ratings=ratings,
                    with_habits=report_model.recommendations, without_habits=without_habits_recs,
                    h=_habits_built,
                )
                break

    fixes.attach_fixes(report_model.recommendations)
    # UX-3: a habit covered by a rule that fired here shouldn't report
    # its own saving too -- see habits.COVERED_BY. Also after recommend()
    # runs, for the same reason: which rules fired isn't known before it.
    habits.apply_covered_by(report_model)
    # Display copy last: it never touches table names, column keys or row
    # values, so recommend() above sees exactly what the builders emitted.
    helptext.annotate(report_model)

    return report_model


def _report_units(corpus: Corpus, pricing: Pricing, config: Config, config_dir) -> units_mod.Units:
    """How amounts are phrased for this report's billing mode. For a
    subscription with a usage log, fit how much of the weekly limit a
    list-price dollar is worth (``elasticity``, with ``config.toml``'s
    ``[thresholds.elasticity]``); the fit refuses itself when there are
    too few readings, and amounts fall back to list-price equivalents.
    The same fit is rendered as the ``elasticity`` section."""
    fitted = None
    if config.billing == "subscription" and config_dir is not None:
        usage_rows = log_usage.load_usage_log(Path(config_dir) / "usage-log.csv")
        if usage_rows:
            results = [tr for bundle in corpus.sessions for tr in ([bundle.top] if bundle.top else []) + bundle.subs]
            fitted = elasticity.compute_elasticity(
                usage_rows, results, pricing, elasticity.ElasticityThresholds.from_config(config.thresholds)
            )
    return units_mod.Units(billing_mode=config.billing, currency=pricing.currency, elasticity=fitted)


def _top_level_ctx_values(rs: recache.RecacheStats) -> list[int]:
    """Sorted ``ctx`` values from top-level-only turns in ``rs.records``
    (R7 fix): ``recache.RecacheStats.add`` stamps every record's
    ``agent_type`` as ``result.meta.agent_type or "top-level"`` (see
    ``recache.py``), so filtering to ``"top-level"`` here excludes every
    subagent transcript. A subagent's own ctx runs far larger than its
    parent's (subagents typically start from a large system-prompt/task
    payload) and would otherwise skew both the scorecard's
    context-hygiene dimension and the overview's long-context-share
    metric upward, hiding an actually-healthy top-level session behind
    its subagents' naturally bigger context windows.
    """
    return sorted(r.turn.ctx for r in rs.records if r.agent_type == "top-level" and r.turn.ctx)


def _recache_shares(all_turns: list[Turn]) -> tuple[float | None, float | None]:
    """``(recache_share_pct, limit_recache_share_pct)``: rebuilt cache
    writes as a share of all cache writes, and the part of that share a
    usage-limit pause forced (limits.py) rather than a workflow choice --
    excluded from the cache_efficiency level rather than scored as one
    (see scorecard.ScorecardInputs.limit_recache_share_pct's docstring).
    Only rebuilds count towards the second: a turn after a limit pause
    whose cache survived is not part of the first either."""
    total_cc_all = sum(t.cache_creation_tokens for t in all_turns)
    if not total_cc_all:
        return None, None
    recache_turns = [t for t in all_turns if t.is_recache]
    total_cc_recache = sum(t.cache_creation_tokens for t in recache_turns)
    total_cc_limit = sum(t.cache_creation_tokens for t in recache_turns if t.recache_signature == "limit-expiry")
    return 100.0 * total_cc_recache / total_cc_all, 100.0 * total_cc_limit / total_cc_all


#: The context window ``ScorecardThresholds.context_p90_ctx``'s literal
#: defaults were tuned for (D2/COV-12): scaled against whatever a
#: corpus's own model actually resolves to, below.
_SCORECARD_DEFAULT_CONTEXT_WINDOW = 200_000


def _representative_context_window(all_turns: list[Turn], pricing: Pricing) -> int:
    """The context-window size (COV-12) of the most-used resolvable model
    among ``all_turns`` -- used to scale ``scorecard.py``'s
    ``context_p90_ctx`` thresholds (D2), which were sized for a
    200k-window model and otherwise unfairly score a corpus run on a
    natively 1M-window model (V24) as having "poor" context hygiene just
    for using the window it actually has. Falls back to
    :data:`_SCORECARD_DEFAULT_CONTEXT_WINDOW` when nothing resolves.
    """
    counts: dict[str, int] = {}
    for t in all_turns:
        if t.model:
            counts[t.model] = counts.get(t.model, 0) + 1
    for model in sorted(counts, key=lambda m: counts[m], reverse=True):
        resolved = pricing.resolve_model(model)
        if resolved is not None:
            return resolved.rates.context_window_tokens
    return _SCORECARD_DEFAULT_CONTEXT_WINDOW


def _build_scorecard_section(
    rs: recache.RecacheStats,
    ls: limits.LimitStats,
    ts: ttl.TtlStats,
    tp: topology.TopologyStats,
    cs: compaction.CompactionStats,
    pricing_coverage: PricingCoverage,
    diagnostics: Diagnostics,
    session_records: list[SessionRecord],
    snaps: list[Snapshot] | None,
    config: Config,
    th: scorecard.ScorecardThresholds,
    pricing: Pricing,
) -> Section:
    all_turns = [r.turn for r in rs.records]
    # D2/COV-12: only scale when the caller hasn't already customised
    # context_p90_ctx via [thresholds.scorecard] -- an explicit override
    # is respected as-is, same convention as every other *_th.from_config
    # value in this module.
    if th.context_p90_ctx == scorecard.ScorecardThresholds().context_p90_ctx:
        window = _representative_context_window(all_turns, pricing)
        if window and window != _SCORECARD_DEFAULT_CONTEXT_WINDOW:
            scale = window / _SCORECARD_DEFAULT_CONTEXT_WINDOW
            th = dataclasses.replace(th, context_p90_ctx=tuple(v * scale for v in th.context_p90_ctx))
    total_cc_all = sum(t.cache_creation_tokens for t in all_turns)
    recache_share_pct, limit_recache_share_pct = _recache_shares(all_turns)

    total_read = sum(t.cache_read_tokens for t in all_turns)
    total_input = sum(t.input_tokens for t in all_turns)
    hit_denom = total_read + total_cc_all + total_input
    cache_hit_ratio_pct = 100.0 * total_read / hit_denom if hit_denom else None

    # Top-level-only (excludes subagent transcripts) -- see
    # _top_level_ctx_values's docstring. Was previously built from
    # ``all_turns`` (every transcript, top-level and subagent alike).
    top_level_ctx = _top_level_ctx_values(rs)
    median_ctx = statistics.median(top_level_ctx) if top_level_ctx else None
    p90_ctx = None
    if top_level_ctx:
        idx = max(0, min(len(top_level_ctx) - 1, int(round(0.9 * (len(top_level_ctx) - 1)))))
        p90_ctx = float(top_level_ctx[idx])

    has_spawns = tp.total_spawns > 0
    agent_variance = None
    if has_spawns:
        per_type_mean = {
            agent_type: statistics.mean(costs)
            for agent_type, costs in tp.cost_by_agent_type.items()
            if costs
        }
        if per_type_mean:
            median_cost = statistics.median(per_type_mean.values())
            max_cost = max(per_type_mean.values())
            agent_variance = max_cost / median_cost if median_cost else None

    has_snapshot = bool(snaps)
    changed_keys = len(snapshots_mod.diff_keys(snaps)) if snaps else 0

    parse_error_rate_pct = (
        100.0 * diagnostics.unparsable_lines / diagnostics.lines if diagnostics.lines else 0.0
    )

    inputs = scorecard.ScorecardInputs(
        recache_share_pct=recache_share_pct,
        limit_recache_share_pct=limit_recache_share_pct,
        cache_hit_ratio_pct=cache_hit_ratio_pct,
        median_top_level_ctx=median_ctx,
        p90_top_level_ctx=p90_ctx,
        compaction_count=len(cs.records),
        dropped_share_pct=cs.dropped_share_of_new_tokens,
        has_spawns=has_spawns,
        agent_cost_variance_ratio=agent_variance,
        has_snapshot=has_snapshot,
        changed_config_keys=changed_keys,
        pricing_coverage_pct=pricing_coverage.coverage_pct,
        parse_error_rate_pct=parse_error_rate_pct,
        limit_pause_sessions=len(ls.sessions_affected),
        closest_match_turns=pricing_coverage.closest_match_turns,
    )
    return scorecard.build_section(inputs, th)


__all__ = ["build_report"]

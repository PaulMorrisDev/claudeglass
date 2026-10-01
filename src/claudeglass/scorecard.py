"""Optimisation scorecard (WP10a): five 1-5 levels summarising a corpus's
cache efficiency, context hygiene, agent efficiency, config fit and data
quality (plan "Optimisation scorecard" section).

This module deliberately depends only on ``model.py`` (frozen contract),
matching ``workstyle.py``'s convention: every cross-module metric
(re-cache share, TTL fidelity, topology cost variance, config-snapshot
diff counts, pricing coverage, parse diagnostics) is computed by report
assembly (``report.py``) and handed in here as a plain
:class:`ScorecardInputs`, so this module never needs to import
``recache``/``ttl``/``topology``/``compaction``/``snapshots``/``pricing``
itself and stays independently testable from hand-built inputs, per the
plan's test list ("scorecard boundaries, pinned in a test").

Levels are 1 (poor) to 5 (excellent). Per the plan: "the overall level is
the minimum of the four non-data dimensions, never an average" — data
quality is reported but excluded from ``overall``, since a low-fidelity
measurement shouldn't be conflated with a genuinely poor working pattern.

Deviation, reported rather than made silently (project convention, see
``model.py``'s module docstring): the plan's "config fit" dimension is
described as "whether observed TTL mix, effort distribution and
model-by-role match the archetype's profile" — but no ``profiles.py``
module (plan Appendix A7) exists yet anywhere in this codebase for a
session's observed shape to be compared against. This dimension is
implemented here as a proxy using only what already exists: whether a
config snapshot covers the corpus's projects at all, and (when one does)
how many distinct config keys changed between one project's own
snapshots (snapshots of two projects are never compared: they differ
anyway) — real config-fit signal, but a narrower measurement than the
full profile-match the plan describes.
When no snapshot is available at all, the dimension is scored 5
("no observed instability") rather than penalised, since the absence of
snapshot data is a missing-input problem, not evidence of a bad fit; a
note on the row says so.

Similarly, "agent efficiency" is measured here as the variance in mean
cost-per-agent-type versus the median across agent types (an available,
cheap proxy for "cost per spawn vs corpus median") rather than the plan's
full list (report size flowing up, spawn write size flowing down,
truncations) — those numbers exist in ``topology.TopologyStats`` but
combining four proxies into one defensible single-metric level is a
product decision better made once real corpora are available to tune
against; the table only ever shows one representative metric per
dimension by design (see :func:`build_section`'s docstring).

v3-limits addition: a usage-cap pause forces a full prefix rewrite on
resume (see ``ttl.py``'s "limit-expiry" branch) regardless of how
disciplined the session's own cache usage was, so counting that re-cache
share against ``cache_efficiency`` would penalise an account-level pause
as if it were a workflow choice. ``ScorecardInputs.limit_recache_share_pct``
lets report assembly pass the portion of ``recache_share_pct`` already
known (from ``limits.LimitStats``) to be limit-attributable; when given,
``_cache_efficiency`` scores the remainder and reports the exclusion in a
note rather than silently changing the number. Likewise
``ScorecardInputs.limit_pause_sessions`` (also from ``limits.LimitStats``)
is surfaced as a ``data_quality`` note when non-zero — a session that hit
a usage cap has its gap/re-cache measurements pause-discounted elsewhere
(``classify.py``, ``ttl.py``) rather than excluded, which is worth
flagging alongside the other data-quality caveats even though it does not
change the pricing-coverage level itself.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import ClassVar

from .model import Column, Section, Table


class ScorecardError(Exception):
    """A ``[thresholds.scorecard]`` config.toml value is invalid --
    written to stand alone as user-facing output, the same convention
    as ``config.ConfigError``/``pricing.PricingError`` (not literally
    ``config.ConfigError`` itself: this module deliberately depends on
    nothing but ``model.py``, see the module docstring).
    """


#: 1 (poor) .. 5 (excellent) level labels, printed alongside the level
#: number in the report table.
LEVEL_LABELS: dict[int, str] = {
    5: "excellent",
    4: "good",
    3: "fair",
    2: "poor",
    1: "very poor",
}

#: The four dimensions ``overall`` is the minimum of. "data_quality" is
#: reported but deliberately excluded (see module docstring).
_OVERALL_DIMENSIONS: tuple[str, ...] = (
    "cache_efficiency",
    "context_hygiene",
    "agent_efficiency",
    "config_fit",
)

ALL_DIMENSIONS: tuple[str, ...] = (*_OVERALL_DIMENSIONS, "data_quality")


@dataclass(slots=True)
class ScorecardThresholds:
    """Boundary values for each dimension's metric, each a 4-tuple giving
    the upper (lower-is-better metrics) or lower (higher-is-better
    metrics) bound for levels 5, 4, 3 and 2 in that order — a value
    beyond the fourth entry scores level 1. All overridable via
    ``config.toml``'s ``[thresholds.scorecard]`` table.
    """

    #: Re-cache share of cache_creation tokens (pct, lower is better).
    cache_recache_share_pct: tuple[float, float, float, float] = (5.0, 15.0, 30.0, 50.0)
    #: p90 top-level turn context size, tokens (lower is better).
    context_p90_ctx: tuple[float, float, float, float] = (50_000, 100_000, 150_000, 200_000)
    #: Mean cost-per-agent-type variance ratio (max/median, lower is better).
    agent_cost_variance_ratio: tuple[float, float, float, float] = (1.2, 1.5, 2.0, 3.0)
    #: Distinct config keys that changed between one project's own
    #: snapshots when a snapshot exists (count, lower is better).
    config_changed_keys: tuple[float, float, float, float] = (0, 3, 6, 10)
    #: Pricing coverage, pct of tokens priced (higher is better).
    data_pricing_coverage_pct: tuple[float, float, float, float] = (99.5, 97.0, 90.0, 75.0)

    #: Fields whose 4-tuple is (level-5 bound, level-4, level-3, level-2)
    #: in *ascending* order -- lower values are better.
    _LOWER_IS_BETTER_FIELDS: ClassVar[tuple[str, ...]] = (
        "cache_recache_share_pct",
        "context_p90_ctx",
        "agent_cost_variance_ratio",
        "config_changed_keys",
    )
    #: Fields whose 4-tuple is in *descending* order -- higher is better.
    _HIGHER_IS_BETTER_FIELDS: ClassVar[tuple[str, ...]] = ("data_pricing_coverage_pct",)

    @classmethod
    def from_config(cls, data: dict | None) -> "ScorecardThresholds":
        """Build thresholds from ``config.toml``'s ``[thresholds.scorecard]``
        table (a flat dict of this class's field names to 4-item
        sequences). Any absent or malformed key keeps this class's
        default. Unknown keys are ignored.

        Fix R20: ``_level_lower_is_better``/``_level_higher_is_better``
        below assume each tuple is monotonic (ascending for a
        lower-is-better metric, descending for the one higher-is-better
        metric) and short-circuit on that assumption -- a misordered
        override would silently score a corpus at the wrong level
        rather than fail loudly, so a tuple that isn't correctly
        ordered raises :class:`ScorecardError` naming the offending key
        instead of being accepted.
        """
        defaults = cls()
        if not isinstance(data, dict):
            return defaults
        kwargs: dict = {}
        for field_name in (*cls._LOWER_IS_BETTER_FIELDS, *cls._HIGHER_IS_BETTER_FIELDS):
            raw = data.get(field_name)
            if isinstance(raw, (list, tuple)) and len(raw) == 4:
                try:
                    values = tuple(float(v) for v in raw)
                except (TypeError, ValueError):
                    continue
                b5, b4, b3, b2 = values
                if field_name in cls._LOWER_IS_BETTER_FIELDS:
                    ordered, direction = b5 <= b4 <= b3 <= b2, "ascending"
                else:
                    ordered, direction = b5 >= b4 >= b3 >= b2, "descending"
                if not ordered:
                    raise ScorecardError(
                        f"[thresholds.scorecard].{field_name} must be {direction} "
                        f"(level 5, 4, 3, 2 bounds in that order), got {values!r}"
                    )
                kwargs[field_name] = values
        if not kwargs:
            return defaults
        return dataclasses.replace(defaults, **kwargs)


@dataclass(slots=True)
class ScorecardInputs:
    """Every metric a scorecard dimension needs, computed by report
    assembly from the corpus's already-built accumulators (see module
    docstring for why this module never computes them itself).

    ``None`` for a metric means "nothing to measure" (e.g. no priced
    turns at all yet) rather than a real zero value; the corresponding
    dimension is then skipped rather than scored.
    """

    # cache efficiency
    recache_share_pct: float | None = None
    cache_hit_ratio_pct: float | None = None
    full_expiry_share_pct: float | None = None
    #: Portion of ``recache_share_pct`` (percentage points) already known
    #: to be forced by a usage-limit pause rather than a workflow choice
    #: (from ``limits.LimitStats``). ``None`` means "not known" -- scored
    #: on the raw ``recache_share_pct`` as before, same as any other
    #: unset optional metric.
    limit_recache_share_pct: float | None = None

    # context hygiene
    median_top_level_ctx: float | None = None
    p90_top_level_ctx: float | None = None
    compaction_count: int = 0
    dropped_share_pct: float | None = None
    redundant_read_ratio_pct: float | None = None

    # agent efficiency (None when the corpus never spawned an agent)
    has_spawns: bool = False
    agent_cost_variance_ratio: float | None = None

    # config fit: ``changed_config_keys`` counts each key that changed
    # within some project's own snapshots once (``snapshots.changed_keys``)
    has_snapshot: bool = False
    changed_config_keys: int = 0

    # data quality
    pricing_coverage_pct: float = 100.0
    parse_error_rate_pct: float = 0.0
    #: Count of sessions with at least one usage-limit pause (from
    #: ``limits.LimitStats``). Surfaced as a ``data_quality`` note when
    #: non-zero; does not itself change the pricing-coverage level.
    limit_pause_sessions: int = 0
    #: Turns priced by closest (prefix) match rather than their own
    #: pricing.toml row (from ``pricing.PricingCoverage.closest_match_turns``
    #: — fix 2). Surfaced as a ``data_quality`` note when non-zero; the
    #: level itself is already driven by ``pricing_coverage_pct``, which
    #: counts a closest-match turn as priced.
    closest_match_turns: int = 0


def _level_lower_is_better(value: float, bounds: tuple[float, float, float, float]) -> int:
    b5, b4, b3, b2 = bounds
    if value <= b5:
        return 5
    if value <= b4:
        return 4
    if value <= b3:
        return 3
    if value <= b2:
        return 2
    return 1


def _level_higher_is_better(value: float, bounds: tuple[float, float, float, float]) -> int:
    b5, b4, b3, b2 = bounds
    if value >= b5:
        return 5
    if value >= b4:
        return 4
    if value >= b3:
        return 3
    if value >= b2:
        return 2
    return 1


@dataclass(slots=True)
class _DimensionResult:
    dimension: str
    level: int
    metric: str
    value: float
    threshold: str
    note: str | None = None


def _cache_efficiency(inputs: ScorecardInputs, th: ScorecardThresholds) -> _DimensionResult | None:
    if inputs.recache_share_pct is None:
        return None
    value = inputs.recache_share_pct
    note = None
    if inputs.limit_recache_share_pct is not None and inputs.limit_recache_share_pct > 0:
        value = max(0.0, inputs.recache_share_pct - inputs.limit_recache_share_pct)
        note = (
            f"Cache efficiency leaves out {inputs.limit_recache_share_pct:.1f} of the "
            f"{inputs.recache_share_pct:.1f} percentage points of cache rebuilds: those came "
            "after a usage-limit pause, which you can't avoid."
        )
    level = _level_lower_is_better(value, th.cache_recache_share_pct)
    return _DimensionResult(
        dimension="cache_efficiency",
        level=level,
        metric="recache_share_pct",
        value=value,
        threshold=f"<= {th.cache_recache_share_pct[5 - level]:.1f}%" if level > 1 else f"> {th.cache_recache_share_pct[-1]:.1f}%",
        note=note,
    )


def _context_hygiene(inputs: ScorecardInputs, th: ScorecardThresholds) -> _DimensionResult | None:
    if inputs.p90_top_level_ctx is None:
        return None
    level = _level_lower_is_better(inputs.p90_top_level_ctx, th.context_p90_ctx)
    return _DimensionResult(
        dimension="context_hygiene",
        level=level,
        metric="p90_top_level_ctx",
        value=inputs.p90_top_level_ctx,
        threshold=f"<= {th.context_p90_ctx[5 - level]:,.0f} tokens" if level > 1 else f"> {th.context_p90_ctx[-1]:,.0f} tokens",
    )


def _agent_efficiency(inputs: ScorecardInputs, th: ScorecardThresholds) -> _DimensionResult | None:
    if not inputs.has_spawns or inputs.agent_cost_variance_ratio is None:
        return None
    level = _level_lower_is_better(inputs.agent_cost_variance_ratio, th.agent_cost_variance_ratio)
    return _DimensionResult(
        dimension="agent_efficiency",
        level=level,
        metric="agent_cost_variance_ratio",
        value=inputs.agent_cost_variance_ratio,
        threshold=f"<= {th.agent_cost_variance_ratio[5 - level]:.2f}x" if level > 1 else f"> {th.agent_cost_variance_ratio[-1]:.2f}x",
        note="Subagent cost balance compares the average run of your costliest agent type "
        "with the typical agent type.",
    )


def _config_fit(inputs: ScorecardInputs, th: ScorecardThresholds) -> _DimensionResult:
    if not inputs.has_snapshot:
        return _DimensionResult(
            dimension="config_fit",
            level=5,
            metric="changed_config_keys",
            value=0,
            threshold="no config snapshot available",
            note="No settings snapshot is recorded for the projects shown, so config stability can't be "
            "measured. It is rated as stable rather than marked down.",
        )
    level = _level_lower_is_better(inputs.changed_config_keys, th.config_changed_keys)
    return _DimensionResult(
        dimension="config_fit",
        level=level,
        metric="changed_config_keys",
        value=inputs.changed_config_keys,
        threshold=f"<= {th.config_changed_keys[5 - level]:.0f} settings" if level > 1 else f"> {th.config_changed_keys[-1]:.0f} settings",
    )


def _data_quality(inputs: ScorecardInputs, th: ScorecardThresholds) -> _DimensionResult:
    level = _level_higher_is_better(inputs.pricing_coverage_pct, th.data_pricing_coverage_pct)
    note_parts: list[str] = []
    if inputs.limit_pause_sessions > 0:
        plural = "s" if inputs.limit_pause_sessions != 1 else ""
        note_parts.append(
            f"{inputs.limit_pause_sessions} session{plural} hit a usage limit and paused. "
            "Waits and cache rebuilds caused by those pauses are counted separately "
            "(see Usage limits), not left out."
        )
    if inputs.closest_match_turns > 0:
        plural = inputs.closest_match_turns != 1
        note_parts.append(
            f"{inputs.closest_match_turns} turn{'s' if plural else ''} counted as priced above "
            f"{'were' if plural else 'was'} only priced by closest match, not "
            f"{'their' if plural else 'its'} own model's rate (see Usage's "
            "\"Priced by closest match\" table)."
        )
    note = " ".join(note_parts) or None
    return _DimensionResult(
        dimension="data_quality",
        level=level,
        metric="pricing_coverage_pct",
        value=inputs.pricing_coverage_pct,
        threshold=f">= {th.data_pricing_coverage_pct[5 - level]:.1f}%" if level > 1 else f"< {th.data_pricing_coverage_pct[-1]:.1f}%",
        note=note,
    )


def build_section(inputs: ScorecardInputs, thresholds: ScorecardThresholds | None = None) -> Section:
    """Build the "Scorecard" report section (key ``"scorecard"``): one row
    per dimension in a ``dimensions`` table (dimension, level, label,
    metric, value, threshold), plus a one-row ``overall`` table.

    Only one representative metric is printed per dimension by design —
    each dimension's docstring in this module explains which one and why.
    A dimension with nothing to measure (e.g. ``agent_efficiency`` for a
    corpus that never spawned an agent) is left out of the table
    entirely and out of the ``overall`` computation, rather than
    guessing a level for it.
    """
    th = thresholds or ScorecardThresholds()

    results: list[_DimensionResult] = []
    cache_result = _cache_efficiency(inputs, th)
    if cache_result is not None:
        results.append(cache_result)
    context_result = _context_hygiene(inputs, th)
    if context_result is not None:
        results.append(context_result)
    agent_result = _agent_efficiency(inputs, th)
    if agent_result is not None:
        results.append(agent_result)
    results.append(_config_fit(inputs, th))
    data_result = _data_quality(inputs, th)
    results.append(data_result)

    dim_rows = [
        [r.dimension, r.level, LEVEL_LABELS[r.level], r.metric, r.value, r.threshold]
        for r in results
    ]
    dimensions_table = Table(
        name="dimensions",
        title="Scorecard dimensions",
        columns=[
            Column(key="dimension", label="Dimension", kind="str"),
            Column(key="level", label="Level", kind="int"),
            Column(key="label", label="Rating", kind="str"),
            Column(key="metric", label="Metric", kind="str"),
            Column(key="value", label="Value", kind="float"),
            Column(key="threshold", label="Threshold", kind="str"),
        ],
        rows=dim_rows,
        notes=[r.note for r in results if r.note],
    )

    overall_levels = [r.level for r in results if r.dimension in _OVERALL_DIMENSIONS]
    overall_level = min(overall_levels) if overall_levels else None
    overall_table = Table(
        name="overall",
        title="Overall",
        columns=[
            Column(key="metric", label="Metric", kind="str"),
            Column(key="level", label="Level", kind="int"),
            Column(key="label", label="Rating", kind="str"),
        ],
        rows=[
            [
                "overall",
                overall_level if overall_level is not None else 0,
                LEVEL_LABELS[overall_level] if overall_level is not None else "unmeasured",
            ]
        ],
    )

    notes = [
        "Overall is the lowest of cache efficiency, context size, subagent cost "
        "balance and config stability, not an average. Data quality is shown "
        "but not counted.",
    ]
    if agent_result is None:
        notes.append("Subagent cost balance is not rated: no subagents ran in this window.")

    return Section(
        key="scorecard",
        title="Scorecard",
        tables=[dimensions_table, overall_table],
        notes=notes,
    )


__all__ = [
    "LEVEL_LABELS",
    "ALL_DIMENSIONS",
    "ScorecardError",
    "ScorecardThresholds",
    "ScorecardInputs",
    "build_section",
]

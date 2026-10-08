"""``claudeglass baseline`` (v0.3 ``init``/onboarding milestone,
plan "Milestone v0.3" / "Running on other people's machines"): builds a
JSON "baseline" record from the corpus already discovered under a
project root, and a companion four-section Markdown onboarding report.

Nothing here writes to SQLite -- a baseline is one JSON file under
``<config_dir>/baselines/<id>.json`` (plus an optional sibling
``<id>.md`` for the rendered report). This mirrors ``config.py``'s own
"plain files under the config dir" posture, not the service package's
SQLite store (``service/store.py``, out of this module's scope).

Every field this module puts in a baseline record is read out of an
already-built :class:`~claudeglass.model.ReportModel` -- the same
"never fabricate, only cite the report's own tables" convention
``recommend.py``'s ``Recommendation.evidence`` contract already
enforces (see that module's docstring) -- rather than recomputed from
the corpus independently. In particular "projected saving" is exactly
the sum of the ``ttl`` section's ``ttl_by_agent_type`` table's
``saving_usd`` column, located by column *key* (never a hardcoded
index), because that's the one number in the report the plan's
"Risks and gaps" section already documents as a simulation, not a
promise.

Deviation from the plan, reported rather than made silently (the same
convention ``profiles/catalogue.py`` already follows for its own
``suggest()`` gap): ``profiles.catalogue.suggest(archetype, purposes)``
can never return ``"overnight-batch"`` by construction (its signature
has no session *mode* parameter -- see
``profiles/catalogue.py``'s ``UNREACHABLE_BY_SUGGEST``). Since a
majority-overnight corpus is exactly the case that profile exists for,
:func:`_suggested_profile` applies that override itself, directly from
the corpus's own ``sessions_by_mode`` table, before ever calling
``suggest()``.

Second deviation, also reported rather than silently resolved: the plan
describes billing-mode-aware suppression of subagent TTL recommendations
under subscription billing (a subagent's 1h TTL is documented as not
actually taking effect there), but doesn't give an exact cross-check
rule for detecting a *mis-set* ``billing`` value. This module's
heuristic (:func:`_billing_mismatch_warning`): when ``config.billing ==
"subscription"`` and any non-top-level agent-type row in
``ttl_by_agent_type`` shows a materially non-zero ``observed_1h_pct``
(more than :data:`BILLING_MISMATCH_THRESHOLD_PCT`), warn that ``billing``
may actually be ``"api"`` -- observing a 1h TTL happening somewhere it's
supposed to be impossible is evidence the stated mode is wrong.

v0.3 Task 2 addition: ``build_baseline`` now also records the metrics
:func:`~claudeglass.report.build_report`'s own ``baseline_comparison``
section diffs a later report against (``cost_per_session``,
``recache_share_pct``, ``compactions_per_session``, TTL mix, session
baseline size, mean spawn write, scorecard dimension levels, and a
per-mode ``by_mode`` breakdown of the first three). The extraction
functions for these live in ``report.py`` (as public, non-underscore
names) rather than here, and this module imports them, because
``report.py`` needs the identical logic for the *current* window's side
of that same comparison and ``baseline.py`` already imports from
``report.py`` (never the other way around) -- duplicating the table
lookups in both modules would risk the two sides of a comparison
silently drifting apart.

Third deviation, reported per this module's own convention: per-mode
stratification is scoped to just those three metrics (cost/re-cache/
compactions), computed by re-running :func:`~claudeglass.report.build_report`
once per mode value over a session-filtered sub-corpus. TTL mix, session
baseline size, mean spawn write and scorecard levels stay corpus-wide
only -- the accumulators behind them (``topology.TopologyStats``,
``ttl.TtlStats``) accumulate flat lists/dicts with no per-session id
retained, so stratifying those would need changes to modules outside
this work package's file list.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import classify, discovery, snapshots
from .cache import DigestCache
from .config import Config
from .corpus import load_corpus
from .model import ReportModel, Table
from .parse import load_or_create_salt
from .pricing import Pricing
from .profiles import catalogue
from .units import Units
from .report import (
    build_report,
    compactions_per_session_metric,
    mean_spawn_write_by_agent_type_metric,
    overview_metric,
    recache_share_pct_metric,
    scorecard_dimensions_metric,
    session_baseline_size_metric,
    ttl_mix_by_agent_type_metric,
)

__all__ = [
    "CaptureStatus",
    "capture_status",
    "format_capture_status",
    "build_baseline",
    "baselines_dir",
    "save_baseline",
    "list_baselines",
    "load_baseline",
    "render_onboarding_report",
]

#: A majority-overnight corpus (by session count, from the "sessions"
#: section's "sessions_by_mode" table) overrides catalogue.suggest()'s
#: own answer to "overnight-batch" -- see module docstring.
_OVERNIGHT_OVERRIDE_SHARE = 0.5

#: Share of main sessions that plan and build in one session before the
#: suggestion becomes ``plan-then-build``.
_PLAN_BUILD_SHARE = 0.5

#: See module docstring's second deviation note. A percentage point
#: value, not a fraction (matches ttl.py's own "*_pct" column
#: convention).
BILLING_MISMATCH_THRESHOLD_PCT = 5.0

#: How many of the "sessions_by_purpose" table's rows (already sorted
#: most-common-first by classify.build_section) count as "dominant".
_DOMINANT_PURPOSES_LIMIT = 3

#: Profile suggested for a project with no sessions in the window yet --
#: never provided by catalogue.suggest() (which requires an archetype),
#: so named directly.
_NO_DATA_PROFILE = "interactive-chat"


@dataclass(slots=True)
class CaptureStatus:
    """Where a project stands against its ``init``-started onboarding
    capture window (``config.capture_window``/``config.capture_started``).
    """

    #: Whether ``init`` has started a capture window at all.
    started: bool
    #: The configured window length in days, or ``None`` if not set.
    window_days: int | None
    #: The raw ``config.capture_started`` value, unparsed.
    started_at: str | None
    #: Days elapsed since ``started_at``, or ``None`` if not started or
    #: ``started_at`` could not be parsed.
    elapsed_days: float | None
    #: Days remaining in the window (floored at 0), or ``None``.
    remaining_days: float | None
    #: Whether the window has fully elapsed (a baseline built now is not
    #: provisional on capture-window grounds alone).
    complete: bool


def capture_status(config: Config, now: datetime | None = None) -> CaptureStatus:
    """The current :class:`CaptureStatus` for ``config``. Never raises --
    an unparsable ``capture_started`` is reported as "started, but
    elapsed/remaining unknown" rather than failing the caller, the same
    "never crash on a foreign shape" posture ``config.py`` documents for
    ``sessions.toml``.
    """
    now = now or datetime.now(timezone.utc)
    if config.capture_started is None or config.capture_window is None:
        return CaptureStatus(
            started=False,
            window_days=config.capture_window,
            started_at=config.capture_started,
            elapsed_days=None,
            remaining_days=None,
            complete=False,
        )

    try:
        started_dt = datetime.fromisoformat(config.capture_started)
    except ValueError:
        return CaptureStatus(
            started=True,
            window_days=config.capture_window,
            started_at=config.capture_started,
            elapsed_days=None,
            remaining_days=None,
            complete=False,
        )
    if started_dt.tzinfo is None:
        started_dt = started_dt.replace(tzinfo=timezone.utc)

    elapsed_days = (now - started_dt).total_seconds() / 86400.0
    remaining_days = max(0.0, config.capture_window - elapsed_days)
    complete = elapsed_days >= config.capture_window
    return CaptureStatus(
        started=True,
        window_days=config.capture_window,
        started_at=config.capture_started,
        elapsed_days=elapsed_days,
        remaining_days=remaining_days,
        complete=complete,
    )


def format_capture_status(status: CaptureStatus) -> str:
    """One human-readable line summarising ``status``, for ``init`` and
    ``baseline`` to print."""
    if not status.started:
        return "Capture window: not started (run 'claudeglass init' first)."
    if status.elapsed_days is None:
        return "Capture window: started, but its recorded start time could not be read."
    if status.complete:
        return (
            f"Capture window: complete ({status.elapsed_days:.1f} of "
            f"{status.window_days} days) -- a baseline built now is not provisional."
        )
    return (
        f"Capture window: in progress ({status.elapsed_days:.1f} of "
        f"{status.window_days} days, {status.remaining_days:.1f} days remaining) -- "
        "a baseline built now is provisional."
    )


# -- report-table extraction (never fabricate, only cite) -------------------


def _table(model: ReportModel, section_key: str, table_name: str) -> Table | None:
    for section in model.sections:
        if section.key != section_key:
            continue
        for table in section.tables:
            if table.name == table_name:
                return table
    return None


def _mode_mix(model: ReportModel) -> dict[str, int]:
    """``{mode: session_count}`` straight from the "sessions" section's
    "sessions_by_mode" table (already sorted largest-group-first by
    ``classify._group_summary_table``)."""
    table = _table(model, "sessions", "sessions_by_mode")
    if table is None:
        return {}
    return {str(row[0]): int(row[1]) for row in table.rows}


def _dominant_purposes(model: ReportModel, limit: int = _DOMINANT_PURPOSES_LIMIT) -> list[str]:
    table = _table(model, "sessions", "sessions_by_purpose")
    if table is None:
        return []
    return [str(row[0]) for row in table.rows[:limit]]


def _dominant_tasks(model: ReportModel, limit: int = _DOMINANT_PURPOSES_LIMIT) -> list[str]:
    """The kinds of task metrics capture reported, costliest first (the
    Work habits section's ``habits_by_task``, without its ``all`` row)."""
    table = _table(model, "habits", "habits_by_task")
    if table is None:
        return []
    return [str(row[0]) for row in table.rows if row and row[0] != "all"][:limit]


def _plan_build(model: ReportModel) -> dict | None:
    """Main sessions that approved a plan and built it in the same
    session (the Work habits section's ``habits_by_shape``, ``plan_build``
    row): ``sessions``, ``total`` main sessions, ``share`` (percent) and
    the /cg-feedback handoff answers. ``None`` without that row."""
    table = _table(model, "habits", "habits_by_shape")
    if table is None:
        return None
    rows = {row[0]: dict(zip((c.key for c in table.columns), row)) for row in table.rows if row}
    row = rows.get("plan_build")
    if row is None:
        return None
    return {
        "sessions": row.get("sessions") or 0,
        "total": sum(r.get("sessions") or 0 for r in rows.values()),
        "share": row.get("share") or 0.0,
        "yes": row.get("handoff_yes") or 0,
        "partly": row.get("handoff_partly") or 0,
        "no": row.get("handoff_no") or 0,
    }


def _corpus_archetype(model: ReportModel) -> str | None:
    """The corpus's workstyle archetype: the one that cost the most. The
    "workstyle" section's "workstyle_archetypes" table is sorted by spend,
    descending (by session count when no spend is known), so row 0 is the
    archetype ``workstyle.corpus_archetype`` gives ``recommend()`` -- the two
    order with the same function -- or there are no rows at all."""
    table = _table(model, "workstyle", "workstyle_archetypes")
    if table is None or not table.rows:
        return None
    return table.rows[0][0]


def _scorecard_overall(model: ReportModel) -> tuple[int, str] | None:
    table = _table(model, "scorecard", "overall")
    if table is None or not table.rows:
        return None
    row = table.rows[0]
    return int(row[1]), str(row[2])


def _column_index(table: Table, key: str) -> int | None:
    for index, column in enumerate(table.columns):
        if column.key == key:
            return index
    return None


def _projected_saving_usd(model: ReportModel) -> float:
    """Sum of the "ttl" section's "ttl_by_agent_type" table's
    "saving_usd" column (already 0-floored per row by ``ttl.py``) --
    the only number the plan's "computed only from the TTL simulation
    already in the report" constraint permits here."""
    table = _table(model, "ttl", "ttl_by_agent_type")
    if table is None:
        return 0.0
    index = _column_index(table, "saving_usd")
    if index is None:
        return 0.0
    total = 0.0
    for row in table.rows:
        value = row[index]
        if isinstance(value, (int, float)):
            total += float(value)
    return total


def _suggested_profile(
    mode_mix: dict[str, int],
    archetype: str | None,
    purposes: list[str],
    tasks: list[str] | None = None,
    plan_build: dict | None = None,
) -> tuple[str, str]:
    """The catalogue profile id to suggest, and a one-line reason citing
    the evidence -- see module docstring's first deviation note for why
    the overnight override has to live here rather than inside
    ``catalogue.suggest()``. ``overnight`` is the mode where Claude worked
    on its own for two hours or more at night while you were away
    (``classify.classify_mode``), so the override is for a corpus of
    unattended night runs, not of long days. ``plan_build`` (:func:`_plan_build`) makes
    the corpus ``plan-then-build`` when at least
    ``_PLAN_BUILD_SHARE`` of its main sessions planned and built in one
    session."""
    total_sessions = sum(mode_mix.values())
    overnight_count = mode_mix.get("overnight", 0)
    if total_sessions > 0 and overnight_count / total_sessions >= _OVERNIGHT_OVERRIDE_SHARE:
        return (
            "overnight-batch",
            f"{overnight_count}/{total_sessions} sessions were overnight runs, where Claude worked on "
            "its own for two hours or more at night while you were away "
            f"(>= {_OVERNIGHT_OVERRIDE_SHARE:.0%}) -- catalogue.suggest() can never reach "
            "'overnight-batch' by design (see profiles/catalogue.py's "
            "UNREACHABLE_BY_SUGGEST), so this override is applied directly from the "
            "corpus's own sessions_by_mode table.",
        )
    shape = None
    evidence = ""
    if plan_build and plan_build["share"] >= _PLAN_BUILD_SHARE * 100:
        shape = "plan-then-build"
        evidence = (
            f"{plan_build['sessions']} of {plan_build['total']} main sessions approved a plan and built it in "
            "the same session (habits.habits_by_shape)"
        )
        answers = plan_build["yes"] + plan_build["partly"] + plan_build["no"]
        if answers:
            evidence += (
                f"; your /cg-feedback said the plan alone was enough for {plan_build['yes']} of {answers} builds"
            )
        evidence += " -- "
    profile_id = catalogue.suggest(archetype, purposes, tasks or (), shape)
    return (
        profile_id,
        evidence
        + f"catalogue.suggest(archetype={archetype!r}, purposes={purposes!r}"
        + (f", tasks={tasks!r}" if tasks else "")
        + (f", shape={shape!r}" if shape else "")
        + ")",
    )


def _billing_mismatch_warning(config: Config, model: ReportModel) -> str | None:
    if config.billing != "subscription":
        return None
    table = _table(model, "ttl", "ttl_by_agent_type")
    if table is None:
        return None
    agent_index = _column_index(table, "agent_type")
    pct_index = _column_index(table, "observed_1h_pct")
    if agent_index is None or pct_index is None:
        return None
    for row in table.rows:
        agent_type = row[agent_index]
        pct = row[pct_index]
        if agent_type == "top-level" or not isinstance(pct, (int, float)):
            continue
        if pct > BILLING_MISMATCH_THRESHOLD_PCT:
            return (
                f"config.toml sets billing='subscription', but agent_type={agent_type!r} "
                f"shows observed_1h_pct={pct:.1f}% -- a subagent's 1h TTL is documented as "
                "not taking effect under subscription billing, so this may mean billing is "
                "actually 'api'."
            )
    return None


# -- assembly -----------------------------------------------------------


def build_baseline(
    *,
    config: Config,
    pricing: Pricing,
    config_dir: str | Path,
    project_dirs: list[Path],
    days: int | None = None,
    finalise: bool = False,
    now: datetime | None = None,
) -> tuple[dict, ReportModel | None]:
    """Build a baseline record for ``project_dirs``. Returns ``(record,
    model)`` -- ``model`` is the full :class:`ReportModel` the record was
    extracted from (``None`` when there were no sessions in the window,
    in which case the record is a minimal all-defaults one rather than
    calling :func:`~claudeglass.report.build_report` against an
    empty corpus at all).

    ``record`` is a plain JSON-safe ``dict`` (not a dataclass), ready
    for :func:`save_baseline` -- kept as a dict rather than a frozen
    dataclass because it is a versioned on-disk format, the same choice
    ``snapshots.py``'s schema makes for the same reason.
    """
    now = now or datetime.now(timezone.utc)
    status = capture_status(config, now=now)
    provisional = not finalise and not status.complete

    config_dir = Path(config_dir)
    salt = load_or_create_salt(config_dir)
    cache = DigestCache(config_dir, salt=salt)
    corpus = load_corpus(
        project_dirs,
        days=days,
        cache=cache,
        exclude_projects=config.exclude_projects,
        salt=salt,
        config_dir=config_dir,
    )

    redacted_projects = [discovery.redact_slug(p.name) for p in project_dirs]

    if not corpus.sessions:
        record = {
            "id": uuid.uuid4().hex[:12],
            "created_at": now.isoformat(),
            "window_days": days,
            "provisional": True,
            "sessions_analysed": 0,
            "mode_mix": {},
            "mode_rules": classify.MODE_RULES,
            "dominant_purposes": [],
            "archetype": None,
            "scorecard_overall": None,
            "scorecard_label": None,
            "suggested_profile": _NO_DATA_PROFILE,
            "suggested_profile_reason": "no sessions found in this window yet",
            "projected_saving_usd": 0.0,
            # UX-2: so render_onboarding_report -- which only ever sees a
            # saved record, never a live Config/ReportModel -- can still
            # phrase amounts for the right billing mode.
            "billing_mode": config.billing,
            "currency": pricing.currency,
            "billing_mismatch_warning": None,
            "projects": redacted_projects,
            # v0.3 Task 2 fields -- see module docstring's addition note.
            "cost_per_session": 0.0,
            "recache_share_pct": None,
            "compactions_per_session": None,
            "ttl_mix_top_level": None,
            "ttl_mix_by_agent_type": {},
            "session_baseline_size": None,
            "mean_spawn_write_by_agent_type": {},
            "scorecard_dimensions": {},
            "by_mode": {},
        }
        return record, None

    projects = tuple(p.name for p in project_dirs)
    snaps = snapshots.load_snapshots(config_dir) or None
    window = f"last {days} days" if days else "all time"
    model = build_report(corpus, pricing, config, projects=projects, window=window, snapshots=snaps)

    mode_mix = _mode_mix(model)
    purposes = _dominant_purposes(model)
    archetype = _corpus_archetype(model)
    scorecard = _scorecard_overall(model)
    profile_id, profile_reason = _suggested_profile(
        mode_mix, archetype, purposes, _dominant_tasks(model), _plan_build(model)
    )

    # v0.3 Task 2: metrics report.py's own baseline_comparison section
    # will later diff a fresh window against -- see module docstring's
    # addition note for why the extraction functions live in report.py.
    # Orphan bundles (subagent transcripts whose session file is gone) add
    # cost but are not sessions, the same way build_report skips them.
    sessions_analysed = sum(1 for bundle in corpus.sessions if bundle.top is not None)
    total_cost = overview_metric(model.sections, "total_cost_usd")
    cost_per_session = (total_cost / sessions_analysed) if total_cost is not None and sessions_analysed else 0.0
    recache_share_pct = recache_share_pct_metric(model.sections)
    compactions_per_session = compactions_per_session_metric(model.sections)
    ttl_mix = ttl_mix_by_agent_type_metric(model.sections)
    ttl_mix_top_level = ttl_mix.get("top-level")
    ttl_mix_by_agent_type = {agent_type: mix for agent_type, mix in ttl_mix.items() if agent_type != "top-level"}
    session_baseline_size = session_baseline_size_metric(model.sections)
    mean_spawn_write_by_agent_type = mean_spawn_write_by_agent_type_metric(model.sections)
    scorecard_dimensions = scorecard_dimensions_metric(model.sections)
    by_mode = _by_mode_metrics(corpus, pricing, config, projects, window, snaps)

    record = {
        "id": uuid.uuid4().hex[:12],
        "created_at": now.isoformat(),
        "window_days": days,
        "provisional": provisional,
        "sessions_analysed": sessions_analysed,
        "mode_mix": mode_mix,
        "mode_rules": classify.MODE_RULES,
        "dominant_purposes": purposes,
        "archetype": archetype,
        "scorecard_overall": scorecard[0] if scorecard else None,
        "scorecard_label": scorecard[1] if scorecard else None,
        "suggested_profile": profile_id,
        "suggested_profile_reason": profile_reason,
        "projected_saving_usd": round(_projected_saving_usd(model), 4),
        # UX-2: so render_onboarding_report -- which only ever sees a
        # saved record, never a live Config/ReportModel -- can still
        # phrase amounts for the right billing mode.
        "billing_mode": config.billing,
        "currency": pricing.currency,
        "billing_mismatch_warning": _billing_mismatch_warning(config, model),
        "projects": redacted_projects,
        # v0.3 Task 2 fields -- see module docstring's addition note.
        "cost_per_session": round(cost_per_session, 4),
        "recache_share_pct": recache_share_pct,
        "compactions_per_session": compactions_per_session,
        "ttl_mix_top_level": ttl_mix_top_level,
        "ttl_mix_by_agent_type": ttl_mix_by_agent_type,
        "session_baseline_size": session_baseline_size,
        "mean_spawn_write_by_agent_type": mean_spawn_write_by_agent_type,
        "scorecard_dimensions": scorecard_dimensions,
        "by_mode": by_mode,
    }
    return record, model


def _by_mode_metrics(
    corpus,
    pricing: Pricing,
    config: Config,
    projects: tuple[str, ...],
    window: str,
    snaps,
) -> dict[str, dict[str, float | None]]:
    """``{mode: {"sessions": n, "cost_per_session": ..., "recache_share_pct":
    ..., "compactions_per_session": ...}}`` -- one extra
    :func:`~claudeglass.report.build_report` call per distinct mode
    present in ``corpus``, each over a session-filtered sub-corpus, so the
    per-mode figures come from the exact same table-extraction functions
    as the corpus-wide ones above (see module docstring's third deviation
    note for why stratification stops at these three metrics).
    """
    buckets: dict[str, list] = {}
    for bundle in corpus.sessions:
        if bundle.top is None:
            continue
        classification = classify.classify_session(bundle.top, bundle.subs, {}, config.tz)
        buckets.setdefault(classification.mode, []).append(bundle)

    by_mode: dict[str, dict[str, float | None]] = {}
    for mode, bundles in buckets.items():
        n = len(bundles)
        sub_corpus = dataclasses.replace(corpus, sessions=bundles)
        sub_model = build_report(sub_corpus, pricing, config, projects=projects, window=window, snapshots=snaps)
        sub_total_cost = overview_metric(sub_model.sections, "total_cost_usd")
        by_mode[mode] = {
            "sessions": n,
            "cost_per_session": round(sub_total_cost / n, 4) if sub_total_cost is not None and n else None,
            "recache_share_pct": recache_share_pct_metric(sub_model.sections),
            "compactions_per_session": compactions_per_session_metric(sub_model.sections),
        }
    return by_mode


# -- persistence: plain JSON files, never SQLite -----------------------


def baselines_dir(config_dir: str | Path) -> Path:
    return Path(config_dir) / "baselines"


def save_baseline(config_dir: str | Path, record: dict, report_markdown: str | None = None) -> Path:
    """Write ``record`` to ``<config_dir>/baselines/<id>.json`` (and, if
    given, ``report_markdown`` to the sibling ``<id>.md``). Returns the
    JSON file's path.
    """
    directory = baselines_dir(config_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{record['id']}.json"
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if report_markdown is not None:
        (directory / f"{record['id']}.md").write_text(report_markdown, encoding="utf-8")
    return path


def list_baselines(config_dir: str | Path) -> list[dict]:
    """Every baseline record under ``<config_dir>/baselines/``, oldest
    first. A missing directory returns ``[]``; an unreadable or
    malformed record file is skipped, the same tolerant posture
    ``snapshots.load_snapshots`` documents for its own directory scan.
    """
    directory = baselines_dir(config_dir)
    if not directory.is_dir():
        return []
    records: list[dict] = []
    for path in sorted(directory.glob("*.json")):
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    records.sort(key=lambda r: r.get("created_at", ""))
    return records


def load_baseline(config_dir: str | Path, baseline_id: str) -> dict | None:
    path = baselines_dir(config_dir) / f"{baseline_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# -- rendering ------------------------------------------------------------


def render_onboarding_report(record: dict) -> str:
    """The four-section Markdown onboarding report the brief requires:
    Summary, Suggested profile, Projected saving, Next steps. Every
    figure in it comes straight from ``record`` (itself built only from
    the report's own tables -- see :func:`build_baseline`), so there is
    nothing here to fabricate.
    """
    # UX-2: a saved record has no live Config/ReportModel to read a
    # Units instance off of, so build one from the billing_mode/currency
    # build_baseline stored alongside it -- an older record from before
    # that field existed falls back to "api" (its previous, only, behaviour).
    # No elasticity fit travels with a record, so a subscription record
    # phrases its saving as a list-price equivalent with the usual hint,
    # never a bare "$".
    units = Units(billing_mode=record.get("billing_mode") or "api", currency=record.get("currency") or "USD")
    lines: list[str] = ["# Onboarding baseline report", ""]

    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Sessions analysed: {record['sessions_analysed']}")
    lines.append(f"- Provisional: {'yes' if record['provisional'] else 'no'}")
    if record.get("archetype"):
        lines.append(f"- Dominant workstyle archetype: {record['archetype']}")
    if record.get("dominant_purposes"):
        lines.append(f"- Dominant purposes: {', '.join(record['dominant_purposes'])}")
    if record.get("mode_mix"):
        mix = ", ".join(f"{mode}={count}" for mode, count in record["mode_mix"].items())
        lines.append(f"- Session mode mix: {mix}")
    if record.get("scorecard_overall") is not None:
        lines.append(f"- Scorecard overall: {record['scorecard_overall']} ({record['scorecard_label']})")
    if record.get("billing_mismatch_warning"):
        lines.append(f"- Warning: {record['billing_mismatch_warning']}")
    lines.append("")

    lines.append("## Suggested profile")
    lines.append("")
    lines.append(f"- Profile: `{record['suggested_profile']}`")
    lines.append(f"- Reason: {record['suggested_profile_reason']}")
    lines.append("")

    lines.append("## Projected saving")
    lines.append("")
    lines.append(
        f"- {units.money_text(record['projected_saving_usd'])}, the sum of the TTL break-even "
        "simulation's `saving_usd` column across every agent type already computed in "
        "this report (see the `ttl` section's `ttl_by_agent_type` table)."
    )
    lines.append("")

    lines.append("## Next steps")
    lines.append("")
    if record["provisional"]:
        lines.append(
            "- This baseline is provisional -- the onboarding capture window has not "
            "finished yet. Re-run `claudeglass baseline` once it has, or pass "
            "`--finalise` to accept this baseline now."
        )
    lines.append(
        f"- Review the suggested profile's settings: see "
        f"`profiles/catalogue/{record['suggested_profile']}.toml` and "
        "`claudeglass report --patch-set`."
    )
    lines.append(
        "- Applying a profile automatically is not implemented in this package version "
        "yet; apply the settings it names by hand for now."
    )
    lines.append("")

    return "\n".join(lines)

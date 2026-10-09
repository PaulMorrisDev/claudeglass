"""A/B compare (v0.3 milestone, plan "Feature expansion" item 6): compare
two arms of sessions selected by a date window, a snapshot config-key
value, a profile id, or a project slug list, stratified by purpose/mode
with a minimum-sample gate, so a config/profile change can be read
against its own workload rather than the whole corpus at once.

Every number here comes from the same pricing/classification code the
main report uses (:mod:`pricing`, :mod:`classify`, :mod:`recache`) --
never recomputed independently -- but the small one-line per-transcript
helpers (``_priced_turns``, ``_transcripts_of``, ``_parse_ts``) are
deliberately duplicated rather than imported from ``report.py``, matching
the convention ``report.py``/``usage.py``/``workflows.py``/``phases.py``/
``cli.py`` each document in their own module docstrings: report.py itself
is not writable for this work package, and these helpers are private
(underscore-prefixed) there in any case.

Plan "Risks and gaps" item 2 (correlation is not causation) is why every
table this module builds carries an "observed, not controlled" note plus
each arm's own exact selection rule, why :func:`compare` stratifies by
purpose/mode with a minimum-sample gate (default 5 sessions/arm, the same
default ``Config.min_sessions`` uses elsewhere), and why the co-changed
table exists at all: a config-keyed A/B split can look like it explains a
cost difference when some other key actually moved at the same time.

Design choices made here rather than left to guesswork, per this
project's "document deviations instead of silently resolving them"
convention (see ``model.py``/``report.py``/``snapshots.py``/``classify.py``
module docstrings for the same pattern):

- ``compare_overview``'s ``Table`` contract requires one ``Column.kind``
  per column, shared by every row -- but the brief's headline metrics
  span five different kinds (money, tokens, pct, float, secs). Rather
  than force every metric into its own four-column group (arm A/arm B/
  delta/delta-%), which would make one table roughly forty columns wide,
  this module uses one row per metric with the value/delta/delta-% cells
  pre-formatted (via ``render.tables.format_cell``, the project's single
  formatting entry point, called here instead of at render time) into
  ``kind="str"`` columns. This keeps the table narrow and readable but
  means its JSON/CSV export carries display text for this one table, not
  raw numbers, unlike every other table in this codebase (see
  ``render/csv_out.py``'s module docstring for the raw-value convention
  this knowingly departs from).
- Review finding S4: ``cost``, ``new_tokens`` and ``priced_turns`` are
  arm *totals* -- summed across every session in the arm -- so an arm
  with more sessions than the other always produced a large headline
  delta on these three rows even when nothing about the sessions
  themselves differed. ``_METRIC_SPECS`` now leads with the per-session
  means (``cost_per_session``, ``new_tokens_per_session``,
  ``priced_turns_per_session``) computed by ``_aggregate``; the raw
  totals are kept as separate rows labelled "... (informational)" further
  down the table so the aggregate figures are not lost, just no longer
  presented as if they were rates. ``_build_stratum_table`` applies the
  same per-session normalisation to its cost/new-tokens columns for the
  same reason.
- ``compare_by_stratum`` reports a reduced set of the overview's metrics
  (session counts, cost per session, new tokens per session, cache-read
  share) rather than repeating every headline metric per stratum -- again
  to keep one row per stratum a fixed-width, fixed-kind-per-column,
  raw-valued table (so its CSV/JSON export stays numeric) instead of a
  very wide or pre-formatted one. The overview table already carries the
  full headline breakdown for the corpus as a whole.
- The ``profile:<id>`` arm form selects the sessions that started while
  profile ``<id>`` was active (``snapshots.profile_for``). With
  ``config_dir``, that reads the config directory's whole record: the
  config hook's capture of the ``active-profile`` marker at each
  session start, ``apply``'s stamps, and undone applies. Without it,
  only the hook captures in ``snapshots`` count. A session that started
  before anything recorded a profile, or with none applied, matches no
  ``profile:`` arm.
- ``compare_co_changed`` is only ever populated when *both* arms are
  ``key:``-selected (comparing two representative snapshots makes sense
  only when both arms are themselves defined by a snapshot key/value);
  for any other arm-kind combination it is an empty table with a note
  explaining why. When an arm's matched sessions joined more than one
  distinct snapshot (``snapshots.snapshot_for`` per session), the most
  common one is used as that arm's "representative" snapshot for this
  table, noted explicitly since a per-session join can differ from it.
"""

from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import classify, discovery, recache
from . import snapshots as snapshots_mod
from .calibration import model_family
from .config import Config
from .corpus import Corpus, SessionBundle
from .model import Column, EventKind, Section, Table, TranscriptResult, Turn
from .pricing import Pricing, PricingCoverage, price_turn
from .render.tables import format_cell
from .snapshots import Snapshot

#: The default stratification keys (mirrors
#: ``classify.Classification.mode``/``.purpose``, the only two fields the
#: brief names for stratification).
_STRATIFY_KEYS = ("purpose", "mode")
#: Every key :func:`compare` can stratify by: ``task`` is the kind of task
#: metrics capture reported for the session (``classify.reported_task``),
#: ``untagged`` without one.
STRATIFY_CHOICES = ("purpose", "mode", "task")
#: With no keys given, ``task`` joins the defaults once at least this
#: share of both arms' sessions has a reported task.
TASK_COVERAGE_PCT = 50.0


# -- arm selection -----------------------------------------------------------


@dataclass(slots=True)
class ArmSpec:
    """One side of an A/B comparison: how to select sessions for it.

    ``label`` is the verbatim spec text the user (or a test) passed in --
    used as-is for "the exact selection rule of each arm" every table's
    mandatory note must carry, so the note is always reproducible from the
    spec that produced it.
    """

    kind: str  # "window" | "key" | "profile" | "project"
    label: str
    since: str | None = None
    until: str | None = None
    since_dt: datetime | None = None
    until_dt: datetime | None = None
    key: str | None = None
    value: str | None = None
    profile_id: str | None = None
    projects: tuple[str, ...] = ()


def parse_arm_spec(text: str) -> ArmSpec:
    """Parse one ``--a``/``--b`` CLI spec string into an :class:`ArmSpec`.

    Recognised forms: ``window:<since>..<until>`` (either date may be
    omitted but not both, ``YYYY-MM-DD``), ``key:<key>=<value>`` (``key``
    a flattened snapshot key as ``snapshots.flatten_snapshot`` produces
    it, e.g. ``user_settings.autoCompactWindow``), ``profile:<id>``, or
    ``project:<slug>[,<slug>...]``. Raises ``ValueError`` with a single-
    line, user-facing message on anything else -- the CLI catches this
    and exits 2.
    """
    raw = text
    kind, sep, rest = text.partition(":")
    if not sep:
        raise ValueError(
            f"bad arm spec {raw!r}: expected window:<since>..<until>, "
            "key:<key>=<value>, profile:<id>, or project:<slug>[,<slug>...]"
        )

    if kind == "window":
        if ".." not in rest:
            raise ValueError(f"bad arm spec {raw!r}: window needs since..until, e.g. window:2026-08-01..2026-08-31")
        since_s, _, until_s = rest.partition("..")
        since_s = since_s.strip()
        until_s = until_s.strip()
        try:
            since_dt = datetime.strptime(since_s, "%Y-%m-%d").replace(tzinfo=timezone.utc) if since_s else None
            until_dt = (
                datetime.strptime(until_s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                + timedelta(days=1)
                - timedelta(microseconds=1)
                if until_s
                else None
            )
        except ValueError as exc:
            raise ValueError(f"bad arm spec {raw!r}: dates must be YYYY-MM-DD") from exc
        if since_dt is None and until_dt is None:
            raise ValueError(f"bad arm spec {raw!r}: window needs at least one of since/until")
        return ArmSpec(
            kind="window", label=raw, since=since_s or None, until=until_s or None, since_dt=since_dt, until_dt=until_dt
        )

    if kind == "key":
        if "=" not in rest:
            raise ValueError(f"bad arm spec {raw!r}: expected key:<key>=<value>")
        key_name, _, value = rest.partition("=")
        key_name = key_name.strip()
        value = value.strip()
        if not key_name or not value:
            raise ValueError(f"bad arm spec {raw!r}: expected key:<key>=<value>, both non-empty")
        return ArmSpec(kind="key", label=raw, key=key_name, value=value)

    if kind == "profile":
        profile_id = rest.strip()
        if not profile_id:
            raise ValueError(f"bad arm spec {raw!r}: expected profile:<id>")
        return ArmSpec(kind="profile", label=raw, profile_id=profile_id)

    if kind == "project":
        projects = tuple(discovery.redact_slug(s.strip()) for s in rest.split(",") if s.strip())
        if not projects:
            raise ValueError(f"bad arm spec {raw!r}: expected project:<slug>[,<slug>...]")
        return ArmSpec(kind="project", label=raw, projects=projects)

    raise ValueError(f"bad arm spec {raw!r}: unknown selector {kind!r} (expected window/key/profile/project)")


# -- per-session metrics ------------------------------------------------------


def _priced_turns(result: TranscriptResult) -> list[Turn]:
    """Duplicated one-line helper -- see module docstring."""
    return [t for t in result.turns if t.turn_index > 0]


def _transcripts_of(bundle: SessionBundle) -> list[TranscriptResult]:
    """Duplicated one-line helper -- see module docstring."""
    transcripts: list[TranscriptResult] = []
    if bundle.top is not None:
        transcripts.append(bundle.top)
    transcripts.extend(bundle.subs)
    return transcripts


def _parse_ts(ts: str | None) -> datetime | None:
    """Duplicated one-line helper -- see module docstring."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass(slots=True)
class _SessionMetrics:
    """One session's worth of the numbers every compare table is built
    from -- computed once per :func:`compare` call and then filtered into
    each arm, so a session appearing in both arms (specs are independent
    membership tests, not mutually exclusive partitions) is only priced
    once.
    """

    session_id: str
    slug: str
    first_ts: str | None
    span_s: float
    mode: str
    purpose: str
    profile_id: str | None
    #: Every key the session's project's snapshots can carry
    #: (``snapshots.snapshot_project_keys``).
    project_key: tuple[str, ...] | str | None = None
    task: str = "untagged"
    priced_turns: int = 0
    cost: float = 0.0
    input_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    recache_cc: int = 0
    compactions: int = 0
    first_turn_write: int | None = None
    #: The model family of that first call: the same start measures
    #: differently on each model, so the arms are compared on one.
    first_turn_family: str = ""


def _collect_session_metrics(
    corpus: Corpus,
    pricing: Pricing,
    config: Config,
    session_overrides: dict,
    recache_th: recache.RecacheThresholds,
    coverage: PricingCoverage,
    profile_marks: list[snapshots_mod.ProfileMark] | None = None,
) -> list[_SessionMetrics]:
    """Classify and price every session in ``corpus`` once, folding pricing
    coverage into ``coverage`` as it goes (mirrors
    ``report.build_report``'s own accumulation, standalone here since
    ``report.py`` is not writable for this work package -- see
    ``cli.py``'s own ``_build_session_metrics`` for the same "standalone
    recomputation from public APIs" precedent this follows).
    """
    mode_thresholds, purpose_thresholds = classify.mode_and_purpose_thresholds_from_config(config.thresholds)
    results: list[_SessionMetrics] = []
    for bundle in corpus.sessions:
        if bundle.top is None:
            continue
        subs = bundle.subs
        # Same redaction point report.py itself uses, so a project slug
        # built from a real filesystem path never carries a raw username
        # into a compare table (privacy invariant, model.py's docstring).
        slug = discovery.redact_slug(bundle.slug)

        classification = classify.classify_session(
            bundle.top,
            subs,
            session_overrides,
            config.tz,
            workflows=len(bundle.workflows),
            entrypoint=bundle.top.meta.entrypoint,
            mode_thresholds=mode_thresholds,
            purpose_thresholds=purpose_thresholds,
        )
        record = classify.build_session_record(bundle.top, subs, bundle.workflows, classification, slug)

        sm = _SessionMetrics(
            session_id=record.session_id,
            slug=slug,
            first_ts=record.first_ts,
            span_s=record.span_s,
            mode=classification.mode,
            purpose=classification.purpose,
            profile_id=snapshots_mod.profile_for(record.first_ts, profile_marks or [], record.session_id),
            project_key=snapshots_mod.snapshot_project_keys(bundle.slug),
            task=classify.reported_task(bundle.top)[0] or "untagged",
        )

        first_priced = _priced_turns(bundle.top)
        if first_priced:
            sm.first_turn_write = first_priced[0].cache_creation_tokens
            sm.first_turn_family = model_family(first_priced[0].model)

        for tr in _transcripts_of(bundle):
            flagged = recache.detect(tr.turns, recache_th)
            sm.recache_cc += sum(t.cache_creation_tokens for t in flagged)
            for turn in _priced_turns(tr):
                resolved = pricing.resolve_model(turn.model)
                breakdown = price_turn(turn, resolved)
                coverage.add(turn, breakdown)
                sm.cost += breakdown.total
                sm.priced_turns += 1
                sm.input_tokens += turn.input_tokens
                sm.cache_creation_tokens += turn.cache_creation_tokens
                sm.cache_read_tokens += turn.cache_read_tokens

        # Fix cli.py's own _build_session_metrics precedent: top-level
        # transcript only, matching "did *this session's* own settings
        # take effect" rather than folding in subagent compactions too.
        sm.compactions = sum(1 for e in bundle.top.events if e.kind == EventKind.COMPACT_BOUNDARY)

        results.append(sm)
    return results


def _value_matches(raw, wanted: str) -> bool:
    """Whether a flattened snapshot value ``raw`` matches the CLI string
    ``wanted`` -- a plain string compare (case-insensitive as a fallback,
    since a boolean/number's ``str()`` form and a human-typed value can
    differ only in case, e.g. ``True`` vs ``true``).
    """
    if raw is None:
        return False
    text = str(raw)
    return text == wanted or text.lower() == wanted.lower()


def _session_matches(sm: _SessionMetrics, spec: ArmSpec, snapshot_by_session: dict[str, Snapshot | None]) -> bool:
    if spec.kind == "window":
        dt = _parse_ts(sm.first_ts)
        if dt is None:
            return False
        if spec.since_dt is not None and dt < spec.since_dt:
            return False
        if spec.until_dt is not None and dt > spec.until_dt:
            return False
        return True
    if spec.kind == "key":
        snap = snapshot_by_session.get(sm.session_id)
        if snap is None:
            return False
        flat = snapshots_mod.flatten_snapshot(snap)
        return _value_matches(flat.get(spec.key), spec.value)
    if spec.kind == "profile":
        return spec.profile_id is not None and sm.profile_id == spec.profile_id
    if spec.kind == "project":
        return sm.slug in spec.projects
    return False


# -- aggregation --------------------------------------------------------------

#: (field name in the dict _aggregate returns, display label, Column.kind)
#: -- the headline metrics for compare_overview.
#:
#: Review finding S4: the original ordering put arm *totals* (cost,
#: new_tokens, priced_turns) at the top of the table, so an arm with more
#: sessions than the other always showed a large headline delta driven
#: purely by arm size rather than by any behavioural difference. The
#: per-session-session metrics now carry the headline rows; the raw totals
#: are kept further down as separate, clearly-labelled "Total ..." rows so
#: the information is not lost, just no longer mistaken for a rate.
_METRIC_SPECS: tuple[tuple[str, str, str], ...] = (
    ("sessions", "Sessions", "int"),
    ("priced_turns_per_session", "Priced turns per session", "float"),
    ("cost_per_session", "Cost per session", "money"),
    ("new_tokens_per_session", "New tokens per session (input + cache-creation)", "tokens"),
    ("cache_read_share_pct", "Cache-read share of tokens processed", "pct"),
    ("recache_share_pct", "Re-cache share of cache-creation", "pct"),
    ("compactions_per_session", "Compactions per session", "float"),
    ("median_span_s", "Median session span", "secs"),
    ("mean_first_turn_write", "Mean first-turn cache-creation write", "tokens"),
    ("priced_turns", "Total priced turns (informational)", "int"),
    ("cost", "Total cost (informational)", "money"),
    ("new_tokens", "Total new tokens (informational)", "tokens"),
)


def _held_family(group_a: list[_SessionMetrics], group_b: list[_SessionMetrics]) -> str | None:
    """The model family the first-turn write is compared on: the one both
    arms' first calls ran on most, ties to the first by name. ``None`` when
    the arms share none (mirrors ``impact._held_model``)."""
    counts = [
        Counter(m.first_turn_family for m in group if m.first_turn_write is not None and m.first_turn_family)
        for group in (group_a, group_b)
    ]
    shared = set(counts[0]) & set(counts[1])
    if not shared:
        return None
    return max(sorted(shared), key=lambda family: counts[0][family] + counts[1][family])


def _aggregate(group: list[_SessionMetrics], family: str | None = None) -> dict[str, float | None]:
    sessions = len(group)
    priced_turns = sum(m.priced_turns for m in group)
    cost = sum(m.cost for m in group)
    new_tokens = sum(m.input_tokens + m.cache_creation_tokens for m in group)
    cache_read_tokens = sum(m.cache_read_tokens for m in group)
    denom = new_tokens + cache_read_tokens
    cache_read_share_pct = (100.0 * cache_read_tokens / denom) if denom > 0 else None
    cache_creation_total = sum(m.cache_creation_tokens for m in group)
    recache_cc_total = sum(m.recache_cc for m in group)
    recache_share_pct = (100.0 * recache_cc_total / cache_creation_total) if cache_creation_total > 0 else None
    compactions_total = sum(m.compactions for m in group)
    compactions_per_session = (compactions_total / sessions) if sessions > 0 else None
    spans = [m.span_s for m in group]
    median_span_s = statistics.median(spans) if spans else None
    writes = [
        m.first_turn_write
        for m in group
        if m.first_turn_write is not None and (family is None or m.first_turn_family == family)
    ]
    mean_first_turn_write = statistics.mean(writes) if writes else None
    # S4: per-session means so that a headline delta reflects a behavioural
    # difference rather than one arm simply having more sessions than the
    # other. The raw totals are still returned (and shown) separately.
    cost_per_session = (cost / sessions) if sessions > 0 else None
    new_tokens_per_session = (new_tokens / sessions) if sessions > 0 else None
    priced_turns_per_session = (priced_turns / sessions) if sessions > 0 else None
    return {
        "sessions": float(sessions),
        "priced_turns": float(priced_turns),
        "cost": cost,
        "new_tokens": float(new_tokens),
        "cache_read_share_pct": cache_read_share_pct,
        "recache_share_pct": recache_share_pct,
        "compactions_per_session": compactions_per_session,
        "median_span_s": median_span_s,
        "mean_first_turn_write": mean_first_turn_write,
        "cost_per_session": cost_per_session,
        "new_tokens_per_session": new_tokens_per_session,
        "priced_turns_per_session": priced_turns_per_session,
    }


def _pct_of_a(value_a: float | None, value_b: float | None) -> float | None:
    if value_a is None or value_b is None or value_a == 0:
        return None
    return (value_b - value_a) / value_a * 100.0


def _fmt_metric_row(label: str, kind: str, value_a, value_b, currency: str) -> list:
    """One ``compare_overview`` row: metric label plus pre-formatted arm
    A/B/delta/delta-% text -- see module docstring for why these cells
    are pre-formatted strings rather than raw numbers.
    """
    a_str = format_cell(value_a, kind, currency)
    b_str = format_cell(value_b, kind, currency)
    if value_a is None or value_b is None:
        return [label, a_str, b_str, "-", "-"]
    delta_raw = value_b - value_a
    delta_str = ("+" if delta_raw > 0 else "") + format_cell(delta_raw, kind, currency)
    pct = _pct_of_a(value_a, value_b)
    delta_pct_str = "n/a (Arm A = 0)" if pct is None else ("+" if pct > 0 else "") + f"{pct:.1f}%"
    return [label, a_str, b_str, delta_str, delta_pct_str]


def _build_overview_table(
    group_a: list[_SessionMetrics], group_b: list[_SessionMetrics], min_sessions: int, currency: str
) -> Table:
    family = _held_family(group_a, group_b)
    agg_a = _aggregate(group_a, family)
    agg_b = _aggregate(group_b, family)
    if family is None:
        agg_a["mean_first_turn_write"] = agg_b["mean_first_turn_write"] = None
    sample_ok = "yes" if (len(group_a) >= min_sessions and len(group_b) >= min_sessions) else "no"
    rows = []
    for field_name, label, kind in _METRIC_SPECS:
        rows.append([*_fmt_metric_row(label, kind, agg_a[field_name], agg_b[field_name], currency), sample_ok])
    columns = [
        Column(key="metric", label="Metric", kind="str"),
        Column(key="arm_a", label="Arm A", kind="str"),
        Column(key="arm_b", label="Arm B", kind="str"),
        Column(key="delta", label="Delta (B - A)", kind="str"),
        Column(key="delta_pct", label="Delta (% of A)", kind="str"),
        Column(key="sample_ok", label="Sample OK", kind="str"),
    ]
    notes = [
        f"Sample size: Arm A={len(group_a)} session(s), Arm B={len(group_b)} session(s); "
        f"minimum required per arm={min_sessions}."
    ]
    if family:
        notes.append(
            f"Mean first-turn write compares sessions on {family} only: "
            "the same start measures differently on each model."
        )
    else:
        notes.append("No model ran first calls in both arms, so mean first-turn write is not compared.")
    return Table(name="compare_overview", title="Overview: Arm A vs Arm B", columns=columns, rows=rows, notes=notes)


def _stratum_key(sm: _SessionMetrics, stratify_by: tuple[str, ...]) -> tuple[str, ...]:
    field_map = {"purpose": sm.purpose, "mode": sm.mode, "task": sm.task}
    return tuple(field_map.get(f, "") for f in stratify_by)


def _stratum_label(key_tuple: tuple[str, ...], stratify_by: tuple[str, ...]) -> str:
    if not stratify_by:
        return "all"
    return ", ".join(f"{f}={v}" for f, v in zip(stratify_by, key_tuple))


def _build_stratum_table(
    group_a: list[_SessionMetrics],
    group_b: list[_SessionMetrics],
    stratify_by: tuple[str, ...],
    min_sessions: int,
    currency: str,
) -> Table:
    by_a: dict[tuple[str, ...], list[_SessionMetrics]] = {}
    by_b: dict[tuple[str, ...], list[_SessionMetrics]] = {}
    for m in group_a:
        by_a.setdefault(_stratum_key(m, stratify_by), []).append(m)
    for m in group_b:
        by_b.setdefault(_stratum_key(m, stratify_by), []).append(m)
    all_keys = sorted(set(by_a) | set(by_b))

    # S4: as with compare_overview, per-stratum cost/token figures are shown
    # as per-session means so a stratum with more sessions in one arm than
    # the other doesn't produce a delta driven by arm size alone.
    columns = [
        Column(key="stratum", label="Stratum", kind="str"),
        Column(key="sessions_a", label="Sessions (A)", kind="int"),
        Column(key="sessions_b", label="Sessions (B)", kind="int"),
        Column(key="sample_ok", label="Sample OK", kind="str"),
        Column(key="cost_a", label="Cost per session (A)", kind="money"),
        Column(key="cost_b", label="Cost per session (B)", kind="money"),
        Column(key="cost_delta_pct", label="Cost per session delta (% of A)", kind="pct"),
        Column(key="new_tokens_a", label="New tokens per session (A)", kind="tokens"),
        Column(key="new_tokens_b", label="New tokens per session (B)", kind="tokens"),
        Column(key="new_tokens_delta_pct", label="New tokens per session delta (% of A)", kind="pct"),
        Column(key="cache_read_share_a", label="Cache-read share (A)", kind="pct"),
        Column(key="cache_read_share_b", label="Cache-read share (B)", kind="pct"),
        Column(key="note", label="Note", kind="str"),
    ]
    rows = []
    for key in all_keys:
        ga = by_a.get(key, [])
        gb = by_b.get(key, [])
        na, nb = len(ga), len(gb)
        if na < min_sessions or nb < min_sessions:
            rows.append(
                [
                    _stratum_label(key, stratify_by),
                    na,
                    nb,
                    "no",
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    f"suppressed: needs >={min_sessions} session(s) per arm (A has {na}, B has {nb})",
                ]
            )
            continue
        agg_a = _aggregate(ga)
        agg_b = _aggregate(gb)
        rows.append(
            [
                _stratum_label(key, stratify_by),
                na,
                nb,
                "yes",
                agg_a["cost_per_session"],
                agg_b["cost_per_session"],
                _pct_of_a(agg_a["cost_per_session"], agg_b["cost_per_session"]),
                agg_a["new_tokens_per_session"],
                agg_b["new_tokens_per_session"],
                _pct_of_a(agg_a["new_tokens_per_session"], agg_b["new_tokens_per_session"]),
                agg_a["cache_read_share_pct"],
                agg_b["cache_read_share_pct"],
                "",
            ]
        )
    notes = [
        f"Minimum sample per arm: {min_sessions} session(s); a stratum below this in either arm is shown with its "
        "session counts only, its metrics suppressed (see the Note column)."
    ]
    return Table(
        name="compare_by_stratum",
        title=f"By stratum ({', '.join(stratify_by) if stratify_by else 'none'})",
        columns=columns,
        rows=rows,
        notes=notes,
    )


def _representative_snapshot(
    group: list[_SessionMetrics], snapshot_by_session: dict[str, Snapshot | None]
) -> Snapshot | None:
    """The most common snapshot ``group``'s sessions actually joined to
    (ties broken by latest ``ts``), or ``None`` if none joined any.
    """
    by_ts: dict[str, list[Snapshot]] = {}
    for m in group:
        snap = snapshot_by_session.get(m.session_id)
        if snap is None:
            continue
        by_ts.setdefault(snap.ts, []).append(snap)
    if not by_ts:
        return None
    best_ts = max(by_ts, key=lambda ts: (len(by_ts[ts]), ts))
    return by_ts[best_ts][0]


def _build_co_changed_table(
    group_a: list[_SessionMetrics],
    group_b: list[_SessionMetrics],
    arm_a: ArmSpec,
    arm_b: ArmSpec,
    snapshot_by_session: dict[str, Snapshot | None],
) -> Table:
    columns = [
        Column(key="key", label="Key", kind="str"),
        Column(key="arm_a_value", label="Arm A value", kind="str"),
        Column(key="arm_b_value", label="Arm B value", kind="str"),
    ]
    if arm_a.kind != "key" or arm_b.kind != "key":
        return Table(
            name="compare_co_changed",
            title="Co-changed config keys",
            columns=columns,
            rows=[],
            notes=[
                "Only computed when both arms are snapshot-keyed (key:<key>=<value> specs); "
                "at least one arm here is selected a different way, so there is nothing to compare."
            ],
        )

    rep_a = _representative_snapshot(group_a, snapshot_by_session)
    rep_b = _representative_snapshot(group_b, snapshot_by_session)
    if rep_a is None or rep_b is None:
        return Table(
            name="compare_co_changed",
            title="Co-changed config keys",
            columns=columns,
            rows=[],
            notes=["No sessions in one or both arms joined to a config snapshot; nothing to compare."],
        )

    changed = [key for key in snapshots_mod.co_changed_keys(rep_a, rep_b) if key != arm_a.key]
    flat_a = snapshots_mod.flatten_snapshot(rep_a)
    flat_b = snapshots_mod.flatten_snapshot(rep_b)
    rows = [[key, str(flat_a.get(key)), str(flat_b.get(key))] for key in changed]
    notes = [
        f"Representative snapshots: Arm A={rep_a.ts}, Arm B={rep_b.ts} (the snapshot most of each arm's own "
        "sessions actually joined to). A given session's own joined snapshot may differ from this "
        "representative one if an arm's sessions span more than one snapshot.",
    ]
    if not rows:
        notes.append("No other allowlisted key differed between the two representative snapshots.")
    return Table(name="compare_co_changed", title="Co-changed config keys", columns=columns, rows=rows, notes=notes)


# -- entry point ---------------------------------------------------------


def compare(
    corpus: Corpus,
    pricing: Pricing,
    config: Config,
    *,
    arm_a: ArmSpec,
    arm_b: ArmSpec,
    stratify_by: tuple[str, ...] | None = None,
    min_sessions: int = 5,
    snapshots: list[Snapshot] | None = None,
    session_overrides: dict | None = None,
    config_dir: str | Path | None = None,
) -> Section:
    """Build the A/B ``compare`` :class:`Section`: ``compare_overview``,
    ``compare_by_stratum``, ``compare_co_changed``.

    ``arm_a``/``arm_b`` select sessions independently -- a session may
    belong to both, neither, or exactly one arm, since each spec is a
    membership test, not a partition. ``snapshots`` is required for
    ``key:``-kind arms (a session with no config snapshots loaded never
    matches one); ``session_overrides`` is the ``sessions.toml``-shaped
    dict :func:`config.load_session_overrides` produces, passed straight
    through to :func:`classify.classify_session` exactly as
    ``report.build_report`` does. ``config_dir`` (read only) gives
    ``profile:`` arms the full record of which profile was active when;
    without it they use the hook captures in ``snapshots``.
    ``stratify_by`` (keys from :data:`STRATIFY_CHOICES`): ``None`` means
    purpose and mode, plus the reported task once at least
    :data:`TASK_COVERAGE_PCT` of both arms' sessions have one.

    Never raises for a too-small sample: an arm with fewer than
    ``min_sessions`` sessions still gets a full ``compare_overview`` row
    set, just with ``sample_ok="no"`` on every row (the CLI's contract:
    print what there is and exit 0, per the plan's minimum-sample gate
    being a caveat on the reading, not a reason to refuse to show data).
    """
    session_overrides = session_overrides or {}
    recache_th = recache.RecacheThresholds.from_config(config.thresholds)
    coverage = PricingCoverage()
    profile_marks = (
        snapshots_mod.load_profile_marks(config_dir)
        if config_dir is not None
        else snapshots_mod.profile_marks_from_snapshots(snapshots or [])
    )
    metrics = _collect_session_metrics(corpus, pricing, config, session_overrides, recache_th, coverage, profile_marks)

    snapshot_by_session: dict[str, Snapshot | None] = {}
    if snapshots:
        for m in metrics:
            snapshot_by_session[m.session_id] = snapshots_mod.snapshot_for(m.first_ts, snapshots, m.project_key) if m.first_ts else None

    group_a = [m for m in metrics if _session_matches(m, arm_a, snapshot_by_session)]
    group_b = [m for m in metrics if _session_matches(m, arm_b, snapshot_by_session)]

    if stratify_by is None:
        both = group_a + group_b
        tagged = sum(1 for m in both if m.task != "untagged")
        covered = bool(both) and 100.0 * tagged / len(both) >= TASK_COVERAGE_PCT
        stratify_by = (*_STRATIFY_KEYS, "task") if covered else _STRATIFY_KEYS

    currency = pricing.currency
    overview_table = _build_overview_table(group_a, group_b, min_sessions, currency)
    stratum_table = _build_stratum_table(group_a, group_b, stratify_by, min_sessions, currency)
    co_changed_table = _build_co_changed_table(group_a, group_b, arm_a, arm_b, snapshot_by_session)

    section_notes = [
        "Observed, not controlled: sessions in each arm also differ in workload, so a difference here is not "
        "evidence that the arm's own setting caused it (plan 'Risks and gaps': correlation is not causation).",
        f"Arm A selection: {arm_a.label}",
        f"Arm B selection: {arm_b.label}",
    ]
    for table in (overview_table, stratum_table, co_changed_table):
        table.notes = [*section_notes, *table.notes]

    return Section(
        key="compare",
        title="A/B compare",
        tables=[overview_table, stratum_table, co_changed_table],
        notes=section_notes,
    )


__all__ = ["ArmSpec", "STRATIFY_CHOICES", "TASK_COVERAGE_PCT", "parse_arm_spec", "compare"]

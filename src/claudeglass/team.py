"""Team aggregate export/import/report (v0.3 Task 1).

Three pieces, matching the plan's own split:

* :func:`machine_id` + :func:`build_team_aggregate` -- ``export --aggregate``
  turns one machine's local corpus into a small, JSON-serialisable
  document: a stable-but-non-reversible per-machine id, the report
  window, and per-group aggregates (never a session id, never a raw
  slug) across five axes (archetype/mode/purpose/agent_type/model),
  plus the corpus-wide scorecard levels.
* :func:`validate_team_document` + :func:`save_team_document` +
  :func:`load_latest_team_documents` -- ``import``'s own contract: a
  document is schema-checked against an explicit key allowlist and a
  64-character string cap before it is ever written to
  ``<config_dir>/team/``, and a stale copy from an old run of the same
  machine is superseded (kept: latest ``generated_at`` per
  ``machine_id``) rather than piling up forever.
* :func:`build_team_report_section` -- ``team-report``'s per-archetype
  and per-agent-type comparison tables across machines, gated by the
  same minimum-sample rule the rest of the codebase uses.

Privacy (the whole point of this module): no session id, slug (hashed
or not, unless ``include_projects``), hostname, username or filesystem
path ever reaches a built document -- only counts, sums, means and
percentages keyed by an already-coarse group label. ``machine_id`` uses
the same salted-HMAC construction :func:`exports._hash_slug` documents
(domain-tag + truncation-length separated so namespaces never collide),
keyed by the config dir's own salt (:func:`parse.load_or_create_salt`),
so it is stable for one machine/config dir but never reveals or
reverses to the hostname it was built from.

Review finding S10: the ``by_agent_type`` axis's group value is a
transcript's ``TranscriptMeta.agent_type`` -- for a project-defined
custom subagent (as opposed to one of Claude Code's own bundled agent
types) this is frequently a product- or project-named string (e.g. a
project's own reviewer/implementer agent names), which is exactly the
kind of identifying detail this module otherwise goes out of its way
never to export. ``_agent_type_group_label`` keeps the synthetic
``"top-level"``/``"unknown"`` labels and every entry of
:data:`recommend._BUILTIN_AGENT_TYPES` verbatim (there is nothing
project-identifying about a stock agent type), and hashes anything else
-- a custom agent name -- to ``custom:<8 hex chars>`` using the same
salted-HMAC construction as :func:`exports._hash_slug`, just its own
domain tag and truncation length so the namespace can't collide with
the project-slug or machine-id ones.

Deviation (report, don't silently resolve): ``compaction_rate`` for the
per-transcript axes (``by_agent_type``, ``by_model``) is really "the
mean compactions-per-session of sessions that used this agent type/
model at least once", not "compactions caused by this agent type/
model" -- ``compaction.CompactionStats`` records one compaction event
per *session*, not per transcript, so a session with two agent types
attributes its whole compaction count to both. This matches the same
session-level attribution ``report.py``'s own ``current_by_mode``
block already uses for the (session-level) ``by_mode`` axis, just
carried over to the transcript-level axes where it is a slightly
looser statement -- flagged here rather than building a
transcript-level compaction attribution that would need changes to
``compaction.py`` (not writable this round).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import platform
import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from . import classify, compaction, recache, scorecard, ttl, workstyle
from .exports import _TOOL_VERSION, _hash_slug
from .model import Column, Section, Table
from .parse import load_or_create_salt
from .pricing import price_turn
from .recommend import _BUILTIN_AGENT_TYPES
from .render.tables import format_cell
from .report import (
    _dominant_transcript_model,
    _extract_workstyle_features,
    _priced_turns,
    _transcripts_of,
    build_report,
    scorecard_dimensions_metric,
)

if TYPE_CHECKING:
    from .config import Config
    from .corpus import Corpus
    from .pricing import Pricing
    from .snapshots import Snapshot

#: The five grouping axes ``export --aggregate``/``team-report`` share,
#: in the order the plan lists them and the order documents/tables
#: present them.
GROUP_AXES: tuple[str, ...] = ("archetype", "mode", "purpose", "agent_type", "model")

#: Default minimum-sample threshold for both ``build_team_aggregate``'s
#: own cells (rows below this are still emitted -- only ``sessions``
#: is ever suppressed nowhere in the export itself, since the
#: aggregate is one machine's own data) and ``team-report``'s
#: cross-machine comparison cells (which *are* gated -- see
#: :func:`build_team_report_section`).
MIN_SESSIONS = 5

#: ``machine_id`` is always exactly 12 lowercase hex characters -- the
#: truncated HMAC digest :func:`machine_id` produces. A document whose
#: ``machine_id`` doesn't match this shape is rejected outright rather
#: than trusted as a filename component (review B1): an untrusted
#: document's ``machine_id`` reaches ``save_team_document``'s output
#: path verbatim, so anything looser than "exactly what our own
#: exporter writes" is a traversal surface.
_MACHINE_ID_RE = re.compile(r"^[0-9a-f]{12}\Z")

#: ``generated_at`` is always the ISO-8601 UTC "basic" shape the
#: exporter writes (``_resolve_generated_at`` in ``cli.py`` can also
#: produce the no-fractional-seconds form via ``SOURCE_DATE_EPOCH`` /
#: ``datetime.isoformat()``, hence the optional fractional group) --
#: never anything containing ``/`` or ``\`` or ``..`` that could
#: escape the team directory once it reaches a filename (review B1).
_GENERATED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")

_TOP_LEVEL_KEYS = {
    "tool_version",
    "generated_at",
    "machine_id",
    "window",
    "by_archetype",
    "by_mode",
    "by_purpose",
    "by_agent_type",
    "by_model",
    "scorecard",
    "projects",
}
_GROUP_ROW_KEYS = {
    "value",
    "sessions",
    "priced_turns",
    "tokens",
    "cost_usd",
    "recache_share_pct",
    "compaction_rate",
    "ttl_mix",
    "mean_spawn_write",
    "mean_report_size",
}
_TOKEN_KEYS = {"input", "cache_creation", "cache_read", "output"}
_TTL_MIX_KEYS = {"5m_pct", "1h_pct"}
_GROUP_AXIS_KEYS = tuple(f"by_{axis}" for axis in GROUP_AXES)
_MAX_STRING_LEN = 64


# -- machine id ---------------------------------------------------------


def machine_id(config_dir: str | Path) -> str:
    """A stable-per-machine, non-reversible id: the first 12 hex
    characters of a salted HMAC-SHA256 over this machine's hostname
    (``platform.node()``), keyed by ``<config_dir>/salt``.

    Same HMAC construction as :func:`exports._hash_slug` (salt as the
    HMAC key, a domain tag, SHA-256, truncated to hex characters) but
    with its own ``"machine:"`` tag -- so this id's namespace can never
    collide with the project-slug namespace even if both truncate to
    the same length, matching the separation ``exports._hash_slug``'s
    own docstring documents against ``parse.py``'s read-target hash.
    Stable across runs (same hostname + same salt file -> same id) and
    stable across ``export --aggregate`` calls on the same machine, but
    not reversible to the hostname without the salt.
    """
    salt = load_or_create_salt(config_dir)
    digest = hmac.new(salt, b"machine:" + platform.node().encode("utf-8"), hashlib.sha256).hexdigest()
    return digest[:12]


# -- aggregation ----------------------------------------------------------


@dataclass(slots=True)
class _GroupAcc:
    """Running totals for one group value on one axis, before the
    per-group percentages/means below are derived from them."""

    session_ids: set[str] = field(default_factory=set)
    priced_turns: int = 0
    input_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    recache_cc: int = 0
    total_cc: int = 0
    cc_5m: int = 0
    cc_1h: int = 0
    spawn_writes: list[int] = field(default_factory=list)
    report_sizes: list[int] = field(default_factory=list)


def _agent_type_label(tr, top) -> str:
    """Duplicated from ``topology._agent_type_label``'s own
    ``"top-level" if result is top else (result.meta.agent_type or
    "unknown")`` convention -- ``topology.py`` isn't writable this
    round, so this tiny one-liner is kept locally rather than imported,
    the same small-helper-duplication convention ``report.py``'s own
    module docstring documents for this exact function.
    """
    return "top-level" if tr is top else (tr.meta.agent_type or "unknown")


def _hash_custom_agent_type(agent_type: str, salt: bytes) -> str:
    """First 8 hex characters of a salted HMAC-SHA256 over a custom
    (non-built-in) agent type name, domain-separated with an
    ``agent_type:`` tag.

    Same HMAC construction as :func:`exports._hash_slug` (salt as the
    HMAC key, SHA-256) but its own domain tag and truncation length, so
    this namespace can never collide with the project-slug or
    machine-id namespaces even if two happened to truncate to the same
    length (review S10, matching the separation
    :func:`exports._hash_slug`'s own docstring documents against
    :func:`machine_id`).
    """
    digest = hmac.new(salt, b"agent_type:" + agent_type.encode("utf-8"), hashlib.sha256).hexdigest()
    return digest[:8]


def _agent_type_group_label(agent_type: str, salt: bytes) -> str:
    """The ``by_agent_type`` axis's exported group value for one raw
    ``agent_type`` (review S10).

    ``"top-level"``/``"unknown"`` (the synthetic labels
    :func:`_agent_type_label` itself produces) and every entry of
    :data:`recommend._BUILTIN_AGENT_TYPES` (Claude Code's own bundled
    agent types) are kept verbatim -- there is nothing project-
    identifying about a stock agent type. Anything else is a project- or
    user-defined custom agent, frequently named after the project or
    its own conventions, so it is hashed rather than exported as
    plaintext, the same privacy posture already applied to project
    slugs elsewhere in this module.
    """
    if agent_type in ("top-level", "unknown") or agent_type in _BUILTIN_AGENT_TYPES:
        return agent_type
    return f"custom:{_hash_custom_agent_type(agent_type, salt)}"


def _build_axis_buckets(
    corpus: "Corpus", pricing: "Pricing", config: "Config"
) -> tuple[dict[str, dict[str, _GroupAcc]], dict[str, int]]:
    """One pass over every transcript in ``corpus``, fanning each
    transcript's own turn-level totals out into all five axis buckets
    at once (computed once per transcript, not once per axis). Returns
    the axis buckets plus a ``{session_id: compaction_count}`` map
    (see the module docstring's compaction-rate deviation note).
    """
    recache_th = recache.RecacheThresholds.from_config(config.thresholds)
    ttl_th = ttl.TtlThresholds.from_config(config.thresholds)
    cs = compaction.CompactionStats()

    axes: dict[str, dict[str, _GroupAcc]] = {axis: {} for axis in GROUP_AXES}

    for bundle in corpus.sessions:
        top = bundle.top
        if top is None:
            continue
        subs = bundle.subs
        classification = classify.classify_session(top, subs, {}, config.tz)
        session_id = top.meta.session_id
        features = _extract_workstyle_features(top, subs, bundle.workflows)
        archetype, _evidence = workstyle.detect_archetype(features)

        for tr in _transcripts_of(bundle):
            dominant_model = _dominant_transcript_model(tr)
            dominant_rate = pricing.resolve_model(dominant_model) if dominant_model else None
            cs.add_transcript(tr, dominant_rate, recache_th)

            priced = _priced_turns(tr)
            if not priced:
                continue

            turns_input = turns_cc = turns_cr = turns_out = 0
            turns_cost = 0.0
            for turn in priced:
                resolved = pricing.resolve_model(turn.model)
                breakdown = price_turn(turn, resolved)
                turns_input += turn.input_tokens
                turns_cc += turn.cache_creation_tokens
                turns_cr += turn.cache_read_tokens
                turns_out += turn.output_tokens
                turns_cost += breakdown.total

            flagged = recache.detect(tr.turns, recache_th)
            recache_cc = sum(t.cache_creation_tokens for t in flagged)

            normalized = ttl.normalize_ttl_split(tr.turns, ttl_th)
            normalized_priced = [t for t in normalized if t.turn_index > 0]
            cc_5m = sum(t.cc_5m for t in normalized_priced)
            cc_1h = sum(t.cc_1h for t in normalized_priced)

            is_subagent = tr is not top
            spawn_write = priced[0].cache_creation_tokens if is_subagent else None
            report_size = priced[-1].output_tokens if is_subagent else None

            dominant_model_label = dominant_model or "<unknown>"
            agent_label = _agent_type_label(tr, top)

            for axis_name, group_value in (
                ("archetype", archetype),
                ("mode", classification.mode),
                ("purpose", classification.purpose),
                ("agent_type", agent_label),
                ("model", dominant_model_label),
            ):
                acc = axes[axis_name].setdefault(group_value, _GroupAcc())
                acc.session_ids.add(session_id)
                acc.priced_turns += len(priced)
                acc.input_tokens += turns_input
                acc.cache_creation_tokens += turns_cc
                acc.cache_read_tokens += turns_cr
                acc.output_tokens += turns_out
                acc.cost += turns_cost
                acc.recache_cc += recache_cc
                acc.total_cc += turns_cc
                acc.cc_5m += cc_5m
                acc.cc_1h += cc_1h
                if spawn_write is not None:
                    acc.spawn_writes.append(spawn_write)
                if report_size is not None:
                    acc.report_sizes.append(report_size)

    compaction_counts = {sid: count for sid, count, _dropped, _cost in cs.per_session_summary()}
    return axes, compaction_counts


def _group_row(value: str, acc: _GroupAcc, compaction_counts: dict[str, int]) -> dict:
    n_sessions = len(acc.session_ids)
    ttl_denom = acc.cc_5m + acc.cc_1h
    return {
        "value": value,
        "sessions": n_sessions,
        "priced_turns": acc.priced_turns,
        "tokens": {
            "input": acc.input_tokens,
            "cache_creation": acc.cache_creation_tokens,
            "cache_read": acc.cache_read_tokens,
            "output": acc.output_tokens,
        },
        "cost_usd": round(acc.cost, 6),
        # 0.0 (not None) on a zero denominator, matching recache.py's
        # own _pct() convention and report.py's current_by_mode block.
        "recache_share_pct": round(100.0 * acc.recache_cc / acc.total_cc, 4) if acc.total_cc > 0 else 0.0,
        "compaction_rate": (
            round(sum(compaction_counts.get(sid, 0) for sid in acc.session_ids) / n_sessions, 4)
            if n_sessions
            else None
        ),
        "ttl_mix": {
            "5m_pct": round(100.0 * acc.cc_5m / ttl_denom, 2) if ttl_denom > 0 else None,
            "1h_pct": round(100.0 * acc.cc_1h / ttl_denom, 2) if ttl_denom > 0 else None,
        },
        "mean_spawn_write": round(statistics.fmean(acc.spawn_writes), 2) if acc.spawn_writes else None,
        "mean_report_size": round(statistics.fmean(acc.report_sizes), 2) if acc.report_sizes else None,
    }


def build_team_aggregate(
    corpus: "Corpus",
    pricing: "Pricing",
    config: "Config",
    config_dir: str | Path,
    *,
    window: str,
    projects: tuple[str, ...] = (),
    include_projects: bool = False,
    snapshots: list["Snapshot"] | None = None,
    generated_at: str | None = None,
) -> dict:
    """Build one ``export --aggregate`` team document for ``corpus``.

    Aggregate-only by construction: nothing here is keyed or grouped by
    session id, and no slug is included at all unless
    ``include_projects=True``, in which case only its
    :func:`exports._hash_slug` hash is (never the plaintext slug) --
    matching the plan's "no session ids, no slugs (even hashed) unless
    ``--include-projects``, in which case hashed slugs only".
    """
    axes, compaction_counts = _build_axis_buckets(corpus, pricing, config)

    model = build_report(corpus, pricing, config, projects=projects, window=window, snapshots=snapshots)

    # Loaded unconditionally (not just under include_projects) because
    # the by_agent_type axis now hashes custom agent names regardless of
    # that flag -- see _agent_type_group_label (review S10).
    salt = load_or_create_salt(config_dir)

    document: dict = {
        "tool_version": _TOOL_VERSION,
        "generated_at": generated_at or (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + "000Z"),
        "machine_id": machine_id(config_dir),
        "window": window,
        "scorecard": scorecard_dimensions_metric(model.sections),
    }
    for axis in GROUP_AXES:
        if axis == "agent_type":
            rows = [
                _group_row(_agent_type_group_label(value, salt), acc, compaction_counts)
                for value, acc in axes[axis].items()
            ]
        else:
            rows = [_group_row(value, acc, compaction_counts) for value, acc in axes[axis].items()]
        rows.sort(key=lambda row: (-row["sessions"], row["value"]))
        document[f"by_{axis}"] = rows

    if include_projects:
        document["projects"] = sorted({_hash_slug(p, salt) for p in projects if p})

    return document


# -- validation / storage -------------------------------------------------


def _walk_string_values(obj):
    """Every string *value* in ``obj`` (never a dict key), recursively --
    used only for the 64-character cap; disallowed keys are caught
    separately (and more precisely) by the structural checks in
    :func:`validate_team_document`.
    """
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for value in obj.values():
            yield from _walk_string_values(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_string_values(item)


def validate_team_document(doc) -> str | None:
    """``None`` when ``doc`` is a well-formed team-aggregate document
    matching exactly the shape :func:`build_team_aggregate` produces;
    otherwise a one-line reason naming the first problem found.

    ``import``'s own contract (v0.3 plan): "schema-checked, rejects
    anything with a key outside the allowlist or any string over 64
    chars, exit 2 with the reason" -- this function never raises, so
    the caller (``cli._cmd_import``) turns a non-``None`` return into
    that exit-2 message itself.
    """
    if not isinstance(doc, dict):
        return "top-level document is not a JSON object"

    extra = set(doc) - _TOP_LEVEL_KEYS
    if extra:
        return f"unexpected top-level key(s): {sorted(extra)}"
    for key in ("tool_version", "generated_at", "machine_id", "window"):
        if key not in doc:
            return f"missing required key: {key!r}"
        if not isinstance(doc[key], str):
            return f"{key!r} must be a string"

    if not _MACHINE_ID_RE.match(doc["machine_id"]):
        return "'machine_id' must be exactly 12 lowercase hex characters"
    if not _GENERATED_AT_RE.match(doc["generated_at"]):
        return "'generated_at' must be an ISO-8601 UTC timestamp such as '2026-09-19T00:00:00.000Z'"

    for axis_key in _GROUP_AXIS_KEYS:
        rows = doc.get(axis_key, [])
        if not isinstance(rows, list):
            return f"{axis_key!r} is not a list"
        for row in rows:
            if not isinstance(row, dict):
                return f"{axis_key!r} has a row that is not an object"
            row_extra = set(row) - _GROUP_ROW_KEYS
            if row_extra:
                return f"{axis_key!r} row has unexpected key(s): {sorted(row_extra)}"
            tokens = row.get("tokens")
            if tokens is not None and (not isinstance(tokens, dict) or set(tokens) - _TOKEN_KEYS):
                return f"{axis_key!r} row has an invalid 'tokens' object"
            ttl_mix = row.get("ttl_mix")
            if ttl_mix is not None and (not isinstance(ttl_mix, dict) or set(ttl_mix) - _TTL_MIX_KEYS):
                return f"{axis_key!r} row has an invalid 'ttl_mix' object"

    scorecard_obj = doc.get("scorecard")
    if scorecard_obj is not None:
        if not isinstance(scorecard_obj, dict) or set(scorecard_obj) - set(scorecard.ALL_DIMENSIONS):
            return "'scorecard' has an unexpected key"

    projects = doc.get("projects")
    if projects is not None and not isinstance(projects, list):
        return "'projects' is not a list"

    for value in _walk_string_values(doc):
        if len(value) > _MAX_STRING_LEN:
            return f"a string value exceeds {_MAX_STRING_LEN} characters: {value[:20]!r}..."

    return None


def team_dir(config_dir: str | Path) -> Path:
    return Path(config_dir) / "team"


def save_team_document(config_dir: str | Path, document: dict) -> Path:
    """Copy ``document`` into
    ``<config_dir>/team/<machine_id>-<generated_at>.json`` -- matching
    ``import``'s own contract exactly. Callers validate with
    :func:`validate_team_document` first, which already rejects a
    ``machine_id``/``generated_at`` shaped for path traversal (review
    B1); this function re-checks both against the same patterns and
    confirms the resolved path stays inside the team directory before
    writing, so a caller that skips validation can't be tricked into
    writing outside ``<config_dir>/team/`` either. The ``generated_at``
    timestamp is then sanitised into a filesystem-safe token since
    ``:`` isn't valid in a Windows filename.
    """
    directory = team_dir(config_dir)
    directory.mkdir(parents=True, exist_ok=True)
    machine = str(document.get("machine_id", "unknown"))
    generated_at_raw = str(document.get("generated_at", "unknown"))
    if not _MACHINE_ID_RE.match(machine):
        raise ValueError(f"refusing to save team document: invalid machine_id {machine!r}")
    if not _GENERATED_AT_RE.match(generated_at_raw):
        raise ValueError(f"refusing to save team document: invalid generated_at {generated_at_raw!r}")
    generated_at = generated_at_raw.replace(":", "").replace(".", "-")
    path = directory / f"{machine}-{generated_at}.json"
    resolved_directory = directory.resolve()
    if not path.resolve().is_relative_to(resolved_directory):
        raise ValueError("refusing to save team document outside the team directory")
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def load_latest_team_documents(config_dir: str | Path) -> list[dict]:
    """Every saved team document under ``<config_dir>/team/``, keeping
    only the latest (by ``generated_at``, string-sortable since it's
    always the same zero-padded ISO-8601 shape) per ``machine_id`` --
    matches ``team-report``'s "keeps the latest document per machine".
    A file that fails to parse, or fails :func:`validate_team_document`,
    is skipped rather than raising -- the same tolerant posture
    ``baseline.list_baselines`` documents for its own directory scan.
    """
    directory = team_dir(config_dir)
    if not directory.is_dir():
        return []
    latest: dict[str, dict] = {}
    for path in sorted(directory.glob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if validate_team_document(doc) is not None:
            continue
        machine = doc.get("machine_id")
        if not machine:
            continue
        existing = latest.get(machine)
        if existing is None or str(doc.get("generated_at", "")) > str(existing.get("generated_at", "")):
            latest[machine] = doc
    return list(latest.values())


# -- team-report ------------------------------------------------------------


def _format_team_cell(row: dict | None, min_sessions: int) -> str:
    if row is None:
        return "-"
    sessions = row.get("sessions") or 0
    if sessions < min_sessions:
        return f"n<{min_sessions}"
    cost_usd = row.get("cost_usd")
    cost_per_session = (cost_usd / sessions) if isinstance(cost_usd, (int, float)) and sessions else None
    if cost_per_session is None:
        return f"{sessions} session(s)"
    # UX-2: pooled across machines that may each be on a different
    # billing mode, so this can't be phrased as any one machine's share
    # of its own weekly usage limit -- forced to a plain currency-
    # suffixed number (never a bare "$") via format_cell with no `units`,
    # same fallback every other billing-mode-aware amount in this
    # codebase uses when it can't be phrased for a specific mode.
    return f"{format_cell(cost_per_session, 'money')}/session ({sessions} sessions)"


def _build_team_axis_table(documents: list[dict], axis: str, row_label: str, min_sessions: int) -> Table:
    axis_key = f"by_{axis}"
    machines = sorted({str(doc.get("machine_id", "?")) for doc in documents})
    per_machine_rows: dict[str, dict[str, dict]] = {}
    group_values: set[str] = set()
    for doc in documents:
        machine = str(doc.get("machine_id", "?"))
        rows_by_value = {row.get("value"): row for row in doc.get(axis_key, []) if isinstance(row, dict)}
        per_machine_rows[machine] = rows_by_value
        group_values.update(rows_by_value)

    columns = [Column(key="group_value", label=row_label, kind="str")]
    columns.extend(Column(key=f"machine_{machine}", label=machine, kind="str") for machine in machines)

    rows = []
    for value in sorted(group_values):
        row = [value]
        for machine in machines:
            row.append(_format_team_cell(per_machine_rows.get(machine, {}).get(value), min_sessions))
        rows.append(row)

    return Table(
        name=f"team_{axis_key}",
        title=f"Team comparison: {row_label.lower()}",
        columns=columns,
        rows=rows,
    )


def build_team_report_section(documents: list[dict], *, min_sessions: int = MIN_SESSIONS) -> Section:
    """The ``team-report`` section: per-archetype and per-agent-type
    comparison tables across machines, one column per machine (its own
    hashed ``machine_id``, never a hostname), cells below
    ``min_sessions`` printing ``"n<N"`` rather than a number that would
    imply more confidence than the sample supports.
    """
    tables = [
        _build_team_axis_table(documents, "archetype", "Archetype", min_sessions),
        _build_team_axis_table(documents, "agent_type", "Agent type", min_sessions),
    ]
    notes = [
        "Observed, not controlled: differences between machines may reflect different work, not a "
        "settings difference.",
        f"Minimum sample: {min_sessions} session(s) required in a machine's own row for that cell to "
        f"show a number; below that the cell reads 'n<{min_sessions}'.",
        "Column keys are each machine's own hashed machine id (a short, non-reversible per-machine "
        "value derived from its hostname) -- never a hostname itself.",
    ]
    return Section(key="team_report", title="Team report", tables=tables, notes=notes)


__all__ = [
    "GROUP_AXES",
    "MIN_SESSIONS",
    "build_team_aggregate",
    "build_team_report_section",
    "load_latest_team_documents",
    "machine_id",
    "save_team_document",
    "team_dir",
    "validate_team_document",
]

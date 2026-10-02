"""Aggregate, privacy-safe exports of a corpus (S1-exports, plan
"Feeds existing tooling" / "Aggregation without surveillance" / feature
9 "Team aggregate"): ``claudeglass export --format
csv-flat|json|otel-jsonl``.

For team leads: aggregate-only is the default (no session ids, no
per-session rows) and project slugs are hashed **by default in every
mode**, whether or not aggregate-only is in effect (see
:func:`resolve_export_options` -- fix for review finding 2: the *more*
identifying ``--per-session`` mode must never get *less* protection than
the default). Per-person/per-session detail is opt-in (``--per-session``).
Hashing itself is opt-out-able (``--no-hash-slugs``), but that opt-out
does not print the fully raw slug either: :func:`_redact_slug` still
replaces the OS-username segment (``Users-<name>-``/``home-<name>-``)
with ``<user>`` (see that function's docstring), and ``cli.py``'s
``_cmd_export`` prints a one-line stderr warning naming the residual
risk (the rest of the path shape/client name is still visible) whenever
that opt-out is used. No text (prompts, tool output, file paths) is ever
in an export; every column here is a count, a token total or a cost.

Row grain (``csv-flat``/``json``): one row per (day, project, model,
entrypoint, agent_type), plus ``session_id`` as an extra grouping
dimension when per-session is in effect. This mirrors ``usage.py``'s own
day/project/entrypoint axes and its ``_day_key``/``_to_local``/
``_parse_ts``/``_priced_turns`` helpers -- duplicated here rather than
imported, per this project's established small-helper convention (see
``usage.py``'s and ``workflows.py``'s own module docstrings) -- plus
``model`` and ``agent_type``, since a BI import wants those split out
rather than pre-summed away.

Formats:

- ``csv-flat``: the row grain above as plain CSV (raw, unformatted
  values, matching ``render/csv_out.py``'s own convention).
- ``json``: the same rows as a JSON list under ``"rows"``, plus a
  ``"meta"`` block (tool version, window, pricing version,
  ``generated_at``, ``hash_slugs``).
- ``otel-jsonl``: one JSON line per (day, model, token type) shaped like
  an OpenTelemetry metric data point, using the metric names Claude
  Code's own OTel integration documents (``claude_code.token.usage``
  with attribute ``type`` in ``input``/``output``/``cacheRead``/
  ``cacheCreation``, and ``claude_code.cost.usage``, both carrying a
  ``model`` attribute) so an existing collector's dashboards for those
  names can ingest this file. This is an **offline approximation**
  built from transcripts after the fact, not a live OTel exporter --
  there is no resource/scope metadata and no real collector transport,
  and ``time_unix_nano`` is simply the UTC instant of local-day start
  for the day the tokens were attributed to, not the moment they were
  actually used. This format carries no project/session attribute at
  all (the documented metric names don't have one), so
  ``--aggregate-only``/``--hash-slugs`` don't change its output.

Fix for review finding 7 (cache-creation disagreement): ``csv-flat``/
``json`` split cache-creation writes into ``cache_write_5m_tokens``/
``cache_write_1h_tokens`` (from ``Turn.cc_5m``/``Turn.cc_1h``), which are
both 0 for a pre-TTL-split transcript whose JSONL carried no nested
``cache_creation`` object (``Turn.ttl_split_unknown`` -- see
``model.py``) even though the write itself did happen. A trailing
``cache_write_tokens`` column (from ``Turn.cache_creation_tokens``, the
turn's own unsplit total) is always present so ``input + cache_write +
cache_read + output`` is a closed sum regardless of split availability,
and it matches ``otel-jsonl``'s own ``cacheCreation`` value exactly for
the same corpus (see the reconciliation test in ``tests/test_exports.py``).
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import __version__ as _TOOL_VERSION
from . import discovery
from .config import Config
from .corpus import Corpus, SessionBundle
from .model import TranscriptResult, Turn
from .parse import load_or_create_salt
from .pricing import Pricing, price_turn

#: Fixed CSV/JSON row column order for the aggregate grain (before an
#: optional trailing "session_id" in per-session mode). ``cache_write_tokens``
#: (review finding 7) is the turn's own unsplit cache-creation total,
#: alongside the pre-existing 5m/1h split columns -- see the module
#: docstring.
_ROW_FIELDS: tuple[str, ...] = (
    "day",
    "project",
    "model",
    "entrypoint",
    "agent_type",
    "turns",
    "input_tokens",
    "cache_write_5m_tokens",
    "cache_write_1h_tokens",
    "cache_write_tokens",
    "cache_read_tokens",
    "output_tokens",
    "thinking_tokens",
    "cost",
    "recache_turns",
    "recache_cache_creation",
)


# -- small helpers duplicated per this project's convention ------------------


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _to_local(dt: datetime, tz: str | None) -> datetime:
    return discovery.to_local(dt, tz)


def _priced_turns(result: TranscriptResult) -> list[Turn]:
    return [t for t in result.turns if t.turn_index > 0]


def _transcripts_of(bundle: SessionBundle) -> list[TranscriptResult]:
    transcripts: list[TranscriptResult] = []
    if bundle.top is not None:
        transcripts.append(bundle.top)
    transcripts.extend(bundle.subs)
    return transcripts


def _day_key(local_dt: datetime) -> str:
    return local_dt.strftime("%Y-%m-%d")


def _agent_type_of(tr: TranscriptResult) -> str:
    return tr.meta.agent_type or tr.meta.kind or "unknown"


# -- --aggregate-only / --hash-slugs resolution -------------------------------


@dataclass(slots=True)
class ExportOptions:
    fmt: str
    aggregate_only: bool
    hash_slugs: bool


def resolve_export_options(fmt: str, aggregate_only: bool | None, hash_slugs: bool | None) -> ExportOptions:
    """Resolve the CLI's ``--aggregate-only``/``--per-session`` and
    ``--hash-slugs``/``--no-hash-slugs`` flags (each ``None`` until the
    user picks a side -- see ``cli.py``'s ``_add_export_args``) into
    concrete booleans. ``aggregate_only`` defaults to ``True``.

    Fix for review finding 2: ``hash_slugs`` now defaults to ``True``
    **unconditionally**, independent of ``aggregate_only`` -- it used to
    default to whatever ``aggregate_only`` resolved to, which meant
    ``--per-session`` (the *more* identifying mode) got *less*
    protection by default (raw slugs) than the aggregate-only default
    (hashed slugs). An explicit ``--no-hash-slugs`` is still honoured
    even together with ``--aggregate-only``, since that is the
    exporter's own informed choice, not a default -- see
    :func:`_apply_slug_redaction` for what that opt-out actually emits
    (not the fully raw slug either).
    """
    resolved_aggregate_only = True if aggregate_only is None else aggregate_only
    resolved_hash_slugs = True if hash_slugs is None else hash_slugs
    return ExportOptions(fmt=fmt, aggregate_only=resolved_aggregate_only, hash_slugs=resolved_hash_slugs)


def _hash_slug(slug: str, salt: bytes) -> str:
    """First 12 hex characters of a salted HMAC-SHA256 over the project
    slug, domain-separated with a ``slug:`` tag.

    Fix for review finding 10: this used to be plain
    ``sha256(salt + slug)`` -- salted, but not the same *construction*
    ``parse.py``'s own ``_read_target_hash`` uses (HMAC, not
    concatenation) despite docs claiming otherwise, and with no domain
    tag, so unifying the two constructions later would have silently
    collided this module's project-slug namespace with ``parse.py``'s
    read-target-path namespace. Genuinely the same construction now
    (HMAC-SHA256 over the shared ``<config_dir>/salt`` file, via
    ``parse.load_or_create_salt``), just a different truncation length
    (12 hex chars here vs. 16 there) and domain tag (``"slug:"`` vs. a
    raw normalised path), so the two can never collide even if a future
    change makes both truncate to the same length.
    """
    digest = hmac.new(salt, b"slug:" + slug.encode("utf-8"), hashlib.sha256).hexdigest()
    return digest[:12]


#: Fix for review finding 2 (slug redaction fallback): matches the
#: OS-username segment of a ``discovery.slug_for`` slug right after a
#: ``Users-``/``home-``/``c-Users-`` anchor (the MSYS/Git-Bash slug shape
#: prefixes a bare drive letter, e.g. ``c-Users-...``), so it can be
#: replaced with ``<user>`` -- see :func:`_redact_slug`.
_SLUG_USER_SEGMENT_RE = re.compile(r"(?i)((?:^|-)(?:c-)?(?:Users|home)-)[A-Za-z0-9_.]+-")

try:
    # A sibling fix (branch fix-review-service) is adding
    # discovery.redact_slug with this exact behaviour -- reuse it once it
    # exists rather than keeping two copies (see the module docstring's
    # raw-slug decision). Imported lazily via try/except, not at
    # module-import time unconditionally, so this module still works
    # standalone before that branch merges.
    from .discovery import redact_slug as _redact_slug
except ImportError:  # pragma: no cover - exercised once fix-review-service merges

    def _redact_slug(slug: str) -> str:
        """Fallback for ``discovery.redact_slug`` (not yet on this
        branch): replaces the OS-username segment right after a
        ``Users-``/``home-``/``c-Users-`` anchor with ``<user>``, the
        same behaviour the sibling fix adds to ``discovery.py``. Drop
        this fallback once ``discovery.redact_slug`` exists and always
        imports cleanly.
        """
        return _SLUG_USER_SEGMENT_RE.sub(lambda m: f"{m.group(1)}<user>-", slug)


# -- csv-flat / json row grain -------------------------------------------


@dataclass(slots=True)
class _Cell:
    turns: int = 0
    input_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    cost: float = 0.0
    recache_turns: int = 0
    recache_cache_creation: int = 0


def build_export_rows(corpus: Corpus, pricing: Pricing, config: Config, *, per_session: bool) -> list[dict]:
    """One row per (day, project, model, entrypoint, agent_type)
    [, session_id], per the module docstring. Turns with no parseable
    local timestamp are skipped (nothing to bucket them by day with) --
    the same "day_cell only when local_dt is not None" rule
    ``usage.py``'s ``build_section`` already applies to its own by-day
    table.
    """
    cells: dict[tuple, _Cell] = {}
    for bundle in corpus.sessions:
        if bundle.top is None:
            continue
        for tr in _transcripts_of(bundle):
            entrypoint = tr.meta.entrypoint or "unknown"
            agent_type = _agent_type_of(tr)
            for turn in _priced_turns(tr):
                parsed = _parse_ts(turn.ts)
                if parsed is None:
                    continue
                local_dt = _to_local(parsed, config.tz)
                day = _day_key(local_dt)
                model = turn.model or "<unknown>"

                key: tuple = (day, bundle.slug, model, entrypoint, agent_type)
                if per_session:
                    key = key + (bundle.session_id,)

                cell = cells.setdefault(key, _Cell())
                cell.turns += 1
                cell.input_tokens += turn.input_tokens
                cell.cache_write_5m_tokens += turn.cc_5m
                cell.cache_write_1h_tokens += turn.cc_1h
                cell.cache_write_tokens += turn.cache_creation_tokens
                cell.cache_read_tokens += turn.cache_read_tokens
                cell.output_tokens += turn.output_tokens
                cell.thinking_tokens += turn.thinking_tokens
                resolved = pricing.resolve_model(turn.model)
                cell.cost += price_turn(turn, resolved).total
                if turn.is_recache:
                    cell.recache_turns += 1
                    cell.recache_cache_creation += turn.cache_creation_tokens

    rows: list[dict] = []
    for key, cell in sorted(cells.items(), key=lambda kv: kv[0]):
        if per_session:
            day, slug, model, entrypoint, agent_type, session_id = key
        else:
            day, slug, model, entrypoint, agent_type = key
            session_id = None

        row = {
            "day": day,
            "project": slug,
            "model": model,
            "entrypoint": entrypoint,
            "agent_type": agent_type,
            "turns": cell.turns,
            "input_tokens": cell.input_tokens,
            "cache_write_5m_tokens": cell.cache_write_5m_tokens,
            "cache_write_1h_tokens": cell.cache_write_1h_tokens,
            "cache_write_tokens": cell.cache_write_tokens,
            "cache_read_tokens": cell.cache_read_tokens,
            "output_tokens": cell.output_tokens,
            "thinking_tokens": cell.thinking_tokens,
            "cost": cell.cost,
            "recache_turns": cell.recache_turns,
            "recache_cache_creation": cell.recache_cache_creation,
        }
        if per_session:
            row["session_id"] = session_id
        rows.append(row)
    return rows


def _apply_hash_slugs(rows: list[dict], config_dir: str | Path) -> None:
    """Hash each project slug and each custom agent's name (often named
    after the project); stock agent types and the transcript kinds
    ``_agent_type_of`` falls back to stay as they are, as in a team
    document (``team._agent_type_group_label``)."""
    from .team import _agent_type_group_label  # team imports this module

    salt = load_or_create_salt(config_dir)
    for row in rows:
        row["project"] = _hash_slug(row["project"], salt)
        agent_type = row.get("agent_type")
        if agent_type and agent_type not in ("subagent", "workflow-agent"):
            row["agent_type"] = _agent_type_group_label(agent_type, salt)


def _apply_slug_redaction(rows: list[dict]) -> None:
    """Applied instead of hashing when the caller explicitly opts out
    with ``--no-hash-slugs`` (review finding 2's raw-slug decision): the
    project slug still isn't emitted fully raw -- the OS-username
    segment is replaced with ``<user>`` via :func:`_redact_slug`. The
    caller (``cli.py``'s ``_cmd_export``) is responsible for the
    one-line stderr warning naming the residual risk; this function only
    does the redaction itself.
    """
    for row in rows:
        row["project"] = _redact_slug(row["project"])


def render_csv_flat(rows: list[dict], *, per_session: bool) -> str:
    fieldnames = list(_ROW_FIELDS) + (["session_id"] if per_session else [])
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buf.getvalue()


def render_json(rows: list[dict], meta: dict) -> str:
    return json.dumps({"meta": meta, "rows": rows}, indent=2, sort_keys=True)


# -- otel-jsonl ---------------------------------------------------------


@dataclass(slots=True)
class _OtelCell:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost: float = 0.0
    day_start_unix_nano: int = 0


def _day_start_unix_nano(local_dt: datetime) -> int:
    day_start = local_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(day_start.timestamp() * 1_000_000_000)


def build_otel_cells(corpus: Corpus, pricing: Pricing, config: Config) -> dict[tuple[str, str], _OtelCell]:
    """Per (day, model) totals for :func:`render_otel_jsonl`. No
    project/entrypoint/agent_type/session dimension at all -- the
    documented OTel metric names this format mirrors don't carry one
    (see the module docstring)."""
    cells: dict[tuple[str, str], _OtelCell] = {}
    for bundle in corpus.sessions:
        if bundle.top is None:
            continue
        for tr in _transcripts_of(bundle):
            for turn in _priced_turns(tr):
                parsed = _parse_ts(turn.ts)
                if parsed is None:
                    continue
                local_dt = _to_local(parsed, config.tz)
                day = _day_key(local_dt)
                model = turn.model or "<unknown>"
                key = (day, model)
                cell = cells.setdefault(key, _OtelCell(day_start_unix_nano=_day_start_unix_nano(local_dt)))
                cell.input_tokens += turn.input_tokens
                cell.output_tokens += turn.output_tokens
                cell.cache_read_tokens += turn.cache_read_tokens
                cell.cache_creation_tokens += turn.cache_creation_tokens
                resolved = pricing.resolve_model(turn.model)
                cell.cost += price_turn(turn, resolved).total
    return cells


def render_otel_jsonl(corpus: Corpus, pricing: Pricing, config: Config) -> str:
    cells = build_otel_cells(corpus, pricing, config)
    lines: list[str] = []
    for (day, model), cell in sorted(cells.items(), key=lambda kv: kv[0]):
        for otel_type, value in (
            ("input", cell.input_tokens),
            ("output", cell.output_tokens),
            ("cacheRead", cell.cache_read_tokens),
            ("cacheCreation", cell.cache_creation_tokens),
        ):
            lines.append(
                json.dumps(
                    {
                        "name": "claude_code.token.usage",
                        "attributes": {"type": otel_type, "model": model},
                        "time_unix_nano": cell.day_start_unix_nano,
                        "value": value,
                    },
                    sort_keys=True,
                )
            )
        lines.append(
            json.dumps(
                {
                    "name": "claude_code.cost.usage",
                    "attributes": {"model": model},
                    "time_unix_nano": cell.day_start_unix_nano,
                    "value": cell.cost,
                },
                sort_keys=True,
            )
        )
    return "\n".join(lines) + ("\n" if lines else "")


# -- top-level entry point ------------------------------------------------


def build_export_text(
    corpus: Corpus,
    pricing: Pricing,
    config: Config,
    config_dir: str | Path,
    options: ExportOptions,
    *,
    window: str = "",
    generated_at: str | None = None,
) -> str:
    """The full export text for ``options.fmt``, ready to print to
    stdout or write to a file (see ``cli.py``'s ``_cmd_export``)."""
    if options.fmt == "otel-jsonl":
        return render_otel_jsonl(corpus, pricing, config)

    per_session = not options.aggregate_only
    rows = build_export_rows(corpus, pricing, config, per_session=per_session)
    if options.hash_slugs:
        _apply_hash_slugs(rows, config_dir)
    else:
        _apply_slug_redaction(rows)

    if options.fmt == "csv-flat":
        return render_csv_flat(rows, per_session=per_session)
    if options.fmt == "json":
        meta = {
            "tool_version": _TOOL_VERSION,
            "window": window,
            "pricing_version": pricing.version,
            "generated_at": generated_at or datetime.now().astimezone().isoformat(),
            "hash_slugs": options.hash_slugs,
            "aggregate_only": options.aggregate_only,
        }
        return render_json(rows, meta)
    raise ValueError(f"unknown export format: {options.fmt!r}")


__all__ = [
    "ExportOptions",
    "resolve_export_options",
    "build_export_rows",
    "render_csv_flat",
    "render_json",
    "build_otel_cells",
    "render_otel_jsonl",
    "build_export_text",
]

"""Monthly finance report (S1-exports, plan "Finance" / feature 10
"Scheduled reports", promoted to v0.2 ``serve --monthly-report``):
``claudeglass monthly-report --out DIR [--month YYYY-MM]`` writes
``DIR/claudeglass-YYYY-MM.md`` and the matching ``.html`` for one
calendar month.

Scope: deliberately just a finance summary, not the full multi-section
report ``report`` prints -- this is the scheduled, habit-forming
artefact the plan's "Finance" section describes (cost/tokens by
model/project/entrypoint, five-hour blocks for subscription billing),
so the body is a short finance header table followed by the ``usage``
section alone (``build_report(..., include={"usage"})``), not
recache/ttl/compaction/topology/etc., which are optimisation-focused
rather than finance-focused.

Month attribution: a session is attributed to the calendar month of its
*first* top-level turn's local timestamp (``config.tz``, through the
same ``discovery.to_local`` fallback rule ``usage.py``'s/``classify.py``'s
own ``_to_local`` use). A session whose turns straddle a month boundary is
therefore counted wholly in the month it started, not split across two
reports -- a documented approximation, the same kind ``usage.py``'s own
five-hour-block grid already accepts for a similar reason (no exact
per-turn slicing without touching every other section's own per-session
assumptions).

Idempotency: the same ``(corpus, pricing, config, month)`` produces
files identical apart from a single trailing "Generated at: ..." line
(Markdown) / HTML comment before ``</body>`` -- the report body itself
carries no other wall-clock value anywhere, unlike
``render/markdown.py``/``render/html.py``'s own ``render_markdown``/
``render_html``, which bake ``model.meta.generated_at`` into a bullet
near the top and are therefore *not* used here. This module renders its
own compact tables (via ``render.tables.format_cell``/``escape_md``,
the same formatting primitives every other renderer already shares).
Fix for review finding 11: a *genuinely* byte-identical run (not merely
"apart from one line") is available by passing a fixed
``generated_at`` string to :func:`write_monthly_report` -- wired to
``monthly-report --generated-at``/``SOURCE_DATE_EPOCH`` by ``cli.py``,
the same reproducible-build convention ``export`` already offers (see
``exports.build_export_text``'s own ``generated_at`` parameter). A test
asserting the "apart from one line" idempotency still strips that one
line/comment before comparing, since a caller that doesn't pin
``generated_at`` gets the previous behaviour.

Entry points: ``write_monthly_report`` takes an already-loaded
``corpus``/``pricing``/``config`` rather than loading them itself,
matching ``report.build_report``'s own "caller loads, this function only
assembles" contract. ``run_monthly_report`` wraps it with the steps the
``monthly-report`` command and ``serve --monthly-report DIR``
(``service/monthly_job.py``) share: the no-projects/no-sessions checks,
the usage log and the empty-month note.
"""

from __future__ import annotations

import dataclasses
import html as _html_mod
import re
from calendar import monthrange
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from . import discovery, invocation, pages
from .config import Config
from .corpus import Corpus, SessionBundle
from .model import ReportModel, Table
from .pricing import Pricing
from .render.tables import escape_md, format_cell
from .report import _effort_mismatch_share_threshold, build_report

if TYPE_CHECKING:
    from .units import Units

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

_NUMERIC_KINDS = frozenset({"int", "float", "pct", "money", "tokens", "secs"})


def resolve_month(month_str: str | None, tz: str | None = None, *, now: datetime | None = None) -> str:
    """``month_str`` validated as ``YYYY-MM``, or (when ``None``/empty)
    the previous calendar month relative to today, in ``YYYY-MM`` form.
    Raises ``ValueError`` with a one-line, CLI-printable reason on a
    malformed value.

    Fix for review finding 12: the "previous month" default used to be
    computed from ``date.today()`` -- the machine's own local zone --
    while every other month/day bucketing in this module (and in
    ``usage.py``/``classify.py``) uses ``config.tz``. On the 1st of a
    month, a user whose ``config.tz`` is behind the machine's own zone
    got a report for the wrong month, silently (an empty/partial month
    still writes files and exits 0 -- see nit 17). ``tz`` (typically
    ``config.tz``, threaded in from ``cli.py``'s ``_cmd_monthly_report``)
    is now used the same way :func:`_to_local` resolves every other
    timestamp in this module, falling back to the machine's own zone
    when absent or unresolvable -- unchanged default behaviour for a
    caller that doesn't pass it. ``now``, accepted for tests, defaults to
    the current instant.
    """
    if not month_str:
        current = (now or datetime.now(timezone.utc))
        current = _to_local(current if current.tzinfo else current.replace(tzinfo=timezone.utc), tz)
        year, month = current.year, current.month
        if month == 1:
            return f"{year - 1:04d}-12"
        return f"{year:04d}-{month - 1:02d}"
    if not _MONTH_RE.match(month_str):
        raise ValueError(f"--month must be YYYY-MM, got {month_str!r}")
    return month_str


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


# -- month-scoped corpus filtering -------------------------------------------


def _session_local_month(bundle: SessionBundle, tz: str | None) -> str | None:
    """The ``YYYY-MM`` of ``bundle``'s earliest top-level turn's local
    timestamp, or ``None`` when it has no top-level transcript or no
    turn with a parseable timestamp."""
    if bundle.top is None:
        return None
    for turn in bundle.top.turns:
        parsed = _parse_ts(turn.ts)
        if parsed is not None:
            return _to_local(parsed, tz).strftime("%Y-%m")
    return None


def filter_corpus_to_month(corpus: Corpus, month: str, tz: str | None) -> Corpus:
    """A new :class:`Corpus` carrying only the sessions attributed to
    ``month`` (see the module docstring's month-attribution rule)."""
    sessions = [b for b in corpus.sessions if _session_local_month(b, tz) == month]
    return dataclasses.replace(corpus, sessions=sessions)


# -- finance header -----------------------------------------------------


def _find_table(model: ReportModel, section_key: str, table_name: str) -> Table | None:
    for section in model.sections:
        if section.key != section_key:
            continue
        for table in section.tables:
            if table.name == table_name:
                return table
    return None


def _regroup_by_model(by_month_table: Table | None) -> Table:
    """``by_month``'s ``(period, model, turns, tokens, cost)`` rows,
    regrouped to ``(model, turns, tokens, cost)`` -- ``period`` is
    dropped since every row already belongs to the single target month
    (the corpus was pre-filtered), and any rows that still share a model
    (a timezone edge landing two period buckets on the same calendar
    month) are summed together rather than left duplicated.
    """
    totals: dict[str, list] = {}
    for row in (by_month_table.rows if by_month_table else []):
        _period, model, turns, tokens, cost = row
        bucket = totals.setdefault(model, [0, 0, 0.0])
        bucket[0] += turns
        bucket[1] += tokens
        bucket[2] += cost
    rows = [[model, turns, tokens, cost] for model, (turns, tokens, cost) in sorted(totals.items(), key=lambda kv: -kv[1][2])]
    from .model import Column

    return Table(
        name="cost_by_model",
        title="Cost by model",
        columns=[
            Column(key="model", label="Model", kind="str"),
            Column(key="turns", label="Turns", kind="int"),
            Column(key="tokens", label="Tokens", kind="tokens"),
            Column(key="cost", label="Cost", kind="money"),
        ],
        rows=rows,
    )


def _finance_summary_table(
    *,
    total_cost: float,
    total_tokens: int,
    sessions: int,
    five_hour_blocks_used: int | None,
    currency: str,
    units: "Units | None" = None,
) -> Table:
    from .model import Column

    rows = [
        ["Total cost", total_cost],
        ["Total tokens", total_tokens],
        ["Sessions", sessions],
    ]
    if five_hour_blocks_used is not None:
        rows.append(["Five-hour blocks used", five_hour_blocks_used])
    kinds = ["money", "tokens", "int", "int"]
    # UX-1: units (model.units, already set by build_report) makes "Total
    # cost" a subscription's own weekly-usage-limit share alongside the
    # list-price figure, not a bare dollar amount that means little to a
    # flat-fee plan -- format_cell's own units branch, ignored for the
    # non-money kinds above.
    formatted_rows = [
        [label, format_cell(value, kinds[i], currency, units)] for i, (label, value) in enumerate(rows)
    ]
    return Table(
        name="finance_summary",
        title="Finance summary",
        columns=[Column(key="metric", label="Metric", kind="str"), Column(key="value", label="Value", kind="str")],
        rows=formatted_rows,
    )


def _habits_digest_table(
    corpus: Corpus,
    pricing: Pricing,
    currency: str,
    ratings: dict | None,
    units: "Units | None" = None,
    config: Config | None = None,
) -> Table | None:
    """The Work habits digest (``habits.digest_table``) for the month,
    pre-formatted like :func:`_finance_summary_table`: the habits worth
    the most, what habits already picked up save, and what a piece of
    work that met its goal cost. ``None`` when there's nothing to say.
    ``config``, when given, resolves ``effort_fit``'s share gate to the
    same configured number the main report's ``effort-mismatch`` rule
    uses (UX-3, "one shared effort threshold"), and counts weeks in its
    ``tz``; without it, the class default and the machine's own zone."""
    from . import habits
    from .helptext import TABLE_COPY
    from .model import Column

    threshold_kwargs = (
        {"effort_share_threshold_pct": _effort_mismatch_share_threshold(config)} if config is not None else {}
    )
    tz = config.tz if config is not None else None
    digest = habits.digest_table(habits.collect(corpus, pricing, ratings=ratings, tz=tz, **threshold_kwargs))
    if not digest.rows:
        return None
    copy = TABLE_COPY["habits_digest"]
    rows = [
        [
            copy.value_labels.get(item, item),
            what,
            format_cell(value, copy.row_kinds.get(item, "str"), currency, units),
            detail,
        ]
        for item, what, value, detail in digest.rows
    ]
    return Table(
        name="habits_digest",
        title="Work habits",
        columns=[
            Column(key="item", label="Item", kind="str"),
            Column(key="what", label="What", kind="str"),
            Column(key="value", label="Saving a week, or the figure", kind="str"),
            Column(key="detail", label="Detail", kind="str"),
        ],
        rows=rows,
        notes=["Savings are a week's worth at this month's pace; {{page:habits}} has an example to copy for each."],
    )


# -- local, deterministic renderers (see module docstring) -------------------


def _md_table(table: Table, currency: str, units: "Units | None" = None) -> list[str]:
    lines = [f"### {table.title}", ""]
    aligns = ["right" if c.kind in _NUMERIC_KINDS else "left" for c in table.columns]
    header = "| " + " | ".join(escape_md(c.label) for c in table.columns) + " |"
    divider = "| " + " | ".join(("---:" if a == "right" else "---") for a in aligns) + " |"
    lines.append(header)
    lines.append(divider)
    for row in table.rows:
        cells = []
        for value, column in zip(row, table.columns):
            # finance_summary's own "value" column is pre-formatted text
            # (mixed units row to row), everything else formats by kind.
            cell_text = value if (table.name == "finance_summary" and column.key == "value") else format_cell(value, column.kind, currency, units)
            cells.append(escape_md(cell_text))
        lines.append("| " + " | ".join(cells) + " |")
    if table.notes:
        lines.append("")
        lines.extend(f"- {pages.plain(note)}" for note in table.notes)
    return lines


def _render_month_markdown(month: str, tables: list[Table], currency: str, generated_at: str, units: "Units | None" = None) -> str:
    lines = [f"# ClaudeGlass — Monthly report — {month}", ""]
    for table in tables:
        lines.extend(_md_table(table, currency, units))
        lines.append("")
    lines.append(f"Generated at: {generated_at}")
    return "\n".join(lines).rstrip("\n") + "\n"


def _esc(value) -> str:
    return _html_mod.escape(str(value), quote=True)


def _html_table(table: Table, currency: str, units: "Units | None" = None) -> str:
    thead = "<tr>" + "".join(f"<th>{_esc(c.label)}</th>" for c in table.columns) + "</tr>"
    body_rows = []
    for row in table.rows:
        cells = []
        for value, column in zip(row, table.columns):
            cell_text = value if (table.name == "finance_summary" and column.key == "value") else format_cell(value, column.kind, currency, units)
            cells.append(f"<td>{_esc(cell_text)}</td>")
        body_rows.append("<tr>" + "".join(cells) + "</tr>")
    notes_html = ""
    if table.notes:
        notes_html = "<ul>" + "".join(f"<li>{_esc(pages.plain(note))}</li>" for note in table.notes) + "</ul>"
    return f"<h2>{_esc(table.title)}</h2><table><thead>{thead}</thead><tbody>{''.join(body_rows)}</tbody></table>{notes_html}"


def _render_month_html(month: str, tables: list[Table], currency: str, generated_at: str, units: "Units | None" = None) -> str:
    tables_html = "".join(_html_table(t, currency, units) for t in tables)
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        f"<title>{_esc(f'ClaudeGlass — Monthly report — {month}')}</title>\n"
        "<style>table{border-collapse:collapse;margin-bottom:1.5em}"
        "th,td{border:1px solid #ccc;padding:4px 8px;text-align:left}</style>\n"
        "</head>\n"
        "<body>\n"
        f"<h1>{_esc(f'ClaudeGlass — Monthly report — {month}')}</h1>\n"
        f"{tables_html}\n"
        f"<!-- Generated at: {_esc(generated_at)} -->\n"
        "</body>\n"
        "</html>\n"
    )


# -- top-level entry point ------------------------------------------------


def write_monthly_report(
    corpus: Corpus,
    pricing: Pricing,
    config: Config,
    month: str,
    out_dir: str | Path,
    usage_log_rows: list[dict] | None = None,
    generated_at: str | None = None,
    ratings: dict | None = None,
) -> list[Path]:
    """Write ``claudeglass-<month>.md`` and ``.html`` into
    ``out_dir`` (created if absent) for the given ``month`` (``YYYY-MM``,
    see :func:`resolve_month`). Returns the two paths written, in that
    order.

    ``usage_log_rows`` (fix for review finding 9): ``docs/exports.md``
    promises the monthly report's ``usage`` section carries
    ``cache_ground_truth`` "when a usage log is available", but this
    function used to call ``build_report`` without ever passing
    ``usage_log_rows`` at all, so the table could never appear -- the
    promise was unkeepable regardless of what was on disk. A caller
    (``cli.py``'s ``_cmd_monthly_report``) now loads
    ``<config_dir>/usage-log.csv`` the same way ``_cmd_report_like``
    already does and passes the rows here; they are scoped down to just
    this month's sessions (mirroring finding 8's project/window scoping
    for the ordinary ``report`` command) before reaching
    :func:`~claudeglass.report.build_report`.

    ``generated_at`` (fix for review finding 11): the module docstring's
    "byte-identical across repeated runs" claim was only true modulo the
    one "Generated at: ..." line/comment -- contradicting its own next
    clause, which a strict reading of "byte-identical" doesn't allow.
    Mirroring ``exports.build_export_text``'s own ``generated_at``
    parameter, passing a fixed value here (wired to ``--generated-at``/
    ``SOURCE_DATE_EPOCH`` by ``cli.py``'s ``_cmd_monthly_report``, same
    as the ``export`` command) now makes the output genuinely
    byte-identical, not merely "identical apart from one line". Defaults
    to ``datetime.now().astimezone().isoformat()`` when omitted,
    preserving the previous behaviour for any other caller.

    ``ratings``: your Sessions-tab ratings by session id, for the Work
    habits digest (``Store.all_feedback``).
    """
    filtered = filter_corpus_to_month(corpus, month, config.tz)
    projects = tuple(sorted({b.slug for b in filtered.sessions if b.slug}))
    year, month_num = (int(part) for part in month.split("-"))
    days_in_month = monthrange(year, month_num)[1]
    window = f"{month}-01 to {month}-{days_in_month:02d} (calendar month)"

    month_session_ids = {b.session_id for b in filtered.sessions}
    scoped_usage_log_rows = (
        [row for row in usage_log_rows if row.get("session_id") in month_session_ids]
        if usage_log_rows
        else None
    )

    model = build_report(
        filtered,
        pricing,
        config,
        projects=projects,
        window=window,
        include={"usage"},
        usage_log_rows=scoped_usage_log_rows,
    )

    by_month_table = _find_table(model, "usage", "by_month")
    by_project_table = _find_table(model, "usage", "by_project")
    by_entrypoint_table = _find_table(model, "usage", "by_entrypoint")
    five_hour_table = _find_table(model, "usage", "five_hour_blocks")

    total_cost = sum(row[4] for row in (by_month_table.rows if by_month_table else []))
    total_tokens = sum(row[3] for row in (by_month_table.rows if by_month_table else []))
    five_hour_blocks_used = (
        len(five_hour_table.rows) if (config.billing == "subscription" and five_hour_table is not None) else None
    )

    currency = model.meta.pricing.currency
    # UX-1: model.units is already set by build_report -- threaded through
    # every table below so the digest's money cells follow the billing
    # mode (a subscription's own weekly-usage-limit share alongside the
    # list-price figure) rather than a bare dollar amount, matching the
    # module docstring's "uses Units" wiring item.
    units = model.units
    finance_summary = _finance_summary_table(
        total_cost=total_cost,
        total_tokens=total_tokens,
        sessions=len(filtered.sessions),
        five_hour_blocks_used=five_hour_blocks_used,
        currency=currency,
        units=units,
    )
    cost_by_model = _regroup_by_model(by_month_table)
    cost_by_project = dataclasses.replace(by_project_table, title="Cost by project") if by_project_table else None
    cost_by_entrypoint = dataclasses.replace(by_entrypoint_table, title="Cost by entrypoint") if by_entrypoint_table else None

    header_tables = [finance_summary, cost_by_model]
    if cost_by_project is not None:
        header_tables.append(cost_by_project)
    if cost_by_entrypoint is not None:
        header_tables.append(cost_by_entrypoint)
    digest = _habits_digest_table(filtered, pricing, currency, ratings, units, config)
    if digest is not None:
        header_tables.append(digest)

    usage_section = next((s for s in model.sections if s.key == "usage"), None)
    body_tables = list(usage_section.tables) if usage_section else []

    all_tables = header_tables + body_tables

    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    if generated_at is None:
        generated_at = datetime.now().astimezone().isoformat()

    md_path, html_path = report_paths(out_path, month)
    # Commands in the notes, in the form that runs on this install.
    markdown = invocation.rewrite_rendered(_render_month_markdown(month, all_tables, currency, generated_at, units), "markdown")
    page = invocation.rewrite_rendered(_render_month_html(month, all_tables, currency, generated_at, units), "html")
    md_path.write_text(markdown, encoding="utf-8")
    html_path.write_text(page, encoding="utf-8")
    return [md_path, html_path]


def report_paths(out_dir: str | Path, month: str) -> list[Path]:
    """The two files :func:`write_monthly_report` writes for ``month``
    into ``out_dir`` (Markdown first, then HTML), without writing them --
    so ``serve --monthly-report`` can tell whether a month is done."""
    out_path = Path(out_dir)
    return [out_path / f"claudeglass-{month}.md", out_path / f"claudeglass-{month}.html"]


class MonthlyReportError(Exception):
    """A monthly report could not be written for a reason worth one line
    (no project folders, no sessions at all). ``exit_code`` is what the
    ``monthly-report`` command exits with."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def run_monthly_report(
    *,
    config: Config,
    pricing: Pricing,
    config_dir: str | Path,
    root: str | Path,
    project_dirs: list[Path],
    month: str,
    out_dir: str | Path,
    load_corpus: Callable[[list[Path]], Corpus],
    generated_at: str | None = None,
    note: Callable[[str], None] | None = None,
) -> list[Path]:
    """Everything the ``monthly-report`` command does once its config,
    pricing, project folders and month are resolved, shared with
    ``serve --monthly-report`` (``service/monthly_job.py``) so the two
    write the same report. ``load_corpus`` loads the sessions for
    ``project_dirs`` (each caller keeps its own cache and flag
    handling). Loads ``<config_dir>/usage-log.csv`` when present, for
    the ``cache_ground_truth`` table. Raises :class:`MonthlyReportError`
    when there are no project folders under ``root`` or no sessions in
    them; an empty ``month`` is not an error -- ``note`` is told, and the
    report is written with zeroed tables. Returns the paths written.
    """
    # Local import: statusline pulls in installer, which this module
    # otherwise never needs.
    from .statusline import load_usage_log_ground_truth

    if not project_dirs:
        raise MonthlyReportError(f"no matching project directories under {root}")

    # The monthly report always covers exactly the calendar month itself:
    # load the corpus for the project(s) and let write_monthly_report do
    # its own month-window filtering against each turn's local timestamp
    # (there is no discovery-level filter for one calendar month).
    corpus = load_corpus(project_dirs)
    if not corpus.sessions:
        raise MonthlyReportError(f"no sessions found under {root}")

    # Review finding 9: docs/exports.md promises cache_ground_truth "when
    # a usage log is available" -- load it the same tolerant way
    # ``report`` does; write_monthly_report scopes it to this month.
    usage_log_csv_path = Path(config_dir) / "usage-log.csv"
    usage_log_rows = load_usage_log_ground_truth(usage_log_csv_path) if usage_log_csv_path.exists() else None

    # Nit 17: an empty target month still writes files with zeroed tables
    # (a scheduled job should not fail just because nothing happened that
    # month), but says so rather than failing silent.
    if note is not None and not filter_corpus_to_month(corpus, month, config.tz).sessions:
        note(f"no sessions found for {month} -- writing a report with zeroed tables")

    from .service.serve import STORE_FILENAME
    from .service.store import read_session_marks

    _tags, ratings = read_session_marks(Path(config_dir) / STORE_FILENAME)
    return write_monthly_report(
        corpus, pricing, config, month, out_dir, usage_log_rows=usage_log_rows, generated_at=generated_at,
        ratings=ratings,
    )


__all__ = [
    "resolve_month",
    "filter_corpus_to_month",
    "write_monthly_report",
    "report_paths",
    "MonthlyReportError",
    "run_monthly_report",
]

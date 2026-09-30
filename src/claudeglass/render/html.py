"""HTML renderer: ``ReportModel`` -> one self-contained HTML file.

"Self-contained" means no external reference of any kind: no stylesheet
or script ``src``/``href``, no ``@import``, no ``url(...)``, no bare
``http://``/``https://`` literal. Everything the page needs (CSS, the
optional click-to-sort script) is inlined. ``tests/test_render.py``
greps the rendered output for those substrings and fails on any hit, so
this module must never introduce one.

Note on the plan's pricing ``source_url``: that field lives in
``pricing.toml`` (parsed by WP2), not on ``model.PricingMeta`` in this
codebase (``path``, ``version``, ``sha8``, ``currency``,
``coverage_pct`` only — see ``model.py``'s deviation note). There is
therefore nothing to strip a scheme from today. If a future work
package adds it to ``PricingMeta``, render it as plain escaped text
(never as an ``<a href>``) to keep this module's no-external-reference
guarantee.

Every piece of report text is passed through ``html.escape`` before
being placed in the page, whether as element content or as an
attribute value.
"""

from __future__ import annotations

import dataclasses
import html as _html

from .. import pages
from ..fixes import fix_note
from ..model import Diagnostics, ReportModel, Table
from .tables import SCOPE_LABELS, SEVERITY_LABELS, display_cell, fix_subject, evidence_source, format_evidence_value, help_parts

#: Column kinds that read as quantities: right-aligned, sortable numerically.
_NUMERIC_KINDS = frozenset({"int", "float", "pct", "money", "tokens", "secs"})

_STYLE = """
:root {
  --bg: #ffffff;
  --fg: #1a1a1a;
  --muted: #666666;
  --border: #d8d8d8;
  --header-bg: #f2f2f2;
  --zebra: #f8f8f8;
  --accent: #2563eb;
  --bar-bg: #cfe0fb;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #121212;
    --fg: #e8e8e8;
    --muted: #a3a3a3;
    --border: #3a3a3a;
    --header-bg: #1e1e1e;
    --zebra: #191919;
    --accent: #7aa8f7;
    --bar-bg: #223a5e;
  }
}
* {
  box-sizing: border-box;
}
body {
  margin: 0;
  padding: 1.5rem;
  background: var(--bg);
  color: var(--fg);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  line-height: 1.5;
}
h1 {
  font-size: 1.6rem;
}
h2 {
  margin-top: 2rem;
  padding-bottom: 0.25rem;
  border-bottom: 1px solid var(--border);
}
h3 {
  margin-top: 1.25rem;
}
.intro {
  color: var(--muted);
  max-width: 70ch;
}
details.help {
  margin: 0.25rem 0 0.5rem;
  font-size: 0.9rem;
}
details.help summary {
  cursor: pointer;
  color: var(--accent);
  width: fit-content;
}
details.help dl {
  margin: 0.4rem 0 0;
  padding-left: 0.8rem;
  border-left: 3px solid var(--border);
  max-width: 75ch;
}
details.help dt {
  font-weight: 600;
}
details.help dd {
  margin: 0 0 0.4rem;
}
table {
  width: 100%;
  border-collapse: collapse;
  margin: 0.5rem 0 1rem;
  font-size: 0.9rem;
}
th, td {
  padding: 0.35rem 0.6rem;
  border: 1px solid var(--border);
  text-align: left;
}
thead th {
  position: sticky;
  top: 0;
  background: var(--header-bg);
  cursor: pointer;
  user-select: none;
}
tbody tr:nth-child(even) {
  background: var(--zebra);
}
td.num, th.num {
  text-align: right;
}
.bar {
  display: inline-block;
  height: 0.5em;
  margin-right: 0.4em;
  vertical-align: middle;
  background: var(--bar-bg);
  border-radius: 2px;
}
.notes, .evidence-list {
  color: var(--muted);
  font-size: 0.85rem;
}
.rec {
  margin: 0.75rem 0;
  padding: 0.75rem 1rem;
  border: 1px solid var(--border);
  border-left: 4px solid var(--accent);
  border-radius: 4px;
}
footer {
  margin-top: 2rem;
  color: var(--muted);
  font-size: 0.8rem;
}
""".strip()

_SCRIPT = """
(function () {
  document.querySelectorAll("table.sortable").forEach(function (table) {
    var heads = table.querySelectorAll("th");
    heads.forEach(function (th, index) {
      th.addEventListener("click", function () {
        var tbody = table.querySelector("tbody");
        var rows = Array.prototype.slice.call(tbody.querySelectorAll("tr"));
        var ascending = th.getAttribute("data-dir") !== "asc";
        heads.forEach(function (h) { h.removeAttribute("data-dir"); });
        th.setAttribute("data-dir", ascending ? "asc" : "desc");
        rows.sort(function (a, b) {
          var av = a.children[index].getAttribute("data-sort") || "";
          var bv = b.children[index].getAttribute("data-sort") || "";
          var an = parseFloat(av);
          var bn = parseFloat(bv);
          var cmp;
          if (!isNaN(an) && !isNaN(bn)) {
            cmp = an - bn;
          } else {
            cmp = av.localeCompare(bv);
          }
          return ascending ? cmp : -cmp;
        });
        rows.forEach(function (row) { tbody.appendChild(row); });
      });
    });
  });
})();
""".strip()


def _esc(value) -> str:
    return _html.escape("" if value is None else str(value))


def _list_html(lines: list[str], css_class: str) -> str:
    items = "".join(f"<li>{_esc(line)}</li>" for line in lines)
    return f'<ul class="{css_class}">{items}</ul>'


def _meta_lines(model: ReportModel) -> list[str]:
    meta = model.meta
    pricing = meta.pricing
    lines = [
        f"Tool version: {meta.tool_version or '-'}",
        f"Generated at: {meta.generated_at or '-'}",
        f"Window: {meta.window or '-'}",
        f"Projects: {', '.join(meta.projects) if meta.projects else '-'}",
        "Pricing: "
        + ", ".join(
            [
                f"path={pricing.path or '-'}",
                f"version={pricing.version or '-'}",
                f"sha8={pricing.sha8 or '-'}",
                f"currency={pricing.currency}",
                f"coverage={pricing.coverage_pct:.1f}%",
            ]
        ),
        f"Billing mode: {meta.billing_mode}" + (f" ({meta.billing_source})" if meta.billing_source else ""),
    ]
    thresholds = ", ".join(f"{k}={v}" for k, v in meta.thresholds.items()) or "-"
    lines.append(f"Thresholds: {thresholds}")
    return lines


def _assumptions_lines(model: ReportModel) -> list[str]:
    return list(model.meta.assumptions) or ["None recorded."]


def _diagnostics_lines(model: ReportModel) -> list[str]:
    lines = []
    diagnostics = model.diagnostics
    for field_def in dataclasses.fields(Diagnostics):
        value = getattr(diagnostics, field_def.name)
        if isinstance(value, dict):
            value = ", ".join(f"{k}={v}" for k, v in value.items()) if value else "-"
        lines.append(f"{field_def.name}: {value}")
    # Parser-signals addition (SURV-6/7, see model.py's module docstring):
    # ReportModel.parser_notes is a sibling side channel to Diagnostics,
    # not one of its fields, so it's printed the same way just below
    # (same convention render/markdown.py's own _render_diagnostics uses).
    for note_key, counts in model.parser_notes.items():
        value = ", ".join(f"{k}={v}" for k, v in counts.items()) if counts else "-"
        lines.append(f"{note_key}: {value}")
    return lines


def _help_html(pairs: list[tuple[str, str]], summary: str = "How to read this") -> str:
    """A collapsed ``<details>`` block, or "" when there is no help."""
    if not pairs:
        return ""
    items = "".join(f"<dt>{_esc(heading)}</dt><dd>{_esc(pages.plain(text))}</dd>" for heading, text in pairs)
    return f'<details class="help"><summary>{_esc(summary)}</summary><dl>{items}</dl></details>'


def _table_html(table: Table, currency: str, table_id: str, units=None) -> str:
    head_cells = []
    for column in table.columns:
        cls_attr = ' class="num"' if column.kind in _NUMERIC_KINDS else ""
        head_cells.append(
            f'<th{cls_attr} data-key="{_esc(column.key)}">{_esc(column.label)}</th>'
        )
    thead = f"<thead><tr>{''.join(head_cells)}</tr></thead>"

    body_rows = []
    for row in table.rows:
        cells = []
        for value, column in zip(row, table.columns):
            display = _esc(display_cell(value, column, table, currency, units))
            cls_attr = ' class="num"' if column.kind in _NUMERIC_KINDS else ""
            sort_value = "" if value is None else str(value)
            cell_html = display
            if column.kind == "pct" and value is not None:
                try:
                    pct = max(0.0, min(100.0, float(value)))
                except (TypeError, ValueError):
                    pct = 0.0
                cell_html = (
                    f'<span class="bar" style="width:{pct:.1f}%"></span>'
                    f'<span class="bar-text">{display}</span>'
                )
            cells.append(
                f'<td{cls_attr} data-sort="{_esc(sort_value)}">{cell_html}</td>'
            )
        body_rows.append(f"<tr>{''.join(cells)}</tr>")
    tbody = f"<tbody>{''.join(body_rows)}</tbody>"

    notes_html = ""
    if table.notes:
        notes_html = (
            '<ul class="notes">'
            + "".join(f"<li>{_esc(pages.plain(note))}</li>" for note in table.notes)
            + "</ul>"
        )

    column_help = _help_html(
        [(column.label, column.help) for column in table.columns if column.help], "What the columns mean"
    )
    return (
        f"<h3>{_esc(table.title)}</h3>"
        f"{_help_html(help_parts(table.help))}"
        f'<table class="sortable" id="{_esc(table_id)}">{thead}{tbody}</table>'
        f"{column_help}"
        f"{notes_html}"
    )


def _sections_html(model: ReportModel) -> str:
    currency = model.meta.pricing.currency
    units = model.units
    parts = []
    for section_index, section in enumerate(model.sections):
        parts.append(f"<section><h2>{_esc(section.title)}</h2>")
        if section.intro:
            parts.append(f'<p class="intro">{_esc(pages.plain(section.intro))}</p>')
        parts.append(_help_html(help_parts(section.help)))
        for table_index, table in enumerate(section.tables):
            table_id = f"table-{section_index}-{table_index}"
            parts.append(_table_html(table, currency, table_id, units))
        if section.notes:
            parts.append(
                '<ul class="notes">'
                + "".join(f"<li>{_esc(pages.plain(note))}</li>" for note in section.notes)
                + "</ul>"
            )
        parts.append("</section>")
    return "".join(parts)


def _fix_html(fix: dict) -> str:
    """One ``fixes.build_fix`` entry, collapsed: explainer, prompt,
    (for a plain setting) the dry-run command, and the note
    ``fixes.fix_note`` picks for it (the restart reminder by default, the
    "where should this apply" note for a scope prompt, or nothing)."""
    subject = fix_subject(fix)
    parts = [f'<details class="help"><summary>{_esc("How to make this change" + subject)}</summary>']
    if fix.get("explainer"):
        parts.append("<dl>")
        for heading, text in fix["explainer"]:
            parts.append(f"<dt>{_esc(heading)}</dt><dd>{_esc(pages.plain(text))}</dd>")
        parts.append("</dl>")
    # UX-8: a purely informational workflow card (fixes.build_fixes) has
    # an explainer but no prompt -- nothing to ask Claude to do.
    if fix.get("prompt"):
        parts.append(f"<p>Ask Claude to do it:</p><pre>{_esc(fix['prompt'])}</pre>")
    if fix.get("command"):
        parts.append(
            "<p>Or run this command (it only shows the change; run it again without --dry-run to make it):</p>"
            + (f"<p><strong>{_esc(fix['command_warning'])}</strong></p>" if fix.get("command_warning") else "")
            + f"<pre>{_esc(fix['command'])}</pre>"
        )
    note = fix_note(fix)
    if note:
        parts.append(f"<p>{_esc(note)}</p>")
    parts.append("</details>")
    return "".join(parts)


def _recommendations_html(model: ReportModel) -> str:
    if not model.recommendations:
        return "<p>None.</p>"
    currency = model.meta.pricing.currency
    units = model.units
    parts = []
    for rec in model.recommendations:
        parts.append('<article class="rec">')
        parts.append(f"<h3>{_esc(SEVERITY_LABELS.get(rec.severity, rec.severity))}: {_esc(rec.title)}</h3>")
        if rec.why:
            parts.append(f"<p>{_esc(pages.plain(rec.why))}</p>")
        parts.append(f"<p>What to do: {_esc(pages.plain(rec.action))}</p>")
        if rec.estimated_saving:
            parts.append(f"<p><strong>Estimated saving:</strong> {_esc(pages.plain(rec.estimated_saving))}</p>")
        if rec.lever and not rec.fixes:
            scope = SCOPE_LABELS.get(rec.scope, rec.scope)
            parts.append(f"<p>Setting to change: {_esc(rec.lever)} ({_esc(scope)})</p>")
        if rec.scope != "managed":
            parts.extend(_fix_html(fix) for fix in rec.fixes)
        if rec.evidence:
            parts.append('<details><summary>The numbers behind this</summary><ul class="evidence-list">')
            for label, value, source_table, row_key in rec.evidence:
                # Fix A3: format the cited value using its home table
                # column's kind, same as the Markdown renderer -- see
                # render/tables.py's module docstring.
                formatted = format_evidence_value(model, value, source_table, row_key, currency, units)
                text = f"{label}: {formatted} ({evidence_source(model, source_table, row_key)})"
                parts.append(f"<li>{_esc(text)}</li>")
            parts.append("</ul></details>")
        parts.append("</article>")
    return "".join(parts)


def render_html(model: ReportModel) -> str:
    """Render ``model`` as a single, self-contained HTML document with
    inline CSS and an optional inline click-to-sort script.
    """
    meta_html = _list_html(_meta_lines(model), "meta-list")
    assumptions_html = _list_html(_assumptions_lines(model), "assumptions-list")
    sections_html = _sections_html(model)
    recommendations_html = _recommendations_html(model)
    diagnostics_html = _list_html(_diagnostics_lines(model), "diagnostics-list")

    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        f"<title>{_esc('ClaudeGlass report')}</title>\n"
        f"<style>{_STYLE}</style>\n"
        "</head>\n"
        "<body>\n"
        "<h1>ClaudeGlass report</h1>\n"
        f"<section><h2>Overview</h2>{meta_html}</section>\n"
        f"<section><h2>Assumptions</h2>{assumptions_html}</section>\n"
        f"{sections_html}\n"
        f"<section><h2>Recommendations</h2>{recommendations_html}</section>\n"
        f"<section><h2>Diagnostics</h2>{diagnostics_html}</section>\n"
        f"<script>{_SCRIPT}</script>\n"
        "</body>\n"
        "</html>\n"
    )


__all__ = ["render_html"]

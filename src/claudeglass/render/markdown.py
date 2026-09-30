"""Markdown renderer: ``ReportModel`` -> a single GitHub-flavoured
Markdown document.

Layout (fixed, see the project plan's "Renderers and CLI" section):

1. ``# ClaudeGlass report``
2. A meta block (tool version, generated at, window, projects, pricing
   provenance, billing mode, thresholds).
3. ``## Assumptions`` (from ``model.meta.assumptions``).
4. ``## <Section.title>`` per section, each ``Table`` as a GitHub pipe
   table plus its notes. With ``explain=True`` (``report --explain``)
   each section also gets its intro and "how to read this" help, and
   each table its help and a column glossary.
5. ``## Recommendations``.
6. ``## Diagnostics``.

No emoji. British English in every fixed string this module writes.
"""

from __future__ import annotations

import dataclasses

from .. import pages
from ..fixes import fix_note
from ..model import Diagnostics, ReportModel, Table
from .tables import SCOPE_LABELS, SEVERITY_LABELS, display_cell, display_row, fix_subject, escape_md, evidence_source, format_evidence_value, help_parts

#: Column kinds that read as quantities and so are right-aligned by
#: default in a pipe table, unless the column overrides ``align``.
_NUMERIC_KINDS = frozenset({"int", "float", "pct", "money", "tokens", "secs"})


def _align_for(kind: str, align: str | None) -> str:
    if align in ("left", "right", "center"):
        return align
    return "right" if kind in _NUMERIC_KINDS else "left"


def _alignment_marker(align: str) -> str:
    if align == "right":
        return "---:"
    if align == "center":
        return ":---:"
    return "---"


def _render_meta(model: ReportModel) -> list[str]:
    meta = model.meta
    pricing = meta.pricing
    lines = [
        f"- Tool version: {meta.tool_version or '-'}",
        f"- Generated at: {meta.generated_at or '-'}",
        f"- Window: {meta.window or '-'}",
        f"- Projects: {', '.join(meta.projects) if meta.projects else '-'}",
        "- Pricing: "
        + ", ".join(
            [
                f"path={pricing.path or '-'}",
                f"version={pricing.version or '-'}",
                f"sha8={pricing.sha8 or '-'}",
                f"currency={pricing.currency}",
                f"coverage={pricing.coverage_pct:.1f}%",
            ]
        ),
        f"- Billing mode: {meta.billing_mode}" + (f" ({meta.billing_source})" if meta.billing_source else ""),
    ]
    if meta.thresholds:
        thresholds = ", ".join(f"{k}={v}" for k, v in meta.thresholds.items())
    else:
        thresholds = "-"
    lines.append(f"- Thresholds: {thresholds}")
    return lines


def _render_assumptions(model: ReportModel) -> list[str]:
    lines = ["## Assumptions", ""]
    if model.meta.assumptions:
        lines.extend(f"- {text}" for text in model.meta.assumptions)
    else:
        lines.append("- None recorded.")
    return lines


def _help_lines(help_) -> list[str]:
    lines = [f"**{heading}.** {pages.plain(text)}" for heading, text in help_parts(help_)]
    return [line for pair in zip(lines, [""] * len(lines)) for line in pair]


def _render_table(table: Table, currency: str, explain: bool = False, units=None) -> list[str]:
    lines = [f"### {table.title}", ""]
    if explain:
        lines.extend(_help_lines(table.help))
    aligns = [_align_for(column.kind, column.align) for column in table.columns]
    header = "| " + " | ".join(escape_md(column.label) for column in table.columns) + " |"
    divider = "| " + " | ".join(_alignment_marker(a) for a in aligns) + " |"
    lines.append(header)
    lines.append(divider)
    group = None
    for row in table.rows:
        row_group = table.row_groups.get(row[0]) if row and isinstance(row[0], str) else None
        if row_group and row_group != group:
            group = row_group
            lines.append("| " + " | ".join([f"**{escape_md(group)}**"] + [""] * (len(table.columns) - 1)) + " |")
        cells = [escape_md(text) for text in display_row(row, table, currency, units)]
        lines.append("| " + " | ".join(cells) + " |")
    if table.notes:
        lines.append("")
        lines.extend(f"- {pages.plain(note)}" for note in table.notes)
    if explain and any(column.help for column in table.columns):
        lines.append("")
        lines.append("Columns:")
        lines.extend(f"- {column.label}: {pages.plain(column.help)}" for column in table.columns if column.help)
    return lines


def _render_sections(model: ReportModel, explain: bool = False) -> list[str]:
    currency = model.meta.pricing.currency
    units = model.units
    lines: list[str] = []
    for section in model.sections:
        lines.append(f"## {section.title}")
        lines.append("")
        if explain:
            if section.intro:
                lines.extend([pages.plain(section.intro), ""])
            lines.extend(_help_lines(section.help))
        for table in section.tables:
            lines.extend(_render_table(table, currency, explain, units))
            lines.append("")
        if section.notes:
            lines.extend(f"- {pages.plain(note)}" for note in section.notes)
            lines.append("")
    return lines


def _render_fix(fix: dict) -> list[str]:
    """One ``fixes.build_fix`` entry: the explainer, the prompt for
    Claude, for a plain setting the dry-run command, and the note
    ``fixes.fix_note`` picks for it (the restart reminder by default, the
    "where should this apply" note for a scope prompt, or nothing)."""
    lines: list[str] = []
    if fix.get("explainer"):
        subject = fix_subject(fix)
        lines += ["", f"What you're changing{subject}:", ""]
        lines += [f"- **{heading}.** {pages.plain(text)}" for heading, text in fix["explainer"]]
    # UX-8: a purely informational workflow card (fixes.build_fixes) has
    # an explainer but no prompt -- nothing to ask Claude to do.
    if fix.get("prompt"):
        lines += ["", "Ask Claude to do it:", "", "```text", fix["prompt"], "```"]
    if fix.get("command"):
        lines += [
            "",
            "Or run this command (it only shows the change; run it again without --dry-run to make it):",
            *(["", fix["command_warning"]] if fix.get("command_warning") else []),
            "",
            "```bash",
            fix["command"],
            "```",
        ]
    note = fix_note(fix)
    if note:
        lines += ["", note]
    return lines


def _render_recommendations(model: ReportModel) -> list[str]:
    currency = model.meta.pricing.currency
    units = model.units
    lines = ["## Recommendations", ""]
    if not model.recommendations:
        lines.append("None.")
        return lines
    for rec in model.recommendations:
        lines.append(f"### {SEVERITY_LABELS.get(rec.severity, rec.severity)}: {rec.title}")
        lines.append("")
        if rec.why:
            lines.append(pages.plain(rec.why))
            lines.append("")
        lines.append(f"What to do: {pages.plain(rec.action)}")
        if rec.estimated_saving:
            lines.append("")
            lines.append(f"Estimated saving: {pages.plain(rec.estimated_saving)}")
        if rec.lever and not rec.fixes:
            lines.append("")
            lines.append(f"Setting to change: {rec.lever} ({SCOPE_LABELS.get(rec.scope, rec.scope)})")
        if rec.scope != "managed":
            for fix in rec.fixes:
                lines.extend(_render_fix(fix))
        if rec.evidence:
            lines.append("")
            lines.append("The numbers behind this:")
            for label, value, source_table, row_key in rec.evidence:
                # Fix A3: format the cited value using its home table
                # column's kind (e.g. "63.7%", "47,345 tokens") instead
                # of printing the raw float -- see render/tables.py's
                # module docstring.
                formatted = format_evidence_value(model, value, source_table, row_key, currency, units)
                lines.append(f"- {label}: {formatted} ({evidence_source(model, source_table, row_key)})")
        lines.append("")
    return lines


def _render_diagnostics(model: ReportModel) -> list[str]:
    lines = ["## Diagnostics", ""]
    diagnostics = model.diagnostics
    for field_def in dataclasses.fields(Diagnostics):
        value = getattr(diagnostics, field_def.name)
        if isinstance(value, dict):
            if value:
                value = ", ".join(f"{k}={v}" for k, v in value.items())
            else:
                value = "-"
        lines.append(f"- {field_def.name}: {value}")
    # Parser-signals addition (SURV-6/7, see model.py's module docstring):
    # ReportModel.parser_notes is a sibling side channel to Diagnostics,
    # not one of its fields, so it's printed the same way just below.
    for note_key, counts in model.parser_notes.items():
        value = ", ".join(f"{k}={v}" for k, v in counts.items()) if counts else "-"
        lines.append(f"- {note_key}: {value}")
    return lines


def render_markdown(model: ReportModel, explain: bool = False) -> str:
    """Render ``model`` as a single Markdown document (see module
    docstring for the fixed section order). ``explain`` adds the help
    text ``helptext.annotate`` put on each section, table and column.
    """
    lines: list[str] = ["# ClaudeGlass report", ""]
    lines.extend(_render_meta(model))
    lines.append("")
    lines.extend(_render_assumptions(model))
    lines.append("")
    lines.extend(_render_sections(model, explain))
    lines.extend(_render_recommendations(model))
    lines.append("")
    lines.extend(_render_diagnostics(model))
    return "\n".join(lines).rstrip("\n") + "\n"


__all__ = ["render_markdown"]

"""``helptext.py``: every table has a dashboard placement, the sections
that have been rewritten carry help on every kept table and column, and
the copy follows ``docs/writing-help.md`` (no internal names, no banned
terms).

``NOT_YET_COVERED`` is a shrinking allowlist: sections still waiting for
their plain-English copy. A section that becomes fully covered fails the
test until it is removed from the list, so the list can only shrink.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from claudeglass import helptext
from claudeglass.config import Config
from claudeglass.corpus import load_corpus
from claudeglass.model import DASHBOARD_PLACEMENTS
from claudeglass.pricing import load_pricing
from claudeglass.report import build_report

SRC = Path(__file__).resolve().parent.parent / "src" / "claudeglass"

#: Sections whose tables don't have help yet. Remove a key once its copy
#: is written in ``helptext.py``.
NOT_YET_COVERED: set[str] = set()

#: Words the house style replaces (see the "Words to use" table).
BANNED = re.compile(
    r"\b(re-?cache[sd]?|top-level|briefing|cache_creation|cache_read|attribution_\w+|per_turn_\w+|tabs?)\b", re.I
)
SNAKE_CASE = re.compile(r"\b[a-z]+_[a-z0-9_]+\b")
#: A setting key or field name written in camelCase ("autoCompactWindow").
CAMEL_CASE = re.compile(r"\b[a-z]+[A-Z][A-Za-z]*\b")
#: Words the "Dashboard copy" rules rule out.
FILLER = re.compile(r"\b(just|simply)\b", re.I)
#: The hard limit on a sentence (``docs/writing-help.md``: aim for under 20).
MAX_SENTENCE_WORDS = 25


def _static_table_names() -> set[str]:
    """Every literal table name a builder passes as ``Table(name=...)`` or
    as the first argument of a ``_build_*_table`` helper."""
    names: set[str] = set()
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            if func == "Table":
                for kw in node.keywords:
                    if kw.arg == "name" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                        names.add(kw.value.value)
            elif func.startswith("_build_") and func.endswith("_table") and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    names.add(first.value)
    return names


def test_every_table_has_a_placement():
    names = _static_table_names()
    assert names, "the scan found no tables"
    unplaced = sorted(
        n for n in names if n not in helptext.PLACEMENT and not any(n.startswith(p) for p, _ in helptext.PLACEMENT_PREFIXES)
    )
    assert unplaced == [], f"add these tables to helptext.PLACEMENT: {unplaced}"


def _source_strings() -> set[str]:
    """Every string literal in the package outside ``helptext.py``: table
    names that are built at run time still appear as a literal."""
    found: set[str] = set()
    for path in SRC.rglob("*.py"):
        if path.name == "helptext.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                found.add(node.value)
    return found


def test_placements_are_valid_and_refer_to_real_tables():
    names = _static_table_names() | _source_strings()
    assert all(p in DASHBOARD_PLACEMENTS for p in helptext.PLACEMENT.values())
    assert all(p in DASHBOARD_PLACEMENTS for _, p in helptext.PLACEMENT_PREFIXES)
    stale = sorted(n for n in helptext.PLACEMENT if n not in names)
    assert stale == [], f"helptext.PLACEMENT names tables no builder emits: {stale}"
    assert sorted(n for n in helptext.TABLE_COPY if n not in names) == []


def _copy_strings():
    for key, copy in helptext.SECTION_COPY.items():
        yield f"section {key}", copy.title
        yield f"section {key}", copy.intro
        if copy.help:
            yield from ((f"section {key}", s) for s in (copy.help.shows, copy.help.read, copy.help.act))
    for name, copy in helptext.TABLE_COPY.items():
        yield f"table {name}", copy.title
        if copy.help:
            yield from ((f"table {name}", s) for s in (copy.help.shows, copy.help.read, copy.help.act))
        for col, (label, help_text) in copy.columns.items():
            yield f"{name}.{col}", label
            yield f"{name}.{col}", help_text
        for raw, label in copy.value_labels.items():
            yield f"{name} value {raw}", label
    for col, help_text in helptext.COMMON_COLUMN_HELP.items():
        yield f"common {col}", help_text


@pytest.mark.parametrize("where,text", [pair for pair in _copy_strings() if pair[1]])
def test_copy_has_no_internal_names_or_banned_terms(where, text):
    assert not SNAKE_CASE.search(text), f"{where}: internal name in {text!r}"
    assert not BANNED.search(text), f"{where}: banned term in {text!r}"


@pytest.mark.parametrize("where,text", [pair for pair in _copy_strings() if pair[1]])
def test_copy_keeps_to_short_plain_sentences(where, text):
    """One idea per sentence, 25 words at most, no " -- " aside, no
    "just" or "simply", and no camelCase setting key: a setting's key
    belongs in a fix's command or prompt, not in help."""
    for sentence in re.split(r"(?<=[.?!])\s+", text):
        assert len(sentence.split()) <= MAX_SENTENCE_WORDS, f"{where}: sentence over 25 words: {sentence!r}"
    assert " -- " not in text, f"{where}: dash aside in {text!r}"
    assert not FILLER.search(text), f"{where}: filler word in {text!r}"
    assert not CAMEL_CASE.search(text), f"{where}: camelCase name in {text!r}"


def test_the_settings_help_says_changes_use_every_snapshot_not_only_the_window():
    read = helptext.SECTION_COPY["config"].help.read
    assert "This uses every snapshot recorded, not only this window's." in read


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    project_dir = tmp_path_factory.mktemp("help") / "proj"
    project_dir.mkdir()
    return build_report(load_corpus([project_dir]), load_pricing(), Config(), projects=("proj",), window="w")


def test_covered_sections_have_help_on_every_kept_table_and_column(report):
    missing: list[str] = []
    for section in report.sections:
        if section.key in NOT_YET_COVERED:
            continue
        for table in section.tables:
            assert table.dashboard == helptext.placement_for(table.name)
            if table.dashboard == "report":
                continue
            if table.help is None or not table.help.shows:
                missing.append(f"{table.name}: no help")
            missing.extend(f"{table.name}.{c.key}: no column help" for c in table.columns if not c.help)
    assert missing == []


def test_not_yet_covered_list_only_shrinks(report):
    done = []
    for section in report.sections:
        if section.key not in NOT_YET_COVERED:
            continue
        kept = [t for t in section.tables if t.dashboard != "report"]
        if kept and all(t.help and t.help.shows and all(c.help for c in t.columns) for t in kept):
            done.append(section.key)
    assert done == [], f"remove from NOT_YET_COVERED: {done}"


#: The columns of ``agent_models.build_table``, in order.
AGENT_MODEL_COLUMNS = [
    "case", "agent_type", "verdict", "runs", "roles", "model", "cost_usd", "cost_on_sonnet_usd", "saving_usd",
    "saving_pct", "write_turns", "workflow_runs", "first_seen", "last_seen", "later_compliant", "env_var_set",
]


def test_the_agent_models_table_is_kept_and_every_column_has_copy():
    name = "model_swap_agent_models"
    copy = helptext.TABLE_COPY[name]
    assert helptext.placement_for(name) == "keep"
    assert list(copy.columns) == AGENT_MODEL_COLUMNS
    for key, (label, text) in copy.columns.items():
        assert label and text, key
    assert copy.help and copy.help.shows and copy.help.read and copy.help.act
    assert copy.lead_columns[0] == "case" and len(copy.lead_columns) <= 7
    assert all(key in copy.columns for key in copy.lead_columns)
    assert copy.columns["model"][1] == "The model most of these agents ran on."
    env_help = copy.columns["env_var_set"][1]
    assert "force" not in env_help and "is set now" in env_help


def test_the_agent_models_verdict_help_explains_each_finding_in_plain_words():
    copy = helptext.TABLE_COPY["model_swap_agent_models"]
    verdict_help = copy.columns["verdict"][1]
    for raw, words in (
        ("inherited", "No model set"),
        ("asked", "Asked for a larger model"),
        ("decide-apply", "Decided and changed code"),
    ):
        assert copy.value_labels[raw] == words
        assert words in verdict_help


@pytest.mark.parametrize("billing", ["api", "subscription"])
def test_annotating_the_agent_models_table_fills_its_help_and_column_help(billing):
    from claudeglass.model import Column, Section, Table

    kinds = {"cost_usd": "money", "cost_on_sonnet_usd": "money", "saving_usd": "money", "saving_pct": "pct"}
    table = Table(
        name="model_swap_agent_models",
        columns=[Column(key=key, label=key, kind=kinds.get(key, "str")) for key in AGENT_MODEL_COLUMNS],
        rows=[],
        value_labels={"workflow-subagent:inherited": "Workflow agents, no model set"},
    )
    helptext.annotate_section(Section(key="model_swap", tables=[table]), billing)
    assert table.dashboard == "keep"
    assert table.title == "Agents that ran on a larger model than their work needed"
    assert table.help and table.help.shows
    assert [c.label for c in table.columns][:4] == ["Agents and finding", "Agent", "Finding", "Agents"]
    assert all(c.help for c in table.columns)
    # The table's own row labels stay beside the copy's.
    assert table.value_labels["workflow-subagent:inherited"] == "Workflow agents, no model set"
    assert table.value_labels["inherited"] == "No model set"
    if billing == "subscription":
        assert all("list price" in c.help for c in table.columns if c.kind == "money")


def test_annotate_keeps_names_keys_and_rows(report):
    # The annotated report still carries the raw table names recommend()
    # and the JSON/CSV exports use.
    names = {t.name for s in report.sections for t in s.tables}
    assert "topology_spawn_write" in names
    assert "recache_summary" in names
    spawn = next(t for s in report.sections for t in s.tables if t.name == "topology_spawn_write")
    assert [c.key for c in spawn.columns][:2] == ["agent_type", "spawns"]


def test_subscription_money_columns_say_list_price(tmp_path):
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    sub = build_report(load_corpus([project_dir]), load_pricing(), Config(billing="subscription"), projects=("proj",), window="w")
    money = [c for s in sub.sections for t in s.tables for c in t.columns if c.kind == "money"]
    assert money and all("list price" in c.help for c in money)


def _labelled_model():
    from claudeglass.model import Column, Help, ReportModel, Section, Table

    table = Table(
        name="t",
        title="Things",
        columns=[Column(key="transcript_kind", label="Kind", kind="str", help="Main session or subagents.")],
        rows=[["top-level"]],
        help=Help(shows="Each row is a kind.", read="Bigger is more.", act=""),
        value_labels={"top-level": "Main session"},
    )
    return ReportModel(sections=[Section(key="s", title="Section", tables=[table], intro="An intro.")])


def test_markdown_labels_values_and_explains_only_on_request():
    from claudeglass.render.markdown import render_markdown

    model = _labelled_model()
    plain = render_markdown(model)
    assert "| Main session |" in plain and "top-level" not in plain
    assert "What it shows" not in plain and "An intro." not in plain
    explained = render_markdown(model, explain=True)
    assert "**What it shows.** Each row is a kind." in explained
    assert "When to act" not in explained  # empty parts are left out
    assert "- Kind: Main session or subagents." in explained
    assert "An intro." in explained


def test_html_help_is_collapsed_and_json_csv_keep_raw_values(tmp_path):
    import json

    from claudeglass.render.csv_out import write_csv_dir
    from claudeglass.render.html import render_html
    from claudeglass.render.json_out import render_json

    model = _labelled_model()
    html = render_html(model)
    assert '<details class="help"><summary>How to read this</summary>' in html
    assert "Main session" in html
    table = json.loads(render_json(model))["report"]["sections"][0]["tables"][0]
    assert table["rows"] == [["top-level"]]
    assert table["value_labels"] == {"top-level": "Main session"}
    write_csv_dir(model, tmp_path)
    assert "top-level" in "".join(p.read_text(encoding="utf-8") for p in tmp_path.glob("*.csv"))


def test_every_diagnostics_field_has_a_plain_label():
    import dataclasses

    from claudeglass.model import Diagnostics

    fields = {f.name for f in dataclasses.fields(Diagnostics)}
    assert set(helptext.DIAGNOSTIC_LABELS) == fields
    for key, (label, meaning) in helptext.DIAGNOSTIC_LABELS.items():
        assert label and meaning, key
        assert not SNAKE_CASE.search(label + " " + meaning), key
    table = helptext.diagnostics_table(Diagnostics(modes={"plan": 2}, truncated_final_line=True))
    by_key = {row[0]: row[1] for row in table.rows}
    assert by_key["modes"] == "plan: 2"
    assert by_key["truncated_final_line"] == "yes"
    assert table.value_labels["lines"] == "Lines read"


def test_usage_log_tables_have_help_and_measured_causes_sit_on_the_cache_tab(tmp_path):
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    rows = [{"session_id": "s1", "cache_warm": False, "cache_misses": 2, "cache_miss_causes": "tools:2"}]
    model = build_report(
        load_corpus([project_dir]), load_pricing(), Config(), projects=("proj",), window="w", usage_log_rows=rows
    )
    tables = {t.name: (s.key, t) for s in model.sections for t in s.tables}
    section_key, measured = tables["measured_miss_causes"]
    assert section_key == "recache"
    for name in ("measured_miss_causes", "cache_ground_truth"):
        table = tables[name][1]
        assert table.help and table.help.shows, name
        assert all(c.help for c in table.columns), name

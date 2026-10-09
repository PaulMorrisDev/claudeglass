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


def _plain(text: str, where: str) -> None:
    """The copy rules for one string the dashboard shows or a copyable
    prompt carries: no internal name, banned term, filler or dash aside,
    and no sentence over 25 words."""
    assert not SNAKE_CASE.search(text), f"{where}: internal name in {text!r}"
    assert not BANNED.search(text), f"{where}: banned term in {text!r}"
    assert not FILLER.search(text), f"{where}: filler word in {text!r}"
    assert " -- " not in text, f"{where}: dash aside in {text!r}"
    for sentence in re.split(r"(?<=[.?!])\s+", text):
        assert len(sentence.split()) <= MAX_SENTENCE_WORDS, f"{where}: sentence over 25 words: {sentence!r}"


def test_the_copy_the_feedback_answers_add_keeps_to_the_help_rules():
    """What your /cg-feedback answers add to the habits, the tips table,
    the plan-handoff card and the coaching summary: the new strings
    follow the same rules as the help text."""
    from claudeglass import capture_catalogue, coaching, fixes, habits, prompting
    from claudeglass.capture_catalogue import MISSED_IN_LINES
    from claudeglass.habits import Habits, Piece, SessionShape

    strings: list[tuple[str, str]] = []
    for word, line in MISSED_IN_LINES.items():
        strings.append((f"missed_in line {word or 'unsaid'}", line))
    for table in (habits.EXAMPLES, habits.BASES, habits.WHERE, habits.TRADE_OFFS, habits.UNDO):
        value = table["check_work"]
        strings.extend(("check_work", part) for part in ((value,) if isinstance(value, str) else value))
    strings.append(("check_work title", habits.ITEMS["check_work"][1]))
    for word, (title, where) in habits._CHECK_WORK_VARIANTS.items():
        strings.extend((f"check_work {word}", part) for part in (title, where) if part)
    strings += [(f"place {word}", place) for word, place in habits._MISSED_PLACES.items()]
    strings.append(("plan_first basis", prompting.BASIS["plan_first"]))
    # The explainer rows are shown; the prompt is text for Claude, which the other cards' prompts are too.
    strings += [("plan-handoff:fuller_plans", part) for part in fixes._WORKFLOW_EXPLAINER["plan-handoff:fuller_plans"]]
    section = prompting.build_section([prompting.SessionPrompting("s", None, notes={"drip_feed": 1})])
    strings += [("tips table", note) for note in section.tables[1].notes]
    data = {
        "muted": ["status_poll"], "once": ["big_paste"], "plan_fresh": False,
        "thresholds": {"drip_count": 9, "cold_min_tokens": 1_000_000},
    }
    strings += [("coaching summary", line) for line in coaching.describe(data)]
    strings += [("excused note", note) for note in habits._excused_notes(Habits(cycles=[
        habits.CycleFact("s", None, "", 1.0, 1, None, excused=True)]))]
    for where, text in strings:
        _plain(text, where)

    # The evidence the cards show once there are answers.
    gave = dict(
        outcome="partly", cost=1.0, cycles=3, task="feature", slow=(), source="your feedback", why_given=True,
        helped_given=True,
    )
    pieces = [
        Piece(**gave, why=("left_out", "missed"), missed_in="plan", plan="covered", helped=("context", "plan"),
              followups=3, followup_cost=2.0, followup_tokens=12_000)
        for _ in range(3)
    ]
    h = Habits(
        pieces=pieces,
        cycles=[habits.CycleFact("s", None, "", 1.0, 1, None, plan_check="covered", tokens=2_000, redone=True)],
        shapes=[SessionShape("plan_build", 1.0, 80_000)],
    )
    items = habits.playbook(h)
    assert {"brief_clearly", "check_work"} <= {item.key for item in items}
    assert any("pieces cheaper" in item.evidence for item in items)
    for item in items:
        _plain(item.evidence, f"{item.key} evidence")
        _plain(item.title or habits.item_title(item), f"{item.key} title")


def test_the_plan_handoff_card_words_your_answers_add_keep_to_the_help_rules():
    """The card's opening explanation is older and longer; what your
    answers add to it, and the actions they change, follow the rules."""
    from test_handoff import RULES, HandoffThresholds, _report

    reports = [
        _report(3, answers=("no", "no", "partly")),
        _report(3, answers=("yes", "yes", "yes", "no"), costly=3),
        _report(3, plans=("gap", "gap", "covered")),
        _report(3, answers=("no", "no", "no"), plans=("gap", "gap", "gap"), costly=3),
    ]
    plain = RULES[0](_report(3), HandoffThresholds())[0]
    for report in reports:
        [rec] = RULES[0](report, HandoffThresholds())
        _plain(rec.title, "plan-handoff title")
        for label, *_ in rec.evidence:
            _plain(label, "plan-handoff evidence")
        for sentence in re.split(r"(?<=[.?!])\s+", rec.why):
            if sentence.startswith("You "):
                _plain(sentence, "plan-handoff why")
        if rec.action != plain.action:
            _plain(rec.action, "plan-handoff action")


def test_the_rating_questions_and_feedback_items_keep_to_the_help_rules():
    """The questions the dashboard's session rating shows, and what the
    Capture page says each feedback item captures and why."""
    from claudeglass import capture_catalogue

    for q in capture_catalogue.FEEDBACK_QUESTIONS:
        for text in (q.question, q.short, *(o[1] for o in q.options), *(o[2] for o in q.options)):
            _plain(text, f"question {q.key}")
    for metric in capture_catalogue.METRICS:
        if metric.group == "feedback":
            _plain(metric.what, f"{metric.id} what")
            _plain(metric.why, f"{metric.id} why")


def test_the_copy_the_rework_section_builds_keeps_to_the_help_rules():
    """Every sentence, Try line, line to copy and note rework.py writes,
    for the API and for a subscription, from a section with every row: the
    same rules as the help text."""
    from claudeglass import capture_catalogue, habits, pieces, rework
    from claudeglass.model import Feedback
    from claudeglass.units import Units
    from test_rework import _admitting, _h, _pieces, _week_piece
    from test_pieces import _aside, _build, _fix, _msg, _tag

    pricing_min = load_pricing(path=Path(__file__).resolve().parent / "fixtures" / "pricing_min.toml")
    work = [
        *_pieces(fixes=[{"tag": _tag(shift="fix", why=why)} for why in ("left_out", "missed", "changed", "tools")]),
        *_pieces(fixes=[{"human_correction": True}], feedback=Feedback(source="answers", why=("missed",), missed_in="plan")),
        *pieces.pieces_of([_build(0), _fix(10, human_correction=True, admit_caught="user", **_admitting())], rates=pricing_min),
        *pieces.pieces_of([_build(0), _fix(10, human_correction=True), _fix(20, human_correction=True), _msg(30)], rates=pricing_min),
        *[_week_piece(0, reworked=True) for _ in range(rework.MIN_WEEK_REWORKED)],
    ]
    possible = _msg(10, files=("c",), human_prompt_chars=400)
    possible.facts = {"admit_candidate": True}
    work += pieces.pieces_of([_build(0), possible, _msg(20, tag=_tag(shift="new"))])
    work += pieces.pieces_of([_build(0), _aside(5), _aside(6)], rates=pricing_min)
    plans = [habits.PlanFix(shape="plan_build", typed=3, cost=0.5) for _ in range(habits.MIN_GROUP)]
    strings: list[tuple[str, str]] = [(f"try {cause}", line) for cause, line in rework.TRY.items()]
    strings += [(f"paste {cause}", line) for cause, line in rework.PASTE.items() if line]
    strings += [(f"missed_in {word or 'unsaid'}", line) for word, line in capture_catalogue.MISSED_IN_LINES.items()]
    strings += [("possible", rework.possible_text(n)) for n in (1, 3)]
    for units in (Units(), Units(billing_mode="subscription")):
        section = rework.build_section(_h(*work, plan_fixes=plans), units)
        strings += [("note", note) for note in section.notes]
        for table in section.tables:
            texts = [c.key for c in table.columns if c.key in ("text", "detail", "try", "fix")]
            for row in table.rows:
                cells = dict(zip((c.key for c in table.columns), row))
                strings += [(f"{table.name}.{key}", cells[key]) for key in texts if cells[key]]
    assert {w for w, _ in strings} >= {"rework_headline.text", "rework_causes.detail", "rework_admitted.text"}
    assert any(text.startswith("Not counted as rework: 2 messages you sent while background work ran") for _, text in strings)
    for where, text in strings:
        _plain(text, where)


def test_the_rework_help_says_what_makes_a_follow_up_rework_and_what_does_not():
    """A flag or a tag makes a follow-up rework. A short message that only
    changes the same files again does not, and the help no longer says it
    asks for a change to the same files."""
    read = helptext.TABLE_COPY["rework_headline"].help.read
    assert "corrected Claude, adjusted work it had changed, or its tag called it a fix or a redo." in read
    assert "A short message that only changes the same files again does not count on its own." in read
    assert "asked again for a change to the same files" not in read


def test_the_rework_help_calls_asides_messages_you_sent_while_background_work_ran():
    """Asides are side questions and messages that steer the running work, so
    no help text calls them side questions."""
    headline = helptext.TABLE_COPY["rework_headline"]
    by_level = helptext.TABLE_COPY["rework_by_level"]
    texts = [
        headline.help.read,
        *(text for _label, text in headline.columns.values()),
        *headline.value_labels.values(),
        *(text for _label, text in by_level.columns.values()),
    ]
    assert "Messages you sent while background work ran that changed no files" in headline.help.read
    assert "messages you sent while background work ran" in headline.columns["count"][1]
    assert "Messages you sent while background work ran that changed no files are left out." in by_level.columns["requests"][1]
    assert not [text for text in texts if "side question" in text.lower()]


def test_the_copy_the_failed_calls_check_and_the_work_habits_row_add_keeps_to_the_help_rules(tmp_path):
    """The "Failed and blocked tool calls" check: its question and why, the
    fix's explainer and the prompt to copy, and what it says when it finds
    something, when it finds little and when it finds nothing; and the
    sentence the Work habits row adds under its rework lead and its saving."""
    from claudeglass import quick_actions as qa
    from claudeglass import waste
    from test_quick_actions import _BLOCKED_BY, _WASTE_BY_CAUSE, _WASTED_TURNS, _ctx, _waste_model

    check = next(c for c in qa.CHECKS if c.id == "failed-calls")
    strings = [("question", check.question), ("why", check.why)]
    fix = qa._failed_calls_fix()
    strings += [(f"fix {key}", text) for key, text in waste.CALL_FAILURE_FIX.items()]
    strings += [(f"explainer {heading}", text) for heading, text in fix["explainer"]]
    # The scope question every prompt ends with is the shared wording's, not this check's.
    assert fix["prompt"].endswith(qa.PROMPT_SCOPE)
    strings += [("fix title", fix["title"]), ("fix prompt", fix["prompt"].removesuffix(" " + qa.PROMPT_SCOPE))]
    found = _waste_model(recommendations=[_WASTED_TURNS], waste_by_cause=_WASTE_BY_CAUSE, waste_blocked_by=_BLOCKED_BY)
    quiet = _waste_model(waste_by_cause=[{"cause": "tool-error", "turns": 2, "cost_usd": 0.5, "lever": "Check paths first."}])
    none = _waste_model(waste_by_cause=[{"cause": "interrupt", "turns": 2, "cost_usd": 0.5, "lever": "Batch instructions."}])
    redirects = _waste_model(
        waste_by_cause=[{"cause": "redirected", "turns": 4, "cost_usd": 1.0, "lever": "Not waste."}],
        waste_blocked_by=[row for row in _BLOCKED_BY if row["kind"] == "saver"],
    )
    elsewhere = _waste_model(
        recommendations=[_WASTED_TURNS],
        waste_by_cause=[{"cause": "interrupt", "turns": 40, "cost_usd": 30.0, "lever": "Batch instructions."}],
    )
    for name, model in (
        ("found", found), ("quiet", quiet), ("none", none), ("redirects", redirects), ("elsewhere", elsewhere),
    ):
        result = qa.run("failed-calls", _ctx(tmp_path, model=model))
        strings.append((f"{name} summary", re.sub(r"\{\{page:[a-z/-]+\}\}", "the Savings page", result["summary"])))
    habits_result = qa.run("habits", _ctx(tmp_path, model=found))
    if habits_result["saving"]:
        strings.append(("habits saving", habits_result["saving"]))
    assert len(strings) >= 12
    for where, text in strings:
        _plain(text, where)


def test_the_copy_the_changes_page_adds_keeps_to_the_help_rules(tmp_path):
    """The mix sentence for each kind of session, the verdict for each
    reading on every measure's label (in each billing mode's figures), the
    labels of a change to the feedback prompts, and the two strings
    page-changes.js adds: the same rules as the help text."""
    from datetime import datetime, timedelta, timezone

    from claudeglass import change_points, impact
    from claudeglass import config as config_mod
    from claudeglass.change_points import ChangePoint
    from claudeglass.units import Units

    start = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)

    def session(days: float, mode: str, scheduled: bool) -> impact.SessionFacts:
        return impact.SessionFacts(
            start=start + timedelta(days=days), main=impact._Transcript(cost=1.0, turns=4), mode=mode, scheduled=scheduled
        )

    strings: list[tuple[str, str]] = []
    # The sentence for every kind the mix names, and for one it doesn't.
    for kind in (*impact._MIX_SUBJECT, "unheard-of"):
        scheduled = kind == "scheduled"
        before = [session(-d, "interactive" if scheduled or kind == "mixed" else "mixed", False) for d in (1, 2, 3)]
        after = [session(d, kind, scheduled) for d in (0.1, 0.2, 0.3)]
        mix = impact.session_mix(before, after)
        assert mix["flagged"] and mix["text"], kind
        strings.append((f"mix {kind}", mix["text"]))
    # Each reading's verdict, on the label of every measure a change can lead with.
    labels = set()
    for keys in (
        ["model"], ["effortLevel"], ["fastMode"], ["autoCompactWindow"], ["capture.level"], ["capture.coaching"],
        ["capture.feedback"], ["habit.drip_feed"], ["habit.split_large"], ["Explore: model"], ["spend_limit"],
    ):
        labels |= {m.label for m in impact.measures_for(ChangePoint(start, "apply", "x", keys=keys))}
    for units in (Units(), Units(billing_mode="subscription")):
        for label in sorted(labels):
            for reading, direction in (
                ("lower", "lower"), ("higher", "higher"), ("possibly_lower", "lower"), ("possibly_higher", "higher"),
                ("no_clear_change", "lower"), ("no_clear_change", "same"), ("too_little_data", "lower"),
            ):
                row = {
                    "key": "k", "label": label, "label_key": reading, "direction": direction, "p": 0.01,
                    "change_pct": -30.0, "demoted": False, "before_value": 12.34, "after_value": 8.64,
                    "before": impact._text("money", 12.34, units), "after": impact._text("money", 8.64, units),
                }
                strings.append((f"verdict {label} {reading}", impact._verdict([row], 3, 4, True)))
    strings.append(("verdict empty", impact._verdict([], 3, 4, True)))
    strings.append(("verdict few after", impact._verdict([], 3, 1, False)))
    strings.append(("verdict few before", impact._verdict([], 1, 3, False)))
    # What a change to the feedback prompts is called.
    for n, ids in enumerate((["feedback_skill"], ["feedback_skill", "feedback_note"], [])):
        config_mod.set_capture(tmp_path, feedback=ids, now=datetime(2026, 9, 1 + n, 9, tzinfo=timezone.utc))
    strings += [(f"change {p.label}", p.label) for p in change_points.change_points(tmp_path)]
    # The strings the card itself adds.
    text = (Path(__file__).resolve().parents[1] / "src" / "claudeglass" / "service" / "static" / "page-changes.js").read_text(
        encoding="utf-8"
    )
    for said in ("Session mix changed", "Read last: the mix of sessions changed."):
        assert f'"{said}"' in text, said
        strings.append(("card", said))
    assert len(strings) > 100
    for where, string in strings:
        _plain(string, where)


def test_the_copy_the_limit_stops_build_keeps_to_the_help_rules_and_never_says_percent_of_your_window(tmp_path):
    """The roll-up sentence, every note, the stop rows' spender text and the
    help on every share column: the same rules as the help text, and each
    share says it is a share of list-price spend, not a share of the window."""
    from claudeglass import limits
    from test_limits import _limit_line, _spend_reply, _stats_priced, _transcript, _when

    reset = _when(18, 14)
    main = _transcript(
        tmp_path, "main", [_spend_reply("m1", _when(18, 10, 30)), _limit_line("l1", _when(18, 12), reset)]
    )
    agents = [
        _transcript(
            tmp_path,
            f"a{i}",
            [_spend_reply(f"a{i}", _when(18, 10, 0)), _spend_reply(f"b{i}", _when(18, 11, 0))],
            kind="subagent" if i % 2 else "workflow-agent",
            agent_type="reviewer",
            session_id=f"s{i}",
        )
        for i in range(3)
    ]
    (tmp_path / "quiet").mkdir()
    quiet = _transcript(
        tmp_path / "quiet", "q", [_spend_reply("q1", _when(19, 11)), _limit_line("ql", _when(19, 12), _when(19, 14))]
    )
    strings: list[tuple[str, str]] = []
    for name, results in (("busy", (main, *agents)), ("quiet", (quiet,))):
        section = limits.build_section(_stats_priced(*results))
        helptext.annotate_section(section)
        for table in section.tables:
            if table.name not in ("limits_stops_rollup", "limits_stops", "limits_summary"):
                continue
            strings += [(f"{name} {table.name} note", note) for note in table.notes]
            if table.name == "limits_summary":
                continue
            keys = [column.key for column in table.columns]
            for row in table.rows:
                cells = dict(zip(keys, row))
                strings += [(f"{name} {table.name}.{k}", cells[k]) for k in ("top_spender", "second_spender") if cells.get(k)]
            for column in table.columns:
                if column.key.endswith("share_pct"):
                    assert "share of list-price spend; the limit may weigh models differently" in column.help, (
                        table.name,
                        column.key,
                    )
    assert any(where.endswith("limits_stops_rollup note") for where, _ in strings)
    assert any("3 or more agents worked at once" in text for _, text in strings)
    for where, text in strings:
        _plain(text, where)
    for where, text in _copy_strings():
        assert "% of your window" not in text, where


def test_the_mode_tables_name_overnight_and_one_shot_as_the_plan_words_them():
    """The overnight value reads "Overnight (unattended)" and a session with a
    single message "One-shot", in both tables that show a mode, and the
    sessions table's help says what each means."""
    by_mode = helptext.TABLE_COPY["sessions_by_mode"]
    compared = helptext.TABLE_COPY["baseline_comparison_by_mode"]
    for table in (by_mode, compared):
        assert table.value_labels["overnight"] == "Overnight (unattended)"
        assert table.value_labels["one-shot"] == "One-shot"
        assert table.value_labels["mixed"] == "Mixed"
    assert "One request" not in {*by_mode.value_labels.values(), *compared.value_labels.values()}
    shows = by_mode.help.shows
    assert "Overnight: Claude worked on its own for two hours or more at night while you were away." in shows
    assert "One-shot: one request (yours or a scheduled task's), then Claude worked with no more messages from you." in shows
    assert "ran into the night" not in shows


def test_the_mix_sentence_calls_a_one_message_session_one_shot():
    from claudeglass import impact

    assert impact._MIX_SUBJECT["one-shot"] == "One-shot sessions"


def test_the_copy_the_capture_page_and_status_add_keeps_to_the_help_rules():
    """The warning for each level and tagger, the status-line notes, the
    overhead line's fixed sentences, the tuning block and the empty
    status-line table: new strings the dashboard and `capture status`
    show follow the same rules as the help text."""
    from claudeglass import capture_catalogue, capture_view, context_budget, footprint
    from claudeglass.config import CaptureConfig

    strings: list[tuple[str, str]] = [("general warning", capture_view.WARNING)]
    for tagger in ("claude", "haiku"):
        for level in capture_catalogue.LEVELS:
            strings.append((f"{level} warning ({tagger})", capture_view.warning_text(capture_catalogue.level_metrics(level), tagger)))
        for metric_id in capture_catalogue.METRICS_BY_ID:
            strings.append((f"{metric_id} warning ({tagger})", capture_view.warning_text((metric_id,), tagger)))
    strings.append(("every feedback metric", capture_view.warning_text(capture_catalogue.DEEP_FEEDBACK_IDS)))
    strings.append(("deep with its feedback", capture_view.warning_text(capture_catalogue.level_includes("deep") + capture_catalogue.DEEP_FEEDBACK_IDS)))
    for metric_id, text in capture_view.DESKTOP_STATUSLINE_NOTES.items():
        strings.append((f"{metric_id} desktop note", text.format(total="204")))
    mix = {"claude-desktop": {"count": 204, "last_ts": ""}, "cli": {"count": 1, "last_ts": ""}}
    for metric_id in capture_view.STATUSLINE_NOTES:
        strings.append((f"{metric_id} status line", capture_view.STATUSLINE_NOTES[metric_id]))
        strings.append((f"{metric_id} mix", capture_view.statusline_note(metric_id, False, mix) or ""))
    strings.append(("overhead line", capture_view.overhead_text("Over your last 7 days", "", "about 0.40 USD", "nothing")))
    strings.append(("tuning title", capture_view.TUNING_TITLE))
    strings.append(("tuning text", capture_view.TUNING_TEXT))
    strings.append(("desktop empty note", context_budget.DESKTOP_EMPTY_NOTE))
    strings += [
        (f"capture on, {level}", footprint.expectations(CaptureConfig(level=level))[0][1])
        for level in ("free", "essentials", "standard", "deep")
    ]
    for where, text in strings:
        assert text, where
        _plain(text.replace("{{page:setup/capture}}", "Setup > Capture"), where)


def test_the_status_line_table_help_says_the_desktop_app_runs_no_status_line():
    help_ = helptext.TABLE_COPY["context_budget_statusline"].help
    assert "The desktop app doesn't run status lines" in help_.read
    assert "stays empty" not in help_.read or "install the status line logger" in help_.read


def test_the_copy_the_project_files_check_and_the_agent_stack_add_keeps_to_the_help_rules(tmp_path):
    """The check's question and why, what it says in each of its answers,
    the table it draws, the five prompts to copy and their explainers for a
    file read by agents and for one a CLAUDE.md pulls in, the starting-context
    stack's notes and column help, and the lines the tuning summary adds."""
    from claudeglass import claude_md_review, context_files, tuning
    from claudeglass import quick_actions as qa
    from claudeglass.model import Section
    from test_context_files import _startup
    from test_quick_actions import UNITS, _pf_data, _pf_hash, _pf_read, _pf_run, _pf_setup
    from test_tuning import _sample

    claude_md_review._NAMES.clear()
    ctx, project, salt = _pf_setup(tmp_path)
    context = project / "docs" / "context.md"
    check = next(c for c in qa.CHECKS if c.id == "project-files")
    strings = [("question", check.question), ("why", check.why)]

    found = _pf_run(ctx, _pf_data(_pf_read(_pf_hash(context, salt))))
    grown = _pf_run(
        ctx,
        _pf_data(_pf_read(_pf_hash(context, salt), tokens=3000, types=("Explore",), weekly={"2026-08-31": 2000, "2026-09-28": 3000})),
    )
    wide = _pf_run(ctx, _pf_data(_pf_read(_pf_hash(context, salt), weekly={"2026-09-28": 9000})))
    gone = _pf_run(ctx, _pf_data(_pf_read("0123456789abcdef")))
    quiet = _pf_run(ctx, _pf_data(_pf_read("0123456789abcdef", tokens=900, weekly={"2026-09-28": 900})))
    (project / "src").mkdir()
    (project / "src" / "app.py").write_text("print(1)\n", encoding="utf-8")
    code = _pf_run(ctx, _pf_data(_pf_read(_pf_hash(project / "src" / "app.py", salt))))
    none = _pf_run(ctx, {})
    assert (found["status"], gone["status"], quiet["status"], code["status"], none["status"]) == ("act", "ok", "ok", "ok", "no_data")
    for name, result in (
        ("found", found), ("grown", grown), ("wide", wide), ("gone", gone), ("quiet", quiet), ("code", code), ("none", none)
    ):
        strings.append((f"{name} summary", result["summary"]))
        if result["table"]:
            strings += [(f"{name} column", column["label"]) for column in result["table"]["columns"]]
            strings += [(f"{name} cell", cell) for row in result["table"]["rows"] for cell in row if isinstance(cell, str)]

    row = found["table"]["rows"][0]
    reads = {
        "name": "docs/context.md",
        "project": "repo",
        "tokens": 9000,
        "change_pct": 60.0,
        "cost_month_usd": 12.0,
        "reach": [{"reach": name, "runs": 9, "share": 0.9, "standing": True} for name in ("Explore", "Plan", "Review")],
    }
    for source in ("read", "import"):
        for fix in claude_md_review.project_file_fixes({**reads, "source": source}, UNITS):
            strings.append((f"{source} title", fix["title"]))
            strings += [(f"{source} {heading}", text) for heading, text in fix["explainer"]]
            assert fix["prompt"].endswith(claude_md_review._PROMPT_TAIL)
            strings.append((f"{source} prompt", fix["prompt"].removesuffix(" " + claude_md_review._PROMPT_TAIL)))
    strings.append(("tail", claude_md_review._PROMPT_TAIL.removesuffix(" " + claude_md_review.PROMPT_RESTART)))

    table = context_files.build_stack_table({"standing": {}}, _startup(("Explore", 8, 20000.0, 3000.0, 500.0)))
    strings += [("stack note", note) for note in table.notes]
    section = Section(key="agent_startup", title="Agent startup", tables=[table])
    helptext.annotate_section(section)
    strings += [(f"stack help {name}", text) for name, text in (("shows", table.help.shows), ("read", table.help.read), ("act", table.help.act))]
    strings += [(f"stack column {column.key}", column.help) for column in table.columns if column.help]

    strings += [("tuning summary", line) for line in tuning.summary_text(_sample()).splitlines() if "roject file" in line or line.startswith("Of those")]
    # What the Agents page's section says the check covers (page-agents.js).
    page = (SRC / "service" / "static" / "page-agents.js").read_text(encoding="utf-8")
    for said in (
        "The Overview check looks at text files only (.md and .txt), which you can split or trim.",
        "Code and data files are listed after them and never flagged.",
        "No changes to suggest: the Overview check covers text files (.md and .txt), and this file is code or data.",
    ):
        assert said in page, said
        strings.append(("page-agents", said))
    assert row[0] == "docs/context.md" and len(strings) > 60
    for where, text in strings:
        _plain(text, where)
    claude_md_review._NAMES.clear()

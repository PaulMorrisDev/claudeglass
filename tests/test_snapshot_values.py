"""A snapshot value that is a list (an agent's ``tools`` line) reads as its
entries joined with ``", "`` wherever a user or an export sees it: the
config-diff rows, the report's Config section, the CLI's ``config-diff``,
``compare``'s co-changed keys, "Your changes", and a card's "Now" value.
No output holds a Python list's ``['`` and ``']``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claudeglass import change_points, cli, fixes, recommend, report, snapshots
from claudeglass import compare as compare_mod
from claudeglass.config import Config
from claudeglass.corpus import load_corpus
from claudeglass.model import PricingMeta, Recommendation, ReportMeta, ReportModel, SettingChange
from claudeglass.pricing import load_pricing
from claudeglass.render import html, json_out, markdown

from helpers import turn_line, write_jsonl

BEFORE = ["Bash", "Glob", "Grep", "Read"]
AFTER = ["Bash", "Glob", "Grep", "Read", "Edit"]
LINE = "Bash, Glob, Grep, Read"
KEY = "agents.reviewer.tools"


def _no_repr(text: str) -> None:
    assert "['" not in text and "']" not in text, text


def _doc(ts: str, tools: list) -> dict:
    return {
        "ts": ts,
        "schema_version": 2,
        "project_slug": "slug:abc",
        "agents": {"reviewer": {"source": "user", "tools": tools}},
        "effective": {"allowedTools": tools},
        "effective_provenance": {"allowedTools": "user"},
    }


def _write(config_dir: Path, ts: str, tools: list) -> None:
    folder = config_dir / "snapshots"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{ts}.json").write_text(json.dumps(_doc(ts, tools)), encoding="utf-8")


def _pair(tmp_path: Path) -> tuple[Path, list[snapshots.Snapshot]]:
    config_dir = tmp_path / "claudeglass"
    _write(config_dir, "20260801T000000Z", BEFORE)
    _write(config_dir, "20260901T000000Z", AFTER)
    return config_dir, snapshots.load_snapshots(config_dir)


def _sessions() -> list[dict]:
    return [
        {"session_id": "a", "first_ts": "2026-08-10T00:00:00.000Z", "turns": 3, "cost": 1.0, "recache_cc": 0, "cc_total": 10, "compactions": 0, "span_s": 60},
        {"session_id": "b", "first_ts": "2026-09-10T00:00:00.000Z", "turns": 3, "cost": 2.0, "recache_cc": 0, "cc_total": 10, "compactions": 0, "span_s": 60},
    ]


def _cells(table) -> list[str]:
    return [cell for row in table.rows for cell in row if isinstance(cell, str)]


# -- the helper -------------------------------------------------------------


def test_a_list_of_strings_reads_as_its_entries_joined_with_a_comma_and_space():
    assert snapshots.config_value_text(BEFORE) == LINE
    assert snapshots.config_value_text(tuple(BEFORE)) == LINE
    assert snapshots.config_value_text(["Read"]) == "Read"


def test_scalars_and_unset_read_as_before():
    assert snapshots.config_value_text(None) == "(unset)"
    assert snapshots.config_value_text("high") == "high"
    assert snapshots.config_value_text(150000) == "150000"
    assert snapshots.config_value_text(True) == "True"


@pytest.mark.parametrize(
    "value",
    [
        [],
        {},
        [1, 2.5, None, True, "x"],
        [["a", "b"], ["c"]],
        {"a": ["x", "y"], "b": {"c": [1, None]}},
        [{"a": 1}, ("t", "u")],
        [object(), b"bytes", {1, 2}],
    ],
)
def test_the_helper_never_raises_and_never_prints_a_python_list(value):
    text = snapshots.config_value_text(value)
    assert isinstance(text, str) and text
    _no_repr(text)


def test_non_string_entries_and_nested_values_read_plainly():
    assert snapshots.config_value_text([1, 2.5, None, "x"]) == "1, 2.5, (unset), x"
    assert snapshots.config_value_text([["a", "b"], ["c"]]) == "[a, b], [c]"
    assert snapshots.config_value_text({"a": ["x", "y"], "b": 2}) == "a: [x, y]; b: 2"
    assert snapshots.config_value_text([]) == "(empty list)"
    assert snapshots.config_value_text({}) == "(none)"


# -- the config-difference rows ---------------------------------------------


def test_the_config_diff_rows_read_a_tools_line_as_a_joined_line(tmp_path):
    _config_dir, snaps = _pair(tmp_path)
    table = snapshots.build_config_diff_table(_sessions(), snaps, KEY)
    assert {row[0] for row in table.rows} == {LINE, LINE + ", Edit"}
    for cell in _cells(table):
        _no_repr(cell)
    # A value that is a number stays the number the rows were grouped by.
    other = snapshots.build_config_diff_table(
        _sessions(),
        [snapshots.Snapshot(path=None, ts="20260801T000000Z", data={"user_settings": {"autoCompactWindow": 5}})],
        "user_settings.autoCompactWindow",
    )
    assert other.rows[0][0] == 5


def test_the_config_section_and_the_reports_config_section_read_it_the_same_way(tmp_path):
    _config_dir, snaps = _pair(tmp_path)
    section = snapshots.build_config_section(
        _sessions(), snaps, KEY, include_effective=True, sessions_with_observed=None
    )
    canonical = snapshots.canonical_project_keys(set())
    built = report._build_config_section(_sessions(), snaps, snaps, canonical)
    for part in (section, built):
        cells = [cell for table in part.tables for cell in _cells(table)]
        assert LINE in cells and LINE + ", Edit" in cells
        for cell in cells:
            _no_repr(cell)
    effective = next(t for t in built.tables if t.name == "effective-config")
    assert [row[2] for row in effective.rows] == [LINE + ", Edit"]


def test_every_renderer_shows_the_config_diff_rows_without_a_python_list(tmp_path):
    _config_dir, snaps = _pair(tmp_path)
    canonical = snapshots.canonical_project_keys(set())
    section = report._build_config_section(_sessions(), snaps, snaps, canonical)
    model = ReportModel(meta=ReportMeta(pricing=PricingMeta(coverage_pct=100.0)), sections=[section])
    markdown_text = markdown.render_markdown(model)
    assert LINE in markdown_text
    for text in (markdown_text, html.render_html(model), json_out.render_json(model)):
        _no_repr(text)


def test_the_cli_config_diff_reads_a_tools_line_as_a_joined_line(tmp_path, capsys):
    root = tmp_path / "projects"
    project = root / "proj-diff"
    project.mkdir(parents=True)
    write_jsonl(project / "session-1.jsonl", [turn_line(timestamp="2026-08-10T00:00:00.000Z")])
    config_dir = tmp_path / "home" / ".claude" / "claudeglass"
    _write(config_dir, "20200101T000000Z", BEFORE)
    _write(config_dir, "20200201T000000Z", AFTER)
    for doc in (config_dir / "snapshots").glob("*.json"):
        data = json.loads(doc.read_text(encoding="utf-8"))
        del data["project_slug"]
        doc.write_text(json.dumps(data), encoding="utf-8")
    code = cli.main(
        [
            "config-diff",
            "--projects-root", str(root),
            "--project", "proj-diff",
            "--config-dir", str(config_dir),
            "--key", KEY,
        ]
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert LINE in out
    _no_repr(out)


# -- compare ----------------------------------------------------------------


def test_compare_co_changed_keys_read_a_tools_line_as_a_joined_line(tmp_path):
    root = tmp_path / "projects"
    project = root / "proj"
    project.mkdir(parents=True)
    write_jsonl(project / "before.jsonl", [turn_line(timestamp="2026-08-10T10:00:00.000Z")])
    write_jsonl(project / "after.jsonl", [turn_line(timestamp="2026-09-10T10:00:00.000Z")])
    corpus = load_corpus([project])
    snaps = [
        snapshots.Snapshot(path=Path("a.json"), ts="20260801T000000Z", data={"user_settings": {"model": "sonnet"}, "agents": {"reviewer": {"tools": BEFORE}}}),
        snapshots.Snapshot(path=Path("b.json"), ts="20260901T000000Z", data={"user_settings": {"model": "fable"}, "agents": {"reviewer": {"tools": AFTER}}}),
    ]
    section = compare_mod.compare(
        corpus,
        load_pricing(),
        Config(),
        arm_a=compare_mod.parse_arm_spec("key:user_settings.model=sonnet"),
        arm_b=compare_mod.parse_arm_spec("key:user_settings.model=fable"),
        min_sessions=1,
        snapshots=snaps,
    )
    table = next(t for t in section.tables if t.name == "compare_co_changed")
    row = next(r for r in table.rows if r[0] == KEY)
    assert row[1:] == [LINE, LINE + ", Edit"]
    for cell in _cells(table):
        _no_repr(cell)


# -- "Your changes" -----------------------------------------------------------


def test_your_changes_reads_a_tools_line_change_without_a_python_list(tmp_path):
    config_dir, _snaps = _pair(tmp_path)
    [point] = change_points.change_points(config_dir)
    assert KEY in point.keys or "effective.allowedTools" in point.keys
    texts = [change_points.summary(point), point.to_dict()["summary"], ", ".join(point.keys)]
    for change in point.changes:
        texts += [str(change.get("old")), str(change.get("new"))]
    for text in texts:
        _no_repr(text)


def test_the_words_of_a_change_join_a_list_and_survive_odd_entries():
    assert change_points._words(BEFORE) == LINE
    assert change_points._words([1, None, ["a", "b"]]) == "1, (unset), [a, b]"
    _no_repr(change_points._words([["a"], {"b": ["c"]}]))


# -- a card's "Now" value ---------------------------------------------------


def _tools_rec(current) -> Recommendation:
    rec = Recommendation(
        id="spawn-tools-list",
        severity="advice",
        category="settings",
        archetypes=(),
        title="reviewer is given tools it rarely calls",
        action="Limit reviewer's tools.",
        lever=None,
        scope="user",
        agent_type="reviewer",
        changes=[SettingChange(target="agent", key="tools", agent="reviewer", value=["Read", "Grep"], current=current)],
    )
    fixes.attach_fixes([rec])
    return rec


def test_a_cards_now_value_and_the_patch_set_read_a_tools_line_without_a_python_list():
    rec = _tools_rec([*BEFORE, ["nested"], 3])
    prompt = rec.fixes[0]["prompt"]
    _no_repr(prompt)
    patch = recommend.render_patch_set([rec])
    assert f"[{LINE}, [nested], 3]" in patch
    explainer = " ".join(text for _label, text in fixes.explainer_for(rec, rec.changes[0]))
    assert "Now: Bash, Glob, Grep, Read, [nested], 3." in explainer
    _no_repr(explainer)

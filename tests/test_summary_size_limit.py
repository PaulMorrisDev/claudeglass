"""The dashboard's summary-size advice stays within the same limit the
advice does. ``compaction_sim_by_window`` carries a ``within_limit`` column
(``compaction_sim.build_section``): true when a candidate window summarises
no more often than ``max_compactions_per_session``. The Overview's saving
lever (``savingsLevers``), the summary-point chart's "cheapest" size
(``cheapestAllowed``) and the glossary's "cheapest window" sentence read
it, so none of them offers a window the compaction-window rule and the
what-if refuse.

The functions run in Node, over a table shaped like a real report's (the
audit's report30: 250,000 summarises 3.94 times a session and saves $90.92,
300,000 does 2.72, 400,000 does 1.8 and costs more).
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from test_service_static import _balanced_end, _function_source, _static_text

COLUMNS = ["window", "compactions_per_session", "mean_ctx", "cost", "delta_usd", "delta_pct", "within_limit"]


def _table(rows: list[list], *, with_limit: bool = True) -> dict:
    columns = COLUMNS if with_limit else COLUMNS[:-1]
    return {
        "name": "compaction_sim_by_window",
        "columns": [{"key": key} for key in columns],
        "rows": [row if with_limit else row[:-1] for row in rows],
    }


#: The audit's table: the two cheapest windows summarise too often to offer.
REPORT30 = [
    ["none", 1.1, 180_000, 700.0, 0.0, 0.0, True],
    ["250,000", 3.94, 120_000, 609.08, -90.92, -13.0, False],
    ["300,000", 2.72, 150_000, 640.0, -60.0, -8.6, False],
    ["400,000", 1.8, 200_000, 720.0, 20.0, 2.9, True],
]


def _node(expression: str) -> object:
    """Run ``savingsLevers``'s and ``cheapestAllowed``'s source in Node,
    with the helpers they read, and return ``expression``'s value as JSON."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node isn't installed")
    charts = _static_text("charts-types.js")
    preamble = "function thousands(n) { return String(n); }\n"
    script = preamble + "\n".join(
        _function_source(charts, name) for name in ("num", "tableObjects", "withinLimit", "cheapestAllowed", "savingsLevers")
    )
    script += f"\nprocess.stdout.write(JSON.stringify({expression}));"
    done = subprocess.run(
        [node, "-e", script], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180, check=True
    )
    return json.loads(done.stdout)


def _lever(rows: list[list], **kwargs) -> dict | None:
    levers = _node(f"savingsLevers({{ compaction_sim_by_window: {json.dumps(_table(rows, **kwargs))} }})")
    return next((lever for lever in levers if lever["key"] == "compaction_sim"), None)


def test_the_lever_does_not_offer_a_window_that_summarises_too_often() -> None:
    """The cheapest raw window (250,000 at 3.94 a session) is beyond the
    limit, and so is 300,000; 400,000 costs more than now. Nothing is left
    to offer, so there is no lever."""
    assert _lever(REPORT30) is None


def test_the_lever_offers_the_cheapest_window_within_the_limit() -> None:
    rows = [*REPORT30, ["500,000", 1.0, 250_000, 650.0, -50.0, -7.1, True], ["600,000", 0.8, 280_000, 670.0, -30.0, -4.3, True]]
    lever = _lever(rows)
    assert lever["row"] == "500,000"
    assert lever["usd"] == pytest.approx(50.0)
    assert lever["detail"] == "Summarising at 500,000 tokens"


def test_a_table_without_the_column_offers_every_window() -> None:
    """A report from before the column existed keeps its old behaviour."""
    assert _lever(REPORT30, with_limit=False)["row"] == "250,000"


@pytest.mark.parametrize("marker", ["False", "false", False])
def test_the_limit_reads_a_rendered_false_as_well_as_a_json_one(marker) -> None:
    rows = [["none", 1.0, 1, 100.0, 0.0, 0.0, True], ["250,000", 4.0, 1, 50.0, -50.0, -50.0, marker]]
    assert _lever(rows) is None


def _sizes(rows: list[list]) -> str:
    """The chart's own per-size objects, built the way ``line`` builds them."""
    sizes = [
        {"tokens": int(row[0].replace(",", "")), "cost": row[3], "allowed": row[6] is not False}
        for row in rows
        if row[0] != "none"
    ]
    return json.dumps(sizes)


def test_the_summary_chart_names_only_a_size_within_the_limit() -> None:
    """With 250,000 and 300,000 beyond the limit and 400,000 dearer than
    now, the chart has no size to name; add a cheaper size within the limit
    and it names that one, not the cheapest raw size."""
    assert _node(f"cheapestAllowed({_sizes(REPORT30)})") == {"tokens": 400_000, "cost": 720.0, "allowed": True}

    rows = [*REPORT30, ["500,000", 1.0, 250_000, 650.0, -50.0, -7.1, True]]
    assert _node(f"cheapestAllowed({_sizes(rows)})")["tokens"] == 500_000
    assert _node("cheapestAllowed([])") is None
    assert _node('cheapestAllowed([{ tokens: 1, cost: 1, allowed: false }])') is None


def test_the_summary_chart_says_nothing_cheaper_when_the_only_cheaper_sizes_are_refused() -> None:
    """`line` falls back to the "cheapest" wording unless the named size
    costs less than now, and survives there being no size to name."""
    chart = _function_source(_static_text("charts-types.js"), "line")
    assert "var cheapest = cheapestAllowed(sizes);" in chart
    assert 'var variant = cheapest && cheapest.cost < nowCost ? null : "cheapest";' in chart
    assert "best: cheapest ? thousands(cheapest.tokens) : \"\"," in chart
    assert "allowed: withinLimit(row)" in chart
    specs = _static_text("charts.js")
    assert "No size within the summaries-per-session limit would cost less than now." in specs


def _glossary_summaries_fact(rows: list[list], **kwargs) -> str:
    """What the glossary's "conversation-summaries" figure says over this
    table: the function's own source, run in Node with the table helpers it
    reads and a plain money formatter."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node isn't installed")
    glossary = _static_text("page-glossary.js")
    marker = '"conversation-summaries": function (ctx) {'
    start = glossary.index(marker)
    end = _balanced_end(glossary, glossary.index("{", start + len(marker) - 1))
    fact = "var fact = " + glossary[start + len('"conversation-summaries": '):end] + ";"
    script = "\n".join(
        [
            "function findTable(report) { return report.table; }",
            "function moneyText(value) { return '$' + value.toFixed(2); }",
            _function_source(glossary, "colIndex"),
            fact,
            f"process.stdout.write(JSON.stringify(fact({{ report: {{ table: {json.dumps(_table(rows, **kwargs))} }} }})));",
        ]
    )
    done = subprocess.run(
        [node, "-e", script], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180, check=True
    )
    return json.loads(done.stdout)


def test_the_glossary_does_not_name_a_window_beyond_the_limit() -> None:
    """The cheapest raw window (250,000, at 3.94 summaries a session) is one
    the advice refuses, so the glossary must not call it the cheapest this
    data suggests. Nothing within the limit is cheaper than now."""
    text = _glossary_summaries_fact(REPORT30)
    assert "250,000" not in text
    assert "already looks like the cheapest" in text


def test_the_glossary_names_the_cheapest_window_within_the_limit() -> None:
    rows = [*REPORT30, ["500,000", 1.0, 250_000, 650.0, -50.0, -7.1, True]]
    text = _glossary_summaries_fact(rows)
    assert "The cheapest window this data suggests is 500,000 tokens" in text
    assert "250,000" not in text


def test_the_glossary_offers_every_window_when_the_table_has_no_limit_column() -> None:
    text = _glossary_summaries_fact(REPORT30, with_limit=False)
    assert "The cheapest window this data suggests is 250,000 tokens" in text

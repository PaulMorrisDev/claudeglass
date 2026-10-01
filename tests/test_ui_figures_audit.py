"""The Overview's figures agree with the pages behind them (``service/static``).

What the design audit found, and what now holds, read from the source:

- the Overview counts, ranks, titles, links and prices recommendations
  as Actions lists them (``groupRecommendations``, exported once from
  page-actions.js), and so does the sidebar's "Do this" count;
- the daily spend chart counts replies by the day they were sent while
  the Spend figure counts whole sessions: the chart's reading gives both
  and says why they differ, on the Overview and on Spend › Usage;
- the chart spans the window's days, not only the days with spend, and
  "so far" sits in the margin, where no bar or change label reaches;
- the sessions scatter says how many sessions it leaves out, and why;
- the tallest pages fold tables no evidence link, chart or action
  points at under More tables;
- every price is found by the rate card's own id, whatever id a session
  recorded (costs.js's modelIdFor, in pricing.py's order), so the
  Overview's cache note and the Cache page name the same model's price.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from claudeglass import helptext
from claudeglass.pricing import load_pricing
from test_service_static import (
    _app_js,
    _chart_specs,
    _evidence_sources,
    _function_source,
    _static_text,
)


def _body(module: str, name: str) -> str:
    return _function_source(_static_text(module), name)


# -- P1-3: recommendations counted as Actions lists them ----------------------


def test_the_grouping_is_written_once_and_exported() -> None:
    actions = _static_text("page-actions.js")
    for name in ("groupRecommendations", "groupTitle", "groupSavingUsd", "listSaving"):
        assert "export function " + name + "(" in actions, name
    assert len(re.findall(r"function\s+groupRecommendations\s*\(", _app_js())) == 1
    overview = _static_text("page-overview.js")
    assert 'import { groupRecommendations, groupSavingUsd, groupTitle, listSaving } from "./page-actions.js";' in overview
    # No ranking of its own: the Overview's order is Actions' order.
    for gone in ("function rankActions", "function severityRank", "SEVERITY_ORDER"):
        assert gone not in overview, gone


def test_the_overview_counts_and_links_the_groups_actions_shows() -> None:
    render = _body("page-overview.js", "renderOverview")
    assert "var groups = groupRecommendations(recs);" in render
    assert "facts.saving = availableSaving(levers, groups, reportTables);" in render
    assert "facts.worth = rows" in render
    # "Anything wrong?": every check, with the Actions items its rules
    # raised on the same row; an item no check draws on is a row too.
    assert "checklistRows((checksBody.data && checksBody.data.checks) || [], groups)" in render
    # The sentence counts the checklist's rows, so the two agree.
    assert 'return row.state === "fix" || row.state === "look";' in render
    rows = _body("page-overview.js", "checklistRows")
    assert "return ids.indexOf(group.id) !== -1;" in rows
    assert "if (!claimed[group.key]) rows.push(" in rows
    row = _body("page-overview.js", "checklistRow")
    assert "listSaving(lead)" in row
    # A group for several agent types opens on Actions, where each has its prompt.
    assert '"See the " + lead.members.length + " prompts"' in row


def test_each_copy_prompt_button_is_named_for_its_action() -> None:
    """Five "Copy prompt" buttons read as five different things: each is
    named "Copy prompt for <action>", the visible words first (WCAG
    2.5.3), as Actions names its own."""
    copy = _body("page-overview.js", "copyPromptButton")
    assert 'button("Copy prompt", {' in copy
    assert 'label: "Copy prompt" + (about ? " for " + about : "")' in copy
    row = _body("page-overview.js", "checklistRow")
    assert row.count("copyPromptButton(") == 1 and "copyPromptButton(fix.prompt, groupTitle(lead))" in row


def test_the_sidebar_count_is_the_inbox_count() -> None:
    badge = _body("app.js", "refreshActionsBadge")
    assert "groupRecommendations(recs).filter(" in badge
    assert 'group.severity === "action"' in badge
    assert 'import { groupRecommendations, renderQuickActions, renderRecommendations } from "./page-actions.js";' in _static_text("app.js")


def test_available_saving_counts_each_group_once() -> None:
    available = _body("page-overview.js", "availableSaving")
    assert "groups.forEach(" in available
    assert "if (LEVER_RULES[group.id]) return;" in available
    assert "var usd = groupSavingUsd(group);" in available
    assert "rec.saving_usd" not in available
    # The ways to save overlap, so they are combined, never added up.
    assert "return combinedSaving(items, spend);" in available
    saving = _body("page-actions.js", "groupSavingUsd")
    assert "if (seen[key] ||" in saving
    assert "!(rec.saving_usd > 0)" in saving


# -- P1-2: the chart's total and the Spend figure -----------------------------


def test_daily_spend_says_why_its_total_differs_from_spend() -> None:
    spec = _chart_specs()["daily-spend"]
    assert spec["summary"].startswith("Replies sent {span} cost {total}.")
    assert set(spec["alt"]) == {"sessions", "firstDay", "oneDay", "oneDaySessions", "oneDayFirstDay"}
    for key, text in spec["alt"].items():
        assert text.startswith("Replies sent {span} cost {total}.")
        assert ("{sessionsTotal}" in text) == (key != "oneDay")
    assert "earlier replies" in spec["alt"]["sessions"] and "earlier replies" in spec["alt"]["oneDaySessions"]
    assert "counts all of the first day" in spec["alt"]["firstDay"]
    # One day has no busiest day and no first day of several.
    for key in ("oneDay", "oneDaySessions", "oneDayFirstDay"):
        assert "busiest" not in spec["alt"][key] and "first day" not in spec["alt"][key]
    assert "not only this window" in spec["alt"]["oneDayFirstDay"]
    for text in [spec["summary"], *spec["alt"].values()]:
        assert "$" not in text and "USD" not in text
    columns = _body("charts-types.js", "stackedColumns")
    # Only when the two read differently, in the billing mode's own words.
    assert "moneyText(sessionsUsd) !== moneyText(grand) ? moneyText(sessionsUsd) : null" in columns
    assert 'var variant = sessionsTotal ? (sessionsUsd > grand ? "sessions" : "firstDay") : null;' in columns
    assert 'if (days.length === 1) variant = { sessions: "oneDaySessions", firstDay: "oneDayFirstDay" }[variant] || "oneDay";' in columns
    assert "sessionsTotal: sessionsTotal," in columns
    assert "span: spanText(days)," in columns
    tiles = _body("page-overview.js", "renderTiles")
    assert '"Every session with a reply in this window, earlier replies included."' in tiles
    # The change window counts the sessions started since (api._window_query).
    assert 'state.window === "change"' in tiles
    assert '"Every session started since your last change, so all of it ran on the new settings."' in tiles


def test_both_daily_spend_charts_get_the_sessions_and_the_window() -> None:
    overview = _body("page-overview.js", "renderOverview")
    usage = _body("page-spend.js", "renderUsage")
    assert 'fetchJson(withWindow("/api/summary"))' in usage
    assert "Promise.all([loads[wanted], impactLoad, summaryLoad])" in usage
    assert "Promise.all([dailyLoad, impactLoad, reportLoad, figuresDone, summaryLoad])" in overview
    for source, body in (("renderOverview", overview), ("renderUsage", usage)):
        assert re.search(r"sessionsTotal: \w+ && \w+\.ok === true \? \w+\.data\.total_cost : null", body), source
        assert "windowDays(state.window)" in body, source


# -- P2-13: the axis and "so far" ---------------------------------------------


def test_daily_spend_spans_the_window_it_counts() -> None:
    days = _body("charts-types.js", "windowDays")
    assert "export function windowDays(windowValue, now)" in _static_text("charts-types.js")
    assert '{ "1h": 1, "24h": 24 }[windowValue]' in days
    assert 'windowValue === "today"' in days and "setHours(0, 0, 0, 0)" in days
    assert "return {};" in days
    assert "return { first: utcDay(start), last: utcDay(now) };" in days
    columns = _body("charts-types.js", "stackedColumns")
    # Widened to the window; a day with spend outside it still shows.
    assert "data.first < seen[0] ? data.first : seen[0]" in columns
    assert "data.last > seen[seen.length - 1] ? data.last : seen[seen.length - 1]" in columns
    assert "var days = dayRange(first, last);" in columns


def test_so_far_sits_in_the_margin_over_the_change_labels() -> None:
    columns = _body("charts-types.js", "stackedColumns")
    top = re.search(r"\{ top: changes\.length \? (\d+) : (\d+) \}", columns)
    so_far = re.search(r"var soFarY = changes\.length \? (-\d+) : (-\d+);", columns)
    label_y = re.search(r'\.attr\("class", "chart-rule-label"\)\s*\.attr\("x", cx \+ 4\)\s*\.attr\("y", (-\d+)\)', columns)
    assert top and so_far and label_y
    # Above the plot, above a change label's line, inside the top margin.
    assert -int(top.group(1)) < int(so_far.group(1)) < int(label_y.group(1)) - 12
    assert -int(top.group(2)) < int(so_far.group(2)) < 0
    assert '.attr("y", soFarY)' in columns
    assert "y(d.total) - 6" not in columns
    assert "inner.w + ctx.margin.right - 20" in columns


def test_change_labels_keep_clear_of_each_other_and_the_edge() -> None:
    columns = _body("charts-types.js", "stackedColumns")
    assert "placeRuleLabel(label, change.label, cx, ruleXs, labelSpans, labelBounds);" in columns
    assert "return a.cx - b.cx;" in columns
    # A day's changes share one rule: their labels printed over each other.
    assert 'group.label = single ? group.changes[0].label : group.changes.length + " changes";' in columns
    assert "var ruleXs = ruleDays.map(" in columns
    place = _body("charts-types.js", "placeRuleLabel")
    assert "getComputedTextLength" in place
    assert '.attr("text-anchor", right ? "start" : "end")' in place
    assert "spans.push(" in place
    assert "fitLabel(full," in place


# -- P2-14: what the scatter leaves out ---------------------------------------


def test_the_scatter_says_how_many_sessions_it_leaves_out() -> None:
    spec = _chart_specs()["session-outliers"]
    assert spec["alt"]["unplotted"].startswith("{count} of {total} sessions. {unplotted}.")
    scatter = _body("charts-types.js", "scatter")
    assert "var unplotted = unplottedText(sessions);" in scatter
    assert 'variant: unplotted ? "unplotted" : null' in scatter
    assert "total: thousands(sessions.length)," in scatter
    unplotted = _body("charts-types.js", "unplottedText")
    for words in ('" cost nothing and"', "\" aren't plotted\"", "\" isn't plotted\"", '" no start time'):
        assert words in unplotted, words
    assert "if (!left) return null;" in unplotted


# -- P2-16: the tallest pages fold their secondary tables ---------------------

#: Folded under More tables: the daily table the chart above it already
#: shows (Spend › Usage), the skills rollup (Subagents) and the working
#: patterns (Quality).
_FOLDED = ("by_day", "topology_skills_rollup", "workstyle_archetypes")


def test_long_pages_fold_tables_nothing_points_at() -> None:
    cited = {table for _, table in _evidence_sources()}
    for name in _FOLDED:
        assert helptext.PLACEMENT[name] == "advanced", name
        assert name not in cited, f"{name} is evidence: it stays in view"
    charted = set()
    for spec in _chart_specs().values():
        sources = spec["source"] if isinstance(spec["source"], list) else [spec["source"]]
        charted.update(source.split(".")[-1] for source in sources)
    assert not charted & set(_FOLDED)
    # The list of every conversation summary folds under its count.
    raw = _body("page-spend.js", "renderCompactionsRaw")
    assert 'host = el("details", { class: "advanced-detail" });' in raw
    assert '" summary" : " summaries"' in raw


# -- the ways to save, together -----------------------------------------------


def _node(functions: list[tuple[str, str]], expression: str) -> object:
    """Run dashboard functions in Node: each (module, name) function's
    source, then ``expression``, whose value comes back through JSON."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node isn't installed")
    script = "\n".join(_body(module, name) for module, name in functions)
    script += f"\nprocess.stdout.write(JSON.stringify({expression}));"
    # A cold Node on a busy Windows runner has taken over 30 s to start.
    done = subprocess.run(
        [node, "-e", script], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180, check=True
    )
    return json.loads(done.stdout)


def _combined(items: list[dict], spend: dict[str, float]) -> float:
    """``combinedSaving`` from page-overview.js, run in Node."""
    expression = f"combinedSaving({json.dumps(items)}, {json.dumps(spend)})"
    return float(_node([("page-overview.js", "combinedSaving")], expression))


def test_overlapping_savings_multiply_instead_of_adding_up() -> None:
    """Two ways to save half of the same $100 save $75 together, not $100:
    the second halves what the first leaves."""
    items = [{"usd": 50, "agent": None}, {"usd": 50, "agent": None}]
    assert abs(_combined(items, {"top-level": 100}) - 75) < 1e-9


def test_a_saving_for_one_agent_type_comes_from_its_own_spend() -> None:
    """Half the main session's $90, then a tenth of everything: the main
    session keeps 90 x 0.5 x 0.9 and the subagent 10 x 0.9."""
    items = [{"usd": 45, "agent": "top-level"}, {"usd": 10, "agent": None}]
    total = _combined(items, {"top-level": 90, "Explore": 10})
    assert abs(total - (90 * (1 - 0.5 * 0.9) + 10 * (1 - 0.9))) < 1e-9


def test_the_ways_to_save_never_come_to_more_than_the_spend() -> None:
    items = [{"usd": 80, "agent": None}, {"usd": 80, "agent": "top-level"}, {"usd": 500, "agent": None}]
    assert _combined(items, {"top-level": 100}) <= 100


def test_without_a_spend_by_agent_type_the_savings_are_added_up() -> None:
    items = [{"usd": 3, "agent": None}, {"usd": 4, "agent": "top-level"}]
    assert _combined(items, {}) == 7


# -- every model id finds its prices ------------------------------------------

#: A report's meta as report.py writes it: rates keyed by the rate
#: card's own ids, and model_ids for the aliases and the ids it prices
#: as another model.
_META = {
    "rates": {
        "claude-opus-4": {"input": 15.0, "cache_read_ratio": 0.1},
        "claude-opus-4-1": {"input": 15.0, "cache_read_ratio": 0.11},
        "claude-sonnet-5": {"input": 2.0, "cache_read_ratio": 0.12},
        "claude-sonnet-5-5": {"input": 2.0, "cache_read_ratio": 0.13},
        "claude-haiku-4-5-20251001": {"input": 1.0, "cache_read_ratio": 0.14},
    },
    "model_ids": {
        "sonnet": "claude-sonnet-5-5",
        "haiku": "claude-haiku-4-5-20251001",
        "claude-haiku-4-5": "claude-haiku-4-5-20251001",
        "retired": "claude-sonnet-3",
    },
}

_ID_FUNCTIONS = [("costs.js", "modelIdFor"), ("costs.js", "rateFor")]


def _model_ids(meta: dict | None, ids: list) -> list:
    """``modelIdFor`` from costs.js over each id, run in Node."""
    return _node(
        _ID_FUNCTIONS,
        f"{json.dumps(ids)}.map(function (id) {{ return modelIdFor({json.dumps(meta)}, id); }})",
    )


def test_a_recorded_id_finds_the_rate_cards_own_id() -> None:
    found = {
        # The id itself.
        "claude-sonnet-5-5": "claude-sonnet-5-5",
        # An alias, through meta.model_ids.
        "sonnet": "claude-sonnet-5-5",
        "claude-haiku-4-5": "claude-haiku-4-5-20251001",
        # With "[1m]" taken off, then as before.
        "claude-sonnet-5-5[1m]": "claude-sonnet-5-5",
        "haiku[1m]": "claude-haiku-4-5-20251001",
        # A dated id, by the longest priced id it starts with.
        "claude-sonnet-5-20261001": "claude-sonnet-5",
        "claude-sonnet-5-5-20261001": "claude-sonnet-5-5",
        # Only up to a "-" or "@": claude-opus-4-10 isn't claude-opus-4-1.
        "claude-opus-4-10": "claude-opus-4",
        # Bedrock and Vertex wrapping.
        "us.anthropic.claude-opus-4-1-20250805-v1:0": "claude-opus-4-1",
        "claude-opus-4-1@20250805": "claude-opus-4-1",
        "us.anthropic.claude-opus-4-1-v1:0[1m]": "claude-opus-4-1",
    }
    assert _model_ids(_META, list(found)) == list(found.values())


def test_an_id_the_rate_card_doesnt_price_finds_nothing() -> None:
    unknown = ["gpt-5", "claude-sonnet-55", "<unknown>", "<synthetic>", "", None, "constructor", "retired"]
    assert _model_ids(_META, unknown) == [None] * len(unknown)
    # A report built without model_ids (cli.py's own reports) still
    # finds every id the rate card prices by its own name.
    bare = {"rates": _META["rates"]}
    assert _model_ids(bare, ["sonnet", "claude-sonnet-5-5-20261001"]) == [None, "claude-sonnet-5-5"]
    assert _model_ids(None, ["claude-sonnet-5-5"]) == [None]


def test_rate_for_gives_the_prices_the_id_finds() -> None:
    meta = json.dumps(_META)
    rates = _node(
        _ID_FUNCTIONS,
        f'["sonnet", "claude-opus-4-1@20250805", "gpt-5"].map(function (id) {{ return rateFor({meta}, id); }})',
    )
    assert rates == [_META["rates"]["claude-sonnet-5-5"], _META["rates"]["claude-opus-4-1"], None]
    assert _node(_ID_FUNCTIONS, 'rateFor(null, "claude-sonnet-5-5")') is None


def test_the_cost_sentences_name_each_model_used_once_by_its_own_id() -> None:
    """pricingFacts kept only the by_model ids meta.rates had as keys, so
    a newer release priced as an older one (or a dated or cloud id) fell
    out and "the model you spent most on" named another."""
    rows = [["claude-sonnet-5-5-20261001"], ["claude-sonnet-5-5"], ["<unknown>"], ["claude-haiku-4-5"]]
    report = {"meta": _META, "sections": [{"key": "overview", "tables": [{"name": "by_model", "rows": rows}]}]}
    facts = _node(
        [("api.js", "findSection"), *_ID_FUNCTIONS, ("costs.js", "pricingFacts")],
        f"pricingFacts({json.dumps(report)})",
    )
    assert facts["used"] == ["claude-sonnet-5-5", "claude-haiku-4-5-20251001"]
    assert facts["mainId"] == "claude-sonnet-5-5"
    assert facts["main"] == _META["rates"]["claude-sonnet-5-5"]


def test_the_overview_cache_note_adds_up_each_models_reads() -> None:
    """The daily rows carry the recorded id: two ids of one model add up
    before the model that read most is picked, and its price is found."""
    functions = [*_ID_FUNCTIONS, ("page-overview.js", "cacheReadRatio")]
    rows = [
        {"model": "claude-opus-4-1-20250805", "cache_read_tokens": 6},
        {"model": "us.anthropic.claude-opus-4-1-20250805-v1:0", "cache_read_tokens": 6},
        {"model": "claude-sonnet-5-5", "cache_read_tokens": 10},
    ]
    assert _node(functions, f"cacheReadRatio({json.dumps(_META)}, {json.dumps(rows)})") == 0.11
    # The model that read most has no price: no ratio, as before.
    rows = [{"model": "gpt-5", "cache_read_tokens": 100}, {"model": "claude-sonnet-5-5", "cache_read_tokens": 1}]
    assert _node(functions, f"cacheReadRatio({json.dumps(_META)}, {json.dumps(rows)})") is None


def test_the_dashboard_finds_the_model_pricing_py_prices() -> None:
    """modelIdFor and pricing.py's resolve_model agree on every id the
    packaged rate card names, its "[1m]" forms, and recorded ids that
    only the cleaning and the prefix step can place. model_ids holds the
    aliases alone here, so the fallback does all the rest."""
    pricing = load_pricing()
    names = sorted(pricing.models) + sorted(pricing.aliases)
    ids = names + [name + "[1m]" for name in names] + [
        "claude-sonnet-5-20261001",
        "claude-sonnet-5-5",
        "claude-sonnet-5-5-20261001",
        "claude-sonnet-55",
        "claude-opus-4-10",
        "claude-opus-4-1-20250805",
        "claude-haiku-4-5-20250901",
        "us.anthropic.claude-opus-4-1-20250805-v1:0",
        "eu.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "anthropic.claude-haiku-4-5-20251001-v1:0",
        "us.anthropic.sonnet",
        "us.anthropic.claude-opus-4-1-v1:0[1m]",
        "claude-opus-4-1@20250805",
        "claude-sonnet-4-5@20250929",
        "claude-widget-9",
        "gpt-5",
        "<synthetic>",
        "<unknown>",
        "constructor",
    ]
    meta = {"rates": {model_id: {} for model_id in pricing.models}, "model_ids": dict(pricing.aliases)}

    def canonical(model_id: str) -> str | None:
        resolved = pricing.resolve_model(model_id)
        return resolved.canonical_id if resolved else None

    assert _model_ids(meta, ids) == [canonical(model_id) for model_id in ids]

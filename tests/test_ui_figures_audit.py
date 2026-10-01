"""The Overview's figures agree with the pages behind them (``service/static``).

What the design audit found, and what now holds, read from the source:

- the Overview counts, ranks, titles, links and prices recommendations
  as Actions lists them (``groupRecommendations``, exported once from
  page-actions.js), and so does the sidebar's "Do this" count;
- the daily spend chart counts replies by the day they were sent while
  the Spend figure counts whole sessions: the chart's reading gives both
  and says why they differ, on the Overview and on Spend › Usage;
- the chart spans the window's days, not only the days with spend (the
  service's local days, from /api/summary's period), and "so far" sits
  in the margin, where no bar or change label reaches;
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
    # Days run local midnight to midnight, so the chart reads higher than
    # the sessions only by the quarter hour before a window that starts
    # part way through a day (the last hour, 24 hours, since a change).
    for key in ("firstDay", "oneDayFirstDay"):
        assert "replies from just before the window began" in spec["alt"][key], key
    # One day has no busiest day and no first day of several.
    for key in ("oneDay", "oneDaySessions", "oneDayFirstDay"):
        assert "busiest" not in spec["alt"][key] and "first day" not in spec["alt"][key]
    for text in [spec["summary"], *spec["alt"].values()]:
        assert "$" not in text and "USD" not in text
        assert "UTC" not in text and "midnight" not in text
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
        # The axis comes from the summary's own period: the browser does
        # no zone maths of its own.
        assert re.search(r"windowSpan\((summaryBody|summary)\)", body), source
        assert "windowDays" not in body, source


# -- P2-13: the axis and "so far" ---------------------------------------------


def test_daily_spend_spans_the_window_it_counts() -> None:
    span = _body("charts-types.js", "windowSpan")
    assert "export function windowSpan(summaryBody)" in _static_text("charts-types.js")
    assert "windowDays" not in _app_js()
    assert "summaryBody.data.period" in span
    assert "return {};" in span
    assert "return { first: period.first_day, last: period.last_day, today: period.today };" in span
    # The Spend, Overview and Your changes charts all take their span from it.
    changes = _body("page-changes.js", "renderChanges")
    assert 'fetchJson(withWindow("/api/summary"))' in changes
    assert "windowSpan(loaded[3].body)" in changes
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


def _node(functions: list[tuple[str, str]], expression: str, preamble: str = "") -> object:
    """Run dashboard functions in Node: ``preamble`` (the module-level
    variables they read), each (module, name) function's source, then
    ``expression``, whose value (or its promise's) comes back through JSON."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node isn't installed")
    script = preamble + "\n".join(_body(module, name) for module, name in functions)
    script += f"\nPromise.resolve({expression}).then(function (value) {{ process.stdout.write(JSON.stringify(value)); }});"
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


# -- days are the service's local days ------------------------------------------

#: /api/summary's period for Last 7 days, as api.py writes it: the window's
#: local days and which is today.
_PERIOD = {"since": "2026-09-25T00:00:00Z", "until": None, "tz": None, "first_day": "2026-09-25", "last_day": "2026-10-01", "today": "2026-10-01"}


def _window_span(body: object) -> object:
    """``windowSpan`` from charts-types.js over a summary body, run in Node."""
    return _node([("charts-types.js", "windowSpan")], f"windowSpan({json.dumps(body)})")


def test_the_window_span_is_the_summarys_own_days() -> None:
    body = {"ok": True, "data": {"total_cost": 1.5, "period": _PERIOD}}
    assert _window_span(body) == {"first": "2026-09-25", "last": "2026-10-01", "today": "2026-10-01"}
    # Seven days of keys: the axis is the span, whatever days have spend.
    days = _node(
        [("charts-types.js", "utcDay"), ("charts-types.js", "dayRange")],
        'dayRange("2026-09-25", "2026-10-01")',
        preamble="var DAY_MS = 86400000;\n",
    )
    assert days == ["2026-09-25", "2026-09-26", "2026-09-27", "2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01"]


def test_a_summary_without_a_period_leaves_the_axis_to_the_days_drawn() -> None:
    """A service from before ``period``, a canned answer and a failed
    summary give no span at all. All time (no first day) leaves the axis to
    the days drawn too, but still names today, so the day still filling is
    the service's own."""
    assert _window_span({"ok": True, "data": {"total_cost": 1.5}}) == {}
    assert _window_span({"ok": True, "data": {"period": None}}) == {}
    assert _window_span({"ok": False, "error": {"code": "bad_request", "message": "no change"}}) == {}
    assert _window_span(None) == {}
    assert _window_span({"ok": True, "data": {"period": {**_PERIOD, "since": None, "first_day": None}}}) == {"today": "2026-10-01"}


def test_a_change_is_on_the_local_day_the_service_names() -> None:
    expression = (
        "[{day: '2026-09-30', ts: '2026-10-01T01:30:00Z'}, {ts: '2026-10-01T01:30:00Z'}, {day: null, ts: '2026-10-01T23:59:00Z'}, {}, null]"
        ".map(function (change) { return changeDay(change); })"
    )
    # The day wins over the timestamp's UTC date; an older service's rows
    # have only the timestamp.
    assert _node([("charts-types.js", "changeDay")], expression) == ["2026-09-30", "2026-10-01", "2026-10-01", "", ""]
    impact = {"ok": True, "data": {"changes": [{"change": {"day": "2026-09-30", "ts": "2026-10-01T01:30:00Z", "label": "Model"}}]}}
    marks = _node(
        [("charts-types.js", "changeDay"), ("charts-types.js", "dailyChanges")],
        f"dailyChanges({json.dumps(impact)}).map(function (mark) {{ return [mark.day, mark.label]; }})",
    )
    assert marks == [["2026-09-30", "Model"]]


def test_the_period_before_is_named_for_each_window() -> None:
    windows = ["1h", "24h", "today", "1", "7", "30", "90", "all", "change"]
    phrases = _node([("page-overview.js", "previousPhrase")], f"{json.dumps(windows)}.map(function (w) {{ return previousPhrase(w); }})")
    assert phrases == [
        "the hour before",
        "the 24 hours before",
        "the same hours yesterday",
        "the day before",
        "the 7 days before",
        "the 30 days before",
        "the 90 days before",
        None,
        None,
    ]


def test_the_charts_say_local_time_not_utc() -> None:
    text = _static_text("charts-types.js")
    assert 'label: "Day (UTC)"' not in text
    assert text.count('{ key: "day", label: "Day", kind: "str" }') == 2
    for name in ("stackedColumns", "changeSteps"):
        body = _body("charts-types.js", name)
        assert "Days run midnight to midnight, local time." in body, name
        assert re.findall(r'"[^"]*UTC[^"]*"', body) == [], name
    assert "(a UTC day)" not in _app_js()
    assert "a UTC day" not in _body("page-spend.js", "renderSessions")


def _sessions_on(rows: list[dict], day: str) -> list[str]:
    """The ids ``shownSessions`` (page-spend.js) lists for a picked day."""
    preamble = f"var sessionsView = {{ rows: {json.dumps(rows)}, range: null, day: {json.dumps(day)} }};\n"
    return _node([("page-spend.js", "shownSessions")], "shownSessions().map(function (row) { return row.id; })", preamble=preamble)


def test_a_picked_day_lists_the_sessions_active_on_the_charts_local_day() -> None:
    """The column is a local day, so the list matches on the service's
    first_day and last_day, not on a UTC day cut from the timestamps."""
    rows = [
        # 22:30 and 00:30 UTC are both on 1 Oct in a zone 2 hours east.
        {"id": "east", "first_ts": "2026-09-30T22:30:00Z", "last_ts": "2026-10-01T00:30:00Z", "first_day": "2026-10-01", "last_day": "2026-10-01"},
        {"id": "spans", "first_ts": "2026-09-28T09:00:00Z", "last_ts": "2026-10-02T09:00:00Z", "first_day": "2026-09-28", "last_day": "2026-10-02"},
    ]
    assert _sessions_on(rows, "2026-10-01") == ["east", "spans"]
    assert _sessions_on(rows, "2026-09-30") == ["spans"]
    assert _sessions_on(rows, "2026-10-03") == []


def test_a_picked_day_falls_back_to_the_timestamps_for_an_older_service() -> None:
    rows = [
        {"id": "ts-only", "first_ts": "2026-09-29T10:00:00Z", "last_ts": "2026-10-01T09:00:00Z"},
        {"id": "one-reply", "first_ts": "2026-09-30T10:00:00Z"},
        {"id": "no-last", "first_ts": "2026-09-30T10:00:00Z", "first_day": "2026-09-30", "last_day": None},
    ]
    assert _sessions_on(rows, "2026-09-30") == ["ts-only", "one-reply", "no-last"]
    assert _sessions_on(rows, "2026-10-01") == ["ts-only"]
    assert _sessions_on(rows, "2026-09-28") == []


#: What loadReport needs of the page around it, with a fetch that answers
#: only when told to, and a clock the test sets.
_REPORT_PREAMBLE = """
var state = { window: "30", project: "", reportPromises: {}, recommendationPromises: {}, quickActionPromises: {}, currency: "USD", units: null };
var connection = { reportFailed: false };
var calls = [];
var resolvers = [];
var clock = 0;
Date.now = function () { return clock; };
function fetchJson(url) { calls.push(url); return new Promise(function (resolve) { resolvers.push(resolve); }); }
function noteFiguresAsOf() {}
function setKnownProjects() {}
"""

_REPORT_FUNCTIONS = [
    ("api.js", name)
    for name in (
        "windowParam", "projectParam", "addParams", "withWindow", "scopeKey", "browserDay", "reportKept",
        "loadReport", "loadRecommendations", "loadQuickActions",
    )
]


def _report_script(expression: str) -> object:
    kept = re.search(r"^var REPORT_KEPT_MS = [^;]+;", _static_text("api.js"), re.M)
    assert kept, "api.js no longer says how long a report is kept"
    return _node(_REPORT_FUNCTIONS, expression, preamble=_REPORT_PREAMBLE + kept.group(0) + "\n")


def test_an_open_tab_fetches_the_report_again_after_five_minutes() -> None:
    """A "Since my last change" window starts at the newest change, so a
    tab left open mustn't keep the report it first fetched."""
    counts = _report_script(
        """(function () {
          var counts = [];
          function at(h, m, s) { clock = new Date(2026, 9, 1, h, m, s).getTime(); }
          at(12, 0, 0); loadReport(); loadReport(); counts.push(calls.length);
          at(12, 4, 59); loadReport(); counts.push(calls.length);
          at(12, 5, 0); loadReport(); loadReport(); counts.push(calls.length);
          state.project = "proj-a"; loadReport(); counts.push(calls.length);
          return counts;
        })()"""
    )
    assert counts == [1, 1, 2, 3]


def test_an_open_tab_fetches_the_report_again_when_its_day_turns_over() -> None:
    """A number of days starts at local midnight: past it, the report in
    hand is for yesterday's window, however recently it was fetched."""
    counts = _report_script(
        """(function () {
          var counts = [];
          clock = new Date(2026, 9, 1, 23, 58, 0).getTime(); loadReport(); counts.push(calls.length);
          clock = new Date(2026, 9, 1, 23, 59, 30).getTime(); loadReport(); counts.push(calls.length);
          clock = new Date(2026, 9, 2, 0, 1, 0).getTime(); loadReport(); loadReport(); counts.push(calls.length);
          return counts;
        })()"""
    )
    assert counts == [1, 1, 2]


def test_a_failed_report_is_not_kept_but_a_newer_fetch_is() -> None:
    kept = _report_script(
        """(function () {
          var failure = { httpStatus: 0, body: { ok: false, error: { code: "network_error", message: "gone" } } };
          clock = new Date(2026, 9, 1, 12, 0, 0).getTime();
          var first = loadReport();
          clock = new Date(2026, 9, 1, 12, 6, 0).getTime();
          var second = loadReport();
          // The first, expired fetch fails late: the second stays.
          resolvers[0](failure);
          return first.then(function (loaded) {
            var stayed = state.reportPromises["30"] === second;
            resolvers[1](failure);
            return second.then(function () {
              return [!!loaded.error, stayed, Object.keys(state.reportPromises).length];
            });
          });
        })()"""
    )
    assert kept == [True, True, 0]


def test_recommendations_and_checks_expire_with_the_report() -> None:
    """Recommendations and checks are built from the report, so a tab left
    open fetches them again when it fetches the report again: after five
    minutes, or past midnight."""
    counts = _report_script(
        """(function () {
          var counts = [];
          function both() { loadRecommendations(); loadQuickActions(); }
          clock = new Date(2026, 9, 1, 12, 0, 0).getTime(); both(); both(); counts.push(calls.length);
          clock = new Date(2026, 9, 1, 12, 4, 59).getTime(); both(); counts.push(calls.length);
          clock = new Date(2026, 9, 1, 12, 5, 0).getTime(); both(); both(); counts.push(calls.length);
          clock = new Date(2026, 9, 2, 0, 0, 1).getTime(); both(); counts.push(calls.length);
          return counts;
        })()"""
    )
    assert counts == [2, 2, 4, 6]

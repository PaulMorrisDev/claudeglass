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
from datetime import datetime, timedelta, timezone

import pytest

from claudeglass import helptext, impact
from claudeglass.impact import SessionFacts, _Transcript
from claudeglass.pricing import load_pricing
from claudeglass.units import Units
from test_service_static import (
    _app_js,
    _chart_specs,
    _declaration_source,
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


def _available(levers: list[dict], groups: list[dict], agents: list[list]) -> float:
    """``availableSaving`` from page-overview.js, run in Node, over the
    model lever's rows by agent type ([agent_type, observed_cost, saving_usd])."""
    preamble = "".join(_declaration_source(_static_text("page-overview.js"), name) + ";\n" for name in ("LEVER_RULES", "AGENT_MODEL_RULES"))
    preamble += 'var MAIN_AGENT = "top-level";\nvar WORKFLOW_AGENT = "workflow-subagent";\n'
    functions = [
        ("charts-types.js", "tableObjects"),
        ("page-actions.js", "groupSavingUsd"),
        ("page-overview.js", "usdOf"),
        ("page-overview.js", "combinedSaving"),
        ("page-overview.js", "agentModelItems"),
        ("page-overview.js", "availableSaving"),
    ]
    columns = [{"key": "agent_type"}, {"key": "observed_cost"}, {"key": "saving_usd"}]
    tables = {"model_swap_by_agent_type": {"columns": columns, "rows": agents}}
    expression = f"availableSaving({json.dumps(levers)}, {json.dumps(groups)}, {json.dumps(tables)})"
    return float(_node(functions, expression, preamble=preamble))


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


def test_an_agent_model_saving_joins_only_where_nothing_else_counts_it() -> None:
    """The model lever prices an Agent-tool type's inherited runs, so only
    a workflow agent's saving joins it. The asked figure is a ceiling for
    information, decided-and-changed has no saving, and an inherited card
    at info level looks fixed."""
    assert 'var WORKFLOW_AGENT = "workflow-subagent";' in _static_text("page-overview.js")
    agents = [["top-level", 300, 0], ["general-purpose", 100, 20], ["workflow-subagent", 50, 0]]

    def member(rule: str, agent: str, usd: float | None) -> dict:
        return {"id": rule, "key": f"{rule}:{agent}", "agent_type": agent, "saving_usd": usd}

    groups = [
        {
            "id": "agent-model-inherited",
            "severity": "advice",
            "members": [member("agent-model-inherited", "general-purpose", 20), member("agent-model-inherited", "workflow-subagent", 10)],
        },
        {"id": "agent-model-asked", "severity": "info", "members": [member("agent-model-asked", "claude-implementer", 40)]},
        {"id": "agent-decide-apply", "severity": "info", "members": [member("agent-decide-apply", "general-purpose", None)]},
    ]
    spend = {"top-level": 300, "general-purpose": 100, "workflow-subagent": 50}
    counted_once = _combined([{"usd": 20, "agent": "general-purpose"}, {"usd": 10, "agent": None}], spend)
    # general-purpose's $20 is the model lever's: the workflow agents' $10 joins it.
    assert abs(_available([{"key": "model_swap", "usd": 20}], groups, agents) - counted_once) < 1e-9
    # With no lever saving for general-purpose, its inherited $20 is counted once, here.
    agents[1][2] = 0
    assert abs(_available([], groups, agents) - counted_once) < 1e-9
    # An inherited card at info level looks fixed: nothing of it is left to save.
    groups[0]["severity"] = "info"
    assert _available([], groups, agents) == 0


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


# -- the change cards follow the window and the project ---------------------


#: What renderChangeCards needs of the page around it, as a small element
#: tree: the service's own words (windowWhen, changeDay) are real; the
#: elements, links, buttons and the card itself are stand-ins that keep
#: what a test reads. ``state`` is set by each call.
_CARDS_PREAMBLE = """
var state = { window: "30", project: "" };
function projectName(slug) { return slug; }
function plain(n) {
  if (n.text !== undefined) return n.text;
  return n.children.map(plain).join("");
}
function walk(n, found) {
  n.children.forEach(function (c) { if (c.children) { found.push(c); walk(c, found); } });
  return found;
}
function el(tag, attrs, children) {
  var n = { tag: tag, className: (attrs && attrs.class) || "", hidden: false, children: [], tabIndex: null, focused: false };
  n.appendChild = function (c) { n.children.push(c); return c; };
  n.removeChild = function (c) { n.children = n.children.filter(function (x) { return x !== c; }); };
  n.focus = function () { n.focused = true; };
  function matches(c, selector) {
    var named = (" " + c.className + " ").indexOf(" " + selector.split("[")[0].slice(1) + " ") !== -1;
    return named && (selector.indexOf("[hidden]") === -1 || c.hidden);
  }
  n.querySelectorAll = function (selector) { return walk(n, []).filter(function (c) { return matches(c, selector); }); };
  n.querySelector = function (selector) { return n.querySelectorAll(selector)[0] || null; };
  (children || []).forEach(function (c) { if (c !== null && c !== undefined) n.appendChild(typeof c === "string" ? { text: c } : c); });
  return n;
}
function emptyState(message, gate, next) {
  var box = el("div", { class: "empty-state" }, [message]);
  if (next) box.appendChild(typeof next === "string" ? { text: " " + next } : el("span", {}, [" ", next]));
  return box;
}
function pageLink(key, text, params) {
  var link = el("a", { class: "page-link" }, [text]);
  link.params = params || null;
  return link;
}
function button(label, opts) {
  var node = el("button", { class: "button" }, [label]);
  node.click = opts.action;
  return node;
}
function changeCard(item) {
  var card = el("article", { class: "change-card" });
  card.label = item.change.label;
  return card;
}
function drawn(data, opts, scope) {
  state.window = scope.window;
  state.project = scope.project;
  var container = el("div");
  var count = renderChangeCards(container, data, opts);
  var list = container.children[0];
  var cards = list && list.className === "change-cards";
  return {
    count: count,
    text: plain(container),
    items: cards
      ? list.children.map(function (c) { return c.label !== undefined ? { card: c.label, hidden: c.hidden } : { text: plain(c), class: c.className }; })
      : [],
    links: walk(container, []).filter(function (c) { return c.params; }).map(function (c) { return [plain(c), c.params]; }),
  };
}
"""

_CARDS_FUNCTIONS = [
    ("format.js", "windowWhen"),
    ("charts-types.js", "changeDay"),
    ("page-changes.js", "judgedFirst"),
    ("page-changes.js", "waitingLine"),
    ("page-changes.js", "showOlderChanges"),
    ("page-changes.js", "olderButton"),
    ("page-changes.js", "renderChangeCards"),
]


def _cards_preamble() -> str:
    """The preamble with the file's own NO_CHANGES_NEXT, so the empty
    states are checked against the words the dashboard has."""
    said = re.search(r'^var NO_CHANGES_NEXT = "[^"]*";', _static_text("page-changes.js"), re.M)
    assert said, "page-changes.js no longer says what a change is for"
    return _CARDS_PREAMBLE + said.group(0) + "\n"


def _change(label: str, ts: str, *, enough: bool = True, before: int = 5, after: int = 5) -> dict:
    """One row of /api/impact's changes, as the cards read it."""
    return {
        "change": {"label": label, "ts": ts, "day": ts[:10]},
        "enough": enough,
        "before_sessions": before,
        "after_sessions": after,
    }


def _data(*changes: dict) -> dict:
    return {"changes": list(changes), "min_sessions": 3, "caveat": ""}


def _drawn(data: dict | None, opts: dict | None = None, *, window: str = "30", project: str = "") -> dict:
    scope = {"window": window, "project": project}
    expression = f"drawn({json.dumps(data)}, {json.dumps(opts or {})}, {json.dumps(scope)})"
    return _node(_CARDS_FUNCTIONS, expression, preamble=_cards_preamble())


def _picked(changes: list[dict], limit: int = 2) -> dict:
    """``judgedFirst``'s answer over ``changes``, as labels."""
    expression = (
        "(function (picked) { return {"
        " judged: picked.judged.map(function (c) { return c.change.label; }),"
        " waiting: picked.waiting.map(function (c) { return c.change.label; }) }; })"
        f"(judgedFirst({json.dumps(changes)}, {limit}, 3))"
    )
    return _node(_CARDS_FUNCTIONS, expression, preamble=_cards_preamble())


#: A window's changes, newest first: two still waiting for sessions after
#: them, two that can be judged, and one waiting that is older than them.
_MIXED = [
    _change("C6", "2026-09-30T10:00:00Z", enough=False, after=0),
    _change("C5", "2026-09-29T10:00:00Z", enough=False, after=2),
    _change("C4", "2026-09-27T10:00:00Z"),
    _change("C3", "2026-09-26T10:00:00Z"),
    _change("C2", "2026-09-25T10:00:00Z", enough=False, after=1),
    _change("C1", "2026-09-24T10:00:00Z"),
]


def test_the_overview_leads_with_the_changes_it_can_judge() -> None:
    # The newest two that can be judged lead; the waiting ones are the
    # newer ones, not C2, which is older than the newest judged.
    assert _picked(_MIXED) == {"judged": ["C4", "C3"], "waiting": ["C6", "C5"]}
    assert _picked(_MIXED, limit=1)["judged"] == ["C4"]
    assert _picked(_MIXED, limit=9)["judged"] == ["C4", "C3", "C1"]


def test_only_changes_short_of_sessions_after_them_are_too_new_to_judge() -> None:
    """A newer change short of sessions before it isn't too new: counting
    it would promise sessions that won't change its answer."""
    early = _change("early", "2026-09-30T10:00:00Z", enough=False, before=1, after=5)
    fresh = _change("fresh", "2026-09-29T10:00:00Z", enough=False, before=5, after=0)
    held = _change("held", "2026-09-27T10:00:00Z")
    assert _picked([early, fresh, held])["waiting"] == ["fresh"]
    # Short before, and nothing else newer: no line at all.
    drawn = _drawn(_data(early, held), {"compact": True, "limit": 2, "judgedFirst": True})
    assert [item.get("card") for item in drawn["items"]] == ["held"]
    assert drawn["links"] == []


def test_none_judged_means_nothing_waits() -> None:
    changes = [_change(f"C{n}", f"2026-09-{20 + n}T10:00:00Z", enough=False, after=n - 1) for n in (3, 2, 1)]
    assert _picked(changes) == {"judged": [], "waiting": []}


def test_the_waiting_line_counts_the_newer_changes_and_leads_to_the_newest() -> None:
    drawn = _drawn(_data(*_MIXED), {"compact": True, "limit": 2, "judgedFirst": True})
    assert drawn["items"][0] == {
        "text": "2 newer changes are too new to judge yet: each needs 3 sessions after it.",
        "class": "notes change-waiting",
    }
    assert [item["card"] for item in drawn["items"][1:]] == ["C4", "C3"]
    # The count leads to Your changes at the newest of them, on the day the
    # service names; and every change in the window is counted for the link.
    assert drawn["links"] == [["2 newer changes", {"day": "2026-09-30"}]]
    assert drawn["count"] == len(_MIXED)


def test_one_waiting_change_reads_in_the_singular() -> None:
    changes = [_change("C2", "2026-09-30T10:00:00Z", enough=False, after=1), _change("C1", "2026-09-26T10:00:00Z")]
    drawn = _drawn(_data(*changes), {"compact": True, "limit": 2, "judgedFirst": True})
    assert drawn["items"][0]["text"] == "1 newer change is too new to judge yet: it needs 3 sessions after it."
    assert drawn["links"] == [["1 newer change", {"day": "2026-09-30"}]]


def test_with_none_judged_the_newest_changes_are_drawn_as_they_are() -> None:
    changes = [_change(f"C{n}", f"2026-09-{20 + n}T10:00:00Z", enough=False, after=n - 1) for n in (3, 2, 1)]
    drawn = _drawn(_data(*changes), {"compact": True, "limit": 2, "judgedFirst": True})
    assert drawn["items"] == [{"card": "C3", "hidden": False}, {"card": "C2", "hidden": False}]
    assert drawn["links"] == []
    assert drawn["count"] == 3
    # Without judgedFirst the newest are drawn the same way, whatever can be judged.
    unordered = _drawn(_data(*_MIXED), {"compact": True, "limit": 2})
    assert [item["card"] for item in unordered["items"]] == ["C6", "C5"]


def test_an_empty_window_says_where_older_changes_are() -> None:
    nothing = _data()
    overview = _drawn(nothing, {"compact": True}, window="7")
    assert overview["text"] == "No changes in the last 7 days. See older ones on Your changes under All time."
    assert overview["links"] == [["Your changes", {"w": "all"}]]
    page = _drawn(nothing, {}, window="7")
    assert page["text"] == "No changes in the last 7 days. Pick All time to see older ones."
    assert page["links"] == [["All time", {"w": "all"}]]
    # The window's words come from windowWhen, one for each.
    assert _drawn(nothing, {}, window="today")["text"].startswith("No changes today.")
    assert _drawn(nothing, {}, window="24h")["text"].startswith("No changes in the last 24 hours.")
    assert _drawn(nothing, {}, window="1")["text"].startswith("No changes in the last day.")
    assert _drawn(nothing, {}, window="change")["text"].startswith("No changes since your last change.")
    # A project is named, in every window but All time.
    assert _drawn(nothing, {}, window="30", project="acme")["text"].startswith("No changes in acme in the last 30 days.")


def test_all_time_with_no_change_says_so_for_the_project_or_for_every_project() -> None:
    nothing = _data()
    every = _drawn(nothing, {}, window="all")
    assert every["text"].startswith("No changes recorded yet. When you apply a profile or fix")
    assert every["links"] == []
    mine = _drawn(nothing, {}, window="all", project="acme")
    assert mine["text"].startswith("No changes in acme yet. When you apply a profile or fix")
    # "Since my last change" with none recorded has no older ones to point
    # to either: it reads as All time with nothing in it.
    nowhere = _drawn(None, {"noOlder": True}, window="change")
    assert nowhere["text"].startswith("No changes recorded yet.") and nowhere["links"] == []
    nowhere = _drawn(None, {"noOlder": True, "compact": True}, window="change", project="acme")
    assert nowhere["text"].startswith("No changes in acme yet.")


def test_a_change_window_with_no_change_recorded_is_not_an_error() -> None:
    failure = {"ok": False, "error": {"code": "bad_request", "message": "No change recorded yet. Pick another window."}}
    unknown = {"ok": False, "error": {"code": "bad_request", "message": "'project' does not match a known project"}}
    outage = {"ok": False, "error": {"code": "internal", "message": "boom"}}
    ok = {"ok": True, "data": {"changes": []}}
    functions = [("page-changes.js", "noChangeYet")]
    preamble = 'var state = { window: "x", project: "" };\n'

    def asked(window: str, body: object) -> object:
        expression = f"(function () {{ state.window = {json.dumps(window)}; return noChangeYet({json.dumps(body)}); }})()"
        return _node(functions, expression, preamble=preamble)

    assert asked("change", failure) is True
    # An unknown project gives way to every project on its own; any other
    # failure, and any other window, stays an error.
    assert asked("change", unknown) is False
    assert asked("change", outage) is False
    assert asked("change", ok) is False
    assert asked("change", None) is False
    assert asked("7", failure) is False


def test_your_changes_folds_the_cards_past_ten_behind_a_button() -> None:
    changes = [_change(f"C{n:02d}", f"2026-09-{n:02d}T10:00:00Z") for n in range(13, 0, -1)]
    expression = (
        """(function () {
      state.window = "all";
      state.project = "";
      var container = el("div");
      var count = renderChangeCards(container, %s, { foldAfter: 10 });
      var list = container.children[0];
      var before = list.children.map(function (c) { return c.label !== undefined ? [c.label, c.hidden] : plain(c); });
      list.children[list.children.length - 1].children[0].click();
      var cards = list.children.filter(function (c) { return c.label !== undefined; });
      return {
        count: count,
        before: before,
        hiddenAfter: cards.filter(function (c) { return c.hidden; }).length,
        kids: list.children.length,
        focused: cards.filter(function (c) { return c.focused; }).map(function (c) { return c.label; }),
      };
    })()"""
        % json.dumps(_data(*changes))
    )
    out = _node(_CARDS_FUNCTIONS, expression, preamble=_cards_preamble())
    assert out["count"] == 13
    shown = [entry for entry in out["before"] if isinstance(entry, list)]
    assert [label for label, hidden in shown if not hidden] == [f"C{n:02d}" for n in range(13, 3, -1)]
    assert [label for label, hidden in shown if hidden] == ["C03", "C02", "C01"]
    assert out["before"][-1] == "Show 3 older changes"
    # The button is gone once it is used, every card shows, and the first
    # of the older ones takes focus.
    assert out["hiddenAfter"] == 0 and out["kids"] == 13
    assert out["focused"] == ["C03"]


def test_ten_or_fewer_changes_have_nothing_to_fold() -> None:
    changes = [_change(f"C{n:02d}", f"2026-09-{n:02d}T10:00:00Z") for n in range(10, 0, -1)]
    drawn = _drawn(_data(*changes), {"foldAfter": 10}, window="all")
    assert len(drawn["items"]) == 10 and not any(item.get("hidden") for item in drawn["items"])
    assert "Show" not in drawn["text"]


# -- a change card reads tokens and replies by which way is better -----------


def test_a_tokens_row_and_a_count_row_read_by_which_way_is_better() -> None:
    """A model change is judged on tokens and replies as well as money.
    Fewer of either is good news and more is bad, as it is for a price,
    and the card draws the server's own text for them, never money."""

    def sessions(replies: int, tokens: int) -> list[SessionFacts]:
        return [
            SessionFacts(
                start=datetime(2026, 9, 20, tzinfo=timezone.utc) + timedelta(hours=n),
                main=_Transcript(turns=replies, total_tokens=tokens),
            )
            for n in range(4)
        ]

    def row(measure: impact.Measure, before: list[SessionFacts], after: list[SessionFacts]) -> dict:
        rows = [impact._measure_row(measure, before, after, Units(billing_mode="api", currency="USD"))]
        impact._label_rows(rows)
        return rows[0]

    rows = [
        row(impact._TOKENS, sessions(10, 100_000), sessions(10, 50_000)),
        row(impact._TOKENS, sessions(10, 50_000), sessions(10, 100_000)),
        row(impact._REPLIES, sessions(20, 1), sessions(10, 1)),
        row(impact._REPLIES, sessions(10, 1), sessions(20, 1)),
        row(impact._TOKENS, sessions(10, 50_000), sessions(10, 50_000)),
    ]
    assert [(r["kind"], r["better"], r["before"], r["after"]) for r in rows] == [
        ("tokens", "lower", "100,000 tokens", "50,000 tokens"),
        ("tokens", "lower", "50,000 tokens", "100,000 tokens"),
        ("count", "lower", "20.0", "10.0"),
        ("count", "lower", "10.0", "20.0"),
        ("tokens", "lower", "50,000 tokens", "50,000 tokens"),
    ]
    tones = _node(
        [("page-changes.js", "readingTone")],
        f"{json.dumps(rows)}.map(readingTone)",
        preamble=_declaration_source(_static_text("page-changes.js"), "READINGS") + ";\n",
    )
    assert tones == ["good", "bad", "good", "bad", "neutral"]


# -- the lead measure and the mix of sessions on a change card ------------------


def _lead(item: dict) -> object:
    """``leadMeasure`` from page-changes.js, run in Node: the key it picks, or None."""
    expression = f"(function (m) {{ return m && m.key; }})(leadMeasure({json.dumps(item)}))"
    return _node([("page-changes.js", "leadMeasure")], expression)


def test_a_change_card_leads_with_the_measure_the_server_names_not_the_first_listed() -> None:
    measures = [{"key": "tokens_per_session"}, {"key": "cost_per_turn"}, {"key": "cost_per_session"}]
    assert _lead({"lead": "cost_per_turn", "measures": measures}) == "cost_per_turn"
    # No lead named (too few sessions, or an older server): the first it lists.
    assert _lead({"lead": None, "measures": measures}) == "tokens_per_session"
    assert _lead({"measures": measures}) == "tokens_per_session"
    # A name no measure has falls back the same way; no measures is no lead.
    assert _lead({"lead": "nothing_like_it", "measures": measures}) == "tokens_per_session"
    assert _lead({"lead": "cost_per_turn", "measures": []}) is None
    assert _lead({}) is None


_MIX_PREAMBLE = """
function el(tag, attrs, children) {
  return { tag: tag, attrs: attrs || {}, children: (children || []).filter(function (c) { return c !== null && c !== undefined; }) };
}
function chip(text, opts) { return { chip: text, opts: opts }; }
"""


def _mix_note(item: dict) -> object:
    return _node([("page-changes.js", "mixNote")], f"mixNote({json.dumps(item)})", preamble=_MIX_PREAMBLE)


def test_the_mix_note_shows_the_servers_sentence_only_when_the_mix_moved() -> None:
    said = "Scheduled runs were 0% of the sessions before this change and 40% after."
    note = _mix_note({"mix": {"flagged": True, "text": said}})
    assert note["attrs"]["class"] == "change-mix"
    chip_node, text_node = note["children"]
    assert chip_node["chip"] == "Session mix changed" and chip_node["opts"]["tone"] == "warn"
    assert chip_node["opts"]["icon"] == "warning"
    assert text_node["attrs"] == {"class": "change-mix-text", "text": said}
    for item in ({}, {"mix": None}, {"mix": {"flagged": False, "text": ""}}):
        assert _mix_note(item) is None, item

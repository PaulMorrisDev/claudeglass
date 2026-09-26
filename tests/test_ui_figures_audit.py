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
  points at under More tables.
"""

from __future__ import annotations

import re

from claudeglass import helptext
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
    assert set(spec["alt"]) == {"sessions", "firstDay"}
    for text in spec["alt"].values():
        assert text.startswith("Replies sent {span} cost {total}.")
        assert "{sessionsTotal}" in text
    assert "earlier replies" in spec["alt"]["sessions"]
    assert "counts all of the first day" in spec["alt"]["firstDay"]
    for text in [spec["summary"], *spec["alt"].values()]:
        assert "$" not in text and "USD" not in text
    columns = _body("charts-types.js", "stackedColumns")
    # Only when the two read differently, in the billing mode's own words.
    assert "moneyText(sessionsUsd) !== moneyText(grand) ? moneyText(sessionsUsd) : null" in columns
    assert 'variant: sessionsTotal ? (sessionsUsd > grand ? "sessions" : "firstDay") : null' in columns
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


def _combined(items: list[dict], spend: dict[str, float]) -> float:
    """``combinedSaving`` from page-overview.js, run in Node."""
    import json
    import shutil
    import subprocess

    import pytest

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node isn't installed")
    script = (
        _body("page-overview.js", "combinedSaving")
        + f"\nprocess.stdout.write(String(combinedSaving({json.dumps(items)}, {json.dumps(spend)})));"
    )
    done = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30, check=True)
    return float(done.stdout)


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

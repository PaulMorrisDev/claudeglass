"""The dashboard's motion, forced colours and load budget (``service/static``).

What a screenshot can't prove, read from the source (docs/ui.md,
"Motion", "Forced colours" and "Performance"):

- a new view fades in over the old one as a View Transition, only for
  a real change of view with motion welcome, and at the timing app.css
  sets;
- the Overview's entrance: the headline figures count up with the final
  text in place first and guaranteed at the end, the chart draws in
  80ms after them, and the next best actions arrive 24ms apart, at most
  six staggered;
- a chart resized mid-draw carries its draw-in on to the new size;
- under reduced motion none of it runs;
- in forced colours the charts keep their own colours while the words,
  lines, rings and borders around them take the system's;
- the shell marks when it is ready, and Actions' figures are fetched
  while the Overview is idle, once per window.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = REPO_ROOT / "src" / "claudeglass" / "service" / "static"


def _text(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


def _css() -> str:
    return _text("app.css")


def _block_end(src: str, open_brace: int) -> int:
    """The index just past the brace that closes the one at
    ``open_brace``. The modules checked here keep braces out of their
    strings and comments near these functions."""
    depth = 0
    for i in range(open_brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    raise AssertionError("unbalanced braces")


def _function(module: str, name: str) -> str:
    src = _text(module)
    match = re.search(r"(?<![\w$.])function\s+" + re.escape(name) + r"\s*\(", src)
    assert match, f"no function {name}() in {module}"
    return src[match.start() : _block_end(src, src.index("{", match.end()))]


def _rule(css: str, selector: str) -> str:
    """The body of the first rule whose selector list is exactly
    ``selector`` (whitespace between its parts ignored)."""
    pattern = r"(?:^|[}\s])" + r"\s*".join(re.escape(part) for part in selector.split()) + r"\s*\{([^{}]*)\}"
    match = re.search(pattern, css, re.MULTILINE)
    assert match, f"app.css has no rule for {selector!r}"
    return match.group(1)


def _media(css: str, query: str) -> str:
    """Everything inside ``@media (query) { ... }``."""
    start = css.index("@media (" + query + ")")
    return css[start : _block_end(css, css.index("{", start))]


# -- a new view -----------------------------------------------------------


def test_a_view_transition_runs_only_for_a_real_change_with_motion_welcome() -> None:
    change = _function("app.js", "changeView")
    for guard in (
        "router.current !== null",
        "key !== router.current",
        'typeof document.startViewTransition === "function"',
        "motionOK()",
        "!document.hidden",
    ):
        assert guard in change, guard
    # Otherwise the change is made at once.
    assert re.search(r"if \(!moving\) \{\s*change\(\);\s*return;", change)
    assert "document.startViewTransition(function" in change
    # A transition cut short has still made its change: nothing rejects
    # unhandled, and the naming class always comes off.
    assert "transition.ready.catch(" in change
    assert "transition.finished.then(settled, settled)" in change
    assert 'root.classList.remove("view-changing")' in change


def test_every_route_change_goes_through_the_transition() -> None:
    route = _function("app.js", "resolveRoute")
    assert "changeView(key, function () {" in route
    assert "showView(key, options);" in route
    # Focus and scroll are still showView's, inside the change.
    show = _function("app.js", "showView")
    assert 'behavior: "instant"' in show
    assert "if (options.focus) focusTitle();" in show


def test_the_views_have_their_own_transition_name_and_timing() -> None:
    css = _css()
    assert "view-transition-name: views;" in _rule(css, ".view-changing .views")
    old = _rule(css, "::view-transition-old(views)")
    assert "fade-out var(--dur-page-out) var(--ease-in)" in old
    new = _rule(css, "::view-transition-new(views)")
    assert "rise-in var(--dur-page) var(--ease-out)" in new
    assert re.search(r"--dur-page:\s*220ms;", css)
    assert re.search(r"--dur-page-out:\s*120ms;", css)
    rise = re.search(r"@keyframes rise-in \{(.*?)\n\}", css, re.S)
    assert rise, "no rise-in keyframes"
    assert "translateY(6px)" in rise.group(1)


def test_a_click_during_a_page_change_reaches_the_live_page() -> None:
    # Chromium hit-tests only the root while the views fade, so a click in
    # those 220ms ends the transition and goes to what is under the pointer.
    app = _text("app.js")
    assert 'document.addEventListener("click", passClickThrough);' in app
    through = _function("app.js", "passClickThrough")
    assert "event.target !== document.documentElement" in through
    assert "transition.skipTransition();" in through
    assert "document.elementFromPoint(x, y)" in through
    assert 'new MouseEvent("click"' in through
    change = _function("app.js", "changeView")
    assert "liveTransition = transition;" in change
    assert "if (liveTransition === transition) liveTransition = null;" in change


def test_ui_keyframes_move_only_transform_and_opacity() -> None:
    css = _css()
    for name in ("rise-in", "fade-out"):
        frames = re.search(r"@keyframes " + name + r" \{(.*?)\n\}", css, re.S)
        assert frames, name
        properties = set(re.findall(r"([a-z-]+)\s*:", frames.group(1)))
        assert properties <= {"opacity", "transform"}, (name, properties)


# -- the Overview's entrance ---------------------------------------------


def test_a_figure_counts_up_from_its_final_text_and_always_ends_on_it() -> None:
    count = _function("ui.js", "countUp")
    assert "motionOK()" in count
    assert "document.hidden" in count
    # The final text is read from the node, so it is in the DOM first.
    assert "var final = node.textContent;" in count
    assert "node.textContent = final;" in count
    # If animation frames stop, the timer or the tab going away finishes.
    assert "setTimeout(finish, COUNT_MS + 100)" in count
    assert 'addEventListener("visibilitychange", finish)' in count
    assert re.search(r"var COUNT_MS = 700;", _text("ui.js"))
    # Figures that land on a view you have left show at once: a count run
    # out of sight would be over, or replay late, by the time you look.
    assert "!node.getClientRects().length" in count


def test_the_headline_figures_count_from_the_last_ones_shown() -> None:
    overview = _text("page-overview.js")
    tiles = _function("page-overview.js", "countTiles")
    assert "shownFigures || {}" in tiles
    assert "countUp(count.node, from[count.key] || 0, count.value, count.write)" in tiles
    # Money stays in the billing mode's units, each step in the unit the
    # figure ends in: a count past 200% of the weekly limit doesn't switch
    # to weeks midway.
    assert "return moneyParts(usd, { like: final }).value;" in _function("page-overview.js", "moneyValue")
    assert "countTiles(tiles.counts);" in overview


def test_the_chart_draws_in_after_the_figures() -> None:
    overview = _text("page-overview.js")
    assert re.search(r"var CHART_AFTER_MS = 80;", overview)
    assert "entrance + CHART_AFTER_MS - performance.now()" in overview
    # It also waits for the window's sessions, which its reading gives.
    assert "Promise.all([dailyLoad, impactLoad, reportLoad, figuresDone, summaryLoad])" in overview
    # A failure drawing the figures doesn't stop the chart.
    assert "var figuresDone = figuresDrawn.then(null, function (err) {" in overview
    # The delay reaches every transition a chart form starts.
    assert "selection.transition().delay(ctx.delay)" in _text("charts-types.js")
    context = _function("charts.js", "drawContext")
    assert "var delay = (animate && how.delay) || 0;" in context


def test_the_checklist_rows_arrive_in_turn() -> None:
    enter = _function("ui.js", "enterInTurn")
    assert "motionOK()" in enter
    assert "Math.min(i, 5) * 24" in enter
    assert 'classList.remove("is-entering")' in enter
    assert '"animationcancel"' in enter
    # Rows drawn on a view you have left arrive as they are.
    assert "!rows[0].getClientRects().length" in enter
    assert 'enterInTurn(wrong.body.querySelectorAll(".check-row"), ROWS_AFTER_MS)' in _text("page-overview.js")


def test_a_chart_resized_mid_draw_carries_its_draw_in_on() -> None:
    carry = _function("charts.js", "redraw")
    assert "now < moving.ends" in carry
    assert "ms: moving.ends - Math.max(now, moving.starts)" in carry
    # A settled chart is redrawn at once, anything still moving stopped.
    assert ".interrupt()" in carry
    assert "else redraw(frame);" in _function("charts.js", "wireResize")
    # The session timeline, rebuilt on every draw, starts its line from as
    # much as was drawn rather than drawing it again from nothing.
    timeline = _function("charts-types.js", "buildSessionTimeline")
    assert 'var left = ctx.resize ? lineStillToDraw(plot.select(".chart-line").node()) : 1;' in timeline
    assert '.attr("stroke-dashoffset", length * left)' in timeline
    assert "if (ms && left > 0)" in timeline
    still = _function("charts-types.js", "lineStillToDraw")
    assert 'getAttribute("stroke-dashoffset")' in still and "if (!(length > 0)) return 0;" in still


def test_a_morph_skips_charts_with_too_many_marks() -> None:
    assert re.search(r"var MAX_MORPH_MARKS = 1500;", _text("charts.js"))
    assert "(marks || 0) > MAX_MORPH_MARKS) return 0;" in _function("charts.js", "drawContext")


# -- reduced motion -------------------------------------------------------


def test_reduced_motion_stills_the_transition_and_the_entrance() -> None:
    block = _media(_css(), "prefers-reduced-motion: reduce")
    assert re.search(r"::view-transition-group\(\*\),\s*::view-transition-old\(\*\),\s*::view-transition-new\(\*\)\s*\{\s*animation: none !important;", block)
    assert re.search(r"\.is-entering\s*\{\s*animation: none;", block)
    # And the modules start none of it (see the guards above).
    assert "motionOK()" in _function("ui.js", "countUp")
    assert "motionOK()" in _function("charts.js", "drawContext")


# -- forced colours -------------------------------------------------------


def test_forced_colours_keep_chart_colours_and_system_chrome() -> None:
    block = _media(_css(), "forced-colors: active")
    assert re.search(r"\.chart-svg,\s*\.sparkline,\s*\.swatch\s*\{\s*forced-color-adjust: none;", block)
    assert re.search(r"\.chart-axis text,[^{]*\{\s*fill: CanvasText;", block)
    assert re.search(r"\.chart-grid\s*\{\s*stroke: GrayText;", block)
    assert re.search(r"\.chart-baseline,[^{]*\{\s*stroke: CanvasText;", block)
    assert re.search(r":focus-visible,\s*\.chart-plot:focus-visible\s*\{\s*outline-color: Highlight;", block)
    borders = re.search(r"\.severity-badge,([^{]*)\{\s*border-color: CanvasText;", block)
    assert borders, "no border rule for chips and tiles"
    for part in (".chip", ".metric-tile", ".drawer", ".popover", ".menu", ".filter-chip", ".control-button"):
        assert re.search(re.escape(part) + r"[,\s]", borders.group(1)), part


# -- the load budget ------------------------------------------------------


def test_the_shell_marks_when_it_is_ready() -> None:
    init = _function("app.js", "init")
    assert init.rstrip("}").rstrip().endswith('performance.mark("cg-shell-ready");')


def test_actions_figures_are_fetched_while_the_overview_is_idle() -> None:
    prefetch = _function("api.js", "prefetchActions")
    assert "loadRecommendations();" in prefetch
    assert "loadQuickActions();" in prefetch
    assert 'window.requestIdleCallback(fetchBoth, { timeout: 2000 })' in prefetch
    assert "setTimeout(fetchBoth" in prefetch
    assert "if (current()) prefetchActions();" in _text("page-overview.js")


def test_the_checks_are_fetched_once_per_window_and_project() -> None:
    quick = _function("api.js", "loadQuickActions")
    assert "var key = scopeKey();" in quick
    assert "state.quickActionPromises[key]" in quick
    assert "delete state.quickActionPromises[key]" in quick
    assert "quickActionPromises: {}" in _text("core.js")
    # A new window or project fetches them fresh (app.js's scopeChanged).
    assert "delete state.quickActionPromises[scopeKey()];" in _function("app.js", "scopeChanged")
    assert "state.quickActionPromises = {};" in _function("shell.js", "redrawEverything")
    # Actions reads it through the cache, never straight from the API.
    actions = _text("page-actions.js")
    assert 'withWindow("/api/quick-actions")' not in actions
    assert actions.count("loadQuickActions()") == 2
    # Search shares the same cache, and keeps its entries with the rest,
    # so a new window or project and Redraw figures clear them too.
    assert "loadQuickActions()" in _text("palette.js")
    assert '"/api/quick-actions")' not in _text("palette.js")
    entries = _function("palette.js", "loadEntries")
    assert "state.searchPromises[key]" in entries
    assert "var loaded" not in _text("palette.js")
    assert "searchPromises: {}" in _text("core.js")
    assert "delete state.searchPromises[scopeKey()];" in _function("app.js", "scopeChanged")
    assert "state.searchPromises = {};" in _function("shell.js", "redrawEverything")

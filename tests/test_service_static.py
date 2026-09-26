"""Static web UI asset tests (work package S1-ui).

Three things are checked here:

1. Egress/safety of the first-party ``service/static/`` files (every
   ``*.html``/``*.js``/``*.css`` directly in it; the pinned third-party
   ``vendor/`` and ``fonts/`` trees are covered by their own tests):
   ``index.html`` references only same-origin ``/static/`` files that
   exist, and none of them contains a bare ``http(s)://`` literal, an inline
   ``<script>`` body, an ``on<event>=`` handler attribute, a dynamic
   ``import(``/``eval(`` call, an ``@import url(http...)``, or an emoji
   code point -- the same "no external reference, no inline execution"
   posture ``SECURITY.md`` and ``docs/ui.md`` require of this UI.
2. The dashboard's modules call every documented ``GET`` route in
   ``docs/api.md`` (minus the routes this test deliberately
   excludes -- see ``_EXCLUDED_ROUTE_PREFIXES`` and
   ``_NOT_FETCHED_BY_DASHBOARD``), and ``service/static/*``
   is registered as package data in ``pyproject.toml``.
3. A tiny, test-only fixture HTTP server -- there is no ``api.py`` yet
   for a real end-to-end run against -- serves the static directory
   plus canned JSON for every ``/api/*`` route, built from
   ``tests/helpers`` builders through ``report.build_report`` and
   ``render/json_out``, plus a seeded ``Store``. ``urllib.request``
   fetches ``/`` and each static file and asserts a 200 status and the
   expected content type.
"""

from __future__ import annotations

import ast
import http.server
import json
import re
import threading
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from claudeglass import backtest, capture_view, footprint, helptext, quick_actions, setup_status, skills_review
from claudeglass.config import CaptureConfig, Config
from claudeglass.corpus import load_corpus
from claudeglass.pricing import load_pricing
from claudeglass.profiles import goals
from claudeglass.profiles import schema as profile_schema
from claudeglass.report import build_report
from claudeglass.render.json_out import render_json, to_jsonable
from claudeglass.service.store import Store
from claudeglass.snapshots import Snapshot
from claudeglass.units import Units

from helpers import turn_line, write_jsonl

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = REPO_ROOT / "src" / "claudeglass" / "service" / "static"
API_MD = REPO_ROOT / "docs" / "api.md"
README_MD = REPO_ROOT / "README.md"
SECTIONS_REFERENCE_MD = REPO_ROOT / "docs" / "sections-reference.md"
PYPROJECT_TOML = REPO_ROOT / "pyproject.toml"

#: The files the dashboard cannot start without.
STATIC_FILES = ("index.html", "app.js", "app.css")

#: A floor on how many first-party ES modules the glob below must find, so
#: a glob that silently matches nothing (or only app.js) fails loudly
#: instead of turning every scan in this file into a no-op.
_MIN_JS_MODULES = 26


def _first_party_files() -> list[Path]:
    """Every first-party file the page loads: the ``*.html``, ``*.js`` and
    ``*.css`` files directly in ``static/``. The vendored d3 and the fonts
    live in the ``vendor/`` and ``fonts/`` subdirectories, which this
    never descends into."""
    return sorted(p for p in STATIC_DIR.iterdir() if p.is_file() and p.suffix in (".html", ".js", ".css"))


FIRST_PARTY_FILES = tuple(p.name for p in _first_party_files())

#: Documented GET routes this test does not require the dashboard's modules to fetch:
#: the profile-diff route is only ever reached from a click handler
#: (its id is dynamic, so there is no static literal to grep for), and
#: report.md/report.html are alternative renderings of report.json the
#: UI has no reason to also fetch.
_EXCLUDED_ROUTE_PREFIXES = (
    "/api/profiles/<id>/diff",
    "/api/report.md",
    "/api/report.html",
)

#: Documented GET routes the fixture server still serves but the
#: dashboard no longer fetches: /api/recache is all history and takes no
#: window, so Cache > Rebuilds draws the window's own breakdown from
#: report.json's recache_signature_split instead (design audit P1-4).
_NOT_FETCHED_BY_DASHBOARD = ("/api/recache",)

_FORBIDDEN_SUBSTRING_PATTERNS = {
    "bare http(s):// literal": re.compile(r"https?://"),
    "on<event>= handler attribute": re.compile(r"\bon[a-z]+\s*=", re.IGNORECASE),
    "dynamic import(...)": re.compile(r"\bimport\s*\("),
    "eval(...)": re.compile(r"\beval\s*\("),
    "@import url(http...)": re.compile(r"@import\s+url\(\s*['\"]?https?:", re.IGNORECASE),
}

#: Emoji/decorative-symbol code point ranges. Deliberately excludes the
#: em/en dash, ellipsis and similar typographic punctuation the UI does
#: use -- those are not emoji.
_EMOJI_RANGES = (
    (0x1F1E6, 0x1FAFF),  # regional indicators through symbols/pictographs
    (0x2600, 0x27BF),  # misc symbols and dingbats
    (0x2B00, 0x2BFF),  # misc symbols and arrows
    (0x2190, 0x21FF),  # arrows
    (0x200D, 0x200D),  # zero-width joiner (emoji sequences)
    (0xFE0F, 0xFE0F),  # variation selector-16 (emoji presentation)
)


def _is_emoji_code_point(code_point: int) -> bool:
    return any(low <= code_point <= high for low, high in _EMOJI_RANGES)


def _static_text(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


def _js_modules() -> list[Path]:
    modules = [p for p in _first_party_files() if p.suffix == ".js"]
    assert len(modules) >= _MIN_JS_MODULES, f"found only {len(modules)} first-party JS files: {modules}"
    return modules


def _app_js() -> str:
    """All first-party JavaScript, one module after another. Source checks
    that used to read the single app.js read this, so they keep holding
    wherever a function lives after the module split."""
    return "\n".join(p.read_text(encoding="utf-8") for p in _js_modules())


def _skip_js_string_or_comment(src: str, i: int) -> int:
    """If ``src[i]`` opens a string, template or comment, return the index
    just past it; otherwise return ``i`` unchanged."""
    ch = src[i]
    if ch in "\"'`":
        j = i + 1
        while j < len(src) and src[j] != ch:
            j += 2 if src[j] == "\\" else 1
        return j + 1
    if src.startswith("//", i):
        end = src.find("\n", i)
        return len(src) if end == -1 else end
    if src.startswith("/*", i):
        end = src.find("*/", i + 2)
        return len(src) if end == -1 else end + 2
    return i


def _balanced_end(src: str, open_index: int) -> int:
    """Index just past the bracket that closes ``src[open_index]``,
    skipping strings and comments."""
    pairs = {"{": "}", "(": ")", "[": "]"}
    stack = []
    i = open_index
    while i < len(src):
        skipped = _skip_js_string_or_comment(src, i)
        if skipped != i:
            i = skipped
            continue
        ch = src[i]
        if ch in pairs:
            stack.append(pairs[ch])
        elif stack and ch == stack[-1]:
            stack.pop()
            if not stack:
                return i + 1
        i += 1
    raise AssertionError(f"unbalanced bracket opened at {open_index}")


def _function_source(app_js: str, name: str) -> str:
    """The full source of ``function NAME(...) {...}``, at any indent, in
    any first-party module: from the ``function`` keyword to its matching
    closing brace."""
    match = re.search(r"(?<![\w$.])function\s+" + re.escape(name) + r"\s*\(", app_js)
    assert match, f"no function {name}() in the dashboard's modules"
    params_end = _balanced_end(app_js, match.end() - 1)
    body_start = app_js.index("{", params_end)
    return app_js[match.start() : _balanced_end(app_js, body_start)]


def _declaration_source(app_js: str, name: str) -> str:
    """The source of a top-level ``[export] var|let|const NAME = ...``
    object or array literal, from the keyword to its closing bracket."""
    match = re.search(r"(?:export\s+)?(?:var|let|const)\s+" + re.escape(name) + r"\s*=\s*", app_js)
    assert match, f"no declaration of {name} in the dashboard's modules"
    return app_js[match.start() : _balanced_end(app_js, match.end())]


# -- deliverable 1: file existence / index.html reference restriction ----


def _js_literal_to_json(src: str) -> object:
    """A JS object or array literal (double-quoted strings, bare keys,
    trailing commas) as Python data."""

    def quote_key(match: re.Match) -> str:
        return match.group(0) if match.group(1) is None else '"' + match.group(1) + '":'

    text = re.sub(r'"(?:[^"\\]|\\.)*"|([A-Za-z_]\w*)\s*:', quote_key, src)
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    return json.loads(text)


def _pages() -> list[dict]:
    """links.js's PAGES: the sidebar's pages and their segments."""
    source = _declaration_source(_app_js(), "PAGES")
    return _js_literal_to_json(source[source.index("[") :].rstrip().rstrip(";"))


def _view_keys() -> list[str]:
    """Every view key ("spend/usage", or a page id alone), in sidebar order."""
    keys = []
    for page in _pages():
        if page.get("segments"):
            keys.extend(page["id"] + "/" + segment["id"] for segment in page["segments"])
        else:
            keys.append(page["id"])
    return keys


def _view_labels() -> list[str]:
    """Each view as the reader sees it: "Spend \u203a Usage" (viewLabel)."""
    labels = []
    for page in _pages():
        if page.get("segments"):
            labels.extend(page["label"] + " \u203a " + segment["label"] for segment in page["segments"])
        else:
            labels.append(page["label"])
    return labels


def _js_string_map(app_js: str, var_name: str) -> dict[str, str]:
    """A declaration's "key": "value" pairs, keys bare or quoted, dotted
    or slashed."""
    source = _declaration_source(app_js, var_name)
    return dict(re.findall(r'^\s*"?([a-z_./]+)"?\s*:\s*"([^"]*)"', source, re.MULTILINE))


def test_all_three_static_files_exist() -> None:
    for name in STATIC_FILES:
        path = STATIC_DIR / name
        assert path.is_file(), f"missing {path}"


def test_first_party_files_hold_no_control_characters() -> None:
    """A tab, newline or carriage return is text; any other C0 control
    character (a backspace from a mangled regex escape, a NUL) is a
    broken edit that a browser reads without complaint."""
    for path in FIRST_PARTY_FILES:
        text = (STATIC_DIR / path).read_text(encoding="utf-8")
        bad = sorted({hex(ord(ch)) for ch in text if ord(ch) < 32 and ch not in "\t\n\r"})
        assert not bad, (path, bad)


def test_first_party_glob_finds_every_module() -> None:
    assert set(STATIC_FILES) <= set(FIRST_PARTY_FILES), FIRST_PARTY_FILES
    assert len(_js_modules()) >= _MIN_JS_MODULES


def test_index_html_references_only_its_own_static_assets() -> None:
    """Every ``href``/``src`` in index.html is a same-origin ``/static/``
    file that exists on disk or an in-page ``#/`` route to a page that
    exists, and a preloaded font carries ``crossorigin`` (fonts are
    fetched in CORS mode, so a preload without it is fetched twice)."""
    html = _static_text("index.html")
    referenced = set(re.findall(r'(?:href|src)="([^"]+)"', html))
    assert {"/static/app.css", "/static/app.js"} <= referenced, referenced
    page_ids = {page["id"] for page in _pages()}
    for ref in referenced:
        if ref.startswith("#/"):
            assert ref[2:].split("/")[0].split("?")[0] in page_ids, f"index.html links to unknown page {ref!r}"
            continue
        assert ref.startswith("/static/"), f"index.html references {ref!r}, outside /static/"
        assert (STATIC_DIR / ref[len("/static/") :]).is_file(), f"index.html references missing file {ref!r}"
    for tag in re.findall(r"<link\b[^>]*>", html):
        if 'rel="preload"' in tag and 'as="font"' in tag:
            assert "crossorigin" in tag, f"font preload without crossorigin: {tag}"


def test_index_html_loads_app_js_as_an_es_module() -> None:
    """The dashboard is native ES modules with no build step: app.js is
    the one entry point, loaded as a module, and imports the rest."""
    html = _static_text("index.html")
    assert '<script type="module" src="/static/app.js"></script>' in html


def test_theme_is_set_before_the_first_paint() -> None:
    """A module script is deferred, so the saved theme is applied by a
    classic script in <head>, ahead of the stylesheet; otherwise a
    viewer who picked dark sees a light flash on every load."""
    head = _static_text("index.html").split("</head>")[0]
    boot = head.find('<script src="/static/theme-boot.js"></script>')
    assert boot != -1, "theme-boot.js must be a classic <script> in <head>"
    assert boot < head.find('<link rel="stylesheet"')
    assert "tls:theme" in _static_text("theme-boot.js")
    assert 'setAttribute("data-theme"' in _static_text("theme-boot.js")


# -- deliverable 1: forbidden-substring / no-emoji scans -----------------


@pytest.mark.parametrize("name", FIRST_PARTY_FILES)
def test_no_forbidden_substrings(name: str) -> None:
    text = _static_text(name)
    for label, pattern in _FORBIDDEN_SUBSTRING_PATTERNS.items():
        matches = pattern.findall(text)
        assert not matches, f"{name} contains forbidden pattern ({label}): {matches!r}"


@pytest.mark.parametrize("name", FIRST_PARTY_FILES)
def test_no_inline_script_bodies(name: str) -> None:
    """Every ``<script ...>`` tag in the first-party files must carry a
    ``src=`` attribute -- i.e. it loads an external (same-origin) file
    rather than running an inline body, per the CSP's ``script-src
    'self'`` and the brief's "no inline <script>" constraint."""
    text = _static_text(name)
    for tag in re.findall(r"<script\b[^>]*>", text, flags=re.IGNORECASE):
        assert "src=" in tag.lower(), f"{name} has a <script> tag without src=: {tag!r}"


def test_no_button_label_or_handler_says_apply() -> None:
    """UX-8/F2: the "No Apply button" hard constraint -- the dashboard
    only ever offers a prompt or a ``claudeglass ... --dry-run``
    command; it never claims to apply a Claude Code config change
    itself (the one documented exception, the Capture page writing
    ``[capture]`` into this tool's own config.toml, is a settings
    toggle, not a button labelled "Apply"). Regression test for "Apply
    it to:"/"Apply tags" (now "Target file:"/"Save tags") and the latent
    ``data.apply_command`` fallback (both since removed from
    the dashboard): no button's visible text may start with the word
    "Apply", and no JS identifier naming a button or its click handler
    may combine "apply" with "btn"/"button"/"handler". Prose that
    explains the CLI's own ``apply`` subcommand (e.g. "you then apply it
    with the ... command it shows") is unaffected -- only labels and
    handler/variable names are checked.
    """
    app_js = _app_js()
    index_html = _static_text("index.html")

    button_labels = re.findall(r'el\("button",\s*\{[\s\S]*?text:\s*"([^"]*)"', app_js)
    button_labels += re.findall(r'\bbutton\(\s*"([^"]*)"', app_js)
    button_labels += re.findall(r"<button\b[^>]*>([^<]*)</button>", index_html)
    assert len(button_labels) >= 20, button_labels  # the scan sees the button() helper's calls
    offenders = [label for label in button_labels if re.match(r"(?i)^apply\b", label)]
    assert not offenders, f"a button label starts with \"Apply\": {offenders!r}"

    handler_names = re.findall(r"\b([A-Za-z_$][\w$]*)\b", app_js)
    apply_handlers = [
        name
        for name in set(handler_names)
        if re.search(r"(?i)apply", name) and re.search(r"(?i)btn|button|handler", name)
    ]
    assert not apply_handlers, f"the dashboard has an apply-named button/handler identifier: {apply_handlers!r}"

    assert "apply_command" not in app_js, "the dashboard must not read the removed data.apply_command fallback"

    # The ui.js helper every new button goes through refuses the label
    # outright, so a label built at run time can't slip past the scan.
    helper = _function_source(app_js, "button")
    assert "/^\\s*apply\\b/i.test(label" in helper, helper
    assert "throw new Error" in helper


def test_data_grid_keeps_each_tables_sort() -> None:
    """Phase 4: a sort the reader picks is stored per table
    (tls:sort:<table>) and read back when the grid draws again, so it
    survives a window change or a reload. Before, it was written but
    never read."""
    grid = _function_source(_app_js(), "dataGrid")
    read = 'readJson("tls:sort:" + gridId)'
    write = 'storageSet("tls:sort:" + gridId, JSON.stringify(sort))'
    assert read in grid, "dataGrid never reads the stored sort"
    assert write in grid, "dataGrid never stores the sort"
    assert grid.index(read) < grid.index(write)


def test_long_report_tables_open_on_their_first_rows() -> None:
    """Phase 9: a report table of more than REPORT_ROWS + 2 rows draws
    its first REPORT_ROWS (after the sort, so a sorted table shows its
    top rows) and a "Show all N rows" button; an evidence link to a
    later row shows them all before it pulses (gridScrollTo, which
    pulseRow calls). Savings ran to 5,900px on four 15-20 row tables."""
    source = _app_js()
    grid = _function_source(source, "dataGrid")
    assert "var size = spec.limit;" in _function_source(grid, "foldSize")
    assert "var size = foldSize();" in _function_source(grid, "drawBody")
    assert "orderedRows.slice(0, size)" in grid
    assert '"Show all " + rows.length + " rows"' in grid
    assert 'moreButton.setAttribute("aria-expanded", "false")' in grid
    limited = grid.index("if (limited) {")
    assert "table.gridScrollTo = function (rowKey)" in grid[limited:]
    assert "if (!expanded) setExpanded(true);" in grid[limited:]
    # The capped table is short: it only scrolls in its own box once all
    # its rows show (a 46-row table clipped its tenth row).
    assert "var tall = (folded() ? foldSize() : rows.length) > TALL_ROWS;" in grid
    assert "fitBox();\n    drawBody();" in grid
    table = _function_source(source, "renderTable")
    # row_groups and row_kinds arrive as {} when a table has neither.
    assert "limit: hasKeys(table.row_groups) || hasKeys(table.row_kinds) || (options && options.allRows) ? 0 : REPORT_ROWS" in table
    # Data quality's checks list stays whole: a problem row must not hide
    # behind the button.
    assert "allRows: true" in _function_source(source, "renderDataQuality")
    pulse = _function_source(source, "pulseRow")
    assert 'typeof table.gridScrollTo === "function"' in pulse


def test_dated_report_tables_fold_to_their_latest_rows() -> None:
    """Phase 9 review: usage.py lists by_day, by_week, by_month and
    five_hour_blocks oldest first, so a capped Usage by day opened on the
    oldest days. NEWEST_LAST tables fold to their last rows until sorted;
    every name is a real table."""
    from claudeglass.helptext import TABLE_COPY

    source = _app_js()
    match = re.search(r"export var NEWEST_LAST = \{([^}]*)\};", source)
    assert match, "grid.js declares NEWEST_LAST"
    names = re.findall(r"(\w+): true", match.group(1))
    assert set(names) == {"by_day", "by_week", "by_month", "five_hour_blocks"}
    assert all(name in TABLE_COPY for name in names)
    grid = _function_source(source, "dataGrid")
    assert "fromEnd() ? orderedRows.slice(-size)" in grid
    assert 'return spec.limitFrom === "end" && !sort;' in grid
    assert 'limitFrom: NEWEST_LAST[table.name] ? "end" : "start"' in _function_source(source, "renderTable")


def test_daily_spend_split_outlives_a_window_change_and_a_visit() -> None:
    """Phase 9 review: a window change kept only ?id=, and coming back
    from Savings called the params handler with {}, so the chart fell
    back to the agent split. The split last picked is kept, and an
    address without one keeps the split on screen."""
    source = _app_js()
    usage = _function_source(source, "renderUsage")
    assert "usageSplit(state.params.split || chosenSplit)" in usage
    assert "chosenSplit = split;" in usage
    assert "if (params.split && usageSplit(params.split) !== split) chooseSplit(params.split);" in usage
    assert "else keepSplitInAddress();" in usage


def test_cache_tiles_count_what_they_cost() -> None:
    """Phase 9 review: the Cache page's saving subtracts the write
    premium, so it is named apart from the Overview's "Saved by the
    cache"; the avoidable-rebuild count leaves out rebuilds after a
    usage-limit pause, as avoidable_cost_usd does."""
    source = _app_js()
    explainer = _function_source(source, "renderCacheExplainer")
    assert 'label: "Saved by the cache after write costs"' in explainer
    # Phase 10 review: the count moved to costs.js's avoidableRebuilds,
    # shared with Glossary's rebuilds card (test below).
    count = _function_source(source, "avoidableRebuilds")
    assert '"recache_signature_split"' in count
    assert 'row[signature] === "limit-expiry" ? sum' in count
    assert "var times = avoidableRebuilds(report);" in explainer
    # Design audit P1-4: the note named 431 beside a tile of 457. It now
    # says the avoidable count is part of every rebuild, the tile's
    # recache_turns, and never gives recache_turns as the count itself.
    assert "avoidableSentence(times, isFinite(total) ? total : times)" in explainer
    assert "var total = Number(rebuilds.recache_turns);" in explainer
    assert "The cache was rebuilt" not in explainer
    # The Overview's figure is before the cache writes; Cache's is after,
    # so each label says which (design review: 1197.3% beside 1147.1%).
    assert 'moneyTile("Saved by cache reads",' in source
    assert "before paying for the cache writes" in source
    # Phase 10 review: "...where reading it would have cost a tenth of."
    assert 'readWords + " the input price."' in explainer


def test_glossary_rebuilds_card_counts_what_its_cost_covers() -> None:
    """Phase 10 review: Glossary said "456 replies rebuilt the cache, at
    about $506" while Cache > Rebuilds said "rebuilt 430 times" for the
    same $506: recache_turns counts rebuilds after a usage-limit pause,
    which avoidable_cost_usd leaves out. Both count with one helper."""
    source = _app_js()
    cards = _declaration_source(source, "CARD_NUMBERS")
    start = cards.index('"cache-rebuilds": function')
    rebuilds = cards[start : _balanced_end(cards, cards.index("{", start))]
    assert "var times = avoidableRebuilds(ctx.report);" in rebuilds
    assert "thousands(times)" in rebuilds
    assert "thousands(stats.recache_turns)" not in rebuilds
    assert "stats.avoidable_cost_usd" in rebuilds
    assert "Rebuilds after a usage-limit pause aren't counted." in rebuilds
    assert "import { avoidableRebuilds, cardRuleText, pricingFacts } from \"./costs.js\";" in _static_text("page-glossary.js")
    assert "import { avoidableRebuilds } from \"./costs.js\";" in _static_text("page-cache.js")


def test_glossary_billing_card_says_when_amounts_are_list_price() -> None:
    """Phase 10 review: on a plan with no usage-limit readings
    (share_per_usd null) every amount reads "$... list-price equivalent",
    but the Billing mode card said amounts show as a share of the weekly
    limit. It follows format.js's money(): no share, no share wording."""
    cards = _declaration_source(_app_js(), "CARD_NUMBERS")
    start = cards.index('"billing-mode": function')
    billing = cards[start : _balanced_end(cards, cards.index("{", start))]
    null_check = "units.share_per_usd === null || units.share_per_usd === undefined"
    assert null_check in billing
    assert "sharePerUsd === null || sharePerUsd === undefined" in _function_source(_app_js(), "money")
    branch = billing[billing.index(null_check) :]
    assert "list-price equivalents until your usage-limit readings are logged" in branch.split("}", 1)[0]


def test_capture_group_opens_for_a_metric_that_needs_you() -> None:
    """Phase 10 review: a Capture group opened for a missing hook entry or
    install, but not for a status-line note, so "this line won't show"
    sat in a closed group."""
    metrics = _function_source(_app_js(), "renderCaptureMetrics")
    assert "group.open = section.metrics.some(" in metrics
    assert "return row.needs_hook || row.needs_install || row.statusline_note;" in metrics
    # The note it opens for is the one each row shows.
    assert "if (row.statusline_note) box.appendChild(" in _function_source(_app_js(), "renderMetricRow")


def test_a_change_marker_leads_to_its_change_on_your_changes() -> None:
    """Phase 10 review (fedd805 gaps): a change marker on a daily spend
    chart, or on Your changes' timeline, opens Your changes with ?day=,
    which pulses that day's card; the marker's label is a link the
    keyboard reaches too."""
    source = _app_js()
    changes = _function_source(source, "dailyChanges")
    assert 'goTo("changes", { params: { day: day } })' in changes
    assert 'String(change.ts || "").slice(0, 10)' in changes
    assert 'goTo("changes", { params: { day: day } })' in _function_source(source, "timelineChanges")
    page = _function_source(source, "renderChanges")
    assert 'onParams("changes", function (params)' in page
    assert r'if (!/^\d{4}-\d\d-\d\d$/.test(params.day || "")) return;' in page
    assert "cardsDrawn.then(" in page
    assert ".change-card[data-day=\"' + params.day + '\"]" in page
    assert 'pulseNode(card, "block-target")' in page
    assert '"data-day": String(change.ts || "").slice(0, 10)' in _function_source(source, "changeCard")
    # The timeline's own change labels are links the keyboard reaches.
    steps = _function_source(source, "changeSteps")
    assert 'ctx.layer("rule-links", { links: true })' in steps
    for attr in ('.attr("tabindex", 0)', '.attr("role", "link")', ': see what it did"', '.on("click", lead.open)'):
        assert attr in steps, attr
    # Keyboard: a Tab stop named for what it opens; Enter or Space opens it.
    columns = _function_source(source, "stackedColumns")
    assert 'ctx.layer("rule-links", { links: true })' in columns
    for attr in ('.attr("tabindex", 0)', '.attr("role", "link")', '.attr("aria-label", '):
        assert attr in columns, attr
    assert ': see what it did"' in columns
    assert '.on("click", change.open)' in columns
    keydown = columns[columns.index('.on("keydown"') :]
    assert 'event.key !== "Enter" && event.key !== " "' in keydown
    assert "change.open();" in keydown
    # The drawing stays hidden from screen readers, layer by layer, all
    # but a layer of links; the svg itself isn't hidden, so they keep
    # their names.
    context = _function_source(source, "drawContext")
    assert '.attr("role", "none")' in context and '"aria-hidden", "true").attr("focusable"' not in context
    assert 'if (!(layerOpts && layerOpts.links)) g.attr("aria-hidden", "true");' in context
    # Every focusable element gets the ring; nothing takes it off an SVG label.
    css = _static_text("app.css")
    assert re.search(r"(?m)^:focus-visible\s*\{[^}]*outline: 2px solid var\(--focus\)", css)
    assert re.search(r"\.chart-rule-label\.is-openable:focus-visible\s*\{[^}]*outline: 2px solid var\(--focus\)", css)


def test_profile_copy_names_a_section_not_a_direction() -> None:
    """Phase 10 review: "Edit it below" (inside a drawer, with the editor
    behind it) and "in the list above" (the list is below Create a
    profile) pointed the wrong way. Each names its section."""
    source = _app_js()
    diff = _function_source(source, "renderProfileDiff")
    assert "open Edit settings directly on this page" in diff and "below" not in diff
    draft = _function_source(source, "renderGoalDraft")
    assert "It's under Your profiles and the built-in ones" in draft and "list above" not in draft
    assert '"Edit settings directly"' in _function_source(source, "renderProfiles")
    assert '"Your profiles and the built-in ones"' in _function_source(source, "renderProfiles")


def test_kind_of_task_reads_by_its_plain_name() -> None:
    """Phase 10 review: the Kind of task picker showed "bugfix" and named
    the profile "bugfix tasks". The draft carries each task's plain name
    (task_labels, from capture_catalogue.TASK_LABELS); the value sent
    back stays the task word."""
    source = _app_js()
    assert "draft.task_labels && draft.task_labels[task]" in _function_source(source, "taskName")
    draft = _function_source(source, "renderGoalDraft")
    assert 'el("option", { value: task, text: taskName(draft, task),' in draft
    assert 'taskName(draft, draft.task) + " tasks"' in draft
    assert "for: draft.task ? [draft.task] : []" in draft


def test_show_all_sessions_hands_focus_to_the_list() -> None:
    """Phase 9 review: focus went to a <table> with no tabindex, so it
    fell to <body>. The grid's scroller takes focus (tabIndex -1)."""
    source = _app_js()
    sessions = _function_source(source, "renderSessions")
    assert 'tableContainer.querySelector(".grid-scroll")' in sessions
    assert "tabIndex: -1" in _function_source(source, "dataGrid")


def test_session_scatter_picks_a_range_from_the_keyboard() -> None:
    """Phase 9 review: d3.brushX is pointer-only. Shift with the arrow
    keys picks the sessions from where the cursor started; the scatter
    hands the frame a pick(from, to) that moves the brush and filters."""
    source = _app_js()
    keys = _function_source(source, "wireKeys")
    assert "event.shiftKey && frame.pickRange" in keys
    assert "frame.pickRange(frame.anchor, next);" in keys
    scatter = _function_source(source, "scatter")
    assert "pick: function (from, to)" in scatter
    assert "brushLayer.call(brush.move," in scatter
    assert "hold Shift and press the arrow keys" in scatter


def test_a_money_row_in_a_mixed_column_carries_its_unit() -> None:
    """Phase 10: Workflow runs lists counts and amounts in one Value
    column, so no header can hold the unit; "Total cost" read "208". A
    row whose kind is money is written in the billing mode (moneyText),
    while a money column stays a plain number under its header's unit."""
    cell = _function_source(_app_js(), "cellContent")
    assert 'if (rowKind === "money" && kind === "money" && column.index > 0 && typeof value === "number") return moneyText(value);' in cell


def test_amount_rewrite_keeps_row_keys_and_their_labels_in_step() -> None:
    """Phase 10: readableAmounts turns "Total cost (USD)" into "Total
    cost ($)" in a row's cell; a table's value_labels and row_kinds are
    keyed on the same text, so their keys are rewritten too. Before, the
    Workflow runs table showed "Total cost ($)" unlabelled and its money
    rows as plain numbers."""
    source = _function_source(_app_js(), "readableAmounts")
    assert "var readable = readableAmounts(key);" in source
    assert "if (readable !== key) delete value[key];" in source
    assert "value[readable] = item;" in source


def test_command_block_shows_every_explainer_line() -> None:
    """Phase 4: a command block carries what changes, where, the
    trade-off and how to undo it -- the explainer fixes.build_fix
    writes (test_spawn_parts and test_fixes pin its headings). The block
    renders every pair as a definition list, whatever the headings, so
    none is dropped on the way to the page. Phase 8: each answer goes
    through prose(), so a page link in it is a link."""
    block = _function_source(_app_js(), "commandBlock")
    assert "fix.explainer.forEach(function (pair)" in block, block
    assert 'el("dt", { text: pair[0] })' in block
    assert 'el("dd", null, prose(pair[1]))' in block
    assert 'class: "fix-explainer"' in block


def test_habits_playbook_caps_featured_cards_and_collapses_the_rest() -> None:
    """UX-4/7 (F3: "uncapped playbook"): only the top
    ``PLAYBOOK_CARD_LIMIT`` habits get a card outright; the rest render
    into a collapsed ``<details>`` so the tab isn't a wall of cards down
    to the least useful habit. Regression test for
    ``renderHabitsPlaybook``/``appendHabitCards`` in page-habits.js."""
    app_js = _app_js()
    limit_match = re.search(r"(?:export\s+)?(?:var|let|const) PLAYBOOK_CARD_LIMIT = (\d+);", app_js)
    assert limit_match, "the dashboard no longer defines PLAYBOOK_CARD_LIMIT"
    assert int(limit_match.group(1)) == 5

    body = _function_source(app_js, "renderHabitsPlaybook")
    assert "PLAYBOOK_CARD_LIMIT" in body
    assert '"details"' in body and "more habit" in body, "the rest of the playbook must collapse into a <details>"
    assert "appendHabitCards" in body


def test_habits_digest_money_cards_follow_the_billing_mode() -> None:
    """UX-1 (F1): the Work habits digest's money cards go through
    ``moneyParts()`` (the ``Units.money`` mirror), not a bare "X USD" from
    ``formatCell``, so a Pro or Max plan sees a weekly-limit share or a
    list-price equivalent instead of plain dollars -- at a tile's size,
    its unit on the line under the figure, not the whole phrase as the
    headline."""
    app_js = _app_js()
    body = _function_source(app_js, "renderHabitsDigest")
    assert 'kind === "money" ? moneyParts(' in body
    assert "unit: amount ? amount.unit || null : null" in body and "amount.secondary" in body
    # The habits the playbook shows as cards aren't repeated above them.
    assert "carded" in body and "if (rows.length && !own.length) return;" in body


def test_a_profile_estimate_scales_by_its_normalised_tasks() -> None:
    """F11: renderProfileEstimate sends the profile's ``tasks`` (its
    ``for`` words normalised to the task vocabulary), never a raw ``for``
    word such as "implementation", which /api/whatif rejects."""
    app_js = _app_js()
    body = _function_source(app_js, "renderProfileEstimate")
    assert "p.tasks" in body and "p.for" not in body


@pytest.mark.parametrize("name", FIRST_PARTY_FILES)
def test_no_emoji_code_points(name: str) -> None:
    text = _static_text(name)
    offenders = [ch for ch in text if _is_emoji_code_point(ord(ch))]
    assert not offenders, f"{name} contains emoji-range code point(s): {[hex(ord(c)) for c in offenders]}"


# -- deliverable 1: app.js sanity + package-data registration -----------


@pytest.mark.parametrize("name", [n for n in FIRST_PARTY_FILES if n.endswith(".js")])
def test_app_js_has_balanced_braces(name: str) -> None:
    text = _static_text(name)
    # Braces inside string/regex literals or comments could in principle
    # throw this simple counter off, but a genuinely broken brace count
    # is exactly the failure mode this smoke check exists to catch, and
    # `node --check` (run manually during development, not a repo
    # dependency here) already validates full syntax.
    assert text.count("{") == text.count("}"), f"{name} has unbalanced {{ }}"
    assert text.count("(") == text.count(")"), f"{name} has unbalanced ( )"
    assert text.count("[") == text.count("]"), f"{name} has unbalanced [ ]"


def _js_code_only(src: str) -> str:
    """``src`` with comments removed and every string or template emptied,
    so a name only counts where it is real code."""
    out = []
    i = 0
    while i < len(src):
        skipped = _skip_js_string_or_comment(src, i)
        if skipped != i:
            if src[i] in "\"'`":
                out.append(src[i] * 2)
            i = skipped
            continue
        out.append(src[i])
        i += 1
    return "".join(out)


_IMPORT_RE = re.compile(r'^import\s*\{([^}]*)\}\s*from\s*"\./([\w-]+\.js)";', re.M)
_EXPORT_RE = re.compile(r"^export\s+(?:function|var|let|const)\s+([A-Za-z_$][\w$]*)", re.M)
_DECLARED_RE = re.compile(r"(?:\b(?:var|let|const|function)\s+|\bcatch\s*\()([A-Za-z_$][\w$]*)")
_PARAMS_RE = re.compile(r"\bfunction\b[^(]*\(([^)]*)\)")


def test_es_modules_import_what_they_use_and_never_import_in_a_cycle() -> None:
    """A module that uses another module's function without importing it
    only fails when that code path runs, so check it here: every name a
    module uses from another module is imported, every import names a
    real export, and the import graph has no cycles (a cycle can leave a
    ``var`` undefined while the modules load)."""
    modules = {p.name: p.read_text(encoding="utf-8") for p in _js_modules()}
    exports = {name: set(_EXPORT_RE.findall(text)) for name, text in modules.items()}
    graph = {}
    for name, text in modules.items():
        imported = set()
        graph[name] = set()
        for names, target in _IMPORT_RE.findall(text):
            assert target in modules, f"{name} imports missing module {target}"
            graph[name].add(target)
            for item in (n.strip() for n in names.split(",")):
                if not item:
                    continue
                assert item in exports[target], f"{name} imports {item}, which {target} does not export"
                imported.add(item)
        code = _js_code_only(text)
        local = set(_DECLARED_RE.findall(code))
        for params in _PARAMS_RE.findall(code):
            local.update(p.strip() for p in params.split(",") if p.strip())
        for other, names in exports.items():
            if other == name:
                continue
            for used in names - imported - local:
                if re.search(r"(?<![\w$.])" + re.escape(used) + r"(?![\w$])(?!\s*:)", code):
                    raise AssertionError(f"{name} uses {used} from {other} without importing it")

    def visit(node: str, path: list[str]) -> None:
        for nxt in graph[node]:
            assert nxt not in path, "import cycle: " + " -> ".join([*path, nxt])
            visit(nxt, [*path, nxt])

    for name in graph:
        visit(name, [name])


def test_every_import_names_a_file_that_exists() -> None:
    """The named-import check above skips a side-effect import
    (``import "./x.js"``) and a default import (``import d3 from``);
    those still have to name a file that is really there."""
    for path in _js_modules():
        for target in re.findall(r'^import\s+(?:[^"\';]*?\s+from\s+)?"(\./[^"]+)";', path.read_text(encoding="utf-8"), re.M):
            assert (STATIC_DIR / target[2:]).is_file(), f"{path.name} imports missing {target}"


def test_app_js_restart_note_matches_fixes() -> None:
    """The dashboard's reminder to restart Claude Code is the same text
    the CLI and the reports print (fixes.RESTART_NOTE)."""
    import re

    from claudeglass.fixes import RESTART_NOTE

    match = re.search(r"(?:export\s+)?(?:var|let|const) RESTART_NOTE =((?:\s*\"[^\"]*\"\s*\+?)+);", _app_js())
    assert match, "the dashboard no longer defines RESTART_NOTE"
    assert "".join(re.findall(r'"([^"]*)"', match.group(1))) == RESTART_NOTE


def test_pyproject_declares_static_as_package_data() -> None:
    data = tomllib.loads(PYPROJECT_TOML.read_text(encoding="utf-8"))
    package_data = data["tool"]["setuptools"]["package-data"]["claudeglass"]
    assert any("service/static" in entry for entry in package_data), package_data


def _documented_get_routes() -> list[str]:
    text = API_MD.read_text(encoding="utf-8")
    # Only the route headings are the contract; prose may mention a
    # route family in shorthand (e.g. "`GET /api/report.*`").
    routes = re.findall(r"^### `GET (/api/[^`]+)`", text, flags=re.M)
    assert routes, "no GET routes found in docs/api.md -- has its format changed?"
    return [r for r in routes if r not in _EXCLUDED_ROUTE_PREFIXES]


def test_every_documented_get_route_is_fetched_by_the_dashboard() -> None:
    app_js = _app_js()
    routes = [r for r in _documented_get_routes() if r not in _NOT_FETCHED_BY_DASHBOARD]
    assert routes  # sanity: the exclusion list didn't eat everything
    for route in routes:
        # Routes with a path parameter (e.g. "/api/session/<id>") are
        # built up via string concatenation in the dashboard's modules, not present
        # verbatim -- match on the literal prefix before "<" instead.
        prefix = route.split("<")[0]
        assert prefix in app_js, f"the dashboard never fetches documented route {route!r} (looked for prefix {prefix!r})"


# -- deliverable 3: fixture HTTP server -----------------------------------

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript",
    ".css": "text/css; charset=utf-8",
}


class _FixtureHandler(http.server.BaseHTTPRequestHandler):
    """Serves the static directory plus canned ``/api/*`` JSON built by
    ``_build_fixture_data`` -- everything precomputed on the main thread
    before the server starts, so request handling never touches the
    ``Store`` (and its per-thread ``:memory:`` connections) at all."""

    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
        pass  # keep test output quiet

    def do_GET(self) -> None:  # noqa: N802 - stdlib method name
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if path == "/":
            self._serve_static("index.html")
            return
        if path.startswith("/static/"):
            self._serve_static(path[len("/static/") :])
            return
        if path.startswith("/api/session/"):
            session_id = path[len("/api/session/") :]
            data = self.server.fixture_sessions.get(session_id)  # type: ignore[attr-defined]
            if data is None:
                self._send_json(404, {"ok": False, "error": {"code": "not_found", "message": "session not found"}})
            else:
                self._send_json(200, {"ok": True, "data": data})
            return
        if path.startswith("/api/profiles/") and path.endswith("/diff"):
            self._send_json(501, {"ok": False, "error": {"code": "not_implemented", "message": "profile diff ships in v0.3"}})
            return
        canned = self.server.fixture_canned.get(path)  # type: ignore[attr-defined]
        if canned is None:
            self._send_json(404, {"ok": False, "error": {"code": "not_found", "message": "not found"}})
            return
        if path == "/api/report.json":
            # Review finding 1 (blocking): docs/api.md deliberately keeps
            # report.json unwrapped ({"schema_version": ..., "report":
            # {...}}, no {"ok": ..., "data": ...} envelope) for CLI byte
            # parity -- serve it raw here too, matching api.py's real
            # route, instead of wrapping it like every other canned route.
            self._send_json(200, canned)
            return
        self._send_json(200, {"ok": True, "data": canned})

    def _serve_static(self, name: str) -> None:
        file_path = (STATIC_DIR / name).resolve()
        if STATIC_DIR.resolve() not in file_path.parents or not file_path.is_file():
            self._send_json(404, {"ok": False, "error": {"code": "not_found", "message": "no such file"}})
            return
        content_type = _CONTENT_TYPES.get(file_path.suffix, "application/octet-stream")
        body = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _seed_store() -> Store:
    store = Store(":memory:")
    store.open()
    snapshot_id = store.upsert_snapshot(
        project_slug="proj-ui",
        project_root_path="proj-ui",
        ts="2026-09-18T12:00:00Z",
        schema_version=store.schema_version() or 1,
        digest_json=json.dumps({"billing": "api"}),
    )
    store.upsert_session(
        session_id="session-ui-1",
        project_slug="proj-ui",
        project_root_path="proj-ui",
        slug="proj-ui",
        first_ts="2026-09-18T12:00:00Z",
        last_ts="2026-09-18T13:00:00Z",
        span_s=3600.0,
        archetype="plan-high-implement-low",
        mode="agentic",
        mode_source="tool-signature",
        purpose="refactor",
        purpose_source="intent-signature",
        entrypoint="cli",
        billing_mode="subscription",
        snapshot_id=snapshot_id,
        profile_id="p1",
        total_cost=1.23,
        total_tokens=45000,
    )
    store.upsert_transcript(
        session_id="session-ui-1",
        path="fixture-top.jsonl",
        kind="top-level",
        agent_id=None,
        agent_type=None,
        spawn_depth=0,
        parent_agent_id=None,
        mtime_ns=1,
        size_bytes=2,
        parser_version=4,
        digest_json=json.dumps({"turns": 4}),
        turns_agg=[
            {
                "day": "2026-09-18",
                "model": "claude-sonnet-5",
                "turns": 4,
                "input_tokens": 400,
                "cache_creation_tokens": 4000,
                "cache_read_tokens": 2000,
                "output_tokens": 120,
                "thinking_tokens": 20,
                "cc_5m": 0,
                "cc_1h": 4000,
                "cost": 1.23,
            }
        ],
        recache_turns=[
            {
                "turn_index": 2,
                "signature": "full-expiry",
                "cache_creation_tokens": 4000,
                "preceding_primary": "HUMAN_TEXT",
                "gap_s": 400.0,
            }
        ],
        events=[{"kind": "compact_boundary", "subkind": None, "count": 1, "dropped_tokens_sum": 0, "duration_ms_sum": 0}],
        compactions=[
            {
                "ts": "2026-09-18T12:30:00Z",
                "pre_tokens": 150000,
                "post_tokens": 30000,
                "dropped_tokens": 120000,
                "trigger": "auto",
                "join_delta_s": 4.0,
            }
        ],
    )
    store.upsert_profile(profile_id="p1", name="ui-fixture-profile", toml_path="p1.toml")
    store.record_baseline(
        project_slug="proj-ui",
        project_root_path="proj-ui",
        window_start="2026-09-11T00:00:00Z",
        window_end="2026-09-18T00:00:00Z",
        archetype="plan-high-implement-low",
        digest_json=json.dumps({"sessions": 1}),
    )
    store.set_tag("session-ui-1", "purpose", "refactor-override")
    return store


def _build_fixture_data(tmp_path: Path) -> tuple[dict, dict]:
    """Build the canned ``/api/*`` payloads: store-backed routes from a
    seeded ``Store``, report-backed routes from a synthetic JSONL corpus
    run through ``report.build_report`` and ``render/json_out``."""
    store = _seed_store()
    sessions_map = {"session-ui-1": store.session("session-ui-1")}
    canned: dict = {
        "/api/health": {
            "status": "ok",
            "version": "0.9.0",
            "schema_version": store.schema_version(),
            "watcher": {"finished_at": "2026-09-19T00:00:00Z", "files_parsed": 1, "errors": 0},
            "capture": {**capture_view.config_block(CaptureConfig()), "hooks_ok": None},
        },
        "/api/summary": store.summary(),
        "/api/daily-usage": store.daily_usage(split="agent"),
        "/api/sessions": store.sessions(),
        "/api/recache": store.recache(),
        "/api/compactions": store.compactions(),
        "/api/profiles": store.profiles(),
        "/api/baseline": store.baselines(),
    }
    store.close()

    project_dir = tmp_path / "proj-ui"
    project_dir.mkdir()
    write_jsonl(
        project_dir / "session-ui-1.jsonl",
        [
            turn_line(
                input_tokens=100 + i,
                output_tokens=20 + i,
                cache_creation_input_tokens=1000,
                cache_read_input_tokens=500,
            )
            for i in range(4)
        ],
    )
    corpus = load_corpus([project_dir])
    pricing = load_pricing()
    config = Config()
    snapshots = [Snapshot(path="fixture-snapshot.json", ts="2026-09-18T12:00:00.000Z", data={"billing": "api"})]
    report = build_report(
        corpus,
        pricing,
        config,
        projects=("proj-ui",),
        window="last 7 days",
        phases=True,
        snapshots=snapshots,
    )

    canned["/api/report.json"] = json.loads(render_json(report))

    ttl_section = next((s for s in report.sections if s.key == "ttl"), None)
    canned["/api/ttl"] = to_jsonable(ttl_section) if ttl_section is not None else {"tables": []}

    # v4 wiring round: same "report-backed section" canning as ttl above.
    for route, section_key in (
        ("/api/carry", "carry"),
        ("/api/compaction-sim", "compaction_sim"),
        ("/api/plan-handoff", "plan_handoff"),
        ("/api/model-swap", "model_swap"),
        ("/api/waste", "waste"),
    ):
        section = next((s for s in report.sections if s.key == section_key), None)
        canned[route] = to_jsonable(section) if section is not None else {"tables": []}

    config_section = next((s for s in report.sections if s.key == "config"), None)
    canned["/api/config-diff"] = to_jsonable(config_section) if config_section is not None else {"tables": []}

    canned["/api/recommendations"] = [to_jsonable(rec) for rec in report.recommendations]
    canned["/api/diagnostics"] = to_jsonable(helptext.diagnostics_table(report.diagnostics))
    canned["/api/profile-schema"] = {
        "settings": [{"key": key, "label": key, "kind": spec.kind} for key, spec in profile_schema.SETTINGS_ALLOWLIST.items()],
        "agents": [{"key": key, "label": key, "kind": spec.kind} for key, spec in profile_schema.AGENT_ALLOWLIST.items()],
        "env": sorted(profile_schema.ENV_ALLOWLIST),
        "archetypes": list(profile_schema.ARCHETYPES),
        "scopes": [{"key": "user", "label": "Your user settings, every project"}],
    }

    # Goal-first profiles, quick actions, context files, impact and setup:
    # built by the same modules the real routes call, over the same report.
    config_dir = tmp_path / "claude-config"
    config_dir.mkdir()
    units = report.units if report.units is not None else Units(billing_mode="api", currency="USD")
    period = "over the last 7 days"
    ctx = quick_actions.Context(
        model=report, units=units, period=period, config_dir=config_dir, effective={}, effective_agents={}
    )
    canned["/api/quick-actions"] = {"period": period, "checks": quick_actions.run_all(ctx)}
    canned["/api/profile-goals"] = {"goals": goals.goals_list()}
    canned["/api/skills"] = skills_review.review(config_dir, report.context_files or {}, units, period, projects=[])
    canned["/api/claude-md"] = {"period": period, "transcripts": 0, "files": []}
    canned["/api/impact"] = {"changes": [], "caveat": "", "min_sessions": 3, "lookback_days": 30}
    canned["/api/backtest"] = {"predictions": [], "judged_just_now": 0, "verdicts": list(backtest.VERDICTS)}
    canned["/api/setup"] = {
        "items": [item.as_dict() for item in footprint.inventory(config_dir, service_registered=False)],
        "expectations": [{"title": title, "text": text} for title, text in footprint.EXPECTATIONS],
        "uninstall_command": footprint.UNINSTALL_COMMAND,
    }
    canned["/api/capture"] = capture_view.view(CaptureConfig(), units=units)
    setup_items = setup_status.check_setup(config_dir, is_registered=lambda: False, running=True, url=None)
    canned["/api/setup/status"] = {
        "items": [setup_status.to_jsonable(item) for item in setup_items],
        "done": setup_status.done(setup_items),
        "needs_attention": len(setup_status.needs_attention(setup_items)),
        "verdict": setup_status.verdict(setup_items),
    }

    return canned, sessions_map


@pytest.fixture
def fixture_server(tmp_path: Path):
    canned, sessions_map = _build_fixture_data(tmp_path)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    server.fixture_canned = canned  # type: ignore[attr-defined]
    server.fixture_sessions = sessions_map  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base_url
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _get(base_url: str, path: str) -> tuple[int, str, bytes]:
    try:
        with urllib.request.urlopen(base_url + path, timeout=5) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read()


def test_fixture_server_serves_index_at_root(fixture_server: str) -> None:
    status, content_type, body = _get(fixture_server, "/")
    assert status == 200
    assert content_type.startswith("text/html")
    assert b"claudeglass" in body


@pytest.mark.parametrize(
    ("name", "expected_type"),
    [
        ("app.js", "text/javascript"),
        ("app.css", "text/css"),
    ],
)
def test_fixture_server_serves_static_files(fixture_server: str, name: str, expected_type: str) -> None:
    status, content_type, body = _get(fixture_server, "/static/" + name)
    assert status == 200
    assert content_type.startswith(expected_type)
    assert len(body) > 0


def test_fixture_server_serves_every_canned_api_route(fixture_server: str) -> None:
    for route in _documented_get_routes():
        if "<id>" in route:
            continue  # exercised separately below with a real id
        status, content_type, body = _get(fixture_server, route)
        assert status == 200, f"{route} -> {status}"
        assert content_type.startswith("application/json")
        envelope = json.loads(body)
        if route == "/api/report.json":
            # Finding 1: report.json is the one route that is never
            # {"ok": ..., "data": ...} -- see
            # test_fixture_server_serves_report_json_unwrapped below.
            continue
        assert envelope["ok"] is True, f"{route} -> {envelope}"


def test_fixture_server_serves_report_json_unwrapped(fixture_server: str) -> None:
    """Regression test for review finding 1 (blocking): docs/api.md
    documents ``/api/report.json`` as the raw rendered document, kept
    unwrapped for CLI byte parity -- it must never gain an ``{"ok": ...,
    "data": ...}`` envelope the way every other ``/api/*`` route does.
    """
    status, content_type, body = _get(fixture_server, "/api/report.json")
    assert status == 200
    assert content_type.startswith("application/json")
    payload = json.loads(body)
    assert "schema_version" in payload
    assert "report" in payload
    assert "ok" not in payload
    assert "data" not in payload


def test_app_js_load_report_accepts_the_unwrapped_report_json_shape() -> None:
    """Regression test for review finding 1 (blocking): app.js's
    ``loadReport()`` used to gate success on ``body.ok !== true`` and
    only ever read the report out of ``body.data.report`` -- since the
    real ``/api/report.json`` response never sets ``body.ok`` (see
    ``test_fixture_server_serves_report_json_unwrapped`` above), every
    tab that calls ``loadReport()`` (Overview/Cache/TTL/Agents/Config/
    Usage/Diagnostics/Recommendations) treated a successful 200 response
    as a hard failure. This fails against the pre-fix source (which
    contains neither ``body.report`` nor an ``ok === false`` failure
    check) and passes once ``loadReport()`` accepts the unwrapped shape.
    """
    # Scoped to loadReport()'s own body, not a coincidental match
    # elsewhere in the dashboard.
    load_report_src = _function_source(_app_js(), "loadReport")
    assert "body.report" in load_report_src, (
        "loadReport() must read the unwrapped report.json shape's `body.report` directly"
    )
    assert "body.ok !== true" not in load_report_src, (
        "loadReport() must not treat report.json's lack of `ok: true` as a failure"
    )
    assert "body.ok === false" in load_report_src, (
        "loadReport() should still treat an explicit `ok: false` body as a failure"
    )


def test_load_report_cache_is_keyed_by_the_selected_window() -> None:
    """Regression test for review finding 21 (should-fix): ``loadReport()``
    used to memoize a single ``state.reportPromise`` shared by every
    caller, regardless of which window was requested -- once Overview's
    window selector triggered one fetch, every tab (including Overview's
    own Scorecard/Totals) kept reading that same cached promise forever,
    silently showing one window's data no matter what the selector said.
    ``/api/report.json`` accepts a ``window_days`` query parameter
    (``docs/api.md``) and the fix caches per requested window instead of
    once globally. Fails against the pre-fix source (a single
    ``state.reportPromise`` field, and ``loadReport()`` taking no
    parameter and always fetching the bare ``/api/report.json`` URL).
    """
    app_js = _app_js()
    assert "reportPromise:" not in app_js, "the report cache must not be a single unkeyed promise"
    assert "reportPromises" in app_js, "the report cache should be keyed (by the selected window)"

    load_report_src = _function_source(app_js, "loadReport")
    assert "scopeKey()" in load_report_src, "loadReport() must key its cache by the selected window (and project)"
    assert 'return state.window + (state.project ? "|" + state.project : "")' in _function_source(app_js, "scopeKey")
    assert 'withWindow("/api/report.json")' in load_report_src, (
        "loadReport() must forward the window to /api/report.json"
    )

    # The window lives in the page header's picker and applies to every
    # view that follows it: a change must drop every drawn view and redraw
    # the one on screen, so no view keeps showing the previous window's
    # numbers. The project picker shares the same path (scopeChanged,
    # redrawForScope).
    assert "setWindow(" in _function_source(app_js, "initWindowPicker")
    set_window_src = _function_source(app_js, "setWindow")
    assert "applyWindow(value)" in set_window_src
    assert "redrawForScope()" in set_window_src
    assert "force: true" in _function_source(app_js, "redrawForScope")
    assert "applyScope(value, state.project)" in _function_source(app_js, "applyWindow")
    apply_src = _function_source(app_js, "applyScope")
    assert "state.window = windowValue" in apply_src
    assert "scopeChanged(windowChanged)" in apply_src
    changed_src = _function_source(app_js, "scopeChanged")
    assert "delete renderedViews[key]" in changed_src
    assert "delete state.reportPromises[scopeKey()]" in changed_src
    # A new window refreshes every project's report too (the picker's list).
    assert "if (windowChanged) delete state.reportPromises[state.window]" in changed_src


def test_every_windowed_request_carries_the_picked_project() -> None:
    """The project picker narrows every figure that follows the window:
    withWindow adds the project beside the window, so each route the
    dashboard asks through it is filtered (docs/api.md, "Filtering by
    project"). The Overview's previous window asks by since and until, so
    it adds the project on its own."""
    app_js = _app_js()
    with_window = _function_source(app_js, "withWindow")
    assert "windowParam()" in with_window and "projectParam()" in with_window
    assert "projectParam()" in _function_source(app_js, "withProject")
    assert '"project=" + encodeURIComponent(state.project)' in _function_source(app_js, "projectParam")
    assert 'withProject("/api/summary?since="' in _function_source(app_js, "renderOverview")
    # Only windowParam writes the window query: a request that built its
    # own would drop the project. The one exception is the every-project
    # report the picker lists projects from.
    assert app_js.count('"window_days="') == 1
    assert app_js.count('"/api/report.json?" + windowParam()') == 1
    assert _js_code_only(app_js).count("windowParam()") == 3


def test_the_project_lives_in_the_address_only() -> None:
    """A picked project narrows every figure, so it lasts only as long as
    the address that names it: nothing stores it, and a visit without it
    shows every project. The address puts it straight after the window."""
    app_js = _app_js()
    assert "tls:project" not in app_js
    assert 'project: ""' in _declaration_source(app_js, "state")
    resolve = _function_source(app_js, "resolveRoute")
    assert 'var project = params.project || "";' in resolve
    assert "delete extra.project" in resolve
    assert "LEADING_PARAMS = { w: 0, project: 1 }" in app_js
    assert "paramRank(a) - paramRank(b)" in _function_source(app_js, "formatHash")
    assert "w: state.window, project: state.project" in _function_source(app_js, "scopeParams")
    # Every address goes through scopeParams: one that wrote the window
    # itself would drop the project (a link, a page saying what it has
    # open, the router).
    assert app_js.count("w: state.window") == 1
    for name in ("pageLink", "replaceParams", "goTo", "redrawForScope", "resolveRoute", "evidenceItem"):
        assert "scopeParams()" in _function_source(app_js, name), name
    # Back to another window and project changes both at once, so no
    # request asks for the new window with the old project.
    resolve = _function_source(app_js, "resolveRoute")
    assert "applyScope(windowValue, project)" in resolve
    assert "applyWindow(" not in resolve and "applyProject(" not in resolve


def test_the_project_picker_is_a_radio_menu_beside_the_window() -> None:
    """The project picker shares the window picker's menu: radio rows, All
    projects first, the full folder name on hover, and it hides wherever
    the window does. The header button looks set while a project is
    picked, so narrowed figures never go unnoticed."""
    app_js = _app_js()
    index = _static_text("index.html")
    assert index.index('id="project-picker"') < index.index('id="window-picker"')
    menu = _function_source(app_js, "menuControl")
    assert '"menuitemradio"' in menu and '"aria-checked"' in menu
    rows = _function_source(app_js, "setProjectRows")
    assert 'value: "", label: "All projects"' in rows
    assert "title: slug" in rows
    assert "No sessions in this window" in rows
    label = _function_source(app_js, "drawProjectLabel")
    assert '"is-filtered"' in label
    controls = _function_source(app_js, "updateScopeControls")
    assert "projectMenu.button.hidden = !follow" in controls
    assert "setProjectHandler(setProject)" in _function_source(app_js, "init")


def test_only_every_project_report_names_the_projects() -> None:
    """A report for one project lists only that project, so the picker
    and the short project names read the list from every project's
    report for the window."""
    app_js = _app_js()
    load = _function_source(app_js, "loadReport")
    assert "var everyProject = allProjects || !state.project;" in load
    assert "everyProject) setKnownProjects(report.meta.projects)" in load
    assert "loadReport({ allProjects: true })" in _function_source(app_js, "loadProjects")


def test_an_unknown_project_gives_way_to_every_project() -> None:
    """An address can name a project the service doesn't know (an old
    bookmark, a moved folder): every route answers 400 for it. The
    dashboard asks once, then shows every project and says why, as an
    unknown window gives way to the one in use."""
    app_js = _app_js()
    check = _function_source(app_js, "checkProject")
    assert "result.httpStatus !== 400" in check
    assert "\"'project'\"" in check
    assert 'setProject("")' in check
    assert "unknownProjectToast()" in check
    # The answer is kept: an address naming it again gives way at once.
    assert 'checkedProjects[value] = "unknown"' in check
    assert 'checkedProjects[project] === "unknown"' in _function_source(app_js, "applyScope")
    notice = _function_source(app_js, "errorNotice")
    assert 'pickProject("", { focus: true })' in notice and "Show all projects" in notice
    # The Overview doesn't read a project error as "no change recorded".
    assert "!projectError" in _function_source(app_js, "renderOverview")


def test_search_finds_projects() -> None:
    """Search lists the window's projects, and offers "Show all projects"
    while one is picked; both go through the picker's own handler."""
    app_js = _app_js()
    assert "pickProject(slug)" in _function_source(app_js, "projectEntries")
    assert "safely(loadProjects(), projectEntries)" in _function_source(app_js, "loadEntries")
    assert '"Show all projects"' in _function_source(app_js, "commandEntries")


def test_section_page_map_includes_recache_by_group() -> None:
    """Regression test for review finding 20 (should-fix): docs/ui.md
    documents ``recache_by_group`` as shown beside ``recache`` itself,
    but the section map once listed only ``recache`` -- a docs/code
    mismatch. Both map to Cache \u203a Rebuilds."""
    mapping = _js_string_map(_app_js(), "SECTION_PAGE_MAP")
    assert mapping["recache"] == "cache/rebuilds"
    assert mapping["recache_by_group"] == mapping["recache"], (
        "recache_by_group should map to the same view as recache"
    )


def test_savings_segment_wires_up_pages_renderers_and_section_map() -> None:
    """v4 wiring round: Spend \u203a Savings (carry/compaction_sim/
    model_swap/waste) must be consistent across the places a view is
    registered -- links.js's PAGES, app.js's VIEW_RENDERERS, and
    SECTION_PAGE_MAP (so the four sections don't also fall through to
    Data quality, the same trap ``recache_by_group`` hit above)."""
    app_js = _app_js()
    assert "spend/savings" in _view_keys()
    renderers_src = _declaration_source(app_js, "VIEW_RENDERERS")
    assert re.search(r'"spend/savings"\s*:\s*renderSavings', renderers_src)
    mapping = _js_string_map(app_js, "SECTION_PAGE_MAP")
    for section_key in ("carry", "compaction_sim", "model_swap", "waste"):
        assert mapping.get(section_key) == "spend/savings", (
            f"{section_key} should map to Spend \u203a Savings, not fall through to Data quality"
        )


def test_render_baseline_shows_the_project_slug_not_the_raw_row_id() -> None:
    """Regression test for review nit 27: ``Store.baselines()`` was
    fixed to join in the owning project's redacted ``slug`` so a caller
    doesn't have to show the meaningless ``projects.id`` primary key --
    but ``renderBaseline`` in ``app.js`` kept reading ``row.project_id``
    for the "Project" column, so the store-side fix never reached the
    screen: the Baseline table still showed an opaque integer under a
    "Project" heading. Fails against the pre-fix source (``row.project_id``
    with no ``row.project_slug`` anywhere in the function) and passes
    once the column reads ``row.project_slug`` instead.
    """
    render_baseline_src = _function_source(_app_js(), "renderBaseline")
    assert "row.project_slug" in render_baseline_src, (
        "the Project column must render the joined, redacted project_slug"
    )
    assert "row.project_id" not in render_baseline_src, (
        "the Project column must not fall back to the opaque projects.id primary key"
    )


def test_app_js_timeline_never_uses_math_max_apply() -> None:
    """Regression test for review finding 10 (should-fix):
    ``Math.max.apply(null, array)`` spreads ``array`` as individual call
    arguments -- a session with tens of thousands of turns can exceed the
    engine's call-stack/argument-count limit. Fails against the pre-fix
    source (which used exactly this pattern in
    ``buildSessionTimeline``) and passes once it's replaced with a plain
    loop.
    """
    app_js = _app_js()
    assert "Math.max.apply" not in app_js
    assert ".apply(" not in app_js


def test_app_js_timeline_draws_a_circle_for_a_single_turn_session() -> None:
    """Regression test for review finding 11 (should-fix): a session with
    exactly one priced turn produces a single point, and an SVG
    ``<polyline>`` needs at least two points to render anything -- a
    single-turn session's chart silently rendered nothing at all. Fails
    against the pre-fix source (a single unconditional ``<polyline>``
    push, no ``points.length`` branch) and passes once
    ``buildSessionTimeline`` draws a ``<circle>`` for the one-point case.
    The timeline is a d3 chart type in charts-types.js now, so the circle
    is appended with d3 rather than written as markup.
    """
    timeline_src = _function_source(_app_js(), "buildSessionTimeline")
    marker = "points.length === 1"
    assert marker in timeline_src, "buildSessionTimeline must special-case a single-point series"
    one_point = timeline_src.split(marker, 1)[1].split("}", 1)[0]
    assert 'append("circle")' in one_point


def _split_top_level_args(args_str: str) -> list[str]:
    """Split a `loadInto(...)` argument string on top-level commas only,
    so a nested call like `encodeURIComponent(profile.id)` inside one
    argument doesn't get mistaken for an argument boundary."""
    parts = []
    depth = 0
    current = ""
    for ch in args_str:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += ch
    parts.append(current)
    return parts


def _loadinto_named_render_callbacks(app_js: str) -> list[str]:
    """Every bare identifier passed as `loadInto(container, url, <name>)`'s
    third argument -- i.e. render callbacks referenced by name, not the
    inline `function (data, container) {...}` literals `loadInto` is also
    called with."""
    names = []
    # loadInto's own body calls itself again to retry, passing its own
    # `render` parameter on: that is not a render callback's name.
    own_body = _function_source(app_js, "loadInto")
    own_start = app_js.index(own_body)
    own_end = own_start + len(own_body)
    for match in re.finditer(r"loadInto\(", app_js):
        # Skip `function loadInto(container, url, render, options) {...}`
        # itself -- its own parameter list isn't a call site.
        if app_js[: match.start()].rstrip().endswith("function"):
            continue
        if own_start <= match.start() < own_end:
            continue
        open_paren = match.end() - 1
        depth = 0
        i = open_paren
        while i < len(app_js):
            if app_js[i] == "(":
                depth += 1
            elif app_js[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        args = _split_top_level_args(app_js[open_paren + 1 : i])
        if len(args) >= 3:
            third = args[2].strip()
            if re.fullmatch(r"[A-Za-z_$][\w$]*", third):
                names.append(third)
    return names


def test_loadinto_render_callbacks_take_data_first_container_second() -> None:
    """Regression test: `loadInto(container, url, render)` (app.js's
    fetch-and-render helper) always invokes its third argument as
    `render(data, container)` -- data first, container second. Every
    named render callback it's called with must declare its parameters
    in that same order.

    This catches the bug where `renderSummaryCards` declared
    `(container, summary)` -- the reverse of what `loadInto` actually
    passes -- so the Overview tab's summary cards received the summary
    object where `container` was expected and blew up with
    `container.appendChild is not a function`. Fails against the
    pre-fix source (`renderSummaryCards(container, summary)`) and
    passes once the parameter order matches every other callback.
    """
    app_js = _app_js()
    names = _loadinto_named_render_callbacks(app_js)
    assert names, "no named render callbacks found -- has loadInto's call pattern changed?"

    bad_first_params = {"container", "target", "panel", "el"}
    good_second_params = {"container", "target"}
    for name in names:
        match = re.search(r"function\s+" + re.escape(name) + r"\s*\(([^)]*)\)", app_js)
        assert match, f"no declaration found for render callback {name!r}"
        params = [p.strip() for p in match.group(1).split(",")]
        assert len(params) >= 2, f"{name}({', '.join(params)}) declares fewer than 2 parameters"
        assert params[0] not in bad_first_params, (
            f"{name}'s first parameter is {params[0]!r} -- loadInto calls render(data, container), "
            f"so the first parameter must be the data argument, not the container"
        )
        assert params[1] in good_second_params, (
            f"{name}'s second parameter is {params[1]!r}, expected one of {sorted(good_second_params)}"
        )


def test_the_setup_card_shows_until_every_essential_part_works() -> None:
    """``/api/setup/status`` (``setup_status.py``) drives the Overview's
    Setup card: someone who skipped ``install-service``, never connected,
    or never said how they pay needs to see it in the UI, with the
    command that fixes it. It replaces the old logon notice, so the
    dashboard at logon still says why it matters (``cleanupPeriodDays``)
    -- in the item's own text, which ``setup_status`` writes. Data
    quality shows the whole checklist. Source check: there is no browser
    in this test process.
    """
    app_js = _app_js()
    card_src = _function_source(app_js, "renderSetupCard")
    assert "setup.done" in card_src, "the card must hide once setup is done"
    assert "item.essential" in card_src, "the card lists only the parts that matter"
    item_src = _function_source(app_js, "setupItem")
    assert "codeBlockWithCopy(withCli(item.fix)" in item_src, "each fix is a command to copy, in this install's form"
    assert "renderLogonNotice" not in app_js

    # The fix arrives already in this install's form, and the module has
    # the command's name: withCli must not swap it in a second time
    # ("python -m python -m claudeglass init").
    with_cli = _function_source(app_js, "withCli")
    pattern = re.search(r"text\.replace\(/(.+)/g,", with_cli).group(1).replace("\\/", "/")
    swap = lambda text: re.sub(pattern, lambda m: m.group(1) + "python -m claudeglass ", text)  # noqa: E731
    assert swap("python -m claudeglass init") == "python -m claudeglass init"
    assert swap("Run claudeglass init") == "Run python -m claudeglass init"
    assert swap("my-claudeglass init") == "my-claudeglass init"

    overview = _function_source(app_js, "renderOverview")
    assert 'fetchJson("/api/setup/status")' in overview and "renderSetupCard(" in overview, "the Overview lost the card"
    assert '"/api/setup/status", renderSetupList' in _function_source(app_js, "renderDataQuality"), (
        "Data quality no longer shows the whole checklist"
    )
    assert '"/api/health", renderHealth' in _function_source(app_js, "renderDataQuality"), (
        "Data quality no longer shows the service's health in full"
    )
    service = setup_status._service(False, True, None)
    assert service.essential and service.state != "ok"
    assert "cleanupPeriodDays" in service.detail and service.fix == "claudeglass install-service"


def test_ignored_recommendations_leave_every_list_but_their_own() -> None:
    """``/api/recommendations`` marks each row ``ignored`` (ignores.py).
    ``groupRecommendations`` leaves those out unless asked for them, so
    the Overview, the Actions badge and the checks' links all count what
    the To do list shows; search and the "Feeds N actions" index skip
    them too. Only Actions asks for the ignored ones, and says "Ignore",
    never "Dismiss" (docs/writing-help.md)."""
    app_js = _app_js()
    grouping = _function_source(app_js, "groupRecommendations")
    assert "rec.ignored" in grouping and "opts.ignored" in grouping
    assert "if (rec.ignored) return;" in _function_source(app_js, "recommendationEntries")
    assert "if (rec.ignored) return;" in _function_source(app_js, "actionIndex")
    assert "groupRecommendations(recs, { ignored: true })" in _function_source(app_js, "renderRecommendations")
    section = _function_source(app_js, "ignoreSection")
    assert 'postJson(withWindow("/api/recommendations/ignore")' in section
    assert "state.recommendationPromises = {}" in section
    assert '"Ignore this recommendation"' in section and "Dismiss" not in section
    assert "Stop ignoring" in section


def test_the_overview_compares_like_with_like() -> None:
    """The Overview's deltas compare /api/summary with /api/summary for
    the period of the same length just before (docs/api.md: store and
    report price differently, so a summary is never compared with a
    report figure), and there is no earlier period for "all time" or
    "since my last change"."""
    app_js = _app_js()
    overview = _function_source(app_js, "renderOverview")
    assert 'fetchJson(withWindow("/api/summary"))' in overview
    assert '"/api/summary?since="' in overview and '"&until="' in overview
    assert 'withWindow("/api/daily-usage") + "&split=agent"' in overview, "chart 1 on the Overview splits main and subagents"
    assert 'renderChart(' in overview and '"daily-spend"' in overview

    previous = _function_source(app_js, "previousPeriod")
    for phrase in ("the hour before", "the 24 hours before", "the same hours yesterday", "days before"):
        assert phrase in previous, f"previousPeriod never names {phrase!r}"
    assert '"all"' not in previous and '"change"' not in previous, "all time and since-last-change have no earlier period"


def test_the_overview_leaves_the_health_detail_to_data_quality() -> None:
    """The Overview shows only the logon warning; the full service health
    moved to Data quality (docs/ui.md)."""
    overview = _function_source(_app_js(), "renderOverview")
    assert "renderHealth(" not in overview
    assert "Service health" not in overview


def test_the_overview_chart_runs_the_width_of_where_your_tokens_go() -> None:
    """The Overview's daily spend sits in "Where do your tokens go?" at
    the page's width, not fitted to a panel beside it; a chart resized
    mid-draw still carries its draw-in on, and anything else is stopped
    first so it can't finish at the old size (docs/ui.md)."""
    overview = _function_source(_app_js(), "renderOverview")
    assert "tokens.body.appendChild(chartHost);" in overview
    assert "fittedChartHeight" not in _app_js() and "setChartHeight" not in _app_js()
    carry = _function_source(_app_js(), "redraw")
    assert "now < moving.ends" in carry
    assert ".interrupt()" in carry
    assert "resize: true" in carry


def test_the_first_run_tells_a_running_scan_from_an_empty_one() -> None:
    """`scan.running` only says the scanner thread is alive; a scan in
    progress is `scan.scanning` (docs/api.md)."""
    first = _function_source(_app_js(), "firstRun")
    assert "health.scan.scanning" in first
    assert "scan.running" not in first
    assert 'pageLink("data"' in first


def test_available_saving_is_never_below_an_action_it_lists() -> None:
    """The model lever adds up every agent type's cheapest alternative
    (the model-tier action prices a subset of them), and a priced action
    no lever counts joins the total, so "At most" holds (docs/ui.md). An
    action that is a lever's own finding joins it only once: the
    auto-compact window once added $38 on top of the compaction lever's
    $44, so the ways to save came to more than the spend."""
    levers = _function_source(_app_js(), "savingsLevers")
    assert "tables.model_swap_by_agent_type" in levers
    assert "model_swap_summary" not in levers
    available = _function_source(_app_js(), "availableSaving")
    # Priced actions are counted as Actions lists them, one group each.
    assert "groupSavingUsd(group)" in available
    assert "if (LEVER_RULES[group.id]) return;" in available
    assert "rec.saving_usd" in _function_source(_app_js(), "groupSavingUsd")
    for rule, lever in (
        ("model-tier", "model_swap"),
        ("compaction-window", "compaction_sim"),
        ("tool-output-carry", "carry"),
        ("wasted-turns", "waste"),
    ):
        assert f'"{rule}": "{lever}"' in _app_js(), rule


def test_a_delta_of_three_times_or_more_reads_as_a_sentence() -> None:
    chip = _function_source(_app_js(), "deltaChip")
    assert 'times + " as much as " + period' in chip


def test_fixture_server_serves_session_detail(fixture_server: str) -> None:
    status, content_type, body = _get(fixture_server, "/api/session/session-ui-1")
    assert status == 200
    assert content_type.startswith("application/json")
    envelope = json.loads(body)
    assert envelope["ok"] is True
    assert envelope["data"]["id"] == "session-ui-1"
    assert envelope["data"]["tags"] == {"purpose": "refactor-override"}


def test_fixture_server_404s_unknown_session(fixture_server: str) -> None:
    status, _content_type, body = _get(fixture_server, "/api/session/does-not-exist")
    assert status == 404
    envelope = json.loads(body)
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "not_found"


def _js_object_keys(app_js: str, var_name: str) -> dict[str, str]:
    source = _declaration_source(app_js, var_name)
    return dict(re.findall(r'^\s*"?([a-z_]+)"?\s*:\s*"([^"]*)"', source, re.MULTILINE))


def test_every_report_section_is_mapped_to_a_view() -> None:
    """A section missing from SECTION_PAGE_MAP silently lands on Data
    quality; every section report.py can emit must be placed on purpose,
    and on a view that exists. A table placed away from its section
    (TABLE_PAGE_MAP) names a real section and a real view too."""
    from claudeglass.report import _SECTION_ORDER

    app_js = _app_js()
    views = set(_view_keys())
    mapping = _js_string_map(app_js, "SECTION_PAGE_MAP")
    unmapped = [key for key in _SECTION_ORDER if key not in mapping]
    assert unmapped == []
    assert set(mapping.values()) <= views, set(mapping.values()) - views
    tables = _js_string_map(app_js, "TABLE_PAGE_MAP")
    assert tables, "TABLE_PAGE_MAP has no entries"
    for key, view in tables.items():
        section, _, table = key.partition(".")
        assert section in _SECTION_ORDER and table, key
        assert view in views, (key, view)


def test_pages_registry_matches_the_view_renderers() -> None:
    """Every page and segment in links.js's PAGES has a renderer in
    app.js's VIEW_RENDERERS (in sidebar order), an address-safe id and
    an intro line, and every name is used once."""
    app_js = _app_js()
    pages = _pages()
    renderers = re.findall(r'^\s*"?([a-z/-]+)"?\s*:\s*render[A-Za-z]+,', _declaration_source(app_js, "VIEW_RENDERERS"), re.M)
    assert renderers == _view_keys()
    id_shape = re.compile(r"^[a-z]+(-[a-z]+)*$")
    for page in pages:
        assert id_shape.match(page["id"]), page["id"]
        assert page["label"] and page["icon"], page["id"]
        for segment in page.get("segments") or []:
            assert id_shape.match(segment["id"]), segment["id"]
            assert segment["intro"], segment["id"]
        if not page.get("segments"):
            assert page["intro"], page["id"]
    labels = _view_labels()
    assert len(labels) == len(set(labels)), labels
    assert len({page["label"] for page in pages}) == len(pages)


def test_heading_policy_one_h1_per_page() -> None:
    """The page title in the header is the one h1; views add h2 per
    section, h3 per table and h4 at most below that."""
    app_js = _app_js()
    html = _static_text("index.html")
    assert re.findall(r"<h1\b[^>]*>", html) == ['<h1 id="page-title" tabindex="-1">']
    assert 'el("h1"' not in app_js
    for deeper in ("h5", "h6"):
        assert f'el("{deeper}"' not in app_js
    assert 'el("h2", { class: "section-title"' in _function_source(app_js, "renderSectionGeneric")
    assert 'el("h3", { text: table.title || table.name })' in _function_source(app_js, "renderTable")


def _readme_page_table_names() -> list[str]:
    text = README_MD.read_text(encoding="utf-8")
    section = re.search(r"## What each page answers\n\n.*?(\| Page \|.+?)\n\n", text, re.S)
    assert section, "README.md's page table section has changed shape"
    rows = section.group(1).splitlines()[2:]  # drop the header row and its --- separator
    return [row.split("|")[1].strip() for row in rows]


def test_readme_page_table_matches_the_pages() -> None:
    """D8: the README's table of what each part of the dashboard answers
    once listed 14 tabs while the dashboard shipped 16. Regression test:
    its rows, in order, name exactly the pages and segments PAGES
    defines, as the dashboard names them ("Spend \u203a Usage")."""
    assert _readme_page_table_names() == _view_labels()


def _readme_glossary_terms() -> dict[str, str]:
    text = README_MD.read_text(encoding="utf-8")
    section = re.search(r"## Glossary\n\n(.+?)\n\n## ", text, re.S)
    assert section, "README.md's Glossary section has changed shape"
    entries = re.findall(r"^- \*\*([^*]+)\*\*: (.+)$", section.group(1), re.M)
    assert entries, "no glossary entries found in README.md"
    return {name: re.sub(r"`([^`]*)`", r"\1", body) for name, body in entries}


def test_glossary_page_matches_the_readme_glossary() -> None:
    """D9: the README's glossary once listed 40 terms while app.js's
    GLOSSARY (the dashboard's Glossary tab) had 31 -- the metrics-capture
    terms (Metrics capture, Capture level, Tag, Prompt cycle, Work
    habits, Feedback skill, Brief templates, Sampling, Time-box) were
    never carried over. Regression test: both must name the same terms
    with the same wording (README's backtick code-spans read as plain
    text on the dashboard, since GLOSSARY renders via `.textContent`)."""
    app_js = _app_js()
    pairs = re.findall(r'\["([^"]+)", "([^"]+)"\]', _declaration_source(app_js, "GLOSSARY"))
    assert pairs, "GLOSSARY has no entries"
    app_glossary = dict(pairs)
    assert app_glossary == _readme_glossary_terms()


# -- Phase 8: links inside text, glossary terms, "Feeds N actions" ------------


def test_every_jargon_term_is_a_glossary_term_and_matches_its_own_name() -> None:
    """links.js's JARGON lists the words ui.js's prose() turns into a
    glossary popover. Each must be a GLOSSARY term (the popover shows
    its definition) and its pattern must match the term's own name, so
    the popover fires on the word the Glossary uses."""
    app_js = _app_js()
    glossary = dict(re.findall(r'\["([^"]+)", "([^"]+)"\]', _declaration_source(app_js, "GLOSSARY")))
    jargon = re.findall(r'\["([^"]+)", "([^"]+)"\]', _declaration_source(app_js, "JARGON"))
    assert jargon, "JARGON has no entries"
    for term, pattern in jargon:
        assert term in glossary, f"JARGON names {term!r}, which the Glossary doesn't define"
        assert re.search(r"\b(?:" + pattern + r")\b", term, re.I), f"{term!r} doesn't match its own pattern {pattern!r}"


def test_page_token_pattern_is_the_servers() -> None:
    """The client renders the server's {{page:...}} tokens as links: its
    pattern accepts every page and segment pages.PAGES names, as the
    server's does, and rejects what the server's rejects."""
    from claudeglass import pages

    match = re.search(r"var PAGE_TOKEN = /(.+)/g;", _app_js())
    assert match, "links.js no longer declares PAGE_TOKEN"
    client = re.compile(match.group(1))
    tokens = []
    for page in pages.PAGES:
        tokens.append("{{page:" + page.id + "}}")
        tokens.extend("{{page:" + page.id + "/" + segment.id + "}}" for segment in page.segments)
    for token in tokens:
        assert client.fullmatch(token), token
        assert pages._TOKEN_RE.fullmatch(token), token
    for bad in ("{{page:Spend}}", "{{page:spend_usage}}", "{{page:spend/}}", "{{page:}}"):
        assert not client.fullmatch(bad), bad
        assert not pages._TOKEN_RE.fullmatch(bad), bad


def test_server_text_on_the_page_goes_through_prose() -> None:
    """Help, notes, intros, actions and explanations from the server may
    carry {{page:...}} tokens: each place the dashboard shows them
    renders through prose() (links.js's linkText underneath), never as
    bare text, so no token is ever shown raw."""
    app_js = _app_js()
    assert "export function linkText(" in app_js and "export function prose(" in app_js
    expected = {
        "helpButton": 'el("dd", null, prose(pair[1]))',
        "headerCell": "prose(column.help)",
        "notesList": "prose(note, seen)",
        "renderSectionGeneric": "prose(section.intro, seen)",
        "renderTips": "prose(tip.text, seen)",
        "callout": "prose(opts.text)",
        "emptyState": "prose(text)",
        "renderRecommendationDetail": "prose(focus.action, seen)",
        "renderCheckDetail": "prose(check.summary, seen)",
        "renderSessionExplain": "prose(sentence, seen)",
        "renderHabitsSection": "notesList(section.notes",
        "renderSetup": "prose(item.text)",
    }
    for name, call in expected.items():
        body = _function_source(app_js, name)
        assert call in body, f"{name}() no longer renders its server text through prose(): {call}"
    assert 'el("li", { text: note })' not in app_js


def test_tokens_read_as_page_names_where_a_link_cannot_go() -> None:
    """A tooltip, a toast and a grid cell can't hold a link: a token in
    their text reads as the page's name (plainText, the client's
    pages.plain())."""
    app_js = _app_js()
    assert "export function plainText(" in app_js
    assert "content = plainText(content)" in _function_source(app_js, "attachTooltip")
    assert "text: plainText(message)" in _function_source(app_js, "toast")
    assert "plainText(text)" in _function_source(app_js, "cellContent")


def test_each_glossary_term_is_explained_once_per_card() -> None:
    """The first use of a term in a card or section gets the popover;
    later uses stay plain words, so the text doesn't turn into a row of
    underlines."""
    body = _function_source(_app_js(), "termNodes")
    assert "seen.has(term)" in body and "seen.add(term)" in body


def test_popovers_close_when_a_link_inside_is_followed() -> None:
    """A link inside a popover (a page link in help, "See it in the
    glossary", an action a table feeds) leaves the view: the popover
    closes with it rather than floating over the next page."""
    body = _function_source(_app_js(), "popoverButton")
    assert 'closest("a[href]")' in body and "pop.hidePopover()" in body


def test_recommendations_are_fetched_once_per_window() -> None:
    """The Actions badge and inbox, the Overview and the "Feeds N
    actions" chips share one /api/recommendations fetch per window, which
    a new window or a redraw drops with the report's."""
    app_js = _app_js()
    assert app_js.count('withWindow("/api/recommendations")') == 1
    assert "state.recommendationPromises[key]" in _function_source(app_js, "loadRecommendations")
    assert "state.recommendationPromises[scopeKey()]" in _function_source(app_js, "scopeChanged")
    assert "state.recommendationPromises = {}" in _function_source(app_js, "redrawEverything")


def test_tables_say_which_actions_they_feed() -> None:
    """Every report table that a recommendation cites as evidence says so
    in its header ("Feeds N actions"), and each cited row carries a mark;
    both open a list of the actions, each linked to its detail."""
    app_js = _app_js()
    index = _function_source(app_js, "actionIndex")
    assert "rec.evidence" in index and "item[2]" in index and "item[3]" in index
    assert "markFeeds(wrap, head, gridNode, table.name, table.title || table.name)" in _function_source(app_js, "renderTable")
    feeds = _function_source(app_js, "markFeeds")
    assert "index.byTable[tableName]" in feeds and "markRows(" in feeds
    button = _function_source(app_js, "feedsButton")
    assert '"Feeds " + words' in button
    assert 'pageLink("actions/recommendations", action.title, { id: action.key })' in button
    grid = _function_source(app_js, "dataGrid")
    assert "var rowMark = spec.rowMark || null;" in grid and "markRows: function (mark)" in grid


def _js_string_literals(src: str) -> list[str]:
    """Every string and template literal in first-party JavaScript,
    comments skipped."""
    found = []
    i = 0
    while i < len(src):
        skipped = _skip_js_string_or_comment(src, i)
        if skipped == i:
            i += 1
            continue
        if src[i] in "\"'`":
            found.append(src[i + 1 : skipped - 1])
        i = skipped
    return found


def test_no_tab_names_in_dashboard_text() -> None:
    """The dashboard has pages, not tabs: no string it shows says "the X
    tab" (the server's text has the same ban, test_pages.py). Words
    only: a one-word literal is a key name ("Tab"), an ARIA role ("tab",
    the command block's Prompt and Command) or a class name."""
    offenders = [
        literal
        for literal in _js_string_literals(_app_js())
        if " " in literal.strip() and re.search(r"\btabs?\b", literal, re.I)
    ]
    assert offenders == [], offenders


def _sections_table_keys() -> list[str]:
    text = SECTIONS_REFERENCE_MD.read_text(encoding="utf-8")
    section = re.search(
        r"\| Section key \| Title \| Module \| What it answers \|\n\|---\|---\|---\|---\|\n(.+?)\n\n", text, re.S
    )
    assert section, "docs/sections-reference.md's report-sections table has changed shape"
    return [row.split("|")[1].strip().strip("`") for row in section.group(1).splitlines()]


def _sections_reference_order() -> list[str]:
    text = SECTIONS_REFERENCE_MD.read_text(encoding="utf-8")
    match = re.search(r"in this order:(.+?only with `--baseline`\))", text, re.S)
    assert match, "docs/sections-reference.md's section-order sentence has changed shape"
    return re.findall(r"`([a-z_]+)`", match.group(1))


def test_sections_reference_lists_every_report_section_in_order() -> None:
    """D13: report.build_report's actual _SECTION_ORDER (plus
    baseline_comparison, appended unconditionally after it) once ran
    ahead of both lists -- habits and capture were missing from each,
    and the sections table (then in the README) also lacked elasticity,
    agent_startup, context_budget and baseline_comparison. Regression
    test: sections-reference.md's "Sections at a glance" table and its
    section-order sentence must both name every section build_report
    can emit, in its exact order."""
    from claudeglass.report import _SECTION_ORDER

    expected = [*_SECTION_ORDER, "baseline_comparison"]
    assert _sections_table_keys() == expected
    assert _sections_reference_order() == expected


def test_sections_table_workstyle_row_names_every_archetype() -> None:
    """D15: the sections table's workstyle row (then in the README) once
    named six archetypes while workstyle.py detects seven -- `mixed`, the
    fallback when none of the other six match, was missing. Regression
    test: the row's backtick archetype names must match workstyle.py's
    real set."""
    from claudeglass.workstyle import _ARCHETYPE_DESCRIPTIONS

    text = SECTIONS_REFERENCE_MD.read_text(encoding="utf-8")
    row = next(line for line in text.splitlines() if line.startswith("| `workstyle` |"))
    named = set(re.findall(r"`([a-z-]+)`", row)) - {"workstyle", "workstyle.py"}
    assert named == set(_ARCHETYPE_DESCRIPTIONS)


def test_every_working_pattern_has_a_plain_label() -> None:
    """Phase 10: the working patterns grid showed workstyle.py's keys
    ("overseer-fanout", "plan-high-implement-low"). Each key now has a
    plain label in helptext's value_labels for the table."""
    from claudeglass.helptext import TABLE_COPY
    from claudeglass.workstyle import _ARCHETYPE_DESCRIPTIONS

    labels = TABLE_COPY["workstyle_archetypes"].value_labels
    assert set(labels) == set(_ARCHETYPE_DESCRIPTIONS)
    assert all(label and "-" not in label for label in labels.values())


def test_every_task_word_has_a_plain_name_in_task_tables() -> None:
    """Phase 10: Kinds of task and the brief templates showed capture's
    task words ("bugfix", "plan"). Every word has a plain name, and every
    table with a task column carries them."""
    from claudeglass.capture_catalogue import TAG_VOCAB
    from claudeglass.helptext import TABLE_COPY, TASK_LABELS, TASK_TABLES

    assert set(TASK_LABELS) == set(TAG_VOCAB["task"])
    with_task = {name for name, copy in TABLE_COPY.items() if "task" in copy.columns}
    assert with_task == set(TASK_TABLES)
    for name in TASK_TABLES:
        assert set(TASK_LABELS) <= set(TABLE_COPY[name].value_labels), name


def test_capture_banner_is_polled_with_health_and_links_to_its_segment() -> None:
    """The capture banner sits under the health banner on every page and
    is refreshed from /api/health's capture block; the sidebar's status
    line always says the capture level and links to Setup \u203a Capture."""
    app_js = _app_js()
    html = _static_text("index.html")
    assert html.index('id="health-banner"') < html.index('id="capture-banner"') < html.index('id="views"')
    assert "updateCaptureBanner(health.capture)" in _function_source(app_js, "pollHealth")
    banner = _function_source(app_js, "renderCaptureBanner")
    assert "feedback_note" in banner and "captureLink(" in banner
    status = _function_source(app_js, "renderStatusLine")
    assert "captureLink(" in status and "captureStatusText(" in status


def test_loading_says_what_it_is_waiting_for() -> None:
    """A view loading for the first time says what is loading in words
    over its skeleton (not only to a screen reader), a view being
    refetched keeps its figures under an "Updating" label, and the status
    line says when a later scan is checking for new sessions, with its
    progress, polling quickly while it runs."""
    app_js = _app_js()
    css = _static_text("app.css")
    node = _function_source(app_js, "loadingNode")
    assert 'class: "loading-label"' in node and "visually-hidden" not in node
    # Every loadInto names what it waits for, by its route.
    assert "loadingLabel(url)" in _function_source(app_js, "loadInto")
    labels = re.search(r"var LOADING_LABELS = \[(.*?)\n\];", app_js, re.S).group(1)
    prefixes = re.findall(r'\["(/api/[^"]*)"', labels)
    for match in re.finditer(r'loadInto\(\s*\w+,\s*(?:withWindow\()?"(/api/[^"?]*)', app_js):
        assert any(match.group(1).startswith(p.split("?")[0]) for p in prefixes), match.group(1)
    assert "loadingNode()" not in app_js
    assert ".loading-label {" in css
    assert ".is-refreshing::after {" in css and 'content: "Updating\\2026";' in css
    assert ".is-refreshing > * {" in css
    status = _function_source(app_js, "renderStatusLine")
    assert '"Checking for new sessions"' in status
    assert "scanProgressText(scan)" in status and "status-detail" in status
    progress = _function_source(app_js, "scanProgressText")
    for phase in ('"finding"', '"reading"', '"storing"'):
        assert phase in progress
    poll = _function_source(app_js, "pollHealth")
    assert '(health.status === "starting" || healthPoll.rescanning) ? 3000 : 60000' in poll
    # A rescan that stored sessions offers the redraw once it finishes.
    assert "wasRescanning && health && !healthPoll.rescanning" in poll and "healthPoll.redrawDue = true" in poll


def test_capture_segment_repeats_the_cost_warning_before_using_more_tokens() -> None:
    """Switching to a level, a metric or a larger sample that asks Claude
    for more goes through confirmCapture, which shows data.warning."""
    app_js = _app_js()
    for name in ("renderCaptureLevels", "renderCaptureControls", "renderMetricRow"):
        src = _function_source(app_js, name)
        assert "confirmCapture(" in src and "data.warning" in src, name
    post = _function_source(app_js, "postCapture")
    assert 'postJson("/api/capture"' in post and "error.commands" in post


# -- P9b: UX-6/9 (accessibility, mobile, dark-mode fixes) and the P4 --------
# -- leftovers (emptyState()/API gate) --------------------------------------


def test_page_title_focus_ring_is_not_suppressed() -> None:
    """A tab panel's focus rule once suppressed the outline outright
    (``outline: none``) though a keyboard user landed there right after
    switching tabs -- a WCAG 2.4.7 gap. Moving between pages now puts
    focus on the page title (#page-title), which gets the same visible
    ring as every other focusable control: the global :focus-visible
    rule (tests/test_ui_tokens.py checks that rule itself), with nothing
    on the title, h1 or the view that takes it away."""
    app_css = _static_text("app.css")
    assert re.search(r"(?m)^:focus-visible\s*\{[^}]*outline: 2px solid var\(--focus\)", app_css)
    for match in re.finditer(r"([^{}]*)\{([^}]*)\}", app_css):
        selector, body = match.group(1), match.group(2)
        if re.search(r"#page-title|\bh1\b|\.page-heading|\.view\b", selector) and ":focus" in selector:
            assert "outline: none" not in body and "outline: 0" not in body, selector.strip()
    assert "focusTitle" in _function_source(_app_js(), "showView")


def test_lever_grid_column_minimum_shrinks_on_narrow_viewports() -> None:
    """A bare ``minmax(320px, 1fr)`` forces horizontal overflow once the
    viewport (minus app-shell's own side padding) drops under 320px --
    the fix wraps the minimum in min(320px, 100%) so the track can't
    exceed the container's own width."""
    app_css = _static_text("app.css")
    match = re.search(r"\.lever-grid\s*\{([^}]*)\}", app_css)
    assert match, "app.css no longer defines .lever-grid"
    assert "minmax(min(320px, 100%), 1fr)" in match.group(1)


def test_advanced_detail_raw_diff_wraps_instead_of_overflowing() -> None:
    """The raw settings-file diff (profile launch card, "Show the file
    changes") is a bare <pre> with no wrap rule of its own elsewhere --
    unlike .code-block pre / .rec pre, a long line forced the whole
    panel to scroll horizontally."""
    app_css = _static_text("app.css")
    match = re.search(r"\.advanced-detail pre\s*\{([^}]*)\}", app_css)
    assert match, "app.css no longer defines .advanced-detail pre"
    body = match.group(1)
    assert "white-space: pre-wrap" in body
    assert "overflow-wrap: anywhere" in body


def test_recommendation_severity_is_a_chip_with_an_icon_and_a_label() -> None:
    """A recommendation's severity used to be a coloured left border on
    its card, with no dark-mode colour of its own and nothing but colour
    to carry it (WCAG 1.4.1). It is now a chip in the card's head: an
    icon and the label, tinted from the status tokens, which have a dark
    value each (tests/test_ui_tokens.py)."""
    app_js = _app_js()
    chip = _function_source(app_js, "severityChip")
    assert "icon(" in chip and "SEVERITY_LABELS" in chip
    card = _function_source(app_js, "renderRecommendationDetail")
    # Inside the heading, so moving by headings reads the severity first.
    assert re.search(
        r'el\("h\d", \{ class: "rec-head", tabIndex: -1 \}, \[\s*severityChip\(group\.severity\)', card
    )
    assert "visually-hidden" in card
    app_css = _static_text("app.css")
    for severity, token in (("action", "serious"), ("advice", "warn")):
        match = re.search(r"\.severity-" + severity + r"\s*\{([^}]*)\}", app_css)
        assert match, severity
        assert "color: var(--" + token + ")" in match.group(1)
        assert "background: var(--" + token + "-soft)" in match.group(1)
    assert not re.search(r"\.rec-severity-\w+\s*\{[^}]*border-left", app_css)


def test_timeline_markers_use_a_distinct_shape_per_kind_not_only_color() -> None:
    """Every timeline marker used to be an identical <circle>,
    distinguished only by fill color (WCAG 1.4.1) -- a colorblind viewer
    or a low-color display can't tell recache from compaction from
    spawn. All 7 marker kinds (4 turn markers + 3 usage-limit markers)
    must now map to 7 distinct shapes, and the legend's own swatch must
    draw the real shape (markerGlyph), not just a color dot."""
    app_js = _app_js()
    glyph = _function_source(app_js, "markerGlyph")
    for shape in ("square", "triangle-up", "triangle-down", "diamond", "plus", "x", "circle"):
        assert ('"' + shape + '"') in glyph, shape

    timeline = _function_source(app_js, "buildSessionTimeline")
    shapes_match = re.search(r"(?:var|let|const) markerShapes = (\{[^}]*\});", timeline)
    limit_shapes_match = re.search(r"(?:var|let|const) limitMarkerShapes = (\{[^}]*\});", timeline)
    assert shapes_match and limit_shapes_match
    shapes = dict(re.findall(r'(\w+):\s*"([\w-]+)"', shapes_match.group(1)))
    limit_shapes = dict(re.findall(r'(\w+):\s*"([\w-]+)"', limit_shapes_match.group(1)))
    all_kinds = {**shapes, **limit_shapes}
    assert len(all_kinds) == 7, all_kinds
    assert len(set(all_kinds.values())) == 7, "two marker kinds share a shape: " + repr(all_kinds)
    # The legend draws the same glyph, not a plain color circle.
    assert "swatchIcon" in timeline and "markerGlyph(shape" in timeline
    # Shape tells the kinds apart, so every marker is drawn in ink: a
    # chart colour (aqua, yellow, magenta) falls under 3:1 against the
    # light panel (WCAG 1.4.11), and ink holds it in both themes.
    for name in ("markerColors", "limitMarkerColors"):
        colours = re.search(r"(?:var|let|const) " + name + r" = (\{[^}]*\});", timeline)
        assert colours, name
        values = re.findall(r'\w+:\s*"([^"]+)"', colours.group(1))
        assert values and all(re.fullmatch(r"var\(--ink-[12]\)", value) for value in values), values


def test_a_signed_change_uses_a_true_minus_sign() -> None:
    """A delta reads "+12%" or "−3%": the true minus (U+2212) is as
    wide as the plus, so signed columns line up."""
    helper = _function_source(_app_js(), "signedPercent")
    assert '"\u2212"' in helper and '"+"' in helper
    assert "signedPercent(measure.change_pct)" in _function_source(_app_js(), "measureRow")
    assert not re.search(r'> 0 \? "\+" : ""\)', _app_js())


def test_a_change_card_says_what_changed_where_and_each_measures_reading() -> None:
    source = _app_js()
    card = _function_source(source, "changeCard")
    assert "change.summary ||" in card
    # Named as the project picker names it, not by its folder.
    assert 'change.project ? "In " + (change.project_name ? projectName(change.project_name)' in _function_source(source, "changeWhere")
    # Each measure: before and after as bars on one scale, the change,
    # and the ratio test's reading, coloured by which way is better.
    row = _function_source(source, "measureRow")
    assert "Math.max(isFinite(before) ? before : 0, isFinite(after) ? after : 0)" in row
    assert "readingBadge(measure)" in row
    tone = _function_source(source, "readingTone")
    assert "if (!reading.side || !measure.better) return \"neutral\";" in tone
    assert 'reading.side === measure.better ? "good" : "bad"' in tone


def test_a_change_card_says_what_the_sessions_since_saved() -> None:
    source = _app_js()
    card = _function_source(source, "changeCard")
    assert "savedLine(item.without)" in card
    saved = _function_source(source, "savedLine")
    assert 'if (!without || typeof without.saved_usd !== "number") return null;' in saved
    assert '"Saved so far: "' in saved and '"Cost more so far: "' in saved and "without.fidelity_text" in saved
    # A row per setting only when the headline fell back to the sessions before.
    assert 'without.fidelity === "before" ? without.per_key' in _function_source(source, "perKeyTable")


def test_the_last_change_window_says_what_it_would_have_cost_without_that_change() -> None:
    source = _app_js()
    line = _function_source(source, "lastChangeLine")
    assert "(impactBody.data.changes || [])[0]" in line
    assert "without.since_text" in line and '"Without your last change ("' in line
    assert 'pageLink("changes"' in line
    assert "projectName(change.project_name)" in line
    overview = _function_source(source, "renderOverview")
    assert 'state.window !== "change" || state.project' in overview
    assert "lastChangeLine(loaded[0].body)" in overview


def test_an_estimate_is_logged_when_a_change_is_saved_or_its_command_copied() -> None:
    """EST-P5: the dashboard logs a prediction (``"log": true``) for a
    change you mean to make, never while you tick or explore."""
    source = _app_js()
    goal = _function_source(source, "renderGoalDraft")
    assert goal.count("log: true") == 1
    assert goal.index('toast("Profile saved.")') < goal.index("log: true")
    estimate = _function_source(source, "renderProfileEstimate")
    assert "if (logged || !request) return;" in estimate and "log: true" in estimate
    assert "renderProfileDiff(data, box, logEstimate)" in _function_source(source, "renderProfileDetail")
    assert "{ onCopy: onCopy }" in _function_source(source, "renderProfileDiff")
    copy = _function_source(source, "codeBlockWithCopy")
    assert "if (ok && onCopy) onCopy();" in copy
    block = _function_source(source, "commandBlock")
    assert block.count("opts.onCopy") == 2


def test_copy_button_only_claims_success_when_the_clipboard_write_succeeded() -> None:
    """copyToClipboard used to fire-and-forget navigator.clipboard.write-
    Text and the button always flipped to "Copied" regardless of what
    happened -- a rejected promise (insecure context, denied permission)
    left it falsely claiming success."""
    app_js = _app_js()
    copy_fn = _function_source(app_js, "copyToClipboard")
    assert "return navigator.clipboard.writeText(text).then(" in copy_fn
    assert "return Promise.resolve(false)" in copy_fn

    code_block = _function_source(app_js, "codeBlockWithCopy")
    assert "copyToClipboard(text || \"\").then(function (ok) {" in code_block
    assert 'button.textContent = ok ? "Copied" :' in code_block


def test_health_banner_skips_rebuilding_when_nothing_shown_would_change() -> None:
    """renderHealthBanner is an aria-live="polite" region polled every
    3-60s (pollHealth); it used to clear() and rebuild its children on
    every single poll even when the message was identical, which some
    screen readers re-announce as if it were new content."""
    app_js = _app_js()
    fn = _function_source(app_js, "renderHealthBanner")
    assert 'banner.getAttribute("data-render-sig") === sig) return' in fn
    assert 'banner.setAttribute("data-render-sig", sig)' in fn
    # The guard's early return must come before the rebuild, not after.
    assert fn.index('=== sig) return') < fn.index("clear(banner)")


def test_capture_banner_also_skips_rebuilding_when_unchanged() -> None:
    app_js = _app_js()
    fn = _function_source(app_js, "renderCaptureBanner")
    assert 'banner.getAttribute("data-render-sig") === sig) return' in fn
    assert 'banner.setAttribute("data-render-sig", sig)' in fn


def test_capture_banner_dismissal_is_a_seven_day_snooze_not_permanent() -> None:
    """A dismissed notes list used to store a bare "1" forever (or would
    have) -- once hidden, hidden for good, even after the notes
    themselves changed. UX-6/9 wants a 7-day snooze instead, so a quiet
    banner returns on its own. (The capture invite and its own snooze
    are gone: the sidebar's status line says the level instead.)"""
    app_js = _app_js()
    assert re.search(r"(?:export\s+)?(?:var|let|const) BANNER_SNOOZE_MS = 7 \* 24 \* 60 \* 60 \* 1000;", app_js)
    notes_fn = _function_source(app_js, "notesSnoozed")
    assert "Date.now() - ts < BANNER_SNOOZE_MS" in notes_fn
    banner_fn = _function_source(app_js, "renderCaptureBanner")
    assert 'storageSet("tls:captureNotesHidden", Date.now() + "|" + notesSignature(notes))' in banner_fn
    # No permanent "1" write for the dismissal, and no invite left to hide.
    assert '"tls:captureNotesHidden", "1"' not in app_js
    assert "tls:captureInviteHidden" not in app_js


def test_empty_state_helper_exists_and_is_used_for_not_enough_data_states() -> None:
    """One consistent "not enough data yet" box (P4's leftover
    emptyState() helper) instead of each tab building its own ad hoc
    paragraph, and it folds in the structured {reason, have, need}
    ``gate`` object api.py now attaches to /api/impact's per-change
    rows when a helper has one."""
    app_js = _app_js()
    helper = _function_source(app_js, "emptyState")
    assert "gate.have" in helper and "gate.need" in helper

    check_detail = _function_source(app_js, "renderCheckDetail")
    assert "emptyState(check.summary)" in check_detail

    assert "emptyState(item.verdict, item.gate)" in _function_source(app_js, "changeCard")
    assert "emptyState(\n        \"No changes recorded yet" in _function_source(app_js, "renderChangeCards")

    backtest_fn = _function_source(app_js, "renderBacktest")
    assert "emptyState(\"No estimates logged yet" in backtest_fn



def test_service_text_amounts_are_brought_in_line_as_they_arrive() -> None:
    """Phase 4: the service writes an amount the CLI's way ("1,962.05
    USD"); the dashboard writes "$1,962.05" everywhere. fetchJson runs
    every response through format.js's readableAmounts, so text from any
    route reads like the dashboard's own amounts, and a unit in a label
    reads "($)". Other currencies already read the same both ways."""
    app_js = _app_js()
    fetch_source = _function_source(app_js, "fetchJson")
    assert "readableAmounts(body)" in fetch_source
    format_js = _static_text("format.js")
    service_usd = re.search(r"var SERVICE_USD = /(.+)/g;", format_js)
    assert service_usd, "format.js should define SERVICE_USD"
    assert service_usd.group(1).endswith(r") USD\b"), service_usd.group(1)  # "1,962.05 USD", not "USD prices"
    assert "\u2212" in service_usd.group(1), "a true minus sign before the number is carried over"
    assert r"var SERVICE_USD_UNIT = /\(USD\)/g;" in format_js
    helper = _function_source(app_js, "readableAmounts")
    assert 'if (value.indexOf("USD") === -1) return value;' in helper
    assert "Object.keys(value).forEach" in helper  # values only: keys stay as the service sent them


def test_overlays_give_focus_back_to_what_opened_them() -> None:
    """Phase 4: the drawer, the confirm dialog and every popover return
    focus to the control that opened them, and a popover moves focus to
    its first control that can take it (a disabled box can't: the
    chooser's first column always shows, so its box is disabled)."""
    app_js = _app_js()
    for name in ("drawer", "confirmDialog"):
        source = _function_source(app_js, name)
        assert "var opener = document.activeElement;" in source, name
        assert "opener.isConnected && opener.focus" in source, name
    popover = _function_source(app_js, "popoverButton")
    assert "anchorButton.focus()" in popover
    assert "input:not([disabled])" in popover and "button:not([disabled])" in popover


def test_evidence_row_pulse_moves_only_opacity_and_holds_under_reduced_motion() -> None:
    """An evidence link's target row glows and fades. UI motion is
    transform and opacity only, so the glow is a layer whose opacity
    moves, never the cell background. Under reduced motion the glow holds
    still until the next click or key, instead of vanishing on a timer
    before a slow reader finds the row."""
    app_css = _static_text("app.css")
    keyframes = re.search(r"@keyframes row-pulse \{(.*?)\n\}", app_css, re.S)
    assert keyframes, "row-pulse keyframes missing"
    properties = set(re.findall(r"([a-z-]+)\s*:", keyframes.group(1)))
    assert properties == {"opacity"}, properties
    assert re.search(r"tr\.row-target > td::before \{[^}]*animation: row-pulse", app_css)
    reduced = app_css[app_css.index("@media (prefers-reduced-motion: reduce)") :]
    assert re.search(r"tr\.row-target > td::before \{\s*opacity: 1;\s*animation: none;", reduced)
    # pulseRow finds the row; pulseNode (shared with the scorecard's
    # block pulse) runs the glow.
    assert 'pulseNode(row, "row-target")' in _function_source(_app_js(), "pulseRow")
    assert re.search(r"\.block-target::after \{\s*opacity: 1;\s*animation: none;", reduced)
    pulse = _function_source(_app_js(), "pulseNode")
    assert "if (motionOK())" in pulse
    assert 'addEventListener("pointerdown", clearTarget, true)' in pulse
    assert 'addEventListener("keydown", clearTarget, true)' in pulse


# -- charts (charts.js, charts-types.js) ------------------------------------


#: docs/ui.md's chart catalogue: the only charts the dashboard draws, by
#: catalogue row. A new chart needs a row there first.
_CHART_CATALOGUE = {
    "daily-spend": 1,
    "savings-levers": 2,
    "summary-point": 3,
    "session-outliers": 4,
    "session-context": 5,
    "idle-gaps": 6,
    "lifetime-by-agent": 7,
    "startup-context": 8,
    "change-timeline": 9,
}


def _chart_specs() -> dict:
    source = _declaration_source(_app_js(), "CHART_SPECS")
    return _js_literal_to_json(source[source.index("{") :])


def _chart_forms() -> set[str]:
    source = _declaration_source(_app_js(), "FORMS")
    return set(re.findall(r'^\s*"?([\w-]+)"?\s*:', source, re.MULTILINE))


def test_chart_specs_are_exactly_the_catalogue() -> None:
    """The catalogue is closed: CHART_SPECS lists catalogue rows 1-9 and
    nothing else, each titled with the question it answers and read out
    by a summary built from its figures."""
    specs = _chart_specs()
    assert {key: spec["n"] for key, spec in specs.items()} == _CHART_CATALOGUE
    forms = _chart_forms()
    for key, spec in specs.items():
        assert spec["title"].endswith("?"), key
        assert re.search(r"\{\w+\}", spec["summary"]), key
        assert spec["form"] in forms, f"{key}: no chart type draws form {spec['form']!r}"
        for variant, text in spec.get("alt", {}).items():
            assert text.endswith("."), (key, variant)


def test_every_chart_source_is_a_route_or_a_report_table() -> None:
    """A chart's figures come from a documented route or from a table the
    report really builds, so the catalogue can't name data that doesn't
    exist."""
    api_py = (REPO_ROOT / "src" / "claudeglass" / "service" / "api.py").read_text(encoding="utf-8")
    python = "\n".join(p.read_text(encoding="utf-8") for p in (REPO_ROOT / "src" / "claudeglass").rglob("*.py"))
    for key, spec in _chart_specs().items():
        sources = spec["source"] if isinstance(spec["source"], list) else [spec["source"]]
        for source in sources:
            if source.startswith("/api/"):
                # A route with an id is matched by a pattern (^/api/session/...).
                prefix = source.split("<")[0]
                assert '"' + prefix in api_py or "^" + prefix in api_py, f"{key}: no route {source}"
            else:
                section, table = source.split(".")
                assert re.search(r'["\']' + re.escape(table) + r'["\']', python), f"{key}: no report table {table}"
                assert re.search(r'["\']' + re.escape(section) + r'["\']', python), f"{key}: no report section {section}"


def test_charts_are_drawn_only_through_render_chart_and_the_catalogue() -> None:
    """Every page draws a chart with renderChart and a catalogued key;
    only the chart types reach drawChart itself."""
    specs = _chart_specs()
    for path in _js_modules():
        text = path.read_text(encoding="utf-8")
        for key in re.findall(r'renderChart\(\s*[\w.]+\s*,\s*"([\w-]+)"', text):
            assert key in specs, f"{path.name} draws uncatalogued chart {key!r}"
        if path.name not in ("charts.js", "charts-types.js"):
            assert "drawChart(" not in text, f"{path.name} calls drawChart; use renderChart"
    assert re.findall(r'renderChart\(\s*[\w.]+\s*,\s*"([\w-]+)"', _app_js()), "no chart is drawn anywhere"


def test_every_chart_has_a_table_view_and_a_reading() -> None:
    """Colour is never the only way in: every chart frame carries a Table
    toggle with the same figures, and every chart type returns a table
    and the facts its summary sentence is built from."""
    frame = _function_source(_app_js(), "buildFrame")
    assert '"Show as table"' in frame and '"aria-pressed"' in frame
    assert '"Show as chart"' in _function_source(_app_js(), "showTable")
    source = _app_js()
    forms = _declaration_source(source, "FORMS")
    for name in re.findall(r":\s*(\w+),?\s*$", forms, re.MULTILINE):
        body = _function_source(source, name)
        assert "table: {" in body, f"{name} returns no table view"
        assert "facts: {" in body, f"{name} returns no facts for its summary"
        assert "empty:" in body, f"{name} has no reasoned empty state"


def test_bars_stay_thin_and_charts_hold_the_minimum_points() -> None:
    """Thin marks (24px at most) and no chart for fewer than three points:
    below that the page shows tiles instead."""
    source = _app_js()
    assert re.search(r"var MAX_BAR = (\d+);", source) and int(re.search(r"var MAX_BAR = (\d+);", source).group(1)) <= 24
    assert int(re.search(r"var BAR = (\d+);", source).group(1)) <= 24
    assert "var MIN_POINTS = 3;" in source
    for name in ("bars", "line", "histogram", "diverging", "stackedBars", "scatter"):
        assert "MIN_POINTS" in _function_source(source, name), name


def test_a_replaced_chart_lets_go_and_a_fresh_one_starts_as_a_chart() -> None:
    """A chart replaced in its slot (the next session opened, or an error)
    disconnects its resize observer and highlight listener, so frames
    don't pile up; a fresh chart drops the last one's table choice; a
    table view measures its own width, so it follows new figures while
    the plot is hidden; and new figures clear the scatter's brush and
    tell the page, so its grid isn't left filtered to a range the chart
    no longer shows."""
    source = _app_js()
    retire = _function_source(source, "retireFrame")
    assert "observer.disconnect()" in retire and "stopHighlight()" in retire
    draw = _function_source(source, "drawChart")
    assert "retireFrame(frame)" in draw and "delete tableShown[slot]" in draw
    assert "plotWidth(frame)" in draw
    assert "retireFrame(" in _function_source(source, "chartError")
    assert "frame.tableHost" in _function_source(source, "plotWidth")
    scatter = _function_source(source, "scatter")
    assert "opts.brushed(null)" in scatter


def test_money_axis_mirrors_the_billing_units() -> None:
    """A money axis says what its numbers are in the same words units.py
    uses: a share of the usage limit, list-price dollars, or dollars."""
    units_py = (REPO_ROOT / "src" / "claudeglass" / "units.py").read_text(encoding="utf-8")
    assert "% of your weekly usage limit" in units_py and "list-price" in units_py
    axis = _function_source(_app_js(), "moneyAxis")
    assert '"% of your "' in axis and '"weekly usage limit"' in axis
    assert '"list-price "' in axis
    assert "share_per_usd" in axis


def test_chart_colours_follow_the_entity_not_the_window() -> None:
    """A thing keeps its colour everywhere: the maps are fixed, and
    whatever doesn't fit falls to Other in grey."""
    source = _declaration_source(_app_js(), "ENTITY_COLOURS").split("=", 1)[1]
    colours = _js_literal_to_json(re.sub(r"^\s*//.*$", "", source, flags=re.MULTILINE))
    for kind, mapping in colours.items():
        assert mapping.get("other") == "var(--chart-other)" or kind == "agent", kind
        for value in mapping.values():
            assert re.fullmatch(r"var\(--chart-(?:[1-8]|other)\)", value), (kind, value)


# -- Actions: areas, evidence links and deep links ---------------------------

SRC_DIR = REPO_ROOT / "src" / "claudeglass"


def _js_hyphen_map(app_js: str, var_name: str) -> dict[str, str]:
    """A declaration's "key": "value" pairs whose keys may hold hyphens
    (rule ids)."""
    source = _declaration_source(app_js, var_name)
    return dict(re.findall(r'^\s*"?([a-z0-9_./-]+)"?\s*:\s*"([^"]*)"', source, re.MULTILINE))


def _recommendation_ids() -> set[str]:
    """Every literal ``id="..."`` a ``Recommendation(...)`` call passes in
    the package. quick_actions.py builds one only to render a fix, never
    to send, so its placeholder id is left out."""
    ids: set[str] = set()
    for path in SRC_DIR.rglob("*.py"):
        if path.name == "quick_actions.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Recommendation":
                for kw in node.keywords:
                    if kw.arg == "id" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                        ids.add(kw.value.value)
    return ids


def _evidence_sources() -> set[tuple[str, str]]:
    """Every (section, table) an ``_evidence(label, value, section,
    table, row)`` call names. Each must be a literal, so this scan sees
    every place a recommendation can point."""
    sources: set[tuple[str, str]] = set()
    for path in SRC_DIR.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_evidence":
                section, table = node.args[2], node.args[3]
                assert isinstance(section, ast.Constant) and isinstance(table, ast.Constant), (
                    f"{path.name}:{node.lineno} names its evidence table indirectly"
                )
                sources.add((section.value, table.value))
    return sources


def test_every_recommendation_rule_has_an_area_on_the_actions_page() -> None:
    """The area chips (Models, Cache, Context, Agents, Habits, Data and
    settings) come from page-actions.js's RULE_AREA, since a
    recommendation's category only says settings, workflow or data. A
    new rule id with no area would fall into "Data and settings"
    silently; every one is placed on purpose, and only on an area the
    filter row has."""
    app_js = _app_js()
    areas = _js_hyphen_map(app_js, "RULE_AREA")
    ids = _recommendation_ids()
    assert len(ids) > 30, ids
    assert sorted(ids - set(areas)) == []
    assert sorted(set(areas) - ids) == [], "RULE_AREA names a rule no module sends"
    area_ids = set(re.findall(r'\{ id: "([a-z]+)", label: "', _declaration_source(app_js, "AREAS")))
    assert set(areas.values()) <= area_ids
    # Every rule with a "what the change does" sentence has an area too.
    mechanisms = _js_hyphen_map(app_js, "RULE_MECHANISM")
    assert set(mechanisms) <= set(areas)


def test_every_evidence_source_resolves_to_a_page_or_the_table_drawer() -> None:
    """Each evidence entry names a report table; its link opens the page
    that shows it (TABLE_PAGE_MAP, then SECTION_PAGE_MAP) or, for a
    section no page shows, the table drawer. None may point at a
    section that would fall through to Data quality by accident."""
    from claudeglass.report import _SECTION_ORDER

    app_js = _app_js()
    sections = _js_string_map(app_js, "SECTION_PAGE_MAP")
    tables = _js_string_map(app_js, "TABLE_PAGE_MAP")
    sources = _evidence_sources()
    assert len(sources) > 20, sources
    package = "".join(path.read_text(encoding="utf-8") for path in SRC_DIR.rglob("*.py"))
    for section, table in sorted(sources):
        # The table is a real one: its name is written somewhere.
        assert '"' + table + '"' in package, (section, table)
        if section + "." + table in tables or section in sections:
            continue
        # Only a section the report doesn't list (a dormant one) may
        # rely on the drawer alone.
        assert section not in _SECTION_ORDER, (section, table)
    evidence = _function_source(app_js, "evidenceView")
    assert "TABLE_PAGE_MAP[sourceTable]" in evidence and "SECTION_PAGE_MAP[parts.section]" in evidence
    assert 'dashboard === "report"' in evidence
    opener = _function_source(app_js, "openEvidence")
    assert "tableDrawer(sourceTable, rowKey)" in opener
    # A page that doesn't draw the table (not for this window) hands the
    # link to the drawer too.
    assert "tableDrawer(sourceTable, rowKey)" in _function_source(app_js, "revealEvidence")
    # Tables carry the name and row the links look for; the scorecard is
    # one, folded under the Overview's answers.
    assert '"data-table-name": table.name || null' in _function_source(app_js, "renderTable")
    details = _function_source(app_js, "renderDetails")
    assert 'tableNamed(findSection(report, "scorecard"), "dimensions")' in details and "renderTable(scores," in details


def test_recommendations_and_checks_open_from_the_address() -> None:
    """#/actions/recommendations?id=<key> opens that recommendation (a
    member's key opens its group), and #/actions/checks?id=<check> that
    check. The address follows what is picked, the router hands a new
    id to the open page, and links from the Overview and between checks
    and recommendations carry the id."""
    app_js = _app_js()
    for view in ("actions/recommendations", "actions/checks"):
        assert 'onParams("' + view + '"' in app_js, view
    inbox_source = _function_source(app_js, "inbox")
    assert "replaceParams({ id: memberKey || item.key })" in inbox_source
    assert "formatHash(spec.viewKey, Object.assign(scopeParams(), { id: item.key }))" in inbox_source
    assert "paramsChanged(key, extra)" in _function_source(app_js, "resolveRoute")
    assert "params" in _function_source(app_js, "pageLink")
    assert "history.replaceState" in _function_source(app_js, "replaceParams")
    overview = _function_source(app_js, "checklistRow")
    assert 'pageLink("actions/recommendations", lead.members.length > 1 ? "See the " + lead.members.length + " prompts" : "See the fix", { id: lead.key })' in overview
    assert 'pageLink("actions/checks", "See the check", { id: check.id })' in overview
    assert 'pageLink("actions/checks", check.question, { id: check.id })' in _function_source(app_js, "renderRecommendationDetail")
    assert 'pageLink("actions/recommendations", groupTitle(group), { id: group.key })' in _function_source(app_js, "renderCheckDetail")
    # An id this window doesn't have says so, instead of opening nothing.
    assert "missingNote(" in _function_source(app_js, "renderRecommendations")
    assert "missingNote(" in _function_source(app_js, "renderQuickActions")


# -- Phase 9: the Spend and Cache pages --------------------------------------


def test_a_section_draws_its_catalogued_chart_through_the_grid_hook() -> None:
    """A report section whose table a catalogue chart reads (compaction
    summaries, idle gaps, lifetime by agent, startup context) draws that
    chart between its intro and its tables. grid.js can't import the
    charts, so app.js hands it charts-types.js's sectionChart."""
    app_js = _app_js()
    assert "setSectionChart(sectionChart)" in _function_source(app_js, "init")
    generic = _function_source(app_js, "renderSectionGeneric")
    assert "sectionChart(section)" in generic
    assert generic.index("section.intro") < generic.index("sectionChart(section)") < generic.index("renderPlacedTables(")
    chart = _function_source(app_js, "sectionChart")
    assert "CHART_SPECS" in chart and 'slot: "section"' in chart
    specs = _chart_specs()
    drawn = {key for key, spec in specs.items() if isinstance(spec.get("source"), str) and "." in spec["source"]}
    assert drawn == {"summary-point", "idle-gaps", "lifetime-by-agent", "startup-context"}
    # A mark leads to its row's action, or to its row in the table below.
    leads = _function_source(app_js, "markLeads")
    assert 'goTo("actions/recommendations"' in leads and "t: source, row:" in leads


def test_a_one_row_table_reads_as_tiles_with_every_figure_one_click_away() -> None:
    """A one-row summary table with lead columns shows at most four of
    them as tiles, amounts in the billing mode, and the rest in an
    "All figures" disclosure that an evidence link opens."""
    app_js = _app_js()
    assert "var STRIP_TILES = 4;" in app_js
    columns = _function_source(app_js, "summaryColumns")
    assert "table.rows.length !== 1" in columns or "rows.length === 1" in columns or "length !== 1" in columns
    assert "lead_columns" in columns
    # A list that has one row today (one agent type) stays a grid: only a
    # table whose row key isn't a headline reads as tiles.
    assert "table.lead_columns.indexOf(table.columns[0].key) !== -1) return null" in columns
    tile_source = _function_source(app_js, "summaryTile")
    assert "moneyParts(" in tile_source
    render = _function_source(app_js, "renderTable")
    assert '"All figures (' in render and "summary-details" in render
    assert 'querySelector("details.summary-details")' in _function_source(app_js, "revealEvidence")


def test_sessions_list_follows_the_scatter_brush_and_a_picked_day() -> None:
    """Spend > Sessions: the scatter's time brush and a ?day= from a
    daily spend chart narrow the list; "Show all sessions" clears both.
    One fetch covers the window: there is no pager any more."""
    app_js = _app_js()
    sessions = _function_source(app_js, "renderSessions")
    assert 'renderChart(chartHost, "session-outliers"' in sessions
    assert "brushed:" in sessions and "openSessionDrawer(row.id)" in sessions
    assert 'onParams("spend/sessions"' in sessions
    assert '"Show all sessions"' in sessions
    assert "replaceParams(" in sessions and "day: null" in sessions
    assert "sessionsState" not in app_js and '"Previous"' not in sessions
    shown = _function_source(app_js, "shownSessions")
    assert "first_ts" in shown and "last_ts" in shown
    assert r"/^\d{4}-\d\d-\d\d$/" in _function_source(app_js, "validDay")
    # Rows light up with their dot and carry its colour.
    table = _function_source(app_js, "renderSessionsTable")
    assert 'scope: "session"' in table and "modeColour(row.mode)" in table
    # A day on either daily spend chart leads here.
    assert 'goTo("spend/sessions", { params: { day: day } })' in _function_source(app_js, "renderOverview")
    assert 'goTo("spend/sessions", { params: { day: day } })' in _function_source(app_js, "renderUsage")


def test_usage_daily_chart_splits_by_agent_or_model_from_the_address() -> None:
    """Spend > Usage draws chart 1 with a "Split by" choice kept in the
    address (?split=model), marked with aria-pressed, and follows Back
    and Forward."""
    app_js = _app_js()
    usage = _function_source(app_js, "renderUsage")
    assert 'renderChart(' in usage and '"daily-spend"' in usage and 'slot: "usage"' in usage
    assert '"&split=" + wanted' in usage
    assert '"aria-pressed"' in usage and "filter-chip" in usage
    assert 'onParams("spend/usage"' in usage
    assert "split: option.value" in usage
    assert "dailyChanges(" in usage and "dailyChanges(" in _function_source(app_js, "renderOverview")


def test_savings_levers_chart_leads_to_each_levers_row() -> None:
    """Spend > Savings opens with chart 2, built from the same four
    responses as its sections; a lever leads to the row its figure
    comes from."""
    savings = _function_source(_app_js(), "renderSavings")
    assert "Promise.all(loads)" in savings
    assert 'renderChart(chartHost, "savings-levers", savingsLevers(tables)' in savings
    assert 'goTo("spend/savings", { params: { t: lever.source, row: lever.row } })' in savings


def test_cache_page_opens_with_what_the_cache_does_for_you() -> None:
    """Cache > Rebuilds opens with three facts in your own numbers: what
    reading from the cache saved, what avoidable rebuilds cost, and how
    many agent types a 1-hour lifetime would help. Multipliers come
    from the report's rates, never typed in."""
    app_js = _app_js()
    explainer = _function_source(app_js, "renderCacheExplainer")
    for table in ("ttl_cache_economy", "recache_summary", "ttl_break_even_share"):
        assert '"' + table + '"' in explainer, table
    for field in ("net_saving_usd", "avoidable_cost_usd", "margin"):
        assert field in explainer, field
    # The rebuild count reads the per-cause turns through costs.js.
    assert "avoidableRebuilds(report)" in explainer
    count = _function_source(app_js, "avoidableRebuilds")
    assert '"recache_signature_split"' in count and 'keys.indexOf("turns")' in count
    for ratio in ("cache_read_ratio", "cache_write_5m_ratio", "cache_write_1h_ratio"):
        assert "fraction(rates." + ratio + ")" in explainer, ratio
    assert "moneyParts(" in explainer
    assert 'pageLink("cache/lifetime"' in explainer
    assert "renderCacheExplainer(result.report, explainer)" in _function_source(app_js, "renderCache")


def test_long_table_notes_fold_away() -> None:
    """Notes that say how figures were worked out fold into a disclosure
    once there are more than two, or they run long, so a page stays
    short; one or two short notes stay in view."""
    notes = _function_source(_app_js(), "notesList")
    assert "var NOTES_IN_VIEW_CHARS = 240;" in _app_js()
    assert "notes.length <= 2 && length <= NOTES_IN_VIEW_CHARS" in notes
    assert '"How these figures are worked out"' in notes
    # A table's own notes say so, so they don't read like its section's.
    assert '"How this table is worked out"' in notes


def test_links_inside_a_drawer_work_and_close_it() -> None:
    """A drawer is a modal <dialog>, so the page behind it is inert: a
    popover opened inside it joins the dialog, and a link to another
    view closes the drawer without pulling focus back to its opener. A
    help popover holding a link takes focus, so Tab can reach it."""
    app_js = _app_js()
    popover = _function_source(app_js, "popoverButton")
    assert '(anchorButton.closest("dialog") || document.body).appendChild(pop)' in popover
    assert 'opts.focusInside !== false || pop.querySelector("a[href]")' in popover
    drawer = _function_source(app_js, "drawer")
    assert "a[href^='#/']" in drawer and "close(true)" in drawer
    assert "leaving !== true" in drawer


# -- Phase 11: search (Ctrl+K) and the keyboard shortcuts ----------------------


def test_search_lists_every_page_and_segment() -> None:
    """Search offers every view the sidebar has, named as the sidebar
    names it: pageEntries walks links.js VIEW_KEYS, so a page added
    there is found with nothing more to do. app.js lends it the window
    and theme controls."""
    source = _static_text("palette.js")
    pages = _function_source(source, "pageEntries")
    assert "VIEW_KEYS.map(" in pages
    assert "viewLabel(key)," in pages
    assert "goTo(key, { focus: true });" in pages
    app = _static_text("app.js")
    assert 'import { initPalette } from "./palette.js";' in app
    assert "initPalette({ setWindow: setWindow, setTheme: setTheme });" in _function_source(app, "init")
    # Recent sessions open their drawer from any page.
    assert "export function openSessionDrawer(" in _static_text("page-spend.js")


def test_go_keys_cover_every_main_page() -> None:
    """G then a letter opens each of the sidebar's main pages (Data
    quality and the Glossary, in its foot, are a search away), and the
    shortcut sheet lists the same letters."""
    source = _static_text("palette.js")
    match = re.search(r"export var GO_KEYS = \{([^}]*)\};", source)
    assert match, "palette.js declares GO_KEYS"
    keys = dict(re.findall(r'(\w): "([a-z-]+)"', match.group(1)))
    links = _static_text("links.js")
    block = links[links.index("export var PAGES = ["):links.index("export function findPage")]
    main = []
    for chunk in re.split(r"\n  \{\n", block)[1:]:
        page_id = re.match(r'\s*id: "([a-z-]+)"', chunk).group(1)
        if "\n    foot: true" not in chunk:
            main.append(page_id)
    assert sorted(keys.values()) == sorted(main)
    assert "Object.keys(GO_KEYS).map(" in source[source.index("var SHORTCUTS = ["):]


def test_search_is_a_combobox_in_a_modal_dialog() -> None:
    """Focus stays in the search box while the arrow keys move
    aria-activedescendant through a listbox, in a modal <dialog>: Esc
    closes it and focus goes back to what opened it. The list says it is
    busy until the window's actions, tables and sessions arrive."""
    palette = _function_source(_static_text("palette.js"), "openPalette")
    assert 'role: "combobox",' in palette
    assert 'role: "listbox",' in palette
    assert 'role: "option",' in palette
    assert 'input.setAttribute("aria-activedescendant", options[index].id);' in palette
    assert '"aria-busy": "true"' in palette
    assert 'list.removeAttribute("aria-busy");' in palette
    assert "dialog.showModal();" in palette
    assert 'dialog.addEventListener("cancel",' in palette
    assert "opener.focus()" in palette


def test_shortcuts_leave_typing_and_open_panels_alone() -> None:
    """A key typed in a text box, pressed while a panel, menu or popover
    is open, or pressed with Ctrl, Alt or the Windows/Command key is not
    a shortcut (Ctrl+K, which opens search, apart)."""
    source = _static_text("palette.js")
    keydown = _function_source(source, "onKeydown")
    assert "if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey) return;" in keydown
    assert "if (typing(event.target) || overlayOpen()) {" in keydown
    assert keydown.index("(event.ctrlKey || event.metaKey)") < keydown.index("event.defaultPrevented")
    assert 'target.closest("input, textarea, select, [contenteditable]' in _function_source(source, "typing")
    overlay = _function_source(source, "overlayOpen")
    assert 'document.querySelector("dialog[open]")' in overlay
    assert '":popover-open"' in overlay


def test_search_only_moves_around_and_copies() -> None:
    """Like every other part of the dashboard, search never changes
    Claude Code: it reads, opens pages and copies prompts. No write
    route, and nothing named Apply."""
    source = _static_text("palette.js")
    assert "postJson" not in source
    assert "method:" not in source
    assert not re.search(r"apply", source, re.IGNORECASE)
    assert "copyToClipboard(prompt)" in _function_source(source, "recommendationEntries")



def test_models_read_by_name_on_screen() -> None:
    """A model id in a table cell, a check's evidence or a change's
    current value reads as its name ("Sonnet 5 (+3 more)", "Haiku 4.5"),
    with the id on hover. Display only: the id stays the row key, the
    evidence key and the sort value. Only Claude model ids match, so an
    agent or folder named claude-something is left alone."""
    fmt = _static_text("format.js")
    match = re.search(r"var MODEL_ID = /(.+)/g;", fmt)
    assert match
    model_id = re.compile(match.group(1))
    for text, ids in (
        ("claude-sonnet-5 (+3 more)", ["claude-sonnet-5"]),
        ("not set (used claude-opus-5-5 (+1 more))", ["claude-opus-5-5"]),
        ("claude-haiku-4-5-20251001", ["claude-haiku-4-5-20251001"]),
        ("claude-3-5-sonnet-20241022", ["claude-3-5-sonnet-20241022"]),
        ("claude-opus-5-5[1m]", ["claude-opus-5-5[1m]"]),
        ("claude-implementer", []),
        ("C--Dev-claudeglass", []),
    ):
        assert [m.group(0) for m in model_id.finditer(text)] == ids, text
    assert '" (1M context)"' in _function_source(fmt, "modelNames")
    # Every place server text shows a model goes through it.
    assert "return modelNames(String(value));" in _function_source(fmt, "formatCell")
    grid = _static_text("grid.js")
    cell = _function_source(grid, "cellContent")
    assert 'kind === "str") return el("span", { title: value, text: plainText(text) });' in cell
    table = _function_source(grid, "simpleTable")
    assert "var text = modelNames(String(cell));" in table
    assert 'el("span", { title: String(cell), text: text })' in table
    assert "return modelNames(String(value));" in _function_source(_static_text("page-actions.js"), "valueText")

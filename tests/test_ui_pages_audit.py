"""Design-audit fixes on Cache > Rebuilds, Setup, Actions > Checks, the
Glossary, evidence cards and the status line.

Each test holds one fix in place, read from the dashboard's source the
way ``test_service_static.py`` and ``test_cost_cards.py`` read it (there
is no JS runtime in this suite):

- one count per figure on Cache > Rebuilds: no all-time cause strip
  between the window's blocks, an explainer that says what its count
  is, and "Total pause time" shown once;
- every form control and repeated button has a name of its own;
- a check's "Worth a look" is amber, like the recommendations it leads
  to, not red;
- a glossary term keeps the punctuation beside it on its line;
- evidence is a list parted by hairlines, not cards in a panel;
- Glossary > Terms has a labelled filter;
- the status line reads relative times and is a named group;
- the fixed-window chip and the all-time chips say what is true.
"""

from __future__ import annotations

import re
from pathlib import Path

from claudeglass import helptext

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = REPO_ROOT / "src" / "claudeglass" / "service" / "static"


def _static_text(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


# -- tiny JS source readers (kept local, as each UI test module does) --


def _skip_js_string_or_comment(src: str, i: int) -> int:
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
    pairs = {"{": "}", "(": ")", "[": "]"}
    stack: list[str] = []
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


def _function_source(src: str, name: str) -> str:
    match = re.search(r"(?<![\w$.])function\s+" + re.escape(name) + r"\s*\(", src)
    assert match, f"no function {name}()"
    params_end = _balanced_end(src, match.end() - 1)
    body_start = src.index("{", params_end)
    return src[match.start() : _balanced_end(src, body_start)]


def _css_rule(css: str, selector: str) -> str:
    """The body of the first rule whose whole selector list is
    ``selector``."""
    match = re.search(r"(?:^|\})\s*" + re.escape(selector) + r"\s*\{([^{}]*)\}", css, re.MULTILINE)
    assert match, f"app.css has no rule for {selector!r}"
    return match.group(1)


# -- Cache > Rebuilds: one count per figure --------------------------------


def test_rebuilds_has_no_all_time_cause_strip_between_window_blocks() -> None:
    """The strip counted every rebuild ever (/api/recache) between blocks
    that follow the window, under other names than the server's. The
    window's own "Why the cache was rebuilt" table is the one breakdown;
    All time in the picker gives the whole history."""
    cache = _static_text("page-cache.js")
    assert "/api/recache" not in cache
    assert "Cache expired while idle" not in cache
    assert "Cache invalidated by a change" not in cache
    assert "all-time-chip" not in cache
    labels = helptext.TABLE_COPY["recache_signature_split"].value_labels
    assert labels["full-expiry"] == "Cache expired"
    assert labels["prefix-invalidated"] == "Cache broken by a change"


def test_rebuilds_explainer_says_its_count_is_the_avoidable_ones() -> None:
    """It used to say "The cache was rebuilt 431 times" beside a tile of
    457: 431 is only the avoidable rebuilds."""
    cache = _static_text("page-cache.js")
    assert "The cache was rebuilt" not in cache
    explainer = _function_source(cache, "renderCacheExplainer")
    assert "avoidableRebuilds(report)" in explainer
    assert "rebuilds.recache_turns" in explainer
    sentence = _function_source(cache, "avoidableSentence")
    assert "were avoidable" in sentence
    assert "usage-limit pause" in sentence


def test_total_pause_time_is_a_tile_once_on_rebuilds() -> None:
    summary = helptext.TABLE_COPY["limits_summary"].lead_columns
    pauses = helptext.TABLE_COPY["limits_pauses"].lead_columns
    assert "total_s" in pauses
    assert "pause_total_s" not in summary


# -- named controls ------------------------------------------------------------


def test_profile_json_box_has_a_label_and_the_note_as_its_description() -> None:
    setup = _static_text("page-setup.js")
    assert re.search(r'el\("label", \{ for: "profile-form-json", text: "Profile as JSON" \}\)', setup)
    assert '"aria-describedby": "profile-form-json-note"' in setup
    assert 'id: "profile-form-json-note"' in setup


def test_repeated_setup_buttons_name_their_card() -> None:
    """Eight "Start here" and seven "Show what it changes" buttons read
    the same to a screen reader. Each name starts with the words on the
    button (WCAG 2.5.3), then says which card."""
    setup = _static_text("page-setup.js")
    assert 'button("Start here", { label: "Start here: " + goal.title })' in setup
    assert 'label: "Show what it changes: " + (profile.name || profile.id)' in setup


def test_copy_buttons_are_named_for_what_they_copy() -> None:
    ui = _static_text("ui.js")
    block = _function_source(ui, "codeBlockWithCopy")
    assert "function codeBlockWithCopy(text, what, about, onCopy)" in block
    assert '"aria-label": "Copy " + kind + (about ? " for " + about : "")' in block
    command = _function_source(ui, "commandBlock")
    assert command.count("fixSubject(fix)") == 3
    habits = _static_text("page-habits.js")
    assert 'codeBlockWithCopy(row.example, "Example", ' in habits
    assert 'codeBlockWithCopy(row.template || "", "Template", ' in habits
    # Every caller that shows several blocks on one page says which.
    for name in ("page-habits.js", "page-setup.js"):
        assert not re.search(r"codeBlockWithCopy\([^,()]*(?:\([^()]*\))?[^,()]*\)", _static_text(name)), name


# -- Actions > Checks: quieter status ------------------------------------------------


def test_a_check_worth_a_look_is_amber_with_its_icon_and_words() -> None:
    ui = _static_text("ui.js")
    match = re.search(r"var CHECK_STATUS = \{(.*?)\n\};", ui, re.DOTALL)
    assert match
    act = re.search(r"act: \{([^}]*)\}", match.group(1)).group(1)
    assert 'label: "Worth a look"' in act
    assert 'cls: "severity-advice"' in act
    assert 'icon: "warning"' in act
    assert "severity-action" not in match.group(1)


# -- glossary terms keep their punctuation ------------------------------------------


def test_a_term_is_wrapped_with_the_punctuation_beside_it() -> None:
    ui = _static_text("ui.js")
    nodes = _function_source(ui, "termNodes")
    assert "TRAILING_MARKS" in nodes and "LEADING_MARKS" in nodes
    assert 'el("span", { class: "term-wrap" }' in nodes
    assert "jargonPattern.lastIndex = last" in nodes
    assert re.search(r"var TRAILING_MARKS = /\^\[\.,;:", ui)
    assert "white-space: nowrap" in _css_rule(_static_text("app.css"), ".term-wrap")


# -- evidence: a list, not nested cards -----------------------------------------------


def test_evidence_items_are_hairline_rows_not_cards() -> None:
    css = _static_text("app.css")
    item = _css_rule(css, ".evidence-item")
    for banned in ("background", "border-radius", "border:"):
        assert banned not in item, banned
    assert "border-top: 1px solid var(--border-subtle)" in _css_rule(css, ".evidence-item + .evidence-item")
    listing = _css_rule(css, ".evidence-list,\n.view .evidence-list")
    assert "padding: 0" in listing


# -- Glossary > Terms: a filter ---------------------------------------------------------


def test_terms_has_a_labelled_filter_with_an_empty_state() -> None:
    glossary = _static_text("page-glossary.js")
    render = _function_source(glossary, "renderGlossary")
    assert "glossaryFilter(entries)" in render
    # A ?term= link clears a filter that hides its entry.
    assert "if (node.hidden) filter.reset();" in render
    filt = _function_source(glossary, "glossaryFilter")
    assert 'type: "search"' in filt
    assert 'el("label", { for: "glossary-filter", text: "Find a term" })' in filt
    assert 'role: "status"' in filt
    assert '"Show every term"' in filt
    assert 'event.key !== "Escape"' in filt
    assert "Try a shorter word" in filt


# -- the status line -------------------------------------------------------------------------


def test_status_line_reads_relative_times_that_stay_current() -> None:
    shell = _static_text("shell.js")
    line = _function_source(shell, "renderStatusLine")
    assert "timeNode(lastScan)" in line
    assert "timeNode(figures.asOf)" in line
    assert "shortTs" not in line
    # The signature holds the raw times; the words move on in place.
    assert "refreshTimes(line)" in line
    refresh = _function_source(shell, "refreshTimes")
    assert "relativeTime(node.dateTime)" in refresh
    assert re.search(r'addEventListener\("visibilitychange"[\s\S]{0,200}refreshTimes\(line\)', shell)


def test_status_line_is_a_named_group_not_a_bare_labelled_div() -> None:
    html = _static_text("index.html")
    tag = re.search(r'<div[^>]*id="status-line"[^>]*>', html).group(0)
    assert 'role="group"' in tag
    assert 'aria-label="Service status"' in tag
    assert "aria-live" not in tag


# -- window chips ----------------------------------------------------------------------------


def test_fixed_window_chip_and_all_time_chips_say_what_is_true() -> None:
    app = _static_text("app.js")
    assert '"Same for every window"' in app
    assert "doesn't apply here" not in app
    assert "cache rebuild causes" not in app
    section = _function_source(_static_text("page-setup.js"), "setupSection")
    assert 'chip(state.project ? "All time, all projects" : "All time", { icon: "clock", class: "all-time-chip" })' in section
    setup = _static_text("page-setup.js")
    assert re.search(re.escape("Latest baseline") + r'", "[\w-]+", \{ allTime: true \}\)', setup)
    # Your changes' cards follow the window and the project, so they carry
    # no chip. The estimates aren't kept per project: with one picked, their
    # section says "All projects" (and never "All time", which they aren't).
    changes = _static_text("page-changes.js")
    section = _function_source(changes, "changesSection")
    assert 'allProjects && state.project ? chip("All projects", { icon: "folder", class: "all-time-chip" }) : null' in section
    assert "All time" not in section
    assert 'changesSection(panel, "Each change, before and after")' in changes
    assert 'changesSection(panel, "Did your estimates come true?", true)' in changes

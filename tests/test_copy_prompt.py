"""The Copy buttons when the browser refuses the clipboard (``service/static``).

The Claude Desktop browser pane refuses ``navigator.clipboard.writeText``
and a page served over plain http has no ``navigator.clipboard`` at all;
``document.execCommand("copy")`` on a selected textarea still works in the
pane. ``copyToClipboard`` (ui.js) tries the API, then that. When both fail,
the project bundle's Copy prompt shows the prompt under its row, selected
and focused, and the toast says so. These cases run the page's own
functions in Node, over a small stand-in for the parts of the DOM they use.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from test_service_static import REPO_ROOT, _function_source, _static_text

PROMPT = "Work through these fixes.\n\n## ~/.claude/settings.json\n\nSet the model to sonnet."

#: The DOM the copy functions use, as far as they use it. ``scenario``
#: says how the browser behaves: ``clipboard`` is "resolves", "rejects",
#: "throws" or "missing"; ``exec`` is true, false or "throws".
_DOM = r"""
var scenario = %(scenario)s;
var log = { exec: [], toasts: [], removed: 0, marks: [] };
function FakeNode(tag) {
  this.tagName = String(tag).toUpperCase();
  this.children = [];
  this.parentNode = null;
  this.style = {};
  this.attrs = {};
  this.className = "";
  this.textContent = "";
  this.value = "";
}
Object.defineProperty(FakeNode.prototype, "firstChild", { get: function () { return this.children[0] || null; } });
FakeNode.prototype.appendChild = function (child) {
  if (child.parentNode) child.parentNode.removeChild(child);
  child.parentNode = this;
  this.children.push(child);
  return child;
};
FakeNode.prototype.removeChild = function (child) {
  this.children.splice(this.children.indexOf(child), 1);
  child.parentNode = null;
  if (child.tagName === "TEXTAREA") log.removed += 1;
  return child;
};
FakeNode.prototype.setAttribute = function (key, value) { this.attrs[key] = value; };
FakeNode.prototype.addEventListener = function (type, fn) { (this.listeners = this.listeners || {})[type] = fn; };
FakeNode.prototype.closest = function (tag) {
  for (var node = this; node; node = node.parentNode) if (node.tagName === tag.toUpperCase()) return node;
  return null;
};
// While a modal dialog is open (the drawer), everything outside it is
// inert and cannot take focus; "focus": "never" refuses it everywhere.
FakeNode.prototype.focus = function () {
  if (scenario.focus === "never" && this.tagName === "TEXTAREA") return;
  if (scenario.dialog && !this.closest("dialog") && this.tagName !== "BODY") return;
  document.activeElement = this;
};
FakeNode.prototype.select = function () {
  document.selected = this;
  this.selectionStart = 0;
  this.selectionEnd = this.value.length;
};
var page = new FakeNode("body");
var field = new FakeNode("input");
var drawer = new FakeNode("dialog");
if (scenario.dialog) {
  page.appendChild(drawer);
  drawer.appendChild(field);
}
var document = {
  body: page,
  activeElement: field,
  selected: null,
  createElement: function (tag) { return new FakeNode(tag); },
  createTextNode: function (text) { var n = new FakeNode("#text"); n.textContent = text; return n; },
  createRange: function () { return { selectNodeContents: function (node) { this.node = node; } }; },
  execCommand: function (command) {
    var area = document.activeElement;
    log.exec.push({
      command: command, tag: area.tagName, value: area.value,
      selected: document.selected === area && area.selectionStart === 0 && area.selectionEnd === area.value.length,
      attached: !!area.parentNode, parent: area.parentNode ? area.parentNode.tagName : null,
      hidden: "aria-hidden" in area.attrs,
    });
    if (scenario.exec === "throws") throw new Error("not allowed");
    return scenario.exec;
  },
};
var selection = { range: null };
var window = {
  getSelection: function () {
    return { removeAllRanges: function () { selection.range = null; }, addRange: function (range) { selection.range = range; } };
  },
};
var navigator = scenario.clipboard === "missing" ? {} : {
  clipboard: {
    writeText: function (text) {
      log.written = text;
      if (scenario.clipboard === "throws") throw new Error("denied");
      return scenario.clipboard === "resolves" ? Promise.resolve() : Promise.reject(new Error("NotAllowedError"));
    },
  },
};
var buttons = [];
function button(text, opts) { buttons.push(opts); return { opts: opts }; }
function toast(message, opts) { log.toasts.push({ message: message, tone: opts.tone }); }
function agentName(agent) { return String(agent); }
var markCalls = 0;
function markCards(mark, keys) {
  var call = markCalls++;
  log.marks.push({ mark: mark, keys: keys });
  return Promise.resolve((scenario.failMarks || []).indexOf(call) === -1);
}
var scopeNow = "30";
function scopeKey() { return scopeNow; }
// The copy button's 1.5 s reset would keep Node waiting; a timer of 0 ms is left alone.
var realSetTimeout = globalThis.setTimeout;
function setTimeout(fn, ms) { return ms >= 1000 ? 0 : realSetTimeout(fn, ms); }
"""

_FUNCTIONS = [
    ("core.js", "el"),
    ("core.js", "clear"),
    ("ui.js", "copyToClipboard"),
    ("ui.js", "copyBySelection"),
    ("ui.js", "codeBlockWithCopy"),
    ("api.js", "markCardsShown"),
    ("page-actions.js", "bundleLine"),
    ("page-actions.js", "bundleBlock"),
    ("page-actions.js", "showBundlePrompt"),
    ("page-actions.js", "copyPromptButton"),
]


def _run(scenario: dict, expression: str) -> dict:
    """``expression`` (JS, over the stand-in DOM and the page's own copy
    functions), awaited, its value back through JSON."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node isn't installed")
    script = _DOM % {"scenario": json.dumps(scenario)}
    script += _declaration_line("page-actions.js", "BUNDLE_COPY_REFUSED") + "\n"
    script += _declaration_line("ui.js", "COPY_FAILED") + "\n"
    for name in ("shownSent", "shownWaiting", "shownWatching", "MARK_KEYS_MAX"):
        script += _declaration_line("api.js", name) + "\n"
    # A function the page doesn't have yet is left out, so a case about
    # another one fails on what the page does, not on the setup.
    script += "\n".join(
        _function_source(_static_text(module), name)
        for module, name in _FUNCTIONS
        if re.search(r"function\s+" + name + r"\s*\(", _static_text(module))
    )
    script += f"\nreturn Promise.resolve({expression});"
    # Inside a function, so the stand-in `navigator` and `window` replace Node's own.
    script = "(function () {\n" + script + "\n})().then(function (value) { process.stdout.write(JSON.stringify(value)); });"
    done = subprocess.run([node, "-e", script], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180, check=True)
    return json.loads(done.stdout)


def _declaration_line(module: str, name: str) -> str:
    """``var NAME = ...;`` as one line of the module, or nothing if absent."""
    match = re.search(r"^var " + name + r" = .*;$", _static_text(module), re.M)
    return match.group(0) if match else ""


def _copy(scenario: dict) -> dict:
    return _run(
        scenario,
        "copyToClipboard(%s).then(function (ok) { return { ok: ok, log: log, active: document.activeElement === field,"
        " body: page.children.length }; })" % json.dumps(PROMPT),
    )


def _press(scenario: dict, keys: list[str] | None = None) -> dict:
    """Press the bundle's Copy prompt and report what the page then holds."""
    data = {"prompt": PROMPT, "files": [{"file": "~/.claude/settings.json", "fixes": 1}], "several": 0, "other": 0}
    if keys is not None:
        data["keys"] = keys
    expression = (
        "(function () {"
        " var block = bundleBlock(%s, 'proj');"
        " var slot = block.children[1];"
        " buttons[0].action();"
        " return new Promise(function (done) { setTimeout(done, 0); }).then(function () {"
        "  var pre = slot.firstChild && slot.firstChild.firstChild;"
        "  return { toasts: log.toasts, shown: pre ? pre.textContent : null, name: pre ? pre.attrs['aria-label'] : null,"
        "   role: pre ? pre.attrs.role : null,"
        "   focused: !!pre && document.activeElement === pre,"
        "   selected: !!pre && !!selection.range && selection.range.node === pre,"
        "   written: log.written, exec: log.exec, label: buttons[0].label, marks: log.marks }; });"
        " })()" % json.dumps(data)
    )
    return _run(scenario, expression)


# -- copyToClipboard: the second try ------------------------------------------


@pytest.mark.parametrize("clipboard", ["rejects", "throws", "missing"])
def test_a_refused_clipboard_api_falls_back_to_copying_a_selected_textarea(clipboard: str) -> None:
    got = _copy({"clipboard": clipboard, "exec": True})
    assert got["ok"] is True
    [call] = got["log"]["exec"]
    assert call == {"command": "copy", "tag": "TEXTAREA", "value": PROMPT, "selected": True, "attached": True, "parent": "BODY", "hidden": False}


@pytest.mark.parametrize("clipboard", ["rejects", "throws", "missing"])
def test_the_textarea_is_removed_and_the_focus_goes_back(clipboard: str) -> None:
    got = _copy({"clipboard": clipboard, "exec": True})
    assert got["log"]["removed"] == 1 and got["body"] == 0
    assert got["active"] is True


@pytest.mark.parametrize("clipboard", ["rejects", "missing"])
@pytest.mark.parametrize("exec_result", [False, "throws"])
def test_a_copy_that_both_tries_refuse_resolves_false_and_cleans_up(clipboard: str, exec_result) -> None:
    got = _copy({"clipboard": clipboard, "exec": exec_result})
    assert got["ok"] is False
    assert got["log"]["removed"] == 1 and got["body"] == 0 and got["active"] is True


def test_a_working_clipboard_api_does_not_touch_the_page() -> None:
    got = _copy({"clipboard": "resolves", "exec": False})
    assert got["ok"] is True
    assert got["log"]["written"] == PROMPT
    assert got["log"]["exec"] == [] and got["log"]["removed"] == 0


# -- a drawer (a modal dialog) makes the rest of the page inert --------------


def test_in_a_drawer_the_textarea_goes_inside_the_dialog_and_the_copy_works() -> None:
    got = _copy({"clipboard": "rejects", "exec": True, "dialog": True})
    assert got["ok"] is True
    [call] = got["log"]["exec"]
    assert call["tag"] == "TEXTAREA" and call["selected"] is True and call["parent"] == "DIALOG"
    assert got["log"]["removed"] == 1 and got["active"] is True


def test_a_copy_is_not_claimed_when_the_textarea_cannot_take_focus() -> None:
    """``execCommand`` returns true when there is nothing to copy, so the
    textarea must be the focused, selected element before it is asked."""
    for scenario in ({"focus": "never"}, {"focus": "never", "dialog": True}):
        got = _copy({"clipboard": "rejects", "exec": True, **scenario})
        assert got["ok"] is False, scenario
        assert got["log"]["exec"] == [], scenario
        assert got["log"]["removed"] == 1 and got["active"] is True, scenario


def test_a_copy_with_no_clipboard_api_in_a_drawer_works_the_same() -> None:
    got = _copy({"clipboard": "missing", "exec": True, "dialog": True})
    assert got["ok"] is True
    assert got["log"]["exec"][0]["tag"] == "TEXTAREA" and got["log"]["exec"][0]["parent"] == "DIALOG"


# -- the project bundle's Copy prompt -----------------------------------------


def test_when_the_api_rejects_and_the_textarea_copy_works_no_fallback_text_is_shown() -> None:
    got = _press({"clipboard": "rejects", "exec": True})
    assert got["shown"] is None
    assert got["toasts"] == [{"message": "Prompt copied. Paste it into Claude Code.", "tone": "success"}]


@pytest.mark.parametrize("clipboard", ["rejects", "missing"])
def test_when_both_tries_fail_the_prompt_is_shown_selected_and_focused_and_the_toast_names_it(clipboard: str) -> None:
    got = _press({"clipboard": clipboard, "exec": False})
    assert got["shown"] == PROMPT
    assert got["focused"] is True and got["selected"] is True
    # The focused block is named, as the button that opens it is.
    assert got["name"] == "Prompt for every open fix in proj" and got["role"] == "group"
    assert got["toasts"] == [
        {"message": "Couldn't copy. The prompt is selected below: copy it from there.", "tone": "warning"}
    ]


def test_a_press_that_works_after_a_refused_one_takes_the_shown_prompt_away() -> None:
    expression = (
        "(function () {"
        " var data = {prompt: %s, files: [], several: 1, other: 0};"
        " var slot = bundleBlock(data, 'proj').children[1];"
        " var press = function () { buttons[0].action(); return new Promise(function (done) { setTimeout(done, 0); }); };"
        " return press().then(function () {"
        "  var first = slot.children.length;"
        "  scenario.exec = true;"
        "  return press().then(function () { return { first: first, second: slot.children.length }; });"
        " });"
        "})()" % json.dumps(PROMPT)
    )
    assert _run({"clipboard": "missing", "exec": False}, expression) == {"first": 1, "second": 0}


def test_the_bundle_button_is_named_with_its_visible_words_first() -> None:
    got = _press({"clipboard": "resolves", "exec": False})
    assert got["label"] == "Copy prompt for every open fix in proj"
    assert got["label"].startswith("Copy prompt")


# -- the note beside the button -----------------------------------------------


def _line(files: list[int], several: int, other: int) -> str:
    data = {"files": [{"file": f"f{i}", "fixes": n} for i, n in enumerate(files)], "several": several, "other": other}
    return _run({"clipboard": "missing", "exec": False}, f"bundleLine({json.dumps(data)}, 'proj')")


@pytest.mark.parametrize(
    ("files", "several", "other", "line"),
    [
        ([2, 1], 0, 0, "3 open fixes for proj: 2 files to edit."),
        ([1], 0, 0, "1 open fix for proj: 1 file to edit."),
        ([2, 1], 3, 0, "6 open fixes for proj: 2 files to edit, plus 3 that span several files or set a standing rule."),
        ([2], 1, 0, "3 open fixes for proj: 1 file to edit, plus 1 that spans several files or sets a standing rule."),
        ([2], 0, 1, "3 open fixes for proj: 1 file to edit and 1 with no file to edit."),
        (
            [2],
            2,
            1,
            "5 open fixes for proj: 1 file to edit, plus 2 that span several files or set a standing rule"
            " and 1 with no file to edit.",
        ),
        ([], 2, 1, "3 open fixes for proj: 2 that span several files or set a standing rule and 1 with no file to edit."),
        ([], 1, 0, "1 open fix for proj: 1 that spans several files or sets a standing rule."),
        ([], 0, 2, "2 open fixes for proj, with no file to edit."),
    ],
)
def test_the_bundle_note_counts_files_apart_from_prompts_that_span_several(files, several, other, line) -> None:
    assert _line(files, several, other) == line


def test_the_shown_prompt_keeps_no_padding_for_a_copy_button_it_does_not_have() -> None:
    css = _static_text("app.css")
    rule = re.search(r"\.bundle-prompt \.code-block pre \{([^}]*)\}", css)
    assert rule and "padding-right: var(--space-5);" in rule.group(1)


# -- a copy of a card's prompt is recorded, a refused one is not --------------


def _card_press(scenario: dict, card: str | None) -> list:
    """Press a card's per-agent Copy button and report the marks it sent."""
    expression = (
        "(function () {"
        " copyPromptButton(%s, 'agent', null, null, null, %s);"
        " buttons[0].action();"
        " return new Promise(function (done) { setTimeout(done, 0); }).then(function () { return log.marks; });"
        " })()" % (json.dumps(PROMPT), json.dumps(card))
    )
    return _run(scenario, expression)


@pytest.mark.parametrize(
    ("scenario", "marks"),
    [
        ({"clipboard": "resolves", "exec": False}, [{"mark": "copied", "keys": ["spawn-tools-list:x"]}]),
        ({"clipboard": "rejects", "exec": True}, [{"mark": "copied", "keys": ["spawn-tools-list:x"]}]),
        ({"clipboard": "rejects", "exec": False}, []),
        ({"clipboard": "missing", "exec": False}, []),
    ],
)
def test_a_card_copy_that_worked_is_recorded_once_and_a_refused_one_is_not(scenario: dict, marks: list) -> None:
    assert _card_press(scenario, "spawn-tools-list:x") == marks


def test_a_copy_button_that_belongs_to_no_card_records_nothing() -> None:
    assert _card_press({"clipboard": "resolves", "exec": False}, None) == []


def test_a_bundle_copy_that_worked_records_its_cards_and_a_refused_one_records_none() -> None:
    worked = _press({"clipboard": "resolves", "exec": False}, ["a", "b"])
    assert worked["marks"] == [{"mark": "bundle", "keys": ["a", "b"]}]
    refused = _press({"clipboard": "rejects", "exec": False}, ["a", "b"])
    assert refused["marks"] == []
    # A bundle that names no card (or one from an older service) sends the empty
    # list, which markCards drops before it reaches the service.
    assert _press({"clipboard": "resolves", "exec": False})["marks"] == [{"mark": "bundle", "keys": []}]


@pytest.mark.parametrize(
    ("scenario", "calls", "label"),
    [
        ({"clipboard": "resolves", "exec": False}, 1, "Copied"),
        ({"clipboard": "rejects", "exec": True}, 1, "Copied"),
        ({"clipboard": "rejects", "exec": False}, 0, "Couldn't copy"),
    ],
)
def test_a_code_block_copy_calls_back_only_when_the_copy_worked(scenario: dict, calls: int, label: str) -> None:
    """The hook the card copies rely on (R6): ``codeBlockWithCopy`` runs its
    ``onCopy`` only after a copy that worked."""
    expression = (
        "(function () {"
        " var calls = 0;"
        " var block = codeBlockWithCopy('text', 'Prompt', 'x', function () { calls += 1; });"
        " var press = block.children[1];"
        " press.listeners.click();"
        " return new Promise(function (done) { setTimeout(done, 0); }).then(function () {"
        "  return { calls: calls, label: press.textContent }; });"
        " })()"
    )
    assert _run(scenario, expression) == {"calls": calls, "label": label}


def test_cards_shown_are_sent_once_per_scope_and_only_while_the_tab_is_visible() -> None:
    expression = (
        "(function () {"
        " var sent = function () { return log.marks.map(function (m) { return m.mark + ':' + m.keys.join(','); }); };"
        " var fire = null;"
        " document.addEventListener = function (type, fn) { fire = fn; };"
        " document.visibilityState = 'hidden';"
        " markCardsShown(['a', 'b']);"
        " var hidden = sent();"
        " document.visibilityState = 'visible';"
        " fire();"
        " var back = sent();"
        " markCardsShown(['a', 'b', 'c']);"  # only c is new
        " markCardsShown(['a']);"  # nothing new
        " markCardsShown([]);"  # this scope has been told already
        " scopeNow = '7';"
        " markCardsShown([]);"  # a new scope with an empty list says so, once
        " markCardsShown([]);"
        " return { hidden: hidden, back: back, all: sent() };"
        " })()"
    )
    got = _run({"clipboard": "missing", "exec": False}, expression)
    assert got == {"hidden": [], "back": ["shown:a,b"], "all": ["shown:a,b", "shown:c", "shown:"]}


# -- a mark is sent without a word on screen ------------------------------------

#: What markCards (api.js) touches, over a ``fetch`` that behaves as the case says
#: ("rejects", "throws", or the HTTP status of a reply).
_MARK_DOM = r"""
var scenario = %(scenario)s;
var log = { fetches: [], connection: [], figures: [] };
function noteConnection(up) { log.connection.push(up); }
function noteFiguresAsOf(asOf) { log.figures.push(asOf); }
function readableAmounts(body) { return body; }
function withWindow(url) { return url + "?window=30"; }
var fetch = function (url, options) {
  log.fetches.push({ url: url, options: options });
  if (scenario.fetch === "throws") throw new Error("no network");
  if (scenario.fetch === "rejects") return Promise.reject(new Error("Failed to fetch"));
  return Promise.resolve({
    status: scenario.fetch, ok: scenario.fetch >= 200 && scenario.fetch < 300,
    headers: { get: function () { return null; } },
    json: function () { return Promise.resolve({ ok: scenario.fetch < 300, data: {} }); },
  });
};
"""


def _run_marks(scenario: dict, expression: str):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node isn't installed")
    script = _MARK_DOM % {"scenario": json.dumps(scenario)}
    # A function the page doesn't have yet is left out, as in _run.
    script += "\n".join(
        _function_source(_static_text("api.js"), name)
        for name in ("fetchJson", "jsonPost", "postJson", "markCards")
        if re.search(r"function\s+" + name + r"\s*\(", _static_text("api.js"))
    )
    script += f"\nreturn Promise.resolve({expression});"
    script = "(function () {\n" + script + "\n})().then(function (value) { process.stdout.write(JSON.stringify(value)); });"
    done = subprocess.run([node, "-e", script], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60, check=True)
    return json.loads(done.stdout)


@pytest.mark.parametrize(
    ("fetch", "worked"), [("rejects", False), ("throws", False), (400, False), (500, False), (200, True)]
)
def test_a_mark_that_fails_changes_nothing_on_screen(fetch, worked) -> None:
    got = _run_marks(
        {"fetch": fetch},
        "Promise.resolve(markCards('copied', ['a'])).then(function (ok) {"
        " return new Promise(function (done) { setTimeout(done, 5); }).then(function () {"
        "  return { ok: ok === undefined ? null : ok, log: log }; }); })",
    )
    # No dimmed page, no health poll, no "Connected to the service again.": the connection
    # note, which fetchJson makes on a network failure, is not touched.
    assert got["log"]["connection"] == [] and got["log"]["figures"] == []
    assert len(got["log"]["fetches"]) == 1
    assert got["ok"] is worked


def test_a_mark_is_the_request_post_json_would_build() -> None:
    got = _run_marks(
        {"fetch": 200},
        "(function () {"
        " return markCards('shown', ['a', 'b']).then(function () {"
        "  var mark = log.fetches[0];"
        "  return postJson('/api/recommendations/marks?window=30', { mark: 'shown', keys: ['a', 'b'] }).then(function () {"
        "   return { mark: mark, post: log.fetches[1] }; }); }); })()",
    )
    assert got["mark"] == got["post"]
    assert got["mark"]["options"]["method"] == "POST" and json.loads(got["mark"]["options"]["body"]) == {
        "mark": "shown",
        "keys": ["a", "b"],
    }


# -- more cards than the route takes in one mark -----------------------------------


def test_the_most_keys_a_mark_carries_is_the_routes_limit() -> None:
    page = re.search(r"^var MARK_KEYS_MAX = (\d+);$", _static_text("api.js"), re.M)
    route = re.search(r"^_MAX_IGNORE_KEYS = (\d+)$", (REPO_ROOT / "src/claudeglass/service/api.py").read_text(encoding="utf-8"), re.M)
    assert page and route and page.group(1) == route.group(1)


def test_cards_shown_are_sent_in_slices_of_what_the_route_takes() -> None:
    expression = (
        "(function () {"
        " document.visibilityState = 'visible';"
        " var keys = []; for (var i = 0; i < 250; i++) keys.push('k' + i);"
        " markCardsShown(keys);"
        " return new Promise(function (done) { setTimeout(done, 5); }).then(function () {"
        "  return log.marks.map(function (m) { return [m.mark, m.keys.length, m.keys[0]]; }); });"
        " })()"
    )
    got = _run({"clipboard": "missing", "exec": False}, expression)
    assert got == [["shown", 100, "k0"], ["shown", 100, "k100"], ["shown", 50, "k200"]]


def test_a_slice_that_fails_is_not_counted_as_sent() -> None:
    expression = (
        "(function () {"
        " document.visibilityState = 'visible';"
        " var keys = []; for (var i = 0; i < 250; i++) keys.push('k' + i);"
        " var wait = function () { return new Promise(function (done) { setTimeout(done, 5); }); };"
        " markCardsShown(keys);"
        " return wait().then(function () {"
        "  var first = log.marks.length;"
        "  markCardsShown(keys);"  # the second slice (call 1) failed: only its keys go again
        "  return wait().then(function () {"
        "   return { first: first, again: log.marks.slice(first).map(function (m) { return [m.keys.length, m.keys[0]]; }) };"
        "  }); });"
        " })()"
    )
    got = _run({"clipboard": "missing", "exec": False, "failMarks": [1]}, expression)
    assert got == {"first": 3, "again": [[100, "k100"]]}

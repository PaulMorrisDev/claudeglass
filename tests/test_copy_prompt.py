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

from test_service_static import _function_source, _static_text

PROMPT = "Work through these fixes.\n\n## ~/.claude/settings.json\n\nSet the model to sonnet."

#: The DOM the copy functions use, as far as they use it. ``scenario``
#: says how the browser behaves: ``clipboard`` is "resolves", "rejects",
#: "throws" or "missing"; ``exec`` is true, false or "throws".
_DOM = r"""
var scenario = %(scenario)s;
var log = { exec: [], toasts: [], removed: 0 };
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
"""

_FUNCTIONS = [
    ("core.js", "el"),
    ("core.js", "clear"),
    ("ui.js", "copyToClipboard"),
    ("ui.js", "copyBySelection"),
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


def _press(scenario: dict) -> dict:
    """Press the bundle's Copy prompt and report what the page then holds."""
    data = {"prompt": PROMPT, "files": [{"file": "~/.claude/settings.json", "fixes": 1}], "several": 0, "other": 0}
    expression = (
        "(function () {"
        " var block = bundleBlock(%s, 'proj');"
        " var slot = block.children[1];"
        " buttons[0].action();"
        " return new Promise(function (done) { setTimeout(done, 0); }).then(function () {"
        "  var pre = slot.firstChild && slot.firstChild.firstChild;"
        "  return { toasts: log.toasts, shown: pre ? pre.textContent : null,"
        "   focused: !!pre && document.activeElement === pre,"
        "   selected: !!pre && !!selection.range && selection.range.node === pre,"
        "   written: log.written, exec: log.exec, label: buttons[0].label }; });"
        " })()" % json.dumps(data)
    )
    return _run(scenario, expression)


# -- copyToClipboard: the second try ------------------------------------------


@pytest.mark.parametrize("clipboard", ["rejects", "throws", "missing"])
def test_a_refused_clipboard_api_falls_back_to_copying_a_selected_textarea(clipboard: str) -> None:
    got = _copy({"clipboard": clipboard, "exec": True})
    assert got["ok"] is True
    [call] = got["log"]["exec"]
    assert call == {"command": "copy", "tag": "TEXTAREA", "value": PROMPT, "selected": True, "attached": True, "parent": "BODY"}


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
    assert got["toasts"] == [
        {"message": "Couldn't copy. The prompt is shown below: select it and copy it.", "tone": "warning"}
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

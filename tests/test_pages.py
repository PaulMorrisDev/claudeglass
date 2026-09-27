"""``pages.py``: the ``{{page:...}}`` token registry and the ``plain()``
renderer that turns a token into the same plain label the dashboard
shows.

Covers, per ``docs/writing-help.md``'s "Linking to another page" section:

- ``pages.PAGES`` stays in sync with the dashboard's own registry,
  ``PAGES`` in ``service/static/links.js``.
- Every ``{{page:...}}`` token actually written into a server string
  under ``src/`` resolves against that registry.
- A token never reaches a recommendation's ``why``/``title`` or a
  ``fixes[].prompt``/``fixes[].command`` (``fixes.py``'s ``prompt_for``
  and ``build_fix`` build those from ``why``/``title`` verbatim, into
  text a person or Claude reads outside the dashboard).
- The renderers that strip a token for a plain-text reader --
  ``render/markdown.py``, ``render/html.py``, ``quick_actions.py``'s own
  ``render_markdown``, and the CLI's ``_print_table`` -- actually do.
- No stale "<Name> tab" wording is left in server text (the redesign
  replaced tabs with pages; a page name belongs in a token or in plain
  "page" wording, never hard-coded as "the X tab").
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from claudeglass import pages

SRC = Path(__file__).resolve().parent.parent / "src" / "claudeglass"

_LINKS_JS = SRC / "service" / "static" / "links.js"


def _js_objects(array_body: str) -> list[str]:
    """The inner text of each top-level ``{...}`` object in a JS array's
    body, tracking brace depth so a nested object (a segment inside a
    page) isn't split out on its own."""
    objects: list[str] = []
    depth = 0
    start: int | None = None
    for i, ch in enumerate(array_body):
        if ch == "{":
            if depth == 0:
                start = i + 1
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                objects.append(array_body[start:i])
                start = None
    return objects


def _js_bracketed_body(text: str, after: str) -> str:
    """The ``[...]`` body of the first ``[`` following ``after`` in
    ``text``, matching brackets so a nested array can't end it early."""
    start = text.index(after) + len(after)
    open_idx = text.index("[", start)
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == "[":
            depth += 1
        elif text[i] == "]":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1 : i]
    raise ValueError(f"unbalanced [ following {after!r}")


_JS_ID_RE = re.compile(r'id:\s*"([a-z]+(?:-[a-z]+)*)"')
_JS_LABEL_RE = re.compile(r'label:\s*"([^"]+)"')


def _parse_links_js_pages(text: str) -> dict[str, dict]:
    """``{page_id: {"label": ..., "segments": {segment_id: label}}}``,
    read from ``links.js``'s ``export var PAGES = [...]``.

    Not a general JS parser: it relies on ``id`` and ``label`` always
    being an object's first two properties (true of every page and
    segment in this file today) and on ``.search`` finding the
    left-most, i.e. the object's own, ``id:``/``label:`` before it ever
    reaches a nested segment's.
    """
    array_body = _js_bracketed_body(text, "export var PAGES = ")
    result: dict[str, dict] = {}
    for page_text in _js_objects(array_body):
        page_id = _JS_ID_RE.search(page_text).group(1)
        page_label = _JS_LABEL_RE.search(page_text).group(1)
        segments: dict[str, str] = {}
        if "segments:" in page_text:
            for seg_text in _js_objects(_js_bracketed_body(page_text, "segments:")):
                segments[_JS_ID_RE.search(seg_text).group(1)] = _JS_LABEL_RE.search(seg_text).group(1)
        result[page_id] = {"label": page_label, "segments": segments}
    return result


def test_pages_registry_matches_dashboard_links_js():
    js_pages = _parse_links_js_pages(_LINKS_JS.read_text(encoding="utf-8"))
    py_pages = {page.id: {"label": page.label, "segments": {s.id: s.label for s in page.segments}} for page in pages.PAGES}
    assert set(py_pages) == set(js_pages), (
        f"pages.PAGES and links.js PAGES name different pages: "
        f"python-only={set(py_pages) - set(js_pages)}, js-only={set(js_pages) - set(py_pages)}"
    )
    for page_id, js_page in js_pages.items():
        py_page = py_pages[page_id]
        assert py_page["label"] == js_page["label"], f"{page_id}: {py_page['label']!r} != {js_page['label']!r}"
        assert py_page["segments"] == js_page["segments"], f"{page_id}: segment mismatch ({py_page['segments']} != {js_page['segments']})"


def test_every_page_and_segment_id_matches_the_router_id_shape():
    id_re = re.compile(r"^[a-z]+(-[a-z]+)*$")
    for page in pages.PAGES:
        assert id_re.match(page.id), page.id
        for segment in page.segments:
            assert id_re.match(segment.id), f"{page.id}/{segment.id}"


def test_label_for_every_page_and_segment():
    for page in pages.PAGES:
        assert pages.label_for(page.id) == page.label
        for segment in page.segments:
            assert pages.label_for(f"{page.id}/{segment.id}") == f"{page.label}{pages.SEPARATOR}{segment.label}"


def test_label_for_rejects_unknown_ids():
    with pytest.raises(ValueError):
        pages.label_for("not-a-real-page")
    with pytest.raises(ValueError):
        pages.label_for("cache/not-a-real-segment")


def test_plain_replaces_tokens_and_is_a_no_op_without_one():
    assert pages.plain("Check {{page:cache/rebuilds}} for the causes.") == "Check Cache › Rebuilds for the causes."
    assert pages.plain("Nothing to see here.") == "Nothing to see here."
    assert pages.plain("") == ""
    assert pages.plain(None) is None


def test_tokens_in_finds_every_token_in_order():
    text = "See {{page:habits}} and then {{page:cache/rebuilds}} and {{page:habits}} again."
    assert pages.tokens_in(text) == ["habits", "cache/rebuilds", "habits"]


# -- every token actually written in a server string resolves ---------------


def _all_tokens_by_file() -> dict[Path, list[str]]:
    found: dict[Path, list[str]] = {}
    for path in SRC.rglob("*.py"):
        tokens = pages.tokens_in(path.read_text(encoding="utf-8"))
        if tokens:
            found[path] = tokens
    return found


def test_every_page_token_under_src_resolves():
    offenders = []
    seen_any = False
    for path, tokens in _all_tokens_by_file().items():
        for token in tokens:
            seen_any = True
            try:
                pages.label_for(token)
            except ValueError as exc:
                offenders.append(f"{path.relative_to(SRC)}: {token!r} ({exc})")
    assert seen_any, "the scan found no {{page:...}} tokens under src/ -- has the token syntax changed?"
    assert offenders == [], "unknown page/segment id in a token:\n" + "\n".join(offenders)


def test_no_page_token_is_halved_by_an_f_string():
    # Inside an f-string, {{ is an escaped brace: a token written there
    # comes out as "{page:...}", which neither the dashboard nor
    # pages.plain() recognises, so the reader sees it raw.
    offenders = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.JoinedStr):
                continue
            for part in node.values:
                if isinstance(part, ast.Constant) and isinstance(part.value, str) and re.search(r"(?<!\{)\{page:", part.value):
                    offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert offenders == [], "a {{page:...}} token inside an f-string loses a brace; put it in a plain string:\n" + "\n".join(offenders)


# -- a token never reaches why/title or a fix's prompt/command --------------


def _string_value(node: ast.AST) -> str | None:
    """The literal text of a plain string constant or an f-string whose
    parts are all literal (good enough to catch a token written straight
    into the string; an interpolated ``{...}`` value can't spell out
    ``{{page:...}}`` by coincidence, so a partial f-string is skipped
    rather than mis-flagged)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                return None
        return "".join(parts)
    return None


def test_no_page_token_in_recommendation_why_title_or_fix_prompt_command():
    offenders = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Attribute) and target.attr in ("why", "title"):
                        text = _string_value(node.value)
                        if text and "{{page:" in text:
                            offenders.append(f"{path.relative_to(SRC)}:{node.lineno}: .{target.attr} = {text!r}")
            elif isinstance(node, ast.Call):
                func_name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
                if func_name == "Recommendation":
                    for kw in node.keywords:
                        if kw.arg in ("why", "title"):
                            text = _string_value(kw.value)
                            if text and "{{page:" in text:
                                offenders.append(f"{path.relative_to(SRC)}:{node.lineno}: Recommendation({kw.arg}=...) carries a token")
                elif func_name in ("_fix", "build_fix") or func_name == "dict":
                    # fixes.py's own dict literals (_fix(...) and the
                    # dashboard fix-dict builders): "prompt"/"command"
                    # values, wherever they're built as a literal.
                    for kw in node.keywords:
                        if kw.arg in ("prompt", "command"):
                            text = _string_value(kw.value)
                            if text and "{{page:" in text:
                                offenders.append(f"{path.relative_to(SRC)}:{node.lineno}: {func_name}({kw.arg}=...) carries a token")
    assert offenders == [], "\n".join(offenders)


# -- the renderers that must strip a token actually do -----------------------


def _model_with_a_token():
    from claudeglass.model import Column, Help, ReportModel, Section, Table

    table = Table(
        name="t",
        title="Things",
        columns=[Column(key="a", label="A", kind="str", help="A column.")],
        rows=[["x"]],
        notes=["See {{page:cache/rebuilds}} for details."],
        help=Help(shows="Each row is a thing.", read="", act="{{page:spend/savings}} estimates the effect."),
    )
    section = Section(key="s", title="Section", tables=[table], intro="See {{page:habits}} for the playbook.")
    return ReportModel(sections=[section])


def test_markdown_and_html_strip_tokens_but_json_keeps_them():
    import json

    from claudeglass.render.html import render_html
    from claudeglass.render.json_out import render_json
    from claudeglass.render.markdown import render_markdown

    model = _model_with_a_token()

    plain_md = render_markdown(model)
    assert "{{page:" not in plain_md

    explained_md = render_markdown(model, explain=True)
    assert "{{page:" not in explained_md
    assert "Cache › Rebuilds" in explained_md
    assert "Spend › Savings" in explained_md
    assert "Work habits" in explained_md

    html = render_html(model)
    assert "{{page:" not in html
    assert "Cache › Rebuilds" in html

    raw = render_json(model)
    assert "{{page:cache/rebuilds}}" in raw
    assert "{{page:spend/savings}}" in raw
    assert "{{page:habits}}" in raw
    parsed = json.loads(raw)
    note = parsed["report"]["sections"][0]["tables"][0]["notes"][0]
    assert note == "See {{page:cache/rebuilds}} for details."


def test_cli_print_table_strips_tokens(capsys):
    from claudeglass.cli import _print_table
    from claudeglass.model import Column, Table

    table = Table(
        name="t",
        title="A table",
        columns=[Column(key="a", label="A", kind="str")],
        rows=[["x"]],
        notes=["See {{page:cache/rebuilds}} for details."],
    )
    _print_table(table, currency="USD")
    out = capsys.readouterr().out
    assert "{{page:" not in out
    assert "Cache › Rebuilds" in out


def test_quick_actions_render_markdown_strips_tokens():
    from claudeglass import quick_actions as qa

    result = {
        "question": "Is anything costing tokens?",
        "summary": "See {{page:habits}} for more.",
        "tips": [{"title": "Tip", "text": "Check {{page:cache/rebuilds}}."}],
        "fixes": [
            {
                "title": "Fix it",
                "key": "k",
                "explainer": [["Where and who it affects", "{{page:setup/capture}} shows it."]],
                "prompt": "Do X.",
                "command": None,
            }
        ],
    }
    md = qa.render_markdown(result)
    assert "{{page:" not in md
    assert "Work habits" in md
    assert "Cache › Rebuilds" in md
    assert "Setup › Capture" in md


# -- no stale "<Name> tab" wording --------------------------------------------

#: A literal ASCII tab character, not the dashboard's old tab-based
#: navigation -- YAML frontmatter parsing and a JSON round-trip that
#: mangles whitespace. Real code (not a docstring), so the ban below
#: would otherwise flag it.
_TAB_MENTION_ALLOWLIST = (
    "tab indentation is not supported",
    "turned part of it into a tab or newline",
    # Claude Code's plan-mode key, not a dashboard tab.
    "(Shift+Tab)",
)
_TAB_MENTION_RE = re.compile(r"\btabs?\b", re.IGNORECASE)


def _docstring_constant_ids(tree: ast.AST) -> set[int]:
    """``id()`` of every ``ast.Constant`` that is a module/class/function
    docstring (the first statement of its body)."""
    ids: set[int] = set()
    nodes = [tree] + [n for n in ast.walk(tree) if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))]
    for node in nodes:
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            ids.add(id(first.value))
    return ids


def _joined_str_child_ids(tree: ast.AST) -> set[int]:
    """``id()`` of every ``ast.Constant`` that is a literal piece of an
    f-string, so it isn't checked twice: once on its own, once as part
    of the f-string's reconstructed text."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            for value in node.values:
                if isinstance(value, ast.Constant):
                    ids.add(id(value))
    return ids


def test_no_stale_tab_names_in_server_text():
    """A page name belongs in a ``{{page:...}}`` token or in plain "page"
    wording -- never hard-coded as "the X tab", a leftover from the
    dashboard's pre-redesign tab navigation. Skips docstrings (comments
    are already invisible to ``ast``) and the two places a literal tab
    *character* is what's meant, not a page."""
    offenders = []
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(path))
        doc_ids = _docstring_constant_ids(tree)
        joined_child_ids = _joined_str_child_ids(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) in doc_ids or id(node) in joined_child_ids:
                    continue
                value = node.value
            elif isinstance(node, ast.JoinedStr):
                value = _string_value(node)
                if value is None:
                    continue
            else:
                continue
            if not _TAB_MENTION_RE.search(value):
                continue
            if any(allowed in value for allowed in _TAB_MENTION_ALLOWLIST):
                continue
            offenders.append(f"{path.relative_to(SRC)}:{node.lineno}: {value!r}")
    assert offenders == [], (
        "stale dashboard-tab wording -- use a {{page:...}} token, or plain "
        "'page' wording, instead:\n" + "\n".join(offenders)
    )

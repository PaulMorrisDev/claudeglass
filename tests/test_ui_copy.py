"""The dashboard's own words follow docs/writing-help.md.

Every string the dashboard shows is written in JS, so these checks read
the first-party modules' string literals (comments and regular
expressions skipped) and pick out the ones that are copy: text with a
space in it that isn't a class list, a key or markup. Server help text
has its own guard in test_help_coverage.py.

- No word from the "Not" column of the Words to use table (cache
  rebuild, not recache; main session, not top-level; ...), and no
  internal field names.
- No snake_case: a reader never sees a field or section key.
- Short sentences: 25 words at most (the house style aims under 20).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = REPO_ROOT / "src" / "claudeglass" / "service" / "static"

#: docs/writing-help.md's "Not" column, plus the internal field-name
#: families a reader should never see.
BANNED = re.compile(
    r"\b(?:re-?cache[sd]?|top-level|briefing|spawn write|baseline write|cache_creation|cache_read|attribution_\w+|per_turn_\w+)\b",
    re.IGNORECASE,
)
SNAKE_CASE = re.compile(r"\b[a-z]+(?:_[a-z0-9]+)+\b")
MAX_SENTENCE_WORDS = 25

# The character before a "/" that makes it the start of a regular
# expression rather than a division.
_REGEX_PRECEDERS = set("=(,:;!&|?{}[\n")


def _modules() -> list[Path]:
    modules = sorted(STATIC_DIR.glob("*.js"))
    assert len(modules) >= 20, modules
    return modules


def _string_literals(src: str) -> list[tuple[int, str]]:
    """(line, text) for each quoted string, skipping comments and regex
    literals."""
    found = []
    i, n = 0, len(src)
    while i < n:
        ch = src[i]
        if src.startswith("//", i):
            end = src.find("\n", i)
            i = n if end == -1 else end
            continue
        if src.startswith("/*", i):
            end = src.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        if ch in "\"'`":
            j = i + 1
            while j < n and src[j] != ch:
                j += 2 if src[j] == "\\" else 1
            found.append((src.count("\n", 0, i) + 1, src[i + 1 : j]))
            i = j + 1
            continue
        if ch == "/":
            before = src[:i].rstrip()
            if not before or before[-1] in _REGEX_PRECEDERS:
                j = i + 1
                while j < n and src[j] not in "/\n":
                    if src[j] == "\\":
                        j += 1
                    elif src[j] == "[":
                        while j < n and src[j] != "]":
                            j += 2 if src[j] == "\\" else 1
                    j += 1
                i = j + 1
                continue
        i += 1
    return found


def _is_copy(text: str) -> bool:
    """Words a reader sees, not a class list, key, path or markup."""
    if " " not in text.strip() or "<" in text or "viewBox" in text:
        return False
    if re.fullmatch(r"[a-z0-9 _:.\-/]+", text):
        return False
    return bool(re.search(r"[A-Za-z]{2,}", text))


def _copy() -> list[tuple[str, int, str]]:
    return [
        (path.name, line, text)
        for path in _modules()
        for line, text in _string_literals(path.read_text(encoding="utf-8"))
        if _is_copy(text)
    ]


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def test_the_scan_finds_the_dashboards_copy() -> None:
    """A guard on the guard: the scan sees real copy, and would catch
    each kind of problem."""
    copy = _copy()
    assert len(copy) >= 150, len(copy)
    texts = {text for _, _, text in copy}
    assert "Same for every window" in texts
    sample = 'el("p", { text: "The top-level session_id was recached." }); // "a recache comment"\nvar r = /"x"/;'
    found = [text for _, text in _string_literals(sample) if _is_copy(text)]
    assert found == ["The top-level session_id was recached."]
    assert BANNED.search(found[0]) and SNAKE_CASE.search(found[0])


def test_ui_copy_uses_the_house_words() -> None:
    bad = [f"{name}:{line}: {text}" for name, line, text in _copy() if BANNED.search(text)]
    assert bad == [], "\n".join(bad)


def test_ui_copy_never_shows_a_field_name() -> None:
    bad = [f"{name}:{line}: {text}" for name, line, text in _copy() if SNAKE_CASE.search(text)]
    assert bad == [], "\n".join(bad)


def test_ui_sentences_are_short() -> None:
    bad = [
        f"{name}:{line}: {len(sentence.split())} words: {sentence}"
        for name, line, text in _copy()
        for sentence in _sentences(text)
        if len(sentence.split()) > MAX_SENTENCE_WORDS
    ]
    assert bad == [], "\n".join(bad)


def test_no_amount_is_written_in_raw_usd() -> None:
    """Phase 4: amounts follow the billing mode through format.js
    (money, moneyText, currencyAmount), so no page writes "12.34 USD"
    itself. "USD" appears only as the currency code format.js and
    core.js compare against; a grid's money column carries its unit in
    the header (moneyUnit)."""
    found = [
        (path.name, line, text)
        for path in _modules()
        for line, text in _string_literals(path.read_text(encoding="utf-8"))
        if "USD" in text
    ]
    assert found, "the scan should see the currency code"
    bad = [f"{name}:{line}: {text!r}" for name, line, text in found if text != "USD" or name not in {"format.js", "core.js"}]
    assert bad == [], "\n".join(bad)


def test_the_data_quality_line_about_the_figures_is_copy_the_scan_reads_and_passes() -> None:
    """K1: the line under Data quality that points at the tuning file's figures."""
    line = (
        "Some advice rests on figures no page shows, such as cost per reply and what a summary saves. "
        "The tuning file holds them. "
    )
    found = [(name, text) for name, _line, text in _copy() if text == line]
    assert found == [("page-data.js", line)]
    assert not BANNED.search(line) and not SNAKE_CASE.search(line)
    assert all(len(sentence.split()) <= MAX_SENTENCE_WORDS for sentence in _sentences(line))
    assert "Figures behind the advice" in {text for _, _, text in _copy()}

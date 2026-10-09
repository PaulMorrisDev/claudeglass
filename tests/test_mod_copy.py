"""The mod's own words keep to the house style (``docs/writing-help.md``).

The mod's words live in ``claudeglass.mod_copy`` so the same checks the
dashboard's and the server's help text face cover them.
"""

from __future__ import annotations

import re
import string

import pytest

from claudeglass import mod_copy
from claudeglass.mod_copy import COPY, GLOSSARY

from test_help_coverage import BANNED, CAMEL_CASE, FILLER, MAX_SENTENCE_WORDS, SNAKE_CASE

_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")


def _strings() -> list[tuple[str, str]]:
    out = [(f"COPY[{key!r}]", text) for key, text in COPY.items()]
    for key, entry in GLOSSARY.items():
        out.extend((f"GLOSSARY[{key!r}][{field!r}]", text) for field, text in entry.items())
    return out


def _without_placeholders(text: str) -> str:
    """``text`` without its ``{placeholders}``, which are the mod's own
    fill-in names, not words a reader sees."""
    return _PLACEHOLDER.sub("", text)


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.?!])\s+", text) if s.strip()]


def test_the_module_exports_the_copy_and_the_glossary():
    assert set(mod_copy.__all__) == {"COPY", "GLOSSARY"}
    assert len(COPY) > 40 and len(GLOSSARY) > 5


@pytest.mark.parametrize("where,text", _strings())
def test_every_string_is_plain_words(where, text):
    assert isinstance(text, str) and text.strip(), where
    assert text == text.strip(), f"{where}: stray space at an end of {text!r}"
    assert not SNAKE_CASE.search(_without_placeholders(text)), f"{where}: internal name in {text!r}"
    assert not BANNED.search(text), f"{where}: banned term in {text!r}"
    assert not FILLER.search(text), f"{where}: filler word in {text!r}"
    assert not CAMEL_CASE.search(_without_placeholders(text)), f"{where}: camelCase name in {text!r}"
    assert " -- " not in text, f"{where}: dash aside in {text!r}"


@pytest.mark.parametrize("where,text", _strings())
def test_every_sentence_is_short(where, text):
    for sentence in _sentences(text):
        assert len(sentence.split()) <= MAX_SENTENCE_WORDS, f"{where}: sentence over 25 words: {sentence!r}"


@pytest.mark.parametrize("where,text", _strings())
def test_no_amount_is_written_with_a_bare_dollar_sign_or_an_approximation_mark(where, text):
    """Amounts arrive phrased for the billing mode, so no template writes
    "$" itself; "about" says "approximately"."""
    assert "$" not in text, f"{where}: bare $ in {text!r}"
    assert "≈" not in text and "~" not in text, f"{where}: approximation mark in {text!r}"


@pytest.mark.parametrize("where,text", _strings())
def test_the_mod_never_says_apply(where, text):
    """ClaudeGlass offers prompts and commands, never an apply button."""
    assert not re.search(r"\bapply\b", text, re.I), f"{where}: {text!r}"


@pytest.mark.parametrize("where,text", [pair for pair in _strings() if pair[0].startswith("COPY")])
def test_placeholders_are_single_lower_case_words(where, text):
    names = [name for _, name, _, _ in string.Formatter().parse(text) if name is not None]
    assert names == _PLACEHOLDER.findall(text), where
    for name in names:
        assert re.fullmatch(r"[a-z]+", name), f"{where}: placeholder {{{name}}} is not one lower-case word"


def test_ids_are_dotted_lower_case():
    for key in COPY:
        assert re.fullmatch(r"[a-z]+(?:\.[a-z]+)+", key), key


def test_every_glossary_entry_has_a_title_and_a_body_and_nothing_else():
    for key, entry in GLOSSARY.items():
        assert set(entry) == {"title", "body"}, key
        assert entry["title"] and entry["body"], key


def test_a_title_is_a_short_label_and_a_body_is_a_few_sentences():
    for key, entry in GLOSSARY.items():
        assert len(entry["title"].split()) <= 8, key
        assert not entry["title"].endswith("."), key
        assert 1 <= len(_sentences(entry["body"])) <= 4, key


def test_the_scan_would_catch_each_kind_of_problem():
    """A guard on the guard."""
    assert SNAKE_CASE.search("the cache_read total")
    assert BANNED.search("a top-level session") and BANNED.search("open the tab")
    assert FILLER.search("just do it") and CAMEL_CASE.search("set autoCompactWindow")
    assert not SNAKE_CASE.search(_without_placeholders("{pct} full")) and _without_placeholders("{a_b} x") == " x"

"""The safe tuning export (``tuning.py``): a document of counts and closed
words that carries what a tuning review needs from one machine to another.

Three kinds of test. The spec and the validator are tried on
``tests/fixtures/tuning/sample.json``, a small valid document by hand, with
one change at a time. The summary is held to the dashboard's copy rules.
``build`` is run over a world written to disk (six sessions, four agent
runs, Haiku's tag files, a settings.json) and checked against the
aggregators the dashboard uses on the same corpus, and against what the
world was made to hold.
"""

from __future__ import annotations

import copy
import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from claudeglass import PARSER_VERSION, __version__
from claudeglass import capture as capture_mod
from claudeglass import capture_catalogue as catalogue
from claudeglass import coaching as coaching_mod
from claudeglass import context_budget, habits, haiku_tags, hook_health, parse, ratings, rework, tuning
from claudeglass.capture_tags import GROUNDED_KEYS
from claudeglass.config import CaptureConfig, Config
from claudeglass.corpus import load_corpus
from claudeglass.pricing import load_pricing

from helpers import (
    assert_privacy_deep,
    attachment_line,
    system_line,
    tool_result_block,
    tool_use_block,
    turn_line,
    user_block_line,
    user_str_line,
    write_jsonl,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "tuning"
BASE = datetime(2026, 9, 18, 9, 0, 0, tzinfo=timezone.utc)
TODAY = date(2026, 9, 19)
DAYS = 30
TAG_IDS = ["task", "level", "size", "shift", "plan", "skill", "prior", "check"]
SENT_BACK = (
    "The user doesn't want to proceed with this tool use. The tool use was rejected (eg. if it was a file edit, "
    "the new_string was NOT written to the file). To tell you how to proceed, the user said:\n"
)

#: The copy rules of ``tests/test_help_coverage.py``: what the summary's lines
#: must keep to, as the dashboard's own words do.
BANNED = re.compile(
    r"\b(re-?cache[sd]?|top-level|briefing|cache_creation|cache_read|attribution_\w+|per_turn_\w+|tabs?)\b", re.I
)
SNAKE_CASE = re.compile(r"\b[a-z]+_[a-z0-9_]+\b")
CAMEL_CASE = re.compile(r"\b[a-z]+[A-Z][A-Za-z]*\b")
FILLER = re.compile(r"\b(just|simply)\b", re.I)
MAX_SENTENCE_WORDS = 25


@pytest.fixture(autouse=True)
def _salt():
    parse.set_salt(b"c" * 32)


def _plain(text: str, where: str = "summary") -> None:
    """The house copy rules for one string the dashboard or the summary shows."""
    assert not SNAKE_CASE.search(text), f"{where}: internal name in {text!r}"
    assert not BANNED.search(text), f"{where}: banned term in {text!r}"
    assert not FILLER.search(text), f"{where}: filler word in {text!r}"
    assert not CAMEL_CASE.search(text), f"{where}: camelCase name in {text!r}"
    assert " -- " not in text, f"{where}: dash aside in {text!r}"
    for sentence in re.split(r"(?<=[.?!])\s+", text):
        assert len(sentence.split()) <= MAX_SENTENCE_WORDS, f"{where}: sentence over 25 words: {sentence!r}"


# -- the sample document and ways to break it ------------------------------------------------


def _sample() -> dict:
    return json.loads((FIXTURES / "sample.json").read_text(encoding="utf-8"))


def _put(doc: dict, path: str, value) -> dict:
    """``doc`` with ``value`` at the dotted ``path`` (a list index is a number)."""
    *parents, last = path.split(".")
    node = doc
    for key in parents:
        node = node[int(key)] if isinstance(node, list) else node[key]
    if isinstance(node, list):
        node[int(last)] = value
    else:
        node[last] = value
    return doc


def _spec_nodes(node=None, path=""):
    """Every node of the spec tree with its path (``*`` for a map's value, ``[]`` for a list's)."""
    node = tuning.spec() if node is None else node
    yield path, node
    if isinstance(node, tuning.Obj):
        for key, child in node.fields.items():
            yield from _spec_nodes(child, f"{path}.{key}" if path else key)
    elif isinstance(node, tuning.Map):
        yield from _spec_nodes(node.item, f"{path}.*")
    elif isinstance(node, tuning.Arr):
        yield from _spec_nodes(node.item, f"{path}[]")


def test_the_sample_document_is_valid_and_holds_every_block():
    doc = tuning.read_file(FIXTURES / "sample.json")
    assert tuning.validate(doc) == []
    spec = tuning.spec()
    assert set(doc) == set(spec.fields) == {*tuning._HEADER, *tuning._BLOCKS}
    assert [name for name in doc if name in tuning._BLOCKS] == list(tuning._BLOCKS)
    assert (doc["kind"], doc["format"]) == (tuning.KIND, tuning.FORMAT) == ("claudeglass-tuning", 1)
    assert tuning.MAX_BYTES == 256 * 1024
    # What a person is shown to check is what a machine reads back.
    assert tuning.loads(tuning.dumps(doc)) == doc


@pytest.mark.parametrize(
    "path, value, check, leak",
    [
        ("window_days", -1, "window_days: must be 0 or more", ""),
        ("window_days", 1.5, "window_days: must be a whole number", ""),
        ("window_days", True, "window_days: must be a whole number", ""),
        ("window_days", "30", "window_days: must be a whole number", "30"),
        ("window_days", None, "window_days: must be a whole number", ""),
        ("pieces.rework_usd", -0.5, "pieces.rework_usd: must be 0 or more", ""),
        ("pieces.rework_usd", True, "pieces.rework_usd: must be a number", ""),
        ("pieces.rework_usd", "1.5", "pieces.rework_usd: must be a number", "1.5"),
        ("pieces.rework_usd", [1.5], "pieces.rework_usd: must be a number", ""),
        ("pieces.rework_usd", float("nan"), "pieces.rework_usd: must be a finite number", ""),
        ("pieces.rework_usd", float("inf"), "pieces.rework_usd: must be a finite number", ""),
        ("pieces.rework_usd", 10**13, "pieces.rework_usd: must be 0 or more and not absurdly large", ""),
        ("pieces.total", 10**13, "pieces.total: must be 0 or more and not absurdly large", ""),
        ("kind", "something-else", "kind: is not the value this version of the format has", "something-else"),
        ("format", 2, "format: is not the value this version of the format has", ""),
        ("format", True, "format: is not the value this version of the format has", ""),
        ("format", 1.0, "format: is not the value this version of the format has", ""),
        ("tool_version", "1.2.3 beta", "tool_version: must match the pattern the spec allows", "beta"),
        ("tool_version", "../../etc", "tool_version: must match the pattern the spec allows", "etc"),
        ("tool_version", "", "tool_version: must match the pattern the spec allows", ""),
        ("generated_on", "2026-02-30", "generated_on: must match the pattern the spec allows", "02-30"),
        ("generated_on", "yesterday", "generated_on: must match the pattern the spec allows", "yesterday"),
        ("generated_on", 20260919, "generated_on: must match the pattern the spec allows", ""),
        ("capture.level", "sudo", "capture.level: must be one of the words the spec allows", "sudo"),
        ("capture.level", ["standard"], "capture.level: must be one of the words the spec allows", ""),
        ("capture.level", None, "capture.level: must be one of the words the spec allows", ""),
        ("capture.metrics", {"task": 1}, "capture.metrics: must be a list", "task"),
        ("capture.metrics", "task", "capture.metrics: must be a list", ""),
        ("capture.metrics", ["not_a_metric"], "capture.metrics[0]: must be one of the words", "not_a_metric"),
        ("pieces.modes", [1], "pieces.modes: must be an object", ""),
        ("pieces.modes", "interactive", "pieces.modes: must be an object", ""),
        ("pieces.modes", {"interactive": "5"}, "pieces.modes.interactive: must be a whole number", ""),
        ("pieces.modes", {"interactive": 2.5}, "pieces.modes.interactive: must be a whole number", ""),
        ("pieces.modes", {"interactive": -2}, "pieces.modes.interactive: must be 0 or more", ""),
        ("pieces.modes", {"my-notes-session": 2}, "pieces.modes: has a key that is not in the spec", "my-notes"),
        ("agents.types", {"my-private-reviewer": 3}, "agents.types: has a key that is not in the spec", "private"),
        ("agents.models", {"claude-opus-4-1-20250805": {"runs": 1, "cost_usd": 1.0}}, "agents.models: has a key", "4-1"),
        ("agents.startup_diet_usd", {"my-private-reviewer": 2.0}, "agents.startup_diet_usd: has a key", "private"),
        ("agents.startup_diet_usd", {"custom": -1.0}, "agents.startup_diet_usd.custom: must be 0 or more", ""),
        ("agents.startup_diet_usd", {"custom": "2 USD"}, "agents.startup_diet_usd.custom: must be a number", ""),
        ("agents.probes", {"calls": 5, "single": 2, "by_shell": 1, "runs": 1, "files": 3}, "agents.probes: has a key", "files"),
        ("agents.probes", {"calls": 5, "single": -2}, "agents.probes.single: must be 0 or more", ""),
        ("agents.probes", {"calls": "5"}, "agents.probes.calls: must be a whole number", ""),
        ("agents.report_turns", {"acknowledged": 1, "ignored": 2}, "agents.report_turns: has a key", "ignored"),
        ("agents.report_turns", {"acted": 2.5}, "agents.report_turns.acted: must be a whole number", ""),
        ("agents.report_turns", ["acted"], "agents.report_turns: must be an object", ""),
        ("overhead.hooks", {"PreToolUse": {"runs": 1, "recorded": 0}}, "overhead.hooks: has a key", "PreToolUse"),
        ("overhead.entrypoints", {"cli": 1, "C": 1}, "overhead.entrypoints: has a key", ""),
        (
            "prompting.weeks",
            {"2026-W99": {"messages": 1, "habits": {}}},
            "prompting.weeks: has a key that is not in the spec",
            "W99",
        ),
        (
            "prompting.weeks",
            {"week of the move": {"messages": 1, "habits": {}}},
            "prompting.weeks: has a key that is not in the spec",
            "move",
        ),
        ("prompting.weeks", {"2026-W38": {"habits": {}}}, "prompting.weeks.2026-W38.messages: is missing", ""),
        ("tags.grounding", {"level:easy>hard": 1}, "tags.grounding: has a key that is not in the spec", "easy"),
        ("tags.grounding", {"task:chat>nothing": 1}, "tags.grounding: has a key that is not in the spec", "nothing"),
        ("tags.grounding", {"task:chat": 1}, "tags.grounding: has a key that is not in the spec", ""),
        ("tags.settling", {"plan:following>made extra": 1}, "tags.settling: has a key", "extra"),
        ("tags.words", {"mood": {}}, "tags.words: has a key that is not in the spec", "mood"),
        ("tags.words", {"task": {"someone": {"feature": 1}}}, "tags.words.task: has a key", "someone"),
        ("tags.words", {"task": {"claude": {"poetry": 1}}}, "tags.words.task.claude: has a key", "poetry"),
        ("tags.words", {"task": {"claude": [1]}}, "tags.words.task.claude: must be an object", ""),
        ("tags.admissions", {"claim": {"user": [1]}}, "tags.admissions.claim.user: must be an object", ""),
        ("capture.sample_percent", "100%", "capture.sample_percent: must be a whole number", "100%"),
    ],
)
def test_a_change_to_the_sample_fails_the_check_that_names_it(path, value, check, leak):
    problems = tuning.validate(_put(_sample(), path, value))
    assert any(check in problem for problem in problems), problems
    # A line says where and which check, never what was found there.
    if leak:
        assert not any(leak in problem for problem in problems), problems
    with pytest.raises(tuning.TuningError) as raised:
        tuning.dumps(_put(_sample(), path, value))
    assert raised.value.problems == problems
    if leak:
        assert leak not in str(raised.value)


def test_a_key_the_spec_does_not_name_fails_at_every_depth_and_is_never_repeated():
    doc = _sample()
    doc["session_name"] = "x"
    doc["capture"]["project"] = "x"
    doc["pieces"]["rework_by_level"]["easy"]["notes"] = 1
    doc["agents"]["judge"]["main"]["model"] = "claude-haiku-4-5"
    problems = tuning.validate(doc)
    assert sorted(problems) == sorted(
        [
            "document: has a key that is not in the spec",
            "capture: has a key that is not in the spec",
            "pieces.rework_by_level.easy: has a key that is not in the spec",
            "agents.judge.main: has a key that is not in the spec",
        ]
    )
    assert not any(word in " ".join(problems) for word in ("session_name", "project", "notes", "haiku"))


def test_a_missing_required_key_fails_and_a_missing_optional_one_does_not():
    doc = _sample()
    del doc["pieces"]
    del doc["capture"]["level"]
    del doc["agents"]["runs"]["direct"]["cost_usd"]
    assert sorted(tuning.validate(doc)) == [
        "agents.runs.direct.cost_usd: is missing",
        "capture.level: is missing",
        "pieces: is missing",
    ]
    doc = _sample()
    for path in ("capture.hints", "capture.metrics", "pieces.rounds", "pieces.drip_runs", "tags.grounding"):
        *parents, last = path.split(".")
        node = doc
        for key in parents:
            node = node[key]
        del node[last]
    del doc["prompting"]["weeks"]["2026-W37"]["rates"]
    assert tuning.validate(doc) == []


@pytest.mark.parametrize("doc", [[], "text", None, 5, 1.5, True, [{"kind": "claudeglass-tuning"}]])
def test_a_document_that_is_not_an_object_fails_whole(doc):
    assert tuning.validate(doc) == ["document: must be an object"]


def test_a_dict_where_the_spec_wants_a_list_and_a_list_where_it_wants_a_dict_both_fail():
    doc = _sample()
    doc["capture"]["metrics"] = {"task": "level"}
    doc["capture"]["coaching"] = {}
    doc["pieces"]["rework_by_week"] = ["2026-W37"]
    doc["agents"]["runs"] = []
    doc["tags"]["words"]["task"]["claude"] = [("feature", 1)]
    assert sorted(tuning.validate(doc)) == [
        "agents.runs: must be an object",
        "capture.coaching: must be a list",
        "capture.metrics: must be a list",
        "pieces.rework_by_week: must be an object",
        "tags.words.task.claude: must be an object",
    ]


def test_a_list_or_map_over_its_limit_fails_without_reading_every_item():
    doc = _sample()
    doc["capture"]["metrics"] = ["task"] * (len(catalogue.METRICS_BY_ID) + 1)
    doc["pieces"]["rework_by_week"] = {
        f"{year}-W{week:02d}": {"pieces": 1, "reworked": 0} for year in range(2020, 2030) for week in range(1, 53)
    }
    assert sorted(tuning.validate(doc)) == [
        "capture.metrics: has more items than the spec allows",
        "pieces.rework_by_week: has more entries than the spec allows",
    ]


def test_what_json_cannot_hold_fails_as_a_document_that_cannot_be_written():
    for hold in (lambda d: d["capture"]["thresholds"].update({(1, 2): 1.0}), lambda d: d.update(extra=object())):
        doc = _sample()
        hold(doc)
        problems = tuning.validate(doc)
        assert "document: cannot be written as plain JSON" in problems
        assert any("not in the spec" in problem for problem in problems)
    doc = _sample()
    doc["pieces"]["rework_usd"] = float("nan")
    assert tuning.validate(doc) == [
        "pieces.rework_usd: must be a finite number",
        "document: cannot be written as plain JSON",
    ]


# -- the second scan --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, what",
    [
        ("C:\\Users\\me\\project", "a drive path"),
        ("D:/work/project", "a drive path"),
        ("/home/me/project", "a home folder path"),
        ("home/me", "a home folder path"),
        ("Users\\me", "a Users folder path"),
        ("Users/me", "a Users folder path"),
        ("/c/Dev/project", "a Git Bash drive path"),
        ("me@example", "an at sign"),
        ("https://example/x", "a web address"),
        ("ftp://example", "a web address"),
        ("www.example", "a web address"),
    ],
)
def test_the_second_scan_catches_what_the_spec_would_have_let_through(monkeypatch, text, what):
    # A spec that accepted any string for a version: the scan is the second wall.
    monkeypatch.setitem(tuning._HEADER, "tool_version", tuning.Pattern(re.compile(r".+")))
    doc = _put(_sample(), "tool_version", text)
    assert f"document: holds {what}" in tuning.validate(doc)
    assert not any(text in problem for problem in tuning.validate(doc))
    # The same string under a key is caught in the key too.
    monkeypatch.setitem(tuning._HEADER, "generated_on", tuning.Pattern(re.compile(r".+")))
    assert f"document: holds {what}" in tuning.validate(_put(_sample(), "generated_on", text))


def test_the_second_scan_leaves_the_words_the_document_really_uses_alone():
    doc = _sample()
    doc["overhead"]["entrypoints"] = {"claude-desktop": 1, "claude-vscode": 1, "sdk-cli": 1}
    doc["tags"]["grounding"] = {"task:chat>research": 1, "shift:fix>": 1}
    assert tuning.validate(doc) == []


def test_no_closed_word_of_the_spec_could_be_a_path_an_address_or_free_text():
    word = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
    seen = 0
    for path, node in _spec_nodes():
        if isinstance(node, tuning.Word):
            words = node.words
        elif isinstance(node, tuning.Map) and isinstance(node.keys, tuning.Closed):
            words = node.keys.words
        elif isinstance(node, tuning.Obj):
            words = set(node.fields)
        else:
            continue
        for item in words:
            seen += 1
            assert word.fullmatch(item), (path, item)
            assert not any(pattern.search(item) for _what, pattern in tuning._LEAKS), (path, item)
    assert seen > 200


def test_every_leaf_of_the_spec_is_a_number_a_closed_word_or_a_tight_pattern():
    hostile = ["C:\\Users\\me", "me@example.com", "https://example.com", "/home/me", "free text here", "x" * 300]
    leaves = Counter()
    for path, node in _spec_nodes():
        kind = type(node).__name__
        assert kind in {"Obj", "Map", "Arr", "Count", "Num", "Const", "Word", "Pattern"}, (path, kind)
        leaves[kind] += 1
        if isinstance(node, tuning.Pattern):
            assert not any(node.regex.fullmatch(text) for text in hostile), path
        if isinstance(node, tuning.Map):
            assert isinstance(node.keys, (tuning.Closed, tuning.Weeks, tuning.Changes)), path
        if isinstance(node, tuning.Obj):
            assert set(node.required) <= set(node.fields), path
    assert leaves["Pattern"] == 2 and leaves["Const"] == 2


def test_amounts_are_named_usd_and_no_other_number_is_an_amount():
    amounts = []
    for path, node in _spec_nodes():
        if isinstance(node, tuning.Obj):
            for key, child in node.fields.items():
                if isinstance(child, tuning.Num):
                    amounts.append(key)
    assert amounts
    assert all(key.endswith("_usd") or key == "median_ms" for key in amounts), amounts


def test_the_fullest_document_the_spec_allows_is_valid_and_fits_the_size_limit():
    examples = {tuning._VERSION_RE: "0.14.0", tuning._DATE_RE: "2026-09-19"}

    def changes() -> list[str]:
        found = []
        for name in GROUNDED_KEYS:
            vocab = catalogue.TAG_VOCAB[name]
            found += [f"{name}:{old}>{new}" for old in vocab for new in ("", *vocab)]
        return found

    def weeks(n: int) -> list[str]:
        return [f"{2020 + i // 52}-W{i % 52 + 1:02d}" for i in range(n)]

    def fill(node):
        if isinstance(node, tuning.Count):
            return 123456
        if isinstance(node, tuning.Num):
            return 123456.789012
        if isinstance(node, tuning.Const):
            return node.expected
        if isinstance(node, tuning.Word):
            return sorted(node.words)[0]
        if isinstance(node, tuning.Pattern):
            return examples[node.regex]
        if isinstance(node, tuning.Obj):
            return {key: fill(child) for key, child in node.fields.items()}
        if isinstance(node, tuning.Arr):
            return [fill(node.item)] * node.limit
        keys = node.keys
        names = (
            sorted(keys.words)
            if isinstance(keys, tuning.Closed)
            else weeks(node.limit)
            if isinstance(keys, tuning.Weeks)
            else changes()[: node.limit]
        )
        return {key: fill(node.item) for key in names}

    doc = fill(tuning.spec())
    assert tuning.validate(doc) == []
    assert len(tuning.dumps(doc).encode("utf-8")) < tuning.MAX_BYTES
    # Every change grounding can make has a place.
    assert len(changes()) <= tuning._MAX_CHANGES
    assert all(tuning.Changes().accepts(change) for change in changes())
    assert tuning.summary_text(doc)


# -- size, files and text ---------------------------------------------------------------------


def test_a_document_over_the_size_limit_fails_whatever_it_holds(monkeypatch):
    doc = _sample()
    assert tuning.validate(doc) == []
    monkeypatch.setattr(tuning, "MAX_BYTES", len(tuning.dumps(doc).encode("utf-8")) - 2)
    assert tuning.validate(doc) == ["document: is over the size limit"]
    with pytest.raises(tuning.TuningError):
        tuning.dumps(doc)
    with pytest.raises(tuning.TuningError):
        tuning.summary_text(doc)


def test_text_over_twice_the_limit_is_refused_before_it_is_parsed(monkeypatch):
    monkeypatch.setattr(tuning, "MAX_BYTES", 100)
    with pytest.raises(tuning.TuningError) as raised:
        tuning.loads("[" * 201)
    assert raised.value.problems == ["file: is over the size limit"]


def test_a_file_over_twice_the_limit_is_refused_unread(tmp_path, monkeypatch):
    monkeypatch.setattr(tuning, "MAX_BYTES", 100)
    big = tmp_path / "big.json"
    # Not text at all: a read would say so, and the size check says its piece first.
    big.write_bytes(b"\xff" * 201)
    with pytest.raises(tuning.TuningError) as raised:
        tuning.read_file(big)
    assert raised.value.problems == ["file: is over the size limit"]


def test_loads_says_not_json_and_validates_what_parses():
    for text in ("", "not json", "{'kind': 1}", "[" * 100_000, '{"kind": NaN}x'):
        with pytest.raises(tuning.TuningError) as raised:
            tuning.loads(text)
        assert raised.value.problems == ["file: is not JSON"]
    with pytest.raises(tuning.TuningError) as raised:
        tuning.loads("[]")
    assert raised.value.problems == ["document: must be an object"]
    with pytest.raises(tuning.TuningError) as raised:
        tuning.loads(json.dumps({**_sample(), "pieces": {"total": "C:\\Users\\me"}}))
    assert "Users" not in str(raised.value)


def test_a_file_with_a_key_twice_is_refused_whatever_the_dropped_one_held():
    text = tuning.dumps(_sample())
    bs = chr(92)
    top = text.replace('"kind": "claudeglass-tuning"', '"kind": "C:' + bs * 2 + 'Users", "kind": "claudeglass-tuning"', 1)
    nested = text.replace('"level": "standard"', '"level": "off", "level": "standard"', 1)
    for bad in (top, nested):
        assert bad != text
        with pytest.raises(tuning.TuningError) as raised:
            tuning.loads(bad)
        assert raised.value.problems == ["file: has a key more than once"]


def test_a_leak_found_by_both_scans_is_one_problem_line():
    doc = _sample()
    doc["capture"]["level"] = "C:" + chr(92) + "x"
    with pytest.raises(tuning.TuningError) as raised:
        tuning.loads(json.dumps(doc))
    assert raised.value.problems.count("document: holds a drive path") == 1


def test_read_file_round_trips_and_never_names_the_path(tmp_path):
    doc = _sample()
    path = tmp_path / "tuning.json"
    path.write_text(tuning.dumps(doc), encoding="utf-8")
    assert tuning.read_file(path) == doc
    assert tuning.read_file(str(path)) == doc
    for bad in (tmp_path / "missing.json", tmp_path):
        with pytest.raises(tuning.TuningError) as raised:
            tuning.read_file(bad)
        assert raised.value.problems == ["file: cannot be read as text"]
        assert str(tmp_path) not in str(raised.value)
    path.write_bytes(b"\xff\xfe\x00\x00")
    with pytest.raises(tuning.TuningError) as raised:
        tuning.read_file(path)
    assert raised.value.problems == ["file: cannot be read as text"]


def test_dumps_writes_what_a_person_can_read_and_ends_the_line():
    text = tuning.dumps(_sample())
    assert text.endswith("}\n") and text.count("\n") > 100
    assert json.loads(text) == _sample()
    assert text.isascii()


def test_a_tuning_error_shows_five_problems_and_counts_the_rest():
    error = tuning.TuningError([f"p{i}: bad" for i in range(8)])
    assert str(error) == "p0: bad; p1: bad; p2: bad; p3: bad; p4: bad; and 3 more"
    assert error.problems[-1] == "p7: bad"
    assert str(tuning.TuningError("file: is not JSON")) == "file: is not JSON"
    assert isinstance(error, ValueError)


# -- adding a block (the next phase) ----------------------------------------------------------


def test_a_block_is_added_by_one_registration_and_older_documents_still_pass(monkeypatch):
    monkeypatch.setattr(tuning, "_BLOCKS", dict(tuning._BLOCKS))
    node = tuning.Obj(
        {"kinds": tuning.Map(["alpha", "beta"], tuning.Obj({"spend_usd": tuning.Num()}, required=("spend_usd",)))},
        required=("kinds",),
    )

    @tuning._block("spend_kinds", node, required=False)
    def _kinds(ctx):
        return {"kinds": {"alpha": {"spend_usd": 1.5}}}

    assert list(tuning.spec().fields)[-1] == "spend_kinds"
    # A document made before the block was added has no such key, and is valid.
    assert tuning.validate(_sample()) == []
    doc = {**_sample(), "spend_kinds": {"kinds": {"beta": {"spend_usd": 2.0}}}}
    assert tuning.validate(doc) == []
    for path, value, check in (
        ("spend_kinds.kinds", {"gamma": {"spend_usd": 1.0}}, "spend_kinds.kinds: has a key"),
        ("spend_kinds.kinds", {"alpha": {"spend_usd": -1.0}}, "spend_usd: must be 0 or more"),
        ("spend_kinds.kinds", {"alpha": {"spend_usd": "9"}}, "spend_usd: must be a number"),
        ("spend_kinds.kinds", {"alpha": {}}, "spend_usd: is missing"),
        ("spend_kinds.kinds", [], "spend_kinds.kinds: must be an object"),
    ):
        assert any(check in problem for problem in tuning.validate(_put(copy.deepcopy(doc), path, value)))
    # The block is built beside the others and checked with them.
    built = tuning.build(NS(sessions=[]), Config(), None, config_dir=None, days=7, today=TODAY, claude_root=None)
    assert built["spend_kinds"] == {"kinds": {"alpha": {"spend_usd": 1.5}}}
    assert list(built)[-1] == "spend_kinds"
    # A block that must be there fails a document without it.
    monkeypatch.setitem(tuning._BLOCKS, "spend_kinds", (node, _kinds, True))
    assert tuning.validate(_sample()) == ["spend_kinds: is missing"]


def test_a_builder_that_writes_something_the_spec_refuses_stops_the_build(monkeypatch, tmp_path):
    original = tuning._BLOCKS["pieces"]

    def leaky(ctx):
        block = original[1](ctx)
        block["total"] = "C:\\Users\\me\\project"
        return block

    monkeypatch.setitem(tuning._BLOCKS, "pieces", (original[0], leaky, original[2]))
    with pytest.raises(tuning.TuningError) as raised:
        tuning.build(NS(sessions=[]), Config(), None, config_dir=None, days=7, today=TODAY, claude_root=tmp_path)
    problems = raised.value.problems
    assert problems[0] == "pieces.total: must be a whole number"
    assert problems[1:] and all(line.startswith("document: holds ") for line in problems[1:])
    assert not any("project" in line for line in problems)


# -- the summary ------------------------------------------------------------------------------


def _required_only(node):
    """The smallest document the spec accepts: required keys, zero counts, empty maps."""
    examples = {tuning._VERSION_RE: "0.14.0", tuning._DATE_RE: "2026-09-19"}
    if isinstance(node, tuning.Count):
        return 0
    if isinstance(node, tuning.Num):
        return 0.0
    if isinstance(node, tuning.Const):
        return node.expected
    if isinstance(node, tuning.Word):
        return sorted(node.words)[0]
    if isinstance(node, tuning.Pattern):
        return examples[node.regex]
    if isinstance(node, tuning.Obj):
        return {key: _required_only(node.fields[key]) for key in node.required}
    if isinstance(node, tuning.Arr):
        return []
    return {}


def test_the_summary_validates_first_and_a_bad_document_prints_nothing():
    for bad in (_put(_sample(), "pieces.total", "C:\\Users\\me"), [], {}, _put(_sample(), "extra", 1)):
        with pytest.raises(tuning.TuningError):
            tuning.summary_text(bad)


def test_the_summary_is_a_few_plain_lines_a_block_with_the_plans_pieces_line():
    text = tuning.summary_text(_sample())
    lines = text.splitlines()
    assert lines[0] == "Tuning figures made on 2026-09-19 for the last 30 days, by ClaudeGlass 0.14.0."
    assert "Pieces of work: 67." in lines
    assert "Of the 55 delivered pieces with a clear start, 10 needed changes after delivery." in lines
    assert "Hooks: SessionStart ran 54 times, median 140 ms; Stop ran 800 times, no time recorded." in lines
    assert "Haiku judged 15 replies and 12 agent runs for $0.04, and 3 failed." in lines
    assert "Capture: level standard, tags written by claude, 100% of sessions, 4 metrics on." in lines
    assert "Prompting: 120 messages typed, plus 9 typed while Claude worked." in lines
    assert "Cold returns: 6, rewriting 900,000 cache tokens. Wake-ups: 2, rewriting 260,000." in lines
    assert "Agent runs: 21 direct costing $14.50, 9 from workflows costing $6.25." in lines
    assert "Capture cost $4.20 and coaching notes $0.35 at list prices." in lines
    for name in ("Tips:", "Plans:", "Tags:", "Rework:", "Agent answers:", "Hooks:", "Limits:"):
        assert any(line.startswith(name) for line in lines), name
    assert len(lines) < 50


def test_the_summary_gives_the_tools_list_saving_by_agent_type_when_the_file_has_one():
    lines = tuning.summary_text(_sample()).splitlines()
    assert (
        "A tools list on your agents would save about $5.50 over the window at list prices. "
        "By type: workflow-subagent $3.50, general-purpose $1.25 and custom $0.75."
    ) in lines
    for line in lines:
        _plain(line)
    # A file without it, as an older one is, has no such line and is still valid.
    doc = _sample()
    del doc["agents"]["startup_diet_usd"]
    assert tuning.validate(doc) == []
    assert "tools list" not in tuning.summary_text(doc)


def test_the_summary_keeps_to_the_dashboards_copy_rules():
    for line in tuning.summary_text(_sample()).splitlines():
        _plain(line)
    # The stored words are snake_case and run together; the lines spell them out.
    text = tuning.summary_text(_sample())
    for word in ("five-hour", "context carried", "no word", "haiku-fallback", "plan rejected"):
        assert word in text
    assert "_" not in text and " -- " not in text


def test_the_summary_counts_one_in_the_singular_and_the_rest_in_the_plural():
    doc = _sample()
    doc["prompting"]["totals"].update(typed=1, go_aheads=1, status_checks=0, corrections=1, adjustments=2)
    doc["tags"]["grounding"] = {"task:chat>research": 1}
    doc["tags"]["settling"] = {}
    doc["tags"]["unconfirmed_admissions"] = 1
    doc["tags"]["admissions"] = {"claim": {"user": {"claude": 1}}}
    doc["pieces"]["rework_cycles"] = 1
    text = tuning.summary_text(doc)
    assert "Prompting: 1 message typed" in text
    assert "1 go-ahead, 0 status checks, 1 correction, 2 adjustments" in text
    assert "Words were put right 1 time by the hook and 0 times by the transcript." in text
    assert "Claude admitted 1 mistake: you caught 1 and it caught 0. 1 more reply read like" in text
    assert "Rework: 1 follow-up after delivery" in text
    for line in text.splitlines():
        _plain(line)


def test_the_smallest_valid_document_has_a_summary_too():
    doc = _required_only(tuning.spec())
    assert tuning.validate(doc) == []
    text = tuning.summary_text(doc)
    assert "Pieces of work: 0." in text.splitlines()
    assert "Capture cost $0.00 and coaching notes $0.00 at list prices." in text
    for line in text.splitlines():
        _plain(line)


def test_a_small_amount_shows_four_places_so_it_does_not_read_as_nothing():
    assert tuning._usd(0.0014) == "$0.0014"
    assert tuning._usd(0.0) == "$0.00"
    assert tuning._usd(0.01) == "$0.01"
    assert tuning._usd(12345.6) == "$12,345.60"


# -- the small readers -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "n, bounds, words, expected",
    [
        (0, tuning._COUNT_BOUNDS, tuning.COUNT_BUCKETS, "one"),
        (1, tuning._COUNT_BOUNDS, tuning.COUNT_BUCKETS, "one"),
        (2, tuning._COUNT_BOUNDS, tuning.COUNT_BUCKETS, "two"),
        (3, tuning._COUNT_BOUNDS, tuning.COUNT_BUCKETS, "three"),
        (4, tuning._COUNT_BOUNDS, tuning.COUNT_BUCKETS, "four_to_six"),
        (6, tuning._COUNT_BOUNDS, tuning.COUNT_BUCKETS, "four_to_six"),
        (7, tuning._COUNT_BOUNDS, tuning.COUNT_BUCKETS, "seven_or_more"),
        (500, tuning._COUNT_BOUNDS, tuning.COUNT_BUCKETS, "seven_or_more"),
        (30, tuning._GAP_BOUNDS, tuning.GAP_BUCKETS, "to_1m"),
        (60, tuning._GAP_BOUNDS, tuning.GAP_BUCKETS, "to_1m"),
        (61, tuning._GAP_BOUNDS, tuning.GAP_BUCKETS, "to_5m"),
        (1200, tuning._GAP_BOUNDS, tuning.GAP_BUCKETS, "to_20m"),
        (1201, tuning._GAP_BOUNDS, tuning.GAP_BUCKETS, "over_20m"),
        (0.5, tuning._HOUR_BOUNDS, tuning.HOUR_BUCKETS, "to_1h"),
        (3.5, tuning._HOUR_BOUNDS, tuning.HOUR_BUCKETS, "to_4h"),
        (8.0, tuning._HOUR_BOUNDS, tuning.HOUR_BUCKETS, "to_8h"),
        (8.1, tuning._HOUR_BOUNDS, tuning.HOUR_BUCKETS, "over_8h"),
    ],
)
def test_a_bucket_is_the_first_word_whose_bound_holds_the_number(n, bounds, words, expected):
    assert tuning._bucket(n, bounds, words) == expected
    assert len(bounds) + 1 == len(words)


@pytest.mark.parametrize(
    "monday, week",
    [
        ("2026-09-14", "2026-W38"),
        ("2026-12-28", "2026-W53"),
        ("2027-01-04", "2027-W01"),
        ("2020-12-28", "2020-W53"),
        ("", ""),
        ("not a date", ""),
        # A bad clock is no week the spec has room for.
        ("1970-01-05", ""),
        ("2101-01-04", ""),
    ],
)
def test_the_dashboards_monday_becomes_an_iso_week_or_nothing(monday, week):
    assert tuning._iso_week(monday) == week


def test_an_amount_is_a_plain_finite_number_of_six_places_or_zero():
    assert tuning._num(1.23456789) == 1.234568
    assert tuning._num(3) == 3.0
    for bad in (-3, -0.1, float("nan"), float("inf"), float("-inf"), "x", None, [1]):
        assert tuning._num(bad) == 0.0
    assert tuning._num("2.5") == 2.5


def test_the_changes_grounding_makes_are_a_grounded_key_and_words_of_its_vocabulary():
    changes = tuning.Changes()
    assert changes.accepts("task:chat>research") and changes.accepts("shift:fix>") and changes.accepts("plan:made>none")
    for bad in ("level:easy>hard", "task:chat", "task:nope>chat", "task:chat>nope", ":a>b", "task:chat>research>x", "x"):
        assert not changes.accepts(bad), bad


def test_the_closed_sets_come_from_the_catalogues():
    assert tuning.WRITERS == ("claude", "haiku", "haiku-fallback")
    assert set(catalogue.JUDGE_WRITERS) <= set(tuning.WRITERS)
    assert tuning.LEVELS == (*catalogue.LEVELS, "custom")
    assert set(tuning.HINTS) == set(catalogue.FEEDBACK_VOCAB["tip_hint"])
    assert tuning.VERDICTS == ("done", "partial", "blocked", "none")
    assert "custom" in tuning.AGENT_TYPES and "other" in tuning.ENTRYPOINTS and "other" in tuning.MODELS
    assert {"Explore", "Plan", "general-purpose", "workflow-subagent"} <= set(tuning.AGENT_TYPES)
    assert {"SessionStart", "Stop", "UserPromptSubmit", "SubagentStart"} <= set(tuning.HOOK_EVENTS)
    assert tuning.LIMIT_KINDS == ("five_hour", "weekly")


# -- a world on disk ---------------------------------------------------------------------------


def _at(second: int) -> str:
    return (BASE + timedelta(seconds=second)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _note(second: int) -> dict:
    text = catalogue.note_text(TAG_IDS, "main")
    wrapped = f"<system-reminder>\nSessionStart hook additional context: {text}\n</system-reminder>"
    line = attachment_line(
        "hook_additional_context",
        rendered=wrapped,
        content=[text],
        hookName="SessionStart",
        hookEvent="SessionStart",
        toolUseID="SessionStart",
    )
    line["timestamp"] = _at(second)
    return line


def _human(second: int, text: str, **kw) -> dict:
    return user_str_line(text, origin={"kind": "human"}, timestamp=_at(second), **kw)


def _say(second: int, text: str, **kw) -> dict:
    return turn_line(content=[{"type": "text", "text": text}], timestamp=_at(second), **kw)


def _edit(second: int, n: int, path: str, **kw) -> dict:
    block = tool_use_block("Edit", f"tu_{n}", {"file_path": path, "old_string": "a", "new_string": "b"})
    return turn_line(content=[block], timestamp=_at(second), **kw)


def _ok(second: int, n: int) -> dict:
    return user_block_line([tool_result_block(f"tu_{n}", "ok")], timestamp=_at(second))


def _queued(second: int, text: str) -> dict:
    line = attachment_line("queued_command", prompt=text, commandMode="prompt", origin={"kind": "human"})
    line["timestamp"] = _at(second)
    return line


def _notice(second: int) -> dict:
    text = "<task-notification>\n<task-id>bg1</task-id>\n<status>completed</status>\n<result>ok</result>\n</task-notification>"
    return user_str_line(text, timestamp=_at(second))


def _plan_call(second: int, tool_id: str) -> dict:
    plan = "# Plan\n\n1. Edit src/fetch.py\n2. Run tests/test_fetch.py\n"
    return turn_line(content=[tool_use_block("ExitPlanMode", tool_id, {"plan": plan})], timestamp=_at(second))


def _asked(questions) -> list[dict]:
    return [
        {
            "question": q.question,
            "header": q.header,
            "multiSelect": q.multi,
            "options": [{"label": label, "description": text} for _word, label, text in q.options],
        }
        for q in questions
    ]


def _feedback_lines(start: int) -> list[dict]:
    """A /cg-feedback run that answers the outcome and the worth of the piece."""
    ask = {q.key: q for q in catalogue.FEEDBACK_QUESTIONS}
    core = _asked(catalogue.feedback_questions()[0])
    answers = {ask["outcome"].question: "Yes", ask["worth"].question: "Too costly"}
    return [
        user_str_line(
            "<command-message>cg-feedback</command-message>\n<command-name>/cg-feedback</command-name>",
            timestamp=_at(start),
        ),
        user_block_line([{"type": "text", "text": catalogue.feedback_skill_text()}], isMeta=True, timestamp=_at(start)),
        turn_line(content=[tool_use_block("AskUserQuestion", "tu_q", {"questions": core})], timestamp=_at(start + 1)),
        user_block_line(
            [tool_result_block("tu_q", "User has answered your questions.")],
            toolUseResult={"questions": core, "answers": answers},
            timestamp=_at(start + 2),
        ),
        _say(start + 3, "Thanks: ClaudeGlass will use this for your savings tips."),
    ]


def _agent_turn(second: int, message_id: str, *blocks, model: str = "claude-sonnet-5", **kw) -> dict:
    return turn_line(content=list(blocks), timestamp=_at(second), message_id=message_id, model=model, **kw)


def _write_agent(folder: Path, name: str, meta: dict, lines: list[dict]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    write_jsonl(folder / f"{name}.jsonl", lines)
    (folder / f"{name}.meta.json").write_text(json.dumps(meta), encoding="utf-8")


@dataclass
class World:
    corpus: object
    pricing: object
    config: Config
    config_dir: Path
    claude_root: Path
    root: Path


#: Names that live only in the world's transcripts, tag files and folders: none may reach the document.
PRIVATE = (
    "my-private-reviewer", "proj-alpha", "proj-beta", "src/app.py", "src/lib.py", "src/fetch.py", "login", "logout",
    "msg_wake", "msg_a1", "msg_a2", "msg_l1", "claude-haiku-4-5", "claude-sonnet-5", "workflow-subagent-1", "wf_1",
    "timeouts", "retry logic", "agent-a1", "bg1",
)  # fmt: skip


def _make_world(root: Path) -> World:
    """Five main sessions across two projects, four agent runs under the
    first, Haiku's tag files, and a settings.json that runs the capture hook
    on two events.

    * s1: a piece delivered, a correction that admits a mistake, a second
      piece typed after a long gap (a cold return), a message typed while
      Claude worked, and a reply a background task woke an hour later.
    * s2: a plan sent back once with a question, then approved, and a
      /cg-feedback run.
    * s3: Claude working alone from midnight to the small hours.
    * s4: small requests one after another, then a correction whose reply
      reads like an admission with no tag.
    * s5: a session limit and a weekly limit.
    """
    projects = root / "projects"
    alpha, beta = projects / "proj-alpha", projects / "proj-beta"
    alpha.mkdir(parents=True)
    beta.mkdir(parents=True)
    first = [
        _note(0),
        attachment_line(
            "hook_success",
            hookName="SessionStart",
            hookEvent="SessionStart",
            command=f"python {catalogue.HOOK_SCRIPT}",
            durationMs=140,
        ),
        _human(10, "add the login form to src/app.py", entrypoint="cli"),
        _edit(11, 1, "src/app.py"),
        _ok(12, 1),
        _say(13, "Done.\n[cg: task=feature level=hard shift=new size=m plan=none check=targeted]"),
        _human(60, "no, that's wrong, it's broken"),
        _edit(61, 2, "src/app.py"),
        _ok(62, 2),
        _say(
            63,
            "Sorry, my mistake, I missed the validation.\n[cg: task=bugfix level=normal shift=fix why=missed admit=claim]",
            cache_read_input_tokens=120_000,
        ),
        _human(1000, "add the logout button to src/lib.py"),
        _edit(1001, 3, "src/lib.py", cache_creation_input_tokens=50_000, ephemeral_5m_input_tokens=50_000),
        _queued(1002, "also use the new button style"),
        _ok(1003, 3),
        _say(1004, "Done.\n[cg: task=feature level=easy shift=new size=s]"),
        _notice(5004),
        _say(5005, "The background tests passed.", message_id="msg_wake", cache_creation_input_tokens=30_000),
    ]
    write_jsonl(alpha / "s1.jsonl", first)
    folder = alpha / "s1" / "subagents"
    haiku = "claude-haiku-4-5"
    _write_agent(
        folder,
        "agent-a1",
        {"agentType": "Explore", "model": haiku},
        [
            _agent_turn(1002, "msg_a1_1", {"type": "text", "text": "Found it."}, model=haiku),
            _agent_turn(1003, "msg_a1_2", tool_use_block("StructuredOutput", "tu_so", {"answer": "x"}), model=haiku),
        ],
    )
    _write_agent(
        folder,
        "agent-a2",
        {"agentType": "my-private-reviewer"},
        [_agent_turn(1002, "msg_a2_1", {"type": "text", "text": "Half done."})],
    )
    _write_agent(
        folder,
        "agent-a3",
        {"agentType": "general-purpose"},
        [_agent_turn(1002, "msg_a3_1", tool_use_block("SubagentHandback", "tu_hb", {"result": "x"}))],
    )
    _write_agent(
        folder / "workflows" / "wf_1",
        "agent-w1",
        {"agentType": "workflow-subagent", "model": "sonnet"},
        [_agent_turn(1002, "msg_w1_1", {"type": "text", "text": "Done."})],
    )
    second = [
        _note(0),
        _human(10, "plan the retry logic for src/fetch.py", entrypoint="claude-desktop"),
        _plan_call(11, "tu_p1"),
        user_block_line(
            [tool_result_block("tu_p1", SENT_BACK + "what about timeouts?", is_error=True)],
            toolDenialKind="user-rejected",
            timestamp=_at(14),
        ),
        _plan_call(20, "tu_p2"),
        user_block_line([tool_result_block("tu_p2", "User has approved your plan.")], timestamp=_at(25)),
        _edit(26, 5, "src/fetch.py"),
        _ok(27, 5),
        _say(28, "Done.\n[cg: task=feature level=normal shift=new plan=following check=run]"),
        *_feedback_lines(40),
    ]
    write_jsonl(beta / "s2.jsonl", second)
    night = [_note(-37800), _human(-37800, "refactor the whole parser module overnight please", entrypoint="cli")]
    for i in range(42):
        at = -36000 + 300 * i
        night += [_edit(at, 100 + i, f"src/mod{i % 3}.py"), _ok(at + 1, 100 + i)]
    night.append(_say(-36000 + 300 * 42, "All modules refactored.\n[cg: task=refactor level=hard shift=new size=xl]"))
    write_jsonl(alpha / "s3.jsonl", night)
    drip = [_note(0)]
    for i, text in enumerate(
        ("add a test for the retry", "add a test for the timeout", "add a docstring to the helper", "rename the helper")
    ):
        at = 20000 + 120 * i
        drip += [
            _human(at, text, **({"entrypoint": "cli"} if i == 0 else {})),
            _edit(at + 5, 200 + i, "src/util.py"),
            _ok(at + 6, 200 + i),
            _say(at + 10, f"Done {i}."),
        ]
    drip += [
        _human(20700, "no, that's wrong, it's broken"),
        _edit(20705, 210, "src/util.py"),
        _ok(20706, 210),
        _say(20710, "Sorry, my mistake, I left the import out. It is fixed now."),
    ]
    write_jsonl(beta / "s4.jsonl", drip)
    limited = [
        _say(30000, "Working on it.", message_id="msg_l1", input_tokens=30_000),
        turn_line(
            message_id="msg_lim1",
            model="<synthetic>",
            isApiErrorMessage=True,
            input_tokens=0,
            output_tokens=0,
            content=[{"type": "text", "text": "You've hit your session limit"}],
            timestamp=_at(30005),
            quotaLimits={"resetsAt": (BASE + timedelta(seconds=33600)).timestamp()},
        ),
        _human(33600, "ok it has reset, carry on"),
        _say(33610, "Carrying on.", message_id="msg_l2", input_tokens=30_000, cache_creation_input_tokens=25_000),
        turn_line(
            message_id="msg_lim2",
            model="<synthetic>",
            isApiErrorMessage=True,
            input_tokens=0,
            output_tokens=0,
            content=[{"type": "text", "text": "You've hit your weekly limit"}],
            timestamp=_at(33700),
            quotaLimits={"resetsAt": (BASE + timedelta(seconds=90000)).timestamp()},
        ),
    ]
    write_jsonl(beta / "s5.jsonl", limited)
    config_dir = root / "cg"
    tags = config_dir / "tags"
    tags.mkdir(parents=True)
    records = [
        # Haiku fell back to tagging a reply Claude left bare; another it tagged as the tagger (no writer mark).
        {"ts": "2026-09-18T10:00:00Z", "reply": "msg_wake", "tl": "task=research level=easy", "w": "haiku-fallback",
         "g": "task:chat>research", "usd": 0.0011},
        {"ts": "2026-09-18T10:00:30Z", "reply": "msg_l1", "tl": "task=chat level=easy", "usd": 0.0012},
        {"ts": "2026-09-18T10:01:00Z", "reply": "msg_gone", "err": "timeout"},
        {"ts": "2026-09-18T10:02:00Z", "reply": "msg_a1_2", "agent": "result=done", "usd": 0.0009},
        {"ts": "2026-09-18T10:03:00Z", "reply": "msg_a2_1", "agent": "", "err": "no_answer"},
        {"ts": "2026-09-18T10:04:00Z", "reply": "msg_a3_1", "agent": "result=partial retry=brief", "usd": 0.0008},
    ]  # fmt: skip
    (tags / "2026-09.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    # Older than the window, and in a month file of its own: not counted.
    (tags / "2026-07.jsonl").write_text(
        json.dumps({"ts": "2026-07-01T10:00:00Z", "reply": "msg_old", "err": "timeout"}) + "\n", encoding="utf-8"
    )
    claude_root = root / "claude"
    claude_root.mkdir()
    command = f"python {catalogue.HOOK_SCRIPT}"
    settings = {
        "hooks": {
            event: [{"hooks": [{"type": "command", "command": command, "timeout": 10}]}]
            for event in ("SessionStart", "Stop")
        }
    }
    (claude_root / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    corpus = load_corpus([alpha, beta])
    assert haiku_tags.apply(corpus, config_dir) == 4
    config = Config(tz="UTC", capture=CaptureConfig(level="standard", coaching=["coaching_line"]))
    return World(corpus, load_pricing(), config, config_dir, claude_root, root)


@pytest.fixture()
def world(tmp_path):
    return _make_world(tmp_path)


def _build(w: World, **kw) -> dict:
    kw.setdefault("days", DAYS)
    kw.setdefault("today", TODAY)
    return tuning.build(
        kw.pop("corpus", w.corpus),
        kw.pop("config", w.config),
        w.pricing,
        config_dir=kw.pop("config_dir", w.config_dir),
        claude_root=kw.pop("claude_root", w.claude_root),
        **kw,
    )


@pytest.fixture()
def doc(world):
    return _build(world)


def _habits(w: World) -> habits.Habits:
    return habits.collect(w.corpus, w.pricing, tz=w.config.tz, window=f"last {DAYS} days", since="")


def test_the_built_document_is_valid_and_has_the_header_and_every_block(doc):
    assert tuning.validate(doc) == []
    assert list(doc) == [
        *tuning._HEADER,
        *("capture", "prompting", "tags", "pieces", "agents", "overhead", "cost_centres", "project_files", "compactions"),
    ]
    assert doc["kind"] == "claudeglass-tuning" and doc["format"] == 1
    assert doc["tool_version"] == re.match(r"\d+(?:\.\d+)+", __version__).group(0)
    assert doc["parser_version"] == int(PARSER_VERSION)
    assert doc["generated_on"] == "2026-09-19" and doc["window_days"] == 30
    assert tuning.loads(tuning.dumps(doc)) == doc
    assert 2_000 < len(tuning.dumps(doc).encode("utf-8")) < 40_000


def test_the_built_document_holds_nothing_that_names_anything(doc, world):
    assert_privacy_deep(doc)
    text = tuning.dumps(doc)
    for name in (*PRIVATE, str(world.root), world.root.name, "Users", "@", "://"):
        assert name not in text, name
    # Every word of every key and every string is the spec's own.
    assert tuning.validate(json.loads(text)) == []


def test_the_summary_of_the_built_document_keeps_to_the_copy_rules(doc):
    text = tuning.summary_text(doc)
    for line in text.splitlines():
        _plain(line)
    lines = text.splitlines()
    assert "Pieces of work: 6." in lines
    segmented = doc["pieces"]["segmented"]
    assert (
        f"Of the {segmented} delivered piece{'' if segmented == 1 else 's'} with a clear start, "
        "1 needed changes after delivery."
    ) in lines


def test_the_built_document_is_the_same_every_time_and_for_any_order_of_sessions(world, doc):
    assert _build(world) == doc
    assert tuning.dumps(_build(world)) == tuning.dumps(doc)
    shuffled = copy.copy(world.corpus)
    shuffled.sessions = list(reversed(world.corpus.sessions))
    assert _build(world, corpus=shuffled) == doc


def test_pieces_are_counted_by_the_dashboards_own_aggregator(doc, world):
    h = _habits(world)
    r = rework.collect(h)
    pieces = doc["pieces"]
    assert pieces["total"] == len(h.work_pieces) == 6
    assert (pieces["delivered"], pieces["segmented"], pieces["reworked"]) == (r.delivered, r.rate.segmented, r.rate.reworked)
    assert (pieces["reworked"], pieces["rework_cycles"]) == (1, r.cycles)
    assert pieces["rework_usd"] == pytest.approx(r.rate.rework_cost + r.unsegmented.rework_cost, abs=1e-6)
    assert pieces["rework_usd"] > 0
    assert pieces["rework_by_cause"] == {
        row.cause: {"cycles": row.cycles, "cost_usd": pytest.approx(row.cost, abs=1e-6)} for row in r.causes
    }
    assert pieces["rework_by_cause"].keys() == {"missed", "not_reported"}
    assert pieces["rework_by_level"] == {
        word: {"pieces": rate.pieces, "reworked": rate.reworked, "requests": rate.substantive, "rework_cycles": rate.rework}
        for word, rate in r.levels.items()
    }
    assert set(pieces["rework_by_level"]) == {"easy", "normal", "hard", "unknown"}
    # The dashboard's local Monday, 2026-09-14, is ISO week 38.
    assert pieces["rework_by_week"] == {
        "2026-W38": {"pieces": 2, "reworked": 1}
    } == {tuning._iso_week(row.week): {"pieces": row.pieces, "reworked": row.reworked} for row in r.weeks if row.pieces}
    assert sum(pieces["rounds"].values()) == len(
        [p for p in h.work_pieces if p.delivered and not p.unsegmented and p.substantive >= 1]
    )


def test_the_modes_nights_drips_and_breaks_come_out_as_the_world_was_made(doc):
    pieces = doc["pieces"]
    assert pieces["modes"] == {"overnight": 1, "long-agentic": 1, "interactive": 2, "one-shot": 1}
    assert pieces["unattended_nights"] == {"to_4h": 1}
    assert pieces["drip_runs"] == {"by_length": {"three": 1}, "by_gap": {"to_5m": 1}}
    # The message typed 937 seconds after a 120,000-token reply, and the reply a task woke an hour later.
    assert pieces["cold_returns"] == {"count": 1, "cache_write_tokens": 50_000}
    assert pieces["wake_ups"] == {"count": 1, "cache_write_tokens": 30_000}


def _breaks(tmp_path, lines) -> tuple[dict, dict]:
    project = tmp_path / "mini"
    project.mkdir(exist_ok=True)
    write_jsonl(project / "m1.jsonl", lines)
    built = tuning.build(
        load_corpus([project]), Config(tz="UTC"), None, config_dir=None, days=DAYS, today=TODAY, claude_root=tmp_path
    )
    return built["pieces"]["cold_returns"], built["pieces"]["wake_ups"]


def _big_reply(**kw) -> dict:
    return _say(10, "Done.", cache_read_input_tokens=kw.pop("context", 120_000), **kw)


@pytest.mark.parametrize(
    "gap, kw, count",
    [
        (938, {}, 1),
        (301, {}, 1),
        (300, {}, 0),
        (200, {}, 0),
        (938, {"context": 10_000}, 0),
        # An hour's cache (the reply before wrote to it) lasts past 938 seconds, not past 4000.
        (938, {"cache_creation_input_tokens": 1000, "ephemeral_1h_input_tokens": 1000}, 0),
        (4000, {"cache_creation_input_tokens": 1000, "ephemeral_1h_input_tokens": 1000}, 1),
    ],
)
def test_a_cold_return_is_a_typed_message_after_the_cache_lapsed_on_a_big_context(tmp_path, gap, kw, count):
    cold, wake = _breaks(
        tmp_path,
        [
            _note(0),
            _human(5, "add the login form to src/app.py"),
            _big_reply(**kw),
            _human(9 + gap, "keep going"),
            _say(10 + gap, "Done.", cache_creation_input_tokens=7000),
        ],
    )
    assert cold == {"count": count, "cache_write_tokens": 7000 * count}
    assert wake == {"count": 0, "cache_write_tokens": 0}


def test_a_gap_over_a_compaction_a_usage_limit_or_no_typed_message_is_not_a_cold_return(tmp_path):
    base = [_note(0), _human(5, "add the login form to src/app.py"), _big_reply()]
    after = [_human(1000, "keep going"), _say(1001, "Done.", cache_creation_input_tokens=7000)]
    assert _breaks(tmp_path, [*base, *after])[0]["count"] == 1
    compacted = [system_line("compact_boundary", timestamp=_at(900)), *after]
    assert _breaks(tmp_path, [*base, *compacted])[0]["count"] == 0
    # A reply that follows a background task's notice, not a message of yours.
    notified = [_notice(1000), _say(1001, "Done.", cache_creation_input_tokens=7000)]
    cold, wake = _breaks(tmp_path, [*base, *notified])
    assert cold["count"] == 0 and wake["count"] == 0


@pytest.mark.parametrize("gap, count", [(3600, 1), (4000, 1), (3599, 0), (600, 0)])
def test_a_wake_up_is_a_reply_to_a_task_notice_an_hour_or_more_after_the_last_reply(tmp_path, gap, count):
    cold, wake = _breaks(
        tmp_path,
        [
            _note(0),
            _human(5, "run the long tests in the background"),
            _say(10, "Started.", cache_read_input_tokens=120_000),
            _notice(9 + gap),
            _say(10 + gap, "They passed.", cache_creation_input_tokens=9000),
        ],
    )
    assert wake == {"count": count, "cache_write_tokens": 9000 * count}
    assert cold["count"] == 0


def test_prompting_counts_the_messages_plans_and_denials_the_dashboard_counts(doc, world):
    prompting = doc["prompting"]
    sessions = tuning.prompting_mod.collect(world.corpus, world.pricing)
    assert prompting["totals"]["typed"] == sum(len(s.messages) for s in sessions) == 12
    assert prompting["totals"]["queued"] == 1 and prompting["totals"]["corrections"] == 2
    assert prompting["totals"]["adjustments"] == 1
    assert sum(week["messages"] for week in prompting["weeks"].values()) == sum(m.asks for s in sessions for m in s.messages)
    assert list(prompting["weeks"]) == ["2026-W38"]
    plans = {key: prompting["plans"][key] for key in ("approved", "rounds", "rejected_rounds", "feedback_rounds")}
    assert plans == {
        "approved": 1,
        "rounds": {"two": 1},
        "rejected_rounds": 1,
        "feedback_rounds": {"question": 1},
    }
    assert prompting["denials"] == {"plan_rejected": 1}
    habit_total = sum(sum(week["habits"].values()) for week in prompting["weeks"].values())
    assert habit_total == sum(len(s.occurrences) for s in sessions)


def test_a_week_has_rates_only_with_enough_messages(world, monkeypatch):
    week = lambda: _build(world)["prompting"]["weeks"]["2026-W38"]  # noqa: E731
    monkeypatch.setattr(tuning.prompting_mod, "TREND_MIN_MESSAGES", 1)
    assert week()["rates"]
    monkeypatch.setattr(tuning.prompting_mod, "TREND_MIN_MESSAGES", 10_000)
    assert "rates" not in week() and week()["habits"]
    monkeypatch.setattr(tuning.prompting_mod, "TREND_MIN_MESSAGES", 1)
    rates = week()["rates"]
    assert rates == {
        habit: round(100.0 * n / week()["messages"], 1) for habit, n in week()["habits"].items()
    }


def test_the_tags_are_counted_by_who_wrote_them(doc, world):
    tags = doc["tags"]
    assert tags["tagged"] == {"claude": 5, "haiku": 1, "haiku-fallback": 1}
    assert tags["words"]["task"] == {
        "claude": {"feature": 3, "bugfix": 1, "refactor": 1},
        "haiku": {"chat": 1},
        "haiku-fallback": {"research": 1},
    }
    assert tags["words"]["level"]["haiku"] == {"easy": 1} and tags["words"]["admit"] == {"claude": {"claim": 1}}
    # The count by writer adds up to the count of tags.
    for key, by_writer in tags["words"].items():
        for writer, counted in by_writer.items():
            assert sum(counted.values()) <= tags["tagged"][writer], (key, writer)
    assert tags["grounding"] == {"task:chat>research": 1}
    assert tags["settling"] == {"plan:following>made": 1}
    assert tags["shift"] == {
        "new": {"cycles": 4, "corrections": 0, "adjustments": 0, "rework": 0},
        "fix": {"cycles": 1, "corrections": 1, "adjustments": 0, "rework": 1},
    }


def test_admissions_add_up_to_the_dashboards_mistakes_and_possible_ones(doc, world):
    r = rework.collect(_habits(world))
    admissions = doc["tags"]["admissions"]
    assert admissions == {"claim": {"user": {"claude": 1}}}
    counts = [(kind, caught, n) for kind, by in admissions.items() for caught, by_writer in by.items() for n in by_writer.values()]
    assert sum(n for *_rest, n in counts) == r.admitted.mistakes == 1
    assert sum(n for _kind, caught, n in counts if caught == "user") == r.admitted.user
    assert sum(n for _kind, caught, n in counts if caught == "self") == r.admitted.itself
    assert sum(n for kind, _caught, n in counts if kind == "instruction") == r.admitted.instruction
    # The reply of session four that reads like an admission and carries no tag.
    assert doc["tags"]["unconfirmed_admissions"] == r.admitted.possible == 1


def test_feedback_answers_are_counted_by_question_and_word(doc, world):
    feedback = doc["tags"]["feedback"]
    assert (feedback["answered"], feedback["skipped"]) == (1, 0)
    assert feedback["answers"] == {"outcome": {"met": 1}, "worth": {"no": 1}}
    assert feedback["mapped_from_text"] == {} and feedback["missed_in"] == {}
    spans = [span for bundle in world.corpus.sessions for span in capture_mod.feedback_spans(capture_mod.prompt_cycles(bundle.top, list(bundle.subs)))]
    assert len(spans) == 1


def test_the_judge_counts_are_haikus_own_summary_over_the_window(doc, world):
    start = datetime(2026, 9, 20, tzinfo=timezone.utc) - timedelta(days=DAYS)
    agent_ids = haiku_tags.agent_replies(world.corpus)
    for side in ("main", "agent"):
        seen = haiku_tags.summary(world.config_dir, since=start, kind=side, agent_reply_ids=agent_ids)
        assert doc["agents"]["judge"][side] == {
            "calls": seen.calls,
            "tagged": seen.tagged,
            "cost_usd": pytest.approx(seen.usd, abs=1e-6),
            "errors": dict(seen.errors),
        }
    assert doc["agents"]["judge"]["main"]["calls"] == 3 and doc["agents"]["judge"]["main"]["errors"] == {"timeout": 1}
    assert doc["agents"]["judge"]["agent"]["calls"] == 3 and doc["agents"]["judge"]["agent"]["tagged"] == 2
    assert doc["agents"]["raced"] == 1
    # The July line is older than the window.
    short = _build(world, days=1)
    assert short["window_days"] == 1
    assert short["agents"]["judge"]["main"]["calls"] == 0 and short["pieces"] == doc["pieces"]
    assert _build(world, days=0)["window_days"] == 1


def test_agent_runs_are_split_by_how_they_answered_and_by_type_and_model(doc, world):
    agents = doc["agents"]
    # A run Haiku read a result from, by the tool its last reply called; the other two said nothing.
    assert agents["verdicts"] == {"structured": {"done": 1}, "handback": {"partial": 1}, "text": {"none": 2}}
    assert agents["runs"]["direct"]["runs"] == 3 and agents["runs"]["workflow"]["runs"] == 1
    assert agents["types"] == {"Explore": 1, "general-purpose": 1, "workflow-subagent": 1, "custom": 1}
    assert agents["models"]["haiku"]["runs"] == 1 and agents["models"]["sonnet"]["runs"] == 3
    facts = _habits(world).agents
    assert agents["runs"]["direct"]["cost_usd"] == pytest.approx(sum(f.cost for f in facts if f.direct), abs=1e-6)
    assert agents["runs"]["workflow"]["cost_usd"] == pytest.approx(sum(f.cost for f in facts if not f.direct), abs=1e-6)
    assert sum(row["cost_usd"] for row in agents["models"].values()) == pytest.approx(sum(f.cost for f in facts), abs=1e-6)


def _agents_block(tmp_path, **found) -> dict:
    ctx = tuning._Ctx(NS(sessions=[]), Config(tz="UTC"), None, tmp_path, 30, TODAY, None)
    ctx.habits = habits.Habits(**found)
    return tuning._agents(ctx)


def _probing_agent(**kw) -> habits.AgentFact:
    return habits.AgentFact("s1", "Explore", "2026-W38", 0.5, **kw)


def test_the_agents_block_counts_single_lookups_and_the_replies_to_reports(tmp_path):
    block = _agents_block(
        tmp_path,
        agents=[
            _probing_agent(calls=20, probe_calls=8, probe_shell_calls=3, probe_runs=2),
            _probing_agent(calls=10, probe_calls=1, probe_shell_calls=1),
            _probing_agent(calls=0, direct=False),
        ],
        report_turns=[habits.ReportFact("s1", "2026-W38", kind, 0.1) for kind in ("acted", "acknowledged", "acknowledged")],
    )
    assert block["probes"] == {"calls": 30, "single": 9, "by_shell": 4, "runs": 2}
    assert block["report_turns"] == {"acknowledged": 2, "acted": 1}
    # Counts only: the spec accepts it, and the summary reads it.
    doc = _sample()
    doc["agents"] = {**doc["agents"], "probes": block["probes"], "report_turns": block["report_turns"]}
    assert tuning.validate(doc) == []
    text = tuning.summary_text(doc)
    assert "Of 30 agent replies, 9 made one read-only call and nothing else, 4 of them by shell command" in text
    assert "They came in 2 stretches of two or more in a row." in text
    assert "Replies to an agent's report: acknowledged 2 and acted 1." in text


def test_the_agents_block_leaves_out_lookups_and_report_replies_there_were_none_of(tmp_path):
    block = _agents_block(tmp_path, agents=[_probing_agent(calls=12)])
    assert "probes" not in block and "report_turns" not in block
    doc = _sample()
    for key in ("probes", "report_turns"):
        del doc["agents"][key]
    # A file from before these were added is still valid, and its summary has no line for them.
    assert tuning.validate(doc) == []
    text = tuning.summary_text(doc)
    assert "read-only call" not in text and "agent's report" not in text


def test_the_tools_list_saving_is_left_out_when_no_agent_recorded_its_tools(doc):
    # The world's agent runs carry no tools snapshot, so there is nothing to price.
    assert "startup_diet_usd" not in doc["agents"]


def _diet_stats(*accs):
    stats = context_budget.ContextBudgetStats()
    for acc in accs:
        stats.agents[acc.agent_type] = acc
    return stats


def test_the_tools_list_saving_is_summed_by_built_in_type_with_every_other_type_as_custom():
    from test_spawn_parts import _diet_acc

    stats = _diet_stats(
        _diet_acc("general-purpose"),
        _diet_acc("workflow-subagent"),
        _diet_acc("my-private-reviewer"),
        _diet_acc("another-private-agent", spawns=10),
        # Left alone by the diet, and with no file to give a list.
        _diet_acc("Explore"),
        _diet_acc("Plan"),
        _diet_acc("fork"),
    )
    saved = tuning._startup_diet_usd(stats)
    assert set(saved) == {"general-purpose", "workflow-subagent", "custom"}
    rows = {
        row[0]: dict(zip((c.key for c in table.columns), row))
        for table in context_budget.build_startup_section(stats).tables
        if table.name == "agent_startup_diet"
        for row in table.rows
    }
    assert saved["general-purpose"] == pytest.approx(rows["general-purpose"]["saving_usd"], abs=1e-6)
    custom = rows["my-private-reviewer"]["saving_usd"] + rows["another-private-agent"]["saving_usd"]
    assert saved["custom"] == pytest.approx(custom, abs=1e-6)
    # Amounts under closed words only: no agent's name reaches it.
    assert "private" not in json.dumps(saved)
    assert tuning.validate(_put(_sample(), "agents.startup_diet_usd", saved)) == []


def test_the_tools_list_saving_follows_the_cards_own_rule_for_when_a_list_is_worth_giving():
    from test_spawn_parts import _diet_acc

    # Too few spawns, and nothing called to put on the list: no saving.
    assert tuning._startup_diet_usd(_diet_stats(_diet_acc("reviewer", spawns=3, uses={"Read": 3}))) == {}
    assert tuning._startup_diet_usd(_diet_stats(_diet_acc("reviewer", uses={}))) == {}
    assert tuning._startup_diet_usd(_diet_stats()) == {}


def test_overhead_counts_sessions_hooks_costs_and_limit_stops(doc, world):
    overhead = doc["overhead"]
    assert overhead["entrypoints"] == {"cli": 3, "claude-desktop": 1, "unknown": 1}
    assert set(overhead["hooks"]) == {"SessionStart", "Stop"}
    assert overhead["hooks"]["SessionStart"] == {"runs": 5, "recorded": 1, "median_ms": 140.0}
    assert overhead["hooks"]["Stop"] == {"runs": 0, "recorded": 0}
    use = capture_mod.usage(world.corpus, world.pricing, since="")
    assert overhead["capture_usd"] == pytest.approx(use.cost, abs=1e-6) and overhead["capture_usd"] > 0
    assert overhead["coaching_usd"] == 0.0
    assert overhead["limits"] == {
        "five_hour": {"episodes": 1, "kept_working": 0, "cut_off_direct": 0, "cut_off_workflow": 0, "cut_off_usd": 0.0},
        "weekly": {"episodes": 1, "kept_working": 0, "cut_off_direct": 0, "cut_off_workflow": 0, "cut_off_usd": 0.0},
    }
    specs = hook_health.installed_specs(world.claude_root)
    assert [spec.event for spec in specs] == ["SessionStart", "Stop"]


def test_hooks_are_those_settings_json_runs_and_none_without_one(world, tmp_path):
    empty = tmp_path / "empty-claude-dir"
    empty.mkdir()
    assert _build(world, claude_root=empty)["overhead"]["hooks"] == {}


def test_hook_runs_and_costs_start_when_capture_was_turned_on(world):
    later = Config(
        tz="UTC", capture=CaptureConfig(level="standard", coaching=["coaching_line"], enabled_at="2099-01-01T00:00:00+00:00")
    )
    overhead = _build(world, config=later)["overhead"]
    assert overhead["capture_usd"] == 0.0 and overhead["coaching_usd"] == 0.0
    assert overhead["hooks"] and all(row["runs"] == 0 and row["recorded"] == 0 for row in overhead["hooks"].values())


def test_hook_events_the_transcripts_cannot_count_are_left_out(world, tmp_path):
    root = tmp_path / "claude-with-notification"
    root.mkdir()
    command = f"python {catalogue.HOOK_SCRIPT}"
    settings = {
        "hooks": {
            event: [{"hooks": [{"type": "command", "command": command, "timeout": 10}]}]
            for event in ("SessionStart", "Notification")
        }
    }
    (root / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    hooks = _build(world, claude_root=root)["overhead"]["hooks"]
    assert "SessionStart" in hooks and "Notification" not in hooks


def test_capture_is_the_setup_in_force(doc, world, tmp_path):
    capture = doc["capture"]
    assert (capture["level"], capture["tagger"], capture["sample_percent"]) == ("standard", "claude", 100)
    assert capture["metrics"] == [m for m in catalogue.METRICS_BY_ID if m in world.config.capture.active_metrics()]
    assert capture["coaching"] == ["coaching_line"] and capture["hints"] == {}
    expected = ratings.coaching_thresholds(world.config.thresholds, coaching_mod.read(world.config_dir))
    assert capture["thresholds"] == {key: float(expected[key]) for key in catalogue.COACHING_THRESHOLDS}
    assert set(capture["thresholds"]) == set(catalogue.COACHING_THRESHOLDS)

    off = _build(world, config=Config(tz="UTC"))["capture"]
    assert (off["level"], off["metrics"], off["coaching"]) == ("off", [], [])
    custom = Config(tz="UTC", capture=CaptureConfig(level="custom", metrics=["task", "plan"], sample=50, tagger="haiku"))
    shown = _build(world, config=custom)["capture"]
    assert (shown["level"], shown["tagger"], shown["sample_percent"]) == ("custom", "haiku", 50)
    assert shown["metrics"] == ["task", "plan"]


def test_the_thresholds_are_the_ones_the_hook_would_read(world):
    assert _build(world)["pieces"]["cold_returns"]["count"] == 1
    (world.config_dir / "coaching.json").write_text(
        json.dumps({"thresholds": {"cold_min_tokens": 200_000, "drip_count": "many", "queued_minutes": -5}}),
        encoding="utf-8",
    )
    config = Config(tz="UTC", capture=world.config.capture, thresholds={"coaching_drip_count": 4, "coaching_big_paste_tokens": 5})
    thresholds = _build(world, config=config)["capture"]["thresholds"]
    # Your file, then the config's own, and nothing that isn't a number of 0 or more.
    assert thresholds["cold_min_tokens"] == 200_000.0 and thresholds["drip_count"] == 4.0
    assert thresholds["big_paste_tokens"] == 5.0
    assert thresholds["queued_minutes"] == catalogue.COACHING_THRESHOLDS["queued_minutes"]
    # And a coaching threshold changes what counts as a cold return: 120,000 tokens no longer is one.
    assert _build(world, config=config)["pieces"]["cold_returns"]["count"] == 0


def test_hints_count_the_notes_tips_misfires_and_answers_of_each_tip(world, monkeypatch):
    collect = tuning.prompting_mod.collect

    def with_tips(corpus, pricing, ratings=None):
        sessions = collect(corpus, pricing, ratings)
        sessions[0].notes.update({"plan_fresh": 3, "drip_feed": 1, "not_a_hint": 9})
        sessions[0].tips.update({"plan_fresh": 2})
        sessions[1].misfires.update({"plan_fresh": 1})
        sessions[1].tip_answers.update({("plan_fresh", "useful"): 1, ("plan_fresh", "wrong"): 1, ("drip_feed", "known"): 2})
        return sessions

    monkeypatch.setattr(tuning.prompting_mod, "collect", with_tips)
    built = _build(world)
    assert built["capture"]["hints"] == {
        "plan_fresh": {"notes": 3, "tips": 2, "misfires": 1, "useful": 1, "known": 0, "wrong": 1},
        "drip_feed": {"notes": 1, "tips": 0, "misfires": 0, "useful": 0, "known": 2, "wrong": 0},
    }
    assert tuning.validate(built) == []
    text = tuning.summary_text(built)
    assert "Tips: 4 notes asked for one, 2 shown, 1 called a misfire." in text
    assert "You rated 4 tips: 1 useful, 2 known and 1 wrong." in text


def test_dashboard_ratings_count_as_they_do_on_the_dashboard(world):
    rated = {
        bundle.session_id: {"outcome": "partly", "why": ["missed"], "missed_in": "plan", "tip": "wrong", "tip_hint": "drip_feed"}
        for bundle in world.corpus.sessions
    }
    built = _build(world, ratings=rated)
    dashboard = rework.collect(
        habits.collect(world.corpus, world.pricing, ratings=rated, tz=world.config.tz, window=f"last {DAYS} days", since="")
    )
    assert built["tags"]["feedback"]["missed_in"] == dict(dashboard.missed_in) == {"plan": 3}
    assert built["pieces"]["reworked"] == dashboard.rate.reworked
    assert built["capture"]["hints"]["drip_feed"]["wrong"] == 5
    assert tuning.validate(built) == []
    assert _build(world)["tags"]["feedback"]["missed_in"] == {}


def test_tip_card_answers_count_for_their_hint(world):
    cards = {
        ("tip", "plan_fresh"): {"answer": "trying", "set_at": "2026-09-18T12:00:00Z"},
        ("habit", "drip_feed"): {"answer": "wrong", "set_at": "2026-09-18T12:00:00Z"},
    }
    hints = _build(world, tip_feedback=cards)["capture"]["hints"]
    assert hints == {"plan_fresh": {"notes": 0, "tips": 0, "misfires": 0, "useful": 1, "known": 0, "wrong": 0}}


def test_the_window_starts_at_local_midnight_in_the_config_zone():
    tokyo = timezone(timedelta(hours=9))
    ctx = tuning._Ctx(NS(sessions=[]), Config(tz=tokyo), None, None, 30, TODAY, None)
    assert ctx.start == datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc)
    utc = tuning._Ctx(NS(sessions=[]), Config(tz="UTC"), None, None, 30, TODAY, None)
    assert utc.start == datetime(2026, 8, 21, tzinfo=timezone.utc)


def test_the_startup_stats_use_the_corpus_calibration_as_the_report_does():
    ctx = tuning._Ctx(NS(sessions=[]), Config(tz="UTC"), None, None, 30, TODAY, None)
    assert ctx.startup.calibration is ctx.calibration


def test_an_empty_corpus_builds_a_valid_document_with_a_summary(tmp_path):
    built = tuning.build(
        NS(sessions=[]), Config(tz="UTC"), load_pricing(), config_dir=tmp_path / "nowhere", days=7, today=TODAY,
        claude_root=tmp_path,
    )
    assert tuning.validate(built) == []
    assert built["pieces"]["total"] == 0 and built["prompting"]["weeks"] == {} and built["tags"]["tagged"] == {}
    assert built["window_days"] == 7
    text = tuning.summary_text(built)
    assert "Pieces of work: 0." in text.splitlines()
    # No pricing is no cost, never a crash.
    bare = tuning.build(NS(sessions=[]), Config(tz="UTC"), None, config_dir=None, days=7, today=TODAY, claude_root=tmp_path)
    assert bare["overhead"]["capture_usd"] == 0.0


def test_a_session_with_a_bad_clock_adds_no_week_the_spec_has_no_room_for(tmp_path):
    project = tmp_path / "old"
    project.mkdir()
    write_jsonl(
        project / "m1.jsonl",
        [
            user_str_line("add a thing to src/a.py", origin={"kind": "human"}, timestamp="1970-01-05T10:00:00.000Z"),
            turn_line(timestamp="1970-01-05T10:00:05.000Z"),
        ],
    )
    built = tuning.build(
        load_corpus([project]), Config(tz="UTC"), None, config_dir=None, days=DAYS, today=TODAY, claude_root=tmp_path
    )
    assert tuning.validate(built) == [] and built["prompting"]["weeks"] == {}


def test_a_long_window_keeps_the_latest_weeks_the_spec_has_room_for(world, monkeypatch):
    # Every message in its own week, and room for five: the oldest seven go.
    seen: dict = {}

    def spread(at, tz):
        n = seen.setdefault(at, len(seen))
        return (date(2026, 1, 5) + timedelta(weeks=n)).isoformat()

    monkeypatch.setattr(tuning.habits_mod, "_week", spread)
    monkeypatch.setattr(tuning, "_MAX_WEEKS", 5)
    built = _build(world)
    weeks = list(built["prompting"]["weeks"])
    assert len(seen) > 5 and len(weeks) == 5 and weeks == sorted(weeks)
    assert tuning._iso_week("2026-01-05") not in weeks
    assert len(built["pieces"]["rework_by_week"]) <= 5
    assert tuning.validate(built) == []


def test_the_week_maps_hold_a_hundred_and_sixty_weeks_and_no_more():
    room = [f"{2020 + i // 52}-W{i % 52 + 1:02d}" for i in range(tuning._MAX_WEEKS)]
    row = {"messages": 1, "habits": {}}
    assert tuning.validate(_put(_sample(), "prompting.weeks", {week: row for week in room})) == []
    over = {week: row for week in [*room, "2029-W01"]}
    assert tuning.validate(_put(_sample(), "prompting.weeks", over)) == ["prompting.weeks: has more entries than the spec allows"]


def test_the_summary_keeps_signed_out_judge_calls_apart_from_failures():
    doc = _sample()
    doc["agents"]["judge"]["agent"]["errors"] = {"no_login": 74, "failed": 3}
    lines = tuning.summary_text(doc).splitlines()
    assert "Haiku judged 15 replies and 12 agent runs for $0.04, and 4 failed." in lines
    assert "74 calls found the claude command signed out: sign in to the claude command in a terminal." in lines


# -- cost_centres (Phase 8a) ----------------------------------------------------------------------


def test_the_cost_centres_block_is_the_matrix_in_list_price_amounts_and_nothing_else(doc, world):
    from claudeglass import cost_centres
    from claudeglass.recache import RecacheThresholds

    block = doc["cost_centres"]
    assert block, "the world has spend, so it has a matrix"
    assert set(block) <= set(cost_centres.CENTRES)
    for centre, cells in block.items():
        assert cells and set(cells) <= {f"{cell}_usd" for cell in cost_centres.CELLS}
    sessions = [(b.top, b.subs) for b in world.corpus.sessions if b.top is not None]
    found = cost_centres.compute(sessions, world.pricing, RecacheThresholds.from_config(world.config.thresholds))
    # The rows add up to the whole window's spend, to the six places an amount keeps.
    assert sum(sum(cells.values()) for cells in block.values()) == pytest.approx(found.total(), abs=1e-4)
    assert found.total() > 0


def test_the_cost_centres_block_rejects_a_name_a_missing_amount_is_fine_and_old_documents_pass():
    doc = _sample()
    assert tuning.validate(doc) == []
    older = _sample()
    del older["cost_centres"]
    assert tuning.validate(older) == []
    for path, value, check in (
        ("cost_centres", {"my-project": {"base_read_usd": 1.0}}, "cost_centres: has a key that is not in the spec"),
        ("cost_centres.main", {"base_read": 1.0}, "cost_centres.main: has a key that is not in the spec"),
        ("cost_centres.main", {"base_read_usd": -1.0}, "cost_centres.main.base_read_usd: must be 0 or more"),
        ("cost_centres.main", {"base_read_usd": "9"}, "cost_centres.main.base_read_usd: must be a number"),
        ("cost_centres", [], "cost_centres: must be an object"),
    ):
        assert any(check in problem for problem in tuning.validate(_put(_sample(), path, value))), path
    assert tuning.validate(_put(_sample(), "cost_centres.main", {})) == []


def test_the_summary_names_the_cost_centres_by_their_spend():
    lines = tuning.summary_text(_sample()).splitlines()
    assert (
        "Spend by cost centre, at list prices: main session $34.50, direct agents $7.00, "
        "workflow agents $1.75 and session start (1-hour write) $1.50." in lines
    )
    empty = _put(_sample(), "cost_centres", {})
    assert not any(line.startswith("Spend by cost centre") for line in tuning.summary_text(empty).splitlines())


# -- project_files (Phase 8a) ---------------------------------------------------------------------


def _file_row(file_hash: str, tokens: int, reach: dict, weekly: dict | None = None, cost: float = 1.0) -> dict:
    """One ``reads`` row of ``context_files.to_dict`` (a file agents read)."""
    return {
        "hash": file_hash,
        "source": "read",
        "tokens": tokens,
        "reach": reach,
        "reads": dict(reach),
        "read_tokens": {name: tokens * runs for name, runs in reach.items()},
        "cost_usd": cost,
        "cost_by_reach": {},
        "weekly": weekly or {"2026-09-14": tokens},
        "last_seen": "2026-09-14T10:00:00+00:00",
    }


def _files_data(rows: list[dict], files: list[dict] | None = None, **transcripts) -> dict:
    return {
        "transcripts": transcripts or {"main": 10, "Explore": 10, "Plan": 10, "my-secret-agent": 10},
        "files": files or [],
        "reads": rows,
        "standing": {},
        "window_days": 30,
        "newest": "2026-09-14",
    }


def test_the_project_files_block_holds_closed_words_and_shares_and_no_name(tmp_path):
    from claudeglass import claude_md_review

    claude_root = tmp_path / ".claude"
    config_dir = claude_root / "claudeglass"
    config_dir.mkdir(parents=True)
    repo = tmp_path / "work" / "secret-repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "src").mkdir()
    (repo / "docs" / "context-notes.md").write_text("notes\n", encoding="utf-8")
    (repo / "src" / "engine.py").write_text("x = 1\n", encoding="utf-8")
    (claude_root / "projects" / "repo").mkdir(parents=True)
    (claude_root / "projects" / "repo" / "s.jsonl").write_text(json.dumps({"cwd": str(repo)}) + "\n", encoding="utf-8")
    salt = parse.load_or_create_salt(config_dir)
    md = parse.path_hash(str(repo / "docs" / "context-notes.md"), salt)
    py = parse.path_hash(str(repo / "src" / "engine.py"), salt)
    gone = parse.path_hash(str(repo / "deleted.md"), salt)
    data = _files_data(
        [
            _file_row(
                md,
                9000,
                {"main": 4, "Explore": 6, "Plan": 5, "my-secret-agent": 10},
                {"2026-08-17": 4000, "2026-08-24": 6000, "2026-09-07": 9000},
                cost=3.0,
            ),
            _file_row(py, 25000, {"main": 3}, cost=2.0),
            _file_row(gone, 800, {"Explore": 5}, cost=1.0),
        ]
    )
    block = tuning._project_files(NS(context_files=data, config_dir=config_dir))
    assert block["total"] == 3
    by_size = {item["size"]: item for item in block["files"]}
    notes = by_size["to_10k"]
    assert (notes["ext"], notes["source"]) == ("md", "read")
    # A custom agent's runs count as "custom"; shares are of that reach's runs.
    assert notes["reach"] == {"main": 0.4, "Explore": 0.6, "Plan": 0.5, "custom": 1.0}
    assert notes["weekly"] == {
        "2026-W34": "to_5k",
        "2026-W35": "to_10k",
        "2026-W36": "to_10k",
        "2026-W37": "to_10k",
        "2026-W38": "to_10k",
    }
    assert (by_size["over_20k"]["ext"], by_size["over_20k"]["reach"]) == ("code", {"main": 0.3})
    # A file this machine cannot find has no extension class to give.
    assert (by_size["to_1k"]["ext"], by_size["to_1k"]["source"]) == ("unknown", "read")
    doc = {**_sample(), "project_files": block}
    assert tuning.validate(doc) == []
    assert_privacy_deep(doc)
    text = tuning.dumps(doc)
    for private in (md, py, gone, "context-notes", "engine", "secret-repo", "my-secret-agent", "deleted", str(tmp_path)):
        assert private not in text, private
    assert claude_md_review.EXT_CLASSES  # the vocabulary the block draws from


def test_the_project_files_block_keeps_the_dearest_files_and_counts_them_all():
    rows = [_file_row(f"{i:016x}", 1000 + i, {"main": 5}, cost=float(i)) for i in range(1, 61)]
    block = tuning._project_files(NS(context_files=_files_data(rows), config_dir=None))
    assert block["total"] == 60
    assert len(block["files"]) == tuning.FILES_KEPT
    assert tuning.validate({**_sample(), "project_files": block}) == []
    # Loaded files are markdown; a file nothing can name says so.
    auto = {
        "hash": "a" * 16,
        "type": "Project",
        "scoped": False,
        "tokens": 1500,
        "reach": {"main": 8},
        "cost_usd": 1.0,
        "last_seen": "2026-09-14T10:00:00+00:00",
        "weekly": {"2026-09-14": 1500},
    }
    block = tuning._project_files(NS(context_files=_files_data([], [auto]), config_dir=None))
    assert block["files"] == [
        {"ext": "md", "source": "auto", "size": "to_2k", "weekly": {"2026-W38": "to_2k"}, "reach": {"main": 0.8}}
    ]


def test_the_project_files_block_is_empty_without_data_or_a_rate_card(world):
    assert tuning._project_files(NS(context_files={}, config_dir=None)) == {"total": 0, "files": []}
    built = _build(world, corpus=NS(sessions=[]))
    assert built["project_files"] == {"total": 0, "files": []}
    assert not any(line.startswith("Project files") for line in tuning.summary_text(built).splitlines())


def test_the_project_files_block_rejects_a_name_a_bad_word_and_too_many_files():
    doc = _sample()
    assert tuning.validate(doc) == []
    older = _sample()
    del older["project_files"]
    assert tuning.validate(older) == []
    one = "project_files.files.0"
    at = "project_files.files[0]"
    for path, value, check in (
        (one, {**doc["project_files"]["files"][0], "name": "docs/context.md"}, f"{at}: has a key that is not in the spec"),
        (f"{one}.ext", "context.md", f"{at}.ext: must be one of the words"),
        (f"{one}.source", "memory", f"{at}.source: must be one of the words"),
        (f"{one}.size", "9000", f"{at}.size: must be one of the words"),
        (f"{one}.weekly", {"last-week": "to_5k"}, f"{at}.weekly: has a key that is not in the spec"),
        (f"{one}.weekly", {"2026-W31": "huge"}, f"{at}.weekly.2026-W31: must be one of the words"),
        (f"{one}.reach", {"my-agent": 0.5}, f"{at}.reach: has a key that is not in the spec"),
        (f"{one}.reach", {"main": -0.1}, f"{at}.reach.main: must be 0 or more"),
        ("project_files.files", doc["project_files"]["files"] * 20, "project_files.files: has more items than"),
        ("project_files", [], "project_files: must be an object"),
    ):
        assert any(check in problem for problem in tuning.validate(_put(copy.deepcopy(doc), path, value))), path


def test_the_summary_counts_the_project_files_by_size_and_reach():
    lines = tuning.summary_text(_sample()).splitlines()
    assert "Project files agents take in: 7 files, 2 of the biggest read by habit." in lines
    assert "Of those, 1 over 10,000 tokens and 1 used by 3 or more agent types." in lines
    quiet = _sample()
    quiet["project_files"]["files"] = [quiet["project_files"]["files"][1]]
    lines = tuning.summary_text(quiet).splitlines()
    assert "Project files agents take in: 7 files, 0 of the biggest read by habit." in lines
    assert not any(line.startswith("Of those") for line in lines)


# -- Phase 8: how the runs started, who chose the model, what summaries cost, how builds began ------------


def _launching_agent(launch: str, run: str, **kw) -> habits.AgentFact:
    return habits.AgentFact("s1", kw.pop("agent_type", "Explore"), "2026-W38", kw.pop("cost", 0.5), launch=launch, run=run, **kw)


def test_the_agents_block_counts_the_runs_by_how_they_started(tmp_path):
    block = _agents_block(
        tmp_path,
        agents=[
            _launching_agent("background", "r1", calls=20, probe_calls=8, start_tokens=1000, compactions=2),
            _launching_agent("background", "r2", calls=10, shared_reads=1, shared_tokens=300, shared_cost=0.25, cost=1.0),
            # A workflow's agents share the run the script started, and an agent with no reply counts nowhere.
            _launching_agent("workflow", "w1", calls=5, start_tokens=100, direct=False),
            _launching_agent("workflow", "w1", calls=7, start_tokens=100, direct=False, compactions=1, cost=0.25),
            _launching_agent("foreground", "f1", calls=0),
        ],
    )
    assert block["launches"] == {
        "background": {
            "runs": 2, "agents": 2, "replies": 30, "single": 8, "start_reads": 20_000, "compactions": 2, "compacted": 1,
            "shared_reads": 1, "shared_usd": 0.25, "cost_usd": 1.5,
        },
        "workflow": {
            "runs": 1, "agents": 2, "replies": 12, "single": 0, "start_reads": 1_200, "compactions": 1, "compacted": 1,
            "shared_reads": 0, "shared_usd": 0.0, "cost_usd": 0.75,
        },
    }  # fmt: skip
    # Words and counts of the spec's own, in the order the dashboard lists them.
    assert list(block["launches"]) == ["background", "workflow"]
    doc = _put(_sample(), "agents.launches", block["launches"])
    assert tuning.validate(doc) == []


def test_the_launches_are_the_run_receipts_tables_own_groups(tmp_path):
    found = [
        _launching_agent("background", "r1", calls=20, probe_calls=8, start_tokens=1000, compactions=2, cost=0.4),
        _launching_agent("foreground", "f1", calls=3, start_tokens=500, shared_reads=2, shared_tokens=90, shared_cost=0.1),
        _launching_agent("workflow", "w1", calls=5, start_tokens=100, direct=False),
        _launching_agent("workflow", "w1", calls=7, start_tokens=100, direct=False, compactions=1),
    ]
    block = _agents_block(tmp_path, agents=found)["launches"]
    table = habits._run_receipts_table(habits.Habits(agents=found))
    keys = [column.key for column in table.columns]
    assert [row[0] for row in table.rows] == list(block)
    for row in table.rows:
        cells = dict(zip(keys, row))
        mine = block[cells["launch"]]
        assert mine["runs"] == cells["runs"] and mine["agents"] == cells["agents"] and mine["replies"] == cells["calls"]
        assert mine["start_reads"] == cells["start_reads"] and mine["compactions"] == cells["compactions"]
        assert mine["compacted"] == cells["compacted_agents"] and mine["shared_reads"] == cells["shared_reads"]
        assert mine["cost_usd"] == pytest.approx(cells["cost"], abs=1e-6)


def test_the_agents_block_has_no_launches_or_model_choice_when_there_were_no_replies_or_no_rate_card(tmp_path):
    block = _agents_block(tmp_path, agents=[_launching_agent("background", "r1", calls=0)])
    assert "launches" not in block and "model_choice" not in block
    assert tuning.validate(_put(_sample(), "agents", {**_sample()["agents"], **block})) == []


def _mini_world(root: Path, file_model: str | None = "opus") -> World:
    """One main session that summarised three times (an automatic summary, a
    manual one and one whose trigger the export has no word for), and under it
    four agent runs on the newer meta shape: a custom type whose agent file
    names ``file_model``, a built-in one that named Opus in its call, one that
    named nothing and a workflow's agent. The Explore run summarised once."""
    alpha = root / "projects" / "proj-mini"
    alpha.mkdir(parents=True)
    config_dir = root / "claudeglass"
    config_dir.mkdir()
    claude_root = root / "claude"
    claude_root.mkdir()
    if file_model is not None:
        (config_dir / "snapshots").mkdir()
        snapshot = {"ts": "2026-09-18T08:00:00Z", "agents": {"my-private-reviewer": {"model": file_model}}}
        (config_dir / "snapshots" / "2026-09-18T08-00-00.json").write_text(json.dumps(snapshot), encoding="utf-8")

    def boundary(second: int, trigger: str, dropped: int) -> dict:
        meta = {"trigger": trigger, "preTokens": 100_000, "postTokens": 20_000, "cumulativeDroppedTokens": dropped}
        return system_line("compact_boundary", timestamp=_at(second), compactMetadata=meta)

    def reply(second: int, message_id: str, **kw) -> dict:
        return _say(second, "Working.", message_id=message_id, **kw)

    main = [
        _human(0, "start the work"),
        reply(5, "m1", input_tokens=1_000, output_tokens=200),
        boundary(10, "auto", 80_000),
        reply(20, "m2", cache_creation_input_tokens=25_000, ephemeral_1h_input_tokens=25_000),
        boundary(30, "manual", 160_000),
        reply(40, "m3", cache_creation_input_tokens=25_000, ephemeral_1h_input_tokens=25_000),
        boundary(50, "mystery-trigger", 240_000),
        reply(60, "m4", cache_creation_input_tokens=25_000, ephemeral_1h_input_tokens=25_000),
    ]
    write_jsonl(alpha / "s1.jsonl", main)
    folder = alpha / "s1" / "subagents"
    opus = "claude-opus-4-1"
    for name, meta, model in (
        ("agent-fi", {"agentType": "my-private-reviewer", "description": "d"}, opus),
        ("agent-ca", {"agentType": "general-purpose", "description": "d", "model": "opus"}, opus),
        ("agent-in", {"agentType": "Explore", "description": "d"}, opus),
    ):
        lines = [_agent_turn(70, f"{name}_1", {"type": "text", "text": "x"}, model=model, input_tokens=2_000, output_tokens=3_000)]
        if name == "agent-in":
            lines.append(boundary(75, "auto", 50_000))
            lines.append(_agent_turn(80, f"{name}_2", {"type": "text", "text": "y"}, model=model))
        _write_agent(folder, name, meta, lines)
    _write_agent(
        folder / "workflows" / "wf_1",
        "agent-w1",
        {"agentType": "workflow-subagent", "description": "d"},
        [_agent_turn(70, "agent-w1_1", {"type": "text", "text": "z"})],
    )
    corpus = load_corpus([alpha])
    return World(corpus, load_pricing(), Config(tz="UTC"), config_dir, claude_root, root)


def _ctx(w: World, pricing=True) -> tuning._Ctx:
    return tuning._Ctx(w.corpus, w.config, w.pricing if pricing else None, w.config_dir, DAYS, TODAY, w.claude_root)


def test_the_model_choice_rows_say_who_chose_each_model_in_words_and_amounts(tmp_path):
    w = _mini_world(tmp_path)
    rows = tuning._model_choice(_ctx(w))
    by_key = {(r["started_by"], r["agent_type"], r["model"], r["chosen"]): r for r in rows}
    assert set(by_key) == {
        ("direct", "custom", "opus", "file"),
        ("direct", "general-purpose", "opus", "call"),
        ("direct", "Explore", "opus", "inherited"),
        ("workflow", "workflow-subagent", "sonnet", "inherited"),
    }
    assert all(r["runs"] == 1 for r in rows)
    # An Opus run has a Sonnet ceiling, a Sonnet run none; the rows come dearest first.
    assert "ceiling_usd" not in by_key[("workflow", "workflow-subagent", "sonnet", "inherited")]
    assert all(r["ceiling_usd"] > 0 for r in rows if r["model"] == "opus")
    assert [r["cost_usd"] for r in rows] == sorted((r["cost_usd"] for r in rows), reverse=True)
    # The same rows the dashboard's table has, to the six places an amount keeps.
    from claudeglass import cost_centres

    sessions = [(b.top, b.subs) for b in w.corpus.sessions if b.top is not None]
    found = cost_centres.model_choice(sessions, w.pricing, {"my-private-reviewer": "opus"})
    assert sum(r["cost_usd"] for r in rows) == pytest.approx(sum(row.cost for row in found.values()), abs=1e-4)
    assert sum(r["runs"] for r in rows) == sum(row.runs for row in found.values())
    doc = _put(_sample(), "agents.model_choice", rows)
    assert tuning.validate(doc) == []
    assert "my-private-reviewer" not in json.dumps(rows)


@pytest.mark.parametrize(
    "file_model, chosen",
    [("opus", "file"), ("inherit", "file"), ("sonnet", "inherited"), (None, "inherited")],
)
def test_a_choice_is_the_agent_files_only_when_a_settings_snapshot_says_it_names_the_model(tmp_path, file_model, chosen):
    w = _mini_world(tmp_path, file_model=file_model)
    rows = tuning._model_choice(_ctx(w))
    [custom] = [r for r in rows if r["agent_type"] == "custom"]
    assert custom["chosen"] == chosen


def test_there_is_no_model_choice_without_a_rate_card(tmp_path):
    w = _mini_world(tmp_path)
    assert tuning._model_choice(_ctx(w, pricing=False)) == []


def test_the_model_choice_keeps_the_dearest_rows_and_adds_up_the_rest_on_the_same_words(tmp_path, monkeypatch):
    from claudeglass import cost_centres

    rows = {
        ("direct", "my-first-agent", "opus", "call"): cost_centres.ModelRow(runs=2, cost=3.0, cost_on_sonnet=1.0),
        ("direct", "my-second-agent", "opus", "call"): cost_centres.ModelRow(runs=1, cost=1.0, cost_on_sonnet=0.5),
        ("direct", "Plan", "haiku", "not recorded"): cost_centres.ModelRow(runs=4, cost=0.5),
        ("workflow", "workflow-subagent", "unseen-tier", "call"): cost_centres.ModelRow(runs=1, cost=0.25),
    }
    monkeypatch.setattr(cost_centres, "model_choice", lambda *a, **k: rows)
    ctx = tuning._Ctx(NS(sessions=[]), Config(tz="UTC"), object(), tmp_path, 30, TODAY, None)
    ctx.agent_files = {}
    block = tuning._model_choice(ctx)
    # Two custom agents on one tier are one row; a tier the table has no word for is unknown.
    assert block[0] == {
        "started_by": "direct", "agent_type": "custom", "model": "opus", "chosen": "call", "runs": 3,
        "cost_usd": 4.0, "ceiling_usd": 2.5,
    }  # fmt: skip
    assert {r["model"] for r in block} == {"opus", "haiku", "unknown"}
    assert [r["chosen"] for r in block if r["agent_type"] == "Plan"] == ["not_recorded"]
    monkeypatch.setattr(tuning, "MODEL_ROWS_KEPT", 2)
    assert len(tuning._model_choice(ctx)) == 2


def test_the_compactions_block_counts_the_summaries_and_prices_them_at_list_prices(tmp_path):
    from claudeglass.pricing import price_turn

    w = _mini_world(tmp_path)
    block = tuning._compactions(_ctx(w))
    assert block["sessions"] == 1 and block["compacted"] == 1 and block["summaries"] == 3
    # The trigger word of a summary is a closed word or ``other``; the agents' own are counted apart.
    assert block["triggers"] == {"auto": 1, "manual": 1, "other": 1}
    assert block["in_agents"] == {"subagent": 1, "workflow": 0}
    assert block["dropped_tokens"] == 80_000 * 3 + 50_000
    assert block["write_usd"] > 0
    top = w.corpus.sessions[0].top
    main = sum(price_turn(t, w.pricing.resolve_model(t.model)).total for t in top.turns if t.turn_index > 0)
    # The one session summarised three times, so it is the heavy one and all of the main-session cost.
    assert block["heavy"] == {"sessions": 1, "cost_usd": pytest.approx(main, abs=1e-6), "main_usd": pytest.approx(main, abs=1e-6)}
    doc = _put(_sample(), "compactions", block)
    assert tuning.validate(doc) == []
    text = tuning.summary_text(doc)
    assert "Summaries: 3 summaries in 1 session of 1 (auto 1, manual 1 and other 1)." in text
    assert "1 session summarised 3 times or more:" in text and ", 100% of the total." in text


def test_the_compactions_block_is_counts_alone_without_a_rate_card(tmp_path):
    w = _mini_world(tmp_path)
    block = tuning._compactions(_ctx(w, pricing=False))
    assert block["summaries"] == 3 and block["compacted"] == 1
    assert not {"write_usd", "summary_usd", "heavy"} & set(block)
    assert tuning.validate(_put(_sample(), "compactions", block)) == []


def test_the_built_document_has_a_compactions_block_and_the_model_choice_and_nothing_that_names_anything(tmp_path):
    w = _mini_world(tmp_path)
    doc = tuning.build(w.corpus, w.config, w.pricing, config_dir=w.config_dir, days=DAYS, today=TODAY, claude_root=w.claude_root)
    assert list(doc)[-1] == "compactions" and doc["compactions"]["summaries"] == 3
    assert len(doc["agents"]["model_choice"]) == 4 and doc["agents"]["launches"]["foreground"]["agents"] == 3
    assert_privacy_deep(doc)
    text = tuning.dumps(doc)
    for name in ("my-private-reviewer", "proj-mini", "claude-opus-4-1", "mystery-trigger", "agent-fi", "s1", str(w.root)):
        assert name not in text, name
    assert tuning.validate(json.loads(text)) == []


def _prompting_block(tmp_path, **found) -> dict:
    ctx = tuning._Ctx(NS(sessions=[]), Config(tz="UTC"), None, tmp_path, 30, TODAY, None)
    ctx.prompting = []
    ctx.habits = habits.Habits(plan_rounds=found.pop("plan_rounds", []))
    ctx.approvals = found.pop("approvals", [])
    return tuning._prompting(ctx)


def _approval(start: str, **kw) -> "tuning.handoff_mod.PlanApproval":
    return tuning.handoff_mod.PlanApproval("s1", 3, kw.pop("typed", False), start, **kw)


def test_plans_count_how_each_build_began_and_the_plans_put_up_for_each_ask(tmp_path):
    block = _prompting_block(
        tmp_path,
        approvals=[
            _approval("kept", typed=True, tokens_carried=120_000, build_turns=10, build_usd=1.5, build_context=900_000),
            _approval("kept", tokens_carried=80_000, build_turns=6, build_usd=0.5, build_context=300_000),
            _approval("cleared", build_turns=8, build_usd=0.25, build_context=100_000),
        ],
        plan_rounds=[
            habits.PlanFact("s1", "2026-W38", 1, 0, True, typed=True, tokens=0, cost=0.0),
            habits.PlanFact("s1", "2026-W38", 3, 2, True, tokens=4_000, cost=0.75, asked=1),
            habits.PlanFact("s2", "2026-W38", 2, 1, False, tokens=900, cost=0.125, asked=1),
        ],
    )["plans"]
    assert block["builds"] == {
        "kept": {
            "approvals": 2, "typed": 1, "carried_tokens": 200_000, "replies": 16, "context_tokens": 1_200_000,
            "cost_usd": 2.0,
        },
        "cleared": {
            "approvals": 1, "typed": 0, "carried_tokens": 0, "replies": 8, "context_tokens": 100_000, "cost_usd": 0.25,
        },
    }  # fmt: skip
    assert block["asks"] == {
        "all": {"plans": 2, "typed": 1, "sent_back": 2, "asked": 1, "tokens": 4_000, "cost_usd": 0.75},
        "none": {"plans": 1, "typed": 1, "sent_back": 0, "asked": 0, "tokens": 0, "cost_usd": 0.0},
        "twice": {"plans": 1, "typed": 0, "sent_back": 2, "asked": 1, "tokens": 4_000, "cost_usd": 0.75},
        "dropped": {"plans": 1, "typed": 0, "sent_back": 1, "asked": 1, "tokens": 900, "cost_usd": 0.125},
    }
    # The groups are the Plans sent back table's own.
    table = habits._plan_rounds_table(habits.Habits(plan_rounds=[
        habits.PlanFact("s1", "2026-W38", 1, 0, True, typed=True),
        habits.PlanFact("s1", "2026-W38", 3, 2, True, tokens=4_000, cost=0.75, asked=1),
        habits.PlanFact("s2", "2026-W38", 2, 1, False, tokens=900, cost=0.125, asked=1),
    ]))
    assert [row[0] for row in table.rows] == list(block["asks"])
    assert [row[1] for row in table.rows] == [row["plans"] for row in block["asks"].values()]
    assert tuning.validate(_put(_sample(), "prompting.plans", {**_sample()["prompting"]["plans"], **block})) == []


def test_plans_leave_out_builds_and_asks_there_were_none_of(tmp_path):
    block = _prompting_block(tmp_path)["plans"]
    assert "builds" not in block and "asks" not in block
    assert tuning.validate(_put(_sample(), "prompting.plans", block)) == []


def test_the_built_plans_hold_the_same_approvals_the_plan_handoff_section_counts(doc, world):
    approvals = tuning.handoff_mod.compute_handoff([r for s in world.corpus.sessions for r in (s.top, *s.subs)], world.pricing).approvals
    builds = doc["prompting"]["plans"]["builds"]
    assert sum(row["approvals"] for row in builds.values()) == len(approvals) == 1
    assert builds["kept"]["replies"] == sum(a.build_turns for a in approvals)
    assert builds["kept"]["cost_usd"] == pytest.approx(sum(a.build_usd for a in approvals), abs=1e-6)
    asks = doc["prompting"]["plans"]["asks"]
    assert asks["all"]["plans"] == 1 and asks["once"]["plans"] == 1 and asks["all"]["asked"] == 1


@pytest.mark.parametrize(
    "path, value, check",
    [
        ("agents.launches", {"my-launch": {"runs": 1, "agents": 1, "replies": 1, "cost_usd": 1.0}}, "agents.launches: has a key"),
        ("agents.launches.background", {"runs": 1, "agents": 1, "replies": 1}, "agents.launches.background.cost_usd: is missing"),
        ("agents.launches.background", {"runs": 1, "agents": 1, "replies": 1, "cost_usd": 1.0, "prompt": 1}, "agents.launches.background: has a key"),
        ("agents.launches.background.cost_usd", -1.0, "agents.launches.background.cost_usd: must be 0 or more"),
        ("agents.model_choice.0.agent_type", "my-private-reviewer", "agents.model_choice[0].agent_type: must be one of the words"),
        ("agents.model_choice.0.model", "claude-opus-4-1", "agents.model_choice[0].model: must be one of the words"),
        ("agents.model_choice.0.chosen", "not recorded", "agents.model_choice[0].chosen: must be one of the words"),
        ("agents.model_choice.0.started_by", "main", "agents.model_choice[0].started_by: must be one of the words"),
        ("agents.model_choice.0.runs", "4", "agents.model_choice[0].runs: must be a whole number"),
        ("agents.model_choice", {}, "agents.model_choice: must be a list"),
        ("prompting.plans.builds", {"fresh": {"approvals": 1, "replies": 1, "cost_usd": 1.0}}, "prompting.plans.builds: has a key"),
        ("prompting.plans.builds.kept.cost_usd", "9", "prompting.plans.builds.kept.cost_usd: must be a number"),
        ("prompting.plans.asks", {"thrice": {"plans": 1, "sent_back": 1, "asked": 1, "cost_usd": 1.0}}, "prompting.plans.asks: has a key"),
        ("prompting.plans.asks.once.plans", -1, "prompting.plans.asks.once.plans: must be 0 or more"),
        ("compactions", {"sessions": 1, "compacted": 1}, "compactions.summaries: is missing"),
        ("compactions.triggers", {"mystery-trigger": 1}, "compactions.triggers: has a key"),
        ("compactions.in_agents", {"background": 1}, "compactions.in_agents: has a key"),
        ("compactions.heavy", {"sessions": 1, "cost_usd": 1.0}, "compactions.heavy.main_usd: is missing"),
        ("compactions.write_usd", -0.5, "compactions.write_usd: must be 0 or more"),
        ("compactions", [], "compactions: must be an object"),
    ],
)
def test_the_phase_8_keys_reject_a_name_a_bad_word_and_a_wrong_kind(path, value, check):
    problems = tuning.validate(_put(_sample(), path, value))
    assert any(check in problem for problem in problems), (path, problems)


def test_a_file_from_before_phase_8_still_validates_and_its_summary_has_no_line_for_it():
    older = _sample()
    for path in ("agents.launches", "agents.model_choice", "prompting.plans.builds", "prompting.plans.asks"):
        *parents, last = path.split(".")
        node = older
        for key in parents:
            node = node[key]
        del node[last]
    del older["compactions"]
    assert tuning.validate(older) == []
    text = tuning.summary_text(older)
    for phrase in ("by how they were started", "Model choice", "Builds after", "Summaries:", "summarised"):
        assert phrase not in text, phrase
    # A smaller document still: no cost centres or project files either.
    assert tuning.validate({k: v for k, v in older.items() if k not in ("cost_centres", "project_files")}) == []


def test_the_summary_reads_the_phase_8_blocks_in_plain_words():
    lines = tuning.summary_text(_sample()).splitlines()
    assert "Builds after an approved plan: kept 5, cleared 2 and handoff 1, 8 plans in all." in lines
    assert (
        "Agents by how they were started: background 12 agents at $0.96 each, foreground 9 agents at $0.33 each "
        "and workflow 9 agents at $0.69 each."
    ) in lines
    assert (
        "Model choice: 4 agent runs on Opus or above; chosen by file 10, not recorded 9 and inherited 4. "
        "Sonnet could save up to $3.10 at list prices."
    ) in lines
    assert "Summaries: 220 summaries in 40 sessions of 61 (auto 200, manual 15 and other 5)." in lines
    assert "Inside agent runs: 48 summaries." in lines
    assert "They cost $17.90 in cache written on the next reply and about $20.75 to write, at list prices." in lines
    assert "26 sessions summarised 3 times or more: $159.50 of main-session cost, 86% of the total." in lines
    for line in lines:
        _plain(line)
    # A run of summaries with nothing to count says nothing.
    quiet = _put(_sample(), "compactions", {"sessions": 5, "compacted": 0, "summaries": 0})
    assert not any(line.startswith("Summaries:") for line in tuning.summary_text(quiet).splitlines())


def test_the_fullest_phase_8_blocks_still_fit_the_size_limit():
    doc = _sample()
    doc["agents"]["model_choice"] = [
        {
            "started_by": started, "agent_type": agent_type, "model": model, "chosen": chosen, "runs": 10**6,
            "cost_usd": 10.0**6, "ceiling_usd": 10.0**6,
        }
        for started in tuning.MODEL_STARTERS
        for agent_type in tuning.AGENT_TYPES
        for model in tuning.MODEL_TIERS
        for chosen in tuning.MODEL_CHOSEN
    ][: tuning.MODEL_ROWS_KEPT]
    assert len(doc["agents"]["model_choice"]) == tuning.MODEL_ROWS_KEPT
    assert tuning.validate(doc) == []
    assert len(tuning.dumps(doc).encode("utf-8")) < tuning.MAX_BYTES
    assert "not_recorded" in tuning.MODEL_CHOSEN

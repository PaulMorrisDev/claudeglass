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
from claudeglass import habits, haiku_tags, hook_health, parse, ratings, rework, tuning
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
        {"centres": tuning.Map(["alpha", "beta"], tuning.Obj({"spend_usd": tuning.Num()}, required=("spend_usd",)))},
        required=("centres",),
    )

    @tuning._block("cost_centres", node, required=False)
    def _centres(ctx):
        return {"centres": {"alpha": {"spend_usd": 1.5}}}

    assert list(tuning.spec().fields)[-1] == "cost_centres"
    # A document made before the block was added has no such key, and is valid.
    assert tuning.validate(_sample()) == []
    doc = {**_sample(), "cost_centres": {"centres": {"beta": {"spend_usd": 2.0}}}}
    assert tuning.validate(doc) == []
    for path, value, check in (
        ("cost_centres.centres", {"gamma": {"spend_usd": 1.0}}, "cost_centres.centres: has a key"),
        ("cost_centres.centres", {"alpha": {"spend_usd": -1.0}}, "spend_usd: must be 0 or more"),
        ("cost_centres.centres", {"alpha": {"spend_usd": "9"}}, "spend_usd: must be a number"),
        ("cost_centres.centres", {"alpha": {}}, "spend_usd: is missing"),
        ("cost_centres.centres", [], "cost_centres.centres: must be an object"),
    ):
        assert any(check in problem for problem in tuning.validate(_put(copy.deepcopy(doc), path, value)))
    # The block is built beside the others and checked with them.
    built = tuning.build(NS(sessions=[]), Config(), None, config_dir=None, days=7, today=TODAY, claude_root=None)
    assert built["cost_centres"] == {"centres": {"alpha": {"spend_usd": 1.5}}}
    assert list(built)[-1] == "cost_centres"
    # A block that must be there fails a document without it.
    monkeypatch.setitem(tuning._BLOCKS, "cost_centres", (node, _centres, True))
    assert tuning.validate(_sample()) == ["cost_centres: is missing"]


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
    assert len(lines) < 40


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
    assert list(doc) == [*tuning._HEADER, "capture", "prompting", "tags", "pieces", "agents", "overhead"]
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
    assert prompting["plans"] == {
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

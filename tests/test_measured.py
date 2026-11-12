"""R10: a field a newer parser added or redefined reads "not measured" in a
digest an older parser wrote (``src/claudeglass/measured.py``). The table of
fields, the decoder's marker for a digest from before the stamp, and the
Data quality sentence; later tests in this file build a store with one older
session and follow it through every figure that leaves it out.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta, timezone

import pytest

from claudeglass import PARSER_VERSION
from claudeglass.cache import DigestCache, encode_result, result_from_jsonable
from claudeglass.corpus import SessionBundle
from claudeglass.measured import (
    BEFORE_STAMP,
    FIELD_VERSIONS,
    NEWEST,
    is_older,
    measures,
    older_sentence,
    older_sessions,
)
from claudeglass.model import TranscriptMeta, TranscriptResult, Turn
from claudeglass.parse import parse_transcript
from claudeglass.service.rebuild import corpus_from_store

from helpers import fallback_read_session, store_with_older_sessions, turn_line, write_jsonl


def _parsed(tmp_path):
    path = tmp_path / "session-1.jsonl"
    write_jsonl(path, [turn_line(input_tokens=100, output_tokens=20), turn_line(input_tokens=110, output_tokens=25)])
    meta = TranscriptMeta(
        path=str(path), kind="top-level", session_id=path.stem, mtime_ns=1_000_000, size_bytes=path.stat().st_size
    )
    return path, meta, parse_transcript(path, meta)


def test_every_field_in_the_table_is_a_turn_or_result_field_at_or_below_the_parser_version():
    known = {f.name for f in dataclasses.fields(Turn)} | {f.name for f in dataclasses.fields(TranscriptResult)}
    assert set(FIELD_VERSIONS) <= known
    assert all(version <= PARSER_VERSION for version in FIELD_VERSIONS.values())
    assert NEWEST == max(FIELD_VERSIONS.values())


def test_a_digest_without_the_stamp_decodes_as_written_before_it(tmp_path):
    _, _, result = _parsed(tmp_path)
    data = json.loads(json.dumps(encode_result(result)))
    assert result_from_jsonable(data).parser_version == PARSER_VERSION
    data["parser_version"] = None
    assert result_from_jsonable(data).parser_version is None
    del data["parser_version"]
    assert result_from_jsonable(data).parser_version == BEFORE_STAMP == 0


def test_a_fresh_parse_measures_every_field(tmp_path):
    path, meta, result = _parsed(tmp_path)
    results = [result]
    cache = DigestCache(tmp_path / "config")
    cache.put(path, meta, result)
    results.append(cache.get(path, meta))
    when = datetime.now(timezone.utc) - timedelta(days=2)
    store, _ = store_with_older_sessions(
        tmp_path / "store", {"sess-a": fallback_read_session("sess-a", when)}, older=set()
    )
    [bundle] = corpus_from_store(store).sessions
    results.append(bundle.top)
    assert len(results) == 3
    for each in results:
        assert each is not None and each.parser_version == PARSER_VERSION
        assert measures(each, *FIELD_VERSIONS)
        assert not is_older(each)


@pytest.mark.parametrize("field", sorted(FIELD_VERSIONS))
def test_an_unstamped_digest_measures_none_of_the_newer_fields(field):
    result = TranscriptResult(parser_version=BEFORE_STAMP)
    assert not measures(result, field)
    assert is_older(result)


def test_a_result_built_in_memory_reads_as_current():
    result = TranscriptResult()
    assert result.parser_version is None
    assert measures(result, *FIELD_VERSIONS)
    assert not is_older(result)


def test_older_sessions_counts_a_session_once_when_any_of_its_transcripts_is_older():
    fresh = TranscriptResult(parser_version=PARSER_VERSION)
    old = TranscriptResult(parser_version=BEFORE_STAMP)
    bundles = [
        SessionBundle(session_id="a", top=fresh, subs=[fresh]),
        SessionBundle(session_id="b", top=fresh, subs=[old]),
        SessionBundle(session_id="c", top=old, subs=[old, old]),
        SessionBundle(session_id="d", top=None, subs=[old]),
    ]
    assert older_sessions(bundles) == (2, 3)


def test_the_older_sentence_in_the_singular_the_plural_and_never_at_zero():
    from test_help_coverage import _plain

    assert older_sentence(1, 2) == (
        "1 of 2 sessions in this window was read by an older version and its transcripts are gone, "
        "so the newer figures leave it out."
    )
    assert older_sentence(73, 82) == (
        "73 of 82 sessions in this window were read by an older version and their transcripts are gone, "
        "so the newer figures leave them out."
    )
    assert older_sentence(1, 1) == (
        "1 of 1 session in this window was read by an older version and its transcripts are gone, "
        "so the newer figures leave it out."
    )
    assert older_sentence(0, 5) == ""
    for n, m in ((1, 2), (73, 82), (1, 1)):
        _plain(older_sentence(n, m), f"older_sentence({n}, {m})")

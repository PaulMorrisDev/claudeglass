"""Tests for WP11's on-disk digest cache (``src/claudeglass/cache.py``):
hit/miss on mtime and size change, schema/parser version invalidation,
live-file bypass, corrupt-file recovery, purge, stats, and the
encode/decode round trip against every real fixture under
``tests/fixtures/``.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import pytest

from claudeglass import PARSER_VERSION, SCHEMA_VERSION
from claudeglass.cache import (
    FINGERPRINT,
    STALE_CACHE_VERSION_DAYS,
    CacheStats,
    DigestCache,
    encode_result,
    result_from_jsonable,
)
from claudeglass.model import TranscriptMeta
from claudeglass.parse import parse_transcript

from helpers import turn_line, write_jsonl

FIXTURES_ROOT = Path(__file__).resolve().parent / "fixtures"

#: A file older than the 60-second live window (see cache.py's
#: LIVE_FILE_WINDOW_S), used throughout so ordinary put/get tests aren't
#: accidentally exercising the live-file bypass path.
_OLD_NS = time.time_ns() - 3600 * 1_000_000_000  # one hour ago


def _write_transcript(tmp_path: Path, name: str = "session-1") -> Path:
    path = tmp_path / f"{name}.jsonl"
    write_jsonl(
        path,
        [
            turn_line(input_tokens=100, output_tokens=20),
            turn_line(input_tokens=110, output_tokens=25),
        ],
    )
    return path


def _meta_for(path: Path, mtime_ns: int = _OLD_NS) -> TranscriptMeta:
    size = path.stat().st_size
    return TranscriptMeta(path=str(path), kind="top-level", session_id=path.stem, mtime_ns=mtime_ns, size_bytes=size)


# -- basic put/get hit --------------------------------------------------


def test_put_then_get_is_a_hit(tmp_path):
    cache = DigestCache(tmp_path / "config")
    transcript_path = _write_transcript(tmp_path)
    meta = _meta_for(transcript_path)
    result = parse_transcript(transcript_path, meta)

    assert cache.get(transcript_path, meta) is None  # nothing cached yet

    cache.put(transcript_path, meta, result)
    hit = cache.get(transcript_path, meta)

    assert hit is not None
    assert hit == result


def test_get_before_any_put_is_a_miss(tmp_path):
    cache = DigestCache(tmp_path / "config")
    transcript_path = _write_transcript(tmp_path)
    meta = _meta_for(transcript_path)
    assert cache.get(transcript_path, meta) is None


# -- invalidation on mtime/size change -----------------------------------


def test_miss_when_mtime_changes(tmp_path):
    cache = DigestCache(tmp_path / "config")
    transcript_path = _write_transcript(tmp_path)
    meta = _meta_for(transcript_path)
    result = parse_transcript(transcript_path, meta)
    cache.put(transcript_path, meta, result)

    changed_meta = _meta_for(transcript_path, mtime_ns=meta.mtime_ns + 1)
    assert cache.get(transcript_path, changed_meta) is None
    # A stale (version/size/mtime mismatch) entry is left in place for a
    # later put() to overwrite -- it is not treated as corrupt.
    assert any(cache.cache_dir.glob("*.json"))


def test_miss_when_size_changes(tmp_path):
    cache = DigestCache(tmp_path / "config")
    transcript_path = _write_transcript(tmp_path)
    meta = _meta_for(transcript_path)
    result = parse_transcript(transcript_path, meta)
    cache.put(transcript_path, meta, result)

    changed_meta = TranscriptMeta(
        path=meta.path,
        kind=meta.kind,
        session_id=meta.session_id,
        mtime_ns=meta.mtime_ns,
        size_bytes=meta.size_bytes + 5,
    )
    assert cache.get(transcript_path, changed_meta) is None


# -- schema/parser version invalidation -----------------------------------


def test_miss_when_schema_version_does_not_match(tmp_path, monkeypatch):
    cache = DigestCache(tmp_path / "config")
    transcript_path = _write_transcript(tmp_path)
    meta = _meta_for(transcript_path)
    result = parse_transcript(transcript_path, meta)
    cache.put(transcript_path, meta, result)

    cache_file = next(cache.cache_dir.glob("*.json"))
    raw = json.loads(cache_file.read_text(encoding="utf-8"))
    assert raw["header"]["schema_version"] == SCHEMA_VERSION
    raw["header"]["schema_version"] = SCHEMA_VERSION + 1
    cache_file.write_text(json.dumps(raw), encoding="utf-8")

    assert cache.get(transcript_path, meta) is None
    # Still on disk -- a version bump is a stale miss, not corruption.
    assert cache_file.exists()


def test_miss_when_parser_version_does_not_match(tmp_path):
    cache = DigestCache(tmp_path / "config")
    transcript_path = _write_transcript(tmp_path)
    meta = _meta_for(transcript_path)
    result = parse_transcript(transcript_path, meta)
    cache.put(transcript_path, meta, result)

    cache_file = next(cache.cache_dir.glob("*.json"))
    raw = json.loads(cache_file.read_text(encoding="utf-8"))
    assert raw["header"]["parser_version"] == PARSER_VERSION
    raw["header"]["parser_version"] = PARSER_VERSION + 1
    cache_file.write_text(json.dumps(raw), encoding="utf-8")

    assert cache.get(transcript_path, meta) is None
    assert cache_file.exists()


def test_miss_when_fingerprint_does_not_match(tmp_path):
    """ROB-P4/P5: a header whose vocabulary fingerprint doesn't match the
    running code's own is a stale miss, the same as a parser-version
    mismatch -- catches a vocabulary edit even when PARSER_VERSION itself
    wasn't bumped for it.
    """
    cache = DigestCache(tmp_path / "config")
    transcript_path = _write_transcript(tmp_path)
    meta = _meta_for(transcript_path)
    result = parse_transcript(transcript_path, meta)
    cache.put(transcript_path, meta, result)

    cache_file = next(cache.cache_dir.glob("*.json"))
    raw = json.loads(cache_file.read_text(encoding="utf-8"))
    assert raw["header"]["fingerprint"] == FINGERPRINT
    raw["header"]["fingerprint"] = "not-the-real-fingerprint"
    cache_file.write_text(json.dumps(raw), encoding="utf-8")

    assert cache.get(transcript_path, meta) is None
    assert cache_file.exists()


def test_fingerprint_is_pinned():
    """ROB-P4/P5: any change to a closed vocabulary, tag key/label or
    ``PROMPT_FLAGS`` word changes :data:`FINGERPRINT` -- this pin fails
    the moment that happens, as a deliberate speed bump: it forces
    whoever made the change to notice it invalidates every existing
    cache entry (nothing else does -- unlike PARSER_VERSION, nobody has
    to remember to bump this by hand), not to silently ship it.
    """
    # Re-pinned for the ``report_reread`` and ``split_run`` tip titles (parser 43 already starts a new cache generation).
    assert FINGERPRINT == "1b51edd3cea4b3c2a19d137e9d9a76024e6c9050d133fe108fd40278df393080"


# -- salt fingerprint (SEC-P8) ----------------------------------------------


def test_put_stores_the_salt_fingerprint_of_the_given_salt(tmp_path):
    salt = b"a" * 32
    cache = DigestCache(tmp_path / "config", salt=salt)
    transcript_path = _write_transcript(tmp_path)
    meta = _meta_for(transcript_path)
    result = parse_transcript(transcript_path, meta)
    cache.put(transcript_path, meta, result)

    cache_file = next(cache.cache_dir.glob("*.json"))
    raw = json.loads(cache_file.read_text(encoding="utf-8"))
    assert raw["header"]["salt_fp"] == hashlib.sha256(salt).hexdigest()


def test_hit_when_a_later_instance_uses_the_same_salt(tmp_path):
    salt = b"b" * 32
    config_dir = tmp_path / "config"
    transcript_path = _write_transcript(tmp_path)
    meta = _meta_for(transcript_path)
    result = parse_transcript(transcript_path, meta)
    DigestCache(config_dir, salt=salt).put(transcript_path, meta, result)

    hit = DigestCache(config_dir, salt=salt).get(transcript_path, meta)
    assert hit is not None


def test_miss_when_the_salt_rotates(tmp_path):
    """SEC-P8: a cache entry written under one salt must never be served
    to a reader carrying a different one -- mtime/size/fingerprint alone
    would still match, but the entry's own salted hashes
    (``Turn.read_target_hashes``, skill-name hashes -- see
    ``cache._salt_fingerprint``'s docstring) no longer agree with
    anything a fresh parse under the new salt would produce.
    """
    config_dir = tmp_path / "config"
    transcript_path = _write_transcript(tmp_path)
    meta = _meta_for(transcript_path)
    result = parse_transcript(transcript_path, meta)
    DigestCache(config_dir, salt=b"c" * 32).put(transcript_path, meta, result)

    miss = DigestCache(config_dir, salt=b"d" * 32).get(transcript_path, meta)
    assert miss is None


def test_salt_is_not_checked_when_the_reader_has_none(tmp_path):
    """A caller that never threads a salt through (e.g. a purge-only or
    cache-inspection path) doesn't care what salt, if any, wrote the
    entry -- see ``DigestCache.__init__``'s own docstring note.
    """
    config_dir = tmp_path / "config"
    transcript_path = _write_transcript(tmp_path)
    meta = _meta_for(transcript_path)
    result = parse_transcript(transcript_path, meta)
    DigestCache(config_dir, salt=b"e" * 32).put(transcript_path, meta, result)

    hit = DigestCache(config_dir).get(transcript_path, meta)
    assert hit is not None


def test_miss_when_salt_fingerprint_does_not_match(tmp_path):
    """Same tampered-header pattern as
    ``test_miss_when_fingerprint_does_not_match``, for ``salt_fp``.
    """
    salt = b"f" * 32
    cache = DigestCache(tmp_path / "config", salt=salt)
    transcript_path = _write_transcript(tmp_path)
    meta = _meta_for(transcript_path)
    result = parse_transcript(transcript_path, meta)
    cache.put(transcript_path, meta, result)

    cache_file = next(cache.cache_dir.glob("*.json"))
    raw = json.loads(cache_file.read_text(encoding="utf-8"))
    raw["header"]["salt_fp"] = "not-the-real-salt-fp"
    cache_file.write_text(json.dumps(raw), encoding="utf-8")

    assert cache.get(transcript_path, meta) is None
    assert cache_file.exists()


def test_miss_for_a_pre_existing_entry_with_no_salt_fp_when_reader_has_a_salt(tmp_path):
    """An entry written before this feature existed (no ``salt_fp`` key
    at all) is treated as a stale miss once a salted reader comes along
    -- the same "unknown means invalidate, not silently trust" posture
    every other header field here already has.
    """
    config_dir = tmp_path / "config"
    transcript_path = _write_transcript(tmp_path)
    meta = _meta_for(transcript_path)
    result = parse_transcript(transcript_path, meta)
    DigestCache(config_dir).put(transcript_path, meta, result)

    cache_file = next((config_dir / "cache").glob(f"p{PARSER_VERSION}/*.json"))
    raw = json.loads(cache_file.read_text(encoding="utf-8"))
    assert raw["header"]["salt_fp"] is None
    del raw["header"]["salt_fp"]
    cache_file.write_text(json.dumps(raw), encoding="utf-8")

    salted_cache = DigestCache(config_dir, salt=b"g" * 32)
    assert salted_cache.get(transcript_path, meta) is None


# -- live-file bypass ------------------------------------------------------


def test_put_is_a_noop_for_a_live_file(tmp_path):
    cache = DigestCache(tmp_path / "config")
    transcript_path = _write_transcript(tmp_path)
    live_meta = _meta_for(transcript_path, mtime_ns=time.time_ns())
    result = parse_transcript(transcript_path, live_meta)

    cache.put(transcript_path, live_meta, result)

    assert not cache.cache_dir.exists() or not any(cache.cache_dir.glob("*.json"))


def test_get_never_serves_a_live_file_even_if_a_cache_entry_exists(tmp_path):
    cache = DigestCache(tmp_path / "config")
    transcript_path = _write_transcript(tmp_path)
    old_meta = _meta_for(transcript_path)
    result = parse_transcript(transcript_path, old_meta)
    cache.put(transcript_path, old_meta, result)
    assert cache.get(transcript_path, old_meta) is not None  # sanity: it's cached

    live_meta = TranscriptMeta(
        path=old_meta.path,
        kind=old_meta.kind,
        session_id=old_meta.session_id,
        mtime_ns=time.time_ns(),
        size_bytes=old_meta.size_bytes,
    )
    assert cache.get(transcript_path, live_meta) is None


# -- corrupt file recovery -------------------------------------------------


def test_corrupt_cache_file_is_a_miss_and_is_deleted(tmp_path):
    cache = DigestCache(tmp_path / "config")
    transcript_path = _write_transcript(tmp_path)
    meta = _meta_for(transcript_path)
    result = parse_transcript(transcript_path, meta)
    cache.put(transcript_path, meta, result)

    cache_file = next(cache.cache_dir.glob("*.json"))
    cache_file.write_text("not valid json{{{", encoding="utf-8")

    assert cache.get(transcript_path, meta) is None
    assert not cache_file.exists()


def test_cache_file_missing_result_key_is_a_miss_and_is_deleted(tmp_path):
    cache = DigestCache(tmp_path / "config")
    transcript_path = _write_transcript(tmp_path)
    meta = _meta_for(transcript_path)
    result = parse_transcript(transcript_path, meta)
    cache.put(transcript_path, meta, result)

    cache_file = next(cache.cache_dir.glob("*.json"))
    cache_file.write_text(json.dumps({"header": {}}), encoding="utf-8")

    assert cache.get(transcript_path, meta) is None
    assert not cache_file.exists()


# -- purge -----------------------------------------------------------------


def test_purge_all_removes_every_entry(tmp_path):
    cache = DigestCache(tmp_path / "config")
    for name in ("session-a", "session-b"):
        transcript_path = _write_transcript(tmp_path, name)
        meta = _meta_for(transcript_path)
        result = parse_transcript(transcript_path, meta)
        cache.put(transcript_path, meta, result)

    assert cache.stats().files == 2
    removed = cache.purge(all=True)
    assert removed == 2
    assert cache.stats().files == 0


def test_purge_older_than_days_keeps_recent_entries(tmp_path):
    import os

    cache = DigestCache(tmp_path / "config")
    old_path = _write_transcript(tmp_path, "session-old")
    old_meta = _meta_for(old_path)
    cache.put(old_path, old_meta, parse_transcript(old_path, old_meta))

    new_path = _write_transcript(tmp_path, "session-new")
    new_meta = _meta_for(new_path)
    cache.put(new_path, new_meta, parse_transcript(new_path, new_meta))

    # Backdate the "old" cache file's own mtime well past the cutoff.
    old_cache_file = cache._cache_path(old_path)
    ancient = time.time() - 40 * 86400
    os.utime(old_cache_file, (ancient, ancient))

    removed = cache.purge(older_than_days=30)
    assert removed == 1
    remaining = {p.name for p in cache.cache_dir.glob("*.json")}
    assert cache._cache_path(new_path).name in remaining
    assert cache._cache_path(old_path).name not in remaining


def test_purge_defaults_to_no_op(tmp_path):
    cache = DigestCache(tmp_path / "config")
    transcript_path = _write_transcript(tmp_path)
    meta = _meta_for(transcript_path)
    cache.put(transcript_path, meta, parse_transcript(transcript_path, meta))
    assert cache.purge() == 0
    assert cache.stats().files == 1


def test_purge_on_empty_cache_dir_is_a_noop(tmp_path):
    cache = DigestCache(tmp_path / "config")
    assert cache.purge(all=True) == 0
    assert cache.stats() == CacheStats(files=0, bytes=0)


# -- stats -------------------------------------------------------------------


def test_stats_counts_files_and_bytes(tmp_path):
    cache = DigestCache(tmp_path / "config")
    paths = []
    for name in ("session-x", "session-y", "session-z"):
        transcript_path = _write_transcript(tmp_path, name)
        meta = _meta_for(transcript_path)
        cache.put(transcript_path, meta, parse_transcript(transcript_path, meta))
        paths.append(transcript_path)

    stats = cache.stats()
    assert stats.files == 3
    on_disk_bytes = sum(p.stat().st_size for p in cache.cache_dir.glob("*.json"))
    assert stats.bytes == on_disk_bytes


# -- per-version cache path / stale-folder pruning (ROB-P4/P5) --------------


def test_cache_dir_is_nested_under_a_parser_version_folder(tmp_path):
    cache = DigestCache(tmp_path / "config")
    assert cache.cache_dir == tmp_path / "config" / "cache" / f"p{PARSER_VERSION}"
    assert cache.versions_dir == tmp_path / "config" / "cache"

    transcript_path = _write_transcript(tmp_path)
    meta = _meta_for(transcript_path)
    cache.put(transcript_path, meta, parse_transcript(transcript_path, meta))
    assert any((tmp_path / "config" / "cache" / f"p{PARSER_VERSION}").glob("*.json"))


def _age_dir(path: Path, days_old: float) -> None:
    """Back-date every file directly inside ``path`` (and the folder
    itself) by ``days_old`` days, for :meth:`DigestCache.prune_stale_versions`
    tests."""
    stamp = time.time() - days_old * 86400
    for child in path.iterdir():
        os.utime(child, (stamp, stamp))
    os.utime(path, (stamp, stamp))


def test_prune_stale_versions_removes_an_old_version_folder(tmp_path):
    config_dir = tmp_path / "config"
    cache = DigestCache(config_dir)
    transcript_path = _write_transcript(tmp_path)
    meta = _meta_for(transcript_path)
    cache.put(transcript_path, meta, parse_transcript(transcript_path, meta))

    old_version_dir = config_dir / "cache" / f"p{PARSER_VERSION - 1}"
    old_version_dir.mkdir(parents=True)
    (old_version_dir / "abc123.json").write_text("{}", encoding="utf-8")
    _age_dir(old_version_dir, STALE_CACHE_VERSION_DAYS + 1)

    removed = cache.prune_stale_versions()
    assert removed == 1
    assert not old_version_dir.exists()
    # This version's own folder is never touched by pruning.
    assert cache.cache_dir.exists()
    assert any(cache.cache_dir.glob("*.json"))


def test_prune_stale_versions_keeps_a_recent_old_version_folder(tmp_path):
    config_dir = tmp_path / "config"
    cache = DigestCache(config_dir)
    old_version_dir = config_dir / "cache" / f"p{PARSER_VERSION - 1}"
    old_version_dir.mkdir(parents=True)
    (old_version_dir / "abc123.json").write_text("{}", encoding="utf-8")
    # Freshly written -- well inside the retention window.

    assert cache.prune_stale_versions() == 0
    assert old_version_dir.exists()


def test_prune_stale_versions_ignores_folders_that_are_not_version_folders(tmp_path):
    config_dir = tmp_path / "config"
    cache = DigestCache(config_dir)
    stray = config_dir / "cache" / "not-a-version"
    stray.mkdir(parents=True)
    (stray / "leftover.json").write_text("{}", encoding="utf-8")
    _age_dir(stray, STALE_CACHE_VERSION_DAYS + 1)

    assert cache.prune_stale_versions() == 0
    assert stray.exists()


def test_prune_stale_versions_on_a_missing_cache_dir_is_a_noop(tmp_path):
    cache = DigestCache(tmp_path / "config")
    assert cache.prune_stale_versions() == 0


# -- key identity ------------------------------------------------------------


def test_same_realpath_different_case_hits_the_same_entry(tmp_path):
    cache = DigestCache(tmp_path / "config")
    transcript_path = _write_transcript(tmp_path, "session-CaseTest")
    meta = _meta_for(transcript_path)
    result = parse_transcript(transcript_path, meta)
    cache.put(transcript_path, meta, result)

    # Same file, differently-cased path string (Windows paths are
    # case-insensitive; os.path.normcase folds this before hashing).
    upper_path = Path(str(transcript_path).upper())
    if upper_path.exists():  # only meaningful on a case-insensitive filesystem
        assert cache.get(upper_path, meta) is not None


# -- encode/decode round trip against real fixtures --------------------------


def _all_fixture_jsonl() -> list[Path]:
    return sorted(FIXTURES_ROOT.rglob("*.jsonl"))


@pytest.mark.parametrize(
    "jsonl_path",
    _all_fixture_jsonl(),
    ids=lambda p: str(p.relative_to(FIXTURES_ROOT)).replace("\\", "/"),
)
def test_round_trip_every_fixture(jsonl_path):
    meta = TranscriptMeta(path=str(jsonl_path), kind="top-level", session_id=jsonl_path.stem)
    result = parse_transcript(jsonl_path, meta)

    encoded = encode_result(result)
    # Round through real JSON text, not just Python objects, so a value
    # that merely *looks* JSON-safe in memory (e.g. a non-str dict key)
    # would still be caught here.
    text = json.dumps(encoded)
    decoded = result_from_jsonable(json.loads(text))

    assert decoded == result


def test_round_trip_handles_an_empty_transcript(tmp_path):
    path = tmp_path / "empty.jsonl"
    write_jsonl(path, [])
    meta = TranscriptMeta(path=str(path), kind="top-level", session_id="empty")
    result = parse_transcript(path, meta)

    decoded = result_from_jsonable(json.loads(json.dumps(encode_result(result))))
    assert decoded == result
    assert decoded.turns == []
    assert decoded.events == []

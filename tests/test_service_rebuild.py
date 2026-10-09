"""Tests for ``service.rebuild.corpus_from_store``: the round trip that
lets a report be built from the SQLite store alone, with no transcript
files on disk (see that module's own docstring for why this matters —
it's what lets the store outlive Claude Code's own transcript
retention).

The headline test (``test_watcher_then_rebuild_matches_fresh_corpus_report``)
is the round-trip requirement from this work package's brief: load
``tests/fixtures/real/session-a`` and every ``tests/fixtures/diversity/*``
fixture as a fresh :class:`~claudeglass.corpus.Corpus`, build a
report from it, then separately copy the same fixture files into a
temporary ``projects_root``, run one :class:`FileWatcher` tick into a
temporary store, rebuild a corpus from *that* store with
``corpus_from_store``, and build a second report. The two reports' JSON
renderings must be byte-for-byte identical once ``meta.generated_at`` is
zeroed in both (the one field that's allowed, expected, to differ, since
each ``build_report`` call stamps it with the current wall-clock time).

Both fixture trees are skipped (not failed) when absent, matching the
existing convention in ``tests/test_real_fixture.py``/``test_report.py``.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from claudeglass.config import Config
from claudeglass.corpus import load_corpus
from claudeglass.pricing import load_pricing
from claudeglass.report import build_report
from claudeglass.render.json_out import render_json
from claudeglass.service.contracts import ServeOptions
from claudeglass.service.rebuild import corpus_from_store
from claudeglass.service.store import Store
from claudeglass.service.watcher import FileWatcher

from helpers import assert_privacy, turn_line, write_jsonl

PRICING = load_pricing()

REAL_DIR = Path(__file__).parent / "fixtures" / "real" / "session-a"
DIVERSITY_ROOT = Path(__file__).parent / "fixtures" / "diversity"

_DIVERSITY_DIRS = sorted(DIVERSITY_ROOT.glob("*")) if DIVERSITY_ROOT.exists() else []

pytestmark = pytest.mark.skipif(
    not REAL_DIR.exists() or not any(REAL_DIR.glob("*.jsonl")) or not _DIVERSITY_DIRS,
    reason="tests/fixtures/real/session-a or tests/fixtures/diversity/* not present",
)


def _fixture_project_dirs() -> list[Path]:
    return [REAL_DIR, *_DIVERSITY_DIRS]


def _zero_generated_at(rendered: str) -> dict:
    data = json.loads(rendered)
    data["report"]["meta"]["generated_at"] = ""
    return data


def test_watcher_then_rebuild_matches_fresh_corpus_report(tmp_path: Path):
    project_dirs = _fixture_project_dirs()
    slugs = tuple(d.name for d in project_dirs)

    fresh_corpus = load_corpus(project_dirs)
    fresh_report = build_report(fresh_corpus, PRICING, Config(), projects=slugs, window="round trip")
    fresh_json = _zero_generated_at(render_json(fresh_report))

    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    for project_dir in project_dirs:
        shutil.copytree(project_dir, projects_root / project_dir.name)

    config_dir = tmp_path / "config"
    options = ServeOptions(projects_root=projects_root, config_dir=config_dir)
    store = Store(":memory:")
    store.open()
    watcher = FileWatcher(store, options)
    stats = watcher.run_once()
    assert_privacy(stats)
    assert stats.errors == 0

    rebuilt_corpus = corpus_from_store(store)
    rebuilt_report = build_report(rebuilt_corpus, PRICING, Config(), projects=slugs, window="round trip")
    rebuilt_json = _zero_generated_at(render_json(rebuilt_report))

    assert rebuilt_json == fresh_json


def _api_backing_bodies(store: Store) -> dict:
    """Every ``/api/*`` Store-backed route's own data (as opposed to the
    Report-backed routes, which go through ``corpus_from_store`` --
    ``api.py``'s own module docstring names the split), serialised
    through the exact same JSON round trip a real HTTP response body
    goes through -- so a change to internal storage shape (S1-perf's
    ``digest_json``->``digest_blob`` compression, ``events``->
    ``events_agg`` aggregation) that somehow leaked into one of these
    dicts would show up as a JSON diff here, not just as a Python
    object-identity difference "close enough" not to notice."""
    session_ids = [row["id"] for row in store.sessions(limit=1_000_000)]
    return json.loads(
        json.dumps(
            {
                "summary": store.summary(),
                "sessions": store.sessions(limit=1_000_000),
                "recache": store.recache(),
                "compactions": store.compactions(),
                "session_detail": {sid: store.session(sid) for sid in session_ids},
            },
            sort_keys=True,
        )
    )


def test_api_backing_bodies_survive_a_full_store_rebuild(tmp_path: Path):
    """S1-perf's storage-shape changes (item 3's write batching, item 4's
    ``events_agg`` aggregation and ``digest_blob`` compression) must not
    change a single byte of any ``/api/*`` response body (this work
    package's own acceptance bar). ``Store.migrate``'s only migration
    path is "drop every table and let the next watcher tick repopulate
    them from scratch" (see ``schema.py``'s module docstring) -- so a
    full store rebuild is simulated exactly that way: run one watcher
    tick over a real fixture corpus, capture every Store-backed route's
    own body, drop and recreate every table, run a second watcher tick
    over the *same* on-disk files into the now-empty store, and assert
    the five bodies it produces are identical to the first tick's.
    """
    project_dirs = _fixture_project_dirs()

    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    for project_dir in project_dirs:
        shutil.copytree(project_dir, projects_root / project_dir.name)

    config_dir = tmp_path / "config"
    options = ServeOptions(projects_root=projects_root, config_dir=config_dir)
    store = Store(tmp_path / "rebuild.db")
    store.open()

    stats1 = FileWatcher(store, options).run_once()
    assert_privacy(stats1)
    assert stats1.errors == 0
    before = _api_backing_bodies(store)

    # Simulate the schema's only "migration": drop every table and let a
    # fresh tick repopulate them (Store.migrate's own behaviour whenever
    # the recorded schema_version differs from schema.SCHEMA_VERSION).
    conn = store._connection()
    store._drop_all_tables(conn)
    store.migrate()
    assert store.known_files() == {}

    stats2 = FileWatcher(store, options).run_once()
    assert_privacy(stats2)
    assert stats2.errors == 0
    after = _api_backing_bodies(store)

    assert after == before


def test_corpus_from_store_window_filters_like_discovery(tmp_path: Path):
    """A session outside the requested window is excluded, the same way
    ``discovery.find_sessions(..., days=N)`` would never have surfaced it
    to a fresh ``load_corpus`` call."""
    root = tmp_path / "projects"
    project_dir = root / "proj-a"
    project_dir.mkdir(parents=True)
    old_path = project_dir / "sess-old.jsonl"
    new_path = project_dir / "sess-new.jsonl"
    write_jsonl(old_path, [turn_line(timestamp="2020-01-01T00:00:00.000Z")])
    write_jsonl(new_path, [turn_line(timestamp="2026-09-18T00:00:00.000Z")])

    import os
    import time

    old_mtime = time.time() - 10 * 365 * 24 * 3600
    os.utime(old_path, (old_mtime, old_mtime))
    new_mtime = time.time() - 3600
    os.utime(new_path, (new_mtime, new_mtime))

    options = ServeOptions(projects_root=root, config_dir=tmp_path / "config")
    store = Store(":memory:")
    store.open()
    watcher = FileWatcher(store, options)
    watcher.run_once()

    corpus_all = corpus_from_store(store)
    assert {b.session_id for b in corpus_all.sessions} == {"sess-old", "sess-new"}

    corpus_recent = corpus_from_store(store, days=30)
    assert {b.session_id for b in corpus_recent.sessions} == {"sess-new"}


def test_corpus_from_store_first_reply_leaves_out_sessions_already_running(tmp_path: Path):
    """``window_by="first-reply"`` (the "since my last change" window)
    keeps the sessions that started in the window, not those that were
    only still running in it."""
    root = tmp_path / "projects"
    project_dir = root / "proj-a"
    project_dir.mkdir(parents=True)
    write_jsonl(
        project_dir / "sess-running.jsonl",
        [turn_line(timestamp="2026-09-17T10:00:00.000Z"), turn_line(timestamp="2026-09-18T10:00:00.000Z")],
    )
    write_jsonl(project_dir / "sess-started.jsonl", [turn_line(timestamp="2026-09-18T09:00:00.000Z")])

    options = ServeOptions(projects_root=root, config_dir=tmp_path / "config")
    store = Store(":memory:")
    store.open()
    FileWatcher(store, options).run_once()

    since = "2026-09-17T12:00:00Z"
    last = corpus_from_store(store, since=since)
    assert {b.session_id for b in last.sessions} == {"sess-running", "sess-started"}
    first = corpus_from_store(store, since=since, window_by="first-reply")
    assert {b.session_id for b in first.sessions} == {"sess-started"}


def test_corpus_from_store_filters_by_project_slugs(tmp_path: Path):
    """``project_slugs`` (additive, project-filter work) keeps only
    sessions whose raw ``sessions.slug`` is in the given list, and
    composes with window filtering rather than replacing it -- a project
    filter plus a window that excludes that project's only session still
    leaves nothing, even though the project itself is allowed."""
    root = tmp_path / "projects"
    proj_a = root / "proj-a"
    proj_b = root / "proj-b"
    proj_a.mkdir(parents=True)
    proj_b.mkdir(parents=True)
    write_jsonl(proj_a / "sess-a.jsonl", [turn_line(timestamp="2026-09-18T00:00:00.000Z")])
    write_jsonl(proj_b / "sess-b.jsonl", [turn_line(timestamp="2026-09-18T00:00:00.000Z")])

    options = ServeOptions(projects_root=root, config_dir=tmp_path / "config")
    store = Store(":memory:")
    store.open()
    FileWatcher(store, options).run_once()

    corpus_a = corpus_from_store(store, project_slugs=["proj-a"])
    assert {b.session_id for b in corpus_a.sessions} == {"sess-a"}

    corpus_b = corpus_from_store(store, project_slugs=["proj-b"])
    assert {b.session_id for b in corpus_b.sessions} == {"sess-b"}

    corpus_both = corpus_from_store(store, project_slugs=["proj-a", "proj-b"])
    assert {b.session_id for b in corpus_both.sessions} == {"sess-a", "sess-b"}

    corpus_unknown = corpus_from_store(store, project_slugs=["no-such-project"])
    assert corpus_unknown.sessions == []

    corpus_windowed = corpus_from_store(store, since="2030-01-01T00:00:00Z", project_slugs=["proj-a"])
    assert corpus_windowed.sessions == []


def test_corpus_from_store_session_ids_keep_only_those_sessions_whatever_the_window(tmp_path: Path):
    """``session_ids`` (the session drawer's rating facts, the banner's
    unrated list) keeps the named sessions only, and a session outside the
    window is still found: the drawer opens sessions of any age."""
    root = tmp_path / "projects"
    proj_a = root / "proj-a"
    proj_b = root / "proj-b"
    proj_a.mkdir(parents=True)
    proj_b.mkdir(parents=True)
    old_path = proj_a / "sess-old.jsonl"
    write_jsonl(old_path, [turn_line(timestamp="2020-01-01T00:00:00.000Z")])
    write_jsonl(proj_a / "sess-new.jsonl", [turn_line(timestamp="2026-09-18T00:00:00.000Z")])
    write_jsonl(proj_b / "sess-other.jsonl", [turn_line(timestamp="2026-09-18T00:00:00.000Z")])

    options = ServeOptions(projects_root=root, config_dir=tmp_path / "config")
    store = Store(":memory:")
    store.open()
    FileWatcher(store, options).run_once()

    assert {b.session_id for b in corpus_from_store(store, session_ids=["sess-new"]).sessions} == {"sess-new"}
    both = corpus_from_store(store, session_ids=["sess-new", "sess-other", "no-such-session"])
    assert {b.session_id for b in both.sessions} == {"sess-new", "sess-other"}
    # The window leaves the old one out of a report, but not out of a lookup by id.
    assert {b.session_id for b in corpus_from_store(store, since="2026-01-01T00:00:00Z").sessions} == {
        "sess-new", "sess-other",
    }
    old = corpus_from_store(store, since="2026-01-01T00:00:00Z", session_ids=["sess-old"])
    assert [b.session_id for b in old.sessions] == ["sess-old"] and old.sessions[0].top is not None
    # It composes with the project filter, and an empty list is no sessions at all.
    assert corpus_from_store(store, session_ids=["sess-new"], project_slugs=["proj-b"]).sessions == []
    assert corpus_from_store(store, session_ids=[]).sessions == []
    assert len(corpus_from_store(store, session_ids=None).sessions) == 3


def test_corpus_from_store_skips_session_with_no_stored_transcripts(tmp_path: Path):
    """A ``sessions`` row with no matching ``transcripts`` rows at all
    (shouldn't normally arise from the watcher, but is cheap to guard) is
    skipped rather than producing a bundle with ``top=None``."""
    store = Store(":memory:")
    store.open()
    store.upsert_session(session_id="ghost", project_slug="proj-a", slug="proj-a")

    corpus = corpus_from_store(store)
    assert corpus.sessions == []

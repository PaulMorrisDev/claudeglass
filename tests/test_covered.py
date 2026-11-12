"""What an export or a CLI report covers (``covered``, R12): the sentences,
the store's list of a window's sessions and the report's meta lines. The CLI's own promises are in
``tests/test_cli_covered.py``.
"""

from __future__ import annotations

import html
import sqlite3
import zlib
from datetime import datetime, timedelta, timezone

import pytest

from claudeglass import covered, discovery, parse
from claudeglass.corpus import load_corpus
from claudeglass.model import ReportMeta, ReportModel
from claudeglass.render.html import render_html
from claudeglass.render.markdown import render_markdown
from claudeglass.service import rebuild
from claudeglass.service.store import Store

from helpers import fallback_read_session, store_with_gone_sessions
from test_tuning import _plain

NOW = datetime.now(timezone.utc).replace(microsecond=0)

ONE = (
    "These figures cover 1 of 3 sessions in this window. "
    "The transcripts of the other 2 were not found, and the dashboard still has those sessions."
)


def S(session_id: str, days: float) -> list[dict]:
    return fallback_read_session(session_id, NOW - timedelta(days=days))


def three_sessions(tmp_path):
    """``(projects_root, config_dir, claude_root)`` of the plan's world: one session on disk, two gone."""
    return store_with_gone_sessions(
        tmp_path,
        {"proj-a": {"keep": S("keep", 1), "gone-a": S("gone-a", 2), "gone-b": S("gone-b", 3)}},
        {"gone-a", "gone-b"},
    )


@pytest.fixture(autouse=True)
def _salt():
    parse.set_salt(b"d" * 32)


def test_the_gone_sentence_in_the_singular_the_plural_and_never_when_all_are_covered():
    assert covered.gone_sentence(1, 3) == ONE
    assert covered.gone_sentence(9, 82) == (
        "These figures cover 9 of 82 sessions in this window. "
        "The transcripts of the other 73 were not found, and the dashboard still has those sessions."
    )
    assert covered.gone_sentence(0, 1) == (
        "These figures cover 0 of 1 session in this window. "
        "The transcript of the other session was not found, and the dashboard still has that session."
    )
    assert covered.gone_sentence(8, 9) == (
        "These figures cover 8 of 9 sessions in this window. "
        "The transcript of the other session was not found, and the dashboard still has that session."
    )
    assert covered.gone_sentence(3, 3) == ""
    assert covered.gone_sentence(4, 3) == ""
    for sentence in (covered.gone_sentence(1, 3), covered.gone_sentence(9, 82), covered.gone_sentence(0, 1)):
        _plain(sentence)


def test_the_gone_sentence_of_a_file_names_the_machine_that_made_it():
    assert covered.gone_sentence(9, 82, of_file=True) == (
        "These figures cover 9 of 82 sessions in this window. "
        "The transcripts of the other 73 were not found on the machine that made this file."
    )
    assert covered.gone_sentence(0, 1, of_file=True) == (
        "These figures cover 0 of 1 session in this window. "
        "The transcript of the other session was not found on the machine that made this file."
    )
    assert covered.gone_sentence(8, 9, of_file=True) == (
        "These figures cover 8 of 9 sessions in this window. "
        "The transcript of the other session was not found on the machine that made this file."
    )
    assert covered.gone_sentence(3, 3, of_file=True) == ""
    for sentence in (covered.gone_sentence(9, 82, of_file=True), covered.gone_sentence(0, 1, of_file=True)):
        _plain(sentence)


def test_the_stored_sentence_in_the_singular_the_plural_and_never_at_zero():
    assert covered.stored_sentence(73, 82) == (
        "73 of 82 sessions in this window are read from what the dashboard kept, "
        "because their transcripts were not found."
    )
    assert covered.stored_sentence(1, 3) == (
        "1 of 3 sessions in this window is read from what the dashboard kept, "
        "because their transcripts were not found."
    )
    assert covered.stored_sentence(1, 1) == (
        "1 of 1 session in this window is read from what the dashboard kept, "
        "because their transcripts were not found."
    )
    assert covered.stored_sentence(0, 3) == ""
    for sentence in (covered.stored_sentence(73, 82), covered.stored_sentence(1, 3), covered.stored_sentence(1, 1)):
        _plain(sentence)


def test_the_stored_sentence_of_a_file_names_the_machine_that_made_it():
    assert covered.stored_sentence(73, 82, of_file=True) == (
        "73 of 82 sessions in this window were read from what the dashboard kept "
        "on the machine that made this file. Their transcripts were not found there."
    )
    assert covered.stored_sentence(1, 3, of_file=True) == (
        "1 of 3 sessions in this window was read from what the dashboard kept "
        "on the machine that made this file. Its transcript was not found there."
    )
    assert covered.stored_sentence(0, 3, of_file=True) == ""
    # Nothing in it says "here" or speaks of the reader's own dashboard.
    for sentence in (covered.stored_sentence(73, 82, of_file=True), covered.gone_sentence(9, 82, of_file=True)):
        assert "here" not in sentence.split() and "your" not in sentence.split()
        _plain(sentence)


def test_no_sentence_gives_a_reason_the_code_does_not_check():
    sentences = (
        covered.gone_sentence(1, 3),
        covered.stored_sentence(2, 3),
        covered.gone_sentence(1, 3, of_file=True),
        covered.stored_sentence(2, 3, of_file=True),
    )
    for sentence in sentences:
        assert "day" not in sentence and "keeps" not in sentence
    assert not hasattr(covered, "kept_days")


def test_window_session_ids_are_the_sessions_corpus_from_store_builds(tmp_path):
    _projects, config_dir, _claude = three_sessions(tmp_path)
    store = Store(config_dir / "service.db")
    try:
        store.ensure_session(session_id="bare", project_slug="proj-a")  # a row with no transcript
        assert store.window_session_ids() == {"keep": "proj-a", "gone-a": "proj-a", "gone-b": "proj-a"}
        since = discovery.utc_stamp(NOW - timedelta(days=2.5), seconds=True)
        in_window = store.window_session_ids(since=since)
        assert set(in_window) == {"keep", "gone-a"}
        assert set(in_window) == {b.session_id for b in rebuild.corpus_from_store(store, since=since).sessions}
    finally:
        store.close()


def test_the_markdown_and_html_meta_say_what_the_report_covers_only_when_some_are_gone():
    def model(**meta) -> ReportModel:
        return ReportModel(meta=ReportMeta(window="last 30 days", sessions=1, **meta))

    gone = model(sessions_left_out=2)
    lines = render_markdown(gone).splitlines()
    assert lines.count("- " + ONE) == 1
    assert lines.index("- " + ONE) == next(i for i, line in enumerate(lines) if line.startswith("- Window:")) + 1
    assert render_html(gone).count(html.escape(ONE)) == 1
    for none_gone in (model(), model(sessions_left_out=0)):
        assert "These figures cover" not in render_markdown(none_gone)
        assert "These figures cover" not in render_html(none_gone)


def test_open_read_only_keeps_the_config_dir(tmp_path):
    _projects, config_dir, _claude = three_sessions(tmp_path)
    with_dir = Store.open_read_only(config_dir / "service.db", config_dir=config_dir)
    without = Store.open_read_only(config_dir / "service.db")
    try:
        assert with_dir.config_dir == config_dir
        assert without.config_dir is None
    finally:
        with_dir.close()
        without.close()


def test_add_stored_sessions_adds_only_what_disk_lacks_in_load_corpus_order(tmp_path):
    projects, config_dir, _claude = three_sessions(tmp_path)
    disk = load_corpus([projects / "proj-a"], salt=b"d" * 32, config_dir=config_dir)
    assert [b.session_id for b in disk.sessions] == ["keep"]
    store = Store.open_read_only(config_dir / "service.db", config_dir=config_dir)
    try:
        merged = rebuild.add_stored_sessions(disk, store, ["gone-b", "keep", "gone-a", "nope"])
        # First reply, oldest first: the order load_corpus gives.
        assert [b.session_id for b in merged.sessions] == ["gone-b", "gone-a", "keep"]
        assert merged.sessions[2] is disk.sessions[0]
        assert merged.total_files == disk.total_files + 2
        assert rebuild.add_stored_sessions(disk, store, []) is disk
    finally:
        store.close()


def test_add_stored_sessions_skips_the_session_whose_digest_cannot_be_read(tmp_path):
    projects, config_dir, _claude = three_sessions(tmp_path)
    conn = sqlite3.connect(config_dir / "service.db")
    conn.execute("UPDATE transcripts SET digest_blob = ? WHERE session_id = 'gone-b'", (zlib.compress(b'{"turns": 5}'),))
    conn.commit()
    conn.close()
    disk = load_corpus([projects / "proj-a"], salt=b"d" * 32, config_dir=config_dir)
    store = Store.open_read_only(config_dir / "service.db", config_dir=config_dir)
    try:
        merged = rebuild.add_stored_sessions(disk, store, ["gone-a", "gone-b"])
        assert [b.session_id for b in merged.sessions] == ["gone-a", "keep"]
        assert rebuild.add_stored_sessions(disk, store, ["gone-b"]) is disk
    finally:
        store.close()

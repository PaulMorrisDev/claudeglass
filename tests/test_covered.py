"""What an export or a CLI report covers (``covered``, R12): the sentences, the
days Claude Code keeps transcripts, the store's list of a window's sessions
and the report's meta lines. The CLI's own promises are in
``tests/test_cli_covered.py``.
"""

from __future__ import annotations

import html
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest

from claudeglass import covered, discovery, parse
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
    "Claude Code keeps transcripts for 30 days here, and the dashboard has the other 2."
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


def test_kept_days_is_the_newest_settings_snapshots_cleanup_period_else_30():
    def eff(value):
        return NS(data={"effective": {"cleanupPeriodDays": value}})

    assert covered.kept_days([]) == 30
    assert covered.kept_days([eff(45)]) == 45
    # The newest snapshot records no settings, so the older one that does is used.
    assert covered.kept_days([eff(45), NS(data={"user_settings": {}})]) == 45
    assert covered.kept_days([eff(45), NS(data={"effective": {}})]) == 30
    assert covered.kept_days([eff(45), eff(7)]) == 7
    for value in ("45", True, -1):
        assert covered.kept_days([eff(value)]) == 30
    assert covered.kept_days([eff(0)]) == 0


def test_the_gone_sentence_in_the_singular_the_plural_and_never_when_all_are_covered():
    assert covered.gone_sentence(1, 3, 30) == ONE
    assert covered.gone_sentence(9, 82, 1) == (
        "These figures cover 9 of 82 sessions in this window. "
        "Claude Code keeps transcripts for 1 day here, and the dashboard has the other 73."
    )
    assert covered.gone_sentence(0, 1, 30) == (
        "These figures cover 0 of 1 session in this window. "
        "Claude Code keeps transcripts for 30 days here, and the dashboard has the other 1."
    )
    assert covered.gone_sentence(3, 3, 30) == ""
    assert covered.gone_sentence(4, 3, 30) == ""
    for sentence in (covered.gone_sentence(1, 3, 30), covered.gone_sentence(9, 82, 1), covered.gone_sentence(0, 1, 30)):
        _plain(sentence)


def test_the_stored_sentence_in_the_singular_the_plural_and_never_at_zero():
    assert covered.stored_sentence(2, 3, 30) == (
        "2 of 3 sessions in this window are read from what the dashboard kept, "
        "because Claude Code keeps transcripts for 30 days here."
    )
    assert covered.stored_sentence(1, 3, 45) == (
        "1 of 3 sessions in this window is read from what the dashboard kept, "
        "because Claude Code keeps transcripts for 45 days here."
    )
    assert covered.stored_sentence(1, 1, 30) == (
        "1 of 1 session in this window is read from what the dashboard kept, "
        "because Claude Code keeps transcripts for 30 days here."
    )
    assert covered.stored_sentence(0, 3, 30) == ""
    for sentence in (covered.stored_sentence(2, 3, 30), covered.stored_sentence(1, 3, 45), covered.stored_sentence(1, 1, 30)):
        _plain(sentence)


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

    gone = model(sessions_known=3, kept_days=30)
    lines = render_markdown(gone).splitlines()
    assert lines.count("- " + ONE) == 1
    assert lines.index("- " + ONE) == next(i for i, line in enumerate(lines) if line.startswith("- Window:")) + 1
    assert render_html(gone).count(html.escape(ONE)) == 1
    for none_gone in (model(), model(sessions_known=1, kept_days=30)):
        assert "These figures cover" not in render_markdown(none_gone)
        assert "These figures cover" not in render_html(none_gone)

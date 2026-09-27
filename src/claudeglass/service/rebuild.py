"""Reconstruct a :class:`~claudeglass.corpus.Corpus` from a
:class:`~claudeglass.service.store.Store` alone, with no transcript
files on disk.

This is what lets the store outlive Claude Code's own
``cleanupPeriodDays`` transcript retention: once ``FileWatcher`` (see
``service/watcher.py``) has folded a transcript's
``TranscriptResult`` into ``transcripts.digest_blob`` (via
``cache.encode_result`` then ``store.encode_digest_blob`` — the exact
same JSON encoding ``cache.DigestCache`` uses on disk, zlib-compressed
before storage, per that column's own docstring in
``service/schema.py``), the original ``.jsonl`` file is no longer needed
to answer a report query against that window: :func:`corpus_from_store`
decodes every stored digest back into a full ``TranscriptResult``
(``cache.result_from_jsonable`` — a lossless round trip, the same one
``tests/test_cache.py`` already asserts for the on-disk cache) and
reassembles them into the same ``SessionBundle``/``Corpus`` shape
``corpus.load_corpus`` would have produced from the live files, so
``report.build_report(corpus, ...)`` runs unmodified against either.

**Workflow runs** (S1-integration fix 1.d): ``SessionBundle.workflows``
is rebuilt from the ``workflow_runs`` table the watcher now populates
(``workflows.parse_workflow_file``/``link_workflow_agents``, already
cost-linked at write time). One deliberate, documented approximation
survives: ``workflow_runs.phases`` stores only ``WorkflowRun
.phase_titles`` (names — never ``detail``, which carries workflow
source/prompt text), so a rebuilt ``WorkflowRun.phases`` count is
``len(phase_titles)`` rather than the fresh parse's own count of *every*
phase entry in the run file — the two differ only when some phase entry
in the original file had no ``title`` at all, which none of this work
package's fixtures (including the new ``tests/fixtures/diversity/
workflow-session``) do, so the round-trip test still matches byte for
byte.

What else does NOT round-trip, and why:

- **``SessionBundle.project_dir``.** Always ``""`` here — nothing in
  ``report.build_report``'s own code path reads it (grepped: only
  ``corpus.py`` itself references ``.project_dir``), so this is a
  no-op loss, not a reportable one.
- **A project directory with zero stored sessions.** ``corpus_from_store``
  has no way to know a project directory exists at all unless the store
  has at least one session row for it -- an empty (or transcript-less)
  project directory under ``--projects-root`` that a fresh
  ``discovery``-based scan would still list is simply absent from a
  rebuilt ``Corpus``, and therefore from a report-backed route's
  ``report.meta.projects`` (``docs/api.md``'s "Report routes: how they
  are computed" section documents this from the API side). Confirmed
  against a real corpus during v0.2 release verification -- not fixed,
  since the only way to close it is a live directory read the whole
  point of this module is to avoid.
- Everything else on ``TranscriptResult``/``TranscriptMeta`` (including
  the three provenance fields ``schema.py`` singles out as
  store-internal-only — ``path``, seen here only for grouping rows by
  ``session_id``/``kind``, never copied into a rebuilt dataclass field)
  round-trips exactly, because ``digest_blob`` already *is* the encoded
  (compressed) form of the whole dataclass, not a lossy summary of it.
"""

from __future__ import annotations

import json

from .. import haiku_tags
from ..cache import result_from_jsonable
from ..corpus import Corpus, SessionBundle, _session_sort_key
from ..discovery import _resolve_window, ts_in_window
from ..model import WorkflowRun
from .store import Store, decode_digest_blob, window_column


def _window_ts(top_row, window_by: str):
    """The same "window key" concept ``discovery._session_window_ts``
    computes from a live file, derived instead from what the store
    already has for that transcript.

    ``window_by="mtime"`` uses the stored ``transcripts.mtime_ns`` for
    the session's top-level transcript (exact match to the live-file
    behaviour). ``window_by="timestamp"`` is necessarily an
    approximation: the store has no raw JSONL lines left to find "the
    first user/assistant line's timestamp" from, so this uses the
    earliest turn timestamp across the (already-decoded) top transcript
    instead — the same value :func:`~claudeglass.corpus
    ._session_first_ts` would compute for the reassembled bundle, just
    computed one transcript early so it can gate inclusion before the
    rest of the session's transcripts are even decoded.
    """
    if window_by == "mtime":
        from datetime import datetime, timezone

        mtime_ns = top_row["mtime_ns"]
        if mtime_ns is None:
            return None
        return datetime.fromtimestamp(mtime_ns / 1_000_000_000, tz=timezone.utc)
    if window_by == "timestamp":
        top = result_from_jsonable(json.loads(decode_digest_blob(top_row["digest_blob"])))
        for turn in top.turns:
            if turn.ts:
                from datetime import datetime

                try:
                    return datetime.fromisoformat(turn.ts.replace("Z", "+00:00"))
                except ValueError:
                    continue
        return None
    raise ValueError(f"unknown window_by: {window_by!r}")


def corpus_from_store(
    store: Store,
    *,
    days: int | None = None,
    since: str | None = None,
    until: str | None = None,
    window_by: str = "last-reply",
    project_slugs: list[str] | None = None,
) -> Corpus:
    """Rebuild a :class:`Corpus` entirely from ``store`` — no transcript
    files read. See the module docstring for what this makes possible
    and the two fields that don't survive the round trip.

    ``days``/``since``/``until``/``window_by`` mirror
    ``discovery.find_sessions``'s own parameters (same
    ``discovery._resolve_window`` resolution): a session whose top-level
    transcript falls outside the resolved window is skipped, exactly as
    it would never have been discovered by a fresh ``load_corpus`` call
    over the same window. ``window_by="last-reply"`` (the default) reads
    the session row's stored ``last_ts``, its last reply across every
    transcript, which is what ``load_corpus`` filters on;
    ``"first-reply"`` reads its ``first_ts`` (service only: the "since my
    last change" window counts the sessions that started after it). A session with
    no stored top-level transcript
    at all (shouldn't normally happen — ``FileWatcher`` always upserts
    one alongside any of a session's subagents) is skipped rather than
    guessed at, matching ``report.build_report``'s own
    ``if top is None: continue`` posture for a bundle with no top.

    ``project_slugs`` (additive, project-filter work): raw
    ``sessions.slug`` values (already resolved from a client's redacted
    ``project`` query param by ``api.py``'s ``_project_query``/
    ``Store.resolve_project_slug``) to keep; ``None`` (the default) keeps
    every project, matching every existing caller exactly.
    """
    conn = store._connection()
    since_dt, until_dt = _resolve_window(days, since, until)
    has_window_filter = since_dt is not None or until_dt is not None
    allowed_slugs = set(project_slugs) if project_slugs is not None else None

    session_rows = conn.execute("SELECT id, slug, first_ts, last_ts FROM sessions").fetchall()

    bundles: list[SessionBundle] = []
    total_bytes = 0
    total_files = 0

    for session_row in session_rows:
        session_id = session_row["id"]
        slug = session_row["slug"]
        if allowed_slugs is not None and slug not in allowed_slugs:
            continue
        if window_by in ("last-reply", "first-reply") and not ts_in_window(
            session_row[window_column(window_by)], since_dt, until_dt
        ):
            continue

        transcript_rows = conn.execute(
            "SELECT kind, digest_blob, mtime_ns, size_bytes FROM transcripts "
            "WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
        if not transcript_rows:
            continue

        top_row = next((row for row in transcript_rows if row["kind"] == "top-level"), None)
        if top_row is None:
            continue

        if has_window_filter and window_by not in ("last-reply", "first-reply"):
            window_ts = _window_ts(top_row, window_by)
            if window_ts is None:
                continue
            if since_dt is not None and window_ts < since_dt:
                continue
            if until_dt is not None and window_ts > until_dt:
                continue

        top = result_from_jsonable(json.loads(decode_digest_blob(top_row["digest_blob"])))
        subs = [
            result_from_jsonable(json.loads(decode_digest_blob(row["digest_blob"])))
            for row in transcript_rows
            if row["kind"] != "top-level"
        ]

        for row in transcript_rows:
            total_files += 1
            total_bytes += row["size_bytes"] or 0

        workflow_rows = conn.execute(
            "SELECT run_id, agent_count, phases, started, finished, cost, status "
            "FROM workflow_runs WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
        workflow_runs = []
        for wrow in workflow_rows:
            try:
                phase_titles = tuple(json.loads(wrow["phases"]))
            except (TypeError, ValueError):
                phase_titles = ()
            workflow_runs.append(
                WorkflowRun(
                    run_id=wrow["run_id"],
                    session_id=session_id,
                    agent_count=wrow["agent_count"],
                    phases=len(phase_titles),
                    started=wrow["started"],
                    finished=wrow["finished"],
                    cost=wrow["cost"],
                    status=wrow["status"],
                    phase_titles=phase_titles,
                )
            )

        bundles.append(
            SessionBundle(
                session_id=session_id,
                slug=slug,
                top=top,
                subs=subs,
                workflows=workflow_runs,
                project_dir="",
            )
        )

    bundles.sort(key=_session_sort_key)

    corpus = Corpus(
        sessions=bundles,
        total_files=total_files,
        total_bytes=total_bytes,
        cache_hits=0,
        cache_misses=0,
        elapsed_s=0.0,
    )
    # The tags Claude Haiku wrote ([capture] tagger), kept beside the
    # store rather than in it: they land after the turn was parsed.
    haiku_tags.apply(corpus, store.config_dir)
    return corpus


__all__ = ["corpus_from_store"]

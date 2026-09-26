"""Tests for ``service.watcher.FileWatcher`` against synthetic transcript
trees under a temporary ``projects_root`` — no real ``~/.claude`` data.

``_STABLE_AGE_S``-backdated files (via ``os.utime``) are the default
posture for every test that isn't specifically exercising the live-file
window, so a slow CI box never flips a test's expectation by accident.
Liveness tests instead drive a :class:`_FakeClock` passed as the
``now`` constructor argument, so no test ever sleeps for real seconds.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from claudeglass import PARSER_VERSION, config, signals
from claudeglass.service.contracts import ServeOptions
from claudeglass.service.store import Store
from claudeglass.service.watcher import LIVE_FILE_WINDOW_S, LIVE_REPARSE_S, FileWatcher
from claudeglass.tools import log_usage

from helpers import assert_privacy, turn_line, write_jsonl

#: Comfortably outside the live window, for a file meant to look "stable"
#: (finished being written) from the very first tick that sees it.
_STABLE_AGE_S = LIVE_FILE_WINDOW_S * 10


class _FakeClock:
    """A settable ``now()`` callable, so a test can move the watcher's
    clock forward without a real ``time.sleep``."""

    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _backdated(seconds_ago: float) -> float:
    return time.time() - seconds_ago


def _write_session(root: Path, slug: str, session_id: str, lines: list[dict], age_s: float = _STABLE_AGE_S) -> Path:
    project_dir = root / slug
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / f"{session_id}.jsonl"
    write_jsonl(path, lines)
    mtime = _backdated(age_s)
    os.utime(path, (mtime, mtime))
    return path


def _write_subagent(
    root: Path,
    slug: str,
    session_id: str,
    agent_id: str,
    lines: list[dict],
    meta: dict | None = None,
    age_s: float = _STABLE_AGE_S,
) -> Path:
    subagents_dir = root / slug / session_id / "subagents"
    subagents_dir.mkdir(parents=True, exist_ok=True)
    path = subagents_dir / f"{agent_id}.jsonl"
    write_jsonl(path, lines)
    meta_path = subagents_dir / f"{agent_id}.meta.json"
    meta_path.write_text(json.dumps(meta or {"agentType": "claude-implementer"}), encoding="utf-8")
    mtime = _backdated(age_s)
    os.utime(path, (mtime, mtime))
    os.utime(meta_path, (mtime, mtime))
    return path


def _two_turns() -> list[dict]:
    return [
        turn_line(timestamp="2026-09-18T12:00:00.000Z", input_tokens=100, output_tokens=10),
        turn_line(timestamp="2026-09-18T12:05:00.000Z", input_tokens=120, output_tokens=12),
    ]


def _iso_ago(seconds_ago: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - seconds_ago))


def _two_turns_ago(seconds_ago: float) -> list[dict]:
    """Two replies, the last ``seconds_ago`` seconds ago."""
    return [
        turn_line(timestamp=_iso_ago(seconds_ago + 300), input_tokens=100, output_tokens=10),
        turn_line(timestamp=_iso_ago(seconds_ago), input_tokens=120, output_tokens=12),
    ]


@pytest.fixture
def store() -> Store:
    s = Store(":memory:")
    s.open()
    return s


def _options(tmp_path: Path, **overrides) -> ServeOptions:
    kwargs = {
        "projects_root": tmp_path / "projects",
        "config_dir": tmp_path / "config",
    }
    kwargs.update(overrides)
    return ServeOptions(**kwargs)


# -- basic corpus parse ----------------------------------------------------


def test_run_once_parses_synthetic_two_session_corpus(tmp_path: Path, store: Store):
    root = tmp_path / "projects"
    _write_session(root, "proj-a", "sess-a1", _two_turns())
    _write_session(root, "proj-b", "sess-b1", _two_turns() + [turn_line(timestamp="2026-09-18T12:10:00.000Z")])
    _write_subagent(root, "proj-b", "sess-b1", "agent-1", [turn_line(timestamp="2026-09-18T12:06:00.000Z")])

    options = _options(tmp_path)
    watcher = FileWatcher(store, options)
    stats = watcher.run_once()

    assert_privacy(stats)
    assert stats.errors == 0
    assert stats.files_scanned == 3
    assert stats.files_parsed == 3
    assert stats.files_skipped_live == 0
    assert stats.sessions_upserted == 2

    summary = store.summary()
    assert_privacy(summary)
    assert summary["sessions"] == 2
    assert summary["transcripts"] == 3

    session_a = store.session("sess-a1")
    assert session_a is not None
    assert len(session_a["transcripts"]) == 1
    session_b = store.session("sess-b1")
    assert session_b is not None
    assert len(session_b["transcripts"]) == 2
    kinds = {t["kind"] for t in session_b["transcripts"]}
    assert kinds == {"top-level", "subagent"}


# -- /api/summary windowing parity with the report's overview totals -------


def test_summary_windowing_matches_report_overview_totals(tmp_path: Path, store: Store):
    """``Store.summary(window_days=7)`` agrees exactly with the same
    window built the way the CLI ``report`` command does: a fresh
    ``corpus.load_corpus(project_dirs, days=7)`` fed through
    ``report.build_report``, read back from its "overview" section's
    "totals" table (``sessions``, ``top_level_transcripts`` +
    ``subagent_transcripts``). Both count a session by its last reply:
    one replied to recently (with a subagent) counts; one last replied
    to 40 days ago doesn't, even when its file changed ten minutes ago,
    as when Claude Code appends a title to an old transcript.
    """
    from claudeglass.config import Config
    from claudeglass.corpus import load_corpus
    from claudeglass.pricing import load_pricing
    from claudeglass.report import build_report

    root = tmp_path / "projects"
    project_dir = root / "proj-a"
    _write_session(root, "proj-a", "sess-recent", _two_turns_ago(900.0), age_s=600.0)
    _write_subagent(root, "proj-a", "sess-recent", "agent-1", [turn_line(timestamp=_iso_ago(800.0))], age_s=600.0)
    _write_session(root, "proj-a", "sess-old", _two_turns_ago(40 * 86400.0), age_s=40 * 86400.0)
    _write_session(root, "proj-a", "sess-touched", _two_turns_ago(40 * 86400.0), age_s=600.0)

    options = _options(tmp_path)
    watcher = FileWatcher(store, options)
    stats = watcher.run_once()
    assert stats.errors == 0

    summary = store.summary(window_days=7)
    assert_privacy(summary)

    fresh_corpus = load_corpus([project_dir], days=7)
    fresh_report = build_report(fresh_corpus, load_pricing(), Config(), projects=("proj-a",), window="last 7 days")
    totals = next(t for s in fresh_report.sections if s.key == "overview" for t in s.tables if t.name == "totals")
    overview = {row[0]: row[1] for row in totals.rows}

    assert summary["sessions"] == overview["sessions"] == 1
    assert summary["transcripts"] == overview["top_level_transcripts"] + overview["subagent_transcripts"] == 2


# -- baseline / profile ingestion -------------------------------------------


def _write_baseline_record(config_dir: Path, *, record_id: str, archetype: str | None = "exploratory", window_days: int | None = 7, created_at: str = "2026-09-18T12:00:00+00:00") -> Path:
    baselines_dir = config_dir / "baselines"
    baselines_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "id": record_id,
        "created_at": created_at,
        "window_days": window_days,
        "provisional": False,
        "sessions_analysed": 3,
        "mode_mix": {"interactive": 3},
        "dominant_purposes": ["implementation"],
        "archetype": archetype,
        "scorecard_overall": 72,
        "scorecard_label": "good",
        "suggested_profile": "implementation-heavy",
        "suggested_profile_reason": "test fixture",
        "projected_saving_usd": 1.5,
        "billing_mismatch_warning": None,
        "projects": ["proj-a"],
    }
    path = baselines_dir / f"{record_id}.json"
    path.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    return path


def test_scan_baselines_ingests_every_record_under_config_dir(tmp_path: Path, store: Store):
    options = _options(tmp_path)
    _write_baseline_record(options.config_dir, record_id="base-1")

    watcher = FileWatcher(store, options)
    stats = watcher.run_once()

    assert_privacy(stats)
    assert stats.errors == 0
    baselines = store.baselines()
    assert_privacy(baselines)
    assert len(baselines) == 1
    assert baselines[0]["archetype"] == "exploratory"
    assert baselines[0]["window_start"] == "2026-09-11T12:00:00+00:00"
    assert baselines[0]["window_end"] == "2026-09-18T12:00:00+00:00"


def test_scan_baselines_is_a_no_op_on_an_unchanged_repeat_tick(tmp_path: Path, store: Store):
    options = _options(tmp_path)
    _write_baseline_record(options.config_dir, record_id="base-1")

    watcher = FileWatcher(store, options)
    watcher.run_once()
    first = store.baselines()

    second_stats = watcher.run_once()
    second = store.baselines()

    assert_privacy(second_stats)
    assert len(second) == len(first) == 1
    assert second[0]["id"] == first[0]["id"]


def test_scan_baselines_updates_in_place_when_the_record_changes(tmp_path: Path, store: Store):
    options = _options(tmp_path)
    _write_baseline_record(options.config_dir, record_id="base-1", archetype="exploratory")

    watcher = FileWatcher(store, options)
    watcher.run_once()
    first = store.baselines()
    assert len(first) == 1

    _write_baseline_record(options.config_dir, record_id="base-1", archetype="deep-focus")
    watcher.run_once()
    second = store.baselines()

    assert len(second) == 1
    assert second[0]["id"] == first[0]["id"]
    assert second[0]["archetype"] == "deep-focus"


def _write_user_profile(config_dir: Path, *, profile_id: str, name: str = "My Profile") -> Path:
    from claudeglass.profiles.schema import Profile, dump_profile

    profiles_dir = config_dir / "profiles"
    profiles_dir.mkdir(parents=True, exist_ok=True)
    profile = Profile(id=profile_id, name=name, settings={"promptCacheTtl": "1h"})
    path = profiles_dir / f"{profile_id}.toml"
    path.write_text(dump_profile(profile), encoding="utf-8")
    return path


def test_scan_profiles_ingests_every_user_toml_under_config_dir(tmp_path: Path, store: Store):
    options = _options(tmp_path)
    _write_user_profile(options.config_dir, profile_id="my-profile", name="My Profile")

    watcher = FileWatcher(store, options)
    stats = watcher.run_once()

    assert_privacy(stats)
    assert stats.errors == 0
    profiles = store.profiles()
    assert_privacy(profiles)
    assert [p["id"] for p in profiles] == ["my-profile"]
    assert profiles[0]["name"] == "My Profile"


def test_scan_profiles_is_a_no_op_on_an_unchanged_repeat_tick(tmp_path: Path, store: Store):
    options = _options(tmp_path)
    _write_user_profile(options.config_dir, profile_id="my-profile")

    watcher = FileWatcher(store, options)
    watcher.run_once()
    first = store.profiles()

    watcher.run_once()
    second = store.profiles()

    assert len(second) == len(first) == 1
    assert second[0]["updated_at"] == first[0]["updated_at"]


def test_scan_profiles_re_ingests_when_the_file_changes(tmp_path: Path, store: Store):
    options = _options(tmp_path)
    _write_user_profile(options.config_dir, profile_id="my-profile", name="Original Name")

    watcher = FileWatcher(store, options)
    watcher.run_once()
    first = store.profiles()

    _write_user_profile(options.config_dir, profile_id="my-profile", name="Renamed")
    watcher.run_once()
    second = store.profiles()

    assert len(second) == 1
    assert second[0]["name"] == "Renamed"


def test_scan_profiles_never_ingests_a_catalogue_id(tmp_path: Path, store: Store):
    options = _options(tmp_path)
    _write_user_profile(options.config_dir, profile_id="interactive-chat", name="Shadow attempt")

    watcher = FileWatcher(store, options)
    stats = watcher.run_once()

    assert stats.errors == 0
    assert store.profiles() == []


# -- EST-P5: prediction-log.jsonl ingestion ---------------------------------


def test_scan_predictions_ingests_every_record_under_config_dir(tmp_path: Path, store: Store):
    from claudeglass import config as config_mod

    options = _options(tmp_path)
    config_mod.append_prediction_log(
        options.config_dir,
        source="whatif",
        measure_key="model",
        agent=None,
        predicted_usd=1.5,
        predicted_pct=None,
        fidelity="ceiling",
    )

    watcher = FileWatcher(store, options)
    stats = watcher.run_once()

    assert_privacy(stats)
    assert stats.errors == 0
    predictions = store.predictions()
    assert_privacy(predictions)
    assert len(predictions) == 1
    assert predictions[0]["source"] == "whatif"
    assert predictions[0]["measure_key"] == "model"
    assert predictions[0]["predicted_usd"] == 1.5


def test_scan_predictions_is_a_no_op_on_an_unchanged_repeat_tick(tmp_path: Path, store: Store):
    from claudeglass import config as config_mod

    options = _options(tmp_path)
    config_mod.append_prediction_log(
        options.config_dir,
        source="whatif",
        measure_key="model",
        agent=None,
        predicted_usd=1.5,
        predicted_pct=None,
        fidelity="ceiling",
    )

    watcher = FileWatcher(store, options)
    watcher.run_once()
    first = store.predictions()

    watcher.run_once()
    second = store.predictions()

    assert len(second) == len(first) == 1
    assert second[0]["id"] == first[0]["id"]


def test_scan_predictions_ingests_each_new_line_appended_later(tmp_path: Path, store: Store):
    from claudeglass import config as config_mod

    options = _options(tmp_path)
    config_mod.append_prediction_log(
        options.config_dir,
        source="whatif",
        measure_key="model",
        agent=None,
        predicted_usd=1.0,
        predicted_pct=None,
        fidelity="ceiling",
    )

    watcher = FileWatcher(store, options)
    watcher.run_once()
    assert len(store.predictions()) == 1

    config_mod.append_prediction_log(
        options.config_dir,
        source="whatif",
        measure_key="rebuild_share",
        agent="reviewer",
        predicted_usd=None,
        predicted_pct=-10.0,
        fidelity="simulated",
    )
    watcher.run_once()
    predictions = store.predictions()
    assert len(predictions) == 2
    assert {p["measure_key"] for p in predictions} == {"model", "rebuild_share"}


# -- incremental re-parse ---------------------------------------------------


def test_incremental_reparse_only_touches_the_changed_file(tmp_path: Path, store: Store):
    root = tmp_path / "projects"
    path_a = _write_session(root, "proj-a", "sess-a1", _two_turns())
    _write_session(root, "proj-b", "sess-b1", _two_turns())

    options = _options(tmp_path)
    watcher = FileWatcher(store, options)
    watcher.run_once()

    daily_before = {(r["day"], r["model"]): r["turns"] for r in store.daily_usage(days=3650)}
    assert sum(daily_before.values()) == 4  # 2 turns x 2 sessions

    # Grow session A's file with one more turn, still well outside the
    # live window (a different, later backdated mtime -- "changed").
    lines = _two_turns() + [turn_line(timestamp="2026-09-18T12:10:00.000Z")]
    write_jsonl(path_a, lines)
    new_mtime = _backdated(_STABLE_AGE_S - 1)  # still stable, but a different (newer) mtime than before
    os.utime(path_a, (new_mtime, new_mtime))

    stats2 = watcher.run_once()
    assert_privacy(stats2)
    assert stats2.errors == 0
    assert stats2.files_parsed == 1  # only session A's top-level file

    daily_after = {(r["day"], r["model"]): r["turns"] for r in store.daily_usage(days=3650)}
    assert sum(daily_after.values()) == 5  # A grew from 2 to 3 turns; B unchanged

    session_a = store.session("sess-a1")
    assert len(session_a["transcripts"]) == 1  # no duplicate row for the same path


def _settled_watcher(tmp_path: Path, store: Store) -> FileWatcher:
    """A watcher over one session with a subagent and a workflow run,
    after the tick that parses them and the tick that folds them from
    the stored digests (and remembers that it did)."""
    root = tmp_path / "projects"
    _write_session(root, "proj-a", "sess-a1", _two_turns())
    _write_subagent(root, "proj-a", "sess-a1", "agent-1", [turn_line(timestamp="2026-09-18T12:06:00.000Z")])
    _write_workflow(
        root, "proj-a", "sess-a1", "wf_test-002",
        {"runId": "wf_test-002", "agentCount": 0, "phases": [], "status": "completed"},
    )
    watcher = FileWatcher(store, _options(tmp_path))
    watcher.run_once()
    watcher.run_once()
    return watcher


def test_an_unchanged_session_is_not_read_or_folded_again(
    tmp_path: Path, store: Store, monkeypatch: pytest.MonkeyPatch
):
    watcher = _settled_watcher(tmp_path, store)
    token = store.change_token()
    loads = []
    monkeypatch.setattr(watcher, "_load_existing", lambda *args: loads.append(args))

    stats = watcher.run_once()

    assert stats.errors == 0
    assert stats.files_scanned == 3  # the session, its subagent and its workflow run
    assert stats.files_parsed == 0
    assert stats.sessions_upserted == 0
    assert loads == []
    assert store.change_token() == token
    assert store.session("sess-a1")["total_cost"] > 0


def test_a_changed_subagent_folds_its_session_again(tmp_path: Path, store: Store):
    watcher = _settled_watcher(tmp_path, store)
    cost_before = store.session("sess-a1")["total_cost"]
    agent_path = tmp_path / "projects" / "proj-a" / "sess-a1" / "subagents" / "agent-1.jsonl"
    write_jsonl(
        agent_path,
        [turn_line(timestamp="2026-09-18T12:06:00.000Z"), turn_line(timestamp="2026-09-18T12:07:00.000Z")],
    )
    mtime = _backdated(_STABLE_AGE_S - 1)
    os.utime(agent_path, (mtime, mtime))

    stats = watcher.run_once()

    assert stats.files_parsed == 1
    assert stats.sessions_upserted == 1
    assert store.session("sess-a1")["total_cost"] > cost_before


def test_a_removed_subagent_folds_its_session_again(tmp_path: Path, store: Store):
    watcher = _settled_watcher(tmp_path, store)
    cost_before = store.session("sess-a1")["total_cost"]
    (tmp_path / "projects" / "proj-a" / "sess-a1" / "subagents" / "agent-1.jsonl").unlink()

    stats = watcher.run_once()

    assert stats.sessions_upserted == 1
    assert store.session("sess-a1")["total_cost"] < cost_before


def test_a_new_snapshot_folds_settled_sessions_again(tmp_path: Path, store: Store):
    watcher = _settled_watcher(tmp_path, store)
    assert store.session("sess-a1")["profile_id"] is None
    snapshots_dir = tmp_path / "config" / "snapshots"
    snapshots_dir.mkdir(parents=True)
    (snapshots_dir / "20260918T110000Z.json").write_text(
        json.dumps({"ts": "20260918T110000Z", "schema_version": 2, "profile_id": "lean"}), encoding="utf-8"
    )

    stats = watcher.run_once()

    assert stats.sessions_upserted == 1
    assert store.session("sess-a1")["profile_id"] == "lean"


# -- stale parser_version forces a re-parse of an otherwise-unchanged file --


def test_stale_parser_version_forces_reparse_of_an_unchanged_file(tmp_path: Path, store: Store):
    """The confirmed bug: a transcript whose file hasn't changed since
    the last tick was never re-parsed even when its stored digest was
    produced under an older PARSER_VERSION, because _resolve/
    _needs_parse_this_tick only ever compared (mtime_ns, size_bytes).
    Force the stored row back to a stale parser_version without
    touching the file at all, then confirm the very next tick re-parses
    it anyway, bumps files_reparsed_stale_parser, and lands the row back
    on the current PARSER_VERSION."""
    root = tmp_path / "projects"
    path_a = _write_session(root, "proj-a", "sess-a1", _two_turns())

    options = _options(tmp_path)
    watcher = FileWatcher(store, options)
    stats1 = watcher.run_once()
    assert stats1.files_parsed == 1
    assert stats1.files_reparsed_stale_parser == 0
    assert store.known_files()[str(path_a)][2] == PARSER_VERSION

    # Roll the stored row back to an older parser_version, as if it had
    # been parsed by a since-upgraded build -- the file on disk is left
    # completely untouched (same mtime, same bytes).
    store._connection().execute(
        "UPDATE transcripts SET parser_version = ? WHERE path = ?",
        (PARSER_VERSION - 1, str(path_a)),
    )

    stats2 = watcher.run_once()
    assert_privacy(stats2)
    assert stats2.errors == 0
    assert stats2.files_parsed == 1
    assert stats2.files_skipped_live == 0
    assert stats2.files_reparsed_stale_parser == 1
    assert store.known_files()[str(path_a)][2] == PARSER_VERSION

    # Now up to date again (same mtime/size, current parser_version):
    # the very next tick must not re-parse it a second time.
    stats3 = watcher.run_once()
    assert stats3.files_parsed == 0
    assert stats3.files_reparsed_stale_parser == 0


def test_a_newer_parser_version_is_never_downgraded(tmp_path: Path, store: Store):
    """A digest written by a newer build (a service still running the old
    code after an upgrade, sharing the store with the new one) is left
    alone: re-parsing it here would throw away fields this build doesn't
    know, and the two builds would keep overwriting each other."""
    root = tmp_path / "projects"
    path_a = _write_session(root, "proj-a", "sess-a1", _two_turns())

    watcher = FileWatcher(store, _options(tmp_path))
    assert watcher.run_once().files_parsed == 1
    store._connection().execute(
        "UPDATE transcripts SET parser_version = ? WHERE path = ?",
        (PARSER_VERSION + 1, str(path_a)),
    )

    for _ in range(2):
        stats = watcher.run_once()
        assert stats.files_parsed == 0
        assert stats.files_reparsed_stale_parser == 0
    assert store.known_files()[str(path_a)][2] == PARSER_VERSION + 1


def test_up_to_date_parser_version_is_not_reparsed_on_an_unchanged_file(tmp_path: Path, store: Store):
    """The counterpart to the stale-parser test above: a row already at
    the current PARSER_VERSION with an unchanged file must never be
    counted as a stale-parser re-parse, on any number of repeat ticks."""
    root = tmp_path / "projects"
    _write_session(root, "proj-a", "sess-a1", _two_turns())

    options = _options(tmp_path)
    watcher = FileWatcher(store, options)
    stats1 = watcher.run_once()
    assert stats1.files_parsed == 1

    for _ in range(3):
        stats = watcher.run_once()
        assert stats.files_parsed == 0
        assert stats.files_reparsed_stale_parser == 0


# -- live-file skip / re-check next tick ------------------------------------


def test_live_file_is_skipped_then_reparsed_once_stable(tmp_path: Path, store: Store):
    root = tmp_path / "projects"
    path_a = _write_session(root, "proj-a", "sess-a1", _two_turns())

    clock = _FakeClock(time.time())
    options = _options(tmp_path)
    watcher = FileWatcher(store, options, now=clock)

    stats1 = watcher.run_once()
    assert stats1.files_parsed == 1
    assert stats1.files_skipped_live == 0

    # "Touch" the file (grow it, bump mtime to right now -- live) without
    # advancing the watcher's clock.
    live_mtime = time.time()
    write_jsonl(path_a, _two_turns() + [turn_line(timestamp="2026-09-18T12:10:00.000Z")])
    os.utime(path_a, (live_mtime, live_mtime))
    clock.value = live_mtime  # age 0s -- well inside the live window

    stats2 = watcher.run_once()
    assert_privacy(stats2)
    assert stats2.files_parsed == 0
    assert stats2.files_skipped_live == 1
    # Store still reflects the old (2-turn) content -- never touched.
    daily = {(r["day"], r["model"]): r["turns"] for r in store.daily_usage(days=3650)}
    assert sum(daily.values()) == 2

    # Advance the clock past the live window without touching the file
    # again -- it's re-checked (still "changed" per known_files()) and,
    # now stable, actually re-parsed.
    clock.value = live_mtime + LIVE_FILE_WINDOW_S + 1

    stats3 = watcher.run_once()
    assert stats3.files_parsed == 1
    assert stats3.files_skipped_live == 0
    daily_final = {(r["day"], r["model"]): r["turns"] for r in store.daily_usage(days=3650)}
    assert sum(daily_final.values()) == 3


def test_a_file_that_never_goes_quiet_is_still_reparsed(tmp_path: Path, store: Store):
    """A session written to more than once a minute is live on every tick.
    Waiting for it to go quiet froze its figures for as long as it ran
    (3.5 hours on a real cloud session), so it is parsed again once
    LIVE_REPARSE_S has passed since its last parse, live or not."""
    root = tmp_path / "projects"
    path_a = _write_session(root, "proj-a", "sess-a1", _two_turns())
    clock = _FakeClock(time.time())
    watcher = FileWatcher(store, _options(tmp_path), now=clock)
    assert watcher.run_once().files_parsed == 1
    parsed_at = clock.value

    def grow(turns: int) -> None:
        lines = _two_turns() + [turn_line(timestamp=f"2026-09-18T12:{10 + i:02d}:00.000Z") for i in range(turns)]
        write_jsonl(path_a, lines)
        os.utime(path_a, (clock.value, clock.value))

    # Written to every 20 seconds: live each tick, skipped until due.
    for step in (1, 2):
        clock.value = parsed_at + 20 * step
        grow(step)
        stats = watcher.run_once()
        assert (stats.files_parsed, stats.files_skipped_live) == (0, 1)

    clock.value = parsed_at + LIVE_REPARSE_S
    grow(3)
    stats = watcher.run_once()
    assert (stats.files_parsed, stats.files_skipped_live) == (1, 0)
    assert sum(r["turns"] for r in store.daily_usage(days=3650)) == 5


def test_never_seen_live_file_is_parsed_immediately(tmp_path: Path, store: Store):
    root = tmp_path / "projects"
    path_a = _write_session(root, "proj-a", "sess-a1", _two_turns(), age_s=0.0)

    now = time.time()
    clock = _FakeClock(now)
    options = _options(tmp_path)
    watcher = FileWatcher(store, options, now=clock)

    stats = watcher.run_once()
    assert stats.files_parsed == 1  # parsed despite being live, since never seen before
    assert stats.files_skipped_live == 0
    assert str(path_a) in watcher._pending_stabilize


# -- removed-file cleanup ----------------------------------------------------


def test_removed_file_cleanup(tmp_path: Path, store: Store):
    """A transcript whose file disappears is marked missing, not deleted
    (review finding 3: the store must be able to outlive Claude Code's
    own ``cleanupPeriodDays`` transcript retention) -- it keeps serving
    ``store.session()``/a rebuilt report until ``retention_prune()`` or
    ``--purge`` actually removes the row."""
    root = tmp_path / "projects"
    path_a = _write_session(root, "proj-a", "sess-a1", _two_turns())
    _write_session(root, "proj-b", "sess-b1", _two_turns())

    options = _options(tmp_path)
    watcher = FileWatcher(store, options)
    watcher.run_once()
    assert store.summary()["transcripts"] == 2
    assert store.count_missing_transcripts() == 0

    path_a.unlink()
    stats = watcher.run_once()
    assert_privacy(stats)
    assert stats.files_removed == 1  # newly marked missing this tick, not deleted
    assert stats.transcripts_missing == 1
    # The row (and its digest) is kept, not deleted.
    assert store.summary()["transcripts"] == 2
    assert store.count_missing_transcripts() == 1
    session_a = store.session("sess-a1")
    assert session_a is not None
    assert len(session_a["transcripts"]) == 1
    assert session_a["transcripts"][0]["kind"] == "top-level"

    # A further tick with the file still gone doesn't grow the marked
    # count again -- remove_missing()'s own newly-marked delta is 0.
    stats2 = watcher.run_once()
    assert stats2.files_removed == 0
    assert stats2.transcripts_missing == 1


def test_reappeared_file_clears_missing_since(tmp_path: Path, store: Store):
    """A file that comes back (e.g. a transient mount hiccup, or the
    watcher briefly racing a rewrite) has its ``missing_since`` cleared
    the next time it's seen -- ``remove_missing`` is not one-way."""
    root = tmp_path / "projects"
    path_a = _write_session(root, "proj-a", "sess-a1", _two_turns())

    options = _options(tmp_path)
    watcher = FileWatcher(store, options)
    watcher.run_once()

    original_bytes = path_a.read_bytes()
    path_a.unlink()
    watcher.run_once()
    assert store.count_missing_transcripts() == 1

    path_a.write_bytes(original_bytes)
    mtime = _backdated(_STABLE_AGE_S)
    os.utime(path_a, (mtime, mtime))
    watcher.run_once()
    assert store.count_missing_transcripts() == 0


# -- bad file never raises, never leaks a path ------------------------------


def test_bad_transcript_is_recorded_without_a_path(tmp_path: Path, store: Store, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "projects"
    bad_path = _write_session(root, "proj-a", "sess-bad", _two_turns())
    good_path = _write_session(root, "proj-b", "sess-good", _two_turns())

    import claudeglass.service.watcher as watcher_mod

    real_parse = watcher_mod.parse_transcript

    def _boom(path, meta):
        if str(path) == str(bad_path):
            raise ValueError("simulated corrupt transcript")
        return real_parse(path, meta)

    monkeypatch.setattr(watcher_mod, "parse_transcript", _boom)

    options = _options(tmp_path)
    watcher = FileWatcher(store, options)
    stats = watcher.run_once()

    assert stats.errors == 1
    assert len(stats.error_messages) == 1
    message = stats.error_messages[0]
    assert str(bad_path) not in message
    assert str(root) not in message
    assert "proj-a" not in message
    assert "sess-bad" not in message
    assert_privacy(stats)

    # The good session still got parsed and folded despite the other one
    # blowing up.
    assert store.session("sess-good") is not None
    assert store.session("sess-bad") is None


# -- snapshot ingestion -------------------------------------------------------


def _write_snapshot(config_dir: Path, ts: str, schema: int = 1) -> Path:
    snapshots_dir = config_dir / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    path = snapshots_dir / f"{ts}.json"
    data = {
        "schema": schema,
        "ts": ts,
        "session_id": "sess-x",
        "cwd_hash": "sha256:deadbeef",
        "user_settings": {"model": "claude-sonnet-5"},
        "managed_settings": {},
        "managed_keys": [],
        "project_settings": {},
        "mcp_servers": {"names": [], "enabled_mcpjson_servers": [], "disabled_mcpjson_servers": []},
        "enabled_plugins": [],
        "agents": {},
        "env_names": [],
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_snapshot_ingestion_is_deduped_across_ticks(tmp_path: Path, store: Store):
    options = _options(tmp_path)
    _write_snapshot(options.config_dir, "20260918T120000Z", schema=1)
    _write_snapshot(options.config_dir, "20260918T130000Z", schema=2)

    watcher = FileWatcher(store, options)
    watcher.run_once()

    rows = store.snapshots()
    assert_privacy(rows)
    assert len(rows) == 2
    schema_versions = sorted(r["schema_version"] for r in rows)
    assert schema_versions == [1, 2]
    # Snapshots carry no project slug of their own -- the watcher files
    # them under the global/machine-wide attribution, which Store.snapshots()
    # reports honestly as a null project_slug (deliverable 1.c).
    assert all(r["project_slug"] is None for r in rows)

    # A second tick over the same two files must not duplicate them.
    watcher.run_once()
    assert len(store.snapshots()) == 2



def test_stored_snapshot_keeps_the_fields_its_accessors_read(tmp_path: Path, store: Store):
    # api.py rebuilds a Snapshot from the stored digest; effective config,
    # managed keys and effective agents must survive the round trip.
    options = _options(tmp_path)
    path = _write_snapshot(options.config_dir, "20260918T130000Z", schema=2)
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(effective={"effortLevel": "high"}, managed_keys=["model"], effective_agents={"rev": {"effort": "low"}})
    path.write_text(json.dumps(data), encoding="utf-8")
    FileWatcher(store, options).run_once()

    from claudeglass import snapshots as snapshots_mod
    from claudeglass.snapshots import Snapshot

    stored = json.loads(store.snapshots()[0]["digest_json"])
    snap = Snapshot(path=Path(""), ts="20260918T130000Z", data=stored)
    assert snapshots_mod.effective_config(snap) == {"effortLevel": "high"}
    assert snapshots_mod.managed_keys(snap) == ["model"]
    assert stored["effective_agents"] == {"rev": {"effort": "low"}}


# -- billing_mode wiring (deliverable 1.a) -----------------------------------


def test_billing_mode_is_stamped_onto_sessions_from_options(tmp_path: Path, store: Store):
    root = tmp_path / "projects"
    _write_session(root, "proj-a", "sess-a1", _two_turns())

    options = _options(tmp_path, billing_mode="subscription")
    watcher = FileWatcher(store, options)
    watcher.run_once()

    row = store._connection().execute(
        "SELECT billing_mode FROM sessions WHERE id = ?", ("sess-a1",)
    ).fetchone()
    assert row["billing_mode"] == "subscription"


def test_billing_mode_defaults_to_api(tmp_path: Path, store: Store):
    root = tmp_path / "projects"
    _write_session(root, "proj-a", "sess-a1", _two_turns())

    options = _options(tmp_path)
    watcher = FileWatcher(store, options)
    watcher.run_once()

    row = store._connection().execute(
        "SELECT billing_mode FROM sessions WHERE id = ?", ("sess-a1",)
    ).fetchone()
    assert row["billing_mode"] == "api"


# -- real-time tag overrides (S1-perf item 6) ---------------------------------


def test_session_tag_override_is_applied_on_the_next_tick(tmp_path: Path, store: Store):
    """A ``POST /api/sessions/<id>/tags`` write (``Store.set_tag``) must
    change ``sessions.mode``/``purpose`` on the very next watcher tick,
    not only the next time a report is built -- ``api.py``'s
    ``_build_report_model`` already merged ``store.all_tags()`` into its
    own overrides, but ``_fold_session`` was still classifying every
    session with an empty override mapping, so ``/api/sessions`` kept
    showing the pre-override classification until a full store rebuild.
    Nothing on disk changes between the ticks: the session is settled
    (folded from its stored digests and remembered as such), so only the
    tag, which is part of its fingerprint, makes the tick fold it again.
    """
    root = tmp_path / "projects"
    _write_session(root, "proj-a", "sess-a1", _two_turns())

    options = _options(tmp_path)
    watcher = FileWatcher(store, options)
    watcher.run_once()
    watcher.run_once()

    before = store.session("sess-a1")
    assert before is not None
    assert before["mode_source"] != "override"
    assert before["purpose_source"] != "override"

    store.set_tag("sess-a1", "mode", "manual-override-mode")
    store.set_tag("sess-a1", "purpose", "manual-override-purpose")

    watcher.run_once()

    after = store.session("sess-a1")
    assert after is not None
    assert after["mode"] == "manual-override-mode"
    assert after["mode_source"] == "override"
    assert after["purpose"] == "manual-override-purpose"
    assert after["purpose_source"] == "override"


# -- last_stats (deliverable 1.e) ---------------------------------------------


def test_last_stats_is_none_before_run_once_and_set_after(tmp_path: Path, store: Store):
    root = tmp_path / "projects"
    _write_session(root, "proj-a", "sess-a1", _two_turns())

    options = _options(tmp_path)
    watcher = FileWatcher(store, options)
    assert watcher.last_stats is None

    stats = watcher.run_once()
    assert watcher.last_stats is stats
    assert watcher.last_stats.files_parsed == 1

    stats2 = watcher.run_once()
    assert watcher.last_stats is stats2
    assert watcher.last_stats is not stats


# -- workflow run persistence (deliverable 1.d) -------------------------------


def _write_workflow(
    root: Path,
    slug: str,
    session_id: str,
    run_id: str,
    body: dict,
    agent_lines: list[dict] | None = None,
) -> Path:
    workflows_dir = root / slug / session_id / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)
    run_path = workflows_dir / f"{run_id}.json"
    run_path.write_text(json.dumps(body), encoding="utf-8")
    mtime = _backdated(_STABLE_AGE_S)
    os.utime(run_path, (mtime, mtime))
    if agent_lines is not None:
        agent_dir = root / slug / session_id / "subagents" / "workflows" / run_id
        agent_dir.mkdir(parents=True, exist_ok=True)
        agent_path = agent_dir / "agent-1.jsonl"
        write_jsonl(agent_path, agent_lines)
        (agent_dir / "agent-1.meta.json").write_text(
            json.dumps({"agentType": "claude-implementer"}), encoding="utf-8"
        )
        os.utime(agent_path, (mtime, mtime))
        os.utime(agent_dir / "agent-1.meta.json", (mtime, mtime))
    return run_path


def test_workflow_run_is_parsed_and_persisted(tmp_path: Path, store: Store):
    root = tmp_path / "projects"
    _write_session(root, "proj-a", "sess-a1", _two_turns())
    _write_workflow(
        root,
        "proj-a",
        "sess-a1",
        "wf_test-000",
        {
            "runId": "wf_test-000",
            "timestamp": "2026-09-18T12:00:00.000Z",
            "agentCount": 1,
            "phases": [{"title": "Only phase", "detail": "never read"}],
            "status": "completed",
        },
        agent_lines=[turn_line(timestamp="2026-09-18T12:00:10.000Z", input_tokens=50, output_tokens=5)],
    )

    options = _options(tmp_path)
    watcher = FileWatcher(store, options)
    stats = watcher.run_once()
    assert_privacy(stats)
    assert stats.errors == 0

    row = store._connection().execute(
        "SELECT session_id, run_id, agent_count, phases, started, status FROM workflow_runs"
    ).fetchone()
    assert row is not None
    assert row["session_id"] == "sess-a1"
    assert row["run_id"] == "wf_test-000"
    assert row["agent_count"] == 1
    assert json.loads(row["phases"]) == ["Only phase"]
    assert row["started"] == "2026-09-18T12:00:00.000Z"
    assert row["status"] == "completed"


def test_malformed_workflow_file_never_raises_and_still_upserts(tmp_path: Path, store: Store):
    root = tmp_path / "projects"
    _write_session(root, "proj-a", "sess-a1", _two_turns())
    # Genuinely invalid JSON -- parse_workflow_file's own documented
    # tolerant-parsing posture (never raises) is what's under test here,
    # not the watcher's own try/except.
    workflows_dir = root / "proj-a" / "sess-a1" / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)
    bad_path = workflows_dir / "wf_bad-000.json"
    bad_path.write_text("{not valid json", encoding="utf-8")
    mtime = _backdated(_STABLE_AGE_S)
    os.utime(bad_path, (mtime, mtime))

    options = _options(tmp_path)
    watcher = FileWatcher(store, options)
    stats = watcher.run_once()
    assert_privacy(stats)
    assert stats.errors == 0

    row = store._connection().execute(
        "SELECT run_id, agent_count, phases, status FROM workflow_runs"
    ).fetchone()
    assert row is not None
    assert row["run_id"] == "wf_bad-000"  # falls back to the filename stem
    assert row["agent_count"] == 0
    assert json.loads(row["phases"]) == []
    assert row["status"] is None


def test_workflow_run_is_deduped_across_ticks(tmp_path: Path, store: Store):
    root = tmp_path / "projects"
    _write_session(root, "proj-a", "sess-a1", _two_turns())
    _write_workflow(
        root,
        "proj-a",
        "sess-a1",
        "wf_test-001",
        {"runId": "wf_test-001", "agentCount": 0, "phases": [], "status": "completed"},
    )

    options = _options(tmp_path)
    watcher = FileWatcher(store, options)
    watcher.run_once()
    watcher.run_once()

    count = store._connection().execute("SELECT COUNT(*) FROM workflow_runs").fetchone()[0]
    assert count == 1


# -- start()/stop() lifecycle -------------------------------------------------


def test_start_twice_starts_only_one_thread(tmp_path: Path):
    db_path = tmp_path / "store.db"
    store_obj = Store(str(db_path))
    store_obj.open()

    options = _options(tmp_path, poll_interval_s=0.05)
    watcher = FileWatcher(store_obj, options)

    before = {t.ident for t in threading.enumerate()}
    watcher.start()
    first_thread = watcher._thread
    watcher.start()  # idempotent
    second_thread = watcher._thread

    assert first_thread is second_thread
    watcher_threads = [t for t in threading.enumerate() if t.name == "claudeglass-watcher"]
    assert len(watcher_threads) == 1

    watcher.stop()
    assert watcher._thread is None
    after = {t.ident for t in threading.enumerate()}
    assert after == before

    store_obj.close()


__all__: list[str] = []


def test_sessions_carry_the_profile_active_at_their_start(tmp_path: Path, store: Store):
    root = tmp_path / "projects"
    _write_session(root, "proj-a", "sess-a1", _two_turns())
    options = _options(tmp_path)
    snapshots_dir = options.config_dir / "snapshots"
    snapshots_dir.mkdir(parents=True)
    (snapshots_dir / "20260918T110000Z.json").write_text(
        json.dumps({"ts": "20260918T110000Z", "schema_version": 2, "profile_id": "lean"}), encoding="utf-8"
    )

    FileWatcher(store, options).run_once()

    assert store.session("sess-a1")["profile_id"] == "lean"
    assert [s["profile_id"] for s in store.sessions()] == ["lean"]


# -- several folders of projects (WSL) --------------------------------------


def test_extra_projects_roots_are_scanned_too(tmp_path: Path, store: Store):
    _write_session(tmp_path / "projects", "proj-a", "sess-a1", _two_turns())
    _write_session(tmp_path / "wsl-projects", "-home-alice-repo", "sess-w1", _two_turns())

    watcher = FileWatcher(store, _options(tmp_path, extra_projects_roots=(tmp_path / "wsl-projects",)))
    stats = watcher.run_once()

    assert stats.sessions_upserted == 2
    assert {row["id"] for row in store.sessions()} == {"sess-a1", "sess-w1"}
    assert all(row["source"] == "This computer" for row in store.sessions())


def test_an_unreachable_extra_root_does_not_mark_its_sessions_missing(tmp_path: Path, store: Store):
    """A WSL distro that shuts down takes its folder away for a while;
    its sessions must not be marked missing on the strength of that."""
    _write_session(tmp_path / "projects", "proj-a", "sess-a1", _two_turns())
    wsl_root = tmp_path / "wsl-projects"
    _write_session(wsl_root, "-home-alice-repo", "sess-w1", _two_turns())
    watcher = FileWatcher(store, _options(tmp_path, extra_projects_roots=(wsl_root,)))
    watcher.run_once()

    wsl_root.rename(tmp_path / "wsl-away")
    stats = watcher.run_once()

    assert_privacy(stats)
    assert stats.files_removed == 0
    assert store.count_missing_transcripts() == 0
    assert any("not reachable" in message for message in stats.error_messages)


# -- a failing tick never kills the scanner -----------------------------------


def test_a_store_that_cannot_open_fails_the_tick_not_the_watcher(
    tmp_path: Path, store: Store, monkeypatch: pytest.MonkeyPatch
):
    import sqlite3

    _write_session(tmp_path / "projects", "proj-a", "sess-a1", _two_turns())
    watcher = FileWatcher(store, _options(tmp_path))
    real_open = store.open
    calls = {"n": 0}

    def _locked_once():
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        real_open()

    monkeypatch.setattr(store, "open", _locked_once)

    failed = watcher.run_once()
    assert failed.error_messages == ("tick failed: OperationalError: database is locked",)
    state = watcher.state()
    assert state.last_tick_failed is True
    assert state.last_success_at is None
    assert state.scanning is False

    ok = watcher.run_once()
    assert ok.errors == 0
    state = watcher.state()
    assert state.last_tick_failed is False
    assert state.last_success_at == ok.finished_at
    assert store.session("sess-a1") is not None


def test_the_background_thread_outlives_failing_ticks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import sqlite3

    store_obj = Store(str(tmp_path / "store.db"))
    watcher = FileWatcher(store_obj, _options(tmp_path, poll_interval_s=0.01))
    real_open = store_obj.open
    calls = {"n": 0}

    def _locked_twice():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise sqlite3.OperationalError("database is locked")
        real_open()

    monkeypatch.setattr(store_obj, "open", _locked_twice)
    watcher.start()
    try:
        deadline = time.monotonic() + 10
        while watcher.state().last_success_at is None and time.monotonic() < deadline:
            time.sleep(0.01)
        state = watcher.state()
        assert state.running is True
        assert state.last_success_at is not None
    finally:
        watcher.stop()
    assert watcher.state().running is False


def test_after_tick_runs_after_each_background_tick_and_may_fail(tmp_path: Path):
    """``serve`` checks for code changed on disk here; a hook that raises
    must not stop the scans."""
    store_obj = Store(str(tmp_path / "store.db"))
    ticks: list[str | None] = []

    def _after_tick():
        ticks.append(watcher.state().last_success_at)
        raise RuntimeError("the hook failed")

    watcher = FileWatcher(store_obj, _options(tmp_path, poll_interval_s=0.01), after_tick=_after_tick)
    # run_once (serve --once, tests) is not a background tick.
    watcher.run_once()
    assert ticks == []
    watcher.start()
    try:
        deadline = time.monotonic() + 10
        while len(ticks) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(ticks) >= 3
        # Called after the tick finished, never before the first one.
        assert ticks[0] is not None
        assert watcher.state().running is True
    finally:
        watcher.stop()
        store_obj.close()


def test_state_reports_progress_while_storing(tmp_path: Path, store: Store, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "projects"
    _write_session(root, "proj-a", "sess-a1", _two_turns())
    _write_session(root, "proj-b", "sess-b1", _two_turns())
    watcher = FileWatcher(store, _options(tmp_path))
    seen = []
    real_scan = watcher._scan_session

    def _spy(*args, **kwargs):
        seen.append(watcher.state())
        return real_scan(*args, **kwargs)

    monkeypatch.setattr(watcher, "_scan_session", _spy)
    watcher.run_once()

    assert [(s.scanning, s.phase, s.done, s.total) for s in seen] == [
        (True, "storing", 0, 2),
        (True, "storing", 1, 2),
    ]
    assert seen[0].scan_started_at is not None
    idle = watcher.state()
    assert (idle.scanning, idle.phase, idle.done, idle.total) == (False, None, 0, 0)


def test_error_summary_keeps_sqlite_text_but_never_an_os_error_path(tmp_path: Path):
    import sqlite3

    from claudeglass.service.watcher import _error_summary

    assert _error_summary(sqlite3.OperationalError("database is locked")) == "OperationalError: database is locked"
    assert _error_summary(sqlite3.OperationalError("")) == "OperationalError"
    secret = str(tmp_path / "proj-secret" / "sess.jsonl")
    assert _error_summary(FileNotFoundError(2, "No such file", secret)) == "FileNotFoundError"
    assert _error_summary(ValueError(secret)) == "ValueError"


def test_a_large_tick_reports_finding_then_reading_then_storing(
    tmp_path: Path, store: Store, monkeypatch: pytest.MonkeyPatch
):
    from claudeglass.cache import DigestCache
    import claudeglass.service.watcher as watcher_mod

    root = tmp_path / "projects"
    _write_session(root, "proj-a", "sess-a1", _two_turns())
    _write_session(root, "proj-a", "sess-a2", _two_turns())
    _write_subagent(root, "proj-a", "sess-a1", "agent-1", _two_turns())
    monkeypatch.setattr(watcher_mod, "_PARALLEL_PARSE_THRESHOLD", 0)
    monkeypatch.setattr(watcher_mod.os, "cpu_count", lambda: 1)  # no process pool in a unit test
    watcher = FileWatcher(store, _options(tmp_path), cache=DigestCache(tmp_path / "config"))
    phases = []
    real_set = watcher._set_progress

    def _spy(phase, total=0):
        phases.append((phase, total))
        real_set(phase, total)

    monkeypatch.setattr(watcher, "_set_progress", _spy)
    watcher.run_once()

    assert phases == [("finding", 0), ("reading", 3), ("storing", 2), (None, 0)]


# -- SEC-P8/G7: signal/capture-log pruning is unconditional -----------------


def _write_signal_month_file(config_dir: Path, year: int, month: int) -> Path:
    signals.signals_dir(config_dir).mkdir(parents=True, exist_ok=True)
    path = signals.signals_dir(config_dir) / f"{year:04d}-{month:02d}.jsonl"
    path.write_text("", encoding="utf-8")
    return path


def _write_capture_log(config_dir: Path, *timestamps: datetime) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({"ts": ts.isoformat(timespec="seconds"), "level": "essentials", "changed": {}}, sort_keys=True)
        for ts in timestamps
    ]
    (config_dir / config.CAPTURE_LOG_NAME).write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_run_once_prunes_old_signals_and_capture_log_with_no_retention_days_set(tmp_path: Path, store: Store):
    # Previously this whole block (store rows, signal files, capture-log)
    # was skipped entirely unless the user set retention_days -- G7 found
    # capture-log.jsonl (and, the same way, signal files) grew forever by
    # default. Now the telemetry files are pruned unconditionally, at the
    # 180-day default, even with retention_days left at None.
    options = _options(tmp_path)
    old_signal = _write_signal_month_file(options.config_dir, 2020, 1)
    recent_signal = _write_signal_month_file(
        options.config_dir, datetime.now(timezone.utc).year, datetime.now(timezone.utc).month
    )
    now = datetime.now(timezone.utc)
    _write_capture_log(options.config_dir, now - timedelta(days=200), now - timedelta(days=5))

    watcher = FileWatcher(store, options)
    watcher.run_once()

    assert not old_signal.exists()
    assert recent_signal.exists()
    log = config.load_capture_log(options.config_dir)
    assert len(log) == 1
    assert log[0]["ts"] == (now - timedelta(days=5)).isoformat(timespec="seconds")


def test_run_once_uses_an_explicit_retention_days_for_signals_and_capture_log(tmp_path: Path, store: Store):
    # An explicit retention_days narrower than the 180-day default must
    # still reach the signal/capture-log path, not just store rows.
    options = _options(tmp_path, retention_days=30)
    old_signal = _write_signal_month_file(options.config_dir, 2020, 1)
    now = datetime.now(timezone.utc)
    _write_capture_log(options.config_dir, now - timedelta(days=60), now - timedelta(days=1))

    watcher = FileWatcher(store, options)
    watcher.run_once()

    assert not old_signal.exists()
    log = config.load_capture_log(options.config_dir)
    assert len(log) == 1
    assert log[0]["ts"] == (now - timedelta(days=1)).isoformat(timespec="seconds")


def test_run_once_prunes_old_usage_log_rows_with_no_retention_days_set(tmp_path: Path, store: Store):
    # SIG-5: usage-log.csv is written unconditionally on every statusline
    # refresh (capture on or off), so -- like signals/capture-log above --
    # it must be pruned on every tick even when nobody set retention_days.
    options = _options(tmp_path)
    csv_path = log_usage.default_usage_log_path(options.config_dir)
    now = datetime.now(timezone.utc)
    old = {"session_id": "old", "window": "five_hour", "used_percentage": 1.0, "resets_at": "r"}
    recent = {"session_id": "recent", "window": "five_hour", "used_percentage": 2.0, "resets_at": "r"}
    log_usage.append_rows(csv_path, [old], now=now - timedelta(days=200))
    log_usage.append_rows(csv_path, [recent], now=now - timedelta(days=5))

    watcher = FileWatcher(store, options)
    watcher.run_once()

    rows = log_usage.load_usage_log(csv_path)
    assert len(rows) == 1
    assert rows[0]["session_id"] == "recent"


def test_run_once_uses_an_explicit_retention_days_for_usage_log(tmp_path: Path, store: Store):
    options = _options(tmp_path, retention_days=30)
    csv_path = log_usage.default_usage_log_path(options.config_dir)
    now = datetime.now(timezone.utc)
    old = {"session_id": "old", "window": "five_hour", "used_percentage": 1.0, "resets_at": "r"}
    recent = {"session_id": "recent", "window": "five_hour", "used_percentage": 2.0, "resets_at": "r"}
    log_usage.append_rows(csv_path, [old], now=now - timedelta(days=60))
    log_usage.append_rows(csv_path, [recent], now=now - timedelta(days=1))

    watcher = FileWatcher(store, options)
    watcher.run_once()

    rows = log_usage.load_usage_log(csv_path)
    assert len(rows) == 1
    assert rows[0]["session_id"] == "recent"

"""Tests for V3-compare's ``compare.py`` (``claudeglass compare``):
``parse_arm_spec``'s four spec forms and their error paths, the
``compare()`` entry point's overview/stratum/co-changed tables against
synthetic corpora built with ``tests/helpers``, the minimum-sample gate
(``sample_ok``), the ``profile:`` arm's join to the profile history,
and the CLI wiring (``cli.main(["compare", ...])`` exit codes and
output). Every returned ``Section`` is also run through
``tests.helpers.assert_privacy``, matching this codebase's existing
privacy-invariant testing convention (see ``tests/test_privacy.py``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claudeglass import cli
from claudeglass import compare as compare_mod
from claudeglass import snapshots as snapshots_mod
from claudeglass.config import Config
from claudeglass.corpus import load_corpus
from claudeglass.pricing import load_pricing

from helpers import assert_privacy, attachment_line, turn_line, write_jsonl

PRICING = load_pricing()
CONFIG = Config()


def _write_session(project_dir: Path, session_id: str, timestamp: str, **overrides) -> Path:
    path = project_dir / f"{session_id}.jsonl"
    write_jsonl(path, [turn_line(timestamp=timestamp, **overrides)])
    return path


def _session_id_by_ts(corpus, ts: str) -> str:
    """Find a built corpus's session id for the session whose first turn
    has timestamp ``ts`` -- used to key ``session_overrides`` correctly
    without hardcoding the filename-stem-as-session-id convention twice.
    """
    for bundle in corpus.sessions:
        if bundle.top is not None and bundle.top.turns and bundle.top.turns[0].ts == ts:
            return bundle.session_id
    raise AssertionError(f"no session with first turn ts={ts!r}")


def _table(section, name: str):
    for table in section.tables:
        if table.name == name:
            return table
    raise AssertionError(f"no table named {name!r} in section {section.key!r}")


# -- parse_arm_spec: happy paths ---------------------------------------------


def test_parse_arm_spec_window_both_dates():
    spec = compare_mod.parse_arm_spec("window:2026-08-01..2026-08-31")
    assert spec.kind == "window"
    assert spec.label == "window:2026-08-01..2026-08-31"
    assert spec.since == "2026-08-01"
    assert spec.until == "2026-08-31"
    assert spec.since_dt is not None and spec.until_dt is not None
    assert spec.since_dt < spec.until_dt


def test_parse_arm_spec_window_since_only():
    spec = compare_mod.parse_arm_spec("window:2026-08-01..")
    assert spec.since == "2026-08-01"
    assert spec.until is None
    assert spec.since_dt is not None
    assert spec.until_dt is None


def test_parse_arm_spec_window_until_only():
    spec = compare_mod.parse_arm_spec("window:..2026-08-31")
    assert spec.since is None
    assert spec.until == "2026-08-31"
    assert spec.since_dt is None
    assert spec.until_dt is not None


def test_parse_arm_spec_key():
    spec = compare_mod.parse_arm_spec("key:user_settings.autoCompactWindow=5")
    assert spec.kind == "key"
    assert spec.key == "user_settings.autoCompactWindow"
    assert spec.value == "5"


def test_parse_arm_spec_profile():
    spec = compare_mod.parse_arm_spec("profile:default")
    assert spec.kind == "profile"
    assert spec.profile_id == "default"


def test_parse_arm_spec_project_single():
    spec = compare_mod.parse_arm_spec("project:proj-a")
    assert spec.kind == "project"
    assert spec.projects == ("proj-a",)


def test_parse_arm_spec_project_multiple():
    spec = compare_mod.parse_arm_spec("project:proj-a,proj-b")
    assert spec.projects == ("proj-a", "proj-b")


def test_parse_arm_spec_project_slug_is_redacted():
    spec = compare_mod.parse_arm_spec("project:Users-alice-repo")
    assert spec.projects == ("Users-<user>-repo",)


# -- parse_arm_spec: error paths ---------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "no-colon-at-all",
        "window:2026-08-01",  # missing ".."
        "window:..",  # both sides empty
        "window:2026-13-40..2026-08-31",  # bad date
        "key:no-equals-sign",
        "key:=value",  # empty key
        "key:name=",  # empty value
        "profile:",
        "project:",
        "project: , ,",
        "bogus:whatever",
    ],
)
def test_parse_arm_spec_rejects_malformed(bad):
    with pytest.raises(ValueError):
        compare_mod.parse_arm_spec(bad)


# -- compare(): overview + sample_ok ------------------------------------------


def test_compare_window_arms_basic_overview(tmp_path):
    root = tmp_path / "projects"
    project_dir = root / "proj"
    project_dir.mkdir(parents=True)
    _write_session(
        project_dir,
        "aug-session",
        "2026-08-15T10:00:00.000Z",
        input_tokens=1000,
        cache_creation_input_tokens=200,
        cache_read_input_tokens=50,
        output_tokens=100,
    )
    _write_session(
        project_dir,
        "sep-session",
        "2026-09-05T10:00:00.000Z",
        input_tokens=2000,
        cache_creation_input_tokens=400,
        cache_read_input_tokens=100,
        output_tokens=200,
    )
    corpus = load_corpus([project_dir])

    arm_a = compare_mod.parse_arm_spec("window:2026-08-01..2026-08-31")
    arm_b = compare_mod.parse_arm_spec("window:2026-09-01..2026-09-30")
    section = compare_mod.compare(corpus, PRICING, CONFIG, arm_a=arm_a, arm_b=arm_b, min_sessions=1)

    assert section.key == "compare"
    overview = _table(section, "compare_overview")
    rows_by_metric = {row[0]: row for row in overview.rows}

    sessions_row = rows_by_metric["Sessions"]
    assert sessions_row[1] == "1" and sessions_row[2] == "1"
    assert sessions_row[5] == "yes"  # sample_ok, min_sessions=1 and both arms have 1

    # One session per arm, so the per-session mean equals the arm total.
    new_tokens_row = rows_by_metric["New tokens per session (input + cache-creation)"]
    # Arm A: 1000 + 200 = 1200; Arm B: 2000 + 400 = 2400 -> +100.0%
    assert "1,200" in new_tokens_row[1] or "1200" in new_tokens_row[1]
    assert "+100.0%" == new_tokens_row[4]

    # The raw arm totals are still reported, separately and clearly labelled.
    total_new_tokens_row = rows_by_metric["Total new tokens (informational)"]
    assert "1,200" in total_new_tokens_row[1] or "1200" in total_new_tokens_row[1]
    assert "+100.0%" == total_new_tokens_row[4]

    # Every arm's exact selection rule must be reproduced in the notes.
    assert any("window:2026-08-01..2026-08-31" in n for n in overview.notes)
    assert any("window:2026-09-01..2026-09-30" in n for n in overview.notes)
    assert any("observed, not controlled" in n.lower() for n in overview.notes)


def test_compare_overview_sample_ok_no_below_min_sessions(tmp_path):
    root = tmp_path / "projects"
    project_dir = root / "proj"
    project_dir.mkdir(parents=True)
    _write_session(project_dir, "aug-session", "2026-08-15T10:00:00.000Z")
    _write_session(project_dir, "sep-session", "2026-09-05T10:00:00.000Z")
    corpus = load_corpus([project_dir])

    arm_a = compare_mod.parse_arm_spec("window:2026-08-01..2026-08-31")
    arm_b = compare_mod.parse_arm_spec("window:2026-09-01..2026-09-30")
    # min_sessions=5, but each arm only has 1 session -> sample_ok "no",
    # yet the full table must still be produced (never raises, never
    # suppresses the overview itself).
    section = compare_mod.compare(corpus, PRICING, CONFIG, arm_a=arm_a, arm_b=arm_b, min_sessions=5)

    overview = _table(section, "compare_overview")
    for row in overview.rows:
        assert row[5] == "no"
    assert "Arm A=1 session(s), Arm B=1 session(s)" in overview.notes[-1]


def test_compare_overview_means_are_unaffected_by_arm_size(tmp_path):
    """Review finding S4 regression: arm A has 5 sessions and arm B has 10
    *identical* sessions (same per-session token/cost shape). Before the
    fix, ``cost``/``new_tokens``/``priced_turns`` were arm totals, so arm
    B's headline numbers looked 100% higher purely because it has twice
    the sessions -- not because anything about the work differed. The
    per-session-mean rows must show a 0% delta; only the informational
    totals rows are allowed to show the arm-size-driven +100%.
    """
    root = tmp_path / "projects"
    project_dir = root / "proj"
    project_dir.mkdir(parents=True)
    for i in range(5):
        _write_session(
            project_dir,
            f"aug-{i}",
            f"2026-08-{10 + i:02d}T10:00:00.000Z",
            input_tokens=1000,
            cache_creation_input_tokens=200,
            cache_read_input_tokens=50,
            output_tokens=100,
        )
    for i in range(10):
        _write_session(
            project_dir,
            f"sep-{i}",
            f"2026-09-{1 + i:02d}T10:00:00.000Z",
            input_tokens=1000,
            cache_creation_input_tokens=200,
            cache_read_input_tokens=50,
            output_tokens=100,
        )
    corpus = load_corpus([project_dir])

    arm_a = compare_mod.parse_arm_spec("window:2026-08-01..2026-08-31")
    arm_b = compare_mod.parse_arm_spec("window:2026-09-01..2026-09-30")
    section = compare_mod.compare(corpus, PRICING, CONFIG, arm_a=arm_a, arm_b=arm_b, min_sessions=1)

    overview = _table(section, "compare_overview")
    rows_by_metric = {row[0]: row for row in overview.rows}

    sessions_row = rows_by_metric["Sessions"]
    assert sessions_row[1] == "5" and sessions_row[2] == "10"

    for label in (
        "Cost per session",
        "New tokens per session (input + cache-creation)",
        "Priced turns per session",
    ):
        row = rows_by_metric[label]
        # Identical per-session shape in both arms -> a flat 0% delta,
        # regardless of arm A having 5 sessions and arm B having 10.
        # (Floating-point division can land on -0.0 as well as 0.0.)
        assert row[4] in ("0.0%", "-0.0%"), f"{label}: expected a 0% delta, got {row[4]!r}"

    for label in (
        "Total cost (informational)",
        "Total new tokens (informational)",
        "Total priced turns (informational)",
    ):
        row = rows_by_metric[label]
        assert row[4] == "+100.0%", f"{label}: expected the totals to still show arm-size-driven +100%, got {row[4]!r}"


def test_compare_window_arm_excludes_out_of_range_session(tmp_path):
    root = tmp_path / "projects"
    project_dir = root / "proj"
    project_dir.mkdir(parents=True)
    _write_session(project_dir, "aug-session", "2026-08-15T10:00:00.000Z")
    _write_session(project_dir, "oct-session", "2026-10-05T10:00:00.000Z")
    corpus = load_corpus([project_dir])

    arm_a = compare_mod.parse_arm_spec("window:2026-08-01..2026-08-31")
    arm_b = compare_mod.parse_arm_spec("window:2026-09-01..2026-09-30")
    section = compare_mod.compare(corpus, PRICING, CONFIG, arm_a=arm_a, arm_b=arm_b, min_sessions=1)

    overview = _table(section, "compare_overview")
    rows_by_metric = {row[0]: row for row in overview.rows}
    sessions_row = rows_by_metric["Sessions"]
    # Arm A picks up the August session; Arm B (September) matches nothing
    # -- the October session is outside both windows.
    assert sessions_row[1] == "1"
    assert sessions_row[2] == "0"


# -- compare(): stratification -------------------------------------------


def test_compare_by_stratum_suppresses_below_min_sessions(tmp_path):
    root = tmp_path / "projects"
    project_dir = root / "proj"
    project_dir.mkdir(parents=True)
    _write_session(project_dir, "aug-1", "2026-08-10T10:00:00.000Z")
    _write_session(project_dir, "aug-2", "2026-08-11T10:00:00.000Z")
    _write_session(project_dir, "sep-1", "2026-09-05T10:00:00.000Z")
    corpus = load_corpus([project_dir])

    # Force deterministic mode/purpose via session_overrides rather than
    # relying on the classifier's heuristics for a synthetic transcript
    # shape (classify.classify_session applies an override ahead of its
    # own rules -- see classify.py's own docstring for this mechanism).
    overrides = {
        _session_id_by_ts(corpus, "2026-08-10T10:00:00.000Z"): {"mode": "interactive", "purpose": "review"},
        _session_id_by_ts(corpus, "2026-08-11T10:00:00.000Z"): {"mode": "interactive", "purpose": "review"},
        _session_id_by_ts(corpus, "2026-09-05T10:00:00.000Z"): {"mode": "interactive", "purpose": "review"},
    }

    arm_a = compare_mod.parse_arm_spec("window:2026-08-01..2026-08-31")
    arm_b = compare_mod.parse_arm_spec("window:2026-09-01..2026-09-30")
    section = compare_mod.compare(
        corpus,
        PRICING,
        CONFIG,
        arm_a=arm_a,
        arm_b=arm_b,
        min_sessions=2,
        session_overrides=overrides,
    )

    stratum = _table(section, "compare_by_stratum")
    assert len(stratum.rows) == 1
    row = stratum.rows[0]
    assert row[0] == "purpose=review, mode=interactive"
    sessions_a, sessions_b, sample_ok, note = row[1], row[2], row[3], row[-1]
    assert sessions_a == 2  # both August sessions
    assert sessions_b == 1  # one September session, below min_sessions=2
    assert sample_ok == "no"
    assert "suppressed" in note
    assert "A has 2, B has 1" in note
    # Metric cells must be suppressed (None), not a fabricated 0.
    assert row[4] is None and row[5] is None


def test_compare_by_stratum_shows_metrics_when_sample_ok(tmp_path):
    root = tmp_path / "projects"
    project_dir = root / "proj"
    project_dir.mkdir(parents=True)
    _write_session(project_dir, "aug-1", "2026-08-10T10:00:00.000Z")
    _write_session(project_dir, "aug-2", "2026-08-11T10:00:00.000Z")
    _write_session(project_dir, "sep-1", "2026-09-05T10:00:00.000Z")
    _write_session(project_dir, "sep-2", "2026-09-06T10:00:00.000Z")
    corpus = load_corpus([project_dir])
    overrides = {
        bundle.session_id: {"mode": "interactive", "purpose": "review"} for bundle in corpus.sessions
    }

    arm_a = compare_mod.parse_arm_spec("window:2026-08-01..2026-08-31")
    arm_b = compare_mod.parse_arm_spec("window:2026-09-01..2026-09-30")
    section = compare_mod.compare(
        corpus,
        PRICING,
        CONFIG,
        arm_a=arm_a,
        arm_b=arm_b,
        min_sessions=2,
        session_overrides=overrides,
    )
    stratum = _table(section, "compare_by_stratum")
    assert len(stratum.rows) == 1
    row = stratum.rows[0]
    assert row[3] == "yes"
    assert row[4] is not None and row[5] is not None  # cost_a, cost_b populated


def test_compare_stratify_by_empty_tuple_uses_all_label(tmp_path):
    root = tmp_path / "projects"
    project_dir = root / "proj"
    project_dir.mkdir(parents=True)
    _write_session(project_dir, "aug-1", "2026-08-10T10:00:00.000Z")
    _write_session(project_dir, "sep-1", "2026-09-05T10:00:00.000Z")
    corpus = load_corpus([project_dir])

    arm_a = compare_mod.parse_arm_spec("window:2026-08-01..2026-08-31")
    arm_b = compare_mod.parse_arm_spec("window:2026-09-01..2026-09-30")
    section = compare_mod.compare(
        corpus, PRICING, CONFIG, arm_a=arm_a, arm_b=arm_b, stratify_by=(), min_sessions=1
    )
    stratum = _table(section, "compare_by_stratum")
    assert len(stratum.rows) == 1
    assert stratum.rows[0][0] == "all"


# -- compare(): key: arms + co-changed ----------------------------------


def _snapshot(ts: str, autocompact) -> snapshots_mod.Snapshot:
    return snapshots_mod.Snapshot(
        path=Path(f"{ts}.json"),
        ts=ts,
        data={
            "user_settings": {"autoCompactWindow": autocompact, "model": "sonnet"},
        },
    )


def test_compare_key_arm_matches_via_snapshot_and_reports_co_changed(tmp_path):
    root = tmp_path / "projects"
    project_dir = root / "proj"
    project_dir.mkdir(parents=True)
    _write_session(project_dir, "before", "2026-08-10T10:00:00.000Z")
    _write_session(project_dir, "after", "2026-09-10T10:00:00.000Z")
    corpus = load_corpus([project_dir])

    snap_before = _snapshot("20260801T000000Z", autocompact=5)
    snap_after = _snapshot("20260901T000000Z", autocompact=10)
    # "before" session's first_ts (2026-08-10) postdates snap_before only;
    # "after" session's first_ts (2026-09-10) postdates both, so it joins
    # snap_after (the latest snapshot with ts <= first_ts).
    snaps = [snap_before, snap_after]

    arm_a = compare_mod.parse_arm_spec("key:user_settings.autoCompactWindow=5")
    arm_b = compare_mod.parse_arm_spec("key:user_settings.autoCompactWindow=10")
    section = compare_mod.compare(
        corpus, PRICING, CONFIG, arm_a=arm_a, arm_b=arm_b, min_sessions=1, snapshots=snaps
    )

    overview = _table(section, "compare_overview")
    rows_by_metric = {row[0]: row for row in overview.rows}
    assert rows_by_metric["Sessions"][1] == "1"
    assert rows_by_metric["Sessions"][2] == "1"

    co_changed = _table(section, "compare_co_changed")
    keys = [row[0] for row in co_changed.rows]
    # "model" co-changed (sonnet in both here, so actually unchanged --
    # only autoCompactWindow differs, and that's the arm's own key so
    # it's excluded). With both snapshots sharing the same model value,
    # co_changed should be empty and the table should note it.
    assert keys == []
    assert any("no other" in n.lower() for n in co_changed.notes)


def test_compare_co_changed_reports_other_differing_keys(tmp_path):
    root = tmp_path / "projects"
    project_dir = root / "proj"
    project_dir.mkdir(parents=True)
    _write_session(project_dir, "before", "2026-08-10T10:00:00.000Z")
    _write_session(project_dir, "after", "2026-09-10T10:00:00.000Z")
    corpus = load_corpus([project_dir])

    snap_before = snapshots_mod.Snapshot(
        path=Path("a.json"),
        ts="20260801T000000Z",
        data={"user_settings": {"autoCompactWindow": 5, "model": "sonnet"}},
    )
    snap_after = snapshots_mod.Snapshot(
        path=Path("b.json"),
        ts="20260901T000000Z",
        data={"user_settings": {"autoCompactWindow": 10, "model": "fable"}},
    )
    snaps = [snap_before, snap_after]

    arm_a = compare_mod.parse_arm_spec("key:user_settings.autoCompactWindow=5")
    arm_b = compare_mod.parse_arm_spec("key:user_settings.autoCompactWindow=10")
    section = compare_mod.compare(
        corpus, PRICING, CONFIG, arm_a=arm_a, arm_b=arm_b, min_sessions=1, snapshots=snaps
    )
    co_changed = _table(section, "compare_co_changed")
    keys = [row[0] for row in co_changed.rows]
    assert "user_settings.model" in keys
    assert "user_settings.autoCompactWindow" not in keys  # the arm's own key is excluded


def test_compare_co_changed_empty_when_arms_are_not_both_key_kind(tmp_path):
    root = tmp_path / "projects"
    project_dir = root / "proj"
    project_dir.mkdir(parents=True)
    _write_session(project_dir, "aug-1", "2026-08-10T10:00:00.000Z")
    _write_session(project_dir, "sep-1", "2026-09-05T10:00:00.000Z")
    corpus = load_corpus([project_dir])

    arm_a = compare_mod.parse_arm_spec("window:2026-08-01..2026-08-31")
    arm_b = compare_mod.parse_arm_spec("window:2026-09-01..2026-09-30")
    section = compare_mod.compare(corpus, PRICING, CONFIG, arm_a=arm_a, arm_b=arm_b, min_sessions=1)
    co_changed = _table(section, "compare_co_changed")
    assert co_changed.rows == []
    assert any("only computed when both arms are snapshot-keyed" in n.lower() for n in co_changed.notes)


# -- compare(): project: and profile: arms -------------------------------


def test_compare_project_arm_selects_by_slug(tmp_path):
    root = tmp_path / "projects"
    project_a = root / "proj-a"
    project_b = root / "proj-b"
    project_a.mkdir(parents=True)
    project_b.mkdir(parents=True)
    _write_session(project_a, "s1", "2026-09-01T10:00:00.000Z")
    _write_session(project_b, "s2", "2026-09-02T10:00:00.000Z")
    corpus = load_corpus([project_a, project_b])

    arm_a = compare_mod.parse_arm_spec("project:proj-a")
    arm_b = compare_mod.parse_arm_spec("project:proj-b")
    section = compare_mod.compare(corpus, PRICING, CONFIG, arm_a=arm_a, arm_b=arm_b, min_sessions=1)
    overview = _table(section, "compare_overview")
    rows_by_metric = {row[0]: row for row in overview.rows}
    assert rows_by_metric["Sessions"][1] == "1"
    assert rows_by_metric["Sessions"][2] == "1"


def _write_profile_history(config_dir: Path) -> None:
    """A hook capture with no profile on 2026-09-01, an ``apply`` stamp
    for ``lean`` on 2026-09-05, a one-off ``--set`` stamp on 2026-09-06
    (ignored: it leaves the active profile alone), and that ``lean``
    apply undone on 2026-09-08 (back to no profile)."""
    snaps = config_dir / "snapshots"
    snaps.mkdir(parents=True)
    (snaps / "20260901T000000Z.json").write_text(
        json.dumps({"schema": 2, "ts": "20260901T000000Z", "profile_id": None, "user_settings": {}}), encoding="utf-8"
    )
    (snaps / "20260905T000000Z.json").write_text(
        json.dumps({"ts": "20260905T000000Z", "schema_version": 2, "profile_id": "lean"}), encoding="utf-8"
    )
    (snaps / "20260906T000000Z.json").write_text(
        json.dumps({"ts": "20260906T000000Z", "schema_version": 2, "profile_id": "one-off"}), encoding="utf-8"
    )
    backup = config_dir / "backups" / "20260905T000000Z"
    backup.mkdir(parents=True)
    (backup / "manifest.json").write_text(
        json.dumps(
            {
                "ts": "20260905T000000Z",
                "profile_id": "lean",
                "scope": "user",
                "entries": [{"kind": "active_profile", "path": str(config_dir / "active-profile"), "backup": None}],
            }
        ),
        encoding="utf-8",
    )
    (backup / "reverted.json").write_text(json.dumps({"reverted_at": "2026-09-08T00:00:00Z"}), encoding="utf-8")


def test_compare_profile_arm_selects_sessions_started_under_that_profile(tmp_path):
    root = tmp_path / "projects"
    project_dir = root / "proj"
    project_dir.mkdir(parents=True)
    _write_session(project_dir, "before", "2026-09-02T10:00:00.000Z")
    _write_session(project_dir, "during", "2026-09-06T10:00:00.000Z")
    _write_session(project_dir, "undone", "2026-09-09T10:00:00.000Z")
    corpus = load_corpus([project_dir])
    config_dir = tmp_path / "config"
    _write_profile_history(config_dir)

    arm_a = compare_mod.parse_arm_spec("profile:lean")
    arm_b = compare_mod.parse_arm_spec("window:2026-09-01..2026-09-30")
    section = compare_mod.compare(
        corpus, PRICING, CONFIG, arm_a=arm_a, arm_b=arm_b, min_sessions=1, config_dir=config_dir
    )
    rows_by_metric = {row[0]: row for row in _table(section, "compare_overview").rows}
    assert rows_by_metric["Sessions"][1] == "1"
    assert rows_by_metric["Sessions"][2] == "3"


def test_compare_profile_arm_uses_hook_captures_without_config_dir(tmp_path):
    root = tmp_path / "projects"
    project_dir = root / "proj"
    project_dir.mkdir(parents=True)
    _write_session(project_dir, "s1", "2026-09-01T10:00:00.000Z")
    _write_session(project_dir, "s2", "2026-09-03T10:00:00.000Z")
    corpus = load_corpus([project_dir])
    snaps = [
        snapshots_mod.Snapshot(path=Path("a.json"), ts="20260902T000000Z", data={"profile_id": "lean", "user_settings": {}}),
    ]

    arm_a = compare_mod.parse_arm_spec("profile:lean")
    arm_b = compare_mod.parse_arm_spec("window:2026-09-01..2026-09-30")
    section = compare_mod.compare(corpus, PRICING, CONFIG, arm_a=arm_a, arm_b=arm_b, min_sessions=1, snapshots=snaps)
    rows_by_metric = {row[0]: row for row in _table(section, "compare_overview").rows}
    assert rows_by_metric["Sessions"][1] == "1"


# -- privacy ---------------------------------------------------------------


def test_compare_section_passes_privacy_scan(tmp_path):
    root = tmp_path / "projects"
    project_dir = root / "proj"
    project_dir.mkdir(parents=True)
    _write_session(project_dir, "aug-1", "2026-08-10T10:00:00.000Z")
    _write_session(project_dir, "sep-1", "2026-09-05T10:00:00.000Z")
    corpus = load_corpus([project_dir])

    arm_a = compare_mod.parse_arm_spec("window:2026-08-01..2026-08-31")
    arm_b = compare_mod.parse_arm_spec("project:proj")
    section = compare_mod.compare(corpus, PRICING, CONFIG, arm_a=arm_a, arm_b=arm_b, min_sessions=1)
    assert_privacy(section)


# -- CLI wiring --------------------------------------------------------------


def _write_cli_project(root: Path, slug: str, session_id: str, timestamp: str) -> Path:
    project_dir = root / slug
    project_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(project_dir / f"{session_id}.jsonl", [turn_line(timestamp=timestamp)])
    return project_dir


def test_cli_compare_bad_arm_spec_exits_2(tmp_path, capsys):
    root = tmp_path / "projects"
    _write_cli_project(root, "proj", "s1", "2026-09-01T10:00:00.000Z")
    exit_code = cli.main(
        [
            "compare",
            "--projects-root",
            str(root),
            "--project",
            "proj",
            "--a",
            "not-a-valid-spec",
            "--b",
            "window:2026-09-01..2026-09-30",
        ]
    )
    assert exit_code == 2
    err = capsys.readouterr().err
    assert "claudeglass compare:" in err
    assert "not-a-valid-spec" in err


def test_cli_compare_bad_stratify_key_exits_2(tmp_path, capsys):
    root = tmp_path / "projects"
    _write_cli_project(root, "proj", "s1", "2026-09-01T10:00:00.000Z")
    exit_code = cli.main(
        [
            "compare",
            "--projects-root",
            str(root),
            "--project",
            "proj",
            "--a",
            "window:2026-08-01..2026-08-31",
            "--b",
            "window:2026-09-01..2026-09-30",
            "--stratify",
            "purpose,not-a-real-key",
        ]
    )
    assert exit_code == 2
    err = capsys.readouterr().err
    assert "bad --stratify key" in err


def test_cli_compare_end_to_end_json(tmp_path, capsys):
    root = tmp_path / "projects"
    _write_cli_project(root, "proj", "aug-1", "2026-08-10T10:00:00.000Z")
    _write_cli_project(root, "proj", "sep-1", "2026-09-05T10:00:00.000Z")
    exit_code = cli.main(
        [
            "compare",
            "--projects-root",
            str(root),
            "--project",
            "proj",
            "--a",
            "window:2026-08-01..2026-08-31",
            "--b",
            "window:2026-09-01..2026-09-30",
            "--min-sessions",
            "1",
            "--json",
        ]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    section_keys = {s["key"] for s in payload["report"]["sections"]}
    assert "compare" in section_keys
    assert_privacy(payload)


def test_cli_compare_sample_below_min_sessions_still_exits_0(tmp_path, capsys):
    root = tmp_path / "projects"
    _write_cli_project(root, "proj", "aug-1", "2026-08-10T10:00:00.000Z")
    _write_cli_project(root, "proj", "sep-1", "2026-09-05T10:00:00.000Z")
    exit_code = cli.main(
        [
            "compare",
            "--projects-root",
            str(root),
            "--project",
            "proj",
            "--a",
            "window:2026-08-01..2026-08-31",
            "--b",
            "window:2026-09-01..2026-09-30",
            "--min-sessions",
            "5",
        ]
    )
    # Fewer than min_sessions in either arm is not an error -- compare
    # still prints a full overview with sample_ok="no" and exits 0.
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "no" in out


# -- stratifying by the kind of task Claude reported (metrics capture) --------------


def _write_tagged(project_dir: Path, session_id: str, day: str, task: str | None) -> None:
    from helpers import user_str_line

    # SEC-P2: a `[cg: ...]` tag only counts once a capture note has been
    # seen and the metric it answers was requested -- "task" here.
    note_text = "ClaudeGlass metrics capture (cg-cap v1 task): ..."
    note = attachment_line(
        "hook_additional_context",
        rendered=f"<system-reminder>\nSessionStart hook additional context: {note_text}\n</system-reminder>",
        content=[note_text], hookName="SessionStart", hookEvent="SessionStart", toolUseID="SessionStart",
    )
    note["timestamp"] = f"{day}T09:59:59.000Z"
    lines = [note]
    for n in range(2):
        lines.append(user_str_line("go on", origin={"kind": "human"}, timestamp=f"{day}T10:00:{2 * n:02d}.000Z"))
        text = f"Done.\n[cg: task={task}]" if task else "Done."
        lines.append(turn_line(content=[{"type": "text", "text": text}], timestamp=f"{day}T10:00:{2 * n + 1:02d}.000Z"))
    write_jsonl(project_dir / f"{session_id}.jsonl", lines)


def _tagged_corpus(tmp_path, tasks):
    project_dir = tmp_path / "projects" / "proj"
    project_dir.mkdir(parents=True)
    for n, task in enumerate(tasks):
        _write_tagged(project_dir, f"aug-{n}", f"2026-08-{10 + n:02d}", task)
        _write_tagged(project_dir, f"sep-{n}", f"2026-09-{10 + n:02d}", task)
    return load_corpus([project_dir])


def _compare_tagged(corpus, **kw):
    return compare_mod.compare(
        corpus, PRICING, CONFIG,
        arm_a=compare_mod.parse_arm_spec("window:2026-08-01..2026-08-31"),
        arm_b=compare_mod.parse_arm_spec("window:2026-09-01..2026-09-30"),
        min_sessions=1, **kw,
    )


def test_the_reported_task_joins_the_strata_once_half_the_sessions_have_one(tmp_path):
    stratum = _table(_compare_tagged(_tagged_corpus(tmp_path, ["bugfix", "bugfix", None])), "compare_by_stratum")
    assert stratum.title == "By stratum (purpose, mode, task)"
    assert any("task=bugfix" in row[0] for row in stratum.rows)
    assert any("task=untagged" in row[0] for row in stratum.rows)


def test_mostly_untagged_sessions_keep_the_default_strata(tmp_path):
    stratum = _table(_compare_tagged(_tagged_corpus(tmp_path, ["bugfix", None, None])), "compare_by_stratum")
    assert stratum.title == "By stratum (purpose, mode)"


def test_task_can_be_asked_for_by_name(tmp_path):
    section = _compare_tagged(_tagged_corpus(tmp_path, ["review", None, None]), stratify_by=("task",))
    rows = _table(section, "compare_by_stratum").rows
    assert {row[0] for row in rows} == {"task=review", "task=untagged"}


def test_cli_compare_accepts_task_as_a_stratify_key(tmp_path, capsys):
    root = tmp_path / "projects"
    _write_cli_project(root, "proj", "s1", "2026-09-01T10:00:00.000Z")
    exit_code = cli.main([
        "compare", "--projects-root", str(root), "--project", "proj",
        "--a", "window:2026-08-01..2026-08-31", "--b", "window:2026-09-01..2026-09-30",
        "--stratify", "task", "--json",
    ])
    assert exit_code == 0
    assert "(task)" in capsys.readouterr().out

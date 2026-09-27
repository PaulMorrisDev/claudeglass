"""Change points: applies, their undo, and settings changes the config
snapshots show (``change_points``)."""

from __future__ import annotations

import json
from pathlib import Path

from claudeglass import change_points, snapshots
from claudeglass.profiles import apply as apply_mod
from claudeglass.profiles.schema import load_dict


def _apply(tmp_path: Path, settings: dict):
    claude_root = tmp_path / ".claude"
    config_dir = claude_root / "claudeglass"
    claude_root.mkdir(exist_ok=True)
    profile = load_dict({"id": "one-off", "settings": settings})
    plan = apply_mod.plan_apply(profile, scope="user", project_path=None, config_dir=config_dir, claude_root=claude_root)
    return config_dir, apply_mod.execute(plan, config_dir=config_dir)


def _snapshot(config_dir: Path, ts: str, effective: dict, provenance: dict | None = None) -> None:
    folder = config_dir / "snapshots"
    folder.mkdir(parents=True, exist_ok=True)
    doc = {"ts": ts, "schema_version": 2, "project_slug": "slug:abc", "effective": effective}
    if provenance is not None:
        doc["effective_provenance"] = provenance
    (folder / f"{ts}.json").write_text(json.dumps(doc), encoding="utf-8")


def test_an_apply_and_its_undo_are_change_points_with_their_keys(tmp_path):
    config_dir, result = _apply(tmp_path, {"effortLevel": "medium"})
    [point] = change_points.change_points(config_dir)
    assert point.source == "apply"
    assert point.label == "Applied a one-off change"
    assert point.keys == ["effortLevel"]
    assert point.changes[0]["new"] == "medium"
    apply_mod.revert(result.ts, config_dir=config_dir)
    points = change_points.change_points(config_dir)
    assert [p.source for p in points] == ["apply", "revert"]
    assert points[0].reverted
    assert change_points.latest(config_dir).source == "revert"


def test_a_settings_change_between_snapshots_is_a_change_point(tmp_path):
    config_dir = tmp_path / "tl"
    _snapshot(config_dir, "20260920T100000Z", {"model": "opus"})
    _snapshot(config_dir, "20260921T100000Z", {"model": "opus"})
    _snapshot(config_dir, "20260922T100000Z", {"model": "sonnet"})
    [point] = change_points.change_points(config_dir)
    assert point.source == "config"
    assert point.iso() == "2026-09-22T10:00:00Z"
    assert point.keys == ["effective.model"]


def test_a_settings_change_records_its_values_and_applies_everywhere_from_user_settings(tmp_path):
    config_dir = tmp_path / "tl"
    _snapshot(config_dir, "20260921T100000Z", {"model": "opus", "autoCompactWindow": 100000}, {"model": "user"})
    _snapshot(config_dir, "20260922T100000Z", {"model": "sonnet", "autoCompactWindow": 120000}, {"model": "user"})
    [point] = change_points.change_points(config_dir)
    assert point.changes == [
        {"key": "autoCompactWindow", "agent": None, "old": 100000, "new": 120000},
        {"key": "model", "agent": None, "old": "opus", "new": "sonnet"},
    ]
    assert point.project == ""
    assert point.to_dict()["summary"] == "autoCompactWindow: 100,000 → 120,000; model: opus → sonnet"


def test_a_change_only_a_projects_own_settings_made_applies_to_that_project(tmp_path):
    config_dir = tmp_path / "tl"
    _snapshot(config_dir, "20260921T100000Z", {"model": "opus"}, {"model": "user"})
    _snapshot(config_dir, "20260922T100000Z", {"model": "sonnet"}, {"model": "project_local"})
    [point] = change_points.change_points(config_dir)
    assert point.project == "slug:abc"
    assert point.to_dict()["project"] == "slug:abc"


def test_a_long_value_is_named_but_not_recorded(tmp_path):
    config_dir = tmp_path / "tl"
    _snapshot(config_dir, "20260921T100000Z", {"apiKeyHelper": "a" * 200})
    _snapshot(config_dir, "20260922T100000Z", {"apiKeyHelper": "b" * 200})
    [point] = change_points.change_points(config_dir)
    assert point.keys == ["effective.apiKeyHelper"] and point.changes == []
    assert change_points.summary(point) == "effective.apiKeyHelper"


def test_an_apply_to_a_project_names_that_project(tmp_path):
    claude_root = tmp_path / ".claude"
    config_dir = claude_root / "claudeglass"
    claude_root.mkdir()
    project = tmp_path / "repo"
    project.mkdir()
    profile = load_dict({"id": "one-off", "settings": {"effortLevel": "medium"}})
    plan = apply_mod.plan_apply(profile, scope="project-local", project_path=project, config_dir=config_dir, claude_root=claude_root)
    apply_mod.execute(plan, config_dir=config_dir)
    [point] = change_points.change_points(config_dir)
    assert point.project == change_points.project_key(project)
    (tmp_path / "u").mkdir()
    user_dir, _result = _apply(tmp_path / "u", {"effortLevel": "medium"})
    assert change_points.change_points(user_dir)[0].project == ""


def test_a_snapshot_difference_spanning_an_apply_is_not_counted_twice(tmp_path):
    _snapshot(tmp_path / ".claude" / "claudeglass", "20000101T000000Z", {"effortLevel": "high"})
    config_dir, _result = _apply(tmp_path, {"effortLevel": "medium"})
    _snapshot(config_dir, "20990101T000000Z", {"effortLevel": "medium"})
    assert [p.source for p in change_points.change_points(config_dir)] == ["apply"]


def test_no_changes_means_no_latest(tmp_path):
    assert change_points.latest(tmp_path) is None


def test_an_older_apply_without_recorded_changes_names_keys_from_its_backup(tmp_path):
    """Applies from before changes were recorded: the keys come from the
    backed-up file against the file now."""
    config_dir, result = _apply(tmp_path, {"effortLevel": "medium"})
    manifest_path = config_dir / "backups" / result.ts / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in manifest["entries"]:
        entry.pop("changes", None)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    [point] = change_points.change_points(config_dir)
    assert point.keys == ["effortLevel"]


# -- metrics capture changes ---------------------------------------------------


def test_each_capture_change_is_a_change_point(tmp_path):
    from datetime import datetime, timezone

    from claudeglass import config as config_mod

    config_mod.set_capture(tmp_path, level="essentials", now=datetime(2026, 9, 1, 9, tzinfo=timezone.utc))
    config_mod.set_capture(tmp_path, level="standard", now=datetime(2026, 9, 8, 9, tzinfo=timezone.utc))
    config_mod.set_capture(tmp_path, sample=50, now=datetime(2026, 9, 9, 9, tzinfo=timezone.utc))
    config_mod.set_capture(tmp_path, level="off", now=datetime(2026, 9, 15, 9, tzinfo=timezone.utc))
    points = change_points.change_points(tmp_path)
    assert [p.source for p in points] == ["capture"] * 4
    assert [p.label for p in points] == [
        "Turned metrics capture on: Essentials",
        "Metrics capture level: Standard",
        "Changed metrics capture",
        "Turned metrics capture off",
    ]
    # CAP-8: the first, off -> on call also gets the default time-box, so
    # its own log entry (and this change point) carries a "until" change
    # too, alongside "level".
    assert points[0].keys == ["capture.level", "capture.until"]
    assert points[0].changes == [
        {"key": "capture.level", "agent": None, "old": "off", "new": "essentials"},
        {"key": "capture.until", "agent": None, "old": "", "new": "2026-09-15T09:00:00+00:00"},
    ]
    assert points[2].keys == ["capture.sample"]
    assert change_points.latest(tmp_path).label == "Turned metrics capture off"


def test_turning_coaching_notes_on_and_off_is_named_as_such(tmp_path):
    from datetime import datetime, timezone

    from claudeglass import config as config_mod

    config_mod.set_capture(tmp_path, coaching=["coaching_notes"], now=datetime(2026, 9, 1, 9, tzinfo=timezone.utc))
    config_mod.set_capture(tmp_path, coaching=["coaching_notes", "coaching_line"],
                           now=datetime(2026, 9, 2, 9, tzinfo=timezone.utc))
    config_mod.set_capture(tmp_path, coaching=[], now=datetime(2026, 9, 3, 9, tzinfo=timezone.utc))
    points = change_points.change_points(tmp_path)
    assert [p.label for p in points] == ["Turned coaching notes on", "Changed live coaching", "Turned coaching notes off"]
    assert points[0].keys == ["capture.coaching"]
    assert points[0].to_dict()["summary"] == "capture.coaching: none → Coaching notes from Claude"


def test_a_broken_capture_log_line_is_skipped(tmp_path):
    (tmp_path / "capture-log.jsonl").write_text(
        'not json\n{"ts": "2026-09-01T09:00:00+00:00", "level": "free", "changed": {}}\n'
        '{"ts": "2026-09-02T09:00:00+00:00", "level": "free", "changed": {"level": {"from": "off", "to": "free"}}}\n',
        encoding="utf-8",
    )
    [point] = change_points.change_points(tmp_path)
    assert point.label == "Turned metrics capture on: Free"


# -- EST-P9: change points a transcript itself shows -------------------------


def _session_file(project_dir, session_id, *, claude_md_chars, model, ts_prefix):
    from helpers import attachment_line, turn_line, write_jsonl

    # attachment_line() has no timestamp kwarg of its own (unlike
    # turn_line()/_base_line()'s other builders) -- set it directly so
    # the instructions event lands before the session's first turn.
    instructions = attachment_line(
        "instructions",
        files=[{"path": "C:/repo/CLAUDE.md", "type": "Project", "content": "x" * claude_md_chars}],
    )
    instructions["timestamp"] = f"{ts_prefix}T08:59:55.000Z"
    lines = [
        instructions,
        turn_line(timestamp=f"{ts_prefix}T09:00:00.000Z", model=model),
        turn_line(timestamp=f"{ts_prefix}T09:00:05.000Z", model=model),
    ]
    write_jsonl(project_dir / f"{session_id}.jsonl", lines)


def test_a_big_claude_md_size_change_between_sessions_is_a_change_point(tmp_path):
    from claudeglass.corpus import load_corpus

    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    _session_file(project_dir, "s1", claude_md_chars=1000, model="claude-sonnet-5", ts_prefix="2026-09-10")
    _session_file(project_dir, "s2", claude_md_chars=2000, model="claude-sonnet-5", ts_prefix="2026-09-17")
    corpus = load_corpus([project_dir])
    [point] = change_points.change_points(tmp_path, corpus)
    assert point.source == "transcript"
    assert point.keys == ["claude_md_chars"]
    assert point.changes == [{"key": "claude_md_chars", "agent": None, "old": 1000, "new": 2000}]
    assert point.label == "CLAUDE.md size changed"


def test_a_small_claude_md_size_change_is_not_a_change_point(tmp_path):
    from claudeglass.corpus import load_corpus

    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    _session_file(project_dir, "s1", claude_md_chars=1000, model="claude-sonnet-5", ts_prefix="2026-09-10")
    _session_file(project_dir, "s2", claude_md_chars=1050, model="claude-sonnet-5", ts_prefix="2026-09-17")
    corpus = load_corpus([project_dir])
    assert change_points.change_points(tmp_path, corpus) == []


def test_a_dominant_model_shift_between_sessions_is_a_change_point(tmp_path):
    from claudeglass.corpus import load_corpus

    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    _session_file(project_dir, "s1", claude_md_chars=0, model="claude-sonnet-5", ts_prefix="2026-09-10")
    _session_file(project_dir, "s2", claude_md_chars=0, model="claude-opus-5", ts_prefix="2026-09-17")
    corpus = load_corpus([project_dir])
    [point] = change_points.change_points(tmp_path, corpus)
    assert point.source == "transcript"
    assert point.project == snapshots.snapshot_project_key(corpus.sessions[0].slug)
    assert point.keys == ["model"]
    assert point.changes == [{"key": "model", "agent": None, "old": "claude-sonnet-5", "new": "claude-opus-5"}]
    assert point.label == "Model changed"


def test_sessions_in_different_projects_are_not_compared(tmp_path):
    """Two projects on steady, different models: moving between them
    changes nothing. The dashboard's corpus comes from the store, whose
    bundles have no ``project_dir``, so each is known by its slug."""
    from dataclasses import replace

    from claudeglass.corpus import load_corpus

    shop, docs = tmp_path / "C--work-shop", tmp_path / "C--work-docs"
    for folder in (shop, docs):
        folder.mkdir()
    _session_file(shop, "s1", claude_md_chars=1000, model="claude-opus-5", ts_prefix="2026-09-10")
    _session_file(docs, "s2", claude_md_chars=4000, model="claude-sonnet-5", ts_prefix="2026-09-11")
    _session_file(shop, "s3", claude_md_chars=1000, model="claude-opus-5", ts_prefix="2026-09-12")
    _session_file(docs, "s4", claude_md_chars=4000, model="claude-sonnet-5", ts_prefix="2026-09-13")
    corpus = load_corpus([shop, docs])
    assert change_points.change_points(tmp_path, corpus) == []
    corpus.sessions[:] = [replace(bundle, project_dir="") for bundle in corpus.sessions]
    assert change_points.change_points(tmp_path, corpus) == []


def test_a_transcript_change_a_recorded_change_explains_is_not_a_second_point(tmp_path):
    """A model setting changed between two sessions, and the second one
    shows it: one change, not two (a second would cut the first one's
    after sessions short). What the setting doesn't explain stays."""
    from claudeglass.corpus import load_corpus

    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    _session_file(project_dir, "s1", claude_md_chars=1000, model="claude-sonnet-5", ts_prefix="2026-09-10")
    _session_file(project_dir, "s2", claude_md_chars=2000, model="claude-opus-5", ts_prefix="2026-09-17")
    _snapshot(tmp_path, "20260912T000000Z", {"model": "sonnet"})
    _snapshot(tmp_path, "20260913T000000Z", {"model": "opus"})
    corpus = load_corpus([project_dir])
    config, seen = change_points.change_points(tmp_path, corpus)
    assert config.source == "config"
    assert seen.source == "transcript" and seen.keys == ["claude_md_chars"]
    assert [c["key"] for c in seen.changes] == ["claude_md_chars"]
    assert seen.label == "CLAUDE.md size changed"


def test_a_change_in_another_project_or_outside_the_gap_explains_nothing(tmp_path):
    from claudeglass.corpus import load_corpus

    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    _session_file(project_dir, "s1", claude_md_chars=0, model="claude-sonnet-5", ts_prefix="2026-09-10")
    _session_file(project_dir, "s2", claude_md_chars=0, model="claude-opus-5", ts_prefix="2026-09-17")
    # Before the first session: the change the second one shows came later.
    _snapshot(tmp_path, "20260901T000000Z", {"model": "sonnet"})
    _snapshot(tmp_path, "20260902T000000Z", {"model": "opus"})
    # In the gap, but in another project's own settings.
    project_layer = {"model": "project_local"}
    _snapshot(tmp_path, "20260912T000000Z", {"model": "opus"}, project_layer)
    _snapshot(tmp_path, "20260913T000000Z", {"model": "haiku"}, project_layer)
    corpus = load_corpus([project_dir])
    sources = [p.source for p in change_points.change_points(tmp_path, corpus)]
    assert sources == ["config", "config", "transcript"]


def test_without_a_corpus_transcript_points_are_left_out(tmp_path):
    """change_points(config_dir) with no corpus is the "since my last
    change" caller's path -- it can't classify transcript signatures
    without one, so it just doesn't try."""
    assert change_points.change_points(tmp_path) == []
    assert change_points.change_points(tmp_path, corpus=None) == []

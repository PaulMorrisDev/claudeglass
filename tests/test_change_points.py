"""Change points: applies, their undo, and settings changes the config
snapshots show (``change_points``)."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

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


def _snapshot(
    config_dir: Path,
    ts: str,
    effective: dict | None = None,
    provenance: dict | None = None,
    *,
    slug: str = "slug:abc",
    **sections,
) -> None:
    folder = config_dir / "snapshots"
    folder.mkdir(parents=True, exist_ok=True)
    doc = {"ts": ts, "schema_version": 2, "project_slug": slug, **sections}
    if effective is not None:
        doc["effective"] = effective
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


def test_a_project_folder_gets_the_same_key_whichever_case_its_drive_letter_is_in(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_PROJECT_DIR_NAME", raising=False)
    canonical = snapshots.snapshot_project_key("C--work-x")
    assert change_points.project_key("c:\\work\\x") == change_points.project_key("C:\\work\\x") == canonical


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


# -- one edit seen by several projects' snapshots -----------------------------


def _edit_seen_by(config_dir: Path, *sightings: tuple[str, str, str]) -> None:
    """Your user settings' model going opus -> sonnet, as each project's
    snapshots show it: ``(project, snapshot before, snapshot after)``."""
    for slug, before, after in sightings:
        _snapshot(config_dir, before, {"model": "opus"}, {"model": "user"}, slug=slug)
        _snapshot(config_dir, after, {"model": "sonnet"}, {"model": "user"}, slug=slug)


def test_one_edit_to_your_settings_seen_in_three_projects_is_one_change_point(tmp_path):
    _edit_seen_by(
        tmp_path,
        ("slug:p1", "20260901T100000Z", "20260902T100000Z"),
        ("slug:p2", "20260901T110000Z", "20260903T100000Z"),
        ("slug:p3", "20260901T120000Z", "20260904T100000Z"),
    )
    [point] = change_points.change_points(tmp_path)
    assert point.iso() == "2026-09-02T10:00:00Z"
    assert point.keys == ["effective.model"] and point.project == ""


def test_the_same_value_reached_again_later_is_a_new_change_point(tmp_path):
    _edit_seen_by(tmp_path, ("slug:p1", "20260901T100000Z", "20260902T100000Z"))
    _snapshot(tmp_path, "20260903T100000Z", {"model": "opus"}, {"model": "user"}, slug="slug:p1")
    _snapshot(tmp_path, "20260904T100000Z", {"model": "sonnet"}, {"model": "user"}, slug="slug:p1")
    _snapshot(tmp_path, "20260901T110000Z", {"model": "opus"}, {"model": "user"}, slug="slug:p2")
    _snapshot(tmp_path, "20260905T100000Z", {"model": "sonnet"}, {"model": "user"}, slug="slug:p2")
    assert [p.iso() for p in change_points.change_points(tmp_path)] == [
        "2026-09-02T10:00:00Z",
        "2026-09-03T10:00:00Z",
        "2026-09-04T10:00:00Z",
    ]


def test_a_change_a_projects_own_settings_made_is_not_the_same_change_in_another_project(tmp_path):
    for slug, before, after in (
        ("slug:p1", "20260901T100000Z", "20260902T100000Z"),
        ("slug:p2", "20260901T110000Z", "20260903T100000Z"),
    ):
        _snapshot(tmp_path, before, {"model": "sonnet"}, {"model": "user"}, slug=slug)
        _snapshot(tmp_path, after, {"model": "haiku"}, {"model": "project_local"}, slug=slug)
    first, second = change_points.change_points(tmp_path)
    assert (first.project, second.project) == ("slug:p1", "slug:p2")


def test_a_key_another_snapshot_already_reported_goes_but_a_projects_own_agent_stays(tmp_path):
    _snapshot(tmp_path, "20260901T100000Z", {"model": "opus"}, {"model": "user"}, slug="slug:p1")
    _snapshot(tmp_path, "20260902T100000Z", {"model": "sonnet"}, {"model": "user"}, slug="slug:p1")
    reviewer = {"reviewer": {"source": "project", "model": "haiku"}}
    _snapshot(tmp_path, "20260901T110000Z", {"model": "opus"}, {"model": "user"}, slug="slug:p2", agents=reviewer)
    reviewer = {"reviewer": {"source": "project", "model": "sonnet"}}
    _snapshot(tmp_path, "20260903T100000Z", {"model": "sonnet"}, {"model": "user"}, slug="slug:p2", agents=reviewer)
    first, second = change_points.change_points(tmp_path)
    assert first.keys == ["effective.model"] and first.project == ""
    assert second.keys == ["agents.reviewer.model"]
    assert second.changes == [{"key": "model", "agent": "reviewer", "old": "haiku", "new": "sonnet"}]
    assert second.project == "slug:p2"


@pytest.mark.parametrize("overriding_first", [False, True])
def test_one_edit_to_your_settings_is_one_change_point_where_a_project_overrides_the_setting(
    tmp_path, overriding_first
):
    """A project whose own settings set the model shows your edit to it,
    and to the theme with it, as ``user_settings.*`` rather than
    ``effective.model``: the same edit, whichever project sees it first."""
    early, late = ("20260901T100000Z", "20260902T100000Z"), ("20260901T110000Z", "20260903T100000Z")
    plain, overriding = (late, early) if overriding_first else (early, late)
    for ts, model, theme in ((plain[0], "opus", "dark"), (plain[1], "sonnet", "light")):
        _snapshot(tmp_path, ts, {"model": model}, {"model": "user"}, slug="slug:plain",
                  user_settings={"model": model, "theme": theme})
    for ts, model, theme in ((overriding[0], "opus", "dark"), (overriding[1], "sonnet", "light")):
        _snapshot(tmp_path, ts, {"model": "haiku"}, {"model": "project_local"}, slug="slug:overriding",
                  user_settings={"model": model, "theme": theme})
    [point] = change_points.change_points(tmp_path)
    assert point.iso() == "2026-09-02T10:00:00Z" and point.project == ""


def test_a_user_setting_changed_with_a_projects_own_change_is_still_a_change_everywhere(tmp_path):
    """The project's own model change hides your theme edit in its
    snapshots, so another project's snapshot of the theme edit is the
    change point for it."""
    _snapshot(tmp_path, "20260901T100000Z", {"model": "haiku"}, {"model": "project_local"}, slug="slug:p1",
              user_settings={"theme": "dark"})
    _snapshot(tmp_path, "20260902T100000Z", {"model": "opus"}, {"model": "project_local"}, slug="slug:p1",
              user_settings={"theme": "light"})
    _snapshot(tmp_path, "20260901T110000Z", {"model": "sonnet"}, {"model": "user"}, slug="slug:p2",
              user_settings={"theme": "dark"})
    _snapshot(tmp_path, "20260903T100000Z", {"model": "sonnet"}, {"model": "user"}, slug="slug:p2",
              user_settings={"theme": "light"})
    points = change_points.change_points(tmp_path)
    assert [(p.keys, p.project) for p in points] == [(["effective.model"], "slug:p1"), (["user_settings.theme"], "")]


def test_an_apply_somewhere_does_not_hide_a_later_snapshot_of_the_same_edit(tmp_path):
    """The first project to see the edit took its last snapshot before an
    unrelated apply, so its difference spans the apply and is left out. The
    next project to see it is the change point, not a copy of one."""
    config_dir, _result = _apply(tmp_path, {"effortLevel": "medium"})
    _edit_seen_by(
        config_dir,
        ("slug:stale", "20000101T000000Z", "20950101T000000Z"),
        ("slug:busy", "20300101T000000Z", "20990101T000000Z"),
    )
    apply, config = change_points.change_points(config_dir)
    assert (apply.source, config.source) == ("apply", "config")
    assert config.iso() == "2099-01-01T00:00:00Z"


# -- MCP servers --------------------------------------------------------------


def _mcp(names: list[str], own: list[str]) -> dict:
    """A snapshot's MCP servers: every name the project can reach, and the
    ones its own ``.mcp.json`` has."""
    return {"mcp_servers": {"names": names}, "content_layers": {"mcp_json": {"present": bool(own), "names": own}}}


def test_a_server_added_to_your_own_config_is_one_change_point_in_every_project(tmp_path):
    _snapshot(tmp_path, "20260901T100000Z", slug="slug:p1", **_mcp(["a"], []))
    _snapshot(tmp_path, "20260902T100000Z", slug="slug:p1", **_mcp(["a", "global"], []))
    _snapshot(tmp_path, "20260901T110000Z", slug="slug:p2", **_mcp(["a", "own"], ["own"]))
    _snapshot(tmp_path, "20260903T100000Z", slug="slug:p2", **_mcp(["a", "global", "own"], ["own"]))
    [point] = change_points.change_points(tmp_path)
    assert point.iso() == "2026-09-02T10:00:00Z"
    assert point.keys == ["mcp_servers.names"] and point.project == ""


def test_a_server_added_to_a_projects_own_mcp_json_is_a_change_point_for_that_project(tmp_path):
    _snapshot(tmp_path, "20260901T100000Z", slug="slug:p1", **_mcp(["a"], []))
    _snapshot(tmp_path, "20260902T100000Z", slug="slug:p1", **_mcp(["a", "own"], ["own"]))
    _snapshot(tmp_path, "20260901T110000Z", slug="slug:p2", **_mcp(["a"], []))
    _snapshot(tmp_path, "20260903T100000Z", slug="slug:p2", **_mcp(["a", "own"], ["own"]))
    first, second = change_points.change_points(tmp_path)
    assert (first.project, second.project) == ("slug:p1", "slug:p2")


def test_a_projects_own_server_seen_with_a_server_already_reported_is_a_change_point_for_that_project(tmp_path):
    _snapshot(tmp_path, "20260901T100000Z", slug="slug:p1", **_mcp(["a"], []))
    _snapshot(tmp_path, "20260902T100000Z", slug="slug:p1", **_mcp(["a", "global"], []))
    _snapshot(tmp_path, "20260901T110000Z", slug="slug:p2", **_mcp(["a"], []))
    _snapshot(tmp_path, "20260905T100000Z", slug="slug:p2", **_mcp(["a", "global", "own"], ["own"]))
    first, second = change_points.change_points(tmp_path)
    assert (first.iso(), first.project) == ("2026-09-02T10:00:00Z", "")
    assert (second.iso(), second.keys, second.project) == (
        "2026-09-05T10:00:00Z", ["mcp_servers.names"], "slug:p2",
    )


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


# -- habits you marked "Trying it" ------------------------------------------------


def _tried(tmp_path, kind, item, *, day, state="trying"):
    from datetime import datetime, timezone

    from claudeglass import config as config_mod

    config_mod.append_habit_log(
        tmp_path, kind=kind, item=item, state=state, now=datetime(2026, 9, day, 9, tzinfo=timezone.utc)
    )


def test_each_card_you_marked_trying_is_a_change_point_for_every_project(tmp_path):
    _tried(tmp_path, "habit", "split_large", day=2)
    _tried(tmp_path, "tip", "drip_feed", day=3)
    _tried(tmp_path, "recommendation", "model.default", day=4)
    points = change_points.change_points(tmp_path)
    assert [p.source for p in points] == ["habit"] * 3
    assert [p.label for p in points] == [
        "Started trying: Split large asks into planned steps",
        "Started trying: Small requests sent one at a time",
        "Started trying a recommendation",
    ]
    assert [p.keys for p in points] == [["habit.split_large"], ["habit.drip_feed"], ["habit.model.default"]]
    assert points[0].changes == [{"key": "habit.split_large", "agent": None, "old": None, "new": "trying"}]
    assert [p.ts.day for p in points] == [2, 3, 4]
    # A habit is yours, not a project's: it applies everywhere.
    assert all(change_points.applies_to(p, "any-project") for p in points)
    assert change_points.latest(tmp_path).label == "Started trying a recommendation"


def test_a_habit_point_says_where_it_came_from_and_names_no_setting(tmp_path):
    _tried(tmp_path, "tip", "plan_fresh", day=2)
    [point] = change_points.change_points(tmp_path)
    assert change_points.summary(point) == "You marked it as trying on the dashboard"
    assert point.to_dict()["summary"] == "You marked it as trying on the dashboard"
    assert point.label.startswith("Started trying: ")


def test_a_tip_that_has_no_title_of_its_own_still_gets_a_neutral_label(tmp_path):
    _tried(tmp_path, "tip", "something-new", day=2)
    [point] = change_points.change_points(tmp_path)
    assert point.label == "Started trying a habit" and point.keys == ["habit.something-new"]


def test_only_trying_counts_and_a_broken_habit_line_is_skipped(tmp_path):
    _tried(tmp_path, "habit", "split_large", day=2, state="useful")
    (tmp_path / "habit-log.jsonl").write_text(
        "not json\n"
        + json.dumps({"ts": "never", "kind": "habit", "item": "split_large", "state": "trying"}) + "\n"
        + json.dumps({"ts": "2026-09-03T09:00:00+00:00", "kind": "idea", "item": "x", "state": "trying"}) + "\n"
        + json.dumps({"ts": "2026-09-04T09:00:00+00:00", "kind": "habit", "item": "split_large", "state": "trying"}) + "\n",
        encoding="utf-8",
    )
    points = change_points.change_points(tmp_path)
    assert [(p.source, p.ts.day) for p in points] == [("habit", 4)]


def test_habit_points_sit_among_the_others_in_time_order(tmp_path):
    from datetime import datetime, timezone

    from claudeglass import config as config_mod

    config_mod.set_capture(tmp_path, level="essentials", now=datetime(2026, 9, 1, 9, tzinfo=timezone.utc))
    _tried(tmp_path, "habit", "split_large", day=5)
    config_mod.set_capture(tmp_path, sample=50, now=datetime(2026, 9, 9, 9, tzinfo=timezone.utc))
    points = change_points.change_points(tmp_path)
    assert [p.source for p in points] == ["capture", "habit", "capture"]


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


def test_a_capture_change_is_named_by_what_changed_not_by_where_capture_stands(tmp_path):
    """The log's ``level`` is where capture is after the change, so it
    says "off" for a change made while capture is off."""
    records = [
        {"ts": "2026-09-01T09:00:00+00:00", "level": "off", "changed": {"projects": {"from": [], "to": ["shop"]}}},
        {"ts": "2026-09-02T09:00:00+00:00", "level": "off", "changed": {"sample": {"from": 100, "to": 50}}},
        {
            "ts": "2026-09-03T09:00:00+00:00",
            "level": "standard",
            "changed": {"level": {"from": "off", "to": "standard"}, "projects": {"from": ["shop"], "to": []}},
        },
        {"ts": "2026-09-04T09:00:00+00:00", "level": "standard", "changed": {"projects": {"from": [], "to": ["shop"]}}},
        {"ts": "2026-09-05T09:00:00+00:00", "level": "off", "changed": {"level": {"from": "standard", "to": "off"}}},
    ]
    (tmp_path / "capture-log.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    points = change_points.change_points(tmp_path)
    assert [p.label for p in points] == [
        "Changed which projects capture and coaching run in",
        "Changed metrics capture",
        "Turned metrics capture on: Standard",
        "Changed which projects capture and coaching run in",
        "Turned metrics capture off",
    ]
    assert {p.project for p in points} == {""}


def test_records_one_capture_command_wrote_seconds_apart_are_one_change_point(tmp_path):
    """``capture on`` logs which projects, then the level, a few seconds
    apart: one card, named by the level, each key from its first value to
    its last."""
    records = [
        {"ts": "2026-09-24T18:04:49+00:00", "level": "off", "changed": {"projects": {"from": [], "to": ["shop"]}}},
        {
            "ts": "2026-09-24T18:04:53+00:00",
            "level": "standard",
            "changed": {"level": {"from": "off", "to": "standard"}, "projects": {"from": ["shop"], "to": ["shop", "x"]}},
        },
        {"ts": "2026-09-24T18:24:40+00:00", "level": "deep", "changed": {"level": {"from": "standard", "to": "deep"}}},
    ]
    (tmp_path / "capture-log.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    first, second = change_points.change_points(tmp_path)
    assert first.iso() == "2026-09-24T18:04:49Z"
    assert first.label == "Turned metrics capture on: Standard"
    assert first.changes == [
        {"key": "capture.level", "agent": None, "old": "off", "new": "standard"},
        {"key": "capture.projects", "agent": None, "old": [], "new": ["shop", "x"]},
    ]
    assert second.label == "Metrics capture level: Deep"


def test_one_settings_edit_saved_twice_seconds_apart_is_one_change_point(tmp_path):
    _snapshot(tmp_path, "20260924T160000Z", {"model": "opus"}, {"model": "user"}, slug="slug:p1")
    _snapshot(tmp_path, "20260924T161648Z", {"model": "sonnet"}, {"model": "user"}, slug="slug:p1")
    _snapshot(tmp_path, "20260924T161704Z", {"model": "haiku"}, {"model": "user"}, slug="slug:p1")
    _snapshot(tmp_path, "20260924T180000Z", {"model": "opus"}, {"model": "user"}, slug="slug:p1")
    first, second = change_points.change_points(tmp_path)
    assert first.iso() == "2026-09-24T16:16:48Z"
    assert first.keys == ["effective.model"]
    assert first.changes == [{"key": "model", "agent": None, "old": "opus", "new": "haiku"}]
    assert second.iso() == "2026-09-24T18:00:00Z"


# -- turning capture on also rewrites your settings' hooks --------------------

_NOW = datetime(2026, 9, 5, tzinfo=timezone.utc)


def test_turning_capture_on_is_one_change_point_not_a_capture_one_and_a_hooks_one(tmp_path):
    from claudeglass import config as config_mod

    _snapshot(tmp_path, "20260901T080000Z", user_settings={"hooks": "dict(3)"})
    config_mod.set_capture(tmp_path, level="essentials", now=datetime(2026, 9, 1, 9, tzinfo=timezone.utc))
    _snapshot(tmp_path, "20260901T100000Z", user_settings={"hooks": "dict(5)"})
    # Another project's snapshot of the same hook change, taken between the
    # capture being logged and the hooks being written, still isn't a second.
    _snapshot(tmp_path, "20260901T093000Z", slug="slug:other", user_settings={"hooks": "dict(3)"})
    _snapshot(tmp_path, "20260901T110000Z", slug="slug:other", user_settings={"hooks": "dict(5)"})
    assert [p.source for p in change_points.change_points(tmp_path, now=_NOW)] == ["capture"]


def test_a_settings_change_made_with_the_hooks_stays_without_them(tmp_path):
    from claudeglass import config as config_mod

    _snapshot(tmp_path, "20260901T080000Z", user_settings={"hooks": "dict(3)", "theme": "dark"})
    config_mod.set_capture(tmp_path, level="essentials", now=datetime(2026, 9, 1, 9, tzinfo=timezone.utc))
    _snapshot(tmp_path, "20260901T100000Z", user_settings={"hooks": "dict(5)", "theme": "light"})
    capture, config = change_points.change_points(tmp_path, now=_NOW)
    assert (capture.source, config.source) == ("capture", "config")
    assert config.keys == ["user_settings.theme"]


def test_a_hooks_change_with_no_capture_change_in_its_window_stays_a_change_point(tmp_path):
    _snapshot(tmp_path, "20260901T080000Z", user_settings={"hooks": "dict(3)"})
    _snapshot(tmp_path, "20260901T100000Z", user_settings={"hooks": "dict(5)"})
    [point] = change_points.change_points(tmp_path, now=_NOW)
    assert point.source == "config" and point.keys == ["user_settings.hooks"]


def _old_hooks_changes(config_dir: Path) -> None:
    _snapshot(config_dir, "20260101T000000Z", user_settings={"hooks": "dict(3)", "theme": "dark"})
    _snapshot(config_dir, "20260102T000000Z", user_settings={"hooks": "dict(5)", "theme": "dark"})
    _snapshot(config_dir, "20260103T000000Z", user_settings={"hooks": "dict(6)", "theme": "light"})
    _snapshot(config_dir, "20260801T000000Z", user_settings={"hooks": "dict(7)", "theme": "light"})


def test_a_hooks_only_change_older_than_the_capture_log_is_left_out(tmp_path):
    """Capture's own record of turning it on is gone by now, so a hooks
    change from before the log starts is taken to be that."""
    _old_hooks_changes(tmp_path)
    points = change_points.change_points(tmp_path, now=_NOW)
    assert [(p.iso(), p.keys) for p in points] == [
        ("2026-01-03T00:00:00Z", ["user_settings.hooks", "user_settings.theme"]),
        ("2026-08-01T00:00:00Z", ["user_settings.hooks"]),
    ]


def test_the_capture_log_goes_back_as_far_as_the_retention_setting_says(tmp_path):
    _old_hooks_changes(tmp_path)
    (tmp_path / "config.toml").write_text("retention_days = 400\n", encoding="utf-8")
    assert len(change_points.change_points(tmp_path, now=_NOW)) == 3


def test_a_hooks_change_too_old_for_the_capture_log_is_not_shown_from_another_project_later(tmp_path):
    """Another project's snapshots last ran before the old hooks change and
    next ran after the log's start: still that change, not a new one."""
    _snapshot(tmp_path, "20260101T000000Z", user_settings={"hooks": "dict(3)"})
    _snapshot(tmp_path, "20260102T000000Z", user_settings={"hooks": "dict(5)"})
    _snapshot(tmp_path, "20251231T000000Z", slug="slug:stale", user_settings={"hooks": "dict(3)"})
    _snapshot(tmp_path, "20260601T000000Z", slug="slug:stale", user_settings={"hooks": "dict(5)"})
    assert change_points.change_points(tmp_path, now=_NOW) == []


# -- EST-P9: change points a transcript itself shows -------------------------

OLD, NEW, THIRD = "claude-sonnet-5", "claude-opus-5", "claude-haiku-5"


def _session_file(project_dir, session_id, *, claude_md_chars, model, ts_prefix, effort=None):
    from helpers import attachment_line, turn_line, write_jsonl

    # attachment_line() has no timestamp kwarg of its own (unlike
    # turn_line()/_base_line()'s other builders) -- set it directly so
    # the instructions event lands before the session's first turn.
    instructions = attachment_line(
        "instructions",
        files=[{"path": "C:/repo/CLAUDE.md", "type": "Project", "content": "x" * claude_md_chars}],
    )
    instructions["timestamp"] = f"{ts_prefix}T08:59:55.000Z"
    extra = {"effort": effort} if effort else {}
    lines = [
        instructions,
        turn_line(timestamp=f"{ts_prefix}T09:00:00.000Z", model=model, **extra),
        turn_line(timestamp=f"{ts_prefix}T09:00:05.000Z", model=model, **extra),
    ]
    write_jsonl(project_dir / f"{session_id}.jsonl", lines)


def _history(tmp_path, *sessions, folder="proj"):
    """The corpus of one project's sessions, oldest first and a day apart
    from 10 September. Each is a model, or a dict of ``_session_file``'s
    arguments (``model``, ``claude_md_chars``, ``effort``)."""
    from claudeglass.corpus import load_corpus

    project_dir = tmp_path / folder
    project_dir.mkdir(parents=True, exist_ok=True)
    for index, spec in enumerate(sessions):
        spec = {"claude_md_chars": 0, **({"model": spec} if isinstance(spec, str) else spec)}
        day = date(2026, 9, 10) + timedelta(days=index)
        _session_file(project_dir, f"s{index:02d}", ts_prefix=day.isoformat(), **spec)
    return load_corpus([project_dir])


def _sized(*sizes, model=OLD):
    return [{"model": model, "claude_md_chars": size} for size in sizes]


def _transcript_changes(tmp_path, *sessions, **kwargs):
    return change_points.change_points(tmp_path, _history(tmp_path, *sessions, **kwargs))


def test_a_big_claude_md_size_change_that_holds_is_a_change_point(tmp_path):
    [point] = _transcript_changes(tmp_path, *_sized(1000, 1000, 1000, 2000, 2000, 2000))
    assert point.source == "transcript"
    assert point.keys == ["claude_md_chars"]
    assert point.changes == [{"key": "claude_md_chars", "agent": None, "old": 1000, "new": 2000}]
    assert point.label == "CLAUDE.md size changed"


def test_a_small_claude_md_size_change_is_not_a_change_point(tmp_path):
    assert _transcript_changes(tmp_path, *_sized(1000, 1000, 1000, 1050, 1050, 1050)) == []


def test_a_claude_md_that_grows_a_little_at_a_time_is_not_a_change_point(tmp_path):
    assert _transcript_changes(tmp_path, *_sized(1000, 1050, 1100, 1160, 1220, 1280)) == []


def test_a_session_without_a_claude_md_is_not_a_change_in_it(tmp_path):
    """Scratch and worktree sessions have none, which says nothing about
    the project's own CLAUDE.md coming and going."""
    assert _transcript_changes(tmp_path, *_sized(1000, 1000, 1000, 0, 1000, 1000)) == []
    assert _transcript_changes(tmp_path / "again", *_sized(1000, 1000, 1000, 0, 0, 0, 1000)) == []


def test_a_dominant_model_that_holds_for_three_sessions_is_a_change_point(tmp_path):
    corpus = _history(tmp_path, OLD, OLD, OLD, NEW, NEW, NEW)
    [point] = change_points.change_points(tmp_path, corpus)
    assert point.source == "transcript"
    assert point.project == snapshots.snapshot_project_key(corpus.sessions[0].slug)
    assert point.keys == ["model"]
    assert point.changes == [{"key": "model", "agent": None, "old": OLD, "new": NEW}]
    assert point.label == "Model changed"
    # It is the first session on the new model, and the change happened
    # after the last one on the old.
    [(since, same)] = change_points._transcript_points(corpus)
    assert (since.date(), same.ts.date(), point.ts) == (date(2026, 9, 12), date(2026, 9, 13), same.ts)


def test_a_model_that_alternates_is_not_a_change_point(tmp_path):
    assert _transcript_changes(tmp_path, OLD, NEW, OLD, NEW, OLD, NEW, OLD, NEW) == []
    assert _transcript_changes(tmp_path / "settled", OLD, OLD, OLD, NEW, OLD, NEW, OLD, NEW) == []


def test_a_new_model_is_not_a_change_point_until_it_has_three_sessions(tmp_path):
    assert _transcript_changes(tmp_path, OLD, OLD, OLD, NEW, NEW) == []
    [point] = _transcript_changes(tmp_path / "third", OLD, OLD, OLD, NEW, NEW, NEW)
    assert point.iso() == "2026-09-13T09:00:00Z"


def test_a_switch_that_starts_over_has_its_point_where_the_last_run_began(tmp_path):
    [point] = _transcript_changes(tmp_path, OLD, OLD, OLD, NEW, NEW, OLD, NEW, NEW, NEW)
    assert point.iso() == "2026-09-16T09:00:00Z"


def test_one_old_session_is_enough_to_show_a_switch_in_a_new_project(tmp_path):
    [point] = _transcript_changes(tmp_path, OLD, NEW, NEW, NEW)
    assert point.keys == ["model"]
    assert point.changes == [{"key": "model", "agent": None, "old": OLD, "new": NEW}]


def test_a_start_that_shows_two_models_and_then_settles_is_not_a_switch(tmp_path):
    """No old value held for long enough to say the new one replaced it."""
    assert _transcript_changes(tmp_path, OLD, NEW, OLD, NEW, NEW, NEW) == []
    # But the one it settled on is the old value from then on.
    [point] = _transcript_changes(tmp_path / "then", OLD, NEW, OLD, NEW, NEW, NEW, THIRD, THIRD, THIRD)
    assert point.changes == [{"key": "model", "agent": None, "old": NEW, "new": THIRD}]


def test_the_effort_level_is_a_change_point_when_it_holds(tmp_path):
    efforts = ("medium", "medium", "medium", "high", "high", "high")
    [point] = _transcript_changes(tmp_path, *({"model": OLD, "effort": effort} for effort in efforts))
    assert point.keys == ["effortLevel"]
    assert point.changes == [{"key": "effortLevel", "agent": None, "old": "medium", "new": "high"}]
    assert point.label == "Effort level changed"


def test_a_session_that_records_no_effort_level_is_not_a_different_one(tmp_path):
    efforts = (None, None, None, "high", "high", "high")
    assert _transcript_changes(tmp_path, *({"model": OLD, "effort": effort} for effort in efforts)) == []
    efforts = ("high", "high", "high", None, None, None)
    assert _transcript_changes(tmp_path / "back", *({"model": OLD, "effort": effort} for effort in efforts)) == []


def test_changes_that_start_at_the_same_session_are_one_change_point(tmp_path):
    [point] = _transcript_changes(tmp_path, *_sized(1000, 1000, 1000), *_sized(2000, 2000, 2000, model=NEW))
    assert point.keys == ["model", "claude_md_chars"]
    assert [c["key"] for c in point.changes] == ["model", "claude_md_chars"]
    assert point.label == "Model and CLAUDE.md size changed"


def test_changes_that_start_at_different_sessions_are_separate_change_points(tmp_path):
    model, size = _transcript_changes(tmp_path, *_sized(1000, 1000, 1000), *_sized(1000, 2000, 2000, 2000, model=NEW))
    assert (model.keys, size.keys) == (["model"], ["claude_md_chars"])
    assert size.ts > model.ts


def test_sessions_in_different_projects_are_not_compared(tmp_path):
    """Two projects on steady, different models: three sessions in one,
    then three in the other, change nothing. The dashboard's corpus comes
    from the store, whose bundles have no ``project_dir``, so each is
    known by its slug."""
    from dataclasses import replace

    from claudeglass.corpus import load_corpus

    shop, docs = tmp_path / "C--work-shop", tmp_path / "C--work-docs"
    for folder in (shop, docs):
        folder.mkdir()
    for index in range(3):
        _session_file(shop, f"s{index}", claude_md_chars=1000, model=NEW, ts_prefix=f"2026-09-{10 + index}")
        _session_file(docs, f"s{index + 3}", claude_md_chars=4000, model=OLD, ts_prefix=f"2026-09-{13 + index}")
    corpus = load_corpus([shop, docs])
    assert change_points.change_points(tmp_path, corpus) == []
    corpus.sessions[:] = [replace(bundle, project_dir="") for bundle in corpus.sessions]
    assert change_points.change_points(tmp_path, corpus) == []


def test_sessions_under_either_case_of_the_drive_letter_are_one_project(tmp_path):
    from dataclasses import replace

    corpus = _history(tmp_path, OLD, OLD, OLD, NEW, NEW, NEW, folder="C--work-x")
    # As the dashboard's corpus has them: no project folder, only the slug.
    corpus.sessions[:] = [
        replace(bundle, slug="c--work-x" if bundle.session_id < "s03" else "C--work-x", project_dir="")
        for bundle in corpus.sessions
    ]
    [point] = change_points.change_points(tmp_path, corpus)
    assert point.project == snapshots.snapshot_project_key("C--work-x")


def test_a_config_change_stored_under_the_other_case_of_the_drive_letter_explains_the_sessions(tmp_path):
    from dataclasses import replace

    corpus = _history(tmp_path, OLD, OLD, OLD, NEW, NEW, NEW, folder="C--work-x")
    corpus.sessions[:] = [replace(bundle, slug="c--work-x") for bundle in corpus.sessions]
    canonical, legacy = snapshots.snapshot_project_keys("c--work-x")
    # Taken before the config hook upper-cased the drive letter: filed under
    # the lower-case key, in a settings layer the project's own files set.
    _snapshot(tmp_path, "20260912T100000Z", {"model": "sonnet"}, {"model": "user"}, slug=legacy)
    _snapshot(tmp_path, "20260912T200000Z", {"model": "opus"}, {"model": "project_local"}, slug=legacy)
    [point] = change_points.change_points(tmp_path, corpus)
    assert (point.source, point.project) == ("config", canonical)


def test_a_transcript_change_a_recorded_change_explains_is_not_a_second_point(tmp_path):
    """A model setting changed between two sessions, and the first one on
    the new model shows it: one change, not two (a second would cut the
    first one's after sessions short). What the setting doesn't explain
    stays."""
    corpus = _history(tmp_path, *_sized(1000, 1000, 1000), *_sized(2000, 2000, 2000, model=NEW))
    _snapshot(tmp_path, "20260912T100000Z", {"model": "sonnet"})
    _snapshot(tmp_path, "20260912T200000Z", {"model": "opus"})
    config, seen = change_points.change_points(tmp_path, corpus)
    assert config.source == "config"
    assert seen.source == "transcript" and seen.keys == ["claude_md_chars"]
    assert [c["key"] for c in seen.changes] == ["claude_md_chars"]
    assert seen.label == "CLAUDE.md size changed"


def test_a_change_in_another_project_or_outside_the_gap_explains_nothing(tmp_path):
    corpus = _history(tmp_path, OLD, OLD, OLD, NEW, NEW, NEW)
    # Before the first session: the change the sessions show came later.
    _snapshot(tmp_path, "20260901T000000Z", {"model": "sonnet"})
    _snapshot(tmp_path, "20260902T000000Z", {"model": "opus"})
    # In the gap, but in another project's own settings.
    project_layer = {"model": "project_local"}
    _snapshot(tmp_path, "20260912T100000Z", {"model": "opus"}, project_layer)
    _snapshot(tmp_path, "20260912T200000Z", {"model": "haiku"}, project_layer)
    sources = [p.source for p in change_points.change_points(tmp_path, corpus)]
    assert sources == ["config", "config", "transcript"]


def test_without_a_corpus_transcript_points_are_left_out(tmp_path):
    """change_points(config_dir) with no corpus is the "since my last
    change" caller's path -- it can't classify transcript signatures
    without one, so it just doesn't try."""
    assert change_points.change_points(tmp_path) == []
    assert change_points.change_points(tmp_path, corpus=None) == []

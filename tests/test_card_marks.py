"""``card_marks``: what the dashboard keeps of a card, and when a card's fix
has landed. Snapshots are built by hand, one per day, so the day a setting
moved is the day the test names."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from claudeglass import card_marks
from claudeglass import snapshots as snapshots_mod
from claudeglass.model import Recommendation, SettingChange
from claudeglass.snapshots import Snapshot

_D0 = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)


def _day(n: int) -> datetime:
    return _D0 + timedelta(days=n)


def _stamp(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _snap(day: int, *, slug: str = "proj-a", effective: dict | None = None, agents: dict | None = None, ts=None):
    data = {"schema_version": 2, "project_slug": slug, "effective": effective or {}, "agents": agents or {}}
    return Snapshot(path=Path(""), ts=ts if ts is not None else _stamp(_day(day)), data=data)


def _model(value: str) -> dict:
    return {"target": "settings", "key": "model", "agent": None, "value": value}


def _tools(agent: str, value) -> dict:
    return {"target": "agent", "key": "tools", "agent": agent, "value": value}


def _land(changes, snaps, first_shown):
    snapshots_mod._normalise_tool_lines(snaps)  # the form load_snapshots leaves
    return card_marks.landing(changes, snaps, snapshots_mod.canonical_project_keys([]), first_shown)


def test_a_card_lands_when_every_setting_it_names_moves_after_it_was_shown_and_stays():
    snaps = [_snap(0, effective={"model": "opus"}), _snap(3, effective={"model": "sonnet"})]
    shown = _day(0) + timedelta(hours=1)
    assert _land([_model("sonnet")], snaps, shown) == (_day(3), True)
    # A card asking for haiku: the setting moved, but not to the card's own value.
    assert _land([_model("haiku")], snaps, shown) == (_day(3), False)


def test_a_setting_moved_back_before_the_newest_snapshot_has_not_landed():
    shown = _day(0) + timedelta(hours=1)
    snaps = [
        _snap(0, effective={"model": "opus"}),
        _snap(3, effective={"model": "sonnet"}),
        _snap(5, effective={"model": "opus"}),
    ]
    assert _land([_model("sonnet")], snaps, shown) is None
    snaps.append(_snap(6, effective={"model": "sonnet"}))
    assert _land([_model("sonnet")], snaps, shown) == (_day(6), True)


def test_a_setting_already_moved_before_the_card_was_shown_has_not_landed():
    snaps = [_snap(0, effective={"model": "sonnet"})]
    assert _land([_model("sonnet")], snaps, _day(1)) is None
    # Unset when shown, set by the first snapshot after it: that is a move.
    assert _land([_model("sonnet")], snaps, _day(0) - timedelta(hours=1)) == (_day(0), True)


def test_every_change_of_a_card_must_move():
    snaps = [
        _snap(0, effective={"model": "opus"}, agents={"x": {"model": "opus"}}),
        _snap(3, effective={"model": "sonnet"}, agents={"x": {"model": "opus"}}),
    ]
    shown = _day(0) + timedelta(hours=1)
    both = [_model("sonnet"), {"target": "agent", "key": "model", "agent": "x", "value": "sonnet"}]
    assert _land(both, snaps, shown) is None
    assert _land(both[:1], snaps, shown) == (_day(3), True)


def test_a_tools_line_set_with_more_tools_than_the_card_asked_for_lands_but_not_exact():
    snaps = [
        _snap(0, agents={"x": {"source": "user"}}),
        _snap(2, agents={"x": {"source": "user", "tools": ["Read", "Grep", "Glob"]}}),
    ]
    shown = _day(0) + timedelta(hours=1)
    assert _land([_tools("x", ["Read", "Grep"])], snaps, shown) == (_day(2), False)
    assert _land([_tools("x", ["Read", "Grep", "Glob"])], snaps, shown) == (_day(2), True)


def test_a_tools_line_written_in_another_order_has_not_moved():
    snaps = [
        _snap(0, agents={"x": {"tools": ["Read", "Grep"]}}),
        _snap(2, agents={"x": {"tools": ["Grep", "Read"]}}),
        _snap(3, agents={"x": {"tools": "Read, Grep"}}),
    ]
    assert _land([_tools("x", ["Read"])], snaps, _day(0) + timedelta(hours=1)) is None


def test_no_changes_never_lands():
    snaps = [_snap(0, effective={"model": "opus"}), _snap(3, effective={"model": "sonnet"})]
    assert _land([], snaps, _day(0)) is None
    assert _land([_model("sonnet")], [], _day(0)) is None


def test_a_snapshot_with_an_unreadable_time_is_skipped():
    snaps = [
        _snap(0, effective={"model": "opus"}),
        _snap(1, effective={"model": "opus"}, ts="not a time"),
        _snap(3, effective={"model": "sonnet"}),
        _snap(4, effective={"model": "opus"}, ts="also not a time"),
    ]
    # The unreadable snapshots neither break the run nor start one, but the
    # newest merged snapshot (the last in the list) still sets "exact".
    found = _land([_model("sonnet")], snaps, _day(0) + timedelta(hours=1))
    assert found is not None and found[0] == _day(3)


def test_an_agent_recorded_only_in_another_project_is_read_through_the_merge():
    snaps = [
        _snap(0, slug="proj-a", effective={"model": "opus"}),
        _snap(1, slug="proj-b", agents={"x": {"source": "project", "tools": ["Read"]}}),
        _snap(3, slug="proj-a", effective={"model": "opus"}),
        _snap(4, slug="proj-b", agents={"x": {"source": "project", "tools": ["Read", "Grep"]}}),
        _snap(5, slug="proj-a", effective={"model": "opus"}),
    ]
    snaps.sort(key=lambda s: s.ts)
    canonical = snapshots_mod.canonical_project_keys(["proj-a", "proj-b"])
    shown = _day(2)
    # The newest snapshot (proj-a, day 5) has no agents of its own; the merge
    # brings proj-b's latest project agent in, so the tools line moved from day 4 onward.
    got = card_marks.landing([_tools("x", ["Read", "Grep"])], snaps, canonical, shown)
    assert got == (_day(4), True)


def test_a_setting_is_read_in_the_project_the_card_was_first_shown_in():
    # Project b holds sonnet all along, project a opus (the newest snapshot when the card
    # was first shown). Nobody changes anything; b starts another session.
    snaps = [
        _snap(0, slug="proj-b", effective={"model": "sonnet"}),
        _snap(1, slug="proj-a", effective={"model": "opus"}),
        _snap(3, slug="proj-b", effective={"model": "sonnet"}),
    ]
    assert _land([_model("sonnet")], snaps, _day(1) + timedelta(hours=1)) is None


def test_a_fix_made_in_that_project_lands_although_another_project_still_holds_the_old_value():
    snaps = [
        _snap(0, slug="proj-b", effective={"model": "opus"}),
        _snap(1, slug="proj-a", effective={"model": "opus"}),
        _snap(2, slug="proj-a", effective={"model": "sonnet"}),
        _snap(3, slug="proj-b", effective={"model": "opus"}),
    ]
    assert _land([_model("sonnet")], snaps, _day(1) + timedelta(hours=1)) == (_day(2), True)


def test_a_shared_cache_gives_the_same_answer():
    snaps = [_snap(0, effective={"model": "opus"}), _snap(3, effective={"model": "sonnet"})]
    shown = _day(0) + timedelta(hours=1)
    cache: dict = {}
    canonical = snapshots_mod.canonical_project_keys([])
    first = card_marks.landing([_model("sonnet")], snaps, canonical, shown, cache)
    assert cache and card_marks.landing([_model("sonnet")], snaps, canonical, shown, cache) == first


def test_flat_key_and_value_in_read_a_change_where_the_advice_reads_it():
    snap = _snap(0, effective={"model": "opus"}, agents={"x": {"model": "haiku"}})
    agent_change = {"target": "agent", "key": "model", "agent": "x", "value": "sonnet"}
    assert card_marks.flat_key(agent_change) == "agents.x.model"
    assert card_marks.flat_key(_model("sonnet")) == "effective.model"
    assert card_marks.value_in(snap, agent_change) == "haiku"
    assert card_marks.value_in(snap, _model("sonnet")) == "opus"
    # Any missing level is unset.
    assert card_marks.value_in(snap, {**agent_change, "agent": "nobody"}) is None
    assert card_marks.value_in(_snap(0, agents={"x": "not a map"}), agent_change) is None
    snap.data["agents"] = "not a map"
    assert card_marks.value_in(snap, agent_change) is None


def test_card_row_names_the_card_its_rule_agent_changes_and_saving():
    rec = Recommendation(
        id="spawn-tools-list",
        key="spawn-tools-list:x",
        agent_type="x",
        saving_usd=1.5,
        changes=[SettingChange(target="agent", key="tools", agent="x", value=["Read"], suggested="s")],
    )
    assert card_marks.card_row(rec) == {
        "key": "spawn-tools-list:x",
        "rule_id": "spawn-tools-list",
        "agent_type": "x",
        "changes": [{"target": "agent", "key": "tools", "agent": "x", "value": ["Read"]}],
        "saving_usd": 1.5,
    }
    bare = card_marks.card_row(Recommendation(id="habit", key="habit"))
    assert bare["changes"] == [] and bare["agent_type"] is None and bare["saving_usd"] is None

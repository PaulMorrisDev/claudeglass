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


def test_a_card_shown_before_any_snapshot_takes_the_first_one_as_what_was_set():
    shown = _day(1)
    # The setting was sonnet all along; the first snapshot comes after the card was shown.
    assert _land([_model("sonnet")], [_snap(3, effective={"model": "sonnet"})], shown) is None
    snaps = [_snap(3, effective={"model": "sonnet"}), _snap(4, effective={"model": "sonnet"})]
    assert _land([_model("sonnet")], snaps, shown) is None
    # Only a move after that first snapshot counts.
    snaps = [_snap(3, effective={"model": "opus"}), _snap(5, effective={"model": "sonnet"})]
    assert _land([_model("sonnet")], snaps, shown) == (_day(5), True)
    # A snapshot with an unreadable time is not the baseline.
    snaps = [_snap(2, effective={"model": "opus"}, ts="not a time"), _snap(3, effective={"model": "opus"}),
             _snap(5, effective={"model": "sonnet"})]
    assert _land([_model("sonnet")], snaps, shown) == (_day(5), True)


def test_every_change_of_a_card_must_move():
    snaps = [
        _snap(0, effective={"model": "opus"}, agents={"x": {"model": "opus"}}),
        _snap(3, effective={"model": "sonnet"}, agents={"x": {"model": "opus"}}),
    ]
    shown = _day(0) + timedelta(hours=1)
    both = [_model("sonnet"), {"target": "agent", "key": "model", "agent": "x", "value": "sonnet"}]
    assert _land(both, snaps, shown) is None
    assert _land(both[:1], snaps, shown) == (_day(3), True)


def test_a_card_with_two_changes_is_exact_only_when_both_hold_the_cards_own_value():
    snaps = [
        _snap(0, effective={"model": "opus"}, agents={"x": {"model": "opus"}}),
        _snap(3, effective={"model": "sonnet"}, agents={"x": {"model": "haiku"}}),
    ]
    shown = _day(0) + timedelta(hours=1)
    both = [_model("sonnet"), _agent_model("x", "sonnet")]
    # Both moved, but the agent to haiku, not to the card's sonnet.
    assert _land(both, snaps, shown) == (_day(3), False)
    snaps[1] = _snap(3, effective={"model": "sonnet"}, agents={"x": {"model": "sonnet"}})
    assert _land(both, snaps, shown) == (_day(3), True)


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


def test_every_place_that_says_the_store_can_be_purged_or_rebuilt_names_the_card_marks():
    root = Path(__file__).resolve().parents[1]
    places = {
        "docs/api.md": ("derived cache",),
        "docs/deploy.md": ("derived cache", "Downgrading rebuilds the store"),
        "src/claudeglass/cli.py": ("always a derived cache",),
    }
    for name, anchors in places.items():
        text = (root / name).read_text(encoding="utf-8").replace("\r\n", "\n")
        for anchor in anchors:
            assert anchor in text, (name, anchor)
            start = 0
            while (start := text.find(anchor, start)) != -1:
                # The paragraph, list item or docstring the sentence is in.
                begin = max(text.rfind("\n\n", 0, start) + 2, text.rfind("\n- ", 0, start) + 1)
                ends = [i for i in (text.find("\n\n", start), text.find("\n- ", start)) if i != -1]
                paragraph = " ".join(text[begin : min(ends) if ends else len(text)].split())
                assert "card marks" in paragraph, (name, anchor, paragraph[:80])
                start += len(anchor)


def _agent_model(agent: str, value: str) -> dict:
    return {"target": "agent", "key": "model", "agent": agent, "value": value}


def test_a_deleted_agent_has_not_moved_its_setting():
    snaps = [
        _snap(0, agents={"x": {"model": "opus", "source": "user"}}),
        _snap(3, agents={}),
    ]
    assert _land([_agent_model("x", "sonnet")], snaps, _day(1)) is None
    # The agent set to the card's value instead is a move.
    snaps[1] = _snap(3, agents={"x": {"model": "sonnet", "source": "user"}})
    assert _land([_agent_model("x", "sonnet")], snaps, _day(1)) == (_day(3), True)


def test_a_renamed_agent_has_not_moved_its_setting():
    snaps = [
        _snap(0, agents={"x": {"model": "opus", "source": "user"}}),
        _snap(3, agents={"x-new": {"model": "opus", "source": "user"}}),
    ]
    assert _land([_agent_model("x", "sonnet")], snaps, _day(1)) is None


def test_an_agent_that_was_absent_when_the_card_was_shown_can_still_move_once_it_is_there():
    snaps = [
        _snap(0, agents={}),
        _snap(3, agents={"x": {"model": "sonnet", "source": "user"}}),
    ]
    assert _land([_agent_model("x", "sonnet")], snaps, _day(1)) == (_day(3), True)
    # And an agent that is deleted again after that breaks the run.
    snaps.append(_snap(5, agents={}))
    assert _land([_agent_model("x", "sonnet")], snaps, _day(1)) is None


def _landing_by_remerging(changes, snaps, canonical, first_shown):
    """The rule as it was first written, and as the spec words it: the merged
    snapshot at each step is made again from every snapshot up to it. Slow
    (quadratic in the snapshots), kept as the reference the walk is held to."""
    if not changes:
        return None
    stamps = [snapshots_mod._parse_ts(s.ts) for s in snaps]

    def merged(i):
        return snapshots_mod.with_every_project_agents(snaps[: i + 1], canonical)

    start = None
    for i, when in enumerate(stamps):
        if when is not None and when <= first_shown:
            start = i
    if start is None:
        start = next((i for i, when in enumerate(stamps) if when is not None), None)
    if start is None:
        return None
    base = next((s for s in reversed(snaps[: start + 1]) if isinstance(s.data.get("effective"), dict)), snaps[start])
    label = snapshots_mod._canonical_label(base, canonical)
    newest, own = None, []
    for snap in snaps:
        if isinstance(snap.data.get("effective"), dict) and snapshots_mod._canonical_label(snap, canonical) == label:
            newest = snap
        own.append(newest)

    def read(i, change):
        snap = own[i] if change["target"] != "agent" and own[i] is not None else merged(i)
        return card_marks.value_in(snap, change)

    def gone(i, change):
        agents = merged(i).data.get("agents")
        return change["target"] == "agent" and not (isinstance(agents, dict) and change["agent"] in agents)

    before = [read(start, c) for c in changes]
    run = None
    for i in range(start + 1, len(snaps)):
        if stamps[i] is None:
            continue
        moved = all(
            not gone(i, c) and not snapshots_mod.same_value(card_marks.flat_key(c), old, read(i, c))
            for c, old in zip(changes, before)
        )
        if not moved:
            run = None
        elif run is None:
            run = stamps[i]
    if run is None:
        return None
    last = len(snaps) - 1
    return run, all(card_marks.already_set(c["key"], c["value"], read(last, c)) for c in changes)


def _history(rng):
    """A random view: snapshots of up to three projects, a day or so apart (some with a
    time that does not read, some without settings), each with a setting and two
    agents that come and go and change value."""
    slugs = ["proj-a", "proj-b", "proj-c"][: rng.randint(1, 3)]
    snaps, day = [], 0
    for _ in range(rng.randint(2, 14)):
        day += rng.choice([0, 1, 1, 2])  # now and then two snapshots at the same time
        agents = {}
        for name in ("x", "y"):
            if rng.random() < 0.8:
                agents[name] = {
                    "model": rng.choice(["opus", "sonnet", "haiku", None]),
                    "tools": rng.choice(["Read, Grep", "Grep, Read", "Read", None]),
                    "source": rng.choice(["user", "project"]),
                }
                agents[name] = {k: v for k, v in agents[name].items() if v is not None}
        snap = _snap(
            day,
            slug=rng.choice(slugs),
            effective={"model": rng.choice(["opus", "sonnet", "haiku"])},
            agents=agents,
            ts="not a time" if rng.random() < 0.08 else None,
        )
        if rng.random() < 0.1:
            del snap.data["effective"]
        snaps.append(snap)
    snaps.sort(key=lambda s: (s.ts == "not a time", s.ts))
    return snaps, slugs


def _a_card(rng):
    pool = [
        _model("sonnet"),
        _model("haiku"),
        _agent_model("x", "sonnet"),
        _agent_model("y", "haiku"),
        _tools("x", ["Read", "Grep"]),
        _tools("y", ["Read"]),
    ]
    return rng.sample(pool, rng.randint(1, 2))


def test_the_walk_gives_the_answers_the_remerged_rule_gives():
    import random

    rng = random.Random(20261113)
    landed = 0
    for _ in range(600):
        snaps, slugs = _history(rng)
        snapshots_mod._normalise_tool_lines(snaps)
        canonical = snapshots_mod.canonical_project_keys(slugs)
        cache: dict = {}  # shared by the cards of the view, as the route shares it
        for _card in range(4):
            changes = _a_card(rng)
            shown = _day(rng.randint(-1, 14)) + timedelta(hours=rng.randint(0, 20))
            expected = _landing_by_remerging(changes, snaps, canonical, shown)
            assert card_marks.landing(changes, snaps, canonical, shown, cache) == expected, (changes, shown)
            assert card_marks.landing(changes, snaps, canonical, shown) == expected
            landed += expected is not None
    assert landed > 100  # the cases are not all "has not landed"


def test_the_walk_does_not_merge_the_snapshots_again_at_every_step(monkeypatch):
    snaps = [_snap(day, slug=f"proj-{day % 4}", effective={"model": f"m{day // 3}"}, agents={"x": {"model": "opus"}})
             for day in range(400)]
    canonical = snapshots_mod.canonical_project_keys([f"proj-{n}" for n in range(4)])
    handed = []
    for name in ("with_every_project_agents", "latest_snapshot_per_project"):
        real = getattr(snapshots_mod, name)

        def counting(snapshots, *args, _real=real, **kwargs):
            handed.append(len(snapshots))
            return _real(snapshots, *args, **kwargs)

        monkeypatch.setattr(snapshots_mod, name, counting)
    cache: dict = {}
    for n in range(20):  # twenty cards of one view
        change = [_agent_model("x", f"m{n}")] if n % 2 else [_model("m9")]
        card_marks.landing(change, snaps, canonical, _day(n), cache)
    # Re-merging every prefix would hand over about 400 * 200 snapshots per card.
    assert sum(handed) <= 4 * len(snaps)


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

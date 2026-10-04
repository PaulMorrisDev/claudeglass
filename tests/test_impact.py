"""Did it work? Sessions before a change against sessions after it
(``impact``)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from claudeglass import impact
from claudeglass.change_points import ChangePoint, applies_to
from claudeglass.corpus import load_corpus
from claudeglass.impact import Measure, SessionFacts, _Transcript
from claudeglass.model import TranscriptResult, Turn
from claudeglass.pricing import load_pricing
from claudeglass.units import Units

from helpers import turn_line, write_jsonl

UNITS = Units(billing_mode="api", currency="USD")
CHANGE = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)


def _session(days: float, cost: float, *, startup: int = 20_000, agent_cost: float = 0.0) -> SessionFacts:
    spawns = [("Explore", _Transcript(cost=agent_cost, turns=3, startup_tokens=9000))] if agent_cost else []
    return SessionFacts(
        start=CHANGE + timedelta(days=days),
        main=_Transcript(cost=cost, turns=10, startup_tokens=startup, write_tokens=1000, rebuild_tokens=200),
        spawns=spawns,
    )


def _tasked(days: float, cost: float, task: str) -> SessionFacts:
    facts = _session(days, cost)
    facts.task = task
    return facts


def test_measures_follow_the_changed_keys():
    # A model change is judged on the tokens it spends first, then on cost.
    assert [m.key for m in impact.measures_for(ChangePoint(CHANGE, "apply", "x", keys=["model"]))] == [
        "tokens_per_session",
        "output_per_turn",
        "turns_per_session",
        "cost_per_turn",
        "cost_per_substantive_cycle",
        "cost_per_session",
    ]
    for key in ("effortLevel", "alwaysThinkingEnabled", "MAX_THINKING_TOKENS"):
        assert [m.key for m in impact.measures_for(ChangePoint(CHANGE, "apply", "x", keys=[key]))] == [
            "output_per_turn",
            "cost_per_turn",
            "cost_per_session",
        ], key
    # Fast mode changes the price and the speed, not the tokens.
    assert [m.key for m in impact.measures_for(ChangePoint(CHANGE, "apply", "x", keys=["fastMode"]))] == [
        "cost_per_turn",
        "cost_per_session",
    ]
    # A settings edit lists its keys alphabetically, so effort or fast mode
    # can come before the model: the tokens still come before the money.
    for keys in (
        ["effective.effortLevel", "effective.model"],
        ["effective.fastMode", "effective.model"],
        ["effective.alwaysThinkingEnabled", "effective.effortLevel", "effective.fastMode", "effective.model"],
    ):
        assert [m.key for m in impact.measures_for(ChangePoint(CHANGE, "config", "x", keys=keys))] == [
            "tokens_per_session",
            "output_per_turn",
            "turns_per_session",
            "cost_per_turn",
            "cost_per_substantive_cycle",
            "cost_per_session",
        ], keys
    assert [m.key for m in impact.measures_for(ChangePoint(CHANGE, "config", "x", keys=["fastMode", "effortLevel"]))] == [
        "output_per_turn",
        "cost_per_turn",
        "cost_per_session",
    ]
    agent = impact.measures_for(ChangePoint(CHANGE, "apply", "x", keys=["Explore: model"]))
    assert [(m.key, m.agent) for m in agent][:2] == [("agent_cost", "Explore"), ("agent_startup", "Explore")]
    # An agent's model change stays on that agent's cost and context.
    for key in ("Explore: model", "agents.Explore.model"):
        scoped = impact.measures_for(ChangePoint(CHANGE, "apply", "x", keys=[key]))
        assert [(m.key, m.agent) for m in scoped] == [
            ("agent_cost", "Explore"),
            ("agent_startup", "Explore"),
            ("cost_per_session", None),
        ], key
    # The settings layers name the same key.
    layered = impact.measures_for(ChangePoint(CHANGE, "config", "x", keys=["effective.model"]))
    assert [m.key for m in layered][0] == "tokens_per_session"
    ttl = impact.measures_for(ChangePoint(CHANGE, "config", "x", keys=["effective.promptCacheTtl"]))
    assert ttl[0].key == "rebuild_share"
    assert impact.measures_for(ChangePoint(CHANGE, "apply", "x"))[0].key == "startup_tokens"


def test_a_habit_you_started_is_measured_on_the_habits_it_names():
    # A prompting habit: how often you do it, and for the small requests
    # sent one at a time, their share as well.
    drip = impact.measures_for(ChangePoint(CHANGE, "habit", "x", keys=["habit.drip_feed"]))
    assert [m.key for m in drip] == ["prompting_habits", "drip_share", "cost_per_substantive_cycle", "cost_per_session"]
    other = impact.measures_for(ChangePoint(CHANGE, "habit", "x", keys=["habit.plan_first"]))
    assert [m.key for m in other] == ["prompting_habits", "cost_per_substantive_cycle", "cost_per_session"]
    # A playbook item or a recommendation has no habit rate: judge it on
    # the tokens a session uses, then on cost.
    for key in ("habit.split_large", "habit.model.default"):
        assert [m.key for m in impact.measures_for(ChangePoint(CHANGE, "habit", "x", keys=[key]))] == [
            "tokens_per_session",
            "cost_per_substantive_cycle",
            "cost_per_session",
        ], key


def test_compare_reports_a_drop_with_counts():
    sessions = [_session(-d, 2.0, agent_cost=1.0) for d in (1, 2, 3)] + [
        _session(d, 1.0, agent_cost=0.5) for d in (0.1, 0.2, 0.3, 0.4)
    ]
    point = ChangePoint(CHANGE, "apply", "Applied profile cheap", keys=["Explore: model"])
    result = impact.compare(point, sessions, UNITS, now=CHANGE + timedelta(days=1))
    assert result["enough"]
    assert (result["before_sessions"], result["after_sessions"]) == (3, 4)
    lead = result["measures"][0]
    assert lead["label"] == "Explore: cost per spawn"
    assert lead["change_pct"] == -50.0 and lead["direction"] == "lower"
    assert result["verdict"].startswith("Explore: cost per spawn fell 50%")


def test_a_change_in_one_project_is_judged_on_that_projects_sessions():
    sessions = [_session(-d, 2.0) for d in (1, 2, 3)] + [_session(d, 1.0) for d in (0.1, 0.2, 0.3)]
    elsewhere = [_session(-d, 9.0) for d in (1.5, 2.5)] + [_session(0.5, 9.0)]
    for facts in sessions:
        facts.project = "slug:mine"
    for facts in elsewhere:
        facts.project = "slug:other"
    point = ChangePoint(CHANGE, "config", "Your settings changed", keys=["effective.model"], project="slug:mine")
    result = impact.compare(point, sessions + elsewhere, UNITS, now=CHANGE + timedelta(days=1))
    assert (result["before_sessions"], result["after_sessions"]) == (3, 3)
    everywhere = ChangePoint(CHANGE, "config", "Your settings changed", keys=["effective.model"])
    result = impact.compare(everywhere, sessions + elsewhere, UNITS, now=CHANGE + timedelta(days=1))
    assert (result["before_sessions"], result["after_sessions"]) == (5, 4)


def test_too_few_sessions_after_gives_no_verdict():
    sessions = [_session(-d, 2.0) for d in (1, 2, 3)] + [_session(0.1, 1.0)]
    result = impact.compare(ChangePoint(CHANGE, "apply", "x", keys=["model"]), sessions, UNITS)
    assert not result["enough"]
    assert "1 so far" in result["verdict"]


def test_before_stops_at_the_previous_change_and_after_at_the_next():
    sessions = [_session(-d, 5.0) for d in (4, 5)] + [_session(-d, 2.0) for d in (0.5, 1, 1.5)] + [
        _session(d, 1.0) for d in (0.1, 0.2, 0.3)
    ] + [_session(3, 9.0)]
    points = [
        ChangePoint(CHANGE - timedelta(days=2), "apply", "first"),
        ChangePoint(CHANGE, "apply", "second"),
        ChangePoint(CHANGE + timedelta(days=2), "apply", "third"),
    ]
    newest_first = impact.impact(points, sessions, UNITS)
    second = newest_first[1]
    assert second["change"]["label"] == "second"
    assert (second["before_sessions"], second["after_sessions"]) == (3, 3)
    cost = next(row for row in second["measures"] if row["label"] == "Cost per session")
    assert cost["before"] == "2.00 USD" and cost["after"] == "1.00 USD"


def _three_changes() -> tuple[list[SessionFacts], list[ChangePoint]]:
    sessions = [_session(-d, 5.0) for d in (4, 5)] + [_session(-d, 2.0) for d in (0.5, 1, 1.5)] + [
        _session(d, 1.0) for d in (0.1, 0.2, 0.3)
    ] + [_session(3, 9.0)]
    points = [
        ChangePoint(CHANGE - timedelta(days=2), "apply", "first"),
        ChangePoint(CHANGE, "apply", "second"),
        ChangePoint(CHANGE + timedelta(days=2), "apply", "third"),
    ]
    return sessions, points


def test_listed_compares_only_the_points_it_accepts_newest_first():
    sessions, points = _three_changes()
    everything = {row["change"]["label"]: row for row in impact.impact(points, sessions, UNITS)}
    rows = impact.impact(points, sessions, UNITS, listed=lambda p: p.label != "second")
    assert [row["change"]["label"] for row in rows] == ["third", "first"]
    # Each one's neighbours are still every point's, so its sides and its
    # measures are what the full run gave it.
    for row in rows:
        full = everything[row["change"]["label"]]
        assert (row["before_sessions"], row["after_sessions"]) == (full["before_sessions"], full["after_sessions"])
        assert row["measures"] == full["measures"] and row["verdict"] == full["verdict"]


def test_listed_leaves_a_point_it_rejects_as_a_neighbour():
    """``second`` is not listed, but it still cuts ``first``'s after side."""
    sessions, points = _three_changes()
    (first,) = impact.impact(points, sessions, UNITS, listed=lambda p: p.label == "first")
    assert first["after_sessions"] == 3  # not the 6 the sessions after it would give with no ``second``
    alone, = impact.impact(points[:1], sessions, UNITS)
    assert alone["after_sessions"] > first["after_sessions"]


def test_no_limit_returns_every_point_and_a_limit_counts_only_the_listed_ones():
    sessions, points = _three_changes()
    assert [r["change"]["label"] for r in impact.impact(points, sessions, UNITS, limit=None)] == [
        "third",
        "second",
        "first",
    ]
    assert [r["change"]["label"] for r in impact.impact(points, sessions, UNITS, limit=2)] == ["third", "second"]
    rows = impact.impact(points, sessions, UNITS, limit=1, listed=lambda p: p.label != "third")
    assert [r["change"]["label"] for r in rows] == ["second"]
    assert impact.impact(points, sessions, UNITS, limit=None, listed=lambda p: False) == []


def test_changes_made_together_share_their_before_and_after():
    """One apply that wrote two files gives two change points seconds
    apart; neither cuts the other's comparison to nothing."""
    sessions = [_session(-d, 2.0) for d in (1, 2, 3)] + [_session(d, 1.0) for d in (0.1, 0.2, 0.3)]
    points = [
        ChangePoint(CHANGE, "apply", "first"),
        ChangePoint(CHANGE + timedelta(seconds=1), "apply", "second"),
    ]
    for result in impact.impact(points, sessions, UNITS):
        assert (result["before_sessions"], result["after_sessions"]) == (3, 3), result["change"]["label"]
        assert result["enough"]


def test_changes_with_too_few_sessions_between_them_share_their_before_and_after():
    """Metrics capture turned on everywhere, then a model change one
    project's sessions show 12 minutes later, with no session between:
    cut at each other, capture has nothing after it and the model change
    nothing before it, however many sessions run since."""
    sessions = [_session(-d, 2.0) for d in (1, 2, 3)] + [_session(d, 1.0) for d in (0.1, 0.2, 0.3)]
    for facts in sessions:
        facts.project = "slug:mine"
    capture = ChangePoint(CHANGE - timedelta(minutes=12), "capture", "Metrics capture level: Deep",
                          keys=["capture.level"])
    model = ChangePoint(CHANGE, "transcript", "Model changed", keys=["model"], project="slug:mine")
    assert impact.neighbours([capture, model], model) == (capture, None)
    assert impact.neighbours([capture, model], model, sessions) == (None, None)
    for result in impact.impact([capture, model], sessions, UNITS):
        assert (result["before_sessions"], result["after_sessions"]) == (3, 3), result["change"]["label"]
        assert result["enough"]


def test_enough_sessions_between_two_changes_keep_them_apart():
    between = [_session(-d / 10, 3.0) for d in (1, 2, 3)]
    sessions = [_session(-d, 2.0) for d in (1, 2, 3)] + between + [_session(d, 1.0) for d in (0.1, 0.2, 0.3)]
    first = ChangePoint(CHANGE - timedelta(hours=12), "apply", "first")
    second = ChangePoint(CHANGE, "apply", "second")
    assert impact.neighbours([first, second], second, sessions) == (first, None)
    assert impact.neighbours([first, second], second, sessions[:-4]) == (None, None)
    # Only sessions both changes apply to count: two in another project
    # don't keep a change in this one apart.
    second.project = "slug:mine"
    for facts in sessions:
        facts.project = "slug:mine"
    assert impact.neighbours([first, second], second, sessions) == (first, None)
    for facts in between[:2]:
        facts.project = "slug:other"
    assert impact.neighbours([first, second], second, sessions) == (None, None)


# -- bounds: a change in one project cuts that project's window and no other's --


def _in(project: str, days: float, cost: float = 1.0) -> SessionFacts:
    facts = _session(days, cost)
    facts.project = project
    return facts


def _count(sessions: list[SessionFacts], project: str) -> int:
    return sum(1 for s in sessions if s.project == project)


def test_a_change_in_one_project_cuts_a_global_changes_window_there_and_nowhere_else():
    """A change to every project, with a change in project X three days
    before it and another two days after it. X's sessions are read between
    those two; Y's, which neither change touched, across the whole span."""
    earlier = ChangePoint(CHANGE - timedelta(days=3), "apply", "x earlier", keys=["model"], project="slug:x")
    everywhere = ChangePoint(CHANGE, "config", "everywhere", keys=["effective.model"])
    later = ChangePoint(CHANGE + timedelta(days=2), "apply", "x later", keys=["model"], project="slug:x")
    points = [earlier, everywhere, later]
    days = (-4, -2.5, -2, -1.5, -1, 0.5, 1, 1.5, 2.5, 3)
    sessions = sorted([_in("slug:x", d) for d in days] + [_in("slug:y", d) for d in days], key=lambda s: s.start)
    now = CHANGE + timedelta(days=9)

    previous, following, per_project = impact.bounds(points, everywhere, sessions)
    assert (previous, following) == (None, None)
    assert per_project == {"slug:x": (earlier, later), "slug:y": (None, None)}
    before, after = impact.sides(
        everywhere, sessions, previous=previous, following=following, per_project=per_project, now=now
    )
    # Before: X from its earlier change on, Y back the full 14 days.
    assert (_count(before, "slug:x"), _count(before, "slug:y")) == (4, 5)
    # After: X until its later change, Y to now.
    assert (_count(after, "slug:x"), _count(after, "slug:y")) == (3, 5)
    result = impact.compare(
        everywhere, sessions, UNITS, previous=previous, following=following, per_project=per_project, now=now
    )
    assert (result["before_sessions"], result["after_sessions"]) == (9, 8)
    # The nearest changes that apply anywhere cut every project alike,
    # which is what this replaces.
    cut_everywhere = impact.neighbours(points, everywhere, sessions)
    assert cut_everywhere == (earlier, later)
    before, after = impact.sides(everywhere, sessions, previous=earlier, following=later, now=now)
    assert (_count(before, "slug:y"), _count(after, "slug:y")) == (4, 3)


def test_the_bounds_of_a_change_in_one_project_are_its_neighbours():
    everywhere = ChangePoint(CHANGE - timedelta(days=3), "config", "everywhere", keys=["effective.model"])
    mine = ChangePoint(CHANGE, "apply", "mine", keys=["model"], project="slug:x")
    elsewhere = ChangePoint(CHANGE + timedelta(days=1), "apply", "elsewhere", keys=["model"], project="slug:y")
    last = ChangePoint(CHANGE + timedelta(days=2), "config", "last", keys=["effective.model"])
    points = [everywhere, mine, elsewhere, last]
    sessions = [_in("slug:x", d) for d in (-2, -1.5, -1, 0.5, 1, 1.5, 2.5)]
    sessions += [_in("slug:y", d) for d in (-2, -1, 0.5, 1.5)]
    previous, following, per_project = impact.bounds(points, mine, sessions)
    assert (previous, following) == impact.neighbours(points, mine, sessions) == (everywhere, last)
    assert per_project is None


def test_sessions_with_no_project_use_the_changes_to_every_project():
    """A session the project of which isn't known isn't cut by a change
    made in a project, and a project with no sessions gets no bounds."""
    first = ChangePoint(CHANGE - timedelta(days=3), "config", "first", keys=["effective.model"])
    mine = ChangePoint(CHANGE, "apply", "mine", keys=["model"], project="slug:gone")
    last = ChangePoint(CHANGE + timedelta(days=2), "config", "last", keys=["effective.model"])
    point = ChangePoint(CHANGE - timedelta(days=1), "config", "point", keys=["effective.model"])
    sessions = [_session(d, 1.0) for d in (-2.5, -2, -1.5, -0.5, -0.25, 0.5, 1, 1.5, 2.5)]
    previous, following, per_project = impact.bounds([first, point, mine, last], point, sessions)
    assert (previous, following) == (first, last)
    assert per_project == {}


def test_the_all_projects_reading_is_the_project_readings_put_together():
    """Every change, judged on every project's sessions at once, reads as
    the sum of the same change judged on each project's sessions alone."""
    first = ChangePoint(CHANGE, "config", "first", keys=["effective.model"])
    y_only = ChangePoint(CHANGE + timedelta(days=1), "apply", "y only", keys=["model"], project="slug:y")
    x_only = ChangePoint(CHANGE + timedelta(days=2), "apply", "x only", keys=["model"], project="slug:x")
    last = ChangePoint(CHANGE + timedelta(days=4), "config", "last", keys=["effective.model"])
    points = [first, y_only, x_only, last]
    x_days = (-3, -2, -1, 0.5, 1, 1.5, 2.5, 3, 3.5, 4.5, 5)
    y_days = (-3, -2, -1, 0.2, 0.4, 0.6, 0.8, 1.5, 2, 3, 3.5, 4.5, 5, 6)
    sessions = sorted([_in("slug:x", d) for d in x_days] + [_in("slug:y", d) for d in y_days], key=lambda s: s.start)

    everything = {r["change"]["label"]: r for r in impact.impact(points, sessions, UNITS)}
    views = []
    for project in ("slug:x", "slug:y"):
        mine = [s for s in sessions if s.project == project]
        shown = [p for p in points if applies_to(p, project)]
        views.append({r["change"]["label"]: r for r in impact.impact(shown, mine, UNITS)})
    assert set(everything) == {"first", "y only", "x only", "last"}
    for label, row in everything.items():
        seen = [view[label] for view in views if label in view]
        assert row["before_sessions"] == sum(r["before_sessions"] for r in seen), label
        assert row["after_sessions"] == sum(r["after_sessions"] for r in seen), label
    # Not the same as one project's change cutting the other's: X's
    # sessions run to its own change two days on, Y's to its own at one.
    assert (everything["first"]["before_sessions"], everything["first"]["after_sessions"]) == (6, 7)
    assert (everything["last"]["before_sessions"], everything["last"]["after_sessions"]) == (7, 5)


# -- one project, two spellings of its drive letter ---------------------------


def test_session_facts_carry_every_key_a_drive_letter_project_goes_by(tmp_path):
    from claudeglass import snapshots

    def facts_in(folder: str):
        project_dir = tmp_path / folder
        project_dir.mkdir()
        write_jsonl(
            project_dir / "s.jsonl",
            [turn_line(timestamp=ts) for ts in ("2026-09-18T12:00:00.000Z", "2026-09-18T12:05:00.000Z")],
        )
        [facts] = impact.session_facts(load_corpus([project_dir]), load_pricing())
        return facts

    lower = facts_in("c--Dev-X")
    assert lower.keys == snapshots.snapshot_project_keys("c--Dev-X")
    assert lower.project == lower.keys[0] == snapshots.snapshot_project_key("C--Dev-X")
    assert lower.keys[0] != lower.keys[1]
    plain = facts_in("proj")
    assert plain.keys == snapshots.snapshot_project_keys("proj")
    assert plain.project == plain.keys[0] == snapshots.snapshot_project_key("proj")


def test_a_change_applies_to_a_session_under_either_spelling_of_its_drive_letter():
    from claudeglass import snapshots

    canonical, legacy = snapshots.snapshot_project_keys("c--Dev-X")
    sessions = [_session(d, 1.0) for d in (-3, -2, -1, 0.1, 0.2, 0.3)]
    for facts in sessions:
        facts.project, facts.keys = canonical, (canonical, legacy)
    now = CHANGE + timedelta(days=1)
    for key in (canonical, legacy):
        point = ChangePoint(CHANGE, "apply", "x", keys=["model"], project=key)
        result = impact.compare(point, sessions, UNITS, now=now)
        assert (result["before_sessions"], result["after_sessions"]) == (3, 3), key
    elsewhere = ChangePoint(CHANGE, "apply", "x", keys=["model"], project=snapshots.snapshot_project_key("c--Dev-Y"))
    assert impact.sides(elsewhere, sessions, now=now) == ([], [])
    # And a change under either spelling keeps another apart from this one
    # only when enough of the project's sessions ran between them.
    first = ChangePoint(CHANGE - timedelta(hours=12), "apply", "first", keys=["model"])
    second = ChangePoint(CHANGE, "apply", "second", keys=["model"], project=legacy)
    between = [_session(-d / 10, 3.0) for d in (1, 2, 3)]
    for facts in between:
        facts.project, facts.keys = canonical, (canonical, legacy)
    assert impact.neighbours([first, second], second, sessions + between) == (first, None)
    assert impact.neighbours([first, second], second, sessions + between[:2]) == (None, None)


def test_too_few_sessions_before_says_new_sessions_wont_fill_it():
    sessions = [_session(-1, 2.0)] + [_session(d, 1.0) for d in (0.1, 0.2, 0.3)]
    result = impact.compare(ChangePoint(CHANGE, "apply", "x", keys=["model"]), sessions, UNITS)
    assert not result["enough"]
    assert result["verdict"] == (
        "Too few sessions before the change to compare: 1 of the 3 needed. "
        "Only sessions started before it count here."
    )


def test_without_is_asked_about_each_change_with_its_own_sides():
    """``without`` (counterfactual.for_impact) gets each change point and
    the sessions on each side of it; its answer is the row's "without"."""
    sessions = [_session(-d, 2.0) for d in (1, 2, 3)] + [_session(d, 1.0) for d in (0.1, 0.2, 0.3)]
    point = ChangePoint(CHANGE, "apply", "x")
    seen = []

    def without(p, before, after):
        seen.append((p, len(before), len(after)))
        return {"text": "Without this change: about 6.00 USD."}

    [result] = impact.impact([point], sessions, UNITS, without=without)
    assert seen == [(point, 3, 3)]
    assert result["without"] == {"text": "Without this change: about 6.00 USD."}
    assert impact.compare(point, sessions, UNITS)["without"] is None


def test_quality_compares_the_changed_agents_runs_before_and_after():
    from claudeglass import quality

    def runs(errors: int) -> list:
        return [quality.Run(group="Explore", kind="subagent", replies=10, tool_calls=50, tool_errors=errors,
                            cut_off=False)]

    sessions = [_session(-d / 10, 2.0, agent_cost=1.0) for d in range(1, 7)]
    sessions += [_session(d / 10, 1.0, agent_cost=0.5) for d in range(1, 7)]
    for i, session in enumerate(sessions):
        session.runs = runs(1 if i < 6 else 10) + [quality.Run(replies=5)]
    point = ChangePoint(CHANGE, "apply", "Applied profile cheap", keys=["Explore: model"])
    assert impact.quality_groups(point) == ["Explore"]
    assert impact.quality_groups(ChangePoint(CHANGE, "apply", "x", keys=["model"])) == [quality.MAIN]
    group = impact.compare(point, sessions, UNITS, now=CHANGE + timedelta(days=1))["quality"][0]
    assert (group["label"], group["before_runs"], group["after_runs"]) == ("Explore", 6, 6)
    assert group["verdict"].startswith("Quality looks worse: tool calls that failed rose from 2.0% to 20%")
    cost = next(row for row in group["signals"] if row["key"] == "cost")
    assert cost["label_key"] == "no_clear_change"


def test_a_group_with_too_few_runs_is_not_judged():
    from claudeglass import quality

    sessions = [_session(-d / 10, 2.0) for d in range(1, 7)] + [_session(0.1, 1.0)]
    for session in sessions:
        session.runs = [quality.Run(replies=5, tool_calls=20)]
    group = impact.compare(ChangePoint(CHANGE, "apply", "x", keys=["model"]), sessions, UNITS)["quality"][0]
    assert group["judged"] is False and group["min_runs"] == quality.MIN_RUNS
    assert "6 before and 1 after" in group["verdict"]


def test_scheduled_main_sessions_are_left_out_of_the_quality_check():
    from claudeglass import quality

    sessions = [_session(-d / 10, 2.0) for d in range(1, 7)] + [_session(d / 10, 1.0) for d in range(1, 7)]
    for i, session in enumerate(sessions):
        session.runs = [quality.Run(replies=5, tool_calls=20, scheduled=i % 2 == 1)]
    group = impact.compare(ChangePoint(CHANGE, "apply", "x", keys=["model"]), sessions, UNITS)["quality"][0]
    assert (group["before_runs"], group["after_runs"]) == (3, 3)


# -- a model change is judged on tokens as well as money ------------------------


def _widgets(tmp_path):
    """A rate card with two versions of one family, the newer at twice the
    price per token."""
    card = tmp_path / "pricing.toml"
    card.write_text(
        'version = "2099-01-01-test"\ncurrency = "USD"\nsource_url = "https://example.invalid/pricing"\n'
        'retrieved = "2099-01-01"\nnotes = "Two versions of one family, the newer dearer per token."\n\n'
        '[models."claude-widget-4"]\naliases = []\ninput = 1.0\noutput = 5.0\n'
        "cache_write_5m = 1.25\ncache_write_1h = 2.0\ncache_read = 0.1\n\n"
        '[models."claude-widget-4-1"]\naliases = []\ninput = 2.0\noutput = 10.0\n'
        "cache_write_5m = 2.5\ncache_write_1h = 4.0\ncache_read = 0.2\n",
        encoding="utf-8",
    )
    return load_pricing(path=card)


def _widget_session(pricing, days: float, model: str, *, output: int = 400, replies: int = 4) -> SessionFacts:
    """A session of ``replies`` replies on ``model``, each reading 32,100 tokens and writing ``output``."""
    turns = [
        Turn(
            message_id=f"{model}-{days}-{n}",
            turn_index=n + 1,
            model=model,
            input_tokens=100,
            cache_creation_tokens=2_000,
            cc_5m=2_000,
            cache_read_tokens=30_000,
            output_tokens=output,
        )
        for n in range(replies)
    ]
    return SessionFacts(
        start=CHANGE + timedelta(days=days), main=impact._transcript(TranscriptResult(turns=turns), pricing)
    )


def test_a_dearer_version_with_the_same_usage_moves_cost_but_not_tokens(tmp_path):
    pricing = _widgets(tmp_path)
    before = [_widget_session(pricing, -d, "claude-widget-4") for d in (1, 2, 3)]
    after = [_widget_session(pricing, d, "claude-widget-4-1") for d in (0.1, 0.2, 0.3, 0.4)]
    point = ChangePoint(CHANGE, "transcript", "Model changed", keys=["model"])
    result = impact.compare(point, before + after, UNITS, now=CHANGE + timedelta(days=1))
    assert result["enough"]
    by_key = {m["key"]: m for m in result["measures"]}
    # Every measure is there; the one the test is surest of leads, then the rest as listed.
    assert list(by_key) == [
        "cost_per_turn", "tokens_per_session", "output_per_turn", "turns_per_session", "cost_per_substantive_cycle",
        "cost_per_session",
    ]
    assert result["lead"] == "cost_per_turn"
    # Same tokens, replies and output per reply before and after: nothing to see.
    for key in ("tokens_per_session", "output_per_turn", "turns_per_session"):
        assert by_key[key]["change_pct"] == 0.0 and by_key[key]["direction"] == "same", key
        assert by_key[key]["label_key"] == "no_clear_change", key
    # Twice the price for them: cost per reply and per session double.
    for key in ("cost_per_turn", "cost_per_session"):
        assert by_key[key]["change_pct"] == 100.0 and by_key[key]["direction"] == "higher", key
        assert by_key[key]["label_key"] == "higher", key
    # Read in their own units, and lower is better for each.
    assert [(by_key[k]["kind"], by_key[k]["before"]) for k in ("tokens_per_session", "output_per_turn", "turns_per_session")] == [
        ("tokens", "130,000 tokens"), ("tokens", "400 tokens"), ("count", "4.0"),
    ]
    assert all(row["better"] == "lower" for row in by_key.values())
    # The headline is the one that moved: usage didn't, so it is the price.
    assert result["verdict"].startswith("Cost per reply rose 100%, from ")
    assert result["verdict"].endswith("(3 sessions before, 4 after).")


def test_a_version_that_writes_half_as_much_a_reply_reads_lower_though_it_costs_more(tmp_path):
    pricing = _widgets(tmp_path)
    before = [_widget_session(pricing, -d, "claude-widget-4", output=400) for d in (1, 2, 3)]
    after = [_widget_session(pricing, d, "claude-widget-4-1", output=200) for d in (0.1, 0.2, 0.3, 0.4)]
    point = ChangePoint(CHANGE, "transcript", "Model changed", keys=["model"])
    result = impact.compare(point, before + after, UNITS, now=CHANGE + timedelta(days=1))
    assert result["enough"]
    by_key = {m["key"]: m for m in result["measures"]}
    # Half the output a reply: a clear fall.
    output = by_key["output_per_turn"]
    assert (output["before"], output["after"]) == ("400 tokens", "200 tokens")
    assert output["change_pct"] == -50.0 and output["direction"] == "lower"
    assert output["label_key"] == "lower"
    # Cache reads dwarf the output, so a session's tokens barely fall: too little to lead with,
    # however sure the test is of it, so the output that halved does.
    tokens = by_key["tokens_per_session"]
    assert (tokens["before"], tokens["after"]) == ("130,000 tokens", "129,200 tokens")
    assert tokens["change_pct"] == -0.6 and tokens["direction"] == "same"
    assert result["lead"] == "output_per_turn" and result["measures"][0] is output
    assert result["verdict"] == "Output tokens per reply fell 50%, from 400 tokens to 200 tokens (3 sessions before, 4 after)."
    assert by_key["turns_per_session"]["direction"] == "same"
    # Twice the price per token still costs more a reply, for all the shorter replies.
    for key in ("cost_per_turn", "cost_per_session"):
        assert by_key[key]["change_pct"] == 73.7 and by_key[key]["direction"] == "higher", key
        assert by_key[key]["label_key"] == "higher", key


def test_fewer_output_tokens_a_reply_after_a_change_read_as_lower():
    def session(days: float, output: int) -> SessionFacts:
        return SessionFacts(
            start=CHANGE + timedelta(days=days),
            main=_Transcript(cost=1.0, turns=10, output_tokens=10 * output, total_tokens=10 * (output + 30_000)),
        )

    before = [session(-d, output) for d, output in ((1, 380), (2, 400), (3, 420))]
    after = [session(d, output) for d, output in ((0.1, 190), (0.2, 200), (0.3, 210), (0.4, 200))]
    point = ChangePoint(CHANGE, "transcript", "Effort level changed", keys=["effortLevel"])
    result = impact.compare(point, before + after, UNITS, now=CHANGE + timedelta(days=1))
    assert [m["key"] for m in result["measures"]] == ["output_per_turn", "cost_per_turn", "cost_per_session"]
    row = result["measures"][0]
    assert (row["kind"], row["better"]) == ("tokens", "lower")
    assert (row["before"], row["after"]) == ("400 tokens", "200 tokens")
    assert row["change_pct"] == -50.0 and row["direction"] == "lower"
    assert row["label_key"] == "lower"
    assert result["verdict"].startswith("Output tokens per reply fell 50%, from 400 tokens to 200 tokens")
    # Cost per session didn't move, so only the tokens did.
    assert result["measures"][-1]["direction"] == "same"


def test_a_transcript_counts_output_once_and_every_token_read_or_written():
    def turn(index: int, **fields) -> Turn:
        return Turn(message_id=f"m{index}", turn_index=index, model="claude-sonnet-5", **fields)

    result = TranscriptResult(
        turns=[
            # Not a priced reply: counted nowhere.
            turn(0, input_tokens=9_000, cache_creation_tokens=9_000, cache_read_tokens=9_000, output_tokens=9_000),
            turn(1, input_tokens=100, cache_creation_tokens=2_000, cache_read_tokens=30_000, output_tokens=400,
                 thinking_tokens=150),
            turn(2, input_tokens=10, cache_creation_tokens=500, cache_read_tokens=32_000, output_tokens=250,
                 thinking_tokens=250),
            # The estimated request that wrote a compaction's summary: spend, not a reply.
            turn(3, is_synthetic=True, estimated="compaction", input_tokens=1_000, cache_read_tokens=32_000,
                 output_tokens=8_000),
        ]
    )
    facts = impact._transcript(result, load_pricing())
    assert facts.turns == 2
    # The thinking is inside output_tokens already: 400 + 250, not + 150 + 250.
    # A compaction's output counts in the total, never in the replies' output.
    assert facts.output_tokens == 650
    assert facts.total_tokens == (
        (100 + 2_000 + 30_000 + 400) + (10 + 500 + 32_000 + 250) + (1_000 + 32_000 + 8_000)
    )


def test_tokens_per_session_counts_the_spawns_but_output_and_replies_are_the_main_sessions():
    def session(days: float) -> SessionFacts:
        return SessionFacts(
            start=CHANGE + timedelta(days=days),
            main=_Transcript(turns=10, output_tokens=1_000, total_tokens=50_000),
            spawns=[("Explore", _Transcript(turns=3, output_tokens=9_000, total_tokens=20_000))],
        )

    sessions = [session(d) for d in (0.1, 0.2, 0.3)]
    assert sessions[0].tokens == 70_000
    assert impact._value(impact._TOKENS, sessions) == (70_000.0, 3)
    assert impact._value(impact._OUTPUT, sessions) == (100.0, 3)
    assert impact._value(impact._REPLIES, sessions) == (10.0, 3)


def test_session_facts_carries_a_sessions_token_totals(tmp_path):
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    write_jsonl(
        project_dir / "session-abc.jsonl",
        [
            turn_line(timestamp=ts, input_tokens=100, cache_read_input_tokens=1_000, output_tokens=50)
            for ts in ("2026-09-18T12:00:00.000Z", "2026-09-18T12:05:00.000Z")
        ],
    )
    [facts] = impact.session_facts(load_corpus([project_dir]), load_pricing())
    assert (facts.main.turns, facts.main.output_tokens) == (2, 100)
    assert facts.main.total_tokens == 2 * (100 + 1_000 + 50) == facts.tokens


# -- metrics capture changes ---------------------------------------------------


def _captured(days: float, chars: int, messages: int, tagged: int) -> SessionFacts:
    return SessionFacts(
        start=CHANGE + timedelta(days=days),
        main=_Transcript(cost=1.0, turns=messages, capture_chars=chars),
        spawns=[("Explore", _Transcript(cost=0.1, turns=2, capture_chars=chars // 2))],
        messages=messages,
        tagged=tagged,
    )


def test_a_coaching_change_is_measured_by_the_prompting_habits_it_warns_about():
    point = ChangePoint(CHANGE, "capture", "Turned coaching notes on", keys=["capture.coaching"])
    assert [m.key for m in impact.measures_for(point)] == [
        "prompting_habits", "drip_share", "capture_tokens", "cost_per_session",
    ]

    def session(days, habits, drip, messages=10):
        facts = _captured(days, 0, messages, 0)
        facts.habits, facts.drip_messages = habits, drip
        return facts

    before = [session(-d, 3, 4) for d in (1, 2, 3)]
    after = [session(d, 1, 0) for d in (0.1, 0.2, 0.3)]
    assert impact._value(impact._HABITS, before) == (30.0, 3)
    assert impact._value(impact._DRIP, after) == (0.0, 3)
    result = impact.compare(point, before + after, UNITS, now=CHANGE + timedelta(days=1))
    habits_row = result["measures"][0]
    assert habits_row["label"] == "Prompting habits per 100 of your messages" and habits_row["better"] == "lower"


def test_a_capture_change_is_measured_by_what_capture_adds_and_how_much_was_tagged():
    point = ChangePoint(CHANGE, "capture", "Turned metrics capture on: Essentials", keys=["capture.level"])
    assert [m.key for m in impact.measures_for(point)] == ["capture_tokens", "tagged_share", "cost_per_session"]
    after = [_captured(d, 800, 4, 3) for d in (0.1, 0.2, 0.3)]
    # 800 characters in the main session and 400 in its agent: 300 tokens.
    assert impact._value(impact._CAPTURE, after) == (300.0, 3)
    assert impact._value(impact._TAGGED, after) == (75.0, 3)
    sessions = [_captured(-d, 0, 4, 0) for d in (1, 2, 3)] + after
    result = impact.compare(point, sessions, UNITS, now=CHANGE + timedelta(days=1))
    assert [m["label"] for m in result["measures"]][:2] == [
        "Metrics capture notes and tags per session",
        "Messages Claude tagged",
    ]
    # Each measure says its unit and which way is good, so the dashboard
    # can draw it and colour its reading: fewer tokens is better; a larger
    # tagged share is coverage, neither better nor worse.
    by_key = {m["key"]: m for m in result["measures"]}
    assert (by_key["capture_tokens"]["kind"], by_key["capture_tokens"]["better"]) == ("tokens", "lower")
    assert (by_key["tagged_share"]["kind"], by_key["tagged_share"]["better"]) == ("pct", None)
    assert (by_key["cost_per_session"]["kind"], by_key["cost_per_session"]["better"]) == ("money", "lower")


# -- EST-P3: task/purpose/mode, the ratio test, and stratification -------------


def test_session_facts_populates_session_id_purpose_and_mode(tmp_path):
    """session_facts() classifies each session standalone (SessionBundle
    has no pre-built classification) -- session_id from the transcript,
    purpose/mode from classify.classify_session, task left None without
    at least two agreeing capture tags."""
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    write_jsonl(
        project_dir / "session-abc.jsonl",
        [turn_line(timestamp=ts) for ts in ("2026-09-18T12:00:00.000Z", "2026-09-18T12:05:00.000Z")],
    )
    corpus = load_corpus([project_dir])
    facts = impact.session_facts(corpus, load_pricing())
    assert len(facts) == 1
    assert facts[0].session_id == "session-abc"
    assert facts[0].purpose and facts[0].mode
    assert facts[0].task is None


def test_stratum_prefers_task_then_purpose_then_a_catch_all():
    facts = _session(0, 1.0)
    assert impact.stratum(facts) == "(unspecified)"
    facts.purpose = "refactor"
    assert impact.stratum(facts) == "refactor"
    facts.task = "test"
    assert impact.stratum(facts) == "test"


def test_how_hard_and_how_big_split_the_stratum_once_half_the_sessions_carry_them():
    facts = _tasked(0, 1.0, "feature")
    facts.level = "hard"
    assert impact.stratum(facts, ("level", "size")) == "feature/hard/-"
    assert impact.stratum(facts) == "feature"
    others = [_tasked(0, 1.0, "feature") for _ in range(2)]
    assert impact.stratum_fields([facts] + others) == ()
    others[0].level, others[0].size = "easy", "s"
    assert impact.stratum_fields([facts] + others) == ("level",)


def test_harder_work_after_a_change_doesnt_read_as_the_change_costing_more(tmp_path):
    """Before: easy features at 2.00. After: easy features at 2.00 and
    hard ones at 20.00. Reweighted to before's all-easy mix, nothing
    changed; the same mix by task alone reads as a big rise."""
    def work(days, cost, level):
        facts = _tasked(days, cost, "feature")
        facts.level = level
        return facts

    before = [work(-d, 2.0, "easy") for d in (1, 2, 3)]
    after = [work(d, 2.0, "easy") for d in (0.1, 0.2)] + [work(d, 20.0, "hard") for d in (0.3, 0.4, 0.5)]
    assert impact._stratified_estimate(impact._COST, before, after).value == pytest.approx(2.0)
    for s in before + after:
        s.level = None
    assert impact._stratified_estimate(impact._COST, before, after).value > 10.0


def test_session_facts_reads_how_hard_and_how_big_from_capture_tags(tmp_path):
    from helpers import attachment_line, user_str_line

    note_text = "ClaudeGlass metrics capture (cg-cap v1 task,level,size): ..."
    note = attachment_line(
        "hook_additional_context",
        rendered=f"<system-reminder>\nSessionStart hook additional context: {note_text}\n</system-reminder>",
        content=[note_text], hookName="SessionStart", hookEvent="SessionStart", toolUseID="SessionStart",
    )
    note["timestamp"] = "2026-09-18T11:59:59.000Z"
    lines = [note]
    for n, tag in enumerate(("task=feature level=hard size=l", "task=feature level=hard size=m", "task=feature level=hard size=l")):
        lines.append(user_str_line("go on", origin={"kind": "human"}, timestamp=f"2026-09-18T12:00:{2 * n:02d}.000Z"))
        lines.append(turn_line(content=[{"type": "text", "text": f"Done.\n[cg: {tag}]"}], timestamp=f"2026-09-18T12:00:{2 * n + 1:02d}.000Z"))
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    write_jsonl(project_dir / "s.jsonl", lines)
    [facts] = impact.session_facts(load_corpus([project_dir]), load_pricing())
    assert (facts.task, facts.level, facts.size) == ("feature", "hard", "l")


def test_scheduled_sessions_are_their_own_stratum_so_their_count_does_not_move_cost():
    """Before: 3 real sessions at 10.00 and 3 scheduled checks at 0.10.
    After: the same real sessions and prices, but 30 checks ran. Pooled,
    cost per session falls about 85%; reweighted to before's mix it
    doesn't move."""
    def scheduled(days: float) -> SessionFacts:
        facts = _session(days, 0.10)
        facts.scheduled = True
        return facts

    assert impact.stratum(scheduled(0)) == "(scheduled)"
    before = [_session(-d, 10.0) for d in (1, 2, 3)] + [scheduled(-d - 0.5) for d in (1, 2, 3)]
    after = [_session(d, 10.0) for d in (0.1, 0.2, 0.3)] + [scheduled(0.4 + d / 100) for d in range(30)]
    cost = impact._COST
    old = impact._ratio_estimate(impact._pairs(cost, before)).value
    pooled = impact._ratio_estimate(impact._pairs(cost, after)).value
    assert pooled < 0.2 * old
    assert impact._stratified_estimate(cost, before, after).value == pytest.approx(old)


def test_stratified_after_estimate_matches_before_task_mix():
    """"Before" is all "code" work. "After" mixes a little more "code"
    work with several much pricier "review" sessions -- a shift in the
    kind of work, not a real cost change. The pooled after-average reads
    that mix shift as a huge rise; reweighted to before's all-"code" mix
    (EST-P3), the "review" sessions (0% of before) drop out and the
    estimate reflects "code" alone, unchanged."""
    before = [_tasked(-d, 2.0, "code") for d in (1, 2, 3)]
    after = [_tasked(d, 1.0, "code") for d in (0.1, 0.2)] + [
        _tasked(d, 100.0, "review") for d in (0.3, 0.4, 0.5, 0.6, 0.7)
    ]
    pooled = impact._ratio_estimate(impact._pairs(impact._COST, after))
    stratified = impact._stratified_estimate(impact._COST, before, after)
    assert pooled.value > 50.0
    assert stratified.value == 1.0


def test_ratio_test_flags_a_clear_drop_and_leaves_noise_unlabelled():
    before_clear = [_session(-d, 2.0) for d in (1, 2, 3)]
    after_clear = [_session(d, 1.0) for d in (0.1, 0.2, 0.3, 0.4)]
    clear_row = impact._measure_row(impact._COST, before_clear, after_clear, UNITS)
    assert clear_row["p"] == 0.0
    impact._label_rows([clear_row])
    assert clear_row["label_key"] == "lower"
    assert clear_row["label_text"] == "Lower"

    before_noisy = [_session(-1, 1.0), _session(-2, 5.0), _session(-3, 3.0)]
    after_noisy = [_session(1, 2.0), _session(2, 6.0), _session(3, 4.0)]
    noisy_row = impact._measure_row(impact._COST, before_noisy, after_noisy, UNITS)
    impact._label_rows([noisy_row])
    assert noisy_row["label_key"] == "no_clear_change"


def test_ratio_test_needs_enough_sessions_per_row_not_just_overall():
    """An agent-specific measure can have too few of its own data points
    to test even when the overall session counts clear MIN_SESSIONS."""
    before = [_session(-d, 2.0, agent_cost=1.0 if d == 1 else 0.0) for d in (1, 2, 3)]
    after = [_session(d, 1.0, agent_cost=1.0 if d == 0.1 else 0.0) for d in (0.1, 0.2, 0.3)]
    row = impact._measure_row(Measure("agent_cost", "Explore: cost per spawn", "money", "Explore"), before, after, UNITS)
    assert row["before_n"] == 1 and row["after_n"] == 1
    assert row["label_key"] == "too_little_data"
    assert row["p"] is None


# -- Phase 5: the lead measure, the mix of sessions and cost per request --------


def _row(
    key: str, label_key: str, *, p: float | None = 0.01, direction: str | None = "lower",
    change_pct: float | None = -30.0, demoted: bool = False, label: str | None = None,
) -> dict:
    """A comparison row with only what the lead and the verdict read."""
    return {
        "key": key, "label": label or key.replace("_", " ").capitalize(), "label_key": label_key,
        "direction": direction, "p": p, "change_pct": change_pct, "demoted": demoted,
        "before_value": 1.0, "after_value": 0.7, "before": "1.00 USD", "after": "0.70 USD",
    }


def test_the_lead_is_the_measure_the_test_is_surest_of_not_the_first_one_listed():
    rows = [
        _row("a", "no_clear_change", p=0.6, direction="higher"),
        _row("b", "possibly_lower", p=0.03),
        _row("c", "lower", p=0.04),
        _row("d", "too_little_data", p=None),
    ]
    # A clear difference before a possible one, whatever their p-values, before no clear change.
    assert impact.lead_row(rows)["key"] == "c"
    # Among clear ones the smaller p-value leads, and among ties the earlier row.
    rows.append(_row("e", "higher", p=0.002, direction="higher"))
    assert impact.lead_row(rows)["key"] == "e"
    rows.append(_row("f", "higher", p=0.002, direction="higher"))
    assert impact.lead_row(rows)["key"] == "e"
    # Nothing judged: the least doubtful of the rest, not the size of the move.
    quiet = [_row("a", "too_little_data", p=None), _row("b", "no_clear_change", p=0.9, direction="same")]
    assert impact.lead_row(quiet)["key"] == "b"
    assert impact.lead_row([_row("a", "too_little_data", p=None)])["key"] == "a"
    assert impact.lead_row([]) is None


def test_a_significant_move_under_the_noise_floor_does_not_lead():
    # With enough sessions a 0.6% wobble tests as significant; it is not what the card is about.
    wobble = _row("tokens_per_session", "lower", p=0.0, direction="same", change_pct=-0.6, label="Tokens per session")
    move = _row("output_per_turn", "possibly_lower", p=0.04, change_pct=-50.0)
    assert impact.lead_row([wobble, move])["key"] == "output_per_turn"
    # Left alone it reads as the same, never as a fall.
    assert impact._reading(wobble) == "no_clear_change"
    assert impact._verdict([wobble], 5, 5, True) == (
        "Tokens per session: about the same (1.00 USD before, 0.70 USD after)."
    )


def test_a_demoted_measure_leads_only_when_nothing_else_has_a_reading():
    cost = _row("cost_per_session", "lower", p=0.0, demoted=True)
    other = _row("capture_tokens", "no_clear_change", p=1.0, direction="same", change_pct=0.0)
    assert impact.lead_row([cost, other])["key"] == "capture_tokens"
    # Too little data on the others is not a reading: cost still reads before them.
    thin = _row("tagged_share", "too_little_data", p=None)
    assert impact.lead_row([cost, thin])["key"] == "cost_per_session"
    # Not demoted, the same row leads.
    assert impact.lead_row([dict(cost, demoted=False), other])["key"] == "cost_per_session"


@pytest.mark.parametrize(
    "reading, direction, change, expected",
    [
        ("lower", "lower", -40.2, "Cost per session fell 40%, from 1.00 USD to 0.70 USD (3 sessions before, 4 after)."),
        ("higher", "higher", 25.0, "Cost per session rose 25%, from 1.00 USD to 0.70 USD (3 sessions before, 4 after)."),
        ("possibly_lower", "lower", -12.0, "Cost per session may have fallen 12%, from 1.00 USD to 0.70 USD (3 sessions before, 4 after)."),
        ("possibly_higher", "higher", 12.0, "Cost per session may have risen 12%, from 1.00 USD to 0.70 USD (3 sessions before, 4 after)."),
        ("no_clear_change", "lower", -20.0, "Cost per session: no clear change (1.00 USD before, 0.70 USD after)."),
        ("no_clear_change", "same", 1.0, "Cost per session: about the same (1.00 USD before, 0.70 USD after)."),
        ("too_little_data", "lower", -20.0, "Cost per session: too little data to judge yet (1.00 USD before, 0.70 USD after)."),
    ],
)
def test_the_verdict_says_only_what_the_lead_rows_reading_allows(reading, direction, change, expected):
    row = _row("cost_per_session", reading, direction=direction, change_pct=change, label="Cost per session")
    assert impact._verdict([row], 3, 4, True) == expected


def test_the_verdict_follows_the_lead_not_the_first_row():
    rows = [
        _row("tokens_per_session", "no_clear_change", p=0.5, direction="same", change_pct=1.0, label="Tokens per session"),
        _row("cost_per_turn", "higher", p=0.001, direction="higher", change_pct=100.0, label="Cost per reply"),
    ]
    assert impact._verdict(rows, 3, 4, True).startswith("Cost per reply rose 100%")


def test_a_verdict_with_no_percentage_or_no_values_still_reads():
    unknown = _row("cost_per_session", "lower", change_pct=None, label="Cost per session")
    assert impact._verdict([unknown], 3, 4, True) == (
        "Cost per session fell, from 1.00 USD to 0.70 USD (3 sessions before, 4 after)."
    )
    empty = dict(_row("cost_per_session", "too_little_data", p=None), before_value=None, after_value=None)
    assert impact._verdict([empty], 3, 4, True) == "No data on the measures this change should move."
    assert impact._verdict([], 3, 4, True) == "No data on the measures this change should move."


def test_the_verdict_for_too_few_sessions_does_not_name_a_measure():
    assert "Too few sessions since the change" in impact._verdict([_row("a", "lower")], 5, 1, False)
    assert "Too few sessions before the change" in impact._verdict([_row("a", "lower")], 1, 5, False)


def _kinded(days: float, *, mode: str = "interactive", scheduled: bool = False) -> SessionFacts:
    facts = _session(days, 1.0)
    facts.mode, facts.scheduled = mode, scheduled
    return facts


def test_the_mix_is_flagged_when_a_kind_of_session_moves_by_25_points():
    before = [_kinded(-d) for d in (1, 2, 3, 4)]
    # One scheduled run in four after: 0% to 25%, exactly the bar.
    after = [_kinded(0.1, scheduled=True)] + [_kinded(d) for d in (0.2, 0.3, 0.4)]
    mix = impact.session_mix(before, after)
    assert mix["flagged"] is True
    assert (mix["kind"], mix["before_pct"], mix["after_pct"], mix["shift_pts"]) == ("scheduled", 0.0, 25.0, 25.0)
    assert mix["text"] == (
        "Scheduled runs were 0% of the sessions before this change and 25% after. "
        "Cost per session compares different kinds of work here, so read the other measures first."
    )


def test_the_mix_is_not_flagged_a_point_short_of_the_bar():
    before = [_kinded(-0.01 * n) for n in range(25)]
    after = [_kinded(0.01 * n, scheduled=n < 6) for n in range(25)]
    mix = impact.session_mix(before, after)
    assert mix["flagged"] is False and mix["shift_pts"] == 24.0 and mix["text"] == ""
    after = [_kinded(0.01 * n, scheduled=n < 7) for n in range(25)]
    assert impact.session_mix(before, after)["flagged"] is True


def test_the_mix_follows_the_session_mode_and_a_share_moving_down_counts_too():
    before = [_kinded(-d, mode="long-agentic") for d in (1, 2, 3, 4)]
    after = [_kinded(d, mode="interactive") for d in (0.1, 0.2, 0.3, 0.4)]
    mix = impact.session_mix(before, after)
    assert mix["flagged"] is True and mix["shift_pts"] == 100.0
    # Two kinds move the same 100 points: the scheduled one would win a tie, neither is scheduled.
    assert mix["kind"] in {"interactive", "long-agentic"}
    assert mix["text"].endswith("Cost per session compares different kinds of work here, so read the other measures first.")
    back = impact.session_mix(after, before)
    assert back["flagged"] is True and back["shift_pts"] == 100.0


def test_a_scheduled_run_counts_as_scheduled_whatever_its_mode():
    before = [_kinded(-d) for d in (1, 2, 3, 4)]
    after = [_kinded(d, mode="long-agentic", scheduled=True) for d in (0.1, 0.2, 0.3, 0.4)]
    mix = impact.session_mix(before, after)
    assert mix["kind"] == "scheduled" and mix["shift_pts"] == 100.0
    assert mix["text"].startswith("Scheduled runs were 0%")


def test_the_mix_is_not_judged_without_sessions_on_both_sides_or_without_modes():
    sessions = [_kinded(d) for d in (0.1, 0.2)]
    assert impact.session_mix([], sessions) is None and impact.session_mix(sessions, []) is None
    # Sessions built by hand carry no mode: nothing to compare.
    bare = [_session(d, 1.0) for d in (0.1, 0.2, 0.3)]
    mix = impact.session_mix(bare, bare)
    assert mix == {"flagged": False, "kind": "", "before_pct": 0.0, "after_pct": 0.0, "shift_pts": 0.0, "text": ""}


def test_compare_carries_the_mix_the_lead_and_each_rows_demotion():
    point = ChangePoint(CHANGE, "apply", "Changed a setting", keys=["effortLevel"])
    before = [_kinded(-d) for d in (1, 2, 3)]
    after = [_kinded(d, scheduled=True) for d in (0.1, 0.2, 0.3)]
    result = impact.compare(point, before + after, UNITS, now=CHANGE + timedelta(days=1))
    assert result["mix"]["flagged"] is True and result["mix"]["kind"] == "scheduled"
    assert result["lead"] == result["measures"][0]["key"]
    assert not any(row["demoted"] for row in result["measures"])
    # Too few sessions after: no lead and no mix to speak of.
    thin = impact.compare(point, before + after[:1], UNITS, now=CHANGE + timedelta(days=1))
    assert thin["enough"] is False and thin["lead"] is None and thin["mix"] is None


@pytest.mark.parametrize("keys", [["capture.level"], ["capture.coaching"], ["capture.feedback"]])
def test_a_capture_change_reads_cost_per_session_last_however_far_it_moved(keys):
    source = "capture" if keys != ["capture.feedback"] else "config"
    point = ChangePoint(CHANGE, source, "Turned something on", keys=keys)
    before = [_captured(-d, 0, 4, 0) for d in (1, 2, 3)]
    after = [_captured(d, 0, 4, 0) for d in (0.1, 0.2, 0.3)]
    for facts in before:
        facts.main.cost = 4.0
    for facts in after:
        facts.main.cost = 2.0
    result = impact.compare(point, before + after, UNITS, now=CHANGE + timedelta(days=1))
    by_key = {m["key"]: m for m in result["measures"]}
    cost = by_key["cost_per_session"]
    # Cost fell by half with no spread: the test is sure, and still it is not the headline.
    assert cost["label_key"] == "lower" and cost["demoted"] is True
    assert result["lead"] != "cost_per_session" and result["measures"][0]["key"] == result["lead"]
    assert not result["verdict"].startswith("Cost per session")
    assert [row["key"] for row in result["measures"] if row["demoted"]] == ["cost_per_session"]


def test_a_cost_change_is_not_demoted_when_cost_is_what_it_is_about():
    point = ChangePoint(CHANGE, "transcript", "Model changed", keys=["model"])
    before = [_session(-d, 4.0) for d in (1, 2, 3)]
    after = [_session(d, 2.0) for d in (0.1, 0.2, 0.3)]
    result = impact.compare(point, before + after, UNITS, now=CHANGE + timedelta(days=1))
    assert not any(row["demoted"] for row in result["measures"])
    # Cost is the headline here: cost per reply, per request and per session all halved.
    assert result["lead"] in {"cost_per_turn", "cost_per_session", "cost_per_substantive_cycle"}
    assert result["lead"] == result["measures"][0]["key"]


def _asked(days: float, cost: float, *, asks: int, cycles: int, messages: int) -> SessionFacts:
    facts = _session(days, cost)
    facts.messages, facts.asks, facts.substantive = messages, asks, cycles
    return facts


def test_cost_per_request_divides_by_the_cycles_that_asked_for_something():
    # Two of ten prompt cycles asked for anything before, five after, for the same cost.
    before = [_asked(-d, 10.0, asks=2, cycles=2, messages=10) for d in (1, 2, 3)]
    after = [_asked(d, 10.0, asks=5, cycles=5, messages=10) for d in (0.1, 0.2, 0.3)]
    assert impact._value(impact._CYCLE, before) == (5.0, 3)
    assert impact._value(impact._CYCLE, after) == (2.0, 3)
    # Cost per session can't tell them apart; cost per request can.
    assert impact._value(impact._COST, before)[0] == impact._value(impact._COST, after)[0]
    point = ChangePoint(CHANGE, "transcript", "Model changed", keys=["model"])
    result = impact.compare(point, before + after, UNITS, now=CHANGE + timedelta(days=1))
    row = next(m for m in result["measures"] if m["key"] == "cost_per_substantive_cycle")
    assert row["label"] == "Cost per request" and row["change_pct"] == -60.0 and row["label_key"] == "lower"
    assert (row["kind"], row["better"]) == ("money", "lower")
    # It is the only measure that moved, so it leads and the verdict is about it.
    assert result["lead"] == "cost_per_substantive_cycle"
    assert result["verdict"].startswith("Cost per request fell 60%, from ")
    # And it sits just before cost per session in the order measures_for gives.
    keys = [m.key for m in impact.measures_for(point)]
    assert keys.index("cost_per_substantive_cycle") == keys.index("cost_per_session") - 1


def test_a_scheduled_run_has_no_cost_per_request_of_its_own():
    # No message of yours, so no prompt cycle that asked for anything: it leaves the figure alone.
    scheduled = _asked(0, 50.0, asks=0, cycles=0, messages=1)
    scheduled.scheduled = True
    real = [_asked(-d, 10.0, asks=2, cycles=2, messages=4) for d in (1, 2, 3)]
    assert impact._value(impact._CYCLE, real + [scheduled]) == impact._value(impact._CYCLE, real) == (5.0, 3)


def test_cost_per_request_is_only_for_a_change_of_model_or_a_habit_you_started():
    def keys(*changed: str) -> list[str]:
        return [m.key for m in impact.measures_for(ChangePoint(CHANGE, "transcript", "x", keys=list(changed)))]

    assert "cost_per_substantive_cycle" in keys("model")
    assert "cost_per_substantive_cycle" in keys("habit.drip_feed")
    assert "cost_per_substantive_cycle" in keys("habit.split_large")
    for other in (["effortLevel"], ["fastMode"], ["capture.level"], ["capture.coaching"], ["autoCompactWindow"]):
        assert "cost_per_substantive_cycle" not in keys(*other), other
    assert keys("model")[-2:] == ["cost_per_substantive_cycle", "cost_per_session"]


def test_the_habit_rates_divide_by_the_messages_that_asked_for_something():
    def session(days, *, messages, asks):
        facts = _asked(days, 1.0, asks=asks, cycles=asks, messages=messages)
        facts.habits, facts.drip_messages = 2, 3
        return facts

    # Ten messages, five of them asks: 2 habits and 3 drip messages in 5 asks.
    five = [session(d, messages=10, asks=5) for d in (0.1, 0.2, 0.3)]
    assert impact._value(impact._HABITS, five) == (40.0, 3)
    assert impact._value(impact._DRIP, five) == (60.0, 3)
    # Ten more go-ahead messages change neither rate: they asked for nothing.
    padded = [session(d, messages=20, asks=5) for d in (0.1, 0.2, 0.3)]
    assert impact._value(impact._HABITS, padded) == impact._value(impact._HABITS, five)
    assert impact._value(impact._DRIP, padded) == impact._value(impact._DRIP, five)
    # A session counted by hand (no asks) falls back to its messages.
    by_hand = _captured(0.1, 0, 10, 0)
    by_hand.habits, by_hand.drip_messages = 3, 4
    assert (by_hand.requests, by_hand.substantive_cycles) == (10, 10)
    assert impact._value(impact._HABITS, [by_hand]) == (30.0, 1)


def test_both_sides_of_a_habit_comparison_use_the_same_count_of_asks():
    point = ChangePoint(CHANGE, "habit", "Started a habit", keys=["habit.drip_feed"])
    before = [_asked(-d, 1.0, asks=5, cycles=5, messages=20) for d in (1, 2, 3)]
    after = [_asked(d, 1.0, asks=5, cycles=5, messages=10) for d in (0.1, 0.2, 0.3)]
    for facts in before + after:
        facts.habits, facts.drip_messages = 2, 3
    result = impact.compare(point, before + after, UNITS, now=CHANGE + timedelta(days=1))
    for key in ("prompting_habits", "drip_share"):
        row = next(m for m in result["measures"] if m["key"] == key)
        # Half the messages after, the same asks and the same habits: no change, not a doubling.
        assert row["before_value"] == row["after_value"], key
        assert row["direction"] == "same" and row["label_key"] == "no_clear_change", key


def test_session_facts_count_what_asked_for_something_the_way_the_habit_rates_do(tmp_path):
    from claudeglass import prompting
    from helpers import user_str_line

    lines = []
    for n, text in enumerate(("fix the parser and add a test for it", "continue", "how is it going?", "also rename the helper")):
        lines.append(user_str_line(text, origin={"kind": "human"}, timestamp=f"2026-09-18T12:00:{2 * n:02d}.000Z"))
        lines.append(turn_line(content=[{"type": "text", "text": "Done."}], timestamp=f"2026-09-18T12:00:{2 * n + 1:02d}.000Z"))
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    write_jsonl(project_dir / "s.jsonl", lines)
    corpus = load_corpus([project_dir])
    [facts] = impact.session_facts(corpus, load_pricing())
    # Four prompt cycles; the go-ahead and the status check asked for nothing.
    assert facts.messages == 4
    assert (facts.asks, facts.substantive) == (2, 2)
    assert (facts.requests, facts.substantive_cycles) == (2, 2)
    # The same count the Work habits rates divide by.
    habit_facts = prompting.session_prompting(corpus.sessions[0], prompting._Prices(load_pricing()))
    assert prompting.habit_rates(habit_facts)[2] == facts.asks


def test_a_card_reads_a_sure_move_under_the_noise_floor_as_no_clear_change():
    def facts(days, cost, tokens):
        return SessionFacts(
            start=CHANGE + timedelta(days=days),
            main=_Transcript(cost=cost, turns=10, output_tokens=4000, total_tokens=tokens),
        )

    before = [facts(-d, c, 1_000_000) for d, c in zip((1, 2, 3, 4, 5), (10, 30, 50, 20, 40))]
    after = [facts(d / 10, c, 980_000) for d, c in zip((1, 2, 3, 4, 5), (12, 28, 47, 25, 38))]
    point = ChangePoint(CHANGE, "transcript", "Model changed", keys=["model"])
    result = impact.compare(point, before + after, UNITS, now=CHANGE + timedelta(days=1))
    tokens = next(m for m in result["measures"] if m["key"] == "tokens_per_session")
    # A 2% fall with no spread: the test is sure of it, the card still says no clear change.
    assert tokens["direction"] == "same" and tokens["p"] == 0.0
    assert tokens["label_key"] == "no_clear_change"
    assert tokens["label_text"] == impact.quality.LABELS["no_clear_change"]
    assert result["verdict"].startswith("Tokens per session: about the same")

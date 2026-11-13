"""Did a fix that landed do what its card or profile said it would
(``outcomes``)? The reading of one fix: the lever, the method, the sessions
each side of its change point, what was measured and what was predicted,
and the guardrail."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from claudeglass import counterfactual, impact, model_swap, outcomes, quality
from claudeglass.change_points import ChangePoint
from claudeglass.pricing import load_pricing

from test_counterfactual import (
    _agent_model_point,
    _agent_model_world,
    _agent_run,
    _main_model_point,
    _main_model_world,
    _read_main_model,
)

PRICING = load_pricing()
OPUS, SONNET, HAIKU = "claude-opus-5", "claude-sonnet-5", "claude-haiku-5-5"

T = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)


def _point(*labels, ts=T, project="", source="config"):
    return ChangePoint(ts, source, "x", keys=list(labels), project=project)


# -- the lever a change names, and how it can be measured -------------------


def test_a_changes_lever_is_its_last_key_and_an_agent_only_for_an_agents_field():
    assert outcomes.lever_of({"target": "agent", "agent": "gp", "key": "tools", "value": ["Read"]}) == ("gp", "tools")
    assert outcomes.lever_of({"target": "settings", "agent": None, "key": "model", "value": "sonnet"}) == (None, "model")
    assert outcomes.lever_of({"target": "settings", "key": "env.MAX_THINKING_TOKENS", "value": "1"}) == (
        None, "MAX_THINKING_TOKENS",
    )
    # A settings change never names an agent, whatever the stored row holds.
    assert outcomes.lever_of({"target": "settings", "agent": "gp", "key": "model"}) == (None, "model")


@pytest.mark.parametrize(
    "agent, key, method",
    [
        (None, "model", "exact"),
        ("gp", "model", "exact"),
        (None, "fastMode", "exact"),
        (None, "promptCacheTtl", "exact"),
        (None, "subagentPromptCacheTtl", "exact"),
        (None, "autoCompactWindow", "exact"),
        ("gp", "promptCacheTtl", "none"),
        ("gp", "autoCompactWindow", "none"),
        ("gp", "tools", "start_context"),
        ("gp", "disallowedTools", "start_context"),
        ("gp", "omitClaudeMd", "start_context"),
        ("gp", "skills", "start_context"),
        ("gp", "mcpServers", "start_context"),
        (None, "enabledPlugins", "start_context"),
        (None, "claude_md_chars", "start_context"),
        (None, "tools", "none"),
        (None, "effortLevel", "none"),
        ("gp", "effort", "none"),
        (None, "MAX_THINKING_TOKENS", "none"),
        (None, "", "none"),
    ],
)
def test_the_method_a_lever_can_be_measured_by(agent, key, method):
    assert outcomes.method_for(agent, key) == method


def test_a_card_reads_as_one_lever_only_when_all_its_changes_agree():
    pick = outcomes.lever_for
    assert pick([("gp", "tools")]) == ("gp", "tools")
    # Two start-context fields of one agent: one lever, the first named.
    assert pick([("gp", "tools"), ("gp", "disallowedTools")]) == ("gp", "tools")
    # Different methods, or an agent's field with a setting, are not one lever.
    assert outcomes.method_for(*pick([("gp", "tools"), ("gp", "model")])) == "none"
    assert outcomes.method_for(*pick([("gp", "tools"), (None, "model")])) == "none"
    # A card with no changes names none.
    assert outcomes.method_for(*pick([])) == "none"


# -- the change point a landing matches -------------------------------------


def test_a_landing_matches_the_latest_point_at_or_before_it_that_names_its_lever():
    landed = T + timedelta(days=2)
    older = _point("agents.gp.tools", ts=T - timedelta(days=5))
    wanted = _point("agents.gp.tools", ts=T)
    other = _point("effective.effortLevel", ts=T + timedelta(days=1))
    after = _point("agents.gp.tools", ts=T + timedelta(days=3))
    undone = _point("agents.gp.tools", ts=T + timedelta(days=1), source="revert")
    found = outcomes.match_point([("gp", "tools")], set(), landed, [older, wanted, other, after, undone])
    assert found is wanted
    assert outcomes.match_point([("gp", "model")], set(), landed, [older, wanted]) is None
    # A point a minute after the landing is the same edit.
    assert outcomes.match_point([("gp", "tools")], set(), T - timedelta(seconds=30), [wanted]) is wanted


def test_a_landing_matches_only_a_point_that_applies_in_its_view():
    mine = _point("agents.gp.tools", ts=T - timedelta(days=1), project="proj-a")
    theirs = _point("agents.gp.tools", ts=T, project="proj-b")
    everywhere = _point("agents.gp.tools", ts=T - timedelta(days=3))
    landed = T + timedelta(hours=1)
    assert outcomes.match_point([("gp", "tools")], {"proj-a"}, landed, [mine, theirs, everywhere]) is mine
    assert outcomes.match_point([("gp", "tools")], {"proj-c"}, landed, [mine, theirs, everywhere]) is everywhere
    # The every-project view takes a point of any one project.
    assert outcomes.match_point([("gp", "tools")], set(), landed, [mine, theirs, everywhere]) is theirs


def test_rows_of_two_views_for_one_change_point_are_one_fix():
    everywhere = _point("agents.gp.tools")
    in_a = _point("agents.gp.tools", project="proj-a")
    rows = {"*": set(), "proj-a": {"proj-a"}, "proj-b": {"proj-b"}}
    # A point for every project: the two views' rows share a key, and the "*" row ranks first.
    assert outcomes.fix_key("spawn-tools-list", "gp", everywhere) == outcomes.fix_key("spawn-tools-list", "gp", everywhere)
    assert outcomes.view_rank(everywhere, rows["*"]) > outcomes.view_rank(everywhere, rows["proj-a"])
    # A point of one project: that project's row ranks first, the "*" row next, another project's last.
    assert outcomes.view_rank(in_a, rows["proj-a"]) > outcomes.view_rank(in_a, rows["*"]) > outcomes.view_rank(
        in_a, rows["proj-b"]
    )
    # The key tells rule, agent, point and project apart.
    keys = {
        outcomes.fix_key("spawn-tools-list", "gp", everywhere),
        outcomes.fix_key("spawn-tools-list", "gp", in_a),
        outcomes.fix_key("spawn-tools-list", "other", everywhere),
        outcomes.fix_key("spawn-claude-md", "gp", everywhere),
    }
    assert len(keys) == 4
    assert outcomes.fix_key("model", None, everywhere) == ("model", None, everywhere.iso(), "")


# -- reading one fix ----------------------------------------------------------

NOW = T + timedelta(days=12)
START_CORPUS = T - timedelta(days=40)


class _StubCarry:
    """Stands in for ``context_files._Carry``: ``cost(chars, start, end)`` is
    ``chars / 1000``, so 40,000 tokens at 4 characters each cost 160 a spawn."""

    def __init__(self, sub, pricing):
        self.sub = sub

    def cost(self, chars, start, end):
        return chars / 1000


@pytest.fixture
def stub_carry(monkeypatch):
    monkeypatch.setattr(counterfactual, "_Carry", _StubCarry)


def _spawn(tokens, family="sonnet-5", cost=1.0):
    return impact._Transcript(cost=cost, turns=4, startup_tokens=tokens, model=family)


def _session(sid, days, spawns=(), *, project="proj", scheduled=False, main=None, runs=()):
    return impact.SessionFacts(
        start=T + timedelta(days=days),
        main=main or impact._Transcript(cost=2.0, turns=5),
        session_id=sid,
        spawns=[("gp", spawn) for spawn in spawns],
        project=project,
        scheduled=scheduled,
        runs=list(runs),
    )


def _bundles(sessions):
    """Stand-ins for the transcripts the carry prices: four priced replies each."""
    turns = [SimpleNamespace(turn_index=i) for i in range(5)]
    return {
        s.session_id: SimpleNamespace(
            top=SimpleNamespace(turns=turns), subs=[SimpleNamespace(turns=turns) for _ in s.spawns]
        )
        for s in sessions
    }


def _spawn_world(before, after, *, project="proj", gap=0.5):
    """One session per spawn: ``before`` and ``after`` are lists of (family,
    start tokens), the first side from nine days before ``T``, the second
    from just after it."""
    sessions = [
        _session(f"b{n}", -9 + n * gap, [_spawn(tokens, family)], project=project) for n, (family, tokens) in enumerate(before)
    ] + [
        _session(f"a{n}", 0.5 + n * gap, [_spawn(tokens, family)], project=project) for n, (family, tokens) in enumerate(after)
    ]
    return sessions, _bundles(sessions)


def _same(tokens, family="sonnet-5"):
    return [(family, t) for t in tokens]


def _merged(*worlds):
    """Several worlds as one, each session id given its world's number."""
    sessions, bundles = [], {}
    for n, (many, some) in enumerate(worlds):
        sessions += [replace(s, session_id=f"{n}{s.session_id}") for s in many]
        bundles.update({f"{n}{key}": value for key, value in some.items()})
    return sessions, bundles


def _read(point, agent, key, prediction, world, *, points=None, now=NOW, corpus_start=START_CORPUS):
    sessions, bundles = world
    return outcomes.read_fix(
        point, agent, key, prediction, sessions, [point] if points is None else points, bundles, None, now, corpus_start
    )


def test_a_tools_line_reads_the_drop_in_start_context_on_one_family(stub_carry):
    world = _spawn_world(_same([60_000] * 6), _same([20_000] * 6))
    reading = _read(_point("agents.gp.tools"), "gp", "tools", outcomes.Prediction(30.0, 30_000), world)
    assert reading.method == "start_context"
    assert (reading.before_units, reading.after_units, reading.projects) == (6, 6, 1)
    # 40,000 tokens at 4 characters each on each of 6 spawns, at the stub's 1/1000.
    assert reading.measured_usd == pytest.approx(6 * 160.0)
    # The card's 30,000 tokens, carried the same way on the same spawns.
    assert reading.predicted_usd == pytest.approx(6 * 120.0)
    assert reading.verdict == "half_or_more"
    assert reading.short_export is False


def test_a_fix_that_took_out_less_than_half_of_what_its_card_said_is_under_half(stub_carry):
    world = _spawn_world(_same([60_000] * 6), _same([20_000] * 6))
    reading = _read(_point("agents.gp.tools"), "gp", "tools", outcomes.Prediction(None, 100_000), world)
    # 40,000 of the 100,000 tokens the card named: 0.4.
    assert (reading.measured_usd, reading.predicted_usd) == (pytest.approx(960.0), pytest.approx(2400.0))
    assert reading.verdict == "under_half"
    # Exactly half counts.
    half = _read(_point("agents.gp.tools"), "gp", "tools", outcomes.Prediction(None, 80_000), world)
    assert half.verdict == "half_or_more"


def test_a_fix_that_added_context_is_a_loss_under_half(stub_carry):
    world = _spawn_world(_same([20_000] * 6), _same([60_000] * 6))
    reading = _read(_point("agents.gp.tools"), "gp", "tools", outcomes.Prediction(30.0, 30_000), world)
    assert reading.measured_usd == pytest.approx(-960.0)
    assert reading.verdict == "under_half"


def test_fewer_than_five_spawns_a_side_is_too_little_data_never_a_number(stub_carry):
    world = _spawn_world(_same([60_000] * 6), _same([20_000] * 4))
    reading = _read(_point("agents.gp.tools"), "gp", "tools", outcomes.Prediction(30.0, 30_000), world)
    assert reading.verdict == "too_little_data"
    assert reading.measured_usd is None and reading.predicted_usd is None
    assert reading.short_export is False
    world = _spawn_world(_same([60_000] * 4), _same([20_000] * 6))
    assert _read(_point("agents.gp.tools"), "gp", "tools", outcomes.Prediction(30.0, 30_000), world).verdict == (
        "too_little_data"
    )


def test_a_start_context_basis_under_the_floor_is_not_measured(stub_carry):
    world = _spawn_world(_same([60_000] * 6), _same([58_000] * 6))
    reading = _read(_point("agents.gp.tools"), "gp", "tools", outcomes.Prediction(None, 2_000), world)
    # A 2,000-token drop matches a 2,000-token basis, but 2,000 of a 60,000-token start is lost in the noise.
    assert outcomes.START_FLOOR == 0.45
    assert reading.verdict == "not_measured"
    assert reading.note == "the basis is too small a share of the start to tell from noise"
    assert reading.measured_usd is None and reading.predicted_usd is None


def test_a_basis_at_the_floor_is_read_and_one_token_under_is_not(stub_carry):
    world = _spawn_world(_same([60_000] * 6), _same([33_000] * 6))
    at = _read(_point("agents.gp.tools"), "gp", "tools", outcomes.Prediction(None, 27_000), world)
    assert at.verdict == "half_or_more"
    under = _read(_point("agents.gp.tools"), "gp", "tools", outcomes.Prediction(None, 26_999), world)
    assert under.verdict == "not_measured"


def test_the_floor_is_the_before_median_on_the_held_family(stub_carry):
    before = _same([60_000] * 6) + _same([200_000] * 7, "haiku-4-5")
    world = _spawn_world(before, _same([30_000] * 6))
    reading = _read(_point("agents.gp.tools"), "gp", "tools", outcomes.Prediction(None, 30_000), world)
    # Half of the held family's 60,000 start; against every spawn's median it would be 15% and floored.
    assert reading.verdict == "half_or_more" and reading.before_units == 6


def test_an_every_project_fix_leaves_out_only_the_projects_under_the_floor(stub_carry):
    world = _merged(
        _spawn_world(_same([60_000] * 6), _same([20_000] * 6), project="proj-a"),
        _spawn_world(_same([200_000] * 6), _same([190_000] * 6), project="proj-b"),
    )
    reading = _read(_point("agents.gp.tools"), "gp", "tools", outcomes.Prediction(None, 30_000), world)
    assert reading.projects == 1
    assert (reading.before_units, reading.after_units) == (6, 6)
    assert reading.measured_usd == pytest.approx(6 * 160.0)
    assert reading.predicted_usd == pytest.approx(6 * 120.0)
    assert reading.verdict == "half_or_more"


def test_a_side_too_thin_to_read_is_too_little_data_not_under_the_floor(stub_carry):
    world = _spawn_world(_same([60_000] * 6), _same([58_000] * 4))
    reading = _read(_point("agents.gp.tools"), "gp", "tools", outcomes.Prediction(None, 2_000), world)
    assert reading.verdict == "too_little_data"


def test_a_model_change_at_the_same_point_leaves_no_family_to_compare(stub_carry):
    world = _spawn_world(_same([60_000] * 6, "haiku-4-5"), _same([20_000] * 6, "sonnet-5"))
    reading = _read(
        _point("agents.gp.tools", "agents.gp.model"), "gp", "tools", outcomes.Prediction(30.0, 30_000), world
    )
    assert reading.verdict == "too_little_data"
    assert reading.measured_usd is None
    assert (reading.before_units, reading.after_units) == (0, 0)


def test_another_change_to_what_every_start_carries_cuts_the_before_side(stub_carry):
    # Twelve spawns before: six started more than six days before the change, six within six days.
    world = _spawn_world(_same([90_000] * 6 + [60_000] * 6), _same([20_000] * 8), gap=0.5)
    plugins = _point("enabled_plugins", ts=T - timedelta(days=6.25))
    tools = _point("agents.gp.tools")
    uncut = _read(tools, "gp", "tools", outcomes.Prediction(None, 40_000), world, points=[tools])
    assert uncut.before_units == 12
    cut = _read(tools, "gp", "tools", outcomes.Prediction(None, 40_000), world, points=[tools, plugins])
    # Only the spawns after the plugins change are read, so only the 60,000 starts.
    assert cut.before_units == 6
    assert cut.measured_usd == pytest.approx(8 * 160.0)
    # A change to another of this agent's settings cuts it the same way.
    model = _point("agents.gp.model", ts=T - timedelta(days=6.25))
    assert _read(tools, "gp", "tools", outcomes.Prediction(None, 40_000), world, points=[tools, model]).before_units == 6
    # A change after this one cuts the after side.
    later = _point("agents.gp.skills", ts=T + timedelta(days=2.2))
    after = _read(tools, "gp", "tools", outcomes.Prediction(None, 40_000), world, points=[tools, later])
    assert after.verdict == "too_little_data" and after.measured_usd is None


def test_an_unrelated_change_does_not_cut_the_sides(stub_carry):
    world = _spawn_world(_same([60_000] * 12), _same([20_000] * 12))
    tools = _point("agents.gp.tools")
    effort = _point("effective.effortLevel", ts=T - timedelta(days=6.25))
    other_agent = _point("agents.other.tools", ts=T + timedelta(days=2))
    reading = _read(tools, "gp", "tools", outcomes.Prediction(None, 30_000), world, points=[tools, effort, other_agent])
    assert (reading.before_units, reading.after_units) == (12, 12)


def test_the_sides_are_a_fortnight_before_and_thirty_days_after(stub_carry):
    sessions = [_session(f"b{n}", -20 + n, [_spawn(60_000)]) for n in range(6)]  # 20 to 15 days before: out
    sessions += [_session(f"c{n}", -13 + n * 0.5, [_spawn(60_000)]) for n in range(6)]  # within 14 days: in
    sessions += [_session(f"a{n}", 1 + n, [_spawn(20_000)]) for n in range(6)]  # in
    sessions += [_session(f"z{n}", 31 + n, [_spawn(20_000)]) for n in range(6)]  # past 30 days: out
    world = (sessions, _bundles(sessions))
    reading = _read(
        _point("agents.gp.tools"), "gp", "tools", outcomes.Prediction(None, 30_000), world, now=T + timedelta(days=60)
    )
    assert (reading.before_units, reading.after_units) == (6, 6)
    # Until now when that is sooner.
    soon = _read(
        _point("agents.gp.tools"), "gp", "tools", outcomes.Prediction(None, 30_000), world, now=T + timedelta(days=3.5)
    )
    assert soon.verdict == "too_little_data" and soon.measured_usd is None


def test_an_every_project_point_reads_each_project_alone_and_adds_them_up(stub_carry):
    world = _merged(
        _spawn_world(_same([60_000] * 5), _same([20_000] * 5), project="proj-a"),
        _spawn_world(_same([50_000] * 6), _same([20_000] * 6), project="proj-b"),
        _spawn_world(_same([50_000] * 6), _same([20_000] * 4), project="proj-c"),
    )
    reading = _read(_point("agents.gp.tools"), "gp", "tools", outcomes.Prediction(None, 30_000), world)
    # proj-a: 40,000 on 5 spawns, proj-b: 30,000 on 6; proj-c has 4 after and is left out.
    assert reading.projects == 2
    assert (reading.before_units, reading.after_units) == (11, 11)
    assert reading.measured_usd == pytest.approx(5 * 160.0 + 6 * 120.0)
    assert reading.predicted_usd == pytest.approx(11 * 120.0)


def test_a_change_in_one_project_reads_only_that_projects_sessions(stub_carry):
    world = _merged(
        _spawn_world(_same([60_000] * 5), _same([20_000] * 5), project="proj-a"),
        _spawn_world(_same([50_000] * 6), _same([20_000] * 6), project="proj-b"),
    )
    reading = _read(
        _point("agents.gp.tools", project="proj-b"), "gp", "tools", outcomes.Prediction(None, 30_000), world
    )
    assert (reading.projects, reading.before_units, reading.after_units) == (1, 6, 6)
    assert reading.measured_usd == pytest.approx(6 * 120.0)


def test_a_change_made_before_the_exports_first_session_is_marked_short_export(stub_carry):
    world = _spawn_world(_same([60_000] * 3), _same([20_000] * 6))
    start = T - timedelta(days=5)
    prediction = outcomes.Prediction(None, 30_000)
    reading = _read(_point("agents.gp.tools"), "gp", "tools", prediction, world, corpus_start=start)
    assert reading.verdict == "too_little_data" and reading.short_export is True
    # Not so when the corpus reaches back a lookback.
    full = _read(_point("agents.gp.tools"), "gp", "tools", prediction, world)
    assert full.verdict == "too_little_data" and full.short_export is False
    # Nor when the fix reads.
    enough = _spawn_world(_same([60_000] * 6), _same([20_000] * 6))
    assert _read(_point("agents.gp.tools"), "gp", "tools", prediction, enough, corpus_start=start).short_export is False
    # Only a start-context fix has a before side to be short.
    few = _exact_world(after=2)
    exact = _read(_point("effective.model"), None, "model", outcomes.Prediction(5.0, 10.0), few, corpus_start=start)
    assert exact.verdict == "too_little_data" and exact.short_export is False


def _main_world(before, after):
    """Main sessions, one per start size; ``before`` and ``after`` are lists of tokens."""
    sessions = [
        _session(f"b{n}", -9 + n * 0.5, main=impact._Transcript(cost=2.0, turns=5, startup_tokens=t, model="sonnet-5"))
        for n, t in enumerate(before)
    ] + [
        _session(f"a{n}", 0.5 + n * 0.5, main=impact._Transcript(cost=2.0, turns=5, startup_tokens=t, model="sonnet-5"))
        for n, t in enumerate(after)
    ]
    return sessions, _bundles(sessions)


def _approximate(paid, carried, key="enabledPlugins"):
    row = counterfactual.KeyResult(key, None, "approximate", paid, paid + carried, "")
    return counterfactual.Counterfactual(paid, paid + carried, "approximate", "", 6, [row])


def test_a_main_session_start_context_fix_is_predicted_by_the_same_carry_on_its_basis(stub_carry, monkeypatch):
    world = _main_world([60_000] * 6, [20_000] * 6)
    seen = {}

    def fake(point, before, after, bundles, pricing):
        seen["sides"] = (len(before), len(after))
        return _approximate(10.0, 6 * 160.0)

    monkeypatch.setattr(counterfactual, "without_change", fake)
    reading = _read(_point("effective.enabledPlugins"), None, "enabledPlugins", outcomes.Prediction(None, 30_000), world)
    # The measured carry is for a 40,000-token drop on 6 sessions; the card named 30,000 tokens.
    assert seen["sides"] == (6, 6)
    assert reading.method == "start_context"
    assert reading.measured_usd == pytest.approx(6 * 160.0)
    assert reading.predicted_usd == pytest.approx(6 * 120.0)
    assert reading.verdict == "half_or_more"
    assert (reading.before_units, reading.after_units) == (6, 6)


def test_a_main_session_start_context_basis_under_the_floor_is_not_measured(stub_carry, monkeypatch):
    monkeypatch.setattr(counterfactual, "without_change", lambda *a: pytest.fail("under the floor"))
    world = _main_world([60_000] * 6, [58_000] * 6)
    reading = _read(_point("effective.enabledPlugins"), None, "enabledPlugins", outcomes.Prediction(None, 2_000), world)
    assert reading.verdict == "not_measured"
    assert reading.note == "the basis is too small a share of the start to tell from noise"


def test_a_main_session_start_context_fix_without_enough_sessions_on_a_family_is_too_little_data(monkeypatch):
    monkeypatch.setattr(counterfactual, "without_change", lambda *a: pytest.fail("no sessions to read"))
    world = _main_world([60_000] * 6, [20_000] * 4)
    reading = _read(_point("effective.enabledPlugins"), None, "enabledPlugins", outcomes.Prediction(None, 20_000), world)
    assert reading.verdict == "too_little_data" and reading.measured_usd is None


def test_a_scheduled_main_session_is_not_a_unit(monkeypatch):
    monkeypatch.setattr(counterfactual, "without_change", lambda *a: pytest.fail("no sessions to read"))
    sessions, bundles = _main_world([60_000] * 6, [20_000] * 6)
    sessions = [replace(s, scheduled=s.session_id.startswith("a")) for s in sessions]
    reading = _read(
        _point("effective.enabledPlugins"), None, "enabledPlugins", outcomes.Prediction(None, 20_000), (sessions, bundles)
    )
    assert reading.verdict == "too_little_data"


def _exact_world(after=6, before=3, project="proj"):
    sessions = [_session(f"b{n}", -5 + n, project=project) for n in range(before)] + [
        _session(f"a{n}", 0.5 + n * 0.5, project=project) for n in range(after)
    ]
    return sessions, _bundles(sessions)


def _repriced(paid=10.0, without=25.0):
    row = counterfactual.KeyResult("model", None, "repriced", paid, without, "")
    return counterfactual.Counterfactual(paid + 20.0, without + 20.0, "repriced", "", 6, [row])


def test_a_model_change_is_read_exactly_on_the_same_replies(monkeypatch):
    seen = {}

    def fake(point, agent, key, before, after, bundles, pricing):
        seen["sides"] = (len(before), len(after))
        return _repriced().per_key[0]

    monkeypatch.setattr(counterfactual, "key_row", fake)
    reading = _read(_point("effective.model"), None, "model", outcomes.Prediction(6.0, 12.0), _exact_world())
    assert reading.method == "exact"
    assert seen["sides"] == (3, 6)
    # Without the change the replies would have cost 25 rather than 10.
    assert reading.measured_usd == pytest.approx(15.0)
    # The card saved 6 of the 12 it repriced: half of what the units would have cost without it.
    assert reading.predicted_usd == pytest.approx(6.0 / 12.0 * 25.0)
    assert reading.verdict == "half_or_more"
    assert (reading.before_units, reading.after_units, reading.projects) == (3, 6, 1)


def test_an_exact_prediction_is_the_cards_share_of_its_cost_applied_to_the_units_without_the_change(monkeypatch):
    monkeypatch.setattr(counterfactual, "key_row", lambda *a: _repriced().per_key[0])
    world = _exact_world()
    # Measured 15 against a prediction of 20 (8 of 10 repriced, on 25).
    close = _read(_point("effective.model"), None, "model", outcomes.Prediction(8.0, 10.0), world)
    assert close.predicted_usd == pytest.approx(20.0) and close.verdict == "half_or_more"
    way_off = _read(_point("effective.model"), None, "model", outcomes.Prediction(60.0, 10.0), world)
    assert way_off.predicted_usd == pytest.approx(150.0) and way_off.verdict == "under_half"


def test_an_exact_fix_needs_five_units_after_pooled_over_projects(monkeypatch):
    monkeypatch.setattr(counterfactual, "key_row", lambda *a: pytest.fail("too few units"))
    prediction = outcomes.Prediction(6.0, 12.0)
    assert _read(_point("effective.model"), None, "model", prediction, _exact_world(after=4)).verdict == "too_little_data"
    # Three in each of two projects: six after in all.
    monkeypatch.setattr(counterfactual, "key_row", lambda *a: _repriced().per_key[0])
    world = _merged(_exact_world(after=3, project="proj-a"), _exact_world(after=3, project="proj-b"))
    reading = _read(_point("effective.model"), None, "model", prediction, world)
    assert reading.after_units == 6 and reading.projects == 1 and reading.verdict == "half_or_more"


def test_an_exact_fix_with_no_row_for_its_key_is_not_measured(monkeypatch):
    # A lowered window, or no old value: the counterfactual has no row for the key, on sessions enough to ask.
    # Waiting does not change that, so it is not "too little data".
    monkeypatch.setattr(counterfactual, "without_change", lambda *a: None)
    prediction = outcomes.Prediction(6.0, 12.0)
    reading = _read(_point("effective.model"), None, "model", prediction, _exact_world())
    assert reading.verdict == "not_measured" and reading.note == "no exact price for this change"
    assert reading.measured_usd is None and reading.predicted_usd is None
    assert (reading.before_units, reading.after_units, reading.projects) == (3, 6, 1)
    row = counterfactual.KeyResult("model", None, "approximate", 10.0, 25.0, "")
    only = counterfactual.Counterfactual(30.0, 45.0, "before", "", 6, [row])
    monkeypatch.setattr(counterfactual, "without_change", lambda *a: only)
    again = _read(_point("effective.model"), None, "model", prediction, _exact_world())
    assert again.verdict == "not_measured" and again.note == "no exact price for this change"


def test_an_exact_agent_fix_in_too_few_sessions_is_still_too_little_data(monkeypatch):
    # Six runs of the agent, but in two sessions: the counterfactual asks for three, and more may come.
    monkeypatch.setattr(counterfactual, "without_change", lambda *a: None)
    runs = [_agent_run(SONNET) for _ in range(3)]
    sessions = []
    for n in range(2):
        spawn = impact._Transcript(cost=1.0, turns=1, model="x")
        sessions.append(replace(_session(f"a{n}", 0.5 + n * 0.5), spawns=[("gp", spawn)] * 3))
    bundles = {s.session_id: SimpleNamespace(top=None, subs=list(runs)) for s in sessions}
    reading = _read(_point("agents.gp.model"), "gp", "model", outcomes.Prediction(6.0, 12.0), (sessions, bundles))
    assert reading.verdict == "too_little_data" and reading.note == ""


def _agent_model_card():
    """The card for ``gp``'s six Opus runs, as ``model_swap`` prices it: the saving of moving to
    Sonnet, and the cost of the runs it was priced on (a share of about 0.6)."""
    stats = model_swap.compute_model_swap([_agent_run(OPUS) for _ in range(6)], PRICING).by_key["gp"]
    return outcomes.Prediction(stats.tier_verdict.saving_usd, stats.lever.observed_cost)


def _read_agent_model(after_runs):
    before, after, bundles = _agent_model_world(after_runs, [_agent_run(OPUS) for _ in range(6)], at=T)
    point = _agent_model_point(T)
    return outcomes.read_fix(
        point, "gp", "model", _agent_model_card(), before + after, [point], bundles, PRICING, NOW, START_CORPUS
    )


def _ratio(reading):
    return reading.measured_usd / reading.predicted_usd


def test_an_agents_model_fix_that_took_on_every_run_its_file_decides_reads_as_the_card_said():
    reading = _read_agent_model([_agent_run(SONNET) for _ in range(6)])
    assert _agent_model_card().saving_usd / _agent_model_card().basis == pytest.approx(0.6, abs=0.1)
    assert reading.method == "exact" and reading.verdict == "half_or_more"
    assert _ratio(reading) == pytest.approx(1.0)
    assert reading.after_units == 6


def test_an_agents_model_fix_is_read_only_on_the_runs_its_file_decides():
    # Six more runs a spawn gave Opus: the file does not decide them, so they change nothing.
    reading = _read_agent_model([_agent_run(SONNET) for _ in range(6)] + [_agent_run(OPUS, "opus") for _ in range(6)])
    assert reading.verdict == "half_or_more"
    assert _ratio(reading) == pytest.approx(1.0)
    assert reading.after_units == 6
    # Five that the file decides still make a reading; the thirteen runs are not the units.
    five = _read_agent_model([_agent_run(SONNET) for _ in range(5)] + [_agent_run(OPUS, "opus") for _ in range(8)])
    assert five.verdict == "half_or_more"
    assert _ratio(five) == pytest.approx(1.0)
    assert five.after_units == 5


def test_an_agents_model_fix_with_every_run_given_a_model_by_its_spawn_is_too_little_data():
    for model in (OPUS, HAIKU):
        reading = _read_agent_model([_agent_run(model, "opus") for _ in range(6)])
        assert reading.verdict == "too_little_data"
        assert (reading.measured_usd, reading.predicted_usd, reading.after_units) == (None, None, 0)
    # Four that the file decides are fewer than the five a reading needs.
    four = _read_agent_model([_agent_run(SONNET) for _ in range(4)] + [_agent_run(OPUS, "opus") for _ in range(8)])
    assert four.verdict == "too_little_data" and four.measured_usd is None


def test_an_every_project_model_change_leaves_out_a_project_that_keeps_its_own_model():
    world = _main_model_world({"p": ([OPUS] * 6, [SONNET] * 6), "q": ([OPUS] * 6, [OPUS] * 6)})
    reading = _read_main_model(_main_model_point(), world)
    assert (reading.verdict, reading.after_units) == ("half_or_more", 6)
    assert reading.measured_usd / reading.predicted_usd == pytest.approx(1.0)


def test_sessions_from_before_a_project_moved_are_left_out():
    world = _main_model_world({"p": ([OPUS] * 6, [SONNET] * 6), "s": ([OPUS] * 6, [OPUS] * 3 + [SONNET] * 6)})
    reading = _read_main_model(_main_model_point(), world)
    assert (reading.after_units, reading.verdict) == (12, "half_or_more")
    assert reading.measured_usd / reading.predicted_usd == pytest.approx(1.0)


def test_picks_of_a_smaller_model_after_the_change_are_left_out_too():
    reading = _read_main_model(_main_model_point("p"), _main_model_world({"p": ([OPUS] * 6, [SONNET] * 6 + [HAIKU] * 3)}))
    assert reading.after_units == 6
    assert reading.measured_usd / reading.predicted_usd == pytest.approx(1.0)


def test_a_main_model_change_that_names_no_model_counts_every_main_session():
    # Green before the change was built: "default" (and an unset value) names no model, so nothing is left out.
    reading = _read_main_model(_main_model_point("p", new="default"), _main_model_world({"p": ([OPUS] * 6, [SONNET] * 6)}))
    assert reading.after_units == 6 and reading.verdict == "half_or_more"


@pytest.mark.parametrize("keys", [("effective.model", "effective.effortLevel"), ("effective.model", "agents.gp.tools")])
def test_a_model_change_made_with_other_settings_is_read_on_its_own_row(keys):
    # 2 sessions before (too few for the cost-per-reply method), 6 after on Sonnet.
    world = _main_model_world({"p": ([OPUS] * 2, [SONNET] * 6)})
    reading = _read_main_model(_main_model_point(keys=keys), world)
    assert reading.verdict == "half_or_more" and reading.note == ""
    assert reading.measured_usd / reading.predicted_usd == pytest.approx(1.0)


def test_a_fix_with_no_basis_is_not_measured(stub_carry, monkeypatch):
    monkeypatch.setattr(counterfactual, "without_change", lambda *a: pytest.fail("not measured"))
    world = _spawn_world(_same([60_000] * 6), _same([20_000] * 6))
    for prediction in (None, outcomes.Prediction(30.0, None), outcomes.Prediction(30.0, 0.0), outcomes.Prediction(None, None)):
        tools = _read(_point("agents.gp.tools"), "gp", "tools", prediction, world)
        assert (tools.verdict, tools.measured_usd, tools.predicted_usd) == ("not_measured", None, None)
        assert _read(_point("effective.model"), None, "model", prediction, _exact_world()).verdict == "not_measured"
    # Waiting does not change it: too few sessions and no basis is still not measured.
    short = _spawn_world(_same([60_000] * 2), _same([20_000] * 2))
    assert _read(_point("agents.gp.tools"), "gp", "tools", None, short).verdict == "not_measured"


def test_an_exact_fix_with_no_saving_to_scale_is_not_measured(monkeypatch):
    monkeypatch.setattr(counterfactual, "without_change", lambda *a: pytest.fail("not measured"))
    reading = _read(_point("effective.model"), None, "model", outcomes.Prediction(None, 12.0), _exact_world())
    assert reading.verdict == "not_measured"


def test_levers_with_no_honest_measure_are_not_measured(stub_carry):
    world = _spawn_world(_same([60_000] * 6), _same([20_000] * 6))
    prediction = outcomes.Prediction(30.0, 30_000)
    for point, agent, key in (
        (_point("effective.effortLevel"), None, "effortLevel"),
        (_point("agents.gp.effort"), "gp", "effort"),
        (_point("effective.env.MAX_THINKING_TOKENS"), None, "MAX_THINKING_TOKENS"),
        (_point("agents.gp.maxTurns"), "gp", "maxTurns"),
        (_point("effective.model"), None, ""),
    ):
        reading = _read(point, agent, key, prediction, world)
        assert (reading.method, reading.verdict, reading.measured_usd) == ("none", "not_measured", None), key
    # A card with no changes names no lever.
    agent, key = outcomes.lever_for([])
    assert _read(_point("agents.gp.tools"), agent, key, prediction, world).verdict == "not_measured"


# -- the guardrail and its control ---------------------------------------------


def _run(corrections=0, rework=0, *, messages=5, edits=5, scheduled=False):
    return quality.Run(
        group=quality.MAIN,
        human_messages=messages,
        corrections=corrections,
        edits=edits,
        rework_edits=rework,
        scheduled=scheduled,
    )


def _runs_world(before, after, *, ran=True, earlier=(), project="proj"):
    """A session per run: ``before`` and ``after`` lists of main-session runs
    either side of the change, ``earlier`` those of the fortnight before
    ``before``'s (the control's own before side). Each session ran one spawn
    of ``gp`` unless ``ran`` is false."""

    def session(sid, days, run):
        return _session(sid, days, [_spawn(60_000)] if ran else [], runs=[run], project=project)

    sessions = [session(f"b{n}", -13 + n * 0.5, run) for n, run in enumerate(before)]
    sessions += [session(f"a{n}", 0.5 + n * 0.5, run) for n, run in enumerate(after)]
    sessions += [session(f"c{n}", -27 + n * 0.5, run) for n, run in enumerate(earlier)]
    return sessions, _bundles(sessions)


def _guard(world, agent="gp", key="tools"):
    point = _point(f"agents.{agent}.{key}" if agent else f"effective.{key}")
    return _read(point, agent, key, outcomes.Prediction(None, 30_000), world)


def test_the_guardrail_says_worse_only_when_the_test_holds_with_enough_runs(stub_carry):
    # Four runs a side is too few, whatever the rise.
    few = _guard(_runs_world([_run(0)] * 4, [_run(5, messages=5)] * 4))
    assert few.guard == "too_little_data"
    assert few.guard_counts["runs"] == (4, 4)
    # Eight runs a side, corrections from none in 40 messages to 12 in 40.
    rising = [_run(3)] * 4 + [_run(0)] * 4
    worse = _guard(_runs_world([_run(0)] * 8, rising))
    assert worse.guard == "worse"
    assert worse.guard_counts["corrections"] == (0, 40, 12, 40)
    assert worse.guard_counts["runs"] == (8, 8)
    # A small rise is no clear change.
    steady = _guard(_runs_world([_run(1)] * 4 + [_run(2)] * 4, [_run(1)] * 3 + [_run(2)] * 4 + [_run(3)]))
    assert steady.guard == "holds"
    # Better is not worse.
    assert _guard(_runs_world([_run(3)] * 4 + [_run(0)] * 4, [_run(0)] * 8)).guard == "holds"


def test_a_guardrail_with_one_signal_too_thin_to_read_is_unclear(stub_carry):
    thin_edits = [_run(2, edits=1)] * 8
    reading = _guard(_runs_world(thin_edits, thin_edits))
    # Corrections can be read (8 runs, 40 messages a side) but rework has 8 edits a side.
    assert reading.guard == "unclear"
    assert reading.guard_counts["rework"] == (0, 8, 0, 8)


def test_the_guardrail_counts_only_unscheduled_main_session_runs(stub_carry):
    runs = [_run(0)] * 8 + [_run(5, scheduled=True)] * 8
    reading = _guard(_runs_world(runs, runs))
    assert reading.guard_counts["runs"] == (8, 8)
    assert reading.guard == "holds"


def test_an_agent_fixs_guardrail_reads_only_sessions_that_ran_it(stub_carry):
    steady, rising = [_run(0)] * 8, [_run(5)] * 8
    ran = _runs_world(steady, steady)
    others = _runs_world(steady, rising, ran=False)
    world = _merged(ran, others)
    # The sessions that never ran the agent got worse; the ones that did, did not.
    reading = _guard(world)
    assert reading.guard == "holds"
    assert reading.guard_counts["runs"] == (8, 8)
    # A settings fix reads every main session.
    everyone = _guard(world, agent=None, key="effortLevel")
    assert everyone.guard_counts["runs"] == (16, 16)
    assert everyone.guard == "worse"
    assert everyone.guard_counts["corrections"] == (0, 80, 40, 80)


def test_the_guardrail_is_read_for_a_fix_that_is_not_measured(stub_carry):
    world = _runs_world([_run(0)] * 8, [_run(3)] * 4 + [_run(0)] * 4)
    point = _point("agents.gp.tools")
    for prediction in (None, outcomes.Prediction(None, None)):
        reading = _read(point, "gp", "tools", prediction, world)
        assert reading.verdict == "not_measured" and reading.guard == "worse"
    effort = _read(_point("agents.gp.effort"), "gp", "effort", outcomes.Prediction(None, 5.0), world)
    assert effort.verdict == "not_measured" and effort.guard == "worse"


def test_the_control_reads_the_two_weeks_before_the_change(stub_carry):
    steady, rising = [_run(0)] * 8, [_run(3)] * 4 + [_run(0)] * 4
    # Nothing moves across the change, but the fortnight before it rose against the one before that.
    world = _runs_world(rising, rising, earlier=steady)
    reading = _guard(world)
    assert reading.guard == "holds"
    assert reading.control == "worse"
    # Sessions older than that are not read: with none in the fortnight before, the control has nothing.
    old = [_session(f"o{n}", -40 + n, [_spawn(60_000)], runs=[_run(5)]) for n in range(8)]
    bare = _runs_world(steady, steady)
    assert _guard((bare[0] + old, _bundles(bare[0] + old))).control == "too_little_data"


def test_the_control_of_an_agent_fix_reads_only_the_sessions_that_ran_it(stub_carry):
    steady, rising = [_run(0)] * 8, [_run(3)] * 4 + [_run(0)] * 4
    ran = _runs_world(steady, steady, earlier=steady)
    others = _runs_world(rising, steady, ran=False, earlier=steady)
    assert _guard(_merged(ran, others)).control == "holds"


def test_the_control_of_a_change_in_one_project_reads_that_project(stub_carry):
    steady, rising = [_run(0)] * 8, [_run(3)] * 4 + [_run(0)] * 4
    mine = _runs_world(steady, steady, earlier=steady, project="proj-a")
    theirs = _runs_world(rising, steady, earlier=steady, project="proj-b")
    point = _point("agents.gp.tools", project="proj-a")
    reading = _read(point, "gp", "tools", outcomes.Prediction(None, 30_000), _merged(mine, theirs))
    assert reading.control == "holds"
    assert reading.guard_counts["runs"] == (8, 8)

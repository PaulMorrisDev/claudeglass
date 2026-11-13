"""What the sessions since a change would have cost without it
(counterfactual.py), one test per method."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from claudeglass import compaction_sim, counterfactual, impact, outcomes, ttl
from claudeglass.change_points import ChangePoint
from claudeglass.corpus import load_corpus
from claudeglass.pricing import load_pricing, price_turn
from claudeglass.units import Units

from helpers import system_line, turn_line, write_jsonl

CHANGE = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
UNITS = Units(billing_mode="api", currency="USD")
PRICING = load_pricing()


def _write(project_dir, name, day, *, model="claude-sonnet-5", turns=8, gap_s=120, step=20_000, summarised_at=None):
    """One session of ``turns`` replies growing by ``step``. With ``summarised_at``
    it ends with an automatic summary that fired at that many tokens."""
    start = CHANGE + timedelta(days=day)
    lines = [
        turn_line(
            timestamp=(start + timedelta(seconds=gap_s * i)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            model=model,
            cache_read_input_tokens=step * i,
            cache_creation_input_tokens=5_000,
            ephemeral_5m_input_tokens=5_000,
        )
        for i in range(turns)
    ]
    if summarised_at:
        lines.append(
            system_line(
                "compact_boundary",
                timestamp=(start + timedelta(seconds=gap_s * turns)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                compactMetadata={"trigger": "auto", "preTokens": summarised_at, "postTokens": 20_000},
            )
        )
    write_jsonl(project_dir / f"{name}.jsonl", lines)


def _setup(tmp_path, *, before_model="claude-sonnet-5", after_model="claude-sonnet-5", after_summarised_at=None, **kw):
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    for n, day in enumerate((-3, -2, -1)):
        _write(project_dir, f"b{n}", day, model=before_model, **kw)
    for n, day in enumerate((0.1, 0.2, 0.3)):
        _write(project_dir, f"a{n}", day, model=after_model, summarised_at=after_summarised_at, **kw)
    corpus = load_corpus([project_dir])
    facts = impact.session_facts(corpus, PRICING)
    point = ChangePoint(CHANGE, "apply", "x")
    before, after = impact.sides(point, facts, now=CHANGE + timedelta(days=1))
    bundles = {b.session_id: b for b in corpus.sessions}
    return before, after, bundles


def _after_turns(after, bundles):
    return [turn for s in after for turn in bundles[s.session_id].top.turns if turn.turn_index > 0]


def _point(key, old, new, agent=None):
    return ChangePoint(CHANGE, "apply", "x", keys=[key], changes=[{"key": key, "agent": agent, "old": old, "new": new}])


def test_a_model_change_prices_the_same_replies_at_the_old_model(tmp_path):
    before, after, bundles = _setup(tmp_path, before_model="claude-opus-5-5")
    result = counterfactual.without_change(
        _point("model", "claude-opus-5-5", "claude-sonnet-5"), before, after, bundles, PRICING
    )
    opus = PRICING.resolve_model("claude-opus-5-5")
    assert result.fidelity == "repriced"
    assert result.paid_usd == pytest.approx(sum(s.cost for s in after))
    assert result.without_usd == pytest.approx(sum(price_turn(t, opus).total for t in _after_turns(after, bundles)))
    assert result.saved_usd > 0
    assert result.to_dict(UNITS)["text"].startswith("Without this change: about 0.")


def test_an_unset_old_model_is_the_one_the_sessions_before_ran_on(tmp_path):
    before, after, bundles = _setup(tmp_path, before_model="claude-opus-5-5")
    result = counterfactual.without_change(_point("model", None, "sonnet"), before, after, bundles, PRICING)
    opus = PRICING.resolve_model("claude-opus-5-5")
    assert result.without_usd == pytest.approx(sum(price_turn(t, opus).total for t in _after_turns(after, bundles)))
    assert "claude-opus-5-5" in result.basis


OPUS, SONNET = "claude-opus-5", "claude-sonnet-5"


def _agent_run(model, spawn_model=None):
    """One run of the agent ``gp``: a single reply on ``model``. With ``spawn_model``
    the spawn named a model of its own, so the agent file does not decide the run's."""
    from test_model_swap import _transcript, _turn

    return _transcript(
        [_turn(model=model, input_tokens=100_000, output_tokens=10_000, turn_index=1)],
        kind="subagent",
        agent_type="gp",
        agent_model_alias=spawn_model,
    )


def _run_cost(run):
    return sum(price_turn(t, PRICING.resolve_model(t.model)).total for t in run.turns)


def _agent_model_world(after_runs, before_runs, *, at=CHANGE):
    """One session per run, ``before_runs`` in the days before ``at``, ``after_runs``
    in the days after it; returns ``(before, after, bundles)`` as ``sides`` does."""
    sides, bundles = ([], []), {}
    for side, (label, runs) in enumerate((("b", before_runs), ("a", after_runs))):
        for n, run in enumerate(runs):
            sid = f"{label}{n}"
            start = at + timedelta(days=-5 + n * 0.3 if side == 0 else 0.2 + n * 0.3)
            spawn = impact._Transcript(cost=_run_cost(run), turns=1, model="x")
            sides[side].append(
                impact.SessionFacts(
                    start=start, main=impact._Transcript(cost=1.0, turns=2), session_id=sid, spawns=[("gp", spawn)], project="p"
                )
            )
            bundles[sid] = SimpleNamespace(top=None, subs=[run])
    return sides[0], sides[1], bundles


def _agent_model_point(at=CHANGE):
    change = {"key": "model", "agent": "gp", "old": OPUS, "new": SONNET}
    return ChangePoint(at, "config", "x", keys=["agents.gp.model"], changes=[change])


def test_an_agents_model_change_prices_only_the_runs_its_agent_file_decides():
    file_runs = [_agent_run(SONNET) for _ in range(6)]
    spawn_runs = [_agent_run(OPUS, "opus") for _ in range(6)]
    before, after, bundles = _agent_model_world(file_runs + spawn_runs, [_agent_run(OPUS) for _ in range(6)])
    result = counterfactual.without_change(_agent_model_point(), before, after, bundles, PRICING)
    row = result.per_key[0]
    opus = PRICING.resolve_model(OPUS)
    assert row.fidelity == "repriced"
    # Paid and without cover the six runs the file decides; the spawn-set runs are in neither.
    assert row.paid_usd == pytest.approx(sum(_run_cost(r) for r in file_runs))
    assert row.without_usd == pytest.approx(sum(price_turn(t, opus).total for r in file_runs for t in r.turns))
    assert row.without_usd > row.paid_usd
    assert row.basis == f"gp's replies priced at {OPUS}, in the 6 of 12 runs that left the model to its agent file."
    # The sessions' other cost is the same either way.
    assert result.without_usd - result.paid_usd == pytest.approx(row.without_usd - row.paid_usd)


def test_an_agents_model_change_with_every_run_left_to_its_file_keeps_the_plain_basis():
    before, after, bundles = _agent_model_world([_agent_run(SONNET) for _ in range(6)], [_agent_run(OPUS) for _ in range(6)])
    row = counterfactual.without_change(_agent_model_point(), before, after, bundles, PRICING).per_key[0]
    assert row.basis == f"gp's replies priced at {OPUS}."


def test_an_agents_model_change_no_run_left_to_its_file_has_not_changed_what_you_paid():
    runs = [_agent_run(OPUS, "opus") for _ in range(6)]
    before, after, bundles = _agent_model_world(runs, [_agent_run(OPUS) for _ in range(6)])
    result = counterfactual.without_change(_agent_model_point(), before, after, bundles, PRICING)
    # Not the cost per reply of the sessions before: a row of its own, with nothing priced.
    assert result.fidelity == "repriced"
    row = result.per_key[0]
    assert (row.paid_usd, row.without_usd) == (0.0, 0.0)
    assert row.basis == (
        "No run of gp since this change left the model to its agent file, so the change has not changed what you paid."
    )
    assert result.without_usd == pytest.approx(result.paid_usd)


def _main_run(model):
    """One main session: a single reply of 100,000 tokens in and 10,000 out on ``model``."""
    from test_model_swap import _transcript, _turn

    return _transcript([_turn(model=model, input_tokens=100_000, output_tokens=10_000, turn_index=1)], kind="top-level")


def _main_model_world(sides):
    """``sides`` maps a project to ``(models before, models after)``, one main session each;
    returns ``(sessions, bundles)``. Real transcripts, so the real pricing prices them."""
    sessions, bundles = [], {}
    for project, (before, after) in sides.items():
        for side, models, first in (("b", before, -5), ("a", after, 0.2)):
            for n, model in enumerate(models):
                sid = f"{project}-{side}{n}"
                top = _main_run(model)
                sessions.append(
                    impact.SessionFacts(
                        start=CHANGE + timedelta(days=first + n * 0.3),
                        main=impact._Transcript(cost=_run_cost(top), turns=1, model=model),
                        session_id=sid,
                        spawns=[],
                        project=project,
                    )
                )
                bundles[sid] = SimpleNamespace(top=top, subs=[])
    return sessions, bundles


#: The card's share for an Opus to Sonnet move on one such reply (0.6), priced on 100.
SHARE = 1 - _run_cost(_main_run(SONNET)) / _run_cost(_main_run(OPUS))


def _main_model_point(project="", new=SONNET, keys=("effective.model",)):
    change = {"key": "model", "agent": None, "old": OPUS, "new": new}
    return ChangePoint(CHANGE, "config", "x", keys=list(keys), changes=[change], project=project)


def _read_main_model(point, world):
    sessions, bundles = world
    return outcomes.read_fix(
        point, None, "model", outcomes.Prediction(SHARE * 100, 100.0), sessions, [point], bundles, PRICING,
        CHANGE + timedelta(days=20), CHANGE - timedelta(days=30),
    )


def _main_model_card(point, world):
    sessions, bundles = world
    before = [s for s in sessions if "-b" in s.session_id]
    after = [s for s in sessions if "-a" in s.session_id]
    return counterfactual.without_change(point, before, after, bundles, PRICING)


def test_a_main_model_change_counts_only_the_sessions_that_ran_on_the_model_it_sets():
    # 4 on Sonnet, then 8 picks of Opus: 4 units, under 5.
    few = _read_main_model(_main_model_point("p"), _main_model_world({"p": ([OPUS] * 6, [SONNET] * 4 + [OPUS] * 8)}))
    assert few.verdict == "too_little_data" and few.measured_usd is None
    # 5 on Sonnet, then 8 picks: the 5 are read, exactly as the card said.
    world = _main_model_world({"p": ([OPUS] * 6, [SONNET] * 5 + [OPUS] * 8)})
    five = _read_main_model(_main_model_point("p"), world)
    assert (five.verdict, five.after_units) == ("half_or_more", 5)
    assert five.measured_usd / five.predicted_usd == pytest.approx(1.0)
    row = _main_model_card(_main_model_point("p"), world).per_key[0]
    assert (row.paid_usd, row.without_usd) == (pytest.approx(1.5), pytest.approx(3.75))
    assert row.basis == (
        "Main-session replies priced at claude-opus-5, in the 5 of 13 sessions that ran on the model the change sets."
    )


def test_a_project_already_on_the_new_model_before_the_change_is_left_out():
    world = _main_model_world({"p": ([OPUS] * 6, [SONNET] * 6), "r": ([SONNET] * 6, [SONNET] * 6)})
    reading = _read_main_model(_main_model_point(), world)
    assert reading.after_units == 6 and reading.measured_usd == pytest.approx(2.7)
    card = _main_model_card(_main_model_point(), world)
    assert card.saved_usd == pytest.approx(2.7)
    assert card.per_key[0].basis.endswith("in the 6 of 12 sessions that ran on the model the change sets.")


def test_no_main_session_on_the_model_a_change_sets_gives_a_zero_row():
    world = _main_model_world({"p": ([OPUS] * 6, [OPUS] * 6)})
    card = _main_model_card(_main_model_point("p"), world)
    row = card.per_key[0]
    assert (row.fidelity, row.paid_usd, row.without_usd) == ("repriced", 0.0, 0.0)
    assert row.basis == (
        "No main session since this change ran on the model it sets, so the change has not changed what you paid."
    )
    assert counterfactual.headline(card, UNITS) == (
        "Without this change: about 4.50 USD. You paid 4.50 USD, about the same."
    )
    assert _read_main_model(_main_model_point("p"), world).verdict == "too_little_data"


def test_fast_mode_is_repriced_the_other_way(tmp_path):
    before, after, bundles = _setup(tmp_path, after_model="claude-opus-5-5")
    result = counterfactual.without_change(_point("fastMode", True, False), before, after, bundles, PRICING)
    expected = sum(
        price_turn(replace(t, speed="fast"), PRICING.resolve_model(t.model)).total for t in _after_turns(after, bundles)
    )
    assert result.fidelity == "repriced"
    assert result.without_usd == pytest.approx(expected)
    assert result.without_usd > result.paid_usd


def test_a_cache_lifetime_change_matches_the_ttl_replay(tmp_path):
    before, after, bundles = _setup(tmp_path, gap_s=600)
    result = counterfactual.without_change(_point("promptCacheTtl", "1h", "5m"), before, after, bundles, PRICING)
    delta = sum(
        ttl.simulate(bundles[s.session_id].top.turns, PRICING.resolve_model, ttl.POLICY_1H).cost
        - ttl.simulate(bundles[s.session_id].top.turns, PRICING.resolve_model, ttl.POLICY_5M).cost
        for s in after
    )
    assert result.fidelity == "simulated"
    assert result.saved_usd == pytest.approx(delta)
    assert delta != 0


def test_a_raised_compaction_window_replays_the_old_one(tmp_path):
    before, after, bundles = _setup(tmp_path, step=40_000)
    result = counterfactual.without_change(
        _point("autoCompactWindow", 100_000, 200_000), before, after, bundles, PRICING
    )
    tops = [bundles[s.session_id].top for s in after]
    assert result.fidelity == "simulated"
    assert result.without_usd == pytest.approx(compaction_sim.replay_cost(tops, PRICING.resolve_model, 100_000))
    assert compaction_sim.replay_cost(tops, PRICING.resolve_model, None) == pytest.approx(result.paid_usd)


def test_the_old_window_fires_below_it_by_the_gap_the_sessions_left_under_the_new_one(tmp_path):
    """The sessions since the change ran under a 400,000 window and summarised
    at 390,000, a gap of 10,000. Replayed under the old 300,000 window that
    gap puts the summary at 290,000, not the 267,000 the 33,000 default would.
    The counterfactual hands the replay the window the sessions ran under, so
    it measures the first."""
    before, after, bundles = _setup(tmp_path, step=40_000, after_summarised_at=390_000)
    result = counterfactual.without_change(
        _point("autoCompactWindow", 300_000, 400_000), before, after, bundles, PRICING
    )
    tops = [bundles[s.session_id].top for s in after]
    ran_under = {top.meta.session_id: 400_000 for top in tops}
    lookup = PRICING.resolve_model

    assert result.fidelity == "simulated"
    assert result.without_usd == pytest.approx(compaction_sim.replay_cost(tops, lookup, 300_000, snapshot_windows=ran_under))
    # The gap is measured, not the 33,000 default: the two replays differ.
    assert compaction_sim.replay_cost(tops, lookup, 300_000) != pytest.approx(result.without_usd)


def test_a_lowered_compaction_window_falls_back_to_the_sessions_before(tmp_path):
    before, after, bundles = _setup(tmp_path, step=40_000)
    result = counterfactual.without_change(
        _point("autoCompactWindow", 200_000, 100_000), before, after, bundles, PRICING
    )
    assert result.fidelity == "before"
    per_reply = sum(s.cost for s in before) / sum(s.main.turns for s in before)
    assert result.without_usd == pytest.approx(sum(s.main.turns for s in after) * per_reply)
    assert result.per_key == []


def test_the_before_method_prices_each_kind_of_work_at_its_own_rate(tmp_path):
    before, after, bundles = _setup(tmp_path)
    for s, task in zip(before, ("feature", "feature", "debug")):
        s.task = task
    for s, task in zip(after, ("debug", "debug", "debug")):
        s.task = task
    result = counterfactual.without_change(_point("effortLevel", "high", "medium"), before, after, bundles, PRICING)
    debug = before[2]
    assert result.fidelity == "before"
    assert result.without_usd == pytest.approx(sum(s.main.turns for s in after) * debug.cost / debug.main.turns)


def test_a_claude_md_size_change_is_carried_on_every_reply(tmp_path):
    before, after, bundles = _setup(tmp_path)
    point = ChangePoint(
        CHANGE, "transcript", "CLAUDE.md size changed", keys=["claude_md_chars"],
        changes=[{"key": "claude_md_chars", "agent": None, "old": 40_000, "new": 20_000}],
    )
    result = counterfactual.without_change(point, before, after, bundles, PRICING)
    assert result.fidelity == "approximate"
    assert result.saved_usd > 0
    assert "About 5,000 tokens more carried" in result.basis


def _write_start(project_dir, name, day, model, start_read):
    """A session whose first call is ``start_read`` tokens of shared prefix."""
    start = CHANGE + timedelta(days=day)
    write_jsonl(
        project_dir / f"{name}.jsonl",
        [
            turn_line(
                timestamp=(start + timedelta(seconds=120 * i)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                model=model,
                cache_read_input_tokens=start_read + 1_000 * i,
                cache_creation_input_tokens=5_000,
                ephemeral_5m_input_tokens=5_000,
            )
            for i in range(8)
        ],
    )


def _start_setup(tmp_path, before, after):
    """``before`` and ``after`` are lists of (model, tokens read at the start)."""
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    for n, (model, read) in enumerate(before):
        _write_start(project_dir, f"b{n}", -3 + n * 0.5, model, read)
    for n, (model, read) in enumerate(after):
        _write_start(project_dir, f"a{n}", 0.1 + n * 0.1, model, read)
    corpus = load_corpus([project_dir])
    facts = impact.session_facts(corpus, PRICING)
    point = ChangePoint(CHANGE, "apply", "x")
    sides = impact.sides(point, facts, now=CHANGE + timedelta(days=1))
    return sides[0], sides[1], {b.session_id: b for b in corpus.sessions}


_PLUGINS = ChangePoint(
    CHANGE, "apply", "x", keys=["enabledPlugins"],
    changes=[{"key": "enabledPlugins", "agent": None, "old": ["a"], "new": []}],
)


def test_a_change_in_the_start_of_context_is_read_on_the_model_both_sides_ran_on(tmp_path):
    """Moving from Haiku 4.5 (51.5k of tools) to Sonnet 5 (69.4k) must not
    read as a change that added 18k tokens to every reply."""
    mixed = _start_setup(
        tmp_path,
        [("claude-sonnet-5", 70_000)] * 3,
        [("claude-sonnet-5", 60_000)] * 3 + [("claude-haiku-4-5", 20_000)] * 2,
    )
    result = counterfactual.without_change(_PLUGINS, *mixed, PRICING)
    assert result.fidelity == "approximate"
    # 70k - 60k+5k written = a drop of exactly 10,000 on the one shared model.
    assert "About 10,000 tokens more carried" in result.basis
    assert result.saved_usd > 0


def test_no_start_of_context_figure_when_the_two_sides_share_no_model(tmp_path):
    apart = _start_setup(tmp_path, [("claude-haiku-4-5", 46_500)] * 3, [("claude-sonnet-5", 64_400)] * 3)
    result = counterfactual.without_change(_PLUGINS, *apart, PRICING)
    # No carried figure to give, so the headline falls back to the sessions before.
    assert result.per_key == [] and result.fidelity == "before"


class _StubCarry:
    """Stands in for ``context_files._Carry``: ``cost(chars, start, end)`` is
    ``chars / 1000`` and every call is kept in ``calls``."""

    calls: list = []

    def __init__(self, sub, pricing):
        self.sub = sub

    def cost(self, chars, start, end):
        _StubCarry.calls.append((self.sub, chars, start, end))
        return chars / 1000


@pytest.fixture
def stub_carry(monkeypatch):
    _StubCarry.calls = []
    monkeypatch.setattr(counterfactual, "_Carry", _StubCarry)
    return _StubCarry


def _spawn_world(agents, *, replies=4):
    """``agents`` maps an agent type to ``(before, after)``, each a list of
    ``(model family, start tokens)``. Every spawn is in a session of its own
    that has one spawn of every agent type; returns ``(before, after, bundles)``
    with stand-ins for the transcripts the carry prices."""
    sides, bundles = ([], []), {}
    for side, label in ((0, "b"), (1, "a")):
        count = max(len(spec[side]) for spec in agents.values())
        for n in range(count):
            spawns = [
                (agent, impact._Transcript(cost=1.0, turns=replies, startup_tokens=spec[side][n][1], model=spec[side][n][0]))
                for agent, spec in agents.items()
                if n < len(spec[side])
            ]
            sid = f"{label}{n}"
            start = CHANGE + timedelta(days=-3 + n * 0.1 if side == 0 else 0.1 + n * 0.1)
            sides[side].append(
                impact.SessionFacts(start=start, main=impact._Transcript(cost=2.0, turns=5), session_id=sid, spawns=spawns)
            )
            subs = [SimpleNamespace(turns=[SimpleNamespace(turn_index=i) for i in range(replies + 1)]) for _ in spawns]
            bundles[sid] = SimpleNamespace(subs=subs)
    return sides[0], sides[1], bundles


def _same_family(tokens, family="sonnet-5"):
    return [(family, t) for t in tokens]


def test_an_agents_start_reading_is_the_drop_in_median_start_tokens_on_one_family():
    before, after, _bundles = _spawn_world({"gp": (_same_family([60_000] * 5 + [90_000]), _same_family([20_000] * 6))})
    reading = counterfactual.agent_start_reading("gp", before, after)
    assert reading.agent == "gp" and reading.family == "sonnet-5"
    assert (reading.before_n, reading.after_n) == (6, 6)
    # The median, so the one 90,000 start does not move it.
    assert reading.drop_tokens == 40_000
    assert reading.after_spawns == [(f"a{n}", 0) for n in range(6)]


def test_an_agent_start_reading_keeps_the_before_median_of_its_family():
    before_spec = _same_family([50_000, 60_000, 61_000, 62_000, 70_000]) + _same_family([200_000] * 6, "haiku-4-5")
    before, after, _bundles = _spawn_world({"gp": (before_spec, _same_family([20_000] * 6))})
    reading = counterfactual.agent_start_reading("gp", before, after)
    assert reading.family == "sonnet-5"
    assert reading.before_tokens == 61_000


def test_an_agents_start_reading_holds_the_family_with_most_spawns_on_both_sides():
    haiku = [("haiku-4-5", 50_000)] * 5
    before, after, _bundles = _spawn_world(
        {"gp": (haiku + _same_family([60_000] * 8), _same_family([20_000] * 6) + [("haiku-4-5", 30_000)] * 6)}
    )
    reading = counterfactual.agent_start_reading("gp", before, after)
    # Haiku: 5 before, 6 after -> 5 between them. Sonnet: 8 and 6 -> 6.
    assert reading.family == "sonnet-5"
    assert reading.drop_tokens == 40_000
    assert [i for _sid, i in reading.after_spawns] == [0] * 6
    assert {sid for sid, _i in reading.after_spawns} == {f"a{n}" for n in range(6)}


def test_a_tie_between_two_shared_families_goes_to_the_first_name():
    ran_before = [("sonnet-5", 60_000)] * 5 + [("haiku-4-5", 50_000)] * 5
    ran_after = [("sonnet-5", 20_000)] * 5 + [("haiku-4-5", 10_000)] * 5
    before, after, _bundles = _spawn_world({"gp": (ran_before, ran_after)})
    reading = counterfactual.agent_start_reading("gp", before, after)
    assert (reading.family, reading.drop_tokens) == ("haiku-4-5", 40_000)


def test_an_agents_start_reading_counts_only_its_own_spawns_with_a_recorded_start():
    before, after, _bundles = _spawn_world(
        {
            "gp": (_same_family([60_000] * 5 + [0]), _same_family([20_000] * 5 + [0])),
            "other": (_same_family([99_000] * 6), _same_family([1_000] * 6)),
        }
    )
    reading = counterfactual.agent_start_reading("gp", before, after)
    assert (reading.before_n, reading.after_n, reading.drop_tokens) == (5, 5, 40_000)
    assert [sid for sid, _i in reading.after_spawns] == [f"a{n}" for n in range(5)]
    assert counterfactual.agent_start_reading("nobody", before, after) is None


def test_fewer_than_five_spawns_a_side_gives_no_reading():
    before, after, _bundles = _spawn_world({"gp": (_same_family([60_000] * 6), _same_family([20_000] * 4))})
    assert counterfactual.agent_start_reading("gp", before, after) is None
    before, after, _bundles = _spawn_world({"gp": (_same_family([60_000] * 4), _same_family([20_000] * 6))})
    assert counterfactual.agent_start_reading("gp", before, after) is None
    assert counterfactual.MIN_SPAWNS == 5


def test_no_shared_model_family_gives_no_reading():
    before, after, _bundles = _spawn_world(
        {"gp": (_same_family([60_000] * 6, "haiku-4-5"), _same_family([20_000] * 6, "sonnet-5"))}
    )
    assert counterfactual.agent_start_reading("gp", before, after) is None


def test_the_carry_prices_the_drop_on_each_after_spawn_of_the_reading(stub_carry):
    before, after, bundles = _spawn_world({"gp": (_same_family([60_000] * 6), _same_family([20_000] * 6))}, replies=4)
    reading = counterfactual.agent_start_reading("gp", before, after)
    # 40,000 tokens at 4 characters each, on each of the 6 spawns.
    assert counterfactual.carry_on(reading, 40_000, bundles, PRICING) == pytest.approx(6 * 160.0)
    assert len(stub_carry.calls) == 6
    assert {(chars, start, end) for _sub, chars, start, end in stub_carry.calls} == {(160_000, 0, 4)}
    assert [call[0] for call in stub_carry.calls] == [bundles[f"a{n}"].subs[0] for n in range(6)]


def test_the_carry_is_negative_when_the_tokens_are_negative(stub_carry):
    before, after, bundles = _spawn_world({"gp": (_same_family([20_000] * 6), _same_family([60_000] * 6))})
    reading = counterfactual.agent_start_reading("gp", before, after)
    assert reading.drop_tokens == -40_000
    assert counterfactual.carry_on(reading, reading.drop_tokens, bundles, PRICING) == pytest.approx(-6 * 160.0)
    assert counterfactual.carry_on(reading, 0, bundles, PRICING) == 0


def _agent_point(*labels):
    return ChangePoint(CHANGE, "config", "x", keys=list(labels))


def test_an_agent_tools_change_is_priced_on_the_drop_in_its_start_context(stub_carry):
    before, after, bundles = _spawn_world({"gp": (_same_family([60_000] * 6), _same_family([20_000] * 6))})
    result = counterfactual.without_change(_agent_point("agents.gp.tools"), before, after, bundles, PRICING)
    assert result.fidelity == "approximate"
    assert result.basis == ""
    assert result.sessions == 6
    # 6 spawns, each carrying 40,000 tokens at 4 characters, at the stub's 1/1000.
    assert result.saved_usd == pytest.approx(6 * 160.0)
    assert result.paid_usd == pytest.approx(6 * 3.0)
    [row] = result.per_key
    assert (row.key, row.agent, row.fidelity, row.basis) == ("tools", "gp", "approximate", "")
    assert row.paid_usd == pytest.approx(6.0)
    assert row.without_usd - row.paid_usd == pytest.approx(960.0)
    text = result.to_dict(UNITS)
    assert text["fidelity_text"] == counterfactual.FIDELITY_TEXT["approximate"]
    assert text["since_text"].startswith("the 6 sessions started since would have cost")


def test_an_agent_tools_change_that_added_context_cost_more(stub_carry):
    before, after, bundles = _spawn_world({"gp": (_same_family([20_000] * 6), _same_family([60_000] * 6))})
    result = counterfactual.without_change(_agent_point("agents.gp.tools"), before, after, bundles, PRICING)
    assert result.fidelity == "approximate"
    assert result.saved_usd == pytest.approx(-6 * 160.0)


def test_fewer_than_five_spawns_a_side_gives_no_figure(stub_carry):
    before, after, bundles = _spawn_world({"gp": (_same_family([60_000] * 6), _same_family([20_000] * 4))})
    assert counterfactual.without_change(_agent_point("agents.gp.tools"), before, after, bundles, PRICING) is None


def test_no_shared_model_family_gives_no_figure(stub_carry):
    before, after, bundles = _spawn_world(
        {"gp": (_same_family([60_000] * 6, "haiku-4-5"), _same_family([20_000] * 6, "sonnet-5"))}
    )
    assert counterfactual.without_change(_agent_point("agents.gp.tools"), before, after, bundles, PRICING) is None


def test_an_agent_start_key_with_any_other_key_gives_no_figure(stub_carry):
    """The 2026-11-05 shape: one agent's tools with its model and description."""
    before, after, bundles = _spawn_world({"gp": (_same_family([60_000] * 6), _same_family([20_000] * 6))})
    point = _agent_point("agents.gp.tools", "agents.gp.model", "agents.gp.description")
    assert counterfactual.without_change(point, before, after, bundles, PRICING) is None
    # Nor with a main-session setting beside it.
    mixed = _agent_point("agents.gp.tools", "effective.effortLevel")
    assert counterfactual.without_change(mixed, before, after, bundles, PRICING) is None


def test_an_agent_start_change_where_one_agent_has_no_reading_gives_no_figure(stub_carry):
    before, after, bundles = _spawn_world(
        {
            "gp": (_same_family([60_000] * 6), _same_family([20_000] * 6)),
            "thin": (_same_family([60_000] * 6), _same_family([20_000] * 4)),
        }
    )
    point = _agent_point("agents.gp.tools", "agents.thin.tools")
    assert counterfactual.without_change(point, before, after, bundles, PRICING) is None


def test_two_start_keys_of_one_agent_are_one_reading(stub_carry):
    before, after, bundles = _spawn_world({"gp": (_same_family([60_000] * 6), _same_family([20_000] * 6))})
    for labels in (("agents.gp.tools", "agents.gp.disallowedTools"), ("agents.gp.disallowedTools", "agents.gp.tools")):
        result = counterfactual.without_change(_agent_point(*labels), before, after, bundles, PRICING)
        assert result.fidelity == "approximate"
        assert [row.key for row in result.per_key] == ["tools"]
        assert result.saved_usd == pytest.approx(6 * 160.0)
    skills = counterfactual.without_change(_agent_point("agents.gp.skills"), before, after, bundles, PRICING)
    assert [row.key for row in skills.per_key] == ["skills"]


def test_two_agents_tools_lines_add_up(stub_carry):
    before, after, bundles = _spawn_world(
        {
            "gp": (_same_family([60_000] * 6), _same_family([20_000] * 6)),
            "other": (_same_family([30_000] * 6), _same_family([20_000] * 6)),
        }
    )
    point = _agent_point("agents.gp.tools", "agents.other.tools")
    result = counterfactual.without_change(point, before, after, bundles, PRICING)
    assert result.fidelity == "approximate"
    assert [(row.agent, row.key) for row in result.per_key] == [("gp", "tools"), ("other", "tools")]
    assert [row.without_usd - row.paid_usd for row in result.per_key] == pytest.approx([6 * 160.0, 6 * 40.0])
    assert result.saved_usd == pytest.approx(6 * 160.0 + 6 * 40.0)
    assert result.paid_usd == pytest.approx(6 * 4.0)


def test_a_main_session_change_still_uses_its_own_method(tmp_path):
    """Every other kind of change keeps the method it had: this passes before
    and after the agent start-context method."""
    before, after, bundles = _setup(tmp_path)
    effort = counterfactual.without_change(_point("effortLevel", "high", "medium"), before, after, bundles, PRICING)
    assert effort.fidelity == "before" and effort.per_key == []
    # An agent's setting that is not about its start is not read on its start.
    other = counterfactual.without_change(_agent_point("agents.gp.effort"), before, after, bundles, PRICING)
    assert other.fidelity == "before" and other.per_key == []
    # Nor is a main-session key that happens to share an agent start key's name.
    main_tools = counterfactual.without_change(_agent_point("effective.tools"), before, after, bundles, PRICING)
    assert main_tools.fidelity == "before" and main_tools.per_key == []
    # A model change is still repriced, with or without an agent beside it.
    model = counterfactual.without_change(_point("model", "claude-opus-5-5", "claude-sonnet-5"), before, after, bundles, PRICING)
    assert model.fidelity == "repriced"
    (tmp_path / "start").mkdir()
    start_before, start_after, start_bundles = _start_setup(
        tmp_path / "start", [("claude-sonnet-5", 70_000)] * 3, [("claude-sonnet-5", 60_000)] * 3
    )
    carried = counterfactual.without_change(_PLUGINS, start_before, start_after, start_bundles, PRICING)
    assert carried.fidelity == "approximate" and "About 10,000 tokens more carried" in carried.basis


def test_several_settings_at_once_are_headlined_from_the_sessions_before(tmp_path):
    before, after, bundles = _setup(tmp_path)
    point = ChangePoint(
        CHANGE, "apply", "x", keys=["model", "effortLevel"],
        changes=[
            {"key": "model", "agent": None, "old": "claude-opus-5-5", "new": "claude-sonnet-5"},
            {"key": "effortLevel", "agent": None, "old": "high", "new": "medium"},
        ],
    )
    result = counterfactual.without_change(point, before, after, bundles, PRICING)
    assert result.fidelity == "before"
    assert [row.key for row in result.per_key] == ["model"]
    assert "don't add up" in result.basis


def test_too_few_sessions_after_gives_no_figure(tmp_path):
    before, after, bundles = _setup(tmp_path)
    assert counterfactual.without_change(_point("model", "claude-opus-5-5", "x"), before, after[:2], bundles, PRICING) is None


def test_the_headline_says_saved_or_cost_more():
    saved = counterfactual.Counterfactual(10.0, 12.5, "before", "", 3)
    assert counterfactual.headline(saved, UNITS) == "Without this change: about 12.50 USD. You paid 10.00 USD, so it saved about 2.50 USD."
    cost = counterfactual.Counterfactual(10.0, 8.0, "before", "", 3)
    assert counterfactual.headline(cost, UNITS).endswith("so it cost about 2.00 USD more.")
    same = counterfactual.Counterfactual(10.0, 10.001, "before", "", 3)
    assert counterfactual.headline(same, UNITS).endswith("about the same.")


def test_the_overview_sentence_counts_the_sessions_since_and_the_difference():
    saved = counterfactual.Counterfactual(10.0, 12.5, "before", "", 4)
    assert counterfactual.since_line(saved, UNITS) == (
        "the 4 sessions started since would have cost about 12.50 USD: 2.50 USD more than you paid."
    )
    cost = counterfactual.Counterfactual(10.0, 8.0, "before", "", 4)
    assert counterfactual.since_line(cost, UNITS).endswith(": 2.00 USD less than you paid.")
    assert counterfactual.since_line(counterfactual.Counterfactual(10.0, 10.0, "before", "", 4), UNITS).endswith(
        ", about what you paid."
    )
    assert saved.to_dict(UNITS)["since_text"] == counterfactual.since_line(saved, UNITS)

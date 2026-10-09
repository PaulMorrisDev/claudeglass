"""What the sessions since a change would have cost without it
(counterfactual.py), one test per method."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from claudeglass import compaction_sim, counterfactual, impact, ttl
from claudeglass.change_points import ChangePoint
from claudeglass.corpus import load_corpus
from claudeglass.pricing import load_pricing, price_turn
from claudeglass.units import Units

from helpers import turn_line, write_jsonl

CHANGE = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
UNITS = Units(billing_mode="api", currency="USD")
PRICING = load_pricing()


def _write(project_dir, name, day, *, model="claude-sonnet-5", turns=8, gap_s=120, step=20_000):
    start = CHANGE + timedelta(days=day)
    write_jsonl(
        project_dir / f"{name}.jsonl",
        [
            turn_line(
                timestamp=(start + timedelta(seconds=gap_s * i)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                model=model,
                cache_read_input_tokens=step * i,
                cache_creation_input_tokens=5_000,
                ephemeral_5m_input_tokens=5_000,
            )
            for i in range(turns)
        ],
    )


def _setup(tmp_path, *, before_model="claude-sonnet-5", after_model="claude-sonnet-5", **kw):
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    for n, day in enumerate((-3, -2, -1)):
        _write(project_dir, f"b{n}", day, model=before_model, **kw)
    for n, day in enumerate((0.1, 0.2, 0.3)):
        _write(project_dir, f"a{n}", day, model=after_model, **kw)
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
    before, after, bundles = _setup(tmp_path)
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

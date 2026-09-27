"""Did it work? Sessions before a change against sessions after it
(``impact``)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from claudeglass import impact
from claudeglass.change_points import ChangePoint
from claudeglass.corpus import load_corpus
from claudeglass.impact import Measure, SessionFacts, _Transcript
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
    assert [m.key for m in impact.measures_for(ChangePoint(CHANGE, "apply", "x", keys=["model"]))] == [
        "cost_per_turn",
        "cost_per_session",
    ]
    agent = impact.measures_for(ChangePoint(CHANGE, "apply", "x", keys=["Explore: model"]))
    assert [(m.key, m.agent) for m in agent][:2] == [("agent_cost", "Explore"), ("agent_startup", "Explore")]
    ttl = impact.measures_for(ChangePoint(CHANGE, "config", "x", keys=["effective.promptCacheTtl"]))
    assert ttl[0].key == "rebuild_share"
    assert impact.measures_for(ChangePoint(CHANGE, "apply", "x"))[0].key == "startup_tokens"


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

    note_text = "ClaudeGlass metrics capture (tl-cap v1 task,level,size): ..."
    note = attachment_line(
        "hook_additional_context",
        rendered=f"<system-reminder>\nSessionStart hook additional context: {note_text}\n</system-reminder>",
        content=[note_text], hookName="SessionStart", hookEvent="SessionStart", toolUseID="SessionStart",
    )
    note["timestamp"] = "2026-09-18T11:59:59.000Z"
    lines = [note]
    for n, tag in enumerate(("task=feature level=hard size=l", "task=feature level=hard size=m", "task=feature level=hard size=l")):
        lines.append(user_str_line("go on", origin={"kind": "human"}, timestamp=f"2026-09-18T12:00:{2 * n:02d}.000Z"))
        lines.append(turn_line(content=[{"type": "text", "text": f"Done.\n[tl: {tag}]"}], timestamp=f"2026-09-18T12:00:{2 * n + 1:02d}.000Z"))
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

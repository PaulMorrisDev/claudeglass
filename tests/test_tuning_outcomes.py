"""The ``outcomes`` block of the tuning export (``tuning._outcomes``): what became
of the fixes that landed, a card the dashboard showed or a profile change whose
estimate was logged, read by ``outcomes.read_fix``. The comparison itself is
tried in ``tests/test_outcomes.py``; this holds the block's wiring in ``build``,
what it counts, its place in the spec and its lines in the summary. It reuses
the world on disk of ``tests/test_tuning.py``, with the change points and the
readings put in by hand.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from claudeglass import change_points as change_points_mod
from claudeglass import impact as impact_mod
from claudeglass import outcomes as outcomes_mod
from claudeglass import tuning
from claudeglass.change_points import ChangePoint

from helpers import assert_privacy_deep
from test_tuning import _build, _plain, _put, _sample, world  # noqa: F401  (fixtures)

#: In the world's window (30 days to 2026-09-19, so it starts 2026-08-21).
SEP_10 = datetime(2026, 9, 10, 8, 31, tzinfo=timezone.utc)
SEP_12 = datetime(2026, 9, 12, 19, 49, tzinfo=timezone.utc)
AUG_25 = datetime(2026, 8, 25, 9, 0, tzinfo=timezone.utc)
JUL_01 = datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc)


def _stamp(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _tools(agent: str) -> list[dict]:
    return [{"target": "agent", "key": "tools", "agent": agent, "value": ["Read"]}]


def _row(key, rule="spawn-tools-list", agent="general-purpose", *, project="*", folders=(), changes=None,
         landed=SEP_10, saving=2.0, priced_on=1000.0):
    """One ``Store.card_marks`` row of a card that landed."""
    return {
        "card_key": key,
        "project": project,
        "folders": list(folders),
        "rule_id": rule,
        "agent_type": agent,
        "changes": _tools(agent) if changes is None else changes,
        "saving_usd": saving,
        "priced_on": priced_on,
        "first_shown_at": _stamp(landed or SEP_10),
        "landed_at": _stamp(landed) if landed else None,
        "landed_exact": 0 if landed else None,
    }


def _point(ts, keys, project="", source="config"):
    return ChangePoint(ts, source, "x", keys=list(keys), project=project)


def _prediction(ts, measure_key, agent=None, *, predicted=5.0, priced_on=100.0, source="whatif"):
    return {
        "id": f"pred-{measure_key}-{agent}", "ts": _stamp(ts), "source": source, "measure_key": measure_key,
        "agent": agent, "predicted_usd": predicted, "predicted_pct": None, "fidelity": "ceiling",
        "priced_on": priced_on,
    }


def _inputs(rows=(), predictions=(), folders=("proj-alpha", "proj-beta"), every_project=True):
    return tuning.OutcomeInputs(list(rows), list(predictions), frozenset(folders), every_project)


@pytest.fixture()
def points(monkeypatch):
    """The change points ``build`` reads, put in by hand, and no sessions."""
    found: list[ChangePoint] = []
    monkeypatch.setattr(change_points_mod, "change_points", lambda config_dir, corpus=None, **kw: found)
    monkeypatch.setattr(impact_mod, "session_facts", lambda corpus, pricing: [])
    return found


def _reading(verdict="too_little_data", *, predicted=None, measured=None, guard="too_little_data",
             control="too_little_data", short=False):
    return outcomes_mod.Reading(
        method="start_context", verdict=verdict, predicted_usd=predicted, measured_usd=measured,
        guard=guard, control=control, short_export=short,
    )


def _rows(doc, name):
    return {(r.get("rule") or r.get("lever"), r["agent"]): r for r in doc["outcomes"][name]}


# -- what the block counts ---------------------------------------------------------------------


def test_the_outcomes_block_matches_the_designs_reference_figures(world, points):
    # Five fixes: one general-purpose tools line, and four custom agents' tools lines that share a point.
    points += [
        _point(SEP_10, ["agents.general-purpose.tools"]),
        _point(SEP_12, [f"agents.{name}.tools" for name in ("zeta", "eta", "theta", "iota")]),
    ]
    rows = [_row("gp-all", landed=SEP_10)] + [
        _row(f"c-{name}", agent=name, landed=SEP_12) for name in ("zeta", "eta", "theta", "iota")
    ]
    doc = _build(world, outcomes=_inputs(rows))
    assert doc["outcomes"] == {
        "landed": 5,
        "short_export": 0,
        "controls": {"judged": 0, "worse": 0},
        "cards": [
            {"rule": "spawn-tools-list", "agent": "custom", "landed": 4, "measured": 0, "half_or_more": 0,
             "too_little_data": 4, "guard_holds": 0, "guard_worse": 0},
            {"rule": "spawn-tools-list", "agent": "general-purpose", "landed": 1, "measured": 0, "half_or_more": 0,
             "too_little_data": 1, "guard_holds": 0, "guard_worse": 0},
        ],
        "levers": [],
    }
    assert tuning.validate(doc) == []
    lines = [line for line in tuning.summary_text(doc).splitlines() if "ixes that landed" in line or "Corrections and rework" in line]
    assert lines == [
        "Fixes that landed: 5. None has had enough sessions on both sides to measure yet.",
        "Corrections and rework after a fix: 0 held, 0 got worse, 5 had too few sessions to tell.",
    ]


def test_build_counts_fixes_once_and_names_them_by_rule_and_agent_class(world, points):
    secret, path = "my-secret-agent", "C:\\Users\\me\\proj"
    points += [
        _point(SEP_10, [f"agents.{secret}.tools", f"agents.{secret}.model"]),
        _point(SEP_12, ["effective.model"]),
    ]
    rows = [
        # The same card in the every-project view and in a project's: one fix.
        _row("k1", agent=secret, landed=SEP_10),
        _row("k1", agent=secret, landed=SEP_10, project="proj-alpha", folders=[path]),
    ]
    predictions = [_prediction(SEP_10.replace(day=9), "model", secret), _prediction(SEP_10.replace(day=9), "model", None)]
    doc = _build(world, outcomes=_inputs(rows, predictions, folders=(path, "proj-alpha")))
    block = doc["outcomes"]
    assert block["landed"] == 3
    assert [(r["rule"], r["agent"], r["landed"]) for r in block["cards"]] == [("spawn-tools-list", "custom", 1)]
    # The prediction on the agent is the custom agent's; the one on the main model matches the other point.
    assert {(r["lever"], r["agent"]): r["landed"] for r in block["levers"]} == {("model", "custom"): 1, ("model", "none"): 1}
    assert_privacy_deep(doc)
    text = tuning.dumps(doc)
    for planted in (secret, path, "proj-alpha", "pred-", "k1"):
        assert planted not in text, planted
    assert tuning.validate(json.loads(text)) == []


def test_two_views_of_one_fix_are_one_and_the_every_project_row_is_the_one_read(world, points, monkeypatch):
    points.append(_point(SEP_10, ["agents.general-purpose.tools"]))
    seen = []

    def spy(point, agent, key, prediction, *args, **kw):
        seen.append((agent, key, prediction.saving_usd, prediction.basis))
        return _reading()

    monkeypatch.setattr(outcomes_mod, "read_fix", spy)
    rows = [
        _row("k1", landed=SEP_10, project="proj-alpha", folders=["proj-alpha"], saving=1.0, priced_on=10.0),
        _row("k1", landed=SEP_10, saving=2.0, priced_on=20.0),
    ]
    doc = _build(world, outcomes=_inputs(rows))
    assert doc["outcomes"]["landed"] == 1
    assert seen == [("general-purpose", "tools", 2.0, 20.0)]


def test_a_fix_before_the_window_or_in_a_project_the_export_does_not_cover_is_not_counted(world, points):
    points += [
        _point(JUL_01, ["agents.old.tools"]),
        _point(SEP_12, ["agents.mine.tools", "agents.other.tools"]),
        _point(SEP_10, ["effective.model"], project="elsewhere"),
    ]
    rows = [
        _row("old", agent="old", landed=JUL_01),
        _row("star", agent="mine", landed=SEP_12),
        _row("here", agent="other", landed=SEP_12, project="proj-alpha", folders=["proj-alpha"]),
        _row("not-mine", agent="other", landed=SEP_12, project="proj-gamma", folders=["proj-gamma"]),
    ]
    predictions = [_prediction(SEP_10.replace(day=9), "model")]
    # Every project: the every-project card and the project card the export covers, and the profile
    # change in another project. The card from before the window and the one from a project it does not cover are not.
    doc = _build(world, outcomes=_inputs(rows, predictions, every_project=True))
    assert doc["outcomes"]["landed"] == 3
    # One project: only its own cards, and a profile change that applies there (this one is another project's).
    doc = _build(world, outcomes=_inputs(rows, predictions, folders=("proj-alpha",), every_project=False))
    assert doc["outcomes"]["landed"] == 1
    assert_privacy_deep(doc)


def test_a_measured_loss_is_lost_usd_and_a_saving_saved_usd(world, points, monkeypatch):
    points.append(_point(SEP_10, ["agents.alpha.tools", "agents.beta.tools", "agents.gamma.tools"]))
    readings = {
        "alpha": _reading("half_or_more", predicted=10.0, measured=6.0, guard="holds", control="holds"),
        "beta": _reading("under_half", predicted=8.0, measured=-4.0, guard="worse", control="worse"),
        "gamma": _reading("too_little_data", guard="unclear"),
    }
    monkeypatch.setattr(outcomes_mod, "read_fix", lambda point, agent, *args, **kw: readings[agent])
    rows = [_row(f"k-{name}", agent=name, landed=SEP_10) for name in readings]
    doc = _build(world, outcomes=_inputs(rows))
    (row,) = doc["outcomes"]["cards"]
    assert row == {
        "rule": "spawn-tools-list", "agent": "custom", "landed": 3, "measured": 2, "half_or_more": 1,
        "too_little_data": 1, "guard_holds": 1, "guard_worse": 1,
        "predicted_usd": 18.0, "saved_usd": 6.0, "lost_usd": 4.0,
    }
    assert doc["outcomes"]["controls"] == {"judged": 2, "worse": 1}
    assert tuning.validate(doc) == []
    # Nothing measured: the three amounts are not there.
    monkeypatch.setattr(outcomes_mod, "read_fix", lambda *args, **kw: _reading())
    (bare,) = _build(world, outcomes=_inputs(rows))["outcomes"]["cards"]
    assert not {"predicted_usd", "saved_usd", "lost_usd"} & set(bare)


def test_a_profile_change_is_a_lever_row_and_an_unknown_key_is_other(world, points, monkeypatch):
    points += [_point(SEP_10, ["effective.promptCacheTtl", "effective.key"])]
    seen = []

    def spy(point, agent, key, prediction, *args, **kw):
        seen.append((agent, key, prediction.saving_usd, prediction.basis))
        return _reading("half_or_more", predicted=5.0, measured=5.0)

    monkeypatch.setattr(outcomes_mod, "read_fix", spy)
    before = SEP_10.replace(day=8)
    predictions = [
        _prediction(before, "promptCacheTtl", predicted=4.0, priced_on=40.0),
        _prediction(before, "key", predicted=3.0, priced_on=None),
        _prediction(before, "model", source="other"),
    ]
    doc = _build(world, outcomes=_inputs(predictions=predictions))
    assert doc["outcomes"]["cards"] == []
    assert {(r["lever"], r["agent"]) for r in doc["outcomes"]["levers"]} == {("promptCacheTtl", "none"), ("other", "none")}
    assert sorted(seen, key=repr) == sorted([(None, "promptCacheTtl", 4.0, 40.0), (None, "key", 3.0, None)], key=repr)
    assert tuning.validate(doc) == []


def test_a_prediction_made_after_the_change_matches_no_point(world, points):
    points.append(_point(SEP_10, ["effective.model"]))
    doc = _build(world, outcomes=_inputs(predictions=[_prediction(SEP_12, "model")]))
    assert doc["outcomes"]["landed"] == 0


def test_a_fix_that_landed_too_soon_after_the_window_starts_is_counted_apart(world, points):
    points.append(_point(AUG_25, ["agents.general-purpose.tools"]))
    doc = _build(world, outcomes=_inputs([_row("k1", landed=AUG_25)]))
    assert doc["outcomes"]["short_export"] == 1 and doc["outcomes"]["landed"] == 1
    # A fix a fortnight into the window has its before side in it.
    points[:] = [_point(SEP_10, ["agents.general-purpose.tools"])]
    assert _build(world, outcomes=_inputs([_row("k1", landed=SEP_10)]))["outcomes"]["short_export"] == 0


def test_the_rows_are_cut_to_their_limits_and_landed_still_counts_them_all(world, points, monkeypatch):
    rules = tuning.CARD_RULES[: tuning.OUTCOME_CARD_ROWS + 1]
    points.append(_point(SEP_10, ["agents.general-purpose.tools"]))
    monkeypatch.setattr(outcomes_mod, "read_fix", lambda *args, **kw: _reading())
    rows = [_row(f"k-{rule}", rule=rule, landed=SEP_10) for rule in rules]
    doc = _build(world, outcomes=_inputs(rows))
    assert doc["outcomes"]["landed"] == tuning.OUTCOME_CARD_ROWS + 1
    assert len(doc["outcomes"]["cards"]) == tuning.OUTCOME_CARD_ROWS
    assert tuning.validate(doc) == []


def test_what_a_fix_was_priced_on_does_not_reach_the_document(world, points):
    points.append(_point(SEP_10, ["agents.general-purpose.tools", "effective.model"]))
    rows = [_row("k1", priced_on=424242.5, saving=313.25)]
    predictions = [_prediction(SEP_10.replace(day=9), "model", priced_on=737373.5, predicted=191.5)]
    text = tuning.dumps(_build(world, outcomes=_inputs(rows, predictions)))
    assert "priced_on" not in text
    for figure in ("424242", "737373", "313.25", "191.5"):
        assert figure not in text


# -- when there is nothing to read ---------------------------------------------------------------


def test_the_block_is_absent_without_a_store_or_with_nothing_landed(world, monkeypatch):
    def refuse(*args, **kw):
        raise AssertionError("the corpus pass must not run when nothing landed")

    monkeypatch.setattr(change_points_mod, "change_points", refuse)
    monkeypatch.setattr(impact_mod, "session_facts", refuse)
    assert "outcomes" not in _build(world)
    assert "outcomes" not in _build(world, outcomes=None)
    shown_only = _row("k1", landed=None)
    unlanded_and_no_changes = _row("k2", changes=[])
    assert "outcomes" not in _build(world, outcomes=_inputs([shown_only, unlanded_and_no_changes]))
    # A prediction from somewhere other than the what-if estimate is not one to read either.
    other = _prediction(SEP_10, "model", source="other")
    assert "outcomes" not in _build(world, outcomes=_inputs(predictions=[other]))


def test_landed_cards_with_no_change_point_make_a_block_that_says_none_landed(world, points):
    doc = _build(world, outcomes=_inputs([_row("k1")]))
    assert doc["outcomes"] == {
        "landed": 0, "short_export": 0, "controls": {"judged": 0, "worse": 0}, "cards": [], "levers": [],
    }
    assert tuning.summary_text(doc).splitlines()[-1] == "Fixes that landed: none in this window."


def test_the_block_comes_after_figures_and_the_pass_runs_once_for_all_the_fixes(world, monkeypatch):
    calls = []
    monkeypatch.setattr(
        change_points_mod, "change_points",
        lambda config_dir, corpus=None, **kw: calls.append("points") or [_point(SEP_10, ["agents.general-purpose.tools"])],
    )
    monkeypatch.setattr(impact_mod, "session_facts", lambda corpus, pricing: calls.append("sessions") or [])
    doc = _build(world, outcomes=_inputs([_row("k1"), _row("k2", agent="general-purpose")]))
    names = list(doc)
    assert names.index("figures") < names.index("outcomes")
    assert calls == ["points", "sessions"]


# -- the spec and the older files ----------------------------------------------------------------


def test_a_file_without_an_outcomes_block_still_passes_and_says_nothing_of_it():
    doc = _sample()
    assert "outcomes" in doc
    del doc["outcomes"]
    assert tuning.validate(doc) == []
    assert "Fixes that landed" not in tuning.summary_text(doc)
    # A 0.15.0 file has neither the coverage block nor this one nor the cards.
    del doc["coverage"], doc["cards"]
    assert tuning.validate(doc) == []
    assert "Fixes that landed" not in tuning.summary_text(doc)
    assert any(line.startswith("Fixes that landed") for line in tuning.summary_text(_sample()).splitlines())


def test_an_outcomes_block_fails_the_checks_like_any_block():
    def card(**kw):
        return {**_sample()["outcomes"]["cards"][0], **kw}

    nine = [card()] * (tuning.OUTCOME_CARD_ROWS + 1)
    missing = {k: v for k, v in _sample()["outcomes"].items() if k != "landed"}
    cases = [
        ("outcomes.cards.0.rule", "my-rule", "outcomes.cards[0].rule: must be one of the words", "my-rule"),
        ("outcomes.levers.0.lever", "myLever", "outcomes.levers[0].lever: must be one of the words", "myLever"),
        ("outcomes.cards.0.saved_usd", -1.5, "outcomes.cards[0].saved_usd: must be 0 or more", ""),
        ("outcomes.cards", nine, "outcomes.cards: has more items than the spec allows", ""),
        ("outcomes", missing, "outcomes.landed: is missing", ""),
        ("outcomes.short_export", None, "outcomes.short_export: must be a whole number", ""),
        ("outcomes.cards.0.agent", "my-secret-agent", "outcomes.cards[0].agent: must be one of the words", "my-secret-agent"),
        ("outcomes.controls.judged", 1.5, "outcomes.controls.judged: must be a whole number", ""),
    ]
    for path, value, check, leak in cases:
        problems = tuning.validate(_put(_sample(), path, value))
        assert any(check in problem for problem in problems), (path, problems)
        if leak:
            assert leak not in " ".join(problems), path
    with pytest.raises(tuning.TuningError):
        tuning.dumps(_put(_sample(), "outcomes.cards.0.rule", "my-rule"))


def test_the_outcome_levers_are_the_settings_a_profile_can_change_and_other():
    from claudeglass.profiles import schema

    allowed = set(schema.SETTINGS_ALLOWLIST) | set(schema.AGENT_ALLOWLIST)
    assert set(tuning.OUTCOME_LEVERS[:-1]) == {key.replace(".", "_") for key in allowed}
    assert "experimental_cacheTtl" in tuning.OUTCOME_LEVERS and not any("." in word for word in tuning.OUTCOME_LEVERS)
    assert tuning.OUTCOME_LEVERS[-1] == "other"
    assert list(tuning.OUTCOME_LEVERS[:-1]) == sorted(tuning.OUTCOME_LEVERS[:-1])


# -- the plain-words summary ----------------------------------------------------------------------


def _block(**kw):
    base = {"landed": 0, "short_export": 0, "controls": {"judged": 0, "worse": 0}, "cards": [], "levers": []}
    return {**base, **kw}


def _row_of(**kw):
    return {"rule": "spawn-tools-list", "agent": "custom", "landed": 0, "measured": 0, "half_or_more": 0,
            "too_little_data": 0, "guard_holds": 0, "guard_worse": 0, **kw}


def test_the_summary_says_what_landed_and_what_it_saved():
    assert tuning._outcomes_lines(_block()) == ["Fixes that landed: none in this window."]
    measured = _block(
        landed=9, short_export=0, controls={"judged": 6, "worse": 2},
        cards=[_row_of(landed=7, measured=4, half_or_more=3, too_little_data=2, guard_holds=4, guard_worse=1,
                       predicted_usd=20.0, saved_usd=12.5, lost_usd=2.25)],
        levers=[{"lever": "model", "agent": "none", "landed": 2, "measured": 1, "half_or_more": 0,
                 "too_little_data": 1, "guard_holds": 1, "guard_worse": 0, "predicted_usd": 3.0, "saved_usd": 0.5,
                 "lost_usd": 0.0}],
    )
    lines = tuning._outcomes_lines(measured)
    assert lines == [
        "Fixes that landed: 9. Measured so far: 5, of which 3 saved at least half of what the card or profile estimated.",
        "Over the same work they saved $10.75, against $23.00 estimated.",
        "Corrections and rework after a fix: 5 held, 1 got worse, 3 had too few sessions to tell.",
        "The same check at an earlier date read worse 2 of 6 times.",
    ]
    for line in lines:
        _plain(line)
        assert len(line.split()) <= 25


def test_the_summary_says_when_the_fixes_cost_more_and_names_the_fixes_that_landed_too_soon():
    losing = _block(
        landed=2, short_export=1,
        cards=[_row_of(landed=2, measured=1, half_or_more=0, too_little_data=1, guard_holds=1,
                       predicted_usd=4.0, saved_usd=0.0, lost_usd=1.5)],
    )
    lines = tuning._outcomes_lines(losing)
    assert lines == [
        "Fixes that landed: 2. Measured so far: 1, of which 0 saved at least half of what the card or profile estimated.",
        "1 fix landed too soon after this window's start to measure. Export more days to read it.",
        "Over the same work they cost $1.50 more, against $4.00 estimated.",
        "Corrections and rework after a fix: 1 held, 0 got worse, 1 had too few sessions to tell.",
    ]
    several = _block(landed=3, short_export=3, cards=[_row_of(landed=3, too_little_data=3)])
    assert tuning._outcomes_lines(several)[1] == (
        "3 fixes landed too soon after this window's start to measure. Export more days to read them."
    )
    for line in lines + tuning._outcomes_lines(several):
        _plain(line)
        assert len(line.split()) <= 25


def test_the_summary_of_the_sample_keeps_to_the_copy_rules():
    for line in tuning._outcomes_lines(_sample()["outcomes"]):
        _plain(line)

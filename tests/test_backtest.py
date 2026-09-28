"""EST-P4: matching a logged prediction to the change point it turned
into, then judging it against the sessions before and after
(``backtest``)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from claudeglass import backtest, impact
from claudeglass.change_points import ChangePoint
from claudeglass.corpus import load_corpus
from claudeglass.pricing import load_pricing, price_turn
from claudeglass.profiles import apply as apply_mod
from claudeglass.profiles.schema import load_dict
from claudeglass.service.store import Store
from claudeglass.units import Units

from helpers import turn_line, write_jsonl

UNITS = Units(billing_mode="api", currency="USD")
CHANGE = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)


def _point(ts, keys, source="apply"):
    return ChangePoint(ts, source, "x", keys=keys)


def _prediction(*, ts, measure_key, agent=None, predicted_usd, fidelity="ceiling"):
    return {
        "id": "pred-1",
        "ts": ts,
        "source": "whatif",
        "measure_key": measure_key,
        "agent": agent,
        "predicted_usd": predicted_usd,
        "predicted_pct": None,
        "fidelity": fidelity,
    }


# -- matching ----------------------------------------------------------------


def test_point_settings_keys_parses_plain_agent_and_prefixed_labels():
    assert backtest._point_settings_keys(_point(CHANGE, ["model"])) == [(None, "model")]
    assert backtest._point_settings_keys(_point(CHANGE, ["Explore: model"])) == [("Explore", "model")]
    assert backtest._point_settings_keys(_point(CHANGE, ["effective.promptCacheTtl"])) == [(None, "promptCacheTtl")]
    assert backtest._point_settings_keys(_point(CHANGE, ["agents.Explore.model"])) == [("Explore", "model")]


def test_match_point_picks_the_nearest_later_point_with_the_same_key():
    pred = _prediction(ts="2026-09-19T12:00:00Z", measure_key="model", predicted_usd=1.0)
    early = _point(CHANGE, ["model"])
    later = _point(CHANGE + timedelta(days=5), ["model"])
    unrelated = _point(CHANGE - timedelta(hours=1), ["promptCacheTtl"])
    assert backtest._match_point(pred, [unrelated, later, early]) is early


def test_match_point_never_matches_a_revert():
    pred = _prediction(ts="2026-09-19T12:00:00Z", measure_key="model", predicted_usd=1.0)
    revert_only = _point(CHANGE, ["model"], source="revert")
    assert backtest._match_point(pred, [revert_only]) is None


def test_match_point_ignores_a_point_before_the_prediction():
    pred = _prediction(ts=CHANGE.strftime("%Y-%m-%dT%H:%M:%SZ"), measure_key="model", predicted_usd=1.0)
    too_early = _point(CHANGE - timedelta(days=1), ["model"])
    assert backtest._match_point(pred, [too_early]) is None


def test_match_point_requires_the_same_agent():
    pred = _prediction(ts="2026-09-19T12:00:00Z", measure_key="model", agent="Explore", predicted_usd=1.0)
    main_only = _point(CHANGE, ["model"])
    assert backtest._match_point(pred, [main_only]) is None
    agent_point = _point(CHANGE, ["Explore: model"])
    assert backtest._match_point(pred, [agent_point]) is agent_point


def test_neighbours_skip_points_made_together():
    a = _point(CHANGE - timedelta(days=3), ["model"])
    together = _point(CHANGE - timedelta(minutes=1), ["model"])
    b = _point(CHANGE, ["model"])
    c = _point(CHANGE + timedelta(days=3), ["model"])
    previous, following = impact.neighbours([a, together, b, c], b)
    assert previous is a
    assert following is c


def test_a_change_in_another_project_doesnt_bound_this_one():
    a = _point(CHANGE - timedelta(days=3), ["model"])
    other = _point(CHANGE - timedelta(days=1), ["model"])
    other.project = "slug:other"
    b = _point(CHANGE, ["model"])
    b.project = "slug:mine"
    everywhere = _point(CHANGE + timedelta(days=2), ["model"])
    assert impact.neighbours([a, other, b, everywhere], b) == (a, everywhere)


# -- verdicts ------------------------------------------------------------


def test_verdict_as_estimated_when_magnitudes_are_close():
    assert backtest._verdict(1.0, 1.2, "lower") == "as_estimated"
    assert backtest._verdict(-1.0, -0.6, "higher") == "as_estimated"


def test_verdict_smaller_when_measured_is_much_less_or_not_significant():
    assert backtest._verdict(1.0, 0.1, "lower") == "smaller"
    assert backtest._verdict(1.0, 0.9, "no_clear_change") == "smaller"


def test_verdict_larger_when_measured_dwarfs_the_prediction():
    assert backtest._verdict(1.0, 4.0, "lower") == "larger"
    assert backtest._verdict(0.0, 0.5, "lower") == "larger"


def test_verdict_opposite_when_signs_disagree_and_it_is_significant():
    assert backtest._verdict(1.0, -1.0, "higher") == "opposite"
    assert backtest._verdict(-1.0, 1.0, "lower") == "opposite"


# -- present: display-ready rows ----------------------------------------------


def test_present_adds_money_and_verdict_text_to_a_judged_row():
    units = Units(billing_mode="api", currency="USD")
    row = {"id": "pred-0", "predicted_usd": 5.0, "measured_usd": 2.0, "verdict": "smaller"}
    [out] = backtest.present([row], units)
    assert out["predicted_text"] == "Saves 5.00 USD"
    assert out["measured_text"] == "Saves 2.00 USD"
    assert out["verdict_text"] == "Measured effect was smaller than estimated."


def test_present_marks_an_unjudged_row_not_judged_yet():
    units = Units(billing_mode="api", currency="USD")
    row = {"id": "pred-0", "predicted_usd": 5.0, "measured_usd": None, "verdict": None}
    [out] = backtest.present([row], units)
    assert out["measured_text"] == ""
    assert out["verdict_text"] == "Not judged yet."


def test_present_every_closed_verdict_has_its_own_sentence():
    units = Units(billing_mode="api", currency="USD")
    rows = [{"id": f"pred-{v}", "predicted_usd": 1.0, "measured_usd": 1.0, "verdict": v} for v in backtest.VERDICTS]
    texts = {out["verdict"]: out["verdict_text"] for out in backtest.present(rows, units)}
    assert len(set(texts.values())) == len(backtest.VERDICTS)
    assert all(text and text[0].isupper() for text in texts.values())


# -- EST-P6: calibration_multipliers -----------------------------------------


def _judged_store(rows: list[tuple[str | None, str, float, float | None]]) -> Store:
    """A store with one judged prediction per ``(agent, measure_key,
    predicted_usd, measured_usd)`` row (``measured_usd=None`` for a
    too_little_data verdict)."""
    store = Store(":memory:")
    store.open()
    for i, (agent, key, predicted, measured) in enumerate(rows):
        pid = f"pred-{i}"
        store.upsert_prediction(
            prediction_id=pid, ts="2026-09-20T09:00:00Z", source="whatif", measure_key=key,
            agent=agent, predicted_usd=predicted, predicted_pct=None, fidelity="ceiling",
        )
        verdict = "too_little_data" if measured is None else "as_estimated"
        store.judge_prediction(pid, change_ts="2026-09-21T09:00:00Z", verdict=verdict, measured_usd=measured, measured_pct=None)
    return store


def test_calibration_needs_at_least_three_judged_points_per_key():
    store = _judged_store([(None, "model", 1.0, 2.0), (None, "model", 1.0, 2.0)])
    assert backtest.calibration_multipliers(store) == {}


def test_calibration_averages_the_measured_over_predicted_ratio():
    store = _judged_store([(None, "model", 1.0, 2.0), (None, "model", 2.0, 4.0), (None, "model", 1.0, 2.0)])
    multipliers = backtest.calibration_multipliers(store)
    assert multipliers[(None, "model")] == 2.0


def test_calibration_is_scoped_per_agent_and_key():
    store = _judged_store(
        [
            (None, "model", 1.0, 1.0), (None, "model", 1.0, 1.0), (None, "model", 1.0, 1.0),
            ("Explore", "model", 1.0, 3.0), ("Explore", "model", 1.0, 3.0), ("Explore", "model", 1.0, 3.0),
        ]
    )
    multipliers = backtest.calibration_multipliers(store)
    assert multipliers[(None, "model")] == 1.0
    assert multipliers[("Explore", "model")] == 3.0


def test_calibration_skips_too_little_data_and_zero_predicted_rows():
    store = _judged_store(
        [(None, "model", 1.0, None), (None, "model", 0.0, 5.0), (None, "model", 1.0, 2.0)]
    )
    assert backtest.calibration_multipliers(store) == {}


# -- judge_predictions: end to end -------------------------------------------


def _apply(tmp_path: Path, settings: dict):
    claude_root = tmp_path / ".claude"
    config_dir = claude_root / "claudeglass"
    claude_root.mkdir(exist_ok=True)
    profile = load_dict({"id": "one-off", "settings": settings})
    plan = apply_mod.plan_apply(profile, scope="user", project_path=None, config_dir=config_dir, claude_root=claude_root)
    return config_dir, apply_mod.execute(plan, config_dir=config_dir)


def _session_file(
    project_dir: Path, session_id: str, ts: datetime, *, input_tokens: int, model: str = "claude-sonnet-5"
) -> None:
    stamp = ts.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    lines = [
        turn_line(timestamp=stamp, input_tokens=input_tokens, output_tokens=50, model=model),
        turn_line(timestamp=stamp, input_tokens=input_tokens, output_tokens=50, model=model),
    ]
    write_jsonl(project_dir / f"{session_id}.jsonl", lines)


def test_judge_predictions_matches_windows_and_persists_a_verdict(tmp_path):
    t0 = datetime.now(timezone.utc)
    config_dir, result = _apply(tmp_path, {"effortLevel": "medium"})
    point_ts = backtest.change_points_mod._parse_backup_ts(result.ts)
    assert point_ts is not None

    project_dir = tmp_path / "projects" / "proj"
    project_dir.mkdir(parents=True)
    # effortLevel has no exact counterfactual, so this is judged on the
    # before/after measure. Identical token counts within each side -> zero variance -> the
    # ratio test is maximally significant, so the verdict never comes
    # down to noise in this test.
    for i, hours_before in enumerate((2, 1.5, 1), start=1):
        _session_file(project_dir, f"before-{i}", point_ts - timedelta(hours=hours_before), input_tokens=100_000)
    for i, hours_after in enumerate((1, 2, 3), start=1):
        _session_file(project_dir, f"after-{i}", point_ts + timedelta(hours=hours_after), input_tokens=10_000)

    corpus = load_corpus([project_dir])
    pricing = load_pricing()

    store = Store(":memory:")
    store.open()
    store.upsert_prediction(
        prediction_id="pred-1",
        ts=(t0 - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        source="whatif",
        measure_key="effortLevel",
        agent=None,
        predicted_usd=0.01,
        predicted_pct=None,
        fidelity="ceiling",
    )

    judged = backtest.judge_predictions(
        store, corpus, pricing, UNITS, config_dir, now=point_ts + timedelta(hours=10)
    )

    assert judged == 1
    [row] = store.predictions(judged=True)
    assert row["id"] == "pred-1"
    assert row["change_ts"] == point_ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    # Fewer input tokens after the change -> cheaper -> a real saving;
    # the tiny $0.01 prediction reads as "larger" than what showed up.
    assert row["measured_usd"] > 0
    assert row["verdict"] == "larger"


def test_a_model_prediction_is_judged_on_the_same_sessions_repriced(tmp_path):
    """The model the sessions before ran on (the apply set it from
    unset) is priced on the sessions after, so their fewer input tokens,
    which the model change didn't cause, don't count as its saving."""
    t0 = datetime.now(timezone.utc)
    config_dir, result = _apply(tmp_path, {"model": "sonnet"})
    point_ts = backtest.change_points_mod._parse_backup_ts(result.ts)
    project_dir = tmp_path / "projects" / "proj"
    project_dir.mkdir(parents=True)
    for i, hours_before in enumerate((2, 1.5, 1), start=1):
        _session_file(
            project_dir, f"before-{i}", point_ts - timedelta(hours=hours_before), input_tokens=100_000, model="claude-opus-5-5"
        )
    for i, hours_after in enumerate((1, 2, 3), start=1):
        _session_file(project_dir, f"after-{i}", point_ts + timedelta(hours=hours_after), input_tokens=10_000)
    corpus = load_corpus([project_dir])
    pricing = load_pricing()
    store = Store(":memory:")
    store.open()
    store.upsert_prediction(
        prediction_id="pred-1",
        ts=(t0 - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        source="whatif",
        measure_key="model",
        agent=None,
        predicted_usd=0.05,
        predicted_pct=None,
        fidelity="ceiling",
    )

    backtest.judge_predictions(store, corpus, pricing, UNITS, config_dir, now=point_ts + timedelta(hours=10))

    [row] = store.predictions(judged=True)
    opus, sonnet = pricing.resolve_model("claude-opus-5-5"), pricing.resolve_model("claude-sonnet-5")
    after_turns = [
        turn for bundle in corpus.sessions if bundle.session_id.startswith("after") for turn in bundle.top.turns if turn.turn_index > 0
    ]
    expected = sum(price_turn(t, opus).total - price_turn(t, sonnet).total for t in after_turns)
    assert row["measured_usd"] == pytest.approx(expected, abs=1e-6)
    assert row["measured_pct"] < 0


def test_judge_predictions_leaves_an_unmatched_prediction_alone(tmp_path):
    config_dir, _result = _apply(tmp_path, {"promptCacheTtl": "1h"})
    project_dir = tmp_path / "projects" / "proj"
    project_dir.mkdir(parents=True)
    corpus = load_corpus([project_dir])
    pricing = load_pricing()

    store = Store(":memory:")
    store.open()
    store.upsert_prediction(
        prediction_id="pred-1",
        ts="2026-09-19T12:00:00Z",
        source="whatif",
        measure_key="model",  # no matching change point applied this key
        agent=None,
        predicted_usd=1.0,
        predicted_pct=None,
        fidelity="ceiling",
    )

    judged = backtest.judge_predictions(store, corpus, pricing, UNITS, config_dir)

    assert judged == 0
    assert store.predictions(judged=True) == []
    assert len(store.predictions(judged=False)) == 1


def test_judge_predictions_waits_while_the_after_window_is_still_open(tmp_path):
    t0 = datetime.now(timezone.utc)
    config_dir, result = _apply(tmp_path, {"model": "sonnet"})
    point_ts = backtest.change_points_mod._parse_backup_ts(result.ts)

    project_dir = tmp_path / "projects" / "proj"
    project_dir.mkdir(parents=True)
    for i, hours_before in enumerate((2, 1.5, 1), start=1):
        _session_file(project_dir, f"before-{i}", point_ts - timedelta(hours=hours_before), input_tokens=100_000)
    # Only one session after the change so far -- below MIN_SESSIONS.
    _session_file(project_dir, "after-1", point_ts + timedelta(hours=1), input_tokens=10_000)

    corpus = load_corpus([project_dir])
    pricing = load_pricing()

    store = Store(":memory:")
    store.open()
    store.upsert_prediction(
        prediction_id="pred-1",
        ts=(t0 - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        source="whatif",
        measure_key="model",
        agent=None,
        predicted_usd=1.0,
        predicted_pct=None,
        fidelity="ceiling",
    )

    judged = backtest.judge_predictions(
        store, corpus, pricing, UNITS, config_dir, now=point_ts + timedelta(hours=2)
    )

    assert judged == 0
    assert len(store.predictions(judged=False)) == 1


def _snapshot(config_dir: Path, ts: datetime, effective: dict) -> None:
    import json

    stamp = ts.strftime("%Y%m%dT%H%M%SZ")
    folder = config_dir / "snapshots"
    folder.mkdir(parents=True, exist_ok=True)
    doc = {"ts": stamp, "schema_version": 2, "project_slug": "slug:abc", "effective": effective}
    (folder / f"{stamp}.json").write_text(json.dumps(doc), encoding="utf-8")


@pytest.mark.parametrize(
    ("key", "value", "before_hours", "after_hours", "judged_as"),
    [
        # Only one session before an effort change (which nothing can
        # reprice from the sessions after it alone), and three after it
        # before the *next* change closes the window: never enough to judge.
        ("effortLevel", "low", (2,), (1, 2, 3), "too_little_data"),
        # One session after a model change before the next change: too
        # few between them for the next change to bound this one, so the
        # window is still open and the prediction waits.
        ("model", "sonnet", (2, 1.5, 1), (1,), None),
    ],
)
def test_judge_predictions_closes_out_too_little_data_once_a_later_point_bounds_it(
    tmp_path, key, value, before_hours, after_hours, judged_as
):
    t0 = datetime.now(timezone.utc)
    config_dir, result = _apply(tmp_path, {key: value})
    point_ts = backtest.change_points_mod._parse_backup_ts(result.ts)

    project_dir = tmp_path / "projects" / "proj"
    project_dir.mkdir(parents=True)
    for i, hours in enumerate(before_hours, start=1):
        _session_file(project_dir, f"before-{i}", point_ts - timedelta(hours=hours), input_tokens=100_000)
    for i, hours in enumerate(after_hours, start=1):
        _session_file(project_dir, f"after-{i}", point_ts + timedelta(hours=hours), input_tokens=10_000)

    corpus = load_corpus([project_dir])
    pricing = load_pricing()

    store = Store(":memory:")
    store.open()
    store.upsert_prediction(
        prediction_id="pred-1",
        ts=(t0 - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        source="whatif",
        measure_key=key,
        agent=None,
        predicted_usd=1.0,
        predicted_pct=None,
        fidelity="ceiling",
    )

    # A later, unrelated settings change (two snapshots, well clear of
    # the apply's own timestamp) bounds the apply's after-window.
    _snapshot(config_dir, point_ts + timedelta(hours=5), {"customKey": "a"})
    _snapshot(config_dir, point_ts + timedelta(hours=6), {"customKey": "b"})

    judged = backtest.judge_predictions(
        store, corpus, pricing, UNITS, config_dir, now=point_ts + timedelta(days=1)
    )

    if judged_as is None:
        assert judged == 0
        assert len(store.predictions(judged=False)) == 1
        return
    assert judged == 1
    [row] = store.predictions(judged=True)
    assert row["verdict"] == judged_as
    assert row["measured_usd"] is None


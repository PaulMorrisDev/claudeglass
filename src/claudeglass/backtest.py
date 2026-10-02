"""EST-P4: did a prediction come true? Each row ``route_whatif`` logged
with ``"log": true`` (:mod:`config`'s ``prediction-log.jsonl``, ingested
into the store's ``predictions`` table by
``service.watcher.FileWatcher._scan_predictions``) is matched to the
real change point (:mod:`change_points`) it turned into, then judged
against the sessions before and after that change -- the same
before/after windowing and ratio test :mod:`impact` already uses for
EST-P3, so a back-test verdict and an impact row are never computed two
different ways.

Method, in full (see also ``docs/backtest.md``):

1. **Match.** A prediction names a raw settings key (``measure_key``,
   e.g. ``"model"``, ``"promptCacheTtl"``) and, for an agent-scoped
   change, the ``agent``. It is matched to the *nearest* change point at
   or after the prediction's own timestamp whose own ``keys`` name that
   same ``(agent, key)`` pair (see :func:`_point_settings_keys`, which
   parses a change point's key labels the same way
   ``impact.measures_for`` does). A ``"revert"`` point is never a match
   -- undoing a change isn't making the one that was predicted. A
   prediction with no matching change point yet (you looked, but never
   applied it) is left alone; it eventually expires unjudged
   (``Store.prune_predictions``'s 90-day unseen window).
2. **Window.** Once matched, "before" and "after" are exactly
   :func:`impact.compare`'s own windowing: up to :data:`impact.LOOKBACK_DAYS`
   before the change (or back to the previous change point in the same
   project's chain, if that's sooner), and from the change until the
   *next* change point after it (or now, if there isn't one yet). A
   change point with fewer than :data:`impact.MIN_SESSIONS` sessions
   between it and this one doesn't bound either (:func:`impact.neighbours`).
   A change to every project is bounded in each project by that project's
   own changes (:func:`impact.bounds`), the same call
   :func:`impact.impact` makes, so the two never disagree; its window
   stays open until a later change to every project bounds it
   (``following``).
3. **Measure.** The dollar quantity a prediction estimated is always
   either the whole session's cost (a main-session-level setting) or one
   agent's cost per spawn (an agent-scoped setting) -- the same two
   measures ``whatif.estimate`` itself reprices from
   (``model_swap_by_agent_type``, ``ttl_by_agent_type``, etc. all total
   to one or the other). Its before/after estimate is
   ``impact._measure_row``'s own ratio-of-sums, stratum-reweighted,
   Holm-tested row -- unchanged from EST-P3, just called for one measure
   instead of a change point's whole table. When :mod:`counterfactual`
   can undo the predicted setting on the sessions after the change
   itself (repriced, simulated or approximate, not its from-before
   fallback), that figure is the measured saving instead: it prices the
   same sessions both ways, so other differences between the two sides
   don't count.
4. **Measured total.** ``impact._measure_row`` gives a *rate*
   (dollars per session, or per spawn) before and after. Multiplying the
   rate's drop (or rise) by how many sessions/spawns actually happened
   after the change turns it into a dollar total comparable to
   ``predicted_usd`` -- ``(before_rate - after_rate) * after_n``. It is
   scaled to the after-window this change actually got, which usually
   isn't the same length as the report window open when the prediction
   was made, so read the two totals' *ratio*, not their exact dollar
   difference, as what the verdict is built from.
5. **Verdict**, one of the closed set ``as_estimated``, ``smaller``,
   ``larger``, ``opposite``, ``too_little_data`` (see :func:`_verdict`).
   A prediction whose window is still open (no later change point yet,
   and too little data so far to tell) is left unjudged rather than
   forced to a premature verdict -- it is judged once the window closes
   (a later change point arrives) or there was enough data all along.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from . import change_points as change_points_mod
from . import counterfactual, impact
from .change_points import ChangePoint
from .pricing import Pricing
from .units import Units

#: Closed verdict vocabulary persisted to ``predictions.verdict``.
VERDICTS = ("as_estimated", "smaller", "larger", "opposite", "too_little_data")

#: A predicted saving smaller than this (either way) is treated as "predicted
#: no real effect" -- any measured effect at all is then "larger" than predicted,
#: rather than trying to divide by a near-zero denominator.
_NEGLIGIBLE_USD = 0.01

#: The measured/predicted magnitude has to fall in this band to read as
#: "as estimated" rather than "smaller"/"larger".
_AS_ESTIMATED_LOW = 0.5
_AS_ESTIMATED_HIGH = 2.0

_SIGNIFICANT_LABELS = ("lower", "possibly_lower", "higher", "possibly_higher")


def _point_settings_keys(point: ChangePoint) -> list[tuple[str | None, str]]:
    """``(agent, raw settings key)`` pairs ``point.keys`` names, parsed
    the same way ``impact.measures_for`` parses a change point's own key
    labels -- kept as the raw key here instead of translating it into an
    ``impact.Measure``, since a prediction names the raw key it was
    logged under (``config.append_prediction_log``), not impact's own
    vocabulary."""
    out: list[tuple[str | None, str]] = []
    for label in point.keys or ():
        agent, _, key = label.rpartition(": ")
        key = key.split(".")[-1] if key.startswith(("effective.", "user_settings.", "project_settings.")) else key
        if label.startswith("agents."):
            parts = label.split(".")
            agent, key = (parts[1], parts[-1]) if len(parts) >= 3 else ("", key)
        out.append((agent or None, key))
    return out


def _match_point(prediction: dict, points: list[ChangePoint]) -> ChangePoint | None:
    """The nearest change point at or after this prediction's own
    timestamp that names the same ``(agent, measure_key)`` pair, or
    ``None`` when nothing matches (yet)."""
    pred_ts = change_points_mod._parse_iso(prediction.get("ts"))
    if pred_ts is None:
        return None
    wanted = (prediction.get("agent"), prediction.get("measure_key"))
    best: ChangePoint | None = None
    for point in points:
        if point.source == "revert" or point.ts < pred_ts:
            continue
        if wanted not in _point_settings_keys(point):
            continue
        if best is None or point.ts < best.ts:
            best = point
    return best


def _measure_for(prediction: dict) -> impact.Measure:
    agent = prediction.get("agent")
    if agent:
        return impact.Measure("agent_cost", f"{agent}: cost per spawn", "money", agent)
    return impact._COST


def _verdict(predicted_usd: float, measured_usd: float, label_key: str) -> str:
    significant = label_key in _SIGNIFICANT_LABELS
    if abs(predicted_usd) < _NEGLIGIBLE_USD:
        return "larger" if significant else "as_estimated"
    if significant and (predicted_usd > 0) != (measured_usd > 0):
        return "opposite"
    if not significant:
        return "smaller"
    ratio = abs(measured_usd) / abs(predicted_usd)
    if ratio < _AS_ESTIMATED_LOW:
        return "smaller"
    if ratio > _AS_ESTIMATED_HIGH:
        return "larger"
    return "as_estimated"


@dataclass(slots=True)
class _Judgement:
    verdict: str | None
    measured_usd: float | None
    measured_pct: float | None
    #: Enough before/after data to judge at all, whether or not the
    #: window could still grow.
    enough: bool


def _exact_saving(prediction: dict, point: ChangePoint, before, after, bundles: dict, pricing: Pricing):
    """The counterfactual's own figure for the predicted setting
    (a ``counterfactual.KeyResult``), when it has one that prices the
    after sessions both ways, else ``None``."""
    result = counterfactual.without_change(point, before, after, bundles, pricing)
    if result is None:
        return None
    wanted = (prediction.get("agent") or None, prediction.get("measure_key"))
    return next((row for row in result.per_key if (row.agent or None, row.key) == wanted and row.fidelity != "before"), None)


def _judge_row(
    prediction: dict, before: list[impact.SessionFacts], after: list[impact.SessionFacts], units: Units, exact=None
) -> _Judgement:
    if exact is not None:
        # The same sessions priced both ways: no noise to test, so any
        # difference past the negligible line is a real one.
        measured_usd = round(exact.without_usd - exact.paid_usd, 6)
        label = "no_clear_change" if abs(measured_usd) < _NEGLIGIBLE_USD else "lower" if measured_usd > 0 else "higher"
        pct = round((exact.paid_usd - exact.without_usd) / exact.without_usd * 100.0, 1) if exact.without_usd else None
        return _Judgement(_verdict(prediction["predicted_usd"], measured_usd, label), measured_usd, pct, True)
    measure = _measure_for(prediction)
    row = impact._measure_row(measure, before, after, units)
    # A single-row list: Holm correction over one tested measure is the
    # same as uncorrected (impact._label_rows is what actually turns a
    # row's default "no_clear_change"/"too_little_data" into "lower"/
    # "higher"/"possibly_lower"/"possibly_higher" -- _measure_row alone
    # only computes the p-value, same split impact.compare() itself has
    # between the two calls).
    impact._label_rows([row])
    if row["before_value"] is None or row["after_value"] is None or row["label_key"] == "too_little_data":
        return _Judgement(None, None, None, False)
    measured_usd = round((row["before_value"] - row["after_value"]) * row["after_n"], 6)
    verdict = _verdict(prediction["predicted_usd"], measured_usd, row["label_key"])
    return _Judgement(verdict, measured_usd, row["change_pct"], True)


#: ``meta`` key set once :func:`_reopen_too_little_data` has run.
_REOPENED_KEY = "predictions_too_little_data_reopened"


def _reopen_too_little_data(store) -> None:
    """Once per store, put every prediction judged ``too_little_data``
    back to unjudged. An earlier version bounded a change's windows by
    changes in other projects and by every flip of model, effort or
    CLAUDE.md between sessions, so those verdicts were reached on windows
    that were too short; the sessions now judge them again. Recorded in
    ``meta`` so a verdict reached afterwards, on the windows as they are
    now, stays. One whose window is open again waits like any other
    unjudged prediction, and ``Store.prune_predictions`` drops it 90 days
    after it was made."""
    if store.get_meta(_REOPENED_KEY) is not None:
        return
    store.reopen_predictions("too_little_data")
    store.set_meta(_REOPENED_KEY, "1")


def judge_predictions(
    store, corpus, pricing: Pricing, units: Units, config_dir, *, now: datetime | None = None
) -> int:
    """Match and judge every unjudged prediction that can be, persisting
    each verdict via ``Store.judge_prediction``. Returns how many were
    judged. A prediction that matched a change point but still has too
    little data, and whose after-window is still open (no later change
    point bounds it yet; for a change to every project, no later change to
    every project: :func:`impact.bounds`'s ``following``), is left for a
    later call rather than forced to ``too_little_data`` early. The first
    call on a store also judges again every prediction an earlier version
    closed out as ``too_little_data`` (:func:`_reopen_too_little_data`)."""
    now = now or datetime.now(timezone.utc)
    _reopen_too_little_data(store)
    points = change_points_mod.change_points(config_dir, corpus)
    if not points:
        return 0
    sessions = impact.session_facts(corpus, pricing)
    bundles = {bundle.session_id: bundle for bundle in corpus.sessions}
    judged = 0
    for prediction in store.predictions(judged=False):
        point = _match_point(prediction, points)
        if point is None:
            continue
        # impact's own bounds and project, so a back-tested window and an
        # impact comparison of the same change never disagree.
        previous, following, per_project = impact.bounds(points, point, sessions)
        before, after = impact.sides(
            point, sessions, previous=previous, following=following, per_project=per_project, now=now
        )
        exact = _exact_saving(prediction, point, before, after, bundles, pricing)
        result = _judge_row(prediction, before, after, units, exact)
        if not result.enough:
            if following is None:
                # The after-window is still open -- more sessions may
                # yet arrive to judge this by. Try again next time.
                continue
            result = _Judgement("too_little_data", None, None, True)
        store.judge_prediction(
            prediction["id"],
            change_ts=point.iso(),
            verdict=result.verdict,
            measured_usd=result.measured_usd,
            measured_pct=result.measured_pct,
        )
        judged += 1
    return judged


#: EST-P6: a (agent, measure_key) grouping needs at least this many
#: judged predictions with a real measured amount before its own
#: calibration multiplier is trusted over the prior (1.0, unchanged).
MIN_JUDGED_FOR_CALIBRATION = 3


def calibration_multipliers(store) -> dict[tuple[str | None, str], float]:
    """EST-P6: a ``(agent, measure_key) -> multiplier`` lookup, one entry
    per grouping with at least :data:`MIN_JUDGED_FOR_CALIBRATION` judged
    predictions that had a real measured amount to learn from (a
    ``too_little_data`` verdict has none, and contributes nothing here
    either). The multiplier is the mean of that grouping's own
    ``measured_usd / predicted_usd`` ratios. Passed to
    ``whatif.estimate``'s own ``calibration`` argument, so a setting this
    tool has historically over- or under-estimated for you specifically
    reads adjusted (fidelity ``"calibrated"``) the next time you look --
    a grouping with too few judged points simply has no entry here,
    which is the prior of 1.0 (:func:`whatif.estimate` leaves it alone)."""
    groups: dict[tuple[str | None, str], list[float]] = {}
    for row in store.predictions(judged=True):
        predicted = row.get("predicted_usd")
        measured = row.get("measured_usd")
        if not predicted or measured is None:
            continue
        groups.setdefault((row.get("agent"), row.get("measure_key")), []).append(measured / predicted)
    return {key: sum(ratios) / len(ratios) for key, ratios in groups.items() if len(ratios) >= MIN_JUDGED_FOR_CALIBRATION}


def _money_text(value: float | None, units: Units) -> str:
    if value is None:
        return ""
    amount = units.money(abs(value))
    if amount is None:
        return "No measurable change"
    return f"Saves {amount.text()}" if value > 0 else f"Costs {amount.text()} more"


#: One line per verdict (and the unjudged/not-yet-matched case), the same
#: "server writes the sentence, the dashboard just shows it" rule
#: ``quality.verdict`` and ``impact._verdict`` already follow -- so the
#: Backtest section never has to turn a closed-vocabulary key into English
#: itself.
_VERDICT_TEXT = {
    "as_estimated": "Came in about as estimated.",
    "smaller": "Measured effect was smaller than estimated.",
    "larger": "Measured effect was larger than estimated.",
    "opposite": "Went the other way from the estimate.",
    "too_little_data": "Not enough sessions after the change to judge it.",
}


def _verdict_text(row: dict) -> str:
    verdict = row.get("verdict")
    if verdict is None:
        return "Not judged yet."
    return _VERDICT_TEXT.get(verdict, verdict)


def present(predictions: list[dict], units: Units) -> list[dict]:
    """``store.predictions()`` rows, display-ready: each with
    server-formatted money text (``predicted_text``/``measured_text``)
    and a one-line ``verdict_text``, the same "server formats money and
    verdicts, dashboard just shows them" rule the rest of the UI already
    follows -- the dashboard's own Backtest section never has to format a
    dollar amount or turn a verdict key into English itself."""
    out = []
    for row in predictions:
        out.append(
            {
                **row,
                "predicted_text": _money_text(row.get("predicted_usd"), units),
                "measured_text": _money_text(row.get("measured_usd"), units),
                "verdict_text": _verdict_text(row),
            }
        )
    return out


__all__ = ["VERDICTS", "MIN_JUDGED_FOR_CALIBRATION", "judge_predictions", "calibration_multipliers", "present"]

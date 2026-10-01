# P8: did an estimate come true? (`backtest.py`)

Every "what if?" estimate the dashboard shows you (`whatif.py`) is a
projection: repriced tokens, a replayed cache simulation, a size read
off a table. None of it is checked against what actually happened once
you make the change — until now. `backtest.py` (EST-P4) closes that
loop: it matches an estimate you tracked to the real change it turned
into, and judges it against the sessions before and after, using the
same before/after machinery `docs/profiles.md`'s "Your changes and what
they did" section already uses for every settings change
(`impact.py`, EST-P3). This document is the method; `docs/api.md`'s
`GET /api/backtest` and `POST /api/whatif` sections are the wire
contract, and `docs/profiles.md`'s "Did your estimates come true?"
section is the dashboard-facing summary.

## What gets tracked

Nothing is tracked automatically. `POST /api/whatif` (the "what if?"
tool behind Create a profile, a profile's detail, and Setup ›
Profiles' editor) only logs a prediction when the caller sets `"log":
true`. The dashboard sets it when you save a profile made from a goal
(the ticked changes), and the first time you copy a profile's prompt or
command after opening it: changes you mean to make, never a tick or a
slider moved while you explore. A logged row
goes to this tool's own `prediction-log.jsonl`
(`config.append_prediction_log`), is picked up by the file watcher's
next tick (`service.watcher._scan_predictions`) into the store's
`predictions` table, and from there is judged the next time
`GET /api/backtest` runs (or a running `serve`'s own watcher tick calls
it in the background).

Each row is one settings key, on one occasion: `measure_key` (the raw
settings key, e.g. `"model"`, `"subagentPromptCacheTtl"`), an optional
`agent` (an agent-scoped change), and the *uncalibrated* `predicted_usd`
— never a calibrated figure, so calibrating an already-calibrated
number can never compound (see "Calibration" below).

## The method, in five steps

1. **Match.** A prediction is matched to the *nearest* change point at
   or after its own timestamp whose own keys name the same `(agent,
   key)` pair (`_point_settings_keys`, which parses a change point's
   key labels the same way `impact.measures_for` does). A `"revert"`
   point is never a match — undoing a change isn't making the one that
   was predicted. A prediction with no matching change point yet (you
   looked, but never applied it) is left alone; it eventually expires
   unjudged (`Store.prune_predictions`'s 90-day unseen window).
2. **Window.** Once matched, before/after are exactly `impact.compare`'s
   own windowing: up to `impact.LOOKBACK_DAYS` (14) before the change,
   or back to the previous change point if that's sooner, and from the
   change until the *next* change point after it, or now if there isn't
   one yet (`impact.sides` and `impact.neighbours`). A change point
   within ten minutes of this one, or with fewer than
   `impact.MIN_SESSIONS` (3) sessions between them, doesn't bound it:
   the two share their before and after. A change made in
   one project (an apply to its settings files, or a change only its
   own files made: its settings files, its agents or its `.mcp.json`) is
   judged on that project's sessions only, and only changes that apply
   there bound it. A change to every project is bounded in each project
   on its own (`impact.bounds`, the same call the Your changes cards
   make): by the changes that apply there, with the sessions between
   them counted in that project. So a change made in
   one project cuts that project's before and after and no other's, and a
   change to every project reads, across all projects, as the sum of its
   readings in each. Sessions started under either spelling of a Windows
   drive letter (`c--Dev-x` and `C--Dev-x`) are one project, and a change
   recorded under either applies to them.
3. **Measure.** The dollar quantity a prediction estimated is always
   either the whole session's cost (a main-session-level setting) or
   one agent's cost per spawn (an agent-scoped setting) — the same two
   totals `whatif.estimate` itself reprices from. Its before/after
   estimate is `impact._measure_row`'s own ratio-of-sums,
   stratum-reweighted, Holm-tested row (unchanged from EST-P3), called
   for this one measure instead of a change point's whole table. When
   `counterfactual.py` can undo the predicted setting on the sessions
   after the change themselves (repriced, simulated or approximate, see
   [concepts](concepts.md#6-windows-what-if-estimates-and-beforeafter-comparisons)),
   that figure is the measured total instead: it prices the same
   sessions both ways, so nothing else that differs between the two
   sides counts. It has no noise to test, so any difference of a cent
   or more reads as real.
4. **Measured total.** `impact._measure_row` gives a *rate* (dollars
   per session, or per spawn) before and after. `(before_rate -
   after_rate) * after_n` turns the rate's drop (or rise) into a dollar
   total comparable to `predicted_usd`, scaled to the after-window this
   change actually got — which usually isn't the same length as the
   report window open when the prediction was made. Read the two
   totals' *ratio*, not their exact dollar difference, as what the
   verdict below is built from.
5. **Verdict**, one of a closed set: `as_estimated`, `smaller`,
   `larger`, `opposite`, `too_little_data`.

## Verdicts

| Verdict | Meaning |
|---|---|
| `as_estimated` | The measured effect is within half to double the predicted one (or both are negligible). |
| `smaller` | The measured effect is under half the predicted one, or the before/after difference wasn't statistically significant. |
| `larger` | The measured effect is more than double the predicted one. |
| `opposite` | The measured direction is statistically significant and the opposite sign from the prediction (a predicted saving that cost more instead, or vice versa). |
| `too_little_data` | Too few sessions on one side to tell, and the window has since closed (a later change point bounds it), so no more sessions can ever arrive to help. |

A predicted amount under $0.01 (either way) is treated as "predicted no
real effect": any measurable effect at all is `larger`; no significant
effect is `as_estimated`.

**A still-open window is never forced to a verdict.** `too_little_data`
is deferred, not persisted, the first time a match is found with
insufficient before/after sessions — it is only written once a *later*
change point already bounds the after-window (so no more sessions can
ever arrive for it). Otherwise the prediction is left unjudged and
retried on a future call, so a change you just made isn't prematurely
locked into a permanent negative verdict while data is still coming in.
For a change to every project, only a later change to every project
closes the window: a change made in one project bounds that project's
after-window alone, and another project, or one not seen yet, can still
add sessions. A prediction still waiting 90 days after it was made
expires unjudged (`Store.prune_predictions`), like one that never
matched.

**Older `too_little_data` verdicts are judged again, once.** Versions
before 0.13.0 cut a change's windows at every change made in any project,
and at every flip of model, effort or CLAUDE.md between two sessions, so
many windows closed too short. The first judging on a store puts every
`too_little_data` prediction back to unjudged (a `meta` flag in the
store records that it ran), and the sessions judge them again on the
windows as they are now. A verdict reached after that stays; one whose
window is open again waits, and expires, like any other.

## Calibration (EST-P6)

Once at least `backtest.MIN_JUDGED_FOR_CALIBRATION` (3) of your own
past predictions for the same `(agent, measure_key)` have been judged
— excluding `too_little_data` ones, which have no measured amount —
`backtest.calibration_multipliers` averages their `measured_usd /
predicted_usd` ratios into one multiplier per key. `whatif.estimate`
applies a matching multiplier to that row's `saving_usd`, and its
`fidelity` becomes `"calibrated"`. The prior is implicitly 1.0: a key
with fewer than 3 judged points is estimated exactly as before.

The pre-calibration value and fidelity survive in every row as
`uncalibrated_usd`/`uncalibrated_fidelity` (`null` when calibration
wasn't applicable or didn't apply), specifically so `POST
/api/whatif`'s `"log": true` always logs the *raw* estimate. Logging a
calibrated number would calibrate on top of a previous calibration the
next time round, compounding whatever the multiplier was toward zero or
away from it — this is why the log always uses the uncalibrated figure.

## Reading it

- **Dashboard:** Setup › Settings' "Did your estimates come true?"
  table (`GET /api/backtest`), right under "Your changes and what they
  did" — see `docs/profiles.md`.
- **CLI:** `claudeglass backtest` reads the store read-only
  (`service.store.read_predictions`, the same read-only-connection
  posture `_merge_dashboard_marks` already gives session tags/ratings)
  and prints every judged prediction as a table, plus a count still
  pending. It never judges anything itself — judging happens where the
  data already lives, in a running `serve`'s watcher tick or its own
  `GET /api/backtest` call — so running the CLI command with `serve`
  never running, or before the dashboard has been opened since the
  prediction was logged, can show 0 judged even with predictions
  pending. Exits 1 with no predictions logged at all; exits 0
  otherwise, even with nothing judged yet.
- **API:** `GET /api/backtest` (does the judging, cached in the
  background like `/api/impact`), `POST /api/predictions/seen` (marks a
  shown prediction seen) — see `docs/api.md`.

## What this is not

Like `impact.py`'s own comparisons, a verdict here is **observed, not
controlled** (see `docs/compare.md`'s section of the same name): the
sessions before and after a change also differ in workload, so a
`larger`/`smaller`/`opposite` verdict is a signal about how the
estimate held up in practice, not proof the setting alone caused the
difference. And because the measured total is scaled to whatever
after-window a change happened to get, two verdicts of the same kind
(`smaller`, `larger`, ...) aren't necessarily comparable in raw dollar
terms to each other — only each one's own ratio to its own prediction.

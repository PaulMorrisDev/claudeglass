/* claudeglass mod: lib/rest.js
 *
 * When a hint may show again. Each kind of hint rests after it shows,
 * twice as long each time it comes back (up to a few doublings), and
 * wakes early only when what is at stake has grown. The same rule the
 * capture hook applies (src/claudeglass/hooks/capture_hook.py: _gate,
 * _stamp, _rest_beside, _FRESH_START_HINTS), over a plain state object
 * instead of the coach-state file, with times in seconds. Nothing here
 * mutates its input or reads a clock. On top of that rule the mod keeps
 * its own cap: at most one note every few replies (noteAllowed).
 */

/**
 * One hint kind's last showing.
 * @typedef {Object} HintRow
 * @property {number} ts      When it showed, seconds.
 * @property {number} stake   What was at stake then (tokens, dollars, whatever the kind measures).
 * @property {number} n       How many times it has shown.
 * @property {number} rest    How long it rests, seconds.
 * @property {boolean} [once] Shows once a session, however much grows.
 */

/**
 * @typedef {Object} RestState
 * @property {Object<string, HintRow>} hints  By kind.
 */

/**
 * The rest rule's numbers: capture_catalogue.COACHING_THRESHOLDS (with the
 * user's overrides), of which these are used.
 * @typedef {Object} RestThresholds
 * @property {number} cooldown_minutes  How long a hint rests after its first showing.
 * @property {number} rearm_factor      It wakes early when its stake has grown this many times.
 * @property {number} max_backoff       The most times its rest doubles.
 * @property {number} [cold_rest_hours] How long the cold-return receipt rests.
 */

/**
 * The hints about starting afresh. One that showed rests the others for
 * the cooldown.
 * @type {string[]}
 */
export var FRESH_START_HINTS = ["plan_fresh", "plan_fresh_early", "cold_return"];

function number(value) {
  return typeof value === "number" && isFinite(value) ? value : null;
}

/**
 * A copy of `state` with its own `hints` table and rows, so the caller's
 * state is never changed.
 * @param {RestState|null|undefined} state
 * @returns {RestState}
 */
function copyState(state) {
  var hints = {};
  var source = state && state.hints && typeof state.hints === "object" ? state.hints : {};
  Object.keys(source).forEach(function (kind) {
    if (source[kind] && typeof source[kind] === "object") hints[kind] = Object.assign({}, source[kind]);
  });
  return Object.assign({}, state, { hints: hints });
}

function rowOf(state, kind) {
  var hints = state && state.hints;
  if (!hints || !Object.prototype.hasOwnProperty.call(hints, kind)) return null;
  var row = hints[kind];
  return row && typeof row === "object" ? row : null;
}

/**
 * How long a stamped hint rests: the time stored with it, or the cooldown
 * for a row written before one was stored.
 * @param {HintRow} prior
 * @param {RestThresholds} th
 * @returns {number}
 */
function restSeconds(prior, th) {
  var stored = number(prior.rest);
  return stored !== null && stored > 0 ? stored : th.cooldown_minutes * 60;
}

/**
 * Whether a hint of `kind` may show: not yet shown, its rest is over, or
 * what is at stake has grown `rearm_factor` times since it last showed.
 * One marked `once` never shows again.
 * @param {RestState|null|undefined} state
 * @param {string} kind
 * @param {number} stake
 * @param {number} nowS
 * @param {RestThresholds} th
 * @returns {boolean}
 */
export function mayShow(state, kind, stake, nowS, th) {
  var prior = rowOf(state, kind);
  if (prior === null) return true;
  if (prior.once) return false;
  var lastTs = number(prior.ts);
  if (lastTs !== null && nowS - lastTs >= restSeconds(prior, th)) return true;
  var lastStake = number(prior.stake);
  return lastStake !== null && lastStake > 0 && stake >= lastStake * th.rearm_factor;
}

/**
 * Records that `kind` showed, and returns the new state. It then rests for
 * `rest` seconds or, unless told how long, the cooldown doubled for each
 * earlier showing (up to `max_backoff` doublings): a hint that keeps
 * coming back is one the user has decided to ignore. `once` marks it as
 * not to show again.
 * @param {RestState|null|undefined} state
 * @param {string} kind
 * @param {number} stake
 * @param {number} nowS
 * @param {RestThresholds} th
 * @param {number|null} [rest]
 * @param {boolean} [once]
 * @returns {RestState}
 */
export function stamp(state, kind, stake, nowS, th, rest, once) {
  var next = copyState(state);
  var prior = rowOf(next, kind);
  var shown = prior ? Math.trunc(number(prior.n) || 0) + 1 : 1;
  var seconds = typeof rest === "number" ? rest : th.cooldown_minutes * 60 * Math.pow(2, Math.min(shown - 1, Math.trunc(th.max_backoff)));
  /** @type {HintRow} */
  var row = { ts: nowS, stake: stake, n: shown, rest: seconds };
  if (once) row.once = true;
  next.hints[kind] = row;
  return next;
}

/**
 * Rests `kind` for the cooldown because a hint like it just showed,
 * whatever is at stake now, and returns the new state. It isn't counted as
 * a showing, and a longer rest it already has is kept.
 * @param {RestState|null|undefined} state
 * @param {string} kind
 * @param {number} nowS
 * @param {RestThresholds} th
 * @returns {RestState}
 */
export function restBeside(state, kind, nowS, th) {
  var next = copyState(state);
  var cooldown = th.cooldown_minutes * 60;
  var prior = rowOf(next, kind);
  if (prior !== null) {
    var lastTs = number(prior.ts);
    if (lastTs !== null && lastTs + restSeconds(prior, th) >= nowS + cooldown) {
      prior.stake = 0;
      return next;
    }
  }
  var shown = prior ? Math.trunc(number(prior.n) || 0) : 0;
  /** @type {HintRow} */
  var row = { ts: nowS, stake: 0, n: shown, rest: cooldown };
  if (prior !== null && prior.once) row.once = true;
  next.hints[kind] = row;
  return next;
}

/**
 * Everything that follows a hint showing, as the capture hook does it:
 * the cold-return receipt rests `cold_rest_hours` however the context
 * grows, any other hint backs off; a hint about starting afresh rests the
 * others like it. `kind` may carry a suffix after a colon ("split_run:x");
 * only the part before it names the hint.
 * @param {RestState|null|undefined} state
 * @param {string} kind
 * @param {number} stake
 * @param {number} nowS
 * @param {RestThresholds} th
 * @param {boolean} [once]
 * @returns {RestState}
 */
export function recordShown(state, kind, stake, nowS, th, once) {
  var hint = kind.split(":")[0];
  var next =
    hint === "cold_return"
      ? stamp(state, kind, 0, nowS, th, (th.cold_rest_hours || 0) * 3600, once)
      : stamp(state, kind, stake, nowS, th, null, once);
  if (FRESH_START_HINTS.indexOf(hint) !== -1) {
    for (var i = 0; i < FRESH_START_HINTS.length; i++) {
      if (FRESH_START_HINTS[i] !== hint) next = restBeside(next, FRESH_START_HINTS[i], nowS, th);
    }
  }
  return next;
}

/**
 * The mod's own cap on top of the rest rule: a note may show when none has
 * yet, or at least `every` replies have passed since the last.
 * @param {number|null|undefined} lastNoteReply  Index of the reply the last note showed after.
 * @param {number} replyIndex                     Index of the reply now ending.
 * @param {number} [every]                        Defaults to 5.
 * @returns {boolean}
 */
export function noteAllowed(lastNoteReply, replyIndex, every) {
  var gap = typeof every === "number" ? every : 5;
  if (typeof lastNoteReply !== "number" || !isFinite(lastNoteReply)) return true;
  return replyIndex - lastNoteReply >= gap;
}

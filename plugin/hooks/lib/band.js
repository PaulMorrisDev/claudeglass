/* claudeglass mod: lib/band.js
 *
 * The band above the prompt is one health line and one Button. This is its pure
 * part: which of four states the session is in, and the words of the line at the
 * width there is. Nothing here reads a clock, the disk or the engine; the words
 * come in as templates (the strings in generated/copy.js) and the widths are in
 * cells, which the caller measures.
 *
 *   fine    plenty of room or filling: the Glass Button
 *   tight   close to the summarising point: Summarise (or review first)
 *   review  the last summary lost something the user has not looked at and not yet put back: Re-add
 *   kept    the last summary lost nothing and no reply has ended since: Glass
 */

import { fill, joinParts } from "./text.js";

/**
 * @typedef {"fine"|"tight"|"review"|"kept"} Health
 */

/**
 * @typedef {Object} HealthFacts
 * @property {string|null} reading     The context state (plenty, filling, soon, next); null before the first reading.
 * @property {boolean} summarised      A summary has been checked this session.
 * @property {number} missing          Items the last summary lost (missing or only partly kept).
 * @property {number} pending          Of those, the items not yet put back: what Re-add would add.
 * @property {boolean} seen            The user has looked at that summary.
 * @property {number} total            Items the user said that the last summary was checked against.
 * @property {number} replies          Main replies this session so far.
 * @property {number|null} repliesAt   The reply count when that summary was made; null before one.
 */

/**
 * The state the band is in; null before the first reading (the band then draws nothing new).
 * @param {HealthFacts} facts
 * @returns {Health|null}
 */
export function healthOf(facts) {
  if (facts.reading === null) return null;
  if (facts.summarised && facts.missing > 0 && facts.pending > 0 && !facts.seen) return "review";
  if (facts.reading === "soon" || facts.reading === "next") return "tight";
  // Kept shows until the next reply ends (the count of replies moves on), then it is Fine again.
  if (facts.summarised && facts.missing === 0 && facts.total > 0 && facts.repliesAt !== null && facts.replies === facts.repliesAt) {
    return "kept";
  }
  return "fine";
}

/** Cells a Button takes beyond its label (the brackets and their padding). */
export var BUTTON_PADDING = 4;
/** Cells the dot takes, and the gap between the dot, the state word and the line. */
export var LEAD_CELLS = 1;
export var WORD_GAP = 2;

/**
 * Cells a Button with this label takes.
 * @param {string} label
 * @returns {number}
 */
export function buttonCells(label) {
  return label.length + BUTTON_PADDING;
}

/**
 * @typedef {Object} HealthWords
 * @property {string} full         "{pct}% full"
 * @property {string} replies      "about {replies} replies before a summary"
 * @property {string} repliesOne   "about 1 reply before a summary"
 * @property {string} point        "Claude summarises at {tokens}"
 * @property {string} autoOff       "Claude won't summarise by itself", for a context with no summarising point
 * @property {string} lost         "Summary lost {missing} of {total} items"
 * @property {string} kept         "Summary kept all {total} items"
 * @property {string} keptOne      "Summary kept the 1 item"
 */

/**
 * @typedef {Object} HealthFigures
 * @property {string} pct          The share of the window used, written ("34").
 * @property {number|null} replies Replies before the summary; null when not known yet.
 * @property {string|null} point  The summarising point, written ("160k"); null when auto-compact is off, so the line has no runway and says so.
 * @property {number} missing
 * @property {number} total
 */

/**
 * The forms of the line, longest first. The last is empty: the state word and the Button alone.
 * @param {Health} state
 * @param {HealthFigures} figures
 * @param {HealthWords} words
 * @returns {string[]}
 */
export function healthForms(state, figures, words) {
  if (state === "review") return [fill(words.lost, { missing: figures.missing, total: figures.total }), ""];
  if (state === "kept") return [figures.total === 1 ? words.keptOne : fill(words.kept, { total: figures.total }), ""];
  var full = fill(words.full, { pct: figures.pct });
  var left = figures.replies;
  if (figures.point === null) return [joinParts([full, words.autoOff]), full, ""];
  var runway =
    left === null || left < 1
      ? fill(words.point, { tokens: figures.point })
      : left === 1
        ? words.repliesOne
        : fill(words.replies, { replies: left });
  return [joinParts([full, runway]), full, ""];
}

/**
 * The line that fits `room` cells: the longest form that does; the state word and the Button alone ("") when none does.
 * @param {Health} state
 * @param {HealthFigures} figures
 * @param {HealthWords} words
 * @param {number} room
 * @returns {string}
 */
export function healthLine(state, figures, words, room) {
  var forms = healthForms(state, figures, words);
  for (var i = 0; i < forms.length; i++) {
    var form = /** @type {string} */ (forms[i]);
    if (form.length <= room) return form;
  }
  return "";
}

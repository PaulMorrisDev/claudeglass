/* claudeglass mod: lib/context.js
 *
 * How full the context is, where Claude summarises it, and how many
 * replies are left before that. Reads the figures Claude Code gives the
 * mod ($.session.usage({ breakdown: "summary" }).context) and agrees with
 * /context: the percentage shown is /context's own, and the compaction
 * point is the user's (autoCompactWindow and friends), not the model's
 * raw window. No Python twin; the rules are in the plan (sections 1-3).
 */

/**
 * @typedef {Object} ContextCategory
 * @property {string} name   "System prompt", "Messages", "Free space", ...
 * @property {number} tokens
 * @property {"used"|"free"|"buffer"|"deferred"} kind
 * @property {boolean} [isDeferred]
 */

/**
 * @typedef {Object} ContextBreakdown
 * @property {ContextCategory[]} [categories]
 * @property {number} [maxTokens]            The window /context measures against.
 * @property {number} [rawMaxTokens]         The model's own window.
 * @property {number} [autoCompactThreshold] Where Claude summarises, when it says.
 * @property {number} [percentage]           /context's own percent full, of `maxTokens`.
 * @property {string} [autocompactSource]
 */

/**
 * The `context` of `$.session.usage({ breakdown: "summary" })`.
 * @typedef {Object} ContextUsage
 * @property {number} [tokens]
 * @property {number} [window]
 * @property {number} [percent]    Of the raw window; never shown.
 * @property {ContextBreakdown} [breakdown]
 */

/**
 * @typedef {"plenty"|"filling"|"soon"|"next"} ContextState
 */

/**
 * @typedef {Object} ContextParts
 * @property {number|null} startup       Claude Code and your instructions: used categories except Messages.
 * @property {number|null} conversation  The "Messages" category.
 * @property {number|null} free          The "Free space" category.
 */

/**
 * @typedef {Object} ContextReading
 * @property {number} pct      Percent full, as /context shows it.
 * @property {number} tokens   Tokens in the context now.
 * @property {number} point    Tokens at which Claude summarises.
 * @property {number} window   The model's window.
 * @property {ContextState} state  plenty under 50% of `point`, filling to 75%, soon to 90%, next above.
 * @property {ContextParts} parts  Null parts when there is no breakdown.
 */

/**
 * @param {*} value
 * @returns {value is number}
 */
function finite(value) {
  return typeof value === "number" && isFinite(value);
}

/**
 * @param {*} name
 * @param {string} wanted
 * @returns {boolean}
 */
function same(name, wanted) {
  return typeof name === "string" && name.trim().toLowerCase() === wanted;
}

/**
 * Where the context sits against the point Claude summarises at.
 * @param {number} tokens
 * @param {number} point
 * @returns {ContextState}
 */
function stateOf(tokens, point) {
  if (!(point > 0)) return "plenty";
  var share = tokens / point;
  if (share < 0.5) return "plenty";
  if (share < 0.75) return "filling";
  if (share < 0.9) return "soon";
  return "next";
}

/**
 * The reading the band and the pane show. `pct` is `breakdown.percentage`
 * when present (so it matches /context), else tokens over `maxTokens`, else
 * over the window. `point` is `autoCompactThreshold` when present, else
 * `maxTokens` less the buffer category, else the window. Without a
 * breakdown (or without categories) everything falls back to `tokens`
 * and `window` and `parts` are null.
 * @param {ContextUsage|null|undefined} context
 * @returns {ContextReading}
 */
export function readContext(context) {
  var c = context || {};
  var b = c.breakdown || null;
  var categories = b && Array.isArray(b.categories) ? b.categories : [];

  var startup = 0;
  var conversation = 0;
  var free = 0;
  var usedTotal = 0;
  var buffer = 0;
  var sawStartup = false;
  var sawConversation = false;
  var sawBuffer = false;
  for (var i = 0; i < categories.length; i++) {
    var category = categories[i];
    if (!category || !finite(category.tokens)) continue;
    if (category.kind === "buffer") {
      buffer += category.tokens;
      sawBuffer = true;
    } else if (same(category.name, "free space") || category.kind === "free") {
      free += category.tokens;
    } else if (category.kind === "used" && !category.isDeferred) {
      usedTotal += category.tokens;
      if (same(category.name, "messages")) {
        conversation += category.tokens;
        sawConversation = true;
      } else {
        startup += category.tokens;
        sawStartup = true;
      }
    }
  }

  var maxTokens = b && finite(b.maxTokens) && b.maxTokens > 0 ? b.maxTokens : null;
  var windowTokens = finite(c.window) && c.window > 0 ? c.window : b && finite(b.rawMaxTokens) ? b.rawMaxTokens : maxTokens || 0;

  var tokens;
  if (finite(c.tokens)) tokens = c.tokens;
  else if (sawStartup || sawConversation) tokens = usedTotal;
  else if (b && finite(b.percentage) && maxTokens !== null) tokens = (b.percentage / 100) * maxTokens;
  else tokens = 0;

  var point;
  if (b && finite(b.autoCompactThreshold) && b.autoCompactThreshold > 0) point = b.autoCompactThreshold;
  else if (maxTokens !== null && sawBuffer && maxTokens - buffer > 0) point = maxTokens - buffer;
  else point = windowTokens;

  var pct;
  if (b && finite(b.percentage)) pct = b.percentage;
  else {
    var base = maxTokens !== null ? maxTokens : windowTokens;
    pct = base > 0 ? (tokens / base) * 100 : 0;
  }

  return {
    pct: pct,
    tokens: tokens,
    point: point,
    window: windowTokens,
    state: stateOf(tokens, point),
    parts: {
      startup: categories.length ? startup : null,
      conversation: categories.length ? conversation : null,
      free: categories.length ? free : null,
    },
  };
}

// How many of the latest growing replies set the pace.
var RUNWAY_STEPS = 5;

/**
 * Replies left before Claude summarises. `history` is the context after
 * each main-session reply, oldest first. The pace is the mean growth over
 * the latest (up to five) steps that grew; a drop is a compaction or a
 * /clear and is left out. Null with fewer than two growing steps.
 * @param {number[]} history
 * @param {number} point  Tokens at which Claude summarises.
 * @returns {number|null}
 */
export function runway(history, point) {
  if (!Array.isArray(history) || history.length < 2 || !finite(point)) return null;
  var steps = [];
  for (var i = 1; i < history.length; i++) {
    var step = /** @type {number} */ (history[i]) - /** @type {number} */ (history[i - 1]);
    if (step > 0) steps.push(step);
  }
  if (steps.length < 2) return null;
  var recent = steps.slice(-RUNWAY_STEPS);
  var total = 0;
  for (var j = 0; j < recent.length; j++) total += /** @type {number} */ (recent[j]);
  var mean = total / recent.length;
  return Math.max(0, Math.floor((point - /** @type {number} */ (history[history.length - 1])) / mean));
}

/**
 * Tokens the last tool results added to the context: the growth since the
 * previous reply, less that reply's own output (which is what the model
 * wrote, not what the tools returned). Never below zero.
 * @param {number} prevCtx     Context after the previous reply.
 * @param {number} prevOutput  Output tokens of the previous reply.
 * @param {number} ctx         Context now.
 * @returns {number}
 */
export function toolGrowth(prevCtx, prevOutput, ctx) {
  return Math.max(0, ctx - prevCtx - prevOutput);
}

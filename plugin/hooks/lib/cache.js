/* claudeglass mod: lib/cache.js
 *
 * The cache countdown. The mod gets no cache fields from Claude Code, so
 * warm or cold is worked out from the time since the last reply and the
 * cache lifetime, as the status line's "cache est 5m 04:12" does
 * (src/claudeglass/statusline.py: _fmt_cache_estimate, _cache_remaining_s,
 * resolve_effective_ttl, _format_mmss). Time is always passed in as
 * milliseconds; nothing here reads a clock.
 */

/**
 * @typedef {Object} CacheState
 * @property {boolean} isWarm       True while the cache entry is still alive.
 * @property {number} remainingMs   Time until it goes cold; 0 once cold.
 * @property {number} fractionLeft  `remainingMs` as a share of the whole lifetime, 0 to 1.
 */

// The cache lifetime assumed when nothing says otherwise (statusline.py
// _DEFAULT_TTL_S).
export var DEFAULT_TTL_SECONDS = 300;

var TTL_LABELS = { 300: "5m", 3600: "1h" };

/**
 * Round half to even, as Python's round() does, so a countdown that lands
 * on a half second reads the same here as in the status line.
 * @param {number} x
 * @returns {number}
 */
function roundHalfEven(x) {
  var floor = Math.floor(x);
  var rest = x - floor;
  if (rest < 0.5) return floor;
  if (rest > 0.5) return floor + 1;
  return floor % 2 === 0 ? floor : floor + 1;
}

/**
 * Whether a cache written at `lastResponseMs` is still alive at `nowMs`,
 * and for how long. A reply stamped ahead of `nowMs` (clock skew) never
 * shows more than a full lifetime. With no reply yet (`null`) the cache is
 * cold with nothing left.
 * @param {number|null|undefined} lastResponseMs  When the last reply finished, in epoch ms.
 * @param {number} nowMs
 * @param {number} ttlSeconds  The cache lifetime, 300 or 3600 in practice.
 * @returns {CacheState}
 */
export function cacheState(lastResponseMs, nowMs, ttlSeconds) {
  var ttlMs = ttlSeconds * 1000;
  if (typeof lastResponseMs !== "number" || !isFinite(lastResponseMs) || !(ttlMs > 0)) {
    return { isWarm: false, remainingMs: 0, fractionLeft: 0 };
  }
  var remaining = Math.min(ttlMs - (nowMs - lastResponseMs), ttlMs);
  if (remaining <= 0) return { isWarm: false, remainingMs: 0, fractionLeft: 0 };
  return { isWarm: true, remainingMs: remaining, fractionLeft: remaining / ttlMs };
}

/**
 * A countdown as the status line writes it: minutes and seconds, each two
 * digits ("04:12"). Seconds round to the nearest whole second (halves to
 * even); below zero reads "00:00".
 * @param {number} ms
 * @returns {string}
 */
export function formatClock(ms) {
  var total = Math.max(0, roundHalfEven(ms / 1000));
  var minutes = Math.floor(total / 60);
  var seconds = total % 60;
  return (minutes < 10 ? "0" : "") + minutes + ":" + (seconds < 10 ? "0" : "") + seconds;
}

/**
 * A cache lifetime as seconds: `300`/`"5m"`/`"300s"` and
 * `3600`/`"1h"`/`"3600s"` as the status line reads them, any other whole
 * number of seconds as itself. Null for anything else.
 * @param {*} value
 * @returns {number|null}
 */
export function parseTtl(value) {
  if (typeof value === "boolean") return null;
  if (typeof value === "number") return isFinite(value) ? Math.trunc(value) : null;
  if (typeof value === "string") {
    var low = value.trim().toLowerCase();
    if (low === "5m" || low === "300" || low === "300s") return 300;
    if (low === "1h" || low === "3600" || low === "3600s") return 3600;
    if (/^[+-]?\d+$/.test(low)) return parseInt(low, 10);
  }
  return null;
}

/**
 * The lifetime to assume for the countdown, in the status line's order
 * (resolve_effective_ttl): the payload's `prompt_cache.cache_ttl`, its
 * top-level `cache_ttl`, the configured default, then 300 seconds.
 * @param {Object|null|undefined} payload
 * @param {*} [configDefault]  The settings file's `cache.ttl_seconds`, if any.
 * @returns {number}
 */
export function resolveTtl(payload, configDefault) {
  var p = payload && typeof payload === "object" ? payload : null;
  if (p && p.prompt_cache && typeof p.prompt_cache === "object") {
    var nested = parseTtl(p.prompt_cache.cache_ttl);
    if (nested !== null) return nested;
  }
  if (p) {
    var top = parseTtl(p.cache_ttl);
    if (top !== null) return top;
  }
  var configured = parseTtl(configDefault);
  return configured !== null ? configured : DEFAULT_TTL_SECONDS;
}

/**
 * "5m" or "1h" for the two lifetimes Claude Code uses; "<n>s" otherwise.
 * @param {number} ttlSeconds
 * @returns {string}
 */
export function ttlLabel(ttlSeconds) {
  return Object.prototype.hasOwnProperty.call(TTL_LABELS, ttlSeconds) ? TTL_LABELS[ttlSeconds] : ttlSeconds + "s";
}

/* claudeglass mod: lib/rebuild.js
 *
 * Cache rebuilds: a reply that paid to write again context that should
 * have been read from the cache, and why. Mirrors src/claudeglass/recache.py
 * (detect, _is_rebuild, write_ttl_s, expired_by_clock and the order the
 * signatures are tried in); tests/test_mod_vectors.py runs both over the
 * same request sequences.
 *
 * Where the mod knows less than the dashboard:
 *   - It sees a pair of requests, not a transcript. The shared start of
 *     every context (the first request's cache read, "cr0") is an optional
 *     argument; without it only the original read-ratio test and the
 *     `full_expiry_cr` floor apply, as recache.detect does for a list that
 *     does not start at the first turn.
 *   - Usage has no 5-minute/1-hour split of cache writes, so the lifetime
 *     the previous request wrote at is passed in (`ttlSeconds`) rather than
 *     read from `cc_1h`/`cc_5m`, and is not carried forward past requests
 *     that wrote nothing: the caller does that (see writeTtlSeconds).
 *   - Python marks the reply after a compaction from the estimated
 *     compaction request or a compact_boundary event, and a usage-limit
 *     pause from the gap; the mod is told both (`afterCompaction`,
 *     `afterLimitWait`).
 *   - Python skips synthetic requests and the transcript's first turn; the
 *     mod marks the first request (`first`) and never passes synthetic ones.
 *     A reply with no known previous request (the mod started after the first
 *     turn, say after a resume) is still judged by the read-ratio test, as
 *     recache.detect does for the first turn of a list that starts later.
 */

/**
 * One main-session request, as the mod records it from `turn.step` usage.
 * @typedef {Object} RebuildRequest
 * @property {number} ts                When the reply finished, epoch ms.
 * @property {number} ctx               Context: input + cache read + cache write tokens.
 * @property {number} cacheRead         Cache read tokens.
 * @property {number} cacheWrite        Cache write tokens.
 * @property {boolean} afterCompaction  The first reply after a conversation summary.
 * @property {boolean} afterLimitWait   The wait before it spanned a usage-limit pause.
 * @property {boolean} [first]          The session's first request (never a rebuild).
 * @property {string} [model]
 */

/**
 * @typedef {Object} RebuildThresholds
 * @property {number} ctx_floor       A rebuild needs a context over this many tokens.
 * @property {number} cr_ratio        Flagged when the read is under this share of the context.
 * @property {number} full_expiry_cr  A flagged read under this many tokens is an expiry outright.
 * @property {number} huge_ctx        A very large context (kept for parity; not used here).
 * @property {number} session_read_margin  How far past the shared start a read may go and still be an expiry.
 */

/**
 * @typedef {Object} Rebuild
 * @property {"limit"|"compaction"|"expiry"|"prefix"} cause
 *   limit: a usage-limit pause; compaction: the first reply after a summary;
 *   expiry: the cache timed out; prefix: something upstream of the cached
 *   prefix changed (a model switch, a changed instruction or tool list).
 * @property {number} tokens     Tokens the reply wrote again (its cache write).
 * @property {boolean} expected  True for `limit` and `compaction`: not a habit to fix.
 */

/** The defaults recache.RecacheThresholds and SESSION_READ_MARGIN carry. @type {RebuildThresholds} */
export var REBUILD_DEFAULTS = {
  ctx_floor: 20000,
  cr_ratio: 0.2,
  full_expiry_cr: 2000,
  huge_ctx: 200000,
  session_read_margin: 3000,
};

/**
 * `thresholds` over the defaults; a key that is missing or not a finite
 * number keeps its default.
 * @param {Partial<RebuildThresholds>|null|undefined} thresholds
 * @returns {RebuildThresholds}
 */
function withDefaults(thresholds) {
  var out = {};
  var keys = Object.keys(REBUILD_DEFAULTS);
  for (var i = 0; i < keys.length; i++) {
    var value = thresholds ? thresholds[keys[i]] : undefined;
    out[keys[i]] = typeof value === "number" && isFinite(value) ? value : REBUILD_DEFAULTS[keys[i]];
  }
  return /** @type {RebuildThresholds} */ (out);
}

/**
 * How long the cache entry a request wrote lasts: the lifetime writes are
 * made at, or null when it wrote nothing (recache.write_ttl_s, with no
 * 5-minute/1-hour split to read).
 * @param {RebuildRequest} request
 * @param {number} ttlSeconds
 * @returns {number|null}
 */
export function writeTtlSeconds(request, ttlSeconds) {
  return request && request.cacheWrite > 0 ? ttlSeconds : null;
}

/**
 * Whether a wait reached the time the previous request's cache lasts
 * (recache.expired_by_clock). False when either is unknown.
 * @param {number|null|undefined} gapSeconds
 * @param {number|null|undefined} beforeTtlSeconds
 * @returns {boolean}
 */
export function expiredByClock(gapSeconds, beforeTtlSeconds) {
  return typeof gapSeconds === "number" && typeof beforeTtlSeconds === "number" && gapSeconds >= beforeTtlSeconds;
}

/**
 * Whether `cur` paid to write again context that should have been read
 * (recache._is_rebuild).
 * @param {RebuildRequest} cur
 * @param {RebuildRequest|null} prev
 * @param {number|null} cr0
 * @param {RebuildThresholds} th
 * @returns {boolean}
 */
function isRebuild(cur, prev, cr0, th) {
  var cr = cur.cacheRead;
  if (cr < th.cr_ratio * cur.ctx) return true;
  if (cr0 === null || !prev) return false;
  // Shrunk: a compaction or /clear replaced the session part; nothing expired.
  if (cur.ctx < prev.ctx) return false;
  // What a warm cache would have served: everything the previous request
  // read or wrote. If that was little more than the shared start, a warm
  // read and an expired one look the same.
  if (prev.cacheRead + prev.cacheWrite - cr <= th.session_read_margin) return false;
  if (cr <= cr0 + th.session_read_margin) return true;
  var ownPart = cur.ctx - cr0;
  return ownPart > 0 && cr - cr0 < th.cr_ratio * ownPart;
}

/**
 * Whether `cur` was a cache rebuild, and why. Null when it was not, when it
 * is the session's first request (`cur.first`), or when the context is at or
 * under `ctx_floor`. With no previous request (`prev` null) and not first,
 * only the read-ratio test applies, and the wait is unknown. The cause is the first that applies
 * of: limit (a usage-limit pause), compaction (the first reply after a
 * summary), expiry (the wait reached the previous request's cache
 * lifetime, or the read was no more than the shared start plus the margin,
 * or under `full_expiry_cr`), prefix (anything else).
 * @param {RebuildRequest|null|undefined} prev  The previous main-session request; null when none is known.
 * @param {RebuildRequest} cur
 * @param {Partial<RebuildThresholds>|null} [thresholds]  Over REBUILD_DEFAULTS.
 * @param {number|null} [ttlSeconds]  The lifetime `prev`'s cache had; null when unknown.
 * @param {number|null} [cr0]  The first request's cache read, when known.
 * @returns {Rebuild|null}
 */
export function detectRebuild(prev, cur, thresholds, ttlSeconds, cr0) {
  if (!cur || cur.first) return null;
  var th = withDefaults(thresholds);
  var shared = typeof cr0 === "number" ? cr0 : null;
  if (cur.ctx <= th.ctx_floor) return null;
  if (!isRebuild(cur, prev, shared, th)) return null;

  var cause;
  if (cur.afterLimitWait) {
    cause = "limit";
  } else if (cur.afterCompaction) {
    cause = "compaction";
  } else if (prev && expiredByClock((cur.ts - prev.ts) / 1000, ttlSeconds)) {
    cause = "expiry";
  } else if (cur.cacheRead < th.full_expiry_cr || (shared !== null && cur.cacheRead <= shared + th.session_read_margin)) {
    cause = "expiry";
  } else {
    cause = "prefix";
  }
  return { cause: cause, tokens: cur.cacheWrite, expected: cause === "limit" || cause === "compaction" };
}

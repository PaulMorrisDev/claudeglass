/* claudeglass mod: lib/shape.js
 *
 * Values kept in `$.state` outlive a reload of the mod (an update in the
 * middle of a session), so a value an older version wrote can lack fields
 * this version reads. fillMissing gives such a value the fields it lacks
 * instead of letting the code that reads it throw.
 */

/**
 * @param {unknown} value
 * @returns {boolean}
 */
function isPlain(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

/**
 * `value` with each key of `defaults` it lacks (or holds as undefined) taken
 * from `defaults`, and plain objects under both filled the same way. Arrays,
 * nulls and other values `value` holds are kept as they are. `value` itself
 * comes back when nothing was missing, and is never changed; a `value` that
 * is not a plain object gives `defaults`.
 * @template T
 * @param {unknown} value
 * @param {T} defaults
 * @returns {T}
 */
export function fillMissing(value, defaults) {
  if (!isPlain(value)) return defaults;
  if (!isPlain(defaults)) return /** @type {T} */ (value);
  var have = /** @type {Object<string, unknown>} */ (value);
  var want = /** @type {Object<string, unknown>} */ (defaults);
  /** @type {Object<string, unknown>|null} */
  var out = null;
  Object.keys(want).forEach(function (key) {
    var was = have[key];
    var next = was === undefined ? want[key] : isPlain(was) && isPlain(want[key]) ? fillMissing(was, want[key]) : was;
    if (next !== was) {
      if (out === null) out = Object.assign({}, have);
      out[key] = next;
    }
  });
  return /** @type {T} */ (out === null ? value : out);
}

/* claudeglass mod: lib/text.js
 *
 * Small helpers for the mod's words: templates with {placeholders} (the
 * strings in generated/copy.js) and the middle-dot line the band and the
 * hint line are joined into.
 */

/**
 * Fill a template's `{word}` placeholders from `vars`. A placeholder with
 * no value is left as written, so a missing figure shows up in review
 * instead of vanishing. Values are inserted as text and never re-scanned.
 * @param {string} template
 * @param {Object<string, string|number>} vars
 * @returns {string}
 */
export function fill(template, vars) {
  var values = vars || {};
  return String(template).replace(/\{([A-Za-z_][A-Za-z0-9_]*)\}/g, function (whole, name) {
    if (!Object.prototype.hasOwnProperty.call(values, name)) return whole;
    var value = values[name];
    return value === null || value === undefined ? whole : String(value);
  });
}

/**
 * Join the parts that have something in them with `sep` (" · " by
 * default). Empty strings, and anything that is not a string or a number,
 * are skipped.
 * @param {Array<string|number|null|undefined|false>} parts
 * @param {string} [sep]
 * @returns {string}
 */
export function joinParts(parts, sep) {
  var kept = [];
  for (var i = 0; i < (parts || []).length; i++) {
    var part = parts[i];
    if (typeof part === "number" && isFinite(part)) part = String(part);
    if (typeof part === "string" && part !== "") kept.push(part);
  }
  return kept.join(typeof sep === "string" ? sep : " · ");
}

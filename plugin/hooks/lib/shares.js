/* claudeglass mod: lib/shares.js
 *
 * Whole-percent shares that add up to 100. Rounding each share alone can leave a
 * list at 99 or 102 (12.5 / 12.5 / 37.5 / 37.5 reads 13 + 13 + 38 + 38), so the
 * units left over after rounding down go to the largest fractions, as lib/instructions.js
 * toolShares does for tokens. Pure arithmetic.
 */

function zero() {
  return 0;
}

/**
 * Each value's share of their sum as a whole percent, in the order given, adding up to exactly 100. Each is rounded
 * down, then the units left over go one each to the largest fractions (a tie to the larger value, then the earlier
 * one). Negative and non-finite values count as 0; all zero gives all zeros.
 * @param {number[]} values
 * @returns {number[]}
 */
export function wholePercents(values) {
  var clean = values.map(function (value) {
    return typeof value === "number" && isFinite(value) && value > 0 ? value : 0;
  });
  var sum = 0;
  for (var i = 0; i < clean.length; i++) sum += /** @type {number} */ (clean[i]);
  if (!(sum > 0)) return clean.map(zero);
  var out = [];
  /** @type {{at: number, fraction: number, value: number}[]} */
  var parts = [];
  var given = 0;
  for (var j = 0; j < clean.length; j++) {
    var value = /** @type {number} */ (clean[j]);
    var exact = (value * 100) / sum;
    var whole = Math.floor(exact);
    out.push(whole);
    parts.push({ at: j, fraction: exact - whole, value: value });
    given += whole;
  }
  parts.sort(function (x, y) {
    return y.fraction - x.fraction || y.value - x.value || x.at - y.at;
  });
  for (var k = 0; k < 100 - given && k < parts.length; k++) {
    var at = /** @type {{at: number}} */ (parts[k]).at;
    out[at] = /** @type {number} */ (out[at]) + 1;
  }
  return out;
}

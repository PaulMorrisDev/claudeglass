// Tests for the whole-percent shares (plugin/hooks/lib/shares.js).
//
//   node --test tests/js/shares.test.mjs

import test from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath, pathToFileURL } from "node:url";
import { dirname, join } from "node:path";

const lib = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "plugin", "hooks", "lib");
const { wholePercents } = await import(pathToFileURL(join(lib, "shares.js")).href);

const sum = (list) => list.reduce((total, one) => total + one, 0);

test("shares that round badly one by one still add up to exactly 100", () => {
  // 12.5 + 12.5 + 37.5 + 37.5: each rounded alone reads 13 + 13 + 38 + 38 = 102.
  const even = wholePercents([1, 1, 3, 3]);
  assert.equal(sum(even), 100);
  // The units left over go to the largest fractions, a tie to the larger value and then the earlier one.
  assert.deepEqual(even, [12, 12, 38, 38]);
  assert.deepEqual(wholePercents([3, 1, 3, 1]), [38, 12, 38, 12]);
  assert.deepEqual(wholePercents([1, 1, 2]), [25, 25, 50]);
  assert.deepEqual(wholePercents([5, 5, 5, 1]), [32, 31, 31, 6]);
  // Three equal parts: 33 + 33 + 33 is 99.
  assert.deepEqual(wholePercents([1, 1, 1]), [34, 33, 33]);
  assert.deepEqual(wholePercents([2, 1, 1, 0]), [50, 25, 25, 0]);
});

test("whatever the values, the shares add up to 100 and none is negative", () => {
  for (let n = 1; n <= 7; n += 1) {
    for (let seed = 1; seed <= 40; seed += 1) {
      const values = Array.from({ length: n }, (_, at) => ((seed * (at + 3) * 7919) % 101) / 7);
      const shares = wholePercents(values);
      if (sum(values) > 0) assert.equal(sum(shares), 100, JSON.stringify(values));
      assert.ok(shares.every((one) => Number.isInteger(one) && one >= 0));
    }
  }
});

test("nothing to share gives zeros, and bad values count as nothing", () => {
  assert.deepEqual(wholePercents([0, 0]), [0, 0]);
  assert.deepEqual(wholePercents([]), []);
  assert.deepEqual(wholePercents([Number.NaN, -3, 5]), [0, 0, 100]);
});

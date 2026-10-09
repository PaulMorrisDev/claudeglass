// Tests for how the band narrows (plugin/hooks/lib/band.js).
//
//   node --test tests/js/band.test.mjs

import test from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath, pathToFileURL } from "node:url";
import { dirname, join } from "node:path";

const lib = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "plugin", "hooks", "lib");
const { narrow, SEPARATOR_CELLS } = await import(pathToFileURL(join(lib, "band.js")).href);

const context = { role: "context", key: "context", full: 50, short: 28 };
const spend = { role: "spend", key: "spend", full: 20, short: 14 };
const extra = (urgent) => ({ role: "extra", key: "extra", full: 55, short: 30, urgent });

/** The room three pieces take with the given widths. */
const room = (...widths) => widths.reduce((sum, w) => sum + w, 0) + SEPARATOR_CELLS * (widths.length - 1);

const forms = (shape) => [shape.forms.context, shape.forms.spend, shape.forms.extra];

test("everything fits at full length, and the bar fits beside it when there is room", () => {
  const pieces = [context, spend, extra(true)];
  const full = room(50, 20, 55);
  assert.deepEqual(forms(narrow(pieces, full, 16)), ["full", "full", "full"]);
  assert.equal(narrow(pieces, full, 16).bar, false);
  assert.equal(narrow(pieces, full + 16, 16).bar, true);
  assert.equal(narrow(pieces, full + 16, 0).bar, false);
});

test("an urgent extra: the spend goes short, then the context, then the spend goes, then the extra goes short", () => {
  const pieces = [context, spend, extra(true)];
  assert.deepEqual(forms(narrow(pieces, room(50, 14, 55), 16)), ["full", "short", "full"]);
  assert.deepEqual(forms(narrow(pieces, room(28, 14, 55), 16)), ["short", "short", "full"]);
  assert.deepEqual(forms(narrow(pieces, room(28, 55), 16)), ["short", "gone", "full"]);
  assert.deepEqual(forms(narrow(pieces, room(28, 30), 16)), ["short", "gone", "short"]);
  // Only the context is left, short, when even that does not fit.
  assert.deepEqual(forms(narrow(pieces, 10, 16)), ["short", "gone", "gone"]);
});

test("an urgent extra goes last: with room for the context and the extra, the spend is what leaves", () => {
  const pieces = [context, spend, extra(true)];
  for (let cells = room(28, 55); cells >= room(28, 30); cells -= 3) {
    const shape = narrow(pieces, cells, 0);
    assert.equal(shape.forms.extra === "gone", false, `columns ${cells}`);
    assert.equal(shape.forms.spend, "gone", `columns ${cells}`);
  }
});

test("an extra that is not urgent gives way before the spend does", () => {
  const pieces = [context, spend, extra(false)];
  assert.deepEqual(forms(narrow(pieces, room(28, 14, 55), 0)), ["short", "short", "full"]);
  assert.deepEqual(forms(narrow(pieces, room(28, 14, 30), 0)), ["short", "short", "short"]);
  // Dropping the extra brings the spend back.
  assert.deepEqual(forms(narrow(pieces, room(28, 14), 0)), ["short", "short", "gone"]);
  assert.deepEqual(forms(narrow(pieces, room(28), 0)), ["short", "gone", "gone"]);
});

test("a piece with no short form keeps its full width, and pieces that are missing leave no gap", () => {
  const plain = { role: "extra", key: "extra", full: 20, short: null, urgent: false };
  assert.deepEqual(forms(narrow([context, spend, plain], room(28, 14, 20), 0)), ["short", "short", "full"]);
  const two = narrow([context, extra(true)], room(50, 55), 0);
  assert.deepEqual([two.forms.context, two.forms.extra], ["full", "full"]);
  assert.equal("spend" in two.forms, false);
  assert.deepEqual(narrow([context], 5, 16), { forms: { context: "short" }, bar: false });
});

// Tests for how a value an older mod left in $.state gets the fields it lacks
// (plugin/hooks/lib/shape.js).
//
//   node --test tests/js/shape.test.mjs

import test from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath, pathToFileURL } from "node:url";
import { dirname, join } from "node:path";

const lib = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "plugin", "hooks", "lib");
const { fillMissing } = await import(pathToFileURL(join(lib, "shape.js")).href);

const defaults = () => ({ agents: [], reading: null, turn: { rebuilds: [], big: null, switched: false }, rest: { hints: {} } });

test("a value with every field comes back as it is", () => {
  const value = { agents: [{ id: "a" }], reading: { pct: 60 }, turn: { rebuilds: [], big: null, switched: true }, rest: { hints: { x: 1 } } };
  assert.equal(fillMissing(value, defaults()), value);
});

test("missing fields come from the defaults, one level down too, and the value is not changed", () => {
  const value = Object.freeze({ reading: { pct: 60 }, turn: Object.freeze({ rebuilds: [1], big: null }) });
  const filled = fillMissing(value, defaults());
  assert.deepEqual(filled, { reading: { pct: 60 }, agents: [], turn: { rebuilds: [1], big: null, switched: false }, rest: { hints: {} } });
  assert.equal(filled.reading, value.reading);
  assert.deepEqual(Object.keys(value), ["reading", "turn"]);
});

test("a field held as undefined is filled; null, arrays and other values are kept", () => {
  const filled = fillMissing({ agents: undefined, reading: { pct: 1 }, turn: null, rest: { hints: [] } }, defaults());
  assert.deepEqual(filled.agents, []);
  assert.equal(filled.turn, null);
  assert.deepEqual(filled.rest.hints, []);
});

test("anything that is not a plain object gives the defaults", () => {
  for (const value of [null, undefined, 3, "x", []]) {
    assert.deepEqual(fillMissing(value, defaults()), defaults());
  }
});

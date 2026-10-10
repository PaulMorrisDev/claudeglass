// Tests for the band's health line (plugin/hooks/lib/band.js).
//
//   node --test tests/js/band.test.mjs

import test from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath, pathToFileURL } from "node:url";
import { dirname, join } from "node:path";

const lib = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "plugin", "hooks", "lib");
const { healthOf, healthForms, healthLine, buttonCells, BUTTON_PADDING } = await import(pathToFileURL(join(lib, "band.js")).href);

const WORDS = {
  full: "{pct}% full",
  replies: "about {replies} replies before a summary",
  repliesOne: "about 1 reply before a summary",
  point: "Claude summarises at {tokens}",
  lost: "Summary lost {missing} of {total} items",
  kept: "Summary kept all {total} items",
  keptOne: "Summary kept the 1 item",
};

/** The facts of a session with no summary yet, at the given context state. */
const facts = (reading, over = {}) => ({ reading, summarised: false, missing: 0, pending: 0, seen: true, total: 0, replies: 4, repliesAt: null, ...over });

test("before the first reading there is no state", () => {
  assert.equal(healthOf(facts(null)), null);
  assert.equal(healthOf(facts(null, { summarised: true, missing: 2, pending: 2, seen: false, total: 3, repliesAt: 4 })), null);
});

test("plenty of room and filling are Fine; soon and next are Tight", () => {
  assert.equal(healthOf(facts("plenty")), "fine");
  assert.equal(healthOf(facts("filling")), "fine");
  assert.equal(healthOf(facts("soon")), "tight");
  assert.equal(healthOf(facts("next")), "tight");
});

test("a summary that lost items nobody has looked at is Review, whatever the context", () => {
  const lost = { summarised: true, missing: 3, pending: 3, seen: false, total: 9, repliesAt: 4 };
  for (const reading of ["plenty", "filling", "soon", "next"]) assert.equal(healthOf(facts(reading, lost)), "review");
  // Looked at: back to the context's own state.
  assert.equal(healthOf(facts("soon", { ...lost, seen: true })), "tight");
  assert.equal(healthOf(facts("plenty", { ...lost, seen: true })), "fine");
});

test("a summary whose lost items are all put back again is not Review", () => {
  const back = { summarised: true, missing: 3, pending: 0, seen: false, total: 9, repliesAt: 4 };
  assert.equal(healthOf(facts("plenty", back)), "fine");
  assert.equal(healthOf(facts("soon", back)), "tight");
  // Some still to put back: Review.
  assert.equal(healthOf(facts("plenty", { ...back, pending: 1 })), "review");
});

test("Kept shows until a reply ends after the summary, then it is Fine again", () => {
  const kept = { summarised: true, missing: 0, seen: true, total: 9, repliesAt: 4 };
  assert.equal(healthOf(facts("plenty", { ...kept, replies: 4 })), "kept");
  assert.equal(healthOf(facts("plenty", { ...kept, replies: 5 })), "fine");
  // The summary can come after the reply count was last read and before a request: the count, not a clock, decides.
  assert.equal(healthOf(facts("plenty", { ...kept, repliesAt: null })), "fine");
  // Nothing was checked: there is nothing to say was kept.
  assert.equal(healthOf(facts("plenty", { ...kept, total: 0 })), "fine");
  // Tight outranks Kept: the context is close to the point.
  assert.equal(healthOf(facts("soon", kept)), "tight");
});

test("Fine and Tight say how full the context is and how many replies are left", () => {
  const figures = { pct: "34", replies: 11, point: "160k", missing: 0, total: 0 };
  assert.deepEqual(healthForms("fine", figures, WORDS), ["34% full · about 11 replies before a summary", "34% full", ""]);
  assert.deepEqual(healthForms("tight", { ...figures, pct: "71", replies: 1 }, WORDS), ["71% full · about 1 reply before a summary", "71% full", ""]);
});

test("with no count of replies the line gives the summarising point instead", () => {
  const figures = { pct: "71", replies: null, point: "160k", missing: 0, total: 0 };
  assert.equal(healthForms("tight", figures, WORDS)[0], "71% full · Claude summarises at 160k");
  assert.equal(healthForms("tight", { ...figures, replies: 0 }, WORDS)[0], "71% full · Claude summarises at 160k");
});

test("Review and Kept say what the summary did with the items", () => {
  const figures = { pct: "34", replies: 11, point: "160k", missing: 3, total: 9 };
  assert.deepEqual(healthForms("review", figures, WORDS), ["Summary lost 3 of 9 items", ""]);
  assert.deepEqual(healthForms("kept", figures, WORDS), ["Summary kept all 9 items", ""]);
  assert.deepEqual(healthForms("kept", { ...figures, total: 1 }, WORDS), ["Summary kept the 1 item", ""]);
});

test("the line is the longest form that fits, and nothing when none does", () => {
  const figures = { pct: "34", replies: 11, point: "160k", missing: 0, total: 0 };
  const full = "34% full · about 11 replies before a summary";
  assert.equal(healthLine("fine", figures, WORDS, full.length), full);
  assert.equal(healthLine("fine", figures, WORDS, full.length - 1), "34% full");
  assert.equal(healthLine("fine", figures, WORDS, 8), "34% full");
  assert.equal(healthLine("fine", figures, WORDS, 7), "");
  assert.equal(healthLine("review", { ...figures, missing: 3, total: 9 }, WORDS, 10), "");
});

test("a Button takes its label and its brackets", () => {
  assert.equal(buttonCells("Glass"), 5 + BUTTON_PADDING);
});

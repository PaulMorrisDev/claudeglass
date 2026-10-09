// Tests for the mod's logic that has no Python twin: lib/context.js,
// lib/text.js, and the small parts of lib/units.js, lib/rest.js and
// lib/cache.js the shared vectors (mod_vectors.mjs) don't cover.
//
//   node --test tests/js/

import test from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { pathToFileURL } from "node:url";

const staticDir = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "src", "claudeglass", "service", "static");
const lib = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "plugin", "hooks", "lib");
const load = (name) => import(pathToFileURL(join(lib, name)).href);

const { readContext, runway, toolGrowth } = await load("context.js");
const { fill, joinParts } = await load("text.js");
const { formatTokens, formatAmount, formatAmountCell, amountText, moneySymbol } = await load("units.js");
const { noteAllowed, mayShow, stamp } = await load("rest.js");
const { cacheState, formatClock, resolveTtl, parseTtl } = await load("cache.js");
const { resolveModel, ratesFor, priceUsage } = await load("pricing.js");

// What the spike saw: a 1M model with compaction set at 300k.
function spikeContext(overrides = {}) {
  return {
    tokens: 45000,
    window: 1000000,
    percent: 4,
    breakdown: {
      maxTokens: 300000,
      rawMaxTokens: 1000000,
      percentage: 15,
      autocompactSource: "settings",
      categories: [
        { name: "System prompt", tokens: 6000, kind: "used", isDeferred: false },
        { name: "System tools", tokens: 12000, kind: "used", isDeferred: false },
        { name: "System tools (deferred)", tokens: 9000, kind: "deferred", isDeferred: true },
        { name: "Custom agents", tokens: 1000, kind: "used", isDeferred: false },
        { name: "Memory files", tokens: 3000, kind: "used", isDeferred: false },
        { name: "Skills", tokens: 2000, kind: "used", isDeferred: false },
        { name: "Messages", tokens: 21000, kind: "used", isDeferred: false },
        { name: "Autocompact buffer", tokens: 33000, kind: "buffer", isDeferred: false },
        { name: "Free space", tokens: 213000, kind: "free", isDeferred: false },
      ],
      ...overrides,
    },
  };
}

test("readContext shows /context's own percentage and the user's compaction point", () => {
  const reading = readContext(spikeContext());
  assert.equal(reading.pct, 15);
  assert.equal(reading.tokens, 45000);
  assert.equal(reading.point, 267000);
  assert.equal(reading.window, 1000000);
  assert.equal(reading.state, "plenty");
  assert.deepEqual(reading.parts, { startup: 24000, conversation: 21000, free: 213000 });
});

test("readContext prefers the threshold Claude Code names over the buffer", () => {
  const reading = readContext(spikeContext({ autoCompactThreshold: 250000 }));
  assert.equal(reading.point, 250000);
});

test("readContext counts MCP tools in the startup context but not deferred tools", () => {
  const context = spikeContext();
  context.breakdown.categories.push({ name: "MCP tools", tokens: 4000, kind: "used" });
  context.breakdown.categories.push({ name: "MCP tools (deferred)", tokens: 7000, kind: "used", isDeferred: true });
  assert.equal(readContext(context).parts.startup, 28000);
});

test("readContext works out the percentage itself when the breakdown has none", () => {
  const context = spikeContext();
  delete context.breakdown.percentage;
  assert.equal(readContext(context).pct, 15);
});

test("readContext falls back to tokens and the window without a breakdown", () => {
  const reading = readContext({ tokens: 100000, window: 200000, percent: 50 });
  assert.equal(reading.pct, 50);
  assert.equal(reading.tokens, 100000);
  assert.equal(reading.point, 200000);
  assert.equal(reading.state, "filling");
  assert.deepEqual(reading.parts, { startup: null, conversation: null, free: null });
});

test("readContext survives nothing at all", () => {
  for (const input of [undefined, null, {}, { breakdown: {} }, { breakdown: { categories: [] } }]) {
    const reading = readContext(input);
    assert.equal(reading.tokens, 0);
    assert.equal(reading.pct, 0);
    assert.equal(reading.state, "plenty");
    assert.deepEqual(reading.parts, { startup: null, conversation: null, free: null });
  }
});

test("readContext names the state by how far to the compaction point", () => {
  const stateAt = (tokens) => readContext({ tokens, window: 100000 }).state;
  assert.equal(stateAt(49999), "plenty");
  assert.equal(stateAt(50000), "filling");
  assert.equal(stateAt(74999), "filling");
  assert.equal(stateAt(75000), "soon");
  assert.equal(stateAt(89999), "soon");
  assert.equal(stateAt(90000), "next");
  assert.equal(stateAt(150000), "next");
});

test("readContext measures the state against the compaction point, not the window", () => {
  // 210k is 21% of the 1M window but 79% of the way to compaction at 267k.
  assert.equal(readContext({ ...spikeContext(), tokens: 210000 }).state, "soon");
  assert.equal(readContext(spikeContext()).state, "plenty");
});

test("readContext takes the tokens from the categories when the usage has none", () => {
  const context = spikeContext();
  delete context.tokens;
  assert.equal(readContext(context).tokens, 45000);
});

test("runway is the headroom over the mean growth of the latest growing steps", () => {
  // Steps 10k each: 70k to go at 10k a reply.
  assert.equal(runway([10000, 20000, 30000, 40000], 110000), 7);
  // Floors.
  assert.equal(runway([0, 10000, 20000], 35000), 1);
});

test("runway uses only the last five growing steps", () => {
  const history = [0, 50000, 51000, 52000, 53000, 54000, 55000];
  // Steps: 50k, then five of 1k. The last five are the 1k steps.
  assert.equal(runway(history, 65000), 10);
});

test("runway leaves drops out: a compaction or /clear is not growth", () => {
  const history = [10000, 20000, 5000, 15000, 25000];
  // Growing steps 10k, 10k, 10k; the drop to 5k is skipped.
  assert.equal(runway(history, 55000), 3);
});

test("runway is null until two steps have grown", () => {
  assert.equal(runway([], 100000), null);
  assert.equal(runway([10000], 100000), null);
  assert.equal(runway([10000, 20000], 100000), null);
  assert.equal(runway([30000, 20000, 25000], 100000), null);
  assert.equal(runway([10000, 20000, 30000], NaN), null);
});

test("runway is never below zero past the point", () => {
  assert.equal(runway([10000, 20000, 30000], 25000), 0);
});

test("toolGrowth is what the tools added after the model's own output", () => {
  assert.equal(toolGrowth(100000, 2000, 112000), 10000);
  assert.equal(toolGrowth(100000, 2000, 101000), 0);
  assert.equal(toolGrowth(100000, 2000, 90000), 0);
});

test("fill replaces placeholders and leaves unknown ones visible", () => {
  assert.equal(fill("{pct} full · {left} replies", { pct: "60%", left: 6 }), "60% full · 6 replies");
  assert.equal(fill("{known} and {unknown}", { known: "yes" }), "yes and {unknown}");
  assert.equal(fill("{a}{a}", { a: "x" }), "xx");
  assert.equal(fill("no placeholders", { a: "x" }), "no placeholders");
  assert.equal(fill("{n}", { n: 0 }), "0");
  assert.equal(fill("{n}", { n: null }), "{n}");
  assert.equal(fill("{toString}", {}), "{toString}");
});

test("fill does not scan inserted values again", () => {
  assert.equal(fill("{a} {b}", { a: "{b}", b: "x" }), "{b} x");
});

test("joinParts skips empty parts and uses the middle dot", () => {
  assert.equal(joinParts(["60% full", "", "$0.38"]), "60% full · $0.38");
  assert.equal(joinParts(["a", null, undefined, false, "b"], " | "), "a | b");
  assert.equal(joinParts([]), "");
  assert.equal(joinParts(["", ""]), "");
  assert.equal(joinParts([0, "x"]), "0 · x");
});

test("formatTokens writes 3 significant figures with a lower-case k", () => {
  assert.equal(formatTokens(0), "0");
  assert.equal(formatTokens(999), "999");
  assert.equal(formatTokens(1000), "1k");
  assert.equal(formatTokens(1234), "1.23k");
  assert.equal(formatTokens(12400), "12.4k");
  assert.equal(formatTokens(178000), "178k");
  assert.equal(formatTokens(999999), "1M");
  assert.equal(formatTokens(1240000), "1.24M");
  assert.equal(formatTokens(-12400), "−12.4k");
  assert.equal(formatTokens(null), "-");
  assert.equal(formatTokens(NaN), "-");
});

test("formatAmount never returns an empty string", () => {
  const api = { mode: "api" };
  assert.equal(formatAmount(0, api), "0.00 USD");
  assert.equal(formatAmount(NaN, api), "0.00 USD");
  assert.equal(formatAmount(Infinity, api), "0.00 USD");
  assert.equal(formatAmount(-1.5, api), "-1.50 USD");
  assert.equal(formatAmountCell(0, api), "none");
  assert.equal(formatAmountCell(NaN, api), "none");
});

test("formatAmount phrases a plan's amounts without a bare dollar sign", () => {
  const plan = { mode: "subscription", share_per_usd: 0.5 };
  assert.equal(formatAmount(0.8, plan), "about 0.40% of your weekly usage limit");
  assert.equal(amountText(2, plan), "about 1.0% of your weekly usage limit (2.00 USD list-price equivalent)");
  assert.equal(formatAmount(0.8, { mode: "subscription", share_per_usd: null }), "0.80 USD list-price equivalent");
  assert.equal(formatAmount(1000, { mode: "subscription", share_per_usd: 0.5 }), "about 5.0 weeks' worth of your usage limit");
});

test("formatAmount can write the dashboard's dollar sign when asked", () => {
  const api = { mode: "api" };
  assert.equal(formatAmount(0.38, api, { symbol: true }), "$0.38");
  assert.equal(formatAmount(45.67, api, { symbol: true }), "$45.7");
  assert.equal(formatAmount(1234.5, api, { symbol: true }), "$1,235");
  assert.equal(formatAmount(0.001, api, { symbol: true }), "<$0.01");
  assert.equal(formatAmount(5, { mode: "api", currency: "EUR" }, { symbol: true }), "5.00 EUR");
});

// The dashboard's own formatting (service/static/format.js), imported as the
// browser runs it, is what the symbol form has to match character for
// character. format.js needs no DOM to load.
const dashboard = await import(pathToFileURL(join(staticDir, "format.js")).href);
const dashboardState = (await import(pathToFileURL(join(staticDir, "core.js")).href)).state;

test("the dollar form is what the dashboard writes, for the same amounts", () => {
  const amounts = [
    0, 0.001, 0.0049, 0.005, 0.0051, 0.01, 0.015, 0.125, 0.375, 0.995, 1.005, 1.015, 2.675, 9.995, 9.9949, 10, 10.05, 12.345,
    45.65, 45.67, 99.95, 99.949, 100, 100.5, 999.5, 1234.5, 1234567.891, 0.1 + 0.2,
  ];
  for (const currency of ["USD", "EUR"]) {
    dashboardState.currency = currency;
    for (const amount of amounts.flatMap((a) => [a, -a])) {
      assert.equal(moneySymbol(amount, currency), dashboard.currencyAmount(amount), `${amount} ${currency}`);
    }
  }
  dashboardState.currency = "USD";
});

test("the dollar form rounds a tie up, as toLocaleString does, and puts a tiny negative's sign where the dashboard does", () => {
  assert.equal(formatAmount(1.005, { mode: "api" }, { symbol: true }), "$1.01");
  assert.equal(formatAmount(2.675, { mode: "api" }, { symbol: true }), "$2.68");
  assert.equal(moneySymbol(-0.001, "USD"), "<$−0.01");
  assert.equal(moneySymbol(-3.2, "USD"), "−$3.20");
});

test("noteAllowed keeps notes at least five replies apart", () => {
  assert.equal(noteAllowed(null, 0), true);
  assert.equal(noteAllowed(undefined, 3), true);
  assert.equal(noteAllowed(10, 11), false);
  assert.equal(noteAllowed(10, 14), false);
  assert.equal(noteAllowed(10, 15), true);
  assert.equal(noteAllowed(10, 12, 2), true);
  assert.equal(noteAllowed(10, 11, 2), false);
});

test("stamp does not change the state it is given", () => {
  const th = { cooldown_minutes: 30, rearm_factor: 1.5, max_backoff: 3 };
  const before = { hints: { a: { ts: 1, stake: 5, n: 1, rest: 1800 } } };
  const frozen = JSON.stringify(before);
  const after = stamp(before, "a", 9, 100, th);
  assert.equal(JSON.stringify(before), frozen);
  assert.equal(after.hints.a.n, 2);
  assert.equal(after.hints.a.rest, 3600);
  assert.equal(mayShow(after, "a", 9, 100, th), false);
  assert.equal(mayShow(after, "a", 14, 100, th), true);
});

test("cacheState counts down and goes cold", () => {
  const state = cacheState(1000, 1000 + 75000, 300);
  assert.equal(state.isWarm, true);
  assert.equal(state.remainingMs, 225000);
  assert.equal(state.fractionLeft, 0.75);
  assert.equal(formatClock(state.remainingMs), "03:45");
  assert.deepEqual(cacheState(1000, 1000 + 300000, 300), { isWarm: false, remainingMs: 0, fractionLeft: 0 });
  assert.deepEqual(cacheState(null, 5, 300), { isWarm: false, remainingMs: 0, fractionLeft: 0 });
  // A reply stamped ahead of this clock never shows more than a full lifetime.
  assert.equal(cacheState(10000, 0, 300).remainingMs, 300000);
});

test("resolveTtl and parseTtl read what the status line reads", () => {
  assert.equal(parseTtl("1h"), 3600);
  assert.equal(parseTtl(true), null);
  assert.equal(resolveTtl({ prompt_cache: { cache_ttl: "1h" } }, 300), 3600);
  assert.equal(resolveTtl({}, "1h"), 3600);
  assert.equal(resolveTtl(null, null), 300);
});

test("pricing resolves a settings-file rate card", () => {
  const models = {
    "claude-sonnet-5-5": { aliases: ["sonnet"], input: 3, output: 15, cache_write_5m: 3.75, cache_write_1h: 6, cache_read: 0.3 },
    "claude-sonnet-5": { aliases: [], input: 3, output: 15, cache_write_5m: 3.75, cache_write_1h: 6, cache_read: 0.3 },
  };
  assert.equal(resolveModel(models, "sonnet"), "claude-sonnet-5-5");
  assert.equal(resolveModel(models, "claude-sonnet-5-5[1m]"), "claude-sonnet-5-5");
  assert.equal(resolveModel(models, "claude-sonnet-5-20261001"), "claude-sonnet-5");
  assert.equal(resolveModel(models, "claude-sonnet-5-10"), "claude-sonnet-5");
  assert.equal(resolveModel(models, "<synthetic>"), null);
  assert.equal(resolveModel(models, "__proto__"), null);
  assert.equal(resolveModel(null, "sonnet"), null);
  assert.equal(ratesFor(models, "nope"), null);
  const cost = priceUsage(models["claude-sonnet-5-5"], { input_tokens: 1000000, cache_creation_input_tokens: 1000000 }, 3600);
  assert.equal(cost, 9);
  assert.equal(priceUsage(null, { input_tokens: 5 }, 300), 0);
});

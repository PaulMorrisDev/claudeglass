// Shared vectors for the mod's pure logic (plugin/hooks/lib/*.js).
//
// tests/test_mod_vectors.py calls the real Python functions on a set of
// cases and writes {fn, args, expect} for each to a JSON file; this runs
// the same cases through the JavaScript ports and prints every mismatch.
//
//   node tests/js/mod_vectors.mjs <vectors.json>
//
// Exit code 0 when every vector agrees, 1 when any differs, 2 on a usage
// error.

import { readFileSync } from "node:fs";
import { fileURLToPath, pathToFileURL } from "node:url";
import { dirname, join } from "node:path";

const lib = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "plugin", "hooks", "lib");
const load = (name) => import(pathToFileURL(join(lib, name)).href);

const pricing = await load("pricing.js");
const cache = await load("cache.js");
const rebuild = await load("rebuild.js");
const units = await load("units.js");
const rest = await load("rest.js");

// Floats agree to a billionth, relatively; everything else exactly.
function same(a, b) {
  if (typeof a === "number" && typeof b === "number") {
    return a === b || Math.abs(a - b) <= 1e-9 * Math.max(1, Math.abs(a), Math.abs(b));
  }
  if (Array.isArray(a) && Array.isArray(b)) return a.length === b.length && a.every((x, i) => same(x, b[i]));
  if (a && b && typeof a === "object" && typeof b === "object") {
    const ka = Object.keys(a).sort();
    const kb = Object.keys(b).sort();
    return ka.length === kb.length && ka.every((k, i) => k === kb[i] && same(a[k], b[k]));
  }
  return a === b;
}

// The countdown line the status line writes, from the JS pieces.
function cacheEstimate(lastMs, nowMs, ttl) {
  const state = cache.cacheState(lastMs, nowMs, ttl);
  return `cache est ${cache.ttlLabel(ttl)} ` + (state.isWarm ? cache.formatClock(state.remainingMs) : "expired");
}

// Replays a run of rest-rule steps; every step reports what it returned
// and the hint table after it.
function restSequence(th, steps) {
  let state = { hints: {} };
  return steps.map((step) => {
    const out = {};
    if (step.op === "may") {
      out.may = rest.mayShow(state, step.kind, step.stake, step.now, th);
    } else if (step.op === "stamp") {
      state = rest.stamp(state, step.kind, step.stake, step.now, th, step.rest ?? null, !!step.once);
    } else if (step.op === "beside") {
      state = rest.restBeside(state, step.kind, step.now, th);
    } else if (step.op === "show") {
      out.shown = rest.mayShow(state, step.kind, step.stake, step.now, th);
      if (out.shown) state = rest.recordShown(state, step.kind, step.stake, step.now, th, !!step.once);
    } else {
      throw new Error(`unknown rest op: ${step.op}`);
    }
    out.hints = state.hints;
    return out;
  });
}

const functions = {
  resolveModel: ([models, id]) => pricing.resolveModel(models, id),
  priceUsage: ([rates, usage, ttl]) => pricing.priceUsage(rates, usage, ttl),
  sendCost: ([rates, tokens, warm, ttl]) => pricing.sendCost(rates, tokens, warm, ttl),
  // The status line works in float epoch seconds, good to about a ten-thousandth
  // of a millisecond, so both sides are rounded past that noise.
  cacheState: ([last, now, ttl]) => {
    const state = cache.cacheState(last, now, ttl);
    return { ...state, remainingMs: +state.remainingMs.toFixed(3), fractionLeft: +state.fractionLeft.toFixed(7) };
  },
  cacheEstimate: ([last, now, ttl]) => cacheEstimate(last, now, ttl),
  formatClock: ([ms]) => cache.formatClock(ms),
  parseTtl: ([value]) => cache.parseTtl(value),
  resolveTtl: ([payload, configDefault]) => cache.resolveTtl(payload, configDefault),
  detectRebuild: ([prev, cur, th, ttl, cr0]) => rebuild.detectRebuild(prev, cur, th, ttl, cr0),
  writeTtlSeconds: ([request, ttl]) => rebuild.writeTtlSeconds(request, ttl),
  expiredByClock: ([gap, ttl]) => rebuild.expiredByClock(gap, ttl),
  amountParts: ([usd, billing, opts]) => units.amountParts(usd, billing, opts),
  formatAmount: ([usd, billing, opts]) => units.formatAmount(usd, billing, opts),
  amountText: ([usd, billing, opts]) => units.amountText(usd, billing, opts),
  formatAmountCell: ([usd, billing, opts]) => units.formatAmountCell(usd, billing, opts),
  restSequence: ([th, steps]) => restSequence(th, steps),
};

const path = process.argv[2];
if (!path) {
  console.error("usage: node mod_vectors.mjs <vectors.json>");
  process.exit(2);
}
const vectors = JSON.parse(readFileSync(path, "utf8"));

let failures = 0;
const counts = {};
const failed = {};
for (const [i, vector] of vectors.entries()) {
  const fn = functions[vector.fn];
  if (!fn) {
    console.log(`#${i} ${vector.fn}: no such function`);
    failures += 1;
    failed[vector.fn] = (failed[vector.fn] || 0) + 1;
    continue;
  }
  counts[vector.fn] = (counts[vector.fn] || 0) + 1;
  let got;
  try {
    // Through JSON, so undefined fields drop away as they do in the Python side.
    got = JSON.parse(JSON.stringify(fn(vector.args)) ?? "null");
  } catch (error) {
    got = `threw ${error && error.message}`;
  }
  if (!same(got, vector.expect)) {
    failures += 1;
    failed[vector.fn] = (failed[vector.fn] || 0) + 1;
    if (failures <= 40) {
      console.log(`MISMATCH #${i} ${vector.fn}${vector.note ? ` (${vector.note})` : ""}`);
      console.log(`  args:     ${JSON.stringify(vector.args)}`);
      console.log(`  python:   ${JSON.stringify(vector.expect)}`);
      console.log(`  javascript: ${JSON.stringify(got)}`);
    }
  }
}

console.log(`${vectors.length - failures} of ${vectors.length} vectors agree`);
// The last line, for the pytest wrapper: how many ran and how many differed, by function.
console.log(`SUMMARY ${JSON.stringify({ counts, failed })}`);
process.exit(failures ? 1 : 0);

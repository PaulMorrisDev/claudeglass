// Tests for the compaction review's coverage check and drafts
// (plugin/hooks/lib/coverage.js).
//
//   node --test tests/js/coverage.test.mjs

import test from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath, pathToFileURL } from "node:url";
import { dirname, join } from "node:path";

import { planted } from "./planted.mjs";

const root = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "plugin", "hooks");
const load = (path) => import(pathToFileURL(join(root, path)).href);
const { buildKeepList } = await load("lib/keeplist.js");
const { checkCoverage, markItem, prepareSummary, compactKeep, addBackNote, missingItems, shouldHold, judgePrompt, parseJudge, tally, THRESHOLDS, KEEP_MAX, MARK_SYMBOL } =
  await load("lib/coverage.js");
const { COPY } = await load("generated/copy.js");

/** A summary that keeps the decision, the files and the failing test but drops the port rule and "don't read code yet". */
const SUMMARY = `This session is being continued from a previous conversation.
Summary: The user asked for rate limiting on the API and chose option B, the queue-based design.
The team uses pnpm, not npm. Files changed: src/api/limiter.ts, src/api/queue.ts, tests/test_limiter.py, docs/limits.md, src/config.ts and notes/try.ipynb.
Open work: add the burst test and document the header. The last test run failed: pytest tests/test_limiter.py reported test_burst failing, expected 429.`;

const byText = (items, pattern) => items.find((item) => pattern.test(item.text));

test("a summary that drops two constraints: those two are missing, the rest found or safe", () => {
  const items = buildKeepList(planted());
  const coverage = checkCoverage(items, SUMMARY);

  const port = byText(items, /port 8765/);
  const code = byText(items, /read any code/);
  assert.equal(coverage.marks[port.id], "missing");
  assert.equal(coverage.marks[code.id], "missing");

  const decision = byText(items, /option B/);
  assert.equal(coverage.marks[decision.id], "found");
  const pnpm = byText(items, /pnpm/);
  assert.equal(coverage.marks[pnpm.id], "found", "'not npm' is in the summary, so the prohibition is kept");
  for (const task of items.filter((item) => item.kind === "task")) assert.equal(coverage.marks[task.id], "found", task.text);
  const [error] = items.filter((item) => item.kind === "error");
  assert.equal(coverage.marks[error.id], "found");

  assert.deepEqual(
    missingItems(items, coverage.marks).map((item) => item.id).sort(),
    [port.id, code.id].sort(),
  );
  assert.equal(coverage.missing, 2);
  // Safe files are not counted either way.
  assert.equal(coverage.counts.safe, 5);
  assert.equal(coverage.total, coverage.counts.found + coverage.counts.partly + coverage.counts.missing);
  assert.equal(coverage.kept, coverage.counts.found + coverage.counts.partly);
  assert.ok(Object.values(MARK_SYMBOL).every((glyph) => glyph.length >= 1));
});

test("paths, ports and numbers need an exact match", () => {
  const items = buildKeepList([
    { role: "user", text: "The API is at https://staging.example.com/v2 and listens on port 8080." },
    { role: "assistant", text: "", toolUses: [{ tool: "Edit", input: { file_path: "C:\\work\\app\\server.ts" }, text: "ok" }] },
  ]);
  const url = items.find((item) => item.exact[0] === "https://staging.example.com/v2");
  const port = items.find((item) => item.exact[0] === "8080");
  const file = items.find((item) => item.kind === "file");
  assert.ok(url && port && file);
  const mark = (item, text) => markItem(item, prepareSummary(text));
  assert.equal(mark(url, "Uses https://staging.example.com/v2 for the API."), "found");
  assert.equal(mark(url, "Uses the staging API."), "missing");
  assert.equal(mark(port, "Listens on port 8080."), "found");
  assert.equal(mark(port, "Listens on port 18080."), "missing", "a number inside a longer number is not a match");
  assert.equal(mark(port, "Listens on some port."), "missing");
  // A safe file reads as safe whatever the summary says; an unsafe one matches by its file name.
  assert.equal(mark(file, "anything"), "safe");
  const old = { ...file, safe: false, why: "" };
  assert.equal(mark(old, "Edited server.ts."), "found");
  assert.equal(mark(old, "Edited the server."), "missing");
});

test("key-word overlap: found, partly and missing at the documented thresholds", () => {
  assert.deepEqual(THRESHOLDS, { found: 0.6, partly: 0.3 });
  const item = { id: "i1", kind: "instruction", text: "Keep tests fast and deterministic and isolated and hermetic", safe: false, why: "", exact: [], at: 0 };
  const mark = (text) => markItem(item, prepareSummary(text));
  // words: keep, test, fast, deterministic, isolated, hermetic (6)
  assert.equal(mark("Keep tests fast, deterministic and isolated."), "found"); // 5/6
  assert.equal(mark("Tests should be fast and keep going."), "partly"); // 3/6 = 0.5
  assert.equal(mark("Tests."), "missing"); // 1/6
});

test("a prohibition the summary loses counts as missing even when its subject is kept", () => {
  const item = { id: "i2", kind: "instruction", text: "Never use port 8765", safe: false, why: "", exact: ["8765"], at: 0 };
  assert.equal(markItem(item, prepareSummary("The dev server runs on port 8765 and we use it.")), "missing");
  assert.equal(markItem(item, prepareSummary("Do not use port 8765, it is reserved.")), "found");
  assert.equal(markItem(item, prepareSummary("Avoid port 8765 for the server.")), "found");
  // A negation far from the subject does not count.
  const far = "Port 8765 is used here. " + "x ".repeat(120) + " nothing else, never mind.";
  assert.equal(markItem(item, prepareSummary(far)), "missing");
});

test("a leading 'No,' correction is not itself a prohibition", () => {
  const item = { id: "i3", kind: "instruction", text: "No, call it the staging database.", safe: false, why: "", exact: [], at: 0 };
  assert.equal(markItem(item, prepareSummary("Call it the staging database.")), "found");
});

test("compactKeep: the command text, bounded at 1,500 characters", () => {
  const items = buildKeepList(planted(), { pins: [{ text: "rotate the staging key before friday" }] });
  const unsafe = items.filter((item) => !item.safe);
  const text = compactKeep(unsafe, COPY["compaction.keep"], KEEP_MAX, { files: COPY["compaction.label.files"], values: COPY["compaction.label.values"] });
  assert.ok(text.startsWith("/compact Keep: "));
  assert.match(text, /rotate the staging key before friday/);
  assert.match(text, /Never use port 8765/);
  assert.ok(text.length <= KEEP_MAX);
  assert.ok(text.indexOf("rotate the staging key") < text.indexOf("Never use port"), "pins come first");

  const many = Array.from({ length: 80 }, (_, i) => ({ id: `x${i}`, kind: "instruction", text: `Never touch module number ${i} in the billing service, it is frozen`, safe: false, why: "", exact: [], at: i }));
  const bounded = compactKeep(many, COPY["compaction.keep"]);
  assert.ok(bounded.length <= 1500 && bounded.length > 1300, String(bounded.length));
  assert.ok(!/;\s*Never touch module number \d+ in the billing serv$/.test(bounded), "no item is cut in the middle");
  assert.ok(bounded.endsWith("frozen"));

  const one = [{ ...many[0], text: "word ".repeat(400) }];
  assert.ok(compactKeep(one, COPY["compaction.keep"], 200).length <= 200);
  assert.equal(compactKeep([], COPY["compaction.keep"]), "");
});

test("addBackNote: the draft that puts the missing items back", () => {
  const items = buildKeepList(planted());
  const missing = items.filter((item) => /port 8765|read any code/.test(item.text));
  const note = addBackNote(missing, COPY["compaction.addback"]);
  assert.equal(note, "Before compaction we agreed: Never use port 8765 for the dev server, it is reserved; Don't read any code yet; wait for the other session to finish.");
  assert.equal(addBackNote([], COPY["compaction.addback"]), "");
  // Files and values are gathered into one clause each.
  const stuff = buildKeepList([
    { role: "user", text: "Staging runs on port 8080." },
    { role: "assistant", text: "", toolUses: [{ tool: "Edit", input: { file_path: "a.ts" }, text: "ok" }, { tool: "Edit", input: { file_path: "b.ts" }, text: "ok" }] },
  ]);
  const gathered = addBackNote(stuff, COPY["compaction.addback"], { files: COPY["compaction.label.files"], values: COPY["compaction.label.values"] });
  assert.match(gathered, /files we changed: (?:a\.ts, b\.ts|b\.ts, a\.ts)/);
  assert.match(gathered, /values: .*8080/);
});

test("the hold rule: once, with 100k of headroom, never twice in a row, never on a 200k window", () => {
  const base = { enabled: true, trigger: "auto", window: 1000000, point: 300000, heldLast: false };
  assert.equal(shouldHold(base), true);
  assert.equal(shouldHold({ ...base, heldLast: true }), false, "never twice in a row");
  assert.equal(shouldHold({ ...base, enabled: false }), false, "off by default");
  assert.equal(shouldHold({ ...base, trigger: "manual" }), false);
  assert.equal(shouldHold({ ...base, window: 200000, point: 160000 }), false, "a 200k window has no headroom");
  assert.equal(shouldHold({ ...base, window: 400000, point: 300000 }), true, "exactly 100k of headroom holds");
  assert.equal(shouldHold({ ...base, window: 399999, point: 300000 }), false);
  assert.equal(shouldHold({ ...base, window: null, point: 300000 }), false);
});

test("the Haiku judge: a prompt with the list and a parser for its answer", () => {
  const items = buildKeepList(planted()).filter((item) => item.kind === "instruction");
  const prompt = judgePrompt(items, SUMMARY);
  assert.match(prompt, /\d\. Never use port 8765/);
  assert.match(prompt, /SUMMARY/);
  const answer = items.map((_, i) => `${i + 1}: ${i === 0 ? "no" : i === 1 ? "partly" : "yes"}`).join("\n");
  const marks = parseJudge(answer + "\n99: yes\nnoise", items);
  assert.equal(marks[items[0].id], "missing");
  assert.equal(marks[items[1].id], "partly");
  assert.equal(marks[items[2].id], "found");
  const summary = tally(items, { ...checkCoverage(items, SUMMARY).marks, ...marks });
  assert.equal(summary.counts.missing, 1);
});

// Tests for the Agents feature's pure logic (plugin/hooks/lib/agents.js).
//
//   node --test tests/js/agents.test.mjs
//
// tests/test_mod_agents.py runs this file with AGENT_VECTORS set to a JSON
// file of cases built by calling capture_hook._report_hint and _split_hint,
// so the two notes are held to the Python they port.

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath, pathToFileURL } from "node:url";
import { dirname, join } from "node:path";

const lib = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "plugin", "hooks", "lib");
const agents = await import(pathToFileURL(join(lib, "agents.js")).href);
const {
  SKIP_AGENT_TYPES,
  activityOf,
  tierOf,
  shortModel,
  agentLabel,
  sortAgents,
  agentShare,
  kText,
  reportReread,
  splitCounts,
  newSplit,
  addSplit,
  splitRun,
} = agents;

const TH = { report_reread_tokens: 150000, split_brief_chars: 6000 };
const WHY = {
  compaction: "an agent run had to summarise its own context part-way through",
  brief: "an agent run began from a brief of {brief_chars} characters or more",
  both: "agent runs had to summarise their own context part-way through or began from a brief of {brief_chars} characters or more",
};

test("activityOf names what the last tool does in the dashboard's words", () => {
  assert.equal(activityOf("Grep"), "searching");
  assert.equal(activityOf("Glob"), "searching");
  assert.equal(activityOf("Read"), "reading");
  assert.equal(activityOf("Edit"), "editing");
  assert.equal(activityOf("MultiEdit"), "editing");
  assert.equal(activityOf("Write"), "editing");
  assert.equal(activityOf("Bash"), "running");
  assert.equal(activityOf("PowerShell"), "running");
  assert.equal(activityOf("WebSearch"), "fetching");
  assert.equal(activityOf("WebFetch"), "fetching");
  assert.equal(activityOf("mcp__x__y"), "working");
  assert.equal(activityOf(null), "working");
  assert.equal(activityOf(undefined), "working");
});

test("tierOf counts Fable with Opus and knows no other model", () => {
  assert.equal(tierOf("claude-opus-4-6"), "opus");
  assert.equal(tierOf("claude-fable-5"), "opus");
  assert.equal(tierOf("claude-sonnet-4-6"), "sonnet");
  assert.equal(tierOf("claude-haiku-4-5"), "haiku");
  assert.equal(tierOf("gpt-x"), "other");
  assert.equal(tierOf(""), "other");
  assert.equal(tierOf(null), "other");
});

test("shortModel gives the family, or a cut id for a model of no family", () => {
  assert.equal(shortModel("claude-haiku-4-5"), "haiku");
  assert.equal(shortModel("us.anthropic.claude-sonnet-4-6-v1:0"), "sonnet");
  assert.equal(shortModel("claude-opus-4-6"), "opus");
  assert.equal(shortModel("claude-fable-5"), "fable");
  assert.equal(shortModel("some-local-model-name"), "some-local");
  assert.equal(shortModel("claude-x"), "x");
  assert.equal(shortModel(""), "");
  assert.equal(shortModel(undefined), "");
});

test("agentLabel prefers the type, then the description, and cuts with an ellipsis", () => {
  assert.equal(agentLabel("Explore", "Find callers"), "Explore");
  assert.equal(agentLabel("", "Find callers"), "Find callers");
  assert.equal(agentLabel("", ""), "agent");
  assert.equal(agentLabel(null, undefined), "agent");
  assert.equal(agentLabel("  general-purpose  ", "x"), "general-purpose");
  const cut = agentLabel("a-very-long-agent-type-name-indeed", "");
  assert.equal(cut.length, 22);
  assert.ok(cut.endsWith("…"));
  assert.equal(agentLabel("abcdef", "", 4), "abc…");
});

test("sortAgents puts the running first by start, then the finished, newest end first", () => {
  const rows = [
    { id: "d1", status: "done", startedAt: 1, endedAt: 10 },
    { id: "r2", status: "running", startedAt: 5, endedAt: null },
    { id: "f1", status: "failed", startedAt: 2, endedAt: 30 },
    { id: "r1", status: "running", startedAt: 3, endedAt: null },
    { id: "d2", status: "done", startedAt: 4, endedAt: 20 },
  ];
  assert.deepEqual(sortAgents(rows).map((row) => row.id), ["r1", "r2", "f1", "d2", "d1"]);
  // The input is left as it was.
  assert.deepEqual(rows.map((row) => row.id), ["d1", "r2", "f1", "r1", "d2"]);
});

test("agentShare is a whole percent, 0 while the session's cost is unknown, never over 100", () => {
  assert.equal(agentShare(0.25, 1), 25);
  assert.equal(agentShare(0.004, 1), 0);
  assert.equal(agentShare(0.006, 1), 1);
  assert.equal(agentShare(2, 1), 100);
  assert.equal(agentShare(1, 0), 0);
  assert.equal(agentShare(1, null), 0);
  assert.equal(agentShare(1, Number.NaN), 0);
  assert.equal(agentShare(-1, 1), 0);
});

test("kText rounds half to even, as Python's round() does, and groups thousands", () => {
  assert.equal(kText(150000), "150k");
  assert.equal(kText(150499), "150k");
  assert.equal(kText(150500), "150k");
  assert.equal(kText(151500), "152k");
  assert.equal(kText(152500), "152k");
  assert.equal(kText(152501), "153k");
  assert.equal(kText(1234567), "1,235k");
  assert.equal(kText(0), "0k");
});

test("reportReread needs a background start over the threshold and a known context", () => {
  const found = reportReread({ background: true, ctx: 158000, th: TH });
  assert.deepEqual(found, { kind: "report_reread", stake: 158000, fields: { ctx: "158k" } });
  assert.equal(reportReread({ background: false, ctx: 158000, th: TH }), null);
  assert.equal(reportReread({ background: true, ctx: 149999, th: TH }), null);
  assert.equal(reportReread({ background: true, ctx: 150000, th: TH })?.stake, 150000);
  assert.equal(reportReread({ background: true, ctx: null, th: TH }), null);
  assert.equal(reportReread({ background: true, ctx: Number.NaN, th: TH }), null);
});

test("splitCounts counts a run that summarised itself or began from a long brief, except a skipped type", () => {
  const base = { type: "Explore", compacted: false, briefChars: 100, th: TH };
  assert.deepEqual(splitCounts(base), { compaction: 0, brief: 0 });
  assert.deepEqual(splitCounts({ ...base, compacted: true }), { compaction: 1, brief: 0 });
  assert.deepEqual(splitCounts({ ...base, briefChars: 6000 }), { compaction: 0, brief: 1 });
  assert.deepEqual(splitCounts({ ...base, briefChars: 5999 }), { compaction: 0, brief: 0 });
  assert.deepEqual(splitCounts({ ...base, compacted: true, briefChars: 9000 }), { compaction: 1, brief: 1 });
  for (const type of SKIP_AGENT_TYPES) assert.deepEqual(splitCounts({ ...base, type, compacted: true, briefChars: 9000 }), { compaction: 0, brief: 0 });
  assert.deepEqual(splitCounts({ ...base, type: "mine", compacted: true, skip: ["mine"] }), { compaction: 0, brief: 0 });
});

test("addSplit adds counts without changing its inputs", () => {
  const total = newSplit();
  const next = addSplit(addSplit(total, { compaction: 1, brief: 0 }), { compaction: 0, brief: 2 });
  assert.deepEqual(next, { compaction: 1, brief: 2 });
  assert.deepEqual(total, { compaction: 0, brief: 0 });
});

test("splitRun words the reason by which counts are there, and says nothing for none", () => {
  assert.equal(splitRun({ compaction: 0, brief: 0, th: TH, why: WHY }), null);
  assert.equal(splitRun({ compaction: 2, brief: 0, th: TH, why: WHY })?.fields.why, WHY.compaction);
  assert.equal(splitRun({ compaction: 0, brief: 1, th: TH, why: WHY })?.fields.why, "an agent run began from a brief of 6,000 characters or more");
  const both = splitRun({ compaction: 1, brief: 2, th: { split_brief_chars: 12500 }, why: WHY });
  assert.equal(both?.stake, 3);
  assert.equal(both?.kind, "split_run");
  assert.match(both?.fields.why ?? "", /or began from a brief of 12,500 characters or more$/);
});

test("the notes match the Python they port, case by case", { skip: process.env.AGENT_VECTORS ? false : "AGENT_VECTORS isn't set (test_mod_agents.py sets it)" }, () => {
  const vectors = JSON.parse(readFileSync(process.env.AGENT_VECTORS, "utf8"));
  assert.ok(vectors.length > 20, "the vectors are there");
  const wrong = [];
  for (const one of vectors) {
    const got = one.fn === "reportReread" ? reportReread(one.args) : splitRun(one.args);
    const same = JSON.stringify(got) === JSON.stringify(one.expect);
    if (!same) wrong.push(`${one.fn} ${JSON.stringify(one.args)}: expected ${JSON.stringify(one.expect)}, got ${JSON.stringify(got)}`);
  }
  assert.deepEqual(wrong, []);
});

// Tests for the Agents feature's pure logic (plugin/hooks/lib/agents.js).
//
//   node --test tests/js/agents.test.mjs
//
// tests/test_mod_agents.py runs this file under Node.

import test from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath, pathToFileURL } from "node:url";
import { dirname, join } from "node:path";

const lib = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "plugin", "hooks", "lib");
const agents = await import(pathToFileURL(join(lib, "agents.js")).href);
const {
  activityOf,
  tierOf,
  shortModel,
  agentLabel,
  sortAgents,
  agentShare,
  kText,
} = agents;

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

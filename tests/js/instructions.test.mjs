// Tests for the Instructions page's pure logic (plugin/hooks/lib/instructions.js).
//
//   node --test tests/js/instructions.test.mjs

import test from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath, pathToFileURL } from "node:url";
import { dirname, join } from "node:path";

const lib = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "plugin", "hooks", "lib");
const mod = await import(pathToFileURL(join(lib, "instructions.js")).href);
const { newLedger, follow, record, addAttachment, tokensOf, mcpServer, humanize, shortPath, entriesOf, startupChars, largest, adviceFor, groupsOf, receivedClaudeMd, agentTypesOf } = mod;

function item(id, kind, chars, extra = {}) {
  return { id, kind, name: id, chars, reply: 1, from: "engine", startup: true, ...extra };
}

test("items keep the order they loaded in, and loading one again keeps its place and its first reply", () => {
  const ledger = newLedger();
  record(ledger, item("a", "system", 40));
  record(ledger, item("b", "system", 80, { reply: 1 }));
  record(ledger, item("a", "system", 60, { reply: 3 }));
  assert.deepEqual(ledger.items.map((one) => [one.id, one.seq, one.chars, one.reply]), [["a", 1, 60, 1], ["b", 2, 80, 1]]);
});

test("a size measured from the text Claude read is not replaced by a guess", () => {
  const ledger = newLedger();
  record(ledger, item("f", "instructions", 800, { measured: true }));
  record(ledger, item("f", "instructions", 900));
  assert.equal(ledger.items[0].chars, 800);
});

test("a new session empties the ledger; the same one leaves it", () => {
  const ledger = newLedger();
  follow(ledger, "one");
  record(ledger, item("a", "system", 40));
  assert.equal(follow(ledger, "one"), false);
  assert.equal(ledger.items.length, 1);
  assert.equal(follow(ledger, "two"), true);
  assert.deepEqual(ledger.items, []);
  assert.equal(ledger.seq, 0);
});

test("attachments of one type are one item that counts them", () => {
  const ledger = newLedger();
  addAttachment(ledger, "todo_reminder", 100, 2);
  addAttachment(ledger, "todo_reminder", 60, 3);
  addAttachment(ledger, "mcp_instructions_delta", 400, 1);
  addAttachment(ledger, "instructions", 400, 1);
  assert.equal(ledger.items.length, 3);
  assert.deepEqual([ledger.items[0].chars, ledger.items[0].count, ledger.items[0].startup], [160, 2, false]);
  assert.deepEqual(ledger.items.map((one) => one.startup), [false, true, false]);
});

test("tokens are characters over the rate, 4 when the rate is missing", () => {
  assert.equal(tokensOf(4000, 4), 1000);
  assert.equal(tokensOf(3300, 3.3), 1000);
  assert.equal(tokensOf(4000, 0), 1000);
  assert.equal(tokensOf(4000, Number.NaN), 1000);
});

test("an MCP server comes from the provider, else from the tool's name", () => {
  assert.equal(mcpServer("mcp__x__y", "mcp:github"), "github");
  assert.equal(mcpServer("mcp__claude_ai_Gmail__send", "plugin"), "claude_ai_Gmail");
  assert.equal(mcpServer("Bash", "engine"), "");
});

test("names read as words and paths as their last two parts", () => {
  assert.equal(humanize("doing_tasks"), "doing tasks");
  assert.equal(shortPath("C:\\work\\app\\CLAUDE.md"), "app/CLAUDE.md");
  assert.equal(shortPath("/CLAUDE.md"), "CLAUDE.md");
});

function session() {
  const ledger = newLedger();
  record(ledger, item("sys:intro", "system", 8000));
  record(ledger, item("file:a", "instructions", 24000, { path: "/work/CLAUDE.md", from: "project" }));
  record(ledger, item("tool:Bash", "tools", 12000));
  record(ledger, item("tool:Read", "tools", 8000));
  record(ledger, item("tool:mcp__g__a", "mcp", 30000, { server: "g", from: "mcp" }));
  record(ledger, item("tool:mcp__g__b", "mcp", 14000, { server: "g", from: "mcp" }));
  record(ledger, item("tool:mcp__d__s", "mcp", 20000, { server: "d", from: "mcp", deferred: true }));
  record(ledger, item("scoped", "instructions", 8000, { path: "/work/src/CLAUDE.md", from: "scoped", scoped: true, startup: false, reply: 2 }));
  return ledger;
}

test("tools roll up into one row per group and per MCP server; the startup total leaves out what waits and what loaded later", () => {
  const entries = entriesOf(session().items);
  assert.deepEqual(entries.map((one) => [one.key, one.count, one.chars, one.startupChars]), [
    ["sys:intro", 1, 8000, 8000],
    ["file:a", 1, 24000, 24000],
    ["tools", 2, 20000, 20000],
    ["mcp:g", 2, 44000, 44000],
    ["mcp:d", 1, 20000, 0],
    ["scoped", 1, 8000, 0],
  ]);
  assert.deepEqual(entries[2].biggest, [{ name: "tool:Bash", chars: 12000 }, { name: "tool:Read", chars: 8000 }]);
  assert.equal(entries[4].deferred, true);
  assert.equal(entries[3].deferred, false);
  assert.equal(startupChars(entries), 8000 + 24000 + 20000 + 44000);
});

test("the largest rows are the startup ones, largest first", () => {
  const top = largest(entriesOf(session().items), 3);
  assert.deepEqual(top.map((one) => one.key), ["mcp:g", "file:a", "tools"]);
  assert.deepEqual(largest(entriesOf(session().items), 99).map((one) => one.key), ["mcp:g", "file:a", "tools", "sys:intro"]);
});

test("advice names one item: an MCP server in the prompt at the limit, or a file at its own", () => {
  const entries = entriesOf(session().items);
  // 44000 chars is 11k tokens (over 10k); the file is 6k (over 5k): the server is larger.
  assert.deepEqual(adviceFor(entries, 4), { kind: "mcp", name: "g", tokens: 11000 });
  assert.deepEqual(adviceFor(entries, 4, { mcp: 12000 }), { kind: "file", name: "work/CLAUDE.md", tokens: 6000 });
  assert.equal(adviceFor(entries, 4, { mcp: 12000, file: 7000 }), null);
  // A server that waits behind tool search costs nothing in the prompt.
  const deferredOnly = entriesOf([item("t", "mcp", 400000, { server: "d", deferred: true })]);
  assert.equal(adviceFor(deferredOnly, 4), null);
  // The threshold is on the estimate, at the calibrated rate.
  assert.equal(adviceFor(entriesOf([item("t", "mcp", 39996, { server: "d" })]), 4), null);
  assert.equal(adviceFor(entriesOf([item("t", "mcp", 40000, { server: "d" })]), 4).tokens, 10000);
});

test("groups come in the order their first row loaded, rows in load order", () => {
  const groups = groupsOf(entriesOf(session().items));
  assert.deepEqual(groups.map((one) => [one.kind, one.entries.length]), [["system", 1], ["instructions", 2], ["tools", 1], ["mcp", 2]]);
});

test("a conversation that opens with a CLAUDE.md block received it", () => {
  const block = { type: "text", text: "<system-reminder>\n# claudeMd\nCodebase and user instructions are shown below.\n</system-reminder>" };
  assert.equal(receivedClaudeMd([{ role: "user", content: [block, { type: "text", text: "go" }] }]), true);
  assert.equal(receivedClaudeMd([{ role: "user", content: "# claudeMd\nrules" }]), true);
  assert.equal(receivedClaudeMd([{ role: "user", content: "go" }, { role: "assistant", content: "ok" }]), false);
  assert.equal(receivedClaudeMd([]), null);
  assert.equal(receivedClaudeMd({ deny: "no" }), null);
  // A mention of the block far into the conversation does not count.
  assert.equal(receivedClaudeMd([{ role: "user", content: "a" }, { role: "assistant", content: "b" }, { role: "user", content: "c" }, { role: "user", content: "# claudeMd" }]), false);
});

test("agent types split into those that received CLAUDE.md and those that did not; runs not seen are left out", () => {
  const agents = [
    { id: "1", type: "general-purpose" },
    { id: "2", type: "Explore" },
    { id: "3", type: "Explore" },
    { id: "4", type: "Plan" },
    { id: "5", type: "" },
  ];
  assert.deepEqual(agentTypesOf(agents, { 1: true, 2: false, 3: false, 5: true }), { got: ["general-purpose"], skipped: ["Explore"] });
  assert.deepEqual(agentTypesOf(agents, { 2: false, 3: true }), { got: ["Explore"], skipped: [] });
  assert.deepEqual(agentTypesOf(agents, {}), { got: [], skipped: [] });
});

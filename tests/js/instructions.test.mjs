// Tests for the Startup page's pure logic (plugin/hooks/lib/instructions.js).
//
//   node --test tests/js/instructions.test.mjs

import test from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath, pathToFileURL } from "node:url";
import { dirname, join } from "node:path";

const lib = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "plugin", "hooks", "lib");
const mod = await import(pathToFileURL(join(lib, "instructions.js")).href);
const { newLedger, follow, record, addAttachment, isAttached, compacted, tokensOf, mcpServer, shortPath, entriesOf, startupChars, adviceFor, receivedClaudeMd, agentTypesOf, fitToCategories, categoryOf, serverLabel, startupGroups, isDesktopServer, toolShares, mcpShares, mcpToolLabel } = mod;

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

test("a file or a hook's note added to a message is kept apart from Claude Code's reminders of the same type", () => {
  const ledger = newLedger();
  addAttachment(ledger, "hook_additional_context", 100, 2, false);
  addAttachment(ledger, "hook_additional_context", 300, 3, true);
  addAttachment(ledger, "hook_additional_context", 50, 4, true);
  assert.deepEqual(ledger.items.map((one) => [one.id, one.from, one.chars, one.count]), [
    ["att:hook_additional_context", "attachment", 100, 1],
    ["att:hook_additional_context:attached", "attached", 350, 2],
  ]);
  // The engine's own text is a reminder, unless the type carries a file; a settings hook's or a plugin's text is added to the message.
  assert.equal(isAttached("todo_reminder", "engine"), false);
  assert.equal(isAttached("todo_reminder", undefined), false);
  assert.equal(isAttached("file", "engine"), true);
  assert.equal(isAttached("edited_text_file", "engine"), true);
  assert.equal(isAttached("hook_success", "hook"), true);
  assert.equal(isAttached("anything", "plugin"), true);
});

test("a summary drops what loaded after the start and keeps what the session starts with", () => {
  const ledger = newLedger();
  record(ledger, item("sys:intro", "system", 400));
  record(ledger, item("file:u", "instructions", 800, { path: "/h/CLAUDE.md", from: "user" }));
  addAttachment(ledger, "mcp_instructions_delta", 400, 1);
  record(ledger, item("scoped", "instructions", 800, { path: "/w/src/CLAUDE.md", from: "scoped", scoped: true, startup: false, reply: 2 }));
  record(ledger, item("skill:commit", "skills", 200, { from: "skill", startup: false, reply: 2 }));
  addAttachment(ledger, "todo_reminder", 100, 2, false);
  addAttachment(ledger, "file", 600, 3, true);
  assert.equal(ledger.items.length, 7);
  assert.equal(compacted(ledger), true);
  assert.deepEqual(ledger.items.map((one) => one.id), ["sys:intro", "file:u", "att:mcp_instructions_delta"]);
  // Nothing more to drop the second time; what comes after a summary is counted afresh.
  assert.equal(compacted(ledger), false);
  addAttachment(ledger, "todo_reminder", 40, 5, false);
  assert.deepEqual(ledger.items.map((one) => [one.id, one.chars]).slice(-1), [["att:todo_reminder", 40]]);
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

test("paths read as their last two parts", () => {
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

test("a server the Desktop app runs itself is never the advice", () => {
  const entries = entriesOf([item("t", "mcp", 400000, { server: "ccd_session", from: "mcp" }), item("u", "mcp", 60000, { server: "g", from: "mcp" })]);
  assert.deepEqual(adviceFor(entries, 4), { kind: "mcp", name: "g", tokens: 15000 });
  assert.equal(isDesktopServer("ccd_directory"), true);
  assert.equal(isDesktopServer("claude_ccd"), false);
});

test("an MCP row keeps its tools' names in load order", () => {
  const entries = entriesOf(session().items);
  assert.deepEqual(entries.find((one) => one.key === "mcp:g").tools, ["tool:mcp__g__a", "tool:mcp__g__b"]);
  assert.equal(entries.find((one) => one.key === "tools").tools, undefined);
});

test("a rolled-up row keeps every tool, in load order, with whether it waits behind tool search", () => {
  const entries = entriesOf(session().items);
  assert.deepEqual(entries.find((one) => one.key === "tools").members, [
    { name: "tool:Bash", chars: 12000, deferred: false },
    { name: "tool:Read", chars: 8000, deferred: false },
  ]);
  assert.deepEqual(entries.find((one) => one.key === "mcp:g").members.map((one) => [one.name, one.chars, one.deferred]), [["tool:mcp__g__a", 30000, false], ["tool:mcp__g__b", 14000, false]]);
  assert.deepEqual(entries.find((one) => one.key === "mcp:d").members.map((one) => one.deferred), [true]);
  // Past the three that `biggest` keeps.
  const many = entriesOf(["a", "b", "c", "d", "e"].map((name, at) => item("tool:" + name, "tools", 100 * (at + 1), { name })));
  assert.equal(many[0].biggest.length, 3);
  assert.deepEqual(many[0].members.map((one) => one.name), ["a", "b", "c", "d", "e"]);
  // A row that is one item has no members.
  assert.deepEqual(entries.find((one) => one.key === "file:a").members, []);
  // The deferred built-in tools are a row of their own, apart from the plugins'.
  const kinds = entriesOf([item("t:A", "tools", 40, { name: "A" }), item("t:B", "deferred", 80, { name: "B", deferred: true }), item("t:C", "deferred", 20, { name: "C", from: "plugin", deferred: true })]);
  assert.deepEqual(kinds.map((one) => [one.key, one.members.map((member) => member.name)]), [["tools", ["A"]], ["deferred", ["B"]], ["deferred:plugin", ["C"]]]);
});

/** A tool of a row, in the prompt unless it says otherwise. */
const member = (name, chars, deferred = false) => ({ name, chars, deferred });

test("a row's tokens are shared out among its tools by size, largest first, and add up to the row", () => {
  const shares = toolShares([member("small", 1000), member("big", 3000), member("mid", 2000)], 1000);
  assert.deepEqual(shares, [{ name: "big", tokens: 500, deferred: false }, { name: "mid", tokens: 333, deferred: false }, { name: "small", tokens: 167, deferred: false }]);
  // What rounding down leaves over goes one token each to the largest fractions, a tie to the larger tool and then the earlier: 33 + 33 + 33 is 99 of 100.
  const three = [member("a", 100), member("b", 100), member("c", 100)];
  assert.deepEqual(toolShares(three, 100).map((one) => one.tokens), [34, 33, 33]);
  // Never a negative share, and the order stays largest first: two of three equal tools get the 2, six equal tools share 3 as 1 1 1 0 0 0.
  assert.deepEqual(toolShares(three, 2).map((one) => one.tokens), [1, 1, 0]);
  const six = ["a", "b", "c", "d", "e", "f"].map((name) => member(name, 50));
  assert.deepEqual(toolShares(six, 3).map((one) => one.tokens), [1, 1, 1, 0, 0, 0]);
  // The larger fraction wins over the larger tool: 10 over 3, 2 and 1 tools' text is 5, 3.33 and 1.67 before rounding.
  assert.deepEqual(toolShares([member("a", 3), member("b", 2), member("c", 1)], 10).map((one) => one.tokens), [5, 3, 2]);
  // Tools that all weigh nothing share the row equally.
  assert.deepEqual(toolShares([member("a", 0), member("b", 0)], 5).map((one) => one.tokens), [3, 2]);
  for (const tokens of [0, 1, 2, 3, 7, 999]) {
    const all = toolShares(six.concat([member("g", 3), member("h", 900)]), tokens);
    assert.equal(all.reduce((sum, one) => sum + one.tokens, 0), tokens);
    assert.equal(all.every((one) => one.tokens >= 0), true);
    assert.deepEqual(all.map((one) => one.name).slice(0, 2), ["h", "a"]);
  }
  for (const tokens of [0, 1, 7, 999, 12345]) {
    const sum = toolShares([member("a", 7), member("b", 13), member("c", 29), member("d", 1)], tokens).reduce((all, one) => all + one.tokens, 0);
    assert.equal(sum, tokens);
  }
  // A single tool is the whole row; no tools is nothing.
  assert.deepEqual(toolShares([member("only", 5)], 77), [{ name: "only", tokens: 77, deferred: false }]);
  assert.deepEqual(toolShares([], 77), []);
});

test("tools behind tool search come after the others, largest first, with no tokens and no part of the row", () => {
  const shares = toolShares([member("lazy", 9000, true), member("a", 100), member("lazier", 90000, true), member("b", 300)], 400);
  assert.deepEqual(shares, [
    { name: "b", tokens: 300, deferred: false },
    { name: "a", tokens: 100, deferred: false },
    { name: "lazier", tokens: 0, deferred: true },
    { name: "lazy", tokens: 0, deferred: true },
  ]);
  // Only tools that wait: listed, with nothing to share.
  assert.deepEqual(toolShares([member("lazy", 9000, true)], 0), [{ name: "lazy", tokens: 0, deferred: true }]);
  // Sizes given by name stand in for the characters.
  const weighed = toolShares([member("a", 10), member("b", 10)], 100, { a: 1, b: 3 });
  assert.deepEqual(weighed.map((one) => [one.name, one.tokens]), [["b", 75], ["a", 25]]);
});

test("an MCP server's tools share its tokens by Claude Code's count of them, else by characters, never a mix", () => {
  const members = [member("mcp__s__one", 1000), member("mcp__s__two", 3000), member("mcp__s__lazy", 8000, true)];
  /** @param {string} name @param {number} tokens @param {boolean} [isLoaded] */
  const counted = (name, tokens, isLoaded = true) => ({ name, serverName: "s", tokens, isLoaded });
  const tokensOfShares = (shares) => shares.map((one) => [one.name, one.tokens]);
  // By characters: 400 shared 1:3.
  assert.deepEqual(tokensOfShares(mcpShares(members, 400, [])).slice(0, 2), [["mcp__s__two", 300], ["mcp__s__one", 100]]);
  assert.deepEqual(tokensOfShares(mcpShares(members, 400, null)).slice(0, 2), [["mcp__s__two", 300], ["mcp__s__one", 100]]);
  // By Claude Code's count when it lists every tool in the prompt (the one waiting needs no entry); the order follows the count.
  const both = [counted("mcp__s__one", 90), counted("mcp__s__two", 10)];
  assert.deepEqual(tokensOfShares(mcpShares(members, 400, both)).slice(0, 2), [["mcp__s__one", 360], ["mcp__s__two", 40]]);
  // A count of nothing for every tool in the prompt says nothing of their sizes: the characters.
  assert.deepEqual(tokensOfShares(mcpShares(members, 400, [counted("mcp__s__one", 0), counted("mcp__s__two", 0)])).slice(0, 2), [["mcp__s__two", 300], ["mcp__s__one", 100]]);
  // One missing from its list, or listed as not loaded: the characters, for every tool.
  assert.deepEqual(tokensOfShares(mcpShares(members, 400, [counted("mcp__s__one", 90)])).slice(0, 2), [["mcp__s__two", 300], ["mcp__s__one", 100]]);
  assert.deepEqual(tokensOfShares(mcpShares(members, 400, [counted("mcp__s__one", 90), counted("mcp__s__two", 10, false)])).slice(0, 2), [["mcp__s__two", 300], ["mcp__s__one", 100]]);
  // Either way they add up to the server's figure, and the one waiting has none.
  const sum = mcpShares(members, 401, both).reduce((all, one) => all + one.tokens, 0);
  assert.equal(sum, 401);
  assert.deepEqual(mcpShares(members, 400, both)[2], { name: "mcp__s__lazy", tokens: 0, deferred: true });
});

test("an MCP tool is named by the part after its server, as words", () => {
  assert.equal(mcpToolLabel("mcp__github__create_issue", "github"), "create issue");
  assert.equal(mcpToolLabel("mcp__plugin_playwright_playwright__browser-click", "plugin_playwright_playwright"), "browser click");
  assert.equal(mcpToolLabel("mcp__other__thing", "github"), "thing");
  assert.equal(mcpToolLabel("mcp__github__", "github"), "mcp__github__");
});

test("built-in tools are one row of what the person controls, counting those behind tool search; a session with none in the prompt shows no row", () => {
  const sizeOf = (one) => tokensOf(one.startupChars > 0 ? one.startupChars : one.chars, 4);
  const entries = entriesOf([item("sys", "system", 4000), item("t:Bash", "tools", 12000, { name: "Bash" }), item("t:Zed", "deferred", 400, { name: "Zed", deferred: true }), item("t:Read", "tools", 4000, { name: "Read" })]);
  const groups = startupGroups(entries, sizeOf, {});
  assert.deepEqual(groups.control.rows.map((row) => [row.id, row.tokens, row.count]), [["tools", 4000, 3]]);
  assert.deepEqual(groups.control.rows[0].items.map((one) => [one.label, one.tokens, one.deferred]), [["Bash", 3000, false], ["Read", 1000, false], ["Zed", 0, true]]);
  // Claude Code's own is the rest: the system prompt alone, with or without Claude Code's count.
  assert.equal(groups.builtin.tokens, 1000);
  assert.equal(startupGroups(entries, sizeOf, {}, 9000).builtin.tokens, 5000);
  assert.equal(groups.total, 5000);
  // All of them behind tool search: counted, but nothing in the prompt, so no row.
  const lazy = startupGroups(entriesOf([item("sys", "system", 4000), item("t:Zed", "deferred", 400, { name: "Zed", deferred: true })]), sizeOf, {});
  assert.deepEqual(lazy.control.rows, []);
  assert.equal(lazy.builtin.tokens, 1000);
});

test("a server is named as /mcp lists it, else from its wire name, else as a connector of its first tools", () => {
  const ID = "5f3a2c1e-9b4d-4e8a-8c3d-1a2b3c4d5e6f";
  assert.equal(serverLabel("github", { github: "GitHub" }, []), "GitHub");
  assert.equal(serverLabel("Claude_Browser", {}, []), "Claude Browser");
  assert.equal(serverLabel("claude-in-chrome", undefined, undefined), "claude in chrome");
  assert.equal(serverLabel("plugin_playwright_playwright", null, []), "playwright");
  assert.equal(serverLabel("plugin_my-plugin_docs_search", {}, []), "docs search");
  assert.equal(serverLabel("plugin_solo", {}, []), "solo");
  // A plugin's server is listed as plugin:<plugin>:<server>: its server part, as words.
  assert.equal(serverLabel("plugin_playwright_playwright", { plugin_playwright_playwright: "plugin:playwright:playwright" }, []), "playwright");
  assert.equal(serverLabel("plugin_my-plugin_docs_search", { "plugin_my-plugin_docs_search": "plugin:my-plugin:docs_search" }, []), "docs search");
  // An id has no name to show: its first three tools stand in for it.
  assert.equal(serverLabel(ID, {}, [`mcp__${ID}__batch`, `mcp__${ID}__guide`, `mcp__${ID}__update`]), "Connector (batch, guide, update)");
  assert.equal(serverLabel(ID, {}, [`mcp__${ID}__batch`, `mcp__${ID}__guide`, `mcp__${ID}__update`, `mcp__${ID}__query`]), "Connector (batch, guide, update, …)");
  assert.equal(serverLabel(ID, {}, [`mcp__${ID}__only`]), "Connector (only)");
  // Tool names are read as words.
  assert.equal(serverLabel(ID, {}, [`mcp__${ID}__copy_file`, `mcp__${ID}__create_file`, `mcp__${ID}__download_file_content`, `mcp__${ID}__x`]), "Connector (copy file, create file, download file content, …)");
  assert.equal(serverLabel(ID, {}, []), "Connector");
  // A name /mcp lists that is itself an id is no better.
  assert.equal(serverLabel("srv", { srv: ID }, ["mcp__srv__a", "mcp__srv__b"]), "Connector (a, b)");
});

test("the three groups: what the person controls, Claude Code's own, and what was added as the work went on", () => {
  const ledger = newLedger();
  record(ledger, item("sys:intro", "system", 8000));
  record(ledger, item("ctx:date", "context", 40));
  record(ledger, item("file:u", "instructions", 4000, { path: "/home/me/.claude/CLAUDE.md", from: "user" }));
  record(ledger, item("file:p", "instructions", 24000, { path: "/work/CLAUDE.md", from: "project" }));
  record(ledger, item("tool:Bash", "tools", 12000, { name: "Bash", from: "engine" }));
  record(ledger, item("tool:Read", "tools", 4000, { name: "Read", from: "engine" }));
  record(ledger, item("tool:Note", "deferred", 6000, { name: "Note", from: "engine", deferred: true }));
  record(ledger, item("tool:Plug", "tools", 4000, { from: "plugin" }));
  record(ledger, item("tool:Lazy", "deferred", 9000, { from: "plugin", deferred: true }));
  record(ledger, item("tool:mcp__a__x", "mcp", 8000, { name: "mcp__a__x", server: "a", from: "mcp" }));
  record(ledger, item("tool:mcp__b__x", "mcp", 20000, { name: "mcp__b__x", server: "b", from: "mcp" }));
  record(ledger, item("tool:mcp__c__x", "mcp", 40000, { server: "c", from: "mcp", deferred: true }));
  record(ledger, item("tool:mcp__ccd_session__x", "mcp", 16000, { server: "ccd_session", from: "mcp" }));
  record(ledger, item("att:skill_listing", "skills", 6000, { name: "skill listing", from: "skill" }));
  record(ledger, item("att:agent_listing_delta", "attachment", 800, { name: "agent_listing_delta", from: "attachment", startup: true }));
  record(ledger, item("skill:commit", "skills", 2000, { from: "skill", startup: false, reply: 3 }));
  record(ledger, item("scoped", "instructions", 8000, { name: "CLAUDE.md", path: "/work/src/CLAUDE.md", from: "scoped", scoped: true, startup: false, reply: 2 }));
  record(ledger, item("att:todo_reminder", "attachment", 400, { name: "todo_reminder", from: "attachment", startup: false }));
  const entries = entriesOf(ledger.items);
  const sizeOf = (one) => tokensOf(one.startupChars > 0 ? one.startupChars : one.chars, 4);
  const groups = startupGroups(entries, sizeOf, { a: "Alpha" });

  // You control: the three servers (two in the prompt, one waiting behind tool search; the Desktop app's is not theirs), the built-in tools (three, one of them behind tool search), both CLAUDE.md files that loaded at the start, the skill list, the plugin's tool.
  assert.deepEqual(groups.control.rows.map((row) => [row.id, row.tokens, row.count]), [["mcp", 7000, 3], ["tools", 4000, 3], ["files", 7000, 2], ["skills", 1500, 1], ["plugin", 1000, 1]]);
  assert.equal(groups.control.tokens, 20500);
  // Servers largest first, by name, each with its tools in the prompt; the one behind tool search comes last without a size or tools.
  assert.deepEqual(groups.control.rows[0].items, [
    { label: "b", tokens: 5000, deferred: false, tools: [{ label: "x", tokens: 5000 }] },
    { label: "Alpha", tokens: 2000, deferred: false, tools: [{ label: "x", tokens: 2000 }] },
    { label: "c", tokens: 0, deferred: true },
  ]);
  // Built-in tools: largest first with their share of the row, the one behind tool search after with none.
  assert.deepEqual(groups.control.rows[1].items, [
    { label: "Bash", tokens: 3000, deferred: false },
    { label: "Read", tokens: 1000, deferred: false },
    { label: "Note", tokens: 0, deferred: true },
  ]);
  assert.deepEqual(groups.control.rows[2].items.map((one) => one.label), [".claude/CLAUDE.md", "work/CLAUDE.md"]);
  // Claude Code's own: system prompt 2k, date 10, the Desktop server 4k, the agent listing 200. The built-in tools are the person's now.
  assert.equal(groups.builtin.tokens, 2000 + 10 + 4000 + 200);
  assert.deepEqual(groups.later.rows.map((row) => [row.id, row.tokens]), [["files-later", 2000], ["skills-used", 500], ["reminders", 100]]);
  assert.deepEqual(groups.later.rows[0].items, [{ label: "src/CLAUDE.md", tokens: 2000, deferred: false }]);
  assert.equal(groups.later.tokens, 2600);
  // The first two add up to what the session starts with.
  assert.equal(groups.control.tokens + groups.builtin.tokens, tokensOf(startupChars(entries), 4));
  // Given the total the page shows (Claude Code's count), Claude Code's own is what is left of it.
  assert.equal(startupGroups(entries, sizeOf, {}, 40000).builtin.tokens, 40000 - 20500);
  // A total smaller than the rows is not used.
  assert.equal(startupGroups(entries, sizeOf, {}, 100).builtin.tokens, groups.builtin.tokens);
});

test("the instructions your servers gave Claude at the start are part of the servers' row, not Claude Code's own", () => {
  const ledger = newLedger();
  record(ledger, item("sys:intro", "system", 8000));
  record(ledger, item("tool:mcp__a__x", "mcp", 8000, { name: "mcp__a__x", server: "a", from: "mcp" }));
  record(ledger, item("tool:mcp__c__x", "mcp", 40000, { server: "c", from: "mcp", deferred: true }));
  addAttachment(ledger, "mcp_instructions_delta", 4000, 1);
  addAttachment(ledger, "agent_listing_delta", 800, 1);
  const entries = entriesOf(ledger.items);
  const sizeOf = (one) => tokensOf(one.startupChars > 0 ? one.startupChars : one.chars, 4);
  const groups = startupGroups(entries, sizeOf, {});
  // Two servers are listed (one waits behind tool search); the instructions add their size but are not a server.
  assert.deepEqual(groups.control.rows.map((row) => [row.id, row.tokens, row.count]), [["mcp", 3000, 2]]);
  assert.deepEqual(groups.control.rows[0].items, [
    { label: "a", tokens: 2000, deferred: false, tools: [{ label: "x", tokens: 2000 }] },
    { label: "mcp_instructions_delta", tokens: 1000, deferred: false, instructions: true },
    { label: "c", tokens: 0, deferred: true },
  ]);
  // Claude Code's own: the system prompt and the agent listing, not the servers' instructions.
  assert.equal(groups.builtin.tokens, 2000 + 200);
  // The servers' share of Claude Code's count is shared between the tools and the instructions, and both stay in the row.
  const fit = fitToCategories(entries, [{ name: "MCP tools", tokens: 3000, kind: "used", isDeferred: false }, { name: "System prompt", tokens: 2000, kind: "used", isDeferred: false }]);
  const fitted = startupGroups(entries, (one) => fit.sizes[one.key] ?? sizeOf(one), {}, fit.total);
  assert.equal(fitted.control.tokens, 3000);
  assert.equal(fitted.builtin.tokens, 2000);
});

test("files and notes added to a message are their own row in the third group, apart from Claude Code's reminders", () => {
  const ledger = newLedger();
  addAttachment(ledger, "todo_reminder", 400, 2, false);
  addAttachment(ledger, "file", 2000, 2, true);
  addAttachment(ledger, "hook_additional_context", 800, 3, true);
  const entries = entriesOf(ledger.items);
  const groups = startupGroups(entries, (one) => tokensOf(one.chars, 4), {});
  assert.deepEqual(groups.later.rows.map((row) => [row.id, row.tokens, row.count]), [["reminders", 100, 1], ["attached", 700, 2]]);
  assert.equal(groups.later.tokens, 800);
  assert.deepEqual(groups.control.rows, []);
});

test("rows of no tokens are left out, and nothing loaded gives empty groups", () => {
  const empty = startupGroups([], () => 0, {});
  assert.deepEqual(empty, { control: { tokens: 0, rows: [] }, builtin: { tokens: 0 }, later: { tokens: 0, rows: [] }, total: 0, fits: false });
  const only = startupGroups(entriesOf([item("a", "instructions", 4000, { path: "/w/CLAUDE.md" })]), (one) => tokensOf(one.startupChars, 4), {});
  assert.deepEqual(only.control.rows.map((row) => row.id), ["files"]);
  assert.deepEqual(only.later.rows, []);
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

const USED = (name, tokens) => ({ name, tokens, kind: "used", isDeferred: false });

test("Claude Code's categories become the group totals, and each row is its share of its group", () => {
  const entries = entriesOf(session().items);
  const fit = fitToCategories(entries, [
    USED("System prompt", 3000),
    USED("System tools", 9000),
    USED("MCP tools", 20000),
    USED("Memory files", 4000),
    USED("Messages", 50000),
    { name: "Free space", tokens: 100000, kind: "free", isDeferred: false },
    { name: "System tools (deferred)", tokens: 7000, kind: "deferred", isDeferred: true },
  ]);
  assert.equal(fit.matched, true);
  // The total is what /context calls startup: the used rows less the conversation, deferred rows left out.
  assert.equal(fit.total, 3000 + 9000 + 20000 + 4000);
  assert.deepEqual(fit.sizes, { "sys:intro": 3000, "file:a": 4000, tools: 9000, "mcp:g": 20000 });
  // A group with two rows splits by characters.
  const split = fitToCategories(entriesOf([item("a", "instructions", 3000, { startup: true }), item("b", "instructions", 1000, { startup: true })]), [USED("Memory files", 800)]);
  assert.deepEqual(split.sizes, { a: 600, b: 200 });
  // The advice works on the fitted sizes.
  assert.deepEqual(adviceFor(entries, 4, undefined, fit.sizes), { kind: "mcp", name: "g", tokens: 20000 });
  assert.deepEqual(adviceFor(entries, 4, { mcp: 25000, file: 3000 }, fit.sizes), { kind: "file", name: "work/CLAUDE.md", tokens: 4000 });
});

test("nothing is matched before Claude Code counts a startup, or when no category fits a row", () => {
  const entries = entriesOf(session().items);
  assert.equal(fitToCategories(entries, undefined).matched, false);
  assert.equal(fitToCategories(entries, []).matched, false);
  assert.equal(fitToCategories(entries, [USED("System prompt", 0), USED("Messages", 0)]).matched, false);
  // Claude Code's rows are said to cover the rows unless the built-in tools are in the prompt and it has no row for them.
  assert.equal(fitToCategories(entries, [USED("System prompt", 3000), USED("System tools", 9000)]).covered, true);
  assert.equal(fitToCategories(entries, [USED("System prompt", 3000), USED("MCP tools", 20000), USED("Memory files", 4000)]).covered, false);
  assert.equal(fitToCategories(entriesOf([item("a", "system", 400)]), [USED("System prompt", 100)]).covered, true);
  // A category with no row of the page's: counted in the total, shared out to nobody.
  const odd = fitToCategories(entries, [USED("Slash commands", 500)]);
  assert.equal(odd.matched, false);
  assert.equal(odd.total, 500);
  // A row with no category of its own in the breakdown keeps no fitted size.
  const partial = fitToCategories(entries, [USED("System tools", 9000)]);
  assert.deepEqual(partial.sizes, { tools: 9000 });
  // A total too small for the rows that keep their estimates can't hold them: the built-in part is estimated too, and the total is the sum.
  const sizeOf = (one) => partial.sizes[one.key] ?? Math.round(one.startupChars / 4);
  const short = startupGroups(entries, sizeOf, {}, 1);
  assert.equal(short.fits, false);
  assert.equal(short.total, short.control.tokens + short.builtin.tokens);
  const held = startupGroups(entries, sizeOf, {}, 1000000);
  assert.equal(held.fits, true);
  assert.equal(held.total, 1000000);
  assert.equal(held.builtin.tokens, 1000000 - held.control.tokens);
  assert.equal(categoryOf({ kind: "deferred", name: "x" }), "");
  assert.equal(categoryOf({ kind: "attachment", name: "agent_listing_delta" }), "agents");
  assert.equal(categoryOf({ kind: "attachment", name: "mcp_instructions_delta" }), "mcp");
  assert.equal(categoryOf({ kind: "attachment", name: "reminder" }), "");
});

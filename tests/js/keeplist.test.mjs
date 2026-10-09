// Tests for the compaction review's keep list (plugin/hooks/lib/keeplist.js).
//
//   node --test tests/js/keeplist.test.mjs

import test from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath, pathToFileURL } from "node:url";
import { dirname, join } from "node:path";

import { planted } from "./planted.mjs";

const lib = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "plugin", "hooks", "lib");
const { buildKeepList, splitSuggested, contentWords, exactTokens, itemId, trimText, MAX_TEXT, MAX_USER_MESSAGE, LIMITS } = await import(
  pathToFileURL(join(lib, "keeplist.js")).href
);

const kinds = (items, kind) => items.filter((item) => item.kind === kind);

test("the planted conversation gives every kind of item", () => {
  const items = buildKeepList(planted());
  const text = (kind) => kinds(items, kind).map((item) => item.text);

  const instructions = text("instruction");
  assert.ok(instructions.some((t) => /never use port 8765/i.test(t)), instructions.join("|"));
  assert.ok(instructions.some((t) => /don't read any code yet/i.test(t)));
  assert.ok(instructions.some((t) => /use pnpm, not npm/i.test(t)));
  assert.ok(!instructions.some((t) => /rate limiting/i.test(t)), "a plain request is not a constraint");

  assert.ok(text("decision").some((t) => /option B/.test(t)));
  assert.deepEqual(text("task"), ["Add the burst test", "Document the header"]);

  const files = text("file");
  assert.ok(files.includes("src/api/limiter.ts") && files.includes("notes/try.ipynb"));
  assert.equal(files.filter((t) => t === "src/api/limiter.ts").length, 1, "a file edited twice is one item");

  const [error] = kinds(items, "error");
  assert.ok(error, "the failing test is kept");
  assert.match(error.text, /pytest tests\/test_limiter\.py/);
  assert.match(error.text, /test_burst/);
  assert.ok(error.exact.some((token) => token.includes("test_limiter.py")));
});

test("the port is one item, not an instruction and a value", () => {
  const items = buildKeepList(planted());
  assert.equal(kinds(items, "value").filter((item) => item.exact[0] === "8765").length, 0);
  assert.ok(kinds(items, "instruction").some((item) => item.exact.includes("8765")));
});

test("the five newest files are safe anyway, older ones are not", () => {
  const files = kinds(buildKeepList(planted()), "file");
  assert.equal(files.length, 6);
  const safe = files.filter((item) => item.safe).map((item) => item.text);
  const notSafe = files.filter((item) => !item.safe).map((item) => item.text);
  assert.equal(safe.length, 5);
  assert.deepEqual(notSafe, ["src/api/queue.ts"], "the oldest change is the one that is not safe");
  assert.ok(files.every((item) => (item.safe ? item.why === "recent" : item.why === "")));
});

test("an approved plan is a safe decision; a refused one is not listed", () => {
  const plan = [
    { role: "user", text: "Go ahead." },
    { role: "assistant", text: "", toolUses: [{ tool: "ExitPlanMode", input: { plan: "# Add rate limiting\n\n1. Do it" }, text: "approved" }] },
    { role: "assistant", text: "", toolUses: [{ tool: "ExitPlanMode", input: { plan: "# Something else" }, isError: true, text: "rejected" }] },
  ];
  const decisions = kinds(buildKeepList(plan), "decision");
  assert.equal(decisions.length, 1);
  assert.equal(decisions[0].text, "Add rate limiting");
  assert.equal(decisions[0].safe, true);
  assert.equal(decisions[0].why, "plan");
});

test("what CLAUDE.md already says is safe anyway", () => {
  const claudeMd = "# Rules\n- Never use port 8765 for the dev server.\n- Use pnpm.\n";
  const items = buildKeepList(planted(), { claudeMd });
  const port = kinds(items, "instruction").find((item) => /8765/.test(item.text));
  assert.equal(port.safe, true);
  assert.equal(port.why, "claude-md");
  const code = kinds(items, "instruction").find((item) => /read any code/.test(item.text));
  assert.equal(code.safe, false);
});

test("an error run again with success is resolved", () => {
  const messages = [
    ...planted(),
    { role: "assistant", text: "", toolUses: [{ tool: "Bash", input: { command: "pytest tests/test_limiter.py" }, text: "3 passed" }] },
  ];
  assert.equal(kinds(buildKeepList(messages), "error").length, 0);
});

test("values the user typed: urls, versions, ids and numbers", () => {
  const messages = [
    { role: "user", text: "The staging API is at https://staging.example.com/v2, pin node 22.4.1. Ticket ABC-123 has the details. The batch size is 5000." },
    { role: "user", text: "It was released in 2026." },
  ];
  const values = kinds(buildKeepList(messages), "value").map((item) => item.exact[0]);
  for (const wanted of ["https://staging.example.com/v2", "22.4.1", "ABC-123", "5000"]) assert.ok(values.includes(wanted), `${wanted} in ${values.join(",")}`);
  assert.ok(!values.includes("2026"), "a year is not a value");
});

test("corrections are instructions", () => {
  const items = buildKeepList([
    { role: "user", text: "No, call it the staging database." },
    { role: "user", text: "Actually the config lives in app.toml" },
  ]);
  assert.equal(kinds(items, "instruction").length, 2);
});

test("questions, summaries, system notes and tool-only messages are ignored", () => {
  const messages = [
    { role: "user", text: "Why don't we use port 9000?" },
    { role: "user", text: "This session is being continued from a previous conversation. Never use port 1111." },
    { role: "user", text: "<system-reminder>Never do X</system-reminder>" },
    { role: "user", text: "", toolResults: [{ tool_use_id: "a", text: "Never mind", isError: false }] },
  ];
  const items = buildKeepList(messages);
  assert.deepEqual(items.filter((item) => item.kind !== "value").map((item) => item.text), []);
  // A number in a question is still something the user typed; nothing from the summary or the system note is.
  assert.deepEqual(items.map((item) => item.exact[0]), ["9000"]);
});

test("pasted text and a skill's or command's own text give no instructions", () => {
  const pasted = "Never use port 4444. " + "log line with a value 12345 and more words. ".repeat(60);
  assert.ok(pasted.length > MAX_USER_MESSAGE);
  const messages = [
    { role: "user", text: pasted },
    { role: "user", text: "Base directory for this skill: /home/me/.claude/skills/review\n\nNever skip the tests. Always run the linter." },
    { role: "user", text: "Launching skill: review. Always run the linter." },
    { role: "user", text: "Review this.\n<command-name>/review</command-name>\n<command-args>never touch the schema</command-args>" },
    { role: "user", text: "Never push to main." },
  ];
  const items = buildKeepList(messages);
  assert.deepEqual(items.map((item) => item.text), ["Never push to main."]);
  // Just under the limit is still the user's own message.
  const long = ("Always run the linter. " + "x".repeat(40) + " ").repeat(1).padEnd(MAX_USER_MESSAGE, "y");
  assert.equal(buildKeepList([{ role: "user", text: long }]).filter((item) => item.kind === "instruction").length, 1);
});

test("file recency counts the files listed, not the repeated edits skipped", () => {
  const edit = (path) => ({ role: "assistant", text: "", toolUses: [{ tool: "Edit", input: { file_path: path }, text: "ok" }] });
  // Seven files, each edited twice in a row: the two edits of a file are one item.
  const messages = [];
  for (let n = 1; n <= 7; n++) messages.push(edit(`src/f${n}.ts`), edit(`src/f${n}.ts`));
  const files = kinds(buildKeepList(messages), "file");
  assert.equal(files.length, 7);
  assert.equal(files.filter((item) => item.safe).length, 5);
  assert.deepEqual(files.filter((item) => !item.safe).map((item) => item.text), ["src/f2.ts", "src/f1.ts"]);
});

test("pins come first and survive; each kind is capped; texts are one short line", () => {
  const lines = [];
  for (let i = 0; i < 30; i++) lines.push(`Never touch module${i}.`);
  const long = "Always " + "word ".repeat(40) + "end.";
  const messages = [...lines.map((text) => ({ role: "user", text })), { role: "user", text: long }];
  const items = buildKeepList(messages, { pins: [{ text: "keep the\nnotes" }] });
  assert.equal(items[0].kind, "pin");
  assert.equal(items[0].text, "keep the notes");
  assert.equal(kinds(items, "instruction").length, LIMITS.instruction);
  for (const item of items) {
    assert.ok(item.text.length <= MAX_TEXT, item.text);
    assert.ok(!/\n/.test(item.text));
  }
});

test("deterministic: the same messages give the same list and ids", () => {
  assert.deepEqual(buildKeepList(planted()), buildKeepList(planted()));
  assert.equal(itemId("instruction", "Never  use port 8765"), itemId("instruction", "never use port 8765"));
  assert.notEqual(itemId("pin", "x"), itemId("instruction", "x"));
});

test("suggestions: pins first, then instructions and decisions (newest first), at most 7; the rest is under more", () => {
  const items = buildKeepList(planted(), { pins: [{ text: "remember the staging key rotation" }] });
  const { suggested, more } = splitSuggested(items, 7);
  assert.equal(suggested[0].kind, "pin");
  assert.ok(suggested.length <= 7);
  assert.ok(suggested.every((item) => ["pin", "instruction", "decision"].includes(item.kind)));
  assert.ok(more.some((item) => item.kind === "task" || item.kind === "error"));
  assert.equal(more.some((item) => item.safe), false);

  const crowded = buildKeepList(Array.from({ length: 12 }, (_, i) => ({ role: "user", text: `Always check item${i}.` })));
  assert.equal(splitSuggested(crowded).suggested.length, 7);
});

test("helpers: content words and exact tokens", () => {
  assert.deepEqual(contentWords("Never use the port 8765 for reading"), ["use", "port", "read"]);
  assert.deepEqual(exactTokens("see src/a/b.ts and v1.2.3 on port 8765 at https://x.dev/y."), ["https://x.dev/y", "src/a/b.ts", "v1.2.3", "8765"]);
  assert.ok(trimText("a ".repeat(100), 20).length <= 20);
});

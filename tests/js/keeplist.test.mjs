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

test("what the user says about what they can see or know is not an instruction", () => {
  const said = (text) => kinds(buildKeepList([{ role: "user", text }]), "instruction").map((item) => item.text);
  assert.deepEqual(said("I dont really see anything"), []);
  assert.deepEqual(said("I don't know why it fails"), []);
  assert.deepEqual(said("i really can't understand this"), []);
  assert.deepEqual(said("Why don't you use the cache?"), []);
  assert.deepEqual(said("What should I never do here?"), []);
  assert.deepEqual(said("don't read code yet"), ["don't read code yet"]);
  assert.deepEqual(said("I don't want you to touch the tests"), ["I don't want you to touch the tests"]);
  assert.deepEqual(said("we never use port 8765"), ["we never use port 8765"]);
  // Only the "I don't know" clause is dropped: an instruction in the same sentence stays.
  assert.deepEqual(said("I don't know why it fails, so never touch the config."), ["I don't know why it fails, so never touch the config."]);
  assert.deepEqual(said("I don't like tabs, always use spaces."), ["I don't like tabs, always use spaces."]);
  assert.deepEqual(said("I don't think you should touch the tests"), ["I don't think you should touch the tests"]);
  assert.deepEqual(said("I dont really see anything"), []);
  // One message can hold both: the talk is dropped, the constraint stays.
  assert.deepEqual(said("I don't see the button. Never rename the schema."), ["Never rename the schema."]);
});

test("the error is a shell command that failed, never another tool's error or a refusal", () => {
  const call = (tool, text, input = {}) => ({ role: "assistant", text: "", toolUses: [{ tool, input, isError: true, text }] });
  const errors = (messages) => kinds(buildKeepList(messages), "error").map((item) => item.text);
  // The user's own failing commands count, in either shell.
  assert.equal(errors([call("Bash", "FAILED tests/a.py::test_x - AssertionError", { command: "pytest" })]).length, 1);
  assert.equal(errors([call("PowerShell", "npm ERR! missing script: build", { command: "npm run build" })]).length, 1);
  // Failures without an upper-case marker are the user's too.
  assert.equal(errors([call("Bash", "FAIL src/a.test.js\n  1 failed, 3 passed", { command: "npx jest" })]).length, 1);
  assert.equal(errors([call("Bash", "--- FAIL: TestParse (0.00s)\nFAIL", { command: "go test ./..." })]).length, 1);
  assert.match(errors([call("Bash", "bash: foo: command not found", { command: "foo" })])[0], /command not found/);
  assert.match(errors([call("PowerShell", "Get-Content : Cannot find path 'C:/x' because it does not exist." + String.fromCharCode(10) + "+ CategoryInfo          : ObjectNotFound", { command: "Get-Content C:/x" })])[0], /CategoryInfo/);
  assert.equal(errors([call("PowerShell", "foo : The term 'foo' is not recognized as the name of a cmdlet", { command: "foo" })]).length, 1);
  assert.equal(errors([call("Bash", "exit code 1\nsomething odd happened", { command: "make" })]).length, 1);
  assert.equal(errors([call("Bash", "bash: ./run.sh: Permission denied", { command: "./run.sh" })]).length, 1);
  // A refusal (the user said no, a permission rule or a hook stopped it) is not a failure.
  assert.deepEqual(errors([call("Bash", "Permission to use Bash with command rm -rf build has been denied.", { command: "rm -rf build" })]), []);
  assert.deepEqual(errors([call("PowerShell", "The user rejected this tool use. The tool use was rejected by the user.", { command: "x" })]), []);
  assert.deepEqual(errors([call("Bash", "PreToolUse:Bash hook blocked this call: no force pushes", { command: "git push -f" })]), []);
  // A tool that failed for Claude, not the user: an MCP call, a refusal, a missing tool, a failed edit.
  assert.deepEqual(errors([call("mcp__computer-use__request_access", "Error: user denied access", { applications: ["Notepad"] })]), []);
  assert.deepEqual(errors([call("Bash", "The user doesn't want to proceed with this tool use.", { command: "rm -rf build" })]), []);
  assert.deepEqual(errors([call("Edit", "Error: File has not been read yet.", { file_path: "a.ts" })]), []);
  assert.deepEqual(errors([call("Read", "Error: no such file", { file_path: "a.ts" })]), []);
  // A shell failure still wins over a later error of another tool.
  const both = [call("Bash", "fatal: not a git repository", { command: "git status" }), call("mcp__x__y", "Error: boom")];
  assert.match(errors(both)[0], /git status/);
});

test("files in temp or scratch places and plan files are left out; a project file is shown relative", () => {
  const edit = (path) => ({ role: "assistant", text: "", toolUses: [{ tool: "Edit", input: { file_path: path }, text: "ok" }] });
  const names = (paths, cwd) => kinds(buildKeepList(paths.map(edit), { cwd }), "file").map((item) => item.text);
  const scratch = "C:\\Users\\paulm\\AppData\\Local\\Temp\\claude\\proj\\scratchpad\\notes.md";
  assert.deepEqual(names([scratch]), []);
  assert.deepEqual(names(["/tmp/run/out.txt", "/home/me/.claude/plans/plan-1.md", "C:/Users/me/AppData/Local/TEMP/x.json"]), []);
  assert.deepEqual(names(["/work/proj/tmp/cache.ts"]), []);
  // Under the working directory only the part below it counts, so a project that lives in a temp folder is listed.
  assert.deepEqual(names(["/tmp/proj/src/a.ts", "/tmp/proj/tmp/b.ts"], "/tmp/proj"), ["src/a.ts"]);
  // A skipped file takes no place among the recent ones.
  const many = ["a", "b", "c", "d", "e", "f"].map((n) => `src/${n}.ts`);
  const files = kinds(buildKeepList([...many.map(edit), edit("/tmp/z.ts")]), "file");
  assert.equal(files.length, 6);
  assert.equal(files.filter((item) => item.safe).length, 5);

  // A path under the working directory is shown relative to it (either slash, any case on a drive letter).
  assert.deepEqual(names(["C:\\Dev\\claude-token-lens\\plugin\\hooks\\band.tsx"], "C:\\Dev\\claude-token-lens"), ["plugin/hooks/band.tsx"]);
  assert.deepEqual(names(["c:/dev/claude-token-lens/src/a.py"], "C:\\Dev\\claude-token-lens\\"), ["src/a.py"]);
  assert.deepEqual(names(["/work/proj/src/a.ts"], "/work/proj"), ["src/a.ts"]);
  // Not under it: as before, the path with forward slashes.
  assert.deepEqual(names(["/other/place/a.ts"], "/work/proj"), ["/other/place/a.ts"]);
  assert.deepEqual(names(["/work/projector/a.ts"], "/work/proj"), ["/work/projector/a.ts"]);
  // The summary is still checked against the full path.
  const [item] = kinds(buildKeepList([edit("/work/proj/src/a.ts")], { cwd: "/work/proj" }), "file");
  assert.deepEqual(item.exact, ["/work/proj/src/a.ts", "a.ts"]);
  // Long paths outside it are cut to their last three segments.
  const long = "/" + "deep/".repeat(30) + "a/b/c.ts";
  assert.equal(names([long], "/work/proj")[0], "…/a/b/c.ts");
});

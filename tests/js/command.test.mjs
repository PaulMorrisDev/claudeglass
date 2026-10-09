// Tests for the readable-command helper (plugin/hooks/lib/command.js), which the keep list's failed
// command and the Now page's heaviest items share.
//
//   node --test tests/js/command.test.mjs

import test from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath, pathToFileURL } from "node:url";
import { dirname, join } from "node:path";

const lib = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "plugin", "hooks", "lib");
const { commandLabel, bareCommand } = await import(pathToFileURL(join(lib, "command.js")).href);
const { toolWhat } = await import(pathToFileURL(join(lib, "context.js")).href);
const { buildKeepList } = await import(pathToFileURL(join(lib, "keeplist.js")).href);

test("the set-up a command starts with is dropped: assignments, quoted ones, and cd hops", () => {
  // An unquoted value stops at `&`, so `&&` is the separator and not part of the value.
  assert.equal(commandLabel({ command: "T=/c/x&&npm test" }), "npm test");
  assert.equal(commandLabel({ command: "T=/c/x;npm test" }), "npm test");
  assert.equal(commandLabel({ command: "cd /a && T=1 npm run build" }), "npm run build");
  assert.equal(commandLabel({ command: 'A="x y" B=2 make' }), "make");
  assert.equal(commandLabel({ command: "A='x y' make" }), "make");
  assert.equal(commandLabel({ command: 'cd "/my dir"; make' }), "make");
  assert.equal(commandLabel({ command: "cd /a && cd /b && node run.js --flag" }), "node run.js --flag");
  // Only the start is touched: an assignment or a cd later in the command stays.
  assert.equal(commandLabel({ command: "make X=1 && cd out" }), "make X=1 && cd out");
  // A command that is only set-up keeps itself.
  assert.equal(bareCommand("cd /w &&"), "cd /w &&");
  assert.equal(commandLabel({ command: "A=1" }), "A=1");
});

test("the description wins, and the limits are the caller's", () => {
  assert.equal(commandLabel({ description: "Run the plugin tests", command: "T=/c/x&&npm test" }), "Run the plugin tests");
  assert.equal(commandLabel({ command: "echo " + "x".repeat(100) }, 10), "echo xxxxx");
  assert.equal(commandLabel({ description: "d".repeat(100), command: "ls" }, 10, 50).length, 50);
  assert.equal(commandLabel({ description: "d".repeat(100) }, 10).length, 10);
  assert.equal(commandLabel({}), "");
  assert.equal(commandLabel(null), "");
  assert.equal(commandLabel({ command: 5 }), "");
});

test("the heaviest items and the keep list say the same words for the same call", () => {
  const input = { command: "cd /c/Dev/app && T=/c/x&&npm test" };
  assert.equal(toolWhat("Bash", input), "npm test");
  const messages = [{ role: "assistant", text: "", toolUses: [{ tool: "Bash", input, isError: true, text: "Exit code 2" }] }];
  const error = buildKeepList(messages).find((item) => item.kind === "error");
  assert.equal(error.text, "npm test failed: Exit code 2");
});

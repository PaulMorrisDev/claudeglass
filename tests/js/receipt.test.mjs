// Tests for the receipt of a re-add (plugin/hooks/lib/receipt.js): whether the replies that follow
// reference the items put back.
//
//   node --test tests/js/receipt.test.mjs

import test from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath, pathToFileURL } from "node:url";
import { dirname, join } from "node:path";

const lib = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "plugin", "hooks", "lib");
const { settleReceipts, RECEIPT_REPLIES } = await import(pathToFileURL(join(lib, "receipt.js")).href);
const { itemId } = await import(pathToFileURL(join(lib, "keeplist.js")).href);

/** A put-back item. */
function item(kind, text, exact = []) {
  return { id: itemId(kind, text), kind, text, safe: false, why: "", exact, at: 0 };
}

const PORT = item("instruction", "Never use port 8765 for the dev server, it is reserved", ["8765"]);
const FILE = item("file", "src/api/limiter.ts", ["src/api/limiter.ts", "limiter.ts"]);

/** A review that put `items` back at time 1000, none of them seen yet. */
function review(...items) {
  const receipts = {};
  for (const one of items) receipts[one.id] = { at: 1000, replies: 0, referenced: null };
  return { readded: items, receipts };
}

test("a reply that says the item marks it referenced on the first reply", () => {
  const before = review(PORT);
  const after = settleReceipts(before, ["I will keep the dev server off port 8765, since it is reserved and must never be used."]);
  assert.notEqual(after, before);
  assert.deepEqual(after.receipts[PORT.id], { at: 1000, replies: 1, referenced: true });
  assert.deepEqual(before.receipts[PORT.id], { at: 1000, replies: 0, referenced: null });
});

test("an unrelated reply leaves the item unseen and counts", () => {
  let now = review(PORT);
  now = settleReceipts(now, ["Renamed the helper and ran the formatter."]);
  assert.deepEqual(now.receipts[PORT.id], { at: 1000, replies: 1, referenced: null });
  now = settleReceipts(now, ["Added a changelog entry.", "Done."]);
  assert.deepEqual(now.receipts[PORT.id], { at: 1000, replies: 2, referenced: null });
});

test("the tenth reply with no match marks the item not referenced", () => {
  assert.equal(RECEIPT_REPLIES, 10);
  let now = review(PORT);
  for (let reply = 1; reply < RECEIPT_REPLIES; reply++) {
    now = settleReceipts(now, ["Something else entirely."]);
    assert.equal(now.receipts[PORT.id].referenced, null);
  }
  now = settleReceipts(now, ["Something else entirely."]);
  assert.deepEqual(now.receipts[PORT.id], { at: 1000, replies: 10, referenced: false });
});

test("the limit can be given", () => {
  let now = review(PORT);
  now = settleReceipts(now, ["Nothing."], 2);
  assert.equal(now.receipts[PORT.id].referenced, null);
  now = settleReceipts(now, ["Nothing."], 2);
  assert.equal(now.receipts[PORT.id].referenced, false);
});

test("a match on the last reply still counts as referenced", () => {
  let now = review(PORT);
  for (let reply = 1; reply < RECEIPT_REPLIES; reply++) now = settleReceipts(now, ["Nothing."]);
  now = settleReceipts(now, ["Keeping off port 8765, which must never be used."]);
  assert.deepEqual(now.receipts[PORT.id], { at: 1000, replies: 10, referenced: true });
});

test("a tool input with the file's path is a reference to a file item", () => {
  const input = JSON.stringify({ file_path: "src/api/limiter.ts", old_string: "a", new_string: "b" });
  const after = settleReceipts(review(FILE), ["", input]);
  assert.equal(after.receipts[FILE.id].referenced, true);
  // A path with backslashes, as a Windows tool call carries it, is the same file.
  const win = JSON.stringify({ file_path: String.raw`C:\work\src\api\limiter.ts` });
  assert.equal(settleReceipts(review(FILE), [win]).receipts[FILE.id].referenced, true);
  // A file item needs the exact token: a near name is not it.
  assert.equal(settleReceipts(review(FILE), [JSON.stringify({ file_path: "src/api/limiter.test.ts" })]).receipts[FILE.id].referenced, null);
});

test("each item settles on its own", () => {
  const after = settleReceipts(review(PORT, FILE), ["Edited src/api/limiter.ts"]);
  assert.equal(after.receipts[FILE.id].referenced, true);
  assert.deepEqual(after.receipts[PORT.id], { at: 1000, replies: 1, referenced: null });
});

test("a settled receipt is never changed again", () => {
  const settled = { readded: [PORT, FILE], receipts: { [PORT.id]: { at: 1000, replies: 3, referenced: true }, [FILE.id]: { at: 1000, replies: 10, referenced: false } } };
  assert.equal(settleReceipts(settled, ["Never use port 8765, it is reserved", "src/api/limiter.ts"]), settled);
  assert.equal(settleReceipts(settled, []), settled);
});

test("a receipt whose item is no longer put back is left as it is", () => {
  const gone = { readded: [], receipts: { [PORT.id]: { at: 1000, replies: 4, referenced: null } } };
  assert.equal(settleReceipts(gone, ["port 8765 must never be used"]), gone);
});

test("unchanged input returns the same object", () => {
  const empty = { readded: [], receipts: {} };
  assert.equal(settleReceipts(empty, ["anything"]), empty);
  const none = { readded: [PORT], receipts: {} };
  assert.equal(settleReceipts(none, ["port 8765"]), none);
});

test("the reply counts even when it said nothing", () => {
  const after = settleReceipts(review(PORT), []);
  assert.deepEqual(after.receipts[PORT.id], { at: 1000, replies: 1, referenced: null });
});

test("the review it is given is not changed", () => {
  const before = review(PORT);
  const frozen = JSON.stringify(before);
  settleReceipts(before, ["port 8765 must never be used"]);
  assert.equal(JSON.stringify(before), frozen);
});

// Tests for the draft checks (plugin/hooks/lib/draft.js) and /glass tighten's
// words and diff (plugin/hooks/lib/tighten.js).
//
//   node --test tests/js/draft.test.mjs

import test from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath, pathToFileURL } from "node:url";
import { dirname, join } from "node:path";

const lib = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "plugin", "hooks", "lib");
const draft = await import(pathToFileURL(join(lib, "draft.js")).href);
const tighten = await import(pathToFileURL(join(lib, "tighten.js")).href);
const { checkDraft, draftFacts, emptyBag, learnPrompt, learnFile, contentWords, hasContinuation, namesTarget, asksToReadAll, isSlash, stem, STALE_TOKENS, pickDraft, factsKey, carriedTokens, MAX_BAG_WORDS, addWords } = draft;

/** A session about a billing parser and a payment webhook. */
function session() {
  let bag = emptyBag();
  for (const prompt of [
    "Fix the failing test in the billing parser, it crashes on empty invoice lines",
    "Add retry logic to the payment webhook handler so a timeout is tried again with backoff",
    "The invoice totals are wrong when a discount applies to a subscription renewal",
  ]) {
    bag = learnPrompt(bag, prompt);
  }
  bag = learnFile(bag, "C:\\work\\src\\invoice_parser.py");
  bag = learnFile(bag, "/work/src/webhookHandler.ts");
  return bag;
}

const BIG = 80000;
const kind = (text, over = {}) => checkDraft({ text, bag: session(), files: 3, ctx: BIG, ...over });

// -- words ----------------------------------------------------------------------

test("stems meet: plurals, ing and ed", () => {
  assert.equal(stem("tests"), stem("testing"));
  assert.equal(stem("tests"), stem("tested"));
  assert.equal(stem("files"), stem("filed"));
  assert.equal(stem("settings"), stem("setting"));
});

test("content words split identifiers and leave out stop words and short words", () => {
  const words = contentWords("Update the parseSettings function in band_test.tsx to use it");
  assert.ok(words.includes("pars"), words.join());
  assert.ok(words.includes("sett"), words.join());
  assert.ok(words.includes("band"), words.join());
  for (const gone of ["the", "update", "function", "tsx", "to", "use", "it", "in"]) assert.ok(!words.includes(gone), gone);
});

test("a file's words are its name, not its folder or extension", () => {
  assert.deepEqual(draft.fileWords("C:\\Dev\\claude-token-lens\\plugin\\hooks\\band.tsx"), ["band"]);
  assert.deepEqual(draft.fileWords("src/billing/invoiceParser.test.ts"), ["invoic", "parser", "test"]);
  assert.deepEqual(draft.fileWords(""), []);
});

test("the bag is a copy and drops its oldest words past the cap", () => {
  const first = addWords(emptyBag(), ["alpha", "beta"]);
  const second = addWords(first, ["gamma"]);
  assert.deepEqual(Object.keys(first), ["alpha", "beta"]);
  assert.deepEqual(Object.keys(second), ["alpha", "beta", "gamma"]);
  let big = emptyBag();
  const many = Array.from({ length: MAX_BAG_WORDS + 50 }, (_, i) => "word" + i);
  big = addWords(big, many);
  assert.equal(Object.keys(big).length, MAX_BAG_WORDS);
  assert.ok(!("word0" in big) && "word" + (MAX_BAG_WORDS + 49) in big);
});

test("the stale size is the dashboard's", () => {
  assert.equal(STALE_TOKENS, 20000);
});

// -- a new topic ----------------------------------------------------------------

const NEW_TOPICS = [
  "Write a bash script that rotates nginx access logs weekly and uploads the archives to an S3 bucket",
  "Explain how Kubernetes horizontal pod autoscaling picks replica counts from CPU metrics",
  "Draft a README section describing the GraphQL schema for our mobile onboarding flow and the caching rules",
  "Design a Postgres table for storing sensor readings with hourly rollups and a retention policy",
];

for (const text of NEW_TOPICS) {
  test(`a new topic in a big context: ${text.slice(0, 40)}`, () => {
    assert.equal(kind(text), "draft_topic");
  });
}

const NOT_NEW = [
  ["ok go ahead", "a go-ahead"],
  ["yes do that", "agreement"],
  ["now run the tests", "a short follow-up"],
  ["fix the failing one", "a short follow-up"],
  ["Also update the invoice parser tests to cover the empty line case and the discount renewal", "shares the session's words"],
  ["Refactor the webhook handler so retries use exponential backoff and the invoice parser logs errors", "shares the session's words"],
  ["Write a bash script that rotates nginx access logs weekly, same as before, and uploads them to S3", "points back with 'same'"],
  ["Can you explain why the Kubernetes autoscaler picks those replica counts from the CPU metrics above", "points back with 'above'"],
  ["Make it work for the Postgres sensor readings table with hourly rollups and a retention policy", "points back with 'it'"],
  ["The error says GraphQL schema mismatch during mobile onboarding so investigate the caching rules deeply", "the error"],
  ["/compact keep the decisions about the database schema and the migration order for next time", "a slash command"],
  ["> Kubernetes autoscaler picks replicas\nWhy does the horizontal pod autoscaling react to CPU metrics so slowly", "a quoted line"],
  ["Thanks. Now please write a bash script that rotates nginx access logs weekly and uploads archives", "opens with thanks and now"],
];

for (const [text, why] of NOT_NEW) {
  test(`not a new topic (${why}): ${text.slice(0, 40)}`, () => {
    assert.notEqual(kind(text), "draft_topic");
  });
}

test("a new topic needs a context worth clearing", () => {
  const text = NEW_TOPICS[0];
  assert.equal(kind(text, { ctx: STALE_TOKENS - 1 }), null);
  assert.equal(kind(text, { ctx: STALE_TOKENS }), "draft_topic");
});

test("a session that has said little can't tell a new topic", () => {
  assert.equal(kind(NEW_TOPICS[0], { bag: learnPrompt(emptyBag(), "Fix the parser") }), null);
});

test("a draft with fewer than six content words is never a new topic", () => {
  assert.equal(kind("Explain Kubernetes autoscaling metrics"), null);
});

test("pasted text placeholders don't make a topic", () => {
  assert.notEqual(kind("[Pasted text #1 +40 lines]"), "draft_topic");
});

// -- no file named --------------------------------------------------------------

const NO_PATHS = [
  "Please fix the bug where the login form rejects passwords that contain spaces",
  "Add a dark mode toggle to the settings screen and remember the choice between visits",
  "Rename the helper that formats prices so the naming is consistent across the whole store",
];
const NAMED = [
  "Please fix the bug in src/auth/login.ts where the form rejects passwords that contain spaces",
  "Fix the bug where `validatePassword` rejects passwords that contain spaces in the login form",
  "Fix the bug where validatePassword rejects passwords that contain spaces in the login form",
  "Fix the bug where validate_password rejects passwords that contain spaces in the login form",
  "Update login.tsx so that the form accepts passwords that contain spaces and trims nothing",
  "Fix the bug where parse() rejects passwords that contain spaces in the login form please",
  "Please fix C:\\work\\auth\\login so that passwords that contain spaces are accepted again",
];

for (const text of NO_PATHS) {
  test(`no file named: ${text.slice(0, 40)}`, () => {
    assert.equal(kind(text, { files: 0, ctx: 1000 }), "draft_no_paths");
  });
}
for (const text of NAMED) {
  test(`a file or name given: ${text.slice(0, 40)}`, () => {
    assert.equal(kind(text, { files: 0, ctx: 1000 }), null);
  });
}

test("no file hint once the session has touched a file", () => {
  assert.equal(kind(NO_PATHS[0], { files: 1, ctx: 1000 }), null);
});

test("no file hint needs a change asked for and eight words", () => {
  assert.equal(kind("What does the login form do when the password has spaces in it", { files: 0, ctx: 1000 }), null);
  assert.equal(kind("Fix the login form bug", { files: 0, ctx: 1000 }), null);
});

test("no file hint when the draft points back at the conversation", () => {
  assert.equal(kind("Fix it so that the form accepts passwords that contain spaces as well", { files: 0, ctx: 1000 }), null);
});

// -- read everything ------------------------------------------------------------

const READ_ALL = [
  "Read the whole codebase and tell me how authentication works",
  "Please explore the entire repo before answering",
  "Go through all the files in the project and list the TODOs",
  "Look at every file and summarise what each one does",
  "Read all of the source files in this repo first",
  "Scan the full project for unused exports",
];
const NOT_ALL = [
  "Read the auth module and tell me how login works",
  "Read all files in src/auth and explain the flow",
  "Look through all the files in the billing folder for the rounding bug",
  "Review the code I pasted above",
  "Explore how the project handles retries",
  "What is the whole point of this function",
];

for (const text of READ_ALL) {
  test(`reads everything: ${text.slice(0, 40)}`, () => {
    assert.equal(asksToReadAll(text), true);
    assert.equal(kind(text, { bag: emptyBag(), files: 5, ctx: 1000 }), "draft_read_all");
  });
}
for (const text of NOT_ALL) {
  test(`does not read everything: ${text.slice(0, 40)}`, () => {
    assert.equal(asksToReadAll(text), false);
  });
}

// -- the pieces -----------------------------------------------------------------

test("priority: a new topic, then no file named, then read everything", () => {
  const all = { topic: true, noPaths: true, readAll: true, words: 9, shared: 0 };
  assert.equal(pickDraft(all, BIG), "draft_topic");
  assert.equal(pickDraft(all, 100), "draft_no_paths");
  assert.equal(pickDraft({ ...all, noPaths: false }, 100), "draft_read_all");
  assert.equal(pickDraft({ ...all, topic: false, noPaths: false, readAll: false }, BIG), null);
});

test("an empty draft or a slash command has no facts", () => {
  for (const text of ["", "   ", "/clear", "/compact keep the database schema and the migration order decisions"]) {
    const facts = draftFacts(text, session(), 0);
    assert.equal(factsKey(facts), "---", text);
  }
});

test("a path is not a slash command", () => {
  assert.equal(isSlash("/clear"), true);
  assert.equal(isSlash("/glass tighten"), true);
  assert.equal(isSlash("/usr/local/bin/tool needs a rewrite so that it is faster"), false);
});

test("continuation markers", () => {
  for (const text of ["do that again", "the error is back", "same thing for the other file", "as discussed", "you said the cache was stale"]) {
    assert.equal(hasContinuation(text), true, text);
  }
  for (const text of ["Write a script that parses nginx logs", "Design a Postgres table for sensor readings"]) {
    assert.equal(hasContinuation(text), false, text);
  }
});

test("names a target", () => {
  for (const text of ["open src/app.ts", "edit `foo`", "call getUser now", "fix parse_args", "see README.md", "Fix GitHub issues"]) {
    assert.equal(namesTarget(text), true, text);
  }
  assert.equal(namesTarget("fix the login bug"), false);
  assert.equal(namesTarget("e.g. do the thing"), false);
});

test("what a fresh start saves is the context less the startup", () => {
  assert.equal(carriedTokens(90000, 20000), 70000);
  assert.equal(carriedTokens(10000, 20000), 0);
  assert.equal(carriedTokens(90000, null), 90000);
});

test("the same input gives the same answer", () => {
  const text = NEW_TOPICS[1];
  assert.equal(kind(text), kind(text));
  const bag = session();
  const before = JSON.stringify(bag);
  draftFacts(text, bag, 0);
  assert.equal(JSON.stringify(bag), before);
});

// -- tighten --------------------------------------------------------------------

test("the rewrite is cleaned of marks, fences and quotes", () => {
  assert.equal(tighten.cleanRewrite("<prompt>\nFix login.\n</prompt>"), "Fix login.");
  assert.equal(tighten.cleanRewrite("```\nFix login.\n```"), "Fix login.");
  assert.equal(tighten.cleanRewrite('"Fix login."'), "Fix login.");
  assert.equal(tighten.cleanRewrite("  Fix login.  "), "Fix login.");
  assert.equal(tighten.cleanRewrite(""), "");
});

test("the prompt Haiku gets holds the draft between marks, and the instruction says what to keep", () => {
  assert.match(tighten.tightenPrompt("Fix it"), /<prompt>\nFix it\n<\/prompt>/);
  assert.match(tighten.TIGHTEN_SYSTEM, /Keep every requirement, path, name, value and constraint/);
  assert.match(tighten.TIGHTEN_SYSTEM, /Output only the rewritten prompt/);
  assert.equal(tighten.tightenMaxTokens("x".repeat(30)), 210);
  assert.equal(tighten.tightenMaxTokens("x".repeat(100000)), 4000);
});

test("a diff marks what went and what came, a sentence to a line", () => {
  const before = "Please could you look at the login form. It rejects passwords with spaces. Fix that.";
  const after = "Fix the login form rejecting passwords with spaces.";
  const diff = tighten.unifiedDiff(before, after);
  assert.equal(diff.split("\n")[0], "@@ -1,3 +1,1 @@");
  assert.deepEqual(diff.split("\n").slice(1), ["-Please could you look at the login form.", "-It rejects passwords with spaces.", "-Fix that.", "+Fix the login form rejecting passwords with spaces."]);
});

test("a diff keeps what both share as context", () => {
  const diff = tighten.unifiedDiff("One.\nTwo.\nThree.", "One.\nThree.\nFour.");
  assert.deepEqual(diff.split("\n").slice(1), [" One.", "-Two.", " Three.", "+Four."]);
});

test("a diff of nothing against something and back", () => {
  assert.equal(tighten.unifiedDiff("", "Hi"), "@@ -0,0 +1,1 @@\n+Hi");
  assert.equal(tighten.unifiedDiff("Hi", ""), "@@ -1,1 +0,0 @@\n-Hi");
});

test("words are counted by whitespace", () => {
  assert.equal(tighten.words("  one two\nthree "), 3);
  assert.equal(tighten.words(""), 0);
});

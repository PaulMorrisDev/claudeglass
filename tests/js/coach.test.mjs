// Tests for the coaching decisions made before a message is sent
// (plugin/hooks/lib/coach.js).
//
//   node --test tests/js/coach.test.mjs
//
// tests/test_mod_coach.py runs this file with COACH_VECTORS set to a JSON
// file of cases built by calling the capture hook's own functions
// (_is_status, _drip_count, _cold_hint, _fresh_hint, _reminder_due, ...),
// so the decisions are held to the Python they port.

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath, pathToFileURL } from "node:url";
import { dirname, join } from "node:path";

const root = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "plugin", "hooks");
const coach = await import(pathToFileURL(join(root, "lib", "coach.js")).href);
const rest = await import(pathToFileURL(join(root, "lib", "rest.js")).href);
const { PATTERNS, THRESHOLDS } = await import(pathToFileURL(join(root, "generated", "coaching.js")).href);
const {
  HINT_ORDER,
  READ_CHARS,
  BACKGROUND_FORGET_S,
  compilePatterns,
  isStatus,
  isGo,
  isQuestion,
  isChangeRequest,
  endsOnQuestion,
  changesFile,
  isFeedbackPrompt,
  marksNewPiece,
  modelFamily,
  charsPerToken,
  emptyDraft,
  editDraft,
  draftShape,
  pasteHint,
  dripCount,
  dripHint,
  pollHint,
  coldHint,
  pickHint,
  mergeRest,
  startingContext,
  freshHint,
  projectApplies,
  projectSlug,
  latestReminder,
  ratedSince,
  emptyBackground,
  backgroundLaunched,
  backgroundNotified,
  launchedInBackground,
  isNotification,
} = coach;

const p = compilePatterns(PATTERNS);
const TH = THRESHOLDS;
const NOW = 1_700_000_000;

/** The prompt box after typing `text` at the end of `before`. */
function typed(before, text) {
  return { edit: { text: before, start: before.length, end: before.length, inputText: text }, after: { text: before + text } };
}

function draftOf(text) {
  const { edit, after } = typed("", text);
  return editDraft(emptyDraft(), edit, after, p, TH.drip_chars);
}

// -- what a message is ------------------------------------------------------------

test("a status check, a go-ahead, a question and a change request are told apart", () => {
  assert.equal(isStatus("how is it going?", p), true);
  assert.equal(isStatus("rename foo to bar", p), false);
  assert.equal(isGo("go ahead", p), true);
  assert.equal(isGo("rename foo to bar", p), false);
  assert.equal(isQuestion("what does this do", p), true);
  assert.equal(isQuestion("rename foo", p), false);
  assert.equal(isChangeRequest("rename foo to bar", p), true);
  assert.equal(isChangeRequest("can you rename foo?", p), false);
  assert.equal(isChangeRequest("thanks", p), false);
  assert.equal(isChangeRequest("", p), false);
});

test("a reply that ends on a question is told from one that doesn't", () => {
  assert.equal(endsOnQuestion("All done. Want me to also fix the tests?", p), true);
  assert.equal(endsOnQuestion("All done.", p), false);
  assert.equal(endsOnQuestion("Done. Run `foo?` to check.", p), false);
});

test("changesFile counts an edit outside .claude and a shell write, and nothing else", () => {
  assert.equal(changesFile("Edit", { file_path: "src/a.py" }, p), true);
  assert.equal(changesFile("Edit", { file_path: "/home/u/.claude/x.md" }, p), false);
  assert.equal(changesFile("Read", { file_path: "a.py" }, p), false);
  assert.equal(changesFile("Bash", { command: "echo hi > out.txt" }, p), true);
  assert.equal(changesFile("Bash", { command: "ls -la" }, p), false);
  assert.equal(changesFile("Bash", undefined, p), false);
});

test("isFeedbackPrompt matches the command alone or with arguments, not a longer name", () => {
  assert.equal(isFeedbackPrompt("/cg-feedback", "cg-feedback"), true);
  assert.equal(isFeedbackPrompt("  /cg-feedback 4 good", "cg-feedback"), true);
  assert.equal(isFeedbackPrompt("/cg-feedbacks", "cg-feedback"), false);
  assert.equal(isFeedbackPrompt("please /cg-feedback", "cg-feedback"), false);
});

test("marksNewPiece reads shift=new in the reply's tag only", () => {
  assert.equal(marksNewPiece("Done.\n[cg: task=feature size=s shift=new]"), true);
  assert.equal(marksNewPiece("Done.\n[cg: task=feature shift=fix]"), false);
  assert.equal(marksNewPiece("shift=new with no tag"), false);
});

// -- tokens -----------------------------------------------------------------------

test("modelFamily drops the 1m tag and the date, and knows no synthetic model", () => {
  assert.equal(modelFamily("claude-opus-4-6[1m]"), "claude-opus-4-6");
  assert.equal(modelFamily("claude-sonnet-4-5-20250929"), "claude-sonnet-4-5");
  assert.equal(modelFamily("<synthetic>"), "");
  assert.equal(modelFamily(null), "");
});

test("charsPerToken uses the model's figure, else the common family's, else 4", () => {
  const calibration = { text: { "claude-opus-4-6": 3.2, "claude-sonnet-4-6": 3.8 }, family: "claude-sonnet-4-6" };
  assert.equal(charsPerToken(calibration, "claude-opus-4-6"), 3.2);
  assert.equal(charsPerToken(calibration, "claude-haiku-9"), 4);
  assert.equal(charsPerToken(calibration, null), 3.8);
  assert.equal(charsPerToken(null, "claude-opus-4-6"), 4);
  assert.equal(charsPerToken({ text: { x: 0 }, family: "x" }, null), 4);
});

// -- the draft ---------------------------------------------------------------------

test("a short change request is small and a change; a status check is a status", () => {
  const change = draftOf("rename foo to bar");
  assert.deepEqual([change.small, change.change, change.status], [true, true, false]);
  const status = draftOf("how is it going?");
  assert.deepEqual([status.small, status.change, status.status], [true, false, true]);
  const question = draftOf("why is the build slow?");
  assert.deepEqual([question.small, question.change], [true, false]);
});

test("a draft past the small size is not a drip, and one past READ_CHARS is not read for words", () => {
  const big = draftOf("fix " + "word ".repeat(80));
  assert.equal(big.small, false);
  assert.equal(big.change, false);
  const huge = draftOf("rename ".repeat(300));
  assert.equal(huge.len > READ_CHARS, true);
  assert.deepEqual([huge.small, huge.change, huge.status], [false, false, false]);
});

test("clearing the box empties the draft", () => {
  const first = draftOf("rename foo");
  const cleared = editDraft(first, { text: "rename foo", start: 0, end: 10, inputText: "" }, { text: "" }, p, TH.drip_chars);
  assert.deepEqual(cleared, emptyDraft());
});

test("a paste the box shows in a shorter form counts for what was pasted", () => {
  const placeholder = "[Pasted text #1 +500 lines]";
  const pasted = "x".repeat(60_000);
  const after = editDraft(emptyDraft(), { text: "", start: 0, end: 0, inputText: pasted }, { text: placeholder }, p, TH.drip_chars);
  assert.equal(after.len, placeholder.length);
  assert.equal(after.hidden, 60_000 - placeholder.length);
  assert.equal(after.small, false);
  assert.equal(draftShape(after, 4, TH.big_paste_tokens).charAt(0), "b");
  assert.equal(pasteHint(after.len + after.hidden, 4, TH).kind, "big_paste");
});

test("taking the paste out of the box takes it out of the count", () => {
  const placeholder = "[Pasted text #1 +500 lines]";
  const before = "fix this " + placeholder;
  const held = editDraft(emptyDraft(), { text: "", start: 0, end: 0, inputText: "y".repeat(50_000) }, { text: placeholder }, p, TH.drip_chars);
  const typedOn = editDraft(held, { text: placeholder, start: 0, end: 0, inputText: "fix this " }, { text: before }, p, TH.drip_chars);
  assert.equal(typedOn.hidden, held.hidden);
  const removed = editDraft(typedOn, { text: before, start: 9, end: before.length, inputText: "" }, { text: "fix this " }, p, TH.drip_chars);
  assert.equal(removed.hidden, 0);
  assert.equal(removed.len, 9);
});

test("draftShape changes only when a hint could", () => {
  const a = draftOf("rename foo");
  const b = draftOf("rename foo to");
  assert.equal(draftShape(a, 4, TH.big_paste_tokens), draftShape(b, 4, TH.big_paste_tokens));
  assert.notEqual(draftShape(a, 4, TH.big_paste_tokens), draftShape(emptyDraft(), 4, TH.big_paste_tokens));
  assert.notEqual(draftShape(a, 4, TH.big_paste_tokens), draftShape(draftOf("how is it going?"), 4, TH.big_paste_tokens));
});

test("a keystroke on a 20,000 character draft is constant work", () => {
  let text = "a".repeat(20_000);
  let draft = editDraft(emptyDraft(), { text: "", start: 0, end: 0, inputText: text }, { text }, p, TH.drip_chars);
  const started = performance.now();
  for (let i = 0; i < 1000; i++) {
    const before = text;
    text = before + "b";
    draft = editDraft(draft, { text: before, start: before.length, end: before.length, inputText: "b" }, { text }, p, TH.drip_chars);
    draftShape(draft, 4, TH.big_paste_tokens);
  }
  const elapsed = performance.now() - started;
  assert.equal(draft.len, 21_000);
  // A generous bound: 1,000 keystrokes in under a second is under 1 ms each, 50 ms is the budget.
  assert.ok(elapsed < 1000, `1,000 keystrokes took ${elapsed.toFixed(1)} ms`);
});

// -- the hints ---------------------------------------------------------------------

test("big_paste needs about big_paste_tokens, counted at the calibrated rate", () => {
  assert.equal(pasteHint(0, 4, TH), null);
  assert.equal(pasteHint(39_999, 4, TH), null);
  assert.deepEqual(pasteHint(40_000, 4, TH), { kind: "big_paste", stake: 10_000, fields: { tokens: 10_000 } });
  assert.equal(pasteHint(40_000, 3, TH)?.kind, "big_paste");
  assert.equal(pasteHint(30_000, 3, TH)?.kind, "big_paste");
  assert.equal(pasteHint(29_999, 3, TH), null);
});

test("drip_feed needs drip_count small changes in a row, each answered with a file change", () => {
  const done = { answer: false, answered: true, change: true, small: true, edited: true, since: 120 };
  const draft = { small: true, change: true };
  assert.equal(dripHint([done], draft, 60, false, 90_000, TH), null);
  const found = dripHint([done, done], draft, 60, false, 90_000, TH);
  assert.deepEqual(found, { kind: "drip_feed", stake: 3, fields: { count: 3, ctx: 90_000 } });
  assert.equal(dripCount([done, { ...done, edited: false }, done], draft, 60, false, TH), 2);
  assert.equal(dripCount([done, done], { small: true, change: false }, 60, false, TH), 0);
  assert.equal(dripCount([done, done], draft, 60, true, TH), 0);
  assert.equal(dripCount([done, done], draft, null, false, TH), 0);
  assert.equal(dripCount([done, done], draft, TH.drip_window_minutes * 60 + 1, false, TH), 0);
  assert.equal(dripCount([done, { ...done, since: TH.drip_window_minutes * 60 + 1 }], draft, 60, false, TH), 1);
});

test("a question, a go-ahead or an unanswered message neither counts nor ends a drip", () => {
  const done = { answer: false, answered: true, change: true, small: true, edited: true, since: 120 };
  const draft = { small: true, change: true };
  const between = [
    done,
    { ...done, change: false, edited: false },
    { ...done, answered: false },
    { ...done, answer: true },
    done,
  ];
  assert.equal(dripCount(between, draft, 60, false, TH), 3);
});

test("status_poll needs a status check, a warm cache and background work still out", () => {
  const last = { ctx: 120_000, at: NOW - 60, ttl: 300 };
  assert.deepEqual(pollHint(true, last, NOW, true), { kind: "status_poll", stake: 120_000, fields: { ctx: 120_000 } });
  assert.equal(pollHint(false, last, NOW, true), null);
  assert.equal(pollHint(true, last, NOW, false), null);
  assert.equal(pollHint(true, null, NOW, true), null);
  assert.equal(pollHint(true, { ...last, at: NOW - 301 }, NOW, true), null);
});

test("cold_return needs a gap past the cache lifetime and a big enough context", () => {
  const last = { ctx: 150_000, at: NOW - 400, ttl: 300 };
  const none = { stopped: false, reset: null };
  const found = coldHint(last, NOW, false, none, TH);
  assert.equal(found?.kind, "cold_return");
  assert.equal(found?.stake, 150_000);
  assert.deepEqual(found?.fields, { idle: 400, ctx: 150_000 - TH.warm_prefix_tokens });
  assert.equal(coldHint({ ...last, at: NOW - 300 }, NOW, false, none, TH), null);
  assert.equal(coldHint({ ...last, ctx: TH.cold_min_tokens - 1 }, NOW, false, none, TH), null);
  assert.equal(coldHint({ ...last, ctx: TH.cold_min_tokens }, NOW, false, none, TH)?.kind, "cold_return");
  assert.equal(coldHint(null, NOW, false, none, TH), null);
});

test("cold_return is silent after a compaction, and after a usage limit you came back to early", () => {
  const last = { ctx: 150_000, at: NOW - 4000, ttl: 3600 };
  assert.equal(coldHint(last, NOW, true, { stopped: false, reset: null }, TH), null);
  assert.equal(coldHint(last, NOW, false, { stopped: true, reset: null }, TH), null);
  assert.equal(coldHint(last, NOW, false, { stopped: true, reset: NOW - 100 }, TH), null);
  assert.equal(coldHint(last, NOW, false, { stopped: true, reset: NOW - 3601 }, TH)?.kind, "cold_return");
  assert.equal(coldHint(last, NOW, false, { stopped: true, reset: NOW - 3600 }, TH), null);
});

// -- which hint shows ----------------------------------------------------------------

const found = (kind, stake = 10) => ({ kind, stake, fields: {} });

test("the order is the hook's: drip_feed, big_paste, status_poll, cold_return", () => {
  assert.deepEqual(HINT_ORDER, ["drip_feed", "big_paste", "status_poll", "cold_return"]);
  const all = HINT_ORDER.map(kind => found(kind));
  assert.equal(pickHint(all, [], { hints: {} }, NOW, TH)?.kind, "drip_feed");
  assert.equal(pickHint(all.slice(1), [], { hints: {} }, NOW, TH)?.kind, "big_paste");
  assert.equal(pickHint([null, null, found("status_poll"), found("cold_return")], [], { hints: {} }, NOW, TH)?.kind, "status_poll");
  assert.equal(pickHint([null, null, null, found("cold_return")], [], { hints: {} }, NOW, TH)?.kind, "cold_return");
  assert.equal(pickHint([null, null, null, null], [], { hints: {} }, NOW, TH), null);
});

test("a muted kind is passed over for the next", () => {
  const all = HINT_ORDER.map(kind => found(kind));
  assert.equal(pickHint(all, ["drip_feed"], { hints: {} }, NOW, TH)?.kind, "big_paste");
  assert.equal(pickHint(all, ["drip_feed", "big_paste", "status_poll"], { hints: {} }, NOW, TH)?.kind, "cold_return");
  assert.equal(pickHint(all, [...HINT_ORDER], { hints: {} }, NOW, TH), null);
});

test("a resting kind is passed over, and wakes when its stake has grown", () => {
  const state = rest.recordShown({ hints: {} }, "drip_feed", 3, NOW - 60, TH, false);
  assert.equal(pickHint([found("drip_feed", 3), found("big_paste")], [], state, NOW, TH)?.kind, "big_paste");
  assert.equal(pickHint([found("drip_feed", 3 * TH.rearm_factor), found("big_paste")], [], state, NOW, TH)?.kind, "drip_feed");
  const later = NOW + TH.cooldown_minutes * 60;
  assert.equal(pickHint([found("drip_feed", 3), found("big_paste")], [], state, later, TH)?.kind, "drip_feed");
});

test("a kind marked once never shows again, however much is at stake", () => {
  const state = rest.recordShown({ hints: {} }, "big_paste", 10, NOW - 10_000_000, TH, true);
  assert.equal(pickHint([found("big_paste", 1e9)], [], state, NOW, TH), null);
});

test("the hint already on screen keeps its place without a new leave", () => {
  const state = rest.recordShown({ hints: {} }, "cold_return", 150_000, NOW - 5, TH, false);
  assert.equal(pickHint([found("cold_return", 150_000)], [], state, NOW, TH), null);
  assert.equal(pickHint([found("cold_return", 150_000)], [], state, NOW, TH, "cold_return")?.kind, "cold_return");
  assert.equal(pickHint([found("cold_return", 150_000)], ["cold_return"], state, NOW, TH, "cold_return"), null);
});

test("the cold-return receipt rests cold_rest_hours however the context grows", () => {
  const state = rest.recordShown({ hints: {} }, "cold_return", 150_000, NOW - 3600, TH, false);
  assert.equal(pickHint([found("cold_return", 900_000)], [], state, NOW, TH), null);
  assert.equal(pickHint([found("cold_return", 150_000)], [], state, NOW - 3600 + TH.cold_rest_hours * 3600, TH)?.kind, "cold_return");
});

test("mergeRest lays the newer row of a kind over the snapshot's", () => {
  const base = { hints: { a: { ts: 100, stake: 1, n: 1, rest: 10 }, b: { ts: 500, stake: 1, n: 1, rest: 10 } } };
  const overlay = { hints: { a: { ts: 200, stake: 2, n: 2, rest: 20 }, b: { ts: 400, stake: 9, n: 9, rest: 90 }, c: { ts: 1, stake: 1, n: 1, rest: 1 } } };
  const merged = mergeRest(base, overlay);
  assert.equal(merged.hints.a.n, 2);
  assert.equal(merged.hints.b.n, 1);
  assert.equal(merged.hints.c.n, 1);
  assert.deepEqual(mergeRest(null, undefined), { hints: {} });
  assert.equal(base.hints.a.n, 1);
});

// -- plan_fresh -----------------------------------------------------------------------

test("startingContext is the first request's context less your first message", () => {
  assert.equal(startingContext(30_000, 4000, 4), 29_000);
  assert.equal(startingContext(500, 4000, 4), 0);
});

test("plan_fresh needs what a fresh start would drop to reach plan_fresh_tokens", () => {
  const start = 30_000;
  const at = (ctx, planChars = 0, compacted = false) => freshHint(ctx, start, planChars, compacted, 4, TH);
  assert.deepEqual(at(80_000), { kind: "plan_fresh", stake: 50_000, fields: { kept: 50_000 } });
  assert.equal(at(start + TH.plan_fresh_tokens - 1), null);
  assert.equal(at(start + TH.plan_fresh_tokens)?.kind, "plan_fresh");
  assert.equal(at(80_000, 40_000)?.stake, 40_000);
  assert.equal(at(80_000, 80_000), null);
  assert.equal(at(80_000, 0, true), null);
  assert.equal(freshHint(null, start, 0, false, 4, TH), null);
  assert.equal(freshHint(80_000, null, 0, false, 4, TH), null);
});

// -- background work ----------------------------------------------------------------------

test("work launched in the background is pending until a notification names it", () => {
  let state = backgroundLaunched(emptyBackground(), "t1", NOW);
  state = backgroundLaunched(state, "t2", NOW + 1);
  state = backgroundLaunched(state, "t1", NOW + 2);
  assert.deepEqual(state.items.map(one => one.id), ["t1", "t2"]);
  const named = backgroundNotified(state, `${p.notificationPrefix}><tool-use-id>t1</tool-use-id></task-notification>`, p);
  assert.deepEqual(named.items.map(one => one.id), ["t2"]);
  const unnamed = backgroundNotified(state, `${p.notificationPrefix}>done</task-notification>`, p);
  assert.deepEqual(unnamed.items, []);
  assert.equal(state.items.length, 2);
});

test("work that never reports back is forgotten after BACKGROUND_FORGET_S, and only so many are kept", () => {
  let state = backgroundLaunched(emptyBackground(), "old", NOW);
  state = backgroundLaunched(state, "new", NOW + BACKGROUND_FORGET_S);
  assert.deepEqual(state.items.map(one => one.id), ["new"]);
  let many = emptyBackground();
  for (let i = 0; i < 100; i++) many = backgroundLaunched(many, `t${i}`, NOW + i);
  assert.ok(many.items.length <= 40);
  assert.equal(many.items[many.items.length - 1].id, "t99");
});

test("a tool result says it went to the background in its first few hundred characters", () => {
  assert.equal(launchedInBackground("Command running in background with ID: bx1. Output goes to a file", p), true);
  assert.equal(launchedInBackground("Async agent launched successfully. agentId: a1", p), true);
  assert.equal(launchedInBackground("The command finished", p), false);
  assert.equal(launchedInBackground("x".repeat(p.backgroundScan + 10) + "Command running in background with ID: ", p), false);
  assert.equal(launchedInBackground(undefined, p), false);
});

test("a message that opens with the task-notification prefix is a notification", () => {
  assert.equal(isNotification(`  ${p.notificationPrefix}>x</task-notification>`, p), true);
  assert.equal(isNotification("rename foo", p), false);
});

// -- the Python they port ---------------------------------------------------------------------

/** Background work through the steps of a transcript: results that launch some, notifications that end it. */
function backgroundPending(steps) {
  let state = emptyBackground();
  let now = NOW;
  for (const step of steps) {
    now += 1;
    if (step.step === "notify") {
      if (isNotification(step.text, p)) state = backgroundNotified(state, step.text, p);
    } else if (launchedInBackground(step.text, p)) {
      state = backgroundLaunched(state, step.id, now);
    }
  }
  return state.items.length > 0;
}

/** A hint as the vectors keep it: its kind and stake. */
function brief(one) {
  return one ? { kind: one.kind, stake: one.stake } : null;
}

const RUN = {
  isStatus: a => isStatus(a[0], p),
  isGo: a => isGo(a[0], p),
  isQuestion: a => isQuestion(a[0], p),
  isChangeRequest: a => isChangeRequest(a[0], p),
  endsOnQuestion: a => endsOnQuestion(a[0], p),
  changesFile: a => changesFile(a[0], a[1], p),
  dripCount: ([a]) => {
    const small = text => text.trim().length <= a.th.drip_chars;
    const earlier = a.earlier.map(one => ({
      answer: one.answer,
      answered: one.answered,
      change: isChangeRequest(one.text, p),
      small: small(one.text),
      edited: one.edited,
      since: one.since,
    }));
    return dripCount(earlier, { small: small(a.prompt), change: isChangeRequest(a.prompt, p) }, a.since, a.answer, a.th);
  },
  pasteHint: ([a]) => brief(pasteHint(a.chars, 4, a.th)),
  coldHint: ([a]) => brief(coldHint(a.last, a.nowS, a.compactedSince, a.limit, a.th)),
  backgroundSteps: ([steps]) => backgroundPending(steps),
  freshHint: ([a]) => brief(freshHint(a.ctx, startingContext(a.firstCtx, a.promptChars, 4), a.planChars, a.compacted, 4, a.th)),
  projectApplies: ([a]) => projectApplies(a.cwd, a.projects, a.excluded, a.name),
};

// -- who coaching is for ----------------------------------------------------------

test("a project is left out by an exclude pattern or a !pattern, and left in by none or a match", () => {
  assert.equal(projectApplies("/work/api", [], []), true);
  assert.equal(projectApplies("/work/api", ["work"], []), true);
  assert.equal(projectApplies("/work/api", ["other"], []), false);
  assert.equal(projectApplies("/work/api", ["work", "!api"], []), false);
  assert.equal(projectApplies("/work/api", [], ["API"]), false);
  assert.equal(projectApplies("/work/api", ["(unclosed"], []), false, "a pattern that does not compile matches nothing");
  assert.equal(projectApplies("/work/api", [], ["(unclosed"]), true);
  assert.equal(projectApplies(null, ["other"], []), true, "no folder, no filter");
  assert.equal(projectApplies("/anywhere", ["named"], [], "named-project"), true);
});

test("the slug is the folder with every other character as a dash, cut at 200", () => {
  assert.equal(projectSlug("C:\\Dev\\my app"), "C--Dev-my-app");
  assert.equal(projectSlug("/x", "given"), "given");
  const long = projectSlug("/" + "a".repeat(300));
  assert.equal(long.length, 200 + 1 + 8);
});

test("the later reminder wins, and a rating counts when it is not before the piece began", () => {
  assert.equal(latestReminder(100, 200), 200);
  assert.equal(latestReminder(300, 200), 300);
  assert.equal(latestReminder(null, 200), 200);
  assert.equal(latestReminder(null, null), null);
  assert.equal(ratedSince(null, 10), false);
  assert.equal(ratedSince(50, 10), true);
  assert.equal(ratedSince(5, 10), false);
  assert.equal(ratedSince(5, null), true);
});

test("the decisions match the Python they port, case by case", { skip: process.env.COACH_VECTORS ? false : "COACH_VECTORS isn't set (test_mod_coach.py sets it)" }, () => {
  const vectors = JSON.parse(readFileSync(process.env.COACH_VECTORS, "utf8"));
  assert.ok(vectors.length > 500, "the vectors are there");
  const seen = new Set();
  const wrong = [];
  for (const one of vectors) {
    seen.add(one.fn);
    const run = RUN[one.fn];
    if (!run) {
      wrong.push(`${one.fn}: no such function in the test`);
      continue;
    }
    const got = run(one.args);
    if (JSON.stringify(got) !== JSON.stringify(one.expect)) {
      wrong.push(`${one.fn} ${JSON.stringify(one.args).slice(0, 300)}: expected ${JSON.stringify(one.expect)}, got ${JSON.stringify(got)}`);
    }
  }
  assert.deepEqual([...seen].sort(), Object.keys(RUN).sort(), "every function has cases");
  assert.deepEqual(wrong.slice(0, 20), []);
});

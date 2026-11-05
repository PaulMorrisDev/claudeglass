/* claudeglass mod: lib/coverage.js
 *
 * Checks each keep-list item against the conversation summary, and turns the
 * list into the two drafts the user may send: the "/compact Keep: ..." text
 * for before a compaction, and the "Before compaction we agreed: ..." note
 * for adding back what the summary dropped. Also the rule that decides
 * whether an automatic compaction is held once for review.
 *
 * The quick check is local and labelled "(est.)" wherever it shows. Marks:
 *
 *   found    the summary has it
 *   partly   it has some of it
 *   missing  it does not (the review lists these)
 *   safe     it survives compaction whatever the summary says (not counted)
 *
 * Thresholds (THRESHOLDS below):
 *   - Paths, ports, numbers, versions, ids and URLs ("value" and "file"
 *     items) need an exact match: found or missing, nothing between. A path
 *     matches by its full text or by its file name.
 *   - Instructions, decisions, pins, tasks and errors score the share of
 *     their key words the summary has (words are lower-cased, stemmed, and
 *     stop words are dropped). When the item also carries exact strings the
 *     score is half the exact share and half the word share. A score of 0.6
 *     or more is found, 0.3 or more is partly, below that is missing.
 *   - A constraint with a "never" or "don't" in it loses its meaning when
 *     the summary keeps the subject but not the prohibition. So if none of
 *     the summary's mentions of the item's key words has a negation within
 *     100 characters, a found or partly mark becomes missing.
 *
 * Nothing here reads a clock, the disk or the engine.
 */

import { contentWords, squash, trimText } from "./keeplist.js";

/**
 * An item of the keep list (keeplist.js builds them).
 * @typedef {Object} KeepItem
 * @property {string} id
 * @property {"pin"|"instruction"|"decision"|"error"|"task"|"file"|"value"} kind
 * @property {string} text
 * @property {boolean} safe
 * @property {"recent"|"plan"|"claude-md"|""} why
 * @property {string[]} exact
 * @property {number} at
 */

/**
 * @typedef {"found"|"partly"|"missing"|"safe"} Mark
 */

/**
 * @typedef {Object} Coverage
 * @property {Record<string, Mark>} marks   By item id.
 * @property {{ found: number, partly: number, missing: number, safe: number }} counts  Every kind of item, files and values included.
 * @property {number} kept      Things the user said (see SAID_KINDS) the summary has at least partly, safe ones not counted.
 * @property {number} missing   Things the user said that are marked missing.
 * @property {number} total     Things the user said that were checked, safe ones not counted.
 */

/** Score thresholds of the quick check. */
export const THRESHOLDS = { found: 0.6, partly: 0.3 };
/** How far from a mention a negation may sit, in characters. */
export const NEGATION_WINDOW = 100;
/**
 * The kinds of item that are "things you said": what the user told Claude to do or decided, or pinned.
 * Files, values, tasks and errors are checked too, but only the full list shows them.
 */
export const SAID_KINDS = ["instruction", "decision", "pin"];

/**
 * Whether an item is one of the things the user said.
 * @param {KeepItem} item
 * @returns {boolean}
 */
export function isSaid(item) {
  return SAID_KINDS.indexOf(item.kind) >= 0;
}

/**
 * How many of the items are things the user said and are not safe: the number a headline counts against.
 * @param {KeepItem[]} items
 * @returns {number}
 */
export function saidTotal(items) {
  return (Array.isArray(items) ? items : []).filter(function (item) {
    return isSaid(item) && !item.safe;
  }).length;
}

/** The headroom the model's window must have over the compaction point before a compaction is held. */
export const HOLD_HEADROOM = 100000;
/** A compaction is never held when the context is within this many tokens of the window: Claude Code needs the room for the summary and the next request. */
export const HOLD_MARGIN = 20000;
/** The longest "Keep:" command, in characters. */
export const KEEP_MAX = 1500;
/** The most summary text sent to a model judge. */
export const JUDGE_SUMMARY_MAX = 12000;

/** The mark glyphs the review draws. */
export const MARK_SYMBOL = { found: "✓", partly: "◐", missing: "✗", safe: "ⓘ" };

const NEGATION_IN_ITEM = /\b(never|don'?t|do not|dont|not|no|avoid|without|shouldn'?t|should not|mustn'?t|must not|can'?t|cannot|stop)\b/i;
const NEGATION_IN_SUMMARY =
  /\b(never|don'?t|do not|dont|not|no|avoid|avoids|avoided|without|shouldn'?t|should not|mustn'?t|must not|can'?t|cannot|stop|skip|forbid|forbidden|prohibit|prohibited|off-limits|instead|rather than|over|prefer\w*|favou?r\w*|except)\b|n't\b/i;
const LEADING = /^(?:no|nope|actually|wait|not that|instead)\b[\s,.:;!-]+/i;

/**
 * @typedef {Object} Summary
 * @property {string} lower     The summary, lower-cased, with / for \.
 * @property {Set<string>} words  Its key words, stemmed.
 */

/**
 * Prepares a summary for repeated checks.
 * @param {unknown} text
 * @returns {Summary}
 */
export function prepareSummary(text) {
  var raw = typeof text === "string" ? text : "";
  return { lower: raw.replace(/\\/g, "/").toLowerCase(), words: new Set(contentWords(raw)) };
}

/**
 * True when the summary has `token` exactly (numbers and ids on word edges).
 * @param {Summary} summary
 * @param {string} token
 * @returns {boolean}
 */
function hasToken(summary, token) {
  var needle = token.replace(/\\/g, "/").toLowerCase();
  if (needle === "") return false;
  var from = 0;
  for (;;) {
    var at = summary.lower.indexOf(needle, from);
    if (at < 0) return false;
    var before = at === 0 ? "" : summary.lower.charAt(at - 1);
    var after = summary.lower.charAt(at + needle.length);
    var startsWord = /[a-z0-9]/.test(needle.charAt(0));
    var endsWord = /[a-z0-9]/.test(needle.charAt(needle.length - 1));
    var leftOk = !startsWord || !/[a-z0-9]/.test(before);
    var rightOk = !endsWord || !/[a-z0-9]/.test(after);
    if (leftOk && rightOk) return true;
    from = at + 1;
  }
}

/**
 * True when some mention of the item's key words has a negation near it.
 * @param {Summary} summary
 * @param {string[]} anchors  Lower-case strings to look for.
 * @returns {boolean}
 */
function negatedNear(summary, anchors) {
  for (var i = 0; i < anchors.length; i++) {
    var anchor = String(anchors[i]);
    if (anchor === "") continue;
    var from = 0;
    for (;;) {
      var at = summary.lower.indexOf(anchor, from);
      if (at < 0) break;
      var window = summary.lower.slice(Math.max(0, at - NEGATION_WINDOW), at + anchor.length + NEGATION_WINDOW);
      if (NEGATION_IN_SUMMARY.test(window)) return true;
      from = at + 1;
    }
  }
  return false;
}

/**
 * The words of an item to look for in a summary, as they would appear
 * (stems are compared as prefixes of the summary's words).
 * @param {string[]} words
 * @param {Summary} summary
 * @returns {string[]}
 */
function present(words, summary) {
  return words.filter(function (word) {
    return summary.words.has(word);
  });
}

/**
 * @param {number} score
 * @returns {Mark}
 */
function band(score) {
  if (score >= THRESHOLDS.found) return "found";
  if (score >= THRESHOLDS.partly) return "partly";
  return "missing";
}

/**
 * The mark for one item.
 * @param {KeepItem} item
 * @param {Summary} summary  From prepareSummary.
 * @returns {Mark}
 */
export function markItem(item, summary) {
  if (item.safe) return "safe";
  var exact = Array.isArray(item.exact) ? item.exact : [];
  if (item.kind === "file") {
    for (var f = 0; f < exact.length; f++) if (hasToken(summary, String(exact[f]))) return "found";
    return "missing";
  }
  if (item.kind === "value") {
    for (var v = 0; v < exact.length; v++) if (!hasToken(summary, String(exact[v]))) return "missing";
    return exact.length > 0 ? "found" : band(wordScore(item, summary));
  }
  var words = contentWords(item.text);
  var wordShare = words.length === 0 ? null : present(words, summary).length / words.length;
  var exactShare = null;
  if (exact.length > 0) {
    var hits = 0;
    for (var e = 0; e < exact.length; e++) if (hasToken(summary, String(exact[e]))) hits++;
    exactShare = hits / exact.length;
  }
  var score;
  if (wordShare !== null && exactShare !== null) score = 0.5 * wordShare + 0.5 * exactShare;
  else if (exactShare !== null) score = exactShare;
  else score = wordShare === null ? 0 : wordShare;
  var mark = band(score);
  if (mark === "missing") return mark;
  // The summary names the subject; does it keep the prohibition?
  var text = squash(item.text).replace(LEADING, "");
  if (NEGATION_IN_ITEM.test(text)) {
    var anchors = exact.map(function (token) {
      return String(token).replace(/\\/g, "/").toLowerCase();
    });
    if (anchors.length === 0) {
      anchors = words
        .slice()
        .sort(function (a, b) {
          return b.length - a.length;
        })
        .slice(0, 3)
        .filter(function (word) {
          return summary.words.has(word);
        });
    }
    if (!negatedNear(summary, anchors)) return "missing";
  }
  return mark;
}

/**
 * @param {KeepItem} item
 * @param {Summary} summary
 * @returns {number}
 */
function wordScore(item, summary) {
  var words = contentWords(item.text);
  return words.length === 0 ? 0 : present(words, summary).length / words.length;
}

/**
 * Marks every item against the summary.
 * @param {KeepItem[]} items
 * @param {unknown} summaryText
 * @returns {Coverage}
 */
export function checkCoverage(items, summaryText) {
  var summary = prepareSummary(summaryText);
  /** @type {Record<string, Mark>} */
  var marks = {};
  var list = Array.isArray(items) ? items : [];
  for (var i = 0; i < list.length; i++) {
    var item = /** @type {KeepItem} */ (list[i]);
    marks[item.id] = markItem(item, summary);
  }
  return tally(list, marks);
}

/**
 * The items a mark map calls missing, in list order.
 * @param {KeepItem[]} items
 * @param {Record<string, Mark>} marks
 * @returns {KeepItem[]}
 */
export function missingItems(items, marks) {
  return (Array.isArray(items) ? items : []).filter(function (item) {
    return marks[item.id] === "missing";
  });
}

/**
 * The missing items that are things the user said, in list order: what the review's first screen offers to add back.
 * @param {KeepItem[]} items
 * @param {Record<string, Mark>} marks
 * @returns {KeepItem[]}
 */
export function missingSaid(items, marks) {
  return missingItems(items, marks).filter(isSaid);
}

/**
 * One item as a clause in a sentence: its text, with a closing full stop removed.
 * @param {KeepItem} item
 * @returns {string}
 */
function clause(item) {
  return squash(item.text).replace(/[.!;:,\s]+$/, "");
}

/**
 * The clauses of a list of items, with files and values each gathered in one.
 * `labels` gives the lead words of the two gathered clauses.
 * @param {KeepItem[]} items
 * @param {{ files?: string, values?: string }} [labels]
 * @returns {string[]}
 */
function clauses(items, labels) {
  var lead = labels || {};
  /** @type {string[]} */
  var out = [];
  /** @type {string[]} */
  var files = [];
  /** @type {string[]} */
  var values = [];
  var list = Array.isArray(items) ? items : [];
  for (var i = 0; i < list.length; i++) {
    var item = /** @type {KeepItem} */ (list[i]);
    if (item.kind === "file") files.push(clause(item));
    else if (item.kind === "value") values.push(clause(item));
    else if (clause(item) !== "") out.push(clause(item));
  }
  if (files.length > 0) out.push((lead.files ? lead.files + " " : "") + files.join(", "));
  if (values.length > 0) out.push((lead.values ? lead.values + " " : "") + values.join("; "));
  return out;
}

/**
 * Fills `{items}` in a template.
 * @param {string} template
 * @param {string} items
 * @returns {string}
 */
function put(template, items) {
  return String(template).replace("{items}", function () {
    return items;
  });
}

/**
 * The "/compact Keep: ..." text for the items, at most `maxChars` characters.
 * Items go in order and none is cut in the middle; if even the first does not
 * fit it is shortened with an ellipsis. Empty when there are no items.
 * @param {KeepItem[]} items
 * @param {string} template  A COPY template with `{items}`, e.g. "/compact Keep: {items}".
 * @param {number} [maxChars]
 * @param {{ files?: string, values?: string }} [labels]
 * @returns {string}
 */
export function compactKeep(items, template, maxChars, labels) {
  var limit = typeof maxChars === "number" && maxChars > 0 ? maxChars : KEEP_MAX;
  var parts = clauses(items, labels);
  if (parts.length === 0) return "";
  var room = limit - put(template, "").length;
  var joined = "";
  for (var i = 0; i < parts.length; i++) {
    var next = joined === "" ? String(parts[i]) : joined + "; " + parts[i];
    if (next.length > room) {
      if (joined === "") joined = trimText(String(parts[i]), Math.max(2, room));
      break;
    }
    joined = next;
  }
  return put(template, joined);
}

/**
 * The note that puts missing items back: "Before compaction we agreed: a; b.".
 * @param {KeepItem[]} items
 * @param {string} template  A COPY template with `{items}`.
 * @param {{ files?: string, values?: string }} [labels]
 * @returns {string}
 */
export function addBackNote(items, template, labels) {
  var parts = clauses(items, labels);
  if (parts.length === 0) return "";
  return put(template, parts.join("; "));
}

/**
 * Where a hold stands. "none": nothing is held. "holding": an automatic compaction was held in the reply now
 * running, and the rest of the reply holds the ones that come. "release": that reply has ended, so the
 * first automatic compaction after it goes through.
 * @typedef {"none"|"holding"|"release"} HoldState
 */

/**
 * What to do with an automatic compaction attempt. An automatic compaction is asked for again at every step
 * of a reply that is past the compaction point, so holding one attempt buys one request, not time to review.
 * A hold lasts the rest of the reply it began in; the first automatic compaction after that reply goes
 * through. Never held: when the opt-in is off, the trigger is manual, the window is not at least
 * HOLD_HEADROOM above the compaction point (so never on a 200k window), or the context is within
 * HOLD_MARGIN of the window (Claude Code could not take another request).
 * @param {{ enabled: boolean, trigger: string, window: number|null, point: number|null, tokens: number|null, state: HoldState }} args
 * @returns {{ hold: boolean, next: HoldState, first: boolean }}  `first` is true for the attempt that begins a hold (the one to announce).
 */
export function holdStep(args) {
  var win = args.window;
  var point = args.point;
  var tokens = args.tokens;
  var eligible =
    args.enabled === true &&
    args.trigger === "auto" &&
    typeof win === "number" &&
    typeof point === "number" &&
    isFinite(win) &&
    isFinite(point) &&
    win - point >= HOLD_HEADROOM &&
    !(typeof tokens === "number" && isFinite(tokens) && tokens >= win - HOLD_MARGIN);
  if (!eligible) return { hold: false, next: "none", first: false };
  if (args.state === "release") return { hold: false, next: "none", first: false };
  return { hold: true, next: "holding", first: args.state !== "holding" };
}

/**
 * A reply ended: a hold that was in force is released for the next automatic compaction.
 * @param {HoldState} state
 * @returns {HoldState}
 */
export function holdAfterReply(state) {
  return state === "holding" ? "release" : state;
}

/**
 * The request a model judge answers: which items does the summary cover.
 * @param {KeepItem[]} items
 * @param {unknown} summaryText
 * @returns {string}
 */
export function judgePrompt(items, summaryText) {
  var summary = String(summaryText || "").slice(0, JUDGE_SUMMARY_MAX);
  var lines = (Array.isArray(items) ? items : []).map(function (item, index) {
    return String(index + 1) + ". " + squash(item.text);
  });
  return (
    "A conversation was summarised. Check whether the summary still carries each item below. " +
    "Answer with one line per item, in the form `<number>: yes`, `<number>: partly` or `<number>: no`, and nothing else. " +
    "Say yes only when the summary keeps the item's meaning, including any prohibition in it.\n\n" +
    "SUMMARY\n<<<\n" +
    summary +
    "\n>>>\n\nITEMS\n" +
    lines.join("\n")
  );
}

/**
 * Reads a judge's answer into marks by item id. Items it did not answer are
 * left out, so the quick check's mark stands for them.
 * @param {unknown} answer
 * @param {KeepItem[]} items
 * @returns {Record<string, Mark>}
 */
export function parseJudge(answer, items) {
  /** @type {Record<string, Mark>} */
  var marks = {};
  var list = Array.isArray(items) ? items : [];
  var lines = String(answer || "").split(/\n+/);
  for (var i = 0; i < lines.length; i++) {
    var hit = /^\W*(\d+)\W*[:.)-]\s*(yes|partly|no)\b/i.exec(String(lines[i]));
    if (!hit) continue;
    var item = list[Number(hit[1]) - 1];
    if (!item || item.safe) continue;
    var word = String(hit[2]).toLowerCase();
    marks[item.id] = word === "yes" ? "found" : word === "partly" ? "partly" : "missing";
  }
  return marks;
}

/**
 * Counts for a coverage after some marks were replaced (a judge's answer).
 * @param {KeepItem[]} items
 * @param {Record<string, Mark>} marks
 * @returns {Coverage}
 */
export function tally(items, marks) {
  var counts = { found: 0, partly: 0, missing: 0, safe: 0 };
  var said = { found: 0, partly: 0, missing: 0, safe: 0 };
  var list = Array.isArray(items) ? items : [];
  for (var i = 0; i < list.length; i++) {
    var item = /** @type {KeepItem} */ (list[i]);
    var mark = marks[item.id];
    if (!mark) continue;
    counts[mark] += 1;
    if (isSaid(item)) said[mark] += 1;
  }
  return {
    marks: marks,
    counts: counts,
    kept: said.found + said.partly,
    missing: said.missing,
    total: said.found + said.partly + said.missing,
  };
}

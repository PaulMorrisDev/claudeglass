/* claudeglass mod: lib/tighten.js
 *
 * The words of /glass tighten and the diff it shows: the instruction Haiku is
 * given, the rewrite cleaned of anything but the prompt, and a unified diff of
 * the draft against the rewrite, a sentence to a line so the diff says what
 * changed. Nothing here reads the engine or the disk; the draft and the
 * rewrite are the person's text and stay in the caller's memory.
 */

/** Longest draft that is sent to Haiku, in characters. */
export var MAX_DRAFT_CHARS = 20000;

/** The instruction Haiku is given. */
export var TIGHTEN_SYSTEM =
  "You tighten a prompt that someone is about to send to a coding assistant. " +
  "Rewrite it shorter and more specific. Keep every requirement, path, name, value and constraint. " +
  "Add nothing that is not in it, and do not answer it or ask questions. " +
  "Output only the rewritten prompt, with no preface, no quotes and no explanation.";

/**
 * The message Haiku is given: the draft between marks, so it is rewritten and not answered.
 * @param {string} draft
 * @returns {string}
 */
export function tightenPrompt(draft) {
  return "Rewrite this prompt.\n\n<prompt>\n" + draft + "\n</prompt>";
}

/**
 * The reply's cap in tokens: a rewrite is shorter than its draft, so a
 * third of the draft's characters plus some room is plenty.
 * @param {string} draft
 * @returns {number}
 */
export function tightenMaxTokens(draft) {
  return Math.min(4000, Math.ceil(String(draft).length / 3) + 200);
}

/**
 * Whether a reply was cut off by the cap: it used all the tokens it was allowed. A cut rewrite is not a rewrite.
 * @param {number} outputTokens
 * @param {number} maxTokens
 * @returns {boolean}
 */
export function wasCut(outputTokens, maxTokens) {
  return outputTokens >= maxTokens;
}

/**
 * The reply with anything around the prompt taken off: the <prompt> marks,
 * a code fence, a pair of quotes.
 * @param {string} reply
 * @returns {string}
 */
export function cleanRewrite(reply) {
  var text = String(reply || "").trim();
  var marked = /<prompt>\s*([\s\S]*?)\s*<\/prompt>/i.exec(text);
  if (marked !== null) text = (marked[1] || "").trim();
  var fenced = /^```[A-Za-z]*\n([\s\S]*?)\n```$/.exec(text);
  if (fenced !== null) text = (fenced[1] || "").trim();
  if (text.length >= 2 && ((text[0] === '"' && text[text.length - 1] === '"') || (text[0] === "“" && text[text.length - 1] === "”"))) {
    text = text.slice(1, -1).trim();
  }
  return text;
}

/**
 * Words in a text.
 * @param {string} text
 * @returns {number}
 */
export function words(text) {
  var found = String(text || "")
    .trim()
    .match(/\S+/g);
  return found === null ? 0 : found.length;
}

/** The most lines either side of a diff has; past it the rest is left out. */
var MAX_LINES = 400;

/**
 * A text as lines for a diff: one per line of the text, and a line is cut
 * after a sentence's end so a long paragraph shows what changed in it.
 * @param {string} text
 * @returns {string[]}
 */
export function lines(text) {
  var source = String(text || "").replace(/\r\n?/g, "\n");
  /** @type {string[]} */
  var out = [];
  var current = "";
  for (var i = 0; i < source.length; i++) {
    var ch = /** @type {string} */ (source[i]);
    if (ch === "\n") {
      out.push(current);
      current = "";
      continue;
    }
    current += ch;
    if ((ch === "." || ch === "!" || ch === "?") && source[i + 1] === " ") {
      out.push(current);
      current = "";
      i += 1;
    }
  }
  if (current !== "") out.push(current);
  return out.slice(0, MAX_LINES);
}

/**
 * A unified diff (one hunk) of `before` against `after`, by line.
 * @param {string} before
 * @param {string} after
 * @returns {string}
 */
export function unifiedDiff(before, after) {
  var a = lines(before);
  var b = lines(after);
  var n = a.length;
  var m = b.length;
  /** @type {number[][]} */
  var table = [];
  for (var i = 0; i <= n; i++) {
    /** @type {number[]} */
    var row = [];
    for (var j = 0; j <= m; j++) row.push(0);
    table.push(row);
  }
  for (var x = n - 1; x >= 0; x--) {
    for (var y = m - 1; y >= 0; y--) {
      var down = /** @type {number[]} */ (table[x + 1])[y] || 0;
      var right = /** @type {number[]} */ (table[x])[y + 1] || 0;
      var diagonal = (/** @type {number[]} */ (table[x + 1])[y + 1] || 0) + 1;
      /** @type {number[]} */ (table[x])[y] = a[x] === b[y] ? diagonal : Math.max(down, right);
    }
  }
  /** @type {string[]} */
  var out = [];
  var p = 0;
  var q = 0;
  while (p < n || q < m) {
    if (p < n && q < m && a[p] === b[q]) {
      out.push(" " + a[p]);
      p += 1;
      q += 1;
    } else if (q < m && (p >= n || (/** @type {number[]} */ (table[p])[q + 1] || 0) > (/** @type {number[]} */ (table[p + 1])[q] || 0))) {
      out.push("+" + b[q]);
      q += 1;
    } else {
      out.push("-" + a[p]);
      p += 1;
    }
  }
  return "@@ -" + (n === 0 ? "0,0" : "1," + n) + " +" + (m === 0 ? "0,0" : "1," + m) + " @@\n" + out.join("\n");
}

/* claudeglass mod: lib/keeplist.js
 *
 * The keep list: what the user said and what the session did that a
 * conversation summary (compaction) must not lose. It is built from the
 * session's own messages and tool calls, locally and with no model: plain
 * pattern rules, so the same messages always give the same list (plan 5b).
 *
 * Kinds, in the order the review shows them:
 *   pin          the user pinned it (/glass pin, [p] pin, a press on an item)
 *   instruction  a constraint or a correction the user typed: "never use port 8765"
 *   decision     "let's go with ...", "agreed", or a plan the user approved
 *   error        the last shell command (Bash, PowerShell) that failed and was not run again with success
 *   task         an open item of the last TodoWrite list
 *   file         a file Edit / Write / NotebookEdit changed (not one in a temp or scratch place, or a plan file)
 *   value        a port, version, id, URL or number the user typed
 *
 * What survives a compaction whatever the summary says ("safe anyway"):
 *   - files among the last 5 modified (Claude Code re-reads them),
 *   - the approved plan,
 *   - anything the mod can read in CLAUDE.md (project and user level only;
 *     imported files, nested and path-scoped rules and auto memory are not read).
 *
 * Nothing here reads a clock, the disk or the engine. Message text is only
 * ever held in memory by the caller.
 */

import { commandLabel } from "./command.js";

/**
 * @typedef {Object} ToolUseLike
 * @property {string} [tool]
 * @property {Record<string, unknown>} [input]
 * @property {boolean} [isError]
 * @property {string} [text]
 */

/**
 * @typedef {Object} ToolResultLike
 * @property {string} [tool_use_id]
 * @property {string} [text]
 * @property {boolean} [isError]
 */

/**
 * One message, in the shape of `$.session.messages()`.
 * @typedef {Object} MessageLike
 * @property {string} role
 * @property {string} [text]
 * @property {ToolUseLike[]} [toolUses]
 * @property {ToolResultLike[]} [toolResults]
 */

/**
 * @typedef {"pin"|"instruction"|"decision"|"error"|"task"|"file"|"value"} KeepKind
 */

/**
 * @typedef {Object} KeepItem
 * @property {string} id       Same text and kind, same id.
 * @property {KeepKind} kind
 * @property {string} text     One short line.
 * @property {boolean} safe    Survives compaction whatever the summary says.
 * @property {"recent"|"plan"|"claude-md"|""} why  Why it is safe; "" when it is not.
 * @property {string[]} exact  Strings the summary must contain verbatim (paths, numbers, ports, ids, URLs).
 * @property {number} at       Where in the conversation (message index; later is newer).
 */

/**
 * @typedef {Object} PinLike
 * @property {string} text
 */

/**
 * @typedef {Object} BuildOptions
 * @property {PinLike[]} [pins]
 * @property {string|null} [claudeMd]   Text of the CLAUDE.md files the mod can read, joined.
 * @property {number} [recentFiles]     Files this recent are safe (default 5).
 * @property {string|null} [cwd]        The session's working directory: a file under it is shown relative to it.
 */

/** The kinds in the order a review lists them. */
export const KINDS = /** @type {KeepKind[]} */ (["pin", "instruction", "decision", "error", "task", "file", "value"]);

/** The most items kept per kind. */
export const LIMITS = { pin: 20, instruction: 12, decision: 6, error: 1, task: 8, file: 12, value: 8 };

/** The longest an item's text runs, in characters. */
export const MAX_TEXT = 120;

/** A sentence longer than this is a paste, not something the user said to Claude. */
const MAX_SENTENCE = 260;
/** Only this much of a message is read. */
const MAX_MESSAGE = 4000;
/** At most this many instruction items come from one message. */
const PER_MESSAGE = 3;
/** The files Claude Code re-reads after a compaction. */
export const RECENT_FILES = 5;
/** Suggestions in the "before" view. */
export const SUGGEST_LIMIT = 7;

const EDIT_TOOLS = ["Edit", "Write", "MultiEdit", "NotebookEdit"];

const INSTRUCTION =
  /\b(don'?t|do not|dont|never|always|must(?:n'?t| not)?|only|should(?:n'?t| not)|avoid|make sure|be sure|ensure|without|no more|stop (?:using|doing)|not allowed)\b|\buse\b.+\b(?:not|instead of|rather than)\b/i;
/**
 * "I don't really see anything", "I don't know why it fails": the user talking about what they can see or know, not
 * telling Claude what to do. Only this clause is dropped; what is left of the sentence is tested again, so
 * "I don't know why it fails, so never touch the config." is still an instruction. (A question is skipped for
 * ending in "?".)
 */
const SELF_TALK =
  /\bI\s+(?:really\s+|actually\s+|even\s+|just\s+)?(?:don['’]?t|dont|do not|can['’]?t|cannot|never)\s+(?:really\s+|actually\s+|even\s+|quite\s+)?(?:see|know|understand|think|get|follow|remember|recall|notice|believe|feel|mind|care|like)\b/i;
/** "I don't think you should touch the tests" says "you shouldn't", so it stays an instruction. */
const THINK_SHOULD = /\bI\s+(?:really\s+|actually\s+)?(?:don['’]?t|dont|do not)\s+think\s+(?:you|we)\s+should\b/i;
const CORRECTION = /^(?:no|nope|actually|wait|not that|that'?s not|that is not|instead)\b[\s,.:;!-]+\S/i;
const DECISION =
  /\b(let'?s go with|lets go with|let'?s do|go with|going with|we'?ll go with|we will go with|agreed|i agree|approved?|proceed with|we decided|decided to|the decision is|sounds good,? (?:use|go|do))\b/i;
const NOISE_START = /^(?:<|this session is being continued|\[request interrupted|caveat:|\[image|base directory for this skill|launching skill)/i;
/** A skill's or command's body arrives as a user message whose header names the command; anywhere in the text counts. */
const COMMAND_HEADER = /<command-(?:name|message|args)>|<skill-(?:name|format)>/i;
/** A user message longer than this (characters, whitespace squashed) is pasted text, not an instruction. */
export const MAX_USER_MESSAGE = 2000;
/** The tools whose failure is the user's own command failing; any other tool's error is Claude's or the host's. */
const SHELL_TOOLS = ["Bash", "PowerShell"];
/** A path segment that marks a temp or scratch place. */
const SCRATCH_SEGMENT = /(?:^|\/)(?:temp|tmp|scratchpad)(?:\/|$)/i;
/** Claude Code's plan files; the approved plan is its own item. */
const PLAN_FILE = /(?:^|\/)\.claude\/plans\//i;
const FAILURE_LINE =
  /\b(FAILED|FAIL|ERROR|Error:|Traceback|AssertionError|Exception|npm ERR!|error TS\d+|fatal:|panic:)|\bfailed\b|not found|CategoryInfo|is not recognized/;
/**
 * A shell tool's result that is Claude Code refusing the call, not the command failing: the user said no, a
 * permission rule or a hook stopped it. (A shell's own "Permission denied" is a failure and is not matched.)
 */
const REFUSAL =
  /doesn'?t want to proceed|rejected by the user|the user rejected|user denied|permission to use|permission for this|has been denied|blocked by (?:a |the )?hook|hook .{0,60}(?:blocked|denied)/i;

const STOP = new Set(
  (
    "a an and are as at be been but by can could did do does for from get got had has have how i if in into is it its just let " +
    "like me my of on or our please so than that the their them then there these they this to too us was we were what when where " +
    "which while who will with would you your also any all some more most make made need want should shall may might one two " +
    "never dont don't not no always must only avoid without ensure sure stop using doing yet still now first next"
  ).split(" "),
);

/**
 * Collapses whitespace.
 * @param {unknown} text
 * @returns {string}
 */
export function squash(text) {
  return typeof text === "string" ? text.replace(/\s+/g, " ").trim() : "";
}

/**
 * Cuts `text` to `max` characters, with an ellipsis, on a word edge when one is close.
 * @param {string} text
 * @param {number} [max]
 * @returns {string}
 */
export function trimText(text, max) {
  var limit = typeof max === "number" && max > 1 ? max : MAX_TEXT;
  var line = squash(text);
  if (line.length <= limit) return line;
  var cut = line.slice(0, limit - 1);
  var space = cut.lastIndexOf(" ");
  if (space > limit * 0.6) cut = cut.slice(0, space);
  return cut.replace(/[\s,;:.-]+$/, "") + "…";
}

/**
 * The part of `path` below `cwd`, or null when it is not under it. The comparison ignores case when the folder
 * starts with a drive letter, as Windows paths do.
 * @param {string} path  With forward slashes.
 * @param {string|null|undefined} cwd
 * @returns {string|null}
 */
function underCwd(path, cwd) {
  if (typeof cwd !== "string" || cwd === "") return null;
  var base = cwd.replace(/\\/g, "/").replace(/\/+$/, "");
  if (base === "") return null;
  var head = path.slice(0, base.length + 1);
  var same = /^[A-Za-z]:/.test(base) ? head.toLowerCase() === (base + "/").toLowerCase() : head === base + "/";
  return same && path.length > base.length + 1 ? path.slice(base.length + 1) : null;
}

/**
 * A path as the list shows it: relative to the working directory when it is under it, otherwise as it is,
 * cut to its last three segments when it is long.
 * @param {string} path
 * @param {string|null} [cwd]
 * @returns {string}
 */
function shortPath(path, cwd) {
  var clean = path.replace(/\\/g, "/");
  var inside = underCwd(clean, cwd);
  if (inside !== null) clean = inside;
  if (clean.length <= MAX_TEXT) return clean;
  var parts = clean.split("/");
  return "…/" + parts.slice(-3).join("/");
}

/**
 * Whether the list leaves an edited path out: a temp or scratch place (checked below the working directory when
 * the path is under it, so a project that lives in one is still listed) or one of Claude Code's plan files.
 * @param {string} path
 * @param {string|null} [cwd]
 * @returns {boolean}
 */
function isScratch(path, cwd) {
  var clean = path.replace(/\\/g, "/");
  var inside = underCwd(clean, cwd);
  return PLAN_FILE.test(clean) || SCRATCH_SEGMENT.test(inside === null ? clean : inside);
}

/**
 * A stable short id: kind letter plus a 32-bit FNV-1a hash of the lower-cased text.
 * @param {string} kind
 * @param {string} text
 * @returns {string}
 */
export function itemId(kind, text) {
  var key = squash(text).toLowerCase();
  var hash = 0x811c9dc5;
  for (var i = 0; i < key.length; i++) {
    hash ^= key.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return kind.charAt(0) + ("00000000" + hash.toString(16)).slice(-8);
}

/**
 * Lightly stems a lower-case word, so "reading", "reads" and "read" meet.
 * @param {string} word
 * @returns {string}
 */
function stem(word) {
  var w = word;
  if (w.length > 6 && w.endsWith("ing")) w = w.slice(0, -3);
  else if (w.length > 5 && w.endsWith("ed")) w = w.slice(0, -2);
  else if (w.length > 5 && w.endsWith("es")) w = w.slice(0, -2);
  else if (w.length > 3 && w.endsWith("s") && !w.endsWith("ss")) w = w.slice(0, -1);
  return w;
}

/**
 * The words of `text` that carry its meaning: lower-case, stemmed, no stop words,
 * no negations (those are checked on their own), no pure numbers (those are exact).
 * @param {string} text
 * @returns {string[]}
 */
export function contentWords(text) {
  var words = squash(text).toLowerCase().match(/[a-z][a-z0-9_]*/g) || [];
  /** @type {string[]} */
  var out = [];
  for (var i = 0; i < words.length; i++) {
    var raw = /** @type {string} */ (words[i]);
    if (raw.length < 3 || STOP.has(raw)) continue;
    var word = stem(raw);
    if (out.indexOf(word) < 0) out.push(word);
  }
  return out;
}

/**
 * Strings in `text` that a summary has to repeat exactly to have kept it:
 * URLs, ports, versions, ticket and commit ids, UUIDs, issue numbers, paths and
 * numbers of three digits or more.
 * @param {string} text
 * @returns {string[]}
 */
export function exactTokens(text) {
  var source = squash(text);
  /** @type {string[]} */
  var found = [];
  /** @param {string} value */
  function add(value) {
    var v = value.replace(/[.,;:)\]}>'"]+$/, "");
    if (v.length >= 2 && found.indexOf(v) < 0) found.push(v);
  }
  var rest = source;
  var urls = rest.match(/https?:\/\/[^\s)>\]"']+/g) || [];
  for (var u = 0; u < urls.length; u++) add(/** @type {string} */ (urls[u]));
  rest = rest.replace(/https?:\/\/[^\s)>\]"']+/g, " ");
  var paths = rest.match(/(?:[A-Za-z]:)?(?:[\w.-]+[\\/])+[\w.-]+\.[A-Za-z0-9]{1,8}\b|\b[\w-]+\.(?:ts|tsx|js|jsx|mjs|py|json|toml|md|yml|yaml|css|html|sql|sh|rs|go|java)\b/g) || [];
  for (var p = 0; p < paths.length; p++) add(/** @type {string} */ (paths[p]));
  rest = rest.replace(/(?:[A-Za-z]:)?(?:[\w.-]+[\\/])+[\w.-]+\.[A-Za-z0-9]{1,8}\b/g, " ");
  var uuid = rest.match(/\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/gi) || [];
  for (var q = 0; q < uuid.length; q++) add(/** @type {string} */ (uuid[q]));
  var tickets = rest.match(/\b[A-Z][A-Z0-9]{1,9}-\d{1,6}\b/g) || [];
  for (var t = 0; t < tickets.length; t++) add(/** @type {string} */ (tickets[t]));
  var issues = rest.match(/#\d{1,7}\b/g) || [];
  for (var s = 0; s < issues.length; s++) add(/** @type {string} */ (issues[s]));
  var hex = rest.match(/\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b/g) || [];
  for (var h = 0; h < hex.length; h++) add(/** @type {string} */ (hex[h]));
  var versions = rest.match(/\bv?\d+\.\d+(?:\.\d+){0,2}\b/g) || [];
  for (var v = 0; v < versions.length; v++) add(/** @type {string} */ (versions[v]));
  var numbers = rest.match(/\b\d{3,}(?:,\d{3})*\b/g) || [];
  for (var n = 0; n < numbers.length; n++) {
    var digits = /** @type {string} */ (numbers[n]);
    if (/^(?:19|20)\d\d$/.test(digits)) continue;
    add(digits);
  }
  return found.slice(0, 6);
}

/**
 * Splits a message into sentences and lines.
 * @param {string} text
 * @returns {string[]}
 */
function sentences(text) {
  var parts = text.slice(0, MAX_MESSAGE).split(/(?<=[.!?])\s+|\n+/);
  /** @type {string[]} */
  var out = [];
  for (var i = 0; i < parts.length; i++) {
    var sentence = squash(parts[i]);
    if (sentence.length >= 4 && sentence.length <= MAX_SENTENCE) out.push(sentence);
  }
  return out;
}

/**
 * True for a user message that is the user's own words, not a summary, a
 * system note, a skill's or command's text, pasted text (over MAX_USER_MESSAGE
 * characters) or a tool answer.
 * @param {MessageLike} message
 * @returns {boolean}
 */
function isUserWords(message) {
  if (message.role !== "user") return false;
  var text = squash(message.text);
  if (text === "" || NOISE_START.test(text) || COMMAND_HEADER.test(text)) return false;
  return text.length <= MAX_USER_MESSAGE;
}

/**
 * @param {string} kind
 * @param {string} text
 * @param {number} at
 * @param {string[]} [exact]
 * @param {string} [key]  What the id is made from, when the text alone would not tell items apart.
 * @returns {KeepItem}
 */
function make(kind, text, at, exact, key) {
  var line = trimText(text, MAX_TEXT);
  return {
    id: itemId(kind, typeof key === "string" ? key : line),
    kind: /** @type {KeepKind} */ (kind),
    text: line,
    safe: false,
    why: "",
    exact: exact || exactTokens(text),
    at: at,
  };
}

/**
 * A value in a sentence, as a short line around it.
 * @param {string} sentence
 * @param {string} value
 * @returns {string}
 */
function around(sentence, value) {
  var line = squash(sentence);
  if (line.length <= 100) return line;
  var at = line.indexOf(value);
  var start = Math.max(0, at - 40);
  return (start > 0 ? "…" : "") + line.slice(start, start + 100).trim();
}

/**
 * The first line of a plan, without markdown marks.
 * @param {string} plan
 * @returns {string}
 */
function planTitle(plan) {
  var lines = plan.split(/\n+/);
  for (var i = 0; i < lines.length; i++) {
    var line = squash(String(lines[i]).replace(/^[#>*\-\s\d.]+/, ""));
    if (line.length >= 4) return line;
  }
  return "";
}

/**
 * The tool uses of a message that changed a file, as `{ path }`.
 * @param {ToolUseLike} use
 * @returns {string|null}
 */
function editedPath(use) {
  if (!use || typeof use.tool !== "string" || EDIT_TOOLS.indexOf(use.tool) < 0 || use.isError === true) return null;
  var input = use.input || {};
  var path = input.file_path || input.notebook_path || input.path;
  return typeof path === "string" && path !== "" ? path : null;
}

/**
 * A key for "the same call again": the tool and, for a command, its first words.
 * @param {ToolUseLike} use
 * @returns {string}
 */
function callKey(use) {
  var input = use.input || {};
  var command = typeof input.command === "string" ? squash(input.command).slice(0, 60) : "";
  var path = typeof input.file_path === "string" ? input.file_path : "";
  return String(use.tool) + "|" + command + "|" + path;
}

/**
 * Whether a result is Claude Code refusing the call (the user said no, or a permission rule or hook stopped it)
 * rather than the command failing.
 * @param {string} text
 * @returns {boolean}
 */
function isRefusal(text) {
  return REFUSAL.test(String(text || ""));
}

/**
 * The line of a failing result worth keeping.
 * @param {string} text
 * @returns {string}
 */
function failureLine(text) {
  var lines = String(text || "").split(/\n+/);
  for (var i = 0; i < lines.length; i++) {
    var line = squash(lines[i]);
    if (FAILURE_LINE.test(line)) return line;
  }
  return squash(lines[0]);
}

/**
 * What to call a failed shell command: the call's own description when it has one ("Validate the plugin"), else the
 * command without the setup it starts with (leading `VAR=value` assignments and `cd dir &&` steps, see lib/command.js),
 * else the tool's name.
 * @param {ToolUseLike} use
 * @returns {string}
 */
function commandName(use) {
  var label = commandLabel(use.input);
  return label !== "" ? label : String(use.tool);
}

/**
 * The last shell command that failed and was not later run again with success. Only a shell command's failure
 * counts, whatever its output looks like: another tool's error, or a call the user or a permission rule refused,
 * is not something the user ran.
 * @param {MessageLike[]} messages
 * @returns {KeepItem|null}
 */
function lastError(messages) {
  /** @type {Set<string>} */
  var later = new Set();
  for (var i = messages.length - 1; i >= 0; i--) {
    var uses = (messages[i] || {}).toolUses || [];
    for (var j = uses.length - 1; j >= 0; j--) {
      var use = /** @type {ToolUseLike} */ (uses[j]);
      var key = callKey(use);
      if (use.isError === true) {
        if (SHELL_TOOLS.indexOf(String(use.tool)) < 0 || isRefusal(use.text || "")) continue;
        if (later.has(key)) continue;
        var command = commandName(use);
        var detail = failureLine(use.text || "");
        var text = detail !== "" ? trimText(command, 50) + " failed: " + detail : command + " failed";
        var item = make("error", text, i, exactTokens(detail));
        if (item.exact.length === 0 && /\w+::\w+/.test(detail)) item.exact = [(detail.match(/[\w/.-]+::\w+/) || [detail])[0]];
        return item;
      }
      if (typeof use.text === "string") later.add(key);
    }
  }
  return null;
}

/**
 * The open items of the last TodoWrite list.
 * @param {MessageLike[]} messages
 * @returns {KeepItem[]}
 */
function openTasks(messages) {
  for (var i = messages.length - 1; i >= 0; i--) {
    var uses = (messages[i] || {}).toolUses || [];
    for (var j = uses.length - 1; j >= 0; j--) {
      var use = /** @type {ToolUseLike} */ (uses[j]);
      if (use.tool !== "TodoWrite") continue;
      var todos = use.input && Array.isArray(use.input.todos) ? use.input.todos : [];
      /** @type {KeepItem[]} */
      var out = [];
      for (var k = 0; k < todos.length; k++) {
        var todo = todos[k] || {};
        var content = typeof todo.content === "string" ? todo.content : typeof todo.subject === "string" ? todo.subject : "";
        if (content === "" || todo.status === "completed") continue;
        out.push(make("task", content, i));
      }
      return out;
    }
  }
  return [];
}

/**
 * Files changed, newest change last; scratch and plan files are left out.
 * @param {MessageLike[]} messages
 * @param {string|null} [cwd]
 * @returns {KeepItem[]}
 */
function changedFiles(messages, cwd) {
  /** @type {Map<string, number>} */
  var last = new Map();
  for (var i = 0; i < messages.length; i++) {
    var uses = (messages[i] || {}).toolUses || [];
    for (var j = 0; j < uses.length; j++) {
      var path = editedPath(/** @type {ToolUseLike} */ (uses[j]));
      if (path === null || isScratch(path, cwd)) continue;
      last.delete(path);
      last.set(path, i + (j + 1) / 1000);
    }
  }
  /** @type {KeepItem[]} */
  var out = [];
  last.forEach(function (at, path) {
    var clean = path.replace(/\\/g, "/");
    var base = clean.slice(clean.lastIndexOf("/") + 1);
    var item = make("file", shortPath(path, cwd), at, [clean, base]);
    out.push(item);
  });
  return out;
}

/**
 * Values the user typed in a sentence.
 * @param {string} sentence
 * @param {number} at
 * @returns {KeepItem[]}
 */
function valuesIn(sentence, at) {
  var tokens = exactTokens(sentence);
  /** @type {KeepItem[]} */
  var out = [];
  var ports = sentence.match(/\bport\s*[:=]?\s*(\d{2,5})\b|\blocalhost:(\d{2,5})\b/gi) || [];
  for (var p = 0; p < ports.length; p++) {
    var digits = String(ports[p]).match(/\d{2,5}/);
    if (digits && tokens.indexOf(digits[0]) < 0) tokens.push(digits[0]);
  }
  for (var i = 0; i < tokens.length; i++) {
    var value = /** @type {string} */ (tokens[i]);
    if (/[\\/]/.test(value) && !/^https?:/.test(value)) continue;
    out.push(make("value", around(sentence, value), at, [value], value));
  }
  return out;
}

/**
 * The share of `item`'s meaning that `claudeMd` carries.
 * @param {KeepItem} item
 * @param {string} claudeMd
 * @returns {boolean}
 */
function inClaudeMd(item, claudeMd) {
  var haystack = claudeMd.toLowerCase();
  if (item.exact.length > 0) {
    for (var i = 0; i < item.exact.length; i++) if (haystack.indexOf(String(item.exact[i]).toLowerCase()) < 0) return false;
  }
  var words = contentWords(item.text);
  if (words.length === 0) return item.exact.length > 0;
  var have = new Set(contentWords(claudeMd));
  var hits = 0;
  for (var w = 0; w < words.length; w++) if (have.has(/** @type {string} */ (words[w]))) hits++;
  return hits / words.length >= (item.exact.length > 0 ? 0.5 : 0.75);
}

/**
 * Builds the keep list from a conversation, newest information winning.
 * Deterministic: the same messages, pins and options give the same list.
 * Items are capped per kind (LIMITS) and each text is one short line.
 * @param {MessageLike[]} messages  Oldest first.
 * @param {BuildOptions} [options]
 * @returns {KeepItem[]}
 */
export function buildKeepList(messages, options) {
  var opts = options || {};
  var list = Array.isArray(messages) ? messages : [];
  /** @type {KeepItem[]} */
  var instructions = [];
  /** @type {KeepItem[]} */
  var decisions = [];
  /** @type {KeepItem[]} */
  var values = [];

  for (var i = 0; i < list.length; i++) {
    var message = /** @type {MessageLike} */ (list[i]);
    var uses = message.toolUses || [];
    // An approved plan: ExitPlanMode that was not refused.
    for (var u = 0; u < uses.length; u++) {
      var use = /** @type {ToolUseLike} */ (uses[u]);
      if (use.tool === "ExitPlanMode" && use.isError !== true && use.input && typeof use.input.plan === "string") {
        var title = planTitle(use.input.plan);
        if (title !== "") {
          var plan = make("decision", title, i);
          plan.safe = true;
          plan.why = "plan";
          decisions.push(plan);
        }
      }
    }
    if (!isUserWords(message)) continue;
    var taken = 0;
    var parts = sentences(String(message.text));
    for (var s = 0; s < parts.length; s++) {
      var sentence = /** @type {string} */ (parts[s]);
      var isQuestion = /\?$/.test(sentence);
      // Drop the "I don't know" clause and test what is left, so "I don't know why, so never touch it" counts.
      var selfTalk = SELF_TALK.test(sentence);
      var rest = selfTalk ? sentence.replace(SELF_TALK, " ") : sentence;
      var correction = !selfTalk && CORRECTION.test(sentence);
      var told = INSTRUCTION.test(rest) || (selfTalk && THINK_SHOULD.test(sentence));
      if (!isQuestion && taken < PER_MESSAGE && (told || correction)) {
        instructions.push(make("instruction", sentence, i));
        taken += 1;
      } else if (!isQuestion && sentence.split(" ").length >= 4 && DECISION.test(sentence)) {
        decisions.push(make("decision", sentence, i));
      }
      var found = valuesIn(sentence, i);
      for (var f = 0; f < found.length; f++) values.push(/** @type {KeepItem} */ (found[f]));
    }
  }

  /** @type {Record<string, KeepItem[]>} */
  var byKind = {
    pin: (Array.isArray(opts.pins) ? opts.pins : [])
      .filter(function (pin) {
        return pin && squash(pin.text) !== "";
      })
      .map(function (pin, index) {
        return make("pin", pin.text, 1000000 + index);
      }),
    instruction: instructions,
    decision: decisions,
    error: [],
    task: openTasks(list),
    file: changedFiles(list, typeof opts.cwd === "string" ? opts.cwd : null),
    value: values,
  };
  var error = lastError(list);
  if (error !== null) byKind.error = [error];

  var recent = typeof opts.recentFiles === "number" ? opts.recentFiles : RECENT_FILES;
  var claudeMd = typeof opts.claudeMd === "string" && opts.claudeMd !== "" ? opts.claudeMd : null;

  /** @type {KeepItem[]} */
  var out = [];
  /** @type {Set<string>} */
  var seen = new Set();
  /** Exact strings an instruction already carries, so a value is not listed twice. */
  /** @type {Set<string>} */
  var covered = new Set();
  for (var c = 0; c < instructions.length; c++) {
    var carried = /** @type {KeepItem} */ (instructions[c]).exact;
    for (var e = 0; e < carried.length; e++) covered.add(String(carried[e]));
  }

  for (var k = 0; k < KINDS.length; k++) {
    var kind = /** @type {KeepKind} */ (KINDS[k]);
    var items = byKind[kind] || [];
    // Newest first, then the cap, so the cap drops the oldest.
    var ordered = items.slice().sort(function (a, b) {
      return b.at - a.at;
    });
    var kept = 0;
    for (var m = 0; m < ordered.length && kept < LIMITS[kind]; m++) {
      var item = /** @type {KeepItem} */ (ordered[m]);
      if (seen.has(item.id)) continue;
      if (kind === "value" && item.exact.length > 0 && covered.has(String(item.exact[0]))) continue;
      seen.add(item.id);
      if (kind === "file") {
        // The newest files kept, counting the ones listed (a file edited again is skipped above and takes no place).
        if (kept < recent) {
          item.safe = true;
          item.why = "recent";
        }
      } else if (kind !== "pin" && !item.safe && claudeMd !== null && inClaudeMd(item, claudeMd)) {
        item.safe = true;
        item.why = "claude-md";
      }
      out.push(item);
      kept += 1;
    }
  }
  return out;
}

/**
 * Splits the list into the items the "before" view suggests (pins first, then
 * instructions and decisions, newest first, none that are safe anyway) and the
 * rest, which sit behind "more".
 * @param {KeepItem[]} items
 * @param {number} [limit]
 * @returns {{ suggested: KeepItem[], more: KeepItem[] }}
 */
export function splitSuggested(items, limit) {
  var max = typeof limit === "number" && limit > 0 ? limit : SUGGEST_LIMIT;
  var list = Array.isArray(items) ? items : [];
  var pins = list.filter(function (item) {
    return item.kind === "pin";
  });
  var said = list
    .filter(function (item) {
      return (item.kind === "instruction" || item.kind === "decision") && !item.safe;
    })
    .sort(function (a, b) {
      return b.at - a.at;
    });
  var suggested = pins.concat(said).slice(0, max);
  var ids = new Set(
    suggested.map(function (item) {
      return item.id;
    }),
  );
  var more = list.filter(function (item) {
    return !ids.has(item.id) && !item.safe;
  });
  return { suggested: suggested, more: more };
}

/**
 * The text a pin keeps: one line, at most 160 characters.
 * @param {unknown} text
 * @returns {string}
 */
export function pinText(text) {
  return trimText(squash(text), 160);
}

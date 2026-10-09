/* claudeglass mod: lib/draft.js
 *
 * Checks on the draft in the prompt box, for the hint line and the guard:
 *
 *   draft_topic      a new topic in a large context: the draft shares next to
 *                    nothing with what the session has been about
 *   draft_no_paths   a change asked for with no file or function named, in a
 *                    session that has touched no file yet
 *   draft_read_all   Claude is asked to read the whole codebase
 *
 * The session's "bag" is the set of its content words (the words of the
 * prompts sent and the names of the files touched), kept by the caller in
 * memory only and never written anywhere. Nothing here reads a clock or the
 * engine, and nothing mutates its input. The numbers are ClaudeGlass's
 * (STALE_TOKENS is habits.STALE_TOKENS; a test holds them together).
 */

/**
 * Words the session has used, by stem, mapped to how often.
 * @typedef {Object<string, number>} Bag
 */

/**
 * What the draft looks like, whatever the size of the context.
 * @typedef {Object} Facts
 * @property {boolean} topic    Long enough, no markers of a continuation, and next to nothing in common with the session.
 * @property {boolean} noPaths  A change asked for, with nothing named, in a session that has touched no file.
 * @property {boolean} readAll  Asks Claude to read the whole codebase.
 * @property {number} words     Content words in the draft.
 * @property {number} shared    How many of them the session has used.
 */

/** The context, in tokens, past which a new topic is worth starting fresh for (habits.STALE_TOKENS). */
export var STALE_TOKENS = 20000;
/** Content words a draft needs before it can count as a new topic. */
export var MIN_TOPIC_WORDS = 6;
/** Words in the session's bag before it says enough to tell a new topic. */
export var MIN_BAG_WORDS = 20;
/** Words a draft needs before a missing file name is worth a hint. */
export var MIN_PATH_WORDS = 8;
/** Most words the bag keeps; the oldest go first. */
export var MAX_BAG_WORDS = 4000;
/** How much of a draft is looked at. */
export var MAX_TEXT_CHARS = 4000;

/** @type {string[]} */
var STOP_LIST = (
  "a about above after again all also am an and any are as at be because been before being below between both but by " +
  "can could did do does doing done down during each few for from further had has have having he her here hers him his " +
  "how i if in into is it its itself just let like me more most my myself no nor not now of off on once only or other " +
  "our ours out over own same she should so some such than that the their theirs them then there these they this those " +
  "through to too under until up very was we were what when where which while who whom why will with would you your yours " +
  "yet via per etc eg ie " +
  "please thanks thank want wants need needs needed help helps get gets got getting give gives make makes made making " +
  "take takes put puts use uses used using try tries trying see sees look looks looking show shows run runs running " +
  "write writes wrote create creates created add adds added change changes changed update updates updated fix fixes fixed " +
  "check checks checked go goes going went come comes know knows think thinks say says tell tells ask asks " +
  "work works working worked way ways thing things stuff something anything everything nothing one two three first second " +
  "new old good bad better best big small little much many lot lots really actually maybe perhaps sure okay ok yes yeah " +
  "code file files function functions class classes method methods project projects repo repos app apps " +
  "tsx jsx mjs cjs css html json toml yaml yml txt src lib dist node pyc"
).split(" ");

/** @type {Object<string, boolean>} */
var STOP = {};
STOP_LIST.forEach(function (word) {
  STOP[word] = true;
});

/**
 * A word cut down to a stem, so "tests", "tested" and "testing" meet: a
 * plural s, then ing or ed, then a final e.
 * @param {string} word  Lower case.
 * @returns {string}
 */
export function stem(word) {
  var out = word;
  if (out.length > 4 && /ies$/.test(out)) out = out.slice(0, -3) + "y";
  else if (out.length > 3 && /s$/.test(out) && !/ss$/.test(out)) out = out.slice(0, -1);
  if (out.length > 5 && /ing$/.test(out)) out = out.slice(0, -3);
  else if (out.length > 4 && /ed$/.test(out)) out = out.slice(0, -2);
  if (out.length > 3 && /e$/.test(out)) out = out.slice(0, -1);
  return out.length >= 3 ? out : word;
}

/**
 * The content words of a text, as stems, in order and without repeats:
 * identifiers are split where they change case or at an underscore, words
 * of under three letters and the stop list are left out.
 * @param {string} text
 * @returns {string[]}
 */
export function contentWords(text) {
  var found = String(text || "").slice(0, MAX_TEXT_CHARS).match(/[A-Za-z][A-Za-z0-9]*/g) || [];
  /** @type {string[]} */
  var out = [];
  /** @type {Object<string, boolean>} */
  var seen = {};
  found.forEach(function (token) {
    token
      .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
      .replace(/([A-Z]+)([A-Z][a-z])/g, "$1 $2")
      .split(" ")
      .forEach(function (part) {
        var word = part.toLowerCase();
        if (word.length < 3 || STOP[word] === true) return;
        var stemmed = stem(word);
        if (STOP[stemmed] === true || seen[stemmed] === true) return;
        seen[stemmed] = true;
        out.push(stemmed);
      });
  });
  return out;
}

/** @returns {Bag} */
export function emptyBag() {
  return {};
}

/**
 * How many words the bag holds.
 * @param {Bag} bag
 * @returns {number}
 */
export function bagSize(bag) {
  return Object.keys(bag || {}).length;
}

/**
 * The bag with `words` added (a copy). Past MAX_BAG_WORDS the words added
 * longest ago are dropped.
 * @param {Bag} bag
 * @param {string[]} words
 * @returns {Bag}
 */
export function addWords(bag, words) {
  /** @type {Bag} */
  var next = Object.assign({}, bag);
  words.forEach(function (word) {
    // Deleting first moves a word seen again to the newest end.
    var count = (next[word] || 0) + 1;
    delete next[word];
    next[word] = count;
  });
  var keys = Object.keys(next);
  for (var i = 0; i < keys.length - MAX_BAG_WORDS; i++) delete next[/** @type {string} */ (keys[i])];
  return next;
}

/**
 * The bag with the words of a message the person sent added.
 * @param {Bag} bag
 * @param {string} text
 * @returns {Bag}
 */
export function learnPrompt(bag, text) {
  return addWords(bag, contentWords(text));
}

/**
 * The words of a file's name (its last part, without the extension).
 * @param {string} path
 * @returns {string[]}
 */
export function fileWords(path) {
  var name = String(path || "")
    .split(/[\\/]/)
    .pop();
  if (!name) return [];
  var dot = name.lastIndexOf(".");
  return contentWords(dot > 0 ? name.slice(0, dot) : name);
}

/**
 * The bag with the words of a file's name added.
 * @param {Bag} bag
 * @param {string} path
 * @returns {Bag}
 */
export function learnFile(bag, path) {
  return addWords(bag, fileWords(path));
}

/**
 * True when the text is a slash command (/clear, /compact keep this), not a path.
 * @param {string} text
 * @returns {boolean}
 */
export function isSlash(text) {
  return /^\s*\/[A-Za-z][\w:-]*(\s|$)/.test(String(text || ""));
}

/** @type {RegExp[]} */
var CONTINUATION = [
  // Pointing back at the conversation.
  /\b(this|these|those|it|its|them|above|below|again|also|same|instead|already|still|another|previous|earlier|former|latter)\b/,
  /\bthat\s*(?:$|[.,;:!?]|one\b|error\b|file\b|test\b|tests\b|bug\b|function\b|part\b|change\b|code\b|output\b|issue\b|thing\b|works?\b|was\b|is\b)/,
  /(?:^|[.!?]\s+)that\b/,
  /\bthe\s+(error|failure|failures|failing|failed|last|previous|first|second|other|same|above|result|stack\s*trace|traceback)\b/,
  /\bas\s+(?:discussed|mentioned|before|above|planned|agreed|said)\b/,
  /\b(?:you|we)\s+(?:said|wrote|mentioned|suggested|proposed|told|gave|made|just|did|discussed|agreed|decided)\b/,
  /\byour\s+(?:last|previous|earlier|plan|answer|suggestion|reply|response|proposal|summary)\b/,
  /\b(?:last|previous|earlier|prior)\s+(?:message|reply|answer|step|output|error|result|run|commit|change|one)\b/,
  /\b(?:continue|proceed|carry\s+on|keep\s+going|go\s+ahead|go\s+on|next\s+step|do\s+that|try\s+again|one\s+more|as\s+well|too)\b/,
  // A word of agreement or sequence that opens a message.
  /^\s*(?:ok|okay|yes|yep|yeah|sure|thanks|thank\s+you|great|good|nice|cool|now|next|then|and|but|so|actually|wait|hmm|right|alright|fine)\b/,
  // Quoted earlier output.
  /^\s*>/m,
  /^\s*(?:error|traceback|exception|warning)[:\s]/m,
  /\bat\s+line\s+\d+/,
];

/**
 * True when the draft points back at the conversation: a demonstrative or
 * "again", "the error", "same", an opening "ok" or "now", a quoted line.
 * @param {string} text
 * @returns {boolean}
 */
export function hasContinuation(text) {
  var lower = String(text || "").slice(0, MAX_TEXT_CHARS).toLowerCase();
  return CONTINUATION.some(function (pattern) {
    return pattern.test(lower);
  });
}

/**
 * How many whitespace-separated words a text has.
 * @param {string} text
 * @returns {number}
 */
export function wordCount(text) {
  var found = String(text || "")
    .trim()
    .match(/\S+/g);
  return found === null ? 0 : found.length;
}

var CHANGE_VERB =
  /\b(fix|fixes|fixing|change|changing|update|updating|add|adding|remove|removing|delete|deleting|refactor|refactoring|rename|renaming|implement|implementing|debug|debugging|rewrite|rewriting|modify|modifying|replace|replacing|move|moving|extract|extracting|optimi[sz]e|optimi[sz]ing|migrate|migrating|convert|converting|patch|patching|wire|hook\s+up)\b/i;

var EXTENSIONS =
  "js|jsx|ts|tsx|mjs|cjs|py|pyi|rb|go|rs|java|kt|swift|c|h|cc|cpp|hpp|cs|php|sh|bash|ps1|sql|md|mdx|txt|json|yaml|yml|toml|ini|cfg|conf|env|xml|html|htm|css|scss|vue|svelte|lock|csv|log|ipynb|gradle|tf";

/** @type {RegExp[]} */
var NAMES_TARGET = [
  // A path: a folder separator inside a word, a drive, ./ ../ ~/.
  /[\w.-]+[\\/][\w.\\/-]+/,
  /\b[A-Za-z]:\\/,
  /(?:^|\s)(?:\.{1,2}|~)[\\/]/,
  // A file name with an extension.
  new RegExp("\\b[\\w-]+\\.(?:" + EXTENSIONS + ")\\b", "i"),
  // A backticked name.
  /`[^`\n]+`/,
  // snake_case, camelCase, PascalCase, a call such as parse(), a dotted member.
  /\b[a-z][a-z0-9]*_[a-z0-9_]+\b/,
  /\b[a-z]+[A-Z][A-Za-z0-9]*\b/,
  /\b[A-Z][a-z0-9]+[A-Z][A-Za-z0-9]*\b/,
  /\b[A-Za-z_]\w*\(/,
  /\b[A-Za-z_]\w*\.[A-Za-z_]\w*\(/,
];

/**
 * True when the text names something to open: a path, a file name with an
 * extension, a backticked name, or a code identifier.
 * @param {string} text
 * @returns {boolean}
 */
export function namesTarget(text) {
  var body = String(text || "").slice(0, MAX_TEXT_CHARS);
  return NAMES_TARGET.some(function (pattern) {
    return pattern.test(body);
  });
}

/**
 * True when the text asks for a change to code (fix, update, rename, ...).
 * @param {string} text
 * @returns {boolean}
 */
export function asksForChange(text) {
  return CHANGE_VERB.test(String(text || "").slice(0, MAX_TEXT_CHARS));
}

var EVERYTHING =
  "(?:whole|entire|full|complete|all\\s+(?:of\\s+)?(?:the|my|our|this)?|every|each|everything\\s+in)\\s*(?:of\\s+)?(?:the\\s+|my\\s+|our\\s+|this\\s+)?(?:\\w+\\s+)?";
var READ_ALL = new RegExp(
  "\\b(?:read|explore|scan|go\\s+through|look\\s+(?:at|through|over)|review|analy[sz]e|study|understand|index|crawl|open|skim|search\\s+through|survey)\\b[^.!?\\n]{0,40}?\\b" +
    EVERYTHING +
    "(?:codebase|code\\s*base|repo|repository|project|source(?:\\s+code)?|files|code|app|application|monorepo|directory|folder)\\b" +
    "|\\b(?:all|every)\\s+(?:of\\s+)?(?:the\\s+|my\\s+|our\\s+)?(?:files|source\\s+files)\\b[^.!?\\n]{0,30}?\\b(?:in|of)\\s+(?:the\\s+|my\\s+|our\\s+|this\\s+)(?:repo|repository|project|codebase)\\b" +
    "|\\b(?:read|explore|scan|review|look\\s+at|go\\s+through|open)\\s+(?:through\\s+)?every\\s+(?:single\\s+)?file\\b",
  "i",
);
var SCOPED = /\b(?:in|under|inside|within)\s+(?:the\s+|my\s+|our\s+)?[`'"]?[\w.-]*[\\/]|\b(?:in|under|inside|within)\s+(?:the\s+|my\s+|our\s+)?[`'"]?[\w.-]+[`'"]?\s+(?:folder|directory|dir|package|module)\b/i;

/**
 * True when the draft asks Claude to read the whole codebase (the whole
 * repo, all the files, every file), unless it narrows that to a folder.
 * @param {string} text
 * @returns {boolean}
 */
export function asksToReadAll(text) {
  var body = String(text || "").slice(0, MAX_TEXT_CHARS);
  var match = READ_ALL.exec(body);
  if (match === null) return false;
  var after = body.slice(match.index + match[0].length, match.index + match[0].length + 60);
  if (SCOPED.test(after) || SCOPED.test(match[0])) return false;
  return true;
}

/**
 * What the draft looks like. `files` is how many files the session has
 * touched so far.
 * @param {string} text
 * @param {Bag} bag
 * @param {number} files
 * @returns {Facts}
 */
export function draftFacts(text, bag, files) {
  var body = String(text || "").slice(0, MAX_TEXT_CHARS);
  var facts = { topic: false, noPaths: false, readAll: false, words: 0, shared: 0 };
  if (body.trim() === "" || isSlash(body)) return facts;
  var words = contentWords(body);
  var shared = 0;
  words.forEach(function (word) {
    if (Object.prototype.hasOwnProperty.call(bag || {}, word)) shared += 1;
  });
  facts.words = words.length;
  facts.shared = shared;
  var continues = hasContinuation(body);
  facts.topic =
    !continues && words.length >= MIN_TOPIC_WORDS && bagSize(bag) >= MIN_BAG_WORDS && shared <= 1 && shared / words.length <= 0.2;
  facts.noPaths = !continues && files === 0 && wordCount(body) >= MIN_PATH_WORDS && asksForChange(body) && !namesTarget(body);
  facts.readAll = asksToReadAll(body);
  return facts;
}

/**
 * The one check that applies, in priority order, for a context of `ctx`
 * tokens, or null. A new topic needs a context worth clearing.
 * @param {Facts} facts
 * @param {number} ctx
 * @returns {"draft_topic"|"draft_no_paths"|"draft_read_all"|null}
 */
export function pickDraft(facts, ctx) {
  if (facts.topic && ctx >= STALE_TOKENS) return "draft_topic";
  if (facts.noPaths) return "draft_no_paths";
  if (facts.readAll) return "draft_read_all";
  return null;
}

/**
 * The draft's check for a context of `ctx` tokens, or null.
 * @param {{ text: string, bag: Bag, files: number, ctx: number }} input
 * @returns {"draft_topic"|"draft_no_paths"|"draft_read_all"|null}
 */
export function checkDraft(input) {
  return pickDraft(draftFacts(input.text, input.bag, input.files), input.ctx);
}

/**
 * A short string that changes when the facts change in a way a hint could
 * follow, so the caller redraws only then.
 * @param {Facts} facts
 * @returns {string}
 */
export function factsKey(facts) {
  return (facts.topic ? "t" : "-") + (facts.noPaths ? "p" : "-") + (facts.readAll ? "r" : "-");
}

/**
 * The tokens a fresh start would leave out of every reply: the context less
 * what a session starts with.
 * @param {number} ctx
 * @param {number|null} startup
 * @returns {number}
 */
export function carriedTokens(ctx, startup) {
  var base = typeof startup === "number" && isFinite(startup) && startup > 0 ? startup : 0;
  return Math.max(0, ctx - base);
}

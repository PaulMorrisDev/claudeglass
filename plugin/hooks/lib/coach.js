/* claudeglass mod: lib/coach.js
 *
 * The coaching the capture hook gives after a message is sent
 * (src/claudeglass/hooks/capture_hook.py: _cold_hint, _poll_hint,
 * _drip_hint, _paste_hint, _fresh_hint, _reminder_due), decided here from
 * what the mod sees instead: the draft before it is sent, the replies so
 * far, the background work still running. The same thresholds, the same
 * order of hints and the same text patterns (generated/coaching.js
 * PATTERNS, from capture_catalogue); the rest rules are lib/rest.js.
 * Nothing here reads a clock, a file or the engine, and nothing keeps
 * your words: the draft's text is looked at, answered with yes or no, and
 * dropped. Times are seconds, sizes tokens, characters turn into tokens at
 * the calibrated rate (charsPerToken).
 */

import { mayShow } from "./rest.js";

/**
 * One pattern as the generated assets carry it: Python's source with the
 * flags JavaScript spells.
 * @typedef {{source: string, flags: string}} RawPattern
 */

/**
 * generated/coaching.js PATTERNS.
 * @typedef {Object} RawPatterns
 * @property {RawPattern} status
 * @property {number} status_max_chars
 * @property {RawPattern} go
 * @property {number} go_max_chars
 * @property {RawPattern} ack
 * @property {RawPattern} review
 * @property {RawPattern} asks
 * @property {RawPattern} change
 * @property {number} change_scan_chars
 * @property {RawPattern} config_path
 * @property {RawPattern} shell_write
 * @property {readonly string[]} edit_tools
 * @property {RawPattern} background_launch
 * @property {number} background_scan_chars
 * @property {RawPattern} task_notification_call
 * @property {string} task_notification_prefix
 * @property {RawPattern} reply_fence
 * @property {RawPattern} reply_tip_block
 * @property {RawPattern} reply_inline_code
 * @property {RawPattern} reply_url
 * @property {RawPattern} reply_quoted
 * @property {RawPattern} reply_reminder_line
 * @property {RawPattern} reply_tags
 * @property {RawPattern} reply_list_start
 * @property {RawPattern} reply_unit
 * @property {number} reply_scan_chars
 * @property {string} reply_question_trim
 * @property {string} feedback_skill
 */

/**
 * The patterns, compiled (compilePatterns).
 * @typedef {Object} Patterns
 * @property {RegExp} status
 * @property {number} statusMax
 * @property {RegExp} go
 * @property {number} goMax
 * @property {RegExp} ack
 * @property {RegExp} review
 * @property {RegExp} asks
 * @property {RegExp} change
 * @property {number} changeScan
 * @property {RegExp} configPath
 * @property {RegExp} shellWrite
 * @property {readonly string[]} editTools
 * @property {RegExp} backgroundLaunch
 * @property {number} backgroundScan
 * @property {RegExp} notificationCall
 * @property {string} notificationPrefix
 * @property {RegExp} fence
 * @property {RegExp} tipBlock
 * @property {RegExp} inlineCode
 * @property {RegExp} url
 * @property {RegExp} quoted
 * @property {RegExp} reminderLine
 * @property {RegExp} tags
 * @property {RegExp} listStart
 * @property {RegExp} unit
 * @property {number} replyScan
 * @property {string} questionTrim
 * @property {string} feedbackSkill
 */

/**
 * The thresholds used here (capture_catalogue.COACHING_THRESHOLDS with the
 * user's overrides), with those of the rest rule.
 * @typedef {Object} Thresholds
 * @property {number} big_paste_tokens
 * @property {number} cold_min_tokens
 * @property {number} cold_rest_hours
 * @property {number} cooldown_minutes
 * @property {number} drip_chars
 * @property {number} drip_count
 * @property {number} drip_window_minutes
 * @property {number} max_backoff
 * @property {number} plan_fresh_tokens
 * @property {number} rating_min_tokens
 * @property {number} rating_rest_days
 * @property {number} rating_typical_factor
 * @property {number} rearm_factor
 * @property {number} warm_prefix_tokens
 */

/**
 * A hint that applies: its kind, what is at stake (for the rest rule) and
 * the figures its words need.
 * @typedef {Object} Found
 * @property {string} kind
 * @property {number} stake
 * @property {Object<string, number>} fields
 */

/** Characters per token when nothing was measured (calibration.FALLBACK). */
export var FALLBACK_CHARS_PER_TOKEN = 4;

/**
 * The hints decided before sending, in the order the capture hook tries
 * them: the first that applies, isn't muted and isn't resting wins.
 * @type {string[]}
 */
export var HINT_ORDER = ["drip_feed", "big_paste", "status_poll", "cold_return"];

/** A draft longer than this is not looked at for its words, only counted. */
export var READ_CHARS = 1024;

var SHELL_TOOLS = ["Bash", "PowerShell"];

// -- patterns ---------------------------------------------------------------

/**
 * @param {RawPattern} spec
 * @param {string} extraFlags
 * @param {"full" | "start" | "search"} whole
 */
function build(spec, extraFlags, whole) {
  var flags = spec.flags;
  for (var i = 0; i < extraFlags.length; i++) if (flags.indexOf(extraFlags.charAt(i)) === -1) flags += extraFlags.charAt(i);
  var source = whole === "full" ? "^(?:" + spec.source + ")$" : whole === "start" ? "^(?:" + spec.source + ")" : spec.source;
  return new RegExp(source, flags);
}

/**
 * The generated patterns (coaching.js PATTERNS) as the regular expressions
 * the functions below use, each matched the way the hook matches it.
 * @param {RawPatterns} raw
 * @returns {Patterns}
 */
export function compilePatterns(raw) {
  return {
    status: build(raw.status, "i", "full"),
    statusMax: raw.status_max_chars,
    go: build(raw.go, "i", "full"),
    goMax: raw.go_max_chars,
    ack: build(raw.ack, "i", "full"),
    review: build(raw.review, "i", "start"),
    asks: build(raw.asks, "i", "start"),
    change: build(raw.change, "i", "search"),
    changeScan: raw.change_scan_chars,
    configPath: build(raw.config_path, "", "search"),
    shellWrite: build(raw.shell_write, "i", "search"),
    editTools: raw.edit_tools,
    backgroundLaunch: build(raw.background_launch, "", "search"),
    backgroundScan: raw.background_scan_chars,
    notificationCall: build(raw.task_notification_call, "g", "search"),
    notificationPrefix: raw.task_notification_prefix,
    fence: build(raw.reply_fence, "g", "search"),
    tipBlock: build(raw.reply_tip_block, "g", "search"),
    inlineCode: build(raw.reply_inline_code, "g", "search"),
    url: build(raw.reply_url, "g", "search"),
    quoted: build(raw.reply_quoted, "g", "search"),
    reminderLine: build(raw.reply_reminder_line, "g", "search"),
    tags: build(raw.reply_tags, "", "search"),
    listStart: build(raw.reply_list_start, "", "search"),
    unit: build(raw.reply_unit, "", "search"),
    replyScan: raw.reply_scan_chars,
    questionTrim: raw.reply_question_trim,
    feedbackSkill: raw.feedback_skill,
  };
}

// -- what a message is -------------------------------------------------------

/**
 * A message that only asks how the work is going (prompt_shape.is_status).
 * @param {string} text
 * @param {Patterns} p
 */
export function isStatus(text, p) {
  var t = text.trim();
  return t.length < p.statusMax && p.status.test(t);
}

/**
 * A message that only tells Claude to carry on (prompt_shape.is_go).
 * @param {string} text
 * @param {Patterns} p
 */
export function isGo(text, p) {
  var t = text.trim();
  return t.length <= p.goMax && p.go.test(t);
}

/**
 * A message that asks something (prompt_shape.is_question).
 * @param {string} text
 * @param {Patterns} p
 */
export function isQuestion(text, p) {
  var t = text.trim();
  return t.slice(-1) === "?" || p.asks.test(t);
}

/**
 * A message that asks Claude to change something, and is not a thank-you, a
 * go-ahead, a status check, a question or a request to look
 * (prompt_shape.is_change_request).
 * @param {string} text
 * @param {Patterns} p
 */
export function isChangeRequest(text, p) {
  var t = text.trim();
  if (t === "") return false;
  if (!p.change.test(t.slice(0, p.changeScan))) return false;
  return !(p.ack.test(t) || isGo(t, p) || isStatus(t, p) || isQuestion(t, p) || p.review.test(t));
}

/**
 * @param {string} text
 * @param {string} chars
 */
function rstripChars(text, chars) {
  var end = text.length;
  while (end > 0 && chars.indexOf(text.charAt(end - 1)) !== -1) end--;
  return text.slice(0, end);
}

/**
 * @param {string} unit
 * @param {Patterns} p
 */
function asks(unit, p) {
  return rstripChars(unit.trimEnd(), p.questionTrim).slice(-1) === "?";
}

/**
 * Whether a reply ends on a question to you (prompt_shape.ends_on_question,
 * as capture_hook._ends_on_question): the message after it answers it.
 * @param {string} text
 * @param {Patterns} p
 */
export function endsOnQuestion(text, p) {
  var t = text.replace(p.fence, " ");
  t = t.replace(p.tipBlock, "");
  t = t.replace(p.inlineCode, " ").replace(p.url, " ");
  var prose = t.replace(p.reminderLine, "").replace(p.quoted, " ");
  prose = prose.trimEnd().replace(p.tags, "").trimEnd();
  var tail = prose.slice(-p.replyScan);
  /** @type {string[]} */
  var units = [];
  var parts = tail.split(p.unit);
  for (var i = 0; i < parts.length; i++) {
    var part = parts[i];
    if (part !== undefined && part.trim() !== "") units.push(part);
  }
  var end = units.length;
  while (end > 0) {
    var last = /** @type {string} */ (units[end - 1]);
    if (!p.listStart.test(last)) break;
    if (asks(last, p)) return true;
    end--;
  }
  for (var j = Math.max(0, end - 2); j < end; j++) if (asks(/** @type {string} */ (units[j]), p)) return true;
  return false;
}

/**
 * Whether a tool call changes a file of yours: an edit tool aimed outside a
 * .claude folder, or a shell command that writes one (capture_hook._changes_file).
 * @param {string} name
 * @param {Object<string, unknown>|null|undefined} input  The call's arguments.
 * @param {Patterns} p
 */
export function changesFile(name, input, p) {
  var args = input && typeof input === "object" ? input : {};
  if (p.editTools.indexOf(name) !== -1) {
    var path = args.file_path || args.notebook_path;
    return !(typeof path === "string" && p.configPath.test(path));
  }
  if (SHELL_TOOLS.indexOf(name) !== -1) {
    var command = args.command;
    return typeof command === "string" && p.shellWrite.test(command) && !p.configPath.test(command);
  }
  return false;
}

/**
 * Whether a prompt is the /cg-feedback command, with or without arguments
 * (capture_hook._is_feedback_prompt).
 * @param {string} text
 * @param {string} skill
 */
export function isFeedbackPrompt(text, skill) {
  if (typeof text !== "string") return false;
  var head = text.trimStart().slice(0, skill.length + 2);
  return head.indexOf("/" + skill) === 0 && (head.length === skill.length + 1 || /\s/.test(head.slice(-1)));
}

/**
 * Whether a reply carries a tag that marks a new piece of work
 * ([cg: ... shift=new]).
 * @param {string} text
 */
export function marksNewPiece(text) {
  var tag = /\[(?:cg|tl):([^[\]\n]{0,400})\]/gi;
  var found;
  while ((found = tag.exec(text)) !== null) if (/\bshift=new\b/.test(found[1] || "")) return true;
  return false;
}

// -- tokens ------------------------------------------------------------------

/**
 * A model id less its [1m] tag and date suffix, lower-cased; "" for none or
 * a synthetic one (calibration.model_family).
 * @param {string|null|undefined} model
 */
export function modelFamily(model) {
  if (typeof model !== "string") return "";
  var name = model.trim().toLowerCase();
  if (name === "" || name.charAt(0) === "<") return "";
  if (name.slice(-4) === "[1m]") name = name.slice(0, -4);
  return name.replace(/-\d{8}$/, "");
}

/**
 * Characters per token of text on `model`: the calibration's figure for its
 * family, else for the family most sessions run on, else 4.
 * @param {{text: Object<string, number>, family: string}|null|undefined} calibration
 * @param {string|null|undefined} model
 */
export function charsPerToken(calibration, model) {
  var text = calibration && calibration.text ? calibration.text : {};
  var family = modelFamily(model) || (calibration && calibration.family) || "";
  var value = Object.prototype.hasOwnProperty.call(text, family) ? text[family] : null;
  return typeof value === "number" && isFinite(value) && value > 0 ? value : FALLBACK_CHARS_PER_TOKEN;
}

// -- the draft ---------------------------------------------------------------

/**
 * What the mod keeps of the draft in the box.
 * @typedef {Object} Draft
 * @property {number} len      Characters in the box.
 * @property {number} hidden   Characters of pastes the box shows in a shorter form.
 * @property {boolean} small   Short enough to be a small request.
 * @property {boolean} change  A small message that asks for a change.
 * @property {boolean} status  A message that only asks how the work is going.
 */

/** @returns {Draft} */
export function emptyDraft() {
  return { len: 0, hidden: 0, small: false, change: false, status: false };
}

/**
 * The draft after one edit of the prompt box, without reading the whole of
 * it: the length comes from the box's, a paste the box shortens is counted
 * from what was pasted, and the words are looked at only while the draft is
 * short enough to be a small request. Constant work however long the draft.
 * @param {Draft} draft   Before the edit.
 * @param {{text: string, start: number, end: number, inputText: string}} edit  prompt.edit's input.
 * @param {{text: string}} after  The box after the edit.
 * @param {Patterns} p
 * @param {number} dripChars  The longest small request (drip_chars).
 * @returns {Draft}
 */
export function editDraft(draft, edit, after, p, dripChars) {
  var len = after.text.length;
  if (len === 0) return emptyDraft();
  var kept = len - (edit.text.length - (edit.end - edit.start));
  var lost = edit.inputText.length - kept;
  var hidden = draft.hidden + (lost > 0 ? lost : 0);
  if (draft.hidden > 0 && edit.inputText === "" && edit.end > edit.start && edit.text.slice(edit.start, Math.min(edit.end, edit.start + 400)).indexOf("[Pasted") !== -1) hidden = 0;
  if (len > READ_CHARS) return { len: len, hidden: hidden, small: false, change: false, status: false };
  var small = hidden === 0 && after.text.trim().length <= dripChars;
  var short = hidden === 0 && len <= p.statusMax + 64;
  return {
    len: len,
    hidden: hidden,
    small: small,
    change: small && isChangeRequest(after.text, p),
    status: short && isStatus(after.text, p),
  };
}

/**
 * A word for the parts of a draft that decide a hint, so a redraw is asked
 * for only when one changes.
 * @param {Draft} draft
 * @param {number} cpt   Characters per token.
 * @param {number} bigTokens  big_paste_tokens.
 */
export function draftShape(draft, cpt, bigTokens) {
  var big = (draft.len + draft.hidden) / cpt >= bigTokens;
  return (big ? "b" : "-") + (draft.small ? "s" : "-") + (draft.change ? "c" : "-") + (draft.status ? "q" : "-") + (draft.len > 0 ? "d" : "-");
}

// -- the hints ---------------------------------------------------------------

/**
 * `big_paste` for a draft of `chars` characters: about big_paste_tokens or
 * more (capture_hook._paste_hint, which counts the characters of the sent
 * message; here of the draft).
 * @param {number} chars
 * @param {number} cpt
 * @param {Thresholds} th
 * @returns {Found|null}
 */
export function pasteHint(chars, cpt, th) {
  if (!(chars > 0)) return null;
  var tokens = chars / cpt;
  return tokens >= th.big_paste_tokens ? { kind: "big_paste", stake: tokens, fields: { tokens: tokens } } : null;
}

/**
 * One message sent earlier.
 * @typedef {Object} Exchange
 * @property {boolean} answer    It answered a question Claude asked.
 * @property {boolean} answered  Claude's reply to it ended.
 * @property {boolean} change    It asked for a change.
 * @property {boolean} small     It was short.
 * @property {boolean} edited    The reply it started changed a file.
 * @property {number|null} since Seconds after the message before it.
 */

/**
 * How many small change requests in a row the draft makes: it and the
 * messages before it, each short, asking for a change and sent within
 * drip_window_minutes of the one before, the earlier ones each answered
 * with a file change (capture_hook._drip_count).
 * @param {Exchange[]} earlier  Oldest first.
 * @param {{small: boolean, change: boolean}} draft
 * @param {number|null} since  Seconds since the last message sent.
 * @param {boolean} answer     The draft answers a question Claude asked.
 * @param {Thresholds} th
 * @returns {number}
 */
export function dripCount(earlier, draft, since, answer, th) {
  var window = th.drip_window_minutes * 60;
  if (!draft.small || !draft.change || answer || since === null || since > window) return 0;
  var count = 1;
  for (var i = earlier.length - 1; i >= 0; i--) {
    var ex = /** @type {Exchange} */ (earlier[i]);
    if (ex.answer || !ex.answered || !ex.change) continue;
    if (!(ex.small && ex.edited && ex.since !== null && ex.since <= window)) break;
    count++;
  }
  return count;
}

/**
 * `drip_feed`: drip_count or more small change requests in a row
 * (capture_hook._drip_hint).
 * @param {Exchange[]} earlier
 * @param {{small: boolean, change: boolean}} draft
 * @param {number|null} since
 * @param {boolean} answer
 * @param {number} ctx   The context the last reply left, for the figure.
 * @param {Thresholds} th
 * @returns {Found|null}
 */
export function dripHint(earlier, draft, since, answer, ctx, th) {
  var count = dripCount(earlier, draft, since, answer, th);
  return count >= th.drip_count ? { kind: "drip_feed", stake: count, fields: { count: count, ctx: ctx } } : null;
}

/**
 * The newest reply: the context it left, when, and how long its cache lives.
 * @typedef {{ctx: number, at: number, ttl: number}} LastReply
 */

/**
 * `status_poll` for a draft that only asks how the work is going while
 * background work is out and the cache is warm (capture_hook._poll_hint).
 * After a break that outlasted the cache, the cold-return hint speaks.
 * @param {boolean} status   The draft is a status check.
 * @param {LastReply|null} last
 * @param {number} nowS
 * @param {boolean} pending  Background work is still out.
 * @returns {Found|null}
 */
export function pollHint(status, last, nowS, pending) {
  if (!status || last === null || nowS - last.at > last.ttl || !pending) return null;
  return { kind: "status_poll", stake: last.ctx, fields: { ctx: last.ctx } };
}

/**
 * `cold_return`: the gap since the newest reply is longer than its cache
 * lifetime and the context it left is cold_min_tokens or more
 * (capture_hook._cold_hint). Silent after a compaction since that reply,
 * and when a usage limit stopped the work and you are back within a cache
 * lifetime of its reset (or the reset isn't known).
 * @param {LastReply|null} last
 * @param {number} nowS
 * @param {boolean} compactedSince
 * @param {{stopped: boolean, reset: number|null}} limit  `reset` in seconds.
 * @param {Thresholds} th
 * @returns {Found|null}
 */
export function coldHint(last, nowS, compactedSince, limit, th) {
  if (last === null || last.ctx < th.cold_min_tokens) return null;
  var idle = nowS - last.at;
  if (idle <= last.ttl || compactedSince) return null;
  if (limit.stopped && (limit.reset === null || nowS - limit.reset <= last.ttl)) return null;
  var ctx = Math.max(0, last.ctx - th.warm_prefix_tokens);
  return { kind: "cold_return", stake: last.ctx, fields: { idle: idle, ctx: ctx } };
}

/**
 * The first of `candidates` (in the hook's order) that isn't muted and
 * isn't resting; `keep`, the hint already on screen, needs no new leave.
 * @param {Array<Found|null>} candidates
 * @param {string[]} muted
 * @param {import('./rest.js').RestState} rest
 * @param {number} nowS
 * @param {Thresholds} th
 * @param {string|null} [keep]
 * @returns {Found|null}
 */
export function pickHint(candidates, muted, rest, nowS, th, keep) {
  for (var i = 0; i < candidates.length; i++) {
    var one = candidates[i];
    if (!one || muted.indexOf(one.kind) !== -1) continue;
    if (one.kind !== keep && !mayShow(rest, one.kind, one.stake, nowS, th)) continue;
    return one;
  }
  return null;
}

/**
 * The rest table with the rows the render hooks stamped since (module
 * memory, which a render may write) laid over the snapshot's: the newer
 * row of a kind stands.
 * @param {import('./rest.js').RestState|null|undefined} base
 * @param {import('./rest.js').RestState|null|undefined} overlay
 * @returns {import('./rest.js').RestState}
 */
export function mergeRest(base, overlay) {
  var hints = Object.assign({}, base && base.hints);
  var over = (overlay && overlay.hints) || {};
  Object.keys(over).forEach(function (kind) {
    var mine = over[kind];
    var theirs = hints[kind];
    if (mine && (!theirs || typeof theirs.ts !== "number" || mine.ts >= theirs.ts)) hints[kind] = mine;
  });
  return { hints: hints };
}

// -- plan_fresh ---------------------------------------------------------------

/**
 * What a fresh session starts with: the first request's context less your
 * first message (handoff.starting_context).
 * @param {number} firstCtx
 * @param {number} firstPromptChars
 * @param {number} cpt
 */
export function startingContext(firstCtx, firstPromptChars, cpt) {
  return Math.max(0, firstCtx - Math.floor(firstPromptChars / cpt));
}

/**
 * `plan_fresh` for a plan of `planChars` characters approved after a lot of
 * planning: what a fresh start would drop is the approving request's
 * context less the session's starting context and the plan
 * (capture_hook._fresh_hint). Nothing when the start isn't known, or the
 * session was compacted since it began.
 * @param {number|null} ctx    The context of the request that asked to leave plan mode.
 * @param {number|null} start  startingContext(), or null when unknown.
 * @param {number} planChars
 * @param {boolean} compacted
 * @param {number} cpt
 * @param {Thresholds} th
 * @returns {Found|null}
 */
export function freshHint(ctx, start, planChars, compacted, cpt, th) {
  if (ctx === null || start === null) return null;
  var kept = ctx - start - Math.floor(planChars / cpt);
  if (kept < th.plan_fresh_tokens || compacted) return null;
  return { kind: "plan_fresh", stake: kept, fields: { kept: kept } };
}

// -- background work ---------------------------------------------------------

/**
 * Work that went to the background and has not reported back.
 * @typedef {Object} BackgroundItem
 * @property {string} id    The tool call's id.
 * @property {number} at    When it started, seconds.
 */

/**
 * @typedef {Object} Background
 * @property {BackgroundItem[]} items  Oldest first.
 */

/** Work launched in the background is forgotten after this long without a word. */
export var BACKGROUND_FORGET_S = 6 * 3600;
var MAX_BACKGROUND = 32;

/** @returns {Background} */
export function emptyBackground() {
  return { items: [] };
}

/**
 * Work a tool launched in the background: its result says so
 * (launchedInBackground, capture_hook._background_pending). A call counts
 * once. Work forgotten for BACKGROUND_FORGET_S is dropped.
 * @param {Background} state
 * @param {string} id  The call's id.
 * @param {number} nowS
 * @returns {Background}
 */
export function backgroundLaunched(state, id, nowS) {
  var items = state.items.filter(function (one) {
    return nowS - one.at < BACKGROUND_FORGET_S;
  });
  if (!items.some(function (one) { return one.id === id; })) items.push({ id: id, at: nowS });
  return { items: items.slice(-MAX_BACKGROUND) };
}

/**
 * A task notification arrived: it ends the wait for the calls it names, or
 * for all of them when it names none (capture_hook._background_pending).
 * @param {Background} state
 * @param {string} text  The notification message.
 * @param {Patterns} p
 * @returns {Background}
 */
export function backgroundNotified(state, text, p) {
  /** @type {string[]} */
  var called = [];
  var re = new RegExp(p.notificationCall.source, "g");
  var found;
  while ((found = re.exec(text)) !== null) if (found[1] !== undefined) called.push(found[1]);
  if (called.length === 0) return emptyBackground();
  return {
    items: state.items.filter(function (one) {
      return called.indexOf(one.id) === -1;
    }),
  };
}

/**
 * Whether a tool result's text says the work went to the background.
 * @param {string|undefined} text
 * @param {Patterns} p
 */
export function launchedInBackground(text, p) {
  return typeof text === "string" && p.backgroundLaunch.test(text.slice(0, p.backgroundScan));
}

/**
 * Whether a message is a background task's notification.
 * @param {string} text
 * @param {Patterns} p
 */
export function isNotification(text, p) {
  return typeof text === "string" && text.trimStart().indexOf(p.notificationPrefix) === 0;
}

// -- who coaching is for -----------------------------------------------------

var SLUG_MAX_CHARS = 200;

/**
 * A pattern from the user's own `[capture] projects` / `exclude_projects`,
 * compiled as Python's `re.search(pattern, slug, re.IGNORECASE)` reads it;
 * null for one that does not compile (it matches nothing, as in the hook).
 * @param {string} pattern
 * @returns {RegExp|null}
 */
function slugPattern(pattern) {
  try {
    return new RegExp(pattern, "i");
  } catch (e) {
    return null;
  }
}

/**
 * @param {string} pattern
 * @param {string} slug
 */
function slugMatches(pattern, slug) {
  var re = slugPattern(pattern);
  return re !== null && re.test(slug);
}

/**
 * The project slug of a folder (capture_hook.slug_for): `name` when Claude
 * Code names the project itself (CLAUDE_CODE_PROJECT_DIR_NAME), else every
 * character that is not a letter or digit as "-". A path over 200
 * characters is cut there and ends "-" and eight hex digits of a hash; the
 * hash is not reproduced (zeros stand in), so a pattern that depends on
 * those eight digits does not match here as it does in the hook.
 * @param {string} cwd
 * @param {string} [name]
 * @returns {string}
 */
export function projectSlug(cwd, name) {
  if (typeof name === "string" && name !== "") return name;
  var slug = cwd.replace(/[^A-Za-z0-9]/g, "-");
  return slug.length <= SLUG_MAX_CHARS ? slug : slug.slice(0, SLUG_MAX_CHARS) + "-00000000";
}

/**
 * Whether the project is one `[capture] projects` and `exclude_projects`
 * leave in (capture_hook.project_allowed): an excluded pattern, or a
 * "!pattern" in `projects`, that matches the slug leaves it out; with
 * ordinary patterns in `projects` it has to match one. Without a folder
 * there is no filter to apply (the hook lets the call through).
 * @param {string|null|undefined} cwd
 * @param {readonly string[]} projects
 * @param {readonly string[]} excluded
 * @param {string} [name]  CLAUDE_CODE_PROJECT_DIR_NAME, when set.
 * @returns {boolean}
 */
export function projectApplies(cwd, projects, excluded, name) {
  if (typeof cwd !== "string" || cwd === "") return true;
  var slug = projectSlug(cwd, name);
  if (excluded.some(function (pattern) { return slugMatches(pattern, slug); })) return false;
  /** @type {string[]} */
  var includes = [];
  for (var j = 0; j < projects.length; j++) {
    var one = String(projects[j]);
    if (one.charAt(0) === "!") {
      if (slugMatches(one.slice(1), slug)) return false;
    } else includes.push(one);
  }
  if (includes.length === 0) return true;
  return includes.some(function (pattern) { return slugMatches(pattern, slug); });
}

/**
 * The later of the times a rating reminder last went out, in seconds: the
 * hook's (from the settings file) and the mod's own (mod/coach.json).
 * @param {number|null|undefined} hook
 * @param {number|null|undefined} mod
 * @returns {number|null}
 */
export function latestReminder(hook, mod) {
  var a = typeof hook === "number" && isFinite(hook) ? hook : null;
  var b = typeof mod === "number" && isFinite(mod) ? mod : null;
  if (a === null) return b;
  if (b === null) return a;
  return Math.max(a, b);
}

/**
 * Whether the session was rated on the dashboard since this piece of work
 * began (capture_hook._reminder_due): a rating counts when the piece's start
 * is not known, or the rating came at or after it.
 * @param {number|null|undefined} ratedAt  Seconds, null for never rated.
 * @param {number|null|undefined} pieceAt  When the piece began, seconds; null when unknown.
 * @returns {boolean}
 */
export function ratedSince(ratedAt, pieceAt) {
  if (typeof ratedAt !== "number" || !isFinite(ratedAt)) return false;
  return typeof pieceAt !== "number" || ratedAt >= pieceAt;
}

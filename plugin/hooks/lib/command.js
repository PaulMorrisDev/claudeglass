/* claudeglass mod: lib/command.js
 *
 * What to call a shell command in a list: the keep list's failed command and the
 * heaviest items on the Now page say the same words for the same call. Pure text
 * work; nothing here reads a clock, the disk or the engine.
 */

/** A leading `VAR=value` step: an unquoted value stops at a space, `;` or `&`, so `T=/c/x&&npm test` keeps `npm test`. */
var ASSIGNMENT = /^[A-Za-z_][A-Za-z0-9_]*=(?:"[^"]*"|'[^']*'|[^\s;&]*)(?:\s*(?:&&|;)\s*|\s+|$)/;
/** A leading `cd dir &&` or `cd dir;` step. */
var HOP = /^cd\s+(?:"[^"]*"|'[^']*'|[^\s;&]+)\s*(?:&&|;)\s*/;

/**
 * @param {unknown} value
 * @returns {string}
 */
function words(value) {
  return typeof value === "string" ? value.replace(/\s+/g, " ").trim() : "";
}

/**
 * A command without the setup it starts with: the leading `VAR=value` assignments and `cd dir &&` / `cd dir;` hops.
 * A command that is nothing but setup is returned as it was.
 * @param {string} command
 * @returns {string}
 */
export function bareCommand(command) {
  var whole = words(command);
  var rest = whole;
  for (;;) {
    var next = rest.replace(ASSIGNMENT, "").replace(HOP, "");
    if (next === rest) break;
    rest = next;
  }
  return rest !== "" ? rest : whole;
}

/**
 * What a shell call did, in a few words: its `description` input when it has one ("Validate the plugin"), else its
 * command without the setup it starts with (`bareCommand`). "" when the input has neither.
 * @param {unknown} input  The arguments the model gave the tool.
 * @param {number} [max]  The most characters of a command kept (all of it when left out).
 * @param {number} [describedMax]  The most characters of a description kept (`max` when left out).
 * @returns {string}
 */
export function commandLabel(input, max, describedMax) {
  if (input === null || typeof input !== "object") return "";
  var args = /** @type {Object<string, unknown>} */ (input);
  var described = words(args.description);
  if (described !== "") return cut(described, typeof describedMax === "number" ? describedMax : max);
  var command = words(args.command);
  return command === "" ? "" : cut(bareCommand(command), max);
}

/**
 * @param {string} text
 * @param {number|undefined} max
 * @returns {string}
 */
function cut(text, max) {
  return typeof max === "number" ? text.slice(0, max).trim() : text;
}

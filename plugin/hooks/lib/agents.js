/* claudeglass mod: lib/agents.js
 *
 * What the Agents page and the agent rows work out from the agents' own
 * events: the words for what an agent is doing, a model's short name and
 * tier, the order of the rows, and an agent's share of the session's cost.
 * None of it has a Python twin except the skip list (tests/test_mod_agents.py
 * holds that to the catalogue's).
 *
 * Plain functions over plain values: no clock, no state, no I/O.
 */

/**
 * The agent types whose runs are never coached on
 * (capture_catalogue.SKIP_AGENT_TYPES; the exported copy is generated/coaching.js).
 * @type {string[]}
 */
export var SKIP_AGENT_TYPES = ["statusline-setup", "output-style-setup"];

/**
 * @typedef {"searching"|"reading"|"editing"|"running"|"fetching"|"working"} Activity
 */

/**
 * What an agent is doing, in the dashboard's plain words' keys, from the
 * tool it called last.
 * @param {string|null|undefined} tool
 * @returns {Activity}
 */
export function activityOf(tool) {
  switch (tool) {
    case "WebSearch":
    case "WebFetch":
      return "fetching";
    case "Read":
      return "reading";
    case "Edit":
    case "MultiEdit":
    case "Write":
    case "NotebookEdit":
      return "editing";
    case "Bash":
    case "PowerShell":
    case "BashOutput":
      return "running";
    case "Grep":
    case "Glob":
      return "searching";
    default:
      return "working";
  }
}

/**
 * A model's tier, for its colour: Fable counts with Opus (charts.js modelTier).
 * @param {string|null|undefined} model
 * @returns {"opus"|"sonnet"|"haiku"|"other"}
 */
export function tierOf(model) {
  var name = String(model || "").toLowerCase();
  if (name.indexOf("opus") !== -1 || name.indexOf("fable") !== -1) return "opus";
  if (name.indexOf("sonnet") !== -1) return "sonnet";
  if (name.indexOf("haiku") !== -1) return "haiku";
  return "other";
}

/**
 * The model as a row writes it: its family ("haiku"), or for a model of no
 * family the id without a "claude-" lead, cut to ten characters.
 * @param {string|null|undefined} model
 * @returns {string}
 */
export function shortModel(model) {
  var name = String(model || "").toLowerCase();
  var families = ["opus", "sonnet", "haiku", "fable"];
  for (var i = 0; i < families.length; i++) {
    var family = String(families[i]);
    if (name.indexOf(family) !== -1) return family;
  }
  var plain = name.replace(/^claude-/, "");
  return plain.length > 10 ? plain.slice(0, 10) : plain;
}

/**
 * The name a row carries: the agent type, else the task's short
 * description, cut to `max` characters with an ellipsis.
 * @param {string|null|undefined} type
 * @param {string|null|undefined} description
 * @param {number} [max]
 * @returns {string}
 */
export function agentLabel(type, description, max) {
  var limit = typeof max === "number" && max > 1 ? max : 22;
  var text = String(type || "").trim() || String(description || "").trim() || "agent";
  return text.length > limit ? text.slice(0, limit - 1) + "…" : text;
}

/**
 * @typedef {Object} RowLike
 * @property {string} status  "running", "done" or "failed".
 * @property {number} startedAt
 * @property {number|null} endedAt
 */

/**
 * The rows in the order the page lists them: running first, in the order
 * they started; then the finished, the newest to end first.
 * @template {RowLike} T
 * @param {T[]} rows
 * @returns {T[]}
 */
export function sortAgents(rows) {
  var running = rows.filter(function (one) {
    return one.status === "running";
  });
  var finished = rows.filter(function (one) {
    return one.status !== "running";
  });
  running.sort(function (a, b) {
    return a.startedAt - b.startedAt;
  });
  finished.sort(function (a, b) {
    return (b.endedAt === null ? b.startedAt : b.endedAt) - (a.endedAt === null ? a.startedAt : a.endedAt);
  });
  return running.concat(finished);
}

/**
 * The agents' share of the session's cost, as a whole percent from 0 to
 * 100; 0 while the session's cost isn't known yet.
 * @param {number} agentUsd
 * @param {number|null|undefined} sessionUsd
 * @returns {number}
 */
export function agentShare(agentUsd, sessionUsd) {
  if (typeof sessionUsd !== "number" || !isFinite(sessionUsd) || !(sessionUsd > 0)) return 0;
  var share = Math.round((Math.max(0, agentUsd) / sessionUsd) * 100);
  return Math.min(100, Math.max(0, share));
}

/**
 * capture_hook._k: tokens as "150k", thousands grouped, rounded half to
 * even as Python's round() does.
 * @param {number} tokens
 * @returns {string}
 */
export function kText(tokens) {
  var thousands = tokens / 1000;
  var floor = Math.floor(thousands);
  var diff = thousands - floor;
  var rounded = diff < 0.5 ? floor : diff > 0.5 ? floor + 1 : floor % 2 === 0 ? floor : floor + 1;
  return String(rounded).replace(/\B(?=(\d{3})+(?!\d))/g, ",") + "k";
}

/* claudeglass mod: lib/agents.js
 *
 * What the Agents page and the agent rows work out from the agents' own
 * events: the words for what an agent is doing, a model's short name and
 * tier, the order of the rows, an agent's share of the session's cost, and
 * the two coaching notes that belong to agent runs, report_reread and
 * split_run. The two notes are ports of capture_hook._report_hint and
 * capture_hook._split_hint / keep_run (tests/test_mod_agents.py runs both
 * over the same cases); the rest has no Python twin.
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

/**
 * @typedef {Object} Decision
 * @property {string} kind
 * @property {number} stake
 * @property {Object<string, string>} fields  Fills the tip's {placeholders}.
 */

/**
 * report_reread: an agent started in the background while the main session
 * holds `report_reread_tokens` or more. Each report it sends back is
 * answered by a reply that reads the whole session again. Only an agent
 * the main session started counts (a workflow's agents have no row of
 * their own to show it on).
 * @param {{background: boolean, ctx: number|null, th: {report_reread_tokens: number}}} input
 * @returns {Decision|null}
 */
export function reportReread(input) {
  if (!input.background) return null;
  var ctx = input.ctx;
  if (typeof ctx !== "number" || !isFinite(ctx) || ctx < input.th.report_reread_tokens) return null;
  return { kind: "report_reread", stake: Math.trunc(ctx), fields: { ctx: kText(Math.trunc(ctx)) } };
}

/**
 * What one finished run adds to the split_run count (keep_run): a run
 * counts when it summarised its own context part-way through, or began
 * from a brief of `split_brief_chars` characters or more. Types in
 * `skip` are never coached on.
 * @param {{type: string, compacted: boolean, briefChars: number, th: {split_brief_chars: number}, skip?: string[]}} input
 * @returns {{compaction: number, brief: number}}
 */
export function splitCounts(input) {
  var skip = input.skip || SKIP_AGENT_TYPES;
  if (skip.indexOf(input.type) !== -1) return { compaction: 0, brief: 0 };
  return {
    compaction: input.compacted ? 1 : 0,
    brief: input.briefChars >= input.th.split_brief_chars ? 1 : 0,
  };
}

/**
 * The runs that ended too big and have not been told yet.
 * @typedef {Object} Split
 * @property {number} compaction
 * @property {number} brief
 */

/** @returns {Split} */
export function newSplit() {
  return { compaction: 0, brief: 0 };
}

/**
 * `total` and one more run's counts.
 * @param {Split} total
 * @param {Split} one
 * @returns {Split}
 */
export function addSplit(total, one) {
  return { compaction: total.compaction + one.compaction, brief: total.brief + one.brief };
}

/**
 * Groups thousands the way Python's "{:,}" does.
 * @param {number} n
 * @returns {string}
 */
function grouped(n) {
  return String(Math.trunc(n)).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

/**
 * split_run (_split_hint): the runs counted since it was last told. The
 * stake is how many there are.
 * @param {{compaction: number, brief: number, th: {split_brief_chars: number}, why: Object<string, string>}} input
 *   `why` is capture_catalogue.COACHING_SPLIT_WHY.
 * @returns {Decision|null}
 */
export function splitRun(input) {
  var compaction = Math.trunc(input.compaction || 0);
  var brief = Math.trunc(input.brief || 0);
  if (compaction <= 0 && brief <= 0) return null;
  var word = compaction > 0 && brief > 0 ? "both" : compaction > 0 ? "compaction" : "brief";
  var template = input.why[word] || "";
  var text = template.replace(/\{brief_chars\}/g, grouped(input.th.split_brief_chars));
  return { kind: "split_run", stake: compaction + brief, fields: { why: text } };
}

/* claudeglass mod: lib/instructions.js
 *
 * What the Instructions page works out from the events that load things into
 * Claude's context: a ledger of items (a system prompt section, a CLAUDE.md
 * or rules file, a skill, a tool's description, an attachment), each with its
 * size in characters, kept in the order it loaded; then the rows the page
 * draws (tool descriptions rolled up into one row for the built-in tools, one
 * for the deferred tools and one per MCP server), the startup total, the
 * largest items, the one line of advice, and which agent runs received
 * CLAUDE.md. Sizes become tokens only here, at the calibrated characters per
 * token the caller passes in.
 *
 * Plain functions over plain values: no clock, no state, no I/O. Names and
 * paths pass through to the page and nowhere else.
 */

/**
 * The kinds of item, in the order the page lists their groups when none
 * loaded earlier than another.
 * @type {string[]}
 */
export var KINDS = ["system", "instructions", "context", "skills", "tools", "deferred", "mcp", "attachment"];

/** Tokens an MCP server's tool descriptions must reach, in the prompt, before the page advises turning it off. */
export var MCP_TOKENS = 10000;

/** Tokens a CLAUDE.md or rules file must reach before the page advises trimming it. */
export var FILE_TOKENS = 5000;

/**
 * The attachment types that are part of what a session starts with, as
 * /context counts it: the MCP servers' instructions and the agent listing.
 * (The skill listing is its own kind. The `instructions` attachment carries
 * the CLAUDE.md files a second time, and the deferred tools' names are not in
 * /context's startup figure, so neither counts here.)
 */
export var STARTUP_ATTACHMENTS = ["mcp_instructions_delta", "agent_listing_delta"];

/** The tools rolled up into the largest-first names a row shows. */
var BIGGEST = 3;

/**
 * One thing that loaded into the context.
 * @typedef {Object} Item
 * @property {string} id        Stable key: loading it again replaces it.
 * @property {string} kind      One of KINDS.
 * @property {string} name      What the page calls it (a section id, a file path, a skill or tool name).
 * @property {number} chars     Its text, in characters.
 * @property {number} seq       Its place in the load order, from 1.
 * @property {number} reply     The reply that was under way when it loaded (1 is the first); 0 when not known.
 * @property {string} from      Where it came from: engine, managed, user, project, local, memory, imported, scoped, rules, plugin, mcp, skill, attachment.
 * @property {boolean} startup  Part of what the session starts with.
 * @property {string} [path]    A file's absolute path.
 * @property {string} [parent]  The file that imported it.
 * @property {string} [server]  An MCP tool's server.
 * @property {boolean} [deferred]  A tool whose schema is not in the prompt until it is asked for.
 * @property {boolean} [scoped]    A file that loaded because Claude touched matching paths.
 * @property {boolean} [measured]  Its size came from the text Claude read, not from reading the file again.
 * @property {string[]} [globs]    The patterns that made a scoped file load.
 * @property {number} [count]      Attachments of this type so far (one item holds them all).
 */

/**
 * @typedef {Object} Ledger
 * @property {string|null} session  The session the items belong to.
 * @property {Item[]} items         In load order.
 * @property {number} seq           The last sequence number given.
 * @property {boolean} composed     The system prompt has been recorded.
 * @property {Object<string, boolean>} agents  Agent id to whether its context carried CLAUDE.md.
 */

/** @returns {Ledger} */
export function newLedger() {
  return { session: null, items: [], seq: 0, composed: false, agents: {} };
}

/**
 * Empties the ledger when `session` is not the one it holds.
 * @param {Ledger} ledger
 * @param {string|null} session
 * @returns {boolean} True when it was emptied.
 */
export function follow(ledger, session) {
  if (ledger.session === session) return false;
  var had = ledger.items.length > 0 || ledger.session !== null;
  ledger.session = session;
  ledger.items = [];
  ledger.seq = 0;
  ledger.composed = false;
  ledger.agents = {};
  return had;
}

/**
 * Adds an item, or replaces the one with the same id (a reload after a
 * summary) keeping its place and the reply it first loaded in.
 * @param {Ledger} ledger
 * @param {Omit<Item, "seq">} item
 * @returns {boolean} True when the ledger changed.
 */
export function record(ledger, item) {
  for (var i = 0; i < ledger.items.length; i++) {
    var was = /** @type {Item} */ (ledger.items[i]);
    if (was.id !== item.id) continue;
    var next = Object.assign({}, was, item, { seq: was.seq, reply: was.reply || item.reply });
    if (was.measured === true && item.measured !== true) {
      next.chars = was.chars;
      next.measured = true;
    }
    var changed = next.chars !== was.chars || next.name !== was.name || next.deferred !== was.deferred || next.startup !== was.startup;
    ledger.items[i] = next;
    return changed;
  }
  ledger.seq += 1;
  ledger.items.push(Object.assign({}, item, { seq: ledger.seq }));
  return true;
}

/**
 * Adds an attachment's text to the item for its type (one item holds every
 * attachment of a type).
 * @param {Ledger} ledger
 * @param {string} type
 * @param {number} chars
 * @param {number} reply
 */
export function addAttachment(ledger, type, chars, reply) {
  var id = "att:" + type;
  var existing = ledger.items.find(function (one) {
    return one.id === id;
  });
  if (existing === undefined) {
    record(ledger, { id: id, kind: "attachment", name: type, chars: chars, reply: reply, from: "attachment", startup: STARTUP_ATTACHMENTS.indexOf(type) >= 0 && reply <= 1, count: 1 });
    return;
  }
  existing.chars += chars;
  existing.count = (existing.count || 1) + 1;
}

/**
 * Tokens for `chars` characters of text at `cpt` characters per token.
 * @param {number} chars
 * @param {number} cpt  Falls back to 4 when not a positive number.
 */
export function tokensOf(chars, cpt) {
  var per = typeof cpt === "number" && isFinite(cpt) && cpt > 0 ? cpt : 4;
  return Math.round(chars / per);
}

/**
 * The server of an MCP tool: the provider says it (`mcp:<server>`), else the
 * name does (`mcp__<server>__<tool>`); "" for a tool that is not an MCP one.
 * @param {string} tool
 * @param {string|null|undefined} provider  The provider's plugin name.
 */
export function mcpServer(tool, provider) {
  if (typeof provider === "string" && provider.indexOf("mcp:") === 0 && provider.length > 4) return provider.slice(4);
  if (tool.indexOf("mcp__") !== 0) return "";
  var rest = tool.slice(5);
  var at = rest.indexOf("__");
  return at > 0 ? rest.slice(0, at) : rest;
}

/**
 * A section id or attachment type as words ("doing_tasks" is "doing tasks").
 * @param {string} id
 */
export function humanize(id) {
  return id.replace(/[_-]+/g, " ").trim();
}

/**
 * The last two parts of a path, for a row ("project/CLAUDE.md").
 * @param {string} path
 */
export function shortPath(path) {
  var parts = path.replace(/\\/g, "/").split("/").filter(function (part) {
    return part !== "";
  });
  return parts.slice(-2).join("/");
}

/**
 * One row of the page: an item, or the tools of one group rolled up.
 * @typedef {Object} Entry
 * @property {string} key       Names the row in the page's state.
 * @property {string} kind
 * @property {string} name      The item's own name; "" for a rolled-up group (the page names it).
 * @property {number} chars     Everything the row stands for, in characters.
 * @property {number} startupChars  The part of it the session starts with and keeps in the prompt.
 * @property {number} count     Tools in a rolled-up row, attachments of a type, else 1.
 * @property {number} seq       When its first item loaded.
 * @property {number} reply
 * @property {string} from
 * @property {boolean} deferred Everything in it waits behind tool search.
 * @property {boolean} scoped
 * @property {string} [server]
 * @property {string} [path]
 * @property {string} [parent]
 * @property {string[]} [globs]
 * @property {{name: string, chars: number}[]} biggest  A rolled-up row's largest tools.
 */

/**
 * The rows the page draws, in load order.
 * @param {Item[]} items
 * @returns {Entry[]}
 */
export function entriesOf(items) {
  /** @type {Entry[]} */
  var out = [];
  /** @type {Object<string, Entry>} */
  var rolled = {};
  for (var i = 0; i < items.length; i++) {
    var item = /** @type {Item} */ (items[i]);
    var live = item.startup && item.deferred !== true ? item.chars : 0;
    var group = item.kind === "tools" || item.kind === "deferred" ? item.kind : item.kind === "mcp" ? "mcp:" + (item.server || "") : "";
    if (group !== "") {
      var row = rolled[group];
      if (row === undefined) {
        row = {
          key: group,
          kind: item.kind,
          name: "",
          chars: 0,
          startupChars: 0,
          count: 0,
          seq: item.seq,
          reply: item.reply,
          from: item.from,
          deferred: true,
          scoped: false,
          biggest: [],
        };
        if (item.kind === "mcp") row.server = item.server || "";
        rolled[group] = row;
        out.push(row);
      }
      row.chars += item.chars;
      row.startupChars += live;
      row.count += 1;
      if (item.deferred !== true) row.deferred = false;
      row.biggest.push({ name: item.name, chars: item.chars });
      row.biggest.sort(function (a, b) {
        return b.chars - a.chars;
      });
      row.biggest.splice(BIGGEST);
      continue;
    }
    /** @type {Entry} */
    var one = {
      key: item.id,
      kind: item.kind,
      name: item.name,
      chars: item.chars,
      startupChars: live,
      count: item.count || 1,
      seq: item.seq,
      reply: item.reply,
      from: item.from,
      deferred: false,
      scoped: item.scoped === true,
      biggest: [],
    };
    if (item.path !== undefined) one.path = item.path;
    if (item.parent !== undefined) one.parent = item.parent;
    if (item.globs !== undefined) one.globs = item.globs;
    out.push(one);
  }
  return out;
}

/**
 * Characters the session starts with, over every row.
 * @param {Entry[]} entries
 */
export function startupChars(entries) {
  var total = 0;
  for (var i = 0; i < entries.length; i++) total += /** @type {Entry} */ (entries[i]).startupChars;
  return total;
}

/**
 * The `n` largest rows of the startup context, largest first (ties in load order).
 * @param {Entry[]} entries
 * @param {number} n
 */
export function largest(entries, n) {
  return entries
    .filter(function (one) {
      return one.startupChars > 0;
    })
    .sort(function (a, b) {
      return b.startupChars - a.startupChars || a.seq - b.seq;
    })
    .slice(0, n);
}

/**
 * The one thing worth fixing, if something is: an MCP server whose tool
 * descriptions sit in the prompt at `mcpTokens` or more, or a CLAUDE.md or
 * rules file at `fileTokens` or more; the larger of the two kinds' worst.
 * @param {Entry[]} entries
 * @param {number} cpt
 * @param {{mcp?: number, file?: number}} [limits]
 * @returns {{kind: "mcp"|"file", name: string, tokens: number}|null}
 */
export function adviceFor(entries, cpt, limits) {
  var mcpAt = limits && typeof limits.mcp === "number" ? limits.mcp : MCP_TOKENS;
  var fileAt = limits && typeof limits.file === "number" ? limits.file : FILE_TOKENS;
  /** @type {{kind: "mcp"|"file", name: string, tokens: number}|null} */
  var best = null;
  for (var i = 0; i < entries.length; i++) {
    var one = /** @type {Entry} */ (entries[i]);
    /** @type {{kind: "mcp"|"file", name: string, tokens: number}|null} */
    var found = null;
    if (one.kind === "mcp") {
      var live = tokensOf(one.startupChars, cpt);
      if (live >= mcpAt) found = { kind: "mcp", name: one.server || "", tokens: live };
    } else if (one.kind === "instructions") {
      var size = tokensOf(one.chars, cpt);
      if (size >= fileAt) found = { kind: "file", name: one.path !== undefined ? shortPath(one.path) : one.name, tokens: size };
    }
    if (found !== null && (best === null || found.tokens > best.tokens)) best = found;
  }
  return best;
}

/**
 * One group of the Detailed list: a kind.
 * @typedef {Object} Group
 * @property {string} kind
 * @property {Entry[]} entries
 */

/**
 * The rows grouped by kind, groups in the order their first row loaded, rows in load order.
 * @param {Entry[]} entries
 * @returns {Group[]}
 */
export function groupsOf(entries) {
  /** @type {Group[]} */
  var out = [];
  for (var i = 0; i < entries.length; i++) {
    var one = /** @type {Entry} */ (entries[i]);
    var group = out.find(function (g) {
      return g.kind === one.kind;
    });
    if (group === undefined) {
      group = { kind: one.kind, entries: [] };
      out.push(group);
    }
    group.entries.push(one);
  }
  return out;
}

/**
 * Whether an agent's conversation opened with a CLAUDE.md block, from its
 * messages in the API form; null when they can't say.
 * @param {unknown} messages
 * @returns {boolean|null}
 */
export function receivedClaudeMd(messages) {
  if (!Array.isArray(messages)) return null;
  var seen = 0;
  for (var i = 0; i < messages.length && seen < 2; i++) {
    var message = /** @type {{role?: unknown, content?: unknown}|null} */ (messages[i]);
    if (message === null || typeof message !== "object" || message.role !== "user") continue;
    seen += 1;
    var content = message.content;
    var text = "";
    if (typeof content === "string") text = content;
    else if (Array.isArray(content)) {
      for (var j = 0; j < content.length; j++) {
        var block = /** @type {{text?: unknown}|null} */ (content[j]);
        if (block !== null && typeof block === "object" && typeof block.text === "string") text += block.text + "\n";
      }
    }
    if (/(^|\n)# claudeMd\b/.test(text)) return true;
  }
  return seen === 0 ? null : false;
}

/**
 * The agent types that received CLAUDE.md and those that did not, from the
 * session's agents and what was seen of each run's context. A type is in
 * `skipped` only when every run of it lacked the block.
 * @param {{id: string, type: string}[]} agents
 * @param {Object<string, boolean>} received
 * @returns {{got: string[], skipped: string[]}}
 */
export function agentTypesOf(agents, received) {
  /** @type {Object<string, boolean>} */
  var got = {};
  var order = [];
  for (var i = 0; i < agents.length; i++) {
    var agent = /** @type {{id: string, type: string}} */ (agents[i]);
    if (agent.type === "" || !Object.prototype.hasOwnProperty.call(received, agent.id)) continue;
    if (!Object.prototype.hasOwnProperty.call(got, agent.type)) {
      got[agent.type] = false;
      order.push(agent.type);
    }
    if (received[agent.id] === true) got[agent.type] = true;
  }
  return {
    got: order.filter(function (type) {
      return got[type] === true;
    }),
    skipped: order.filter(function (type) {
      return got[type] !== true;
    }),
  };
}

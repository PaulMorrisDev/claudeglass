/* claudeglass mod: lib/instructions.js
 *
 * What the Startup page works out from the events that load things into
 * Claude's context: a ledger of items (a system prompt section, a CLAUDE.md
 * or rules file, a skill, a tool's description, an attachment), each with its
 * size in characters, kept in the order it loaded; then the rows the page
 * draws (tool descriptions rolled up into one row for the built-in tools, one
 * for the deferred tools and one per MCP server), the startup total, the three
 * groups the page shows it in (what you control, Claude Code's own, what was
 * added as you work), the one line of advice, and which agent runs received
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

/**
 * The attachment types that carry text of the person's or of a file rather
 * than a reminder from Claude Code, though the engine raises them itself: a
 * file the person names with an at sign, a file changed since Claude read it.
 */
export var ATTACHED_TYPES = ["file", "edited_text_file"];

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
 * @property {string} from      Where it came from: engine, managed, user, project, local, memory, imported, scoped, rules, plugin, mcp, skill, attachment (a reminder from Claude Code), attached (a file or a settings hook's note added to a message).
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
 * Whether an attachment is a file or a note added to the person's message,
 * not a reminder from Claude Code: a settings hook's or a plugin's text, or a
 * type that carries a file.
 * @param {string} type
 * @param {string|null|undefined} origin  `origin.kind` of the attachment: engine, hook or plugin; none counts as the engine.
 */
export function isAttached(type, origin) {
  return (typeof origin === "string" && origin !== "engine") || ATTACHED_TYPES.indexOf(type) >= 0;
}

/**
 * Adds an attachment's text to the item for its type and kind (one item holds
 * every reminder of a type, and one every attached file or note of it).
 * @param {Ledger} ledger
 * @param {string} type
 * @param {number} chars
 * @param {number} reply
 * @param {boolean} [attached]  A file or a note added to a message (see isAttached), not a reminder.
 */
export function addAttachment(ledger, type, chars, reply, attached) {
  var added = attached === true;
  var id = "att:" + type + (added ? ":attached" : "");
  var existing = ledger.items.find(function (one) {
    return one.id === id;
  });
  if (existing === undefined) {
    record(ledger, { id: id, kind: "attachment", name: type, chars: chars, reply: reply, from: added ? "attached" : "attachment", startup: !added && STARTUP_ATTACHMENTS.indexOf(type) >= 0 && reply <= 1, count: 1 });
    return;
  }
  existing.chars += chars;
  existing.count = (existing.count || 1) + 1;
}

/**
 * Drops what loaded after the start, as a summary of the conversation does:
 * the files Claude reached, the skills it ran and the reminders. What the
 * session starts with stays.
 * @param {Ledger} ledger
 * @returns {boolean} True when anything was dropped.
 */
export function compacted(ledger) {
  var kept = ledger.items.filter(function (one) {
    return one.startup;
  });
  var dropped = kept.length !== ledger.items.length;
  ledger.items = kept;
  return dropped;
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
 * Whether an MCP server (by the server part of its tools' names) is one the
 * Claude Desktop app runs itself (`ccd_session`, `ccd_directory`, ...).
 * @param {string} server
 */
export function isDesktopServer(server) {
  return server.indexOf("ccd_") === 0;
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
 * @property {string[]} [tools] An MCP server's tool names (`mcp__<server>__<tool>`), in load order.
 * @property {string} [path]
 * @property {string} [parent]
 * @property {string[]} [globs]
 * @property {{name: string, chars: number}[]} biggest  A rolled-up row's largest tools.
 */

/**
 * The rows the page draws, in load order. The tools of one kind roll up into one row, a plugin's tools into a row of their own.
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
    var group = item.kind === "tools" || item.kind === "deferred" ? item.kind + (item.from === "plugin" ? ":plugin" : "") : item.kind === "mcp" ? "mcp:" + (item.server || "") : "";
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
        if (item.kind === "mcp") {
          row.server = item.server || "";
          row.tools = [];
        }
        rolled[group] = row;
        out.push(row);
      }
      row.chars += item.chars;
      row.startupChars += live;
      row.count += 1;
      if (item.deferred !== true) row.deferred = false;
      if (row.tools !== undefined) row.tools.push(item.name);
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
 * The one thing worth fixing, if something is: an MCP server whose tool
 * descriptions sit in the prompt at `mcpTokens` or more, or a CLAUDE.md or
 * rules file at `fileTokens` or more; the larger of the two kinds' worst.
 * A server the Desktop app runs itself is never advised against.
 * @param {Entry[]} entries
 * @param {number} cpt
 * @param {{mcp?: number, file?: number}} [limits]
 * @param {Object<string, number>} [sizes]  Tokens by row key where Claude Code's count has sized the row (see fitToCategories).
 * @returns {{kind: "mcp"|"file", name: string, tokens: number}|null}
 */
export function adviceFor(entries, cpt, limits, sizes) {
  var fitted = sizes || {};
  var mcpAt = limits && typeof limits.mcp === "number" ? limits.mcp : MCP_TOKENS;
  var fileAt = limits && typeof limits.file === "number" ? limits.file : FILE_TOKENS;
  /** @type {{kind: "mcp"|"file", name: string, tokens: number}|null} */
  var best = null;
  for (var i = 0; i < entries.length; i++) {
    var one = /** @type {Entry} */ (entries[i]);
    /** @type {{kind: "mcp"|"file", name: string, tokens: number}|null} */
    var found = null;
    if (one.kind === "mcp") {
      // The Desktop app's own servers are not the person's to turn off.
      if (isDesktopServer(one.server || "")) continue;
      var live = typeof fitted[one.key] === "number" ? /** @type {number} */ (fitted[one.key]) : tokensOf(one.startupChars, cpt);
      if (live >= mcpAt) found = { kind: "mcp", name: one.server || "", tokens: live };
    } else if (one.kind === "instructions") {
      var size = typeof fitted[one.key] === "number" ? /** @type {number} */ (fitted[one.key]) : tokensOf(one.chars, cpt);
      if (size >= fileAt) found = { kind: "file", name: one.path !== undefined ? shortPath(one.path) : one.name, tokens: size };
    }
    if (found !== null && (best === null || found.tokens > best.tokens)) best = found;
  }
  return best;
}

/**
 * A row of Claude Code's context breakdown (`$.session.usage({ breakdown: "summary" }).context.breakdown.categories`).
 * @typedef {Object} Category
 * @property {string} name
 * @property {number} tokens
 * @property {string} [kind]       "used", "free", "buffer" or "deferred".
 * @property {boolean} [isDeferred]
 */

/**
 * Which row of /context an entry's tokens are part of: "system", "memory", "skills", "tools", "mcp" or "agents";
 * "" for a row that is not in the prompt at startup (a deferred tool) or that /context has no row for.
 * @param {Entry} entry
 * @returns {string}
 */
export function categoryOf(entry) {
  switch (entry.kind) {
    case "system":
    case "context":
      return "system";
    case "instructions":
      return "memory";
    case "skills":
      return "skills";
    case "tools":
      return "tools";
    case "mcp":
      return "mcp";
    case "attachment":
      return entry.name === "agent_listing_delta" ? "agents" : entry.name === "mcp_instructions_delta" ? "mcp" : "";
    default:
      return "";
  }
}

/** @type {Object<string, string>} */
var CATEGORY_NAMES = {
  "system prompt": "system",
  "memory files": "memory",
  skills: "skills",
  "system tools": "tools",
  "mcp tools": "mcp",
  "custom agents": "agents",
};

/**
 * The startup context as Claude Code counts it, split among the rows of the page. The total is the sum of the
 * breakdown's rows that sit in the window, less the conversation (the same figure the Now page calls startup),
 * and each category's tokens are shared out among the rows that belong to it by their size in characters, so a
 * row's figure is still an estimate but the groups add up to Claude Code's own count. A row whose category the
 * breakdown has no row for gets no size here (the caller keeps its estimate). Without a breakdown, or while it
 * counts no startup tokens (before the first reply), nothing is matched.
 * @param {Entry[]} entries
 * @param {Category[]|null|undefined} categories
 * @returns {{ matched: boolean, total: number, sizes: Object<string, number> }}
 */
export function fitToCategories(entries, categories) {
  /** @type {Object<string, number>} */
  var sizes = {};
  var list = Array.isArray(categories) ? categories : [];
  var total = 0;
  /** @type {Object<string, number>} */
  var bucket = {};
  for (var i = 0; i < list.length; i++) {
    var category = list[i];
    if (!category || typeof category.tokens !== "number" || !isFinite(category.tokens) || category.tokens <= 0) continue;
    var name = String(category.name).toLowerCase();
    if (category.kind !== "used" || category.isDeferred === true || name === "messages") continue;
    total += category.tokens;
    var key = CATEGORY_NAMES[name];
    if (key !== undefined) bucket[key] = (bucket[key] || 0) + category.tokens;
  }
  if (!(total > 0)) return { matched: false, total: 0, sizes: sizes };
  /** @type {Object<string, number>} */
  var chars = {};
  for (var j = 0; j < entries.length; j++) {
    var one = /** @type {Entry} */ (entries[j]);
    var group = categoryOf(one);
    if (group !== "" && one.startupChars > 0 && bucket[group] !== undefined) chars[group] = (chars[group] || 0) + one.startupChars;
  }
  var matched = false;
  for (var k = 0; k < entries.length; k++) {
    var row = /** @type {Entry} */ (entries[k]);
    var share = categoryOf(row);
    var all = chars[share];
    if (share === "" || row.startupChars <= 0 || all === undefined || all <= 0) continue;
    sizes[row.key] = Math.round(/** @type {number} */ (bucket[share]) * (row.startupChars / all));
    matched = true;
  }
  return { matched: matched, total: total, sizes: sizes };
}

/** A server name that is an identifier, not a name (`5f3a2c1e-9b4d-...`). */
var UUID_LIKE = /^[0-9a-f]{8}[-_][0-9a-f]{4}[-_]/i;

/** The name Claude Code gives a plugin's server: `plugin:<plugin>:<server>`. */
var PLUGIN_SERVER = /^plugin:[^:]+:(.+)$/;

/**
 * A wire name read as words: `_` and `-` as spaces.
 * @param {string} name
 * @param {string} fallback  Used when nothing is left.
 */
function asWords(name, fallback) {
  var words = name.replace(/[_-]+/g, " ").trim();
  return words === "" ? fallback : words;
}

/**
 * What an MCP server is called on the page: the name /mcp lists when Claude
 * Code gave one (a plugin's `plugin:<plugin>:<server>` by its server part, as
 * words), else its wire name read as words (`plugin_<plugin>_` taken off, `_`
 * and `-` as spaces); a server that only has an id is "Connector" with its
 * first three tools, as words.
 * @param {string} wire  The server part of its tools' names.
 * @param {Object<string, string>|null|undefined} names  /mcp's name for each wire name.
 * @param {string[]|null|undefined} toolNames  The server's tools, as `mcp__<server>__<tool>`.
 * @returns {string}
 */
export function serverLabel(wire, names, toolNames) {
  var listed = names && typeof names[wire] === "string" ? names[wire] : "";
  var plugged = PLUGIN_SERVER.exec(listed);
  if (plugged !== null) return asWords(/** @type {string} */ (plugged[1]), listed);
  if (listed !== "" && !UUID_LIKE.test(listed)) return listed;
  if (UUID_LIKE.test(wire) || listed !== "") {
    var tools = Array.isArray(toolNames) ? toolNames : [];
    var prefix = "mcp__" + wire + "__";
    var short = tools.slice(0, 3).map(function (tool) {
      return (tool.indexOf(prefix) === 0 ? tool.slice(prefix.length) : tool.replace(/^mcp__.+?__/, "")).replace(/_/g, " ");
    });
    if (short.length === 0) return "Connector";
    return "Connector (" + short.join(", ") + (tools.length > 3 ? ", \u2026" : "") + ")";
  }
  var rest = wire;
  if (rest.indexOf("plugin_") === 0) {
    var parts = rest.slice(7).split("_");
    // The plugin's name comes first; a name with nothing after it is kept whole.
    rest = parts.length > 1 ? parts.slice(1).join("_") : parts.join("_");
  }
  return asWords(rest, wire);
}

/**
 * One line under a row of the Startup page: a file or a server.
 * @typedef {Object} StartupItem
 * @property {string} label
 * @property {number} tokens
 * @property {boolean} deferred  Waits behind tool search, so costs nothing at startup.
 * @property {boolean} [instructions]  Not a server: the instructions the servers gave Claude, which the label does not name (the page does).
 */

/**
 * One row of a group on the Startup page.
 * @typedef {Object} StartupRow
 * @property {string} id      "mcp", "files", "skills" or "plugin" (you control); "files-later", "skills-used", "reminders" or "attached" (added as you work).
 * @property {number} tokens
 * @property {number} count   What the row stands for: servers (every one listed, those behind tool search too), files, skills, tools.
 * @property {StartupItem[]} items  Largest first for servers; in load order otherwise.
 */

/**
 * @typedef {Object} StartupGroups
 * @property {{tokens: number, rows: StartupRow[]}} control  What the person can change.
 * @property {{tokens: number}} builtin  Claude Code's own: the rest of the startup context.
 * @property {{tokens: number, rows: StartupRow[]}} later    What is not in the startup context.
 */

/**
 * The page's three groups. "You control" is the MCP servers (all but the
 * Desktop app's own, with the instructions they gave Claude at the start), the
 * CLAUDE.md and memory files, the skill list and the plugins' tools; "Claude
 * Code's own" is the rest of the startup context, one total; "Added as you
 * work" is what loaded after the start (files Claude reached, skills it ran,
 * Claude Code's reminders, files and notes added to your messages). A tool that
 * waits behind tool search costs nothing: its server is counted and listed
 * without a size, and no other row has it. Rows of no tokens are left out. `total` is
 * the startup total the page shows: given, the built-in part is what is left
 * of it once "You control" is taken out.
 * @param {Entry[]} entries
 * @param {(entry: Entry) => number} sizeOf  Tokens for a row.
 * @param {Object<string, string>|null|undefined} names  /mcp's name for each server (see serverLabel).
 * @param {number} [total]
 * @returns {StartupGroups}
 */
export function startupGroups(entries, sizeOf, names, total) {
  var ids = ["mcp", "files", "skills", "plugin", "files-later", "skills-used", "reminders", "attached"];
  /** @type {Object<string, StartupRow>} */
  var rows = {};
  for (var r = 0; r < ids.length; r++) {
    var made = /** @type {string} */ (ids[r]);
    rows[made] = { id: made, tokens: 0, count: 0, items: [] };
  }
  var builtin = 0;
  for (var i = 0; i < entries.length; i++) {
    var one = /** @type {Entry} */ (entries[i]);
    var size = sizeOf(one);
    var label = one.path !== undefined ? shortPath(one.path) : one.name;
    if (one.startupChars <= 0) {
      if (one.deferred) {
        // Behind tool search: a server is listed under its row without a size, so the detailed list is whole.
        if (one.kind === "mcp" && !isDesktopServer(one.server || "")) {
          var waiting = /** @type {StartupRow} */ (rows.mcp);
          waiting.count += 1;
          waiting.items.push({ label: serverLabel(one.server || "", names, one.tools), tokens: 0, deferred: true });
        }
        continue;
      }
      var later = one.kind === "instructions" ? "files-later" : one.kind === "attachment" ? (one.from === "attached" ? "attached" : "reminders") : one.name === "skill listing" ? "reminders" : one.kind === "skills" ? "skills-used" : "";
      if (later === "") continue;
      var after = /** @type {StartupRow} */ (rows[later]);
      after.tokens += size;
      after.count += 1;
      after.items.push({ label: label, tokens: size, deferred: false });
      continue;
    }
    var id = "";
    // What the servers told Claude at the start is part of the MCP servers' share, not Claude Code's own.
    var told = one.kind === "attachment" && one.name === "mcp_instructions_delta";
    if (told || (one.kind === "mcp" && !isDesktopServer(one.server || ""))) id = "mcp";
    else if (one.kind === "instructions") id = "files";
    else if (one.kind === "skills") id = "skills";
    else if ((one.kind === "tools" || one.kind === "deferred") && one.from === "plugin") id = "plugin";
    if (id === "") {
      builtin += size;
      continue;
    }
    var row = /** @type {StartupRow} */ (rows[id]);
    row.tokens += size;
    row.count += told ? 0 : id === "plugin" ? one.count : 1;
    if (told) row.items.push({ label: label, tokens: size, deferred: false, instructions: true });
    else row.items.push({ label: id === "mcp" ? serverLabel(one.server || "", names, one.tools) : label, tokens: size, deferred: false });
  }
  /** @type {StartupRow} */ (rows.mcp).items.sort(function (a, b) {
    return b.tokens - a.tokens;
  });
  /** @param {string[]} list */
  var present = function (list) {
    return list
      .map(function (id) {
        return /** @type {StartupRow} */ (rows[id]);
      })
      .filter(function (one) {
        return one.tokens > 0;
      });
  };
  /** @param {StartupRow[]} list */
  var sum = function (list) {
    var all = 0;
    for (var k = 0; k < list.length; k++) all += /** @type {StartupRow} */ (list[k]).tokens;
    return all;
  };
  var control = present(["mcp", "files", "skills", "plugin"]);
  var added = present(["files-later", "skills-used", "reminders", "attached"]);
  var yours = sum(control);
  return {
    control: { tokens: yours, rows: control },
    builtin: { tokens: typeof total === "number" && total >= yours ? total - yours : builtin },
    later: { tokens: sum(added), rows: added },
  };
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

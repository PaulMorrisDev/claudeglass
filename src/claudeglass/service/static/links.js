/* claudeglass service UI: links.js
 *
 * Where things live: the sidebar's pages and their segments, which page
 * shows each report section and table, the #/page/segment routes, and
 * links between pages.
 */

import { el, goTo, state, withCli } from "./core.js";

// ======================================================================
// Pages and segments
// ======================================================================

// The sidebar, in order. A page with segments shows them as a segmented
// control under its title; a page without shows its own intro. The
// first pages answer the questions people come with (am I doing
// something wrong? did my changes work?); `group: "details"` pages, the
// evidence behind those answers, sit under a Details heading. `foot`
// pages sit at the bottom of the sidebar. `window: false` marks a page
// whose figures don't depend on the window, so the header says so
// instead of offering the picker. Ids are lower-case words joined by
// hyphens: they appear in the address bar (#/spend/usage).
export var PAGES = [
  {
    id: "overview",
    label: "Overview",
    icon: "overview",
    intro: "Anything wrong, did your changes work, and where your tokens went in this window.",
  },
  {
    id: "changes",
    label: "Your changes",
    icon: "changes",
    intro: "Did the changes you made work, and by how much? Cost per reply around each change, then each change's sessions before against after.",
  },
  {
    id: "actions",
    label: "Actions",
    icon: "actions",
    segments: [
      {
        id: "recommendations",
        label: "Recommendations",
        intro: "Changes worth making, most important first. Each one comes with a prompt or command you can copy.",
      },
      {
        id: "checks",
        label: "Checks",
        intro:
          "One question per way of saving tokens, answered from your own sessions, with the evidence and a fix you can copy. Nothing here changes Claude Code by itself.",
      },
    ],
  },
  {
    id: "spend",
    label: "Spend",
    icon: "spend",
    group: "details",
    segments: [
      { id: "usage", label: "Usage", intro: "Usage over time, by model and project, and in five-hour blocks." },
      {
        id: "savings",
        label: "Savings",
        intro:
          "What you could save: shorter tool output, earlier conversation summaries, cheaper models, and replies that did no useful work.",
      },
      { id: "sessions", label: "Sessions", intro: "Every session, newest first. Pick one to see its replies on a timeline." },
    ],
  },
  {
    id: "cache",
    label: "Cache",
    icon: "cache",
    group: "details",
    segments: [
      {
        id: "rebuilds",
        label: "Rebuilds",
        intro:
          "When Claude Code had to rebuild the prompt cache, and why. A rebuild writes the whole conversation to the cache again, at the cache-write price.",
      },
      {
        id: "lifetime",
        label: "Lifetime (TTL)",
        intro: "How long the prompt cache stays warm, and whether a longer cache lifetime would have paid for itself.",
      },
    ],
  },
  {
    id: "agents",
    label: "Agents & context",
    icon: "agents",
    group: "details",
    segments: [
      { id: "subagents", label: "Subagents", intro: "What your subagents cost, what each one is given when it starts, and whether long runs should be split." },
      { id: "quality", label: "Quality", intro: "How subagent work went: runs that were retried, workflows, and how you split the work." },
      {
        id: "context",
        label: "Context",
        intro:
          "What Claude reads at the start of every session and subagent: your CLAUDE.md files and the skills list. How often each is sent, what it costs, and how to trim it. Also what tool search saves by keeping MCP tool definitions out.",
      },
      { id: "hooks", label: "Hooks", intro: "Whether each hook you set up works, and what it costs in kept context, blocked calls and waiting." },
    ],
  },
  {
    id: "habits",
    label: "Work habits",
    icon: "habits",
    group: "details",
    intro:
      "How the way you work shapes what it costs. The habits that would have saved the most in your own sessions, each with an example to copy.",
  },
  {
    id: "setup",
    label: "Setup",
    icon: "setup",
    group: "details",
    segments: [
      {
        id: "settings",
        label: "Settings",
        intro: "Your Claude Code settings, how they changed, and how this window compares with your baseline.",
      },
      {
        id: "profiles",
        label: "Profiles",
        intro:
          "Groups of settings. Make one from a goal with an estimate of its effect, compare it with yours, and see what each change you made did.",
      },
      {
        id: "capture",
        label: "Capture",
        window: false,
        intro:
          "Metrics capture: short tags that tell ClaudeGlass what each piece of work was and how it went, so suggestions fit how you work. Choose how much, and see what it costs.",
      },
    ],
  },
  {
    id: "data",
    label: "Data quality",
    icon: "data",
    foot: true,
    intro: "What ClaudeGlass installed and what to expect, how much of your data it read, and whether its cost matches Claude Code's own.",
  },
  {
    id: "glossary",
    label: "Glossary",
    icon: "glossary",
    foot: true,
    segments: [
      { id: "terms", label: "Terms", window: false, intro: "The words this dashboard uses, in plain English." },
      {
        id: "how-costs-work",
        label: "How costs work",
        intro: "How each kind of token is priced, with the multipliers from your pricing and your own numbers, and what each change saves you.",
      },
    ],
  },
];

export function findPage(pageId) {
  for (var i = 0; i < PAGES.length; i++) {
    if (PAGES[i].id === pageId) return PAGES[i];
  }
  return null;
}

export function findSegment(page, segmentId) {
  var segments = (page && page.segments) || [];
  for (var i = 0; i < segments.length; i++) {
    if (segments[i].id === segmentId) return segments[i];
  }
  return null;
}

// Every view a route can show, as "page" or "page/segment", in sidebar
// order: what app.js mounts a renderer for.
export var VIEW_KEYS = PAGES.reduce(function (keys, page) {
  if (!page.segments) return keys.concat([page.id]);
  return keys.concat(
    page.segments.map(function (segment) {
      return page.id + "/" + segment.id;
    })
  );
}, []);

// The page and segment a view key names, or null for an unknown key.
export function viewFor(key) {
  var parts = String(key || "").split("/");
  var page = findPage(parts[0]);
  if (!page) return null;
  if (!page.segments) return parts.length === 1 ? { key: page.id, page: page, segment: null } : null;
  var segment = findSegment(page, parts[1]);
  return segment ? { key: page.id + "/" + segment.id, page: page, segment: segment } : null;
}

// ======================================================================
// Routes: #/<page>[/<segment>][?w=<window>&project=<slug>&...]
// ======================================================================

// A route from location.hash. A page with segments and none named (or
// an unknown one) gets segment null, for the router to fill in with the
// page's last-used or first segment. Anything else is not a route.
export function parseHash(hash) {
  var match = /^#\/([a-z]+(?:-[a-z]+)*)(?:\/([a-z]+(?:-[a-z]+)*))?(?:\?(.*))?$/.exec(hash || "");
  if (!match) return null;
  var page = findPage(match[1]);
  if (!page) return null;
  var segment = page.segments ? findSegment(page, match[2]) : null;
  var params = {};
  new URLSearchParams(match[3] || "").forEach(function (value, name) {
    params[name] = value;
  });
  return { page: page, segment: segment, params: params };
}

// The hash for a view key and its parameters; the window (w) goes
// first, then the project, so every address starts with what it shows.
// An empty value (all projects) is left out.
var LEADING_PARAMS = { w: 0, project: 1 };

function paramRank(name) {
  return name in LEADING_PARAMS ? LEADING_PARAMS[name] : 2;
}

export function formatHash(key, params) {
  var query = new URLSearchParams();
  var names = Object.keys(params || {}).sort(function (a, b) {
    return paramRank(a) - paramRank(b) || (a < b ? -1 : a > b ? 1 : 0);
  });
  names.forEach(function (name) {
    var value = params[name];
    if (value !== null && value !== undefined && value !== "") query.set(name, value);
  });
  var text = query.toString();
  return "#/" + key + (text ? "?" + text : "");
}

// The old tab keys (tls:activeTab, before the sidebar), for the one-time
// move to a route.
export var OLD_TAB_VIEWS = {
  overview: "overview",
  quick: "actions/checks",
  recommendations: "actions/recommendations",
  sessions: "spend/sessions",
  savings: "spend/savings",
  usage: "spend/usage",
  cache: "cache/rebuilds",
  ttl: "cache/lifetime",
  agents: "agents/subagents",
  context: "agents/context",
  habits: "habits",
  config: "setup/settings",
  profiles: "setup/profiles",
  capture: "setup/capture",
  diagnostics: "data",
  glossary: "glossary/terms",
};

// ======================================================================
// Which view shows each report section and table
// ======================================================================

// Section key -> view. Anything not listed lands on Data quality, so a
// new section is never silently dropped when report.py grows one.
export var SECTION_PAGE_MAP = {
  // The Overview draws these itself: the scorecard as tiles, the
  // overview section's totals under Details (its other tables are placed
  // by TABLE_PAGE_MAP).
  scorecard: "overview",
  overview: "overview",
  // Spend.
  usage: "spend/usage",
  // Subscription only: how many tokens a usage limit holds.
  elasticity: "spend/usage",
  compactions: "spend/usage",
  phases: "spend/usage",
  // Savings reads each of these from its own report-backed route
  // (/api/carry and the rest); mapped so they never fall through to
  // Data quality when the whole report is walked.
  carry: "spend/savings",
  compaction_sim: "spend/savings",
  plan_handoff: "spend/savings",
  model_swap: "spend/savings",
  waste: "spend/savings",
  sessions: "spend/sessions",
  // Cache. recache_by_group arrives today as a table inside recache;
  // mapped too, so a report that promotes it to its own section still
  // lands here. Usage-limit pauses force the same full re-write the
  // rebuild sections count, so they sit beside them.
  recache: "cache/rebuilds",
  recache_by_group: "cache/rebuilds",
  limits: "cache/rebuilds",
  // Lifetime reads /api/ttl; mapped for the same reason as Savings.
  ttl: "cache/lifetime",
  // Agents & context.
  agent_startup: "agents/subagents",
  agents: "agents/subagents",
  run_split: "agents/subagents",
  quality: "agents/quality",
  workflows: "agents/quality",
  workstyle: "agents/quality",
  context_budget: "agents/context",
  tool_search: "agents/context",
  hooks: "agents/hooks",
  habits: "habits",
  // Setup. Settings draws the config section's tables once, from
  // /api/config-diff?auto_keys=1, and skips the section itself.
  config: "setup/settings",
  baseline_comparison: "setup/settings",
  // The capture section's one table is report-only; the Capture segment
  // shows its own figures from /api/capture.
  capture: "setup/capture",
  // Data quality: ClaudeGlass's cost against Claude Code's own record.
  cost_record: "data",
};

// "section.table" -> view, for a table that lives somewhere other than
// its section's view. Checked before SECTION_PAGE_MAP.
export var TABLE_PAGE_MAP = {
  // The Overview's totals stay there; cost by model is spend detail.
  "overview.totals": "overview",
  "overview.by_model": "spend/usage",
  // Which setup worked best for each kind of task feeds profiles.
  "habits.habits_setups": "setup/profiles",
};

export function viewForSection(sectionKey) {
  return SECTION_PAGE_MAP[sectionKey] || "data";
}

export function viewForTable(sectionKey, tableName) {
  return TABLE_PAGE_MAP[sectionKey + "." + tableName] || viewForSection(sectionKey);
}

// ======================================================================
// Page intros and links
// ======================================================================

// The one line under the page header saying what the view answers. The
// view's title is the page header's h1, so a view adds no heading of
// its own.
export function viewIntro(container, key) {
  var view = viewFor(key);
  var text = view ? (view.segment ? view.segment.intro : view.page.intro) : "";
  if (text) container.appendChild(el("p", { class: "view-intro", text: text }));
}

// What every address carries: the window, and the project when one is
// picked (formatHash leaves an empty one out). Links, the router and the
// pages all take it from here, so none drops the project.
export function scopeParams() {
  return { w: state.window, project: state.project };
}

// A link to another view: a real #/ address (so it opens in a new tab
// and shows in the status bar), which moves focus to the new page's
// title when followed here. Without text it reads as the view's name.
// params: what to open there ({id: a recommendation's key}).
export function pageLink(key, text, params) {
  var link = el("a", { class: "page-link", href: formatHash(key, Object.assign(scopeParams(), params || {})), text: text || viewLabel(key) });
  link.addEventListener("click", function (event) {
    if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    goTo(key, { focus: !params, params: params || null });
  });
  return link;
}

// Say in the address what the view on screen has open (the selected
// recommendation), without a new history entry: Back leaves the view,
// not each item looked at.
export function replaceParams(params) {
  state.params = Object.assign({}, params || {});
  var clean = {};
  Object.keys(state.params).forEach(function (name) {
    if (state.params[name] !== null && state.params[name] !== undefined) clean[name] = state.params[name];
  });
  state.params = clean;
  window.history.replaceState(null, "", formatHash(state.view, Object.assign(scopeParams(), clean)));
}

export function captureLink(text) {
  return pageLink("setup/capture", text);
}

// The page and segment a view key names, as the reader sees it:
// "Spend › Usage" (the same in the README and docs).
export function viewLabel(key) {
  var view = viewFor(key);
  if (!view) return key;
  return view.segment ? view.page.label + " \u203a " + view.segment.label : view.page.label;
}

// ======================================================================
// Glossary terms and cost concepts
// ======================================================================

// The words the dashboard uses, with the README glossary's wording
// (tests/test_service_static.py keeps the two the same).
export var GLOSSARY = [
  ["Session", "One conversation with Claude Code, from start to exit. Resuming it continues the same session."],
  ["Main session", "The conversation you type into, as opposed to the subagents it starts."],
  ["Subagent", "A separate Claude that your session starts for one task, such as a search or a review. It has its own context and reports back when done."],
  ["Transcript", "The log file Claude Code writes for a session or a subagent run. Everything here is read from these files on your machine."],
  ["Reply", "One response from Claude, including any tool calls it makes. Every reply is billed for the whole context it reads."],
  ["Token", "The unit models read and write, roughly three quarters of a word. Prices are per million tokens."],
  ["Context", "Everything Claude reads on a reply: system prompt, tools, CLAUDE.md files and the conversation so far."],
  ["Startup context", "What Claude reads before your first message, or before a subagent's task: system prompt, tool list, CLAUDE.md files, skills and more."],
  ["Prompt cache", "A copy of the start of the context kept on Anthropic's side, so the next reply can re-read it cheaply instead of paying full price."],
  ["Cache read", "Re-reading context from the prompt cache, for a small part of the normal input price: a tenth or a twentieth, depending on the model."],
  ["Cache write", "Putting context into the prompt cache. Costs more than normal input: 1.25 times for a 5-minute lifetime, 2 times for 1 hour."],
  ["Cache rebuild", "Writing context to the cache again because the cached copy expired or something early in the conversation changed."],
  ["Cache lifetime (TTL)", "How long the prompt cache stays warm after a reply: 5 minutes or 1 hour. On a Pro or Max plan within its usage limits, the main session gets 1 hour by default. Otherwise, and for subagents, the default is 5 minutes. A pause longer than this means a rebuild."],
  ["Conversation summary", "When the context gets too large, Claude Code replaces the conversation so far with a summary. Also called compaction."],
  ["List price", "Anthropic's published price per token. On a Pro or Max plan you don't pay this; it is shown to compare costs."],
  ["Usage limits", "On a Pro or Max plan, the share of your five-hour and weekly allowance you have used."],
  ["Billing mode", "How amounts are shown. On a Pro or Max plan, as a share of your usage limits when there are enough readings, otherwise as a list-price equivalent. On pay-per-token billing, as money."],
  ["Effort level", "How hard Claude thinks before replying. Thinking is billed as output, the most expensive token type."],
  ["Scorecard", "Five areas rated 1 (very poor) to 5 (excellent), each from one number in your data."],
  ["Recommendation", "A change worth making, with what it changes, the trade-off, a prompt you can give Claude and a command you can run."],
  ["Profile", "A named group of settings you can compare with yours, try for one session, or apply."],
  ["Scope", "Where a change is written: your user settings (every project), this project on your machine only, or this project for everyone."],
  ["Managed setting", "A setting your organisation's policy controls. Only your administrator can change it."],
  ["Snapshot", "A record of your Claude Code settings at one moment, taken so changes can be compared over time."],
  ["Window", "The stretch of time the numbers cover, picked at the top of the dashboard. It can be the last hour, today, the last 24 hours, 7, 30 or 90 days, all time, or since your last change. A session counts, in full, when it was last active in the window; since your last change, when it started after the change."],
  ["Change point", "A moment your settings changed: an apply, its undo, or a change the settings snapshot saw. The dashboard compares the sessions before it with those after it."],
  ["Quick action", "One question about a way to spend less, answered from your own sessions with the evidence and a fix you can copy. The dashboard lists them on the Actions page, under Checks."],
  ["What-if estimate", "What a change would have saved over the window, worked out from your own sessions. It is an estimate: cheaper settings can change how Claude works, which the estimate can't see."],
  ["CLAUDE.md", "Instruction files Claude reads at the start of every session, and of most subagents: yours, each project's, and rule files. Every line is paid for on every reply that re-reads it."],
  ["Skill", "A packaged set of instructions Claude can load when a task needs it. Its name and description are listed to Claude at the start of every session, used or not."],
  ["Quality signal", "A sign of whether the work went well, not only what it cost: tool calls that failed, agent runs that didn't finish, your corrections. Compared across models and efforts, and before and after each change you make."],
  ["Metrics capture", "An opt-in feature, off by default: Claude adds a one-line tag saying what a piece of work was and how it went. It costs tokens while it's on. init's last questions and claudeglass capture turn it on, change what it asks for, or turn it off."],
  ["Capture level", "How much metrics capture asks for: off, free, essentials, standard or deep, each adding more of it. Set at init or with claudeglass capture level."],
  ["Tag", "The one-line, closed-vocabulary note metrics capture has Claude add to a reply, such as [tl: task=bugfix brief=clear] or [result: done fit=right]. Only words from a fixed list are kept; nothing Claude writes in its own words is."],
  ["Prompt cycle", "One message of yours and everything Claude did to answer it, subagents at any depth included. The unit metrics capture and the Work habits page measure by."],
  ["Work habits", "The page (and report section) that turns prompt cycles into habits worth trying, with a rough saving for each. Each shows where its evidence came from: reported by Claude, inferred from the transcript, or your own feedback."],
  ["Feedback skill", "/tl-feedback, a skill you can add and run after a piece of work. It asks whether the work delivered, what slowed it, whether it was worth the tokens, and what would have helped. Works at any capture level, even off; picking deep turns it on, with its reminders."],
  ["Brief templates", "Checklists per kind of task on the Work habits page, built from what your own requests tend to lack. Turned on, it also adds a /tl-brief skill that checks a request against its checklist and asks once for anything missing before Claude starts."],
  ["Sampling", "Running metrics capture in only a share of sessions (100, 50, 25 or 10 percent, [capture] sample) to spend fewer tokens on it. Picked at random, per session."],
  ["Time-box", "The date metrics capture switches itself back off. By default it's 14 days after you turn a level on, whether at init, with capture on or level, or on the Capture page. So turning it on never means it runs unattended forever. --for or --capture-for sets another length, and --no-limit or --capture-no-limit turns the limit off. You can also say so when asked."],
];
// Two entries name a command; they read in this install's form.
GLOSSARY.forEach(function (pair) {
  pair[1] = withCli(pair[1]);
});

// A glossary term's anchor: #/glossary/terms?term=<slug>.
export function termSlug(term) {
  return String(term || "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

// The cost concepts Glossary > How costs work explains, one card each,
// in order: #/glossary/how-costs-work?card=<slug>. `terms` are the
// glossary entries each card explains; their entries say why they
// matter in the card's own words and link to it.
export var COST_CARDS = [
  { slug: "cache-reads", title: "Cache reads", terms: ["Cache read", "Prompt cache"] },
  { slug: "cache-writes", title: "Cache writes and lifetime (TTL)", terms: ["Cache write", "Cache lifetime (TTL)"] },
  { slug: "cache-rebuilds", title: "Cache rebuilds", terms: ["Cache rebuild"] },
  { slug: "model-choice", title: "Model choice", terms: [] },
  { slug: "startup-context", title: "Startup context", terms: ["Startup context", "CLAUDE.md"] },
  { slug: "tool-output", title: "Tool output kept", terms: [] },
  { slug: "conversation-summaries", title: "Conversation summaries", terms: ["Conversation summary"] },
  { slug: "billing-mode", title: "Billing mode", terms: ["Billing mode", "List price", "Usage limits"] },
];

// A link to a glossary term or a cost card.
export function termLink(term, text) {
  return pageLink("glossary/terms", text || term, { term: termSlug(term) });
}

export function cardLink(slug, text) {
  return pageLink("glossary/how-costs-work", text, { card: slug });
}

// The glossary terms worth explaining where they appear: not everyday
// words ("session", "token") but the ones this dashboard coined or
// borrowed. Each is [GLOSSARY term, the words that name it], longest
// first, so "cache lifetime" wins over a shorter term at the same place.
export var JARGON = [
  ["Cache lifetime (TTL)", "cache lifetimes?|TTL"],
  ["Conversation summary", "conversation summar(?:y|ies)|compactions?"],
  ["Startup context", "startup context"],
  ["Cache rebuild", "cache rebuilds?"],
  ["Prompt cache", "prompt cache"],
  ["Cache write", "cache writes?"],
  ["Cache read", "cache reads?"],
  ["What-if estimate", "what-if estimates?"],
  ["Quality signal", "quality signals?"],
  ["Metrics capture", "metrics capture"],
  ["Capture level", "capture levels?"],
  ["Managed setting", "managed settings?"],
  ["Prompt cycle", "prompt cycles?"],
  ["Change point", "change points?"],
  ["Effort level", "effort levels?"],
  ["Billing mode", "billing mode"],
  ["Usage limits", "usage limits?"],
  ["List price", "list[- ]price"],
  ["Subagent", "subagents?"],
];

// The definition GLOSSARY gives a term.
export function glossaryText(term) {
  for (var i = 0; i < GLOSSARY.length; i++) {
    if (GLOSSARY[i][0] === term) return GLOSSARY[i][1];
  }
  return "";
}

// ======================================================================
// Page tokens in server text
// ======================================================================

// Server text names another page with a token, {{page:<page>}} or
// {{page:<page>/<segment>}} (pages.py, docs/writing-help.md). linkText
// turns each into a link to that view, named as the sidebar names it;
// plainText gives the name alone, for text that can't hold a link (a
// tooltip, a toast). An unknown token is left as it is: tests keep
// every token the server writes resolvable.
var PAGE_TOKEN = /\{\{page:([a-z]+(?:-[a-z]+)*)(?:\/([a-z]+(?:-[a-z]+)*))?\}\}/g;

function tokenTarget(pageId, segmentId) {
  var page = findPage(pageId);
  if (!page) return null;
  if (!segmentId) return { key: page.segments ? page.id + "/" + page.segments[0].id : page.id, label: page.label };
  var view = viewFor(pageId + "/" + segmentId);
  return view ? { key: view.key, label: viewLabel(view.key) } : null;
}

export function linkText(text) {
  var source = text === null || text === undefined ? "" : String(text);
  var nodes = [];
  var last = 0;
  var match;
  PAGE_TOKEN.lastIndex = 0;
  while ((match = PAGE_TOKEN.exec(source))) {
    if (match.index > last) nodes.push(document.createTextNode(source.slice(last, match.index)));
    var target = tokenTarget(match[1], match[2]);
    nodes.push(target ? pageLink(target.key, target.label) : document.createTextNode(match[0]));
    last = PAGE_TOKEN.lastIndex;
  }
  if (last < source.length) nodes.push(document.createTextNode(source.slice(last)));
  return nodes;
}

export function plainText(text) {
  var source = text === null || text === undefined ? "" : String(text);
  return source.replace(PAGE_TOKEN, function (whole, pageId, segmentId) {
    var target = tokenTarget(pageId, segmentId);
    return target ? target.label : whole;
  });
}

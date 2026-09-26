/* claudeglass service UI: page-overview.js
 *
 * The Overview answers the three questions people open the dashboard
 * with, in order: Anything wrong? (every check as one list, with what
 * fixing it saves), Did your changes work? (the latest changes, before
 * against after), and Where do your tokens go? (three headline numbers,
 * daily spend, and spend by project and by model). A sentence above
 * says how the window went; the scores and totals are folded below.
 */

import { clear, el, goTo, state, WINDOW_OPTIONS } from "./core.js";
import { fraction, money, moneyParts, projectName, shortTs, thousands } from "./format.js";
import { fetchJson, findSection, loadQuickActions, loadRecommendations, loadReport, prefetchActions, withProject, withWindow } from "./api.js";
import {
  button,
  copyToClipboard,
  countUp,
  deltaChip,
  emptyState,
  enterInTurn,
  errorNotice,
  loadingNode,
  tile,
  tileRow,
  timesText,
  toast,
} from "./ui.js";
import { dataGrid, headRow, renderTable } from "./grid.js";
import { icon } from "./icons.js";
import { pageLink, viewIntro } from "./links.js";
import { renderSetupCard } from "./shell.js";
import { chartError, holdChart } from "./charts.js";
import { dailyChanges, renderChart, savingsLevers, sparkline, tableObjects, windowDays } from "./charts-types.js";
import { groupRecommendations, groupSavingUsd, groupTitle, listSaving } from "./page-actions.js";
import { changesLink, renderChangeCards } from "./page-changes.js";

// A page draw that a newer one (a new window) has replaced: its late
// answers are dropped, so they can't take the chart back.
var overviewRun = 0;

// The entrance (docs/ui.md, "Motion"): the headline figures count up,
// the chart draws in 80ms after they start, and the checklist's rows
// arrive in turn. Under reduced motion nothing moves.
var CHART_AFTER_MS = 80;
var ROWS_AFTER_MS = 160;

// How many changes "Did your changes work?" shows; Your changes has all.
var CHANGES_SHOWN = 2;

// The headline figures last drawn, by tile: a new window counts up from
// them, the first draw from 0.
var shownFigures = null;

// -- the period before -----------------------------------------------------------

var DAY_MS = 24 * 3600 * 1000;

// The "Since my last change" window's counterfactual (counterfactual.py):
// the newest change /api/impact judges is the one the window starts at.
// Nothing when too few sessions have started since it to say.
function lastChangeLine(impactBody) {
  var first = impactBody && impactBody.ok === true && impactBody.data && (impactBody.data.changes || [])[0];
  var without = first && first.without;
  if (!without || !without.since_text) return null;
  var change = first.change || {};
  var when = [shortTs(change.ts)];
  if (change.project) when.push("in " + (change.project_name ? projectName(change.project_name) : "one project") + " only");
  return el("p", { class: "notes last-change-without" }, [
    el("span", { text: "Without your last change (" }),
    pageLink("changes", change.label || "a settings change", { day: String(change.ts || "").slice(0, 10) }),
    el("span", { text: ", " + when.join(", ") + "), " + without.since_text }),
  ]);
}

// The period of the same length just before this window, for the
// deltas: its bounds, and how the sentence names it. None for "all" and
// "since my last change", which have nothing the same length before them.
function previousPeriod(windowValue, now) {
  var end;
  var length;
  if (/^[0-9]+$/.test(windowValue)) {
    var days = Number(windowValue);
    length = days * DAY_MS;
    end = now - length;
    return { since: end - length, until: end, phrase: days === 1 ? "the day before" : "the " + days + " days before" };
  }
  if (windowValue === "1h") return { since: now - 2 * 3600 * 1000, until: now - 3600 * 1000, phrase: "the hour before" };
  if (windowValue === "24h") return { since: now - 2 * DAY_MS, until: now - DAY_MS, phrase: "the 24 hours before" };
  if (windowValue === "today") {
    // The same hours yesterday: local midnight to this time, a day back.
    var midnight = new Date(now);
    midnight.setHours(0, 0, 0, 0);
    return { since: midnight.getTime() - DAY_MS, until: now - DAY_MS, phrase: "the same hours yesterday" };
  }
  return null;
}

function isoMinute(ms) {
  return new Date(ms).toISOString().slice(0, 16) + ":00Z";
}

// -- the summary sentence ----------------------------------------------------------

function windowLabel(value) {
  for (var i = 0; i < WINDOW_OPTIONS.length; i++) {
    if (WINDOW_OPTIONS[i].value === value) return WINDOW_OPTIONS[i].label;
  }
  return /^[0-9]+$/.test(value) ? "Last " + value + " days" : "This window";
}

// "No sessions <when>."
function windowWhen(value) {
  if (/^[0-9]+$/.test(value)) return value === "1" ? "in the last day" : "in the last " + value + " days";
  return { "1h": "in the last hour", today: "today", "24h": "in the last 24 hours", change: "since your last change" }[value] || "yet";
}

// What the window cost, as a clause: dollars on the API, a share of the
// weekly limit on a Pro or Max plan.
function spendClause(usd) {
  var amount = money(usd);
  if (!amount) return "nothing was spent";
  var units = state.units || {};
  if (units.mode !== "subscription") return "you spent " + amount.primary;
  if (units.share_per_usd === null || units.share_per_usd === undefined) return "your tokens came to " + amount.primary;
  return "you used " + amount.primary;
}

function changeClause(current, previous, phrase) {
  if (!phrase || typeof previous !== "number" || previous < 0 || typeof current !== "number") return "";
  if (previous === 0) return current > 0 ? ", with nothing in " + phrase : "";
  if (current / previous >= 3) return ", " + timesText(current / previous) + " as much as " + phrase;
  var change = ((current - previous) / previous) * 100;
  if (Math.abs(change) < 1) return ", about the same as " + phrase;
  var rounded = Math.abs(change) >= 10 ? Math.round(Math.abs(change)) : Math.round(Math.abs(change) * 10) / 10;
  return ", " + rounded + "% " + (change > 0 ? "more" : "less") + " than " + phrase;
}

// The window, and the project when one is picked: "Last 30 days in
// claudeglass".
function scopeLabel() {
  return windowLabel(state.window) + (state.project ? " in " + projectName(state.project) : "");
}

// Built from fixed wording and numbers only (docs/writing-help.md).
function summarySentence(facts) {
  var first = scopeLabel() + ": " + spendClause(facts.cost) + changeClause(facts.cost, facts.previousCost, facts.phrase) + ".";
  var saving = money(facts.saving);
  var worth = facts.worth;
  var second;
  if (worth && saving) {
    second = (worth === 1 ? "1 change is" : thousands(worth) + " changes are") + " worth making, and all the ways to save together come to at most " + saving.primary + ".";
  } else if (worth) {
    second = worth === 1 ? "1 change is worth making." : thousands(worth) + " changes are worth making.";
  } else if (saving) {
    second = "Nothing needs changing now, but all the ways to save together come to at most " + saving.primary + ".";
  } else {
    second = "Nothing stands out to change.";
  }
  return first + " " + second;
}

// Before any session is read: what the tool does, in three lines.
function firstRun(container, health) {
  var scanning = health && (health.status === "starting" || (health.scan && health.scan.scanning));
  container.appendChild(
    el("div", { class: "overview-first-run" }, [
      scanning
        ? el("p", { class: "overview-summary", text: "Your Claude Code history is being read now. Figures appear here as soon as it finishes." })
        : el("p", { class: "overview-summary" }, [
            "No Claude Code sessions have been read yet. They appear here soon after you use Claude Code. ",
            pageLink("data", "Data quality"),
            " shows how many transcript files the last scan checked.",
          ]),
      el("h2", { text: "What ClaudeGlass does for you" }),
      el("ul", { class: "overview-promise" }, [
        el("li", { text: "Reads the Claude Code transcripts on this computer and works out what each session cost. Nothing leaves your machine." }),
        el("li", { text: "Shows where the tokens went: the cache, subagents, long tool output and conversation summaries." }),
        el("li", { text: "Suggests changes as prompts and commands you copy and run yourself. It never changes your settings." }),
      ]),
    ])
  );
}

// -- the headline numbers ----------------------------------------------------------------

// The price of a cache read against fresh input on the model that read
// the most from the cache in this window (report.meta.rates; the daily
// rows say which model read what).
function cacheReadRatio(meta, dailyRows) {
  var rates = (meta && meta.rates) || {};
  var reads = {};
  (dailyRows || []).forEach(function (row) {
    if (row.model) reads[row.model] = (reads[row.model] || 0) + (Number(row.cache_read_tokens) || 0);
  });
  var top = Object.keys(reads).sort(function (a, b) {
    return reads[b] - reads[a];
  })[0];
  var ratio = top && rates[top] && rates[top].cache_read_ratio;
  return typeof ratio === "number" && ratio > 0 ? ratio : null;
}

function moneyTile(label, usd, opts) {
  var parts = moneyParts(usd);
  return tile({
    label: label,
    value: parts.value,
    unit: parts.unit,
    basis: opts.basis,
    delta: opts.delta,
    hint: opts.hint || parts.secondary || null,
    note: opts.note,
    link: opts.link,
    class: opts.class,
  });
}

// Actions whose saving a Savings lever already counts. The auto-compact
// window is the compaction lever's own finding, and a fresh session after
// a plan, or a fresh subagent run, saves the same carried context, so
// none of them is added on top. Nor is the tool-output action (the
// tool-output lever) or wasted replies (their lever).
var LEVER_RULES = {
  "model-tier": "model_swap",
  "model-tier-main": "model_swap",
  "compaction-window": "compaction_sim",
  "compaction-churn": "compaction_sim",
  "plan-handoff": "compaction_sim",
  "run-split": "compaction_sim",
  "tool-output-carry": "carry",
  "wasted-turns": "waste",
};

// The main session's agent type, as model_swap_by_agent_type and a
// recommendation's agent_type name it.
var MAIN_AGENT = "top-level";

function usdOf(value) {
  var number = typeof value === "number" ? value : parseFloat(value);
  return isFinite(number) && number > 0 ? number : 0;
}

// What doing every way to save would come to: the four Savings levers,
// and any priced action no lever counts (lower effort, say), counted once
// as Actions lists it. They overlap, so they aren't added up. Each is a
// share of the spend it comes from, taken from what the others leave: a
// cheaper model prices the fewer tokens earlier summaries leave, so the
// two multiply. A saving for one agent type comes from that type's spend
// (the model lever per type; the summary lever from the main session);
// any other from all of it. Each lever is a ceiling, so the whole is too,
// and never more than the spend.
function availableSaving(levers, groups, tables) {
  var agents = tableObjects((tables || {}).model_swap_by_agent_type);
  var items = [];
  levers.forEach(function (lever) {
    if (!(lever.usd > 0)) return;
    if (lever.key === "model_swap" && agents.length) {
      agents.forEach(function (row) {
        if (usdOf(row.saving_usd)) items.push({ usd: usdOf(row.saving_usd), agent: row.agent_type });
      });
    } else {
      items.push({ usd: lever.usd, agent: lever.key === "compaction_sim" ? MAIN_AGENT : null });
    }
  });
  groups.forEach(function (group) {
    if (LEVER_RULES[group.id]) return;
    var usd = groupSavingUsd(group);
    var owners = group.members.map(function (rec) {
      return rec.agent_type || null;
    });
    var single = owners.every(function (agent) {
      return agent && agent === owners[0];
    });
    if (usd > 0) items.push({ usd: usd, agent: single ? owners[0] : null });
  });
  var spend = {};
  agents.forEach(function (row) {
    if (usdOf(row.observed_cost)) spend[row.agent_type] = usdOf(row.observed_cost);
  });
  return combinedSaving(items, spend);
}

// ``items`` ({usd, agent}) combined over ``spend`` (agent type -> cost):
// each agent type keeps (1 - share) of its spend per saving, so the whole
// comes to spend less what is kept. With no spend by agent type (no
// report), the savings are simply added up.
function combinedSaving(items, spend) {
  var agents = Object.keys(spend);
  var whole = agents.reduce(function (sum, agent) {
    return sum + spend[agent];
  }, 0);
  if (!(whole > 0)) {
    return items.reduce(function (sum, item) {
      return sum + item.usd;
    }, 0);
  }
  var kept = {};
  agents.forEach(function (agent) {
    kept[agent] = 1;
  });
  items.forEach(function (item) {
    if (item.agent && spend[item.agent]) {
      kept[item.agent] *= Math.max(0, 1 - item.usd / spend[item.agent]);
      return;
    }
    var share = Math.max(0, 1 - item.usd / whole);
    agents.forEach(function (agent) {
      kept[agent] *= share;
    });
  });
  return agents.reduce(function (sum, agent) {
    return sum + spend[agent] * (1 - kept[agent]);
  }, 0);
}

// Spend, what cache reads saved and the sessions, against the period
// before. What the ways to save come to is in the sentence above, and
// each one on its row of "Anything wrong?".
function renderTiles(container, facts, meta, dailyRows) {
  var period = facts.phrase;
  var hasPrevious = !!period;
  var ratio = cacheReadRatio(meta, dailyRows);
  var ratioWords = ratio ? fraction(ratio) : "";
  var subagentRuns = Math.max(0, (facts.summary.transcripts || 0) - (facts.summary.sessions || 0));

  // Whole sessions (/api/summary): the daily spend chart counts replies
  // by the day they were sent, and says so beside this figure.
  var spend = moneyTile("Spend", facts.cost, {
    delta: hasPrevious ? deltaChip(facts.cost, facts.previousCost, { period: period }) : null,
    note:
      state.window === "change"
        ? "Every session started since your last change, so all of it ran on the new settings."
        : "Every session with a reply in this window, earlier replies included.",
    class: "overview-spend",
  });
  // Before the cost of writing to the cache: Cache › Rebuilds leads with
  // the saving after it, so the two figures differ and each says which.
  var cache = moneyTile("Saved by cache reads", facts.summary.cache_saved || 0, {
    basis: "estimate",
    delta: hasPrevious ? deltaChip(facts.summary.cache_saved, facts.previousSummary && facts.previousSummary.cache_saved, { period: period, upIsGood: true }) : null,
    note: ratioWords
      ? "On the model you use most, a cache read costs " + ratioWords + " the input price. This is what those reads saved against sending them fresh, before paying for the cache writes."
      : "What your cache reads saved against sending them fresh, before paying for the cache writes.",
    link: pageLink("cache/rebuilds", "See how the cache is doing"),
  });
  var sessions = tile({
    label: "Sessions",
    value: thousands(facts.summary.sessions || 0),
    hint: subagentRuns ? "and " + thousands(subagentRuns) + (subagentRuns === 1 ? " subagent run" : " subagent runs") : "No subagent runs",
    link: pageLink("spend/sessions", "See the sessions"),
  });
  container.appendChild(tileRow([spend, cache, sessions], { class: "overview-tiles" }));
  var counts = [tileCount(spend, "spend", facts.cost, moneyValue(facts.cost)), tileCount(cache, "cache", facts.summary.cache_saved || 0, moneyValue(facts.summary.cache_saved || 0)), tileCount(sessions, "sessions", facts.summary.sessions || 0, wholeNumber)];
  return { spend: spend, counts: counts };
}

// A tile's figure as it counts up: its node, the value it ends on and how
// to write the values on the way (money in the billing mode's units).
function tileCount(tileNode, key, value, write) {
  return { node: tileNode.querySelector(".metric-value > span"), key: key, value: value, write: write };
}

// A writer for a tile counting up to final: each step in final's unit,
// so a count past 200% of the weekly limit doesn't switch to weeks midway.
function moneyValue(final) {
  return function (usd) {
    return moneyParts(usd, { like: final }).value;
  };
}

function wholeNumber(n) {
  return thousands(Math.round(n));
}

// Count each headline figure up from the one this tile last showed.
function countTiles(counts) {
  var from = shownFigures || {};
  shownFigures = {};
  counts.forEach(function (count) {
    countUp(count.node, from[count.key] || 0, count.value, count.write);
    shownFigures[count.key] = count.value;
  });
}

// The Spend tile's trend: one point a day, from 3 days up.
function addSpendTrend(spendTile, rows) {
  if (!spendTile) return;
  var byDay = {};
  rows.forEach(function (row) {
    if (row.day) byDay[row.day] = (byDay[row.day] || 0) + (Number(row.cost) || 0);
  });
  var days = Object.keys(byDay).sort();
  if (days.length < 3) return;
  var line = sparkline(
    days.map(function (day) {
      return byDay[day];
    }),
    { width: 120, height: 24 }
  );
  if (line) spendTile.appendChild(el("div", { class: "metric-spark", title: "Daily spend, " + thousands(days.length) + " days" }, [line]));
}

// -- anything wrong? ------------------------------------------------------------------------

// about: the action it copies for. The button shows "Copy prompt" and is
// named "Copy prompt for <action>", as codeBlockWithCopy names its
// buttons, so several of them read as several different things.
function copyPromptButton(prompt, about) {
  var node = button("Copy prompt", { variant: "quiet", icon: "prompt", class: "action-copy", label: "Copy prompt" + (about ? " for " + about : "") });
  node.addEventListener("click", function () {
    copyToClipboard(prompt).then(function (ok) {
      toast(ok ? "Prompt copied. Paste it into Claude Code." : "Couldn't copy. Open the action and copy the prompt from there.", {
        tone: ok ? "success" : "warning",
      });
    });
  });
  return node;
}

// Each check's area in a few words (quick_actions.CHECKS), for the
// checklist's rows.
var CHECK_NAMES = {
  models: "Models",
  effort: "Thinking effort",
  compaction: "Conversation summaries",
  cache: "Cache lifetime",
  tools: "Tools, MCP servers and skills",
  skills: "Skills",
  "claude-md": "CLAUDE.md files",
  "tool-output": "Tool output",
  hooks: "Hooks",
  "tool-search": "MCP tool search",
  habits: "Work habits",
  quality: "Agent quality",
  "cost-record": "ClaudeGlass's own figures",
};

// A row's state, in the icon and word Actions uses for it: fix (a rule
// that says "Do this"), look (worth a look), fine (nothing to do) and
// unknown (not enough data).
var ROW_LOOK = {
  fix: { cls: "severity-action", icon: "critical", word: "Do this" },
  look: { cls: "severity-advice", icon: "warning", word: "Worth a look" },
  fine: { cls: "severity-good", icon: "success", word: "Nothing to do" },
  unknown: { cls: "severity-info", icon: "info", word: "Not enough data" },
};

function rowBadge(state) {
  var look = ROW_LOOK[state];
  return el("span", { class: "severity-badge " + look.cls }, [icon(look.icon, { size: 14 }), el("span", { text: look.word })]);
}

// The checklist's rows: one per check (/api/quick-actions), carrying the
// Actions items (groupRecommendations) its rules raised, so a check and
// the recommendation it leads to are one row. An item no check draws on
// is a row of its own. For-your-information items aren't problems.
export function checklistRows(checks, groups) {
  var problems = groups.filter(function (group) {
    return group.severity === "action" || group.severity === "advice";
  });
  var claimed = {};
  var rows = checks.map(function (check) {
    var ids = check.rule_ids || [];
    var mine = problems.filter(function (group) {
      return ids.indexOf(group.id) !== -1;
    });
    mine.forEach(function (group) {
      claimed[group.key] = true;
    });
    var raised = mine.length ? (mine.some(isAction) ? "fix" : "look") : null;
    var state = raised || (check.status === "act" ? "look" : check.status === "ok" ? "fine" : "unknown");
    return { check: check, groups: mine, state: state };
  });
  problems.forEach(function (group) {
    if (!claimed[group.key]) rows.push({ check: null, groups: [group], state: isAction(group) ? "fix" : "look" });
  });
  var order = { fix: 0, look: 1, fine: 2, unknown: 3 };
  return rows
    .map(function (row, i) {
      row.saving = row.groups.reduce(function (most, group) {
        return Math.max(most, groupSavingUsd(group));
      }, 0);
      row.index = i;
      return row;
    })
    .sort(function (a, b) {
      return order[a.state] - order[b.state] || b.saving - a.saving || a.index - b.index;
    });
}

function isAction(group) {
  return group.severity === "action";
}

// The first sentence of a check's answer, for its row.
function firstSentence(text) {
  var match = String(text || "").match(/^.*?[.!?](\s|$)/);
  return (match ? match[0] : String(text || "")).trim();
}

// One row: its state, its area and what's wrong, what fixing it saves,
// and the way to the fix (a prompt to copy when there's one, and a link
// to the item or the check on Actions).
function checklistRow(row) {
  var check = row.check;
  var lead = row.groups[0];
  var name = check ? CHECK_NAMES[check.id] || check.question : "Other";
  var finding = lead ? groupTitle(lead) + (row.groups.length > 1 ? " (and " + (row.groups.length - 1) + " more)" : "") : firstSentence(check && check.summary);
  var text = el("div", { class: "check-row-text" }, [el("p", { class: "check-row-title" }, [el("strong", { text: name }), el("span", { text: finding })])]);
  if (row.state === "fix" || row.state === "look") {
    var saving = lead ? listSaving(lead) : "";
    text.appendChild(
      el("p", {
        class: "check-row-detail" + (saving ? "" : " is-unestimated"),
        text: saving || (lead ? "Saving not worked out: it depends on how you use it." : ""),
      })
    );
  }
  var action = el("div", { class: "check-row-action" });
  var fix = lead && lead.members.length === 1 ? (lead.members[0].fixes || [])[0] : null;
  if (fix && fix.prompt) action.appendChild(copyPromptButton(fix.prompt, groupTitle(lead)));
  if (lead) action.appendChild(pageLink("actions/recommendations", lead.members.length > 1 ? "See the " + lead.members.length + " prompts" : "See the fix", { id: lead.key }));
  else if (check && (row.state === "look" || row.state === "fix")) action.appendChild(pageLink("actions/checks", "See the check", { id: check.id }));
  return el("li", { class: "check-row is-" + row.state }, [rowBadge(row.state), text, action]);
}

// A folded list of the rows that need nothing: their area and the first
// sentence of their answer, each linked to its check.
function quietRows(rows, summaryText) {
  var list = el(
    "ul",
    { class: "check-quiet-list" },
    rows.map(function (row) {
      return el("li", null, [
        rowBadge(row.state),
        pageLink("actions/checks", CHECK_NAMES[row.check.id] || row.check.question, { id: row.check.id }),
        el("span", { class: "check-quiet-text", text: " " + firstSentence(row.check.summary) }),
      ]);
    })
  );
  return el("details", { class: "disclosure check-quiet" }, [el("summary", { text: summaryText }), list]);
}

function countWord(n, one, many) {
  return n === 1 ? "1 " + one : thousands(n) + " " + many;
}

// "Anything wrong?": what needs fixing first, then what's worth a look,
// then the checks with nothing to do and those with too little data,
// each folded into one line.
function renderChecklist(container, head, rows) {
  var by = { fix: [], look: [], fine: [], unknown: [] };
  rows.forEach(function (row) {
    by[row.state].push(row);
  });
  var parts = [];
  if (by.fix.length) parts.push(countWord(by.fix.length, "thing to fix", "things to fix"));
  if (by.look.length) parts.push(countWord(by.look.length, "worth a look", "worth a look"));
  if (by.fine.length) parts.push(countWord(by.fine.length, "check fine", "checks fine"));
  if (by.unknown.length) parts.push(countWord(by.unknown.length, "without enough data", "without enough data"));
  head.appendChild(el("p", { class: "overview-answer-count", text: parts.join(" · ") }));
  var problems = by.fix.concat(by.look);
  if (!problems.length) {
    container.appendChild(
      el("p", { class: "check-none" }, [rowBadge("fine"), el("span", { text: " Nothing looks wrong in this window: every check with enough data found nothing to fix." })])
    );
  } else {
    container.appendChild(el("ul", { class: "check-rows" }, problems.map(checklistRow)));
  }
  if (by.fine.length) container.appendChild(quietRows(by.fine, countWord(by.fine.length, "check found nothing to fix", "checks found nothing to fix")));
  if (by.unknown.length) container.appendChild(quietRows(by.unknown, countWord(by.unknown.length, "check had too little data to answer", "checks had too little data to answer")));
}


function tableNamed(section, name) {
  return ((section && section.tables) || []).filter(function (t) {
    return t.name === name;
  })[0];
}


// -- the page ------------------------------------------------------------------------------------

// One of the Overview's three answers: an h2 asking the question, a
// line under it that answers in brief, then the body.
function answerSection(id, question, extraClass) {
  var head = el("div", { class: "overview-answer-head" }, [el("h2", { id: id + "-title", text: question })]);
  var body = el("div", { class: "overview-answer-body" });
  var section = el("section", { class: "overview-answer" + (extraClass ? " " + extraClass : ""), id: id, "aria-labelledby": id + "-title" }, [head, body]);
  return { section: section, head: head, body: body };
}

// Where the window's tokens went, by model: the daily rows summed.
function byModelRows(dailyRows) {
  var cost = {};
  dailyRows.forEach(function (row) {
    if (row.model) cost[row.model] = (cost[row.model] || 0) + (Number(row.cost) || 0);
  });
  return Object.keys(cost)
    .filter(function (model) {
      return cost[model] > 0;
    })
    .sort(function (a, b) {
      return cost[b] - cost[a];
    })
    .map(function (model) {
      return [model, cost[model]];
    });
}

// "Where do your tokens go?" under the chart: by project (the report's
// usage table) and by model, each a short ranked list with bars. A list
// of one says nothing a ranking would, so it isn't drawn.
function renderBreakdown(container, report, dailyRows) {
  clear(container);
  var parts = [];
  var byProject = report ? tableNamed(findSection(report, "usage"), "by_project") : null;
  if (byProject && byProject.rows && byProject.rows.length > 1) {
    // The table's "How to read this" sits beside this heading.
    var head = headRow(el("h3", { text: "By project" }), null, "By project");
    parts.push(el("div", { class: "overview-breakdown-part" }, [head, renderTable(byProject, "overview-by-project", state.currency, { heading: false, helpInto: head })]));
  }
  var models = byModelRows(dailyRows);
  if (models.length > 1) {
    parts.push(
      el("div", { class: "overview-breakdown-part" }, [
        el("h3", { text: "By model" }),
        dataGrid({
          id: "overview-by-model",
          caption: "Spend by model",
          columns: [
            { key: "model", label: "Model", kind: "str" },
            { key: "cost", label: "Cost", kind: "money" },
          ],
          rows: models,
          sortable: false,
        }),
      ])
    );
  }
  parts.forEach(function (part) {
    container.appendChild(part);
  });
}

export function renderOverview(panel) {
  var run = ++overviewRun;
  function current() {
    return run === overviewRun;
  }
  clear(panel);
  viewIntro(panel, "overview");

  var notices = el("div", { class: "overview-notices" });
  var sentence = el("div", { class: "overview-lead", "aria-live": "polite" }, [loadingNode("Loading this window", "lines")]);

  // 1. Anything wrong?
  var wrong = answerSection("overview-wrong", "Anything wrong?");
  wrong.body.appendChild(loadingNode("Loading the checks", "rows"));
  // 2. Did your changes work?
  var changes = answerSection("overview-changes", "Did your changes work?");
  changes.body.appendChild(loadingNode("Loading your changes", "rows"));
  // 3. Where do your tokens go?
  var tokens = answerSection("overview-tokens", "Where do your tokens go?");
  var tilesHost = el("div", { class: "overview-tiles-host" }, [loadingNode("Loading the headline numbers", "tiles")]);
  var chartHost = el("div", { class: "overview-chart" });
  var breakdownHost = el("div", { class: "overview-breakdown" });
  tokens.body.appendChild(tilesHost);
  tokens.body.appendChild(chartHost);
  tokens.body.appendChild(breakdownHost);

  var details = el("details", { class: "disclosure overview-details", id: "overview-details" });
  details.appendChild(el("summary", { text: "Scores, totals, and how amounts are counted" }));
  var detailsBody = el("div", { class: "overview-details-body" });
  details.appendChild(detailsBody);

  var body = el("div", { class: "overview-body" }, [wrong.section, changes.section, tokens.section, details]);
  panel.appendChild(notices);
  panel.appendChild(sentence);
  panel.appendChild(body);

  // Every load starts at once; the drawing waits for the report, which
  // sets the billing mode every amount is written in.
  var reportLoad = loadReport();
  var healthLoad = fetchJson("/api/health");
  var setupLoad = fetchJson("/api/setup/status");
  var summaryLoad = fetchJson(withWindow("/api/summary"));
  var previous = previousPeriod(state.window, Date.now());
  var previousLoad = previous
    ? fetchJson(withProject("/api/summary?since=" + encodeURIComponent(isoMinute(previous.since)) + "&until=" + encodeURIComponent(isoMinute(previous.until))))
    : Promise.resolve(null);
  var dailyLoad = fetchJson(withWindow("/api/daily-usage") + "&split=agent");
  var impactLoad = fetchJson("/api/impact");
  // Recommendations and checks are built from the report, so they follow it.
  var recsLoad = reportLoad.then(function () {
    return loadRecommendations();
  });
  var checksLoad = reportLoad.then(function () {
    return loadQuickActions();
  });
  if (!holdChart(chartHost, "daily-spend", { slot: "overview" })) chartHost.appendChild(loadingNode("Loading daily spend", "chart"));
  // When the headline figures began to count: the chart draws in after.
  var entrance = null;

  setupLoad.then(function (result) {
    if (!current()) return;
    renderSetupCard(result.body && result.body.ok ? result.body.data : null, notices);
  });

  var figuresDrawn = Promise.all([reportLoad, summaryLoad, previousLoad, recsLoad, healthLoad, dailyLoad, checksLoad]).then(function (loaded) {
    if (!current()) return;
    var reportResult = loaded[0];
    var summaryBody = loaded[1].body;
    var previousBody = loaded[2] && loaded[2].body;
    var recsBody = loaded[3].body;
    var healthBody = loaded[4].body;
    var dailyBody = loaded[5].body;
    var checksBody = loaded[6].body;
    var dailyRows = dailyBody && dailyBody.ok === true ? dailyBody.data || [] : [];
    clear(sentence);
    clear(tilesHost);
    var summaryError = summaryBody && summaryBody.ok !== true ? summaryBody.error : null;
    var projectError = !!summaryError && String(summaryError.message).indexOf("'project'") !== -1;
    if (summaryError && state.window === "change" && summaryError.code === "bad_request" && !projectError) {
      // "Since my last change" with no change recorded has nowhere to start.
      sentence.appendChild(
        el("p", { class: "overview-summary", text: "No change recorded yet, so this window has nowhere to start. Pick another window, or come back after you change a setting." })
      );
      body.hidden = true;
      return;
    }
    if (!summaryBody || summaryBody.ok !== true) {
      sentence.appendChild(errorNotice(summaryBody && summaryBody.error, function () {
        goTo("overview", { force: true });
      }));
      body.hidden = true;
      return;
    }
    var summary = summaryBody.data || {};
    var report = reportResult && reportResult.report;
    var meta = (report && report.meta) || {};
    var recs = recsBody && recsBody.ok === true ? recsBody.data || [] : [];
    // Counted as Actions lists them: one item per rule and severity,
    // whatever the number of agent types it's for.
    var groups = groupRecommendations(recs);

    if (!summary.sessions) {
      body.hidden = true;
      if (state.window === "all" && !state.project) {
        firstRun(sentence, healthBody && healthBody.ok ? healthBody.data : null);
      } else if (state.project) {
        sentence.appendChild(
          el("p", {
            class: "overview-summary",
            text: "No sessions in " + projectName(state.project) + " " + windowWhen(state.window) + ". Pick a longer window, or all projects.",
          })
        );
      } else {
        sentence.appendChild(el("p", { class: "overview-summary", text: "No sessions " + windowWhen(state.window) + ". Pick a longer window to see older ones." }));
      }
      return;
    }

    var reportTables = report ? tablesOf(report) : {};
    var levers = report ? savingsLevers(reportTables) : [];
    var facts = {
      summary: summary,
      previousSummary: previousBody && previousBody.ok === true ? previousBody.data : null,
      cost: summary.total_cost || 0,
      phrase: previous ? previous.phrase : null,
    };
    facts.previousCost = facts.previousSummary ? facts.previousSummary.total_cost || 0 : null;
    facts.saving = availableSaving(levers, groups, reportTables);
    var tiles = renderTiles(tilesHost, facts, meta, dailyRows);
    addSpendTrend(tiles.spend, dailyRows);
    countTiles(tiles.counts);
    entrance = performance.now();
    // 1. Anything wrong? Every check, with the Actions items its rules
    // raised on the same row. The sentence counts the same rows.
    var rows = checksBody && checksBody.ok === true ? checklistRows((checksBody.data && checksBody.data.checks) || [], groups) : null;
    facts.worth = rows
      ? rows.filter(function (row) {
          return row.state === "fix" || row.state === "look";
        }).length
      : groups.filter(function (group) {
          return group.severity === "action" || group.severity === "advice";
        }).length;
    sentence.appendChild(el("p", { class: "overview-summary", text: summarySentence(facts) }));
    clear(wrong.body);
    if (rows) {
      renderChecklist(wrong.body, wrong.head, rows);
      enterInTurn(wrong.body.querySelectorAll(".check-row"), ROWS_AFTER_MS);
    } else {
      wrong.body.appendChild(errorNotice(checksBody && checksBody.error, function () {
        goTo("overview", { force: true });
      }));
    }
    renderBreakdown(breakdownHost, report, dailyRows);

    clear(detailsBody);
    if (reportResult.error) {
      detailsBody.appendChild(errorNotice(reportResult.error));
      return;
    }
    renderDetails(detailsBody, report);
  });

  // The figures failing to draw still let the rest draw; their error is
  // thrown on its own, as it would have been.
  var figuresDone = figuresDrawn.then(null, function (err) {
    setTimeout(function () {
      throw err;
    });
  });

  // 2. Did your changes work? The latest two, in short; the rest on
  // Your changes.
  Promise.all([impactLoad, figuresDone]).then(function (loaded) {
    if (!current() || body.hidden) return;
    clear(changes.body);
    var impact = loaded[0].body;
    if (!impact || impact.ok !== true) {
      changes.body.appendChild(errorNotice(impact && impact.error));
      return;
    }
    var count = renderChangeCards(changes.body, impact.data, { compact: true, limit: CHANGES_SHOWN });
    if (count) changes.head.appendChild(el("p", { class: "overview-answer-count" }, [changesLink(count)]));
  });

  // 3. Where do your tokens go? The chart waits for the headline
  // figures, so it draws in after them.
  var chartDone = Promise.all([dailyLoad, impactLoad, reportLoad, figuresDone, summaryLoad]).then(function (loaded) {
    if (!current() || body.hidden) return;
    var daily = loaded[0].body;
    if (!daily || daily.ok !== true) {
      chartError(chartHost, "daily-spend", daily && daily.error, function () {
        goTo("overview", { force: true });
      }, { slot: "overview", titleTag: "h3" });
      return;
    }
    var summaryBody = loaded[4].body;
    renderChart(
      chartHost,
      "daily-spend",
      // Every day of the window, and the Spend tile's figure, so the
      // chart's reading says why its total differs.
      Object.assign(
        { rows: daily.data || [], split: "agent", changes: dailyChanges(loaded[1].body), sessionsTotal: summaryBody && summaryBody.ok === true ? summaryBody.data.total_cost : null },
        windowDays(state.window)
      ),
      {
        slot: "overview",
        titleTag: "h3",
        delay: entrance === null ? 0 : Math.max(0, entrance + CHART_AFTER_MS - performance.now()),
        // A day leads to the sessions active on it.
        open: function (day) {
          goTo("spend/sessions", { params: { day: day } });
        },
      }
    );
  });

  // "Since my last change": what the sessions started since would have
  // cost without it, under the headline. All projects only: the figure
  // isn't split by project.
  Promise.all([impactLoad, figuresDone]).then(function (loaded) {
    if (!current() || body.hidden || state.window !== "change" || state.project) return;
    var line = lastChangeLine(loaded[0].body);
    if (line) sentence.appendChild(line);
  });

  // Once the page has settled, Actions' figures load while it is idle.
  chartDone.then(function () {
    if (current()) prefetchActions();
  });
}


// The report's tables by name, across sections, for the savings levers.
function tablesOf(report) {
  var tables = {};
  (report.sections || []).forEach(function (section) {
    (section.tables || []).forEach(function (table) {
      tables[table.name] = table;
    });
  });
  return tables;
}

// Details: which billing mode the amounts follow and why, how the setup
// scores (the table a recommendation's evidence can point at), and the
// totals.
function renderDetails(container, report) {
  var meta = report.meta || {};
  // meta.amounts_basis says whether amounts are shares of the weekly
  // limit or list-price equivalents (older reports lack it).
  var basis =
    meta.amounts_basis ||
    (meta.billing_mode === "subscription" ? "Amounts are list-price equivalents, not what you are charged." : "Amounts are what the tokens cost at list price.");
  container.appendChild(
    el("p", {
      class: "notes",
      id: "overview-billing",
      text:
        (meta.billing_mode === "subscription" ? "Billing: Pro or Max plan" : "Billing: pay per token (API)") +
        (meta.billing_source ? " (" + meta.billing_source + "). " : ". ") +
        basis,
    })
  );
  var scores = tableNamed(findSection(report, "scorecard"), "dimensions");
  if (scores) container.appendChild(renderTable(scores, "overview-scores-table", state.currency));
  var totals = tableNamed(findSection(report, "overview"), "totals");
  // Cost by model is on Spend, Usage (links.js's TABLE_PAGE_MAP).
  if (totals) container.appendChild(renderTable(totals, "overview-totals-table", state.currency));
  else container.appendChild(emptyState("No totals for this window: it had no sessions.", null, "Pick a longer window."));
}

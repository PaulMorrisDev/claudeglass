/* claudeglass service UI: page-actions.js
 *
 * The Actions page: Recommendations and Checks. Each is an inbox: the
 * list to pick from on the left, the one picked on the right. The
 * address says which one is open (#/actions/recommendations?id=<key>),
 * so a link from anywhere can open it directly.
 */

import { clear, el, goTo, onParams, renderedViews, state } from "./core.js";
import { fetchJson, findSection, groupedTitle, loadInto, loadQuickActions, loadRecommendations, loadReport, postJson, withWindow } from "./api.js";
import {
  AGENT_LABELS,
  basisChip,
  button,
  callout,
  CHECK_STATUS_ORDER,
  chip,
  commandBlock,
  copyToClipboard,
  emptyState,
  errorNotice,
  loadingNode,
  motionOK,
  prose,
  renderFix,
  renderTips,
  SCOPE_LABELS,
  SEVERITY_LABELS,
  SEVERITY_ORDER,
  severityChip,
  severityMark,
  statusBadge,
  statusLabel,
  statusMark,
  toast,
} from "./ui.js";
import { cardRating, dataGrid, simpleTable } from "./grid.js";
import { modelName, modelNames, projectName } from "./format.js";
import { formatHash, pageLink, replaceParams, scopeParams, viewIntro } from "./links.js";
import { evidenceList } from "./evidence.js";
import { modelSentence, priced, pricingFacts } from "./costs.js";

// ======================================================================
// What each recommendation is about
// ======================================================================

// The part of your setup each rule is about, for the area filter
// (Recommendation.category only says settings, workflow or data). A
// test keeps it in step with every rule id the service can send.
export var RULE_AREA = {
  "model-tier": "models",
  "model-tier-main": "models",
  "agent-model-inherited": "models",
  "agent-model-asked": "models",
  "agent-decide-apply": "models",
  "effort-mismatch": "models",
  "env-subagent-model": "models",
  "env-max-output-tokens": "models",
  "ttl-switch": "cache",
  "long-tool-waits": "cache",
  "notification-invalidation": "cache",
  "batch-instructions": "cache",
  "cache-read-dominance": "cache",
  "env-disable-prompt-caching": "cache",
  "compaction-churn": "context",
  "compaction-window": "context",
  "long-context-share": "context",
  "baseline-bloat": "context",
  "mcp-unused-server": "context",
  "tool-output-carry": "context",
  "plan-handoff": "context",
  "hook-context-carry": "context",
  "env-tool-search": "context",
  "saver-tool-roi": "context",
  "unused-skills": "context",
  "unused-skill": "context",
  "tool-skill-name-only": "context",
  "spawn-claude-md": "agents",
  "spawn-shared-claude-md": "agents",
  "spawn-cost": "agents",
  "spawn-unused-skills": "agents",
  "spawn-unused-mcp": "agents",
  "spawn-read-only-tools": "agents",
  "spawn-task-prompt": "agents",
  "subagent-volume": "agents",
  "agent-report-size": "agents",
  "run-split": "agents",
  "discovery-share": "habits",
  "limit-pressure": "habits",
  "window-budget": "habits",
  "wasted-turns": "habits",
  "data-quality": "data",
  "pricing-coverage": "data",
  "env-attribution-deprecated": "data",
  "hook-failures": "data",
  "hook-block-resent": "data",
};

var AREAS = [
  { id: "models", label: "Models" },
  { id: "cache", label: "Cache" },
  { id: "context", label: "Context" },
  { id: "agents", label: "Agents" },
  { id: "habits", label: "Habits" },
  { id: "data", label: "Data and settings" },
];

function areaOf(ruleId) {
  return RULE_AREA[ruleId] || "data";
}

function areaLabel(area) {
  for (var i = 0; i < AREAS.length; i++) {
    if (AREAS[i].id === area) return AREAS[i].label;
  }
  return area;
}

// How each kind of change saves money: which of the sentences below
// explains it. A rule not listed has no price rule to explain (a usage
// pattern, a data caveat).
var RULE_MECHANISM = {
  "model-tier": "model",
  "model-tier-main": "model",
  "agent-model-inherited": "model",
  "agent-model-asked": "model",
  "agent-decide-apply": "model",
  "env-subagent-model": "model",
  "effort-mismatch": "thinking",
  "env-max-output-tokens": "output",
  "ttl-switch": "lifetime",
  "long-tool-waits": "rebuild",
  "notification-invalidation": "rebuild",
  "batch-instructions": "rebuild",
  "cache-read-dominance": "read",
  "env-disable-prompt-caching": "caching-off",
  "compaction-churn": "summary",
  "compaction-window": "summary",
  "long-context-share": "carried",
  "tool-output-carry": "carried",
  "plan-handoff": "carried",
  "hook-context-carry": "carried",
  "run-split": "carried",
  "baseline-bloat": "carried",
  "mcp-unused-server": "carried",
  "env-tool-search": "carried",
  "saver-tool-roi": "carried",
  "discovery-share": "carried",
  "unused-skills": "startup",
  "unused-skill": "startup",
  "tool-skill-name-only": "startup",
  "spawn-claude-md": "startup",
  "spawn-shared-claude-md": "startup",
  "spawn-cost": "startup",
  "spawn-unused-skills": "startup",
  "spawn-unused-mcp": "startup",
  "spawn-read-only-tools": "startup",
  "spawn-task-prompt": "startup",
  "agent-report-size": "report",
};

// One or two short sentences on what the change does to the price, with
// the multiplier from your pricing. Empty when the rates are missing.
function mechanismText(group, facts) {
  var kind = RULE_MECHANISM[group.id];
  var main = facts.main;
  if (!kind || !main) return "";
  var read = priced(main.cache_read_ratio);
  var write = priced(main.cache_write_5m_ratio);
  var output = main.input ? priced(main.output / main.input) : "";
  switch (kind) {
    case "model":
      return modelSentence(facts, group);
    case "thinking":
      return output ? "Thinking is billed as output, at " + output + ". Lower effort means less thinking on work that doesn't need it." : "";
    case "output":
      return output ? "Replies are billed as output, at " + output + ". A higher cap lets long replies run on, and every extra token costs that much." : "";
    case "lifetime":
      return write && main.cache_write_1h_ratio
        ? "Writing to the cache costs " + write + " for a 5-minute lifetime, and " + priced(main.cache_write_1h_ratio) + " for 1 hour. The longer lifetime pays for itself when it avoids enough rebuilds after idle gaps."
        : "";
    case "rebuild":
      return read && write
        ? "Reading from the cache costs " + read + ". A rebuild writes the whole conversation to the cache again, at " + write + ". Each rebuild you avoid saves the difference."
        : "";
    case "read":
      return read ? "Reading from the cache costs " + read + ", so most of what you send already goes at the cheapest rate." : "";
    case "caching-off":
      return read ? "With caching on, the part of the conversation already sent is read back at " + read + ". With it off, every turn pays the full price for all of it." : "";
    case "summary":
      return "Every turn reads the whole conversation again. A summary replaces it with a shorter one, so each later turn reads less.";
    case "carried":
      return read
        ? "Everything in the conversation is read again on every later turn: at " + read + " from the cache, and more after a rebuild. Less to carry makes every turn cheaper."
        : "";
    case "startup":
      return write ? "Each subagent starts with its own context and writes it to the cache at " + write + ", once per spawn. Whatever you trim is saved on every spawn." : "";
    case "report":
      return "A subagent's report joins the main conversation, which reads it again on every later turn. A shorter report costs less on each of them.";
    default:
      return "";
  }
}

// How sure the saving is, from how it was worked out (saving_basis).
function savingBasis(rec) {
  var text = rec.saving_basis || "";
  if (!rec.estimated_saving) return null;
  if (/ceiling|at most/i.test(text + " " + rec.estimated_saving)) return "ceiling";
  if (/simulat|replay/i.test(text)) return "simulated";
  if (/calibrat/i.test(text)) return "calibrated";
  return "estimate";
}

// ======================================================================
// Recommendations that say the same thing for several agents
// ======================================================================

// The inbox's items: a rule that fires once per agent type, at one
// severity, is one item for all of them. The Overview and the sidebar's
// count read the same items, so every page counts what this list shows.
// Recommendations you ignored are left out; opts.ignored asks for only
// those instead (the Actions list's Ignored filter).
export function groupRecommendations(recs, opts) {
  var wantIgnored = !!(opts && opts.ignored);
  var groups = [];
  var shared = {};
  recs.forEach(function (rec) {
    if (!!rec.ignored !== wantIgnored) return;
    var slot = rec.id + "|" + rec.severity;
    if (rec.agent_type && shared[slot]) {
      shared[slot].members.push(rec);
      return;
    }
    var group = { key: rec.key || rec.id, id: rec.id, severity: rec.severity, area: areaOf(rec.id), members: [rec] };
    groups.push(group);
    if (rec.agent_type) shared[slot] = group;
  });
  // Most important first; the service's own order (by saving) within.
  var rank = function (group) {
    var i = SEVERITY_ORDER.indexOf(group.severity);
    return i === -1 ? SEVERITY_ORDER.length : i;
  };
  return groups
    .map(function (group, i) {
      return { group: group, i: i };
    })
    .sort(function (a, b) {
      return rank(a.group) - rank(b.group) || a.i - b.i;
    })
    .map(function (entry) {
      return entry.group;
    });
}

export function groupTitle(group) {
  return groupedTitle(group.id, group.members.length, group.members[0].title);
}

// What a group's changes save together, in dollars: each member's own
// saving, once. Members are different agent types, so none overlaps.
export function groupSavingUsd(group) {
  var seen = {};
  return group.members.reduce(function (total, rec) {
    var key = rec.key || rec.id;
    if (seen[key] || typeof rec.saving_usd !== "number" || !(rec.saving_usd > 0)) return total;
    seen[key] = true;
    return total + rec.saving_usd;
  }, 0);
}

function agentName(agent) {
  return AGENT_LABELS[agent] || agent;
}

// The agent types a group is for: each member's, or (one recommendation
// with a change per agent) each change's.
function groupAgents(group) {
  var names = [];
  function add(name) {
    if (name && names.indexOf(name) === -1) names.push(name);
  }
  group.members.forEach(function (rec) {
    if (rec.agent_type) add(rec.agent_type);
    else
      (rec.changes || []).forEach(function (change) {
        add(change.agent || (change.key === "model" && !change.agent ? "top-level" : ""));
      });
  });
  return names;
}

function findGroup(groups, key) {
  if (!key) return null;
  for (var i = 0; i < groups.length; i++) {
    if (groups[i].key === key) return groups[i];
    for (var j = 0; j < groups[i].members.length; j++) {
      if (groups[i].members[j].key === key) return groups[i];
    }
  }
  // A rule's id alone (a habit's covered_by_rule) opens its group.
  for (var k = 0; k < groups.length; k++) {
    if (groups[k].id === key) return groups[k];
  }
  return null;
}

// ======================================================================
// The inbox: a list to pick from, the one picked beside it
// ======================================================================

function inAppClick(event) {
  return event.button === 0 && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey;
}

// One row of filter chips: pressing one shows only its items.
// options: [{value, label, count}].
function filterRow(label, options, current, choose) {
  var row = el("div", { class: "filter-row", role: "group", "aria-label": label });
  options.forEach(function (option) {
    var chipButton = el("button", { type: "button", class: "filter-chip", "aria-pressed": option.value === current ? "true" : "false" }, [
      el("span", { text: option.label }),
      el("span", { class: "filter-count", text: String(option.count) }),
    ]);
    chipButton.addEventListener("click", function () {
      choose(option.value);
    });
    row.appendChild(chipButton);
  });
  return row;
}

// spec: viewKey, label (the list's name), items [{key, ...}], matches
// (item, filters) -> bool, filters {name: value}, drawFilters(host,
// filters, redraw), itemContent(item) -> nodes, renderDetail(item, pane,
// memberKey), empty (text, or filters -> text, when a filter leaves
// nothing), resetFilters(item) (show the item a link names).
function inbox(container, spec) {
  var root = el("div", { class: "inbox" });
  var listPane = el("div", { class: "inbox-list-pane" });
  var filtersHost = el("div", { class: "inbox-filters" });
  var list = el("ul", { class: "inbox-list", "aria-label": spec.label });
  var detail = el("div", { class: "inbox-detail" });
  listPane.appendChild(filtersHost);
  listPane.appendChild(list);
  root.appendChild(listPane);
  root.appendChild(detail);
  container.appendChild(root);

  var selected = null;
  var shownItems = [];

  function draw() {
    clear(filtersHost);
    spec.drawFilters(filtersHost, spec.filters, function () {
      draw();
      if (!selected || shownItems.indexOf(selected) === -1) {
        if (shownItems[0]) select(shownItems[0], null, { address: true });
        else {
          selected = null;
          clear(detail);
          replaceParams({});
        }
      }
    });
    clear(list);
    shownItems = spec.items.filter(function (item) {
      return spec.matches(item, spec.filters);
    });
    if (!shownItems.length) list.appendChild(el("li", { class: "inbox-none", text: typeof spec.empty === "function" ? spec.empty(spec.filters) : spec.empty }));
    shownItems.forEach(function (item) {
      var link = el("a", { class: "inbox-item", href: formatHash(spec.viewKey, Object.assign(scopeParams(), { id: item.key })), "data-key": item.key }, spec.itemContent(item));
      if (item === selected) link.setAttribute("aria-current", "true");
      link.addEventListener("click", function (event) {
        if (!inAppClick(event)) return;
        event.preventDefault();
        select(item, null, { address: true, reveal: true });
      });
      list.appendChild(el("li", null, [link]));
    });
  }

  // opts: address (say it in the address), focus (move focus to the
  // detail's title), reveal (scroll the detail into view if it's above).
  function select(item, memberKey, opts) {
    opts = opts || {};
    selected = item;
    Array.prototype.forEach.call(list.querySelectorAll(".inbox-item"), function (link) {
      if (link.getAttribute("data-key") === item.key) link.setAttribute("aria-current", "true");
      else link.removeAttribute("aria-current");
    });
    clear(detail);
    spec.renderDetail(item, detail, memberKey, function (key) {
      select(item, key, { address: true });
    });
    if (opts.address) replaceParams({ id: memberKey || item.key });
    if (opts.focus) {
      var title = detail.querySelector("h2");
      if (title) title.focus({ preventScroll: true });
    }
    if (opts.reveal || opts.focus) {
      var header = document.getElementById("page-header");
      var top = header ? header.getBoundingClientRect().bottom : 0;
      var rect = detail.getBoundingClientRect();
      if (rect.top < top || rect.top > window.innerHeight) {
        window.scrollBy({ top: rect.top - top - 16, behavior: motionOK() ? "smooth" : "instant" });
      }
    }
  }

  // The item a key names (a member's key opens its group), shown even if
  // a filter was hiding it.
  function selectKey(key, opts) {
    var item = spec.find(key);
    if (!item) return false;
    if (!spec.matches(item, spec.filters)) {
      spec.resetFilters(item);
      draw();
    }
    select(item, key !== item.key ? key : null, opts);
    return true;
  }

  draw();
  return { select: select, selectKey: selectKey, first: function () { return shownItems[0] || null; }, detail: detail, current: function () { return selected; } };
}

// A deep link to something this window doesn't have: say so above the
// first item instead of opening nothing.
function missingNote(detail, what) {
  detail.insertBefore(
    callout({ tone: "info", text: "The " + what + " in that link isn't in this window. It may need a longer window, or it no longer applies.", class: "inbox-missing" }),
    detail.firstChild
  );
}

// ======================================================================
// Actions, Recommendations
// ======================================================================

var recFilters = { show: "todo", severity: "all", area: "all" };

export function renderRecommendations(panel) {
  clear(panel);
  viewIntro(panel, "actions/recommendations");
  var noticeContainer = el("div", { id: "recommendations-notice" });
  var container = el("div", { id: "recommendations-list" });
  panel.appendChild(noticeContainer);
  panel.appendChild(container);
  container.appendChild(loadingNode("Loading recommendations", "rows"));

  // v0.3: same "capture window open: provisional" notice the Settings
  // baseline panel shows (docs/api.md's /api/baseline
  // capture_status) -- a recommendation built while onboarding's
  // capture window is still running may change once it completes.
  fetchJson("/api/baseline").then(function (result) {
    var status = result.body && result.body.ok === true ? result.body.data.capture_status : null;
    if (status && status.started && !status.complete) {
      clear(noticeContainer);
      noticeContainer.appendChild(
        callout({
          tone: "info",
          title: "These may change.",
          text: "ClaudeGlass is still recording your first sessions, so the recommendations below may change once that finishes.",
        })
      );
    }
  });

  // The key asked for before the list arrives is kept until it does.
  var wanted = state.params.id || null;
  var box = null;
  onParams("actions/recommendations", function (params) {
    wanted = params.id || null;
    if (box && wanted) {
      var current = box.current();
      if (!current || (current.key !== wanted && !findGroup([current], wanted))) {
        if (!box.selectKey(wanted, { focus: true })) missingNote(box.detail, "recommendation");
      }
    }
  });

  var checksLoad = loadQuickActions();
  // Ignores are kept per profile: the detail says which.
  var profileLoad = fetchJson("/api/profiles").then(function (result) {
    var data = result.body && result.body.ok === true ? result.body.data : null;
    return data ? data.active_profile_name || null : null;
  });
  Promise.all([loadRecommendations(), loadReport()]).then(function (results) {
    // A newer render (the window changed) has replaced this one; drawing
    // it would select a stale item and rewrite the address.
    if (!container.isConnected) return;
    clear(container);
    var body = results[0].body;
    if (!body || body.ok !== true) {
      container.appendChild(errorNotice(body && body.error));
      return;
    }
    var recs = Array.isArray(body.data) ? body.data : [];
    var report = results[1].report || null;
    // Before any filter: a list you ignored all of still shows, with
    // the filter that brings them back.
    if (!recs.length) {
      container.appendChild(emptyState("Nothing to change in this window: no setting or habit stood out.", null, "Pick a longer window to check more sessions."));
      return;
    }
    var ctx = { report: report, facts: pricingFacts(report), checks: checksLoad, profile: profileLoad };
    var groups = groupRecommendations(recs).concat(
      groupRecommendations(recs, { ignored: true }).map(function (group) {
        group.ignored = true;
        return group;
      })
    );
    box = recommendationInbox(container, groups, ctx);
    var opened = wanted && box.selectKey(wanted, { focus: true, address: false });
    if (!opened) {
      var first = box.first();
      if (first) box.select(first, null, { address: !!wanted });
      if (wanted) missingNote(box.detail, "recommendation");
    }
  });
}

function recommendationInbox(container, allGroups, ctx) {
  var ignoredCount = allGroups.filter(function (g) {
    return g.ignored;
  }).length;
  // Nothing ignored any more: the list is the to-do list.
  if (!ignoredCount) recFilters.show = "todo";
  return inbox(container, {
    viewKey: "actions/recommendations",
    label: "Recommendations",
    items: allGroups,
    filters: recFilters,
    empty: function (filters) {
      var shown = allGroups.filter(function (g) {
        return !!g.ignored === (filters.show === "ignored");
      });
      if (shown.length) return "Nothing matches these filters.";
      return filters.show === "ignored"
        ? "Nothing ignored in this window."
        : "Nothing left to do: you've ignored every recommendation in this window. Ignored shows them.";
    },
    find: function (key) {
      return findGroup(allGroups, key);
    },
    matches: function (group, filters) {
      return (
        !!group.ignored === (filters.show === "ignored") &&
        (filters.severity === "all" || group.severity === filters.severity) &&
        (filters.area === "all" || group.area === filters.area)
      );
    },
    resetFilters: function (item) {
      recFilters.show = item && item.ignored ? "ignored" : "todo";
      recFilters.severity = "all";
      recFilters.area = "all";
    },
    drawFilters: function (host, filters, redraw) {
      if (ignoredCount) {
        host.appendChild(
          filterRow(
            "Show",
            [
              { value: "todo", label: "To do", count: allGroups.length - ignoredCount },
              { value: "ignored", label: "Ignored", count: ignoredCount },
            ],
            filters.show,
            function (value) {
              filters.show = value;
              filters.severity = "all";
              filters.area = "all";
              redraw();
            }
          )
        );
      }
      // The other filters count what the Show filter leaves.
      var groups = allGroups.filter(function (g) {
        return !!g.ignored === (filters.show === "ignored");
      });
      var severities = SEVERITY_ORDER.filter(function (s) {
        return groups.some(function (g) {
          return g.severity === s;
        });
      });
      var areas = AREAS.filter(function (area) {
        return groups.some(function (g) {
          return g.area === area.id;
        });
      });
      var count = function (test) {
        return groups.filter(test).length;
      };
      host.appendChild(
        filterRow(
          "Show by importance",
          [{ value: "all", label: "All", count: groups.length }].concat(
            severities.map(function (s) {
              return { value: s, label: SEVERITY_LABELS[s] || s, count: count(function (g) { return g.severity === s; }) };
            })
          ),
          filters.severity,
          function (value) {
            filters.severity = value;
            redraw();
          }
        )
      );
      if (areas.length > 1) {
        host.appendChild(
          filterRow(
            "Show by area",
            [{ value: "all", label: "Every area", count: groups.length }].concat(
              areas.map(function (area) {
                return { value: area.id, label: area.label, count: count(function (g) { return g.area === area.id; }) };
              })
            ),
            filters.area,
            function (value) {
              filters.area = value;
              redraw();
            }
          )
        );
      }
    },
    itemContent: function (group) {
      var agents = groupAgents(group);
      var meta = [SEVERITY_LABELS[group.severity] || group.severity, areaLabel(group.area)];
      if (agents.length > 1) meta.push(agents.length + " agent types");
      var nodes = [
        severityMark(group.severity),
        el("span", { class: "inbox-item-body" }, [
          el("span", { class: "inbox-item-title", text: groupTitle(group) }),
          el("span", { class: "inbox-item-meta", text: meta.join(" · ") }),
        ]),
      ];
      var saving = listSaving(group);
      if (saving) nodes[1].appendChild(el("span", { class: "inbox-item-saving", text: saving }));
      return nodes;
    },
    renderDetail: function (group, pane, memberKey, focusMember) {
      renderRecommendationDetail(pane, group, memberKey, focusMember, ctx);
    },
  });
}

// The saving a list row shows: the recommendation's own; for a group,
// the largest member's. The Overview's next best actions show the same.
export function listSaving(group) {
  var lead = group.members[0];
  if (!lead.estimated_saving) return "";
  return group.members.length > 1 ? "Largest: " + lead.estimated_saving : lead.estimated_saving;
}

// A setting's value in words: a switch as on or off, a list joined.
function valueText(value) {
  if (value === true) return "On";
  if (value === false) return "Off";
  if (Array.isArray(value)) return value.length ? value.join(", ") : "None";
  if (value === null || value === undefined || value === "") return "—";
  return modelNames(String(value));
}

// What a change sets: a model by the name the rest of the page uses,
// and a value left to your judgement as what to choose.
function changeValueText(change) {
  if (change.value === null || change.value === undefined || change.value === "") return change.suggested || "Your choice";
  if (change.key === "model") return modelName(String(change.value));
  return valueText(change.value);
}

// A saving in a table cell: the amount without "over the period in this
// report", which the column's header says once.
function shortSaving(text) {
  if (!text) return "—";
  return String(text).replace(/\s+(over|across) the (period|spawns) in this report\.?$/, "");
}

function copyPromptButton(prompt, agent) {
  return button("Copy", {
    variant: "quiet",
    icon: "prompt",
    class: "action-copy",
    label: "Copy prompt for " + agentName(agent),
    action: function () {
      copyToClipboard(prompt).then(function (ok) {
        toast(ok ? "Prompt copied. Paste it into Claude Code." : "Couldn't copy. Open the change below and copy it from there.", { tone: ok ? "success" : "warning" });
      });
    },
  });
}

// Every agent's change in one table: now, after, the saving and the
// prompt to copy. For a group, picking a row shows that agent's change.
function changesTable(group, focus, focusMember) {
  var rows = [];
  if (group.members.length > 1) {
    group.members.forEach(function (rec) {
      var change = (rec.changes || [])[0] || {};
      rows.push({ agent: rec.agent_type, change: change, saving: rec.estimated_saving, fix: (rec.fixes || [])[0], key: rec.key });
    });
  } else {
    (focus.changes || []).forEach(function (change) {
      var fix = (focus.fixes || []).filter(function (f) {
        return f.key === change.key && (f.agent || "") === (change.agent || "");
      })[0];
      rows.push({ agent: change.agent || "top-level", change: change, saving: change.saving, fix: fix, key: null });
    });
  }
  if (rows.length < 2) return null;
  // The same "now" for every agent is said once, above the table.
  var sameNow = rows.every(function (row) {
    return valueText(row.change.current) === valueText(rows[0].change.current);
  });
  // The column names the setting when every row changes the same one.
  var sameKey = rows.every(function (row) {
    return row.change.key && row.change.key === rows[0].change.key;
  });
  var afterLabel = sameKey ? "Set " + rows[0].change.key + " to" : "After";
  var spec = {
    id: "changes-" + group.id,
    caption: "The change for each agent",
    sortable: false,
    bar: -1,
    rows: rows,
    rowKey: function (row) {
      return row.agent;
    },
    columns: [
      { key: "agent", label: "Agent", nowrap: true, render: function (row) { return agentName(row.agent); } },
      { key: "after", label: afterLabel, nowrap: true, render: function (row) { return changeValueText(row.change); } },
      { key: "saving", label: "Saving in this window", render: function (row) { return shortSaving(row.saving); } },
      {
        key: "copy",
        label: "Prompt",
        nowrap: true,
        render: function (row) {
          return row.fix && row.fix.prompt ? copyPromptButton(row.fix.prompt, row.agent) : "—";
        },
      },
    ],
  };
  // Values that differ per agent sit under its name, which keeps the
  // table narrow enough for the detail pane at 1280px.
  if (!sameNow) {
    spec.columns[0] = {
      key: "agent",
      label: "Agent and now",
      render: function (row) {
        return el("span", { class: "cell-stack" }, [
          el("span", { class: "nowrap", text: agentName(row.agent) }),
          el("span", { class: "cell-sub", text: "Now: " + valueText(row.change.current) }),
        ]);
      },
    };
  }
  if (group.members.length > 1) {
    spec.rowAction = {
      label: function (row) {
        return "Show the change for " + agentName(row.agent);
      },
      run: function (row) {
        focusMember(row.key);
      },
    };
    spec.rowClass = function (row) {
      return row.key === focus.key ? "is-selected" : null;
    };
  }
  var grid = dataGrid(spec);
  if (!sameNow || rows[0].change.current === undefined) return grid;
  return el("div", null, [el("p", { class: "notes", text: "Now, for every agent: " + valueText(rows[0].change.current) + "." }), grid]);
}

function detailSection(title, nodes, cls) {
  return el("section", { class: "detail-section" + (cls ? " " + cls : "") }, [el("h3", { text: title })].concat(nodes));
}

function renderRecommendationDetail(pane, group, memberKey, focusMember, ctx) {
  var members = group.members;
  var focus =
    members.filter(function (rec) {
      return rec.key === memberKey;
    })[0] || members[0];
  var many = members.length > 1;
  var article = el("article", { class: "inbox-detail-body" });

  // The chip sits inside the heading, so a screen reader moving by
  // headings hears the severity ("Do this") before the title.
  article.appendChild(
    el("h2", { class: "rec-head", tabIndex: -1 }, [
      severityChip(group.severity),
      el("span", { class: "visually-hidden", text: ": " }),
      el("span", { text: groupTitle(group) }),
    ])
  );
  var facts = el("div", { class: "detail-facts" });
  var agents = groupAgents(group);
  if (agents.length === 1) facts.appendChild(chip("For " + agentName(agents[0])));
  else if (agents.length > 1) facts.appendChild(chip("For " + agents.length + " agent types", { tip: agents.map(agentName).join(", ") }));
  facts.appendChild(chip(areaLabel(group.area)));
  if (focus.scope && SCOPE_LABELS[focus.scope] && focus.scope !== "managed") facts.appendChild(chip("Changes " + SCOPE_LABELS[focus.scope]));
  article.appendChild(facts);

  // How this saves you money: what it costs now, what the change does
  // to the price, and the saving with how sure it is.
  var story = el("dl", { class: "story-list" });
  // Each glossary term is explained once in the detail: its first use.
  var seen = new Set();
  function storyRow(term, nodes) {
    story.appendChild(el("div", { class: "story-row" }, [el("dt", { text: term }), el("dd", null, nodes)]));
  }
  if (focus.why) storyRow("What it costs you now", [el("p", null, prose(focus.why, seen))]);
  var how = mechanismText(group, ctx.facts);
  if (how) storyRow("What the change does", [el("p", null, prose(how, seen))]);
  var savingNodes = [];
  if (focus.estimated_saving) {
    savingNodes.push(
      el("p", { class: "story-saving" }, [
        el("span", { text: (many ? "For " + agentName(focus.agent_type) + ": " : "") + focus.estimated_saving + " " }),
        // "At most" already says it's a ceiling; the chip would repeat it.
        /^at most\b/i.test(focus.estimated_saving) ? null : basisChip(savingBasis(focus)),
      ])
    );
    if (focus.saving_basis) savingNodes.push(el("p", { class: "story-basis" }, prose(focus.saving_basis, seen)));
  } else {
    savingNodes.push(el("p", { class: "story-basis", text: "Not worked out for this one: it depends on how you use it." }));
  }
  storyRow("What you could save", savingNodes);
  article.appendChild(detailSection("How this saves you money", [story], "detail-story"));

  // What to do: the action, every agent's change, and how to make it.
  var todo = [el("p", null, prose(focus.action, seen))];
  var table = changesTable(group, focus, focusMember);
  if (table) {
    if (many) todo.push(el("p", { class: "notes", text: "Pick an agent to see its change below." }));
    todo.push(table);
  }
  var fixes = focus.fixes || [];
  if (focus.scope === "managed") {
    todo.push(
      callout({
        tone: "info",
        text: "Your organisation's policy sets this, so you can't change it yourself. Raise it with your administrator.",
      })
    );
  } else if (focus.lever && !fixes.length) {
    todo.push(el("p", { text: "Setting to change: " + focus.lever + " (" + (SCOPE_LABELS[focus.scope] || focus.scope) + ")" }));
  }
  if (focus.scope !== "managed") {
    if (fixes.length === 1) todo.push(commandBlock(fixes[0], { heading: true }));
    else
      fixes.forEach(function (fix, i) {
        var block = renderFix(fix, true);
        if (i === 0) block.open = true;
        todo.push(block);
      });
  }
  article.appendChild(detailSection(many ? "What to do for " + agentName(focus.agent_type) : "What to do", todo, "detail-todo"));

  if (focus.evidence && focus.evidence.length) {
    article.appendChild(detailSection("The numbers behind this", [evidenceList(ctx.report, focus.evidence)], "detail-evidence"));
  }

  article.appendChild(
    detailSection("Was this useful?", [
      el("p", { class: "notes", text: "A rating only. It changes no setting, and it tells ClaudeGlass how well its advice fits your work." }),
      cardRating("recommendation", String(focus.key || focus.id || group.id)),
    ])
  );

  article.appendChild(ignoreSection(group, ctx));

  var related = el("div", { class: "detail-related" });
  article.appendChild(related);
  ctx.checks.then(function (result) {
    var body = result.body;
    var checks = body && body.ok === true && body.data ? body.data.checks || [] : [];
    var linked = checks.filter(function (check) {
      return (check.rule_ids || []).indexOf(group.id) !== -1;
    });
    if (!linked.length || !related.isConnected) return;
    var list = el("ul", { class: "related-list" });
    linked.forEach(function (check) {
      list.appendChild(
        el("li", null, [statusMark(check.status), pageLink("actions/checks", check.question, { id: check.id }), el("span", { class: "related-meta", text: " · " + statusLabel(check.status) })])
      );
    });
    related.appendChild(detailSection(linked.length === 1 ? "The check this answers" : "The checks this answers", [list]));
  });

  pane.appendChild(article);
}

// ======================================================================
// Ignoring a recommendation (ignores.py, POST /api/recommendations/ignore)
// ======================================================================

function dateText(iso) {
  var date = iso ? new Date(iso) : null;
  if (!date || isNaN(date.getTime())) return "an earlier date";
  return date.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

// Where an ignore applies: the project on screen, or every project.
function ignoreScopeText(everywhere) {
  return everywhere || !state.project ? "in every project" : "in " + projectName(state.project);
}

// Why a recommendation you ignored shows again: it now suggests
// something else. One clause per change whose value moved.
function shownAgainText(rec) {
  var before = rec.ignored_before;
  if (!before) return "";
  var clauses = [];
  (rec.changes || []).forEach(function (change) {
    var old = (before.changes || []).filter(function (c) {
      return c.key === change.key && (c.agent || null) === (change.agent || null);
    })[0];
    if (old && valueText(old.value) !== valueText(change.value)) {
      clauses.push("it now suggests " + valueText(change.value) + " (you ignored " + valueText(old.value) + ")");
    }
  });
  var when = dateText(before.ignored_at);
  if (!clauses.length) return "You ignored this on " + when + ". It shows again because it has changed since.";
  return "You ignored this on " + when + ". It shows again because " + clauses.join(", and ") + ".";
}

function ignoreSection(group, ctx) {
  var members = group.members;
  var ignored = !!group.ignored;
  var first = members[0];
  var everywhere = ignored && first.ignored_in === "all";
  var nodes = [];
  var note = el("p", { class: "notes" });
  nodes.push(note);
  var again = shownAgainText(
    members.filter(function (rec) {
      return rec.ignored_before;
    })[0] || {}
  );
  if (again) nodes.unshift(el("p", { text: again }));
  ctx.profile.then(function (profileName) {
    note.textContent = ignored
      ? "Ignored on " + dateText(first.ignored_at) + " " + ignoreScopeText(everywhere) + " while " + (profileName ? profileName + " was active" : "no profile was applied") +
        ". It shows again if it starts suggesting something else, or under another profile."
      : "Ignoring hides it " + ignoreScopeText(false) + " while " + (profileName ? profileName + " is active" : "no profile is applied") +
        ", until it suggests something else. The Overview's available saving still counts it.";
  });
  var label = ignored ? (everywhere && state.project ? "Stop ignoring in every project" : "Stop ignoring") : "Ignore this recommendation";
  var status = el("p", { class: "notes", role: "status" });
  nodes.push(
    button(label, {
      variant: ignored ? "primary" : null,
      action: function (event) {
        var pressed = event && event.currentTarget;
        if (pressed) pressed.disabled = true;
        status.textContent = "";
        var keys = members.map(function (rec) {
          return rec.key || rec.id;
        });
        postJson(withWindow("/api/recommendations/ignore"), { keys: keys, ignored: !ignored }).then(function (result) {
          if (!result.body || result.body.ok !== true) {
            if (pressed) pressed.disabled = false;
            status.textContent = "Couldn't save that: " + ((result.body && result.body.error && result.body.error.message) || "the dashboard didn't answer") + ".";
            return;
          }
          toast(ignored ? "Shown again in To do." : "Ignored. Ignored shows it.", { tone: "success" });
          // Every list built from the recommendations counts it
          // differently now: the badge, the Overview, search, checks.
          state.recommendationPromises = {};
          state.quickActionPromises = {};
          state.searchPromises = {};
          Object.keys(renderedViews).forEach(function (key) {
            delete renderedViews[key];
          });
          recFilters.show = "todo";
          goTo("actions/recommendations", { force: true, params: ignored ? { id: group.key } : {} });
        });
      },
    })
  );
  nodes.push(status);
  return detailSection(ignored ? "Ignored" : "Not for you?", nodes, "detail-ignore");
}

// ======================================================================
// Actions, Checks: one question per way of saving, answered for the window
// ======================================================================

var checkFilters = { status: "all" };

export function renderQuickActions(panel) {
  clear(panel);
  viewIntro(panel, "actions/checks");
  var container = el("div", { class: "checks-inbox" });
  panel.appendChild(container);
  container.appendChild(loadingNode("Loading the checks", "rows"));

  var wanted = state.params.id || null;
  var box = null;
  onParams("actions/checks", function (params) {
    wanted = params.id || null;
    if (box && wanted) {
      var current = box.current();
      if (!current || current.key !== wanted) {
        if (!box.selectKey(wanted, { focus: true })) missingNote(box.detail, "check");
      }
    }
  });

  var recsLoad = loadRecommendations();
  loadQuickActions().then(function (result) {
    if (!container.isConnected) return;
    clear(container);
    var body = result.body;
    if (!body || body.ok !== true) {
      container.appendChild(errorNotice(body && body.error));
      return;
    }
    var checks = (body.data && body.data.checks) || [];
    if (!checks.length) {
      container.appendChild(emptyState("No checks to show for this window.", null, "Pick a longer window to include more sessions."));
      return;
    }
    var items = checks
      .map(function (check, i) {
        return { check: check, i: i };
      })
      .sort(function (a, b) {
        return CHECK_STATUS_ORDER.indexOf(a.check.status) - CHECK_STATUS_ORDER.indexOf(b.check.status) || a.i - b.i;
      })
      .map(function (entry) {
        return Object.assign({ key: entry.check.id }, entry.check);
      });
    var ctx = { period: body.data.period || "", recs: recsLoad };
    box = checksInbox(container, items, ctx);
    var opened = wanted && box.selectKey(wanted, { focus: true, address: false });
    if (!opened) {
      var first = box.first();
      if (first) box.select(first, null, { address: !!wanted });
      if (wanted) missingNote(box.detail, "check");
    }
  });
}

function checksInbox(container, items, ctx) {
  return inbox(container, {
    viewKey: "actions/checks",
    label: "Checks",
    items: items,
    filters: checkFilters,
    empty: "No check has this answer in this window.",
    find: function (key) {
      return (
        items.filter(function (item) {
          return item.key === key;
        })[0] || null
      );
    },
    matches: function (item, filters) {
      return filters.status === "all" || item.status === filters.status;
    },
    resetFilters: function () {
      checkFilters.status = "all";
    },
    drawFilters: function (host, filters, redraw) {
      var statuses = CHECK_STATUS_ORDER.filter(function (status) {
        return items.some(function (item) {
          return item.status === status;
        });
      });
      host.appendChild(
        filterRow(
          "Show by answer",
          [{ value: "all", label: "All", count: items.length }].concat(
            statuses.map(function (status) {
              return {
                value: status,
                label: statusLabel(status),
                count: items.filter(function (item) {
                  return item.status === status;
                }).length,
              };
            })
          ),
          filters.status,
          function (value) {
            filters.status = value;
            redraw();
          }
        )
      );
    },
    itemContent: function (item) {
      return [
        statusMark(item.status),
        el("span", { class: "inbox-item-body" }, [
          el("span", { class: "inbox-item-title", text: item.question }),
          el("span", { class: "inbox-item-meta", text: statusLabel(item.status) }),
        ]),
      ];
    },
    renderDetail: function (item, pane) {
      renderCheckDetail(pane, item, ctx);
    },
  });
}

function renderCheckDetail(pane, check, ctx) {
  var article = el("article", { class: "inbox-detail-body" });
  article.appendChild(el("h2", { class: "check-head", tabIndex: -1, text: check.question }));
  var facts = el("div", { class: "detail-facts" }, [statusBadge(check.status)]);
  if (ctx.period) facts.appendChild(chip("Answered " + ctx.period));
  article.appendChild(facts);
  // Each glossary term is explained once in the detail: its first use.
  var seen = new Set();
  article.appendChild(el("p", { class: "detail-why" }, prose(check.why, seen)));
  if (check.status === "no_data") {
    article.appendChild(emptyState(check.summary));
  } else {
    article.appendChild(el("p", { class: "check-summary" }, prose(check.summary, seen)));
    var more = el("div", { class: "check-more" });
    article.appendChild(more);
    loadInto(
      more,
      withWindow("/api/quick-actions/" + encodeURIComponent(check.id)),
      function (data, container) {
        renderCheckEvidence(data, container, seen);
      },
      { skeleton: "rows" }
    );
  }

  var related = el("div", { class: "detail-related" });
  article.appendChild(related);
  ctx.recs.then(function (result) {
    var body = result.body;
    var recs = body && body.ok === true && Array.isArray(body.data) ? body.data : [];
    var groups = groupRecommendations(recs).filter(function (group) {
      return (check.rule_ids || []).indexOf(group.id) !== -1;
    });
    if (!groups.length || !related.isConnected) return;
    var list = el("ul", { class: "related-list" });
    groups.forEach(function (group) {
      list.appendChild(
        el("li", null, [
          severityMark(group.severity),
          pageLink("actions/recommendations", groupTitle(group), { id: group.key }),
          el("span", { class: "related-meta", text: " · " + (SEVERITY_LABELS[group.severity] || group.severity) }),
        ])
      );
    });
    related.appendChild(detailSection(groups.length === 1 ? "The recommendation it leads to" : "The recommendations it leads to", [list]));
  });

  pane.appendChild(article);
}

function renderCheckEvidence(data, container, seen) {
  if (data.table && data.table.rows && data.table.rows.length) {
    container.appendChild(detailSection("The numbers", [simpleTable(data.table.columns, data.table.rows)]));
  }
  if (data.fixes && data.fixes.length) {
    var fixes = [
      el("p", {
        class: "notes",
        text: "Each fix is a prompt for Claude, which shows you the change before saving it. Where it applies, there is also a command that previews the change with --dry-run. Nothing here changes Claude Code by itself.",
      }),
    ];
    data.fixes.forEach(function (fix, i) {
      var block = renderFix(fix, data.fixes.length > 1);
      if (i === 0 && data.fixes.length > 1) block.open = true;
      fixes.push(block);
    });
    container.appendChild(detailSection(data.fixes.length === 1 ? "The fix" : "The fixes", fixes));
  }
  if (data.tips && data.tips.length) {
    var tips = el("div");
    renderTips(data.tips, tips, "h3", seen);
    container.appendChild(el("section", { class: "detail-section" }, Array.prototype.slice.call(tips.childNodes)));
  }
}

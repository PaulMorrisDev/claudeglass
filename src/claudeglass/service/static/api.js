/* claudeglass service UI: api.js
 *
 * Fetching from the service: the {ok, data} envelope, the window and
 * project every session-reading route is asked for, the report cache
 * and the figures-as-of stamp.
 */

import { clear, state } from "./core.js";
import { readableAmounts, setKnownProjects } from "./format.js";
import { errorNotice, loadingNode } from "./ui.js";

// -- fetch / envelope handling ----------------------------------------

// quiet: the response isn't drawn as figures (the project picker's list
// of projects), so its X-Figures-As-Of isn't noted.
export function fetchJson(url, options, quiet) {
  return fetch(url, options)
    .then(function (res) {
      return res
        .json()
        .catch(function () {
          return { ok: false, error: { code: "bad_response", message: "response was not valid JSON (HTTP " + res.status + ")" } };
        })
        .then(function (body) {
          // A report-backed route says when its figures are from
          // (docs/api.md, "Caching").
          var asOf = res.headers.get("X-Figures-As-Of");
          if (asOf && !quiet) noteFiguresAsOf(asOf);
          noteConnection(true);
          return { httpStatus: res.status, body: readableAmounts(body), asOf: asOf };
        });
    })
    .catch(function (err) {
      noteConnection(false);
      return { httpStatus: 0, body: { ok: false, error: { code: "network_error", message: String(err && err.message ? err.message : err) } } };
    });
}

// Whether the local service answers (docs/ui.md, "Service unreachable").
// shell.js sets notify: it says so under the page header, retries, and
// on reconnecting runs the loads that failed meanwhile (retryOnReconnect).
// reportFailed: a report load failed while it was gone, so the views
// drawn from it need drawing again, not just their own loads.
export var connection = { up: true, notify: null, retries: [], reportFailed: false };

function noteConnection(up) {
  if (connection.up === up) return;
  connection.up = up;
  if (connection.notify) connection.notify(up);
}

// A load that failed because the service was gone runs again once it's
// back, if what it draws into is still on the page.
export function retryOnReconnect(retry) {
  connection.retries.push(retry);
}

export function runReconnectRetries() {
  var retries = connection.retries;
  connection.retries = [];
  connection.reportFailed = false;
  retries.forEach(function (retry) {
    retry();
  });
}

/**
 * Fetch `url` and draw it into `container`: `render(data, container)` on
 * `ok: true`, or an error callout with a Try again button on `ok: false`
 * or a network failure. Never throws -- this is the one place most
 * views' data flow funnels through, per docs/ui.md's "or shows the
 * error.message inline... never a raw stack trace" contract.
 *
 * Empty, the container shows a skeleton while the answer is on its way
 * (options.skeleton picks its shape: "lines", "rows", "tiles"), under
 * what is loading in words (loadingLabel, or options.label). Already
 * drawn, it keeps what it shows, dimmed, until the answer lands; and if
 * the service has gone, it keeps it, marked stale, and tries again once
 * the service is back. options also passes through to fetch.
 */
// The words over a loading view's skeleton, by the route it reads.
var LOADING_LABELS = [
  ["/api/claude-md/", "Loading this file"],
  ["/api/claude-md", "Loading your CLAUDE.md files"],
  ["/api/skills", "Loading your skills"],
  ["/api/setup/status", "Checking your setup"],
  ["/api/setup", "Loading your setup"],
  ["/api/health", "Checking the service"],
  ["/api/diagnostics", "Loading the counters"],
  ["/api/impact", "Loading your changes"],
  ["/api/backtest", "Loading past estimates"],
  ["/api/config-diff", "Comparing your settings"],
  ["/api/baseline", "Loading your baseline"],
  ["/api/profiles/", "Comparing this profile with your settings"],
  ["/api/profiles", "Loading profiles"],
  ["/api/profile-goals?goal=", "Working out the settings"],
  ["/api/profile-goals", "Loading goals"],
  ["/api/quick-actions/", "Checking your sessions"],
  ["/api/ttl", "Loading the cache lifetimes"],
  ["/api/capture", "Loading capture settings"],
  ["/api/sessions", "Loading sessions"],
  ["/api/session/", "Explaining this session"],
  ["/api/compactions", "Loading summaries"],
  ["/api/carry", "Loading tool output"],
  ["/api/compaction-sim", "Replaying summaries"],
  ["/api/plan-handoff", "Loading approved plans"],
  ["/api/model-swap", "Loading model prices"],
  ["/api/waste", "Loading wasted replies"],
];

export function loadingLabel(url) {
  for (var i = 0; i < LOADING_LABELS.length; i++) {
    if (url.indexOf(LOADING_LABELS[i][0]) === 0) return LOADING_LABELS[i][1];
  }
  return "Loading";
}

export function loadInto(container, url, render, options) {
  var drawn = container.firstChild !== null && !container.querySelector(".loading, .callout-critical");
  if (drawn) {
    container.classList.add("is-refreshing");
    container.setAttribute("aria-busy", "true");
  } else {
    clear(container);
    container.appendChild(loadingNode((options && options.label) || loadingLabel(url), options && options.skeleton));
  }
  function retry() {
    if (container.isConnected) loadInto(container, url, render, options);
  }
  return fetchJson(url, options).then(function (result) {
    container.classList.remove("is-refreshing");
    container.removeAttribute("aria-busy");
    var body = result.body;
    if (!body || body.ok !== true) {
      var error = body && body.error;
      var offline = result.httpStatus === 0;
      if (offline) retryOnReconnect(retry);
      if (offline && drawn) {
        container.classList.add("is-stale");
        return null;
      }
      clear(container);
      container.appendChild(errorNotice(error, retry));
      return null;
    }
    clear(container);
    container.classList.remove("is-stale");
    try {
      render(body.data, container);
    } catch (err) {
      container.appendChild(errorNotice({ code: "render_error", message: String(err && err.message ? err.message : err) }));
    }
    return body.data;
  });
}

// The window as a query parameter, for every window-aware route.
function windowParam() {
  var value = state.window || "all";
  return /^[0-9]+$/.test(value) ? "window_days=" + value : "window=" + encodeURIComponent(value);
}

// The picked project as a query parameter, or "" for all projects
// (docs/api.md, "Filtering by project").
function projectParam() {
  return state.project ? "project=" + encodeURIComponent(state.project) : "";
}

function addParams(url, params) {
  var text = params
    .filter(function (param) {
      return param;
    })
    .join("&");
  if (!text) return url;
  return url + (url.indexOf("?") === -1 ? "?" : "&") + text;
}

// The window and the picked project, for every route that reads
// sessions: what the page header says is on screen.
export function withWindow(url) {
  return addParams(url, [windowParam(), projectParam()]);
}

// What the window and project decide, as one cache key.
export function scopeKey() {
  return state.window + (state.project ? "|" + state.project : "");
}

// How long a fetched report is served again. A window's start moves
// while a tab sits open (the newest change for "Since my last change",
// local midnight for a number of days), so a report is fetched afresh
// after this long, and when the browser's day turns over.
var REPORT_KEPT_MS = 5 * 60 * 1000;

// The browser's local day, as a key: a change of it expires the reports.
function browserDay() {
  var now = new Date(Date.now());
  return now.getFullYear() + "-" + (now.getMonth() + 1) + "-" + now.getDate();
}

// A kept report promise carries when and on which day it was fetched.
function reportKept(promise) {
  return Date.now() - promise.fetchedAt < REPORT_KEPT_MS && promise.fetchedDay === browserDay();
}

// The report for the window and the picked project. options.allProjects
// asks for every project's report instead: its meta.projects is the list
// the project picker offers (a report for one project lists only that
// one).
export function loadReport(options) {
  var allProjects = !!(options && options.allProjects && state.project);
  var key = allProjects ? state.window : scopeKey();
  if (state.reportPromises[key] && !reportKept(state.reportPromises[key])) delete state.reportPromises[key];
  if (state.reportPromises[key]) {
    // A report fetched earlier is drawn again: its figures' time counts.
    if (!allProjects) {
      state.reportPromises[key].then(function (loaded) {
        if (loaded && loaded.asOf) noteFiguresAsOf(loaded.asOf);
      });
    }
  } else {
    var url = allProjects ? "/api/report.json?" + windowParam() : withWindow("/api/report.json");
    var everyProject = allProjects || !state.project;
    var fetched = fetchJson(url, undefined, allProjects).then(function (result) {
      var body = result.body;
      if (!body || body.ok === false) {
        // Not kept: the next view that asks fetches it again (the
        // service may be back, or the failure passing). A newer fetch
        // that replaced this one stays.
        if (state.reportPromises[key] === fetched) delete state.reportPromises[key];
        if (result.httpStatus === 0) connection.reportFailed = true;
        return { error: (body && body.error) || { code: "error", message: "failed to load report" } };
      }
      var asOf = result.asOf;
      // docs/api.md: unlike every other route, /api/report.json is the
      // raw rendered document ({"schema_version": ..., "report": {...}}),
      // not the {"ok": true, "data": ...} envelope -- kept unwrapped for
      // byte parity with the CLI's own `report --json` output. Accept
      // both shapes here: `body.ok === true` is an enveloped response
      // (a possible future/alternate deployment), whose report lives at
      // `body.data.report`; anything else that reached this point (no
      // `ok` key, or `ok` truthy-but-not-boolean) is the real unwrapped
      // shape, whose report is `body.report` directly.
      var report = body.ok === true ? body.data && body.data.report : body.report;
      if (report && report.meta && report.meta.pricing && report.meta.pricing.currency) {
        state.currency = report.meta.pricing.currency;
      }
      if (report && report.meta && report.meta.units) {
        state.units = report.meta.units;
      }
      // Readable project names are worked out from every project, so a
      // report for one project (which lists only that one) leaves them.
      if (report && report.meta && report.meta.projects && everyProject) setKnownProjects(report.meta.projects);
      return { report: report, asOf: asOf };
    });
    fetched.fetchedAt = Date.now();
    fetched.fetchedDay = browserDay();
    state.reportPromises[key] = fetched;
  }
  return state.reportPromises[key];
}

// Every project with a session in the window, costliest first, for the
// project picker and search: resolves to a list of slugs, or null when
// the report couldn't load.
export function loadProjects() {
  return loadReport({ allProjects: true }).then(function (loaded) {
    var meta = loaded && loaded.report && loaded.report.meta;
    return meta && Array.isArray(meta.projects) ? meta.projects : null;
  });
}

// /api/recommendations for the window and project on screen, fetched
// once per window and project and shared by every view that reads it
// (the Actions badge and inbox, the Overview, the "Feeds N actions"
// chips). Built from the report, so kept as long as a report is
// (reportKept). Resolves to fetchJson's result; a failed fetch isn't
// kept, so the next caller asks again.
export function loadRecommendations() {
  var key = scopeKey();
  if (state.recommendationPromises[key] && !reportKept(state.recommendationPromises[key])) delete state.recommendationPromises[key];
  if (!state.recommendationPromises[key]) {
    var fetched = fetchJson(withWindow("/api/recommendations")).then(function (result) {
      var body = result.body;
      if ((!body || body.ok !== true || !Array.isArray(body.data)) && state.recommendationPromises[key] === fetched) delete state.recommendationPromises[key];
      return result;
    });
    fetched.fetchedAt = Date.now();
    fetched.fetchedDay = browserDay();
    state.recommendationPromises[key] = fetched;
  }
  return state.recommendationPromises[key];
}

// /api/quick-actions (the checks) for the window and project on screen,
// fetched once per window and project the same way: Actions › Checks,
// the recommendation detail and search share it. Kept as long as a
// report is; a failed fetch isn't kept.
export function loadQuickActions() {
  var key = scopeKey();
  if (state.quickActionPromises[key] && !reportKept(state.quickActionPromises[key])) delete state.quickActionPromises[key];
  if (!state.quickActionPromises[key]) {
    var fetched = fetchJson(withWindow("/api/quick-actions")).then(function (result) {
      var body = result.body;
      if ((!body || body.ok !== true) && state.quickActionPromises[key] === fetched) delete state.quickActionPromises[key];
      return result;
    });
    fetched.fetchedAt = Date.now();
    fetched.fetchedDay = browserDay();
    state.quickActionPromises[key] = fetched;
  }
  return state.quickActionPromises[key];
}

// Once the Overview has settled, fetch what Actions draws from while the
// page is idle, so Actions opens from the cache (docs/ui.md,
// "Performance"): requestIdleCallback where the browser has it, with a
// timeout so a busy page still gets there, and a short timer where it
// doesn't.
export function prefetchActions() {
  function fetchBoth() {
    loadRecommendations();
    loadQuickActions();
  }
  if (typeof window.requestIdleCallback === "function") window.requestIdleCallback(fetchBoth, { timeout: 2000 });
  else setTimeout(fetchBoth, 500);
}

// A rule that fires once per agent type sends one recommendation each;
// the inbox shows them as one item, titled for all of them, and so do
// the "Feeds N actions" lists.
var GROUP_TITLES = {
  "model-tier": function (n) {
    return n + " agent types could run a cheaper model";
  },
  "ttl-switch": function (n) {
    return "The cache lifetime (TTL) is a poor fit for " + n + " agent types";
  },
  "subagent-volume": function (n) {
    return n + " agent types take most of the subagent cost";
  },
  "agent-report-size": function (n) {
    return "Reports from " + n + " agent types come back large";
  },
  "spawn-cost": function (n) {
    return "Spawning " + n + " agent types is expensive before they do any work";
  },
  "spawn-claude-md": function (n) {
    return n + " agent types are sent your CLAUDE.md files every time they start";
  },
  "spawn-unused-skills": function (n) {
    return n + " agent types are given the skills list but never used a skill";
  },
  "spawn-unused-mcp": function (n) {
    return n + " agent types are offered MCP tools but never used one";
  },
  "spawn-read-only-tools": function (n) {
    return n + " agent types only ever searched and read files";
  },
  "spawn-task-prompt": function (n) {
    return "The instructions written for " + n + " agent types are long";
  },
};

// A group's title: its one member's own, or the shared wording for n.
export function groupedTitle(id, n, firstTitle) {
  if (n <= 1) return firstTitle;
  var phrase = GROUP_TITLES[id];
  return phrase ? phrase(n) : firstTitle + " (and " + (n - 1) + " more)";
}

// Which actions each report table is evidence for, from the
// recommendations' evidence ([label, value, "section.table", row_key]):
// byTable[table name] and byRow[table name + "\n" + row key] -> the
// actions, one per recommendation id (the inbox shows a shared id as
// one item). Report table names are unique across the report.
var indexed = { data: null, index: null };

export function actionIndex() {
  return loadRecommendations().then(function (result) {
    var body = result.body;
    var recs = body && body.ok === true && Array.isArray(body.data) ? body.data : [];
    if (indexed.data === recs) return indexed.index;
    var index = { byTable: {}, byRow: {} };
    // One entry per inbox item: recommendations for several agent types
    // that share an id and severity are one item there.
    function add(map, name, rec) {
      var list = map[name] || (map[name] = []);
      var slot = rec.agent_type ? rec.id + "|" + rec.severity : rec.key || rec.id;
      for (var i = 0; i < list.length; i++) {
        if (list[i].slot === slot) {
          list[i].members += 1;
          list[i].title = groupedTitle(rec.id, list[i].members, list[i].first);
          return;
        }
      }
      var title = rec.title || rec.id;
      list.push({ id: rec.id, key: rec.key || rec.id, title: title, first: title, slot: slot, members: 1 });
    }
    recs.forEach(function (rec) {
      // Ignored ones feed nothing on screen: only Actions' Ignored list.
      if (rec.ignored) return;
      (rec.evidence || []).forEach(function (item) {
        if (!Array.isArray(item) || !item[2]) return;
        var source = String(item[2]);
        var name = source.slice(source.indexOf(".") + 1);
        if (!name) return;
        add(index.byTable, name, rec);
        if (item[3] !== null && item[3] !== undefined && item[3] !== "") add(index.byRow, name + "\n" + String(item[3]), rec);
      });
    });
    indexed = { data: recs, index: index };
    return index;
  });
}

export function findSection(report, key) {
  if (!report || !Array.isArray(report.sections)) return null;
  for (var i = 0; i < report.sections.length; i++) {
    if (report.sections[i].key === key) return report.sections[i];
  }
  return null;
}

// The time the oldest figures drawn since the views were last dropped
// are from: the oldest X-Figures-As-Of a report-backed response has
// carried. Views keep what they drew and don't refetch on their own, so
// X-Figures-Refreshing isn't shown: "Redraw figures", a new window or
// a reload picks up the newer report.
// notify is set by shell.js, which redraws the sidebar's status line.
export var figures = { asOf: null, notify: null };

function noteFiguresAsOf(asOf) {
  if (figures.asOf && asOf >= figures.asOf) return;
  figures.asOf = asOf;
  if (figures.notify) figures.notify();
}

export function resetFiguresAsOf() {
  figures.asOf = null;
  if (figures.notify) figures.notify();
}

// A JSON POST to one of the service's write routes (docs/api.md),
// through the same envelope handling as every GET.
export function postJson(url, body) {
  return fetchJson(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

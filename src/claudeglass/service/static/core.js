/* claudeglass service UI: core.js
 *
 * Shared DOM helpers, browser storage, the dashboard's state and the
 * navigation hook (goTo) that lets a module link to another view
 * without importing app.js, and the hub that links a grid row to the
 * chart mark for the same thing (highlight), so grid.js and charts.js
 * never import each other.
 */

// -- tiny DOM helpers ------------------------------------------------

export function el(tag, attrs, children) {
  var node = document.createElement(tag);
  if (attrs) {
    Object.keys(attrs).forEach(function (key) {
      var value = attrs[key];
      if (value === null || value === undefined) return;
      if (key === "class") {
        node.className = value;
      } else if (key === "text") {
        node.textContent = value;
      } else if (key === "html") {
        // Only ever used with strings the calling module built itself from
        // escaped/known-safe fragments -- never with server data.
        node.innerHTML = value;
      } else if (key === "for") {
        // <label for="..."> is exposed as the `htmlFor` IDL property,
        // not `for` (a reserved word in the DOM API, not just JS).
        node.htmlFor = value;
      } else if (key === "role" || key.indexOf("data-") === 0 || key.indexOf("aria-") === 0) {
        node.setAttribute(key, value);
      } else {
        node[key] = value;
      }
    });
  }
  (children || []).forEach(function (child) {
    if (child === null || child === undefined) return;
    node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
  });
  return node;
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

// Used only where a value must go through innerHTML (the habit
// sparkline in charts-types.js) rather than textContent/setAttribute.
export function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

export function storageGet(key) {
  try {
    return window.localStorage.getItem(key);
  } catch (err) {
    return null;
  }
}

export function storageSet(key, value) {
  try {
    window.localStorage.setItem(key, value);
  } catch (err) {
    /* private window, blocked site data, or a full quota -- ignore */
  }
}

export function storageRemove(key) {
  try {
    window.localStorage.removeItem(key);
  } catch (err) {
    /* private window or blocked site data -- ignore */
  }
}

// -- the command that runs claudeglass here ---------------------

// The short claudeglass works only when pip's Scripts folder is on
// PATH, so the service writes the form that runs on this install into
// index.html (invocation.py). Server text arrives with it already in
// place; commands this code writes go through cli().
export var CLI_COMMAND = readCliCommand();

function readCliCommand() {
  var meta = typeof document !== "undefined" ? document.querySelector('meta[name="cg-command"]') : null;
  return (meta && meta.getAttribute("content")) || "claudeglass";
}

export function cli(rest) {
  return CLI_COMMAND + " " + rest;
}

// Fixed text that names a command (the glossary), in this install's form.
export function withCli(text) {
  if (CLI_COMMAND === "claudeglass") return text;
  // Not part of a longer name (my-claudeglass, .claudeglass), and not
  // the module in "python -m claudeglass", which server text already
  // has in this form: as invocation.py's pattern.
  return text.replace(/(^|[^\w.\/\\-])(?<!-m )claudeglass (?=[a-z])/g, function (_match, lead) {
    return lead + CLI_COMMAND + " ";
  });
}

// -- report.json cache (shared by every view that reads the report) ---

export var state = {
  // One report promise per window and project (review finding 21):
  // /api/report.json takes both and the server memoizes its answer per
  // window, project and change (docs/api.md). A single shared promise
  // kept every view on the first window's report after the picker
  // changed. Keyed by api.js's scopeKey().
  reportPromises: {},
  // The same for /api/recommendations (api.js's loadRecommendations).
  recommendationPromises: {},
  // And for /api/quick-actions, the checks (api.js's loadQuickActions).
  quickActionPromises: {},
  // Search's entries (palette.js's loadEntries), built from the three.
  searchPromises: {},
  currency: "USD",
  // UX-1: report.meta.units {mode, share_per_usd, period_label,
  // basis} (model.py's ReportMeta.units) -- the billing-mode facts
  // format.js's money()/moneyText() need to phrase an amount client-side.
  // null until the first report loads, same as currency defaulting
  // to "USD" until then.
  units: null,
  // The one window every view reads (the picker in the page header): a
  // number of days, or a named window the server resolves ("1h",
  // "today", "24h", "change", "all").
  window: "30",
  // The one project every view reads (the project picker), as its slug
  // from report.meta.projects (already redacted), or "" for all of them.
  // Kept in the address (&project=) and nowhere else: a filter that
  // outlived the visit would quietly shrink every figure next time.
  project: "",
  // The view on screen, as links.js's view key ("spend/usage").
  view: null,
  // The address's other parameters for that view (everything but w):
  // an item to select (id), a table and row to show (t, row).
  params: {},
};

// Short windows show a change's effect within the hour; "Since my
// last change" starts at the newest apply, undo or settings change.
// Last 7, 30 or 90 days are local calendar days: today and the days
// before it, from midnight in the service's zone. A window counts every
// session active in it, whole; "Since my last change" counts the
// sessions started after it.
export var WINDOW_OPTIONS = [
  { label: "Last hour", value: "1h" },
  { label: "Today", value: "today" },
  { label: "Last 24 hours", value: "24h" },
  { label: "Last 7 days", value: "7" },
  { label: "Last 30 days", value: "30" },
  { label: "Last 90 days", value: "90" },
  { label: "All time", value: "all" },
  { label: "Since my last change", value: "change" },
];

// View keys already drawn for the current window; a view not listed
// draws when next shown.
export var renderedViews = {};

// -- navigation hook -----------------------------------------------------
// app.js owns the router. Every other module that links to a view calls
// goTo, which app.js wires up at start, so no module has to import
// app.js and the module graph has no cycles.

var routeHandler = null;

export function setRouteHandler(handler) {
  routeHandler = handler;
}

// Show a view ("spend/usage", or a page id for its last-used segment).
// options: focus (move focus to the page title), force (draw again),
// params (the address's other parameters: {id}, or {t, row}).
export function goTo(viewKey, options) {
  if (routeHandler) routeHandler(viewKey, options);
}

// The project picker lives in app.js too. A module that offers "Show only
// this project" or "Show all projects" (search, an error notice) calls
// pickProject, with "" for all projects. options.focus moves focus to the
// page title, for a button the redraw takes away.
var projectHandler = null;

export function setProjectHandler(handler) {
  projectHandler = handler;
}

export function pickProject(slug, options) {
  if (projectHandler) projectHandler(slug || "", options || {});
}

// A view that takes parameters (an item to select) says how to follow
// them: app.js calls the handler each time the address changes, after
// the view is on screen. The view registers it each time it draws.
var paramsHandlers = {};

export function onParams(viewKey, handler) {
  paramsHandlers[viewKey] = handler;
}

export function paramsChanged(viewKey, params) {
  var handler = paramsHandlers[viewKey];
  if (handler) handler(params || {});
}

// -- linked highlight ------------------------------------------------------
// A grid row and a chart mark that mean the same thing light up together
// (charts.js, grid.js). highlight(scope, key) tells every listener; a
// null key clears. scope names what the keys are ("session",
// "agent-type"). A listener returns false once its chart or grid has
// left the page, and is dropped.

var highlightListeners = [];

export function highlight(scope, key) {
  var active = key === undefined ? null : key;
  highlightListeners = highlightListeners.filter(function (listener) {
    return listener(scope, active) !== false;
  });
}

export function listenHighlight(listener) {
  highlightListeners.push(listener);
  return function stop() {
    highlightListeners = highlightListeners.filter(function (other) {
      return other !== listener;
    });
  };
}

/* claudeglass service UI.
 *
 * The entry point: the shell (sidebar, page header, window picker,
 * theme), the #/ router and the renderer for each view. The other
 * first-party modules hold everything else, one per concern (core,
 * format, api, ui, grid, links, shell, icons) and one per page
 * (page-*.js).
 *
 * Vanilla ES2020 in native ES modules, no framework, no build step, no
 * external reference of any kind (see docs/ui.md's Constraints
 * section). index.html loads this file with
 * <script type="module" src="/static/app.js"> under the service's CSP
 * (`script-src 'self'`, so no inline handlers/scripts anywhere).
 *
 * Every route the dashboard talks to is one of docs/api.md's `/api/*`
 * routes, always same-origin, always the `{"ok": true, "data": ...}` /
 * `{"ok": false, "error": {...}}` envelope. `fetchJson` is the one
 * place that envelope is unwrapped; every renderer either gets
 * `data` or a rendered inline error notice -- never a raw stack trace,
 * never a blank view.
 *
 * No view holds state the server doesn't already have (docs/ui.md's Data
 * flow section): the address (#/page/segment?w=window&project=slug)
 * says what is on screen, and `localStorage` keeps only per-viewer
 * conveniences -- the last view, the chosen window, the theme, the
 * sidebar's width and a table's sort -- never data the server is the
 * source of truth for.
 * Reads/writes are wrapped in try/catch: a private window or blocked
 * site data must never break rendering.
 */

import { clear, el, paramsChanged, renderedViews, setProjectHandler, setRouteHandler, state, storageGet, storageRemove, storageSet, WINDOW_OPTIONS } from "./core.js";
import { icon } from "./icons.js";
import { fetchJson, loadProjects, loadRecommendations, resetFiguresAsOf, scopeKey } from "./api.js";
import { motionOK, toast } from "./ui.js";
import { projectName } from "./format.js";
import { pollHealth } from "./shell.js";
import { formatHash, OLD_TAB_VIEWS, PAGES, parseHash, scopeParams, VIEW_KEYS, viewFor } from "./links.js";
import { revealEvidence } from "./evidence.js";
import { setSectionChart } from "./grid.js";
import { sectionChart } from "./charts-types.js";
import { renderOverview } from "./page-overview.js";
import { renderChanges } from "./page-changes.js";
import { groupRecommendations, renderQuickActions, renderRecommendations } from "./page-actions.js";
import { renderSavings, renderSessions, renderUsage } from "./page-spend.js";
import { renderCache, renderTtl } from "./page-cache.js";
import { renderAgentHooks, renderAgentQuality, renderAgents, renderContextFiles } from "./page-agents.js";
import { renderHabits } from "./page-habits.js";
import { renderConfig, renderProfiles } from "./page-setup.js";
import { renderCapture } from "./page-capture.js";
import { renderDataQuality } from "./page-data.js";
import { renderCostCards, renderGlossary } from "./page-glossary.js";
import { initPalette } from "./palette.js";

// One renderer per view key (links.js's VIEW_KEYS, in sidebar order).
var VIEW_RENDERERS = {
  overview: renderOverview,
  changes: renderChanges,
  "actions/recommendations": renderRecommendations,
  "actions/checks": renderQuickActions,
  "spend/usage": renderUsage,
  "spend/savings": renderSavings,
  "spend/sessions": renderSessions,
  "cache/rebuilds": renderCache,
  "cache/lifetime": renderTtl,
  "agents/subagents": renderAgents,
  "agents/quality": renderAgentQuality,
  "agents/context": renderContextFiles,
  "agents/hooks": renderAgentHooks,
  habits: renderHabits,
  "setup/settings": renderConfig,
  "setup/profiles": renderProfiles,
  "setup/capture": renderCapture,
  data: renderDataQuality,
  "glossary/terms": renderGlossary,
  "glossary/how-costs-work": renderCostCards,
};

// ======================================================================
// The router: #/<page>[/<segment>][?w=<window>&project=<slug>&id=<item>&t=<table>&row=<row>]
// ======================================================================

var router = {
  // The view on screen, each page's last-used segment, and each view's
  // scroll position, restored on Back and Forward.
  current: null,
  lastSegment: {},
  scroll: {},
  // Set by goTo for the hashchange it causes: a new visit starts at the
  // top; a change nobody asked for here (Back, Forward, a typed address)
  // returns to where the view was left.
  pending: null,
};

// How a view relates to the window and project: "follow" (the default),
// "fixed" (its figures don't change with the window; the header says
// so) or "none" (no figures at all, so nothing is said). Only a view
// that follows them offers the window and project pickers.
function windowMode(view) {
  var flag = view.segment && view.segment.window !== undefined ? view.segment.window : view.page.window;
  if (flag === false) return view.page.segments ? "fixed" : "none";
  return "follow";
}

function knownWindow(value) {
  return WINDOW_OPTIONS.some(function (opt) {
    return opt.value === value;
  });
}

// The view key for a page and an optional segment: a page with segments
// and none named opens the one last used there, or its first.
function routeKey(page, segment) {
  if (!page.segments) return page.id;
  var segmentId = segment ? segment.id : router.lastSegment[page.id] || page.segments[0].id;
  return page.id + "/" + segmentId;
}

// The view to open when the address names none: the last one shown, or
// the view the old tab bar last had selected (moved over once).
function startingView() {
  var saved = storageGet("tls:view");
  if (saved && viewFor(saved)) return saved;
  var oldTab = storageGet("tls:activeTab");
  storageRemove("tls:activeTab");
  if (oldTab && OLD_TAB_VIEWS[oldTab]) return OLD_TAB_VIEWS[oldTab];
  return "overview";
}

// Read the address and show what it names. An address that is not a
// route, or leaves out the segment or window, is rewritten in place (no
// extra history entry) to the full form first. The project comes only
// from the address: one without it shows every project.
function resolveRoute() {
  var route = parseHash(window.location.hash);
  var key = route ? routeKey(route.page, route.segment) : startingView();
  var params = route ? route.params : {};
  // Both at once, so Back from one window and project to another never
  // asks for the new window with the old project.
  var windowValue = params.w && knownWindow(params.w) ? params.w : state.window;
  var project = params.project || "";
  if (windowValue !== state.window || project !== state.project) applyScope(windowValue, project);
  Object.assign(params, scopeParams());
  var hash = formatHash(key, params);
  if (window.location.hash !== hash) window.history.replaceState(null, "", hash);
  var extra = Object.assign({}, params);
  delete extra.w;
  delete extra.project;
  state.params = extra;

  var pending = router.pending;
  router.pending = null;
  var asked = pending !== null && pending.key === key;
  // An evidence link (t, row) scrolls to its row itself.
  var target = extra.t ? "keep" : null;
  var options = {
    force: asked && pending.options.force,
    focus: asked && pending.options.focus,
    scroll: target || (asked ? (key === router.current ? "keep" : "top") : "restore"),
  };
  changeView(key, function () {
    showView(key, options);
    if (extra.t) revealEvidence(viewPanel(key), extra.t, extra.row);
    paramsChanged(key, extra);
  });
}

// A new view fades in over the old one (docs/ui.md, "Motion"): a View
// Transition, where the browser has them, with the timing in app.css.
// Only for a real change of view, with motion welcome and no dialog
// open (the new view would cover it while it fades in). The sidebar,
// the page header and the toasts are named while it runs, so they
// change at once instead of fading with the view. Otherwise the change
// is made at once.
var viewChanges = 0;
var liveTransition = null;

function changeView(key, change) {
  var moving =
    router.current !== null &&
    key !== router.current &&
    typeof document.startViewTransition === "function" &&
    motionOK() &&
    !document.hidden &&
    !document.querySelector("dialog[open]");
  if (!moving) {
    change();
    return;
  }
  var root = document.documentElement;
  var views = document.getElementById("views");
  var top = views.getBoundingClientRect().top;
  viewChanges += 1;
  root.classList.add("view-changing");
  var transition = document.startViewTransition(function () {
    change();
    // The old view stays where it was on screen, however far the page
    // scrolls for the new one.
    root.style.setProperty("--view-shift", Math.round(top - views.getBoundingClientRect().top) + "px");
  });
  liveTransition = transition;
  function settled() {
    if (liveTransition === transition) liveTransition = null;
    viewChanges -= 1;
    if (viewChanges > 0) return;
    root.classList.remove("view-changing");
    root.style.removeProperty("--view-shift");
  }
  // A transition cut short (the next view was asked for first) has still
  // made its change.
  transition.ready.catch(function () {});
  transition.finished.then(settled, settled);
}

// While a View Transition runs, Chromium hit-tests only the page root
// (CSS can't change that), so a click in those 220ms (the next page, a
// segment, Copy prompt on the arriving view) would land nowhere. It
// ends the transition instead and goes to what is under the pointer.
function passClickThrough(event) {
  var transition = liveTransition;
  if (!transition || event.target !== document.documentElement) return;
  var x = event.clientX;
  var y = event.clientY;
  transition.skipTransition();
  transition.finished.then(function () {
    var target = document.elementFromPoint(x, y);
    if (!target || target === document.documentElement) return;
    target.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window, clientX: x, clientY: y }));
  });
}

document.addEventListener("click", passClickThrough);

// Every in-app navigation comes through here (links.js's pageLink, the
// sidebar, the segments): it adds one history entry, and resolveRoute
// does the rest when the hash changes. A page id alone opens that
// page's last-used segment. options.params go in the address too: an
// item to select (id) or a table row to show (t, row). They go over the
// picked window and project, as pageLink's own address does, so a link
// can name another window ({ w: "all" }) and the click lands where the
// address says.
function goTo(target, options) {
  options = options || {};
  var route = parseHash("#/" + String(target));
  if (!route) return;
  var key = routeKey(route.page, route.segment);
  if (key === router.current && !options.force && !options.params) {
    if (options.focus) focusTitle();
    return;
  }
  router.pending = { key: key, options: options };
  var hash = formatHash(key, Object.assign({}, scopeParams(), options.params || {}));
  if (window.location.hash === hash) resolveRoute();
  else window.location.hash = hash;
}

function viewPanel(key) {
  var id = "view-" + key.replace("/", "-");
  var panel = document.getElementById(id);
  if (!panel) {
    panel = el("section", { class: "view", id: id, "data-view": key, "aria-labelledby": "page-title", hidden: true });
    document.getElementById("views").appendChild(panel);
  }
  return panel;
}

function showView(key, options) {
  var view = viewFor(key);
  if (!view) return;
  var previous = router.current;
  if (previous && previous !== key) router.scroll[previous] = window.scrollY;
  router.current = key;
  state.view = key;
  if (view.segment) router.lastSegment[view.page.id] = view.segment.id;
  storageSet("tls:view", key);

  updateSidebar(view);
  updateHeader(view);
  document.title = (view.segment ? view.segment.label + " \u00b7 " : "") + view.page.label + " \u00b7 claudeglass";

  var panel = viewPanel(key);
  Array.prototype.forEach.call(document.getElementById("views").children, function (child) {
    child.hidden = child !== panel;
  });
  if (!renderedViews[key] || options.force) {
    renderedViews[key] = true;
    VIEW_RENDERERS[key](panel);
  }
  // A redraw asked for here (Redraw figures, a new window) may change
  // the recommendations too.
  if (options.force) refreshActionsBadge(true);

  // A new view jumps into place: the page's smooth scrolling is for
  // moves within a view, not between them.
  if (options.scroll === "top") window.scrollTo({ top: 0, behavior: "instant" });
  else if (options.scroll === "restore") window.scrollTo({ top: router.scroll[key] || 0, behavior: "instant" });
  if (options.focus) focusTitle();
}

function focusTitle() {
  var title = document.getElementById("page-title");
  if (title) title.focus({ preventScroll: true });
}

// ======================================================================
// Sidebar: the pages, the Actions count and the rail
// ======================================================================

// A plain left click stays in the app; a modified or middle click keeps
// the browser's own meaning (a new tab or window).
function inAppClick(event) {
  return event.button === 0 && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey;
}

function buildSidebar() {
  var main = document.getElementById("nav-main");
  var foot = document.getElementById("nav-foot");
  var detailsHeaded = false;
  PAGES.forEach(function (page) {
    // The evidence pages sit under a Details heading (a rule in the rail).
    if (page.group === "details" && !detailsHeaded) {
      detailsHeaded = true;
      main.appendChild(el("li", { class: "nav-group", role: "presentation" }, [el("span", { class: "nav-group-label", text: "Details" })]));
    }
    var link = el("a", { class: "nav-item", href: formatHash(page.id, {}), "data-page": page.id, "data-tip": page.label }, [
      icon(page.icon),
      el("span", { class: "nav-label", text: page.label }),
    ]);
    if (page.id === "actions") {
      link.appendChild(el("span", { class: "nav-badge", id: "actions-badge", hidden: true }));
    }
    link.addEventListener("click", function (event) {
      if (!inAppClick(event)) return;
      event.preventDefault();
      goTo(page.id);
    });
    (page.foot ? foot : main).appendChild(el("li", null, [link]));
  });

  var brand = document.getElementById("brand");
  brand.addEventListener("click", function (event) {
    if (!inAppClick(event)) return;
    event.preventDefault();
    goTo("overview");
  });

  // The rail: icons only, with each page's name as a tooltip. Chosen
  // with the toggle (tls:sidebar), and always on below 1024px wide.
  var app = document.getElementById("app");
  var toggle = document.getElementById("sidebar-toggle");
  var narrow = window.matchMedia("(max-width: 1023px)");
  toggle.appendChild(icon("sidebar"));
  function drawRail() {
    var rail = narrow.matches || storageGet("tls:sidebar") === "rail";
    app.setAttribute("data-sidebar", rail ? "rail" : "full");
    toggle.hidden = narrow.matches;
    toggle.setAttribute("aria-expanded", rail ? "false" : "true");
    toggle.setAttribute("aria-label", rail ? "Show page names" : "Hide page names");
    toggle.setAttribute("data-tip", rail ? "Show page names" : "Hide page names");
  }
  toggle.addEventListener("click", function () {
    storageSet("tls:sidebar", app.getAttribute("data-sidebar") === "rail" ? "full" : "rail");
    drawRail();
  });
  narrow.addEventListener("change", drawRail);
  drawRail();
}

function updateSidebar(view) {
  Array.prototype.forEach.call(document.querySelectorAll(".nav-item"), function (link) {
    if (link.getAttribute("data-page") === view.page.id) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
}

// The number of "Do this" recommendations for the window and project,
// beside Actions, counted as the inbox lists them (one item for a rule
// that fires per agent type). Hidden when there are none or they can't
// be counted.
var badgeFor = null;

function refreshActionsBadge(again) {
  var scopeAsked = scopeKey();
  if (badgeFor === scopeAsked && !again) return;
  badgeFor = scopeAsked;
  loadRecommendations().then(function (result) {
    if (scopeKey() !== scopeAsked) return;
    var badge = document.getElementById("actions-badge");
    if (!badge) return;
    var body = result.body;
    var recs = body && body.ok === true && Array.isArray(body.data) ? body.data : [];
    var count = groupRecommendations(recs).filter(function (group) {
      return group.severity === "action";
    }).length;
    badge.textContent = "";
    badge.hidden = !count;
    if (!count) return;
    badge.appendChild(el("span", { "aria-hidden": "true", text: String(count) }));
    badge.appendChild(el("span", { class: "visually-hidden", text: ", " + count + (count === 1 ? " change" : " changes") + " to make" }));
  });
}

// ======================================================================
// Page header: title, segments, project, window, theme
// ======================================================================

function updateHeader(view) {
  document.getElementById("page-title").textContent = view.page.label;
  // The sidebar's links open with the window and project on screen, in
  // this tab or a new one.
  Array.prototype.forEach.call(document.querySelectorAll(".nav-item[data-page]"), function (link) {
    link.setAttribute("href", formatHash(link.getAttribute("data-page"), scopeParams()));
  });

  var nav = document.getElementById("segments");
  nav.textContent = "";
  nav.hidden = !view.page.segments;
  if (view.page.segments) {
    nav.setAttribute("aria-label", view.page.label + " sections");
    view.page.segments.forEach(function (segment) {
      var key = view.page.id + "/" + segment.id;
      var link = el("a", { class: "segment", href: formatHash(key, scopeParams()), text: segment.label });
      if (segment === view.segment) link.setAttribute("aria-current", "page");
      link.addEventListener("click", function (event) {
        if (!inAppClick(event)) return;
        event.preventDefault();
        goTo(key);
      });
      nav.appendChild(link);
    });
  }
  updateScopeControls(view);
}

// ======================================================================
// Header menus: the project and the window
// ======================================================================

// A header button that opens a menu of choices, one of them checked: a
// role=menu list of menuitemradio rows. Up, Down, Home and End move, a
// letter jumps to the next row starting with it, Enter or a click picks
// and Esc closes; focus goes back to the button. opts: id, icon, name
// (what a screen reader hears before the choice), note (a line under the
// rows), pick(value), open() (called as the menu opens).
function menuControl(host, opts) {
  var label = el("span", { class: "control-label" });
  var button = el(
    "button",
    {
      type: "button",
      class: "control-button",
      id: opts.id + "-button",
      "aria-haspopup": "menu",
      "aria-expanded": "false",
      "aria-controls": opts.id + "-menu",
    },
    [icon(opts.icon), el("span", { class: "visually-hidden", text: opts.name + ": " }), label, icon("chevron-down")]
  );
  var rows = el("div", { class: "menu-rows", role: "none" });
  var menu = el("div", { class: "menu", id: opts.id + "-menu", role: "menu", "aria-label": opts.name, hidden: true }, [rows]);
  if (opts.note) menu.appendChild(el("p", { class: "menu-note", text: opts.note }));
  host.appendChild(button);
  host.appendChild(menu);
  var checked = null;

  // The rows the keys move through: a line saying why there is nothing
  // to pick is read, not stopped on.
  function items() {
    return Array.prototype.slice.call(rows.querySelectorAll(".menu-item:not(.is-disabled)"));
  }
  function mark(value) {
    checked = value;
    Array.prototype.forEach.call(rows.querySelectorAll("[role=menuitemradio]"), function (item) {
      item.setAttribute("aria-checked", item.getAttribute("data-value") === value ? "true" : "false");
    });
  }
  function focusRow(value) {
    var target = null;
    items().forEach(function (item) {
      if (item.getAttribute("data-value") === value) target = item;
    });
    target = target || items()[0];
    if (target) target.focus();
  }
  function focusChecked() {
    focusRow(checked);
  }
  // list: [{value, label, detail, title}], and {label, disabled: true}
  // for a line saying why there is nothing to pick.
  function setRows(list) {
    // New rows while the menu is open (the list arrived): focus stays on
    // the row it was on.
    var hadFocus = !menu.hidden && menu.contains(document.activeElement);
    var focusedValue = hadFocus ? document.activeElement.getAttribute("data-value") : null;
    clear(rows);
    list.forEach(function (row) {
      if (row.disabled) {
        rows.appendChild(el("div", { class: "menu-item is-disabled", role: "menuitem", "aria-disabled": "true", text: row.label }));
        return;
      }
      var item = el("button", { type: "button", class: "menu-item", role: "menuitemradio", "data-value": row.value, tabIndex: -1, title: row.title || null }, [
        icon("check"),
        el("span", { class: "menu-item-label", text: row.label }),
        row.detail ? el("span", { class: "menu-item-detail", text: row.detail }) : null,
      ]);
      item.addEventListener("click", function () {
        close(true);
        opts.pick(row.value);
      });
      rows.appendChild(item);
    });
    mark(checked);
    if (hadFocus) focusRow(focusedValue === null ? checked : focusedValue);
  }
  function open() {
    if (opts.open) opts.open();
    menu.hidden = false;
    button.setAttribute("aria-expanded", "true");
    focusChecked();
  }
  function close(returnFocus) {
    if (menu.hidden) return;
    menu.hidden = true;
    button.setAttribute("aria-expanded", "false");
    if (returnFocus) button.focus();
  }
  button.addEventListener("click", function () {
    if (menu.hidden) open();
    else close(false);
  });
  button.addEventListener("keydown", function (event) {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      open();
    }
  });
  menu.addEventListener("keydown", function (event) {
    var list = items();
    var index = list.indexOf(document.activeElement);
    var next = null;
    if (!list.length) return;
    if (event.key === "ArrowDown") next = (index + 1) % list.length;
    else if (event.key === "ArrowUp") next = (index - 1 + list.length) % list.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = list.length - 1;
    else if (event.key === "Escape") {
      event.preventDefault();
      close(true);
      return;
    } else if (event.key === "Tab") {
      close(false);
      return;
    } else if (event.key.length === 1 && event.key !== " " && !event.ctrlKey && !event.metaKey && !event.altKey) {
      var letter = event.key.toLowerCase();
      for (var step = 1; step <= list.length; step++) {
        var at = (index + step) % list.length;
        if (list[at].textContent.trim().toLowerCase().indexOf(letter) === 0) {
          next = at;
          break;
        }
      }
    }
    if (next === null) return;
    event.preventDefault();
    list[next].focus();
  });
  document.addEventListener("pointerdown", function (event) {
    if (!host.contains(event.target)) close(false);
  });
  return { button: button, label: label, menu: menu, mark: mark, setRows: setRows };
}

var windowMenu = null;
var windowFixed = null;
var projectMenu = null;

function windowLabel(value) {
  for (var i = 0; i < WINDOW_OPTIONS.length; i++) {
    if (WINDOW_OPTIONS[i].value === value) return WINDOW_OPTIONS[i].label;
  }
  return value;
}

function updateScopeControls(view) {
  var follow = windowMode(view) === "follow";
  windowMenu.button.hidden = !follow;
  windowFixed.hidden = windowMode(view) !== "fixed";
  windowMenu.label.textContent = windowLabel(state.window);
  windowMenu.mark(state.window);
  projectMenu.button.hidden = !follow;
  drawProjectLabel();
}

// A new window: every view that follows the window draws again when next
// shown (setWindow redraws the one on screen).
function applyWindow(value) {
  applyScope(value, state.project);
}

// A new project ("" for all of them), the same way.
function applyProject(value) {
  applyScope(state.window, value);
}

// A new window, project or both, as one change. A project the service
// has already said it doesn't know gives way to every project at once.
function applyScope(windowValue, project) {
  if (project && checkedProjects[project] === "unknown") {
    project = "";
    unknownProjectToast();
  }
  var windowChanged = windowValue !== state.window;
  var projectChanged = project !== state.project;
  if (windowChanged) {
    state.window = windowValue;
    storageSet("tls:window", windowValue);
  }
  state.project = project;
  scopeChanged(windowChanged);
  if (!projectChanged || !project) return;
  // A project picked in the address is named from the full list, which
  // this fetches if nothing has yet.
  drawProjectRows();
  checkProject(project);
}

// An address can name a project the service doesn't know (an old
// bookmark, a folder since moved): every route answers 400 for it
// (docs/api.md, "Filtering by project"). Asked once per project, and the
// answer kept ("asking", "known" or "unknown"): an unknown one gives way
// to every project, now and whenever an address names it again, as an
// unknown window gives way to the one in use.
var checkedProjects = {};

function checkProject(value) {
  if (checkedProjects[value]) return;
  checkedProjects[value] = "asking";
  fetchJson("/api/sessions?limit=1&project=" + encodeURIComponent(value)).then(function (result) {
    var error = result.body && result.body.error;
    if (result.httpStatus !== 400 || !error || String(error.message).indexOf("'project'") === -1) {
      // Only a clear answer counts: a failed check is asked again.
      if (result.httpStatus === 0) delete checkedProjects[value];
      else checkedProjects[value] = "known";
      return;
    }
    checkedProjects[value] = "unknown";
    if (state.project !== value) return;
    setProject("");
    unknownProjectToast();
  });
}

function unknownProjectToast() {
  toast("ClaudeGlass has no project by the name in the address, so this shows every project.", { tone: "warning" });
}

// After a new window or project, the figures for it are fetched fresh
// and every view that follows them draws again when next shown. A new
// window refreshes its list of projects too (every project's report).
function scopeChanged(windowChanged) {
  delete state.reportPromises[scopeKey()];
  delete state.recommendationPromises[scopeKey()];
  delete state.quickActionPromises[scopeKey()];
  delete state.searchPromises[scopeKey()];
  if (windowChanged) delete state.reportPromises[state.window];
  resetFiguresAsOf();
  Object.keys(renderedViews).forEach(function (key) {
    var view = viewFor(key);
    if (!view || windowMode(view) === "follow") delete renderedViews[key];
  });
  refreshActionsBadge();
}

function setWindow(value) {
  if (value === state.window || !knownWindow(value)) return;
  applyWindow(value);
  redrawForScope();
}

// options.focus: the control that asked is redrawn away, so focus moves
// to the page title (the error notice's "Show all projects").
function setProject(value, options) {
  value = value || "";
  if (value === state.project) return;
  applyProject(value);
  redrawForScope();
  if (options && options.focus) focusTitle();
}

// The address and header say what the pickers now show, and the view on
// screen draws again if it follows them. What the view had open stays
// open if the new figures have it; a table row shown from an evidence
// link was a one-off. Neither picker adds a history entry. While a move
// to another view is under way (its address set, not yet shown), that
// address is rewritten with the new scope, over any window the link
// named, so resolveRoute keeps the pick when the move lands.
function redrawForScope() {
  if (router.pending) {
    var pending = router.pending;
    window.history.replaceState(null, "", formatHash(pending.key, Object.assign({}, pending.options.params || {}, scopeParams())));
    return;
  }
  var view = viewFor(router.current);
  var keep = {};
  if (state.params.id) keep.id = state.params.id;
  state.params = keep;
  window.history.replaceState(null, "", formatHash(router.current, Object.assign(scopeParams(), keep)));
  updateHeader(view);
  if (windowMode(view) === "follow") showView(router.current, { force: true, scroll: "keep" });
}

function initWindowPicker() {
  var saved = storageGet("tls:window");
  if (saved === null) {
    var legacy = storageGet("tls:overviewWindow");
    if (legacy !== null) saved = legacy || "all";
  }
  if (knownWindow(saved)) state.window = saved;

  var host = document.getElementById("window-picker");
  windowMenu = menuControl(host, {
    id: "window",
    icon: "clock",
    name: "Window",
    note: "Last 7, 30 or 90 days are today and the days before it, from midnight. A window counts every session with a reply in it, in full; Since my last change counts the sessions started after it.",
    pick: function (value) {
      setWindow(value);
    },
  });
  windowMenu.setRows(
    WINDOW_OPTIONS.map(function (opt) {
      return { value: opt.value, label: opt.label };
    })
  );
  // A fixed view (Setup > Capture, Glossary > Terms) reads the same
  // whatever the window. Its figures aren't all time, and the Glossary
  // has none, so the chip says only that.
  windowFixed = el("span", { class: "window-fixed", hidden: true }, [icon("clock"), el("span", { text: "Same for every window" })]);
  host.appendChild(windowFixed);
}

// The project picker: all projects, then every project with a session in
// the window, the most expensive first (report.meta.projects). The list
// is read when the menu opens; the last one seen shows meanwhile.
var projectLists = {};

function initProjectPicker() {
  projectMenu = menuControl(document.getElementById("project-picker"), {
    id: "project",
    icon: "folder",
    name: "Project",
    note: "Projects with a session in this window, the most expensive first. Your changes, settings and CLAUDE.md files follow the pick too. The latest baseline, whether your estimates came true, and the hook and statusline checks always cover every project. Profiles uses your newest settings from any project.",
    pick: function (value) {
      setProject(value);
    },
    open: drawProjectRows,
  });
  setProjectRows(undefined);
}

function drawProjectRows() {
  var windowAsked = state.window;
  setProjectRows(projectLists[windowAsked]);
  loadProjects().then(function (slugs) {
    if (slugs) projectLists[windowAsked] = slugs;
    if (state.window !== windowAsked) return;
    // The names read better once every project is known.
    drawProjectLabel();
    setProjectRows(slugs || projectLists[windowAsked] || null);
  });
}

// A long folder name keeps its start and its end, where two folders
// under one parent differ ("home-user-projec…-checkout-service-staging").
var MENU_NAME_CHARS = 42;

function menuName(slug) {
  var name = projectName(slug);
  if (name.length <= MENU_NAME_CHARS) return name;
  return name.slice(0, 16) + "…" + name.slice(17 - MENU_NAME_CHARS);
}

// slugs: the window's projects, undefined while they load, null when
// they couldn't.
function setProjectRows(slugs) {
  var rows = [{ value: "", label: "All projects" }];
  if (!slugs) {
    rows.push({ disabled: true, label: slugs === null ? "Couldn't load the projects. Open this menu again to retry." : "Loading the projects\u2026" });
  } else {
    // A project from the address with no sessions in this window stays
    // pickable, so the menu still shows what is picked.
    if (state.project && slugs.indexOf(state.project) === -1) {
      rows.push({ value: state.project, label: menuName(state.project), detail: "No sessions in this window", title: state.project });
    }
    slugs.forEach(function (slug) {
      rows.push({ value: slug, label: menuName(slug), title: slug });
    });
    if (!slugs.length) rows.push({ disabled: true, label: "No project has a session in this window." });
  }
  projectMenu.setRows(rows);
  projectMenu.mark(state.project);
}

// The button names the picked project (its full folder name on hover)
// and looks set while one is, so narrowed figures never go unnoticed.
function drawProjectLabel() {
  var button = projectMenu.button;
  projectMenu.label.textContent = state.project ? projectName(state.project) : "All projects";
  button.classList.toggle("is-filtered", !!state.project);
  if (state.project) button.setAttribute("title", state.project);
  else button.removeAttribute("title");
  projectMenu.mark(state.project);
}

// The theme: follow the system, or the viewer's pick (tls:theme, which
// theme-boot.js applies before the first paint on the next load).
var THEMES = [
  { value: "system", label: "Theme: same as the system", icon: "system" },
  { value: "light", label: "Theme: light", icon: "sun" },
  { value: "dark", label: "Theme: dark", icon: "moon" },
];

function themeIndex() {
  var value = document.documentElement.getAttribute("data-theme");
  return value === "light" ? 1 : value === "dark" ? 2 : 0;
}

function drawThemeToggle() {
  var button = document.getElementById("theme-toggle");
  var theme = THEMES[themeIndex()];
  var next = THEMES[(themeIndex() + 1) % THEMES.length];
  button.textContent = "";
  button.appendChild(icon(theme.icon));
  button.setAttribute("aria-label", theme.label + ". Switch to " + next.value);
  button.setAttribute("data-tip", theme.label);
}

// The toggle and search's theme commands both come here.
function setTheme(value) {
  document.documentElement.setAttribute("data-theme", value);
  if (value === "system") storageRemove("tls:theme");
  else storageSet("tls:theme", value);
  drawThemeToggle();
}

function initThemeToggle() {
  document.getElementById("theme-toggle").addEventListener("click", function () {
    setTheme(THEMES[(themeIndex() + 1) % THEMES.length].value);
  });
  drawThemeToggle();
}

// A hairline under the page header once content scrolls beneath it.
function initStickyHeader() {
  var header = document.getElementById("page-header");
  var sentinel = document.getElementById("header-sentinel");
  if (!("IntersectionObserver" in window)) return;
  new IntersectionObserver(function (entries) {
    header.classList.toggle("is-stuck", !entries[0].isIntersecting);
  }).observe(sentinel);
}

function init() {
  if ("scrollRestoration" in window.history) window.history.scrollRestoration = "manual";
  setRouteHandler(goTo);
  setProjectHandler(setProject);
  setSectionChart(sectionChart);
  buildSidebar();
  initWindowPicker();
  initProjectPicker();
  initThemeToggle();
  initPalette({ setWindow: setWindow, setTheme: setTheme });
  initStickyHeader();
  document.getElementById("skip-link").addEventListener("click", focusTitle);
  pollHealth();
  window.addEventListener("hashchange", resolveRoute);
  resolveRoute();
  refreshActionsBadge();
  // The shell answers from here (docs/ui.md, "Performance").
  performance.mark("cg-shell-ready");
}

// VIEW_KEYS and VIEW_RENDERERS must name the same views: a page added to
// links.js without a renderer would otherwise show an empty view.
VIEW_KEYS.forEach(function (key) {
  if (!VIEW_RENDERERS[key]) throw new Error("no renderer for view " + key);
});

document.addEventListener("DOMContentLoaded", init);

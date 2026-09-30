/* claudeglass service UI: ui.js
 *
 * The component library (docs/ui.md, "Components"): buttons, chips,
 * metric tiles, panels, callouts, empty states, skeletons, command
 * blocks, drawers, toasts, tooltips and popovers. Every page builds
 * from these, so a thing looks and behaves the same wherever it shows.
 */

import { clear, el, pickProject, state } from "./core.js";
import { icon } from "./icons.js";
import { cardLink, glossaryText, JARGON, linkText, plainText, termLink } from "./links.js";

// Reduced motion asked for (docs/ui.md, "Motion"): components that
// animate in JS check this; CSS has its own media query.
export function motionOK() {
  try {
    return !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  } catch (err) {
    return true;
  }
}

// A figure counting up to its value (docs/ui.md, "Motion"). The node
// already shows the final text, so a reader who never sees a frame
// (reduced motion, a hidden tab) reads the right figure. The count
// writes write(value) from `from` to `to` over 700ms (--dur-count),
// easing out, then puts the final text back. So does a timer, and the
// tab going out of sight, in case animation frames stop mid-count.
var COUNT_MS = 700;

export function countUp(node, from, to, write) {
  // A view out of sight (figures that landed after you left) shows the
  // final figure: a count nobody sees would be over by the time you look.
  if (!node || !motionOK() || document.hidden || !node.getClientRects().length || from === to || !isFinite(from) || !isFinite(to)) return;
  var final = node.textContent;
  var start = null;
  var done = false;
  function finish() {
    if (done) return;
    done = true;
    node.textContent = final;
    document.removeEventListener("visibilitychange", finish);
  }
  function step(now) {
    if (done) return;
    if (start === null) start = now;
    var t = (now - start) / COUNT_MS;
    if (t >= 1) {
      finish();
      return;
    }
    // Expo-out, the curve of --ease-out.
    node.textContent = write(from + (to - from) * (1 - Math.pow(2, -10 * t)));
    requestAnimationFrame(step);
  }
  node.textContent = write(from);
  document.addEventListener("visibilitychange", finish);
  setTimeout(finish, COUNT_MS + 100);
  requestAnimationFrame(step);
}

// Rows that arrive one after another (docs/ui.md, "Motion"): each fades
// in and rises 6px (app.css's .is-entering), 24ms after the one before,
// the first `delay` ms from now. Only the first 6 are staggered; any
// more arrive with the sixth. The rows are already in place, so this
// only eases them in, and nothing moves under reduced motion or out of
// sight (rows drawn on a view you have left arrive as they are).
export function enterInTurn(rows, delay) {
  if (!motionOK() || document.hidden || !rows.length || !rows[0].getClientRects().length) return;
  Array.prototype.forEach.call(rows, function (row, i) {
    function settle(event) {
      if (event.target !== row) return;
      row.classList.remove("is-entering");
      row.style.removeProperty("--enter-delay");
      row.removeEventListener("animationend", settle);
      row.removeEventListener("animationcancel", settle);
    }
    row.style.setProperty("--enter-delay", (delay || 0) + Math.min(i, 5) * 24 + "ms");
    row.classList.add("is-entering");
    row.addEventListener("animationend", settle);
    row.addEventListener("animationcancel", settle);
  });
}

// -- buttons -------------------------------------------------------------

// A button whose label says what happens ("Copy prompt", "Open session").
// Never "Apply": the dashboard offers prompts and dry-run commands, and
// never changes Claude Code's settings itself (docs/writing-help.md).
// opts: variant ("primary", "quiet", "link"), icon, title, action (the
// click handler), label (the accessible name, for an icon-only button).
export function button(label, opts) {
  opts = opts || {};
  if (/^\s*apply\b/i.test(label || "")) throw new Error("A button never says Apply: " + label);
  var variant = opts.variant ? " button-" + opts.variant : "";
  var node = el("button", {
    type: "button",
    class: "button" + variant + (opts.class ? " " + opts.class : ""),
    title: opts.title || null,
    "aria-label": opts.label || null,
  });
  if (opts.icon) node.appendChild(icon(opts.icon, { size: 14 }));
  if (label) node.appendChild(el("span", { class: "button-label", text: label }));
  if (opts.action) node.addEventListener("click", opts.action);
  return node;
}

// -- copying -------------------------------------------------------------

// UX-6/9: used to fire-and-forget navigator.clipboard.writeText and
// always flip the button to "Copied" regardless of what happened --
// an insecure context, a denied permission or any other rejection of
// the Promise it returns (not just a missing API, which the old
// try/catch did cover) left the button falsely claiming success.
// Returns a Promise<boolean> so the caller can tell the two apart.
export function copyToClipboard(text) {
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text).then(
        function () { return true; },
        function () { return false; }
      );
    }
  } catch (err) {
    /* clipboard unavailable (insecure context, permissions) -- fall through */
  }
  return Promise.resolve(false);
}

var COPY_FAILED = "Couldn't copy. Select the text and copy it yourself.";

// A block of text to paste somewhere, in the mono face, with a Copy
// button that says "Copied" only when the copy worked (and a toast says
// what went where). what: the kind of text ("Prompt", "Command");
// about: what it is for ("code-reviewer"). The button shows "Copy"
// and is named "Copy prompt for code-reviewer", so a page of them
// reads as a list of different things.
// onCopy, when given, runs after a successful copy.
export function codeBlockWithCopy(text, what, about, onCopy) {
  var wrap = el("div", { class: "code-block" });
  var pre = el("pre", { text: text || "" });
  var kind = what ? what.charAt(0).toLowerCase() + what.slice(1) : "text";
  var button = el("button", { type: "button", class: "copy-button", text: "Copy", "aria-label": "Copy " + kind + (about ? " for " + about : "") });
  button.addEventListener("click", function () {
    copyToClipboard(text || "").then(function (ok) {
      if (ok && onCopy) onCopy();
      button.textContent = ok ? "Copied" : "Couldn't copy";
      toast(ok ? (what || "Text") + " copied to the clipboard." : COPY_FAILED, { tone: ok ? "success" : "warning" });
      setTimeout(function () {
        button.textContent = "Copy";
      }, 1500);
    });
  });
  wrap.appendChild(pre);
  wrap.appendChild(button);
  return wrap;
}

// -- labels shared by several pages --------------------------------------

// Recommendation.agent_type values that are not agent names.
export var AGENT_LABELS = {
  "top-level": "Your main session",
  unknown: "Subagents with no recorded type",
  "workflow-subagent": "Workflow subagents",
};

// Recommendation.scope, in plain words.
export var SCOPE_LABELS = {
  user: "your user settings, every project",
  repo: "this project's settings or agent files",
  managed: "set by your organisation's policy",
};

// -- chips ---------------------------------------------------------------

// A small label: tone is neutral, accent, good, warn, serious or
// critical. Status tones always come with an icon and words, never the
// colour alone (WCAG 1.4.1).
export function chip(text, opts) {
  opts = opts || {};
  var node = el("span", { class: "chip chip-" + (opts.tone || "neutral") + (opts.class ? " " + opts.class : "") });
  if (opts.icon) node.appendChild(icon(opts.icon, { size: 12 }));
  node.appendChild(el("span", { text: text }));
  if (opts.tip) attachTooltip(node, opts.tip);
  return node;
}

// The small colour square that ties a legend entry or a grid row to its
// chart marks (charts.js's entity colours). opts.hatch: an estimate's
// hatched fill; opts.line: a line series; opts.glyph: a marker's own
// shape, as SVG markup the caller built from fixed values.
export function swatch(colour, opts) {
  opts = opts || {};
  var node = el("span", {
    class: "swatch" + (opts.hatch ? " is-hatched" : "") + (opts.line ? " is-line" : "") + (opts.glyph ? " is-glyph" : ""),
    "aria-hidden": "true",
    html: opts.glyph || null,
  });
  if (colour) node.style.setProperty("--swatch", colour);
  return node;
}

export var SEVERITY_ORDER = ["action", "advice", "info"];

export var SEVERITY_LABELS = { action: "Do this", advice: "Worth considering", info: "For your information" };
var SEVERITY_ICONS = { action: "critical", advice: "warning", info: "info" };

// A recommendation's severity as a chip: the icon and the label carry
// it, the tint only repeats them (WCAG 1.4.1).
export function severityChip(severity) {
  var chipNode = el("span", { class: "severity-badge severity-" + severity });
  chipNode.appendChild(icon(SEVERITY_ICONS[severity] || "info", { size: 14 }));
  chipNode.appendChild(el("span", { text: SEVERITY_LABELS[severity] || severity }));
  return chipNode;
}

// The severity's icon alone, in its colour, for a tight list whose
// rows also say the severity in words (the Actions inbox).
export function severityMark(severity) {
  var node = el("span", { class: "severity-mark severity-" + severity, "aria-hidden": "true" });
  node.appendChild(icon(SEVERITY_ICONS[severity] || "info", { size: 16 }));
  return node;
}

// A check's status (quick_actions.py): the order the Checks list uses,
// and each one's words.
export var CHECK_STATUS_ORDER = ["act", "ok", "no_data"];

var CHECK_STATUS = {
  // Amber like a "Worth considering" recommendation, not red like "Do
  // this": a check points at recommendations, and shouldn't sound more
  // urgent than they do.
  act: { label: "Worth a look", cls: "severity-advice", icon: "warning" },
  ok: { label: "Nothing to do", cls: "severity-good", icon: "success" },
  no_data: { label: "Not enough data", cls: "severity-info", icon: "info" },
};

export function statusLabel(status) {
  return (CHECK_STATUS[status] || { label: status }).label;
}

export function statusBadge(status) {
  var info = CHECK_STATUS[status] || { label: status, cls: "severity-info", icon: "info" };
  var badge = el("span", { class: "severity-badge " + info.cls });
  badge.appendChild(icon(info.icon, { size: 14 }));
  badge.appendChild(el("span", { text: info.label }));
  return badge;
}

// A check's status as its icon alone, like severityMark.
export function statusMark(status) {
  var info = CHECK_STATUS[status] || { cls: "severity-info", icon: "info" };
  var node = el("span", { class: "severity-mark " + info.cls, "aria-hidden": "true" });
  node.appendChild(icon(info.icon, { size: 16 }));
  return node;
}

// How sure a number is (docs/ui.md, "Basis"): the same words on tiles,
// charts, grids and prose. A measured number carries no chip.
export var BASIS = {
  measured: null,
  estimate: { label: "Estimate", tip: "Worked out from your sessions, not measured directly." },
  ceiling: { label: "At most", tip: "The most it could be. The real figure is likely lower." },
  simulated: { label: "Simulated", tip: "What replaying your sessions with the change gives." },
  calibrated: { label: "Calibrated", tip: "An estimate checked against what happened after earlier changes." },
};

export function basisChip(basis) {
  var info = BASIS[basis];
  if (!info) return null;
  return chip(info.label, { class: "basis-chip basis-" + basis, tip: info.tip });
}

// A multiple in words: "3.2 times", "12 times".
export function timesText(ratio) {
  var rounded = ratio >= 10 ? Math.round(ratio) : Math.round(ratio * 10) / 10;
  return rounded + " times";
}

// A change against the previous period of the same length: "+12%" with
// an arrow, coloured by whether that direction is good (opts.upIsGood),
// and neutral within 1%. opts.period names the comparison ("the 30 days
// before"). No chip without an earlier figure to compare with.
export function deltaChip(current, previous, opts) {
  opts = opts || {};
  var period = opts.period || "the period before";
  if (typeof current !== "number" || typeof previous !== "number" || !isFinite(current) || !isFinite(previous) || previous < 0) {
    return chip("No earlier period", { class: "delta-chip delta-none" });
  }
  if (previous === 0) {
    return chip(current > 0 ? "None before" : "None", { class: "delta-chip delta-none", tip: "Nothing in " + period + "." });
  }
  var change = ((current - previous) / previous) * 100;
  if (Math.abs(change) < 1) {
    return chip("About the same", { icon: "minus", class: "delta-chip delta-flat", tip: "Within 1% of " + period + "." });
  }
  var up = change > 0;
  var good = opts.upIsGood ? up : !up;
  var rounded = Math.abs(change) >= 10 ? Math.round(Math.abs(change)) : Math.round(Math.abs(change) * 10) / 10;
  // Three times or more reads better as a multiple than as "+412%".
  var times = current / previous >= 3 ? timesText(current / previous) : "";
  var node = el("span", { class: "chip delta-chip " + (good ? "delta-good" : "delta-bad") });
  node.appendChild(icon(up ? "arrow-up" : "arrow-down", { size: 12 }));
  node.appendChild(el("span", { text: times || (up ? "+" : "−") + rounded + "%" }));
  attachTooltip(node, times ? times + " as much as " + period + "." : (up ? "Up " : "Down ") + rounded + "% on " + period + ".");
  return node;
}

// -- metric tile -----------------------------------------------------------

// One headline number: its label, the value (already formatted, in the
// billing mode for money), how sure it is, the change on the period
// before, a line of context and, optionally, a sparkline and a link to
// the page it comes from. A tile is never a hero: 28px, not 40.
// opts: label, value (text or node), unit, basis, delta (node), hint,
// caption (a line saying what the value is of), note (a longer line of
// detail), spark (node), link (node), class.
export function tile(opts) {
  var node = el("div", { class: "metric-tile" + (opts.class ? " " + opts.class : "") });
  var head = el("div", { class: "metric-head" }, [el("span", { class: "metric-label", text: opts.label })]);
  var basis = opts.basis ? basisChip(opts.basis) : null;
  if (basis) head.appendChild(basis);
  node.appendChild(head);
  var value = el("div", { class: "metric-value" });
  if (typeof opts.value === "string" || typeof opts.value === "number") value.appendChild(el("span", { text: String(opts.value) }));
  else if (opts.value) value.appendChild(opts.value);
  if (opts.unit) value.appendChild(el("span", { class: "unit", text: " " + opts.unit }));
  node.appendChild(value);
  if (opts.delta || opts.hint) {
    var foot = el("div", { class: "metric-foot" });
    if (opts.delta) foot.appendChild(opts.delta);
    if (opts.hint) foot.appendChild(el("span", { class: "metric-hint", text: opts.hint }));
    node.appendChild(foot);
  }
  if (opts.caption) node.appendChild(el("p", { class: "metric-caption", text: opts.caption }));
  if (opts.note) node.appendChild(el("p", { class: "metric-note" }, prose(opts.note)));
  if (opts.spark) node.appendChild(opts.spark);
  if (opts.link) node.appendChild(el("div", { class: "metric-link" }, [opts.link]));
  return node;
}

// A row of tiles.
export function tileRow(tiles, opts) {
  return el("div", { class: "metric-tiles" + (opts && opts.class ? " " + opts.class : "") }, tiles);
}

// -- panel -------------------------------------------------------------------

// A surface for a chart, a grid or a detail: a header (title, one-line
// intro, a "How to read this" popover, chips) and a body. Panels never
// nest: a panel inside a panel draws no second border (app.css).
// opts: title, level (2 or 3), intro, help (model.Help), chips, body
// (nodes), class, id.
export function panel(opts) {
  var node = el("section", { class: "panel" + (opts.class ? " " + opts.class : ""), id: opts.id || null });
  if (opts.title || opts.intro || opts.help || (opts.chips && opts.chips.length)) {
    var head = el("header", { class: "panel-head" });
    var titleRow = el("div", { class: "panel-title-row" });
    if (opts.title) titleRow.appendChild(el(opts.level === 2 ? "h2" : "h3", { class: "panel-title", text: opts.title }));
    var help = opts.help ? helpButton(opts.help, opts.title) : null;
    if (help) titleRow.appendChild(help);
    (opts.chips || []).forEach(function (c) {
      if (c) titleRow.appendChild(c);
    });
    head.appendChild(titleRow);
    if (opts.intro) head.appendChild(el("p", { class: "panel-intro", text: opts.intro }));
    node.appendChild(head);
  }
  var body = el("div", { class: "panel-body" }, opts.body || []);
  node.appendChild(body);
  node.body = body;
  return node;
}

// -- callouts ------------------------------------------------------------------

var TONES = {
  info: { icon: "info", label: "Note" },
  success: { icon: "success", label: "Done" },
  warning: { icon: "warning", label: "Warning" },
  critical: { icon: "critical", label: "Problem" },
};

// A message that sits in the page's flow: an icon and its tone's name
// (read aloud), a tinted surface, the text, then any actions. No side
// stripe. opts: tone, title, text, children (nodes), actions (nodes),
// dismiss (a function; adds a Dismiss button), role, class.
export function callout(opts) {
  var tone = TONES[opts.tone] ? opts.tone : "info";
  var node = el("div", {
    class: "callout callout-" + tone + (opts.class ? " " + opts.class : ""),
    role: opts.role || (tone === "critical" ? "alert" : null),
  });
  node.appendChild(el("span", { class: "callout-icon" }, [icon(TONES[tone].icon, { size: 16 })]));
  var body = el("div", { class: "callout-body" });
  if (opts.title) {
    body.appendChild(
      el("p", { class: "callout-title" }, [
        el("span", { class: "visually-hidden", text: TONES[tone].label + ": " }),
        el("span", { text: opts.title }),
      ])
    );
  } else {
    body.appendChild(el("span", { class: "visually-hidden", text: TONES[tone].label + ": " }));
  }
  if (opts.text) body.appendChild(el("p", null, prose(opts.text)));
  (opts.children || []).forEach(function (child) {
    if (child) body.appendChild(child);
  });
  var actions = (opts.actions || []).slice();
  if (opts.dismiss) {
    actions.push(
      button("Dismiss", {
        variant: "quiet",
        action: function () {
          opts.dismiss(node);
        },
      })
    );
  }
  if (actions.length) body.appendChild(el("div", { class: "callout-actions" }, actions));
  node.appendChild(body);
  return node;
}

// A failed load, said plainly: what went wrong, the service's own
// message and code, and (given retry) a way to try again.
export function errorNotice(error, retry) {
  var code = (error && error.code) || "error";
  var message = (error && error.message) || "Something went wrong.";
  var offline = code === "network_error";
  // An address from a bookmark can name a project the service no longer
  // knows (docs/api.md: a 400 that names the project parameter).
  if (code === "bad_request" && state.project && message.indexOf("'project'") !== -1) {
    return callout({
      tone: "warning",
      title: "ClaudeGlass has no project by this name.",
      children: [
        el("p", {
          class: "callout-detail",
          text: "The address picks a project that isn't in the sessions ClaudeGlass has read. Its folder may have moved or been renamed.",
        }),
      ],
      actions: [
        button("Show all projects", {
          action: function () {
            pickProject("", { focus: true });
          },
        }),
      ],
    });
  }
  // restart_needed: the service's code changed on disk (docs/api.md); the
  // message carries the command that restarts it.
  return callout({
    tone: "critical",
    title: offline ? "Couldn't reach ClaudeGlass's local service." : code === "restart_needed" ? "ClaudeGlass needs a restart." : "Couldn't load this.",
    children: [
      el("p", { class: "callout-detail" }, [
        el("span", { text: offline ? "Check that it's still running. " : message + " " }),
        el("span", { class: "error-code", text: "(" + code + ")" }),
      ]),
    ],
    actions: retry ? [button("Try again", { icon: "refresh", action: retry })] : [],
  });
}

// Shown after every way of making a change (fixes.RESTART_NOTE; a test
// keeps the two the same).
var RESTART_NOTE =
  "Restart Claude Code to pick up the change. It reads settings and agent files when it starts, so a " +
  "session that is already open keeps the old ones (claude --continue picks your last conversation back up).";

export function restartNote() {
  return el("p", { class: "restart-note" }, [icon("refresh", { size: 14 }), el("span", { text: RESTART_NOTE })]);
}

// Shown instead of RESTART_NOTE below a "from now on" prompt (one that
// ends in fixes.PROMPT_SCOPE): pasting it doesn't save a Claude Code
// file a restart would pick back up, it asks Claude where to keep the
// rule (fixes.SCOPE_NOTE; a test keeps the two the same).
var SCOPE_NOTE =
  "Claude asks whether this is for this session, this project or all your projects. A line in CLAUDE.md " +
  "lasts; new sessions read it when they start.";

export function scopeNote() {
  return el("p", { class: "restart-note" }, [icon("info", { size: 14 }), el("span", { text: SCOPE_NOTE })]);
}

// -- empty states and skeletons ---------------------------------------------------

// P4 leftover / UX-6/9: one consistent "nothing to show" box, instead
// of each view building its own ad hoc paragraph. message says what
// happened and why ("No sessions in the last 24 hours."); next says
// what would fill it, as text or a node (a link). `gate` is the
// structured {reason, have, need} object some routes carry (see
// api.py's _min_sessions_gate, currently /api/impact) -- when given,
// its numbers are appended so the box reads "2 of 3 sessions so far"
// rather than only the prose message repeating what "not enough" means.
export function emptyState(message, gate, next) {
  var box = el("div", { class: "empty-state" });
  var text = message || "Not enough data yet.";
  // A message that already gives the count ("0 so far", "0 of the 3
  // needed") isn't told twice.
  if (gate && typeof gate.have === "number" && typeof gate.need === "number" && !/ so far| needed/.test(text)) {
    text += " (" + gate.have + " of " + gate.need + " so far.)";
  }
  box.appendChild(el("p", { class: "empty-message" }, prose(text)));
  if (next) box.appendChild(el("p", { class: "empty-next" }, typeof next === "string" ? prose(next) : [next]));
  return box;
}

// Grey shapes where content is on its way, so the page doesn't jump
// when it lands. kind: "lines" (default), "tiles", "rows" (a grid) or
// "chart". Shimmers unless reduced motion is asked for (app.css).
export function skeleton(kind, count) {
  kind = kind || "lines";
  var node = el("div", { class: "skeleton skeleton-" + kind, "aria-hidden": "true" });
  var n = count || (kind === "tiles" ? 4 : kind === "rows" ? 6 : 3);
  if (kind === "chart") {
    node.appendChild(el("div", { class: "skeleton-block" }));
    return node;
  }
  for (var i = 0; i < n; i++) node.appendChild(el("div", { class: "skeleton-block" }));
  return node;
}

// While a view's data is on its way: what is loading, in words, over a
// skeleton in its shape. The words stay when reduced motion stops the
// shimmer.
export function loadingNode(label, kind) {
  var node = el("div", { class: "loading", role: "status", "aria-busy": "true" });
  node.appendChild(el("p", { class: "loading-label", text: (label || "Loading") + "\u2026" }));
  node.appendChild(skeleton(kind || "lines"));
  return node;
}

// -- command block: how to make a change ---------------------------------------------

// Who a fix is for, when it names someone: the agent, or the main
// session for a model change (the same rule as fixTitle below).
function fixSubject(fix) {
  return fix.agent || (fix.key === "model" ? "your main session" : "");
}

function fixTitle(fix) {
  if (fix.title) return fix.title;
  // Same rule as render/tables.py's fix_subject.
  if (!fix.key) return "What you're changing";
  var who = fix.agent ? " for " + fix.agent : fix.key === "model" ? " for your main session" : "";
  return "What you're changing: " + fix.key + who;
}

// Arrow keys move between tabs (the WAI-ARIA tabs pattern).
function tabList(tabs, idBase) {
  var list = el("div", { class: "command-tabs", role: "tablist", "aria-label": "Ways to make the change" });
  var panels = [];
  var buttons = tabs.map(function (tab, i) {
    var tabButton = el("button", {
      type: "button",
      class: "command-tab",
      role: "tab",
      id: idBase + "-tab-" + i,
      "aria-selected": i === 0 ? "true" : "false",
      "aria-controls": idBase + "-panel-" + i,
      tabIndex: i === 0 ? 0 : -1,
    });
    tabButton.appendChild(icon(tab.icon, { size: 14 }));
    tabButton.appendChild(el("span", { text: tab.label }));
    var tabPanel = el("div", { class: "command-panel", role: "tabpanel", id: idBase + "-panel-" + i, "aria-labelledby": tabButton.id, hidden: i !== 0 }, tab.body);
    panels.push(tabPanel);
    return tabButton;
  });
  function select(index, focus) {
    buttons.forEach(function (b, i) {
      b.setAttribute("aria-selected", i === index ? "true" : "false");
      b.tabIndex = i === index ? 0 : -1;
      panels[i].hidden = i !== index;
    });
    if (focus) buttons[index].focus();
  }
  buttons.forEach(function (b, i) {
    b.addEventListener("click", function () {
      select(i, false);
    });
    b.addEventListener("keydown", function (event) {
      var next = null;
      if (event.key === "ArrowRight") next = (i + 1) % buttons.length;
      else if (event.key === "ArrowLeft") next = (i - 1 + buttons.length) % buttons.length;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = buttons.length - 1;
      if (next === null) return;
      event.preventDefault();
      select(next, true);
    });
    list.appendChild(b);
  });
  return { list: list, panels: panels };
}

var commandBlockCount = 0;

// One way to make a change, as fixes.build_fix describes it: the prompt
// for Claude and (for a plain setting) the dry-run command, as tabs;
// what changes, where, the trade-off and how to undo it; and a note
// (restart, scope, or none -- see noteFor). Nothing here changes
// anything: every path ends in text for you to paste.
// fix: {explainer: [[heading, text], ...], prompt, command,
// command_warning, trial_command, trial_note (a profile's one-session
// trial), note ("scope"/"none", absent for the restart default -- see
// fixes.fix_note)}; opts.heading adds the fix's title as an h4.

// fixes.fix_note, mirrored: which note (if any) belongs under this fix's
// prompt/command.
function noteFor(fix) {
  if (fix.note === "none") return null;
  if (fix.note === "scope") return scopeNote();
  return restartNote();
}

export function commandBlock(fix, opts) {
  opts = opts || {};
  var box = el("div", { class: "command-block" });
  if (opts.heading) box.appendChild(el("h4", { text: fixTitle(fix) }));
  var tabs = [];
  if (fix.prompt) {
    var promptHint =
      fix.note === "scope"
        ? "Paste this into Claude Code. It asks where this should apply before saving anything."
        : "Paste this into Claude Code. It shows you the change before saving it.";
    tabs.push({
      label: "Prompt for Claude",
      icon: "prompt",
      body: [el("p", { class: "command-hint", text: promptHint }), codeBlockWithCopy(fix.prompt, "Prompt", fixSubject(fix), opts.onCopy)],
    });
  }
  if (fix.command) {
    tabs.push({
      label: "Dry-run command",
      icon: "terminal",
      body: [
        el("p", { class: "command-hint", text: "It shows the change without writing anything. Run it again without --dry-run to make the change; the output tells you how to undo it." }),
        fix.command_warning ? callout({ tone: "warning", text: fix.command_warning, class: "fix-warning" }) : null,
        codeBlockWithCopy(fix.command, "Command", fixSubject(fix), opts.onCopy),
      ],
    });
  }
  if (fix.trial_command) {
    tabs.push({
      label: "Try it for one session",
      icon: "clock",
      body: [el("p", { class: "command-hint" }, prose(fix.trial_note || "")), codeBlockWithCopy(fix.trial_command, "Command", fixSubject(fix))],
    });
  }
  if (tabs.length > 1) {
    commandBlockCount += 1;
    var built = tabList(tabs, "command-" + commandBlockCount);
    box.appendChild(built.list);
    built.panels.forEach(function (p) {
      box.appendChild(p);
    });
  } else if (tabs.length === 1) {
    box.appendChild(el("p", { class: "command-label" }, [icon(tabs[0].icon, { size: 14 }), el("span", { text: tabs[0].label })]));
    tabs[0].body.forEach(function (node) {
      if (node) box.appendChild(node);
    });
  } else if (opts.expectCommand) {
    box.appendChild(el("p", { class: "command-hint", text: "No command for this change: it needs your judgement." }));
  }
  if (fix.explainer && fix.explainer.length) {
    var list = el("dl", { class: "fix-explainer" });
    fix.explainer.forEach(function (pair) {
      list.appendChild(el("dt", { text: pair[0] }));
      list.appendChild(el("dd", null, prose(pair[1])));
    });
    box.appendChild(list);
  }
  if (fix.prompt || fix.command || opts.restart) {
    var note = fix.prompt || fix.command ? noteFor(fix) : restartNote();
    if (note) box.appendChild(note);
  }
  return box;
}

// One fixes.build_fix entry. collapsed: a <details> with the fix's
// title, for a recommendation with several changes.
export function renderFix(fix, collapsed) {
  if (!collapsed) return commandBlock(fix, { heading: true });
  var box = el("details", { class: "fix" });
  box.appendChild(el("summary", { text: fixTitle(fix) }));
  box.appendChild(commandBlock(fix));
  return box;
}

export function renderFixList(fixes, container) {
  (fixes || []).forEach(function (fix, i) {
    container.appendChild(renderFix(fix, i > 0));
  });
}

// level: the heading's tag, for where the tips sit ("h4" by default);
// seen: the Set of glossary terms already explained on this card.
export function renderTips(tips, container, level, seen) {
  if (!tips || !tips.length) return;
  container.appendChild(el(level || "h4", { text: "Habits that help" }));
  container.appendChild(
    el(
      "ul",
      { class: "notes" },
      tips.map(function (tip) {
        return el("li", null, [el("strong", { text: tip.title + ". " }), el("span", null, prose(tip.text, seen))]);
      })
    )
  );
}

// -- toast ---------------------------------------------------------------------------

var toastState = { node: null, timer: null, paused: false };

function toastRegion() {
  var region = document.getElementById("toast-region");
  if (!region) {
    region = el("div", { id: "toast-region", class: "toast-region", role: "status", "aria-live": "polite" });
    document.body.appendChild(region);
  }
  return region;
}

function scheduleToastClose() {
  clearTimeout(toastState.timer);
  toastState.timer = setTimeout(function () {
    if (!toastState.paused) closeToast();
  }, 3500);
}

function closeToast() {
  var node = toastState.node;
  if (!node) return;
  toastState.node = null;
  node.classList.remove("is-open");
  setTimeout(function () {
    node.remove();
  }, motionOK() ? 200 : 0);
}

// A short confirmation at the bottom right, one at a time: it goes
// after 3.5 seconds, and waits while the pointer or focus is on it.
// opts.tone: success (default), info or warning.
export function toast(message, opts) {
  opts = opts || {};
  var region = toastRegion();
  if (toastState.node) toastState.node.remove();
  var tone = opts.tone || "success";
  var node = el("div", { class: "toast toast-" + tone }, [
    icon(tone === "warning" ? "warning" : tone === "info" ? "info" : "success", { size: 16 }),
    el("span", { text: plainText(message) }),
  ]);
  node.addEventListener("mouseenter", function () {
    toastState.paused = true;
  });
  node.addEventListener("mouseleave", function () {
    toastState.paused = false;
    scheduleToastClose();
  });
  region.appendChild(node);
  toastState.node = node;
  requestAnimationFrame(function () {
    node.classList.add("is-open");
  });
  scheduleToastClose();
  return node;
}

// -- tooltip -------------------------------------------------------------------------

// One tooltip element for the whole page: shown on hover and keyboard
// focus, text only (textContent, never markup), kept inside the window.
// content: a string, or a function returning one or {value, label} (a
// chart's reading: the value first, strong, then what it is).
var tip = { node: null, timer: null, owner: null };

function tipNode() {
  if (!tip.node) {
    tip.node = el("div", { id: "tooltip", class: "tooltip", role: "tooltip", hidden: true });
    document.body.appendChild(tip.node);
  }
  return tip.node;
}

function fillTip(content) {
  var node = tipNode();
  clear(node);
  var value = typeof content === "function" ? content() : content;
  if (value && typeof value === "object") {
    if (value.value) node.appendChild(el("strong", { class: "tooltip-value", text: value.value }));
    if (value.label) node.appendChild(el("span", { class: "tooltip-label", text: value.label }));
    (value.lines || []).forEach(function (line) {
      node.appendChild(el("span", { class: "tooltip-line", text: line }));
    });
  } else {
    node.appendChild(el("span", { text: String(value || "") }));
  }
  return node;
}

function placeTip(node, rect) {
  var gap = 8;
  node.style.left = "0px";
  node.style.top = "0px";
  var w = node.offsetWidth;
  var h = node.offsetHeight;
  var left = rect.left + rect.width / 2 - w / 2;
  left = Math.max(8, Math.min(left, window.innerWidth - w - 8));
  var top = rect.top - h - gap;
  if (top < 8) top = rect.bottom + gap;
  node.style.left = Math.round(left) + "px";
  node.style.top = Math.round(top) + "px";
}

// Show the tooltip beside a rect (a chart mark, or a pointer position as
// a zero-size rect); hideTooltip() takes it away.
export function showTooltipAt(rect, content) {
  var node = fillTip(content);
  node.hidden = false;
  placeTip(node, rect);
  node.classList.add("is-open");
}

export function hideTooltip() {
  clearTimeout(tip.timer);
  tip.owner = null;
  if (!tip.node) return;
  tip.node.classList.remove("is-open");
  tip.node.hidden = true;
}

// A string tip is also there for a screen reader: as the description
// of a control (a hidden child it points at), or read in line after a
// plain chip. A chip is never made a Tab stop just for its tip.
export function attachTooltip(target, content) {
  // A tooltip can't hold a link: a page link in its text reads as the
  // page's name.
  if (typeof content === "string") content = plainText(content);
  var interactive = /^(BUTTON|A|INPUT|SELECT|TEXTAREA|SUMMARY)$/.test(target.tagName) || target.hasAttribute("tabindex");
  if (typeof content === "string") {
    if (interactive) {
      tipCount += 1;
      var description = el("span", { id: "tip-" + tipCount, hidden: true, text: content });
      target.appendChild(description);
      target.setAttribute("aria-describedby", description.id);
    } else {
      target.appendChild(el("span", { class: "visually-hidden", text: " (" + content + ")" }));
    }
  }
  function show() {
    clearTimeout(tip.timer);
    tip.timer = setTimeout(function () {
      tip.owner = target;
      showTooltipAt(target.getBoundingClientRect(), content);
    }, 40);
  }
  function hide() {
    if (tip.owner === target || !tip.owner) hideTooltip();
  }
  target.addEventListener("mouseenter", show);
  target.addEventListener("mouseleave", hide);
  if (interactive) {
    target.addEventListener("focus", show);
    target.addEventListener("blur", hide);
    target.addEventListener("keydown", function (event) {
      if (event.key === "Escape") hide();
    });
  }
  return target;
}

var tipCount = 0;

// -- popover -------------------------------------------------------------------------

var popoverCount = 0;

// A panel that opens from a button: the browser's popover (top layer,
// Esc and a click outside close it), placed under the button and kept
// inside the window. Focus goes back to the button when it closes.
// build(body) fills it when first opened. Returns the button.
export function popoverButton(anchorButton, build, opts) {
  opts = opts || {};
  popoverCount += 1;
  var id = "popover-" + popoverCount;
  var pop = el("div", { class: "popover" + (opts.class ? " " + opts.class : ""), id: id, role: opts.role || "dialog", "aria-label": opts.label || null });
  pop.popover = "auto";
  var built = false;
  anchorButton.setAttribute("aria-expanded", "false");
  anchorButton.setAttribute("aria-controls", id);
  function place() {
    var rect = anchorButton.getBoundingClientRect();
    pop.style.left = "0px";
    pop.style.top = "0px";
    var w = pop.offsetWidth;
    var h = pop.offsetHeight;
    var left = opts.align === "end" ? rect.right - w : rect.left;
    left = Math.max(8, Math.min(left, window.innerWidth - w - 8));
    var top = rect.bottom + 6;
    if (top + h > window.innerHeight - 8 && rect.top - h - 6 > 8) top = rect.top - h - 6;
    pop.style.left = Math.round(left) + "px";
    pop.style.top = Math.round(top) + "px";
  }
  // Following a link inside (a page link, a glossary term's "See it in
  // the glossary") leaves this view: the popover closes with it.
  pop.addEventListener("click", function (event) {
    if (event.target.closest && event.target.closest("a[href]") && pop.matches(":popover-open")) pop.hidePopover();
  });
  pop.addEventListener("toggle", function (event) {
    var open = event.newState === "open";
    anchorButton.setAttribute("aria-expanded", open ? "true" : "false");
    if (!open && pop.contains(document.activeElement)) anchorButton.focus();
  });
  anchorButton.addEventListener("click", function () {
    // Inside a drawer (a modal <dialog>) the rest of the page is inert:
    // the popover joins the drawer so its links and buttons work.
    if (!pop.isConnected) (anchorButton.closest("dialog") || document.body).appendChild(pop);
    if (!built) {
      build(pop);
      built = true;
    }
    if (pop.matches(":popover-open")) {
      pop.hidePopover();
      return;
    }
    pop.showPopover();
    place();
    // The first control that can take focus (the chooser's first box is
    // disabled: that column always shows). A popover that only explains
    // keeps focus on its button, unless it holds a link: the popover
    // sits at the end of the page, where Tab from the button never goes.
    var first = pop.querySelector("button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])");
    if (first && (opts.focusInside !== false || pop.querySelector("a[href]"))) first.focus();
  });
  return anchorButton;
}

// -- prose: server text with its page links and glossary terms --------------------------

// Server text as nodes: each {{page:...}} token a link to its page
// (links.js linkText). Given `seen`, a Set one card or section shares,
// the first mention of each glossary term (links.js JARGON) also
// becomes a button that shows its definition; later mentions stay
// plain, so a paragraph isn't a row of underlines.
export function prose(text, seen) {
  var nodes = linkText(text);
  if (!seen) return nodes;
  var out = [];
  nodes.forEach(function (node) {
    if (node.nodeType !== 3) {
      out.push(node);
      return;
    }
    termNodes(node.textContent, seen).forEach(function (part) {
      out.push(part);
    });
  });
  return out;
}

var jargonPattern = null;

// Punctuation that belongs to the word beside it: an opening bracket or
// quote just before a term, and a full stop, comma, closing bracket or
// quote just after. A term is a button, which never breaks inside, so
// without this "list price" could end a line and its full stop start
// the next one alone.
var LEADING_MARKS = /[(\[\u201c\u2018"']+$/;
var TRAILING_MARKS = /^[.,;:!?)\]\u201d\u2019"'\u2026]+/;

function termNodes(text, seen) {
  if (!jargonPattern) {
    jargonPattern = new RegExp(
      "\\b(?:" +
        JARGON.map(function (entry) {
          return "(" + entry[1] + ")";
        }).join("|") +
        ")\\b",
      "gi"
    );
  }
  var nodes = [];
  var last = 0;
  var match;
  jargonPattern.lastIndex = 0;
  while ((match = jargonPattern.exec(text))) {
    var which = 0;
    while (match[which + 1] === undefined) which += 1;
    var term = JARGON[which][0];
    if (seen.has(term) || !glossaryText(term)) continue;
    seen.add(term);
    var end = jargonPattern.lastIndex;
    var before = text.slice(last, match.index);
    var lead = (before.match(LEADING_MARKS) || [""])[0];
    var trail = (text.slice(end).match(TRAILING_MARKS) || [""])[0];
    before = before.slice(0, before.length - lead.length);
    if (before) nodes.push(document.createTextNode(before));
    var trigger = termButton(term, match[0]);
    // Kept on one line with its punctuation (app.css .term-wrap).
    nodes.push(lead || trail ? el("span", { class: "term-wrap" }, [lead || null, trigger, trail || null]) : trigger);
    last = end + trail.length;
    jargonPattern.lastIndex = last;
  }
  if (last < text.length) nodes.push(document.createTextNode(text.slice(last)));
  return nodes;
}

// A glossary term in running text: a button styled as the word with a
// dotted underline, opening the definition and a link to the Glossary.
function termButton(term, words) {
  var trigger = el("button", { type: "button", class: "term", text: words });
  return popoverButton(
    trigger,
    function (body) {
      body.appendChild(el("p", { class: "popover-title", text: term }));
      body.appendChild(el("p", { class: "term-definition", text: glossaryText(term) }));
      body.appendChild(el("p", { class: "term-more" }, [termLink(term, "See it in the glossary")]));
    },
    { class: "term-popover", label: term }
  );
}

// The (i) that opens a table's or section's "How to read this": what
// it shows, how to read it, when to act (model.Help). card: a
// Glossary > How costs work card (links.js COST_CARDS) that explains
// the price behind it, linked at the end.
export function helpButton(help, subject, card) {
  if (!help || !(help.shows || help.read || help.act)) return null;
  var trigger = button("", { variant: "icon", icon: "info", label: "How to read " + (subject || "this"), class: "help-button" });
  return popoverButton(
    trigger,
    function (body) {
      body.appendChild(el("p", { class: "popover-title", text: "How to read this" }));
      var list = el("dl", { class: "help-list" });
      [
        ["What it shows", help.shows],
        ["How to read it", help.read],
        ["When to act", help.act],
      ].forEach(function (pair) {
        if (!pair[1]) return;
        list.appendChild(el("dt", { text: pair[0] }));
        list.appendChild(el("dd", null, prose(pair[1])));
      });
      body.appendChild(list);
      if (card) {
        var more = cardLink(card.slug, "How costs work: " + card.title.charAt(0).toLowerCase() + card.title.slice(1));
        body.appendChild(el("p", { class: "term-more" }, [more]));
      }
    },
    { class: "help-popover", label: "How to read " + (subject || "this"), focusInside: false }
  );
}

// -- drawer --------------------------------------------------------------------------

// Detail that slides in from the right over the page (a session, a
// table, a CLAUDE.md file): a modal <dialog>, so focus stays inside and
// Esc closes it; focus returns to whatever opened it. opts: title,
// wide (720px rather than 560), link (a #/ address to copy), fill
// (body) -> fills the body, closed () -> called after it closes.
export function drawer(opts) {
  var opener = document.activeElement;
  var dialog = el("dialog", { class: "drawer" + (opts.wide ? " drawer-wide" : ""), "aria-labelledby": "drawer-title" });
  var head = el("header", { class: "drawer-head" }, [el("h2", { id: "drawer-title", class: "drawer-title", text: opts.title })]);
  var tools = el("div", { class: "drawer-tools" });
  if (opts.link) {
    tools.appendChild(
      button("", {
        variant: "icon",
        icon: "link",
        label: "Copy a link to this",
        title: "Copy a link to this",
        action: function () {
          var address = window.location.origin + window.location.pathname + opts.link;
          copyToClipboard(address).then(function (ok) {
            toast(ok ? "Link copied to the clipboard." : COPY_FAILED, { tone: ok ? "success" : "warning" });
          });
        },
      })
    );
  }
  var closeButton = button("", { variant: "icon", icon: "close", label: "Close", title: "Close (Esc)", action: close });
  tools.appendChild(closeButton);
  head.appendChild(tools);
  var body = el("div", { class: "drawer-body" });
  dialog.appendChild(head);
  dialog.appendChild(body);
  document.body.appendChild(dialog);

  var closing = false;
  // leaving: a link inside led to another view, which takes the focus.
  function close(leaving) {
    if (closing) return;
    closing = true;
    dialog.classList.remove("is-open");
    setTimeout(
      function () {
        dialog.close();
        dialog.remove();
        if (leaving !== true && opener && opener.isConnected && opener.focus) opener.focus();
        if (opts.closed) opts.closed();
      },
      motionOK() ? 240 : 0
    );
  }
  dialog.addEventListener("cancel", function (event) {
    event.preventDefault();
    close();
  });
  // A click on the backdrop (outside the panel) closes it too, and so
  // does following a link inside to another view (in the text, or in a
  // popover opened from it): the view changes under the drawer.
  dialog.addEventListener("click", function (event) {
    var link = event.target.closest ? event.target.closest("a[href^='#/']") : null;
    if (link && event.button === 0 && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey) {
      close(true);
      return;
    }
    if (event.target === dialog) {
      var rect = dialog.getBoundingClientRect();
      var inside = event.clientX >= rect.left && event.clientX <= rect.right && event.clientY >= rect.top && event.clientY <= rect.bottom;
      if (!inside) close();
    }
  });
  dialog.showModal();
  closeButton.focus();
  requestAnimationFrame(function () {
    dialog.classList.add("is-open");
  });
  if (opts.fill) opts.fill(body);
  return { dialog: dialog, body: body, close: close };
}

// A yes/no question before something that can't be taken back: a
// modal <dialog> with the question, what happens, and two buttons.
// Resolves true for yes. Focus goes back to the control that asked.
export function confirmDialog(opts) {
  var opener = document.activeElement;
  return new Promise(function (resolve) {
    var dialog = el("dialog", { class: "confirm-dialog", "aria-labelledby": "confirm-title" });
    dialog.appendChild(el("h2", { id: "confirm-title", text: opts.title }));
    (opts.lines || []).forEach(function (line) {
      dialog.appendChild(el("p", { text: line }));
    });
    var answered = false;
    function answer(value) {
      if (answered) return;
      answered = true;
      dialog.close();
      dialog.remove();
      if (opener && opener.isConnected && opener.focus) opener.focus();
      resolve(value);
    }
    var no = button(opts.no || "Cancel", { variant: "quiet", action: function () { answer(false); } });
    var yes = button(opts.yes, { variant: "primary", action: function () { answer(true); } });
    dialog.appendChild(el("div", { class: "dialog-actions" }, [no, yes]));
    dialog.addEventListener("cancel", function () {
      answer(false);
    });
    document.body.appendChild(dialog);
    dialog.showModal();
    no.focus();
  });
}

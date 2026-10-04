/* claudeglass service UI: shell.js
 *
 * The parts on every page: the sidebar's status line (service health,
 * figures, capture level, version), the health banner and the
 * metrics-capture banner. Also the setup checklist, which the Overview
 * and Data quality both draw.
 */

import { clear, el, goTo, renderedViews, state, storageGet, storageSet, withCli } from "./core.js";
import { compactNumber, projectName, relativeTime, shortTs, thousands, timeNode } from "./format.js";
import { connection, fetchJson, figures, resetFiguresAsOf, runReconnectRetries } from "./api.js";
import { icon } from "./icons.js";
import { captureLink, pageLink } from "./links.js";
import { openSessionDrawer } from "./page-spend.js";
import { button, callout, codeBlockWithCopy, prose, toast } from "./ui.js";

// -- the setup checklist (setup_status.py, /api/setup/status) ------------------

// Each state in words with its icon, never the colour alone. Amber, not
// red, for a fix: it's a command to run, not a failure.
var SETUP_STATE = {
  ok: { cls: "severity-good", icon: "success" },
  waiting: { cls: "severity-info", icon: "info" },
  problem: { cls: "severity-advice", icon: "warning" },
  off: { cls: "severity-info", icon: "info" },
};

function setupItem(item) {
  var look = SETUP_STATE[item.state] || SETUP_STATE.off;
  var badge = el("span", { class: "severity-badge " + look.cls }, [icon(look.icon, { size: 14 }), el("span", { text: item.word })]);
  var row = el("li", { class: "setup-item" }, [
    el("p", { class: "setup-item-head" }, [el("strong", { text: item.label }), badge]),
    el("p", null, prose(withCli(item.detail || ""))),
  ]);
  // The dashboard changes nothing itself: a fix is a command to copy.
  if (item.fix && item.state !== "ok") row.appendChild(codeBlockWithCopy(withCli(item.fix), "Command", item.label));
  return row;
}

// Said at the top of the Overview while a part that matters isn't
// working yet: how you pay, the connection to Claude Code, the dashboard
// at logon (Claude Code deletes transcripts after cleanupPeriodDays, and
// only a running dashboard keeps their figures: that item's text says
// so), and capture when it's on. One line names the parts, so the
// Overview's own answer still comes first; the steps, each with its
// command, open beneath it. Data quality has the whole checklist.
export function renderSetupCard(setup, container) {
  if (!setup || setup.done) return;
  var pending = (setup.items || []).filter(function (item) {
    return item.essential && item.state !== "ok";
  });
  if (!pending.length) return;
  var toFix = pending.some(function (item) {
    return item.state !== "waiting";
  });
  var names = pending.map(function (item) {
    return item.label;
  });
  var steps = el("details", { class: "disclosure setup-steps" }, [
    el("summary", { text: pending.length === 1 ? "Show the step" : "Show the " + pending.length + " steps" }),
    el("ul", { class: "setup-list" }, pending.map(setupItem)),
  ]);
  container.appendChild(
    callout({
      tone: toFix ? "warning" : "info",
      class: "setup-card",
      children: [
        el("p", { class: "setup-card-line" }, [
          el("strong", { text: toFix ? "Setup isn't finished: " : "Setup is almost done: " }),
          el("span", { text: names.join(", ") + ". " }),
          pageLink("data", "Data quality"),
          " has the whole checklist.",
        ]),
        steps,
      ],
    })
  );
}

// The whole checklist, as 'claudeglass status' prints it.
export function renderSetupList(setup, container) {
  container.appendChild(el("p", { text: setup.verdict }));
  container.appendChild(el("ul", { class: "setup-list" }, (setup.items || []).map(setupItem)));
}

export function renderHealth(health, container) {
  var watcher = health.watcher || {};
  var scan = health.scan || {};
  if (health.message) {
    container.appendChild(callout({ tone: health.status === "starting" ? "info" : "critical", text: health.message }));
  }
  var code = health.code;
  var facts = [
    ["Status", HEALTH_LABELS[health.status] || health.status || "Unknown"],
    ["Version", health.version || "-"],
    [
      "Code on disk",
      !code ? "-" : code.changed ? "Changed " + shortTs(code.changed_at) + (code.version_on_disk ? ", version " + code.version_on_disk : "") : "Same as running",
    ],
    ["Database version", health.schema_version === undefined ? "-" : String(health.schema_version)],
    ["Last scan finished", scan.last_success_at ? shortTs(scan.last_success_at) : watcher.finished_at ? shortTs(watcher.finished_at) : "Not yet"],
    ["Transcript files checked", thousands(watcher.files_scanned || 0)],
    ["Changed files read", thousands(watcher.files_parsed || 0)],
    ["Sessions updated", thousands(watcher.sessions_upserted || 0)],
    ["Errors in the last scan", thousands(watcher.errors || 0)],
  ];
  var list = el("dl", { class: "fact-list" });
  facts.forEach(function (pair) {
    list.appendChild(el("dt", { text: pair[0] }));
    list.appendChild(el("dd", { text: pair[1] }));
  });
  container.appendChild(list);
  if (watcher.error_messages && watcher.error_messages.length) {
    container.appendChild(el("p", { text: "Recent errors" }));
    container.appendChild(
      el(
        "ul",
        { class: "notes" },
        watcher.error_messages.map(function (msg) {
          return el("li", { text: msg });
        })
      )
    );
  }
}

// ======================================================================
// The sidebar's status line
// ======================================================================

var HEALTH_LABELS = {
  ok: "Up to date",
  starting: "Scanning your history",
  degraded: "Last scan failed",
  stale: "Not updating",
  outdated: "Restart needed",
  unreachable: "Can't reach the service",
};

// The dot beside the label, by status token. The label always says the
// same thing in words.
var HEALTH_TONES = { ok: "good", starting: "accent", degraded: "serious", stale: "warn", outdated: "serious", unreachable: "critical" };

function captureStatusText(block) {
  if (!block) return "";
  if (!block.on) return "Capture: off";
  var title = block.title || block.level;
  if (block.expired) return "Capture: " + title + ", ended";
  if (block.hooks_ok === false) return "Capture: " + title + ", hooks not connected";
  return "Capture: " + title;
}

// A running scan's progress in a few words, from /api/health's scan
// block (the phases the banner's message names); "" before there's any.
function scanProgressText(scan) {
  if (scan.phase === "finding" && scan.done) return "Found " + thousands(scan.done) + " transcript files so far";
  if (scan.phase === "reading" && scan.total) return "Read " + thousands(scan.done || 0) + " of " + thousands(scan.total) + " changed files";
  if (scan.phase === "storing" && scan.total) return "Stored " + thousands(scan.done || 0) + " of " + thousands(scan.total) + " sessions";
  return "";
}

function renderStatusLine() {
  var line = document.getElementById("status-line");
  if (!line) return;
  var health = healthPoll.health;
  var status = healthPoll.status || "starting";
  var watcher = (health && health.watcher) || {};
  var scan = (health && health.scan) || {};
  var lastScan = scan.last_success_at || watcher.finished_at;
  // A later scan runs with the status still "ok": say so while it does.
  var rescanning = status === "ok" && !!scan.scanning;
  var label = rescanning ? "Checking for new sessions" : HEALTH_LABELS[status] || "Service " + status;
  var tone = rescanning ? "accent" : HEALTH_TONES[status] || "muted";
  var progress = status === "starting" || rescanning ? scanProgressText(scan) : "";
  var parts = [
    status,
    rescanning ? "1" : "0",
    progress,
    lastScan || "",
    watcher.errors || 0,
    figures.asOf || "",
    healthPoll.redrawDue ? "1" : "0",
    captureStatusText(health && health.capture),
    (health && health.version) || "",
  ];
  // Rebuilt only when something shown changed: the poll runs every few
  // seconds during a scan.
  var sig = parts.join("|");
  if (line.getAttribute("data-render-sig") === sig) {
    refreshTimes(line);
    return;
  }
  line.setAttribute("data-render-sig", sig);
  clear(line);

  line.appendChild(
    el("div", { class: "status-row status-health", "data-tip": label }, [
      el("span", { class: "status-dot tone-" + tone, "aria-hidden": "true" }),
      el("span", { class: "status-text", text: label }),
    ])
  );
  if (progress) line.appendChild(el("div", { class: "status-row status-detail", text: progress }));
  // Freshness reads relative, with the absolute time on hover (timeNode).
  if (lastScan || watcher.errors) {
    var scanRow = el("div", { class: "status-row status-detail" });
    if (lastScan) scanRow.appendChild(el("span", null, ["Last scan ", timeNode(lastScan)]));
    if (watcher.errors) scanRow.appendChild(el("span", { text: (lastScan ? ", " : "") + watcher.errors + (watcher.errors === 1 ? " error" : " errors") }));
    line.appendChild(scanRow);
  }
  if (figures.asOf || healthPoll.redrawDue) {
    var row = el("div", { class: "status-row status-detail" });
    if (figures.asOf) row.appendChild(el("span", null, ["Figures updated ", timeNode(figures.asOf)]));
    if (healthPoll.redrawDue) row.appendChild(redrawButton());
    line.appendChild(row);
  }
  var capture = captureStatusText(health && health.capture);
  if (capture) line.appendChild(el("div", { class: "status-row status-detail" }, [captureLink(capture)]));
  line.appendChild(
    el("div", {
      class: "status-row status-detail",
      text: (health && health.version ? "claudeglass " + health.version + ". " : "") + "Your data stays on this machine.",
    })
  );
}

// The status line's times read "5 min ago" (timeNode). Each health poll
// (every minute at most) moves their words on in place, so nothing else
// in the line is rebuilt under the reader (a focused Redraw figures
// keeps its focus); so does coming back to the page.
function refreshTimes(line) {
  Array.prototype.forEach.call(line.querySelectorAll("time[datetime]"), function (node) {
    var words = relativeTime(node.dateTime);
    if (node.textContent !== words) node.textContent = words;
  });
}

document.addEventListener("visibilitychange", function () {
  var line = document.getElementById("status-line");
  if (line && !document.hidden) refreshTimes(line);
});

// A newer or reset figures-as-of time (api.js) redraws the status line.
figures.notify = renderStatusLine;

// ======================================================================
// The health banner, under the page header on every page
// ======================================================================

// What /api/health's status means when it is not "ok" (first scan in
// progress, a failed scan, a scanner that has stopped, code changed on
// disk) and, once a scan that was running when the page drew its figures
// finishes, a way to redraw them (also offered in the status line).
// codeId: the service's code.id when this page loaded; a different one
// means it restarted on other code, which this page's scripts may not
// match, so the banner offers a reload (reloadDue).
var healthPoll = {
  status: null,
  timer: null,
  health: null,
  redrawDue: false,
  rescanning: false,
  inflight: false,
  misses: 0,
  codeId: null,
  reloadDue: false,
};

// options.focus moves focus to the page title: the button pressed is
// redrawn away. A redraw after reconnecting leaves focus where it is.
function redrawEverything(options) {
  healthPoll.redrawDue = false;
  var banner = document.getElementById("health-banner");
  if (banner) {
    banner.removeAttribute("data-scan-finished");
    // The next poll draws whatever is still due (a warning, a reload).
    banner.removeAttribute("data-render-sig");
    banner.hidden = true;
  }
  state.reportPromises = {};
  state.recommendationPromises = {};
  state.quickActionPromises = {};
  state.searchPromises = {};
  resetFiguresAsOf();
  Object.keys(renderedViews).forEach(function (key) {
    delete renderedViews[key];
  });
  // The button pressed is redrawn away, so focus moves to the page
  // title rather than dropping to the document.
  goTo(state.view || "overview", { force: true, focus: !!(options && options.focus) });
  renderStatusLine();
}

function redrawButton() {
  var redraw = el("button", { type: "button", class: "link-button", text: "Redraw figures" });
  redraw.addEventListener("click", function () {
    redrawEverything({ focus: true });
  });
  return redraw;
}

// failed: the error /api/health answered with, when the service is up but
// couldn't report its state (health is null then, as when unreachable).
function renderHealthBanner(health, previous, failed) {
  var banner = document.getElementById("health-banner");
  if (!banner) return;
  // UX-6/9: this is an aria-live="polite" region polled every 3-60s
  // (see pollHealth) -- rebuilding its children on every poll, even
  // when nothing about to be shown actually changed, used to tear
  // down and recreate the same <p>/<progress> each time, and a
  // screen reader has no way to tell that apart from genuinely new
  // content, so it re-announced an unchanged "Scanning... 4 of 12"
  // every few seconds. Skip the rebuild entirely when what would be
  // shown is identical to what is already on screen.
  var scanNow = (health && health.scan) || {};
  var sig = failed
    ? "failed|" + (failed.message || "")
    : !health
      ? "unreachable"
      : healthPoll.reloadDue
        ? "reload"
        : health.status === "ok"
          ? previous === "starting" || banner.getAttribute("data-scan-finished") === "true"
            ? "scan-finished"
            : "hidden"
          : ["active", health.status, health.message || "", scanNow.total || 0, scanNow.done || 0].join("|");
  if (banner.getAttribute("data-render-sig") === sig) return;
  banner.setAttribute("data-render-sig", sig);
  clear(banner);
  banner.className = "health-banner";
  if (failed) {
    banner.classList.add("error");
    banner.appendChild(
      el("p", { text: "ClaudeGlass's local service is running but can't report its state. " + (failed.message || "") })
    );
    banner.hidden = false;
    return;
  }
  if (!health) {
    // No countdown: this region is read aloud when it changes, so it
    // says once that it retries by itself, with a way to try now.
    banner.classList.add("error");
    banner.appendChild(
      el("p", {}, [
        el("span", { text: "Can't reach ClaudeGlass's local service, so the figures on screen may be out of date. It keeps trying by itself. " }),
        button("Try now", { variant: "link", action: pollHealth }),
      ])
    );
    banner.hidden = false;
    return;
  }
  if (healthPoll.reloadDue) {
    // Ahead of everything else: this page's own scripts are the old ones.
    banner.appendChild(
      el("p", {}, [
        el("span", { text: "ClaudeGlass restarted on updated code. Reload the page to use it. " }),
        button("Reload page", {
          variant: "link",
          action: function () {
            window.location.reload();
          },
        }),
      ])
    );
    banner.hidden = false;
    return;
  }
  if (health.status === "ok") {
    if (previous === "starting" || banner.getAttribute("data-scan-finished") === "true") {
      banner.setAttribute("data-scan-finished", "true");
      healthPoll.redrawDue = true;
      banner.appendChild(el("p", {}, [el("span", { text: "The scan has finished. " }), redrawButton()]));
      banner.hidden = false;
    } else {
      banner.hidden = true;
    }
    return;
  }
  banner.removeAttribute("data-scan-finished");
  if (health.status !== "starting") banner.classList.add("error");
  banner.appendChild(el("p", { text: health.message || HEALTH_LABELS[health.status] || health.status }));
  var scan = health.scan || {};
  if (health.status === "starting" && scan.total) {
    banner.appendChild(el("progress", { max: String(scan.total), value: String(scan.done || 0) }));
  }
  banner.hidden = false;
}

// While the service can't be reached: try again after 2, 4, 8, 16, then
// every 30 seconds.
var RETRY_SECONDS = [2, 4, 8, 16, 30];

export function pollHealth() {
  if (healthPoll.inflight) return;
  healthPoll.inflight = true;
  clearTimeout(healthPoll.timer);
  fetchJson("/api/health").then(function (result) {
    healthPoll.inflight = false;
    var body = result.body;
    var health = body && body.ok === true ? body.data : null;
    var previous = healthPoll.status;
    healthPoll.status = health ? health.status : result.httpStatus === 0 ? "unreachable" : previous || "starting";
    if (health) healthPoll.health = health;
    if (health && health.code) {
      if (!healthPoll.codeId) healthPoll.codeId = health.code.id;
      else if (health.code.id !== healthPoll.codeId) healthPoll.reloadDue = true;
    }
    // A later scan that stored sessions, seen running and now finished:
    // the figures on screen are older than it, so offer the redraw.
    var wasRescanning = healthPoll.rescanning;
    healthPoll.rescanning = !!(health && health.status === "ok" && health.scan && health.scan.scanning);
    if (wasRescanning && health && !healthPoll.rescanning && health.watcher && health.watcher.sessions_upserted) {
      healthPoll.redrawDue = true;
    }
    var failed = result.httpStatus !== 0 && !health ? (body && body.error) || {} : null;
    renderHealthBanner(result.httpStatus === 0 ? null : health, previous, failed);
    renderStatusLine();
    if (health) updateCaptureBanner(health.capture);
    var delay;
    if (result.httpStatus === 0) {
      delay = RETRY_SECONDS[Math.min(healthPoll.misses, RETRY_SECONDS.length - 1)] * 1000;
      healthPoll.misses += 1;
    } else {
      healthPoll.misses = 0;
      // Poll quickly while a scan's progress is worth watching.
      delay = health && (health.status === "starting" || healthPoll.rescanning) ? 3000 : 60000;
    }
    healthPoll.timer = setTimeout(pollHealth, delay);
  });
}

// Any request that finds the service gone (or back) says so here: the
// views are marked stale and the health poll starts retrying at once;
// back again, what failed meanwhile loads again.
connection.notify = function (up) {
  var views = document.getElementById("views");
  if (views) views.toggleAttribute("data-stale", !up);
  if (!up) {
    if (healthPoll.status !== "unreachable") pollHealth();
    return;
  }
  toast("Connected to the service again.", { tone: "info" });
  if (connection.reportFailed) {
    connection.retries = [];
    connection.reportFailed = false;
    redrawEverything();
  } else {
    runReconnectRetries();
  }
};

// ======================================================================
// Metrics capture: the banner, shown only when there is something to act
// on (the status line always says the level)
// ======================================================================

// /api/capture's answer, kept for the banner: fetched again when the
// capture block /api/health carries changes, or every few minutes for
// fresh figures (the server keeps its own copy, so this is cheap).
export var capturePoll = { data: null, sig: null, fetchedAt: 0, pending: false };

var CAPTURE_REFRESH_MS = 5 * 60 * 1000;

function captureSignature(block) {
  if (!block) return "none";
  return [block.level, block.enabled_at, block.sample, block.until, block.expired, block.hooks_ok, (block.metrics || []).join(","), (block.feedback || []).join(",")].join("|");
}

function updateCaptureBanner(block) {
  var banner = document.getElementById("capture-banner");
  if (!banner) return;
  if (!block) {
    banner.hidden = true;
    return;
  }
  var sig = captureSignature(block);
  var stale = Date.now() - capturePoll.fetchedAt > CAPTURE_REFRESH_MS;
  if (capturePoll.data && sig === capturePoll.sig && !stale) return;
  if (capturePoll.pending) return;
  capturePoll.pending = true;
  fetchJson("/api/capture").then(function (result) {
    capturePoll.pending = false;
    var body = result.body;
    if (!body || body.ok !== true) {
      banner.hidden = true;
      return;
    }
    capturePoll.sig = sig;
    showCaptureData(body.data);
  });
}

// A fresh /api/capture answer: keep it and redraw the banner.
export function showCaptureData(data) {
  capturePoll.data = data;
  capturePoll.fetchedAt = Date.now();
  renderCaptureBanner(data);
}

// UX-6/9: a notes dismissal stores its timestamp, not a bare "1", and
// lapses after BANNER_SNOOZE_MS, so a quiet banner returns on its own
// after a week rather than staying hidden for good.
var BANNER_SNOOZE_MS = 7 * 24 * 60 * 60 * 1000;

function notesSignature(notes) {
  return (notes || []).join("\n");
}

// The sessions waiting for a rating: their ids, so a list that changed
// (a new session, one rated) shows again at once.
function unratedSignature(unrated) {
  return ((unrated && unrated.pieces) || [])
    .map(function (piece) {
      return piece.session_id;
    })
    .join(",");
}

// A list keys its own snooze to its exact current content
// (timestamp + signature, "|"-joined) so a list that changed since the
// dismissal -- a new warning, say -- shows again immediately rather
// than staying suppressed for the rest of the week.
function snoozed(storeKey, signature) {
  var raw = storageGet(storeKey);
  if (!raw) return false;
  var sep = raw.indexOf("|");
  if (sep === -1) return false;
  var ts = Number(raw.slice(0, sep));
  return isFinite(ts) && raw.slice(sep + 1) === signature && Date.now() - ts < BANNER_SNOOZE_MS;
}

function notesSnoozed(notes) {
  return snoozed("tls:captureNotesHidden", notesSignature(notes));
}

function unratedSnoozed(unrated) {
  return snoozed("tls:captureUnratedHidden", unratedSignature(unrated));
}

// The banner's list of sessions big enough for the rating reminder that
// have no rating: each opens its session, where the questions are. The
// dashboard asks for a rating, and changes nothing.
function unratedBlock(unrated, data) {
  var block = el("div", { class: "capture-unrated" });
  block.appendChild(el("p", null, prose(unrated.text)));
  block.appendChild(
    el(
      "ul",
      { class: "capture-notes capture-unrated-list" },
      unrated.pieces.map(function (piece) {
        var rate = el("button", { type: "button", class: "link-button", text: "Rate it" });
        rate.setAttribute("aria-label", "Rate the " + projectName(piece.slug) + " session from " + relativeTime(piece.last_ts));
        rate.addEventListener("click", function () {
          openSessionDrawer(piece.session_id);
        });
        return el("li", null, [
          el("span", { text: projectName(piece.slug) }),
          " · " + relativeTime(piece.last_ts) + " · " + compactNumber(piece.tokens) + " tokens ",
          rate,
        ]);
      })
    )
  );
  var more = unrated.total - unrated.pieces.length;
  if (more > 0) {
    block.appendChild(el("p", { class: "notes" }, [thousands(more) + " more. ", pageLink("spend/sessions", "Sessions")]));
  }
  var dismiss = el("button", { type: "button", class: "link-button capture-hide", text: "Dismiss for a week" });
  dismiss.addEventListener("click", function () {
    storageSet("tls:captureUnratedHidden", Date.now() + "|" + unratedSignature(unrated));
    renderCaptureBanner(data);
  });
  block.appendChild(dismiss);
  return block;
}

function renderCaptureBanner(data) {
  var banner = document.getElementById("capture-banner");
  if (!banner) return;
  var info = data.banner || {};
  var notes = info.notes || [];
  var notesVisible = notes.length > 0 && !notesSnoozed(notes);
  var unrated = info.unrated && info.unrated.pieces && info.unrated.pieces.length ? info.unrated : null;
  var unratedVisible = !!unrated && !unratedSnoozed(unrated);
  var hidden = !info.feedback_note && !notesVisible && !unratedVisible;
  // Same "skip the rebuild when nothing shown would change" guard as
  // renderHealthBanner -- this is an aria-live="polite" region too,
  // and gets re-rendered on every capture poll (see updateCaptureBanner),
  // not only on an actual content change.
  var sig = hidden ? "hidden" : ["shown", info.on ? "1" : "0", info.headline || "", info.feedback_note || "", notesVisible ? notesSignature(notes) : "", unratedVisible ? unrated.text + "|" + unratedSignature(unrated) : ""].join("~");
  if (banner.getAttribute("data-render-sig") === sig) return;
  banner.setAttribute("data-render-sig", sig);
  clear(banner);
  banner.className = "capture-banner " + (info.on ? "capture-on" : "capture-off");
  if (hidden) {
    banner.hidden = true;
    return;
  }
  var line = el("p", { class: "capture-headline" }, [el("span", { text: info.headline + " " })]);
  line.appendChild(captureLink(info.on ? "Capture settings" : "See what it captures"));
  if (info.on) {
    line.appendChild(document.createTextNode(" "));
    line.appendChild(pageLink("habits", "Work habits"));
  }
  banner.appendChild(line);
  if (notesVisible) {
    banner.appendChild(
      el(
        "ul",
        { class: "capture-notes" },
        notes.map(function (note) {
          return el("li", null, prose(note));
        })
      )
    );
    var dismissNotes = el("button", { type: "button", class: "link-button capture-hide", text: "Dismiss for a week" });
    dismissNotes.addEventListener("click", function () {
      storageSet("tls:captureNotesHidden", Date.now() + "|" + notesSignature(notes));
      renderCaptureBanner(data);
    });
    banner.appendChild(dismissNotes);
  }
  if (unratedVisible) banner.appendChild(unratedBlock(unrated, data));
  if (info.feedback_note) banner.appendChild(el("p", { class: "capture-feedback-note", text: info.feedback_note }));
  banner.hidden = false;
}

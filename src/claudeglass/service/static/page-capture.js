/* claudeglass service UI: page-capture.js
 *
 * Setup, Capture: metrics-capture level, sample and metrics. Its saves
 * are the one config write the dashboard makes, to ClaudeGlass's own
 * config.toml.
 */

import { clear, cli, el } from "./core.js";
import { currencyAmount, formatCell, thousands } from "./format.js";
import { loadInto, postJson } from "./api.js";
import { button, callout, chip, codeBlockWithCopy, confirmDialog, emptyState, errorNotice, prose, toast } from "./ui.js";
import { simpleTable } from "./grid.js";
import { viewIntro } from "./links.js";
import { capturePoll, showCaptureData } from "./shell.js";

// -- Setup, Capture ---------------------------------------------------

// The billing mode capture's amounts were phrased for (/api/capture's
// billing.mode), set each time the page draws.
var captureBilling = "";

// An amount /api/capture phrased for the billing mode ({usd, text}). On
// pay-per-token billing it reads the way every other page writes money
// ("$1.29 a week"); on a plan the service's phrase stands, because only
// the service knows the share of the weekly limit it comes to.
function billed(amount, period, prefix) {
  if (!amount) return "";
  if (captureBilling === "api" && typeof amount.usd === "number") {
    if (amount.usd <= 0) return "nothing";
    return (prefix || "") + currencyAmount(amount.usd) + (period ? " " + period : "");
  }
  return amount.text || "";
}

export function renderCapture(panel) {
  clear(panel);
  viewIntro(panel, "setup/capture");
  var container = el("div", { id: "capture-content" });
  panel.appendChild(container);
  loadInto(
    container,
    "/api/capture",
    function (data, target) {
      showCaptureData(data);
      renderCaptureData(data, target);
    },
    { skeleton: "lines" }
  );
}

// One part of the page: an h2, then its body.
function captureBlock(container, title) {
  var block = el("section", { class: "report-section" });
  block.appendChild(el("h2", { class: "section-title", text: title }));
  container.appendChild(block);
  return block;
}

function levelMetricIds(data) {
  var levelIds = {};
  (data.sections || []).forEach(function (section) {
    section.metrics.forEach(function (row) {
      if (row.kind === "level") levelIds[row.id] = true;
    });
  });
  return (data.config.metrics || []).filter(function (id) {
    return levelIds[id];
  });
}

function allRows(data) {
  var rows = [];
  (data.sections || []).forEach(function (section) {
    rows = rows.concat(section.metrics);
  });
  return rows;
}

function rowById(data, id) {
  return allRows(data).filter(function (row) {
    return row.id === id;
  })[0];
}

// What a change adds that makes Claude use more tokens: the metrics
// it switches on that ask Claude to read or write something.
function costlyAdditions(data, beforeIds, afterIds) {
  return afterIds.filter(function (id) {
    var row = rowById(data, id);
    return beforeIds.indexOf(id) === -1 && row && row.asks_claude;
  });
}

function titlesOf(data, ids) {
  return ids
    .map(function (id) {
      var row = rowById(data, id);
      return row ? row.title.toLowerCase() : id;
    })
    .join(", ");
}

// The cost warning again, before anything that uses more tokens: a
// modal dialog (ui.js's confirmDialog) with Cancel focused.
function confirmCapture(title, lines, onYes) {
  confirmDialog({ title: title, lines: lines.filter(Boolean), yes: "Go ahead", no: "Cancel" }).then(function (ok) {
    if (ok) onYes();
  });
}

function captureStatus(container) {
  return container.querySelector(".capture-status");
}

// Send a change; redraw the view and the banner from the answer.
function postCapture(change, container, doneText) {
  var status = captureStatus(container);
  if (status) {
    clear(status);
    status.appendChild(el("p", { class: "status-text", text: "Saving…" }));
  }
  postJson("/api/capture", change).then(function (result) {
    var body = result.body;
    if (!body || body.ok !== true) {
      if (!status) return;
      clear(status);
      status.appendChild(errorNotice(body && body.error));
      var commands = (body && body.error && body.error.commands) || [];
      if (commands.length) {
        status.appendChild(el("p", { text: "From a terminal on this machine:" }));
        status.appendChild(codeBlockWithCopy(commands.join("\n")));
      }
      return;
    }
    capturePoll.sig = null;
    showCaptureData(body.data);
    clear(container);
    renderCaptureData(body.data, container);
    var message = body.data.changed
      ? doneText + " It takes effect in sessions and subagents started from now on."
      : "Nothing changed: it was already set that way.";
    var fresh = captureStatus(container);
    if (fresh) fresh.appendChild(callout({ tone: body.data.changed ? "success" : "info", text: message }));
    if (body.data.changed) toast(doneText, { tone: "success" });
  });
}

function estimateLine(estimate) {
  if (!estimate) return "";
  var share = estimate.share_text ? ", " + estimate.share_text + " of what you spent" : "";
  return "About " + estimate.tokens_text + " tokens and " + billed(estimate, "a week") + share + ".";
}

function roughLine(rough) {
  var parts = [];
  if (rough.session_note) parts.push("about " + rough.session_note + " tokens of note when a session starts, is cleared or compacts");
  if (rough.subagent_note) parts.push("about " + rough.subagent_note + " when a subagent starts");
  if (rough.reply_tag) parts.push("about " + rough.reply_tag + " tokens of tag per reply");
  if (rough.reminder) parts.push("about " + rough.reminder + " tokens once a session for the /cg-feedback reminder");
  if (rough.report_tag) parts.push("about " + rough.report_tag + " per agent report");
  if (rough.tool_note) parts.push("about " + rough.tool_note + " after each large or web tool result");
  if (rough.agent_judge) parts.push("a Claude Haiku call after each agent run (the agent is asked for nothing)");
  return parts.length ? "Roughly " + parts.join("; ") + "." : "";
}

function renderCaptureData(data, container) {
  var config = data.config || {};
  captureBilling = (data.billing && data.billing.mode) || "";
  container.appendChild(callout({ tone: "warning", text: data.warning, class: "capture-warning" }));
  container.appendChild(el("div", { class: "capture-status", role: "status", "aria-live": "polite" }));

  // Where it stands now.
  var nowBlock = captureBlock(container, "Now");
  var now = [el("strong", { text: "Metrics capture: " + config.describe + ". " })];
  if (config.expired) now.push(el("span", { text: "Its end time has passed, so nothing is captured now. " }));
  if (config.projects_limited) now.push(el("span", { text: "Only some projects are captured ([capture] projects in config.toml). " }));
  nowBlock.appendChild(el("p", {}, now));
  var measured = data.measured;
  if (measured) {
    if (measured.sessions || measured.subagents) {
      var lines = [
        "Measured since " + (measured.since || "").slice(0, 10) + ": " + measured.sessions + " sessions and " + measured.subagents + " subagents captured.",
        "About " + thousands(measured.note_tokens) + " tokens of note and " + thousands(measured.tag_tokens) + " tokens of tag: " + billed(measured) + (measured.share_text ? ", " + measured.share_text + " of what those sessions cost." : "."),
      ];
      if (measured.coverage_text) {
        var tagger = config.tagger === "haiku" ? "Claude Haiku" : "Claude";
        lines.push(tagger + " tagged " + measured.coverage_text + " of your messages" + (measured.report_coverage_pct !== null ? "; Claude Haiku judged " + formatCell(measured.report_coverage_pct, "pct") + " of agent runs." : "."));
      }
      nowBlock.appendChild(el("ul", { class: "notes" }, lines.map(function (line) {
        return el("li", { text: line });
      })));
      var scopeNames = { main: "Main session", subagent: "Subagents", tool: "After tool results", brief: "Agent briefs", haiku: "Claude Haiku's calls" };
      var scopeRows = Object.keys(measured.scopes || {}).map(function (key) {
        var scope = measured.scopes[key];
        return [scopeNames[key] || key, thousands(scope.note_tokens), thousands(scope.tag_tokens), billed(scope)];
      });
      if (scopeRows.length) {
        nowBlock.appendChild(
          simpleTable(
            [{ label: "Where" }, { label: "Note tokens" }, { label: "Tag tokens" }, { label: "Cost" }],
            scopeRows,
            "Where the tokens went"
          )
        );
      }
    } else {
      nowBlock.appendChild(
        emptyState(
          "No captured sessions yet: capture covers sessions, and their subagent runs, started after capture was turned on.",
          null,
          "Start a new Claude Code session to see the first figures here."
        )
      );
    }
  }
  if (data.roi && data.roi.cost && data.roi.cost.usd > 0) {
    // UX-2: roi.cost.text/roi.value.text already carry their own
    // "about" (capture_view.py's _roi) -- not repeated here, or a
    // subscription's would double into "about about X%...". billed()
    // adds it back only when it writes the amount itself.
    var roiText = data.roi.measured
      ? "Capture cost " + billed(data.roi.cost, "a week", "about ") + "; suggestions that rely on it are worth " + billed(data.roi.value, "a week", "about ") + "."
      : "Capture cost " + billed(data.roi.cost, "a week", "about ") + "; nothing measured yet relies on it.";
    nowBlock.appendChild(el("p", { class: "notes", text: roiText }));
  }
  if (data.history && data.history.sessions) {
    nowBlock.appendChild(
      el("p", {
        class: "notes",
        text: "Estimates replay your last " + data.history.days + " days: " + data.history.sessions + " sessions and " + data.history.subagents + " subagents, as if capture had been on.",
      })
    );
  }
  if (data.billing && data.billing.basis) nowBlock.appendChild(el("p", { class: "notes" }, prose(data.billing.basis)));

  // Hook entries Claude Code needs to run for the chosen metrics.
  var hooks = data.hooks || {};
  if (hooks.ok === false && hooks.blocked_by) {
    // A settings policy stops Claude Code running these hooks at all;
    // 'capture connect' can't change that, so it isn't offered.
    nowBlock.appendChild(callout({ tone: "critical", text: hooks.summary }));
  } else if (hooks.ok === false) {
    nowBlock.appendChild(
      callout({
        tone: "critical",
        text: hooks.summary,
        children: [
          (hooks.missing || []).length > 1
            ? el("ul", {}, hooks.missing.map(function (entry) {
                return el("li", { text: "Missing: " + entry });
              }))
            : null,
          el("p", { text: "This page never changes Claude Code's settings.json. This command shows the change and asks before making it; it backs the file up first, and '" + cli("capture remove") + "' takes the entries out again." }),
          codeBlockWithCopy(hooks.connect_command, "Command"),
        ],
      })
    );
  }

  renderCaptureLevels(data, container);
  renderCaptureControls(data, container);
  renderCaptureMetrics(data, container);
}

function renderCaptureLevels(data, container) {
  var config = data.config || {};
  var block = captureBlock(container, "Level");
  block.appendChild(el("p", { class: "notes", text: "Each level adds to the one before. Estimates are per week, from your own sessions" + (config.sample < 100 ? ", with " + config.sample + "% of sessions captured" : "") + "." }));
  var grid = el("div", { class: "capture-levels", role: "list" });
  (data.levels || []).forEach(function (level) {
    var card = el("div", { class: "capture-level" + (level.current ? " current" : ""), role: "listitem" });
    var head = el("div", { class: "capture-level-head" }, [el("strong", { text: level.title })]);
    if (level.current) head.appendChild(chip("Current", { tone: "accent", icon: "check" }));
    card.appendChild(head);
    card.appendChild(el("p", null, prose(level.summary)));
    var cost;
    if (level.id === "off") cost = "No tokens.";
    else if (!level.asks_claude && level.metrics.length) cost = "No Claude tokens.";
    else cost = estimateLine(level.estimate) || roughLine(level.rough);
    if (cost) card.appendChild(el("p", { class: "capture-cost", text: cost }));
    if (level.adds && level.adds.length) card.appendChild(el("p", { class: "notes", text: "Adds: " + level.adds.join(", ") + "." }));
    if (level.id !== "custom" && !level.current) {
      var switchButton = button(level.id === "off" ? "Switch off" : "Switch to " + level.title, { class: "capture-button" });
      switchButton.addEventListener("click", function () {
        var before = config.metrics || [];
        var added = costlyAdditions(data, before, level.metrics);
        var send = function () {
          postCapture({ level: level.id }, container, level.id === "off" ? "Switched off." : "Saved: " + level.title + ".");
        };
        if (!added.length) {
          send();
          return;
        }
        confirmCapture("Use more tokens for metrics capture?", [
          data.warning,
          "Switching to " + level.title + " adds " + titlesOf(data, added) + ".",
          level.estimate ? "At the pace of your last two weeks: " + estimateLine(level.estimate) : roughLine(level.rough),
        ], send);
      });
      card.appendChild(switchButton);
    }
    grid.appendChild(card);
  });
  block.appendChild(grid);
}

// When capture ends, as the end-time menu's first choice: "In 12 days:
// 2026-10-09 06:00 UTC", or "Ended: ..." once it has passed.
function endText(until, expired) {
  if (expired) return "Ended: " + endMoment(until);
  var hours = (Date.parse(until) - Date.now()) / 3600000;
  var soon = hours < 36 ? Math.max(1, Math.round(hours)) + (Math.round(hours) <= 1 ? " hour" : " hours") : Math.round(hours / 24) + " days";
  return "In " + soon + ": " + endMoment(until);
}

// "2026-10-09T06:00:00+00:00" -> "2026-10-09 06:00 UTC", whatever
// offset the end was saved with (the CLI's --until takes any).
function endMoment(until) {
  var at = Date.parse(until);
  if (!isFinite(at)) return String(until);
  return new Date(at).toISOString().slice(0, 16).replace("T", " ") + " UTC";
}

var CAPTURE_ENDS = [
  { value: "", label: "No end" },
  { value: "1", label: "1 day from now" },
  { value: "3", label: "3 days from now" },
  { value: "7", label: "7 days from now" },
  { value: "14", label: "14 days from now" },
  { value: "30", label: "30 days from now" },
];

function renderCaptureControls(data, container) {
  var config = data.config || {};
  var block = captureBlock(container, "How much and for how long");
  if (!config.on) {
    block.appendChild(el("p", { class: "notes", text: "When you switch capture on, it ends by itself after " + config.timebox_days + " days. Once it's on, you can pick another end here, or none." }));
    return;
  }
  var form = el("div", { class: "capture-controls" });
  var sampleId = "capture-sample";
  var sample = el("select", { id: sampleId });
  (data.samples || []).forEach(function (value) {
    sample.appendChild(el("option", { value: String(value), text: value === 100 ? "Every session" : value + "% of sessions" }));
  });
  sample.value = String(config.sample);
  sample.addEventListener("change", function () {
    var value = parseInt(sample.value, 10);
    var send = function () {
      postCapture({ sample: value }, container, "Saved: " + (value === 100 ? "every session" : value + "% of sessions") + ".");
    };
    if (value > config.sample) {
      confirmCapture("Capture more sessions?", [data.warning, "Capturing " + value + "% of sessions instead of " + config.sample + "% uses about " + (value / config.sample).toFixed(1) + " times the tokens."], send);
      sample.value = String(config.sample);
    } else {
      send();
    }
  });
  form.appendChild(el("label", { for: sampleId, text: "Sessions captured" }));
  form.appendChild(sample);
  form.appendChild(el("p", { class: "notes", text: "A session is in or out for its whole life, and its subagents with it. Fewer sessions cost less and still give a fair picture over time." }));

  var taggerId = "capture-tagger";
  var tagger = el("select", { id: taggerId });
  tagger.appendChild(el("option", { value: "claude", text: "Claude, at the end of its replies" }));
  tagger.appendChild(el("option", { value: "haiku", text: "Claude Haiku, after each turn" }));
  tagger.value = config.tagger || "claude";
  tagger.addEventListener("change", function () {
    var value = tagger.value;
    var send = function () {
      postCapture({ tagger: value }, container, "Saved: " + (value === "haiku" ? "Claude Haiku" : "Claude") + " writes the tags in new sessions.");
    };
    if (value === "haiku") {
      confirmCapture("Let Claude Haiku write the tags?", [(data.tagger_text || {}).haiku], send);
      tagger.value = config.tagger || "claude";
    } else {
      send();
    }
  });
  form.appendChild(el("label", { for: taggerId, text: "Tags written by" }));
  form.appendChild(tagger);
  form.appendChild(el("p", { class: "notes", text: "Claude Haiku keeps the tags out of Claude's replies and context. After each turn, a hook sends it a short excerpt, through your own Claude Code login. Only the tag's words are kept. Agent runs are Claude Haiku's to judge either way." }));

  var endId = "capture-end";
  var end = el("select", { id: endId });
  // The end already set, as the first choice (the others count from now).
  if (config.until) end.appendChild(el("option", { value: "keep", text: endText(config.until, config.expired) }));
  CAPTURE_ENDS.forEach(function (opt) {
    end.appendChild(el("option", { value: opt.value, text: opt.label }));
  });
  end.value = config.until ? "keep" : "";
  end.addEventListener("change", function () {
    if (end.value === "keep") return;
    var until = "";
    if (end.value) until = new Date(Date.now() + parseInt(end.value, 10) * 86400000).toISOString().slice(0, 19) + "+00:00";
    postCapture({ until: until }, container, until ? "Saved: capture ends " + endMoment(until) + "." : "Saved: capture has no end.");
  });
  form.appendChild(el("label", { for: endId, text: "Capture ends" }));
  form.appendChild(end);
  form.appendChild(el("p", { class: "notes", text: "A choice saves as soon as you pick it. The days count from when you pick, not from when capture started." }));
  form.appendChild(el("p", { class: "notes", text: "At the end, capture stops by itself and the banner says so. Your level stays set, so picking a new end starts it again." }));
  block.appendChild(form);
}

// Each group of metrics folds, so the page opens on the levels. A group
// with a metric that needs something from you (a hook entry, an
// install) or that won't show (the status line isn't this tool's)
// starts open.
function renderCaptureMetrics(data, container) {
  var block = captureBlock(container, "Metrics");
  block.appendChild(el("p", { class: "notes", text: "What each one captures and what it costs. Open one for why it helps and what Claude writes for it. Ticking one here picks your own set (Custom)." }));
  (data.sections || []).forEach(function (section) {
    var on = section.metrics.filter(function (row) {
      return row.on;
    }).length;
    var group = el("details", { class: "disclosure capture-group" });
    group.open = section.metrics.some(function (row) {
      return row.needs_hook || row.needs_install || row.statusline_note;
    });
    group.appendChild(el("summary", { text: section.title + " (" + on + " of " + section.metrics.length + " on)" }));
    var list = el("div", { class: "capture-metric-list" });
    section.metrics.forEach(function (row) {
      list.appendChild(renderMetricRow(row, data, container));
    });
    group.appendChild(list);
    block.appendChild(group);
  });
}

function renderMetricRow(row, data, container) {
  var config = data.config || {};
  var box = el("div", { class: "capture-metric" + (row.on ? " on" : "") });
  var id = "metric-" + row.id;
  var head = el("div", { class: "capture-metric-head" });
  var toggle = el("input", { type: "checkbox", id: id, checked: row.on, disabled: !row.toggle });
  head.appendChild(toggle);
  head.appendChild(el("label", { for: id, class: "capture-metric-title", text: row.title }));
  if (!row.toggle) head.appendChild(chip("Always measured"));
  if (row.needs_hook) head.appendChild(chip("Needs a hook entry", { tone: "warn", icon: "warning" }));
  if (row.needs_install) head.appendChild(chip("Needs installing", { tone: "warn", icon: "warning" }));
  if (row.enough) head.appendChild(chip("Enough collected", { tone: "good", icon: "check" }));
  box.appendChild(head);
  // Each glossary term is explained once per metric: its first use.
  var seen = new Set();
  box.appendChild(el("p", { class: "capture-metric-what" }, prose(row.what, seen)));
  // What it costs and how far along it is stay in view; why it helps
  // and what Claude writes are one click away.
  var cost;
  if (!row.asks_claude) cost = row.cost_note || (row.kind === "free" ? "No Claude tokens: a hook logs it to a local file." : "No tokens.");
  else if (row.estimate) cost = (row.on ? "Saves about " : "Adds about ") + billed(row.estimate, "a week") + (row.on ? " if switched off." : ".");
  if (row.actual) cost = (cost ? cost + " " : "") + (row.actual_label || "Since it was turned on") + ": " + billed(row.actual) + ".";
  var status = [];
  if (cost) status.push(cost);
  if (row.target) status.push("Collected " + row.answers + " of " + row.target + " answers" + (row.enough ? (row.asks_claude ? ": enough for firm suggestions, so switching it off would save its cost." : ": enough for firm suggestions.") : "."));
  if (status.length) box.appendChild(el("p", { class: "capture-metric-cost", text: status.join(" ") }));
  var facts = el("dl", { class: "capture-metric-facts" });
  function fact(label, value, cls) {
    if (!value) return;
    facts.appendChild(el("dt", { text: label }));
    facts.appendChild(el("dd", { class: cls || null }, prose(value, seen)));
  }
  fact("Why", row.why);
  if (row.tag) fact("Claude writes", row.tag, "capture-metric-tag");
  if (row.powers && row.powers.length) fact("Helps with", row.powers.join(", "));
  if (facts.childNodes.length) {
    var more = el("details", { class: "capture-metric-more" });
    more.appendChild(el("summary", { text: row.tag ? "Why it helps and what Claude writes" : "Why it helps" }));
    more.appendChild(facts);
    box.appendChild(more);
  }
  if (row.needs_hook && row.hook_command && !(data.hooks && data.hooks.blocked_by)) {
    // The chip says an entry is missing; this says how to add it. The
    // dashboard never writes settings.json: the CLI shows the change first.
    box.appendChild(el("p", { class: "notes" }, prose("settings.json has no entry for this. The dashboard doesn't change it, so add the entry from a terminal:")));
    box.appendChild(codeBlockWithCopy(row.hook_command, "Command"));
  }
  if (row.needs_install) {
    // The dashboard never writes Claude Code's folder: the CLI adds the
    // skill after showing it and asking.
    box.appendChild(el("p", { class: "notes" }, prose(row.install_note + ". The dashboard doesn't write Claude Code's folder, so add it from a terminal:")));
    box.appendChild(codeBlockWithCopy(row.install_command, "Command"));
  }
  if (row.statusline_note) box.appendChild(el("p", { class: "notes" }, prose(row.statusline_note)));

  toggle.addEventListener("change", function () {
    var turningOn = toggle.checked;
    toggle.checked = row.on; // redrawn from the server's answer
    var change;
    var dropped = [];
    if (row.kind === "level") {
      var current = levelMetricIds(data);
      var next;
      if (turningOn) {
        next = current.concat([row.id]);
      } else {
        dropped = allRows(data)
          .filter(function (other) {
            return other.on && other.id !== row.id && (other.requires || []).indexOf(row.id) !== -1;
          })
          .map(function (other) {
            return other.id;
          });
        next = current.filter(function (id) {
          return id !== row.id && dropped.indexOf(id) === -1;
        });
      }
      change = { metrics: next };
    } else {
      var key = row.kind === "coaching" ? "coaching" : "feedback";
      var list = (config[key] || []).slice();
      change = {};
      change[key] = turningOn ? list.concat([row.id]) : list.filter(function (id) {
        return id !== row.id;
      });
    }
    var done = (turningOn ? "Switched on: " : "Switched off: ") + row.title.toLowerCase() + (dropped.length ? ", and " + titlesOf(data, dropped) + ", which need it" : "") + ".";
    var send = function () {
      postCapture(change, container, done);
    };
    if (turningOn && row.asks_claude) {
      var needs = (row.requires || []).filter(function (id) {
        return (config.metrics || []).indexOf(id) === -1;
      });
      confirmCapture("Use more tokens for metrics capture?", [
        data.warning,
        "Switching on " + row.title.toLowerCase() + (needs.length ? " (with " + titlesOf(data, needs) + ", which it needs)" : "") + (row.estimate ? " adds about " + billed(row.estimate, "a week") + "." : "."),
      ], send);
    } else {
      send();
    }
  });
  return box;
}

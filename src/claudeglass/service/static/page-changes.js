/* claudeglass service UI: page-changes.js
 *
 * Your changes: did the changes you made work, and by how much? At the
 * top, cost per reply by day with each change marked and a flat line at
 * the average between one change and the next (chart 9). Under it, a
 * card per change (/api/impact): the measures that change should move,
 * before against after, how sure the difference is, what it saved so
 * far, whether quality held, and how to undo it. Then whether the
 * estimates Profiles showed came true (/api/backtest).
 *
 * The Overview's "Did your changes work?" draws the latest cards in
 * short (renderChangeCards with opts.compact).
 */

import { clear, cli, el, goTo, onParams, state } from "./core.js";
import { fetchJson, loadInto, loadReport, withWindow } from "./api.js";
import { modelNames, moneyText, projectName, shortTs, signedPercent } from "./format.js";
import { chip, codeBlockWithCopy, emptyState, errorNotice, loadingNode, prose } from "./ui.js";
import { icon } from "./icons.js";
import { pulseNode, simpleTable } from "./grid.js";
import { captureLink, pageLink, viewIntro } from "./links.js";
import { chartError, holdChart } from "./charts.js";
import { changeDay, renderChart, windowSpan } from "./charts-types.js";

// -- how a measure reads -------------------------------------------------------

// Each ratio-test reading (impact.py's label_key) in words, and whether
// it is good news for a measure where lower is better. A measure with no
// better side (better: null) reads neutral whichever way it moved.
var READINGS = {
  lower: { words: "Lower", side: "lower" },
  possibly_lower: { words: "Possibly lower", side: "lower" },
  higher: { words: "Higher", side: "higher" },
  possibly_higher: { words: "Possibly higher", side: "higher" },
  no_clear_change: { words: "No clear change", side: null },
  too_little_data: { words: "Too early to tell", side: null },
};

// "good", "bad" or "neutral": the reading against the side that's better.
export function readingTone(measure) {
  var reading = READINGS[measure.label_key] || READINGS.too_little_data;
  if (!reading.side || !measure.better) return "neutral";
  return reading.side === measure.better ? "good" : "bad";
}

var TONE_LOOK = {
  good: { cls: "severity-good", icon: "success" },
  bad: { cls: "severity-advice", icon: "warning" },
  neutral: { cls: "severity-info", icon: "info" },
};

function readingBadge(measure) {
  var reading = READINGS[measure.label_key] || READINGS.too_little_data;
  var look = TONE_LOOK[readingTone(measure)];
  return el("span", { class: "severity-badge " + look.cls }, [icon(look.icon, { size: 14 }), el("span", { text: reading.words })]);
}

// -- a measure, before against after ---------------------------------------------

// Two bars on one scale, the before in the quieter colour, then the
// change and how sure it is. The figures are the server's text, in the
// billing mode's units.
function measureRow(measure) {
  var before = Number(measure.before_value);
  var after = Number(measure.after_value);
  var top = Math.max(isFinite(before) ? before : 0, isFinite(after) ? after : 0);
  function bar(value, which) {
    var fill = el("span", { class: "change-bar-fill is-" + which });
    fill.style.width = top > 0 && isFinite(value) ? Math.max(1, (value / top) * 100) + "%" : "0";
    return el("span", { class: "change-bar", "aria-hidden": "true" }, [fill]);
  }
  var tone = readingTone(measure);
  var reading = el("p", { class: "change-reading" }, [
    measure.change_pct === null || measure.change_pct === undefined ? null : el("span", { class: "change-delta is-" + tone, text: signedPercent(measure.change_pct) }),
    readingBadge(measure),
    el("span", { class: "change-counts", text: countsText(measure) }),
  ]);
  return el("div", { class: "change-measure" }, [
    el("p", { class: "change-measure-label", text: measure.label }),
    el("div", { class: "change-bars" }, [
      el("span", { class: "change-bar-name", text: "Before" }),
      bar(before, "before"),
      el("span", { class: "change-bar-text", text: measure.before || "no data" }),
      el("span", { class: "change-bar-name", text: "After" }),
      bar(after, "after"),
      el("span", { class: "change-bar-text", text: measure.after || "no data" }),
    ]),
    reading,
  ]);
}

function countsText(measure) {
  var noun = /spawn/.test(measure.label) ? "runs" : "sessions";
  if (measure.before_n === undefined || measure.after_n === undefined) return "";
  return measure.before_n + " " + noun + " before, " + measure.after_n + " after";
}

// -- what it saved ---------------------------------------------------------------

// What the sessions since would have cost without the change
// (counterfactual.py), as the card's headline figure. The server's own
// sentence and how it was priced follow in the quieter ink.
function savedLine(without) {
  if (!without || typeof without.saved_usd !== "number") return null;
  var saved = without.saved_usd;
  var sessions = without.sessions ? " over the " + without.sessions + (without.sessions === 1 ? " session" : " sessions") + " since" : " so far";
  var lead =
    saved > 0
      ? [el("strong", { text: "Saved so far: " }), el("span", { text: moneyText(saved) + sessions + "." })]
      : saved < 0
        ? [el("strong", { text: "Cost more so far: " }), el("span", { text: moneyText(-saved) + sessions + "." })]
        : [el("strong", { text: "No saving so far" }), el("span", { text: sessions + "." })];
  return el("div", { class: "change-saved" }, [
    el("p", null, lead),
    el("p", { class: "notes", text: modelNames([without.fidelity_text, without.basis].filter(Boolean).join(" ")) }),
  ]);
}

// Several settings at once: a row per setting a method covers. Their
// figures overlap, so the headline uses the sessions before instead.
function perKeyTable(without) {
  var rows = without && without.fidelity === "before" ? without.per_key || [] : [];
  if (!rows.length) return null;
  return simpleTable(
    [{ label: "Setting" }, { label: "Without it" }, { label: "How" }],
    rows.map(function (row) {
      return [(row.agent ? row.agent + ": " : "") + row.key, row.saved_text, row.fidelity_text];
    })
  );
}

// -- quality ---------------------------------------------------------------------

function qualityNodes(item) {
  var nodes = [];
  var groups = item.quality || [];
  var unjudged = groups.filter(function (group) {
    return !group.judged;
  });
  groups.forEach(function (group) {
    if (!group.judged) return;
    nodes.push(el("p", { class: "change-quality" }, [el("strong", { text: "Quality, " + group.label + ": " }), el("span", { text: group.verdict })]));
    var box = el("details", { class: "disclosure" });
    box.appendChild(el("summary", { text: "Every quality signal (" + group.before_runs + " runs before, " + group.after_runs + " after)" }));
    box.appendChild(
      simpleTable(
        [{ label: "Signal" }, { label: "Before" }, { label: "After" }, { label: "Verdict" }],
        (group.signals || []).map(function (s) {
          return [s.label, s.before_text + " (" + s.before_counts + ")", s.after_text + " (" + s.after_counts + ")", s.verdict];
        })
      )
    );
    nodes.push(box);
  });
  if (unjudged.length) {
    nodes.push(
      el("p", { class: "change-quality notes" }, [
        el("strong", { text: "Quality: " }),
        el("span", {
          text:
            "too few runs yet to judge " +
            unjudged
              .map(function (group) {
                return group.label + " (" + group.before_runs + " before, " + group.after_runs + " after)";
              })
              .join(", ") +
            ". Each needs at least " + unjudged[0].min_runs + " runs on each side.",
        }),
      ])
    );
  }
  return nodes;
}

// -- undoing it ------------------------------------------------------------------

function undoNodes(change) {
  if (change.source === "apply" && change.backup_ts && !change.reverted) {
    return [el("p", { class: "notes", text: "To undo it:" }), codeBlockWithCopy(cli("apply --revert " + change.backup_ts), "Command", "undoing " + change.label)];
  }
  var levelChange =
    change.source === "capture" &&
    (change.changes || []).filter(function (c) {
      return c.key === "capture.level" && c.old;
    })[0];
  if (levelChange) {
    return [
      el("p", { class: "notes" }, [el("span", { text: "To change it back, use " }), captureLink(), el("span", { text: " or:" })]),
      codeBlockWithCopy(levelChange.old === "off" ? cli("capture off") : cli("capture level " + levelChange.old), "Command", "undoing " + change.label),
    ];
  }
  return [];
}

// -- a change's card -------------------------------------------------------------

function changeWhere(change) {
  return change.project ? "In " + (change.project_name ? projectName(change.project_name) : "one project") + " only" : "Every project";
}

// One change: what changed and where, its reading, the measures before
// against after, what it saved so far, quality and the way back.
// opts.compact (the Overview): the lead measure and the saving only.
function changeCard(item, opts) {
  var change = item.change || {};
  var measures = item.measures || [];
  var lead = measures[0] || null;
  var card = el("article", { class: "change-card" + (opts.compact ? " is-compact" : ""), "data-day": changeDay(change) });
  var head = el("div", { class: "change-card-head" }, [
    el("h3", { class: "change-card-title", text: change.label + (change.reverted ? " (since undone)" : "") }),
    item.enough && lead ? readingBadge(lead) : chip("Too early to tell", { class: "change-early" }),
  ]);
  card.appendChild(head);
  var what = modelNames(change.summary || (change.keys || []).join(", "));
  card.appendChild(el("p", { class: "change-meta", text: [shortTs(change.ts), changeWhere(change), what].filter(Boolean).join(" · ") }));

  if (!item.enough) {
    // The one "not enough data yet" box, with how many sessions it has
    // and needs (item.gate).
    card.appendChild(emptyState(item.verdict, item.gate));
    return card;
  }

  var shown = opts.compact ? (lead ? [lead] : []) : measures;
  if (shown.length) card.appendChild(el("div", { class: "change-measures" }, shown.map(measureRow)));
  var saved = savedLine(item.without);
  if (saved) card.appendChild(saved);
  if (opts.compact) return card;
  var perKey = perKeyTable(item.without);
  if (perKey) card.appendChild(perKey);
  qualityNodes(item).forEach(function (node) {
    card.appendChild(node);
  });
  undoNodes(change).forEach(function (node) {
    card.appendChild(node);
  });
  return card;
}

// Every change in /api/impact's data, newest first, as cards. opts:
// compact (the lead measure and the saving only) and limit (how many).
// Returns how many changes there are in all.
export function renderChangeCards(container, data, opts) {
  opts = opts || {};
  var changes = (data && data.changes) || [];
  if (!changes.length) {
    container.appendChild(
      emptyState(
        "No changes recorded yet.",
        null,
        "When you apply a profile or fix, or change a setting or model, this compares the sessions before and after it."
      )
    );
    return 0;
  }
  var list = el("div", { class: "change-cards" });
  changes.slice(0, opts.limit || changes.length).forEach(function (item) {
    list.appendChild(changeCard(item, opts));
  });
  container.appendChild(list);
  if (!opts.compact && data.caveat) container.appendChild(el("p", { class: "notes" }, prose(data.caveat)));
  return changes.length;
}

// -- the timeline's changes --------------------------------------------------------

// The changes to mark on the timeline, from /api/impact's body: each
// leads to its card below. One made in another project than the one
// picked says which.
function timelineChanges(impactBody) {
  var rows = (impactBody && impactBody.ok === true && impactBody.data && impactBody.data.changes) || [];
  return rows.map(function (row) {
    var change = row.change || {};
    var day = changeDay(change);
    var elsewhere = change.project && change.project_name !== state.project;
    return {
      day: day,
      label: (change.label || "A settings change") + (elsewhere && change.project_name ? " (" + projectName(change.project_name) + ")" : ""),
      open: function () {
        goTo("changes", { params: { day: day } });
      },
    };
  });
}

// -- estimates ----------------------------------------------------------------------

// EST-P4/P8: did a saving estimate come true? Rows are
// backtest.present()'s own display-ready shape (predicted_text,
// measured_text and verdict_text are already server-formatted
// sentences) -- this just lays them out in a table, no client-side
// money or verdict logic, per docs/ui.md's "server formats, dashboard
// shows" rule.
function renderBacktest(data, container) {
  var predictions = (data && data.predictions) || [];
  if (!predictions.length) {
    container.appendChild(
      emptyState("No estimates logged yet. Each estimated effect Profiles shows is logged, then checked here once enough sessions after a matching change arrive.")
    );
    return;
  }
  container.appendChild(
    el("p", {
      class: "notes",
      text: "Estimated effects from Profiles, checked against what happened after a matching change.",
    })
  );
  container.appendChild(
    simpleTable(
      [{ label: "Change" }, { label: "When" }, { label: "Estimated" }, { label: "Measured" }, { label: "Verdict" }],
      predictions.map(function (row) {
        return [row.agent ? row.agent + ": " + row.measure_key : row.measure_key, shortTs(row.ts), row.predicted_text, row.measured_text || "—", row.verdict_text];
      })
    )
  );
}

// -- the page ------------------------------------------------------------------------

// A part of the page: an h2 (with "All time" when it covers every change
// ever, whatever the window) and its body.
function changesSection(panel, title, allTime) {
  var section = el("section", { class: "report-section" }, [
    el("div", { class: "block-head" }, [
      el("h2", { class: "section-title", text: title }),
      allTime ? chip(state.project ? "All time, all projects" : "All time", { icon: "clock", class: "all-time-chip" }) : null,
    ]),
  ]);
  var body = el("div");
  section.appendChild(body);
  panel.appendChild(section);
  return body;
}

var pageRun = 0;

export function renderChanges(panel) {
  var run = ++pageRun;
  clear(panel);
  viewIntro(panel, "changes");

  var chartHost = el("div", { class: "changes-chart" });
  panel.appendChild(chartHost);
  if (!holdChart(chartHost, "change-timeline", { slot: "changes" })) chartHost.appendChild(loadingNode("Loading cost per reply", "chart"));

  var cardsHost = changesSection(panel, "Each change, before and after", true);
  var backtestHost = changesSection(panel, "Did your estimates come true?", true);
  loadInto(backtestHost, "/api/backtest", renderBacktest, { skeleton: "rows" });

  // Every amount follows the billing mode, which the report sets: the
  // drawing waits for it (a failed report still draws, in list price).
  var unitsLoad = loadReport().then(null, function () {
    return null;
  });
  var impactLoad = fetchJson("/api/impact");
  var dailyLoad = fetchJson(withWindow("/api/daily-usage") + "&split=agent");
  // The window's first and last day, as the service counts them.
  var summaryLoad = fetchJson(withWindow("/api/summary"));
  cardsHost.appendChild(loadingNode("Loading your changes", "rows"));

  var cardsDrawn = Promise.all([impactLoad, unitsLoad]).then(function (loaded) {
    var result = loaded[0];
    if (run !== pageRun) return;
    clear(cardsHost);
    var body = result.body;
    if (!body || body.ok !== true) {
      cardsHost.appendChild(errorNotice(body && body.error));
      return;
    }
    renderChangeCards(cardsHost, body.data, {});
  });

  Promise.all([dailyLoad, impactLoad, unitsLoad, summaryLoad]).then(function (loaded) {
    if (run !== pageRun) return;
    var daily = loaded[0].body;
    if (!daily || daily.ok !== true) {
      chartError(chartHost, "change-timeline", daily && daily.error, function () {
        goTo("changes", { force: true });
      }, { slot: "changes", titleTag: "h2" });
      return;
    }
    renderChart(
      chartHost,
      "change-timeline",
      Object.assign({ rows: daily.data || [], changes: timelineChanges(loaded[1].body) }, windowSpan(loaded[3].body)),
      {
        slot: "changes",
        titleTag: "h2",
        // A day leads to the sessions active on it.
        open: function (day) {
          goTo("spend/sessions", { params: { day: day } });
        },
      }
    );
  });

  // ?day=: a change marked on a chart. That day's card (the first, so the
  // latest that day) is brought into view and pulsed.
  onParams("changes", function (params) {
    if (!/^\d{4}-\d\d-\d\d$/.test(params.day || "")) return;
    cardsDrawn.then(function () {
      var card = cardsHost.querySelector('.change-card[data-day="' + params.day + '"]');
      if (card) pulseNode(card, "block-target");
    });
  });
}

// A link to the page, for the Overview's summary of it.
export function changesLink(count) {
  return pageLink("changes", count > 1 ? "See all " + count + " changes" : "See the change in full");
}

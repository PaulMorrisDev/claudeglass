/* claudeglass service UI: page-changes.js
 *
 * Your changes: did the changes you made work, and by how much? At the
 * top, cost per reply by day with each change marked and a flat line at
 * the average between one change and the next (chart 9). Under it, a
 * card per change (/api/impact): the measures that change should move,
 * before against after, how sure the difference is, what it saved so
 * far, whether quality held, and how to undo it. Then whether the
 * estimates Profiles showed came true (/api/backtest). The changes follow
 * the window and the picked project; the estimates, which aren't kept per
 * project, follow only the window.
 *
 * A card leads with the measure the ratio test is surest of (item.lead,
 * which impact.py picks by the test's reading, not by order), and says so
 * when the mix of sessions moved between its two sides (item.mix): cost
 * per session then compares different jobs, and for a change to capture,
 * coaching or the feedback prompts it is read last (measure.demoted).
 *
 * The Overview's "Did your changes work?" draws the latest cards in
 * short (renderChangeCards with opts.compact), the ones that can be
 * judged first (opts.judgedFirst).
 */

import { clear, cli, el, goTo, onParams, state } from "./core.js";
import { fetchJson, loadInto, loadingLabel, loadReport, withWindow } from "./api.js";
import { modelNames, moneyText, projectName, shortTs, signedPercent, windowWhen } from "./format.js";
import { button, chip, codeBlockWithCopy, emptyState, errorNotice, loadingNode, prose } from "./ui.js";
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

// The measure that leads a card: the one /api/impact names (the one its
// ratio test is surest of), else the first it lists.
function leadMeasure(item) {
  var measures = item.measures || [];
  var named = measures.filter(function (measure) {
    return measure.key === item.lead;
  })[0];
  return named || measures[0] || null;
}

// Two bars on one scale, the before in the quieter colour, then the
// change and how sure it is. The figures are the server's text, in the
// billing mode's units. mixMoved: the mix of sessions changed, which a
// demoted measure (cost per session of a change that isn't about cost)
// says is why it is read last.
function measureRow(measure, mixMoved) {
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
  return el("div", { class: "change-measure" + (measure.demoted ? " is-demoted" : "") }, [
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
    measure.demoted && mixMoved ? el("p", { class: "notes change-demoted", text: "Read last: the mix of sessions changed." }) : null,
  ]);
}

// The mix of sessions moved between the two sides (impact.session_mix):
// the server's sentence says which kind and by how much.
function mixNote(item) {
  var mix = item.mix;
  if (!mix || !mix.flagged) return null;
  return el("div", { class: "change-mix" }, [
    chip("Session mix changed", { icon: "warning", tone: "warn", class: "change-mix-chip" }),
    el("span", { class: "change-mix-text", text: mix.text }),
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
  var lead = leadMeasure(item);
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

  var mixMoved = !!(item.mix && item.mix.flagged);
  var mix = mixNote(item);
  if (mix) card.appendChild(mix);
  var shown = opts.compact ? (lead ? [lead] : []) : measures;
  if (shown.length) {
    card.appendChild(
      el(
        "div",
        { class: "change-measures" },
        shown.map(function (measure) {
          return measureRow(measure, mixMoved);
        })
      )
    );
  }
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

// How many cards Your changes draws before the older ones fold behind a
// button.
var CARDS_SHOWN = 10;

var NO_CHANGES_NEXT = "When you apply a profile or fix, or change a setting or model, this compares the sessions before and after it.";

// "Since my last change" with no change recorded has nowhere to start, and
// the service answers 400 for it (one about the project gives way to every
// project on its own). That reads as nothing to show, not as a failure.
function noChangeYet(body) {
  var error = body && body.ok !== true ? body.error : null;
  return state.window === "change" && !!error && error.code === "bad_request" && String(error.message).indexOf("'project'") === -1;
}

// The Overview's pick from a window's changes (newest first, as
// /api/impact lists them): the newest `limit` that can be judged, and the
// newer ones still waiting for sessions after them. A newer change short
// only of sessions before it isn't too new, so it isn't counted; it stays
// on Your changes. Timestamps are all YYYY-MM-DDTHH:MM:SSZ, so they compare
// as text.
export function judgedFirst(changes, limit, minSessions) {
  var judged = changes
    .filter(function (item) {
      return item.enough;
    })
    .slice(0, limit);
  if (!judged.length) return { judged: [], waiting: [] };
  var newest = String((judged[0].change || {}).ts || "");
  var waiting = changes.filter(function (item) {
    return !item.enough && item.after_sessions < minSessions && String((item.change || {}).ts || "") > newest;
  });
  return { judged: judged, waiting: waiting };
}

// "3 newer changes are too new to judge yet": the count leads to the
// newest of them on Your changes.
function waitingLine(waiting, need) {
  var count = waiting.length;
  var link = pageLink("changes", count === 1 ? "1 newer change" : count + " newer changes", { day: changeDay(waiting[0].change) });
  return el("p", { class: "notes change-waiting" }, [
    link,
    count === 1 ? " is too new to judge yet: it needs " + need + " sessions after it." : " are too new to judge yet: each needs " + need + " sessions after it.",
  ]);
}

// The cards past the fold, brought into view. focus: a click on the
// button moves on to the first of them, as the button is gone.
function showOlderChanges(list, focus) {
  var folded = list.querySelectorAll(".change-card[hidden]");
  for (var i = 0; i < folded.length; i++) folded[i].hidden = false;
  var more = list.querySelector(".change-cards-more");
  if (more) list.removeChild(more);
  if (focus && folded.length) {
    folded[0].tabIndex = -1;
    folded[0].focus();
  }
}

function olderButton(list, count) {
  var label = "Show " + count + " older " + (count === 1 ? "change" : "changes");
  return el("div", { class: "change-cards-more" }, [
    button(label, {
      variant: "quiet",
      action: function () {
        showOlderChanges(list, true);
      },
    }),
  ]);
}

// Every change in /api/impact's data, newest first, as cards. opts:
// compact (the lead measure and the saving only), limit (how many),
// judgedFirst (the Overview: the newest changes that can be judged lead,
// and the newer ones still waiting for sessions become one line above
// them; with none judged, the newest are drawn as they are, each with
// what it is waiting for), foldAfter (how many cards to draw before the
// older ones fold behind a button) and noOlder (nothing is recorded, so
// an empty window has no older changes to point to). Returns how many
// changes there are in all.
export function renderChangeCards(container, data, opts) {
  opts = opts || {};
  var changes = (data && data.changes) || [];
  if (!changes.length) {
    if (state.window !== "all" && !opts.noOlder) {
      // Older ones may be recorded: say where to find them.
      var where = state.project ? " in " + projectName(state.project) : "";
      var older = opts.compact
        ? el("span", {}, ["See older ones on ", pageLink("changes", "Your changes", { w: "all" }), " under All time."])
        : el("span", {}, ["Pick ", pageLink("changes", "All time", { w: "all" }), " to see older ones."]);
      container.appendChild(emptyState("No changes" + where + " " + windowWhen(state.window) + ".", null, older));
      return 0;
    }
    if (state.project) {
      container.appendChild(emptyState("No changes in " + projectName(state.project) + " yet.", null, NO_CHANGES_NEXT));
      return 0;
    }
    container.appendChild(
      emptyState(
        "No changes recorded yet.",
        null,
        NO_CHANGES_NEXT
      )
    );
    return 0;
  }
  var shown = changes.slice(0, opts.limit || changes.length);
  var waiting = [];
  if (opts.judgedFirst) {
    var picked = judgedFirst(changes, opts.limit || changes.length, data.min_sessions);
    if (picked.judged.length) {
      shown = picked.judged;
      waiting = picked.waiting;
    }
  }
  var list = el("div", { class: "change-cards" });
  if (waiting.length) list.appendChild(waitingLine(waiting, data.min_sessions));
  shown.forEach(function (item, index) {
    var card = changeCard(item, opts);
    if (opts.foldAfter && index >= opts.foldAfter) card.hidden = true;
    list.appendChild(card);
  });
  if (opts.foldAfter && shown.length > opts.foldAfter) list.appendChild(olderButton(list, shown.length - opts.foldAfter));
  container.appendChild(list);
  if (!opts.compact && data.caveat) container.appendChild(el("p", { class: "notes" }, prose(data.caveat)));
  return changes.length;
}

// -- the timeline's changes --------------------------------------------------------

// The changes to mark on the timeline, from /api/impact's body: each
// leads to its card below. Across every project, one made in a single
// project says which; with a project picked, every change is its own or
// everyone's.
function timelineChanges(impactBody) {
  var rows = (impactBody && impactBody.ok === true && impactBody.data && impactBody.data.changes) || [];
  return rows.map(function (row) {
    var change = row.change || {};
    var day = changeDay(change);
    var inProject = !state.project && change.project && change.project_name;
    return {
      day: day,
      label: (change.label || "A settings change") + (inProject ? " (" + projectName(change.project_name) + ")" : ""),
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
//
// noOlder: no change is recorded at all, so "Since my last change" has no
// start. Estimates may still be logged (saving or copying a change's
// command logs one), so it points to All time rather than say there are none.
function renderBacktest(data, container, noOlder) {
  var predictions = (data && data.predictions) || [];
  if (!predictions.length) {
    if (noOlder) {
      container.appendChild(
        emptyState("No change recorded yet, so this window has nowhere to start.", null, el("span", {}, ["Pick ", pageLink("changes", "All time", { w: "all" }), " to see every estimate logged."]))
      );
      return;
    }
    if (state.window !== "all") {
      container.appendChild(
        emptyState("No estimates logged " + windowWhen(state.window) + ".", null, el("span", {}, ["Pick ", pageLink("changes", "All time", { w: "all" }), " to see older ones."]))
      );
      return;
    }
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

// A part of the page: an h2 and its body. allProjects: the figures cover
// every project whatever the picker says (the estimates aren't kept per
// project), so a picked project gets a chip saying so.
function changesSection(panel, title, allProjects) {
  var section = el("section", { class: "report-section" }, [
    el("div", { class: "block-head" }, [
      el("h2", { class: "section-title", text: title }),
      allProjects && state.project ? chip("All projects", { icon: "folder", class: "all-time-chip" }) : null,
    ]),
  ]);
  var body = el("div");
  section.appendChild(body);
  panel.appendChild(section);
  return body;
}

var pageRun = 0;

// The estimates, for the window. They are every project's, so "Since my
// last change" starts at the newest change in any project, whatever the
// cards above say for the one picked; with no change recorded anywhere the
// service answers 400, which reads as nothing to show.
function loadEstimates(host) {
  var url = withWindow("/api/backtest");
  if (state.window !== "change") return loadInto(host, url, renderBacktest, { skeleton: "rows" });
  host.appendChild(loadingNode(loadingLabel(url), "rows"));
  return fetchJson(url).then(function (result) {
    if (!host.isConnected) return null;
    var body = result.body;
    if (noChangeYet(body)) {
      clear(host);
      renderBacktest(null, host, true);
      return null;
    }
    // A failure: loadInto says so, with a way to try again.
    if (!body || body.ok !== true) return loadInto(host, url, renderBacktest, { skeleton: "rows" });
    clear(host);
    renderBacktest(body.data, host);
    return body.data;
  });
}

export function renderChanges(panel) {
  var run = ++pageRun;
  clear(panel);
  viewIntro(panel, "changes");

  var chartHost = el("div", { class: "changes-chart" });
  panel.appendChild(chartHost);
  if (!holdChart(chartHost, "change-timeline", { slot: "changes" })) chartHost.appendChild(loadingNode("Loading cost per reply", "chart"));

  var cardsHost = changesSection(panel, "Each change, before and after");
  var backtestHost = changesSection(panel, "Did your estimates come true?", true);

  // Every amount follows the billing mode, which the report sets: the
  // drawing waits for it (a failed report still draws, in list price).
  var unitsLoad = loadReport().then(null, function () {
    return null;
  });
  var impactLoad = fetchJson(withWindow("/api/impact"));
  loadEstimates(backtestHost);
  var dailyLoad = fetchJson(withWindow("/api/daily-usage") + "&split=agent");
  // The window's first and last day, as the service counts them.
  var summaryLoad = fetchJson(withWindow("/api/summary"));
  cardsHost.appendChild(loadingNode("Loading your changes", "rows"));

  var cardsDrawn = Promise.all([impactLoad, unitsLoad]).then(function (loaded) {
    var result = loaded[0];
    if (run !== pageRun) return;
    clear(cardsHost);
    var body = result.body;
    if (noChangeYet(body)) {
      renderChangeCards(cardsHost, null, { noOlder: true });
      return;
    }
    if (!body || body.ok !== true) {
      cardsHost.appendChild(errorNotice(body && body.error));
      return;
    }
    renderChangeCards(cardsHost, body.data, { foldAfter: CARDS_SHOWN });
  });

  Promise.all([dailyLoad, impactLoad, unitsLoad, summaryLoad]).then(function (loaded) {
    if (run !== pageRun) return;
    var daily = loaded[0].body;
    if (noChangeYet(daily)) {
      // No days to draw from, as the cards and the estimates below say too.
      renderChart(chartHost, "change-timeline", { rows: [] }, {
        slot: "changes",
        titleTag: "h2",
        empty: state.project ? "No change recorded for this project yet, so this window has nowhere to start." : "No change recorded yet, so this window has nowhere to start.",
      });
      return;
    }
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
  // latest that day) is brought into view and pulsed, out from the fold
  // if it was folded.
  onParams("changes", function (params) {
    if (!/^\d{4}-\d\d-\d\d$/.test(params.day || "")) return;
    cardsDrawn.then(function () {
      var card = cardsHost.querySelector('.change-card[data-day="' + params.day + '"]');
      if (card && card.hidden) showOlderChanges(cardsHost.querySelector(".change-cards"), false);
      if (card) pulseNode(card, "block-target");
    });
  });
}

// A link to the page, for the Overview's summary of it.
export function changesLink(count) {
  return pageLink("changes", count > 1 ? "See all " + count + " changes" : "See the change in full");
}

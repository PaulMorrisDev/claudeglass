/* claudeglass service UI: page-spend.js
 *
 * The Spend page: Usage, Savings and Sessions.
 */

import { clear, el, goTo, onParams, state } from "./core.js";
import { compactNumber, formatDuration, fullValue, moneyParts, projectName, shortTs, thousands } from "./format.js";
import { fetchJson, loadInto, loadReport, postJson, withWindow } from "./api.js";
import { button, drawer, errorNotice, loadingNode, prose, tile, tileRow, toast } from "./ui.js";
import { dataGrid, renderMappedSections, renderReportBackedSection } from "./grid.js";
import { replaceParams, viewIntro } from "./links.js";
import { chartError, dayLabel, holdChart } from "./charts.js";
import { dailyChanges, modeColour, renderChart, savingsLevers, sessionContextChart, windowSpan } from "./charts-types.js";

// ======================================================================
// Spend, Sessions: which sessions stand out (chart 4), over the list. A
// stretch of time picked on the chart, or a day picked on a daily spend
// chart (?day=YYYY-MM-DD, a local day as the service counts it), narrows
// the list.
// ======================================================================

// The window's sessions, newest first, up to this many.
var SESSIONS_LIMIT = 2000;

// run: the draw on screen, so an older draw's late answer is dropped.
var sessionsView = { run: 0, rows: [], range: null, day: null };

function validDay(value) {
  return /^\d{4}-\d\d-\d\d$/.test(String(value || "")) && !isNaN(Date.parse(value + "T00:00:00Z")) ? value : null;
}

// The sessions the list shows: those started in the picked stretch, or
// active on the picked day. The service names each session's first and
// last day (/api/sessions: first_day, last_day, its local days), so the
// day matches the chart's column; an older service gives the timestamps,
// whose UTC dates stand in.
function shownSessions() {
  var rows = sessionsView.rows;
  if (sessionsView.range) {
    var from = sessionsView.range[0];
    var to = sessionsView.range[1];
    return rows.filter(function (row) {
      var start = Date.parse(row.first_ts);
      return start >= from && start <= to;
    });
  }
  if (sessionsView.day) {
    var day = sessionsView.day;
    return rows.filter(function (row) {
      var first = row.first_day || String(row.first_ts || "").slice(0, 10);
      var last = row.last_day || (row.last_ts ? String(row.last_ts).slice(0, 10) : first);
      return first <= day && last >= day;
    });
  }
  return rows;
}

export function renderSessions(panel) {
  var run = ++sessionsView.run;
  function current() {
    return run === sessionsView.run;
  }
  clear(panel);
  viewIntro(panel, "spend/sessions");
  sessionsView.rows = [];
  sessionsView.range = null;
  sessionsView.day = validDay(state.params.day);

  var chartHost = el("div", { class: "sessions-chart" });
  var filterHost = el("div", { class: "sessions-filter", "aria-live": "polite" });
  var tableContainer = el("div", { id: "sessions-table" });
  var sectionContainer = el("div", { id: "sessions-sections" });
  panel.appendChild(chartHost);
  panel.appendChild(filterHost);
  panel.appendChild(tableContainer);
  panel.appendChild(sectionContainer);

  loadReport().then(function (result) {
    if (!current()) return;
    if (result.error) {
      sectionContainer.appendChild(errorNotice(result.error));
      return;
    }
    renderMappedSections(result.report, "spend/sessions", sectionContainer);
  });

  function drawScatter(fresh) {
    renderChart(chartHost, "session-outliers", sessionsView.rows, {
      slot: "sessions",
      titleTag: "h2",
      fresh: fresh,
      open: function (row) {
        openSessionDrawer(row.id);
      },
      brushed: function (range) {
        if (!current()) return;
        sessionsView.range = range;
        // A stretch picked on the chart replaces a day picked elsewhere.
        if (range && sessionsView.day) {
          sessionsView.day = null;
          replaceParams(Object.assign({}, state.params, { day: null }));
        }
        drawList();
      },
    });
  }

  function drawFilter(shown) {
    clear(filterHost);
    var what = null;
    if (sessionsView.range) {
      what = "Sessions started from " + shortTs(new Date(sessionsView.range[0]).toISOString()) + " to " + shortTs(new Date(sessionsView.range[1]).toISOString());
    } else if (sessionsView.day) {
      what = "Sessions active on " + dayLabel(sessionsView.day, true);
    }
    if (!what) return;
    var showAll = button("Show all sessions", { variant: "quiet" });
    showAll.addEventListener("click", function () {
      var hadRange = !!sessionsView.range;
      sessionsView.range = null;
      if (sessionsView.day) {
        sessionsView.day = null;
        replaceParams(Object.assign({}, state.params, { day: null }));
      }
      // The chart drops its picked stretch by drawing afresh.
      if (hadRange) drawScatter(true);
      drawList();
      // Focus goes to the list it widened (its scroller takes focus).
      var list = tableContainer.querySelector(".grid-scroll");
      if (list) list.focus({ preventScroll: true });
    });
    filterHost.appendChild(
      el("div", { class: "filter-row" }, [el("p", { class: "notes", text: what + ": " + thousands(shown) + " of " + thousands(sessionsView.rows.length) + "." }), showAll])
    );
  }

  function drawList() {
    if (!current()) return;
    var shown = shownSessions();
    drawFilter(shown.length);
    clear(tableContainer);
    renderSessionsTable(shown, tableContainer, sessionsView.rows.length >= SESSIONS_LIMIT);
  }

  // A day picked on a daily spend chart while this view is open.
  onParams("spend/sessions", function (params) {
    var day = validDay(params.day);
    if (day === sessionsView.day || !current()) return;
    sessionsView.day = day;
    if (day && sessionsView.range) {
      sessionsView.range = null;
      drawScatter(true);
    }
    drawList();
  });

  if (!holdChart(chartHost, "session-outliers", { slot: "sessions" })) chartHost.appendChild(loadingNode("Loading sessions", "chart"));
  loadInto(
    tableContainer,
    withWindow("/api/sessions?limit=" + SESSIONS_LIMIT),
    function (rows) {
      if (!current()) return;
      sessionsView.rows = rows || [];
      drawList();
      drawScatter(false);
    },
    { skeleton: "rows" }
  ).then(function (rows) {
    if (rows !== null || !current()) return;
    // The list says what went wrong; the chart says it too.
    if (!chartHost.querySelector(".chart.is-refreshing")) {
      chartError(chartHost, "session-outliers", null, function () {
        goTo("spend/sessions", { force: true });
      }, { slot: "sessions", titleTag: "h2" });
    }
  });
}

function timeCell(row, value) {
  return el("span", { class: "nowrap", text: shortTs(value) });
}

var SESSION_COLUMNS = [
  {
    key: "id",
    label: "Session",
    kind: "str",
    // The short id; the whole one on hover.
    render: function (row) {
      return el("span", { class: "mono-id", title: String(row.id || ""), text: String(row.id || "").slice(0, 8) });
    },
  },
  { key: "slug", label: "Project", kind: "str" },
  { key: "first_ts", label: "Started", kind: "str", render: timeCell },
  { key: "last_ts", label: "Last reply", kind: "str", render: timeCell },
  { key: "span_s", label: "Span", kind: "secs" },
  { key: "mode", label: "Mode", kind: "str" },
  { key: "purpose", label: "Purpose", kind: "str" },
  { key: "entrypoint", label: "Started from", kind: "str" },
  { key: "total_cost", label: "Cost", kind: "money" },
  { key: "total_tokens", label: "Tokens", kind: "tokens" },
];

// Shown only when some sessions ran somewhere else, such as WSL.
var SOURCE_COLUMN = { key: "source", label: "Where", kind: "str" };

// capped: the window has more sessions than the list holds.
function renderSessionsTable(rows, container, capped) {
  var columns = SESSION_COLUMNS.slice();
  var elsewhere = rows.some(function (row) {
    return row.source && row.source !== "This computer";
  });
  if (elsewhere) columns.splice(2, 0, SOURCE_COLUMN);
  container.appendChild(
    dataGrid({
      id: "sessions-list-table",
      caption: "Sessions",
      columns: columns,
      rows: rows,
      lead: ["slug", "source", "last_ts", "span_s", "mode", "total_cost", "total_tokens"],
      rowKey: function (row) {
        return row.id;
      },
      rowAction: {
        label: function (row) {
          return "Open session " + String(row.id || "").slice(0, 8);
        },
        run: function (row) {
          openSessionDrawer(row.id);
        },
      },
      // A row and its dot on the chart light up together.
      link: {
        scope: "session",
        key: function (row) {
          return row.id;
        },
      },
      swatch: function (row) {
        return modeColour(row.mode);
      },
      empty: "No sessions in this window.",
      emptyNext: "Pick a longer window to see older ones.",
    })
  );
  if (capped) {
    container.appendChild(
      el("p", { class: "notes", text: "Showing the newest " + thousands(SESSIONS_LIMIT) + " sessions in this window. Pick a shorter window to see the rest." })
    );
  }
}

// A session's detail slides in from the right, over the list (or over
// any page: search opens recent sessions from anywhere).
export function openSessionDrawer(sessionId) {
  drawer({
    title: "Session " + String(sessionId).slice(0, 8),
    wide: true,
    fill: function (body) {
      renderSessionDetail(body, sessionId);
    },
  });
}

function renderSessionDetail(container, sessionId) {
  clear(container);
  container.appendChild(loadingNode("Loading the session", "lines"));
  fetchJson("/api/session/" + encodeURIComponent(sessionId)).then(function (result) {
    clear(container);
    var body = result.body;
    if (!body || body.ok !== true) {
      container.appendChild(errorNotice(body && body.error));
      return;
    }
    buildSessionDetail(container, body.data);
  });
}

// -- your rating (Setup, Capture: dashboard rating) -> POST /api/sessions/<id>/feedback --
function buildSessionRating(container, session) {
  var saved = session.feedback || {};
  var form = el("fieldset", { class: "session-rating" });
  form.appendChild(el("legend", { text: "Rate this session" }));
  form.appendChild(el("p", { class: "notes", text: "The /cg-feedback questions as checkboxes. Kept in ClaudeGlass's own store, so it costs no tokens." }));
  var inputs = {};
  session.feedback_questions.forEach(function (q) {
    var group = el("div", { class: "rating-question", role: "group", "aria-label": q.question });
    group.appendChild(el("p", { class: "rating-label", text: q.question + (q.multi ? " (tick any)" : "") }));
    var chosen = q.multi ? saved[q.key] || [] : saved[q.key] ? [saved[q.key]] : [];
    inputs[q.key] = [];
    q.options.forEach(function (opt) {
      var id = "rate-" + q.key + "-" + opt.word;
      var box = el("input", { type: q.multi ? "checkbox" : "radio", id: id, name: "rate-" + q.key, value: opt.word, checked: chosen.indexOf(opt.word) !== -1 });
      inputs[q.key].push(box);
      group.appendChild(el("span", { class: "rating-option" }, [box, el("label", { for: id, text: opt.label })]));
    });
    form.appendChild(group);
  });
  var save = button("Save rating", { variant: "primary" });
  var reset = button("Clear", { variant: "quiet" });
  var status = el("span", { class: "notes", role: "status" });
  if (saved.set_at) status.textContent = "Rated " + saved.set_at.slice(0, 10) + ".";
  form.appendChild(el("div", { class: "rating-actions" }, [save, reset, status]));

  function send(clearAll) {
    var payload = {};
    session.feedback_questions.forEach(function (q) {
      var ticked = clearAll ? [] : inputs[q.key].filter(function (box) {
        return box.checked;
      }).map(function (box) {
        return box.value;
      });
      payload[q.key] = q.multi ? ticked : ticked[0] || null;
    });
    save.disabled = reset.disabled = true;
    status.textContent = "Saving…";
    postJson("/api/sessions/" + encodeURIComponent(session.id) + "/feedback", payload).then(function (res) {
      save.disabled = reset.disabled = false;
      if (!res.body || res.body.ok !== true) {
        status.textContent = "";
        form.appendChild(errorNotice(res.body && res.body.error));
        return;
      }
      toast(clearAll ? "Rating cleared." : "Rating saved.");
      renderSessionDetail(container, session.id);
    });
  }
  save.addEventListener("click", function () {
    send(false);
  });
  reset.addEventListener("click", function () {
    send(true);
  });
  return form;
}

function buildSessionDetail(container, session) {
  var wrap = el("div", { class: "session-detail" });
  wrap.appendChild(el("p", { class: "mono-id session-full-id", text: session.id }));

  var cost = moneyParts(session.total_cost);
  wrap.appendChild(
    tileRow(
      [
        tile({ label: "Cost", value: cost.value, unit: cost.unit, hint: cost.secondary || null }),
        tile({
          label: "Tokens",
          value: el("span", { text: compactNumber(session.total_tokens || 0), title: fullValue(session.total_tokens, "tokens") || null }),
        }),
        tile({ label: "Span", value: formatDuration(session.span_s) }),
      ],
      { class: "metric-tiles-compact" }
    )
  );
  var facts = el("dl", { class: "fact-list" });
  [
    ["Project", el("span", { title: session.slug || "", text: projectName(session.slug) })],
    ["Where it ran", session.source || "This computer"],
    ["Kind of session", session.archetype || "-"],
    ["Billing", session.billing_mode === "subscription" ? "Pro or Max plan" : session.billing_mode === "api" ? "Pay per token (API)" : session.billing_mode || "-"],
    ["Profile", session.profile_id || "None"],
  ].forEach(function (pair) {
    facts.appendChild(el("dt", { text: pair[0] }));
    facts.appendChild(typeof pair[1] === "string" ? el("dd", { text: pair[1] }) : el("dd", null, [pair[1]]));
  });
  wrap.appendChild(facts);

  // -- "why was this session expensive?" (template sentences, no model) --
  var explainBox = el("div", { class: "session-explain" });
  wrap.appendChild(el("h3", { text: "Why did this session cost what it did?" }));
  wrap.appendChild(explainBox);
  loadInto(explainBox, "/api/session/" + encodeURIComponent(session.id) + "/explain", renderSessionExplain);

  // -- tag overrides (mode/purpose) -> POST /api/sessions/<id>/tags --
  var tagControls = el("div", { class: "tag-controls" });
  var modeLabel = el("label", { text: "Mode override" });
  var modeSelect = buildTagSelect(["", "interactive", "long-agentic", "overnight", "mixed"], (session.tags && session.tags.mode) || session.mode);
  modeLabel.appendChild(modeSelect);

  var purposeLabel = el("label", { text: "Purpose override" });
  var purposeSelect = buildTagSelect(
    ["", "local-llm-pipeline", "agent-fanout", "workflow-run", "review", "test-triage", "planning", "docs", "refactor", "general-dev"],
    (session.tags && session.tags.purpose) || session.purpose
  );
  purposeLabel.appendChild(purposeSelect);

  var saveTagsBtn = button("Save tags");
  var tagStatus = el("span", { class: "notes", role: "status" });
  tagControls.appendChild(modeLabel);
  tagControls.appendChild(purposeLabel);
  tagControls.appendChild(saveTagsBtn);
  tagControls.appendChild(tagStatus);
  wrap.appendChild(tagControls);

  saveTagsBtn.addEventListener("click", function () {
    saveTagsBtn.disabled = true;
    tagStatus.textContent = "Saving…";
    var updates = [];
    if (modeSelect.value) updates.push(["mode", modeSelect.value]);
    if (purposeSelect.value) updates.push(["purpose", purposeSelect.value]);
    if (!updates.length) {
      tagStatus.textContent = "Choose a mode or purpose first.";
      saveTagsBtn.disabled = false;
      return;
    }
    Promise.all(
      updates.map(function (pair) {
        return fetchJson("/api/sessions/" + encodeURIComponent(session.id) + "/tags", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ key: pair[0], value: pair[1] }),
        });
      })
    ).then(function (results) {
      saveTagsBtn.disabled = false;
      var failed = results.filter(function (r) {
        return !r.body || r.body.ok !== true;
      });
      if (failed.length) {
        tagStatus.textContent = "";
        tagControls.appendChild(errorNotice(failed[0].body && failed[0].body.error));
      } else {
        toast("Tags saved.");
        renderSessionDetail(container, session.id);
      }
    });
  });

  if (session.feedback_questions) wrap.appendChild(buildSessionRating(container, session));

  // -- chart 5: context size over turns with event markers ------------
  wrap.appendChild(sessionContextChart(session));

  // -- transcripts table (no path -- see docs/api.md's privacy rule) --
  wrap.appendChild(el("h3", { text: "Transcripts" }));
  wrap.appendChild(
    dataGrid({
      id: "session-transcripts",
      caption: "Transcripts in this session",
      columns: [
        { key: "kind", label: "Kind", kind: "str" },
        {
          key: "agent_type",
          label: "Agent type",
          kind: "str",
          value: function (t) {
            return t.agent_type || "Main session";
          },
        },
        {
          key: "spawn_depth",
          label: "Spawn depth",
          kind: "int",
          value: function (t) {
            return t.spawn_depth === undefined ? null : t.spawn_depth;
          },
        },
        {
          key: "parent_agent_id",
          label: "Started by",
          kind: "str",
          value: function (t) {
            return t.parent_agent_id || "-";
          },
        },
      ],
      rows: session.transcripts || [],
      empty: "No transcripts recorded for this session.",
    })
  );

  container.appendChild(wrap);
}

function renderSessionExplain(data, container) {
  container.appendChild(el("p", { class: "explain-headline" }, prose(data.headline)));
  // Each glossary term is explained once in the explanation: its first use.
  var seen = new Set();
  if (data.sentences && data.sentences.length) {
    container.appendChild(
      el(
        "ul",
        { class: "explain-sentences" },
        data.sentences.map(function (sentence) {
          return el("li", null, prose(sentence, seen));
        })
      )
    );
  }
  var split = (data.cost_split || []).filter(function (part) {
    return part.share_pct >= 0.05;
  });
  if (!split.length) return;
  container.appendChild(
    dataGrid({
      id: "session-cost-split",
      class: "explain-split",
      caption: "What the cost went on",
      sortable: false,
      bar: 1,
      columns: [
        { key: "label", label: "What the cost went on", kind: "str" },
        { key: "share_pct", label: "Share", kind: "pct" },
      ],
      rows: split,
    })
  );
  container.appendChild(el("p", { class: "notes", text: "Shares are worked out at list price for each model's token counts." }));
}

function buildTagSelect(options, current) {
  var select = el("select");
  options.forEach(function (opt) {
    select.appendChild(el("option", { value: opt, text: opt || "(no override)" }));
  });
  if (current && options.indexOf(current) !== -1) select.value = current;
  return select;
}

// ======================================================================
// Spend, Savings (v4 wiring round) -- carry, compaction_sim, model_swap
// and waste each have their own dedicated report-backed route, same
// as ttl in page-cache.js, fetched directly rather than waiting on the full
// report.json. The recommendation cards these sections' rules feed
// stay on Actions, Recommendations, same as every other section --
// this view is the tables only.
// ======================================================================

var SAVINGS_SECTIONS = [
  { url: "/api/carry", id: "savings-carry", empty: "No tool output to weigh in this window: its sessions kept none worth trimming." },
  { url: "/api/compaction-sim", id: "savings-compaction-sim", empty: "No conversation summaries to replay in this window." },
  { url: "/api/plan-handoff", id: "savings-plan-handoff", empty: "No approved plans in this window." },
  { url: "/api/model-swap", id: "savings-model-swap", empty: "No agent in this window could move to a cheaper model." },
  { url: "/api/waste", id: "savings-waste", empty: "No wasted replies in this window." },
];

export function renderSavings(panel) {
  clear(panel);
  viewIntro(panel, "spend/savings");
  // Chart 2: the four ways to save side by side, from the same figures
  // as the sections under it.
  var chartHost = el("div", { class: "savings-chart" });
  panel.appendChild(chartHost);
  if (!holdChart(chartHost, "savings-levers", { slot: "savings" })) chartHost.appendChild(loadingNode("Loading the ways to save", "chart"));
  var loads = SAVINGS_SECTIONS.map(function (spec) {
    var container = el("div", { id: spec.id });
    panel.appendChild(container);
    return loadInto(
      container,
      withWindow(spec.url),
      function (data, target) {
        renderReportBackedSection(data, target, spec.id, spec.empty, "Pick a longer window to include more sessions.");
      },
      { skeleton: "rows" }
    );
  });
  Promise.all(loads).then(function (sections) {
    // A newer draw of this view has its own chart.
    if (!chartHost.isConnected) return;
    if (
      sections.every(function (data) {
        return data === null;
      })
    ) {
      // Each section says what went wrong.
      clear(chartHost);
      return;
    }
    var tables = {};
    sections.forEach(function (data) {
      ((data && data.tables) || []).forEach(function (table) {
        tables[table.name] = table;
      });
    });
    renderChart(chartHost, "savings-levers", savingsLevers(tables), {
      slot: "savings",
      titleTag: "h2",
      empty: "No way to save showed up in this window.",
      emptyNext: "Pick a longer window to include more sessions.",
      // A lever leads to the row its figure comes from, in its section below.
      open: function (lever) {
        goTo("spend/savings", { params: { t: lever.source, row: lever.row } });
      },
    });
  });
}

// ======================================================================
// Spend, Usage (usage/elasticity/compactions/phases sections, cost by
// model + a raw /api/compactions list)
// ======================================================================

// The daily spend chart's split: who ran the turns, or which model.
var SPLITS = [
  { value: "agent", label: "Main session and subagents" },
  { value: "model", label: "Model" },
];

function usageSplit(value) {
  return value === "model" ? "model" : "agent";
}

// The split last picked: it outlives a redraw for a new window and a
// visit to another page, and goes back into the address.
var chosenSplit = "agent";

export function renderUsage(panel) {
  clear(panel);
  viewIntro(panel, "spend/usage");

  // Chart 1, the same as the Overview's, with a choice of split.
  var split = usageSplit(state.params.split || chosenSplit);
  keepSplitInAddress();
  var controls = el("div", { class: "filter-row chart-controls", role: "group", "aria-label": "Split daily spend by" }, [el("span", { class: "filter-label", text: "Split by" })]);
  var chips = SPLITS.map(function (option) {
    var chipButton = el("button", { type: "button", class: "filter-chip", "aria-pressed": option.value === split ? "true" : "false", "data-split": option.value, text: option.label });
    chipButton.addEventListener("click", function () {
      if (option.value === split) return;
      replaceParams(Object.assign({}, state.params, { split: option.value === "agent" ? null : option.value }));
      chooseSplit(option.value);
    });
    controls.appendChild(chipButton);
    return chipButton;
  });
  var chartHost = el("div", { class: "usage-chart" });
  panel.appendChild(controls);
  panel.appendChild(chartHost);
  var loads = {};
  // The changes made in the window (and the project), marked on the chart.
  var impactLoad = fetchJson(withWindow("/api/impact"));
  // The window's whole sessions, as the cost by model below counts them:
  // the chart's reading gives that figure too, and says why it differs.
  var summaryLoad = fetchJson(withWindow("/api/summary"));
  function drawDaily() {
    var wanted = split;
    if (!holdChart(chartHost, "daily-spend", { slot: "usage" })) chartHost.appendChild(loadingNode("Loading daily spend", "chart"));
    loads[wanted] = loads[wanted] || fetchJson(withWindow("/api/daily-usage") + "&split=" + wanted);
    Promise.all([loads[wanted], impactLoad, summaryLoad]).then(function (loaded) {
      // A newer draw of this view, or another split since, has its own.
      if (!chartHost.isConnected || wanted !== split) return;
      var daily = loaded[0].body;
      if (!daily || daily.ok !== true) {
        delete loads[wanted];
        chartError(chartHost, "daily-spend", daily && daily.error, drawDaily, { slot: "usage", titleTag: "h2" });
        return;
      }
      var summary = loaded[2].body;
      renderChart(
        chartHost,
        "daily-spend",
        Object.assign(
          { rows: daily.data || [], split: wanted, changes: dailyChanges(loaded[1].body), sessionsTotal: summary && summary.ok === true ? summary.data.total_cost : null },
          windowSpan(summary)
        ),
        {
          slot: "usage",
          titleTag: "h2",
          open: function (day) {
            goTo("spend/sessions", { params: { day: day } });
          },
        }
      );
    });
  }
  function keepSplitInAddress() {
    if (split !== "agent" && state.params.split !== split) replaceParams(Object.assign({}, state.params, { split: split }));
  }
  function chooseSplit(value) {
    split = usageSplit(value);
    chosenSplit = split;
    chips.forEach(function (chipButton) {
      chipButton.setAttribute("aria-pressed", chipButton.getAttribute("data-split") === split ? "true" : "false");
    });
    drawDaily();
  }
  // A link or Back to this view: an address without a split keeps the
  // one on screen.
  onParams("spend/usage", function (params) {
    if (!chartHost.isConnected) return;
    if (params.split && usageSplit(params.split) !== split) chooseSplit(params.split);
    else keepSplitInAddress();
  });
  drawDaily();

  var sectionContainer = el("div", { id: "usage-sections" });
  panel.appendChild(sectionContainer);
  sectionContainer.appendChild(loadingNode("Loading usage", "rows"));
  loadReport().then(function (result) {
    clear(sectionContainer);
    if (result.error) {
      sectionContainer.appendChild(errorNotice(result.error));
      return;
    }
    renderMappedSections(result.report, "spend/usage", sectionContainer);
  });

  // The compactions of the sessions the window counts, whole, as the
  // compactions section above counts them: the server lists them so.
  var compactions = el("section", { class: "report-section", id: "usage-compactions-section" });
  compactions.appendChild(el("h2", { class: "section-title", text: "Every conversation summary in this window's sessions" }));
  compactions.appendChild(
    el("p", {
      class: "section-intro",
      text: "Each time Claude Code summarised a conversation to make room (a compaction), newest first. A session counts whole, so one that began before this window lists all its summaries.",
    })
  );
  var compactionsContainer = el("div", { id: "usage-compactions" });
  compactions.appendChild(compactionsContainer);
  panel.appendChild(compactions);
  loadInto(compactionsContainer, withWindow("/api/compactions"), renderCompactionsRaw, { skeleton: "rows" });
}

var COMPACTION_COLUMNS = [
  {
    key: "ts",
    label: "Time",
    kind: "str",
    render: function (row) {
      return el("span", { class: "nowrap", text: shortTs(row.ts) });
    },
  },
  {
    key: "transcript_id",
    label: "Transcript",
    kind: "str",
    // The store's own opaque number, not a quantity.
    render: function (row) {
      return row.transcript_id === null || row.transcript_id === undefined ? "-" : "#" + row.transcript_id;
    },
  },
  { key: "pre_tokens", label: "Tokens before", kind: "tokens" },
  { key: "post_tokens", label: "Tokens after", kind: "tokens" },
  { key: "dropped_tokens", label: "Tokens dropped", kind: "tokens" },
  { key: "trigger", label: "Trigger", kind: "str" },
  { key: "join_delta_s", label: "Next reply after", kind: "secs" },
];

function renderCompactionsRaw(rows, container) {
  // The API lists them oldest first; the grid starts with the newest.
  rows = rows.slice().sort(function (a, b) {
    return String(b.ts || "").localeCompare(String(a.ts || ""));
  });
  // The list folds under its count, as a section's More tables do: the
  // compactions section above already sums it up.
  var host = container;
  if (rows.length) {
    host = el("details", { class: "advanced-detail" });
    host.appendChild(el("summary", { text: thousands(rows.length) + (rows.length === 1 ? " summary" : " summaries") }));
    container.appendChild(host);
  }
  host.appendChild(
    dataGrid({
      id: "usage-compactions-grid",
      caption: "Conversation summaries",
      columns: COMPACTION_COLUMNS,
      rows: rows,
      bar: 4,
      empty: "No conversation summaries in this window: no session grew big enough to need one.",
      emptyNext: "Pick a longer window to see older ones.",
    })
  );
}

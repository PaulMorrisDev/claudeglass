/* claudeglass service UI: charts.js
 *
 * The one way the dashboard draws a chart (docs/ui.md, "Charts"). A
 * chart sits in a frame: the question it answers, a one-line reading of
 * it (also what a screen reader hears), a legend, and a Table toggle
 * that shows the same figures as a grid. Around the chart types in
 * charts-types.js this module adds axes in the house style, the
 * tooltip, reading by keyboard, redrawing on resize, the morph when the
 * window changes, colours that stay with the thing they mean, the hatch
 * that marks an estimate, and the highlight that links a chart's marks
 * to a grid's rows.
 */

import d3 from "./d3.js";
import { clear, el, highlight, listenHighlight } from "./core.js";
import { compactNumber } from "./format.js";
import { dataGrid } from "./grid.js";
import { button, emptyState, errorNotice, hideTooltip, motionOK, showTooltipAt, swatch } from "./ui.js";

// -- the catalogue ---------------------------------------------------------------

// Every chart the dashboard draws, and only these (docs/ui.md, "Chart
// catalogue"). A chart earns a row when it shows at a glance what a
// sorted grid with inline bars can't (a trend, a distribution against a
// threshold, a sign across entities, a part-to-whole of 8 or fewer
// parts, outliers in two dimensions) and leads to an action or a
// drill-down. renderChart draws nothing that isn't listed here.
//   n        the catalogue row
//   source   the route or "section.table" the figures come from
//   form     the chart type (charts-types.js)
//   title    the question the chart answers
//   summary  the one-line reading; {names} are filled from the figures
//   alt      other readings, for when the figures say something else
export var CHART_SPECS = {
  "daily-spend": {
    n: 1,
    source: "/api/daily-usage",
    form: "stacked-columns",
    title: "Is spend rising, and did my changes move it?",
    summary: "Replies sent {span} cost {total}. The busiest day was {peakDay}, at {peak}.",
    alt: {
      sessions: "Replies sent {span} cost {total}. With their earlier replies, the window's sessions cost {sessionsTotal}. The busiest day was {peakDay}, at {peak}.",
      firstDay: "Replies sent {span} cost {total}. That counts all of the first day, from midnight UTC; the sessions in this window cost {sessionsTotal}. The busiest day was {peakDay}, at {peak}.",
      oneDay: "Replies sent {span} cost {total}.",
      oneDaySessions: "Replies sent {span} cost {total}. With their earlier replies, the window's sessions cost {sessionsTotal}.",
      oneDayFirstDay: "Replies sent {span} cost {total}. That counts the whole day from midnight UTC, not only this window; the sessions in this window cost {sessionsTotal}.",
    },
  },
  "savings-levers": {
    n: 2,
    source: ["carry.carry_truncation_savings", "compaction_sim.compaction_sim_by_window", "model_swap.model_swap_by_agent_type", "waste.waste_summary"],
    form: "bars",
    title: "Which change saves the most, and how sure is it?",
    summary: "{top} saves the most: {topAmount}, {topBasis}.",
  },
  "summary-point": {
    n: 3,
    source: "compaction_sim.compaction_sim_by_window",
    form: "line",
    title: "Would summarising conversations at a different size cost less?",
    summary: "Summarising at {best} tokens would cost {saving} less than now.",
    alt: { cheapest: "None of the sizes tried would cost less than now." },
  },
  "session-outliers": {
    n: 4,
    source: "/api/sessions",
    form: "scatter",
    title: "Which sessions are the expensive outliers?",
    summary: "{count} sessions. The most expensive cost {max}, {ratio} times the typical session.",
    alt: { unplotted: "{count} of {total} sessions. {unplotted}. The most expensive cost {max}, {ratio} times the typical session." },
  },
  "session-context": {
    n: 5,
    source: "/api/session/<id>",
    form: "timeline",
    title: "Where in this session did context grow or reset?",
    summary: "Context peaked at {peak} tokens on turn {peakTurn} of {turns}.",
  },
  "idle-gaps": {
    n: 6,
    source: "recache.recache_gap_buckets",
    form: "histogram",
    title: "Do idle gaps outlast the cache?",
    summary: "{overShare} of cache rebuilds came after a gap longer than the 5-minute cache lifetime.",
  },
  "lifetime-by-agent": {
    n: 7,
    source: "ttl.ttl_break_even_share",
    form: "diverging",
    title: "Which agent types are cheaper on a 1-hour cache lifetime?",
    summary: "{gainers} of {count} agent types cost less on a 1-hour cache lifetime than on 5 minutes. The table below says whether that needs a change.",
  },
  "startup-context": {
    n: 8,
    source: "agent_startup.agent_startup_breakdown",
    form: "stacked-bars",
    title: "What fills each agent's context before it starts?",
    summary: "{top} starts with the most context: {topTokens} tokens. The largest part is {topPart}.",
  },
  "change-timeline": {
    n: 9,
    source: "/api/daily-usage",
    form: "change-steps",
    title: "Is each reply cheaper since your changes?",
    summary: "Since {change} on {day}, a reply costs {after} on average, against {before} before it ({delta}).",
    alt: {
      none: "No change recorded in this window. A reply cost {average} on average {span}.",
      early: "{change} on {day}: no replies since it yet, so there's nothing to compare.",
    },
  },
};

// Fill a reading's {names} from the drawn figures.
export function fillSummary(template, facts) {
  return String(template).replace(/\{(\w+)\}/g, function (match, name) {
    return facts && facts[name] !== undefined && facts[name] !== null ? String(facts[name]) : match;
  });
}

// -- colours that stay with their entity ------------------------------------------

// A thing keeps its colour on every chart, page and window. The maps are
// fixed, never ranked by the window on screen, so changing the window
// never repaints the survivors. Slots follow the palette's fixed order
// (docs/ui.md, "Colour"); what doesn't fit folds into Other, in grey.
export var ENTITY_COLOURS = {
  // Chart 1, split by who ran the turns.
  agent: { main: "var(--chart-1)", subagent: "var(--chart-3)" },
  // Chart 1, split by model: a model takes its tier's colour.
  tier: { opus: "var(--chart-1)", sonnet: "var(--chart-2)", haiku: "var(--chart-3)", other: "var(--chart-other)" },
  // Chart 4: how the session ran. Mixed and unclassified are Other.
  mode: { interactive: "var(--chart-1)", "long-agentic": "var(--chart-2)", overnight: "var(--chart-3)", other: "var(--chart-other)" },
  // Chart 8: what an agent's startup context is made of.
  startup: {
    system_prompt: "var(--chart-1)",
    tool_definitions: "var(--chart-2)",
    claude_md: "var(--chart-3)",
    skills_listing: "var(--chart-4)",
    tool_lists: "var(--chart-5)",
    task_prompt: "var(--chart-6)",
    hook_context: "var(--chart-7)",
    other: "var(--chart-other)",
  },
};

export function entityColour(kind, key) {
  var map = ENTITY_COLOURS[kind] || {};
  return map[key] || map.other || "var(--chart-other)";
}

// A model's tier, for its colour: Fable counts with Opus.
export function modelTier(model) {
  var name = String(model || "").toLowerCase();
  if (name.indexOf("opus") !== -1 || name.indexOf("fable") !== -1) return "opus";
  if (name.indexOf("sonnet") !== -1) return "sonnet";
  if (name.indexOf("haiku") !== -1) return "haiku";
  return "other";
}

// -- figures ------------------------------------------------------------------------

// How much a chart moves: the morph when figures change, the draw-in on
// first show. None with reduced motion, and none past 1,500 marks,
// where a morph costs more than it explains. A page can hold either
// back a little (opts.delay: the Overview's chart waits 80ms for its
// headline figures to start counting).
var MORPH_MS = 600;
var DRAW_IN_MS = 600;
var MAX_MORPH_MARKS = 1500;

// Token axes and labels read compact ("1.24M").
export function tokenTick(value) {
  return compactNumber(value);
}

// A day as a chart writes it: "3 Sep", or "Wed 3 Sep" in a tooltip.
// Days from the store run midnight to midnight UTC.
var DAY_FORMAT = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", timeZone: "UTC" });
var DAY_LONG_FORMAT = new Intl.DateTimeFormat("en-GB", { weekday: "short", day: "numeric", month: "short", timeZone: "UTC" });

export function dayLabel(day, long) {
  var date = day instanceof Date ? day : new Date(String(day) + "T00:00:00Z");
  if (isNaN(date.getTime())) return String(day);
  return (long ? DAY_LONG_FORMAT : DAY_FORMAT).format(date);
}

// -- axes -----------------------------------------------------------------------------

// A money scale's ticks, stepped in the unit its axis is written in
// (moneyAxis): a share of the weekly limit reads 5%, 10%, 15%, not the
// 5.5%, 11%, 16% that round dollars come to. With nice, the scale's
// domain first widens to whole steps of that unit, as .nice(count) would
// in dollars.
export function moneyTicks(scale, axis, count, nice) {
  var k = axis.factor || 1;
  var shown = d3.scaleLinear().domain(
    scale.domain().map(function (d) {
      return d * k;
    })
  );
  if (nice) {
    shown.nice(count);
    scale.domain(
      shown.domain().map(function (d) {
        return d / k;
      })
    );
  }
  return shown.ticks(count).map(function (d) {
    return d / k;
  });
}

// A value axis in the house style: solid 1px hairline grid lines across
// the plot, ticks in the quietest ink, no domain line (the chart draws
// its baseline where it belongs). opts: orient ("left" or "bottom"),
// span (the grid lines' length), ticks (how many), values, format.
export function valueAxis(g, scale, opts) {
  var vertical = opts.orient === "left";
  var values = opts.values || scale.ticks(opts.ticks || 5);
  var format = opts.format || String;
  g.classed("chart-axis chart-axis-" + (vertical ? "y" : "x"), true);
  var tick = g.selectAll("g.tick").data(values, function (d) {
    return d;
  });
  tick.exit().remove();
  var entered = tick.enter().append("g").attr("class", "tick");
  entered.append("line").attr("class", "chart-grid");
  entered.append("text");
  var all = entered.merge(tick);
  if (vertical) {
    all.attr("transform", function (d) {
      return "translate(0," + (Math.round(scale(d)) + 0.5) + ")";
    });
    all.select("line").attr("x1", 0).attr("x2", opts.span);
    all.select("text").attr("x", -8).attr("dy", "0.32em").attr("text-anchor", "end").text(format);
  } else {
    all.attr("transform", function (d) {
      return "translate(" + (Math.round(scale(d)) + 0.5) + ",0)";
    });
    all.select("line").attr("y1", 0).attr("y2", -(opts.span || 0));
    all.select("text").attr("y", 18).attr("text-anchor", "middle").text(format);
  }
  return g;
}

// Labels under a band axis (days, gap sizes), thinned so they never
// collide: every nth label, at least minGap pixels apart.
export function bandAxis(g, scale, opts) {
  opts = opts || {};
  var domain = scale.domain();
  var every = Math.max(1, Math.ceil((opts.minGap || 64) / Math.max(1, scale.step())));
  var shown = domain.filter(function (d, i) {
    return (domain.length - 1 - i) % every === 0;
  });
  g.classed("chart-axis chart-axis-x", true);
  var tick = g.selectAll("g.tick").data(shown, function (d) {
    return d;
  });
  tick.exit().remove();
  var entered = tick.enter().append("g").attr("class", "tick");
  entered.append("text");
  var all = entered.merge(tick);
  all.attr("transform", function (d) {
    return "translate(" + (scale(d) + scale.bandwidth() / 2) + ",0)";
  });
  all.select("text").attr("y", 18).attr("text-anchor", "middle").text(opts.format || String);
  return g;
}

// A bar with a rounded data end and a square base: bars grow from the
// baseline, and only the end away from it is rounded (4px, less for a
// thin bar). side: "top" (a column), "right" or "left" (a bar). The
// path always has the same commands, so a morph between two sizes
// interpolates cleanly.
export function roundedBar(x, y, w, h, side, radius) {
  w = Math.max(0, w);
  h = Math.max(0, h);
  var r = Math.min(radius === undefined ? 4 : radius, w / 2, h / 2);
  function n(value) {
    return Math.round(value * 100) / 100;
  }
  if (side === "right") {
    return "M" + n(x) + "," + n(y) + "h" + n(w - r) + "a" + n(r) + "," + n(r) + " 0 0 1 " + n(r) + "," + n(r) + "v" + n(h - 2 * r) + "a" + n(r) + "," + n(r) + " 0 0 1 " + n(-r) + "," + n(r) + "h" + n(-(w - r)) + "z";
  }
  if (side === "left") {
    return "M" + n(x + w) + "," + n(y) + "h" + n(-(w - r)) + "a" + n(r) + "," + n(r) + " 0 0 0 " + n(-r) + "," + n(r) + "v" + n(h - 2 * r) + "a" + n(r) + "," + n(r) + " 0 0 0 " + n(r) + "," + n(r) + "h" + n(w - r) + "z";
  }
  return "M" + n(x) + "," + n(y + h) + "v" + n(-(h - r)) + "a" + n(r) + "," + n(r) + " 0 0 1 " + n(r) + "," + n(-r) + "h" + n(w - 2 * r) + "a" + n(r) + "," + n(r) + " 0 0 1 " + n(r) + "," + n(r) + "v" + n(h - r) + "z";
}

// -- the frame ------------------------------------------------------------------------

var frameCount = 0;
// The last frame drawn in each slot ("overview:daily-spend"), so a view
// drawn again for a new window morphs the chart it had instead of
// starting over.
var framesBySlot = {};
// Charts the reader switched to a table, by slot, for this visit.
var tableShown = {};

function slotName(key, opts) {
  return (opts && opts.slot ? opts.slot + ":" : "") + key;
}

function buildFrame(key, spec, opts) {
  frameCount += 1;
  var uid = "chart-" + frameCount;
  var figure = el("figure", { class: "chart chart-" + spec.form, "data-chart": key, id: uid });
  var title = el(opts.titleTag || "h3", { class: "chart-title", id: uid + "-title", text: opts.title || spec.title });
  var toggle = button("Show as table", { variant: "quiet", icon: "table", class: "chart-toggle" });
  toggle.setAttribute("aria-pressed", "false");
  toggle.setAttribute("aria-controls", uid + "-table");
  var head = el("div", { class: "chart-head" }, [title, toggle]);
  var summary = el("p", { class: "chart-summary", id: uid + "-summary" });
  var legend = el("div", { class: "chart-legend", role: "list", hidden: true });
  // The plot is one Tab stop: the arrow keys read it mark by mark.
  var plot = el("div", {
    class: "chart-plot",
    tabIndex: 0,
    role: "group",
    "aria-roledescription": "chart",
    "aria-labelledby": uid + "-title " + uid + "-summary",
    "aria-describedby": uid + "-keys",
  });
  var keys = el("span", { class: "visually-hidden", id: uid + "-keys", text: "Use the arrow keys to read each value." });
  var readout = el("div", { class: "visually-hidden", "aria-live": "polite" });
  var tableHost = el("div", { class: "chart-table", id: uid + "-table", hidden: true });
  var note = el("p", { class: "chart-note", hidden: true });
  [head, summary, legend, plot, tableHost, note, keys, readout].forEach(function (node) {
    figure.appendChild(node);
  });

  var frame = {
    key: key,
    spec: spec,
    uid: uid,
    slot: slotName(key, opts),
    node: figure,
    plot: plot,
    summaryNode: summary,
    legendNode: legend,
    tableHost: tableHost,
    noteNode: note,
    readout: readout,
    toggle: toggle,
    svg: null,
    width: 0,
    drawn: false,
    data: null,
    opts: opts,
    form: null,
    points: [],
    cursor: -1,
    anchor: -1,
    table: null,
    patterns: {},
    linkScope: null,
    stopHighlight: null,
    observer: null,
    // The move under way: {first, starts, ends} (performance.now()).
    moving: null,
  };

  toggle.addEventListener("click", function () {
    showTable(frame, toggle.getAttribute("aria-pressed") !== "true");
  });
  wireKeys(frame);
  wireResize(frame);
  return frame;
}

function showTable(frame, asTable) {
  tableShown[frame.slot] = asTable;
  frame.toggle.setAttribute("aria-pressed", asTable ? "true" : "false");
  var label = frame.toggle.querySelector(".button-label");
  if (label) label.textContent = asTable ? "Show as chart" : "Show as table";
  frame.plot.hidden = asTable;
  frame.legendNode.hidden = asTable || !frame.legendNode.childNodes.length;
  frame.tableHost.hidden = !asTable;
  if (asTable) renderFrameTable(frame);
  else clear(frame.tableHost);
}

// The Table view: the same figures the chart draws, as a sortable grid.
function renderFrameTable(frame) {
  clear(frame.tableHost);
  // Not drawn yet (not on the page): the first draw fills it.
  if (!frame.drawn) return;
  if (!frame.table) {
    frame.tableHost.appendChild(emptyState(frame.summaryNode.textContent || "Nothing to list for this window."));
    return;
  }
  frame.tableHost.appendChild(
    dataGrid({
      id: frame.slot.replace(/[^\w-]/g, "-") + "-table",
      caption: frame.spec.title,
      columns: frame.table.columns,
      rows: frame.table.rows,
      bar: -1,
      empty: "Nothing to list for this window.",
    })
  );
}

// The width the chart draws at: the plot's, or the table's while the
// reader has the table showing (the hidden plot measures 0), so the
// table still follows new figures.
function plotWidth(frame) {
  return Math.floor((frame.plot.hidden ? frame.tableHost : frame.plot).clientWidth);
}

// Redraw when the plot's width changes (the sidebar rail, the window).
// The first draw waits for a width: a frame built before it is on the
// page draws once it lands.
function wireResize(frame) {
  if (typeof ResizeObserver !== "function") return;
  var pending = false;
  frame.observer = new ResizeObserver(function () {
    if (pending) return;
    pending = true;
    requestAnimationFrame(function () {
      pending = false;
      var width = plotWidth(frame);
      if (!width || width === frame.width || !frame.data) return;
      var first = !frame.drawn;
      frame.width = width;
      if (first) drawFrame(frame, { first: true, resize: false });
      else redraw(frame);
    });
  });
  frame.observer.observe(frame.plot);
  frame.observer.observe(frame.tableHost);
}

// A frame replaced in its slot lets go of what keeps it alive: its
// resize observer and its linked-highlight listener.
function retireFrame(frame) {
  if (!frame) return;
  if (frame.observer) frame.observer.disconnect();
  frame.observer = null;
  if (frame.stopHighlight) frame.stopHighlight();
  frame.stopHighlight = null;
}

// -- keyboard reading ----------------------------------------------------------------

// The plot takes focus once; the arrow keys step through its marks in
// reading order, Home and End jump to the ends, Enter opens the mark
// when it leads somewhere, and Esc lets go.
function wireKeys(frame) {
  frame.plot.addEventListener("keydown", function (event) {
    var points = frame.points;
    if (!points.length) return;
    var from = frame.cursor;
    var next = frame.cursor;
    if (event.key === "ArrowRight" || event.key === "ArrowDown") next = Math.min(points.length - 1, frame.cursor + 1);
    else if (event.key === "ArrowLeft" || event.key === "ArrowUp") next = Math.max(0, frame.cursor - 1);
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = points.length - 1;
    else if (event.key === "Enter" || event.key === " ") {
      var point = points[frame.cursor];
      if (point && point.open) {
        event.preventDefault();
        point.open();
      }
      return;
    } else if (event.key === "Escape") {
      if (frame.cursor !== -1) event.stopPropagation();
      releaseCursor(frame);
      return;
    } else return;
    event.preventDefault();
    moveCursor(frame, next);
    // Shift with a move picks the marks from where it started (a chart
    // that takes a range, the session scatter's time brush).
    if (event.shiftKey && frame.pickRange) {
      if (frame.anchor === -1) frame.anchor = from === -1 ? next : from;
      frame.pickRange(frame.anchor, next);
    } else frame.anchor = -1;
  });
  frame.plot.addEventListener("blur", function () {
    releaseCursor(frame);
  });
}

function moveCursor(frame, index) {
  var point = frame.points[index];
  if (!point || !frame.svg) return;
  frame.cursor = index;
  var cursor = frame.svg.select("g.chart-cursor");
  if (cursor.empty()) cursor = frame.svg.append("g").attr("class", "chart-cursor").attr("aria-hidden", "true");
  cursor.selectAll("*").remove();
  if (point.band) {
    cursor
      .append("rect")
      .attr("class", "chart-cursor-band")
      .attr("x", point.band.x - 3)
      .attr("y", point.band.y - 3)
      .attr("width", point.band.w + 6)
      .attr("height", point.band.h + 6)
      .attr("rx", 6);
  } else {
    cursor.append("circle").attr("class", "chart-cursor-ring").attr("cx", point.x).attr("cy", point.y).attr("r", 8);
  }
  if (point.key !== undefined && frame.linkScope) highlight(frame.linkScope, point.key);
  var box = frame.svg.node().getBoundingClientRect();
  var x = box.left + point.x;
  var y = box.top + (point.band ? point.band.y : point.y);
  showTooltipAt({ left: x, right: x, top: y - 4, bottom: y + 4, width: 0, height: 8 }, point.tip);
  frame.readout.textContent = tipText(point.tip);
}

function releaseCursor(frame) {
  frame.anchor = -1;
  if (frame.cursor === -1) return;
  frame.cursor = -1;
  if (frame.svg) frame.svg.select("g.chart-cursor").remove();
  if (frame.linkScope) highlight(frame.linkScope, null);
  hideTooltip();
}

function tipText(tip) {
  if (!tip) return "";
  if (typeof tip === "string") return tip;
  return [tip.value, tip.label].concat(tip.lines || []).filter(Boolean).join(". ");
}

// -- the context a chart type draws with ---------------------------------------------

// What a chart type gets: the svg (a d3 selection), its size, the plot
// area inside the margins, and helpers. A chart type returns {facts,
// points, table, legend, note, variant}, or {empty: "the reason"}.
function drawContext(frame, how) {
  var opts = frame.opts;
  // Screen readers skip the drawing (each layer is aria-hidden): the
  // plot reads out mark by mark from the keyboard instead. The svg
  // itself isn't hidden, so a layer of links (layer(name, {links:
  // true})) keeps the names of the labels you can Tab to.
  if (!frame.svg) {
    frame.svg = d3.select(frame.plot).append("svg").attr("class", "chart-svg").attr("role", "none").attr("focusable", "false");
    frame.svg.append("defs");
  }
  // A hidden tab never runs the frames a morph needs: draw it settled.
  // A resize is drawn at once, unless it lands mid-move (how.ms: see
  // redraw).
  var animate = motionOK() && (!how.resize || how.ms > 0) && !document.hidden;
  var delay = (animate && how.delay) || 0;
  var ctx = {
    frame: frame,
    svg: frame.svg,
    width: frame.width,
    height: 0,
    margin: null,
    inner: null,
    first: how.first,
    // Redrawn for a new width, not new figures.
    resize: !!how.resize,
    opts: opts,
    // Set the chart's height and margins; the plot area is what's left.
    size: function (height, margin) {
      ctx.height = height;
      ctx.margin = Object.assign({ top: 24, right: 16, bottom: 32, left: 56 }, margin || {});
      ctx.inner = {
        w: Math.max(10, ctx.width - ctx.margin.left - ctx.margin.right),
        h: Math.max(10, height - ctx.margin.top - ctx.margin.bottom),
      };
      frame.svg.attr("viewBox", "0 0 " + ctx.width + " " + height).attr("width", ctx.width).attr("height", height);
      return ctx.inner;
    },
    // The morph for a window change; the draw-in on first show. The
    // frame notes when the move ends, so a resize before then carries
    // it on.
    duration: function (marks) {
      if (!animate || (marks || 0) > MAX_MORPH_MARKS) return 0;
      var ms = how.ms || (how.first ? DRAW_IN_MS : MORPH_MS);
      var now = performance.now();
      frame.moving = { first: !!how.first, starts: now + delay, ends: now + delay + ms };
      return ms;
    },
    // How long the marks wait before they move.
    delay: delay,
    ease: how.first ? d3.easeExpOut : d3.easeCubicInOut,
    // A named layer inside the plot area (drawn in the order first asked
    // for), hidden from screen readers unless layerOpts.links.
    layer: function (name, layerOpts) {
      var g = frame.svg.select("g.layer-" + name);
      if (g.empty()) {
        g = frame.svg.append("g").attr("class", "layer-" + name);
        if (!(layerOpts && layerOpts.links)) g.attr("aria-hidden", "true");
      }
      g.attr("transform", "translate(" + ctx.margin.left + "," + ctx.margin.top + ")");
      return g;
    },
    // An axis's unit: at the top left, above the value axis ("% of your
    // weekly usage limit"), or under the end of the bottom axis ("Turn").
    // It needs a top margin of 20 or a bottom margin of 44. A bare "$"
    // isn't written: every tick already carries it.
    unit: function (text, where) {
      var g = ctx.layer("unit");
      var side = where === "bottom" ? "bottom" : "left";
      var label = g.select("text.chart-unit-" + side);
      if (!text || text === "$") {
        label.remove();
        return;
      }
      if (label.empty()) label = g.append("text").attr("class", "chart-unit chart-unit-" + side);
      if (where === "bottom") label.attr("x", ctx.inner.w).attr("y", ctx.inner.h + 36).attr("text-anchor", "end");
      else label.attr("x", -ctx.margin.left + 4).attr("y", -ctx.margin.top + 12).attr("text-anchor", "start");
      label.text(text);
    },
    hatch: function (colour) {
      return hatchFill(frame, colour);
    },
    tooltip: pointerTooltip,
    hideTooltip: hideTooltip,
    link: function (selection, scope, keyOf) {
      linkMarks(frame, selection, scope, keyOf);
    },
  };
  return ctx;
}

// A drawn chart at a new size (width or height). A draw-in or morph
// still under way carries on to the new size in the time it had left:
// stopped, or left to run, it would finish at the old size. A settled
// chart is redrawn in place at once, with no morph.
function redraw(frame) {
  var moving = frame.moving;
  var now = performance.now();
  frame.moving = null;
  if (moving && now < moving.ends) {
    drawFrame(frame, { first: moving.first, resize: true, delay: Math.max(0, moving.starts - now), ms: moving.ends - Math.max(now, moving.starts) });
    return;
  }
  frame.svg.selectAll("*").interrupt();
  drawFrame(frame, { first: false, resize: true });
}

function drawFrame(frame, how) {
  if (!frame.form || !frame.data) return;
  releaseCursor(frame);
  var ctx = drawContext(frame, how);
  var result = frame.form(ctx, frame.data) || {};
  frame.drawn = true;
  var old = frame.plot.querySelector(".chart-empty");
  if (old) old.remove();
  frame.plot.classList.toggle("is-empty", !!result.empty);
  if (result.empty) {
    frame.svg.selectAll("g").remove();
    frame.svg.attr("height", 0);
    frame.plot.appendChild(el("div", { class: "chart-empty" }, [emptyState(result.empty, null, frame.opts.emptyNext)]));
    frame.points = [];
    frame.pickRange = null;
    frame.table = null;
    // The box says it; the summary keeps the words for a screen reader
    // (the chart is described by it) without printing them twice.
    frame.summaryNode.textContent = result.empty;
    frame.summaryNode.classList.add("visually-hidden");
    setLegend(frame, []);
    frame.noteNode.hidden = true;
    return;
  }
  frame.points = result.points || [];
  frame.pickRange = result.pick || null;
  frame.table = result.table || null;
  var template = (result.variant && frame.spec.alt && frame.spec.alt[result.variant]) || frame.spec.summary;
  frame.summaryNode.textContent = frame.opts.summary || fillSummary(template, result.facts || {});
  frame.summaryNode.classList.remove("visually-hidden");
  setLegend(frame, result.legend || []);
  frame.noteNode.hidden = !result.note;
  frame.noteNode.textContent = result.note || "";
  if (!frame.tableHost.hidden) renderFrameTable(frame);
}

// The legend: there for two series or more, never for one (the title
// names it). Items: {label, colour, hatch, line, glyph, key}.
function setLegend(frame, items) {
  var legend = frame.legendNode;
  clear(legend);
  items.forEach(function (item) {
    var entry = el("span", { class: "chart-legend-item", role: "listitem" }, [
      swatch(item.colour, { hatch: item.hatch, line: item.line, glyph: item.glyph }),
      el("span", { text: item.label }),
    ]);
    if (item.key !== undefined && frame.linkScope) {
      entry.addEventListener("mouseenter", function () {
        highlight(frame.linkScope, item.key);
      });
      entry.addEventListener("mouseleave", function () {
        highlight(frame.linkScope, null);
      });
    }
    legend.appendChild(entry);
  });
  legend.hidden = !items.length || !frame.tableHost.hidden;
}

// -- hatch: the mark of an estimate ---------------------------------------------------

// One directional fill at 45 degrees, in the series colour over a light
// wash of it: an estimate, a ceiling or a simulation reads as hatched
// wherever it's drawn, in print and in forced colours too.
function hatchFill(frame, colour) {
  if (frame.patterns[colour]) return "url(#" + frame.patterns[colour] + ")";
  var id = frame.uid + "-hatch-" + Object.keys(frame.patterns).length;
  var pattern = frame.svg
    .select("defs")
    .append("pattern")
    .attr("id", id)
    .attr("patternUnits", "userSpaceOnUse")
    .attr("width", 6)
    .attr("height", 6)
    .attr("patternTransform", "rotate(45)");
  pattern.append("rect").attr("width", 6).attr("height", 6).attr("fill", colour).attr("fill-opacity", 0.22);
  pattern.append("line").attr("x1", 1).attr("y1", 0).attr("x2", 1).attr("y2", 6).attr("stroke", colour).attr("stroke-width", 2.5);
  frame.patterns[colour] = id;
  return "url(#" + id + ")";
}

// -- tooltip --------------------------------------------------------------------------

// The tooltip for the mark under the pointer: value first, then what it
// is, then the other lines.
export function pointerTooltip(event, tip) {
  var x = event.clientX;
  var y = event.clientY;
  showTooltipAt({ left: x, right: x, top: y - 8, bottom: y + 8, width: 0, height: 16 }, tip);
}

// -- linked highlight -----------------------------------------------------------------

// Hovering or focusing a grid row lights the same entity's mark in the
// chart, and the other way round (core.js's highlight). scope names what
// the keys are ("session", "agent-type"); keyOf(d) is a mark's key, and
// each mark carries it as data-key.
function linkMarks(frame, selection, scope, keyOf) {
  frame.linkScope = scope;
  selection
    .attr("data-key", function (d) {
      return String(keyOf(d));
    })
    .on("mouseenter.link", function (event, d) {
      highlight(scope, keyOf(d));
    })
    .on("mouseleave.link", function () {
      highlight(scope, null);
    });
  if (frame.stopHighlight) frame.stopHighlight();
  frame.stopHighlight = listenHighlight(function (activeScope, key) {
    if (!frame.node.isConnected) return false;
    if (activeScope !== scope) return true;
    frame.svg.selectAll("[data-key]").each(function () {
      var mine = this.getAttribute("data-key") === String(key);
      this.classList.toggle("is-linked", key !== null && mine);
      this.classList.toggle("is-dimmed", key !== null && !mine);
    });
    return true;
  });
}

// -- drawing a chart ------------------------------------------------------------------

// Draw `data` as chart `key` into `container`, with the chart type
// `form` (charts-types.js's renderChart picks it from CHART_SPECS). A
// view drawn again (a new window) gets its slot's chart back and morphs
// it to the new figures. opts: slot (where the chart sits, when the same
// chart shows on two pages), fresh (start over: a different session),
// title, titleTag, summary (a reading the page wrote), empty and
// emptyNext (why there's nothing to draw, and what would fill it),
// open(d) (where a mark leads), and whatever the chart type reads.
export function drawChart(container, key, data, form, opts) {
  opts = opts || {};
  var spec = CHART_SPECS[key];
  if (!spec) throw new Error("No chart " + key + " in CHART_SPECS");
  var slot = slotName(key, opts);
  var frame = framesBySlot[slot];
  if (!frame || opts.fresh || (frame.node.isConnected && frame.node.parentNode !== container)) {
    retireFrame(frame);
    // A fresh chart (another session) starts as a chart, whatever the
    // last one in its slot was showing.
    if (opts.fresh) delete tableShown[slot];
    frame = buildFrame(key, spec, opts);
    framesBySlot[slot] = frame;
  }
  frame.opts = opts;
  frame.form = form;
  frame.data = data;
  if (frame.node.parentNode !== container) {
    clear(container);
    container.appendChild(frame.node);
  }
  frame.node.classList.remove("is-refreshing");
  if (tableShown[slot]) showTable(frame, true);
  var width = plotWidth(frame);
  if (width) {
    var first = !frame.drawn;
    frame.width = width;
    drawFrame(frame, { first: first, resize: false, delay: opts.delay });
  }
  return frame;
}

// While a view fetches new figures, put its slot's last chart back,
// dimmed, instead of a skeleton: nothing jumps, and the morph that
// follows shows what changed. Returns false when there's no chart to
// hold yet (the caller shows its skeleton).
export function holdChart(container, key, opts) {
  var frame = framesBySlot[slotName(key, opts)];
  if (!frame || !frame.drawn || (frame.node.isConnected && frame.node.parentNode !== container)) return false;
  if (frame.node.parentNode !== container) {
    clear(container);
    container.appendChild(frame.node);
  }
  frame.node.classList.add("is-refreshing");
  return true;
}

// A chart whose figures couldn't load: the frame and its question stay,
// with the error and a way to try again inside it.
export function chartError(container, key, error, retry, opts) {
  opts = opts || {};
  var spec = CHART_SPECS[key];
  if (!spec) throw new Error("No chart " + key + " in CHART_SPECS");
  var slot = slotName(key, opts);
  retireFrame(framesBySlot[slot]);
  delete framesBySlot[slot];
  clear(container);
  var figure = el("figure", { class: "chart chart-" + spec.form + " is-error", "data-chart": key }, [
    el("div", { class: "chart-head" }, [el(opts.titleTag || "h3", { class: "chart-title", text: opts.title || spec.title })]),
    el("div", { class: "chart-plot is-empty" }, [el("div", { class: "chart-empty" }, [errorNotice(error, retry)])]),
  ]);
  container.appendChild(figure);
  return figure;
}

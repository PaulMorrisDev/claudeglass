/* claudeglass service UI: charts-types.js
 *
 * The chart types that charts.js frames (docs/ui.md, "Charts"): stacked
 * columns, bars hatched by how sure they are, a line with markers, a
 * histogram with rules, diverging bars, stacked bars, a scatter with a
 * brush and the session timeline. Also the two micro-forms a tile or a
 * card carries: the sparkline and the segmented meter.
 *
 * renderChart is the only way a page draws a chart, and it draws only
 * the charts CHART_SPECS lists. A chart type is function (ctx, data):
 * it draws into ctx (charts.js, "the context a chart type draws with")
 * and returns {facts, points, table, legend, note, variant}, or
 * {empty: "the reason"} when there's nothing worth drawing.
 */

import d3 from "./d3.js";
import { el, escapeHtml, goTo, highlight, state } from "./core.js";
import { actionIndex } from "./api.js";
import { MINUS, compactNumber, formatDuration, moneyAxis, moneyCell, moneyText, projectName, shortTs, signedPercent, thousands } from "./format.js";
import { BASIS } from "./ui.js";
import { CHART_SPECS, bandAxis, dayLabel, drawChart, entityColour, modelTier, moneyTicks, roundedBar, tokenTick, valueAxis } from "./charts.js";

// Fewer points than this and a chart says less than a table would: the
// page shows tiles instead (docs/ui.md, "Admission rule"). Daily spend
// is the one exception: a short window is a day or two of columns.
var MIN_POINTS = 3;
// Bars and columns are thin marks: never thicker than this.
var MAX_BAR = 24;
var BAR = 16;
var ROW = 28;
var DAY_MS = 86400000;

// -- shared helpers --------------------------------------------------------------------

function num(value) {
  var n = Number(value);
  return isFinite(n) ? n : 0;
}

// A report table ({columns, rows, value_labels}) as one object per row.
export function tableObjects(table) {
  if (!table || !Array.isArray(table.rows)) return [];
  var keys = (table.columns || []).map(function (column) {
    return column.key;
  });
  return table.rows.map(function (row) {
    if (!Array.isArray(row)) return row;
    var object = {};
    keys.forEach(function (key, i) {
      object[key] = row[i];
    });
    return object;
  });
}

function valueLabel(table, value) {
  var labels = (table && table.value_labels) || {};
  return labels[value] !== undefined ? labels[value] : value;
}

// A report table, whether it came alone or as {table}.
function tableOf(data) {
  return data && data.table ? data.table : data;
}

// A child group with its own offset inside a layer ("the x axis sits
// at the foot of the plot").
function sublayer(g, name, x, y) {
  var inner = g.select('g[data-part="' + name + '"]');
  if (inner.empty()) inner = g.append("g").attr("data-part", name);
  return inner.attr("transform", "translate(" + x + "," + y + ")");
}

// Set attributes now, or over the morph when there is one (after the
// delay the page asked for).
function morph(selection, ctx, marks) {
  var ms = ctx.duration(marks);
  return ms ? selection.transition().delay(ctx.delay).duration(ms).ease(ctx.ease) : selection;
}

// The tooltip and the click for a mark whose datum carries tip and open.
function hover(selection, ctx) {
  selection
    .classed("is-openable", function (d) {
      return !!d.open;
    })
    .on("mousemove.tip", function (event, d) {
      ctx.tooltip(event, d.tip);
    })
    .on("mouseleave.tip", function () {
      ctx.hideTooltip();
    })
    .on("click.open", function (event, d) {
      if (d.open) d.open();
    });
}

// Where a mark leads, when the page said: opts[name](what) as a thunk.
function opener(opts, name, what) {
  var open = opts && opts[name];
  return typeof open === "function"
    ? function () {
        open(what);
      }
    : null;
}

// An amount with its sign: "+$12", "−$145".
function signedMoney(usd) {
  var text = moneyText(Math.abs(usd));
  return usd > 0 ? "+" + text : usd < 0 ? MINUS + text : text;
}

// A label cut to fit, whole in its tooltip.
function fitLabel(text, chars) {
  text = String(text);
  return text.length > chars ? text.slice(0, Math.max(1, chars - 1)) + "…" : text;
}

// Room for the longest label in a column of labels, at 12px.
function labelWidth(labels) {
  var longest = 0;
  labels.forEach(function (label) {
    longest = Math.max(longest, String(label).length);
  });
  return Math.max(96, Math.min(220, Math.round(longest * 6.8) + 16));
}

function percentText(part, whole) {
  if (!whole) return "0%";
  var pct = (part / whole) * 100;
  return (pct >= 10 || pct === 0 ? Math.round(pct) : Number(pct.toFixed(1))) + "%";
}

function sum(values) {
  var total = 0;
  values.forEach(function (value) {
    total += num(value);
  });
  return total;
}

// -- 1. daily spend: stacked columns ---------------------------------------------------

var AGENT_SERIES = [
  { key: "main", label: "Main session" },
  { key: "subagent", label: "Subagents" },
];
var TIER_SERIES = [
  { key: "opus", label: "Opus and Fable" },
  { key: "sonnet", label: "Sonnet" },
  { key: "haiku", label: "Haiku" },
  { key: "other", label: "Other models" },
];

// A day key is a calendar date, "2026-09-03": the service counts the day
// in its own zone, so the browser never works one out. Stepping a key
// through UTC midnights is zone-free: it can't skip or repeat a date.
function utcDay(ms) {
  return new Date(ms).toISOString().slice(0, 10);
}

// The days a window with fixed bounds reaches, as /api/summary's
// data.period gives them (the service's local days, first to last, and
// which is today). Given to daily spend as {first, last, today}, so the
// axis spans the window, not only the days with spend (a project picked,
// say). All time has no first day: its axis spans the days drawn, and only
// today comes from the period. A service that predates period (or a canned
// answer) sends none: {}.
export function windowSpan(summaryBody) {
  var period = summaryBody && summaryBody.data && summaryBody.data.period;
  if (!period) return {};
  if (!period.first_day) return period.today ? { today: period.today } : {};
  return { first: period.first_day, last: period.last_day, today: period.today };
}

// A day in a span's words: "26 Aug", with the year when the span crosses one.
var SPAN_YEAR_FORMAT = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });

function spanText(days) {
  var first = days[0];
  var last = days[days.length - 1];
  if (days.length === 1) return "on " + dayLabel(first, true);
  if (first.slice(0, 4) === last.slice(0, 4)) return "from " + dayLabel(first) + " to " + dayLabel(last);
  return "from " + SPAN_YEAR_FORMAT.format(new Date(first + "T00:00:00Z")) + " to " + SPAN_YEAR_FORMAT.format(new Date(last + "T00:00:00Z"));
}

// Every day from first to last, so a quiet day shows as a gap rather
// than vanishing.
function dayRange(first, last) {
  var start = Date.parse(first + "T00:00:00Z");
  var end = Date.parse(last + "T00:00:00Z");
  if (isNaN(start) || isNaN(end) || end < start) return [first];
  var days = [];
  for (var ms = start; ms <= end && days.length < 1000; ms += DAY_MS) days.push(utcDay(ms));
  return days;
}

// The local day a change was made, as a chart and its card name it:
// /api/impact's change.day. A service that predates it sends only the
// timestamp, whose UTC date stands in.
export function changeDay(change) {
  change = change || {};
  return change.day || String(change.ts || "").slice(0, 10);
}

// The settings changes to mark on a daily spend chart, from /api/impact's
// body. Each leads to what it did: its card on Your changes.
export function dailyChanges(impact) {
  var rows = (impact && impact.ok === true && impact.data && impact.data.changes) || [];
  return rows.map(function (row) {
    var change = row.change || {};
    var day = changeDay(change);
    return {
      day: day,
      label: change.label || "A settings change",
      // Your changes pulses that day's card.
      open: function () {
        goTo("changes", { params: { day: day } });
      },
    };
  });
}

// A change's label beside its rule: to the right when it fits before
// the next rule, the next label and the chart's edge; else to the left;
// else cut to the larger room. Its whole name stays in the tooltip and,
// for a link, in its name. spans: the labels placed so far, [from, to].
function placeRuleLabel(label, full, cx, ruleXs, spans, bounds) {
  var node = label.node();
  var width = node && node.getComputedTextLength ? node.getComputedTextLength() : 0;
  if (!width) return;
  var rightEnd = bounds[1];
  var leftEnd = bounds[0];
  ruleXs.forEach(function (rx) {
    if (rx > cx + 1) rightEnd = Math.min(rightEnd, rx - 4);
    else if (rx < cx - 1) leftEnd = Math.max(leftEnd, rx + 4);
  });
  spans.forEach(function (span) {
    if (span[0] >= cx) rightEnd = Math.min(rightEnd, span[0] - 6);
    else leftEnd = Math.max(leftEnd, span[1] + 6);
  });
  var rightRoom = rightEnd - (cx + 4);
  var leftRoom = cx - 4 - leftEnd;
  var right = width <= rightRoom || (width > leftRoom && rightRoom >= leftRoom);
  var room = right ? rightRoom : leftRoom;
  if (width > room) {
    var text = label.text();
    label.text(fitLabel(full, Math.max(2, Math.floor((Math.max(0, room) / width) * text.length))));
    width = node.getComputedTextLength();
  }
  label.attr("x", right ? cx + 4 : cx - 4).attr("text-anchor", right ? "start" : "end");
  spans.push(right ? [cx + 4, cx + 4 + width] : [cx - 4 - width, cx - 4]);
}

// data: {rows, split, first, last, today, changes, sessionsTotal} or
// the rows alone. rows are /api/daily-usage's (day, model, cost, and
// agent with split=agent). first, last and today (windowSpan) widen the
// axis to the whole window and say which day is still filling; a day with
// spend outside them still shows. Without them the axis spans the days
// drawn, and today is UTC's.
// changes: [{day, label, open}] drawn as labelled rules. sessionsTotal
// is /api/summary's total_cost, which counts whole sessions: when it
// reads differently, the summary gives it and says why.
// opts.open(day) leads from a day to its sessions.
function stackedColumns(ctx, data) {
  var opts = ctx.opts;
  var rows = Array.isArray(data) ? data : (data && data.rows) || [];
  var split = (data && data.split) || opts.split || "agent";
  if (
    split === "agent" &&
    !rows.some(function (row) {
      return row.agent;
    })
  )
    split = "model";
  var kind = split === "agent" ? "agent" : "tier";
  var seriesOf =
    split === "agent"
      ? function (row) {
          return row.agent === "main" ? "main" : "subagent";
        }
      : function (row) {
          return modelTier(row.model);
        };
  var byDay = {};
  rows.forEach(function (row) {
    if (!row.day) return;
    var day = byDay[row.day] || (byDay[row.day] = {});
    var key = seriesOf(row);
    day[key] = (day[key] || 0) + num(row.cost);
  });
  var seen = Object.keys(byDay).sort();
  var grand = sum(
    seen.map(function (day) {
      return sum(Object.values(byDay[day]));
    })
  );
  if (!seen.length || !grand) return { empty: opts.empty || "No spend in this window." };

  var first = data && data.first && data.first < seen[0] ? data.first : seen[0];
  var last = data && data.last && data.last > seen[seen.length - 1] ? data.last : seen[seen.length - 1];
  var days = dayRange(first, last);
  var series = (split === "agent" ? AGENT_SERIES : TIER_SERIES).filter(function (s) {
    return days.some(function (day) {
      return (byDay[day] || {})[s.key] > 0;
    });
  });
  var today = (data && data.today) || utcDay(Date.now());
  var changes = ((data && data.changes) || []).filter(function (change) {
    return days.indexOf(change.day) !== -1;
  });
  var columns = days.map(function (day) {
    var values = byDay[day] || {};
    var base = 0;
    var parts = series.map(function (s) {
      var value = values[s.key] || 0;
      var part = { id: day + ":" + s.key, day: day, key: s.key, label: s.label, value: value, y0: base, y1: base + value };
      base += value;
      return part;
    });
    return {
      day: day,
      total: base,
      parts: parts,
      partial: day === today,
      changes: changes.filter(function (change) {
        return change.day === day;
      }),
    };
  });

  var axis = moneyAxis();
  var inner = ctx.size(opts.height || 240, { top: changes.length ? 40 : 24 });
  // Change labels sit 12px above the plot; "so far" sits over them.
  var soFarY = changes.length ? -28 : -8;
  var x = d3.scaleBand().domain(days).range([0, inner.w]).paddingInner(0.3).paddingOuter(0.15);
  var width = Math.min(MAX_BAR, x.bandwidth());
  var inset = (x.bandwidth() - width) / 2;
  var y = d3
    .scaleLinear()
    .domain([
      0,
      d3.max(columns, function (c) {
        return c.total;
      }) || 1,
    ])
    .range([inner.h, 0]);
  valueAxis(ctx.layer("axis-y"), y, { orient: "left", span: inner.w, values: moneyTicks(y, axis, 4, true), format: axis.tick });
  bandAxis(sublayer(ctx.layer("axis-x"), "base", 0, inner.h), x, {
    minGap: 56,
    format: function (day) {
      return dayLabel(day);
    },
  });
  ctx.unit(axis.unit);

  var ox = ctx.margin.left;
  var oy = ctx.margin.top;
  var marks = ctx.layer("marks");
  var flat = [];
  columns.forEach(function (column) {
    column.parts.forEach(function (part) {
      part.partial = column.partial;
      part.top = part.value > 0 && part.y1 === column.total;
      flat.push(part);
    });
  });
  // Stacked segments sit 2px apart; only the top one is rounded.
  function shape(part) {
    var top = y(part.y1);
    var bottom = y(part.y0) - (part.y0 > 0 ? 2 : 0);
    return roundedBar(x(part.day) + inset, top, width, Math.max(0, bottom - top), "top", part.top ? 4 : 0);
  }
  function grounded(part) {
    return roundedBar(x(part.day) + inset, inner.h, width, 0, "top", 0);
  }
  var segments = marks.selectAll("path.chart-segment").data(flat, function (d) {
    return d.id;
  });
  segments.exit().remove();
  var entered = segments.enter().append("path").attr("class", "chart-segment").attr("d", grounded);
  var all = entered.merge(segments);
  all
    .attr("fill", function (d) {
      return entityColour(kind, d.key);
    })
    .classed("is-partial", function (d) {
      return d.partial;
    });
  morph(all, ctx, flat.length).attr("d", shape);
  ctx.link(all, "day", function (d) {
    return d.day;
  });

  // "so far" over today's column, in the margin above the plot: no bar
  // reaches it, and it sits over the change labels, so a taller
  // neighbour or a change can't run into it. The day isn't over.
  var partial = columns.filter(function (c) {
    return c.partial && c.total > 0;
  });
  var soFar = marks.selectAll("text.chart-label").data(partial, function (d) {
    return d.day;
  });
  soFar.exit().remove();
  soFar
    .enter()
    .append("text")
    .attr("class", "chart-label")
    .attr("text-anchor", "middle")
    .text("so far")
    .merge(soFar)
    .attr("x", function (d) {
      // Kept inside the chart's right edge (today is the last column).
      return Math.min(x(d.day) + x.bandwidth() / 2, inner.w + ctx.margin.right - 20);
    })
    .attr("y", soFarY);

  // Changes you made, as labelled rules that lead to what they did. A
  // label that leads somewhere is a link in the one layer screen readers
  // reach: a Tab stop that Enter or Space opens, like a click.
  // Left to right, so labels are placed, and reached by Tab, in the
  // order they read.
  var rules = ctx.layer("rules");
  var ruleLinks = ctx.layer("rule-links", { links: true });
  rules.selectAll("*").remove();
  ruleLinks.selectAll("*").remove();
  // A day's changes share one rule and one label: they sit on the same
  // column, where their labels printed over each other, and all open
  // that day on Your changes. The day's tooltip names each.
  var ruleDays = [];
  changes.forEach(function (change) {
    var group = ruleDays.find(function (g) {
      return g.day === change.day;
    });
    if (!group) ruleDays.push((group = { day: change.day, changes: [] }));
    group.changes.push(change);
  });
  ruleDays.forEach(function (group) {
    var single = group.changes.length === 1;
    var opener = group.changes.find(function (change) {
      return change.open;
    });
    group.label = single ? group.changes[0].label : group.changes.length + " changes";
    group.open = opener ? opener.open : null;
    group.aria = single
      ? group.label + ", changed on " + dayLabel(group.day, true) + ": see what it did"
      : group.label + " on " + dayLabel(group.day, true) + ": see what they did";
  });
  var ruleXs = ruleDays.map(function (group) {
    return Math.round(x(group.day) + x.bandwidth() / 2) + 0.5;
  });
  var labelSpans = [];
  var labelBounds = [-ctx.margin.left + 4, inner.w + ctx.margin.right - 2];
  ruleDays
    .map(function (group, i) {
      return { change: group, cx: ruleXs[i] };
    })
    .sort(function (a, b) {
      return a.cx - b.cx;
    })
    .forEach(function (placed) {
      var change = placed.change;
      var cx = placed.cx;
      rules.append("line").attr("class", "chart-rule").attr("x1", cx).attr("x2", cx).attr("y1", -8).attr("y2", inner.h);
      var label = (change.open ? ruleLinks : rules)
        .append("text")
        .attr("class", "chart-rule-label")
        .attr("x", cx + 4)
        .attr("y", -12)
        .text(fitLabel(change.label, 28));
      placeRuleLabel(label, change.label, cx, ruleXs, labelSpans, labelBounds);
      if (!change.open) return;
      label
        .classed("is-openable", true)
        .attr("tabindex", 0)
        .attr("role", "link")
        .attr("aria-label", change.aria)
        .on("click", change.open)
        .on("keydown", function (event) {
          if (event.key !== "Enter" && event.key !== " ") return;
          // The plot's own keys (Enter opens a day's sessions) stay out of it.
          event.preventDefault();
          event.stopPropagation();
          change.open();
        });
    });

  columns.forEach(function (column) {
    column.tip = {
      value: moneyText(column.total),
      label: dayLabel(column.day, true) + (column.partial ? ", so far" : ""),
      lines: column.parts
        .filter(function (part) {
          return part.value > 0 && series.length > 1;
        })
        .map(function (part) {
          return part.label + ": " + moneyText(part.value);
        })
        .concat(
          column.changes.map(function (change) {
            return "You changed: " + change.label;
          })
        ),
    };
    column.open = opener(opts, "open", column.day);
  });
  var hits = ctx.layer("hits").selectAll("rect.chart-hit").data(columns, function (d) {
    return d.day;
  });
  hits.exit().remove();
  var hitsAll = hits.enter().append("rect").attr("class", "chart-hit").merge(hits);
  hitsAll
    .attr("x", function (d) {
      return x(d.day) - (x.step() - x.bandwidth()) / 2;
    })
    .attr("width", x.step())
    .attr("y", 0)
    .attr("height", inner.h);
  hover(hitsAll, ctx);
  ctx.link(hitsAll, "day", function (d) {
    return d.day;
  });

  var peak = columns.reduce(function (best, c) {
    return c.total > best.total ? c : best;
  }, columns[0]);
  // The window's whole sessions, when they read differently from the
  // replies drawn: the Spend figure beside this chart counts them. More
  // when sessions began before the first day; less only when a window that
  // starts part way through a day (the last hour or 24 hours, or since
  // your last change) draws the quarter hour before it began. Days run
  // midnight to midnight, local time.
  var sessionsUsd = data && typeof data.sessionsTotal === "number" && data.sessionsTotal > 0 ? data.sessionsTotal : null;
  var sessionsTotal = sessionsUsd !== null && moneyText(sessionsUsd) !== moneyText(grand) ? moneyText(sessionsUsd) : null;
  var variant = sessionsTotal ? (sessionsUsd > grand ? "sessions" : "firstDay") : null;
  // One day: no busiest day to name, and no first day of several.
  if (days.length === 1) variant = { sessions: "oneDaySessions", firstDay: "oneDayFirstDay" }[variant] || "oneDay";
  return {
    variant: variant,
    facts: {
      total: moneyText(grand),
      span: spanText(days),
      sessionsTotal: sessionsTotal,
      peakDay: dayLabel(peak.day, true),
      peak: moneyText(peak.total),
    },
    points: columns.map(function (c) {
      return {
        x: ox + x(c.day) + x.bandwidth() / 2,
        band: { x: ox + x(c.day) + inset, y: oy + y(c.total), w: width, h: Math.max(2, inner.h - y(c.total)) },
        tip: c.tip,
        key: c.day,
        open: c.open,
      };
    }),
    table: {
      columns: [{ key: "day", label: "Day", kind: "str" }]
        .concat(
          series.map(function (s) {
            return { key: s.key, label: s.label, kind: "money" };
          })
        )
        .concat(series.length > 1 ? [{ key: "total", label: "Total", kind: "money" }] : []),
      rows: columns.map(function (c) {
        var row = { day: c.day, total: c.total };
        c.parts.forEach(function (part) {
          row[part.key] = part.value;
        });
        return row;
      }),
    },
    legend:
      series.length > 1
        ? series.map(function (s) {
            return { label: s.label, colour: entityColour(kind, s.key) };
          })
        : [],
    note: "Days run midnight to midnight, local time.",
  };
}

// -- 9. change timeline: cost per reply, stepped at each change ---------------------------

// data: {rows, changes, first, last}. rows are /api/daily-usage's (day,
// turns, cost; any split: the day's rows are summed). changes: [{day,
// label, open}] from /api/impact, drawn as labelled rules. first and last
// (windowSpan) widen the axis to the whole window.
//
// Each day is a dot at its cost per reply. The days between one change
// and the next are one period, drawn as a flat line at its own average
// (the period's cost over its replies), so a step down at a change is
// the saving at a glance. A change's own day counts after it, so each
// step sits on its change's rule. The flat lines are this chart's own
// sums, over every project picked; a change's card compares it like
// with like.
function changeSteps(ctx, data) {
  var opts = ctx.opts;
  var rows = (data && data.rows) || [];
  var byDay = {};
  rows.forEach(function (row) {
    if (!row.day) return;
    var day = byDay[row.day] || (byDay[row.day] = { cost: 0, turns: 0 });
    day.cost += num(row.cost);
    day.turns += num(row.turns);
  });
  var seen = Object.keys(byDay)
    .filter(function (day) {
      return byDay[day].turns > 0;
    })
    .sort();
  if (seen.length < 2) return { empty: opts.empty || "Fewer than two days with replies in this window, so there's no trend to show." };

  var first = data.first && data.first < seen[0] ? data.first : seen[0];
  var last = data.last && data.last > seen[seen.length - 1] ? data.last : seen[seen.length - 1];
  var days = dayRange(first, last);
  var changes = ((data && data.changes) || [])
    .filter(function (change) {
      return change.day > days[0] && days.indexOf(change.day) !== -1;
    })
    .sort(function (a, b) {
      return a.day < b.day ? -1 : a.day > b.day ? 1 : 0;
    });

  // The periods: from the window's start to the first change, from each
  // change to the next, and from the last to the window's end.
  var starts = [days[0]].concat(
    changes.map(function (change) {
      return change.day;
    })
  );
  var periods = [];
  starts.forEach(function (start, i) {
    if (periods.length && periods[periods.length - 1].from === start) {
      // Two changes the same day: one period after both.
      periods[periods.length - 1].changes.push(changes[i - 1]);
      return;
    }
    periods.push({ from: start, changes: i ? [changes[i - 1]] : [] });
  });
  periods.forEach(function (period, i) {
    var next = periods[i + 1];
    period.days = days.filter(function (day) {
      return day >= period.from && (!next || day < next.from);
    });
    var cost = 0;
    var turns = 0;
    period.days.forEach(function (day) {
      if (!byDay[day]) return;
      cost += byDay[day].cost;
      turns += byDay[day].turns;
    });
    period.turns = turns;
    period.value = turns > 0 ? cost / turns : null;
    var before = i ? periods[i - 1] : null;
    period.delta = before && before.value && period.value !== null ? ((period.value - before.value) / before.value) * 100 : null;
    period.name = i ? "After " + period.changes.map(function (c) { return c.label; }).join(" and ") : changes.length ? "Before " + changes[0].label : "This window";
  });
  var points = days.map(function (day) {
    var totals = byDay[day];
    return { day: day, value: totals && totals.turns > 0 ? totals.cost / totals.turns : null, turns: totals ? totals.turns : 0, cost: totals ? totals.cost : 0 };
  });

  var axis = moneyAxis();
  var inner = ctx.size(opts.height || 260, { top: changes.length ? 40 : 24, right: 72 });
  var x = d3.scaleBand().domain(days).range([0, inner.w]).paddingInner(0.3).paddingOuter(0.15);
  var high = d3.max(points, function (p) {
    return p.value || 0;
  });
  var y = d3.scaleLinear().domain([0, high * 1.1 || 1]).range([inner.h, 0]);
  valueAxis(ctx.layer("axis-y"), y, { orient: "left", span: inner.w, values: moneyTicks(y, axis, 4, true), format: axis.tick });
  bandAxis(sublayer(ctx.layer("axis-x"), "base", 0, inner.h), x, {
    minGap: 56,
    format: function (day) {
      return dayLabel(day);
    },
  });
  ctx.unit(axis.unit + ", per reply");
  var cx = function (day) {
    return x(day) + x.bandwidth() / 2;
  };

  // The days, as dots joined where they follow each other.
  var marks = ctx.layer("marks");
  var trail = marks.selectAll("path.chart-line.change-trail").data([points]);
  trail
    .enter()
    .append("path")
    .attr("class", "chart-line change-trail")
    .merge(trail)
    .attr(
      "d",
      d3
        .line()
        .defined(function (p) {
          return p.value !== null;
        })
        .x(function (p) {
          return cx(p.day);
        })
        .y(function (p) {
          return y(p.value);
        })
    );
  var shown = points.filter(function (p) {
    return p.value !== null;
  });
  var dots = marks.selectAll("circle.chart-dot").data(shown, function (p) {
    return p.day;
  });
  dots.exit().remove();
  var dotsAll = dots
    .enter()
    .append("circle")
    .attr("class", "chart-dot change-day")
    .attr("r", 3.5)
    .attr("cx", function (p) {
      return cx(p.day);
    })
    .attr("cy", inner.h)
    .merge(dots);
  morph(dotsAll, ctx, shown.length)
    .attr("cx", function (p) {
      return cx(p.day);
    })
    .attr("cy", function (p) {
      return y(p.value);
    });

  // Each period's average, a flat line across its days, with its figure
  // at the right end.
  var steps = ctx.layer("steps");
  steps.selectAll("*").remove();
  periods.forEach(function (period) {
    if (period.value === null || !period.days.length) return;
    var x0 = x(period.days[0]);
    var x1 = x(period.days[period.days.length - 1]) + x.bandwidth();
    var py = Math.round(y(period.value)) + 0.5;
    steps.append("line").attr("class", "change-step").attr("x1", x0).attr("x2", x1).attr("y1", py).attr("y2", py);
    steps
      .append("text")
      .attr("class", "chart-value change-step-value")
      .attr("x", x1 + 4)
      .attr("y", py)
      .attr("dy", "0.32em")
      .text(axis.tick(period.value));
  });

  // The changes, as labelled rules; a label that leads somewhere is a
  // link the keyboard reaches. Its step, up or down, is in the label.
  var rules = ctx.layer("rules");
  var ruleLinks = ctx.layer("rule-links", { links: true });
  rules.selectAll("*").remove();
  ruleLinks.selectAll("*").remove();
  var ruleXs = changes.map(function (change) {
    return Math.round(x(change.day)) - 0.5;
  });
  var spans = [];
  var bounds = [-ctx.margin.left + 4, inner.w + ctx.margin.right - 2];
  periods.forEach(function (period) {
    if (!period.changes.length) return;
    var rx = Math.round(x(period.from)) - 0.5;
    rules.append("line").attr("class", "chart-rule").attr("x1", rx).attr("x2", rx).attr("y1", -8).attr("y2", inner.h);
    var named = period.changes
      .map(function (change) {
        return change.label;
      })
      .join(" and ");
    var full = named + (period.delta === null ? "" : ": " + signedPercent(Math.round(period.delta)) + " a reply");
    var lead = period.changes[0];
    var label = (lead.open ? ruleLinks : rules).append("text").attr("class", "chart-rule-label").attr("x", rx + 4).attr("y", -12).text(fitLabel(full, 40));
    placeRuleLabel(label, full, rx, ruleXs, spans, bounds);
    if (!lead.open) return;
    label
      .classed("is-openable", true)
      .attr("tabindex", 0)
      .attr("role", "link")
      .attr("aria-label", full + ", changed on " + dayLabel(period.from, true) + ": see what it did")
      .on("click", lead.open)
      .on("keydown", function (event) {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        event.stopPropagation();
        lead.open();
      });
  });

  var periodOf = {};
  periods.forEach(function (period) {
    period.days.forEach(function (day) {
      periodOf[day] = period;
    });
  });
  points.forEach(function (p) {
    var period = periodOf[p.day];
    p.tip = {
      value: p.value === null ? "No replies" : moneyText(p.value) + " a reply",
      label: dayLabel(p.day, true),
      lines: (p.turns ? [thousands(p.turns) + (p.turns === 1 ? " reply, " : " replies, ") + moneyText(p.cost)] : []).concat(
        period && period.value !== null ? [period.name + ": " + moneyText(period.value) + " a reply on average"] : []
      ),
    };
    p.open = opener(opts, "open", p.day);
  });
  var hits = ctx.layer("hits").selectAll("rect.chart-hit").data(points, function (p) {
    return p.day;
  });
  hits.exit().remove();
  var hitsAll = hits.enter().append("rect").attr("class", "chart-hit").merge(hits);
  hitsAll
    .attr("x", function (p) {
      return x(p.day) - (x.step() - x.bandwidth()) / 2;
    })
    .attr("width", x.step())
    .attr("y", 0)
    .attr("height", inner.h);
  hover(hitsAll, ctx);

  var ox = ctx.margin.left;
  var oy = ctx.margin.top;
  var lastPeriod = periods[periods.length - 1];
  var before = periods.length > 1 ? periods[periods.length - 2] : null;
  var variant = !changes.length ? "none" : lastPeriod.value === null ? "early" : null;
  return {
    variant: variant,
    facts: {
      change: lastPeriod.changes.length ? lastPeriod.changes.map(function (c) { return c.label; }).join(" and ") : "",
      day: dayLabel(lastPeriod.from, true),
      before: before && before.value !== null ? moneyCell(before.value) : "",
      after: lastPeriod.value !== null ? moneyCell(lastPeriod.value) : "",
      delta: lastPeriod.delta === null ? "no earlier replies to compare" : signedPercent(Math.round(lastPeriod.delta)),
      average: moneyCell(periods[0].value || 0),
      span: spanText(days),
    },
    points: points.map(function (p) {
      return {
        x: ox + cx(p.day),
        // A day without replies has no dot: the keyboard's ring sits on
        // the baseline there.
        y: oy + (p.value === null ? inner.h : y(p.value)),
        band: p.value === null ? null : { x: ox + cx(p.day) - 5, y: oy + y(p.value) - 5, w: 10, h: 10 },
        tip: p.tip,
        key: p.day,
        open: p.open,
      };
    }),
    table: {
      columns: [
        { key: "day", label: "Day", kind: "str" },
        { key: "turns", label: "Replies", kind: "int" },
        { key: "cost", label: "Cost", kind: "money" },
        { key: "per_reply", label: "Cost per reply", kind: "money" },
        { key: "period", label: "Period", kind: "str" },
      ],
      rows: points.map(function (p) {
        var period = periodOf[p.day];
        return { day: p.day, turns: p.turns, cost: p.cost, per_reply: p.value, period: period ? period.name : "" };
      }),
    },
    legend: [],
    note: "Each flat line is the average cost per reply between two changes, over the projects shown. A change's own day counts after it. Days run midnight to midnight, local time.",
  };
}

// -- 2. savings levers: bars hatched by how sure they are -----------------------------------

// How sure a figure is, as the end of a sentence ("..., an estimate").
var BASIS_PHRASES = {
  measured: "measured from your sessions",
  estimate: "an estimate",
  ceiling: "the most it could be",
  simulated: "from replaying your sessions",
  calibrated: "an estimate checked against earlier changes",
};
var BASIS_SHORT = { estimate: "estimate", ceiling: "at most", simulated: "simulated", calibrated: "calibrated" };

function basisLabel(basis) {
  return BASIS[basis] ? BASIS[basis].label : "Measured";
}

// The four levers chart 2 compares, from their report tables, keyed by
// table name. Each says where its figure comes from (source, row), for
// the link to its evidence.
export function savingsLevers(tables) {
  tables = tables || {};
  var levers = [];
  var carry = tableObjects(tables.carry_truncation_savings);
  if (carry.length) {
    var cap = carry.reduce(function (best, row) {
      return num(row.usd_saved) > num(best.usd_saved) ? row : best;
    }, carry[0]);
    levers.push({
      key: "carry",
      label: "Trim long tool output",
      usd: num(cap.usd_saved),
      basis: "ceiling",
      detail: "Each result cut to " + thousands(cap.truncate_to_tokens) + " tokens",
      source: "carry.carry_truncation_savings",
      row: String(cap.truncate_to_tokens),
    });
  }
  var windows = tableObjects(tables.compaction_sim_by_window).filter(function (row) {
    return row.window !== "none";
  });
  if (windows.length) {
    var cheapest = windows.reduce(function (best, row) {
      return num(row.delta_usd) < num(best.delta_usd) ? row : best;
    }, windows[0]);
    if (num(cheapest.delta_usd) < 0) {
      levers.push({
        key: "compaction_sim",
        label: "Summarise conversations at a better size",
        usd: -num(cheapest.delta_usd),
        basis: "simulated",
        detail: "Summarising at " + cheapest.window + " tokens",
        source: "compaction_sim.compaction_sim_by_window",
        row: String(cheapest.window),
      });
    }
  }
  // Each agent type's cheapest alternative, added up: the most a model
  // change in settings or an agent file could save (a subagent's saving
  // counts only the runs its agent file decides), so the model-tier
  // action (a subset of these agent types) never shows a bigger figure
  // than its lever.
  var movable = tableObjects(tables.model_swap_by_agent_type).filter(function (row) {
    return num(row.saving_usd) > 0;
  });
  if (movable.length) {
    var biggest = movable.reduce(function (best, row) {
      return num(row.saving_usd) > num(best.saving_usd) ? row : best;
    }, movable[0]);
    var subagentTypes = movable.filter(function (row) {
      return row.agent_type !== "top-level";
    }).length;
    var mainToo = subagentTypes < movable.length;
    levers.push({
      key: "model_swap",
      label: "Move work to a cheaper model",
      usd: movable.reduce(function (sum, row) {
        return sum + num(row.saving_usd);
      }, 0),
      basis: "ceiling",
      detail:
        (mainToo ? "Your main session and " : "") +
        thousands(subagentTypes) +
        (subagentTypes === 1 ? " subagent type" : " subagent types") +
        " could move",
      source: "model_swap.model_swap_by_agent_type",
      row: String(biggest.agent_type),
    });
  }
  var waste = tableObjects(tables.waste_summary)[0];
  if (waste) {
    levers.push({
      key: "waste",
      label: "Avoid wasted replies",
      usd: num(waste.wasted_cost_usd),
      basis: "ceiling",
      detail: thousands(waste.wasted_turns) + " replies did no useful work",
      source: "waste.waste_summary",
      row: String(waste.metric),
    });
  }
  return levers;
}

// data: [{key, label, usd, basis, detail}] (savingsLevers) or {bars}.
// opts.open(lever) leads to the lever's section and actions.
function bars(ctx, data) {
  var opts = ctx.opts;
  var items = (Array.isArray(data) ? data : (data && data.bars) || [])
    .filter(function (d) {
      return num(d.usd) > 0;
    })
    .slice()
    .sort(function (a, b) {
      return num(b.usd) - num(a.usd);
    });
  if (items.length < MIN_POINTS) {
    return { empty: opts.empty || "Fewer than three changes would save anything in this window, so there's nothing to compare." };
  }
  var axis = moneyAxis();
  var rowH = 44;
  var inner = ctx.size(items.length * rowH + 56, { left: 16, right: 112, top: 12, bottom: 44 });
  var x = d3
    .scaleLinear()
    .domain([
      0,
      d3.max(items, function (d) {
        return num(d.usd);
      }),
    ])
    .range([0, inner.w]);
  valueAxis(sublayer(ctx.layer("axis-x"), "base", 0, inner.h), x, { orient: "bottom", span: inner.h, values: moneyTicks(x, axis, 4, true), format: axis.tick });
  ctx.unit(axis.unit, "bottom");
  var ox = ctx.margin.left;
  var oy = ctx.margin.top;
  var colour = "var(--chart-1)";
  items.forEach(function (d, i) {
    d.top = i * rowH + 18;
    d.tip = { value: moneyText(d.usd), label: d.label, lines: [BASIS_PHRASES[d.basis] || BASIS_PHRASES.measured].concat(d.detail ? [d.detail] : []) };
    d.tip.lines[0] = d.tip.lines[0].charAt(0).toUpperCase() + d.tip.lines[0].slice(1);
    d.open = opener(opts, "open", d);
  });

  var marks = ctx.layer("marks");
  var rows = marks.selectAll("g.chart-row").data(items, function (d) {
    return d.key;
  });
  rows.exit().remove();
  var entered = rows.enter().append("g").attr("class", "chart-row");
  entered.append("text").attr("class", "chart-label");
  entered
    .append("path")
    .attr("class", "chart-bar")
    .attr("d", function (d) {
      return roundedBar(0, d.top, 0, BAR, "right", 0);
    });
  entered.append("text").attr("class", "chart-value");
  var all = entered.merge(rows);
  all
    .select("text.chart-label")
    .attr("x", 0)
    .attr("y", function (d) {
      return d.top - 6;
    })
    .text(function (d) {
      return d.label;
    });
  var bar = all
    .select("path.chart-bar")
    .attr("fill", function (d) {
      return d.basis && d.basis !== "measured" ? ctx.hatch(colour) : colour;
    })
    .attr("stroke", colour)
    .classed("is-hatched", function (d) {
      return d.basis && d.basis !== "measured";
    });
  morph(bar, ctx, items.length).attr("d", function (d) {
    return roundedBar(0, d.top, x(num(d.usd)), BAR, "right", 4);
  });
  morph(all.select("text.chart-value"), ctx, items.length)
    .attr("x", function (d) {
      return x(num(d.usd)) + 8;
    })
    .attr("y", function (d) {
      return d.top + BAR / 2;
    })
    .attr("dy", "0.32em")
    .text(function (d) {
      var short = BASIS_SHORT[d.basis];
      return axis.tick(num(d.usd)) + (short ? " " + short : "");
    });

  var hits = ctx.layer("hits").selectAll("rect.chart-hit").data(items, function (d) {
    return d.key;
  });
  hits.exit().remove();
  var hitsAll = hits.enter().append("rect").attr("class", "chart-hit").merge(hits);
  hitsAll
    .attr("x", 0)
    .attr("width", inner.w)
    .attr("y", function (d) {
      return d.top - 18;
    })
    .attr("height", rowH);
  hover(hitsAll, ctx);

  var top = items[0];
  var hatched = items.some(function (d) {
    return d.basis && d.basis !== "measured";
  });
  return {
    facts: { top: top.label, topAmount: moneyText(top.usd), topBasis: BASIS_PHRASES[top.basis] || BASIS_PHRASES.measured },
    points: items.map(function (d) {
      return { x: ox + x(num(d.usd)), band: { x: ox, y: oy + d.top, w: x(num(d.usd)), h: BAR }, tip: d.tip, key: d.key, open: d.open };
    }),
    table: {
      columns: [
        { key: "label", label: "Change", kind: "str" },
        { key: "usd", label: "Saving", kind: "money" },
        {
          key: "basis",
          label: "How sure",
          kind: "str",
          value: function (row) {
            return basisLabel(row.basis);
          },
        },
      ],
      rows: items,
    },
    legend: [],
    note: hatched ? "A hatched bar isn't measured: it's an estimate, a simulation, or the most the change could save." : null,
  };
}

// -- 3. summary point: a line with the cheapest size marked ------------------------------------

// data: compaction_sim_by_window (a report table, or {table}).
// opts.open() leads to the conversation-summary recommendation.
function line(ctx, data) {
  var opts = ctx.opts;
  var table = tableOf(data);
  var rows = tableObjects(table);
  var now = rows.filter(function (row) {
    return row.window === "none";
  })[0];
  var sizes = rows
    .filter(function (row) {
      return row.window !== "none";
    })
    .map(function (row) {
      return {
        key: String(row.window),
        tokens: num(String(row.window).replace(/,/g, "")),
        cost: num(row.cost),
        delta: num(row.delta_usd),
        perSession: num(row.compactions_per_session),
      };
    })
    .filter(function (d) {
      return d.tokens > 0;
    })
    .sort(function (a, b) {
      return a.tokens - b.tokens;
    });
  if (!now || sizes.length < MIN_POINTS) return { empty: opts.empty || "Too few summary sizes were tried in this window to compare them." };

  var nowCost = num(now.cost);
  var cheapest = sizes.reduce(function (best, d) {
    return d.cost < best.cost ? d : best;
  }, sizes[0]);
  var variant = cheapest.cost < nowCost ? null : "cheapest";
  // A size far costlier than the rest would flatten the others into a
  // line: it's drawn at the top edge, marked off the scale.
  var costs = sizes
    .map(function (d) {
      return d.cost;
    })
    .concat([nowCost]);
  var typical = d3.median(costs);
  var cap =
    d3.max(
      costs.filter(function (cost) {
        return cost <= typical * 2.5;
      })
    ) || typical;
  sizes.forEach(function (d) {
    d.offscale = d.cost > cap;
  });
  var low = d3.min(costs);
  var pad = (cap - low) * 0.12 || cap * 0.1 || 1;

  var axis = moneyAxis();
  var inner = ctx.size(opts.height || 240, { bottom: 44, right: 104 });
  var x = d3
    .scaleLinear()
    .domain([sizes[0].tokens, sizes[sizes.length - 1].tokens])
    .range([0, inner.w]);
  var y = d3
    .scaleLinear()
    .domain([Math.max(0, low - pad), cap + pad])
    .range([inner.h, 0]);
  valueAxis(ctx.layer("axis-y"), y, { orient: "left", span: inner.w, values: moneyTicks(y, axis, 4, true), format: axis.tick });
  valueAxis(sublayer(ctx.layer("axis-x"), "base", 0, inner.h), x, { orient: "bottom", span: 0, ticks: 6, format: tokenTick });
  ctx.unit(axis.unit);
  ctx.unit("Conversation size when summarised (tokens)", "bottom");
  var ox = ctx.margin.left;
  var oy = ctx.margin.top;
  function px(d) {
    return x(d.tokens);
  }
  function py(d) {
    return d.offscale ? 0 : y(d.cost);
  }

  // Now, as a labelled rule: every size is read against it.
  var rules = ctx.layer("rules");
  var ny = Math.round(y(nowCost)) + 0.5;
  var rule = rules.select("line.chart-rule");
  if (rule.empty()) rule = rules.append("line").attr("class", "chart-rule");
  var ruleLabel = rules.select("text.chart-rule-label");
  if (ruleLabel.empty()) ruleLabel = rules.append("text").attr("class", "chart-rule-label").attr("dy", "0.32em");
  morph(rule, ctx, sizes.length).attr("x1", 0).attr("x2", inner.w).attr("y1", ny).attr("y2", ny);
  morph(ruleLabel, ctx, sizes.length)
    .attr("x", inner.w + 8)
    .attr("y", ny)
    .text("Now: " + axis.tick(nowCost));

  var marks = ctx.layer("marks");
  var path = marks.select("path.chart-line");
  if (path.empty()) path = marks.append("path").attr("class", "chart-line is-simulated").attr("stroke", "var(--chart-1)");
  var drawLine = d3
    .line()
    .defined(function (d) {
      return !d.offscale;
    })
    .x(px)
    .y(py)
    .curve(d3.curveMonotoneX);
  morph(path, ctx, sizes.length).attr("d", drawLine(sizes));

  sizes.forEach(function (d) {
    var lines = [];
    if (d.cost < nowCost) lines.push(moneyText(nowCost - d.cost) + " less than now");
    else if (d.cost > nowCost) lines.push(moneyText(d.cost - nowCost) + " more than now");
    else lines.push("The same as now");
    lines.push(Number(d.perSession.toFixed(1)) + " summaries per session");
    if (d.offscale) lines.push("Off the top of the scale");
    d.tip = { value: moneyText(d.cost), label: "Summarised at " + thousands(d.tokens) + " tokens", lines: lines };
    d.open = opener(opts, "open", d.key);
    d.best = d === cheapest && !variant;
  });
  var dots = marks.selectAll("g.chart-point").data(sizes, function (d) {
    return d.key;
  });
  dots.exit().remove();
  var entered = dots.enter().append("g").attr("class", "chart-point");
  entered.append("circle").attr("class", "chart-dot").attr("r", 0);
  entered.append("path").attr("class", "chart-offscale").attr("d", "M-5,4L0,-4L5,4Z");
  var all = entered.merge(dots);
  morph(all, ctx, sizes.length).attr("transform", function (d) {
    return "translate(" + px(d) + "," + py(d) + ")";
  });
  all
    .select("circle")
    .classed("is-best", function (d) {
      return d.best;
    })
    .attr("fill", "var(--chart-1)")
    .attr("display", function (d) {
      return d.offscale ? "none" : null;
    });
  morph(all.select("circle"), ctx, sizes.length).attr("r", function (d) {
    return d.best ? 6 : 4;
  });
  all.select("path.chart-offscale").attr("display", function (d) {
    return d.offscale ? null : "none";
  });

  var bestLabel = marks.select("text.chart-label");
  if (bestLabel.empty()) bestLabel = marks.append("text").attr("class", "chart-label").attr("text-anchor", "middle");
  bestLabel.attr("display", variant ? "none" : null);
  if (!variant) {
    morph(bestLabel, ctx, sizes.length)
      .attr("x", px(cheapest))
      .attr("y", py(cheapest) + 22)
      .text("Cheapest: " + tokenTick(cheapest.tokens));
  }

  // Hover reads the nearest size along the line.
  var hits = ctx.layer("hits");
  var overlay = hits.select("rect.chart-hit");
  if (overlay.empty()) overlay = hits.append("rect").attr("class", "chart-hit");
  var xs = sizes.map(px);
  function nearest(event) {
    return sizes[d3.bisectCenter(xs, d3.pointer(event, overlay.node())[0])];
  }
  overlay
    .attr("x", 0)
    .attr("y", 0)
    .attr("width", inner.w)
    .attr("height", inner.h)
    .on("mousemove.tip", function (event) {
      var d = nearest(event);
      all.classed("is-hover", function (other) {
        return other === d;
      });
      ctx.tooltip(event, d.tip);
    })
    .on("mouseleave.tip", function () {
      all.classed("is-hover", false);
      ctx.hideTooltip();
    })
    .on("click.open", function (event) {
      var d = nearest(event);
      if (d.open) d.open();
    });

  return {
    variant: variant,
    facts: { best: thousands(cheapest.tokens), saving: moneyText(nowCost - cheapest.cost) },
    points: sizes.map(function (d) {
      return { x: ox + px(d), y: oy + py(d), tip: d.tip, open: d.open };
    }),
    table: {
      columns: [
        { key: "size", label: "Summarised at", kind: "str" },
        { key: "perSession", label: "Summaries per session", kind: "float" },
        { key: "cost", label: "Cost", kind: "money" },
        { key: "change", label: "Change from now", kind: "money" },
      ],
      rows: [{ size: "As now", perSession: num(now.compactions_per_session), cost: nowCost, change: 0 }].concat(
        sizes.map(function (d) {
          return { size: thousands(d.tokens) + " tokens", perSession: d.perSession, cost: d.cost, change: d.cost - nowCost };
        })
      ),
    },
    legend: [],
    note: "Dashed because it's simulated: your sessions replayed with each summary size.",
  };
}

// -- 4. session outliers: a scatter with a time brush -----------------------------------------

var MODE_SERIES = [
  { key: "interactive", label: "Interactive" },
  { key: "long-agentic", label: "Long agent runs" },
  { key: "overnight", label: "Overnight" },
  { key: "other", label: "Mixed or not known" },
];

function modeKey(mode) {
  return mode === "interactive" || mode === "long-agentic" || mode === "overnight" ? mode : "other";
}

function modeLabel(key) {
  for (var i = 0; i < MODE_SERIES.length; i++) if (MODE_SERIES[i].key === key) return MODE_SERIES[i].label;
  return key;
}

// A session's colour on the scatter, for its row's swatch in the grid.
export function modeColour(mode) {
  return entityColour("mode", modeKey(mode));
}

// Tick values for a log scale: 1, 2 and 5 per decade, or only the
// powers of ten when that's too many.
function logTicks(domain, factor) {
  // Stepped in the axis's own unit (factor per USD): 1%, 2%, 5% of the
  // weekly limit rather than $1, $2, $5 written as shares.
  var k = factor || 1;
  var low = domain[0] * k;
  var high = domain[1] * k;
  var ticks = [];
  for (var power = Math.floor(Math.log10(low)); power <= Math.ceil(Math.log10(high)); power++) {
    [1, 2, 5].forEach(function (step) {
      var value = step * Math.pow(10, power);
      if (value >= low && value <= high) ticks.push({ value: value / k, step: step });
    });
  }
  if (ticks.length > 6) {
    ticks = ticks.filter(function (tick) {
      return tick.step === 1;
    });
  }
  return ticks.map(function (tick) {
    return tick.value;
  });
}

// The sessions a scatter leaves out, as a sentence without its full
// stop: "3 cost nothing and aren't plotted". Null when it draws them all.
function unplottedText(sessions) {
  var free = 0;
  var undated = 0;
  sessions.forEach(function (d) {
    if (!(d.cost > 0)) free += 1;
    else if (isNaN(d.ms)) undated += 1;
  });
  var left = free + undated;
  if (!left) return null;
  var plotted = left === 1 ? " isn't plotted" : " aren't plotted";
  if (free && undated) return thousands(free) + " cost nothing and " + thousands(undated) + (undated === 1 ? " has" : " have") + " no start time, so " + thousands(left) + plotted;
  if (free) return thousands(free) + " cost nothing and" + plotted;
  return thousands(undated) + (undated === 1 ? " has" : " have") + " no start time and" + plotted;
}

// data: /api/sessions rows, or {sessions}. opts.open(row) opens the
// session; opts.brushed([fromMs, toMs] or null) filters the page's grid.
function scatter(ctx, data) {
  var opts = ctx.opts;
  var frame = ctx.frame;
  var rows = Array.isArray(data) ? data : (data && data.sessions) || [];
  var sessions = rows.map(function (row) {
    return { id: row.id, ms: Date.parse(row.first_ts), cost: num(row.total_cost), mode: modeKey(row.mode), row: row };
  });
  // A log scale has no place for a session that cost nothing.
  var dots = sessions.filter(function (d) {
    return !isNaN(d.ms) && d.cost > 0;
  });
  if (dots.length < MIN_POINTS) return { empty: opts.empty || "Fewer than three sessions cost anything in this window." };

  var axis = moneyAxis();
  var inner = ctx.size(opts.height || 280, { left: 64 });
  var span = d3.extent(dots, function (d) {
    return d.ms;
  });
  if (span[0] === span[1]) span = [span[0] - 3600000, span[1] + 3600000];
  var x = d3.scaleUtc().domain(span).range([0, inner.w]);
  var costs = d3.extent(dots, function (d) {
    return d.cost;
  });
  var y = d3
    .scaleLog()
    .domain([costs[0] / 1.4, costs[1] * 1.4])
    .range([inner.h, 0]);
  var short = span[1] - span[0] < 2 * DAY_MS;
  var timeFormat = d3.utcFormat("%H:%M");
  valueAxis(ctx.layer("axis-y"), y, { orient: "left", span: inner.w, values: logTicks(y.domain(), axis.factor), format: axis.tick });
  valueAxis(sublayer(ctx.layer("axis-x"), "base", 0, inner.h), x, {
    orient: "bottom",
    span: 0,
    ticks: Math.max(2, Math.min(8, Math.floor(inner.w / 110))),
    format: function (value) {
      return short ? timeFormat(value) : dayLabel(value);
    },
  });
  ctx.unit(axis.unit + " (log scale)");
  var ox = ctx.margin.left;
  var oy = ctx.margin.top;

  dots.sort(function (a, b) {
    return a.ms - b.ms;
  });
  dots.forEach(function (d) {
    d.cx = x(d.ms);
    d.cy = y(d.cost);
    d.tip = {
      value: moneyText(d.cost),
      label: projectName(d.row.slug),
      lines: [shortTs(d.row.first_ts), "Ran for " + formatDuration(d.row.span_s), modeLabel(d.mode)],
    };
    d.open = opener(opts, "open", d.row);
  });
  var marks = ctx.layer("marks");
  var circles = marks.selectAll("circle.chart-dot").data(dots, function (d) {
    return d.id;
  });
  circles.exit().remove();
  var entered = circles
    .enter()
    .append("circle")
    .attr("class", "chart-dot")
    .attr("r", 0)
    .attr("cx", function (d) {
      return d.cx;
    })
    .attr("cy", function (d) {
      return d.cy;
    });
  var all = entered.merge(circles).attr("fill", function (d) {
    return entityColour("mode", d.mode);
  });
  morph(all, ctx, dots.length)
    .attr("cx", function (d) {
      return d.cx;
    })
    .attr("cy", function (d) {
      return d.cy;
    })
    .attr("r", 4);
  ctx.link(all, "session", function (d) {
    return d.id;
  });

  // The brush picks a stretch of time; the page filters its grid to it.
  // Hover and click find the nearest session within 24px (a Voronoi
  // search), since the brush lies over the dots.
  var delaunay = d3.Delaunay.from(
    dots,
    function (d) {
      return d.cx;
    },
    function (d) {
      return d.cy;
    }
  );
  function under(event, node) {
    var at = d3.pointer(event, node);
    var d = dots[delaunay.find(at[0], at[1])];
    return d && Math.hypot(d.cx - at[0], d.cy - at[1]) <= 24 ? d : null;
  }
  var brushLayer = ctx.layer("brush").classed("chart-brush", true);
  var brush = d3
    .brushX()
    .extent([
      [0, 0],
      [inner.w, inner.h],
    ])
    .on("end", function (event) {
      if (!event.sourceEvent) return;
      frame.brushRange = event.selection
        ? [x.invert(event.selection[0]).getTime(), x.invert(event.selection[1]).getTime()]
        : null;
      if (typeof opts.brushed === "function") opts.brushed(frame.brushRange);
    });
  brushLayer.call(brush);
  // A press on a session opens it: it never starts or clears the brush.
  brushLayer.selectAll(".overlay, .selection").on("mousedown.open", function (event) {
    if (under(event, brushLayer.node())) event.stopPropagation();
  });
  // New figures clear the brush, and the page's grid with it: a range
  // picked on the old window would filter rows the chart no longer shows.
  if (!ctx.resize && frame.brushRange) {
    frame.brushRange = null;
    if (typeof opts.brushed === "function") opts.brushed(null);
  }
  if (frame.brushRange) brushLayer.call(brush.move, [x(frame.brushRange[0]), x(frame.brushRange[1])]);
  else brushLayer.call(brush.move, null);
  var hovered = null;
  brushLayer
    .on("mousemove.tip", function (event) {
      var d = under(event, this);
      if (d !== hovered) highlight("session", d ? d.id : null);
      hovered = d;
      this.classList.toggle("is-over-mark", !!d);
      if (d) ctx.tooltip(event, d.tip);
      else ctx.hideTooltip();
    })
    .on("mouseleave.tip", function () {
      if (hovered) highlight("session", null);
      hovered = null;
      ctx.hideTooltip();
    })
    .on("click.open", function (event) {
      var d = under(event, this);
      if (d && d.open) d.open();
    });

  var present = MODE_SERIES.filter(function (s) {
    return dots.some(function (d) {
      return d.mode === s.key;
    });
  });
  var sorted = dots
    .map(function (d) {
      return d.cost;
    })
    .sort(function (a, b) {
      return a - b;
    });
  var median = d3.median(sorted) || sorted[0];
  var ratio = sorted[sorted.length - 1] / median;
  // The grid and the Sessions tile count every session: the reading says
  // how many aren't drawn, and why.
  var unplotted = unplottedText(sessions);
  return {
    variant: unplotted ? "unplotted" : null,
    facts: {
      count: thousands(dots.length),
      total: thousands(sessions.length),
      unplotted: unplotted,
      max: moneyText(sorted[sorted.length - 1]),
      ratio: ratio >= 10 ? thousands(ratio) : String(Number(ratio.toFixed(1))),
    },
    points: dots.map(function (d) {
      return { x: ox + d.cx, y: oy + d.cy, tip: d.tip, key: d.id, open: d.open };
    }),
    table: {
      columns: [
        { key: "started", label: "Started", kind: "str" },
        { key: "project", label: "Project", kind: "str" },
        { key: "cost", label: "Cost", kind: "money" },
        { key: "length", label: "Length", kind: "secs" },
        { key: "mode", label: "How it ran", kind: "str" },
      ],
      rows: dots.map(function (d) {
        return { started: shortTs(d.row.first_ts), project: projectName(d.row.slug), cost: d.cost, length: num(d.row.span_s), mode: modeLabel(d.mode) };
      }),
    },
    legend: present.map(function (s) {
      return { label: s.label, colour: entityColour("mode", s.key), key: undefined };
    }),
    // The keyboard's brush: the sessions from one dot to another, in
    // time order (the points are).
    pick: function (from, to) {
      var lo = dots[Math.min(from, to)].ms;
      var hi = dots[Math.max(from, to)].ms;
      frame.brushRange = [lo, hi];
      var x0 = x(lo);
      var x1 = x(hi);
      if (x1 - x0 < 4) {
        x0 -= 2;
        x1 += 2;
      }
      brushLayer.call(brush.move, [Math.max(0, x0), Math.min(inner.w, x1)]);
      if (typeof opts.brushed === "function") opts.brushed(frame.brushRange);
    },
    note: "Drag across the chart, or hold Shift and press the arrow keys, to list only the sessions that started in that time.",
  };
}

// -- 5. session context: the timeline -----------------------------------------------------------

// `/api/session/<id>` (S1-integration fix 1.g, see docs/api.md) adds
// `turn_series`: a list of `[turn_index, ctx, cache_creation_tokens,
// is_recache, preceding_primary]` per priced turn of the session's
// top-level transcript, plus `markers`: `{compactions, spawns,
// human}`, each a list of turn_index values, and (v3-limits wiring)
// `limit_markers`: `[{ts, kind, detail}, ...]` usage-cap pause/resume/
// agent-terminated events. It's absent (rather than an empty list)
// whenever the store has no stored top-level transcript digest to
// source it from -- e.g. a session ingested before the watcher parsed
// a top-level transcript, or one whose digest failed to decode -- so
// the chart says so rather than inventing a curve.
function findPerTurnSeries(session) {
  var series = session && session.turn_series;
  return Array.isArray(series) && series.length ? series : null;
}

function toTurnIndexSet(list) {
  var set = {};
  (list || []).forEach(function (turnIndex) {
    set[turnIndex] = true;
  });
  return set;
}

// What each marker means, in the reader's words.
var MARKER_LABELS = {
  recache: "Cache rebuilt",
  compaction: "Conversation summarised",
  spawn: "Subagent started",
  human: "Your message",
  limit_hit: "Usage limit reached",
  limit_resume: "Resumed after the limit",
  agent_terminated: "Subagent stopped at the limit",
};

// UX-6/9: every timeline marker used to be an identical circle,
// distinguished only by fill colour -- color alone (WCAG 1.4.1), so a
// colorblind viewer or a low-color display can't tell recache from
// compaction from spawn, etc. Each kind now also gets its own shape;
// colour stays as a second, redundant cue rather than the only one.
// ``titleText`` is optional (the legend's own tiny icons pass none).
function markerGlyph(shape, cx, cy, r, fill, titleText) {
  var title = titleText ? "<title>" + titleText + "</title>" : "";
  var pts;
  switch (shape) {
    case "square":
      return (
        '<rect x="' + (cx - r * 0.9).toFixed(1) + '" y="' + (cy - r * 0.9).toFixed(1) +
        '" width="' + (r * 1.8).toFixed(1) + '" height="' + (r * 1.8).toFixed(1) +
        '" fill="' + fill + '">' + title + "</rect>"
      );
    case "triangle-up":
    case "triangle-down":
      var flip = shape === "triangle-down" ? -1 : 1;
      pts = [
        [cx, cy - flip * r * 1.3],
        [cx - r * 1.2, cy + flip * r * 0.9],
        [cx + r * 1.2, cy + flip * r * 0.9],
      ];
      return (
        '<polygon points="' +
        pts.map(function (p) { return p[0].toFixed(1) + "," + p[1].toFixed(1); }).join(" ") +
        '" fill="' + fill + '">' + title + "</polygon>"
      );
    case "diamond":
      pts = [
        [cx, cy - r * 1.3],
        [cx + r * 1.3, cy],
        [cx, cy + r * 1.3],
        [cx - r * 1.3, cy],
      ];
      return (
        '<polygon points="' +
        pts.map(function (p) { return p[0].toFixed(1) + "," + p[1].toFixed(1); }).join(" ") +
        '" fill="' + fill + '">' + title + "</polygon>"
      );
    case "plus":
      return (
        '<rect x="' + (cx - r * 0.35).toFixed(1) + '" y="' + (cy - r * 1.2).toFixed(1) +
        '" width="' + (r * 0.7).toFixed(1) + '" height="' + (r * 2.4).toFixed(1) + '" fill="' + fill + '"></rect>' +
        '<rect x="' + (cx - r * 1.2).toFixed(1) + '" y="' + (cy - r * 0.35).toFixed(1) +
        '" width="' + (r * 2.4).toFixed(1) + '" height="' + (r * 0.7).toFixed(1) + '" fill="' + fill + '">' + title + "</rect>"
      );
    case "x":
      return (
        '<line x1="' + (cx - r * 1.1).toFixed(1) + '" y1="' + (cy - r * 1.1).toFixed(1) +
        '" x2="' + (cx + r * 1.1).toFixed(1) + '" y2="' + (cy + r * 1.1).toFixed(1) +
        '" stroke="' + fill + '" stroke-width="1.6"></line>' +
        '<line x1="' + (cx - r * 1.1).toFixed(1) + '" y1="' + (cy + r * 1.1).toFixed(1) +
        '" x2="' + (cx + r * 1.1).toFixed(1) + '" y2="' + (cy - r * 1.1).toFixed(1) +
        '" stroke="' + fill + '" stroke-width="1.6">' + title + "</line>"
      );
    case "circle":
    default:
      return '<circle cx="' + cx.toFixed(1) + '" cy="' + cy.toFixed(1) + '" r="' + r + '" fill="' + fill + '">' + title + "</circle>";
  }
}

// How much of a line drawing in is still to come, as a share of its
// length: 0 once it is drawn (the dash is cleared at the end).
function lineStillToDraw(line) {
  var dash = line && line.getAttribute("stroke-dasharray");
  var length = dash ? parseFloat(dash) : 0;
  if (!(length > 0)) return 0;
  return Math.min(1, Math.max(0, parseFloat(line.getAttribute("stroke-dashoffset")) / length || 0));
}

// Chart 5: the context size at every turn of one session, with a
// marker where the cache was rebuilt, the conversation was summarised,
// a subagent started or you wrote, and the usage-limit pauses in a strip
// above. data is the /api/session/<id> body.
function buildSessionTimeline(ctx, session) {
  var series = findPerTurnSeries(session);
  if (!series) {
    return { empty: ctx.opts.empty || "No turn-by-turn record for this session: ClaudeGlass hasn't stored its main transcript yet." };
  }
  var markers = session.markers || {};
  var compactionTurns = toTurnIndexSet(markers.compactions);
  var spawnTurns = toTurnIndexSet(markers.spawns);
  var humanTurns = toTurnIndexSet(markers.human);
  // Markers are drawn in ink, not chart colours: the shape tells kinds
  // apart, and ink keeps every glyph at 3:1 or better against the panel
  // in both themes (WCAG 1.4.11), where a light yellow or pink would not.
  var markerColors = { recache: "var(--ink-2)", compaction: "var(--ink-2)", spawn: "var(--ink-2)", human: "var(--ink-2)" };
  // UX-6/9: one shape per kind (see markerGlyph above), never reused
  // across the two marker sets below -- 7 kinds, 7 distinct shapes.
  var markerShapes = { recache: "circle", compaction: "square", spawn: "triangle-up", human: "diamond" };
  // v3-limits wiring: drawn in the strip above the context-size line
  // rather than pinned to a turn's own point -- a usage-limit event's
  // `ts` falls *inside* the pause gap between two turns, not at a
  // turn_index of its own. Positioned instead by interpolating `ts`
  // between the session's own `first_ts`/`last_ts` (docs/limits.md's
  // "Session-timeline marker contract" / docs/ui.md).
  var limitMarkerColors = { limit_hit: "var(--ink-2)", limit_resume: "var(--ink-2)", agent_terminated: "var(--ink-2)" };
  var limitMarkerShapes = { limit_hit: "triangle-down", limit_resume: "plus", agent_terminated: "x" };

  // Finding 10: this used to compute the max via Math.max, spreading
  // the whole per-turn array as individual call arguments -- a
  // session with tens of thousands of turns could blow the engine's
  // argument-count/call-stack limit ("Maximum call stack size
  // exceeded"). A plain loop has no such limit (also cheaper: no
  // intermediate array allocation).
  var maxCtx = 0;
  var peakAt = 0;
  for (var mi = 0; mi < series.length; mi++) {
    var ctxValue = series[mi][1] || 0;
    if (ctxValue > maxCtx) {
      maxCtx = ctxValue;
      peakAt = mi;
    }
  }
  maxCtx = maxCtx || 1;

  var limitMarkers = (Array.isArray(session.limit_markers) ? session.limit_markers : []).filter(function (marker) {
    return marker && !isNaN(Date.parse(marker.ts));
  });
  // One lane per kind of limit event, in the order they happen, so
  // a pause, a stopped subagent and the resume at the same moment
  // never draw over each other and every glyph keeps its true time.
  var limitLanes = ["limit_hit", "agent_terminated", "limit_resume"].filter(function (kind) {
    return limitMarkers.some(function (marker) {
      return marker.kind === kind;
    });
  });
  limitMarkers.forEach(function (marker) {
    if (limitLanes.indexOf(marker.kind) < 0) limitLanes.push(marker.kind);
  });
  var inner = ctx.size(ctx.opts.height || 240, { top: limitLanes.length ? 28 + limitLanes.length * 10 : 24, bottom: 44 });
  function turnOf(turn, i) {
    return turn[0] === null || turn[0] === undefined ? i + 1 : turn[0];
  }
  var firstTurn = turnOf(series[0], 0);
  var lastTurn = turnOf(series[series.length - 1], series.length - 1);
  var x = d3
    .scaleLinear()
    .domain(firstTurn === lastTurn ? [firstTurn - 1, lastTurn + 1] : [firstTurn, lastTurn])
    .range([0, inner.w]);
  var y = d3.scaleLinear().domain([0, maxCtx]).nice(4).range([inner.h, 0]);
  valueAxis(ctx.layer("axis-y"), y, { orient: "left", span: inner.w, ticks: 4, format: tokenTick });
  valueAxis(sublayer(ctx.layer("axis-x"), "base", 0, inner.h), x, {
    orient: "bottom",
    span: 0,
    values: x.ticks(Math.max(2, Math.min(8, Math.floor(inner.w / 90)))).filter(Number.isInteger),
    format: thousands,
  });
  ctx.unit("Tokens in context");
  ctx.unit("Turn", "bottom");
  var ox = ctx.margin.left;
  var oy = ctx.margin.top;

  var points = series.map(function (turn, i) {
    return [x(turnOf(turn, i)), y(turn[1] || 0)];
  });
  // A fresh frame per session, so there's nothing to morph: draw anew.
  // A resize mid draw-in carries on from as much of the line as was
  // drawn, rather than starting it again.
  var plot = ctx.layer("plot");
  var left = ctx.resize ? lineStillToDraw(plot.select(".chart-line").node()) : 1;
  plot.selectAll("*").interrupt().remove();
  var overlay = plot.append("rect").attr("class", "chart-hit").attr("width", inner.w).attr("height", inner.h);
  if (points.length > 1) {
    var path = plot.append("path").attr("class", "chart-line").attr("stroke", "var(--chart-1)").attr("d", d3.line()(points));
    var ms = ctx.duration(points.length);
    if (ms && left > 0) {
      var length = path.node().getTotalLength();
      path
        .attr("stroke-dasharray", length + " " + length)
        .attr("stroke-dashoffset", length * left)
        .transition()
        .delay(ctx.delay)
        .duration(ms)
        .ease(ctx.ease)
        .attr("stroke-dashoffset", 0)
        .on("end", function () {
          d3.select(this).attr("stroke-dasharray", null).attr("stroke-dashoffset", null);
        });
    }
  } else if (points.length === 1) {
    // Finding 11: a single-turn session has exactly one point, and a
    // line needs at least two to draw anything -- it silently rendered
    // nothing at all. Draw the one point as a dot instead.
    plot.append("circle").attr("class", "chart-dot").attr("cx", points[0][0]).attr("cy", points[0][1]).attr("r", 4).attr("fill", "var(--chart-1)");
  }

  var turnEvents = [];
  var marks = [];
  // Markers shrink a little when the turns sit closer than their width.
  var markerSize = inner.w / series.length < 6 ? 3 : 4;
  series.forEach(function (turn, i) {
    var turnIndex = turn[0];
    var kinds = [];
    if (turn[3]) kinds.push("recache");
    if (compactionTurns[turnIndex]) kinds.push("compaction");
    if (spawnTurns[turnIndex]) kinds.push("spawn");
    if (humanTurns[turnIndex]) kinds.push("human");
    turnEvents.push(kinds);
    kinds.forEach(function (kind, k) {
      marks.push({
        shape: markerShapes[kind] || "circle",
        fill: markerColors[kind] || "var(--ink-3)",
        x: points[i][0],
        y: points[i][1] - k * 11,
        tip: { value: MARKER_LABELS[kind], label: "Turn " + thousands(turnOf(turn, i)), lines: [compactNumber(turn[1] || 0) + " tokens in context"] },
      });
    });
  });

  var limitKindsSeen = {};
  var firstMs = Date.parse(session.first_ts);
  var lastMs = Date.parse(session.last_ts);
  var hasTimeRange = !isNaN(firstMs) && !isNaN(lastMs) && lastMs > firstMs;
  // A run of the same event closer than one glyph becomes one mark
  // whose tooltip gives the count and the time span.
  var limitGap = markerSize * 2 + 4;
  var limitMarks = [];
  limitMarkers
    .map(function (marker) {
      var fraction = hasTimeRange ? Math.max(0, Math.min(1, (Date.parse(marker.ts) - firstMs) / (lastMs - firstMs))) : 0;
      return { kind: marker.kind, ts: marker.ts, x: fraction * inner.w };
    })
    .sort(function (a, b) {
      return a.x - b.x;
    })
    .forEach(function (marker) {
      limitKindsSeen[marker.kind] = true;
      var run = null;
      for (var k = limitMarks.length - 1; k >= 0; k--) {
        if (limitMarks[k].kind === marker.kind) {
          run = limitMarks[k];
          break;
        }
      }
      if (run && marker.x - run.x < limitGap) {
        run.count += 1;
        run.lastTs = marker.ts;
        return;
      }
      limitMarks.push({ kind: marker.kind, x: marker.x, count: 1, firstTs: marker.ts, lastTs: marker.ts });
    });
  limitMarks.forEach(function (mark) {
    marks.push({
      shape: limitMarkerShapes[mark.kind] || "circle",
      fill: limitMarkerColors[mark.kind] || "var(--ink-3)",
      x: mark.x,
      y: -14 - (limitLanes.length - 1 - limitLanes.indexOf(mark.kind)) * 10,
      limit: true,
      tip: {
        value: MARKER_LABELS[mark.kind] || "Usage limit event",
        label: mark.count > 1 ? thousands(mark.count) + " times, " + shortTs(mark.firstTs) + " to " + shortTs(mark.lastTs) : shortTs(mark.firstTs),
      },
    });
  });
  var glyphs = plot
    .selectAll("g.chart-marker")
    .data(marks)
    .enter()
    .append("g")
    .attr("class", "chart-marker")
    .attr("transform", function (d) {
      return "translate(" + d.x.toFixed(1) + "," + d.y.toFixed(1) + ")";
    })
    .each(function (d) {
      this.innerHTML = markerGlyph(d.shape, 0, 0, markerSize, d.fill);
    });
  hover(glyphs, ctx);

  // Hover anywhere on the plot reads the nearest turn.
  function turnTip(i) {
    var turn = series[i];
    return {
      value: compactNumber(turn[1] || 0) + " tokens in context",
      label: "Turn " + thousands(turnOf(turn, i)),
      lines: turnEvents[i].map(function (kind) {
        return MARKER_LABELS[kind];
      }),
    };
  }
  var ring = plot.append("circle").attr("class", "chart-hover-ring").attr("r", 6).attr("display", "none");
  var xs = points.map(function (p) {
    return p[0];
  });
  overlay
    .on("mousemove.tip", function (event) {
      var i = d3.bisectCenter(xs, d3.pointer(event, this)[0]);
      ring.attr("display", null).attr("cx", points[i][0]).attr("cy", points[i][1]);
      ctx.tooltip(event, turnTip(i));
    })
    .on("mouseleave.tip", function () {
      ring.attr("display", "none");
      ctx.hideTooltip();
    });

  // UX-6/9: the legend's own swatch mirrors the marker's real shape
  // (not just a colour dot), via the same markerGlyph a viewer just
  // saw drawn on the chart -- so the legend stays a genuine key
  // rather than a second color-only cue.
  function swatchIcon(shape, fill) {
    return '<svg viewBox="0 0 14 14" width="14" height="14" aria-hidden="true">' + markerGlyph(shape, 7, 7, 3, fill) + "</svg>";
  }
  var legend = [{ label: "Context size", colour: "var(--chart-1)", line: true }];
  Object.keys(markerColors).forEach(function (kind) {
    legend.push({ label: MARKER_LABELS[kind], glyph: swatchIcon(markerShapes[kind] || "circle", markerColors[kind]) });
  });
  Object.keys(limitMarkerColors).forEach(function (kind) {
    if (!limitKindsSeen[kind]) return;
    legend.push({ label: MARKER_LABELS[kind], glyph: swatchIcon(limitMarkerShapes[kind] || "circle", limitMarkerColors[kind]) });
  });

  var reading = series
    .map(function (turn, i) {
      return { x: ox + points[i][0], y: oy + points[i][1], tip: turnTip(i) };
    })
    .concat(
      marks
        .filter(function (d) {
          return d.limit;
        })
        .map(function (d) {
          return { x: ox + d.x, y: oy + d.y, tip: d.tip };
        })
    )
    .sort(function (a, b) {
      return a.x - b.x;
    });
  return {
    facts: {
      peak: compactNumber(maxCtx),
      peakTurn: thousands(turnOf(series[peakAt], peakAt)),
      turns: thousands(series.length),
    },
    points: reading,
    table: {
      columns: [
        { key: "turn", label: "Turn", kind: "int" },
        { key: "tokens", label: "Tokens in context", kind: "tokens" },
        { key: "events", label: "What happened", kind: "str" },
      ],
      rows: series.map(function (turn, i) {
        return {
          turn: turnOf(turn, i),
          tokens: turn[1] || 0,
          events: turnEvents[i]
            .map(function (kind) {
              return MARKER_LABELS[kind];
            })
            .join(", "),
        };
      }),
    },
    legend: legend,
    // Finding 11: /api/session/<id> downsamples turn_series above
    // Store.MAX_TURN_SERIES_POINTS -- say so rather than silently
    // showing a thinned-out chart as the complete picture.
    note: session.truncated ? "This session has many turns, so the chart shows a sample of them. Every marked turn is kept." : null,
  };
}

// The session drawer's chart: a new frame for each session opened.
export function sessionContextChart(session, opts) {
  var host = el("div", { class: "chart-host" });
  renderChart(
    host,
    "session-context",
    session,
    Object.assign(
      {
        slot: "session",
        fresh: true,
        emptyNext: "It appears once the service has read the session. Open it again in a minute.",
      },
      opts || {}
    )
  );
  return host;
}

// -- 6. idle gaps: a histogram with the cache lifetimes as rules ------------------------------------

var GAP_ORDER = ["<1m", "1-5m", "5-15m", "15-60m", ">60m"];
// Buckets past the 5-minute lifetime: a rebuild there came from the
// cache expiring.
var PAST_FIVE_MINUTES = { "5-15m": true, "15-60m": true, ">60m": true };

// data: recache_gap_buckets (a report table, or {table}). opts.open(bucket)
// leads to the cache lifetime recommendation.
function histogram(ctx, data) {
  var opts = ctx.opts;
  var table = tableOf(data);
  var byBucket = {};
  tableObjects(table).forEach(function (row) {
    byBucket[row.bucket] = row;
  });
  var buckets = GAP_ORDER.filter(function (key) {
    return byBucket[key];
  }).map(function (key) {
    return { key: key, label: valueLabel(table, key), count: num(byBucket[key].turns), tokens: num(byBucket[key].cc_tokens) };
  });
  var total = sum(
    buckets.map(function (d) {
      return d.count;
    })
  );
  if (buckets.length < MIN_POINTS || !total) return { empty: opts.empty || "No cache rebuilds in this window." };
  var unknown = byBucket.unknown ? num(byBucket.unknown.turns) : 0;

  var inner = ctx.size(opts.height || 240, { top: 40 });
  var x = d3
    .scaleBand()
    .domain(
      buckets.map(function (d) {
        return d.key;
      })
    )
    .range([0, inner.w])
    .paddingInner(0.3)
    .paddingOuter(0.2);
  var width = Math.min(MAX_BAR, x.bandwidth());
  var inset = (x.bandwidth() - width) / 2;
  var y = d3
    .scaleLinear()
    .domain([
      0,
      d3.max(buckets, function (d) {
        return d.count;
      }),
    ])
    .nice(4)
    .range([inner.h, 0]);
  valueAxis(ctx.layer("axis-y"), y, { orient: "left", span: inner.w, ticks: 4, format: thousands });
  bandAxis(sublayer(ctx.layer("axis-x"), "base", 0, inner.h), x, {
    minGap: 40,
    format: function (key) {
      return key === "<1m" ? "Under 1 min" : key === ">60m" ? "Over 1 hour" : key.replace("m", " min");
    },
  });
  ctx.unit("Cache rebuilds");
  var ox = ctx.margin.left;
  var oy = ctx.margin.top;

  buckets.forEach(function (d) {
    d.tip = {
      value: thousands(d.count) + " cache rebuilds",
      label: "After a gap of " + String(d.label).toLowerCase(),
      lines: [percentText(d.count, total) + " of cache rebuilds", compactNumber(d.tokens) + " tokens written again"],
    };
    d.open = opener(opts, "open", d.key);
  });
  var marks = ctx.layer("marks");
  var columns = marks.selectAll("path.chart-bar").data(buckets, function (d) {
    return d.key;
  });
  columns.exit().remove();
  var entered = columns
    .enter()
    .append("path")
    .attr("class", "chart-bar")
    .attr("d", function (d) {
      return roundedBar(x(d.key) + inset, inner.h, width, 0, "top", 0);
    });
  var all = entered.merge(columns).attr("fill", "var(--chart-1)");
  morph(all, ctx, buckets.length).attr("d", function (d) {
    return roundedBar(x(d.key) + inset, y(d.count), width, inner.h - y(d.count), "top", 4);
  });

  // The two cache lifetimes, between the buckets they divide.
  var rules = ctx.layer("rules");
  rules.selectAll("*").remove();
  [
    ["1-5m", "5-15m", "5-minute cache lifetime"],
    ["15-60m", ">60m", "1-hour cache lifetime"],
  ].forEach(function (rule) {
    if (!byBucket[rule[0]] || !byBucket[rule[1]]) return;
    var rx = Math.round((x(rule[0]) + x.bandwidth() + x(rule[1])) / 2) + 0.5;
    rules.append("line").attr("class", "chart-rule").attr("x1", rx).attr("x2", rx).attr("y1", -8).attr("y2", inner.h);
    rules
      .append("text")
      .attr("class", "chart-rule-label")
      .attr("x", rx + 4)
      .attr("y", -12)
      .text(rule[2]);
  });

  var hits = ctx.layer("hits").selectAll("rect.chart-hit").data(buckets, function (d) {
    return d.key;
  });
  hits.exit().remove();
  var hitsAll = hits.enter().append("rect").attr("class", "chart-hit").merge(hits);
  hitsAll
    .attr("x", function (d) {
      return x(d.key) - (x.step() - x.bandwidth()) / 2;
    })
    .attr("width", x.step())
    .attr("y", 0)
    .attr("height", inner.h);
  hover(hitsAll, ctx);

  var over = sum(
    buckets
      .filter(function (d) {
        return PAST_FIVE_MINUTES[d.key];
      })
      .map(function (d) {
        return d.count;
      })
  );
  return {
    facts: { overShare: percentText(over, total) },
    points: buckets.map(function (d) {
      return {
        x: ox + x(d.key) + x.bandwidth() / 2,
        band: { x: ox + x(d.key) + inset, y: oy + y(d.count), w: width, h: Math.max(2, inner.h - y(d.count)) },
        tip: d.tip,
        key: d.key,
        open: d.open,
      };
    }),
    table: {
      columns: [
        { key: "label", label: "Gap before the rebuild", kind: "str" },
        { key: "count", label: "Cache rebuilds", kind: "int" },
        {
          key: "share",
          label: "Share",
          kind: "pct",
          value: function (row) {
            return (row.count / total) * 100;
          },
        },
        { key: "tokens", label: "Tokens written again", kind: "tokens" },
      ],
      rows: buckets,
    },
    legend: [],
    note: unknown ? thousands(unknown) + " cache rebuilds with no known gap aren't shown." : null,
  };
}

// -- 7. lifetime by agent: diverging bars ----------------------------------------------------------

// data: ttl_break_even_share (a report table, or {table}). margin > 0:
// a 1-hour cache lifetime saves; below 0 it costs more. opts.open(key)
// leads to that agent type's cache lifetime action.
function diverging(ctx, data) {
  var opts = ctx.opts;
  var table = tableOf(data);
  var rows = tableObjects(table)
    .map(function (row) {
      return {
        key: String(row.agent_type),
        label: String(valueLabel(table, row.agent_type)),
        margin: num(row.margin),
        premium: num(row.premium_all_1h),
        loss: num(row.expiry_loss_all_5m),
        verdict: String(valueLabel(table, row.verdict) || ""),
      };
    })
    .sort(function (a, b) {
      return b.margin - a.margin;
    });
  if (rows.length < MIN_POINTS) return { empty: opts.empty || "Fewer than three agent types used the cache in this window." };

  var axis = moneyAxis();
  var left = labelWidth(
    rows.map(function (d) {
      return d.label;
    })
  );
  var inner = ctx.size(rows.length * ROW + 52, { left: left, right: 72, top: 8, bottom: 44 });
  var low = Math.min(
    0,
    d3.min(rows, function (d) {
      return d.margin;
    })
  );
  var high = Math.max(
    0,
    d3.max(rows, function (d) {
      return d.margin;
    })
  );
  if (low === high) high = 1;
  // Not rounded out to whole ticks: a small loss beside a large saving
  // would leave half the plot empty.
  var pad = (high - low) * 0.04;
  var x = d3
    .scaleLinear()
    .domain([low < 0 ? low - pad : 0, high > 0 ? high + pad : 0])
    .range([0, inner.w]);
  var zero = Math.round(x(0)) + 0.5;
  valueAxis(sublayer(ctx.layer("axis-x"), "base", 0, inner.h), x, { orient: "bottom", span: inner.h, values: moneyTicks(x, axis, 4, false), format: axis.tick });
  ctx.unit("Saving with a 1-hour lifetime (" + axis.unit + ")", "bottom");
  var ox = ctx.margin.left;
  var oy = ctx.margin.top;
  var chars = Math.floor((left - 16) / 6.8);

  rows.forEach(function (d, i) {
    d.top = i * ROW + (ROW - BAR) / 2;
    d.colour = d.margin > 0 ? "var(--div-pos)" : "var(--div-neg)";
    d.tip = {
      value: d.margin > 0 ? "Saves " + moneyText(d.margin) : d.margin < 0 ? moneyText(-d.margin) + " more" : "No difference",
      label: d.label,
      lines: [
        "1-hour cache writes would cost " + moneyText(d.premium) + " more",
        "Rebuilds after the 5-minute lifetime cost " + moneyText(d.loss),
      ].concat(d.verdict ? [d.verdict] : []),
    };
    d.open = opener(opts, "open", d.key);
  });
  function shape(d) {
    var end = x(d.margin);
    return d.margin >= 0 ? roundedBar(x(0), d.top, end - x(0), BAR, "right", 4) : roundedBar(end, d.top, x(0) - end, BAR, "left", 4);
  }
  var marks = ctx.layer("marks");
  var groups = marks.selectAll("g.chart-row").data(rows, function (d) {
    return d.key;
  });
  groups.exit().remove();
  var entered = groups.enter().append("g").attr("class", "chart-row");
  entered.append("text").attr("class", "chart-label").attr("text-anchor", "end");
  entered
    .append("path")
    .attr("class", "chart-bar")
    .attr("d", function (d) {
      return roundedBar(x(0), d.top, 0, BAR, "right", 0);
    });
  entered.append("text").attr("class", "chart-value");
  var all = entered.merge(groups);
  morph(all.select("text.chart-label"), ctx, rows.length)
    .attr("x", -8)
    .attr("y", function (d) {
      return d.top + BAR / 2;
    })
    .attr("dy", "0.32em")
    .text(function (d) {
      return fitLabel(d.label, chars);
    });
  var bar = all.select("path.chart-bar").attr("fill", function (d) {
    return d.colour;
  });
  // A bar that changes sides starts again from zero on its new side:
  // the two sides' outlines can't morph into each other.
  bar.each(function (d) {
    var side = d.margin >= 0 ? "right" : "left";
    if (this.getAttribute("data-side") === side) return;
    this.setAttribute("data-side", side);
    this.setAttribute("d", roundedBar(x(0), d.top, 0, BAR, side, 0));
  });
  morph(bar, ctx, rows.length).attr("d", shape);
  // The amount sits past the bar's end, or on the far side of zero for
  // a loss, so it never runs into the names.
  morph(all.select("text.chart-value"), ctx, rows.length)
    .attr("x", function (d) {
      return d.margin > 0 ? x(d.margin) + 6 : x(0) + 6;
    })
    .attr("y", function (d) {
      return d.top + BAR / 2;
    })
    .attr("dy", "0.32em")
    .text(function (d) {
      return d.margin > 0 ? "+" + axis.tick(d.margin) : axis.tick(d.margin);
    });
  ctx.link(bar, "agent-type", function (d) {
    return d.key;
  });

  var base = ctx.layer("baseline");
  var baseline = base.select("line.chart-baseline");
  if (baseline.empty()) baseline = base.append("line").attr("class", "chart-baseline");
  baseline.attr("x1", zero).attr("x2", zero).attr("y1", 0).attr("y2", inner.h);

  var hits = ctx.layer("hits").selectAll("rect.chart-hit").data(rows, function (d) {
    return d.key;
  });
  hits.exit().remove();
  var hitsAll = hits.enter().append("rect").attr("class", "chart-hit").merge(hits);
  hitsAll
    .attr("x", -left)
    .attr("width", inner.w + left)
    .attr("y", function (d, i) {
      return i * ROW;
    })
    .attr("height", ROW);
  hover(hitsAll, ctx);
  ctx.link(hitsAll, "agent-type", function (d) {
    return d.key;
  });

  // Which lifetime is cheaper, not a change to make: an agent type may
  // already be on the cheaper one, as the table under the chart says.
  var gainers = rows.filter(function (d) {
    return d.margin > 0;
  }).length;
  var legend = [];
  if (gainers) legend.push({ label: "Cheaper on a 1-hour lifetime", colour: "var(--div-pos)" });
  if (gainers < rows.length) legend.push({ label: "Dearer on a 1-hour lifetime", colour: "var(--div-neg)" });
  return {
    facts: { gainers: gainers ? thousands(gainers) : "None", count: thousands(rows.length) },
    points: rows.map(function (d) {
      var end = x(d.margin);
      return {
        x: ox + end,
        band: { x: ox + Math.min(end, x(0)), y: oy + d.top, w: Math.max(2, Math.abs(end - x(0))), h: BAR },
        tip: d.tip,
        key: d.key,
        open: d.open,
      };
    }),
    table: {
      columns: [
        { key: "label", label: "Agent type", kind: "str" },
        { key: "margin", label: "Saving with 1 hour", kind: "money" },
        { key: "premium", label: "Extra cost of 1-hour writes", kind: "money" },
        { key: "loss", label: "Lost to 5-minute expiry", kind: "money" },
        { key: "verdict", label: "Verdict", kind: "str" },
      ],
      rows: rows,
    },
    legend: legend,
  };
}

// -- 8. startup context: stacked bars per agent type -----------------------------------------------

// What an agent's startup context is made of, in the palette's fixed
// order; the rest folds into Other, and what ClaudeGlass couldn't break
// down is hatched grey.
var STARTUP_PARTS = [
  { key: "system_prompt", label: "System prompt", phrase: "the system prompt" },
  { key: "tool_definitions", label: "Tool definitions", phrase: "tool definitions" },
  { key: "claude_md", label: "CLAUDE.md", phrase: "CLAUDE.md files" },
  { key: "skills_listing", label: "Skills list", phrase: "the skills list" },
  { key: "tool_lists", label: "Tool lists", phrase: "tool lists" },
  { key: "task_prompt", label: "Task prompt", phrase: "the task prompt" },
  { key: "hook_context", label: "Hook output", phrase: "hook output" },
  { key: "other_attachments", label: "Other notes from Claude Code", phrase: "Claude Code's other notes", colour: "other" },
  { key: "not_recorded", label: "Not broken down", phrase: "context ClaudeGlass can't break down", colour: "other", hatch: true },
];
var STARTUP_ROWS = 12;

// data: agent_startup_breakdown (a report table, or {table}).
// opts.open(agentType) leads to that agent's trim actions.
function stackedBars(ctx, data) {
  var opts = ctx.opts;
  var table = tableOf(data);
  var all = tableObjects(table)
    .map(function (row) {
      var base = 0;
      var parts = STARTUP_PARTS.map(function (part) {
        var value = num(row[part.key]);
        var piece = { id: row.agent_type + ":" + part.key, agent: String(row.agent_type), part: part, value: value, x0: base, x1: base + value };
        base += value;
        return piece;
      });
      return { key: String(row.agent_type), label: String(valueLabel(table, row.agent_type)), spawns: num(row.spawns), total: base, parts: parts };
    })
    .filter(function (d) {
      return d.total > 0;
    })
    .sort(function (a, b) {
      return b.total - a.total;
    });
  if (all.length < MIN_POINTS) return { empty: opts.empty || "Fewer than three agent types started in this window." };
  var rows = all.slice(0, STARTUP_ROWS);

  var left = labelWidth(
    rows.map(function (d) {
      return d.label;
    })
  );
  var inner = ctx.size(rows.length * ROW + 52, { left: left, right: 64, top: 8, bottom: 44 });
  var x = d3
    .scaleLinear()
    .domain([
      0,
      d3.max(rows, function (d) {
        return d.total;
      }),
    ])
    .nice(4)
    .range([0, inner.w]);
  valueAxis(sublayer(ctx.layer("axis-x"), "base", 0, inner.h), x, { orient: "bottom", span: inner.h, ticks: 4, format: tokenTick });
  ctx.unit("Tokens at the start of each run", "bottom");
  var ox = ctx.margin.left;
  var oy = ctx.margin.top;
  var chars = Math.floor((left - 16) / 6.8);

  var pieces = [];
  rows.forEach(function (row, i) {
    row.top = i * ROW + (ROW - BAR) / 2;
    var shown = row.parts.filter(function (piece) {
      return piece.value > 0;
    });
    shown.forEach(function (piece, k) {
      piece.top = row.top;
      piece.last = k === shown.length - 1;
      piece.row = row;
      piece.tip = {
        value: compactNumber(piece.value) + " tokens",
        label: piece.part.label + ", " + row.label,
        lines: [percentText(piece.value, row.total) + " of its startup context", "Started " + thousands(row.spawns) + " times"],
      };
      piece.open = opener(opts, "open", row.key);
      pieces.push(piece);
    });
    row.tip = {
      value: compactNumber(row.total) + " tokens at the start",
      label: row.label,
      lines: shown.map(function (piece) {
        return piece.part.label + ": " + compactNumber(piece.value);
      }),
    };
    row.open = opener(opts, "open", row.key);
  });
  // Segments sit 2px apart; only the end of the bar is rounded.
  function shape(piece) {
    var start = x(piece.x0);
    var width = x(piece.x1) - start - (piece.last ? 0 : 2);
    return roundedBar(start, piece.top, Math.max(0, width), BAR, "right", piece.last ? 4 : 0);
  }
  function fill(piece) {
    var colour = entityColour("startup", piece.part.colour || piece.part.key);
    return piece.part.hatch ? ctx.hatch(colour) : colour;
  }
  var marks = ctx.layer("marks");
  var segments = marks.selectAll("path.chart-segment").data(pieces, function (d) {
    return d.id;
  });
  segments.exit().remove();
  var entered = segments
    .enter()
    .append("path")
    .attr("class", "chart-segment")
    .attr("d", function (d) {
      return roundedBar(0, d.top, 0, BAR, "right", 0);
    });
  var segmentsAll = entered
    .merge(segments)
    .attr("fill", fill)
    .classed("is-hatched", function (d) {
      return !!d.part.hatch;
    });
  morph(segmentsAll, ctx, pieces.length).attr("d", shape);
  ctx.link(segmentsAll, "agent-type", function (d) {
    return d.agent;
  });
  // Each part's hit area is its row's full height, so a thin part is
  // still easy to point at.
  var hits = ctx.layer("hits").selectAll("rect.chart-hit").data(pieces, function (d) {
    return d.id;
  });
  hits.exit().remove();
  var hitsAll = hits.enter().append("rect").attr("class", "chart-hit").merge(hits);
  hitsAll
    .attr("x", function (d) {
      return x(d.x0);
    })
    .attr("width", function (d) {
      return Math.max(4, x(d.x1) - x(d.x0));
    })
    .attr("y", function (d) {
      return d.top - (ROW - BAR) / 2;
    })
    .attr("height", ROW);
  hover(hitsAll, ctx);
  ctx.link(hitsAll, "agent-type", function (d) {
    return d.agent;
  });

  var labels = marks.selectAll("text.chart-label").data(rows, function (d) {
    return d.key;
  });
  labels.exit().remove();
  labels
    .enter()
    .append("text")
    .attr("class", "chart-label")
    .attr("text-anchor", "end")
    .attr("dy", "0.32em")
    .merge(labels)
    .attr("x", -8)
    .attr("y", function (d) {
      return d.top + BAR / 2;
    })
    .text(function (d) {
      return fitLabel(d.label, chars);
    });
  var values = marks.selectAll("text.chart-value").data(rows, function (d) {
    return d.key;
  });
  values.exit().remove();
  morph(values.enter().append("text").attr("class", "chart-value").attr("dy", "0.32em").merge(values), ctx, rows.length)
    .attr("x", function (d) {
      return x(d.total) + 6;
    })
    .attr("y", function (d) {
      return d.top + BAR / 2;
    })
    .text(function (d) {
      return compactNumber(d.total);
    });

  var present = STARTUP_PARTS.filter(function (part) {
    return rows.some(function (row) {
      return num(row.parts[STARTUP_PARTS.indexOf(part)].value) > 0;
    });
  });
  var top = rows[0];
  var largest = top.parts.reduce(function (best, piece) {
    return piece.value > best.value ? piece : best;
  }, top.parts[0]);
  return {
    facts: { top: top.label, topTokens: compactNumber(top.total), topPart: largest.part.phrase },
    points: rows.map(function (row) {
      return { x: ox + x(row.total), band: { x: ox, y: oy + row.top, w: Math.max(2, x(row.total)), h: BAR }, tip: row.tip, key: row.key, open: row.open };
    }),
    table: {
      columns: [
        { key: "label", label: "Agent type", kind: "str" },
        { key: "spawns", label: "Runs", kind: "int" },
        { key: "total", label: "Startup context", kind: "tokens" },
      ].concat(
        STARTUP_PARTS.map(function (part, i) {
          return {
            key: part.key,
            label: part.label,
            kind: "tokens",
            value: function (row) {
              return row.parts[i].value;
            },
          };
        })
      ),
      rows: all,
    },
    legend: present.map(function (part) {
      return { label: part.label, colour: entityColour("startup", part.colour || part.key), hatch: !!part.hatch };
    }),
    note: all.length > rows.length ? "Showing the " + STARTUP_ROWS + " agent types with the most startup context. The table lists all " + thousands(all.length) + "." : null,
  };
}

// -- drawing a chart -------------------------------------------------------------------------------

// Each CHART_SPECS form, drawn by its chart type.
var FORMS = {
  "stacked-columns": stackedColumns,
  bars: bars,
  line: line,
  histogram: histogram,
  diverging: diverging,
  "stacked-bars": stackedBars,
  scatter: scatter,
  timeline: buildSessionTimeline,
  "change-steps": changeSteps,
};

// Draw chart `key` (a CHART_SPECS key) from `data` into `container`.
// The only way a page draws a chart; opts as charts.js's drawChart.
export function renderChart(container, key, data, opts) {
  var spec = CHART_SPECS[key];
  if (!spec) throw new Error("No chart " + key + " in CHART_SPECS");
  return drawChart(container, key, data, FORMS[spec.form], opts);
}

// -- a report section's own chart -------------------------------------------------------------------

// The chart a report section draws above its tables: the CHART_SPECS row
// whose source is one of the section's tables, or null. grid.js's
// renderSectionGeneric calls it through setSectionChart (grid.js can't
// import the charts: charts.js draws its Table view with grid.js).
export function sectionChart(section) {
  var tables = section.tables || [];
  var keys = Object.keys(CHART_SPECS);
  for (var i = 0; i < keys.length; i++) {
    var source = CHART_SPECS[keys[i]].source;
    if (typeof source !== "string" || source.indexOf(section.key + ".") !== 0) continue;
    var table = tableNamed(tables, source.slice(section.key.length + 1));
    if (!table) continue;
    var host = el("div", { class: "section-chart" });
    renderChart(host, keys[i], table, { slot: "section", open: markLeads(source) });
    return host;
  }
  return null;
}

function tableNamed(tables, name) {
  for (var i = 0; i < tables.length; i++) if (tables[i].name === name) return tables[i];
  return null;
}

// Where a section chart's mark leads: the action its row is evidence for
// (the first, when there are several), else that row in the table under
// the chart, pulsed.
function markLeads(source) {
  var tableName = source.slice(source.indexOf(".") + 1);
  return function (rowKey) {
    var view = state.view;
    actionIndex().then(function (index) {
      var actions = index.byRow[tableName + "\n" + rowKey] || [];
      if (actions.length) goTo("actions/recommendations", { params: { id: actions[0].key } });
      else if (view) goTo(view, { params: { t: source, row: rowKey } });
    });
  };
}

// -- micro-forms ------------------------------------------------------------------------------------

// A tile's trend: a quiet line with the latest value marked. No axes and
// no tooltip: the tile's own number is the reading.
export function sparkline(values, opts) {
  opts = opts || {};
  var series = (values || []).map(Number).filter(function (value) {
    return isFinite(value);
  });
  if (series.length < 2) return null;
  var width = opts.width || 72;
  var height = opts.height || 20;
  var pad = 3;
  var x = d3
    .scaleLinear()
    .domain([0, series.length - 1])
    .range([pad, width - pad]);
  var span = d3.extent(series);
  if (span[0] === span[1]) span = [span[0] - 1, span[1] + 1];
  var y = d3.scaleLinear().domain(span).range([height - pad, pad]);
  var svg = d3
    .create("svg")
    .attr("class", "sparkline")
    .attr("viewBox", "0 0 " + width + " " + height)
    .attr("width", width)
    .attr("height", height)
    .attr("aria-hidden", "true")
    .attr("focusable", "false");
  svg
    .append("path")
    .attr("class", "sparkline-line")
    .attr(
      "d",
      d3
        .line()
        .x(function (value, i) {
          return x(i);
        })
        .y(function (value) {
          return y(value);
        })(series)
    );
  svg
    .append("circle")
    .attr("class", "sparkline-end")
    .attr("cx", x(series.length - 1))
    .attr("cy", y(series[series.length - 1]))
    .attr("r", 2.5);
  return svg.node();
}

// A level against a target: segments lit up to the level, with the
// level always written beside them, so colour is never the only cue.
// opts: max (the top level, 5), segments (how many, max by default),
// label (what is measured, for a screen reader), text (the level in
// words; "3 of 5" by default), status (good, warn, serious, critical).
export function meter(level, opts) {
  opts = opts || {};
  var max = opts.max || 5;
  var value = Math.max(0, Math.min(max, Number(level) || 0));
  var segments = opts.segments || max;
  var lit = Math.round((value / max) * segments);
  var text = opts.text || thousands(value) + " of " + thousands(max);
  var node = el("div", {
    class: "meter" + (opts.status ? " meter-" + opts.status : ""),
    role: "meter",
    "aria-valuemin": "0",
    "aria-valuemax": String(max),
    "aria-valuenow": String(value),
    "aria-valuetext": text,
    "aria-label": opts.label || null,
  });
  var track = el("span", { class: "meter-track", "aria-hidden": "true" });
  for (var i = 1; i <= segments; i++) track.appendChild(el("span", { class: "meter-seg" + (i <= lit ? " is-on" : "") }));
  node.appendChild(track);
  node.appendChild(el("span", { class: "meter-text", text: text }));
  return node;
}

// The playbook's `weeks` column: 0-100 per week, "-" for a week with
// too few messages, drawn as a small bar chart.
export function habitSparkline(weeks, label) {
  var values = String(weeks || "").split(" ").filter(function (part) {
    return part !== "";
  });
  if (!values.length) return null;
  var width = 8 * values.length;
  var height = 24;
  var parts = ['<svg viewBox="0 0 ' + width + " " + height + '" class="habit-spark" role="img" aria-label="' + escapeHtml(label) + '">'];
  values.forEach(function (value, i) {
    if (value === "-") {
      parts.push('<rect x="' + (i * 8 + 1) + '" y="' + (height - 1) + '" width="6" height="1" fill="var(--axis-line)"></rect>');
      return;
    }
    var h = Math.max(1, Math.round((Number(value) / 100) * (height - 2)));
    parts.push('<rect x="' + (i * 8 + 1) + '" y="' + (height - h) + '" width="6" height="' + h + '" fill="var(--chart-1)"></rect>');
  });
  parts.push("</svg>");
  var wrap = el("span", { class: "habit-spark-wrap" });
  wrap.innerHTML = parts.join("");
  return wrap;
}

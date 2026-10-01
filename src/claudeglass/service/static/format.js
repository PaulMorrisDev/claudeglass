/* claudeglass service UI: format.js
 *
 * Number, money and time formatting: the one place a value becomes
 * text, so the same value reads the same on every page.
 *
 * The rules (docs/ui.md, "One number format"):
 *   money     2 decimals under 10, 1 under 100, none above ("<0.01"
 *             for a positive amount that rounds to nothing)
 *   shares    1 decimal ("12.4%")
 *   tokens    3 significant figures, compacted ("1.24M"); under 1,000
 *             as they are
 *   counts    thousands separators
 *   durations "2h 14m", "14m 5s", "45s"
 *   times     "2026-09-23 10:44 UTC" (shortTs), with "5 min ago"
 *             (relativeTime) where freshness is the point
 * A negative number carries a true minus sign (U+2212), the width of
 * the plus. Sorting never reads the text: grid.js sorts on the raw
 * value.
 */

import { el, state } from "./core.js";

export var MINUS = "−";

export function thousands(n) {
  return Math.round(Number(n)).toLocaleString("en-US");
}

function signed(text, negative) {
  return negative ? MINUS + text : text;
}

// A money amount as a plain number: "1,235", "45.7", "3.46", "<0.01".
export function moneyNumber(value) {
  var n = Number(value);
  if (!isFinite(n)) return "-";
  var abs = Math.abs(n);
  if (abs > 0 && abs < 0.005) return n > 0 ? "<0.01" : "<" + MINUS + "0.01";
  var digits = abs < 10 ? 2 : abs < 100 ? 1 : 0;
  var text = abs.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
  // A value that rounds to zero is plain zero, never "-0.00".
  return signed(text, n < 0 && Number(abs.toFixed(digits)) !== 0);
}

// A count at 3 significant figures: 999 -> "999", 12400 -> "12.4K",
// 1240000 -> "1.24M".
export function compactNumber(value) {
  var n = Number(value);
  if (!isFinite(n)) return "-";
  var abs = Math.abs(n);
  if (abs < 1000) return signed(Math.round(abs).toLocaleString("en-US"), n < 0 && Math.round(abs) !== 0);
  var units = [
    [1e12, "T"],
    [1e9, "B"],
    [1e6, "M"],
    [1e3, "K"],
  ];
  for (var i = 0; i < units.length; i++) {
    if (abs >= units[i][0] * 0.9995) {
      var scaled = abs / units[i][0];
      // 3 significant figures: 1.24, 12.4, 124.
      var digits = scaled < 10 ? 2 : scaled < 100 ? 1 : 0;
      var text = Number(scaled.toFixed(digits)).toString();
      // 999.95K rounds to 1000K: step up to the next unit instead.
      if (Number(text) >= 1000 && i > 0) {
        text = Number((abs / units[i - 1][0]).toFixed(2)).toString();
        return signed(text + units[i - 1][1], n < 0);
      }
      return signed(text + units[i][1], n < 0);
    }
  }
  return String(n);
}

// "2h 14m", "14m 5s", "45s"; under a second, "<1s".
export function formatDuration(value) {
  var n = Number(value);
  if (!isFinite(n)) return "-";
  var negative = n < 0;
  var total = Math.abs(n);
  if (total > 0 && total < 1) return signed("<1s", negative);
  total = Math.round(total);
  var hours = Math.floor(total / 3600);
  var minutes = Math.floor((total % 3600) / 60);
  var seconds = total % 60;
  var text = hours ? hours + "h " + minutes + "m" : minutes ? minutes + "m " + seconds + "s" : seconds + "s";
  return signed(text, negative && total !== 0);
}

var COLUMN_KINDS = ["str", "int", "float", "pct", "money", "tokens", "secs"];

export var NUMERIC_KINDS = { int: true, float: true, pct: true, money: true, tokens: true, secs: true };

// One cell of a report table (model.Column's kind switch, the same kinds
// render/tables.py::format_cell knows). "money" is a plain number here:
// a grid says its unit once, in the column header (moneyUnit()), so the
// column stays readable and sortable. Prose that quotes an amount
// phrases it with moneyText() instead. currency is kept for callers'
// sake; the unit comes from state.
export function formatCell(value, kind, currency) {
  if (value === null || value === undefined) return "-";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (COLUMN_KINDS.indexOf(kind) === -1) kind = "str";
  switch (kind) {
    case "str":
      // A mixed "metric / value" table: numbers read with separators.
      if (typeof value === "number" && isFinite(value)) {
        return signed(Math.abs(value).toLocaleString("en-US", { maximumFractionDigits: 2 }), value < 0);
      }
      return modelNames(String(value));
    case "int":
      return signed(thousands(Math.abs(Number(value))), Number(value) < 0 && Math.round(Number(value)) !== 0);
    case "tokens":
      return compactNumber(value);
    case "float":
      return signed(
        Math.abs(Number(value)).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }),
        Number(value) < 0 && Number(Math.abs(Number(value)).toFixed(2)) !== 0
      );
    case "pct":
      return signed(Math.abs(Number(value)).toFixed(1), Number(value) < 0 && Number(Math.abs(Number(value)).toFixed(1)) !== 0) + "%";
    case "money":
      return moneyNumber(value);
    case "secs":
      return formatDuration(value);
    default:
      return String(value);
  }
}

// A float that is a whole number reads as one: "24", not "24.00". For a
// single figure (a tile, a fact); a grid decides per column, so its
// digits stay lined up (grid.js).
export function wholeKind(value, kind) {
  return kind === "float" && typeof value === "number" && Number.isInteger(value) ? "int" : kind;
}

// The full value behind a compacted cell, for its tooltip ("1,243,112
// tokens"), or "" when the cell already shows it all.
export function fullValue(value, kind) {
  if (typeof value !== "number" || !isFinite(value)) return "";
  if (kind === "tokens" && Math.abs(value) >= 1000) return thousands(value) + " tokens";
  return "";
}

// An amount in the pricing currency: "$12.34", or "12.34 EUR" for a
// currency with no symbol here.
export function currencyAmount(usd) {
  var currency = state.currency || "USD";
  var number = moneyNumber(usd);
  if (currency === "USD") {
    return number.charAt(0) === MINUS ? MINUS + "$" + number.slice(1) : number.charAt(0) === "<" ? "<$" + number.slice(1) : "$" + number;
  }
  return number + " " + currency;
}

// The unit a money column's header carries: "$" on the API, "list-price
// $" on a Pro or Max plan (the amounts are what the tokens would cost at
// list price, not a bill).
export function moneyUnit() {
  var currency = state.currency || "USD";
  var symbol = currency === "USD" ? "$" : currency;
  var units = state.units || {};
  return units.mode === "subscription" ? "list-price " + symbol : symbol;
}

// Text from the service writes an amount the CLI's way ("1,962.05 USD",
// "Total cost (USD)"). The dashboard writes every amount as "$1,962.05"
// and a unit as "($)", so fetchJson brings the service's text in line as
// it arrives. Another pricing currency already reads the same both ways
// ("12.34 EUR"), so only USD changes. Numbers and keys are untouched.
var SERVICE_USD = /([−\-]?)(\d[\d,]*(?:\.\d+)?) USD\b/g;
var SERVICE_USD_UNIT = /\(USD\)/g;

export function readableAmounts(value) {
  if (typeof value === "string") {
    if (value.indexOf("USD") === -1) return value;
    return value
      .replace(SERVICE_USD, function (match, sign, number) {
        return (sign ? MINUS : "") + "$" + number;
      })
      .replace(SERVICE_USD_UNIT, "($)");
  }
  if (Array.isArray(value)) {
    for (var i = 0; i < value.length; i++) value[i] = readableAmounts(value[i]);
    return value;
  }
  if (value && typeof value === "object") {
    Object.keys(value).forEach(function (key) {
      // A key that names a row ("Total cost (USD)" in a table's
      // value_labels or row_kinds) is rewritten like the row's own cell,
      // so the two still match.
      var readable = readableAmounts(key);
      var item = readableAmounts(value[key]);
      if (readable !== key) delete value[key];
      value[readable] = item;
    });
  }
  return value;
}

// A share of the weekly limit at or above this many percent reads as
// weeks of it ("about 2.5 weeks' worth of your usage limit"): 248.7% of a
// limit is hard to picture. Mirrors units.WEEKS_FROM_SHARE.
var WEEKS_FROM_SHARE = 200;

// A share of the weekly limit: two decimals below 1%, so a small saving
// doesn't read as "0.0%"; one above.
function shareText(share) {
  return share < 1 ? share.toFixed(2) + "%" : formatCell(share, "pct");
}

function weeksText(share) {
  return (share / 100).toFixed(1);
}

// Mirrors units.Units.money (src/claudeglass/units.py): usd (a
// list-price amount over opts.period, e.g. "a week") phrased for the
// billing mode from state.units (report.meta.units -- {mode,
// share_per_usd, period_label, basis}, set when a report loads).
// Returns null for a non-positive or non-finite amount, same contract
// as the Python original. share_per_usd is already the window-%-per-
// USD slope (elasticity.express_in_window(1.0, ...)), so a share for
// an arbitrary usd is just usd * share_per_usd -- linear, no curve
// fit needed client-side.
export function money(usd, opts) {
  opts = opts || {};
  var period = opts.period || "";
  if (typeof usd !== "number" || !isFinite(usd) || usd <= 0) return null;
  var suffix = period ? " " + period : "";
  var dollars = currencyAmount(usd);
  var unitsInfo = state.units || {};
  if (unitsInfo.mode !== "subscription") {
    return { primary: dollars + suffix, secondary: "", basis: "at list price" };
  }
  var sharePerUsd = unitsInfo.share_per_usd;
  if (sharePerUsd === null || sharePerUsd === undefined) {
    return { primary: dollars + " list-price equivalent" + suffix, secondary: "", basis: unitsInfo.basis || "" };
  }
  var share = usd * sharePerUsd;
  var primary =
    share >= WEEKS_FROM_SHARE
      ? "about " + weeksText(share) + " weeks' worth of your usage limit" + suffix
      : "about " + shareText(share) + " of your " + (unitsInfo.period_label || "weekly usage limit") + suffix;
  return {
    primary: primary,
    secondary: dollars + " list-price equivalent",
    basis: unitsInfo.basis || "",
  };
}

// Mirrors units.Units.money_text/Amount.phrase: a one-line amount
// that is never empty. opts.prefix (e.g. "about ") is joined without
// doubling "about" when money()'s own primary text already opens with
// it (a subscription's "about X% of your weekly usage limit" -- finding
// F3's "about about" bug, mirrored client-side).
export function moneyText(usd, opts) {
  opts = opts || {};
  var prefix = opts.prefix || "";
  var amount = money(usd, opts);
  var text = amount ? (amount.secondary ? amount.primary + " (" + amount.secondary + ")" : amount.primary) : null;
  if (text === null) {
    var value = typeof usd === "number" && isFinite(usd) ? usd : 0;
    return currencyAmount(value);
  }
  if (!prefix) return text;
  var strippedPrefix = prefix.replace(/\.$/, "").trim().toLowerCase();
  if (strippedPrefix === "about" && text.toLowerCase().indexOf("about ") === 0) return text;
  var joiner = /[ \-\u2011]$/.test(prefix) ? "" : " ";
  return prefix + joiner + text;
}

// An amount as a node: the billing-mode phrase, with the list-price
// equivalent (on a plan) in the quieter ink after it.
export function moneyNode(usd, opts) {
  var amount = money(usd, opts);
  if (!amount) return el("span", { class: "amount", text: currencyAmount(typeof usd === "number" && isFinite(usd) ? usd : 0) });
  var node = el("span", { class: "amount" }, [el("span", { text: amount.primary })]);
  if (amount.secondary) node.appendChild(el("span", { class: "unit", text: " (" + amount.secondary + ")" }));
  return node;
}

// An amount split for a metric tile: the number, its unit in the
// quieter ink on the line below, and (on a plan) the list-price
// equivalent as a hint. API: "$12.34". Pro or Max: "3.2%" "of your weekly
// usage limit", or from WEEKS_FROM_SHARE up "2.5" "weeks of your usage
// limit" (one line in a tile), with "$12.34 list-price equivalent"
// beneath. opts.like (another amount in USD) picks the form that amount
// takes, so a tile counting up to it writes every step in its final unit.
export function moneyParts(usd, opts) {
  opts = opts || {};
  var value = typeof usd === "number" && isFinite(usd) ? usd : 0;
  var unitsInfo = state.units || {};
  if (unitsInfo.mode !== "subscription") return { value: currencyAmount(value), unit: "", secondary: "" };
  var sharePerUsd = unitsInfo.share_per_usd;
  if (sharePerUsd === null || sharePerUsd === undefined) {
    return { value: currencyAmount(value), unit: "list-price", secondary: "" };
  }
  var share = value * sharePerUsd;
  var like = typeof opts.like === "number" && isFinite(opts.like) ? opts.like * sharePerUsd : share;
  var secondary = currencyAmount(value) + " list-price equivalent" + (opts.period ? " " + opts.period : "");
  if (like >= WEEKS_FROM_SHARE) return { value: weeksText(share), unit: "weeks of your usage limit", secondary: secondary };
  return { value: shareText(share), unit: "of your " + (unitsInfo.period_label || "weekly usage limit"), secondary: secondary };
}

// Mirrors units.Units.money_cell: an amount short enough for a table cell
// or a chart's reading, "0.07% ($0.06)" on a plan with a known share of
// the weekly limit, else the plain figure. Always a share, never weeks.
export function moneyCell(usd) {
  if (typeof usd !== "number" || !isFinite(usd) || usd <= 0) return currencyAmount(0);
  var unitsInfo = state.units || {};
  var sharePerUsd = unitsInfo.mode === "subscription" ? unitsInfo.share_per_usd : null;
  if (sharePerUsd === null || sharePerUsd === undefined) return currencyAmount(usd);
  return shareText(usd * sharePerUsd) + " (" + currencyAmount(usd) + ")";
}

// A value and its unit as a node: the unit in the quieter ink, one step
// smaller ("12.4" "% of weekly limit").
export function withUnit(value, unit) {
  var node = el("span", { class: "with-unit" }, [el("span", { text: value })]);
  if (unit) node.appendChild(el("span", { class: "unit", text: " " + unit }));
  return node;
}

// A short amount for a chart's ticks and labels: "$0", "$2.5", "$45",
// "$1.2K". Another currency writes the number alone; the axis names
// the unit.
function axisNumber(value) {
  var abs = Math.abs(value);
  var text = abs >= 1000 ? compactNumber(abs) : abs >= 10 ? String(Math.round(abs)) : String(Number(abs.toFixed(abs >= 1 ? 1 : 2)));
  return signed(text, value < 0 && text !== "0");
}

// Mirrors units.Units.money for a chart axis: the unit the axis is
// labelled in, and each value written in it. On a plan with a known
// share of the weekly limit, the axis reads in that share; on a plan
// without one, in list-price dollars; on the API, in dollars. Values
// stay in USD for the scale: tick(usd) writes one in the axis's unit.
export function moneyAxis() {
  var currency = state.currency || "USD";
  var symbol = currency === "USD" ? "$" : currency;
  var unitsInfo = state.units || {};
  var share = unitsInfo.mode === "subscription" ? unitsInfo.share_per_usd : null;
  if (share !== null && share !== undefined) {
    return {
      unit: "% of your " + (unitsInfo.period_label || "weekly usage limit"),
      // What one USD is in the axis's unit, so a chart can step its ticks
      // in that unit (charts.js's moneyTicks).
      factor: share,
      tick: function (usd) {
        var value = usd * share;
        var abs = Math.abs(value);
        var text = abs >= 10 ? String(Math.round(abs)) : String(Number(abs.toFixed(abs >= 1 ? 1 : 2)));
        return signed(text, value < 0 && text !== "0") + "%";
      },
    };
  }
  return {
    unit: unitsInfo.mode === "subscription" ? "list-price " + symbol : symbol,
    factor: 1,
    tick: function (usd) {
      var text = axisNumber(usd);
      if (currency !== "USD") return text;
      return text.charAt(0) === MINUS ? MINUS + "$" + text.slice(1) : "$" + text;
    },
  };
}

export function cellSortValue(value) {
  if (value === null || value === undefined) return "";
  return String(value);
}

// A price ratio in words, to put before "the input price": 0.1 is "a
// tenth of", 0.2 "a fifth of", 1.25 "1.25 times", 2 "twice". The ratios
// come from the report's meta.rates (pricing.toml), never from here.
var FRACTION_WORDS = [
  [0.05, "a twentieth of"],
  [0.1, "a tenth of"],
  [0.125, "an eighth of"],
  [0.2, "a fifth of"],
  [0.25, "a quarter of"],
  [1 / 3, "a third of"],
  [0.5, "half"],
  [1, "the same as"],
  [2, "twice"],
];

export function fraction(ratio) {
  var n = Number(ratio);
  if (!isFinite(n) || n <= 0) return "";
  for (var i = 0; i < FRACTION_WORDS.length; i++) {
    if (Math.abs(n - FRACTION_WORDS[i][0]) < 0.005) return FRACTION_WORDS[i][1];
  }
  if (n < 1) return Math.round(n * 100) + "% of";
  return String(Math.round(n * 100) / 100) + " times";
}

// A model id as people say it: "claude-opus-5-5" -> "Opus 5.5",
// "claude-3-5-haiku-20241022" -> "Haiku 3.5". An id of another shape
// comes back as it is.
export function modelName(id) {
  var text = String(id || "").replace(/^claude-/, "").replace(/-\d{8}$/, "");
  var family = (text.match(/[a-z]+/) || [""])[0];
  var numbers = text.match(/\d+/g) || [];
  if (!family) return String(id || "");
  return family.charAt(0).toUpperCase() + family.slice(1) + (numbers.length ? " " + numbers.join(".") : "");
}

// Text with model ids in it, each as people say it: "claude-sonnet-5
// (+3 more)" -> "Sonnet 5 (+3 more)". For display only: the ids stay
// the data (row keys, evidence, sorting). Only Claude model ids match,
// so a name like "claude-implementer" is left alone.
var MODEL_ID = /\bclaude-(?:(?:opus|sonnet|haiku|fable)(?:-\d+)+|\d+(?:-\d+)*-(?:opus|sonnet|haiku|fable)(?:-\d{8})?)\b(\[1m\])?/g;

export function modelNames(text) {
  return String(text).replace(MODEL_ID, function (id, wide) {
    return wide ? modelName(id.slice(0, -wide.length)) + " (1M context)" : modelName(id);
  });
}

// A change as a signed percentage: "+12%", "−3%" with a true minus
// sign (U+2212, the width of the plus), "0%". Blank when there is none.
export function signedPercent(value) {
  if (value === null || value === undefined || value === "") return "";
  var n = Number(value);
  if (n > 0) return "+" + value + "%";
  if (n < 0) return "−" + String(value).replace(/^-/, "") + "%";
  return value + "%";
}

// "2026-09-23T10:44:22.705Z" -> "2026-09-23 10:44 UTC": the one absolute
// time format, for cells, tooltips and the status line.
export function shortTs(ts) {
  var text = String(ts || "");
  if (!/^\d{4}-\d\d-\d\dT\d\d:\d\d/.test(text)) return text || "-";
  return text.slice(0, 16).replace("T", " ") + (/Z$/.test(text) ? " UTC" : "");
}

// "just now", "5 min ago", "3 h ago", "2 days ago" -- for freshness,
// with the absolute time beside it on hover (timeNode). now is for tests.
export function relativeTime(ts, now) {
  var then = Date.parse(ts);
  if (!isFinite(then)) return shortTs(ts);
  var seconds = Math.round(((now === undefined ? Date.now() : now) - then) / 1000);
  if (seconds < 45) return "just now";
  var minutes = Math.round(seconds / 60);
  if (minutes < 60) return minutes + " min ago";
  var hours = Math.round(minutes / 60);
  if (hours < 36) return hours + " h ago";
  var days = Math.round(hours / 24);
  return days + (days === 1 ? " day ago" : " days ago");
}

// A <time> that reads relative and shows the absolute time on hover.
export function timeNode(ts) {
  return el("time", { dateTime: String(ts || ""), title: shortTs(ts), text: relativeTime(ts) });
}

// -- the window in words -------------------------------------------------

// The window as the end of a sentence: "No sessions <when>.", "No changes
// <when>."
export function windowWhen(value) {
  if (/^[0-9]+$/.test(value)) return value === "1" ? "in the last day" : "in the last " + value + " days";
  return { "1h": "in the last hour", today: "today", "24h": "in the last 24 hours", change: "since your last change" }[value] || "yet";
}

// -- entity names --------------------------------------------------------

// Claude Code names a project's folder after its path with every
// separator turned into "-" ("C:\Dev\claudeglass" becomes
// "C--Dev-claudeglass"), so a hyphen in a folder name and a path
// separator look the same. The readable name drops the drive, a Windows
// home folder, and the one parent folder the known projects share
// ("Dev"), and names a worktree after its project. The full slug stays
// in the tooltip. Known projects come from report.meta.projects.
var knownProjects = [];

export function setKnownProjects(slugs) {
  knownProjects = Array.isArray(slugs) ? slugs.slice() : [];
}

var WORKTREE = "--claude-worktrees-";

function projectBase(slug) {
  var text = String(slug);
  var cut = text.indexOf(WORKTREE);
  if (cut !== -1) text = text.slice(0, cut);
  return text.replace(/^[A-Za-z]--/, "").replace(/^Users-[^-]+-/, "");
}

function sharedParent() {
  var counts = {};
  knownProjects.forEach(function (slug) {
    var base = projectBase(slug);
    var dash = base.indexOf("-");
    if (dash > 0 && dash < base.length - 1) {
      var parent = base.slice(0, dash + 1);
      counts[parent] = (counts[parent] || 0) + 1;
    }
  });
  var best = "";
  Object.keys(counts).forEach(function (parent) {
    if (counts[parent] >= 2 && (!best || counts[parent] > counts[best])) best = parent;
  });
  return best;
}

export function projectName(slug) {
  if (slug === null || slug === undefined || slug === "") return "-";
  var text = String(slug);
  if (text === "all") return "All projects";
  var cut = text.indexOf(WORKTREE);
  var worktree = cut === -1 ? "" : text.slice(cut + WORKTREE.length);
  var base = projectBase(text);
  var parent = sharedParent();
  if (parent && base.indexOf(parent) === 0 && base.length > parent.length) base = base.slice(parent.length);
  return worktree ? base + " / " + worktree : base || text;
}

// Column keys whose values are project slugs.
export var PROJECT_KEYS = { project: true, slug: true, project_slug: true };

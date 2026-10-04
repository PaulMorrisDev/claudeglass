/* claudeglass service UI: grid.js
 *
 * The data grid (docs/ui.md, "Data grid") and the report sections each
 * view draws from report.json. One grid for every table on every page:
 * sticky header, sort that is kept per table (tls:sort:<table>), a
 * column chooser for wide tables (tls:cols:<table>), numbers right-
 * aligned in even-width digits, an inline bar on the lead measure, an
 * optional tint by value, readable project names, and only the visible
 * rows drawn once a table passes 200 rows. A list the report sends A to
 * Z opens as a ranking, biggest first on its lead measure. Headers wrap
 * to two lines, and a grid wider than its box shows a shadow at the
 * edge it scrolls towards. A row can link to the same thing's mark in
 * the chart above it (core.js's highlight). A table that is evidence
 * for a recommendation says so: "Feeds N actions" in its header, and a
 * mark on each row the recommendation cites, which a fold never hides.
 */

import { clear, cli, el, highlight, listenHighlight, state, storageGet, storageSet } from "./core.js";
import { cellSortValue, formatCell, fullValue, modelNames, moneyParts, moneyText, moneyUnit, NUMERIC_KINDS, PROJECT_KEYS, projectName, wholeKind } from "./format.js";
import { actionIndex, fetchJson, findSection, postJson } from "./api.js";
import { COST_CARDS, pageLink, plainText, viewFor, viewForSection, viewForTable } from "./links.js";
import { button, emptyState, helpButton, motionOK, popoverButton, prose, swatch, tile, tileRow } from "./ui.js";
import { icon } from "./icons.js";

// Mirrors render/tables.py::resolve_evidence_column_kind /
// format_evidence_value: a Recommendation.evidence tuple carries no
// column reference of its own, so the cited (source_table, row_key,
// value) is looked up in the already-fetched report model to find
// which column actually holds it, and formatted with that column's
// kind -- the same number the report's own tables show, not a raw
// float. An amount reads in the billing mode (it's prose, not a grid).
function cellMatches(cell, value) {
  if (typeof cell === "boolean" || typeof value === "boolean") return cell === value;
  var cellNum = Number(cell);
  var valueNum = Number(value);
  if (!isNaN(cellNum) && !isNaN(valueNum) && cell !== null && cell !== "" && value !== null && value !== "") {
    return Math.abs(cellNum - valueNum) < 1e-9;
  }
  return cell === value || String(cell) === String(value);
}

function resolveEvidenceColumnKind(report, sourceTable, rowKey, value) {
  if (!report || typeof sourceTable !== "string") return "str";
  var dot = sourceTable.indexOf(".");
  if (dot === -1) return "str";
  var sectionKey = sourceTable.slice(0, dot);
  var tableName = sourceTable.slice(dot + 1);
  var section = findSection(report, sectionKey);
  if (!section) return "str";
  for (var t = 0; t < section.tables.length; t++) {
    var table = section.tables[t];
    if (table.name !== tableName) continue;
    for (var r = 0; r < table.rows.length; r++) {
      var row = table.rows[r];
      if (!row.length || !cellMatches(row[0], rowKey)) continue;
      for (var c = 0; c < row.length && c < table.columns.length; c++) {
        if (cellMatches(row[c], value)) return table.columns[c].kind;
      }
    }
  }
  return "str";
}

export function formatEvidenceValue(report, value, sourceTable, rowKey, currency) {
  var kind = wholeKind(value, resolveEvidenceColumnKind(report, sourceTable, rowKey, value));
  if (kind === "money" && typeof value === "number") return moneyText(value);
  return formatCell(value, kind, currency);
}

// -- the data grid ------------------------------------------------------

// Above this many rows only the rows in view are drawn.
var VIRTUAL_ROWS = 200;
// Above this many rows the grid scrolls in its own box, header pinned.
var TALL_ROWS = 20;
// A wide table shows this many columns until you choose more.
var LEAD_COLUMNS = 7;
// A report table longer than this shows its first REPORT_ROWS rows (in
// the order it is sorted) and a button for the rest.
var REPORT_ROWS = 10;

// Report tables listed oldest first (usage.py): capped, they show their
// latest rows.
export var NEWEST_LAST = { by_day: true, by_week: true, by_month: true, five_hour_blocks: true };

// The width a header's label keeps, in ch: all of a short label, which
// stays on one line; else the longest line of its most even split into
// two or three lines at spaces, so a long header grows down, not across,
// and a table of them fits the desktop width. A line of words sets
// narrower than its count of ch (at most 94% of it across the report's
// headers), and a label is never narrower than its longest word. words:
// the label's words, a short unit joined to the last (it never wraps
// alone), hyphenated words whole.
var LABEL_ONE_LINE = 12;

function labelWidth(words) {
  var length = words.join(" ").length;
  if (length <= LABEL_ONE_LINE) return length;
  function span(from, to) {
    return words.slice(from, to).join(" ").length;
  }
  var best = length;
  for (var i = 1; i < words.length; i++) {
    best = Math.min(best, Math.max(span(0, i), span(i, words.length)));
    for (var j = i + 1; j < words.length; j++) {
      best = Math.min(best, Math.max(span(0, i), span(i, j), span(j, words.length)));
    }
  }
  return best;
}

function hasKeys(object) {
  return !!object && Object.keys(object).length > 0;
}

function readJson(key) {
  var raw = storageGet(key);
  if (!raw) return null;
  try {
    return JSON.parse(raw);
  } catch (err) {
    return null;
  }
}

// The value a column holds for a row: row[index] for a report table's
// arrays, row[key] for an API list's objects, or the column's own
// value(row).
function columnValue(column, row) {
  if (column.value) return column.value(row);
  return Array.isArray(row) ? row[column.index] : row[column.key];
}

function isNumeric(column) {
  return !!NUMERIC_KINDS[column.kind];
}

// The inline bar goes on one column: the spec's, else the first money
// column, else the first tokens column.
function barColumn(columns, spec) {
  if (spec.bar !== undefined) return spec.bar;
  var money = -1;
  var tokens = -1;
  columns.forEach(function (column, i) {
    if (column.kind === "money" && money === -1) money = i;
    if (column.kind === "tokens" && tokens === -1) tokens = i;
  });
  return money !== -1 ? money : tokens;
}

function columnMaxima(columns, rows) {
  return columns.map(function (column) {
    var max = 0;
    rows.forEach(function (row) {
      var value = columnValue(column, row);
      if (typeof value === "number" && isFinite(value) && value > max) max = value;
    });
    return max;
  });
}

function barNode(value, max) {
  // Built as a markup string for the same HTML5-foreign-content reason
  // icons.js uses, so no namespace literal is needed. value and max are
  // numbers from the report JSON, never interpolated as text.
  var width = ((Math.max(0, Math.min(1, value / max)) * 60) || 0).toFixed(1);
  return el("span", {
    class: "bar-track",
    html: '<svg viewBox="0 0 60 8" class="bar-svg" aria-hidden="true" preserveAspectRatio="none"><rect x="0" y="1" width="' + width + '" height="6" rx="1.5"></rect></svg>',
  });
}

// The display of one cell, as text or a node.
function cellContent(column, row, value, spec, rowKind) {
  if (column.render) return column.render(row, value);
  var kind = column.kind;
  // Table.row_kinds: how to format this row's "str" cells. A money row
  // says its unit itself: the column mixes kinds, so its header can't.
  if (rowKind && column.index > 0 && kind === "str" && typeof value === "number") kind = rowKind;
  if (rowKind === "money" && kind === "money" && column.index > 0 && typeof value === "number") return moneyText(value);
  if (PROJECT_KEYS[column.key] && typeof value === "string") {
    return el("span", { class: "entity-name", title: value, text: projectName(value) });
  }
  var labels = spec.valueLabels;
  if (labels && typeof value === "string" && Object.prototype.hasOwnProperty.call(labels, value)) {
    return el("span", { class: "value-label", title: value, "data-raw": value, text: labels[value] });
  }
  // A cell can't hold a link (a row opens its own detail): a page link
  // in its text reads as the page's name. A model reads by its name,
  // with its id on hover.
  var text = formatCell(value, kind, state.currency);
  if (typeof text !== "string") return text;
  if (typeof value === "string" && text !== value && kind === "str") return el("span", { title: value, text: plainText(text) });
  return plainText(text);
}

// A text column holding sentences wraps as prose; any other text (a
// name, a model, a key) stays on one line, so "Haiku 4.5 (+1 more)"
// never breaks across lines. A column drawn by its own render is left alone.
var PROSE_CHARS = 40;

// labels: the table's value labels. A value shown by its label ("Context
// size that 9 in 10 main session replies stay under") is judged by the
// label, not by its short key.
function proseColumns(columns, rows, labels) {
  var wraps = {};
  columns.forEach(function (column) {
    if (column.render || NUMERIC_KINDS[column.kind]) return;
    wraps[column.index] = rows.some(function (row) {
      var value = columnValue(column, row);
      if (typeof value !== "string") return false;
      var shown = labels && Object.prototype.hasOwnProperty.call(labels, value) ? String(labels[value]) : value;
      return shown.length > PROSE_CHARS;
    });
  });
  return wraps;
}

// Report tables read as a heat grid: agent by quality signal, where the
// darker cell is the signal to look at first.
var TINT_TABLES = ["quality_by_agent"];

// A grid for spec (see docs/ui.md for the whole contract):
//   id        stable across draws: the saved sort and columns hang on it
//   columns   [{key, label, kind, help, render(row, value), value(row),
//              sortValue(row), nowrap}]; a report table's columns get
//              their index from their position
//   rows      arrays (a report table) or objects (an API list)
//   lead      column keys shown until more are chosen (else the first 7
//             of a table wider than 8)
//   bar       the index of the column with the inline bar (-1: none)
//   tint      true: percentage cells shade by value, each column against
//             its own largest (the Quality grid)
//   rowAction {label(row), run(row, tr)}: each row opens something
//   rowKey    row -> the key evidence links and pulses use
//   rowClass  row -> a class for its <tr> (e.g. "row-unchanged"), or null
//   link      {scope, key(row)}: hovering or focusing a row lights the
//             chart mark with the same key in that scope, and back
//   swatch    row -> the colour the row's entity has on the chart, shown
//             as a swatch before its first cell, or null
//   sortable  false for a form laid out as a table
//   rank      true: until the reader picks a sort, rows run biggest
//             first on the bar column (the lead measure)
//   totalLast the key of a row (a total, or "other") that stays last
//             under any sort and takes no bar
//   limit     show the first limit rows and a "Show all N rows" button,
//             when there are more than limit + 2 (else all of them); a
//             row an action cites (markRows) is always in the first ones
//   empty     what to say when there are no rows
//   caption   the table's name, read aloud
//   valueLabels, rowGroups, rowKinds: the report Table's own fields
export function dataGrid(spec) {
  var columns = spec.columns.map(function (column, i) {
    return Object.assign({ index: i, kind: column.kind || "str" }, column);
  });
  var rows = spec.rows || [];
  // A float column of whole numbers ("Typical prompts from you") reads
  // as counts, "3" rather than "3.00"; one fraction keeps the decimals
  // for every row, so the digits line up.
  columns.forEach(function (column) {
    if (column.kind !== "float" || column.render || !rows.length) return;
    var whole = rows.every(function (row) {
      var value = columnValue(column, row);
      return value === null || value === undefined || (typeof value === "number" && Number.isInteger(value));
    });
    if (whole) column.kind = "int";
  });
  var gridId = spec.id;
  var sortable = spec.sortable !== false && rows.length > 1;
  var wrap = el("div", { class: "grid" + (spec.class ? " " + spec.class : ""), "data-grid": gridId });
  // Rows that are evidence for an action: rowMark(key) -> a node for the
  // row's first cell, or null. Set later through wrap.grid.markRows,
  // once the recommendations have loaded.
  var rowMark = spec.rowMark || null;

  if (!rows.length) {
    wrap.appendChild(emptyState(spec.empty || "Nothing to show for this window.", null, spec.emptyNext));
    return wrap;
  }

  // -- which columns show ------------------------------------------------
  var chooserOn = columns.length > LEAD_COLUMNS + 1;
  var defaultKeys = columns
    .filter(function (column, i) {
      if (!chooserOn) return true;
      if (spec.lead) return i === 0 || spec.lead.indexOf(column.key) !== -1;
      return i < LEAD_COLUMNS;
    })
    .map(function (column) {
      return column.key;
    });
  var savedKeys = chooserOn ? readJson("tls:cols:" + gridId) : null;
  var shownKeys = Array.isArray(savedKeys) && savedKeys.length ? savedKeys : defaultKeys;

  // The key evidence links and pulses use.
  function keyOf(row) {
    return spec.rowKey ? spec.rowKey(row) : Array.isArray(row) ? row[0] : null;
  }

  // The row that stays last (spec.totalLast): out of the ranking, and
  // with no bar, so the other rows' bars compare with each other.
  function isTotal(row) {
    return spec.totalLast !== undefined && spec.totalLast !== null && String(keyOf(row)) === String(spec.totalLast);
  }

  // -- sort: the saved one, read back ------------------------------------
  var sort = sortable ? readJson("tls:sort:" + gridId) : null;
  if (sort && !columns.some(function (c) { return c.key === sort.key; })) sort = null;

  var maxima = columnMaxima(
    columns,
    rows.filter(function (row) {
      return !isTotal(row);
    })
  );
  var proseCols = proseColumns(columns, rows, spec.valueLabels);
  var bar = barColumn(columns, spec);
  if (rows.length < 2) bar = -1;
  // A ranking opens biggest first on its lead measure, the column with
  // the inline bar. That order isn't stored: only the reader's own is.
  if (!sort && sortable && spec.rank && bar !== -1) sort = { key: columns[bar].key, ascending: false };

  // A control each grid on a page has (the column chooser, the fold,
  // a column's help) is named by its visible words, then its table's.
  function inTable(words) {
    return spec.caption ? words + ": " + spec.caption : words;
  }

  var toolbar = null;
  var chooserButton = null;
  if (chooserOn) {
    toolbar = el("div", { class: "grid-toolbar" });
    chooserButton = button("", { variant: "quiet", icon: "table", class: "grid-columns" });
    toolbar.appendChild(
      popoverButton(chooserButton, buildChooser, { class: "grid-chooser", label: inTable("Columns to show"), align: "end" })
    );
    wrap.appendChild(toolbar);
  }

  var scroller = el("div", { class: "grid-scroll", tabIndex: -1 });
  var table = el("table", { id: gridId, class: "data-grid" + (spec.tint ? " grid-tint" : "") });
  if (spec.caption) table.appendChild(el("caption", { class: "visually-hidden", text: spec.caption }));
  var thead = el("thead");
  var tbody = el("tbody");
  table.appendChild(thead);
  table.appendChild(tbody);
  scroller.appendChild(table);
  // The frame holds the edge shadows (app.css), which stay put while the
  // grid scrolls under them.
  var frame = el("div", { class: "grid-frame" }, [scroller]);
  wrap.appendChild(frame);

  var virtual = rows.length > VIRTUAL_ROWS;
  if (virtual) scroller.classList.add("grid-virtual");
  var orderedRows = rows;
  var rowHeight = 33;
  // A long table opens on its top rows; the rest are one click away.
  var limited = !virtual && spec.limit > 0 && rows.length > spec.limit + 2;
  var expanded = false;
  var moreButton = null;
  // The keys of the rows an action cites (markRows), or null until the
  // recommendations load.
  var cited = null;

  // How many rows the folded table shows: its limit, or as many as it
  // takes to include every row an action cites, counted from the end the
  // fold keeps. A fold that would leave out 2 rows or fewer shows all.
  function foldSize() {
    var size = spec.limit;
    if (cited) {
      orderedRows.forEach(function (row, i) {
        if (cited[String(keyOf(row))]) size = Math.max(size, fromEnd() ? orderedRows.length - i : i + 1);
      });
    }
    return size;
  }

  function folded() {
    return limited && !expanded && rows.length > foldSize() + 2;
  }

  // A grid drawing more than TALL_ROWS rows scrolls in its own box, so
  // a capped table only does once all its rows show.
  function fitBox() {
    var tall = (folded() ? foldSize() : rows.length) > TALL_ROWS;
    scroller.classList.toggle("grid-tall", tall);
    if (tall) {
      scroller.tabIndex = 0;
      scroller.setAttribute("role", "region");
      scroller.setAttribute("aria-label", (spec.caption || "Table") + ", scrolls");
    } else if (!scroller.classList.contains("grid-wide")) {
      scroller.tabIndex = -1;
      scroller.removeAttribute("role");
      scroller.removeAttribute("aria-label");
    }
  }
  fitBox();

  function visibleColumns() {
    return columns.filter(function (column) {
      return shownKeys.indexOf(column.key) !== -1 || column.index === 0;
    });
  }

  function updateChooserLabel() {
    if (!chooserButton) return;
    var label = chooserButton.querySelector(".button-label");
    var text = "Columns (" + visibleColumns().length + " of " + columns.length + ")";
    if (label) label.textContent = text;
    else chooserButton.appendChild(el("span", { class: "button-label", text: text }));
    chooserButton.setAttribute("aria-label", inTable(text));
  }

  function buildChooser(body) {
    body.appendChild(el("p", { class: "popover-title", text: "Columns to show" }));
    var list = el("div", { class: "chooser-list" });
    columns.forEach(function (column) {
      var id = gridId + "-col-" + column.index;
      var box = el("input", { type: "checkbox", id: id, checked: shownKeys.indexOf(column.key) !== -1 || column.index === 0, disabled: column.index === 0 });
      box.addEventListener("change", function () {
        if (box.checked && shownKeys.indexOf(column.key) === -1) shownKeys = shownKeys.concat([column.key]);
        if (!box.checked) shownKeys = shownKeys.filter(function (k) { return k !== column.key; });
        storageSet("tls:cols:" + gridId, JSON.stringify(shownKeys));
        drawHead();
        drawBody();
        updateChooserLabel();
      });
      list.appendChild(el("label", { class: "chooser-item", for: id }, [box, el("span", { text: column.label || column.key })]));
    });
    body.appendChild(list);
    var all = button("Show all", {
      variant: "quiet",
      action: function () {
        shownKeys = columns.map(function (c) { return c.key; });
        storageSet("tls:cols:" + gridId, JSON.stringify(shownKeys));
        list.querySelectorAll("input").forEach(function (b) { b.checked = true; });
        drawHead();
        drawBody();
        updateChooserLabel();
      },
    });
    var reset = button("Back to the usual", {
      variant: "quiet",
      action: function () {
        shownKeys = defaultKeys.slice();
        storageSet("tls:cols:" + gridId, JSON.stringify(shownKeys));
        list.querySelectorAll("input").forEach(function (b, i) { b.checked = shownKeys.indexOf(columns[i].key) !== -1 || i === 0; });
        drawHead();
        drawBody();
        updateChooserLabel();
      },
    });
    body.appendChild(el("div", { class: "chooser-actions" }, [all, reset]));
  }

  // A column of a table whose rows each carry their own kind (the
  // Overview totals: counts, tokens, money in one column) reads as
  // numeric when every value in it is a number, so its header lines up
  // with its right-aligned cells.
  function headerNumeric(column) {
    if (isNumeric(column)) return true;
    if (!spec.rowKinds || column.index === 0 || !rows.length) return false;
    return rows.every(function (row) {
      var value = columnValue(column, row);
      return value === null || value === undefined || typeof value === "number";
    });
  }

  // A header wraps to three lines at most (app.css): its label keeps room
  // for the longest line of its most even split (labelWidth), and one
  // still cut says the rest in a title (titleCutLabels). Its accessible name is
  // the label and unit alone, not the (?)'s. Column help opens from the
  // (?) or, on a heading that sorts, its ? key, so a column is one Tab
  // stop, not two.
  function headerCell(column) {
    var name = column.label || column.key;
    var unit = column.kind === "money" ? moneyUnit() : "";
    // A label that names its unit already, for the CLI's tables ("Cost
    // (list-price equivalent)"), drops it: the heading's unit says it.
    if (unit) name = name.replace(/\s*\((list-price equivalent|\$|USD)\)$/, "");
    var th = el("th", {
      scope: "col",
      class: (headerNumeric(column) ? "num" : "") + (column.key === "__select" ? " col-select" : ""),
      "data-key": column.key,
      "aria-label": unit ? name + " (" + unit + ")" : name,
    });
    // A hyphenated word, and the last word with a short unit ("$"),
    // don't break. A long unit ("list-price $") is a line of its own
    // when it needs one, so it doesn't widen the column it heads.
    var words = name.split(/\s+/).filter(Boolean);
    var ownLine = unit.length > 3;
    var label = el("span", { class: "th-label" });
    words.forEach(function (word, i) {
      var last = unit && !ownLine && i === words.length - 1;
      if (i) label.appendChild(document.createTextNode(" "));
      if (!last && word.indexOf("-") === -1) {
        label.appendChild(document.createTextNode(word));
        return;
      }
      var whole = label.appendChild(el("span", { class: "nowrap", text: word }));
      if (last) whole.appendChild(el("span", { class: "unit", text: " " + unit }));
    });
    if (unit && ownLine) {
      label.appendChild(document.createTextNode(" "));
      label.appendChild(el("span", { class: "unit nowrap", text: unit }));
      words.push(unit);
    } else if (unit && words.length) words[words.length - 1] += " " + unit;
    label.style.minWidth = labelWidth(words) + "ch";
    var inner = el("span", { class: "th-inner" }, [label]);
    th.appendChild(inner);
    var helpBtn = null;
    if (column.help) {
      helpBtn = button("?", {
        class: "col-help-btn",
        label: "What is " + name + (spec.caption ? " in " + spec.caption.replace(/\?$/, "") : "") + "?",
      });
      popoverButton(
        helpBtn,
        function (body) {
          body.appendChild(el("p", { class: "popover-title", text: name }));
          body.appendChild(el("p", null, prose(column.help)));
        },
        { class: "help-popover", label: name, focusInside: false }
      );
      // A click or Enter on the (?) must not also sort the column.
      helpBtn.addEventListener("click", function (event) {
        event.stopPropagation();
      });
      helpBtn.addEventListener("keydown", function (event) {
        event.stopPropagation();
      });
      // The heading takes the Tab stop, and its ? key opens the help.
      if (sortable) helpBtn.tabIndex = -1;
      inner.appendChild(helpBtn);
    }
    if (sortable) {
      th.tabIndex = 0;
      th.classList.add("sortable");
      var active = sort && sort.key === column.key;
      th.setAttribute("aria-sort", active ? (sort.ascending ? "ascending" : "descending") : "none");
      if (helpBtn) th.setAttribute("aria-keyshortcuts", "?");
      var indicator = el("span", { class: "sort-indicator" });
      if (active) indicator.appendChild(icon(sort.ascending ? "arrow-up" : "arrow-down", { size: 12 }));
      inner.appendChild(indicator);
      th.addEventListener("click", function () {
        sortBy(column);
      });
      th.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          sortBy(column);
        } else if (event.key === "?" && helpBtn) {
          // Handled here, so the page's own ? (the shortcuts) stays out.
          event.preventDefault();
          helpBtn.click();
        }
      });
    }
    return th;
  }

  // A label still cut at two lines says the rest in its title. Read
  // after layout: when the grid is drawn, sorted or resized.
  function titleCutLabels() {
    Array.prototype.forEach.call(thead.querySelectorAll(".th-label"), function (label) {
      if (label.scrollHeight > label.clientHeight + 1) label.title = label.textContent;
      else label.removeAttribute("title");
    });
  }

  function drawHead() {
    clear(thead);
    var tr = el("tr");
    visibleColumns().forEach(function (column) {
      tr.appendChild(headerCell(column));
    });
    thead.appendChild(tr);
    if (table.isConnected) titleCutLabels();
  }

  function sortValue(column, row) {
    return column.sortValue ? column.sortValue(row) : columnValue(column, row);
  }

  function applySort() {
    if (!sort) {
      orderedRows = rows;
      return;
    }
    var column = columns.filter(function (c) { return c.key === sort.key; })[0];
    orderedRows = rows.slice().sort(function (a, b) {
      // A total stays last whichever way the other rows run.
      if (isTotal(a) !== isTotal(b)) return isTotal(a) ? 1 : -1;
      var av = sortValue(column, a);
      var bv = sortValue(column, b);
      var an = typeof av === "number" ? av : parseFloat(av);
      var bn = typeof bv === "number" ? bv : parseFloat(bv);
      var cmp;
      if (typeof av === "number" && typeof bv === "number") cmp = av - bv;
      else if (!isNaN(an) && !isNaN(bn) && av !== null && bv !== null && isNumeric(column)) cmp = an - bn;
      else cmp = cellSortValue(av).localeCompare(cellSortValue(bv));
      return sort.ascending ? cmp : -cmp;
    });
  }

  // First press: biggest first for a number, A to Z for text; the next
  // press turns it round.
  function sortBy(column) {
    var ascending = sort && sort.key === column.key ? !sort.ascending : !isNumeric(column);
    sort = { key: column.key, ascending: ascending };
    storageSet("tls:sort:" + gridId, JSON.stringify(sort));
    applySort();
    var focusedKey = document.activeElement && document.activeElement.getAttribute && document.activeElement.getAttribute("data-key");
    // The fold keeps the cited rows of the new order.
    fitBox();
    drawHead();
    drawBody();
    updateMore();
    if (focusedKey) {
      var again = thead.querySelector('th[data-key="' + CSS.escape(focusedKey) + '"]');
      if (again) again.focus();
    }
  }

  function bodyRow(row) {
    var tr = el("tr");
    var key = keyOf(row);
    var total = isTotal(row);
    if (key !== null && key !== undefined) tr.setAttribute("data-row-key", String(key));
    var rowClass = spec.rowClass ? spec.rowClass(row) : null;
    if (rowClass) tr.classList.add(rowClass);
    var rowKind = spec.rowKinds && Array.isArray(row) && typeof row[0] === "string" ? spec.rowKinds[row[0]] : null;
    if (spec.link) linkRow(tr, spec.link.key(row));
    var colour = spec.swatch ? spec.swatch(row) : null;
    visibleColumns().forEach(function (column, position) {
      var value = columnValue(column, row);
      // Same rule as the header (headerNumeric): in a table whose rows
      // carry their own kinds, a number is right-aligned even in a row
      // with no kind listed.
      // A metric-and-value table's empty value ("-") lines up with the
      // numbers above and below it.
      var numeric = isNumeric(column) || (!!spec.rowKinds && column.index > 0 && (typeof value === "number" || value === null || value === undefined));
      var wrapClass = numeric ? "num" : proseCols[column.index] ? "cell-prose" : column.nowrap || proseCols[column.index] === false ? "nowrap" : "";
      var td = el("td", { class: wrapClass + " col-" + column.key, "data-sort": cellSortValue(value) });
      var content = cellContent(column, row, value, spec, rowKind);
      var full = fullValue(value, column.kind);
      if (full) td.title = plainText(full);
      if (column.index === bar && !total && typeof value === "number" && value > 0 && maxima[column.index] > 0) {
        td.appendChild(
          el("span", { class: "bar-cell" }, [barNode(value, maxima[column.index]), typeof content === "string" ? el("span", { text: content }) : content])
        );
      } else if (typeof content === "string") {
        td.textContent = content;
      } else if (content) {
        td.appendChild(content);
      }
      if (colour && position === 0) td.insertBefore(swatch(colour), td.firstChild);
      if (spec.tint && column.kind === "pct" && typeof value === "number" && maxima[column.index] > 0 && column.index !== bar) {
        td.classList.add("tint-" + Math.max(1, Math.min(5, Math.ceil((value / maxima[column.index]) * 5))));
      }
      tr.appendChild(td);
    });
    if (rowMark && key !== null && key !== undefined) markRow(tr, rowMark(String(key), rowName(tr)));
    if (spec.rowAction) {
      tr.classList.add("clickable");
      tr.tabIndex = 0;
      tr.setAttribute("aria-label", spec.rowAction.label(row));
      var run = function () {
        spec.rowAction.run(row, tr);
      };
      tr.addEventListener("click", function (event) {
        // A control inside the row keeps its own click.
        if (event.target.closest && event.target.closest("button, a, input, select, label")) return;
        run();
      });
      tr.addEventListener("keydown", function (event) {
        if (event.target !== tr) return;
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          run();
        }
      });
    }
    return tr;
  }

  // A row's name as its first cell shows it (before any mark joins it).
  function rowName(tr) {
    return tr.firstChild ? tr.firstChild.textContent.trim() : "";
  }

  function markRow(tr, mark) {
    if (!mark || !tr.firstChild) return;
    tr.classList.add("is-evidence");
    tr.firstChild.appendChild(mark);
  }

  // A row that means the same thing as a chart mark: pointing at either
  // lights both.
  function linkRow(tr, key) {
    if (key === null || key === undefined) return;
    tr.setAttribute("data-link-key", String(key));
    var on = function () {
      highlight(spec.link.scope, key);
    };
    var off = function () {
      highlight(spec.link.scope, null);
    };
    tr.addEventListener("mouseenter", on);
    tr.addEventListener("mouseleave", off);
    tr.addEventListener("focusin", on);
    tr.addEventListener("focusout", off);
  }

  function spacer(height) {
    var tr = el("tr", { class: "grid-spacer", "aria-hidden": "true" });
    var td = el("td", { colSpan: visibleColumns().length });
    td.style.height = height + "px";
    tr.appendChild(td);
    return tr;
  }

  // Row groups (Table.row_groups): a heading row wherever the group
  // changes -- only in the table's own order; a sorted table drops them.
  function drawBody() {
    clear(tbody);
    if (virtual) {
      drawWindow();
      return;
    }
    var group = null;
    var groups = sort ? null : spec.rowGroups;
    var size = foldSize();
    var drawn = folded() ? (fromEnd() ? orderedRows.slice(-size) : orderedRows.slice(0, size)) : orderedRows;
    drawn.forEach(function (row) {
      var rowGroup = groups && Array.isArray(row) && typeof row[0] === "string" ? groups[row[0]] : null;
      if (rowGroup && rowGroup !== group) {
        group = rowGroup;
        tbody.appendChild(el("tr", { class: "row-group" }, [el("th", { scope: "colgroup", colSpan: visibleColumns().length, text: group })]));
      }
      tbody.appendChild(bodyRow(row));
    });
  }

  // Only the rows in view (and a margin either side), between two
  // spacers as tall as the rows left out.
  var windowStart = -1;
  function drawWindow(force) {
    var viewport = scroller.clientHeight || 560;
    var start = Math.max(0, Math.floor(scroller.scrollTop / rowHeight) - 15);
    var end = Math.min(orderedRows.length, start + Math.ceil(viewport / rowHeight) + 30);
    if (!force && start === windowStart && tbody.firstChild) return;
    windowStart = start;
    clear(tbody);
    tbody.appendChild(spacer(start * rowHeight));
    for (var i = start; i < end; i++) tbody.appendChild(bodyRow(orderedRows[i]));
    tbody.appendChild(spacer((orderedRows.length - end) * rowHeight));
  }

  function rowIndex(rowKey) {
    for (var i = 0; i < orderedRows.length; i++) {
      if (String(keyOf(orderedRows[i])) === String(rowKey)) return i;
    }
    return -1;
  }

  // A dated table (spec.limitFrom "end", oldest row first) folds to its
  // latest rows while it keeps its own order.
  function fromEnd() {
    return spec.limitFrom === "end" && !sort;
  }

  // The fold's button says what it does next. It goes once the fold,
  // grown to take in a cited row, would leave out 2 rows or fewer.
  var moreRow = null;
  function updateMore() {
    if (!moreButton) return;
    var size = foldSize();
    moreRow.hidden = rows.length <= size + 2;
    moreButton.setAttribute("aria-expanded", expanded ? "true" : "false");
    var words = expanded ? (fromEnd() ? "Show the latest " : "Show the first ") + size : "Show all " + rows.length + " rows";
    moreButton.querySelector(".button-label").textContent = words;
    moreButton.setAttribute("aria-label", inTable(words));
  }

  function setExpanded(open) {
    expanded = open;
    updateMore();
    fitBox();
    drawBody();
  }

  if (limited) {
    moreButton = button("Show all " + rows.length + " rows", {
      variant: "link",
      action: function () {
        setExpanded(!expanded);
      },
    });
    moreButton.setAttribute("aria-expanded", "false");
    moreButton.setAttribute("aria-label", inTable("Show all " + rows.length + " rows"));
    moreButton.setAttribute("aria-controls", gridId);
    moreRow = wrap.appendChild(el("div", { class: "grid-more" }, [moreButton]));
    // An evidence link's row may be past the first rows: show them all
    // (pulseRow calls this). Returns whether the key is one of the rows.
    table.gridScrollTo = function (rowKey) {
      if (rowIndex(rowKey) === -1) return false;
      if (!expanded) setExpanded(true);
      return true;
    };
  }

  if (virtual) {
    // An evidence link's row may be out of the drawn window: scroll the
    // grid so it is drawn (pulseRow calls this). Returns whether the
    // key is one of the grid's rows.
    table.gridScrollTo = function (rowKey) {
      var index = rowIndex(rowKey);
      if (index === -1) return false;
      var viewport = scroller.clientHeight || 560;
      scroller.scrollTop = Math.max(0, index * rowHeight - viewport / 2 + rowHeight / 2);
      drawWindow(true);
      return true;
    };
    var pending = false;
    scroller.addEventListener("scroll", function () {
      if (pending) return;
      pending = true;
      requestAnimationFrame(function () {
        pending = false;
        drawWindow();
      });
    });
    // Measure a real row once it is on screen, then draw to fit.
    requestAnimationFrame(function () {
      var sample = tbody.querySelector("tr:not(.grid-spacer)");
      if (sample && sample.getBoundingClientRect().height) rowHeight = sample.getBoundingClientRect().height;
      drawWindow(true);
    });
  }

  applySort();
  drawHead();
  drawBody();
  updateChooserLabel();

  // Edge cues (app.css): a shadow at the right while the grid scrolls
  // further right, and one off the pinned first column once it has
  // scrolled. Set from the scroll position, at most once a frame.
  var cueFrame = 0;
  function setCues() {
    cueFrame = 0;
    var room = scroller.scrollWidth - scroller.clientWidth;
    frame.classList.toggle("cue-start", room > 1 && scroller.scrollLeft > 1);
    frame.classList.toggle("cue-end", room > 1 && scroller.scrollLeft < room - 1);
  }
  scroller.addEventListener(
    "scroll",
    function () {
      if (!cueFrame) cueFrame = requestAnimationFrame(setCues);
    },
    { passive: true }
  );

  // A grid wider than its box scrolls sideways: it becomes a Tab stop
  // so the arrow keys can scroll it, and its first column stays put.
  // Checked whenever the box or the table changes size (the window, a
  // column chosen, rows shown), so a grid drawn inside a closed "More
  // tables" is checked when it opens.
  if (typeof ResizeObserver === "function") {
    var observer = new ResizeObserver(function () {
      // Before it scrolls, a grid too wide for its box draws tighter
      // (app.css's .grid-snug), which fits most tables at the desktop
      // width. The class then stays: the change of size it makes calls
      // back here to judge the tighter table.
      if (!scroller.classList.contains("grid-snug") && scroller.clientWidth > 0 && scroller.scrollWidth > scroller.clientWidth + 1) {
        scroller.classList.add("grid-snug");
        return;
      }
      var wide = scroller.clientWidth > 0 && scroller.scrollWidth > scroller.clientWidth + 1;
      scroller.classList.toggle("grid-wide", wide);
      if (wide && scroller.tabIndex !== 0) {
        scroller.tabIndex = 0;
        if (!scroller.hasAttribute("role")) {
          scroller.setAttribute("role", "region");
          scroller.setAttribute("aria-label", (spec.caption || "Table") + ", scrolls sideways");
        }
      }
      setCues();
      titleCutLabels();
    });
    observer.observe(scroller);
    observer.observe(table);
  }
  if (spec.link) {
    // Dropped once the grid has been on the page and left it.
    var seen = false;
    listenHighlight(function (scope, key) {
      if (!wrap.isConnected) return !seen;
      seen = true;
      if (scope !== spec.link.scope) return true;
      Array.prototype.forEach.call(tbody.querySelectorAll("tr[data-link-key]"), function (tr) {
        tr.classList.toggle("is-linked", key !== null && tr.getAttribute("data-link-key") === String(key));
      });
      return true;
    });
  }
  wrap.grid = {
    table: table,
    scroller: scroller,
    // Marks the rows drawn now; rows drawn later (sorting, scrolling a
    // long grid) are marked as they are drawn. mark(key, name) is told
    // the row's name as drawn. A folded table first grows its fold to
    // take in every row an action cites.
    markRows: function (mark) {
      rowMark = mark;
      if (limited) {
        var before = foldSize();
        cited = {};
        rows.forEach(function (row) {
          var key = keyOf(row);
          if (key !== null && key !== undefined && mark(String(key))) cited[String(key)] = true;
        });
        if (foldSize() !== before) {
          updateMore();
          fitBox();
          drawBody();
          return;
        }
      }
      Array.prototype.forEach.call(tbody.querySelectorAll("tr[data-row-key]"), function (tr) {
        if (!tr.classList.contains("is-evidence")) markRow(tr, mark(tr.getAttribute("data-row-key"), rowName(tr)));
      });
    },
  };
  return wrap;
}

// Bring a row into view and pulse it (an evidence link's target). A
// virtualised grid scrolls to the row first. grid: the table's id or
// the table itself. Returns false when the row isn't in the grid.
export function pulseRow(grid, rowKey) {
  var table = typeof grid === "string" ? document.getElementById(grid) : grid;
  if (!table) return false;
  var selector = 'tr[data-row-key="' + CSS.escape(String(rowKey)) + '"]';
  var row = table.querySelector(selector);
  if (!row && typeof table.gridScrollTo === "function" && table.gridScrollTo(rowKey)) row = table.querySelector(selector);
  if (!row) return false;
  pulseNode(row, "row-target");
  return true;
}

// Bring any evidence target into view and pulse it: a grid row
// ("row-target") or a block that isn't a grid, such as a scorecard area
// ("block-target").
export function pulseNode(node, cls) {
  cls = cls || "block-target";
  node.scrollIntoView({ block: "center", behavior: motionOK() ? "smooth" : "instant" });
  node.classList.remove(cls);
  void node.offsetWidth;
  node.classList.add(cls);
  if (motionOK()) {
    setTimeout(function () {
      node.classList.remove(cls);
    }, 1400);
    return;
  }
  // Reduced motion: the highlight holds still until the next click or
  // key, so a slower reader never loses the row.
  function clearTarget() {
    node.classList.remove(cls);
    document.removeEventListener("pointerdown", clearTarget, true);
    document.removeEventListener("keydown", clearTarget, true);
  }
  setTimeout(function () {
    document.addEventListener("pointerdown", clearTarget, true);
    document.addEventListener("keydown", clearTarget, true);
  }, 0);
}

// -- report tables and sections ------------------------------------------------

// The Glossary > How costs work card behind each section's figures,
// linked from the end of its "How to read this".
var SECTION_CARDS = {
  recache: "cache-rebuilds",
  recache_by_group: "cache-rebuilds",
  limits: "billing-mode",
  ttl: "cache-writes",
  model_swap: "model-choice",
  agent_startup: "startup-context",
  context_budget: "startup-context",
  carry: "tool-output",
  compaction_sim: "conversation-summaries",
  // The planning context kept is re-read on every later reply.
  plan_handoff: "cache-reads",
  // What a long run has read is re-read on every later reply.
  run_split: "cache-reads",
  // A hook's added context is re-read on every later reply.
  hooks: "cache-reads",
  // A definition tool search keeps out would be read on every reply.
  tool_search: "cache-reads",
  compactions: "conversation-summaries",
  elasticity: "billing-mode",
};

function sectionCard(sectionKey) {
  var slug = SECTION_CARDS[sectionKey];
  for (var i = 0; slug && i < COST_CARDS.length; i++) if (COST_CARDS[i].slug === slug) return COST_CARDS[i];
  return null;
}

// A section or table's header: its heading, then the (i) that opens
// "How to read this". card: the cost card it links to, if any.
export function headRow(heading, help, subject, card) {
  var row = el("div", { class: "block-head" }, [heading]);
  var helpNode = helpButton(help, subject, card);
  if (helpNode) row.appendChild(helpNode);
  return row;
}

// Notes under a table or section, with page links and glossary terms. A
// short note or two stay in view; more, or longer, fold away: they say
// how the figures were worked out, which few readers need, and keep the
// page short.
var NOTES_IN_VIEW_CHARS = 240;

// ofTable: the notes are one table's, under it. Folded, they say "How
// this table is worked out", so they don't read like the section's own
// "How these figures are worked out" a few lines further down.
export function notesList(notes, seen, ofTable) {
  var list = el(
    "ul",
    { class: "notes" },
    notes.map(function (note) {
      return el("li", null, prose(note, seen));
    })
  );
  var length = notes.reduce(function (total, note) {
    return total + String(note).length;
  }, 0);
  if (notes.length <= 2 && length <= NOTES_IN_VIEW_CHARS) return list;
  return el("details", { class: "disclosure notes-detail" }, [
    el("summary", { text: (ofTable ? "How this table is worked out" : "How these figures are worked out") + " (" + (notes.length === 1 ? "1 note" : notes.length + " notes") + ")" }),
    list,
  ]);
}

// The actions a table or row is evidence for (api.js's actionIndex), as
// a button that lists them, each linked to its detail in Actions.
// chip: the header's "Feeds N actions"; otherwise a row's small mark.
// of: the table's title (the chip) or the row's name and table (the
// mark), which ends the button's name, so a page's marks don't read alike.
function feedsButton(actions, chip, of) {
  var words = actions.length === 1 ? "1 action" : actions.length + " actions";
  var named = function (text) {
    return of ? text + ": " + of : text;
  };
  var trigger = chip
    ? el("button", { type: "button", class: "chip chip-accent feeds-chip", "aria-label": named("Feeds " + words) }, [icon("actions", { size: 12 }), el("span", { text: "Feeds " + words })])
    : el("button", { type: "button", class: "row-feeds", "aria-label": named("Evidence for " + words), title: "Evidence for " + words }, [icon("actions", { size: 12 })]);
  return popoverButton(
    trigger,
    function (body) {
      body.appendChild(el("p", { class: "popover-title", text: chip ? "Actions these figures feed" : "Actions this row is evidence for" }));
      body.appendChild(
        el(
          "ul",
          { class: "feeds-list" },
          actions.map(function (action) {
            return el("li", null, [pageLink("actions/recommendations", action.title, { id: action.key })]);
          })
        )
      );
    },
    { class: "feeds-popover", label: chip ? "Actions these figures feed" : "Actions this row is evidence for" }
  );
}

// Once the recommendations load: the header chip and the row marks.
// title: the table's, which the chip's name ends with.
function markFeeds(wrap, head, gridNode, tableName, title) {
  actionIndex().then(function (index) {
    var actions = index.byTable[tableName];
    if (!actions || !actions.length) return;
    if (!head) {
      head = el("div", { class: "block-head block-head-help" });
      wrap.insertBefore(head, wrap.firstChild);
    }
    head.appendChild(feedsButton(actions, true, title));
    if (gridNode && gridNode.grid) {
      gridNode.grid.markRows(function (key, name) {
        var rowActions = index.byRow[tableName + "\n" + key];
        // A row can be cited in more than one table on a page.
        return rowActions && rowActions.length ? feedsButton(rowActions, false, (name || key) + (title ? " in " + title : "")) : null;
      });
    }
  });
}

// -- a one-row table as tiles ---------------------------------------------------

// A summary table (one row) with lead columns (helptext.py) reads as a
// strip of at most this many tiles, its other figures in "All figures".
var STRIP_TILES = 4;

// The column indexes a summary table shows as tiles, or null for a table
// that stays a grid.
function summaryColumns(table) {
  if (!table.rows || table.rows.length !== 1 || !table.lead_columns || !table.lead_columns.length) return null;
  // A table that leads with its row key is a list that happens to have
  // one row today (one agent type, one project): it stays a grid.
  if (table.columns.length && table.lead_columns.indexOf(table.columns[0].key) !== -1) return null;
  var indexes = [];
  table.lead_columns.forEach(function (key) {
    for (var i = 0; i < table.columns.length; i++) if (table.columns[i].key === key) indexes.push(i);
  });
  return indexes.length ? indexes.slice(0, STRIP_TILES) : null;
}

// A figure as a sentence would quote it: an amount in the billing mode, a
// raw value by its label.
function factValue(table, column, value) {
  if (column.kind === "money" && typeof value === "number") return moneyText(value);
  if (typeof value === "string" && table.value_labels && table.value_labels[value]) return table.value_labels[value];
  return plainText(formatCell(value, wholeKind(value, column.kind)));
}

function summaryTile(table, column, value) {
  var opts = { label: column.label || column.key };
  if (column.kind === "money" && typeof value === "number") {
    var parts = moneyParts(value);
    opts.value = parts.value;
    opts.unit = parts.unit;
    opts.hint = parts.secondary || null;
  } else {
    opts.value = el("span", { text: factValue(table, column, value), title: fullValue(value, column.kind) || null });
  }
  return tile(opts);
}

// Every figure of a summary table, as a list. A first column named
// "metric" only labels the row ("all"), so it is left out. The row key
// is on the list, for an evidence link to pulse.
function summaryFacts(table) {
  var row = table.rows[0];
  var list = el("dl", { class: "fact-list summary-facts", "data-row-key": String(row[0]) });
  table.columns.forEach(function (column, i) {
    if (i === 0 && column.key === "metric") return;
    list.appendChild(el("dt", { text: column.label || column.key }));
    list.appendChild(el("dd", { text: fullValue(row[i], column.kind) || factValue(table, column, row[i]) }));
  });
  return list;
}

// -- which report tables rank ----------------------------------------------------

// The report lists rows in an order that means something (by date, by
// bucket or size, ranked on some figure) or, where none does, A to Z by
// name. An A-to-Z list opens as a ranking instead, biggest first on its
// lead measure (dataGrid's rank). A table keeps its own order when it is
// dated (NEWEST_LAST), read down its rows (row groups or kinds), or led
// by a scale: numbers, dates, hours, sizes, buckets or levels.
var SCALE_KEY = /(?:^|_)(?:bucket|window|depth|level|effort|hour|day|date|week|month|period|start)$/;
var SCALE_VALUE = /^[<>~]?\s*\d/;

function inOrder(names, by) {
  for (var i = 1; i < names.length; i++) if (!(by(names[i - 1]) < by(names[i]))) return false;
  return true;
}

function aToZ(names) {
  return inOrder(names, String) || inOrder(names, function (name) { return name.toLowerCase(); });
}

// null when the table keeps its order. Else {total}: the table ranks,
// and total is the key of a last row after the A-to-Z run (a total, or
// "other"), which stays last, or null.
function ranking(table) {
  var rows = table.rows || [];
  var first = (table.columns || [])[0];
  if (rows.length < 2 || !first || NEWEST_LAST[table.name] || hasKeys(table.row_groups) || hasKeys(table.row_kinds)) return null;
  if (NUMERIC_KINDS[first.kind] || SCALE_KEY.test(first.key || "")) return null;
  var names = rows.map(function (row) {
    return row[0];
  });
  var named = names.every(function (name) {
    return typeof name === "string" && !SCALE_VALUE.test(name);
  });
  if (!named) return null;
  // A row for all of them ("all", read "All projects") is the total
  // wherever the server's A to Z put it, so it stays last.
  var total = names.filter(function (name) {
    return TOTAL_KEY.test(name);
  })[0];
  if (total !== undefined) {
    var others = names.filter(function (name) {
      return name !== total;
    });
    if (others.length >= 2 && aToZ(others)) return { total: total };
  }
  if (aToZ(names)) return { total: null };
  if (names.length > 3 && aToZ(names.slice(0, -1))) return { total: names[names.length - 1] };
  return null;
}

var TOTAL_KEY = /^(all|total)$/i;

// A table's own empty state where "a longer window" is the wrong reason:
// [what happened, why or what next].
var EMPTY_TEXT = {
  context_budget_statusline: ["No status line readings in this window.", "This table fills once the status line logger is installed and has logged a session."],
  habits_outcomes: ["No feedback on your work in this window.", "Answer /cg-feedback, or rate a session on {{page:spend/sessions}}, to fill this table."],
  habits_by_shape: ["No main sessions with a message of yours in this window.", "A longer window may include some."],
};

// A table whose every figure is zero or blank says nothing its rows of
// zeros would: it shows as the grid's own "Nothing to show" note, not
// as tiles or rows of zeros. Text cells (a cause, a label) aren't
// figures, and a table without any figures is judged by its rows alone.
function tableIsEmpty(table) {
  var rows = table.rows || [];
  if (!rows.length) return true;
  var byRow = hasKeys(table.row_kinds);
  // A summary is judged by its tiles' figures: its other figures are
  // often totals it is a share of ("of 3,107 replies").
  var strip = summaryColumns(table);
  var figures = 0;
  for (var r = 0; r < rows.length; r++) {
    for (var c = 0; c < table.columns.length; c++) {
      if (strip && strip.indexOf(c) === -1) continue;
      var value = rows[r][c];
      var blank = value === null || value === undefined || value === "" || value === "-";
      var figure = NUMERIC_KINDS[table.columns[c].kind] || (byRow && c > 0 && (typeof value === "number" || blank));
      if (!figure) continue;
      figures += 1;
      if (!blank && value !== 0) return false;
    }
  }
  return figures > 0;
}

function emptyText(table) {
  // Five-hour blocks come only with a plan's usage limits.
  if (table.name === "five_hour_blocks" && (state.units || {}).mode !== "subscription") {
    return ["Five-hour blocks exist only on a Pro or Max plan.", "Your billing is set to pay per token (API), so there are none to show."];
  }
  return EMPTY_TEXT[table.name] || ["Nothing to show for this window.", "A longer window may include some."];
}

// options.heading false: the caller has already titled the table (a
// table shown away from its section, under its own section heading).
// options.seen: the glossary terms its section has already explained.
export function renderTable(table, tableId, currency, options) {
  // Named for evidence links (evidence.js): report table names are
  // unique across the report.
  var wrap = el("div", { class: "table-wrap", "data-table-name": table.name || null });
  var head = null;
  if (!options || options.heading !== false) {
    head = wrap.appendChild(headRow(el("h3", { text: table.title || table.name }), table.help, table.title || table.name));
  } else if (options.helpInto) {
    // The heading above says it already: its row takes the table's
    // "How to read this" (unless it has one) and its Feeds chip.
    head = options.helpInto;
    var ownHelp = table.help && !head.querySelector(".help-button") ? helpButton(table.help, table.title || table.name) : null;
    if (ownHelp) head.appendChild(ownHelp);
  } else if (table.help) {
    var helpNode = helpButton(table.help, table.title || table.name);
    if (helpNode) head = wrap.appendChild(el("div", { class: "block-head block-head-help" }, [helpNode]));
  }
  if (tableIsEmpty(table)) {
    var none = emptyText(table);
    wrap.classList.add("table-empty");
    wrap.appendChild(emptyState(none[0], null, none[1]));
    if (table.name) markFeeds(wrap, head, null, table.name, table.title || table.name);
    return wrap;
  }
  var strip = summaryColumns(table);
  if (strip) {
    // A summary (one row): its headline figures as tiles, every figure
    // one click away.
    wrap.classList.add("summary-table");
    wrap.appendChild(
      tileRow(
        strip.map(function (i) {
          return summaryTile(table, table.columns[i], table.rows[0][i]);
        }),
        { class: "summary-tiles" }
      )
    );
    var facts = summaryFacts(table);
    wrap.appendChild(
      el("details", { class: "disclosure summary-details" }, [el("summary", { text: "All figures (" + facts.querySelectorAll("dt").length + ")" }), facts])
    );
    if (table.notes && table.notes.length) wrap.appendChild(notesList(table.notes, (options && options.seen) || new Set(), true));
    if (table.name) markFeeds(wrap, head, null, table.name, table.title || table.name);
    return wrap;
  }
  var rank = ranking(table);
  var empty = emptyText(table);
  var gridNode = wrap.appendChild(
    dataGrid({
      id: tableId,
      caption: table.title || table.name,
      columns: table.columns,
      rows: table.rows,
      lead: table.lead_columns || null,
      valueLabels: table.value_labels,
      rowGroups: table.row_groups,
      rowKinds: table.row_kinds,
      // A table read down its rows (grouped, or one kind per row) stays
      // whole, as does one a page asks for whole (options.allRows). The
      // report sends both as {} when a table has neither.
      limit: hasKeys(table.row_groups) || hasKeys(table.row_kinds) || (options && options.allRows) ? 0 : REPORT_ROWS,
      limitFrom: NEWEST_LAST[table.name] ? "end" : "start",
      rank: !!rank,
      totalLast: rank ? rank.total : null,
      empty: empty[0],
      emptyNext: empty[1],
      tint: TINT_TABLES.indexOf(table.name) !== -1,
    })
  );
  if (table.notes && table.notes.length) wrap.appendChild(notesList(table.notes, (options && options.seen) || new Set(), true));
  if (table.name) markFeeds(wrap, head, gridNode, table.name, table.title || table.name);
  return wrap;
}

// Opens a table a page leaves to the full report in the table drawer:
// open(table, sectionTitle). evidence.js sets it, since this module
// can't import that one (it imports this).
var reportTableDrawer = null;

export function setReportTableDrawer(open) {
  reportTableDrawer = open;
}

// The line under a section's tables for the ones left to the full
// report, with a button that opens each in the table drawer.
function reportTablesNote(tables, sectionTitle) {
  var note = el("p", {
    class: "notes report-tables-note",
    text:
      (tables.length === 1 ? "1 more table is" : tables.length + " more tables are") +
      " in the full report (" + cli("report") + ").",
  });
  if (!reportTableDrawer) return note;
  tables.forEach(function (table) {
    note.appendChild(document.createTextNode(" "));
    note.appendChild(
      button("Open " + (table.title || table.name), {
        variant: "link",
        action: function () {
          reportTableDrawer(table, sectionTitle);
        },
      })
    );
  });
  return note;
}

// Tables by dashboard placement (helptext.py's table audit): "keep"
// shown, "advanced" collapsed into "More tables", "report" left to the
// CLI report (and the JSON/CSV exports), with a line saying so that
// opens each in the table drawer.
export function renderPlacedTables(container, tables, currency, idPrefix, sectionTitle, seen) {
  var advanced = [];
  var reportOnly = [];
  tables.forEach(function (table, i) {
    var tableId = idPrefix + "-" + table.name + "-" + i;
    var placement = table.dashboard || "keep";
    if (placement === "report") {
      reportOnly.push(table);
    } else if (placement === "advanced") {
      advanced.push({ table: table, id: tableId });
    } else {
      // A section's one table often shares its title: say it once, in
      // the section's own heading row.
      var sameTitle = sectionTitle && (table.title || table.name) === sectionTitle;
      var section = sameTitle ? (container.closest && container.closest("section")) || container : null;
      var sectionHead = section ? section.querySelector(".block-head") : null;
      container.appendChild(renderTable(table, tableId, currency, { heading: !sameTitle, helpInto: sectionHead, seen: seen }));
    }
  });
  if (advanced.length) {
    var details = el("details", { class: "advanced-detail" });
    details.appendChild(el("summary", { text: "More tables (" + advanced.length + ")" }));
    advanced.forEach(function (item) {
      details.appendChild(renderTable(item.table, item.id, currency, { seen: seen }));
    });
    container.appendChild(details);
  }
  if (reportOnly.length) container.appendChild(reportTablesNote(reportOnly, sectionTitle));
}

// The chart a section draws above its tables (charts-types.js's
// sectionChart), set once by app.js: grid.js can't import the charts,
// which draw their Table view with this module.
var sectionChart = null;

export function setSectionChart(draw) {
  sectionChart = draw;
}

// The intro the view a section belongs on opens with (links.js's
// viewIntro): a section with the same words doesn't say them again.
function pageIntro(sectionKey) {
  var view = viewFor(viewForSection(sectionKey));
  return view ? (view.segment ? view.segment.intro : view.page.intro) || "" : "";
}

export function renderSectionGeneric(container, section, currency, idPrefix) {
  if (!section) return;
  var block = el("section", { class: "report-section", "data-section": section.key || null });
  // Sections are h2 and their tables h3: the page title is the one h1.
  block.appendChild(
    headRow(el("h2", { class: "section-title", text: section.title || section.key }), section.help, section.title || section.key, sectionCard(section.key))
  );
  // Each glossary term is explained once per section: its first use.
  var seen = new Set();
  if (section.intro && section.intro !== pageIntro(section.key)) block.appendChild(el("p", { class: "section-intro" }, prose(section.intro, seen)));
  var chart = sectionChart ? sectionChart(section) : null;
  // A section with nothing in any table it shows is one "Nothing to
  // show" note under its heading: no zero tiles, no tables of zeros, no
  // notes on how figures it doesn't have were worked out.
  // (Its "More tables" count too: a section is empty only when they are.)
  var shown = (section.tables || []).filter(function (table) {
    return (table.dashboard || "keep") !== "report";
  });
  if (!chart && shown.length && shown.every(tableIsEmpty)) {
    var none = emptyText(shown[0]);
    block.classList.add("section-empty");
    block.appendChild(emptyState(none[0], null, none[1]));
    container.appendChild(block);
    return;
  }
  if (chart) block.appendChild(chart);
  // Its shown tables all empty, only "More tables" with figures: one note
  // says so for all of them, then the rest as usual.
  var tables = section.tables || [];
  var kept = shown.filter(function (table) {
    return (table.dashboard || "keep") === "keep";
  });
  if (kept.length > 1 && kept.every(tableIsEmpty)) {
    var noneKept = emptyText(kept[0]);
    block.appendChild(emptyState(noneKept[0], null, noneKept[1]));
    tables = tables.filter(function (table) {
      return kept.indexOf(table) === -1;
    });
  }
  renderPlacedTables(block, tables, currency, idPrefix || section.key, section.title || section.key, seen);
  if (section.notes && section.notes.length) block.appendChild(notesList(section.notes, seen));
  container.appendChild(block);
}

// Every report section and table that belongs on a view (links.js's
// SECTION_PAGE_MAP and TABLE_PAGE_MAP). A section drops the tables placed
// elsewhere; a table placed here from another section's gets its own
// heading. skip: section keys the view draws some other way.
export function renderMappedSections(report, viewKey, container, skip) {
  if (!report || !Array.isArray(report.sections)) return;
  report.sections.forEach(function (section) {
    if (skip && skip.indexOf(section.key) !== -1) return;
    var here = (section.tables || []).filter(function (table) {
      return viewForTable(section.key, table.name) === viewKey;
    });
    if (viewForSection(section.key) === viewKey) {
      // The Overview draws its own section.
      if (section.key === "overview") return;
      renderSectionGeneric(container, Object.assign({}, section, { tables: here }), state.currency, "report");
      return;
    }
    here.forEach(function (table, i) {
      var block = el("section", { class: "report-section" });
      // The table's "How to read this" sits beside this heading, not on a
      // line of its own under it.
      var head = block.appendChild(headRow(el("h2", { class: "section-title", text: table.title || table.name }), null, table.title || table.name));
      block.appendChild(renderTable(table, "report-" + section.key + "-" + table.name + "-" + i, state.currency, { heading: false, helpInto: head }));
      container.appendChild(block);
    });
  });
}

// Shared by every report-backed route that returns a single Section
// directly (ttl, and the carry/compaction-sim/model-swap/waste routes in
// page-cache.js and page-spend.js) rather than the full report.json.
// Defensive: docs/api.md pins this to "the same shape as the CLI's
// ... section tables" but not byte-exactly to Section (a bare
// `{tables: [...]}` or a list of Table dicts are both plausible),
// and the route returns `null` outright when the section is absent
// from the assembled report -- handle each shape rather than
// assuming one and rendering nothing on a mismatch.
export function renderReportBackedSection(data, container, idPrefix, emptyNotice, emptyNext) {
  if (data && Array.isArray(data.tables)) {
    renderSectionGeneric(container, data, state.currency, idPrefix);
  } else if (Array.isArray(data)) {
    data.forEach(function (table, i) {
      container.appendChild(renderTable(table, idPrefix + "-" + i, state.currency));
    });
  } else {
    container.appendChild(emptyState(emptyNotice, null, emptyNext));
  }
}

// A small table of values already written out (a list the page built
// itself): the grid's look, in the order given. A column whose every
// cell reads as a number is right-aligned.
export function simpleTable(columns, rows, caption, id) {
  var wrap = el("div", { class: "table-wrap" });
  if (caption) wrap.appendChild(el("h3", { text: caption }));
  var numericColumn = columns.map(function (col, i) {
    return rows.length > 0 && rows.every(function (row) {
      var cell = row[i];
      return cell === null || cell === undefined || cell === "" || cell === "-" || /^[\u2212+\-<$]?[\d,.]+(%|[KMBT]| ?[a-z]+)?$/.test(String(cell));
    });
  });
  simpleTableCount += 1;
  wrap.appendChild(
    dataGrid({
      id: id || "simple-" + simpleTableCount,
      caption: caption || null,
      sortable: false,
      bar: -1,
      columns: columns.map(function (col, i) {
        return {
          key: "c" + i,
          label: col.label,
          render: function (row) {
            var cell = row[i];
            if (cell === null || cell === undefined) return "";
            // A model reads by its name, with its id on hover.
            var text = modelNames(String(cell));
            return text === String(cell) ? text : el("span", { title: String(cell), text: text });
          },
          kind: numericColumn[i] && i > 0 ? "float" : "str",
        };
      }),
      rows: rows,
    })
  );
  return wrap;
}

var simpleTableCount = 0;

// ======================================================================
// What you say about a card (a tip, a habit or a recommendation): a
// rating, never a setting. POST /api/tip-feedback keeps it in the
// service's own store; nothing in Claude Code changes.
// ======================================================================

// The answers load once and every card on the page shares them. The page
// keeps them up to date as you rate, and loads them again after half a
// minute.
var cardRatings = { loading: null, at: 0, options: [], answers: {} };

function loadCardRatings() {
  if (cardRatings.loading && Date.now() - cardRatings.at < 30000) return cardRatings.loading;
  cardRatings.at = Date.now();
  cardRatings.loading = fetchJson("/api/tip-feedback", null, true).then(function (res) {
    if (res.body && res.body.ok === true && res.body.data) {
      cardRatings.options = res.body.data.options || [];
      cardRatings.answers = {};
      (res.body.data.answers || []).forEach(function (row) {
        cardRatings.answers[row.kind + "|" + row.item] = row.answer;
      });
    } else {
      cardRatings.at = 0;
    }
    return cardRatings;
  });
  return cardRatings.loading;
}

// kind: "tip", "habit" or "recommendation"; item: the card's id. The row
// stays empty when the service has no rating to offer (an older one).
// Pressing the rating you gave takes it back.
export function cardRating(kind, item) {
  var row = el("div", { class: "card-rating", role: "group", "aria-label": "Your rating of this " + kind });
  var status = el("span", { class: "notes", role: "status" });
  row.appendChild(status);
  loadCardRatings().then(function (ratings) {
    if (!ratings.options.length) return;
    var key = kind + "|" + item;
    var buttons = {};
    function show() {
      Object.keys(buttons).forEach(function (word) {
        buttons[word].setAttribute("aria-pressed", ratings.answers[key] === word ? "true" : "false");
      });
    }
    function busy(on) {
      Object.keys(buttons).forEach(function (word) {
        buttons[word].disabled = on;
      });
    }
    row.insertBefore(el("span", { class: "card-rating-label", text: "Your rating:" }), status);
    ratings.options.forEach(function (opt) {
      var choice = button(opt.label, { variant: "quiet", title: opt.description, class: "card-rating-button" });
      buttons[opt.word] = choice;
      choice.addEventListener("click", function () {
        var next = ratings.answers[key] === opt.word ? null : opt.word;
        busy(true);
        status.textContent = "Saving…";
        postJson("/api/tip-feedback", { kind: kind, item: item, answer: next }).then(function (res) {
          busy(false);
          if (!res.body || res.body.ok !== true) {
            status.textContent = "Couldn't save that: " + ((res.body && res.body.error && res.body.error.message) || "the dashboard didn't answer") + ".";
            return;
          }
          if (next) ratings.answers[key] = next;
          else delete ratings.answers[key];
          show();
          status.textContent = next === "trying" ? "Noted. Its effect is measured from today." : next ? "Saved." : "Rating taken back.";
        });
      });
      row.insertBefore(choice, status);
    });
    show();
  });
  return row;
}

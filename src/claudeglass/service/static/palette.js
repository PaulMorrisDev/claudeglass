/* claudeglass service UI: palette.js
 *
 * Search (Ctrl+K) and the keyboard shortcuts. Search finds the pages,
 * the report's sections and tables, the recommendations and checks for
 * the window, glossary terms, recent sessions and projects, and runs a
 * few commands: a window, a project, a theme, copying a recommendation's
 * prompt. Like
 * the rest of the dashboard it only moves around and copies text; it
 * never changes Claude Code.
 *
 * Search is a modal <dialog> holding an ARIA combobox: focus stays in
 * the text box while the arrow keys move aria-activedescendant through
 * the results.
 */

import { clear, el, goTo, pickProject, state, WINDOW_OPTIONS } from "./core.js";
import { icon } from "./icons.js";
import { fetchJson, loadProjects, loadQuickActions, loadRecommendations, loadReport, scopeKey, withWindow } from "./api.js";
import { COST_CARDS, findPage, GLOSSARY, goToHabit, plainText, REWORK_ITEM, termSlug, VIEW_KEYS, viewFor, viewForSection, viewLabel } from "./links.js";
import { evidenceView, openEvidence } from "./evidence.js";
import { button, copyToClipboard, SEVERITY_LABELS, statusLabel, toast } from "./ui.js";
import { moneyText, projectName, shortTs } from "./format.js";
import { openSessionDrawer } from "./page-spend.js";
import { sessionWord } from "./charts-types.js";

// What app.js lends the palette: setWindow(value) and setTheme(value),
// which live with the controls they drive.
var hooks = { setWindow: null, setTheme: null };

// The result groups, in the order they show when nothing is typed.
var GROUPS = [
  { kind: "page", label: "Pages" },
  { kind: "command", label: "Commands" },
  { kind: "recommendation", label: "Recommendations" },
  { kind: "check", label: "Checks" },
  { kind: "habit", label: "Work habits" },
  { kind: "table", label: "Sections and tables" },
  { kind: "term", label: "Glossary" },
  { kind: "session", label: "Recent sessions" },
  { kind: "project", label: "Projects" },
];

// Results shown per group once something is typed.
var PER_GROUP = 8;
var RECENT_SESSIONS = 20;

// One result: its name, where it is or what it is (detail), other words
// it is found by, its icon, and what choosing it does. `typed`: shown
// only once something is typed (the long lists: every recommendation,
// every table).
function entry(kind, label, detail, run, opts) {
  opts = opts || {};
  return { kind: kind, label: label, detail: detail || "", words: opts.words || "", icon: opts.icon || "arrow-right", run: run, typed: !!opts.typed };
}

// ======================================================================
// What search can find
// ======================================================================

// Every page and segment (links.js VIEW_KEYS), named as the sidebar
// names it.
function pageEntries() {
  return VIEW_KEYS.map(function (key) {
    var view = viewFor(key);
    var intro = view.segment ? view.segment.intro : view.page.intro;
    return entry(
      "page",
      viewLabel(key),
      intro,
      function () {
        goTo(key, { focus: true });
      },
      { words: view.page.label + " " + (view.segment ? view.segment.label : ""), icon: view.page.icon }
    );
  });
}

var THEME_COMMANDS = [
  { value: "light", label: "Use the light theme", icon: "sun" },
  { value: "dark", label: "Use the dark theme", icon: "moon" },
  { value: "system", label: "Use the system's theme", icon: "system" },
];

function commandEntries() {
  var list = WINDOW_OPTIONS.map(function (opt) {
    return entry(
      "command",
      "Set window: " + opt.label,
      opt.value === state.window ? "Shown now" : "",
      function () {
        hooks.setWindow(opt.value);
      },
      { words: "window time period range", icon: "clock" }
    );
  });
  if (state.project) {
    list.push(
      entry(
        "command",
        "Show all projects",
        "Shown now: " + projectName(state.project),
        function () {
          pickProject("");
        },
        { words: "project projects filter every", icon: "folder" }
      )
    );
  }
  THEME_COMMANDS.forEach(function (theme) {
    list.push(
      entry(
        "command",
        theme.label,
        "",
        function () {
          hooks.setTheme(theme.value);
        },
        { words: "theme appearance colours colors mode", icon: theme.icon }
      )
    );
  });
  list.push(entry("command", "Show keyboard shortcuts", "", showShortcuts, { words: "keys keyboard help", icon: "info" }));
  return list;
}

function glossaryEntries() {
  var list = GLOSSARY.map(function (item) {
    return entry(
      "term",
      item[0],
      item[1],
      function () {
        goTo("glossary/terms", { params: { term: termSlug(item[0]) } });
      },
      { words: "glossary meaning definition", icon: "glossary", typed: true }
    );
  });
  COST_CARDS.forEach(function (card) {
    list.push(
      entry(
        "term",
        card.title,
        "How costs work",
        function () {
          goTo("glossary/how-costs-work", { params: { card: card.slug } });
        },
        { words: "how costs work price multiplier", icon: "glossary", typed: true }
      )
    );
  });
  return list;
}

// A recommendation's first prompt, the one its detail offers first.
function firstPrompt(rec) {
  var fixes = Array.isArray(rec.fixes) ? rec.fixes : [];
  for (var i = 0; i < fixes.length; i++) {
    if (fixes[i] && fixes[i].prompt) return fixes[i].prompt;
  }
  return "";
}

function recommendationEntries(recs) {
  var list = [];
  recs.forEach(function (rec) {
    // Ignored ones are on Actions' Ignored list only.
    if (rec.ignored) return;
    var key = rec.key || rec.id;
    var title = plainText(rec.title || rec.id);
    var words = String(rec.id || "").replace(/-/g, " ") + " " + (rec.agent_type || "");
    function open() {
      goTo("actions/recommendations", { params: { id: key } });
    }
    list.push(entry("recommendation", title, SEVERITY_LABELS[rec.severity] || "", open, { words: words, icon: "actions", typed: true }));
    var prompt = firstPrompt(rec);
    if (!prompt) return;
    list.push(
      entry(
        "command",
        "Copy prompt: " + title,
        "The prompt to give Claude",
        function () {
          copyToClipboard(prompt).then(function (ok) {
            if (ok) {
              toast("Prompt copied to the clipboard.");
              return;
            }
            // The prompt isn't on screen to copy by hand: show it.
            open();
            toast("Couldn't copy. The recommendation is open: copy its prompt from there.", { tone: "warning" });
          });
        },
        { words: "copy prompt " + words, icon: "prompt", typed: true }
      )
    );
  });
  return list;
}

function checkEntries(checks) {
  return checks.map(function (check) {
    return entry(
      "check",
      plainText(check.question || check.id),
      statusLabel(check.status),
      function () {
        goTo("actions/checks", { params: { id: check.id } });
      },
      { words: String(check.id || "").replace(/[-_]/g, " "), icon: "check", typed: true }
    );
  });
}

// The report's sections and tables. A section opens its page at its
// first table there; a table opens where it is shown, or in a panel
// when no page shows it (evidence.js, as evidence links do).
function tableEntries(report) {
  var list = [];
  ((report && report.sections) || []).forEach(function (section) {
    var tables = section.tables || [];
    var view = viewForSection(section.key);
    var first = null;
    tables.forEach(function (table) {
      var source = section.key + "." + table.name;
      var shownOn = table.dashboard !== "report" ? evidenceView(report, source) : null;
      if (!first && shownOn) first = { source: source, view: shownOn };
    });
    if (section.title) {
      list.push(
        entry(
          "table",
          plainText(section.title),
          viewLabel(first ? first.view : view),
          function () {
            if (first) goTo(first.view, { params: { t: first.source } });
            else goTo(view, { focus: true });
          },
          { words: "section", icon: "table", typed: true }
        )
      );
    }
    tables.forEach(function (table) {
      if (!table.title || table.title === section.title) return;
      var source = section.key + "." + table.name;
      var shownOn = evidenceView(report, source);
      list.push(
        entry(
          "table",
          plainText(table.title),
          shownOn ? viewLabel(shownOn) : "Opens in a panel",
          function () {
            openEvidence(report, source, null);
          },
          { words: "table " + plainText(section.title || ""), icon: "table", typed: true }
        )
      );
    });
  });
  return list;
}

// The Work habits page's cards: a playbook habit, a prompting habit and
// the rework section each open the page at that card, un-folded and
// highlighted (links.js goToHabit, ?item=<key>). A habit the window has
// no card for isn't offered.
function habitEntries(report) {
  var list = [];
  function cards(sectionKey, tableName, detail, words) {
    var section = ((report && report.sections) || []).filter(function (item) {
      return item.key === sectionKey;
    })[0];
    var table = ((section && section.tables) || []).filter(function (item) {
      return item.name === tableName;
    })[0];
    if (!table) return;
    var at = (table.columns || []).map(function (column) {
      return column.key;
    }).indexOf("habit");
    if (at < 0) return;
    var labels = table.value_labels || {};
    (table.rows || []).forEach(function (row) {
      var key = String(row[at] || "");
      if (!key) return;
      var label = plainText(String(labels[key] || key));
      list.push(
        entry(
          "habit",
          label,
          detail,
          function () {
            goToHabit(key);
          },
          { words: words + " " + key.replace(/[-_]/g, " "), icon: "check", typed: true }
        )
      );
    });
  }
  cards("habits", "habits_playbook", "Work habits: a habit worth trying", "habit playbook card");
  cards("prompting", "prompting_habits", "Work habits: how you prompt", "habit prompting card");
  var rework = ((report && report.sections) || []).filter(function (item) {
    return item.key === "rework";
  })[0];
  if (rework) {
    list.push(
      entry(
        "habit",
        "Rework after delivery",
        "Work habits: changes you asked for after Claude delivered",
        function () {
          goToHabit(REWORK_ITEM);
        },
        { words: "rework redo changes pieces of work habit", icon: "check", typed: true }
      )
    );
  }
  return list;
}

function sessionEntries(rows) {
  return rows.map(function (row) {
    var id = String(row.id || "");
    var project = row.slug ? projectName(row.slug) : "";
    var detail = [shortTs(row.last_ts), typeof row.total_cost === "number" ? moneyText(row.total_cost) : ""].filter(Boolean).join(" · ");
    return entry(
      "session",
      "Session " + id.slice(0, 8) + (project ? ", " + project : ""),
      detail,
      function () {
        openSessionDrawer(id);
      },
      {
        words: [id, row.slug, row.mode, sessionWord("mode", row.mode), row.purpose, sessionWord("purpose", row.purpose), sessionWord("entrypoint", row.entrypoint)]
          .filter(Boolean)
          .join(" "),
        icon: "clock",
        typed: true,
      }
    );
  });
}

// Every project with a session in the window: choosing one shows only
// that project (the project picker's list).
function projectEntries(slugs) {
  return (slugs || []).map(function (slug) {
    return entry(
      "project",
      projectName(slug),
      slug === state.project ? "Shown now" : "Show only this project",
      function () {
        pickProject(slug);
      },
      { words: "project filter folder " + slug, icon: "folder", typed: true }
    );
  });
}

// The window's recommendations, checks, report, recent sessions and
// projects, fetched once per window and project when search first
// opens, and cleared with the caches they come from (a new window or
// project, Redraw figures). A source that fails adds nothing; the rest
// still come.
function loadEntries() {
  var key = scopeKey();
  if (state.searchPromises[key]) return state.searchPromises[key];
  function safely(promise, build) {
    return promise.then(build, function () {
      return [];
    }).then(null, function () {
      return [];
    });
  }
  var entries = Promise.all([
    safely(loadRecommendations(), function (result) {
      var body = result.body;
      return recommendationEntries(body && body.ok === true && Array.isArray(body.data) ? body.data : []);
    }),
    safely(loadQuickActions(), function (result) {
      var body = result.body;
      return checkEntries(body && body.ok === true && body.data && Array.isArray(body.data.checks) ? body.data.checks : []);
    }),
    safely(loadReport(), function (result) {
      return tableEntries(result && result.report).concat(habitEntries(result && result.report));
    }),
    safely(fetchJson(withWindow("/api/sessions?limit=" + RECENT_SESSIONS)), function (result) {
      var body = result.body;
      return sessionEntries(body && body.ok === true && Array.isArray(body.data) ? body.data : []);
    }),
    safely(loadProjects(), projectEntries),
  ]).then(function (lists) {
    return lists.reduce(function (all, list) {
      return all.concat(list);
    }, []);
  });
  state.searchPromises[key] = entries;
  entries.then(function (list) {
    // Nothing came (the service is away): ask again next time.
    if (!list.length && state.searchPromises[key] === entries) delete state.searchPromises[key];
  });
  return entries;
}

// ======================================================================
// Matching
// ======================================================================

function normalise(text) {
  return String(text || "")
    .toLowerCase()
    .replace(/[›·:,()]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

// How well one typed word matches some text: at the start of the text
// counts most, then at the start of a word, then inside a word, then
// its letters in order with at most two gaps (so "rbld" finds
// "rebuild"). 0: no match.
function partScore(word, text) {
  var at = text.indexOf(word);
  if (at === 0) return 8;
  if (at > 0) return text.charAt(at - 1) === " " ? 6 : 4;
  if (word.length < 3) return 0;
  var i = 0;
  var gaps = 0;
  var last = -1;
  for (var j = 0; j < text.length && i < word.length; j++) {
    if (text.charAt(j) !== word.charAt(i)) continue;
    if (last !== -1 && j !== last + 1) gaps += 1;
    last = j;
    i += 1;
  }
  if (i < word.length || gaps > 2) return 0;
  return 3 - gaps;
}

// A result's score for what was typed: every word must match its name
// or its other words, and a match in the name counts more. 0: leave it
// out.
export function matchScore(query, item) {
  var words = normalise(query).split(" ").filter(Boolean);
  if (!words.length) return 1;
  var label = normalise(item.label);
  var extra = normalise(item.words + " " + item.detail);
  var total = 0;
  for (var i = 0; i < words.length; i++) {
    var inLabel = partScore(words[i], label);
    var score = inLabel ? inLabel + 2 : partScore(words[i], extra);
    if (!score) return 0;
    total += score;
  }
  if (label.indexOf(normalise(query)) === 0) total += 6;
  return total;
}

// The results to show, grouped: nothing typed lists the pages and
// commands; otherwise each group's best matches, the group with the
// best match first.
function results(query, entries) {
  var typed = normalise(query) !== "";
  var byKind = {};
  entries.forEach(function (item, index) {
    if (!typed && item.typed) return;
    var score = matchScore(query, item);
    if (!score) return;
    (byKind[item.kind] || (byKind[item.kind] = [])).push({ item: item, score: score, index: index });
  });
  var groups = [];
  GROUPS.forEach(function (group, order) {
    var found = byKind[group.kind];
    if (!found) return;
    found.sort(function (a, b) {
      return b.score - a.score || a.index - b.index;
    });
    groups.push({ label: group.label, order: order, best: found[0].score, items: typed ? found.slice(0, PER_GROUP) : found });
  });
  if (typed) {
    groups.sort(function (a, b) {
      return b.best - a.best || a.order - b.order;
    });
  }
  return groups;
}

// The name with each typed word's first match in bold.
function markedLabel(label, query) {
  var lower = String(label).toLowerCase();
  var ranges = [];
  normalise(query)
    .split(" ")
    .filter(Boolean)
    .forEach(function (word) {
      var at = lower.indexOf(word);
      if (at !== -1) ranges.push([at, at + word.length]);
    });
  ranges.sort(function (a, b) {
    return a[0] - b[0];
  });
  var nodes = [];
  var last = 0;
  ranges.forEach(function (range) {
    var start = Math.max(range[0], last);
    if (start >= range[1]) return;
    if (start > last) nodes.push(document.createTextNode(label.slice(last, start)));
    nodes.push(el("mark", { text: label.slice(start, range[1]) }));
    last = range[1];
  });
  if (last < label.length) nodes.push(document.createTextNode(label.slice(last)));
  return nodes;
}

// ======================================================================
// The search dialog
// ======================================================================

var palette = { dialog: null };

export function openPalette() {
  if (palette.dialog) return;
  var opener = document.activeElement;
  var entries = pageEntries().concat(commandEntries(), glossaryEntries());
  var shown = [];
  var active = -1;
  var waiting = true;

  var input = el("input", {
    type: "text",
    class: "palette-input",
    role: "combobox",
    "aria-expanded": "true",
    "aria-controls": "palette-results",
    "aria-autocomplete": "list",
    "aria-label": "Search",
    placeholder: "Search pages, tables, actions, terms, sessions and projects",
    autocomplete: "off",
    spellcheck: "false",
  });
  var list = el("div", { class: "palette-results", id: "palette-results", role: "listbox", "aria-label": "Results", "aria-busy": "true" });
  var status = el("p", { class: "palette-status", role: "status", "aria-live": "polite" });
  var shortcutsButton = button("Keyboard shortcuts", { variant: "link", class: "palette-keys" });
  var dialog = el("dialog", { class: "palette", "aria-label": "Search" }, [
    el("div", { class: "palette-search" }, [icon("search"), input, el("kbd", { text: "Esc" })]),
    list,
    el("div", { class: "palette-foot" }, [
      status,
      el("span", { class: "palette-hint" }, [
        el("kbd", { text: "Up" }),
        el("kbd", { text: "Down" }),
        el("span", { text: " to move, " }),
        el("kbd", { text: "Enter" }),
        el("span", { text: " to open" }),
      ]),
      shortcutsButton,
    ]),
  ]);
  palette.dialog = dialog;

  function setActive(index, scroll) {
    var options = list.querySelectorAll("[role=option]");
    if (active >= 0 && options[active]) options[active].setAttribute("aria-selected", "false");
    active = index;
    if (index < 0 || !options[index]) {
      active = -1;
      input.removeAttribute("aria-activedescendant");
      return;
    }
    options[index].setAttribute("aria-selected", "true");
    input.setAttribute("aria-activedescendant", options[index].id);
    if (scroll) options[index].scrollIntoView({ block: "nearest" });
  }

  var statusTimer = null;
  function say(text) {
    clearTimeout(statusTimer);
    statusTimer = setTimeout(function () {
      status.textContent = text;
    }, 250);
  }

  function draw() {
    var query = input.value;
    var groups = results(query, entries);
    clear(list);
    shown = [];
    groups.forEach(function (group, g) {
      var heading = el("div", { class: "palette-group-label", id: "palette-group-" + g, role: "presentation", text: group.label });
      var box = el("div", { class: "palette-group", role: "group", "aria-labelledby": heading.id }, [heading]);
      group.items.forEach(function (found) {
        var index = shown.length;
        var item = found.item;
        var option = el("div", { class: "palette-option", id: "palette-option-" + index, role: "option", "aria-selected": "false" }, [
          icon(item.icon),
          el("span", { class: "palette-option-label" }, markedLabel(item.label, query)),
          item.detail ? el("span", { class: "palette-option-detail", text: item.detail }) : null,
        ]);
        option.addEventListener("mousemove", function () {
          if (active !== index) setActive(index, false);
        });
        option.addEventListener("click", function () {
          choose(index);
        });
        box.appendChild(option);
        shown.push(item);
      });
      list.appendChild(box);
    });
    active = -1;
    setActive(shown.length ? 0 : -1, false);
    list.scrollTop = 0;
    var typed = normalise(query) !== "";
    if (!shown.length) {
      list.appendChild(
        el("p", {
          class: "palette-empty",
          text: waiting ? "Searching…" : "Nothing matches “" + query.trim() + "”. Try fewer words, or another name for it.",
        })
      );
      say(waiting ? "" : "No results");
    } else if (waiting && typed) say(shown.length + (shown.length === 1 ? " result" : " results") + " so far. Still loading tables, actions, sessions and projects.");
    else say(shown.length + (shown.length === 1 ? " result" : " results"));
  }

  function choose(index) {
    var item = shown[index];
    if (!item) return;
    close(false);
    item.run();
  }

  // returnFocus false: the chosen result moves focus itself.
  function close(returnFocus) {
    if (!palette.dialog) return;
    palette.dialog = null;
    clearTimeout(statusTimer);
    dialog.close();
    dialog.remove();
    if (returnFocus !== false && opener && opener.isConnected && opener.focus) opener.focus();
    else if (opener && opener.isConnected && opener.focus) opener.focus({ preventScroll: true });
  }
  palette.close = close;

  input.addEventListener("input", draw);
  input.addEventListener("keydown", function (event) {
    if (event.isComposing) return;
    var count = shown.length;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (!count) return;
      var step = event.key === "ArrowDown" ? 1 : -1;
      setActive(active === -1 ? 0 : (active + step + count) % count, true);
    } else if (event.key === "PageDown" || event.key === "PageUp") {
      event.preventDefault();
      if (!count) return;
      var jump = event.key === "PageDown" ? 8 : -8;
      setActive(Math.max(0, Math.min(count - 1, (active === -1 ? 0 : active) + jump)), true);
    } else if (event.key === "Enter") {
      event.preventDefault();
      if (active !== -1) choose(active);
    }
  });
  shortcutsButton.addEventListener("click", function () {
    close(true);
    showShortcuts();
  });
  dialog.addEventListener("cancel", function (event) {
    event.preventDefault();
    close(true);
  });
  // A click outside the panel (on the backdrop) closes it.
  dialog.addEventListener("click", function (event) {
    if (event.target !== dialog) return;
    var rect = dialog.getBoundingClientRect();
    var inside = event.clientX >= rect.left && event.clientX <= rect.right && event.clientY >= rect.top && event.clientY <= rect.bottom;
    if (!inside) close(true);
  });

  document.body.appendChild(dialog);
  dialog.showModal();
  input.focus();
  requestAnimationFrame(function () {
    dialog.classList.add("is-open");
  });
  draw();
  loadEntries().then(function (more) {
    waiting = false;
    if (palette.dialog !== dialog) return;
    list.removeAttribute("aria-busy");
    entries = entries.concat(more);
    var keep = active !== -1 && shown[active] ? shown[active] : null;
    draw();
    // What the arrow keys had picked stays picked if it's still there.
    if (keep && normalise(input.value) === "") {
      var at = shown.indexOf(keep);
      if (at !== -1) setActive(at, true);
    }
  });
}

export function closePalette() {
  if (palette.dialog && palette.close) palette.close(true);
}

// ======================================================================
// Keyboard shortcuts
// ======================================================================

// G then a letter opens a page. E for Agents & context, U for Setup:
// their first letters are taken.
export var GO_KEYS = { o: "overview", y: "changes", a: "actions", s: "spend", c: "cache", e: "agents", h: "habits", u: "setup" };

var SHORTCUTS = [
  {
    heading: "Anywhere",
    rows: [
      [["Ctrl", "K"], "Search pages, tables, actions, terms, sessions and projects"],
      [["?"], "Show these shortcuts"],
      [["Esc"], "Close a panel, menu or search"],
    ],
  },
  {
    heading: "Go to a page: G, then",
    rows: Object.keys(GO_KEYS).map(function (letter) {
      var page = findPage(GO_KEYS[letter]);
      return [[letter.toUpperCase()], page ? page.label : GO_KEYS[letter]];
    }),
  },
  {
    heading: "On a page",
    rows: [
      [["[", "]"], "The page's previous or next section"],
      [["J", "K"], "The next or previous item in its list, such as a recommendation or a session"],
      [["Enter"], "Open the item"],
      [["?"], "On a column heading: what the column means"],
    ],
  },
];

var sheet = { dialog: null };

export function showShortcuts() {
  if (sheet.dialog) return;
  var opener = document.activeElement;
  var dialog = el("dialog", { class: "shortcut-sheet", "aria-labelledby": "shortcut-title" });
  dialog.appendChild(el("h2", { id: "shortcut-title", text: "Keyboard shortcuts" }));
  SHORTCUTS.forEach(function (group) {
    dialog.appendChild(el("h3", { text: group.heading }));
    var list = el("dl", { class: "shortcut-list" });
    group.rows.forEach(function (row) {
      var keys = el("dt");
      row[0].forEach(function (key, i) {
        if (i) keys.appendChild(el("span", { class: "shortcut-sep", text: row[0][0] === "Ctrl" ? "+" : "or" }));
        keys.appendChild(el("kbd", { text: key }));
      });
      list.appendChild(el("div", { class: "shortcut-row" }, [keys, el("dd", { text: row[1] })]));
    });
    dialog.appendChild(list);
  });
  function close() {
    if (!sheet.dialog) return;
    sheet.dialog = null;
    dialog.close();
    dialog.remove();
    if (opener && opener.isConnected && opener.focus) opener.focus();
  }
  var done = button("Close", { variant: "quiet", action: close });
  dialog.appendChild(el("div", { class: "dialog-actions" }, [done]));
  dialog.addEventListener("cancel", function (event) {
    event.preventDefault();
    close();
  });
  dialog.addEventListener("click", function (event) {
    if (event.target !== dialog) return;
    var rect = dialog.getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) close();
  });
  sheet.dialog = dialog;
  document.body.appendChild(dialog);
  dialog.showModal();
  done.focus();
}

// A key pressed while typing, or while a panel, menu or popover is open,
// belongs to that; shortcuts leave it alone.
function typing(target) {
  return !!(target && target.closest && target.closest("input, textarea, select, [contenteditable]:not([contenteditable='false'])"));
}

function overlayOpen() {
  if (document.querySelector("dialog[open]")) return true;
  if (document.querySelector(".page-controls .menu:not([hidden])")) return true;
  try {
    return !!document.querySelector(":popover-open");
  } catch (error) {
    return false;
  }
}

function visibleView() {
  return document.querySelector("#views > .view:not([hidden])");
}

// [ and ]: the page's previous or next segment.
function stepSegment(step) {
  var view = viewFor(state.view);
  if (!view || !view.page.segments) return;
  var segments = view.page.segments;
  var at = segments.indexOf(view.segment);
  var next = segments[(at + step + segments.length) % segments.length];
  goTo(view.page.id + "/" + next.id);
}

// J and K: an inbox's items (Actions), or the rows of the view's first
// list that opens its rows (Sessions, CLAUDE.md files).
function stepList(step) {
  var panel = visibleView();
  if (!panel) return;
  var items = Array.prototype.slice.call(panel.querySelectorAll(".inbox-item"));
  if (items.length) {
    var current = -1;
    items.forEach(function (link, i) {
      if (link.getAttribute("aria-current") === "true") current = i;
    });
    var next = items[current === -1 ? 0 : Math.max(0, Math.min(items.length - 1, current + step))];
    if (next.getAttribute("aria-current") !== "true") next.click();
    next.focus();
    return;
  }
  var focused = document.activeElement;
  var table = focused && focused.tagName === "TR" && panel.contains(focused) ? focused.closest("table") : null;
  if (!table) {
    var firstRow = panel.querySelector("table.data-grid tbody tr[tabindex='0']");
    table = firstRow ? firstRow.closest("table") : null;
  }
  if (!table) return;
  var rows = Array.prototype.slice.call(table.querySelectorAll("tbody tr[tabindex='0']"));
  var at = rows.indexOf(focused);
  var row = rows[at === -1 ? 0 : Math.max(0, Math.min(rows.length - 1, at + step))];
  if (row) row.focus();
}

var go = { timer: null };

function onKeydown(event) {
  if (event.isComposing) return;
  var key = event.key;
  if ((event.ctrlKey || event.metaKey) && !event.altKey && !event.shiftKey && (key === "k" || key === "K")) {
    if (palette.dialog) {
      event.preventDefault();
      closePalette();
      return;
    }
    if (overlayOpen()) return;
    event.preventDefault();
    openPalette();
    return;
  }
  if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey) return;
  if (typing(event.target) || overlayOpen()) {
    go.timer = null;
    return;
  }
  var lower = key.length === 1 ? key.toLowerCase() : key;
  if (go.timer) {
    clearTimeout(go.timer);
    go.timer = null;
    if (GO_KEYS[lower]) {
      event.preventDefault();
      goTo(GO_KEYS[lower], { focus: true });
      return;
    }
  }
  if (lower === "g" && !event.shiftKey) {
    go.timer = setTimeout(function () {
      go.timer = null;
    }, 1500);
    return;
  }
  var action = null;
  if (key === "?") action = showShortcuts;
  else if (key === "[") action = function () { stepSegment(-1); };
  else if (key === "]") action = function () { stepSegment(1); };
  else if (lower === "j" && !event.shiftKey) action = function () { stepList(1); };
  else if (lower === "k" && !event.shiftKey) action = function () { stepList(-1); };
  if (!action) return;
  event.preventDefault();
  action();
}

// The Search button in the page header, and the keys. opts: setWindow,
// setTheme (app.js).
export function initPalette(opts) {
  hooks.setWindow = opts.setWindow;
  hooks.setTheme = opts.setTheme;
  var controls = document.querySelector(".page-controls");
  var search = el("button", { type: "button", class: "control-button search-button", id: "search-button", "aria-label": "Search", "aria-keyshortcuts": "Control+K" }, [
    icon("search"),
    el("span", { class: "search-label", "aria-hidden": "true", text: "Search" }),
    el("kbd", { "aria-hidden": "true", text: "Ctrl K" }),
  ]);
  search.addEventListener("click", openPalette);
  if (controls) controls.insertBefore(search, controls.firstChild);
  document.addEventListener("keydown", onKeydown);
}

/* claudeglass service UI: page-habits.js
 *
 * The Work habits page.
 */

import { clear, el, state } from "./core.js";
import { formatCell, moneyParts, moneyText } from "./format.js";
import { findSection, loadReport } from "./api.js";
import { chip, codeBlockWithCopy, emptyState, errorNotice, helpButton, loadingNode, prose, tile, tileRow } from "./ui.js";
import { headRow, notesList, renderPlacedTables } from "./grid.js";
import { pageLink, viewIntro } from "./links.js";
import { habitSparkline } from "./charts-types.js";

// ======================================================================
// Work habits: the habits section's "This week" digest as tiles,
// the playbook as cards with a by-week sparkline and the example to
// copy, brief templates with Copy buttons, then its other tables; then
// How you prompt (the prompting section) as a card per habit.
// ======================================================================

export function renderHabits(panel) {
  clear(panel);
  viewIntro(panel, "habits");
  var container = el("div", { id: "habits-sections" });
  panel.appendChild(container);
  container.appendChild(loadingNode("Loading your work habits", "tiles"));
  loadReport().then(function (result) {
    clear(container);
    if (result.error) {
      container.appendChild(errorNotice(result.error));
      return;
    }
    var section = findSection(result.report, "habits");
    var prompting = findSection(result.report, "prompting");
    if (!section && !prompting) {
      container.appendChild(
        emptyState(
          "No work-habit figures for this window: none of its sessions had enough messages to compare.",
          null,
          "Pick a longer window, or turn on metrics capture on the Capture page for richer figures."
        )
      );
      return;
    }
    if (section) {
      // The section's "How to read this" sits at the end of the page's intro.
      var intro = panel.querySelector(".view-intro");
      var sectionHelp = helpButton(section.help, section.title || "Work habits");
      if (intro && sectionHelp) intro.appendChild(sectionHelp);
      renderHabitsSection(section, container);
    }
    if (prompting) renderPromptingSection(prompting, container);
  });
}

// How you prompt: a card per habit seen (how often, what it cost, its
// trend by week, what to try instead), then the tips Claude showed.
function renderPromptingSection(section, container) {
  var tables = section.tables || [];
  var habitsTable = tables.filter(function (table) {
    return table.name === "prompting_habits";
  })[0];
  var block = el("section", { class: "report-section", "data-section": "prompting" });
  block.appendChild(headRow(el("h2", { class: "section-title", text: section.title }), section.help, section.title));
  if (section.intro) block.appendChild(el("p", { class: "section-intro", text: section.intro }));
  container.appendChild(block);
  var rows = habitsTable ? tableRowsAsObjects(habitsTable) : [];
  if (!rows.length) {
    block.appendChild(
      emptyState(
        "None of these habits turned up in this window.",
        null,
        "Coaching notes warn you about small requests, huge pastes and checks on a background task as they happen."
      )
    );
  } else {
    var cards = el("div", { class: "habit-cards" });
    rows.forEach(function (row) {
      cards.appendChild(promptingCard(habitsTable, row));
    });
    block.appendChild(cards);
  }
  var rest = tables.filter(function (table) {
    return table.name !== "prompting_habits";
  });
  renderPlacedTables(block, rest, state.currency, "habits");
}

function promptingCard(table, row) {
  var title = String(labelFor(table, row.habit));
  var card = el("article", { class: "habit-card", "data-habit": row.habit });
  card.appendChild(el("div", { class: "card-head" }, [el("h3", { text: title })]));
  var cost = row.cost === null || row.cost === undefined ? "Not priced" : moneyText(row.cost, { prefix: "About " });
  card.appendChild(el("p", { class: "habit-saving", text: cost }));
  var seen = new Set();
  if (row.try) {
    card.appendChild(el("p", { class: "habit-try", text: "Try instead:" }));
    card.appendChild(el("p", null, prose(row.try, seen)));
  }
  var meta = [
    "Seen " + formatCell(row.times, "int", state.currency),
    // One decimal is plenty for a rate per 100 messages: "2.6", "12".
    Number(row.per_100).toFixed(1).replace(/\.0$/, "") + " per 100 messages",
    "trend " + String(labelFor(table, row.trend) || "").toLowerCase(),
  ];
  var metaLine = el("p", { class: "profile-card-meta", text: meta.join(" · ") });
  var spark = habitSparkline(row.weeks, "By week, " + labelFor(table, row.trend) + ": " + row.weeks);
  if (spark) metaLine.appendChild(spark);
  card.appendChild(metaLine);
  if (row.basis) card.appendChild(el("p", { class: "cell-hint" }, prose("Cost worked out from " + row.basis + ".", seen)));
  return card;
}

function tableRowsAsObjects(table) {
  return (table.rows || []).map(function (row) {
    var out = {};
    (table.columns || []).forEach(function (column, i) {
      out[column.key] = row[i];
    });
    return out;
  });
}

function labelFor(table, value) {
  var labels = table.value_labels || {};
  return typeof value === "string" && labels[value] ? labels[value] : value;
}

function renderHabitsSection(section, container) {
  var tables = section.tables || [];
  var byName = {};
  tables.forEach(function (table) {
    byName[table.name] = table;
  });
  // The habits the playbook shows as cards, which the digest leaves to it.
  var carded = byName.habits_playbook
    ? tableRowsAsObjects(byName.habits_playbook)
        .slice(0, PLAYBOOK_CARD_LIMIT)
        .map(function (row) {
          return String(labelFor(byName.habits_playbook, row.habit)).trim().toLowerCase();
        })
    : [];
  if (byName.habits_digest) renderHabitsDigest(byName.habits_digest, container, carded);
  if (byName.habits_playbook) renderHabitsPlaybook(byName.habits_playbook, container);
  if (byName.habits_brief_templates) renderBriefTemplates(byName.habits_brief_templates, container);
  var rest = tables.filter(function (table) {
    return ["habits_digest", "habits_playbook", "habits_brief_templates", "habits_setups"].indexOf(table.name) === -1;
  });
  renderPlacedTables(container, rest, state.currency, "habits");
  if (section.notes && section.notes.length) container.appendChild(notesList(section.notes, new Set()));
}

// A block of the page: an h2 with its "How to read this", then the body.
function habitsBlock(table, container) {
  var block = el("section", { class: "report-section", "data-table": table.name });
  block.appendChild(headRow(el("h2", { class: "section-title", text: table.title }), table.help, table.title));
  container.appendChild(block);
  return block;
}

// carded: the habits the playbook below shows as cards (lower case). The
// digest doesn't repeat them: it keeps what the cards don't say ("Already
// saving"), and says nothing when that is none.
function renderHabitsDigest(table, container, carded) {
  var rows = tableRowsAsObjects(table);
  var own = rows.filter(function (row) {
    return !row.what || (carded || []).indexOf(String(row.what).trim().toLowerCase()) === -1;
  });
  if (rows.length && !own.length) return;
  var block = habitsBlock(table, container);
  if (!rows.length) {
    block.appendChild(
      emptyState("Nothing to show for this window yet: it needs a few prompt cycles to compare.", null, "Pick a longer window to include more sessions.")
    );
    return;
  }
  var tiles = [];
  own.forEach(function (row) {
    var kind = (table.row_kinds || {})[row.item] || "str";
    // UX-1: a money card follows the billing mode (moneyParts mirrors
    // Units.money): the figure at the tile's size, its unit ("of your
    // weekly usage limit", or "list-price" with no share to show) on the
    // line under it, and the list-price equivalent beneath.
    var amount = kind === "money" ? moneyParts(Number(row.value)) : null;
    tiles.push(
      tile({
        label: labelFor(table, row.item),
        value: amount ? amount.value : formatCell(row.value, kind, state.currency),
        unit: amount ? amount.unit || null : null,
        hint: amount ? amount.secondary || null : null,
        caption: row.what || null,
        note: row.detail || null,
      })
    );
  });
  block.appendChild(tileRow(tiles, { class: "metric-tiles-fit habits-digest" }));
}

//: UX-4/7: habits shown as cards before the rest collapse into <details>
//: (F3: "uncapped playbook" -- every habit got a card, largest and
//: smallest saving alike, crowding out the ones worth trying first).
var PLAYBOOK_CARD_LIMIT = 5;

function renderHabitsPlaybook(table, container) {
  var block = habitsBlock(table, container);
  var rows = tableRowsAsObjects(table);
  if (!rows.length) {
    block.appendChild(
      emptyState("No habit stood out in this window: your sessions didn't repeat a pattern worth changing.", null, "Check again after a busier week.")
    );
    return;
  }
  var featured = rows.slice(0, PLAYBOOK_CARD_LIMIT);
  var rest = rows.slice(PLAYBOOK_CARD_LIMIT);
  var cards = el("div", { class: "habit-cards" });
  appendHabitCards(table, featured, cards);
  block.appendChild(cards);
  if (rest.length) {
    var more = el("details", { class: "disclosure" });
    more.appendChild(el("summary", { text: rest.length + " more habit" + (rest.length === 1 ? "" : "s") + " worth trying" }));
    var restCards = el("div", { class: "habit-cards" });
    appendHabitCards(table, rest, restCards);
    more.appendChild(restCards);
    block.appendChild(more);
  }
}

function appendHabitCards(table, rows, cards) {
  rows.forEach(function (row) {
    var card = el("article", { class: "habit-card" });
    var head = el("div", { class: "card-head" });
    head.appendChild(el("h3", { text: labelFor(table, row.habit) }));
    if (row.theme) head.appendChild(chip(String(labelFor(table, row.theme)), { class: "habit-theme" }));
    card.appendChild(head);
    // UX-1/UX-2: routed through moneyText so a subscription reads "about
    // X% of your weekly usage limit" instead of a bare "$" figure; "a
    // week" is dropped under a subscription since the primary text
    // already says "...weekly usage limit" (finding F3's "weekly ...
    // a week" doubling, mirrored client-side -- see capture_view.py's
    // _roi for the same call).
    // UX-3: a habit apply_covered_by (habits.py) matched to a rule that
    // fired shows no saving of its own -- it would double-count the
    // rule's -- and names the rule instead.
    if (row.covered_by) {
      // Linked to the recommendation itself when the rule is named
      // (covered_by_rule); its title alone otherwise.
      var rule = row.covered_by_rule
        ? pageLink("actions/recommendations", row.covered_by, { id: row.covered_by_rule })
        : el("span", { text: "“" + row.covered_by + "”" });
      card.appendChild(el("p", { class: "habit-saving" }, [el("span", { text: "Covered by the recommendation " }), rule, el("span", { text: "." })]));
    } else {
      var savingPeriod = (state.units || {}).mode === "subscription" ? "" : "a week";
      var saving = row.saving === null || row.saving === undefined
        ? "Saving not priced"
        : moneyText(row.saving, { period: savingPeriod, prefix: "About " });
      card.appendChild(el("p", { class: "habit-saving", text: saving }));
    }
    // Each glossary term is explained once per card: its first use.
    var seen = new Set();
    if (row.evidence) card.appendChild(el("p", null, prose(row.evidence, seen)));
    if (row.example) {
      card.appendChild(el("p", { class: "habit-try", text: "Try:" }));
      card.appendChild(codeBlockWithCopy(row.example, "Example", String(labelFor(table, row.habit))));
    }
    var meta = [
      "Seen " + formatCell(row.n, "int", state.currency),
      String(labelFor(table, row.source) || ""),
      "confidence " + String(labelFor(table, row.confidence) || "").toLowerCase(),
      "trend " + String(labelFor(table, row.trend) || "").toLowerCase(),
    ].filter(function (part) {
      return part && part.trim();
    });
    var metaLine = el("p", { class: "profile-card-meta", text: meta.join(" · ") });
    var spark = habitSparkline(row.weeks, "By week, " + labelFor(table, row.trend) + ": " + row.weeks);
    if (spark) metaLine.appendChild(spark);
    card.appendChild(metaLine);
    // UX-3: basis explains a saving figure that isn't shown once covered.
    if (row.basis && !row.covered_by) card.appendChild(el("p", { class: "cell-hint" }, prose("How the saving is worked out: " + row.basis + ".", seen)));
    // UX-8: same where/trade-off/undo shape as a recommendation's fix
    // explainer (page-actions.js's renderFix), collapsed by default so it doesn't
    // crowd out the habit itself.
    if (row.where || row.trade_off || row.how_to_undo) {
      var explainer = el("details", { class: "disclosure" });
      explainer.appendChild(el("summary", { text: "Where, trade-off and how to undo it" }));
      var list = el("dl", { class: "fix-explainer" });
      [["Where", row.where], ["Trade-off", row.trade_off], ["How to undo it", row.how_to_undo]].forEach(function (pair) {
        if (!pair[1]) return;
        list.appendChild(el("dt", { text: pair[0] }));
        list.appendChild(el("dd", null, prose(pair[1], seen)));
      });
      explainer.appendChild(list);
      card.appendChild(explainer);
    }
    cards.appendChild(card);
  });
}

function renderBriefTemplates(table, container) {
  var block = habitsBlock(table, container);
  var cards = el("div", { class: "habit-cards" });
  tableRowsAsObjects(table).forEach(function (row) {
    var card = el("article", { class: "habit-card" });
    card.appendChild(el("div", { class: "card-head" }, [el("h3", { text: labelFor(table, row.task) })]));
    if (row.why) card.appendChild(el("p", { class: "profile-card-meta" }, prose(row.why, new Set())));
    card.appendChild(codeBlockWithCopy(row.template || "", "Template", String(labelFor(table, row.task))));
    cards.appendChild(card);
  });
  block.appendChild(cards);
}

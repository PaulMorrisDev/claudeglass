/* claudeglass service UI: page-habits.js
 *
 * The Work habits page.
 */

import { clear, el, onParams, state } from "./core.js";
import { formatCell, money, moneyParts, moneyText } from "./format.js";
import { findSection, loadReport } from "./api.js";
import { chip, codeBlockWithCopy, emptyState, errorNotice, helpButton, loadingNode, prose, tile, tileRow } from "./ui.js";
import { cardRating, headRow, notesList, pulseNode, renderPlacedTables, renderTable } from "./grid.js";
import { habitItem, pageLink, REWORK_ITEM, viewIntro } from "./links.js";
import { habitSparkline } from "./charts-types.js";

// ======================================================================
// Work habits: the habits section's "This week" digest as tiles,
// the playbook as cards with a by-week sparkline and the example to
// copy, then Rework after delivery (the rework section: the headline,
// a card per cause, the mistakes Claude admitted, the weeks as bars and
// the rework by level), brief templates with Copy buttons, and the
// habits section's other tables; then How you prompt (the prompting
// section) as a card per habit. A card is addressed by its key
// (#/habits?item=<key>, links.js's habitLink): the page scrolls to it, opens
// it out of a fold and highlights it.
// ======================================================================

export function renderHabits(panel) {
  clear(panel);
  viewIntro(panel, "habits");
  var container = el("div", { id: "habits-sections" });
  panel.appendChild(container);
  container.appendChild(loadingNode("Loading your work habits", "tiles"));
  var drawn = loadReport().then(function (result) {
    clear(container);
    if (result.error) {
      container.appendChild(errorNotice(result.error));
      return;
    }
    var section = findSection(result.report, "habits");
    var prompting = findSection(result.report, "prompting");
    var rework = findSection(result.report, "rework");
    if (!section && !prompting && !rework) {
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
      renderHabitsSection(section, container, rework);
    } else if (rework) {
      renderRework(rework, container);
    }
    if (prompting) renderPromptingSection(prompting, container);
  });

  // ?item=<key>: the card for that habit, or the rework section, once the
  // page is drawn. An item this window has no card for leaves the page as it is.
  onParams("habits", function (params) {
    var item = habitItem(params);
    if (!item) return;
    drawn.then(function () {
      showHabitItem(container, item);
    });
  });
}

// Bring the card for ``item`` into view and pulse it. A card folded under
// "more habits" (or "more causes") opens its fold first, so it is on screen
// when the page scrolls to it.
function showHabitItem(container, item) {
  var node = container.querySelector('[data-item="' + CSS.escape(item) + '"]');
  if (!node) return;
  var fold = node.parentElement ? node.parentElement.closest("details") : null;
  while (fold) {
    fold.open = true;
    fold = fold.parentElement ? fold.parentElement.closest("details") : null;
  }
  pulseNode(node, "block-target");
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
  var card = el("article", { class: "habit-card", "data-habit": row.habit, "data-item": row.habit });
  card.appendChild(el("div", { class: "card-head" }, [el("h3", { text: title })]));
  // A cost is a total over the report's window (row.period), never a bare
  // zero: a habit with nothing priced says so.
  var cost = typeof row.cost === "number" && row.cost > 0 ? periodMoney(row.cost, row.period, "About ") : "Not priced";
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
  var spark = habitSparkline(row.weeks, "By week, " + labelFor(table, row.trend) + ": " + weeksLabel(row.weeks));
  if (spark) metaLine.appendChild(spark);
  card.appendChild(metaLine);
  if (row.basis) card.appendChild(el("p", { class: "cell-hint" }, prose("Cost worked out from " + row.basis + ".", seen)));
  card.appendChild(cardRating("tip", String(row.habit)));
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

// The by-week string in words: a "-" is a week that can't be measured (too
// few messages, or before capture was turned on), read out as an en dash.
function weeksLabel(weeks) {
  return String(weeks || "")
    .split(" ")
    .map(function (part) {
      return part === "-" ? "–" : part;
    })
    .join(" ");
}

// An amount with the period it covers: "$1.40 a week" at list price. On a
// plan the share is already of a weekly limit, so the period goes with the
// list-price equivalent, as moneyParts does, rather than doubling the
// "weekly" in "about 3% of your weekly usage limit a week".
function periodMoney(usd, period, prefix) {
  var plan = (state.units || {}).mode === "subscription";
  var amount = plan && period ? money(usd) : null;
  if (!amount) return moneyText(usd, { period: period, prefix: prefix });
  var phrase = amount.secondary
    ? amount.primary + " (" + amount.secondary + " " + period + ")"
    : amount.primary + " " + period;
  return /^about /i.test(phrase) ? phrase : (prefix || "") + phrase;
}

function renderHabitsSection(section, container, rework) {
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
  // After the top habit cards: what the rework cost and why is the next
  // thing to read once you have seen which habits to change.
  if (rework) renderRework(rework, container);
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

// What a money tile in the digest covers, so no figure is bare: a habit's
// saving is a week's worth, the cost of a goal met is one piece of work's.
var DIGEST_PERIODS = {
  top_1: "a week",
  top_2: "a week",
  top_3: "a week",
  adopted: "a week",
  cost_per_met: "per piece of work",
};

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
    var period = DIGEST_PERIODS[row.item] || "";
    var amount = kind === "money" ? moneyParts(Number(row.value), { period: period }) : null;
    // The period rides with the unit when nothing under the figure carries
    // it: at list price, or with no usage-limit share to show.
    var unit = amount ? amount.unit || "" : "";
    if (amount && !amount.secondary && period) unit = unit ? unit + " " + period : period;
    tiles.push(
      tile({
        label: labelFor(table, row.item),
        value: amount ? amount.value : formatCell(row.value, kind, state.currency),
        unit: unit || null,
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
    var card = el("article", { class: "habit-card", "data-item": row.habit });
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
      var saving = typeof row.saving === "number" && row.saving > 0
        ? periodMoney(row.saving, "a week", "About ")
        : "Saving not priced";
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
    var spark = habitSparkline(row.weeks, "By week, " + labelFor(table, row.trend) + ": " + weeksLabel(row.weeks));
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
    card.appendChild(cardRating("habit", String(row.habit)));
    cards.appendChild(card);
  });
}

// ======================================================================
// Rework after delivery (the rework section, built by rework.py): the
// headline in words, a card per cause with where it came from, what to
// try and a line to copy, the mistakes Claude admitted, the weeks as
// bars and the rework per request by how hard the work was. The
// sentences are rework.py's; the amounts beside them are formatted here
// (moneyText), so each follows the billing mode and carries its period.
// ======================================================================

//: Cause cards shown before the rest fold into <details>, as the playbook's.
var CAUSE_CARD_LIMIT = 5;

//: A week's bar is the share of its pieces that needed changes; it has
//: one only with this many that did (rework.py's MIN_WEEK_REWORKED).
var WEEK_MIN_REWORKED = 5;

function renderRework(section, container) {
  var byName = {};
  (section.tables || []).forEach(function (table) {
    byName[table.name] = table;
  });
  var headlineTable = byName.rework_headline;
  var headline = headlineTable ? tableRowsAsObjects(headlineTable) : [];
  var title = (headlineTable && headlineTable.title) || section.title || "Rework after delivery";
  var block = el("section", { class: "report-section", "data-section": "rework", "data-item": REWORK_ITEM });
  block.appendChild(headRow(el("h2", { class: "section-title", text: title }), section.help || (headlineTable && headlineTable.help), section.title || title));
  if (section.intro) block.appendChild(el("p", { class: "section-intro" }, prose(section.intro, new Set())));
  container.appendChild(block);
  if (!headline.length) {
    block.appendChild(
      emptyState(
        "No piece of work in this window has changed files yet, so there is no rework to count.",
        null,
        "Pick a longer window to include more sessions."
      )
    );
    return;
  }
  var period = headline[0].period || "";
  renderReworkHeadline(headline, block);
  if (byName.rework_causes) renderReworkCauses(byName.rework_causes, period, container);
  if (byName.rework_admitted) renderReworkAdmitted(byName.rework_admitted, container);
  if (byName.rework_by_week) renderReworkWeeks(byName.rework_by_week, container);
  if (byName.rework_by_level) renderReworkLevels(byName.rework_by_level, container);
  if (section.notes && section.notes.length) container.appendChild(notesList(section.notes, new Set()));
}

// Tiles for the figures, then rework.py's sentences as they are: the
// pieces, the sessions we couldn't cut into pieces, the share we couldn't
// tell the cause of, and the messages sent while background work ran left
// out of the count.
function renderReworkHeadline(rows, block) {
  var byItem = {};
  rows.forEach(function (row) {
    byItem[row.item] = row;
  });
  var seen = new Set();
  var lead = byItem.pieces || byItem.requests;
  var amount = Number(lead.cost) > 0 ? moneyParts(Number(lead.cost)) : null;
  block.appendChild(
    tileRow(
      [
        tile({
          label: byItem.pieces ? "Pieces needing changes" : "Requests needing changes",
          value: formatCell(lead.count, "int", state.currency) + " of " + formatCell(lead.total, "int", state.currency),
          caption: formatCell(lead.share, "pct", state.currency) + (byItem.pieces ? " of your pieces of work" : " of requests"),
        }),
        tile({
          label: "What the rework cost",
          value: amount ? amount.value : "Not priced",
          unit: amount ? amount.unit || null : null,
          hint: amount ? amount.secondary || null : null,
          caption: lead.period || null,
        }),
      ],
      { class: "metric-tiles-fit" }
    )
  );
  ["pieces", "requests", "unknown", "asides"].forEach(function (item) {
    if (byItem[item]) block.appendChild(el("p", { class: item === "unknown" || item === "asides" ? "cell-hint" : null }, prose(byItem[item].text, seen)));
  });
}

function renderReworkCauses(table, period, container) {
  var rows = tableRowsAsObjects(table);
  if (!rows.length) return;
  var block = habitsBlock(table, container);
  // A cause that came from several places has one Try line and one line
  // to copy: on the first card it has, not repeated on each.
  var told = new Set();
  var featured = rows.slice(0, CAUSE_CARD_LIMIT);
  var rest = rows.slice(CAUSE_CARD_LIMIT);
  var cards = el("div", { class: "habit-cards" });
  featured.forEach(function (row) {
    cards.appendChild(reworkCauseCard(table, row, period, told));
  });
  block.appendChild(cards);
  if (rest.length) {
    var more = el("details", { class: "disclosure" });
    more.appendChild(el("summary", { text: rest.length + " more cause" + (rest.length === 1 ? "" : "s") }));
    var restCards = el("div", { class: "habit-cards" });
    rest.forEach(function (row) {
      restCards.appendChild(reworkCauseCard(table, row, period, told));
    });
    more.appendChild(restCards);
    block.appendChild(more);
  }
}

function reworkCauseCard(table, row, period, told) {
  var title = String(labelFor(table, row.cause));
  var card = el("article", { class: "habit-card", "data-cause": row.cause, "data-source": row.source });
  var head = el("div", { class: "card-head" }, [el("h3", { text: title })]);
  head.appendChild(chip(String(labelFor(table, row.source)), { class: "habit-theme" }));
  card.appendChild(head);
  var cost = Number(row.cost) > 0 ? moneyText(row.cost, { period: period, prefix: "That rework cost " }) + "." : "That rework was not priced.";
  card.appendChild(el("p", { class: "habit-saving", text: cost }));
  var seen = new Set();
  if (row.detail) card.appendChild(el("p", null, prose(row.detail, seen)));
  if (row.try && !told.has(row.cause)) {
    told.add(row.cause);
    card.appendChild(el("p", { class: "habit-try", text: "Try:" }));
    card.appendChild(el("p", null, prose(row.try, seen)));
    if (row.paste) card.appendChild(codeBlockWithCopy(row.paste, "Line", title));
  }
  var meta = [formatCell(row.cycles, "int", state.currency) + " follow-ups"];
  if (row.share !== null && row.share !== undefined) meta.push(formatCell(row.share, "pct", state.currency) + " of the rework");
  if (row.tokens) meta.push(formatCell(row.tokens, "tokens", state.currency) + " tokens");
  card.appendChild(el("p", { class: "profile-card-meta", text: meta.join(" · ") }));
  return card;
}

// The mistakes Claude owned up to: rework.py's sentence, the change to
// make (from where you said Claude missed it) with a line to copy, and
// the replies that only read like an admission, said apart.
function renderReworkAdmitted(table, container) {
  var rows = tableRowsAsObjects(table);
  var block = habitsBlock(table, container);
  var byItem = {};
  rows.forEach(function (row) {
    byItem[row.item] = row;
  });
  if (!rows.length) {
    block.appendChild(
      emptyState(
        "Claude admitted no mistakes in this window.",
        null,
        "Replies tagged by metrics capture are where they come from: turn it on from the Capture page."
      )
    );
    return;
  }
  var seen = new Set();
  var said = byItem.admitted;
  if (said) {
    block.appendChild(el("p", null, prose(said.text, seen)));
    if (said.fix) {
      block.appendChild(el("p", { class: "habit-try", text: "What to change:" }));
      block.appendChild(el("p", null, prose(said.fix, seen)));
    }
    if (said.paste) block.appendChild(codeBlockWithCopy(said.paste, "Line", table.title));
  }
  if (byItem.possible) block.appendChild(el("p", { class: "cell-hint" }, prose(byItem.possible.text, seen)));
}

// By week: bars for the share of pieces that needed changes and for the
// mistakes you caught per piece, a dash for a week with too few to say,
// and the figures behind them folded under.
function renderReworkWeeks(table, container) {
  var rows = tableRowsAsObjects(table);
  if (!rows.length) return;
  var block = habitsBlock(table, container);
  var shares = rows
    .map(function (row) {
      return row.share === null || row.share === undefined ? "-" : String(Math.max(0, Math.min(100, Math.round(Number(row.share)))));
    })
    .join(" ");
  // Caught per piece is 0 to 1 and over: the bar is full at one.
  var caught = rows
    .map(function (row) {
      return row.caught_per_piece === null || row.caught_per_piece === undefined
        ? "-"
        : String(Math.max(0, Math.min(100, Math.round(Number(row.caught_per_piece) * 100))));
    })
    .join(" ");
  var shareLine = el("p", { class: "profile-card-meta", text: "Pieces that needed changes, by week " });
  var shareSpark = habitSparkline(shares, "Share of pieces that needed changes, by week: " + shares);
  if (shareSpark) shareLine.appendChild(shareSpark);
  block.appendChild(shareLine);
  if (/\d/.test(caught)) {
    var caughtLine = el("p", { class: "profile-card-meta", text: "Mistakes you caught per piece, by week " });
    var caughtSpark = habitSparkline(caught, "Mistakes you caught per piece, by week: " + caught);
    if (caughtSpark) caughtLine.appendChild(caughtSpark);
    block.appendChild(caughtLine);
  }
  var weeks = "Weeks from " + rows[0].week + " to " + rows[rows.length - 1].week + ".";
  var hint = weeks + " A week gets a bar once " + WEEK_MIN_REWORKED + " of its pieces needed changes; a dash means too few to say.";
  block.appendChild(el("p", { class: "cell-hint", text: hint }));
  var details = el("details", { class: "disclosure" });
  details.appendChild(el("summary", { text: "Week by week" }));
  details.appendChild(renderTable(table, "rework-" + table.name, state.currency, { heading: false }));
  block.appendChild(details);
}

function renderReworkLevels(table, container) {
  if (!tableRowsAsObjects(table).length) return;
  var block = habitsBlock(table, container);
  block.appendChild(renderTable(table, "rework-" + table.name, state.currency, { heading: false }));
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
  // The /cg-brief offer is a note here, there only while the brief_clearly card shows.
  if (table.notes && table.notes.length) block.appendChild(notesList(table.notes, new Set(), true));
}

/* claudeglass service UI: page-glossary.js
 *
 * The Glossary page: Terms (links.js's GLOSSARY, the same wording as the
 * README's glossary) and How costs work (links.js's COST_CARDS, one
 * prose card per concept: the rule, your own numbers for this window,
 * and where to act on it). Both segments carry anchors so a link can
 * point at one entry (#/glossary/terms?term=<slug>) or one card
 * (#/glossary/how-costs-work?card=<slug>) and have it scroll into view
 * and pulse, the same way an evidence link does (grid.js's pulseNode).
 */

import { clear, el, onParams, state } from "./core.js";
import { fetchJson, findSection, loadReport, withWindow } from "./api.js";
import { compactNumber, moneyText, thousands } from "./format.js";
import { pulseNode } from "./grid.js";
import { button, errorNotice, loadingNode, panel } from "./ui.js";
import { cardLink, COST_CARDS, GLOSSARY, pageLink, termSlug, viewIntro } from "./links.js";
import { avoidableRebuilds, cardRuleText, pricingFacts } from "./costs.js";

// ======================================================================
// Small table helpers (report.json's {columns, rows} shape, read by
// column key rather than position so a column reorder can't miswire a
// sentence).
// ======================================================================

function findTable(report, sectionKey, tableName) {
  var section = report ? findSection(report, sectionKey) : null;
  var tables = (section && section.tables) || [];
  for (var i = 0; i < tables.length; i++) {
    if (tables[i].name === tableName) return tables[i];
  }
  return null;
}

function rowObject(table, row) {
  var out = {};
  (table.columns || []).forEach(function (column, i) {
    out[column.key] = row[i];
  });
  return out;
}

function colIndex(table, key) {
  var idx = -1;
  (table.columns || []).forEach(function (column, i) {
    if (column.key === key) idx = i;
  });
  return idx;
}

// ======================================================================
// Glossary > Terms
// ======================================================================

function termAnchorId(slug) {
  return "term-" + slug;
}

export function renderGlossary(panelNode) {
  clear(panelNode);
  viewIntro(panelNode, "glossary/terms");

  var termToCard = {};
  COST_CARDS.forEach(function (card) {
    (card.terms || []).forEach(function (term) {
      termToCard[term] = card.slug;
    });
  });

  var whySlots = {};
  var entries = [];
  var list = el("dl", { class: "glossary", id: "glossary-list" });
  GLOSSARY.forEach(function (pair) {
    var term = pair[0];
    var entry = el("div", { class: "glossary-entry", id: termAnchorId(termSlug(term)) });
    entry.appendChild(el("dt", { text: term }));
    entry.appendChild(el("dd", { text: pair[1] }));
    if (termToCard[term]) {
      var why = el("dd", { class: "glossary-why", hidden: true });
      entry.appendChild(why);
      whySlots[term] = why;
    }
    list.appendChild(entry);
    entries.push({ node: entry, text: (term + " " + pair[1]).toLowerCase() });
  });
  var filter = glossaryFilter(entries);
  panelNode.appendChild(filter.node);
  panelNode.appendChild(list);
  panelNode.appendChild(filter.empty);

  // A term link (?term=<slug>) scrolls to and pulses its entry, on the
  // first draw and on every later address change while this segment
  // stays open (app.js calls this right after showing the view, and
  // again on each address change that doesn't redraw it). A filter that
  // hides the entry is cleared first.
  onParams("glossary/terms", function (params) {
    if (!params.term) return;
    var node = document.getElementById(termAnchorId(params.term));
    if (!node) return;
    if (node.hidden) filter.reset();
    pulseNode(node, "block-target");
  });

  // "Why it matters": the same rule sentence the term's cost card
  // states (costs.js's cardRuleText, the one source both segments
  // read), so the two never say the price two different ways.
  loadReport().then(function (result) {
    if (!panelNode.isConnected || result.error) return;
    var facts = pricingFacts(result.report);
    Object.keys(termToCard).forEach(function (term) {
      var slot = whySlots[term];
      var slug = termToCard[term];
      var text = slug ? cardRuleText(slug, facts) : "";
      if (!slot || !text) return;
      clear(slot);
      slot.hidden = false;
      slot.appendChild(el("strong", { text: "Why it matters: " }));
      slot.appendChild(document.createTextNode(text + " "));
      slot.appendChild(cardLink(slug, "How costs work"));
    });
  });
}

// The filter over Terms: a labelled search field that narrows the list
// as you type. An entry shows when every word typed is in its term or
// its definition. Esc empties the field; with nothing matching, the
// page says so and offers every term again. entries: [{node, text}],
// text in lower case.
function glossaryFilter(entries) {
  var input = el("input", {
    type: "search",
    id: "glossary-filter",
    class: "glossary-filter-input",
    autocomplete: "off",
    spellcheck: false,
    "aria-controls": "glossary-list",
    "aria-describedby": "glossary-filter-hint",
  });
  var count = el("p", { class: "glossary-filter-count", role: "status" });
  var node = el("div", { class: "glossary-filter" }, [
    el("label", { for: "glossary-filter", text: "Find a term" }),
    input,
    el("p", { class: "notes", id: "glossary-filter-hint", text: "Matches a term or its definition. Esc clears it." }),
    count,
  ]);
  var emptyText = el("span");
  var showAll = button("Show every term", { variant: "quiet", action: reset });
  var empty = el("div", { class: "empty-state glossary-empty", hidden: true }, [
    el("p", { class: "empty-message" }, [emptyText]),
    el("p", { class: "empty-next", text: "Try a shorter word or a different spelling." }),
    el("div", { class: "glossary-empty-actions" }, [showAll]),
  ]);

  function apply() {
    var words = input.value.toLowerCase().split(/\s+/).filter(Boolean);
    var shown = 0;
    entries.forEach(function (entry) {
      var match = words.every(function (word) {
        return entry.text.indexOf(word) !== -1;
      });
      entry.node.hidden = !match;
      if (match) shown += 1;
    });
    // Said once per change, not per keystroke that changes nothing.
    var said = !words.length ? "" : !shown ? "No term matches." : shown === entries.length ? "Every term matches." : shown + " of " + entries.length + " terms match.";
    if (count.textContent !== said) count.textContent = said;
    // With nothing to show, the box below says it (with the words typed)
    // on screen; the count still says it to a screen reader.
    count.classList.toggle("visually-hidden", !shown);
    empty.hidden = shown > 0;
    emptyText.textContent = shown ? "" : "No term matches “" + input.value.trim() + "”.";
  }

  function reset() {
    input.value = "";
    apply();
    input.focus();
  }

  input.addEventListener("input", apply);
  input.addEventListener("keydown", function (event) {
    if (event.key !== "Escape" || !input.value) return;
    // The field empties, and Esc goes no further (it would close nothing).
    event.preventDefault();
    event.stopPropagation();
    reset();
  });
  return { node: node, empty: empty, reset: reset };
}

// ======================================================================
// Glossary > How costs work
// ======================================================================

function cardAnchorId(slug) {
  return "cost-card-" + slug;
}

// Where each card's "what to do" link points: the view that shows the
// section its numbers come from (links.js's SECTION_PAGE_MAP would
// answer the same question for a report section; billing-mode and
// cache-reads have no section of their own, so they name a view
// directly).
var CARD_LINKS = {
  "cache-reads": function () {
    return pageLink("cache/rebuilds", "See what forces a rebuild");
  },
  "cache-writes": function () {
    return pageLink("cache/lifetime", "See the break-even by agent type");
  },
  "cache-rebuilds": function () {
    return pageLink("cache/rebuilds", "See what's causing them");
  },
  "model-choice": function () {
    return pageLink("spend/savings", "See the model-swap numbers");
  },
  "startup-context": function () {
    return pageLink("agents/subagents", "See what each agent type sends at startup");
  },
  "tool-output": function () {
    return pageLink("spend/savings", "See the cost of keeping each tool's output");
  },
  "conversation-summaries": function () {
    return pageLink("spend/savings", "See the cheapest window");
  },
  "billing-mode": function () {
    return pageLink("overview", "See your spend in this mode");
  },
};

// The "your own numbers" paragraph for one card: report.json's own
// figures for this window, worded plainly, or one sentence saying there
// isn't enough data yet. ctx: {report, summary, facts}.
var CARD_NUMBERS = {
  "cache-reads": function (ctx) {
    var summary = ctx.summary;
    if (!summary || !summary.cache_read_tokens) {
      return "Your sessions haven't read anything from the cache in this window.";
    }
    return (
      "In this window, your sessions read " +
      compactNumber(summary.cache_read_tokens) +
      " tokens from the cache. Sent fresh instead, that would have cost " +
      moneyText(summary.cache_saved || 0, { prefix: "about" }) +
      " more."
    );
  },
  "cache-writes": function (ctx) {
    var table = findTable(ctx.report, "ttl", "ttl_break_even_share");
    var rows = table ? table.rows : [];
    if (!rows.length) {
      return "None of your agent types went idle long enough this window to compare a 5-minute and a 1-hour cache lifetime.";
    }
    var marginIdx = colIndex(table, "margin");
    var positive = rows.filter(function (row) {
      return Number(row[marginIdx]) > 0;
    });
    if (!positive.length) {
      return "None of your agent types would gain from a 1-hour lifetime in this window: the 5-minute lifetime already wins for all " + thousands(rows.length) + " of them.";
    }
    var totalMargin = positive.reduce(function (sum, row) {
      return sum + Number(row[marginIdx]);
    }, 0);
    return (
      "Across " +
      thousands(rows.length) +
      " agent types this window, a 1-hour lifetime would have paid for itself for " +
      thousands(positive.length) +
      " of them, for " +
      moneyText(totalMargin, { prefix: "about" }) +
      " together."
    );
  },
  "cache-rebuilds": function (ctx) {
    var table = findTable(ctx.report, "recache", "recache_summary");
    var row = table && table.rows[0];
    var stats = row ? rowObject(table, row) : null;
    if (!stats || !stats.recache_turns) {
      return "No cache rebuilds happened in this window.";
    }
    // Counted as Cache > Rebuilds counts them: the rebuilds the cost
    // covers, without the ones after a usage-limit pause.
    var times = avoidableRebuilds(ctx.report);
    if (times === 0) {
      return "Every cache rebuild in this window came after a usage-limit pause. You can't avoid those, so there's nothing to save.";
    }
    var cost = moneyText(stats.avoidable_cost_usd || 0, { prefix: "about" });
    return (
      "In this window, " +
      (times === null ? "cache rebuilds cost " + cost : thousands(times) + (times === 1 ? " reply" : " replies") + " rebuilt the cache, at " + cost) +
      " more than reading the same tokens from the cache would have cost. Rebuilds after a usage-limit pause aren't counted."
    );
  },
  "model-choice": function (ctx) {
    var table = findTable(ctx.report, "model_swap", "model_swap_summary");
    var row = table && table.rows[0];
    var stats = row ? rowObject(table, row) : null;
    // model_swap_summary counts subagent types on Fable or Opus only; the
    // main session is priced on other models in the table the link opens.
    if (!stats || !stats.saving_usd) {
      return "No subagent type in this window runs on Fable or Opus, so none has a cheaper tier to move down to. The model-swap numbers also price your main session on cheaper models.";
    }
    return (
      "In this window, " +
      thousands(stats.agent_types) +
      (Number(stats.agent_types) === 1 ? " subagent type runs" : " subagent types run") +
      " on Fable or Opus: moving them down one tier would come to " +
      moneyText(stats.cost_after_tier_down_usd, { prefix: "about" }) +
      " instead of " +
      moneyText(stats.observed_cost_usd, { prefix: "about" }) +
      ", a ceiling saving of " +
      moneyText(stats.saving_usd, { prefix: "about" }) +
      "."
    );
  },
  "startup-context": function (ctx) {
    var table = findTable(ctx.report, "agent_startup", "agent_startup_breakdown");
    var rows = table ? table.rows : [];
    if (!rows.length) {
      return "No subagents started in this window.";
    }
    var totals = rows.reduce(
      function (acc, row) {
        var stats = rowObject(table, row);
        var spawns = Number(stats.spawns) || 0;
        var tokens = Number(stats.startup_tokens) || 0;
        var price = Number(stats.write_price) || 0;
        acc.spawns += spawns;
        acc.tokens += tokens * spawns;
        acc.cost += (tokens * spawns * price) / 1e6;
        return acc;
      },
      { spawns: 0, tokens: 0, cost: 0 }
    );
    if (!totals.spawns) {
      return "No subagents started in this window.";
    }
    return (
      "In this window, your subagents started " +
      thousands(totals.spawns) +
      " times and sent " +
      compactNumber(totals.tokens) +
      " tokens of startup context in total, at " +
      moneyText(totals.cost, { prefix: "about" }) +
      "."
    );
  },
  "tool-output": function (ctx) {
    var table = findTable(ctx.report, "carry", "carry_by_tool");
    var rows = table ? table.rows : [];
    if (!rows.length) {
      return "No large tool output was kept in context in this window.";
    }
    var costIdx = colIndex(table, "carry_cost_usd");
    var tokensIdx = colIndex(table, "carry_tokens");
    var totals = rows.reduce(
      function (acc, row) {
        acc.tokens += Number(row[tokensIdx]) || 0;
        acc.cost += Number(row[costIdx]) || 0;
        return acc;
      },
      { tokens: 0, cost: 0 }
    );
    if (!totals.cost) {
      return "No large tool output was kept in context in this window.";
    }
    return (
      "In this window, tool output already sent was carried forward and read again on later turns: " +
      compactNumber(totals.tokens) +
      " tokens in total, at " +
      moneyText(totals.cost, { prefix: "about" }) +
      "."
    );
  },
  "conversation-summaries": function (ctx) {
    var table = findTable(ctx.report, "compaction_sim", "compaction_sim_by_window");
    var rows = table ? table.rows : [];
    if (!rows.length) {
      return "Not enough conversations were summarised in this window to compare window sizes.";
    }
    var windowIdx = colIndex(table, "window");
    var costIdx = colIndex(table, "cost");
    var deltaIdx = colIndex(table, "delta_usd");
    var limitIdx = colIndex(table, "within_limit");
    var current =
      rows.filter(function (row) {
        return Math.abs(Number(row[deltaIdx])) < 0.005;
      })[0] || rows[0];
    // Only the sizes the advice may offer: one that would summarise more
    // often than the summaries-per-session limit is refused by the
    // compaction-window rule, the savings lever and the summary-point chart
    // alike (charts-types.js's withinLimit, the same rule). A table from
    // before the column existed offers every size.
    var offered = rows.filter(function (row) {
      var value = limitIdx < 0 ? true : row[limitIdx];
      return !(value === false || value === "False" || value === "false");
    });
    var best = offered.reduce(function (min, row) {
      return Number(row[costIdx]) < Number(min[costIdx]) ? row : min;
    }, offered[0] || current);
    var saving = Number(current[costIdx]) - Number(best[costIdx]);
    if (!(saving > 0.005)) {
      return "Your current summary window already looks like the cheapest one this data suggests.";
    }
    return (
      "Right now your main session summarises near " +
      current[windowIdx] +
      " tokens, at " +
      moneyText(Number(current[costIdx]), { prefix: "about" }) +
      ". The cheapest window this data suggests is " +
      best[windowIdx] +
      " tokens, a possible saving of " +
      moneyText(saving, { prefix: "about" }) +
      "."
    );
  },
  "billing-mode": function () {
    var units = state.units || {};
    if (units.mode !== "subscription") return "You're on pay-per-token billing, so amounts here show in money.";
    // format.js's money() falls back to list-price dollars until the
    // usage-limit readings give a share of the weekly limit.
    if (units.share_per_usd === null || units.share_per_usd === undefined) {
      return "You're on a Pro or Max plan. Amounts here show as list-price equivalents until your usage-limit readings are logged, then as a share of your weekly limit.";
    }
    return "You're on a Pro or Max plan, so amounts here show as a share of your weekly usage limit, with the list-price equivalent alongside.";
  },
};

function renderCostCard(card, ctx) {
  var rule = cardRuleText(card.slug, ctx.facts);
  var body = [
    el("p", { text: rule || "Your pricing doesn't have this priced yet, so the multiplier isn't shown." }),
    el("p", { text: CARD_NUMBERS[card.slug] ? CARD_NUMBERS[card.slug](ctx) : "" }),
  ];
  var link = CARD_LINKS[card.slug] ? CARD_LINKS[card.slug]() : null;
  if (link) body.push(el("p", { class: "cost-card-action" }, [link]));
  return panel({ id: cardAnchorId(card.slug), class: "cost-card", level: 2, title: card.title, body: body });
}

export function renderCostCards(panelNode) {
  clear(panelNode);
  viewIntro(panelNode, "glossary/how-costs-work");

  var container = el("div", { class: "cost-cards", id: "cost-cards" });
  panelNode.appendChild(container);
  container.appendChild(loadingNode("Loading your pricing and numbers", "rows"));

  var wanted = null;
  function reveal(slug) {
    var node = document.getElementById(cardAnchorId(slug));
    if (node) pulseNode(node, "block-target");
  }
  onParams("glossary/how-costs-work", function (params) {
    wanted = params.card || null;
    if (wanted && container.childElementCount && !container.querySelector(".loading")) reveal(wanted);
  });

  Promise.all([loadReport(), fetchJson(withWindow("/api/summary"))]).then(function (results) {
    if (!container.isConnected) return;
    clear(container);
    var loaded = results[0];
    if (loaded.error) {
      container.appendChild(errorNotice(loaded.error));
      return;
    }
    var summaryBody = results[1].body;
    var ctx = {
      report: loaded.report,
      summary: summaryBody && summaryBody.ok === true ? summaryBody.data : null,
      facts: pricingFacts(loaded.report),
    };
    COST_CARDS.forEach(function (card) {
      container.appendChild(renderCostCard(card, ctx));
    });
    if (wanted) reveal(wanted);
  });
}

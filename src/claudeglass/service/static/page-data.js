/* claudeglass service UI: page-data.js
 *
 * The Data quality page: the service's health, parse diagnostics, what
 * this tool installed, and any report section no other page shows.
 */

import { clear, el, state } from "./core.js";
import { loadInto, loadReport, withWindow } from "./api.js";
import { codeBlockWithCopy, errorNotice, loadingNode, prose } from "./ui.js";
import { headRow, renderMappedSections, renderTable } from "./grid.js";
import { tuningLink, viewIntro } from "./links.js";
import { renderHealth, renderSetupList } from "./shell.js";

// ======================================================================
// Data quality (unmapped sections + the parse-quality counters)
// ======================================================================

export function renderDataQuality(panel) {
  clear(panel);
  viewIntro(panel, "data");
  // Whether each part works: the checklist 'claudeglass status'
  // prints, and the Overview's Setup card lists the parts still to do.
  var statusBlock = el("section", { class: "report-section", id: "data-setup-status" });
  statusBlock.appendChild(el("h2", { class: "section-title", text: "Your setup" }));
  var statusContainer = el("div");
  statusBlock.appendChild(statusContainer);
  panel.appendChild(statusBlock);
  loadInto(statusContainer, "/api/setup/status", renderSetupList, { skeleton: "lines" });

  var setupBlock = el("section", { class: "report-section" });
  setupBlock.appendChild(el("h2", { class: "section-title", text: "What this tool installed, and what to expect" }));
  var setupContainer = el("div", { id: "diagnostics-setup" });
  setupBlock.appendChild(setupContainer);
  panel.appendChild(setupBlock);
  loadInto(setupContainer, "/api/setup", renderSetup, { skeleton: "lines" });

  // The service's health in full; the sidebar's status line and the
  // banner under the header say when something needs a look.
  var healthBlock = el("section", { class: "report-section", id: "data-health" });
  healthBlock.appendChild(el("h2", { class: "section-title", text: "Service health" }));
  var healthContainer = el("div");
  healthBlock.appendChild(healthContainer);
  panel.appendChild(healthBlock);
  loadInto(healthContainer, "/api/health", renderHealth, { skeleton: "lines" });
  var sectionContainer = el("div", { id: "diagnostics-sections" });
  panel.appendChild(sectionContainer);
  sectionContainer.appendChild(loadingNode("Loading the report", "rows"));
  loadReport().then(function (result) {
    clear(sectionContainer);
    if (result.error) {
      sectionContainer.appendChild(errorNotice(result.error));
      return;
    }
    renderMappedSections(result.report, "data", sectionContainer);
  });

  // The parse-quality counters, labelled (helptext.diagnostics_table).
  var countersContainer = el("section", { class: "report-section", id: "diagnostics-counters" });
  panel.appendChild(countersContainer);
  loadInto(
    countersContainer,
    withWindow("/api/diagnostics"),
    function (table, target) {
      var title = table.title || "How well your transcripts were read";
      var head = target.appendChild(headRow(el("h2", { class: "section-title", text: title }), table.help, title));
      target.appendChild(renderTable(table, "diagnostics-counters-table", state.currency, { heading: false, helpInto: head, allRows: true }));
    },
    { skeleton: "rows" }
  );

  // Some advice rests on figures no page shows; the tuning file holds them (K1).
  panel.appendChild(
    el("p", { class: "notes", id: "data-figures" }, [
      "Some advice rests on figures no page shows, such as cost per reply and what a summary saves. The tuning file holds them. ",
      tuningLink("Figures behind the advice"),
    ])
  );
}

// ======================================================================
// Data quality: what this tool installed, what to expect, and how to
// take it back out
// ======================================================================

function renderSetup(data, container) {
  container.appendChild(el("h3", { text: "What to expect" }));
  container.appendChild(
    el(
      "ul",
      { class: "notes expectations" },
      (data.expectations || []).map(function (item) {
        return el("li", null, [el("strong", { text: item.title + ". " }), el("span", null, prose(item.text))]);
      })
    )
  );
  container.appendChild(el("h3", { text: "What it installed and changed" }));
  (data.items || []).forEach(function (item) {
    var box = el("details", { class: "disclosure" });
    box.appendChild(el("summary", { text: item.title + ": " + item.status }));
    var list = el("dl", { class: "fix-explainer" });
    [["Where", item.where], ["What it does", item.what_it_does], ["Tokens", item.token_cost], ["To undo it", item.undo]].forEach(function (pair) {
      list.appendChild(el("dt", { text: pair[0] }));
      list.appendChild(el("dd", null, prose(pair[1] || "")));
    });
    box.appendChild(list);
    container.appendChild(box);
  });
  container.appendChild(el("h3", { text: "Remove everything" }));
  container.appendChild(el("p", { class: "notes", text: "This command shows what it would remove, undo and delete. Run it again without --dry-run to do it: it asks before each step and backs up settings.json first." }));
  container.appendChild(codeBlockWithCopy(data.uninstall_command, "Command"));
}

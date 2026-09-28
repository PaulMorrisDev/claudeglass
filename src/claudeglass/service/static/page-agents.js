/* claudeglass service UI: page-agents.js
 *
 * The Agents & context page: Agents, Quality, Context and Hooks.
 */

import { clear, el } from "./core.js";
import { formatCell, thousands } from "./format.js";
import { loadInto, loadReport, withWindow } from "./api.js";
import { drawer, emptyState, errorNotice, loadingNode, renderFix, renderFixList } from "./ui.js";
import { dataGrid, renderMappedSections, simpleTable } from "./grid.js";
import { viewIntro } from "./links.js";

// ======================================================================
// Agents & context, Agents (agent_startup, agents, run_split), Quality (quality,
// workflows, workstyle) and Hooks (hooks): report sections only
// ======================================================================

function renderReportSections(container, viewKey) {
  container.appendChild(loadingNode("Loading the report", "rows"));
  loadReport().then(function (result) {
    clear(container);
    if (result.error) {
      container.appendChild(errorNotice(result.error));
      return;
    }
    renderMappedSections(result.report, viewKey, container);
  });
}

export function renderAgents(panel) {
  clear(panel);
  viewIntro(panel, "agents/subagents");
  var container = el("div", { id: "agents-sections" });
  panel.appendChild(container);
  renderReportSections(container, "agents/subagents");
}

export function renderAgentQuality(panel) {
  clear(panel);
  viewIntro(panel, "agents/quality");
  var container = el("div", { id: "agent-quality-sections" });
  panel.appendChild(container);
  renderReportSections(container, "agents/quality");
}

export function renderAgentHooks(panel) {
  clear(panel);
  viewIntro(panel, "agents/hooks");
  var container = el("div", { id: "agent-hooks-sections" });
  panel.appendChild(container);
  renderReportSections(container, "agents/hooks");
}

// ======================================================================
// Agents & context, Context: every CLAUDE.md file and every skill Claude
// Code lists, with how often each is sent and what it costs, then the
// context budget section
// ======================================================================

export function renderContextFiles(panel) {
  clear(panel);
  viewIntro(panel, "agents/context");
  var files = contextSection(
    panel,
    "CLAUDE.md files",
    "Read from disk when you open this page and never stored. Sent to your main session at its start and to most subagents each time one starts.",
    "context-claude-md"
  );
  loadInto(files, withWindow("/api/claude-md"), renderClaudeMdList, { skeleton: "rows" });

  var skills = contextSection(
    panel,
    "Skills",
    "Claude Code lists every skill's name and description at the start of each session and subagent, used or not. Descriptions are read from your newest transcript and never stored.",
    "context-skills"
  );
  loadInto(skills, withWindow("/api/skills"), renderSkills, { skeleton: "rows" });

  var budget = el("div", { id: "context-budget" });
  panel.appendChild(budget);
  renderReportSections(budget, "agents/context");
}

// One part of the Context view: a titled section, its one-line intro,
// and the body its data fills.
function contextSection(panel, title, intro, id) {
  var section = el("section", { class: "report-section" });
  section.appendChild(el("div", { class: "block-head" }, [el("h2", { class: "section-title", text: title })]));
  section.appendChild(el("p", { class: "section-intro", text: intro }));
  var body = el("div", { id: id });
  section.appendChild(body);
  panel.appendChild(section);
  return body;
}

function openClaudeMd(file) {
  drawer({
    title: file.path,
    wide: true,
    fill: function (body) {
      if (file.findings && file.findings.length) {
        body.appendChild(
          el(
            "ul",
            { class: "notes" },
            file.findings.map(function (finding) {
              return el("li", { text: finding });
            })
          )
        );
      }
      var detail = el("div");
      body.appendChild(detail);
      loadInto(detail, withWindow("/api/claude-md/" + encodeURIComponent(file.id)), renderClaudeMdDetail);
    },
  });
}

function renderClaudeMdList(data, container) {
  var rows = data.files || [];
  if (!rows.length) {
    container.appendChild(
      emptyState(
        "No CLAUDE.md files found in your projects or your home folder.",
        null,
        "Claude Code reads one at the start of every session once you add it."
      )
    );
    return;
  }
  container.appendChild(
    dataGrid({
      id: "context-claude-md-grid",
      caption: "CLAUDE.md files",
      columns: [
        {
          key: "path",
          label: "File",
          render: function (row) {
            return el("span", { class: "entity-name", title: row.path, text: row.path });
          },
        },
        { key: "who", label: "Read by", kind: "str" },
        { key: "tokens", label: "Size", kind: "tokens" },
        { key: "sends", label: "Times sent", kind: "int" },
        {
          key: "reach_text",
          label: "Sent to",
          kind: "str",
          value: function (row) {
            return row.seen ? row.reach_text : "Not seen in this window";
          },
        },
        { key: "cost_usd", label: "Cost", kind: "money" },
        { key: "fix_count", label: "Fixes", kind: "int" },
      ],
      rows: rows,
      rowKey: function (row) {
        return row.id;
      },
      rowAction: {
        label: function (row) {
          return "Review " + row.path;
        },
        run: function (row) {
          openClaudeMd(row);
        },
      },
    })
  );
}

function renderClaudeMdDetail(data, container) {
  container.appendChild(
    el("p", { class: "notes", text: thousands(data.tokens) + " tokens" + (data.reach_text ? ", sent to " + data.reach_text : "") + (data.cost_text ? ", " + data.cost_text : "") + "." })
  );
  var sections = data.section_rows || [];
  if (sections.length) {
    container.appendChild(
      simpleTable(
        [{ label: "Section" }, { label: "Line" }, { label: "Tokens" }, { label: "Share" }, { label: "Cost" }, { label: "Only about" }],
        sections.map(function (s) {
          return [
            (s.level > 1 ? "  ".repeat(s.level - 1) : "") + (s.heading || "(before the first heading)"),
            s.line,
            thousands(s.tokens),
            formatCell(s.share * 100, "pct"),
            s.cost_text || "",
            (s.agents || []).join(", "),
          ];
        }),
        "Sections, largest share of the file first in the prompt"
      )
    );
  }
  if (data.duplicates && data.duplicates.length) {
    container.appendChild(el("h4", { text: "Repeated text" }));
    container.appendChild(el("ul", { class: "notes" }, data.duplicates.map(function (d) {
      var where = (d.also_in || []).map(function (o) {
        return o.file + " line " + o.line;
      });
      return el("li", { text: "Line " + d.line + ", about " + d.tokens + " tokens: “" + d.excerpt + "”" + (where.length ? ", also in " + where.join(", ") : "") });
    })));
  }
  if (data.stale && data.stale.length) {
    container.appendChild(el("h4", { text: "References to things that no longer exist" }));
    container.appendChild(el("ul", { class: "notes" }, data.stale.map(function (d) {
      return el("li", { text: "Line " + d.line + ": " + d.reference + " (" + d.kind + ")" });
    })));
  }
  if (data.fixes && data.fixes.length) {
    container.appendChild(el("h3", { text: "What you could change" }));
    renderFixList(data.fixes, container);
  } else {
    container.appendChild(emptyState("Nothing to change in this file: it has no repeated text, and nothing in it points at something that's gone."));
  }
}

var SKILL_STATUS = {
  unused: "Never used",
  used: "Used",
  listed: "Listed",
  "not listed": "Not listed",
  hidden: "Already hidden",
  "needed by a tool": "Needed by a Claude Code tool",
  "no longer listed": "No longer listed",
};

function openSkill(row) {
  drawer({
    title: row.name,
    fill: function (body) {
      body.appendChild(el("p", { text: row.description || "(no description in the listing)" }));
      var facts = el("dl", { class: "fact-list" });
      [
        ["Where it comes from", row.source_label],
        ["Status", SKILL_STATUS[row.status] || row.status],
        ["In the listing", thousands(row.listing_tokens) + " tokens" + (row.listing_cost_text ? ", " + row.listing_cost_text : "")],
        ["Listed to", row.listed_text],
        ["Used", row.use_text],
        ["File", row.path],
      ].forEach(function (pair) {
        if (!pair[1]) return;
        facts.appendChild(el("dt", { text: pair[0] }));
        facts.appendChild(el("dd", { text: pair[1] }));
      });
      body.appendChild(facts);
      renderFixList(row.fixes, body);
    },
  });
}

function renderSkills(data, container) {
  var rows = data.skills || [];
  if (!rows.length) {
    container.appendChild(
      emptyState(
        "No skill listing in this window: none of its sessions listed any skills.",
        null,
        "Pick a longer window to include more sessions."
      )
    );
    return;
  }
  container.appendChild(
    el("p", {
      class: "quick-summary",
      text:
        rows.length + " skills listed, " + thousands(data.listing_tokens) + " tokens at each start" +
        (data.listing_cost_text ? ", " + data.listing_cost_text : "") + ". " +
        (data.unused ? data.unused + " were never used." : "Every listed skill was used.") +
        (data.limited_text ? " " + data.limited_text : ""),
    })
  );
  // The changes for the whole listing, folded: each opens to its prompt.
  (data.fixes || []).forEach(function (fix) {
    container.appendChild(renderFix(fix, true));
  });
  var filterRow = el("div", { class: "filter-row" });
  var unusedOnly = el("input", { type: "checkbox", id: "skills-unused-only", checked: Boolean(data.unused) });
  filterRow.appendChild(unusedOnly);
  filterRow.appendChild(el("label", { for: "skills-unused-only", text: "Show only skills Claude never used" }));
  container.appendChild(filterRow);
  var list = el("div", { id: "context-skills-list" });
  container.appendChild(list);
  function draw() {
    clear(list);
    list.appendChild(
      dataGrid({
        id: "context-skills-grid",
        caption: "Skills",
        columns: [
          { key: "name", label: "Skill", kind: "str" },
          { key: "source_label", label: "Where it comes from", kind: "str" },
          {
            key: "status",
            label: "Status",
            kind: "str",
            value: function (row) {
              return SKILL_STATUS[row.status] || row.status;
            },
          },
          { key: "listing_tokens", label: "Listing size", kind: "tokens" },
          { key: "invoked", label: "Uses", kind: "int" },
          { key: "listing_cost_usd", label: "Listing cost", kind: "money" },
        ],
        rows: rows.filter(function (row) {
          return !unusedOnly.checked || row.status === "unused";
        }),
        rowKey: function (row) {
          return row.name;
        },
        rowAction: {
          label: function (row) {
            return "Show " + row.name;
          },
          run: function (row) {
            openSkill(row);
          },
        },
      })
    );
  }
  unusedOnly.addEventListener("change", draw);
  draw();
}

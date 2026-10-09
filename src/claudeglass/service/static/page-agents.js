/* claudeglass service UI: page-agents.js
 *
 * The Agents & context page: Agents, Quality, Context and Hooks.
 */

import { clear, el } from "./core.js";
import { formatCell, moneyText, thousands } from "./format.js";
import { loadInto, loadReport, withWindow } from "./api.js";
import { drawer, emptyState, errorNotice, loadingNode, renderFix, renderFixList } from "./ui.js";
import { dataGrid, renderMappedSections, simpleTable } from "./grid.js";
import { sparkline } from "./charts-types.js";
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

  var files = projectFilesSection(panel);
  loadInto(files, withWindow("/api/project-files"), renderProjectFiles, { skeleton: "rows" });
}

// ======================================================================
// Agents, Project files your agents read: the CLAUDE.md files Claude Code
// loads, the files they import and the files agents read by habit
// (context_files.project_files), each with its size, its change over about
// 30 days, who reads it and what it costs a month. Names are worked out
// from the project folders when the page opens and are never stored.
// ======================================================================

// The table the Overview's project-files check and links.js
// PROJECT_FILES_TABLE point at (evidence.js revealEvidence finds it by this name).
var PROJECT_FILES_NAME = "project_files";

function projectFilesSection(panel) {
  var section = el("section", { class: "report-section", "data-table-name": PROJECT_FILES_NAME });
  section.appendChild(el("div", { class: "block-head" }, [el("h2", { class: "section-title", text: "Project files your agents read" })]));
  section.appendChild(
    el("p", {
      class: "section-intro",
      text:
        "These files go into your agents' runs. Claude Code loads some and CLAUDE.md files import some. Agents read the rest in 3 or more runs, or in a fifth of their runs. " +
        "Names are read from your project folders when you open this page and never stored.",
    })
  );
  var body = el("div", { id: "agents-project-files" });
  section.appendChild(body);
  panel.appendChild(section);
  return body;
}

var FILE_SOURCES = {
  auto: "Loaded by Claude Code",
  import: "Imported by a CLAUDE.md",
  read: "Read by agents",
};

function reachName(reach) {
  return reach === "main" ? "Main session" : reach;
}

function percentOf(share) {
  return Math.round((Number(share) || 0) * 100) + "%";
}

// "Main session 100%, Explore 60%": the readers that read it by habit.
function readersText(row, limit) {
  var readers = (row.reach || []).filter(function (item) {
    return item.standing;
  });
  var shown = readers.slice(0, limit || readers.length).map(function (item) {
    return reachName(item.reach) + " " + percentOf(item.share);
  });
  if (readers.length > shown.length) shown.push("and " + (readers.length - shown.length) + " more");
  return shown.join(", ");
}

function changeText(row) {
  if (row.change_pct === null || row.change_pct === undefined) return "";
  var pct = Math.round(row.change_pct);
  return (pct > 0 ? "+" : pct < 0 ? "−" : "") + Math.abs(pct) + "%";
}

function fileLabel(row) {
  return row.name || "A file not found on this machine";
}

function renderProjectFiles(data, container) {
  var rows = data.files || [];
  if (!rows.length) {
    container.appendChild(
      emptyState(
        "No project files stood out in this window.",
        null,
        "A file shows here once Claude Code loads it for your agents. It also shows once agents read it in 3 or more runs, or in a fifth of them. Pick a longer window to include more sessions."
      )
    );
    return;
  }
  var unnamed = data.total - data.named;
  var notes = [thousands(data.total) + " files" + (data.period ? ", " + data.period : "") + "."];
  if (unnamed > 0) {
    notes.push(
      thousands(unnamed) + " could not be named from the project folders on this machine, because they were deleted, moved or sit outside them." +
        (data.truncated ? " The folders were too large to search in full." : "")
    );
  }
  if (data.total > rows.length) notes.push("The " + thousands(rows.length) + " that cost the most are shown.");
  container.appendChild(el("p", { class: "quick-summary", text: notes.join(" ") }));
  container.appendChild(
    dataGrid({
      id: "agents-project-files-grid",
      caption: "Project files your agents read",
      columns: [
        {
          key: "name",
          label: "File",
          kind: "str",
          value: fileLabel,
          render: function (row) {
            var node = el("span", { class: "entity-name", title: row.name ? row.name : "Its name is not known here", text: fileLabel(row) });
            if (!row.project) return node;
            return el("span", null, [node, el("span", { class: "unit", text: " in " + row.project })]);
          },
        },
        {
          key: "source",
          label: "How it arrives",
          kind: "str",
          value: function (row) {
            return FILE_SOURCES[row.source] || row.source;
          },
        },
        { key: "tokens", label: "Size now", kind: "tokens" },
        {
          key: "change_pct",
          label: "Change in 30 days",
          kind: "pct",
          render: function (row) {
            var text = changeText(row);
            var line = sparkline(row.series || [], { width: 64, height: 18 });
            if (!text && !line) return "-";
            var node = el("span", { class: "with-spark" });
            if (text) node.appendChild(el("span", { text: text }));
            if (line) node.appendChild(el("span", { class: "metric-spark", title: "Size by week, " + thousands((row.series || []).length) + " weeks" }, [line]));
            return node;
          },
        },
        {
          key: "types",
          label: "Read by",
          kind: "str",
          value: function (row) {
            return readersText(row, 3);
          },
          render: function (row) {
            return el("span", { title: readersText(row), text: readersText(row, 3) || "-" });
          },
        },
        { key: "cost_month_usd", label: "Cost a month", kind: "money" },
      ],
      rows: rows,
      rowKey: function (row) {
        return row.hash;
      },
      rowAction: {
        label: function (row) {
          return "Show " + fileLabel(row);
        },
        run: function (row) {
          openProjectFile(row);
        },
      },
    })
  );
}

function openProjectFile(row) {
  drawer({
    title: fileLabel(row),
    wide: true,
    fill: function (body) {
      var facts = el("dl", { class: "fact-list" });
      [
        ["How it arrives", FILE_SOURCES[row.source] || row.source],
        ["Project", row.project],
        ["Size now", "About " + thousands(row.tokens) + " tokens"],
        ["About 30 days ago", row.then ? "About " + thousands(row.then) + " tokens" : ""],
        ["Change", changeText(row)],
        ["Cost a month", moneyText(row.cost_month_usd, { period: "a month" })],
      ].forEach(function (pair) {
        if (!pair[1]) return;
        facts.appendChild(el("dt", { text: pair[0] }));
        facts.appendChild(el("dd", { text: pair[1] }));
      });
      body.appendChild(facts);
      var reach = row.reach || [];
      if (reach.length) {
        body.appendChild(
          simpleTable(
            [{ label: "Who" }, { label: "Runs that had it" }, { label: "Share of their runs" }, { label: "By habit" }, { label: "Mean read size" }],
            reach.map(function (item) {
              return [
                reachName(item.reach),
                thousands(item.runs),
                percentOf(item.share),
                item.standing ? "Yes" : "No",
                item.mean_tokens ? "About " + thousands(item.mean_tokens) + " tokens" : "-",
              ];
            }),
            "Who has this file in a run"
          )
        );
      }
      if (row.fixes && row.fixes.length) {
        body.appendChild(el("h3", { text: "What you could change" }));
        renderFixList(row.fixes, body);
      } else if (!row.name) {
        body.appendChild(
          emptyState("No changes to suggest: this file is not in any project folder on this machine, so there is nothing to point a prompt at.")
        );
      }
    },
  });
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
// Code lists (the picked project's, once one is picked), with how often
// each is sent and what it costs, then the context budget section
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

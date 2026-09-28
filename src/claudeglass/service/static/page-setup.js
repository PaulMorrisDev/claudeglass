/* claudeglass service UI: page-setup.js
 *
 * The Setup page's Settings and Profiles.
 */

import { clear, cli, el, state } from "./core.js";
import { fetchJson, findSection, loadInto, loadReport, postJson, withWindow } from "./api.js";
import { shortTs } from "./format.js";
import { button, callout, chip, commandBlock, drawer, emptyState, errorNotice, loadingNode, prose, toast } from "./ui.js";
import { dataGrid, renderMappedSections, renderPlacedTables, renderSectionGeneric, simpleTable } from "./grid.js";
import { captureLink, pageLink, viewIntro } from "./links.js";

// ======================================================================
// Setup, Settings (config-diff + baseline + baseline_comparison)
// ======================================================================

export function renderConfig(panel) {
  clear(panel);
  viewIntro(panel, "setup/settings");

  // What your changes did, and whether estimates came true, are on
  // Your changes (page-changes.js); this page is what is set, and where.
  panel.appendChild(el("p", { class: "notes" }, [el("span", { text: "What each change you made did is on " }), pageLink("changes"), el("span", { text: "." })]));

  var driftContainer = setupSection(panel, "Your settings and how they changed", "config-drift");
  loadInto(driftContainer, withWindow("/api/config-diff?auto_keys=1"), renderConfigDiff, { skeleton: "rows" });

  var baselineContainer = setupSection(panel, "Latest baseline", "config-baseline", { allTime: true });
  loadInto(baselineContainer, "/api/baseline", renderBaseline, { skeleton: "rows" });

  var sectionContainer = el("div", { id: "config-sections" });
  panel.appendChild(sectionContainer);
  sectionContainer.appendChild(loadingNode("Loading the comparison", "rows"));
  loadReport().then(function (result) {
    clear(sectionContainer);
    if (result.error) {
      sectionContainer.appendChild(errorNotice(result.error));
      return;
    }
    // The config section's own tables came from /api/config-diff above.
    renderMappedSections(result.report, "setup/settings", sectionContainer, ["config"]);
  });

}

// One part of a Setup view: a titled section and the body its data
// fills (id: the body's).
// opts.allTime: the section covers all history and every project, on a
// view whose other sections follow the window. Its chip says "All
// time", and "All time, all projects" while the picker shows one.
function setupSection(panel, title, id, opts) {
  var section = el("section", { class: "report-section" });
  var allTime = opts && opts.allTime;
  section.appendChild(
    el("div", { class: "block-head" }, [
      el("h2", { class: "section-title", text: title }),
      allTime ? chip(state.project ? "All time, all projects" : "All time", { icon: "clock", class: "all-time-chip" }) : null,
    ])
  );
  var body = el("div", { id: id });
  section.appendChild(body);
  panel.appendChild(section);
  return body;
}

function renderConfigDiff(data, container) {
  if (data && Array.isArray(data.tables)) {
    renderSectionGeneric(container, data, state.currency, "config-diff");
  } else if (Array.isArray(data) && data.length && data[0] && Array.isArray(data[0].tables)) {
    data.forEach(function (section, i) {
      renderSectionGeneric(container, section, state.currency, "config-diff-" + i);
    });
  } else if (Array.isArray(data) && data.length) {
    renderPlacedTables(container, data, state.currency, "config-diff");
  } else {
    // Only changes the session-start hook saw in your settings files are
    // listed here. A change found in your sessions (the model they ran
    // on, say) is on Your changes, so this can't say your settings didn't
    // change.
    container.appendChild(
      emptyState(
        "No settings changes recorded in this window.",
        null,
        "This lists the changes ClaudeGlass sees in your settings files as each session starts, once it's connected. A change your sessions show another way, such as a different model, is on Your changes."
      )
    );
  }
}

// v0.3: GET /api/baseline now returns {"baseline", "history",
// "capture_status"} (docs/api.md) rather than a bare list -- the
// shape-defensive fallbacks docs/ui.md's own "Shape-defensive
// rendering" note flagged for trimming once api.py landed are gone;
// this reads that shape directly.
function renderBaseline(data, container) {
  var status = data && data.capture_status;
  if (status && status.summary) {
    container.appendChild(callout({ tone: "info", text: status.summary }));
  }

  var latest = data && data.baseline;
  if (!latest) {
    container.appendChild(
      emptyState(
        "No baseline yet: ClaudeGlass hasn't taken a snapshot of your usage to compare later changes against.",
        null,
        "Run " + cli("baseline") + " to take one."
      )
    );
    return;
  }
  if (status && status.started && !status.complete) {
    container.appendChild(
      callout({
        tone: "info",
        title: "This may change.",
        text: "ClaudeGlass is still recording your first sessions, so this baseline may change once that finishes.",
      })
    );
  }

  function timeCell(row, value) {
    return el("span", { class: "nowrap", text: value ? shortTs(value) : "-" });
  }
  var rows = data.history && data.history.length ? data.history : [latest];
  container.appendChild(
    dataGrid({
      id: "config-baseline-grid",
      caption: "Baselines",
      columns: [
        // Nit 27: Store.baselines() joins in the owning project's
        // (redacted) slug specifically so this table doesn't have to
        // show the meaningless projects.id primary key -- render that
        // instead of the raw project_id the route used to be the only
        // thing available here.
        {
          key: "project_slug",
          label: "Project",
          kind: "str",
          value: function (row) {
            return row.project_slug || "-";
          },
        },
        { key: "window_start", label: "From", kind: "str", render: timeCell },
        { key: "window_end", label: "To", kind: "str", render: timeCell },
        { key: "archetype", label: "Kind of work", kind: "str", value: function (row) { return row.archetype || "-"; } },
        { key: "created_at", label: "Taken", kind: "str", render: timeCell },
      ],
      rows: rows,
    })
  );
}

// ======================================================================
// Setup, Profiles
// ======================================================================
//
// v0.3: GET /api/profiles now returns {"profiles": [...each tagged
// source: "catalogue"|"user"...], "suggested_profile_id"} (docs/api.md)
// rather than a bare list of indexed (user-only) profiles, and GET
// /api/profiles/<id>/diff is a real computation (profiles/diff.py)
// rather than a 501 stub -- see that route's own docstring in api.py.

// Filled from GET /api/profile-schema on first use: every key a
// profile may set, with its label, type and plain-English text.
var profileSchemaPromise = null;

function loadProfileSchema() {
  if (!profileSchemaPromise) {
    profileSchemaPromise = fetchJson("/api/profile-schema").then(function (result) {
      return result.body && result.body.ok === true ? result.body.data : null;
    });
  }
  return profileSchemaPromise;
}

var PROFILE_SCOPE_LABELS = {
  user: "Your user settings, every project",
  "project-local": "This project, on your machine only",
  repo: "This project, shared with everyone who works in it",
};

export function renderProfiles(panel) {
  clear(panel);
  viewIntro(panel, "setup/profiles");

  // -- save what you have now, so you can compare or go back later --
  var saveCurrentRow = el("div", { class: "profile-actions" });
  var saveCurrentBtn = button("Save my current settings as a profile", { variant: "primary" });
  saveCurrentBtn.id = "profiles-save-current";
  var saveCurrentStatus = el("span", { class: "notes", role: "status" });
  saveCurrentRow.appendChild(saveCurrentBtn);
  saveCurrentRow.appendChild(saveCurrentStatus);
  panel.appendChild(saveCurrentRow);
  panel.appendChild(
    el("p", {
      class: "notes",
      text: "This saves a copy in this tool's own profile folder. It never changes your Claude Code settings.",
    })
  );

  var formContainer = el("div", { id: "profiles-save-form" });
  var creatorContainer = setupSection(panel, "Create a profile", "profiles-create");
  var listContainer = setupSection(panel, "Your profiles and the built-in ones", "profiles-list");
  var setupsContainer = setupSection(panel, "Best setup for each kind of task", "profiles-setups");
  renderTaskSetups(setupsContainer);
  var editorDetails = el("details", { class: "advanced-detail" });
  editorDetails.appendChild(el("summary", { text: "Edit settings directly" }));
  editorDetails.appendChild(formContainer);
  panel.appendChild(editorDetails);

  var editorShown = false;

  function refreshList() {
    loadInto(listContainer, "/api/profiles", function (data, container) {
      renderProfilesList(data, container);
      // Built once, so a save's status line stays on screen.
      if (!editorShown) {
        editorShown = true;
        renderProfileEditor(formContainer, (data && data.profiles) || [], refreshList);
      }
    });
  }

  var replaceBtn = button("Replace the saved copy");
  replaceBtn.hidden = true;
  saveCurrentRow.appendChild(replaceBtn);

  function saveCurrent(replace) {
    saveCurrentBtn.disabled = true;
    replaceBtn.hidden = true;
    saveCurrentStatus.textContent = "Saving…";
    fetchJson("/api/profiles/from-current" + (replace ? "?replace=1" : ""), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    }).then(function (result) {
      saveCurrentBtn.disabled = false;
      var body = result.body;
      if (result.httpStatus === 409 && /already exists/.test((body && body.error && body.error.message) || "")) {
        // Saved before: ask before overwriting that copy.
        saveCurrentStatus.textContent = "You saved your settings before. Replace that copy with today's settings?";
        replaceBtn.hidden = false;
        return;
      }
      if (!body || body.ok !== true) {
        saveCurrentStatus.textContent = (body && body.error && body.error.message) || "Could not save your settings.";
        return;
      }
      var skipped = body.data.skipped_managed || [];
      saveCurrentStatus.textContent =
        'Saved as "' + (body.data.name || body.data.id) + '".' +
        (skipped.length ? " Left out, because your organisation's policy sets them: " + skipped.join(", ") + "." : "");
      toast("Your current settings are saved as a profile.");
      refreshList();
    });
  }
  saveCurrentBtn.addEventListener("click", function () {
    saveCurrent(false);
  });
  replaceBtn.addEventListener("click", function () {
    saveCurrent(true);
  });

  renderProfileCreator(creatorContainer, refreshList);
  refreshList();
}

function renderProfilesList(data, container) {
  var profiles = (data && data.profiles) || [];
  var suggestedId = data && data.suggested_profile_id;
  if (!profiles.length) {
    container.appendChild(emptyState("No profiles yet.", null, "Save your current settings above, or create one from a goal."));
    return;
  }
  var cards = el("div", { class: "profile-cards" });
  profiles.forEach(function (profile) {
    var isSuggested = Boolean(suggestedId) && profile.id === suggestedId;
    var card = el("article", { class: "profile-card" + (isSuggested ? " profile-card-suggested" : "") });
    var head = el("div", { class: "profile-card-head" }, [el("h3", { text: profile.name || profile.id })]);
    if (isSuggested) head.appendChild(chip("Suggested for you", { tone: "accent", icon: "check" }));
    card.appendChild(head);

    var meta = [profile.source === "catalogue" ? "Built in" : "Yours"];
    if (profile.for && profile.for.length) meta.push("for " + profile.for.join(", ").replace(/-/g, " "));
    if (profile.updated_at) meta.push("saved " + String(profile.updated_at).slice(0, 10));
    card.appendChild(el("p", { class: "profile-card-meta", text: meta.join(" · ") }));

    var summary = el("p", { class: "profile-card-summary" });
    card.appendChild(summary);
    // "Changes 3 settings: Model, Effort level, ..." from the profile
    // and the schema's labels (catalogue notes are for maintainers).
    Promise.all([fetchJson("/api/profiles/" + encodeURIComponent(profile.id)), loadProfileSchema()]).then(function (results) {
      var body = results[0].body;
      if (!body || body.ok !== true) return;
      var p = body.data;
      var labels = {};
      ((results[1] && results[1].settings) || []).concat((results[1] && results[1].agents) || []).forEach(function (lever) {
        labels[lever.key] = lever.label;
      });
      var names = Object.keys(p.settings || {}).map(function (key) {
        return labels[key] || key;
      });
      Object.keys(p.agents || {}).forEach(function (agent) {
        Object.keys(p.agents[agent]).forEach(function (key) {
          names.push((labels[key] || key) + " (" + agent + ")");
        });
      });
      names = names.concat(Object.keys(p.env || {}));
      var count = p.setting_count || names.length;
      summary.textContent =
        "Changes " + count + (count === 1 ? " setting" : " settings") + (names.length ? ": " + names.join(", ") + "." : ".");
    });

    card.appendChild(
      button("Show what it changes", {
        // Every card has this button: its name adds which profile, after
        // the words on it (so saying them still finds it).
        label: "Show what it changes: " + (profile.name || profile.id),
        action: function () {
          drawer({
            title: "What " + (profile.name || profile.id) + " changes",
            wide: true,
            fill: function (body) {
              renderProfileDetail(profile, body);
            },
          });
        },
      })
    );
    cards.appendChild(card);
  });
  container.appendChild(cards);
  if (suggestedId) {
    container.appendChild(el("p", { class: "notes", text: "Suggested for you: the profile your latest baseline matches best." }));
  }
}

function _diffRowValue(value) {
  if (value === null || value === undefined) return "not set";
  if (Array.isArray(value)) return value.length ? value.join(", ") : "(empty list)";
  if (typeof value === "boolean") return value ? "on" : "off";
  return String(value);
}

function diffRowSame(row) {
  return JSON.stringify(row.current_value) === JSON.stringify(row.proposed_value);
}

// One table for every key the profile sets: plain label, now, after,
// and the file it would be written to under the chosen scope.
function renderDiffRowsTable(rows) {
  return dataGrid({
    id: "profile-diff-grid",
    class: "profile-diff-table",
    caption: "What the profile changes",
    sortable: false,
    bar: -1,
    rowClass: function (row) {
      return diffRowSame(row) || row.managed ? "row-unchanged" : null;
    },
    columns: [
      {
        key: "setting",
        label: "Setting",
        render: function (row) {
          var label = row.label || row.setting || row.key;
          if (row.agent) label += " (" + row.agent + " agent)";
          var cell = el("span", null, [el("span", { text: label })]);
          if (row.description) cell.appendChild(el("span", { class: "cell-hint", text: row.description }));
          return cell;
        },
      },
      {
        key: "current_value",
        label: "Now",
        render: function (row) {
          return _diffRowValue(row.current_value);
        },
      },
      {
        key: "proposed_value",
        label: "After",
        render: function (row) {
          return _diffRowValue(row.managed ? row.current_value : row.proposed_value);
        },
      },
      {
        key: "where",
        label: "Set in",
        render: function (row) {
          if (row.managed) return "Locked by your organisation's policy; not changed";
          if (diffRowSame(row)) return "Already set; no change";
          return row.where || row.target_file || "-";
        },
      },
    ],
    rows: rows,
  });
}

// Inside the profile's drawer: the file to target, the estimate, then
// the changes and how to make them.
function renderProfileDetail(profile, container) {
  var scopeRow = el("div", { class: "pager" });
  scopeRow.appendChild(el("label", { for: "profile-scope", text: "Target file:" }));
  var scopeSelect = el("select", { id: "profile-scope" });
  Object.keys(PROFILE_SCOPE_LABELS).forEach(function (scope) {
    scopeSelect.appendChild(el("option", { value: scope, text: PROFILE_SCOPE_LABELS[scope] }));
  });
  scopeRow.appendChild(scopeSelect);
  container.appendChild(scopeRow);
  var estimate = el("div", { class: "profile-estimate" });
  container.appendChild(estimate);
  var logEstimate = renderProfileEstimate(profile, estimate);
  var body = el("div");
  container.appendChild(body);
  function load() {
    loadInto(
      body,
      "/api/profiles/" + encodeURIComponent(profile.id) + "/diff?scope=" + encodeURIComponent(scopeSelect.value),
      function (data, box) {
        renderProfileDiff(data, box, logEstimate);
      }
    );
  }
  scopeSelect.addEventListener("change", load);
  load();
}

// onCopy: runs when its prompt or command is copied (the estimate is
// logged then, as a change you mean to make).
function renderProfileDiff(data, container, onCopy) {
  (data.notes || []).forEach(function (note) {
    container.appendChild(callout({ tone: "info", text: note }));
  });

  var rows = (data.settings || []).concat(data.agents || [], data.env || []);
  if (rows.length) {
    container.appendChild(renderDiffRowsTable(rows));
  } else {
    container.appendChild(emptyState("This profile doesn't change any setting.", null, "To add some, open Edit settings directly on this page."));
  }

  container.appendChild(el("h3", { text: "How to use it" }));
  container.appendChild(
    commandBlock({
      prompt: data.prompt,
      command: data.dry_run_command,
      trial_command: data.launch_command,
      trial_note:
        "It saves these settings to a file in this tool's own folder and prints the command that starts " +
        "Claude Code with them on top of yours. Your settings files aren't changed." +
        ((data.agents || []).length || (data.env || []).length
          ? " A one-session trial carries the settings only, not the agent or environment changes."
          : ""),
    }, { onCopy: onCopy })
  );

  var raw = el("details", { class: "advanced-detail" });
  raw.appendChild(el("summary", { text: "Show the file changes" }));
  raw.appendChild(el("pre", { text: data.diff || "(no changes against your current settings)" }));
  container.appendChild(raw);
}

// -- profile editor: a form built from /api/profile-schema -----------

function leverInput(lever, idPrefix) {
  var id = idPrefix + lever.key.replace(/[^A-Za-z0-9]/g, "-");
  var input;
  if (lever.kind === "enum" || lever.kind === "bool") {
    input = el("select", { id: id });
    input.appendChild(el("option", { value: "", text: "Leave as it is" }));
    var values = lever.kind === "bool" ? ["true", "false"] : lever.values || [];
    values.forEach(function (v) {
      var text = lever.kind === "bool" ? (v === "true" ? "On" : "Off") : v;
      input.appendChild(el("option", { value: v, text: text }));
    });
  } else if (lever.kind === "int") {
    input = el("input", { type: "number", id: id, placeholder: "Leave as it is" });
    if (lever.min !== null && lever.min !== undefined) input.min = String(lever.min);
    if (lever.max !== null && lever.max !== undefined) input.max = String(lever.max);
  } else {
    input = el("input", {
      type: "text",
      id: id,
      placeholder: lever.kind === "list[str]" ? "Comma-separated; leave empty to keep" : "Leave empty to keep",
    });
  }
  var label = el("label", { class: "lever", for: id }, [el("span", { class: "lever-label", text: lever.label })]);
  var hint = [lever.description, lever.tradeoff].filter(Boolean).join(" ");
  var field = el("div", { class: "lever-field" }, [label, input]);
  if (hint) field.appendChild(el("div", { class: "cell-hint", text: hint }));
  return { lever: lever, input: input, node: field };
}

function leverValue(field) {
  var raw = String(field.input.value || "").trim();
  if (!raw) return undefined;
  var kind = field.lever.kind;
  if (kind === "bool") return raw === "true";
  if (kind === "int") return parseInt(raw, 10);
  if (kind === "list[str]") {
    return raw
      .split(",")
      .map(function (s) {
        return s.trim();
      })
      .filter(Boolean);
  }
  return raw;
}

function setLeverValue(field, value) {
  if (value === undefined || value === null) field.input.value = "";
  else if (Array.isArray(value)) field.input.value = value.join(", ");
  else field.input.value = String(value);
}

function renderProfileEditor(container, profiles, onSaved) {
  clear(container);
  container.appendChild(loadingNode("Loading the profile editor"));
  loadProfileSchema().then(function (schema) {
    clear(container);
    if (!schema) {
      container.appendChild(errorNotice({ code: "unavailable", message: "Could not load the list of settings a profile may change." }));
      return;
    }
    buildProfileEditor(container, schema, profiles, onSaved);
  });
}

function buildProfileEditor(container, schema, profiles, onSaved) {
  var form = el("form", { class: "profile-form" });
  container.appendChild(
    el("p", {
      class: "notes",
      text: "Pick only the settings you want to change; anything left empty stays as it is. Saving writes a profile file for this tool. Nothing changes in Claude Code until you use the prompt or command it gives you.",
    })
  );

  var startSelect = el("select", { id: "profile-form-start" });
  startSelect.appendChild(el("option", { value: "", text: "An empty profile" }));
  profiles.forEach(function (p) {
    startSelect.appendChild(el("option", { value: p.id, text: p.name || p.id }));
  });
  var idInput = el("input", { type: "text", id: "profile-form-id", required: true, placeholder: "my-profile" });
  var nameInput = el("input", { type: "text", id: "profile-form-name", placeholder: "My profile" });
  form.appendChild(el("div", { class: "lever-field" }, [el("label", { for: "profile-form-start", text: "Start from" }), startSelect]));
  form.appendChild(
    el("div", { class: "lever-field" }, [el("label", { for: "profile-form-id", text: "Short name (lowercase letters, digits and hyphens)" }), idInput])
  );
  form.appendChild(el("div", { class: "lever-field" }, [el("label", { for: "profile-form-name", text: "Display name" }), nameInput]));

  form.appendChild(el("h3", { text: "Settings for every session" }));
  var settingFields = schema.settings.map(function (lever) {
    return leverInput(lever, "profile-setting-");
  });
  var settingsGrid = el("div", { class: "lever-grid" });
  settingFields.forEach(function (f) {
    settingsGrid.appendChild(f.node);
  });
  form.appendChild(settingsGrid);

  form.appendChild(el("h3", { text: "Settings for one agent" }));
  form.appendChild(el("p", { class: "notes", text: "Written to that agent's file. Use the agent's name as it appears under Agents & context." }));
  var agentBlocks = [];
  var agentsWrap = el("div", { class: "agent-blocks" });
  form.appendChild(agentsWrap);
  var addAgentBtn = button("Add an agent", { variant: "quiet", icon: "plus" });
  form.appendChild(addAgentBtn);

  function addAgentBlock(name, values) {
    var index = agentBlocks.length;
    var nameId = "profile-agent-name-" + index;
    var nameField = el("input", { type: "text", id: nameId, placeholder: "e.g. code-reviewer" });
    nameField.value = name || "";
    var block = el("fieldset", { class: "agent-block" }, [el("legend", { text: "Agent" })]);
    block.appendChild(el("div", { class: "lever-field" }, [el("label", { for: nameId, text: "Agent name" }), nameField]));
    var grid = el("div", { class: "lever-grid" });
    var fields = schema.agents.map(function (lever) {
      var f = leverInput(lever, "profile-agent-" + index + "-");
      setLeverValue(f, (values || {})[lever.key]);
      grid.appendChild(f.node);
      return f;
    });
    block.appendChild(grid);
    var removeBtn = button("Remove this agent", { variant: "quiet", icon: "close" });
    var entry = { name: nameField, fields: fields, node: block };
    removeBtn.addEventListener("click", function () {
      agentBlocks.splice(agentBlocks.indexOf(entry), 1);
      agentsWrap.removeChild(block);
    });
    block.appendChild(removeBtn);
    agentBlocks.push(entry);
    agentsWrap.appendChild(block);
  }
  addAgentBtn.addEventListener("click", function () {
    addAgentBlock("", {});
  });

  var jsonBox = el("details", { class: "advanced-detail" });
  jsonBox.appendChild(el("summary", { text: "Edit as JSON instead" }));
  // Named by its label and described by the note above it, so a screen
  // reader says what the box holds and what saving does with it.
  var jsonInput = el("textarea", { id: "profile-form-json", rows: 8, "aria-describedby": "profile-form-json-note" });
  jsonBox.appendChild(
    el("p", {
      class: "notes",
      id: "profile-form-json-note",
      text: "While this is open, saving uses the JSON below and ignores the form. It starts as a copy of the form.",
    })
  );
  jsonBox.appendChild(el("div", { class: "lever-field" }, [el("label", { for: "profile-form-json", text: "Profile as JSON" }), jsonInput]));
  jsonBox.addEventListener("toggle", function () {
    if (jsonBox.open) jsonInput.value = JSON.stringify(collect(), null, 2);
  });
  form.appendChild(jsonBox);

  var errorNode = el("div", { class: "form-error", hidden: true });
  var statusNode = el("p", { class: "notes", role: "status" });
  var submit = button("Save profile", { variant: "primary" });
  submit.type = "submit";
  form.appendChild(submit);
  form.appendChild(errorNode);
  form.appendChild(statusNode);
  container.appendChild(form);

  function collect() {
    var doc = { id: idInput.value.trim(), settings: {}, agents: {} };
    var name = nameInput.value.trim();
    if (name) doc.name = name;
    settingFields.forEach(function (f) {
      var v = leverValue(f);
      if (v !== undefined) doc.settings[f.lever.key] = v;
    });
    agentBlocks.forEach(function (block) {
      var agentName = block.name.value.trim();
      if (!agentName) return;
      var values = {};
      block.fields.forEach(function (f) {
        var v = leverValue(f);
        if (v !== undefined) values[f.lever.key] = v;
      });
      if (Object.keys(values).length) doc.agents[agentName] = values;
    });
    return doc;
  }

  startSelect.addEventListener("change", function () {
    settingFields.forEach(function (f) {
      setLeverValue(f, undefined);
    });
    agentBlocks.slice().forEach(function (block) {
      agentsWrap.removeChild(block.node);
    });
    agentBlocks.length = 0;
    if (!startSelect.value) return;
    fetchJson("/api/profiles/" + encodeURIComponent(startSelect.value)).then(function (result) {
      var body = result.body;
      if (!body || body.ok !== true) return;
      var p = body.data;
      settingFields.forEach(function (f) {
        setLeverValue(f, p.settings[f.lever.key]);
      });
      Object.keys(p.agents || {}).forEach(function (agentName) {
        addAgentBlock(agentName, p.agents[agentName]);
      });
      if (!nameInput.value) nameInput.value = (p.name || p.id) + " (my copy)";
    });
  });

  function showError(message) {
    clear(errorNode);
    errorNode.appendChild(callout({ tone: "critical", title: "The profile wasn't saved.", text: message }));
    errorNode.hidden = false;
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    errorNode.hidden = true;
    clear(errorNode);
    statusNode.textContent = "";

    var doc;
    if (jsonBox.open) {
      try {
        doc = JSON.parse(jsonInput.value);
      } catch (err) {
        showError("The JSON isn't valid: " + (err && err.message ? err.message : String(err)));
        return;
      }
      if (typeof doc !== "object" || doc === null || Array.isArray(doc)) {
        showError("The JSON must be an object, like {\"id\": \"my-profile\", \"settings\": {}}.");
        return;
      }
    } else {
      doc = collect();
    }
    if (!doc.id) {
      showError("Give the profile a short name.");
      return;
    }

    fetchJson("/api/profiles", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(doc),
    }).then(function (result) {
      var respBody = result.body;
      if (!respBody || respBody.ok !== true) {
        showError((respBody && respBody.error && respBody.error.message) || "Could not save the profile.");
        return;
      }
      statusNode.textContent = 'Saved "' + (respBody.data.name || respBody.data.id) + '". It is in the list above.';
      toast("Profile saved.");
      if (onSaved) onSaved();
    });
  });
}

function candidateValueText(value) {
  return _diffRowValue(value);
}

function renderWhatIf(data, container) {
  clear(container);
  if (!data || !data.rows || !data.rows.length) {
    container.appendChild(el("p", { class: "notes", text: "Tick a change to see its estimated effect." }));
    return;
  }
  if (data.total_text) container.appendChild(el("p", { class: "quick-summary", text: "Estimated effect of these changes: " + data.total_text + "." }));
  if (data.total_note) container.appendChild(el("p", { class: "notes" }, prose(data.total_note)));
  if (data.not_estimated) {
    container.appendChild(
      el("p", { class: "notes", text: data.not_estimated + (data.not_estimated === 1 ? " change isn't" : " changes aren't") + " estimated; see each row." })
    );
  }
}

function renderProfileCreator(container, onSaved) {
  clear(container);
  var goalsBox = el("div", { class: "profile-cards" });
  var draftBox = el("div", { class: "goal-draft" });
  container.appendChild(el("p", { class: "notes", text: "1. Pick what you want. 2. Tick the changes. 3. Name it and save. Saving writes only this tool's profile folder; you then apply it with the prompt or command it shows." }));
  container.appendChild(goalsBox);
  container.appendChild(draftBox);
  loadInto(goalsBox, "/api/profile-goals", function (data, target) {
    // "Start from my current settings" is the Save my current settings
    // button at the top of the page, so it isn't a card here as well.
    (data.goals || []).forEach(function (goal) {
      if (goal.id === "current") return;
      var card = el("article", { class: "profile-card goal-card" });
      card.appendChild(el("h3", { text: goal.title }));
      card.appendChild(el("p", { class: "profile-card-summary" }, prose(goal.what)));
      // Every goal card has this button: its name says which goal.
      var pick = button("Start here", { label: "Start here: " + goal.title });
      pick.addEventListener("click", function () {
        loadInto(draftBox, withWindow("/api/profile-goals?goal=" + encodeURIComponent(goal.id)), function (draft, box) {
          renderGoalDraft(draft, box, onSaved);
        });
        draftBox.scrollIntoView({ behavior: "smooth", block: "start" });
      });
      card.appendChild(pick);
      target.appendChild(card);
    });
  });
}

function renderTaskSetups(container) {
  container.appendChild(loadingNode("Loading setups", "rows"));
  loadReport().then(function (result) {
    clear(container);
    if (result.error) {
      container.appendChild(errorNotice(result.error));
      return;
    }
    var section = findSection(result.report, "habits");
    var table = section && (section.tables || []).filter(function (t) {
      return t.name === "habits_setups";
    })[0];
    if (!table || !(table.rows || []).length) {
      container.appendChild(
        emptyState(
          "Nothing yet: this needs the kind of task Claude reports, which metrics capture records at Essentials or above.",
          null,
          captureLink("Turn on metrics capture")
        )
      );
      return;
    }
    container.appendChild(el("p", { class: "notes", text: "To make a profile from a cheaper setup, pick \"A profile for one kind of task\" under Create a profile." }));
    renderPlacedTables(container, [table], state.currency, "profiles", "Best setup for each kind of task");
  });
}

// A kind of task by its plain name ("Bug fix"), from the server's
// labels for capture's task words.
function taskName(draft, task) {
  return (draft.task_labels && draft.task_labels[task]) || task;
}

function renderGoalDraft(draft, container, onSaved) {
  container.appendChild(el("h3", { text: draft.goal.title }));
  if (draft.tasks && draft.tasks.length > 1) {
    var taskPick = el("select", { id: "goal-task-pick" });
    draft.tasks.forEach(function (task) {
      taskPick.appendChild(el("option", { value: task, text: taskName(draft, task), selected: task === draft.task }));
    });
    taskPick.addEventListener("change", function () {
      var url = "/api/profile-goals?goal=" + encodeURIComponent(draft.goal.id) + "&task=" + encodeURIComponent(taskPick.value);
      loadInto(container, withWindow(url), function (next, box) {
        renderGoalDraft(next, box, onSaved);
      });
    });
    container.appendChild(el("div", { class: "profile-actions" }, [el("label", { for: "goal-task-pick", text: "Kind of task" }), taskPick]));
  }
  if (draft.note) container.appendChild(el("p", { class: "notes" }, prose(draft.note)));
  var candidates = draft.candidates || [];
  if (!candidates.length) {
    if (!draft.note) {
      container.appendChild(
        emptyState(
          "Nothing to change for this goal " + (draft.period || "in this window") + ": your settings already match what the data supports, or there isn't enough data yet.",
          null,
          "Pick a longer window to include more sessions."
        )
      );
    }
    return;
  }
  container.appendChild(el("p", { class: "notes", text: "Ticked changes are the ones your data supports. Unticked ones are a trade-off for you to decide." }));
  var total = el("div", { class: "whatif-total", role: "status" });
  var boxes = candidates.map(function (c, i) {
    return el("input", { type: "checkbox", id: "goal-candidate-" + i, checked: Boolean(c.ticked) });
  });
  container.appendChild(
    dataGrid({
      id: "goal-candidates",
      class: "goal-table",
      caption: "Changes for this goal",
      sortable: false,
      bar: -1,
      columns: [
        {
          key: "__select",
          label: "Use",
          render: function (row) {
            return boxes[row.index];
          },
        },
        {
          key: "label",
          label: "Setting",
          render: function (row) {
            var c = row.candidate;
            return el("label", { for: boxes[row.index].id, text: c.label + (c.agent ? " (" + c.agent + ")" : "") });
          },
        },
        {
          key: "now",
          label: "Now",
          render: function (row) {
            return candidateValueText(row.candidate.now);
          },
        },
        {
          key: "value",
          label: "After",
          render: function (row) {
            return candidateValueText(row.candidate.value);
          },
        },
        {
          key: "effect",
          label: "Estimated effect",
          render: function (row) {
            return (row.candidate.estimate || {}).effect_text || "";
          },
        },
        {
          key: "why",
          label: "Why, and the trade-off",
          render: function (row) {
            var c = row.candidate;
            var estimate = c.estimate || {};
            return el("div", { class: "cell-prose" }, [
              el("p", { text: c.evidence }),
              c.tradeoff ? el("p", { class: "notes", text: "Trade-off: " + c.tradeoff }) : null,
              estimate.basis ? el("p", { class: "notes", text: estimate.fidelity_text + " " + estimate.basis }) : null,
            ]);
          },
        },
      ],
      rows: candidates.map(function (c, i) {
        return { index: i, candidate: c };
      }),
    })
  );
  container.appendChild(total);

  function chosen() {
    var settings = {};
    var agents = {};
    candidates.forEach(function (c, i) {
      if (!boxes[i].checked) return;
      if (c.agent) {
        agents[c.agent] = agents[c.agent] || {};
        agents[c.agent][c.key] = c.value;
      } else {
        settings[c.key] = c.value;
      }
    });
    return { settings: settings, agents: agents };
  }
  var pending = 0;
  var whatifUrl = function () {
    return withWindow("/api/whatif") + (draft.task ? "&task=" + encodeURIComponent(draft.task) : "");
  };
  function refreshTotal() {
    var ticket = ++pending;
    total.textContent = "Working out the estimate…";
    postJson(whatifUrl(), chosen()).then(function (result) {
      if (ticket !== pending) return;
      var body = result.body;
      if (!body || body.ok !== true) {
        clear(total);
        total.appendChild(errorNotice(body && body.error));
        return;
      }
      renderWhatIf(body.data, total);
    });
  }
  boxes.forEach(function (box) {
    box.addEventListener("change", refreshTotal);
  });
  refreshTotal();

  var form = el("div", { class: "profile-actions" });
  var name = el("input", { type: "text", id: "goal-profile-name", value: draft.task ? taskName(draft, draft.task) + " tasks" : draft.goal.title });
  form.appendChild(el("label", { for: "goal-profile-name", text: "Name" }));
  form.appendChild(name);
  var save = button("Save as a profile", { variant: "primary" });
  var status = el("span", { class: "notes", role: "status" });
  form.appendChild(save);
  form.appendChild(status);
  container.appendChild(form);
  save.addEventListener("click", function () {
    var picked = chosen();
    if (!Object.keys(picked.settings).length && !Object.keys(picked.agents).length) {
      status.textContent = "Tick at least one change first.";
      return;
    }
    var id = (name.value || draft.goal.id).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 60) || draft.goal.id;
    save.disabled = true;
    status.textContent = "Saving…";
    postJson("/api/profiles", {
      id: id,
      name: name.value || draft.goal.title,
      for: draft.task ? [draft.task] : [],
      settings: picked.settings,
      agents: picked.agents,
      notes: "Made from the goal \"" + draft.goal.title + "\" " + (draft.period || "") + ".",
    }).then(function (result) {
      save.disabled = false;
      var body = result.body;
      if (!body || body.ok !== true) {
        status.textContent = (body && body.error && body.error.message) || "Could not save the profile.";
        return;
      }
      status.textContent = "Saved. It's under Your profiles and the built-in ones: pick \"Show what it changes\" for the prompt and the command that make the change.";
      toast("Profile saved.");
      // Log the estimate, so Setup can later check it against what the
      // change did (see "Did your estimates come true?").
      postJson(whatifUrl(), { settings: picked.settings, agents: picked.agents, log: true });
      if (onSaved) onSaved(body.data.id);
    });
  });
}

// Returns a function that logs this estimate (at most once), so Setup can
// later check it against what the change did.
function renderProfileEstimate(profile, container) {
  var request = null;
  var logged = false;
  Promise.all([fetchJson("/api/profiles/" + encodeURIComponent(profile.id)), loadProfileSchema()]).then(function (results) {
    var body = results[0].body;
    if (!body || body.ok !== true) return;
    var p = body.data;
    var labels = {};
    ((results[1] && results[1].settings) || []).concat((results[1] && results[1].agents) || []).forEach(function (lever) {
      labels[lever.key] = lever.label;
    });
    // F11: `tasks` is the profile's `for` words normalised to the task
    // vocabulary (a catalogue word like "implementation" isn't one, and
    // /api/whatif rejects it); several scale by their combined share.
    var tasks = p.tasks && p.tasks.length ? p.tasks.join(",") : "";
    var url = withWindow("/api/whatif") + (tasks ? "&task=" + encodeURIComponent(tasks) : "");
    request = { url: url, settings: p.settings || {}, agents: p.agents || {} };
    postJson(url, { settings: request.settings, agents: request.agents }).then(function (res) {
      var data = res.body && res.body.ok === true ? res.body.data : null;
      if (!data || !data.rows.length) return;
      clear(container);
      container.appendChild(el("h3", { text: "Estimated effect" }));
      renderWhatIf(data, container);
      container.appendChild(
        simpleTable(
          [{ label: "Change" }, { label: "Effect" }, { label: "How it was worked out" }],
          data.rows.map(function (row) {
            return [
              (labels[row.key] || row.key) + (row.agent ? " (" + row.agent + ")" : "") + ": " + _diffRowValue(row.value),
              row.effect_text,
              (row.fidelity_text + " " + row.basis).trim(),
            ];
          })
        )
      );
    });
  });
  return function () {
    if (logged || !request) return;
    logged = true;
    postJson(request.url, { settings: request.settings, agents: request.agents, log: true });
  };
}

# Configuration layers and per-project effective config

This is the field-by-field companion to the plan's "Configuration layers
and per-project effective config" section and Appendix A6, for schema 2
of the config snapshot (`hooks/snapshot-config.py`, `snapshots.py`).
Schema 2 is additive over schema 1 — every schema-1 field keeps the same
name and shape, and `snapshots.load_snapshots` still loads a schema-1
file unchanged.

## The layer model

Claude Code resolves settings from several files, highest precedence
first. Schema 2 records each one separately (`settings_layers`) as well
as the merged result (`effective`), so a report can say not just *what*
a lever is set to but *which file* set it:

| Layer            | File                                | Notes |
|------------------|--------------------------------------|-------|
| `managed`        | the platform's `managed-settings.json` | enterprise policy, outside any user's control (macOS `/Library/Application Support/ClaudeCode/…`, Linux `/etc/claude-code/…`, Windows `%ProgramFiles%\ClaudeCode\…` — COV-05: the legacy `%ProgramData%` path this doc and `default_managed_settings_dir` used to disagree on was a doc/code conflict, fixed in favour of `%ProgramFiles%`, `ProgramFiles` still env-overridable so tests don't need a real one) |
| `project_local`  | `<project>/.claude/settings.local.json` | per-machine, not checked in |
| `project_shared` | `<project>/.claude/settings.json`   | checked in, shared with a team |
| `user`           | `~/.claude/settings.json` (or `$CLAUDE_CONFIG_DIR/settings.json`) | this machine's default |

`managed-mcp.json` lives in the same system managed-settings directory as
`managed-settings.json` (`default_managed_settings_dir`), **not** under
the project's own `claude_root` — COV-05 fixed a lookup that previously
looked in the wrong place and so silently never found a real managed-MCP
file. `~/.claude.json` (the CLI's own per-machine state file — see
"`claude_json`" below) honours `CLAUDE_CONFIG_DIR` the same way
`~/.claude/settings.json` does: when set, it's read from
`$CLAUDE_CONFIG_DIR/.claude.json`, not unconditionally from the home
directory (COV-05, fixing a second doc/code conflict where the file was
always read from `~` even when every other per-machine file honoured the
override).

`SETTINGS_LAYER_ORDER` in the hook (`SETTINGS_LAYER_NAMES` in
`snapshots.py`) fixes this precedence; `effective`/`effective_provenance`
walk it in this order and the first layer that defines a key wins.
`claude --settings` one-launch overlays and shell-profile environment
variables sit outside this file-based model entirely — the hook can't
see them, which is exactly what `detect_drift`/`build_config_drift_table`
exist to surface (a session whose *observed* behaviour disagrees with
its snapshot's `effective` config implies one of these).

## What each layer records (`settings_layers[<layer>]`)

- `present` — whether the file exists at all.
- `source_path_hash` — `sha256:<hex>` of the file's own path. The raw
  path is never recorded.
- `content_hash` — `sha256:<hex>` of the raw JSON, so two layers (or the
  same layer across two machines) can be compared for equality without
  diffing the file itself. `null` when `present` is `false`.
- `redacted` — the whole file, redacted key-by-key with the existing
  allowlist rule (see "Redaction rule" below).
- `env_names` — the settings file's own `env` block, **names only**.
- `permissions` — `{allow_count, deny_count, ask_count, default_mode}`.
  Never the allow/deny/ask rules themselves — a Bash allowlist entry is
  exactly the kind of content this hook must not record.
- `hooks` — `{event_name: entry_count}`. Never the commands a hook runs.
- `enabled_plugins` — plugin names from this layer's `enabledPlugins`.
- The named safe scalars, verbatim: `model`, `effort_level`,
  `max_effort_level` (PROF-03, a hard cap — see "PROF-03" below),
  `always_thinking_enabled`, `auto_compact_window`, `prompt_cache_ttl`,
  `subagent_prompt_cache_ttl`, `cleanup_period_days`, `output_style`.
- `model_settings` (PROF-03) — `{model_id: {effortLevel}}`, redacted per
  model — see "PROF-03" below.
- `statusline_present` — a boolean, never the statusline command itself.

## Redaction rule (settings and agent frontmatter alike)

A value is kept as-is only if its key is on the safe allowlist, or the
value itself is a `bool`/`int`/`float`/`None` (a toggle or a small limit,
never content). Everything else becomes a shape-only marker: `dict(n)`,
`list(n)`, or `str(len)`. This applies uniformly, so an unknown key in a
future Claude Code version degrades safely instead of leaking its value.

**Safe settings allowlist** (kept verbatim): `model`, `effortLevel`,
`outputStyle`, `autoCompactWindow`, `autoCompactEnabled`,
`promptCacheTtl`, `subagentPromptCacheTtl`, `cleanupPeriodDays`,
`desktopSessionCleanupPeriodDays`, `autoUpdatesChannel`,
`alwaysThinkingEnabled`, `includeCoAuthoredBy` (COV-09: a plain Boolean,
docs/en/settings-reference.md — deprecated since v2.0.62 in favour of
`attribution`, but still honoured by Claude Code until a layer sets
`attribution.commit`/`attribution.pr`, so still worth recording),
`maxEffortLevel` (PROF-03, a hard cap on effort level, same short
enum-like string posture as `effortLevel` itself), `fastMode` (PROF-08,
a plain Boolean, docs/en/settings-reference.md — a documented per-model
price premium, `pricing.toml`'s `[.fast]` tables, traded for a faster
reply; lets a "turn it off" profile candidate skip itself when it's
already off).

Four keys get their own summary shape instead of either "kept verbatim"
or the generic marker:

- `statusLine` → a bare `true`/`false` (a report only ever needs "is a
  statusline configured", never the command it runs).
- `modelPricing` → `{"present": bool, "model_ids": [...]}` — **the
  model ids it overrides, never the overridden numbers**. Those numbers
  are exactly the kind of "silently adopt whatever the file says" figure
  this project's own `pricing.toml` exists to keep user-editable and out
  of code.
- `attribution` (COV-09) → `{"commit_set": bool, "pr_set": bool,
  "session_url": bool|None}` — **never the free-text `commit`/`pr`
  trailer strings themselves**, since those can hold anything the layer
  author wrote; only whether each was customised, plus the plain
  `sessionUrl` Boolean verbatim.
- `modelSettings` (**PROF-03**) → `{model_id: {"effortLevel":
  str|None}}` — per-model effort overrides (`docs/en/settings-reference.md`):
  for each model id the layer names, only its own `effortLevel`, never
  any other sub-key a future Claude Code version might add per model.
  Model ids run through the same `_clip_name` every other name in this
  schema gets. A per-model `effortLevel` here, or `CLAUDE_CODE_EFFORT_LEVEL`
  being set at all (`content_layers.effort_level_env_set` above), both
  beat the top-level `effortLevel` scalar for that model — so a profile
  goal drafting an `effortLevel` change checks both and, when either
  applies, appends "Won't apply to `<model>`: … use `--effort` instead"
  to the candidate's evidence rather than silently implying the profile
  alone would move it (`profiles/goals.py`'s `_effort_override_note`).

`effective`/`effective_provenance` merge exactly this same key set
(`SETTINGS_SUMMARY_KEYS`) across the four layers — a value there has
already gone through the same redaction, so `effective["statusLine"]`
is a bool and `effective["modelPricing"]` is the present/model-ids shape,
never a raw value.

## Environment variables

Only variable **names** are ever recorded (`env_names`), never values,
with one deliberate exception below. Captured names are anything
matching a prefix in `ANTHROPIC_*` / `CLAUDE_*` / `OTEL_*`, plus a fixed
list of irregularly-named levers: `MAX_THINKING_TOKENS`,
`DISABLE_NON_ESSENTIAL_MODEL_CALLS`, `MAX_MCP_OUTPUT_TOKENS`,
`BASH_MAX_OUTPUT_LENGTH`. The `CLAUDE_*`/`ANTHROPIC_*` prefixes alone
already cover most of the documented Claude Code levers by name —
`CLAUDE_CODE_SUBAGENT_MODEL(_FORCE)`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`,
`CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`, `CLAUDE_CODE_USE_BEDROCK`/`_VERTEX`/`_FOUNDRY`,
`ANTHROPIC_DEFAULT_OPUS_MODEL`/`_SONNET_MODEL`/`_HAIKU_MODEL`/`_FABLE_MODEL` —
without needing an individual name added; `OTEL_*` covers OpenTelemetry
export configuration the same way.

**Exception — numeric caps** (`env_numeric_caps`): `MAX_THINKING_TOKENS`,
`MAX_MCP_OUTPUT_TOKENS`, `BASH_MAX_OUTPUT_LENGTH`, and
`CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` are limits, not secrets or content —
the integer value itself is recorded alongside the name. A value that
doesn't parse as an integer is silently skipped from
`env_numeric_caps` (the name still appears in `env_names`). Every other
captured environment variable stays names-only.

## Deep-merged `effective_*` collections (COV-03)

`effective`/`effective_provenance` only ever merge the *scalar*
`SETTINGS_SUMMARY_KEYS` (above) — one value per key, highest-precedence
layer wins outright. Five settings keys are not scalars: `env`,
`permissions`, `hooks`, `enabledPlugins`, and the
`enabledMcpjsonServers`/`disabledMcpjsonServers` pair. Reading only the
single highest-precedence layer's copy of these (schema 1's behaviour)
silently hides, e.g., a user-layer env var a project layer doesn't
mention, or a hook a lower layer adds on top of a higher layer's own
hooks. COV-03 adds six fields that deep-merge each of these with the
rule that key actually has, across all four layers in precedence order:

- `effective_env_names` / `effective_env_provenance` — the union of
  every layer's `env` block **names** (never values, matching
  `env_names`), each name's provenance the *highest* layer that sets it
  (per-name precedence, not whole-block precedence — a lower layer's own
  distinct names still count).
- `effective_permissions` — `{allow_count, deny_count, ask_count,
  default_mode}` summed as an **additive union of rule counts** across
  every layer (a permission rule is cumulative, not overridden), with
  `default_mode` taken from the highest layer that sets one.
- `effective_hooks` — `{event_name: entry_count}`, additive union of
  entry counts across every layer (a project's own hook doesn't replace
  a user's hook for the same event; both run).
- `effective_enabled_plugins` — plugin names from every layer's
  `enabledPlugins`, per-name highest-layer-wins (a lower layer can still
  enable a plugin a higher layer doesn't mention; a higher layer can
  disable one a lower layer enables).
- `effective_mcpjson_servers` — the union of every layer's
  `enabledMcpjsonServers` minus every layer's `disabledMcpjsonServers`
  (a rejection at any layer wins over an acceptance at any other layer).

`mcpServers` is **not** a real settings.json key at any scope (only
`managedMcpServers`, managed-layer-only, is) — an earlier reader that
merged a settings-file `mcpServers` block was merging a key Claude Code
itself never writes there; COV-03 fixed this dead-code path rather than
extending it.

`recommend.py`'s `_rule_baseline_bloat` now prefers
`effective_enabled_plugins` (deep-merged) over the older single-layer
`enabled_plugins` field when the snapshot has it, so a plugin-count
bloat recommendation reflects every layer's plugins, not just the
user layer's.

## `effective_agents`

`agents` (schema 1) is extended with `source` (`"user"` or `"project"`)
and, on a name clash, `shadowed_by_project: true` on the project entry
that won the merge (project agents win, matching Claude Code's own
resolution order). `effective_agents[name]` reduces this to exactly the
fields a TTL/model/effort recommendation keys off:
`{source, experimental_cache_ttl, model, effort, max_turns}`.

## `claude_json` — the `~/.claude.json` cross-check

`~/.claude.json` is the CLI's own per-machine state file — distinct from
any Claude Code *settings* file. Schema 2 reads the entry for the
current project, matched by comparing
`os.path.normcase(os.path.realpath(...))` on every project key against
the current directory: on a real machine this file has been observed to
hold the *same* directory under several spellings at once (forward
slashes, backslashes, drive-letter case), so an exact string match would
silently miss it. **The raw matching key is never stored** — only the
matched entry's own values.

When matched, `claude_json` records:

- `mcp_servers` — MCP server names from the project's own `mcpServers`.
- `enabled_mcpjson_servers` / `disabled_mcpjson_servers` — names.
- `allowed_tools_count` — a count, never the tool list.
- `has_trust_dialog_accepted` — a boolean.
- `last_session` — the project's own `last*` session-statistics fields,
  if present: `lastCost`, `lastDuration`, `lastAPIDuration`,
  `lastTotalInputTokens`, `lastTotalOutputTokens`,
  `lastTotalCacheCreationInputTokens`, `lastTotalCacheReadInputTokens`,
  `lastSessionId` (an opaque id, never message text), `lastLinesAdded`,
  `lastLinesRemoved`. `snapshots.claude_json_cross_check` joins this
  against this tool's own per-session totals by `lastSessionId`, and
  reports any numeric disagreement — a sanity check on both sides'
  accounting for the same session, never anything content-bearing.
- `top_level` — `num_projects` (the size of the `projects` dict) plus
  `scalars`: every top-level `~/.claude.json` field that is itself a
  `bool`/`int`/`float` kept verbatim, or a `str` reduced to `str(len)`
  (e.g. `numStartups`, `autoUpdates`). Dict/list top-level fields
  (`oauthAccount`, `tipsHistory`, …) are skipped entirely — their shape
  is unpredictable and could hold identity data, unlike the small fixed
  set of per-project fields handled above.

A missing, unreadable, or malformed `~/.claude.json` degrades to the bare
`{"matched": false}` — never raises. A `~/.claude.json` that reads fine
but has no entry for the current project still reports `"matched":
false`, but keeps the `top_level` block described above (there is
nothing project-specific to redact, but the file's own top-level scalars
are still safe to report): `{"matched": false, "top_level": {...}}`.

## `content_layers`

Sizes, counts and names only — **never content** — for every content
layer the plan's "Configuration layers" section lists:

- `claude_md` — byte counts for the user (`~/.claude/CLAUDE.md`),
  project root (`CLAUDE.md`), and project-local (`CLAUDE.local.md`)
  files, plus a bounded walk's count/total-bytes for nested `CLAUDE.md`
  files below the project root. The walk is depth-limited (≤6), visits
  at most 5,000 directories, stops after 1 second, and skips `.git`,
  `node_modules`, `.venv`, `bin`, `obj`, `dist`, `build`, `target`,
  `__pycache__`, `.next`, `vendor`, `Pods` and `packages` — so a huge or
  symlink-cyclic tree can't make a session start hang. COV-10: each of
  the three fixed files also gets a `user_imports` /
  `project_root_imports` / `project_local_imports` **count** of its own
  `@path/to/file` import references (`_count_claude_md_imports`) —
  never the imported paths themselves, just how many a file has, so a
  recommendation can flag an unusually import-heavy CLAUDE.md without
  ever reading what it imports.
- `rules` / `commands` — count and total bytes of
  `.claude/rules/*.md` / `.claude/commands/**/*.md`.
- `skills` — `{project, user}`, each `{names, total_bytes, config}` from
  `.claude/skills/*/SKILL.md` (a skill directory without a `SKILL.md`
  isn't a skill Claude Code will load, so it's excluded). COV-10/PROF-09:
  `config` is `{skill_name: {model, effort, context, paths_count}}` — a
  summary of each skill's own frontmatter (`model`/`effort`/`context`
  string values run through the same `_clip_name` every other *name*
  in this schema gets: a path/URL-shaped value is redacted to a shape
  marker, otherwise truncated to a max length — not restricted to a
  fixed enum, since a skill's `model`/`effort` can validly be an
  arbitrary short string a settings scalar cannot; `paths_count` a count
  of its `paths` list, never the paths themselves). **Never the skill
  body text or any free-text `description`** — a skill's prose is
  exactly the kind of content this hook must not record, matching the
  module-wide redaction rule.
- `agents_summary` — `{count, user_count, project_count,
  shadowed_count}`, rolled up from `agents`'s own source/shadow flags.
  COV-10: agent directories are now scanned **recursively** (a nested
  agent directory previously went uncounted).
- `mcp_json` — `{present, names}` for the project's own `.mcp.json`.
- `managed_mcp_present` / `managed_mcp` — `managed_mcp_present` is the
  bare boolean an earlier reader used; `managed_mcp` is the richer
  `{present, names}` shape added alongside it. Both now agree: COV-05b
  moved the lookup to the system managed-settings directory
  (`default_managed_settings_dir`) — the same place `managed-settings.json`
  lives — **not** `<claude root>`, which is where an earlier version of
  this scan incorrectly looked (a doc/code conflict; a real
  managed-mcp.json is a system-wide file, not a per-project one, so
  `claude_root` could never actually hold it).
- `plugin_content` (COV-10) — `{plugin_name: {skill_names, agent_count}}`
  for every installed plugin, a best-effort scan of each plugin
  directory's default `skills/`/`agents/` layout (skipped, not raised,
  for a plugin using a non-default layout) — names and counts only,
  never a plugin skill's own body text.
- `output_styles` — names from `<claude root>/output-styles/*.md`.
- `memory` — `{present, files, bytes}` for this project's auto-memory
  directory (`~/.claude/projects/<slug>/memory/`) — never its content.
- `plugins` — `{names, marketplaces}` — installed plugin directory
  names and a marketplace count, never plugin content.
- `claude_config_dir_set` — whether `CLAUDE_CONFIG_DIR` is set at all
  (never its value, which is a path).
- `effort_level_env_set` (PROF-03) — whether `CLAUDE_CODE_EFFORT_LEVEL`
  is set at all, never its value. It beats every settings-layer effort
  lever (`effortLevel`, `modelSettings.<id>.effortLevel`) and `--effort`/
  `/effort` too, so a profile candidate that would set `effortLevel`
  checks this flag first — see "PROF-03" under `effective` below.

## `project_slug`

The same algorithm `discovery.slug_for` uses (duplicated in the
standalone hook rather than imported — see that file's module
docstring): non-alphanumeric characters become `-`, truncated to 200
characters plus an 8-hex-char hash when longer. Honours
`CLAUDE_CODE_PROJECT_DIR_NAME` the same way `slug_for` does. The slug is
deliberately *not* treated as a raw path needing a hash — it's already
the on-disk directory name every transcript under
`~/.claude/projects/<slug>/` uses, and the non-alnum substitution means
it no longer contains a drive-letter colon or path separator.

The snapshot stores the slug hashed (`slug:` and the first 12 hex
characters of its SHA-256), with a leading Windows drive letter
upper-cased first. Claude Code writes the drive letter in whichever case
the folder was opened with, so `C--Dev-x` and `c--Dev-x` are one project
and get one key. A snapshot taken before 0.13.0 may carry the hash of the
lower-case spelling instead: `snapshots.snapshot_project_keys` gives both
keys, and the package matches a project's sessions to its snapshots and
changes under either.

## Multi-project tables (`snapshots.py`)

A single `<config-dir>/snapshots/` directory accumulates snapshots from
every project the hook has ever run in (exactly like
`~/.claude/projects/` itself), identified by each snapshot's own
`project_slug`. A schema-1 snapshot (no `project_slug`) collapses into
one `"(unknown project)"` bucket.

- `build_effective_config_table` — one row per (project, key) for each
  project's *latest* snapshot: the value in effect and which layer
  supplied it.
- `build_config_layers_table` — one row per (project, layer): whether
  that layer is present, plus a per-project content-layer summary
  (agent/skill/rule/command counts, total CLAUDE.md bytes, MCP server
  count) so a reader sees a project's whole config footprint in one
  table.
- `build_config_groups_table` — projects grouped by an identical
  *current* effective-config hash, with an optional session count per
  group when the caller supplies session data.
- `build_config_drift_table` / `detect_drift` — sessions whose
  *observed* behaviour (a caller-computed dominant model, TTL mix, or
  effort mode — this module never touches turn data itself) disagrees
  with what their joined snapshot's `effective` config says should be
  in effect. A mismatch implies a shell-profile environment variable or
  a `--settings` one-launch overlay the hook cannot see — evidence, not
  proof. COV-02: `report.py`'s `build_report` now feeds both halves —
  each session's top-level transcript's own dominant `model`
  (`_dominant_transcript_model`) *and* dominant `effortLevel`
  (`_dominant_transcript_effort`, new this phase) — into the
  `observed` dict, so the `config-drift` table (`config` section) is
  this schema's realisation of the plan's "a CLI/overlay layer, inferred
  when the transcript's model or effort disagrees with the settings":
  there's no separate named layer in the schema itself (nothing at
  session-snapshot-capture time can know what a later `--model`/
  `--effort` flag or shell env var will do), but a `config-drift` row is
  exactly that inference, reported per session/key. Model comparisons
  (`_settings_model_agrees`) follow what the setting can mean. `default`
  names no fixed model, so it never counts as drift. `opusplan` accepts
  an observed Opus or Sonnet model. An alias (`opus`, `sonnet`, `haiku`,
  `fable`, `best`, with or without `[1m]`: any value with no digit once
  `[1m]` is stripped) compares by family (`workstyle.model_tier`, with
  `best` read as Fable through the rate card), because an alias follows
  the newest release: `opus` against an older `claude-opus-5` session is
  not drift. Every other value (a `claude-*` id, a Bedrock or Vertex id,
  an ARN) is an explicit pin and compares exactly, by the canonical id
  `pricing.resolve_model` gives both sides (fix #15), so a pin moved from
  Opus 4.1 to 4.5 still reports. A newer release the rate card has no
  row for yet keeps its own version (`pricing.newer_version_id`), so a
  `claude-opus-5-7` pin against a `claude-opus-5` session still reports
  even while both are priced as `claude-opus-5`. Effort comparisons are
  plain equality, since `turn.effort` already uses the same enum
  `effortLevel` does (`low`/`medium`/`high`/`xhigh`/`max`).
- `build_env_levers_table` (COV-09) — one row per COV-09 env-var lever
  (`DISABLE_PROMPT_CACHING` and its per-model variants,
  `ENABLE_TOOL_SEARCH`, `CLAUDE_CODE_MAX_OUTPUT_TOKENS`,
  `CLAUDE_CODE_SUBAGENT_MODEL`, `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`,
  `CLAUDE_CODE_AUTO_COMPACT_WINDOW`) plus
  `attribution`/`includeCoAuthoredBy`, each row `{name, present, value}`
  off the corpus-wide snapshot (`snapshots_mod.with_every_project_agents`)
  — a small, lever-name-keyed table built specifically so
  `recommend.py`'s new COV-09 rules (below) have an unambiguous evidence
  row to cite, since the project-keyed `build_effective_config_table`
  can't be cited for one specific key (`_row` matches only on
  `row[0]` == the row key, and that table's row key is the project, not
  the settings key).
- `project_chains`, `changed_keys` and `build_config_diff_table` —
  what changed between snapshots is always read within one project's own
  run of snapshots (`project_chains`, oldest first, a project filed under
  both drive-letter keys being one chain). The hook records one
  snapshot per session start, in whichever project the session opened,
  so a single list across projects would read every switch from one
  project to another as a settings change. `changed_keys` is every
  flattened key that changed in some project's chain, each key counted
  once however many projects changed it, and it is what the `config`
  section's changed-settings count, the scorecard's config stability and
  `config-diff --auto-keys` use. The snapshots are every one recorded,
  not only the window's, so a setting you changed last month still counts
  in a 7-day report.
  `build_config_diff_table`'s note on keys that changed alongside the
  one you named looks at the same project's two consecutive snapshots.
  With a project picked (or the folder you ran in, on the CLI), only that
  project's snapshots are read; with all projects, every project's chain
  is diffed on its own.

`build_config_section(..., include_effective=True, sessions_with_observed=...)`
appends these tables to the existing config-diff section; both keyword
arguments are optional and off by default, so an existing caller keeps
getting exactly the one table it always has.

## COV-09 recommendation rules and `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`

Five `recommend.py` rules read the env-var/deprecated-setting levers
above and cite `config.env-levers` as their evidence. None of them
populate `Recommendation.changes`/`SettingChange` — an env var has no
single canonical file to write to (a shell profile vs. a settings.json
`env` block is a genuinely ambiguous choice `apply.py` doesn't resolve
today), so each rule's `action` text is fully self-contained prose
stating what changes, where, the trade-off, and how to undo it, and
`lever` is a descriptive `"env:NAME"` placeholder string for a future
apply-side wiring:

- `env-disable-prompt-caching` (severity `action`) — fires when any of
  `DISABLE_PROMPT_CACHING`/`_SONNET`/`_OPUS`/`_HAIKU`/`_FABLE` is set.
- `env-tool-search` (severity `advice`) — fires when
  `ANTHROPIC_BASE_URL` is set (a proxy is plausibly in use) without
  `ENABLE_TOOL_SEARCH`, and the snapshot's MCP/plugin count clears the
  existing `baseline_bloat_min_mcp_or_plugins` threshold (default 5) —
  reused rather than adding a new threshold field, matching
  presence-based rules elsewhere in the module. The action text caveats
  that a snapshot can't distinguish a first-party vs. proxy
  `ANTHROPIC_BASE_URL`.
- `env-max-output-tokens` (severity `advice`) — fires when
  `CLAUDE_CODE_MAX_OUTPUT_TOKENS` is set (`env_numeric_caps`), citing
  the compactions-per-session mean as secondary evidence when present.
- `env-subagent-model` (severity `info`) — fires when
  `CLAUDE_CODE_SUBAGENT_MODEL` is set, for an archetype that actually
  spawns subagents. States the exact resolution order
  (docs/en/sub-agents.md "Choose a model"): 1) a per-invocation `model`
  parameter, 2) the subagent's own `model` frontmatter (including
  `inherit`), 3) `CLAUDE_CODE_SUBAGENT_MODEL`, 4) the main conversation's
  model — and that setting it alone does **not** change what the
  built-in Explore/Plan subagents run on.
- `env-attribution-deprecated` (severity `info`) — fires when
  `includeCoAuthoredBy` is set in `effective` and `attribution` is not
  (docs/en/settings-reference.md: `attribution` replaces the deprecated
  `includeCoAuthoredBy`, which Claude Code still honours only until a
  layer sets `attribution.commit`/`attribution.pr`).

`CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` additionally feeds the *compaction
simulation* (`compaction_sim` section), not just a recommendation:
`report._apply_autocompact_pct_override` scales a session's configured
`autoCompactWindow` down by the override percentage (docs/en/env-vars.md:
"1-100"; a value outside that range, or missing, leaves the window
unscaled — the override only ever narrows the window, never widens it)
before `simulate_compaction_windows` runs, so the simulation reflects
the window a session actually ran under rather than the nominal
unscaled setting.

**P7b note (implemented)**: these five recommendation ids now offer a
real `--dry-run` command instead of prose-only instructions, for the
three whose proposed value is concrete enough to state (`env-tool-
search`, `env-disable-prompt-caching`, `env-max-output-tokens`); the
other two stay prose-only on purpose (`env-subagent-model` proposes no
value at all, and `env-attribution-deprecated`'s real target,
`attribution.commit`, isn't on `SETTINGS_ALLOWLIST`). The wiring: each
rule populates `changes` with a `SettingChange(target="settings",
key="env.NAME", ...)`; `fixes.py`'s `command_for`/`prompt_for`/`_where`
render that as an `apply --set env.NAME=value --dry-run` command and
matching settings.json-`env`-block prompt text; `apply.py`'s
`plan_apply` merges a profile's (or a `--set env.NAME=value` one-off's)
`env` entries into the target settings file's own `"env"` object,
exactly like any other settings key — no more separate print-only
path (see `apply.py`'s module docstring for the full deviation note,
which also corrects `docs/profiles.md`'s now-stale "printed, never
written" section).

## CLI

- `claudeglass snapshot-config --project-dir PATH` — runs the same
  hook logic for an explicit project directory instead of the current
  one. (Named `--project-dir`, not `--project`: the common `--project`
  flag every subcommand already has means "a repeatable project slug to
  filter a report by" — reusing it here for a single directory path
  would collide with that meaning.)
- `claudeglass probe-config [--project-dir PATH]` — runs the same
  scan **without** a session and without writing a snapshot file; prints
  the settings-layers and effective-config tables as Markdown. Defaults
  to the current directory. Its output never contains a raw path —
  only the project slug and content hashes, matching the hook's own
  privacy posture.

## What `init` could ask (not built yet)

`init` exists (`onboarding.py`), but it does not read this scan yet. The
plan is for it to use the same schema-2 scan to open with a
project-specific summary instead of a blank slate:

- Which settings layers are present for this project, and which one
  currently wins each effective-config key — so a suggested change can
  say *"set in `.claude/settings.json`, currently overridden by your
  user settings"* instead of guessing.
- Whether this project's agents are entirely inherited from `~/.claude/
  agents/`, or already has project-level overrides (`shadowed_by_project`)
  worth confirming before `init` proposes new ones.
- Whether a `.mcp.json`/managed MCP config already exists, so `init`
  doesn't propose a redundant server list.
- The project's current CLAUDE.md/rules/commands footprint (byte counts
  only), to calibrate whether `init` should propose splitting a large
  `CLAUDE.md` into `.claude/rules/*.md` files.
- Whether `~/.claude.json` already has a trust-dialog-accepted entry and
  prior session totals for this project, so `init` can skip
  onboarding questions Claude Code itself has already answered.

None of this reads message text, file contents, or raw paths — the same
guarantee schema 2 already provides for reporting.

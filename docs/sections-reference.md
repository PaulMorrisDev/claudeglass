# Report sections, in detail

This file lists every report section at a glance, then every table
each `build_section(...)` function produces today. It also covers the
TTL section's utilisation metrics, the recommendations, and a worked
example against a real, scrubbed transcript. [`cli.md`](cli.md) lists
the commands that print them.

`report.build_report` assembles these sections into one `ReportModel`,
in this order: `overview`, `usage`, `elasticity` (only under
subscription billing with usage-log readings), `sessions`, `recache`, `ttl`,
`limits`, `carry`, `compaction_sim`, `plan_handoff`, `model_swap`, `waste`,
`compactions`, `agent_startup`, `agents`, `run_split`, `hooks`, `quality`, `workstyle`, `habits`,
`workflows`, `phases` (CLI only with `--phases`; the dashboard always has it), `config` (only when config
snapshots exist), `context_budget`, `tool_search`, `capture`, `cost_record`, `scorecard`, and
`baseline_comparison` (only with `--baseline`). `claudeglass
report` prints it. This file groups sections by topic, so its order
differs.

These sections are not part of the assembled report: `config_diff`,
`compare`, `reconcile` and `team_report` (each printed by its own
subcommand), and `usage_windows` and `savers` (no subcommand prints
them yet; call their `build_section` directly).

Table names below are the exact `Table.name` values. A table's CSV
export is `<section key>__<table name>.csv`, and a recommendation's
evidence cites it by section key and table name. Each section can also
be produced by calling the named module's own `build_section` function
directly against `TranscriptResult`/`SessionRecord` objects from
`parse_transcript`, which is what the worked example at the end of this
file does.

## Sections at a glance

`python -m claudeglass report` prints every section below, in the
order of this table: Markdown by default, or `--json`, `--html` and
`--csv-dir` for the other formats (see [`cli.md`](cli.md#report)). Each
section is also, on its own, a `build_section(...)` function returning a
`Section` of `Table`s ([`model.py`](../src/claudeglass/model.py)),
fully tested and runnable from a short Python script against your own
transcripts. That is how the [worked example](#worked-example) was made,
and it's still useful when you want one section by itself.

| Section key | Title | Module | What it answers |
|---|---|---|---|
| `overview` | Overview | `report.py` | corpus-wide totals (sessions, transcripts, turns, the four raw token counts, cost, cache-read cost share, cache ROI) plus a per-model breakdown |
| `usage` | Usage | `usage.py` | day/week/month/project/entrypoint cost and token breakdowns, plus five-hour usage blocks (subscription billing only — see [`reference.md`](reference.md#what-it-reads-and-what-it-cant)) |
| `elasticity` | Elasticity | `elasticity.py` | how many percentage points of a subscription's 5-hour/7-day/spend-limit window one million tokens (or one list-price dollar) is actually worth, measured from this machine's own usage-log samples — subscription billing only, and only when a usage log exists (see [`elasticity.md`](elasticity.md)) |
| `sessions` | Sessions | `classify.py` | mode (interactive/long-agentic/overnight/mixed) and purpose (docs/refactor/test-triage/...) per session, with the evidence that produced each classification |
| `recache` | Re-cache events | `recache.py` | which turns paid to re-write a prefix that should have been a cache hit, why, and what it cost — see [`concepts.md`](concepts.md#3-cache-rebuild-definitions-and-signatures) |
| `ttl` | Cache TTL break-even | `ttl.py` | per agent type: observed cost vs. simulated 5m-only/1h-only cost, plus the utilisation metrics below |
| `limits` | Usage limits | `limits.py` | usage-cap pauses (5-hour/weekly), harness-forced subagent terminations, and the desktop app's resume pings, as first-class attributable facts instead of behavioural noise — see [`limits.md`](limits.md) |
| `carry` | Context carry cost per tool | `carry.py` | cost of a tool result riding along in the cached prefix on every turn after the one it entered on, by tool and by agent type, plus the saving a truncation cap would have made — see [`carry.md`](carry.md) |
| `compaction_sim` | Compaction-window sweep | `compaction_sim.py` | modelled cost under other `autoCompactWindow` settings, a fidelity check against each session's actually-configured window, and a conservative "at least W" recommendation — see [`compaction-sim.md`](compaction-sim.md) |
| `plan_handoff` | Building fresh after a plan | `handoff.py` | what the replies after each approved plan would have cost in a fresh session started from the plan alone, and the same build at Sonnet's prices — see [`plan-handoff.md`](plan-handoff.md) |
| `model_swap` | Model-swap counterfactual | `model_swap.py` | ceiling saving from repricing every already-observed turn one model tier down, per agent type and corpus-wide — see [`model-swap.md`](model-swap.md) |
| `waste` | Wasted-turn spend | `waste.py` | spend on turns whose output was never used (tool error, interrupt, tool denial, harness-killed subagent), by cause, agent type and top session — see [`waste.md`](waste.md) |
| `compactions` | Compactions | `compaction.py` | compaction count, trigger mix, pre/post/dropped tokens, and the re-cache cost of the turn right after each compaction |
| `agent_startup` | Subagent startup | `context_budget.py` | what each agent type is given before its first turn, what it was given but never used, and what every agent type receives alike |
| `agents` | Agents and information flow | `topology.py` | downward cost (briefing/system-prompt writes into each agent type), upward cost (`Agent`/`Workflow` tool-result sizes flowing back), skill roll-ups, spawn-depth chains |
| `run_split` | Splitting long subagent runs | `run_split.py` | what long subagent runs would have cost as several shorter runs, each starting fresh from a short note, at several split intervals, and the interval that saves most per agent type — see [`run-split.md`](run-split.md) |
| `hooks` | Your hooks | `hook_costs.py` | whether each hook you set up works (failed runs and why, relative script paths), what the context it adds costs to keep, what the calls it blocks cost and how often Claude sent them again unchanged, and time waited — see [`hooks.md`](hooks.md) |
| `quality` | Quality signals | `quality.py` | whether the work went well: agent runs that didn't finish or likely ran out of turns, failed tool calls and shell commands, denials, corrections, edits redone, per agent type and per model and effort, with a significance test — see [`concepts.md`](concepts.md#7-quality-signals) |
| `workstyle` | Workstyle | `workstyle.py` | one archetype per session/corpus: `overseer-fanout`, `plan-high-implement-low`, `workflow-heavy`, `effort-varied`, `chat-only`, `single-model`, `mixed` (the fallback when none of the other six match), with the evidence features |
| `habits` | Work habits | `habits.py` | the "Weekly pace" digest, habits worth trying with a saving estimate and evidence, per-task and per-agent setup comparisons, and (once you rate sessions or use `/tl-feedback`) cost per piece of work that met its goal |
| `workflows` | Workflows | `workflows.py` | per-run agent count, phase count, duration and cost from `<session>/workflows/wf_*.json` |
| `phases` | Phases | `phases.py` | cost split across DISCOVERY (read/search only), IMPLEMENTATION (real edits or an ordinary shell command), VERIFICATION (a test/build tool, or a scratch-file edit), OTHER — in the CLI's report only when `--phases` is given; the dashboard always builds it |
| `config` | Config | `report.py` via `snapshots.py` | one diff table per config key that changed across the window's snapshots (capped at 20 keys) — only present when `snapshot-config` snapshots exist for the window |
| `context_budget` | Context budget | `context_budget.py` | an estimated breakdown of what a session's context window is spent on before any real work (system prompt and tools, skills, memory files, custom agents, MCP tools), plus ground truth where the statusline logged it |
| `tool_search` | What tool search saves | `tool_search.py` | how many tool definitions MCP tool search kept out of each request, by MCP server, what that saved at each reply's own cache rate, and the net after the name list and the replies that only searched — see [`tool-search.md`](tool-search.md) |
| `capture` | Capture | `habits.py` | what metrics capture has cost since it was turned on, measured from the transcripts, and what the habits and feedback that depend on it are worth a week — see [`capture.md`](capture.md) |
| `cost_record` | Claude Code's own cost record | `reconcile.py` | whether ClaudeGlass's cost matches what Claude Code itself recorded for the same sessions, over the same span, with the known reasons they differ split out — on Data quality, and `claudeglass check cost-record` |
| `scorecard` | Scorecard | `scorecard.py` | five 1-5 levels (cache efficiency, context hygiene, agent efficiency, config fit, data quality) plus an overall level (the minimum of the first four, never an average) |
| `baseline_comparison` | Baseline comparison | `report.py` via `baseline.py` | before/after the last captured onboarding baseline, plus a per-mode breakdown — only present when `--baseline` resolves one (added unconditionally, even on the single-section subcommands) |

Two more parts of the report aren't sections, so they aren't in the
table. [Recommendations](#recommendations-recommendpy) is its own
`ReportModel.recommendations` list, and [Diagnostics](#diagnostics-reportmodeldiagnostics)
(parse-quality counters) is `ReportModel.diagnostics`, which every
renderer prints directly rather than as a table.

`config-diff --key K` prints one standalone plain-text table from
`snapshots.build_config_diff_table` (see [`config_diff`](#config_diff-snapshotspy)).
It doesn't go through `build_report`, so it isn't the same code as the
report's own `config` section.

## `overview` (`report.py`)

- `totals` — one `metric`/`value` row per corpus-wide total: sessions,
  top-level/subagent transcripts, workflow runs, priced turns, the four
  raw token counts (`input_tokens`, `cache_creation_tokens`,
  `cache_read_tokens`, `output_tokens`), the two derived totals from
  [Concepts section 1](concepts.md#1-the-two-token-totals)
  (`usage_tokens`, `new_tokens`), `total_cost_usd`,
  `cache_read_cost_share_pct` (cache-read cost as a percentage of total
  cost), `cache_roi` (from `ttl.py`'s cache-economy totals — see
  [`ttl_cache_economy`](#ttl-utilisation-metrics) below), and two
  top-level-only context figures: `top_level_median_ctx` and
  `top_level_turns_ctx_ge_200k_pct` (the share of top-level turns at
  200,000 tokens of context or more).
- `by_model` — turns, the four raw token counts and cost, one row per
  model id, sorted by cost descending.

## `usage` (`usage.py`)

Finance/enterprise-facing breakdowns by calendar period, project,
entrypoint, and (subscription billing only) a fixed 5-hour local-calendar
block grid — see [what it reads, and what it can't](reference.md#what-it-reads-and-what-it-cant)
for the billing-mode distinction every money column in this section
respects.

- `by_day` / `by_week` / `by_month` — period x model:
  turns, tokens, cost. The period key is computed in `config.tz` (falling
  back to the machine's own local zone).
- `by_project` — sessions and cost per project slug.
- `by_entrypoint` — transcripts, turns, tokens, cost per
  `entrypoint` (e.g. `claude-desktop`, `claude-code`).
- `five_hour_blocks` — sessions, turns, tokens, cost per fixed
  00:00/05:00/10:00/15:00/20:00-local block, populated only when
  `config.billing == "subscription"` (a genuine per-account rolling
  5-hour window can't be observed from transcripts alone, so this is a
  documented, deterministic proxy grid instead); under `"api"` billing
  the table is empty with a one-line note explaining the skip.
- `pricing_unknown_models` — one row per model id that `pricing.toml`
  has no price for: `model_id`, `turns`, `tokens`. Those replies are
  priced at zero. Built by `pricing.PricingCoverage.as_table` from the
  same coverage count behind `meta.pricing.coverage_pct`, and appended
  onto this section by `report.build_report` only when at least one
  reply was unpriced. The `pricing-coverage` recommendation names these
  model ids. Shown on Spend › Usage under More tables.
- `pricing_closest_match` — one row per model id that resolved only via
  `pricing.Pricing.resolve_model`'s longest-registered-id *prefix* step
  (`ResolvedRates.approximate`; not `"exact"`/`"alias"`/`"strip_1m"`, and
  not a `"cloud_strip"` that itself landed on an exact/alias id):
  `model_id`, `priced_as` (the registered id whose rate was used),
  `turns`, `tokens`. These replies count as priced (`coverage_pct`
  treats them as covered), but only at another, similar model's rate —
  make this visible instead of letting a report read as "every model has
  its own price" when `coverage_pct` is 100%. Built by
  `pricing.PricingCoverage.as_closest_match_table`, appended onto this
  section only when at least one reply matched this way, and named by
  the `pricing-coverage` recommendation alongside (or instead of) any
  unpriced model ids. Shown on Spend › Usage under More tables.
- `pricing_fast_priced_as_standard` — one row per model id seen with at
  least one reply flagged `usage.speed == "fast"` whose rate card entry
  has no `[models."<id>".fast]` table, so it was priced at that model's
  standard rate instead: `model_id`, `turns`, `tokens`. Built by
  `pricing.PricingCoverage.as_fast_priced_as_standard_table`, appended
  onto this section only when at least one such reply exists. Shown
  on Spend › Usage under More tables.
- `pricing_fast_applied` (PROF-08) — the mirror image of the table
  above: one row per model id seen with at least one reply *actually*
  billed at its `[models."<id>".fast]` rate: `model_id`, `turns`,
  `tokens`, `cost` (what was actually billed), `standard_cost` (what the
  same replies would have cost at that model's standard rate instead,
  with the flat per-request server-tool fee — never scaled by the fast
  multiplier — added back unscaled on both sides). Built by
  `pricing.PricingCoverage.as_fast_applied_table`, appended onto this
  section only when at least one such reply exists. Shown under
  More tables on Spend › Usage; read by `whatif._fast_mode` to price a
  profile's `fastMode = false` candidate.
- `cache_ground_truth` (S1-exports) — one row per session: `session_id`,
  `rows_logged`, `warm_share` (percentage of *logged rows* — statusline
  refreshes, not wall-clock time — where `statusline.py`'s real,
  non-estimated `prompt_cache.warm` was `true`; refreshes aren't evenly
  spaced in time, so this can diverge from the share of wall-clock
  session time spent warm), `misses` (the peak/max `prompt_cache.misses`
  observed), `top_miss_causes` (a short `cause:count` summary, e.g.
  `ttl:2, tools:1`), and `mean_recache_tokens_if_cold`. Built by
  `statusline.py`'s `build_cache_ground_truth_table` from the usage-log
  CSV's `cache_*` trailing columns and appended onto this section by
  `report.build_report` only when it is given `usage_log_rows` (see
  below) — absent otherwise, same as `context_budget_statusline`.
  `top_miss_causes` (fix for review finding 5) is read from the wire's
  own cumulative `prompt_cache.miss_causes` per-cause counts (persisted
  as the `cache_miss_causes` column) whenever a session's log carries
  that field, rather than counting the sticky `last_miss_cause` once per
  logged row — the old approach re-counted one real miss on every quiet
  subsequent refresh, since `last_miss_cause` stays set until the next
  miss. Logs with no `cache_miss_causes` data at all (old-format rows)
  fall back to counting `last_miss_cause` only on a row where
  `misses` genuinely increased over the previous row for that session.

Every money column's label switches to "Cost (list-price equivalent)"
under subscription billing, and a section note repeats that these are
not real invoice lines.

## `sessions` (`classify.py`)

- `sessions_by_mode` — sessions, turns, subagents, median span and
  median human prompts per `mode`. First match wins, in this order:
  `overnight` (span over 4h once usage-limit pauses are discounted, a
  human gap over 60min, and real activity in the local 22:00-07:00
  window), `long-agentic` (a self-chained run, or subagents or at least
  30 turns with at most 10 human prompts), `interactive` (median human
  gap under 5min with at most 2 subagents), else `mixed`. A section note
  states the overnight window in use.
- `sessions_by_purpose` — the same columns by `purpose`. First match
  wins, in this order: `local-llm-pipeline`, `workflow-run`, `review`,
  `test-triage`, `planning`, `docs-or-light-edit`, `refactor`,
  `agent-fanout`, else `general-dev`. While metrics capture is on, the
  kind of task Claude reported (`task=`) for at least half of a
  session's tagged messages (two or more) decides the purpose where it
  maps one to one: `review`, `test` (`test-triage`), `plan`
  (`planning`), `docs` (`docs-or-light-edit`) and `refactor`. The two
  structural purposes, `local-llm-pipeline` and `workflow-run`, still
  win.
- `sessions_detail` — the 50 most recently started sessions, one row
  each: project, mode, purpose, sources (whether each came from a
  `sessions.toml` override, the rule engine, or for the purpose, the
  task Claude `reported`), start time, span, turns and subagent count.

Per-session `mode`/`purpose` overrides live in
`<config-dir>/sessions.toml` (`<config-dir>` defaults to
`~/.claude/claudeglass`) and always win over the rule engine
(`config.load_session_overrides`). Tags set in the dashboard's session
drawer (Spend › Sessions) win over `sessions.toml`, in the dashboard and
in the CLI's `report`, `compare` and `config-diff`, which read them from
`<config-dir>/service.db` without writing to it
(`service.store.read_session_marks`).

## `recache` (`recache.py`)

Definitions: [Concepts section 3](concepts.md#3-cache-rebuild-definitions-and-signatures).

- `recache_summary` — transcripts, priced turns, re-cache turns and
  share, cache-creation tokens (re-cache vs. all), avoidable cost, and
  `unavoidable_limit_expiry_cost_usd` (re-cache turns right after a
  usage-limit pause, kept out of avoidable cost).
- `recache_signature_split` — `full-expiry`, `prefix-invalidated` and
  `limit-expiry` (the gap spanned a usage-limit pause): turns,
  cache-creation tokens, avoidable cost, median ctx, median gap. Every
  cause table below leaves `limit-expiry` turns out.
- `recache_gap_buckets` — re-cache turns and their control-group share
  (all priced turns), bucketed by inter-turn gap (`<1m`, `1-5m`, `5-15m`,
  `15-60m`, `>60m`, `unknown`), both by turn count and by cache-creation
  token volume — the control columns are what make an over-representation
  claim evidence rather than noise.
- `recache_preceding_tool` — the same shape, bucketed by the tool that
  immediately preceded the re-cache turn.
- `recache_top_command_prefixes` — the top preceding Bash/PowerShell
  command prefixes (≤40 chars) among re-cache turns.
- `recache_primary_cause` — re-cache turns and cache-creation tokens by
  `preceding_primary` event kind, each row's turn/token share compared
  against its control share, with an explicit
  "over-representation" column (`share - control_share`).
- `recache_primary_cause_prefix_invalidated` — the same, restricted to
  `prefix-invalidated` turns, with shares taken within that subset.
  Full-expiry turns are left out because their cache had fully expired
  whatever preceded them; a prefix-invalidated turn is the one a
  preceding event can actually explain.
- `recache_event_cooccurrence` — every event kind's presence (not just
  the precedence-resolved primary) among re-cache turns vs. control, so
  the precedence order never hides a contributing cause.
- `recache_attachment_subsplit` — attachment `subkind` breakdown for
  `prefix-invalidated` turns specifically.
- `recache_by_agent_type` — priced turns, re-cache turns and share,
  cache-creation tokens, avoidable cost, per agent type (`"top-level"`
  for the main conversation).
- `recache_huge_context` — turns at or above `huge_ctx` (default
  200,000 tokens) and their share of total cache-read volume: a
  context-hygiene metric, not a pricing surcharge (current-generation
  models bill the full context window at standard rates).
- `recache_by_group` — only with `report --group-by`: the
  `recache_summary` columns, one row per group.
- `measured_miss_causes` — only when the usage-log CSV carries the
  statusline's cache-miss causes: the main session's cache misses by the
  cause Claude Code itself reported (misses, share, sessions), to set
  beside the causes inferred above.

## `ttl` (`ttl.py`)

Simulation assumptions: [Concepts section 4](concepts.md#4-ttl-simulation-assumptions).

- `ttl_by_agent_type` — per agent type and `"top-level"`: spawns, priced
  turns, observed 5m/1h mix, gaps > 5min, gaps > 60min, limit gaps, gap
  p50/p90, cost observed / all-5m / all-1h, best policy, delta vs. best
  (USD and %), saving if switched, fidelity, unsimulatable and unpriced
  turns, a recommendation string, and the lever (`promptCacheTtl` for
  the top-level row; `subagentPromptCacheTtl` for the `"unknown"` row,
  which has no agent file; otherwise `experimental.cacheTtl` in
  `<agent>.md`, or `subagentPromptCacheTtl` for all subagents). A
  subscription gets the same advice as pay-per-token billing, plus a
  note on what changes while the plan is on usage credits
  ([Concepts section 2](concepts.md#2-how-caching-works-in-claude-code)).
- `ttl_gap_distribution` — inter-turn gap histogram per agent type.

### TTL utilisation metrics

- `ttl_wasted_writes` — per agent type: `writes`, `wasted_writes`
  (never read back before the entry expired), `tokens_written`,
  `tokens_wasted`, `share` (wasted tokens as a percentage of tokens
  written, leaving out each transcript's last write), `usd_wasted`, and
  `terminal_writes` (those last writes, counted separately).
- `ttl_premium_waste` — for turns using a 1h TTL: tokens/USD where the
  extra write premium was never earned back by a hit a 5m TTL would have
  missed (`h1_not_needed_*`) vs. tokens/USD where it was earned
  (`h1_earned_*`) vs. tokens/USD where the 1h entry expired anyway
  (`h1_expired_*`); symmetrically for 5m turns, `m5_fine_tokens` (no
  loss) vs. `m5_loss_*` (expired when a 1h TTL would have survived) and
  `m5_would_expire_tokens` (would have expired under 1h too).
- `ttl_break_even_share` — `premium_all_1h`/`expiry_loss_all_5m` (what
  every write would have cost end to end under each fixed policy),
  `margin` (`expiry_loss_all_5m - premium_all_1h`, positive means 1h
  wins), `in_window_pct` (the prefix-weighted share of gaps between 5
  and 60 minutes) against `break_even_pct` (the share at which 1h starts
  to pay), and a `verdict`: `"marginal"` when the margin is within 5% of
  the larger side or under $1.00, else `"1h pays"` or `"5m pays"`.
- `ttl_near_miss` — turns whose gap fell within `near_miss_window_s`
  (default 60s) of a TTL boundary, on the side that just missed and the
  side that just made it, for both the 5m and 1h boundaries.
- `ttl_addressable_share` — per agent type, re-cache tokens, USD and
  share split by signature: `full_expiry_*` (TTL-addressable: the entry
  expired, so a longer TTL could have kept it) vs.
  `prefix_invalidated_*` (content-addressable: something upstream of
  the cached prefix changed, which no TTL choice fixes).
- `ttl_cache_economy` — per agent type plus an `overall` row:
  `tokens_written`, `tokens_read`, `write_usd`, `read_usd`,
  `uncached_equivalent_usd` (every cache token priced as plain input),
  `net_saving_usd` (uncached-equivalent minus write and read USD), and
  `cache_roi` (net saving divided by write USD) — a standalone "is
  caching worth it at all" number.

Fidelity self-check: `ttl.dominant_ttl`/`ttl.fidelity` replay the
simulation at a transcript's own dominant observed TTL and compare it to
observed cost; `TtlThresholds.fidelity_warn_pct` (default 10.0) is the
flag threshold `build_section` applies per agent type.

## `limits` (`limits.py`)

Full field-by-field contract: [`docs/limits.md`](limits.md#the-limits-report-section).

A usage-cap pause (the harness pausing when the account hits its 5-hour/
weekly limit), a harness-forced early subagent termination, and the
desktop app's resume ping, turned into first-class facts rather than
behavioural noise (see `docs/limits.md`'s module-docstring summary for
why an unattributed pause otherwise misreads as an ordinary long idle
gap in `recache`/`ttl`/`sessions`).

- `limits_summary` — one "all" row: transcripts, sessions affected,
  limit hits (session + weekly split), resumes, agents terminated (and
  by rate limit specifically), pause count/total time, and the
  cache-creation tokens/write cost paid by the turn immediately
  following each pause.
- `limits_hits_by_kind` — `session_limit`/`weekly_limit` hit counts and
  share.
- `limits_agent_terminated` — `rate_limit`/`other` termination counts
  and share.
- `limits_pauses` — corpus-wide pause count/total/mean duration.
- `limits_reset_hour_histogram` — count and share of `LIMIT_HIT` resets
  by local hour of day (0-23).
- `limits_by_agent_type` — per-agent-type roll-up: hits, resumes,
  terminations, pause count/total/median/max, and the post-pause
  cache-creation tokens/cost.
- `limits_csv_cross_check` — transcript-derived hit counts vs.
  `usage-log.csv`'s own exhaustion-row counts for `five_hour`/
  `seven_day`, appended as an extra table on this section only when
  `report.build_report` is given `usage_log_rows` (same "extra table
  bolted on" convention `cache_ground_truth` uses for the `usage`
  section above).

`scorecard.py`'s `cache_efficiency` dimension excludes the portion of
re-cache share already known to be pause-forced
(`ScorecardInputs.limit_recache_share_pct`); `data_quality` notes the
count of sessions with at least one pause
(`ScorecardInputs.limit_pause_sessions`) and, separately, the count of
turns counted as priced above that were only priced by closest match
rather than their own model's rate
(`ScorecardInputs.closest_match_turns`, from
`pricing.PricingCoverage.closest_match_turns` — see the `usage`
section's `pricing_closest_match` table above). Neither note changes
the `pricing_coverage_pct` metric or level itself, which already counts
a closest-match turn as priced. `recommend.py`'s `limit-pressure` rule
fires off this section's own `limits_summary` counts.

## `carry` (`carry.py`)

Full field-by-field contract: [`docs/carry.md`](carry.md#the-carry-report-section).

Every other section prices a tool result once, at the turn it entered
context. `carry` prices it again for every later turn it keeps riding
along inside the cached prefix — re-read at the flat `cache_read` rate,
or re-written at a `cache_write_5m`/`cache_write_1h` rate on a re-cache
— until a `COMPACT_BOUNDARY` drops it or the transcript ends.

- `carry_by_tool` — per tool name: carried-result count, tokens
  entered, mean turns carried, carry tokens, carry cost, and that
  tool's carry-token share of the corpus's total cache volume (an
  attribution share, not a partition — rows need not sum to 100%,
  since one physical cache read carries every still-live result in
  that turn's prefix at once).
- `carry_by_agent_type` — the same roll-up keyed by agent type
  (`"top-level"` for the main session).
- `carry_top_results` — the single most expensive individual carried
  results corpus-wide: tool name, agent type, tokens, turns carried,
  cost — no content, path, or command.
- `carry_truncation_savings` — for each configured cap in
  `CarryThresholds.truncation_tokens` (default 2,000 and 8,000 tokens):
  how many carried results exceed it and the exact tokens/USD saved had
  every one been capped there, computed by linear scaling rather than
  re-simulation (carry cost is exactly proportional to a result's own
  token size for a fixed run of later turns).
- `carry_output_cap_savings` — the same saving for each output-cap
  setting the tool-output check suggests (`BASH_MAX_OUTPUT_LENGTH` at
  15,000 characters over Bash and PowerShell results,
  `MAX_MCP_OUTPUT_TOKENS` at 10,000 tokens over MCP results), with what
  carrying the results it covers cost. `carry_by_tool` and
  `carry_by_agent_type` also carry `saving_if_capped_usd`, each row's own
  saving at `big_result_tokens`.

`recommend.recommend()` runs the `tool-output-carry` rule
(`carry.RULES`). It fires when a tool's carry-token share of cache
volume is more than `CarryThresholds.carry_share_pct` (default 25%) on
at least `min_sample_results` (default 5) carried results. Its action
names a workflow lever (truncate long Bash/PowerShell output, prefer
`Grep` over `Read`, cap agent report length) and cites that tool's own
`saving_if_capped_usd` (its results capped at `big_result_tokens`,
default 8,000) as the projected saving.

## `compaction_sim` (`compaction_sim.py`)

The `autoCompactWindow` sweep: full write-up and worked example in
[`docs/compaction-sim.md`](compaction-sim.md).

- `compaction_sim_by_window` — top-level sessions only, one row per
  candidate window (100k/150k/200k/250k/300k/400k/500k/`none`):
  simulated compactions per session, mean ctx, total cost, and delta vs.
  the observed (`none`) cost in USD and percent — **negative delta means
  cheaper**, the opposite sign convention to `ttl`'s own delta columns
  (see the module docstring for why).
- `compaction_sim_by_agent_type` — every agent type's (`"top-level"` and
  each subagent type) sessions, observed cost, best candidate window,
  its cost, the saving vs. observed (0 floor), the delta in percent, and
  a recommendation string naming the window. Only the `"top-level"` row
  says to set it: the window is one setting for the whole session.
- `compaction_sim_by_task` — the same best-window roll-up as
  `compaction_sim_by_agent_type`, keyed by the kind of task metrics
  capture reported (`task=`) instead of agent type, main sessions only.
  A task appears only once at least `MIN_TASK_SESSIONS` (5, mirroring
  `habits.MIN_GROUP`) main sessions reported it (EST-P8).
- `compaction_sim_fidelity` — for each top-level session whose project
  snapshot carries a known configured `autoCompactWindow`: that
  configured window, the simulated cost at it, the observed cost, and
  the fidelity gap in percent. A gap above
  `CompactionSimThresholds.fidelity_warn_pct` (default 10%) says the
  model's assumptions don't hold for that session.

A simulated compaction fires at the window less this corpus's own
trigger reserve (median `window − preTokens` across real auto
compactions under a known window), and resets context to the session's
own starting context plus a summary of this corpus's median `postTokens`.
It charges the summary request (never logged in the transcript) and the
reply after it re-caching its whole context, with the share of the
starting context real compactions still read from cache read, not
written. Files re-read after a summary aren't charged by the sweep. A
real, already-observed compaction is kept as-is under every candidate
window rather than re-simulated, so a window above the one a session ran
at costs what it did: raising the window can't be tested. Main sessions a
scheduled or looped task started, with no message of yours, are not
replayed (they never compact), and the `compactions` section leaves them
out too.

`recommend.recommend()` runs the `compaction-window` rule (lever
`autoCompactWindow`, category `settings`). It names a floor ("at least
W"), not a single best window: the smallest window with at most 2
simulated compactions per session whose saving, after a rediscovery
cost (this corpus's median post-compaction re-cache write cost per
redundant read in `topology_redundant_reads`), is still more
than 5% of observed cost (`1 - switch_pct`) and more than $1.00
(`switch_usd`). The action says the figure is modelled, not observed.
See [`docs/compaction-sim.md`](compaction-sim.md#the-report-section).
Once the sweep has priced the main sessions, `compaction-churn` is
dropped and `long-context-share` keeps only its workflow advice, whether
or not `compaction-window` fires: the replay is the one answer on the
setting.

## `plan_handoff` (`handoff.py`)

Full write-up: [`docs/plan-handoff.md`](plan-handoff.md). Main sessions
only; scheduled ones are left out.

- `plan_handoff_summary` — one row (`main sessions`): main sessions,
  sessions with an approved plan, sessions where a fresh start pays,
  the median planning context those plans kept, the saving (an upper
  bound) and its share of main-session cost, main-session cost, and
  the replies after approved plans priced as they ran (`build_usd`) and
  at Sonnet's prices (`build_usd_sonnet`, `null` when the rate card has
  no `sonnet` alias).
- `plan_handoff_by_session` — one row per session with an approved plan,
  largest saving first (top `plan_handoff_top_n`, default 20): approved
  plans, the most context any of them would have dropped, replies after
  them, whether any counts, the saving, and its build replies and cost
  at both prices.

`recommend.recommend()` runs the `plan-handoff` rule (`handoff.RULES`,
category `workflow`, no lever, no setting change). It fires when at
least `plan_handoff_min_sessions` (default 3) sessions have a plan that
counts and the saving is at least `plan_handoff_min_saving_share_pct`
(default 1%) of main-session cost. Its action says to `/clear` and
carry out the plan file, and that `/branch` saves nothing because it
copies the whole conversation. Its saving overlaps with
`compaction-window`'s, so the Overview's available saving doesn't add
it on top.

## `model_swap` (`model_swap.py`)

Full contract: [`docs/model-swap.md`](model-swap.md).

For each agent type (and the top-level conversation), reprices every
already-observed priced turn at every model `pricing.toml` carries —
same tokens, same observed 5m/1h cache-write split, same `price_turn`
the rest of the engine uses — and reports the ceiling saving from
moving one tier down (fable -> opus -> sonnet -> haiku, via
`workstyle.model_tier` and `Pricing.aliases`, never a hardcoded id).
Every figure is a price ceiling at today's usage shape, not a
prediction: a smaller model may need more turns or fail the task
outright, and neither possibility is priced here. With metrics capture
on, the agent type's `habits_agents` row is cited as evidence (the
share of its work Claude reported easy, and runs that said a smaller
model would do), and the merged suggestion leaves out an agent whose
runs said it needed a larger model, whose work was mostly reported
hard, or that was retried for the model (`habits.unfit_agents`, used by
`advice._merge_model_tier` and the Models quick action). None of these
changes a figure.

- `model_swap_by_agent_type` — spawns, priced turns, unpriced turns
  (unknown model), observed model, observed cost, a `Cost at
  <model-id>` column per model in the rate card, the best cheaper
  alternative (model id and label), and the ceiling saving in USD and
  %. A row's alternative is empty and its saving `0.0` whenever the
  observed model is already the cheapest available, its own volumes
  already beat the next tier down, or the family/tier can't be
  determined — the table never implies a saving where none exists.
  Each row also carries the `lever` to change. A subagent's saving and
  alternative cover only the runs its agent file's `model:` line
  decides (`model_swap.model_set_by`): runs a workflow script started,
  or given a model when they started, keep that model whatever the
  file says. Additive columns: `lever_runs`, `lever_priced_turns`,
  `lever_model`, `lever_cost` (those runs), `workflow_runs` and
  `spawn_model_runs` (the rest). Spawns, observed cost and every `Cost
  at` column still cover every run. Two more states: `set_elsewhere`
  (no run followed the file) and `no_lever` (`workflow-subagent`,
  `fork`, `unknown`).
- `model_swap_summary` — `scope`, `agent_types`, `observed_cost_usd`,
  `cost_after_tier_down_usd`, `saving_usd` and `saving_pct`: the
  corpus-wide ceiling if every subagent type whose agent-file runs are
  on Fable or Opus moved one tier down, priced on those runs only
  (excludes top-level and any Fable/Opus type already cheaper than its
  next tier).
- `model_swap_agent_file_runs` (report tier) — per named subagent type
  with agent-file runs: `runs`, `priced_turns`, `observed_model`,
  `observed_cost` and a `cost_<model-id>` column per model, for those
  runs only. The what-if engine prices a subagent's model from it.

`recommend.recommend()` runs the `model-tier` rule
(`model_swap.RULES`). It fires per qualifying row (real cheaper
alternative, sample and saving thresholds cleared) and names the exact
lever: `settings.json`'s `"model"` key for the top-level conversation,
or the subagent's `.claude/agents/<type>.md` frontmatter `model:` line.
When some runs were set elsewhere, it says the saving covers the runs
started without a model and names how many a workflow script started
or were given a model, with where each is set. A type whose runs were
all set elsewhere gets no `.md` advice. A Sonnet main session never gets Haiku: its row's state is
`main_floor`, with no alternative and no saving. `advice.finish` gives
the main session's card its own id, `model-tier-main`, ranked last
among cards of its severity.

## `waste` (`waste.py`)

Full field-by-field contract: [`docs/waste.md`](waste.md#the-waste-report-section).

Prices the turns whose output the user never actually benefited from —
a failed tool call, a turn the user interrupted, one stopped by a tool
denial, or every turn in a subagent transcript the harness killed
before it could report back — and attributes each to a cause with a
lever, so a "recoverable spend ceiling" always points at what to change
to stop paying for it again. Purely a reader of state `parse.py`/
`events.py` already produce (the v4-wasted-turns parser addition,
`Turn.tool_error_count`/`Turn.tool_error_chars`, plus the existing
`EventKind.INTERRUPT`/`TOOL_DENIAL`/`API_ERROR` and `TranscriptMeta.
stopped_by_user`) — it detects nothing new. A turn following a
usage-cap pause (`Turn.gap_cause == "limit"`) is excluded outright,
since `limits.py` already owns that attribution.

- `waste_summary` — one "all" row: total priced turns/cost, wasted
  turns and their share of all priced turns, wasted cost (the
  recoverable spend ceiling) and its share of all priced cost, wasted
  tokens, the limit-pause-excluded count, and the api-error-retry count
  (frequency only, never priced).
- `waste_by_cause` — one row per cause (`tool-error`, `interrupt`,
  `tool-denial`, `max-turns`, fixed order) plus an `api-error-retry`
  row: turns, share of all priced turns, cost, share of all priced
  cost, tokens, and that cause's own lever text.
- `waste_by_agent_type` — per-agent-type roll-up: turns, share of
  turns, cost, share of cost, tokens; sorted descending by cost.
- `waste_top_sessions` — the 20 sessions with the highest wasted cost:
  a salted, non-reversible session hash, turns, cost, share of cost,
  and a cause-mix string.

Every `share_pct` column here is against the whole corpus's priced
turns/cost, not just the wasted subset, so `waste_by_cause`'s shares
sum to `waste_summary`'s own totals. `recommend.recommend()` runs the
`wasted-turns` rule (`waste.RULES`). It fires when `waste_summary`'s own
`wasted_cost_share_pct` clears `WasteThresholds.share_pct` (default
10%) and the corpus meets the usual minimum-sample gate, naming the
dominant cause and its lever.

## `compactions` (`compaction.py`)

- `compactions_summary` — one metric/value row each: sessions with ≥1
  compaction, total sessions, compactions per session (mean/max),
  compactions per compacting session, pre/post-compaction tokens
  (median), dropped tokens in total, dropped tokens as a share of
  `cache_creation` and of `new_tokens` (`input_tokens +
  cache_creation_tokens`), mean compaction duration, total
  post-compaction write cost, and the part of it on turns flagged as a
  re-cache.
- `compactions_trigger_mix` — trigger value (`auto`/`manual`/`unknown`)
  counts and share.
- `compactions_per_session` — top 20 sessions by dropped tokens:
  session, compaction count, dropped tokens, post-compaction write cost.

Post-compaction
write/recache cost aggregates exclude any join to the next turn that
took longer than 15 minutes (the join is presumed stale, not a genuine
immediate-post-compaction cost).

## `agents` (`topology.py`)

Answers "how do tokens, cost and information flow between a session and
the agents/skills/workflows it spawns" with numbers only:

- `topology_spawn_write` — downward: mean/median first-turn
  `cache_creation` per agent type (the briefing + system prompt + preloaded
  skills a new spawn pays for), plus the mean `agent_brief_chars` (mean
  briefing chars) the spawning turn handed that agent type.
- `topology_session_baseline` — the top-level session's own first-turn
  `cache_creation` (system prompt + `CLAUDE.md` + prefix-loaded tool
  schemas) across sessions, for baseline-bloat comparison.
- `topology_upward_tool_result` — upward: `Agent`/`Workflow` tool_result
  sizes, the report that lands back in the parent's context, by agent
  type.
- `topology_report_proxy` — a per-subagent report-size proxy by agent
  type, for transcripts where the parent-side tool_result isn't directly
  available.
- `topology_skills_rollup` — a skill's own direct cost plus every agent
  (and workflow) it spawned, recursively via `parentAgentId`: invocations,
  direct cost, spawned cost, mean spawns per invocation, mean report
  size.
- `topology_spawn_depth` — spawn-depth histogram.
- `topology_cost_per_spawn` — cost per spawn by agent type, plus the mean
  `tool_wait_s` (mean tool wait) across that agent type's priced turns.
- `topology_chains_summary` — `stoppedByUser`/`maxTurns` truncation
  signals.
- `topology_reminder_hook_pressure` — attachment/hook-output counts per
  turn, by transcript kind (`top-level`/`subagent`/`workflow-agent`).
- `topology_cache_signal_histogram` — `CACHE_SIGNAL` subkind counts
  (model switches, thinking-stripped, ultra-effort enter/exit, deferred/
  prefix-loaded tool deltas, plan-mode/auto-mode transitions, output
  style changes).
- `topology_mcp_cost` — cost by `attribution_mcp_server`.
- `topology_effort_tokens` — output and thinking tokens by the
  session's `effort` setting.
- `topology_per_turn_effort_tokens` — the same, by `per_turn_effort`.
- `topology_effort_by_agent_type` — output and thinking tokens and the
  thinking share, by agent type.
- `topology_context_composition` — context composition per turn
  (baseline / tool results by tool / assistant output / notifications
  and attachments / compaction summaries), averaged per transcript kind
  (`top-level`/`subagent`/`workflow-agent`).
- `topology_redundant_work` — repeated Bash/PowerShell command prefixes
  and post-compaction rediscovery signals ("how much am I paying to
  re-learn").
- `topology_redundant_reads` — the same file (by salted
  `Turn.read_target_hashes`, never a path) read more than once in a
  session, and how many of those repeats land within a compaction's
  rediscovery window; every count is 0 unless the corpus load wired up a
  hashing salt (see `parse.load_or_create_salt`).

## `run_split` (`run_split.py`)

Full write-up: [`docs/run-split.md`](run-split.md). Subagent runs only;
workflow agents are left out. Every saving is net of what each split
adds back, at list price.

- `run_split_summary` — one row (`subagent runs`): subagent runs, agent
  types where splitting pays, the runs it would split and their splits
  at each such type's best interval, the median context each split
  drops, those runs' cost, the saving and its share of subagent cost, and
  subagent cost.
- `run_split_by_agent` — one row per agent type, largest saving first
  (top `run_split_top_n`, default 20): runs, longest run, the best split
  interval (`every_n`, `null` when none pays), the runs it would split,
  their median length, splits, median context dropped, those runs' cost,
  the saving, its share of the agent type's cost, and its cost.
- `run_split_sweep` — one row per interval tried
  (`run_split_intervals`): runs it would split, splits, the net saving
  across every agent type (below zero when splitting costs more), and
  how many agent types it is the best interval for.

`recommend.recommend()` runs the `run-split` rule (`run_split.RULES`,
category `workflow`, no lever, no setting change, one card per agent
type) after `plan-handoff`. It fires when an agent type has a best
interval and splitting there saves at least
`run_split_min_saving_share_pct` (default 5%) of its cost. Its saving
overlaps with `compaction-window`'s, so the Overview's available saving
doesn't add it on top.

## `hooks` (`hook_costs.py`)

Full write-up: [`docs/hooks.md`](hooks.md). Every transcript in the
window, main sessions and subagents alike. A hook is named by its
script's file name, never by its command or path.

- `hooks_summary` — one row (`your hooks`): hooks seen, hooks that
  failed, failed runs, time waited on failed runs, calls blocked, calls
  sent again unchanged, the cost of blocks, the context added (tokens)
  and the cost of keeping it.
- `hooks_by_script` — one row per hook, costliest first (top
  `hooks_top_n`, default 20): the events it runs on, failed runs, why it
  failed (`script not found`, `timed out` or `error`, plus
  `(relative path)` or `(%VAR% not expanded)` when a script not found is
  named by one), sessions it
  failed in, the last day it failed, runs seen working, calls blocked,
  sent again unchanged, cost of blocks, times it added context, context
  added, the cost of keeping it, and time waited.

The notes give the thresholds and, when there is any, the context
Claude Code's own hooks added (left out of the tables).

`recommend.recommend()` runs `hook_costs.RULES` after `run-split`:
`hook-failures` (severity `action`), `hook-block-resent` and
`hook-context-carry` (severity `advice`). Each is category `workflow`
with no lever and no setting change; see
[`hooks.md`](hooks.md#the-recommendations) for when each fires.

## `quality` (`quality.py`)

Whether the work went well, not only what it cost. One *run* is one
transcript (a main session or one subagent run); every signal is a
ratio of two counts summed over runs. Definitions, the significance
test and privacy are in [concepts](concepts.md#7-quality-signals).

- `quality_by_agent` — per group (the main session, each agent type,
  and all subagents pooled when there are two types or more): runs,
  then as shares didn't finish, likely out of turns, retried on a
  larger model, failed tool calls,
  failed shell commands, denied, stopped by you, corrections, edited
  again and hit the output limit, then replies and cost per run. A
  signal that doesn't apply to the group (corrections for a subagent,
  say) is blank.
- `quality_by_setup` — per agent type, model and effort (the model and
  effort most of a run's replies used; runs that never replied are left
  out, and so are main sessions a scheduled or looped task started with
  no message of yours, `Run.scheduled`): the main shares and per-run
  measures, the setup compared with (the one that agent used most), a
  verdict (`only`, `baseline`, `worse`, `possibly_worse`, `better`,
  `possibly_better`, `no_clear_difference`, `too_little_data`,
  `not_comparable`) and the difference in words. `not_comparable` means
  the two setups' mean replies per run are more than
  `quality.COMPARABLE_SIZE` (5) times apart, so no test is run. Setups
  ran at different times on possibly different work. The retried share
  is shown but not compared (the largest model can never be retried on
  a larger one).
- `quality_retried` — per agent type and model with at least one run
  retried on a larger model (`quality.retried_rows`; the rule is in
  [concepts](concepts.md#7-quality-signals)): runs that edited files,
  retried runs and their share, files edited again against files those
  runs edited, how many of the retries said `[retry: model]`, the model
  the retries most often used, and the day of the latest.
  `quality.retried_models` turns it into the
  `{(agent, family): row}` guard the models recommendation, the Models
  quick action and the Profiles models goal check, alongside
  `quality.worse_models`.
- `quality_retry_reasons` — per agent type and model whose runs were
  retried with a brief that said why (`quality.retry_reason_rows`; the
  markers are in [concepts](concepts.md#7-quality-signals)): retries
  that said why, then how many said the model, the brief, tools or
  other, and the day of the latest.
- `quality_failing_tools` (advanced) — agent type, tool, failed calls
  and runs with a failure, top 25.
- `quality_counts` (advanced) — the raw counts behind every share:
  replies, tool calls, failures, denials, messages, corrections, edits,
  summaries, files edited again on a larger model, the recorded
  outcomes (reported done, failure, stopped, other, none recorded), cut
  off, likely out of turns, retried on a larger model, ended early,
  never replied, and runs whose last reply said `[result: done]`,
  `partial` or `blocked`.
- `quality_markers` (advanced) — one row per marker (`[retry: ...]`,
  `[result: ...]`): what it records, agent runs with it, agent runs that
  could have (Explore and Plan, which start without CLAUDE.md, can't
  write a result marker), the share, how many said each word, and about
  how many output tokens writing them took and what that cost at the
  writing model's output price.

## `habits` (`habits.py`)

How the way you work shapes what it costs, and the habits that would
have saved the most in your own sessions. Built per message of yours (a
*cycle*: one message you typed and every reply and agent run that
answered it, `capture.prompt_cycles`) and per subagent run at any
depth. It reads what metrics capture's tags reported where they are
there, what the parser measures without asking (whether a message
named a file or pasted an error, a command failing again and again, a
skill loaded late), and your ratings from Spend › Sessions. Every table
is always there, empty when there's nothing to show; the notes say when
capture is off or no feedback has been given.

- `habits_digest` — "Weekly pace (last N days)": the three habits worth the most (saving
  a week, `top_1` to `top_3`), what the habits you already picked up
  save (`adopted`), the average cost of a piece of work that met its
  goal (`cost_per_met`), and the share of messages Claude tagged
  (`tagged`). The monthly report carries the same digest. `N` is
  `Habits.span_days`; a saving is only spread into a per-week rate once
  there's a full week of it (`Habits.span_weeks`, UX-4/7/F3) -- under 7
  days it's the raw total observed so far, not a figure stretched by
  dividing by a fraction of a week.
- `habits_playbook` — one row per habit worth trying (`habits.ITEMS`),
  the largest weekly saving first: theme, saving a week, what your
  sessions show, an example to copy, how the saving is worked out, how
  often it was seen, the source (`reported`, `inferred`, `your
  feedback`), confidence (`high` from 20 cases, `medium` from 8; inferred
  alone is never `high`), trend (`new`, `falling`, `rising`, `steady`)
  and the rate per message over the last eight weeks scaled to 0-100
  (`-` for a week with fewer than three messages), then where trying it
  affects things, its trade-off and how to undo it (`where`,
  `trade_off`, `how_to_undo` -- UX-8, the same three-part shape as a
  recommendation's fix explainer), and `covered_by`: the recommendation
  already reporting this same saving, when one fired this report, in
  which case `saving` is blank rather than double-counted (UX-3,
  `habits.COVERED_BY`/`apply_covered_by`). A fall over at least four
  known weeks counts as picked up, and the saving it implies goes into
  the digest's `adopted` row. The dashboard shows the top 5 habits as
  cards; the rest collapse into a "more habits worth trying" `<details>`
  (UX-4/7).
- `habits_by_task` — per kind of task Claude reported (`task=`), after
  an `all` row: messages, share, cost, per message, and the shares
  that were clear asks, large asks, redone by your next message (a
  `shift=redo` or `shift=fix` tag, or a correction), and met their goal.
- `habits_briefs` — per brief word (`clear`, `partial`, `vague`):
  messages, per message, redone, met the goal, and the lines most
  often missing.
- `habits_brief_templates` — per kind of task (the defaults while
  nothing is tagged): the checklist, why those lines (the one most
  often missing from your asks, or a starting point), and the template
  to copy. The `/tl-brief` skill (`capture brief on`) asks for the same
  lines, from `capture_catalogue.BRIEF_CHECKLISTS`.
- `habits_agents` — per agent type (and `top-level` for how hard the main
  session's work was): runs, cost, typical report size, the share
  asked for a short report, finished, retried and retried for the
  model, what the runs said about the model (smaller would do, right,
  needed larger) and CLAUDE.md (used, didn't use), the shares of work
  reported easy and hard, files read again that the parent had read,
  and runs started by another agent.
- `habits_effort_fit` — per reported level and effort (`easy:high`):
  messages, per message, thinking share of output, redone, met the
  goal, and for easy work at high effort or above, what lower effort
  would save (half the thinking, or a quarter of the output without
  thinking).
- `habits_setups` — per kind of task Claude reported, all levels
  together and then by how hard it said the work was (`all`, `easy`,
  `normal`, `hard`): each model family and effort the main session ran
  on, messages, per message, the share that went well (your feedback's
  `met` where you gave it, otherwise not redone by your next message),
  the messages your feedback covers, and the verdict: `usual` (the most
  used) and `cheaper` (the cheapest with at least 5 messages that cost
  less and went well within 5 points of the usual one), with how much
  cheaper per message. On the `all` rows the two are compared level for
  level on the levels both ran, weighted by the usual setup's mix
  (`habits._like_for_like`), and only when those levels hold at least
  half the usual setup's messages; the per-message and went-well
  columns stay as measured. Shown on Setup › Profiles; the `tasks` profile
  goal drafts from its `all` rows.
- `habits_agents_by_task` — `habits_agents`, split by the kind of task
  Claude reported for the message that spawned each run: per task and
  agent type, runs, per run, finished, and what the runs said about the
  model (smaller would do, right, needed larger). Names a cheaper model
  only when at least `MIN_GROUP` runs support it, the saving clears
  `CHEAPER_MODEL_MIN_PCT`, and `habits.unfit_agents` doesn't veto the
  agent type. Shown on Setup › Profiles; the `tasks` profile goal drafts
  agent candidates from it (vetoed again there by the quality check).
- `habits_outcomes` — per outcome you gave (`met`, `partly`, `missed`,
  ...): pieces of work, messages, cost, per piece, the most common kind
  of task, what slowed it most, what would have helped most, and where
  the answers came from (`/tl-feedback` or a dashboard rating).
- `habits_by_shape` — main sessions by shape (`handoff.plan_shape`):
  `plan_build` (a plan approved with `ExitPlanMode`, then files edited in
  the same session), `plan_only` (approved, nothing edited after it) and
  `no_plan`. Per shape: sessions, their share, the average cost, the
  median planning context a fresh start would have dropped
  (`handoff.plan_carried`), the pieces of work rated, the share that met
  its goal, the shares worth it and too costly, and the /tl-feedback
  handoff answers (`yes`, `partly`, `no`). The `plan-handoff` card and
  the suggested profile read it.
- `habits_self_report` — Claude's own reports against your feedback: per
  `level` word (`easy`, `normal`, `hard`) and `brief` word (`clear`,
  `partial`, `vague`) it tagged a message with, the messages that carries,
  how many your feedback covers, the shares that met or missed their
  goal, and the share your next message redid, fixed or corrected. A
  note says whether work Claude called easy missed its goal more often
  than normal work, once there is enough rated feedback on both sides to
  tell (`habits.MIN_GROUP`); when it does, the habits built from the
  `level` word (`effort_fit` and others in `habits._LEVEL_ITEMS`) are
  capped at low confidence in `habits_playbook`, with a note in their
  evidence explaining why.
- `habits_prompt_flags` — per thing a message contained (a file path, a
  code block, an error, a link, what done means, numbered steps, a
  paste): messages and share, then cost per message and reads and
  searches per message with and without it.
- `habits_skills` — per skill: runs by you, loads by Claude, loads after
  three or more replies and what had been spent before them, and what
  the tags said (helped, wasn't needed, would have helped). The
  `/tl-feedback` skill is left out.
- `habits_tool_output` — per tool with outputs over the large-output
  threshold: how many, their tokens and what carrying them cost; then a
  `loops` row for commands that failed three or more times within one
  message, and what those attempts cost.

## `capture` (`habits.py`)

- `capture_usage` — what metrics capture cost while it was on, measured
  from the transcripts (`capture.usage`): the level, since when, note
  and tag tokens, cost and share of spend, how often Claude tagged its
  replies and its agent reports, and the `/tl-feedback` runs and their
  cost; `sessions_with_notes` (main sessions that carried a capture
  note) and `after_compact_notes`/`after_compact_cost` (SURV-3: notes
  landing at or after a real compact boundary, priced at the fuller
  post-compaction rate and broken out as their own line rather than
  folded into a scope's cost); then what it has cost a week since it
  began (`capture.weekly_cost`) next to what the habits worth trying
  that need its reports or your feedback are worth a week
  (`habits.capture_dependent_value`) — a note says so instead of a value
  when nothing measured yet depends on either. The dashboard's Setup ›
  Capture and capture banner show the same figures from `/api/capture`'s
  `roi` field; its per-metric "worth" table (SURV-8) is hidden until at
  least `habits.MIN_GROUP` sessions have a note to measure from.
  `step_down_target`/`step_down_tokens_saved`/`step_down_weekly_saving`
  (CAP-7, `habits.capture_step_down_suggestion`) carry a suggestion to
  step the `[capture] level` down one step (`essentials`/`standard`/
  `deep` only -- `free` asks Claude nothing, so there's no smaller step
  to suggest), blank/zero when there isn't one: every metric the step
  would drop needs enough of its own answers (the per-metric bar
  `capture_view`'s "Enough collected" note also uses) *and*
  `habits.d_level_stability` needs to say the self-report calibration
  signal that evidence backs has settled, not just have enough of it. A
  note spells it out with a runnable `claudeglass capture level
  <lower> --dry-run` command and the command that undoes it -- this
  never changes `config.toml` itself ("no apply button": ClaudeGlass
  never lowers the level on its own). The dashboard's capture banner
  shows a cheaper, unstable-signal-agnostic version of the same command
  (`capture_view._step_down_note`) once the dropped metrics alone have
  enough answers, since checking `d_level_stability` there would need a
  full habits pass the dashboard's poll doesn't already pay for.

## `workstyle` (`workstyle.py`)

- `workstyle_archetypes` — one row per detected archetype
  (`overseer-fanout`, `plan-high-implement-low`, `workflow-heavy`,
  `effort-varied`, `chat-only`, `single-model`, else `mixed`): sessions,
  share and a one-sentence description. Each session gets the first
  archetype whose evidence (model by role, effort spread, spawn counts,
  plan-mode-then-lower-model-implementer sequences) it matches, in that
  order. `recommend.py` conditions
  on this archetype so an overseer session is never told to "stop
  spawning agents" and a chat-only session is never told about subagent
  TTLs — see [Recommendations](#recommendations-recommendpy) below.

## `workflows` (`workflows.py`)

From `<session>/workflows/wf_*.json` run files:

- `workflows_summary` — one metric/value row each: total runs, total
  agents spawned, total cost, mean agents per run, mean cost per run.
- `workflows_status_mix` — run `status` (`completed`/`killed`/other
  observed values) counts and share.
- `workflows_detail` — the costliest runs, one row each: run id,
  session id, status, agent count, phase count (never a phase's
  `detail`, which can carry workflow source or prompt text), cost,
  started/finished.

## `phases` (`phases.py`)

Classification rule (`classify_turn_phase`, first match wins): **OTHER**
if the turn used no tools; **DISCOVERY** if every tool used is one of
Read/Grep/Glob/WebFetch/WebSearch/ListAgents and the turn made no edit;
**IMPLEMENTATION** if the turn made a "real" (non-scratch) edit or ran a
Bash/PowerShell command that isn't a recognised test/build tool;
**VERIFICATION** if the turn ran a recognised test/build tool (`pytest`,
`python -m pytest`, `dotnet test`/`build`, `npm test`, `npx
vitest`/`playwright`, `go test`, `cargo test`, `make`, `mvn`, `gradle`)
or made a scratch (temp-dir) edit; **OTHER** otherwise. A turn that both
edits *and* runs a test/build command in the same turn lands in
IMPLEMENTATION, since DISCOVERY/IMPLEMENTATION are checked first — noted
here since a turn usually does one or the other, not both.

- `phases_summary` — turns, new tokens, cache-read tokens, output
  tokens, cost and cost share per phase.
- `phases_by_transcript_kind` — the same, cross-tabbed by transcript
  kind (`top-level`/`subagent`/`workflow-agent`). A workflow agent is one
  a workflow run started, found under
  `<session>/subagents/workflows/<run_id>/`; it keeps the agent type it
  was started as (`workflow-subagent` when it has no name), so
  `phases_by_agent_type` still files a named one under its type.
- `phases_by_agent_type` — the same, cross-tabbed by agent type.

A DISCOVERY cost share above the module's threshold (default 35%,
evaluated whenever the section is present: with `--phases` on the CLI,
always on the dashboard) feeds the `discovery-share`
recommendation — see [Recommendations](#recommendations-recommendpy)
below.

## `config` (`report.py` via `snapshots.py`) and `config-diff` (CLI-only)

The assembled report's own `config` section renders one
`config-diff-<key>` table per config key that changed across the
window's `snapshot-config` snapshots (the first 20 keys alphabetically —
`report._MAX_CONFIG_DIFF_KEYS`), automatically, with no key to name.
When snapshots exist it also adds:

- `effective-config` — per project, each key's value in the latest
  snapshot and the settings layer it came from.
- `config-layers` — per project and settings layer: whether the layer
  file is present, and its agents, skills, rules, CLAUDE.md bytes,
  commands and MCP servers.
- `config-groups` — projects that share the same effective config
  (hash, project count and list, sessions).
- `config-drift` — only when observed session values exist: sessions
  whose observed value (for example the model) differs from the
  snapshot's.

The standalone `claudeglass config-diff` subcommand, described
next, is a separate, narrower consumer of the same underlying table
function for when you want exactly one key (or every changed key)
outside a full report run.

## `config_diff` (`snapshots.py`)

Reads the JSON files `hooks/snapshot-config.py` writes (see
[The SessionStart hook](reference.md#the-sessionstart-hook)).

- `config-diff-<key>` — `config-diff --key KEY` prints one, and
  `config-diff --auto-keys` prints one per changed key. Per distinct
  value of that config key across a window: sessions, turns, cost, cost
  per session, re-cache share, compactions per session, median span. A
  table note lists the keys that also changed in the same snapshot,
  since a before/after comparison across two different snapshots can't
  isolate one key's effect from everything else that changed alongside
  it.

The subcommand prints these tables directly as Markdown.
`snapshots.build_config_section` wraps the same table in a
`config_diff` section for a library caller; nothing in the CLI or the
report calls it.

`snapshot_for(session, snapshots)` joins a session to the latest snapshot
whose timestamp is at or before the session's start; `diff_keys` and
`co_changed_keys` are the lower-level functions this table is built from.

## `compare` (`compare.py`) — CLI-only

`claudeglass compare --a <spec> --b <spec>` (v0.3 "Feature
expansion" item 6). Not part of `report.build_report`'s fixed section
list — a standalone comparison of two independently-selected arms of
sessions, each named by a `window:<since>..<until>`, `key:<key>=<value>`
(a flattened `snapshots.flatten_snapshot` key), `profile:<id>`, or
`project:<slug>[,<slug>...]` spec (see [`docs/compare.md`](compare.md)
for the full grammar). A session can match both arms, neither, or
exactly one — the specs are independent membership tests, not a
partition.

- `compare_overview` — twelve metrics, one row per metric, Arm A/Arm
  B/delta/delta-%% pre-formatted as display text rather than raw numbers
  (see the module docstring: the `Table` contract's one-`kind`-per-column
  rule can't otherwise fit five different metric kinds in one narrow
  table). The headline nine are per-session means or ratios — sessions,
  priced turns per session, cost per session, new tokens per session,
  cache-read share, re-cache share, compactions per session, median
  session span, mean first-turn cache-creation write — so a delta
  reflects a real behavioural difference rather than one arm simply
  having more sessions than the other (review finding S4: arm *totals*
  used to lead the table, so a bigger arm always showed a large delta
  regardless of any per-session change). The three arm totals (priced
  turns, cost, new tokens) are kept as separate rows labelled "...
  (informational)" further down the table rather than dropped. A
  `sample_ok` column (`yes`/`no`) flags whether *both* arms cleared
  `--min-sessions`.
- `compare_by_stratum` — the same two arms split by `--stratify`
  (`purpose`, `mode`, `task`, or any mix — default `purpose,mode`, plus
  `task` once half of both arms' sessions have a reported one), with a reduced,
  raw-valued metric set (session counts, a `sample_ok` flag, cost and
  new tokens per session, cache-read share, and a note) so this table's
  own CSV/JSON export stays numeric. A stratum
  below `--min-sessions` in either arm shows its session counts only,
  every metric cell blank, and a note explaining the suppression.
- `compare_co_changed` — only populated when *both* arms are
  `key:`-selected: the other flattened config keys that differed between
  each arm's "representative" snapshot (the snapshot most of that arm's
  sessions actually joined to), excluding the arm's own compared key.
  Empty with an explanatory note for any other arm-kind combination.

Every table's notes always carry the plan's "observed, not controlled"
caveat (Risks and gaps item 2: correlation is not causation) plus each
arm's own exact selection rule, so a delta is never presented as
evidence the arm's own setting *caused* it. The `profile:<id>` arm form
is implemented and tested, but nothing copies the `profile_id` the
`snapshot-config` hook records onto `SessionRecord.profile_id`, so it
currently matches zero sessions in any real corpus.

## `reconcile` (`reconcile.py`) — CLI-only

`claudeglass reconcile --admin-csv <file> [--by day|model|day,model]`
(plan "Enterprise use"/"Finance"). Also not part of the assembled
report — an entirely offline comparison of this tool's own per-turn
accounting against a CSV export you already pulled from the Anthropic
Console/Admin API. No network call is ever made.

- `reconcile_by_period` — one row per distinct day (and/or model, per
  `--by`), each token metric (input, cache-creation, cache-read, output)
  and cost as four columns, `<metric>_local`, `<metric>_admin`,
  `<metric>_delta` and `<metric>_delta_pct` (delta is always local
  minus Admin; delta-% is against the Admin figure), plus a fixed
  `TOTAL` row. `--days`/`--since`/`--until` (the same common-parser flags
  every other subcommand uses) restrict both the local and the Admin
  side to the same window before grouping.

The Admin CSV's header row is mapped tolerantly (see
[`docs/compare.md`](compare.md#admin-csv-column-mapping) for the exact
mapping table) — every header spelling and the `_5m`/`_1h`
cache-creation-split convention are this module's own assumption, not
confirmed against Anthropic's published export schema. A column the
mapper can't place is listed, not silently dropped, in a section note;
another note always lists the fixed set of reasons a correct local
figure and a correct Admin figure can still legitimately differ
(subscription usage having no Admin cost, a shared API key used by
other tools, workspace filters on the Admin export, UTC-day-boundary
disagreement, and an unknown model priced at zero locally).

`reconcile.claude_code_reported_costs(corpus, pricing)` (SURV-5,
`PARSER_VERSION` 19) is a second, separate comparison the same module
now offers: one `ClaudeCodeCost` per session whose top-level transcript
carried at least one `cost-state` line (most don't — an infrequent,
apparently version-gated line), pairing Claude Code's own self-reported
running total (`TranscriptMeta.cc_cost_usd`/`cc_cost_has_unknown_model`)
against this tool's own locally-priced total for that same session.
Unlike `reconcile_by_period` above, this needs no Admin CSV and makes no
network call — it is the `cost-state` half of plan P9's later "Q1 gap
metric" (the other half, a statusline ground-truth signal, is separate,
later work); this phase stops at supplying the paired numbers, not the
gap-metric table/note/threshold display itself.

## `usage_windows` (`tools/log_usage.py`)

Not to be confused with the report's own [`usage`](#usage-usagepy)
section above (`usage.py`, day/week/month/project/entrypoint cost from
transcripts) — this section is about your Claude Code *subscription's*
5-hour/7-day plan percentages, from an entirely different data source:
`~/.claude/claudeglass/usage-log.csv` rows appended by the statusline
logger, or by `python -m claudeglass.tools.log_usage` from a
pasted `get_usage` result, deduped by session/reset-time/used
percentage. It has a `build_section` function like every other section
here, but nothing in `report.py`/`cli.py` calls it yet, so it doesn't
appear in `claudeglass report`'s output — call it directly:

- `usage_windows_latest` — per window (`five_hour`/`seven_day`/
  `spend_limit`), the
  latest used percentage, its reset time, and sample count. Comes back
  empty (with a note) when no usage-log rows exist yet.
- `usage_windows_regression` — a simple linear fit of used-percentage
  against cumulative new-token volume within a reset period, per window:
  slope (`% per million tokens`), intercept, sample count, and the reset
  period it was fit against. Requires `token_totals_by_window` to be
  supplied by the caller; skipped with a note when not enough samples
  exist.

## `agent_startup` (`context_budget.py`)

What each subagent type is given before its first turn. Built from each
subagent transcript's events before its first priced turn
(`ContextBudgetStats.add_subagent`); sizes are characters / 4. A fork
(its first turn reads most of the parent's context from cache, or its
agent type is `fork`) is counted in `fork_spawns` and kept out of every
average. No tables and one note when nothing was measured.

- `agent_startup_breakdown` — per agent type: `spawns`, `fork_spawns`,
  `startup_tokens` (the first turn's whole input), the mean per spawn of
  `task_prompt`, `claude_md`, `skills_listing`, `tool_lists`,
  `hook_context`, `other_attachments`, `system_prompt` and
  `tool_definitions` (the last two only when a system-prompt snapshot
  was recorded), `not_recorded` (the rest), `measured_pct`,
  `write_price` (the first turn's model's 5-minute cache-write list
  price per million tokens, used to price each part), and
  `claude_md_managed` (PROF-11/F13 — the share of `claude_md` that is
  Managed policy CLAUDE.md, which still loads regardless of
  `omitClaudeMd`; `goals._omit_claude_md`, `whatif._omit_claude_md` and
  recommend.py's `spawn-claude-md` rule all subtract it out before
  pricing what `omitClaudeMd` would save).
- `agent_startup_unused` — per agent type: spawns measured, the skills
  list size, spawns given it and spawns that called the Skill tool,
  spawns offered MCP tools and spawns that called one, the CLAUDE.md
  size and spawns that only used search and read tools.
- `agent_startup_shared` — parts (CLAUDE.md by source, and the other
  parts) that at least half the agent types receive at about the same
  size, with where they come from and the total across spawns.

## `context_budget` (`context_budget.py`)

Answers the owner question "do we track preloaded skills, the system
prompt, and the autocompact buffer?" A transcript never carries those
sizes directly, so every column ending `(est)` is a clearly labelled
*estimate* built from what is captured (first-turn `cache_creation`, a
HUMAN_TEXT/`skill_listing` attachment's own `size_chars`, a schema-2
config snapshot's `content_layers`) — Claude Code's own `/context` view
remains the authoritative breakdown; treat every `(est)` figure here as a
rough proxy, never as ground truth. Skipped cleanly (no tables, one note)
when the corpus has no top-level transcripts at all.

- `context_budget_baseline` — per project, plus one `all` row summing
  every project: the measured mean/median top-level first-turn
  `cache_creation` (the same metric `agents`' `topology_session_baseline`
  reports, computed independently here rather than read back off that
  table), next to estimated buckets in tokens for `human_prompt` (the
  first HUMAN_TEXT event's `size_chars`, or the first turn's own
  `human_prompt_chars`, divided by 4), `skills_listing` (every
  `skill_listing` attachment's `size_chars` preceding the first turn,
  divided by 4), `memory_files` (the joined schema-2 snapshot's
  `content_layers` CLAUDE.md family + rules bytes, divided by 4; `null`
  without a snapshot), `custom_agents` (the snapshot's agent count times
  a labelled 60-tokens-per-agent-listing constant; `null` without a
  snapshot), `mcp_tools` (`"present, size unknown"` when the snapshot
  names at least one MCP server, else `null` — this module has no way to
  measure an MCP server's own tool-schema size), and
  `system_prompt_and_tools` — the residual: mean baseline minus every
  other known `(est)` bucket, floored at 0. The `all` row's
  snapshot-derived buckets are always `null` (they can't be meaningfully
  combined across different projects' own snapshots).
- `context_budget_autocompact` — per project: the configured
  `autoCompactWindow` from the latest schema-2 snapshot's effective
  settings (`null` if absent), the model's context window size (from a
  statusline ground-truth row when one is available for the project,
  else assumed as 1,000,000 for a `"[1m]"` model alias or 200,000
  otherwise — `context_window_source` names which), the *observed*
  effective autocompact threshold (median `compactMetadata.preTokens`
  over this project's own `trigger == "auto"` compactions —
  `compaction.effective_autocompact_threshold`), the implied buffer
  (window minus threshold), how many auto compactions were observed, and
  whether the observed threshold drifted more than 10% from the
  configured window (`null` when either figure is unavailable).
- `context_budget_statusline` — one real, non-estimated line per
  session (last reported used tokens, window size, used percentage),
  present only once at least one usage-log row carries `context_window`
  fields (see `statusline.py`'s module docstring for how those columns
  get there — `python -m claudeglass.statusline` appends them to
  the same usage-log CSV `usage_windows` already reads, as three new
  trailing columns old-format rows simply don't have). Empty with a note
  otherwise. `claudeglass report` (S1-exports) now loads
  `<config_dir>/usage-log.csv`, when present, with a tolerant reader and
  passes the resulting rows into `build_report` as `usage_log_rows` —
  so both this table and `cache_ground_truth` above populate for the
  ordinary CLI report too, not only for a caller that constructs
  `usage_log_rows` itself. These rows are scoped to the report's own
  `--days`/`--since`/`--until`/`--project` window (fix for review
  finding 8) — the usage-log CSV can span a session's entire history, so
  without this scoping a narrow-window report would silently mix in
  ground-truth rows logged long before or after the reported period.

`recommend.py`'s `baseline-bloat` rule (see
[Recommendations](#recommendations-recommendpy) below) cites this
section's sized buckets as its evidence, and names the largest one in
its action text, whenever `context_budget` is present in the report —
falling back to its older single-mean-baseline evidence otherwise.

## `tool_search` (`tool_search.py`)

Full write-up: [`docs/tool-search.md`](tool-search.md). Every transcript
in the window, main sessions and subagents alike: each gets its own
deferred-tool list.

- `tool_search_summary` — one row (`all replies`): replies requested
  with tools deferred, the most tools deferred in one reply and how many
  of those were MCP tools, definitions measured and their average size
  (tokens), tokens kept out of each reply, what keeping them out saved,
  the cost of the name list, replies that only called `ToolSearch` and
  their cost, and the net saving. The token and money figures are blank
  when no definition was loaded in the window.
- `tool_search_by_server` — one row per MCP server (`built-in` for
  Claude Code's own tools), largest saving first (top
  `tool_search_top_n`, default 20): the most of its tools deferred in one
  reply, its definitions measured, the size each deferred tool was
  counted at, whether that size came from `its own tools` or `all
  servers`, replies, tokens kept out of each reply and the saving.

There is no rule: tool search is already on wherever this section has
anything to measure. The quick action `tool-search` reads both tables.

## `savers` (`savers.py`)

Full field-by-field contract: [`docs/savers.md`](savers.md#the-savers-report-section).

Whether an installed third-party token-saver tool (an MCP server,
plugin, or skill claiming to save tokens) actually nets a saving once
its own overhead is paid for. Detection merges an explicit
`config.toml` `[savers] names = [...]` allowlist with auto-detection: a
case-insensitive regex (`token|saver|savior|optimi[sz]|compress|
context|memory|cache|lean|trim|condens`) over MCP server names
(`Turn.attribution_mcp_server`, `mcp__<server>__<tool>` tool-name
prefixes), config-snapshot `mcp_servers`/`enabled_plugins` entries, and
`Turn.attribution_skill`.

- `savers_detected` — one row per candidate: name, whether it came from
  the explicit `[savers]` list, and every auto-detection source
  matched.
- `savers_overhead` — attributed turns and cost, tool-call count, mean
  result size, and a distinct-tool-name count as a schema/prefix-load
  footprint proxy.
- `savers_effect_by_stratum` — cost/session, new tokens/session,
  cache-creation/turn, mean tool-result tokens/turn, re-cache share,
  compactions/session and turns/session, compared between sessions with
  the saver present versus absent, overall and by purpose/mode stratum,
  gated on a minimum 5 sessions per arm (`SaverThresholds.
  min_sessions_per_arm`).
- `savers_search_substitution` — for the working theory that a saver's
  real job is a code-search replacement: native `Grep`/`Glob`/`Read`/
  shell-search calls per session versus the saver's own calls per
  session, mean result size on each side, and each side's carry-cost
  implication (reusing `carry.compute_carry` per arm).
- `savers_verdict` — net saving per session = (cost/session absent −
  cost/session present) − overhead/session, labelled "observed, not
  controlled", plus any other config key that co-changed between the
  two arms' representative snapshots.

Nothing calls `savers.py` yet: the report, `recommend.recommend()`, the
CLI and the dashboard all leave it out, on purpose. Its name-based
detection treats ordinary tools whose names contain "context", "memory"
or "cache" as savers; a saver configured for every session leaves no
sessions to compare against, and the two groups differ in workload
anyway; and its rule's lever (`mcpServers.<name>`) is not a settings key
the report's fixes can change, with dollar amounts written directly
rather than in the billing mode. Its `saver-tool-roi` rule lives
in `savers.RULES` (same `(report, thresholds, snapshot=None) ->
list[Recommendation]` shape as `model_swap.RULES`). It would fire per
candidate whose verdict row clears the
minimum sample and whose net saving per session clears
`SaverThresholds.net_saving_usd_min` (default $0.01) in either
direction — keep (naming a result-size lever when overhead eats too
much of the gross saving, and citing displaced native search calls plus
smaller mean result size when the search-substitution table supports
it) or disable.

## `elasticity` (`elasticity.py`)

Full field-by-field contract: [`docs/elasticity.md`](elasticity.md).

How many percentage points of a `five_hour`/`seven_day`/`spend_limit`
usage window one million tokens (or one list-price dollar) is actually
worth, fit empirically from consecutive `tools/log_usage.py` CSV samples
paired with the token volume this machine's own transcripts (top-level
and subagent, every project) consumed between them. A weighted (`1/x`)
least-squares fit through the origin — `slope = sum(deltas) /
sum(volumes)` — per window kind and per volume metric (new tokens,
cache-read tokens, list-price USD); a fit below 8 pairs or an R² of 0.5
is refused outright rather than reported with a caveat.

- `elasticity_fit` — one row per window kind × metric: unit, the fitted
  window-percent-per-unit slope (blank when refused), R², pairs used,
  residual spread, whether it was accepted, and the reason when not.
- `elasticity_budget` — one row per window kind: the derived million
  new tokens a full window is worth (`100 / slope`), blank unless that
  window's new-tokens fit was accepted with a positive slope.
- `elasticity_recent_burn` — the last 24h's new-token volume expressed
  as a share of `ElasticityThresholds.weekly_window` (`seven_day` by
  default, "your weekly window").

Under subscription billing with rows in `<config-dir>/usage-log.csv`,
`report.build_report` fits elasticity once (thresholds from
`config.toml`'s `[thresholds.elasticity]`), adds this section right
after `usage`, and hands the same fit to `units.Units`, whose `money`
phrases every amount as "about x% of your weekly usage limit"
(`express_in_window`). Under API billing, or with no readings, the
section is left out. On the dashboard it sits on Spend › Usage:
`elasticity_budget` and `elasticity_recent_burn` are shown, and
`elasticity_fit` is under More tables.

`recommend.recommend()` runs the `window-budget` rule
(`elasticity.RULES`) last, after the other recommendations are
finished. It fires only under subscription billing with an accepted
weekly-limit fit, states how many million new tokens a full weekly
limit holds and the last 24 hours' share of it, and names the most
important other recommendation by its title, never repeating its
numbers.

## `team_report` (`team.py`) — CLI-only

`claudeglass team-report` compares the team documents each
machine saved (see [`docs/team.md`](team.md)). One column per machine,
keyed by its hashed machine id, never a hostname.

- `team_by_archetype` — one row per archetype: each machine's cost per
  session and session count.
- `team_by_agent_type` — the same, one row per agent type.

A cell reads `n<N` when that machine has fewer than `N` sessions for
the row (default 5), and `-` when it has none. The section notes carry
the "observed, not controlled" caveat.

## `cost_record` (`reconcile.py`)

Claude Code writes a running total of a session's cost (a `cost-state`
line) now and then. For every session with one, this compares that
total with ClaudeGlass's own pricing of the same session up to when the
total was last written (`TranscriptMeta.cc_cost_as_of`; subagents
included).

- `cost_record_summary` — one `all` row: `sessions`, `cc_usd` (Claude
  Code's own), `local_usd` (ClaudeGlass, same span), `difference_pct`,
  then the known reasons: `stopped_usd` (replies stopped mid-stream,
  which ClaudeGlass prices and Claude Code leaves out), `unlogged_usd`
  (Claude Code's cost on models with no reply in any transcript, such as
  a request for a session title) and `estimated_usd` (the estimated
  compaction calls both sides count); `unexplained_pct` is what is left
  once the first two are taken out, and `worst_unexplained_pct` the
  largest share in one session.
- `cost_record_sessions` — the 20 newest records: `session_id`, `as_of`,
  `cc_usd`, `local_usd`, `difference_pct`, `unexplained_pct`.

Empty when no session in the window has a record (only some Claude Code
versions write one). On three real Claude Code 2.1.283 sessions, $66 in
all, 0.01% was left unexplained.

## `baseline_comparison` (`report.py`)

Only with `report --baseline <id>`: this window against a saved
baseline.

- `baseline_comparison_overview` — `metric`, `baseline`, `current`,
  `delta`, `delta_pct` (pre-formatted text): cost per session, re-cache
  share, compactions per session, session baseline size, the TTL mix and
  mean spawn write per agent type, and each scorecard level.
- `baseline_comparison_by_mode` — per mode: sessions in each window, a
  `sample_ok` flag, and baseline, current and delta-% for cost per
  session, re-cache share and compactions per session. A mode with fewer
  than 5 sessions on either side shows its session counts only.

## `scorecard` (`scorecard.py`)

Five 1-5 levels (1 poor, 5 excellent) summarising a corpus's cache
efficiency, context hygiene, agent efficiency, config fit and data
quality — see [Sections at a glance](#sections-at-a-glance).

- `dimensions` — one row per scored dimension: `dimension`,
  `level` (1-5), `label` (headed "Rating": `very poor`/`poor`/`fair`/
  `good`/`excellent`, from `LEVEL_LABELS`), the one representative
  `metric` name and its
  `value`, and the `threshold` band it was scored against. A dimension with
  nothing to measure in this corpus (e.g. `agent_efficiency` when no
  session ever spawned an agent) is left out of the table entirely
  rather than guessing a level.
- `overall` — one row (`metric`, `level`, `label`): the minimum level
  across
  `cache_efficiency`/`context_hygiene`/`agent_efficiency`/`config_fit`
  (never an average, and never including `data_quality`, which is
  reported alongside but deliberately excluded from `overall` — a
  low-fidelity measurement shouldn't be conflated with a genuinely poor
  working pattern).

`config_fit` is a proxy for config stability: how many keys changed
across the window's config snapshots (`changed_config_keys`), not a
match against a profile. With no snapshot it is rated 5 with a note,
rather than marked down. `agent_efficiency` is similarly a proxy
(`agent_cost_variance_ratio`): the ratio of the costliest agent type's
mean cost to the median across agent types.

## Recommendations (`recommend.py`)

Not a `Section`/`Table` like the others — `ReportModel.recommendations`
is a dedicated `list[Recommendation]` field, rendered by every renderer
in its own way (a Markdown/HTML block per recommendation; a JSON array;
excluded from CSV, which is table-shaped only). `recommend.recommend()`
builds it by reading back cells from the report's own already-rendered
tables — never a raw accumulator — so every recommendation's evidence
is guaranteed to cite a real, checkable number. Each `Recommendation`
(`model.py`) has these fields:

| Field | Meaning |
|---|---|
| `id` | stable identifier for the rule that fired (e.g. `ttl-switch`, `compaction-churn`) |
| `severity` | `"info"` \| `"advice"` \| `"action"` |
| `category` | `"settings"` \| `"workflow"` \| `"data"` |
| `scope` | where the lever named below applies: `"user"` (`~/.claude/settings.json`), `"repo"` (a project `.claude/settings.json` or agent frontmatter path), or `"managed"` (an org-pushed `managed-settings.json` key the user can't change locally — the action text then also says "raise with your administrator") |
| `lever` | the bare settings key or frontmatter path the recommendation would change (e.g. `promptCacheTtl`, `experimental.cacheTtl` in `<agent>.md`), or `None` |
| `evidence` | one or more `(label, value, source_table, row_key)` tuples, each citing a real cell from a table already in the report — a test walks every recommendation this module produces and confirms the value it cites is genuine, not recomputed |
| `why` | one plain sentence on why it matters |
| `estimated_saving` / `saving_basis` | the saving phrased for your billing mode (`units.py`), and how it was worked out |
| `changes` | the concrete edits proposed: `SettingChange` (`target` `settings`/`agent`, `key`, `agent`, `value` or a `suggested` description when the value needs your judgement, `current`, `note`, `unconfirmed`, `new_agent_file`) |
| `fixes` | per change, from `fixes.py`: a six-part `explainer`, an `apply --set ... --dry-run` `command` (when the value is known), a `command_warning` when the command alone isn't enough, and a `prompt` to give Claude |

The dashboard never changes your Claude Code settings. Each change comes
with the prompt and, for a plain setting, the command. The command's
`--dry-run` shows the diff first, and a real run prints how to undo it.

Rules implemented today, in the order they run. From `recommend.py`'s
own `_rule_*` functions: `ttl-switch`, `long-tool-waits`,
`notification-invalidation`, `batch-instructions`, `subagent-volume`,
`compaction-churn`, `long-context-share`, `cache-read-dominance`,
`baseline-bloat`, `agent-report-size`, `spawn-cost` (for agent types
without `agent_startup` data; otherwise the per-part `spawn-claude-md`,
`spawn-unused-skills`, `spawn-unused-mcp`, `spawn-read-only-tools`,
`spawn-task-prompt` and `spawn-shared-claude-md`), `effort-mismatch`,
`discovery-share` (when the `phases` section is present), `pricing-coverage`,
`data-quality`, `limit-pressure`. Then each module's own rule:
`tool-output-carry` (`carry.RULES`), `plan-handoff` (`handoff.RULES`),
`run-split` (`run_split.RULES`), `hook-failures`, `hook-block-resent`
and `hook-context-carry` (`hook_costs.RULES`), `compaction-window`
(`compaction_sim.RULES`), `model-tier` (`model_swap.RULES`) and
`wasted-turns` (`waste.RULES`). Last, `window-budget`
(`elasticity.RULES`, subscription billing only). Rules are gated by
archetype (a `ttl-switch` recommendation for a `chat-only` session's
subagents is suppressed, since a chat-only session barely has any), a
minimum-sample size (`min_sessions`/`min_turns` in `config.toml`'s
`[thresholds]` table), and managed-settings awareness (see
[`team.md`](team.md#settings-for-teams-and-enterprise)).
`report --patch-set` renders the whole set as unified-diff-style text
via `recommend.render_patch_set`, showing the before and after value for
each lever. A managed-scope lever is marked
`# managed by policy -- shown for reference only`.

With metrics capture on, some rules read the `habits` section too. Each
cites the cell it used:

- `effort-mismatch` comes straight from `habits_effort_fit` when five or
  more messages Claude reported easy ran at high effort or above with
  a thinking share over the threshold. Otherwise it falls back to the
  corpus-wide approximation.
- `spawn-claude-md` is held back when more of an agent type's runs said
  they used CLAUDE.md than said they didn't, and cites the ones that
  didn't.
- `model-tier` cites `habits_agents` (see `model_swap` above).
- `wasted-turns` cites the share of messages redone by the next one and
  the pieces of work you said missed their goal.
- The `sessions` purpose uses the task Claude reported (see `sessions`
  above).

## Diagnostics (`ReportModel.diagnostics`)

Also not a `Section`/`Table` — `Diagnostics` (`model.py`) is a
dataclass of parse-quality counters accumulated across every transcript
in the corpus, exposed as `ReportModel.diagnostics` and rendered
directly by every renderer (a dedicated block, not a table). Fields
include `lines`, `unparsable_lines`, `truncated_final_line`,
`assistant_lines`, `distinct_turns`, `synthetic_turns`,
`turns_missing_usage`, `ttl_sum_mismatch`, `late_duplicate_ids`,
`ignored_line_types` (a count per ignored line type, keyed on the
sanitised type or `other` since `PARSER_VERSION` 20), `oversized_lines`,
`trailing_events`, `replayed_lines`, `copied_lines` (lines of another
session whose own file holds them too, skipped since `PARSER_VERSION`
26), `compaction_calls` / `compaction_calls_unsized` (compactions whose
summary request was estimated and priced, and those left out, since
`PARSER_VERSION` 27), `timestamp_parse_failures`,
`agent_settings`, `modes`, `attachment_catch_all`, `limit_hits`,
`limit_resumes`, `agents_terminated`,
`pre_split_turns` (pre-split `cache_creation` reads normalised at parse
time — see the CHANGELOG), and `pricing_closest_match_turns` /
`pricing_fast_priced_as_standard_turns` (corpus-wide totals mirroring
the `usage` section's `pricing_closest_match`/
`pricing_fast_priced_as_standard` tables above — unlike every other
field here, these two are set once by `report.build_report` from the
finished `pricing.PricingCoverage` accumulator rather than merged
per-transcript, since they only exist once every turn has been priced
against the rate card, not at parse time). `recommend.py`'s
`data-quality` rule reads this field directly to decide whether its
unparsable-lines/ttl-mismatch clauses additionally fire, alongside the
`scorecard.dimensions` `data_quality` row it cites as evidence.

Parser-signals addition (`PARSER_VERSION` 19): `ReportModel.parser_notes`
is a sibling side channel next to `Diagnostics`, not one of its fields —
kept separate because it holds counters that don't fit the closed
per-field merge `report._merge_diagnostics` already does. Two keys,
each a `dict[str, int]`, rendered the same way as a `Diagnostics` field
directly below it: `unknown_line_types` (a count per sanitised, closed-
shape line-`type` token that no detection rule in `events.classify_line`
recognised at all — distinct from `ignored_line_types` above, which
also covers types the parser *does* recognise and deliberately drops)
and `unsized_blocks` (a count per content-block type — `image`/
`document` — this parser could not size by Anthropic's documented
image-token rule, from either a tool_result's or a human prompt's own
content blocks; see `events.content_block_size`). Both are present only
when non-empty, so a corpus that never saw either carries no side
channel at all.

## Worked example

The tables below are the real output of `recache.build_section` and
`compaction.build_section`, run against
[`tests/fixtures/real/session-a`](../tests/fixtures/real/session-a) — a
genuine Claude Code session (1 top-level transcript, 25 subagent
transcripts) that was put through `tools/scrub.py`'s whitelist rewrite:
every id (including the session id below) is HMAC-rehashed with a random
key, and every string field is either a small documented-safe value or
an `x`-filled, length-preserving placeholder. Nothing here is invented,
and nothing here is the original session's real identifier.

**Re-cache summary** (`recache_summary`):

| Metric | Transcripts | Priced turns | Re-cache turns | Re-cache turn share | Cache-creation tokens (re-cache) | Cache-creation tokens (all) | Cache-creation share | Avoidable cost | Unavoidable cost (limit-expiry) |
|---|---|---|---|---|---|---|---|---|---|
| all | 26 | 2,037 | 7 | 0.3% | 1,003,923 | 10,234,286 | 9.8% | 5.80 USD | 0.00 USD |

**Re-cache signature split** (`recache_signature_split`):

| Signature | Turns | Cache-creation tokens | Avoidable cost | Median ctx | Median gap |
|---|---|---|---|---|---|
| full-expiry | 7 | 1,003,923 | 5.80 USD | 130,555 | 10m 21s |
| prefix-invalidated | 0 | 0 | 0.00 USD | - | - |
| limit-expiry | 0 | 0 | 0.00 USD | - | - |

Every re-cache turn in this session was a genuine TTL expiry
(`full-expiry`), not a broken-prefix invalidation or a usage-limit
pause.

**Compaction summary** (`compactions_summary`):

| Metric | Value |
|---|---|
| Sessions with ≥1 compaction | 1 |
| Total sessions | 1 |
| Compactions per session (mean) | 45 |
| Compactions per compacting session (mean) | 45 |
| Compactions per session (max) | 45 |
| Pre-compaction tokens (median) | 121,010 |
| Post-compaction tokens (median) | 17,798 |
| Dropped tokens (total) | 6,370,510 |
| Dropped tokens (share of cache_creation) | 62.2% |
| Dropped tokens (share of new_tokens: input+cache_creation) | 62.2% |
| Mean duration (ms) | 170,334 |
| Total post-compaction write cost (USD) | 18.12 |
| Total post-compaction RE-CACHE-flagged write cost (USD) | 1.87 |

**Compaction trigger mix** (`compactions_trigger_mix`):

| Trigger | Count | Share |
|---|---|---|
| unknown | 45 | 100.0% |

This particular scrubbed fixture's compaction lines didn't carry a
`trigger` value the parser could read (see `tools/scrub.py`'s own
deviation note: `compactMetadata.trigger` isn't on the scrub tool's
keep-list, so a scrubbed fixture's trigger comes back blank/`unknown` by
construction — a property of the scrub tool, not evidence about how
compactions are triggered in general).

To reproduce these tables (or generate your own against a real session),
call the same functions directly:

```python
from pathlib import Path
from claudeglass import discovery, recache, compaction
from claudeglass.model import TranscriptMeta
from claudeglass.parse import parse_transcript
from claudeglass.pricing import load_pricing

fixture_dir = Path("tests/fixtures/real/session-a")
top_path = next(fixture_dir.glob("*.jsonl"))
session_id = top_path.stem
top = parse_transcript(top_path, TranscriptMeta(path=str(top_path), kind="top-level", session_id=session_id))

subs = []
for jsonl_path, _meta in discovery.find_subagents(fixture_dir, session_id):
    meta = discovery.load_meta(jsonl_path.with_name(jsonl_path.stem + ".meta.json"))
    subs.append(parse_transcript(jsonl_path, meta))

pricing = load_pricing()
th = recache.RecacheThresholds()
stats = recache.RecacheStats(th)
for result in [top, *subs]:
    stats.add(result, lambda m: pricing.resolve_model(m))

section = recache.build_section(stats, pricing, th)
for table in section.tables:
    print(table.name, [c.label for c in table.columns])
    for row in table.rows:
        print(row)
```

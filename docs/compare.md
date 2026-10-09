# A/B compare and Admin CSV reconciliation

v0.3 adds two standalone, offline subcommands that sit outside the
assembled `report`: `compare` (plan "Feature expansion" item 6, A/B
compare) and `reconcile` (plan "Enterprise use"/"Finance", Admin API
reconciliation). Both build one `Section` from an already-loaded corpus
and print it through the same Markdown/JSON/CSV/HTML renderers `report`
uses — see [`docs/sections-reference.md`](sections-reference.md#compare-comparepy--cli-only)
for the exact table/column reference. This file covers the arm-spec
grammar, the stratification rule, the "observed, not controlled" stance,
and the Admin CSV column mapping (including which column names are
assumed rather than confirmed).

## `claudeglass compare`

```bash
python -m claudeglass compare \
  --a window:2026-08-01..2026-08-31 \
  --b window:2026-09-01..2026-09-30 \
  --stratify purpose,mode \
  --min-sessions 5
```

### Arm-spec grammar

`--a`/`--b` each take one spec, in one of four forms:

| Form | Example | Selects |
|---|---|---|
| `window:<since>..<until>` | `window:2026-08-01..2026-08-31` | Sessions whose first turn timestamp falls in `[since, until]`, read as UTC dates (either date may be omitted, but not both; `until` is inclusive of the whole day). |
| `key:<key>=<value>` | `key:user_settings.autoCompactWindow=5` | Sessions whose joined config snapshot (`snapshots.snapshot_for`: the latest snapshot at or before the session's first turn) has that flattened key equal to that value. Requires config snapshots to exist (`snapshot-config` hook) — a session with no snapshot never matches. |
| `profile:<id>` | `profile:default` | Sessions that started while profile `<id>` was active — see "Which profile a session ran under" below. |
| `project:<slug>[,<slug>...]` | `project:my-app,my-app-staging` | Sessions under any of the named (redacted) project slugs. |

Dates are `YYYY-MM-DD`. A malformed spec (missing separator, unknown
selector, non-date text, an empty key/value/profile-id/slug list) is
rejected with a single-line message on stderr and the command exits 2
before touching the corpus at all.

A session can match both arms, neither, or exactly one — each spec is
an independent membership test against the whole corpus, not a
partition of it.

**Which profile a session ran under.** `SessionRecord.profile_id` is
the profile active when the session started (`snapshots.profile_for`).
It comes from, in order of preference:

1. the config hook's own capture for that session, which records the
   `active-profile` marker at session start;
2. otherwise the latest record at or before the session's first turn:
   another hook capture, an `apply` stamp, or an undone apply (from the
   moment it was undone, the marker it had backed up). A one-off
   `apply --set` change is not a profile and is ignored.

A session that started with no profile applied, or before anything
recorded one, matches no `profile:` arm. `compare.compare(...,
config_dir=...)` reads the whole record from the config directory;
without `config_dir` only the hook captures in `snapshots` count
(`apply` stamps are not loaded as snapshots). The report, and the
service's `/api/sessions` `profile_id` field, use the same rule.

### Overview metrics: per-session means vs totals (review finding S4)

`compare_overview`'s headline rows are **per-session means** — cost per
session, new tokens per session (input + cache-creation), and priced
turns per session — not arm totals. This matters because the two arms
of a comparison very rarely have the same number of matched sessions:
before this fix, `compare_overview` reported raw arm totals for cost,
new tokens and priced turns, so an arm with (say) twice as many sessions
as the other always showed a roughly 100% higher "cost"/"new tokens"
figure even when nothing about the two arms' sessions actually differed
per-session. That headline delta was dominated by arm size, not by the
config/profile/window difference the comparison was meant to isolate.

The raw arm totals are still reported — as separate rows labelled
"Total cost (informational)", "Total new tokens (informational)" and
"Total priced turns (informational)" further down the table — so the
aggregate figures are not lost, only no longer presented as if they were
a rate comparable across arms of different sizes. `compare_by_stratum`'s
cost/new-tokens columns are per-session means for the same reason.

The remaining headline metrics (`sessions` itself, cache-read share,
re-cache share, compactions per session, median session span, mean
first-turn cache-creation write) were already session-count-independent
and are unchanged.

Mean first-turn cache-creation write is compared on one model only: the
model family both arms' first calls ran on most. The same start measures
differently on each model. With no model in both arms the row shows `-`.

### Stratification and the minimum-sample gate

`--stratify` (default `purpose,mode`) splits `compare_by_stratum` by
`classify.Classification.purpose`/`.mode` — the same values the rest of
the tool already classifies every session into (`review`,
`test-triage`, `planning`, `docs-or-light-edit`, `refactor`,
`agent-fanout`, `workflow-run`, `local-llm-pipeline`, `general-dev` for
purpose; `overnight`, `long-agentic`, `interactive`, `one-shot`, `mixed` for mode).
Pass `--stratify purpose`, `--stratify mode`, or `--stratify ""` (no
split — one "all" row) to narrow it.

A third key, `task`, is the kind of task metrics capture reported for
the session (`classify.reported_task`: at least two tagged messages,
half of them the same kind), or `untagged`. Without `--stratify`, `task`
joins the default keys once at least half of both arms' sessions have
one (`compare.TASK_COVERAGE_PCT`), so arms tagged at different rates
aren't split into near-empty strata. Name it (`--stratify task`, or
`purpose,task`) to use it regardless.

`--min-sessions` (default: `config.toml`'s `min_sessions`, itself 5)
gates whether a row's metrics are shown at all:

- In `compare_overview`, every row still prints both arms' aggregated
  values, but the shared `sample_ok` column reads `no` whenever either
  arm's *total* matched-session count is below the threshold.
- In `compare_by_stratum`, a stratum below the threshold in either arm
  shows only its session counts (`sessions_a`/`sessions_b`) and a note
  explaining the suppression — its cost/token/cache-read cells are left
  blank rather than computed from too few sessions.

A too-small sample is **never an error**: `compare` always finishes and
prints the full table set, and the CLI still exits 0. The gate is a
caveat on how to read the numbers, not a reason to withhold them.

### Observed, not controlled

Every table `compare` produces carries two things in its notes, always:

1. The plan's own "Risks and gaps" item 2 caveat, verbatim in spirit:
   **sessions in each arm also differ in workload, so a difference here
   is not evidence that the arm's own setting caused it** — correlation
   is not causation. A cost delta between two config-keyed arms could
   equally be explained by the arms simply containing different kinds
   of work.
2. Each arm's own exact selection rule (the spec text you passed to
   `--a`/`--b`, verbatim), so a reader can always reproduce which
   sessions a table's numbers came from without re-running the command.

`compare_co_changed` exists specifically to surface the sharpest version
of that risk: when both arms are `key:`-selected, it lists every other
flattened config key that also differed between the two arms'
representative snapshots (the snapshot most of that arm's own sessions
actually joined to), excluding the key you're comparing on. If your
`--a`/`--b` key isn't the only thing that changed at the same time, this
table names what else moved alongside it.

### CLI output

`--json`/`--html PATH`/`--csv-dir DIR` work exactly as they do for
`report` (a single `Section` wrapped in a minimal `ReportModel` for the
same renderers); the default is Markdown to stdout. A bad arm spec, a
bad `--stratify` key, or a `sessions.toml` load failure exits 2. No
matching project directory, or no sessions at all in the window, exits
1. Anything else — including fewer than `--min-sessions` sessions in
either arm — still exits 0 with a full report.

## `claudeglass reconcile`

```bash
python -m claudeglass reconcile --admin-csv admin-export.csv --by day,model
```

`reconcile` never makes a network call: it reads a CSV you already
exported from the Anthropic Console/Admin API and compares it, entirely
offline, against this tool's own per-turn accounting for the same
sessions (restricted to the same `--days`/`--since`/`--until` window the
common parser already exposes on every subcommand — there is no
separate `reconcile`-only date flag).

### Admin CSV column mapping

**Every header spelling and token-column convention below is an assumed
shape, not one confirmed against Anthropic's own published Admin API
export schema.** `parse_admin_csv` maps tolerantly — several plausible
spellings per field — and reports any column it couldn't place (as a
section note, "Admin CSV column(s) not recognised and ignored: ...")
rather than silently dropping it or failing the whole file.

| Canonical field | Recognised header spellings (case-insensitive, punctuation folded to `_`) |
|---|---|
| `date` | `date`, `day`, `usage_date`, `bucket_start` |
| `model` | `model`, `model_name` |
| `input_tokens` | `input_tokens`, `uncached_input_tokens` |
| `output_tokens` | `output_tokens` |
| `cache_read_tokens` | `cache_read_input_tokens`, `cache_read_tokens` |
| `cache_creation_tokens` (flat) | `cache_creation_input_tokens`, `cache_creation_tokens` |
| `cache_creation_tokens` (5-minute split — **assumed** naming) | `cache_creation_input_tokens_5m`, `cache_creation_5m_input_tokens`, `cache_creation_5m_tokens`, `ephemeral_5m_input_tokens`, `cache_creation_ephemeral_5m_input_tokens` |
| `cache_creation_tokens` (1-hour split — **assumed** naming) | `cache_creation_input_tokens_1h`, `cache_creation_1h_input_tokens`, `cache_creation_1h_tokens`, `ephemeral_1h_input_tokens`, `cache_creation_ephemeral_1h_input_tokens` |
| `cost` | `cost`, `cost_usd`, `total_cost` |
| `cost` (cents — divided by 100) | `cost_cents`, `total_cost_cents` |

The flat, 5-minute-split and 1-hour-split columns (however named) all
feed one `cache_creation_tokens` total. The flat column is taken to be
the split's own total, so each row counts the larger of the flat figure
and the 5-minute plus 1-hour sum, never both added together. A file with
only the flat column, only the split, or both gives the same total. A
`cost_cents` column is added to `cost` after dividing by 100. A file with no recognisable
`date` column is refused outright (there's nothing to group by); every
other field defaults to zero/`None` when absent.

### Day bucketing (assumed)

This tool's own local turns are bucketed by their timestamp's **UTC
calendar day**, on the assumption that an Admin export also buckets by
UTC day. An Admin date cell that holds a full timestamp (for example a
`bucket_start` of `2026-08-05T00:00:00Z`) is reduced to its UTC day the
same way, and the `--days`/`--since`/`--until` window is converted to
UTC days before either side is filtered. This is itself an unconfirmed assumption — if the real export
actually buckets by, say, workspace-local time, a session whose turns
straddle midnight will land in a different day on each side even though
every token was accounted for correctly on both. This is exactly why
"UTC day boundaries" is one of the fixed reasons listed below.

### Reading a difference

`reconcile_by_period`'s delta is always **local minus Admin**; delta-%
is that delta relative to the Admin figure. Every reconciliation table's
notes list the fixed set of reasons a correct local figure and a
correct Admin figure can still legitimately differ:

- subscription usage has no Admin cost
- other tools may use the same API key
- workspace filters on the Admin export
- UTC day boundaries (a session's turns are bucketed by UTC day here;
  the Admin export's own day boundary may differ)
- an unknown model is priced at zero locally

None of these is a defect in either side — a nonzero delta is
informative, not necessarily wrong.

### Errors

A CSV that can't be opened, has no header row, has no recognisable date
column, or has a data row that fails to parse raises a single-line
error and the command exits 2. **A bad-row message never includes the
row's own content** — only the 1-based line number
(`cannot parse admin CSV at line 7`) — so a reconcile failure can be
pasted into a shared terminal or ticket without risk of leaking whatever
the export actually contained. Once the CSV parses, `reconcile` exits 1
only when no project directory matches or the window holds no sessions;
otherwise it exits 0 — a large or unexplained delta is not treated as a
failure.

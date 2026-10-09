# Exports and monthly reports

There are two ways to get your numbers into other tools without
asking every team member to run claudeglass by hand:

- a one-shot, privacy-safe `export` (`src/claudeglass/exports.py`)
  for BI/observability pipelines;
- a recurring `monthly-report` (`src/claudeglass/monthly.py`) for
  a habit-forming finance summary.

Both are read-only over an already-loaded corpus — neither writes
anywhere except the file(s) you point them at.

A third command, [`tuning`](#claudeglass-tuning), is for moving your own
figures to another machine so you can tune from them there. It writes a
small checked file of counts and fixed words, and reads one back.

## `claudeglass export`

```bash
python -m claudeglass export --format csv-flat --out team-usage.csv
python -m claudeglass export --format json --per-session --no-hash-slugs --out my-usage.json
python -m claudeglass export --format otel-jsonl --out usage.otel.jsonl
```

`--format` is `csv-flat` (the default), `json` or `otel-jsonl`. The
export goes to stdout unless `--out PATH` is given. Like every other
command it covers the current directory's project unless you pass
`--project` (repeatable) or `--all-projects`, and every session unless
you pass `--days N`, `--since` or `--until`. `--generated-at` (ISO
8601) or `SOURCE_DATE_EPOCH` pins `--format json`'s `meta.generated_at`
so two runs are byte-identical.

### Privacy guarantees (for team leads)

- **Aggregate-only is the default.** Rows are grouped by
  `(day, project, model, entrypoint, agent_type)` — there is no
  `session_id` column unless you opt in with `--per-session`.
- **Project slugs are hashed by default, in every mode — including
  `--per-session`.** A repo/project slug is itself identifying
  information (it can name a client or an internal codename), so an
  export that still prints real slugs would leak exactly the kind of
  detail this tool is meant to protect, whether or not the rows are
  aggregated. Hashing uses a salted HMAC-SHA256 over the slug (domain-
  separated with a `slug:` tag, truncated to 12 hex characters), keyed
  by the same `<config_dir>/salt` file — and the same HMAC-SHA256
  construction, just a different truncation length and domain tag — as
  every other hashed value in this project (`parse.load_or_create_salt`,
  see [SECURITY.md](../SECURITY.md)). The same slug therefore hashes to
  the same value across every export and every report run against the
  same config dir, letting you correlate rows without ever seeing the
  real name. Pass `--no-hash-slugs` to opt out explicitly — see the next
  point for what that actually prints.
- **`--no-hash-slugs` is an explicit, informed opt-out, not a return to
  fully raw slugs.** It replaces just the OS-username segment of a slug
  (the part shaped like `Users-<name>-` / `home-<name>-`) with
  `<user>`, printing a one-line warning to stderr naming the risk. The
  rest of the slug (e.g. the project directory name) is still printed
  verbatim — this is a deliberate, narrower privacy floor under the
  opt-out, not a bug.
- **No text ever leaves in an export.** Every column is a count, a
  token total, or a cost — never a prompt, a tool result, or a file
  path. This is the same guarantee `report`'s own privacy scan enforces
  (see [SECURITY.md](../SECURITY.md)), just narrower in scope since an
  export has far fewer fields to begin with.
- **`--per-session` is an explicit opt-in.** Only pass it when you
  specifically need per-session drill-down (e.g. debugging one person's
  own usage with their consent) — it exposes `session_id`. Project slugs
  are still hashed by default in this mode too (see above).

### `--aggregate-only` / `--hash-slugs` resolution

| `--aggregate-only` / `--per-session` | `--hash-slugs` / `--no-hash-slugs` | Result |
| --- | --- | --- |
| unset (default) | unset (default) | aggregate-only, hashed slugs |
| `--aggregate-only` | unset | aggregate-only, hashed slugs |
| `--aggregate-only` | `--no-hash-slugs` | aggregate-only, **username-redacted** slugs |
| `--per-session` | unset | per-session, **hashed slugs** |
| `--per-session` | `--no-hash-slugs` | per-session, **username-redacted** slugs |

### Row grain and columns (`csv-flat` / `json`)

One row per `(day, project, model, entrypoint, agent_type)`, plus a
trailing `session_id` column when `--per-session` is in effect. This
mirrors `usage.py`'s own day/project/entrypoint axes, plus `model` and
`agent_type` split out (rather than pre-summed away) since a BI import
usually wants those as separate dimensions.

| Column | Meaning |
| --- | --- |
| `day` | local calendar day (`YYYY-MM-DD`, in `config.tz` or the machine's own zone) |
| `project` | project slug, or its hash (see above) |
| `model` | model id as recorded on the turn, or `<unknown>` |
| `entrypoint` | e.g. `claude-desktop`, `claude-code`, or `unknown` |
| `agent_type` | the transcript's agent type, falling back to its `kind` (`top-level`, `subagent`, or `workflow-agent` for one a workflow run started), or `unknown`. A custom agent's own name is hashed to `custom:<8 hex>` with the project names, as in a team document (below); with `--no-hash-slugs` it is printed as is |
| `turns` | priced turn count in this cell |
| `input_tokens` | summed input tokens |
| `cache_write_5m_tokens` / `cache_write_1h_tokens` | summed `ephemeral_5m`/`ephemeral_1h` cache-creation tokens |
| `cache_write_tokens` | summed **total** cache-creation tokens (see the note below — this is the reconciliation-safe total, not just the sum of the two split columns) |
| `cache_read_tokens` | summed cache-read tokens |
| `output_tokens` | summed output tokens |
| `thinking_tokens` | summed thinking tokens |
| `cost` | summed cost (list-price equivalent USD under subscription billing, same convention as every other cost column in this project) |
| `recache_turns` | count of turns flagged as a RE-CACHE event (see [Concepts, section 3](concepts.md#3-cache-rebuild-definitions-and-signatures)) |
| `recache_cache_creation` | cache-creation tokens summed over just those RE-CACHE turns |
| `session_id` | (only with `--per-session`) the session's id |

**On older, pre-TTL-split transcripts** (recorded before Claude Code
split cache-creation tokens into separate 5m/1h counters), a turn can
carry a real, non-zero cache-creation total with `cache_write_5m_tokens`
and `cache_write_1h_tokens` both `0` — the split simply wasn't recorded
yet. Always use `cache_write_tokens` (not the sum of the two split
columns) when reconciling totals against another format or against
`report`'s own overview total; the two split columns are provided for
transcripts where the split *is* known, not as an alternate way to
recover the total. `--format otel-jsonl`'s `cacheCreation` data point and
`report`'s own `cache_creation_tokens` overview total both key off the
same underlying `cache_write_tokens`/`cache_creation_tokens` field, so
all three agree for the same corpus.

`--format csv-flat` writes these as plain, unformatted CSV (raw numbers,
no thousands separators or unit suffixes), matching `render/csv_out.py`'s
own convention. `--format json` writes `{"meta": {...}, "rows": [...]}`,
where `meta` carries `tool_version`, `window`, `pricing_version`,
`generated_at`, `hash_slugs`, and `aggregate_only`.

### `--format otel-jsonl`

One JSON line per `(day, model, token type)`, shaped like an
OpenTelemetry metric data point, using the metric names Claude Code's own
OTel integration documents: `claude_code.token.usage` (with an
`attributes.type` of `input`/`output`/`cacheRead`/`cacheCreation`, plus
`attributes.model`) and `claude_code.cost.usage` (`attributes.model`
only). `time_unix_nano` is the UTC instant of local-day start for the day
the tokens were attributed to. The `cacheCreation` point's value is the
same total `cache_creation_tokens` figure `csv-flat`'s `cache_write_tokens`
column and `report`'s overview totals use — see the note above.

**This is an offline approximation, not a live OTel exporter** — there is
no resource/scope metadata and no real collector transport, and the
timestamp is the day's start, not the moment the tokens were actually
used. It exists so an existing collector's dashboards built against those
metric names can ingest a claudeglass corpus after the fact. This
format carries no project/session dimension at all (the documented
metric names don't have one), so `--aggregate-only`/`--hash-slugs` have
no effect on it.

### `--aggregate` (team documents)

```bash
python -m claudeglass export --aggregate --out my-machine.json
python -m claudeglass export --aggregate --include-projects --out my-machine.json
```

`--aggregate` writes a different, fixed shape from every other
`--format`: a **team document** (`src/claudeglass/team.py`), built
for `claudeglass import`/`team-report` on a team lead's machine
rather than a BI pipeline. It is always JSON regardless of `--format`,
and `--aggregate-only`/`--per-session`/`--hash-slugs`/`--no-hash-slugs`
have no effect on it — a team document is aggregate-only and hashes
project slugs by construction, the same way `--format otel-jsonl`
ignores those flags for its own reason.

A team document carries:

- `tool_version`, `generated_at`, `window`.
- `machine_id` — a stable-but-non-reversible id for this machine: the
  first 12 hex characters of a salted HMAC-SHA256 over the machine's
  hostname (`platform.node()`), keyed by the same `<config_dir>/salt`
  file every other hashed value in this project uses, with its own
  `machine:` domain tag so its namespace can never collide with the
  project-slug namespace `_hash_slug` uses for `--hash-slugs` above.
  Stable across runs on the same machine and config dir; never reveals
  or reverses to the hostname.
- `by_archetype`, `by_mode`, `by_purpose`, `by_agent_type`, `by_model`
  — one row per group value on each axis, most sessions first: `value`
  (the group value itself), `sessions`, `priced_turns`, `tokens`
  (`input`/`cache_creation`/`cache_read`/`output`), `cost_usd`,
  `recache_share_pct`, `compaction_rate`, `ttl_mix` (`5m_pct`/`1h_pct`),
  `mean_spawn_write`, `mean_report_size`. Never a session id, never a
  slug. A custom agent's name in `by_agent_type` is hashed to
  `custom:<8 hex chars>`; built-in agent types are kept as they are.
- `scorecard` — `{dimension: level}`, the corpus-wide scorecard level
  (1-5) per dimension, read from the same `scorecard` section every
  report renders (never independently recomputed).
- `projects` — present **only** with `--include-projects`: a sorted
  list of hashed project slugs (the same `_hash_slug` construction
  `--hash-slugs` uses above), never the plaintext slug. Omitted by
  default — this is opt-in per person, on top of the aggregate's
  already-hashed-or-absent posture.

See [docs/team.md](team.md) for the full `export --aggregate` ->
`import` -> `team-report` flow, and
[its guarantees](team.md#for-team-leads-the-guarantees) in one place.

## `claudeglass monthly-report`

```bash
python -m claudeglass monthly-report --out ./monthly-reports
python -m claudeglass monthly-report --out ./monthly-reports --month 2026-08
```

Writes `DIR/claudeglass-YYYY-MM.md` and the matching `.html` for
one calendar month (default: the previous calendar month relative to
today). The body is deliberately just a finance summary, not the full
multi-section `report` output:

1. **Finance summary** — total cost, total tokens, session count, and
   (subscription billing only) five-hour blocks used.
2. **Cost by model** (the `usage` section's `by_month` table summed per
   model), then **Cost by project** and **Cost by entrypoint** (its
   `by_project`/`by_entrypoint` tables under new titles).
3. The full `usage` section's own tables (by day/week/month, by project,
   by entrypoint, five-hour blocks, and `cache_ground_truth` when a
   usage log is available). `cache_ground_truth` is scoped to sessions
   attributed to the reported month, the same window-scoping this
   report already applies to every other table — a usage-log row logged
   for a session outside the month never leaks into an unrelated
   report, the same way `report`'s own `--days`/`--since`/`--until`
   scoping keeps `cache_ground_truth` bounded to the requested window
   there.

4. **Work habits** — the Work habits digest for the month
   (`habits.digest_table`): the three habits worth the most (a week's
   saving at this month's pace), what the habits you already picked up
   save, what a piece of work that met its goal cost, and how many
   messages Claude tagged. It is left out when there's nothing to say.
   Dashboard ratings count here too: the command and the service read
   them from `<config-dir>/service.db`.

Recache/TTL/compaction/topology and the other optimisation-focused
sections are out of scope for this report — it is a finance artefact,
not a tuning one; use `report`/`ttl`/`recache`/`compactions` for those.

### Month attribution

A session is attributed to the calendar month of its **first** top-level
turn's local timestamp — not sliced per turn. A session whose turns
straddle a month boundary is therefore counted wholly in the month it
started. This is a documented approximation, the same kind `usage.py`'s
own five-hour-block grid already accepts, to avoid a much larger rewrite
of every other analytics module's own per-session assumptions.

The default month (when `--month` is omitted) is the previous calendar
month relative to *now in `config.tz`* — not the machine's own local
zone. On the 1st of a month, a machine whose own zone is ahead of
`config.tz` would otherwise silently resolve to the wrong month.

**An empty target month is not an error.** If no session is attributed
to the requested (or defaulted) month, `monthly-report` still writes
both files with zeroed finance tables and exits `0` — a reasonable
choice for an unattended scheduled job, which should not fail just
because nothing happened that month — but prints a one-line note to
stderr saying so, so the asymmetry with "no sessions found at all under
the given project root(s)" (which does exit non-zero) is visible rather
than silent.

### Idempotency

The same `(corpus, pricing, config, month)` produces `.md`/`.html` files
that are identical apart from a single trailing "Generated at: ..."
line (Markdown) or HTML comment immediately before `</body>` (HTML) —
strip that one line/comment before diffing two runs if you need a
byte-for-byte comparison without pinning `generated_at`.

For a **genuinely** byte-identical run — no stripping needed — pass a
fixed `--generated-at` (ISO 8601) or set `SOURCE_DATE_EPOCH`, the same
reproducible-build convention `export` already offers. This makes it
safe to schedule (cron, a CI job) with a deterministic timestamp (e.g. the run's own scheduled
time) without producing spurious diffs even at the byte level.

### Calling it from code

`monthly.write_monthly_report(corpus, pricing, config, month, out_dir,
usage_log_rows=None, generated_at=None, ratings=None) -> list[Path]`
(`ratings`: the dashboard's session ratings, `Store.all_feedback`'s
shape, for the digest) takes an
already-loaded `corpus`/`pricing`/`config` rather than loading them
itself (matching `report.build_report`'s own "caller loads, this
function only assembles" contract). `month` (`YYYY-MM`) is required:
resolve it first with `monthly.resolve_month(None, config.tz)` for the
previous month. It returns the two paths it wrote (Markdown first, then
HTML) so the caller can log or serve them without having to
reconstruct the filenames itself.

`monthly.run_monthly_report(...)` wraps it with the steps the command
and the service share: the "no project folders"/"no sessions" checks
(raised as `MonthlyReportError`), loading the usage log, and the
empty-month note.

### `serve --monthly-report DIR`

```bash
python -m claudeglass serve --monthly-report ./monthly-reports
```

While the dashboard runs, it writes the previous calendar month's
report into `DIR` — the same two files `monthly-report --out DIR`
writes — whenever either of them is missing. It checks when `serve`
starts and then every hour, on a background thread
(`service/monthly_job.py`), so building a report never holds up the
dashboard. When both files are there, a check does nothing, so each
month is written once; delete a file and the next check writes it
again. The report covers every project the dashboard scans: everything
under `--projects-root` except `config.toml`'s `exclude_projects` and
any `--exclude-project`. "Previous month" is worked out in `config.tz`,
as for the command.

If a report can't be written (no sessions yet, a bad `config.toml`, a
full disk), `serve` prints one line to stderr saying why and tries again
at the next check; it never stops. An empty month is written with
zeroed tables, and a line says so. `serve --once --monthly-report DIR`
checks once after its watcher tick, which suits a cron job.

## `claudeglass tuning`

```bash
python -m claudeglass tuning export --out my-figures.json
python -m claudeglass tuning export --days 90 --out my-figures.json
python -m claudeglass tuning summary my-figures.json
```

`tuning export` writes the figures that show how you work with Claude: how
you prompt, how your replies were tagged, how pieces of work went, what
agents cost, where the spend goes, and what ClaudeGlass itself costs. `tuning summary FILE` reads
a file back in plain words. Use them to take your figures to another
machine, or to hand them to someone helping you tune your setup. It is not
the team comparison: for that, see
[`export --aggregate`](#--aggregate-team-documents) and
[docs/team.md](team.md). There is no import into the dashboard. The file is
for reading with `summary`, or for a person or Claude to read.

### `tuning export`

| Flag | What it does |
| --- | --- |
| `--out PATH` | Write the file to `PATH`. Without it the JSON goes to the terminal |
| `--days N` | How far back to look: whole local days, today and the `N - 1` before it, as the dashboard's Last N days does. Default 30, from 1 to 365 |
| `--project SLUG`, `--project-family REGEX` | Narrow to some projects. Without either, **every** project is included, unlike `export`, which defaults to the current directory's project |
| `--claude-root PATH` | The Claude Code folder holding `settings.json`, to find which of ClaudeGlass's hooks are installed. Default as for `init` |

`--since`, `--until` and `--limit` are refused: the window is `--days`
alone, so the file's `window_days` is always what it covered. Where the
dashboard shows a figure, the file counts it with the same code, but over
the file's own window rather than the period a page is set to. The overhead
block covers that window too, from when capture was turned on if that was
later, not the Capture page's last 7 days. Session modes are the rules' own,
without an override you set. Money is list price, whatever you pay, and the
keys that hold it end in `_usd`.

With `--out`, the file is written whole or not at all: ClaudeGlass writes a
temporary file beside it and renames it into place, so a failure leaves any
file already there as it was. Exit `0` is a written file, `1` means no
projects or no sessions matched, and `2` is a bad flag, a file that can't be
written, or figures that failed the checks below.

### What the file holds

A header (`kind`, `format`, `tool_version`, `parser_version`,
`generated_on` and `window_days`) and nine blocks. A map lists only the words
that were counted, so a missing word reads as zero. A file made before the
`cost_centres` block was added has six, one made before `project_files` has
seven, one made before `compactions` has eight, and all are still valid. So is
a file without the keys that `prompting` and `agents` gained with `compactions`
(`builds`, `asks`, `probes`, `report_turns`, `launches` and `model_choice`): each is left out when
there is nothing to count, and a file from before them never had them.

| Block | Holds |
| --- | --- |
| `capture` | The setup: capture level, who writes the tags, the share of sessions, which metrics and coaching notes are on and the thresholds in force. For each tip: the notes that asked for it, the replies that showed it, the ones Claude called a misfire, and your useful, known and wrong answers |
| `prompting` | Messages you typed, ones you typed while Claude worked, go-aheads, status checks, corrections, adjustments and reminders. Per week: messages and the times each habit showed. Pieces of work with an approved plan, plan rounds declined in the dialog (a decline you then answered with a go-ahead included) and how your feedback to them read. The approved plans by how the build began (in the same session, after a `/clear`, or in a session that opens with the plan): how many you approved by typing, the context carried, and the replies of the build with their context and list-price cost. The plans put up for each ask, grouped by how many times you sent one back: how many, how many you typed, the rounds and questions, and what they cost. Tool calls that were turned away, by reason |
| `tags` | Each tag word as written, by who wrote it (Claude, Haiku, or Haiku filling a reply Claude left bare). Words put right by the hook and by the transcript. The shift Claude wrote against corrections, adjustments and rework. Mistakes Claude admitted, by kind and who caught them. Your `/cg-feedback` answers as counts |
| `pieces` | Pieces of work, how many requests each took, rework by cause, level and week. Sessions by mode, and the unattended night time of those with some. Runs of small requests sent one at a time. Returns after a break and wake-ups by a background task, with the cache tokens they wrote again |
| `agents` | The result word agents left, by how their last reply answered. Haiku's judge calls, cost and errors. Direct and workflow runs and their cost, by built-in type (everything else counts as `custom`) and model family. When a tools list would help some agent types, the list-price saving over the window, by the same types. How many agent replies were one read-only call and nothing else, how many of those were a shell command, and in how many stretches of two or more in a row. The main session's replies to an agent's report: acknowledged, acted on and started more agents. The runs by how they were started (background, foreground or workflow): the agents, their replies and single read-only calls, the starting context times the replies, the summaries made inside the runs, the files sibling agents had already read, and the list-price cost, so a cost per run is a division. The model choice table: for each of the 60 dearest rows, who started the run (direct or workflow), the built-in agent type (a custom one is `custom`), the model family, who chose the model (the call, the agent file, the session, or not recorded), the runs, their list-price cost and the most that Sonnet could save. No agent name, file or model id |
| `overhead` | How sessions were started. How often each ClaudeGlass hook ran, for the events the transcripts can count, and the median time Claude Code recorded. What capture and coaching notes cost. Hook runs and both costs count from when capture was turned on, if that was inside the window. Usage-limit stops by kind |
| `cost_centres` | Where the window's spend went: for the main session, direct agents, workflow agents and each session's first call, the list-price amount in each of base read, above-base read, growth write, rewrite, post-compaction and output. A row or cell with no spend is left out. Amounts only: the parts of the base read are not in it |
| `project_files` | The files that go into agents' runs: the CLAUDE.md files Claude Code loads, the files they import and the files agents read by habit. For each of the 40 that cost most: its extension class (`md`, `txt`, `json`, `config`, `code`, `other`, or `unknown` for a file this machine could not find), how it arrives (`auto`, `import` or `read`), its size now and each recent week's as a bucket (up to 1k, 2k, 5k, 10k and 20k tokens, or over), and, for the main session and each agent type, the share of its runs that had the file. A custom agent's runs count as `custom`. No name, path, hash or cost; `total` counts every file |
| `compactions` | The conversation summaries Claude Code made. The main sessions, how many of them summarised and the summaries in them, by what triggered each (`auto`, `manual` or `other`); those made inside subagent and workflow runs; the tokens they dropped. With a rate card, at list prices: the cache write on the reply after each summary, the estimated cost of writing them, and, for the sessions that summarised 3 times or more, how many there were, what their main conversations cost and what that was in all main-session cost. Counts and amounts: no session, no summary text |

Your session ratings and tip-card answers on the dashboard count as they do
there. They are read from its database without writing to it, and only their
counts reach the file.

### What it never holds

Names, paths and hashes. No session id, project name, skill name or custom
agent name (a custom agent counts as `custom`). No model id (models are
reduced to a family). No prompt, reply, tool result or other text of yours or
Claude's. The words in the file come from fixed lists in ClaudeGlass, never
from your data: every key of every map is one of those words, so a key can't
carry anything either. See [SECURITY.md](../SECURITY.md#tuning-export-tuning)
for the same guarantees as security properties.

### The checks, at write and at read

The file is checked against a fixed description of everything it may hold,
and the check fails closed:

- every key must be one the description names, and every string one of its
  allowed words or a tight pattern (a version, a date, an ISO week);
- every number must be finite and 0 or more;
- no key may appear twice in one object, since a reader keeps only the last
  and the others would go unchecked;
- a second scan of the whole text refuses a drive path, `home/`, `Users\`,
  a Git Bash `/c/` path, an `@`, `://` or `www.`;
- the file must be 256 KB or less. A real one is about 35 KB.

`tuning export` runs the checks before it writes anything. If they fail it
prints the problems to stderr and exits `2`, with no file and no JSON on
stdout. `tuning summary` runs the same checks on the file it is given, so a
file edited to hold anything the checks refuse, made by something else or
too big prints no figure and exits `2`. The checks hold no checksum, so an
edit they allow, such as a changed count, is not caught. A problem line
gives the key path and the check, never the value or the key it found:

```
claudeglass tuning summary: this file fails the checks, so none of its figures are shown:
  capture.level: must be one of the words the spec allows
  document: holds a Users folder path
```

A newer ClaudeGlass may add blocks. A file made by an older one still reads,
but a file with a block your version doesn't know is refused, so read a file
with the same ClaudeGlass that made it or a newer one.

### `tuning summary`

`tuning summary FILE` prints a line or two for each block. It reads the file
alone, so it works on a machine with no Claude Code projects, and `--out`,
`--days`, `--since` and `--until` are refused because the file holds its own
window. An example, with made-up figures from the test fixture, cut short:

```
Tuning figures made on 2026-09-19 for the last 30 days, by ClaudeGlass 0.14.0.
Capture: level standard, tags written by claude, 100% of sessions, 4 metrics on.
Tips: 4 notes asked for one, 3 shown, 1 called a misfire.
Prompting: 120 messages typed, plus 9 typed while Claude worked.
Plans: 8 pieces of work with an approved plan, 4 rounds declined in the dialog.
Builds after an approved plan: kept 5, cleared 2 and handoff 1, 8 plans in all.
Pieces of work: 67.
Of the 55 delivered pieces with a clear start, 10 needed changes after delivery.
Rework: 14 follow-ups after delivery, $31.25 at list prices, most often left out.
Cold returns: 6, rewriting 900,000 cache tokens. Wake-ups: 2, rewriting 260,000.
Agent runs: 21 direct costing $14.50, 9 from workflows costing $6.25.
Agents by how they were started: background 12 agents at $0.96 each, foreground 9 agents at $0.33 each and workflow 9 agents at $0.69 each.
Model choice: 4 agent runs on Opus or above; chosen by file 10, not recorded 9 and inherited 4. Sonnet could save up to $3.10 at list prices.
Hooks: SessionStart ran 54 times, median 140 ms; Stop ran 800 times, no time recorded.
Limits: 3 five-hour stops and 1 weekly stop.
Summaries: 220 summaries in 40 sessions of 61 (auto 200, manual 15 and other 5).
26 sessions summarised 3 times or more: $159.50 of main-session cost, 86% of the total.
```

## Statusline payload key recording (`statusline-keys.json`)

Not an export, but safe to share the same way: the statusline records
the payload's key names (never values, at most 200) to
`<config_dir>/statusline-keys.json`, which you can attach to a bug
report. [SECURITY.md](../SECURITY.md) describes what it holds.

# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Your changes: did each change work, and by how much?** A new page,
  second in the sidebar. Its chart shows cost per reply a day with each
  change marked and a flat line at the average between one change and
  the next, so a saving shows as a step down. Under it, a card per
  change: each measure before against after as bars, the change as a
  percentage, how sure the difference is, what it saved so far, whether
  quality held, and how to undo it. It takes over "Your changes and
  what they did" and the estimates check from Setup › Settings.
  `/api/impact`'s measures now carry `key`, `kind` and `better`.
- **What MCP tool search saves.** Claude Code lists MCP tools by name
  and loads a tool's full definition only when Claude needs it. A new
  section, on Agents & context › Context, and a new check
  (`claudeglass check tool-search`) say how many definitions that kept
  out of each request, by MCP server, and what it saved at each reply's
  own cache rate, less the name list and the replies that only searched
  for a tool. Tools never loaded are sized from the ones that were, so
  it is an estimate, and it says which servers were sized from their
  own tools. See [`docs/tool-search.md`](docs/tool-search.md). Every
  log is read again once to pick this up.
- **Do ClaudeGlass's figures match Claude Code's own?** Claude Code
  writes down what it thinks a session cost, now and then. A new section
  on Data quality, and a new check (`claudeglass check cost-record`),
  compare that with ClaudeGlass's cost for the same replies, and split
  out the known reasons they differ: a reply stopped mid-stream (its
  tokens were used, so ClaudeGlass counts it and Claude Code doesn't),
  and small requests no log records, such as naming the session. The
  check asks you to report it if more than 5% is left unexplained. On
  three real sessions, $66 in all, 0.01% was left.

### Changed

- **The Overview answers three questions, in order.** *Anything
  wrong?* is one checklist: every check, with the recommendation it
  leads to on the same row, what fixing it saves and a prompt to copy;
  the checks with nothing to do fold into one line. It replaces the
  next best actions and the 1-to-5 score meters (the scores are a table
  in the folded details). *Did your changes work?* shows the latest two
  changes before against after. *Where do your tokens go?* has Spend,
  cache and Sessions tiles, daily spend at full width, and spend by
  project and by model.
- **The sidebar puts the answers first.** Overview, Your changes and
  Actions, then a **Details** heading over Spend, Cache, Agents &
  context, Work habits and Setup. G then Y opens Your changes.

- **The dashboard reads more easily at desktop width.**
  - A tile's unit sits on its own line under the number, so "of your
    weekly usage limit" no longer splits across the figure's line.
  - From 200% of the weekly limit, an amount reads as weeks ("about 2.5
    weeks' worth of your usage limit", in the CLI too). A table cell
    that quotes an amount uses a short form ("222.4% ($202.33)"), not a
    sentence that wrapped the column into a tall stack.
  - Money charts step their ticks in the unit shown (5%, 10%, 15%, not
    5.5%, 11%, 16%).
  - Every report table fits at 1280px. Headings wrap to three lines,
    with "list-price $" on a line of its own, and a table still too wide
    draws tighter before it scrolls.
  - A table or section with nothing but zeros shows one "Nothing to
    show" note instead of zero tiles and rows of zeros, so the long
    pages (Spend › Savings most) are shorter.
  - The Overview's setup card is one line, with its steps folded
    beneath it. Every next best action says what it saves, or that the
    saving isn't worked out.
  - Work habits no longer repeats, above its cards, the habits the cards
    show, and its "Already saving" figure is tile-sized, not a headline.
- `docs/ui.md` says the dashboard is desktop only: no phone or tablet
  layout is planned.

### Fixed

- **The dashboard stopped updating a session you were still in.** A
  transcript written to in the last minute was skipped until it went
  quiet, so a session busy for hours showed the figures from when the
  dashboard first read it: on a real cloud session, 3.5 hours and $15
  behind. It is now read again at most once a minute while it keeps
  changing; a 44 MB transcript takes under half a second.
- **The Overview's ways to save could come to more than you spent.**
  Its sentence added every way to save up, but they overlap: a cheaper
  model prices the fewer tokens earlier summaries leave, so doing both
  saves less than the two added. And the auto-compact action was counted
  on top of the compaction saving it comes from. Each way to save is now
  a share of the spend it comes from (its agent type's, or all of it),
  taken from what the others leave, and an action a saving already
  counts joins it once. On real sessions the sentence went from $107 of
  $103 spent to $58. Each row keeps its own figure.
- **A conversation summary's own request was left out of spend.**
  Claude Code bills the request that writes a summary but logs only
  that a summary happened. Each summary now adds an estimated request,
  on the model of the reply before it: that reply's context read from
  the cache once more (written again if the cache had expired), with
  the summary as output. It counts in every spend total, but not as a
  reply, and Conversation summaries shows what summaries cost. On a
  real 780,000-token summary the estimate came within 2% of Claude
  Code's own cost for it (about $0.33). Data quality counts summaries
  estimated and any that couldn't be. Every log is read again once to
  pick this up.
- **The check against Claude Code's own cost found gaps that weren't
  there.** It compared a whole session with Claude Code's last recorded
  total, which can be hours old, so a session that went on showed
  ClaudeGlass 5.6% over. It now stops where Claude Code's total stops.
- **A session started with `/clear` in a web or mobile session was
  counted twice.** Claude Code writes the new session's lines to its own
  log and also into the earlier session's log, so every reply in it was
  priced once in each. A log now skips the lines of another session
  whose own log sits beside it, and Data quality counts them as "Lines
  copied from another session". On one real pair of sessions this took
  5.7% off total spend, and the earlier session now comes within 0.5% of
  Claude Code's own cost record. A copy with no log of its own is still
  counted. Every log is read again once to pick this up.
- Figures that seemed to disagree now say which they are: the
  Overview's "Saved by cache reads" is before paying for the cache
  writes (Cache › Rebuilds leads with the saving after them); the
  glossary gives a cache read as a tenth or a twentieth of the input
  price, by model; How costs work's model card says it counts subagents
  on Fable or Opus only; the cache lifetime chart asks which lifetime is
  cheaper, not which would save, since an agent type can already be on
  it; and Setup › Settings no longer says your settings didn't change
  when it only has no record of them.
- A whole-number figure in a float column reads "24", not "24.00"; a
  metric's empty value lines up with the numbers; a total row ("All
  projects") stays last; a moved table's help sits beside its heading;
  an empty chart states why once; a table's own folded notes read "How
  this table is worked out"; and "CACHE_SIGNAL subkind histogram",
  "pair(s) with usable new_tokens volume" and "R²=" read in plain words.
- Profiles no longer shows a "Start from my current settings" card that
  only pressed the "Save my current settings" button above it, and a
  section intro the page already opens with isn't said twice (Hooks).

## [0.9.0] - 2026-09-26

### Changed

- **claude-token-lens is now ClaudeGlass.** The package, its command and
  module (`claudeglass`, `python -m claudeglass`), the data folder
  (`~/.claude/claudeglass`), the logon service (Scheduled Task
  `ClaudeGlass`, `claudeglass.service`, `com.claudeglass`), the
  `CLAUDEGLASS_COMMAND` variable and the repository
  ([PaulMorrisDev/claudeglass](https://github.com/PaulMorrisDev/claudeglass))
  all take the new name. It is a fresh install, not an upgrade: nothing
  reads the old names. Remove the old tool with its own
  `python -m claude_token_lens uninstall`, then
  `pip uninstall claude-token-lens`, then install ClaudeGlass and run
  `init`. To keep your history, move `~/.claude/token-lens` to
  `~/.claude/claudeglass` before `init`. See
  [Coming from claude-token-lens](README.md#coming-from-claude-token-lens).
  The `[tl: ...]` tags, the `/tl-feedback` and `/tl-brief` skills and the
  CLAUDE.md markers heading keep their names, so sessions recorded
  before the rename read the same.
- **On PyPI.** `pip install claudeglass`, `pipx install claudeglass`, or
  `uvx claudeglass check --all-projects` without installing. The release
  workflow publishes each tagged version through PyPI's trusted
  publishing, so no token is stored anywhere.
- **The README starts with what it finds**, and who it is for, then a
  one-minute try. Troubleshooting, the WSL notes and the old-dashboard
  and more-than-one-Python fixes moved to
  [`docs/first-run.md`](docs/first-run.md#troubleshooting), and the
  messages that point there link to it in full.

### Fixed

- `uninstall-service`, and the logon-service step of `uninstall`, said
  "Register Scheduled Task ... and start it now" and listed the unit
  file it deletes under "will write". They now say "Stop the dashboard
  and remove its logon service" and "will remove".
- The dashboard compared each session with the one before it in *any*
  project when looking for changes the transcripts show, so moving
  between two projects on different models, effort levels or CLAUDE.md
  sizes marked a "Model changed" (or effort, or CLAUDE.md size) change on
  the spend chart and under "Your changes and what they did" on
  Setup › Settings each time. It now compares sessions within the same
  project only, as the reports already did.
- On Linux and macOS, `--project` and `--project-family` stopped with
  "expected one argument", because a project slug there starts with `-`
  (`/home/alice/shop` is `-home-alice-shop`). They take it as the value
  now.

## [0.8.0] - 2026-09-25

### Added

- **Coaching notes: live hints inside the session, in the desktop app
  too.** The status line's coaching line only shows in a terminal. With
  `capture enable coaching_notes`, the capture hook adds a short note
  (about 50 to 120 tokens) to Claude's context when a hint applies, and
  Claude acts on it or tells you in one line:
  - `plan_fresh`: an approved plan kept a lot of planning context, so
    building it after `/clear` would carry less on every reply.
  - `split_run`: a subagent run passed the reply count your own history
    says its type is best split at.
  - `quiet_output`: a tool result of about 8,000 tokens or more.
  - `explore_reads`: 8 reads and searches for one message.
  - `cache_cold` and `clear_context`: you sent a message after the cache
    expired, or with 100,000 tokens of context, and it starts something
    unrelated.

  Notes run at any capture level, rest 30 minutes per hint and session,
  and never change a setting. Your split points and plan habit come from
  `coaching.json`, which the service works out daily from your last 30
  days (leaving out tips you ignored) and `capture refresh` works out
  now. `capture status` and Setup › Capture show what the notes cost.
  Every threshold is a `[thresholds] coaching_*` key. See
  [`docs/coaching.md`](docs/coaching.md). Transcripts are read again
  once (`PARSER_VERSION` 25).
- **Agents & context › Hooks: whether each hook you set up works, and
  what it costs.** Per hook, by its script's file name: failed runs and
  why (script not found, timed out or an error), whether its path is
  relative, calls it blocked and how many Claude then sent again
  unchanged, the context it added and what keeping that context cost,
  and time waited. Three recommendations come with it: `hook-failures`
  (suggests `${CLAUDE_PROJECT_DIR}` for a relative path, and `$HOME`
  for a Windows `%VAR%` path, which the hook's shell doesn't expand),
  `hook-block-resent` (suggests `additionalContext` or `updatedInput`
  instead of a block) and `hook-context-carry`. Actions › Checks and
  `claude-token-lens check hooks` ask the same question. No command,
  output or full path is stored. See [`docs/hooks.md`](docs/hooks.md).
  Transcripts are read again once (`PARSER_VERSION` 23).
- **Agents & context › Subagents: whether splitting long runs pays.**
  Everything a subagent reads is read again on every later reply, so a
  long run costs more than its length suggests. The new `run_split`
  section prices each agent type's runs split every 50, 75, 100, 150,
  200 or 300 replies, each part starting fresh from a short note. It
  takes off what each split adds back: the note, the parent carrying it,
  a fresh cache write and an allowance for re-reading files. It then
  picks the interval that saves most per agent type. Workflow agents are
  left out, even those started as a named agent type: their script
  decides how the work is split. The `run-split` recommendation gives a
  prompt that adds the habit to your CLAUDE.md.
  See [`docs/run-split.md`](docs/run-split.md).
- **The dashboard notices when its own code changes on disk.** An
  editable install left running across a pull or release kept its old
  modules in memory, and lazily loaded new ones against them: `/api/health`
  still showed the old version while `/api/recommendations` and
  `/api/quick-actions` failed with `internal_error (ImportError)`. Now,
  after every scan, `serve` compares the package's files with the code it
  loaded:
  - `/api/health` reports `status: "outdated"` and a `code` block.
  - The banner says to restart with `install-service`.
  - A route that can't import answers `503 restart_needed` rather than
    `internal_error`.
  - The new `serve --exit-on-code-change`, which `install-service` and
    both service scripts now register, exits once the change settles, so
    the service starts again on the new code. Task Scheduler doesn't rerun
    a task that exits with an error, so on Windows `serve` first arranges
    for the `ClaudeTokenLens` task to be started again.
  - Run `install-service` once to add the flag to an existing
    registration. See [`docs/deploy.md`](docs/deploy.md#updating-under-a-running-serve).

### Changed

- **The dashboard's service idles on far less CPU.** Every 30 seconds
  it re-read and re-totalled every session, whether or not anything had
  changed: about 14 seconds of CPU a check on a corpus of 2,700
  transcripts. It now skips a session whose files, tags, snapshots and
  profile are all unchanged since it last totalled it, and reads each
  subagent's details once a check instead of four times. The same idle
  check now takes about 1 second. The first check after the service
  starts still totals every session once.
- **CLAUDE.md and Checks open faster.** Looking for each stale file
  reference walked every project's folders again on every request:
  over 4 seconds for each view. The walk is now quicker and kept for 2
  minutes, so the CLAUDE.md view takes about 0.2 seconds, and Checks
  about 2 seconds, down from nearly 5.
- **Reports build about a third faster** (35 to 24 seconds on a
  2,700-transcript corpus), with the same figures to the byte. The
  compaction replay prices each unchanged reply once rather than once
  for each of its 12 candidate windows, and snapshot times and model
  names are each worked out once.

### Fixed

- **`CLAUDE_CODE_AUTO_COMPACT_WINDOW` counts as your auto-compact
  window.** The variable overrides the `autoCompactWindow` setting, but
  only the setting was read. So the compaction replay started from the
  wrong window, and the advice asked you to change a setting that had no
  effect. The config hook now keeps the variable's value, and the window
  comes from the variable whenever it's set. The replay, the context
  budget table and the "Now" value on every card use it. The
  compaction advice then changes the variable, in a settings file's
  `env` block, whose value replaces the one from your shell. A profile
  draft still offers the setting, unticked, and says it won't apply.
  Refresh the hook for the value to be kept; until then the window
  reads as unknown while the variable is set.

- **Workflow agents get their own row wherever a table splits by
  transcript kind.** Every agent a workflow run started was filed as a
  subagent, so "Workflow agents" never appeared in "Cost by phase: main
  session vs subagents" (`report --phases`), "Claude Code notes and hook
  output per reply" or "What fills the context window". On a 30-day
  corpus of 1,586 subagent transcripts, 525 move to the new row. Totals,
  and every table not split by kind, are unchanged. A workflow agent
  keeps the agent type it was started as, so a named reviewer still
  counts under its own name. When a session's explanation names the
  costliest agent type, it now adds up that type's direct and workflow
  runs. Transcripts are read again once (`PARSER_VERSION` 24).

- **Model advice for a subagent prices only the runs its agent file
  decides.** A model named when a run starts wins over the agent file's
  `model:` line, and a workflow script sets the model for the agents it
  starts. The `.meta.json` `model` turned out to be the model the spawn
  asked for: on real history it matched the spawn's own `model` on every
  direct run, and the run's replies always used it. The saving still
  counted those runs, so it offered to change a file that decided almost
  none of them. On a 30-day corpus, one reviewer type had 106 runs: 60
  from workflow scripts, 43 given a model, 3 following its file; its
  saving falls from $82.08 to $4.39. The corpus-wide ceiling falls from
  $333.08 to $47.36. A type none of whose runs followed its file gets no
  `.md` advice. The advice now says how many runs a workflow script or
  the spawn decided, and where to change those. `model_swap_by_agent_type`
  gains additive `lever_runs`, `lever_priced_turns`, `lever_model`,
  `lever_cost`, `workflow_runs` and `spawn_model_runs` columns, and a
  report-tier `model_swap_agent_file_runs` table feeds the what-if
  engine. Spawns, observed cost and every `Cost at` column are
  unchanged. The Models quick action gains a "Model set by" column.

- **Spend › Usage shows where the work went.** The dashboard never
  built the phases section, so "Cost by phase" and its main session,
  subagent and workflow-agent split never appeared, and the
  `discovery-share` recommendation could never fire there. It is now
  always built, adding about 1% to a report build (10.4 s to 10.55 s on
  a 30-day store); its figures match `report --phases`. The CLI still
  needs `--phases`. The section's notes and the recommendation's
  evidence now name the phases as the tables do (Exploring, Building,
  Checking, Other).

- **The dashboard reads each project's own settings again.** It filed
  every settings snapshot under "(unknown project)", so Setup › Settings
  showed one project instead of each, and per-project settings never
  reached the sessions they applied to. With an auto-compact window set
  in a project, the context budget then assumed a 200,000-token window,
  not the 1,000,000 in use. The CLI's report was right; the dashboard
  now matches it.

- **Kept reports stay kept while a workflow runs.** Re-reading a
  workflow run file that hadn't changed stamped its row as updated,
  which told every kept report the data had changed. Each view then
  rebuilt its report (about a minute of CPU each) every 30 seconds while
  a session with a workflow existed.

- **Data quality counts usage-limit stops again.** "Usage-limit stops",
  "Resumes after a limit" and "Subagents stopped early" always read 0:
  each session's counts were never added into the report's totals. The
  last is also renamed from "Subagents stopped by a limit", since
  Claude Code stops a subagent early for other reasons too, such as a
  network error.

## [0.7.0] - 2026-09-25

### Changed

- **`init` asks four things and shows one review.** How you pay (`1` a
  plan, `2` an API key), whether to connect to Claude Code, whether to
  start the dashboard at logon, and optionally sharper tips (metrics
  capture at Essentials for 14 days, plus `/tl-feedback`). Nothing is
  written until one `Go ahead? [Y/n/d]`, where `d` shows the exact
  `settings.json` change. It ends with a checklist of what's done, off
  or needs attention. Pressing Enter no longer saves "api" on a fresh
  machine: with nothing saved, the billing question asks again. The
  rest of the old questions moved behind `init --advanced`, and
  `projects/<slug>.toml` is written only there. `--dry-run` now writes
  nothing at all, not even the hook copy. WSL folders it finds are
  included and named. [`docs/onboarding.md`](docs/onboarding.md#init)
  has the details.
- **The main session is never told to use Haiku.** A main session on
  Sonnet gets no model card; Opus still steps down to Sonnet. That card
  is its own recommendation now, ranked after the compaction tips, and
  its saving no longer counts in the Overview's available saving when it
  would suggest Haiku.
- **Essentials also tags the size of each piece of work.** The
  before-and-after comparison and the new "without this change" figure
  use it with the kind and difficulty of the work, so a change is judged
  on like-for-like work. It adds about 10 tokens to each session's
  note. A new "Measuring your changes" theme on the Capture page says
  why these three are collected. Installed hooks pick it up when the
  dashboard next starts, or after `update`.
- **Your changes record what they changed, and where.** Each settings
  change keeps its old and new values ("model: opus → sonnet"), and a
  change made in one project's own settings is judged on that project's
  sessions only. Changes only your sessions show (a model, effort or
  CLAUDE.md size change) now appear in "Your changes and what they did"
  and start the "Since my last change" window. Each measure shows its
  reading ("Lower", "Possibly higher", "No clear change").

### Fixed

- **"Since my last change" counts only the sessions started since.**
  It counted every session with a reply after the change, whole, so a
  session begun before the change brought its earlier cost in, and the
  daily spend chart took in the whole of the change's day. Every figure
  on the page, the chart included, now covers the same sessions as the
  "Without your last change" sentence.
- **Loading says what it's waiting for.** Each view shows what is
  loading in words over its skeleton, a view being refreshed keeps its
  figures under an "Updating…" label, and the status line says when the
  service is checking for new sessions, with how far the scan has got.
- **Every recommendation has a key of its own.** A rule that made
  several cards, such as one per CLAUDE.md source, gave them the same
  key, so the dashboard couldn't tell them apart.
- **The dashboard logs your estimates.** Saving a profile, or copying
  the command or prompt for one, now logs its what-if estimate, so
  "Did your estimates come true?" has something to judge.
- **A change your sessions show isn't counted twice.** When an apply
  changes the model and the next session runs on it, that's one change,
  not two; the second one used to cut the first one's after sessions
  short.
- **Cache lifetime advice for subagents on a Pro or Max plan.** It was
  always held back, as if every subscription were on usage credits.
  Claude Code only draws on usage credits once a plan goes over its
  limit. Within plan usage a subagent's 1-hour lifetime works, so the
  advice now shows. A note says what changes on usage credits: the
  main session drops to 5 minutes, and a 1-hour lifetime in an agent
  file is ignored.
- **The setting to change for a subagent's cache lifetime.** When
  `subagentPromptCacheTtl` is already set, Claude Code uses it before
  any agent file, so the card now changes that setting instead of an
  agent file that would have no effect. Subagents whose type wasn't
  recorded also get that setting, not an agent file named `unknown.md`.
- The `promptCacheTtl` and `subagentPromptCacheTtl` explainers no longer
  say a 1-hour lifetime is ignored on usage credits. Only an agent
  file's is. The glossary's "Cache lifetime (TTL)" entry now gives the
  main session's 1-hour default on a Pro or Max plan.

### Added

- **`claude-token-lens status`** says whether each part of the setup
  works: how you pay, the Claude Code connection and whether it has seen
  a session yet, the dashboard and its logon task, metrics capture and
  the `/tl-feedback` skill. Each item says what fixes it. It exits 1
  only when something essential needs attention. The Overview shows the
  same checklist while anything essential isn't done, and the Data
  quality page shows it in full. It replaces the old logon notice.
- **Ignore a recommendation.** Its detail view has an "Ignore this
  recommendation" button, and Actions gains "To do" and "Ignored"
  filters. An ignore belongs to the profile that was active and the
  project you were looking at (or every project), and lapses when the
  recommendation changes what it suggests. Ignored ones also stay out of
  "Start from my recommendations".
- **"Start building in a fresh session once a big plan is approved."**
  A new tip for sessions that approved a plan and then carried a lot of
  planning context through the build. It suggests `/clear` and building
  from the plan file, and says why forking the conversation saves
  nothing. [`docs/plan-handoff.md`](docs/plan-handoff.md) has the
  method.
- **`/tl-feedback` asks whether the build could have started fresh.**
  After a piece of work where you approved a plan, it asks one more
  question. Your answers back or stop the fresh-session tip.
- **A plan-then-build profile.** When at least half your recent
  sessions plan and build in one session, the suggested profile is the
  new built-in `plan-then-build`. Its detail offers "Plan on Opus,
  build on Sonnet" (`model = opusplan`) as an unticked option, with its
  saving, when your main sessions run on Opus.
- **What it would have cost without a change.** Each change in "Your
  changes and what they did" leads with "Without this change: about X.
  You paid Y, so it saved about Z", and says how that was worked out: a
  model or fast mode change is repriced reply by reply, a cache lifetime
  or raised compaction window is replayed, removed context is priced as
  carried, and anything else uses the sessions before it. The "Since my
  last change" window adds the same figure under its headline, and the
  back-test uses it where it's exact.
  [`docs/concepts.md`](docs/concepts.md#6-windows-what-if-estimates-and-beforeafter-comparisons)
  explains each method.

## [0.6.3] - 2026-09-25

### Changed

- **The README is a short landing page now.** It keeps the quick start,
  the page table, troubleshooting and the glossary, and gains dashboard
  screenshots (synthetic data), a Privacy section and a "What it can't
  measure" section. Its reference sections moved into `docs/`, each
  linked from the README's Documentation table:
  - every command and flag, exit codes and CLI timings:
    new [`docs/cli.md`](docs/cli.md);
  - what it reads, the SessionStart hook, the status line, Windows
    notes and the roadmap: new [`docs/reference.md`](docs/reference.md);
  - the report-sections table and recommendation fields:
    [`docs/sections-reference.md`](docs/sections-reference.md#sections-at-a-glance);
  - `exclude_projects`, `retention_days`, managed settings and provider
    detection: [`docs/team.md`](docs/team.md#settings-for-teams-and-enterprise);
  - the privacy grep audit: [`SECURITY.md`](SECURITY.md).
- **Claims corrected while moving them.** The README no longer says the
  tool makes "no network calls": `update` runs pip to download the new
  version. `SECURITY.md` and `docs/team.md` now say a malformed
  `exclude_projects` pattern stops `config.toml` from loading; it was
  described as skipped. Commands in the docs use
  `python -m claude_token_lens`, which works whether or not pip's
  Scripts folder is on your `PATH`.

### Fixed

- **On Windows the dashboard no longer flashes a console window every
  few minutes, or opens a new tab in Windows Terminal.** While a
  dashboard page is open it checks that its logon task is still set up
  by running `schtasks`. The dashboard itself runs without a console
  (`pythonw`), so each check got a console of its own. It now runs
  without one, and terminal windows you already have open are left
  alone. Run `python -m claude_token_lens update` to get the fix and
  restart the dashboard.
- **Quality verdicts no longer compare your work with scheduled checks.**
  Main sessions a scheduled or looped task started, with no message of
  yours, are left out of "Quality by model and effort" and of the
  quality check in "Your changes and what they did". Twenty-two
  two-reply watchdog runs had become the main session's most-used setup,
  so real sessions at another effort were marked worse against them. A
  setup whose runs averaged more than 5 times as many replies as the one
  it would be compared with, or under a fifth, is now "Not comparable"
  instead of tested.
- **Scheduled checks no longer dilute the compaction figures.** The same
  sessions are left out of the Compactions section and the compaction
  replay: they never summarise, so they lowered the summaries per
  session that the replay's "at most 2 a session" limit reads. In "Your
  changes and what they did" they are a group of their own, so more or
  fewer of them running after a change no longer reads as a saving or a
  rise in cost per session.
- **The compaction cards no longer disagree.** Once the compaction
  replay has priced your main sessions, its verdict is the one answer on
  the auto-compact window: "summarised often" (raise it) is dropped, and
  "context running large" keeps only its workflow advice, whose prompt
  no longer suggests lowering the window. A subagent type's row in "Best
  auto-compact window for each agent type" names its cheapest window
  without telling you to set it, since the window is one setting for the
  whole session. The compaction check and the replay notes now say that
  a larger window can't be tested: the replay keeps every real summary,
  so windows above yours cost what your sessions did.

### Added

- `tests/test_doc_links.py` checks that every Markdown link to a
  heading in the README, `docs/`, `SECURITY.md` and this file resolves.
- `scripts/demo-corpus.py` builds the synthetic sessions the README's
  screenshots use, so they can be redone when the dashboard changes.

## [0.6.2] - 2026-09-25

### Fixed

- The dashboard didn't start from the logon task on Windows in 0.6.1.
  The task runs it windowless (`pythonw`), which has no console
  output, and 0.6.1's rewriting of printed commands failed on its
  first line there, so the service stopped at once and the page never
  loaded. It starts again; run `python -m claude_token_lens update` to
  get the fix and restart it.

## [0.6.1] - 2026-09-25

Updating from 0.6.0, whose `update` stops after installing, run
`python -m claude_token_lens update --finish` once afterwards; from
then on `update` alone does the whole upgrade.

### Added

- **Capture can say a reply fixes a fault in earlier work.** The
  `shift` tag gains a fifth word, `fix`, for when the conversation
  moves on to fixing a fault found in work done earlier. `redo` still
  means doing earlier work again. Work habits counts a fix as rework,
  the same as a redo: the earlier message counts as redone, and its
  cost feeds the same savings. The Essentials session note grows by
  about 6 tokens. The new word changes the digest cache's vocabulary
  fingerprint, so the first dashboard start after updating re-reads
  every transcript once.

### Changed

- `update` does the whole upgrade in one command. After pip installs the
  new version it hands over to it (`update --finish`, run by the new
  code, so each later update runs the newest steps), which restarts the
  dashboard on it, then:
  - on Windows, names an older dashboard started by hand that still
    holds the port and, after a yes, stops it and starts the new one
    (never a program that isn't a Python, such as Docker);
  - brings Claude Code's settings.json up to date: a SessionStart hook
    command that can't run, the entries capture needs, and this tool's
    statusline when it runs another Python's copy, each shown and made
    after a yes, with settings.json backed up first;
  - finds copies of this tool installed for other Pythons (the one the
    dashboard ran until now, the `py` launcher's, and each `python` on
    `PATH`) and, once the dashboard runs the new version and the
    statusline no longer uses them, offers to remove them.

  `--yes` answers yes throughout. Updating from 0.6.0 or older, whose
  `update` doesn't hand over, run `update --finish` once afterwards.

### Fixed

- The dashboard's commands now run on the machine that shows them. They
  said `claude-token-lens ...`, which works only when pip's Scripts
  folder is on `PATH`; a default Windows Python install leaves it off,
  so a copied `claude-token-lens capture connect` was "not recognized".
  The service now writes each command in the form that runs its own
  install: `claude-token-lens` when that launcher belongs to its Python,
  `python <archive>` for the `.pyz`, else `python -m claude_token_lens`,
  with the interpreter's full path when the `python` on `PATH` is a
  different one. Set `CLAUDE_TOKEN_LENS_COMMAND` to choose the form
  yourself (for example an alias).
- The same goes for every command the CLI prints (help, next steps,
  fixes, undo lines), the `report --html`/`--json` and `monthly-report`
  files, and the dashboard's Markdown and HTML reports. A message's own
  label ("claude-token-lens update: ...") stays as it is, and data other
  programs read (the statusline, `export`, `--json` output) is printed
  as written. Commands that named only the subcommand ('capture status',
  `apply --revert`) now name the whole command, `update` prints its two
  steps quoted so they paste, and `uninstall` ends with the pip command
  for this Python (or the file to delete for the `.pyz`).
- A profile's apply command for a project (`--scope project-local` or
  `repo`) was refused with exit status 2: the dashboard left out
  `--project-dir`. It now passes `--project-dir .` and says to run it in
  the project's folder (the dashboard still never shows a path).
- Setup › Capture says how its end time works: a choice saves as soon
  as you pick it, the days count from that moment, and the menu's first
  entry shows when capture ends now ("In 12 days: 2026-10-07 09:00
  UTC"). While capture is off, it says the first switch on ends by
  itself after 14 days. `/api/capture`'s `config` gains `timebox_days`.

## [0.6.0] - 2026-09-25

After updating, the first dashboard start re-reads every transcript (a
few minutes): `PARSER_VERSION` bumped to 20 (from 14) to pick up each
reply's fast-mode flag, the fuller edit records, the quality markers,
the metrics-capture tags and notes below, the feedback tag, the
capture-integrity fixes below (tag/reminder splitting, forged-tag and
self-authorisation rejection, the coverage-denominator and per-call
sizing corrections), each hook call's event name, real duration and
whether it was Token Lens's own, the new parser signals (task and
structured-output events, the `thinking_drop` cache signal, Claude
Code's own `cost-state` totals, image and document sizing) and the
sanitised `ignored_line_types` keys (below).

Metrics capture's own displayed and estimated costs rise: a tag's cost
now includes carrying it to the next compaction, and a session billed
under the 1-hour cache TTL prices that write at the 1-hour rate instead
of the 5-minute one (below).

### Added

- **Report tables name the columns the dashboard shows first.** A new,
  display-only `Table.lead_columns` (in `report --json`,
  `/api/report.json` and every section route) lists up to 7 column
  keys, the row key first, for each dashboard table wider than 7
  columns, such as cache lifetime's 22 and model choice's 31, and up to
  4 headline values for each one-row summary table (cache rebuilds,
  usage limits, wasted replies, one tier down), which the dashboard
  shows as tiles. Rows, CSV exports and the Markdown and HTML reports
  are unchanged. See `docs/api.md`.
- **Token Lens now says when a settings policy stops its hooks running.**
  On a machine whose managed settings set `allowManagedHooksOnly` or
  `disableAllHooks` (read from `managed-settings.json` and its
  `managed-settings.d/` drop-ins), or whose own settings.json sets
  `disableAllHooks`, Claude Code runs none of the hooks you add
  yourself. `capture on`/`connect` now say so before showing the
  change, and `capture status`, Setup › Capture, the capture banner and
  the Data quality page's snapshot-hook row name the policy instead of
  reporting the hooks as set up or offering `capture connect`, which
  can't fix it. `/api/capture`'s `hooks` gains `blocked_by`. Policies
  delivered another way (the Windows registry, a macOS profile,
  server-managed settings) aren't visible on disk; there, capture
  status's measured "0 sessions captured" is the tell.
- **Metrics capture (opt-in, off by default, and it uses tokens while
  it's on).** Turn it on and Claude ends each reply — and a subagent's
  final report — with a one-line, closed-vocabulary tag such as
  `[tl: task=bugfix brief=clear]`, at one of four levels (Free,
  Essentials, Standard, Deep) that each add more of it; nothing outside
  the fixed word lists is ever kept. `init`'s last-but-one question
  offers it, after a warning that it costs tokens and a table of what
  each level would have cost over your own last 14 days; turning a
  level on asks one more question, a 14-day time-box that switches
  capture back off by itself unless you turn the limit off or set a
  different length (`capture on --for`). `claude-token-lens capture`
  (`status`/`on`/`off`/`level`/`enable`/`disable`/`connect`/`remove`)
  changes it at any other time, always showing the `settings.json` diff
  first and asking before writing it. See
  [`docs/capture.md`](docs/capture.md).
- **A capture banner and Setup › Capture on the dashboard.** When
  something needs you, such as a missing hook entry, no notes seen yet
  or a time-box that has passed, a banner under the health banner says
  so, with capture's running token cost and coverage. Setup › Capture
  adds level cards, a row per metric (what it captures, the exact
  tag, why, what it feeds, estimate against actual cost, how much has
  been collected), and sampling and time-box controls, each repeating
  the cost warning before anything that spends more tokens. Turning
  something on writes only `[capture]` in Token Lens's own
  `config.toml`, from a loopback request; it never touches Claude
  Code's `settings.json` itself — a missing hook entry shows the
  `capture connect` command to run instead.
- **Free capture signals, and none of them need a level.** How a
  session ended, how long you waited on a notification or a permission
  prompt, and (already in every transcript, so no hook is needed) which
  instruction files, commands, skills, task lists and API errors came
  up are logged to `<config-dir>/signals/`, keyed by a salted hash of
  the session id rather than the id itself.
- **The `/tl-feedback` skill and a second status line.** `capture
  feedback on` (or saying yes to `init`'s last question) adds an
  optional skill you run after a piece of work to rate whether it
  delivered, what slowed it, whether it was worth the tokens and what
  would have helped — shown in full and written only after a yes, and
  it works at any capture level, even off. The statusline can now show
  a second line: a live coaching hint (a large context building up,
  a large last tool result, many reads so far) or a reminder to run
  `/tl-feedback`; the first line is unchanged.
- **Work habits page and habit playbook.** A new section, built per
  message and per agent run, turns everything metrics capture and your
  own feedback have reported into a weekly digest and a playbook of
  habits worth trying, each with its evidence, a rough saving, and
  where the evidence came from (what Claude reported, what the
  transcript shows, or your own feedback, in that order of trust).
  Brief templates — checklists per kind of task, built from what your
  own requests tend to lack — and an optional `/tl-brief` skill that
  checks a new request against its checklist are part of the same page.
  The model-tier, effort-fit, spawn-CLAUDE.md and wasted-turns checks,
  and session purpose, now also read what capture reported, and
  `report`, `compare` and `config-diff` read the dashboard's own tags
  and ratings.
- **Profiles tuned per kind of task.** Once enough sessions carry a
  reported task, Work habits breaks cost and how often the work went
  well down by task, model and effort, and picks the cheapest setup
  that did as well as your usual one; a new "A profile for one kind of
  task" goal on Setup › Profiles drafts from it, and
  `compare` can stratify by task the same way. A `capture` change (a
  level, enabling a metric) now counts as a change point the same way
  an `apply` does, measured by capture's own token cost and the share
  of messages tagged.
- **Claude can say why it re-ran an agent and whether one finished.**
  "Is any agent struggling?" offers two lines for `~/.claude/CLAUDE.md`
  (about 100 tokens, read from the prompt cache after each session's
  first reply) that ask Claude to start a re-run agent's brief with
  `[retry: model|brief|tools|other]` and a subagent to end its last
  reply with `[result: done|partial|blocked]`, about six output tokens
  each. Only the word is kept. A retry that blames the brief, tools or
  something else no longer counts against the cheaper model (two or more
  for one agent become a tip to fix its task prompt or tools); one that
  blames the model counts even for a different agent type. Partial or
  blocked counts as didn't finish. Agents & context › Quality adds **Why
  agents were run again** and, under More tables, **Markers Claude
  wrote** with how often each was written and what it cost. The fix
  explains how to remove the lines again.
- **Spots when a cheaper model wasn't enough.** When an agent's run on
  a cheaper model is followed, in the same session, by the same agent
  started again on a larger model that edits the same files within two
  hours, the run counts as retried on a larger model.
  Agents & context › Quality lists each agent and model this happened
  to ("Agent runs retried on a larger model"), and the retried share
  joins the quality signals and Profiles' before-and-after. Once a
  tenth of an agent's runs on a model were retried, that model is no
  longer suggested for it: the models recommendation, the models check
  on Actions › Checks and the Profiles models goal skip it and say why.
  When the agent file is on that model and it happened twice or more, "Is any agent struggling?" offers to move it
  back up; when the agent file names another model, it says the cheaper
  model was picked by whatever started the agent. On real history this
  flagged claude-implementer on Haiku (4 of 31 runs retried on Sonnet,
  against 2 of 350 Sonnet runs retried on Opus), which the models check
  had been recommending.
- **The dashboard opens straight away.** `serve` now binds its port
  before reading your history, instead of refusing connections until a
  first scan of the whole history finished (a minute or more on a large
  one). A banner shows the scan's progress (files found, read and
  stored), and once it finishes offers **Redraw figures**.
- **Pages no longer wait on a report rebuild while you work.** Every
  reply in a live session changed the store and threw away every built
  report, so each page opened afterwards rebuilt the whole report first,
  and the "last hour" and "last 24 hours" windows rebuilt every minute.
  The dashboard now answers from the report it has and rebuilds it in
  the background, and the sidebar's status line says what time the
  figures are from.
  A report is only rebuilt while you wait when there is none for that
  window yet or it is over ten minutes old. Responses built from a
  report carry `X-Figures-As-Of` (and `X-Figures-Refreshing: 1` while a
  newer one is built).
- **Every change now ends by telling you to restart Claude Code.**
  Claude Code reads settings and agent files when it starts, so a
  session already open kept the old ones with nothing saying so. Every
  fix on the dashboard and in `report`/`check` output, the apply
  command on Setup › Profiles, and `apply`, `apply --revert`, `init`'s
  connect and hook repair and `uninstall`'s settings removal now say to
  restart it, and every prompt for Claude asks it to remind you once it
  has saved.
- `serve --store PATH` puts the dashboard's database somewhere other
  than `<config-dir>/service.db`, so a second copy (a dev checkout) can
  run beside the logon service without sharing it.
- **Edits made through a shell command now count.** The quality
  signals (edits, files edited, edited again, retried on a larger model)
  saw only Edit, Write and NotebookEdit. They now also see MultiEdit and
  the files a Bash or PowerShell command writes with content it
  authored: `sed -i`, `perl -i`, `Set-Content`/`Add-Content`, a heredoc
  or `echo` redirected to a file. A program's output captured to a log
  (`npm test > test.log`) isn't an edit. Relative paths resolve against
  the directory the command ran in, and Git Bash's `/c/` form matches
  `C:\`, so the same file changed both ways counts once. Only a salted
  hash of each path is kept, as before. An edit whose tool call failed
  (the text to replace wasn't found, you declined it) no longer counts.
- **A hook that fails on most of its calls is now flagged.** Every hook
  attachment Claude Code writes to a transcript (`PreToolUse`,
  `PostToolUse`, and so on — the event name only, never the
  matcher/tool-name suffix, so an MCP server or tool name can never
  surface) is tallied by outcome; `capture status` now prints one plain
  prompt when a hook's non-blocking-error rate crosses 50% over at
  least 20 recorded runs, naming it, its failure count and share of
  recorded runs (Claude Code doesn't record every run that passes, so
  the real share can be lower), where to look for it (the user,
  project and local settings files and enabled plugins), the
  latency/noise trade-off, and the undo. This only ever prints —
  nothing here changes `settings.json`.
- **`capture status` now shows Deep's actual measured wait**, replacing
  the old, unsourced "a fraction of a second" guess: the big_output/web
  PostToolUse hook's real `durationMs` (Claude Code records one on every
  hook call; this parser used to drop it) is now kept, and while either
  metric is on, `capture status` prints the median and p90 wait over
  Token Lens's own calls in the last 7 days ("Deep's large-output/web
  hook waited ≈Ns (median, p90 ≈Ns) over N calls this week").
- **The digest cache now carries a salt fingerprint.** A cache entry's
  path/skill-name hashes are salted; without recording which salt wrote
  them, a cache hit after the salt rotated (e.g. a fresh `~/.claude`) would
  keep serving hashes salted under the old one. `DigestCache` now hashes
  the salt itself (never the raw salt) into each entry's header and
  misses when it doesn't match a reader that was itself given a salt; a
  reader given no salt is unaffected.
- **Signal files and the capture-change log now prune themselves by
  default.** `serve`'s watcher already pruned report data
  (`retention_days`) only when you set it; it now also prunes
  `<config-dir>/signals/` and `capture-log.jsonl` on every tick
  regardless, at `retention_days` when set or a new 180-day default
  (`config.SIGNAL_RETENTION_DEFAULT_DAYS`) otherwise — this is Token
  Lens's own background telemetry, not visible report data, so it was
  never meant to accumulate forever. `capture prune` runs the same
  housekeeping by hand (`--dry-run` to preview) for anyone not running
  the service.
- **Did your estimate come true? (P8: evidence, back-test, prediction
  log.)** "Your changes and what they did" now backs its before/after
  verdict with a real statistical test — a ratio-of-sums estimate with
  delta-method variance, Holm-corrected across the measures compared in
  one change, giving each a `lower`/`possibly_lower`/`higher`/
  `possibly_higher`/`no_clear_change`/`too_little_data` verdict instead
  of only "about the same" or a raw percentage — and the "after" side is
  now reweighted to match "before"'s mix of task/purpose first, so a
  change in the kind of work people did after a settings change doesn't
  read as the change's own effect. A session's own transcript can now
  surface a change point nothing else caught: a CLAUDE.md or memory size
  change of 10% or more, or the dominant model or effort level shifting,
  from one session to the next in the same project. New: whenever you
  tick a change to track (not while just exploring "what if?"), the
  dashboard logs its estimate and later checks it against what actually
  happened once a matching real change and enough sessions have come in
  — a new "Did your estimates come true?" table on Setup › Settings
  (`GET /api/backtest`) and a read-only `claude-token-lens backtest` CLI
  command show a verdict (`as_estimated`, `smaller`, `larger`,
  `opposite`, or `too_little_data` while the window is still open) for
  each one, and once at least 3 of your own past estimates for the same
  kind of change have been judged, later "what if?" estimates of that
  kind are calibrated by how it actually turned out for you before
  (shown as fidelity `calibrated`) instead of guessed cold every time.
  See [`docs/backtest.md`](docs/backtest.md).

#### P7a: config coverage (COV-02/03/05/09/10, PROF-09)

- **The effective-settings view only ever showed the highest-priority
  layer's own `env`/permissions/hooks/plugins/MCP-server lists, hiding
  whatever a lower layer added underneath.** These now deep-merge across
  every settings layer with the rule each actually has: `env` and
  `enabledPlugins` per-name (highest layer wins per name, not per file),
  permission and hook counts additively (a lower layer's rule or hook
  still applies), and `enabledMcpjsonServers`/`disabledMcpjsonServers`
  as a union where a rejection at any layer wins. A stray `mcpServers`
  settings key was also being merged even though Claude Code never
  writes settings there (only `managedMcpServers`, managed-layer-only,
  is real) — that dead-code path is removed.
- **On Windows, the system managed-settings scan looked in
  `%ProgramData%\ClaudeCode`, and `~/.claude.json` was always read from
  the home directory.** Both were doc/code conflicts against Claude
  Code's own docs: the managed directory is `%ProgramFiles%\ClaudeCode`,
  and `managed-mcp.json` lives there too, not under the project's own
  `claude_root`; `.claude.json` now honours `CLAUDE_CONFIG_DIR` the same
  way `settings.json` does.
- **Five new recommendations for easy-to-miss environment-variable and
  deprecated-setting levers**: any `DISABLE_PROMPT_CACHING*` variant set
  (high severity — this quietly turns off prompt caching entirely);
  `ANTHROPIC_BASE_URL` set without `ENABLE_TOOL_SEARCH` on a config with
  several MCP servers or plugins; `CLAUDE_CODE_MAX_OUTPUT_TOKENS` set
  (shrinks the effective context window ahead of auto-compaction);
  `CLAUDE_CODE_SUBAGENT_MODEL` set on an archetype that spawns
  subagents (names the exact model-resolution order, and that it never
  reaches the built-in Explore/Plan subagents); and the deprecated
  `includeCoAuthoredBy` set without the `attribution` setting that
  replaces it. Each recommendation explains the trade-off and how to
  undo it in place, since an environment variable has no single
  settings file to write a fix into yet. `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`
  now also scales the simulated auto-compact window in the compaction
  simulation, so that simulation matches what a session with the
  override actually ran under instead of the unscaled default.
- **Config scanning now covers more of what a project or skill
  actually configures.** Agent directories are scanned recursively
  (a nested agent directory previously went uncounted); each CLAUDE.md
  file's own `@import` count is recorded; each skill's `model`/
  `effort`/`context`/`paths` frontmatter is summarised (never its body
  or description); and an installed plugin's own skill names and agent
  count are recorded (best-effort, its default `skills/`/`agents/`
  layout).
- **A session's own observed model or effort could silently diverge
  from what its settings snapshot says is configured** (a shell-profile
  env var or a `--settings`/`--model`/`--effort` CLI override the
  config hook can't see) with only the model half ever surfacing in the
  config-drift table. The report now also feeds each session's own
  dominant observed effort in alongside its dominant observed model, so
  a settings/effort mismatch shows up the same way a settings/model
  mismatch already did.
- **Money and advice presentation now follow your billing mode
  everywhere, and every card says where, what it costs, and how to undo
  it.** Under a subscription, the dashboard, the report and every
  recommendation phrase an amount as a share of your weekly usage limit
  (falling back to a labelled list-price equivalent without an accepted
  elasticity fit), instead of a bare dollar figure that means little
  when you're not billed per token; "about" no longer doubles into
  "about about" when a share and a caveat combine. `meta.units`
  (`{mode, share_per_usd, period_label, basis}`) carries the same facts
  to a JSON API consumer. Every recommendation and every Work habits
  playbook item now has a "where and who it affects", a trade-off and
  how to undo it — including the 23 workflow-only recommendation rules
  that propose no setting change (a purely informational one, like
  cache-read-dominance, keeps its explainer but drops the "ask Claude to
  do it" prompt it never had), and every one of the 21 playbook habits,
  with `allow_routine` stating its security trade-off and the
  `/permissions` command that undoes it. A habit already covered by a
  recommendation that fired this report (`effort_fit` by
  `effort-mismatch`, `short_reports` by `agent-report-size`,
  `quiet_output` by `tool-output-carry`) shows no saving of its own and
  links to the recommendation instead of reporting the same figure
  twice; `effort_fit` and `effort-mismatch` now also agree on the exact
  message-count and thinking-share gate that decides whether there's
  enough evidence to say something, instead of two independent numbers
  that could disagree; the tips on Actions › Checks pick at most
  one habit per theme and skip one already covered by a recommendation,
  instead of listing near-duplicates. A saving spread over "a week" no
  longer divides by a fraction of a week for a corpus under 7 days old
  (which used to multiply a single day's total by about 7x to fake a
  weekly rate) — under 7 days it's the raw total so far, and the Work
  habits digest is titled "Weekly pace (last N days)" rather than a
  fixed "This week" that implied a calendar week regardless of span.
  The playbook shows the 5 habits worth the most as cards up front; the
  rest collapse into a "more habits worth trying" section instead of a
  long, uncapped wall of cards.
- **`Stop` and `StopFailure` hook calls now add a `turn_signals` line**
  (how the turn ended, its API-error kind, closed words only, `Stop`
  sampled and `StopFailure` never) to the same signals log the
  session-end and wait metrics already use, cross-checked in `limits.py`
  against what the transcript itself shows for the same session; habit
  calibration and limit detection read them the same way they already
  read the other free signals.
- **The statusline now writes its own local ground-truth line**
  (salted session id, running cost total or cache-recache figure,
  numbers only) at most once every 60 seconds per session, whenever
  metrics capture is on. `reconcile()` uses it — or, when a session
  already has Claude Code's own `cost-state` line, that instead — to add
  a `cost_ground_truth_gap` table: this tool's local cost against
  Claude Code's own figure, in billing-mode units, for each session and
  over the window. Once at least 10 sessions have a computable gap and
  the median is over 5%, a note says some spend isn't showing up in the
  transcripts (an auxiliary call this tool can't see). Reading this
  ground truth back never creates the salt file if one doesn't already
  exist.
- **The usage-log CSV's dead 9th column** (written as
  `context_window_autocompact_threshold`, never actually populated with
  autocompact data) **now carries the cache-recache figure** the
  statusline already had, under its real name,
  `context_window_cache_read_tokens`; every reader was updated together.
  The log is also now pruned on the configured retention schedule
  (`serve`'s watcher tick and `capture prune`, next to the signals and
  capture-log prunes it was missing from), and re-reading it for
  dedupe on each statusline refresh now scans only the final 64KB
  instead of the whole, ever-growing file.
- **The statusline's own coaching hint** (near a context-window cap, a
  stuck wait, an active rate limit) **now has a per-kind cooldown with
  hysteresis** so it doesn't flicker between messages turn to turn, is
  capped at 60 characters, respects an `until` already in force, and
  shows the `/tl-feedback` nudge at most once per session; the
  transcript reads behind it are all bounded tail reads, not
  whole-file.
- **Actions › Checks and the changes and estimates tables on
  Setup › Settings now show a plain "not enough data yet" placeholder —
  with the actual count ("N of need so far") once one is available —
  instead of an empty panel or a bare prose sentence**, and `/api/impact`'s per-change rows
  carry a structured `gate` (`{"reason": "min_sessions", "have",
  "need"}`, or `null` once there's enough) alongside the existing prose
  verdict, so the UI doesn't have to parse a sentence to decide whether
  to show a count.

#### P7b: config coverage (COV-01/04/07/11, PROF-02)

- **Advice could recommend a settings layer that a higher-precedence
  layer had already overridden**, silently wasting the suggestion (for
  example, proposing a repo-level change your own project-local
  settings already re-set). Every settings-scoped recommendation and
  its `apply` command now resolve their target layer against the
  current snapshot's own provenance (`effective_provenance`/
  `effective_env_provenance`) instead of a fixed guess, and
  project-local is now a first-class scope alongside user/repo/managed.
- **`apply` compared a proposed change against whichever project's
  config snapshot happened to be newest, not the project you were
  actually applying to**, so its override warning could reference the
  wrong project's settings, or a stale snapshot from a different
  project's last run. It now looks up the current project's (`--project-dir`)
  own latest snapshot, and warns — for a settings key and for an `env`
  entry alike — whenever a higher-precedence layer already sets the
  value, since writing wouldn't change what Claude Code actually uses.
- **Five environment-variable and deprecated-setting recommendations
  that used to be prose-only now offer a real
  `apply --set env.NAME=value --dry-run` command**: three of the five
  (`env-tool-search`, `env-disable-prompt-caching`,
  `env-max-output-tokens`); the other two stay prose, since one
  proposes no value and the other's target isn't allowlisted. `apply`
  now writes an environment variable into the target settings file's
  own `env` object — the same place Claude Code itself reads it from,
  and the same mechanism `effective_env_provenance` already tracked —
  instead of only ever printing an `export NAME=value` line for you to
  run yourself, which never actually changed anything Claude Code
  would see. An env-lever recommendation's card now also shows the
  currently effective value, not only the proposed one.
- **`apply <id> --launch --dry-run` now prints `--effort LEVEL` on the
  `claude --settings ...` command line when the profile sets an effort
  level**, so a session-only try-it launch actually carries the effort
  change instead of silently dropping it. An agent-level model change
  (frontmatter, not a settings key) has no session-only equivalent —
  scoping it to one session would need the agent's full prompt body
  inline on `--agents`, which this dashboard never reads or copies —
  so those recommendations are now labelled "persistent: affects every
  task this agent runs" and show the plain saving figure rather than
  the session "at most" ceiling used for a change you might only try
  once.

- **`task_status` and `structured_output` attachment lines now get their
  own event kinds** instead of falling into the generic attachment
  catch-all: `task_status` keeps only a closed status word
  (`running`/`completed`/`failed`/`stopped`/`cancelled`, else `other`)
  and task type (`local_bash`/`local_agent`, else `other`) — never the
  description, delta summary, output file path or shell command these
  lines also carry in the real corpus; `structured_output` keeps only
  the size of its payload, never the payload itself.
- **A model dropping its own prior extended-thinking blocks
  (`thinking_drop`, a prefix mismatch) now joins the `CACHE_SIGNAL`
  family** as a likely cache-bust, alongside thinking being stripped —
  only the closed drop reason and block/turn counts are kept.
- **`cost-state` lines (Claude Code's own running cost total for the
  session) are now read**, numbers only: `totalCostUSD`/
  `hasUnknownModelCost` land on the session's own metadata as a check on
  this tool's own pricing. `reconcile.claude_code_reported_costs(corpus,
  pricing)` pairs each session's self-reported total against this
  tool's own locally-priced total for the same session — the
  `cost-state` half of a later cost-gap metric; no Admin CSV, no network
  call, same as the rest of `reconcile.py`.
- **A tool_result's or a human prompt's own image/document content
  blocks are now sized** by Anthropic's documented Standard-tier
  image-token rule (`tokens = ceil(width/28) * ceil(height/28)`, itself
  capped at 1568 tokens) instead of silently counting as zero characters
  — PNG, GIF, JPEG and WebP headers are read just far enough to get
  their pixel dimensions, never decoded further. A block this parser
  can't size confidently (a document, an oversized or high-resolution-
  tier image, a malformed payload) is now counted as **unsized** rather
  than guessed at (`unsized_blocks`, by block type).
- **A line type no detection rule recognises at all is now counted
  separately** from one this parser knows about and deliberately ignores
  (`unknown_line_types`, apart from the existing `ignored_line_types`) —
  the type name itself is sanitised to a closed, safe token shape (or
  counted as `other`) before it ever reaches a diagnostic counter's key,
  since it comes straight off the wire. Both new counters live on a new
  `parser_notes` side channel next to `Diagnostics` (present only when
  non-empty) and are rendered alongside it by every renderer.
- **`/api/summary` and `/api/daily-usage` take the same window as the
  rest of the API, and both carry more pricing detail.** `/api/summary`
  now accepts `since`/`until` (rounded down to the minute, like a named
  window's own start) alongside the existing `window`/`window_days`;
  with no params it still covers all time, unchanged. It gains
  `cache_read_tokens` and `cache_saved` -- what cache reads in that
  window's sessions (counted whole, like `total_cost`) saved in USD
  against paying the input rate for the same
  tokens, at list price, skipping any model this rate card doesn't
  price. `/api/daily-usage` keeps its `days` param and its output
  exactly as before by default, now also accepts `window`/`window_days`/
  `since`/`until` (taking precedence when given), and a new `split=agent`
  or `split=model` param that breaks each day's totals into main-session
  versus subagent activity or leaves them by model. `report.json`'s
  `meta` gains `rates`: every priced model's own per-million-token rates
  and a few derived ratios, keyed by model id, straight from
  `pricing.toml`.
- **Recommendations, quick actions and habits carry the ids a dashboard
  needs to link between them.** Every recommendation (`/api/recommendations`,
  `report.json`) gains `key`: `id` alone repeats across agent types (the
  same rule fires once per subagent type), so `key` adds a slug of
  `agent_type` when one is set, stays URL-safe (`[a-z0-9._:-]`) and stable
  across two runs of the same corpus, for a link such as
  `#/actions/recommendations?id=<key>`. Each quick-action check
  (`/api/quick-actions`, `/api/quick-actions/<id>`) gains `rule_ids`: the
  recommendation rule ids that check draws its fixes or evidence from,
  `[]` for a check with no rule behind it. Each habit in the "Habits
  worth trying" table gains `covered_by_rule`, the rule id behind its
  existing `covered_by` title, once that rule has actually fired.
- **A `project` filter, on every route that reads sessions.** Every
  report-backed route (`report.json`/`.md`/`.html`, `/api/ttl`,
  `/api/carry`, `/api/compaction-sim`, `/api/model-swap`, `/api/waste`,
  `/api/config-diff`, `/api/recommendations`, `/api/diagnostics`,
  `/api/claude-md`, `/api/claude-md/<id>`, `/api/skills`,
  `/api/profile-goals`, `/api/quick-actions`, `/api/quick-actions/<id>`,
  `/api/whatif`) plus `/api/summary`, `/api/sessions`, `/api/daily-usage`
  and `/api/compactions` now accept `project=<slug>`, narrowing to that
  one project's sessions; an unrecognised slug is a `400 bad_request`
  ("'project' does not match a known project") that never echoes the
  value back. `report.json`'s `meta` gains `projects`: every project
  with a session in the window, already redacted, sorted by that
  window's cost descending (ties broken alphabetically -- the same
  order `usage.by_project`'s own rows already sort by), so a dashboard
  project picker can render straight from it. The report cache key now
  includes `project`, so a filtered and an unfiltered request for the
  same window never share a cached report.

### Changed

- **The dashboard is redesigned around what to change next.** Seven pages in a sidebar replace the sixteen tabs, and every page and segment has its own address. The Overview opens with what the window cost and the five changes most worth making. Charts answer one question each, with **Show as table** beside them. Each figure behind a recommendation links to the exact row it comes from. Ctrl+K searches pages, tables, recommendations, checks, sessions and projects. A project picker narrows the figures to one project. Light and dark themes follow your system until you pick one. Pages and figures move to show what changed, and stop if your system asks for reduced motion. Notes, labels and help are in plain English, and a Glossary term opens its definition in place. The entries below give the detail.
- **The dashboard's sixteen tabs are now seven pages in a sidebar.** Overview, Actions, Spend, Cache, Agents & context, Work habits and Setup, with Data quality and the Glossary below them. A page with more than one part shows them as segments beside its title, such as Spend › Usage, Savings and Sessions. Each page has its own address (`#/spend/usage?w=30`), so Back, Forward and bookmarks work, and each keeps its scroll position. The page title, its segments, Search, the project picker, the window picker and the theme toggle stay pinned while you scroll. The window picker is a menu you can drive from the keyboard, and pages whose figures don't depend on it say so. The sidebar can narrow to a rail of icons (`tls:sidebar`); below 1024px wide it always does. Its foot holds a status line: whether the figures are up to date, when they are from, **Redraw figures** after a scan, the capture level and the version. The capture banner now shows only when a note needs attention; the invitation to turn capture on moved to the status line's link. Cost by model moved from the Overview to Spend › Usage, and the context budget from Config to Agents & context › Context. The last tab you had open opens once as its page. Links between pages replace the old "see the X tab" text, and the dashboard's own words are held to `docs/writing-help.md` by a new test.
- **The Overview now answers "What should I change next?"** It opens with one sentence on the window: what it cost, how that compares with the period of the same length before, how many changes are worth making and what the ways to save come to, in your billing mode. Four tiles follow: Spend (with its change and a daily trend), Available saving (the ways to save on Spend › Savings added up, marked "At most" because they overlap), Saved by the cache (what your cache reads would have cost sent fresh, with what a cache read costs on the model you use most) and Sessions (with the subagent runs). Daily spend is split into your main session and subagents, with your settings changes marked on the days they happened, beside the five next best actions, each with a Copy prompt button. How your setup scores is now five meters, each linking to where to look and to a change that would help. The totals and the billing basis moved into a disclosure, and the full service health moved to Data quality; the warning when the service doesn't start at logon still shows on the Overview. Before any session is read, the Overview says what Token Lens does for you.
- **The dashboard's figures agree from page to page.** The Overview counts, lists and adds up recommendations the way Actions groups them, so both say the same number and a group's saving counts once. The daily spend chart gives its own total and the Spend tile's, and says why they differ: the tile counts whole sessions with a reply in the window. Cache › Rebuilds gives one rebuild count and one name per cause. The session scatter says how many sessions it leaves out and why. A ranking grid opens biggest first, and a folded grid shows every row an action cites. Column headings wrap instead of cutting off, a grid that scrolls sideways shades its edge, and ? on a heading explains the column. The Glossary has a find box, the status line gives times as "5 min ago", and repeated buttons carry what they act on in their names.
- **Actions is now an inbox you can link to.** Recommendations and Checks each show a list to pick from beside the one you picked, with filter chips for importance and area (Models, Cache, Context, Agents, Habits, Data and settings), or for a check's answer. A rule that fires for several agent types is one item with one table of changes: the agent, the value it sets, the saving and a Copy button per row, with the value now said once when every agent shares it. Each recommendation now explains **how it saves you money**: what it costs you now, what the change does to the price with the multiplier from your own pricing ("Reading from the cache costs a tenth of the input price"), and the saving with how sure it is. **The numbers behind this** are links: each opens the page that shows that table, opens what hides it, scrolls to the row and highlights it, or shows the table in a side panel when no page does. Recommendations link to the checks they answer and checks to the recommendations they lead to, and the Overview's next best actions open the one they name. The address says what is open (`#/actions/recommendations?id=<key>`), so Back, Forward and bookmarks return to it.
- **Spend and Cache now lead with the chart that answers each page's question.** Spend › Usage shows daily spend split by main session and subagents or by model (kept in the address as `?split=model`, and through a window change); a day opens the sessions active on it. Spend › Savings opens with the four ways to save side by side, hatched unless measured, and each bar leads to the row its figure comes from. Spend › Sessions plots every session in the window by when it started and what it cost, coloured by how it ran: drag across the chart, or hold Shift and press the arrow keys, to list only the sessions from that stretch, and **Show all sessions** clears it. The list now holds the whole window in one sortable grid instead of pages of 50. Cache › Rebuilds opens with **What the cache does for you**: what the cache saved you after paying for its writes, what avoidable rebuilds cost (rebuilds after a usage-limit pause aren't counted), and how many agent types a 1-hour lifetime would help, each with the price multiplier from your pricing and a link to how it works. Sections draw their chart above their tables: the compaction window, idle gaps against the cache lifetime, the 1-hour lifetime per agent type and startup context. A one-row summary table reads as up to four tiles with **All figures** one click away, and long notes on how figures are worked out fold away, so pages are shorter.
- **Setup, Agents & context and Work habits are shorter, with detail one click away.** Setup › Settings now opens with what your changes did and whether your estimates came true, beside the settings they judge; a change marker on the daily spend chart opens it at that day's change. On Setup › Profiles, "Show what it changes" opens the profile in a side panel. Setup › Capture folds each group of metrics, showing how many are on, and each metric shows what it captures and costs, with why it helps and what Claude writes one click away. Agents & context › Context lists CLAUDE.md files and skills as sortable tables; a row opens the file's review or the skill's detail in a side panel, and the prompt that hides unused skills is folded until you want it. Agents & context › Quality shades each quality signal by how high it runs, so the one to look at first stands out. Work habits keeps its habits, brief templates and kinds of task in view and folds the other breakdowns under **More tables**.
- **Long tables open on their top rows.** A report table of more than 12 rows shows its first 10, in the order you sorted it, with a "Show all N rows" button; a table by day, week, month or five-hour block shows its latest 10, and a link to a later row shows them all first. Cost by MCP server and the main session against subagents context table moved under More tables on Agents & context › Subagents, and the cheaper-model table no longer repeats its saving as a sentence.
- **The dashboard's charts share one frame, drawn with d3.** Each chart asks a question as its title, opens with a sentence that answers it from your figures, and has **Show as table** for the same rows as a grid. You can read it with the keyboard: Tab to the chart, then the arrow keys step from mark to mark. Colours stay with the thing they stand for (the main session, subagents, a model tier), so changing the window never repaints what stays on screen, and money axes follow your billing mode. On the session timeline, usage-limit events sit in one lane per kind, and a run of the same event becomes one mark whose tooltip gives the count and the time span, so they no longer draw over each other. Eight charts use the frame, each catalogued in `docs/ui.md` ("Charts") with the rule a new chart must meet.
- **Text on the dashboard now links where it points, and explains its terms.** Where help, notes, a recommendation, a check, a habit or a session's explanation names another page, the name is a link to it. The first time a card or section uses a term the Glossary explains, such as cache lifetime (TTL), cache rebuild or subagent, the word has a dotted underline: it opens the definition, with a link to the Glossary. A section's "How to read this" ends with a link to the Glossary card that explains the price behind it. A table whose figures back a recommendation says **Feeds N actions** beside its heading, and each row a recommendation cites has a small mark; both list the actions, each a link to its detail in Actions. A habit already covered by a recommendation now links to it.
- **Links in a drawer work.** A popover opened in the session or table drawer can be clicked and reached with Tab, and a link to another page closes the drawer instead of changing the page behind it. A help popover that holds a link takes focus, so the keyboard reaches it. The **Feeds N actions** list names a rule that fires for several agent types the way the Actions inbox does ("7 agent types are sent your CLAUDE.md files every time they start").
- **The Glossary now explains how costs work.** A new "How costs work" segment gives one short card per pricing rule (cache reads, writes and rebuilds, model choice, startup context, tool output kept, conversation summaries, billing mode), each stating the rule, its price multiplier, your own numbers for the window, and a link to the page that acts on it. Terms that a cost card explains now show a "Why it matters" line linking straight to it, and both segments support deep links (`?card=`, `?term=`) that scroll to and briefly highlight the right entry.
- **Search the dashboard with Ctrl+K, and move around it from the keyboard.** Search finds every page, the report's sections and tables, the window's recommendations and checks, glossary terms and cost cards, recent sessions and projects, and runs a few commands: set the window, show all projects, pick a theme, or copy a recommendation's prompt. With nothing typed it lists the pages. G then a letter opens a page (O, A, S, C, E, H, U), `[` and `]` step through a page's segments, J and K move through the Actions inbox and the Sessions list, and `?` shows them all. Search only opens pages and copies text; it never changes Claude Code.
- **Show one project at a time.** A project picker beside the window picker narrows every page that follows the window to one project, listed as its folder name with the most expensive first. The address keeps it (`?project=`) and nothing else stores it, so it never outlives the visit that chose it. Panels that always cover every project say so while one is picked, search finds projects too, and an address naming a project Token Lens doesn't know shows every project and says why.
- **One set of dashboard components and one number format.** Buttons, chips, tiles, panels, callouts, command blocks, drawers, toasts and tooltips now look and behave the same on every page. Every table is one grid: its sort is kept per table, a wide table shows its first 7 columns with a chooser for the rest, the lead measure carries a thin bar, and long tables stay quick. Numbers follow one format everywhere ("$56.7", "1.24M tokens", "2h 14m"), project names read as their folder name, and amounts follow your billing mode on every page, including Setup › Capture; the service's "1,962.05 USD" now reads "$1,962.05". Empty states say what happened and what to do next, loading shows the shape of what is coming, and if the service stops answering the last figures stay on the page, marked stale, while it retries. A change's explainer (what it controls, where, the trade-off and how to undo it) sits under its prompt and command.
- **The dashboard has a new visual system.** Every colour, type size, space and motion value is now a token in `app.css`. Each has a light and a dark value. The theme follows the system, and a saved choice (`tls:theme`) is applied before the first paint. Text is set in Inter and commands in JetBrains Mono, both served by the service itself. A recommendation's severity and a check's status are now chips with an icon and a label, not a coloured card edge. Every focusable control shows the same focus ring. A negative change reads with a true minus sign. The session timeline's markers are drawn in ink, so each one stands out in both themes. Animation stops under reduced motion. d3 7.9.0 ships in `static/vendor/` for the charts. The vendored d3 and fonts are pinned by sha256 in `static/THIRD_PARTY.sha256`, and each name carries its release. They are the one thing the service lets the browser cache (`immutable`); everything else stays `no-store`.
- **The dashboard moves with a purpose, and reads in High Contrast.** A new page fades in over the old one. On the Overview, the headline figures count up to their values, the chart draws in after them and the next best actions arrive one after another. None of it runs if your system asks for reduced motion. Actions' figures load while the Overview sits idle, so Actions opens at once. In Windows High Contrast, charts keep their colours and hatching, while text, gridlines, borders and focus rings use your system's colours.
- **Report notes and column names are in plain English.** Section and table notes, threshold lines and column labels no longer name code fields (`cache_read`, `agent_type`, `share_pct = 10.0%`) or say "USD": a money column reads "Cache write cost" or "Saving if switched", and each note says what its figure means. The pricing note reads "Costs use prices from pricing.toml, version …" without the currency and file hash. Column keys, row values and CSV/JSON fields are unchanged.
- **The dashboard is native ES modules.** `service/static/app.js` is now the entry point that loads a set of small modules (`core.js`, `format.js`, `api.js`, `ui.js`, `icons.js`, `grid.js`, `links.js`, `evidence.js`, `costs.js`, `charts.js`, `charts-types.js`, `palette.js`, `shell.js` and one `page-*.js` per page, with Setup › Capture in its own), loaded with `<script type="module">` and no build step. The service now serves `.js` as `text/javascript` and pins the types for `.css`, `.woff2` and `.svg`: browsers refuse a module script with the wrong type under `nosniff`, and the pins keep Python's `mimetypes` from reading a type out of the Windows registry.
- **Deep includes the feedback survey.** Switching capture into Deep (`capture on`/`level`, `init`, or Setup › Capture) also turns on the `/tl-feedback` survey, its reminder note, and Claude's one-line reminder to run it when a piece of work is done. The CLI and `init` write the skill after the usual diff and yes; Setup › Capture offers the command. The Deep card, its estimate and `docs/capture.md` count the reminder (about 43 more note tokens at each session start). Leaving Deep keeps them, `capture feedback off` removes them, and picking Deep again while already on it doesn't bring them back.

The report's performance work (building the work-habits facts once per
report, and the cache-carry and compaction-replay costing off their
linear paths) changes no figure: each is proven equal to the old
algorithm to 1e-12 on fixtures and randomised sessions.

- **A `[tl: ...]`/`[result: ...]` tag's cost now includes what it costs
  to carry**, not only what it cost to write. Every reply after the one
  that wrote a tag re-sends it as part of the prompt until the next
  compaction — a cache write into the very next turn, then cache reads
  after that — priced the same way a note's own carry already was
  (`context_files._Carry`, one rate for the 5-minute TTL and one for the
  1-hour TTL, chosen the same way `habits._Rates.write` already chose
  between them). This lands in both places capture prices a tag: the
  real, post-hoc `capture.usage()` (already carry-priced tags going into
  this phase) and the pre-enable `capture.history()`/`capture.estimate()`
  path used for "what would this level have cost you" projections, which
  had priced a tag's own output only — found while bringing the two
  paths to parity. The brief marker (`[spawn: ...]`/`[retry: ...]`) is
  words inside the *spawning* tool call's own prompt, not a `[tl:]`/
  `[result:]` tag, and stays priced at output cost alone in both paths,
  unchanged.
- **What capture's habits section is worth to `recommend()`, measured
  rather than assumed.** `recommend()` now runs a second time per report
  with the habits section stripped, and the capture section's
  `habit_value` is the dollar total of recommendations that only exist,
  or grew, with it — matched by `(id, agent_type, lever)`, the larger of
  a pair taken (never summed) when the same lever is named through more
  than one route, weighted down to the share of that total capture's own
  evidence actually reported (vs. inferred from the transcript alone),
  and normalised per week since capture was turned on. A recommendation
  capture's evidence argued *against* making no longer inflates this
  total — it's counted and shown as "held back N", a new row on
  Setup › Capture's own usage table, not folded into the savings figure.
- **A per-metric worth table** on Setup › Capture and in `docs/capture.md`
  (generated, not hand-edited): each metric's own tokens a week set
  against the dollar value of the decisions it feeds, so "is this metric
  worth what it costs" has a direct answer per row instead of one lump
  sum for the whole level. Hidden below `MIN_GROUP` (5) sessions with
  notes, the same small-sample floor the rest of Work habits already
  uses — it built a full row per metric off a single session before this.
- **`spawn`, `detour` and `useful` (the web-result note and its
  `PostToolUse` matcher) are retired from the metrics vocabulary** —
  their evidence didn't hold up against what the transcript already
  shows on its own. A `config.toml` written before this still loads (the
  retired ids are accepted, just no longer asked for or shown). The
  `explore_research` habit now reads `found=no|partial` off the *specific*
  heavy-research cycles it's judging, rather than a corpus-wide count
  disconnected from which cycles it's pricing.
- **Effort index, brief clarity and contradiction flags.** Every rated
  task now gets a percentile-ranked effort index; `d_level = 2·AUC−1`
  scores how well self-reported difficulty actually separates the tasks
  that needed more effort from the ones that didn't (an AUC/Mann-Whitney
  rank-sum computed directly, no numpy/scipy dependency), with
  `brief_clarity_index` its twin for reported brief clarity. Contradiction
  flags (e.g. `check=none` reported on a task the transcript shows was
  actually redone) feed `_self_report_calibration`, and `confidence()`
  now downgrades a habit's confidence when its own self-reports don't
  calibrate against what happened.
- **`autoCompactWindow` swept per kind of task, not only per session or
  agent type.** `compaction_sim_by_task` groups the existing window sweep
  by the task metrics capture reported (`task=`), once at least 5 main
  sessions have reported the same one — the same shape and recommendation
  rule as the existing per-agent-type table. The tasks profile goal now
  drafts that task's own `autoCompactWindow` from it
  (`goals._task_compaction`):
  - only when the task's best window saves at least 5%;
  - only when the corpus-wide sweep says that window summarises no more
    often than the compaction-window rule allows;
  - ticked only once the task has 20 sessions behind it.
- **A note written after a real compaction is priced and shown
  separately.** The carried prefix a compaction would otherwise have
  discounted it against is gone by then, so it costs more — the Work
  habits capture table now shows `sessions_with_notes` and
  `after_compact_notes`/`after_compact_cost` as their own line rather
  than silently folding a higher rate into a scope's ordinary cost.
- **`/api/whatif` and saved profiles now scale to a single kind of
  task, not just the whole window.** A `?task=` query param (validated
  against metrics capture's own closed task vocabulary) scales every
  row down to that task's own share of the window, the same way the
  tasks goal's own draft already did; a saved profile whose `for` names
  a task scales the same way. `refreshTotal` passes it through so a
  saved profile's live total stays scoped to the task it was drafted
  for instead of pricing against the whole corpus.
- **One veto-and-gate helper replaces four independent copies of the
  same model-swap check** (audit finding F9: "model-switch gates differ
  across goals, habits, model_swap and quality"). `model_gate.py` is
  now the one place that checks whether the quality section found a
  model swap did clearly worse (`quality.worse_models`), whether its
  runs on that model were often retried on a larger one
  (`quality.retried_models`, `quality.RETRIED_SHARE`), and whether
  metrics capture said the agent's work needed a larger model or was
  mostly hard (`habits.unfit_agents`) — used by the models goal, a
  single task's candidate, the `model-tier` recommendation and the
  quick-actions tip that explains why a cheaper model wasn't offered.
  It also closes a gap the corpus-wide `unfit_agents` check never
  covered: a *task's own* runs saying a larger model was needed even
  when the agent isn't flagged unfit overall (the "larger model per
  task" veto), and every check now shares one sample-size floor
  (`ModelSwapThresholds.min_sessions`) instead of some running with no
  floor at all.
- **`habits_by_task` now reports main-session cost per task**, with
  inheriting subagents' cost folded in rather than left out, and the
  evidence wording corrected to match.
- **Quality's redo-rate comparison between setups is now a proper ratio
  test with a Holm correction across the setups compared**, instead of
  a raw percentage-point difference. A setup only gets ticked as
  "cheaper" in the tasks goal once it has at least 20 sessions of its
  own (shown from 5, so there's something to look at sooner, but not
  auto-ticked on a small sample); the comparison now also splits by
  parser/schema version and by resolved effort and speed rather than
  pooling runs that may not be comparable, adds a main-only cost
  column, leaves each session's own last message out of the redo-rate
  count (it can't have been redone yet), and adds a hard-work veto so a
  setup that only looked cheaper because it skipped the hard tasks
  doesn't get credit for it.
- **The config snapshot hook now records the per-model `modelSettings`
  effort, `maxEffortLevel` (a hard cap), and whether
  `CLAUDE_CODE_EFFORT_LEVEL` is set** in the environment (a Boolean
  only — never its value). An `effortLevel` override a profile goal
  would otherwise suggest is now marked "won't apply to `<model>`; use
  `--effort`" when the snapshot shows that model has no per-model
  effort setting to override.
- **`fastMode` is now priced instead of ignored.** It's in the
  settings allowlist; `pricing.py` tracks how many turns were actually
  priced at a fast-mode rate versus standard, and the whatif engine
  reprices fast-mode turns at standard rates when asked what turning it
  off would cost (fidelity `simulated`) — fast mode is a documented
  per-model price premium (2x list price), not free.
- **The profile catalogue's `for` lists mixed purposes and tasks, and
  the `ops` task (metrics capture's own closed vocabulary) had no
  catalogue profile at all** (F11). `workflow-ultracode` — scripted,
  multi-step automation and maintenance — is now also `ops`'s starting
  point; `classify.py`'s own comment notes `ops` spans several
  purposes, so this is the closest fit of the seven catalogue shapes
  rather than a clean 1:1 match. *Scoped down from the plan's fuller
  ask (splitting the catalogue's `for` field itself into separate
  `tasks`/`runs` lists): that's a schema-level change touching
  `schema.py` validation, `api.py` routes and `app.js` rendering well
  beyond this fix, so only the concrete `ops` mapping shipped here.*
- **A goal's own "this catalogue profile is a starting point" note
  could recommend a profile whose settings actually contradict the
  draft sitting right above it** (F12). The note is now dropped
  whenever the named catalogue profile's own settings disagree with a
  main-session candidate the draft already proposed for the same key.
- **`omitClaudeMd`'s estimated saving counted Managed policy CLAUDE.md
  tokens, which still load regardless of the setting** (F13,
  `fixes.py:46`). `agent_startup_breakdown` now breaks out a
  `claude_md_managed` column, and the profile goal, the `spawn-
  claude-md` recommendation and the whatif estimate all subtract it out
  before pricing or deciding whether there's enough CLAUDE.md to offer
  the lever at all.
- **Thinking toggles on models where they do nothing** (F14, V26): you
  can't turn thinking off on Opus 5.5 or the Fable models, so
  `alwaysThinkingEnabled` and `MAX_THINKING_TOKENS` do nothing there.
  The thinking goal's lower-effort candidate is still drafted on those
  models, because effort still works (V25) and keeps the cache (V13).
  Its evidence now says the toggles are no way round it, for any agent
  type whose observed model is one of those. The model-swap table's
  per-agent-type column supplies that model. The
  `alwaysThinkingEnabled` diff wording and profiles.md say the same.
- **`autoCompactWindow`'s whatif estimate is now held to the same
  compactions-per-session floor the compaction-window rule and the
  profile goals already use** (EST-P2): a window that would summarise
  more than `CompactionSimThresholds().max_compactions_per_session`
  times a session is no longer estimated, however cheap it simulates.
- **`omitClaudeMd`'s whatif estimate only priced the one cache write
  each spawn, not what carrying CLAUDE.md across the rest of that
  spawn's turns costs afterwards** (EST-P10). It's now priced as the
  greater of the write-only figure (kept as a floor) and the same
  per-file carry cost (cache reads until the file is re-sent) that
  `/api/context-files` already reports, with Managed policy CLAUDE.md
  excluded from both.
- **CAP-7: a suggest-only hint to step `[capture] level` down one
  step**, never applied automatically ("no apply button" holds here
  too — Token Lens never lowers the level itself). `habits.
  capture_step_down_suggestion` fires only once every metric the step
  would drop has its own `capture.enough_target` answers *and*
  `habits.d_level_stability` says the self-report calibration signal
  that evidence backs has settled: its own two independent,
  chronological halves' `d_level` land within `D_LEVEL_STABILITY_
  TOLERANCE` (0.1) of each other — an Assumption, labelled in the
  docstring. Only essentials/standard/deep are ever a target: stepping
  essentials down would land on free, which asks Claude nothing at all,
  a bigger decision already covered by `capture off`/switching a metric
  off one at a time. The report's `capture` section carries the full,
  calibration-gated suggestion (new `step_down_target`/
  `step_down_tokens_saved`/`step_down_weekly_saving` rows and a note
  with a runnable `claude-token-lens capture level <lower> --dry-run`
  command and its undo, in numbers and level names only); the capture
  banner (`capture_view._step_down_note`) shows a cheaper,
  readiness-only version of the same command, since checking
  `d_level_stability` there would need a full habits pass the
  dashboard's poll doesn't already pay for. Both name the metrics the
  step drops, the trade-off (they stop collecting; what they feed keeps
  its evidence so far), where it lands (`[capture] level` in Token
  Lens's config.toml, and Claude Code's settings.json only where the
  lower level needs fewer hook entries) and the undo, via one shared
  `habits.step_down_terms`; the report note gives the session-start and
  subagent-start token savings separately. `capture level <level>
  --dry-run` already existed and works (verified live, in a scratch
  config/claude-root) — no CLI change was needed for that part.
- **`context_files._Carry`'s `index_at`/`cost` off the linear path**
  (ROB-P2): `index_at`'s per-call scan over every turn is now a
  `bisect` over a precomputed sorted-timestamp index, and `cost`'s
  per-call resummation of every turn's cache rate is now two prefix
  sums (one for plain reads, one correcting for a rebuilt turn's write
  rate) plus a direct O(1) correction for "the first turn of the
  queried range always writes" (P10a's warning: that can't be folded
  into the rebuilt-only prefix array, since it applies whether or not
  that first turn is itself in `rebuilt_ids`). `resolve_model` is now
  cached by model string (never the full `effective_rates` result,
  which also depends on the turn's own `ctx`/`speed`/`inference_geo`).
  Reference copies of the pre-change algorithm live in
  `tests/test_context_files.py`, checked for exact agreement (1e-12) on
  fixtures and 8 seeds of 120-turn randomised sessions, plus a
  3,000-turn timing test.
- **`compaction_sim._replay_transcript`'s uncached `lookup(turn.model)`
  now caches by model string per window**, the same pattern
  `habits._Rates._resolve` already used — never applied as an identity
  shortcut to the window's own `dataclasses.replace`-heavy cost
  functions, which mutate `turn.ctx` and can cross the long-context
  threshold.
- **Post-parse stage measurably faster**: profiled the same way as
  P10a (`cProfile` around everything after parsing, on the real
  corpus), post-parse time drops from ~41.8s to ~29.9s (about 29%) with
  these two fixes on top of P10a's.
- **D17: `docs/capture.md`'s per-level note sizes now come from
  `capture_catalogue.rough_tokens`**, the same function the Levels
  table's rendering already had available, instead of a separate
  `len(note_text(...)) / 4` calculation that had drifted from it
  (Essentials showed 182/88 tokens; `rough_tokens` says 201/107, the
  figure an earlier audit had already measured by hand; Standard and
  Deep similarly corrected). Regenerated via `capture_catalogue.
  render_markdown()` — never hand-edit this file — with a new sync test
  (`test_the_levels_table_note_sizes_match_rough_tokens`). No other doc
  quoted the stale sizes.
- **`habits.collect()` walked the whole corpus twice per report** — once
  for the "Work habits" section, again for "Metrics capture"'s
  `capture_dependent_value`. `report.build_report` now runs it once and
  passes the result to both (`habits.section_from`, and a new optional
  `habits.capture_section(..., h=...)`), falling back to its own
  `collect()` only on the rare config that resolves a non-default
  effort-mismatch share threshold, so the two sections can't disagree
  on it.
- **Context-carry costing (`carry.py:_extract_results`) re-summed every
  later turn's cache rate for every carried tool result — O(turns ×
  tool results) per transcript**, the report's single largest post-parse
  cost. It now precomputes each turn's rate once, prefix-sums them, and
  reads off an O(log turns) range sum per tool result instead (`bisect`
  over turn index, since indices can skip). The now-unused per-pair
  `_carry_cost_for_turn` is removed.
- **`ttl.cache_economy`'s `_cache_tokens_at_input_rate` priced every
  cached turn twice** (once for real, once more with its cache emptied,
  via `dataclasses.replace`, just to isolate the input-rate cost) —
  `pricing.effective_rates` already folds in fast-mode, long-context and
  geo the same way, so it's called once and multiplied directly.
- **`habits._CarryCost` re-resolved the same turn's effective rates
  twice**, once each for `.read()` and `.write()`. A new
  `_Rates.read_write()` resolves once and returns both.
- Measured on the smoke corpus (`--all-projects --since <30d> --jobs
  4`), the post-parse stage (everything after transcript parsing) drops
  from a ~25.0s to a ~16.0s median of 3 runs, about 36% -- short of
  halving it. The remaining top hot spots are the same shape of problem
  in `context_files.py`'s `_Carry` (linear `index_at`, an uncached
  `resolve_model` per turn) and `compaction_sim.py` (an uncached
  `lookup(turn.model)` per replay window; its own heavy
  `dataclasses.replace` use doesn't share carry.py's fix, since it
  changes `ctx` itself, which can cross the long-context threshold) —
  both out of this phase's file scope, left for a follow-up.

### Fixed

- Data quality › Setup showed `{page:setup/capture}` in the capture hooks' token cost line while capture was on; it now reads as a link to Setup › Capture. A test stops a page token being written inside an f-string again.
- **A subagent's main-session tag no longer sets the task of the prompt it ran in.** Found by validating live capture: an Explore agent asked only for `[result: ...]` also wrote a full `[tl: task=... level=...]` tag, and SEC-P2's filter never checked the main-session keys in a subagent, so they reached the cycle's merged tag. Keys only the other scope is asked for are now dropped, a subagent's `[tl: ...]` no longer counts as a tag, and a subagent's `out=` is kept when a large-output note asked for it. The cost weighting also skips retired metrics (`detour`, `web`) instead of failing on an older transcript. PARSER_VERSION 21: every transcript is re-parsed once.
- **A catalogue profile's "Estimated effect" on Setup › Profiles never
  appeared.** The page sent the profile's first `for` word
  (`implementation`, `data-exploration`, ...) to `/api/whatif` as its
  task, which only takes the capture task words, so the request failed
  quietly. Profiles now carry `tasks`, their `for` words normalised to
  those task words, and `/api/whatif`'s `task` takes several,
  comma-separated, scaling by their combined share. The Work habits
  digest's money cards also follow the billing mode now instead of
  printing a bare amount in USD.
- **Repeated reads were miscounted.** An edit counted as a read of the
  same file, and a read straight after an edit to it counted again too
  (870 -> 138 repeated reads on a real corpus once fixed). Agent report
  size also used the agent's own last output tokens instead of the
  result its parent actually received (or the task notification for a
  background agent), and `hook_system_message` lines — shown to you
  only, never sent to Claude — were counted as context.
- **Fast mode was ignored when pricing a quality marker's cost** (the
  `[tl: ...]`/`[result: ...]` tags and capture notes): every marker
  priced at a turn's standard rate even when that turn ran at 2x fast
  mode, long-context or data-residency rates. Marker cost is now priced
  at the turn that actually wrote it, using the same effective rate the
  context-carry figures already use.
- **A `.pyz` or wheel install could ship or run without the capture
  hook script.** `hooks/capture-hook.py` is now installed from package
  resources the same way the config-snapshot hook already was (fixing
  `install_hook` inside a `.pyz`), and was missing from package data
  entirely for a wheel build; a test now checks every hook file is
  actually shipped.
- **A live coaching hint could claim more cache life than a lifetime
  allows.** When a reply's own timestamp was stamped ahead of the
  clock (clock skew, a resumed session), the cache-freshness estimate
  behind the statusline's coaching line could read past a full TTL. It
  is now capped at the cache lifetime in effect.
- A skill run with a slash (`/tl-feedback`, `/tl-brief`) wasn't
  recorded in `commands_run`, because Claude Code writes it inside a
  `<command-message>` block first; it is now.
- **The dashboard could stop updating for good and still report
  healthy.** When the database was busy at the moment the background
  scanner started a scan (a second `serve` on the same database, say),
  the scanner thread died. The page kept serving the figures it had,
  frozen, while `/api/health` still said `ok`. A failed scan now fails
  only that scan, the scanner retries at the next poll, and a busy
  database is waited on for up to 30 seconds instead of 5.
- `/api/health`'s `status` is no longer always `ok`: it is `starting`
  during the first scan, `degraded` when the last scan failed and
  `stale` when the scanner has stopped or nothing has finished for ten
  minutes, with a plain-words `message` and the scan's progress
  (`scan`). The dashboard shows these in a banner on every page and in
  the sidebar's status line, with the command to restart it.
- A failed scan's error now keeps SQLite's own reason ("database is
  locked") rather than only "OperationalError".
- Two `serve`s on one database are refused. `serve` locks its database
  for as long as it runs; a second one exits naming the process that
  holds it and its address, and `serve --purge` refuses to delete a
  database a running `serve` has open.
- A port already in use is reported in a sentence instead of a
  traceback.
- Setup › Profiles' "Or try it for one session" command was
  `claude --settings <config-dir>/profiles/<id>.settings.json`: a
  placeholder, pointing at a file that only `apply <id> --launch`
  writes. It is now `claude-token-lens apply <id> --launch`, which
  writes the file and prints the command with its real path, and the
  page says when the profile's agent or environment changes can't come
  along for a one-session trial.
- `--since` and `--until` given a bare date (`--since 2026-09-01`), as
  the README documents, or a time with no offset, crashed comparing it
  with the transcripts' own times. Both are now read as UTC.
- **Claude Opus 5.5 had no rate card entry, so it silently priced at
  Opus 5's rate: a quarter too much on input and output and two and a
  half times too much on cache reads.** `pricing.toml` now carries its own row (input $4,
  output $20, cache writes $5/$8 for a five-minute/one-hour TTL, cache
  reads $0.20, all per million tokens, plus the documented 1.1x "us"
  data-residency uplift) instead of falling back to a prefix match on
  the shorter "claude-opus-5" id. Every other rate in the file was
  checked against the current pricing page while this was open; none
  needed a correction.
- **A reply priced against another, similar model's rate — because its
  own model id only prefix-matched, not because it had its own
  pricing.toml row — counted as "100% priced," so the report read as if
  every model had an exact price.** Spend › Usage now gets a "Priced by
  closest match" table (model, priced as, replies, tokens) whenever this
  happens, the Data quality page's counters and the `pricing-coverage`
  recommendation name it too, and `pricing-check --models` marks a
  closest-match resolution `(closest match, not this model's own rate)`.
  The coverage percentage itself is unchanged — a closest-match reply
  still counts as priced, since its cost isn't zero — only the wording
  now says so plainly instead of implying an exact price.
- **Fast mode (`usage.speed == "fast"`, currently 2x standard rates on
  Claude Opus 5.5, Opus 5 and Opus 4.8) was ignored and every reply was
  billed at its standard rate regardless.** `pricing.toml` now carries a
  `[models."<id>".fast]` multiplier for those three models, and a fast
  reply on a model with no such table still prices at standard (as
  before) but now says so: a new "Fast turns priced at standard rate"
  table on Spend › Usage, and matching Data quality page counters, name
  which models and how many replies. Recording each reply's own speed
  needed a new `Turn.speed` field, hence the `PARSER_VERSION` bump above.
- **The `opus`/`opus[1m]` aliases resolved to Claude Opus 5 instead of
  Opus 5.5**, so a session or agent config that named the family alias
  priced (and reported) as the older model. `pricing.toml` now carries
  those aliases on `claude-opus-5-5`; `claude-opus-5` resolves only by
  its own id.
- **Web search requests were tracked but never priced.** `[server_tools]
  .web_search_per_1000` was `0.0` and nothing read it. It's now $10 per
  1,000 requests (the documented rate), included in every turn's total
  as its own `server_tool_cost` line; `web_fetch` requests are still
  counted but have no documented per-request rate, so they remain
  unpriced.
- `xhigh` was a real effort level Claude Code accepts for
  `effortLevel`/an agent's `effort`, but the profile schema's closed
  vocabulary didn't include it, so a profile or observed session using
  it failed validation. `_EFFORT_LEVELS` now lists it between `high`
  and `max`.
- **Every "near the context limit" table (autocompaction, huge-context
  cache reads, the optimisation scorecard, the compaction-window sweep)
  assumed a flat 200,000-token window regardless of which model was
  actually running**, so a session on a natively 1M-token model (Fable
  5.1, Fable 5, Sonnet 5, Opus 4.7 and later) was flagged as constantly
  near its limit when it had 5x the room. `pricing.toml` now carries
  each model's real `context_window_tokens` (1,000,000 for the models
  above, 200,000 elsewhere by default), and `context_budget.py`,
  `recache.py`'s huge-context table, the scorecard's context-hygiene
  threshold and `compaction_sim.py`'s candidate-window sweep (widened
  past 500,000, up to the ~967,000-token point Claude Code itself
  compacts a 1M window at) all resolve it per model instead of assuming
  200,000. Expect fewer "near the limit" warnings on 1M-context models —
  that's the correct behavior, not a regression.
- **A what-if model change was labelled "Simulated" like a real replay,
  when it's actually a ceiling** on the saving: the same tokens
  repriced at the new model's rate, which can't capture that model
  needing more or fewer replies for the same work. It now gets its own
  `fidelity: "ceiling"` (`fidelity_text` explains the difference),
  matching the wording `/api/model-swap` already used for the same
  number.
- **The dashboard still said "Apply" in two places, and kept a latent
  fallback that would have shown an apply-directly command if the
  dry-run one were ever missing** — this tool never changes your Claude
  Code config itself (see "What the dashboard can change" in
  `SECURITY.md`). "Apply it to:" is now "Target file:" (it picks which
  settings file a profile's diff targets), "Apply tags" is now "Save
  tags" (it writes to this tool's own tag overrides, not Claude Code),
  and the dry-run command box no longer falls back to
  `data.apply_command`. A new static test fails if any button label or
  click-handler name says "Apply" again.
- **A shell command with one huge, unbroken run of characters (a base64
  heredoc body, say) could take minutes to parse.** Redacting a
  command's paths/URLs/user@host targets ran its regexes over the
  *whole* command before only ever keeping the first 40 characters of
  the result; one of those regexes backtracks quadratically over a long
  run with no `@` and no whitespace, so a 1 MB such command could take
  tens of minutes. The command is now capped to 512 characters (at a
  whitespace boundary, so a token straddling the cut is dropped whole
  rather than left as a raw, un-redacted fragment) before redaction
  runs at all — 1,000x the kept length, so this changes nothing for any
  realistic command, only the pathological ones.
- **The comment explaining why the metrics-capture hook that adds a
  large-tool-result note runs in the foreground was wrong.** It said
  Claude Code ignores what a background hook prints; the docs actually
  say an async hook's `additionalContext` does reach Claude, just on
  the next conversation turn — a full reply late for a note about the
  result Claude just saw, which is the real reason this stays
  synchronous. No behavior changed, only the comment (`capture-hook.py`,
  `capture_catalogue.py`), re-verified against the current hooks
  reference.
- **`GET /api/profiles/<id>` and `.../diff` could be made to read a file
  outside your profiles folder.** The route only ever sees one raw path
  segment (`..`/`/` would already fail to match), but a percent-encoded
  separator (`..%2F..%2Fetc%2Fpasswd`, `C:%5CWindows%5C...`) hid it from
  that check and was then decoded back into a real separator before the
  id reached the filesystem. `_load_profile_by_id` now checks the
  decoded id against the same shape a profile's own id must already
  satisfy to be saved (`^[a-z0-9-]{1,40}$`) before touching disk;
  neither route ever served anything outside `<config-dir>/profiles/`
  under a valid id, but this closes the traversal for good.
- **No `POST` route capped how large a request body could be.** This
  API has no authentication (local-only, by design), so any local
  process could force an arbitrarily large body to be read into memory
  and JSON-parsed on every `POST` route. Every route now caps the body
  at 64 KB — checked against `Content-Length` before a byte is read off
  the socket, `413 payload_too_large` otherwise. Every route's actual
  body (a profile, a tag, a feedback payload) is small hand-typed or
  hand-picked JSON, well under the cap. See "Body size limit (G5)" in
  `docs/api.md`.
- **A capture tag glued straight onto the reminder sentence could eat
  part of it, or leave part of the tag behind as if it were prose.**
  `capture_tags.py` now strips the exact reminder sentence out of a
  reply before it looks for the tag at the tail, whichever order Claude
  wrote them in; the catalogue's own reminder text is placed before the
  tag it explains, not after, so the two are never adjacent to begin
  with either.
- **A forged `[tl-fb: ...]` line — typed into a reply by hand, or
  copied from an earlier one — could count as real `/tl-feedback`
  answers.** It now counts only when the cycle's first turn actually
  ran `/tl-feedback`; work-habit calibration already only reads real
  answers and dashboard ratings, never the tag itself.
- **A captured tag key could be kept even when nothing asked Claude for
  it** — a model volunteering a field the current level never
  requested, or a stale key surviving a level change mid-session. A
  tag key is now kept only when the session's own note asked for it
  (there was at least one capture note) and the key names a metric that
  note actually requested. `reported_task` is now counted once per
  cycle instead of once per line that mentions it, so a reply that
  repeats its own tag doesn't inflate the count.
- **A skill could claim a capture note was meant for it by naming
  itself in a reply, and a name that didn't match Claude Code's own
  skill-id shape was recorded as if it were real.** `Turn.skills_invoked`
  now validates every name against the same pattern Claude Code itself
  uses for a skill id, and drops a skill whose `Skill` tool call
  actually errored — a skill can no longer self-authorise its own
  capture note by name alone.
- **Coverage undercounted or overcounted depending on what a cycle
  actually was.** Feedback cycles (`/tl-feedback` itself), interrupted
  cycles and cycles that hit `max_tokens` are no longer counted in the
  coverage denominator — none of them could ever carry a normal tag, so
  they only ever diluted the percentage. A cycle's tags are now merged
  key by key as later lines arrive instead of one line's tags replacing
  the whole set, `_big_output` is sized per tool call instead of once
  per turn, and `_WRAP`'s per-hook accounting carries the `:Tool` suffix
  it was missing.
- **The 14-day capture time-box (`capture on`'s default `--for`) only
  applied from the interactive `init` flow.** Non-interactive `init`,
  `capture on`/`capture level`, `config.set_capture` and
  `POST /api/capture` now all default a fresh switch-on to the same 14
  days unless `--capture-no-limit`/`--for`/`--until` says otherwise or
  an `until` is already set. `init --capture-for DAYS` sets a different
  default non-interactively; the help text for the existing flags now
  says what happens when none of them are given.
- **A downgrade to an older schema version, followed by an upgrade back,
  silently dropped your `/tl-feedback` answers and any capture tags the
  older schema doesn't know about.** `session_feedback` and every
  captured tag are now exported before a downgrade drops them and
  re-imported after an upgrade brings the columns back, verified with a
  v6-to-v5-to-v6 round trip. Each schema step's backup file is now
  timestamped (`.bak-<version>-<timestamp>`) so a second downgrade in
  the same run never overwrites the first one's backup.
- **`retention_days` and `exclude_projects` in `config.toml` were
  trusted without checking.** `retention_days` is now rejected outside
  1–36500; each `exclude_projects` pattern is compiled once at load
  (a bad regex fails fast, naming the pattern, instead of failing later
  inside the hook), and the hook itself now skips one bad pattern at a
  time instead of a bad pattern anywhere in the list silently
  disabling every exclusion. The `config.toml` writer now escapes
  control characters instead of writing them raw, and re-parses the
  file it's about to write before replacing the real one, so a bug in
  the writer can never leave `config.toml` unreadable.
- **The on-disk digest cache had no way to notice a vocabulary or
  scoring change that didn't come with a `PARSER_VERSION` bump.** Cache
  entries now live under a `cache/p<version>/` folder per
  `PARSER_VERSION`, and each entry also carries a fingerprint hash of
  the closed vocabularies, labels and prompt flags it was written
  against — a mismatch on either is a cache miss, same as before. Old
  version folders past 14 days old are pruned automatically.
- **A subagent running inside a workflow (`subagents/workflows/<run
  id>/agent-*.jsonl`, one directory deeper than an ordinary
  `subagents/agent-*.jsonl`) wasn't recognised as a subagent transcript
  by the capture hook**, so it never got the subagent capture note
  after a compaction. Subagent detection now also matches on the
  transcript's own filename shape, not only its parent directory name.
- **A keyboard user tabbing onto a dashboard panel lost the focus
  ring** (`.panel:focus-visible` had turned outlines off entirely); it's
  back, offset so it doesn't crowd the panel's own border.
- **The lever grid could overflow a narrow phone screen** — its columns
  had a hard 320px minimum wider than some phones' own viewport. The
  minimum is now `min(320px, 100%)`, so a column shrinks to fit instead
  of forcing horizontal scroll; the advanced-detail panel's raw diff
  text now wraps for the same reason instead of running off the edge.
- **`.rec-severity-action`/`.rec-severity-advice`'s left-border colour
  had no dark-mode override**, unlike every other severity colour on
  the same list, so both looked identical (and hard to read) in dark
  mode; they now have one.
- **Timeline markers were told apart only by colour** — recache,
  compaction, spawn, human-turn, limit-hit, limit-resume and
  agent-terminated each now draw a distinct shape (circle, square,
  triangle up/down, diamond, plus, x) as well, and the legend's swatches
  match.
- **The "Copied" label on a code block's copy button showed even when
  the clipboard write actually failed** (no `navigator.clipboard`, or a
  denied permission); it now only claims success once the copy really
  went through, and says so when it didn't.
- **The health and capture banners, and their live regions, rebuilt
  themselves — and could re-announce identical text to a screen
  reader — on every refresh even when nothing in them had changed.**
  Both now skip the rebuild when their content signature hasn't moved.
  The capture banner's "Hide" was a permanent, one-way dismissal; it's
  now a 7-day snooze (reappears after a week, same as the notes list's
  new "Dismiss for a week"), and a session with an old permanent
  dismissal already on disk is treated as merely expired rather than
  needing a migration.

- **`SECURITY.md` corrected against the current code.** It claimed
  `/tl-feedback`'s answer was checked for a question mark or negation
  before being kept — there is no free-text answer at all; all four
  questions are checkbox-only, and the section now says so and points at
  `POST /api/sessions/<id>/feedback`. It also claimed a wait signal
  records how long you waited before answering a prompt — only the
  categorical kind (permission/idle/question/agent/quota/other) is ever
  logged, never a duration, and the text is corrected to say so. The
  "what the dashboard can change" section now documents the feedback
  POST and P8's `POST /api/predictions/seen` alongside the existing
  tags/profiles/capture routes. The network section now documents that
  `update` is the one command that reaches the real internet — a pip
  install from the unpinned GitHub source, plus two loopback
  `GET /api/health` probes — instead of undercounting it as one function.
- **The README's tab table and glossary now match the dashboard**, with
  a regression test for each (`test_service_static.py`): the "What each
  tab answers" table was missing its Work habits and Capture rows (14 of
  16), and the glossary was missing the nine metrics-capture terms
  app.js's own `GLOSSARY` never got (Metrics capture, Capture level,
  Tag, Prompt cycle, Work habits, Feedback skill, Brief templates,
  Sampling, Time-box) — added to both README.md and `app.js` so they
  read the same. `docs/ui.md`'s "fifteen tabs" and "fourteen-tab"
  summaries are corrected to sixteen. `docs/api.md`'s quick-actions
  summary now lists all ten `quick_actions.CHECK_IDS` (it was missing
  "quality"), with its own sync test in `test_quick_actions.py`.
- **`Diagnostics.ignored_line_types` now sanitises its key the same way
  its sibling `unknown_line_types` already did.** Both are populated
  from a line's own top-level `type` — wire input, not a trusted enum —
  but only `unknown_line_types` ran it through `sanitize_line_type`
  first; `ignored_line_types` (a type the parser recognises and
  deliberately drops, including any `file-history-`/`artifact-`-prefixed
  or unclassified type) stored it verbatim. Neither the privacy suite's
  generic field walk nor `assert_privacy` opens a `dict`-typed field
  key-by-key, so this had no fixture catching it; both now do the same
  sanitize-or-`"other"` before the key is ever used.
- **New privacy fixtures** (`tests/test_privacy.py`) put a `[tl: ...]`
  tag, a `[tl-fb: ...]` tag, `/tl-feedback`'s AskUserQuestion answers,
  and a capture note through `parse_transcript` for the first time in
  this file, proving unknown keys, free-text "Other" answers, and a
  note's own surrounding text never reach a `Turn`/`Event` field; plus a
  fixture locking in the `ignored_line_types` fix above. Skill names
  already had a dedicated fixture (SEC-P3); not duplicated.
- **`docs/ui.md` no longer says the Sessions list and the Usage tab's
  compaction list ignore the date window.** Both now honour it
  (`app.js`'s `withWindow("/api/sessions?...")` and
  `withWindow("/api/compactions")`, backed by `route_sessions`/
  `route_compactions`'s own `_listing_window`) — confirmed against the
  other panels the same sentence names (the Cache tab's rebuild counts,
  the baseline panel, "Your changes and what they did", the setup panel
  and service health), which genuinely still cover all history.
- **`docs/api.md` now names metrics capture as a source of change points**
  in both the `change` window's description and `/api/impact`'s intro
  sentence — `change_points.py` already treats a `capture-log.jsonl`
  entry as one (`source: "capture"`), and the API's own 400 error text
  already said so; only the docs were behind. `/api/capture`'s `data`
  key list now includes `feedback`, which was already fully documented
  below it but missing from the summary tuple.
- **The README's "Reading the report sections" table and
  `docs/sections-reference.md`'s order sentence now list every section
  `report.build_report` actually emits, in its real order**
  (`report._SECTION_ORDER` plus the unconditionally-appended
  `baseline_comparison`): both were missing `habits` and `capture`, and
  the README table was also missing `elasticity`, `agent_startup`,
  `context_budget` and `baseline_comparison`. New regression test
  `test_readme_and_sections_reference_list_every_report_section_in_order`
  (`test_service_static.py`) keeps both in sync with `_SECTION_ORDER`.
- **The README's workstyle row now names all seven archetypes**
  (`workstyle.py` detects `mixed` — the fallback when none of the other
  six match — alongside the six named ones), with a new
  `test_readme_workstyle_row_names_every_archetype` regression test.
- Checked the CHANGELOG's latest released version heading against
  `__version__`/`pyproject.toml`: both already read `0.5.2` — no fix
  needed.

## [0.5.2] - 2026-09-23

### Fixed

- **Short windows counted sessions with no replies in them.** A session
  counted in a window when its transcript file last changed there, and
  Claude Code (the desktop app especially) appends titles and other
  notes to old transcripts. "Last 24 hours" could show a week-old
  session's full cost with nothing run that day. A session now counts
  when its last reply falls in the window, in the dashboard, `report`
  and every other command (`--window-by last-reply`, the new default;
  `mtime` keeps the old rule).
- The dashboard's Sessions list and the Usage tab's conversation
  summaries ignored the window picker and always listed everything; both
  now show only what falls in the window. `GET /api/sessions` and
  `GET /api/compactions` accept the same window parameters as the
  report.

## [0.5.1] - 2026-09-23

After updating, the first dashboard start re-reads every transcript
(a few minutes), and output and thinking tokens, and the costs built on
them, come out noticeably higher: they were undercounted before.

### Fixed

- **Output and thinking tokens were undercounted by more than half.**
  Claude Code writes a streamed reply as one line per content block, and
  only the last line carries the full output count and the thinking
  count; the parser took the first line's. Cached digests are re-parsed
  (`PARSER_VERSION` 12).
- **The auto-compact window advice overstated savings several times
  over.** The simulation dropped a session's context to about 15% after
  each summary, but the system prompt, tools, CLAUDE.md and skills
  listing (about 80,000 tokens on a typical corpus) stay. A simulated
  summary now leaves the session's own starting context plus a summary
  of your usual size, fires the same distance below the window as your
  real ones (a 300,000 window fires near 267,000), and is charged for
  the summary request and the re-cached reply after it. The rule's
  "at most 2 summaries a session" limit now also applies to the
  compaction profile goal. When `autoCompactWindow` is already the best
  point, the compaction check now says so, and says that its last
  column compares each point with your sessions as they ran.
- **The model and quality checks gave opposite advice for the same
  agent.** A cheaper model the quality section found an agent did worse
  on is no longer suggested for it by the models check, the report's
  model-tier card or the models profile goal; each says it was left
  out. A setup clearly worse on some signals and clearly better on
  others is now "Mixed" rather than "Worse", with no switch offered, and
  going back to a larger model says it costs more rather than warning
  about extra replies.
- **The skills review offered to hide skills Claude Code's own tools
  need.** The Artifact tool tells Claude to load `artifact-design`,
  `artifact-capabilities`, `artifact-diagramming` and `workshop`, and
  the Workflow tool `workflow-authoring`; hidden, those tools'
  instructions break. They are left out of the hide-all fix, marked
  "Needed by a Claude Code tool", and offered `name-only` instead.
- **The skills review offered to hide skills you had deleted.** A skill
  with no file left on disk was labelled "Built into Claude Code" and
  offered for hiding, though hiding it saves nothing once it's gone. One
  that no listing has named for 14 days before the newest one is now
  "Removed", marked "No longer listed", and gets no fixes.
- **The CLAUDE.md review called sections agent-only when they weren't.**
  An agent with a one-word name (`claude`, `Explore`, `Plan`) matched
  every "Claude Code", `.claude/` path or "plan", so whole files were
  offered for moving into that agent. A one-word name now counts only
  in backticks, before "agent" or "subagent", or as a `subagent_type`.
- **The shell output cap quoted every shell result's cost as its
  saving.** The tool-output check now prices each cap on the results it
  would cut (the new `carry_output_cap_savings` table) and offers it
  only when it saves at least 10% of what those results cost; otherwise
  it says why not. The `tool-output-carry` recommendation now quotes
  that tool's own saving (`carry_by_tool`'s new `saving_if_capped_usd`)
  rather than every tool's.
- **Failing tests and hook blocks were counted as wasted tool errors.**
  The parser now records why each tool call failed (the kind only, never
  the text). A command that ran and reported failure, such as a failing
  test or build, is no longer wasted (it is counted in `waste_summary`'s
  new `failed_command_turns`); a hook or Claude Code guard block is its
  own `blocked` cause with its own lever; a denial counts as
  `tool-denial`. `tool-error` now means a call that couldn't run as
  written.
- **Advice about another project's agents.** Recommendations, `check`,
  and the dashboard's quick actions and profile goals read agent
  settings from the newest config snapshot only, which records just the
  agents of the project it was taken in. Another project's agents showed
  as "not set", custom agents could be called built into Claude Code,
  and fixes pointed at `~/.claude/agents` with `--scope user` instead of
  the project's own agent file. Agents now come from every project's
  latest snapshot.
- **Changes already made were still recommended.** Advice is measured
  over the whole period, so a change made part-way through it kept being
  offered at its full saving. A change the current config already makes
  (a model alias such as `sonnet` matches `claude-sonnet-5`) is now left
  out, and a card with nothing left to change is dropped. Quick actions
  and profile goals also read an agent's cache lifetime and max turns
  under the wrong names, so they always showed as "not set".
- The skills review no longer offers to hide a skill that
  `~/.claude/settings.json` already hides, or one whose plugin is turned
  off; offering `user-invocable-only` for a skill set to `off` would have
  shown it again. Such skills are marked "Already hidden".
- The dashboard asked for each report once per tab: opening several tabs
  built the same slow report several times over. Requests for a report
  that is already being built now wait for that build.
- **Dashboard layout and wording.** Overview's two "start here" links ran
  together into one line ("…and howOr check…"). Long prompts on Context
  files and the sessions table widened the whole page past the window;
  code blocks now wrap and wide tables scroll in their own box. Money
  reads `1,234.56 USD` as in the report; times read `2026-09-23 13:26 UTC`
  rather than raw ISO; session ids show 8 characters with the full id on
  hover; skill descriptions in Quick actions are shortened; setting values
  and counts get thousands separators. The Usage tab's "Recent
  conversation summaries" showed the oldest 50, not the newest, and
  formatted its transcript number as a quantity ("1,038"). Quick
  actions' evidence buttons now say what they show and hide.

## [0.5.0] - 2026-09-23

### Added

- **Sessions from WSL.** `init` finds Claude Code sessions inside WSL
  distros (`\\wsl.localhost\<distro>\home\<user>\.claude\projects`) and asks
  whether to include them. They go in `config.toml`'s new
  `extra_projects_roots`, which the dashboard, the logon service and
  every command read alongside this computer's own folder.
  `--projects-root` can now be given more than once.
- The Sessions tab shows a **Where** column ("This computer" or
  "WSL: Ubuntu") when any session ran outside this computer, and the
  session detail says where it ran. `GET /api/sessions` and
  `GET /api/session/<id>` carry it as `source`, never the path.
- **`update`**: installs the newest version and restarts the dashboard
  on it in one command (`python -m claude_token_lens update`).
- `install-service` says when the dashboard answering on the port is a
  different version from the one just installed: an older copy is still
  holding the port.

### Changed

- The dashboard no longer marks a folder's transcripts missing while
  that folder is out of reach (a WSL distro that was shut down).

## [0.4.1] - 2026-09-23

### Changed

- **Updating is two commands.** On Windows, `install-service` now stops
  a dashboard the logon task already started, re-registers the task for
  the Python you ran it with, and starts it straight away (a first
  install no longer waits for the next logon). On Linux it also restarts
  the service. So after `pip install --force-reinstall`, running
  `python -m claude_token_lens install-service` switches the dashboard
  to the new version.
- The dashboard's footer and `GET /api/health` (`version`) show the
  running version, so a dashboard still on an old copy is easy to spot.
- `init` asks its questions in plain words ("How do you pay for Claude
  Code?"), and the billing question accepts `pro`, `max`, `team`,
  `enterprise` and `plan` as `subscription`.
- The README's quick start covers checking Python, installing,
  updating, uninstalling and what to do when `claude-token-lens` isn't
  found.

### Fixed

- The hook health check expands `%VAR%` on every platform, not only on
  Windows, so its tests pass on Linux CI.

## [0.4.0] - 2026-09-23

### Added

- **Monthly reports from the dashboard.** `serve --monthly-report DIR`
  now writes last month's report into DIR when it's missing, checking at
  startup and every hour; a failure logs one line and is retried later.
- **Sessions record their profile.** Each session carries the profile
  active when it started (from the config hook, `apply` and undone
  applies), so `compare --a profile:<id>` selects real sessions and the
  session list shows the profile.
- **Usage-limit headroom.** For subscription users with usage-limit
  readings, the new `elasticity` section (Usage tab) shows how many
  tokens a full limit holds and how much of the weekly limit the last
  day used; `[thresholds.elasticity]` in `config.toml` now takes effect.
- Replies from a model with no price are listed under the Usage tab's
  advanced detail, and the "Some usage has no price" recommendation
  names those models.
- **Quality signals: is the work going well?** A cheaper model or lower
  effort only saves money if the work still gets done. The new
  `quality` section (Agents tab, `quality` command) counts, per agent
  type and per model and effort: agent runs that didn't finish
  (reported failure, stopped, never replied, or cut off, and those that
  most likely ran out of turns), failed tool calls and shell commands,
  denials, replies you stopped, your corrections (a yes/no from a fixed
  phrase list, text never kept), files edited again and replies cut off
  at the output limit. Setups are compared with the one each agent used
  most, with a per-run z-test and a Holm correction; "Your changes and
  what they did" compares them before and after each change; and the
  Quick actions check "Is any agent struggling?" turns both into fixes
  (`quality.py`). Agent outcomes are read from task notifications,
  including queued ones, and a workflow agent's from its run file. The
  parser keeps stop reasons, tool calls and errors by tool, and edit
  targets as salted hashes (`PARSER_VERSION` 10: sessions are re-read
  once).
- **Quick actions tab and `check` command.** Ten questions: nine, one per
  way of saving: the right model per agent, effort, the summary point
  (`autoCompactWindow`), cache lifetime, unused tools and MCP servers on
  agents, unused skills, CLAUDE.md size, large tool output
  (`BASH_MAX_OUTPUT_LENGTH`, `MAX_MCP_OUTPUT_TOKENS`) and habits, and
  whether any agent is struggling. Each
  always answers, including "nothing to do", with the evidence table,
  fixes (a prompt, and a `--dry-run` command for a plain setting) and
  tips (`quick_actions.py`, `GET /api/quick-actions`,
  `GET /api/quick-actions/<id>`).
- **Context files tab and `review claude-md|skills`.** Each CLAUDE.md
  file's size by section, how often it was sent and to which agents, its
  estimated cost, agent-only sections, duplicates and stale references,
  each with a fix prompt. Each skill's description, source, how often it
  was listed and used, and one fix that hides every unused skill
  (`skillOverrides`). Transcripts now keep per-file and per-skill sizes
  (never text); file text and skill descriptions are read on request and
  never stored (`claude_md_review.py`, `skills_review.py`,
  `context_files.py`, `GET /api/claude-md`, `GET /api/claude-md/<id>`,
  `GET /api/skills`).
- **Create a profile from a goal.** Pick a goal (spend less on
  subagents, cheaper models, cheaper cache, shorter conversations, less
  thinking, from my recommendations, from my current settings), tick the
  changes your data supports, see the what-if estimate update, name it
  and save it (`profiles/goals.py`, `whatif.py`,
  `GET /api/profile-goals`, `POST /api/whatif`). Profile details show
  their estimated effect.
- **Your changes and what they did** on the Profiles tab: each `apply`,
  undo or settings change, with sessions before against sessions after
  on the measures that change should move (`change_points.py`,
  `impact.py`, `GET /api/impact`).
- **Window picker in the header**, for every tab: the last hour, today,
  the last 24 hours, 7/30/90 days, all time, or since my last change
  (`?window=1h|today|24h|change|all`, or `?window_days=N`, on every
  report-backed route).
- **What this tool installed, and what to expect** on the Data quality
  tab (`GET /api/setup`) and in `changes`: it never uses your Claude
  tokens, the hook and statusline add none, and what each piece does
  and how to undo it. New `uninstall` command removes the hook,
  statusline and service, and with `--revert-changes`/`--delete-data`
  undoes applied changes and deletes the data folder.
- `init` connects the snapshot hook (and a statusline when you have
  none) after showing the exact `settings.json` diff and asking;
  `--connect` skips the question. `settings.json` is backed up first.
- `skillOverrides` joins the profile settings allowlist;
  `enabledPlugins` is now a name-to-on/off map merged into the existing
  object.
- Glossary entries for Window, Change point, Quick action, What-if
  estimate, CLAUDE.md and Skill.
- **Start here** on the Overview tab: the three most important
  recommendations for the selected window, with why and the estimated
  saving, and any scorecard area rated poor or worse.
- **"Why was this session expensive?"** in each session's detail:
  template sentences and a cost split from
  `GET /api/session/<id>/explain` (`service/explain.py`,
  `Store.session_parts`, `Store.median_session_cost`). No LLM.
- **Profiles tab redesigned.** Cards show "Suggested for you",
  built-in or yours, and which settings a profile changes. The detail
  table reads Setting / Now / After / Set in, marks settings locked by
  policy or already set, and ends with "Ask Claude to do it" (a
  prompt), "Or run this command" (`apply <id> --dry-run`) and "Or try
  it for one session". A form editor built from the schema ("Start
  from", settings, per-agent fields, JSON as an escape hatch) and
  "Save my current settings as a profile" write only to this tool's own
  profile store.
- **Glossary tab**, worded the same as the README's glossary (the two
  are kept in step by hand).
- New read-only routes `GET /api/profile-schema` (every allowlisted
  key with label, type, allowed values, description and trade-off) and
  `GET /api/profiles/<id>`, and `POST /api/profiles/from-current`,
  which saves the latest snapshot's allowlisted, non-managed settings as
  a user profile (`409` when no snapshot records config).
- `GET /api/profiles/<id>/diff` rows carry `setting`, `agent`, `label`,
  `description` and `where`; the response adds `dry_run_command` and a
  `prompt` (`fixes.profile_prompt`) that names each file, setting and
  value.
- **Cache misses Claude Code measured**: a `measured_miss_causes`
  table on the Cache tab lists the main session's misses by the cause
  Claude Code reported through the statusline, next to the
  transcript-inferred causes.
- `apply` and `apply --dry-run` explain each change (what the setting
  controls, now and after, where, the trade-off, how to undo it).
  Backup manifests record each key's old and new value and the SHA-256
  of what was written; `apply --revert` refuses, restoring nothing,
  when a file changed since, unless `--ignore-changes`.
- **Readable dashboard and reports** (readability stage 1). Every table
  and section can carry plain-English help (`Column.help`,
  `Table.help`/`value_labels`/`dashboard`, `Section.intro`/`help`, all
  defaulted in `model.py`), written by the new `helptext.py` after
  `recommend()` runs, so table names, column keys and row values are
  unchanged. The dashboard shows an intro, a "How to read this" block,
  a `?` per column and readable row labels (raw key on hover), and
  folds rarely needed tables into "Advanced detail". `report --explain`
  adds the same help to the Markdown report; the HTML report has it in
  collapsed blocks. The Agents, Subagent startup, Workstyle and
  Workflows sections are covered so far; `tests/test_help_coverage.py`
  keeps a shrinking list of the rest. House style:
  [`docs/writing-help.md`](docs/writing-help.md).
- **Subagent startup section** (`agent_startup`, `context_budget.py`):
  what each agent type is given before its first turn (task prompt,
  CLAUDE.md and memory, skills list, tool lists, hook output, other
  notes, and the system prompt and tool definitions when recorded, with
  the rest shown as "Not recorded"), what it was given but never used,
  and what most agent types receive alike. Forks are counted but kept
  out of the averages. `agent_startup_breakdown` also carries each
  agent's cache-write list price (`write_price`).
- **Per-part subagent recommendations** replace the single generic
  `spawn-cost` advice wherever the startup breakdown has data:
  `spawn-claude-md` (`omitClaudeMd`; never for Explore or Plan),
  `spawn-unused-skills` (add `Skill` to `disallowedTools`, marked
  unconfirmed), `spawn-unused-mcp` (`mcpServers`),
  `spawn-read-only-tools` (`tools`), `spawn-task-prompt` and
  `spawn-shared-claude-md`. Built-in agent types get a prompt to create
  a same-named override instead of a command; workflow subagents and
  forks get no override advice. `spawn-cost` remains for agent types
  without startup data.
- **Recommendations say what to change and how.** `Recommendation`
  gains `changes` (`SettingChange`: target, key, agent, value, current
  value, suggestion, notes), `estimated_saving`, `saving_basis`, `why`
  and `fixes`. The new `fixes.py` turns each change into a six-part
  explainer (what the setting controls, now and after, where and who it
  affects, expected effect, trade-off, how to undo it), an
  `apply --set ... --dry-run` command when the value is known, and a
  self-contained prompt for Claude. When a change needs work first
  (`omitClaudeMd`: move the CLAUDE.md rules the agent needs into its own
  agent file), the prompt asks Claude to do that before setting the key,
  and the command carries a `command_warning`. Shown on the dashboard cards, in the
  Markdown report and in the HTML report.
- **Amounts follow the billing mode** (`units.py`): dollars at list
  price for API billing; for Pro and Max plans, the share of the weekly
  usage limit (fitted by `elasticity.py` from your statusline readings)
  with the list-price equivalent next to it, or the list-price
  equivalent plus a hint when there are too few readings.
- **`apply --set KEY=VALUE [--agent NAME]`**: change one allowlisted
  setting or agent frontmatter field without a profile, with the same
  validation, backups, `--dry-run` and `--revert` as a profile apply.
  It never writes the active-profile marker. `mcpServers` joins the
  agent frontmatter allowlist.
- **`GET /api/diagnostics`**: the parse-quality counters as a labelled
  table, led by a check of the config snapshot hook.
- **Snapshot hook health** (`hook_health.py`): finds the SessionStart
  hook that runs `snapshot-config.py`, spots a Windows path broken by a
  single backslash in JSON (`\t` read as a tab), and reports how long
  ago the last snapshot was taken. `init` reports it and offers to fix
  the command; `init --repair-hook` fixes it without asking. Only that
  command string changes, after a `settings.json.bak-<timestamp>` backup.
- **`billing = "auto"`**, the new default: subscription once the usage
  log holds a usage-limit reading (only Pro and Max plans report them),
  API otherwise. `ReportMeta.billing_source` says why; the report header
  and the dashboard's Overview show it.
- **Host allowlist** for `serve` (DNS rebinding): every request whose
  `Host` header isn't a loopback name, the bind address or an
  `--allowed-host NAME` gets `403`.
- **`elasticity.py`** (v4-elasticity): fits how many percentage points
  of a `five_hour`/`seven_day`/`spend_limit` usage window one million
  tokens (or one list-price dollar) is actually worth, from consecutive
  `tools/log_usage.py` samples paired with the token volume this
  machine's own transcripts consumed between them (weighted
  least-squares through the origin, refusing to report a figure below
  8 pairs or an R² of 0.5). New `elasticity` report section
  (`elasticity_fit`, `elasticity_budget`, `elasticity_recent_burn`)
  derives the million-new-tokens-per-window budget and the last-24h
  burn share of the weekly window; new `express_in_window` function
  converts a USD saving into "≈ x% of your weekly window" for
  subscription-billed accounts; new `window-budget` recommendation
  rule (`elasticity.RULES`) states the derived budget/burn and points
  at the biggest other lever already on the report by id. See
  [`docs/elasticity.md`](docs/elasticity.md).
- **v4-carry-cost context carry cost per tool** (`carry.py`, work
  package v4-carry-cost): a tool result doesn't cost tokens only on the
  turn it's produced — it rides along in the cached prefix, re-read or
  re-written on every later turn until a compaction drops it. New
  `carry` report section (`carry_by_tool`, `carry_by_agent_type`,
  `carry_top_results`, `carry_truncation_savings`) prices that ongoing
  cost per tool and per agent type, and reports the exact saving from
  capping large results at 2,000/8,000 tokens. New `tool-output-carry`
  recommendation rule (`carry.RULES`) fires when a tool's carry cost
  exceeds a configurable share of the corpus's cache volume, naming a
  concrete truncation lever and citing the projected saving. See
  [`docs/carry.md`](docs/carry.md).
- **`model_swap.py`** (v4-model-swap): a model-swap counterfactual per
  agent type — reprices every already-observed priced turn at every
  model `pricing.toml` carries (same tokens, same observed 5m/1h
  cache-write split) and reports the ceiling saving from moving one
  tier down (fable -> opus -> sonnet -> haiku), plus a `model-tier`
  recommendation naming the exact `settings.json`/`<agent>.md` lever.
  Every saving is stated as a price ceiling at today's usage shape,
  never a prediction. See [`docs/model-swap.md`](docs/model-swap.md).
- **`compaction_sim.py`**: the `autoCompactWindow` sweep — replays every
  top-level transcript's priced turns under each of a fixed set of
  candidate auto-compaction windows (100k/150k/200k/250k/300k/400k/500k/
  none), estimating total cost under each against this corpus's own
  observed compression ratio and post-compaction rediscovery cost, with
  a fidelity self-check against the session's actual configured window
  and a `compaction-window` recommendation naming the cheapest one and
  its projected saving. See [`docs/compaction-sim.md`](docs/compaction-sim.md)
  and the `compaction_sim` entry in
  [`docs/sections-reference.md`](docs/sections-reference.md). Wired into
  `report.py`/`cli.py`/`recommend.py`/the service and UI in the v4
  wiring round below.
- **v4-wasted-turns spend tracking** (`waste.py`, work package
  v4-wasted-turns): prices every turn whose output the user never
  actually benefited from -- a failed tool call, a turn the user
  interrupted, one stopped by a tool denial, or every turn in a
  subagent transcript the harness killed before it could report back
  -- and attributes each to a cause with a lever, so the report can say
  not just what was spent but what's recoverable and how. New `waste`
  report section (`waste_summary`, `waste_by_cause`,
  `waste_by_agent_type`, `waste_top_sessions`), `compute_waste`/
  `WasteStats`/`build_section` entry points, `WasteThresholds`
  (`share_pct`, `min_sessions`, `min_turns`), and a new `wasted-turns`
  recommendation rule (`waste.RULES`) that fires when the wasted-cost
  share of total priced spend clears `WasteThresholds.share_pct`
  (default 10%), naming the dominant cause and its lever.
  `api-error-retry` (a turn preceded by a 529/retry gap)
  is counted alongside the other causes but never priced -- the harness
  already retried it automatically. Turns following a usage-cap pause
  (`Turn.gap_cause == "limit"`) are excluded outright, since
  `limits.py` already owns that attribution. Wired into
  `report.py`/`cli.py`/`recommend.py`/the service and UI in the v4
  wiring round below.
- **v4 wiring round**: `carry`, `compaction_sim`, `model_swap` and
  `waste` are now first-class report sections (`_SECTION_ORDER`:
  ...`limits`, `carry`, `compaction_sim`, `model_swap`, `waste`,
  `compactions`...), built by `report.build_report` from each module's
  own `from_config`/`build_section`, with `compaction_sim`'s
  `snapshot_windows` sourced the same way `context_budget.py` maps a
  session to its project's latest `autoCompactWindow` snapshot. Their
  four recommendation rules (`tool-output-carry`, `compaction-window`,
  `model-tier`, `wasted-turns`) are registered in `recommend.recommend()`
  alongside the existing baseline rules. New CLI subcommands `carry`,
  `compaction-sim`, `model-swap`, `waste` (`_REPORT_LIKE_SECTIONS`, same
  pattern as `limits`). New service routes `/api/carry`,
  `/api/compaction-sim`, `/api/model-swap`, `/api/waste` (see
  [`docs/api.md`](docs/api.md)). New UI "Savings" tab holding all four
  sections' tables (see [`docs/ui.md`](docs/ui.md)).
  `docs/sections-reference.md`'s section order now matches
  `report.py`'s assembly order exactly.
- **`savers.py`** (v4-saver-roi): third-party token-saver tool ROI —
  detects candidate "saver" MCP servers/plugins/skills via an explicit
  `config.toml` `[savers]` allowlist plus auto-detection (a
  case-insensitive name regex over MCP server names, config-snapshot
  `mcp_servers`/`enabled_plugins`, and `attribution_skill`), then reports
  each candidate's own overhead, its effect on cost/tokens/re-cache/
  compactions/turns in sessions where it was present versus absent
  (stratified by purpose/mode, gated on a 5-session-per-arm minimum),
  a search-substitution comparison against native `Grep`/`Glob`/`Read`/
  shell search calls (reusing `carry.compute_carry`'s per-turn pricing),
  and a net-saving-per-session verdict labelled "observed, not
  controlled". New `saver-tool-roi` recommendation rule (`savers.RULES`)
  recommends keeping or disabling a saver based on that net saving. See
  [`docs/savers.md`](docs/savers.md).

### Changed

- **README restructured**: a plain description, a three-step quick
  start, what each tab answers, acting on a recommendation, and a
  glossary, then the reference sections (renumbered). The token totals,
  how caching works, cache rebuild definitions and the TTL simulation
  assumptions moved to [`docs/concepts.md`](docs/concepts.md); links
  across the docs are updated.
- `render_patch_set` renders a recommendation's `changes` with current
  and proposed values instead of guessing from the lever text.
- The dashboard subtitle says what the tool is for; the Cache tab's
  statusline table is titled "Cache health per session, from your
  statusline". CLI wording changed; JSON and CSV keys are unchanged.
- **Recommendations in plain words.** Severity reads "Do this",
  "Worth considering" or "For your information"; cards say who a
  change is for and "What to do", fold multiple fixes, and put the
  evidence under "Show the numbers behind this", citing the table
  title and row label. Markdown and HTML reports use the same wording.
- **Totals for the window grouped and labelled with units**
  (Activity, Tokens, Cost, Context size) via display-only
  `Table.row_groups`/`row_kinds`; numbers in mixed metric tables get
  thousands separators.
- **Scorecard tiles explain themselves**: what each area measures,
  which way is better, and what the next rating needs, in words
  rather than threshold keys.
- CLI and dashboard wording: section, table and column titles are
  plainer; the Markdown and HTML reports show readable row labels. JSON
  and CSV output keep the raw keys and values.
- Dashboard: every report section is mapped to a tab (`sessions`,
  `context_budget`, `baseline_comparison`, `phases`, `agent_startup`,
  `scorecard`); the Config tab renders its tables once; the Cache tab
  explains the limit-expiry cause; tabs are renamed "Cache lifetime
  (TTL)" and "Data quality"; each tab has one heading and an intro.
- **Recommendations are written in plain words** by a new pass,
  `advice.py`, run at the end of `recommend()`: each card has a plain
  title, a `why` sentence, an action, and (where a setting is involved)
  `changes` with the current value, so it comes with an explainer, a
  command and a prompt. Cards that disagreed are consolidated:
  `compaction-window` replaces `compaction-churn` and takes the setting
  from `long-context-share`, and is dropped when `autoCompactWindow` is
  already at or below its floor; the per-agent `model-tier` cards merge
  into one with a change per agent type (as the `haiku`/`sonnet`/`opus`
  alias), skipping workflow subagents and forks. `spawn-cost` no longer
  fires for agents no file can change. Cards are ordered by severity,
  then by estimated saving (`Recommendation.saving_usd`). A setting
  locked by managed policy gets a prompt that drafts a request to your
  administrator instead of a command. The rules' ids, evidence and JSON
  keys are unchanged.
- `data-quality` fires on cache-write mismatches only when they are a
  real share of replies (the same bar as unreadable lines), not on a
  single odd reply.
- **`compaction_sim.py`'s `compaction-window` rule, conservatively
  rewritten**: the window sweep only ever charged a flat rediscovery
  allowance, so a smaller window always looked cheaper in isolation —
  against a real corpus this produced an incredible-looking "100k
  window saves 68%" recommendation. The rule now recommends a *floor*
  ("set autoCompactWindow to at least W"), not a single "best" point: the
  smallest candidate window whose simulated compactions-per-session stay
  at or below 2 and whose saving still clears threshold after subtracting
  an extra, more conservative rediscovery estimate derived from the
  corpus's own measured `topology_redundant_reads` (or, when that figure
  isn't available, simply doubling the flat allowance, and saying so in
  the note). The action text always says "modelled, not observed" and
  cites `compaction_sim_fidelity` when it has rows. This is the one
  change made to the module's own arithmetic during the v4 wiring round
  (every other module's arithmetic was left untouched).
- **`report.build_report`** gained an optional `config_dir` keyword,
  used only to tell `waste.WasteStats` where to read/write its salted
  session-id-hashing salt file. A caller that omits it (every pre-v4
  test, `baseline.py`, `team.py`) now falls back to an OS-temp-directory
  default rather than `waste.py`'s own default of the real
  `~/.claude/token-lens` — `build_report` must never touch a real user
  config directory unless a caller explicitly hands it one.
  `service/api.py` passes its own real `config_dir` so the running
  service's salt lives alongside its other state as intended.
- **`PARSER_VERSION` 5 -> 6** (`__init__.py`, v4-wasted-turns): two new
  additive `Turn` fields, `tool_error_count`/`tool_error_chars`,
  derived from each turn's own tool_result blocks that carry
  `is_error: true` (length only, never the error text itself -- see
  `model.py`'s module docstring). No pre-batch digest cache entry ever
  computed these, so any cache built under `PARSER_VERSION` 5 or
  earlier is invalidated and transcripts are reparsed on next use.

### Fixed

- `reconcile` no longer counts cache-write tokens twice when an Admin
  export has both the total column and the 5-minute/1-hour split, says
  correctly that local usage is grouped by UTC day, and groups an Admin
  date column holding full timestamps by UTC day too.
- Every command finds Claude Code's `settings.json` the same way:
  `--claude-root`, else `CLAUDE_CONFIG_DIR`, else `~/.claude`. `init`,
  `--repair-hook`, `changes`, `uninstall` and the dashboard used the
  folder above `--config-dir` and could change the wrong file. With a
  non-default `--config-dir`, the hook and statusline commands now carry
  it, and `--repair-hook` keeps those arguments.
- Running `init` again no longer restarts the capture window.
- On Windows, `uninstall-service` stops the running dashboard before
  removing the scheduled task, and every platform says which steps it did.
- The token-saver analysis stays out of the report; its docs now say why.
- The SessionStart hook and statusline commands name this Python and
  the script by full path. `py -3` fails where the launcher isn't on the
  `PATH`, and Git Bash doesn't expand `%USERPROFILE%`; either stopped the
  hook without any visible error. The hook health check now reports
  both, and `init --repair-hook` fixes them: it writes out a `%VAR%`
  and keeps your own interpreter when it's found, and otherwise names
  the base Python rather than a virtual environment's, since the hook
  needs only the standard library.
- The Data quality tab says when the statusline isn't logging (it runs
  only in Claude Code in a terminal).
- "All time" on the dashboard now means all time on every tab; it used
  to fall back to 30 days on report-backed tabs.
- A service from an older build no longer re-reads transcripts that a
  newer build already read. Two services sharing one store used to undo
  each other's work on every pass.
- The skills and CLAUDE.md checks say "not enough data" when no session
  in the window recorded those files, instead of "nothing to do".
- **An apply stamp was read as the latest config snapshot.** `apply`
  writes a small `{ts, schema_version, profile_id}` record into the
  snapshot directory; every reader took it for a snapshot with no
  settings, so the scorecard counted every setting as changed, profile
  diffs showed empty "Now" values, "save my current settings" saved
  nothing, and hook health dated the last snapshot from the apply. New
  `snapshots.records_config` skips records that hold no config.
- **The dashboard never saw the statusline usage log**: it now passes
  the log into its report, scoped to the window's sessions like the CLI
  (`statusline.scoped_usage_log_rows`).
- **Stored snapshots lost their config.** The watcher stored snapshots
  flattened, so the service's rebuilt snapshots had no effective
  config, managed keys or agents. It now stores the hook's own
  (redacted) document.
- **Dashboard cost no longer dips while the service rescans.** The
  watcher's placeholder session row (written before a session's
  subagents are parsed) reset the session's stored cost and tokens to
  zero until the fold finished, so a large session could briefly vanish
  from the totals. It is now insert-only (`Store.ensure_session`).
- **Context budget never found a project's settings snapshot.** The
  config hook stores `project_slug` as a hash (`slug:<12 hex>`), but
  `context_budget.py` looked snapshots up by the readable slug, so
  CLAUDE.md, agent-list and MCP estimates, the auto-compact setting and
  its drift check were always blank. New `snapshots.snapshot_project_key`
  computes the hook's key.
- **Sessions were joined to other projects' snapshots.**
  `snapshots.snapshot_for` takes an optional `project_key` and ignores
  snapshots from other projects (schema-1 snapshots with no project
  still match). Config diff, config drift, `compare` and `savers` pass it.
- **Subagents with no recorded type were counted as the main session**
  in carry, waste, cache rebuilds and limits; phases filed the main
  session under "unknown". All now use `model.agent_type_label`:
  `top-level` for the main session, `unknown` for an untyped subagent.
- `model_swap`: the unpriced-turns note now says those turns are still
  priced at the alternatives (so the saving is understated), and the
  lever for a built-in agent says to create an overriding agent file;
  workflow, fork and untyped subagents have none.
- Baseline `cost_per_session` no longer counts orphaned subagent bundles
  as sessions.
- Mixed "metric / value" tables (overview totals, compactions summary)
  show numbers with separators and at most 2 decimals instead of raw
  floats.
- TTL near-miss column labels follow `near_miss_window_s`, and the
  just-missed token count uses the same basis as its cost (the context
  rewritten).
- The scorecard's usage-limit share of cache rebuilds counted every
  write after a limit pause; it now counts only limit-expiry rebuilds.
- The auto-compact simulation (`compaction_sim`) shrank everything a
  session added after a simulated summary by the compression ratio, so
  context grew far too slowly afterwards and small windows looked much
  cheaper than they are. It now removes only the tokens the summary
  dropped. On the worked example the 100,000 saving falls from 69% to
  35%, and the recommended floor moves to 150,000.
- Attachment sizes are measured from the fields real transcripts carry
  (`skill_listing.content`, `instructions.files[]`,
  `deferred_tools_delta.addedLines` and others). They were always 0,
  because the code read a `rendered` field that real transcripts never
  have.
- The dashboard's recommendation cards no longer show a stale
  `apply <lever> --dry-run` hint.
- **The watcher never re-parsed a transcript whose file hadn't changed
  but whose stored `parser_version` had fallen behind** (`watcher.py`
  `FileWatcher._resolve`/`_needs_parse_this_tick`, `store.py`
  `Store.known_files`) — the re-parse decision compared only
  `(mtime_ns, size_bytes)` against `Store.known_files()`, so a
  `PARSER_VERSION` bump (e.g. 5 -> 6, above) only reached a transcript
  the next time its file actually changed; an untouched file kept
  serving fields computed under the old parser indefinitely.
  `known_files()` now also returns each transcript's stored
  `parser_version`, and the watcher treats a mismatch against the
  running `PARSER_VERSION` as needing a re-parse even when the file
  itself is unchanged, reusing the on-disk digest cache (already keyed
  on `parser_version`) so the rebuild costs one cold-ish tick per
  transcript, then nothing. A new `files_reparsed_stale_parser`
  `WatcherStats` counter (surfaced in `/api/health`'s `watcher` block)
  lets an operator see the rebuild actually happen.

## [0.3.0] - 2026-09-19

### Added

- **`docs/first-run.md`**: the numbered, Windows-first walkthrough for a
  first-time user on a locked-down work machine (no admin rights,
  possibly no `git`, possibly no `pip` network access) — install (all
  three routes: `.pyz`, `pip` from a local clone, `pip` from GitHub),
  `init`, the logon-service step and how to confirm it actually
  registered, opening the dashboard, the first `report`, a privacy
  self-check, and a complete uninstall, plus a troubleshooting table
  and a POSIX quick variant. Every command in it was rehearsed
  end-to-end against a synthetic project during this work.
- **Release CI (`.github/workflows/release.yml`)**: on every `v*` tag
  push, builds `dist/claude-token-lens.pyz` with `scripts/build-pyz.py`,
  smoke-tests it with `--version`, and attaches it to the GitHub
  Release via `softprops/action-gh-release`.
- **Cross-platform service installer (`install-service`/
  `uninstall-service`, and `init`'s new logon-service step)**
  (`src/claude_token_lens/installer.py`, work package v3): registers
  `claude-token-lens serve` to run continuously from logon/boot, so
  history isn't lost the first time Claude Code's own
  `cleanupPeriodDays` cleans up a transcript that no watcher was
  running to see. Windows registers a `-RunLevel Limited` Scheduled
  Task (no admin rights) via inline PowerShell cmdlets; Linux writes a
  hardened `~/.config/systemd/user/claude-token-lens.service` unit and
  runs `systemctl --user enable --now`; macOS writes a LaunchAgent
  plist and runs `launchctl bootstrap`. Building a plan
  (`plan_service_install`) never has a side effect, so `--dry-run`
  (on both the new subcommands and `init` itself) always prints
  exactly what would be written/run without touching the machine, and
  every real write/run goes through an injectable `runner` so the test
  suite never shells out to `schtasks`/`systemctl`/`launchctl`/
  `powershell.exe` for real. Running from a `.pyz` build registers an
  action that re-invokes that same archive. `claude-token-lens init`
  now finishes with a "Run the service at logon?" question (default
  yes; `--install-service`/`--no-service` to answer up front; derives
  to *not* installing under `--non-interactive` unless
  `--install-service` is also given), after which it probes
  `is_registered()` and `GET /api/health` once and prints the dashboard
  URL. `GET /api/health` gains a `service_registered: true|false|null`
  field (`null` when the probe can't run at all), cached for ten
  minutes per running `serve` process; the dashboard's Overview tab
  shows a banner when it comes back `false`. See
  [`docs/deploy.md`](docs/deploy.md).
- **Service: baseline and profile ingestion, and the real `/api/profiles*`
  routes** (`service/watcher.py`, `service/store.py`, `service/api.py`,
  work package V3-service): the watcher now ingests every
  `<config_dir>/baselines/*.json` baseline record and every
  `<config_dir>/profiles/*.toml` user profile into new `baselines`/
  `profiles` store tables on each tick, content-hash deduped so a
  repeat tick over an unchanged file is a no-op (`SCHEMA_VERSION` 4 to
  5, for the new `content_hash`/`record_id` columns). `GET
  /api/baseline` now returns the latest stored baseline, its full
  history, and a `capture_status` block (with a one-line human-readable
  `summary`) so the UI can mark recommendations provisional while a
  capture window is open. `GET /api/profiles` now returns the seven
  shipped catalogue profiles plus every stored user profile, each
  tagged `source: "catalogue"|"user"`, and the latest baseline's
  `suggested_profile_id` if any. `GET /api/profiles/<id>/diff` is a
  real route: it diffs the requested profile against the latest config
  snapshot's effective config (or an empty one, with a note, if no
  snapshot has been recorded yet) via `profiles/diff.py`, returning the
  settings/agent/env overlay rows, the unified diff text, and the
  `apply_command`/`launch_command` to run on the host — it never
  accepts a client-supplied project directory, so no filesystem path
  can round-trip through the API. `POST /api/profiles` is a real route:
  it validates the request body against `profiles/schema.py`, rejects
  an unknown key with `400` and the schema's own error text, refuses to
  overwrite a catalogue id (`409`), refuses to overwrite an existing
  user profile unless `?replace=1` is given (`409`), writes the new
  profile TOML file atomically, and re-ingests it immediately so the
  `201` response is consistent with a following `GET /api/profiles`.
- **Service UI: Profiles tab, and a baseline panel on Config** (`service/
  static/app.js`, `app.css`, work package V3-service): a new Profiles
  tab lists the catalogue and user profiles (marking the one suggested
  by the latest baseline), renders a selected profile's diff as
  settings/agent/environment tables plus the full unified diff text,
  and shows the apply/launch commands in a code block with a copy
  button. A minimal "save as a new user profile" form (id, name, a JSON
  settings-overlay textarea) posts to `POST /api/profiles` and surfaces
  the server's `400`/`409` validation message inline. The Config tab
  gained a "Latest baseline" panel (the stored baseline, its history,
  and the capture-window status line); the Recommendations tab shows
  the same "capture window open: provisional" notice while a capture
  window is in progress.
- **v0.3 team aggregate: `export --aggregate`, `import`, `team-report`**
  (`team.py`, v0.3 Task 1): `claude-token-lens export --aggregate
  [--include-projects]` writes one machine's own team document — tool
  version, generated-at, a stable-but-non-reversible `machine_id`
  (salted HMAC-SHA256 over the hostname, same construction/domain-tag
  separation convention as `exports._hash_slug`), the report window,
  and per-group aggregates only (sessions, priced turns, tokens by
  kind, cost, re-cache share, compaction rate, TTL mix, mean spawn
  write, mean report size) across five axes — archetype, mode,
  purpose, agent type, model — plus the corpus-wide scorecard levels.
  No session id ever; a project slug appears only as its hash, and
  only with `--include-projects`. `claude-token-lens import FILE...`
  schema-checks each document (`team.validate_team_document`: an
  explicit key allowlist, no string over 64 characters) before copying
  it into `<config_dir>/team/<machine_id>-<generated_at>.json`,
  exiting 2 with the reason on the first invalid file and writing
  nothing for the rest of the batch. `claude-token-lens team-report
  [--json|--html|--csv-dir]` keeps the latest document per machine and
  renders per-archetype and per-agent-type comparison tables across
  machines (a machine's short hashed id as the column key, never a
  hostname), gated by a minimum-sample rule (5 sessions per cell;
  below that a cell reads `n<5`), with an "observed, not controlled"
  note. See [docs/team.md](docs/team.md) and the README's "For team
  leads" section.
- **v0.3 baseline comparison in the report** (`report.py`/`baseline.py`,
  v0.3 Task 2): `report --baseline <id|latest>` (and every report-like
  subcommand — `sessions`/`recache`/`ttl`/`compactions` — that builds
  the same `ReportModel`) adds a `## Baseline comparison` section:
  cost per session, re-cache share, compactions per session, session
  baseline size, TTL mix (top-level and per agent type), mean spawn
  write per agent type, and scorecard level per dimension, each shown
  as baseline value / current value / delta / delta %, plus a
  per-mode breakdown table (cost/re-cache/compactions only) when the
  baseline recorded a mode mix, gated by the same 5-session minimum
  the rest of the codebase uses. Unresolvable (`--baseline
  does-not-exist`) or absent (`--baseline latest` with nothing saved)
  baselines omit the section and add a note to `## Assumptions`
  instead of failing the run. `baseline.build_baseline`'s own record
  gained the matching fields (`cost_per_session`,
  `recache_share_pct`, `compactions_per_session`, `ttl_mix_top_level`,
  `ttl_mix_by_agent_type`, `session_baseline_size`,
  `mean_spawn_write_by_agent_type`, `scorecard_dimensions`,
  `by_mode`), extracted from an already-built report's own tables via
  new shared functions in `report.py` (`overview_metric`,
  `recache_share_pct_metric`, `compactions_per_session_metric`,
  `ttl_mix_by_agent_type_metric`, `session_baseline_size_metric`,
  `mean_spawn_write_by_agent_type_metric`,
  `scorecard_dimensions_metric`) — never independently recomputed. See
  [docs/onboarding.md](docs/onboarding.md)'s baseline-record table.

### Fixed

- **`statusline.print_install_fragment()` emitted a `-m` command that
  cannot work from a `.pyz` build** (`statusline.py`) — the printed
  `statusLine` fragment (both from `claude-token-lens init` and
  `statusline --print-install-fragment`) always read `py -3 -m
  claude_token_lens.statusline`/`python3 -m claude_token_lens.statusline`,
  regardless of how the tool was installed. Run from inside a `.pyz`
  archive, `-m claude_token_lens.statusline` fails outright (`No module
  named claude_token_lens.statusline`) because the package lives inside
  the zip, not on `sys.path` — a pyz-only user who followed `init`'s own
  printed instructions ended up with a statusline that never worked.
  `print_install_fragment` now mirrors `installer.plan_service_install`'s
  existing pyz-awareness: it detects the running `.pyz` the same way
  (`installer.detect_pyz_path`, also newly hardened to always return an
  **absolute** path even when `sys.argv[0]` itself was relative — the
  same absolute-path requirement `_serve_argv`'s Scheduled-Task/systemd/
  launchd action already depended on) and, when running from one,
  emits `"<python>" "<abs path to .pyz>" statusline` instead. An ordinary
  installed package/checkout is unaffected — the fragment keeps the
  original `-m` form. New unit tests cover both modes (explicit
  `pyz_path=`, auto-detected via `sys.argv[0]`, and the no-pyz default).
- **`apply` user scope resolved the wrong Claude Code directory**
  (`profiles/apply.py`, `cli.py`) — user scope derived its target as
  `config_dir.parent`, so with `config_dir` defaulting to
  `<claude-root>/token-lens` this happened to work out, but the two are
  independent by design (`--config-dir` can point anywhere), and
  deriving one from the other meant a user-scope apply actually wrote
  `<config_dir_parent>/.claude/settings.json` (effectively
  `~/.claude/.claude/settings.json`) and could never find a user-scope
  agent file to patch at all. `plan_apply` now takes an explicit
  `claude_root` parameter, resolved by the new `cli._resolve_claude_root`
  (a new `--claude-root PATH` flag, else `$CLAUDE_CONFIG_DIR`, else
  `~/.claude`) — never derived from `--config-dir`. See
  [`docs/profiles.md`](docs/profiles.md).
- **`apply --dry-run` diffed against a stale snapshot instead of the
  real target files** (`profiles/apply.py`) — the preview text was
  built from the caller's *snapshot* of the effective config
  (`diff.diff_against_effective`), which can already be stale by apply
  time (an agent file hand-edited since the snapshot was taken, for
  example), so the diff could show a change against a value the file
  no longer has. `plan_apply` now renders the dry-run diff
  (`render_plan_diff`) from the exact same `actions` — real
  before/after file bytes — that a real apply writes from, so the
  preview and the real write are provably one computation.
- **`apply --dry-run` exited `0` even when the real apply would
  refuse** (`cli.py`) — a git-tracked target or a missing agent file
  now prints each `plan.blocked` reason to stderr and exits `2` from
  `--dry-run` too, instead of only surfacing the refusal once the user
  ran the apply for real.
- **`init` ignored `--all-projects`/`--project`/`--project-family` for
  its initial baseline capture** (`onboarding.py`) — the baseline was
  always hard-wired to the current directory's own project slug.
  `run_init` now honours all three selectors for the baseline the same
  way `report`'s own project selection does, falling back to the
  current project only when none of the three are given.
- **Git-tracked-file refusal only ever checked project scope**
  (`profiles/apply.py`) — a `~/.claude` kept under version control in a
  personal dotfiles repository could be silently overwritten by a
  user-scope apply, since `_is_git_tracked` was only consulted for
  `project-local`/`repo` targets. The check now applies at every scope;
  `--allow-tracked` still opts in.
- **Frontmatter parser refused any YAML block scalar** (`profiles/
  frontmatter.py`) — a `description: |` or `>` block (with its
  indented continuation lines) raised `FrontmatterError` outright
  instead of parsing. Block scalars are now recognised and kept as
  opaque, byte-preserved blocks: every line is left untouched, and only
  a top-level scalar key or the `experimental:` mapping can still be
  patched (attempting to patch the block-scalar key itself still
  raises, rather than guessing how to collapse it).
- **README's `report` row documented a removed `--allow-titles`
  flag** — the flag itself was removed as part of an earlier fix
  (R17); the CLI reference table's `report` row still listed it as a
  no-op option. Removed.
- **Settings JSON rewrite always reformatted to 2-space indent and
  `\n` line endings** (`profiles/apply.py`) — a merged `settings.json`
  is now written back with the existing file's own indent width (2 vs
  4 spaces) and line ending (`\r\n` vs `\n`) detected and preserved,
  matching how agent-frontmatter patching already only ever touches the
  lines it changes.
- **`tests/test_onboarding.py` imported `assert_privacy_deep` but never
  called it** — the privacy assertion its import implied was never
  actually exercised against `init`'s own output. Now called against
  the written `config.toml`, the written `projects/<slug>.toml`, and
  `init`'s stdout; fixing this surfaced a real leak in the latter
  (`onboarding.py`'s "Wrote ..." confirmation lines printed the full
  absolute path), now printed relative to `config_dir` instead.
- **`/api/summary` windowing bug**: for a given `window_days`, this
  route counted sessions and transcripts by a session row's own stored
  timestamp instead of by the top-level transcript file's mtime — the
  same `window_by="mtime"` rule `discovery.find_sessions`/
  `corpus.load_corpus` already use, and that the CLI `report` overview
  has always honoured. The two could disagree by hundreds of
  transcripts on a real corpus (window_days=7 returned sessions=8/
  transcripts=1867 against the report's sessions=11/top-level 11/
  subagent 270 on the same corpus). `Store.summary()` now windows the
  same way the report does; a synthetic-corpus regression test proves
  the two stay in parity.
- **`profiles/diff.py`: `apply_command` printed the wrong CLI flag** —
  it hardcoded `--project`, but the real `apply` subcommand flag for a
  project directory is `--project-dir` (`--project`, singular, is
  already taken by every subcommand's own repeatable project-slug
  filter). Fixed in `apply_command` and its docstring, and in
  `docs/profiles.md`'s own description of the two-line invocation it
  returns, which had the same stale flag.
- **v3-limits usage-limit tracking** (`limits.py`, work package v3-limits):
  a 5-hour/weekly usage-cap pause, a harness-forced early subagent
  termination, and the desktop app's resume ping are now first-class,
  attributable facts instead of behavioural noise. New `limits` report
  section (`limits_summary`, `limits_hits_by_kind`,
  `limits_agent_terminated`, `limits_pauses`,
  `limits_reset_hour_histogram`, `limits_by_agent_type`,
  `limits_csv_cross_check`) plus `limit_pause_intervals`/`limit_markers`
  for other consumers. Attribution threaded through:
  `classify.py` (`SessionFeatures.limit_pause_s`; pause time discounted
  out of gap/span statistics and the overnight-mode check),
  `recommend.py` (new `limit-pressure` rule), `scorecard.py`
  (`ScorecardInputs.limit_recache_share_pct`/`limit_pause_sessions`
  exclude pause-forced re-cache from the `cache_efficiency` level and
  note affected sessions under `data_quality`), and `statusline.py` (a
  `5h`/`7d` segment gets a `!` warning marker at >=90% used, and a
  `usage-log.csv` row for an exhausted `five_hour`/`seven_day` window is
  tagged `source=limit_hit`). See [`docs/limits.md`](docs/limits.md).
- **v3-limits wired into `report.py`, the `limits` CLI subcommand, and
  the service/UI**: `report.build_report` now folds every transcript
  into a `limits.LimitStats` accumulator and appends the `limits`
  section (gated by `include`, like every other section), with
  `limits.csv_cross_check` bolted on as an extra table whenever
  `usage_log_rows` is supplied; `limits.ASSUMPTIONS` joins the
  report's assumptions list, and the scorecard's `cache_efficiency`/
  `data_quality` dimensions now actually receive
  `limit_recache_share_pct`/`limit_pause_sessions` (previously computed
  fields on `ScorecardInputs` that nothing ever populated).
  `claude-token-lens limits` is a new focused-view subcommand (`overview`
  + `limits`), alongside `recache`/`ttl`/`compactions`. `GET
  /api/session/<id>` gains `limit_markers` (`Store.turns_for_session`,
  reshaping `limits.limit_markers`'s triples into `{"ts", "kind",
  "detail"}` objects); the service UI's session timeline draws them as
  their own marker kind, positioned by timestamp interpolation between
  the session's `first_ts`/`last_ts` rather than by turn index (a
  usage-limit event's `ts` falls inside the gap between two turns, with
  no `turn_series` point of its own), and the `limits` report section
  itself renders on the Cache tab alongside `recache`/`ttl`. See
  [`docs/limits.md`](docs/limits.md), [`docs/sections-reference.md`](docs/sections-reference.md),
  [`docs/api.md`](docs/api.md) and [`docs/ui.md`](docs/ui.md).
- **`cli.py`: removed the dead `apply --dry-run` `--project` ->
  `--project-dir` substitution workaround** — it patched the suggested
  invocation text for a bug in `profiles/diff.py`'s `apply_command`
  that was already fixed (the "printed the wrong CLI flag" entry
  above), so the `.replace(...)` call had matched nothing for a while;
  `apply_command`'s own output now prints through unchanged. README's
  CLI reference table also had three stale "Planned for v0.2/v0.3" stub
  rows for `init`/`baseline`/`serve` left over from before those
  subcommands were implemented, contradicting the real rows already
  above them; removed, and `serve`'s row now documents its real flags
  (`--port`, `--bind`, `--allow-remote`, `--poll-interval`,
  `--retention-days`, `--exclude-project`, `--billing-mode`,
  `--monthly-report`, `--once`, `--purge --yes`) instead of the old
  "prints which milestone it's planned for and exits 2" stub text.
- **`limits.py`: `limits_reset_hour_histogram` used a bare `int` local
  hour (0-23) as its row key** — every other table's first column is a
  non-empty `str` label (the cross-module row-key contract in
  `tests/test_recommend_contract.py`), a mismatch this table was never
  caught on until `limits` was actually wired into `report.py` and
  exercised by that contract test for the first time. Row key is now a
  zero-padded `"00"`-`"23"` string; `Column.kind` updated from `"int"`
  to `"str"` to match.
- **`scripts/windows/Register-TokenLensTask.ps1`: registering the
  Scheduled Task failed with "Access is denied" for a non-admin
  user** — `New-ScheduledTaskTrigger -AtLogOn` with no `-User` creates
  an *any-user* logon trigger, which Task Scheduler treats as
  machine-wide and refuses to register without admin rights, even
  though the task's own `-Principal` was already scoped to the current
  account with `-RunLevel Limited`. The trigger now also carries
  `-User "$env:USERDOMAIN\$env:USERNAME"`, scoping it to this one
  account's logons; the `schtasks /create` fallback mirrors this with
  `/RU "$env:USERDOMAIN\$env:USERNAME" /IT`. Also added
  `-ExecutionTimeLimit ([TimeSpan]::Zero)` to the task settings, since
  `serve` is meant to run indefinitely and Task Scheduler's own default
  72-hour limit would otherwise kill it after three days. See
  [`docs/deploy.md`](docs/deploy.md).
- Overview tab: summary cards failed to render because the render
  callback's parameter order was reversed.
- Test suite: three tests only passed on Windows by coincidence and
  failed on Linux CI — a redacted-slug assertion that depended on the
  shape of the platform's own temp directory, a resolve-month timezone
  test with an arithmetically wrong expected value (masked on Windows by
  a missing-tzdata fallback), and a `~/.claude.json` path-matching test
  that assumed case-insensitive filesystems everywhere. No production
  behaviour changed.
- **`import`: path traversal via an untrusted document's `machine_id`**
  (review finding B1, `team.py`): a team document's `machine_id` was
  written verbatim into `<config_dir>/team/<machine_id>-<generated_at>.json`
  with no shape check, so a crafted `machine_id` (e.g. containing `../`)
  could write outside the team directory. `machine_id`/`generated_at`
  are now validated against the exact shapes this tool's own exporter
  produces (12 lowercase hex characters; an ISO-8601 UTC timestamp)
  both in `validate_team_document` (rejects with exit 2 before any file
  I/O) and again, defence-in-depth, in `save_team_document` itself,
  which also asserts the resolved output path stays inside
  `<config_dir>/team` before writing. Review finding N4: every other
  required string field (`window`, `tool_version`) is now type-checked
  as a string too, not just present.
- **`store.migrate()` dropped every table on any `SCHEMA_VERSION`
  mismatch** (review finding B2, `service/store.py`): an older store
  (e.g. one built by v0.2.0) hit the same code path as a newer,
  unreadable one, silently losing every row on the next `serve` run
  instead of being upgraded in place. `migrate()` now walks an additive
  migration ladder (currently one step, 4→5: `ALTER TABLE ADD COLUMN`
  for `profiles.content_hash`/`baselines.record_id`/
  `baselines.content_hash`, plus the `CREATE UNIQUE INDEX` SQLite
  requires in place of an `ALTER`-added `UNIQUE`, all in one
  transaction that stamps the new version last) and only falls back to
  the old drop-and-rebuild behaviour for a genuinely newer-than-code
  store or a version with no ladder step — and even then takes a
  `service.db.bak-<version>` backup first.
- **`recache_summary.avoidable_cost_usd` double-counted limit-pause
  cost** (review findings B5/N2, `recache.py`/`limits.py`): a
  `limit-expiry` re-cache (forced by a usage-limit pause, not by
  anything the agent could have avoided) was summed into
  `avoidable_cost_usd` alongside genuinely avoidable re-cache, and the
  same cost was *also* reported by the `limits` section — so the two
  sections' cost figures overlapped without saying so. `limit-expiry`
  turns are now excluded from `avoidable_cost_usd`, and a new
  `unavoidable_limit_expiry_cost_usd` row reports them separately; both
  `recache.py` and `limits.py` now carry a note cross-referencing the
  other section's own cost-of-a-limit-pause figure, and
  [`docs/limits.md`](docs/limits.md) documents precisely how the two
  numbers relate.
- **`import`: `FileNotFoundError` when the team directory doesn't
  exist yet** (review finding S2, `cli.py`): the first `import` run on
  a fresh `--config-dir` crashed instead of creating
  `<config_dir>/team/`. `_cmd_import` now wraps the save call and
  turns an `OSError`/`ValueError` into a single-line stderr message and
  exit 2, on top of `save_team_document`'s existing `mkdir(parents=True)`.
- **Service API: mutating routes accepted cross-origin POSTs**
  (review finding S3, `service/api.py`): `POST /api/profiles` and
  `POST /api/sessions/<id>/tags` had no origin check of any kind, so a
  malicious page open in the same browser could POST to the local
  service. `do_POST` now rejects a request whose `Origin` header is
  present and doesn't match the server's own origin, or whose
  `Sec-Fetch-Site` header is present and isn't `same-origin`/`none`,
  with `403 {"error": {"code": "forbidden"}}`, and separately requires
  `Content-Type: application/json` (`400` otherwise). See
  [`docs/api.md`](docs/api.md)'s new "Cross-site protection" section.
- **`compare`: overview headline metrics were arm totals, not
  per-session means** (review finding S4, `compare.py`): `cost`,
  `new_tokens` and `priced_turns` were summed across every session in
  an arm, so an arm with more sessions than the other always showed a
  large headline delta driven by arm size rather than by any real
  difference between the two arms' work. `compare_overview` now leads
  with per-session means (`cost_per_session`, per-session new tokens,
  per-session priced turns); the raw totals are kept as separate rows
  labelled "... (informational)". `compare_by_stratum`'s `cost_a`/
  `cost_b` (and its new-tokens columns) are normalised the same way.
  See [`docs/compare.md`](docs/compare.md)'s new "Overview metrics"
  section.
- **Team documents' `by_agent_type` axis carried raw custom agent
  names** (review finding S10, `team.py`): a project- or user-defined
  custom subagent's name (as opposed to one of Claude Code's own
  bundled agent types) is frequently product- or project-named, which
  is exactly the kind of detail this module otherwise never exports.
  Built-in agent types (and the synthetic `top-level`/`unknown`
  labels) are kept verbatim; any other agent type is now hashed to
  `custom:<8 hex chars>` with the same salted-HMAC construction as
  `machine_id`/project slugs before a team document is ever written.
  See [`docs/team.md`](docs/team.md)'s privacy-guarantees list.
- **Version stayed `0.2.0` throughout the v0.3 release** (review
  finding S5, `pyproject.toml`, `src/claude_token_lens/__init__.py`,
  `service/api.py`): it reached user-visible output — the report's
  "Tool version" line and every team document's `tool_version` field
  (`exports.py`/`team.py`, both already derived from `__version__` and
  so needed no code change) — and the HTTP `Server` response header,
  which was a hardcoded literal. Bumped to `0.3.0`; the `Server` header
  is now built from `__version__` (`claude-token-lens/{major}.{minor}`)
  so it can't go stale on a future release again.

### Planned

A v0.4 backlog, kept here until scheduled into a milestone:

- **Budget check.** `check --weekly-tokens N --daily-usd N` exits non-zero
  when exceeded; UI banner; uses `log-usage` window data when present.
  Guardrail for overnight runs.
- **Anomaly outliers.** Sessions or spawns whose cost is more than 3 median
  absolute deviations from their mode/purpose group, with the composition
  table attached. Catches runaway agents.
- **Scheduled reports.** `serve --monthly-report DIR` exists and is
  threaded onto `ServeOptions.monthly_report_dir`, but nothing consumes
  it yet — no watcher tick actually renders a report on that schedule.
  Wire a month-boundary check into the watcher's poll loop that calls
  `monthly.write_monthly_report` when the directory is set. Habit-forming
  review.
- **Opt-in local path view.** `--show-paths` (local only, never in exports)
  lists the top files by Read tokens, as token-dashboard does.

## [0.2.0] - 2026-09-19

### Added

- **v0.3 `init`/`baseline` onboarding pair** (`onboarding.py`,
  `baseline.py`, work package V3-init): `claude-token-lens init
  [--answers FILE] [--non-interactive] [--no-install]` detects what's
  already on the machine (config-dir/snapshot/usage-log/project-count
  facts), asks — or, non-interactively, derives and reports — a short
  question set (billing mode, excluded projects, settings-overlay
  usage, shared-project-config, timezone, default apply scope, and the
  onboarding capture-window length), writes `config.toml` and this
  project's own `projects/<slug>.toml`, prints the SessionStart
  hook/statusline install fragments (unless `--no-install`), and runs
  an initial baseline capture. `claude-token-lens baseline [--days N]
  [--finalise] [--list] [--show ID]` extracts a JSON baseline record —
  mode mix, dominant purposes, workstyle archetype, scorecard, a
  projected caching saving, a suggested profile, and an optional
  billing-mismatch warning — entirely from an already-built report's
  own tables (never recomputed independently), stored as plain JSON
  files under `<config_dir>/baselines/` (no SQLite), plus a
  four-section Markdown report. `_suggested_profile` applies a
  majority-overnight override that `profiles.catalogue.suggest()`
  can never reach on its own, and `_billing_mismatch_warning` flags a
  subscription-billing config showing an observed 1h TTL on a
  non-top-level agent type. See
  [docs/onboarding.md](docs/onboarding.md) for the full contract,
  including a noted scope gap against `docs/config-layers.md`'s
  richer "what `init` will ask" preview.
- **`config.py`: per-project TOML config and a generic config writer**
  (work package V3-init): a new `ProjectConfig` dataclass and
  `<config_dir>/projects/<slug>.toml` loading
  (`load_project_configs`)/writing (`save_project_config`), five new
  top-level `Config` fields (`capture_window`, `capture_started`,
  `launch_overlays`, `shared_project_config`, `apply_scope`,
  `projects`), and `write_config_values`/`_dump_toml_table` — a
  generic, validate-before-write `config.toml` merger built on the
  existing hand-rolled TOML value formatter, extended to one level of
  nested `[section]` tables, falling back to a `config.toml.new`
  sibling file (leaving the real file untouched) for a shape it can't
  safely round-trip.

- **v0.3 profile schema, catalogue and diff renderer** (`profiles/`,
  work package V3-profiles): a new `claude_token_lens.profiles`
  package with an allowlist-driven `Profile` schema (`schema.py`) —
  every settings/agent-frontmatter/environment-variable key a profile
  may set, with its type, permitted values, and a doc reference back
  to `docs/config-layers.md`/`docs/api.md`, so `validate()`'s
  rejections and [docs/profiles.md](docs/profiles.md)'s tables come
  from the same source of truth. A hand-rolled deterministic TOML
  emitter (`dump_profile`) round-trips every allowlisted value, since
  the standard library has no TOML writer. Seven shipped catalogue
  profiles (`profiles/catalogue/*.toml`, `catalogue.py`'s
  `list_profiles`/`get`/`suggest`) each cite a real report table/column
  as justification, never an invented number. `diff.py`'s
  `diff_against_effective`/`render_unified_diff`/`apply_command` are
  pure functions (no filesystem access) that render a profile's
  proposed changes against a project's effective config for the
  `"user"`/`"project-local"`/`"repo"` scopes, excluding managed-policy
  keys from the diff body in favour of a "managed by policy" note. See
  [docs/profiles.md](docs/profiles.md) for the full schema/catalogue/
  `suggest()`/diff contract, including the one `recommend.py` lever
  (`"mcpServers"`) that has no exact-name allowlist counterpart. This
  package does not wire `cli.py`'s `apply`/`init`/`baseline` or the
  `/api/profiles*` routes — those remain a later work package's scope.
- **`claude-token-lens apply`** (`profiles/apply.py`, `profiles/
  frontmatter.py`, work package V3-apply): applies a catalogue profile
  (or your own profile TOML file) to a project or your user config —
  the host-side write path the V3-profiles entry above deliberately
  left out of scope. `--dry-run` prints `diff.py`'s own unified-diff
  text before anything is written; a real apply backs up every touched
  file byte for byte under `<config-dir>/backups/<ts>/` before writing,
  so `apply --revert <ts>` always restores the exact prior state.
  `frontmatter.py` is a new, from-scratch parser/patcher for a `.claude/
  agents/<name>.md` file's `---`-delimited frontmatter block: it updates
  an allowlisted key in place while preserving every other character
  (comments, unrelated keys, formatting) verbatim, and refuses outright
  — rather than guessing — on any frontmatter shape it cannot safely
  round-trip (tab indentation, more than one level of nested mapping, a
  duplicate key, an unterminated fence, and a handful of other
  ambiguous shapes; see the module's own docstring for the full list).
  A project-scoped write to a file already tracked by git is refused
  unless `--allow-tracked` is given; a profile agent key with no
  existing `<name>.md` file is refused unless `--force` is given (it
  then creates one from scratch). A managed-settings key is never
  written regardless of scope or flags, and an `env` value is only ever
  printed as `export NAME=value` guidance, never written to any file.
  `--launch` writes a one-session `<config-dir>/profiles/<id>.settings
  .json` overlay instead of a persisted apply. `--project-dir` (not
  `--project`, already taken by the global project-slug filter) selects
  the target directory for `project-local`/`repo` scope, matching the
  identical collision `snapshot-config`/`probe-config` resolve the same
  way. See [docs/profiles.md#applying-a-profile](docs/profiles.md#applying-a-profile),
  [docs/cli.md's `apply`](docs/cli.md#apply),
  and [SECURITY.md](SECURITY.md#applying-a-profile-the-one-command-that-writes-outside-config-dir)
  for full detail.
- **`compare` subcommand** (`compare.py`, work package V3-compare, plan
  "Feature expansion" item 6): A/B compare two arms of sessions, each
  independently selected by a `window:<since>..<until>`,
  `key:<key>=<value>` (a flattened config-snapshot key), `profile:<id>`,
  or `project:<slug>[,<slug>...]` spec, stratified by purpose/mode with
  a minimum-sample gate (`--min-sessions`, default 5). Every table
  carries an "observed, not controlled" note plus each arm's exact
  selection rule (plan "Risks and gaps" item 2: correlation is not
  causation), and a dedicated `compare_co_changed` table surfaces other
  config keys that changed alongside a `key:`-selected pair of arms. See
  [`docs/compare.md`](docs/compare.md).
- **`reconcile` subcommand** (`reconcile.py`, work package V3-compare,
  plan "Enterprise use"/"Finance"): offline-only comparison of this
  tool's own per-turn accounting against an Admin API usage/cost export
  CSV (`--admin-csv FILE`, grouped `--by day|model|day,model`), via a
  tolerant header mapper that recognises several plausible Admin export
  column spellings (including `_5m`/`_1h` cache-creation splits and
  `cost_cents`) and reports any column it couldn't place. A parse
  failure names only the 1-based bad-line number, never the row's own
  content. See [`docs/compare.md`](docs/compare.md).
- **Service web UI** (`service/static/index.html`/`app.js`/`app.css`,
  work package S1-ui): a CSP-compliant, framework-free, no-build-step
  UI with ten keyboard-navigable tabs (Overview, Sessions, Cache, TTL,
  Agents, Config, Profiles, Recommendations, Usage, Diagnostics)
  covering every documented `/api/*` route, `prefers-color-scheme`
  dark/light theming reused from the CLI's standalone HTML report, and
  inline-SVG scorecard/table bar charts. The session timeline now
  renders a real per-turn context/cache-creation series with
  compaction/spawn/human markers (see the S1-integration entry below
  for `turn_series`/`markers`), replacing the placeholder this shipped
  with initially.
- **Capture-improvements batch** (`PARSER_VERSION` 3 -> 4): seven new
  additive `Turn` fields, all derived from data the transcript already
  carries -- no new raw content is ever retained:
  - `tool_wait_s`/`model_latency_s`: timing either side of a turn's own
    tool calls, from the timestamps of the tool_result line(s) answering
    that turn's `tool_use_id`s -- `tool_wait_s` is the harness/tool
    round-trip, `model_latency_s` is the model's own think time before
    its next turn. Both `None` when the turn made no tool calls or a
    timestamp is missing.
  - `tool_result_chars_by_tool: dict[str, int]`: per-turn breakdown (by
    tool name) of the same lengths `TranscriptResult.tool_result_chars`
    already totals for the whole transcript, enabling per-turn context
    composition.
  - `agent_brief_chars: int | None` / `tool_input_chars_by_tool: dict[str,
    int]`: the total length of an `Agent`/`Task` tool_use's own `prompt`
    input string(s) (never the text itself), and a per-tool total of
    every tool_use's JSON-encoded input size for the turn.
  - `read_target_hashes: tuple[str, ...]`: salted HMAC-SHA256 (16 hex
    chars) of each `Read`/`Edit`/`Write`/`NotebookEdit` tool_use's own
    target path in the turn -- never the path itself. The salt is a
    random 32-byte file at `<config_dir>/salt`, created on first use via
    `parse.load_or_create_salt` (0600 where the OS supports it) and wired
    into the parser via the new module-level `parse.set_salt`, which
    keeps `parse_transcript`'s own signature unchanged so a
    `ProcessPoolExecutor` worker can still initialise it once per
    process. With no salt set, `read_target_hashes` is always empty.
  - `human_prompt_chars: int | None` / `human_prompt_has_paste: bool`: on
    the turn following a HUMAN_TEXT event, the summed length of that
    event's own text content and whether any of it looks pasted (over
    2,000 chars, or a `[Pasted text` marker) -- `events.classify_line`
    now also populates `Event.size_chars`/`detail["has_paste"]` for every
    HUMAN_TEXT event it emits, which this reads from.
  - Every new field is covered by `tests/test_capture_improvements.py`
    (including a same-salt/different-salt stability check for
    `read_target_hashes` and a scan confirming the hash never contains a
    path segment) and passes the existing `assert_privacy` scan.
  - `probe.compare_with_parser(result) -> list[str]`: lists every
    `<line_type>.<key>` a probed transcript actually carries that
    `parse.READ_KEYS` (a new, hand-maintained map of the top-level keys
    `parse_transcript` reads per raw line type) says the parser never
    looks up -- printed by `probe`'s CLI output under a new "unread keys
    (vs parse.py)" section. Run once over
    `tests/fixtures/real/session-a`: the unread keys are almost entirely
    session/process bookkeeping already captured elsewhere by
    `discovery.py` (`sessionId`, `parentUuid`, `cwd`, `gitBranch`,
    `agentId`, `slug`, `userType`) plus a handful of narrower items worth
    a look for a future batch -- `user.toolUseResult` (a possible
    alternate/duplicate tool-result representation), `queue-operation.
    content`/`reason`, and `system.stopReason` (present but empty in
    every observed line in this fixture, consistent with this batch's
    decision not to implement the related `TranscriptMeta` fields below).
  - Investigated but deliberately **not implemented**: the task's
    suggested `TranscriptMeta.endedReason`/`stopReason`/`maxTurnsReached`
    additive fields. None of the three keys exist meaningfully in
    `tests/fixtures/real/session-a` -- every subagent `.meta.json` in
    that fixture carries only `{agentType, description, model,
    spawnDepth, toolUseId}`, and the transcript's own `stopReason` field
    (on `system`/`stop_hook_summary` lines) is present but an empty
    string across all 104 occurrences, unrelated to subagent completion.
    Left for a future batch once a fixture that actually populates one of
    these keys is available.
  - `topology.py`'s spawn-write table (`topology_spawn_write`) gains a
    "Mean briefing chars" column (from the new `agent_brief_chars`,
    joined back to each direct spawn via the same tool_use_id join the
    skill roll-up uses); the cost-per-spawn table (`topology_cost_per_spawn`)
    gains a "Mean tool wait" column (mean `tool_wait_s` across that agent
    type's own priced turns). Existing columns on both tables are
    unchanged.
- **Config snapshot schema 2** (additive over schema 1 — a schema-1
  snapshot still loads unchanged): `hooks/snapshot-config.py` now
  resolves and records every settings layer (`managed` >
  `.claude/settings.local.json` > `.claude/settings.json` >
  `~/.claude/settings.json`) individually as `settings_layers`, plus the
  merged `effective`/`effective_provenance` result across them; a
  redacted read of `~/.claude.json` (`claude_json` — MCP server/plugin
  names, trust-dialog/allowed-tools counts, and per-project `last*`
  session totals, matched to the current project by
  `normcase(realpath(...))`, never the raw matching key); and
  `content_layers` (sizes/counts/names only, never content, for the
  CLAUDE.md family including a bounded nested walk, `.claude/rules/`,
  `.claude/commands/`, skills, agents source/shadow rollup, `.mcp.json`,
  output styles, auto-memory footprint, and installed plugins).
  `agents` entries are now tagged `source` (`user`/`project`) and, on a
  name clash, `shadowed_by_project`. See
  [`docs/config-layers.md`](docs/config-layers.md).
- Widened the settings allowlist (`autoCompactEnabled`, `modelPricing` —
  present flag + overridden model ids, never the numbers — plus a
  dedicated `statusLine` present-flag shape) and the environment-name
  allowlist (`OTEL_*`, plus enough irregular names —
  `MAX_THINKING_TOKENS`, `MAX_MCP_OUTPUT_TOKENS`,
  `BASH_MAX_OUTPUT_LENGTH`, `DISABLE_NON_ESSENTIAL_MODEL_CALLS` — that
  the existing `ANTHROPIC_*`/`CLAUDE_*` prefixes now cover every
  documented Claude Code environment lever by name). Four of those
  names are numeric caps rather than secrets, so their integer value is
  recorded too (`env_numeric_caps`): `MAX_THINKING_TOKENS`,
  `MAX_MCP_OUTPUT_TOKENS`, `BASH_MAX_OUTPUT_LENGTH`,
  `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`.
- `snapshots.py`: `effective_config`, `effective_provenance`, `layers`,
  `latest_snapshot_per_project`, `build_effective_config_table`,
  `build_config_layers_table`, `build_config_groups_table`,
  `detect_drift`, `build_config_drift_table`, `claude_json_cross_check`.
  `build_config_section` gains optional `include_effective=True` and
  `sessions_with_observed=` keyword arguments (both off by default, so
  an existing caller's output is unchanged).
- CLI: `claude-token-lens probe-config [--project-dir PATH]` scans a
  project's config layers without a session and prints the layers +
  effective-config tables as Markdown, never a raw path. `snapshot-config`
  gains `--project-dir PATH` to run the hook for an explicit project
  directory instead of the current one (named `--project-dir` rather
  than `--project`, which every subcommand already uses for "a
  repeatable project slug to filter a report by").
- **`claude-token-lens serve`'s JSON API** (S1-api, part of the v0.2
  milestone below): every `/api/*` route in `docs/api.md`
  (`service/api.py`'s `make_handler`), the `serve.py` runtime that opens
  the store, runs one watcher tick, and serves until interrupted, and
  the `serve` CLI subcommand (`--port`, `--bind`, `--allow-remote`,
  `--poll-interval`, `--retention-days`, `--exclude-project`, `--once`).
  Ships alongside an egress test proving no route ever opens an
  outbound connection. The watcher thread and the static web UI (also
  part of the v0.2 milestone) ship from concurrent sibling work
  packages.
- **Service watcher and store rebuild (v0.2)** — `FileWatcher`
  (`service/watcher.py`) polls `--projects-root` on a background thread,
  parsing new or changed transcripts (top-level, subagent and
  workflow-nested subagent) and config snapshots into the SQLite store,
  skipping files still inside the 60-second live-file window until they
  stabilise. `service/rebuild.py`'s `corpus_from_store` reconstructs a
  full `Corpus` from the store's `digest_json` columns alone, with no
  transcript files on disk, so a report can still be built for a session
  after Claude Code's own `cleanupPeriodDays` retention has removed its
  transcript.
- **Service integration (S1-integration)** — reconciled the seams the
  S0/S1-watcher/S1-api/S1-ui work packages documented against each
  other, and shipped the v0.2 deployment artefacts:
  - **Store schema v2**: `Store.upsert_snapshot` now dedupes by its
    natural key (`project_id`, `ts`, `schema_version`) via `ON CONFLICT
    DO UPDATE`, backed by a new unique index; `Store.migrate()`
    drop-and-rebuilds the store whenever a stored schema version is
    older than the code's, since the store is always a derived cache.
    Snapshots without a project slug keep their `__global__`
    attribution but `Store.snapshots()` now reports `project_slug` as
    `null` for them, and `/api/config-diff` treats those rows as
    user-level layers rather than a fabricated project.
  - **`workflow_runs` table**: the watcher now persists
    `<session>/workflows/*.json` via a new `workflows.py`, and
    `rebuild.corpus_from_store` reads them back into
    `SessionBundle.workflows`, closing the round-trip loss S1-watcher
    flagged (covered by a new `workflow-session` fixture).
  - **`ServeOptions.billing_mode`/`monthly_report_dir`**: `serve` gains
    `--billing-mode {api,subscription}` (defaulted from
    `<config-dir>/config.toml` when present) and `--monthly-report
    DIR`; the watcher stamps `sessions.billing_mode` from it.
  - **`Watcher.last_stats`**: the `contracts.Watcher` protocol now
    exposes the watcher's own last-tick stats directly, so `serve.run`
    no longer has to guess at them.
  - **`Store.change_token()`**: `api.py`'s report-model memo now
    invalidates on this instead of reaching into `store._connection()`.
  - **Session timeline data**: `GET /api/session/<id>` gains
    `turn_series` (per-turn context size, cache-creation tokens, RE-CACHE
    flag, preceding-primary flag) and `markers` (compaction/spawn/human
    turn indices), documented in `docs/api.md`; `static/app.js`'s
    `buildSessionTimeline` consumes this directly, replacing the
    placeholder chart S1-ui shipped with. `docs/ui.md`'s tab list is
    reconciled with the ten tabs `index.html` actually ships.
  - **CLI**: `serve --purge` (with `--yes`) deletes
    `<config-dir>/service.db` and its `-wal`/`-shm` sidecars after
    printing what it will delete.
  - **Deployment**: a hardened `Dockerfile`
    (non-root, `HEALTHCHECK` via stdlib `urllib`, no curl/wget) and
    `docker-compose.yml` (read-only config bind, named data volume,
    loopback-only port, `read_only` root filesystem, `cap_drop: [ALL]`,
    `no-new-privileges`); a Windows Scheduled Task pair
    (`scripts/windows/Register-TokenLensTask.ps1`/
    `Unregister-TokenLensTask.ps1`, `-RunLevel Limited`, PowerShell
    5.1-compatible); a systemd user unit
    (`scripts/systemd/claude-token-lens.service`,
    `ProtectHome=read-only` plus a carved-out `ReadWritePaths`); and
    `scripts/build-pyz.py`, a dependency-free `.pyz` build targeting
    `claude_token_lens.__main__:main` so real exit codes propagate. See
    [docs/deploy.md](docs/deploy.md).

- **S1-context-budget**: new `context_budget.py` section (`Section(key="context_budget")`)
  answering "do we track preloaded skills, the system prompt, and the
  autocompact buffer?" -- `context_budget_baseline` (per-project measured
  mean/median first-turn `cache_creation` next to labelled `(est)`
  buckets for human prompt, skills listing, memory files, custom agents,
  MCP tools and a residual system-prompt-and-tools share, from
  characters/bytes divided by 4), `context_budget_autocompact` (the
  configured `autoCompactWindow` vs. the observed effective threshold --
  median `compactMetadata.preTokens` over `trigger == "auto"`
  compactions, a new `compaction.effective_autocompact_threshold` helper
  -- the implied buffer, and a >10% drift flag), and
  `context_budget_statusline` (real, non-estimated ground truth once a
  usage-log row carries `context_window` fields). `recommend.py`'s
  `baseline-bloat` rule now cites this section's sized buckets as its
  evidence and names the largest one in its action text when the section
  is present. `statusline.py` appends three new trailing columns
  (`context_window_used_tokens`, `context_window_size`,
  `context_window_autocompact_threshold`) to the usage-log CSV whenever
  the payload's `context_window` carries numeric fields -- old-format
  (six-column) rows are still read without error.
- **S1-exports**: real `prompt_cache` cache ground truth in the
  statusline, an aggregate-only `export` command, and a scheduled
  `monthly-report`.
  - **Statusline cache segment** (`statusline.py`) now renders real
    ground truth instead of a guessed hit ratio: `cache warm 5m 03:12`
    (a `MM:SS` countdown to `prompt_cache.expires_at`) while warm, or
    `cache cold` with an optional `recache ~12k tokens` hint once
    expired; falls back to an estimate (`cache est ...`) only when the
    payload carries no usable `prompt_cache`, now driven by a
    transcript-derived TTL hint
    (`message.usage.cache_creation.ephemeral_1h_input_tokens`) rather
    than the caller-supplied TTL value. The numeric `prompt_cache`
    fields are appended to the usage-log CSV as six further trailing
    columns (15 columns total), feeding a new `cache_ground_truth` table
    in the `usage` section (`statusline.build_cache_ground_truth_table`,
    wired in by `report.build_report` via `usage_log_rows`).
  - **`report`/`sessions`/`recache`/`ttl`/`compactions`** now load
    `<config_dir>/usage-log.csv`, when present, with a tolerant reader
    and pass the rows into `build_report` as `usage_log_rows` — so
    `context_budget_statusline` and `cache_ground_truth` populate for
    the ordinary CLI report, not only for a caller that builds
    `usage_log_rows` itself.
  - **`claude-token-lens export --format csv-flat|json|otel-jsonl`**
    (`exports.py`): a privacy-safe, aggregate-only-by-default export for
    BI/observability tooling — one row per
    `day`/`project`/`model`/`entrypoint`/`agent_type` (`--per-session`
    opts into a `session_id` column), project slugs hashed by default
    whenever aggregate-only is in effect (`--no-hash-slugs` to opt out),
    reusing the existing salted-hash construction and salt file. The
    `otel-jsonl` format mirrors Claude Code's own OpenTelemetry metric
    names (`claude_code.token.usage`, `claude_code.cost.usage`) as an
    offline approximation. See [`docs/exports.md`](docs/exports.md).
  - **`claude-token-lens monthly-report --out DIR [--month YYYY-MM]`**
    (`monthly.py`): writes `claude-token-lens-YYYY-MM.md`/`.html` for one
    calendar month (default: the previous month) — a finance header
    (cost/tokens by model/project/entrypoint, five-hour blocks under
    subscription billing) plus the `usage` section. The same inputs
    always produce byte-identical files (idempotent), and
    `monthly.write_monthly_report` is the entry point the v0.2 service
    will wire up to `serve --monthly-report DIR`.

### Fixed

- **`export --format csv-flat` doubled every CRLF line ending on
  Windows**, corrupting the file for BI/pandas import; `--out` and
  stdout are now opened with `newline=""`.
- **`--per-session` used to default to raw (unhashed) project slugs.**
  `hash_slugs` now defaults to `True` unconditionally; `--no-hash-slugs`
  still opts out but now redacts just the OS-username segment and warns
  on stderr; `assert_privacy` gained a matching slug-shaped-username
  check.
- **Statusline hardening.** Output is now bounded to 120 characters
  (truncating or dropping the cache segment first) and never wraps to a
  second line; echoed string fields are sanitised and length-capped; a
  non-numeric or millisecond-scale `expires_at` is handled correctly;
  the warm countdown shows `expiring` instead of a clock-skew-stuck
  `00:00`.
- **`top_miss_causes` no longer double-counts a sticky field** — it now
  reads the wire's own cumulative `prompt_cache.miss_causes` counts (a
  new `cache_miss_causes` usage-log column) instead of re-counting one
  real miss on every quiet refresh.
- **The usage-log CSV header is now upgraded in place, once,** when a
  legacy file has fewer columns than the current writer expects, rather
  than silently misaligning columns forever.
- **csv-flat/otel-jsonl cache-creation totals now agree**, including for
  pre-TTL-split transcripts, by keying off the same
  `cache_creation_tokens` total rather than the 5m/1h split; a new
  `cache_write_tokens` csv-flat column carries this total explicitly.
- **`cache_ground_truth` now respects the report's own window/project
  scope** instead of including every ground-truth row ever logged; the
  monthly report applies the equivalent month-scoping.
- **`monthly-report` can now actually produce the `cache_ground_truth`
  table `docs/exports.md` already promised** — `write_monthly_report`
  gained the `usage_log_rows` parameter it was missing.
- **`context_window` field-name fallbacks widened** for used tokens,
  window size and percentage; every statusline invocation now records
  the payload's own key names to `statusline-keys.json` when they
  differ from what is stored.
- **`monthly-report`'s "byte-identical" idempotency claim is now
  actually true** — `write_monthly_report` gained a `generated_at`
  parameter (`--generated-at`/`SOURCE_DATE_EPOCH`) for a genuinely
  byte-identical run.
- **`resolve_month`'s "previous calendar month" default now uses
  `config.tz`**, not the machine's own local zone.
- Hash construction and "byte-identical" documentation corrections in
  `docs/exports.md`, `SECURITY.md`, and `README.md` — the project-slug
  hash now genuinely shares `parse.py`'s read-target-path hash
  construction (HMAC-SHA256, distinct domain tag and truncation length
  so the two can never collide).
- **A1 — `recommend.py`'s spawn-cost rule** now only offers the
  `omitClaudeMd` frontmatter lever for agent types that actually have a
  frontmatter file to trim; built-in agent types get `category="workflow"`
  advice instead.
- **A2 — no table row key may be a bare `int`.** `recache.py`'s and
  `topology.py`'s session/turn/depth-keyed tables now carry a real
  string row key with the count in its own typed column.
- **A3 — recommendation evidence values are now formatted by their
  cited column's kind** (e.g. `63.7%`, `47,345`) instead of printed
  raw; the JSON renderer is unaffected by design.
- **`statusline --config-dir` is now honoured.** `cli.py`'s
  `_cmd_statusline` never forwarded `args.config_dir` to
  `statusline.main()`, so an explicit `--config-dir` was silently
  ignored and the real `~/.claude/token-lens` was written to instead;
  found and fixed during v0.2 release verification against a real
  corpus.
- **`serve --once` now prints its `WatcherStats` line** (duration,
  discovery/parse/store timings, file and session counts) instead of
  discarding them silently, matching what `docs/api.md` already
  documented as a diagnostic.
- **Report-backed API routes now accept `since`/`until`.**
  `/api/report.json`, `/api/ttl`, `/api/config-diff` and
  `/api/recommendations` previously read only `window_days` and
  silently ignored `since`/`until`; they now resolve the window the
  same way the CLI does and cache the result under a `(window_days,
  since, until)` key. See `docs/api.md`.
- **`load_or_create_salt` now opens the salt file in binary mode on
  Windows.** The previous text-mode `os.open()` call silently turned a
  `\n` (`0x0a`) byte in the random salt into `\r\n`, corrupting the
  stored salt whenever one was drawn — the root cause of the
  intermittently flaky `test_load_or_create_salt_persists_across_calls`.

### Security
**`.pyz` zipapp: `_load_snapshot_hook_module` crashed under zipimport**
(`cli.py`, work package V3-init, found while wiring `init` to the same
hook loader) — unrelated to the v0.2-exports batch above:

- `importlib.resources.files(...)` returns a `zipfile.Path` inside a
  built `.pyz`, which `importlib.util.spec_from_file_location` rejects
  (`TypeError: expected str, bytes or os.PathLike object, not Path`) —
  this pre-existing bug affected `snapshot-config` and `probe-config`
  too, but no test exercised either via a built `.pyz` fixture before
  now. Fixed by reading the hook script's source text and `exec`-ing it
  into a fresh `types.ModuleType`, which works identically on a normal
  filesystem install and inside a zip.

### Planned

- Confirmed no route may return `transcripts.path`/`projects.root_path`
  during v0.2 release verification: a full privacy audit of every saved
  API response body, the generated report, and a full-text dump of
  `service.db` (all tables, decompressed `digest_blob`) found no path,
  username, or over-length string leak.

## [0.1.0] - 2026-09-19

### Added

- Repository scaffold: licence, changelog, security policy, `pyproject.toml`,
  the frozen `model.py` data contract, `render/tables.py` cell-formatting
  primitives, and the `cli.py` argparse skeleton with subcommand stubs.
- `workstyle.build_section`, `workflows.build_section`, and
  `snapshots.build_config_section`: report `Section`/`Table` wrappers for
  archetype counts, workflow-run summaries, and the config diff table,
  matching the pattern already used by `classify`/`compaction`/`recache`/
  `ttl`.
- `recommend.py` (WP10b): `recommend()` turns an assembled `ReportModel`
  into evidence-backed `Recommendation`s, implementing every Appendix A5
  rule (`ttl-switch`, `long-tool-waits`, `notification-invalidation`,
  `batch-instructions`, `subagent-volume`, `compaction-churn`,
  `long-context-share`, `cache-read-dominance`, `baseline-bloat`,
  `agent-report-size`, `spawn-cost`, `effort-mismatch`,
  `discovery-share`, `pricing-coverage`, `data-quality`) with archetype
  gating, a minimum-sample gate, and managed-settings-aware scope
  encoding; `render_patch_set()` renders the settings/frontmatter changes
  a recommendation set implies as unified-diff-style text.
- `report.build_report` now accepts `session_overrides` and populates
  `ReportModel.recommendations` via `recommend.recommend()`, using the
  corpus's own archetype and latest config snapshot.
- CLI wiring (WP10c): `report`/`sessions`/`recache`/`ttl`/`compactions` are
  now real, backed by `report.build_report` (the four focused subcommands
  via `include={"overview", <section>}`), with shared `--json`/`--html`/
  `--csv-dir`/`--phases`/`--allow-titles` output flags and `--patch-set`
  on `report` (a no-op until `recommend.py` lands, guarded by
  `importlib.util.find_spec`). `config-diff --key K|--auto-keys` is its
  own standalone consumer of `snapshots.build_config_diff_table`.
  `log-usage`, `scrub-fixture` and `statusline` delegate to their
  existing modules; `probe` (new `probe.py`) is a content-free schema
  histogram (line types, key names, attachment types, system subtypes,
  `version` field values -- every recorded string capped at 64 chars) of
  a project or a single transcript file, for pasting into a bug report
  without leaking transcript content. `init`/`baseline`/`serve` now
  print which future milestone they're planned for. Added `--jobs` to
  the global flag set. `__main__.py` makes `python -m claude_token_lens`
  (and a `python -m zipapp`-built `.pyz`) propagate the real exit code.
- `Recommendation.scope: str = "user"` (`"user"` | `"repo"` | `"managed"`,
  plan "Enterprise use" section) lands as a real `model.py` field on the
  wp10b/wp10c merge, replacing the `"[managed] "` string prefix on
  `Recommendation.lever` that `recommend.py` used as a workaround while
  `model.py` was outside its work package's file list. `render/
  markdown.py` and `render/html.py` show it alongside `Lever:`;
  `render/json_out.py` emits it automatically (generic dataclass-field
  serialisation). `cli.py`'s report-like subcommands now also load
  `<config_dir>/sessions.toml` via `config.load_session_overrides` and
  pass it to `report.build_report(session_overrides=...)`, closing the
  gap `report.py`'s module docstring flagged (WP10a had no `config_dir`
  parameter to load it from).

### Fixed

Independent review of the WP4/WP6/WP8 report-assembly modules found 15
defects (12 from the numbered review pass, 3 added by the coordinator
alongside it), each fixed and landed as its own commit, same
one-commit-per-fix / green-tests discipline throughout:

- **RE-CACHE detection and attribution (`recache.py`, `compaction.py`)**
  — cache-hit signatures are now applied to transcript turns before
  they're scored, instead of leaving every turn unsignatured; added a
  token-weighted control group and a prefix-invalidated primary-cause
  table (invalidation cause was previously undercounted for
  prefix-broken turns); `compaction.py` now marks turns as re-cache via
  the shared `recache.apply()` detector instead of a separate internal
  `is_recache_turn()` heuristic, so its RE-CACHE-flagged write-cost
  figures use the same signature logic as every other module.
- **TTL simulation (`ttl.py`, `compaction.py`)** — simulated turns are
  now priced per-turn via the rates lookup rather than a single blended
  rate; added unconditional counterfactual buckets and a single,
  consistent expiry-loss basis across the 5m/1h comparison; renamed
  `in_window_share`/`break_even_share` to `in_window_pct`/`break_even_pct`
  (stored as percents, turn 0's own prefix included in the shared
  denominator); `delta_usd`/`delta_pct` keep their sign and gained a
  `saving_usd = max(0, delta_usd)` companion; added a `TtlThresholds`
  dataclass (`from_config`/`describe()`, mirroring `RecacheThresholds`)
  threaded through the simulation, fidelity, and report-building
  functions; `TtlTypeStats.recommendation` changed from a property to a
  method taking `th`; `compaction.py`'s RE-CACHE trio now also reads
  from `RecacheThresholds` instead of hardcoding it a second time.
- **Compaction accounting (`compaction.py`)** — added
  `CompactionRecord.join_delta_s` and a module-level `new_tokens()`
  helper (`input_tokens + cache_creation_tokens`); post-compaction
  write/recache cost aggregates and the per-session cost column now
  exclude records whose join to their next turn took longer than 15
  minutes, and the dropped-token share is reported against both the
  `cache_creation` and `new_tokens` denominators.
- **Event and purpose classification (`events.py`, `classify.py`,
  `parse.py`)** — `classify_line` now tests
  `TOOL_DENIAL`/`TOOL_RESULT`/`TASK_NOTIFICATION`/`PEER_MESSAGE` before
  the generic `isMeta` check, so a line carrying both markers gets the
  more specific kind; `META` events gained a `subkind` (the line's
  `origin.kind`, else its leading XML-ish tag name, else `"plain"`).
  `classify_purpose` now checks intent signatures (local-llm-pipeline,
  workflow-run, review, test-triage, planning, docs-or-light-edit,
  refactor) before falling through to the generic
  `agent-fanout`/`general-dev` buckets; `review` relaxed to tolerate up
  to 2 edit turns, `test-triage` relaxed to drop its
  hits-vs-edit-turns comparison once hits reach 5; `build_section` now
  reports its active overnight local-time window as a note.
  `detect_archetype` (`workstyle.py`) now tests `chat-only` before
  `single-model` so a chat-only session with a single resolvable model
  family is no longer misclassified. Pre-split `cache_creation` reads
  (`parse.py`) are now treated as a format difference
  (`Diagnostics.pre_split_turns`) and normalized via
  `ttl.normalize_ttl_split`, rather than silently mis-parsed.
- **Report sections (`workstyle.py`, `workflows.py`, `snapshots.py`)**
  — added `build_section` to all three, matching the pattern already
  used by `classify`/`compaction`/`recache`/`ttl` (see Added above).
- **Statusline robustness (`statusline.py`)** — `_last_assistant_ts`
  now scans the transcript tail with `text.split("\n")` instead of
  `str.splitlines()` (the latter also breaks on `\r`/`\v`/U+2028/U+2029,
  which can legally appear inside a JSON string value and would shear a
  JSONL line into unparsable fragments); `main()` guards
  `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` in a
  `try`/`except`, and every print now goes through a `_safe_print`
  helper that never lets a print failure escape.
- **Test helpers (`tests/helpers.py`)** — `assert_privacy` now inspects
  `Table` cells and `Section` notes, not just top-level dataclass
  fields.

Two deviations from the original plan text, found and reported rather
than silently reconciled:

- **WP4 fidelity bar.** The plan's TTL break-even section (Appendix A4)
  is referenced elsewhere as flagging simulation fidelity above 5%, but
  Appendix A4 itself specifies "flag agent types above 10%" —
  `TtlThresholds.fidelity_warn_pct` implements the 10% figure that
  Appendix A4 actually states.
- **A2 META description.** Plan Appendix A2's detection table describes
  `META` lines without a `subkind`, but observed transcripts carry
  enough structure (an `origin.kind`, or a leading XML-ish tag in the
  content) to sub-classify them usefully — the events.py fix above adds
  `subkind` as an extension of A2's table rather than a literal
  implementation of it.

Further independent review (findings R4/R5/R7/R8/R15/R19/R23), each fixed
and landed as its own commit:

- **R4** — `parse.py`'s `_redact_paths` now also redacts relative
  Windows paths (no drive letter) and `<user@host>`-shaped `@`-tokens,
  not just absolute `C:\Users\<name>\...` paths.
- **R5** — `report.py`'s `--group-by agent`/`model`/`entrypoint` re-fold
  now keys `RecacheStats` by transcript instead of by session, so a
  subagent transcript is grouped under its own agent/model/entrypoint
  rather than inheriting its parent session's.
- **R7** — `report.py`'s scorecard context-hygiene stats
  (`median_ctx`/`median_top_level_ctx`) are now computed from top-level
  transcripts only, not every transcript (subagent ctx values, which run
  much larger, were skewing them).
- **R8** — `compaction.py`'s `compactions_per_session_mean` now divides
  by every session, not just the sessions that compacted; the old value
  is kept as `compactions_per_compacting_session_mean`.
- **R15** — `usage.py`'s five-hour usage blocks now assign each priced
  turn to the block containing that turn's own local timestamp, instead
  of stamping a whole session's turns onto the block its first turn
  landed in (a session spanning several blocks, e.g. 12+ hours, now
  splits across all of them).
- **R19** — stale "WP10 will..." notes in `compaction.py` and
  `topology.py` rewritten to describe current behaviour: the RE-CACHE
  join to the shared `recache.py` detector already landed (WP10b); the
  topology spawn-write/session-baseline tables were never joined against
  a session's snapshot MCP/plugin counts and no such join is planned.
- **R23** — `usage.py` now skips bundles with no top-level transcript
  (an orphaned subagent whose parent session was never discovered) the
  same way `report.py`'s `build_report` always has, so the two sections'
  session/turn counts no longer disagree on a corpus containing one.

Also landed alongside the above, same discipline:

- `report.py` — the "min sample" values printed alongside recommendation
  thresholds are now the ones `recommend()` actually applies
  (`RecommendThresholds`'s own `min_sessions`/`min_turns`, which can be
  overridden independently of `Config.min_sessions`/`min_turns`), not
  `config.min_sessions`/`min_turns` directly.
- `report.py` — the overview `totals` table gained two rows,
  `top_level_median_ctx` and `top_level_turns_ctx_ge_200k_pct`, computed
  from the same top-level-only record set as the R7 fix, giving the
  "long-context share of recent top-level turns" plan anchor a
  turn-count-basis, top-level-only figure to check against.

**Note from the round-3 review, resolved below:** `parse.py`'s redaction
behaviour changed under R4 above in a way that changes parsed output for
previously-cached transcripts (a path that previously leaked through
`_redact_paths` is now redacted) — `PARSER_VERSION` in `__init__.py`
needed bumping to invalidate stale cache entries, per that constant's
own doc comment. Not done in round 3: `__init__.py` was outside that
change's file scope. Done as part of the `0.1.0` release prep (see
"Release prep" below).

Independent review (round 3) fixes, `cli.py`/`recommend.py`/`ttl.py`:

- **Breaking:** removed the `--allow-titles` CLI flag. It implied a
  privacy control that never existed — `report.py`'s own module
  docstring documents `build_report`'s `allow_titles` parameter as a
  permanent no-op, since nothing anywhere in this codebase captures
  `customTitle`/`ai-title` text to gate in the first place.
  `build_report` still accepts the keyword (unused) for signature
  compatibility.
- New `--tz ZONE` flag overrides `config.toml`'s `tz` for a single run,
  threaded through every report-like subcommand and `config-diff`.
- `--quiet` now actually does something: it used to be accepted by
  argparse (mutually exclusive with `--verbose`) but never once
  consulted, so passing it silently changed nothing.
- `scorecard.py`'s `[thresholds.scorecard]` overrides are now validated
  for correct ascending/descending ordering; a misordered tuple used to
  silently score a corpus at the wrong level and now raises a clean,
  named `claude-token-lens report: ...` error (exit 2) instead.
- `init`/`baseline`/`serve`'s `--help` listing now leads with the same
  `(planned)` marker every other not-yet-implemented subcommand uses.

Independent review (round 4) fixes, merged from `fix-review3-a` into
`main` for this release, plus release-prep work:

- **Release prep.** `PARSER_VERSION` bumped `2` -> `3` in `__init__.py`
  (parse-time redaction changed under R4 above, invalidating
  previously-cached digests) and `pyproject.toml`'s version bumped to
  `0.1.0`, resolving the round-3 follow-up note above.
- **`cli.py`** — `report --json --patch-set` used to append the
  patch-set text after the JSON blob, producing invalid JSON on
  stdout. `--json` now embeds the patch set under a top-level
  `patch_set` string key instead of printing anything else to stdout;
  `--html`/`--csv-dir` write a sibling `patch-set.txt` file next to
  their output; Markdown mode is unchanged (still appends the patch
  set after the report text).
- **`recache.py`** — the two "Re-cache primary cause" tables had
  unstable row order for tied all-zero rows, caused by Python's
  randomized `StrEnum`/set-iteration hashing. Every sort in
  `build_section` now ties-break on the row key string, so output is
  deterministic across runs regardless of `PYTHONHASHSEED`; covered by
  a determinism test that builds the section twice from shuffled
  input.
- **Config-dir semantics** — `hooks/snapshot-config.py`'s
  `resolve_config_dir` treated an explicit `--config-dir` as the
  `~/.claude` root and appended `token-lens/snapshots`, while
  `cli.py`/`snapshots.py` already treated an explicit value as the
  token-lens directory itself. Reconciled on the majority convention:
  an explicit `--config-dir X` is the token-lens directory everywhere
  (`X/snapshots`, `X/cache`, `X/config.toml`, `X/usage-log.csv`);
  the default remains `~/.claude/token-lens`, honouring
  `CLAUDE_CONFIG_DIR`. `snapshots.load_snapshots` and the hook script
  (still standalone, no package import) were both updated, with a new
  round-trip test: the hook writes a snapshot with `--config-dir tmp`,
  then `config-diff --auto-keys --config-dir tmp` finds it.

### Documentation

- Retired stale forward-looking "WP8"/"WP10"/"WP12"/"WP3" notes in
  `classify.py`, `corpus.py`, and `ttl.py` now that those work
  packages have landed, replacing them with statements of current
  fact about what populates each field and why `recache_signature` is
  still unset in `report.py`'s TTL path today.
- README's Performance section now carries real timings (`22.1s` cold
  with `--jobs 1`, `7.6s` warm, `12.6s` warm with `--jobs 4`, measured
  2026-09-19 on the owner's own live 1.6 GB corpus: 120 sessions,
  1,644 subagent transcripts, 29 workflow runs, 30-day window)
  replacing the `<cold>`/`<warm>`/`<jobs4>` placeholders; the TTL
  section now also documents that `ttl.py` prints per-agent-type
  simulation fidelity and suppresses TTL-switch advice when the
  projected saving doesn't clear the simulation's own error margin or
  fidelity exceeds the configured bound.
- README rewritten against the code as it actually stands today (WP12b):
  what it measures and cannot (no billing API, user-supplied prices,
  subscription usage-window billing, the JSONL format's observed-not-
  published status and its two stable alternatives), the two token
  totals with a worked example, how Claude Code's prompt cache and its
  5m/1h TTL levers work, the RE-CACHE definitions and signatures, a
  section-by-section report reading guide (moved into
  `docs/sections-reference.md` once it grew past README-length) with a
  real worked example generated from `tests/fixtures/real/session-a`,
  the TTL simulation assumptions verbatim from `ttl.ASSUMPTIONS`,
  SessionStart-hook and statusline installation fragments generated
  from the code (not hand-typed), Windows-specific notes, an honest
  team/enterprise section naming what's implemented (`exclude_projects`,
  `retention_days`, managed-settings capture, Bedrock/Vertex provider
  detection) versus only planned (Foundry detection, per-provider
  pricing, an `export` command), prior-art credits, and licence/
  contributing/roadmap. `SECURITY.md` rewritten as a corporate-review
  sign-off checklist, correcting an earlier claim that an automated
  egress test already exists (it doesn't — there is no `serve` surface
  yet to test; the guarantee today is structural: no networking library
  is imported anywhere in the codebase) and clarifying that
  `--no-cache`/`--rebuild-cache` are parsed but not yet acted on by any
  CLI subcommand. Both files are written to match the CLI's actual
  current state: only `pricing-check` and `snapshot-config` are wired
  up; every other subcommand is a stub.
- README and `docs/sections-reference.md` brought up to date against
  the WP10c CLI wiring and WP10-merge `Recommendation.scope` (WP12c):
  the `<!-- CLI-USAGE -->` placeholder replaced with a subcommand table
  and exit-code reference generated from each subcommand's own
  `--help`; every "not yet built" callout that WP10a/WP10b/WP10c closed
  (`overview`, `usage`, `scorecard`, the `report` command, recommendations
  with `scope`, `--patch-set`, `--no-cache`/`--rebuild-cache` actually
  being wired, the `statusline` and `config-diff`/report `config`
  section distinction) rewritten or removed, verified by running the
  CLI against `tests/fixtures/real/session-a` and reading
  `report.py`/`recommend.py`/`scorecard.py`/`usage.py`/`probe.py`
  directly; the zipapp caveat fixed at the doc level (build against
  `claude_token_lens.__main__:main` instead of `cli:main`, verified with
  a fresh `.pyz` build whose `--version` exits 0 and `serve` exits 2);
  a new Performance subsection recording where the cold/warm/`--jobs`
  timings and the digest-cache-location note live; `docs/sections-reference.md`
  gained `overview`/`usage`/`scorecard` tables and Recommendations/
  Diagnostics blocks. The remaining genuine gaps (Foundry detection,
  per-provider pricing tables, `--allow-titles` being a no-op, and
  `usage_windows` not being wired into the assembled report) are kept,
  stated honestly rather than papered over.

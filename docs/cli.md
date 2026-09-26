# Command reference

Every command runs as `python -m claudeglass <command>`. The
shorter `claudeglass <command>` works too, but only when pip's
Scripts folder is on your `PATH`. With the single-file build, run
`python claudeglass.pyz <command>`.

This page groups the commands and says what each one is for. For the
exact, current list of flags, run the command with `--help`:

```powershell
python -m claudeglass report --help
```

With no command at all, `python -m claudeglass` runs `report`.
Every command also accepts the [global flags](#global-flags), such as
`--all-projects` and `--days`. The tables below list only the flags
each command adds.

- [Everyday](#everyday): `report`, the focused views, `check`, `review`, `serve`
- [Setting up and keeping it running](#setting-up-and-keeping-it-running):
  `init`, `status`, `update`, `install-service`, `uninstall-service`,
  `changes`, `uninstall`, `baseline`
- [Changing settings](#changing-settings): `apply`
- [Metrics capture](#metrics-capture): `capture`
- [Team use and exports](#team-use-and-exports): `export`,
  `monthly-report`, `import`, `team-report`
- [Comparing and checking estimates](#comparing-and-checking-estimates):
  `compare`, `reconcile`, `backtest`, `config-diff`
- [Diagnostics](#diagnostics): `snapshot-config`, `probe-config`,
  `probe`, `pricing-check`, `log-usage`, `statusline`, `scrub-fixture`
- [Global flags](#global-flags), [exit codes](#exit-codes) and
  [performance](#performance-and-the-digest-cache)

## Everyday

### `report`

The full analysis, printed as Markdown. It prints every report section,
from the overview and usage, through cache rebuilds, subagents and
quality, to the scorecard and the recommendations.
[`sections-reference.md`](sections-reference.md#sections-at-a-glance)
lists each section in order.

```powershell
python -m claudeglass report --all-projects --days 30
```

| Flag | What it does |
|---|---|
| `--json` | Print the whole report as JSON instead of Markdown |
| `--html PATH` | Also write a single-file HTML report |
| `--csv-dir DIR` | Also write one CSV file per table, plus an index |
| `--explain` | Add what each section and table shows, how to read it and when to act |
| `--phases` | Add the phase split: reading and searching, editing, and testing or building |
| `--patch-set` | Also print the recommendations as diff-style patches to settings and agent files |
| `--baseline ID\|latest` | Add a comparison against a saved [`baseline`](#baseline) |

### Focused views

Each of these prints the overview plus one section. They take the same
output flags as `report`, except `--patch-set`, since recommendations
aren't part of a focused view.

| Command | Shows | More |
|---|---|---|
| `sessions` | Each session's mode and purpose, with the evidence for each | [sections-reference](sections-reference.md#sessions-classifypy) |
| `recache` | Cache rebuilds: which replies paid to write the cache again, why, and what it cost | [concepts](concepts.md#3-cache-rebuild-definitions-and-signatures) |
| `ttl` | Whether a 5-minute or 1-hour cache lifetime (TTL) is cheaper, per agent type | [sections-reference](sections-reference.md#ttl-utilisation-metrics) |
| `limits` | Usage-limit pauses, subagents stopped by the harness, and resumes | [limits](limits.md) |
| `carry` | What each tool's output costs as it rides along in the context on every later reply | [carry](carry.md) |
| `compaction-sim` | What other `autoCompactWindow` settings would have cost | [compaction-sim](compaction-sim.md) |
| `model-swap` | The most a model or subagent type one tier down could save | [model-swap](model-swap.md) |
| `waste` | Spend on replies whose output was never used | [waste](waste.md) |
| `quality` | Failed tool calls, agent runs that didn't finish, corrections, and each agent's model and effort compared with the one it used most | [concepts](concepts.md#7-quality-signals) |
| `compactions` | Each conversation summary (compaction): count, trigger, tokens before and after, and the cache rebuild that followed | [sections-reference](sections-reference.md#compactions-compactionpy) |

`usage`, `agents`, `workstyle`, `workflows` and `scorecard` are report
sections without a focused view of their own. Use `report`, or
`report --json` and take the section you want.

### `check`

Quick actions in the terminal, the same as **Actions › Checks** on the
dashboard. Each check answers one question from your own sessions, with
the evidence, fixes and tips.

```powershell
python -m claudeglass check --all-projects
python -m claudeglass check models
```

Leave the ID out for every check's one-line answer. Give one to see it
in full: `models`, `effort`, `compaction`, `cache`, `tools`, `skills`,
`claude-md`, `tool-output`, `hooks`, `tool-search`, `habits`, `quality` or `cost-record`
(whether ClaudeGlass's cost matches Claude Code's own record). The window comes
from the global `--days`, `--since` and `--until`.

### `review`

Reviews your CLAUDE.md files or your skills: size, how often each is
sent, what it costs, and what to trim. It's **Agents & context ›
Context** in the terminal.

```powershell
python -m claudeglass review claude-md
python -m claudeglass review skills
```

The window comes from the global flags.

### `serve`

Runs the dashboard: a watcher that reads new transcripts every 30
seconds, a local database, a read-only JSON API and the web pages. It
listens on 127.0.0.1 only, unless you pass `--allow-remote`.
[`deploy.md`](deploy.md) covers running it at logon, and
[`api.md`](api.md) and [`ui.md`](ui.md) cover the API and the pages.

```powershell
python -m claudeglass serve
```

| Flag | What it does |
|---|---|
| `--port N` | The port to listen on. Default 8765 |
| `--bind ADDRESS` | The address to listen on. Default `127.0.0.1`, loopback only |
| `--allow-remote` | Allow `--bind` to a non-loopback address, which is refused otherwise |
| `--allowed-host NAME` | Repeatable. An extra host name the dashboard answers to. Every other `Host` header gets `403`; see [`api.md`](api.md#host-allowlist-dns-rebinding) |
| `--poll-interval SECONDS` | How often the watcher looks for new transcripts. Default 30 |
| `--retention-days N` | Remove sessions older than N days on every poll. Default: `retention_days` in `config.toml`, else keep them forever |
| `--exclude-project SLUG` | Repeatable. A project that is never read |
| `--billing-mode {api,subscription}` | How amounts are shown. Default: `billing` in `config.toml`, else worked out from your transcripts |
| `--monthly-report DIR` | Write last month's [`monthly-report`](#monthly-report) into `DIR` when it's missing. Checked at start and every hour; a failure is logged and tried again at the next check |
| `--once` | Read new transcripts once, print what it did, and exit |
| `--exit-on-code-change` | Exit with status 3 when this package's code changes on disk (an update that landed without a restart), so the service starts again on the new code. `install-service` registers it; see [`deploy.md`](deploy.md#updating-under-a-running-serve) |
| `--store PATH` | The dashboard's database. Default `<config-dir>/service.db`. A running `serve` locks it, so give a second copy its own |
| `--purge --yes` | Delete the dashboard's database and its sidecar files, then exit. Refused while a `serve` has it open |

## Setting up and keeping it running

### `init`

Sets the tool up. It asks four things: how you pay, whether to connect
to Claude Code, whether to start the dashboard at logon, and whether to
turn on sharper tips (metrics capture at Essentials for 14 days, plus
the `/tl-feedback` skill). Then it lists every change in one review and
asks `Go ahead? [Y/n/d]`, where `d` shows the exact changes. After a yes
it writes `config.toml`, makes the changes, records a first baseline
for this project, and ends with a summary of what works.
[`onboarding.md`](onboarding.md) lists every question.

```powershell
python -m claudeglass init
```

| Flag | What it does |
|---|---|
| `--claude-root PATH` | Claude Code's folder, the one holding `settings.json`. Default `$CLAUDE_CONFIG_DIR`, else `~/.claude` |
| `--advanced` | Also ask the rest: projects to leave out, extra settings files, a shared `.claude` folder, the time zone, where `apply` writes, the capture window, WSL folders, and the full metrics capture and feedback questions |
| `--answers FILE` | A JSON file answering some or all of the questions |
| `--non-interactive` | Never ask, and make the changes without a yes. Work out every unanswered question instead, and print what it chose. This doesn't connect to Claude Code unless you add `--connect`, or start the dashboard at logon unless you add `--install-service` |
| `--no-install` | Don't connect to Claude Code |
| `--connect` | Make the `settings.json` change without asking. It's still shown, and the file is backed up first |
| `--install-service` | Start the dashboard at logon without asking |
| `--no-service` | Skip the logon question entirely |
| `--dry-run` | Show every change, including the `settings.json` diff and the logon task, and write nothing: no `config.toml`, no hook files, no baseline |
| `--repair-hook` | Fix a SessionStart hook command that can't run, without asking. It fixes single backslashes in a JSON path, and a `%VARIABLE%` that Git Bash doesn't expand. It also replaces a Python that can't be found, such as a missing `py` launcher. It keeps your own Python when it's found, writes the folder out in full, and backs up `settings.json` first |
| `--capture-level LEVEL` | Answer the metrics capture question: `off`, `free`, `essentials`, `standard` or `deep`. Capture uses tokens |
| `--capture-for DURATION` | Switch capture off by itself after this long, such as `30d`, instead of after 14 days |
| `--capture-no-limit` | Let capture run until you switch it off |
| `--feedback {on,off}` | Answer the `/tl-feedback` question |

Running `init` again is safe. Your answers are the defaults, what is
already done is skipped, and it keeps your capture window. Check the
result any time with [`status`](#status).

### `update`

Installs the newest version with pip, then hands over to the new copy
(`update --finish`). That:

- restarts the dashboard on the new version, when it starts at logon,
  and checks which version answers on the port. On Windows, an older
  copy started by hand that still holds the port is named, and stopped
  after a yes;
- brings this tool's hook and status line entries in Claude Code's
  `settings.json` up to date, showing each change and asking first;
- finds copies installed for other Pythons and offers to remove the
  ones nothing uses any more.

This is the one command that goes online: pip downloads the new version
from GitHub. Updating from 0.6.0 or older, `update` stops after the
install; run `update --finish` once afterwards.

```powershell
python -m claudeglass update --dry-run
python -m claudeglass update
```

| Flag | What it does |
|---|---|
| `--from SOURCE` | What pip installs from. Default: the GitHub repository. A local folder works too |
| `--no-service` | Install the new version but leave the running dashboard alone |
| `--yes` | Answer yes to each change it offers: `settings.json` entries, stopping an old dashboard, removing copies for other Pythons |
| `--finish` | The steps after the install, without installing. `update` runs it itself; run it by hand after updating from 0.6.0 or older |
| `--claude-root PATH` | The Claude Code folder whose `settings.json` it updates, as for `init` |
| `--port N`, `--bind ADDRESS` | Where the dashboard runs, if not the defaults |
| `--dry-run` | Show what it would do, changing nothing |

### `install-service`

Starts the dashboard at every logon and starts it now. `init`'s logon
step calls this. It registers a Windows Scheduled Task, a systemd user
unit on Linux, or a LaunchAgent on macOS. [`deploy.md`](deploy.md)
describes what it writes on each system.

| Flag | What it does |
|---|---|
| `--port N` | Default 8765. Must match how you run `serve` |
| `--bind ADDRESS` | Default `127.0.0.1`, loopback only |
| `--dry-run` | Print exactly what it would write and run, without doing either |

### `uninstall-service`

Removes what `install-service` registered. It stops the running
dashboard (`Stop-ScheduledTask` on Windows, `systemctl --user disable
--now` on Linux, `launchctl bootout` on macOS), deletes the definition
it wrote, and says which steps it did. `--dry-run` prints what it would
remove.

### `status`

Checks that the setup works. Each part reads as Done, Waiting, Off or
Needs attention: how you pay, the connection to Claude Code, the
dashboard at logon, sharper tips (metrics capture), the `/tl-feedback`
skill and the usage-limit readings. A part that needs attention names
the command that fixes it. It exits 1 only when an essential part (how
you pay, the connection, the dashboard, or capture you turned on) needs
attention; Waiting and Off parts exit 0. Takes `--claude-root PATH`, as
for `init`.

### `changes`

Lists everything this tool has installed or changed on this machine.
For each item it shows the token cost, what to expect, and the command
that undoes it. Takes `--claude-root PATH`, as for `init`.

### `uninstall`

Takes it all back out. It removes the SessionStart hook, any metrics
capture hooks and the status line from `settings.json`, showing the diff
and backing the file up first. It offers to remove the `/tl-feedback`
and `/tl-brief` skills, and removes the logon service.

```powershell
python -m claudeglass uninstall --revert-changes --delete-data --dry-run
```

| Flag | What it does |
|---|---|
| `--revert-changes` | Also undo every `apply` still in place, newest first |
| `--delete-data` | Also delete this tool's data folder: database, snapshots, usage log, profiles and backups |
| `--dry-run` | Show every step without changing anything |
| `--yes` | Make the changes without asking. They're still printed |
| `--claude-root PATH` | As for `init` |

### `baseline`

Records an onboarding baseline: your mix of session modes and purposes,
a suggested profile, and what it might save. `init` records the first
one. Run it again once the capture window has enough data.

| Flag | What it does |
|---|---|
| `--finalise` | Accept the baseline even if the capture window hasn't finished |
| `--list` | List saved baselines |
| `--show ID` | Print one saved baseline's report |

## Changing settings

### `apply`

Writes a profile, or one setting, into Claude Code's `settings.json` or
an agent file, with a backup and an undo. The dashboard never runs this;
its recommendation cards show the command for you to run.
[`profiles.md`](profiles.md#applying-a-profile) covers scopes, backups
and what it refuses to do.

```powershell
python -m claudeglass apply --set effortLevel=medium --scope user --dry-run
python -m claudeglass apply interactive-chat --dry-run
python -m claudeglass apply --revert 20260919T100252Z
```

| Flag | What it does |
|---|---|
| `PROFILE` | A catalogue profile id, or the path to your own profile TOML file |
| `--set KEY=VALUE` | Change one allowlisted setting instead of applying a profile. Repeatable. Lists are comma-separated (`tools=Read,Grep`); booleans are `true` or `false` |
| `--agent NAME` | With `--set`: change `.claude/agents/NAME.md` instead of `settings.json` |
| `--scope {user,project-local,repo}` | Which settings file is written. Default `user`, or `project-local` once `--project-dir` is given |
| `--project-dir PATH` | The project, for `project-local` or `repo` scope |
| `--claude-root PATH` | Claude Code's folder for `user` scope. Default `$CLAUDE_CONFIG_DIR`, else `~/.claude` |
| `--dry-run` | Explain each change and print the diff, without writing anything |
| `--launch` | Write a one-session overlay instead, and print the `claude --settings <path>` command that starts Claude Code with it |
| `--allow-tracked` | Allow writing a file that git already tracks, which is refused otherwise |
| `--force` | Create a missing agent file instead of refusing |
| `--revert TS` | Undo an earlier apply, named by the timestamp it printed. Refused if the file was edited since |
| `--ignore-changes` | With `--revert`: restore the backup anyway |
| `--list-backups` | List earlier applies and exit |

Restart Claude Code after a change. It reads some settings, such as the
model and effort level, only when a session starts. A restart makes sure
the change applies.

## Metrics capture

### `capture`

Metrics capture has Claude tag its replies with words from a fixed list,
such as the kind of task and how clear the request was. The tags help
suggestions fit how you work. It's off by default and uses tokens while
it's on. [`capture.md`](capture.md) covers the levels, tags, sampling
and privacy.

```powershell
python -m claudeglass capture status
python -m claudeglass capture on --level essentials --for 7d
```

| Action | What it does |
|---|---|
| `status` | The default. Shows the level, the metrics that are on and their rough size, and what capture has cost since it was turned on. While it's off, shows what each level would have cost over your last 14 days. Also says whether `settings.json` runs the hooks it needs and, with coaching notes on, your split points and what the notes cost over the last 14 days |
| `on`, `off`, `level LEVEL` | Turn capture on or off, or change the level. Anything that uses more tokens shows a cost warning and asks first |
| `enable METRIC...`, `disable METRIC...` | Turn single metrics on or off. `capture status` lists their ids |
| `connect` | Add the hook entries the chosen metrics need to `settings.json`, after showing the diff |
| `remove` | Switch capture off and take the hook entries out, after showing the diff |
| `feedback on\|off` | Add or remove the `/tl-feedback` skill and its status-line reminder |
| `brief on\|off` | Add or remove the `/tl-brief` skill, which asks for what a request is missing before Claude starts |
| `refresh` | Work out your coaching-note split points (`coaching.json`) from your last 30 days now, as the dashboard's service does once a day. `--dry-run` prints them without writing. See [`coaching.md`](coaching.md) |
| `prune` | Delete signal files, capture log records and usage log rows older than `retention_days`, or 180 days. A running `serve` already does this on every poll |

| Flag | What it does |
|---|---|
| `--level LEVEL` | For `on`: `free`, `essentials`, `standard` or `deep`. Default `essentials`, or the current level |
| `--for DURATION` | Switch capture off by itself after this long: a number and `h`, `d` or `w`, such as `7d` |
| `--until DATE` | Switch it off at this date or time instead |
| `--no-limit` | Run until you switch it off, instead of the default 14 days |
| `--sample {100,50,25,10}` | Capture this percentage of sessions, picked at random per session |
| `--yes` | Make the changes without asking. They're still printed |
| `--dry-run` | Show what would change, without changing it |
| `--claude-root PATH` | As for `init` |

## Team use and exports

These commands let a team lead see costs across several people's
machines without collecting anyone's sessions. [`team.md`](team.md) and
[`exports.md`](exports.md) describe what each file holds.

### `export`

Writes a privacy-safe summary of your usage for spreadsheets and
dashboards. By default it has no session ids, and project names are
hashed. Every column is a count, a token total or a cost: never a
prompt, tool output or file path.

```powershell
python -m claudeglass export --format csv-flat --out usage.csv
```

| Flag | What it does |
|---|---|
| `--format {csv-flat,json,otel-jsonl}` | Default `csv-flat`. `otel-jsonl` writes Claude Code's own OpenTelemetry metric names for an existing collector's dashboards. It's an offline file, not a live exporter |
| `--aggregate-only` / `--per-session` | One row per day, project, model, entrypoint and agent type (the default), or one row per session, which includes session ids |
| `--hash-slugs` / `--no-hash-slugs` | Project names are hashed by default in every mode. `--no-hash-slugs` shows them, with your user name still hidden, and prints a warning |
| `--out PATH` | Write to a file instead of the terminal |
| `--generated-at ISO8601` | Fix the `json` export's timestamp, so a rerun is byte-identical |
| `--aggregate` | Write a team document for [`import`](#import) instead |
| `--include-projects` | With `--aggregate`: add hashed project names |

### `monthly-report`

Writes one calendar month's summary as Markdown and HTML. It gives total
cost, tokens and sessions, and cost by model, project and entrypoint. On
a plan it adds the five-hour blocks used. Then comes the usage section. It's sized for a monthly
habit, not the full report.

```powershell
python -m claudeglass monthly-report --out reports
```

| Flag | What it does |
|---|---|
| `--out DIR` | Required. Where to write `claudeglass-YYYY-MM.md` and `.html` |
| `--month YYYY-MM` | Default: last calendar month, in your time zone |
| `--generated-at ISO8601` | Fix the trailing "Generated at" line, so a rerun is byte-identical. `SOURCE_DATE_EPOCH` works too |

A running `serve --monthly-report DIR` writes it for you each month.

### `import`

Checks one or more team documents from `export --aggregate` and copies
them into `<config-dir>/team/` for `team-report`. It stops with exit
code 2 and the reason at the first file that isn't valid.

```powershell
python -m claudeglass import my-machine.json colleague-a.json
```

### `team-report`

Compares every imported machine, one column each, by way of working and
by agent type. A machine shows as a short hash, never its name. A cell
with fewer than 5 sessions reads `n<5`.

| Flag | What it does |
|---|---|
| `--min-sessions N` | The fewest sessions a cell needs to show a number. Default 5 |
| `--json`, `--html PATH`, `--csv-dir DIR` | As for `report` |

## Comparing and checking estimates

### `compare`

Compares two groups of sessions, such as before and after a change. It
splits them by purpose and mode, and by the kind of task once capture
has tagged half the sessions. A row needs a minimum number of sessions
on each side to count. [`compare.md`](compare.md) has the details.

```powershell
python -m claudeglass compare --a window:2026-09-01..2026-09-10 --b window:2026-09-11..2026-09-20
```

| Flag | What it does |
|---|---|
| `--a SPEC`, `--b SPEC` | Required. Each is `window:<since>..<until>`, `key:<key>=<value>`, `profile:<id>` or `project:<slug>[,<slug>...]` |
| `--stratify KEY,KEY` | How to split: `purpose`, `mode`, `task`. Default `purpose,mode`, plus `task` when covered |
| `--min-sessions N` | Sessions needed on each side. Default: `min_sessions` in `config.toml`, itself 5 |
| `--json`, `--html PATH`, `--csv-dir DIR` | As for `report` |

### `reconcile`

Compares this tool's figures with a usage and cost export from
Anthropic's Admin API, offline. [`compare.md`](compare.md) has the
details.

| Flag | What it does |
|---|---|
| `--admin-csv FILE` | Required. The Admin API export |
| `--by {day,model,"day,model"}` | How to group the rows. Default `day` |
| `--json`, `--html PATH`, `--csv-dir DIR` | As for `report` |

The window comes from the global `--days`, `--since` and `--until`.

### `backtest`

Shows every "what if?" estimate the dashboard made for you, matched to
the real change it became. Each one is judged on the sessions before and
after. It also lists estimates still waiting for a match or more data.
It only reads the dashboard's database; a running `serve` does the
judging. [`backtest.md`](backtest.md) has the details.

### `config-diff`

Compares sessions grouped by the value of one Claude Code setting, from
the snapshots the SessionStart hook takes. It prints its own plain-text
tables. Give exactly one of `--key KEY` (one setting) or `--auto-keys`
(every setting that changed across the snapshots).

## Diagnostics

### `snapshot-config`

Takes a snapshot of your Claude Code settings, or installs the
SessionStart hook that takes one at every session start.
[`reference.md`](reference.md#the-sessionstart-hook) covers what it
records.

| Flag | What it does |
|---|---|
| `--print-hook` | Print the `settings.json` fragment, for Windows and for Linux and macOS |
| `--install-hook` | Copy the hook script into `<config-dir>/hooks/` |
| `--project-dir PATH` | Snapshot this project instead of the current folder |
| `--managed-path PATH` | Read managed settings from this file instead of the platform's own |
| `--min-interval SECONDS` | Skip the write when an identical snapshot is younger than this. Default 300 |

### `probe-config`

Reads a project's settings layers straight from disk, without needing a
session: the same view `snapshot-config` records.
[`config-layers.md`](config-layers.md) describes it. Takes
`--project-dir PATH` (default: the current folder) and
`--managed-path PATH`.

### `probe`

Prints a content-free outline of a project's transcripts: line types,
key names, attachment types and Claude Code versions, with every string
cut to 64 characters. It's safe to paste into a bug report. `--file PATH`
probes one transcript file instead.

### `pricing-check`

Prints where the rate card came from and its prices. With `--models
ID,ID,...`, it also shows how each model id is priced, and flags an id
priced at the closest match rather than its own rate.

### `log-usage`

Reads a pasted `get_usage` JSON payload from the terminal and adds its
rows to the local usage log.

### `statusline`

Claude Code's status line command. Claude Code runs it and sends it a
JSON payload on every refresh. `--print-install-fragment` (or
`--install`) prints the `settings.json` fragment instead.
[`reference.md`](reference.md#the-status-line) covers what it shows and
records.

### `scrub-fixture`

Turns a real session folder into a privacy-scrubbed test fixture, or
checks one that is already scrubbed. For contributors.

| Flag | What it does |
|---|---|
| `--session-dir PATH --out PATH` | Scrub this session into this folder |
| `--verify OUT_DIR` | Check a scrubbed folder |
| `--key-seed SEED` | A fixed hashing key, for tests only |

## Global flags

Every command reads these.

| Flag | What it does |
|---|---|
| `--projects-root PATH` | Where Claude Code's project folders are. Default `~/.claude/projects`, or `$CLAUDE_CONFIG_DIR/projects`. Repeatable, to read several folders. `extra_projects_roots` in `config.toml`, such as a WSL folder that `init` added, is always read as well |
| `--project NAME` | Repeatable. A project to include. Default: the one for the current folder |
| `--all-projects` | Include every project |
| `--project-family REGEX` | Treat projects whose names match as one, such as a project and its worktrees |
| `--days N` / `--since DATE` | Where the window starts. Use one or the other |
| `--until DATE` | Where the window ends |
| `--limit N` | Read at most N sessions |
| `--window-by {last-reply,mtime,timestamp}` | What puts a session in the window: its last reply (the default), its file's modified time, or its first reply |
| `--pricing PATH` | Use another rate card instead of the packaged one or your own in the config folder |
| `--config-dir PATH` | This tool's own folder. Default `~/.claude/claudeglass`, or `$CLAUDE_CONFIG_DIR/claudeglass` |
| `--tz ZONE` | A time zone for this run only, such as `America/New_York`. Default: `tz` in `config.toml`, else your machine's |
| `--group-by {agent,entrypoint,mode,model,project,purpose}` | How tables that support it are grouped |
| `--no-cache` / `--rebuild-cache` | Skip the digest cache for this run, or empty it first and fill it again. Use one or the other |
| `--jobs N` | How many files to read at once. Default 1 |
| `--quiet` / `--verbose` | Print less, or more. `--verbose` adds a line on stderr with files read, cache hits and misses, and time taken |
| `--version` | Print the version and exit |

## Exit codes

Every command uses the same three.

| Code | Meaning |
|---|---|
| `0` | It ran and printed its output |
| `1` | No data: no sessions for the projects and window you picked, or, for `config-diff`, no settings snapshots. A one-line reason on stderr names the projects folder and the window |
| `2` | Bad input: a config or rate card file it can't read, a flag combination that doesn't work, or an unknown command |

## Performance and the digest cache

These timings come from a real 1.6 GB set of transcripts, measured on
2026-09-19 on the maintainer's own machine. The run used `--all-projects`
and a 30-day window: 120 sessions, 1,644 subagent transcripts and 29
workflow runs.
Claude Code was writing to those files at the time, so treat them as
typical rather than lab-controlled.

| Run | Time |
|---|---|
| Cold (`--rebuild-cache`, empty digest cache, `--jobs 1`) | 22.1 s |
| Warm (nothing changed, digest cache filled) | 7.6 s |
| Warm, `--jobs 4` | 12.6 s |

The digest cache makes the warm run fast. It lives in
`<config-dir>/cache/`, one JSON file per transcript, keyed by the
transcript's path plus `SCHEMA_VERSION` (in `model.py`) and
`PARSER_VERSION` (in `__init__.py`). A release that changes either one
re-reads only the entries it needs to; see
[`cache.py`](../src/claudeglass/cache.py). Delete the folder, or
pass `--rebuild-cache`, to read everything again. The dashboard's own
timings are in [`deploy.md`](deploy.md#performance).

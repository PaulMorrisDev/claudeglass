# How it works, and what it can't do

The README covers installing and using claudeglass. This page
holds the detail behind it:

- what it reads, and what it can't measure;
- the SessionStart hook and the status line;
- notes for Windows;
- what's planned next.

[`cli.md`](cli.md) lists every command, [`concepts.md`](concepts.md)
explains the numbers, and [`../SECURITY.md`](../SECURITY.md) is the
full privacy and security checklist.

## What it reads, and what it can't

claudeglass reads the transcripts Claude Code keeps on your
machine. These are `~/.claude/projects/<slug>/<session>.jsonl`, plus
`<session>/subagents/agent-*.jsonl` and
`<session>/workflows/wf_*.json`. It groups the lines Claude wrote into
priced replies and turns them into token, cache and cost figures.

It never sends your data anywhere: no telemetry, no update check. The
one command that goes online is `update`, which runs pip to download the
new version from GitHub. The dashboard's server makes no outbound
connections, and a test fails if it ever tries. [`../SECURITY.md`](../SECURITY.md#no-outbound-network-calls)
has the detail.

What it can't do:

- **Read your bill.** Anthropic has no way to read back what a Claude
  Code session actually cost. Every amount is worked out from a rate
  card in [`pricing.toml`](../src/claudeglass/pricing.toml),
  which you can edit. The tool never fetches prices and has no code that
  could.
- **Price a plan in dollars.** On a Pro or Max plan you don't pay per
  token. What limits you is the five-hour and weekly usage window, not a
  dollar total. So on a plan, amounts are a share of your usage limits
  once the status line has logged enough readings. Until then, they're
  list-price equivalents. A list-price equivalent is good for comparing
  two setups. It isn't an invoice.
  - The report says which mode it uses and why. So does the dashboard's
    Overview. The usage section labels plan amounts as list-price
    equivalents. It fills the five-hour blocks table only on a plan; on
    pay-per-token billing it prints a one-line note instead.
  - `billing` in `config.toml` is `"auto"` by default. Auto means
    subscription once the usage log holds a usage-limit reading, and
    `"api"` otherwise. Claude Code reports usage limits only to Pro and
    Max plans. Set `billing = "subscription"` or `"api"` to
    choose yourself.
  - In the code, `ReportMeta.billing_mode` and `Config.billing` hold
    this ([`model.py`](../src/claudeglass/model.py),
    [`config.py`](../src/claudeglass/config.py)).
- **Rely on a stable format.** Claude Code's docs describe the
  transcript format as internal, and it changes between versions. Every
  field this tool reads was found by looking at real transcripts. The
  parser skips line types and fields it doesn't know rather than failing
  (see [Windows notes](#windows-notes) and
  `Diagnostics.ignored_line_types`). If you need a stable contract,
  Claude Code documents two:
  - its OpenTelemetry metrics (`CLAUDE_CODE_ENABLE_TELEMETRY=1`, metrics
    `claude_code.token.usage` and `claude_code.cost.usage`);
  - `claude -p --output-format json` for single scripted runs.

## The SessionStart hook

[`hooks/snapshot-config.py`](../src/claudeglass/hooks/snapshot-config.py)
records your Claude Code settings when a session starts. That lets the
dashboard show what changed, and what that did. The script uses only
Python's standard library and imports nothing from this package, so it
keeps working when copied on its own.

**The easy way:** `python -m claudeglass init` installs the
script, shows the exact `settings.json` change, and makes it only after
you say yes, backing the file up first. `python -m claudeglass
uninstall` takes it out again. The rest of this section is for doing it
by hand.

**What it costs:**

- The hook runs once when a session starts, takes a few milliseconds,
  and prints nothing, so it adds no tokens to the conversation.
- `init` registers it with `"async": true`, so it never delays a
  session.
- The fragments below leave that key out. Add it next to `"command"` if
  you want the same.

To install it by hand:

```powershell
python -m claudeglass snapshot-config --install-hook   # copies the script into <config-dir>/hooks/
python -m claudeglass snapshot-config --print-hook     # prints the settings.json fragment
```

`--print-hook` prints both variants. Merge the one for your system into
the `"hooks"` key of `~/.claude/settings.json`. `SessionStart` may
already have entries: add to the list, don't replace it.

Windows:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "\"C:\\path\\to\\python.exe\" \"C:\\Users\\<you>\\.claude\\claudeglass\\hooks\\snapshot-config.py\""
          }
        ]
      }
    ]
  }
}
```

Linux and macOS:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$HOME/.claude/claudeglass/hooks/snapshot-config.py\""
          }
        ]
      }
    ]
  }
}
```

On Windows the command gives Python and the script by full path. Two
things can stop the hook running without any visible error:

- Claude Code may run hooks through Git Bash, which doesn't expand
  `%USERPROFILE%`.
- The `py` launcher isn't always on the `PATH`.

The Data quality page flags both, and `python -m claudeglass init
--repair-hook` fixes them. It writes the folder out in full and keeps
your own Python when it finds it. A new install names your main Python
rather than a virtual environment's, which could later be deleted. The
hook needs only the standard library.

The hook always exits with code 0. It prints nothing when it works, or
one line on stderr when it fails, so a broken Python can never block a
session from starting. Each run writes one snapshot to
`<config-dir>/snapshots/<UTC timestamp>.json`. It skips the write when
an identical snapshot is younger than `--min-interval`, 300 seconds by
default. A snapshot holds:

- **Environment variable names, never values**, for every name starting
  `ANTHROPIC_`, `CLAUDE_` or `OTEL_`, plus a short list of other
  documented Claude Code variables. A few numeric caps, such as
  `MAX_THINKING_TOKENS`, `CLAUDE_CODE_MAX_OUTPUT_TOKENS` and
  `CLAUDE_CODE_AUTO_COMPACT_WINDOW`, also keep their number, because
  they are limits, not secrets.
- **Settings values, but only for a small allowlist:**
  - `model`, `effortLevel` and `maxEffortLevel`;
  - `outputStyle`;
  - `autoCompactWindow` and `autoCompactEnabled`;
  - `promptCacheTtl` and `subagentPromptCacheTtl`;
  - `cleanupPeriodDays` and `desktopSessionCleanupPeriodDays`;
  - `autoUpdatesChannel`, `alwaysThinkingEnabled`, `includeCoAuthoredBy` and `fastMode`;
  - any plain true/false or whole-number value, since that's a toggle or a limit, never content.

  Every other key is reduced to its shape (`dict(n)`, `list(n)`,
  `str(len)`). A few, such as `statusLine`, are reduced to whether
  they're set.
- **Every agent's frontmatter, redacted the same way.** Model, effort,
  `maxTurns`, `omitClaudeMd` and `experimental.cacheTtl` are kept.
  `description` is always reduced to its length. MCP server names and
  enabled plugin names are recorded as names only.
- **Managed (organisation policy) settings**, read the same way from the
  platform's policy file into `managed_settings`. Their top-level key
  names, never values, go into `managed_keys`, so a recommendation can
  say a setting is managed by policy. The policy file is at:
  - macOS: `/Library/Application Support/ClaudeCode/managed-settings.json`
  - Linux: `/etc/claude-code/managed-settings.json`
  - Windows: `%ProgramData%\ClaudeCode\managed-settings.json`

  Override the path with `--managed-path`.

[`config-layers.md`](config-layers.md) lists every field of a snapshot.

## The status line

The status line is a separate entry point. Either of these prints the
`settings.json` fragment:

```powershell
python -m claudeglass statusline --print-install-fragment
python -m claudeglass.statusline --print-install-fragment
```

Merge the result into `~/.claude/settings.json`. It replaces any
`"statusLine"` key you already have.

Windows:

```json
{ "statusLine": { "type": "command", "command": "\"C:\\path\\to\\python.exe\" -m claudeglass.statusline" } }
```

Linux and macOS:

```json
{ "statusLine": { "type": "command", "command": "python3 -m claudeglass.statusline" } }
```

The status line runs only in Claude Code in a terminal. Sessions in the
desktop app or an IDE never run it, so usage-limit readings come only
from terminal sessions. The Data quality page says when none are
arriving. Like the hook, it adds no tokens to the conversation.

With metrics capture's status-line items on, it prints a second line.
Those items are `feedback = ["feedback_note"]` or
`coaching = ["coaching_line"]` in `[capture]`, set from **Setup ›
Capture** or `python -m claudeglass capture enable`. The second
line shows a live hint when one applies, and otherwise the reminder to
run `/tl-feedback`. The first line doesn't change. A hint appears for:

- a large context at the end of a turn;
- a large last tool output;
- many reads in one message;
- a warm cache about to go cold.

On every refresh, Claude Code sends the status line a JSON payload. It
reads:

- `context_window.used_tokens`, shown as `ctx NNk`;
- `prompt_cache`, the real cache state (see below);
- `rate_limits.{five_hour,seven_day}.used_percentage`, your plan's
  usage-window percentages;
- the last 64 KB of `transcript_path`, never the whole file, to estimate
  the cache lifetime when there is no usable `prompt_cache`.

The usage-window percentages are also added to
`~/.claude/claudeglass/usage-log.csv`, with duplicates by session, reset
time and percentage skipped. The status line never raises an error: if
anything fails, it prints a minimal `claudeglass` line rather than
blanking the status bar.

**Cache segment:**

- **With a real `prompt_cache` object**, the line shows the actual state,
  not an estimate. While the cache is warm, it shows `cache warm 5m
  03:12`, a countdown to `prompt_cache.expires_at`. Once the cache has
  expired, it shows `cache cold`, sometimes followed by `recache ~12k
  tokens` from `prompt_cache.recache_tokens_if_cold`.
- **Without one**, it shows an estimate labelled `cache est`. The cache
  lifetime comes from the transcript's own last reply: a positive
  `message.usage.cache_creation.ephemeral_1h_input_tokens` means 1 hour,
  otherwise 5 minutes.

The whole line stays under 120 characters and never shows message text.

The numeric `prompt_cache` fields go into `usage-log.csv` as six extra
columns: warm, lifetime, expiry, misses, miss cause, and tokens to
rebuild if cold. `python -m claudeglass report` uses them for a
`cache_ground_truth` table (see
[`sections-reference.md`](sections-reference.md)), which summarises real
cache warmth across sessions.

## Windows notes

- **Project name case.** A project's name (its slug) comes from its
  folder (`discovery.slug_for`). Project folders are matched with
  `os.path.normcase(os.path.realpath(path))`, so `C--Dev-MyApp` and
  `c--Dev-MyApp` count as one project on a case-insensitive drive.
  `--project-family REGEX` groups a project's worktrees together.
- **Git Bash paths.** The privacy scan looks for three kinds of path: a
  Windows drive path (`C:\...`), a `\Users\` segment and a Git Bash drive
  path (`/c/Users/...`). A command run from Git Bash on Windows produces
  the third. The scan is `helpers.assert_privacy`, run by
  `tests/test_privacy.py`.
- **Long paths.** On Windows, a path of 255 characters or more gets the
  `\\?\` prefix (`\\?\UNC\` for a network path) before it's opened
  (`jsonl._windows_long_path`).
- **Files still being written.** A transcript changed in the last 60
  seconds counts as still in use. It's read with `errors="replace"`,
  never cached (`cache.py`), and a half-written last line is tolerated.
  The dashboard reads one again at most once a minute while it keeps
  changing (`watcher.LIVE_REPARSE_S`), so a session you're in the middle
  of stays up to date.
- **`CLAUDE_CONFIG_DIR`** moves the whole folder. When it's set, both the
  projects folder (`.../projects`) and this tool's own folder
  (`.../claudeglass`) are found under it instead of `~/.claude`.
- **Where `settings.json` is.** Every part that reads or changes Claude
  Code's `settings.json` looks in the same place: `--claude-root` when
  given, else `$CLAUDE_CONFIG_DIR`, else `~/.claude`. That covers
  `init` and `init --repair-hook`, `apply`, `changes`, `uninstall`, the
  dashboard's hook and status line checks, and the snapshot hook. Of
  these, `init`, `apply`, `changes` and `uninstall` take
  `--claude-root`.
  - `--config-dir` moves only this tool's own folder, and
    `settings.json` is never looked for beside it.
  - When `--config-dir` isn't the default, the hook and status line
    commands `init` adds end with the same `--config-dir`. What they
    record then lands where the commands and the dashboard read.
- **`CLAUDE_CODE_PROJECT_DIR_NAME`** overrides the project name for the
  current folder only, whether or not `CLAUDE_CONFIG_DIR` is set.

## Roadmap

Shipped work is listed release by release in
[`../CHANGELOG.md`](../CHANGELOG.md). Still planned:

- **A budget guardrail.** A command that exits with a non-zero code past a
  weekly token or daily dollar limit, for scripts. It was planned as
  `check --weekly-tokens N --daily-usd N` before `check` became the
  quick-actions command, so it needs another name.
- **Anomaly detection.** Flagging outlier sessions.
- **An opt-in `--show-paths` view.** Local file paths, for your own use,
  kept out of every report by default.

# First run on a locked-down work machine

The one document to follow on a machine you don't fully control: no
admin rights, Python 3.11+ already there (or not), maybe no `git`,
maybe no proxy access to PyPI. The three install routes, `init`,
`install-service --dry-run`, `report` and `serve` with a health check
were run end to end against a synthetic, throwaway project when this
document was written.

On an ordinary machine, the README's [Quick
start](../README.md#quick-start) is the fast path. This document is
for the first five minutes on a machine you've never run it on before.

## 0. Check Python

```powershell
py -3 --version
```

Needs **3.11 or newer** (`pyproject.toml`'s `requires-python = ">=3.11"`
— the code itself uses the stdlib `tomllib` module, only available
from 3.11). No `py` launcher? See the troubleshooting table below. On
Linux/macOS: `python3 --version`.

## 1. Choose an install route

Three routes, in the order to try them on a machine you don't fully
trust:

| Route | Needs | Best for |
|---|---|---|
| **A. `.pyz` download** | Nothing beyond Python itself — no `pip`, no network, no `git` | The default choice on a locked-down machine |
| **B. `pip` from a local clone** | `pip`. It installs from the folder on disk, but pip may still need PyPI for its build tools (see below) | You already have the source checked out |
| **C. `pip` from PyPI or GitHub** | `pip` + network; `git` only for the `git+https` form | A normal, unrestricted machine |

### Route A: `.pyz` (recommended for a locked-down machine)

Download `claudeglass.pyz` from the [Releases
page](https://github.com/PaulMorrisDev/claudeglass/releases) (every
tagged release attaches a pre-built one — no build step, no `pip`, no
`git`). Then:

```powershell
py -3 claudeglass.pyz --version
py -3 claudeglass.pyz init
```

If you'd rather build it yourself from a checkout (needs the source,
not just the interpreter):

```powershell
py -3 scripts\build-pyz.py
py -3 dist\claudeglass.pyz --version
```

Every subcommand works the same way from the archive: `py -3
claudeglass.pyz <subcommand> ...`, in place of `py -3 -m
claudeglass <subcommand> ...` everywhere else in this document.

### Route B: `pip` from a local clone

```powershell
py -3 -m venv .venv
.venv\Scripts\pip install <path-to-the-cloned-repo>
.venv\Scripts\claudeglass --version
```

If `.venv\Scripts\claudeglass.exe` isn't on `PATH` (it never is
unless you `activate` the venv), call it by its full path as above, or
fall back to:

```powershell
.venv\Scripts\python.exe -m claudeglass --version
```

**`pip install --user .` (outside a venv) does *not* usually put
`claudeglass.exe` on `PATH` either** — pip installs it under
`%APPDATA%\Python\Python3xx\Scripts`, which Windows doesn't add to
`PATH` by default, and pip prints a warning to that effect at install
time. `python -m claudeglass ...` is the reliable fallback in
every case above; it never depends on `PATH` at all.
This document uses `py -3 -m claudeglass`; on Route B, use
`.venv\Scripts\python.exe -m claudeglass` in its place.

**A genuinely offline machine (no PyPI access at all) will fail this
route**, even from a local clone: this project's own `dependencies` are
empty, but `pip install` still needs `setuptools`/`wheel` as *build*
dependencies for a source install (PEP 517), and a fresh venv on Python
3.12+ doesn't come with `setuptools` pre-installed. If `pip install
--no-index <path>` is the only thing you can try, expect `No matching
distribution found for setuptools>=68` — that's the signal to fall
back to Route A.

### Route C: `pip` from PyPI or GitHub

```powershell
py -3 -m pip install claudeglass
```

For the latest code on GitHub instead:

```powershell
py -3 -m pip install git+https://github.com/PaulMorrisDev/claudeglass
```

No `git` on the machine? This works without it (a plain HTTPS
download, no `git clone`):

```powershell
py -3 -m pip install https://github.com/PaulMorrisDev/claudeglass/archive/refs/heads/main.zip
```

These need outbound HTTPS access to PyPI, or to GitHub (and, same as
Route B, to PyPI for the `setuptools`/`wheel` build dependencies) — if a corporate
proxy blocks either, use Route A instead.

## 2. Run `init`

```powershell
py -3 -m claudeglass init
```

(or `py -3 claudeglass.pyz init` for Route A). It prints
`Looking for Claude Code history...`, then how many projects it found
(naming any WSL distro, whose sessions it includes), and asks up to
four questions:

| Question | Means |
|---|---|
| How do you pay for Claude Code? | `1` for a Pro, Max, Team or Enterprise plan: amounts show as a share of your usage limits. `2` for an API key: amounts show in dollars. With nothing saved yet, Enter asks again; a saved answer is the default |
| Connect to Claude Code? `[Y/n]` | Adds a SessionStart hook to Claude Code's `settings.json` that records which settings each session ran with, and a status line if you have none. Skipped when it's already connected |
| Start it at logon? `[Y/n]` | Registers the dashboard to start when you log on (a Scheduled Task on Windows), because Claude Code deletes transcripts after 30 days. Skipped when it's already running; a logon task whose dashboard doesn't answer is offered a repair instead |
| Turn on sharper tips? `[y/N]` | Metrics capture at Essentials for 14 days, plus the `/cl-feedback` skill (see "Metrics capture" below). Needs the connection, so it isn't asked if you said no to it. Not asked when capture is already on at another level, which stays as it is |

Nothing is written yet. It then lists every change under
`Ready to set up:` and asks once: `Go ahead? (d shows the exact changes)
[Y/n/d]`. `d` prints the `settings.json` diff, the skill's file and the
logon task's commands, then asks again; `n` writes nothing. After a yes
it saves `config.toml`, copies the hook files, changes `settings.json`,
writes the skill, starts the dashboard and reads this project's history
for a first baseline, one line each. It ends with `Your setup`: each
item done, off or needing attention, and what to open next. The same
list comes back any time with `claudeglass status`.

`init --advanced` also asks these, before the review:

| Question | Means |
|---|---|
| Projects to always leave out | Folder names under `~/.claude/projects`; their transcripts are skipped everywhere (reports, dashboard, exports) |
| Do you start Claude Code with `--settings` or `CLAUDE_CONFIG_DIR` | Affects where a later `apply` writes a profile. Most people answer no |
| Is this project's `.claude` folder committed to a repo colleagues use | Same: affects `apply`'s default scope |
| Time zone | Used to group reports by day; blank uses this computer's |
| Where should changes you apply go by default | `user` (all your projects), `project-local` (this project, just you) or `repo` (this project, everyone) |
| How many days to collect data before the first baseline | How long `baseline` waits before it has enough data for a confident first read (default 7) |
| Claude Code also runs in WSL: Ubuntu on this computer. Include those sessions? | Asked only when `init` finds Claude Code sessions inside a WSL distro (it runs `wsl -l -q` and looks in each distro's `/home/*/.claude/projects`). Yes adds the folder to `config.toml`'s `extra_projects_roots` |
| Metrics capture level: off, free, essentials, standard, deep | In place of the sharper tips question, after a warning that this uses your Claude tokens and a table of what each level would have cost over your last 14 days of your own sessions. Turning a level on asks one more question, about a 14-day time-box |
| Add the /cl-feedback skill? | A short survey you can run yourself after a piece of work, at any capture level (even off). Skipped when you picked Deep, which includes it |

Running it unattended (a script, or just to skip the prompts) derives
every answer instead of asking, prints what it derived and why, then
the review and the checklist:

```powershell
py -3 -m claudeglass init --non-interactive --no-install --no-service `
  --config-dir C:\path\to\config --projects-root C:\path\to\projects
```

`--config-dir`/`--projects-root` are optional — omitted, they default
to `%USERPROFILE%\.claude\claudeglass` and `%USERPROFILE%\.claude\projects`
respectively (or `%CLAUDE_CONFIG_DIR%\claudeglass`/`...\projects` when
that variable is set) — the same place Claude Code itself already
keeps its transcripts, so on an ordinary machine you don't need to pass
either.

Unattended, `init` leaves `settings.json` alone unless you add
`--connect`. `--dry-run` prints the review and every exact change, and
writes nothing at all.

**What `init` writes under `<config-dir>`:**

- `config.toml` — your answers above. An existing file is merged
  key-by-key. If its shape can't be merged automatically,
  `config.toml.new` is written instead and `init` says so, leaving the
  original untouched.
- `projects\<slug>.toml` — this project's own settings, with
  `--advanced` only.
- `baselines\<id>.json` + `<id>.md` — an initial baseline, if any
  sessions were already found for this project.
- `hooks\snapshot-config.py` — a copy of the hook script, and the
  capture hook's files when sharper tips are on.

Running `init` again keeps the capture window where it is: only the
first `init` sets its start. To start a new window, delete the
`capture_started` line from `config.toml` and run `init` again.

**Connecting to Claude Code.** The change to Claude Code's own
`settings.json` (`%USERPROFILE%\.claude\settings.json`, or
`%CLAUDE_CONFIG_DIR%\settings.json` when that is set; `--claude-root`
names another folder) is:

- a `SessionStart` hook that records your settings when a session
  starts, added only if no hook runs `snapshot-config.py` yet;
- a `statusLine` command, added only if you have no statusline. Yours
  is never replaced;
- with sharper tips on, the capture hooks.

It is written only after the review's yes, and the file is first
copied to `settings.json.bak-<UTC time>` beside it. The hook command
names your main Python install and the script by full path; the
script needs only the standard library, so a deleted virtual
environment can't break it. The statusline command names the Python
you installed claudeglass into. Neither needs the `py` launcher
or a `%VARIABLE%`, so both run under Git Bash. Say no and
`settings.json` is left as it was; `claudeglass init --connect`
makes the change later. `--no-install` skips this step.

If your existing hook command is broken (a mis-escaped path, a missing
interpreter or a `%VARIABLE%`), `init` shows the fixed command at the
start and asks before changing it. See the troubleshooting table.

**Metrics capture — optional, and it costs tokens.** The sharper tips
question (or, with `--advanced`, the capture questions) is the only
place this tool ever spends your Claude usage. Say yes and Claude reads
a short note at the start of a session (and a subagent's) and ends each
reply with a one-line tag you will see, such as
`[tl: task=bugfix brief=clear]`. Capture switches itself off after 14
days unless you say otherwise: `claudeglass capture on --for 30d`
keeps it on longer. `/cl-feedback` is an optional self-review skill that
costs nothing until you run it. Skip either at `init` time and turn it
on later with `claudeglass capture on`/`capture feedback on`,
which ask the same way and show the same `settings.json`/skill-file
diff first. Full detail: [`docs/onboarding.md`](onboarding.md).

## What to expect

- **It uses no Claude tokens unless metrics capture is on.** With
  capture off (the default), it only reads files Claude Code has
  already written — it never calls Claude or any other service, so
  there is no bump in usage from running it, however often. Metrics
  capture (above) is the one opt-in exception: while it's on, Claude
  spends a small number of tokens reading a note and writing a tag
  inside your own session, never through a call this tool makes
  itself.
- **The hook and statusline add nothing to your conversations.** The
  hook starts a short Python process in the background when a session
  starts (well under a second) and prints nothing. The statusline draws
  a line under the prompt, in the terminal only. Neither is sent to
  Claude.
- **The first scan takes a while.** The service reads every transcript
  once (seconds to a few minutes for a large history), then only new or
  changed files. The dashboard opens straight away and shows the scan's
  progress; figures fill in as it goes.
- **It reads; it doesn't change.** Nothing about how Claude works
  changes until you apply a change yourself, through a prompt you give
  Claude or `claudeglass apply`. A change takes effect in the next
  session you start.
- **Cheaper isn't free.** A cheaper model, lower effort or an earlier
  summary can make Claude less thorough. Each change says what it trades
  away. Pick **Since my last change** in the window picker, or look at
  **Your changes**, to check the effect.
- **Amounts on a Pro or Max plan are list-price equivalents** until the
  statusline has logged enough usage-limit readings.

`claudeglass changes` prints the same list with everything the
tool installed and the command that undoes each.

## 3. The logon service

`init`'s service step asks whether to register `claudeglass serve`
to start automatically at logon (default yes). `--no-service` skips the
question. `--install-service` answers yes without asking. Under
`--non-interactive` without `--install-service`, the answer is no.
This matters because Claude Code deletes its own transcripts after
`cleanupPeriodDays` — only a service that's actually *running* when
that happens keeps the history.

Preview exactly what registration would do, without doing it:

```powershell
py -3 -m claudeglass install-service --dry-run
```

On Windows this prints the PowerShell it would run to register a
**Scheduled Task named `ClaudeGlass`**, triggered at your own
logon, `-RunLevel Limited` (no admin rights requested or required, and
none needed). Running from a `.pyz`? The printed command already
points at that exact archive's absolute path (not `python -m
claudeglass`, which cannot work once the code is inside a zip) —
confirm the line contains the full path to your `.pyz`, not a bare
`claudeglass` module reference.

Registering also starts it straight away, on every system. Re-running
`install-service` is safe: on Windows it stops the running copy,
re-registers the task for the Python you ran it with, and starts it
again.

Confirm it actually registered, two ways:

```powershell
schtasks /Query /TN ClaudeGlass
curl http://127.0.0.1:8765/api/health
```

The second answers only while the service is running. Its JSON body
includes `"service_registered": true` once
`is_registered()`'s own platform probe (the same `schtasks` query
above) has confirmed it — `false` or `null` (probe inconclusive) means
check the output `install-service` printed. The dashboard's Overview
and Data quality pages also show a warning when this comes back `false`.

## 4. Open the dashboard

```
http://127.0.0.1:8765
```

Live once the service is running (started by the logon task, or by
running `claudeglass serve` directly in a terminal you leave
open). Loopback-only by default — nothing outside
this machine can reach it unless you pass both `--bind <address>` and
`--allow-remote`. It has no login, so don't do that on a shared network.

It opens on the Overview: what to change next, from your own sessions.
The README's [What each page answers](../README.md#what-each-page-answers)
lists every page.

## 5. Run the first report

```powershell
py -3 -m claudeglass report
```

(`report` is the default subcommand — `claudeglass` with no
arguments does the same thing.) Scopes to the current directory's
project by default; add `--all-projects` to report across every
project under the projects root, or `--project <slug>` for a specific
one.

## 6. Verify nothing left the machine

Read [`SECURITY.md`](../SECURITY.md) for the full guarantee (loopback
bind by default, no message text/file contents/shell commands ever
written to the on-disk store, a project slug's username segment
redacted to `<user>` before it reaches any API response). If you have
the source checked out and a dev environment (`pip install .[test]`),
the same check this project runs on every commit is runnable directly:

```powershell
python -m pytest tests\test_privacy.py tests\test_scrub.py -q
```

(Not available from a bare `.pyz` install, which ships no tests —
`SECURITY.md`'s written guarantee and this project's public CI are the
proof in that case.)

## 7. Undo a change, or uninstall completely

**Undo one change.** Every `apply` prints the command that undoes it.
To find it again:

```powershell
py -3 -m claudeglass apply --list-backups
py -3 -m claudeglass apply --revert <TS>
```

`--revert` restores the backup `apply` made under
`<config-dir>\backups\<TS>\`. If a file was edited after the apply, it
refuses and restores nothing; `--ignore-changes` restores it anyway,
discarding those edits.

**Take everything back out.** Look first:

```powershell
py -3 -m claudeglass uninstall --revert-changes --delete-data --dry-run
```

Then run it without `--dry-run`. It shows each step and asks before
making it:

1. Removes this tool's `SessionStart` hook, any metrics-capture hook
   entries, and statusline from `settings.json`. The diff is shown, and
   the file is copied to `settings.json.bak-<UTC time>` first. A
   statusline of your own is left alone. Then, separately, offers to
   remove the `/cl-feedback` and `/cl-brief` skill files, if present.
2. Removes the logon service, if registered, stopping the running
   dashboard first on every system.
3. With `--revert-changes`: undoes every `apply` still in place, newest
   first. If a file was edited after an apply, that apply is not undone
   at all, and the file is named. Without `--revert-changes`, each one
   is listed with its `apply --revert` command.
4. With `--delete-data`: deletes `<config-dir>` (default
   `%USERPROFILE%\.claude\claudeglass`): the database, snapshots, usage
   log, profiles and backups. It refuses while any applied change is
   still in place, because the backups are the only way to undo it.

`--yes` answers yes to every question (each change is still printed).
Nothing removes the `settings.json.bak-*` copies; delete them yourself
once you're happy.

Finally, if installed via `pip`: `python -m pip uninstall claudeglass`. Via
`.pyz`: delete the one file.

## 8. Update to a newer version

One command does it all for routes B and C:

```powershell
py -3 -m claudeglass update
```

It installs the newest version from GitHub (`--from <path-to-the-cloned-repo>`
for Route B, after a `git pull`), then hands over to the new copy
(`update --finish`), which:

- runs `install-service`, restarting the dashboard on it, and checks the
  version that answers on port 8765. On Windows, an older copy started by
  hand that still holds the port is named, and stopped after a yes;
- brings this tool's SessionStart hook, capture hooks and statusline in
  `settings.json` up to date, showing each change and asking first;
- finds copies installed for other Pythons and, once nothing uses them,
  offers to remove them.

`--dry-run` shows what it would do without changing anything; `--yes`
answers yes to every question. A `.pyz` can't update itself: `update`
says so and links the download.

To do it by hand, install the new version the same way you installed
the first one:

| Route | Update |
|---|---|
| A (`.pyz`) | Download the new `claudeglass.pyz` from the [latest release](https://github.com/PaulMorrisDev/claudeglass/releases/latest) over the old file |
| B (local clone) | `git pull` in the clone, then `.venv\Scripts\pip install --force-reinstall <path-to-the-cloned-repo>` |
| C (PyPI) | `py -3 -m pip install --upgrade claudeglass` |
| C (GitHub) | `py -3 -m pip install --force-reinstall git+https://github.com/PaulMorrisDev/claudeglass` |

`--force-reinstall` is needed for a clone or GitHub because pip skips
a copy whose version number hasn't changed. Then finish with the same Python you just
updated:

```powershell
py -3 -m claudeglass update --finish
```

(Route A: `py -3 claudeglass.pyz install-service`; Route B:
`.venv\Scripts\python.exe -m claudeglass update --finish`.) It stops the
running dashboard, re-registers the task for this install and starts it
again. On Linux it restarts the service too; on macOS run
`launchctl kickstart -k gui/$(id -u)/com.claudeglass` instead.
The status line at the foot of the dashboard's sidebar shows the
version it is running. If a new
version reads transcripts differently, the dashboard re-reads them once
after the restart, so the first page load can be slow.

## Troubleshooting

| Symptom | Fix |
|---|---|
| WSL sessions missing from the dashboard | Run `init` again: it adds any WSL folder it finds. Or add it to `extra_projects_roots` in `config.toml` and run `install-service` to restart the dashboard. See [Using Claude Code in WSL too](#using-claude-code-in-wsl-too) |
| Dashboard still shows the old version after an update (see the foot of its sidebar) | Something else still holds port 8765: an older copy started by hand, from another Python install, or from Docker. [An old dashboard won't go away](#an-old-dashboard-wont-go-away) shows how to find and stop it; then run `update --finish` with the Python you updated (section 8), which on Windows offers to stop an older copy itself |
| Not sure setup worked | Run `claudeglass status`. It lists each part as done, off or needing attention, with the command that fixes it, and exits 1 only when something essential needs attention |
| pip stops with "Failed to write executable" and `[WinError 2] ... claudeglass.exe' -> '...claudeglass.exe.deleteme'` | pip couldn't create the `claudeglass.exe` launcher in your Python's `Scripts` folder: you can't write there, or antivirus blocked the new `.exe`. Nothing here needs that launcher. Install for your user instead (`py -3 -m pip install --user --force-reinstall ...`), or use Route A, which pip never touches |
| `claudeglass` not found | Use the full path to the venv's `Scripts\claudeglass.exe`, or `python -m claudeglass` (works regardless of `PATH`). The dashboard's own commands already use the form that runs on your machine; set `CLAUDEGLASS_COMMAND` where the service runs to pick another |
| http://127.0.0.1:8765 doesn't open | Run `python -m claudeglass serve` in a PowerShell window and leave it open; any error prints there. "Already in use by another serve" names the process that has the dashboard's database open: stop that one first |
| A banner says the dashboard is **not updating** or its **last scan failed** | The background scan has stopped or keeps failing, so figures are frozen at the time shown. Restart the dashboard: `python -m claudeglass install-service` (or stop and start `serve`) |
| Amounts are in dollars but you're on a plan | Run `python -m claudeglass init` again and answer `1` to "How do you pay for Claude Code?" |
| `capture status` or **Setup › Capture** says your organisation allows only the hooks it deploys, or that hooks are turned off | A managed policy (`allowManagedHooksOnly` or `disableAllHooks`), or `disableAllHooks` in your own settings.json, stops Claude Code running any hook you add yourself. So capture, the config-snapshot hook and the status line can't run. Reports and the dashboard still work from your transcripts. Only your administrator can lift a managed policy |
| The Data quality page says the SessionStart hook isn't running | The hook command names a Python that isn't installed (`py` with no launcher), uses `%USERPROFILE%` (Claude Code runs hooks through Git Bash, which doesn't expand it), or has a path broken by single backslashes in JSON. Run `claudeglass init --repair-hook`: it shows the fixed command and changes it without asking, after copying `settings.json` to `settings.json.bak-<UTC time>`. It keeps your own Python when it's found and writes any `%VARIABLE%` out in full; otherwise it names your main Python install by full path. It can only fix a command whose script exists: if the script is missing, run `claudeglass init --connect` first, which copies it back into `<config-dir>\hooks\` |
| No usage-limit readings | The statusline runs only in Claude Code in a terminal, not in the desktop app or an IDE. Amounts stay list-price equivalents until readings arrive |
| `py` launcher missing (`'py' is not recognized`) | Use `python`/`python3` directly, or reinstall Python from python.org with "py launcher" checked |
| Python 3.10 or older | `pip install` refuses (`Requires-Python`); the `.pyz` fails at import with a `tomllib`-related error. Install 3.11+ (a user-level install needs no admin rights) |
| Execution policy blocks a `.ps1` script | `install-service`/`init` never need this — they shell out via `powershell.exe -ExecutionPolicy Bypass -Command ...` themselves. Only affects the legacy `scripts\windows\Register-ClaudeGlassTask.ps1` path; run it the same way: `powershell -ExecutionPolicy Bypass -File scripts\windows\Register-ClaudeGlassTask.ps1` |
| Corporate proxy blocks `pip`/PyPI/GitHub | Use Route A (`.pyz`) — no network access needed once downloaded |
| `CLAUDE_CONFIG_DIR` already set (for Claude Code itself) | Harmless — `claudeglass` reads it too and keeps its own files in `<CLAUDE_CONFIG_DIR>\claudeglass`. The connect step, `--repair-hook`, `apply`, `changes`, `uninstall` and the dashboard all use `<CLAUDE_CONFIG_DIR>\settings.json`; the ones that change it show the change first. `--config-dir` moves only this tool's own folder: `settings.json` is never looked for beside it, and the hook and statusline commands `init` adds then carry the same `--config-dir`, so snapshots and the usage log land where the dashboard reads them. Pass `--claude-root` to name Claude Code's folder yourself |
| Port 8765 already in use | `claudeglass serve --port <other>`, or `claudeglass install-service --port <other>` for the logon task (`init`'s service step always uses 8765). The dashboard and `/api/health` URLs change to match |

### An old dashboard won't go away

Only one program can use port 8765. If an old copy holds it, the new one
can't start, and your browser keeps showing the old one. See what is
using the port:

```powershell
Get-NetTCPConnection -LocalPort 8765 -State Listen | ForEach-Object { Get-Process -Id $_.OwningProcess } | Format-Table Id, ProcessName, Path
```

- **`python`, `pythonw` or `py`:** an old copy. Stop it, then start the
  new one:

  ```powershell
  Get-NetTCPConnection -LocalPort 8765 -State Listen | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
  python -m claudeglass install-service
  ```

- **Anything with `docker` in its name:** the old Docker setup. Run
  `docker compose down` in the folder you started it from, or stop the
  container in Docker Desktop. Then run
  `python -m claudeglass install-service`.

Then reload http://127.0.0.1:8765 and check the version at the foot of
the sidebar.

### An update doesn't take (more than one Python)

`update` and `pip install` change only the Python you run them with. If
the dashboard was set up from a different Python, it keeps running the
old copy. Run the update with the Python you want to keep; the one
`python` finds is the easiest. Use a normal PowerShell window, not
Administrator; nothing here needs it:

```powershell
python -m claudeglass update
```

It points the logon task and the status line at this Python, fixes hook
entries that name a Python that no longer exists, and offers to remove
the copies for other Pythons. Your settings and history live in
`%USERPROFILE%\.claude\claudeglass`, which every copy shares, so nothing
is lost.

To see which Python the dashboard runs, and which one `python` is:

```powershell
(Get-ScheduledTask ClaudeGlass).Actions | Format-List Execute, Arguments
(Get-Command python).Source
```

## Using Claude Code in WSL too

If you also run Claude Code inside WSL (Ubuntu on Windows), its sessions
are kept inside Linux, in
`\\wsl.localhost\<distro>\home\<you>\.claude\projects`. Install
ClaudeGlass on **Windows**, not inside WSL, and run
`python -m claudeglass init`. It finds those folders itself and
includes them, naming each distro when it starts. The dashboard then
shows your Windows and WSL sessions together. A **Where** column on
**Spend › Sessions** says which is which ("This computer" or
"WSL: Ubuntu").

- It only reads those folders, the same as your Windows one. It changes
  nothing inside WSL.
- While the dashboard runs, it looks in them every 30 seconds, which
  keeps WSL running in the background. If WSL is shut down, the
  dashboard carries on with what it already has. It picks up the rest
  when WSL is back.
- The "Connect to Claude Code" hook is for Claude Code on Windows. The
  copy of Claude Code inside WSL has its own settings and doesn't need
  it.
- Added a WSL distro later? Run `init` again. To list the folders by
  hand, put them in `config.toml` (in `%USERPROFILE%\.claude\claudeglass`)
  and restart the dashboard with
  `python -m claudeglass install-service`:

  ```toml
  extra_projects_roots = ['\\wsl.localhost\Ubuntu\home\alice\.claude\projects']
  ```

  A one-off command can take several folders too:
  `python -m claudeglass report --all-projects --projects-root <folder> --projects-root <another>`.

## POSIX (Linux/macOS) quick variant

Everything above works the same way; the differences are the launcher
name and default paths.

```bash
python3 --version                       # needs 3.11+

# Route A: .pyz
python3 claudeglass.pyz --version
python3 claudeglass.pyz init

# Route B: pip from a local clone
python3 -m venv .venv
.venv/bin/pip install <path-to-the-cloned-repo>
.venv/bin/claudeglass --version    # or: .venv/bin/python -m claudeglass --version

# Route C: pip from GitHub
python3 -m pip install git+https://github.com/PaulMorrisDev/claudeglass
python3 -m pip install https://github.com/PaulMorrisDev/claudeglass/archive/refs/heads/main.zip

python3 -m claudeglass init
python3 -m claudeglass install-service --dry-run   # prints the systemd user unit / LaunchAgent plan; writes nothing
curl http://127.0.0.1:8765/api/health
python3 -m claudeglass report
python3 -m claudeglass uninstall --revert-changes --delete-data --dry-run   # look first, then run without --dry-run
```

Config defaults to `~/.claude/claudeglass`/`~/.claude/projects`
(or `$CLAUDE_CONFIG_DIR/claudeglass`/`.../projects`). See
[`docs/deploy.md`](deploy.md) for what `install-service` actually
registers on Linux (`systemctl --user`) and macOS (`launchctl`), and
[`docs/onboarding.md`](onboarding.md) for the full `init`/`baseline`
question set and report shape.

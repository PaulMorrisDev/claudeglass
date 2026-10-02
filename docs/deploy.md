# Deploying the service

First time on this machine? [`docs/first-run.md`](first-run.md) is the
short, numbered walkthrough (install, `init`, confirm the service
actually registered, uninstall); this document is the full reference
for every hosting path it links to.

`claudeglass serve` (`docs/api.md`) needs to run *somewhere*
continuously to keep its store fresh and its JSON API/web UI
(`docs/ui.md`) reachable. This document covers the three supported
hosting paths, in the order the project's plan prioritises them: a
native OS-level service first (no container runtime, no admin/root
rights), then Docker for anyone who'd rather manage it that way.

All three run the exact same `claudeglass serve` command
underneath; they differ only in how that process is started, kept
running, and sandboxed by the host.

## The installer: `install-service`/`uninstall-service`

`src/claudeglass/installer.py` (v3) drives Path 1 and Path 2
below from Python, so `claudeglass init`'s service step, and the
standalone `install-service` subcommand, don't require you to copy a
script or a unit file by hand. It exists for one reason: Claude Code
deletes a project's own transcripts after `cleanupPeriodDays`, and this
project's store is only ever fed by a *running* `serve` watcher — a
service that only starts when someone remembers to run it manually
loses that history the moment the underlying JSONL files are cleaned
up. Only a continuously running, logon-registered service actually
keeps it.

```bash
python -m claudeglass install-service                     # register for this platform
python -m claudeglass install-service --dry-run           # print the plan only — writes/runs nothing
python -m claudeglass install-service --port 9000 --bind 127.0.0.1
python -m claudeglass uninstall-service                   # remove whatever was registered
python -m claudeglass uninstall-service --dry-run
```

**Safety posture** (see the module's own docstring for the full
rationale): building a plan (`plan_service_install`) never has a side
effect — no file write, no `subprocess` call — which is what makes
`--dry-run` and this module's entire test suite possible without ever
touching a real Scheduled Task, systemd unit or LaunchAgent. Every
side-effecting call goes through an injected `runner` (default
`subprocess.run`), and `dry_run=True` always means "print exactly what
would happen, then stop" on every platform. `install`/`uninstall` print
every file they are about to write and every command they are about to
run *before* doing either.

**What each platform's plan actually does:**

- **Windows:** builds and runs one `powershell.exe -NoProfile
  -ExecutionPolicy Bypass -Command` script that chains
  `New-ScheduledTaskAction`, `New-ScheduledTaskTrigger -AtLogOn`
  (scoped to `$env:USERDOMAIN\$env:USERNAME`),
  `New-ScheduledTaskPrincipal` (`-RunLevel Limited` — no admin rights),
  `New-ScheduledTaskSettingsSet` (runs on battery, restarts up to 3
  times a minute apart, `-ExecutionTimeLimit ([TimeSpan]::Zero)` since
  `serve` runs indefinitely) and `Register-ScheduledTask -TaskName
  ClaudeGlass -Force`. The action runs `pythonw.exe` beside the
  running interpreter when it exists (no console window at logon), else
  `python.exe`. Writes no file of its own — the task definition lives
  entirely in Task Scheduler's own store. The same command
  first stops a copy the task already started and ends with
  `Start-ScheduledTask`, so the dashboard starts straight away, and
  re-running `install-service` after an update switches it to the new
  code (`update` does both steps). An update that lands without either
  (an editable install after a pull) is caught by `serve
  --exit-on-code-change`, which every platform's plan registers; see
  "Updating under a running `serve`" below. Folders in `config.toml`'s
  `extra_projects_roots`, such as a WSL distro's, are not written into
  the task: `serve` reads them each time it starts. This is the same task
  Path 1 below registers by hand, with one difference: there is no
  `schtasks /create` fallback. `uninstall-service` first runs
  `Stop-ScheduledTask -TaskName ClaudeGlass`, which shuts down a
  dashboard the task already started, then `Unregister-ScheduledTask
  -TaskName ClaudeGlass`. It doesn't stop a `serve` you started by
  hand in a terminal; `Unregister-ClaudeGlassTask.ps1` does.
- **Linux:** writes `~/.config/systemd/user/claudeglass.service`
  (the same hardening as `scripts/systemd/claudeglass.service` —
  see Path 2 below — but with `ExecStart`/`ReadWritePaths` filled in
  with this call's real, absolute `config_dir` rather than `%h`), then
  runs `systemctl --user daemon-reload`, `systemctl --user enable
  --now claudeglass.service` and `systemctl --user restart
  claudeglass.service` (so re-running it after an update runs the
  new code). Prints a note to also run
  `loginctl enable-linger $USER` once, for a headless server with no
  interactive session. `uninstall-service` runs `systemctl --user
  disable --now`, which stops the running service as well as disabling
  it, and deletes the unit file.
- **macOS:** writes `~/Library/LaunchAgents/com.claudeglass.plist`
  (`RunAtLoad`/`KeepAlive` both true) and runs `launchctl bootstrap
  gui/<uid> <path-to-plist>`. `uninstall-service` runs `launchctl
  bootout gui/<uid>/com.claudeglass`, which stops the running
  agent as well as unloading it, and deletes the plist.

**Running from a `.pyz`:** if the current process was itself launched
from a `.pyz` archive (`detect_pyz_path`, a real zip-file check on
`sys.argv[0]`, not just a filename check), the registered action
re-invokes that same archive (`pythonw.exe <path-to-pyz> serve ...`)
instead of `python -m claudeglass serve ...` — so `install-service`
run from a `dist/claudeglass.pyz` build (see "Distribution
without pip" below) registers a service that keeps using that exact
archive.

**Checking registration:** `is_registered()` runs the platform's own
query command (`schtasks /Query /TN ClaudeGlass`, `systemctl --user
is-enabled claudeglass`, or `launchctl print
gui/<uid>/com.claudeglass`) and returns `True`/`False` when it
got a clear answer, or `None` when the probe itself couldn't run (an
unsupported platform, the query tool missing, or a timeout) — `None`
always means "unknown", never "not registered". `install-service`/
`init` call this once, a short delay after a successful install, and
also do a best-effort `GET /api/health` against the newly-registered
port to report whether the service is already answering. `GET
/api/health` itself exposes the same probe as `service_registered:
true|false|null`, cached for ten minutes per running `serve` process so
routine polling doesn't shell out on every request (see
[`docs/api.md`](api.md)) — the dashboard's Overview and Data quality
pages show a warning when it comes back `false`.

**Uninstalling:** `claudeglass uninstall-service` is the
inverse of `install-service` — it runs the platform's own removal
command (`Stop-ScheduledTask` then `Unregister-ScheduledTask`,
`systemctl --user disable --now`, or `launchctl bootout`) and deletes
any file `install-service` wrote (the systemd unit or the LaunchAgent
plist; Windows writes no file of its own). Each platform's first
command also stops a dashboard the service is running, and the output
says what was done, one line per step ("Stopped Scheduled Task
'ClaudeGlass' ...", "Removed Scheduled Task ..."). It is
best-effort past the printed plan: a command or file removal that
fails is reported and the rest still runs, rather than aborting
partway through, the same posture as
`Unregister-ClaudeGlassTask.ps1`/`serve --purge`. To remove everything
else this tool added as well (the hook, the statusline, applied changes
and the data folder), use `claudeglass uninstall` — see
[`docs/first-run.md`](first-run.md#7-undo-a-change-or-uninstall-completely).

Nothing above replaces the hand-run paths below — `install-service`
deliberately mirrors their exact flags/hardening choices rather than
inventing new ones, and Path 1/Path 2 remain the copy-pasteable
mechanism for anyone who'd rather run (or audit) the commands
themselves. `install-service` has no equivalent for Docker (Path 3) —
use Compose's own restart policy there.

## Path 1: Windows Scheduled Task (native, no admin rights)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\Register-ClaudeGlassTask.ps1
```

Registers a Scheduled Task, triggered at logon, running:

```
pythonw -m claudeglass serve --projects-root "$env:USERPROFILE\.claude\projects" --config-dir "$env:USERPROFILE\.claude\claudeglass" --exit-on-code-change
```

- **Runs as the logged-in user, `-RunLevel Limited`** — no admin
  rights requested or required. The service only ever reads
  `%USERPROFILE%\.claude\projects` and reads/writes
  `%USERPROFILE%\.claude\claudeglass`; nothing it does needs elevation.
  The logon trigger itself is also scoped to that one account (via
  `-User "DOMAIN\user"` on `New-ScheduledTaskTrigger`, and `/RU`/`/IT`
  on the `schtasks` fallback) — an unscoped "any user logs on"
  trigger is treated as machine-wide and Task Scheduler refuses to
  register it without admin rights, even though the task's own
  principal is already limited to this account.
- **`pythonw`, not `python`** — no console window appears at logon.
- **No execution time limit** — the settings pass
  `-ExecutionTimeLimit ([TimeSpan]::Zero)`, since `serve` is meant to
  run indefinitely and Task Scheduler's own default (72 hours) would
  otherwise kill it after three days.
- **Falls back to `schtasks /create`** automatically if the
  `ScheduledTasks` PowerShell module is unavailable (some locked-down
  corporate images restrict it even for non-admin users).
- `-BillingMode {api,subscription}` forwards `--billing-mode` (see
  `docs/api.md`'s CLI flags section) if you want it set at
  registration time rather than via `config.toml`.
- **`--exit-on-code-change`** starts the task again on new code after
  an update lands without a restart (see "Updating under a running
  `serve`" below). That only happens under the default `-TaskName
  ClaudeGlass`; under another name the dashboard only reports the
  change.

To remove it and stop any running instance:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\Unregister-ClaudeGlassTask.ps1
```

This unregisters the task (by whichever mechanism registered it) and
searches for any `python.exe`/`pythonw.exe` process whose command line
invokes `claudeglass` via `Get-CimInstance Win32_Process`,
stopping it — a Scheduled Task's own action process carries no other
marker to find it by.

**What this path can/cannot touch:** everything runs as your own
Windows user account. It can read/write anything you can. It never
requests elevation, never installs a Windows service (a Scheduled Task
is not a Windows Service — no `services.msc` entry, no SYSTEM
account), and never touches the registry beyond the Task Scheduler's
own task definition store.

## Path 2: systemd user unit (native, POSIX)

```bash
mkdir -p ~/.config/systemd/user
cp scripts/systemd/claudeglass.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now claudeglass.service
```

Runs `~/.local/bin/claudeglass serve --projects-root
~/.claude/projects --config-dir ~/.claude/claudeglass
--exit-on-code-change` as your own user, restarting on failure
(`Restart=on-failure`), which also covers `serve` exiting with status
`3` after an update (see "Updating under a running `serve`" below).
That `ExecStart` path
assumes a `pip install --user`; edit it if `claudeglass` lives
elsewhere (`command -v claudeglass`), or use `install-service`,
which fills in the real interpreter for you.

**What this path can/cannot touch:**

- **`ProtectHome=read-only`** makes your entire home directory
  read-only to the unit by default.
- **`ReadWritePaths=%h/.claude/claudeglass`** carves out the one
  exception: the service's own SQLite store, config, and snapshots
  directory. It cannot write anywhere else under your home directory,
  even though it can *read* `~/.claude/projects` for transcripts (a
  read-only-by-default home directory still permits reads; only writes
  are blocked outside `ReadWritePaths`).
- **`ProtectSystem=strict`**, **`NoNewPrivileges=true`**,
  **`PrivateTmp=true`** — standard systemd sandboxing beyond the
  home-directory restriction above.
- **`PrivateNetwork`** is deliberately left at its default (network
  access available) rather than set to `yes` — unlike the Docker image
  below, this unit runs directly on the host and needs its bound port
  reachable the ordinary way. The "never opens an outbound connection"
  guarantee (`docs/api.md`'s "Local only" section) is enforced by the
  service's own code (`tests/test_service_egress.py`), not by a network
  namespace, for this path.

**Logs:** `journalctl --user -u claudeglass.service -f`.

**Surviving logout** (e.g. a headless server with no interactive
session): `loginctl enable-linger $USER` once, so the user unit keeps
running after you log out.

**Stopping it:** `systemctl --user disable --now claudeglass.service`.

## Path 3: Docker

```bash
docker compose up -d
```

Builds the image from the repository's own `Dockerfile` (deliverable
2.a) and starts it per `docker-compose.yml` (deliverable 2.b). Set
`CLAUDE_HOME` first if the default (`~/.claude`) isn't where your
Claude Code config lives, or if you want to be certain what path is
actually used regardless of Compose version — see the comment in
`docker-compose.yml` on `~`-expansion, and `docker compose config` to
check what it resolved to before `up`:

```bash
export CLAUDE_HOME=/home/you/.claude   # or set it in a .env file next to docker-compose.yml
docker compose up -d
```

Windows (Docker Desktop) example: `CLAUDE_HOME=C:/Users/<you>/.claude`.

**What this path can/cannot touch:**

- **Read-only bind mount** of your Claude Code config directory to
  `/data/claude` inside the container (`:ro` — the container cannot
  write to it, even if a future bug tried).
- **A named volume** (`claudeglass-data`) for the service's own SQLite
  store and any monthly reports — isolated from the host filesystem
  entirely; nothing outside the container can read it directly.
- **`127.0.0.1:8765:8765`** — the published port is loopback-only on
  the host, regardless of the container's own `--bind 0.0.0.0` (see the
  Dockerfile's comment: a process bound to `127.0.0.1` *inside* a
  container is unreachable from outside it too, so the container itself
  must bind `0.0.0.0` for port publishing to work at all — exposure
  still stays loopback-only from the host's perspective, enforced by
  the `127.0.0.1:` prefix on the port mapping, not by the container's
  own bind address).
- **`read_only: true`** root filesystem with a `tmpfs` `/tmp`, **`cap_drop:
  [ALL]`**, **`no-new-privileges:true`** — a compromised process inside
  the container can neither escalate privileges nor write anywhere but
  the two mounts above and `/tmp`.
- **`network_mode: bridge`**, explicitly — not `none`. `none` would
  also block the one connection this service is supposed to accept
  (the published port); see the next section for how to actually prove
  the service needs no *outbound* connectivity without disabling the
  inbound one it does need.

## Verifying no egress

Three layers, from "always runs" to "manual, occasional":

1. **`tests/test_service_egress.py`** — runs on every `pytest`
   invocation, monkeypatches `socket.socket.connect` for the lifetime
   of a real test server, and asserts every recorded connection target
   is the test client's own loopback address. This is the primary,
   always-on guarantee.
2. **`tests/test_service_docker.py`** — line-based checks that the
   Dockerfile/docker-compose.yml hardening (non-root user, no
   curl/wget/apt-get, `read_only`, `cap_drop`, loopback-only port)
   hasn't silently regressed.
3. **Manual Docker smoke test** — proves the *built image*, not just
   the Python code, needs no outbound connectivity, by removing network
   access from the container entirely and confirming the service still
   answers:

   ```bash
   docker build -t claudeglass:smoke .
   docker run -d --rm --network none --name ctl-smoke \
     -v "$CLAUDE_HOME:/data/claude:ro" \
     -v ctl-smoke-data:/data/claudeglass \
     claudeglass:smoke \
     --projects-root /data/claude/projects --config-dir /data/claudeglass \
     --bind 0.0.0.0 --allow-remote

   # From inside the container -- there is no host-published port to
   # curl from outside when --network none is used, so the check runs
   # where the service actually is:
   docker exec ctl-smoke python -c \
     "import json,urllib.request as u; r=u.urlopen('http://127.0.0.1:8765/api/health', timeout=4); assert json.load(r)['ok'] is True; print('OK')"

   # /api/health alone only proves the process started -- it says
   # nothing about whether $CLAUDE_HOME's read-only mount was actually
   # reachable. Confirm /api/summary sees a non-zero transcript count
   # (skip this check if $CLAUDE_HOME has no Claude Code projects yet):
   docker exec ctl-smoke python -c \
     "import json,urllib.request as u; r=u.urlopen('http://127.0.0.1:8765/api/summary', timeout=4); data=json.load(r)['data']; assert data['transcripts'] > 0, data; print('transcripts:', data['transcripts'])"

   docker stop ctl-smoke
   docker volume rm ctl-smoke-data
   ```

   `--projects-root`/`--config-dir` are passed explicitly here (review
   finding 4) rather than left to `claudeglass serve`'s own
   argparse defaults: any bare `docker run <image> <args>` replaces the
   image's `CMD` entirely (the fixed `ENTRYPOINT` in the `Dockerfile`
   only supplies `claudeglass serve`), so omitting them would
   silently fall back to a `~`-relative default inside the container
   instead of the `/data/claude`/`/data/claudeglass` mount points this
   image and `docker-compose.yml` are actually built around. The named
   `ctl-smoke-data` volume in particular is what proves the Dockerfile's
   `chown -R claudeglass:claudeglass /data/claudeglass` (finding 4) is
   doing its job: a *fresh* named volume is seeded from that path's
   ownership in the image, so the non-root `claudeglass` user can create
   `service.db` in it on first start without a manual `docker exec ...
   chown` step.

   A `--network none` container has no network namespace connectivity
   at all beyond loopback — if `/api/health` still answers `ok: true`
   from inside it, the service provably made no outbound connection to
   get there. This has been run against this repository's own
   `Dockerfile`/`docker-compose.yml` (Docker 29.7.2) as part of
   S1-integration: build succeeds, a `--once` tick and a full `serve`
   both complete under `--network none`, and `/api/health` answers
   correctly from inside the container.

## Distribution without pip: the `.pyz` build

For a machine where `pip install` is unavailable or unwanted (no
internet access to PyPI, a locked-down environment, or just "copy one
file and run it"), `claudeglass` has zero runtime Python
dependencies (`pyproject.toml`'s `dependencies = []`), which makes a
single-file [zipapp](https://docs.python.org/3/library/zipapp.html)
distribution straightforward:

```bash
python scripts/build-pyz.py
# -> dist/claudeglass.pyz

python dist/claudeglass.pyz --version
python dist/claudeglass.pyz serve --projects-root ~/.claude/projects --config-dir ~/.claude/claudeglass
```

Equivalent, if you'd rather invoke `zipapp` yourself directly, to:

```bash
python -m zipapp src -m "claudeglass.__main__:main" -o dist/claudeglass.pyz -p "/usr/bin/env python3"
```

`scripts/build-pyz.py` does the same thing (via the `zipapp` module's
Python API rather than shelling out), plus: copies `src/claudeglass/`
into a clean temporary directory first, skipping `__pycache__`, so a
stray compiled-bytecode cache from your own dev environment never ends
up inside the shipped archive; and includes `service/static/*` (the web
UI) automatically, since it's just an ordinary file tree already living
under `src/claudeglass/service/static/` — no separate packaging
step needed.

**Why `claudeglass.__main__:main`, not `claudeglass.cli:main`:**
zipapp's generated archive-root `__main__.py` (from the `-m`/`main=`
argument) is just `import <module>; <module>.<function>()` — it does
**not** wrap that call in `sys.exit(...)`, so a target function's
returned int exit code would otherwise be silently discarded and the
process would always exit 0. `claudeglass/__main__.py` (the
existing `python -m claudeglass` entry point) already solves this
for itself: its own top-level statement is `sys.exit(main())`, which
runs the instant `import claudeglass.__main__` executes and
raises `SystemExit` with the real code — propagating out through the
zipapp-generated wrapper's own `import` line before its `.main()` call
is ever reached. Pointing zipapp at `cli:main` instead would reproduce
exactly the bug `__main__.py`'s own docstring warns about.

`tests/test_service_build_pyz.py` builds the archive and actually runs
it (`subprocess`, real Python interpreter) to confirm `--version` works
and a non-zero exit code (a stub subcommand) survives the round trip —
verified locally as part of S1-integration.

## Retention and purge

A transcript file the watcher can no longer find on disk (removed by
Claude Code's own `cleanupPeriodDays`, or by hand) is never deleted from
the store on the spot — `Store.remove_missing` only marks its
`missing_since` timestamp (clearing it again if a file at the same path
reappears). `report.*`/the UI keep including it exactly like a
transcript still on disk (`GET /api/health`'s `transcripts_missing`
reports the current count; see [docs/api.md](api.md)). The service
store is deliberately designed to outlive Claude Code's own retention
window, not mirror it — the two options below are the *only* things
that actually delete a row.

- **`--retention-days N`** (existing `serve` flag): every watcher poll
  tick prunes sessions whose transcripts were all last active more than
  `N` days ago (`Store.retention_prune`) — this is what actually deletes
  a marked-missing (or still-present) transcript's row, not the
  missing-file check itself. Off by default: without the flag, `serve`
  uses `retention_days` from `config.toml`, and with neither set nothing
  in the store is ever pruned.

  Metrics capture's own signal files (`<config-dir>/signals/YYYY-MM.jsonl`)
  and `capture-log.jsonl` are different: they're this tool's own
  background telemetry, not report data, so every tick prunes them
  regardless of whether `--retention-days`/`config.toml` set anything —
  at `N` when one is set, or a 180-day default
  (`config.SIGNAL_RETENTION_DEFAULT_DAYS`) otherwise.
  `claudeglass capture prune [--dry-run]` runs the identical
  cleanup by hand for anyone not running `serve`.
- **`serve --purge`** (deliverable 2.e): deletes `<config-dir>/service.db`
  and its `-wal`/`-shm` sidecars, then exits — never starts the watcher
  or API. Always prints exactly which files it would delete first; only
  actually deletes them with `--yes`:

  ```bash
  python -m claudeglass serve --config-dir ~/.claude/claudeglass --purge
  # claudeglass serve --purge: will delete:
  #   /home/you/.claude/claudeglass/service.db
  # Re-run with --yes to actually delete these files.

  python -m claudeglass serve --config-dir ~/.claude/claudeglass --purge --yes
  # Deleted 1 file(s).
  ```

  Safe at any time: the store is always a derived cache, never source
  of truth (`service/store.py`'s module docstring) — the next `serve`
  run simply rebuilds it from the transcripts already on disk, the same
  way a downgrade's drop-and-rebuild (`Store.migrate()`, see "The store's
  schema version" below) does.

  If one of the files cannot be deleted (for example a `-wal` sidecar
  still held open by another process), `--purge` deletes everything it
  can, reports the failure(s) to stderr, and exits with status `1` —
  it never aborts partway through with an unhandled error. While a
  `serve` is running on the store, `--purge` refuses (status `1`) and
  names that process: stop it first.
- **One `serve` per store.** `serve` locks its database
  (`<store>.lock`, beside it) before opening it and holds the lock until
  it exits; the operating system drops it however the process ends, so
  a crash never leaves it stuck. A second `serve` (or `serve --once`)
  on the same store is refused with exit status `1`, naming the process
  that holds it and its address, instead of the two fighting over
  SQLite's write lock (the loser's scans fail with "database is locked"
  and its dashboard stops updating). A starting `serve` waits up to ten
  seconds for the lock, so `install-service` restarting the service
  hands over cleanly. To run a second copy beside the service (a dev
  checkout, say), give it its own database with `--store PATH`; it
  still shares `--config-dir`'s settings and parse cache.

## Updating under a running `serve`

`update` and `install-service` restart the service on the new code. An
update that lands any other way does not: with an editable install
(`pip install -e .`), `serve` runs straight from the checkout, so a
`git pull` or a release merged there changes the files under the
running process. The modules it already loaded stay old, and a module
it imports later, when a route first needs it, comes from the new
files. The two don't fit, and those routes fail with an `ImportError`.

`serve` checks for this after every watcher tick
(`service/codewatch.py`). It compares the package's modules and data
files by name, size and modification time, and hashes their contents
only when those moved, so the check costs a few milliseconds and a
checkout switched away and back is no change. Once the files differ
from the loaded code:

- `GET /api/health` reports `status: "outdated"` and a `code` block
  (see [docs/api.md](api.md#get-apihealth)), and the dashboard's banner
  says the code changed on disk and to run
  `python -m claudeglass install-service`.
- A route that fails to import answers `503 restart_needed`, saying to
  restart, instead of `500 internal_error`.
- With `serve --exit-on-code-change` (which `install-service` and both
  scripts register), `serve` exits with status `3` once the change has
  settled (one tick with no further writes, so a pull still in progress
  isn't caught half-way). The service then starts again on the new
  code:
  - **systemd:** `Restart=on-failure` restarts it five seconds later.
  - **launchd:** `KeepAlive` restarts it.
  - **Task Scheduler:** its restart setting only covers a task that
    fails to start, not one that exits with an error. Tested on
    Windows 11, a task whose action exited with status `3` or `-3` was
    not run again. So before exiting, `serve` starts a hidden
    PowerShell helper that waits for it to exit, then runs
    `Start-ScheduledTask -TaskName ClaudeGlass`. `Stop-ScheduledTask`
    and `uninstall-service` still stop it as before. When no
    `ClaudeGlass` task is registered (you started `serve` by hand,
    or registered it under another name), exiting would leave no
    dashboard at all, so `serve` stays up and only reports the change.

  Once it is back, an open dashboard tab offers to reload the page, so
  its scripts match the new code too.

A task registered before this flag existed doesn't have it: run
`install-service` once more to add it. Until then the banner still
says what to do.

## The store's schema version

`service.db` records the schema version it was written under
(`meta.schema_version`). Version 8, from 0.13.0, adds `turns_agg.bucket`:
the UTC quarter hour each group of replies falls in. It lets the
dashboard cut a window and its daily chart at local midnight in any time
zone, rather than at UTC's.

- **Upgrading from 7 keeps every row.** `Store.migrate()` adds the column
  and marks every stored transcript for one re-parse, the same step a
  rate-card change takes (see below), in one transaction that stamps the
  new version last. The watcher fills the buckets in over its next ticks
  and `watcher.files_reparsed_stale_parser` counts them. Until a
  transcript is read again, its rows have no bucket and count on the UTC
  day they were stored under, so a daily chart can place a late-evening
  reply a day off for that short time.
- **Downgrading rebuilds the store.** An older version pointed at a store
  a newer one migrated can't be served by an additive step, so `migrate()`
  copies `service.db` aside as `service.db.bak-<version>-<timestamp>`
  (and prints where on stderr), drops every table and recreates them. The
  next watcher tick reads the transcripts still on disk again. Your
  session tags and ratings are put back. Logged "what if" predictions are
  re-read from `prediction-log.jsonl` and come back unjudged, so their
  verdicts and seen marks are lost until the dashboard judges them again.
  Transcripts Claude Code has already removed are not rebuilt; they stay
  in the backup file.

## Time zone and `tzdata`

Days, the Today window and the 7, 30 and 90 day windows are counted from
midnight in `config.toml`'s `tz`, else the machine's own zone. A named
zone needs the IANA zone database. Linux and macOS have it. A bare
Windows Python does not, so there a name such as `America/New_York`
can't be found and the machine's zone is used instead, with no error.
Run `pip install tzdata` to make the name work (`UTC` needs nothing).
`GET /api/summary`'s `period.tz` says which zone the days were counted
in: `null` means the machine's own.

## Re-parsing after a parser upgrade

Each watcher tick decides whether to re-parse a transcript from its
`(mtime_ns, size_bytes)` against `Store.known_files()`, but a file that
hasn't changed on disk can still be *stale* relative to the code: every
stored transcript also carries the `parser_version` it was parsed
under (`claudeglass.PARSER_VERSION`, bumped whenever a code
release adds or changes what the parser derives from a transcript —
see `CHANGELOG.md`'s "Fixed"/"Changed" entries for the version
history). A file whose `(mtime_ns, size_bytes)` are unchanged but whose
stored `parser_version` predates the one now running is re-parsed on
the very next tick regardless, so a parser upgrade actually reaches
every already-stored transcript rather than only the ones that happen
to change again afterwards. This reuses the on-disk digest cache under
`<config-dir>/cache/` (already keyed on `parser_version` — see
`cache.py`), so the rebuild costs one cold-ish tick per transcript on
the tick right after an upgrade, then nothing on every later tick.
`GET /api/health`'s `watcher.files_reparsed_stale_parser` (see
[docs/api.md](api.md)) counts how many transcripts this tick re-parsed
for exactly this reason, so an operator can watch the post-upgrade
rebuild happen rather than having to infer it from a slower-than-usual
tick.

## Re-pricing after a rate-card change

The watcher prices replies with the same rate card the dashboard reads:
the file `config.toml`'s `pricing_path` names, else
`<config-dir>/pricing.toml`, else the packaged card. It checks every
tick, so setting `pricing_path`, or creating, editing or removing
`pricing.toml`, takes effect on the next tick without a restart. Saving
the same card again changes nothing.

When the card's contents change (its sha256 differs from the one the
store recorded), the watcher marks every stored transcript as stale and
re-parses each one it finds on disk, the same way as after a parser
upgrade above. That rebuilds the stored daily spend, session totals,
compactions and workflow run costs at the new rates. The digest cache
keeps it cheap, and `watcher.files_reparsed_stale_parser` counts it.
The mark is kept in the store, so a projects folder that is out of reach
during that tick (a WSL distro that is shut down) is re-priced once it
is back, and a restart part-way through loses nothing. Rows a newer
version wrote are left alone, as above. Upgrading to the first version
with this check re-prices the store once, since it has no card recorded
yet.

- **Transcripts no longer on disk keep their old cost.** Once Claude
  Code has removed a file, there is nothing to re-parse, so its stored
  totals (`/api/summary`, `/api/daily-usage`, `/api/sessions`) stay at
  the rates in force when it was last read. `/api/report.*` prices each
  request from the stored transcripts with the current card, so for
  those sessions the two can differ.
- **A card that can't be read keeps the previous one.** A half-saved
  edit or a `pricing_path` that has gone missing never prices anything
  at zero: the watcher carries on with the card it had, and each tick's
  `watcher.error_messages` says "rate card could not be read; kept the
  previous one" until it can be read again. If the card is already
  unreadable when `serve` starts, there is no previous card, so the
  watcher prices with `<config-dir>/pricing.toml` (skipping
  `pricing_path`), else the packaged card. It re-prices the store at
  that card's rates if they differ, says "rate card could not be read;
  using the default one until it can" each tick, and re-prices again
  once yours can be read.

## Performance

S1-perf measured and fixed `serve`'s worst case: a brand-new install's
very first watcher tick over a large, already-existing `~/.claude/projects`
corpus (every prior work package's benchmarks used small synthetic
fixtures or a warm re-run, neither of which this cold-start path looks
like). Measured against a real corpus (1858 transcript files, 1.6 GB on
disk, 135 sessions, one consumer-grade multi-core machine, no other
significant load) on a checkout at each step:

| Step | First tick (cold) | Second tick (warm) | `service.db` size |
|---|---|---|---|
| Baseline (pre-S1-perf) | 43.3 s | 3.9 s | 210.0 MB |
| + item 2 (parallel parse, shared digest cache) | 51.5 s* | 4.1 s | 210.3 MB |
| + items 3–4 (write batching, `events_agg`, `digest_blob`) | **30.5 s** | 3.6 s | **22.8 MB** |

\* Item 2 alone parallelises the CPU-bound parse phase (`parse_s`
23.0 s including pool start-up, down from parsing being folded into an
undifferentiated serial total) but does nothing about write cost —
its own `store_s` (20.4 s, unbatched per-row `execute()` calls) was
untouched and dominates, so the tick's wall-clock total was briefly
*worse* than baseline until items 3–4 landed. Reported here for an
honest step-by-step record, not as a regression left in the shipped
code — the four steps landed together on this branch.

Both of this work package's targets are met on this corpus: first tick
under 60 s (30.5 s, a 30% cut from baseline) and `service.db` under
80 MB (22.8 MB, a 9x cut). The dominant remaining cold-tick cost is
`parse_s` (~21 s: JSONL parsing itself, now parallelised up to 4
workers) — further gains there would mean parsing faster per file, not
scheduling the same work differently.

Per-tick timing is visible at runtime via `GET /api/health`'s
`watcher.discovery_s`/`parse_s`/`store_s` (see [docs/api.md](api.md)),
so a slow tick's dominant phase on your own corpus/hardware doesn't
have to be guessed at.

Re-verified during v0.2.0 release sign-off, two consecutive
`serve --once` runs against a different real corpus: first tick
(cold) `duration_s=30.14` (`discovery_s=0.49`, `parse_s=20.58`,
`store_s=1.81`), second tick (warm, no changed files) `duration_s=3.56`
(`discovery_s=0.64`, `parse_s=0.00`, `store_s=0.14`), `service.db`
22.8 MB — consistent with the S1-perf table above.

Storage-shape changes behind this: `transcripts.digest_blob` (was
`digest_json`) stores the same per-transcript JSON zlib-compressed;
`events_agg` (was `events`) stores one row per `(transcript_id, kind,
subkind)` with `count`/`dropped_tokens_sum`/`duration_ms_sum` instead
of one row per raw event — a real corpus's `events` table held 231k+
rows behind zero readers anywhere in this codebase. Neither change
alters any `/api/*` response body — `tests/test_service_rebuild.py`'s
`test_api_backing_bodies_survive_a_full_store_rebuild` asserts this by
serialising every Store-backed route's body through a full
drop-and-rebuild cycle and diffing the JSON.

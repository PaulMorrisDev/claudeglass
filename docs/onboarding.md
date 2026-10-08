# Onboarding: `init` and `baseline`

Never run `init` on this machine before? [`docs/first-run.md`](first-run.md)
is the short walkthrough, including what each question below actually
means in one line. This document is the full reference.

`claudeglass init` (`src/claudeglass/setup_flow.py`, asking
the questions in `src/claudeglass/onboarding.py`) and
`claudeglass baseline` (`src/claudeglass/baseline.py`) are
the onboarding pair: `init` asks a few questions this codebase genuinely
cannot infer on its own, shows every change it will make, makes them
after one yes, and ends with a summary of what works; `baseline` turns
whatever corpus has accumulated since then into a JSON record plus a
four-section Markdown report, citing only numbers the report itself
already computed.

Neither `onboarding.py` nor `baseline.py` touches `profiles/apply.py`,
the `service/` package, or SQLite — a baseline is one JSON file under
`<config_dir>/baselines/<id>.json` (plus a sibling `<id>.md`), the same
"plain files under the config dir" posture `config.py` already uses for
`config.toml`/`sessions.toml`. `onboarding.py` installs nothing, and
never writes `settings.json` except to repair a broken hook command you
agreed to fix (step 3). The changes outside `<config_dir>` — connecting
to Claude Code, the `/cg-feedback` skill and the logon task — are
`setup_flow.py`'s, made only after the review. The commands they write
come in from `cli.py` (`_setup_flow_inputs`), so neither module imports
`cli.py`.

## `init`

In short, `init` asks four things:

1. how you pay: `1` a plan (Pro, Max, Team or Enterprise), with amounts
   shown as a share of your usage limits, or `2` an API key, with
   amounts in dollars;
2. whether to connect to Claude Code: a SessionStart hook in
   `settings.json` that records which settings each session ran with,
   plus a status line if you have none;
3. whether to start the dashboard when you log on;
4. optionally, whether to turn on sharper tips: metrics capture at
   Essentials for 14 days, plus the `/cg-feedback` skill.

Then it lists every change in one review, `Ready to set up:`, and asks
`Go ahead? (d shows the exact changes) [Y/n/d]`. `d` prints the
`settings.json` diff, the skill's file and the logon task's commands,
then asks again; `n` writes nothing. After a yes it makes the changes
and prints `Your setup`: the same checklist `claudeglass status`
prints, and what to open next.

`init --advanced` also asks the rest: projects to leave out, extra
settings files, a shared `.claude` folder, the time zone, where `apply`
writes, the capture window and any WSL folders. It asks the full metrics
capture and feedback questions in place of question 4.

Running `init` again is safe: your saved answers are the defaults, what
is already done is skipped (an existing connection, a dashboard that
answers), and the capture window you are part-way through carries on.
`--dry-run` shows the review and every exact change, and writes nothing.
For scripts or CI, run
`python -m claudeglass init --non-interactive --no-install`. It
asks nothing and needs no yes, works out the unanswered questions from
what it found, and prints what it chose and why.

The logon service matters because Claude Code deletes its own
transcripts after `cleanupPeriodDays` (30 days by default). Only a
dashboard that is running keeps their figures.
[`deploy.md`](deploy.md) describes what it registers on each system.

The full sequence, step by step:

```
python -m claudeglass init [--advanced] [--answers FILE] [--non-interactive] [--no-install]
                                  [--repair-hook] [--connect]
                                  [--install-service | --no-service] [--dry-run]
                                  [--capture-level LEVEL] [--capture-for DURATION | --capture-no-limit]
                                  [--feedback {on,off}]
```

1. **Detect** what's already on the machine (`onboarding.detect` ->
   `Detection`), after printing `Looking for Claude Code history...`
   (looking inside WSL can take a few seconds): whether `<config_dir>`
   exists yet, the billing saved in `config.toml`, how many config
   snapshots and whether a usage-log CSV are already on file, how many
   projects are discoverable under the projects root, WSL folders
   `config.toml` doesn't list yet, and whether the SessionStart hook is
   set up (`hook_health.check`).
2. **Print the header**: `ClaudeGlass setup`, that everything stays on
   this computer, and how many projects have Claude Code history,
   naming any WSL distro found. Only counts and distro names are
   printed, never a path or session content.
3. **Offer a hook repair**, only when `settings.json` has a SessionStart
   hook command for `snapshot-config.py` that can't run (a path broken
   by JSON escaping, a missing interpreter, or a `%VARIABLE%` that Git
   Bash won't expand) and its script exists. `init` prints the current
   and fixed commands. `--repair-hook` makes the fix without asking;
   otherwise it asks (`[n]`), and under `--non-interactive` or
   `--dry-run` it only prints the command to run. The fix keeps your
   interpreter when it's found and writes each `%VARIABLE%` out in full;
   otherwise it names the base Python install by full path. Only that
   one command string changes, after `settings.json` is copied to
   `settings.json.bak-<UTC timestamp>`.
4. **Ask**, writing nothing yet (`setup_flow._decide`):
   - **How you pay** (`setup_flow._ask_billing`). The saved answer is
     the default, and a saved `auto` stays `auto` on Enter. With nothing
     saved, Enter asks again, as does anything but `1`, `2` or a billing
     word from the table below. The answers file's `billing` key answers
     it; under `--non-interactive` without one, the saved value (or
     `auto`) is used and a `(derived) billing: ...` line says so.
   - Under `--advanced`, the rest of the question set below
     (`onboarding.gather_answers` -> `Answers`). Resolution order per
     question:
     - a value named in `--answers FILE` (a flat JSON object) wins;
     - otherwise, interactive stdin prompting (default shown in
       brackets, blank input accepts it);
     - otherwise, under `--non-interactive`, a derived default is used
       and the derivation is printed as `(derived) <key>: ...` —
       nothing is guessed silently.

     Without `--advanced`, those keys keep their saved value (or the
     answers file's) without a note, and WSL folders found are added
     and named in the header.
   - The answers are checked the way `config.toml` checks them
     (`config.check_config_values`). A value it would refuse, or a
     malformed answers file, stops `init` with exit code 2 before
     anything is written.
   - **Connect to Claude Code** `[Y/n]` (`setup_flow._connect`), with a
     short paragraph on what the hook records. Not asked when the
     SessionStart hook is already there, with `--no-install`, or when it
     can't be done: `settings.json` can't be read, or this Python's path
     or the config folder can't be written safely into a command
     (ROB-P9). The review says which. `--connect` says yes up front;
     under `--non-interactive` without it the answer is no, and a
     `(derived) connect: ...` line says so.
   - **Start the dashboard at logon** `[Y/n]` (`setup_flow._service`).
     Not asked when the dashboard already answers at
     `http://127.0.0.1:8765`. When the logon task is registered but
     nothing answers (a task left from an older install), it asks
     `Repair the dashboard's logon task?` instead. `--no-service` skips
     this entirely; `--install-service` says yes up front; under
     `--non-interactive` without it the task isn't added
     (`(derived) run_service: not given on the command line; used
     default False (pass --install-service to install
     non-interactively)` — installing a background service is never
     assumed on someone's behalf).
   - **Sharper tips** `[y/N]` (`setup_flow._tips_question`), only when
     connected, since capture works through the hooks; otherwise the
     review says `Sharper tips need the Claude Code connection, so they
     stay off.` It says what gets tagged, what it costs (Essentials'
     per-session note and per-reply tag, from
     `capture_catalogue.rough_tokens`), and that it switches itself off
     after `DEFAULT_CAPTURE_TIMEBOX_DAYS` (14) days. Yes turns on
     Essentials with that time-box, and the `/cg-feedback` skill. With
     Essentials already on, the default is yes, and no turns it off and
     takes its hooks out. Capture already on at another level isn't
     asked about: a re-run never changes a level you chose, and the
     review says `..., unchanged.` The rating reminder after a large piece
     of work and the plan check stay Deep-only extras, so Essentials stays
     cheap; the summary mentions the skill instead.
   - Under `--advanced` or `--non-interactive`, or with a level from
     `--capture-level` or the answers file, the full
     [metrics capture and feedback questions](#the-full-capture-and-feedback-questions)
     replace sharper tips.
5. **Review** every change (`setup_flow._review`): where the answers are
   saved and how amounts will show; each `settings.json` addition in
   plain words (the capture hook entries counted, not listed), with the
   organisation-policy warning when a managed `settings.json` stops
   hooks running; the `/cg-feedback` skill; the logon task; and
   `Then read this project's history for a first baseline.` when there
   is some. `Go ahead? (d shows the exact changes) [Y/n/d]`: `d` shows
   the `settings.json` diff, the skill's path and the logon task's
   files and commands (`installer.plan_lines`), then asks again; `n`
   writes nothing. `--dry-run` prints the exact changes after the
   review and stops, having written nothing: no `config.toml`, no hook
   files, no baseline. `--non-interactive` goes ahead without asking:
   the flags are the confirmation.
6. **Make the changes**, quick ones first (`setup_flow._apply`), so
   `settings.json` never names a script that isn't on disk yet, and
   stopping the slow baseline still leaves a working setup:
   1. `config.toml` (`config.write_config_values` — merges into an
      existing file key-by-key, `.toml.new` fallback if the merged shape
      can't be round-tripped), and under `--advanced` this project's own
      `<config_dir>/projects/<slug>.toml` (`config.save_project_config`);
      then `[capture]`.
   2. The capture hook scripts and the salt when capture is on, and the
      snapshot hook script, copied to `<config_dir>/hooks/`.
   3. One change to Claude Code's `settings.json` (`--claude-root`, else
      `$CLAUDE_CONFIG_DIR`, else `~/.claude` — never the folder above
      `<config_dir>`), made by `hook_health.plan_connect` and
      `hook_health.connect` after copying it to
      `settings.json.bak-<UTC timestamp>`:
      - a SessionStart hook (`"async": true`) running the snapshot
        script, added only when no SessionStart hook runs
        `snapshot-config.py` yet (a broken one is step 3's job);
      - a `statusLine`, added only when none is set, so yours is never
        replaced;
      - the capture hook entries the chosen level needs, added, updated
        or removed as `capture on` would. They're left alone when
        capture isn't changing, and refused when any capture command
        can't be written safely (ROB-P9).

      The hook command names the base Python install (not a virtual
      environment's, since the script is stdlib-only) and the script by
      full path. The statusline command names the running Python with
      `-m claudeglass.statusline`, or the `.pyz` by full path.
      When `<config_dir>` is not the default `<Claude folder>/claudeglass`,
      both commands end with `--config-dir "<config_dir>"`, so the hook
      and the statusline write where the CLI and dashboard read.
   4. The `/cg-feedback` skill, written or removed.
   5. The logon task: the plan `install-service` registers
      (`installer.plan_service_install`), installed without its own
      output (`installer.install(quiet=True)`), then
      `GET http://127.0.0.1:8765/api/health` every half second for up to
      `setup_flow.HEALTH_WAIT_S` (10) seconds: `Starting the dashboard...
      running.` `init` always registers port 8765 on `127.0.0.1`; use
      `install-service --port/--bind` for anything else. `serve` answers
      within seconds of starting (it binds its port before reading your
      history), so `not answering yet` means it is still starting or has
      failed.
   6. A first baseline, last because it reads history:
      `Reading this project's history for a first baseline... N sessions.`
      Skipped without a word when no project directory has ever been
      recorded for the current folder. Ctrl-C stops it and keeps
      everything else (`claudeglass baseline` takes it later).
      `--all-projects`, `--project` and `--project-family` choose what
      it reads.
7. **Summarise**: `Your setup` (`setup_status.check_setup`, as
   `claudeglass status` prints it): each part `Done`, `Waiting`,
   `Off` or `Needs attention`, with a fix, then `Everything's set up.`
   or `N things need attention.`, what to open next, a reminder to run
   `/cg-feedback` after a piece of work when the skill is in, the
   restart note when `settings.json` changed, and
   `Check your setup any time: claudeglass status`.

### The full capture and feedback questions

Asked under `--advanced` or `--non-interactive`, or when
`--capture-level` or the answers file names a level, in place of the
sharper-tips question. Their answers join the same review, and nothing
is written before it.

**Metrics capture** (`onboarding.ask_capture_level`, then
`onboarding.ask_capture_until`, through `cli.py`'s
`_init_capture_choice`). It warns that capture uses tokens: Claude reads
a short note when a session starts, and ends each reply
with a one-line tag you will see. It then shows what each level would
have cost over your last 14 days, from every project's sessions
(`capture.history`, amounts in your billing units; not under
`--dry-run`, since reading history keeps a cache), and asks for a level
(`off`, the default, `free`, `essentials`, `standard` or `deep`; `yes`
means `essentials` and `no` means `off`). `--capture-level LEVEL` or
the answers file's `capture_level` key answers it without asking; the
warning is still printed. Under `--non-interactive` with neither,
capture stays off and a `(derived) capture_level: ...` line says so,
without reading your sessions. Capture that is already on is left as it
is unless a level is given.

Turning a level on asks one more question: capture switches itself off
in `DEFAULT_CAPTURE_TIMEBOX_DAYS` (14) days by default, and the prompt
says the exact date and how to change it — keep it on longer with
`claudeglass capture on --for 30d` once it's running, or answer
this question **yes** to turn the time limit off entirely so capture
runs until you switch it off yourself. `--capture-for DURATION` (a
number and `h`, `d` or `w`, e.g. `30d`) picks a different length up
front instead of asking; `--capture-no-limit` or the answers file's
`capture_no_limit` key (`true`/`false`) answers "turn it off entirely"
without asking. The two together, or a `--capture-for` it can't read,
stop `init` with exit code 2 before anything is read or written. Under
`--non-interactive` with none of these, the 14-day default is used and a
`(derived) capture_no_limit: ...` line says so (CAP-8: this reaches a
scripted/unattended `init` too, on the same "no answer, use the derived
default" rule every other onboarding question already follows — capture
left running forever with nobody watching is exactly the failure mode
this default exists to prevent). This only matters the first time a
level is turned on — a later `init` never shortens, extends or removes a
limit (or the lack of one) that capture, already on, already has.

The level's `settings.json` entries are part of the one `settings.json`
change (step 6.3) when `init` connects. With `--no-install`, or
`--non-interactive` without `--connect`, the review says to add them
later with `claudeglass capture connect`.

**Feedback** (`onboarding.ask_feedback`, through `cli.py`'s
`_init_feedback_choice`), whatever the capture level (including off):
whether to add the `/cg-feedback` skill — run it after a piece of work
to tick a few quick questions (did it deliver, what were your follow-up
messages, was it worth the tokens, what would have made it cheaper; after
an approved plan, whether it covered what you then fixed and whether the
build could have started fresh) and get a second status-line reminder
that it's there. It costs nothing until you run it, then about two short
turns (three when a second round of questions applies). `--feedback {on,off}` or the answers file's `feedback`
key answers it without asking. Under `--non-interactive` with neither,
it stays off and a `(derived) feedback: ...` line says so. Choosing Deep
turns the survey on, so the question isn't asked then. Feedback already
on is left as it is unless `--feedback`/the answers file says otherwise;
on without its skill file, the skill is written. With `--no-install`, or
`--non-interactive` without `--connect`, the review gives the
`claudeglass capture feedback on` command instead of writing the
skill.

### The question set

| Key | Asked | Asked as | Feeds |
|---|---|---|---|
| `billing` | Always | How do you pay for Claude Code? `1` a plan, `2` an API key. `subscription`, `api` and `auto` are accepted too, and `pro`, `max`, `team`, `enterprise` and `plan` mean `subscription`; anything else asks again (see [billing modes](reference.md#what-it-reads-and-what-it-cant)) | `config.billing` |
| `exclude_projects` | `--advanced` | Projects to always leave out (folder names under `~/.claude/projects`, comma-separated) | `config.exclude_projects` |
| `launch_overlays` | `--advanced` | Do you start Claude Code with `--settings` or `CLAUDE_CONFIG_DIR` pointing at extra settings? | `config.launch_overlays` and this project's `projects/<slug>.toml` |
| `shared_project_config` | `--advanced` | Is this project's `.claude` folder (agents, skills) committed to a repo colleagues use? | `config.shared_project_config` and this project's `projects/<slug>.toml` |
| `tz` | `--advanced` | Time zone, such as Europe/London (blank for this computer's) | `config.tz` (on Windows, a name other than `UTC` needs `pip install tzdata`; see [deploy.md](deploy.md#time-zone-and-tzdata)) |
| `apply_scope` | `--advanced` | Where should changes you apply go by default (`user`/`project-local`/`repo`) | `config.apply_scope` and this project's `projects/<slug>.toml` |
| `capture_window` | `--advanced` | How many days to collect data before the first baseline | `config.capture_window` (default 7) |
| `capture_level` | `--advanced` | Metrics capture level: off, free, essentials, standard, deep (asked after the token-use warning and each level's estimate; by default, the sharper-tips question stands in for it) | `[capture] level` |
| `capture_no_limit` | `--advanced` | Turn off that time limit (capture then runs until you switch it off) — only asked when a level other than off is chosen | `[capture] until` |
| `feedback` | `--advanced` | Add the /cg-feedback skill? (whatever the capture level; by default, sharper tips adds it) | `[capture] feedback` |
| `extra_projects_roots` | `--advanced` | Asked once per WSL folder `init` finds that `config.toml` doesn't list yet: Claude Code also runs in WSL: <distro>; include those sessions? (default yes). Without `--advanced`, and under `--non-interactive` (with a note), they're added. An answers-file list replaces the whole setting | `config.extra_projects_roots` |

`config.capture_started` is set to the current UTC timestamp by the
first `init` — it isn't a question. Running `init` again (for example
`init --connect` or `init --repair-hook`) keeps it, so the capture
window you are part-way through carries on. To start a new window on
purpose, delete the `capture_started` line from `config.toml` and run
`init` again.

**Scope note** (docs vs. code): `docs/config-layers.md`'s "What `init`
(v0.3) will ask" section previews a richer detection step (per-key
settings-layer provenance, agent-inheritance/`shadowed_by_project`
status, `.mcp.json` presence, CLAUDE.md/rules footprint,
`~/.claude.json` trust-dialog state). This work package's `Detection`
covers the config-dir/snapshot/usage-log/project-count facts above only
— the deeper per-project settings-layer scan that section previews
would need a snapshot (or a fresh `probe-config`-style scan) folded
into `detect()`, which is a reasonable follow-on but not implemented
here. Flagged rather than silently narrowed, per this project's
"report a deviation, don't paper over it" convention (see
`profiles/catalogue.py`'s own such note).

### `--answers FILE`

A flat JSON object naming any subset of the keys above. A key it omits
is asked about (the `--advanced` ones only under `--advanced`), keeps its
saved value, or under `--non-interactive` gets a derived default. A
level in `capture_level` brings the full capture and feedback questions
in place of sharper tips:

```json
{
  "billing": "subscription",
  "exclude_projects": ["work-thing"],
  "launch_overlays": false,
  "shared_project_config": true,
  "tz": "Europe/London",
  "apply_scope": "repo",
  "capture_window": 14,
  "capture_level": "essentials",
  "capture_no_limit": false,
  "feedback": true
}
```

## `baseline`

```
python -m claudeglass baseline [--days N] [--finalise] [--list] [--show ID]
```

With no flags: builds a report over the given window (or all time),
extracts a baseline record from it, saves `<config_dir>/baselines/
<id>.json` (+ `<id>.md`), and prints the Markdown report.
`--list`/`--show ID` read back what's already saved instead of
capturing anything new.

A baseline built before the `init`-started capture window has finished
is **provisional** (`record.provisional == true`) unless `--finalise`
is given, or the window has already elapsed on its own
(`baseline.capture_status`).

### What's in a baseline record

Every field is read straight out of the already-built `ReportModel`'s
own tables — never recomputed independently (the same "never fabricate,
only cite the report's own tables" convention `recommend.py`'s
`Recommendation.evidence` contract already enforces):

| Field | Source |
|---|---|
| `mode_mix` | `sessions` section's `sessions_by_mode` table |
| `mode_rules` | `classify.MODE_RULES` when it was saved: which mode rules sorted its sessions (2: unattended overnight, and one-shot) |
| `dominant_purposes` | `sessions` section's `sessions_by_purpose` table (top 3) |
| `archetype` | `workstyle` section's `workstyle_archetypes` table (top row: the archetype that cost the most) |
| `scorecard_overall` / `scorecard_label` | `scorecard` section's `overall` table |
| `projected_saving_usd` | sum of the `ttl` section's `ttl_by_agent_type` table's `saving_usd` column |
| `suggested_profile` / `suggested_profile_reason` | see below |
| `billing_mismatch_warning` | see below |
| `projects` | project directory names, redacted (`discovery.redact_slug`) |
| `cost_per_session` | `overview` section's `totals` table, `total_cost_usd` ÷ `sessions_analysed` |
| `recache_share_pct` | `recache` section's `recache_summary` table (`report.recache_share_pct_metric`) |
| `compactions_per_session` | `compactions` section's `compactions_summary` table (`report.compactions_per_session_metric`) |
| `ttl_mix_top_level` / `ttl_mix_by_agent_type` | `ttl` section's `ttl_by_agent_type` table (`report.ttl_mix_by_agent_type_metric`) |
| `session_baseline_size` | `agents` section's `topology_session_baseline` table (`report.session_baseline_size_metric`) |
| `mean_spawn_write_by_agent_type` | `agents` section's `topology_spawn_write` table (`report.mean_spawn_write_by_agent_type_metric`) |
| `scorecard_dimensions` | `scorecard` section's `dimensions` table (`report.scorecard_dimensions_metric`) |
| `by_mode` | cost/re-cache/compactions per session, recomputed once per distinct mode over a session-filtered sub-corpus (see below) |

These nine fields (v0.3 Task 2) feed `report --baseline <id|latest>`'s
`## Baseline comparison` section (see
[`baseline_comparison`](sections-reference.md#baseline_comparison-reportpy) and
[docs/exports.md](exports.md) for the wider export surface).
`report.py` and `baseline.py` share the same extraction functions
(defined once in `report.py`, imported by `baseline.py`) rather than
duplicating them, since both sides need identical logic — one for a
saved baseline, one for the current window — and `baseline.py` already
imports one-way from `report.py`.

`by_mode` is deliberately narrower than the other eight fields: it
covers only cost per session, re-cache share and compactions per
session, computed by re-running `build_report` once per distinct
`mode` value present in the corpus (`baseline._by_mode_metrics`), over
a session-filtered sub-corpus for that mode. TTL mix, session baseline
size, mean spawn write and scorecard levels are **not** broken out by
mode, because `topology.TopologyStats`/`ttl.TtlStats` accumulate flat
lists/dicts with no per-session id retained — stratifying those would
need changes to those modules, which is out of this work package's
writable surface.

### Suggested profile, and the overnight-batch override

`profiles.catalogue.suggest(archetype, purposes)` can never return
`"overnight-batch"` by construction — its signature has no session
*mode* parameter (see `profiles/catalogue.py`'s
`UNREACHABLE_BY_SUGGEST`). Since a majority-overnight corpus is exactly
the case that profile exists for, `baseline._suggested_profile` applies
the override itself, directly from the corpus's own `sessions_by_mode`
table: when at least half of the corpus's sessions are overnight runs
(`mode=overnight`: Claude worked on its own for two hours or more at
night while you were away), the suggestion is `"overnight-batch"`
regardless of what `suggest()` would otherwise say, with the reason
string citing the exact count. Below that share, `catalogue.suggest()` is called
normally, with `shape="plan-then-build"` when at least half the main
sessions approved a plan and built it in the same session
(`habits.habits_by_shape`); the reason then cites those sessions and
any `/cg-feedback` handoff answers (see
[docs/profiles.md](profiles.md)).

The baseline never applies the suggested profile. The report names the
profile id and reason only; preview it with `claudeglass apply
<id> --dry-run` (see [docs/profiles.md](profiles.md#applying-a-profile)).

### Billing-mismatch warning

A subagent's 1-hour cache TTL is documented as not actually taking
effect under subscription billing (only the top-level conversation
gets 1h there). So when `config.billing == "subscription"` and any
non-top-level row in `ttl_by_agent_type` shows `observed_1h_pct` above
`baseline.BILLING_MISMATCH_THRESHOLD_PCT` (5.0), the record's
`billing_mismatch_warning` names the agent type and percentage —
observing a 1h TTL happening somewhere it's supposed to be impossible
is evidence the stated `billing` value may actually be `"api"`. This is
a heuristic (documented as such in `baseline.py`'s module docstring),
not a locked plan rule — the plan describes the suppression behaviour
but not an exact cross-check threshold.

### The four-section report

`baseline.render_onboarding_report` always produces exactly these
headings, in order: **Summary**, **Suggested profile**, **Projected
saving**, **Next steps**. "Projected saving" is always the
`projected_saving_usd` figure above with a one-line citation back to
the `ttl_by_agent_type` table it was summed from — never a number
computed any other way.

## Privacy

Every baseline field that could carry a filesystem path is redacted
before it is ever written to disk or printed
(`discovery.redact_slug`); nothing here reads message text, tool output,
or raw session content. `tests/test_baseline.py` and
`tests/test_onboarding.py` run every constructed record and rendered
report through `tests/helpers.assert_privacy_deep`.

`init`'s own CLI feedback is the one exception. `Wrote <path>` lines
name `config.toml`/`projects/<slug>.toml`/a baseline relative to
`--config-dir`. The hook-repair, connect and service steps print the
full `settings.json`, backup and service paths. These are the tool's
own operational file locations on the user's own machine, the same class of message
`snapshot-config --install-hook`/`scrub-fixture --out` already print —
not project- or session-derived content, so it is outside the privacy
scan's scope (see `tests/test_cli.py`'s `test_init_writes_config_and_
runs_an_initial_baseline` for the line this distinction is pinned on).

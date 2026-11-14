# Team reports: `export --aggregate`, `import`, `team-report`

A team lead can compare how several people use Claude Code without
collecting anyone's sessions. It takes three steps, run by different
people on different machines:

1. Each team member runs `python -m claudeglass export --aggregate`
   on their own machine. They hand the file to the team lead by whatever
   channel they already use: Slack, email or a shared drive. This tool
   has no upload of its own.
2. The team lead runs `python -m claudeglass import FILE...` on
   their own machine, naming one or more files, to build up a local
   store.
3. The team lead runs `python -m claudeglass team-report` to see
   the comparison.

Nothing here goes online. Every step reads local files and writes local
files. The code is in `src/claudeglass/team.py`.

The last section, [Settings for teams and enterprise](#settings-for-teams-and-enterprise),
covers the `config.toml` settings that matter on a work machine.

## For team leads: the guarantees

- **Aggregate only.** A team document never contains a session id — it
  is sums, counts and means grouped by archetype, mode, purpose, agent
  type and model, never a per-session row.
- **No text, ever.** Every field is a count, a token total, a cost, a
  percentage or a scorecard level (1-5) — never a prompt, a tool
  result, a file path, or a command line beyond a bare tool name
  elsewhere in this project's other privacy-checked surfaces (a team
  document has no command-line field at all).
- **Hashed, not named.** `machine_id` identifies "the same machine
  across two imports" without naming it — it is a salted HMAC-SHA256
  over the hostname, not the hostname itself, and it cannot be
  reversed without the machine's own `<config_dir>/salt` file (which
  never leaves that machine). Project slugs, when included at all, are
  the same kind of hash, never the plaintext. So is a **custom agent
  type name** in the `agent_type` grouping axis (`by_agent_type`):
  Claude Code's own bundled agent types (`general-purpose`, `Explore`,
  `Plan`, `claude`, `claude-code-guide`, `statusline-setup`,
  `workflow-subagent`), plus the synthetic `top-level`/`unknown`
  labels, are kept as-is since there is nothing project-identifying
  about them — but a project- or user-defined custom agent (frequently
  named after the project or its own conventions, e.g. a project's own
  reviewer/implementer agent) is hashed to `custom:<8 hex chars>`
  before it ever leaves the machine that built the document, the same
  salted-HMAC construction as `machine_id` and project slugs, with its
  own domain tag so the three hash namespaces never collide.
- **Opt-in per person, per project.** Nobody's data reaches a team
  document unless they personally run `export --aggregate`. Project
  slugs specifically are opt-in a second time, on top of that, via
  `--include-projects` — the default `export --aggregate` carries no
  project information whatsoever, only the five grouping axes below.
- **Schema-checked on the way in.** `import` rejects anything that
  doesn't match the expected shape — an unexpected top-level key, or
  any string value over 64 characters — before it ever touches disk,
  so a hand-edited or malformed file can't smuggle something unplanned
  into the store.
- **Observed, not controlled.** The `team-report` section carries this
  note above its comparison tables. A difference between two machines may
  reflect different work (a different mix of projects, a different
  role), not a settings or skill difference — the same caveat
  `compare`'s own tables already carry for A/B arms.

## Step 1: `export --aggregate` (each team member)

```bash
python -m claudeglass export --aggregate --out my-machine.json
python -m claudeglass export --aggregate --include-projects --out my-machine.json --days 30
```

Writes one team document (see [docs/exports.md](exports.md#--aggregate-team-documents)
for the exact field list) covering whatever window/project selection
the usual `--days`/`--since`/`--project`/`--all-projects` flags
resolve to (the same window resolution every other subcommand shares).
Hand the resulting file to whoever is building the team report.

## Step 2: `import` (the team lead)

```bash
python -m claudeglass import my-machine.json colleague-a.json colleague-b.json
```

Validates every file first (`team.validate_team_document`), then
copies each one into `<config_dir>/team/<machine_id>-<generated_at>.json`
(with `:` and `.` in the timestamp replaced so it is a safe file name).
If any file fails validation, `import` exits 2 naming the file and the
reason, and writes nothing at all for the whole batch — a bad file
never partially imports alongside good ones. Re-importing a newer
document from a machine that already has one on file supersedes it;
`team-report` always reads only the latest document per machine (see
below), so old imports never need to be deleted by hand.

`import` never resolves `--project`/`--days`/etc. — it only reads the
files you name and writes under `--config-dir`.

## Step 3: `team-report` (the team lead)

```bash
python -m claudeglass team-report
python -m claudeglass team-report --json
python -m claudeglass team-report --html team-report.html
python -m claudeglass team-report --csv-dir ./team-report-csv
```

Reads every document under `<config_dir>/team/`, keeps only the latest
one per `machine_id`, and renders two comparison tables — **by
archetype** and **by agent type** — with one column per machine (its
short hashed `machine_id`, never a hostname) and one row per group
value seen in any machine's document. Each cell shows the machine's
own cost-per-session and session count for that group, or `n<5` when
that machine recorded fewer than 5 sessions in that cell (the same
minimum-sample gate `compare`'s stratified tables use) — override the
threshold with `--min-sessions N`. Output defaults to Markdown, the
same "no flag means Markdown" convention every other report-like or
compare-like subcommand in this CLI already follows; `--json`/`--html`/
`--csv-dir` go through the same renderers `report`/`compare` use.

If nothing has been imported yet, `team-report` exits 1 with a message
pointing at `import`.

## What isn't here

- **No upload/download mechanism.** Getting a file from a team
  member's machine to the team lead's machine is out of scope — use
  whatever channel your team already trusts.
- **No per-purpose or per-model comparison table yet.** `team-report`
  covers archetype and agent type only (the plan's own two named
  axes); `by_purpose`/`by_model` are still in every team document
  (see [docs/exports.md](exports.md#--aggregate-team-documents)) for a
  future `team-report` extension or your own tooling to read directly.
- **No automatic aggregation across imports.** Each `team-report` run
  reflects whatever is currently under `<config_dir>/team/` — nothing
  is merged or averaged across multiple import batches beyond keeping
  the latest document per machine.

## Settings for teams and enterprise

These settings live in `config.toml`, in the config folder
(`~/.claude/claudeglass` unless you pass `--config-dir`).

- **`exclude_projects`** is a list of regular expressions, matched
  against project folder names and ignoring case. A matching project's
  transcripts are never read at all, not only hidden from the output. The
  SessionStart hook still records that project's settings and the names
  and sizes of its CLAUDE.md files. Use it for a confidential repo. A pattern that isn't a valid regular expression
  stops the config loading, with an error naming it. So a typo can't
  quietly stop a project being excluded. `serve --exclude-project SLUG`
  excludes one more project for a single run.
- **`retention_days`** is how many days of sessions the dashboard keeps
  in its store (`service.db`). Older sessions are removed on every poll.
  `serve --retention-days N` overrides it for one run. It must be
  between 1 and 36500, and leaving it unset keeps every session. The
  dashboard's own records (capture signals, the capture log, the
  usage log and card marks) are removed after 180 days unless you set it.
- **Managed settings.** The SessionStart hook also records your
  organisation's managed settings file, redacted like your own settings.
  It records the names of the keys that file sets, too. The file is
  `managed-settings.json` in `C:\Program Files\ClaudeCode\` on Windows,
  `/Library/Application Support/ClaudeCode/` on macOS, or
  `/etc/claude-code/` on Linux and WSL. When a recommendation's setting
  is one of those keys, the recommendation is marked as managed. It says
  "This lever is managed by policy, raise with your administrator."
  instead of suggesting a change you can't make. Its command is marked
  `# managed by policy -- shown for reference only`. See
  [Recommendations](sections-reference.md#recommendations-recommendpy).
- **Provider.** Each transcript records which provider billed it, read
  from the shape of its model id. An id starting `anthropic.` or
  `us.anthropic.`, or ending `-v1:0`, is Amazon Bedrock. An id with an
  `@<date>` suffix is Google Vertex AI. Anything else is the Anthropic
  API. Set `provider` in `config.toml` to `anthropic`, `bedrock` or
  `vertex` to say which one you use. On `bedrock` or `vertex`, the
  suggestion to switch cache lifetime (TTL) is turned off, because this
  tool can't confirm a 1-hour cache lifetime works there. Two things
  aren't done yet: Microsoft Foundry isn't recognised, and every
  provider is priced at Anthropic's own API rates, with no per-provider
  prices in `pricing.toml`.

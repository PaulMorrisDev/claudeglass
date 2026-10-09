# Security policy

claudeglass is a local analytics tool. It reads the files Claude
Code writes; it never calls Claude or any other remote service. The one
command that goes online is `update`, which runs pip to download a new
version (see "No outbound network calls" below). It uses
none of your tokens by default, and none at all unless you opt in to
the optional **metrics capture** feature or its coaching notes (see
below). Capture has Claude itself read a short note and write a
one-line tag inside your own Claude Code session, and coaching notes add
a short note when a hint applies — this tool still never calls Claude
directly. The
one exception is part of the same opt-in: the capture hook runs the
`claude` command you already use to ask Claude Haiku about each finished
agent run, if you let Haiku write the main session's tags too
(`capture tagger haiku`), about each turn, and, while Claude writes
them, about a reply it left without a tag (see "Claude Haiku as the
tagger", "Claude Haiku as the fallback" and "Agent runs" below). This
document is a sign-off checklist for a corporate security review,
written to be verifiable against the code rather than taken on trust.

**Status note:** every guarantee below describes what the *current*
code does. This includes the `claudeglass serve` service
(watcher, SQLite store, JSON API, static web UI) and its deployment
artefacts — see [docs/deploy.md](docs/deploy.md).

## What is read

- Claude Code transcript JSONL files under `<projects-root>/<slug>/
  <session>.jsonl`, `<session>/subagents/agent-*.jsonl` (+ sibling
  `.meta.json`), and `<session>/workflows/wf_*.json`.
- Claude Code configuration, via the `SessionStart` snapshot hook
  (`hooks/snapshot-config.py`): user and project `settings*.json` files
  (including `.claude/settings.local.json`), the platform's system-wide
  `managed-settings.json` (if present), agent frontmatter
  (`.claude/agents/*.md`), MCP server names, and enabled plugin names.
- Environment variable **names** matching `ANTHROPIC_*` / `CLAUDE_*` /
  `OTEL_*` (plus a short fixed list of irregularly-named levers) —
  never their values, with one exception: `MAX_THINKING_TOKENS`,
  `MAX_MCP_OUTPUT_TOKENS`, `BASH_MAX_OUTPUT_LENGTH`, and
  `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` are numeric caps, not secrets, so
  their integer value is recorded alongside the name.
- (Schema 2 — see [`docs/config-layers.md`](docs/config-layers.md) for
  the full field list) `~/.claude.json`, the CLI's own per-machine state
  file: matched to the current project by
  `os.path.normcase(os.path.realpath(...))` — the raw matching key is
  never stored — yielding MCP server/plugin names, small counts, and
  numeric per-project session totals (a cross-check against this tool's
  own accounting for the same session, joined by session id, never
  message text); and byte counts, file counts, and names only (never
  content) for the CLAUDE.md family, `.claude/rules/`,
  `.claude/commands/`, skills, `.mcp.json`, output styles, auto-memory,
  and installed plugins.

- **On request only**, when you open Agents & context › Context or run
  `claudeglass review claude-md|skills` or `check claude-md|skills`:
  the text of your CLAUDE.md-family files (user, project, local, rule
  files, nested CLAUDE.md files, auto memory `MEMORY.md` and one level of
  `@` imports), and the skill descriptions in the newest transcripts'
  skill listing. They are read to measure sections, find duplicates and
  stale references and show each skill's description, then discarded:
  never written to the database, a snapshot, a log or a cache. The
  prompts built from them name headings and line numbers, not whole
  passages.

- `settings.json` in the folder above `<config-dir>` (normally
  `~/.claude/settings.json`), read directly by `init`, `changes`,
  `uninstall` and the dashboard's Data quality page, to see whether the
  SessionStart hook and statusline are set up. Only the hook command and
  whether `statusLine` runs this tool are used; nothing from it is
  stored.
- The JSON payload Claude Code sends the statusline command on stdin at
  each refresh (see "Statusline" below).

Nothing outside these locations is read.

## What is written, and where

Everything this tool writes by itself lives under `<config-dir>`
(default `~/.claude/claudeglass`, or `$CLAUDE_CONFIG_DIR/claudeglass`):
`config.toml`, `projects/`, `baselines/`, `snapshots/`, `cache/`,
`usage-log.csv`, `statusline-keys.json`, `salt`, `service.db`,
`hooks/snapshot-config.py`, `profiles/`, `backups/`,
`active-profile`, and, once metrics capture has been turned on at
least once: `hooks/capture-hook.py` (a small launcher),
`hooks/capture_hook.py` (the hook's code, with the compiled copy Python
keeps in `hooks/__pycache__/`), `hooks/capture-catalogue.json`
(a copy of the packaged metric catalogue the hook reads),
`capture-log.jsonl` (one JSON line per `[capture]` change — the level,
sample, `until` etc. you set, never anything from a transcript),
`habit-log.jsonl` (one JSON line each time you press **Trying it** on a
dashboard card: the card's kind, its id, the word `trying` and a time,
nothing else) and `signals/YYYY-MM.jsonl` (see "Metrics capture" below), and, while
Claude Haiku writes the tags or fills in a missing one, `tags/YYYY-MM.jsonl`
(see "Claude Haiku as the tagger" and "Claude Haiku as the fallback" under
"Metrics capture"), and, once
coaching notes or a survey item that answers your messages have been turned
on, `coaching.json` (the names of hints
your answers muted or limited, and your typical piece of work) and `coach-state.json` (see "Coaching notes" and "The survey's
notes" under "Metrics capture"), and, while capture is on, `payload-keys.json` (the
key names of each kind of SessionStart payload, once each: names only,
never a value). The only other
files it writes are output files you name on the command line: for
example `--out` (`export`, `monthly-report`, `tuning export`,
`scrub-fixture`), `report --html PATH` or `serve --monthly-report DIR`.

Five commands change something outside `<config-dir>`. Each prints the
change before making it:

- **`init`** (connect step): adds a SessionStart hook and, if you have
  none, a statusline to `settings.json` in the folder above
  `<config-dir>`. It asks first (default no), or does it without asking
  with `--connect`. `init --repair-hook` rewrites only a broken hook
  command. Both copy the file to `settings.json.bak-<UTC time>` beside
  it before writing. `init`'s later metrics-capture and feedback
  questions make the same kind of change — see the next bullet.
- **`capture`** (also reachable through `init`'s metrics-capture and
  feedback questions): `on`, `level`, `enable`/`disable` and `connect`
  add the `settings.json` hook entries the chosen metrics need (never
  more than they need); `off` leaves them in place, inert; `remove`
  takes them out. Every settings.json change is shown as a diff and
  made only after a yes (or `--yes`/`--connect`/answering yes to
  `init`'s question), with the same `settings.json.bak-<UTC time>` copy
  first. `feedback on|off` and `brief on|off` instead add or remove
  `~/.claude/skills/cg-feedback/SKILL.md` or `.../cg-brief/SKILL.md`
  (`--claude-root`'s folder, not `<config-dir>`) — the file (or diff, on
  an update) is shown in full and written only after a yes; a
  `SKILL.md` this tool didn't write is left alone rather than
  overwritten. `[capture]` in `config.toml` itself is written without
  asking (it is this tool's own file), but only after the same
  token-cost warning `capture on`/`init` already print. See "Metrics
  capture" below.
- **`install-service`** (and `init`'s last step, when you say yes):
  registers `serve` to start at logon. On Windows that is a Scheduled
  Task (`-RunLevel Limited`, no file written); on Linux it writes
  `~/.config/systemd/user/claudeglass.service`; on macOS
  `~/Library/LaunchAgents/com.claudeglass.plist`. `--dry-run`
  prints the plan and does nothing. `uninstall-service` removes it. See
  [docs/deploy.md](docs/deploy.md).
- **`uninstall`**: removes this tool's hooks (including the capture
  ones) and statusline from `settings.json` (after the same `.bak-`
  copy), removes the `/cg-feedback`/`/cg-brief` skill files (each shown
  and asked separately), removes the logon service, and, only with the
  matching flags, reverts applied changes (`--revert-changes`) and
  deletes `<config-dir>` (`--delete-data`, which also removes the
  capture files listed above). It asks before each step unless you pass
  `--yes`.
- **`apply`**: see "Applying a profile" below.

Running processes: `install-service`, `uninstall-service`,
`uninstall`, `changes` and the service's `/api/health` run the
platform's own task tool (`schtasks`, `powershell.exe`, `systemctl` or
`launchctl`) to register, remove or check the logon service. `apply`
runs `git ls-files` to check whether a target file is tracked. Nothing
else starts a process.

## What is stored

Only numeric digests and short, non-identifying labels:

- Token counts, timestamps, model ids, tool names, event kinds, and up
  to a **40-character** Bash/PowerShell command prefix
  (`Turn.cmd_prefix`/`preceding_cmd_prefix`).
- Structural shape markers for anything not on an explicit allowlist:
  `dict(n)`, `list(n)`, `str(len)` — used by the config-snapshot hook for
  every settings/frontmatter value that isn't one of the small set of
  named-safe keys (`model`, `effortLevel`, `outputStyle`,
  `autoCompactWindow`, `autoCompactEnabled`, `promptCacheTtl`,
  `subagentPromptCacheTtl`, `cleanupPeriodDays`,
  `desktopSessionCleanupPeriodDays`, `autoUpdatesChannel`,
  `alwaysThinkingEnabled`) or a plain `bool`/`int`/`float`/`None`. Two further keys get
  their own safe summary shape instead of a raw value: `statusLine`
  (a bare present/absent boolean, never the command it runs) and
  `modelPricing` (a present flag plus the model ids it overrides, never
  the overridden numbers).

For tool search, a reply keeps only how many tools were listed by name,
by MCP server name, the size of that list and each server's share of
it, the length of each server's instructions, and how many times Claude
read each server's resources. A transcript keeps the size of each tool
definition it loaded, by tool name; each MCP server's tool names after
its prefix (at most 64 characters each); the size of the tools sent in
full, by server; and each server's last connection problem (pending,
needs sign-in, failed to connect). A definition's description and
schema, and a server's instruction text, are never stored, and a tool
name outside the API's tool-name alphabet (letters, digits, `_`, `-`,
`.`) is dropped.

For the quality signals, a message of yours keeps only a yes/no for
whether its first 200 characters contain a correction phrase
(`events._CORRECTION_RE`; the text itself is dropped), and a task
notification keeps only its task id and status word, and a workflow
agent only its state word from the run file (`done`, `error`,
`progress`), never the run file's prompt or result previews. Edited files are
known by the same salted hash as below (`Turn.edit_target_hashes`).

To tell an agent that writes code from one that decides, an agent keeps
one role word (`TranscriptMeta.role_word`): a single canonical word from
a closed list (`agent_roles.py`: `implement`, `review`, `audit`, ...),
found in its workflow phase, its agent type or the first four words of
its description, and never the phase or description text it came from,
which is dropped as the meta file is read. It also keeps whether its
meta file records a model at all (`TranscriptMeta.model_recorded`:
whether the file has a `description` or `workflowPhase` key, never the
value), and a turn keeps a count of the writes its shell commands made
outside the temp dir (`Turn.shell_write_count`; a file written twice
counts twice), never the paths.

`Turn.read_target_hashes` is the one exception to "no path fragment is
ever stored", and it is deliberately a one-way hash rather than a
shortened/redacted string: for every `Read`/`Edit`/`Write`/
`NotebookEdit` tool call in a turn, `parse.py` stores
`hmac.new(salt, path.replace("\\", "/").casefold(), sha256).hexdigest()[:16]`
(a case- and slash-normalised path, not `os.path.normcase` — that call is a
no-op on POSIX and would leave two spellings of the same Windows path
unmerged) — a 16-character
hex digest that lets the *same* file be recognised as re-read across
turns and sessions (for "which files does this session keep
re-opening" analytics) without the path itself, or any substring of it,
ever appearing in a dataclass field, the digest cache, or a rendered
report. The salt is a random 32-byte value generated once with
`secrets.token_bytes(32)` and stored at `<config-dir>/salt` (`0600`
permissions where the OS supports it); without a salt in effect
(`parse.set_salt` never called), `read_target_hashes` is always empty
rather than falling back to an unsalted, offline-crackable hash. Because
the hash is keyed to a salt private to one machine's `<config-dir>`, it
cannot be correlated against a hash produced on a different machine or
after the salt file is rotated/deleted. The on-disk digest cache
(`cache.py`) enforces the "after rotation" half of that: each entry's
header carries a hash of the salt that wrote it (never the raw salt),
and `DigestCache.get` misses rather than serving a hit when a caller
that was itself given a salt finds a different one on the entry —
without this, a cache entry written before a salt rotation would go on
being served afterward, quietly carrying hashes keyed to the old salt.

**The `claudeglass serve` service's SQLite store**
(`<config-dir>/service.db`) is a narrow, documented exception to "no
path fragment is ever stored": `transcripts.path`, `projects.root_path`
and `profiles.toml_path` hold real local filesystem paths, including
the machine's username where it appears in a Windows/POSIX home
directory (`service/schema.py`'s module docstring). They exist purely
for the watcher's own bookkeeping — deciding what to re-parse and
where a project's scan root is — and every one of `Store`'s read
queries (`summary`, `sessions`, `session`, `daily_usage`, `recache`,
`compactions`, `snapshots`, `profiles`, `baselines`, `tags` and the
rest) is written to leave them out of its result dict entirely, so the
`/api/*` routes and the UI never see them; `tests/test_service_store.py`
asserts this with a distinctive fake path round-tripped through the
read queries.
`sessions.slug` (Claude Code's own project-slug encoding of the
project's absolute path, so it also embeds the username) is read out —
the UI needs some label for "which project" — but every read query
that selects it passes it through `discovery.redact_slug()` first,
which replaces the username segment with the literal `<user>` before
it ever reaches `/api/sessions`, `/api/session/<id>`,
`/api/report.{json,md,html}`, or the rendered UI.

Message text, tool-result content, file contents, full file paths and
full shell commands are never written to a dataclass field, the on-disk
cache, or any rendered output. This is enforced today by
`tests/test_privacy.py` (walks every field `parse_transcript` produces
and asserts no `str` field exceeds 64 characters outside a small named
allowlist, and that command-prefix fields never exceed 40) and
`tests/helpers.assert_privacy` (a second, shape-based scan every
privacy-relevant test fixture also runs, checking for a Windows drive
path, a POSIX `/home/` path, a `\Users\` path, an MSYS drive path, a
bare `@`, or a URL). Run it yourself:

```bash
python -m pytest tests/test_privacy.py tests/test_scrub.py -q
```

The same audit applies to `report --json` and `report --html PATH`
output, or to JSON and HTML you generate by calling `render.json_out` or
`render.html` yourself. Run it by hand over the file:

```bash
grep -RnoE '[^"]{65,}|[A-Za-z]:\\\\|/home/|\\\\Users\\\\|/c/Users/|@' report.json report.html
```

Treat any hit as a bug, and open an issue naming the field (never its
value).

<a id="applying-a-profile-the-one-command-that-writes-outside-config-dir"></a>

## Applying a profile

`claudeglass apply` (`profiles/apply.py`) is the one command that
changes how Claude Code behaves: it writes the settings and agent files
a profile or `--set` names. It does so only when you run it yourself —
never as a side effect of `report`, `snapshot-config`, the dashboard or
any other subcommand, and never on `--dry-run` (which only prints
text). `init` and `uninstall` also edit `settings.json`, but only to add
or remove this tool's own hook and statusline (see "What is written,
and where" above).

A real apply (not `--dry-run`, not `--launch`) writes exactly these
files, depending on `--scope`:

- One of `~/.claude/settings.json` (`user`), `<project>/.claude/
  settings.local.json` (`project-local`), or `<project>/.claude/
  settings.json` (`repo`) — a JSON read-merge-write of the profile's
  allowlisted settings keys only.
- `<project or ~>/.claude/agents/<name>.md` for each agent the profile
  configures — only the allowlisted frontmatter keys are patched in
  place; every surrounding character (comments, unrelated keys,
  formatting) is preserved verbatim.
- `<config-dir>/backups/<ts>/...` — a byte-for-byte pre-image of every
  file above, written *before* the new content, plus a `manifest.json`
  recording which backup corresponds to which target.
- `<config-dir>/snapshots/<ts>.json` and `<config-dir>/active-profile`
  — claudeglass's own bookkeeping, not a Claude Code config file.

`--launch` writes only `<config-dir>/profiles/<id>.settings.json` (a
one-session overlay) and nothing else. `apply` never writes an
environment-variable *value* to any file — a profile's `env` names are
printed as `export NAME=value` guidance only (see `docs/profiles.md`'s
"What a profile cannot do") — and never writes a key a managed-settings
layer currently governs, regardless of scope or flags.

Two refusals are on by default, both requiring an explicit flag to
override: writing to a file already tracked by git, at any scope
(`--allow-tracked`; checked with `git ls-files`), and creating an agent
frontmatter file that doesn't exist yet (`--force`). Every write is
preceded by a byte-for-byte backup, so any apply can be undone exactly
with `claudeglass apply --revert <ts>`: each file is restored from
its backup, and a file the apply created is deleted. A revert checks
each file's hash against the one `apply` recorded and refuses,
restoring nothing, if a file was edited after the apply, so it never
silently discards later changes; `--ignore-changes` overrides that. The
backups stay under `<config-dir>/backups/` until you delete that folder
(`uninstall --delete-data` refuses while an applied change is still in
place). Full detail:
[docs/profiles.md#applying-a-profile](docs/profiles.md#applying-a-profile).

## Metrics capture

**Off by default, and reversible.** `init`'s last-but-one question and
`claudeglass capture on|level` are the only ways this turns on;
`capture off` (or letting the default 14-day time-box run out — see
[docs/onboarding.md](docs/onboarding.md)) turns it off again without
removing the settings.json hook entries or the `[capture]` config, so
turning it back on needs no re-asking of the settings.json/skill
questions. `capture remove` takes the hook entries back out.

**It uses your tokens, and only while it's on.** Each metric it adds
makes Claude read one short note and end its reply with one line of
closed-vocabulary tags, e.g. `[cg: task=bugfix brief=clear]`; a subagent
is asked for nothing; `init` and `capture on` print a token-cost
estimate from your own history before you confirm it (see
[docs/onboarding.md](docs/onboarding.md) for the exact wording). With
capture and coaching notes off, none of this happens — Claude Code runs
exactly as it does without this tool installed.

**Coaching notes.** Off by default and separate from the level:
`capture enable coaching_notes` turns them on, at any level, after the
same settings.json diff and yes. When a hint applies, the capture hook
adds a short note to Claude's context, `cg-coach v1 <hint>` and a
sentence built only from token counts, an idle time and an agent
type's name — never a path, command or anything you wrote. To decide,
the hook reads the last 4 MB of the session's transcript when you send a
message (256 KB when a plan is approved, 64 KB after a tool call or when a
turn ends; the first 512 KB for the plan hint's starting size, and a
subagent's own transcript for the split hint), counting sizes, times and tool names
only; nothing it reads is kept. It also reads your message's words in
memory, only to tell a change request, a go-ahead, a status check or a
question from the rest, and a usage-limit line's opening words and reset
time; it keeps none of them. To count compactions it scans the whole
transcript a line at a time and keeps only the compaction markers' ids.
To tell that work went to the background it matches the first 400
characters of a tool result against four fixed phrases in memory and
drops the text. At a compaction it also looks at the end of any
subagent transcript changed in the last five seconds, for the type and
time of its records only, to tell a subagent's compaction from the main
session's. The note for a tip ends on the tip itself, a fixed sentence
built from the same counts, so Claude can write it for you. The split
hint is never said to a subagent: when a subagent run stops, the hook
reads the head and the compaction records of that run's own transcript in
memory, keeps only a count under the session for each of two words
(`compaction`, `brief`) and prints nothing, and the main session's next
agent call or background-task message says it. No text of the run, its
brief or its type is kept. A
background `Stop` entry prints nothing: when a turn ends it only keeps
the time, context size and cache lifetime of Claude's newest reply, as
numbers, so the cold-return receipt is timed from a real reply. A
usage-limit line is never taken for one. `coach-state.json` holds, per
session, when each hint last showed, how many times and how long it
rests, and those three numbers, keyed by the same salted session hash
as the signals, and, for the agent runs that ended too big and have not
been told yet, the count of each of two words; entries older than a day
are dropped. A hint you said you already knew also
carries a flag, `once`, that is true or absent. `coaching.json` holds
hint ids from the fixed list in the catalogue, and numbers. Neither ever leaves `<config-dir>`. See
[docs/coaching.md](docs/coaching.md).

**The survey's notes.** `capture feedback on` (and Deep) also lets the
capture hook answer a message you send, at any capture level, after the
same settings.json diff and yes. It reads the same end of the transcript
as the coaching hints, in memory, and keeps nothing it reads. When you run
`/cg-feedback` it adds one line, `cg-fb-facts v1`, of numbers and words
from closed lists: the tokens the piece of work used, your typical piece,
how many follow-up messages you sent, a plan's state, a tip's hint id
and similar counts, never a word you wrote. With the plan check on (Deep
turns it on), a message that reads as a fix to work Claude did after you
approved a plan gets a note that has Claude ask one `AskUserQuestion` (the
header "CG plan fix", four fixed options), and with the rating reminder
on, a piece of work of a million tokens or more that hasn't been rated
gets a note that has Claude end its reply with a fixed line. To see
whether you rated that session on the dashboard, the hook opens the
dashboard's `service.db` read-only and reads only the time of that
session's rating. To decide, the hook matches your message's words against
fixed patterns in memory (a correction, an adjustment, a go-ahead, a
thank-you, a status check) and keeps none of them. The parser keeps the
one word you ticked
for the plan check (`PlanCheck.word`: covered, gap, new or none), or none
when you declined or typed your own, and never the message. For all
sessions together, `coach-state.json` also holds how many plan checks in
a row went unanswered and the time the check rests until, the time of the
last reminder, and short salted hashes of the plans and pieces of work
already noted (at most 32 of each) and of the plan-check questions already
counted (the last 16). `coaching.json`
also holds your typical piece of work, a token count worked out from
sessions you have had, and what your tip and plan answers change: the hint
ids to leave out (`muted`) or show once a session (`once`), and the raised
numbers for the hints that have one. Those lists come from counts of closed
words (useful, known, wrong) in the report, never a word you wrote, and the
hook only compares hint names against them. The checks are in
`tests/test_capture_hook.py`
(`test_the_facts_line_holds_only_counts_and_words_of_a_closed_list`,
`test_the_feedback_notes_leave_nothing_of_your_words_in_the_note_or_the_state`),
`tests/test_coaching.py`
(`test_a_tip_you_already_knew_shows_once_a_session_however_much_grows`,
`test_the_hook_reads_muted_and_once_only_as_lists_of_names`)
and `tests/test_capture_parse.py`
(`test_a_declined_plan_check_or_an_other_answer_has_no_word_and_keeps_none_of_your_words`).
See [docs/coaching.md](docs/coaching.md#the-surveys-notes).

**Claude Haiku as the tagger.** Off by default: `capture tagger haiku`
(or "Tags written by" on Setup › Capture, after a yes) turns it on, and
`capture tagger claude` back off. The session note then asks Claude for
no tag. When a turn of the main session ends, the capture hook's `Stop`
entry builds a short excerpt of it from the transcript's end: your
message (up to 2,000 characters) and the one before (300), how many you
sent before and how many minutes after Claude's last reply, how many
files that reply changed and how many changed again, how many short
follow-ups you sent in a row, up to three messages you sent while Claude
worked (300 characters each), what Claude did (model calls, output
tokens, tool names and counts, how many files it changed, the first line
of up to six Bash or PowerShell commands, skill names, subagent and
tool-error counts, whether there was a plan and how often one was
proposed and sent back), the first 600 characters of the plan you
approved or, without one, of your latest message longer than 300, and
the last 1,500 characters of Claude's final reply. Never a tool's
output. All of it is held in memory and handed to the worker. It hands
the excerpt to a worker (the same script with
`--judge`) and returns. The worker runs `claude -p --model haiku --tools ""
--setting-sources "" --strict-mcp-config --no-session-persistence
--output-format json`, the excerpt on stdin (never on the command line,
where other local users could see it) and the instructions (this tool's
own fixed text) in a short-lived file in `<config-dir>`, deleted once the
call ends, with `CLAUDEGLASS_JUDGE=1` set so
the hook does nothing inside that call. That is Claude Code itself, with
your own login and your own provider settings: the excerpt goes where
the rest of the session already went, and this tool reads no API key or
credential. Only the tag's words are kept, each checked against the
closed vocabularies (a skill name after `would-help:` is dropped), in
`<config-dir>/tags/YYYY-MM.jsonl`, with the time, the reply's API message
id, and the call's cost, token counts and model name. `haiku_tags.load`
checks every line again before a report uses it. The files are pruned on
the same retention as the signals. Before the words are written, the
hook settles them against the transcript. It reads your message and
Claude's last reply for a few phrases (a correction, a tweak, an owned
mistake) and the shell commands for ones that move or remove files, in
memory. Only yes/no answers and counts go on to the worker, and none of
that text is kept. When a word changed, the line holds an optional `g`
note for each change, `key:from>to`, in closed vocabulary words only, and
`haiku_tags.load` checks it with the rest of the line.
`tests/test_haiku_tags.py`
(`test_the_worker_logs_the_words_and_the_cost_never_the_excerpt`,
`test_the_loader_checks_every_line_again`,
`test_the_row_notes_each_word_grounding_changed_or_dropped_and_nothing_else`,
`test_the_grounding_note_is_read_back_checked_and_an_old_row_has_none`,
`test_the_stop_hook_hands_the_turn_to_a_worker_that_asks_claude`,
`test_what_the_user_sent_while_claude_worked_goes_to_haiku_and_is_never_kept`)
covers this.

**Claude Haiku as the fallback.** While Claude writes the tags, the
default, a reply that ends a piece of work without one would leave it
untagged. From Essentials up the same `Stop` entry then asks Haiku for
it, with no setting of its own: this is part of the capture opt-in, and
`capture tagger haiku` still moves every tag to Haiku. It is the same
excerpt, worker, `claude -p` command, `CLAUDEGLASS_JUDGE` guard, closed
vocabularies, grounding and tag file as above, and the line holds one
more field, `w`, set to `haiku-fallback` so a report can tell the two
writers apart (a line without it, and any other value, reads as the
tagger's). The hook decides from the end of the transcript and the
payload, in memory. It asks for none of these: a reply that has a tag,
a piece of work in which any reply has one, a reply with no text, a turn
that answers a line you didn't type (a background agent's report, a
message from another session, a scheduled task, a command's output, a
prompt Claude Code sent itself), a turn while a background agent is
still running, a session a scheduled task started (it gets no note
either, as the parser drops tags there), and any call from a script
(`claude -p` or the Agent SDK). `tests/test_haiku_tags.py`
(`test_the_fallback_asks_haiku_for_an_untagged_reply_that_ends_a_piece_of_work`,
`test_the_fallback_leaves_a_tagged_reply_a_tagged_cycle_and_an_empty_reply_alone`,
`test_the_fallback_skips_a_turn_that_answers_a_line_the_user_did_not_type`,
`test_the_fallback_skips_a_session_a_scheduled_task_started`,
`test_the_stop_hook_hands_a_fallback_to_the_worker_unless_a_script_is_running_it`,
`test_the_worker_marks_a_fallback_line_with_its_writer`) and
`tests/test_capture_hook.py` (`test_a_scheduled_task_session_gets_no_note`)
cover this.

**Agent runs.** With any agent metric on (`result`, `retry`,
`agent_brief`, from Essentials up), no subagent is asked for anything and
no brief carries a marker. When a subagent finishes, the capture hook's
`SubagentStop` entry returns at once, handing the worker only the paths
of the agent's transcript and the session's. The worker waits until the
agent's answer call (`StructuredOutput` or `SubagentHandback`) is in the
transcript or the file has stopped growing for three seconds, up to a
cap of 20 seconds, and records `no_answer` without calling Haiku when
the cap is reached. It then builds a short excerpt from the agent's
transcript and the session's: its type and model, its brief (up to 2,000
characters; a workflow agent's computed task, with the line the workflow
was started with beside it, up to 300 characters, as context), the last
300 characters Claude wrote before starting it, what it did (model
calls, output tokens, tool names and counts, the files it changed, the
first line of up to six shell commands, tool-error count), the last
1,500 characters of its report or, when it handed an answer back
through an answer tool, that answer, field by field with each value cut
at 300 characters, and up to four earlier agent runs read from the
whole of the session's transcript (their type, model, the first 200
characters of their brief and the last 200 of their report). Never a
tool's output. The same worker asks Haiku as above, and only the checked
words (`result`, `retry`, `brief`, `missing`) are kept in the same tag
files, with the id of the agent's last reply. A run judged at more than
one stop has a line for each, and the newest is read. Older lines may
also hold a `fit` word, from before that verdict was dropped, and it is
still read. Agents that set up Claude Code itself are skipped.
`tests/test_haiku_tags.py`
(`test_the_agent_excerpt_says_what_the_run_did_and_what_came_before`,
`test_the_worker_logs_an_agent_runs_words_under_their_own_key`,
`test_the_subagent_stop_hook_hands_the_run_to_a_worker`,
`test_the_stop_hook_returns_at_once_and_leaves_the_reading_to_the_worker`,
`test_a_workflow_agents_answer_is_shown_field_by_field_each_value_cut_on_its_own`,
`test_a_file_that_never_stops_growing_is_given_up_on_at_the_cap_and_no_haiku_call_is_made`)
covers this.

**Scripts and the Agent SDK.** A run with nobody at the screen
(`claude -p` or the Agent SDK: Claude Code sets `CLAUDE_CODE_ENTRYPOINT`
to `sdk-cli`, `sdk-ts` or `sdk-py`) gets no note, no coaching and no
Haiku call from the capture hook, whatever capture is set to, since a
script reads what it prints; its signal lines are still logged.
`tests/test_capture_hook.py`
(`test_a_run_with_nobody_at_the_screen_gets_no_note_but_still_logs_signals`)
covers this.

**No free text is ever kept.** `capture_tags.py` reads only the last
`TAIL_SCAN_CHARS` (480) characters of a reply, only when the tags are
the very last thing in it, and checks every key and value against the
closed vocabularies in `capture_catalogue.py`; anything Claude wrote in
its own words — an unknown key, an unknown word, a value that doesn't
match — is dropped, never stored. The one exception is
`skill=would-help:<name>`, and even that survives only when `<name>`
matches a skill the same transcript already listed or invoked, not
whatever string Claude wrote. `/cg-feedback` has one place to type,
the Other choice every `AskUserQuestion` offers, and it is handled the
same way: the text is read once, to pick the closest word, then
discarded. Claude maps it to a word of that question's own list (or
leaves the key out when nothing fits), is told never to copy, quote or
save it, and writes only the word in the tag with the question's key in
`from_text`. The parser sees the answer in the transcript in memory only
(`feedback_from_answers`) and keeps just which questions were answered
that way (`Feedback.other`, keys from the closed list). A word picked
from your note is accepted only when the `AskUserQuestion` result shows
a non-label answer for that key, a ticked answer always wins over the
tag, and a tag with no answers behind it loses its `from_text` words
(`capture_tags.settle_feedback`). The checks are in
`tests/test_capture_feedback.py`
(`test_a_word_picked_from_your_note_counts_when_the_answer_was_not_a_label`,
`test_a_tag_cannot_override_a_ticked_answer`,
`test_free_text_answers_are_never_kept_only_which_question_they_answered`)
and `tests/test_privacy.py`. The dashboard's own rating has no free-text
field: its questions are checkboxes over the closed lists in
`RATING_VOCAB`, which `POST /api/sessions/<id>/feedback` accepts, with
the plan and handoff answers for each plan build. What it shows a session
comes from counts and words worked out in memory from the stored
transcript digest (`ratings.session_facts`): no message text is read,
kept or sent. The Useful, Trying it, Knew it and Wrong here buttons on
tip, habit and recommendation cards are a closed list too
(`TIP_CARD_VOCAB`), kept with the card's id in the store's `tip_feedback`
table (see "What the dashboard can change" below).

**Signals are salted, like everything else here.** The free signals —
why a session ended, and what kind of thing Claude was waiting on when
it sent a `Notification` or asked permission (your permission, your
next message, a clarifying question, a sub-agent, a usage-limit pause,
or other — never how long you took to answer) — are logged to
`<config-dir>/signals/YYYY-MM.jsonl` keyed by
`hmac.new(salt, session_id.encode("utf-8"), hashlib.sha256).hexdigest()[:16]`
(`signals.session_hash`) — the same reused `<config-dir>/salt` file
described under "What is stored" above, not a second salt — and
`test_nothing_is_logged_without_the_salt` (`tests/test_capture_signals.py`)
asserts nothing is written before a salt exists.

**Hook timing.** The SessionStart and SubagentStart note hooks, and the
SessionEnd signal hook, run in the foreground (Claude waits for them,
capped at `CAPTURE_TIMEOUT_S` = 5 seconds). An async hook's
`additionalContext` does still reach Claude (Claude Code delivers it on
the *next* conversation turn), but that's too late for a note about a
tool result Claude just saw, so these stay synchronous. At the Deep
level, the PostToolUse hook that notes an unusually large result or a
web call (matcher `Read|Grep|Glob|WebFetch|WebSearch`, never the shell or
MCP tools) is foreground too, for the same reason. A settings.json
written before they were dropped still runs the hook after them until
the entry is rewritten; it returns at once, before it reads
`config.toml`, a transcript or its state file. Claude Code records the
real `durationMs` of the hook calls that printed something, which is few
of them. `capture status` therefore counts how often each installed hook
ran from the transcripts instead (the tool calls its matcher selects, the
messages you sent, the turns that ended, the sessions and the agent
runs), takes the median of the times that were recorded, and prints one
sentence over your last 7 days, or since capture was turned on if that
was later ("ClaudeGlass's hooks ran about..."). It is counts and
durations only, and an event with no recorded time is named as left out,
never shown as zero. Without the big_output and web metrics nothing is
registered on PostToolUse at all (only `WebFetch|WebSearch` matter when a
custom set turns on the web metric alone).

**Hook health is bucketed, not named.** `hook_health.count_hook_errors`
tallies every hook attachment Claude Code writes to a transcript —
yours as well as ClaudeGlass's own — by the closed hook-*event* name
only (`PreToolUse`, `PostToolUse`, and so on; `events._HOOK_EVENT_NAMES`,
verified against Claude Code's own docs). The matcher/tool-name suffix
after the `:` (e.g. the `Bash` in `PreToolUse:Bash`, or an MCP server's
own name in `PreToolUse:mcp__server__tool`) is always dropped before it
reaches `Event.detail` — never kept, never shown — so `capture status`'s
"this hook keeps failing" prompt can never leak which MCP servers or
tools you have configured. It is a prompt only: nothing here ever
writes to `settings.json`.
The two free signal hooks, Notification and PermissionRequest, run
asynchronously (in the background) since nothing needs to read what
they print.

**Files.** See "What is written, and where" above for
`hooks/capture-hook.py`, `hooks/capture_hook.py`,
`hooks/capture-catalogue.json`, `capture-log.jsonl`, `habit-log.jsonl` and `signals/`, and the `capture` bullet there for
how the settings.json hook entries and the `cg-feedback`/`cg-brief`
skill files under `~/.claude/skills/` are added (diff or full text,
asked, backed up) and removed.

**The dashboard's one write into this.** `POST /api/capture` (see
"What the dashboard can change" below) is the only way the local
`serve` UI changes capture settings, and it can only ever change the
`[capture]` table of ClaudeGlass's *own* `config.toml` — never
`settings.json`, never a skill file. It is refused (`403`) on anything
but a loopback request, on top of the DNS-rebinding and cross-site
checks every other `POST` route gets (see "What a web page can and
can't do to the service" below); a config error comes back as `409`
with the equivalent `claudeglass capture ...` command to run
yourself instead (`service/api.py`'s `route_capture_post`).

**Tested.** Besides the tests already named above,
`tests/test_capture_signals.py` (`test_each_signal_is_one_line_of_closed_words`,
`test_anything_outside_the_word_lists_is_logged_as_other`,
`test_nothing_is_logged_without_the_salt`, `test_nothing_is_logged_when_capture_or_the_metric_is_off`),
`tests/test_capture_parse.py` and `tests/test_privacy.py` all exercise
this feature's output against the same "no free text, no path, no raw
session id" checks the rest of this document describes.
`tests/test_parse_events.py` (`test_hook_output_mcp_matched_hook_name_never_reaches_detail`,
`test_hook_output_command_string_never_reaches_detail`) and
`tests/test_capture_cli.py` (`test_status_never_prints_a_raw_matcher_or_tool_name`)
cover hook health specifically: an MCP tool/matcher name or a hook
command's own path never reaches `Event.detail` or `capture status`'s
output.

## No outbound network calls

The tool never calls Claude, Anthropic or any other remote service on
its own, and uses none of your tokens unless you turn on metrics
capture or coaching notes, which spend tokens inside your own Claude
Code session (never a call this tool makes itself) — see "Metrics
capture" above. From Essentials up, the capture hook starts the `claude`
command once per finished agent run, and, while Claude Haiku writes the
tags, once per turn, or, while Claude writes them, once for a reply that
ended without one; the call is Claude Code's, with your own login,
and the hook imports no networking module (see "Claude Haiku as the
tagger", "Claude Haiku as the fallback" and "Agent runs" above).

**`update` is the one command that reaches the real internet**, and it
does so through `pip`, not through this tool's own networking code:
`_cmd_update` (`cli.py`) shells out to `pip install --upgrade
--force-reinstall --no-deps <source>`, where `<source>` (`--from`)
defaults to this project's own GitHub repository
(`UPDATE_SOURCE = "git+https://github.com/PaulMorrisDev/claudeglass"`)
— pip clones whatever commit is at the tip of that repository's default
branch when you run it (unpinned; pass `--from` a tag, a
commit-pinned URL or a local folder for anything more reproducible).
`--dry-run` prints the exact `pip` command without running it. If the
dashboard is registered to start at logon, `update` then runs the
newly-installed copy's `install-service`, which — the same as a bare
`install-service` or `init`'s service step — probes its own
freshly-(re)started copy over loopback: up to two `GET /api/health`
requests (`_http_health_ok`, then `_http_health_version` once that
succeeds) to the address and port it just registered
(`127.0.0.1:8765` by default), never any other address, purely to
print whether it came back up and on which version.

Outside `src/claudeglass/service/` and `update`'s `pip`
subprocess above, no module imports `socket`, `urllib`, `http.client`,
`requests` or equivalent — `cli.py`'s two `urllib.request.urlopen`
calls (`_http_health_ok`, `_http_health_version`) are the only ones,
both loopback-only as just described. The package has zero runtime
Python dependencies (`pyproject.toml`'s `dependencies = []`). The
dashboard vendors d3 and two fonts, pinned by sha256 in
`service/static/THIRD_PARTY.sha256`, and makes no remote requests.
`rich` is an optional, opt-in extra for nicer terminal output, not a
networking dependency. Pricing comes from a user-edited local `pricing.toml`,
never a live lookup — there is no code path that could fetch it.

For the CLI's analytics/report subcommands this is a structural
guarantee: nothing to call out to, because they contain no networking
code at all. **The `claudeglass serve` service** (a local
`http.server` API and static UI, `src/claudeglass/service/`) is
the main exception: `service/api.py` and `service/serve.py` do import
`http.server` (to listen on its own local socket) and `urllib.parse`
(to parse request query strings — it never builds or fetches a URL).
The service opens one socket — its own local HTTP bind,
`127.0.0.1`-only unless you pass `--bind <address> --allow-remote` — but never
initiates a connection of its own. This is an automated, always-on
guarantee, not just documentation:
`tests/test_service_egress.py` monkeypatches every socket-level call
that could originate an outbound connection or a DNS lookup —
`socket.socket.connect`, `socket.socket.connect_ex`,
`socket.create_connection` and `socket.getaddrinfo` — for the lifetime
of a real running server, and asserts every recorded target is the
test client's own loopback address, so any future change that adds an
outbound call or even resolves a remote hostname fails the suite. It has also
been verified against the built Docker image directly, independent of
the Python-level test: run with `--network none` (no network namespace
connectivity beyond loopback at all), the service still answers
`/api/health` correctly from inside the container — see
[docs/deploy.md](docs/deploy.md#verifying-no-egress) for the exact
commands. The Docker image adds defence in depth on top of that
guarantee (non-root user, read-only root filesystem, `cap_drop:
[ALL]`, `no-new-privileges`); the native Windows Scheduled Task and
systemd hosting paths are scoped instead by OS-level permissions
(`-RunLevel Limited`, `ProtectHome=read-only` plus a carved-out
`ReadWritePaths`) rather than a container boundary — detail on all
three in [docs/deploy.md](docs/deploy.md).

## What a web page can and can't do to the service

The service has no login. Any program or user on this machine can call
it, and so can other machines if you bind it to a non-loopback address
(`--bind <address> --allow-remote`; `serve` refuses a non-loopback bind
without that flag). These guards stop a web page you visit from using
it through your browser:

- **DNS rebinding.** A page can't read the API by pointing its own
  domain name at `127.0.0.1`: every request whose `Host` header isn't a
  loopback name (`127.0.0.1`, `localhost`, `::1`), the specific
  `--bind` address, or a name you added with `serve --allowed-host`
  gets `403` before any route runs. A request with no `Host` header
  (not a browser) is allowed. See
  [docs/api.md](docs/api.md#host-allowlist-dns-rebinding).
- **Cross-site writes.** Every `POST` is refused with `403` when its
  `Origin` header isn't exactly `http://<Host>`, or its
  `Sec-Fetch-Site` is anything but `same-origin` or `none`. A request
  with neither header (a local script) is allowed. A `POST` must also
  be `Content-Type: application/json`, which a plain HTML form can't
  send. See [docs/api.md](docs/api.md#cross-site-protection-review-s3).
- **No CORS.** The service never sends `Access-Control-Allow-*`
  headers, so another site's script can't read a response.
- **Body size.** Every `POST` body is capped at 64 KB — checked
  against `Content-Length` before anything is read off the socket —
  `413` otherwise. Since there's no login, this also bounds how much
  memory and JSON-parse work any local process (not just a web page)
  can force per request. See
  [docs/api.md](docs/api.md#body-size-limit-g5).
- **Response headers.** Every response carries
  `Content-Security-Policy: default-src 'self'` (scripts only from the
  service itself), `X-Content-Type-Options: nosniff` and
  `Cache-Control: no-store`. The vendored d3 and fonts under
  `/static/vendor/` and `/static/fonts/` are the one exception to
  `no-store`. They are pinned by sha256 and carry no data about you.
  Each name carries its release, so new bytes always arrive under a new
  URL, and they are sent as `public, max-age=31536000, immutable`.
- **Static files.** `/static/*` serves only files inside the packaged
  UI folder; a path that escapes it gets `404`.
- **No request log.** Request paths are never written to stdout or a
  log.

## What the dashboard can change

The dashboard never changes your Claude Code configuration. A
recommendation or profile gives you a prompt to paste into Claude Code
(which asks your permission before editing anything under `.claude`)
and a `claudeglass apply ... --dry-run` command to run yourself.
The service's few write routes touch only its own files: session tags
(`mode`/`purpose`), your `/cg-feedback` rating (`POST
/api/sessions/<id>/feedback` — the closed checkbox vocabulary of the
skill's questions, `capture_catalogue.RATING_VOCAB`; an unknown
field or value is `400`, and nothing ticked clears a rating) and your
rating of a tip, habit or recommendation card (`POST /api/tip-feedback`
— `capture_catalogue.TIP_CARD_VOCAB`, with the card's kind and id; an
unknown value or id is `400`, and **Trying it** also appends a line to
`habit-log.jsonl`) in the store, user profiles under `<config-dir>/profiles/` (`POST
/api/profiles`, and `POST /api/profiles/from-current`, which saves the
allowlisted keys of the latest config snapshot there), and the
`[capture]` table of ClaudeGlass's own `config.toml` (`POST
/api/capture` — see "Metrics capture" above; it is the one dashboard
route that can turn metrics capture on, change its level, or turn it
off, and it never touches `settings.json` or a skill file). Profile
writes refuse to overwrite an existing profile unless asked to with
`?replace=1`, and neither can create or change a shipped catalogue
profile. `POST /api/whatif` only works out an estimate and writes
nothing. `POST /api/predictions/seen` (EST-P5) only flips a `seen` flag,
by its own row id, on one of this tool's own logged "what if?"
predictions already in the store, so the "Did your estimates come
true?" table on Setup › Settings can tell a prediction you've looked at
from one still waiting on you — it names no session, setting or transcript
content.

The service's on-disk SQLite store (`<config-dir>/service.db`) is
always a derived cache rebuilt from the same transcripts the CLI
already reads, never a second source of truth — `claudeglass
serve --purge` deletes it safely at any time (it prints exactly which
files it will delete and requires `--yes` before doing so).

## Digest cache

`cache.py`'s on-disk cache stores one JSON file per transcript under
`<config-dir>/cache/<sha256 of normcase(realpath)>.json` — a
provenance header (mtime, size, schema/parser version) plus the same
privacy-clean `TranscriptResult` fields described above, never raw
transcript content. A transcript modified in the last 60 seconds is
never read from or written to the cache (it's still being written by an
active session). To purge it:

```bash
# POSIX
rm -rf ~/.claude/claudeglass/cache
# Windows
Remove-Item -Recurse -Force "$env:USERPROFILE\.claude\claudeglass\cache"
```

`--rebuild-cache` does this for you (purges the cache, then repopulates
it as it parses); `--no-cache` skips the cache entirely for that one run
without deleting anything already on disk. Both are wired through to
`corpus.load_corpus` for every subcommand that loads a corpus — see
[docs/cli.md](docs/cli.md#global-flags).

## Excluding confidential projects

`exclude_projects` in `<config-dir>/config.toml` is a list of slug
regexes (matched with `re.search`, case-insensitive); any project whose
slug matches is excluded from discovery entirely — never scanned, never
parsed, never appearing in a cache file — not merely hidden from
output. A malformed regex stops `config.toml` from loading, with an
error naming the pattern, so a typo can't quietly stop a project being
excluded.

```toml
exclude_projects = ["^confidential-", "client-acme$"]
```

## Aggregate exports (`export`, `monthly-report`)

`claudeglass export` (`src/claudeglass/exports.py`) and
`claudeglass monthly-report` (`src/claudeglass/monthly.py`)
read the same in-memory corpus every other subcommand does — no
additional file access, no network access — and write only counts, token
totals, and costs; never a prompt, a tool result, or a file path.

- **Aggregate-only by default.** `export` defaults to one row per
  `(day, project, model, entrypoint, agent_type)`; `session_id` is only
  present when the caller explicitly passes `--per-session`
  (`exports.resolve_export_options`).
- **Project slugs are hashed by default in every mode, including
  `--per-session`** — `--per-session` alone no longer implies raw
  slugs. Hashing uses a salted HMAC-SHA256 over the slug
  (`hmac.new(salt, b"slug:" + slug, sha256).hexdigest()[:12]`), the same
  HMAC-SHA256 construction (just a different truncation length and
  domain-separation tag) and the same `<config-dir>/salt` file described
  under "What is stored" above (`parse.load_or_create_salt`, reused
  rather than a second salt) — never an unsalted hash, and never the
  fully raw slug unless `--no-hash-slugs` is passed explicitly.
  `--no-hash-slugs` does not print the fully raw slug either: it
  replaces just the OS-username segment (`Users-<name>-`/`home-<name>-`)
  with `<user>` and prints a one-line warning to stderr naming the risk
  — a caller who genuinely needs raw slugs for correlation still cannot
  leak the machine's own username by accident.
- **`otel-jsonl` carries no project/session attribute at all** — the
  OpenTelemetry metric names it mirrors (`claude_code.token.usage`,
  `claude_code.cost.usage`) don't have one, so `--aggregate-only`/
  `--hash-slugs` are moot for that format.
- **`monthly-report` never lowers the bar.** It calls `report.build_report`
  over a month-filtered corpus and inherits that function's own privacy
  properties; it adds no session ids, slugs, or text of its own — only
  numeric finance totals.

`tests/test_exports.py` exercises this with `tests/helpers.assert_privacy`
against the fully rendered export text (not just the in-memory rows) in
every format, plus explicit checks that no real session id string is
present in an aggregate-only export and that no raw slug string is
present when `--hash-slugs` is in effect (`tests/helpers.assert_privacy`
also fails on any slug-shaped `Users-`/`home-`-anchored username segment
anywhere in a scanned string, not just in export output). Full
column-by-column detail: [`docs/exports.md`](docs/exports.md).

## Tuning export (`tuning`)

`claudeglass tuning export` (`src/claudeglass/tuning.py`) writes the
figures that show how you work with Claude, to take to another machine, and
`claudeglass tuning summary FILE` reads one back. Export works over the same
in-memory corpus every other subcommand does, and `summary` reads one local
file and nothing else. Export also reads your dashboard ratings and tip-card
answers from `<config-dir>/service.db`, read-only, as `report` does; only
their counts reach the file. Neither makes a network call. Export writes only
to the terminal or the single file `--out` names. The properties below are the
ones the code enforces, not promises about intent:

- **Only counts, list-price amounts and words from fixed lists.** The
  document never holds a name, a path, a hash, a session id, a project name,
  a skill name, a custom agent name (it counts as `custom`), a model id (it
  is reduced to a family) or any text of yours or Claude's. Any new pattern
  that reads a transcript's text runs in memory and the text is dropped.
- **A closed description of what may appear.** `tuning.spec()` names every
  key. Every string must be one of the words it lists or match a tight
  pattern (a version, a date, an ISO week). Every number must be finite, 0
  or more and no more than a trillion. The keys of every map come from fixed lists
  built from the packaged catalogues, never from the data, so nothing in a
  transcript can become a key.
- **A second scan of the whole text.** Whatever the description says, the
  serialised document is refused if it holds a drive path, `home/`,
  `Users\`, a Git Bash `/c/` path, an `@`, `://` or `www.`. At read, the scan
  also covers the file's own text, and a key repeated in one object is
  refused, so nothing can hide in a value the parser would drop.
- **A size limit.** The document is 256 KB or less. The fullest one the
  description allows is about 225 KB, so a valid document always fits, and
  a real one is about 35 KB.
- **Fail closed, at write and at read.** `tuning.build` validates what it
  built and `tuning.dumps` validates again before it returns text, so a
  document that fails produces no file and no JSON on stdout: `tuning
  export` prints the problems to stderr and exits `2`. `tuning summary` validates the file it reads and, when it
  fails, prints no figure from it and exits `2`. A file is stat-ed first, and
  one over twice the limit is refused without being read.
- **Problem lines never repeat what they found.** A line names a key path
  and the check it failed ("`capture.level`: must be one of the words the
  spec allows"). It never prints a value or a key from the document, so
  printing a refusal can't leak what caused it. A refusal prints at most 20
  lines and counts the rest.
- **The file is written whole or not at all.** With `--out`, the text is
  written to a temporary file in the same folder and renamed into place
  (`cli._write_file_atomic`), so a failure leaves any file already there as it
  was and leaves no partial file or leftover.
- **Nothing is imported.** No command copies a tuning file into
  `<config-dir>`, the dashboard's store or `config.toml`, and the dashboard
  never reads one. A file you receive from someone else is only ever read by
  `summary`, which validates it first.

`tests/test_tuning.py` changes the sample document one thing at a time and
checks that each unknown key, key given twice, free text, path, address, URL
and oversize file fails and that no value or key is echoed. It also runs
`build` over a world on disk and scans the result with
`tests/helpers.assert_privacy_deep` and for every private string it wrote.
`tests/test_cli_tuning.py` holds the command's side: an invalid build leaves
no file and prints no JSON, a tampered file prints no figure, and the refusal
text holds no path. Full detail:
[`docs/exports.md`](docs/exports.md#claudeglass-tuning).

## Statusline

Claude Code runs the statusline command only in a terminal session,
not in the desktop app. Its output is shown to you under the prompt and
is never sent to Claude. From each payload it appends a row to
`<config-dir>/usage-log.csv`: the time, the session id, usage-limit
percentages and reset times, context-window token counts and cache
state. No prompt text, file paths or command text.

### Payload key recording (`statusline-keys.json`)

`claudeglass`'s statusline integration
(`src/claudeglass/statusline.py`) records the *key names* of the
JSON payload Claude Code writes to it on every refresh —
recursively, dotted (e.g. `context_window.used_tokens`), capped at 200
names — to `<config-dir>/statusline-keys.json`, and only when that set
differs from what is already stored. **Names only, never values**: no
prompt text, no token counts, no file paths, no usernames, and no
session ids are ever written to this file, regardless of what the live
payload actually contains. See [`docs/exports.md`](docs/exports.md) for
why this exists (reconciling undocumented, partially-overlapping
field-name lists against the real payload shape).

## Reporting a vulnerability

Please open a private security advisory on the GitHub repository (or,
if that isn't available, open an issue asking for a private contact
channel) rather than a public issue, so a fix can land before the
details are public. Include the claudeglass version
(`claudeglass --version`), your OS, and — since transcripts are
never meant to leave your machine — a minimal *synthetic* reproduction
rather than a real transcript excerpt.

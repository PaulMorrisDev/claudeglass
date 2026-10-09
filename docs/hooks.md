# Your hooks (`hook_costs.py`)

A hook you set up in Claude Code runs a command at a set moment: before a
tool call, after one, when a session starts, when Claude stops. Each one
can cost you in three ways, and fail without telling you:

- **It fails.** A hook whose script isn't there, times out or errors
  doesn't do its job. Claude Code carries on, so you only see it if you
  look.
- **It blocks calls.** A `PreToolUse` hook can deny a call. Claude reads
  the block in its next reply and often sends the same call again
  unchanged, which spends a reply for nothing.
- **It adds context.** A hook's `additionalContext` (or plain output from
  a `SessionStart` or `UserPromptSubmit` hook) stays in the conversation.
  Every later reply reads it again until the next summary.

`hook_costs.py` measures each of these per hook, and three
recommendations suggest a fix when one is large enough.

## What it reads

`parse.py` keeps a few facts per hook record (see `model.py`'s
"Your-hooks addition", `PARSER_VERSION` 22, 23 and 32). It never keeps a
command, its output or a full path.

- **The hook's label.** The file name of the script its command runs
  (`session-digest.ps1`), or the command's first 40 characters with any
  path hidden when it names no script. Context Claude Code adds itself is
  labelled `built-in`.
- **Why a failed run failed.** Exit code 127 or a "not found" message is
  *script not found*; a timeout is *timed out*; anything else is
  *error*.
- **Whether the script path is relative.** A relative path only resolves
  when Claude Code runs the hook from the project root. Claude Code's
  hooks guide recommends `"${CLAUDE_PROJECT_DIR}/.claude/hooks/<script>"`.
  A path inside quotes is read from the opening quote, so a space or tab
  in it doesn't split it.
- **The tool a failed tool hook ran for**, when it is one of Claude
  Code's own (`Bash`); any MCP tool is kept as `mcp`, never by name.
- **Whether the command uses a `%VAR%` variable.** Claude Code runs hooks
  in bash or PowerShell, and neither expands a Windows `%USERPROFILE%`,
  so the path stays as written and the script isn't found.
- **Context per reply.** The characters of hook context that landed before
  each reply, by hook (`Turn.hook_context_chars`). A context record has no
  command, so it takes the label of the successful run just before it for
  the same tool call or session event.
- **Blocked calls.** Tool calls a hook denied, by hook
  (`Turn.hook_blocks`), and later calls with the same tool and the same
  input (`Turn.hook_resends`). The input is compared by hash in memory
  and never stored.

## What it measures

Per hook, over every transcript in the window (main sessions and
subagents alike):

- **Failed runs**, the sessions they fell in, when the last one happened
  (UTC), the most common cause, and whether the path is relative or uses
  a `%VAR%` variable.
- **Stopped failing.** A hook fixed partway through the window keeps its
  old failures until they age out, so each failing hook is judged on
  what came after its last failure, in the projects it failed in (a
  subagent counts in its session's project). A tool hook's clean runs
  leave no record, so its chances to fail are the calls to the tools it
  failed on (any MCP tool counts as one); a `SessionStart` hook's are
  the sessions that start and the summaries; any other hook's are its
  own recorded runs. If, at the rate it failed up to then, at least
  `hooks_quiet_failures` (default 5) more failures should have followed
  and none did, it has stopped failing: the table says so and the
  failing-hooks card leaves it out.
- **Runs seen working**: successful runs, plus calls and stops the hook
  blocked. Claude Code records a tool hook's run only when it fails,
  blocks or adds context, so a hook's clean runs are undercounted.
- **Cost of blocks.** The next reply reads the block, so each block costs
  that reply's price times the blocked calls' share of the calls it
  answered. **Sent again unchanged** takes the same share of that cost.
- **Cost of keeping its context.** The context's tokens (characters / 4)
  times each later reply's carry rate, from the reply it lands before to
  the next summary or the end: the same model as a tool output's in
  [`carry.md`](carry.md).
- **Time waited**: the run times Claude Code records (`durationMs`), and
  how much of that was on failed runs.

ClaudeGlass's own capture note is folded into its hook's row, and the
[capture section](capture.md) prices it too. Claude Code's built-in
context is left out of the table, since you can't change it; a note
gives its total.

## ClaudeGlass's own hook

Its row is labelled `capture-hook.py`, the launcher Claude Code runs.
The hook is three files under `hooks/` in ClaudeGlass's data folder: the
launcher, `capture_hook.py` with the code, and `capture-catalogue.json`
with the word list. Python keeps the compiled form of a module it
imports and none of a script it is started on, so the code sits in a
module to skip compiling it on every call. Uninstalling removes all
three, and the compiled copy, with the data folder.

Claude Code waits for a hook each time it runs one. This one is
registered on UserPromptSubmit and on PostToolUse for `Read`, `Grep`,
`Glob`, `WebFetch`, `WebSearch` and an approved plan, and never for the
shell or MCP tools: they were about two thirds of the calls, and a size
note after them was wrong too often to pay for the wait. With coaching
notes on, it is also registered for the `Agent` and `Workflow` tools, so
it can tell you what each report from background work will cost to
answer when you start it in a long session. It reads no size from their
results and never says a large result is large. It is also registered
for SubagentStop, where it prints nothing: it only notes that an agent
run ended too big for one task, so the main session's next hook can say
so. A call takes
about 44 milliseconds on the author's Windows machine, python's own
start being about 12, and returns before anything else when a result is
under the size a note could apply to. A `settings.json` written before
the shell and MCP tools were dropped keeps starting it after them until
`claudeglass update --finish` or `capture connect` rewrites the entry;
it returns at once. The wait and the size measure are on the
[coaching notes page](coaching.md#what-it-costs).

## The report section

`hooks`, always emitted, with two tables: `hooks_summary` (one row) and
`hooks_by_script` (costliest first: failures, then money, then waiting;
the top `hooks_top_n`, default 20). See
[`sections-reference.md`](sections-reference.md#hooks-hook_costspy) for
the columns. The dashboard shows both on Agents & context › Hooks, and
Actions › Checks asks "Do your hooks work, and what do they cost?".

## The recommendations

None of them changes a setting. Each gives a prompt to hand Claude.

- **`hook-failures`** (severity `action`) when a hook failed at least
  `hooks_min_failures` (default 10) times and hasn't stopped failing
  (below). One card names every such hook; its action follows the top
  hook's cause, and it says when that hook last failed. For a script not found
  by a relative path, it suggests starting the path with
  `${CLAUDE_PROJECT_DIR}`; for one behind a `%VAR%` variable, `$HOME`
  and forward slashes. No saving is claimed: the cost is the hook
  not doing its job.
- **`hook-block-resent`** (severity `advice`) when a hook blocked at least
  `hooks_min_blocks` (default 10) calls and Claude sent at least
  `hooks_resend_share_pct` (default 50%) of them again unchanged. It
  suggests letting the call through with `additionalContext`, or
  changing it with `updatedInput`. The saving is the share of the block
  cost those re-sent calls took.
- **`hook-context-carry`** (severity `advice`) when a hook added context
  at least `hooks_min_contexts` (default 20) times, costing at least
  `hooks_min_context_usd` (default $1.00 at list price) to keep. It
  suggests a shorter message, adding context only when it matters, or
  turning a plugin's hook off where it isn't needed. The saving is at
  most the cost of keeping that context. ClaudeGlass's own capture hook
  is left out: the capture section covers it.

All thresholds live in `config.toml`'s `[thresholds]` table under the
`hooks_` prefix.

# Coaching notes

ClaudeGlass's tips are worked out after the fact, from sessions that have
already ended. Coaching notes bring the ones that can be acted on in the
moment into the session itself. When a hint applies, the capture hook
(`capture-hook.py`) adds a short note to Claude's context, and Claude
acts on it or tells you in a highlighted tip.

They are for wherever the status line doesn't show, such as the Claude
desktop app. In the terminal, the coaching line (`coaching_line`) shows
the same kind of hint in the status line instead, at no token cost.

Coaching notes are off by default. Turn them on with:

```
claudeglass capture enable coaching_notes
```

or on the dashboard's Setup › Capture page. They run at any capture
level, including off, and in every session, but not in a project that
`[capture] projects` or `exclude_projects` leaves out, and never in a
run with nobody at the screen (`claude -p` or the Agent SDK), whose
output a script reads. Like every
capture change, the settings.json entries the hook needs are shown and
added only after you say yes. `capture disable coaching_notes` turns
them off; `capture remove` turns them off too, and takes the entries
out.

## The hints

Each note starts `tl-coach v1 <hint>`, so ClaudeGlass can find it in your
transcripts again and measure what it cost. A note never carries a path,
a command or your words: only token counts, an idle time, a count and an
agent type's name.

| Hint | When | What the note asks of Claude |
|---|---|---|
| `plan_fresh` | You approve a plan, and building it in a fresh session would drop at least 40,000 tokens of planning context. | Tell you in a tip that `/clear`, then asking Claude to carry out the saved plan, would carry that much less on every reply of the build. Then carry on. |
| `split_run` | A subagent run passes the number of replies your own history says its type's runs are best split at (see [below](#your-own-split-points)). | If more than a step or two is left, finish the current step and end the report with what's done, what's left and the files involved, so a fresh agent can carry on. |
| `quiet_output` | A tool result is about 8,000 tokens or more. A read already given a line limit is left alone. | Next time, ask for less: read only the lines needed, use a quieter flag or a filter that keeps every error line (not a bare `head` or `tail`), narrow a search. |
| `explore_reads` | The main session has made 8 reads and searches for one message. | If more searching is needed, hand it to an Explore agent, which searches in its own context and sends back a summary. |
| `repeat_ask` | You send much the same request as one Claude answered in the last hour (80% of its words in common, at least 4 different words). A message you stopped before any reply and sent again isn't a repeat, and nor is one sent again after an API error, an overload or a usage limit ended its reply. | Don't repeat the same approach: say in one line what probably went wrong and try differently, or ask one question. Then suggest in a tip that saying what was wrong gets a better next attempt than resending. |
| `drip_feed` | You send your third small request in a row: short messages, each sent within 20 minutes of Claude's reply, the earlier ones each answered with a file change ("make the button bigger", "now move the logo", "and the footer too"). The words don't matter. A detailed message, a reply that changed nothing, or a longer gap starts the count again. | Make the change, then suggest in a tip that working out everything the work still needs and sending it as one message gets it done in one pass. |
| `stop_loop` | You've stopped Claude (Esc) three times in the last 20 minutes, during a reply or before it started. | Before changing anything, say in two or three lines what it will do, and wait for a go-ahead on a large change. Then suggest plan mode (Shift+Tab), which agrees the approach before any work starts. |
| `plan_first` | You send a request for 4 or more separate changes, 150 characters or longer, outside plan mode, before any plan was approved in the session. A message that mentions a plan is left alone. | Before changing anything, set out in a few lines how it will go about it and in what order, then carry on. Then suggest plan mode (Shift+Tab) for a job that size. |
| `vague_fix` | A fix request of 80 characters or less that names nothing specific: no file, line, quote, error or image, and not what it should be instead ("it's broken", "doesn't work", but not "it should say Hi"). | If the problem isn't clear from the context, ask one short question before changing anything. If it is, fix it and suggest in a tip that saying what you saw and expected, or pasting the error, gets a fix first time. |
| `big_paste` | You send a message of 10,000 tokens or more, such as a pasted log or file. | If most of it is a log, a file or output, suggest in a tip pasting only the part that matters, or saving it to a file and giving the path. |
| `cache_cold` | You send a message after the prompt cache expired (5 minutes idle, or an hour when the session uses the 1-hour cache), with at least 20,000 tokens of context. | If your message starts something unrelated, say in a tip that the reply wrote the whole context again, and that `/clear` before a new task after a break avoids it. Otherwise say nothing. |
| `clear_context` | You send a message with 100,000 tokens or more of context. | If your message starts something unrelated, say in a tip that `/clear` first would have saved re-reading it all. Otherwise say nothing. |

One note at most per tool result or message: the first hint in the table
that applies. The first four come after a tool result, the rest when you
send a message. `split_run` shows only inside the subagent; the rest
only in the main session, except `quiet_output`, which shows in both.
`vague_fix` doesn't show during a run `drip_feed` has already flagged.
A message you didn't type gets no hint: a background agent's report
(which Claude Code hands to Claude as the next message), a scheduled
task, a slash command's output.

## How a tip looks

When a hint asks Claude to tell you something, Claude ends its reply,
after a blank line, with a quote block starting **ClaudeGlass tip:**,
so it stands apart from the work in the terminal and in the desktop app:

> **ClaudeGlass tip:** That's 3 small changes in a row, each its
> own message, and each one re-reads the whole session. Working out
> everything the page still needs and sending it as one message gets
> it done in one pass.

The six prompting hints (`repeat_ask` to `big_paste`) also show you a
one-line notice the moment you send the message, before Claude replies:

```
⚠️ ClaudeGlass: 3 small requests in a row, one message each. Work out everything that needs changing and send it as one prompt: it costs less.
```

The notice is the hook's `systemMessage`: Claude Code shows it to you and
never sends it to Claude, so it costs no tokens. Claude Code's docs don't
say which apps show hook messages, so Claude's reply carries the tip as
well. The other hints have no notice: whether `cache_cold` or
`clear_context` matters depends on what your message asks, which only
Claude can tell.

The /tl-feedback reminder (`feedback_reminder`) has the same look,
labelled **ClaudeGlass:**.

Nothing ClaudeGlass asks Claude to write carries an emoji. Claude copies
what its context shows: with a ⚠️ and a 💡 in these labels, it began
using them as markers of its own in unrelated work. The notice above is
shown only to you and never reaches Claude, so it keeps its ⚠️.

## How your messages are read

The six prompting hints (`repeat_ask` to `big_paste`) read the message
you're sending and your earlier ones at the end of the transcript.

- `drip_feed` goes by what happened, not by your words: each message's
  length, how soon after Claude's reply you sent it, and whether Claude
  changed a file in answer to it. A small request is 300 characters or
  less; a longer one usually plans several changes at once, which is
  what the hint asks for. A reply to a question Claude asked (its last
  words hold a "?") is skipped, and a bare "thanks" or "ok" isn't a
  request. A session's first message never counts: it starts the work.
- `repeat_ask` compares the words of your message with those of your
  earlier ones Claude answered, in memory, as it runs; nothing about
  them is kept.
- `stop_loop` counts the line Claude Code writes when you stop a reply,
  and a message Claude never answered before you sent the next: Esc
  before any reply writes no line, it puts your message back to edit.
- `plan_first` counts the separate changes a message asks for: its list
  lines, its change verbs ("add", "move", "rename" and the like) and the
  items of one sentence that starts with one ("Add login, a settings
  page and an admin screen" is three). It stays quiet when Claude Code
  doesn't tell the hook which permission mode you're in.
- `vague_fix` looks for a fix or correction word in the first 200
  characters ("fix", "still", "wrong", "doesn't work", "that's not what
  I asked").

Slash commands, stopped replies and a subagent's messages don't count.
Nothing about your words is kept or passed on: the note says only how
many.

The coaching line in the status line shows `drip_feed`, `repeat_ask`,
`stop_loop` and `big_paste` too, at their default thresholds.

## After the fact

Work habits › **How you prompt** counts the same six habits in all your
sessions, whether or not coaching notes were on, with what each cost,
its trend by week and what to try instead. Once there are coaching
notes, **Tips Claude showed** says how often Claude passed each hint's
tip on. Turning coaching notes on is a change on **Your changes**,
measured by these habits per 100 of your messages before and after.

Once a hint has shown, it rests for 30 minutes in that session, unless
what's at stake has grown one and a half times since (a context grown
from 100,000 to 150,000 tokens, say). `split_run` rests per run.

## Your own split points

Two hints depend on how you work. The dashboard's service works them out
once a day, from a report of your last 30 days across every project, and
writes them to `coaching.json` in ClaudeGlass's data folder for the hook
to read:

- **Split points.** An agent type gets the `split_run` hint only when the
  [run-split tip](run-split.md) shows its long runs would have cost less
  split, at the interval it found best. An agent type whose tip you
  ignored on the dashboard doesn't get it.
- **The plan hint.** On unless you ignored the
  [plan-handoff tip](plan-handoff.md), or most of your /tl-feedback
  answers say your builds relied on the discussion before the plan. Its
  threshold is the tip's own, `plan_handoff_min_dropped_tokens`.

Without the service, `claudeglass capture refresh` works the file
out now. Until there is one, no agent type gets the split hint and the
plan hint is on. `capture status` says what the file holds.

## Changing when they apply

Every threshold above can be changed in `config.toml`'s `[thresholds]`
table, and wins over the file:

| Key | Default | What it sets |
|---|---|---|
| `coaching_plan_fresh_tokens` | 40000 | Planning context kept after a plan before `plan_fresh` applies. |
| `coaching_quiet_output_tokens` | 8000 | A tool result's size before `quiet_output` applies. |
| `coaching_explore_reads` | 8 | Reads and searches for one message before `explore_reads` applies. |
| `coaching_drip_count` | 3 | Small requests in a row before `drip_feed` applies. |
| `coaching_drip_window_minutes` | 20 | The longest wait after Claude's reply for a message still to count. |
| `coaching_drip_chars` | 300 | The longest message that counts as a small request. |
| `coaching_vague_fix_chars` | 80 | The longest fix request `vague_fix` looks at. |
| `coaching_repeat_similarity` | 0.8 | The share of words two messages share before `repeat_ask` calls it the same request. |
| `coaching_repeat_window_minutes` | 60 | How far back `repeat_ask` looks. |
| `coaching_repeat_min_words` | 4 | The fewest different words a message needs for `repeat_ask`. |
| `coaching_plan_steps` | 4 | Separate changes in one request before `plan_first` applies. |
| `coaching_plan_min_chars` | 150 | The shortest request `plan_first` looks at. |
| `coaching_stop_loop_count` | 3 | Stopped replies before `stop_loop` applies. |
| `coaching_stop_window_minutes` | 20 | How far back `stop_loop` counts them. |
| `coaching_big_paste_tokens` | 10000 | A message's size before `big_paste` applies. |
| `coaching_cold_min_tokens` | 20000 | The smallest context `cache_cold` mentions. |
| `coaching_clear_context_tokens` | 100000 | Context before `clear_context` applies. |
| `coaching_cooldown_minutes` | 30 | How long a hint rests once shown. |
| `coaching_rearm_factor` | 1.5 | How much what's at stake must grow to end the rest early. |

## What it costs

A note is about 50 to 120 tokens, written to the prompt cache once and read on
every later reply of the session, like any other context. When Claude
mentions a hint, that's one more line of output. Claude Code also waits
for the hook after each shell, read, search, web or MCP result and each
message you send: a few tens of milliseconds, a little more when the
hook reads the end of the transcript.

`capture status` and Setup › Capture show how many notes there were over
the last 14 days, of which hints, and what they cost. They count towards
`capture-hook.py`'s line in the Your hooks table, never towards capture's
own note count or tag coverage.

## What it doesn't do

Coaching notes never change your settings, run `/clear` or start an
agent: Claude can only follow a hint within the task you gave it, or
tell you. The hook reads the end of the session's transcript (and a
subagent's own, for `split_run`) and keeps a small state file,
`coach-state.json`, holding when each hint last showed in each session
(by a salted hash of its id, as the free signals keep it) and how many
replies each subagent run has made.
Entries older than a day are dropped.

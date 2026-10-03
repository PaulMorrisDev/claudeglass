# Coaching notes

ClaudeGlass's tips are worked out after the fact, from sessions that have
already ended. Coaching notes bring the ones that can be acted on in the
moment into the session itself. When a hint applies, the capture hook
(`capture-hook.py`) adds a short note to Claude's context, and Claude
acts on it or tells you in a highlighted tip. For a tip, the note's
first sentence tells Claude to write it and its last line is the tip
itself, so it reaches you in every app.

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

Each note starts `cg-coach v1 <hint>`, so ClaudeGlass can find it in your
transcripts again and measure what it cost. A note never carries a path,
a command or your words: only token counts, an idle time, a count and an
agent type's name.

| Hint | When | What the note asks of Claude |
|---|---|---|
| `plan_fresh` | You approve a plan, and building it in a fresh session would drop at least 40,000 tokens of planning context. You approve it in the dialog, by typing a go-ahead after the dialog sent it back, or by leaving plan mode. | Tell you in a tip that building it in a fresh session would carry that much less on every reply of the build: next time, pick the approval option that clears the context first (the desktop), or run `/clear`, then ask Claude to carry out the plan in its file. Then carry on. |
| `plan_fresh_early` | You send a message in plan mode, which Claude Code reports with the message, with at least 40,000 tokens of planning chat in the session since it started: the plan isn't written yet. Not for a message sent while Claude is working, for a session that was compacted since it started, or when the session's start can't be told. It shares its rest with `plan_fresh`, so one plan gets one of the two. | Nothing about the work. End the plan Claude submits for approval with a tip to approve it with a clear context: on the desktop, the approval option that clears the context first (or `/clear`, then asking Claude to carry out the plan in its file, if the dialog has none); in the terminal, `/clear` first, then the same. If the reply doesn't end in a plan, the tip goes on the plan submitted later. |
| `split_run` | A subagent run passes the number of replies your own history says its type's runs are best split at (see [below](#your-own-split-points)). | Nothing: the subagent is never told. You get a one-line notice, once a run, that runs of that type cost you less when split, so next time you can give each agent a smaller piece of the work. |
| `quiet_output` | A read, search or web result is about 8,000 tokens or more, [as measured below](#how-a-results-size-is-measured). A read already given a line limit is left alone. Never after a shell command or an MCP tool. | Next time, ask for less: read only the lines needed, narrow a search or a query. |
| `drip_feed` | You send your third small change request in a row: short messages, each asking for a change (a change verb opens one of its sentences) and sent within 20 minutes of your message before it, the earlier ones each answered with a change to a file of yours in the reply they started ("make the button bigger", "now move the logo", "and make the footer grey"). A go-ahead, a thank-you, a status check, a question, a statement, an explain request and an answer to Claude's question ask for no change: they neither count nor end the run. A detailed message, a reply that changed nothing, or a longer gap starts the count again. A message sent while Claude is still working gets no hint. | Nothing about the work. End the reply with a tip that working out everything the work still needs and sending it as one message gets it done in one pass. |
| `big_paste` | You send a message of 10,000 tokens or more, such as a pasted log or file. | If most of it is a log, a file or output, suggest in a tip pasting only the part that matters, or saving it to a file and giving the path. |
| `status_poll` | You send a message that only asks how the work is going ("how is it going?", "any updates?", "is it done yet?"), while a tool result said work went to the background and no message from that task has arrived since, and the prompt cache is still warm. A message sent while Claude is still working gets no hint. | Nothing about the work. End the reply with a tip that each check makes Claude read the whole session, about that many tokens, to say little that is new, and where to look instead: the task panel on the desktop, `/tasks` in the terminal. It never says a notification will come. |
| `cold_return` | You send a message after the prompt cache expired (5 minutes idle, or an hour when the session uses the 1-hour cache), and the last reply left 100,000 tokens of context or more. A receipt for the break, whatever the message is. Not after a compaction since that reply, for a message sent while Claude is still working, for one you didn't type, or when a usage limit stopped Claude and you come back within a cache lifetime of its reset (or the reset is unknown). It rests 12 hours. | Nothing about the work. End the reply with a tip saying how long the session sat idle, that the reply wrote about that many tokens of context again (the context less the 42,000 tokens every session starts with, and "in a session already compacted twice" when it was), that the last reply or the task panel already says whether the work is done, and that `/clear` first skips the rewrite for new work. |

One note at most per tool result or message: the first hint in the table
that applies. `plan_fresh` (a plan approved in the dialog), `split_run` and
`quiet_output` come after a tool result, the rest when you send a message
(`plan_fresh` too, for a go-ahead you type). `split_run` comes from inside
a subagent run but only
shows you a notice; the rest show only in the main session, except
`quiet_output`, which shows in both. The prompting hints (`drip_feed` and
`big_paste`), `status_poll` and `cold_return` are about how you prompt,
not about the work, so they never change what Claude does: it handles the
message as it would have and only ends its reply with the tip.
A message you didn't type gets no hint: a background agent's report
(which Claude Code hands to Claude as the next message), a scheduled
task, a slash command's output, another session's message, the desktop
app's note after a usage limit or a quit. Nor does a message you send
while Claude is still working (the last line of the transcript is a tool
call or a tool's result, less than 10 minutes old): it nudges work under
way, and the reply to it isn't one that ends the turn. An older last
line is where the work stopped, so a message typed after it is yours to
act on. The exception is a call that launched a subagent and has no
result yet: a subagent runs as long as it needs, so a message typed
meanwhile is a nudge whatever the call's age. A replay of 30 days of one
author's sessions found 5 messages
typed 10 minutes to 2 hours after a tool result that were wrongly read
as queued, so the hint never reached them.

## How a tip looks

A tip is a line Claude writes. Every note that carries one is built the
same way: its first sentence tells Claude to write the tip, and its last
line is the tip itself, word for word, behind the label **ClaudeGlass
tip:**. Claude copies that line to the end of its reply, after a blank
line and before its tag, in a quote block, so it stands apart from the
work in the terminal and in the desktop app:

> **ClaudeGlass tip:** That's 3 small changes in a row, each its
> own message, and each one re-reads the whole session, about 150k
> tokens. Working out everything the work still needs and sending it as
> one message gets it done in one pass, for fewer tokens.

The reply is the one place every app shows, which is why the tip rides
in it. Two kinds of note differ only in the first sentence:

- **Relayed.** The note tells Claude to write the tip every time:
  `drip_feed`, `status_poll`, `cold_return`, `plan_fresh` and
  `plan_fresh_early`. For `plan_fresh` Claude writes it before it starts building; for
  `plan_fresh_early`, as the last line of the plan it submits. A plan that
  ends with the tip counts as having relayed it.
- **Judged.** The note tells Claude to write the tip only if it judges
  your message calls for it, and to say nothing otherwise: `big_paste`
  (is most of it a log or a file?).

Where Claude Code shows hook messages, the same tip also appears at once,
before Claude replies. In the terminal it reads like this, Claude Code
adding the event's name in front:

```
UserPromptSubmit says: ⚠️ ClaudeGlass: That's 3 small changes in a row, each its own message, and each one re-reads the whole session, about 150k tokens. Working out everything the work still needs and sending it as one message gets it done in one pass, for fewer tokens.
```

This is the hook's `systemMessage`: Claude Code shows it to you and never
sends it to Claude, so it costs no tokens. It has the tip's words behind
a ⚠️ label. `plan_fresh`, `plan_fresh_early`, `drip_feed`, `big_paste`,
`status_poll` and `cold_return` have one. The figures in a
tip, such as the context in tokens, come from the session. The hook never
works out a dollar amount: that would need prices it doesn't have.

The desktop app's Code tab doesn't show a hook message that way. It folds
it into a collapsed "Claude Code notice" row of the run summary, and never
shows one that comes from inside a subagent. So when the hook runs from
the desktop app (Claude Code sets `CLAUDE_CODE_ENTRYPOINT` to
`claude-desktop`), it sends no notice for a hint that has a note, and the
tip reaches you through Claude's reply alone. `split_run` has no note to
Claude, so it keeps its notice everywhere, though the desktop app doesn't
show it.

Every hook entry that can return a note or a notice runs in the
foreground. Claude Code doesn't wait for a hook registered async, and its
output only arrives on the next turn: a tip for the message you just sent
would reach Claude a whole reply late. A test holds the entries to that.
The one background entry is `Stop`, which prints nothing: when a turn ends
it only keeps the newest reply's time and size (see
[Cold returns](#cold-returns-and-status-checks)). `claudeglass capture
connect` adds it; until you run that again, `cold_return` works from the
end of the transcript alone and Setup › Capture shows a "Needs a hook entry" chip.

The /cg-feedback reminder (`feedback_reminder`) has the same look,
labelled **ClaudeGlass:**.

Nothing ClaudeGlass asks Claude to write carries an emoji. Claude copies
what its context shows: with a ⚠️ and a 💡 in these labels, it began
using them as markers of its own in unrelated work. The notice above is
shown only to you and never reaches Claude, so it keeps its ⚠️.

## A subagent that compacts

The session note asks for tags, and a subagent is asked for nothing. When
a subagent compacts, Claude Code runs the hook's SessionStart entry for
it, with the main session's transcript path and no field saying whose it
is. The hook treats a compaction as the subagent's, and adds no note,
only when a file under the session's `subagents` folder (a workflow's
agents sit a level deeper) recorded a `compact_boundary` in the last
five seconds. Claude Code writes that line a fraction of a second
before it runs the hook, in every case audited. The main session's own
boundary usually isn't written yet, so the hook never waits for one: any
other compaction is the main session's and gets the note. The note says
"If you are a subagent, ignore this note", for the rare compaction that
check misses.

To see whether Claude Code ever sends an agent field in that payload,
the hook writes the key names of each kind of SessionStart payload to
`payload-keys.json`, once each: names only, never a value, a path or an
id.

## How your messages are read

`drip_feed`, `status_poll`, `cold_return` and `plan_fresh_early` read the
message you're sending and your earlier ones at the end of the
transcript: up to the last 4 MB of it, since a long tool result can push
the last reply far back. Lines the
desktop app wrote again when it resumed a session (the same `uuid`, or a
time behind the lines already written, a message you typed aside) are
left out first, as they would put an old reply last. No hash of your
words is kept for any of this.

- `drip_feed` goes by what happened more than by your words: each
  message's length, how soon after your message before it you sent it,
  and whether Claude changed a file of yours in the reply that message
  started. That is an edit call or a shell write outside a `.claude`
  folder (Claude's memory, plans and scripts aren't your work), or a file
  one of its subagents changed, less an edit that failed. A subagent's
  files go to the message whose reply launched it, and what Claude does
  after a line you didn't type (a background agent's report, a scheduled
  run) is nobody's request. A small request is 300 characters or less; a
  longer one usually plans several changes at once, which is what the
  hint asks for. Words matter only to find a request for a change: a
  change verb opens a sentence ("make", "add", "move", "can you rename",
  read in the first 600 characters). A bare "thanks" or "ok", a go-ahead
  ("continue"), a status check ("how's it going?"), a question (it ends
  in a "?" or opens with a question word), a statement or a report ("the
  button is too small", "it doesn't load") and a request to explain or
  look at something ask for no change, so none counts and none ends the
  run. Nor does a reply to a question Claude asked: its last reply ends on
  one (a "?" closes one of its last two sentences or a list item that ends
  it, not code, a link or its tag) and changed no file. A question that
  closes a reply that changed a file is an offer, so the message after it
  is a request like any other. A session's first message never counts:
  nothing came before it. A message you send while Claude is still
  working (the last line of the transcript is a tool call or a tool's
  result, less than 10 minutes old) is a nudge into work under way and
  gets no hint.
- `plan_fresh_early` goes by the permission mode Claude Code reports with
  your message (`plan`) and the planning chat so far: the last reply's
  context less what the session started with (the first reply's context,
  less your first message). It says nothing when that start can't be
  told (the first reply is too far into the transcript to read) or when
  the session was compacted since it started. A plan you then approve by
  typing a go-ahead, or by leaving plan mode, is `plan_fresh`'s: the
  same rest.
- `big_paste` goes by the message's length alone.

Slash commands, stopped replies and a subagent's messages don't count.
Nothing about your words is kept or passed on: the note says only how
many.

The coaching line in the status line shows `drip_feed` and `big_paste`
too, at their default thresholds.

## Cold returns and status checks

`cold_return` is a receipt, not a judgement. When the gap since the last
reply of Claude's in the main chain is longer than its prompt cache
lasts (5 minutes, or an hour when a recent reply wrote to the 1-hour
cache) and that reply left 100,000 tokens of context or more, the next
reply has to write that context again, and the note says so whatever you
are asking. The figure is the context less the 42,000 tokens every
session starts with, since that part is written again for everyone. It
stays quiet when a compaction came after that reply (the summary is
what is written now), for a message sent while Claude is working or one
you didn't type, and for 12 hours after it showed, however far the
context grows. It shares one rest with `plan_fresh` and
`plan_fresh_early`: each says "start fresh", so one that showed rests
the others for the cooldown.

A usage limit is not a break you took. When Claude Code stops with a
"You've hit your session limit" or "weekly limit" line after the
last real reply, the wait was the limit's, and the cache couldn't have
outlived it. In a replay of 30 days of one author's sessions, `cold_return`
was right in all 10 of its firings, but 27% followed such a stop and told
the author nothing new. So it stays quiet when you come back within
a cache lifetime (5 minutes, or an hour on the 1-hour cache) of the
limit's reset, or when the line doesn't say when the limit resets. Later
than that the limit's wait is over, and the break is read like any
other. Only the time is read from the line, never its words.

The time of the last reply is not read from the transcript's end alone.
A usage-limit line, an overload or any other line Claude Code writes in
place of a reply is no reply, whatever tokens it carries, so a turn that
ended in one never restarts the clock. And the desktop app can write old
lines again at the end of a transcript. So `Stop` and each read, search or web result keep
the newest real reply's time, the context it left and its cache lifetime
in `coach-state.json`: three numbers, no text, never moving back to an
older reply. The receipt is timed from the newer of that and the
transcript's own last reply. When nothing was kept and the transcript
ends in a replay, it stays silent: better no receipt than one for a
break that didn't happen.

`status_poll` is for the other way people spend a cold or a warm
session: asking how it's going. It shows only when a tool result said
that work went to the background and no message from a task has come in
since, so Claude is carrying on without it. The result's own wording is
matched in memory, in its first 400 characters, and dropped:
"Command running in background with ID", "was moved to the background",
"Async agent launched successfully" and "Workflow launched in
background". The call's `run_in_background` setting is not what
counts, as an agent or a workflow goes to the background without it. A
task's message ends the wait in any of the three shapes Claude Code
writes it. The tip never says a notification will come: a background
task sends one only when it finishes, and not every kind does. After a
break that outlasted the cache, `cold_return` speaks instead.

## Hints that no longer show live

Seven hints once spoke up in the session and now don't. Their old notes are
still recognised in earlier transcripts, so what they cost is still
counted.

- `clear_context` told you to start fresh when a message arrived with
  100,000 tokens or more of context. It fired on every long session,
  whether or not the message began something new. **How you prompt**
  now has a row, "Context carried into new pieces": a message the
  reply's tag calls a new task, or one sent after a break of over an
  hour with no tag saying the work went on, counted with what its
  replies paid to read the earlier work again. `cold_return` covers the
  case where the break let the cache expire.
- `explore_reads` counted 8 reads and searches for one message. Reads
  are cheap next to the context an agent re-reads, so Work habits now
  has an **Explore cost by model** table instead: what the Explore
  agents you started cost, per model, with the context each read. A
  workflow's agents are left out.
- `repeat_ask`, `stop_loop` and `vague_fix` fired on polls, go-aheads,
  refusals and questions far more often than on the habit, so they are
  counted after the fact only, with stricter rules (see below), and show
  no note.
- `plan_first` was right in none of 7 firings in a replay of 30 days of
  one author's sessions, which is more wrong than right, so it is counted
  after the fact only, with a stricter rule (see below), and shows no
  note. With that rule, 12 messages asked for 3 or
  more changes and every one was left alone rightly: a long brief (5), a
  message that mentions a plan (5), one already shaped like a plan (1)
  and a merge request (1). That author writes plans upfront, so the hint
  had no real chance to help. Its row keeps the advice.
- `cache_cold` left it to Claude to judge, after any break, whether your
  message started something unrelated, with 20,000 tokens of context. It
  was mostly silent, and a break's cost is the same whatever the message
  is. `cold_return` replaced it: a receipt for every break with 100,000
  tokens or more, with the cost stated less the shared start.

`drip_feed` is not one of these: it stays live, with a tighter rule. A
replay of 30 days of one author's sessions found it right in 2 of 11
firings with the rule it had, which is more wrong than right. With the
tighter rule (see above and below), the same replay fired it 0 times:
630 messages reached the check and 43 were short requests for a change,
but no run got past one request. 23 stopped there, because there was no
earlier request (14), the earlier one had no file change behind it (7)
or the earlier one was too long (2). So it now speaks only on a clear
run of small change requests, and **Tips Claude showed** counts each
time, with any misfire.

## How a result's size is measured

`quiet_output` and the capture note for **Large tool outputs**
(`big_output`, which asks Claude how much of a large result it needed)
go by what Claude reads: the result's characters at four to a token,
worked out in memory from what the hook is handed, and dropped.

- **Words only.** A file's contents, a search's matches or file names,
  a page, a command's output. A field that only shows you a change (a
  diff, the original file, a patch) never reaches Claude, so it isn't
  counted.
- **A picture counts for its tokens, at most 1,600.** A screenshot's
  size is mostly its encoding. Its tokens come from its width and
  height, at 28 pixels a patch, and never more than 1,600, so a picture
  alone never reaches 8,000 tokens. Words beside it still count.
- **A result Claude Code saved to a file counts for its preview.** A
  search past about 20,000 characters, a web page past about 50,000 or
  a command past about 30,000 is written to a file, and only a preview
  of about 2,200 characters reaches Claude. Those limits are what real
  transcripts show; a read is never saved. So a search result is saved
  before it is large enough, and in practice `quiet_output` follows a
  large read or web page.
- **A context rests on its own.** The main session and each subagent
  keep their own rest for `quiet_output` (see below), since one's big
  result is no reason to stay quiet to the other.

## After the fact

**How you prompt**, on Work habits, counts these habits in all your
sessions, whether or not coaching notes were on, with what each cost,
its trend by week and what to try instead: `status_poll`, `drip_feed` and
`big_paste` (the three a live hint warns about), and five counted after
the fact only.

- **Asking how it's going** (`status_poll`): every message that only
  asks how the work is going ("how's it going?", "any updates?", "is it
  done yet?"), whether or not work was running in the background. Each
  is priced from the reply it drew, which read the whole session to say
  little. The figure used to be `repeat_ask`'s: every one of its live
  firings was a poll. The row tells you to look at the last reply or the
  task panel first.
- **Small requests sent one at a time** (`drip_feed`): three or more
  short requests for a change in a row, 300 characters or less each, each
  sent within 20 minutes of your message before it and each answered with
  a change to a file of yours ("make the button bigger", "now move the
  logo", "and make the footer grey"). The cost is the context each later
  message made Claude read again. A longer message usually plans several
  changes at once, which is what the row advises, so it ends the run.
  - **The clock is yours.** The 20 minutes run between your own messages.
    Claude's reply to a background agent's report or a scheduled run is
    not a message of yours and restarts nothing.
  - **A change is the reply's own.** A message is credited only with the
    edits made in the reply it started, in the main session. A subagent's
    edits go to the message whose reply launched that subagent, even if it
    finished after the next message was sent. Edits inside a `.claude`
    folder (memory, plans, scripts) are not your work.
  - **Only a request for a change counts.** A change verb opens a
    sentence: "make the button bigger", "can you add a footer", "now move
    the logo". A statement ("the button is too small"), a report ("it
    doesn't load"), a request to explain or clarify, a question (it ends
    in a "?" or opens with a question word), a bare "thanks" or "ok" and a
    status check ("how's it going?") ask for nothing to change, so none
    counts and none ends the run.
  - **A go-ahead is the next step.** "Continue", "go ahead", "yes, do
    it", "merge it", "push and release", "run the tests", "ship it" and
    "do 1 and 2" carry on work you already asked for. They neither count
    nor end the run. Naming something of your own ("merge the auth logic
    into the helper") is no go-ahead, but it opens with no change verb
    either, so it neither counts nor ends the run. "Try again" is left
    out: it says the last try went wrong.
  - **An answer is skipped.** So is a reply to a question Claude asked:
    its last reply ends on one (a "?" closes one of its last two
    sentences or a list item that ends it, not code, a link or its tag)
    and changed no file. A question that closes a reply that changed a
    file is an offer, so the message after it is a request like any
    other. A message stopped before any reply and sent again is skipped
    too. The first message of a run is the first request.
- **Big tasks without a plan** (`plan_first`): a request for 3 or more
  separate changes in your own prose, 150 characters or longer, sent
  outside plan mode before any plan was approved in the session. It has
  no cost figure, so the page shows "Not priced". The changes are
  counted in what you wrote, leaving out a fenced block, a quoted line
  and a pasted log or stack trace: its list lines, its change verbs
  ("add", "move", "rename" and the like) and the items of one sentence
  that starts with one ("Add login, a settings page and an admin screen"
  is three). A message with no change verb asks for no change, whatever
  it lists. One request written with several commas or "and"s can count
  as three: at 3 changes, 6 of the 2,755 messages in one author's 221
  sessions did.
  - **A long message is a plan.** A message of 2,000 characters or more
    has been written out, whatever its formatting. Five listed items are
    a plan too: lines that start "1." or "1)", lines that start "-" or
    "*", or "1)" numbering inside one paragraph. A heading and 1,500
    characters or more is a plan as well.
  - **A message about a plan is left alone.** So is one that opens by
    asking to review, explain, check or look at something.
  - **A paste is not a request.** A message that holds a fenced block, a
    stack trace or error line, or the "[Pasted text" marker the desktop
    app writes for a long paste is yours to read, not a list of changes.
  - **A merge or a release is the next step.** A message in which a
    sentence or clause asks to merge, ship, deploy, publish or release,
    or to push or tag something, is work you already have, not a piece
    of work to plan first, however many steps it lists. A mention
    further in ("before the release next week") is no such request.
- **The same request again** (`repeat_ask`): you sent much the same
  request as one Claude answered with a file change in the last hour (80%
  of its words in common, at least 4 different words). A poll ("how's it
  going?"), a go-ahead ("continue"), a thank-you, a message you stopped
  before any reply and one sent after a reply that failed (an API error,
  an overload, a usage limit) are never a repeat (a poll is a
  `status_poll`). The cost is the attempt that missed.
- **Stopping Claude again and again** (`stop_loop`): you stopped Claude
  three times in 20 minutes, with Esc on a reply or before it started
  (which puts your message back to edit). A stop that is the tail of a
  call you turned down, a plan you sent back, a question you declined or
  a hook's block, and the session's end, are not counted.
- **Vague corrections** (`vague_fix`): a message of 80 characters or
  less that says something went wrong ("it's still broken", "that didn't
  work", "no change") but names nothing specific and doesn't say what
  it should be instead. A bare "fix this" says nothing went wrong, so it
  doesn't count; nor does a question, a go-ahead, a thank-you, a message
  with an image, or a retry after a reply that failed. It has no cost
  figure, so the page shows "Not priced".
- **Context carried into new pieces** (`context_carried`): as above.
  Only the message that began the piece counts, and only when at least
  20,000 tokens of earlier work were in context.

The thresholds for these five are fixed, not set in `config.toml`.
Once there are coaching
notes, **Tips Claude showed** says, for each hint, how many notes there
were and how many tips showed: "relayed 4 of 5" for a hint whose note
tells Claude to write the tip every time, so a tip left out was missed,
and "judged relevant 2 of 6" for one Claude decides on, where a tip left
out isn't a miss. A last column counts the replies that called the tip a
misfire ("that ClaudeGlass tip doesn't apply"), which says a hint is
firing when it shouldn't. Turning coaching notes on is a change on **Your changes**,
measured by the habits a live hint warns about, per 100 of your messages
before and after.

Once a hint has shown, it rests for 30 minutes in that session, unless
what's at stake has grown one and a half times since (a context grown
from 100,000 to 150,000 tokens, say). A hint that keeps coming back is
one you have decided to ignore, so each time it shows again in a session
its rest doubles: 30 minutes, then 1, 2 and at most 4 hours. `cold_return`
rests a flat 12 hours and growth never wakes it. `quiet_output` rests, and
backs off, apart for the main session and for each subagent. `split_run`
shows once a run.

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
  [plan-handoff tip](plan-handoff.md), or most of your /cg-feedback
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
| `coaching_plan_fresh_tokens` | 40000 | Planning context kept after a plan (or held so far, in plan mode) before `plan_fresh` and `plan_fresh_early` apply. |
| `coaching_quiet_output_tokens` | 8000 | A result's size before `quiet_output` applies. The large-output note's 8,000 is fixed. |
| `coaching_drip_count` | 3 | Small requests in a row before `drip_feed` applies. |
| `coaching_drip_window_minutes` | 20 | The longest wait after your message before it for a message still to count. |
| `coaching_drip_chars` | 300 | The longest message that counts as a small request. |
| `coaching_big_paste_tokens` | 10000 | A message's size before `big_paste` applies. |
| `coaching_cold_min_tokens` | 100000 | The smallest context `cold_return` mentions. |
| `coaching_cold_rest_hours` | 12 | How long `cold_return` rests once shown. |
| `coaching_warm_prefix_tokens` | 42000 | The tokens every session starts with, left out of `cold_return`'s figure. |
| `coaching_queued_minutes` | 10 | How old the last tool call or result may be for a message you send to count as sent while Claude is still working. |
| `coaching_cooldown_minutes` | 30 | How long a hint rests once shown. |
| `coaching_rearm_factor` | 1.5 | How much what's at stake must grow to end the rest early. |
| `coaching_max_backoff` | 3 | The most times a hint's rest may double. |

An older `config.toml` may still hold keys for the hints that no longer show
(`coaching_explore_reads`, `coaching_clear_context_tokens`,
`coaching_vague_fix_chars`, `coaching_repeat_similarity`,
`coaching_repeat_window_minutes`, `coaching_repeat_min_words`,
`coaching_stop_loop_count`, `coaching_stop_window_minutes`,
`coaching_plan_steps` and `coaching_plan_min_chars`). They are ignored:
those habits are counted after the fact with fixed rules.

## What it costs

A note is about 50 to 140 tokens, written to the prompt cache once and read on
every later reply of the session, like any other context. When Claude
mentions a hint, that's one more line of output.

Claude Code also waits for the hook, a few tens of milliseconds a call,
after each message you send and after each read, search or web result
and approved plan. It is never run after a shell command or an MCP tool.
Those were about two thirds of the calls it waited for, and a replay over
real sessions found a size note after them wrong too often: after a shell
result it failed the precision and the tokens-against-time checks, and
after an MCP result it was right 33% of the time (46% counting half
credit) where 50% was the bar. A subagent that only runs commands
therefore never reaches `split_run`, which counts its replies when the
hook runs.

The hook is three files in ClaudeGlass's data folder, under `hooks/`.
`capture-hook.py` is a small launcher, the file `settings.json` runs;
`capture_hook.py` holds the code; `capture-catalogue.json` is the word
list both read. Python keeps the compiled form of a module it imports
and none of a script it is started on, so the launcher is what lets a
call skip compiling the hook's code each time. A call also imports only
what it needs, and returns before anything else when a result is smaller
than any note could apply to. On the author's Windows machine a call took
about 62 milliseconds as one script and takes about 44 now, python's own
start being about 12. `claudeglass capture connect`, `update --finish`
and the dashboard's start put the three files in step, the launcher last
and only once the module is there. An install from before the split
keeps working with its one file until the next of those, and a
`settings.json` that still matches the shell and MCP tools starts the
hook after them, which then returns at once (about 31 milliseconds)
until `update --finish` or `capture connect` rewrites the entry, after
you say yes.

`capture status` and Setup › Capture show how many notes there were over
the last 14 days, of which hints, and what they cost. They count towards
`capture-hook.py`'s line in the Your hooks table, never towards capture's
own note count or tag coverage.

## What it doesn't do

Coaching notes never change your settings, run `/clear` or start an
agent: Claude can only follow a hint within the task you gave it, or
tell you. The hook reads the end of the session's transcript (and a
subagent's own, for `split_run`; and, at a compaction, the end of any
subagent file that changed in the last five seconds, for its record type
and time only) and keeps a small state file,
`coach-state.json`, holding when each hint last showed in each session
(by a salted hash of its id, as the free signals keep it), how many
times, how long it rests, the time, context and cache lifetime of the
newest reply (numbers only), and how many replies each subagent run has
made. Entries older than a day are dropped.

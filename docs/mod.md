# The ClaudeGlass mod

The ClaudeGlass mod is a small plugin that runs inside Claude Code and
draws its own screens there. It is built around one moment: Claude
summarises the conversation. The mod tells every summary what must survive,
shows what the summary lost, and puts it back in one press. A one-line band
above the prompt says how the session stands. Before you send a message, a
hint line can speak up too. The dashboard stays the history. The mod is the
live half.

It exists because the dashboard shows what already happened, and a hint
passed on through Claude reaches you unreliably. In an audit of the
author's sessions in the Claude desktop app, Claude passed a coaching tip on
to you about 1 time in 6. The mod draws its hints itself, so they
arrive every time and cost no context.

The mod acts only on a press, with one exception: every summary is told your
keep list. It never changes a setting, and it never sends a message for you.
[What it never does](#what-it-never-does) lists the rest.

## Adding it

You need Claude Code 2.1.286 or later. At a Claude Code terminal prompt,
type this once:

```
/plugin install claudeglass --marketplace PaulMorrisDev/claudeglass
```

Claude Code asks whether to add the marketplace. Press `y`. The plugin
installs at user scope, so it loads in every project. It then loads in
sessions in the desktop app's Code tab too.

`claudeglass update --finish` prints this line when the mod isn't running
yet, and never installs it for you.

To run it from a folder, for development or from a release zip, unzip
`claudeglass-mod.zip` from the
[Releases page](https://github.com/PaulMorrisDev/claudeglass/releases)
into a folder. A checkout's `plugin/` folder works too. Then
start Claude Code in one of two ways:

- Set `CLAUDE_CODE_PLUGIN_DIRS` to the folder before it starts.
- Run `claude --plugin-dir <folder>`.

To update it, run `claude plugin update claudeglass`. To remove it, run
`claude plugin uninstall claudeglass`. Removing the mod leaves
ClaudeGlass's other parts as they were. The capture hook goes back to passing
on every hint itself ([With and without the mod](#with-and-without-the-mod)).

The mod reads a settings file that ClaudeGlass writes
([What it reads and writes](#what-it-reads-and-writes)). `update --finish`
writes it, and the dashboard's service keeps it fresh. Until it exists the
mod falls back on the rate card built into the plugin and shows no coaching
hints.

## What you see

Each surface answers one question you would act on. Anything that wouldn't
change a decision lives on the dashboard.

| Surface | Where | What it is for |
|---|---|---|
| The band | Above the prompt | How the session stands, and the one thing to do about it |
| The Checkpoint pane | `/glass`, or the band's Glass button | What the next summary must keep, and what the last one lost |
| The Startup pane | `/glass startup`, or Open Startup in the Checkpoint pane | What loads before you type |
| The hint line | Under the prompt, while you type | A costly send, before it is sent |
| The summary line | In the conversation | One line when Claude has summarised |
| The toast | A brief message | A summary lost something |
| The spinner and status line | While Claude works | How full the context is, and what can't wait |

Amounts follow your billing mode. They appear in the hint line, the send
guard, the spinner and the detailed summary line, and nowhere else in the mod:

- **API billing** shows dollars, such as `$0.38`.
- **A subscription** shows a share of your 5-hour limit when Claude Code
  reports it, such as "about 0.4% of your 5-hour limit". Otherwise it shows
  a share of your weekly limit, worked out from your history.
- **When neither share is known**, it shows the list-price equivalent, never
  a bare dollar sign.

The examples below use API billing.

### The band above the prompt

The band is the glance. It is one line: a dot, a state word, a short
sentence, and one button on the right. The button changes with the state.

```
●  Fine     34% full · about 11 replies before a summary                       [ Glass ]
●  Tight    71% full · about 4 replies before a summary      [ Summarise, keeping 9 items ]
●  Review   Summary lost 3 of 9 items                                    [ Re-add 3 items ]
●  Kept     Summary kept all 9 items                                               [ Glass ]
```

| State | Colour | When | Button |
|---|---|---|---|
| Fine | Green | The context is under 75% of the way to the point where Claude summarises | **Glass** (`g`) opens the Checkpoint pane |
| Tight | Amber | The context is 75% or more of the way there | **Summarise, keeping 9 items** (`s`) starts a summary now |
| Review | Red | The last summary lost something you said, you haven't looked, and nothing is put back yet | **Re-add 3 items** (`r`) |
| Kept | Green | The last summary kept everything you said. It shows until the next reply ends, then the band is Fine again | **Glass** (`g`) |

The word and the dot give the state, so colour is never the only signal.
"% full" matches `/context`. The summary point is the one Claude Code uses,
including your `autoCompactWindow` setting. Before Claude has a count of
replies, the line says "Claude summarises at 160k" instead. If you have turned
auto-compact off in Claude Code, no summary will happen on its own, so there
is no point and no count of replies. The line is "% full" followed by "Claude
won't summarise by itself", and Tight means the window itself is 75% full. The
Checkpoint pane's header says the same.

Pressing Summarise starts the summary Claude Code would start with
`/compact`, with the keep list as its instructions. It does nothing while a
reply is running, and the button is dimmed. A summary that another hook
holds says why in a toast. Opening the Checkpoint pane counts as having
looked, so Review gives way to Fine or Tight. With the hold switched on
([Holding an automatic summary](#holding-an-automatic-summary)), the Tight
button reads **Review before summarising** and opens the pane.

On a narrow screen the sentence shortens first, then goes, and the state
word and the button stay. The band shows after Claude's first reply in a
session. It stays out of the way of a survey.

When a summary loses something you said, a toast says so once: "The summary
lost 3 of 9 items. Press Re-add to put them back." It shows only when
something was lost and you haven't opened the pane since.

### While Claude works

The spinner gains a suffix: `· 60% full · +$0.04`. It says how full the
context is and what this reply has cost so far. On a subscription it shows
only the first part.

### The hint line before sending

Under the prompt, while you type, one line can appear. It is the one
moment a send can still change. It shows one item at most, the first that
applies:

| Hint | When it shows |
|---|---|
| Small changes in a row | Your third small change request in a row, each its own message. Listing the rest in one message costs less. |
| Large paste | The draft is about 10,000 tokens or more. If it's a log, paste only the part that matters. |
| Background work | Work is still running in the background and you are about to ask how it's going. Asking re-reads the whole session. |
| Cache expired | You have been idle past the cache lifetime, and the context is 100,000 tokens or more. It says what this send costs, and what `/clear` first would cost. |
| New topic | The draft is a new request that shares no words with the session, and the session carries 20,000 tokens or more beyond its startup. It says what each later reply would save after `/clear`. A message that carries on ("commit this", "now run the tests") isn't a new topic. |
| No file named | You ask for a change in a session that carries under 20,000 tokens and has touched no file, and name none. |
| Read everything | You ask Claude to read the whole codebase. Asking for one folder or file, such as "read all of src/api", doesn't count. |

With `/glass detail` on, a warm send says what it costs ("Sending costs
about $0.07") when nothing else needs saying.

The first four are the coaching hints the capture hook used to give after
you sent the message, when the cost was already paid. They use the same
thresholds, wording, rest rules and `muted` and `once` lists as the hook
([Coaching notes](coaching.md#when-the-claudeglass-mod-is-running)). The last
three are the draft checks, which only the mod makes. All of them need
**coaching notes** turned on, in a project the coaching setting covers
(`claudeglass capture enable coaching_notes`).

A hint rests after it shows: 30 minutes, doubling each time it returns, up
to 3 times. It comes back early only if what is at stake grows by half. A
cache-expired hint rests 12 hours. While it applies, the hint stays on
screen without asking the rest rule again.

The mod looks at your draft's length and shape. It keeps the words of a draft
in memory only, to compare them with the session's, and never writes them
down.

### The summary line

When Claude summarises the main session, the mod checks the summary against
what you said, and writes one line in the conversation:

- `● Glass · Claude summarised the conversation. Everything you said is in
  the summary.`
- `● Glass · Claude summarised the conversation. 2 things you said may be
  missing.` It then points you to the band.

With `/glass detail` on, the line adds the context before and after, such
as "150k tokens became 30k tokens", and what the summary cost when Claude
Code reports it. Nothing shows when you said nothing worth keeping. A summary
a subagent runs for itself is left alone.

### The status line

The status line carries what can't wait. It is set when the 5-hour limit is
forecast to run out within 15 minutes, or when one reply or none is left
before Claude summarises. It is cleared when neither holds, when the session
ends and on `/clear`. While the limit forecast is on the line it is looked at
again every minute, so the minutes follow the clock even when you are idle.

When you approve a plan after a lot of planning, the status line says a
fresh session would save tokens on every reply. That tip has the line while
it shows, and nothing here overwrites it. It goes at the next reply or
summary. Leaving plan mode counts as approving.

## The Checkpoint pane

Type `/glass`, or press **Glass** on the band. It works while Claude is
still replying, and `Esc` closes it. It never opens by itself. There is one
view, with three sections in the order of the summary moment, and a footer.
Only the main buttons carry a key: `s` and `r`. Every other button is quiet,
and the focus keys reach it.

### The header

```
Context 71% full · about 4 replies before a summary     [ Summarise now, keeping 9 items ]
```

The same **Summarise now** button as the band, so there is one way to do the
one thing. It is primary until a re-add is on offer, and then steps back, so
two buttons never compete. While it runs it reads "Summarising…" and a line
says a summary is running. While a reply is running it is dimmed, with the
reason under it.

### Keep through the next summary

The keep list is what the summariser is told to carry. The mod builds it
locally as you work, with no model. Each row has a tick, the item and a
quiet kind label:

```
Keep through the next summary                                         9 items
[x] Never commit from an agent                                         pinned
[x] Use the Sonnet model for implementing, Opus for review       instruction
[x] plugin/hooks/compaction.ts                                          file
[x] Port 8792                                                          value
[ Pin the last thing I said ]                          [ Show 4 more ]
```

- **Order and kinds.** Pins come first, then what you asked, decisions,
  the last failed command, open tasks, files changed and values you typed.
  Items put back after a summary lost them come right after the pins.
- **Five rows show, ticked.** **Show 4 more** lists the rest, unticked.
  **Show fewer** puts them away.
- **Press a row to tick or untick it.** An unticked item is left out of the
  instructions for the next summary. The ticks start over after each
  summary and on `/clear`.
- **Pin the last thing I said** appears when one of your last few messages
  holds an instruction or a decision. It pins it as one line.
- **Unpin** sits at the end of a pin's row.
- With nothing to keep yet, the pane says: "Nothing to keep yet. Pins and
  instructions appear here as you work."

What the kinds mean:

- **Pinned**, which you chose. You can keep 20. They survive a summary and
  clear with `/clear`.
- **Instruction**: your messages with "never", "don't", "always", "must"
  or "only", and corrections such as "no," or "actually". A remark about
  yourself ("I don't see anything", "I don't know why") and a question are
  not instructions.
- **Decision**: "let's go with…", "agreed", and a plan you approved.
- **Error** (a shell command or test that failed and wasn't run again with
  success; Claude's other tool errors don't count), **task**, **file** and
  **value** (ports, versions, IDs, URLs and numbers). Files in temporary or
  scratch folders and the plan file are left out, and files in your project
  show their path from the project folder.

Some items survive any summary anyway: files among the last five Claude
changed, the approved plan, and anything in your project or user-level
CLAUDE.md. The mod reads those two files from disk to know this, in memory
only. They are never counted as lost.

### Last summary

This section shows only after a summary has happened in the session.

```
Last summary · 14:32 · automatic                          kept 6 · lost 3 · re-added 3
✗ Never commit from an agent                                re-added, referenced ✓
✗ Port 8792                                                  re-added, not yet seen
✗ Use Opus for review                                        re-added, referenced ✓
[ Re-add 3 items ]   [ Check with a model ]   [ Show 2 earlier summaries ]
```

- **The heading** gives the time and how the summary ran: automatic, you
  asked (`/compact`), or pressed here. The counts cover only what you said:
  pins, instructions and decisions. Files, values, tasks and errors are told
  to the summariser, but are not counted.
- **The rows** list what the summary dropped or kept only in part, five at
  most, then "and 2 more". ✗ is lost and ◐ is kept in part. When nothing was
  lost, one line says so.
- **The receipt.** Once an item is re-added, its row says how it fared:
  "re-added, not yet seen", "re-added, referenced ✓" or "re-added, not
  referenced". A receipt fills in as replies end. It reads "referenced" when
  a later reply, or a tool input, holds the item. A file or a value must match
  exactly. Text matches on its key words. After ten replies with no match it
  reads "not referenced".
- **Re-add 3 items** (`r`) is primary while something is left to put back.
  See [Re-adding what a summary lost](#re-adding-what-a-summary-lost). Once
  everything is back, it gives way to "Re-added 14:33".
- **Check with a model** has Haiku judge the summary against your list.
  See [Checking with a model](#checking-with-a-model).
- **Show 2 earlier summaries** lists the older summaries of the session,
  eight at most: the time, how it ran, and what it kept and lost.
  **Hide earlier summaries** puts them away. A link in the footer opens
  compactions on the dashboard.

### The footer

```
Hold auto-summaries for review: off   [ Open Startup ]   Open in dashboard   What these mean
```

- **Hold auto-summaries for review** switches the hold on and off. It is off
  until you turn it on. See
  [Holding an automatic summary](#holding-an-automatic-summary).
- **Open Startup** opens the Startup pane. With both panes open, Claude Code
  shows a title for each.
- **Open in dashboard** is a link to the compactions in the dashboard.
- **What these mean** replaces the pane with a short glossary of its
  figures, such as how full the context is, the summary check and the keep
  list. **Hide the meanings** brings the pane back.

## The Startup pane

Type `/glass startup`, or press **Open Startup** in the Checkpoint pane. This
pane answers what is in every request before you type, and what you can do
about it. It opens with one sentence, such as "Every request starts with 69k
tokens of setup." When one MCP server or one instructions file stands
out, a line under it says which and what to do: turn the server off in
`/mcp`, or trim the file. The line also covers a built-in tool that adds 10k
tokens or more to every request and that neither Claude nor a subagent has
called this session: add its name to `permissions.deny` if you never use it.
That tool advice waits until Claude has made three replies, so a fresh or
resumed session isn't told to drop a tool Claude hasn't had a chance to use. It
names the largest single item.

Three groups follow:

- **You control**: your MCP servers, Claude Code's built-in tools, CLAUDE.md
  and memory files, the skills list, and tools from plugins. Servers are named
  as `/mcp` lists them, and the count includes any that wait behind tool
  search; so does the built-in tools' count. The instructions your servers
  gave Claude at the start are part of the servers' figure. Turn off a server
  you don't need, trim a file, or drop a built-in tool you never use to make
  every request smaller. To drop a tool, add its bare name to
  `permissions.deny` in your settings (for example `"deny": ["WebSearch"]`);
  Claude Code then leaves it out of the context, from your next session.
- **Claude Code's own**: its built-in instructions, as one total. You can't
  change these. In the Desktop app the group also counts the servers that
  come with the app, such as Claude Browser, visualize and terminal, and
  `/glass detail` lists what is in it: first Claude Code's instructions and
  system prompt with the rest of the group's tokens, then each server by
  name and size, largest first, so the rows add up to the group. Without
  such a server the group lists nothing. In a terminal, VS Code or the
  mobile app the servers stay under You control.
- **Added as you work**: files that loaded when Claude reached matching
  paths, skills Claude used, Claude Code's reminders, and files and hook
  notes added to your messages. `/clear` and `/compact` empty these, so
  after a summary the group counts only what has been added since. The group
  is left out when nothing has been added.

With `/glass detail` on, the pane lists each server and each CLAUDE.md file
under its row, and which agent types received your CLAUDE.md. Under the
built-in tools it lists the ten largest tools with their sizes, then one row
for the rest. The tools that wait behind tool search cost nothing at the
start, so one dim line names them instead of a row each. It shows as many
whole names as the row holds and ends with ", …" when some don't fit. The
same goes for servers that wait behind tool search. Under each server it
lists the server's five largest tools with their sizes and counts the rest.
Tool sizes are shares of the row's figure by the length of each tool's text,
or, for a server's tools, by Claude Code's own count of them when it gives
one. The footer's link opens your CLAUDE.md files in the dashboard.

The total is Claude Code's own count when it can give one. Row sizes are
shares of that count, worked out from the length of each item's text, and
a note on the pane says they are estimates. Before Claude Code has counted,
the sizes come from text alone and leave out the input schemas of tools,
which are most of a tool's size, so the lead says "(est.)".

Claude Code answers a quick request for the context with estimates, and the
built-in tools come out well above their count (12k too high in one session,
with the conversation as far too low). So once a session, after Claude's
first reply, the mod asks Claude Code for the full count, the one `/context`
makes, and keeps the startup rows from it. The pane's total and the split
between setup and conversation use those rows from then on; the quick
readings after each reply still give everything else. Until that first reply
has finished, and if Claude Code can't count, the quick estimates stand.

## How a summary is handled

A conversation summary (compaction) replaces the conversation with a short
text. Claude Code gives you no way to approve the summary first, so the mod
works on both sides of it.

### Before: every summary is told the keep list

Every summary, whether you ran `/compact`, Claude summarised on its own, a
button pressed here started it, or Claude Code made it ahead of time, is
told your ticked keep list as instructions for the summariser. Anything you
typed after `/compact` comes first, and the list follows. The list is cut to
1,500 characters. This needs no switch.

The list is as of Claude's last reply. If it is empty, the mod scans the
conversation once instead. Subagents' own summaries are left alone.

### After: the check

When a summary is written, the mod checks your list against it with a
**quick check**. It looks for exact paths, numbers, ports and IDs, and for
the overlap of key words in your instructions. Each item is marked found,
partly or missing. The check can be wrong in both directions, and it covers
every item whether you ticked it or not.

### Re-adding what a summary lost

**Re-add** (`r` on the band or in the pane) takes the items the summary lost
and adds a section to Claude's system prompt: "Before compaction we agreed:
never use port 8765." Claude sees it from its next reply on.

- **Nothing is sent for you.** No message goes out, and the prompt box is
  not touched.
- **It stays until a summary keeps the items.** Re-added items also join the
  keep list for the next summary, and the check counts them.
- **It skips teammates.** They have their own conversations, and what the
  main session lost is not theirs to be told.
- **The text is built from the items alone.** A change in the section
  rewrites the prompt cache for the rest of the conversation, so it holds
  no times or counts.

The receipt in Last summary shows whether Claude used the items afterwards.
The mod reads each reply's text and tool inputs in memory to settle it, and
never keeps them.

### Checking with a model

**Check with a model** sends the summary and the list of items to Haiku and
has it judge each one. It runs only when you press it. The pane then shows
the result, such as "The model checked 8 items: 6 found, 1 partly, 1 missing",
and "Checked · about 1.2k tokens". Its marks replace the quick check's, and
the band and the toast use the new count. If the model can't check, the pane
says the quick check stands.

### Holding an automatic summary

**Hold auto-summaries for review** is off until you turn it on in the footer.
When Claude is about to summarise on its own, the mod waits once, so you can
review and summarise at a good moment. A toast says it held, and the Tight
button reads **Review before summarising**.

It holds only with real room to spare. The model's window must be at least
100,000 tokens above the summary point, as with a summary point of 300,000
on a 1 million window. It never holds when the context is within 20,000
tokens of the window, and never on a 200,000 window. The hold lasts for the
reply it began in, and the first automatic summary after that reply goes
through. If you ignore it, nothing else changes.

## Prompt help

### Draft checks

The hint line checks your draft before you send it. The three checks are a
new topic over a large context, a change asked for with no file named, and a
request to read everything. See [The hint line](#the-hint-line-before-sending).

### Tighten

`/glass tighten` has Haiku rewrite your draft shorter and more specific.

1. Write your message in the prompt box.
2. Type `/glass tighten` and send it. The command replaces your draft in the
   box, and Glass remembers the draft.
3. A pane shows your draft against the shorter version as a diff. Lines
   marked `-` are yours and lines marked `+` are the rewrite. It shows the
   word counts and what Haiku used, in tokens.
4. Press `[u]` Use this to fill the prompt box with the shorter version, or
   `[e]` Keep mine to put your draft back and close.

You can also type the text after the command, as `/glass tighten <text>`.
Without text it uses the draft in the box, or the last draft Glass kept when
the box is empty.

Nothing is sent either way. Haiku is called only when you run the command. If
the rewrite isn't shorter, Glass says your draft is already tight and
changes nothing. A rewrite that was cut short because it used its whole
allowance is not used, and Glass says so. Drafts over 20,000 characters are
too long to tighten.

### The guard

The guard is opt-in and off until you turn it on with `/glass guard`. With
it on, a send that would be costly asks first, with the figures: Send, Clear
first or Cancel. Two sends count as costly. One goes over an expired cache
with 100,000 tokens or more in context. The other starts a new topic over a
large context.

- **Send** sends the message as normal.
- **Clear first** puts `/clear` in the box for you to send, and brings your
  message back afterwards.
- **Cancel** keeps your message in the box.

It never asks for a slash command, a queued message, a message with pasted
images or a message you didn't type.

## Every /glass command

| Command | What it does |
|---|---|
| `/glass` | Opens the Checkpoint pane. It works while Claude is replying. |
| `/glass startup` | Opens the Startup pane. |
| `/glass detail` | Turns the detail on or off: the cost of a warm send, the tokens in the summary line, and the extra rows in Startup. |
| `/glass pin <text>` | Adds a pin to the keep list. You can keep 20. |
| `/glass unpin <words>` | Removes the pins that hold those words. `unpin all` removes them all. |
| `/glass tighten [text]` | Has Haiku rewrite your draft, or the text you give, shorter, and shows it as a diff. |
| `/glass guard` | Turns the question before a costly send on or off. `guard on` and `guard off` set it. |

## What moved to the dashboard

The mod used to carry more. These are on the dashboard only now, because
none of them changes what you do at the moment of a summary:

- The Now and Agents pages, and the rows under each agent call.
- The budget, and `/glass budget`.
- Spend and the 5-hour limit on the band.
- The notes at the end of a reply, and the cache-rebuild line.

The capture hook gives the tips for agent reports and split runs, and the
rating reminder, itself again ([With and without the mod](#with-and-without-the-mod)).

## What it reads and writes

All of these files live in ClaudeGlass's data folder, `~/.claude/claudeglass`
or `claudeglass` under `CLAUDE_CONFIG_DIR`.

| File | Who writes it | What it holds |
|---|---|---|
| `mod/settings.json` | ClaudeGlass: `update --finish`, then the dashboard's service after a report, and again at once when you rate a session or change the capture settings | Your billing mode, rate card, cache lifetime and characters per token. Also the coaching thresholds, your `muted` and `once` lists, whether coaching is on, "your usual", and where the dashboard answers. The mod only reads it. |
| `mod/present/<session id>.json` | The mod | A heartbeat: the session id, the last time seen and the time of the session's last request. It tells the capture hook the mod is running ([how it works](hooks.md#with-the-claudeglass-mod)). |
| `mod/sensor/<session id>.json` | The mod | What the mod showed and what you did next, as closed words and counts. |

### The sensor

The sensor file holds closed words, times and counts, and never text. Its
events are:

| Event | What it records |
|---|---|
| `present` | The mod ran in the session, and on which surface of Claude Code. |
| `note` | A hint it showed, where, and what followed within two replies: you sent anyway, cleared, summarised or did nothing. |
| `rebuild` and `cold_send` | A cache rebuild, and a send after the cache expired, with whether the mod had said to expect it. |
| `compaction` | A summary: how it ran (automatic, you asked or pressed here) and how many items it kept, lost and had re-added. It also records whether the summariser was told the keep list, and how many re-added items later replies referenced. |
| `tweak` | A button you pressed: Summarise now, Re-add, Check with a model or the hold. It carries a count before and, once known, a count after: items kept, lost or checked, or summaries held. |

A new Claude Code process for the same session adds to the file rather than
replacing it; history written before this version may be partial. This
happens when you resume a session, or restart or update the app, and the old
process may have ended without a word. The first time the new process writes
the file, it adds the events already in it that are older than its own.

The `present` event is written once Claude Code names the surface. The
Desktop app, VS Code and the mobile app start a session before the client
attaches, so the mod asks again, once a reply, until a surface is named. A
session with no surface at all, such as a headless run, never writes it.

The sensor and heartbeat files never hold a prompt, a message, a summary or
a path. The dashboard's watcher drops any event whose word is outside a fixed
list. The words you type, the summary text, the keep list and the replies
used to settle a receipt stay in the session's memory and are never written
to disk.

The mod makes no network calls. It calls a model in exactly two cases, and
only when you press the button: `/glass tighten` and Check with a model.
Both go through Claude Code, to Haiku. Once a session, after Claude's first
reply, it also asks Claude Code to count what loads before your first
message. Claude Code does that with the token-count request that `/context`
makes. It is not a model reply, and the mod sends nothing itself.

## With and without the mod

Without the mod, ClaudeGlass's coaching works as before. The capture hook
adds a short note to Claude's context, and Claude passes the tip on in its
reply. That is unreliable on Desktop. A hook's message collapses to a single
row there, and Claude passed a tip on to you about 1 time in 6.

While the mod is running in a session, the hook leaves out the hints the mod
shows itself. The mod then runs the same triggers, with the same thresholds,
wording and rest rules:

| Hint | Without the mod | With the mod |
|---|---|---|
| Cache expired | After you send, when the cost is paid | The hint line, before you send |
| Background work | After you send | The hint line |
| Small changes in a row | After you send | The hint line |
| Large paste | After you send | The hint line, where you judge whether it is a log |
| Fresh start after a plan | After you approve a plan | The status line |

Some hints stay with the hook, because they steer Claude or feed a skill, or
because the mod no longer shows them. They are the fresh start tip in plan
mode, the quiet output tip, the plan check, the facts line, the capture note,
the tips for agent reports and split runs, and the rating reminder.

A hint the hook leaves out is not marked as shown. If the mod closes, goes
stale or can't be read, the hint returns at once. Nothing changes without
the mod.

## What it never does

- It never changes a setting of Claude Code or ClaudeGlass for you. The
  hold switch and `/glass guard` change only the mod's own behaviour, and
  only when you use them.
- It never sends a message for you. A re-add adds a section to Claude's
  system prompt, and nothing goes to the prompt box. A shorter prompt from
  tighten goes into the box for you to edit and send.
- It never acts without a press, except to tell every summary your keep
  list.
- It never writes your words, a summary or a path to disk.
- It never calls a model unless you press Check with a model or run
  `/glass tighten`.

## Does it make a difference?

The dashboard's Work habits page has two tables on this. The first,
**Sessions with and without ClaudeGlass**, sets your sessions that ran with
the mod beside the ones that didn't. The figures are cost per reply and the
context when the session ended. They also cover cold-cache sends per 100
replies, sessions with two or more summaries, and tips per Desktop session.
It shows both groups' values and session counts.

Under 10 sessions in a group, it says there are too few to compare.
Otherwise it says whether the gap is larger than chance would likely give.
It does not claim the mod caused the gap, since sessions with the mod may
differ in other ways. Effects of tools like this are usually small, and the
table says "no measurable difference yet" when that is the case.

The second, **Tweaks**, counts what you pressed. One row for each button
shows how often you used it. The Summarise row adds the share of your keep
list that summaries held, with the summariser told the list and without it.
The Re-add row adds how many re-added items later replies referenced. Both
are counts from your own sessions, so a gap does not show that a tweak
caused it.

## Working on the mod

The mod's source is the `plugin/` folder of a checkout. To run it, start
Claude Code with `claude --plugin-dir plugin`.

- `claude plugin validate plugin/` checks the manifest.
- `claude plugin test plugin/` runs the plugin's own tests. It needs Claude
  Code 2.1.286 or later.
- `python -m pytest -q` runs the Python side, including the copy of the mod's
  words in `src/claudeglass/mod_copy.py` and the Node tests for its logic.

Every word the mod shows is in `mod_copy.py`. A test keeps the plugin's
generated copy in step with it, and the house style in
[`writing-help.md`](writing-help.md) applies.

## Limits

- **Estimates are labelled.** A figure that rests on a guess says so.
  Examples are the cache state, item sizes on the Startup pane, and amounts
  worked out from the built-in rate card.
- **The cache state is inferred.** Claude Code doesn't tell the mod whether
  the cache is warm. The mod works it out from the time since the last reply
  and your cache lifetime, 5 minutes or 1 hour.
- **A subscription's share is an estimate.** The 5-hour limit comes from
  Claude Code. Dollars turned into a share of that limit, or of your weekly
  limit, are a rough conversion from your history.
- **The quick check can be wrong.** It matches words and exact values. A
  summary that rephrases an instruction may look missing, and one that keeps
  the words but drops the meaning may look fine.
- **A receipt shows a match, not understanding.** A reply that names an item
  settles it as referenced, whether or not Claude acted on it.
- **Startup sizes are rough.** Row sizes are shares of Claude Code's count,
  or, before it has counted, text alone without tool schemas. Until a
  session's first reply has finished, Claude Code's count is its quick
  estimate, which puts the built-in tools high.
- **A hold is only as good as the headroom.** On a 200,000 window it never
  holds.
- **Pane titles show only with two panes open.** That is why the Checkpoint pane has an Open Startup button.

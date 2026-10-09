# The ClaudeGlass mod

The ClaudeGlass mod is a small plugin that runs inside Claude Code and
draws its own screens there. It shows how full the context is, what the
session has cost and what your agents are doing, while you work. It also
speaks up at the moments you can still change something: before you send a
message, and at the end of a reply. The dashboard stays the history. The mod
is the live half.

It exists because the dashboard shows what already happened, and a hint
passed on through Claude reaches you unreliably. In an audit of the
author's sessions in the Claude desktop app, Claude passed a coaching tip on
to you about 1 time in 6. The mod draws its hints itself, so they
arrive every time and cost no context.

The mod observes and shows. It never changes a setting, and it never sends
a message for you. [What it never does](#what-it-never-does) lists the
rest.

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
mod falls back on the rate card built into the plugin, labels its amounts
"(est.)" and shows no coaching hints.

## What you see

Each surface answers one question you would act on. Anything that wouldn't
change a decision sits behind `[m]` More detail, or on the dashboard.

Amounts follow your billing mode:

- **API billing** shows dollars, such as `$0.38`.
- **A subscription** shows a share of your 5-hour limit when Claude Code
  reports it, such as "about 0.4% of your 5-hour limit". Otherwise it shows
  a share of your weekly limit, worked out from your history.
- **When neither share is known**, it shows the list-price equivalent, never
  a bare dollar sign.

The examples below use API billing.

### While Claude works

The spinner gains a suffix: `· 60% full · +$0.04`. It says how full the
context is and what this reply has cost so far. On a subscription it shows
only the first part.

### The band above the prompt

The band is the glance. It is one row with up to three segments, then a
`[g]` Glass button that opens the pane:

```
◐ 60% full · about 6 replies before Claude summarises │ $0.38 this session (+$0.04)
◕ Compact soon · 78% full · about 2 replies before Claude summarises │ $0.51 (+$0.06) │ resend soon: cheap for 0:45 more
```

**Two segments are always there.**

- **Context.** An icon, how full the context is, and how many replies are
  left before Claude summarises the conversation (compaction). An icon
  and a word give the state, so colour is never the only signal. An empty
  circle means plenty of room: under 50% of the way to the summary point. A
  half circle means filling, up to 75%. "Compact soon" runs to 90%, and
  "Compacting next" is above that. The percentage matches `/context`, and the summary
  point is the one Claude Code uses, including your `autoCompactWindow`
  setting. On the desktop a small bar after this segment splits the context
  into startup context, conversation and tool output.
- **Spend.** What the session has cost, and the change from the last reply.
  If you set a budget, it reads `$0.38 of $2.00 budget` instead. On a
  subscription it shows the 5-hour limit used, such as "5-hour limit 12%
  used".

**One segment appears only when it matters.** The band shows the most
pressing of these:

1. A conversation summary that may have dropped something you said, with a
   `[r]` Review button.
2. The cache has expired (est.) and the context is 100,000 tokens or more.
   It says what the next send costs.
3. The 5-hour limit will run out within the hour, at this pace.
4. The cache is about to expire: the last fifth of its lifetime. It reads
   "resend soon: cheap for 0:45 more".
5. The 5-hour limit will run out before it resets, at a later time.
6. Agents are working.

Near the point where Claude summarises, a `[r]` Review before compacting
button also appears when the compaction review has something to offer.

**Detailed view** adds the tokens (`178k of 300k`) and the 5-hour limit. It
adds the median cost of your own sessions ("your usual") and a cache
countdown while the cache is warm. Turn it on with `/glass detail`, or `[m]` in the
pane.

**When the band gets narrow**, each segment takes a shorter form first. If
that is not enough, the least urgent segments go. The context stays. The
order is fixed. The spend goes short, then the context goes short. The spend
goes while the third segment is urgent. Then the third segment goes short,
and then it goes. The desktop bar draws only when every segment fits in
full.

The band shows after Claude's first reply in a session. It stays out of the
way of a survey.

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

In the Detailed view, a warm send says what it costs ("Sending costs about
$0.07") when nothing else needs saying.

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

### Notes at the end of a reply

At the end of a reply, a note can appear in the conversation, written
`● Glass · …`. It carries one sentence with one action. At most one note
shows every 5 replies, and each kind rests under the same rules as a hint.

| Note | When it shows |
|---|---|
| Replies left | Three replies or fewer remain before Claude summarises. Finish the task, then `/compact` or `/clear`. |
| Large tool output | One tool result added about 8,000 tokens or more. Every later reply re-reads it. |
| Model switch | The second model switch, with 50,000 tokens or more in context. It says what the switch cost. |
| Repeated summaries | Claude has summarised the session twice or more. |
| Cold summary | A conversation summary rebuilt the cache. Compacting while the cache is warm costs less. |
| Past your budget | The session passed the budget you set with `/glass budget`. |
| Rating reminder | A piece of work used a lot of tokens and isn't rated. Needs the feedback reminder switched on, in a project it covers. It doesn't need coaching notes. |

The two notes about agents appear on the agent's row when they can, as
described next. They show here only when there is no row to carry them.

When you approve a plan after a lot of planning, the status line says a
fresh session would save tokens on every reply. It goes at the
next reply or summary. Leaving plan mode counts as approving.

### The status line

The status line carries what can't wait. It is set when the 5-hour limit is
forecast to run out within 15 minutes, or when one reply or none is left
before Claude summarises. It is cleared when neither holds. The plan tip
above has the line while it shows, and nothing here overwrites it. It reads
the same in the Simple and Detailed views.

### Agent rows

Under each Agent call in the conversation, a line shows how the agent did:
`✓ Explore · haiku · $0.004`. A running agent shows `●`, a finished one `✓`
and one that stopped with an error `✗`. The model name takes a colour by
size of model, and the Detailed view adds the tokens in and out. Two notes
can sit under the row. One says the session holds a lot and each report from
background work re-reads it. The other says the run had to summarise its own
context or began from a very long brief.

### The cache rebuild line

When a reply had to write the whole context to the cache again, a line says
so in the conversation:

```
Cache rebuild: about 140k written again, about $0.52. Likely cause: a change near the start of the context, such as the model, effort or tools.
```

It shows when the rebuild was not expected and cost $0.05 or more. On a
subscription the bar is 1% of your 5-hour limit. The line names one likely
cause. It is a usage-limit wait, the conversation summary, the cache
lifetime (TTL) running out, or a change near the start of the context. An expected
rebuild, after a summary or a usage-limit wait, shows only in the Detailed
view.

### The compaction boundary line and badge

When Claude summarises the main session, the mod checks the summary against
what you said, and writes one line:

- `● Glass · Claude summarised the conversation. Everything you said is in
  the summary.`
- `● Glass · Claude summarised the conversation. 2 things you said may be
  missing. Press Review above the prompt to check.`

The Detailed view adds the context before and after, such as "150k tokens
became 30k tokens", and what the summary cost when Claude Code reports it.
The Simple view keeps the line as above.

In the second case the band shows a badge, "2 things may be missing from the
summary", and a `[r]` Review button. The summary row in the conversation
carries a `[v]` Review button too. Nothing shows when you said nothing worth
keeping. A summary a subagent runs for itself is left alone.

## The /glass pane

Type `/glass`, or press `[g]` Glass on the band. It works while Claude is
still replying, and `Esc` closes it. It never opens by itself.

A bar of tabs runs along the top. Press the key in brackets, or the tab.

| Key | Tab | What it answers |
|---|---|---|
| `[n]` | Now | How full is the context, what is filling it, and what has it cost? |
| `[a]` | Agents | Who is working, on what, and what does it cost? |
| `[k]` | Compaction | What did the summary keep, and what can I add back? |
| `[i]` | Instructions | What did this session load before my first message? |

Along the bottom, on every tab:

- `[m]` More detail switches to the Detailed view, and back. It is the same
  switch as `/glass detail`, and Glass remembers it.
- **Open in dashboard** is a link to this session in the dashboard. `[l]`
  Copy link copies it, for when your app doesn't open links.
- `[h]` What these mean explains each figure: what it is, why it is that
  size and what to do.

### Now

The first screen fits without scrolling:

- One plain sentence, such as "Context is 60% full. About 6 replies before
  Claude summarises." Below it is one piece of advice. When Claude summarises
  soon, `[c]` copies `/compact` for you.
- A bar of the context with a mark where Claude summarises.
- **What's filling it**, in three rows: Startup context (Claude Code and
  your instructions), Conversation so far, and Tool output kept in context.
  The last is an estimate, from how much each tool call added, and says
  "(est.)". Press a row (`[1]` to `[3]`) for one sentence on what it is and
  what to do.
- The session so far: the amount and the number of replies.

The Detailed view adds more:

- The cost and context of each recent reply.
- Where the spend goes: re-reading the context, writing it to the cache, new
  input and Claude's output.
- The heaviest single items.
- The 5-hour limit, with a "now" cursor.
- Your usual, and the summaries so far.
- The switch for the guard ([Prompt help](#prompt-help)).

### Agents

A sentence says how many agents are working and what share of the session's
cost they are. Then comes one row for each agent, running first. A row shows
the agent's name, its model and its cost. It also says what the agent is
doing in plain words: searching, reading files, editing files, running
commands or reading the web.

Press a row to see its task prompt, the tools it used and its biggest
results. The prompt shows its first 12 lines, and the mod doesn't keep it
after a reload. A link
opens subagent runs in the dashboard. The Detailed view adds the tokens, the
calls and the share re-read from the cache, and a row for the main session.

### Compaction

See [The compaction review](#the-compaction-review).

### Instructions

This tab lists what the session loaded into Claude's context, with the size
of each item. The first screen gives the startup context total and the five
largest items. A line of advice appears when one MCP server or one
instructions file stands out.

Press an item to see what kind it is, where it came from, and its size.
Files that load only when Claude reaches matching paths show the reply they
loaded in. The Detailed view groups the items by kind. The kinds are system prompt,
CLAUDE.md and rules, session details, skills, built-in tools, deferred tools,
MCP tools, and what Claude Code added during the session. It also lists which agent
types received your CLAUDE.md.

The total is Claude Code's own count when it can give one. Item sizes are
estimates from the text's length. They are shown as shares of their group's
total, and they leave out the input schemas of tools, which are most of a
tool's size.

## The compaction review

A conversation summary (compaction) replaces the conversation with a short
text. Claude Code gives you no way to approve the summary first, so the mod
works on both sides of it.

### The keep list

As you work, the mod builds a **keep list** of what a summary must not lose.
It runs locally, with no model. The kinds are:

- **Pinned**, which you chose.
- **What you asked**: your messages with "never", "don't", "always", "must"
  or "only", and corrections such as "no," or "actually".
- **Decisions**: "let's go with…", "agreed", and a plan you approved.
- **Last error**, **Open tasks**, **Files changed** and **Values you typed**
  (ports, versions, IDs, URLs and numbers).

Some items are marked "safe anyway", because they survive any summary. They
are files among the last five Claude changed, the approved plan, and
anything in your project or user-level CLAUDE.md. The mod reads those two files from
disk to know this, in memory only.

**Pins** are your own additions. Add one three ways:

- Type `/glass pin never use port 8765`.
- Press `[p]` Pin under your newest message that holds an instruction or a
  decision.
- Press an item in the full list.

You can keep 20 pins. They survive a summary and clear with `/clear`.

### Before a summary

When the context is within reach of the summary point, a `[r]` Review before
compacting button appears on the band. It opens the Compaction tab on the
**before** view:

- Up to seven suggested items, pins first, as ticks. Press an item to tick
  or untick it. `[o]` More items shows the rest: files, values, tasks and
  errors.
- `[c]` Compact with these puts `/compact Keep: …` in the prompt box, with
  the ticked items in at most 1,500 characters. You send it at a good
  stopping point. Nothing is sent for you.
- `[w]` switches to the after view when a summary exists.

### After a summary

The tab opens on the **after** view and shows only what may be missing:
things you said that the summary doesn't seem to carry. Each has an Add back
button. It puts a short note in the prompt box, such as "Before compaction
we agreed: never use port 8765." Edit it and send it. Add all
back (`[d]`) puts them all in one draft.

The other buttons:

- `[s]` Show the summary shows the text Claude Code wrote.
- `[f]` Full list shows every item with a mark: ✓ in the summary, a tilde
  for partly, ✗ not in the summary, ⓘ safe anyway. Press an item to pin it.
- `[e]` Earlier messages lists your messages from before the summary. Press
  one to put its text in a draft.

The check is a **quick check (est.)**. It looks for exact paths, numbers,
ports and IDs, and for the overlap of key words in your instructions. It can
be wrong in both directions. The first screen counts only what you said:
instructions, decisions and pins. Files, values, tasks and errors are
checked too, and appear in the full list.

The Detailed view adds three more things:

- **Check with Haiku** (`[z]`) sends the summary and your list to Haiku and has it
  judge each item. It is opt-in: the button shows about how many tokens it
  will send, and it runs only when you press it. The result shows what it
  used.
- **History** lists each summary in the session. A row has the time, whether
  it ran on its own or you ran `/compact`, tokens before and after, and items
  kept and missing. A link opens compactions on the dashboard.
- **Options** holds two switches, both off until you turn them on.

### The two options

- **Hold auto-compact for review** (`[x]`). When Claude is about to
  summarise on its own, the mod waits once, so you can review and compact at
  a good moment. It holds only with real room to spare. The model's window
  must be at least 100,000 tokens above the summary point, as with a summary
  point of 300,000 on a 1 million window. It never holds twice in a row, and
  never on a 200,000 window. If you ignore it, the next automatic summary
  runs as normal.
- **Re-add pins after compaction** (`[y]`). After each summary, the mod
  tells Claude your pins again, so they are in its context.

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

The guard is opt-in and off until you turn it on. With it on, a send that
would be costly asks first, with the figures: Send, Clear first or Cancel.
Two sends count as costly. One goes over an expired cache with 100,000
tokens or more in context. The other starts a new topic over a large
context.

- **Send** sends the message as normal.
- **Clear first** puts `/clear` in the box for you to send, and brings your
  message back afterwards.
- **Cancel** keeps your message in the box.

It never asks for a slash command, a queued message, a message with pasted
images or a message you didn't type. Turn it on and off with `/glass guard`,
or with the switch in the Detailed Now tab (`[s]`).

### The budget

`/glass budget 2` sets a budget for this session. On API billing it is
dollars (`2` or `$2.50`). On a subscription it is a percentage of the 5-hour
limit (`5` or `5%`). The band's spend segment then shows progress, such as
`$0.38 of $2.00 budget`. The first reply to pass it adds one note. The budget
is off by default. `/glass budget` shows it, and `/glass budget off` clears
it. A `/clear` starts the spend over.

## Every /glass command

| Command | What it does |
|---|---|
| `/glass` | Opens the pane. It works while Claude is replying. |
| `/glass detail` | Turns the Detailed view on or off. |
| `/glass pin <text>` | Adds a pin to the keep list. You can keep 20. |
| `/glass tighten [text]` | Has Haiku rewrite your draft, or the text you give, shorter, and shows it as a diff. |
| `/glass guard` | Turns the question before a costly send on or off. `guard on` and `guard off` set it. |
| `/glass budget <amount>` | Sets a budget for this session. `off` clears it, and no argument shows it. |

## What it reads and writes

All of these files live in ClaudeGlass's data folder, `~/.claude/claudeglass`
or `claudeglass` under `CLAUDE_CONFIG_DIR`.

| File | Who writes it | What it holds |
|---|---|---|
| `mod/settings.json` | ClaudeGlass: `update --finish`, then the dashboard's service after a report, and again at once when you rate a session or change the capture settings | Your billing mode, rate card, cache lifetime and characters per token. Also the coaching thresholds, your `muted` and `once` lists, whether coaching is on, "your usual", and where the dashboard answers. The mod only reads it. |
| `mod/present/<session id>.json` | The mod | A heartbeat: the session id and the last time seen. It tells the capture hook the mod is running ([how it works](hooks.md#with-the-claudeglass-mod)). |
| `mod/sensor/<session id>.json` | The mod | What the mod showed and what you did next, as closed words and times. |
| `mod/coach.json` | The mod | The time of the last rating reminder, and nothing else. |

The sensor and heartbeat files hold counts and closed words only. They never
hold a prompt, a message, a summary or a path. The dashboard's watcher drops
any event whose word is outside a fixed list. The words you type, the
summary text and the keep list stay in the session's memory and are never
written to disk.

The mod makes no network calls. It calls a model in exactly two cases, and
only when you press the button: `/glass tighten` and Check with Haiku. Both
go through Claude Code, to Haiku.

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
| Report re-read | After an agent call | The agent's row |
| Run too big | At the next agent call | The agent's row, else a note |
| Fresh start after a plan | After you approve a plan | The status line |
| Rating reminder | A line Claude appends | A note at the end of a reply |

Some hints stay with the hook, because they steer Claude or feed a skill.
They are the fresh start tip in plan mode, the quiet output tip, the plan
check, the facts line and the capture note.

A hint the hook leaves out is not marked as shown. If the mod closes, goes
stale or can't be read, the hint returns at once. Nothing changes without
the mod.

## What it never does

- It never changes a setting of Claude Code or ClaudeGlass for you. The
  switches in the pane (the guard, hold and re-add) change only the mod's
  own behaviour, and only when you press them.
- It never sends a message for you. Every draft it makes, an Add back note, a
  `/compact` command, a shorter prompt, goes into the prompt box for you to
  edit and send.
- It never writes your words, a summary or a path to disk.
- It never calls a model unless you press tighten or Check with Haiku.

## Does it make a difference?

The dashboard's Work habits page has a table, **Sessions with and without
ClaudeGlass**. It sets your sessions that ran with the mod beside the ones
that didn't. The figures are cost per reply and the context when the session
ended. They also cover cold-cache sends per 100 replies, sessions with two or
more summaries, and tips per Desktop session. It shows both groups' values and
session counts.

Under 10 sessions in a group, it says there are too few to compare.
Otherwise it says whether the gap is larger than chance would likely give.
It does not claim the mod caused the gap, since sessions with the mod may
differ in other ways. Effects of tools like this are usually small, and the
table says "no measurable difference yet" when that is the case.

## Limits

- **Estimates are labelled.** A figure that rests on a guess says "(est.)".
  Examples are the cache state, tool output kept in context, item sizes on
  the Instructions tab, and amounts worked out from the built-in rate card.
- **The cache state is inferred.** Claude Code doesn't tell the mod whether
  the cache is warm. The mod works it out from the time since the last reply
  and your cache lifetime, 5 minutes or 1 hour.
- **A subscription's share is an estimate.** The 5-hour limit comes from
  Claude Code. Dollars turned into a share of that limit, or of your weekly
  limit, are a rough conversion from your history.
- **The quick check can be wrong.** It matches words and exact values. A
  summary that rephrases an instruction may look missing, and one that keeps
  the words but drops the meaning may look fine.
- **Instructions sizes are rough.** They count text, not tool schemas.
- **A hold is only as good as the headroom.** On a 200,000 window it never
  holds.
- **Some pieces need the desktop app.** The bar in the band is drawn on the
  desktop. A terminal shows the same words without it.

# Metrics capture

Metrics capture is **opt-in**. Off by default, and off costs nothing: no tag is asked for, Claude Haiku is never asked, and no token is spent on it. Live coaching and your feedback have their own switches, and keep working while capture is off.

Turned on, a hook (`capture-hook.py`) adds a short note to each session start, and asks Claude to end its replies with one line such as `[cg: task=bugfix brief=partial level=normal]` (or Claude Haiku writes it, see [Who writes the tags](#who-writes-the-tags)). A subagent is asked for nothing: its brief and its report are exactly what they would be, and Claude Haiku judges the run once it's done (see [Agent runs](#agent-runs)). The tag always sits at the end of the reply you already read — nothing is hidden — and nothing free-text is ever asked for: every word comes from a closed vocabulary (see [Privacy](#privacy) below).

It costs tokens. The note is written to the prompt cache once, then read from it on every later reply of that session; the tag itself is a handful of output tokens on every reply, and each agent run judged is a Haiku call of about $0.002. [Levels](#levels) below gives rough sizes; once capture is on, Setup › Capture measures the real cost from your own transcripts, and a banner on every page shows the running total.

## Levels

Costs rise with depth, so capture comes in levels, each including every metric of the levels before it. The note is added once at each session's start, `/clear` or compaction (a resumed session already carries the note from its start, so it is not asked again). Agent runs get no note; a level with agent metrics adds a Haiku call per agent run instead, however deep the agent is nested.

| Level | What it adds | Note at session start | Haiku per agent run |
|---|---|---|---|
| Off | No metrics are captured and no tokens are used for them. Live coaching and feedback have their own switches and keep working while capture is off. | – | – |
| Free | Local signals from hooks that log to a file. Uses no Claude tokens. | – | – |
| Essentials | Claude tags each piece of work: what kind it was, how clear the request was, how hard, how big, and when the task changed. Claude Haiku judges whether each agent run finished, and why one was run again. | ~186 tokens | ~$0.002 |
| Standard | Adds what the request lacked, planning, skills, research, and Haiku's view of each agent run's model and brief. | ~304 tokens | ~$0.002 |
| Deep | Adds how much earlier context was needed, how the change was checked, and a short rating after large tool outputs. Also turns on the /cg-feedback survey, its reminder note, and Claude's one-line reminder to run it when a piece of work is done. | ~412 tokens | ~$0.002 |
| Custom | Any other set of metrics, turned on one by one (`capture enable`/`capture disable`). | depends what's on | depends what's on |

These are rough sizes — the note's characters divided by four, plus Claude Code's own hook-wrapper overhead (the system-reminder tags around it) — and don't include the tag Claude writes back (each metric below says roughly how many output tokens its own words cost). Setup › Capture replays your last 14 days of transcripts against each level before you turn it on, and once it's on, measures the real note and tag cost from what Claude Code actually recorded — read that number, not this one, when it matters.

## What each metric is worth

Every metric here has to earn its keep. Something has to read it and turn it into a decision, not only log it. This table is that trace: each metric's rough cost against what it feeds. The Capture page shows the same thing measured from your own transcripts, in tokens a week instead of per occurrence.

| Metric | Level | ~Output tokens each time | Feeds |
|---|---|---|---|
| Kind of task (`task`) | Essentials | ~3 | Profiles per kind of task, Cost per finished piece of work, Model and effort fit, Measuring your changes |
| How clear the request was (`brief`) | Essentials | ~3 | Giving Claude information |
| How hard the work was (`level`) | Essentials | ~3 | Model and effort fit, Profiles per kind of task, Planning, Measuring your changes |
| Task changes (`shift`) | Essentials | ~1 | Breaking down work, Clearing context, Planning |
| Size of the work (`size`) | Essentials | ~2 | Breaking down work, Measuring your changes |
| Did the agent finish (`result`) | Essentials | – | Delegating to agents, Model and effort fit, Cost per finished piece of work |
| Why an agent was run again (`retry`) | Essentials | – | Delegating to agents, Model and effort fit |
| What the request lacked (`missing`) | Standard | ~4 | Giving Claude information, Researching |
| Planning (`plan`) | Standard | ~2 | Planning |
| Skills (`skill`) | Standard | ~3 | Using skills |
| Research result (`found`) | Standard | ~2 | Researching |
| Agent model fit (`fit`) | Standard | – | Model and effort fit, Delegating to agents |
| Agent brief quality (`agent_brief`) | Standard | – | Delegating to agents, Giving Claude information |
| Earlier context needed (`prior`) | Deep | ~2 | Clearing context |
| How changes were checked (`check`) | Deep | ~3 | Checking changes |
| Large tool outputs (`big_output`) | Deep | ~2 | Tool output |
| Why sessions end (`session_end`) | Free | – | Breaking down work, Clearing context |
| Waiting on you (`waits`) | Free | – | Waiting and permissions |
| Permission decisions (`permissions`) | Free | – | Waiting and permissions |
| How turns end (`turn_signals`) | Free | – | Waiting and permissions, Cost per finished piece of work |
| Instruction files loaded (`instructions_loaded`) | Always measured, no hook | – | Giving Claude information |
| Commands and skills you ran (`prompt_expansion`) | Always measured, no hook | – | Using skills |
| Task lists (`tasks`) | Always measured, no hook | – | Breaking down work |
| API errors (`stop_failure`) | Always measured, no hook | – | Cost per finished piece of work |
| What your messages contain (`prompt_features`) | Always measured, no hook | – | Giving Claude information |
| What agent briefs contain (`brief_features`) | Always measured, no hook | – | Delegating to agents, Giving Claude information |
| Plans (`plan_features`) | Always measured, no hook | – | Planning |
| Tool calls turned away (`tool_denials`) | Always measured, no hook | – | Waiting and permissions, Planning |
| Skill timing (`skill_timing`) | Always measured, no hook | – | Using skills |
| Agent chains (`spawn_tree`) | Always measured, no hook | – | Delegating to agents |
| Repeated failures (`tool_loops`) | Always measured, no hook | – | Checking changes, Tool output |
| Where research happens (`research_split`) | Always measured, no hook | – | Researching, Delegating to agents |
| Coaching line (`coaching_line`) | Live coaching, any level | – | Clearing context, Tool output, Researching |
| Coaching notes from Claude (`coaching_notes`) | Live coaching, any level | – | Clearing context, Tool output, Researching, Delegating to agents, Planning |
| Brief templates (`brief_templates`) | Live coaching, any level | – | Giving Claude information |
| Feedback skill (`feedback_skill`) | Feedback, any level; switching to Deep turns it on | – | Cost per finished piece of work, Planning, Profiles per kind of task |
| Feedback reminder in the status line (`feedback_note`) | Feedback, any level; switching to Deep turns it on | – | Cost per finished piece of work |
| Feedback reminder from Claude (`feedback_reminder`) | Feedback, any level; switching to Deep turns it on | ~26 | Cost per finished piece of work |
| Rate sessions on the dashboard (`dashboard_rating`) | Feedback, any level | – | Cost per finished piece of work |

## Main session

### Kind of task (`task`)

- **Level:** Essentials
- **Captures:** What kind of work each of your messages asked for: feature, bugfix, refactor, debug, docs, review, test, research, plan, ops or chat.
- **Why:** Cost per kind of task, and a profile tuned to each kind. Replaces ClaudeGlass's guess from the session's shape.
- **Tag:** `task=feature|bugfix|refactor|debug|docs|review|test|research|plan|ops|chat`
- **Costs:** about 3 output tokens each time
- **Hook:** SessionStart
- **Powers:** Profiles per kind of task, Cost per finished piece of work, Model and effort fit, Measuring your changes

### How clear the request was (`brief`)

- **Level:** Essentials
- **Captures:** Whether your request was clear, partly clear or vague.
- **Why:** What vague requests cost you in extra turns, and how to brief Claude better.
- **Tag:** `brief=clear|partial|vague`
- **Costs:** about 3 output tokens each time
- **Hook:** SessionStart
- **Powers:** Giving Claude information

### How hard the work was (`level`)

- **Level:** Essentials
- **Captures:** Whether the work was easy, normal or hard.
- **Why:** Whether your model and effort fit the work: a lighter setup for easy work, and no cheaper-model suggestion for hard work.
- **Tag:** `level=easy|normal|hard`
- **Costs:** about 3 output tokens each time
- **Hook:** SessionStart
- **Powers:** Model and effort fit, Profiles per kind of task, Planning, Measuring your changes

### Task changes (`shift`)

- **Level:** Essentials
- **Captures:** When the work changed: a new unrelated task, building on the last one, the scope growing, redoing earlier work, or fixing a fault in earlier work.
- **Why:** Task switching, scope creep, rework and fixes, and when a fresh session or plan mode would have been cheaper.
- **Tag:** `shift=new|build|grew|redo|fix`
- **Costs:** about 1 output token each time
- **Hook:** SessionStart
- **Powers:** Breaking down work, Clearing context, Planning

### Size of the work (`size`)

- **Level:** Essentials
- **Captures:** How big each piece of work was, from xs to xl.
- **Why:** How you break work down: big asks that end in compaction or rework, and tiny asks that each pay the start-up cost. With the kind and difficulty of the work, it lets a change be judged on like-for-like work before and after it.
- **Tag:** `size=xs|s|m|l|xl`
- **Costs:** about 2 output tokens each time
- **Hook:** SessionStart
- **Powers:** Breaking down work, Measuring your changes

### What the request lacked (`missing`)

- **Level:** Standard
- **Captures:** What your request left Claude to find or guess: files, goal, constraints, what done means, how to reproduce, scope, or none.
- **Why:** What to put in your prompts, or once in CLAUDE.md, so Claude stops searching for it.
- **Tag:** `missing=files,goal,constraints,done,repro,scope|none`
- **Costs:** about 4 output tokens each time
- **Hook:** SessionStart
- **Powers:** Giving Claude information, Researching

### Research result (`found`)

- **Level:** Standard
- **Captures:** For research and search work, whether Claude found what was asked.
- **Why:** How you research: when to give Claude pointers, and when an Explore agent is cheaper.
- **Tag:** `found=yes|partial|no`
- **Costs:** about 2 output tokens each time
- **Hook:** SessionStart
- **Powers:** Researching

### Earlier context needed (`prior`)

- **Level:** Deep
- **Captures:** How much of the earlier conversation each reply needed.
- **Why:** A direct measure of when /clear was safe, and what it would have saved.
- **Tag:** `prior=needed|some|none`
- **Costs:** about 2 output tokens each time
- **Hook:** SessionStart
- **Powers:** Clearing context

### How changes were checked (`check`)

- **Level:** Deep
- **Captures:** How a change was verified: targeted tests, the full suite, a build, running it, by hand, or not at all.
- **Why:** Unchecked changes that later needed redoing or fixing, and full-suite output carried in context.
- **Tag:** `check=targeted|full|build|run|manual|none`
- **Costs:** about 3 output tokens each time
- **Hook:** SessionStart
- **Powers:** Checking changes

## Subagents and briefs, at any depth

### Did the agent finish (`result`)

- **Level:** Essentials
- **Captures:** Whether each subagent finished its task, finished part of it, or was blocked, judged by Claude Haiku from its brief and the end of its report once it's done.
- **Why:** Which agents and models deliver, and which get re-run.
- **Tag:** No tag. The agent is asked for nothing; Claude Haiku judges its run once it's done (see [Agent runs](#agent-runs)): "result: done|partial|blocked (done = the agent finished what its brief asked; partial = some of it; blocked = it could not go on, such as a missing file, tool or permission)"
- **Hook:** SubagentStop
- **Powers:** Delegating to agents, Model and effort fit, Cost per finished piece of work

### Why an agent was run again (`retry`)

- **Level:** Essentials
- **Captures:** When an agent run redoes an earlier one in the session that fell short, the reason: model, brief, tools, scope or other, judged by Claude Haiku from the two runs.
- **Why:** Why agents are re-run, and a guard that stops ClaudeGlass suggesting a cheaper model for work that needed a stronger one.
- **Tag:** No tag. The agent is asked for nothing; Claude Haiku judges its run once it's done (see [Agent runs](#agent-runs)): "retry: none|model|brief|tools|scope|other (is this run a second try at what an earlier run listed below was meant to deliver, because that run fell short? none = no, or there is no earlier run; model = it needed a stronger model; brief = the earlier brief was too vague or lacked something; tools = the earlier agent lacked a tool or a permission; scope = the task was cut too wide or changed; other)"
- **Hook:** SubagentStop
- **Powers:** Delegating to agents, Model and effort fit
- **Needs:** `result` switched on too

### Agent model fit (`fit`)

- **Level:** Standard
- **Captures:** Whether a smaller model would have done each subagent's task, or it needed a larger one, judged by Claude Haiku once the run is done.
- **Why:** Agent model tuning. Used only to rule a cheaper model out, never to recommend one.
- **Tag:** No tag. The agent is asked for nothing; Claude Haiku judges its run once it's done (see [Agent runs](#agent-runs)): "fit: smaller|right|larger (smaller = a smaller, cheaper model could plainly have done this: simple lookups or mechanical edits; right = it suited the model it ran on; larger = the agent struggled in a way a stronger model would not have)"
- **Hook:** SubagentStop
- **Powers:** Model and effort fit, Delegating to agents
- **Needs:** `result` switched on too

### Agent brief quality (`agent_brief`)

- **Level:** Standard
- **Captures:** How complete each subagent's brief was, and what it lacked: files, goal, scope or what done means, judged by Claude Haiku from the brief and the run.
- **Why:** Brief quality per agent type, and better agent definitions and brief templates.
- **Tag:** No tag. The agent is asked for nothing; Claude Haiku judges its run once it's done (see [Agent runs](#agent-runs)): "brief: clear|partial|vague (how complete the brief was: clear = what to do and what to hand back; partial = the goal without the details; vague = neither); missing: files,goal,scope,done or none (what the brief lacked that the agent had to find or guess; a comma list)"
- **Hook:** SubagentStop
- **Powers:** Delegating to agents, Giving Claude information
- **Needs:** `result` switched on too

## Skills and plans

### Planning (`plan`)

- **Level:** Standard
- **Captures:** Whether the work had no plan, a plan was made, a plan was followed, or the work departed from it.
- **Why:** How accurate plans are, and whether planning first saves rework on hard tasks.
- **Tag:** `plan=none|made|following|deviated`
- **Costs:** about 2 output tokens each time
- **Hook:** SessionStart
- **Powers:** Planning

### Skills (`skill`)

- **Level:** Standard
- **Captures:** Whether a skill helped, wasn't needed, or one of your listed skills would have helped (and which).
- **Why:** When to invoke a skill, skills that don't pay for their context, and skills you forget to use.
- **Tag:** `skill=helped|unneeded|would-help[:name]|none`
- **Costs:** about 3 output tokens each time
- **Hook:** SessionStart
- **Powers:** Using skills

### Commands and skills you ran (`prompt_expansion`)

- **Level:** Always measured, no hook
- **Captures:** Slash commands and skills you ran: name, what they added to context, and when in the session.
- **Why:** When you invoke skills, and what they add to context.
- **Tag:** No tag. Read from the transcript Claude Code already writes; Claude is never asked, and it costs no tokens.
- **Powers:** Using skills

### Plans (`plan_features`)

- **Level:** Always measured, no hook
- **Captures:** Each plan you approved or rejected: steps, files named and length. For a rejected plan, how long your feedback was and one word for how it reads: a question, a criticism or unsure. Whether you approved it by typing a go-ahead or by leaving plan mode.
- **Why:** Plan size against what the work then cost, and how many rounds a plan took before you approved it.
- **Tag:** No tag. Read from the transcript Claude Code already writes; Claude is never asked, and it costs no tokens.
- **Powers:** Planning

### Skill timing (`skill_timing`)

- **Level:** Always measured, no hook
- **Captures:** How far into a piece of work a skill ran, and whether you or Claude started it.
- **Why:** Starting skills earlier, and skills Claude reaches for on its own.
- **Tag:** No tag. Read from the transcript Claude Code already writes; Claude is never asked, and it costs no tokens.
- **Powers:** Using skills

## Tool calls

### Large tool outputs (`big_output`)

- **Level:** Deep
- **Captures:** After a tool result of about 8,000 tokens or more, how much of it Claude needed: all, part or none. Claude Code waits for the hook after each shell, read, search, web or MCP result. 'claudeglass capture status' shows how long that has added, measured from your own sessions.
- **Why:** Quieter commands, offset reads and output caps where big outputs weren't needed.
- **Tag:** `out=needed|part|unneeded`
- **Costs:** about 2 output tokens each time
- **Hook:** PostToolUse
- **Powers:** Tool output

## Free local signals

### Why sessions end (`session_end`)

- **Level:** Free
- **Captures:** Why each session ended: /clear, exit, logout or other.
- **Why:** Session length and batching tips, alongside task changes.
- **Tag:** No tag. A hook records it directly; Claude is never asked.
- **Hook:** SessionEnd
- **Powers:** Breaking down work, Clearing context

### Waiting on you (`waits`)

- **Level:** Free
- **Captures:** When Claude waited for your permission or input. The transcript shows how long until you answered.
- **Why:** How often Claude sat waiting, and allow rules for routine commands.
- **Tag:** No tag. A hook records it directly; Claude is never asked.
- **Hook:** Notification
- **Powers:** Waiting and permissions

### Permission decisions (`permissions`)

- **Level:** Free
- **Captures:** Each permission prompt: the tool name, never its arguments. The transcript shows what you decided.
- **Why:** Denials that led to rework, and allowlist suggestions.
- **Tag:** No tag. A hook records it directly; Claude is never asked.
- **Hook:** PermissionRequest
- **Powers:** Waiting and permissions

### How turns end (`turn_signals`)

- **Level:** Free
- **Captures:** Whether each turn ended normally or Claude Code asked the Stop hook again. On a failed turn, the kind of API error, such as a rate limit or overload, never the error's own text.
- **Why:** An independent, hook-level check next to what the transcript already shows about limit hits and API errors.
- **Tag:** No tag. A hook records it directly; Claude is never asked.
- **Hook:** Stop, StopFailure
- **Powers:** Waiting and permissions, Cost per finished piece of work

## Always measured

### Instruction files loaded (`instructions_loaded`)

- **Level:** Always measured, no hook
- **Captures:** Instruction files that load during a session (nested CLAUDE.md and rules): kind and size.
- **Why:** What nested CLAUDE.md and rules files cost as they load.
- **Tag:** No tag. Read from the transcript Claude Code already writes; Claude is never asked, and it costs no tokens.
- **Powers:** Giving Claude information

### Task lists (`tasks`)

- **Level:** Always measured, no hook
- **Captures:** How many tasks Claude created and completed.
- **Why:** How work is split into tasks, and how many get finished.
- **Tag:** No tag. Read from the transcript Claude Code already writes; Claude is never asked, and it costs no tokens.
- **Powers:** Breaking down work

### API errors (`stop_failure`)

- **Level:** Always measured, no hook
- **Captures:** The kind of each API error that stopped a turn.
- **Why:** Turns lost to errors.
- **Tag:** No tag. Read from the transcript Claude Code already writes; Claude is never asked, and it costs no tokens.
- **Powers:** Cost per finished piece of work

### What your messages contain (`prompt_features`)

- **Level:** Always measured, no hook
- **Captures:** Whether each message names a file, has a code block, an error or stack trace, a URL, done-criteria wording or numbered steps. Also whether it's short, tweaks earlier work, repeats something you said, only says to carry on, or only asks how it's going. Messages you typed while Claude was working are read the same way, and counted. Only yes/no and counts are kept.
- **Why:** How you give Claude information, measured without asking Claude: cost per task with and without file paths or errors.
- **Tag:** No tag. Read from the transcript Claude Code already writes; Claude is never asked, and it costs no tokens.
- **Powers:** Giving Claude information

### What agent briefs contain (`brief_features`)

- **Level:** Always measured, no hook
- **Captures:** The same checks for the briefs Claude writes for agents, and whether a short report was asked for.
- **Why:** Brief quality per agent type, and long reports that weren't capped.
- **Tag:** No tag. Read from the transcript Claude Code already writes; Claude is never asked, and it costs no tokens.
- **Powers:** Delegating to agents, Giving Claude information

### Tool calls turned away (`tool_denials`)

- **Level:** Always measured, no hook
- **Captures:** Why each tool call didn't run, as one word. A plan or question you answered, a hook or auto mode block, a closed dialog, or a call you turned down. Also how many clarifying questions Claude asked you. Only the word and the count are kept.
- **Why:** Counting only the calls you turned down when judging how often you stop Claude, not plan dialogs or hooks.
- **Tag:** No tag. Read from the transcript Claude Code already writes; Claude is never asked, and it costs no tokens.
- **Powers:** Waiting and permissions, Planning

### Agent chains (`spawn_tree`)

- **Level:** Always measured, no hook
- **Captures:** Cost per chain of agents and depth, and files an agent read that its parent had already read.
- **Why:** Flatter chains, and passing findings to agents instead of having them re-read.
- **Tag:** No tag. Read from the transcript Claude Code already writes; Claude is never asked, and it costs no tokens.
- **Powers:** Delegating to agents

### Repeated failures (`tool_loops`)

- **Level:** Always measured, no hook
- **Captures:** The same command failing again and again in one piece of work.
- **Why:** Flaky tests and environment trouble that burn tokens.
- **Tag:** No tag. Read from the transcript Claude Code already writes; Claude is never asked, and it costs no tokens.
- **Powers:** Checking changes, Tool output

### Where research happens (`research_split`)

- **Level:** Always measured, no hook
- **Captures:** Search and read tokens in the main session against those in Explore agents.
- **Why:** When to hand research to an agent.
- **Tag:** No tag. Read from the transcript Claude Code already writes; Claude is never asked, and it costs no tokens.
- **Powers:** Researching, Delegating to agents

## Live coaching

### Coaching line (`coaching_line`)

- **Level:** Live coaching, any level
- **Captures:** A second status line with a live hint from your session. For example, a large context before a new task, a large last output, many reads so far, or small requests sent one at a time.
- **Why:** Advice where you work, at the moment it applies. The status line is never sent to Claude.
- **Tag:** No tag. Shown only in the status line; Claude is never asked, and it costs no tokens.
- **Powers:** Clearing context, Tool output, Researching

### Coaching notes from Claude (`coaching_notes`)

- **Level:** Live coaching, any level
- **Captures:** Live hints for where the status line doesn't show, such as the desktop app. When one applies, a hook adds a short note to Claude's context, and Claude acts on it or tells you in a highlighted tip: a large tool output, many reads for one message, a subagent run past the point where your own history says splitting pays, a plan approved on top of a lot of planning context, or a large context or an expired cache when you send a message. It also flags how you prompt: the same request again, a big task without a plan, small requests sent one at a time, a vague correction, a huge paste, or stopping Claude again and again.
- **Why:** Advice at the moment it applies, and Claude can often act on it itself. Each note is about 50 to 120 tokens, re-read on every later reply of the session. Claude Code waits for the hook after each shell, read, search, web or MCP result and each message you send.
- **Tag:** No tag. A hook adds a note only when a hint applies, and Claude acts on it or tells you in a highlighted tip. Each hint and when it applies: [coaching.md](coaching.md).
- **Hook:** UserPromptSubmit, PostToolUse
- **Powers:** Clearing context, Tool output, Researching, Delegating to agents, Planning

### Brief templates (`brief_templates`)

- **Level:** Live coaching, any level
- **Captures:** Checklists per kind of task, built from what your own requests tend to lack, on Work habits to copy. Turned on, it also adds a /cg-brief skill you run with a request: Claude checks it against its checklist and asks once for anything missing.
- **Why:** Better first messages, so Claude spends less finding things out.
- **Tag:** No tag. The checklists are on Work habits, and /cg-brief runs only when you type it; like any skill, its name and description are listed to Claude at each session start.
- **Powers:** Giving Claude information

## Your feedback

### Feedback skill (`feedback_skill`)

- **Level:** Feedback, any level; switching to Deep turns it on
- **Captures:** A /cg-feedback skill you run after a piece of work. It asks four checkbox questions: the outcome, what slowed it, whether it was worth the tokens, and what would have helped. After an approved plan it asks a fifth: whether the build could have started fresh from the plan.
- **Why:** Cost per piece of work that met its goal, which outranks what Claude reports about itself. The plan answer tells the fresh-session tip and the suggested profile how you work.
- **Tag:** `[cg-fb: outcome=met|partly|missed|stopped slow=unclear,rework,tools,none worth=yes|fair|no helped=context,plan,smaller,none handoff=yes|partly|no]`
- **Powers:** Cost per finished piece of work, Planning, Profiles per kind of task

### Feedback reminder in the status line (`feedback_note`)

- **Level:** Feedback, any level; switching to Deep turns it on
- **Captures:** A second status line reminding you to run /cg-feedback, and the same line on the dashboard banner.
- **Why:** A reminder that costs nothing: the status line is never sent to Claude.
- **Tag:** No tag. Nothing is asked of Claude; see "Captures" above for how it is kept.
- **Powers:** Cost per finished piece of work

### Feedback reminder from Claude (`feedback_reminder`)

- **Level:** Feedback, any level; switching to Deep turns it on
- **Captures:** Claude adds a highlighted note suggesting /cg-feedback once a session, when it finishes its first piece of work.
- **Why:** For people without the status line, such as in the desktop app. Costs a few output tokens once a session.
- **Tag:** No fixed key. The note asks for a line: "The first time in this session you finish a piece of work the user asked for, add this before your tag, after a blank line, and never again after that:
> **ClaudeGlass:** Finished? Run /cg-feedback: a few ticks make your savings tips fit how you work."
- **Costs:** about 26 output tokens each time
- **Hook:** SessionStart
- **Powers:** Cost per finished piece of work

### Rate sessions on the dashboard (`dashboard_rating`)

- **Level:** Feedback, any level
- **Captures:** The same checkboxes on Spend › Sessions, kept in ClaudeGlass's own store.
- **Why:** Feedback without spending tokens.
- **Tag:** No tag. Nothing is asked of Claude; see "Captures" above for how it is kept.
- **Powers:** Cost per finished piece of work

## The tag format

Every note (`capture-hook.py` builds the same text from `capture-catalogue.json`) opens with the same two lines, then the keys for whichever metrics are on:

> The user turned on ClaudeGlass metrics capture, to see where their tokens go.
>
> End your final reply to each user message with one line, [cg: key=word ...], using only these keys and words:

...and, in the main session, closes with: "Leave out a key you can't judge."

A subagent gets no note, and a brief carries no marker: see [Agent runs](#agent-runs). Transcripts from before ClaudeGlass 0.11.0 may hold a subagent's own `[result: ...]` tag or a `[retry: ...]` brief marker; both are still read.

The `/cg-feedback` skill ends with its own line: `[cg-fb: outcome=met|partly|missed|stopped slow=unclear,rework,tools,none worth=yes|fair|no helped=context,plan,smaller,none handoff=yes|partly|no]`.

If Claude writes more than one tag, the last one wins, key by key.

## Who writes the tags

By default Claude writes the `[cg: ...]` tag itself, at the end of its final reply to each of your messages. `claudeglass capture tagger haiku` (or "Tags written by" on Setup › Capture) hands that to Claude Haiku instead, and `capture tagger claude` hands it back:

- The session note no longer carries the tag list, and replies end as they would anyway. At Standard the note drops from ~304 to ~0 tokens.
- When a turn of the main session ends, the hook's `Stop` entry reads the end of the transcript, hands a short excerpt to a worker process of its own, and returns at once. The excerpt holds your message (up to 2,000 characters), your message before it and the end of Claude's reply to that, how many you sent before, what Claude did (model calls, output tokens, tools used, the files it changed, the first line of up to 6 shell commands, whether they ran tests, skills, subagents, tool errors, any plan-mode plan) and the end of its final reply (up to 1,500 characters). Tool output is never in it.
- The worker runs `claude -p --model haiku` with no tools, settings, MCP servers or saved session, through your own Claude Code login, with the excerpt on stdin, and no thinking. Haiku gets a line for each key Claude's note would have asked for, with each word spelled out, after "You label one exchange between a user and Claude, an AI coding assistant, for the user's own usage analytics. You get an excerpt of it: the user's message, what Claude did, and the end of Claude's final reply. Answer with one line and nothing else, [cg: key=word ...], using only these keys and words. In them, "you" means Claude:" and before "Judge only from the excerpt: a plan, skill or check it doesn't show wasn't there. Give every key that applies. Leave one out only when it doesn't fit this work (found outside research or a search; shift on a first message) or the excerpt can't tell at all." It loads no settings file, so your hooks don't run inside it; a login that needs an `apiKeyHelper` from settings.json fails there, and `capture status` says so.
- Only the tag's words are kept, checked against the same vocabularies, in `<config-dir>/tags/YYYY-MM.jsonl`, with the reply's id and what the call cost. What the transcript settles overrides Haiku: a plan-mode plan written that turn is `plan=made`; no skill run is never `skill=helped`; no file changed is `check=none`, and a test run is `check=targeted` or `full` by whether it picked tests; only documentation changed is `task=docs`; and a first message has no `shift` but `new`. A turn that got no tag says why: `no_cli`, `no_login`, `timeout`, `failed`, `no_tag`. `capture status` counts both.
- Each call costs about $0.0020 (about 1,700 tokens read, 35 written), counted as capture's cost. On a subscription it counts toward your usage like any other Haiku use.
- How well it works is measured by `scripts/eval-tagger.py`: recorded sessions with known right answers, judged by Haiku with and without thinking and by Sonnet, against Claude's own tags. [tagger-eval.md](tagger-eval.md) has the results.
- The `Stop` entry runs in the foreground, since `claude -p` exits without waiting for a background hook, but only for as long as it takes to read the transcript's end. Deep's note after a large result isn't added, as no reply carries a tag for its word. Agent runs are Haiku's to judge whichever writes the main session's tags.

## Agent runs

A subagent is never asked for a tag, and a brief never carries a marker. Asked to end its report with `[result: ...]`, a subagent added it after an answer that had to be JSON only, breaking it, and the session that started it took the line for an injected instruction. So the agent metrics (`result`, `retry`, `fit`, `agent_brief`) are judged afterwards instead:

- When a subagent finishes, the hook's `SubagentStop` entry reads its transcript and hands an excerpt to the same worker as above: its type, its brief (up to 2,000 characters, without the line a workflow script's harness puts before a brief it computed), what it did (model calls, tools used, the files it changed, the first line of up to 6 shell commands, tool errors), the end of its report (up to 1,500 characters), or the start of the answer it handed back as structured output when that came last, as a workflow agent's does, and up to 4 earlier agent runs of the session (their type and the start of their brief and the end of their report), so Haiku can tell a re-run. Tool output is never in it.
- Haiku is told "You label one finished run of an AI coding agent, for the user's own usage analytics. You get an excerpt of it: the brief the agent was given, what it did, the end of its report and the session's earlier agent runs. Answer with one line and nothing else, [cg: key=word ...], using only these keys and words:", a line for each agent metric that is on, and "Judge only from the excerpt. Give every key; leave one out only when the excerpt can't tell at all."
- Its words land in `<config-dir>/tags/YYYY-MM.jsonl` beside the main session's, with the id of the agent's last reply, and are read as if the agent had written them. Each call costs about $0.002. Agents that set up Claude Code itself (`statusline-setup`, `output-style-setup`) are skipped. `capture status` says how many runs were judged, what the calls cost and why any got no verdict (a `claude` command that isn't signed in, say: the desktop app keeps its own login), whoever writes the main session's tags.
- Whether an agent used your CLAUDE.md rules (`rules`) can't be told from outside the agent, so it is no longer measured.

## Privacy

Claude and Haiku write closed vocabularies only. Every `[cg: ...]` and `[cg-fb: ...]` word, and every `[result: ...]`, `[retry: ...]` and `[spawn: ...]` word in an older transcript, is checked against the lists on this page; anything else — an unknown word, a key outside those lists, free text, a path — is dropped by the parser and never stored. The one exception that can carry a name is `skill=would-help:<name>`, and only when `<name>` matches a skill this transcript actually listed or invoked in the window; any other name is cut down to a bare `would-help`.

Free local signals never involve Claude at all: a hook logs the session id (hashed with this tool's own salt), the event word, and — for a permission prompt — the tool name, never its arguments, to a local file under `<config-dir>/signals/`. Those files, and the `capture-log.jsonl` record of every on/off/level change, aren't kept forever: `serve`'s watcher (or `capture prune` by hand) deletes entries past your configured retention, a default applying when none is set.

"Always measured" metrics read only what Claude Code's own transcript already contains — instruction files loaded, commands and skills run, task counts, API errors, and simple yes/no facts about a message's shape (does it name a file path, does it contain a code block) — and keep only those flags and counts, never the text itself.

## Turning it on, off or removing it

The hook script and its catalogue (`capture-hook.py`, `capture-catalogue.json`) live side by side under `<config-dir>/hooks/`. A change that needs different hook entries (`capture on`, `level`, `enable`, `disable`, `tagger` or `connect`) also changes `~/.claude/settings.json`, and `capture remove` takes the entries out — each only after showing the diff and asking first, unless you pass `--yes`. `capture off` leaves the entries, which add nothing while it's off, and every other change writes only this tool's own `config.toml`.

- `claudeglass capture status` — the level, what's on, since when, and the cost measured so far. While big_output or web is on, it also prints Deep's actual measured wait (median and p90, over the last 7 days). It also flags any hook — ClaudeGlass's own or one of yours — that failed on most of its calls over the last 14 days, naming it (event name only, never a matcher or tool name), when it last failed, where to find it in `settings.json`, the trade-off, and the undo; this is only ever a printed prompt, never an automatic change. A hook that has stopped failing since is left out.
- `claudeglass capture on [--level LEVEL] [--for DURATION | --until DATE | --no-limit] [--sample N] [--yes] [--dry-run]` — turn it on (default level: Essentials).
- `claudeglass capture level LEVEL` — change the level.
- `claudeglass capture tagger claude|haiku` — who writes the main session's tags (see [Who writes the tags](#who-writes-the-tags)).

A fresh switch from off to on — at `init`, `capture on`/`level`, or the Capture page — gets a 14-day time-box by default, so turning it on doesn't mean it runs unattended forever: it switches itself back off on its own unless you say otherwise. `--for DURATION` (a number and `h`, `d` or `w`, e.g. `30d`) or `--until DATE` picks another length or end date; `--no-limit` turns the time-box off entirely, so capture runs until you switch it off yourself. `init` has the same three choices as `--capture-for DURATION`, `--capture-level LEVEL --capture-no-limit`, or (interactively, or under `--non-interactive` with neither given) the default. Changing the level of capture that's already on leaves an existing time-box (or the lack of one) exactly as it is — the default only ever applies to a fresh switch-on.
- `claudeglass capture enable METRIC...` / `capture disable METRIC...` — turn individual metrics on or off; the level becomes Custom once the set no longer matches a preset.
- `claudeglass capture off` — stop the notes and tags at once, without touching settings.json.
- `claudeglass capture connect` — add the settings.json hook entries the metrics you've chosen need.
- `claudeglass capture remove` — switch off and take those hook entries back out.
- `claudeglass capture feedback on|off` — the `/cg-feedback` skill and its status-line reminder.
- `claudeglass capture brief on|off` — the `/cg-brief` skill.
- `claudeglass capture prune [--dry-run]` — delete signal files, Claude Haiku's tag files and `capture-log.jsonl` records past your configured retention (`retention_days` in `config.toml`, or a default when it's unset); `serve`'s watcher already runs this same cleanup on every tick, so this is for anyone not running it.
- `claudeglass changes` and `claudeglass uninstall` also cover metrics capture: they list everything it installed and can remove all of it — hooks, skills and signal files included.

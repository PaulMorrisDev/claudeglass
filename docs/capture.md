# Metrics capture

Metrics capture is **opt-in**. Off by default, and off costs nothing: no tag is asked for, Claude Haiku is never asked, and no token is spent on it. Live coaching and your feedback have their own switches, and keep working while capture is off.

Turned on, a hook (`capture-hook.py`) adds a short note to each session start, and asks Claude to end its replies with one line such as `[cg: task=bugfix brief=partial level=normal]` (or Claude Haiku writes it, see [Who writes the tags](#who-writes-the-tags)). A subagent is asked for nothing: its brief and its report are exactly what they would be, and Claude Haiku judges the run once it's done (see [Agent runs](#agent-runs)). The tag always sits at the end of the reply you already read — nothing is hidden — and the tags ask for no free text: every word comes from a closed vocabulary (the one place you can type is an Other answer in `/cg-feedback`; see [Privacy](#privacy) below).

It costs tokens. The note is written to the prompt cache once, then read from it on every later reply of that session; the tag itself is a handful of output tokens on every reply, and each agent run judged, or reply Claude ends without its tag, is a Haiku call of about $0.002. [Levels](#levels) below gives rough sizes; once capture is on, Setup › Capture measures the real cost from your own transcripts, and a banner on every page shows the running total.

## Levels

Costs rise with depth, so capture comes in levels, each including every metric of the levels before it. The note is added once at each session's start, `/clear` or compaction (a resumed session already carries the note from its start, so it is not asked again). Agent runs get no note, and nor does a subagent's own compaction, which the hook tells from the main session's by a subagent transcript that has just recorded one; the note also tells a subagent to ignore it. A session a scheduled task started gets none either: it has no message of yours, so none of its tags would be read. A level with agent metrics adds a Haiku call per agent run instead, however deep the agent is nested.

| Level | What it adds | Note at session start | Haiku per agent run |
|---|---|---|---|
| Off | No metrics are captured and no tokens are used for them. Live coaching and feedback have their own switches and keep working while capture is off. | – | – |
| Free | Local signals from hooks that log to a file. Uses no Claude tokens. | – | – |
| Essentials | Claude tags each piece of work: what kind it was, how clear the request was, how hard, how big, and when the task changed. For redone work it adds why, and whether Claude admitted a mistake. Claude Haiku judges whether each agent run finished, and why one was run again. | ~353 tokens | ~$0.002 |
| Standard | Adds what the request lacked, planning and skills, and Haiku's view of each agent run's brief. | ~484 tokens | ~$0.002 |
| Deep | Adds how much earlier context was needed, how the change was checked, and a short rating after large tool outputs. Also turns on the /cg-feedback survey, its reminder note, Claude's one-line reminder to run it after a large piece of work, and a one-question plan check when you fix something after approving a plan. | ~594 tokens | ~$0.002 |
| Custom | Any other set of metrics, turned on one by one (`capture enable`/`capture disable`). | depends what's on | depends what's on |

These are rough sizes — the note's characters divided by four, plus Claude Code's own hook-wrapper overhead (the system-reminder tags around it) — and don't include the tag Claude writes back (each metric below says roughly how many output tokens its own words cost). Setup › Capture replays your last 14 days of transcripts against each level before you turn it on, and once it's on, measures the real note and tag cost from what Claude Code actually recorded — read that number, not this one, when it matters.

## What each metric is worth

Every metric here has to earn its keep. Something has to read it and turn it into a decision, not only log it. This table is that trace: each metric's rough cost against what it feeds. The Capture page shows the same thing measured from your own transcripts, in tokens a week instead of per occurrence.

| Metric | Level | ~Output tokens each time | Feeds |
|---|---|---|---|
| Kind of task (`task`) | Essentials | ~3 | Profiles per kind of task, Cost per finished piece of work, Model and effort fit, Measuring your changes |
| How clear the request was (`brief`) | Essentials | ~3 | Giving Claude information |
| How hard the work was (`level`) | Essentials | ~3 | Model and effort fit, Profiles per kind of task, Planning, Measuring your changes |
| Task changes and their cause (`shift`) | Essentials | ~2 | Breaking down work, Clearing context, Planning |
| Size of the work (`size`) | Essentials | ~2 | Breaking down work, Measuring your changes |
| Did the agent finish (`result`) | Essentials | – | Delegating to agents, Model and effort fit, Cost per finished piece of work |
| Why an agent was run again (`retry`) | Essentials | – | Delegating to agents, Model and effort fit |
| What the request lacked (`missing`) | Standard | ~4 | Giving Claude information, Researching |
| Planning (`plan`) | Standard | ~2 | Planning |
| Skills (`skill`) | Standard | ~3 | Using skills |
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
| Coaching line (`coaching_line`) | Live coaching, any level | – | Clearing context, Tool output |
| Coaching notes from Claude (`coaching_notes`) | Live coaching, any level | – | Clearing context, Tool output, Delegating to agents, Planning |
| Brief templates (`brief_templates`) | Live coaching, any level | – | Giving Claude information |
| Feedback skill (`feedback_skill`) | Feedback, any level; switching to Deep turns it on | – | Cost per finished piece of work, Planning, Profiles per kind of task |
| Feedback reminder in the status line (`feedback_note`) | Feedback, any level; switching to Deep turns it on | – | Cost per finished piece of work |
| Feedback reminder from Claude (`feedback_reminder`) | Feedback, any level; switching to Deep turns it on | ~25 | Cost per finished piece of work |
| Plan check after a fix (`plan_check`) | Feedback, any level; switching to Deep turns it on | ~78 | Planning |
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

### Task changes and their cause (`shift`)

- **Level:** Essentials
- **Captures:** When the work changed: a new unrelated task, building on the last one, the scope growing, redoing work, or fixing what Claude delivered. For redone or fixed work, why: your request or the plan left it out, Claude missed something, you changed your mind, or a tool failed. Whether Claude admitted an earlier mistake: a wrong statement, a wrong change, or an instruction it didn't follow.
- **Why:** Task switching, scope creep, rework and fixes, and when a fresh session or plan mode would have been cheaper. The cause of each redo separates what your request left out from what Claude got wrong, and admitted mistakes are counted.
- **Tag:** `shift=new|build|grew|redo|fix why=left_out|missed|changed|tools admit=claim|change|instruction`
- **Costs:** about 2 output tokens each time
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
- **Tag:** No tag. The agent is asked for nothing; Claude Haiku judges its run once it's done (see [Agent runs](#agent-runs)): "result: done|partial|blocked (done = the agent did its own work and handed back what its brief asked for, whatever it found: findings, refuted claims and an empty list all count as done; partial = it did some of its work; blocked = it could not do its own work, such as a missing file, tool or permission)"
- **Hook:** SubagentStop
- **Powers:** Delegating to agents, Model and effort fit, Cost per finished piece of work

### Why an agent was run again (`retry`)

- **Level:** Essentials
- **Captures:** When an agent run redoes an earlier one in the session that fell short, the reason: model, brief, tools, scope or other, judged by Claude Haiku from the two runs, or model when the same brief reruns on a higher model tier.
- **Why:** Why agents are re-run, and a guard that stops ClaudeGlass suggesting a cheaper model for work that needed a stronger one.
- **Tag:** No tag. The agent is asked for nothing; Claude Haiku judges its run once it's done (see [Agent runs](#agent-runs)): "retry: none|model|brief|tools|scope|other (is this run a second try at what an earlier run listed below was meant to deliver, because that run fell short? none = no, or there is no earlier run; model = it needed a stronger model; brief = the earlier brief was too vague or lacked something; tools = the earlier agent lacked a tool or a permission; scope = the task was cut too wide or changed; other)"
- **Hook:** SubagentStop
- **Powers:** Delegating to agents, Model and effort fit
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
- **Captures:** After a read, search or web result of about 8,000 tokens or more, how much of it Claude needed: all, part or none. Only what Claude reads counts. A picture counts for at most 1,600 tokens. A result Claude Code saved to a file counts for its preview alone. Shell and MCP results are not asked about. Claude Code waits for the hook after each read, search or web result. 'claudeglass capture status' shows how long that has added, measured from your own sessions.
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
- **Captures:** The same command failing again and again in one piece of work. It is counted on the Savings page, with the commands that failed; no hint speaks up while it happens.
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
- **Captures:** A second status line with a live hint from your session. For example, a cache about to expire, a large last output, or small requests sent one at a time.
- **Why:** Advice where you work, at the moment it applies. The status line is never sent to Claude.
- **Tag:** No tag. Shown only in the status line; Claude is never asked, and it costs no tokens.
- **Powers:** Clearing context, Tool output

### Coaching notes from Claude (`coaching_notes`)

- **Level:** Live coaching, any level
- **Captures:** Live hints for where the status line doesn't show, such as the desktop app. When one applies, a hook adds a short note to Claude's context, and Claude acts on it or writes you a highlighted tip: a large read, search or web result, a subagent run past the point where your own history says splitting pays, a plan approved on top of a lot of planning context, a message sent after a break that outlasted the prompt cache, or asking how background work is going while it still runs. It also flags how you prompt: small requests sent one at a time, or a huge paste. Vague corrections, the same request again and stopping Claude again and again are counted after the fact on Work habits, with no live note. So is a big task without a plan.
- **Why:** Advice at the moment it applies, and Claude can often act on it itself. Each note is about 50 to 140 tokens, re-read on every later reply of the session. Claude Code waits for the hook after each read, search or web result and each message you send. A hook after each reply runs in the background and keeps only the time and size of Claude's newest reply, so the cache check is right after a resume.
- **Tag:** No tag. A hook adds a note only when a hint applies, and Claude acts on it or tells you in a highlighted tip: the note's first sentence says to write it and its last line is the tip, word for word, so every app shows it. Each hint and when it applies: [coaching.md](coaching.md).
- **Hook:** UserPromptSubmit, PostToolUse, Stop
- **Powers:** Clearing context, Tool output, Delegating to agents, Planning

### Brief templates (`brief_templates`)

- **Level:** Live coaching, any level
- **Captures:** Checklists per kind of task, built from what your own requests tend to lack, on Work habits to copy. Turned on, it also adds a /cg-brief skill you run with a request: Claude checks it against its checklist and asks once for anything missing.
- **Why:** Better first messages, so Claude spends less finding things out.
- **Tag:** No tag. The checklists are on Work habits, and /cg-brief runs only when you type it; like any skill, its name and description are listed to Claude at each session start.
- **Powers:** Giving Claude information

## Your feedback

### Feedback skill (`feedback_skill`)

- **Level:** Feedback, any level; switching to Deep turns it on
- **Captures:** A /cg-feedback skill you run after a piece of work. It asks a few checkbox questions: the outcome, what your follow-ups were, whether it was worth the tokens, and what would have made it cheaper. After an approved plan it asks whether the plan covered what you fixed and whether the build could have started fresh. A tip question appears only when ClaudeGlass showed a tip. When you run it, a hook adds one line of counts and ids for the piece of work, never any text. The skill uses it to leave out questions that don't apply.
- **Why:** Cost per piece of work that met its goal, which outranks what Claude reports about itself. The follow-up and plan answers tell the tips and the suggested profile where the work went wrong.
- **Tag:** `[cg-fb: outcome=met|partly|missed|stopped why=left_out,missed,changed,none missed_in=message|plan|standing|earlier worth=yes|fair|no helped=context,plan,smaller,none plan=covered|gap|new handoff=yes|partly|no tip=useful|known|wrong tip_hint=<hint id> from_text=<keys>]`
- **Hook:** UserPromptSubmit
- **Powers:** Cost per finished piece of work, Planning, Profiles per kind of task

### Feedback reminder in the status line (`feedback_note`)

- **Level:** Feedback, any level; switching to Deep turns it on
- **Captures:** A second status line reminding you to run /cg-feedback, and the same line on the dashboard banner.
- **Why:** A reminder that costs nothing: the status line is never sent to Claude.
- **Tag:** No tag. Nothing is asked of Claude; see "Captures" above for how it is kept.
- **Powers:** Cost per finished piece of work

### Feedback reminder from Claude (`feedback_reminder`)

- **Level:** Feedback, any level; switching to Deep turns it on
- **Captures:** A note with your next message asks Claude to end its reply with a line suggesting /cg-feedback. It comes only for a piece of work you haven't rated that has used at least 1M tokens and twice your typical piece. A rating you give on the dashboard after the piece started counts too. At most once per piece of work and once every 3 days. A piece starts with the session, a /clear, or a message Claude tags as a new task.
- **Why:** For people without the status line, such as in the desktop app, and only for work big enough to be worth rating. Costs a note of about 100 tokens and a few output tokens, a few times a week at most.
- **Tag:** No tag. A hook note asks Claude to end its reply with a line: "> **ClaudeGlass:** Finished? Run /cg-feedback: a few ticks make your savings tips fit how you work."
- **Costs:** about 25 output tokens each time
- **Hook:** UserPromptSubmit
- **Powers:** Cost per finished piece of work

### Plan check after a fix (`plan_check`)

- **Level:** Feedback, any level; switching to Deep turns it on
- **Captures:** It comes after you approve a plan and Claude changes files. Your next message that corrects or adjusts the work gets one question from Claude first. Did Claude miss something the plan said, did the plan leave it out, is it something new, or is this not a fix? Asked at most once per plan. It stops for 14 days after two declined or Other answers in a row. Only the four ticked words are kept.
- **Why:** Which of your corrections the plan could have prevented. That tells the plan tips whether to ask for fuller plans or for a closer check of the build against the plan.
- **Tag:** No tag. A hook note has Claude ask one question (header "CG plan fix") before it acts on your message; only the ticked word is kept (`covered`, `gap`, `new`, `none`).
- **Costs:** about 78 output tokens each time
- **Hook:** UserPromptSubmit
- **Powers:** Planning

### Rate sessions on the dashboard (`dashboard_rating`)

- **Level:** Feedback, any level
- **Captures:** The /cg-feedback questions as checkboxes on Spend › Sessions. They cover the outcome, your follow-ups, whether it was worth it and what would have made it cheaper. A question about the plan or a tip shows only when it applies to the session. A session with two or more approved plans gets a row for each. Tip and recommendation cards take Useful, Trying it, Knew it or Wrong here. All of it is kept in ClaudeGlass's own store.
- **Why:** Feedback without spending tokens.
- **Tag:** No tag. Nothing is asked of Claude; see "Captures" above for how it is kept.
- **Powers:** Cost per finished piece of work

## The tag format

Every note (`capture_hook.py` builds the same text from `capture-catalogue.json`) opens with the same two lines, then the keys for whichever metrics are on:

> The user turned on ClaudeGlass metrics capture, to see where their tokens go. If you are a subagent, ignore this note.
>
> End your final reply to each user message with one line, [cg: key=word ...], using only these keys and words:

...and, in the main session, closes with: "Leave out a key you can't judge. When carrying out a plan, judge the plan, not the go-ahead. Tag your reply to an agent's report for the request that started the agent."

A subagent gets no note, and a brief carries no marker: see [Agent runs](#agent-runs). Transcripts from before ClaudeGlass 0.11.0 may hold a subagent's own `[result: ...]` tag or a `[retry: ...]` brief marker; both are still read. Older notes also asked for `found` (whether research found what was asked), and a `fit` word judged whether a smaller model would have done an agent's task. Neither is asked for now, and older transcripts that hold them are still read.

The `/cg-feedback` skill ends with its own line: `[cg-fb: outcome=met|partly|missed|stopped why=left_out,missed,changed,none missed_in=message|plan|standing|earlier worth=yes|fair|no helped=context,plan,smaller,none plan=covered|gap|new handoff=yes|partly|no tip=useful|known|wrong tip_hint=<hint id> from_text=<keys>]`.

`tip_hint` is the id of the tip the question was about. `from_text` lists the keys whose word Claude picked from a note you typed under Other instead of a ticked box. Claude reads that note once to pick the closest word, then drops it: only the word is kept, and a ticked answer always wins over the tag. Runs from before the redesign asked what slowed the work (`slow`); those answers are still read.

If Claude writes more than one tag, the last one wins, key by key, except `level` and `size`: the highest wins (`hard` over `normal` over `easy`, `xl` over `xs`), so a trailing "easy" can't relabel a message that took hard work.

What the transcript says outranks the words, for either writer. ClaudeGlass applies these rules when it reads Claude's tags back, and the hook applies the same ones to Haiku's before they are stored. The tag file notes each change as `key:from>to` in an optional `g` field, from the closed words only:

- **`shift`, `why`:** a first message has no `shift` but `new`, and no `why`. `why` stays only with `shift=redo` or `fix`, and `why=tools` only when a tool call failed. A `build` or `grew` becomes `fix` when your message corrects Claude, or tweaks the files Claude changed in its previous reply.
- **`admit`:** Haiku's word stays only when the reply reads like an admission. A reply that reads like one but got no `admit` word is a possible admission, kept out of every total.
- **`check`:** a test run sets it, `full` when the whole suite ran at any point, `targeted` when only chosen tests did. `none` is set only when Claude, its subagents and its workflow agents changed no file and no shell command did. A `git commit`, a redirected `2>&1` and `>/dev/null` change nothing; a merge, a rebase and `sed -i` do.
- **`plan`:** `made` whenever Claude put a plan up in the turn. `following` replaces `made` once you approved a plan earlier, in the dialog or by typing a go-ahead. A plan you sent back doesn't count.
- **`task`, `skill`, `prior`:** `task` is `docs` when only documentation changed. Files a subagent or a workflow agent changed count too, and rule `docs` out. `skill=helped` or `unneeded` becomes `none` when no skill ran, and `prior` is `none` on the first message.

## Who writes the tags

By default Claude writes the `[cg: ...]` tag itself, at the end of its final reply to each of your messages. `claudeglass capture tagger haiku` (or "Tags written by" on Setup › Capture) hands that to Claude Haiku instead, and `capture tagger claude` hands it back:

- The session note no longer carries the tag list, and replies end as they would anyway. At Standard the note drops from ~484 to ~0 tokens.
- When a turn of the main session ends, the hook's `Stop` entry reads the end of the transcript, hands a short excerpt to a worker process of its own, and returns at once. The excerpt holds your message (up to 2,000 characters), your message before it and the end of Claude's reply to that, how many you sent before and how many minutes after Claude's last reply, the files that reply changed and how many changed again, up to 3 messages you queued while Claude worked (300 characters each), how many short follow-ups you sent in a row, what Claude did (model calls, output tokens, tools used, the files it changed, the first line of up to 6 Bash or PowerShell commands, whether they ran tests, skills, subagents, tool errors), the plan-mode state (a plan written, approved or sent back, and how often), the request the work began with (the start of the plan you approved, up to 600 characters, or else of your latest earlier message longer than 300) and the end of its final reply (up to 1,500 characters). Tool output is never in it, and nothing but the tag's words is kept.
- The worker runs `claude -p --model haiku` with no tools, settings, MCP servers or saved session, through your own Claude Code login, with the excerpt on stdin, and no thinking. Haiku gets a line for each key Claude's note would have asked for, with each word spelled out, after "You label one exchange between a user and Claude, an AI coding assistant, for the user's own usage analytics. You get an excerpt of it: the user's message, what Claude did, and the end of Claude's final reply. Answer with one line and nothing else, [cg: key=word ...], using only these keys and words. In them, "you" means Claude:" and before "Judge only from the excerpt: a plan, skill or check it doesn't show wasn't there. Give every key that applies. Leave one out only when it doesn't fit this work (why without a redo or fix; admit without an admission; shift on a first message) or the excerpt can't tell at all. When Claude carried out a plan, judge the plan, not the user's go-ahead." It loads no settings file, so your hooks don't run inside it; a login that needs an `apiKeyHelper` from settings.json fails there, and `capture status` says so.
- Only the tag's words are kept, checked against the same vocabularies, in `<config-dir>/tags/YYYY-MM.jsonl`, with the reply's id and what the call cost. What the transcript settles overrides Haiku, by the rules above, and the line notes what changed. A turn that got no tag says why: `no_cli`, `no_login`, `timeout`, `failed`, `no_tag`, `no_answer`. `capture status` counts both.
- Each call costs about $0.0020 (about 1,700 tokens read, 35 written), counted as capture's cost. On a subscription it counts toward your usage like any other Haiku use.
- While Claude writes the tags, Haiku still catches the ones it leaves out. When Claude ends a reply that finishes a piece of work with no `[cg: ...]` tag, the same `Stop` entry hands that turn to the same worker, and the tag lands in the same file with `"w":"haiku-fallback"`. It leaves alone the reply to a background agent's report, another session's message, a scheduled or looped task, a command's output or a prompt Claude Code sent itself, and a turn that starts a background agent or workflow or ends while one still runs. A session a scheduled task started is left alone too, and so is a cycle in which any reply already carries a tag. Each call costs the same as above and uses the same login, and the excerpt goes only to Haiku.
- How well it works is measured by `scripts/eval-tagger.py`: recorded sessions with known right answers, judged by Haiku with and without thinking and by Sonnet, against Claude's own tags. [tagger-eval.md](tagger-eval.md) has the results.
- The `Stop` entry runs in the foreground, since `claude -p` exits without waiting for a background hook, but only for as long as it takes to read the transcript's end. Deep's note after a large result isn't added, as no reply carries a tag for its word. Agent runs are Haiku's to judge whichever writes the main session's tags.

## Agent runs

A subagent is never asked for a tag, and a brief never carries a marker. Asked to end its report with `[result: ...]`, a subagent added it after an answer that had to be JSON only, breaking it, and the session that started it took the line for an injected instruction. So the agent metrics (`agent_brief`, `result`, `retry`) are judged afterwards instead:

- When a subagent finishes, the hook's `SubagentStop` entry hands a small job (the agent's type and where its transcript is) to the same worker as above and returns at once. The worker first waits for the transcript to settle, since the hook fires before the agent's last lines are written: until the call of an answer tool (`StructuredOutput`, `SubagentHandback`) is there, or the file has stopped growing for 3 seconds, or 20 seconds have passed, which is a `no_answer` and no call to Haiku. Then it reads the transcript and hands Haiku an excerpt: the agent's type, its brief (up to 2,000 characters), what it did (model calls, tools used, the files it changed, the first line of up to 6 shell commands, tool errors), the end of its report (up to 1,500 characters) and up to 4 earlier agent runs of the session, read from the whole of the session's transcript (their type and the start of their brief and the end of their report), so Haiku can tell a re-run. Tool output is never in it.
- A workflow script hands an agent two prompts: the line it was started with, relayed, and the task it computed, each under a harness line saying who wrote it. The computed task is the brief. The relayed line comes along as context (up to 300 characters), marked as not the brief, so that "Continue the plan" isn't judged as one. The harness lines are left out.
- An agent that hands its answer back through an answer tool has it shown field by field, one `key: value` line each, a field cut at 300 characters (for `SubagentHandback`, its `message`). A closing remark of up to 300 characters after the answer is shown too, and the answer is still the report; a longer one is the report instead.
- Haiku is told "You label one finished run of an AI coding agent, for the user's own usage analytics. You get an excerpt of it: the brief the agent was given, what it did, the end of its report or the answer it handed back, and the session's earlier agent runs. Answer with one line and nothing else, [cg: key=word ...], using only these keys and words:", a line for each agent metric that is on, brief and missing first, then result, then retry, and "Judge only from the excerpt. Judge the brief first, from the brief alone, whatever the result. Give every key; leave one out only when the excerpt can't tell at all." Asked for the result first, it rated the same brief by how the run ended.
- Its words land in `<config-dir>/tags/YYYY-MM.jsonl` beside the main session's, with the id of the agent's last reply, and are read as if the agent had written them. Each call costs about $0.002. Agents that set up Claude Code itself (`statusline-setup`, `output-style-setup`) are skipped. `capture status` says how many runs were judged, what the calls cost and why any got no verdict (a `claude` command that isn't signed in, say: the desktop app keeps its own login), whoever writes the main session's tags.
- The hook puts right what the transcript settles, as it does a turn's tag. An agent that handed back its answer through an answer tool wasn't missing what done means, so `done` is taken out of `missing`. A run of the same brief as an earlier run, on a higher model tier (haiku, sonnet, opus, fable), is `retry=model` whatever Haiku said. A workflow's agents are steps of a pipeline, not retries of each other: Haiku is shown no earlier runs for them and they get no `retry`.
- A stop that follows another stop hook's request to carry on (`stop_hook_active`) is judged too. The agent's last reply is then a newer one, and where the verdicts are read the newest verdict for a run wins, and the earlier calls' cost is added to it.
- *Done* is what the agent's own work was, not how the news turned out: findings, claims it refuted and an empty list all count as done, and *blocked* is an agent that couldn't do its own work.
- Whether an agent used your CLAUDE.md rules (`rules`) can't be told from outside the agent, so it is no longer measured. Haiku's model fit verdict (`fit`) said "right" every time, so it is no longer asked; the agent tables measure it instead, from how many of an agent's calls were single read-only probes and how many calls came before its first edit.

## Privacy

Claude and Haiku write closed vocabularies only. Every `[cg: ...]` and `[cg-fb: ...]` word, and every `[result: ...]`, `[retry: ...]` and `[spawn: ...]` word in an older transcript, is checked against the lists on this page; anything else — an unknown word, a key outside those lists, free text, a path — is dropped by the parser and never stored. The one exception that can carry a name is `skill=would-help:<name>`, and only when `<name>` matches a skill this transcript actually listed or invoked in the window; any other name is cut down to a bare `would-help`.

The one place you can type is the Other choice in a `/cg-feedback` question. Claude reads that text once, in the session you are already in, to pick the closest word from that question's list, and writes only the word in the tag, with the question's key in `from_text`. It is told never to copy, quote or save your words. The parser sees your answer in the transcript in memory only and keeps just which questions were answered that way. A word picked from your note counts only when the answer to that question really was typed text, and a ticked answer always wins over the tag. What you typed never reaches the digest, the store, the dashboard or a file.

Free local signals never involve Claude at all: a hook logs the session id (hashed with this tool's own salt), the event word, and — for a permission prompt — the tool name, never its arguments, to a local file under `<config-dir>/signals/`. Those files, the `capture-log.jsonl` record of every on/off/level change, and the `habit-log.jsonl` record of each tip you marked "Trying it" on the dashboard (a habit's id and the time, nothing else), aren't kept forever: `serve`'s watcher (or `capture prune` by hand) deletes entries past your configured retention, a default applying when none is set.

"Always measured" metrics read only what Claude Code's own transcript already contains — instruction files loaded, commands and skills run, task counts, API errors, and simple yes/no facts about a message's shape (does it name a file path, does it contain a code block) — and keep only those flags and counts, never the text itself.

## Turning it on, off or removing it

The hook (`capture-hook.py`, a small launcher), the module it runs (`capture_hook.py`) and its catalogue (`capture-catalogue.json`) live side by side under `<config-dir>/hooks/`. A change that needs different hook entries (`capture on`, `level`, `enable`, `disable`, `tagger` or `connect`) also changes `~/.claude/settings.json`, and `capture remove` takes the entries out — each only after showing the diff and asking first, unless you pass `--yes`. `capture off` leaves the entries, which add nothing while it's off, and every other change writes only this tool's own `config.toml`.

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
- `claudeglass capture prune [--dry-run]` — delete signal files, Claude Haiku's tag files and `capture-log.jsonl` and `habit-log.jsonl` records past your configured retention (`retention_days` in `config.toml`, or a default when it's unset); `serve`'s watcher already runs this same cleanup on every tick, so this is for anyone not running it.
- `claudeglass changes` and `claudeglass uninstall` also cover metrics capture: they list everything it installed and can remove all of it — hooks, skills and signal files included.

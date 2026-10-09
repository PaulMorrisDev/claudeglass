# How well the capture tags come out

With `[capture] tagger = "haiku"`, Claude Haiku writes the main
session's `[cg: ...]` tags from a short excerpt of each turn
([capture.md](capture.md#who-writes-the-tags)). This page is how well it
does, measured by `scripts/eval-tagger.py`: against known right answers,
with and without thinking, against Sonnet, and against the tags Claude
writes itself.

Short answer: **Haiku without thinking gets about 87% of the words right
on work it had never seen. On the recorded sessions, the ones Claude
tagged itself, it matches Claude's own tags (84% each).**
Thinking adds 5 points there, costs 3.2 times as much, and takes 13
seconds a call instead of 2. What made the difference was giving Haiku
the right facts, not more thought, so the hook runs Haiku without thinking.

[Final results](#final-results) and [Agent runs](#agent-runs) were
measured live on 0.15.0, on 2026-10-09. The sections before them record
how the hook got there, with the figures of their time.

## How it's measured

- **Sessions.** 38 scripted sessions were recorded as real Claude Code
  runs (`claude -p`, Sonnet as the session's model) in a small Python
  project, `scripts/tagger-eval/project/`: a stock list with a planted
  bug, repeated code, a typo, a broken command and a project skill. They
  cover every kind of task, clear and vague requests, follow-ups that
  build on, grow, redo or fix the last work, unrelated follow-ups, plans
  written in text, in plan mode, followed and departed from, research
  that finds its answer and research that doesn't, and a skill that ran.
  The capture note asked Claude for its own tags as it worked. 14 more
  sessions are written by hand, for what `claude -p` can't record (see
  [Sessions written by hand](#sessions-written-by-hand)). They count
  from 0.15.0's run on; earlier figures cover the 38 recorded ones.
- **Right answers.** `scripts/tagger-eval/scenarios.json` gives the
  right words for each session's last turn, for the keys with a clear
  answer. Some accept more than one word (a small change is `xs` or `s`),
  and some expect a key left out (`shift` on a first message).
- **Judges.** The last turn of each session is judged the way the hook
  does it: the same excerpt (with Claude's own tag taken out of the
  reply), the same instructions, the same word filter and corrections,
  the same `claude -p` call. There are three judges: Haiku without
  thinking, Haiku with a 4,000-token thinking budget, and Sonnet without
  thinking. Each judges each turn 3 times.
- **Scores.** A score is the share of scored keys, over every run, that
  got a right word. "Same answer in every run" is the share of turn and
  key pairs where all 3 runs agreed. Claude's own tags are scored the same
  way, from the one tag it wrote in the session (a session written by
  hand has none, so it is left out of that figure).
- **Held out.** The judge's instructions were improved using the first
  26 sessions (the tuning set). Then 12 more were written and recorded
  (the held-out set, `ho_*`), with their right answers written before any
  result was seen. They were never used to tune anything. Two more
  held-out sessions were written by hand after the tuning was done.

## First run: the hook as first written

On the 26 tuning sessions:

| Judge | Right | Same answer in every run | $ a call | Seconds a call |
|---|---|---|---|---|
| Haiku | 84% | 77% | $0.0016 | 1.7 |
| Haiku, thinking | 87% | 72% | $0.0053 | 10.3 |
| Sonnet | 90% | 94% | $0.0093* | 2.3 |
| Claude's own tags | 89% | – | – | – |

Most misses were the same for every judge, Claude included. That meant
they came from what the judge was shown and told, not from the model:

- **`plan` (83% for every judge).** The hook took "no ExitPlanMode call"
  to mean "no plan". But a plan written as steps in a reply is a plan,
  and headless plan mode has no ExitPlanMode tool: it writes the plan to
  a file under `~/.claude/plans/` instead.
- **`check` (31–42%, Claude's own too).** Running the test suite read as
  `check=run`, and research that changed nothing got a check.
- **`prior` (Haiku 50%).** Haiku left it out when a message only made
  sense with the conversation before it.
- **`task`.** A README typo fix read as `bugfix`, and a CI workflow as
  `feature`. The excerpt counted the files changed but didn't name them.

## What changed

- **The excerpt.** It now has the end of Claude's reply to your previous
  message (where a plan written in text usually is), the changed files by
  name, whether the shell commands ran tests, and plan mode's plan file.
- **Haiku's key lines.** Haiku now gets its own key lines, spelling out
  each word (`capture_catalogue.JUDGE_LINES`). An example is `check`:
  "targeted = ran the tests for the part changed; full = ran the whole
  test suite; …". Claude's note is unchanged.
- **The last instruction.** It now asks for every key that applies, with
  examples of when one doesn't, instead of inviting Haiku to leave out
  what it can't judge.
- **Corrections from facts.** What the transcript settles now overrides
  Haiku:
  - a plan-mode plan written this turn is `plan=made`, and it reads
    `following` once a plan was approved earlier;
  - no file changed is `check=none`. Files a subagent or a workflow agent
    changed, and commands that move or remove files, count as changes;
  - a test run is `targeted` or `full`, by whether the command picked
    tests, and a whole suite outranks chosen tests;
  - only documentation changed is `task=docs`;
  - a first message has no `shift` but `new`, no `why` and no `prior`
    but `none`;
  - `build` or `grew` is `fix` when your message corrects Claude, or
    tweaks the files the last reply changed;
  - `why` stays only with a redo or a fix, and `why=tools` only with a
    tool error;
  - Haiku's `admit` stays only when the reply reads like an admission;
  - `skill=helped` or `unneeded` is `none` when no skill ran.

  The old "no ExitPlanMode means no plan" correction is gone. The same
  rules now also settle Claude's own tags when ClaudeGlass reads them
  back (`capture_tags.settle`; the hook's twin is `grounded`), and a
  table test holds the two to each other. The tag file notes what the
  hook changed in its `g` field. The runs below came before the `why`,
  `admit`, `fix`, `following`, `skill` and subagent rules, and haven't
  been repeated.

## Final results

Measured on 0.15.0 on 2026-10-09, 3 runs per judge. The tuning set is 26
recorded and 12 hand-written sessions. The held-out set is 12 recorded
and 2 hand-written.

| Judge | Tuning (38) | Held out (14) | Same answer in every run | $ a call | Seconds a call |
|---|---|---|---|---|---|
| **Haiku** | **93%** | **87%** | 82% | $0.0024 | 2.0 |
| Haiku, thinking | 94% | 92% | 82% | $0.0077 | 13.2 |
| Sonnet | 98% | 96% | 92% | $0.0077 | 2.8 |
| Claude's own tags | 87% | 84% | – | – | – |

Claude's own tags cover the recorded sessions only, since a hand-written
session holds none.

The run found one fault in the corrections. A turn that ran tests but
changed no file had its `check` set from the tests, where the key line
says `none`. Every judge lost that session 3 times in 3. The rule now
tests "nothing changed" first. The figures above apply the fixed rule to
the same answers, which needs no new calls.

**Against 0.14.0.** The held-out sessions were also judged with 0.14.0's
hook: its excerpt, instructions and corrections, scored against the same
answer keys.

| Judge | 0.14.0, recorded (12) | 0.15.0, recorded (12) | 0.14.0, by hand (2) | 0.15.0, by hand (2) |
|---|---|---|---|---|
| Haiku | 87% | 84% | 92% | 100% |
| Haiku, thinking | 93% | 89% | 85% | 100% |
| Sonnet | 96% | 95% | 92% | 100% |

Over all 14, the two are level (0.15.0 first): Haiku 87% against 88%,
Haiku with thinking 92% against 92%, Sonnet 96% against 95%. The two
hand-written sessions are what 0.15.0 was built for: a tweak after a
plan, and a message typed while Claude worked. On the 12 recorded ones
0.15.0 is 1 to 4 points lower. Most of that is two things. `task` slips
on a few sessions: `debug` for an empty report to fix, `feature` for
adding type hints, and with thinking `feature` for a Dockerfile. And
"Actually, go back to £ but put the symbol after the number" reads as
`fix` in 6 of 9 runs, where the key says `redo`. 0.15.0's `fix` covers a
tweak of what Claude just delivered, so the two words overlap there. The
held-out set is never used to tune, so these are left as they are.

0.14.0's own release figures (90%, 93% and 94% held out) were scored
against the answer keys of its time, which still had `found`. With the
hook as first written, the held-out set scored Haiku 75%, Haiku with
thinking 83% and Sonnet 90%.

By key, over all 52. `why` and `admit` are new in 0.15.0, and only 3 and
2 sessions score them. `found` is gone.

| Key | Haiku | Haiku, thinking | Sonnet | Claude's own |
|---|---|---|---|---|
| task | 89% | 88% | 99% | 100% |
| brief | 88% | 97% | 99% | 100% |
| level | 93% | 79% | 98% | 91% |
| shift | 92% | 96% | 92% | 86% |
| why | 100% | 100% | 100% | – |
| admit | 100% | 100% | 100% | – |
| size | 100% | 92% | 100% | 92% |
| missing | 70% | 97% | 94% | 100% |
| plan | 93% | 97% | 97% | 97% |
| skill | 100% | 100% | 100% | 100% |
| prior | 86% | 92% | 98% | 44% |
| check | 100% | 97% | 99% | 41% |

## Does Haiku need thinking?

No:

- **Accuracy.** On the tuning set, thinking adds 1 point (94% against
  93%). On the held-out set it adds 5 (92% against 87%).
- **Cost and speed.** Each call costs 3.2 times as much ($0.0077 against
  $0.0024) and takes about 13 seconds instead of 2. The answers are no
  more consistent from run to run (82% both).
- **Where it helps.** `missing` (97% against 70%), `brief` and `prior`.
  `prior` says whether a message relied on the conversation before it.
  ClaudeGlass reads it least, and Claude's own tags get it most wrong
  (44%).
- **Where it hurts.** With thinking, Haiku is worse on `level` (79%
  against 93%) and `size`.

The first run is where thinking looked worth it (+3 on tuning, +8 on
held out). The gap narrowed once the excerpt carried the facts Haiku had
been guessing.

Sonnet is the most accurate and the most consistent. In 0.15.0's run it
cost $0.0077 a call, about 3 times Haiku. That is less per token than
0.14.0's hook paid in the same session ($0.0108 a call), so part of the
longer prompt was likely read from cache; the one-call-at-a-time measure
below predates 0.15.0.

## What a call really costs

\* The eval's own Sonnet cost figures came out lower, between $0.0009 and
$0.011 a call. They were cheaper than real use would be, because the
eval's repeat runs read one another's cache. Measured one call at a time,
each with a new excerpt as in real use:

| Model | Read | Written to cache | Read from cache | $ a call |
|---|---|---|---|---|
| Haiku | 1,681 tokens | – | – | $0.0019 |
| Sonnet | – | 2,234 tokens (1-hour cache) | none | $0.0093 |

- **Sonnet.** Claude Code writes the whole prompt to a 1-hour cache, at
  twice the input price: the fixed instructions plus the new excerpt,
  about 2,200 tokens. The next turn, with the same instructions and a new
  excerpt, read nothing back from it (0 tokens, measured twice), so every
  call pays the write.
- **Haiku.** The prompt is under Haiku's 4,096-token caching minimum, so
  it's never cached. It pays the plain input price, and costs the same
  every call.

`scripts/eval-tagger.py` now adds a line unique to each call, so its cost
column matches real use. The eval's accuracy figures are unaffected:
cached or not, the model reads the same prompt. 0.15.0's longer
instructions and excerpt put Haiku at $0.0024 a call in its run.

## What's still wrong

- **`missing` (Haiku 70%).** When a request lacks details, Haiku often
  says nothing was missing. Thinking fixes most of it.
- **`prior`.** Haiku sometimes says `some`, both where the message stood
  on its own and where it needed the conversation before it.
- **Where the right answer is arguable, every judge disagrees with the
  answer key.** After "list the steps", then "implement it, but don't use
  the csv module", every judge says the plan was `following`. The key
  says `deviated`.
- **Redo or fix.** A change of mind about what Claude just delivered
  reads as `fix`, where the key says `redo` (see
  [Final results](#final-results)).
- **Symptom versus fix.** Haiku calls "it prints nothing, fix it"
  `debug` rather than `bugfix`, and "ok fix it" after an investigation
  `shift=fix` rather than `build`. Haiku with thinking gets the second
  right.

## Sessions written by hand

`claude -p` can't make some sessions: a message typed while Claude works,
a plan sent back from the dialog with feedback, a desktop session's
lines, a screenshot, a workflow, or a session on a model other than
Sonnet. 14 scenarios marked `"source": "hand"` in `scenarios.json` have
their sessions written by hand, in `scripts/tagger-eval/sessions/`, in
the form the recorded ones are trimmed to. A hand-written session also
keeps some things the trimmer drops: a plan's text, the feedback typed
into the plan dialog, a tool result's note (a plan approved, a long output
saved to a file, a workflow started) and a background start's status,
which the excerpt counts as an agent's change. Nothing in them comes from
a real session. Their right answers were written before any judge saw
them.

`record` skips them and says so, so they are never overwritten. They
hold no tag of Claude's, so only the judges are scored on them and
Claude's own figures leave them out. The trimmer now keeps a message
typed while Claude worked (a `queued_command` line: its text and mode,
nothing else), so a session recorded in future carries one too. Every
other attachment is still dropped.

| Session | What it covers |
|---|---|
| `plan_build_rename` | A plan approved and built, then "rename remove_item to take_item": a fix. |
| `plan_build_three_fixes` | A plan built, then three short corrections in a row. The last says Claude missed a step the plan names. |
| `drip_three_small` | Three small edits to one file in a row. They build on each other and don't correct anything. |
| `claude_wrong` | The change breaks what was asked and the user says so. The reply doesn't own it, so no `admit`. |
| `claude_admits` | The earlier fix didn't work. The reply owns that its earlier change was the wrong one (`admit=change`). |
| `queued_correction` | A correction typed while Claude worked. The excerpt shows it. |
| `ho_post_plan_tweak` | Held out. A plan built, then a tweak to the output. |
| `ho_queued_scope` | Held out. A one-line request, and a message typed while Claude worked that widens it to a second file. |
| `desk_plan_rounds` | A plan sent back once with feedback, proposed again, then approved by typing "implement the plan". |
| `desk_interpreter_tests` | Tests run as `C:/Python311/python.exe -m pytest` on one file: chosen tests. |
| `desk_powershell_tests` | The whole suite through the PowerShell tool (`& C:\Python311\python.exe -m pytest`), the long first output saved to a file and read back. |
| `desk_replay_block` | The desktop app resumed the session and wrote the first turn again after the last line, with its old uuids. The last turn is a typo fix. |
| `desk_screenshot` | The request is a screenshot (an image block with no placeholder text) and one sentence. |
| `desk_opus_workflow` | One turn starts a Workflow and ends while its agents are still working, so what they change isn't in the transcript yet. |

The `desk_*` sessions carry `"entrypoint": "claude-desktop"` on every
line, as a desktop session's lines do, and an Opus model where the
recorded ones have Sonnet. `tests/test_eval_tagger.py` checks, for free,
that each session parses and that the excerpt of its last turn shows what
the case is for: the queued message, both test commands as tests, and the
plan rounds. In 0.15.0's run the 12 in the tuning set scored Haiku 88%,
Haiku with thinking 90% and Sonnet 97%, and the 2 held out 100% for
every judge. To judge some on their own, run
`python scripts/eval-tagger.py judge --only claude_wrong desk_replay_block`.

## Limits

- **One project, mostly one model.** All 52 sessions are one small Python
  project. The 38 recorded ones have Sonnet as the session's model. The
  14 written by hand are made up, not recorded; the 6 desktop ones are on
  Opus. Bigger sessions give a longer excerpt, but the excerpt is capped
  (see [capture.md](capture.md#who-writes-the-tags)).
- **The answer keys are one person's reading.** Some keys are judgment
  calls, which is why several accept more than one word.
- **Small numbers.** 3 runs per judge and 14 held-out sessions are
  enough to show large gaps (75% against 90%), not to rank judges a
  point or two apart.

## Agent runs

Since 0.11.0 no subagent is asked for a tag: Haiku judges each finished
run from an excerpt (see [capture.md](capture.md#agent-runs)).
`scripts/eval-agent-judge.py` builds known-answer runs with the hook's
own `agent_excerpt` and asks Haiku about each three times. The first 8
cases are runs that finished, finished part of the work, or were blocked;
a vague brief; a rerun on a stronger model and one for a missing tool;
and two runs that followed an earlier one without redoing it.

| Key | Right |
|---|---|
| `result` (done, partial, blocked) | 21 of 21 |
| `brief` (clear, vague) | 6 of 6 |
| `retry` (model, tools, or none) | 24 of 24 |
| All | 51 of 51 |

`retry` first read "only when this run redoes an earlier run": Haiku then
left it out of a rerun for a missing tool 2 times in 3. Asked for it
every time, with `none` as an answer (dropped before it's kept), it
named both reruns every time and still called no follow-on run a retry.
One case stays out: a sequence you script yourself ("start an agent with
a vague brief, then one with a precise brief") reads as your plan, not a
rerun, and Haiku says `none`.

That table is 0.14.0's judge on those 8 cases. In 0.15.0 the judge asks
for the brief and what it lacked before the result, counts findings,
refuted claims and an empty list as done, reads a workflow agent's
computed task as the brief and its answer field by field, and writes
`retry=model` itself when the same brief reruns on a higher model tier.
The script has 13 more cases for this:

- **Workflow agents that are done.** One refutes a claim, one returns an
  empty list, one returns three findings, a validator confirms one claim
  and refutes another, an implementer did the work and lists its
  concerns, and research answers and lists its risks.
- **A "Continue" relay.** The workflow was started with the line
  "Continue", which says nothing of the task. The computed task is the
  brief (clear), and the relay is context beside it.
- **Hand-backs.** One says it could not work (blocked). Another hands back
  its report and then says "Report delivered." (done: the handback stays
  the answer).
- **A run a stop hook's guard sent back once.** It carried on and finished
  (done). The newest verdict is the run's, so the final answer is scored.
- **An answer written after the hook fired.** The case builds the excerpt
  the way the hook's worker does, after waiting for the late lines to
  arrive (done).
- **One brief, two runs.** The same brief on a run that finished (done)
  and on one that was blocked. The brief reads clear both times.

Each case has an answer shape: `report`, `findings`, `empty`, `verdicts`,
`concerns`, `risks`, `handback` or `blocked` (a run that says it could not
do its work, however it says so). The script prints accuracy per key and
per shape. Before 0.15.0 a miss tended to follow the shape of the
answer, not the key: a list of concerns read as unfinished work, or a
refuted claim read as a blocked run.

0.15.0's judge on all 21 cases, 3 runs each, on 2026-10-09:

| Key | Right |
|---|---|
| `result` (done, partial, blocked) | 57 of 60 |
| `brief` (clear, vague) | 21 of 21 |
| `retry` (model, tools, or none) | 24 of 24 |
| All | 102 of 105 (97%) |

Every shape scored 100% but `blocked`, at 12 of 15. The one miss is the
first blocked case: asked to list the keys of a settings file that
doesn't exist, the run says there were no keys to list. Haiku reads that
as an empty answer, so `done`, 3 times in 3. The hand-back that says it
could not work, and the blocked run of the shared brief, both read
`blocked`. A missing input that leaves an empty answer is the case where
"an empty list is done" and "a missing file is blocked" meet.

### Replaying real runs

The cases are made up, so they can't say how often Haiku is right on your
own runs. A replay set can. Check about 50 real runs by hand and write a
labels file, a JSON list with one entry for each run:

    {"transcript": "C:/Users/you/.claude/projects/.../subagents/agent-a1b2.jsonl",
     "agent_reply": "msg_01...", "shape": "findings",
     "right": {"result": "done", "brief": "clear"}}

`agent_reply` is the id of the agent's last reply, `shape` is one of the
words above, and `right` holds `result` (done, partial or blocked) and
`brief` (clear, partial or vague), either or both. The file holds paths,
reply ids and these words only, never any text of a run, and the loader
refuses anything else. Keep it outside the repository: the loader refuses
a labels file inside it.

    python scripts/eval-agent-judge.py --replay ~/agent-judge-labels.json                   # asks Haiku, as the cases do
    python scripts/eval-agent-judge.py --replay ~/agent-judge-labels.json --stored CONFIG_DIR   # spends nothing

The first builds each excerpt from the transcript the way the hook's
worker does and asks Haiku, about $0.0015 a call. The second asks nothing.
It scores the verdicts the hook already wrote to `CONFIG_DIR/tags/`
against your labels. A run's verdict is the one on its newest reply that
has one, as the dashboard reads it. So a run judged before its last lines
were written still counts. It reports accuracy per key and per shape, how
often the stored verdict says `result=done` against how often your labels
do, and how many labelled runs have no stored verdict. The "Finished"
share is the figure to watch: the audit that led to the judge's changes
found about 43 of 46 runs it called unfinished had delivered.

For 0.15.0, 89 finished runs from the last 30 days on this machine were
checked one by one by agents reading each transcript, blind to the
stored verdicts: 60 workflow agents and 29 direct ones. An Opus agent
labelled 52, and a Sonnet agent labelled 50 in an earlier pass. On the
13 runs both labelled, they agreed on every result and brief. All 89 runs
had delivered, and every brief was clear. So the set shows how often a
finished run is called finished. It can't show whether the judge spots a
partial or blocked run; the known-answer cases cover that.

"Stored" is the verdict the builds before 0.15.0 wrote at the time,
scored with `--stored`. "0.15.0" is the live `--replay`, 3 runs each, on
2026-10-09.

| Answer shape | Runs | Stored: `done` | Stored: `clear` | 0.15.0: `done` | 0.15.0: `clear` |
|---|---|---|---|---|---|
| report | 34 | 24/34 (71%) | 24/34 (71%) | 93/102 (91%) | 98/102 (96%) |
| findings | 15 | 3/15 (20%) | 9/15 (60%) | 45/45 (100%) | 45/45 (100%) |
| handback | 15 | 11/15 (73%) | 15/15 (100%) | 45/45 (100%) | 45/45 (100%) |
| verdicts | 11 | 4/11 (36%) | 4/11 (36%) | 33/33 (100%) | 33/33 (100%) |
| concerns | 9 | 4/9 (44%) | 7/9 (78%) | 24/27 (89%) | 27/27 (100%) |
| risks | 3 | 1/3 (33%) | 2/3 (67%) | 9/9 (100%) | 9/9 (100%) |
| empty | 2 | 0/2 (0%) | 0/2 (0%) | 6/6 (100%) | 6/6 (100%) |
| All | 89 | 47/89 (53%) | 61/89 (69%) | 255/267 (96%) | 263/267 (99%) |

The "Finished" share on these runs goes from 53% to 96%. Every run has a
stored verdict, so none were left out. The 0.15.0 misses are 5 workflow
runs, 4 reports and a list of concerns, each called `partial` in at least
one run. The stored verdicts often took a workflow's relay line for the
brief, which is why `clear` was low.

## Run it again

It spends real tokens, so it never runs in CI. Judging all 52 sessions
with the three judges cost $2.77 and took 8 minutes in 0.15.0's run: 3
runs each, at the per-call costs and times above. The held-out set on an
older hook cost $0.82.

Both scripts call `claude -p` through your own Claude Code login, so a
refresh needs the `claude` command in your terminal to be signed in. Run
`claude` once and log in if it isn't. The tables on this page come from
`python scripts/eval-tagger.py judge` (the main session's tags) and
`python scripts/eval-agent-judge.py` (agent runs).

    python scripts/eval-tagger.py judge                      # judge every session
    python scripts/eval-tagger.py score                      # the report, from the newest results
    python scripts/eval-tagger.py judge --hook OLD/capture-hook.py --set holdout   # an older hook (0.14.0's file name), to compare
    python scripts/eval-tagger.py record --only NEW_ID       # record a new scenario (under a dollar with Sonnet)
    python scripts/eval-tagger.py record                     # skips the 14 written by hand, and says so

Run it after changing the excerpt, the key lines or the corrections, and
compare the held-out column with this page.

    python scripts/eval-agent-judge.py                       # the agent runs, about $0.10
    python scripts/eval-agent-judge.py --replay ~/agent-judge-labels.json  # your labelled real runs, about $0.0015 a call
    python scripts/eval-agent-judge.py --replay ~/agent-judge-labels.json --stored CONFIG_DIR   # the same labels against the stored verdicts, free

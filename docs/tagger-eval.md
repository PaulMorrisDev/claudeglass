# How well the capture tags come out

With `[capture] tagger = "haiku"`, Claude Haiku writes the main
session's `[cg: ...]` tags from a short excerpt of each turn
([capture.md](capture.md#who-writes-the-tags)). This page is how well it
does, measured by `scripts/eval-tagger.py`: against known right answers,
with and without thinking, against Sonnet, and against the tags Claude
writes itself.

Short answer: **Haiku without thinking gets about 90% of the words right
on work it had never seen, a little ahead of Claude's own tags (85%).**
Thinking adds 3 points there, costs 3.4 times as much, and takes 12
seconds a call instead of 2. What made the difference was giving Haiku
the right facts, not more thought, so the hook runs Haiku without thinking.

Every table on this page was measured before 0.15.0. That release changed
the excerpt and the corrections the judges are given, and added the
hand-written sessions and the new agent cases below. None of those has
been judged live yet, so the tables are a baseline and not the current
score. [Run it again](#run-it-again) to refresh them.

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
  [Sessions written by hand](#sessions-written-by-hand)); the figures on
  this page were measured before they were added.
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

| Judge | Tuning (26) | Held out (12) | Same answer in every run | $ a call | Seconds a call |
|---|---|---|---|---|---|
| **Haiku** | **96%** | **90%** | 83% | $0.0019 | 1.7 |
| Haiku, thinking | 96% | 93% | 82% | $0.0064 | 12.2 |
| Sonnet | 98% | 94% | 92% | $0.0093* | 2.4 |
| Claude's own tags | 89% | 85% | – | – | – |

On the held-out set, the same sessions judged with the hook as first
written score Haiku 75%, Haiku with thinking 83% and Sonnet 90%. So most
of the gain holds on work the changes weren't tuned on.

By key, over all 38. This run also scored `found`. That key has since
been dropped, so the scenarios no longer expect it and a new run won't
show it.

| Key | Haiku | Haiku, thinking | Sonnet | Claude's own |
|---|---|---|---|---|
| task | 96% | 97% | 100% | 100% |
| brief | 100% | 100% | 100% | 100% |
| level | 85% | 79% | 91% | 91% |
| shift | 88% | 93% | 88% | 86% |
| size | 100% | 90% | 100% | 92% |
| missing | 100% | 100% | 100% | 100% |
| plan | 95% | 97% | 97% | 97% |
| skill | 100% | 100% | 100% | 100% |
| found | 97% | 95% | 98% | 97% |
| prior | 69% | 98% | 92% | 44% |
| check | 98% | 92% | 94% | 41% |

## Does Haiku need thinking?

No:

- **Accuracy.** On the tuning set, thinking scores the same as no
  thinking (96%). On the held-out set it adds 3 points (93% against 90%).
- **Cost and speed.** Each call costs 3.4 times as much ($0.0064 against
  $0.0019) and takes about 12 seconds instead of 2. The answers are no
  more consistent from run to run (82% against 83%).
- **Where it helps.** Its one clear gain is `prior`: whether a message
  relied on the conversation before it. That key is one ClaudeGlass reads
  least, and it's the one Claude's own tags get most wrong (44%).
- **Where it hurts.** With thinking, Haiku is worse on `level` and
  `size`.

The first run is where thinking looked worth it (+3 on tuning, +8 on
held out). The gap closed once the excerpt carried the facts Haiku had
been guessing.

Sonnet is the most accurate and the most consistent, but it costs about
5 times as much as Haiku.

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
cached or not, the model reads the same prompt.

## What's still wrong

- **`prior`.** Haiku sometimes leaves it out on a follow-up.
- **Where the right answer is arguable, every judge disagrees with the
  answer key:**
  - After "list the steps", then "implement it, but don't use the csv
    module", every judge says the plan was `following`. The key says
    `deviated`.
  - "Where does it send emails?" in a project that sends none reads as
    `found=yes` to Sonnet and to Haiku with thinking. The key says `no`.
- **Symptom versus fix.** Haiku calls "it prints nothing, fix it"
  `debug` rather than `bugfix`, and "ok fix it" after an investigation
  `shift=fix` rather than `build`.

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
plan rounds. These sessions have not been judged yet; the tables above
are the 38 recorded ones. To judge some, run
`python scripts/eval-tagger.py judge --only claude_wrong desk_replay_block`.

## Limits

- **One project, mostly one model.** All 52 sessions are one small Python
  project. The 38 recorded ones have Sonnet as the session's model. The
  14 written by hand are made up, not recorded; the 6 desktop ones are on
  Opus. Bigger sessions give a longer excerpt, but the excerpt is capped
  (see [capture.md](capture.md#who-writes-the-tags)).
- **The answer keys are one person's reading.** Some keys are judgment
  calls, which is why several accept more than one word.
- **Small numbers.** 3 runs per judge and 12 held-out sessions are
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

The figures above were measured on those 8 cases, before the judge
changed in 0.15.0. It now asks for the brief and what it lacked
before the result, counts findings, refuted claims and an empty list as
done, reads a workflow agent's computed task as the brief and its answer
field by field, and writes `retry=model` itself when the same brief
reruns on a higher model tier. The script has 13 more cases for this,
which the table doesn't count yet:

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
per shape. A miss tends to follow the shape of the answer, not the key:
a list of concerns read as unfinished work, or a refuted claim read as a
blocked run. The table above counts the first 8 cases only. Run
`python scripts/eval-agent-judge.py` to measure all 21, and to see the
figures per shape, before quoting any.

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

Before 0.15.0, a replay set of 50 real runs on this machine was checked
one by one by an agent reading each transcript. The mix was 20
workflow agents that answered through `StructuredOutput`, 10 that wrote
a report, 10 handbacks and 10 plain reports. The agent labelled each
run's result and brief without seeing the stored verdict. All 50 runs
had delivered and all 50 briefs were clear. These figures score the
verdicts the builds before 0.15.0 stored, so they are the figures to
beat. Right counts the result and the brief of every run.

| Answer shape | Right | `result=done` said | `result=done` right |
|---|---|---|---|
| findings | 2/12 (17%) | 0/6 (0%) | 6/6 (100%) |
| verdicts | 3/4 (75%) | 2/2 (100%) | 2/2 (100%) |
| concerns | 8/12 (67%) | 3/6 (50%) | 6/6 (100%) |
| report | 33/50 (66%) | 17/25 (68%) | 25/25 (100%) |
| empty | 0/2 (0%) | 0/1 (0%) | 1/1 (100%) |
| handback | 18/20 (90%) | 8/10 (80%) | 10/10 (100%) |
| All | 64/100 (64%) | 30/50 (60%) | 50/50 (100%) |

All 50 runs have a stored verdict, so none were left out. The 0.15.0
judge's figure needs the live `--replay`, run with a signed-in `claude`.

## Run it again

It spends real tokens, so it never runs in CI. Judging all 52 sessions
with the three judges costs about $2.75 and takes about 7 minutes: 3 runs
each, at the per-call costs and times above.

Both scripts call `claude -p` through your own Claude Code login, so a
refresh needs the `claude` command in your terminal to be signed in. Run
`claude` once and log in if it isn't. The tables on this page come from
`python scripts/eval-tagger.py judge` (the main session's tags) and
`python scripts/eval-agent-judge.py` (agent runs). Until both have been
run again on 0.15.0, the figures above are the earlier ones.

    python scripts/eval-tagger.py judge                      # judge the recorded sessions
    python scripts/eval-tagger.py score                      # the report, from the newest results
    python scripts/eval-tagger.py judge --hook OLD/capture_hook.py --set holdout   # an older hook, to compare
    python scripts/eval-tagger.py record --only NEW_ID       # record a new scenario (under a dollar with Sonnet)
    python scripts/eval-tagger.py record                     # skips the 14 written by hand, and says so

Run it after changing the excerpt, the key lines or the corrections, and
compare the held-out column with this page.

    python scripts/eval-agent-judge.py                       # the agent runs, about $0.10
    python scripts/eval-agent-judge.py --replay ~/agent-judge-labels.json  # your labelled real runs, about $0.0015 a call
    python scripts/eval-agent-judge.py --replay ~/agent-judge-labels.json --stored CONFIG_DIR   # the same labels against the stored verdicts, free

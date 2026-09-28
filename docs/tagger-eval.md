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

## How it's measured

- **Sessions.** 38 scripted sessions were recorded as real Claude Code
  runs (`claude -p`, Sonnet as the session's model) in a small Python
  project, `scripts/tagger-eval/project/`: a stock list with a planted
  bug, repeated code, a typo, a broken command and a project skill. They
  cover every kind of task, clear and vague requests, follow-ups that
  build on, grow, redo or fix the last work, unrelated follow-ups, plans
  written in text, in plan mode, followed and departed from, research
  that finds its answer and research that doesn't, and a skill that ran.
  The capture note asked Claude for its own tags as it worked.
- **Right answers.** `scripts/tagger-eval/scenarios.json` gives the
  right words for each session's last turn, for the keys with a clear
  answer. Some accept more than one word (a small change is `xs` or `s`),
  and some expect a key left out (`found` outside research).
- **Judges.** The last turn of each session is judged the way the hook
  does it: the same excerpt (with Claude's own tag taken out of the
  reply), the same instructions, the same word filter and corrections,
  the same `claude -p` call. There are three judges: Haiku without
  thinking, Haiku with a 4,000-token thinking budget, and Sonnet without
  thinking. Each judges each turn 3 times.
- **Scores.** A score is the share of scored keys, over every run, that
  got a right word. "Same answer in every run" is the share of turn and
  key pairs where all 3 runs agreed. Claude's own tags are scored the same
  way, from the one tag it wrote in the session.
- **Held out.** The judge's instructions were improved using the first
  26 sessions (the tuning set). Then 12 more were written and recorded
  (the held-out set, `ho_*`), with their right answers written before any
  result was seen. They were never used to tune anything.

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
  - a plan-mode plan written this turn is `plan=made`;
  - no file changed is `check=none`;
  - a test run is `targeted` or `full`, by whether the command picked
    tests;
  - only documentation changed is `task=docs`;
  - a first message has no `shift` but `new`.

  The old "no ExitPlanMode means no plan" correction is gone.

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

By key, over all 38:

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

## Limits

- **One project, one model.** All 38 sessions are one small Python
  project, with Sonnet as the session's model. Bigger sessions give a
  longer excerpt, but the excerpt is capped (see
  [capture.md](capture.md#who-writes-the-tags)).
- **The answer keys are one person's reading.** Some keys are judgment
  calls, which is why several accept more than one word.
- **Small numbers.** 3 runs per judge and 12 held-out sessions are
  enough to show large gaps (75% against 90%), not to rank judges a
  point or two apart.

## Agent runs

Since 0.11.0 no subagent is asked for a tag: Haiku judges each finished
run from an excerpt (see [capture.md](capture.md#agent-runs)).
`scripts/eval-agent-judge.py` builds known-answer runs with the hook's
own `agent_excerpt` and asks Haiku about each three times: runs that
finished, finished part of the work, or were blocked; a vague brief; a
rerun on a stronger model and one for a missing tool; and two runs that
followed an earlier one without redoing it.

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

## Run it again

It spends real tokens, so it never runs in CI. Judging all 38 sessions
with the three judges costs about $1 and takes about 3 minutes.

    python scripts/eval-tagger.py judge                      # judge the recorded sessions
    python scripts/eval-tagger.py score                      # the report, from the newest results
    python scripts/eval-tagger.py judge --hook OLD/capture-hook.py --set holdout   # an older hook, to compare
    python scripts/eval-tagger.py record --only NEW_ID       # record a new scenario (under a dollar with Sonnet)

Run it after changing the excerpt, the key lines or the corrections, and
compare the held-out column with this page.

    python scripts/eval-agent-judge.py                       # the agent runs, about $0.08

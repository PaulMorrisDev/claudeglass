# Building in a fresh session after a big plan (`handoff.py`)

When you plan in a session, Claude reads files, searches and discusses
before you approve the plan. If you then build in the same session,
every later reply carries all of that planning context and pays to read
it again from the cache. A fresh session that starts from the plan
alone carries only Claude Code's own starting context and the plan.

`handoff.py` measures what that difference cost, per main session and
per approved plan, and the `plan-handoff` recommendation suggests the
habit when it would have saved enough.

## Why `/clear`, not a fork

`/branch` and `claude --continue --fork-session` copy the whole
conversation into the new session, so the build still carries the
planning context. Only `/clear` (or a new session) starts fresh. Claude
Code saves an approved plan under `~/.claude/plans`, so the new session
can be asked to carry it out, one phase per session.

## What it measures

For every main session (scheduled checks left out) and every
`ExitPlanMode` call you approved: its result came back without an error
(`Turn.plan_stats.outcome == "approved"`), or the dialog sent it back and
you then typed a go-ahead before the next plan, or left plan mode
(`"approved_by_message"`):

- **Fresh start.** The session's starting context (its first reply's
  context less your first message, characters / 4) plus the plan
  (`plan_stats.chars` / 4). `habits` uses the same starting context.
- **Planning context kept.** The approving reply's context less the
  fresh start.
- **The window.** The replies after the plan, up to the session's next
  conversation summary (a summary already dropped the planning) or its
  next approved plan (which starts a window of its own).
- **The saving.** Each reply in the window priced with the kept context
  taken out of its cache reads (then its cache writes), the same shrink
  the auto-compact replay uses (`compaction_sim._shrunk_cost`). Less two
  costs a fresh start adds back: the first reply writes the fresh start
  to the cache (5-minute) instead of reading it, and an allowance for
  re-reading files, the one `compaction_sim` measures from this corpus's
  real summaries. Never below zero. The allowance comes from the
  sessions in view, and is $0.00 when they include no real summaries,
  so one project's view can show a larger saving for the same session
  than the all-projects view does.

A plan counts when the kept context is at least
`plan_handoff_min_dropped_tokens` (default 40,000) and at least
`plan_handoff_min_later_turns` (default 10) replies follow it. The
saving is an upper bound: a thin plan can send the fresh session back
to files the planning already read, or to questions it already
answered.

The **build** is priced too: the replies after each approved plan, up to
the next `ExitPlanMode` call, as they ran and at Sonnet's list price
(the rate card's `sonnet` alias). The "plan on Opus, build on Sonnet"
estimate reads these columns.

## How each build began

The tip above reads the plans that count. `plan_handoff_approvals` looks
at every plan you approved, in the dialog or by typing a go-ahead, and at
how its build began. A plan you declined and then told Claude to carry
out ("implement the plan") counts as an approval by typing, and the plan
Claude puts up again unchanged after that is the same plan.

A build is one of three kinds (`handoff.START_WORDS`):

- `kept`: it carried on in the planning session. This is the default.
- `cleared`: you ran `/clear` within 60 seconds of the approval
  (`FRESH_CLEAR_S`), in the same session or as the first thing in a
  new session of the same project.
- `handoff`: a new session of the same project began within an hour of
  the approval (`HANDOFF_LINK_S`) and its first message is Claude Code's
  "Implement the following plan:" (`Turn.human_plan_handoff`, a flag; the
  message is not kept).

The time of the approval is the line that approved it
(`PlanStats.approved_ts`). A fresh build is the new session's replies up
to its own first plan. Each approval is claimed by at most one new
session and each session starts at most one build, the latest approval
before it winning. A session with no project, or an approval with no
time (a digest from before parser 43), is never linked, so it reads as
carried on.

The table has a row for each kind that happened: approvals, how many
were typed, the planning context a build carried, its replies, the
context a typical reply read, the cost per reply and the cost in all.
Compare the context read per reply across the rows. It shows whether a
fresh start would have paid, before you rely on the saving above. The
Builds after a plan check on the Overview says the same in a sentence
and points here. It is information: the saving stays with the card.
The live `plan_fresh` hint does not read it.

## How often a plan is sent back

The Work habits table `habits_plan_rounds` (`habits.PlanFact`) looks at
the asks, not the approvals. One ask is every plan Claude put up for it,
from the first to the one you approved (`handoff.plan_groups`). A plan
counts as sent back when the dialog sent it back and you did not then
type a go-ahead. Claude's edits to its plan file between two plans are
not a build: more than 8 replies that change files between two plans
(`PLAN_BUILD_REPLIES`) start another ask.

Each row groups the approved plans by how many times plans were sent back
first (none, once, twice, three times or more), with the asks whose plan
you never approved in the last row. The tokens and cost are of the
replies after the first plan, through the approval, so an ask with one
plan costs nothing there. How your feedback read on each plan sent
back (`PlanStats.feedback_class`, a closed word) says which rounds a
standing request to critique the plan might have covered: a question, a
critique or a doubt. The `plan-rounds` card fires at 5 approved plans or
more, 30% or more of them sent back, 30% or more of the rounds a
question, a critique or a doubt, and $1 or more to spare. The saving is a
quarter of the rounds' cost, scaled to that share
(`plan_rounds_saving_factor`): a critique will not spare every round.
Its fix is one line for your first planning message, CLAUDE.md or a plan
skill. The message-level `plan_cost` (the cost of planning the habit
"Skip plan mode for easy changes" counts) now runs to the last plan put
up, not the first.

## The report section

`plan_handoff`, always emitted, with three tables:
`plan_handoff_summary` (one row), `plan_handoff_by_session` and
`plan_handoff_approvals`. See
[`sections-reference.md`](sections-reference.md#plan_handoff-handoffpy)
for the columns. The dashboard shows all three on Spend › Savings
(`GET /api/plan-handoff`). `habits_plan_rounds` is on Work habits.

## The recommendation

`plan-handoff` (severity `advice`, category `workflow`, no setting
change) fires when at least `plan_handoff_min_sessions` (default 3)
sessions have a plan that counts and the saving is at least
`plan_handoff_min_saving_share_pct` (default 1%) of main-session cost.
Its fix is a prompt that adds a short reminder to your CLAUDE.md.

Its saving overlaps with the auto-compact window's (`compaction-window`):
both come from carrying less context in later replies. The
Overview's available saving counts the auto-compact figure only, and the
card says the two together save less than their sum. It overlaps with
the "split large asks into planned steps" habit the same way.

## Your feedback

After a piece of work in which you approved a plan, `/cg-feedback` asks
a fifth question in a second call: "Could the build have started in a
fresh session from the plan alone?" (`yes`, `partly`, `no`; the tag's
`handoff` key). `habits.habits_by_shape` counts the answers on sessions
that planned and built (`plan_build`). Once there are at least three
(`handoff.MIN_FEEDBACK_ANSWERS`):

- More than half `no`: the card becomes "Write fuller plans, then build
  in a fresh session". A fresh start would have lost what the build
  needed, so it suggests adding the decisions, file paths and
  constraints to the plan first.
- More than half `yes`: the card cites them ("You said 5 of 6 builds
  could have started from the plan") and tells you to run `/clear` as
  soon as you approve the plan.
- More than half of the rated planned builds too costly (`worth=no`):
  the card says so.

The fixes after a plan count too. Two answers sort a fix into three
groups: the plan check (Claude asks it before the first correction after
a plan is built, `plan_check`) and the plan question in `/cg-feedback`
(`plan`: "After you approved the plan, did it cover what you then fixed or
added?"). They show as `plan_covered`, `plan_gap` and `plan_new` in
`habits_by_shape`:

- `covered`: the plan said it, and the build missed it. These join the
  `check_work` habit as misses in the plan, so it reads "Have Claude tick
  off each plan step before it says done" when most are there.
- `gap`: the plan left it out. When more than half of at least three of
  these answers are `gap`, the card becomes "Write fuller plans, then
  build in a fresh session" too, with its own sentence: "You said 3 of 4
  fixes after a plan were things it left out". Its fix prompt asks Claude
  to add the missing decisions before you approve.
- `new`: you thought of it later. It is no rework: the message it
  followed is not counted as redone, so it stays out of Redone and the
  waste figures.

The daily run also writes your handoff answers into `coaching.json`
(see [coaching.md](coaching.md)). With more than half `yes` the live
`plan_fresh` hint speaks sooner, after `plan_handoff_min_dropped_tokens`
divided by `coaching_rearm_factor`. With more than half `no` it is off,
because a fresh start would have lost what the build needed.

With fewer answers the card is as above.

## Plan on Opus, build on Sonnet

Claude Code's `opusplan` model setting uses Opus in plan mode and
Sonnet otherwise. When the main session ran on Opus, the "Cheaper
models where it's safe" profile goal offers `model = opusplan`,
unticked. `whatif` prices it as `build_usd` less `build_usd_sonnet`
(fidelity `ceiling`). Sessions without a plan would run on Sonnet too,
and their saving isn't counted. The desktop app's model picker
overrides `settings.json`, so the candidate says to choose `opusplan`
there or run `/model opusplan`.

The `plan-then-build` catalogue profile is suggested when at least half
the main sessions plan and build in one session; see
[`profiles.md`](profiles.md).

## Thresholds

In `config.toml`'s `[thresholds]` table, all prefixed `plan_handoff_`
because the table is shared by every module:

| Key | Default | Meaning |
|---|---|---|
| `plan_handoff_min_dropped_tokens` | 40000 | Context a fresh start must drop for a plan to count |
| `plan_handoff_min_later_turns` | 10 | Replies that must follow the plan |
| `plan_handoff_min_sessions` | 3 | Sessions with a counting plan before the card shows |
| `plan_handoff_min_saving_share_pct` | 1.0 | Minimum saving, as a share of main-session cost |
| `plan_handoff_top_n` | 20 | Sessions listed in `plan_handoff_by_session` |

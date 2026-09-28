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
`ExitPlanMode` call whose result came back without an error
(`Turn.plan_stats.outcome == "approved"`):

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

## The report section

`plan_handoff`, always emitted, with two tables:
`plan_handoff_summary` (one row) and `plan_handoff_by_session`. See
[`sections-reference.md`](sections-reference.md#plan_handoff-handoffpy)
for the columns. The dashboard shows both on Spend › Savings
(`GET /api/plan-handoff`).

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
fresh session from just the plan?" (`yes`, `partly`, `no`; the tag's
`handoff` key). `habits.habits_by_shape` counts the answers on sessions
that planned and built (`plan_build`). Once there are at least three
(`handoff.MIN_FEEDBACK_ANSWERS`):

- More than half `no`: the card becomes "Write fuller plans, then build
  in a fresh session". A fresh start would have lost what the build
  needed, so it suggests adding the decisions, file paths and
  constraints to the plan first.
- More than half `yes`: the card cites them ("You said 5 of 6 builds
  could have started from the plan").
- More than half of the rated planned builds too costly (`worth=no`):
  the card says so.

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

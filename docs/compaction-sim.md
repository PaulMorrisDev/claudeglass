# `autoCompactWindow` sweep (`compaction_sim.py`)

Claude Code auto-compacts a session once its context nears
`autoCompactWindow` tokens — observed in config snapshots (e.g.
`300000`; a model's own context window is typically 1,000,000 for a
"[1m]"-aliased model, 200,000 otherwise, per `context_budget.py`'s own
assumed-window constants). `CLAUDE_CODE_AUTO_COMPACT_WINDOW`, while
it's set, overrides the setting, so it's the window that counts then
(`snapshots.auto_compact_window`). That single number is a real,
user-settable lever, and it trades off two costs directly against each
other:

- **Compacting less often** (a larger window, or `none` at all) means
  every turn keeps carrying a bigger context, priced at the cheap
  `cache_read` rate most of the time — but the context keeps growing
  until the harness's own ceiling forces a much larger rewrite, and every
  turn pays cache-read on a bigger prefix in the meantime.
- **Compacting more often** (a smaller window) pays for the summary
  request, the re-cache after it and the files re-read after it every
  time, but every later turn carries a smaller context afterwards.

Every number this module reports leads to a lever
(`autoCompactWindow`) and a projected saving in dollars — the project's
stated purpose (see the top-level README).

`compaction_sim.py` answers "what would this session have cost under a
different `autoCompactWindow`" by replaying its own priced turns, in
order, under each of a fixed set of candidate windows, and comparing the
total simulated cost to what the session actually cost.

## What it measures, precisely

A **real** compaction (one already recorded as a `compact_boundary`
event in the transcript) costs, and saves, exactly what it already did —
this module never re-simulates or removes it. A **simulated** compaction
is shaped like this corpus's real ones:

- **When it fires**: once the running context passes the candidate
  window less a trigger reserve. Claude Code summarises below the
  window (a 300,000 window fires near 267,000); the reserve is the
  median `window − preTokens` across real auto compactions in sessions
  whose configured window is known (0 when there are none).
- **What it leaves**: the session's own starting context (its first
  reply's context: system prompt, tools, CLAUDE.md, skills listing,
  about 80,000 tokens on a typical corpus) plus a summary of this
  corpus's median `postTokens` (20,000 when there are none). A summary
  replaces the conversation, not what sits in front of it, and its size
  barely tracks how long the conversation was. A compaction that would
  not shrink the context is skipped.
- **What it costs**:
  1. The **summary request**, which the transcript never logs: the
     triggering reply's context, read and written the way that reply's
     was, with the median output of this corpus's real summary requests
     as its output. The parser estimates each at about twice the summary
     Claude Code keeps, since the request writes an analysis first; with
     none, the summary size stands in.
  2. The **reply after it**, re-caching its whole new context: the
     share of the starting context real compactions still read from
     cache (the system prompt and tools; median across real ones, 0
     when there are none) is read, the rest written at that reply's own
     5-minute/1-hour mix.

Every turn after a simulated compaction has the tokens that summary
dropped taken out of its context and cache reads (and out of its cache
writes once the reads are used up), until the next compaction; content
added after the summary is kept whole. A real compaction resets this.
The files a session re-reads after a summary can't be seen in the
transcript being replayed, so the sweep doesn't charge them; the
`compaction-window` rule corrects for them instead. Candidate windows swept:
`100k, 150k, 200k, 250k, 300k, 400k, 500k, none` (`none` = never
auto-compact; a real compaction already in the transcript is still kept
under this row — see the "no candidate window" identity below). Because
real compactions are kept, a window above the one a session ran at costs
what the session did: the sweep can only test smaller windows, and the
compaction quick action and the section notes say so. Main sessions a
scheduled or looped task started, with no message of yours
(`model.scheduled_main_session`), are not replayed: they never compact,
so they would only lower the compactions per session the rule gates on.

## The report section

| Table | Scope | What it shows |
|---|---|---|
| `compaction_sim_by_window` | Top-level sessions only | One row per candidate window: simulated compactions per session, mean ctx, total cost, and delta vs. the observed (`none`) cost, in USD and percent. |
| `compaction_sim_by_agent_type` | `"top-level"` and every subagent type | Each key's own cheapest candidate window, its cost, the saving vs. observed (0 floor), and a recommendation string naming the window (only the `"top-level"` row says to set it). |
| `compaction_sim_by_task` | Main sessions only, grouped by the kind of task metrics capture reported (`task=`) | Same shape as `compaction_sim_by_agent_type`, keyed by task instead of agent type. A task appears only once at least `MIN_TASK_SESSIONS` (5) main sessions reported it. |
| `compaction_sim_fidelity` | Top-level sessions with a known configured window | Simulating at the session's own snapshot-configured `autoCompactWindow` against its true observed cost — a trust check on the simulation itself. |

The `compaction_sim_by_agent_type`/`compaction_sim_by_task` recommendation
strings name the cheapest window only when it is both below
`CompactionSimThresholds.switch_pct` (default 0.95) × observed cost and
more than `switch_usd` (default $1.00) cheaper. The window is one setting
for the whole session, so a subagent type's row names its cheapest
window ("Cheapest at 200,000 tokens (saves ...), but the window is one
setting for the whole session: choose it from the main session row")
without telling you to set it; only the `"top-level"` row says "Set the
auto-compact window to ...".

`compaction_sim_by_task` (EST-P8) is accumulated the same way as
`compaction_sim_by_agent_type` -- one `_WindowAccumulator` per
`(task, candidate window)` -- but only for top-level transcripts, and only
once `CompactionSimStats.add_transcript` can read a reported task off the
transcript at all: at least two of its turns carry a metrics-capture
`task=` tag and one task holds at least half of them (mirrors
`classify.reported_task`'s own majority gate; re-implemented locally in
`compaction_sim.py` rather than imported, since `classify.py` imports
`limits.py`, which imports this module, and importing `classify.py` here
would cycle back). A kind of task is a candidate profiles can draft
`autoCompactWindow` from, the same way `goals._compaction` already drafts
it for the whole session from `compaction_sim_by_window`.

Recommendation rule `compaction-window` (category `settings`, lever
`autoCompactWindow`) is more conservative. It reads
`compaction_sim_by_window` and names a floor ("at least W"), not a
single best window:

1. It takes a rediscovery cost off each window's saving, per simulated
   compaction: the rediscovery allowance (this corpus's own median
   post-compaction re-cache write cost, from real compactions'
   `next_turn_write_cost`; $0.00 when there are none) × the corpus's
   mean post-compaction redundant reads per session (the second row of
   `topology_redundant_reads`), or one allowance when the `agents`
   section is missing.
2. It walks the candidate windows from smallest up, skips any with more
   than `max_compactions_per_session` (2) compactions per session, and picks the first whose
   corrected saving is more than (1 − `switch_pct`) of observed cost
   (5% by default) and more than `switch_usd`.

The action says the figure is modelled, not observed, and points to
`compaction_sim_fidelity` when that table has rows. Scope is `"user"`
(`~/.claude/settings.json`), `"project"` (`<project>/.claude/settings.json`
or `<project>/.claude/settings.local.json`, read via
`snapshots.effective_provenance`), or `"managed"` (named, not offered as
user-actionable) depending on which settings layer actually set the
session's effective `autoCompactWindow`. While
`CLAUDE_CODE_AUTO_COMPACT_WINDOW` is set, the title and action name the
variable instead, in the `env` block of the settings file that sets it
now (the user's file when only the shell does, since an `env` entry
replaces the shell's value), and the advice card's change is
`env.CLAUDE_CODE_AUTO_COMPACT_WINDOW`.

Once `compaction_sim_by_window` has priced the main sessions (its
`none` row has a cost), the replay has a verdict on `autoCompactWindow`
whether or not this rule fires, and it is the one answer on that
setting (`advice._consolidate_compaction`): `compaction-churn`
("summaries happen too often", raise the window) is dropped, and
`long-context-share` ("the context is too large") keeps only its
workflow advice (subagents for exploration, a fresh session per task),
never "lower the window". `compaction-window` is also dropped when the
current window is already at or below its floor. Without a replay,
`compaction-churn` keeps the setting and `long-context-share` still
gives it up, so no two cards point the window opposite ways.

## Sign convention (differs from `ttl.py`)

**`delta_usd = candidate_cost - observed_cost` throughout this
module — negative means cheaper.** `ttl.py`'s own tables use the
opposite sign (`cost_observed - best_cost`, positive = a saving); that
convention is unchanged there. This module's sweep walks *many*
candidate windows per key rather than one best-vs-observed pair, so a
uniform "candidate minus observed" avoids re-deriving the sign on every
row. `saving_usd = max(0, -delta_usd)` is always non-negative in both
modules.

## Worked examples

Both are in `tests/test_compaction_sim.py`, priced at the packaged
Sonnet 5 rates, with the turn-by-turn arithmetic in the docstrings.

- **A summary that doesn't pay.** 20 turns growing by 20,000 tokens
  each from a 20,000-token start (`_synthetic_20_turn_transcript`)
  cost **$1.76** as observed. At `window=100,000` four summaries fire;
  each costs $0.37 (the summary request's 20,000 output tokens alone
  are $0.20) against a few cents of reads saved per later turn, so the
  total rises to **$2.448**.
- **A summary that does.** An 80,000-token start, a jump to 280,000,
  then 40 replies that add nothing (`_plateau_transcript`) cost
  **$2.956**. At any window from 100,000 to 250,000 one summary fires
  at turn 2 and every later reply carries 100,000 tokens (start plus
  summary) instead of 280,000: **$1.966**, $0.99 cheaper. The rule
  names "at least 100,000" once `switch_usd` is lowered below that, as
  the rule tests do.

## The "no candidate window" identity

`window=None` never opens the synthetic-compaction guard, so the
per-transcript dropped-token offset never leaves `0` — every turn is priced
via its own unmodified, real values, including any real compaction
already in the transcript. The `window=None` row is therefore *exactly*
the transcript's true observed cost; no separate "observed cost" code
path exists in this module. Asserted directly in
`test_window_none_has_zero_synthetic_compactions_and_matches_true_observed_cost`.

## Assumptions

Printed verbatim in the report section's own notes (`ASSUMPTIONS`):

- A simulated compaction resets context to the session's own starting
  context plus a summary of this corpus's median `postTokens` (20,000
  default); one that would not shrink the context is skipped. (Earlier
  versions scaled the whole context by a compression ratio, dropping the
  starting context every reply carries, which overstated savings at
  small windows several times over.)
- A candidate window fires at the window less this corpus's median
  trigger reserve (0 default).
- A simulated compaction charges the summary request (the median real
  summary request's output, or the summary size when there is none) and
  the reply after it re-caching its whole context, reading the corpus's median cached
  share of the starting context (0 default) and writing the rest.
- Files re-read after a summary are not charged by the sweep; the rule
  corrects for them.
- Every later turn's context and cache reads shrink by the tokens the
  simulated summary dropped (its cache writes too, once the reads are
  used up), until the next compaction, real or simulated. Content added
  after the summary is kept whole.
- A real, observed compaction already in a transcript is kept as-is
  under every candidate window — never re-simulated, never removed.
- `delta_usd = candidate_cost - observed_cost` (see the sign-convention
  section above).

## Wiring into the report and CLI

`report.build_report` builds a per-session `snapshot_windows` map (the
window in the config snapshot each session's own project had when it
started, via `snapshots.snapshot_for`: `CLAUDE_CODE_AUTO_COMPACT_WINDOW`
when it's set, else the effective `autoCompactWindow`), calls
`compaction_sim.simulate_compaction_windows(all_results, rates, snapshot_windows, thresholds)`
and appends `compaction_sim.build_section(...)` after the `carry`
section. `recommend.recommend()` then runs
`compaction_sim.RULES[0](report, CompactionSimThresholds.from_config(config.thresholds), snapshot)`.
The CLI's `compaction-sim` subcommand prints this section plus
`overview`.

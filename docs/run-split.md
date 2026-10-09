# Splitting long subagent runs (`run_split.py`)

Everything a subagent reads stays in its context, and every later reply
reads it again from the cache. So a run's cost grows faster than its
length: reply 200 pays to read everything replies 1 to 199 read. Doing
the same work as several shorter runs, each starting fresh from a short
note of what the last one did, carries less.

`run_split.py` measures what that would have saved, per agent type and
at several split intervals, and the `run-split` recommendation suggests
it where it pays.

## Why a fixed interval isn't enough

Each split has costs of its own: the note, a fresh cache write, and the
new run reading again some files the last one had read. Splitting often
pays those often. On one real corpus, splitting every 50 replies would
have cost $758 more than it saved, while every 150 replies would have
saved $381, all of it from one agent type. So the module tries several
intervals and picks the best one for each agent type.

## What it measures

For every subagent run (workflow agents are left out: their script
decides how work is split), and for each interval in
`run_split_intervals` (default 50, 75, 100, 150, 200 and 300 replies):

- **Where it splits.** Every that many replies, counted from the run's
  start or its last conversation summary. A split counts only when it
  would drop at least `run_split_min_dropped_tokens` (default 20,000).
- **A fresh start.** The run's own first context (its system prompt,
  tools and task) plus the note (`run_split_note_tokens`, default 2,000).
- **What it drops.** The context just before the split, less the fresh
  start. Every reply until the next split, the next summary or the end is
  priced with that taken out of its cache reads (then its cache writes),
  the same shrink the auto-compact replay uses
  (`compaction_sim._shrunk_cost`).
- **What each split adds back:**
  - the run writes the note, as output at its own rates;
  - the parent writes it into the next run's brief, as output at the
    parent's rates, and keeps both the note and the brief in its context
    for the rest of its transcript ([`carry.md`](carry.md)'s rates);
  - the new run's first reply writes its fresh start to the cache instead
    of reading it;
  - an allowance for re-reading files, the one `compaction_sim` measures
    from this corpus's real summaries (as [`plan-handoff.md`](plan-handoff.md)
    uses). It is $0.00 when the sessions in view include no real
    summaries, so one project's view can show a larger saving than the
    all-projects view does.

  When the parent transcript isn't in view, its part is left out and a
  note says for how many runs.

**Current settings only.** The auto-compact window now in force
(`autoCompactWindow`, from the project's newest config snapshot, less any
`CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`) caps how long a run's context can
grow. A run whose peak context, the largest of any of its replies, is
above that window ran under an older, larger setting, and its length
says nothing about what a run costs now. Such runs are left out of every
figure here, the agent's run count and cost included, and the summary
reports how many were left out and what they cost. A card that rested
only on runs from before a window was set goes quiet. A project
with no snapshot takes the newest window any snapshot sets, and with no
window known at all every run counts.

Every split counts, including one near a run's end that costs more than
it saves, since a run can't know in advance how long it will be. An
agent type's **best interval** is the one that saves most among those
with at least `run_split_min_runs` (default 3) runs long enough to
split. The saving is still an upper bound: a thin note can send the next
run back over work the last one had done.

## The report section

`run_split`, always emitted, with three tables:

- `run_split_summary`: one row over every subagent run, counting only
  agent types where splitting pays, plus how many runs were left out as
  older than the current window and what they cost.
- `run_split_by_agent`: one row per agent type, largest saving first (top
  `run_split_top_n`, default 20), with its best interval, or none.
- `run_split_sweep`: one row per interval, with the net saving across
  every agent type (below zero when splitting costs more).

See [`sections-reference.md`](sections-reference.md#run_split-run_splitpy)
for the columns. The dashboard shows them on Agents & context ›
Subagents.

## The recommendation

`run-split` (severity `advice`, category `workflow`, no setting change,
one card per agent type) fires when an agent type's best interval saves
at least `run_split_min_saving_share_pct` (default 5%) of its cost. Its
prompt adds an instruction to your CLAUDE.md: give that agent one part
of a large task per run, and start a fresh one for the next part with a
short note. If the agent has its own file, it also proposes a line
asking it to end each run with that note.

Its saving overlaps with the auto-compact window's (`compaction-window`):
both come from carrying less context in later replies. The Overview's
available saving counts the auto-compact figure only.

All thresholds live in `config.toml`'s `[thresholds]` table under the
`run_split_` prefix; `run_split_intervals` is a list of whole numbers.

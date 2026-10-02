# Model-swap counterfactual (v4-model-swap)

A subagent type's ``.claude/agents/<type>.md`` frontmatter, and the
top-level conversation's ``settings.json``, both carry a ``model``
lever. This feature turns that lever into a number: for each agent
type, and for the top-level conversation, what would today's
already-observed token volumes have cost at every other model this
rate card knows — and, the number that actually leads somewhere, what
is the ceiling saving from moving one tier down (fable -> opus ->
sonnet -> haiku)?

`model_swap.py` does this. It never proposes a jump of more than one
tier, and it never claims the saving is a prediction.

## Which runs the agent file's model reaches

Claude Code picks a subagent's model in this order: a model passed for
that spawn, then the agent file's `model:` line, then
`CLAUDE_CODE_SUBAGENT_MODEL`, then the main session's model. So the
`<type>.md` lever only decides the runs started without a model of
their own. `model_swap.model_set_by(result)` sorts every run:

| Setter | Runs | Where the model is really set |
|---|---|---|
| `settings` | the main session | `settings.json`'s `model` |
| `workflow` | `kind == "workflow-agent"` | the workflow script's `agent()` call, or the workflow's default model |
| `spawn` | a direct run whose `.meta.json` has a `model` | the prompt, skill or command that asked for that model |
| `agent file` | a direct run with no `.meta.json` `model` | `.claude/agents/<type>.md`'s `model:` line |
| `none` | `fork`, `unknown`, `workflow-subagent` | Claude Code or the workflow script; there is no agent file |

The `.meta.json` `model` (`TranscriptMeta.agent_model_alias`) records
the model the spawn *asked for*, not the one it resolved to. On a real
corpus it matched the parent's `Agent` tool call `input.model` on every
direct run, was absent exactly when the call passed none, and the
run's turns always ran on it, even when the agent file named a
different model (a file saying `opus`, a spawn asking for `sonnet`,
turns on Sonnet). A workflow run's model comes from its script or the
run's `defaultModel`, so every workflow-agent run counts as `workflow`
whether or not its `.meta.json` names a model.

Assumption: a workflow-agent run of a named type with no model of its
own follows the workflow's default, not the agent file. The corpus
this was checked on had only a handful of such runs, and there the
default and the agent file named the same model, so the data can't
tell the two apart.

Each row's totals (spawns, observed cost, every `Cost at` column)
still cover every run. The saving, verdict and `model-tier` advice for
a subagent use only its `agent file` runs, so no saving is priced on
runs the lever can't reach. Other agent-file fields (`omitClaudeMd`,
`tools`) still apply to workflow and spawn-model runs, so spawn-cost
advice is unchanged.

## What this is, precisely

For every priced turn already in the corpus, `compute_model_swap`
reprices it at every model `pricing.toml` carries, using the exact same
`pricing.price_turn` the rest of the engine uses to price the turn's
observed model — same input/output/cache-read tokens, and the same
observed 5-minute/1-hour cache-write split (`write_split=None`, so
`price_turn` falls back to the turn's own `cc_5m`/`cc_1h`). Nothing
about the turn's shape changes; only the per-token rate does.

That is a **price ceiling at today's usage shape**, not a forecast. A
smaller model may need more turns to reach the same result, or fail
the task outright — this module has no way to represent either
possibility, and says so in every note and every recommendation's
action text.

## Tier order

"One tier down" reuses `workstyle.model_tier`'s existing ranking
(fable=3, opus=2, sonnet=1, haiku=0) rather than inventing a
cost-derived ordering here. The next cheaper family's *current*
representative model is resolved through `Pricing.aliases[family]` —
the public alias table `pricing.py` already exposes — never a
hardcoded model id. If a future rate card drops a family's bare alias
entirely, the affected row reports "unknown tier" instead of guessing
at a stale id.

Today's packaged rate card points `fable` and `best` at
`claude-fable-5-1`, `opus` at `claude-opus-5-5`, `sonnet` and
`sonnet[1m]` at `claude-sonnet-5-5`, and `haiku` at
`claude-haiku-4-5-20251001`. The `sonnet` aliases moved from
`claude-sonnet-5` to `claude-sonnet-5-5` with the card dated 2026-10-01;
`claude-sonnet-5` keeps its own row at the same rates, so sessions that
ran on it are still priced as themselves. A rate card of your own sets
its own aliases.

## The `model_swap` report section

| Table | What it shows |
|---|---|
| `model_swap_by_agent_type` | One row per agent type (`"top-level"` for the main conversation, else `TranscriptMeta.agent_type` or `"unknown"`): spawns, priced turns, unpriced turns (unknown model), observed model, observed cost, one `Cost at <model-id>` column per model in `pricing.toml`, the best cheaper alternative (model id and human label), the ceiling saving in USD and %, and the lever text (`settings.json` for top-level, `<type>.md`'s frontmatter for a subagent, `none (the workflow script sets it)` for `workflow-subagent`, `none (Claude Code picks)` for forks and untyped runs). Then, added at the end: `lever_runs`, `lever_priced_turns`, `lever_model` and `lever_cost` (the runs the lever decides; for top-level, every run), `workflow_runs` and `spawn_model_runs` (the runs set elsewhere). The saving is priced on the lever runs only. |
| `model_swap_summary` | One row: the corpus-wide ceiling if every subagent type whose agent-file runs are on Fable or Opus moved one tier down. Its observed cost and saving cover those runs only. Excludes the top-level row and any Fable/Opus agent type that's already at or below its next tier's cost at today's volumes. |
| `model_swap_agent_file_runs` | Report tier. One row per named subagent type with at least one `agent file` run: runs, priced turns, observed model and cost, and a `Cost at <model-id>` column per model, for those runs only. The what-if engine prices an agent's model change from this table. |

Every row's "best cheaper alternative" state is one of:

- **cheaper_available** — a real one-tier-down saving exists; the
  model id, USD and % figures are populated.
- **already_cheapest** — the observed model is already Haiku, or its
  own volumes are already cheaper than the next tier down at today's
  rates. The best-cheaper-alternative column is empty and the saving
  is `0.0` in both dollars and percent, never a stale positive number.
- **main_floor** — the main session (`top-level`) runs on Sonnet.
  Haiku is never suggested for the main session: it does the hard,
  open-ended work, and the saving isn't worth the quality trade. The
  alternative is empty and the saving `0.0`, so no card, Savings lever,
  goal or quick action offers it; the `Cost at <model-id>` columns still
  show what Haiku would have cost.
- **unknown_tier** — the observed model's family isn't recognised, or
  the rate card has no alias for the next family down.
- **no_data** — no priced turns for this agent type.
- **set_elsewhere** — a named subagent type none of whose runs followed
  its agent file's model: a workflow script or the spawn set every one.
  The label says how many of each. No `.md` advice.
- **no_lever** — `workflow-subagent`, `fork` or `unknown`: no agent file
  sets the model at all.

Only `cheaper_available` ever carries a non-zero saving, so the table
never implies a saving where none exists.

## The `model-tier` recommendation

`RULES["model-tier"]` reads the *rendered* `model_swap_by_agent_type`
table (never the raw stats — same "rules only read the finished
report" convention `recommend.py`'s own rules follow) and fires one
`Recommendation` per qualifying row: a real cheaper alternative exists,
the row's sample clears `ModelSwapThresholds.min_sessions`/
`min_turns`, and the ceiling saving clears both
`saving_pct_min` (default 10%) and `saving_usd_min` (default $1.00).

The action text names the exact file and line to change
(`settings.json`'s `"model"` key for the top-level conversation, or
`.claude/agents/<type>.md`'s `model:` frontmatter line for a subagent),
cites the saving as a ceiling, and repeats the "held constant / may
need more turns or fail outright / verify quality before committing"
caveat every time. When some of the type's runs were set elsewhere, the
action says the saving covers only the N runs started without a model,
and names how many a workflow script started (set the model in the
script's `agent()` call) and how many were given a model when they
started (set by the prompt, skill or command that asks for it). The
evidence lists both counts as "not counted". Per-agent-type advice is suppressed under an
archetype that never spawns subagents of its own (`chat-only`) — the
top-level row's own model is a real lever regardless of archetype, so
it is never suppressed.

`advice.finish` then rewords the cards for the dashboard. Subagent
cards merge into one `model-tier` card with a change per agent type,
largest saving first. The main session's card becomes `model-tier-main`
on its own: its model is a quality trade that's yours to make, so it
sorts after every other card of the same severity (below the compaction
tips, say), however large its saving. Each subagent change's note
carries the same set-elsewhere sentence, so the dashboard card points
those runs to where their model is really set.

The other readers follow the lever-only figures: the what-if engine
(`whatif._model`) prices a subagent's model from
`model_swap_agent_file_runs`, and says so when no run followed the
agent file; the models goal (`profiles/goals.py`) and
`habits_agents_by_task`'s cheaper-model column read the lever-based
verdict; the quick-action models check adds a "Model set by" column
and one tip when some runs are set elsewhere.

## API and wiring

```python
from claudeglass import model_swap

stats = model_swap.compute_model_swap(results, pricing)          # pure; hand in every transcript at once
section = model_swap.build_section(stats)                        # -> Section(key="model_swap", ...)
recs = model_swap.RULES["model-tier"](report, thresholds, archetype, snapshot)
```

- `model_swap.compute_model_swap(results, pricing, thresholds=None) -> ModelSwapStats`
- `model_swap.build_section(stats, thresholds=None) -> Section`
- `model_swap.RULES["model-tier"](report, thresholds, archetype=None, snapshot=None) -> list[Recommendation]`
- `model_swap.ModelSwapThresholds` — `saving_pct_min` (10.0), `saving_usd_min` (1.00), `min_sessions` (5), `min_turns` (200); `.from_config(dict)` and `.describe()` follow the same convention as `RecacheThresholds`/`TtlThresholds`.
- `model_swap.model_set_by(result) -> str` — which setter decided a run's model (table above).
- `model_swap.set_elsewhere_sentence(workflow_runs, spawn_model_runs) -> str` — the shared sentence naming runs the agent file doesn't decide, or `""`.
- `model_swap.ASSUMPTIONS` — the five caveats this module states about every number it produces; fold into any parent "assumptions" listing.

`report.build_report` appends `model_swap.build_section(...)` after the
`compaction_sim` section and adds `model_swap.ASSUMPTIONS` to the
report's assumptions. `recommend.recommend()` runs
`RULES["model-tier"]` with
`ModelSwapThresholds.from_config(config.thresholds)`, the report's
archetype and the latest snapshot. The CLI's `model-swap` subcommand
prints this section plus `overview`.

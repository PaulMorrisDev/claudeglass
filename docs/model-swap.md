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

A second part, `agent_models.py`, asks a narrower question of agents
whose model nobody chose: did one write code to a settled spec on a
model above Sonnet because the call that started it named no model?
See [Agents that ran on a larger model than their work
needed](#agents-that-ran-on-a-larger-model-than-their-work-needed).

## Which runs the agent file's model reaches

Claude Code picks a subagent's model in this order:

1. The model passed for that spawn.
2. The agent type's file, by its `model:` line. A file that says
   `inherit` pins the main session's model, so the variable below
   never reaches it.
3. `CLAUDE_CODE_SUBAGENT_MODEL`. It never reaches Explore, Plan or a
   fork.
4. The main session's model.

So the `<type>.md` lever only decides the runs started without a model
of their own. `model_swap.model_set_by(result)` sorts every run:

| Setter | Runs | Where the model is really set |
|---|---|---|
| `settings` | the main session | `settings.json`'s `model` |
| `workflow` | `kind == "workflow-agent"` | the `agent()` call's model, else the `agentType`'s agent file, else `CLAUDE_CODE_SUBAGENT_MODEL`, else the main session's model |
| `spawn` | a direct run whose `.meta.json` has a `model` | the prompt, skill or command that asked for that model |
| `agent file` | a direct run with no `.meta.json` `model` | `.claude/agents/<type>.md`'s `model:` line |
| `none` | `fork`, `unknown`, `workflow-subagent` | Claude Code or the workflow script; there is no agent file |

The `.meta.json` `model` (`TranscriptMeta.agent_model_alias`) records
the model the spawn *asked for*, not the one it resolved to. On a real
corpus it matched the parent's `Agent` tool call `input.model` on every
direct run, was absent exactly when the call passed none, and the
run's turns always ran on it, even when the agent file named a
different model (a file saying `opus`, a spawn asking for `sonnet`,
turns on Sonnet). A workflow has no default model of its own. The run
file's `defaultModel` only records the main session's model when the
workflow launched, and a model in `meta.phases` only labels the phase.

Simplification: `model_set_by` counts every workflow-agent run as
`workflow`, whether or not its `.meta.json` names a model. A run of a
named type with no model of its own takes its agent file's `model:`
line when the file has one, as an `agent file` run does, so by the
order above it belongs with those runs, and it isn't counted there.
This was left alone because such runs are rare, and the agent-model
cards below cover the workflow runs whose model nobody chose.

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
| `model_swap_agent_models` | Not built by `build_section`: `report.build_report` appends it. One row per group of agents and finding, for agents that ran on a larger model than their work needed. See [the section below](#agents-that-ran-on-a-larger-model-than-their-work-needed). |

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

## Agents that ran on a larger model than their work needed

The tables above price a swap for a whole agent type. `agent_models.py`
asks a narrower question of each subagent and workflow agent: did it
write code to a settled spec on a model above Sonnet because nobody
chose a model for it? The main session is never judged. Whether the
call set a model is read from the run's `.meta.json`, never inferred
from the model the run happened to use.

### What it reads for each run

- `TranscriptMeta.model_recorded`: the `.meta.json` is the newer shape
  (it has a `description` or `workflowPhase` key), so an absent
  `model` means the call set none.
- `TranscriptMeta.agent_model_alias`: the model the call asked for,
  if it asked for one.
- `TranscriptMeta.role_word`, and its class from
  `agent_roles.role_class`. See "Role words" below.
- Its write turns: replies in which it wrote code. See "What makes a
  writer" below.
- Its dominant model: the model on most of its priced replies (a tie
  goes to the model id that sorts last), and that model's tier from
  `workstyle.model_tier` (Fable 3, Opus 2, Sonnet 1, Haiku 0, or -1
  for a model it doesn't recognise). Above Sonnet means Opus or
  Fable.

It skips these runs, and gives them no row:

- The main session.
- A direct run with no agent type, and any run whose type is `fork`
  or `unknown`. Nothing can change a fork's model. A workflow agent
  with no agent type is kept, under `workflow-subagent`.
- A run whose `.meta.json` is the older shape (`model_recorded` is
  false). Whether it set a model can't be read, and guessing would
  mean reading the workflow script.
- A run with no priced replies.

### Role words

`agent_roles.py` holds a closed list of role words, and for each the
forms that stand for it (`implementer`, `fixing`, `reviewer`). A form
matches as a whole word, so `fixture` is not `fix` and `mapping` is
not `map`. The first source with a known word wins: the workflow
phase, then a named agent type (not `general-purpose` or another type
that names no role), then the first four words of the description.
Only the one canonical word is kept (`role_word`), never the phase,
label or description it came from.

| Class | Words |
|---|---|
| `writer` | implement, fix, apply, test, build, write, migrate, refactor |
| `integrates` | integrate |
| `decider` | review, verify, refute, judge, decide, audit, research, design, challenge, critique, synthesise, plan, find, map, assess, check, completeness, explore, investigate, inventory, baseline, adversarial, analyse |

A run with no known word has no class and counts under the role
`other`. A class is decided when the report is built, so moving a word
between classes needs no re-read of the transcripts. Adding a word or
a form does, because the word is stored when a transcript is parsed.

### What makes a writer

A write turn is a reply in which the agent wrote to a file outside the
temp folder: an Edit, Write, MultiEdit or NotebookEdit call that
targeted such a file (`Turn.edit_kind == "real"`), or a shell command
that wrote to such a target (`Turn.shell_write_count` above 0). A
write into the temp folder, Claude's scratchpad included, doesn't
count, whichever way its path is written.

A run is a writer when either:

- its class is `writer` and it has at least one write turn; or
- it has no class and at least `unknown_min_edit_turns` (default 2)
  write turns.

An `integrates` or `decider` word is never a writer, however much the
agent edited. A `writer` word with no write turn isn't one either: an
agent told to test that only ran the tests wrote nothing.

### Who chose the model

| Chosen by | When |
|---|---|
| `call` | the `.meta.json` names a model: the call asked for it |
| `file` | the call named none, the run has a named agent type (not `workflow-subagent`), and that type's agent file names a model that explains the run: `inherit`, or a model of the family the run ran on |
| `inherited` | neither: nothing chose a model, so the run took the one it was handed |

A file that names another family doesn't excuse the run. A file that
says `sonnet`, for a run that ran on Opus, didn't choose that model.
The agent files come from the latest snapshot, with every project's
agents merged; with no snapshot, no run is `file`.

A writer chosen by `file` gets no verdict: its model was chosen on
purpose. For a direct run, `model-tier` covers that file's `model:`
line. A workflow run whose agent file chose its model is in neither
place (see the simplification above).

### The three verdicts

A run gets a verdict only when its dominant model is above Sonnet:

- `inherited`: a writer that nothing chose a model for.
- `asked`: a writer whose call named the model.
- `decide-apply`: a `decider` with at least
  `decide_apply_min_edit_turns` (default 3) write turns, whoever chose
  its model. It decided and also applied the changes.

A writer chosen by `file` gets none. Neither does an `integrates`
agent, a decider with fewer write turns, or any agent on Sonnet,
Haiku or a model it doesn't recognise.

Each flagged run is filed under a group and its verdict. The group is
`workflow-subagent` for every workflow agent, whatever agent type its
script named, because the lever is the script's `agent()` call. For
an Agent-tool subagent it is the agent type, such as
`general-purpose`. So one workflow card covers every workflow agent,
and each Agent-tool type has its own.

### The `model_swap_agent_models` table

`report.build_report` appends it to the `model_swap` section, and the
dashboard shows it on Spend › Savings. It has one row per group and
verdict, in verdict order (`inherited`, `asked`, `decide-apply`), then
the biggest saving, then cost. With no flagged agent it has no rows.
It carries no run id, label or path. Its notes give the thresholds,
and say so when a flagged reply used a model your pricing file has no
price for.

| Column | Holds |
|---|---|
| `case` | the row key, `<group>:<verdict>` (`workflow-subagent:inherited`), since one type can have a row for more than one verdict. The dashboard shows it as a label such as "Workflow agents, no model set", "`<type>` agents, asked for a larger model" or "`<type>` agents, decided and changed code" |
| `agent_type` | the group: `workflow-subagent` for every workflow agent, else the agent type |
| `verdict` | `inherited`, `asked` or `decide-apply` |
| `runs` | agents counted, one per run |
| `roles` | each role word and its agents, most first, ties in alphabetical order: `implement 4, fix 1`. `other` counts agents with no word |
| `model` | the model most of these agents ran on |
| `cost_usd` | their replies priced at the models they ran on |
| `cost_on_sonnet_usd` | the same replies priced at Sonnet. Empty for `decide-apply` |
| `saving_usd` | cost minus cost on Sonnet, never below 0. Empty for `decide-apply` |
| `saving_pct` | that saving as a share of cost. Empty for `decide-apply` |
| `write_turns` | write turns across these agents |
| `workflow_runs` | how many separate workflow runs they came from; 0 for Agent-tool agents |
| `first_seen` | the date (`YYYY-MM-DD`) of the earliest agent's first reply |
| `last_seen` | the date of the latest agent's first reply |
| `later_compliant` | for `inherited` rows, the later writers on Sonnet or smaller (see "When a card looks fixed"); 0 on the other rows |
| `env_var_set` | `yes` or `no`: whether `CLAUDE_CODE_SUBAGENT_MODEL` is among the latest snapshot's environment variable names. The same on every row |

### The three rules

Each rule reads this rendered table only, as `model-tier` does, and
fires once per row of its verdict. Every card has category `workflow`,
lever `model` and scope `user`, and carries no setting change.
`agent_type` is the row's group, and `subject` is the row's
`last_seen` date. An ignored card therefore stays ignored while more
agents turn up on the same day, and comes back when one runs on a
later day. The rules fire from the first flagged agent: each saving
is small, so there is no dollar floor. They are quiet for the
`chat-only` archetype, and `recommend()`'s own minimum sample still
applies.

| Rule | Severity | Variant | Reads rows |
|---|---|---|---|
| `agent-model-inherited` | `advice`, or `info` once `later_compliant` reaches `later_compliant_for_info` | `fixed` when `info` | `inherited` |
| `agent-model-asked` | `info` | none | `asked` |
| `agent-decide-apply` | `info` | none | `decide-apply` |

The thresholds are `AgentModelThresholds` fields, read from
`config.toml`'s `[thresholds.agent_models]`:

| Field | Default | Meaning |
|---|---|---|
| `later_compliant_for_info` | 3 | an `inherited` card drops from advice to info once this many later writers of the same kind ran on Sonnet or smaller |
| `unknown_min_edit_turns` | 2 | an agent with no role word counts as a writer at this many write turns |
| `decide_apply_min_edit_turns` | 3 | a deciding agent counts as also changing code at this many write turns |

For the `inherited` card of a named Agent-tool type, `advice.finish`
also holds the card at `info` when your quality data says that type
may not do well on Sonnet (`model_gate`'s veto), and the card says so.
A workflow agent has no such veto.

`advice.finish` writes each card's title and words from the row's
evidence, and counts agents, not runs. The `inherited` title follows
`<count> workflow agents wrote code on <model> with no model set`
(or `<count> <agent type> agents` for an Agent-tool type): for
instance "5 workflow agents wrote code on Opus 5.5 with no model
set". The other two read `<count> workflow agents that wrote code
were started on <model>` and `<count> workflow agents decided and
changed code on <model>`, with the agent type in place of "workflow"
for an Agent-tool type.

### How the saving is priced

For each `inherited` or `asked` agent, every priced reply is priced
again at `pricing.models[pricing.aliases["sonnet"]]` with the same
`pricing.price_turn` the rest of the engine uses: the same tokens,
and the same cache-write split. So the saving is a ceiling at today's
usage, never a forecast, and `agent_models.ASSUMPTIONS` says so. A
reply whose model has no price counts as 0 in both costs. A rate card
with no Sonnet model leaves every saving empty.

A `decide-apply` row is never priced again. Its fix is to split the
work between two agents, not to swap a model, so the row shows cost
and write turns only.

None of this is summed into `model-tier` or the Savings levers total,
so no run is counted twice. The `asked` figure is for information
only. The Overview's available saving adds an `inherited` card's
saving only while the card is advice, since an info card looks fixed
already or is held back for quality. It also adds it only where
`model_swap_by_agent_type` has no saving for that agent type, so a
type the model lever already prices isn't counted twice. An `asked` or
`decide-apply` card adds nothing.

### When a card looks fixed

An `inherited` row's `later_compliant` counts the writers of the same
kind that ran on Sonnet or smaller and started after the row's latest
flagged agent. The kind is workflow agents or Agent-tool agents, of
any type, and a writer counts whoever chose its model, so long as its
model has a known tier. Agents are ordered by the time of their first
reply. At `later_compliant_for_info` (default 3) or more, the card
drops to `info` with variant `fixed`. Its explanation adds how many
agents that wrote code ran on Sonnet or a smaller model since, and
that this looks fixed. Its action becomes copying the rule to
`~/.claude/CLAUDE.md` so every project follows it, since the rule may
live in only one project's notes. It keeps its saving. A new flagged
agent moves the start of the count, so the card returns to advice
until enough writers follow it.

### `CLAUDE_CODE_SUBAGENT_MODEL`

`agent_models` reads only the variable's name, from the latest
snapshot's environment variable names. When it is there, `env_var_set`
is `yes` on every row, and an `inherited` card says the agent ran on
"the model CLAUDE_CODE_SUBAGENT_MODEL names" instead of "your main
session's model". The variable changes no verdict: a run that took its
model from it still counts as `inherited`, because no call or file
chose it. `CLAUDE_CODE_SUBAGENT_MODEL_FORCE` is not handled. A
snapshot that names only that variable leaves `env_var_set` at `no`.

### What the cards offer

No card changes a setting. The lever is the call that starts the
agent, so there is no key or agent-file line for ClaudeGlass to patch,
and no Apply button. Each card offers a prompt to copy, and an
explainer that says where it applies, what you give up and how to
undo it.

- `agent-model-inherited` leads with a "from now on" prompt: set the
  model on every subagent and workflow agent you start, never leave it
  to inherit even where a tool's instructions say to omit it, use
  Sonnet for agents that write code to a settled spec and Opus for
  agents that decide, and never give one Opus agent both the deciding
  and the applying. It ends by asking where the rule should apply:
  this session, this project or all your projects.

  Its explainer holds the rest. In a workflow script the model goes on
  each call, as in
  `agent(brief, { phase: 'Implement', model: 'sonnet' })`. A model in
  `meta.phases` only labels the phase. For a named agent type, a
  `model: sonnet` line in its agent file also works, and a file named
  like a built-in agent replaces it whole. Its trade-off says Sonnet
  may need more replies on hard code, and that
  `CLAUDE_CODE_SUBAGENT_MODEL=sonnet` is blunter: it also moves
  reviewers and judges, any model a call or an agent file sets still
  wins, and it never reaches Explore, Plan or forks. The `fixed`
  variant keeps the same prompt and explainer.
- `agent-model-asked` asks Claude to find where agents that write code
  are started with `opus` or `fable` named (skills, agent files,
  workflow scripts and CLAUDE.md files), say whether each place needs
  it, and propose `sonnet` where it doesn't, showing each change
  before saving.
- `agent-decide-apply` is a "from now on" prompt: when an agent's job
  is to review, audit, verify or judge, have it report the exact
  changes instead of making them, then start a separate agent on
  Sonnet to apply them, and keep the deciding agent on Opus.

None of the three suggests `CLAUDE_CODE_SUBAGENT_MODEL_FORCE`. The
`models` check on Actions › Checks claims all three ids: an advice
card makes it act, and every card's prompt is among its fixes. An
info card whose fix has no prompt is a tip instead.

## API and wiring

```python
from claudeglass import agent_models, model_swap

stats = model_swap.compute_model_swap(results, pricing)          # pure; hand in every transcript at once
section = model_swap.build_section(stats)                        # -> Section(key="model_swap", ...)
recs = model_swap.RULES["model-tier"](report, thresholds, archetype, snapshot)

agents = agent_models.compute_agent_models(results, pricing, agent_files=files, env_names=names)
table = agent_models.build_table(agents)                         # -> Table(name="model_swap_agent_models", ...)
recs = agent_models.RULES["agent-model-inherited"](report, thresholds, archetype, snapshot)
```

- `model_swap.compute_model_swap(results, pricing, thresholds=None) -> ModelSwapStats`
- `model_swap.build_section(stats, thresholds=None) -> Section`
- `model_swap.RULES["model-tier"](report, thresholds, archetype=None, snapshot=None) -> list[Recommendation]`
- `model_swap.ModelSwapThresholds` — `saving_pct_min` (10.0), `saving_usd_min` (1.00), `min_sessions` (5), `min_turns` (200); `.from_config(dict)` and `.describe()` follow the same convention as `RecacheThresholds`/`TtlThresholds`.
- `model_swap.model_set_by(result) -> str` — which setter decided a run's model (table above).
- `model_swap.set_elsewhere_sentence(workflow_runs, spawn_model_runs) -> str` — the shared sentence naming runs the agent file doesn't decide, or `""`.
- `model_swap.ASSUMPTIONS` — the five caveats this module states about every number it produces; fold into any parent "assumptions" listing.
- `agent_models.compute_agent_models(results, pricing, *, agent_files=None, env_names=None, thresholds=None) -> AgentModelStats` — pure, like `compute_model_swap`. `agent_files` maps an agent type to the `model` its agent file names (`None` when it names none), and `env_names` is the snapshot's environment variable names. Without them no run counts as chosen by its file, and `env_var_set` is false.
- `agent_models.build_table(stats) -> Table` — `model_swap_agent_models`.
- `agent_models.RULES` — the three rules by id (`agent-model-inherited`, `agent-model-asked`, `agent-decide-apply`), each `(report, thresholds, archetype=None, snapshot=None) -> list[Recommendation]`. The snapshot is accepted for the shared signature; nothing reads it.
- `agent_models.AgentModelThresholds` — `later_compliant_for_info` (3), `unknown_min_edit_turns` (2), `decide_apply_min_edit_turns` (3). `.from_config(dict)` takes either a flat dict or a `[thresholds]` dict with an `agent_models` table, keeps the default for any key that's absent and ignores unknown keys; `.describe()` gives the table's thresholds note.
- `agent_models.ASSUMPTIONS` — two caveats: the saving is a ceiling, and a role is read from a word of the phase, type or description with only the word kept.
- `agent_models.VERDICTS`, `WORKFLOW_GROUP` (`workflow-subagent`), `ENV_VAR` (`CLAUDE_CODE_SUBAGENT_MODEL`) and `TABLE_NAME`.

`report.build_report` appends `model_swap.build_section(...)` after the
`compaction_sim` section and adds `model_swap.ASSUMPTIONS` to the
report's assumptions. `recommend.recommend()` runs
`RULES["model-tier"]` with
`ModelSwapThresholds.from_config(config.thresholds)`, the report's
archetype and the latest snapshot. The CLI's `model-swap` subcommand
prints this section plus `overview`.

`report.build_report` also reads the latest snapshot, with every
project's agents merged, and hands its agent files' `model` lines and
its environment variable names to `agent_models.compute_agent_models`,
with `AgentModelThresholds.from_config(config.thresholds)`. It appends
`agent_models.build_table(...)` to the `model_swap` section's tables
and adds `agent_models.ASSUMPTIONS` after `model_swap.ASSUMPTIONS`.
`recommend.recommend()` runs each rule in `agent_models.RULES` right
after `model-tier`, with the same thresholds, the report's archetype
and the latest snapshot. `advice.finish` writes the cards' words, and
`fixes` holds their prompts and explainers.

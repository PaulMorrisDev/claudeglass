"""Recommendations (WP10b): turn an assembled :class:`~claudeglass.model.ReportModel`
into a list of actionable :class:`~claudeglass.model.Recommendation`\\ s,
per the project plan's "Recommendations" section and Appendix A5's
threshold table.

This module deliberately works only from the *rendered* report (the
``Section``/``Table`` objects already sitting in ``ReportModel.sections``),
never from a raw accumulator or transcript — the same "cross-module
metrics are handed in as plain data" convention ``scorecard.py`` already
follows (see that module's docstring). The payoff is the evidence
contract every recommendation must honour: each entry in
``Recommendation.evidence`` is a ``(label, value, source_table, row_key)``
tuple where ``source_table`` is ``"<section_key>.<table_name>"`` and
``value`` is the exact cell a report reader would see in that table's
``row_key`` row -- so a test can walk every recommendation this module
produces and confirm the number it cites is real, not recomputed. See
``_cell`` for the lookup this relies on.

Deviations from the plan/brief, reported rather than made silently (see
``model.py``'s module docstring for this project's convention):

- WP10-merge update: ``model.py``'s ``Recommendation`` now carries a real
  ``scope: str = "user"`` field (``"user"``/``"repo"``/``"managed"`` --
  plan "Enterprise use" section), proposed by both WP10b and WP10c
  independently and added when their branches merged. This module sets
  it directly (see ``_lever_scope``) instead of the ``"[managed] "``
  string prefix on ``Recommendation.lever`` an earlier revision used as
  a workaround while ``model.py`` was outside this work package's file
  list: ``lever`` is now always the bare settings key / frontmatter
  path, and ``scope`` carries what used to be prefix-encoded. ``scope``
  is ``"repo"`` for a per-agent frontmatter lever (``.claude/agents/
  <type>.md``, which lives in the repo), else ``"user"`` -- upgraded to
  ``"managed"`` when the lever's underlying settings key appears in
  ``snapshots.managed_keys(snapshot)``, in which case ``action`` also
  states "managed by policy, raise with your administrator".
- **COV-01 update: ``scope`` gained a fourth value, ``"project-local"``**
  (``.claude/settings.local.json``, per-machine, never checked in --
  ``docs/config-layers.md``'s layer model), matching the vocabulary
  ``fixes.PROFILE_SCOPE_WHERE``/``apply.py``'s ``--scope`` flag already
  used. For a plain settings-key lever, ``_lever_scope`` now names the
  layer that actually supplies its effective value today
  (``snapshots.effective_provenance``), not always ``"user"`` -- a
  recommendation must never tell you to edit a file a higher layer
  already overrides (finding D5). A per-agent frontmatter lever is
  unaffected: ``docs/config-layers.md``'s ``effective_agents`` section
  confirms agent files have no project-local/project-shared split
  (``source`` is only ever ``"user"``/``"project"``), so that branch
  keeps its plain ``"repo"``.
- A5's ``ttl-switch`` clause "suppressed for subagents in subscription
  mode" is deliberately *not* implemented. Claude Code's prompt-caching
  docs say a subscription only draws on usage credits once it goes over
  the plan's limit; within plan usage a subagent's 1h lifetime works,
  and even on usage credits only an agent file's ``cacheTtl: 1h`` is
  ignored (``subagentPromptCacheTtl`` still applies). ``ttl.build_section``
  explains why this tool can't tell which replies ran on usage credits,
  and states the caveat in a note in subscription mode; ``fixes.py``
  repeats it on the agent-file change itself. Precedence does change the
  lever, though: a ``subagentPromptCacheTtl`` already set in any
  settings layer outranks every agent file's ``cacheTtl``, so
  ``_rule_ttl_switch`` names that setting instead of the agent file
  (``_ttl_row_lever``). The plan's further "Enterprise use" clause
  ("TTL rules are suppressed where the
  [cloud] provider cannot honour 1h") has no supporting capability data
  anywhere in this codebase (``pricing.py`` has no ``[providers.*]``
  table, no ``supports_1h_cache`` flag -- confirmed by reading the whole
  module); lacking that data, this module suppresses every ``ttl-switch``
  recommendation outright whenever ``config.provider`` is set to anything
  other than ``"anthropic"``/``None``, as the conservative reading of
  "cannot confirm 1h works there yet".
- A5's ``data-quality`` rule ("unparsable > 0.1% of lines, ttl mismatches
  > 0, fidelity > 10%") only has one of its three inputs backed by a
  report table: ``ttl_by_agent_type``'s ``fidelity_pct`` column.
  ``report.py``'s own module docstring explains why parse-quality counters
  (``ReportModel.diagnostics``) are deliberately *not* rendered as a
  ``Table`` (the existing renderer contract already covers them from that
  dedicated field). This module still reads ``report.diagnostics``
  directly to decide *whether* the unparsable-lines/ttl-mismatch clauses
  additionally fire, but -- since ``Recommendation.evidence`` can only
  cite real table cells -- always anchors the evidence list on the
  ``scorecard.dimensions`` row for ``"data_quality"`` (a table cell that
  genuinely exists) and states the diagnostics-derived counts in prose in
  ``action`` instead of fabricating a table citation for them.
- A5's ``effort-mismatch`` rule ("thinking share high on docs/general-dev
  sessions") wants the high-effort thinking share computed *for those
  sessions specifically*, but no report table joins
  ``topology_effort_tokens``'s per-effort-level thinking share to
  ``sessions_by_purpose``'s per-purpose session counts -- they're
  independent group-bys (one by effort level, one by purpose) with no
  shared key exposed anywhere in this codebase. This rule instead
  compares the corpus-wide high-effort thinking share against the mere
  presence of docs/general-dev sessions in the corpus, which can over-
  or under-state the mismatch for a corpus whose docs/general-dev
  sessions don't run at high effort (or vice versa). Both the action
  text and the evidence list disclose this: the evidence separately
  cites the (unjoined) thinking-share and session-count cells rather
  than implying a single joined metric.
- A5's ``long-context-share`` rule's second clause ("median top-level ctx
  > 150k") has no table exposing a *median*; ``scorecard.dimensions``'s
  ``context_hygiene`` row exposes ``p90_top_level_ctx`` instead (the one
  representative metric ``scorecard.py`` chose for that dimension, per
  its own docstring). This module uses that p90 figure as the evidence
  for this clause, which is a stricter (harder-to-clear) proxy for the
  same "context bloat" concern than the median the plan names.
- A5's ``batch-instructions``/``long-tool-waits`` rules each combine a
  report-table condition with a second condition that has no dedicated
  table cell (mean queue-operations per session; the joint "gap > 300s
  *and* preceded by Bash/PowerShell" condition split across two
  independent tables). Each is implemented as documented below, next to
  its rule function, using the closest available signal; the cited
  evidence is always a real table cell even where the full joint
  condition described in the plan can only be approximated.
  ``baseline-bloat`` once had a second condition too (MCP-server and
  plugin counts from a config snapshot); it now fires on the measured
  ``controllable_est`` cell alone.
- ``long-tool-waits``, specifically: no table anywhere in this codebase
  exposes the *joint* count of turns that are both preceded by
  Bash/PowerShell (``recache_preceding_tool``) and follow a gap > 300s
  (``recache_gap_buckets``) -- those are two independent turn
  populations, not a single joint one. Rather than approximate the
  joint share with ``min(tool_share, long_gap_share)`` (an upper bound
  on the true joint share, not the share itself -- see this rule's own
  comment), this rule requires each share to independently clear the
  threshold and cites both as separate evidence entries.

v0.2.0 fix A1: ``spawn-cost``'s ``omitClaudeMd`` lever only means anything
for an agent type that actually has a ``.claude/agents/<type>.md``
frontmatter file -- a built-in Claude Code agent type (``general-purpose``,
``Explore``, ``Plan``, ...) has none, so the earlier version of this rule
was naming a lever with nothing on disk for ``apply``/``render_patch_set``
to patch. ``_agent_has_frontmatter`` now answers this from the latest
config snapshot's ``agents`` map when one is available, else from a
built-in list of Claude Code's own bundled agent types
(``_BUILTIN_AGENT_TYPES``). A built-in agent type still gets a
recommendation, just as ``category="workflow"`` advice (a same-named agent
file with a tools list, since a shorter task prompt does not change the
system prompt and tool definitions it starts with) with ``lever=None`` --
which ``render_patch_set`` already skips, so no patch-set stanza is emitted
for it either.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass

from . import (
    agent_models,
    carry,
    compaction_sim,
    cost_centres,
    elasticity,
    handoff,
    hook_costs,
    model_swap,
    pricing,
    run_split,
    tool_search,
    waste,
    whatif,
)
from .config import Config
from .context_budget import DIET_EXCLUDED, MIN_WINDOW_DAYS, REMOVABLE_USE_SHARE, SKILLS_TOOL, diet_usd
from .fixes import BATCH_PROBES_LINE, CRITIQUE_PLAN_LINE
from .model import Recommendation, ReportModel, Section, SettingChange, Table
from .snapshots import Snapshot, effective_config, effective_provenance, managed_keys
from .units import NO_LIMIT_SHARE_HINT, Units

#: Purposes ``classify.classify_purpose`` can return that count as
#: "docs/general-dev" for the ``effort-mismatch`` rule (plan A5 wording).
_DOCS_GENERAL_PURPOSES = frozenset({"docs-or-light-edit", "general-dev"})

#: Archetypes that never spawn subagents of their own -- rules whose
#: advice is about subagent behaviour (TTL-per-agent-type, spawn cost,
#: agent report size, subagent volume, baseline bloat via MCP prefix
#: load) are never emitted for these.
_NO_SUBAGENT_ARCHETYPES = frozenset({"chat-only"})

#: Archetypes for which "stop spawning so much" advice is inappropriate:
#: an overseer-fanout session's whole point is fanning work out to
#: subagents, so ``subagent-volume`` should not tell a corpus that spends
#: much of its money that way to stop (see ``_fanout_spend_share_pct``).
_FANOUT_ARCHETYPES = frozenset({"overseer-fanout"})

#: Every archetype ``workstyle.detect_archetype``/``corpus_archetype`` can
#: return, used as the default ``archetypes`` tuple (rules with no
#: specific gating apply to all of them, including ``None``/unclassified
#: which is represented as the empty tuple meaning "no restriction").
_ALL_ARCHETYPES: tuple[str, ...] = ()

#: Claude Code's own bundled agent types (fix A1) -- these ship with the
#: harness itself and have no ``.claude/agents/<type>.md`` frontmatter
#: file anywhere for ``spawn-cost`` to patch, unlike a project- or
#: user-defined custom agent (e.g. ``claude-implementer``). Used only as
#: the fallback when no config snapshot is available to answer the
#: question directly from its ``agents`` map (see ``_agent_has_frontmatter``).
_BUILTIN_AGENT_TYPES = frozenset(
    {
        "claude",
        "general-purpose",
        "Explore",
        "Plan",
        "claude-code-guide",
        "statusline-setup",
        "workflow-subagent",
    }
)


def _agent_has_frontmatter(agent_type: str, snapshot: Snapshot | None) -> bool:
    """Whether ``agent_type`` has its own ``.claude/agents/<type>.md``
    frontmatter file for ``spawn-cost`` (fix A1) to name as a lever.

    A key in the config snapshot's ``agents`` map (populated by
    ``hooks/snapshot-config.py`` from every agent frontmatter file that
    actually exists on disk -- see ``snapshots.py``'s module docstring)
    has a file. Otherwise fall back to a built-in list of Claude Code's
    own bundled agent types (:data:`_BUILTIN_AGENT_TYPES`) -- anything
    else is assumed to be a custom agent with its own file, since that's
    the only kind a corpus would otherwise be spawning. A missing key is
    not proof of no file: the hook records only the agents of the project
    a session started in, so a project whose sessions took no snapshot
    leaves its agents out of every map.
    """
    if snapshot is not None:
        agents_map = snapshot.data.get("agents")
        if isinstance(agents_map, dict) and agent_type in agents_map:
            return True
    return agent_type not in _BUILTIN_AGENT_TYPES


# -- thresholds --------------------------------------------------------------


@dataclass(slots=True)
class RecommendThresholds:
    """Every tunable number Appendix A5 lists for the recommendation
    rules, with A5's own defaults. All overridable via ``config.toml``'s
    ``[thresholds.recommend]`` table -- see :meth:`from_config`.
    """

    # long-tool-waits: "full-expiry share of re-cache cc > 20% and > 60%
    # of those turns follow a gap > 300s after Bash/PowerShell".
    long_tool_waits_full_expiry_share_pct: float = 20.0
    long_tool_waits_gap_share_pct: float = 60.0

    # notification-invalidation: "TASK_NOTIFICATION primary for > 25% of
    # prefix-invalidated cc and its share exceeds the all-turns control
    # share by > 10 points".
    notification_invalidation_share_pct: float = 25.0
    notification_invalidation_overrep_points: float = 10.0

    # batch-instructions: "mean queue operations per session > 3 and
    # queue-preceded turns' re-cache share exceeds control by > 10
    # points". The mean-queue-ops-per-session half has no report table
    # (see module docstring); this threshold is still read from config
    # for forward compatibility but the rule currently fires on the
    # over-representation half alone.
    batch_instructions_mean_queue_ops: float = 3.0
    batch_instructions_overrep_points: float = 10.0

    # subagent-volume: "one agent type > 40% of corpus cost".
    subagent_volume_cost_share_pct: float = 40.0
    # subagent-volume is not given to a corpus where sessions with the
    # overseer-fanout workstyle account for this much of the spend or more:
    # fanning work out is how it works. Weighted by spend, not by session
    # count, since a few large fan-out runs can cost more than many chats.
    fanout_spend_share_pct: float = 30.0

    # compaction-churn: ">= 2 compactions per session in any mode, or
    # dropped tokens > 30% of new tokens".
    compaction_churn_mean_per_session: float = 2.0
    compaction_churn_dropped_share_pct: float = 30.0

    # long-context-share: "> 20% of cache_read volume from turns with
    # ctx >= 200k, or median top-level ctx > 150k" (p90 used as the
    # median's proxy -- see module docstring).
    long_context_share_pct: float = 20.0
    long_context_p90_ctx: float = 150_000.0

    # cache-read-dominance: "cache_read > 50% of cost".
    cache_read_dominance_pct: float = 50.0

    # baseline-bloat: "the part of a session's first call a setting can
    # change (skills list, memory files, MCP tools of servers you can turn
    # off) >= 30k tokens". Not the whole first call (about 43k tokens of it
    # are Claude Code's own tool JSON), and not a count of configured servers.
    baseline_bloat_tokens: float = 30_000.0
    # env-tool-search: the snapshot names at least this many MCP servers.
    baseline_bloat_min_mcp_or_plugins: int = 5

    # agent-report-size: "mean Agent tool_result > 8k tokens for an
    # agent type".
    agent_report_size_tokens: float = 8_000.0

    # agent-batch-probes: "an agent type that made at least 100 replies, a
    # quarter or more of them one read-only call and nothing else, where
    # batching the runs of those would have spared at least $1". The
    # habits_probes figure is the cache reads of the replies after the first
    # of each run, and takes the lookups of a run to be independent, which
    # some are (a read that needs the name a search just found), so the card
    # quotes this share of it.
    agent_batch_probes_min_replies: int = 100
    agent_batch_probes_share_pct: float = 25.0
    agent_batch_probes_min_saving_usd: float = 1.0
    agent_batch_probes_saving_factor: float = 0.5

    # plan-rounds: "at least 5 approved plans, 30% or more of them sent back
    # before you approved, and 30% or more of those rounds a question, a
    # critique or a doubt, where the replies between the first plan and the
    # approval cost at least $1 after scaling". A standing request to critique
    # the plan can answer the rounds that were a question, a critique or a
    # doubt, and only some of them, so the card quotes this share of the
    # table's figure.
    plan_rounds_min_plans: int = 5
    plan_rounds_min_share_pct: float = 30.0
    plan_rounds_min_asked_pct: float = 30.0
    plan_rounds_min_saving_usd: float = 1.0
    plan_rounds_saving_factor: float = 0.25

    # spawn-cost: "mean first call per agent type > 40k tokens, and at least
    # 5k tokens of its tool definitions it never or rarely uses". The first
    # call alone says little: Claude Code's own tool definitions are 51.5k
    # tokens of it on Haiku 4.5 and 69.4k on Sonnet 5, so what can be left
    # out (a tools list on the agent) is what the card is about.
    spawn_cost_tokens: float = 40_000.0
    spawn_cost_removable_tokens: float = 5_000.0
    # spawn-claude-md / spawn-unused-* / spawn-task-prompt (the
    # agent_startup section, part by part): measured spawns before any of
    # them fires, the per-spawn size a CLAUDE.md or shared part must reach,
    # the size an unused part must reach, and a long task prompt.
    spawn_parts_min_spawns: int = 5
    spawn_part_tokens: float = 2_000.0
    spawn_unused_part_tokens: float = 500.0
    spawn_task_prompt_tokens: float = 4_000.0
    # spawn-tools-list: the tokens a tools list would take out of one spawn's
    # start (tool definitions, deferred tool names, skills list and agent
    # list) before the card fires.
    spawn_tools_list_tokens: float = 5_000.0
    # spawn-unused-mcp names an MCP server when at most this share of the
    # spawns offered it called it, and it costs at least this much over 30
    # days across the spawns that were offered it.
    spawn_unused_mcp_use_share_pct: float = 2.0
    spawn_unused_mcp_min_usd_30d: float = 5.0

    # effort-mismatch: ">= 30% of output tokens are thinking on sessions
    # whose purpose is docs/general-dev at effort high or above".
    effort_mismatch_thinking_share_pct: float = 30.0

    # discovery-share: a phases section with DISCOVERY > 35% (matches
    # phases.DISCOVERY_SHARE_THRESHOLD's 0.35, expressed here as a pct).
    discovery_share_pct: float = 35.0

    # data-quality: "unparsable > 0.1% of lines, ttl mismatches > 0,
    # fidelity > 10%".
    data_quality_unparsable_pct: float = 0.1
    data_quality_fidelity_pct: float = 10.0

    # limit-pressure (v3-limits addition, not part of plan Appendix A5):
    # ">= 2 five-hour limit stops per 7 days, any weekly stop that
    # stopped work, or >= 1 subagent cut off by a limit". A stop is one
    # limit being reached, however many limit messages it wrote
    # (``limits.LimitStats.episodes``). The older
    # ``limit_pressure_min_hits`` key counted messages: it is read but
    # ignored.
    limit_pressure_min_episodes: int = 2
    limit_pressure_min_terminated_rate_limit: int = 1

    #: Minimum sample before ANY rule fires: 5 sessions OR 200 priced
    #: turns in the group (plan A5's closing line). Mirrors
    #: ``Config.min_sessions``/``Config.min_turns`` -- kept here too so a
    #: caller overriding ``[thresholds.recommend]`` can tune it
    #: independently of the corpus-wide config default.
    min_sessions: int = 5
    min_turns: int = 200

    @classmethod
    def from_config(cls, data: dict | None, config: "Config | None" = None) -> "RecommendThresholds":
        """Build thresholds from ``config.toml``'s
        ``[thresholds.recommend]`` table (a flat dict of this class's
        field names). Any absent or malformed key keeps this class's
        default; unknown keys are ignored -- same posture as
        ``recache.RecacheThresholds.from_config``/
        ``ttl.TtlThresholds.from_config``.

        Fix R3: when ``config`` is given, ``min_sessions``/``min_turns``
        default to *its* ``min_sessions``/``min_turns`` rather than this
        class's own hardcoded 5/200 -- ``report.py``'s header prints
        ``config.min_sessions``/``config.min_turns``, so without this the
        two could silently disagree on the very numbers a reader is told
        gate every recommendation. An explicit ``[thresholds.recommend]``
        override still wins over both.
        """
        base_kwargs = {f: getattr(cls(), f) for f in cls.__dataclass_fields__}
        if config is not None:
            base_kwargs["min_sessions"] = config.min_sessions
            base_kwargs["min_turns"] = config.min_turns
        defaults = cls(**base_kwargs)
        if not isinstance(data, dict):
            return defaults
        kwargs: dict = {}
        for f in defaults.__dataclass_fields__:
            if f in data:
                raw = data[f]
                default_value = getattr(defaults, f)
                try:
                    kwargs[f] = type(default_value)(raw)
                except (TypeError, ValueError):
                    continue
        if not kwargs:
            return defaults
        return cls(**{**{f: getattr(defaults, f) for f in defaults.__dataclass_fields__}, **kwargs})


# -- report lookup helpers ----------------------------------------------------


def _section(report: ReportModel, key: str) -> Section | None:
    for section in report.sections:
        if section.key == key:
            return section
    return None


def _table(report: ReportModel, section_key: str, table_name: str) -> Table | None:
    section = _section(report, section_key)
    if section is None:
        return None
    for table in section.tables:
        if table.name == table_name:
            return table
    return None


def _col_index(table: Table, column_key: str) -> int | None:
    for idx, column in enumerate(table.columns):
        if column.key == column_key:
            return idx
    return None


def _row(table: Table, row_key) -> list | None:
    for row in table.rows:
        if row and row[0] == row_key:
            return row
    return None


def _cell(report: ReportModel, section_key: str, table_name: str, row_key, column_key: str):
    """Look up one cell, returning ``None`` when the section/table/row/
    column doesn't exist (a caller treats that as "condition can't be
    evaluated", never as a false ``0``)."""
    table = _table(report, section_key, table_name)
    if table is None:
        return None
    row = _row(table, row_key)
    if row is None:
        return None
    idx = _col_index(table, column_key)
    if idx is None or idx >= len(row):
        return None
    return row[idx]


def _evidence(label: str, value, section_key: str, table_name: str, row_key) -> tuple:
    """One ``Recommendation.evidence`` tuple: ``source_table`` is
    ``"<section_key>.<table_name>"`` and ``row_key`` is exactly
    ``row_key`` -- both per the evidence contract this module's docstring
    describes."""
    return (label, value, f"{section_key}.{table_name}", row_key)


# -- minimum sample gate -------------------------------------------------


def _corpus_sessions_and_priced_turns(report: ReportModel) -> tuple[int, int]:
    """Total sessions and total priced turns for the whole report, read
    from ``overview.totals`` (a single-row-per-metric table built once
    per report, so this is O(1) rather than summing a per-group table)."""
    sessions = _cell(report, "overview", "totals", "sessions", "value")
    priced_turns = _cell(report, "overview", "totals", "priced_turns", "value")
    return (
        int(sessions) if isinstance(sessions, (int, float)) else 0,
        int(priced_turns) if isinstance(priced_turns, (int, float)) else 0,
    )


def _meets_min_sample(report: ReportModel, th: RecommendThresholds) -> bool:
    sessions, priced_turns = _corpus_sessions_and_priced_turns(report)
    return sessions >= th.min_sessions or priced_turns >= th.min_turns


def effective_min_sample(th: RecommendThresholds) -> tuple[int, int]:
    """Fix R3: ``(sessions, turns)`` -- the minimum-sample numbers ``th``
    actually gates recommendations on, for a caller (``report.py``'s
    header) that wants to print the number this module is really using
    rather than assuming it always equals ``Config.min_sessions``/
    ``Config.min_turns`` (an explicit ``[thresholds.recommend]``
    override can deliberately diverge from the config default -- see
    :meth:`RecommendThresholds.from_config`)."""
    return (th.min_sessions, th.min_turns)


def _row_meets_min_sample(th: RecommendThresholds, spawns: int | None, priced_turns: int | None) -> bool:
    """Fix R3: per-group analogue of :func:`_meets_min_sample` for a
    rule that advises on one row of a per-agent-type table (subagent-
    volume, spawn-cost, ttl-switch, agent-report-size) -- the corpus-
    wide gate alone doesn't stop a rule from confidently advising on a
    single-spawn agent type just because the *rest* of the corpus is
    large enough to clear it. ``spawns``/``priced_turns`` are ``None``
    when the row's own table doesn't carry that column (an older or
    hand-built minimal table shape): treated as "can't evaluate this
    half of the gate" rather than a failing zero, so a table exposing
    only one of the two still gates on whichever it has. Every caller
    still applies the corpus-wide :func:`_meets_min_sample` gate too
    (in :func:`recommend`, before any rule runs) -- this is an
    additional floor per group, not a replacement for it."""
    if spawns is None and priced_turns is None:
        return True
    if spawns is not None and spawns >= th.min_sessions:
        return True
    if priced_turns is not None and priced_turns >= th.min_turns:
        return True
    return False


# -- scope encoding (see module docstring's WP10-merge update note) --------

#: A per-agent-type TTL lever names the ``.claude/agents/<type>.md``
#: frontmatter path it would edit -- see ``ttl.TtlTypeStats.lever``. That
#: file lives in the repo, so this lever's default scope is "repo" rather
#: than "user". Shared with ``render_patch_set``, which uses the same
#: pattern to find the agent type for the diff header.
_AGENT_LEVER_RE = re.compile(r"experimental\.cacheTtl in ([^.]+)\.md")


def _lever_managed_keys(lever: str) -> tuple[str, ...]:
    """Top-level ``managed-settings.json`` key name(s) that would govern
    ``lever`` if an administrator locked it down -- the candidates
    ``snapshots.managed_keys(snapshot)`` is checked against."""
    if lever == "promptCacheTtl":
        return ("promptCacheTtl",)
    if "subagentPromptCacheTtl" in lever or "experimental.cacheTtl" in lever:
        return ("subagentPromptCacheTtl", "agents")
    if lever in ("omitClaudeMd", "skills", "autoCompactWindow", "effortLevel"):
        return (lever,)
    return (lever,)


#: ``snapshots.effective_provenance``'s layer names
#: (``snapshots.SETTINGS_LAYER_NAMES``), mapped to the ``"user"``/
#: ``"project-local"``/``"repo"``/``"managed"`` vocabulary this module's
#: ``scope`` field and ``fixes.py``'s ``_SETTINGS_WHERE``/``command_for``
#: use (COV-01). A layer name absent here (there is none today) would
#: fall back to ``"user"`` at each call site below, same as an unset key.
_PROVENANCE_SCOPE = {
    "managed": "managed",
    "project_local": "project-local",
    "project_shared": "repo",
    "user": "user",
}


def _lever_scope(lever: str | None, snapshot: Snapshot | None) -> tuple[str | None, str]:
    """Returns ``(lever, scope)`` -- ``lever`` unchanged (bare, never
    prefixed). ``scope`` is ``"repo"`` when ``lever`` is a per-agent
    frontmatter path (agent files have no project-local split -- see the
    module docstring's COV-01 note). For a plain settings key, COV-01
    reads ``snapshots.effective_provenance`` to name the layer that
    actually supplies its value today (``"user"``/``"project-local"``/
    ``"repo"``/``"managed"``), falling back to ``"user"`` when the key
    has no effective value in any layer yet (nothing overrides it, so
    "user" is where a new value would land by default). Either way,
    upgraded to ``"managed"`` when any of ``lever``'s underlying settings
    keys appears in ``snapshots.managed_keys(snapshot)`` -- a locked-down
    key is "managed" scope even before a session's own config sets it,
    since a recommendation could never be applied locally regardless of
    what ``effective_provenance`` shows today."""
    if lever is None:
        return lever, "user"
    if _AGENT_LEVER_RE.search(lever):
        scope = "repo"
    else:
        scope = "user"
        if snapshot is not None:
            layer = effective_provenance(snapshot).get(lever)
            if layer in _PROVENANCE_SCOPE:
                scope = _PROVENANCE_SCOPE[layer]
    if snapshot is not None:
        keys = set(managed_keys(snapshot))
        if keys and any(k in keys for k in _lever_managed_keys(lever)):
            scope = "managed"
    return lever, scope


def _action_with_scope(action: str, scope: str) -> str:
    if scope == "managed":
        return f"{action} This lever is managed by policy, raise with your administrator."
    return action


def _ttl_row_lever(agent_type, lever, snapshot: Snapshot | None):
    """The lever a ``ttl_by_agent_type`` row's switch can actually pull.
    Claude Code resolves a subagent's cache lifetime from the
    ``subagentPromptCacheTtl`` setting before an agent file's
    ``experimental.cacheTtl`` (prompt-caching docs, "Choose the TTL
    yourself"), so once any settings layer sets it, editing the agent
    file does nothing and the setting is the lever. Otherwise ``lever``
    (``ttl.TtlTypeStats.lever``) unchanged."""
    if agent_type == "top-level" or snapshot is None:
        return lever
    if "subagentPromptCacheTtl" in effective_config(snapshot):
        return "subagentPromptCacheTtl"
    return lever


# -- individual rules ---------------------------------------------------


def _rule_ttl_switch(
    report: ReportModel,
    config: Config,
    snapshot: Snapshot | None,
    archetype: str | None,
    th: RecommendThresholds,
) -> list[Recommendation]:
    if config.provider not in (None, "anthropic"):
        # No [providers.*] capability table exists anywhere in this
        # codebase (see module docstring) -- suppress outright rather
        # than guess whether this provider honours a 1h TTL.
        return []
    table = _table(report, "ttl", "ttl_by_agent_type")
    if table is None:
        return []
    rec_idx = _col_index(table, "recommendation")
    lever_idx = _col_index(table, "lever")
    spawns_idx = _col_index(table, "spawns")
    priced_turns_idx = _col_index(table, "priced_turns")
    if rec_idx is None or lever_idx is None:
        return []
    out: list[Recommendation] = []
    for row in table.rows:
        agent_type = row[0]
        # Fix R10: this rule's own module docstring (see
        # _NO_SUBAGENT_ARCHETYPES above) already claims per-agent-type
        # TTL advice is suppressed for archetypes that never spawn
        # subagents -- but this function accepted `archetype` and never
        # once read it. Honour that claim for non-top-level rows (the
        # top-level row is the session's own TTL, which chat-only
        # sessions still have and can still act on).
        if agent_type != "top-level" and archetype in _NO_SUBAGENT_ARCHETYPES:
            continue
        recommendation_text = row[rec_idx]
        if not isinstance(recommendation_text, str) or not recommendation_text.startswith("switch to "):
            continue
        # Fix R3: this row's own sample size must also clear the
        # minimum-sample bar, not just the corpus as a whole.
        spawns = row[spawns_idx] if spawns_idx is not None and spawns_idx < len(row) else None
        priced_turns = row[priced_turns_idx] if priced_turns_idx is not None and priced_turns_idx < len(row) else None
        if not _row_meets_min_sample(th, spawns, priced_turns):
            continue
        target = recommendation_text[len("switch to ") :]
        lever = _ttl_row_lever(agent_type, row[lever_idx], snapshot)
        lever, scope = _lever_scope(lever, snapshot)
        action = _action_with_scope(
            f"Switch {agent_type}'s prompt cache TTL to {target}.", scope
        )
        out.append(
            Recommendation(
                id="ttl-switch",
                severity="action",
                category="settings",
                archetypes=_ALL_ARCHETYPES,
                title=f"Cache TTL is a poor fit for {agent_type}",
                action=action,
                lever=lever,
                scope=scope,
                agent_type=agent_type,
                evidence=[
                    _evidence("TTL recommendation", recommendation_text, "ttl", "ttl_by_agent_type", agent_type),
                ],
            )
        )
    return out


def _rule_long_tool_waits(report: ReportModel, th: RecommendThresholds) -> list[Recommendation]:
    summary = _table(report, "recache", "recache_summary")
    if summary is None or not summary.rows:
        return []
    summary_row_key = summary.rows[0][0]
    total_recache_cc = _cell(report, "recache", "recache_summary", summary_row_key, "recache_cc_tokens")
    if not total_recache_cc:
        return []
    full_expiry_cc = _cell(report, "recache", "recache_signature_split", "full-expiry", "cc_tokens")
    if full_expiry_cc is None:
        return []
    full_expiry_share_pct = 100.0 * full_expiry_cc / total_recache_cc if total_recache_cc else 0.0
    if full_expiry_share_pct <= th.long_tool_waits_full_expiry_share_pct:
        return []

    # "> 60% of those turns follow a gap > 300s after Bash/PowerShell":
    # recache_preceding_tool and recache_gap_buckets are the two closest
    # tables, but neither -- nor anything else in this codebase --
    # exposes the *joint* count of turns that are both preceded by
    # Bash/PowerShell AND follow a long gap (see module docstring).
    # Fix R14: min(tool_share, long_gap_share) is only an upper bound on
    # that joint share, not the share itself (e.g. two disjoint 70%
    # turn-sets can never overlap by more than 40%, yet min() would
    # still report 70%) -- it could pass this gate on inputs whose real
    # joint share is much smaller. Since the joint count is genuinely
    # unavailable, require each share to independently clear the
    # threshold instead of pretending to combine them, and cite each
    # contributing table cell as its own evidence entry.
    tool_table = _table(report, "recache", "recache_preceding_tool")
    gap_table = _table(report, "recache", "recache_gap_buckets")
    if tool_table is None or gap_table is None:
        return []
    bash_share = _cell(report, "recache", "recache_preceding_tool", "Bash", "share_pct_turns") or 0.0
    pwsh_share = _cell(report, "recache", "recache_preceding_tool", "PowerShell", "share_pct_turns") or 0.0
    tool_share = bash_share + pwsh_share

    long_gap_buckets = (">60m", "15-60m", "5-15m")
    long_gap_evidence = []
    long_gap_share = 0.0
    for bucket in long_gap_buckets:
        value = _cell(report, "recache", "recache_gap_buckets", bucket, "share_pct_turns") or 0.0
        long_gap_share += value
        long_gap_evidence.append(
            _evidence(f"{bucket} gap-bucket re-cache turn share", value, "recache", "recache_gap_buckets", bucket)
        )

    if tool_share <= th.long_tool_waits_gap_share_pct or long_gap_share <= th.long_tool_waits_gap_share_pct:
        return []

    return [
        Recommendation(
            id="long-tool-waits",
            severity="advice",
            category="workflow",
            archetypes=_ALL_ARCHETYPES,
            title="Long tool waits are expiring the cache",
            action=(
                "Batch instructions before a long-running Bash/PowerShell command so the "
                "prefix survives the wait, or shorten the wait itself."
            ),
            lever=None,
            evidence=[
                _evidence("Full-expiry cache-creation tokens", full_expiry_cc, "recache", "recache_signature_split", "full-expiry"),
                _evidence("Bash re-cache turn share", bash_share, "recache", "recache_preceding_tool", "Bash"),
                _evidence("PowerShell re-cache turn share", pwsh_share, "recache", "recache_preceding_tool", "PowerShell"),
                *long_gap_evidence,
            ],
        )
    ]


def _rule_notification_invalidation(report: ReportModel, th: RecommendThresholds) -> list[Recommendation]:
    row_key = "task_notification"
    share_pct = _cell(report, "recache", "recache_primary_cause_prefix_invalidated", row_key, "cc_share_pct")
    if share_pct is None or share_pct <= th.notification_invalidation_share_pct:
        return []
    over_rep = _cell(
        report, "recache", "recache_primary_cause_prefix_invalidated", row_key, "over_representation_points_tokens"
    )
    if over_rep is None or over_rep <= th.notification_invalidation_overrep_points:
        return []
    return [
        Recommendation(
            id="notification-invalidation",
            severity="advice",
            category="workflow",
            archetypes=_ALL_ARCHETYPES,
            title="Task notifications are invalidating the cache prefix",
            action=(
                "Reduce how often a subagent's task-notification lands mid-conversation, or "
                "batch notifications so fewer of them arrive while the parent's cache prefix "
                "is still warm."
            ),
            lever=None,
            evidence=[
                _evidence(
                    "TASK_NOTIFICATION share of prefix-invalidated cc",
                    share_pct,
                    "recache",
                    "recache_primary_cause_prefix_invalidated",
                    row_key,
                ),
                _evidence(
                    "Over-representation vs all-turns control",
                    over_rep,
                    "recache",
                    "recache_primary_cause_prefix_invalidated",
                    row_key,
                ),
            ],
        )
    ]


def _rule_batch_instructions(report: ReportModel, th: RecommendThresholds) -> list[Recommendation]:
    # The mean-queue-ops-per-session half of this rule has no report
    # table (see module docstring); this rule fires on the
    # over-representation half alone.
    row_key = "queue_operation"
    over_rep = _cell(report, "recache", "recache_primary_cause", row_key, "over_representation_points_tokens")
    if over_rep is None or over_rep <= th.batch_instructions_overrep_points:
        return []
    share_pct = _cell(report, "recache", "recache_primary_cause", row_key, "cc_share_pct")
    return [
        Recommendation(
            id="batch-instructions",
            severity="advice",
            category="workflow",
            archetypes=_ALL_ARCHETYPES,
            title="Queued instructions are re-writing the cache prefix",
            action=(
                "Batch queued instructions into a single message instead of sending them one "
                "at a time, so each doesn't force its own cache re-write."
            ),
            lever=None,
            evidence=[
                _evidence(
                    "Over-representation vs all-turns control",
                    over_rep,
                    "recache",
                    "recache_primary_cause",
                    row_key,
                ),
                _evidence("Queue-preceded share of re-cache cc", share_pct, "recache", "recache_primary_cause", row_key),
            ],
        )
    ]


def _fanout_spend_share_pct(report: ReportModel) -> float | None:
    """The percent of the corpus's spend made in sessions with a fan-out
    workstyle (``_FANOUT_ARCHETYPES``), from the Spend column of the
    workstyle table. ``None`` when that table is missing, has no spend
    column or no money in it (a corpus where nothing was priced, or the
    section was left out), which leaves the caller to the corpus's
    ``archetype``."""
    table = _table(report, "workstyle", "workstyle_archetypes")
    if table is None:
        return None
    archetype_idx = _col_index(table, "archetype")
    spend_idx = _col_index(table, "spend")
    if archetype_idx is None or spend_idx is None:
        return None
    spend = [
        (row[archetype_idx], row[spend_idx])
        for row in table.rows
        if spend_idx < len(row) and isinstance(row[spend_idx], (int, float))
    ]
    total = sum(amount for _, amount in spend)
    if total <= 0:
        return None
    return 100.0 * sum(amount for name, amount in spend if name in _FANOUT_ARCHETYPES) / total


def _rule_subagent_volume(report: ReportModel, th: RecommendThresholds, archetype: str | None) -> list[Recommendation]:
    fanout_share = _fanout_spend_share_pct(report)
    if fanout_share is None:
        fanout = archetype in _FANOUT_ARCHETYPES
    else:
        fanout = fanout_share >= th.fanout_spend_share_pct
    if fanout:
        # Fanning work out is how this corpus works: "stop spawning so
        # much" is not appropriate advice for it.
        return []
    table = _table(report, "ttl", "ttl_by_agent_type")
    if table is None:
        return []
    cost_idx = _col_index(table, "cost_observed")
    spawns_idx = _col_index(table, "spawns")
    priced_turns_idx = _col_index(table, "priced_turns")
    if cost_idx is None:
        return []
    total_cost = sum(row[cost_idx] for row in table.rows if cost_idx < len(row) and isinstance(row[cost_idx], (int, float)))
    if not total_cost:
        return []
    # Phase 8a: the card also names the cost centre with the most spend
    # (advice._explain_subagent_volume words it) and cites that row of the
    # spend-by-cost-centre table, absent from a hand-built report.
    centre = cost_centres.largest_centre(report)
    out: list[Recommendation] = []
    for row in table.rows:
        agent_type = row[0]
        if agent_type == "top-level":
            continue
        cost = row[cost_idx]
        share_pct = 100.0 * cost / total_cost
        if share_pct <= th.subagent_volume_cost_share_pct:
            continue
        # Fix R3: this agent type's own sample size must also clear the
        # minimum-sample bar, not just the corpus as a whole.
        spawns = row[spawns_idx] if spawns_idx is not None and spawns_idx < len(row) else None
        priced_turns = row[priced_turns_idx] if priced_turns_idx is not None and priced_turns_idx < len(row) else None
        if not _row_meets_min_sample(th, spawns, priced_turns):
            continue
        out.append(
            Recommendation(
                id="subagent-volume",
                severity="advice",
                category="workflow",
                archetypes=_ALL_ARCHETYPES,
                title=f"{agent_type} dominates subagent cost",
                action=(
                    f"Review why {agent_type} accounts for {share_pct:.1f}% of the corpus's "
                    "subagent spend -- fewer spawns, a cheaper model, or a tighter brief "
                    "before spawning it."
                ),
                lever=None,
                agent_type=agent_type,
                evidence=[
                    # Fix R11: cite the real cost_observed cell -- there is
                    # no "share of corpus cost" column on ttl_by_agent_type
                    # to cite. share_pct is derived from this cell (plus the
                    # table's other rows) and stated in the action text
                    # instead, per the evidence contract (module docstring).
                    _evidence("Cost (observed)", cost, "ttl", "ttl_by_agent_type", agent_type),
                    *(
                        [_evidence("Largest cost centre", centre[2], "agents", "cost_centres", centre[0])]
                        if centre is not None
                        else []
                    ),
                ],
            )
        )
    return out


def _rule_compaction_churn(report: ReportModel, th: RecommendThresholds, snapshot: Snapshot | None) -> list[Recommendation]:
    mean_per_session = _cell(report, "compactions", "compactions_summary", "Compactions per session (mean)", "value")
    dropped_share_row_key = "Dropped tokens (share of new_tokens: input+cache_creation)"
    dropped_share_pct = _cell(report, "compactions", "compactions_summary", dropped_share_row_key, "value")

    fires_on_mean = isinstance(mean_per_session, (int, float)) and mean_per_session >= th.compaction_churn_mean_per_session
    fires_on_dropped = isinstance(dropped_share_pct, (int, float)) and dropped_share_pct > th.compaction_churn_dropped_share_pct
    if not (fires_on_mean or fires_on_dropped):
        return []

    lever, scope = _lever_scope("autoCompactWindow", snapshot)
    evidence = []
    if fires_on_mean:
        evidence.append(
            _evidence("Compactions per session (mean)", mean_per_session, "compactions", "compactions_summary", "Compactions per session (mean)")
        )
    if fires_on_dropped:
        evidence.append(
            _evidence("Dropped-token share of new tokens", dropped_share_pct, "compactions", "compactions_summary", dropped_share_row_key)
        )
    return [
        Recommendation(
            id="compaction-churn",
            severity="advice",
            category="settings",
            archetypes=_ALL_ARCHETYPES,
            title="Compaction is running often enough to matter",
            action=_action_with_scope(
                "Raise autoCompactWindow so compaction fires less often, or reduce session length "
                "between compactions.",
                scope,
            ),
            lever=lever,
            scope=scope,
            evidence=evidence,
        )
    ]


def _rule_long_context_share(report: ReportModel, th: RecommendThresholds, snapshot: Snapshot | None) -> list[Recommendation]:
    huge_table = _table(report, "recache", "recache_huge_context")
    row_key = huge_table.rows[0][0] if huge_table and huge_table.rows else None
    share_pct = _cell(report, "recache", "recache_huge_context", row_key, "share_pct") if row_key is not None else None
    p90_ctx = _cell(report, "scorecard", "dimensions", "context_hygiene", "value")

    fires_on_share = isinstance(share_pct, (int, float)) and share_pct > th.long_context_share_pct
    fires_on_p90 = isinstance(p90_ctx, (int, float)) and p90_ctx > th.long_context_p90_ctx
    if not (fires_on_share or fires_on_p90):
        return []

    lever, scope = _lever_scope("autoCompactWindow", snapshot)
    evidence = []
    if fires_on_share:
        evidence.append(_evidence("Cache-read volume share from huge-context turns", share_pct, "recache", "recache_huge_context", row_key))
    if fires_on_p90:
        evidence.append(_evidence("Context size 9 in 10 main-session replies stay under", p90_ctx, "scorecard", "dimensions", "context_hygiene"))
    return [
        Recommendation(
            id="long-context-share",
            severity="advice",
            category="settings",
            archetypes=_ALL_ARCHETYPES,
            title="Top-level context is running large",
            action=_action_with_scope(
                "Trim what stays resident in the top-level conversation -- compact sooner, or "
                "move exploration into a subagent whose context is discarded when it finishes.",
                scope,
            ),
            lever=lever,
            scope=scope,
            evidence=evidence,
        )
    ]


def _rule_cache_read_dominance(report: ReportModel, th: RecommendThresholds) -> list[Recommendation]:
    share_pct = _cell(report, "overview", "totals", "cache_read_cost_share_pct", "value")
    if not isinstance(share_pct, (int, float)) or share_pct <= th.cache_read_dominance_pct:
        return []
    return [
        Recommendation(
            id="cache-read-dominance",
            severity="info",
            category="workflow",
            archetypes=_ALL_ARCHETYPES,
            title="Cache reads already dominate spend",
            action=(
                "Most of this corpus's cost is already cheap cache-read traffic -- there is "
                "little further caching upside here; look at cache-creation drivers instead."
            ),
            lever=None,
            evidence=[
                _evidence("Cache-read share of cost", share_pct, "overview", "totals", "cache_read_cost_share_pct"),
            ],
        )
    ]


def _controllable_baseline(report: ReportModel) -> tuple[object, float] | None:
    """The ``context_budget_baseline`` row with the most at the start of a
    session that a setting can change (its skills list, memory files and
    the MCP tools of servers you can turn off), as ``(row key, tokens)``.
    A project's own row, as the "all" row has no memory files of its own:
    the "all" row only when there is no project row. ``None`` when the table or column is missing
    or no row sizes it."""
    table = _table(report, "context_budget", "context_budget_baseline")
    if table is None:
        return None
    index = _col_index(table, "controllable_est")
    if index is None:
        return None
    rows = [row for row in table.rows if row and row[0] != "all"] or [row for row in table.rows if row]
    best: tuple[object, float] | None = None
    for row in rows:
        value = row[index] if index < len(row) else None
        if isinstance(value, (int, float)) and (best is None or value > best[1]):
            best = (row[0], float(value))
    return best


def _rule_baseline_bloat(
    report: ReportModel, th: RecommendThresholds, snapshot: Snapshot | None, archetype: str | None
) -> list[Recommendation]:
    if archetype in _NO_SUBAGENT_ARCHETYPES:
        return []
    # Gated on the part a setting can change, never on the whole first
    # call: about 43k tokens of every first call are Claude Code's own
    # tool definitions and system prompt, which no setting removes, so
    # the raw size says nothing about the user's own configuration.
    controllable = _controllable_baseline(report)
    if controllable is None or controllable[1] < th.baseline_bloat_tokens:
        return []
    cb_row_key = controllable[0]

    # The controllable part first, then the sized buckets that make it up:
    # the card names *where* the baseline goes, not just how big it is.
    evidence = [
        _evidence(
            "What you can change at the start of a session (est)",
            controllable[1],
            "context_budget",
            "context_budget_baseline",
            cb_row_key,
        )
    ]
    biggest_bucket_label: str | None = None
    # The residual ("system prompt and tools") is a remainder -- mean first
    # call minus every other known bucket -- not a sized bucket in its own
    # right, so it is excluded from the "biggest bucket" contest entirely.
    # It is still shown as evidence, as is the measured first call.
    bucket_columns = (
        ("human_prompt_est", "human prompt"),
        ("skills_listing_est", "skills listing"),
        ("memory_files_est", "memory files"),
        ("custom_agents_est", "custom agents"),
        ("mcp_removable_tokens", "MCP tools you can turn off"),
    )
    best_value: float | None = None
    for column_key, label in bucket_columns:
        value = _cell(report, "context_budget", "context_budget_baseline", cb_row_key, column_key)
        if not isinstance(value, (int, float)):
            continue
        evidence.append(
            _evidence(f"Estimated {label} (est)", value, "context_budget", "context_budget_baseline", cb_row_key)
        )
        # Only what a setting can change competes for "largest".
        if column_key != "human_prompt_est" and (best_value is None or value > best_value):
            best_value, biggest_bucket_label = value, label
    first_call = _cell(report, "context_budget", "context_budget_baseline", cb_row_key, "mean_baseline")
    if isinstance(first_call, (int, float)):
        evidence.append(
            _evidence(
                "Mean first call (measured)", first_call, "context_budget", "context_budget_baseline", cb_row_key
            )
        )
    residual = _cell(report, "context_budget", "context_budget_baseline", cb_row_key, "system_prompt_and_tools_est")
    if isinstance(residual, (int, float)):
        evidence.append(
            _evidence(
                "Estimated system prompt and tools (est)",
                residual,
                "context_budget",
                "context_budget_baseline",
                cb_row_key,
            )
        )

    action = (
        "Review which MCP servers and tool schemas load by default -- disabling unused "
        "ones shrinks every session's first call."
    )
    if biggest_bucket_label:
        action += f" The largest estimated share of that baseline is {biggest_bucket_label}."

    return [
        Recommendation(
            id="baseline-bloat",
            severity="advice",
            category="settings",
            archetypes=_ALL_ARCHETYPES,
            title="The session baseline is large before any work happens",
            action=action,
            # No lever: MCP servers aren't a settings key (they live in
            # ~/.claude.json and .mcp.json), so --patch-set has nothing to
            # write for this card. Its fix is a prompt (fixes.py), and no
            # one settings file, so no scope chip either.
            lever=None,
            scope="",
            evidence=evidence,
        )
    ]


# -- COV-09: env-var / deprecated-setting lever rules ---------------------
#
# Every rule below reads its gating condition straight from ``snapshot``
# (the latest "current config" snapshot), since none of these five
# levers has a per-project group-by table of its own -- only presence/
# value facts. Evidence still cites a real, already-rendered table cell:
# ``snapshots.build_env_levers_table`` (wired into report.py's "config"
# section as ``config.env-levers``) exists for exactly this, one row per
# lever keyed by its bare name (see that function's own docstring). No
# ``RecommendThresholds`` fields needed: every condition here is "is this
# lever set" (COV-09), not "does a metric cross a numeric line".
#
# COV-07/COV-11 (P7b): the ambiguity the paragraph below used to describe
# ("a shell profile, or a settings.json env block are both legitimate")
# is now resolved -- guidance always goes through the settings.json
# ``env`` block, matching ``fixes.py``'s ``_where``/``prompt_for`` and
# ``apply --set env.NAME=value`` (``cli._one_off_profile``). A rule below
# populates ``changes`` with a real ``SettingChange(key="env.NAME", ...)``
# only when it proposes a concrete value (``env-tool-search``); the two
# that propose *removing* a value (``env-disable-prompt-caching``,
# ``env-max-output-tokens``) still get a ``SettingChange``, but with
# ``value=None``/``suggested=...`` -- ``apply`` has no "unset" primitive
# (only ``--set``), so these get the full where/trade-off/undo explainer
# and a prompt, never a fabricated command that would write a value
# nobody asked for. The remaining two (``env-subagent-model``, purely
# informational; ``env-attribution-deprecated``, whose real target,
# ``attribution.commit``, is not on ``profiles.schema.SETTINGS_ALLOWLIST``)
# still have no ``SettingChange`` and instead pick up a where/trade-off/
# undo explainer from ``fixes._WORKFLOW_EXPLAINER``. Every rule's own
# ``action`` text keeps stating the concrete undo/change itself too (the
# id/where/trade-off/undo hard constraint every rule card must meet on
# its own, independent of whether a fix is also attached).


def _env_lever_scope(name: str, snapshot: Snapshot | None) -> str:
    """COV-01: the scope ``name``'s effective source layer maps to
    (``"user"``/``"project-local"``/``"repo"``/``"managed"``), the same
    four-way vocabulary ``_lever_scope`` gives a settings.json key --
    but these are env vars: ``_lever_scope``'s ``managed_keys()`` lookup
    only walks settings.json keys, so it can't answer this.
    ``effective_env_provenance`` (COV-03, ``build_effective_env_names``)
    already names the winning layer per env var name, so this reads that
    directly and maps it through the same ``_PROVENANCE_SCOPE`` table,
    falling back to ``"user"`` when the name has no effective value in
    any layer yet.
    """
    if snapshot is None:
        return "user"
    provenance = snapshot.data.get("effective_env_provenance")
    if isinstance(provenance, dict):
        layer = provenance.get(name)
        if layer in _PROVENANCE_SCOPE:
            return _PROVENANCE_SCOPE[layer]
    return "user"


#: Highest-precedence-first, for picking one scope to report when several
#: env var names are set at different layers (``env-disable-prompt-
#: caching`` can fire on more than one name at once) -- "advice must never
#: target a layer that a higher layer overrides" (COV-01) means the most
#: overriding layer among the set names is the one worth naming.
_SCOPE_PRECEDENCE: tuple[str, ...] = ("managed", "project-local", "repo", "user")


#: docs/en/env-vars.md: one global switch plus a per-model-family override.
_DISABLE_PROMPT_CACHING_NAMES: tuple[str, ...] = (
    "DISABLE_PROMPT_CACHING",
    "DISABLE_PROMPT_CACHING_SONNET",
    "DISABLE_PROMPT_CACHING_OPUS",
    "DISABLE_PROMPT_CACHING_HAIKU",
    "DISABLE_PROMPT_CACHING_FABLE",
)


def _rule_env_disable_prompt_caching(report: ReportModel, snapshot: Snapshot | None) -> list[Recommendation]:
    """COV-09, "high severity" per the plan (``severity="action"``, this
    codebase's vocabulary -- see the module docstring's severity note
    elsewhere in this file): any of the five ``DISABLE_PROMPT_CACHING*``
    levers (docs/en/env-vars.md) turns prompt caching off for some or all
    models, which is usually accidental -- caching is what makes a
    multi-turn session cheap. The snapshot only records whether each name
    is *set* (never its value, matching every other env lever's privacy
    posture -- snapshot-config.py's ``_ENV_NAME_PREFIXES`` comment: "names
    only, values never recorded"), so the action states the doc's own
    caveat: only a value of ``1`` actually disables caching.
    """
    if snapshot is None:
        return []
    env_names = set(snapshot.data.get("env_names") or [])
    set_names = [name for name in _DISABLE_PROMPT_CACHING_NAMES if name in env_names]
    if not set_names:
        return []

    evidence = [_evidence(name, True, "config", "env-levers", name) for name in set_names]
    scopes = [_env_lever_scope(name, snapshot) for name in set_names]
    scope = next((s for s in _SCOPE_PRECEDENCE if s in scopes), "user")
    named = " and ".join(set_names)
    action = (
        f"{named} {'is' if len(set_names) == 1 else 'are'} set in this environment. If set to `1`, this "
        "turns prompt caching off for the model(s) it names, so every request re-sends and re-processes "
        "the full prefix instead of reading it from cache -- usually far more expensive, not less. Ask "
        f"Claude to find where {set_names[0] if len(set_names) == 1 else 'these'} "
        f"{'is' if len(set_names) == 1 else 'are'} set (a shell profile, or settings.json's `env` block) "
        "and remove it, unless you're deliberately testing without caching. To undo by hand: unset the "
        "variable, or delete its `env` entry in settings.json."
    )
    # COV-07/COV-11: the fix is removing the value, not setting a new one
    # -- ``apply`` has no unset primitive, so each name gets a
    # judgement-needed SettingChange (explainer + prompt, no command)
    # rather than a fabricated "set it to 0" nobody asked for.
    changes = [
        SettingChange(
            target="settings",
            key=f"env.{name}",
            current="set",
            suggested="removed, unless you're deliberately testing without caching",
            scope=_env_lever_scope(name, snapshot),
        )
        for name in set_names
    ]
    return [
        Recommendation(
            id="env-disable-prompt-caching",
            severity="action",
            category="settings",
            archetypes=_ALL_ARCHETYPES,
            title="Prompt caching is disabled by an environment variable",
            action=_action_with_scope(action, scope),
            lever=f"env:{set_names[0]}" if len(set_names) == 1 else "env:DISABLE_PROMPT_CACHING",
            scope=scope,
            evidence=evidence,
            changes=changes,
        )
    ]


def _rule_env_tool_search(
    report: ReportModel, th: RecommendThresholds, snapshot: Snapshot | None, archetype: str | None
) -> list[Recommendation]:
    """COV-09: ``ENABLE_TOOL_SEARCH`` (docs/en/env-vars.md) controls MCP
    tool search -- unset, Claude Code already defers MCP tools by default
    on the Anthropic API, *except* when ``ANTHROPIC_BASE_URL`` points at a
    non-first-party host, where tool search is off by default unless this
    is set to ``true``. The snapshot records names only (never the URL
    value), so this can't tell a first-party override from a proxy --
    the action states that caveat rather than assuming a proxy. Gated on
    the snapshot naming at least ``baseline_bloat_min_mcp_or_plugins``
    MCP servers, since tool search only helps a session that actually
    loads several MCP servers.
    """
    if archetype in _NO_SUBAGENT_ARCHETYPES or snapshot is None:
        return []
    env_names = set(snapshot.data.get("env_names") or [])
    if "ANTHROPIC_BASE_URL" not in env_names or "ENABLE_TOOL_SEARCH" in env_names:
        return []
    mcp_count = len((snapshot.data.get("mcp_servers") or {}).get("names") or [])
    if mcp_count < th.baseline_bloat_min_mcp_or_plugins:
        return []

    scope = _env_lever_scope("ENABLE_TOOL_SEARCH", snapshot)
    action = (
        f"ANTHROPIC_BASE_URL is set and {mcp_count} MCP servers are configured, but "
        "ENABLE_TOOL_SEARCH isn't set. If ANTHROPIC_BASE_URL points at a non-Anthropic proxy or "
        "gateway, Claude Code disables MCP tool search by default there, so every tool's full "
        "schema loads on every turn instead of only the ones a request actually needs. If your "
        "proxy forwards `tool_reference` blocks, set ENABLE_TOOL_SEARCH=true to turn it back on. "
        "If ANTHROPIC_BASE_URL is set to the standard Anthropic endpoint, this doesn't apply -- "
        "this snapshot only records that the variable is set, not what it points at. To undo: "
        "unset ENABLE_TOOL_SEARCH, or remove its `env` entry in settings.json."
    )
    # COV-07/COV-11: the one env-lever rule with a concrete recommended
    # value, so this is the one that gets a real apply --dry-run command.
    change = SettingChange(
        target="settings",
        key="env.ENABLE_TOOL_SEARCH",
        value="true",
        current=None,
        note="Only set this if your proxy actually forwards tool_reference blocks -- a snapshot can't tell.",
        scope=scope,
    )
    return [
        Recommendation(
            id="env-tool-search",
            severity="advice",
            category="settings",
            archetypes=_ALL_ARCHETYPES,
            title="ANTHROPIC_BASE_URL is set without ENABLE_TOOL_SEARCH",
            action=_action_with_scope(action, scope),
            lever="env:ENABLE_TOOL_SEARCH",
            scope=scope,
            evidence=[
                _evidence("ENABLE_TOOL_SEARCH", False, "config", "env-levers", "ENABLE_TOOL_SEARCH"),
            ],
            changes=[change],
        )
    ]


def _rule_env_max_output_tokens(report: ReportModel, snapshot: Snapshot | None) -> list[Recommendation]:
    """COV-09: ``CLAUDE_CODE_MAX_OUTPUT_TOKENS`` (docs/en/env-vars.md)
    raises the output-token cap; the same doc notes this "reduces the
    effective context window available before auto-compaction triggers",
    a direct trade-off worth surfacing whenever it's set. Cites the
    ``compactions`` evidence ``compaction-churn`` uses, when that section
    fired-worthy data exists, to connect the two.
    """
    if snapshot is None:
        return []
    caps = snapshot.data.get("env_numeric_caps")
    value = caps.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS") if isinstance(caps, dict) else None
    if not isinstance(value, int):
        return []

    evidence = [_evidence("CLAUDE_CODE_MAX_OUTPUT_TOKENS", str(value), "config", "env-levers", "CLAUDE_CODE_MAX_OUTPUT_TOKENS")]
    mean_per_session = _cell(report, "compactions", "compactions_summary", "Compactions per session (mean)", "value")
    context_note = ""
    if isinstance(mean_per_session, (int, float)):
        evidence.append(
            _evidence("Compactions per session (mean)", mean_per_session, "compactions", "compactions_summary", "Compactions per session (mean)")
        )
        context_note = f" This corpus already compacts {mean_per_session:.1f} times per session on average."

    scope = _env_lever_scope("CLAUDE_CODE_MAX_OUTPUT_TOKENS", snapshot)
    action = (
        f"CLAUDE_CODE_MAX_OUTPUT_TOKENS is set to {value:,}. Raising it reduces the effective "
        "context window available before auto-compaction triggers, since more of the context "
        f"budget is reserved for a single reply.{context_note} If this was set to work around a "
        "truncated reply rather than as a deliberate trade-off, consider lowering it or unsetting "
        "it (Claude Code then uses the model's own default cap). To undo: unset the variable, or "
        "remove its `env` entry in settings.json."
    )
    # COV-07/COV-11: the recommended value needs judgement (how much
    # lower, or removed outright), so this gets an explainer + prompt but
    # no apply command (same "value=None" convention as env-disable-
    # prompt-caching above) -- ``current`` is the one env lever whose
    # actual number the snapshot keeps (env_numeric_caps), so "Now" can
    # show it exactly rather than just "set".
    change = SettingChange(
        target="settings",
        key="env.CLAUDE_CODE_MAX_OUTPUT_TOKENS",
        current=value,
        suggested="a lower cap, or removed entirely so Claude Code uses the model's own default",
        scope=scope,
    )
    return [
        Recommendation(
            id="env-max-output-tokens",
            severity="advice",
            category="settings",
            archetypes=_ALL_ARCHETYPES,
            title="CLAUDE_CODE_MAX_OUTPUT_TOKENS raises the output cap",
            action=_action_with_scope(action, scope),
            lever="env:CLAUDE_CODE_MAX_OUTPUT_TOKENS",
            scope=scope,
            evidence=evidence,
            changes=[change],
        )
    ]


def _rule_env_subagent_model(report: ReportModel, snapshot: Snapshot | None, archetype: str | None) -> list[Recommendation]:
    """COV-09: ``CLAUDE_CODE_SUBAGENT_MODEL`` (docs/en/sub-agents.md
    "Choose a model") only applies when nothing else assigns a model --
    the per-invocation ``model`` parameter and the agent definition's own
    ``model`` frontmatter (including ``inherit``) both take precedence
    over it (the V17 order), and it never reaches the built-in Explore or
    Plan subagents at all (only ``CLAUDE_CODE_SUBAGENT_MODEL_FORCE``
    does). Presence-only, like every other env lever here.
    """
    if archetype in _NO_SUBAGENT_ARCHETYPES or snapshot is None:
        return []
    env_names = set(snapshot.data.get("env_names") or [])
    if "CLAUDE_CODE_SUBAGENT_MODEL" not in env_names:
        return []

    scope = _env_lever_scope("CLAUDE_CODE_SUBAGENT_MODEL", snapshot)
    action = (
        "CLAUDE_CODE_SUBAGENT_MODEL sets the default model for subagents, agent-team teammates and "
        "workflow agents that aren't otherwise assigned one -- but Claude Code resolves the model in "
        "this order: (1) a model passed for that specific spawn, (2) the subagent definition's own "
        "`model` frontmatter (including `model: inherit`), (3) this variable, (4) the main "
        "conversation's model. An agent file with its own `model:` field, or a spawn that names one "
        "explicitly, ignores this variable entirely. It also never reaches the built-in Explore or "
        "Plan subagents -- only CLAUDE_CODE_SUBAGENT_MODEL_FORCE does. Check your agent files if a "
        "subagent isn't running on the model this variable names. To undo: unset the variable, or "
        "remove its `env` entry in settings.json."
    )
    return [
        Recommendation(
            id="env-subagent-model",
            severity="info",
            category="settings",
            archetypes=_ALL_ARCHETYPES,
            title="CLAUDE_CODE_SUBAGENT_MODEL is set",
            action=_action_with_scope(action, scope),
            lever="env:CLAUDE_CODE_SUBAGENT_MODEL",
            scope=scope,
            evidence=[
                _evidence("CLAUDE_CODE_SUBAGENT_MODEL", True, "config", "env-levers", "CLAUDE_CODE_SUBAGENT_MODEL"),
            ],
        )
    ]


def _rule_attribution_deprecated(report: ReportModel, snapshot: Snapshot | None) -> list[Recommendation]:
    """COV-09: ``includeCoAuthoredBy`` (settings.json) was replaced by
    ``attribution`` (docs/en/settings-reference.md: "Deprecated since
    v2.0.62 ... Claude Code still reads it, but new configurations should
    set `attribution`"). Fires only when ``includeCoAuthoredBy`` is set
    *and* ``attribution`` is not -- once ``attribution.commit``/``.pr`` is
    set, Claude Code ignores ``includeCoAuthoredBy`` outright (same doc),
    so there is nothing to migrate once that's already happened.
    """
    if snapshot is None:
        return []
    effective = snapshot.data.get("effective")
    if not isinstance(effective, dict) or "includeCoAuthoredBy" not in effective or "attribution" in effective:
        return []

    # COV-01: a real settings.json key (unlike its four sibling rules
    # above), so this uses _lever_scope like every other plain-key rule
    # rather than the managed-only check the other four env names need
    # _env_lever_scope for.
    _lever, scope = _lever_scope("includeCoAuthoredBy", snapshot)
    action = (
        "includeCoAuthoredBy is set in settings.json. Claude Code still honours it, but "
        "`attribution` (added in v2.0.62) replaces it and can also change or hide the pull-request "
        "attribution text and the session link separately, not just the commit trailer. Ask Claude "
        "to translate your `includeCoAuthoredBy` value into an equivalent `attribution.commit` "
        "setting (`false` becomes an empty commit trailer) and show you the diff before saving. To "
        "undo: remove the `attribution` key, `includeCoAuthoredBy` (still valid) takes over again."
    )
    return [
        Recommendation(
            id="env-attribution-deprecated",
            severity="info",
            category="settings",
            archetypes=_ALL_ARCHETYPES,
            title="includeCoAuthoredBy is deprecated in favour of attribution",
            action=_action_with_scope(action, scope),
            lever="includeCoAuthoredBy",
            scope=scope,
            evidence=[
                _evidence(
                    "includeCoAuthoredBy",
                    str(effective.get("includeCoAuthoredBy")),
                    "config",
                    "env-levers",
                    "includeCoAuthoredBy",
                ),
            ],
        )
    ]


def _rule_agent_report_size(report: ReportModel, th: RecommendThresholds, archetype: str | None) -> list[Recommendation]:
    if archetype in _NO_SUBAGENT_ARCHETYPES:
        return []
    table = _table(report, "agents", "topology_report_proxy")
    if table is None:
        return []
    spawns_idx = _col_index(table, "spawns")
    out: list[Recommendation] = []
    for row in table.rows:
        agent_type = row[0]
        mean_proxy = _cell(report, "agents", "topology_report_proxy", agent_type, "mean_proxy")
        if not isinstance(mean_proxy, (int, float)) or mean_proxy <= th.agent_report_size_tokens:
            continue
        # Fix R3: topology_report_proxy has no priced_turns column of
        # its own; cross-reference ttl_by_agent_type's for the same
        # agent type (None when absent -- see _row_meets_min_sample).
        spawns = row[spawns_idx] if spawns_idx is not None and spawns_idx < len(row) else None
        priced_turns = _cell(report, "ttl", "ttl_by_agent_type", agent_type, "priced_turns")
        if not _row_meets_min_sample(th, spawns, priced_turns):
            continue
        out.append(
            Recommendation(
                id="agent-report-size",
                severity="advice",
                category="workflow",
                archetypes=_ALL_ARCHETYPES,
                title=f"{agent_type}'s reports come back large",
                action=(
                    f"Ask {agent_type} to report back more concisely -- a shorter report "
                    "costs less to fold into the parent's cache."
                ),
                lever=None,
                agent_type=agent_type,
                evidence=[
                    _evidence("Mean report size (tokens)", mean_proxy, "agents", "topology_report_proxy", agent_type),
                ],
            )
        )
    return out


#: The columns of ``habits_probes`` the rule reads, in the order it uses them.
_PROBE_COLUMNS = ("calls", "probes", "shell", "runs", "batch_cost")


def _rule_agent_batch_probes(report: ReportModel, th: RecommendThresholds, archetype: str | None) -> list[Recommendation]:
    """``agent-batch-probes``: an agent type whose replies are often one
    read-only call and nothing else (a Read, a Grep, a Glob or a file-reading
    shell command). Each such reply reads the agent's whole context again to
    look at one thing, so independent lookups in one message would have cost
    one read instead of several. Reads the Work habits section's
    ``habits_probes``, a row for each agent type; the main session's row is
    left out, since its prompts are yours and the card is for agent
    definitions and workflow prompts. ``saving_usd`` is
    :attr:`~RecommendThresholds.agent_batch_probes_saving_factor` of the
    row's ``batch_cost``, the cache reads of the replies after the first of
    each run: the table's figure is an upper bound, and what a batched
    message still writes is not in it."""
    if archetype in _NO_SUBAGENT_ARCHETYPES:
        return []
    table = _table(report, "habits", "habits_probes")
    if table is None:
        return []
    at = {key: _col_index(table, key) for key in _PROBE_COLUMNS}
    if None in at.values():
        return []
    out: list[Recommendation] = []
    for row in table.rows:
        agent_type = row[0] if row else None
        if not isinstance(agent_type, str) or agent_type == "top-level" or len(row) <= max(at.values()):
            continue
        calls, probes, shell, runs, cost = (row[at[key]] for key in _PROBE_COLUMNS)
        if not all(_is_number(value) for value in (calls, probes, shell, runs, cost)):
            continue
        if calls < th.agent_batch_probes_min_replies or probes * 100 < calls * th.agent_batch_probes_share_pct:
            continue
        saving = cost * th.agent_batch_probes_saving_factor
        if saving < th.agent_batch_probes_min_saving_usd:
            continue
        out.append(
            Recommendation(
                id="agent-batch-probes",
                severity="advice",
                category="workflow",
                archetypes=_ALL_ARCHETYPES,
                title=f"{agent_type} looks things up one call at a time",
                action=(
                    f"Add \"{BATCH_PROBES_LINE}\" to {agent_type}'s agent definition, or to the workflow "
                    "prompt that starts it, so independent lookups share one message."
                ),
                lever=None,
                agent_type=agent_type,
                evidence=[
                    _evidence("Replies it made", calls, "habits", "habits_probes", agent_type),
                    _evidence("Single read-only calls", probes, "habits", "habits_probes", agent_type),
                    _evidence("Of them by shell command", shell, "habits", "habits_probes", agent_type),
                    _evidence("Re-reads a batch would spare", cost, "habits", "habits_probes", agent_type),
                ],
                saving_usd=saving,
            )
        )
    return out


#: The columns of ``habits_plan_rounds`` the rule reads, in the order it uses them.
_PLAN_ROUND_COLUMNS = ("plans", "rounds", "asked", "cost")


def _rule_plan_rounds(report: ReportModel, th: RecommendThresholds) -> list[Recommendation]:
    """``plan-rounds``: plans you sent back before you approved one. Each
    round is a reply that reads the whole planning conversation again, and
    most were a question, a critique or a doubt that Claude could be asked to
    raise against its own plan before it shows you. Reads the Work habits
    section's ``habits_plan_rounds``: the ``all`` row for the totals and the
    ``none`` row for the plans never sent back. A plan you declined
    and then told Claude to carry out is an approval, not one sent back.
    ``saving_usd`` is :attr:`~RecommendThresholds.plan_rounds_saving_factor`
    of the cost of the replies between the first plan and the approval,
    scaled to the share of rounds that were a question, a critique or a
    doubt: the table's figure is an upper bound."""
    table = _table(report, "habits", "habits_plan_rounds")
    if table is None:
        return []
    at = {key: _col_index(table, key) for key in _PLAN_ROUND_COLUMNS}
    if None in at.values():
        return []
    rows = {row[0]: row for row in table.rows if row and isinstance(row[0], str)}
    total = rows.get("all")
    if total is None or len(total) <= max(at.values()):
        return []
    plans, rounds, asked, cost = (total[at[key]] for key in _PLAN_ROUND_COLUMNS)
    if not all(_is_number(value) for value in (plans, rounds, asked, cost)):
        return []
    first = rows.get("none")
    at_once = first[at["plans"]] if first is not None and len(first) > at["plans"] and _is_number(first[at["plans"]]) else 0
    sent_back = plans - at_once
    if plans < th.plan_rounds_min_plans or rounds <= 0:
        return []
    if sent_back * 100 < plans * th.plan_rounds_min_share_pct or asked * 100 < rounds * th.plan_rounds_min_asked_pct:
        return []
    saving = cost * (asked / rounds) * th.plan_rounds_saving_factor
    if saving < th.plan_rounds_min_saving_usd:
        return []
    return [
        Recommendation(
            id="plan-rounds",
            severity="advice",
            category="workflow",
            archetypes=_ALL_ARCHETYPES,
            title="Plans keep being sent back",
            action=(
                "Put one standing request in your first planning message, in CLAUDE.md or in a plan skill. "
                f"It reads \"{CRITIQUE_PLAN_LINE}.\""
            ),
            lever=None,
            evidence=[
                _evidence("Plans you approved", plans, "habits", "habits_plan_rounds", "all"),
                _evidence("Never sent back", at_once, "habits", "habits_plan_rounds", "none"),
                _evidence("Times plans were sent back", rounds, "habits", "habits_plan_rounds", "all"),
                _evidence("Sent back with a question or critique", asked, "habits", "habits_plan_rounds", "all"),
                _evidence("Replies between the first plan and approval", cost, "habits", "habits_plan_rounds", "all"),
            ],
            saving_usd=saving,
        )
    ]


# -- subagent startup, part by part -------------------------------------------

#: Built-in agent types that Claude Code already starts without CLAUDE.md
#: (its sub-agents docs), so omitClaudeMd means nothing for them.
_SKIPS_CLAUDE_MD = frozenset({"Explore", "Plan"})

#: Agent types started by Claude Code itself (workflow scripts, forks)
#: rather than by name, so no agent file can override them.
_NOT_OVERRIDABLE = frozenset({"workflow-subagent", "fork", "unknown", "(unknown)"})

#: The agent type every workflow script's agents run as, unless the script
#: names one: no agent file is its own, so its tools-list card is advice
#: for the script (``agentType`` on its ``agent()`` calls), not a change.
_WORKFLOW_AGENT = "workflow-subagent"

#: ``SettingChange.current`` when there is no config snapshot to read it from.
_CURRENT_UNKNOWN = "unknown (no config snapshot yet)"


def _agent_source(agent_type: str, snapshot: Snapshot | None) -> str | None:
    """``"user"`` or ``"project"``: which agents directory the agent's
    file lives in, per the latest config snapshot (``None`` when unknown
    or when it is a built-in agent type with no file)."""
    if snapshot is None:
        return None
    agents_map = snapshot.data.get("agents")
    entry = agents_map.get(agent_type) if isinstance(agents_map, dict) else None
    source = entry.get("source") if isinstance(entry, dict) else None
    return source if source in ("user", "project") else None


def _agent_current(agent_type: str, key: str, snapshot: Snapshot | None):
    if snapshot is None:
        return _CURRENT_UNKNOWN
    agents_map = snapshot.data.get("agents")
    entry = agents_map.get(agent_type) if isinstance(agents_map, dict) else None
    return entry.get(key) if isinstance(entry, dict) else None


def _startup_saving(units: "Units | None", usd, *, prefix: str = "") -> str:
    """``usd``, a list-price amount over the spawns in this report,
    phrased for the billing mode, or ``""`` when there is none."""
    if units is None or not isinstance(usd, (int, float)) or not usd > 0:
        return ""
    amount = units.money(usd, period="across the spawns in this report")
    if amount is None:
        return ""
    text = f"{prefix}{amount.text()}."
    return text[:1].upper() + text[1:]


def _startup_basis(units: "Units | None", *, carried: bool = True) -> str:
    """How an amount from :func:`_startup_saving` was worked out:
    ``carried`` when it counts the cache reads on the calls after a
    spawn's first, not only the write at the start."""
    if units is None:
        return ""
    if carried:
        basis = (
            "Estimated at list prices: each spawn writes these tokens into the cache, and every later call "
            "reads them back."
        )
    else:
        basis = "Estimated at the list price of writing these tokens into the cache once per spawn."
    probe = units.money(1.0)
    if probe is not None and probe.basis == NO_LIMIT_SHARE_HINT:
        basis += " " + probe.basis
    return basis


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _body_usd(tokens, spawns, write, read, later) -> tuple[float | None, bool]:
    """List-price USD of ``tokens`` that each of ``spawns`` spawns writes
    into the cache at startup and then reads on every later call (the
    ``agent_startup_breakdown`` row's write price, read price and calls
    after the first), and whether the reads were counted. Without a read
    price or a count of later calls, only the write is."""
    if not (_is_number(tokens) and _is_number(spawns) and _is_number(write)) or min(tokens, spawns, write) <= 0:
        return None, False
    counted = _is_number(read) and read > 0 and _is_number(later) and later > 0
    usd = diet_usd(
        spawns,
        0.0,
        tokens,
        write=write,
        read=read if counted else None,
        later_calls=later if counted else 0.0,
        written_share=1.0,
    )
    return (usd or None), counted


def _row_values(report: ReportModel, section_key: str, table_name: str, row_key) -> dict | None:
    """The first row of the table whose first cell is ``row_key``, as
    ``{column key: cell}``; ``None`` when the table or the row is missing."""
    table = _table(report, section_key, table_name)
    row = _row(table, row_key) if table is not None else None
    if row is None:
        return None
    return {column.key: row[i] for i, column in enumerate(table.columns) if i < len(row)}


def _per_30_days(usd, days) -> float | None:
    """``usd`` over a window of ``days`` as a 30-day amount. A window
    shorter than :data:`MIN_WINDOW_DAYS` is stretched to it, so a day or
    two of data never reads as a rate."""
    if not _is_number(usd) or not usd > 0:
        return None
    span = days if _is_number(days) and days > 0 else 30.0
    return usd * 30.0 / max(span, float(MIN_WINDOW_DAYS))


def _split_names(text) -> list[str]:
    return [name for name in str(text or "").split(", ") if name]


#: Tools a tools-list card names as left out, before "and N more".
_TOOLS_SHOWN = 6

#: What ``agent_startup_diet`` takes out of a start, as the card words it:
#: column, then what it is.
_DIET_PARTS = (
    ("dropped_definitions", "tool definitions"),
    ("dropped_deferred", "deferred tool names"),
    ("dropped_skills", "the skills list"),
    ("dropped_roster", "the agent list"),
)

_GENERAL_PURPOSE_WARNING = (
    "general-purpose is the agent Claude Code starts when a task names no type, so a tools list on it limits "
    "every one of those spawns, not only the ones measured here. Keep any tool you want them to have."
)


def _name_list(names: list[str], shown: int = _TOOLS_SHOWN) -> str:
    head = ", ".join(names[:shown])
    extra = len(names) - shown
    return f"{head}, and {extra} more" if extra > 0 else head


def tools_list_offer(
    agent_type: str, diet: dict, th: RecommendThresholds | None = None
) -> tuple[list[str], list[str], float] | None:
    """The tools to keep, the tools to leave off and the tokens that
    leaving them off takes from each start, for one ``agent_startup_diet``
    row (as a dict by column), or ``None`` when no ``spawn-tools-list`` card
    is worth giving: an agent the diet leaves alone, one with no file to
    give a list (a fork or an unknown type), too few spawns, no tool called
    to put on the line, or less than
    :attr:`RecommendThresholds.spawn_tools_list_tokens` to take off it."""
    th = th or RecommendThresholds()
    if agent_type in DIET_EXCLUDED or (agent_type in _NOT_OVERRIDABLE and agent_type != _WORKFLOW_AGENT):
        return None
    measured = diet.get("spawns")
    if not isinstance(measured, int) or measured < th.spawn_parts_min_spawns:
        return None
    keep = _split_names(diet.get("keep_tools"))
    rare = _split_names(diet.get("rare_tools"))
    dropped = sum(diet[column] for column, _label in _DIET_PARTS if _is_number(diet.get(column)))
    if not keep or not rare or dropped < th.spawn_tools_list_tokens:
        return None
    return keep, rare, dropped


def _tools_list_card(
    report: ReportModel,
    th: RecommendThresholds,
    agent_type: str,
    snapshot: Snapshot | None,
    units: "Units | None",
) -> Recommendation | None:
    """``spawn-tools-list``: a ``tools:`` line for one agent type, built
    from the tools at least a tenth of its spawns called, with the rest
    named as rarely used. Needs the ``agent_startup_diet`` row, which
    carries what leaving them out takes from a start."""
    diet = _row_values(report, "agent_startup", "agent_startup_diet", agent_type)
    offer = tools_list_offer(agent_type, diet, th) if diet is not None else None
    if offer is None:
        return None
    keep, rare, dropped = offer
    measured = diet["spawns"]
    sizes = [(label, diet.get(column)) for column, label in _DIET_PARTS]

    workflow = agent_type == _WORKFLOW_AGENT
    has_file = _agent_has_frontmatter(agent_type, snapshot) and not workflow
    source = _agent_source(agent_type, snapshot)
    scope = "user" if workflow else ("repo" if source == "project" or (has_file and source is None) else "user")
    general = agent_type == "general-purpose"
    pct = int(REMOVABLE_USE_SHARE * 100)
    line = "tools: " + ", ".join(keep)
    model = diet.get("model")
    check = (
        "Check it with one spawn before and one after on the same model"
        f"{f' ({model})' if model else ''}, comparing their startup size."
    )
    left_out = (
        f"Rarely used, so left out (each was called in fewer than {pct}% of the spawns offered it): "
        f"{_name_list(rare)}. Claude Code adds StructuredOutput and SubagentHandback to every subagent "
        "whatever the list says."
    )
    if workflow:
        title = "Workflow agents are given tools they rarely call"
        action = (
            "In each workflow script that starts agents, pass agentType with the name of an agent whose file "
            f"sets this line: {line}. {left_out} Workflow agents do different jobs, so give each kind of job "
            f"its own agent file and trim its list to what that job needs. {check}"
        )
        category = "workflow"
    else:
        title = f"{agent_type} is given tools it rarely calls"
        action = f"Limit {agent_type}'s tools to the ones it calls: {line}. {left_out} {check}"
        category = "settings"
        if general:
            action += " " + _GENERAL_PURPOSE_WARNING

    why_parts = [f"{value:,.0f} of {label}" for label, value in sizes if _is_number(value) and value >= 1]
    why = (
        f"In {measured} spawns, {agent_type} called {len(keep)} of the tools it was offered in at least "
        f"{pct}% of them. Leaving out the rest takes about {dropped:,.0f} tokens out of each start: "
        f"{', '.join(why_parts)}."
    )
    evidence = [_evidence("Spawns with tools recorded", measured, "agent_startup", "agent_startup_diet", agent_type)]
    definitions = diet.get("dropped_definitions")
    if _is_number(definitions):
        evidence.append(
            _evidence("Tool definitions left out per spawn", definitions, "agent_startup", "agent_startup_diet", agent_type)
        )
    usd = diet.get("saving_usd")
    usd = float(usd) if _is_number(usd) and usd > 0 else None
    if usd is not None:
        evidence.append(
            _evidence("Saving across these spawns", diet["saving_usd"], "agent_startup", "agent_startup_diet", agent_type)
        )
    basis = (
        _startup_basis(units)
        + " Tool definitions are shared by spawns of one type, so only the spawns that wrote them pay the "
        "write price for them."
    )
    if any(_is_number(diet.get(column)) and diet[column] >= 1 for column in ("dropped_skills", "dropped_roster")):
        basis += (
            " The skills list and the agent list are counted as going with the Skill and Agent tools. Claude "
            "Code's docs don't say so, so the check above is what confirms it."
        )
    kept_instructions = diet.get("kept_instructions")
    if _is_number(kept_instructions) and kept_instructions >= 1:
        basis += (
            f" A tools list leaves MCP server instructions in (about {kept_instructions:,.0f} tokens per spawn). "
            "Switching the server off drops them. A list of the servers an agent may use might too, but that isn't verified."
        )
    changes: list[SettingChange] = []
    if not workflow:
        changes.append(
            SettingChange(
                target="agent",
                key="tools",
                agent=agent_type,
                value=keep,
                current=_agent_current(agent_type, "tools", snapshot),
                new_agent_file=not has_file,
                note=_GENERAL_PURPOSE_WARNING if general else "An MCP server appears as mcp__server__*, meaning all of its tools.",
            )
        )
    return Recommendation(
        id="spawn-tools-list",
        severity="advice",
        category=category,
        archetypes=_ALL_ARCHETYPES,
        title=title,
        action=action,
        lever=None,
        scope=scope,
        agent_type=agent_type,
        evidence=evidence,
        changes=changes,
        estimated_saving=_startup_saving(units, usd, prefix="about "),
        saving_basis=basis if usd is not None else "",
        saving_usd=usd,
        why=why,
        variant="workflow-script" if workflow else "",
    )


def _rule_spawn_parts(
    report: ReportModel,
    th: RecommendThresholds,
    archetype: str | None,
    snapshot: Snapshot | None,
    units: "Units | None",
) -> tuple[list[Recommendation], set[str]]:
    """One recommendation per large or unused part of what a subagent is
    given at startup (the ``agent_startup`` section), instead of one
    generic "spawning is expensive". Returns the recommendations and the
    agent types they cover, so :func:`_rule_spawn_cost` only falls back
    to its generic advice for the rest."""
    if archetype in _NO_SUBAGENT_ARCHETYPES:
        return [], set()
    breakdown = _table(report, "agent_startup", "agent_startup_breakdown")
    unused = _table(report, "agent_startup", "agent_startup_unused")
    if breakdown is None or unused is None:
        return [], set()

    def part(agent_type, column):
        return _cell(report, "agent_startup", "agent_startup_breakdown", agent_type, column)

    def used(agent_type, column):
        return _cell(report, "agent_startup", "agent_startup_unused", agent_type, column)

    whatif_tables = whatif._Tables(report)
    context_files = getattr(report, "context_files", None) or {}
    servers = _table(report, "agent_startup", "agent_startup_servers")
    server_keys = [column.key for column in servers.columns] if servers is not None else []

    out: list[Recommendation] = []
    covered: set[str] = set()
    for row in breakdown.rows:
        agent_type = row[0]
        spawns = used(agent_type, "spawns")
        if not isinstance(spawns, int) or spawns < th.spawn_parts_min_spawns:
            continue
        overridable = agent_type not in _NOT_OVERRIDABLE
        price = part(agent_type, "write_price")
        read_price = part(agent_type, "read_price")
        later_calls = part(agent_type, "later_calls")
        has_file = _agent_has_frontmatter(agent_type, snapshot)
        source = _agent_source(agent_type, snapshot)
        scope = "repo" if source == "project" or (has_file and source is None) else "user"
        spawns_evidence = _evidence("Spawns measured", spawns, "agent_startup", "agent_startup_unused", agent_type)
        read_only = used(agent_type, "read_only_spawns")
        all_read_only = isinstance(read_only, int) and read_only == spawns

        # The tools list comes first: the skills list and MCP cards below
        # point at it, and the generic spawn-cost card leaves the agent alone
        # once it is given.
        tools_card = _tools_list_card(report, th, agent_type, snapshot, units)
        if tools_card is not None:
            out.append(tools_card)
            covered.add(agent_type)
        diet_rare = _split_names(_cell(report, "agent_startup", "agent_startup_diet", agent_type, "rare_tools"))

        claude_md_total = part(agent_type, "claude_md")
        # PROF-11/F13: Managed policy CLAUDE.md still loads regardless of
        # omitClaudeMd, so it never counts towards what this would save.
        claude_md_managed = part(agent_type, "claude_md_managed") or 0.0
        claude_md = (
            max(0.0, claude_md_total - claude_md_managed)
            if isinstance(claude_md_total, (int, float))
            else claude_md_total
        )
        # Metrics capture: whether its runs said they used CLAUDE.md. Held
        # back when most that said, said they did.
        rules_used = _cell(report, "habits", "habits_agents", agent_type, "rules_used") or 0
        rules_unused = _cell(report, "habits", "habits_agents", agent_type, "rules_unused") or 0
        if (
            isinstance(claude_md, (int, float))
            and claude_md >= th.spawn_part_tokens
            and agent_type not in _SKIPS_CLAUDE_MD
            and overridable
            and rules_used <= rules_unused
        ):
            rules_evidence = []
            why = f"Each {agent_type} spawn starts with about {claude_md:,.0f} tokens of CLAUDE.md files and memory."
            if all_read_only:
                why += " Every measured spawn only searched or read files, so it rarely needs your working rules."
            if claude_md_managed:
                why += f" Managed policy CLAUDE.md ({claude_md_managed:,.0f} tokens) still loads either way."
                rules_evidence = [
                    _evidence("Managed policy CLAUDE.md per spawn (still loads)", claude_md_managed, "agent_startup", "agent_startup_breakdown", agent_type)
                ]
            if rules_unused:
                why += (
                    f" {rules_unused} of the {rules_used + rules_unused} runs that said, said they didn't use "
                    "your CLAUDE.md."
                )
                rules_evidence.append(
                    _evidence("Runs that said they didn't use CLAUDE.md", rules_unused, "habits", "habits_agents", agent_type)
                )
            # The same figure the What-if table gives omitClaudeMd: the cache
            # write at each spawn, or what the Context files section prices
            # carrying CLAUDE.md across the rest of each spawn, if that is more.
            omitted = whatif._omit_claude_md(whatif_tables, context_files, agent_type, agent_type)
            claude_md_usd = omitted.get("saving_usd")
            carry_usd = whatif._claude_md_carry_usd(context_files, agent_type)
            carried = _is_number(claude_md_usd) and carry_usd > 0 and abs(claude_md_usd - carry_usd) < 1e-5
            out.append(
                Recommendation(
                    id="spawn-claude-md",
                    severity="advice",
                    category="settings",
                    archetypes=_ALL_ARCHETYPES,
                    title=f"{agent_type} is sent your CLAUDE.md files every time it starts",
                    action=(
                        f"Move the CLAUDE.md rules {agent_type} needs into its agent file, then stop "
                        "sending it CLAUDE.md (omitClaudeMd: true)."
                        if has_file
                        else f"{agent_type} is a built-in agent, so it has no file to change. Create a "
                        f"same-named agent file that does the same job with omitClaudeMd: true; it replaces "
                        "the built-in one."
                    ),
                    lever="omitClaudeMd" if has_file else None,
                    scope=scope,
                    agent_type=agent_type,
                    evidence=[
                        _evidence("CLAUDE.md and memory per spawn", claude_md, "agent_startup", "agent_startup_breakdown", agent_type),
                        spawns_evidence,
                        *rules_evidence,
                    ],
                    changes=[
                        SettingChange(
                            target="agent",
                            key="omitClaudeMd",
                            agent=agent_type,
                            value=True,
                            current=_agent_current(agent_type, "omitClaudeMd", snapshot),
                            new_agent_file=not has_file,
                        )
                    ],
                    estimated_saving=_startup_saving(units, claude_md_usd),
                    saving_basis=(
                        _startup_basis(units, carried=carried)
                        + (" It is priced as the Context files section prices carrying it." if carried else "")
                        if units is not None and _is_number(claude_md_usd)
                        else ""
                    ),
                    why=why,
                )
            )
            covered.add(agent_type)

        skills_tokens = part(agent_type, "skills_listing")
        listed = used(agent_type, "skills_listed_spawns")
        skills_used = used(agent_type, "skills_used_spawns")
        if (
            isinstance(skills_tokens, (int, float))
            and skills_tokens >= th.spawn_unused_part_tokens
            and isinstance(listed, int)
            and listed >= th.spawn_parts_min_spawns
            and skills_used == 0
            and overridable
        ):
            # Whether the tools list above leaves Skill out, which takes the
            # skills list with it: this card is then a part of that saving.
            in_tools_list = tools_card is not None and SKILLS_TOOL in diet_rare
            skills_usd, skills_carried = _body_usd(skills_tokens, listed, price, read_price, later_calls)
            undocumented = (
                " Whether that also drops the skills list from its startup isn't documented, so check the "
                "Skills list column after a few new spawns."
            )
            out.append(
                Recommendation(
                    id="spawn-unused-skills",
                    severity="info",
                    category="settings",
                    archetypes=_ALL_ARCHETYPES,
                    title=f"{agent_type} is given the skills list but never used a skill",
                    action=(
                        f"Leave Skill off the tools list for {agent_type}, which stops it being sent the skills "
                        "list. To change nothing else, add Skill to its disallowedTools instead."
                        if in_tools_list
                        else f"Give {agent_type} a tools list that leaves Skill out, or, to change nothing else, "
                        "add Skill to its disallowedTools."
                    )
                    + undocumented,
                    lever=None,
                    scope=scope,
                    agent_type=agent_type,
                    evidence=[
                        _evidence("Skills list per spawn", skills_tokens, "agent_startup", "agent_startup_breakdown", agent_type),
                        _evidence("Spawns given the skills list", listed, "agent_startup", "agent_startup_unused", agent_type),
                        _evidence("Spawns that used a skill", skills_used, "agent_startup", "agent_startup_unused", agent_type),
                    ],
                    changes=[
                        SettingChange(
                            target="agent",
                            key="disallowedTools",
                            agent=agent_type,
                            # A new file has nothing to keep; an existing one may
                            # already list tools, which a plain --set would drop.
                            value=None if has_file else ["Skill"],
                            suggested="add Skill to the list, keeping anything already there",
                            current=_agent_current(agent_type, "disallowedTools", snapshot),
                            new_agent_file=not has_file,
                            unconfirmed=True,
                        )
                    ],
                    estimated_saving=_startup_saving(units, skills_usd, prefix="if the list is dropped, about "),
                    saving_basis=(
                        _startup_basis(units, carried=skills_carried)
                        + (" It is part of the tools list saving, not on top of it." if in_tools_list else "")
                        if units is not None and skills_usd is not None
                        else ""
                    ),
                    why=f"In {listed} spawns given the skills list, {agent_type} never called a skill.",
                )
            )
            covered.add(agent_type)

        # One card per agent type, naming each server it was offered and
        # hardly ever called. A server is named when at most
        # ``spawn_unused_mcp_use_share_pct`` of the spawns offered it called
        # it and it costs ``spawn_unused_mcp_min_usd_30d`` over 30 days.
        offered = used(agent_type, "mcp_offered_spawns")
        mcp_used = used(agent_type, "mcp_used_spawns")
        unused_servers: list[tuple[str, int, int, float, float]] = []
        first_server_row: dict | None = None
        for server_row in servers.rows if servers is not None and overridable else []:
            if not server_row or server_row[0] != agent_type:
                continue
            values = dict(zip(server_keys, server_row))
            if first_server_row is None:
                first_server_row = values
            server_offered, server_used = values.get("offered_spawns"), values.get("used_spawns")
            if not (isinstance(server_offered, int) and isinstance(server_used, int)):
                continue
            if server_offered < th.spawn_parts_min_spawns:
                continue
            if server_used * 100 > th.spawn_unused_mcp_use_share_pct * server_offered + 1e-9:
                continue
            month = _per_30_days(values.get("saving_usd"), values.get("window_days"))
            if month is None or month < th.spawn_unused_mcp_min_usd_30d:
                continue
            unused_servers.append((str(values.get("server")), server_offered, server_used, values["saving_usd"], month))
        if unused_servers and isinstance(offered, int) and isinstance(mcp_used, int):
            names = [f"{name} (used in {n_used} of {n_offered} spawns)" for name, n_offered, n_used, _u, _m in unused_servers]
            total_usd = sum(window_usd for _n, _o, _u, window_usd, _m in unused_servers)
            costs = ""
            if units is not None:
                costs = " Across the spawns in this report they cost " + "; ".join(
                    f"{units.money_text(window_usd)} ({name})"
                    for name, _o, _u, window_usd, _m in unused_servers[:_TOOLS_SHOWN]
                ) + "."
            mcp_evidence = [
                _evidence("Spawns offered MCP tools", offered, "agent_startup", "agent_startup_unused", agent_type),
                _evidence("Spawns that used an MCP tool", mcp_used, "agent_startup", "agent_startup_unused", agent_type),
            ]
            if first_server_row is not None and str(first_server_row.get("server")) == unused_servers[0][0]:
                first = unused_servers[0]
                mcp_evidence += [
                    _evidence(f"Spawns offered {first[0]}", first[1], "agent_startup", "agent_startup_servers", agent_type),
                    _evidence(f"Spawns that used {first[0]}", first[2], "agent_startup", "agent_startup_servers", agent_type),
                ]
            out.append(
                Recommendation(
                    id="spawn-unused-mcp",
                    severity="info",
                    category="settings",
                    archetypes=_ALL_ARCHETYPES,
                    title=f"{agent_type} is offered MCP servers it hardly ever uses",
                    action=(
                        f"Give {agent_type} an mcpServers list naming only the servers it needs, so these "
                        f"aren't loaded for it: {_name_list(names)}."
                    ),
                    lever=None,
                    scope=scope,
                    agent_type=agent_type,
                    evidence=mcp_evidence,
                    changes=[
                        SettingChange(
                            target="agent",
                            key="mcpServers",
                            agent=agent_type,
                            suggested="only the servers this agent needs",
                            current=_agent_current(agent_type, "mcpServers", snapshot),
                            new_agent_file=not has_file,
                            unconfirmed=True,
                            note=(
                                "ClaudeGlass doesn't record which servers an agent's mcpServers lists, so check "
                                "the agent file for one before you change it."
                            ),
                        )
                    ],
                    estimated_saving=_startup_saving(units, total_usd, prefix="about "),
                    saving_basis=(
                        _startup_basis(units)
                        + " Each server counts its tool definitions and deferred tool names. Its instructions stay under "
                        "a tools list and aren't counted. It is the same saving as the "
                        "tools list card, taken a server at a time, so it isn't added to it."
                        if units is not None
                        else ""
                    ),
                    why=(
                        f"At most {th.spawn_unused_mcp_use_share_pct:g}% of the spawns offered each of these "
                        f"servers called it.{costs}"
                    ),
                )
            )
            covered.add(agent_type)

        task_prompt = part(agent_type, "task_prompt")
        if isinstance(task_prompt, (int, float)) and task_prompt >= th.spawn_task_prompt_tokens:
            prompt_usd, prompt_carried = _body_usd(task_prompt / 2, spawns, price, read_price, later_calls)
            out.append(
                Recommendation(
                    id="spawn-task-prompt",
                    severity="info",
                    category="workflow",
                    archetypes=_ALL_ARCHETYPES,
                    title=f"The instructions written for each {agent_type} are long",
                    action=(
                        f"When starting {agent_type}, point it at files instead of pasting their contents, "
                        "and leave out background it can look up itself."
                    ),
                    lever=None,
                    scope="user",
                    agent_type=agent_type,
                    evidence=[
                        _evidence("Task prompt per spawn", task_prompt, "agent_startup", "agent_startup_breakdown", agent_type),
                        spawns_evidence,
                    ],
                    estimated_saving=_startup_saving(units, prompt_usd, prefix="if they were half as long, about "),
                    saving_basis=(
                        _startup_basis(units, carried=prompt_carried) if units is not None and prompt_usd else ""
                    ),
                    why=f"Each {agent_type} spawn starts with about {task_prompt:,.0f} tokens of instructions from its parent.",
                )
            )
            covered.add(agent_type)

    shared = _table(report, "agent_startup", "agent_startup_shared")
    for row in shared.rows if shared is not None else []:
        key, source_label, reach, mean_tokens = row[0], row[1], row[2], row[3]
        if not str(key).startswith("claude_md:") or not isinstance(mean_tokens, (int, float)):
            continue
        if mean_tokens < th.spawn_part_tokens:
            continue
        out.append(
            Recommendation(
                id="spawn-shared-claude-md",
                severity="advice",
                category="workflow",
                archetypes=_ALL_ARCHETYPES,
                title=f"{source_label} goes to {reach} agent types",
                action=(
                    "Ask Claude to move sections that only some agents need out of this file and into "
                    "those agents' own files or into skills loaded on demand. Every spawn then carries less."
                ),
                lever=None,
                scope="user",
                evidence=[
                    _evidence("Size per spawn", mean_tokens, "agent_startup", "agent_startup_shared", key),
                    _evidence("Agent types given it", reach, "agent_startup", "agent_startup_shared", key),
                ],
                why=f"About {mean_tokens:,.0f} tokens of it are sent to each of these agents when they start.",
            )
        )
    return out, covered


def _rule_spawn_cost(
    report: ReportModel,
    th: RecommendThresholds,
    archetype: str | None,
    snapshot: Snapshot | None,
    covered: set[str] | None = None,
) -> list[Recommendation]:
    if archetype in _NO_SUBAGENT_ARCHETYPES:
        return []
    table = _table(report, "agents", "topology_spawn_write")
    if table is None:
        return []
    spawns_idx = _col_index(table, "spawns")
    out: list[Recommendation] = []
    for row in table.rows:
        agent_type = row[0]
        if covered and agent_type in covered:
            continue
        # Explore, Plan and claude-code-guide are Claude Code's own read-only
        # helpers, started with a small tool set of their own: the tools-list
        # card leaves them alone, so a built-in one with no file of the
        # user's has no lever here either.
        has_file = _agent_has_frontmatter(agent_type, snapshot)
        if not has_file and agent_type in DIET_EXCLUDED:
            continue
        # Gated on the whole first call and on the part of it a tools list
        # on the agent would take out (what its snapshot offered and it
        # rarely or never used), not on the cache write: that write is the
        # task prompt and CLAUDE.md, which the spawn-claude-md and
        # spawn-task-prompt rules read part by part. An agent type the
        # spawn-tools-list card covers is left to it.
        mean_first_call = _cell(report, "agents", "topology_spawn_write", agent_type, "mean_first_call")
        if not isinstance(mean_first_call, (int, float)) or mean_first_call <= th.spawn_cost_tokens:
            continue
        removable = _cell(report, "agent_startup", "agent_startup_breakdown", agent_type, "removable_tools")
        if not isinstance(removable, (int, float)) or removable < th.spawn_cost_removable_tokens:
            continue
        # Fix R3: topology_spawn_write has no priced_turns column of its
        # own; cross-reference ttl_by_agent_type's for the same agent
        # type (None when absent -- see _row_meets_min_sample).
        spawns = row[spawns_idx] if spawns_idx is not None and spawns_idx < len(row) else None
        priced_turns = _cell(report, "ttl", "ttl_by_agent_type", agent_type, "priced_turns")
        if not _row_meets_min_sample(th, spawns, priced_turns):
            continue
        evidence = [
            _evidence("Mean first call", mean_first_call, "agents", "topology_spawn_write", agent_type),
            _evidence(
                "Tool definitions it rarely or never uses",
                removable,
                "agent_startup",
                "agent_startup_breakdown",
                agent_type,
            ),
        ]
        # Fix A1: only a genuine frontmatter-backed agent type has an
        # omitClaudeMd lever this rule can point at -- Claude Code's own
        # bundled agent types (see _BUILTIN_AGENT_TYPES) have no
        # ``.claude/agents/<type>.md`` file to patch. For those (the
        # read-only helpers skipped above apart), most of the first call
        # is Claude Code's own system prompt and tool definitions, which a
        # shorter task prompt does not touch: this is workflow advice that
        # points at a same-named agent file with a tools list (the
        # spawn-tools-list card builds the list), with no lever and
        # therefore no render_patch_set stanza.
        if has_file:
            category = "settings"
            action = (
                f"Trim what {agent_type} is sent at startup -- a tools list, omitClaudeMd or a narrower "
                "skills set cuts what has to be written into its cache on the very first turn."
            )
            lever = "omitClaudeMd"
            # Fix R13: omitClaudeMd is per-agent frontmatter (each
            # .claude/agents/<type>.md has its own copy), not a
            # top-level settings key, so this is "repo" scope the
            # same way a per-agent TTL lever is -- it was previously
            # left at the "user" default because _lever_scope() only
            # ever saw the TTL-switch lever text.
            scope = "repo"
        else:
            category = "workflow"
            action = (
                f"{agent_type} is a built-in agent type, so most of what it reads at startup is Claude Code's "
                "own system prompt and tool definitions, which a shorter task prompt does not change. A "
                "same-named agent file with a tools list leaves out the tools it never calls."
            )
            lever = None
            scope = "user"
        out.append(
            Recommendation(
                id="spawn-cost",
                severity="advice",
                category=category,
                archetypes=_ALL_ARCHETYPES,
                title=f"Spawning {agent_type} is expensive before it does any work",
                action=action,
                lever=lever,
                scope=scope,
                agent_type=agent_type,
                evidence=evidence,
            )
        )
    return out


#: Effort levels that count as high for ``effort-mismatch``.
_HIGH_EFFORT_LEVELS = ("high", "xhigh", "max")
#: Easy messages at one effort level before the direct path cites them.
_EFFORT_MIN_MESSAGES = 5


def _effort_mismatch_reported(
    report: ReportModel, th: RecommendThresholds, snapshot: Snapshot | None
) -> list[Recommendation]:
    """``effort-mismatch`` from messages Claude reported as easy work
    (metrics capture's ``level``) that ran at high effort or above: the
    Work habits section's ``habits_effort_fit`` rows, joined per message,
    so no approximation caveat. Empty without those tags."""
    table = _table(report, "habits", "habits_effort_fit")
    if table is None:
        return []
    cycles_idx = _col_index(table, "cycles")
    share_idx = _col_index(table, "thinking_pct")
    saving_idx = _col_index(table, "saving")
    if None in (cycles_idx, share_idx, saving_idx):
        return []
    rows = [
        row
        for row in table.rows
        if row
        and isinstance(row[0], str)
        and row[0].partition(":")[0] == "easy"
        and row[0].partition(":")[2] in _HIGH_EFFORT_LEVELS
        and isinstance(row[cycles_idx], int)
        and row[cycles_idx] >= _EFFORT_MIN_MESSAGES
        and isinstance(row[share_idx], (int, float))
        and row[share_idx] > th.effort_mismatch_thinking_share_pct
    ]
    if not rows:
        return []
    evidence = []
    for row in rows:
        effort = row[0].partition(":")[2]
        evidence.append(_evidence(f"Easy messages at {effort} effort", row[cycles_idx], "habits", "habits_effort_fit", row[0]))
        evidence.append(
            _evidence(f"Easy work at {effort} effort, thinking share of output", row[share_idx], "habits", "habits_effort_fit", row[0])
        )
    saving = sum(row[saving_idx] for row in rows if isinstance(row[saving_idx], (int, float)))
    lever, scope = _lever_scope("effortLevel", snapshot)
    return [
        Recommendation(
            id="effort-mismatch",
            severity="advice",
            category="settings",
            archetypes=_ALL_ARCHETYPES,
            title="High effort is being spent on easy work",
            action=_action_with_scope(
                "Lower effortLevel -- Claude reported these messages as easy work, yet most of their output "
                "was thinking.",
                scope,
            ),
            lever=lever,
            scope=scope,
            evidence=evidence,
            saving_usd=saving or None,
        )
    ]


def _rule_effort_mismatch(
    report: ReportModel, th: RecommendThresholds, snapshot: Snapshot | None
) -> list[Recommendation]:
    """Direct from reported work levels when metrics capture has them
    (:func:`_effort_mismatch_reported`); otherwise the fallback below.

    Fix R22 (see module docstring's deviations list): the fallback's
    corpus-wide high-effort thinking share and the docs/general-dev
    session counts are read from two independent group-bys with no report
    table joining them by session -- this is an approximation, not a per-
    session join, and both ``action`` and the module docstring say so."""
    direct = _effort_mismatch_reported(report, th, snapshot)
    if direct:
        return direct
    purpose_table = _table(report, "sessions", "sessions_by_purpose")
    effort_table = _table(report, "agents", "topology_effort_tokens")
    if purpose_table is None or effort_table is None:
        return []
    sessions_idx = _col_index(purpose_table, "sessions")
    if sessions_idx is None:
        return []
    docs_purpose_rows = [
        (row[0], row[sessions_idx])
        for row in purpose_table.rows
        if row and row[0] in _DOCS_GENERAL_PURPOSES and sessions_idx < len(row)
    ]
    if not docs_purpose_rows:
        return []
    thinking_share = _cell(report, "agents", "topology_effort_tokens", "high", "thinking_share")
    if not isinstance(thinking_share, (int, float)) or thinking_share <= th.effort_mismatch_thinking_share_pct:
        return []
    evidence = [_evidence("High-effort thinking share of output", thinking_share, "agents", "topology_effort_tokens", "high")]
    for purpose, sessions in docs_purpose_rows:
        evidence.append(_evidence(f"{purpose} sessions in corpus", sessions, "sessions", "sessions_by_purpose", purpose))
    lever, scope = _lever_scope("effortLevel", snapshot)
    return [
        Recommendation(
            id="effort-mismatch",
            severity="advice",
            category="settings",
            archetypes=_ALL_ARCHETYPES,
            title="High effort is being spent on light editing work",
            action=_action_with_scope(
                "Lower effortLevel for docs/general-dev sessions -- thinking tokens dominate "
                "output there without a matching increase in edit complexity. (Approximation: "
                "the thinking share is corpus-wide, not joined to these specific sessions -- "
                "no report table links effort level to session purpose.)",
                scope,
            ),
            lever=lever,
            scope=scope,
            evidence=evidence,
        )
    ]


def _rule_discovery_share(report: ReportModel, th: RecommendThresholds) -> list[Recommendation]:
    share_pct = _cell(report, "phases", "phases_summary", "discovery", "cost_share_pct")
    if not isinstance(share_pct, (int, float)) or share_pct <= th.discovery_share_pct:
        return []
    return [
        Recommendation(
            id="discovery-share",
            severity="advice",
            category="workflow",
            archetypes=_ALL_ARCHETYPES,
            title="Discovery is a large share of the work",
            action=(
                "Front-load exploration into a briefing or a cached reference doc so future "
                "sessions spend less time re-discovering the same ground."
            ),
            lever=None,
            evidence=[
                _evidence("Share of cost", share_pct, "phases", "phases_summary", "discovery"),
            ],
        )
    ]


def _rule_pricing_coverage(report: ReportModel) -> list[Recommendation]:
    """Fires when ``report.meta.pricing.coverage_pct`` is below 100%, or
    when it reads 100% only because every turn was priced by closest
    (prefix) match rather than its own rate (fix 2 -- 100% coverage
    means "no unpriced tokens", not "every model has its own price").
    Cites the ``scorecard.dimensions`` row that mirrors coverage_pct as
    evidence either way.

    ``report.py`` appends ``usage.pricing_unknown_models``
    (``pricing.PricingCoverage.as_table``) whenever a reply came from a
    model with no price, and ``usage.pricing_closest_match``
    (``as_closest_match_table``) whenever one was priced by closest
    match; the action names whichever of those model ids is present.
    A closest-match id that is a newer release of what it was priced as
    (``pricing.newer_version_of``: ``claude-x-5-5`` on ``claude-x-5``)
    gets its own sentence, since it isn't a mismatched model, just one
    the rate card doesn't know yet. Without either table (a report built
    with ``include`` leaving out ``usage``) the rule still fires on
    ``coverage_pct`` alone, with a generic action.
    """
    coverage_pct = report.meta.pricing.coverage_pct
    closest_table = _table(report, "usage", "pricing_closest_match")
    closest_rows = [row for row in closest_table.rows if row] if closest_table is not None else []
    closest_model_ids = [row[0] for row in closest_rows]
    closest_keys = [c.key for c in closest_table.columns] if closest_table is not None else []
    priced_as_at = closest_keys.index("priced_as") if "priced_as" in closest_keys else None
    newer_versions = [
        (row[0], row[priced_as_at])
        for row in closest_rows
        if priced_as_at is not None
        and len(row) > priced_as_at
        and pricing.newer_version_of(str(row[0]), str(row[priced_as_at]))
    ]
    newer_ids = {m for m, _ in newer_versions}
    plain_model_ids = [m for m in closest_model_ids if m not in newer_ids]
    if coverage_pct >= 100.0 and not closest_model_ids:
        return []
    dq_value = _cell(report, "scorecard", "dimensions", "data_quality", "value")
    if dq_value is None:
        return []

    action_parts: list[str] = []
    unknown_table = _table(report, "usage", "pricing_unknown_models")
    unknown_model_ids = (
        [row[0] for row in unknown_table.rows if row] if unknown_table is not None else []
    )
    if unknown_model_ids:
        action_parts.append(
            f"Add {', '.join(str(m) for m in unknown_model_ids)} to pricing.toml so the "
            "report's cost figures cover the whole corpus."
        )
    if plain_model_ids:
        ids = ", ".join(str(m) for m in plain_model_ids)
        action_parts.append(
            f"{ids} was priced by closest match, not its own rate; give it a pricing.toml row of its own "
            "for an exact cost."
            if len(plain_model_ids) == 1
            else f"{ids} were priced by closest match, not their own rates; give them pricing.toml rows of "
            "their own for an exact cost."
        )
    for model_id, priced_as in newer_versions:
        action_parts.append(
            f"There is no rate of its own for {model_id} yet, so it was priced as {priced_as}; update "
            f"ClaudeGlass or add a models.\"{model_id}\" row to pricing.toml for its exact cost."
        )
    if not action_parts:
        action_parts.append(
            "Add the unpriced model id(s) to pricing.toml so the report's cost figures cover "
            "the whole corpus."
        )

    title = (
        "Some usage could not be priced"
        if coverage_pct < 100.0
        else "Some usage was only priced by closest match"
    )

    return [
        Recommendation(
            id="pricing-coverage",
            severity="info",
            category="data",
            archetypes=_ALL_ARCHETYPES,
            title=title,
            action=" ".join(action_parts),
            lever=None,
            evidence=[
                _evidence("Pricing coverage (data-quality dimension)", dq_value, "scorecard", "dimensions", "data_quality"),
            ],
        )
    ]


def _rule_data_quality(report: ReportModel, th: RecommendThresholds) -> list[Recommendation]:
    table = _table(report, "ttl", "ttl_by_agent_type")
    fidelity_row_key = None
    fidelity_value = None
    if table is not None:
        idx = _col_index(table, "fidelity_pct")
        if idx is not None:
            for row in table.rows:
                if idx < len(row) and isinstance(row[idx], (int, float)) and row[idx] > th.data_quality_fidelity_pct:
                    fidelity_row_key = row[0]
                    fidelity_value = row[idx]
                    break

    diagnostics = report.diagnostics
    unparsable_pct = (
        100.0 * diagnostics.unparsable_lines / diagnostics.lines if diagnostics.lines else 0.0
    )
    fires_on_unparsable = unparsable_pct > th.data_quality_unparsable_pct
    # A handful of odd replies is noise; only a real share of them is a
    # caveat worth a card (same bar as unreadable lines).
    priced_turns = _cell(report, "overview", "totals", "priced_turns", "value")
    mismatch_pct = (
        100.0 * diagnostics.ttl_sum_mismatch / priced_turns
        if isinstance(priced_turns, (int, float)) and priced_turns
        else 0.0
    )
    fires_on_ttl_mismatch = mismatch_pct > th.data_quality_unparsable_pct
    fires_on_fidelity = fidelity_row_key is not None

    if not (fires_on_unparsable or fires_on_ttl_mismatch or fires_on_fidelity):
        return []

    dq_value = _cell(report, "scorecard", "dimensions", "data_quality", "value")
    if dq_value is None:
        return []

    notes = []
    if fires_on_unparsable:
        notes.append(f"{unparsable_pct:.2f}% of log lines could not be read")
    if fires_on_ttl_mismatch:
        notes.append(
            f"{mismatch_pct:.2f}% of replies reported cache writes that don't add up across the two cache lifetimes"
        )
    if fires_on_fidelity:
        notes.append(f"the cache lifetime replay for {fidelity_row_key} fits its real cost poorly")

    evidence = [_evidence("Pricing coverage (data-quality dimension)", dq_value, "scorecard", "dimensions", "data_quality")]
    if fires_on_fidelity:
        evidence.append(_evidence("TTL simulation fidelity", fidelity_value, "ttl", "ttl_by_agent_type", fidelity_row_key))

    return [
        Recommendation(
            id="data-quality",
            severity="info",
            category="data",
            archetypes=_ALL_ARCHETYPES,
            title="Some numbers in this report carry a data-quality caveat",
            action="Treat this report's figures with caution: " + "; ".join(notes) + ".",
            lever=None,
            evidence=evidence,
        )
    ]


def _limit_rollup_evidence(report: ReportModel) -> list[tuple]:
    """What the limits section's stops roll-up adds to the limit-pressure
    card: the stops it counted, each cost centre's share of the list-price
    spend before them, and how much of that spend ran while 3 or more agents
    worked at once, in those stops and across all your work. Empty when the
    roll-up is missing or counted no stops; a cell it lacks is left out, so
    every value cited is a cell of that table."""

    def cell(column: str):
        value = _cell(report, "limits", "limits_stops_rollup", "all", column)
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    stops = cell("stops")
    if not stops:
        return []
    evidence = [_evidence("Stops counted in the roll-up", stops, "limits", "limits_stops_rollup", "all")]
    for label, column in (
        ("Main session share of spend", "main_share_pct"),
        ("Direct agents share of spend", "direct_share_pct"),
        ("Workflow agents share of spend", "workflow_share_pct"),
        ("Spend with 3+ agents at once, in these stops", "burst_share_pct"),
        ("Spend with 3+ agents at once, across all your work", "all_burst_share_pct"),
    ):
        value = cell(column)
        if value is not None:
            evidence.append(_evidence(label, value, "limits", "limits_stops_rollup", "all"))
    return evidence


def _rule_limit_pressure(report: ReportModel, th: RecommendThresholds) -> list[Recommendation]:
    """v3-limits addition (not part of plan Appendix A5, see this
    module's module docstring convention for a documented deviation):
    repeated usage-cap stops, a weekly stop that held up your work, or even
    one subagent a limit cut off mid-task, are worth surfacing on
    their own account -- not because there's a single setting to
    change, but because several of this report's other findings
    (recache's limit-expiry rows, ttl's limit_gaps column, the pauses
    classify takes out of a session's away time) are all downstream
    symptoms of the same root cause. Reads the ``limits`` section built
    by ``limits.build_section`` -- returns ``[]`` when that section
    isn't present (e.g. an older cached report, or a report assembled
    before this batch's ``report.py`` wiring landed).

    It counts stops, not limit messages: a single stop writes a storm of
    them. Five-hour stops are read as a rate per seven days, against at
    least a week even when the window is shorter, so one stop in a short
    window is never a rate; each weekly stop that stopped work, and each
    agent cut off, fires it on its own.
    """

    def count(column: str) -> float:
        value = _cell(report, "limits", "limits_summary", "all", column)
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0

    five_hour = count("five_hour_stops")
    weekly = count("weekly_stops")
    weekly_stopped_work = count("weekly_stops_stopped_work")
    cut_off = count("agents_cut_off")
    days = count("window_days")

    per_week = five_hour * 7 / max(days, 7)
    if (
        per_week < th.limit_pressure_min_episodes
        and weekly_stopped_work < 1
        and cut_off < th.limit_pressure_min_terminated_rate_limit
    ):
        return []

    evidence = [
        _evidence("5-hour limit stops", five_hour, "limits", "limits_summary", "all"),
        _evidence("Weekly limit stops", weekly, "limits", "limits_summary", "all"),
        _evidence("Weekly stops that stopped work", weekly_stopped_work, "limits", "limits_summary", "all"),
        _evidence("Agents cut off by a limit", cut_off, "limits", "limits_summary", "all"),
        _evidence("Days covered", days, "limits", "limits_summary", "all"),
    ]
    sessions_affected = _cell(report, "limits", "limits_summary", "all", "sessions_affected")
    if sessions_affected is not None:
        evidence.append(_evidence("Sessions affected", sessions_affected, "limits", "limits_summary", "all"))
    evidence.extend(_limit_rollup_evidence(report))

    return [
        Recommendation(
            id="limit-pressure",
            severity="advice",
            category="workflow",
            archetypes=_ALL_ARCHETYPES,
            title="Usage-cap pauses are a recurring interruption",
            action=(
                "This corpus hit its session/weekly usage cap repeatedly (or had a subagent "
                "cut off by it) -- consider pacing concurrent agents to the usage window, or "
                "reviewing the weekly cap against actual usage, rather than treating the "
                "resulting pauses as ordinary idle time."
            ),
            lever=None,
            evidence=evidence,
        )
    ]


# -- key assignment -------------------------------------------------------
#
# ``id`` alone repeats: a rule like ``spawn-claude-md`` fires once per
# agent type, so several recommendations in the same list share one id.
# ``key`` disambiguates them for a stable dashboard link
# (``#/actions/recommendations?id=<key>``), staying URL-safe
# (``[a-z0-9._:-]``) even for an agent type a user named with spaces or
# capitals.

_KEY_UNSAFE_RE = re.compile(r"[^a-z0-9._-]+")


def _key_slug(text: str) -> str:
    """``text`` lower-cased, with every run of characters outside
    ``[a-z0-9._-]`` collapsed to one ``-`` and leading/trailing ``-``
    trimmed -- the agent-type half of a recommendation ``key``."""
    return _KEY_UNSAFE_RE.sub("-", text.lower()).strip("-")


def _rec_key(rec: Recommendation) -> str:
    """``rec``'s deterministic, URL-safe key: ``id`` alone when it isn't
    agent-scoped, else ``id`` plus a slug of ``agent_type`` so the many
    recommendations one rule produces (one per agent type) each get a
    key that's unique within the list and stable across two runs of the
    same corpus."""
    if not rec.agent_type:
        return rec.id
    return f"{rec.id}:{_key_slug(rec.agent_type)}"


# -- entry point --------------------------------------------------------


def recommend(
    report: ReportModel,
    *,
    config: Config,
    archetype: str | None,
    snapshot: Snapshot | None = None,
    thresholds: RecommendThresholds | None = None,
    units: "Units | None" = None,
) -> list[Recommendation]:
    """Every Appendix A5 recommendation rule this module implements,
    evaluated against ``report`` (an already-assembled
    :class:`~claudeglass.model.ReportModel`) and filtered to the
    rules whose ``archetypes`` gating admits ``archetype`` (a rule with an
    empty ``archetypes`` tuple applies to every archetype, including
    ``None``).

    Returns ``[]`` immediately if the corpus doesn't meet the minimum
    sample (5 sessions or 200 priced turns, plan A5's closing line) --
    every rule below is a comparison across a group, and a group this
    small produces noise, not a recommendation.
    """
    th = thresholds or RecommendThresholds.from_config(
        config.thresholds.get("recommend") if isinstance(config.thresholds, dict) else None,
        config,
    )

    if not _meets_min_sample(report, th):
        return []

    # v4 wiring round: each of these four modules carries its own RULES
    # (and its own from_config-built thresholds) rather than a native
    # _rule_xxx defined in this file -- see each module's own docstring
    # ("a caller folds <module>.RULES into recommend.recommend()'s own
    # rule list"). Built locally here, the same from_config(config.thresholds)
    # convention every other module's thresholds in this file already use,
    # rather than threading them through from build_report -- recommend()
    # already takes ``config`` directly, so there is no need for a second
    # plumbing path.
    carry_th = carry.CarryThresholds.from_config(config.thresholds)
    compaction_sim_th = compaction_sim.CompactionSimThresholds.from_config(config.thresholds)
    model_swap_th = model_swap.ModelSwapThresholds.from_config(config.thresholds)
    agent_models_th = agent_models.AgentModelThresholds.from_config(config.thresholds)
    waste_th = waste.WasteThresholds.from_config(config.thresholds)
    handoff_th = handoff.HandoffThresholds.from_config(config.thresholds)
    hooks_th = hook_costs.HookThresholds.from_config(config.thresholds)
    run_split_th = run_split.RunSplitThresholds.from_config(config.thresholds)
    tool_search_th = tool_search.ToolSearchThresholds.from_config(config.thresholds)

    recs: list[Recommendation] = []
    recs.extend(_rule_ttl_switch(report, config, snapshot, archetype, th))
    recs.extend(_rule_long_tool_waits(report, th))
    recs.extend(_rule_notification_invalidation(report, th))
    recs.extend(_rule_batch_instructions(report, th))
    recs.extend(_rule_subagent_volume(report, th, archetype))
    recs.extend(_rule_compaction_churn(report, th, snapshot))
    recs.extend(_rule_long_context_share(report, th, snapshot))
    recs.extend(_rule_cache_read_dominance(report, th))
    recs.extend(_rule_baseline_bloat(report, th, snapshot, archetype))
    # COV-09: env-var / deprecated-setting lever rules (see their shared
    # comment block above _env_lever_scope for why they don't use
    # RecommendThresholds or SettingChange).
    recs.extend(_rule_env_disable_prompt_caching(report, snapshot))
    recs.extend(_rule_env_tool_search(report, th, snapshot, archetype))
    recs.extend(_rule_env_max_output_tokens(report, snapshot))
    recs.extend(_rule_env_subagent_model(report, snapshot, archetype))
    recs.extend(_rule_attribution_deprecated(report, snapshot))
    recs.extend(_rule_agent_report_size(report, th, archetype))
    recs.extend(_rule_agent_batch_probes(report, th, archetype))
    recs.extend(_rule_plan_rounds(report, th))
    part_recs, covered_agents = _rule_spawn_parts(report, th, archetype, snapshot, units)
    recs.extend(part_recs)
    recs.extend(_rule_spawn_cost(report, th, archetype, snapshot, covered_agents))
    recs.extend(_rule_effort_mismatch(report, th, snapshot))
    if _section(report, "phases") is not None:
        recs.extend(_rule_discovery_share(report, th))
    recs.extend(_rule_pricing_coverage(report))
    recs.extend(_rule_data_quality(report, th))
    recs.extend(_rule_limit_pressure(report, th))
    # v4 wiring round: registered after the four modules' own sections
    # exist in ``report`` (build_report appends them before calling
    # recommend() -- see report.py's own docstring), since each rule
    # reads its evidence back out of its own already-rendered table(s),
    # the same read-rendered-tables-not-raw-accumulators contract every
    # rule in this file follows.
    recs.extend(carry.RULES[0](report, carry_th))
    recs.extend(handoff.RULES[0](report, handoff_th))
    recs.extend(run_split.RULES[0](report, run_split_th))
    for hook_rule in hook_costs.RULES:
        recs.extend(hook_rule(report, hooks_th))
    recs.extend(tool_search.RULES[0](report, tool_search_th))
    recs.extend(compaction_sim.RULES[0](report, compaction_sim_th, snapshot))
    recs.extend(model_swap.RULES["model-tier"](report, model_swap_th, archetype, snapshot))
    # Agents that ran on a larger model than their work needed: its own
    # cards, kept out of model-tier and the Savings levers so nothing is
    # counted twice.
    for agent_models_rule in agent_models.RULES.values():
        recs.extend(agent_models_rule(report, agent_models_th, archetype, snapshot))
    recs.extend(waste.RULES[0](report, waste_th))

    if archetype is not None:
        recs = [r for r in recs if not r.archetypes or archetype in r.archetypes]

    from . import advice  # imported here: advice imports this module

    recs = advice.finish(recs, report, snapshot, units)
    # Runs last: it points at the most important of the finished
    # recommendations above, by its final title. Needs the report's
    # ``elasticity`` section, so it only fires under subscription billing
    # with enough usage-limit readings.
    elasticity_th = elasticity.ElasticityThresholds.from_config(config.thresholds)
    recs.extend(elasticity.RULES[0](dataclasses.replace(report, recommendations=list(recs)), elasticity_th))
    # Last step, after every rule (including the merges/drops above) has
    # had its say on the final list: assign each recommendation's key.
    for rec in recs:
        rec.key = _rec_key(rec)
    _unique_keys(recs)
    return recs


def _unique_keys(recs: list[Recommendation]) -> None:
    """Give every repeated key a suffix from its card's first evidence
    row, so a rule that fires once per row of a table with no agent type
    (``spawn-shared-claude-md``: one card per CLAUDE.md source) still
    gets one key per card, and each card keeps its key whichever of the
    others fire. Ignores and links need a key that names one card."""
    counts: dict[str, int] = {}
    for rec in recs:
        counts[rec.key] = counts.get(rec.key, 0) + 1
    seen: set[str] = set()
    for rec in recs:
        if counts[rec.key] > 1:
            row = rec.evidence[0][3] if rec.evidence else None
            base = f"{rec.key}:{_key_slug(str(row))}" if row not in (None, "") else rec.key
            key, n = base, 2
            while key in seen:
                key, n = f"{base}-{n}", n + 1
            rec.key = key
        seen.add(rec.key)


# -- patch-set rendering --------------------------------------------------
#
# ``_AGENT_LEVER_RE`` is defined in the scope-encoding section above and
# shared here.

_TTL_TARGET_RE = re.compile(r"\bto (1h|5m)\b")

#: The two settings.json cache-lifetime keys a ``ttl-switch`` lever can
#: name, rendered as a settings stanza with the switch's target value.
_TTL_SETTINGS_KEYS = ("promptCacheTtl", "subagentPromptCacheTtl")


def _patch_value(value, missing: str) -> str:
    """A ``SettingChange`` value as ``render_patch_set`` prints it."""
    if value is None:
        return missing
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(str(v) for v in value) + "]"
    return str(value)


def render_patch_set(recs: list[Recommendation]) -> str:
    """A unified-diff-style text of the settings/frontmatter changes
    ``recs`` imply: one ``.claude/agents/<agent_type>.md`` stanza per
    agent type -- merging every per-agent lever for that same agent
    (e.g. both a TTL switch and ``omitClaudeMd``) into a single diff --
    and a bare settings-key stanza (no path -- see module docstring)
    for genuine top-level settings levers. Contains no path other than
    ``.claude/agents/<agent_type>.md``.

    Fix R13: agent routing now prefers ``rec.agent_type`` (set by every
    per-agent-type rule) over sniffing the agent name back out of
    ``lever``'s text with ``_AGENT_LEVER_RE`` -- the regex only ever
    matched the TTL-switch lever's specific wording, so a per-agent
    lever like spawn-cost's ``omitClaudeMd`` used to fall through to
    the generic top-level "settings (user)" stanza even though it is
    genuinely per-agent frontmatter. The regex is kept as a fallback
    for recommendations that don't set ``agent_type`` explicitly.
    """
    lines: list[str] = []
    # agent_type -> ordered {settings_key: rendered_value}, plus whether
    # any lever contributing to it is managed.
    agent_stanzas: dict[str, dict] = {}
    seen_settings_keys: set[str] = set()

    for rec in recs:
        if rec.changes:
            # Readability P5: a recommendation that names its concrete
            # edits (``SettingChange``) renders those -- key, the value in
            # effect now and the value it proposes -- instead of guessing
            # them back out of ``lever``/``action`` text.
            for change in rec.changes:
                scope = change.scope or rec.scope
                old_text = _patch_value(change.current, "(unset)")
                new_text = (
                    _patch_value(change.value, "(unset)")
                    if change.value is not None
                    else f"(your choice: {change.suggested or 'see recommendation'})"
                )
                if change.target == "agent" and change.agent:
                    stanza = agent_stanzas.setdefault(change.agent, {"managed": False, "keys": {}})
                    stanza["managed"] = stanza["managed"] or scope == "managed"
                    stanza["keys"][change.key] = (old_text, new_text)
                    continue
                if change.key in seen_settings_keys:
                    continue
                seen_settings_keys.add(change.key)
                label = f"settings ({'repo' if scope == 'repo' else 'user'})"
                lines.append(f"--- {label}")
                lines.append(f"+++ {label}")
                if scope == "managed":
                    lines.append("# managed by policy -- shown for reference only")
                lines.append(f"-{change.key}: {old_text}")
                lines.append(f"+{change.key}: {new_text}")
                lines.append("")
            continue
        bare_lever = rec.lever
        # The agent-model cards (agent_models.RULES) name the model as their
        # lever, but their fix is a prompt: the call that starts the agent
        # sets it, so there is no agent-file line to patch.
        if not bare_lever or rec.id in agent_models.RULES:
            continue
        is_managed = rec.scope == "managed"

        agent_match = _AGENT_LEVER_RE.search(bare_lever)
        # "top-level" is the main session, not a subagent -- its levers
        # (e.g. promptCacheTtl) are genuine top-level settings keys, so
        # only route on agent_type when it names an actual subagent.
        # subagentPromptCacheTtl is a settings key too, even on a
        # per-agent-type row (see _ttl_row_lever).
        agent_type = rec.agent_type if rec.agent_type not in (None, "top-level") else None
        if bare_lever in _TTL_SETTINGS_KEYS:
            agent_type = None
        if agent_type is None and agent_match:
            agent_type = agent_match.group(1)

        if agent_type is not None:
            stanza = agent_stanzas.setdefault(agent_type, {"managed": False, "keys": {}})
            if is_managed:
                stanza["managed"] = True
            if agent_match or bare_lever == "experimental.cacheTtl":
                target_match = _TTL_TARGET_RE.search(rec.action)
                target = target_match.group(1) if target_match else "5m|1h"
                stanza["keys"]["experimental.cacheTtl"] = target
            elif bare_lever == "omitClaudeMd":
                stanza["keys"]["omitClaudeMd"] = "true"
            else:
                stanza["keys"].setdefault(bare_lever, "(see recommendation action)")
            continue

        if bare_lever in _TTL_SETTINGS_KEYS:
            if bare_lever in seen_settings_keys:
                continue
            seen_settings_keys.add(bare_lever)
            target_match = _TTL_TARGET_RE.search(rec.action)
            target = target_match.group(1) if target_match else "5m|1h"
            lines.append("--- settings (user)")
            lines.append("+++ settings (user)")
            if is_managed:
                lines.append("# managed by policy -- shown for reference only")
            lines.append(f"-{bare_lever}: (unset)")
            lines.append(f"+{bare_lever}: {target}")
            lines.append("")
            continue

        if bare_lever in seen_settings_keys:
            continue
        seen_settings_keys.add(bare_lever)
        lines.append("--- settings (user)")
        lines.append("+++ settings (user)")
        if is_managed:
            lines.append("# managed by policy -- shown for reference only")
        lines.append(f"-{bare_lever}: (unchanged)")
        lines.append(f"+{bare_lever}: (see recommendation action)")
        lines.append("")

    agent_lines: list[str] = []
    for agent_type, stanza in agent_stanzas.items():
        path = f".claude/agents/{agent_type}.md"
        agent_lines.append(f"--- {path}")
        agent_lines.append(f"+++ {path}")
        if stanza["managed"]:
            agent_lines.append("# managed by policy -- shown for reference only")
        for key, value in stanza["keys"].items():
            old_text, new_text = value if isinstance(value, tuple) else ("(unset)", value)
            agent_lines.append(f"-{key}: {old_text}")
            agent_lines.append(f"+{key}: {new_text}")
        agent_lines.append("")

    lines = agent_lines + lines
    return "\n".join(lines).rstrip("\n") + ("\n" if lines else "")


__all__ = [
    "RecommendThresholds",
    "effective_min_sample",
    "recommend",
    "render_patch_set",
    "tools_list_offer",
]

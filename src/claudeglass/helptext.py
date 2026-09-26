"""Plain-English labels and "how to read this" help for the report.

Every report builder names its tables and columns for the code that reads
them (``recommend.py`` looks rows up by those raw keys). This module is
the display layer on top: :func:`annotate` walks an assembled
``ReportModel`` and fills in plain titles, column labels and help, value
labels, section intros, and where each table belongs on the dashboard.
It never changes a table's ``name``, a column's ``key`` or a row value,
so evidence lookups and the JSON/CSV keys stay exactly as they were.

House style: ``docs/writing-help.md``. Coverage and banned-word checks:
``tests/test_help_coverage.py``.

Dashboard placement (``Table.dashboard``) is the table audit: a table
stays on the dashboard ("keep") only if it answers a question you would
act on or explains a recommendation; supporting detail is shown collapsed
("advanced"); everything else is left to the CLI, JSON and CSV reports
("report").
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field

from .capture_catalogue import TASK_LABELS
from .capture_catalogue import THEMES as CAPTURE_THEMES
from .habits import ITEMS as HABIT_ITEMS
from .model import Column, Diagnostics, Help, ReportModel, Section, Table

# -- the table audit ------------------------------------------------------

#: Table name -> dashboard placement. A table not listed here defaults to
#: "keep"; ``tests/test_help_coverage.py`` checks every table the report
#: can emit is listed, so a new table is placed on purpose.
PLACEMENT: dict[str, str] = {
    # overview
    "totals": "keep",
    "by_model": "keep",
    # usage
    "by_day": "advanced",
    "by_week": "advanced",
    "by_month": "advanced",
    "by_project": "keep",
    "by_entrypoint": "advanced",
    "five_hour_blocks": "keep",
    "pricing_unknown_models": "advanced",
    "pricing_closest_match": "advanced",
    "pricing_fast_priced_as_standard": "advanced",
    "pricing_fast_applied": "advanced",
    # usage limits (subscription with usage-log readings)
    "elasticity_budget": "keep",
    "elasticity_recent_burn": "keep",
    "elasticity_fit": "advanced",
    # sessions
    "sessions_by_mode": "keep",
    "sessions_by_purpose": "keep",
    "sessions_detail": "report",
    # cache rebuilds
    "recache_summary": "keep",
    "recache_signature_split": "keep",
    "recache_gap_buckets": "keep",
    "recache_preceding_tool": "advanced",
    "recache_top_command_prefixes": "advanced",
    "recache_primary_cause": "keep",
    "recache_primary_cause_prefix_invalidated": "advanced",
    "recache_event_cooccurrence": "report",
    "recache_attachment_subsplit": "advanced",
    "recache_by_agent_type": "keep",
    "recache_huge_context": "advanced",
    "recache_by_group": "advanced",
    "cache_ground_truth": "advanced",
    "measured_miss_causes": "keep",
    # cache lifetime
    "ttl_by_agent_type": "keep",
    "ttl_gap_distribution": "advanced",
    "ttl_wasted_writes": "advanced",
    "ttl_premium_waste": "advanced",
    "ttl_break_even_share": "keep",
    "ttl_near_miss": "advanced",
    "ttl_addressable_share": "advanced",
    "ttl_cache_economy": "keep",
    # usage limits
    "limits_summary": "keep",
    "limits_hits_by_kind": "advanced",
    "limits_agent_terminated": "advanced",
    "limits_pauses": "keep",
    "limits_reset_hour_histogram": "advanced",
    "limits_by_agent_type": "advanced",
    "limits_csv_cross_check": "advanced",
    "limits_signals_cross_check": "advanced",
    # savings
    "carry_by_tool": "keep",
    "carry_by_agent_type": "advanced",
    "carry_top_results": "advanced",
    "carry_truncation_savings": "keep",
    "carry_output_cap_savings": "advanced",
    "compaction_sim_by_window": "keep",
    "compaction_sim_by_agent_type": "keep",
    "compaction_sim_by_task": "advanced",
    "compaction_sim_fidelity": "advanced",
    "plan_handoff_summary": "keep",
    "plan_handoff_by_session": "keep",
    "model_swap_by_agent_type": "keep",
    "model_swap_summary": "keep",
    "model_swap_agent_file_runs": "report",
    "waste_summary": "keep",
    "waste_by_cause": "keep",
    "waste_by_agent_type": "advanced",
    "waste_top_sessions": "advanced",
    # compactions
    "compactions_summary": "keep",
    "compactions_trigger_mix": "advanced",
    "compactions_per_session": "advanced",
    # subagent startup
    "agent_startup_breakdown": "keep",
    "agent_startup_unused": "keep",
    "agent_startup_shared": "keep",
    # agents
    "topology_spawn_write": "advanced",
    "topology_session_baseline": "advanced",
    "topology_upward_tool_result": "advanced",
    "topology_report_proxy": "advanced",
    "topology_skills_rollup": "advanced",
    "topology_spawn_depth": "advanced",
    "topology_cost_per_spawn": "keep",
    "topology_chains_summary": "advanced",
    "topology_reminder_hook_pressure": "advanced",
    "topology_cache_signal_histogram": "report",
    "topology_mcp_cost": "advanced",
    "topology_effort_tokens": "keep",
    "topology_per_turn_effort_tokens": "report",
    "topology_effort_by_agent_type": "advanced",
    "topology_context_composition": "advanced",
    "topology_redundant_work": "advanced",
    "topology_redundant_reads": "advanced",
    # splitting long subagent runs
    "run_split_summary": "keep",
    "run_split_by_agent": "keep",
    "run_split_sweep": "advanced",
    # your hooks
    "hooks_summary": "keep",
    "hooks_by_script": "keep",
    # what tool search saves
    "tool_search_summary": "keep",
    "tool_search_by_server": "keep",
    "cost_record_summary": "keep",
    "cost_record_sessions": "keep",
    # quality signals
    "quality_by_agent": "keep",
    "quality_by_setup": "keep",
    "quality_retried": "keep",
    "quality_retry_reasons": "keep",
    "quality_failing_tools": "advanced",
    "quality_counts": "advanced",
    "quality_markers": "advanced",
    # work habits and metrics capture
    "habits_digest": "keep",
    "habits_playbook": "keep",
    "habits_by_task": "keep",
    "habits_briefs": "advanced",
    "habits_brief_templates": "keep",
    "habits_agents": "advanced",
    "habits_effort_fit": "advanced",
    "habits_setups": "keep",
    "habits_agents_by_task": "advanced",
    "habits_outcomes": "advanced",
    "habits_by_shape": "keep",
    "habits_self_report": "advanced",
    "habits_prompt_flags": "advanced",
    "habits_skills": "advanced",
    "habits_tool_output": "advanced",
    "capture_usage": "report",
    # workstyle / workflows
    "workstyle_archetypes": "advanced",
    "workflows_summary": "keep",
    "workflows_status_mix": "advanced",
    "workflows_detail": "advanced",
    # phases
    "phases_summary": "keep",
    "phases_by_transcript_kind": "advanced",
    "phases_by_agent_type": "advanced",
    # config
    "effective-config": "keep",
    "config-layers": "advanced",
    "config-groups": "advanced",
    "config-drift": "keep",
    "env-levers": "keep",
    # context budget
    "context_budget_baseline": "keep",
    "context_budget_autocompact": "advanced",
    "context_budget_statusline": "advanced",
    # scorecard
    "dimensions": "keep",
    "overall": "keep",
    # before and after
    "baseline_comparison_overview": "keep",
    "baseline_comparison_by_mode": "advanced",
    "data_quality": "keep",
    # Tables of other CLI commands (compare, finance, pricing, reconcile,
    # usage-windows, monthly), and of savers.py, which nothing builds into
    # a report yet. They never reach the dashboard; "report" records that.
    "compare_overview": "report",
    "compare_by_stratum": "report",
    "compare_co_changed": "report",
    "cost_by_model": "report",
    "finance_summary": "report",
    "pricing_rates": "report",
    "reconcile_by_period": "report",
    "cost_ground_truth_gap": "report",
    "savers_detected": "report",
    "savers_effect_by_stratum": "report",
    "savers_overhead": "report",
    "savers_search_substitution": "report",
    "savers_verdict": "report",
    "usage_windows_latest": "report",
    "usage_windows_regression": "report",
}

#: Table-name prefixes for tables whose names are built at run time (one
#: per changed config key).
PLACEMENT_PREFIXES: tuple[tuple[str, str], ...] = (("config-diff-", "advanced"), ("team_", "keep"))


def placement_for(table_name: str) -> str:
    """The dashboard placement for ``table_name`` (see :data:`PLACEMENT`)."""
    if table_name in PLACEMENT:
        return PLACEMENT[table_name]
    for prefix, placement in PLACEMENT_PREFIXES:
        if table_name.startswith(prefix):
            return placement
    return "keep"


# -- shared column help ---------------------------------------------------

#: Column key -> help, used for any column whose table doesn't give its
#: own. Covers the columns that mean the same thing everywhere.
COMMON_COLUMN_HELP: dict[str, str] = {
    "agent_type": "The subagent type, as named in its agent file or by Claude Code for built-in agents.",
    "spawns": "How many times this agent type was started in the window.",
    "turns": "How many model replies this covers.",
    "priced_turns": "Model replies with token counts. A reply from a model with no known price still counts; it is priced at zero.",
    "sessions": "How many sessions this covers.",
    "transcripts": "How many conversation logs this covers: one per main session and one per subagent run.",
    "cost": "Cost at list prices for the window.",
    "transcript_kind": "Main session, subagents or workflow agents.",
}


# -- per-section and per-table copy ---------------------------------------


@dataclass(frozen=True, slots=True)
class TableCopy:
    """Display copy for one table. ``columns`` maps a column key to
    ``(label, help)``; an empty label keeps the builder's own.
    ``lead_columns`` is ``Table.lead_columns``: at most 7 keys on a wide
    table, at most 4 headline values on a one-row summary table."""

    title: str = ""
    help: Help | None = None
    columns: dict[str, tuple[str, str]] = field(default_factory=dict)
    value_labels: dict[str, str] = field(default_factory=dict)
    row_groups: dict[str, str] = field(default_factory=dict)
    row_kinds: dict[str, str] = field(default_factory=dict)
    lead_columns: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class SectionCopy:
    title: str = ""
    intro: str = ""
    help: Help | None = None


_MAIN_OR_SUB = {"top-level": "Main session", "subagent": "Subagents", "workflow-agent": "Workflow agents"}
_SETTINGS_FILES = {
    "(unknown project)": "Unknown project (older snapshot)",
    "managed": "Managed policy",
    "project_local": "Project, local file (.claude/settings.local.json)",
    "project_shared": "Project, shared file (.claude/settings.json)",
    "user": "Your user settings (~/.claude/settings.json)",
}

_WINDOW_LABELS = {"five_hour": "5-hour limit", "seven_day": "Weekly limit", "spend_limit": "Spend limit"}

SECTION_COPY: dict[str, SectionCopy] = {
    "agent_startup": SectionCopy(
        title="What subagents are given at startup",
        intro=(
            "Every time a subagent starts, Claude Code sends it a set of instructions and lists before it "
            "does any work. You pay to write all of it into the cache, once per spawn."
        ),
        help=Help(
            shows="What each agent type receives before its first reply, what it received but never used, "
            "and what nearly every agent type receives alike.",
            read="Sizes are average tokens per spawn. Multiply by the number of spawns to see the total. "
            "\"Not recorded\" is the part Claude Code doesn't log in the transcript, mostly the tool "
            "definitions and system prompt.",
            act="Look for large parts that an agent never uses, such as a skills list no spawn called, "
            "or CLAUDE.md sent to agents that only search. {{page:actions/recommendations}} turns these into "
            "specific changes.",
        ),
    ),
    "agents": SectionCopy(
        title="Subagents: cost and flow",
        intro="How much your subagents cost, what they send back, and what fills their context.",
        help=Help(
            shows="Cost per subagent run, cost of skills and MCP servers, effort and thinking, and what "
            "fills the context window.",
            read="Compare agent types with each other. A type that costs much more per run, or carries "
            "far more context, is worth a closer look.",
            act="Start with the most expensive agent type per run, then check its startup context above.",
        ),
    ),
    "quality": SectionCopy(
        title="Is the work going well?",
        intro=(
            "Signs that work went badly: agent runs that didn't finish, failed tool calls, replies you "
            "stopped and messages where you corrected Claude. Use them to check that a cheaper model or a "
            "lower effort still does the job."
        ),
        help=Help(
            shows="Each signal per agent type and for the main session, then per model and effort. Each setup is "
            "compared with the one that agent used most.",
            read="Every signal is a share of something counted in your logs, such as failed tool calls out of all "
            "tool calls. A difference is marked only when it is unlikely to be chance; with few runs it says so.",
            act="If a setup is marked worse, move that agent back to the model or effort that did better. "
            "{{page:changes}} shows the same signals before and after each change you made.",
        ),
    ),
    "habits": SectionCopy(
        title="Work habits",
        intro=(
            "How the way you work shapes what it costs, with the habits worth trying. It covers breaking work down, "
            "what you tell Claude, research, planning, skills, agents and checks."
        ),
        help=Help(
            shows="A playbook of habits ranked by what they would have saved you, then the evidence per kind of "
            "task, brief, agent, effort and outcome.",
            read="Everything is worked out per message of yours, with all the work that answered it, subagents "
            "included. Evidence is labelled inferred, reported by Claude or your feedback.",
            act="Try the top habit for a week and watch its trend. Metrics capture ({{page:setup/capture}}) and "
            "feedback fill in the rest of the tables.",
        ),
    ),
    "capture": SectionCopy(
        title="Metrics capture",
        intro="What metrics capture cost while it was on.",
        help=Help(
            shows="The notes and tags metrics capture adds, and what they cost, measured from the transcripts.",
            read="Amounts are measured, not estimated. {{page:setup/capture}} estimates each level before you turn it on.",
            act="Lower the level or turn metrics off on {{page:setup/capture}} once enough has been collected.",
        ),
    ),
    "workstyle": SectionCopy(
        title="How you work",
        intro="Your sessions grouped by working pattern, such as a single operator or a fan-out of subagents.",
        help=Help(
            shows="How many sessions match each working pattern.",
            read="The largest group is your usual way of working. Profiles and recommendations are tuned to it.",
            act="",
        ),
    ),
    "workflows": SectionCopy(
        title="Workflows",
        intro="Multi-agent workflow runs: how many ran, how they ended, and what they cost.",
        help=Help(
            shows="Workflow runs in the window, their status, and the most expensive runs.",
            read="A high failure or cancel share means spend that produced nothing.",
            act="If one workflow dominates cost, check its agent count and phases.",
        ),
    ),
    "overview": SectionCopy(
        title="Overview",
        intro="How much you used in this window, and what it cost at list prices.",
        help=Help(
            shows="Totals for the window, then the same totals split by model.",
            read="Cache reads are usually most of your tokens but a small part of cost. Cost follows cache writes "
            "and output more than the raw token count.",
            act="If one model carries most of the cost, check whether some of its work could run on a cheaper "
            "model. {{page:spend/savings}} estimates the effect.",
        ),
    ),
    "usage": SectionCopy(
        title="Usage over time",
        intro="When you used Claude Code, in which projects, and from which app.",
        help=Help(
            shows="Usage by day, week and month, by project, by app, and in five-hour blocks.",
            read="Token counts here include cache reads, so they run far higher than cost suggests. "
            "Compare cost across periods, not tokens.",
            act="Look for days or projects that stand out, then check what ran there.",
        ),
    ),
    "elasticity": SectionCopy(
        title="What your usage limits hold",
        intro="How many tokens a full usage limit is worth for you, measured from your own usage-limit readings.",
        help=Help(
            shows="Each time the status line logs how full a usage limit is, this takes the rise since the last "
            "reading. That rise is matched against the tokens your sessions used in between.",
            read="With enough readings that agree, this gives the tokens one full limit holds. With too few, or "
            "readings that disagree, no figure is shown and the reason says why.",
            act="Use it to judge how far a saving elsewhere in the report stretches your limits.",
        ),
    ),
    "sessions": SectionCopy(
        title="Sessions by kind",
        intro="What kinds of sessions you run: how you worked in them, and what they were for.",
        help=Help(
            shows="Your sessions grouped two ways: by how you worked (mode) and by the kind of work (purpose).",
            read="Each session is sorted by simple rules on its activity: your prompts, the gaps between them, "
            "subagents and the tools used. Treat the groups as a rough guide.",
            act="A group with far more replies or subagents per session than the rest is where most of your "
            "usage goes. Start there.",
        ),
    ),
    "scorecard": SectionCopy(
        title="Scorecard",
        intro="A quick health check: five ratings from 1 (very poor) to 5 (excellent).",
        help=Help(
            shows="One rating each for cache efficiency, context size, subagent cost balance, config stability "
            "and data quality, plus an overall rating.",
            read="The overall rating is the lowest of the first four, never an average. Data quality is shown "
            "but left out of the overall rating.",
            act="Start with the lowest-rated area. The matching page and the recommendations say what to change.",
        ),
    ),
    "baseline_comparison": SectionCopy(
        title="Before and after",
        intro="How this window compares with a baseline you saved earlier.",
        help=Help(
            shows="The same measures for your saved baseline and for this window, side by side, with the change.",
            read="This is not a controlled test. A change can come from different work, not only from a "
            "setting you changed.",
            act="Look for a clear move in cost per session or cache rebuilds after a change you made. Check the "
            "by-mode table to see whether the kind of work was similar in both windows.",
        ),
    ),
    "phases": SectionCopy(
        title="Where the work went",
        intro="How your cost splits between exploring, building, checking and everything else.",
        help=Help(
            shows="Every reply sorted into a phase by the tools it called. Shown for the whole window, for the main "
            "session and subagents, and for each agent type.",
            read="Phases come from tool names only, not from what Claude intended. A reply that edits a file "
            "and runs tests in one go counts as building.",
            act="If exploring is more than 35% of cost, Claude spends a lot on finding things. Clearer pointers "
            "in CLAUDE.md or in your prompts can cut it.",
        ),
    ),
    "recache": SectionCopy(
        title="Cache rebuilds",
        intro="Which replies had to write their context into the cache again instead of reading it, and why.",
        help=Help(
            shows="Replies that rebuilt the cache, what that cost, and what happened right before each one.",
            read="A cache rebuild is a reply with a large context that read less than a fifth of it from the cache. "
            "Large means over 20,000 tokens by default. Compare each cause's share of rebuilds with its share of all "
            "replies: a cause far more common before rebuilds is likely behind them.",
            act="If rebuilds follow waits of 5 to 60 minutes, a longer cache lifetime (TTL) can help. If they follow "
            "a setting change, a hook or a note from Claude Code, that change is breaking the cache.",
        ),
    ),
    "ttl": SectionCopy(
        title="Cache lifetime (TTL)",
        intro=(
            "Whether a 5-minute or a 1-hour cache lifetime would cost you less, for the main session and "
            "each agent type."
        ),
        help=Help(
            shows="What you paid with the cache lifetimes you used, and what each lifetime would have cost. Also how "
            "much of what you wrote to the cache was ever read back.",
            read="A cache write costs more with a 1-hour lifetime than with a 5-minute one. It pays off only when "
            "enough replies come 5 to 60 minutes after the one before. The estimates replay your own replies "
            "and waits.",
            act="Follow the advice column only when the estimate error is low. A subagent's lifetime is set in its "
            "agent file, or for every subagent at once in your settings, which take priority over agent files. "
            "The main session's is set in your settings.",
        ),
    ),
    "limits": SectionCopy(
        title="Usage limits",
        intro="How often you hit a usage limit, how long you waited, and what restarting the cache cost afterwards.",
        help=Help(
            shows="Usage-limit stops, pauses, subagents stopped early, and the cache writes on the first reply after "
            "each pause.",
            read="After a pause the cache has expired, so the next reply writes its whole context again. That "
            "cost is unavoidable, so {{page:cache/rebuilds}} and {{page:cache/lifetime}} leave it out of their advice.",
            act="If limits stop you often at the same hour, move heavy work, such as large subagent fan-outs, "
            "away from that time.",
        ),
    ),
    "compactions": SectionCopy(
        title="Conversation summaries (compaction)",
        intro="How often Claude Code summarised a long conversation to free up context, and what the next reply cost.",
        help=Help(
            shows="How many sessions were summarised, how much context each summary removed, and the cache write "
            "on the reply after it.",
            read="A summary replaces most of the conversation with a short version. The next reply writes that "
            "shorter context to the cache. Most replies after a summary still read the cache normally.",
            act="If summaries are frequent, start fresh sessions for new tasks, or keep large tool output out of "
            "the conversation.",
        ),
    ),
    "carry": SectionCopy(
        title="Tool output kept in context",
        intro=(
            "What you pay to keep each tool's output in context, reply after reply, until a conversation "
            "summary drops it."
        ),
        help=Help(
            shows="What tool output costs after the reply that received it. Every later reply reads it again "
            "from the cache, or writes it again after a cache rebuild.",
            read="This cost is part of your cache read and cache write spend, not extra on top. Reply counts "
            "are measured. Output sizes are estimated at about 4 characters per token, and the cost split "
            "between cache reads and writes follows each reply's own mix.",
            act="If one tool's output dominates, cap it at the source. Pipe long command output through head or tail, "
            "search with Grep before reading whole files, and ask subagents for short reports.",
        ),
    ),
    "compaction_sim": SectionCopy(
        title="Auto-compact window: what if",
        intro=(
            "Would your sessions cost less if Claude Code wrote its conversation summary at a different "
            "context size?"
        ),
        help=Help(
            shows="A simulation that replays your sessions with the conversation summary (compaction) triggered at "
            "context sizes from 100,000 to 500,000 tokens. The setting is the auto-compact window in settings.json.",
            read="Only the \"As now\" figures are measured; every other cost is simulated. Each simulated summary is "
            "shaped like your own past ones. It fires the same distance below the window, and leaves your usual "
            "starting context plus a summary of the usual size. It is charged for writing the summary and for the "
            "cache rebuild on the reply after it. The simulation can't see what a summary loses or the files re-read "
            "after it, so small windows look better than they are.",
            act="Treat large savings at small windows with caution. {{page:actions/recommendations}} only suggests a "
            "minimum window, after taking off an allowance for re-reading.",
        ),
    ),
    "plan_handoff": SectionCopy(
        title="Building in a fresh session after a big plan",
        intro=(
            "Would the build after an approved plan cost less in a fresh session that starts from the plan alone?"
        ),
        help=Help(
            shows="Each main session where you approved a plan, and how much planning context the build kept. "
            "Also what the replies after it would have cost without it.",
            read="An upper bound: a fresh session may need more than the plan. The cache write of the plan and an "
            "allowance for re-reading files are taken off. Replies after a conversation summary aren't counted.",
            act="When a big plan is approved, run /clear and ask Claude to carry out the plan file. Forking copies "
            "the whole conversation, so it saves nothing.",
        ),
    ),
    "run_split": SectionCopy(
        title="Splitting long subagent runs",
        intro="Would long subagent runs cost less as several shorter runs, each starting fresh from a short note?",
        help=Help(
            shows="Each agent type's runs split every so many replies, and the interval that saves most. Also what "
            "each interval saves across every agent type.",
            read="An upper bound: a thin note can send the next run back over old ground. Each split's note, cache "
            "write and an allowance for re-reading files are taken off.",
            act="Give an agent that pays one part of a large task per run. Start a fresh one for the next part with "
            "a short note.",
        ),
    ),
    "hooks": SectionCopy(
        title="Your hooks",
        intro="Whether each hook you set up works, and what it costs in kept context, blocked calls and waiting.",
        help=Help(
            shows="Each hook by its script's name: failed runs and why, calls it blocked, context it added, and "
            "the time you waited on it.",
            read="A failing hook doesn't do its job. A block costs the reply that reads it. Context a hook adds is "
            "read again on every later reply.",
            act="Fix failing hooks first. A script named by a relative path works only from the project root, so "
            "start it with ${CLAUDE_PROJECT_DIR}. Hooks don't expand a Windows %VAR% path, so use $HOME instead.",
        ),
    ),
    "tool_search": SectionCopy(
        title="What tool search saves",
        intro="With tool search on, Claude Code lists tools by name and loads a tool's full definition only when "
        "Claude needs it. This is what keeping the rest out saved.",
        help=Help(
            shows="How many tool definitions tool search kept out of each request, what that saved, and what the "
            "searches themselves cost.",
            read="Definitions that were never loaded are sized from the ones that were, so the saving is an "
            "estimate. A loaded definition counts as sent either way.",
            act="",
        ),
    ),
    "cost_record": SectionCopy(
        title="Claude Code's own cost record",
        intro="Claude Code writes down what it thinks each session cost. Where it does, this checks ClaudeGlass's "
        "own figures against it.",
        help=Help(
            shows="Claude Code's own cost for each session next to ClaudeGlass's, over the same span.",
            read="Stopped replies and requests no log shows explain part of any difference. \"Left unexplained\" "
            "is the rest.",
            act="If more than 5% is left unexplained, ClaudeGlass's figures may be off, so please report it.",
        ),
    ),
    "model_swap": SectionCopy(
        title="Cheaper model: what if",
        intro="What your main session and each subagent type would have cost on a cheaper model, for the same tokens.",
        help=Help(
            shows="Your measured tokens repriced at every model's list price. Also the saving from moving each agent "
            "type one tier down: Fable to Opus, Opus to Sonnet, Sonnet to Haiku.",
            read="Real cost is measured. Costs at other models are recalculated, not observed: they assume the "
            "cheaper model uses the same tokens and replies. A smaller model may need more replies or fail "
            "the task, so every saving is the most you could save, not a forecast.",
            act="Try a cheaper model on the agent type with the biggest saving, on routine work first, and "
            "compare results before switching more. Set it with the model field in that agent's file. A "
            "workflow script, or a model named when a run starts, overrides that field.",
        ),
    ),
    "waste": SectionCopy(
        title="Replies that produced nothing",
        intro=(
            "How much you spent on replies you got nothing back from: failed tool calls, replies you "
            "stopped, and subagents stopped before they reported."
        ),
        help=Help(
            shows="Replies whose output you never used, grouped by cause, agent type and session.",
            read="Which replies were wasted is measured from your logs. Each one is priced at its full cost, so the "
            "total is the most you could recover. Some of that work would still have been needed.",
            act="Act when wasted cost is above 10% of your total. Start with the most expensive cause.",
        ),
    ),
    "config": SectionCopy(
        title="Your settings and their effect",
        intro=(
            "Which Claude Code settings were in effect, which file each came from, and how sessions "
            "differed after a setting changed."
        ),
        help=Help(
            shows="Your current settings per project, the file each one came from, and cost per session "
            "grouped by the value a setting had.",
            read="Settings come from snapshots the config hook takes when a session starts. Sessions that "
            "started before the first snapshot are left out of the comparisons.",
            act="Before you credit one setting for a cost change, check the note under its table. It lists "
            "the other settings that changed at the same time.",
        ),
    ),
    "context_budget": SectionCopy(
        title="How full the context window gets",
        intro=(
            "How much of the context window a session fills before any work, and when Claude Code "
            "summarises the conversation."
        ),
        help=Help(
            shows="What the main session's startup context is made of, when conversation summaries "
            "(compactions) start, and real context use where your status line logs it.",
            read="Columns marked (est.) are rough estimates from text length, about 4 characters per token. "
            "Claude Code's /context command gives the exact breakdown.",
            act="If startup context is large, trim its biggest part first. {{page:actions/recommendations}} names it.",
        ),
    ),
}

TABLE_COPY: dict[str, TableCopy] = {
    # -- subagent startup ---------------------------------------------------
    "agent_startup_breakdown": TableCopy(
        title="What each agent type is given at startup",
        help=Help(
            shows="One row per agent type. Each column is the average size of one part of the startup "
            "context, in tokens per spawn.",
            read="Startup size is everything the first reply had to read. The parts to its right add up "
            "to \"Share explained\"; the rest is \"Not recorded\".",
            act="A part that is large and repeated on every spawn is the cheapest thing to cut. CLAUDE.md "
            "can be switched off per agent; skills and tools can be narrowed in the agent file.",
        ),
        columns={
            "spawns": ("", "How many times this agent type started."),
            "fork_spawns": ("", "Spawns that copied the main conversation instead of starting fresh. Left out of the averages."),
            "startup_tokens": ("", "Everything the first reply read: task, instructions, tools and system prompt."),
            "task_prompt": ("", "The instructions the parent wrote when it started this agent."),
            "claude_md": ("", "Your CLAUDE.md files and auto memory, as sent to this agent."),
            "skills_listing": ("", "The list of skills the agent could call."),
            "tool_lists": ("", "Lists of deferred tools, MCP server instructions and other agent types it could start."),
            "hook_context": ("", "Text your hooks added before the first reply."),
            "other_attachments": ("", "Claude Code's own notes: environment, model, date and settings."),
            "system_prompt": ("", "Claude Code's system prompt and the agent's own prompt, when recorded."),
            "tool_definitions": ("", "The definitions of every tool the agent could call, when recorded."),
            "not_recorded": ("", "Startup size minus every part above. Mostly tool definitions and system prompt."),
            "measured_pct": ("", "How much of the startup size the parts to the left account for."),
            "write_price": (
                "",
                "List price of writing a million tokens into the cache for this agent's model. "
                "Used to put a price on each part.",
            ),
        },
        lead_columns=[
            "agent_type", "spawns", "startup_tokens", "claude_md", "skills_listing", "tool_lists", "task_prompt",
        ],
    ),
    "agent_startup_unused": TableCopy(
        title="Loaded at startup but never used",
        help=Help(
            shows="For each agent type: how often it was given skills, MCP tools and CLAUDE.md, and how "
            "often it actually used them.",
            read="Compare \"given\" with \"used\". A skills list given on 500 spawns and used on 2 is "
            "paid for 498 times for nothing.",
            act="When an agent almost never uses something it is given, narrow it in that agent's file. Set its "
            "tools, MCP servers or skills. Agents that only search or read can skip CLAUDE.md.",
        ),
        columns={
            "spawns": ("", "Spawns with a measured startup."),
            "skills_listing_tokens": ("", "Average size of the skills list per spawn, in tokens."),
            "skills_listed_spawns": ("", "Spawns that were given the skills list."),
            "skills_used_spawns": ("", "Spawns that called at least one skill."),
            "mcp_offered_spawns": ("", "Spawns that were offered at least one MCP tool."),
            "mcp_used_spawns": ("", "Spawns that called at least one MCP tool."),
            "claude_md_tokens": ("", "Average size of CLAUDE.md and memory per spawn, in tokens."),
            "read_only_spawns": ("", "Spawns given CLAUDE.md that only searched or read files."),
        },
        lead_columns=[
            "agent_type", "spawns", "skills_listing_tokens", "skills_used_spawns", "mcp_used_spawns",
            "claude_md_tokens", "read_only_spawns",
        ],
    ),
    "agent_startup_shared": TableCopy(
        title="Sent to most agent types",
        help=Help(
            shows="Startup parts that most agent types receive at about the same size, and where each "
            "one comes from.",
            read="\"Total across spawns\" is what that one source cost you in cache writes over the window.",
            act="A big shared source is worth trimming at the source. Move agent-specific sections out of CLAUDE.md "
            "into the agent files, or into skills loaded on demand.",
        ),
        columns={
            "part": ("", "The startup part."),
            "source": ("", "The file or feature that adds it."),
            "agent_types": ("", "How many agent types receive it, out of all measured types."),
            "mean_tokens": ("", "Average size per spawn, in tokens."),
            "total_tokens": ("", "Size per spawn times every spawn that received it."),
        },
        value_labels={
            "claude_md:User": "Global CLAUDE.md",
            "claude_md:Project": "Project CLAUDE.md",
            "claude_md:Local": "Project CLAUDE.local.md",
            "claude_md:AutoMem": "Auto memory",
            "claude_md:Managed": "Managed policy CLAUDE.md",
            "claude_md:Nested": "Subfolder CLAUDE.md and rules",
            "claude_md:Other": "Other instruction files",
            "skills_listing": "Skills list",
            "tool_lists": "Tool and agent lists",
            "hook_context": "Hook output",
            "other_attachments": "Environment and settings notes",
            "system_prompt": "System prompt",
            "tool_definitions": "Tool definitions",
        },
    ),
    # -- quality signals ----------------------------------------------------
    "habits_digest": TableCopy(
        title="",  # UX-4/7: the builder's title names the actual day span
        help=Help(
            shows="The three habits worth the most to you right now, and what the habits you already picked up are "
            "saving. Also what a piece of work that met its goal cost.",
            read="Savings are a week's worth at your recent pace once there's a week of it. Under 7 days, it's the "
            "raw total so far, not stretched into a weekly rate. A habit counts as picked up when what it addresses "
            "per message fell by a fifth or more over recent weeks.",
            act="Start with the first habit: Habits worth trying below has an example to copy for each.",
        ),
        columns={
            "item": ("", "Which figure this is."),
            "what": ("", "The habit, or what the figure measures."),
            "value": ("", "A week's saving for a habit, or the figure itself."),
            "detail": ("", "What your sessions show behind it."),
        },
        value_labels={
            "top_1": "Worth the most",
            "top_2": "Next",
            "top_3": "Then",
            "adopted": "Already saving",
            "cost_per_met": "Cost per goal met",
            "tagged": "Messages tagged",
        },
        row_kinds={
            "top_1": "money",
            "top_2": "money",
            "top_3": "money",
            "adopted": "money",
            "cost_per_met": "money",
            "tagged": "pct",
        },
    ),
    "habits_playbook": TableCopy(
        title="Habits worth trying",
        help=Help(
            shows="Ways of working that would have cost less in your own sessions. They cover how you break work "
            "down, brief Claude, research, plan, use skills, delegate to agents and check changes.",
            read="Each saving is rough, with how it was worked out alongside. Source says where the evidence came "
            "from: inferred from the transcripts, reported by Claude in metrics-capture tags, or your own feedback. "
            "Your feedback outranks the rest. By week shows what the habit addresses per message over recent weeks, "
            "scaled so the worst week is 100. A dash is a week with too few messages.",
            act="Copy the example into your next message of that kind. Turning on metrics capture adds the "
            "reported evidence and makes the estimates firmer.",
        ),
        columns={
            "habit": ("Habit", "The habit to try."),
            "theme": ("Theme", "Which part of how you work it's about."),
            "saving": ("Saving a week, about", "What the habit would have saved, spread over the weeks shown."),
            "evidence": ("What your sessions show", "The evidence behind it, as counts and ratios."),
            "example": ("Try", "An example to copy or adapt."),
            "basis": ("How the saving is worked out", "What the estimate counts."),
            "n": ("Seen", "How many times the evidence turned up."),
            "source": ("Source", "Inferred from the transcripts, reported by Claude, or your feedback."),
            "confidence": ("Confidence", "High with 20 or more cases, medium with 8 or more, else low."),
            "trend": ("Trend", "Whether it's getting better or worse over recent weeks."),
            "weeks": ("By week", "What it addresses per message, by week, the worst week as 100."),
            "where": ("Where and who it affects", "Where trying this habit shows up and who it affects."),
            "trade_off": ("Trade-off", "What trying this habit costs or risks."),
            "how_to_undo": ("How to undo it", "How to go back if it doesn't work out."),
            "covered_by": (
                "Already covered by",
                "The recommendation that already reports this saving, if there is one. Blank otherwise.",
            ),
            "covered_by_rule": (
                "Covering rule",
                "The id of the recommendation in \"Already covered by\", for linking to it. Blank otherwise.",
            ),
        },
        value_labels={
            **{key: title for key, (_theme, title) in HABIT_ITEMS.items()},
            **CAPTURE_THEMES,
            "high": "High",
            "medium": "Medium",
            "low": "Low",
            "falling": "Improving",
            "rising": "Getting worse",
            "steady": "Steady",
            "new": "Too early to say",
        },
        lead_columns=["habit", "saving", "evidence", "example", "confidence", "trend", "theme"],
    ),
    "habits_by_task": TableCopy(
        title="Kinds of task",
        help=Help(
            shows="What each kind of task cost, as Claude reported it in metrics-capture tags, with every message "
            "in the first row.",
            read="Clear asks and large asks are shares of the messages Claude rated for them. Redone counts "
            "messages whose next message redid the work, fixed a fault in it or corrected Claude. Met the goal "
            "needs your feedback.",
            act="The costliest kinds are where the brief templates and the habits above pay off most.",
        ),
        columns={
            "task": ("Task", "The kind of task Claude reported."),
            "cycles": ("Messages", "Your messages of this kind, each with the work that answered it."),
            "share": ("Share", "Out of all your messages."),
            "cost": ("Cost", "What the work cost, subagents included."),
            "avg_cost": ("Per message", "The average cost of one."),
            "main_cost": ("Cost (main session only)", "What the work cost the main session alone, leaving out "
                          "any subagents it spawned."),
            "clear_pct": ("Clear asks", "Messages Claude called clear, out of those it rated."),
            "large_pct": ("Large asks", "Messages Claude sized large or extra large."),
            "redo_pct": ("Redone", "Messages whose work was redone, fixed or corrected by your next message."),
            "met_pct": ("Met the goal", "Pieces you said met their goal, out of those you gave feedback on."),
        },
        value_labels={"all": "All messages"},
        lead_columns=["task", "cycles", "share", "cost", "avg_cost", "redo_pct", "met_pct"],
    ),
    "habits_briefs": TableCopy(
        title="How clear your asks were",
        help=Help(
            shows="Your messages by how clear Claude said they were, and what each cost.",
            read="Claude judges your message, so treat it as a sign. A vague ask that costs much more than a "
            "clear one is the pattern to look for.",
            act="Most often missing names what to add; Brief templates has a checklist per kind of task.",
        ),
        columns={
            "brief": ("Brief", "How clear Claude said the message was."),
            "cycles": ("Messages", "Messages it rated this way."),
            "avg_cost": ("Per message", "The average cost of the work."),
            "redo_pct": ("Redone", "Messages whose work was redone, fixed or corrected next."),
            "met_pct": ("Met the goal", "From your feedback."),
            "missing": ("Most often missing", "What Claude said the message left out."),
        },
        value_labels={"clear": "Clear", "partial": "Partly clear", "vague": "Vague"},
    ),
    "habits_brief_templates": TableCopy(
        title="Brief templates",
        help=Help(
            shows="A checklist per kind of task, built from what your own asks most often left out.",
            read="Without metrics capture these are starting points; with it, the lines your asks miss most "
            "come first.",
            act="Copy the template into your message and fill it in. The optional /tl-brief skill gives Claude "
            "the same checklists: claudeglass capture brief on.",
        ),
        columns={
            "task": ("Task", "The kind of task."),
            "checklist": ("Checklist", "What to include."),
            "why": ("Why these", "The evidence for the order."),
            "template": ("Template", "The lines to copy and fill in."),
        },
    ),
    "habits_agents": TableCopy(
        title="How agents were used",
        help=Help(
            shows="Each subagent type: its reports, whether it finished and why it was retried. Also whether Claude "
            "thought its model fit the work, whether it used your CLAUDE.md, and files it read again. The main "
            "session's row says how hard its work was.",
            read="Model fit and CLAUDE.md use are the agent's own report, so they only hold a cheaper model "
            "back and never push one. Files read again are files the main session had already read.",
            act="A cheaper model isn't suggested for an agent whose runs said they needed a larger one or "
            "were mostly hard work. For long reports, ask for a short one in the brief.",
        ),
        columns={
            "agent_type": ("Agent", "The subagent type."),
            "runs": ("Runs", "Its runs, at any depth; for the main session, your messages."),
            "cost": ("Cost", "What the runs cost."),
            "report_tokens": ("Report", "A typical report handed back, in tokens."),
            "capped_pct": ("Asked for a short report", "Briefs that capped the report's length."),
            "done_pct": ("Finished", "Runs that said done, out of those that said."),
            "retried": ("Retried", "Runs started again with a reason."),
            "retried_model": ("Retried for the model", "Retries that said the model wasn't enough."),
            "fit_smaller": ("Smaller would do", "Runs that said a smaller model would have done."),
            "fit_right": ("Model was right", "Runs that said the model fit."),
            "fit_larger": ("Needed larger", "Runs that said a larger model would have done better."),
            "rules_used": ("Used CLAUDE.md", "Runs that said they used your CLAUDE.md."),
            "rules_unused": ("Didn't use CLAUDE.md", "Runs that said they didn't."),
            "easy_pct": ("Easy work", "Runs on messages Claude called easy."),
            "hard_pct": ("Hard work", "Runs on messages Claude called hard."),
            "overlap_reads": ("Files read again", "Files it read that the main session had already read."),
            "nested": ("Started by an agent", "Runs another agent started."),
        },
        value_labels={"top-level": "Main session"},
        lead_columns=["agent_type", "runs", "cost", "done_pct", "retried", "fit_smaller", "report_tokens"],
    ),
    "habits_effort_fit": TableCopy(
        title="Effort against how hard the work was",
        help=Help(
            shows="Your messages by how hard Claude said the work was and the effort it ran at.",
            read="Easy work at high effort spends thinking it doesn't need. Hard work at low effort that was "
            "often redone needed more.",
            act="Lower the effort (/effort, or your effort level setting) for quick edits and raise it for hard "
            "problems.",
        ),
        columns={
            "setup": ("Work and effort", "How hard the work was, and the effort it ran at."),
            "cycles": ("Messages", "Messages with this pairing."),
            "avg_cost": ("Per message", "The average cost of the work."),
            "thinking_pct": ("Thinking share of output", "Output that was thinking."),
            "redo_pct": ("Redone", "Messages whose work was redone, fixed or corrected next."),
            "met_pct": ("Met the goal", "From your feedback."),
            "saving": ("Lower effort would save, about", "Half the thinking on easy work at high effort or above."),
        },
        value_labels={
            f"{level}:{effort}": f"{level.capitalize()} work, {effort_label}"
            for level in ("easy", "normal", "hard")
            for effort, effort_label in (
                ("low", "low effort"),
                ("medium", "medium effort"),
                ("high", "high effort"),
                ("xhigh", "extra high effort"),
                ("max", "max effort"),
                ("default", "default effort"),
            )
        },
    ),
    "habits_setups": TableCopy(
        title="Best setup for each kind of task",
        help=Help(
            shows="Each kind of task Claude reported, all together and by how hard it said the work was. Each is "
            "split by the exact model, effort and speed that answered it. It shows what a message cost and how often "
            "the work went well.",
            read="Went well is your feedback where you gave it, otherwise whether your next message redid, fixed or "
            "corrected the work. "
            "The last message of each session is left out, since nothing after it confirms how it went. Shown from 5 "
            "messages each side. A cheaper setup that's mostly hard work at the all-levels row is held back, even if "
            "nothing else looks wrong. The hard work is what made it look cheap, not the setup itself. A cheaper "
            "setup is only ticked to apply once it has at least 20 messages behind it.",
            act="On {{page:setup/profiles}}, start from the goal A profile for one kind of task, save it, and launch "
            "Claude with it for that kind of work.",
        ),
        columns={
            "task": ("Task", "The kind of task Claude reported."),
            "level": ("How hard", "How hard Claude said the work was. All is every level together."),
            "model": ("Model", "The exact model version most of the work ran on."),
            "effort": ("Effort", "The effort it ran at."),
            "speed": ("Speed", "Standard or fast mode."),
            "cycles": ("Messages", "Messages with this setup."),
            "avg_cost": ("Per message", "The average cost of the work, subagents included."),
            "main_avg_cost": ("Per message (main session only)", "The average cost of the main session's own "
                               "share of the work, leaving out any subagents it spawned."),
            "ok_pct": ("Went well", "Met its goal by your feedback, or not redone by your next message."),
            "rated": ("With your feedback", "Messages your feedback covers."),
            "verdict": ("Setup", "Your usual setup, and the cheaper one that did as well."),
            "saving_pct": ("Cheaper by", "Per message, against your usual setup, level for level."),
        },
        value_labels={
            "all": "All",
            "easy": "Easy",
            "normal": "Normal",
            "hard": "Hard",
            "default": "Default",
            "usual": "Your usual",
            "cheaper": "Cheaper, did as well",
        },
        lead_columns=["task", "level", "model", "effort", "avg_cost", "verdict", "saving_pct"],
    ),
    "habits_agents_by_task": TableCopy(
        title="Agents by kind of task",
        help=Help(
            shows="Each kind of task Claude reported, split by the subagent type that answered it. It shows the runs, "
            "what a run cost, whether it finished, and whether its runs said the model fit.",
            read="Finished and model fit are the agent's own reports: the same signals as How agents were used, split "
            "by task. A cheaper model is named only when three things hold. Enough runs of that pairing point to it. "
            "None of them said it needed a larger model or was mostly hard work. And the quality check hasn't found "
            "that model worse for this agent.",
            act="On {{page:setup/profiles}}, the goal A profile for one kind of task offers this cheaper model as a "
            "candidate when the evidence here supports one.",
        ),
        columns={
            "task": ("Task", "The kind of task Claude reported."),
            "agent_type": ("Agent", "The subagent type that answered it."),
            "runs": ("Runs", "Its runs on this task, at any depth."),
            "avg_cost": ("Per run", "The average cost of a run."),
            "done_pct": ("Finished", "Runs that said done, out of those that said."),
            "fit_smaller": ("Smaller would do", "Runs that said a smaller model would have done."),
            "fit_right": ("Model was right", "Runs that said the model fit."),
            "fit_larger": ("Needed larger", "Runs that said a larger model would have done better."),
            "cheaper_model": ("Cheaper model", "A cheaper model the evidence supports for this pairing, if any."),
            "cheaper_saving_pct": ("Cheaper by", "What that model would have saved, against the model it ran on."),
        },
        lead_columns=["task", "agent_type", "runs", "avg_cost", "done_pct", "cheaper_model", "cheaper_saving_pct"],
    ),
    "habits_outcomes": TableCopy(
        title="Did the work meet its goal?",
        help=Help(
            shows="The pieces of work you gave feedback on, with /tl-feedback or a rating on {{page:spend/sessions}}, "
            "by outcome.",
            read="A /tl-feedback answer rates the messages since the last one; a rating covers the whole "
            "session. Misses that cost much more than work that met its goal are worth a look.",
            act="Slowed most by and Would have helped most say what to change first.",
        ),
        columns={
            "outcome": ("Outcome", "What you said about the result."),
            "pieces": ("Pieces of work", "Answers or ratings with this outcome."),
            "cycles": ("Messages", "The messages they cover."),
            "cost": ("Cost", "What that work cost."),
            "avg_cost": ("Per piece", "The average cost of one piece."),
            "task": ("Most often", "The kind of task Claude reported most for them."),
            "slow": ("Slowed most by", "Your most common answer to what slowed it down."),
            "helped": ("Would have helped most", "Your most common answer to what would have helped."),
            "source": ("Source", "Where the feedback came from."),
        },
        value_labels={"met": "Met", "partly": "Partly", "missed": "Missed", "stopped": "Stopped early"},
        lead_columns=["outcome", "pieces", "cost", "avg_cost", "task", "slow", "helped"],
    ),
    "habits_by_shape": TableCopy(
        title="Planning and building in one session",
        help=Help(
            shows="Your main sessions by whether you approved a plan and then built it in the same session. "
            "Your feedback on each kind of session sits beside it.",
            read="Planning kept is the context from before the plan that the build carried. The last three "
            "columns count your /tl-feedback answers on whether the build could have started from the plan "
            "alone.",
            act="If most of your sessions plan and build and the plan was enough, start the build in a fresh "
            "session. If the build needed the discussion, write fuller plans first.",
        ),
        columns={
            "shape": ("Session", "Whether you approved a plan, and built it in the same session."),
            "sessions": ("Sessions", "Main sessions of this kind."),
            "share": ("Share", "Their share of your main sessions."),
            "avg_cost": ("Per session", "The average cost of one, subagents included, at list price."),
            "carried_median": ("Planning kept (median)", "The median context, in tokens, that a fresh start "
                               "from the plan would have dropped."),
            "pieces": ("Pieces rated", "Pieces of work in these sessions you gave feedback on."),
            "met_pct": ("Met the goal", "Of the rated pieces, the share that met its goal."),
            "worth_pct": ("Worth it", "Of the pieces you said were or weren't worth it, the share worth it."),
            "costly_pct": ("Too costly", "Of the same pieces, the share you said cost too many tokens."),
            "handoff_yes": ("Plan was enough", "Answers saying the build could have started fresh from the plan."),
            "handoff_partly": ("Plan was partly enough", "Answers saying it needed a few things from earlier."),
            "handoff_no": ("Needed the discussion", "Answers saying it relied on the earlier discussion."),
        },
        value_labels={
            "plan_build": "Planned and built in one session",
            "plan_only": "Planned, then built elsewhere",
            "no_plan": "No plan",
        },
        lead_columns=["shape", "sessions", "share", "avg_cost", "carried_median", "met_pct", "handoff_yes"],
    ),
    "habits_self_report": TableCopy(
        title="Claude's reports against your feedback",
        help=Help(
            shows="Each level and brief quality Claude reported, against your feedback. It shows how many messages "
            "each covers, the share that met or missed its goal, and the share your next message redid, fixed or "
            "corrected.",
            read="Met and missed are out of the messages your feedback covers. Redone counts every message tagged "
            "this way, feedback or not. A note below the table says whether work Claude called easy missed its goal "
            "more often than normal work. It appears once there is enough feedback on both to tell.",
            act="If the note says easy work misses more than normal work, treat what Claude calls easy with "
            "caution, including the effort suggestion built from it.",
        ),
        columns={
            "signal": ("What Claude reported", "The level or brief quality it tagged the message with."),
            "cycles": ("Messages", "Messages tagged this way."),
            "rated": ("With your feedback", "Of those, the messages your feedback covers."),
            "met_pct": ("Met the goal", "Of the rated messages, the share that met its goal."),
            "missed_pct": ("Missed", "Of the rated messages, the share that missed its goal."),
            "redone_pct": ("Redone by your next message", "Of all messages tagged this way, the share your "
                           "next message redid, fixed or corrected."),
        },
        value_labels={
            "level:easy": "Called easy",
            "level:normal": "Called normal",
            "level:hard": "Called hard",
            "brief:clear": "Called a clear brief",
            "brief:partial": "Called a partial brief",
            "brief:vague": "Called a vague brief",
        },
    ),
    "habits_prompt_flags": TableCopy(
        title="What your messages contained",
        help=Help(
            shows="How often your messages named a file, had code, an error, a link, done criteria, numbered steps, a "
            "length cap or pasted text. Also what the work cost with and without each. Always measured, no metrics "
            "capture needed.",
            read="Only whether each was there is kept, never the text. Bigger asks tend to carry more of "
            "everything, so compare the reads and searches as well as the cost.",
            act="If messages that name a file need far fewer reads and searches, name the files you know.",
        ),
        columns={
            "flag": ("Contained", "What the message had in it."),
            "cycles": ("Messages", "Messages that had it."),
            "share": ("Share", "Out of all your messages."),
            "avg_with": ("Per message with it", "The average cost of the work when it had it."),
            "avg_without": ("Per message without it", "The same, when it didn't."),
            "reads_with": ("Reads and searches with it", "Main-session reads and searches per message."),
            "reads_without": ("Reads and searches without it", "The same, without it."),
        },
        value_labels={
            "path": "A file path",
            "code": "A code block",
            "error": "An error or stack trace",
            "url": "A link",
            "done": "What done looks like",
            "steps": "Numbered steps",
            "short": "A length cap",
            "paste": "Pasted text",
        },
    ),
    "habits_skills": TableCopy(
        title="When skills ran",
        help=Help(
            shows="Each skill you ran or Claude loaded, how late Claude reached for it, and whether Claude said "
            "it helped.",
            read="Loaded late means after three or more replies to the message, when the work so far could "
            "have been guided by it from the start.",
            act="Run a skill Claude keeps reaching for late yourself at the start. One that often wasn't "
            "needed can be set to load only when you run it (disable-model-invocation).",
        ),
        columns={
            "skill": ("Skill", "The skill's name."),
            "by_you": ("You ran it", "Times you ran it with a slash command."),
            "by_claude": ("Claude loaded it", "Times Claude loaded it itself."),
            "late": ("Loaded late", "Times Claude loaded it after three or more replies."),
            "before": ("Spent before it, typical", "What the message had cost before a late load."),
            "helped": ("Helped", "Messages where Claude said the skill helped."),
            "unneeded": ("Wasn't needed", "Messages where Claude said it wasn't needed."),
            "would_help": ("Would have helped", "Messages where Claude said it would have helped."),
        },
        lead_columns=["skill", "by_you", "by_claude", "late", "before", "helped", "unneeded"],
    ),
    "habits_tool_output": TableCopy(
        title="Big tool output and failing commands",
        help=Help(
            shows="Tool results of 8,000 tokens or more in one reply, per tool, and what keeping them in context "
            "cost. Also commands that failed again and again within one message.",
            read="Carrying is priced from the next reply to the next compaction, a cache write and then a "
            "read per reply, so it's a floor.",
            act="Ask for quieter output (only failures, a tail, an offset read) and to stop after two failed "
            "attempts at the same command.",
        ),
        columns={
            "tool": ("Tool", "The tool that returned it."),
            "outputs": ("Big outputs", "Replies that got 8,000 tokens or more back from it."),
            "tokens": ("Tokens", "Their size together."),
            "cost": ("Carrying them cost", "What keeping them in context cost."),
            "loops": ("Commands failing again and again", "Commands that failed three or more times in one message."),
        },
        value_labels={"loops": "Failing commands"},
    ),
    "capture_usage": TableCopy(
        title="What metrics capture cost",
        help=Help(
            shows="What metrics capture cost since it was turned on, measured from the transcripts. That covers the "
            "notes that ask Claude for tags, the tags Claude wrote and /tl-feedback runs, with a weekly rate. It also "
            "shows what that buys you: the habits worth trying whose evidence needs capture or your feedback.",
            read="Share is out of what the captured sessions cost. Coverage is how many messages and agent reports "
            "carried the tag they were asked for. What it's worth only counts habits whose evidence is reported by "
            "Claude or your feedback, not everything the report finds. When nothing measured yet depends on either, "
            "it says so instead of showing a zero.",
            act="{{page:setup/capture}} turns metrics on and off, one by one or by level. Once what it's worth "
            "clears what it costs by a comfortable margin, a lower level or fewer metrics may do.",
        ),
        columns={
            "metric": ("", "What is measured."),
            "value": ("", "The figure."),
        },
        value_labels={
            "level": "Level",
            "since": "On since",
            "note_tokens": "Notes, tokens",
            "tag_tokens": "Tags, tokens",
            "cost": "Cost",
            "share": "Share of spend",
            "coverage": "Messages tagged",
            "report_coverage": "Agent reports tagged",
            "feedback_runs": "Feedback runs",
            "feedback_cost": "Feedback cost",
            "weekly_cost": "Cost a week",
            "habit_value": "What it's worth a week",
        },
        row_kinds={
            "note_tokens": "tokens",
            "tag_tokens": "tokens",
            "cost": "money",
            "share": "pct",
            "coverage": "pct",
            "report_coverage": "pct",
            "feedback_runs": "int",
            "feedback_cost": "money",
            "weekly_cost": "money",
            "habit_value": "money",
        },
    ),
    "quality_by_agent": TableCopy(
        title="Quality signals by agent",
        help=Help(
            shows="For the main session and each agent type, how often work went badly, and how much work a run "
            "took.",
            read="Each figure is a share of what your logs counted, so an agent with few runs can swing a lot. "
            "Read it with the number of runs.",
            act="An agent may often fail to finish, have its tool calls fail, or need running again on a larger "
            "model. That agent needs a clearer task prompt, the right tools, or a stronger model or effort.",
        ),
        columns={
            "agent_type": ("Agent", "The main session, or the subagent type."),
            "runs": ("Runs", "Main sessions or subagent runs counted in this row."),
            "unfinished_pct": (
                "Didn't finish",
                "Subagent runs that reported failure, were stopped, ended early or were cut off before they answered, "
                "out of the runs where that is known. Ending a reply with [result: partial] or [result: blocked] "
                "counts too.",
            ),
            "turn_limit_pct": (
                "Likely out of turns",
                "Subagent runs cut off right after a tool result came back, without being stopped. That is how a run "
                "ends when it reaches its turn limit. Claude Code doesn't record the reason, so this is likely, not "
                "certain.",
            ),
            "retried_pct": (
                "Retried on a larger model",
                "Subagent runs that were run again on a larger model, out of the runs that edited files or were "
                "retried. The rerun edited the same files within two hours, or its brief started [retry: model]. It "
                "means the cheaper model wasn't enough for that work.",
            ),
            "tool_errors_pct": (
                "Failed tool calls",
                "Tool calls whose result came back as an error, out of all tool calls.",
            ),
            "shell_errors_pct": (
                "Failed shell commands",
                "Shell commands that returned an error, out of all shell commands.",
            ),
            "denials_pct": ("Denied by you", "Tool calls you declined when asked, out of all tool calls."),
            "interrupts_pct": (
                "Stopped by you",
                "Replies you stopped midway, out of all replies. Main session only.",
            ),
            "corrections_pct": (
                "Corrections",
                "Your messages that looked like a correction, such as \"that's wrong\" or \"still broken\", out of all "
                "your messages. Main session only.",
            ),
            "rework_pct": (
                "Edited again",
                "Edits to a file that had already been changed before your last message, out of all edits. Main "
                "session only.",
            ),
            "max_tokens_pct": (
                "Hit output limit",
                "Replies cut off because they reached the output limit, out of all replies.",
            ),
            "replies_per_run": ("Replies per run", "Average replies in one main session or one subagent run."),
            "cost_per_run": ("Cost per run", "Average cost of one run, at list price."),
        },
        value_labels={"(main session)": "Main session", "(all subagents)": "All subagents"},
        lead_columns=[
            "agent_type", "runs", "unfinished_pct", "retried_pct", "tool_errors_pct", "corrections_pct", "cost_per_run",
        ],
    ),
    "quality_by_setup": TableCopy(
        title="Quality by model and effort",
        help=Help(
            shows="Each agent's runs split by the model and effort they used, with each setup compared against "
            "the one that agent used most. Main sessions a scheduled task started, with no message of yours, are "
            "left out.",
            read="Worse or Better means the difference is unlikely to be chance, even allowing for the number of "
            "signals compared. Possibly means it would be, taken alone. The setups ran at different times and "
            "maybe on different work. Check the before and after on {{page:changes}} too.",
            act="If a cheaper setup is marked worse, move that agent back to the setup it is compared with.",
        ),
        columns={
            "agent_type": ("Agent", "The main session, or the subagent type."),
            "model": ("Model", "The model most of the run's replies used."),
            "effort": (
                "Effort",
                "The effort most of the run's replies were sent at. Default when none was recorded.",
            ),
            "runs": ("Runs", "Main sessions or subagent runs counted in this row."),
            "unfinished_pct": (
                "Didn't finish",
                "Subagent runs that reported failure, were stopped, ended early or were cut off before they answered, "
                "out of the runs where that is known. Ending a reply with [result: partial] or [result: blocked] "
                "counts too.",
            ),
            "retried_pct": (
                "Retried on a larger model",
                "Subagent runs that were run again on a larger model, out of the runs that edited files or were "
                "retried. The rerun edited the same files within two hours, or its brief started [retry: model]. It "
                "means the cheaper model wasn't enough for that work.",
            ),
            "tool_errors_pct": (
                "Failed tool calls",
                "Tool calls whose result came back as an error, out of all tool calls.",
            ),
            "shell_errors_pct": (
                "Failed shell commands",
                "Shell commands that returned an error, out of all shell commands.",
            ),
            "max_tokens_pct": (
                "Hit output limit",
                "Replies cut off because they reached the output limit, out of all replies.",
            ),
            "replies_per_run": ("Replies per run", "Average replies in one main session or one subagent run."),
            "tool_calls_per_run": ("Tool calls per run", "Average tool calls in one run."),
            "cost_per_run": ("Cost per run", "Average cost of one run, at list price."),
            "compared_with": (
                "Compared with",
                "The model and effort this agent used most; others are compared with it.",
            ),
            "setup_verdict": (
                "Verdict",
                "Worse if any signal is clearly worse than in the setup it is compared with, and none clearly better. "
                "Mixed if some are clearly worse and others clearly better. Better if one is clearly better and none "
                "worse. Not comparable if its runs averaged over five times as many replies as the setup it would be "
                "compared with, or under a fifth. The work differs too much to judge the setup.",
            ),
            "difference": (
                "Difference",
                "Every signal that differs, with its value here and in the setup it is compared with.",
            ),
            "compared_model": ("Compared model", "The model of the setup it is compared with."),
            "compared_effort": ("Compared effort", "The effort of the setup it is compared with."),
        },
        value_labels={
            "(main session)": "Main session",
            "(all subagents)": "All subagents",
            "worse": "Worse",
            "possibly_worse": "Possibly worse",
            "mixed": "Mixed",
            "better": "Better",
            "possibly_better": "Possibly better",
            "no_clear_difference": "No clear difference",
            "too_little_data": "Too little data",
            "not_comparable": "Not comparable",
            "baseline": "Most used",
            "only": "Only setup",
        },
        lead_columns=["agent_type", "model", "effort", "runs", "cost_per_run", "setup_verdict", "difference"],
    ),
    "quality_retried": TableCopy(
        title="Agent runs retried on a larger model",
        help=Help(
            shows="Each agent and model where the same agent was run again on a larger model soon after one of its "
            "runs ended. The rerun edited the same files: a sign the cheaper model wasn't enough for that work.",
            read="Unless the retry's brief says why ([retry: ...], see Why agents were run again), one retry is a "
            "sign, not proof. Several, or a large share of the runs, is a pattern. A different agent or the main "
            "session editing the files afterwards isn't counted, unless its brief said [retry: model]. A reviewer "
            "after a writer is often the plan. A retry whose brief said the brief, tools or something else was the "
            "problem is never counted.",
            act="Once a tenth of an agent's runs on a model were retried on a larger one, that model isn't "
            "suggested for that agent. If its agent file is on that model and it happened twice or more, Quick "
            "actions offers to move it back up.",
        ),
        columns={
            "agent_type": ("Agent", "The subagent type that was retried."),
            "model": ("Model", "The model those runs used."),
            "runs": ("Runs that edited files", "This agent's runs on this model that edited at least one file."),
            "retried": (
                "Retried on a larger model",
                "Of those, runs after which the same agent was started again on a larger model in the same session. "
                "The rerun edited one of the same files within two hours.",
            ),
            "retried_pct": ("Share retried", "Retried runs out of the runs that edited files."),
            "said_model": (
                "Retries that said the model wasn't enough",
                "Of the retried runs, those whose retry began its brief with [retry: model].",
            ),
            "files_edited_again": (
                "Files edited again",
                "Files the larger model edited again, summed over the retried runs.",
            ),
            "files_edited": ("Files those runs edited", "Every file the retried runs edited, for comparison."),
            "retried_on": ("Retried on", "The model the retries most often used."),
            "last_retried": ("Last time", "The day the latest retried run ended."),
        },
        lead_columns=["agent_type", "model", "runs", "retried", "retried_pct", "retried_on", "last_retried"],
    ),
    "quality_retry_reasons": TableCopy(
        title="Why agents were run again",
        help=Help(
            shows="Each agent and model whose runs were started again with a brief that said why. The reasons are "
            "[retry: model], [retry: brief], [retry: tools], [retry: scope] or [retry: other]. Claude writes these "
            "when metrics capture or CLAUDE.md asks it to (Quick actions, \"Is any agent struggling?\").",
            read="Only retries that said why are here, on any model. A retry is matched to the agent run that ended "
            "last before it started, within two hours. It prefers a run whose files it edited, then one of the same "
            "agent type.",
            act="The model: a larger model is worth trying (Agent runs retried on a larger model counts these). The "
            "brief: say what done looks like in the task prompt. Tools: give the agent the tools and permissions it "
            "needs. The task: split the work smaller before handing it over.",
        ),
        columns={
            "agent_type": ("Agent", "The agent type whose run was retried."),
            "model": ("Model", "The model the retried runs used."),
            "retries": ("Retries that said why", "Runs of this agent on this model whose retry gave a reason."),
            "said_model": ("The model", "Retries that said it needed a stronger model."),
            "said_brief": ("The brief", "Retries that said the instructions were unclear."),
            "said_tools": ("Tools", "Retries that said it lacked a tool or permission."),
            "said_scope": ("The task", "Retries that said the task itself changed or was cut too wide."),
            "said_other": ("Other", "Retries that gave another reason."),
            "last_retried": ("Last time", "The day the latest of these retried runs ended."),
        },
        lead_columns=["agent_type", "model", "retries", "said_model", "said_brief", "said_scope", "said_tools"],
    ),
    "quality_markers": TableCopy(
        title="Markers Claude wrote",
        help=Help(
            shows="How often agent runs carried each marker Claude writes when CLAUDE.md asks it to, and about what "
            "writing them cost.",
            read="Only the word in the marker is kept. Explore and Plan start without CLAUDE.md, so they aren't "
            "counted among the runs that could have ended with a result marker. None at all usually means "
            "CLAUDE.md doesn't ask for them yet.",
            act="Quick actions (\"Is any agent struggling?\") offers the CLAUDE.md lines when no marker has been "
            "seen.",
        ),
        columns={
            "marker": ("Marker", "The marker Claude writes."),
            "what": ("What it records", "What the marker says."),
            "runs": ("Agent runs with it", "Subagent runs that carried this marker."),
            "of_runs": ("Agent runs that could have", "Subagent runs that could have carried it."),
            "share": ("Share", "Runs with the marker out of the runs that could have."),
            "breakdown": ("Said", "How many said each word."),
            "tokens": ("Output tokens, about", "About six output tokens per marker."),
            "cost": ("Cost, about", "Those tokens at the output price of the model that wrote each."),
        },
        lead_columns=["marker", "what", "runs", "of_runs", "share", "breakdown", "cost"],
    ),
    "quality_failing_tools": TableCopy(
        title="Which tools failed",
        help=Help(
            shows="The tools whose calls failed most, per agent.",
            read="A tool that fails in many runs points at a missing permission, a wrong path or a tool the "
            "agent shouldn't have.",
            act="For a tool that keeps failing, give that agent the permission, or say in its task prompt how "
            "to use the tool.",
        ),
        columns={
            "agent_type": ("Agent", "The main session, or the subagent type."),
            "tool": ("Tool", "The tool that was called."),
            "errors": ("Failed calls", "Calls to this tool whose result came back as an error."),
            "runs_with_errors": ("Runs with a failure", "Runs where this tool failed at least once."),
        },
        value_labels={"(main session)": "Main session", "(all subagents)": "All subagents"},
    ),
    "quality_counts": TableCopy(
        title="Quality signal counts",
        help=Help(
            shows="The raw counts behind every quality signal.",
            read="Use these to see how many of something a share is based on.",
            act="",
        ),
        columns={
            "agent_type": ("Agent", "The main session, or the subagent type."),
            "runs": ("Runs", "Main sessions or subagent runs counted in this row."),
            "replies": ("Replies", "Model replies."),
            "tool_calls": ("Tool calls", "Every tool call."),
            "tool_errors": ("Failed tool calls", "Tool calls whose result came back as an error."),
            "shell_calls": ("Shell commands", "Bash and PowerShell calls."),
            "shell_errors": ("Failed shell commands", "Shell commands that returned an error."),
            "denials": ("Denied", "Tool calls you declined."),
            "interrupts": ("Stopped by you", "Replies you stopped."),
            "human_messages": ("Your messages", "Messages you typed in the main session."),
            "corrections": ("Corrections", "Your messages that looked like a correction."),
            "edits": ("Edits", "File edits and writes."),
            "rework_edits": ("Edited again", "Edits to a file already changed before your last message."),
            "retried_files": (
                "Edited again on a larger model",
                "Files a subagent edited that the same agent, run again on a larger model, edited soon after.",
            ),
            "max_tokens": ("Hit output limit", "Replies cut off at the output limit."),
            "api_errors": ("API errors", "Errors from the API, usually retried automatically."),
            "fallbacks": ("Model fallbacks", "Times Claude Code switched to another model after an error."),
            "compactions": ("Summaries", "Times the conversation was summarised."),
            "outcome_completed": ("Reported done", "Subagent runs whose notification or result said completed."),
            "outcome_failed": ("Reported failure", "Subagent runs that reported failure."),
            "outcome_stopped": ("Stopped", "Subagent runs stopped before they finished."),
            "outcome_other": ("Other outcome", "Subagent runs with another reported status."),
            "outcome_unknown": ("No outcome recorded", "Subagent runs with no notification or result to read."),
            "cut_off": (
                "Cut off",
                "Subagent runs that were stopped, never replied, or whose last reply asked for a tool and never "
                "answered.",
            ),
            "turn_limit": (
                "Likely out of turns",
                "Cut-off runs that ended right after a tool result came back, without being stopped: most likely they "
                "reached their turn limit.",
            ),
            "retried": (
                "Retried on a larger model",
                "Subagent runs after which the same agent was run again on a larger model and edited the same files. "
                "A larger model started with a brief that said [retry: model] counts too.",
            ),
            "terminated_early": (
                "Ended early",
                "Subagent runs Claude Code ended early, for example at a rate limit.",
            ),
            "never_replied": ("Never replied", "Runs with no model reply at all. Counted as cut off."),
            "said_done": ("Said done", "Subagent runs whose last reply ended [result: done]."),
            "said_partial": (
                "Said partly done",
                "Subagent runs whose last reply ended [result: partial]. Counted as didn't finish.",
            ),
            "said_blocked": (
                "Said blocked",
                "Subagent runs whose last reply ended [result: blocked]. Counted as didn't finish.",
            ),
        },
        value_labels={"(main session)": "Main session", "(all subagents)": "All subagents"},
        lead_columns=["agent_type", "runs", "replies", "tool_calls", "tool_errors", "interrupts", "corrections"],
    ),
    # -- agents ------------------------------------------------------------
    "topology_spawn_write": TableCopy(
        title="Cache written when each subagent starts",
        help=Help(
            shows="The tokens each agent type writes to the cache on its first reply.",
            read="This is the startup cost you pay once per spawn, before any work.",
            act="If one type is much higher than the rest, see its breakdown in \"What each agent type is given at startup\".",
        ),
        columns={
            "mean_write": ("Average startup write", "Average tokens written to the cache on the first reply."),
            "median_write": ("Typical startup write", "The middle value, less affected by a few very large spawns."),
            "mean_briefing_chars": ("Average task prompt (characters)", "Length of the task the parent wrote when starting the agent."),
        },
    ),
    "topology_session_baseline": TableCopy(
        title="Main session startup write",
        help=Help(
            shows="The tokens your main session writes to the cache on its first reply.",
            read="This is the fixed cost of opening a session: system prompt, tools, CLAUDE.md and your first message.",
            act="",
        ),
        columns={
            "metric": ("", "Which sessions this row covers."),
            "mean_baseline": ("Average startup write", "Average tokens written on the first reply."),
            "median_baseline": ("Typical startup write", "The middle value."),
        },
        value_labels={"all": "All sessions"},
        lead_columns=["median_baseline", "mean_baseline", "sessions"],
    ),
    "topology_upward_tool_result": TableCopy(
        title="Reports returned to the main session",
        help=Help(
            shows="How much text subagents and workflows handed back to the main session.",
            read="Every character returned stays in the main session's context for the rest of the session.",
            act="If reports are long, ask agents for a short summary in their instructions.",
        ),
        columns={
            "tool": ("Returned by", "Subagent (Agent) or workflow runs."),
            "calls": ("Runs", "How many reports came back."),
            "total_chars": ("Total characters", "All report text added to the main session."),
            "mean_chars_per_call": ("Average characters per report", "Average report length."),
        },
        value_labels={"Agent": "Subagents", "Workflow": "Workflows"},
    ),
    "topology_report_proxy": TableCopy(
        title="Size of the report each agent type hands back",
        help=Help(
            shows="The size of the report each agent type hands back, measured where it arrives.",
            read="Higher means longer reports carried in the main session afterwards.",
            act="For a type with long reports, add a length limit to its instructions.",
        ),
        columns={
            "mean_proxy": ("Average report (tokens)", "Average size of the report handed back."),
            "median_proxy": ("Typical report (tokens)", "The middle value."),
        },
    ),
    "topology_skills_rollup": TableCopy(
        title="Cost of each skill, including the subagents it starts",
        help=Help(
            shows="For each skill: its own replies plus every subagent it started, directly or through other agents.",
            read="\"Spawned cost\" is usually most of it for skills that fan out.",
            act="For an expensive skill, check how many agents it starts per use and whether they all need to.",
        ),
        columns={
            "skill": ("", "The skill name."),
            "invocations": ("Uses", "How many times the skill was called."),
            "direct_cost": ("Own cost", "The skill's own replies."),
            "spawned_cost": ("Subagent cost", "Every subagent the skill started, and the ones they started."),
            "total_cost": ("", "Own cost plus subagent cost."),
            "mean_spawns": ("Subagents per use", "Average subagents started per use."),
            "mean_report_proxy": ("Average report (tokens)", "Average size of the reports those subagents handed back."),
        },
    ),
    "topology_spawn_depth": TableCopy(
        title="How deep subagents start other subagents",
        help=Help(
            shows="How many subagents were started by the main session (depth 1) or by another subagent (depth 2 or more).",
            read="Deeper chains multiply startup costs.",
            act="",
        ),
        columns={"depth": ("Depth", "1 means started by the main session."), "count": ("Subagents", "How many subagents at this depth.")},
    ),
    "topology_cost_per_spawn": TableCopy(
        title="Cost per subagent run",
        help=Help(
            shows="What one run of each agent type costs, and how long its tool calls took.",
            read="Compare types doing similar work. The typical run is a fairer guide than the average when a few runs are huge.",
            act="For the most expensive type, check its startup context and whether a cheaper model fits.",
        ),
        columns={
            "mean_cost": ("Average cost per run", "Total cost of a run, averaged."),
            "median_cost": ("Typical cost per run", "The middle value."),
            "mean_tool_wait": ("Average tool wait", "How long each of its tool calls took to answer."),
        },
    ),
    "topology_chains_summary": TableCopy(
        title="Subagent runs stopped early",
        help=Help(
            shows="How many subagent runs there were, and how many you stopped by hand.",
            read="Stopped runs are spend that produced no report.",
            act="",
        ),
        columns={"metric": ("", "What is counted."), "value": ("", "The count.")},
    ),
    "topology_reminder_hook_pressure": TableCopy(
        title="Claude Code notes and hook output per reply",
        help=Help(
            shows="How often Claude Code's own notes and your hooks' output were added, per reply.",
            read="Each addition grows the context and can end cache reuse early.",
            act="If hook output is frequent, check whether your hooks need to print on every call.",
        ),
        columns={
            "mean_reminders_per_turn": ("Notes per reply", "Claude Code's own reminders added per reply."),
            "mean_hook_output_per_turn": ("Hook outputs per reply", "Hook results added per reply."),
        },
        value_labels=_MAIN_OR_SUB,
    ),
    "topology_mcp_cost": TableCopy(
        title="Cost by MCP server",
        help=Help(
            shows="The replies that called each MCP server's tools, and what those replies cost.",
            read="This is the cost of the replies that used the server, not of loading its tool definitions.",
            act="A server that costs a lot for little use is worth disabling where you don't need it.",
        ),
        columns={"mcp_server": ("MCP server", "The server whose tool was called.")},
    ),
    "topology_effort_tokens": TableCopy(
        title="Output and thinking by effort level",
        help=Help(
            shows="For each effort setting: how many replies used it, and how much of their output was thinking.",
            read="Higher effort means more thinking per reply. Thinking is billed as output.",
            act="If high effort is common for routine work, a lower default effort can cut output cost.",
        ),
        columns={
            "effort": ("Effort", "The effort level the reply ran at."),
            "output_tokens": ("Output", "All output tokens, including thinking."),
            "thinking_tokens": ("Thinking", "Output tokens spent thinking."),
            "thinking_share": ("Thinking share", "Thinking as a share of output."),
        },
    ),
    "topology_per_turn_effort_tokens": TableCopy(
        title="Output and thinking by per-reply effort",
        help=Help(
            shows="The same split as by effort level, using the effort recorded on each reply.",
            read="Differs from the table above only when effort changed mid-session.",
            act="",
        ),
        columns={
            "effort": ("Effort", "The effort recorded on the reply."),
            "output_tokens": ("Output", "All output tokens, including thinking."),
            "thinking_tokens": ("Thinking", "Output tokens spent thinking."),
            "thinking_share": ("Thinking share", "Thinking as a share of output."),
        },
    ),
    "topology_effort_by_agent_type": TableCopy(
        title="Output and thinking by agent type",
        help=Help(
            shows="Output and thinking for the main session and each agent type.",
            read="A type with a high thinking share is running at high effort.",
            act="Lower the effort in an agent's file if its work doesn't need deep reasoning.",
        ),
        columns={
            "output_tokens": ("Output", "All output tokens, including thinking."),
            "thinking_tokens": ("Thinking", "Output tokens spent thinking."),
            "thinking_share": ("Thinking share", "Thinking as a share of output."),
        },
        value_labels={"top-level": "Main session"},
    ),
    "topology_context_composition": TableCopy(
        title="What fills the context window: main session vs subagents",
        help=Help(
            shows="For the main session, subagents and workflow agents: the average size of each thing that fills the context, per conversation.",
            read="Startup is the first reply's cache write. Tool results and Claude Code's notes pile up as the conversation runs.",
            act="If tool results dominate, large outputs are being carried turn after turn; see {{page:spend/savings}}.",
        ),
        columns={
            "mean_baseline": ("Startup", "Tokens written to the cache on the first reply."),
            "mean_tool_result_tokens": ("Tool results", "Tool output added over the conversation (estimated)."),
            "mean_output_tokens": ("Claude's replies", "Tokens Claude wrote over the conversation."),
            "mean_attachment_tokens": ("Claude Code's notes", "Reminders, file notes and settings added (estimated)."),
            "mean_compactions": ("Summaries", "Conversation summaries per conversation."),
        },
        value_labels=_MAIN_OR_SUB,
    ),
    "topology_redundant_work": TableCopy(
        title="Repeated work",
        help=Help(
            shows="Commands run again with the same start, and files re-read soon after a conversation summary.",
            read="Repeats after a summary mean the summary dropped something that had to be found again.",
            act="",
        ),
        columns={
            "metric": ("", "What is counted."),
            "mean_per_session": ("Per session", "Average per session."),
            "total": ("Total", "All sessions together."),
        },
    ),
    "topology_redundant_reads": TableCopy(
        title="Files read more than once",
        help=Help(
            shows="Files read more than once in the same session, and how many of those came right after a summary.",
            read="Some re-reads are normal after edits. Many re-reads of the same files add context for no new information.",
            act="",
        ),
        columns={
            "metric": ("", "What is counted."),
            "mean_per_session": ("Per session", "Average per session."),
            "total": ("Total", "All sessions together."),
        },
    ),
    # -- workstyle / workflows ---------------------------------------------
    "workstyle_archetypes": TableCopy(
        title="Your working patterns",
        help=Help(
            shows="Each working pattern, how many sessions match it, and what it means.",
            read="The biggest share is how you usually work.",
            act="",
        ),
        columns={
            "archetype": ("Pattern", "The working pattern."),
            "pct": ("Share", "Share of sessions."),
            "description": ("What it means", "How the pattern is recognised."),
        },
        # One per workstyle._ARCHETYPE_DESCRIPTIONS key (tested).
        value_labels={
            "overseer-fanout": "Lead session hands out work",
            "plan-high-implement-low": "Plan big, build small",
            "workflow-heavy": "Workflow runs",
            "effort-varied": "Mixed effort levels",
            "single-model": "One model, working directly",
            "chat-only": "Conversation only",
            "mixed": "No single pattern",
        },
    ),
    "workflows_summary": TableCopy(
        title="Workflow runs",
        help=Help(shows="Totals for workflow runs in the window.", read="", act=""),
        columns={"metric": ("", "What is counted."), "value": ("", "The total.")},
        value_labels={
            "Total workflow runs": "Workflow runs",
            "Total agents spawned": "Agents started",
            "Total cost (USD)": "Total cost",
            "Mean agents per run": "Agents per run (average)",
            "Mean cost per run (USD)": "Cost per run (average)",
        },
        row_kinds={
            "Total workflow runs": "int",
            "Total agents spawned": "int",
            "Total cost (USD)": "money",
            "Mean agents per run": "float",
            "Mean cost per run (USD)": "money",
        },
    ),
    "workflows_status_mix": TableCopy(
        title="How workflow runs ended",
        help=Help(
            shows="How many runs completed, failed or were cancelled.",
            read="Failed and cancelled runs are spend that produced no result.",
            act="",
        ),
        columns={"status": ("", "How the run ended."), "count": ("Runs", "How many runs."), "pct": ("Share", "Share of runs.")},
    ),
    "workflows_detail": TableCopy(
        title="Most expensive workflow runs",
        help=Help(shows="The 20 workflow runs that cost the most.", read="", act=""),
        columns={
            "run_id": ("", "The run's id."),
            "session_id": ("", "The session it ran in."),
            "status": ("", "How it ended."),
            "agent_count": ("", "Agents it started."),
            "phases": ("", "How many phases it had."),
            "started": ("", "When it started."),
            "finished": ("", "When it finished."),
        },
        lead_columns=["run_id", "cost", "status", "agent_count", "phases", "started", "finished"],
    ),
    # -- overview ---------------------------------------------------------------
    "totals": TableCopy(
        title="Totals for the window",
        help=Help(
            shows="Totals for the window in four groups: activity (counts of sessions, runs and replies), "
            "tokens, cost, and context size. Each row names its unit.",
            read="\"All tokens\" counts cache reads; \"New tokens\" leaves them out and is closer to the work "
            "done. A cache payback above 0 means caching saved more than it cost.",
            act="If many main session replies carry over 200,000 tokens of context, clear or summarise long "
            "sessions sooner.",
        ),
        columns={
            "metric": ("What", "What is counted, with its unit."),
            "value": ("Amount", "The total for the window, in the unit the row names."),
        },
        value_labels={
            "sessions": "Sessions (count)",
            "top_level_transcripts": "Main sessions (count)",
            "subagent_transcripts": "Subagent runs (count)",
            "workflow_runs": "Workflow runs (count)",
            "priced_turns": "Model replies (count)",
            "input_tokens": "Input, not cached (tokens)",
            "cache_creation_tokens": "Written to the cache (tokens)",
            "cache_read_tokens": "Read from the cache (tokens)",
            "output_tokens": "Output, including thinking (tokens)",
            "usage_tokens": "All tokens, including cache reads (tokens)",
            "new_tokens": "New tokens: everything except cache reads (tokens)",
            "total_cost_usd": "Total cost at list price",
            "cache_read_cost_share_pct": "Share of cost from cache reads (%)",
            "cache_roi": "Cache payback: net saving divided by the cost of cache writes",
            "top_level_median_ctx": "Context of a typical main session reply (tokens)",
            "top_level_turns_ctx_ge_200k_pct": "Main session replies with over 200,000 tokens of context (%)",
        },
        row_kinds={
            **{key: "int" for key in ("sessions", "top_level_transcripts", "subagent_transcripts", "workflow_runs", "priced_turns")},
            **{
                key: "tokens"
                for key in (
                    "input_tokens", "cache_creation_tokens", "cache_read_tokens", "output_tokens",
                    "usage_tokens", "new_tokens", "top_level_median_ctx",
                )
            },
            "total_cost_usd": "money",
            "cache_read_cost_share_pct": "pct",
            "cache_roi": "float",
            "top_level_turns_ctx_ge_200k_pct": "pct",
        },
        row_groups={
            "sessions": "Activity",
            "input_tokens": "Tokens",
            "total_cost_usd": "Cost",
            "top_level_median_ctx": "Context size",
        },
    ),
    "by_model": TableCopy(
        title="Usage and cost by model",
        help=Help(
            shows="One row per model: replies, tokens by type, and cost.",
            read="Price per token differs a lot between models. A model with few replies can still cost the most.",
            act="If an expensive model does routine work such as searching, consider a cheaper model for that "
            "agent type. {{page:spend/savings}} estimates the effect.",
        ),
        columns={
            "model": ("", "The model that wrote the replies."),
            "turns": ("Replies", "Model replies from this model, main session and subagents together."),
            "input_tokens": ("Input tokens", "Input tokens not read from or written to the cache."),
            "cache_creation_tokens": ("Cache writes", "Tokens written into the cache."),
            "cache_read_tokens": ("Cache reads", "Tokens read back from the cache. Far cheaper than a cache write."),
            "output_tokens": ("Output tokens", "Tokens Claude wrote, including thinking."),
            "cost": ("", "Cost at list prices for the window."),
        },
        value_labels={"<unknown>": "Unknown model"},
    ),
    # -- usage ------------------------------------------------------------------
    "by_day": TableCopy(
        title="Usage by day",
        help=Help(
            shows="One row per day and model: replies, tokens and cost. Days are in your local time.",
            read="Tokens include cache reads, so they run far above what cost suggests. Compare cost from day "
            "to day. A spike is usually one long session or a big fan-out of subagents.",
            act="",
        ),
        columns={
            "period": ("Day", "The local calendar day."),
            "model": ("", "The model that wrote the replies."),
            "turns": ("Replies", "Model replies that day, main session and subagents together."),
            "tokens": ("", "All tokens for these replies, including cache reads."),
            "cost": ("", "Cost at list prices for these replies."),
        },
        value_labels={"<unknown>": "Unknown model"},
    ),
    "by_week": TableCopy(
        title="Usage by week",
        help=Help(
            shows="One row per week and model: replies, tokens and cost. Weeks run Monday to Sunday.",
            read="Compare cost from week to week. Tokens include cache reads, so they run far above cost.",
            act="",
        ),
        columns={
            "period": ("Week", "The week, as year and week number, such as 2026-W30."),
            "model": ("", "The model that wrote the replies."),
            "turns": ("Replies", "Model replies that week, main session and subagents together."),
            "tokens": ("", "All tokens for these replies, including cache reads."),
            "cost": ("", "Cost at list prices for these replies."),
        },
        value_labels={"<unknown>": "Unknown model"},
    ),
    "by_month": TableCopy(
        title="Usage by month",
        help=Help(
            shows="One row per month and model: replies, tokens and cost.",
            read="Compare cost from month to month. A month still in progress is only partly counted.",
            act="",
        ),
        columns={
            "period": ("Month", "The local calendar month."),
            "model": ("", "The model that wrote the replies."),
            "turns": ("Replies", "Model replies that month, main session and subagents together."),
            "tokens": ("", "All tokens for these replies, including cache reads."),
            "cost": ("", "Cost at list prices for these replies."),
        },
        value_labels={"<unknown>": "Unknown model"},
    ),
    "by_project": TableCopy(
        title="Cost by project",
        help=Help(
            shows="One row per project folder: how many sessions ran there and what they cost.",
            read="Names are the folder names Claude Code uses, with slashes turned into dashes. Each worktree "
            "shows up as its own project.",
            act="",
        ),
        columns={
            "slug": ("", "The project folder, as Claude Code names it."),
            "sessions": ("", "Sessions started in this project."),
            "cost": ("", "Cost at list prices of those sessions, including their subagents."),
        },
    ),
    "by_entrypoint": TableCopy(
        title="Usage by app",
        help=Help(
            shows="One row per app you ran Claude Code from, such as the terminal, the desktop app or the SDK.",
            read="Each main session and each subagent run counts once, under the app that started it.",
            act="",
        ),
        columns={
            "entrypoint": ("App", "Where Claude Code was started from, as it records it."),
            "transcripts": (
                "Sessions and subagent runs",
                "How many conversation logs this covers: one per main session and one per subagent run.",
            ),
            "turns": ("Replies", "Model replies from these sessions and runs."),
            "tokens": ("", "All tokens for these replies, including cache reads."),
            "cost": ("", "Cost at list prices for these replies."),
        },
        value_labels={
            "cli": "Terminal",
            "claude-desktop": "Claude desktop app",
            "claude-vscode": "VS Code extension",
            "sdk": "Agent SDK",
            "sdk-cli": "Agent SDK (command line)",
            "sdk-ts": "Agent SDK (TypeScript)",
            "sdk-py": "Agent SDK (Python)",
            "unknown": "Not recorded",
        },
    ),
    "five_hour_blocks": TableCopy(
        title="Five-hour blocks",
        help=Help(
            shows="Your usage cut into fixed five-hour blocks of local time, starting at 00:00, 05:00, 10:00, "
            "15:00 and 20:00. Shown only for subscription billing.",
            read="Your real usage limit window starts with your first message, not on this grid. Treat each "
            "block as an approximation. Each reply counts in the block its own time falls in.",
            act="Blocks with much higher cost than usual are the ones most likely to hit a usage limit. "
            "{{page:spend/usage}} shows the stops that actually happened.",
        ),
        columns={
            "block_start": ("Block start", "When the block starts, in your local time."),
            "sessions": ("", "Sessions with at least one reply in this block."),
            "turns": ("Replies", "Model replies in this block, main session and subagents together."),
            "tokens": ("", "All tokens in this block, including cache reads."),
            "cost": ("", "Cost at list prices for this block."),
        },
    ),
    "pricing_unknown_models": TableCopy(
        title="Models with no price",
        help=Help(
            shows="One row per model your replies came from that the price list does not include. Shown only "
            "when there is at least one.",
            read="These replies count as costing nothing, so every cost in the report is too low by their share. "
            "Compare the tokens here with your total tokens to see how much is missing.",
            act="Add each model to pricing.toml, with its prices per million tokens, then run the report again.",
        ),
        columns={
            "model_id": ("Model", "The model name exactly as Claude Code recorded it."),
            "turns": ("Replies", "Model replies from this model."),
            "tokens": ("", "All tokens in those replies, including cache reads."),
        },
    ),
    "pricing_closest_match": TableCopy(
        title="Priced by closest match",
        help=Help(
            shows="One row per model with no price list entry of its own, priced instead at the nearest "
            "registered model's rate. Shown only when there is at least one.",
            read="These replies count toward the report's 100% priced score, but the price used is a guess. It is the "
            "closest registered model's rate, not this one's own, so the cost may be off.",
            act="Add this model to pricing.toml, with its own prices per million tokens, for an exact cost.",
        ),
        columns={
            "model_id": ("Model", "The model name exactly as Claude Code recorded it."),
            "priced_as": ("Priced as", "The registered model whose rate was used instead."),
            "turns": ("Replies", "Model replies from this model."),
            "tokens": ("", "All tokens in those replies, including cache reads."),
        },
    ),
    "pricing_fast_priced_as_standard": TableCopy(
        title="Fast turns priced at standard rate",
        help=Help(
            shows="One row per model with at least one fast-mode reply that has no fast-mode price on file. "
            "Shown only when there is at least one.",
            read="Claude Code flagged these replies as fast mode, but this model's price list entry has no fast rate. "
            "So they were costed at the model's standard rate, which is likely too low.",
            act="Add a fast rate for this model in pricing.toml for an exact cost.",
        ),
        columns={
            "model_id": ("Model", "The model name exactly as Claude Code recorded it."),
            "turns": ("Replies", "Fast-mode replies from this model."),
            "tokens": ("", "All tokens in those replies, including cache reads."),
        },
    ),
    "pricing_fast_applied": TableCopy(
        title="Fast-priced replies",
        help=Help(
            shows="One row per model with at least one reply billed at its fast-mode rate. Shown only when there is "
            "at least one. {{page:setup/profiles}} uses it to estimate what turning fast mode off would save.",
            read="\"Cost at fast rate\" is what these replies were billed. \"Cost at standard rate\" is what they "
            "would have cost with fast mode off. The gap is the premium fast mode charges for a faster reply.",
            act="If the extra speed isn't worth its price, draft a profile that turns fast mode off to see the "
            "saving.",
        ),
        columns={
            "model_id": ("Model", "The model name exactly as Claude Code recorded it."),
            "turns": ("Replies", "Fast-mode replies from this model."),
            "tokens": ("", "All tokens in those replies, including cache reads."),
            "cost": ("Cost at fast rate", "What these replies were actually billed, at the fast-mode rate."),
            "standard_cost": ("Cost at standard rate", "What the same replies would have cost at the model's "
                               "standard rate, with fast mode off."),
        },
    ),
    # -- usage limits -------------------------------------------------------------
    "elasticity_budget": TableCopy(
        title="Tokens one full usage limit holds",
        help=Help(
            shows="One row per usage limit: how many million new tokens use up all of it, going by your own "
            "readings.",
            read="New tokens are input, cache writes and output. Cache reads are left out. A blank figure means "
            "there are not yet enough readings that agree; the note says why.",
            act="",
        ),
        columns={
            "window": ("Usage limit", "Which usage limit this row is about."),
            "million_new_tokens_per_window": (
                "Million new tokens per full limit",
                "How many million new tokens take this limit from empty to full.",
            ),
            "note": ("Note", "How many readings the figure rests on and how well they agree, or why it is blank."),
        },
        value_labels=_WINDOW_LABELS,
    ),
    "elasticity_recent_burn": TableCopy(
        title="How much of your weekly limit you used recently",
        help=Help(
            shows="The new tokens your sessions used in the last day, as a share of one full weekly limit.",
            read="Over 14% in a day means you would run out before the week resets if every day were like it.",
            act="If it is high, start with the recommendations that save the most.",
        ),
        columns={
            "window": ("Usage limit", "Which usage limit the share is of."),
            "hours": ("Hours", "How far back this looks, in hours."),
            "new_tokens": ("New tokens", "Input, cache writes and output in that time. Cache reads are left out."),
            "pct_of_window": ("Share of the limit", "Those tokens as a percentage of one full limit."),
            "note": ("Note", "Why the share is blank, when it is."),
        },
        value_labels=_WINDOW_LABELS,
        lead_columns=["pct_of_window", "new_tokens", "hours"],
    ),
    "elasticity_fit": TableCopy(
        title="How well your readings line up",
        help=Help(
            shows="One row per usage limit and measure: how much of the limit, in percent, each unit used, and "
            "how well the readings agree.",
            read="A figure is shown only with enough readings that agree closely. The fit score runs from 0 "
            "to 1; higher means the readings agree more.",
            act="",
        ),
        columns={
            "window": ("Usage limit", "Which usage limit this row is about."),
            "metric": ("Measured by", "What the limit use is compared with."),
            "unit": ("Per", "The unit the percentage is per."),
            "slope": ("Percent of the limit per unit", "How much of the limit one unit used, in percent."),
            "r2": ("Fit score", "How well the readings agree, from 0 to 1."),
            "n_pairs": ("Readings used", "Pairs of back-to-back readings the figure rests on."),
            "residual_std": ("Typical miss", "How far a typical reading sits from the figure, in percentage points."),
            "accepted": ("Shown", "Whether the figure passed the checks and is used."),
            "reason": ("Why not", "Why the figure was held back, when it was."),
        },
        value_labels={
            **_WINDOW_LABELS,
            "new_tokens": "New tokens",
            "cache_read": "Cache reads",
            "usd": "List-price dollars",
            "million new tokens": "Million new tokens",
            "million cache-read tokens": "Million cache-read tokens",
            "USD (list price)": "Dollar at list price",
        },
        lead_columns=["window", "metric", "slope", "unit", "r2", "n_pairs", "accepted"],
    ),
    # -- sessions ---------------------------------------------------------------
    "sessions_by_mode": TableCopy(
        title="Sessions by how you worked",
        help=Help(
            shows="Your sessions grouped by working mode. Interactive: you replied within a few minutes. "
            "Long autonomous run: Claude worked through many steps or subagents with few prompts from you. "
            "Overnight: the session ran into the night with a long gap. Mixed: none of these.",
            read="Replies and subagents are totals for the group. Typical length and typical prompts come from "
            "the middle session in the group.",
            act="",
        ),
        columns={
            "value": ("Mode", "How you worked in the session."),
            "sessions": ("", "Sessions in this group."),
            "turns": ("Replies", "Every reply logged in these sessions, main session and subagents together."),
            "subagents": ("", "Subagent runs started in these sessions, in total."),
            "median_span_s": (
                "Typical length",
                "Time from first to last message in the middle session, including idle time.",
            ),
            "human_prompts_median": ("Typical prompts from you", "Messages you typed in the middle session."),
        },
        value_labels={
            "interactive": "Interactive",
            "long-agentic": "Long autonomous run",
            "overnight": "Overnight",
            "mixed": "Mixed",
            "unknown": "Not classified",
        },
    ),
    "sessions_by_purpose": TableCopy(
        title="Sessions by what they were for",
        help=Help(
            shows="Your sessions grouped by the kind of work, judged from the tools used: tests run, files "
            "edited, reviews, plan mode, subagents started, and so on.",
            read="Each session gets the first purpose whose signs it shows. A review that also started subagents "
            "counts as a review; subagent fan-out only catches sessions with no clearer purpose. While metrics "
            "capture is on, the kind of task Claude reported for most of a session's messages decides instead. That "
            "applies when it matches one of these: review, tests, planning, docs or refactoring.",
            act="Recommendations are tuned to your largest groups. A large subagent fan-out group is worth a "
            "look in {{page:agents/subagents}}.",
        ),
        columns={
            "value": ("Purpose", "What the session was for."),
            "sessions": ("", "Sessions in this group."),
            "turns": ("Replies", "Every reply logged in these sessions, main session and subagents together."),
            "subagents": ("", "Subagent runs started in these sessions, in total."),
            "median_span_s": (
                "Typical length",
                "Time from first to last message in the middle session, including idle time.",
            ),
            "human_prompts_median": ("Typical prompts from you", "Messages you typed in the middle session."),
        },
        value_labels={
            "general-dev": "General development",
            "agent-fanout": "Subagent fan-out",
            "docs-or-light-edit": "Docs or light edits",
            "test-triage": "Running and fixing tests",
            "workflow-run": "Workflow runs",
            "refactor": "Refactoring",
            "planning": "Planning",
            "review": "Code review",
            "local-llm-pipeline": "Calling a local model",
            "unknown": "Not classified",
        },
    ),
    # -- scorecard --------------------------------------------------------------
    "dimensions": TableCopy(
        title="Scorecard ratings",
        help=Help(
            shows="One row per area: its rating, the one number it is based on, and the limit for that rating.",
            read="Ratings run from 1 (very poor) to 5 (excellent). The limit is the bound the number had to stay "
            "within to earn its rating. Cache rebuilds forced by usage-limit pauses are left out of cache "
            "efficiency.",
            act="Work on the lowest rating first. For cache efficiency, see {{page:cache/rebuilds}}. For context "
            "size, clear or summarise long sessions sooner. For subagent cost balance, check the costliest "
            "agent type in {{page:agents/subagents}}.",
        ),
        columns={
            "dimension": ("Area", "What is rated."),
            "level": ("Level", "The rating as a number from 1 to 5. Higher is better."),
            "label": ("Rating", "The rating in words."),
            "metric": ("Based on", "The one number the rating is based on."),
            "value": ("Value", "That number. Its unit is in the name: %, tokens, times, or a count of settings."),
            "threshold": (
                "Limit for this rating",
                "The bound the value stayed within to earn this rating. For a rating of 1, the bound it went past.",
            ),
        },
        value_labels={
            "cache_efficiency": "Cache efficiency",
            "context_hygiene": "Context size",
            "agent_efficiency": "Subagent cost balance",
            "config_fit": "Config stability",
            "data_quality": "Data quality",
            "recache_share_pct": "Cache writes that were rebuilds, excluding usage-limit pauses (%)",
            "p90_top_level_ctx": "Context size that 9 in 10 main session replies stay under (tokens)",
            "agent_cost_variance_ratio": "Cost per run of the costliest agent type, versus the typical type (times)",
            "changed_config_keys": "Settings changed during the window (count)",
            "pricing_coverage_pct": "Tokens with a known price (%)",
            "no config snapshot available": "No config snapshot, so counted as stable",
            "excellent": "Excellent",
            "good": "Good",
            "fair": "Fair",
            "poor": "Poor",
            "very poor": "Very poor",
        },
    ),
    "overall": TableCopy(
        title="Overall rating",
        help=Help(
            shows="Your overall rating: the lowest of cache efficiency, context size, subagent cost balance and "
            "config stability.",
            read="It is the lowest rating, not an average, so one weak area pulls it down. Data quality is left out.",
            act="To raise it, fix the lowest-rated area in the ratings table.",
        ),
        columns={
            "metric": ("", "What is rated."),
            "level": ("Level", "From 1 to 5. Higher is better. 0 means nothing could be measured."),
            "label": ("Rating", "The rating in words."),
        },
        value_labels={
            "overall": "Overall",
            "excellent": "Excellent",
            "good": "Good",
            "fair": "Fair",
            "poor": "Poor",
            "very poor": "Very poor",
            "unmeasured": "Not measured",
        },
        lead_columns=["label"],
    ),
    # -- before and after -------------------------------------------------------
    "baseline_comparison_overview": TableCopy(
        title="",  # the builder's title names the baseline
        help=Help(
            shows="Key measures for the baseline you saved and for this window, with the change between them.",
            read="\"Change\" is this window minus the baseline. \"Change (%)\" is that change as a share of the "
            "baseline. For measures that are already percentages it is a relative change, not points. "
            "Cache lifetime (TTL) is how long a cache write stays readable: 5 minutes or 1 hour.",
            act="A clear drop in cost per session or in the rebuild share after a setting change suggests it "
            "helped. Check the by-mode table before you credit the setting.",
        ),
        columns={
            "metric": ("Measure", "What is compared."),
            "baseline": ("Baseline", "The value when you saved the baseline."),
            "current": ("This window", "The value for this report's window."),
            "delta": ("Change", "This window minus the baseline, in the measure's own unit."),
            "delta_pct": ("Change (%)", "The change as a percentage of the baseline value."),
        },
        value_labels={
            "Cost per session": "Cost per session (list price)",
            "Re-cache share of cache-creation": "Share of cache writes that were rebuilds",
            "Compactions per session": "Conversation summaries per session",
            "Session baseline size (mean top-level first-turn cache-creation)": (
                "Main session startup write (average tokens)"
            ),
            "TTL mix - top-level (5m share)": "Main session cache writes with a 5-minute lifetime",
            "TTL mix - top-level (1h share)": "Main session cache writes with a 1-hour lifetime",
            "Scorecard level - cache_efficiency": "Scorecard: cache efficiency (1 to 5)",
            "Scorecard level - context_hygiene": "Scorecard: context size (1 to 5)",
            "Scorecard level - agent_efficiency": "Scorecard: subagent cost balance (1 to 5)",
            "Scorecard level - config_fit": "Scorecard: config stability (1 to 5)",
            "Scorecard level - data_quality": "Scorecard: data quality (1 to 5)",
        },
    ),
    "baseline_comparison_by_mode": TableCopy(
        title="Before and after, by working mode",
        help=Help(
            shows="Cost per session, rebuild share and conversation summaries, split by working mode so you "
            "compare like with like.",
            read="A mode shows numbers only when it has at least 5 sessions in both windows. Each change is "
            "relative to the baseline, as a % of it, not percentage points.",
            act="If a measure moved in every mode, a setting change is the likelier cause. If it moved in one "
            "mode only, the work itself probably changed.",
        ),
        columns={
            "mode": ("Mode", "How you worked in the session. See \"Sessions by how you worked\"."),
            "sessions_baseline": ("Sessions before", "Sessions of this mode in the baseline."),
            "sessions_current": ("Sessions now", "Sessions of this mode in this window."),
            "sample_ok": ("Enough sessions", "Yes when both windows have at least 5 sessions of this mode."),
            "cost_per_session_baseline": (
                "Cost per session before",
                "Average cost per session in the baseline, at list prices.",
            ),
            "cost_per_session_current": (
                "Cost per session now",
                "Average cost per session in this window, at list prices.",
            ),
            "cost_per_session_delta_pct": ("Cost per session change", "The change as a % of the baseline value."),
            "recache_share_baseline": (
                "Rebuild share before",
                "Share of cache writes that were cache rebuilds in the baseline, in %.",
            ),
            "recache_share_current": (
                "Rebuild share now",
                "Share of cache writes that were cache rebuilds in this window, in %.",
            ),
            "recache_share_delta_pct": (
                "Rebuild share change",
                "The change as a % of the baseline value. Not percentage points.",
            ),
            "compactions_per_session_baseline": (
                "Summaries per session before",
                "Average conversation summaries per session in the baseline.",
            ),
            "compactions_per_session_current": (
                "Summaries per session now",
                "Average conversation summaries per session in this window.",
            ),
            "compactions_per_session_delta_pct": (
                "Summaries per session change",
                "The change as a % of the baseline value.",
            ),
            "note": ("Note", "Why a row shows no numbers: too few sessions in one of the windows."),
        },
        value_labels={
            "interactive": "Interactive",
            "long-agentic": "Long autonomous run",
            "overnight": "Overnight",
            "mixed": "Mixed",
            "unknown": "Not classified",
            "yes": "Yes",
            "no": "No",
        },
        lead_columns=[
            "mode", "sessions_baseline", "sessions_current", "cost_per_session_baseline", "cost_per_session_current",
            "cost_per_session_delta_pct", "recache_share_delta_pct",
        ],
    ),
    # -- phases -----------------------------------------------------------------
    "phases_summary": TableCopy(
        title="Cost by phase",
        help=Help(
            shows="One row per phase: replies, tokens and cost, with each phase's share of total cost.",
            read="Exploring: the reply only read or searched. Building: it edited files or ran a shell command. "
            "Checking: it ran tests or a build, or edited a scratch file. Other: no tools, or other tools "
            "such as starting a subagent.",
            act="If exploring is more than 35% of cost, a recommendation suggests ways to point Claude at the "
            "right files sooner.",
        ),
        columns={
            "phase": ("", "The phase, judged from the tools the reply called."),
            "turns": ("Replies", "Model replies in this phase, main session and subagents together."),
            "new_tokens": ("New tokens", "Input plus cache writes, in tokens. Cache reads are left out."),
            "cache_read_tokens": ("Cache reads", "Tokens read back from the cache."),
            "output_tokens": ("Output tokens", "Tokens Claude wrote, including thinking."),
            "cost": ("", "Cost at list prices for this phase."),
            "cost_share_pct": ("Share of cost", "This phase's cost as a % of all phases."),
        },
        value_labels={
            "discovery": "Exploring",
            "implementation": "Building",
            "verification": "Checking",
            "other": "Other",
        },
    ),
    "phases_by_transcript_kind": TableCopy(
        title="Cost by phase: main session vs subagents",
        help=Help(
            shows="The same split by phase, for the main session, subagents and workflow agents separately.",
            read="Subagents often do most of the exploring. Compare how each group's cost spreads across phases.",
            act="",
        ),
        columns={
            "dimension": ("Where", "Main session, subagents or workflow agents."),
            "phase": ("", "The phase, judged from the tools the reply called."),
            "turns": ("Replies", "Model replies in this phase."),
            "new_tokens": ("New tokens", "Input plus cache writes, in tokens. Cache reads are left out."),
            "cache_read_tokens": ("Cache reads", "Tokens read back from the cache."),
            "output_tokens": ("Output tokens", "Tokens Claude wrote, including thinking."),
            "cost": ("", "Cost at list prices for this phase."),
        },
        value_labels=_MAIN_OR_SUB
        | {"discovery": "Exploring", "implementation": "Building", "verification": "Checking", "other": "Other"},
    ),
    "phases_by_agent_type": TableCopy(
        title="Cost by phase and agent type",
        help=Help(
            shows="The same split by phase, for each agent type.",
            read="An agent type meant to search should be mostly exploring. One that is mostly building or "
            "checking is doing more than its name suggests.",
            act="",
        ),
        columns={
            "dimension": (
                "Agent type",
                "The subagent type, or the main session.",
            ),
            "phase": ("", "The phase, judged from the tools the reply called."),
            "turns": ("Replies", "Model replies in this phase."),
            "new_tokens": ("New tokens", "Input plus cache writes, in tokens. Cache reads are left out."),
            "cache_read_tokens": ("Cache reads", "Tokens read back from the cache."),
            "output_tokens": ("Output tokens", "Tokens Claude wrote, including thinking."),
            "cost": ("", "Cost at list prices for this phase."),
        },
        value_labels={
            "top-level": "Main session",
            "unknown": "Subagent (type not recorded)",
            "discovery": "Exploring",
            "implementation": "Building",
            "verification": "Checking",
            "other": "Other",
        },
    ),
    # -- cache rebuilds -----------------------------------------------------
    "recache_summary": TableCopy(
        title="Cache rebuilds at a glance",
        help=Help(
            shows="Totals for the window: how many replies rebuilt the cache, and what that cost.",
            read="Avoidable cost is what the rebuilds cost above reading the same tokens from the cache. "
            "Rebuilds after a usage-limit pause are counted, but their cost is shown on its own, because "
            "you can't avoid them.",
            act="If avoidable cost is a large part of your spend, use the tables below to find the cause.",
        ),
        columns={
            "metric": ("", "This row covers every reply in the window."),
            "transcripts": ("Conversation logs", "One per main session and one per subagent run."),
            "priced_turns": ("Replies", "Model replies with token counts."),
            "recache_turns": (
                "Cache rebuilds",
                "Replies that rebuilt the cache, including those after a usage-limit pause.",
            ),
            "recache_turn_share_pct": ("Rebuild share", "Cache rebuilds as a share of all replies."),
            "recache_cc_tokens": ("Tokens rebuilt", "Tokens those rebuilds wrote to the cache."),
            "total_cc_tokens": ("All cache writes", "Tokens every reply wrote to the cache."),
            "recache_cc_share_pct": ("Rebuild share of cache writes", "Tokens rebuilt as a share of all cache writes."),
            "avoidable_cost_usd": (
                "Avoidable cost",
                "What rebuilds cost above reading the same tokens from the cache. Leaves out rebuilds after a "
                "usage-limit pause.",
            ),
            "unavoidable_limit_expiry_cost_usd": (
                "Cost after usage-limit pauses",
                "The same extra cost for rebuilds after a usage-limit pause. You can't avoid these.",
            ),
        },
        value_labels={"all": "All replies"},
        lead_columns=["avoidable_cost_usd", "recache_turns", "recache_turn_share_pct", "recache_cc_tokens"],
    ),
    "recache_signature_split": TableCopy(
        title="Why the cache was rebuilt",
        help=Help(
            shows="Cache rebuilds split by what had happened to the cache.",
            read="\"Cache expired\" means almost nothing was left to read: the cache lifetime ran out. \"Cache "
            "broken by a change\" means part was read, but something early in the context changed. \"Expired "
            "during a usage-limit pause\" means you were waiting for a limit to reset.",
            act="Expired caches respond to a longer cache lifetime. Broken ones don't: check what came right before "
            "them in the causes table.",
        ),
        columns={
            "signature": ("Reason", "What had happened to the cache."),
            "turns": ("Rebuilds", "Replies that rebuilt the cache for this reason."),
            "cc_tokens": ("Tokens rebuilt", "Tokens those rebuilds wrote to the cache."),
            "cost_delta_usd": (
                "Cost above a cache read",
                "What these rebuilds cost above reading the same tokens from the cache. "
                "Unavoidable for the usage-limit row.",
            ),
            "median_ctx": ("Typical context size", "The middle context size of these replies, in tokens."),
            "median_gap_s": ("Typical wait before", "The middle wait since the previous reply."),
        },
        value_labels={
            "full-expiry": "Cache expired",
            "prefix-invalidated": "Cache broken by a change",
            "limit-expiry": "Expired during a usage-limit pause",
        },
    ),
    "recache_gap_buckets": TableCopy(
        title="Cache rebuilds by wait since the previous reply",
        help=Help(
            shows="Cache rebuilds grouped by how long it had been since the previous reply, next to the same "
            "split for all replies. Usage-limit pauses are left out.",
            read="A 5-minute cache expires after 5 minutes with no reply. Compare the share of rebuilds with the "
            "share of all replies in each row: a row with far more rebuilds than replies points at expiry.",
            act="If most rebuilds follow waits of 5 to 60 minutes, a 1-hour cache lifetime may pay for itself. "
            "Check {{page:cache/lifetime}}.",
        ),
        columns={
            "bucket": ("Wait since previous reply", "How long it had been since the previous reply."),
            "turns": ("Rebuilds", "Cache rebuilds after a wait in this range."),
            "share_pct_turns": ("Share of rebuilds", "This row's rebuilds as a share of all rebuilds."),
            "control_turns": ("All replies", "Every reply after a wait in this range."),
            "control_share_pct_turns": ("Share of all replies", "This row's replies as a share of all replies."),
            "cc_tokens": ("Tokens rebuilt", "Tokens those rebuilds wrote to the cache."),
            "cc_share_pct": ("Share of tokens rebuilt", "This row's rebuilt tokens as a share of all rebuilt tokens."),
            "control_cc_tokens": ("All cache writes", "Tokens every reply in this range wrote to the cache."),
            "control_cc_share_pct": (
                "Share of all cache writes",
                "This row's cache writes as a share of all cache writes.",
            ),
        },
        value_labels={
            "<1m": "Under 1 minute",
            "1-5m": "1 to 5 minutes",
            "5-15m": "5 to 15 minutes",
            "15-60m": "15 to 60 minutes",
            ">60m": "Over 60 minutes",
            "unknown": "Not known (first reply, or time unreadable)",
        },
        lead_columns=[
            "bucket", "turns", "share_pct_turns", "control_share_pct_turns", "cc_tokens", "cc_share_pct",
            "control_cc_share_pct",
        ],
    ),
    "recache_preceding_tool": TableCopy(
        title="Cache rebuilds by the tool used right before",
        help=Help(
            shows="Cache rebuilds grouped by the tool the previous reply called, next to the same split for all "
            "replies. Usage-limit pauses are left out.",
            read="Bash or PowerShell is shown when the previous reply used it, even if it also called other tools. "
            "Compare the share of rebuilds with the share of all replies.",
            act="A tool far more common before rebuilds than before other replies is worth a look. It is often a "
            "long-running command that outlasts the cache.",
        ),
        columns={
            "preceding_tool": ("Tool used right before", "The tool the previous reply called."),
            "turns": ("Rebuilds", "Cache rebuilds after this tool."),
            "share_pct_turns": ("Share of rebuilds", "This row's rebuilds as a share of all rebuilds."),
            "control_turns": ("All replies", "Every reply after this tool."),
            "control_share_pct_turns": ("Share of all replies", "This row's replies as a share of all replies."),
            "cc_tokens": ("Tokens rebuilt", "Tokens those rebuilds wrote to the cache."),
            "cc_share_pct": ("Share of tokens rebuilt", "This row's rebuilt tokens as a share of all rebuilt tokens."),
            "control_cc_tokens": ("All cache writes", "Tokens every reply after this tool wrote to the cache."),
            "control_cc_share_pct": (
                "Share of all cache writes",
                "This row's cache writes as a share of all cache writes.",
            ),
        },
        value_labels={
            "none": "No tool (a text reply)",
            "n/a": "No previous reply",
        },
        lead_columns=[
            "preceding_tool", "turns", "share_pct_turns", "control_share_pct_turns", "cc_tokens", "cc_share_pct",
            "control_cc_share_pct",
        ],
    ),
    "recache_top_command_prefixes": TableCopy(
        title="Commands run right before a cache rebuild",
        help=Help(
            shows="The 12 shell commands followed by the most rebuilt tokens. Usage-limit pauses are left out.",
            read="Only the first 40 characters are kept, with paths hidden. Waiting loops and long builds often "
            "outlast a 5-minute cache.",
            act="For a command that runs for minutes, run it in the background, or use a 1-hour cache lifetime "
            "for the agent that runs it.",
        ),
        columns={
            "preceding_cmd_prefix": ("Command (start)", "The start of the shell command the previous reply ran."),
            "turns": ("Rebuilds", "Cache rebuilds right after this command."),
            "cc_tokens": ("Tokens rebuilt", "Tokens those rebuilds wrote to the cache."),
        },
    ),
    "recache_primary_cause": TableCopy(
        title="What happened right before each cache rebuild",
        help=Help(
            shows="Cache rebuilds grouped by the most important thing that happened since the previous reply, next "
            "to the same split for all replies. Usage-limit pauses are left out.",
            read="\"Over-represented by\" is the share of rebuilds minus the share of all replies, in percentage "
            "points. A large positive number means that event comes before rebuilds far more often than usual.",
            act="For a cause that is strongly over-represented, change it less often mid-session. For example, switch "
            "model, mode or MCP servers at the start of a session, not in the middle.",
        ),
        columns={
            "preceding_primary": ("What came right before", "The most important event since the previous reply."),
            "turns": ("Rebuilds", "Cache rebuilds after this event."),
            "share_pct_turns": ("Share of rebuilds", "This row's rebuilds as a share of all rebuilds."),
            "control_share_pct_turns": ("Share of all replies", "Replies after this event as a share of all replies."),
            "over_representation_points_turns": (
                "Over-represented by (replies)",
                "Share of rebuilds minus share of all replies, in percentage points.",
            ),
            "cc_tokens": ("Tokens rebuilt", "Tokens those rebuilds wrote to the cache."),
            "cc_share_pct": ("Share of tokens rebuilt", "This row's rebuilt tokens as a share of all rebuilt tokens."),
            "control_cc_tokens": ("All cache writes", "Tokens every reply after this event wrote to the cache."),
            "control_cc_share_pct": (
                "Share of all cache writes",
                "This row's cache writes as a share of all cache writes.",
            ),
            "over_representation_points_tokens": (
                "Over-represented by (tokens)",
                "Share of rebuilt tokens minus share of all cache writes, in percentage points.",
            ),
            "avoidable_cost_usd": (
                "Avoidable cost",
                "What these rebuilds cost above reading the same tokens from the cache.",
            ),
        },
        value_labels={
            "compact_boundary": "Conversation summary (compaction)",
            "compact_summary": "Conversation summary text",
            "api_error": "API error and retry",
            "model_fallback": "Switched to a fallback model",
            "local_command": "Local command output",
            "hook_output": "Hook output",
            "cache_signal": "Model, mode or tool list changed",
            "reminder": "Claude Code reminder",
            "context_inject": "Files, memory or skills added by Claude Code",
            "queue_operation": "Message queued while Claude worked",
            "attachment": "Other Claude Code note",
            "meta": "Hidden message from Claude Code",
            "tool_denial": "Tool call denied",
            "tool_result": "Tool result",
            "task_notification": "Subagent or background task finished",
            "peer_message": "Message from another agent",
            "slash_command": "Slash command",
            "scheduled_task": "Scheduled or looped task",
            "interrupt": "You interrupted Claude",
            "human_text": "Your message",
            "unknown": "Nothing recorded",
            "limit_hit": "Usage limit reached",
            "limit_resume": "Resumed after a usage limit",
            "agent_terminated": "Subagent stopped early",
        },
        lead_columns=[
            "preceding_primary", "avoidable_cost_usd", "turns", "share_pct_turns", "control_share_pct_turns",
            "over_representation_points_turns", "cc_tokens",
        ],
    ),
    "recache_primary_cause_prefix_invalidated": TableCopy(
        title="What came right before a broken cache",
        help=Help(
            shows="The same causes as above, for caches broken by a change only. Expired caches are left out, "
            "because they had run out whatever came before.",
            read="Shares are out of broken-cache rebuilds only, compared with all replies. This is the clearest "
            "view of what breaks a warm cache.",
            act="The top over-represented cause here is the one to change first.",
        ),
        columns={
            "preceding_primary": ("What came right before", "The most important event since the previous reply."),
            "turns": ("Rebuilds", "Broken-cache rebuilds after this event."),
            "share_pct_turns": (
                "Share of broken-cache rebuilds",
                "This row's rebuilds as a share of broken-cache rebuilds.",
            ),
            "control_share_pct_turns": ("Share of all replies", "Replies after this event as a share of all replies."),
            "cc_tokens": ("Tokens rebuilt", "Tokens those rebuilds wrote to the cache."),
            "cc_share_pct": (
                "Share of tokens rebuilt",
                "This row's rebuilt tokens as a share of all broken-cache rebuilt tokens.",
            ),
            "control_cc_tokens": ("All cache writes", "Tokens every reply after this event wrote to the cache."),
            "control_cc_share_pct": (
                "Share of all cache writes",
                "This row's cache writes as a share of all cache writes.",
            ),
            "over_representation_points_tokens": (
                "Over-represented by (tokens)",
                "Share of rebuilt tokens minus share of all cache writes, in percentage points.",
            ),
        },
        value_labels={
            "compact_boundary": "Conversation summary (compaction)",
            "compact_summary": "Conversation summary text",
            "api_error": "API error and retry",
            "model_fallback": "Switched to a fallback model",
            "local_command": "Local command output",
            "hook_output": "Hook output",
            "cache_signal": "Model, mode or tool list changed",
            "reminder": "Claude Code reminder",
            "context_inject": "Files, memory or skills added by Claude Code",
            "queue_operation": "Message queued while Claude worked",
            "attachment": "Other Claude Code note",
            "meta": "Hidden message from Claude Code",
            "tool_denial": "Tool call denied",
            "tool_result": "Tool result",
            "task_notification": "Subagent or background task finished",
            "peer_message": "Message from another agent",
            "slash_command": "Slash command",
            "scheduled_task": "Scheduled or looped task",
            "interrupt": "You interrupted Claude",
            "human_text": "Your message",
            "unknown": "Nothing recorded",
            "limit_hit": "Usage limit reached",
            "limit_resume": "Resumed after a usage limit",
            "agent_terminated": "Subagent stopped early",
        },
        lead_columns=[
            "preceding_primary", "turns", "share_pct_turns", "control_share_pct_turns", "cc_tokens", "cc_share_pct",
            "over_representation_points_tokens",
        ],
    ),
    "recache_attachment_subsplit": TableCopy(
        title="Claude Code notes before a broken cache",
        help=Help(
            shows="The kinds of note Claude Code added right before a cache broken by a change.",
            read="A rebuild with several kinds of note counts once for each, so rows can add up to more than the "
            "number of rebuilds.",
            act="Notes from hooks, output styles or mode switches are ones you control. If one of them leads, "
            "check whether it needs to change mid-session.",
        ),
        columns={
            "attachment_type": ("Note type", "The kind of note Claude Code added."),
            "turns": ("Rebuilds", "Broken-cache rebuilds with this note right before."),
            "cc_tokens": ("Tokens rebuilt", "Tokens those rebuilds wrote to the cache."),
        },
        value_labels={
            "": "Note with no type",
            # hooks
            "hook_success": "Hook output",
            "hook_non_blocking_error": "Hook error (did not block)",
            "hook_blocking_error": "Hook blocked an action",
            "hook_system_message": "Hook message",
            "hook_additional_context": "Context added by a hook",
            "capture_note": "ClaudeGlass metrics capture note",
            "coaching_note": "ClaudeGlass coaching note",
            "hook_cancelled": "Hook cancelled",
            # settings and lists that change mid-session
            "model": "Model changed",
            "thinking_stripped": "Thinking removed from history",
            "ultra_effort_enter": "Ultra effort switched on",
            "ultra_effort_exit": "Ultra effort switched off",
            "deferred_tools_delta": "Deferred tool list changed",
            "deferred_tools_record": "Deferred tool list recorded",
            "mcp_instructions_delta": "MCP server instructions changed",
            "agent_listing_delta": "Agent list changed",
            "plan_mode": "Plan mode on",
            "plan_mode_exit": "Plan mode off",
            "auto_mode": "Auto mode on",
            "auto_mode_exit": "Auto mode off",
            "output_style": "Output style",
            "output_style_instructions": "Output style instructions",
            # reminders
            "total_tokens_reminder": "Token count reminder",
            "batching_reminder_sent": "Batching reminder",
            "silent_turn_reminder": "Silent reply reminder",
            "task_reminder": "Task list reminder",
            "date": "Today's date",
            "date_change": "Date changed",
            # files, memory and context
            "file": "File contents",
            "edited_text_file": "File edited",
            "read_truncation_notice": "File read cut short",
            "nested_memory": "Subfolder CLAUDE.md",
            "prompt_snapshot": "System prompt snapshot",
            "compact_file_reference": "File list after a summary",
            "plan_file_reference": "Plan file",
            "session_context": "Session details",
            "environment": "Environment details",
            "instructions": "CLAUDE.md and memory",
            "skill_listing": "Skills list",
            "invoked_skills": "Skills used",
            "directory": "Folder listing",
            "inlined_image_paths": "Image paths",
            "remote_session_change": "Remote session changed",
            "workflow_keyword_request": "Workflow keyword",
            # queue
            "queued_command": "Queued message",
        },
    ),
    "recache_by_agent_type": TableCopy(
        title="Cache rebuilds by agent type",
        help=Help(
            shows="Cache rebuilds for the main session and each agent type. Usage-limit pauses are left out.",
            read="Rebuild share is rebuilds out of that type's replies. Compare types doing similar work.",
            act="For a type with a high share or cost, check its waits and causes in {{page:cache/lifetime}}.",
        ),
        columns={
            "agent_type": ("", "The subagent type, or the main session."),
            "priced_turns": ("Replies", "Model replies with token counts."),
            "recache_turns": ("Rebuilds", "Replies that rebuilt the cache."),
            "recache_share_pct": ("Rebuild share", "Rebuilds as a share of this type's replies."),
            "cc_tokens": ("Tokens rebuilt", "Tokens those rebuilds wrote to the cache."),
            "avoidable_cost_usd": (
                "Avoidable cost",
                "What these rebuilds cost above reading the same tokens from the cache.",
            ),
        },
        value_labels={"top-level": "Main session", "unknown": "Subagent (type not recorded)"},
    ),
    "recache_huge_context": TableCopy(
        title="Cache reads from very large contexts",
        help=Help(
            shows="How much of your cache reading comes from replies with a very large context (200,000 tokens "
            "or more by default).",
            read="Every reply reads its whole context from the cache. A huge context makes each reply cost more, "
            "even when the cache works. It is not a price surcharge.",
            act="If the share is high, start new sessions for new tasks, or summarise the conversation sooner.",
        ),
        columns={
            "metric": ("", "This row covers every reply in the window."),
            "huge_ctx_turns": (
                "Replies with a huge context",
                "Replies at or over the size limit, in tokens of context.",
            ),
            "total_priced_turns": ("All replies", "Model replies with token counts."),
            "huge_ctx_cache_read_tokens": (
                "Cache reads from those replies",
                "Tokens those replies read from the cache.",
            ),
            "total_cache_read_tokens": ("All cache reads", "Tokens every reply read from the cache."),
            "share_pct": ("Share of cache reads", "Cache reads from huge contexts as a share of all cache reads."),
        },
        value_labels={"all": "All replies"},
        lead_columns=["share_pct", "huge_ctx_turns", "huge_ctx_cache_read_tokens"],
    ),
    "measured_miss_causes": TableCopy(
        title="Cache misses Claude Code measured (main session)",
        help=Help(
            shows="Why the main session's cache missed, as Claude Code itself diagnosed it. Your statusline "
            "logs these while you work. Subagents are not included.",
            read="These are measured, not inferred. Compare them with \"What happened right before each cache "
            "rebuild\": where the two disagree, trust this table.",
            act="If a changed tool list or system prompt leads, stop switching MCP servers, plugins or modes in "
            "the middle of a session. If expiry leads, see {{page:cache/lifetime}}.",
        ),
        columns={
            "cause": ("", "The cause Claude Code reported for the miss."),
            "misses": ("", "Cache misses with this cause."),
            "share_pct": ("Share of misses", "This cause's misses as a share of all measured misses."),
            "sessions": ("", "Sessions with at least one miss from this cause."),
        },
    ),
    "cache_ground_truth": TableCopy(
        title="Cache health per session, from your statusline",
        help=Help(
            shows="For each session your statusline logged, how often the main session's cache was warm, how "
            "many misses Claude Code counted, and their top causes.",
            read="Warm share counts statusline refreshes, not minutes, so one long cold pause can hide behind "
            "many quick warm refreshes.",
            act="",
        ),
        columns={
            "session_id": ("", "The session's id."),
            "rows_logged": ("Refreshes logged", "Statusline refreshes logged for this session."),
            "warm_share": ("", "Refreshes that found a warm cache, as a share of all refreshes."),
            "misses": ("", "Cache misses Claude Code counted in this session."),
            "top_miss_causes": ("", "Up to three causes Claude Code reported, with their counts."),
            "mean_recache_tokens_if_cold": (
                "Tokens to rebuild if cold",
                "On average, how many tokens a miss would have written again.",
            ),
        },
    ),
    "recache_by_group": TableCopy(
        title="Cache rebuilds by group",
        help=Help(
            shows="The cache rebuild totals, one row per group you chose with the group-by option.",
            read="Each row has the same columns as \"Cache rebuilds at a glance\", for that group only.",
            act="",
        ),
        columns={
            "group": ("", "The group this row covers."),
            "metric": ("", "Every reply in the group."),
            "transcripts": ("Conversation logs", "One per main session and one per subagent run."),
            "priced_turns": ("Replies", "Model replies with token counts."),
            "recache_turns": (
                "Cache rebuilds",
                "Replies that rebuilt the cache, including those after a usage-limit pause.",
            ),
            "recache_turn_share_pct": ("Rebuild share", "Cache rebuilds as a share of all replies."),
            "recache_cc_tokens": ("Tokens rebuilt", "Tokens those rebuilds wrote to the cache."),
            "total_cc_tokens": ("All cache writes", "Tokens every reply wrote to the cache."),
            "recache_cc_share_pct": ("Rebuild share of cache writes", "Tokens rebuilt as a share of all cache writes."),
            "avoidable_cost_usd": (
                "Avoidable cost",
                "What rebuilds cost above reading the same tokens from the cache. Leaves out rebuilds after a "
                "usage-limit pause.",
            ),
            "unavoidable_limit_expiry_cost_usd": (
                "Cost after usage-limit pauses",
                "The same extra cost for rebuilds after a usage-limit pause. You can't avoid these.",
            ),
        },
        value_labels={"all": "All", "top-level": "Main session", "unknown": "Not known"},
        lead_columns=[
            "group", "avoidable_cost_usd", "recache_turns", "recache_turn_share_pct", "recache_cc_tokens",
            "recache_cc_share_pct", "priced_turns",
        ],
    ),
    # -- cache lifetime -----------------------------------------------------
    "ttl_by_agent_type": TableCopy(
        title="Which cache lifetime costs less",
        help=Help(
            shows="For the main session and each agent type: what you paid. Also what you would have paid if every "
            "cache write had used a 5-minute or a 1-hour lifetime.",
            read="\"Over the cheaper lifetime\" is what you paid above the cheaper option; negative means your "
            "mix already beat both. \"Estimate error\" is how far the replay misses your real bill. Trust the "
            "advice only when it is low.",
            act="When the advice says to switch, change the setting named in the last column. A subscription "
            "ignores a 1-hour lifetime for subagents, so there the advice covers the main session only.",
        ),
        columns={
            "agent_type": ("", "The subagent type, or the main session."),
            "spawns": ("Runs", "Subagent runs of this type, or main sessions."),
            "priced_turns": ("Replies", "Model replies with token counts."),
            "observed_5m_pct": ("5-minute share", "Share of cache writes that used a 5-minute lifetime."),
            "observed_1h_pct": ("1-hour share", "Share of cache writes that used a 1-hour lifetime."),
            "gaps_over_5m": (
                "Waits over 5 minutes",
                "Waits between replies long enough for a 5-minute cache to expire. Usage-limit pauses are left out.",
            ),
            "gaps_over_1h": (
                "Waits over 1 hour",
                "Waits long enough for a 1-hour cache to expire. Usage-limit pauses are left out.",
            ),
            "limit_gaps": (
                "Usage-limit pauses",
                "Waits caused by a usage limit. The cache expires under either lifetime.",
            ),
            "gap_p50_s": ("Typical wait", "The middle wait between replies."),
            "gap_p90_s": ("Long wait", "9 in 10 waits between replies are shorter than this."),
            "cost_observed": ("Cost as billed", "Total cost of these replies with the cache lifetimes they used."),
            "cost_all_5m": (
                "Cost if all 5-minute",
                "Estimated total cost if every cache write used a 5-minute lifetime.",
            ),
            "cost_all_1h": ("Cost if all 1-hour", "Estimated total cost if every cache write used a 1-hour lifetime."),
            "best_policy": ("Cheaper lifetime", "Which of the two lifetimes would have cost less."),
            "delta_usd": (
                "Over the cheaper lifetime",
                "Cost as billed minus the cheaper lifetime's cost. Negative means your mix was already cheaper.",
            ),
            "delta_pct": ("Over the cheaper lifetime (%)", "The same difference as a share of cost as billed."),
            "saving_usd": (
                "Saving if switched",
                "What switching to the cheaper lifetime would save. Never below zero.",
            ),
            "fidelity_pct": (
                "Estimate error",
                "How far the replay of your own lifetime misses your real bill. Lower is more reliable.",
            ),
            "unsimulatable": (
                "Replies not replayed",
                "Replies with no readable wait time. They keep their real cost in both estimates.",
            ),
            "unpriced_turns": ("Replies with no price", "Replies from a model with no known price. Counted at zero."),
            "recommendation": (
                "Advice",
                "Whether to change this type's cache lifetime, and why not when advice is held back.",
            ),
            "lever": ("Setting to change", "The setting that controls this type's cache lifetime."),
        },
        value_labels={
            "top-level": "Main session",
            "unknown": "Subagent (type not recorded)",
            "5m": "5 minutes",
            "1h": "1 hour",
            "no material difference": "No change needed",
            "switch to 5m": "Switch to 5 minutes",
            "switch to 1h": "Switch to 1 hour",
            "keep 5m (already dominant)": "Keep 5 minutes (already used)",
            "keep 1h (already dominant)": "Keep 1 hour (already used)",
            "promptCacheTtl": "Cache lifetime in your settings",
            "subagentPromptCacheTtl": "Subagent cache lifetime in your settings",
        },
        lead_columns=[
            "agent_type", "spawns", "cost_observed", "best_policy", "saving_usd", "delta_pct", "recommendation",
        ],
    ),
    "ttl_gap_distribution": TableCopy(
        title="Waits between replies",
        help=Help(
            shows="How many waits between replies fell in each range, per agent type. Usage-limit pauses are left out.",
            read="Waits under 5 minutes keep either cache. Waits of 5 to 60 minutes keep only a 1-hour cache. "
            "Longer waits lose both.",
            act="",
        ),
        columns={
            "agent_type": ("", "The subagent type, or the main session."),
            "lt_1m": ("Under 1 min", "Waits under 1 minute."),
            "1_5m": ("1 to 5 min", "Waits of 1 to 5 minutes."),
            "5_15m": ("5 to 15 min", "Waits of 5 to 15 minutes."),
            "15_60m": ("15 to 60 min", "Waits of 15 to 60 minutes."),
            "gt_60m": ("Over 60 min", "Waits over 60 minutes."),
        },
        value_labels={"top-level": "Main session", "unknown": "Subagent (type not recorded)"},
    ),
    "ttl_wasted_writes": TableCopy(
        title="Cache writes never read back",
        help=Help(
            shows="How many cache writes expired before any later reply read them.",
            read="A write that is never read back is paid for and gives nothing. The last reply of each "
            "conversation is counted on its own, because nothing comes after it.",
            act="A high share with long waits points at expiry: see which lifetime costs less above.",
        ),
        columns={
            "agent_type": ("", "The subagent type, or the main session."),
            "writes": (
                "Cache writes",
                "Cache writes before the last reply. A write that used both lifetimes counts twice.",
            ),
            "wasted_writes": ("Never read back", "Writes that expired before a later reply read them."),
            "tokens_written": ("Tokens written", "Tokens in those cache writes."),
            "tokens_wasted": ("Tokens never read back", "Tokens in the writes that were never read back."),
            "share": ("Share never read back", "Tokens never read back as a share of tokens written."),
            "usd_wasted": ("Cost never read back", "What the unused cache writes cost."),
            "terminal_writes": (
                "Writes on the last reply",
                "Writes on a conversation's last reply. Nothing can read them, so they are left out of the share.",
            ),
        },
        value_labels={"top-level": "Main session", "unknown": "Subagent (type not recorded)"},
        lead_columns=[
            "agent_type", "usd_wasted", "share", "wasted_writes", "writes", "tokens_wasted", "terminal_writes",
        ],
    ),
    "ttl_premium_waste": TableCopy(
        title="When a 1-hour lifetime pays off",
        help=Help(
            shows="Every cache write, sorted by how long until the next reply: within 5 minutes, 5 to 60 "
            "minutes, or later.",
            read="Within 5 minutes, a 1-hour lifetime is extra cost for nothing. From 5 to 60 minutes, it saves "
            "a cache rebuild. Later than that, neither lifetime keeps the cache.",
            act="If the saving is larger than the extra cost, a 1-hour lifetime pays for this type.",
        ),
        columns={
            "agent_type": ("", "The subagent type, or the main session."),
            "h1_not_needed_tokens": (
                "1 hour not needed (tokens)",
                "Tokens written where the next reply came within 5 minutes.",
            ),
            "h1_not_needed_usd": (
                "1 hour not needed (extra cost)",
                "What a 1-hour lifetime would add on those tokens, for nothing.",
            ),
            "h1_earned_tokens": (
                "1 hour pays off (tokens)",
                "Tokens written where the next reply came 5 to 60 minutes later.",
            ),
            "h1_earned_usd": (
                "1 hour pays off (saving)",
                "The cache rebuild a 1-hour lifetime would avoid on the next reply.",
            ),
            "h1_expired_tokens": (
                "Expires anyway (tokens)",
                "Tokens written where the next reply came over an hour later, or never.",
            ),
            "h1_expired_usd": (
                "Expires anyway (extra cost)",
                "What a 1-hour lifetime would add on those tokens, for nothing.",
            ),
            "m5_fine_tokens": ("5 minutes enough (tokens)", "Tokens a 5-minute lifetime kept until the next reply."),
            "m5_loss_tokens": (
                "5 minutes expires (tokens)",
                "Tokens whose 5-minute cache expires before the next reply, where 1 hour would not.",
            ),
            "m5_loss_usd": (
                "5 minutes rebuild cost",
                "The cache rebuild a 5-minute lifetime causes on the next reply.",
            ),
            "m5_would_expire_tokens": (
                "Expires under both (tokens)",
                "Tokens whose cache expires before the next reply under either lifetime.",
            ),
        },
        value_labels={"top-level": "Main session", "unknown": "Subagent (type not recorded)"},
        lead_columns=[
            "agent_type", "h1_earned_usd", "h1_not_needed_usd", "h1_expired_usd", "m5_loss_usd", "h1_earned_tokens",
            "m5_loss_tokens",
        ],
    ),
    "ttl_break_even_share": TableCopy(
        title="Does a 1-hour lifetime pay for itself?",
        help=Help(
            shows="For each type: the extra cost of using a 1-hour lifetime everywhere, against the cache "
            "rebuilds a 5-minute lifetime causes.",
            read="A 1-hour lifetime pays when enough of your context comes after waits of 5 to 60 minutes. "
            "Compare that share with the break-even share: above it, 1 hour is cheaper.",
            act="Where the verdict is \"1 hour is cheaper\", consider a 1-hour lifetime for that type. "
            "Check the advice in the first table before changing anything.",
        ),
        columns={
            "agent_type": ("", "The subagent type, or the main session."),
            "premium_all_1h": (
                "Extra cost of 1 hour",
                "What you would pay on top if every cache write used a 1-hour lifetime.",
            ),
            "expiry_loss_all_5m": (
                "Rebuild cost of 5 minutes",
                "What cache rebuilds after waits of 5 to 60 minutes cost with a 5-minute lifetime.",
            ),
            "margin": ("Difference", "Rebuild cost minus extra cost. Positive means 1 hour pays for itself."),
            "in_window_pct": (
                "Context after 5 to 60 minute waits",
                "Share of your context, by size, that came after a wait of 5 to 60 minutes.",
            ),
            "break_even_pct": (
                "Break-even share",
                "The share in the previous column that 1 hour needs to pay for itself.",
            ),
            "verdict": ("Verdict", "Which lifetime is cheaper, or too close to call."),
        },
        value_labels={
            "top-level": "Main session",
            "unknown": "Subagent (type not recorded)",
            "5m pays": "5 minutes is cheaper",
            "1h pays": "1 hour is cheaper",
            "marginal": "Too close to call",
        },
    ),
    "ttl_near_miss": TableCopy(
        title="Waits that narrowly made or missed the cache",
        help=Help(
            shows="Waits that ended close to either side of the 5-minute and 1-hour limits. Close means within the "
            "near-miss window: 1 minute unless you changed it in config.toml.",
            read="A \"narrowly missed\" wait lost the cache by a small margin and rebuilt the whole context. Many of "
            "these mean a slightly quicker reply would have saved the rebuild.",
            act="If many waits narrowly miss 5 minutes, reply a little sooner, or use the status line countdown to "
            "see when the cache will expire.",
        ),
        columns={
            "agent_type": ("", "The subagent type, or the main session."),
            "near_5m_hit": (
                "Narrowly made 5 minutes",
                "Waits that ended shortly before 5 minutes. The cache was still there.",
            ),
            "near_5m_miss": (
                "Narrowly missed 5 minutes",
                "Waits that ended shortly after 5 minutes. The cache had expired.",
            ),
            "near_5m_miss_tokens": ("Tokens rewritten after narrowly missing 5 minutes", "The context rewritten on those replies."),
            "near_5m_miss_usd": (
                "Cost of narrowly missing 5 minutes",
                "Rewriting the whole context on those replies, at the 5-minute write price.",
            ),
            "near_1h_hit": ("Narrowly made 1 hour", "Waits that ended shortly before 1 hour. The cache was still there."),
            "near_1h_miss": ("Narrowly missed 1 hour", "Waits that ended shortly after 1 hour. The cache had expired."),
            "near_1h_miss_tokens": ("Tokens rewritten after narrowly missing 1 hour", "The context rewritten on those replies."),
            "near_1h_miss_usd": (
                "Cost of narrowly missing 1 hour",
                "Rewriting the whole context on those replies, at the 5-minute write price.",
            ),
        },
        value_labels={"top-level": "Main session", "unknown": "Subagent (type not recorded)"},
        lead_columns=[
            "agent_type", "near_5m_miss", "near_5m_miss_usd", "near_5m_hit", "near_1h_miss", "near_1h_miss_usd",
            "near_1h_hit",
        ],
    ),
    "ttl_addressable_share": TableCopy(
        title="Cache rebuilds a longer lifetime could prevent",
        help=Help(
            shows="Cache rebuilds split into those where the cache expired and those where it was broken by a "
            "change.",
            read="A longer cache lifetime can prevent expiry. It can't help when the context itself changed. "
            "Costs here are the full cache write, not only the avoidable part.",
            act="If the expired share is high, look at the cache lifetime advice. If the changed share is high, "
            "look at the causes on {{page:cache/rebuilds}}.",
        ),
        columns={
            "agent_type": ("", "The subagent type, or the main session."),
            "full_expiry_tokens": ("Expired (tokens)", "Tokens rewritten because the cache had expired."),
            "full_expiry_usd": ("Expired (cost)", "What those cache writes cost."),
            "full_expiry_share": ("Expired share", "Expired tokens as a share of all rebuilt tokens."),
            "prefix_invalidated_tokens": (
                "Broken by a change (tokens)",
                "Tokens rewritten because something early in the context changed.",
            ),
            "prefix_invalidated_usd": ("Broken by a change (cost)", "What those cache writes cost."),
            "prefix_invalidated_share": ("Broken share", "Broken-cache tokens as a share of all rebuilt tokens."),
        },
        value_labels={"top-level": "Main session", "unknown": "Subagent (type not recorded)"},
    ),
    "ttl_cache_economy": TableCopy(
        title="What the cache saves you",
        help=Help(
            shows="For each type: what you paid for cache writes and reads, and what the same tokens would cost "
            "with no cache.",
            read="\"Return on cache writes\" is the saving for each unit spent on writes. 10 means every 1 spent "
            "on writes saved 10.",
            act="A low return means the cache is written often but read little. Check the waits and rebuilds for "
            "that type.",
        ),
        columns={
            "agent_type": ("", "The subagent type, or the main session."),
            "tokens_written": ("Cache writes (tokens)", "Tokens written to the cache."),
            "tokens_read": ("Cache reads (tokens)", "Tokens read from the cache."),
            "write_usd": ("Cache write cost", "What the cache writes cost."),
            "read_usd": ("Cache read cost", "What the cache reads cost."),
            "uncached_equivalent_usd": ("Cost with no cache", "What the same tokens would cost as plain input."),
            "net_saving_usd": (
                "Saved by the cache",
                "Cost with no cache, minus what you paid for cache writes and reads.",
            ),
            "cache_roi": ("Return on cache writes", "Saving divided by cache write cost."),
        },
        value_labels={"top-level": "Main session", "unknown": "Subagent (type not recorded)", "overall": "All"},
        lead_columns=[
            "agent_type", "net_saving_usd", "cache_roi", "uncached_equivalent_usd", "write_usd", "read_usd",
            "tokens_read",
        ],
    ),
    # -- usage limits -------------------------------------------------------
    "limits_summary": TableCopy(
        title="Usage limits at a glance",
        help=Help(
            shows="Totals for the window: limit stops, automatic resumes, subagents stopped early, pauses, and "
            "the cache writes after each pause.",
            read="One pause can show several limit messages, so stops can be higher than pauses. The cost after "
            "a pause is a full cache write that you can't avoid.",
            act="If pauses are frequent, spread heavy work over the day or across the week.",
        ),
        columns={
            "metric": ("", "This row covers the whole window."),
            "transcripts": ("Conversation logs", "One per main session and one per subagent run."),
            "sessions_affected": (
                "Sessions affected",
                "Sessions with a limit stop, a resume after one, or a subagent stopped early.",
            ),
            "limit_hits": ("Limit stops", "Times Claude Code showed a usage-limit message."),
            "session_limit_hits": ("5-hour limit stops", "Stops at the rolling 5-hour session limit."),
            "weekly_limit_hits": ("Weekly limit stops", "Stops at the weekly limit."),
            "limit_resumes": ("Automatic resumes", "Times the desktop app carried on by itself after a limit reset."),
            "agents_terminated": (
                "Subagents stopped early",
                "Subagents Claude Code ended before they finished, for any reason.",
            ),
            "agents_terminated_rate_limit": ("Stopped by a limit", "Of those, the subagents stopped by a usage limit."),
            "pause_count": ("Pauses", "Waits between replies that spanned a usage limit."),
            "pause_total_s": ("Total pause time", "All those pauses added together."),
            "limit_turn_cc_tokens": (
                "Cache writes after a pause",
                "Tokens written to the cache on the first reply after each pause.",
            ),
            "limit_turn_write_cost_usd": (
                "Cost of cache writes after a pause",
                "What those cache writes cost in full. {{page:cache/rebuilds}} shows a smaller figure: large "
                "contexts only, and only the cost above a cache read.",
            ),
        },
        value_labels={"all": "All"},
        # Total pause time leads "How long usage-limit pauses lasted", the
        # table beside this one, so it isn't a tile here too.
        lead_columns=["limit_hits", "sessions_affected", "limit_turn_write_cost_usd"],
    ),
    "limits_hits_by_kind": TableCopy(
        title="Which limit you hit",
        help=Help(
            shows="Limit stops split between the 5-hour session limit and the weekly limit.",
            read="Session-limit stops reset within hours. Weekly-limit stops can block you for days.",
            act="",
        ),
        columns={
            "kind": ("Limit", "Which usage limit stopped you."),
            "hits": ("Stops", "Times this limit stopped a reply."),
            "share_pct": ("Share", "Share of all limit stops."),
        },
        value_labels={"session_limit": "5-hour session limit", "weekly_limit": "Weekly limit"},
    ),
    "limits_agent_terminated": TableCopy(
        title="Subagents stopped early",
        help=Help(
            shows="Subagents Claude Code ended before they finished, and why.",
            read="A stopped subagent returns no report, so what it spent produced nothing.",
            act="",
        ),
        columns={
            "kind": ("Reason", "Why Claude Code stopped the subagent."),
            "terminated": ("Subagents", "How many subagents stopped for this reason."),
            "share_pct": ("Share", "Share of all subagents stopped early."),
        },
        value_labels={"rate_limit": "Usage limit", "other": "Other, or not stated"},
    ),
    "limits_pauses": TableCopy(
        title="How long usage-limit pauses lasted",
        help=Help(
            shows="How many times you waited for a usage limit to reset, and for how long.",
            read="Each pause is the wait from the reply before the limit to the first reply after it.",
            act="",
        ),
        columns={
            "metric": ("", "This row covers the whole window."),
            "pause_count": ("Pauses", "Waits between replies that spanned a usage limit."),
            "total_s": ("Total pause time", "All pauses added together."),
            "mean_s": ("Average pause", "Total pause time divided by the number of pauses."),
        },
        value_labels={"all": "All"},
        lead_columns=["pause_count", "total_s", "mean_s"],
    ),
    "limits_reset_hour_histogram": TableCopy(
        title="When your limits reset",
        help=Help(
            shows="Limit stops grouped by the local hour the limit said it would reset.",
            read="A peak at one hour shows when you usually run out. Stops with no reset time are left out.",
            act="If limits often reset at a busy hour, start heavy work soon after a reset.",
        ),
        columns={
            "local_hour": ("Local hour", "The hour of day the limit reset, in your time zone."),
            "resets": ("Limit stops", "Limit stops that named a reset in this hour."),
            "share_pct": ("Share", "Share of limit stops with a reset time."),
        },
        value_labels={f"{hour:02d}": f"{hour:02d}:00" for hour in range(24)},
    ),
    "limits_by_agent_type": TableCopy(
        title="Usage limits by agent type",
        help=Help(
            shows="Limit stops, pauses and the cache writes after them, for the main session and each agent type.",
            read="Most stops show up in the main session. A subagent type with many stops is often running "
            "when you reach the limit.",
            act="",
        ),
        columns={
            "agent_type": ("", "The subagent type, or the main session."),
            "transcripts": ("Conversation logs", "Main sessions or subagent runs of this type."),
            "limit_hits": ("Limit stops", "Times this type showed a usage-limit message."),
            "limit_resumes": ("Automatic resumes", "Times this type carried on by itself after a limit reset."),
            "agents_terminated": ("Subagents stopped early", "Notices of a subagent stopped early, seen by this type."),
            "pause_count": ("Pauses", "Waits between replies that spanned a usage limit."),
            "pause_total_s": ("Total pause time", "All those pauses added together."),
            "pause_median_s": ("Typical pause", "The middle pause length."),
            "pause_max_s": ("Longest pause", "The longest pause."),
            "limit_turn_cc_tokens": (
                "Cache writes after a pause",
                "Tokens written to the cache on the first reply after each pause.",
            ),
            "limit_turn_write_cost_usd": (
                "Cost of cache writes after a pause",
                "What those cache writes cost in full.",
            ),
        },
        value_labels={"top-level": "Main session", "unknown": "Subagent (type not recorded)"},
        lead_columns=[
            "agent_type", "limit_hits", "limit_turn_write_cost_usd", "pause_count", "pause_total_s",
            "agents_terminated", "limit_resumes",
        ],
    ),
    "limits_csv_cross_check": TableCopy(
        title="Usage log compared with conversation logs",
        help=Help(
            shows="How often your usage log recorded a limit as fully used, next to the limit stops found in "
            "your conversation logs.",
            read="The two are recorded separately and won't match exactly. The usage log samples all the time; "
            "a conversation log only records a stop when a reply was blocked.",
            act="A large, lasting gap may mean one of the two logs is missing data.",
        ),
        columns={
            "window": ("Window", "Which usage limit this row compares."),
            "csv_exhaustion_rows": ("Usage log at the limit", "Usage log entries that showed this limit fully used."),
            "transcript_hits": ("Stops in conversation logs", "Limit stops found in your conversation logs."),
            "delta": ("Difference", "Conversation-log stops minus usage-log entries."),
        },
        value_labels={"five_hour": "5-hour session limit", "seven_day": "Weekly limit"},
    ),
    # -- conversation summaries ---------------------------------------------
    "compactions_summary": TableCopy(
        title="Conversation summaries at a glance",
        help=Help(
            shows="How many sessions were summarised, and how large the context was before and after. Also what "
            "writing each summary cost, and the cache write on the reply after it.",
            read="Costs only count replies within 15 minutes of the summary. \"Rebuilt most of the cache\" is the "
            "part where the next reply read under a fifth of its context from the cache.",
            act="If summaries are frequent and large, split long tasks into separate sessions.",
        ),
        columns={
            "metric": ("", "What is measured."),
            "value": ("", "The figure, in the unit named on the left."),
        },
        value_labels={
            "Sessions with >=1 compaction": "Sessions with at least one summary",
            "Total sessions": "All sessions",
            "Compactions per session (mean)": "Summaries per session (average over all sessions)",
            "Compactions per compacting session (mean)": "Summaries per summarised session (average)",
            "Compactions per session (max)": "Most summaries in one session",
            "Pre-compaction tokens (median)": "Context before a summary (typical, tokens)",
            "Post-compaction tokens (median)": "Context after a summary (typical, tokens)",
            "Dropped tokens (total)": "Tokens removed by summaries (total)",
            "Dropped tokens (share of cache_creation)": "Tokens removed, as a % of all cache writes",
            "Dropped tokens (share of new_tokens: input+cache_creation)": (
                "Tokens removed, as a % of all new input and cache writes"
            ),
            "Mean duration (ms)": "Average time to summarise (milliseconds)",
            "Summary requests (estimated, USD)": "Cost of writing the summaries (estimated)",
            "Total post-compaction write cost (USD)": "Cache write cost on the reply after a summary",
            "Total post-compaction RE-CACHE-flagged write cost (USD)": (
                "Of that, replies that rebuilt most of the cache"
            ),
        },
        row_kinds={
            "Sessions with >=1 compaction": "int",
            "Total sessions": "int",
            "Compactions per session (mean)": "float",
            "Compactions per compacting session (mean)": "float",
            "Compactions per session (max)": "int",
            "Pre-compaction tokens (median)": "tokens",
            "Post-compaction tokens (median)": "tokens",
            "Dropped tokens (total)": "tokens",
            "Dropped tokens (share of cache_creation)": "pct",
            "Dropped tokens (share of new_tokens: input+cache_creation)": "pct",
            "Mean duration (ms)": "int",
            "Summary requests (estimated, USD)": "money",
            "Total post-compaction write cost (USD)": "money",
            "Total post-compaction RE-CACHE-flagged write cost (USD)": "money",
        },
    ),
    "compactions_trigger_mix": TableCopy(
        title="What started each summary",
        help=Help(
            shows="Whether each summary ran automatically or because you asked for it.",
            read="Automatic summaries run when the context is nearly full.",
            act="",
        ),
        columns={
            "trigger": ("Started by", "What started the summary."),
            "count": ("Summaries", "How many summaries."),
            "pct": ("Share", "Share of all summaries."),
        },
        value_labels={
            "auto": "Automatic (context nearly full)",
            "manual": "You ran /compact",
            "unknown": "Not recorded",
        },
    ),
    "compactions_per_session": TableCopy(
        title="Sessions that removed the most context",
        help=Help(
            shows="The 20 sessions whose summaries removed the most tokens.",
            read="Many summaries in one session means it ran long enough to fill the context again and again.",
            act="",
        ),
        columns={
            "session": ("", "The session id."),
            "count": ("Summaries", "Summaries in this session, including its subagents."),
            "dropped_tokens": ("Tokens removed", "Tokens the summaries removed from the context."),
            "write_cost": (
                "Cache write cost after summaries",
                "Cache writes on the reply after each summary. Left out when that reply came over 15 minutes later.",
            ),
        },
    ),
    # -- savings: tool output kept in context ---------------------------------
    "carry_by_tool": TableCopy(
        title="Cost of keeping each tool's output in context",
        help=Help(
            shows="One row per tool: how much output it added, how many replies that output stayed for, "
            "and what keeping it cost.",
            read="Kept tokens are output size times later replies. Share of cache compares them with every "
            "token read from or written to the cache. The rest is instructions, messages and replies, so "
            "rows don't add up to 100%.",
            act="A tool above about 25% of the cache is worth capping. \"Saving if capped\" shows what "
            "cutting its outputs to 8,000 tokens would have saved.",
        ),
        columns={
            "key": ("Tool", "The tool that produced the output."),
            "result_count": ("Outputs", "Tool outputs counted. Calls to one tool in the same reply count as one."),
            "tokens_entered": ("Output size (tokens)", "Total size of those outputs, estimated from characters."),
            "mean_turns_carried": (
                "Replies kept for",
                "Average number of later replies each output stayed in context, until a summary or the end.",
            ),
            "carry_tokens": ("Kept tokens", "Output size times the number of later replies it stayed for."),
            "carry_cost_usd": (
                "Cost of keeping it",
                "Cache read and cache write cost of the kept tokens, at list price.",
            ),
            "share_of_cache_volume_pct": (
                "Share of cache",
                "Kept tokens as a share of every token read from or written to the cache.",
            ),
            "saving_if_capped_usd": (
                "Saving if capped",
                "What cutting each of these outputs to 8,000 tokens would have saved, at list price.",
            ),
        },
        lead_columns=[
            "key", "carry_cost_usd", "saving_if_capped_usd", "share_of_cache_volume_pct", "result_count",
            "carry_tokens", "mean_turns_carried",
        ],
    ),
    "carry_by_agent_type": TableCopy(
        title="Cost of keeping tool output in context, by agent type",
        help=Help(
            shows="The same measure as the tool table, for the main session and each subagent type.",
            read="Long conversations keep output for more replies, so the main session usually carries "
            "output the longest.",
            act="If one subagent type keeps a lot of output, ask it to read less or to trim command output.",
        ),
        columns={
            "key": ("Agent type", "The main session or the subagent type that received the output."),
            "result_count": ("Outputs", "Tool outputs counted. Calls to one tool in the same reply count as one."),
            "tokens_entered": ("Output size (tokens)", "Total size of those outputs, estimated from characters."),
            "mean_turns_carried": (
                "Replies kept for",
                "Average number of later replies each output stayed in context, until a summary or the end.",
            ),
            "carry_tokens": ("Kept tokens", "Output size times the number of later replies it stayed for."),
            "carry_cost_usd": (
                "Cost of keeping it",
                "Cache read and cache write cost of the kept tokens, at list price.",
            ),
            "share_of_cache_volume_pct": (
                "Share of cache",
                "Kept tokens as a share of every token read from or written to the cache.",
            ),
            "saving_if_capped_usd": (
                "Saving if capped",
                "What cutting each of these outputs to 8,000 tokens would have saved, at list price.",
            ),
        },
        value_labels={"top-level": "Main session", "unknown": "Subagent (type not recorded)"},
        lead_columns=[
            "key", "carry_cost_usd", "saving_if_capped_usd", "share_of_cache_volume_pct", "result_count",
            "carry_tokens", "mean_turns_carried",
        ],
    ),
    "carry_top_results": TableCopy(
        title="Most expensive single tool outputs",
        help=Help(
            shows="The 10 tool outputs that cost the most to keep in context. Only the tool name and sizes "
            "are stored, never content, paths or commands.",
            read="A large output early in a long conversation costs the most, because every later reply pays "
            "for it again.",
            act="If these are file reads, read only the lines you need. If they are command output, trim it "
            "before Claude sees it.",
        ),
        columns={
            "tool": ("", "The tool that produced the output."),
            "agent_type": ("Where", "The main session or the subagent type that received it."),
            "tokens": ("Size (tokens)", "The output's size, estimated from characters."),
            "turns_carried": ("Replies kept for", "Later replies it stayed in context for."),
            "carry_cost_usd": ("Cost of keeping it", "Cache read and cache write cost of keeping it, at list price."),
        },
        value_labels={"top-level": "Main session", "unknown": "Subagent (type not recorded)"},
    ),
    "carry_truncation_savings": TableCopy(
        title="Saving if large tool outputs were capped",
        help=Help(
            shows="What you would have saved if every tool output had been cut to 2,000 or 8,000 tokens "
            "before Claude saw it.",
            read="Worked out directly from the cost of keeping each output, with no replay. It assumes "
            "nothing else changes. Claude may need an extra read to recover what was cut, so treat it as "
            "an upper bound.",
            act="If the 8,000-token row shows a real saving, cap long outputs at the source.",
        ),
        columns={
            "truncate_to_tokens": ("Cap", "The largest size any one tool output is allowed, in tokens."),
            "results_affected": ("Outputs over the cap", "Tool outputs larger than the cap."),
            "tokens_saved": ("Kept tokens saved", "Tokens over the cap, times the later replies they stayed for."),
            "usd_saved": ("Saving", "Cost of keeping the part over the cap, at list price."),
        },
        value_labels={"2000": "2,000 tokens", "8000": "8,000 tokens"},
    ),
    "carry_output_cap_savings": TableCopy(
        title="Saving from Claude Code's output limits",
        help=Help(
            shows="Each Claude Code setting that limits a tool's output, at the value the tool output check suggests. "
            "It shows how many outputs it would have cut and what that would have saved.",
            read="Worked out the same way as the table above, but only over the outputs that setting limits. "
            "Outputs from one reply are counted together, so treat it as an upper bound.",
            act="Set a limit only if it saves a real share of what those outputs cost. Most shell output is "
            "short, so a lower shell limit often saves little and hides the end of a long log.",
        ),
        columns={
            "setting": ("Setting", "The environment variable in settings.json."),
            "value": ("Suggested value", "The value the tool output check suggests."),
            "cap_tokens": ("Limit (tokens)", "That value in tokens, estimated from characters for the shell limit."),
            "results": ("Outputs it limits", "Outputs from the tools this setting applies to."),
            "results_affected": ("Outputs over the limit", "Those outputs larger than the limit."),
            "tokens_saved": ("Kept tokens saved", "Tokens over the limit, times the later replies they stayed for."),
            "usd_saved": ("Saving", "Cost of keeping the part over the limit, at list price."),
            "carry_cost_usd": ("Cost of keeping them", "What keeping every output it applies to cost, at list price."),
        },
        lead_columns=["setting", "value", "usd_saved", "tokens_saved", "results_affected", "results", "carry_cost_usd"],
    ),
    # -- savings: auto-compact window -------------------------------------------
    "compaction_sim_by_window": TableCopy(
        title="Main session cost at each auto-compact window",
        help=Help(
            shows="One row per window size, for your main sessions. \"As now\" is your real cost; every other "
            "row is simulated.",
            read="A negative change means cheaper. Summaries per session shows how often each window would "
            "summarise. More summaries means more detail lost and more re-reading.",
            act="Look for the smallest window with a clear saving and no more than about 2 summaries per session.",
        ),
        columns={
            "window": ("Window (tokens)", "The context size at which the conversation is summarised."),
            "compactions_per_session": (
                "Summaries per session",
                "Average conversation summaries per session under this window, real ones included.",
            ),
            "mean_ctx": ("Average context (tokens)", "Average context size per reply under this window, simulated."),
            "cost": (
                "Cost",
                "Simulated cost of your main sessions under this window, at list price. \"As now\" is your real cost.",
            ),
            "delta_usd": ("Change in cost", "Simulated cost minus real cost, at list price. Negative means cheaper."),
            "delta_pct": ("Change (%)", "The change as a share of your real cost. Negative means cheaper."),
        },
        value_labels={"none": "As now (no extra summaries)"},
    ),
    "compaction_sim_by_agent_type": TableCopy(
        title="Best auto-compact window for each agent type",
        help=Help(
            shows="For the main session and each subagent type: the window with the lowest simulated cost, "
            "and the saving against your real cost.",
            read="Savings are simulated and never below zero. \"No clear saving\" means the best window saves "
            "under 5% of the cost, or under $1.00 at list price.",
            act="The auto-compact window is one setting for the whole session, not per agent type. Choose it "
            "from the main session row; subagent rows show where summaries would matter most.",
        ),
        columns={
            "agent_type": ("", "The main session or the subagent type."),
            "sessions": ("Runs", "Main session: how many sessions. Subagents: how many runs of that type."),
            "observed_cost": ("Real cost", "Measured cost, at list price."),
            "best_window": ("Best window (tokens)", "The window with the lowest simulated cost."),
            "best_cost": ("Cost at best window", "Simulated cost at that window, at list price."),
            "saving_usd": (
                "Simulated saving",
                "Real cost minus cost at the best window, at list price. Never below 0.",
            ),
            "delta_pct": (
                "Change (%)",
                "Simulated change at the best window, against real cost. Negative means cheaper.",
            ),
            "recommendation": (
                "Suggestion",
                "Whether the simulated saving is big enough to act on. A subagent row names its cheapest window "
                "but leaves the setting to the main session row.",
            ),
        },
        value_labels={
            "top-level": "Main session",
            "unknown": "Unnamed subagent",
            "none": "As now (no extra summaries)",
            "no material difference": "No clear saving",
        },
        lead_columns=[
            "agent_type", "best_window", "saving_usd", "delta_pct", "observed_cost", "sessions", "recommendation",
        ],
    ),
    "compaction_sim_by_task": TableCopy(
        title="Best auto-compact window for each kind of task",
        help=Help(
            shows="Each kind of task metrics capture has seen enough of in your main sessions. For each: the window "
            "with the lowest simulated cost, and the saving against your real cost.",
            read="Savings are simulated and never below zero. \"No clear saving\" means the best window saves "
            "under 5% of the cost, or under $1.00 at list price. A kind of task only appears once you have "
            "enough sessions reporting it.",
            act="Save a window per kind of task as a profile, the same way as a model or effort choice.",
        ),
        columns={
            "task": ("Kind of task", "The kind of task, as reported by metrics capture."),
            "sessions": ("Sessions", "How many main sessions reported this kind of task."),
            "observed_cost": ("Real cost", "Measured cost of those sessions, at list price."),
            "best_window": ("Best window (tokens)", "The window with the lowest simulated cost."),
            "best_cost": ("Cost at best window", "Simulated cost at that window, at list price."),
            "saving_usd": (
                "Simulated saving",
                "Real cost minus cost at the best window, at list price. Never below 0.",
            ),
            "delta_pct": (
                "Change (%)",
                "Simulated change at the best window, against real cost. Negative means cheaper.",
            ),
            "recommendation": ("Suggestion", "Whether the simulated saving is big enough to act on."),
        },
        value_labels={
            "none": "As now (no extra summaries)",
            "no material difference": "No clear saving",
        },
        lead_columns=["task", "best_window", "saving_usd", "delta_pct", "observed_cost", "sessions", "recommendation"],
    ),
    "compaction_sim_fidelity": TableCopy(
        title="Simulation check against your real sessions",
        help=Help(
            shows="Main sessions whose auto-compact window is known, replayed at that same window.",
            read="Error is how far the simulated cost is from the real cost. It should be close to 0%. Above "
            "10% means the simulation doesn't fit your sessions well, so trust the other tables less.",
            act="",
        ),
        columns={
            "session": ("Session", "The session this row replays."),
            "configured_window": ("Your window (tokens)", "The auto-compact window set when the session ran."),
            "simulated_cost": ("Simulated cost", "Cost replayed at your own window, at list price."),
            "observed_cost": ("Real cost", "Measured cost, at list price."),
            "fidelity_pct": ("Error", "Gap between simulated and real cost, as a share of real cost. Lower is better."),
        },
    ),
    # -- savings: cheaper model ---------------------------------------------------
    "model_swap_by_agent_type": TableCopy(
        title="Cost of each agent type on other models",
        help=Help(
            shows="One row for the main session and one per subagent type. Each shows its real cost, the same tokens "
            "repriced at every known model, and the saving one tier down.",
            read="Only real cost is measured. The saving is the most you could save at today's usage, and it "
            "appears only when a cheaper tier exists. A subagent's saving counts only the runs its agent file "
            "decides.",
            act="A saving above 10% and above $1.00 at list price, on at least 5 runs or 200 replies, becomes "
            "a recommendation.",
        ),
        columns={
            "agent_type": ("", "The main session or the subagent type."),
            "spawns": ("Runs", "Main session: how many sessions. Subagents: how many times that type started."),
            "priced_turns": ("Replies", "Model replies counted, including any with an unknown model."),
            "unpriced_turns": (
                "Replies with an unknown model",
                "Replies whose model isn't in this tool's price list. They count as zero in real cost.",
            ),
            "observed_model": ("Model used", "The model most replies used, with a count of any others."),
            "observed_cost": ("Real cost", "Measured tokens at the list price of the models actually used."),
            "best_cheaper_alternative_model": (
                "One tier down",
                "The current model one tier cheaper than the one used. Empty when there is none.",
            ),
            "best_cheaper_alternative": ("Verdict", "The cheaper model and its saving, or why there isn't one."),
            "saving_usd": (
                "Most you could save",
                "Cost of the runs that setting decides, minus their cost one tier down, at list price.",
            ),
            "saving_pct": ("Most you could save (%)", "That saving as a share of those runs' cost."),
            "lever": ("Where to change it", "The file and field that set this agent type's model."),
            "lever_runs": (
                "Runs it decides",
                "Main session: every session. Subagents: runs started without a model of their own, which follow "
                "the agent file.",
            ),
            "lever_priced_turns": ("Replies on those runs", "Model replies on the runs that setting decides."),
            "lever_model": ("Model on those runs", "The model most of those replies used."),
            "lever_cost": ("Cost of those runs", "Measured cost of the runs that setting decides, at list price."),
            "workflow_runs": (
                "From workflows",
                "Runs a workflow script started. The script sets their model, so the agent file doesn't.",
            ),
            "spawn_model_runs": (
                "Model named at start",
                "Runs given a model when they started. That model wins over the agent file.",
            ),
        },
        value_labels={
            "top-level": "Main session",
            "unknown": "Unnamed subagent",
            "model (settings.json)": "model setting in settings.json",
        },
        lead_columns=[
            "agent_type", "observed_model", "observed_cost", "best_cheaper_alternative_model", "saving_usd",
            "saving_pct",
        ],
    ),
    "model_swap_summary": TableCopy(
        title="All Fable and Opus subagents one tier down",
        help=Help(
            shows="The combined saving if every subagent type on Fable or Opus moved one tier down. The main "
            "session is left out.",
            read="The most you could save, not a forecast: same tokens and replies at cheaper prices. Agent "
            "types that would be no cheaper one tier down are left out. So are runs whose model a workflow "
            "script or the run's start named.",
            act="",
        ),
        columns={
            "scope": ("Scope", "Which agent types this row covers."),
            "agent_types": ("Agent types", "Subagent types on Fable or Opus with a cheaper tier available."),
            "observed_cost_usd": (
                "Real cost",
                "Measured cost of the runs those agent types' files decide, at list price.",
            ),
            "cost_after_tier_down_usd": ("Cost one tier down", "The same tokens at the cheaper tier's list price."),
            "saving_usd": ("Most you could save", "Real cost minus the cost one tier down, at list price."),
            "saving_pct": ("Most you could save (%)", "That saving as a share of real cost."),
        },
        value_labels={"subagent types currently on Fable/Opus": "Subagent types on Fable or Opus"},
        lead_columns=["saving_usd", "saving_pct", "observed_cost_usd", "agent_types"],
    ),
    "model_swap_agent_file_runs": TableCopy(
        title="Runs each agent file's model decides, on other models",
        help=Help(
            shows="Per subagent type, only its runs started without a model of their own, repriced at every "
            "known model.",
            read="These are the runs a model line in the agent file would change. A what-if for that line "
            "prices these runs.",
            act="",
        ),
        columns={
            "agent_type": ("", "The subagent type."),
            "runs": ("Runs", "Runs started without a model of their own."),
            "priced_turns": ("Replies", "Model replies on those runs."),
            "observed_model": ("Model used", "The model most of those replies used."),
            "observed_cost": ("Real cost", "Measured cost of those runs, at list price."),
        },
    ),
    # -- savings: building fresh after a plan ------------------------------------
    "plan_handoff_summary": TableCopy(
        title="Building in a fresh session after a big plan",
        help=Help(
            shows="Main sessions with an approved plan, and what starting each build fresh from the plan could "
            "have saved.",
            read="A plan counts when a fresh start would drop a lot of context and many replies follow it. The "
            "saving overlaps with the auto-compact saving.",
            act="",
        ),
        columns={
            "scope": ("Scope", "Which sessions this row covers."),
            "main_sessions": ("Main sessions", "Main sessions in this window, scheduled checks left out."),
            "sessions_with_plan": ("Sessions with an approved plan", "Main sessions where you approved at least one plan."),
            "qualifying_sessions": (
                "Sessions where it pays",
                "Sessions with a plan big enough, and followed by enough replies, to be worth a fresh start.",
            ),
            "tokens_carried_median": (
                "Planning context kept (median)",
                "Context a fresh start would have dropped, over the plans that count. The middle value.",
            ),
            "saving_usd": ("Most you could save", "The replies after those plans without the planning context, at list price."),
            "saving_pct": ("Share of main-session cost", "That saving as a share of all main-session cost."),
            "main_session_usd": ("Main-session cost", "Cost of every main session in this window, at list price."),
            "build_usd": (
                "Cost after approved plans",
                "Replies after each approved plan, up to the next plan, at list price.",
            ),
            "build_usd_sonnet": (
                "Same at Sonnet's prices",
                "Those replies repriced at Sonnet's list price. Empty when the price list has no Sonnet.",
            ),
        },
        value_labels={"main sessions": "Main sessions"},
        lead_columns=["saving_usd", "qualifying_sessions", "tokens_carried_median", "saving_pct"],
    ),
    "plan_handoff_by_session": TableCopy(
        title="Sessions with an approved plan",
        help=Help(
            shows="One row per main session where you approved a plan, largest saving first.",
            read="Planning context kept is what a fresh start would have dropped. Replies after a conversation "
            "summary or the next plan aren't counted.",
            act="",
        ),
        columns={
            "session": ("Session", "The session's id."),
            "plans": ("Approved plans", "Plans you approved in this session."),
            "tokens_carried": ("Planning context kept", "The most context any of its plans would have dropped."),
            "later_turns": ("Replies after the plan", "Replies after its plans, up to a summary or the next plan."),
            "qualifies": ("Worth a fresh session", "Whether any of its plans was big enough to count."),
            "saving_usd": ("Most you could save", "Those replies without the planning context, at list price."),
            "build_turns": ("Build replies", "Replies after its approved plans, up to the next plan."),
            "build_usd": ("Build cost", "Cost of those replies, at list price."),
            "build_usd_sonnet": ("Build cost at Sonnet's prices", "The same replies at Sonnet's list price."),
        },
        value_labels={"yes": "Yes", "no": "No"},
        lead_columns=["session", "tokens_carried", "later_turns", "qualifies", "saving_usd", "build_usd"],
    ),
    # -- splitting long subagent runs ------------------------------------------------
    "run_split_summary": TableCopy(
        title="Splitting long subagent runs",
        help=Help(
            shows="Every subagent run in this window, and what splitting the long ones at each agent type's best "
            "interval could have saved.",
            read="Only agent types where splitting pays are counted. The saving overlaps with the auto-compact "
            "saving.",
            act="",
        ),
        columns={
            "scope": ("Scope", "Which runs this row covers."),
            "runs": ("Subagent runs", "Subagent runs in this window. Workflow agents are left out."),
            "paying_agents": ("Agent types where splitting pays", "Agent types whose runs would cost less split."),
            "long_runs": ("Runs it would split", "Their runs long enough to split at their best interval."),
            "splits": ("Splits", "How many times those runs would start fresh."),
            "dropped_median": (
                "Context each split drops (median)",
                "Context a fresh start would leave behind at each split. The middle value.",
            ),
            "long_run_usd": ("Cost of those runs", "What the runs it would split cost, at list price."),
            "saving_usd": (
                "Most you could save",
                "Those runs split, less what splitting adds back, at list price.",
            ),
            "saving_pct": ("Share of subagent cost", "That saving as a share of all subagent cost."),
            "agent_usd": ("Subagent cost", "Cost of every subagent run in this window, at list price."),
        },
        value_labels={"subagent runs": "Subagent runs"},
        lead_columns=["saving_usd", "paying_agents", "long_runs", "saving_pct"],
    ),
    "run_split_by_agent": TableCopy(
        title="By agent type",
        help=Help(
            shows="One row per agent type: how long its runs get, and the split interval that saves most.",
            read="An empty interval means splitting doesn't pay for that agent type at any interval tried.",
            act="",
        ),
        columns={
            "agent_type": ("Agent type", "The subagent type."),
            "runs": ("Runs", "Its runs in this window."),
            "longest_run": ("Longest run (replies)", "Replies in its longest run."),
            "every_n": ("Split every (replies)", "The interval that saves most. Empty when none saves anything."),
            "long_runs": ("Runs it would split", "Its runs longer than that interval."),
            "replies_median": ("Replies in those runs (median)", "How long those runs were. The middle value."),
            "splits": ("Splits", "How many times those runs would start fresh."),
            "dropped_median": (
                "Context each split drops (median)",
                "Context a fresh start would leave behind at each split. The middle value.",
            ),
            "long_run_usd": ("Cost of those runs", "What those runs cost, at list price."),
            "saving_usd": ("Most you could save", "Those runs split, less what splitting adds back, at list price."),
            "saving_pct": ("Share of its cost", "That saving as a share of all its runs' cost."),
            "agent_usd": ("Cost", "Cost of all its runs, at list price."),
        },
        lead_columns=["agent_type", "every_n", "long_runs", "replies_median", "saving_usd", "saving_pct"],
    ),
    "run_split_sweep": TableCopy(
        title="Each split interval",
        help=Help(
            shows="Every agent type's runs split at each interval tried, and the net saving.",
            read="Short intervals split often and pay the cost of starting fresh often. A net saving below zero "
            "costs more than it saves.",
            act="",
        ),
        columns={
            "interval": ("Split interval", "The interval tried, in replies."),
            "long_runs": ("Runs it would split", "Runs longer than that interval."),
            "splits": ("Splits", "How many times those runs would start fresh."),
            "net_usd": (
                "Net saving, every agent type",
                "What splitting at this interval saves less what it adds back, at list price. Below zero costs more.",
            ),
            "best_for": ("Best interval for (agent types)", "Agent types for which this interval saves most."),
        },
    ),
    # -- your hooks -----------------------------------------------------------------
    "hooks_summary": TableCopy(
        title="Your hooks at a glance",
        help=Help(
            shows="Every hook of yours seen in this window: how often they failed, what they blocked and the "
            "context they added.",
            read="Claude Code records a tool hook's run only when it fails, blocks or adds context. Clean runs "
            "aren't counted.",
            act="",
        ),
        columns={
            "scope": ("Scope", "Which hooks this row covers."),
            "hooks": ("Hooks seen", "Hooks of yours that left a record in this window."),
            "failing_hooks": ("Hooks that failed", "Hooks with at least one failed run."),
            "failed": ("Failed runs", "Runs that ended in an error, so the hook didn't do its job."),
            "failed_wait_secs": ("Time waited on failed runs", "How long those failed runs took, added up."),
            "blocks": ("Calls blocked", "Tool calls your hooks stopped before they ran."),
            "resent": ("Sent again unchanged", "Blocked calls Claude then sent again with the same input."),
            "block_usd": ("Cost of blocks", "The replies that read those blocks, at list price."),
            "context_tokens": ("Context added", "Context your hooks added in front of Claude, in tokens."),
            "carry_usd": (
                "Cost of keeping that context",
                "That context read again on every later reply until a summary, at list price.",
            ),
        },
        value_labels={"your hooks": "Your hooks"},
        lead_columns=["failed", "failing_hooks", "carry_usd", "block_usd"],
    ),
    "hooks_by_script": TableCopy(
        title="Each hook",
        help=Help(
            shows="One row per hook, by its script's name, or the start of its command when it names no script.",
            read="Runs seen working undercounts tool hooks: a clean run that lets the call through leaves no record.",
            act="A hook that failed in many sessions isn't guarding anything. Check its command first.",
        ),
        columns={
            "hook": ("Hook", "The hook's script, or the start of its command."),
            "events": ("Runs on", "The hook events it ran on, as named in settings.json."),
            "failed": ("Failed runs", "Runs that ended in an error."),
            "cause": ("Why it failed", "The most common reason, read from the error. Relative path: the script path is relative."),
            "failed_sessions": ("Sessions it failed in", "Sessions with at least one failed run."),
            "last_failed": ("Last failed", "The day of its most recent failed run."),
            "worked": ("Runs seen working", "Recorded runs that worked, including the calls and stops it blocked."),
            "blocks": ("Calls blocked", "Tool calls it stopped before they ran."),
            "resent": ("Sent again unchanged", "Blocked calls Claude then sent again with the same input."),
            "block_usd": ("Cost of blocks", "The replies that read its blocks, each block taking its share, at list price."),
            "contexts": ("Times it added context", "How often it put a message in front of Claude."),
            "context_tokens": ("Context added", "The size of those messages, in tokens."),
            "carry_usd": (
                "Cost of keeping its context",
                "Those messages read again on every later reply until a summary, at list price.",
            ),
            "wait_secs": ("Time waited", "How long its recorded runs took, added up."),
        },
        value_labels={
            "script not found": "Script not found",
            "script not found (relative path)": "Script not found (relative path)",
            "script not found (%VAR% not expanded)": "Script not found (%VAR% not expanded)",
            "timed out": "Timed out",
            "error": "Error",
        },
        lead_columns=["hook", "failed", "cause", "blocks", "resent", "context_tokens", "carry_usd"],
    ),
    # -- what tool search saves ------------------------------------------------------
    "tool_search_summary": TableCopy(
        title="What tool search saves",
        help=Help(
            shows="Every reply sent while tools were deferred, how much that kept out of each request, and the net "
            "saving.",
            read="Net saving is the saving less the name list and the replies that only searched for a tool.",
            act="",
        ),
        columns={
            "scope": ("Scope", "Which replies this row covers."),
            "replies": ("Replies with tools deferred", "Replies requested while some tools were listed by name only."),
            "most_deferred": (
                "Most tools deferred in one reply",
                "The most tools listed by name only, without their definitions, in one request.",
            ),
            "most_deferred_mcp": ("Of them MCP tools", "How many of those came from MCP servers."),
            "definitions_measured": (
                "Definitions measured",
                "Tools whose full definition was loaded in this window, so its size is known.",
            ),
            "definition_tokens": ("Average definition", "The average size of those definitions, in tokens."),
            "kept_per_reply": ("Kept out of each reply", "Tokens of definitions left out of each request, on average."),
            "gross_usd": (
                "Saved by keeping them out",
                "What those tokens would have cost in every request, at list price.",
            ),
            "list_usd": ("Cost of the name list", "The list of tool names sent in their place, at list price."),
            "search_replies": ("Replies that only searched", "Replies whose only tool call loaded tool definitions."),
            "search_usd": (
                "Cost of those replies",
                "Those replies in full, at list price. Without tool search they wouldn't have happened.",
            ),
            "net_usd": ("Net saving", "The saving less the name list and the search replies, at list price."),
        },
        value_labels={"all replies": "All replies"},
        lead_columns=["net_usd", "kept_per_reply", "most_deferred", "search_replies"],
    ),
    "cost_record_summary": TableCopy(
        title="Against Claude Code's own record",
        help=Help(
            shows="Every session with Claude Code's own cost record, added up, and what explains the difference.",
            read="ClaudeGlass counts only the replies up to when Claude Code last wrote its total.",
            act="",
        ),
        columns={
            "scope": ("Scope", "Which sessions the row covers."),
            "sessions": ("Sessions", "Sessions with Claude Code's own cost record."),
            "cc_usd": ("Claude Code's own cost", "What Claude Code recorded these sessions cost."),
            "local_usd": ("ClaudeGlass, same span", "What ClaudeGlass prices the same replies at."),
            "difference_pct": ("Difference", "ClaudeGlass's cost against Claude Code's, as a % of Claude Code's."),
            "stopped_usd": (
                "Stopped replies",
                "Replies stopped mid-stream. They used tokens, so ClaudeGlass counts them; Claude Code doesn't.",
            ),
            "unlogged_usd": (
                "Requests no log shows",
                "Small requests Claude Code counts but no log records, such as naming the session.",
            ),
            "estimated_usd": (
                "Estimated summaries",
                "The request that writes each conversation summary. Claude Code bills it; ClaudeGlass estimates it.",
            ),
            "unexplained_pct": (
                "Left unexplained",
                "The difference once stopped replies and unlogged requests are taken out, as a % of Claude Code's.",
            ),
            "worst_unexplained_pct": ("Most in one session", "The largest share left unexplained in one session."),
        },
        value_labels={"all": "All sessions with a record"},
        lead_columns=["cc_usd", "local_usd", "difference_pct", "unexplained_pct"],
    ),
    "cost_record_sessions": TableCopy(
        title="By session",
        help=Help(
            shows="Each session with Claude Code's own cost record, newest first.",
            read="\"Recorded\" is when Claude Code last wrote its total. Later replies aren't compared.",
            act="",
        ),
        columns={
            "session_id": ("Session", "The session."),
            "as_of": ("Recorded", "When Claude Code last wrote the session's total."),
            "cc_usd": ("Claude Code's own cost", "What Claude Code recorded the session cost."),
            "local_usd": ("ClaudeGlass, same span", "What ClaudeGlass prices the same replies at."),
            "difference_pct": ("Difference", "ClaudeGlass's cost against Claude Code's, as a % of Claude Code's."),
            "unexplained_pct": (
                "Left unexplained",
                "The difference once stopped replies and unlogged requests are taken out.",
            ),
        },
    ),
    "tool_search_by_server": TableCopy(
        title="By MCP server",
        help=Help(
            shows="Each MCP server whose tools were deferred, and Claude Code's own tools, with what keeping their "
            "definitions out saved.",
            read="A server with no loaded tool is sized at the average of every server. The name list and the "
            "searches aren't split by server.",
            act="",
        ),
        columns={
            "server": ("MCP server", "The server the tools come from, or Claude Code's own tools."),
            "most_deferred": ("Most tools deferred", "The most of its tools listed by name only in one request."),
            "measured": ("Definitions measured", "Its tools whose full definition was loaded in this window."),
            "definition_tokens": ("Average definition", "The size each of its deferred tools was counted at, in tokens."),
            "sized_from": ("Sized from", "Whether that size comes from its own loaded tools or from every server's."),
            "replies": ("Replies", "Replies requested while some of its tools were deferred."),
            "kept_per_reply": (
                "Kept out of each reply",
                "Tokens of its definitions left out of each of those requests, on average.",
            ),
            "saving_usd": ("Saved by keeping them out", "What those tokens would have cost, at list price."),
        },
        value_labels={
            "built-in": "Claude Code's own tools",
            "its own tools": "Its own tools",
            "all servers": "All servers",
        },
        lead_columns=["server", "most_deferred", "definition_tokens", "kept_per_reply", "saving_usd"],
    ),
    # -- savings: wasted replies --------------------------------------------------
    "waste_summary": TableCopy(
        title="Wasted replies at a glance",
        help=Help(
            shows="All your replies and their cost, and how much went on replies that produced nothing.",
            read="Shares are against everything in the window, not only the wasted replies.",
            act="Above 10% of cost is worth acting on. See \"Why replies were wasted\" for what to change.",
        ),
        columns={
            "metric": ("", "Which sessions this row covers."),
            "total_priced_turns": ("All replies", "Every model reply in the window, including the ones left out."),
            "total_priced_cost_usd": ("All cost", "Cost of every reply, at list price."),
            "wasted_turns": ("Wasted replies", "Replies whose output you never used."),
            "wasted_turns_share_pct": ("Share of replies", "Wasted replies as a share of all replies."),
            "wasted_cost_usd": (
                "Wasted cost",
                "Full cost of the wasted replies, at list price. The most you could recover.",
            ),
            "wasted_cost_share_pct": ("Share of cost", "Wasted cost as a share of all cost."),
            "wasted_tokens": (
                "Wasted tokens",
                "Input, cache write, cache read and output tokens of the wasted replies.",
            ),
            "limit_pause_excluded_turns": (
                "Left out: after a usage limit",
                "Replies right after a usage-limit pause. {{page:spend/usage}} covers these.",
            ),
            "api_error_retry_turns": (
                "After an API error",
                "Replies that followed an API error and automatic retry. Counted only, not in wasted cost.",
            ),
            "failed_command_turns": (
                "Not wasted: a command failed",
                "Replies whose only failed tool calls were commands that ran and reported failure, such as a "
                "failing test or build. Claude used that output, so they aren't counted.",
            ),
        },
        value_labels={"all": "All sessions"},
        lead_columns=["wasted_cost_usd", "wasted_cost_share_pct", "wasted_turns", "wasted_turns_share_pct"],
    ),
    "waste_by_cause": TableCopy(
        title="Why replies were wasted",
        help=Help(
            shows="Wasted replies by cause, with what each cost and what to change.",
            read="Each wasted reply has one cause, so the costed causes add up to the totals above. API "
            "errors are counted only, never costed. A failing test or build isn't a failed tool call here: "
            "Claude used that output.",
            act="Start with the cause that cost the most and follow its suggestion.",
        ),
        columns={
            "cause": ("", "What made the reply useless."),
            "turns": ("Replies", "Wasted replies with this cause."),
            "share_of_turns_pct": ("Share of all replies", "Against every reply in the window."),
            "cost_usd": ("Wasted cost", "Full cost of those replies, at list price."),
            "share_of_cost_pct": ("Share of all cost", "Against all cost in the window."),
            "tokens": ("Tokens", "All tokens of those replies."),
            "lever": ("What to change", "The change that stops this kind of waste."),
        },
        value_labels={
            "tool-error": "A tool call couldn't run as written",
            "blocked": "A hook or guard blocked a tool call",
            "interrupt": "You stopped the reply",
            "tool-denial": "You denied a tool call",
            "max-turns": "Subagent stopped before it reported",
            "api-error-retry": "API error, retried automatically",
            # The builder's lever text (waste.LEVERS), reworded for display.
            "Give exact paths and names in briefs, and have Claude check a path exists or read a file before "
            "it edits or runs against it, so a tool call works the first time.": (
                "Give exact paths and names in your task prompts. Have Claude check a path or read a file before it "
                "edits or runs a command on it."
            ),
            "Put the rule a hook enforces into the instructions of the agent that keeps hitting it (its prompt, "
            "or CLAUDE.md for the main session), so Claude doesn't try the blocked action first.": (
                "Put the rule the hook enforces into the instructions of the agent that keeps hitting it: its "
                "prompt, or CLAUDE.md for the main session."
            ),
            "Batch instructions and plan the whole step before running it, so there is less to interrupt "
            "mid-turn.": "Plan the whole step and give your instructions up front, so there is less to stop midway.",
            "Add the repeatedly-denied tool/command to the permissions allowlist so it stops being denied "
            "mid-run.": (
                "If you keep denying the same tool or command, allow it in your permissions, or tell Claude "
                "up front not to use it."
            ),
            "Raise the subagent's maxTurns budget or narrow its brief so it finishes -- and reports back -- "
            "inside the turns it's given.": (
                "Narrow the subagent's task prompt so it finishes sooner, or raise its turn limit. A stopped "
                "run returns no report, so all its replies count."
            ),
            "None -- retried automatically by the harness; investigate only if persistently frequent.": (
                "Nothing to do. Claude Code retried these for you. Look into it only if it keeps happening."
            ),
        },
    ),
    "waste_by_agent_type": TableCopy(
        title="Wasted replies by agent type",
        help=Help(
            shows="Wasted replies and their cost for the main session and each subagent type.",
            read="Shares are against all replies and all cost in the window, not per agent type.",
            act="A subagent type with a high wasted cost may need a clearer task prompt, or permission for "
            "the tools it keeps failing on.",
        ),
        columns={
            "agent_type": ("", "The main session or the subagent type."),
            "turns": ("Wasted replies", "Replies whose output you never used."),
            "share_of_turns_pct": ("Share of all replies", "Against every reply in the window."),
            "cost_usd": ("Wasted cost", "Full cost of those replies, at list price."),
            "share_of_cost_pct": ("Share of all cost", "Against all cost in the window."),
            "tokens": ("Wasted tokens", "All tokens of those replies."),
        },
        value_labels={"top-level": "Main session", "unknown": "Subagent (type not recorded)"},
    ),
    "waste_top_sessions": TableCopy(
        title="Sessions with the most wasted cost",
        help=Help(
            shows="The 20 sessions that spent the most on wasted replies, their subagents included.",
            read="Session ids are scrambled for privacy. Causes lists each cause and its count, most "
            "frequent first.",
            act="In a session with many failed tool calls, look for a path or command that kept going wrong.",
        ),
        columns={
            "session_hash": ("Session", "A scrambled session id, so the real id is never stored."),
            "turns": ("Wasted replies", "Replies in this session whose output you never used."),
            "cost_usd": ("Wasted cost", "Full cost of those replies, at list price."),
            "share_of_cost_pct": ("Share of all cost", "Against all cost in the window."),
            "cause_mix": ("Causes", "Each cause and its count, most frequent first."),
        },
    ),
    # -- config ------------------------------------------------------------
    "effective-config": TableCopy(
        title="Settings in effect",
        help=Help(
            shows="One row per setting per project: its current value and the settings file it came from.",
            read="Only settings that affect cost or context are recorded. When a setting is in more than one "
            "file, the highest-priority file wins: managed policy, then project local, then project shared, "
            "then your user settings.",
            act="To change a setting, edit the file named in \"Set in\". Only your administrator can change "
            "a managed-policy setting.",
        ),
        columns={
            "project": ("", "The project, shown as a short code so no folder path is stored."),
            "key": ("Setting", "The setting name, as written in settings.json."),
            "value": ("", "Its current value. Values that could hold private details are summarised."),
            "provenance": ("Set in", "The settings file that supplied the value."),
        },
        value_labels=_SETTINGS_FILES,
    ),
    "config-layers": TableCopy(
        title="Settings files and project content",
        help=Help(
            shows="For each project: which of the four settings files exist, and how many agents, skills, "
            "rules, commands and MCP servers the project loads.",
            read="The counts to the right describe the whole project, so they repeat on every settings-file "
            "row. They don't belong to that one file.",
            act="",
        ),
        columns={
            "project": ("", "The project, shown as a short code so no folder path is stored."),
            "layer": ("Settings file", "The settings file this row checks."),
            "present": ("Exists", "Whether that settings file exists."),
            "agents": ("", "Custom agents available in this project, from your user and project agent folders."),
            "skills": ("", "Skills in your user and project skill folders. Plugin skills are not counted."),
            "rules": ("", "Rule files in the project's .claude/rules folder."),
            "claude_md_bytes": (
                "CLAUDE.md size (bytes)",
                "Your global, project, local and subfolder CLAUDE.md files together, in bytes.",
            ),
            "commands": ("", "Custom slash commands in the project's .claude/commands folder."),
            "mcp_servers": ("", "MCP servers configured for this project, including your global ones."),
        },
        value_labels=_SETTINGS_FILES,
        lead_columns=["project", "layer", "present", "agents", "skills", "claude_md_bytes", "mcp_servers"],
    ),
    "config-groups": TableCopy(
        title="Projects with the same settings",
        help=Help(
            shows="Projects grouped by identical settings, using each project's latest snapshot.",
            read="Projects in one group run with the same settings. Cost differences between them come "
            "from the work, not the setup.",
            act="",
        ),
        columns={
            "config_hash": ("Settings group", "A short code for one exact set of settings."),
            "project_count": ("Projects", "How many projects share these settings."),
            "projects": ("Projects in group", "The projects, shown as short codes."),
            "sessions": ("", "Sessions matched to these projects' settings snapshots."),
        },
        value_labels={"(unknown project)": "Unknown project (older snapshot)"},
    ),
    "config-drift": TableCopy(
        title="Settings that did not take effect",
        help=Help(
            shows="Sessions where the model or effort level Claude actually used differs from your settings.",
            read="Only the model and effort level are checked. A mismatch usually means something overrode "
            "the setting, such as an environment variable, a command-line flag or a switch during the session.",
            act="If sessions ran on a pricier model or a higher effort level than you set, check your shell "
            "profile and launch command for an override.",
        ),
        columns={
            "session_id": ("Session", "The session's id."),
            "key": ("Setting", "The setting that was checked."),
            "snapshot_value": ("In your settings", "The value your settings files gave when the session started."),
            "observed_value": ("Actually used", "The model or effort level the main session mostly used."),
        },
    ),
    "env-levers": TableCopy(
        title="Environment variable and attribution levers",
        help=Help(
            shows="Whether specific environment variables and settings are set, and their value when that's "
            "safe to show.",
            read="Each row is one lever this report can suggest a change for, such as turning prompt caching "
            "back on. \"Present\" means it's set somewhere in your settings, not necessarily where you'd "
            "expect.",
            act="See the matching recommendation for what to change and how to undo it.",
        ),
        columns={
            "name": ("Name", "The environment variable or setting name."),
            "present": ("Set", "Whether it's set at all."),
            "value": ("Value", "Its value, when that's safe to show."),
        },
    ),
    # Run-time named: one table per changed setting, "config-diff-<setting>";
    # found by prefix (see _table_copy_for).
    "config-diff-": TableCopy(
        title="",
        help=Help(
            shows="Sessions grouped by the value this setting had when each one started, with the cost "
            "and behaviour of each group.",
            read="Compare cost per session and cache rebuild share between rows. The groups are observed, "
            "not controlled: projects, tasks and other settings also differ between them.",
            act="Treat a difference as a lead, not proof. The note below lists other settings that changed "
            "at the same time.",
        ),
        columns={
            "value": ("Setting value", "The value when the session started. \"Not set\" means the setting was absent."),
            "sessions": ("", "Sessions that started with this value. Sessions before the first snapshot are left out."),
            "turns": ("Replies", "Model replies in those sessions, main session and subagents together."),
            "cost": ("", "Cost of those sessions at list prices, main session and subagents together."),
            "cost_per_session": ("Cost per session", "Average cost of one session at list prices."),
            "recache_share": ("Cache rebuild share", "Share of cache writes that were cache rebuilds, in %."),
            "compactions_per_session": (
                "Summaries per session",
                "Average conversation summaries (compactions) per session.",
            ),
            "median_span": ("Typical session length", "The middle session's length, from first to last message."),
        },
        value_labels={"(unset)": "Not set", "{}": "Empty", "[]": "Empty list"},
        lead_columns=[
            "value", "sessions", "cost_per_session", "cost", "recache_share", "compactions_per_session", "median_span",
        ],
    ),
    # -- context budget ------------------------------------------------------
    "context_budget_baseline": TableCopy(
        title="What the main session starts with",
        help=Help(
            shows="One row per project, plus one for all projects: the main session's startup context and "
            "an estimate of what it is made of, in tokens.",
            read="Startup context is measured: it is the cache write on the first reply. The parts to its "
            "right are estimates. \"System prompt and tools\" is whatever the other parts don't explain, so "
            "it also holds anything that could not be estimated.",
            act="If one project's startup context is much larger than the rest, run /context in that "
            "project to see the exact breakdown. Then trim the biggest part.",
        ),
        columns={
            "project": ("", "The project folder. \"All projects\" combines every session."),
            "sessions": ("", "Main sessions in this project."),
            "mean_baseline": (
                "Average startup context",
                "Tokens written to the cache on the main session's first reply, averaged. Measured.",
            ),
            "median_baseline": ("Typical startup context", "The middle value, less affected by a few very large sessions."),
            "human_prompt_est": ("Your first message (est.)", "Your first message, estimated from its length."),
            "skills_listing_est": ("Skills list (est.)", "The list of skills Claude Code sent, estimated from its length."),
            "memory_files_est": (
                "CLAUDE.md and rules (est.)",
                "Your CLAUDE.md files and project rules, estimated from file size. Blank without a settings snapshot.",
            ),
            "custom_agents_est": (
                "Agent list (est.)",
                "Your custom agents, at about 60 tokens each. Blank without a settings snapshot.",
            ),
            "mcp_tools_est": ("MCP tools", "Whether MCP servers are configured. Their size can't be measured here."),
            "system_prompt_and_tools_est": (
                "System prompt and tools (rest)",
                "Average startup context minus every estimate to its left. Blank when the estimates add up "
                "to more than the measurement.",
            ),
        },
        value_labels={"all": "All projects", "present, size unknown": "Yes, size unknown"},
        lead_columns=[
            "project", "sessions", "median_baseline", "memory_files_est", "skills_listing_est", "custom_agents_est",
            "system_prompt_and_tools_est",
        ],
    ),
    "context_budget_autocompact": TableCopy(
        title="When conversations get summarised",
        help=Help(
            shows="For each project: your auto-compact setting, the model's context window, and the context "
            "size at which Claude Code actually summarised the main session.",
            read="\"Summarised at\" is the typical context size right before an automatic summary. \"Room left\" is "
            "the context window minus that. The window is assumed unless your status line logged it.",
            act="If \"Differs from setting\" is yes, summaries start more than 10% away from your auto-compact window "
            "setting. Check whether another settings file or an environment variable overrides it.",
        ),
        columns={
            "project": ("", "The project folder."),
            "configured_window": ("Auto-compact setting", "Your auto-compact window setting, in tokens. Blank if not "
            "set."),
            "context_window_size": ("Context window", "The model's context window, in tokens."),
            "context_window_source": (
                "Window from",
                "Measured by your status line, or assumed: 1,000,000 tokens for a model set with [1m], "
                "otherwise 200,000.",
            ),
            "observed_threshold": (
                "Summarised at",
                "Typical context size right before an automatic summary, in tokens. Blank if none happened.",
            ),
            "implied_buffer": ("Room left", "Context window minus \"Summarised at\", in tokens."),
            "auto_compactions": ("Automatic summaries", "Automatic summaries in this project's main sessions."),
            "drift": (
                "Differs from setting",
                "Yes when \"Summarised at\" is more than 10% away from your auto-compact setting. "
                "Blank when either is missing.",
            ),
        },
        value_labels={"statusline": "Status line", "assumed": "Assumed"},
        lead_columns=[
            "project", "configured_window", "observed_threshold", "context_window_size", "implied_buffer",
            "auto_compactions", "drift",
        ],
    ),
    "context_budget_statusline": TableCopy(
        title="Context used, as reported by Claude Code",
        help=Help(
            shows="The last context size your status line logged for each session. These are Claude Code's "
            "own numbers, not estimates.",
            read="\"Used\" is how full the context window was at the last status line update. The table stays "
            "empty until you install the status line logger.",
            act="If sessions often end near full, start a new session for each new task instead of carrying "
            "old context.",
        ),
        columns={
            "session": ("", "The session's id."),
            "used_tokens": ("Last context size", "Tokens in the context window at the last status line update."),
            "context_window_size": ("Context window", "The model's context window, in tokens."),
            "used_percentage": ("Used", "How full the context window was, in %."),
        },
    ),
}

#: Every table with a task column. Each gets metrics capture's plain
#: names for the task words (``capture_catalogue.TASK_LABELS``, tested),
#: so "bugfix" reads "Bug fix". A table's own label for a word wins.
TASK_TABLES: tuple[str, ...] = (
    "habits_by_task",
    "habits_brief_templates",
    "habits_setups",
    "habits_agents_by_task",
    "habits_outcomes",
    "compaction_sim_by_task",
)

for _name in TASK_TABLES:
    for _word, _label in TASK_LABELS.items():
        TABLE_COPY[_name].value_labels.setdefault(_word, _label)


# -- data quality ----------------------------------------------------------

#: ``Diagnostics`` field -> (label, what it means). Every field is listed;
#: ``tests/test_help_coverage.py`` checks.
DIAGNOSTIC_LABELS: dict[str, tuple[str, str]] = {
    "lines": ("Lines read", "Lines read from every conversation log in the window."),
    "unparsable_lines": ("Lines that could not be read", "Lines that were not valid JSON. A few are normal while a session is still writing."),
    "truncated_final_line": ("Last line cut off", "A log's last line was incomplete, usually because the session was still running."),
    "assistant_lines": ("Claude reply lines", "Lines holding part of one of Claude's replies."),
    "distinct_turns": ("Replies", "Separate replies from Claude, after joining the lines of each one."),
    "synthetic_turns": ("Placeholder replies", "Replies Claude Code wrote itself, such as an error notice. Not billed."),
    "turns_missing_usage": ("Replies without token counts", "Replies with no token counts. They are left out of costs."),
    "ttl_sum_mismatch": ("Cache write split does not add up", "Replies whose five-minute and one-hour cache writes don't sum to the total. Costs use the total."),
    "late_duplicate_ids": ("Repeated reply ids", "The same reply seen again later in a log. Counted once."),
    "ignored_line_types": ("Line types skipped", "Kinds of line this tool does not use, with counts."),
    "oversized_lines": ("Lines too large to read", "Lines over the size limit, skipped without reading."),
    "trailing_events": ("Events after the last reply", "Events logged after a session's last reply, so attached to none."),
    "replayed_lines": ("Lines copied from an earlier session", "Lines a resumed session repeated. Counted once."),
    "copied_lines": ("Lines copied from another session", "Lines another session's log also holds, as after /clear in a web or mobile session. Counted once, in their own session."),
    "compaction_calls": ("Summaries estimated", "Conversation summaries whose own request was estimated and counted in spend. Claude Code bills that request but does not log it."),
    "compaction_calls_unsized": ("Summaries left out", "Conversation summaries with no reply before them or no summary size, so their own request could not be estimated and is not in spend."),
    "timestamp_parse_failures": ("Unreadable timestamps", "Replies whose time could not be read. They are left out of time-based tables."),
    "agent_settings": ("Agent settings seen", "Agent settings recorded in the logs, with counts."),
    "modes": ("Permission modes seen", "Permission modes recorded in the logs, with counts."),
    "attachment_catch_all": ("Unrecognised note types", "Kinds of Claude Code note this tool does not recognise yet, with counts. Worth reporting if large."),
    "pre_split_turns": ("Replies from older Claude Code", "Replies logged before Claude Code split cache writes by lifetime."),
    "limit_hits": ("Usage-limit stops", "Times a session stopped at a usage limit."),
    "limit_resumes": ("Resumes after a limit", "Times a session carried on after a usage-limit stop."),
    "agents_terminated": ("Subagents stopped early", "Subagents Claude Code ended before they finished, for any reason."),
    "pricing_closest_match_turns": ("Replies priced by closest match", "Replies costed at another, similar model's rate because this one has no price list entry of its own. See Usage's \"Priced by closest match\" table."),
    "pricing_fast_priced_as_standard_turns": ("Fast replies priced at standard rate", "Replies flagged fast mode but costed at the standard rate because this model has no fast-mode price on file."),
}


#: Parser-signals addition (SURV-6/7, see model.py's module docstring):
#: ``ReportModel.parser_notes`` key -> (label, meaning), the same shape
#: as ``DIAGNOSTIC_LABELS`` but deliberately a separate dict -- this
#: phase was told not to edit ``DIAGNOSTIC_LABELS`` (and ``parser_notes``
#: isn't a ``Diagnostics`` field to begin with).
_PARSER_NOTE_LABELS: dict[str, tuple[str, str]] = {
    "unknown_line_types": (
        "Unrecognised line types",
        "Kinds of log line that no rule in this tool recognises, with counts. \"Line types skipped\" above also "
        "includes kinds this tool knows about and ignores on purpose.",
    ),
    "unsized_blocks": (
        "Unsized image/document content",
        "Images or document pages this tool could not estimate a token count for, by kind, with counts. An oversized "
        "image or a PDF page has no fixed formula for its cost. They are left out of context-size figures.",
    ),
}


def diagnostics_table(diagnostics: Diagnostics, hook=None, statusline=None, parser_notes: dict | None = None) -> Table:
    """The parse-quality counters as a plain-English table (the Data
    quality page, ``GET /api/diagnostics``). Rows keep the raw field name
    as their key, shown through ``value_labels``. ``hook``, a
    ``hook_health.HookHealth``, adds a first row saying whether the
    config snapshot hook is running; ``statusline``, a
    ``hook_health.statusline_check`` result, adds one for the statusline.
    ``parser_notes`` (``ReportModel.parser_notes``) adds one row per key
    it carries, labelled via ``_PARSER_NOTE_LABELS`` -- a side channel
    for counters that don't fit the ``Diagnostics`` dataclass, see that
    module's docstring."""
    rows = []
    if hook is not None:
        rows.append(["snapshot_hook", "working" if hook.ok else "needs attention", hook.summary()])
    if statusline is not None:
        working, sentence = statusline
        rows.append(["statusline", "working" if working else "needs attention", sentence])
    for field_def in dataclasses.fields(Diagnostics):
        value = getattr(diagnostics, field_def.name)
        if isinstance(value, dict):
            value = ", ".join(f"{k}: {v:,}" if isinstance(v, int) else f"{k}: {v}" for k, v in sorted(value.items())) if value else "none"
        elif isinstance(value, bool):
            value = "yes" if value else "no"
        _, meaning = DIAGNOSTIC_LABELS.get(field_def.name, ("", ""))
        rows.append([field_def.name, value, meaning])
    for note_key, label_pair in _PARSER_NOTE_LABELS.items():
        counts = (parser_notes or {}).get(note_key) or {}
        value = ", ".join(f"{k}: {v:,}" for k, v in sorted(counts.items())) if counts else "none"
        rows.append([note_key, value, label_pair[1]])
    return Table(
        name="data_quality",
        title="What could be read",
        columns=[
            Column(key="check", label="Check", kind="str", help="What was counted."),
            Column(key="value", label="Count", kind="str", help="The count for the window."),
            Column(key="meaning", label="What it means", kind="str", help="What the count tells you."),
        ],
        rows=rows,
        help=Help(
            shows="Counters from reading your conversation logs.",
            read="Most should be zero or small. Large skipped or unreadable counts mean some usage is missing from the rest of the report.",
            act="If unrecognised note types or unreadable lines are large, your Claude Code version may be newer than this tool.",
        ),
        value_labels={"snapshot_hook": "Config snapshot hook", "statusline": "Statusline (usage limits)"}
        | {key: label for key, (label, _) in DIAGNOSTIC_LABELS.items()}
        | {key: label for key, (label, _) in _PARSER_NOTE_LABELS.items()},
    )


def _table_copy_for(table_name: str) -> TableCopy | None:
    """A table's copy by exact name, else by a run-time name's prefix
    (one ``config-diff-<setting>`` table per changed setting)."""
    copy = TABLE_COPY.get(table_name)
    if copy is None and table_name.startswith("config-diff-"):
        copy = TABLE_COPY.get("config-diff-")
    return copy


def _apply_table_copy(table: Table, copy: TableCopy | None, billing_mode: str) -> None:
    table.dashboard = placement_for(table.name)
    if copy is not None:
        if copy.title:
            table.title = copy.title
        if copy.help is not None:
            table.help = copy.help
        if copy.value_labels:
            table.value_labels = dict(copy.value_labels)
        if copy.row_groups:
            table.row_groups = dict(copy.row_groups)
        if copy.row_kinds:
            table.row_kinds = dict(copy.row_kinds)
        if copy.lead_columns:
            table.lead_columns = list(copy.lead_columns)
    for column in table.columns:
        label, help_text = ("", "")
        if copy is not None:
            label, help_text = copy.columns.get(column.key, ("", ""))
        if label:
            column.label = label
        column.help = help_text or column.help or COMMON_COLUMN_HELP.get(column.key, "")
        if column.kind == "money" and billing_mode == "subscription" and "list price" not in column.help:
            # A subscription isn't billed per token: say what the figure is.
            column.help = (column.help + " " if column.help else "") + "Shown at list price; your plan is not billed this way."


def annotate_section(section: Section, billing_mode: str = "api") -> None:
    """Apply :data:`SECTION_COPY`/:data:`TABLE_COPY`/:data:`PLACEMENT` to
    one section, in place."""
    copy = SECTION_COPY.get(section.key)
    if copy is not None:
        if copy.title:
            section.title = copy.title
        if copy.intro:
            section.intro = copy.intro
        if copy.help is not None:
            section.help = copy.help
    for table in section.tables:
        _apply_table_copy(table, _table_copy_for(table.name), billing_mode)


def annotate(model: ReportModel) -> ReportModel:
    """Apply display copy to every section of ``model``, in place, and
    return it. Never changes a table name, a column key or a row value."""
    billing_mode = getattr(model.meta, "billing_mode", "api") or "api"
    for section in model.sections:
        annotate_section(section, billing_mode)
    return model


__all__ = [
    "COMMON_COLUMN_HELP",
    "DIAGNOSTIC_LABELS",
    "PLACEMENT",
    "PLACEMENT_PREFIXES",
    "SECTION_COPY",
    "TABLE_COPY",
    "SectionCopy",
    "TableCopy",
    "annotate",
    "annotate_section",
    "diagnostics_table",
    "placement_for",
]

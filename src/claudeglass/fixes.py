"""What a recommendation asks you to change, said plainly, plus two ways
to make the change: a command and a prompt for Claude.

The dashboard never edits your Claude Code config itself. For each
:class:`~claudeglass.model.SettingChange` on a recommendation,
:func:`build_fix` returns:

- ``explainer``: six (heading, text) pairs -- what the setting controls,
  now and after, where it is set and who it affects, the expected
  effect, the trade-off, and how to undo it;
- ``command``: a ``claudeglass apply --set ... --dry-run`` line,
  or ``None`` when the right value needs your judgement;
- ``command_warning``: what the command does not do (see
  :data:`_PREPARE`), or ``""``;
- ``prompt``: a self-contained request to paste into Claude Code. It
  names the file, the key and the value, says why, warns that Claude
  Code will ask permission (``.claude`` is a protected directory) and
  asks Claude to show the change before saving it.

A recommendation with no setting change (workflow advice) gets a single
fix instead: a where/trade-off/undo explainer (three pairs, not six --
see :data:`_WORKFLOW_EXPLAINER`) when this module has one for its id,
plus a prompt when one is useful (see :data:`_WORKFLOW_PROMPTS`). An id
in neither dict gets no fix at all. When ``rec.why`` is set, its
explainer's first row is "Why it's suggested".

Every fix dict also carries an optional ``note`` key the render layer
reads through :func:`fix_note`: absent for the default -- print
:data:`RESTART_NOTE` below a prompt or command -- ``"scope"`` for a
"from now on" prompt that ends in :data:`PROMPT_SCOPE`, where
:data:`SCOPE_NOTE` is printed instead, and ``"none"`` where neither
applies (an id whose prompt or change was never something a restart
would pick back up).
"""

from __future__ import annotations

import json
import shlex

from .model import Recommendation, SettingChange

#: Shown after every fix and profile change, and printed after an apply:
#: Claude Code reads settings, agent files and CLAUDE.md when it starts,
#: so the session that made the change keeps the old ones.
RESTART_NOTE = (
    "Restart Claude Code to pick up the change. It reads settings and agent files when it starts, so a "
    "session that is already open keeps the old ones (claude --continue picks your last conversation back up)."
)

#: The last line of every prompt for Claude that changes Claude Code's
#: files, so the reminder comes at the moment the change is saved.
PROMPT_RESTART = "Once it's saved, remind me to restart Claude Code so it picks up the change."

#: Shown instead of :data:`RESTART_NOTE` after a "from now on" prompt
#: (:data:`PROMPT_SCOPE`), whose change isn't a file save Claude Code
#: reads at startup -- it's a rule Claude asks where to keep.
SCOPE_NOTE = (
    "Claude asks whether this is for this session, this project or all your projects. A line in CLAUDE.md "
    "lasts; new sessions read it when they start."
)

#: Appended to every "from now on" workflow prompt (one that asks Claude
#: to work differently going forward, rather than to edit a specific
#: setting): a pasted "from now on" rule isn't picked up by a restart --
#: unlike a setting, it only lasts as long as Claude is told to keep it,
#: so this asks Claude to check with you where it should live before
#: acting on it.
PROMPT_SCOPE = (
    "Before you start, use AskUserQuestion to ask me where this should apply: this session only, this "
    "project (one line in its CLAUDE.md), or all my projects (one line in ~/.claude/CLAUDE.md). For a "
    "CLAUDE.md, show me the line before saving. If I pick all my projects, Claude Code will ask my "
    "permission to edit ~/.claude/CLAUDE.md; that is expected."
)

#: Opens every workflow prompt (:data:`_WORKFLOW_PROMPTS`): the finding
#: this prompt is about, quoted once, so the instruction that follows
#: doesn't have to restate it (that used to be ``{title_lower}``, spliced
#: mid-sentence into a lead-in that said the same thing again).
_FINDING_OPEN = 'ClaudeGlass, which tracks my token use, flagged this: "{title}."'

#: key -> (what it controls, trade-off, extra caveat).
SETTING_TEXT: dict[str, tuple[str, str, str]] = {
    "omitClaudeMd": (
        "Whether Claude Code sends your CLAUDE.md files and auto memory to this agent when it starts.",
        "The agent no longer sees your project and personal rules. Only its own prompt (the body of its "
        "agent file) and the skills listed in its frontmatter reach it on every spawn, so the rules it needs "
        "must be moved there first, and the saving shrinks by their size.",
        "Needs Claude Code 2.1.271 or later; older versions ignore it.",
    ),
    "disallowedTools": (
        "Tools this agent may not call. They are removed from what it can use.",
        "The agent can't use the tools you list, even when a task would need them.",
        "",
    ),
    "mcpServers": (
        "The MCP servers this agent can use. Servers left out are not loaded for it.",
        "The agent can't call tools from servers you leave out.",
        "",
    ),
    "tools": (
        "The only tools this agent may call. Definitions of other tools are not sent to it.",
        "The agent can't use any tool missing from the list, for example to edit a file.",
        "",
    ),
    "model": (
        "Which Claude model does the work. Smaller models cost less per token.",
        "A smaller model may need more replies for the same task, or get some tasks wrong. Try it on a few "
        "tasks and compare the results before keeping it.",
        "",
    ),
    "autoCompactWindow": (
        "How large the conversation may grow, in tokens, before Claude Code replaces it with a summary. "
        "Every reply re-reads the whole conversation, so a smaller window means cheaper replies.",
        "A summary drops detail. After one, Claude may re-read files or lose track of earlier decisions.",
        "",
    ),
    "effortLevel": (
        "How hard Claude thinks before replying. Thinking is billed as output.",
        "Lower effort can miss things on hard problems. You can raise it for one task with /effort.",
        "",
    ),
    "promptCacheTtl": (
        "How long the main session's cache is kept between replies: 5 minutes or 1 hour.",
        "A 1-hour cache costs more to write, so it only pays off when you often pause for more than 5 minutes.",
        "Needs Claude Code 2.1.242 or later. Left unset, a Pro or Max plan gets 1 hour within plan usage and "
        "5 minutes once it draws on extra usage credits; a value set here applies either way.",
    ),
    "subagentPromptCacheTtl": (
        "How long every subagent's cache is kept between replies: 5 minutes or 1 hour. It also covers "
        "workflow agents, teammates and compaction.",
        "A 1-hour cache costs more to write, so it only pays off when subagents often wait more than 5 minutes.",
        "Needs Claude Code 2.1.242 or later. Claude Code uses it before any agent file's cacheTtl, and it "
        "still applies while a Pro or Max plan draws on extra usage credits.",
    ),
    "experimental.cacheTtl": (
        "How long this agent's cache is kept between replies: 5 minutes or 1 hour.",
        "A 1-hour cache costs more to write, so it only pays off when this agent often waits more than 5 minutes.",
        "Needs Claude Code 2.1.248 or later. A 1-hour lifetime is ignored while a Pro or Max plan is using "
        "extra usage credits.",
    ),
}

SETTING_TEXT.update(
    {
        "effort": (
            "How hard this agent thinks before replying. Thinking is billed as output.",
            "Lower effort can miss things on hard problems.",
            "",
        ),
        "maxTurns": (
            "The most replies this agent may take before it has to stop and report back.",
            "An agent that hits the limit stops mid-task and returns what it has.",
            "",
        ),
        "memory": (
            "Which persistent memory this agent reads and writes between runs.",
            "Memory adds to what the agent is sent on every run.",
            "",
        ),
        "skills": (
            "The skills loaded into this agent when it starts, in full.",
            "Every listed skill is sent on every spawn, whether the task needs it or not.",
            "",
        ),
        "outputStyle": (
            "The output style Claude uses for replies, such as concise or explanatory.",
            "A terser style gives less explanation.",
            "",
        ),
        "enabledPlugins": (
            "The plugins turned on for Claude Code.",
            "Each plugin can add skills, agents, hooks and MCP servers, which are sent on every session.",
            "",
        ),
        "disabledMcpjsonServers": (
            "MCP servers from the project's .mcp.json that are turned off.",
            "Claude can't call tools from servers you turn off.",
            "",
        ),
        "skillOverrides": (
            "How each skill is shown to Claude. \"name-only\" lists the skill by name without its "
            "description; \"user-invocable-only\" hides it from Claude but keeps it in your / menu; \"off\" "
            "hides it everywhere. Skills not named stay as they are.",
            "Claude uses a skill on its own only when its description tells Claude what it is for. With the "
            "description gone Claude may not reach for it; hidden, only you can start it, by typing /name.",
            "",
        ),
        "enabledMcpjsonServers": (
            "MCP servers from the project's .mcp.json that are turned on.",
            "Every enabled server's tool list is sent with each session.",
            "",
        ),
        "alwaysThinkingEnabled": (
            "Whether Claude always thinks before replying.",
            "Thinking is billed as output, so replies cost more. It has no effect on Opus 5.5 or the Fable "
            "models, which always think; a lower effort is what thinks less there.",
            "",
        ),
        "autoCompactEnabled": (
            "Whether Claude Code summarises the conversation automatically when it grows too large.",
            "With it off, a long session keeps growing until you run /compact or start a new one.",
            "",
        ),
        "cleanupPeriodDays": (
            "How many days Claude Code keeps conversation logs before deleting them.",
            "Logs older than this are gone, including from this tool's reports.",
            "",
        ),
        "includeCoAuthoredBy": (
            "Whether Claude adds a co-authored-by line to git commits and pull requests it creates.",
            "Deprecated: Claude Code still honours it, but only until the newer `attribution` setting is "
            "used, which offers more control (a custom commit trailer, PR text, and a session-link toggle).",
            "",
        ),
        # PROF-08/D5: fast mode is a documented per-model price premium (a
        # flat multiplier over standard rates), traded for a faster reply.
        "fastMode": (
            "Whether replies are billed at a model's fast-mode rate, a documented premium over its standard "
            "rate, for a faster reply.",
            "Turning it off saves money on every reply that would have run fast, but replies come back slower.",
            "Only a few models document a fast rate; on every other model this setting has no effect.",
        ),
    }
)

# COV-07/COV-11: the settings.json `env` block levers the five COV-09
# rules in recommend.py cite (``env-disable-prompt-caching``,
# ``env-tool-search``, ``env-max-output-tokens``) attach a real
# SettingChange to; keyed by the full "env.NAME" string, matching
# ``change.key`` for one of these (never the bare env var name alone).
SETTING_TEXT["env.DISABLE_PROMPT_CACHING"] = (
    "Whether Claude Code's prompt cache is used at all, for every model. A value of 1 turns caching off.",
    "Every request re-sends and re-processes the whole prefix instead of reading it from cache -- far more "
    "expensive on a multi-turn session, not less.",
    "Only a value of 1 actually disables caching; this report can only see that the name is set, not its value.",
)
SETTING_TEXT.update(
    {
        f"env.DISABLE_PROMPT_CACHING_{family}": (
            f"Whether Claude Code's prompt cache is used for {label} specifically. A value of 1 turns it off "
            "for that model only.",
            "Every request to that model re-sends and re-processes the whole prefix instead of reading it "
            "from cache -- far more expensive on a multi-turn session, not less.",
            "Only a value of 1 actually disables caching; this report can only see that the name is set, not "
            "its value.",
        )
        for family, label in (("SONNET", "Sonnet"), ("OPUS", "Opus"), ("HAIKU", "Haiku"), ("FABLE", "Fable"))
    }
)
SETTING_TEXT["env.ENABLE_TOOL_SEARCH"] = (
    "Whether Claude Code searches for the right MCP tool instead of sending every tool's full schema on "
    "every turn -- only matters when ANTHROPIC_BASE_URL points at a non-Anthropic proxy or gateway.",
    "If your proxy doesn't forward tool_reference blocks, turning this on can break tool calls instead of "
    "shrinking them -- try it and check that tools still work.",
    "",
)
SETTING_TEXT["env.CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = (
    "The output-token cap for a single reply, above the model's own default.",
    "A higher cap reserves more of the context budget for one reply, so auto-compaction triggers sooner on "
    "everything else in the conversation.",
    "",
)
SETTING_TEXT["env.CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = (
    SETTING_TEXT["autoCompactWindow"][0]
    + " While this variable is set, it overrides the autoCompactWindow setting.",
    SETTING_TEXT["autoCompactWindow"][1],
    "A value in a settings file's env block replaces the one from your shell.",
)

#: Short names for every allowlisted key, for forms and tables.
LEVER_LABELS = {
    "model": "Model",
    "effortLevel": "Effort level",
    "effort": "Effort level",
    "autoCompactWindow": "Summarise the conversation at (tokens)",
    "autoCompactEnabled": "Summarise the conversation automatically",
    "outputStyle": "Output style",
    "promptCacheTtl": "Main session cache lifetime",
    "subagentPromptCacheTtl": "Subagent cache lifetime",
    "experimental.cacheTtl": "Cache lifetime",
    "enabledPlugins": "Plugins turned on",
    "skillOverrides": "How skills are shown to Claude",
    "disabledMcpjsonServers": "Project MCP servers turned off",
    "enabledMcpjsonServers": "Project MCP servers turned on",
    "alwaysThinkingEnabled": "Always think before replying",
    "cleanupPeriodDays": "Keep conversation logs for (days)",
    "maxTurns": "Most replies per run",
    "omitClaudeMd": "Leave out CLAUDE.md files",
    "memory": "Memory",
    "tools": "Tools it may use",
    "disallowedTools": "Tools it may not use",
    "skills": "Skills loaded at start",
    "mcpServers": "MCP servers it may use",
    "includeCoAuthoredBy": "Co-authored-by line on commits (deprecated)",
    "env.DISABLE_PROMPT_CACHING": "Prompt caching (env override, all models)",
    "env.DISABLE_PROMPT_CACHING_SONNET": "Prompt caching (env override, Sonnet)",
    "env.DISABLE_PROMPT_CACHING_OPUS": "Prompt caching (env override, Opus)",
    "env.DISABLE_PROMPT_CACHING_HAIKU": "Prompt caching (env override, Haiku)",
    "env.DISABLE_PROMPT_CACHING_FABLE": "Prompt caching (env override, Fable)",
    "env.ENABLE_TOOL_SEARCH": "MCP tool search (env override)",
    "env.CLAUDE_CODE_MAX_OUTPUT_TOKENS": "Output token cap (env override)",
    "env.CLAUDE_CODE_AUTO_COMPACT_WINDOW": "Summarise the conversation at (tokens, env override)",
    "fastMode": "Fast mode (price premium for a faster reply)",
}

_SCOPE_WHERE = {
    "user": ("~/.claude/agents/{agent}.md", "your own agent file, used in every project"),
    "repo": (".claude/agents/{agent}.md", "this project's agent file, used by everyone who works in it"),
}
_SETTINGS_WHERE = {
    "user": ("~/.claude/settings.json", "your user settings, used in every project"),
    # COV-01: the layer PROFILE_SCOPE_WHERE already distinguishes from
    # "repo" below -- per-machine, never checked in (docs/config-layers.md).
    "project-local": (".claude/settings.local.json", "this project, on your machine only"),
    "repo": (".claude/settings.json", "this project's shared settings"),
}

#: Workflow recommendations (no setting to change) that come with a
#: prompt anyway, keyed by ``Recommendation.id`` (or, when
#: :data:`Recommendation.variant` picks one, ``"id:variant"`` -- see
#: :func:`build_fixes`). Every entry here is just the instruction: it is
#: appended to :data:`_FINDING_OPEN` plus ``rec.why``, so the finding is
#: stated once, not restated mid-sentence (that used to be
#: ``{title_lower}``). A "from now on" entry -- one that asks Claude to
#: work differently going forward rather than to edit a specific file --
#: ends with :data:`PROMPT_SCOPE`, since unlike a setting change a restart
#: doesn't pick it back up; only a CLAUDE.md line does.
_WORKFLOW_PROMPTS = {
    "spawn-shared-claude-md": (
        "Please read my CLAUDE.md files and my agent files in ~/.claude/agents and .claude/agents. "
        "Find sections that only some agents need, and propose moving each one into those agents' own "
        "files or into a skill they load on demand. Keep rules every agent needs where they are. "
        "Show me the proposed moves and the diff before changing anything. Claude Code will ask my "
        "permission before editing files under .claude."
    ),
    "baseline-bloat": (
        "Please list the MCP servers and plugins I have enabled (MCP servers in ~/.claude.json and this "
        "project's .mcp.json, plugins in ~/.claude/settings.json and this project's .claude/settings.json), "
        "say which ones this project doesn't seem to use, and propose turning those off for this project "
        "only. Show me the proposed change before making it, and don't print any token or key from those "
        "files. Claude Code will ask my permission before editing files under .claude."
    ),
    # {action} is the card's own per-server list: which servers, and the
    # fix for each one's kind (tool_search.server_fix).
    "mcp-unused-server": (
        "Please help me turn them off, as follows. {action} Before running a command, show it to me; "
        "before removing a server, show me its command and arguments (never its tokens, keys or env values) "
        "so I can add it back later. For a switch in the desktop app or on claude.ai, which you can't change "
        "yourself, tell me exactly where to click instead."
    ),
    "agent-report-size": (
        "{agent} sends back long final reports, and each one stays in my main session's context. Please "
        "read {agent}'s agent file (~/.claude/agents/{agent}.md or .claude/agents/{agent}.md) and propose "
        "one or two lines for its prompt asking for a short report: findings, file paths and next steps, "
        "not the working. If {agent} has no agent file (it is built into Claude Code), propose a sentence I "
        "can add to the task prompts I send it instead. Show me the diff before saving. Claude Code will ask "
        "my permission before editing files under .claude."
    ),
    # UX-8: the rest of the workflow-rule ids below (every one that used
    # to fall through build_fixes with no template at all, per this
    # module's own docstring) so every card gets a prompt, not just the
    # three above.
    "ttl-switch": (
        "Please check the current TTL setting for {agent} (promptCacheTtl for the main session, "
        "subagentPromptCacheTtl or experimental.cacheTtl for a named agent) in ~/.claude/settings.json or "
        "its agent file, and switch it to what this finding recommends. If subagentPromptCacheTtl is set, "
        "Claude Code uses it before the agent file. Show me the diff before saving. Claude Code will ask my "
        "permission before editing files under .claude."
    ),
    "long-tool-waits": (
        "From now on, when you're about to run something long-running, batch any instructions I've queued "
        "first so they land before the wait starts, rather than after. " + PROMPT_SCOPE
    ),
    "notification-invalidation": (
        "From now on, when several subagents might report back close together, hold their notifications "
        "and summarise them together instead of one at a time, where that doesn't cost me visibility I "
        "need. " + PROMPT_SCOPE
    ),
    "batch-instructions": (
        "From now on, if I send you a few small separate asks in a row, ask whether I'd like them batched "
        "into one message before you start on the first. " + PROMPT_SCOPE
    ),
    "subagent-volume": (
        "Please look at why {agent} is spawned so often, or so expensively, in my recent sessions, and "
        "propose whether fewer spawns, a cheaper model, or a tighter brief fits best. Show me the change "
        "before making it."
    ),
    "compaction-churn": (
        "Please check the current autoCompactWindow in ~/.claude/settings.json or this project's "
        ".claude/settings.json (or the CLAUDE_CODE_AUTO_COMPACT_WINDOW variable, which overrides it when "
        "set), and raise it to a value that fits this finding. Show me the diff before saving. Claude Code "
        "will ask my permission before editing files under .claude."
    ),
    # Workflow only: this form is used once the auto-compact window has
    # been left to the compaction replay or to compaction-churn (see
    # advice._consolidate_compaction), so it never proposes the setting.
    "long-context-share": (
        "From now on, send searches and exploration-heavy work to a subagent, whose context is discarded "
        "when it finishes, and when I switch to an unrelated task, suggest starting a fresh session instead "
        "of carrying this one on. " + PROMPT_SCOPE
    ),
    # known_savers.active_in_report found tokensave at work: its hook
    # blocks an Explore-agent spawn and a symbol-shaped search in this
    # project, so the plain variant's advice above would just get
    # redirected. Point at tokensave's own tools instead.
    "long-context-share:tokensave": (
        "From now on, find code with tokensave's tools -- tokensave_context for a concept, tokensave_search "
        "for a symbol -- and read only the part of a file you need (tokensave_read in \"lines\" mode, or "
        "Read with offset/limit) instead of the whole file. When I switch to an unrelated task, suggest "
        "starting a fresh session instead of carrying this one on. " + PROMPT_SCOPE
    ),
    "plan-handoff": (
        "Please add a short instruction to my ~/.claude/CLAUDE.md: when I approve a plan that took a lot of "
        "exploring, remind me to run /clear and start the build from the saved plan file, one phase per "
        "session. Show me the diff before saving. Claude Code will ask my permission before editing files "
        "under .claude."
    ),
    "run-split": (
        "My {agent} runs get long, and every later reply reads again all the run has read. Please add a short "
        "instruction to my ~/.claude/CLAUDE.md: when a task for {agent} "
        "is large, give it one part per run, and start a fresh {agent} for the next part with a short note of "
        "what's done, what's left and the files involved, rather than one long run. If {agent} has an agent file "
        "(~/.claude/agents/{agent}.md or .claude/agents/{agent}.md), propose a line asking it to end each run with "
        "that note. Show me the diff before saving. Claude Code will ask my permission before editing files under "
        ".claude."
    ),
    "hook-failures": (
        "Please find these hooks in ~/.claude/settings.json, this project's .claude/settings.json and "
        ".claude/settings.local.json, and any plugin I have enabled. For each one, work out why it fails: a "
        "script named by a relative path (start it with ${{CLAUDE_PROJECT_DIR}} instead), a file that isn't "
        "there, or an error in the script itself. Propose a fix for each and show me the diff before saving. "
        "Claude Code will ask my permission before editing files under .claude."
    ),
    "hook-block-resent": (
        "Please read that hook's script and its entry in my settings.json files. Propose changing it "
        "to let the call through and pass its message as hookSpecificOutput.additionalContext, or to rewrite the "
        "call with updatedInput, where that keeps what the hook is for. Keep any block that stops something harmful. "
        "Show me the diff before saving. Claude Code will ask my permission before editing files under .claude."
    ),
    "hook-context-carry": (
        "Please find that hook in my settings.json files or my enabled plugins and read its script. "
        "Propose making its message shorter, or having it add context only when there's something to act on. If it "
        "comes from a plugin, tell me how to turn it off for projects that don't need it instead. Show me the diff "
        "before saving. Claude Code will ask my permission before editing files under .claude."
    ),
    "spawn-task-prompt": (
        "From now on, when I'm about to give {agent} a long brief, point it at the files it needs instead "
        "of pasting their contents, and leave out background it can look up itself. " + PROMPT_SCOPE
    ),
    "spawn-cost": (
        "Please check whether {agent} has its own agent file; if it does, propose an omitClaudeMd or "
        "narrower-skills change to trim what it's sent at startup, and if it's a built-in agent type with no "
        "file, suggest how to shorten the Agent prompt I write when I spawn it. Show me the change before "
        "making it."
    ),
    "effort-mismatch": (
        "Please check the current effortLevel in ~/.claude/settings.json (or the relevant agent's "
        "frontmatter) and propose lowering it, keeping /effort in mind for the odd hard task. Show me the "
        "diff before saving. Claude Code will ask my permission before editing files under .claude."
    ),
    "discovery-share": (
        "Please draft a short reference doc or briefing from what you've already found in this project, so "
        "a future session can start from it instead of re-discovering the same ground. Show me the draft "
        "before saving it anywhere."
    ),
    "pricing-coverage": (
        "Please look up the missing model id(s) this finding names and add a row for each to pricing.toml "
        "with their real per-token rates, citing the source. Show me the diff before saving."
    ),
    "limit-pressure": (
        "Please look at when these pauses happened in my recent sessions and suggest how to pace concurrent "
        "agents to my usage window, or whether my weekly cap is worth reviewing against actual usage."
    ),
    "tool-output-carry": (
        "From now on, when you'd read a large file or run a command with long output, prefer Grep over Read "
        "for a large file, pipe long shell output through head/tail or a digest script, and keep agent "
        "reports short before they enter context. " + PROMPT_SCOPE
    ),
    "compaction-window": (
        "Please check the current autoCompactWindow in ~/.claude/settings.json or this project's "
        ".claude/settings.json (or the CLAUDE_CODE_AUTO_COMPACT_WINDOW variable, which overrides it when "
        "set), and raise it to at least the value this finding names. Show me the diff "
        "before saving. Claude Code will ask my permission before editing files under .claude."
    ),
    "model-tier": (
        "Please check the current model setting for {agent} (settings.json for the main session, or its "
        "agent file's frontmatter) and propose switching to the cheaper tier this finding names. Show me "
        "the diff before saving, and let's compare quality on a few tasks before keeping it. Claude Code "
        "will ask my permission before editing files under .claude."
    ),
    # Agent-model cards (agent_models.RULES): no SettingChange, so a prompt.
    # The first and third are "from now on" rules, so they end in
    # PROMPT_SCOPE; the second edits files a restart picks up. A prompt has
    # no braces: build_fixes runs it through str.format.
    "agent-model-inherited": (
        "From now on, set the model on every subagent and workflow agent you start. Never leave an agent to "
        "inherit my session's model, even where a tool's instructions say to omit it. Use Sonnet for agents "
        "that write code to a settled spec: implementers, and fixers applying a change a judge has specified. "
        "Use Opus for agents that decide: integrate, review, verify and judge. Never give one Opus agent both "
        "the deciding and the applying. " + PROMPT_SCOPE
    ),
    "agent-model-asked": (
        "Please find where my agents that write code are started with model set to opus or fable: skills, "
        "agent files, workflow scripts and CLAUDE.md files. For each place, say whether its work needs Opus. "
        "Propose sonnet where it doesn't, and show me each change before saving. " + PROMPT_RESTART
    ),
    "agent-decide-apply": (
        "From now on, when an agent's job is to review, audit, verify or judge, have it report the exact "
        "changes instead of making them. Then start a separate agent on Sonnet to apply them. Keep the "
        "deciding agent on Opus. " + PROMPT_SCOPE
    ),
    "wasted-turns": (
        "From now on, when the likely cause repeats (see the finding above), flag it before you start "
        "rather than after. " + PROMPT_SCOPE
    ),
    # COV-07/COV-11: env-attribution-deprecated has no SettingChange
    # (its real target, attribution.commit, isn't on profiles.schema's
    # SETTINGS_ALLOWLIST -- see recommend.py's COV-09 section comment),
    # so it keeps a prompt instead, asking Claude to do the migration.
    "env-attribution-deprecated": (
        "Please translate my current includeCoAuthoredBy value in ~/.claude/settings.json (or this "
        "project's .claude/settings.json, whichever sets it) into an equivalent attribution.commit value -- "
        "false becomes an empty commit trailer, true becomes the default one. Show me the diff before "
        "saving. Claude Code will ask my permission before editing files under .claude."
    ),
}

#: rec.id -> (where and who it affects, trade-off, how to undo it) for
#: every workflow-only recommendation (``rec.changes`` empty) that isn't
#: already covered by :func:`explainer_for`. UX-8: every card gets a
#: where/trade-off/undo entry, not just ones with a real
#: :class:`SettingChange`. An id left out of both this dict and
#: :data:`_WORKFLOW_PROMPTS` still gets no fix at all (see
#: ``build_fixes``); an id here with no counterpart in
#: ``_WORKFLOW_PROMPTS`` gets an explainer with no prompt (a purely
#: informational card with nothing to ask Claude to do). "Where and who
#: it affects"/"How to undo it" for a card whose prompt ends in
#: :data:`PROMPT_SCOPE`: the rule doesn't live in Claude Code's config,
#: it lives wherever Claude was told to keep the CLAUDE.md line (or
#: nowhere, for a session-only rule).
_SCOPE_WHERE_TEXT = (
    "Wherever you tell Claude when it asks: nothing saved (this session only), a line in this project's "
    "CLAUDE.md (everyone who works in it), or a line in your ~/.claude/CLAUDE.md (every project you open)."
)
_SCOPE_UNDO_TEXT = "Remove the line from that CLAUDE.md; a session-only rule ends with the session."

_WORKFLOW_EXPLAINER: dict[str, tuple[str, str, str]] = {
    "ttl-switch": (
        "settings.json's promptCacheTtl (the main session) or subagentPromptCacheTtl (every subagent), or an "
        "agent file's experimental.cacheTtl frontmatter (a named agent type) -- whichever key this finding "
        "names, at user or project scope depending on where it is already set.",
        "A 1-hour cache costs more to write than a 5-minute one, so it only pays off when replies "
        "are often more than 5 minutes apart; a 1-hour lifetime in an agent file is also ignored while a "
        "Pro or Max plan is drawing on extra usage credits.",
        "Set the TTL key back to its previous value (Claude Code shows the change before saving it, and "
        "claudeglass apply --revert undoes a change made with apply).",
    ),
    "long-tool-waits": (
        _SCOPE_WHERE_TEXT,
        "Batching instructions before a long command commits you to them before seeing its output, so "
        "you may still need a follow-up message if the result changes what you'd ask for.",
        _SCOPE_UNDO_TEXT,
    ),
    "notification-invalidation": (
        _SCOPE_WHERE_TEXT,
        "Batching notifications means you see a subagent's progress less often while it runs.",
        _SCOPE_UNDO_TEXT,
    ),
    "batch-instructions": (
        _SCOPE_WHERE_TEXT,
        "One larger message is harder to skim than several short ones, and you lose the chance to react "
        "to Claude's answer to the first before sending the rest.",
        _SCOPE_UNDO_TEXT,
    ),
    "subagent-volume": (
        "Nowhere in Claude Code's config directly -- the fix is fewer spawns, a cheaper model for this "
        "agent type, or a tighter brief; a model change is set in settings.json or the agent's frontmatter.",
        "Spawning this agent type less often, or briefing it more tightly, means less parallel work per "
        "message; a cheaper model may need more turns or miss things a stronger one wouldn't.",
        "Go back to spawning it as before, or set the model back to what it was.",
    ),
    "compaction-churn": (
        "settings.json's autoCompactWindow, or the CLAUDE_CODE_AUTO_COMPACT_WINDOW variable when it's set "
        "(it overrides the setting), at whichever scope this report's \"Setting to change\" line above "
        "names.",
        "A summary drops detail; after one, Claude may re-read files or lose track of earlier decisions -- "
        "raising the window trades that against compacting, and re-reading the growing conversation, more "
        "often.",
        "Set the window back to its previous value (Claude Code shows the change before saving it).",
    ),
    "long-context-share": (
        _SCOPE_WHERE_TEXT,
        "Moving exploration into a subagent means its findings only reach the main session through its final "
        "report, which can lose nuance; a fresh session starts without what the old one knew.",
        _SCOPE_UNDO_TEXT,
    ),
    # known_savers.active_in_report found tokensave at work -- see the
    # matching prompt variant in _WORKFLOW_PROMPTS.
    "long-context-share:tokensave": (
        _SCOPE_WHERE_TEXT,
        "Reading only the lines tokensave points at, instead of the whole file, can miss context nearby; "
        "check the surrounding code before assuming there's nothing else relevant. A fresh session starts "
        "without what the old one knew.",
        _SCOPE_UNDO_TEXT,
    ),
    "plan-handoff": (
        "Nowhere in Claude Code's config: how you move from planning to building in the main session. The "
        "prompt adds a reminder to your CLAUDE.md.",
        "A fresh session knows only the plan and what it reads again, so a thin plan can mean re-reading "
        "files or asking again about decisions made while planning.",
        "Remove the reminder from your CLAUDE.md and keep building in the planning session.",
    ),
    "run-split": (
        "Nowhere in Claude Code's config: how the main session hands work to this agent. The prompt adds an "
        "instruction to your CLAUDE.md, and a line to the agent's file if it has one.",
        "Each fresh run knows only its task and the note, so it may read some files again, and a thin note can "
        "miss a decision the last run made.",
        "Remove the instruction from your CLAUDE.md, and the line from the agent's file.",
    ),
    "hook-failures": (
        "The hooks entries in your settings.json files (user, project or local) or a plugin's hooks, for every "
        "session that runs them.",
        "A hook that starts working again does its job again: a guard that was failing will start blocking the "
        "calls it was written to block.",
        "Put the hook's command back as it was (Claude Code shows the change before saving it).",
    ),
    "hook-block-resent": (
        "The hook's script, and its entry in your settings.json files, for every session that runs it.",
        "Letting a call through with a note trusts Claude to act on the note; a block makes sure the call "
        "doesn't run as sent.",
        "Put the hook's script back as it was.",
    ),
    "hook-context-carry": (
        "The hook's script or its plugin's settings, for every session that runs it.",
        "A shorter message, or one sent less often, gives Claude less to go on when the hook has something "
        "to say.",
        "Put the hook's script back as it was, or turn the plugin's hook back on.",
    ),
    "cache-read-dominance": (
        "Nothing to change here -- this card is informational.",
        "None -- no change is proposed.",
        "Nothing to undo.",
    ),
    "baseline-bloat": (
        "~/.claude.json (your user-scope MCP servers, and each project's local-scope ones and /mcp "
        "switches), a project's .mcp.json, or settings.json's enabledPlugins; and each agent's own "
        "mcpServers frontmatter if only some agents need a given server.",
        "Turning a server off for this project means no agent in it can use that server's tools, even for "
        "a task that would have needed one.",
        "Turn the server back on in the same file (Claude Code shows the change before saving it).",
    ),
    "mcp-unused-server": (
        "Only the servers named on the card. A connector in the desktop app is switched off under + > "
        "Connectors, or, if you added it, disconnected at claude.ai/customize/connectors (which removes it "
        "from claude.ai chat too; one Anthropic provides itself has nothing to disconnect there); a /mcp switch or a local-scope server applies to one project (~/.claude.json); a user-scope "
        "server applies to every project; a project's .mcp.json server is turned off just for you in "
        ".claude/settings.local.json.",
        "Claude can't use a server you've turned off, even for a task that needs it, until you turn it back "
        "on. Already-running sessions keep it until they end or are summarised.",
        "Switch the connector back on under + > Connectors or reconnect it at claude.ai/customize/connectors; "
        "re-enable it in /mcp or /plugin; or add a removed server back with claude mcp add, using the "
        "command and arguments Claude showed you before removing it.",
    ),
    "agent-report-size": (
        "The agent's own file (~/.claude/agents/<type>.md or .claude/agents/<type>.md) if it has one, or "
        "the Agent prompt you write when you spawn a built-in agent type.",
        "A shorter report can leave out detail you'd have wanted, especially for a task whose outcome is "
        "hard to summarise briefly.",
        "Remove the added report-length instruction (Claude Code shows the change before saving it if "
        "it's in an agent file).",
    ),
    "spawn-task-prompt": (
        _SCOPE_WHERE_TEXT,
        "Pointing an agent at files instead of pasting their contents means it spends a turn reading them "
        "itself, which costs a little and assumes it can find the right ones.",
        _SCOPE_UNDO_TEXT,
    ),
    "spawn-shared-claude-md": (
        "The CLAUDE.md file(s) named in this finding, and the agent files that would gain the moved "
        "sections.",
        "A rule moved out of the shared file only reaches the agents it's moved into; any other agent "
        "that relied on it implicitly no longer sees it.",
        "Move the section back into the shared file (Claude Code shows the diff before saving it).",
    ),
    "spawn-cost": (
        "The agent's own frontmatter file, if it has one (omitClaudeMd there trims what it's sent at "
        "spawn); for a built-in agent type with no file, nowhere in the config -- the fix is a shorter "
        "Agent prompt when you spawn it.",
        "Trimming what an agent receives at spawn can remove context it actually needed, costing you a "
        "follow-up message instead.",
        "Undo the frontmatter change, or go back to briefing it as before.",
    ),
    "effort-mismatch": (
        "settings.json's effortLevel (or an agent's own effort frontmatter field), at whichever scope "
        "this report's \"Setting to change\" line above names; /effort raises it back for a single task "
        "without changing the setting.",
        "Lower effort can miss things on genuinely hard problems -- you're trading that risk against the "
        "thinking tokens spent on work that, per this finding, didn't need them.",
        "Set effortLevel back to its previous value, or raise it for one task with /effort without "
        "touching the setting.",
    ),
    "discovery-share": (
        "Nowhere in Claude Code's config -- this is about writing a briefing or reference doc once "
        "instead of re-discovering the same ground each session.",
        "A briefing or reference doc can go stale as the codebase changes, so it needs occasional upkeep "
        "or it starts giving wrong context.",
        "Stop referring to the doc, or delete it -- no setting was changed.",
    ),
    "pricing-coverage": (
        "pricing.toml, in this project or wherever your pricing file lives.",
        "None -- adding a pricing row only makes this report's cost figures more exact; it doesn't change "
        "how Claude Code runs.",
        "Remove the row you added from pricing.toml.",
    ),
    "data-quality": (
        "Nothing to change in Claude Code's config -- this card is a caveat about how much to trust this "
        "report's own figures.",
        "None -- no change is proposed.",
        "Nothing to undo.",
    ),
    "limit-pressure": (
        "Nowhere in Claude Code's config directly -- this is about pacing concurrent agents to your usage "
        "window, or reviewing your weekly cap in the Claude Code / Anthropic Console against actual usage.",
        "Pacing agents to stay under the cap means less work happens in parallel; raising the cap, where "
        "your plan allows it, costs more.",
        "Go back to running agents concurrently as before.",
    ),
    "tool-output-carry": (
        _SCOPE_WHERE_TEXT,
        "Piping output through head/tail or capping a report's length can cut detail you needed, forcing "
        "a follow-up command to see the rest.",
        _SCOPE_UNDO_TEXT,
    ),
    "compaction-window": (
        "settings.json's autoCompactWindow, or the CLAUDE_CODE_AUTO_COMPACT_WINDOW variable when it's set "
        "(it overrides the setting), at whichever scope this report's \"Setting to change\" line above "
        "names.",
        "A larger window means fewer summaries, but each one that does happen drops more; a smaller "
        "window compacts more often and can't see files a session re-reads after a summary the way this "
        "simulation's rediscovery correction accounts for.",
        "Set the window back to its previous value (Claude Code shows the change before saving it).",
    ),
    "model-tier": (
        "settings.json's model key (the main session) or the agent's own model frontmatter field, at "
        "whichever scope this report's \"Setting to change\" line above names.",
        "A smaller model may need more replies for the same task or get some tasks wrong outright -- this "
        "report holds token volumes and turn counts constant, so the real saving depends on trying it and "
        "comparing quality first.",
        "Set the model back to what it was (Claude Code shows the change before saving it).",
    ),
    # Agent-model cards (agent_models.RULES): the lever is the call that
    # starts the agent, so where it goes is a rule, not a setting.
    "agent-model-inherited": (
        _SCOPE_WHERE_TEXT + " In a workflow script the model goes on each call, as in agent(brief, { phase: "
        "'Implement', model: 'sonnet' }). A model in meta.phases only labels the phase. For a named agent "
        "type, a model: sonnet line in its agent file also works; a file named like a built-in agent "
        "replaces it whole.",
        "Sonnet may need more replies on hard, open-ended code, so keep Opus where the spec isn't settled. "
        "CLAUDE_CODE_SUBAGENT_MODEL=sonnet is blunter: it also moves reviewers and judges, any model a call "
        "or agent file sets still wins, and it never reaches Explore, Plan or forks.",
        _SCOPE_UNDO_TEXT,
    ),
    "agent-model-asked": (
        "The prompt, skill, agent file or workflow script that names the model when it starts these agents.",
        "Sonnet may need more replies on hard code. Keep Opus where an agent has to decide as well as write.",
        "Set the model back to opus where you changed it.",
    ),
    "agent-decide-apply": (
        _SCOPE_WHERE_TEXT,
        "Two agents instead of one: the decider's report has to be exact enough for the applier to follow "
        "without deciding again.",
        _SCOPE_UNDO_TEXT,
    ),
    "wasted-turns": (
        _SCOPE_WHERE_TEXT,
        "Slowing down to avoid a wasted turn (double-checking before running a command, say) costs a "
        "little time up front on every turn, not just the ones that would have been wasted.",
        _SCOPE_UNDO_TEXT,
    ),
    "window-budget": (
        "Nothing to change here -- this card states a fact about your plan's weekly limit, not a setting.",
        "None -- no change is proposed.",
        "Nothing to undo.",
    ),
    # COV-07/COV-11: informational env-lever rules with no SettingChange
    # -- env-subagent-model doesn't propose a new value (it's a
    # precedence caveat about the value already set); env-attribution-
    # deprecated's real target isn't on the SETTINGS_ALLOWLIST today (see
    # recommend.py's COV-09 section comment), so it gets a prompt above
    # instead of a command.
    "env-subagent-model": (
        "Nowhere to change -- this card is a caveat about CLAUDE_CODE_SUBAGENT_MODEL's precedence, not a "
        "proposed value. Check the agent files named in the finding above if a subagent isn't running on "
        "the model you expect.",
        "None -- no change is proposed.",
        "Nothing to undo.",
    ),
    "env-attribution-deprecated": (
        "settings.json's env block doesn't apply here -- includeCoAuthoredBy and its replacement, "
        "attribution, are both plain settings.json keys, at whichever scope this report's \"Setting to "
        "change\" line above names.",
        "attribution can also change or hide the pull-request attribution text and the session link "
        "separately, not just the commit trailer -- read its docs before copying the old value over as is.",
        "Remove the attribution key; includeCoAuthoredBy (still valid) takes over again.",
    ),
}


#: Work that has to happen before a key is set, or the change loses
#: something the agent needs. The prompt asks Claude to do it first; the
#: command, which only sets the key, carries it as a warning.
_PREPARE = {
    "omitClaudeMd": (
        "First read the CLAUDE.md files {agent} receives today (~/.claude/CLAUDE.md, the project's CLAUDE.md "
        "and CLAUDE.local.md, and any files they import). List the rules {agent} needs to do its job, show me "
        "the list, and add them to the agent's own prompt (the body of {path}, below the frontmatter). Keep it "
        "short: every line is sent on every spawn."
    ),
}
_COMMAND_WARNINGS = {
    "omitClaudeMd": (
        "This only sets the flag. Move the rules the agent needs into its agent file first, or use the prompt "
        "above, which does both."
    ),
}


def _human(value) -> str:
    if value is None:
        return "not set"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value) if value else "(empty list)"
    if isinstance(value, dict):
        return ", ".join(f"{k}: {_human(v)}" for k, v in value.items()) if value else "(none)"
    return str(value)


def _shown(value) -> str:
    """``_human`` for reading: whole numbers get thousands separators.
    Prompts keep ``_human`` so Claude copies the value as written."""
    if isinstance(value, int) and not isinstance(value, bool):
        return f"{value:,}"
    return _human(value)


def _model_family(model_id: str) -> str:
    # Opus while planning, Sonnet otherwise: not the same as "opus".
    if model_id.startswith("opusplan"):
        return "opusplan"
    for family in ("haiku", "sonnet", "opus", "fable"):
        if family in model_id:
            return family
    return model_id


def already_set(key: str, value, now) -> bool:
    """Whether ``now`` already is ``value``, so offering the change would
    do nothing. A model id matches its family's alias ("claude-sonnet-5"
    is "sonnet"), text ignores case, and a map (skillOverrides) is set
    when every entry it names already has that value."""
    if value is None or now is None:
        return False
    if isinstance(value, dict):
        return isinstance(now, dict) and all(already_set(key, v, now.get(k)) for k, v in value.items())
    if isinstance(value, str) and isinstance(now, str):
        wanted, current = value.strip().lower(), now.strip().lower()
        if key == "model":
            wanted, current = _model_family(wanted), _model_family(current)
        return wanted == current
    if isinstance(value, bool) or isinstance(now, bool):
        return value is now
    return value == now


def _cli_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value)
    if isinstance(value, dict):
        return ",".join(f"{k}:{_cli_value(v)}" for k, v in value.items())
    return str(value)


_MANAGED_WHERE = ("your organisation's managed settings", "set by policy; only your administrator can change it")


def _where(change: SettingChange, scope: str) -> tuple[str, str]:
    scope = change.scope or scope
    if scope == "managed":
        return _MANAGED_WHERE
    if change.target == "agent":
        path, who = _SCOPE_WHERE.get(scope, _SCOPE_WHERE["user"])
        return path.format(agent=change.agent), who
    path, who = _SETTINGS_WHERE.get(scope, _SETTINGS_WHERE["user"])
    if change.key.startswith("env."):
        # COV-07/COV-11: guidance always goes through the settings.json
        # env block -- say so, the same way profile_change_where already
        # does for a profile's own env.<NAME> rows.
        path = f"{path} (env block)"
    return path, who


def command_for(change: SettingChange, scope: str) -> str | None:
    """The ``apply --set`` line for ``change`` (dry run first), or
    ``None`` when the value needs judgement or a new agent file."""
    scope = change.scope or scope
    if change.value is None or change.new_agent_file or scope == "managed":
        return None
    parts = ["claudeglass", "apply", "--set", f"{change.key}={_cli_value(change.value)}"]
    if change.target == "agent" and change.agent:
        parts += ["--agent", change.agent]
    if scope in ("repo", "project-local"):
        parts += ["--scope", scope, "--project-dir", "."]
    else:
        parts += ["--scope", "user"]
    parts.append("--dry-run")
    return " ".join(shlex.quote(p) for p in parts)


def _after(change: SettingChange) -> str:
    if change.value is not None:
        return _human(change.value)
    return change.suggested or "your choice"


#: Model families, cheapest first.
_MODEL_ORDER = ("haiku", "sonnet", "opus", "fable")


def _to_larger_model(change: SettingChange) -> bool:
    """Whether ``change`` moves a model setting up to a pricier family."""
    if change.key != "model" or not isinstance(change.value, str) or not isinstance(change.current, str):
        return False
    after, before = _model_family(change.value.lower()), _model_family(change.current.lower())
    return after in _MODEL_ORDER and before in _MODEL_ORDER and _MODEL_ORDER.index(after) > _MODEL_ORDER.index(before)


def explainer_for(rec: Recommendation, change: SettingChange) -> list[tuple[str, str]]:
    what, tradeoff, caveat = SETTING_TEXT.get(change.key, (f"The {change.key} setting.", "", ""))
    if _to_larger_model(change):
        tradeoff = (
            "A larger model costs more per token, so every reply this agent sends costs more. The models check "
            "shows how much."
        )
    path, who = _where(change, rec.scope)
    if change.new_agent_file:
        where = (
            f"A new file, {path}. {change.agent} is built into Claude Code; an agent file with the same "
            "name replaces it, so the new file must still do the built-in agent's job."
        )
    else:
        where = f"{path}: {who}."
    saving = change.saving or rec.estimated_saving
    effect = saving or "Not estimated: this part isn't measured on its own."
    if saving and rec.saving_basis:
        effect += " " + rec.saving_basis
    if change.unconfirmed:
        effect += " Claude Code's docs don't confirm this effect, so check the numbers after the change."
    notes = " ".join(n for n in (caveat, change.note) if n)
    undo = (
        "Run the 'claudeglass apply --revert' command that apply prints, or set the value back by hand."
        if command_for(change, rec.scope)
        else "Put the file back as it was (Claude Code shows the change before saving it)."
    )
    return [
        ("What this setting controls", what),
        (
            "Now and after",
            f"Now: {_shown(change.current)}. "
            f"After: {_shown(change.value) if change.value is not None else _after(change)}.",
        ),
        ("Where and who it affects", where),
        ("Expected effect", effect),
        ("Trade-off", " ".join(t for t in (tradeoff, notes) if t) or "None known."),
        ("How to undo it", undo),
    ]


def prompt_for(rec: Recommendation, change: SettingChange) -> str:
    if (change.scope or rec.scope) == "managed":
        return (
            f"My organisation's managed settings lock {change.key}, so I can't change it myself. Draft a short "
            f"request to my administrator to set {change.key} to {_after(change)}, saying why: "
            f"{rec.why or rec.title} Keep it under 120 words and don't change any files."
        )
    path, _ = _where(change, rec.scope)
    subject = f"the {change.agent} agent" if change.target == "agent" else "my Claude Code settings"
    prepare = _PREPARE.get(change.key, "").format(agent=change.agent, path=path)
    if change.new_agent_file:
        ask = (
            f"{change.agent} is a built-in Claude Code agent. Create {path}, a custom agent with the same "
            f"name, that does the same job as the built-in one and sets {change.key}: {_after(change)} in "
            "its frontmatter. Write its prompt from what you know of the built-in agent, and keep its "
            "tools the same."
        )
        if prepare:
            ask += " " + prepare
    elif change.key.startswith("env."):
        # COV-07/COV-11: an env lever is a name in the settings.json env
        # block, not a bare top-level key -- phrase it as adding/changing
        # that one entry, matching quick_actions.py's own env-cap prompt.
        env_name = change.key.split(".", 1)[1]
        if change.value is not None:
            ask = (
                f'In {path}, add "{env_name}": {json.dumps(change.value)} to the "env" object (create it if '
                "it's missing), keeping every other entry."
            )
        else:
            ask = f'In {path}, change the "{env_name}" entry in the "env" object: {change.suggested}.'
        if prepare:
            ask = f"{prepare} Then, {ask[0].lower()}{ask[1:]}"
    elif isinstance(change.value, dict):
        # Objects keyed by name (skillOverrides, enabledPlugins): add or
        # update the named entries, as ``apply`` does, never replace.
        entries = ", ".join(f"{json.dumps(k)}: {json.dumps(v)}" for k, v in change.value.items())
        ask = f"In {path}, add {entries} to {change.key}, keeping every entry already there."
        if prepare:
            ask = f"{prepare} Then, {ask[0].lower()}{ask[1:]}"
    elif change.value is not None:
        where = " in the frontmatter" if change.target == "agent" else ""
        ask = f"In {path}, set {change.key} to {json.dumps(change.value)}{where}."
        if prepare:
            ask = f"{prepare} Then, in {path}, set {change.key} to {json.dumps(change.value)}{where}."
    else:
        ask = f"In {path}, change {change.key}: {change.suggested}."
    why = rec.why or rec.title
    lines = [
        f"I want to change a setting for {subject}. {ask}",
        f"Why: {why}",
    ]
    caveat = SETTING_TEXT.get(change.key, ("", "", ""))[2]
    lines += [text for text in (caveat, change.note) if text]
    if change.unconfirmed:
        lines.append("Its effect on startup size isn't documented, so it's an experiment.")
    lines.append(
        "Before saving, restate the change in one sentence and show me the diff. Claude Code will ask "
        "my permission to edit files under .claude; that is expected. Change nothing else. " + PROMPT_RESTART
    )
    return "\n".join(lines)


#: Where a profile's change lands, per ``apply`` scope: settings file,
#: agent file (``{agent}`` filled in), and who it affects.
PROFILE_SCOPE_WHERE = {
    "user": ("~/.claude/settings.json", "~/.claude/agents/{agent}.md", "you, in every project"),
    "project-local": (
        ".claude/settings.local.json",
        ".claude/agents/{agent}.md",
        "this project, on your machine only",
    ),
    "repo": (".claude/settings.json", ".claude/agents/{agent}.md", "everyone who works in this project"),
}


def profile_change_where(key: str, scope: str) -> str:
    """The file a profile diff row's dotted ``key`` (``settings.<name>``,
    ``agents.<agent>.<name>`` or ``env.<NAME>``) is written to under
    ``scope``."""
    settings_path, agent_path, _ = PROFILE_SCOPE_WHERE.get(scope, PROFILE_SCOPE_WHERE["user"])
    if key.startswith("agents."):
        return agent_path.format(agent=key.split(".")[1])
    if key.startswith("env."):
        return f"{settings_path} (env block)"
    return settings_path


def profile_prompt(name: str, rows: list[dict], scope: str) -> str:
    """A self-contained prompt asking Claude to make a profile's changes
    by hand: one line per changed, unmanaged key, naming the file, the
    old and the new value."""
    _, _, who = PROFILE_SCOPE_WHERE.get(scope, PROFILE_SCOPE_WHERE["user"])
    lines = [f'I want to apply the settings profile "{name}" to Claude Code. It affects {who}. Make these changes:']
    for row in rows:
        if row.get("managed") or row.get("current_value") == row.get("proposed_value"):
            continue
        key = row["key"]
        parts = key.split(".")
        where = profile_change_where(key, scope)
        if key.startswith("agents."):
            field = f"{'.'.join(parts[2:])} in the frontmatter"
        elif key.startswith("env."):
            field = f"the environment variable {parts[1]}"
        else:
            field = ".".join(parts[1:])
        lines.append(
            f"- In {where}, set {field} to {json.dumps(row.get('proposed_value'))} "
            f"(now: {_human(row.get('current_value'))})."
        )
    if len(lines) == 1:
        return f'The settings profile "{name}" matches your current settings; there is nothing to change.'
    lines.append(
        "If an agent file doesn't exist, the agent is built into Claude Code: say so and don't create one. "
        "Before saving, restate the changes and show me the diff. Claude Code will ask my permission to "
        "edit files under .claude; that is expected. Change nothing else. " + PROMPT_RESTART
    )
    return "\n".join(lines)


def build_fix(rec: Recommendation, change: SettingChange) -> dict:
    return {
        "key": change.key,
        "agent": change.agent,
        "explainer": [list(pair) for pair in explainer_for(rec, change)],
        "command": command_for(change, rec.scope),
        "command_warning": _COMMAND_WARNINGS.get(change.key, "") if command_for(change, rec.scope) else "",
        "prompt": prompt_for(rec, change),
    }


#: rec.id -> the fix dict's optional ``note`` key (see :func:`fix_note`):
#: "scope" for a "from now on" prompt (:data:`PROMPT_SCOPE`), where
#: :data:`SCOPE_NOTE` replaces the usual :data:`RESTART_NOTE`; "none" for
#: an id whose prompt asks for something that was never a Claude Code
#: file Claude Code re-reads on restart (limit-pressure, discovery-share,
#: pricing-coverage). An id missing here defaults to "none" when it has
#: no prompt at all (:func:`build_fixes` computes that case directly, so
#: every informational id doesn't have to be listed twice) or to the
#: absent key -- :data:`RESTART_NOTE` -- otherwise.
_NOTE_OVERRIDES: dict[str, str] = {
    "long-tool-waits": "scope",
    "notification-invalidation": "scope",
    "batch-instructions": "scope",
    "long-context-share": "scope",
    "spawn-task-prompt": "scope",
    "tool-output-carry": "scope",
    "wasted-turns": "scope",
    "agent-model-inherited": "scope",
    "agent-decide-apply": "scope",
    "limit-pressure": "none",
    "discovery-share": "none",
    "pricing-coverage": "none",
}


def fix_note(fix: dict) -> str:
    """The note to show under a fix's prompt/command: :data:`SCOPE_NOTE`
    when ``fix["note"] == "scope"``, ``""`` when it's ``"none"``, else
    :data:`RESTART_NOTE` -- the fix dict's ``note`` key is absent in that
    last, default case (see :func:`build_fixes`)."""
    note = fix.get("note")
    if note == "none":
        return ""
    if note == "scope":
        return SCOPE_NOTE
    return RESTART_NOTE


def build_fixes(rec: Recommendation) -> list[dict]:
    """One fix per :class:`SettingChange` on ``rec``; for workflow
    advice with no ``SettingChange`` (a bare ``lever`` string or none at
    all), one fix carrying whichever of a where/trade-off/undo explainer
    (:data:`_WORKFLOW_EXPLAINER`, UX-8) and a prompt
    (:data:`_WORKFLOW_PROMPTS`) this id has -- an id with neither gets no
    fix at all, same as before UX-8. ``rec.variant``, when set, picks
    ``"id:variant"`` over plain ``id`` in both dicts, falling back to the
    plain id when there's no variant-specific entry (see
    :data:`Recommendation.variant`). A prompt template may use
    ``{agent}`` (``rec.agent_type``) and ``{action}`` (``rec.action``,
    for a card whose fix differs per item it lists). A purely informational id (no
    change proposed) has an explainer but no prompt: ``prompt`` is then
    ``""``, and the render layer (``render/markdown.py``,
    ``render/html.py``, the dashboard's ``ui.js`` and ``page-setup.js``) skips the "Ask Claude to do it"
    block rather than printing an empty one. When ``rec.why`` is set, the
    explainer's first row is "Why it's suggested": ``rec.why``.
    """
    if rec.changes:
        return [build_fix(rec, change) for change in rec.changes]
    variant_id = f"{rec.id}:{rec.variant}" if rec.variant else rec.id
    workflow_entry = _WORKFLOW_EXPLAINER.get(variant_id, _WORKFLOW_EXPLAINER.get(rec.id))
    template = _WORKFLOW_PROMPTS.get(variant_id, _WORKFLOW_PROMPTS.get(rec.id))
    if template is None and workflow_entry is None:
        return []
    explainer = (
        [
            ["Where and who it affects", workflow_entry[0]],
            ["Trade-off", workflow_entry[1]],
            ["How to undo it", workflow_entry[2]],
        ]
        if workflow_entry is not None
        else []
    )
    if rec.why:
        explainer = [["Why it's suggested", rec.why]] + explainer
    if template is not None:
        opening = _FINDING_OPEN.format(title=rec.title)
        if rec.why:
            opening = f"{opening} {rec.why}"
        prompt = f"{opening} {template.format(agent=rec.agent_type or 'this agent', action=rec.action)}"
    else:
        prompt = ""
    fix = {
        "key": None,
        "agent": None,
        "explainer": explainer,
        "command": None,
        "command_warning": "",
        "prompt": prompt,
    }
    note = _NOTE_OVERRIDES.get(rec.id) or ("none" if not prompt else "")
    if note:
        fix["note"] = note
    return [fix]


def attach_fixes(recommendations: list[Recommendation]) -> None:
    for rec in recommendations:
        rec.fixes = build_fixes(rec)


__all__ = [
    "LEVER_LABELS",
    "PROFILE_SCOPE_WHERE",
    "PROMPT_RESTART",
    "PROMPT_SCOPE",
    "RESTART_NOTE",
    "SCOPE_NOTE",
    "SETTING_TEXT",
    "already_set",
    "attach_fixes",
    "build_fix",
    "build_fixes",
    "command_for",
    "explainer_for",
    "fix_note",
    "profile_change_where",
    "profile_prompt",
    "prompt_for",
]

"""The ClaudeGlass mod's own words: the band above the prompt, the
spinner, the hint line, the notes at the end of a reply, the log lines
and the ``/glass`` pane.

The mod runs inside Claude Code and can't import Python, so
``scripts/export-mod-assets.py`` writes :data:`COPY` into
``plugin/hooks/generated/copy.js`` and a test keeps the two in step.
Keeping the words here means the house-style checks
(``docs/writing-help.md``, ``tests/test_mod_copy.py``) cover them like
the dashboard's.

Each value is one template. ``{placeholders}`` are filled by the mod and
are single lower-case words. Amounts arrive already phrased for the
billing mode (dollars on API billing; a share of the usage limit on a
subscription, as :class:`claudeglass.units.Units` phrases it), so no
template writes a bare "$". Token counts arrive as "178k".

The coaching notes the mod delivers in place of the capture hook keep
their own words in ``capture_catalogue.COACHING_TIP``; these are the
mod's own.
"""

from __future__ import annotations

#: Every template the mod shows, by id. Ids are dotted: the surface, then
#: what the words are for.
COPY: dict[str, str] = {
    # -- Amounts, for the billing mode ------------------------------------
    "amount.share": "about {share} of your weekly limit",
    "amount.list": "{dollars} list-price equivalent",
    "amount.est": "(est.)",
    # -- How full the context is, by state ---------------------------------
    "state.plenty": "Plenty of room",
    "state.filling": "Filling",
    "state.soon": "Compact soon",
    "state.next": "Compacting next",
    # -- The band above the prompt ------------------------------------------
    "band.full": "{pct}% full",
    "band.runway": "about {replies} replies before Claude summarises",
    "band.runway.one": "about 1 reply before Claude summarises",
    "band.point": "Claude summarises at {tokens}",
    "band.tokens": "{tokens} of {point}",
    "band.cost": "{amount} this session",
    "band.change": "(+{amount})",
    "band.usual.under": "below your usual {amount}",
    "band.usual": "your usual {amount}",
    "band.budget": "{pct}% of your {amount} budget",
    "band.cache.ending": "resend soon: cheap for {time} more",
    "band.cache.cold": "cache expired (est.): the next send costs about {amount}",
    "band.pace": "5-hour limit {pct}% used, runs out about {time} at this pace",
    "band.agents": "{count} agents working",
    "band.agents.one": "1 agent working",
    "band.missing": "{count} things may be missing from the summary",
    "band.missing.one": "1 thing may be missing from the summary",
    "band.open": "Glass",
    "band.review": "Review",
    "band.review.before": "Review before compacting",
    # -- The spinner while Claude works -------------------------------------
    "spinner.suffix": "{pct}% full · +{amount}",
    # -- The hint line under the prompt -------------------------------------
    "hint.send": "Sending costs about {amount}",
    "hint.cold": "Idle {minutes} min, so the cache expired (est.): this send costs about {amount}",
    "hint.cold.clear": "/clear first costs about {amount}",
    "hint.topic": "New topic? /clear first makes every reply cheaper",
    # -- Notes at the end of a reply ----------------------------------------
    "note.lead": "Glass",
    "note.runway": "About {replies} replies before Claude summarises. If this task is done, /compact or /clear now.",
    "note.result": "That {tool} output was about {tokens}, and every later reply re-reads it. Next time, ask for only the part you need.",
    "note.switch": "Switching model made that reply cost about {amount} extra. Pick one model per session where you can.",
    "note.effort": "Thinking was {pct}% of output over the last {replies} replies. For simple edits, a lower effort costs less.",
    "note.compactions": "Claude has summarised this session {count} times. Starting fresh with /clear between tasks keeps replies cheaper.",
    "note.cold.compaction": "That summary rebuilt the cache, about {amount}. Compacting while the cache is warm costs less.",
    # -- Lines in the conversation ------------------------------------------
    "log.rebuild": "Cache rebuild: about {tokens} written again, about {amount}. Likely cause: {cause}.",
    "log.rebuild.expected": "Cache rebuild after {cause}, as expected: about {amount}.",
    "log.compacted": "Claude summarised the conversation. {missing}",
    "log.compacted.ok": "Everything on your keep list is in the summary.",
    "log.compacted.missing": "{count} things you said may be missing.",
    "log.compacted.missing.one": "1 thing you said may be missing.",
    "log.compacted.tokens": "{before} became {after}, about {amount}.",
    "cause.limit": "a usage-limit wait",
    "cause.compaction": "the conversation summary (compaction)",
    "cause.expiry": "the cache lifetime (TTL) running out",
    "cause.prefix": "a change near the start of the context, such as the model, effort or tools",
    "cause.unknown": "not known",
    # -- The status line, urgent only ---------------------------------------
    "status.limit": "5-hour limit runs out in about {minutes} min at this pace",
    "status.compact": "About 1 reply before Claude summarises",
    # -- The /glass pane ----------------------------------------------------
    "pane.title": "Glass",
    "pane.command": "Show how full the context is, what this session costs and what your agents are doing",
    "pane.opened": "Glass opened.",
    "pane.now": "Now",
    "pane.agents": "Agents",
    "pane.compaction": "Compaction",
    "pane.instructions": "Instructions",
    "pane.more": "More detail",
    "pane.less": "Less detail",
    "pane.dashboard": "Open in dashboard",
    "pane.link": "Copy link",
    "pane.help": "What these mean",
    "pane.copy": "Copy",
    "pane.copied": "Copied.",
    "now.headline": "Context is {pct}% full. About {replies} replies before Claude summarises.",
    "now.headline.point": "Context is {pct}% full. Claude summarises at {tokens}.",
    "now.advice.soon": "If this task is done, now is a good moment to /compact.",
    "now.advice.next": "Claude summarises after the next reply. Finish the task, or /compact with a focus now.",
    "now.advice.plenty": "Plenty of room. Nothing to do.",
    "now.mark": "summarises here",
    "now.filling": "What's filling it",
    "now.part.conversation": "Conversation so far",
    "now.part.output": "Tool output kept in context",
    "now.part.startup": "Startup context",
    "now.part.startup.detail": "Claude Code and your instructions",
    "now.part.free": "Room left",
    "now.largest": "largest: {name} {tokens}",
    "now.session": "This session {amount} · {replies} replies",
    "now.waiting": "Figures show after Claude's first reply in this session.",
    "now.chart": "Context after each reply, with the cost of each reply",
    "now.spend": "Where this session's spend goes",
    "now.heaviest": "Heaviest single items",
    "now.limit": "5-hour limit",
    "now.compactions": "Conversation summaries",
    "now.compactions.none": "None yet in this session.",
    "agents.headline": "{count} agents working · agents are {pct}% of this session's cost",
    "agents.headline.idle": "No agents working · agents are {pct}% of this session's cost",
    "agents.none": "No agents have run in this session yet. They show here when Claude starts one.",
    "agents.details": "Details",
    "agents.task": "Task prompt",
    "agents.tools": "Tools used",
    "agents.background": "in the background",
    "agent.searching": "searching",
    "agent.reading": "reading files",
    "agent.editing": "editing files",
    "agent.running": "running commands",
    "agent.fetching": "reading the web",
    "agent.working": "working",
    "agent.done": "done",
    "agent.failed": "stopped with an error",
    "instructions.headline": "What this session loaded, in order",
    "instructions.none": "Nothing loaded yet. Instructions show here as Claude Code adds them.",
}


#: What the pane's "What these mean" shows: one entry per figure, a title
#: and a body that says what it is, why it's that size and what to do.
GLOSSARY: dict[str, dict[str, str]] = {
    "full": {
        "title": "How full the context is",
        "body": "The share of the room Claude Code allows before it summarises the conversation. It's the figure /context shows.",
    },
    "runway": {
        "title": "Replies before Claude summarises",
        "body": "The room left, divided by how much each recent reply added. Finish the task, or /compact with a focus, before it runs out.",
    },
    "conversation": {
        "title": "Conversation so far",
        "body": "Your messages and Claude's replies. It grows with every reply until Claude summarises it or you /clear.",
    },
    "output": {
        "title": "Tool output kept in context",
        "body": "Command output, search results and files Claude read stay in the conversation, and every later reply re-reads them. "
        "It's estimated from how much each tool call added. Ask for only the part you need.",
    },
    "startup": {
        "title": "Startup context",
        "body": "What every request starts with: Claude Code's system prompt and tools, your CLAUDE.md files and your skills. "
        "Trim instructions or turn off unused MCP servers to shrink it.",
    },
    "cost": {
        "title": "Cost this session",
        "body": "What this session's requests cost at list price, from Claude Code's own figures. "
        "On a subscription it's shown as a share of your weekly limit.",
    },
    "cache": {
        "title": "Cache lifetime (TTL)",
        "body": "Claude Code keeps your context in a cache for 5 minutes or 1 hour after each reply. "
        "A send inside that time re-reads it cheaply. After it, the whole context is written again.",
    },
    "rebuild": {
        "title": "Cache rebuild",
        "body": "The whole context written to the cache again, at the cache write price. "
        "It's expected after a summary or a usage-limit wait. Otherwise a model, effort or tool change usually causes it.",
    },
    "usual": {
        "title": "Your usual",
        "body": "The median cost of your own sessions, from ClaudeGlass's history. Nobody else's sessions count.",
    },
    "pace": {
        "title": "5-hour limit pace",
        "body": "How fast this window's usage limit is going, against the time left before it resets. "
        "Ahead of pace, a cheaper model or a lower effort makes it last.",
    },
}


__all__ = ["COPY", "GLOSSARY"]

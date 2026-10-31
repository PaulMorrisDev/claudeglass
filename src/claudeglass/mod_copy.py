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
    "amount.share.limit": "about {share} of your 5-hour limit",
    "amount.list": "{dollars} list-price equivalent",
    "amount.est": "(est.)",
    # -- How full the context is, by state ---------------------------------
    # -- The band above the prompt ------------------------------------------
    "band.full": "{pct}% full",
    "band.point": "Claude summarises at {tokens}",
    "band.auto.off": "Claude won't summarise by itself",
    "band.open": "Glass",
    # -- The health line (band) and the Checkpoint pane -----------------------
    "health.fine": "Fine",
    "health.tight": "Tight",
    "health.review": "Review",
    "health.kept": "Kept",
    "health.replies": "about {replies} replies before a summary",
    "health.replies.one": "about 1 reply before a summary",
    "health.lost": "Summary lost {missing} of {total} items",
    "health.kept.all": "Summary kept all {total} items",
    "health.kept.one": "Summary kept the 1 item",
    "health.summarise": "Summarise, keeping {count} items",
    "health.summarise.one": "Summarise, keeping 1 item",
    "health.summarise.none": "Summarise now",
    "health.review.hold": "Review before summarising",
    "health.readd": "Re-add {count} items",
    "health.readd.one": "Re-add 1 item",
    "health.busy": "Summarising…",
    "health.busy.reply": "A reply is running. Summarise when it ends.",
    "health.lost.toast": "The summary lost {missing} of {total} items. Press Re-add to put them back.",
    "checkpoint.title": "Checkpoint",
    "checkpoint.startup.title": "Startup",
    "checkpoint.context": "Context {pct}% full",
    "checkpoint.summarise": "Summarise now, keeping {count} items",
    "checkpoint.summarise.one": "Summarise now, keeping 1 item",
    "checkpoint.summarise.none": "Summarise now",
    "checkpoint.summarise.running": "A summary is running.",
    "checkpoint.summarise.skipped": "The summary was held: {reason}",
    "checkpoint.keep.title": "Keep through the next summary",
    "checkpoint.keep.count": "{count} items",
    "checkpoint.keep.count.one": "1 item",
    "checkpoint.keep.empty": "Nothing to keep yet. Pins and instructions appear here as you work.",
    "checkpoint.pin.last": "Pin the last thing I said",
    "checkpoint.unpin": "Unpin",
    "checkpoint.show.more": "Show {count} more",
    "checkpoint.show.fewer": "Show fewer",
    "checkpoint.kind.pin": "pinned",
    "checkpoint.kind.instruction": "instruction",
    "checkpoint.kind.decision": "decision",
    "checkpoint.kind.error": "error",
    "checkpoint.kind.task": "task",
    "checkpoint.kind.file": "file",
    "checkpoint.kind.value": "value",
    "checkpoint.last.title": "Last summary",
    "checkpoint.trigger.auto": "automatic",
    "checkpoint.trigger.manual": "you asked",
    "checkpoint.trigger.plugin": "pressed here",
    "checkpoint.last.counts": "kept {kept} · lost {missing}",
    "checkpoint.last.counts.readded": "kept {kept} · lost {missing} · re-added {readded}",
    "checkpoint.last.ok": "The summary kept everything you asked for.",
    "checkpoint.receipt.referenced": "re-added, referenced ✓",
    "checkpoint.receipt.waiting": "re-added, not yet seen",
    "checkpoint.receipt.unreferenced": "re-added, not referenced",
    "checkpoint.readd.busy": "Re-adding…",
    "checkpoint.readd.at": "Re-added {time}",
    "checkpoint.readd.done": "Claude is told these again with its next reply.",
    "checkpoint.check": "Check with a model",
    "checkpoint.check.busy": "Checking…",
    "checkpoint.check.at": "Checked · about {tokens} tokens",
    "checkpoint.check.done": "The model checked {count} items: {found} found, {partly} partly, {missing} missing.",
    "checkpoint.check.failed": "The model could not check, so the quick check stands.",
    "checkpoint.earlier": "Show {count} earlier summaries",
    "checkpoint.earlier.one": "Show 1 earlier summary",
    "checkpoint.earlier.hide": "Hide earlier summaries",
    "checkpoint.earlier.row": "{time} · {trigger} · kept {kept} · lost {missing}",
    "checkpoint.hold": "Hold auto-summaries for review: {state}",
    "checkpoint.hold.on": "on",
    "checkpoint.hold.off": "off",
    "checkpoint.hold.detail": "When Claude is about to summarise on its own with room to spare, wait once so you can review.",
    "checkpoint.startup": "Open Startup",
    "checkpoint.command": "Open the Checkpoint pane: what the next summary must keep, and what the last one lost",
    # -- The spinner while Claude works -------------------------------------
    "spinner.suffix": "{pct}% full · +{amount}",
    # -- The hint line under the prompt -------------------------------------
    "hint.send": "Sending costs about {amount}",
    "hint.cold": "Idle {minutes} min, so the cache expired (est.): this send costs about {amount}",
    "hint.cold.clear": "/clear first costs about {amount}",
    "hint.topic": "New topic? /clear first makes every reply cheaper",
    # -- Notes at the end of a reply ----------------------------------------
    "note.lead": "Glass",
    # -- Lines in the conversation ------------------------------------------
    "log.compacted": "Claude summarised the conversation. {missing}",
    "log.compacted.missing": "{count} things you said may be missing.",
    "log.compacted.missing.one": "1 thing you said may be missing.",
    "log.compacted.tokens": "{before} became {after}, about {amount}.",
    # -- The status line, urgent only ---------------------------------------
    "status.limit": "5-hour limit runs out in about {minutes} min at this pace",
    "status.compact": "About 1 reply before Claude summarises",
    # -- The /glass pane ----------------------------------------------------
    "pane.opened": "Glass opened.",
    "pane.dashboard": "Open in dashboard",
    "pane.help": "What these mean",
    "pane.help.hide": "Hide the meanings",
    "pane.detail.on": "Detailed view is on.",
    "pane.detail.off": "Detailed view is off.",
    "pane.failed": "Glass could not open.",
    # -- Agents page and agent rows (more) ----------------------------------
    # -- Coaching the capture hook used to give after a send (coach.ts) -----
    # Same advice and figures as capture_catalogue.COACHING_TIP, worded for a
    # message that is still a draft; plan_fresh and the rest use the tips as
    # they are.
    "hint.poll": "Background work is still running. Asking how it's going re-reads {tokens} tokens. {how}",
    "hint.paste": "Large paste, about {tokens} tokens, and every later reply reads it again. If it's a log, paste only the part that matters, or give the path to a file.",
    "hint.drip": "{count} small changes in a row, each its own message. Each one re-reads the whole session, about {tokens} tokens. Listing the rest in one message costs less.",
    # -- Compaction review: the keep list, the summary check, the drafts -----
    "log.compacted.review": "Press Re-add above the prompt to put them back.",
    "compaction.more": "and {count} more",
    "compaction.addback": "Before compaction we agreed: {items}.",
    "compaction.keep": "/compact Keep: {items}",
    "compaction.instruct": "Keep: {items}",
    "compaction.label.files": "files we changed:",
    "compaction.label.values": "values:",
    "compaction.review": "Review",
    "compaction.held.skip": "Glass held this summary for you to review. Claude summarises at the next chance.",
    "compaction.held.toast": "Summary held once. Review what it must keep, then compact when you are ready.",
    "compaction.pin": "Pin",
    "compaction.pin.done": "Pinned. It stays on your keep list until /clear.",
    "compaction.pin.empty": "Say what to pin, for example /glass pin never use port 8765",
    "compaction.pin.full": "You have {count} pins already. Remove one first with /glass unpin and a few of its words.",
    "compaction.unpin.empty": "Say what to unpin, for example /glass unpin port 8765, or /glass unpin all",
    "compaction.unpin.one": "Unpinned 1 pin.",
    "compaction.unpin.many": "Unpinned {count} pins.",
    "compaction.unpin.none": "No pin has those words.",
    "compaction.unpin.nopins": "You have no pins.",
    # -- Startup page: what is in every request before the person types ----
    # Sizes are estimates (characters over the calibrated characters per
    # token, shared out of Claude Code's count when it gives one). The page
    # says so once, in its footnote or in its lead, never on a row.
    "startup.none": "Nothing has loaded yet. What Claude reads for this session shows here once it starts.",
    "startup.lead": "Every request starts with {tokens} tokens of setup.",
    "startup.lead.est": "Every request starts with about {tokens} tokens of setup (est.).",
    "startup.advice.mcp": "The {server} tools add {tokens} to every request. Turn the server off in /mcp when you don't need it.",
    "startup.advice.tool": "The {tool} tool adds {tokens} to every request and Claude hasn't used it this session. If you never use it, add it to permissions.deny.",
    "startup.advice.file": "{name} adds {tokens} to every request. Trim it if parts are out of date.",
    "startup.control": "You control",
    "startup.mcp": "MCP servers ({count})",
    "startup.files": "CLAUDE.md and memory",
    "startup.mcp.instructions": "Instructions the servers gave Claude",
    "startup.tools": "Built-in tools ({count})",
    "startup.tools.note": "To drop a tool you never use, add its name to permissions.deny in your settings. It takes effect in your next session.",
    "startup.more": "and {count} more",
    "startup.tools.more": "{count} more",
    "startup.deferred.tools": "{count} more load only when asked for: {names}",
    "startup.deferred.tools.one": "1 more loads only when asked for: {names}",
    "startup.deferred.servers": "{count} more servers load only when asked for: {names}",
    "startup.deferred.servers.one": "1 more server loads only when asked for: {names}",
    "startup.skills": "Skills list",
    "startup.plugin": "Plugin tools",
    "startup.own": "Claude Code's own",
    "startup.own.note": "Claude Code's built-in instructions. You can't change these.",
    "startup.own.instructions": "Instructions and system prompt",
    "startup.own.note.desktop": "Claude Code's built-in instructions and the servers the desktop app brings. You can't change these.",
    "startup.later": "Added as you work",
    "startup.later.note": "/clear and /compact empty these.",
    "startup.later.files": "Files loaded as Claude reached them",
    "startup.later.skills": "Skills Claude used",
    "startup.later.reminders": "Reminders from Claude Code",
    "startup.later.attached": "Files and hook notes added to your messages",
    "startup.later.mark": "{name} (loads later)",
    "startup.estimates": "Sizes are estimates, shared out by size of text.",
    "startup.compare": "Claude Code counts {theirs} of startup context in /context. This list adds up to about {mine}.",
    "startup.compare.why": "The list estimates from text at a fixed rate. It leaves out the input schemas of tools, which are most of their size.",
    "startup.agents": "Agents and CLAUDE.md",
    "startup.agents.got": "Received it: {types}",
    "startup.agents.skipped": "Did not receive it: {types}",
    # -- Prompt help (step 8) ------------------------------------------------
    # Draft checks on the hint line, /glass tighten, the guard and the
    # session budget. "hint.topic" (above) is the topic hint without a
    # figure; "hint.topic.save" is the same with what each reply would save.
    "hint.topic.save": "New topic? /clear first makes every reply about {amount} cheaper",
    "hint.nopaths": "No file named yet. Naming the file or function saves Claude a search.",
    "hint.readall": "Reading everything fills the context fast. Name the folders or files that matter instead.",
    "tighten.title": "Tighten",
    "tighten.headline": "A shorter version of your draft",
    "tighten.counts": "Your draft is {before} words. The shorter version is {after}.",
    "tighten.legend": "Lines marked - are yours. Lines marked + are the shorter version. Nothing is sent.",
    "tighten.use": "Use this",
    "tighten.keep": "Keep mine",
    "tighten.cost": "Haiku used {tokens} tokens.",
    "tighten.opened": "A shorter version of your draft is open in the Glass pane. Nothing is sent.",
    "tighten.empty": "Your prompt box is empty, so there is nothing to tighten.",
    "tighten.long": "That draft is too long to tighten here. Shorten it by hand first.",
    "tighten.same": "Your draft is already tight. Haiku found nothing to cut.",
    "tighten.failed": "Haiku could not rewrite the draft, so it is unchanged.",
    "tighten.skip": "Tightening isn't available in this version of Claude Code.",
    "tighten.nofill": "The prompt box couldn't take the text. Keep yours, or copy the shorter version from the pane.",
    "guard.on": "Guard is on. Glass asks before a costly send.",
    "guard.off": "Guard is off.",
    "guard.header": "Costly send",
    "guard.cold": "The cache expired after {minutes} min idle, so sending costs about {amount}. Clearing first costs about {fresh}. What now?",
    "guard.cold.plain": "The cache expired after {minutes} min idle, so this send re-reads the whole session at the higher price. What now?",
    "guard.topic": "This looks like a new topic, and the session carries about {tokens} tokens. A fresh start makes every reply about {amount} cheaper. What now?",
    "guard.topic.plain": "This looks like a new topic, and the session carries about {tokens} tokens. A fresh start makes every reply cheaper. What now?",
    "guard.send": "Send",
    "guard.clear": "Clear first",
    "guard.cancel": "Cancel",
    "guard.dropped": "Not sent. Your message is still in the box.",
    "guard.dropped.clear": "Not sent. Press Enter to clear; your message comes back after.",
    # -- Review fixes 2 ------------------------------------------------------
    # The compaction check counts only what the user said (instructions,
    # decisions and pins); files, values, tasks and errors wait in the full
    # list. The Instructions page's startup total is Claude Code's own count.
    "log.compacted.ok.said": "Everything you said is in the summary.",
    # -- Review fixes 3 ------------------------------------------------------
    # The status line when no reply is left before Claude summarises; the Detailed boundary line
    # when the summary can't be priced; a rewrite that used its whole allowance.
    "status.compact.now": "Claude summarises on the next reply",
    "log.compacted.tokens.plain": "{before} became {after}.",
    "tighten.cut": "Haiku's rewrite was cut short, so it was not used. Try a shorter draft.",
}


#: What the pane's "What these mean" shows: one entry per figure, a title
#: and a body that says what it is, why it's that size and what to do.
GLOSSARY: dict[str, dict[str, str]] = {
    "full": {
        "title": "How full the context is",
        "body": "The share of the context Claude Code allows, as /context shows it. "
        "Claude summarises a little before 100%.",
    },
    "runway": {
        "title": "Replies before Claude summarises",
        "body": "The room left, divided by how much each recent reply added. Finish the task, or /compact with a focus, before it runs out.",
    },
    "startup": {
        "title": "Startup context",
        "body": "What every request starts with: Claude Code's system prompt and tools, your CLAUDE.md files and your skills. "
        "Trim instructions or turn off unused MCP servers to shrink it.",
    },
    "summary": {
        "title": "Summary check",
        "body": "When Claude summarises, it can drop things you said. Glass compares the summary with your keep list "
        "and marks each item kept, partly kept or lost. Re-add puts lost items back.",
    },
    "keep": {
        "title": "Keep list",
        "body": "The instructions, decisions, files and values the next summary is told to carry. "
        "Untick an item to leave it out. Pin a line to keep it until you /clear.",
    },
    "receipt": {
        "title": "Re-add receipt",
        "body": "After you re-add lost items, Glass watches the next ten replies. "
        "Referenced means a reply used the item; not referenced means none did.",
    },
}


__all__ = ["COPY", "GLOSSARY"]
